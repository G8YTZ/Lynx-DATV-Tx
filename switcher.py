#!/usr/bin/env python3
"""Lynx-Tx switcher - an ATEM-style programme bus: Camera | Media.

One encoder that never stops. Both inputs run all the time and the cut is
made in front of the encoder, like a vision mixer:

  camera   : HDMI (NV16, the camera's own 4:2:2)   -> intervideosink "cam"
             no HDMI signal -> colour bars instead, and it keeps trying
  media    : a media PLAYER - video (hardware decode, straight to NV16,
             looped) or a still (decoded once, frozen) -> intervideosink "med"
             The clip can be changed while running, on air or not.
  programme: intervideosrc "cam" -+
             intervideosrc "med" -+-> input-selector -> RE-STAMP -> encoder -> mux -> stdout

The two inputs tick at slightly different phases, so a cut can make the
next frame earlier than the last, and the remux downstream patches that
badly ("non-monotonous DTS", "TS is invalid"). So straight after the
selector each buffer keeps its OWN timestamp - from the pipeline's single
clock, so picture and sound stay in sync - and is only nudged forward when
it would otherwise go backwards. (An earlier version re-stamped by counting
frames and samples: anything lost at a cut became permanent, and the sound
drifted further out with every cut.)

Sound is not switched at all: both inputs go into a MIXER with the off-air
one muted, so there is one continuous sound track on the pipeline clock and
a cut only changes which input is muted. Switching sound buffers left tiny
gaps at every cut, which the AAC encoder absorbed by its own count until it
lost lip sync.

BITRATE TRIM - a closed loop on the buffer. The hardware encoder follows
its target well on most pictures but overshoots for seconds when a scene
suddenly gets hard, draining the stream's slack (measured live by
tsbuffer.py into /tmp/lynx-buffer.json). Every half-second the switcher
reads that slack: below half of --slack it cuts the encoder's target 10%
(20% below a quarter), at most once a second, to a floor of half; above 85%
of --slack it eases back 5% every 3 s. Fast down, slow up - attack and release - so it never hunts.
With no reading for 5 s it drifts back to normal.

Control (Lynx-Tx calls these; the page at / is for stand-alone use):
  POST /take?src=cam|med        a clean cut
  POST /load?file=<name>        play <name> from --media-dir in the Media player
  POST /keyframe                a keyframe now (Lynx-Tx asks at every key-up)
  GET  /status                  JSON: on air, media, camera state

  python3 switcher.py --media-dir /srv/lynxdeck/media --media Ayo4k.mp4 [--out f.ts] [--sim]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from collections import deque
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--media-dir", default="/srv/lynxdeck/media")
ap.add_argument("--media", default="", help="clip to load at start (name in --media-dir)")
ap.add_argument("--start", choices=("cam", "med"), default="cam")
ap.add_argument("--out", default="", help="file; default is stdout")
ap.add_argument("--udp", default="",
                help="HOST:PORT - send the programme there instead of stdout. Lynx-Tx runs the "
                     "mixer this way all the time, so TX On/Off never re-opens HDMI in")
ap.add_argument("--seconds", type=int, default=0)
ap.add_argument("--bps", type=int, default=3_800_000)
ap.add_argument("--gop", type=int, default=25)
ap.add_argument("--qp-floor", type=int, default=34,
                help="finest QP the encoder may use (its floor when the picture is easy); "
                     "lower = sharper still detail, bigger keyframes")
ap.add_argument("--audio-bitrate", type=int, default=192000)
ap.add_argument("--width", type=int, default=3840)
ap.add_argument("--height", type=int, default=2160)
ap.add_argument("--video-device", default="/dev/video0")
ap.add_argument("--audio-device", default="hw:CARD=rockchiphdmiin,DEV=0")
ap.add_argument("--port", type=int, default=8090)
ap.add_argument("--sim", action="store_true", help="software stand-ins, any machine")
ap.add_argument("--fallback-image", default="",
                help="shown on the Camera input with no HDMI signal (name in --media-dir); bars if missing")
ap.add_argument("--slack", type=float, default=0.6,
                help="the stream's slack in seconds (ffmpeg -max_delay); the trim scales with it")
ap.add_argument("--rc", choices=("cbr", "buffer", "hybrid"), default="cbr",
                help="cbr: the encoder's own rate control; buffer: frame-by-frame QP (ratectl.py); "
                     "hybrid: the encoder's rate control, with ratectl.py raising its QP floor")
ap.add_argument("--rc-rate", type=float, default=0,
                help="with --rc buffer/hybrid: the picture's real channel rate, b/s "
                     "(what sound and tables leave); default: --bps")
ap.add_argument("--rc-target", type=float, default=0.3,
                help="with --rc buffer/hybrid: how full (fraction of the slack) the controller "
                     "lets the buffer sit before it coarsens - lower reacts earlier")
ap.add_argument("--look", type=float, default=0.5,
                help="with --rc buffer/hybrid: look-ahead delay in seconds (0 = react only)")
ap.add_argument("--buffer-json", default="/tmp/lynx-buffer.json",
                help="the buffer monitor's live reading, for the bitrate trim")
a = ap.parse_args()

STILL_EXT = {".png", ".jpg", ".jpeg", ".bmp"}


def log(*x):
    print("[switcher]", *x, file=sys.stderr, flush=True)


Gst.init(None)
FRAME = Gst.SECOND // 25
W, H = a.width, a.height
VCAPS = f"video/x-raw,format=NV16,width={W},height={H},framerate=25/1"
ACAPS = "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2"
Q = "queue max-size-time=3000000000 max-size-bytes=0 max-size-buffers=0"
MEDIA_DIR = Path(a.media_dir)
loop = GLib.MainLoop()
state = {"on": None, "media": "", "kind": "", "camera": "starting",
         "media_state": "empty", "cuts": 0, "trim": 0}


# ---- inputs -------------------------------------------------------------
def silence():
    return f"audiotestsrc is-live=true wave=silence ! {ACAPS} ! interaudiosink channel=med_a"


def camera_desc(bars):
    if bars:
        # The fallback with no HDMI signal: the station test card if it is in
        # the media library, else plain 100% bars - either drawn ONCE and held.
        # Redrawing bars at 4K 25 times a second cost most of a core.
        card = MEDIA_DIR / a.fallback_image
        if a.fallback_image and card.is_file():
            q = str(card).replace('"', '\\"')
            still = f'filesrc location="{q}" ! decodebin'
        else:
            still = "videotestsrc num-buffers=1 pattern=smpte100"
        return (f"{still} ! videoconvert ! videoscale ! "
                f"video/x-raw,format=NV16,width={W},height={H},pixel-aspect-ratio=1/1 ! "
                f"imagefreeze is-live=true ! {VCAPS} ! intervideosink channel=cam "
                f"audiotestsrc is-live=true wave=silence ! {ACAPS} ! {Q} ! interaudiosink channel=cam_a")
    if a.sim:
        return (f"videotestsrc is-live=true pattern=ball ! videoconvert ! videoscale ! {VCAPS} ! "
                f"{Q} ! intervideosink channel=cam audiotestsrc is-live=true freq=440 ! {ACAPS} ! "
                f"{Q} ! interaudiosink channel=cam_a")
    return (f"v4l2src device={a.video_device} ! {VCAPS} ! {Q} ! intervideosink channel=cam "
            f"alsasrc device={a.audio_device} buffer-time=200000 latency-time=10000 ! "
            f"audio/x-raw,rate=48000,channels=2 ! audioconvert ! audioresample ! {ACAPS} ! "
            f"{Q} ! interaudiosink channel=cam_a")


def probe_file(path):
    """codec of the first real video stream, of the first audio, and 10-bit?"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,pix_fmt:stream_disposition=attached_pic",
                            "-of", "json", str(path)], capture_output=True, text=True, timeout=20)
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except Exception:
        return None, None, False
    v = [s for s in streams if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")]
    au = [s for s in streams if s.get("codec_type") == "audio"]
    ten_bit = bool(v) and "10" in (v[0].get("pix_fmt") or "")
    return (v[0]["codec_name"] if v else None), (au[0]["codec_name"] if au else None), ten_bit


AUDIO_DEC = {"aac": "aacparse ! avdec_aac", "mp3": "mpegaudioparse ! avdec_mp3",
             "ac3": "ac3parse ! avdec_ac3", "eac3": "ac3parse ! avdec_eac3"}


def media_desc(name):
    """The Media player's pipeline for one clip, or None and the reason."""
    path = MEDIA_DIR / name
    if not path.is_file():
        return None, "no such file"
    quoted = str(path).replace('"', '\\"')
    if path.suffix.lower() in STILL_EXT:
        return (f'filesrc location="{quoted}" ! decodebin ! videoconvert ! videoscale ! '
                f"video/x-raw,format=NV16,width={W},height={H},pixel-aspect-ratio=1/1 ! "
                f"imagefreeze is-live=true ! {VCAPS} ! intervideosink channel=med "
                + silence()), "still"
    vcodec, acodec, ten_bit = probe_file(path)
    parser = {"hevc": "h265parse", "h264": "h264parse"}.get(vcodec)
    if not parser:
        return None, f"{vcodec or 'no video'} cannot be decoded in hardware"
    if ten_bit:
        return None, "10-bit - export as 8-bit (H.265 Main)"
    if a.sim:
        dec = f"avdec_{'h265' if vcodec == 'hevc' else 'h264'} ! videoconvert ! videoscale ! {VCAPS}"
    else:
        # The decoder's own converter writes NV16 at programme size: no CPU.
        dec = f"mppvideodec format=NV16 width={W} height={H} ! {VCAPS}"
    desc = (f'filesrc location="{quoted}" ! qtdemux name=d '
            f"d.video_0 ! {Q} ! {parser} ! {dec} ! intervideosink channel=med ")
    if acodec in AUDIO_DEC:
        desc += (f"d.audio_0 ! {Q} ! {AUDIO_DEC[acodec]} ! audioconvert ! audioresample ! "
                 f"{ACAPS} ! interaudiosink channel=med_a")
    else:
        desc += silence()
    return desc, "video"


# ---- programme ------------------------------------------------------------
if a.sim:
    encode = (f"videoconvert ! video/x-raw,format=I420 ! x265enc name=enc "
              f"bitrate={a.bps // 1000} speed-preset=ultrafast key-int-max={a.gop} ! "
              f"video/x-h265,profile=main")
else:
    # A hard ceiling, and the freedom to reach it: on heavy motion the
    # plugin's default QP limit stopped it shrinking frames, and it ran up
    # to 38% over - more than the whole channel - which broke the stream.
    # bps-max caps it, qp-max 51 lets it go as coarse as H.265 allows, and
    # max-reenc re-encodes a frame that still comes out too big.
    # qp-min-i 30 caps KEYFRAMES on static pictures: with little else to
    # spend on, rate control poured the whole budget into each one (3.2 Mbit
    # on the test card, more than a second of channel), which domestic
    # receivers could not buffer. qp-min-i ALONE IS IGNORED in CBR: measured
    # on air, keyframes still grew to 2.5 Mbit. Starting the rate control at
    # QP 34 (qp-init) with the general floor (qp-min) holds them: ten test-
    # card keyframes in a row at 492-496 kbit, about 0.22 s of channel each.
    # Moving pictures at this bitrate run well above QP 34 anyway.
    encode = (f"mpph265enc name=enc rc-mode=cbr bps={a.bps} bps-max={a.bps} "
              f"qp-max=51 qp-max-i=51 qp-init={a.qp_floor} qp-min={a.qp_floor} qp-min-i={a.qp_floor} max-reenc=1 gop={a.gop} header-mode=each-idr")
    if a.rc == "buffer":
        # Frame-by-frame: fixed-QP mode, with ratectl.py choosing every
        # frame's QP from a model of the receiver's buffer.
        encode = (f"mpph265enc name=enc rc-mode=fixqp qp-init=36 qp-min=36 qp-max=36 "
                  f"qp-min-i=36 qp-max-i=36 gop={a.gop} header-mode=each-idr")
if a.udp:
    _h, _p = a.udp.rsplit(":", 1)
    # 7 packets a datagram (1316 bytes, the usual for TS over UDP); no sync,
    # the encoder is live already. Nobody listening (off air) is fine: the
    # datagrams are simply dropped.
    sink = f"udpsink host={_h} port={int(_p)} sync=false async=false buffer-size=4194304"
else:
    sink = f"filesink location={a.out}" if a.out else "fdsink fd=1"

LOOK = a.look if a.rc in ("buffer", "hybrid") else 0.0
LOOK_Q = (f"queue max-size-time=0 max-size-bytes=0 max-size-buffers=0 "
          f"min-threshold-time={int(LOOK * 1e9)} ! ") if LOOK > 0 else ""
prog = Gst.parse_launch(
    f"input-selector name=vsel ! identity name=vstamp ! {LOOK_Q}{VCAPS} ! {encode} ! "
    f"h265parse config-interval=-1 ! {Q} ! mux. "
    f"audiomixer name=amix ! {ACAPS} ! {LOOK_Q}audioconvert ! "
    f"avenc_aac bitrate={a.audio_bitrate} ! aacparse ! {Q} ! mux. "
    f"mpegtsmux name=mux{' alignment=7' if a.udp else ''} ! {sink} "
    f"intervideosrc name=src_cam channel=cam timeout=10000000000 ! {VCAPS} ! vsel. "
    f"intervideosrc name=src_med channel=med timeout=10000000000 ! {VCAPS} ! vsel. "
    f"interaudiosrc name=asrc_cam channel=cam_a ! {ACAPS} ! amix.sink_0 "
    f"interaudiosrc name=asrc_med channel=med_a ! {ACAPS} ! amix.sink_1")
vsel, amix, enc = (prog.get_by_name(n) for n in ("vsel", "amix", "enc"))

# Anchored, never backwards: each buffer keeps the clock's own timestamp
# unless that would step back past the previous one.
stamp = {"v_next": None}


def vstamp(pad, info):
    buf = info.get_buffer()
    _tick("prog")
    pts = buf.pts
    if pts == Gst.CLOCK_TIME_NONE:
        pts = stamp["v_next"] or 0
    elif stamp["v_next"] is not None and pts < stamp["v_next"]:
        pts = stamp["v_next"]                         # would go backwards: nudge
    buf.pts = buf.dts = pts
    buf.duration = FRAME
    stamp["v_next"] = pts + FRAME
    return Gst.PadProbeReturn.OK




prog.get_by_name("vstamp").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, vstamp)

# ---- frame-by-frame rate control, with look-ahead ------------------------------
# Pictures wait LOOK seconds (12 frames at 0.5 s) in a delay line before the
# encoder; each is scored as it enters - motion against the previous frame,
# and fine detail - on a 240x135 sample grid. Before each frame is encoded,
# ratectl plans one steady QP across the frames still in the line, so a
# strobe, a burst or a cut from a still into busy footage is coarsened for
# a few frames early and gently, instead of overflowing the buffer.
rc = None
rcs = {"n": 0, "fifo": deque(), "qp": None, "force_key": False, "last_log": time.time(),
       "skips": 0, "qps": [], "peak": 0.0, "ahead": deque(), "prev": None, "look_ok": 0}
# Hybrid: the encoder stays in CBR - its own rate control and adaptive
# quantisation, so smoke and backgrounds keep their detail - and the
# controller sets only its QP FLOOR. In easy passages the floor sits at 34,
# exactly as plain cbr; when the encoder would overshoot and make the stream
# late, the floor rises and holds it. This needs the patched plugin (live
# qp-min changes were ignored by the stock one: rc:fqp_min/max never set).
HYBRID = a.rc == "hybrid"
if a.rc in ("buffer", "hybrid"):
    import numpy as np
    from ratectl import BufferRC
    rc = BufferRC(a.rc_rate or a.bps, fps=25.0, slack=a.slack, gop=a.gop, guard=HYBRID,
                  target=max(0.05, min(0.7, a.rc_target)),
                  qp_min=a.qp_floor if HYBRID else 34,
                  qp_start=a.qp_floor if HYBRID else 36)
    HORIZON = max(1, int(round(LOOK * 25)))

    def set_qp(q):
        if q != rcs["qp"]:
            props = ("qp-min", "qp-min-i") if HYBRID else \
                    ("qp-init", "qp-min", "qp-max", "qp-min-i", "qp-max-i")
            for prop in props:
                enc.set_property(prop, q)
            rcs["qp"] = q

    def score(pad, info):
        """As each frame enters the delay line: how hard will it be?"""
        buf = info.get_buffer()
        try:
            ok, mi = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.PadProbeReturn.OK
            try:
                arr = np.frombuffer(mi.data, dtype=np.uint8)
                stride = max(W, len(arr) // (H * 2))           # NV16: luma, then chroma
                y = arr[:stride * H].reshape(H, stride)[::16, :W:16].astype(np.int16)
            finally:
                buf.unmap(mi)
            spatial = float(np.abs(np.diff(y, axis=1)).mean() + np.abs(np.diff(y, axis=0)).mean())
            prev = rcs["prev"]
            temporal = float(np.abs(y - prev).mean()) if prev is not None else spatial
            rcs["prev"] = y
            rcs["ahead"].append((buf.pts, 1.0 + temporal + 0.15 * spatial, 1.0 + spatial))
            rcs["look_ok"] += 1
        except Exception as e:                                  # never stop the picture
            if rcs["look_ok"] >= 0:
                log(f"rc: look-ahead scoring failed ({e}) - reacting only")
                rcs["look_ok"] = -1
        while len(rcs["ahead"]) > HORIZON * 4:
            rcs["ahead"].popleft()
        return Gst.PadProbeReturn.OK

    def future_for(pts):
        """The scores for this frame and the ones queued behind it."""
        ahead = rcs["ahead"]
        while ahead and ahead[0][0] < pts:
            ahead.popleft()
        if not ahead or ahead[0][0] != pts:
            return None
        out, since = [], rc.since_key
        for i, (_, aP, aK) in enumerate(list(ahead)[:HORIZON]):
            k = (i == 0 and rcs["force_key"]) or (since + 1 + i) % rc.gop == 0
            out.append((aP, aK, k))
        return out

    def rc_in(pad, info):
        """Before each frame enters the encoder: choose its QP, or skip it."""
        buf = info.get_buffer()
        n = rcs["n"]
        rcs["n"] += 1
        fut = future_for(buf.pts) if LOOK > 0 and rcs["look_ok"] >= 0 else None
        q = rc.choose(n, key=rcs["force_key"], future=fut)
        if q is None:
            rc.skipped_frame()
            rcs["skips"] += 1
            return Gst.PadProbeReturn.DROP          # the receiver repeats the last frame
        rcs["force_key"] = False
        set_qp(q)
        if os.path.exists("/tmp/rc-trace-on"):         # touch it to trace, rm to stop
            rcs.setdefault("trace", {})[n] = (
                f"{n},{int(bool(rc.inflight.get(n, (0, 0, False))[2]))},{q},{rc.why},"
                f"{rc.B / rc.R:.3f},{len(rc.inflight)},{'none' if fut is None else len(fut)},"
                f"{''.join('K' if k else '.' for _, _, k in (fut or []))},"
                f"{rc.cP:.3g},{rc.cK:.3g},{rc.uP or 0:.3g},{rc.uK or 0:.3g},{rc.inflight.get(n, (0, 0))[1]:.0f}")
        rcs["fifo"].append(n)
        rcs["qps"].append(q)
        return Gst.PadProbeReturn.OK

    def is_keyframe(buf):
        """Is this encoded frame a keyframe? Read from the bitstream itself:
        an H.265 IRAP picture (NAL types 16-21: IDR, CRA, BLA). The buffer's
        delta-unit flag is not to be trusted - on air it stopped being set,
        the controller never saw a keyframe again, believed every frame was
        an overdue one, and swung the floor 30-51-30 frame by frame: the
        flicker. Headers come first in a keyframe, so 4 kB is plenty."""
        if not buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
            return True
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            d = bytes(mi.data[:4096])
        finally:
            buf.unmap(mi)
        i = d.find(b"\x00\x00\x01")
        while 0 <= i < len(d) - 3:
            t = (d[i + 3] >> 1) & 0x3F
            if 16 <= t <= 21:
                return True
            if t < 32 and t not in (35, 39, 40):     # a slice of another kind: not a keyframe
                return False
            i = d.find(b"\x00\x00\x01", i + 3)
        return False

    def rc_out(pad, info):
        """Each frame out of the encoder - in the order they went in."""
        buf = info.get_buffer()
        if rcs["fifo"]:
            m = rcs["fifo"].popleft()
            bits = buf.get_size() * 8
            rc.done(m, bits, is_keyframe(buf))
            line = rcs.get("trace", {}).pop(m, None)
            if line:
                with open("/tmp/rc-trace.csv", "a") as tf:
                    tf.write(f"{line},{bits},{int(is_keyframe(buf))},{int(not buf.has_flags(Gst.BufferFlags.DELTA_UNIT))}\n")
            rcs["peak"] = max(rcs["peak"], rc.B / rc.R)
        now = time.time()
        if now - rcs["last_log"] >= 10 and rcs["qps"]:
            q = rcs["qps"]
            mode = f"look-ahead {LOOK:.1f} s" if LOOK > 0 and rcs["look_ok"] >= 0 else "reactive"
            mode = ("hybrid, " if HYBRID else "") + mode
            log(f"rc ({mode}): {'floor' if HYBRID else 'QP'} mean {sum(q) / len(q):.1f} (range {min(q)}-{max(q)}), "
                f"buffer peak {rcs['peak']:.2f} of {a.slack} s, skipped {rcs['skips']}, "
                f"frames at 46+ {sum(1 for x in q if x >= 46)}, at the base {sum(1 for x in q if x == rc.qp_min)}")
            rcs.update(last_log=now, skips=0, qps=[], peak=0.0)
        return Gst.PadProbeReturn.OK

    prog.get_by_name("vstamp").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, score)
    enc.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, rc_in)
    enc.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, rc_out)


def selector_pads(sel):
    out = {}
    for p in sel.sinkpads:
        caps = p.get_peer().get_parent_element()
        src = caps.get_static_pad("sink").get_peer().get_parent_element().get_name()
        out["cam" if src.endswith("cam") else "med"] = p
    return out


vpads = selector_pads(vsel)
apads = {"cam": amix.get_static_pad("sink_0"), "med": amix.get_static_pad("sink_1")}
for pad in apads.values():
    pad.set_property("mute", True)            # nothing heard until the first take

# ---- running the inputs ----------------------------------------------------
pipes = {"cam": None, "med": None}


def watch(p, name):
    b = p.get_bus()
    b.add_signal_watch()
    b.connect("message", on_msg, name, p)


def hdmi_signal():
    """Is there an HDMI signal? Asks the driver without touching any pipeline."""
    try:
        r = subprocess.run(["v4l2-ctl", "-d", a.video_device, "--query-dv-timings"],
                           capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            if "Active width" in line:
                return int(line.split(":")[1].strip() or 0) > 0
    except Exception:
        pass
    return False


def start_camera(bars=False):
    if pipes["cam"]:
        pipes["cam"].set_state(Gst.State.NULL)
    try:
        p = Gst.parse_launch(camera_desc(bars))
    except GLib.Error as e:
        if bars:
            raise                                     # bars must always build
        log(f"camera: cannot build the HDMI pipeline ({e.message}) - fallback picture")
        return start_camera(bars=True)
    watch(p, "cam")
    pipes["cam"] = p
    p.set_state(Gst.State.PLAYING)
    state["camera"] = "bars" if bars else "live"
    return False


def retry_camera():
    """Every 5 s while on bars: if an HDMI signal has appeared, go back to
    the camera. The check does not touch the bars, so they never flicker."""
    if state["camera"] != "bars":
        return False
    if not a.sim and hdmi_signal():
        log("camera: HDMI signal back")
        start_camera(bars=False)
    return state["camera"] == "bars"


# ---- smoothness: does every frame arrive on time? -------------------------
# A jerk on a pan with nothing late in the stream means a frame was shown
# twice (or skipped) before the encoder ever saw it: the Media player's
# decode fell behind and intervideosrc repeated the last picture. Count the
# frames reaching each side and the gaps between them, by the wall clock.
smooth = {"med": {"n": 0, "slow": 0, "worst": 0.0, "t": None},
          "prog": {"n": 0, "slow": 0, "worst": 0.0, "t": None}}


def _tick(key):
    s = smooth[key]
    now = time.monotonic()
    if s["t"] is not None:
        gap = now - s["t"]
        s["worst"] = max(s["worst"], gap)
        if gap > 0.060:                      # 1.5 frames at 25 fps
            s["slow"] += 1
    s["t"] = now
    s["n"] += 1


def tap_smooth(p):
    """Watch the frames leaving a Media player pipeline."""
    it = p.iterate_elements()
    while True:
        ok, el = it.next()
        if ok != Gst.IteratorResult.OK:
            break
        if el.get_factory() and el.get_factory().get_name() == "intervideosink":
            el.get_static_pad("sink").add_probe(
                Gst.PadProbeType.BUFFER, lambda pad, info: (_tick("med"), Gst.PadProbeReturn.OK)[1])


def report_smooth():
    m, g = smooth["med"], smooth["prog"]
    if m["n"] or m["slow"]:
        log(f"smooth: media decoded {m['n']} frames in 10 s, gaps >60 ms {m['slow']} "
            f"(longest {m['worst'] * 1000:.0f} ms)  |  to encoder {g['n']}, gaps {g['slow']} "
            f"(longest {g['worst'] * 1000:.0f} ms)")
    for s in (m, g):
        s.update(n=0, slow=0, worst=0.0)
    return True


GLib.timeout_add_seconds(10, report_smooth)


def load(name):
    """Play <name> in the Media player. Runs on the main loop."""
    desc, kind = media_desc(name)
    if not desc:
        log(f"media: {name}: {kind}")
        state["media_state"] = f"error: {kind}"
        return False
    if pipes["med"]:
        pipes["med"].set_state(Gst.State.NULL)
    try:
        p = Gst.parse_launch(desc)
    except GLib.Error as e:
        log(f"media: {name}: cannot build the player ({e.message})")
        state["media_state"] = f"error: {e.message}"
        return False
    watch(p, "med")
    tap_smooth(p)
    pipes["med"] = p
    p.set_state(Gst.State.PLAYING)
    state.update(media=name, kind=kind, media_state="playing")
    log(f"media: playing {name} ({kind})")
    return False


def load_black():
    if pipes["med"]:
        pipes["med"].set_state(Gst.State.NULL)
    p = Gst.parse_launch(f"videotestsrc is-live=true pattern=black ! {VCAPS} ! "
                         f"intervideosink channel=med " + silence())
    pipes["med"] = p
    p.set_state(Gst.State.PLAYING)
    return False


def take(which):
    if which not in vpads or which == state["on"]:
        return False
    vsel.set_property("active-pad", vpads[which])
    for name, pad in apads.items():
        pad.set_property("mute", name != which)
    rcs["force_key"] = True                          # the controller plans for it
    ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
    enc.get_static_pad("src").send_event(ev)
    state["on"] = which
    state["cuts"] += 1
    log(f"cut -> {which}")
    return False


def shutdown(*_):
    log("stopping")
    for p in [prog] + [x for x in pipes.values() if x]:
        p.set_state(Gst.State.NULL)
    loop.quit()
    return False


for sig in (signal.SIGINT, signal.SIGTERM):
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, shutdown)


def on_msg(bus, msg, name, pipe):
    # A pipeline that has been replaced can still deliver a late error;
    # it no longer matters, and acting on it restarts the wrong thing.
    if name != "prog" and pipe is not pipes.get(name):
        return
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        log(f"{name} ERROR: {err.message}")
        if name == "prog":
            shutdown()                              # the encoder side: fatal
        elif name == "cam":
            first = state["camera"] != "bars"
            GLib.idle_add(start_camera, True)
            if first:
                log("camera: no usable HDMI signal - fallback picture until it returns")
                GLib.timeout_add(5000, retry_camera)
        elif name == "med":
            state["media_state"] = f"error: {err.message}"
            GLib.idle_add(load_black)
    elif msg.type == Gst.MessageType.EOS and name == "med":
        if pipes["med"]:
            pipes["med"].seek_simple(Gst.Format.TIME,
                                     Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)


watch(prog, "prog")    # registered before the inputs start


# ---- bitrate trim ------------------------------------------------------------
# Thresholds as fractions of the slack: act at half, harder below a
# quarter, release above 85%. (Fixed at 1.0/0.5/1.6 s they suited a 2 s
# slack, but with 0.4 s the trim sat at its floor permanently.)
TRIM_FLOOR = 1.0   # trim off: its live rate changes made the encoder re-settle; headroom + tsprio do the job
TRIM_LOW, TRIM_VERY_LOW, TRIM_HIGH = 0.5 * a.slack, 0.25 * a.slack, 0.85 * a.slack
trim = {"f": 1.0, "last_down": 0.0, "last_up": 0.0, "quiet_since": None, "prev": None}


def set_rate(f):
    bps = int(a.bps * f)
    if a.sim:
        enc.set_property("bitrate", max(1, bps // 1000))      # x265enc: kb/s
    else:
        enc.set_property("bps", bps)
        enc.set_property("bps-max", bps)
    state["trim"] = round((1 - f) * 100)


def trim_tick():
    now = time.time()
    try:
        with open(a.buffer_json) as fh:
            b = json.load(fh)
        fresh = now - b.get("time", 0) < 3
    except Exception:
        b, fresh = {}, False
    f = trim["f"]
    if not fresh:
        trim["quiet_since"] = trim["quiet_since"] or now
        if f < 1.0 and now - trim["quiet_since"] > 5 and now - trim["last_up"] > 3:
            f = min(1.0, f + 0.05)                            # no reading: drift back
            trim["last_up"] = now
    else:
        trim["quiet_since"] = None
        slack = b.get("slack")
        # A cut takes a second or two to show in the buffer, so only cut
        # again while the slack is still falling - not once it is recovering.
        falling = trim["prev"] is None or slack is None or slack <= trim["prev"] + 0.02
        trim["prev"] = slack
        if slack is not None and slack < TRIM_LOW and falling and now - trim["last_down"] > 1:
            f = max(TRIM_FLOOR, f - (0.2 if slack < TRIM_VERY_LOW else 0.1))
            trim["last_down"] = now
        elif slack is not None and slack > TRIM_HIGH and f < 1.0 and now - trim["last_up"] > 3:
            f = min(1.0, f + 0.05)
            trim["last_up"] = now
    if abs(f - trim["f"]) > 1e-6:
        trim["f"] = round(f, 2)
        set_rate(trim["f"])
        log(f"trim: {state['trim']}% below target "
            f"(slack {b.get('slack', 'n/a')} s)" if state["trim"] else "trim: back to full rate")
    return True


GLib.timeout_add(500, trim_tick)


# ---- control -----------------------------------------------------------------
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Lynx-Tx switcher</title>
<style>body{background:#1a1a2e;color:#e0e0e0;font-family:system-ui,sans-serif;padding:1.2em}
button{font-size:1.6em;padding:1.2em;margin:.3em;border-radius:12px;border:3px solid #3a4a63;
background:#16213e;color:#e0e0e0;min-width:8em}button.on{border-color:#e53935;background:#7f1d1d}</style>
</head><body><h2>Lynx-Tx switcher</h2>
<button id="cam" onclick="t('cam')">Camera</button><button id="med" onclick="t('med')">Media</button>
<p id="i"></p><script>
async function t(s){await fetch('/take?src='+s,{method:'POST'});setTimeout(p,150)}
async function p(){try{const j=await(await fetch('/status')).json();
for(const b of['cam','med'])document.getElementById(b).classList.toggle('on',j.on===b);
document.getElementById('i').textContent='Media: '+(j.media||'none')+' - camera: '+j.camera}
catch(e){document.getElementById('i').textContent='not running'}}
document.addEventListener('keydown',e=>{if(e.key==='1')t('cam');if(e.key==='2')t('med')});
p();setInterval(p,1000)</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *x):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            self._send(200, json.dumps(state))
        elif path == "/":
            self._send(200, PAGE, "text/html")
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/take":
            src = (q.get("src") or [""])[0]
            if src not in ("cam", "med"):
                return self._send(400, '{"error":"src must be cam or med"}')
            GLib.idle_add(take, src)
            return self._send(200, json.dumps({"taking": src}))
        if u.path == "/keyframe":
            GLib.idle_add(keyframe)
            return self._send(200, '{"keyframe": true}')
        if u.path == "/load":
            name = os.path.basename((q.get("file") or [""])[0])
            if not name or not (MEDIA_DIR / name).is_file():
                return self._send(404, '{"error":"no such file"}')
            GLib.idle_add(load, name)
            return self._send(200, json.dumps({"loading": name}))
        self._send(404, '{"error":"not found"}')


def keyframe():
    """A keyframe now: when the transmitter keys up mid-stream, the receiver
    then locks at once instead of waiting for the next one in the GOP."""
    ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
    enc.get_static_pad("src").send_event(ev)
    if rc is not None:
        rcs["force_key"] = True
    log("keyframe requested")
    return False


def serve():
    try:
        ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
    except OSError as e:
        log(f"no control port {a.port}: {e}")


threading.Thread(target=serve, daemon=True).start()

# ---- go --------------------------------------------------------------------
start_camera(bars=False)
if a.media:
    load(a.media)
else:
    load_black()
if prog.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    log("could not start the encoder side")
    sys.exit(1)
GLib.timeout_add(1000, lambda: take(a.start))
if a.seconds:
    GLib.timeout_add(a.seconds * 1000, shutdown)
log(f"running; control on port {a.port}")
loop.run()
