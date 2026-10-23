#!/usr/bin/env python3
"""Lynx-Tx programme 2: a carousel of 4K stills, as its own service.

Writes a transport stream on stdout: one HEVC service, no sound, a new still
every --seconds from --dir (in name order, so 01_..., 02_... set the
sequence), each held on screen at 25 frames a second. lynx_tx.py runs it as
TSDuck's merge command, which puts its packets in place of the main
stream's null packets: the live programme loses nothing, and programme 2
lives on the stuffing the channel carries anyway.

Why it is cheap: a still costs one keyframe (about 0.5 Mbit at 4K, QP 34),
repeated every --keyframe seconds so a receiver switching over gets a
picture, and almost nothing in between (the same picture, frame after
frame). Fixed QP, so every still looks the same whatever is in it.

    carousel.py --dir /srv/lynxdeck/media/Carousel --seconds 60 \\
        --service-id 2 --name "GB3OO 4K" --provider GB3OO --tsid 1 --onid 0xFF01

A summary goes to stderr every minute.
"""
import argparse
import os
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True)
ap.add_argument("--seconds", type=float, default=60, help="each still on screen, s")
ap.add_argument("--keyframe", type=float, default=10, help="keyframe repeat, s")
ap.add_argument("--qp", type=int, default=34)
ap.add_argument("--width", type=int, default=3840)
ap.add_argument("--height", type=int, default=2160)
ap.add_argument("--service-id", type=lambda v: int(v, 0), default=2)
ap.add_argument("--name", default="Lynx 4K")
ap.add_argument("--provider", default="")
ap.add_argument("--tsid", type=lambda v: int(v, 0), default=1)
ap.add_argument("--onid", type=lambda v: int(v, 0), default=0xFF01)
ap.add_argument("--service-type", type=lambda v: int(v, 0), default=0x1F)
ap.add_argument("--pmt-pid", type=lambda v: int(v, 0), default=0x200)
ap.add_argument("--video-pid", type=lambda v: int(v, 0), default=0x201)
ap.add_argument("--delay", type=float, default=4.0,
                help="how far the stream clock runs ahead of the pictures, s: a keyframe "
                     "travels in the main stream's spare packets, which takes seconds")
ap.add_argument("--pad", type=float, default=2e6,
                help="output rate with null packets, b/s (see the ffmpeg call)")
ap.add_argument("--sim", action="store_true", help="software encoder (testing off the ROCK)")
a = ap.parse_args()

FPS = 25
EXT = (".png", ".jpg", ".jpeg", ".bmp")
W, H = a.width, a.height
CAPS = f"video/x-raw,format=NV12,width={W},height={H},framerate={FPS}/1,pixel-aspect-ratio=1/1"


def log(*x):
    print("[carousel]", *x, file=sys.stderr, flush=True)


Gst.init(None)


def stills():
    try:
        return sorted(os.path.join(a.dir, f) for f in os.listdir(a.dir)
                      if f.lower().endswith(EXT) and not f.startswith("."))
    except OSError:
        return []


def load(path):
    """One still as a programme-size NV12 frame, letterboxed to keep its
    shape; None if it cannot be read."""
    src = (f'filesrc location="{path}" ! decodebin' if path
           else "videotestsrc num-buffers=1 pattern=smpte100")
    p = Gst.parse_launch(
        f"{src} ! videoconvert ! videoscale add-borders=true ! "
        f"video/x-raw,format=NV12,width={W},height={H},pixel-aspect-ratio=1/1 ! "
        f"appsink name=s max-buffers=1 sync=false")
    p.set_state(Gst.State.PLAYING)
    sample = p.get_by_name("s").emit("try-pull-sample", 20 * Gst.SECOND)
    p.set_state(Gst.State.NULL)
    return sample.get_buffer() if sample else None


gop = max(1, int(round(a.keyframe * FPS)))
if a.sim:
    enc = (f"videoconvert ! x265enc speed-preset=ultrafast key-int-max={gop} "
           f'option-string="qp={a.qp}:keyint={gop}:min-keyint={gop}:scenecut=0"')
else:
    enc = (f"mpph265enc rc-mode=fixqp qp-init={a.qp} qp-min={a.qp} qp-max={a.qp} "
           f"qp-min-i={a.qp} qp-max-i={a.qp} gop={gop} header-mode=each-idr")
# The IDs, names and PIDs, by ffmpeg (as the main programme's): copy only.
ff = subprocess.Popen(
    ["ffmpeg", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0",
     "-c", "copy", "-f", "mpegts",
     "-metadata", f"service_name={a.name}", "-metadata", f"service_provider={a.provider}",
     "-mpegts_original_network_id", str(a.onid), "-mpegts_transport_stream_id", str(a.tsid),
     "-mpegts_service_id", str(a.service_id), "-mpegts_service_type", str(a.service_type),
     "-mpegts_pmt_start_pid", str(a.pmt_pid), "-streamid", f"0:{a.video_pid}",
     "-max_delay", str(int(a.delay * 1e6)), "-pat_period", "0.5", "-sdt_period", "1",
     # Padded with null packets to --pad b/s: TSDuck's merge reads its
     # command in ~95 kB blocks, which a bare 100 kb/s stream takes 8 s to
     # fill (the pictures then arrive too late). The nulls are not merged.
     "-muxrate", str(int(a.pad)), "pipe:1"],
    stdin=subprocess.PIPE, stdout=sys.stdout.buffer)
pipe = Gst.parse_launch(
    f"appsrc name=src is-live=true format=time do-timestamp=false caps={CAPS} ! "
    f"queue max-size-buffers=4 ! {enc} ! h265parse name=parse config-interval=-1 ! "
    f"video/x-h265,stream-format=byte-stream ! mpegtsmux alignment=7 ! fdsink fd={ff.stdin.fileno()} sync=false")


src = pipe.get_by_name("src")
counted = {"bytes": 0}


def count(pad, info):
    counted["bytes"] += info.get_buffer().get_size()
    return Gst.PadProbeReturn.OK


pipe.get_by_name("parse").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, count)

pipe.set_state(Gst.State.PLAYING)


def feeder():
    frame = Gst.SECOND // FPS
    n = 0
    t0 = time.monotonic()
    idx = 0
    still = None
    shown_until = 0
    last_log = t0
    while True:
        now_n = n
        if now_n >= shown_until:
            files = stills()
            path = files[idx % len(files)] if files else None
            idx += 1
            new = load(path)
            if new is None:
                log(f"could not read {path}: skipped")
                new = still or load(None)
            still = new
            shown_until = now_n + int(a.seconds * FPS)
            log(f"showing {os.path.basename(path) if path else 'colour bars (no stills in ' + a.dir + ')'}")
            # A new picture: a keyframe at once, not at the next in the GOP.
            src.send_event(GstVideo.video_event_new_downstream_force_key_unit(
                n * frame, n * frame, n * frame, True, 0))
        b = still.copy()               # shares the picture; new timestamps only
        b.pts = b.dts = n * frame
        b.duration = frame
        if src.emit("push-buffer", b) != Gst.FlowReturn.OK:
            log("encoder stopped")
            os._exit(1)
        n += 1
        # Real time: the merge takes packets as the main stream has room.
        wait = t0 + n / FPS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        if time.monotonic() - last_log >= 60:
            secs = time.monotonic() - last_log
            log(f"{counted['bytes'] * 8 / secs / 1000:.0f} kb/s of pictures over the last minute")
            counted["bytes"] = 0
            last_log = time.monotonic()


threading.Thread(target=feeder, daemon=True).start()
bus = pipe.get_bus()
while True:
    msg = bus.timed_pop_filtered(Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
    if msg:
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log(f"error: {err.message}")
        os._exit(1)
    if ff.poll() is not None:
        log("ffmpeg stopped")
        os._exit(1)
