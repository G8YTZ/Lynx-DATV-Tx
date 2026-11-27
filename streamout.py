"""Lynx-Tx stream out: what is on air, sent to a streaming server over RTMP.

The transmitter does no extra work for it. tsbuffer.py, already forked from
the on-air pipeline, sends a copy of the finished transport stream to
127.0.0.1:UDP_PORT as UDP; nothing waits for it, so the transmitter never
knows whether anyone is listening. This module runs an ffmpeg that reads
that copy and sends it on, entirely separate from the transmitter: if the
internet drops or the server hangs up, only the stream stops, and it
retries on its own.

Video: The H.265 goes out untouched (-c:v copy), as Enhanced RTMP (FLV with
the HEVC FourCC, hvc1). ffmpeg muxes that from 6.1 on; older versions
refuse, and the card says so. Whether a server takes it is the server's
business: classic RTMP servers accept only H.264.

Or H.264 (video "h264"): For servers that take only H.264, the BATC
streamer among them (it keeps the sound and drops H.265 pictures, tested
27 Nov 2026). GStreamer on the ROCK's own hardware: mppvideodec decodes
and scales the H.265, mpph264enc encodes it. Almost no CPU, so the IQ
modulator is never starved, but it shares the video encoder with the
on-air encode: watch the Transmit card's slack and late count.

Sound: Stereo AAC is copied. AC-3 (the 5.1 mode) has no place in classic
FLV, so it becomes stereo AAC, which is cheap.

The stream key never appears in the page, the log or /api/stream.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque

UDP_PORT = 5600
# Its own ffmpeg when installed (tools/install-stream-ffmpeg.sh): Debian
# Bookworm's 5.1 cannot mux H.265 into FLV, and the transmitter's own
# pipeline stays on the system ffmpeg it was proven with.
OWN_FFMPEG = "/usr/local/lib/lynx-tx/ffmpeg-stream/ffmpeg"


def ffmpeg_bin() -> str:
    return OWN_FFMPEG if os.access(OWN_FFMPEG, os.X_OK) else "ffmpeg"
LOG = "/tmp/lynx-stream.log"
DEFAULTS = {"on": False, "always": False, "url": "rtmp://rtmp.batc.org.uk/live", "key": "",
            "video": "h265", "height": 1080, "kbps": 4000}
HEIGHTS = {720: (1280, 41), 1080: (1920, 42), 2160: (3840, 52)}   # width, H.264 level

_proc = None
_lock = threading.Lock()
status = {"state": "off", "detail": "", "since": 0.0, "kbps": 0, "started": 0.0}
_ffmpeg_version = None


def ffmpeg_version() -> tuple:
    """(major, minor) of the installed ffmpeg, or () if it cannot be read
    (a git build, say): then it is simply tried."""
    global _ffmpeg_version
    if _ffmpeg_version is None:
        try:
            out = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True,
                                 text=True, timeout=10).stdout
            m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
            _ffmpeg_version = (int(m[1]), int(m[2])) if m else ()
            if _ffmpeg_version and _ffmpeg_version < (6, 1):
                ver = _ffmpeg_version
                _ffmpeg_version = None       # look again next time: it may be installed
                return ver
        except (OSError, subprocess.SubprocessError):
            _ffmpeg_version = ()
    return _ffmpeg_version


def redact(text: str, key: str) -> str:
    return text.replace(key, "****") if key else text


def build_cmd(cfg: dict, service_id: int, five_one: bool) -> list:
    url = cfg["url"].rstrip("/") + "/" + cfg["key"]
    audio = (["-c:a", "aac", "-ac", "2", "-b:a", "160k"] if five_one
             else ["-c:a", "copy"])
    return ([ffmpeg_bin(), "-nostdin", "-loglevel", "error",
             "-fflags", "+genpts+discardcorrupt",
             "-analyzeduration", "3000000", "-probesize", "5000000",
             "-f", "mpegts", "-i",
             f"udp://127.0.0.1:{UDP_PORT}?fifo_size=100000&overrun_nonfatal=1&timeout=5000000",
             # Programme 1 only: programme 2 (the carousel) rides in the
             # same stream.
             "-map", f"0:p:{service_id}:v:0", "-map", f"0:p:{service_id}:a:0?",
             "-c:v", "copy"] + audio +
            ["-f", "flv", "-flvflags", "no_duration_filesize",
             "-progress", "pipe:1", "-stats_period", "2", url])


def rtmp_sink() -> str:
    """rtmp2sink (gst-plugins-bad) if present, else the older rtmpsink."""
    try:
        if subprocess.run(["gst-inspect-1.0", "rtmp2sink"], capture_output=True,
                          timeout=10).returncode == 0:
            return "rtmp2sink"
    except (OSError, subprocess.SubprocessError):
        pass
    return "rtmpsink"


def build_h264_cmd(cfg: dict, service_id: int, five_one: bool) -> list:
    url = cfg["url"].rstrip("/") + "/" + cfg["key"]
    h = int(cfg["height"]) if int(cfg["height"]) in HEIGHTS else 1080
    w, level = HEIGHTS[h]
    bps = max(500, min(int(cfg["kbps"]), 40000)) * 1000
    audio = ("d. ! queue ! ac3parse ! avdec_ac3 ! audioconvert ! audioresample ! "
             "audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=160000 ! aacparse"
             if five_one else "d. ! queue ! aacparse")
    pipe = (f"udpsrc port={UDP_PORT} buffer-size=4000000 "
            f"caps=video/mpegts,systemstream=true,packetsize=188 ! "
            f"tsdemux name=d program-number={service_id} "
            f"d. ! queue max-size-time=3000000000 max-size-bytes=0 max-size-buffers=0 ! "
            f"h265parse ! mppvideodec format=NV12 width={w} height={h} ! queue ! "
            f"mpph264enc rc-mode=cbr bps={bps} bps-max={bps} gop=50 level={level} ! "
            f"h264parse config-interval=-1 ! queue ! mux. "
            f"{audio} ! queue ! mux. "
            f"flvmux name=mux streamable=true ! {rtmp_sink()} location={url}")
    return ["gst-launch-1.0", "-e", "-q"] + pipe.split()


def _set(state: str, detail: str = ""):
    if state != status["state"]:
        status["since"] = time.time()
    status.update(state=state, detail=detail)


def _read_progress(proc):
    """ffmpeg's -progress output: a block every 2 s. A growing total_size
    means the server is taking the stream."""
    last_size, last_t = 0, time.time()
    for line in proc.stdout:
        k, _, v = line.strip().partition("=")
        if k == "total_size" and v.isdigit():
            now, size = time.time(), int(v)
            if size > last_size:
                status["kbps"] = int((size - last_size) * 8 / 1000 / max(now - last_t, 0.5))
                if status["state"] != "streaming":
                    _set("streaming")
            last_size, last_t = size, now


def _stop():
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
        _proc = None
    status["kbps"] = 0


def watch(get_cfg, feed, service_id, five_one):
    """The supervisor: runs forever in its own thread. get_cfg() gives the
    stream settings; feed() what is making the stream: "tx" (on air),
    "dry" (stream only) or "" (nothing). A change of feed restarts the
    stream cleanly, rather than leaving it to cope with the jump in time."""
    global _proc
    backoff, next_try = 5, 0.0
    last_feed = ""
    while True:
        time.sleep(1)
        cfg = {**DEFAULTS, **(get_cfg() or {})}
        with _lock:
            if not cfg["on"]:
                _stop()
                _set("off")
                backoff, next_try = 5, 0.0
                continue
            if not cfg["url"] or not cfg["key"]:
                _stop()
                _set("error", "Needs a server address and a stream key")
                continue
            now_feed = feed()
            if now_feed != last_feed and _proc is not None:
                _stop()                  # the feed handed over: start afresh
                next_try = 0.0
            last_feed = now_feed
            if not now_feed:
                _stop()
                _set("waiting", "Starts when the transmitter is on")
                backoff, next_try = 5, 0.0
                continue
            h264 = cfg.get("video") == "h264"
            v = ffmpeg_version()
            if not h264 and v and v < (6, 1):
                _set("error", f"ffmpeg {v[0]}.{v[1]} cannot send H.265 over RTMP: "
                              "it needs 6.1 or later. Run sudo bash tools/install-stream-ffmpeg.sh")
                continue
            if _proc is not None and _proc.poll() is not None:
                # It stopped: the server refused, or the network went.
                try:
                    tail = deque(open(LOG, errors="replace"), maxlen=3)
                    why = redact(" ".join(l.strip() for l in tail), cfg["key"])[-300:]
                except OSError:
                    why = ""
                ran = time.time() - status["started"]
                if ran > 60:
                    backoff = 5          # it had been working: retry soon
                _proc = None
                status["kbps"] = 0
                next_try = time.time() + backoff
                _set("retrying", f"Stopped after {int(ran)} s, retrying in {backoff} s"
                                 + (f": {why}" if why else ""))
                backoff = min(backoff * 2, 60)
                continue
            if (h264 and _proc is not None and status["state"] == "connecting"
                    and time.time() - status["started"] > 8):
                # gst-launch reports nothing while it works: still running
                # after 8 s means the server took it.
                status["kbps"] = int(cfg["kbps"])
                _set("streaming")
            if _proc is None and time.time() >= next_try:
                with open(LOG, "w") as log:
                    log.write("")
                build = build_h264_cmd if h264 else build_cmd
                _proc = subprocess.Popen(
                    build(cfg, service_id(), five_one()),
                    stdout=subprocess.PIPE if not h264 else subprocess.DEVNULL,
                    stderr=open(LOG, "a"), text=True)
                status["started"] = time.time()
                _set("connecting", "Connecting to the server")
                if not h264:
                    threading.Thread(target=_read_progress, args=(_proc,), daemon=True).start()


def restart():
    """New settings: drop the current connection; the supervisor makes a new
    one within a second."""
    with _lock:
        _stop()
