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
ap.add_argument("--seconds", type=int, default=0)
ap.add_argument("--bps", type=int, default=3_800_000)
ap.add_argument("--gop", type=int, default=25)
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
              f"qp-max=51 qp-max-i=51 qp-init=34 qp-min=34 qp-min-i=34 max-reenc=1 gop={a.gop} header-mode=each-idr")
sink = f"filesink location={a.out}" if a.out else "fdsink fd=1"

prog = Gst.parse_launch(
    f"input-selector name=vsel ! identity name=vstamp ! {VCAPS} ! {encode} ! "
    f"h265parse config-interval=-1 ! {Q} ! mux. "
    f"audiomixer name=amix ! {ACAPS} ! audioconvert ! "
    f"avenc_aac bitrate={a.audio_bitrate} ! aacparse ! {Q} ! mux. "
    f"mpegtsmux name=mux ! {sink} "
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
TRIM_FLOOR = 1.0
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
        if u.path == "/load":
            name = os.path.basename((q.get("file") or [""])[0])
            if not name or not (MEDIA_DIR / name).is_file():
                return self._send(404, '{"error":"no such file"}')
            GLib.idle_add(load, name)
            return self._send(200, json.dumps({"loading": name}))
        self._send(404, '{"error":"not found"}')


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
