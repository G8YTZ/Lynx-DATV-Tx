#!/usr/bin/env python3
"""
Lynx-Tx — a DATV transmitter you can drive from a web page.

A Radxa ROCK 5T encoding H.265 in hardware, feeding a LibreSDR running
DL1JM's firmware, with one page that owns both ends. Companion piece to
the Lynx receiver; same family, same look, opposite direction.

    ROCK 5T                                    LibreSDR
    ┌────────────────────────────────┐        ┌──────────────┐
    │ HDMI in ─┐                     │        │              │
    │          ├─ mpph265enc ─ mux ─ │─ UDP ─→│ pluto_dvb ─→ │─ RF
    │ media ───┘        ffmpeg  tsp  │  8282  │              │
    └────────────────────────────────┘        └──────────────┘

Why the chain looks like that
-----------------------------
Every stage earns its place, and each was added because the thing broke
without it:

  mpph265enc   the RK3588's hardware encoder. 4K25 HEVC at 23% of one
               core, where a Pi would be doing it in software.

  ffmpeg       remuxes to an exact constant rate and puts video on PID
               256, which is what the firmware's tables expect.
               GStreamer's muxer numbers it 0x41, and the firmware's
               own pcrextract looks for 256 and finds nothing.

  tsp          paces the output. Without it the stream arrives in
               bursts, the modulator's buffer drains between them, and
               it fills the gaps with null packets - which is a green
               screen at the far end while every figure on both
               machines looks perfectly healthy.

The pacing is deliberately a little ABOVE the modulator's own figure.
It wants its buffer kept full; discarding a small excess is harmless,
starving it is not.

The LibreSDR's own remux
------------------------
Its firmware offers a "force compliant" mode that remuxes incoming
streams with ffmpeg on the board itself. Leave it OFF. The Zynq's
Cortex-A9 manages speed=0.549x - about half real time - so it falls
progressively behind until the input buffer overruns and the modulator
restarts. Doing the same work here costs nothing and runs at 651x.

Symbol rate
-----------
Around 2 MS/s is the practical ceiling in Standard-Mode: above it the
spectrum grows notches rather than degrading gracefully, because
pluto_dvb trades oversampling for symbol rate and eventually runs out
of room. Performance-Mode raises the clock and helps, at the cost of
heat on a board that may run unattended.

Justin Cockett, G8YTZ
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

# ── Paths and configuration ───────────────────────────────────

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config" / "lynx_tx.yaml"

# Shared with Lynx-Deck deliberately: the same files, the same layout,
# so a clip prepared for one plays on the other and anything learned
# about one applies to both.
MEDIA_DIR = Path("/srv/lynxdeck/media")

# Where media is written and the encoder reads it from. A v4l2
# loopback, so anything that can write video to a device can be a
# source - this application's own feeder today, Lynx-Deck tomorrow,
# and neither has to know about the other.
#
# Created by: modprobe v4l2loopback video_nr=10 max_width=3840 \
#             max_height=2160
#
# Deliberately WITHOUT exclusive_caps: with it set, ffmpeg's format
# probe fails on a device nothing has written to yet, which reads as
# "ioctl(VIDIOC_G_FMT): Invalid argument" and took an evening to
# attribute correctly.
LOOPBACK_DEVICE = "/dev/video10"

VIDEO_EXT = {".mp4", ".mkv", ".ts", ".m4v", ".mov"}
STILL_EXT = {".png", ".jpg", ".jpeg", ".bmp"}

DEFAULT_CONFIG = {
    "callsign": "NOCALL",
    "provider": "HD",
    "libre": {"host": "192.168.0.172", "user": "root"},
    "video_device": "/dev/video0",
    "audio_device": "hw:3,0",
    "modulation": {
        "frequency_mhz": 437.0,
        "symbol_rate_ks": 1000,
        "constellation": "QPSK",
        "fec": "4/5",
        "pilots": True,
        "rolloff": "0.35",
        "power_db": -20.0,
    },
    "encoder": {
        "width": 1920,
        "height": 1080,
        "fps": 50,
        "gop": 25,
        "audio": True,
        "audio_bitrate": 192000,
        # Of the transport stream, how much goes to video. The
        # firmware's own documentation suggests 70%; the rest is audio,
        # PSI and the stuffing that keeps the modulator fed.
        "video_share": 0.70,
    },
    "source": {"kind": "camera", "file": ""},
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(DEFAULT_CONFIG, f, sort_keys=False)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        loaded = yaml.safe_load(f) or {}
    # Merge rather than replace, so a config written by an older
    # version gains new keys instead of failing on their absence.
    cfg = dict(DEFAULT_CONFIG)
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    tmp.replace(CONFIG_PATH)


config = load_config()


# ── DVB-S2 arithmetic ─────────────────────────────────────────

# Useful bits per FECFRAME, long frames (64800 bits), by code rate.
# The modulator computes its own figure and prints it; this exists so
# the page can show a sensible number before anything is running, and
# so the video bitrate can be chosen without a round trip.
_BCH_KBCH = {
    "1/4": 16008, "1/3": 21408, "2/5": 25728, "1/2": 32208,
    "3/5": 38688, "2/3": 43040, "3/4": 48408, "4/5": 51648,
    "5/6": 53840, "8/9": 57472, "9/10": 58192,
}
_BITS_PER_SYMBOL = {"QPSK": 2, "8PSK": 3, "16APSK": 4, "32APSK": 5}


def ts_bitrate(symbol_rate_ks: int, constellation: str, fec: str,
               pilots: bool) -> int:
    """The net transport stream rate for these parameters.

    Close enough to plan with, but the modulator's own figure is the
    one that matters: it prints "Net TS bitrate input should be N" on
    startup, and feeding it anything else is how a stream ends up
    either starved or overflowing. read_libre_bitrate() fetches that.
    """
    sr = symbol_rate_ks * 1000
    bps = _BITS_PER_SYMBOL.get(constellation.upper(), 2)
    kbch = _BCH_KBCH.get(fec, 48408)
    # 64800 coded bits per frame, plus 90 symbols of PLHEADER, plus a
    # pilot block of 36 symbols every 16 slots when pilots are on.
    symbols = 64800 / bps + 90
    if pilots:
        symbols += 36 * int((64800 / bps) / 1440)
    return int(sr * kbch / symbols)


def read_libre_bitrate() -> Optional[int]:
    """Ask the modulator what it actually wants.

    Its own arithmetic, not ours. Worth the round trip: the web page's
    "TS Rate Available" and this figure have been seen to differ by a
    few kb/s, which is enough to underflow.
    """
    out = libre_ssh("grep -a TsBitrate /tmp/udpts.log | tail -1")
    if out and "TsBitrate" in out:
        try:
            return int(out.strip().split()[-1])
        except ValueError:
            pass
    return None


# ── Talking to the LibreSDR ───────────────────────────────────

def libre_ssh(cmd: str, timeout: int = 8) -> str:
    """Run a command on the LibreSDR over SSH.

    Key-based, non-interactive: a transmitter that stops to ask for a
    password is no use. If this fails, check the board's /root is not
    world-writable - OpenSSH silently refuses key auth when it is, and
    says nothing useful about why.
    """
    lib = config["libre"]
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             f"{lib['user']}@{lib['host']}", cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception as e:
        print(f"[libre] {e}")
        return ""


def write_libre_settings() -> bool:
    """Push the modulation settings and let the board's watcher act.

    settings.txt is the firmware's whole control surface: a watcher
    script notices the file change and restarts the modulator with the
    new values. Nothing else needs calling.

    remux is forced to 0. That is the firmware's "force compliant"
    mode, and it is the single biggest thing standing between this
    chain and a working picture - see the note at the top of this file.
    """
    m = config["modulation"]
    fec_digits = m["fec"].replace("/", "")
    lines = {
        "callsign": config["callsign"],
        "freq": f"{m['frequency_mhz']:g}",
        "channel": "Custom",
        "mode": "DVBS2",
        "mod": m["constellation"],
        "sr": str(m["symbol_rate_ks"]),
        "srselect": str(m["symbol_rate_ks"]),
        "fec": fec_digits,
        "pilots": "On" if m["pilots"] else "Off",
        "frame": "LongFrame",
        "power": f"{m['power_db']:g}",
        "rolloff": m["rolloff"],
        "pcrpts": "100",
        "patperiod": "200",
        "h265box": "",
        "codec": "H265",
        "sound": "Off",
        "audioinput": "line",
        "remux": "0",
        "trvlo": "0",
        "trvloselect": "Custom",
        "provname": config["provider"],
    }
    body = "\n".join(f"{k} {v}" for k, v in lines.items()) + "\n"
    # Written via a here-doc rather than scp: one round trip, no
    # temporary files, and it works on a board with no scp binary.
    cmd = f"cat > /www/settings.txt <<'LYNXTX_EOF'\n{body}LYNXTX_EOF"
    libre_ssh(cmd)
    return True


def libre_status() -> dict:
    """What the board says about itself, for the status strip."""
    out = libre_ssh(
        "grep -a TsBitrate /tmp/udpts.log 2>/dev/null | tail -1; "
        "grep -ac Underflow /tmp/udpts.log 2>/dev/null; "
        "pgrep -c pluto_dvb 2>/dev/null")
    parts = (out or "").strip().split("\n")

    def _int(i):
        try:
            return int(parts[i].strip().split()[-1])
        except (IndexError, ValueError):
            return None

    return {
        "reachable": bool(out),
        "ts_bitrate": _int(0),
        "underflows": _int(1),
        "modulator_running": bool(_int(2)),
    }


# ── The pipeline ──────────────────────────────────────────────

tx_proc: Optional[subprocess.Popen] = None
tx_lock = threading.Lock()
tx_started_at = 0.0
tx_last_command = ""


def hdmi_format() -> dict:
    """What the capture is actually being given.

    The HDMI receiver follows its source, so a camera set to 4K
    presents 4K and one set to 1080p presents 1080p. Reading it beats
    asking the operator, and beats assuming.
    """
    try:
        # Always the camera's own device: this answers "what is plugged
        # in", which is worth knowing even while media is playing.
        r = subprocess.run(
            ["v4l2-ctl", "-d", config["video_device"], "--get-fmt-video"],
            capture_output=True, text=True, timeout=4)
        w = h = None
        for line in r.stdout.split("\n"):
            if "Width/Height" in line:
                bits = line.split(":")[1].strip().split("/")
                w, h = int(bits[0]), int(bits[1])
        return {"width": w, "height": h} if w else {}
    except Exception:
        return {}


feeder_proc: Optional[subprocess.Popen] = None


def stop_feeder():
    """Stop whatever is writing to the loopback."""
    global feeder_proc
    if feeder_proc is not None:
        try:
            os.killpg(os.getpgid(feeder_proc.pid), signal.SIGTERM)
            feeder_proc.wait(timeout=4)
        except Exception:
            try:
                os.killpg(os.getpgid(feeder_proc.pid), signal.SIGKILL)
            except Exception:
                pass
        feeder_proc = None


def start_feeder(kind: str, filename: str):
    """Write a file into the loopback, forever.

    ffmpeg rather than anything cleverer: it decodes every format these
    files come in, converts and scales in one pass, and paces itself
    with -re so the loopback receives at real time rather than as fast
    as the disk can read.

    The format filter is not optional. A 10-bit file - which a Resolve
    export or an iPhone recording will be, whatever anybody remembers -
    written to a loopback expecting 8-bit fails outright, and a frame
    rate that disagrees with the encoder's produces a picture that
    plays at the wrong speed.
    """
    global feeder_proc
    stop_feeder()
    if kind == "camera":
        return

    path = MEDIA_DIR / filename
    if not path.exists():
        print(f"[feeder] {path} is not there")
        return

    enc = config["encoder"]
    size = f"{enc['width']}x{enc['height']}"
    fps = enc["fps"]

    if kind == "still":
        # -loop 1 turns one image into an endless stream. A still at
        # these rates is very nearly lossless: the bits all go into one
        # picture and almost nothing changes afterwards, which is why a
        # 4K test card looks better than 4K video at the same bitrate.
        src = ["-re", "-loop", "1", "-i", str(path)]
    else:
        # -stream_loop -1 repeats the file without anything noticing
        # the join.
        src = ["-re", "-stream_loop", "-1", "-i", str(path)]

    cmd = (["ffmpeg", "-loglevel", "warning"] + src +
           ["-vf", f"format=yuv420p,scale={size},fps={fps}",
            "-f", "v4l2", LOOPBACK_DEVICE])

    # stdin from /dev/null, and -nostdin: without either, ffmpeg
    # tries to read the terminal, gets suspended the moment it is
    # backgrounded, and writes nothing at all. Which looks exactly
    # like the feeder not starting.
    cmd.insert(1, "-nostdin")
    feeder_proc = subprocess.Popen(
        cmd, preexec_fn=os.setsid, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=None)
    print(f"[feeder] {kind}: {filename} -> {LOOPBACK_DEVICE}")
    # The loopback has no format until something writes one, and the
    # encoder's v4l2src fails on a device that has none. A moment here
    # saves a race that looks like the media source not working.
    # Four seconds, not one: at 4K ffmpeg spends a while opening the
    # file and scaling the first frame before it writes anything, and
    # an encoder that opens the loopback before then finds no format
    # and exits. Generous because the cost is a slow source change and
    # the alternative is a transmitter that quits.
    time.sleep(4.0)


def media_files() -> list:
    """Whatever is in the media folder, video and stills separately."""
    if not MEDIA_DIR.exists():
        return []
    out = []
    for p in sorted(MEDIA_DIR.iterdir()):
        if p.is_file():
            ext = p.suffix.lower()
            if ext in VIDEO_EXT:
                out.append({"name": p.name, "kind": "video"})
            elif ext in STILL_EXT:
                out.append({"name": p.name, "kind": "still"})
    return out


def build_pipeline() -> str:
    """The whole chain as one shell command.

    Built fresh each time rather than kept as a template: the rates
    depend on the modulation, the source depends on what is selected,
    and a stale command line is a fault that takes an evening to find.
    """
    enc = config["encoder"]
    m = config["modulation"]
    src = config["source"]

    # The modulator's own figure when we can get it, ours when we
    # cannot. Never the web page's "TS Rate Available" - that has been
    # seen to differ from what the modulator asks for.
    rate = read_libre_bitrate() or ts_bitrate(
        m["symbol_rate_ks"], m["constellation"], m["fec"], m["pilots"])

    # What is left after audio and the overhead the stream genuinely
    # needs - not a fraction anybody chooses.
    #
    # PSI is small and periodic: a PAT and PMT every 200 ms, an SDT
    # every second, a few kb/s in total. The rest of the headroom is
    # stuffing, and it has to be there: the modulator wants its buffer
    # kept full, and an encoder allowed to fill the pipe exactly will
    # starve it on every keyframe. 8% is enough to ride a GOP at these
    # rates and small enough not to waste the channel.
    audio_bps = (enc["audio_bitrate"] + 8000
                 if enc["audio"] and src["kind"] != "still" else 0)
    psi_bps = 12000
    video_bps = int((rate - audio_bps - psi_bps) * 0.92)

    w, h, fps = enc["width"], enc["height"], enc["fps"]

    # -- the source ------------------------------------------------
    #
    # Always a v4l2 device. The camera is one; everything else arrives
    # through the loopback, written by the feeder above. That keeps the
    # encoder pipeline identical whatever is playing, and means a
    # source change is a feeder restart rather than a different
    # pipeline that has to be got right separately.
    if src["kind"] == "camera":
        device = config["video_device"]
        fmt = hdmi_format()
        if fmt.get("width"):
            w, h = fmt["width"], fmt["height"]
        # NV16 is what the HDMI receiver gives; asking for anything
        # else makes GStreamer insert a converter that runs in
        # software at 4K.
        caps = (f"video/x-raw,format=NV16,width={w},height={h},"
                f"framerate={fps}/1")
    else:
        device = LOOPBACK_DEVICE
        # The feeder writes yuv420p, so that is what comes out.
        # No framerate in the caps. A loopback does not advertise one,
        # and asking for it makes v4l2src fail negotiation silently -
        # the pipeline opens, gets EOS immediately, and the whole
        # transmitter exits a few seconds later with nothing anywhere
        # saying why. The feeder paces at the right rate already.
        caps = f"video/x-raw,format=I420,width={w},height={h}"

    # videoconvert between the loopback and the encoder: mpph265enc
    # takes NV12 or NV16, and the feeder writes I420. Without it the
    # pipeline fails to negotiate and stops with nothing more useful
    # than "not-negotiated (-4)".
    if src["kind"] == "camera":
        video_in = f"v4l2src device={device} ! {caps}"
    else:
        video_in = (f"v4l2src device={device} ! {caps} ! "
                    f"videoconvert ! video/x-raw,format=NV12")

    # -- encode and mux --------------------------------------------
    # header-mode=each-idr and config-interval=-1 together repeat the
    # parameter sets at every keyframe. A decoder joining mid-stream
    # has everything it needs within one GOP rather than waiting for a
    # sequence header that may never come again.
    parts = [
        f"gst-launch-1.0 -e -q {video_in} ! "
        f"mpph265enc rc-mode=cbr bps={video_bps} gop={enc['gop']} "
        f"header-mode=each-idr ! h265parse config-interval=-1 ! mux."
    ]
    # Audio follows video.
    #
    # The camera's audio arrives on the HDMI receiver's ALSA device; a
    # video file carries its own. A still has none at all, and encoding
    # silence against a video clock that has no natural rate produces
    # exactly the drift it sounds like.
    #
    # The file is read a second time here rather than being carried
    # through the loopback with the video, because this kernel has no
    # snd-aloop to carry it. A decode of an already-open local file is
    # cheap, and the alternative is a named pipe arrangement that wants
    # designing rather than bolting on.
    want_audio = enc["audio"] and src["kind"] != "still"
    if want_audio and src["kind"] == "camera":
        parts.append(
            f" alsasrc device={config['audio_device']} ! "
            f"audio/x-raw,rate=48000,channels=2 ! audioconvert ! "
            f"avenc_aac bitrate={enc['audio_bitrate']} ! aacparse ! mux.")
    elif want_audio:
        apath = MEDIA_DIR / src["file"]
        parts.append(
            f" filesrc location={apath} ! decodebin ! "
            f"audioconvert ! audioresample ! "
            f"audio/x-raw,rate=48000,channels=2 ! "
            f"avenc_aac bitrate={enc['audio_bitrate']} ! aacparse ! mux.")
    parts.append(" mpegtsmux name=mux ! fdsink fd=1")

    # -- remux to the exact rate, on the right PIDs -----------------
    streamids = "-streamid 0:256"
    if want_audio:
        streamids += " -streamid 1:257"
    parts.append(
        f" | ffmpeg -loglevel warning -analyzeduration 1000000 "
        f"-f mpegts -i - -c copy -f mpegts -muxrate {rate} "
        f'-metadata service_provider="{config["provider"]}" '
        f'-metadata service_name="{config["callsign"]}" ' 
        f"{streamids} -max_delay 400000 -pat_period 0.200 pipe:1")

    # -- pace it ---------------------------------------------------
    # Deliberately above the modulator's figure. It wants its buffer
    # kept full; a small excess is discarded harmlessly, while
    # starvation shows up as missing video packets in a regular
    # pattern and a green screen at the far end.
    paced = int(rate * 1.006)
    # A small buffer: enough to smooth a GOP, not enough to add
    # noticeable delay. TSDuck's default held about 45 seconds.
    parts.append(
        f" | tsp --realtime --buffer-size-mb 0.05 -I file - "
        f"-P regulate --bitrate {paced} "
        f"-O ip {config['libre']['host']}:8282")

    return "".join(parts)


def start_tx() -> dict:
    global tx_proc, tx_started_at, tx_last_command
    with tx_lock:
        stop_tx_locked()
        # Whatever the source is, make sure it is actually being fed.
        # Starting the encoder against a loopback nothing is writing to
        # gives a pipeline that opens and then sits there.
        src = config["source"]
        if src["kind"] != "camera" and feeder_proc is None:
            start_feeder(src["kind"], src["file"])
        write_libre_settings()
        # The board's watcher restarts the modulator on the settings
        # change; give it a moment to be listening before the stream
        # arrives, or the first seconds go nowhere.
        time.sleep(2)
        cmd = build_pipeline()
        tx_last_command = cmd
        tx_proc = subprocess.Popen(
            cmd, shell=True, preexec_fn=os.setsid,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        tx_started_at = time.time()
        print(f"[tx] started: {cmd[:120]}...")
        return {"success": True}


def stop_tx_locked():
    global tx_proc
    if tx_proc is not None:
        try:
            os.killpg(os.getpgid(tx_proc.pid), signal.SIGTERM)
            tx_proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(tx_proc.pid), signal.SIGKILL)
            except Exception:
                pass
        tx_proc = None
    # Belt and braces: the shell pipeline can outlive its process
    # group if a stage has re-parented.
    # NOT a bare pkill for ffmpeg: the feeder writing the loopback is
    # an ffmpeg too, and killing it leaves the encoder opening a device
    # nothing is writing - which is exactly the failure this whole
    # arrangement kept producing. Match the remux stage specifically.
    for pattern in ("gst-launch-1.0", "ffmpeg.*-f mpegts",
                    "tsp --realtime"):
        subprocess.run(["pkill", "-f", pattern], capture_output=True)


def stop_tx() -> dict:
    with tx_lock:
        stop_tx_locked()
        # Mute rather than leave a bare carrier: PTT follows the power
        # setting on this firmware, and below -40 dB it drops out.
        libre_ssh("sed -i 's/^power .*/power -41/' /www/settings.txt")
        return {"success": True}


def tx_running() -> bool:
    return tx_proc is not None and tx_proc.poll() is None


# ── API ───────────────────────────────────────────────────────

app = FastAPI(title="Lynx-Tx", docs_url="/api/docs")


class ModulationUpdate(BaseModel):
    frequency_mhz: Optional[float] = None
    symbol_rate_ks: Optional[int] = None
    constellation: Optional[str] = None
    fec: Optional[str] = None
    pilots: Optional[bool] = None
    power_db: Optional[float] = None


class EncoderUpdate(BaseModel):
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    gop: Optional[int] = None
    audio: Optional[bool] = None
    audio_bitrate: Optional[int] = None
    video_share: Optional[float] = None


class SourceUpdate(BaseModel):
    kind: str
    file: str = ""


@app.get("/api/status")
def api_status():
    m = config["modulation"]
    theoretical = ts_bitrate(m["symbol_rate_ks"], m["constellation"],
                             m["fec"], m["pilots"])
    return {
        "transmitting": tx_running(),
        "uptime": int(time.time() - tx_started_at) if tx_running() else 0,
        "config": config,
        "hdmi": hdmi_format(),
        "media": media_files(),
        "ts_bitrate": theoretical,
        "libre": libre_status(),
    }


@app.post("/api/start")
def api_start():
    return start_tx()


@app.post("/api/stop")
def api_stop():
    return stop_tx()


class IdentUpdate(BaseModel):
    callsign: str
    provider: str


@app.post("/api/ident")
def api_ident(req: IdentUpdate):
    """Callsign and provider - what a receiver shows for this service.

    Written into the transport stream by ffmpeg as service_name and
    service_provider, and into the modulator's own settings so its SDT
    agrees. Both, because they are two different tables and a receiver
    may read either.
    """
    config["callsign"] = req.callsign or "NOCALL"
    config["provider"] = req.provider
    save_config(config)
    if tx_running():
        start_tx()
    return {"success": True}


@app.post("/api/modulation")
def api_modulation(req: ModulationUpdate):
    for k, v in req.model_dump().items():
        if v is not None:
            config["modulation"][k] = v
    save_config(config)
    if tx_running():
        start_tx()   # restart so the change takes effect
    return {"success": True, "config": config["modulation"]}


@app.post("/api/encoder")
def api_encoder(req: EncoderUpdate):
    for k, v in req.model_dump().items():
        if v is not None:
            config["encoder"][k] = v
    save_config(config)
    if tx_running():
        start_tx()
    return {"success": True, "config": config["encoder"]}


@app.post("/api/source")
def api_source(req: SourceUpdate):
    if req.kind not in ("camera", "video", "still"):
        raise HTTPException(400, "kind must be camera, video or still")
    config["source"] = {"kind": req.kind, "file": req.file}
    save_config(config)
    # The feeder first, so the loopback has a format before the encoder
    # opens it - the other order is a race that looks like the media
    # source simply not working.
    start_feeder(req.kind, req.file)
    if tx_running():
        start_tx()
    return {"success": True, "source": config["source"]}


@app.get("/api/command")
def api_command():
    """The pipeline as it would be built, for when something is wrong.

    Copy it, run it by hand, and whatever fails says why - which is
    considerably faster than guessing at a process that writes its
    errors to a pipe nobody is reading.
    """
    return {"command": build_pipeline(), "last": tx_last_command}


# ── The page ──────────────────────────────────────────────────

PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lynx-Tx</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background: #1a1a2e; color: #e0e0e0; }
  .card { background: #16213e; border: 1px solid #0f3460; }
  .card-header { background: #0f3460; color: #fff; font-weight: 600; }
  .status-value { font-family: monospace; color: #4ade80; }
  /* Bootstrap's table and muted text both assume a light page. */
  .table { --bs-table-bg: transparent; --bs-table-color: #e0e0e0; }
  .text-muted { color: #9fb3d1 !important; }
  label.small { color: #9fb3d1; }
  /* Bootstrap's table and muted text both assume a light page. */
  .table { --bs-table-bg: transparent; --bs-table-color: #e0e0e0; }
  .text-muted { color: #9fb3d1 !important; }
  label.small { color: #9fb3d1; }
  .status-off { color: #9ca3af; }
  .big-btn { font-size: 1.3em; padding: 0.6em; }
</style>
</head><body>
<div class="container-fluid py-3">

  <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
    <h4 class="mb-0 me-3">&#x1F4E1; Lynx-Tx</h4>
    <span class="btn btn-sm" id="tx-badge"
          style="background:#3a4a63;color:#fff;cursor:default">IDLE</span>
    <span class="btn btn-sm" id="call-badge"
          style="background:#3a4a63;color:#fff;cursor:default">—</span>
    <span class="btn btn-sm" id="libre-badge"
          style="background:#3a4a63;color:#fff;cursor:default">LibreSDR ?</span>
  </div>

  <div class="row g-3">

    <div class="col-md-4">
      <div class="card mb-3">
        <div class="card-header">&#x25B6;&#xFE0F; Transmit</div>
        <div class="card-body">
          <div class="d-grid gap-2 mb-3">
            <button class="btn btn-success big-btn" onclick="startTx()">Start</button>
            <button class="btn btn-danger big-btn" onclick="stopTx()">Stop</button>
          </div>
          <table class="table table-sm table-borderless mb-0">
            <tr><td class="text-muted">Transport stream</td>
                <td class="status-value text-end" id="s-ts">—</td></tr>
            <tr><td class="text-muted">Modulator wants</td>
                <td class="status-value text-end" id="s-want">—</td></tr>
            <tr><td class="text-muted">Underflows</td>
                <td class="status-value text-end" id="s-under">—</td></tr>
            <tr><td class="text-muted">On air for</td>
                <td class="status-value text-end" id="s-up">—</td></tr>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-header">&#x1F3A5; Source</div>
        <div class="card-body">
          <div class="btn-group w-100 mb-2" role="group">
            <button class="btn btn-outline-light" id="src-camera"
                    onclick="setSource('camera','')">Camera</button>
            <button class="btn btn-outline-light" id="src-media"
                    onclick="showMedia()">Media</button>
          </div>
          <div class="text-muted small mb-2" id="hdmi-info">—</div>
          <div id="media-list"></div>
          <div class="text-muted small mt-2">
            Files live in /srv/lynxdeck/media, shared with Lynx-Deck.
            Changing source restarts the chain, so the carrier drops for
            a second or two.
          </div>
        </div>
      </div>
    </div>

    <div class="col-md-4">
      <div class="card">
        <div class="card-header">&#x1F4FB; Modulation</div>
        <div class="card-body">
          <label class="small text-muted">Service name (callsign)</label>
          <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary mb-2"
                 id="m-call" maxlength="16" onchange="saveIdent()">
          <label class="small text-muted">Provider name (multiplex)</label>
          <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary mb-1"
                 id="m-prov" maxlength="16" onchange="saveIdent()">
          <div class="text-muted small mb-2">
            What a receiver shows as the multiplex. The firmware
            prepends its own version by default; /opt/udpts.sh on the
            LibreSDR has been edited so it does not.
          </div>
          <label class="small text-muted">Frequency (MHz)</label>
          <input type="number" step="0.001" class="form-control form-control-sm bg-dark text-light border-secondary mb-2"
                 id="m-freq" onchange="saveMod()">
          <label class="small text-muted">Symbol rate (kS/s)</label>
          <input type="number" class="form-control form-control-sm bg-dark text-light border-secondary mb-1"
                 id="m-sr" onchange="saveMod()">
          <div class="small mb-2" style="color:#e8a33d" id="sr-warning" hidden>
            Above about 2000 the spectrum grows notches rather than
            degrading gracefully. Performance-Mode helps, at the cost of
            heat on a board that may run unattended.
          </div>
          <div class="row g-2 mb-2">
            <div class="col-6">
              <label class="small text-muted">Constellation</label>
              <select class="form-select form-select-sm bg-dark text-light border-secondary"
                      id="m-const" onchange="saveMod()">
                <option>QPSK</option><option>8PSK</option>
                <option>16APSK</option><option>32APSK</option>
              </select>
            </div>
            <div class="col-6">
              <label class="small text-muted">FEC</label>
              <select class="form-select form-select-sm bg-dark text-light border-secondary"
                      id="m-fec" onchange="saveMod()">
                <option>1/2</option><option>3/5</option><option>2/3</option>
                <option>3/4</option><option>4/5</option><option>5/6</option>
                <option>8/9</option><option>9/10</option>
              </select>
            </div>
          </div>
          <div class="form-check form-switch mb-2">
            <input class="form-check-input" type="checkbox" id="m-pilots"
                   onchange="saveMod()">
            <label class="form-check-label small" for="m-pilots">Pilots</label>
          </div>
          <label class="small text-muted">Power (dB)</label>
          <input type="range" class="form-range" id="m-power" min="-40" max="0"
                 step="0.5" oninput="document.getElementById('m-power-v').textContent=this.value+' dB'"
                 onchange="saveMod()">
          <div class="text-end status-value" id="m-power-v">—</div>
          <div class="text-muted small">
            Below &minus;40 dB this firmware drops PTT, which is how Stop
            leaves the carrier off.
          </div>
        </div>
      </div>
    </div>

    <div class="col-md-4">
      <div class="card">
        <div class="card-header">&#x1F39E;&#xFE0F; Encoder</div>
        <div class="card-body">
          <!-- Camera: shown, not chosen. v4l2src hands the HDMI
               input straight to the encoder with no scaler between
               them, which is why this path costs 44% of a core where
               a converted one costs 135%. A control that is silently
               overwritten is worse than no control at all. -->
          <div id="camera-format" style="display:none">
            <label class="small text-muted">Picture</label>
            <div class="status-value mb-2" id="camera-format-value">&mdash;</div>
            <div class="text-muted small mb-3">
              Resolution and frame rate both follow the HDMI input,
              which takes its format from whatever is plugged in. The
              receiver reports its resolution but not its timings,
              which is why only the one figure appears here.
            </div>
          </div>

          <div id="media-format">
            <label class="small text-muted">Picture</label>
            <select class="form-select form-select-sm bg-dark text-light border-secondary mb-2"
                    id="e-format" onchange="saveEnc()">
              <option value="3840x2160">4K UHD &mdash; 3840&times;2160</option>
              <option value="1920x1080">1080p FHD &mdash; 1920&times;1080</option>
              <option value="1280x720">720p HD &mdash; 1280&times;720</option>
              <option value="1024x576">576p SD &mdash; 1024&times;576</option>
            </select>
            <label class="small text-muted">Frame rate</label>
            <select class="form-select form-select-sm bg-dark text-light border-secondary mb-2"
                    id="e-fps" onchange="saveEnc()">
              <option value="25">25</option>
              <option value="50">50</option>
            </select>
          </div>

          <div class="form-check form-switch mb-2">
            <input class="form-check-input" type="checkbox" id="e-audio"
                   onchange="saveEnc()">
            <label class="form-check-label small" for="e-audio">Audio</label>
          </div>
          <label class="small text-muted">Audio bitrate</label>
          <select class="form-select form-select-sm bg-dark text-light border-secondary mb-2"
                  id="e-abr" onchange="saveEnc()">
            <option value="64000">64 kb/s</option>
            <option value="96000">96 kb/s</option>
            <option value="128000">128 kb/s</option>
            <option value="192000">192 kb/s</option>
          </select>
          <div class="text-muted small mb-2">
            Below about 96 kb/s AAC sounds broken in a way easily
            mistaken for a reception problem.
          </div>

        </div>
      </div>
    </div>

  </div>
</div>

<script>
let cfg = null;

async function api(method, path, body) {
  const opts = {method, headers: {'Content-Type': 'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

async function startTx() { await api('POST', '/api/start'); poll(); }
async function stopTx()  { await api('POST', '/api/stop');  poll(); }

async function setSource(kind, file) {
  await api('POST', '/api/source', {kind, file});
  poll();
}

function showMedia() {
  const el = document.getElementById('media-list');
  if (!cfg) return;
  const files = cfg.media || [];
  if (!files.length) {
    el.innerHTML = '<div class="text-muted small">Nothing in /srv/lynxdeck/media</div>';
    return;
  }
  el.innerHTML = files.map(f =>
    `<button class="btn btn-sm btn-outline-light w-100 mb-1 text-start"
             onclick="setSource('${f.kind}','${f.name}')">
       ${f.kind === 'still' ? '&#x1F5BC;&#xFE0F;' : '&#x1F3AC;'} ${f.name}
     </button>`).join('');
}

async function saveIdent() {
  await api('POST', '/api/ident', {
    callsign: document.getElementById('m-call').value.trim(),
    provider: document.getElementById('m-prov').value.trim()
  });
  poll();
}

async function saveMod() {
  await api('POST', '/api/modulation', {
    frequency_mhz: parseFloat(document.getElementById('m-freq').value),
    symbol_rate_ks: parseInt(document.getElementById('m-sr').value, 10),
    constellation: document.getElementById('m-const').value,
    fec: document.getElementById('m-fec').value,
    pilots: document.getElementById('m-pilots').checked,
    power_db: parseFloat(document.getElementById('m-power').value)
  });
  poll();
}

async function saveEnc() {
  await api('POST', '/api/encoder', {
    width: parseInt(document.getElementById('e-format').value.split('x')[0], 10),
    height: parseInt(document.getElementById('e-format').value.split('x')[1], 10),
    fps: parseInt(document.getElementById('e-fps').value, 10),
    audio: document.getElementById('e-audio').checked,
    audio_bitrate: parseInt(document.getElementById('e-abr').value, 10),
  });
  poll();
}

let firstLoad = true;

async function poll() {
  try {
    cfg = await (await fetch('/api/status')).json();
  } catch (e) { return; }

  const tx = document.getElementById('tx-badge');
  tx.textContent = cfg.transmitting ? 'ON AIR' : 'IDLE';
  tx.style.background = cfg.transmitting ? '#16a34a' : '#3a4a63';

  document.getElementById('call-badge').textContent = cfg.config.callsign;

  const lb = document.getElementById('libre-badge');
  const L = cfg.libre || {};
  lb.textContent = L.reachable
    ? (L.modulator_running ? 'LibreSDR ready' : 'LibreSDR idle')
    : 'LibreSDR unreachable';
  lb.style.background = L.reachable ? (L.modulator_running ? '#16a34a' : '#3a4a63')
                                    : '#b91c1c';

  document.getElementById('s-ts').textContent =
    (cfg.ts_bitrate/1e6).toFixed(3) + ' Mb/s';
  document.getElementById('s-want').textContent =
    L.ts_bitrate ? (L.ts_bitrate/1e6).toFixed(3) + ' Mb/s' : '—';
  const u = document.getElementById('s-under');
  u.textContent = (L.underflows === null || L.underflows === undefined)
    ? '—' : L.underflows;
  u.style.color = L.underflows ? '#f87171' : '#4ade80';
  document.getElementById('s-up').textContent =
    cfg.transmitting ? cfg.uptime + ' s' : '—';

  const h = cfg.hdmi || {};
  document.getElementById('hdmi-info').textContent = h.width
    ? `HDMI in: ${h.width}\\u00d7${h.height}`
    : 'HDMI in: nothing detected';

  document.getElementById('src-camera').className =
    'btn ' + (cfg.config.source.kind === 'camera' ? 'btn-primary' : 'btn-outline-light');
  document.getElementById('src-media').className =
    'btn ' + (cfg.config.source.kind !== 'camera' ? 'btn-primary' : 'btn-outline-light');

  // Which of the two appears. The format controls only mean
  // anything for media, where the file is genuinely scaled on the
  // way in; with the camera the HDMI input decides and the page
  // should say so rather than pretend otherwise.
  const isCam = cfg.config.source.kind === 'camera';
  document.getElementById('camera-format').style.display = isCam ? '' : 'none';
  document.getElementById('media-format').style.display = isCam ? 'none' : '';
  if (isCam) {
    const hf = cfg.hdmi || {};
    document.getElementById('camera-format-value').textContent =
      hf.width ? hf.width + '\u00d7' + hf.height : 'nothing detected';
  }

  document.getElementById('sr-warning').hidden =
    cfg.config.modulation.symbol_rate_ks <= 2000;

  if (firstLoad) {
    const m = cfg.config.modulation, e = cfg.config.encoder;
    document.getElementById('m-call').value = cfg.config.callsign;
    document.getElementById('m-prov').value = cfg.config.provider;
    document.getElementById('m-freq').value = m.frequency_mhz;
    document.getElementById('m-sr').value = m.symbol_rate_ks;
    document.getElementById('m-const').value = m.constellation;
    document.getElementById('m-fec').value = m.fec;
    document.getElementById('m-pilots').checked = m.pilots;
    document.getElementById('m-power').value = m.power_db;
    document.getElementById('m-power-v').textContent = m.power_db + ' dB';
    document.getElementById('e-format').value = e.width + 'x' + e.height;
    document.getElementById('e-fps').value = e.fps;
    document.getElementById('e-audio').checked = e.audio;
    document.getElementById('e-abr').value = e.audio_bitrate;
    if (cfg.config.source.kind !== 'camera') showMedia();
    firstLoad = false;
  }
}

poll();
setInterval(poll, 2000);
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


if __name__ == "__main__":
    print("=== Lynx-Tx ===")
    print(f"    config: {CONFIG_PATH}")
    print(f"    media:  {MEDIA_DIR}")
    print(f"    LibreSDR: {config['libre']['host']}")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
