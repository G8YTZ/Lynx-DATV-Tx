#!/usr/bin/env python3
"""Lynx-Tx priority multiplexer - sound is never late because of pictures.

Reads a transport stream (ffmpeg's remux, NOT constant-rate) on stdin and
writes a constant-rate stream on stdout, choosing each 188-byte slot by a
fixed rule:

    1. tables (PAT, PMT, SDT...)       always first
    2. sound that is due                 before any picture
    3. picture that may be sent          (no more than --slack ahead of need)
    4. otherwise a null packet           to hold the rate

It writes the clock reference (PCR) itself, from the slot count, so the
timing is exact: the stream clock advances 188*8/rate seconds per slot.

It also writes the time of day, for receivers' clocks and the EPG: a TDT
(UTC) every 5 s and a TOT (the local offset, and when it next changes:
BST in the UK) every 10 s, on PID 0x14, from the ROCK's clock (chrony keeps
it true). The offset and the date of the next change come from the system's
time zone database, not a rule written here, so they are right anywhere.
When the encoder bursts beyond its share, pictures queue and arrive later -
a receiver repeats a frame - but sound keeps its place. With ffmpeg's own
constant-rate muxing, sound queued behind the burst and dropped out.

    tsprio.py --rate 2908959 --slack 0.6 --video-pid 0x101 --audio-pid 0x102

The rate must be the channel's exact transport-stream rate; the pacing
stage downstream (tsp regulate) sets the real-time speed by back-pressure.
A summary goes to stderr every 10 seconds.
"""
import argparse
import datetime
import os
import select
import sys
import time
from collections import deque

ap = argparse.ArgumentParser()
ap.add_argument("--rate", type=float, required=True, help="transport stream rate, b/s")
ap.add_argument("--slack", type=float, default=0.6, help="most a picture may be sent ahead, s")
ap.add_argument("--audio-lead", type=float, default=0.12,
                help="most sound may be sent ahead, s (its decoder buffer is tiny)")
ap.add_argument("--video-pid", type=lambda v: int(v, 0), default=0x101)
ap.add_argument("--audio-pid", type=lambda v: int(v, 0), default=0x102)
ap.add_argument("--pcr-every", type=float, default=0.035, help="PCR interval, s")
ap.add_argument("--pace", type=float, default=1.002,
                help="never run the stream clock faster than this times real time")
ap.add_argument("--tz", default="Europe/London",
                help="local time zone for the TOT; empty to send no time at all")
ap.add_argument("--tz-country", default="",
                help="ISO 3166 country code in the TOT (default: from --tz, GBR for London)")
ap.add_argument("--tdt-every", type=float, default=5.0, help="TDT interval, s")
ap.add_argument("--tot-every", type=float, default=10.0, help="TOT interval, s")
a = ap.parse_args()

HZ = 90000.0
WRAP = 1 << 33
SLOT = 188 * 8 / a.rate * HZ                   # 90 kHz ticks per slot
SLACK = a.slack * HZ
ALEAD = a.audio_lead * HZ
PCR_EVERY = a.pcr_every * HZ
NULL = b"\x47\x1f\xff\x10" + b"\xff" * 184


def log(*x):
    print("[prio]", *x, file=sys.stderr, flush=True)


def ts90(b):
    return (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | \
           (b[3] << 7) | (b[4] >> 1)


class Elementary:
    """Packets of one stream, each tagged with when it is due (90 kHz)."""

    def __init__(self, use_dts):
        self.q = deque()
        self.due = None                         # of the PES being received
        self.prev = None
        self.unwrapped = None
        self.use_dts = use_dts

    def add(self, p, start, payload_at):
        if start and payload_at + 14 <= 188:
            pes = p[payload_at:]
            if pes[0:3] == b"\x00\x00\x01":
                flags = pes[7] >> 6
                raw = None
                if flags == 3:
                    raw = ts90(pes[14:19] if self.use_dts else pes[9:14])
                elif flags == 2:
                    raw = ts90(pes[9:14])
                if raw is not None:
                    if self.prev is None:
                        self.unwrapped = raw
                    else:
                        d = (raw - self.prev) % WRAP
                        if d > WRAP // 2:
                            d -= WRAP
                        self.unwrapped += d
                    self.prev = raw
                    self.due = float(self.unwrapped)
        if self.due is not None:
            self.q.append((self.due, p))


tables = deque()
video = Elementary(use_dts=True)
audio = Elementary(use_dts=False)
inbuf = b""
eof = False
fd = sys.stdin.fileno()
out = sys.stdout.buffer
pending = []                                    # output not yet written

T = None                                        # stream clock, 90 kHz, at this slot
last_pcr = -1e18
video_cc = None
stats = {"slots": 0, "null": 0, "audio": 0, "video": 0, "tables": 0,
         "audio_late": 0, "video_late": 0, "worst_video": 0.0, "resync": 0,
         "worst_audio": 0.0, "input_gap": 0.0}
last_input = None
last_report = time.monotonic()


def ingest(chunk):
    global inbuf
    inbuf += chunk
    n = len(inbuf) // 188
    for i in range(n):
        p = inbuf[i * 188:(i + 1) * 188]
        if p[0] != 0x47:
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid == 0x1FFF:
            continue
        afc = (p[3] >> 4) & 0x3
        at = 4 + (1 + p[4] if afc & 0x2 else 0)
        start = bool(p[1] & 0x40) and bool(afc & 0x1)
        if pid == a.video_pid:
            video.add(p, start, at)
        elif pid == a.audio_pid:
            audio.add(p, start, at)
        else:
            tables.append(p)
    inbuf = inbuf[n * 188:]


def flush():
    global pending
    if pending:
        out.write(b"".join(pending))
        out.flush()
        pending = []


def read(timeout):
    """Take whatever input is ready, waiting at most `timeout` seconds."""
    global eof
    if eof:
        return False
    if timeout > 0:
        flush()                                 # never sit on output while waiting
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return False
    chunk = os.read(fd, 188 * 512)
    if not chunk:
        eof = True
        return False
    global last_input
    now = time.monotonic()
    if last_input is not None:
        stats["input_gap"] = max(stats["input_gap"], now - last_input)
    last_input = now
    ingest(chunk)
    return True


# -- Time of day: TDT and TOT (ETSI EN 300 468, 5.2.5 and 5.2.6) ------------
TIME_PID = 0x0014
COUNTRIES = {"Europe/London": "GBR", "Europe/Dublin": "IRL", "Europe/Belfast": "GBR",
             "Europe/Paris": "FRA", "Europe/Brussels": "BEL", "Europe/Amsterdam": "NLD",
             "Europe/Berlin": "DEU", "Europe/Madrid": "ESP", "Europe/Rome": "ITA",
             "America/New_York": "USA", "America/Chicago": "USA", "America/Los_Angeles": "USA",
             "Australia/Melbourne": "AUS", "Australia/Sydney": "AUS", "Pacific/Auckland": "NZL"}
time_cc = 0
tz = None
if a.tz:
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(a.tz)
    except Exception as e:                      # no tzdata: UTC only, said once
        print(f"[prio] time zone {a.tz!r} unavailable ({e}): TDT only, no TOT",
              file=sys.stderr, flush=True)
COUNTRY = (a.tz_country or COUNTRIES.get(a.tz, "GBR")).upper()[:3].encode("ascii")


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


def utc40(dt):
    """MJD (16 bits) then hh mm ss in BCD: EN 300 468 annex C."""
    mjd = (dt.date() - datetime.date(1858, 11, 17)).days
    return bytes([mjd >> 8, mjd & 0xFF, bcd(dt.hour), bcd(dt.minute), bcd(dt.second)])


def offset16(td):
    """An offset as hh mm in BCD, and its polarity (1 = behind UTC)."""
    m = int(td.total_seconds()) // 60
    pol = 1 if m < 0 else 0
    m = abs(m)
    return bytes([bcd(m // 60), bcd(m % 60)]), pol


_change = {"until": None, "at": None, "next": None}


def next_change(now):
    """When the local offset next changes, and what it changes to. Found by
    asking the time zone database day by day, then to the second, so it
    holds wherever and whatever the rule. Cached until it passes."""
    if _change["until"] is not None and now < _change["until"]:
        return _change["at"], _change["next"]
    off = now.astimezone(tz).utcoffset()
    lo, at, nxt = now, None, off
    for day in range(1, 400):
        t = now + datetime.timedelta(days=day)
        if t.astimezone(tz).utcoffset() != off:
            hi = t
            while (hi - lo).total_seconds() > 1:
                mid = lo + (hi - lo) / 2
                if mid.astimezone(tz).utcoffset() == off:
                    lo = mid
                else:
                    hi = mid
            at = hi.replace(microsecond=0)
            nxt = at.astimezone(tz).utcoffset()
            break
        lo = t
    if at is None:                              # no change within a year
        at = (now + datetime.timedelta(days=399)).replace(microsecond=0)
    _change.update(until=min(at, now + datetime.timedelta(hours=1)), at=at, next=nxt)
    return at, nxt


def tdt_section(now):
    return bytes([0x70, 0x70, 0x05]) + utc40(now)


def tot_section(now):
    cur = now.astimezone(tz).utcoffset()
    at, nxt = next_change(now)
    cur16, pol = offset16(cur)
    nxt16, _ = offset16(nxt)
    desc = (COUNTRY + bytes([(0 << 2) | 0x02 | pol]) + cur16 + utc40(at) + nxt16)
    loop = bytes([0x58, len(desc)]) + desc
    body = utc40(now) + bytes([0xF0 | (len(loop) >> 8), len(loop) & 0xFF]) + loop
    length = len(body) + 4
    head = bytes([0x73, 0x70 | (length >> 8), length & 0xFF])
    crc = crc32_mpeg(head + body)
    return head + body + crc.to_bytes(4, "big")


def time_packet(section):
    global time_cc
    head = bytes([0x47, 0x40 | (TIME_PID >> 8), TIME_PID & 0xFF, 0x10 | time_cc])
    time_cc = (time_cc + 1) & 0x0F
    p = head + b"\x00" + section                # pointer_field 0: section starts here
    return p + b"\xff" * (188 - len(p))


def offset_text(td):
    m = int(td.total_seconds()) // 60
    return f"{'+' if m >= 0 else '-'}{abs(m) // 60}:{abs(m) % 60:02d}"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def pcr_bytes(t):
    base = int(t) % WRAP
    return bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
                  (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E, 0x00])


def stamp_pcr(p, t):
    """If this video packet carries a PCR, rewrite it to the slot time."""
    if (p[3] & 0x20) and p[4] >= 7 and (p[5] & 0x10):
        return p[:6] + pcr_bytes(t) + p[12:]
    return p


def pcr_packet(t):
    """An adaptation-only packet on the video PID carrying just a PCR.
    Its continuity counter repeats the last one, as the standard requires
    for a packet with no payload."""
    pid = a.video_pid
    head = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x20 | (video_cc & 0x0F)])
    af = bytes([183, 0x10]) + pcr_bytes(t)
    return head + af + b"\xff" * (188 - 4 - len(af))


wall = {"t": None, "T": None}


def hold_to_real_time():
    """The stream clock may not run ahead of the real one. Downstream (tsp
    regulate) bursts to catch up when it falls behind its schedule; feeding
    such a burst from a backlog of pictures raced the clock seconds ahead
    of real time and made everything - sound too - late.

    Real time is the monotonic clock, not the time of day (3 Nov 2026):
    after a power cut the ROCK boots with a stale clock and NTP then steps
    it. A step back would have looked like the stream running ahead, and
    this would have slept for the size of the step: dead air. Every timer
    in this file is an interval, so all of them use time.monotonic()."""
    if wall["t"] is None:
        wall["t"], wall["T"] = time.monotonic(), T
        return
    now = time.monotonic()
    ahead = (T - wall["T"]) / HZ / a.pace - (now - wall["t"])
    if ahead <= 0:
        # At or behind real time: move the reference here, so the
        # allowance never builds up. Measured from key-up, 0.6% grew to
        # seconds of credit over a long run, which one burst could spend.
        wall["t"], wall["T"] = now, T
    elif ahead > 0.02:
        flush()
        time.sleep(ahead)


def emit(p):
    global T
    hold_to_real_time()
    pending.append(p)
    T += SLOT
    stats["slots"] += 1
    if len(pending) >= 64:
        flush()


def report():
    global last_report
    now = time.monotonic()
    if now - last_report < 10:
        return
    last_report = now
    s = stats
    tot = max(1, s["slots"])
    log(f"10 s: slots {s['slots']}, sound {s['audio']}, picture {s['video']}, tables {s['tables']}, "
        f"null {100 * s['null'] / tot:.0f}%  |  sound late {s['audio_late']}, "
        f"picture late {s['video_late']} (worst {s['worst_video'] / HZ:.2f} s)"
        + (f", sound worst {s['worst_audio'] / HZ:.2f} s" if s["audio_late"] else "")
        + f"  |  longest input gap {s['input_gap'] * 1000:.0f} ms"
        + (f", resyncs {s['resync']}" if s["resync"] else ""))
    for k in s:
        s[k] = 0.0 if k in ("worst_video", "worst_audio", "input_gap") else 0


log(f"rate {a.rate:.0f} b/s, slack {a.slack} s, sound lead {a.audio_lead} s")
if a.tz:
    _n = utc_now()
    if tz is not None:
        _at, _nx = next_change(_n)
        log(f"time: TDT every {a.tdt_every:g} s, TOT every {a.tot_every:g} s, {a.tz} "
            f"({COUNTRY.decode()}) now UTC{offset_text(_n.astimezone(tz).utcoffset())}, "
            f"UTC{offset_text(_nx)} from {_at:%Y-%m-%d %H:%M} UTC")
    else:
        log(f"time: TDT every {a.tdt_every:g} s, no TOT")
next_tdt = next_tot = 0.0

# Start: wait for the first picture with a time, and start the clock a full
# slack before it, so the stream begins with its slack in hand.
while T is None:
    if not video.q:
        if not read(1.0) and eof:
            flush()
            sys.exit(0)
        continue
    T = video.q[0][0] - SLACK
    log("clock started")

try:
    while True:
        while read(0):                          # take all input that is ready
            pass
        report()

        # A jump in the source's timeline (a restart, a loop): resync.
        if video.q and abs(video.q[0][0] - T) > 10 * HZ:
            T = video.q[0][0] - SLACK
            wall["t"] = None
            stats["resync"] += 1

        if video_cc is not None and T - last_pcr >= PCR_EVERY:
            emit(pcr_packet(T))
            last_pcr = T
            continue
        if a.tz and time.monotonic() >= next_tdt:
            next_tdt = time.monotonic() + a.tdt_every
            emit(time_packet(tdt_section(utc_now())))
            continue
        if a.tz and tz is not None and time.monotonic() >= next_tot:
            next_tot = time.monotonic() + a.tot_every
            emit(time_packet(tot_section(utc_now())))
            continue
        if tables:
            emit(tables.popleft())
            stats["tables"] += 1
            continue
        if audio.q and audio.q[0][0] - T <= ALEAD:
            due, p = audio.q.popleft()
            if due < T:
                stats["audio_late"] += 1
                stats["worst_audio"] = max(stats["worst_audio"], T - due)
            emit(p)
            stats["audio"] += 1
            continue
        if video.q and video.q[0][0] - T <= SLACK:
            due, p = video.q.popleft()
            if due < T:
                stats["video_late"] += 1
                stats["worst_video"] = max(stats["worst_video"], T - due)
            video_cc = p[3] & 0x0F
            if (p[3] & 0x20) and p[4] >= 7 and (p[5] & 0x10):
                last_pcr = T
            emit(stamp_pcr(p, T))
            stats["video"] += 1
            continue
        if video.q:
            emit(NULL)                          # pictures are early: hold the rate
            stats["null"] += 1
            continue
        # Nothing to send: wait for input rather than run the clock ahead
        # of the source. A short wait, then nulls, so the stream never stops.
        if not read(0.2):
            if eof and not audio.q and not tables:
                break
            emit(NULL)
            stats["null"] += 1
finally:
    try:
        flush()
    except BrokenPipeError:
        pass
