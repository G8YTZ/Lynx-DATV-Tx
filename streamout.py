"""Lynx-Tx stream out: what is on air, sent on over the internet.

Two outputs, each independent of the other and of the transmitter:

RTMP: For the BATC streamer and the like.
  H.265 as transmitted (copied, as Enhanced RTMP: FLV with the HEVC FourCC).
  Needs ffmpeg 6.1 or later, which Debian Bookworm lacks, so stream out has
  its own (tools/install-stream-ffmpeg.sh). Servers that take only H.264
  refuse it or, like the BATC streamer, keep the sound and drop the
  pictures (tested 27 Nov 2026).
  Or H.264 (hardware): GStreamer decodes and scales the H.265 with
  mppvideodec and encodes with mpph264enc. Almost no CPU, so the IQ
  modulator is never starved, but it shares the video encoder with the
  on-air encode: watch the Transmit card's slack and late count. The BATC
  streamer passes 4K H.264 at 8 Mb/s to external players; its web player
  stops at 1080p (27 Nov 2026).

SRT: For a distribution point (an edge server) that serves the viewers.
  The transport stream exactly as transmitted, by TSDuck (tsp -O srt): both
  programmes, the EPG, the NIT and the time all arrive. Null packets are
  dropped, as they are only padding for the modulator. No encoding at all.
  Falls back to ffmpeg if TSDuck was built without SRT.

How the transmitter is kept out of it: tsbuffer.py, already forked from the
on-air pipeline, sends a fire-and-forget UDP copy to one local port per
output. Nothing waits for it. Each output is its own process: if the
internet drops or the far end hangs up, only that output stops, and it
retries at 5 s, doubling to a minute.

Sound (RTMP): Stereo AAC is copied; AC-3 (the 5.1 mode) becomes stereo AAC.
Secrets (the RTMP key, the SRT passphrase) never appear in the page, the
logs or the API.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque

UDP_PORT = 5600          # RTMP's copy
SRT_UDP_PORT = 5601      # SRT's copy
# Its own ffmpeg when installed (tools/install-stream-ffmpeg.sh): Debian
# Bookworm's 5.1 cannot mux H.265 into FLV, and the transmitter's own
# pipeline stays on the system ffmpeg it was proven with.
OWN_FFMPEG = "/usr/local/lib/lynx-tx/ffmpeg-stream/ffmpeg"

DEFAULTS = {"on": False, "always": False, "url": "rtmp://rtmp.batc.org.uk/live", "key": "",
            "video": "h265", "height": 1080, "kbps": 4000}
SRT_DEFAULTS = {"on": False, "always": False, "host": "", "port": 9000,
                "latency_ms": 500, "passphrase": "", "streamid": ""}
HEIGHTS = {720: (1280, 41), 1080: (1920, 42), 2160: (3840, 51)}   # width, H.264 level (5.1: 4K to 30 fps, the widely decoded one)

_ffmpeg_version = None
_tsp_srt = None


def ffmpeg_bin() -> str:
    return OWN_FFMPEG if os.access(OWN_FFMPEG, os.X_OK) else "ffmpeg"


def ffmpeg_version() -> tuple:
    """(major, minor) of the ffmpeg in use, or () if it cannot be read (a
    git build, say): then it is simply tried."""
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


def tsp_has_srt() -> bool:
    global _tsp_srt
    if _tsp_srt is None:
        try:
            out = subprocess.run(["tsp", "--list-plugins"], capture_output=True,
                                 text=True, timeout=10)
            _tsp_srt = bool(re.search(r"^\s*srt\b", out.stdout + out.stderr, re.M))
        except (OSError, subprocess.SubprocessError):
            _tsp_srt = False
    return _tsp_srt


def rtmp_sink() -> str:
    """rtmp2sink (gst-plugins-bad) if present, else the older rtmpsink."""
    try:
        if subprocess.run(["gst-inspect-1.0", "rtmp2sink"], capture_output=True,
                          timeout=10).returncode == 0:
            return "rtmp2sink"
    except (OSError, subprocess.SubprocessError):
        pass
    return "rtmpsink"


def udp_in(port: int) -> str:
    return (f"udp://127.0.0.1:{port}?fifo_size=100000&overrun_nonfatal=1"
            f"&timeout=5000000")


# ── The commands ─────────────────────────────────────────────────────

def rtmp_copy_cmd(cfg: dict, service_id: int, five_one: bool) -> list:
    url = cfg["url"].rstrip("/") + "/" + cfg["key"]
    audio = (["-c:a", "aac", "-ac", "2", "-b:a", "160k"] if five_one
             else ["-c:a", "copy"])
    return ([ffmpeg_bin(), "-nostdin", "-loglevel", "error",
             "-fflags", "+genpts+discardcorrupt",
             "-analyzeduration", "3000000", "-probesize", "5000000",
             "-f", "mpegts", "-i", udp_in(UDP_PORT),
             # Programme 1 only: programme 2 (the carousel) rides in the
             # same stream.
             "-map", f"0:p:{service_id}:v:0", "-map", f"0:p:{service_id}:a:0?",
             "-c:v", "copy"] + audio +
            ["-f", "flv", "-flvflags", "no_duration_filesize",
             "-progress", "pipe:1", "-stats_period", "2", url])


def rtmp_h264_cmd(cfg: dict, service_id: int, five_one: bool) -> list:
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


def srt_cmd(cfg: dict) -> list:
    host, port = cfg["host"], int(cfg["port"])
    latency = max(20, min(int(cfg["latency_ms"]), 8000))
    if tsp_has_srt():
        cmd = ["tsp", "-I", "ip", str(SRT_UDP_PORT), "--local-address", "127.0.0.1",
               "--receive-timeout", "5000",
               # Null packets are the modulator's padding: no use on the internet.
               "-P", "filter", "--negate", "--pid", "0x1FFF",
               "-O", "srt", "--caller", f"{host}:{port}", "--latency", str(latency)]
        if cfg["passphrase"]:
            cmd += ["--passphrase", cfg["passphrase"]]
        if cfg["streamid"]:
            cmd += ["--streamid", cfg["streamid"]]
        return cmd
    q = f"mode=caller&latency={latency * 1000}&pkt_size=1316"
    if cfg["passphrase"]:
        q += f"&passphrase={cfg['passphrase']}"
    if cfg["streamid"]:
        q += f"&streamid={cfg['streamid']}"
    return [ffmpeg_bin(), "-nostdin", "-loglevel", "error",
            "-f", "mpegts", "-i", udp_in(SRT_UDP_PORT),
            "-map", "0", "-c", "copy", "-f", "mpegts",
            "-progress", "pipe:1", "-stats_period", "2", f"srt://{host}:{port}?{q}"]


# ── An output and its supervisor ─────────────────────────────────────

class Output:
    def __init__(self, name: str, defaults: dict, log: str):
        self.name, self.defaults, self.log = name, defaults, log
        self.proc = None
        self.lock = threading.Lock()
        self.status = {"state": "off", "detail": "", "since": 0.0, "kbps": 0,
                       "started": 0.0, "tool": ""}

    # What each kind needs: overridden by the two below.
    def ready(self, cfg) -> str:
        return ""

    def secrets(self, cfg) -> list:
        return []

    def command(self, cfg, service_id, five_one) -> tuple:
        raise NotImplementedError

    def _set(self, state: str, detail: str = ""):
        if state != self.status["state"]:
            self.status["since"] = time.time()
        self.status.update(state=state, detail=detail)

    def _read_progress(self, proc):
        """ffmpeg's -progress output: a block every 2 s. A growing
        total_size means the far end is taking the stream."""
        last_size, last_t = 0, time.time()
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "total_size" and v.isdigit():
                now, size = time.time(), int(v)
                if size > last_size:
                    self.status["kbps"] = int((size - last_size) * 8 / 1000 / max(now - last_t, 0.5))
                    if self.status["state"] != "streaming":
                        self._set("streaming")
                last_size, last_t = size, now

    def _stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.status["kbps"] = 0

    def restart(self):
        """New settings: drop the current connection; the supervisor makes
        a new one within a second."""
        with self.lock:
            self._stop()

    def redact(self, text: str, cfg) -> str:
        for s in self.secrets(cfg):
            if s:
                text = text.replace(s, "****")
        return text

    def watch(self, get_cfg, feed, service_id, five_one):
        """Runs forever in its own thread. get_cfg() gives this output's
        settings; feed() what is making the stream: "tx" (on air), "dry"
        (stream only) or "" (nothing). A change of feed restarts the output
        cleanly, rather than leaving it to cope with the jump in time."""
        backoff, next_try, last_feed = 5, 0.0, ""
        while True:
            time.sleep(1)
            try:
                cfg = {**self.defaults, **(get_cfg() or {})}
                with self.lock:
                    if not cfg["on"]:
                        self._stop()
                        self._set("off")
                        backoff, next_try = 5, 0.0
                        continue
                    why = self.ready(cfg)
                    if why:
                        self._stop()
                        self._set("error", why)
                        continue
                    now_feed = feed()
                    if now_feed != last_feed and self.proc is not None:
                        self._stop()             # the feed handed over: start afresh
                        next_try = 0.0
                    last_feed = now_feed
                    if not now_feed:
                        self._stop()
                        self._set("waiting", "Starts when the transmitter is on")
                        backoff, next_try = 5, 0.0
                        continue
                    if self.proc is not None and self.proc.poll() is not None:
                        # It stopped: the far end refused, or the network went.
                        try:
                            tail = deque(open(self.log, errors="replace"), maxlen=3)
                            why = self.redact(" ".join(l.strip() for l in tail), cfg)[-300:]
                        except OSError:
                            why = ""
                        ran = time.time() - self.status["started"]
                        if ran > 60:
                            backoff = 5          # it had been working: retry soon
                        self.proc = None
                        self.status["kbps"] = 0
                        next_try = time.time() + backoff
                        self._set("retrying", f"Stopped after {int(ran)} s, retrying in {backoff} s"
                                              + (f": {why}" if why else ""))
                        backoff = min(backoff * 2, 60)
                        continue
                    if (self.proc is not None and self.status["state"] == "connecting"
                            and not self.status.get("progress")
                            and time.time() - self.status["started"] > 8):
                        # gst-launch and tsp report nothing while they work:
                        # still running after 8 s means the far end took it.
                        self.status["kbps"] = self.status.get("set_kbps", 0)
                        self._set("streaming")
                    if self.proc is None and time.time() >= next_try:
                        cmd, progress, set_kbps = self.command(cfg, service_id(), five_one())
                        with open(self.log, "w") as log:
                            log.write("")
                        self.proc = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE if progress else subprocess.DEVNULL,
                            stderr=open(self.log, "a"), text=True)
                        self.status.update(started=time.time(), progress=progress,
                                           set_kbps=set_kbps, tool=os.path.basename(cmd[0]))
                        self._set("connecting", "Connecting")
                        if progress:
                            threading.Thread(target=self._read_progress, args=(self.proc,),
                                             daemon=True).start()
            except Exception as e:
                print(f"[stream {self.name}] {e}")


class RtmpOutput(Output):
    def ready(self, cfg):
        if not cfg["url"] or not cfg["key"]:
            return "Needs a server address and a stream key"
        v = ffmpeg_version()
        if cfg.get("video") != "h264" and v and v < (6, 1):
            return (f"ffmpeg {v[0]}.{v[1]} cannot send H.265 over RTMP: it needs 6.1 or "
                    "later. Run sudo bash tools/install-stream-ffmpeg.sh, or choose H.264")
        return ""

    def secrets(self, cfg):
        return [cfg["key"]]

    def command(self, cfg, service_id, five_one):
        if cfg.get("video") == "h264":
            return rtmp_h264_cmd(cfg, service_id, five_one), False, int(cfg["kbps"])
        return rtmp_copy_cmd(cfg, service_id, five_one), True, 0


class SrtOutput(Output):
    def ready(self, cfg):
        if not cfg["host"]:
            return "Needs the address of the far end"
        return ""

    def secrets(self, cfg):
        return [cfg["passphrase"]]

    def command(self, cfg, service_id, five_one):
        cmd = srt_cmd(cfg)
        return cmd, cmd[0] != "tsp", 0


rtmp = RtmpOutput("rtmp", DEFAULTS, "/tmp/lynx-stream.log")
srt = SrtOutput("srt", SRT_DEFAULTS, "/tmp/lynx-srt.log")
status = rtmp.status            # the RTMP output's, as before


def restart():
    rtmp.restart()


def watch(get_cfg, feed, service_id, five_one):
    rtmp.watch(get_cfg, feed, service_id, five_one)
