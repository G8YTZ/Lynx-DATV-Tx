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

Sound: Stereo AAC is copied. AC-3 (the 5.1 mode) has no place in classic
FLV, so it becomes stereo AAC, which is cheap.

The stream key never appears in the page, the log or /api/stream.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque

UDP_PORT = 5600
LOG = "/tmp/lynx-stream.log"
DEFAULTS = {"on": False, "url": "rtmp://rtmp.batc.org.uk/live", "key": ""}

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
            out = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                                 text=True, timeout=10).stdout
            m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
            _ffmpeg_version = (int(m[1]), int(m[2])) if m else ()
        except (OSError, subprocess.SubprocessError):
            _ffmpeg_version = ()
    return _ffmpeg_version


def redact(text: str, key: str) -> str:
    return text.replace(key, "****") if key else text


def build_cmd(cfg: dict, service_id: int, five_one: bool) -> list:
    url = cfg["url"].rstrip("/") + "/" + cfg["key"]
    audio = (["-c:a", "aac", "-ac", "2", "-b:a", "160k"] if five_one
             else ["-c:a", "copy"])
    return (["ffmpeg", "-nostdin", "-loglevel", "error",
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


def watch(get_cfg, on_air, service_id, five_one):
    """The supervisor: runs forever in its own thread. get_cfg() gives the
    stream settings, on_air() whether the transmitter is running."""
    global _proc
    backoff, next_try = 5, 0.0
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
            if not on_air():
                _stop()
                _set("waiting", "Starts when the transmitter is on")
                backoff, next_try = 5, 0.0
                continue
            v = ffmpeg_version()
            if v and v < (6, 1):
                _set("error", f"ffmpeg {v[0]}.{v[1]} cannot send H.265 over RTMP: "
                              "it needs 6.1 or later")
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
            if _proc is None and time.time() >= next_try:
                with open(LOG, "w") as log:
                    log.write("")
                _proc = subprocess.Popen(
                    build_cmd(cfg, service_id(), five_one()),
                    stdout=subprocess.PIPE, stderr=open(LOG, "a"), text=True)
                status["started"] = time.time()
                _set("connecting", "Connecting to the server")
                threading.Thread(target=_read_progress, args=(_proc,), daemon=True).start()


def restart():
    """New settings: drop the current connection; the supervisor makes a new
    one within a second."""
    with _lock:
        _stop()
