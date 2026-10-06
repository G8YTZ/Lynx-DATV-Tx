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

  ffmpeg       remuxes to an exact constant rate, on the PIDs and with
               the IDs set in the Identity card. GStreamer's muxer
               numbers video 0x41 and chooses its own IDs; nothing
               downstream should have to live with that.

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

Service information
-------------------
This end owns every table a receiver reads: PAT, PMT and SDT from
ffmpeg, the NIT (network name, delivery parameters, LCN) built here and
injected by tsp. The LibreSDR's own SDT rewrite is removed by
ensure_libre_patched(), because it hard-codes service ID 1 and cuts
names at the first space. A TV remembers a channel by the triplet
original network ID / transport stream ID / service ID - change any of
those and every viewer has to rescan, whatever the names say.

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
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import yaml
from xml.sax.saxutils import quoteattr
import json
from urllib.parse import unquote
from fastapi import FastAPI, HTTPException, Request, Response
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
    # By name, not number: the HDMI receiver's card number moves
    # between boots (it has been 2, 3 and 4).
    "audio_device": "hw:CARD=rockchiphdmiin,DEV=0",
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
    # DVB service information. Network > multiplex > provider > service.
    # The triplet original_network_id / ts_id / service_id is what a TV
    # stores; the names are only what it displays.
    "identity": {
        "network_name": "Amateur DATV",
        "network_id": 0xFF01,           # 0xFF01-0xFFFF: private use
        "original_network_id": 0xFF01,
        "ts_id": 1,                     # one per carrier, not per site
        "service_id": 1,                # unique across the network
        "service_type": "digital_tv",
        "nit": True,
        "lcn": 1,
        "lcn_visible": True,
        "pmt_pid": 0x100,
        "video_pid": 0x101,             # PCR travels here too
        "audio_pid": 0x102,
        "polarization": "horizontal",
    },
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

def read_buffer() -> Optional[dict]:
    """The live slack from tsbuffer.py, or None if it is not reporting."""
    try:
        with open("/tmp/lynx-buffer.json") as f:
            b = json.load(f)
        return b if time.time() - b.get("time", 0) < 3 else None
    except Exception:
        return None


def libre_host() -> str:
    """Where the LibreSDR is right now.

    On a direct cable to the ROCK's second port it is always the fixed
    direct address (the ROCK hands it out by DHCP); anywhere else - a
    switch, the demo network - it is the configured host, as before. The
    choice follows the second port's carrier, so plugging the cable in or
    out is all it takes.
    """
    lib = config["libre"]
    port = lib.get("direct_if", "enP4p65s0")
    try:
        if Path(f"/sys/class/net/{port}/carrier").read_text().strip() == "1":
            return lib.get("direct_host", "192.168.254.2")
    except OSError:
        pass
    return lib["host"]


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
             "-o", "StrictHostKeyChecking=accept-new",
             f"{lib['user']}@{libre_host()}", cmd],
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


# ── Keeping the LibreSDR's script compatible ─────────────────
#
# DL1JM's /opt/udpts.sh rewrites the SDT on its way to the modulator
# ("tsp -P sdt -n $CALL -p $MESSAGE -i -s 1"). That hard-codes service
# ID 1, cuts names at the first space, and prepends the firmware
# version to the provider. This end owns the service information, so
# the rewrite is removed; and the board's PCR monitor is pointed at the
# PID the PCR actually travels on.
#
# Rules, not a copy of his script: nothing of DL1JM's is redistributed,
# a replacement board fixes itself, and a firmware that has changed
# those lines is simply left alone. The script runs as a long-lived
# bash loop that keeps the copy it loaded at boot, so a change only
# takes effect after a reboot - done here, and only when idle.

UDPTS = "/opt/udpts.sh"
_SDT_STAGE = re.compile(
    r' -P sdt -n (?:"\$CALL"|\$CALL) -p (?:"\$MESSAGE"|\$MESSAGE) -i -s \d+')
_PCR_PID = re.compile(r'(-P pcrextract\b[^|\n]*?--pid )(\d+)')
_LIVE_LINE = re.compile(r'^tsp .*-I ip 0\.0\.0\.0:8282.*pluto_dvb', re.M)

libre_lock = threading.Lock()
libre_patch = {"state": "unknown", "detail": "Not checked yet", "at": 0}


def libre_ssh_full(cmd: str, stdin: Optional[str] = None,
                   timeout: int = 15) -> tuple:
    """As libre_ssh, but with the return code and stderr - needed to
    tell 'unreachable' from 'refuses our key'."""
    lib = config["libre"]
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{lib['user']}@{libre_host()}", cmd],
            input=stdin, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 255, "", str(e)


def patch_udpts(text: str) -> str:
    """The rules. Idempotent: applying them twice changes nothing."""
    text = _SDT_STAGE.sub("", text)
    pid = str(config["identity"]["video_pid"])
    text = _PCR_PID.sub(lambda m: m.group(1) + pid, text)
    return text


def _set_libre(state: str, detail: str):
    libre_patch.update(state=state, detail=detail, at=int(time.time()))
    print(f"[libre-patch] {state}: {detail}")


def ensure_libre_patched(apply: bool = True) -> dict:
    """Check the board's script and, if allowed, correct it and reboot.

    Never while transmitting: a reboot mid-over is worse than a stale
    table. A needed change found then is reported as pending and made
    on the next idle check.
    """
    with libre_lock:
        rc, text, err = libre_ssh_full(f"cat {UDPTS}")
        host = libre_host()
        if rc == 255:
            if "Permission denied" in err:
                _set_libre("unpaired",
                           f"The LibreSDR refuses this ROCK's key. Pair it "
                           f"once from the ROCK: ssh-copy-id root@{host}")
            else:
                _set_libre("unreachable", f"No answer from {host}")
            return dict(libre_patch)
        if rc != 0 or "pluto_dvb" not in text or not _LIVE_LINE.search(text):
            _set_libre("unrecognised",
                       f"{UDPTS} is not the script these rules know - "
                       "left untouched")
            return dict(libre_patch)

        new = patch_udpts(text)
        if new == text:
            _set_libre("ok", "Script compatible")
            return dict(libre_patch)
        if not apply or tx_running():
            _set_libre("pending", "Script needs updating - will apply "
                                  "when the transmitter is idle")
            return dict(libre_patch)

        # Write beside it, syntax-check, keep the first original we
        # ever saw, then move into place.
        rc, _, err = libre_ssh_full(
            f"cat > {UDPTS}.lynx-new && bash -n {UDPTS}.lynx-new && "
            f"( [ -e {UDPTS}.orig-lynx ] || cp -p {UDPTS} {UDPTS}.orig-lynx ) && "
            f"chmod 755 {UDPTS}.lynx-new && mv {UDPTS}.lynx-new {UDPTS}",
            stdin=new)
        if rc != 0:
            _set_libre("error", f"Could not update the script: {err.strip()}")
            return dict(libre_patch)
        rc, check, _ = libre_ssh_full(f"cat {UDPTS}")
        if rc != 0 or patch_udpts(check) != check:
            _set_libre("error", "Update did not verify - check the board")
            return dict(libre_patch)
        libre_ssh_full("nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 &",
                       timeout=8)
        _set_libre("rebooting", "Script updated - LibreSDR rebooting "
                                "(about a minute)")
        return dict(libre_patch)


def libre_patch_watch():
    """Check at start-up and every ten minutes, so a board swapped on
    site is corrected without anyone remembering to ask."""
    time.sleep(5)
    while True:
        try:
            ensure_libre_patched(apply=True)
        except Exception as e:
            _set_libre("error", str(e))
        time.sleep(600)


# ── The network information table ─────────────────────────────

NIT_DIR = APP_DIR / "run"
_SERVICE_TYPE_CODE = {
    "digital_tv": 0x01,
    "advanced_codec_digital_hdtv": 0x19,
    "hevc_digital_hdtv": 0x1F,
}
_NIT_MODULATION = {"QPSK": "QPSK", "8PSK": "8PSK", "16APSK": "16-QAM",
                   "32APSK": "auto"}
nit_status = {"ok": False, "detail": "Not built yet"}


def build_nit() -> Optional[Path]:
    """The NIT as TSDuck XML, compiled to a section file for tsp.

    Carries the network name, the carrier's own delivery parameters
    (taken from the Modulation card, so it can never disagree with
    what is on air) and the LCN. Returns None if disabled or if the
    table will not compile - better no NIT than a broken one.
    """
    ident = config["identity"]
    if not ident.get("nit", True):
        nit_status.update(ok=False, detail="Disabled")
        return None
    m = config["modulation"]
    stype = _SERVICE_TYPE_CODE.get(ident["service_type"], 0x01)
    sid = ident["service_id"]
    lcn = ""
    if ident.get("lcn"):
        lcn = (f'      <private_data_specifier_descriptor private_data_specifier="EACEM"/>\n'
               f'      <eacem_logical_channel_number_descriptor>\n'
               f'        <service service_id="{sid}" logical_channel_number="{int(ident["lcn"])}" '
               f'visible_service="{"true" if ident.get("lcn_visible", True) else "false"}"/>\n'
               f'      </eacem_logical_channel_number_descriptor>\n')
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n'
        f'  <NIT version="0" current="true" network_id="{ident["network_id"]}" actual="true">\n'
        f'    <network_name_descriptor network_name={quoteattr(ident["network_name"])}/>\n'
        f'    <transport_stream transport_stream_id="{ident["ts_id"]}" '
        f'original_network_id="{ident["original_network_id"]}">\n'
        f'      <service_list_descriptor>\n'
        f'        <service service_id="{sid}" service_type="{stype}"/>\n'
        f'      </service_list_descriptor>\n'
        f'      <satellite_delivery_system_descriptor '
        f'frequency="{int(round(m["frequency_mhz"] * 1e6))}" orbital_position="0.0" '
        f'west_east_flag="east" polarization="{ident.get("polarization", "horizontal")}" '
        f'roll_off="{m["rolloff"]}" modulation_system="DVB-S2" '
        f'modulation_type="{_NIT_MODULATION.get(m["constellation"].upper(), "auto")}" '
        f'symbol_rate="{int(m["symbol_rate_ks"]) * 1000}" FEC_inner="{m["fec"]}"/>\n'
        f'{lcn}'
        f'    </transport_stream>\n  </NIT>\n</tsduck>\n')
    NIT_DIR.mkdir(parents=True, exist_ok=True)
    src, out = NIT_DIR / "nit.xml", NIT_DIR / "nit.bin"
    src.write_text(xml)
    r = subprocess.run(["tstabcomp", str(src), "-o", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        nit_status.update(ok=False, detail=(r.stderr or "tstabcomp failed").strip())
        print(f"[nit] not sent: {nit_status['detail']}")
        return None
    nit_status.update(ok=True, detail=f"{out.stat().st_size} bytes")
    return out


# ── The pipeline ──────────────────────────────────────────────

tx_proc: Optional[subprocess.Popen] = None
tx_lock = threading.Lock()
tx_started_at = 0.0
tx_last_command = ""


def hdmi_framerate(device: str) -> Optional[str]:
    """The HDMI input's real frame rate as a GStreamer fraction.

    The receiver does not enforce a framerate in caps: ask for 50/1
    and v4l2src agrees, then delivers 25. The encoder believes the
    caps, so take the truth from the DV timings instead.
    """
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--query-dv-timings"],
            capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    m = re.search(r"\(([\d.]+) frames per second\)", out)
    if not m:
        return None
    f = float(m.group(1))
    for nominal in (24, 30, 60):        # 23.98, 29.97, 59.94
        if abs(f - nominal * 1000 / 1001) < 0.01:
            return f"{nominal * 1000}/1001"
    return f"{round(f)}/1"


def hdmi_format() -> dict:
    """What the camera is actually sending, or {} when nothing is.

    The live signal timing (--query-dv-timings), not the capture format:
    with no signal the HDMI chip keeps reporting a leftover default format
    (640x480), and believing it made the whole programme 640x480 - a
    squashed test card and black media. No signal means {}, and each caller
    falls back to its own size (the switcher: 3840x2160).
    """
    try:
        r = subprocess.run(
            ["v4l2-ctl", "-d", config["video_device"], "--query-dv-timings"],
            capture_output=True, text=True, timeout=4)
        w = h = 0
        for line in r.stdout.split("\n"):
            if "Active width" in line:
                w = int(line.split(":")[1].strip() or 0)
            elif "Active height" in line:
                h = int(line.split(":")[1].strip() or 0)
        return {"width": w, "height": h} if r.returncode == 0 and w > 0 and h > 0 else {}
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


_probe_cache: dict = {}


def probe_media(path: Path) -> dict:
    """What a file actually is - not what its name says.

    "Ayo4k.MOV" turned out to be 1080p HLG at 30 fps, and an iPhone file
    carries its thumbnail as a second "video" stream; both are things to
    know before putting a file on air. Cached by size and time, because
    ffprobe on a 6 GB file is not free.
    """
    st = path.stat()
    key = (path.name, st.st_size, int(st.st_mtime))
    if key in _probe_cache:
        return _probe_cache[key]
    info = {"ok": False}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,profile,width,height,r_frame_rate,"
             "pix_fmt,color_transfer:stream_disposition=attached_pic:"
             "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams", [])
        video = [x for x in streams if x.get("codec_type") == "video"
                 and not (x.get("disposition") or {}).get("attached_pic")]
        audio = [x for x in streams if x.get("codec_type") == "audio"]
        if video:
            v = video[0]
            num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
            fps = float(num) / float(den or 1) if float(den or 1) else 0.0
            tr = v.get("color_transfer", "")
            info = {
                "ok": True,
                "codec": v.get("codec_name", ""),
                "profile": v.get("profile", ""),
                "width": v.get("width", 0), "height": v.get("height", 0),
                "fps": round(fps, 3),
                "bits": 10 if "10" in (v.get("pix_fmt") or "") else 8,
                "hdr": {"arib-std-b67": "HLG", "smpte2084": "PQ"}.get(tr, ""),
                "audio": audio[0].get("codec_name", "") if audio else "",
                "duration": float((d.get("format") or {}).get("duration") or 0),
            }
    except Exception as e:
        info = {"ok": False, "error": str(e)}
    _probe_cache[key] = info
    return info


def _media_name(name: str) -> str:
    """A bare file name with an extension we play - nothing else."""
    n = unquote(name).strip()
    if not n or n.startswith(".") or "/" in n or "\\" in n:
        raise HTTPException(400, "bad file name")
    if Path(n).suffix.lower() not in VIDEO_EXT | STILL_EXT:
        raise HTTPException(400, "not a video or still this can play")
    return n


def _on_air_file() -> str:
    src = config["source"]
    return src["file"] if src["kind"] != "camera" and tx_running() else ""


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
                 if config.get("use_switcher", True) or
                 (enc["audio"] and src["kind"] != "still") else 0)
    psi_bps = 12000
    # The slack: how far ahead of need the pictures travel (ffmpeg's
    # -max_delay). More rides out encoder overshoots, but every receiver
    # has to hold it: a 2014 Sony Bravia was happy at 0.4 s and gave up
    # ("scrambled or cannot be decoded") at 1.0 s and above. So it is a
    # setting, encoder.slack, and the trim in switcher.py scales with it.
    slack = float(enc.get("slack", 0.6))
    # 85%: the rest is headroom for the encoder's brief excursions above
    # its cap, so sound and tables always find room in the channel.
    # The picture's share of what sound and tables leave (encoder.
    # picture_share, default 0.85); the rest is fixed headroom for the
    # encoder's overshoots - steadier than leaning on the trim alone,
    # which hunted when pushed to react faster.
    # With frame-by-frame rate control (encoder.rate_control: buffer) the
    # encoder no longer overshoots, so the picture can have far more of the
    # channel: encoder.picture_share_rc, default 0.92.
    # Hybrid (encoder.rate_control: hybrid): the encoder keeps its own rate
    # control and adaptive quantisation, and ratectl.py only raises its QP
    # floor, frame by frame, when the receiver's buffer is threatened. It
    # uses picture_share like cbr - raise that once hybrid has proved itself.
    rate_control = enc.get("rate_control", "cbr")
    if rate_control not in ("cbr", "buffer", "hybrid"):
        rate_control = "cbr"
    share = (float(enc.get("picture_share_rc", 0.92)) if rate_control == "buffer"
             else float(enc.get("picture_share", 0.85)))
    video_bps = int((rate - audio_bps - psi_bps) * share)
    # What the picture can really carry, in encoder bits per second - the
    # rate the hybrid controller models the receiver's buffer draining at.
    # Counted in 188-byte packets as tsprio sends them: sound (one PES per
    # 21 ms AAC frame, ~3.5 packets each), PCR-only packets every 35 ms and
    # tables come first; the picture gets the rest, at 184 payload bytes a
    # packet, less a PES header and half a packet of stuffing per frame;
    # then 3% margin. (Taking rate - sound - tables with no overheads, as
    # first tried, was ~6% optimistic: pictures went up to 0.6 s late.)
    # Independent of picture_share, so a bigger share means the floor works
    # harder - not that the controller believes there is more channel.
    pkts = rate / 1504.0
    aac_fps = 48000 / 1024.0
    sound_pkts = aac_fps * ((enc["audio_bitrate"] / 8 / aac_fps + 21) / 184 + 0.6)
    pic_pkts = pkts - sound_pkts - 1 / 0.035 - 12
    picture_channel_bps = int((pic_pkts * 184 * 8 - 25 * (20 + 92) * 8) * 0.97)
    # Hybrid: the encoder's own target must sit INSIDE that channel. With
    # it set from picture_share it was ~1% above, so the encoder always
    # wanted more than the channel carries; every frame the floor held back
    # it then tried to win back (its CBR running "under budget"), and with
    # the floor at 30 the two controllers fought for minutes on end with
    # the floor pinned at 43+. Aim the encoder at 97% of the real channel (itself 3% under the true figure)
    # (encoder.hybrid_aim): the floor is then for bursts only.
    if rate_control == "hybrid":
        video_bps = int(picture_channel_bps * float(enc.get("hybrid_aim", 0.97)))

    w, h, fps = enc["width"], enc["height"], enc["fps"]
    reader = ""
    media_audio = None

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
        framerate = hdmi_framerate(device) or f"{fps}/1"
        caps = (f"video/x-raw,format=NV16,width={w},height={h},"
                f"framerate={framerate}")
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
    if config.get("use_switcher", True):
        # The switcher reads its own sources and works out for itself
        # whether a clip is a video or a still: the single-source checks
        # below would only refuse things it handles (a PNG left recorded
        # as "video" stopped key-up altogether).
        video_in = ""
    elif src["kind"] == "camera":
        video_in = f"v4l2src device={device} ! {caps}"
    elif src["kind"] == "still":
        # A still never changes, so it is decoded, converted and scaled
        # ONCE, and imagefreeze hands the encoder that same frame 25
        # times a second - no feeder, no loopback, next to no CPU. The
        # old route had ffmpeg decode the whole 4K PNG again for every
        # frame, in software, which no 4K still could keep up with.
        # videoscale letterboxes rather than stretches an odd shape.
        still = MEDIA_DIR / src["file"]
        # The picture's own size, not the encoder default: a 4K test
        # card goes out as 4K. Rounded down to even numbers, which the
        # encoder needs, and capped at UHD.
        info = probe_media(still) if still.exists() else {}
        if info.get("width") and info.get("height"):
            w = min(int(info["width"]), 3840) // 2 * 2
            h = min(int(info["height"]), 2160) // 2 * 2
        video_in = (f"filesrc location={shlex.quote(str(still))} ! decodebin ! "
                    f"videoconvert ! videoscale ! "
                    f"video/x-raw,format=NV12,width={w},height={h},pixel-aspect-ratio=1/1 ! "
                    f"imagefreeze is-live=true ! video/x-raw,framerate=25/1")
    elif src["kind"] == "video":
        # ffmpeg reads the file and repackages it as a transport stream -
        # no decoding (-c copy) - at real speed (-re), looping for ever
        # with timestamps that carry on across the join (-stream_loop).
        # GStreamer decodes that in hardware (mppvideodec, about 12x real
        # time for 4K HEVC) and feeds the encoder. Sound comes from the
        # same stream, so it cannot drift; the old route read the file
        # twice and the two copies wandered apart.
        #
        # Each branch starts with its parser, straight after the demuxer:
        # the parser only accepts its own kind of stream, so the video
        # branch always gets the video and the audio branch the audio.
        media = MEDIA_DIR / src["file"]
        info = probe_media(media) if media.exists() else {}
        if not info.get("ok"):
            raise HTTPException(400, f"{src['file']}: cannot read this file")
        parser = {"hevc": "h265parse", "h264": "h264parse"}.get(info["codec"])
        if not parser:
            raise HTTPException(400, f"{src['file']}: {info['codec']} cannot be "
                                     "decoded in hardware - export it as H.265 or H.264")
        if info.get("bits") == 10:
            raise HTTPException(400, f"{src['file']}: 10-bit - export it as "
                                     "8-bit (H.265 Main), the house standard")
        media_audio = {
            "aac": "aacparse ! queue ! avdec_aac",
            "mp3": "mpegaudioparse ! queue ! avdec_mp3",
            "ac3": "ac3parse ! queue ! avdec_ac3",
            "eac3": "ac3parse ! queue ! avdec_eac3",
        }.get(info.get("audio", ""))
        reader = (f"ffmpeg -nostdin -loglevel warning -re -stream_loop -1 "
                  f"-i {shlex.quote(str(media))} -map 0:v:0 "
                  f"{'-map 0:a:0 ' if media_audio else ''}"
                  f"-c copy -f mpegts - | ")
        video_in = (f"fdsrc ! tsdemux name=td td. ! {parser} ! "
                    f"queue max-size-time=3000000000 max-size-bytes=0 max-size-buffers=0 ! "
                    f"mppvideodec ! video/x-raw,format=NV12")
    else:
        video_in = (f"v4l2src device={device} ! {caps} ! "
                    f"videoconvert ! video/x-raw,format=NV12")

    # -- encode and mux --------------------------------------------
    # header-mode=each-idr and config-interval=-1 together repeat the
    # parameter sets at every keyframe. A decoder joining mid-stream
    # has everything it needs within one GOP rather than waiting for a
    # sequence header that may never come again.
    if config.get("use_switcher", True):
        # The normal mode: an ATEM-style programme bus, Camera | Media,
        # cut cleanly in front of one encoder that never stops
        # (switcher.py, beside this file). It writes the programme
        # transport stream to stdout, so from here on the chain is the
        # same as for any source: the remux with our IDs, the NIT, the
        # pacing, the LibreSDR. "use_switcher: false" in the config falls
        # back to the single-source modes below.
        #
        # Programme size follows the HDMI input, or UHD with no signal
        # (the Camera input shows colour bars until one arrives).
        fmt = hdmi_format()
        w, h = (fmt["width"], fmt["height"]) if fmt.get("width") else (3840, 2160)
        clip = src.get("file") or ""
        if clip and not (MEDIA_DIR / clip).is_file():
            clip = ""
        start = src.get("program") or ("med" if src["kind"] in ("video", "still") and clip else "cam")
        parts = [
            f"python3 {shlex.quote(str(APP_DIR / 'switcher.py'))} "
            f"--media-dir {shlex.quote(str(MEDIA_DIR))} "
            + (f"--media {shlex.quote(clip)} " if clip else "")
            + f"--start {start} --bps {video_bps} "
            f"--gop {enc['gop']} --qp-floor {int(enc.get('qp_floor', 34))} --audio-bitrate {enc['audio_bitrate']} --slack {slack} "
            f"--rc {rate_control} "
            f"--rc-rate {picture_channel_bps} "
            f"--look {float(enc.get('look_ahead', 0.5))} "
            f"--width {w} --height {h} "
            f"--video-device {shlex.quote(config['video_device'])} "
            f"--audio-device {shlex.quote(config['audio_device'])}"
        ]
        want_audio = True
    else:
        parts = [
            (reader if src["kind"] == "video" else "") +
            f"gst-launch-1.0 -e -q {video_in} ! "
            f"mpph265enc rc-mode=cbr bps={video_bps} bps-max={video_bps} "
            f"qp-max=51 qp-max-i=51 qp-init=34 qp-min=34 qp-min-i=34 max-reenc=2 gop={enc['gop']} "
            f"header-mode=each-idr ! h265parse config-interval=-1 ! queue max-size-time=3000000000 max-size-bytes=0 max-size-buffers=0 ! mux."
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
                f"avenc_aac bitrate={enc['audio_bitrate']} ! aacparse ! queue max-size-time=3000000000 max-size-bytes=0 max-size-buffers=0 ! mux.")
        elif want_audio and src["kind"] == "video" and not media_audio:
            want_audio = False          # the file has no sound to send
        elif want_audio:
            parts.append(
                f" td. ! {media_audio} ! "
                f"audioconvert ! audioresample ! "
                f"audio/x-raw,rate=48000,channels=2 ! "
                f"avenc_aac bitrate={enc['audio_bitrate']} ! aacparse ! queue max-size-time=3000000000 max-size-bytes=0 max-size-buffers=0 ! mux.")
        parts.append(" mpegtsmux name=mux ! fdsink fd=1")

    # -- remux to the exact rate, with our IDs and PIDs ------------
    # The maps make the output order fixed - video first, audio second
    # - whatever order mpegtsmux delivered them in, so the streamids
    # always land on the right stream.
    ident = config["identity"]
    streamids = f"-streamid 0:{ident['video_pid']}"
    if want_audio:
        streamids += f" -streamid 1:{ident['audio_pid']}"
    # Sound priority (tsprio.py, beside this file): ffmpeg stops building
    # the constant-rate stream and tsprio builds it instead, slot by slot -
    # tables, then sound that is due, then pictures, else nulls - writing
    # the PCR itself. When the encoder bursts, pictures wait, never sound.
    # On a burst test with today's method 93-252 of 375 sound blocks
    # arrived late; through tsprio, none. encoder.sound_priority: false
    # returns to ffmpeg's own constant-rate muxing.
    prio = APP_DIR / "tsprio.py"
    use_prio = bool(enc.get("sound_priority", True)) and prio.exists()
    # With priority, ffmpeg also passes each piece of sound on at once
    # (-pes_payload_size 0: every 21 ms) instead of gathering ~107 ms
    # blocks: sound that sets off 0.1 s behind arrives late whenever a
    # picture burst has used up the slack.
    muxrate = "-pes_payload_size 0 " if use_prio else f"-muxrate {rate} "
    parts.append(
        f" | ffmpeg -loglevel warning -analyzeduration 1000000 "
        f"-f mpegts -i - -map 0:v:0 -map '0:a:0?' -c copy -f mpegts {muxrate}"
        f"-metadata service_provider={shlex.quote(config['provider'])} "
        f"-metadata service_name={shlex.quote(config['callsign'])} "
        f"-mpegts_original_network_id {ident['original_network_id']} "
        f"-mpegts_transport_stream_id {ident['ts_id']} "
        f"-mpegts_service_id {ident['service_id']} "
        f"-mpegts_service_type {ident['service_type']} "
        f"-mpegts_pmt_start_pid {ident['pmt_pid']} "
        f"{streamids} -max_delay {int(slack * 1e6)} -pat_period 0.200 pipe:1")
    if use_prio:
        parts.append(
            f" | python3 {shlex.quote(str(prio))} --rate {rate} --slack {slack} "
            f"--video-pid {ident['video_pid']} --audio-pid {ident['audio_pid']}")

    # -- pace it ---------------------------------------------------
    # Deliberately above the modulator's figure. It wants its buffer
    # kept full; a small excess is discarded harmlessly, while
    # starvation shows up as missing video packets in a regular
    # pattern and a green screen at the far end.
    # -max_delay above is the slack between sending a frame and the
    # receiver needing it. The hardware encoder overshoots for a second or
    # more when a scene suddenly gets hard (measured: 3.85 Mb/s against a
    # 2.29 Mb/s cap); at 0.4 s of slack that broke the timing ("dts < pcr")
    # and receivers dropped sound. 2 s absorbs it, spreading the spike over
    # the next seconds, for about 1.6 s more delay. Set to 1.5 s once the
    # keyframes were capped (qp-min-i): 2 s plus giant keyframes was more
    # than some domestic receivers can hold.
    paced = int(rate * 1.006)
    # A small buffer: enough to smooth a GOP, not enough to add
    # noticeable delay. TSDuck's default held about 45 seconds.
    #
    # The NIT rides in place of stuffing packets, so the rate does not
    # move: about two a second, well inside DVB's 10 s maximum. The PAT
    # gains its pointer to PID 0x10 here too - ffmpeg's has none.
    nit_stage = ""
    nit = build_nit()
    if nit:
        every = max(10, int(rate / 1504 * 0.5))
        nit_stage = (f"-P pat --nit 0x10 "
                     f"-P inject {nit} --pid 0x10 --stuffing "
                     f"--inter-packet {every} ")
    # The buffer monitor (tsbuffer.py, beside this file) gets a copy of the
    # stream and reports the live slack for the Transmit card.
    monitor = APP_DIR / "tsbuffer.py"
    tap_cmd = f"python3 {monitor} {ident['video_pid']}"
    tap = f"-P fork {shlex.quote(tap_cmd)} " if monitor.exists() else ""
    parts.append(
        f" | tsp --realtime --buffer-size-mb 0.05 -I file - "
        f"{nit_stage}{tap}-P regulate --bitrate {paced} "
        f"-O ip {libre_host()}:8282")

    return "".join(parts)


def start_tx() -> dict:
    global tx_proc, tx_started_at, tx_last_command
    with tx_lock:
        stop_tx_locked()
        # Whatever the source is, make sure it is actually being fed.
        # Starting the encoder against a loopback nothing is writing to
        # gives a pipeline that opens and then sits there.
        src = config["source"]
        # Nothing uses the old loopback feeder any more: stills are
        # frozen in GStreamer and video is read by ffmpeg inside the
        # pipeline (see build_pipeline). Stop any left from before.
        stop_feeder()
        write_libre_settings()
        # The board's watcher restarts the modulator on the settings
        # change; give it a moment to be listening before the stream
        # arrives, or the first seconds go nowhere.
        time.sleep(2)
        cmd = build_pipeline()
        tx_last_command = cmd
        tx_proc = subprocess.Popen(
            cmd, shell=True, preexec_fn=os.setsid,
            stdout=subprocess.DEVNULL, stderr=open("/tmp/lynx-tx-pipeline.log", "ab"))
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
                    "tsp --realtime", "switcher.py", "tsprio.py", "tsbuffer.py"):
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
        "libre_patch": libre_patch,
        "buffer": read_buffer() if tx_running() else None,
        "switcher": (switcher_call("GET", "/status")
                     if tx_running() and config.get("use_switcher", True) else None),
        "use_switcher": config.get("use_switcher", True),
        "nit": nit_status,
    }


@app.post("/api/start")
def api_start():
    return start_tx()


@app.post("/api/stop")
def api_stop():
    return stop_tx()


class IdentUpdate(BaseModel):
    callsign: Optional[str] = None
    provider: Optional[str] = None
    network_name: Optional[str] = None
    network_id: Optional[int] = None
    original_network_id: Optional[int] = None
    ts_id: Optional[int] = None
    service_id: Optional[int] = None
    service_type: Optional[str] = None
    nit: Optional[bool] = None
    lcn: Optional[int] = None
    lcn_visible: Optional[bool] = None
    pmt_pid: Optional[int] = None
    video_pid: Optional[int] = None
    audio_pid: Optional[int] = None


def _check_identity(ident: dict):
    """Refuse values that would make an invalid or ambiguous stream."""
    for k in ("network_id", "original_network_id", "ts_id"):
        if not 0 <= int(ident[k]) <= 0xFFFF:
            raise HTTPException(400, f"{k} must be 0-65535")
    if not 1 <= int(ident["service_id"]) <= 0xFFFF:
        raise HTTPException(400, "service_id must be 1-65535")
    if ident["service_type"] not in _SERVICE_TYPE_CODE:
        raise HTTPException(400, "unknown service_type")
    pids = [int(ident[k]) for k in ("pmt_pid", "video_pid", "audio_pid")]
    if any(not 0x20 <= v <= 0x1FFE for v in pids):
        raise HTTPException(400, "PIDs must be 0x0020-0x1FFE")
    if len(set(pids)) != 3:
        raise HTTPException(400, "PMT, video and audio PIDs must differ")
    if ident.get("lcn") and not 1 <= int(ident["lcn"]) <= 1023:
        raise HTTPException(400, "LCN must be 1-1023")


@app.post("/api/ident")
def api_ident(req: IdentUpdate):
    """Everything a receiver reads about who and what this is.

    Names go into the SDT via ffmpeg; the network name, delivery
    parameters and LCN into the NIT; the IDs into all of them. The
    LibreSDR no longer rewrites any of it (see ensure_libre_patched).
    """
    d = req.model_dump()
    ident = dict(config["identity"])
    for k, v in d.items():
        if v is None or k in ("callsign", "provider"):
            continue
        ident[k] = v.strip() if isinstance(v, str) else v
    _check_identity(ident)
    old_video_pid = config["identity"]["video_pid"]
    config["identity"] = ident
    if d["callsign"] is not None:
        config["callsign"] = d["callsign"].strip() or "NOCALL"
    if d["provider"] is not None:
        config["provider"] = d["provider"].strip()
    save_config(config)
    if tx_running():
        start_tx()
    elif ident["video_pid"] != old_video_pid:
        # The board's PCR monitor follows the video PID.
        threading.Thread(target=ensure_libre_patched, daemon=True).start()
    return {"success": True, "identity": ident}


@app.post("/api/libre/check")
def api_libre_check():
    """Check the LibreSDR's script now; correct it if idle."""
    return ensure_libre_patched(apply=True)


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


SWITCHER_URL = "http://127.0.0.1:8090"


def switcher_call(method: str, path: str) -> Optional[dict]:
    """Talk to the running switcher; None if it is not running."""
    import urllib.request
    try:
        req = urllib.request.Request(SWITCHER_URL + path, method=method)
        with urllib.request.urlopen(req, timeout=0.5) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


class TakeRequest(BaseModel):
    src: str


@app.post("/api/switcher/take")
def api_switcher_take(req: TakeRequest):
    """The programme bus. On air: a clean cut. Off air: what starts."""
    if req.src not in ("cam", "med"):
        raise HTTPException(400, "src must be cam or med")
    config["source"]["program"] = req.src
    save_config(config)
    live = tx_running() and switcher_call("POST", f"/take?src={req.src}") is not None
    return {"success": True, "live": live}


class LoadRequest(BaseModel):
    file: str


@app.post("/api/switcher/load")
def api_switcher_load(req: LoadRequest):
    """The media player. On air it changes what the Media input carries."""
    n = _media_name(req.file)
    if not (MEDIA_DIR / n).is_file():
        raise HTTPException(404, "no such file")
    config["source"]["file"] = n
    save_config(config)
    from urllib.parse import quote
    live = tx_running() and switcher_call("POST", f"/load?file={quote(n)}") is not None
    return {"success": True, "live": live}


@app.post("/api/source")
def api_source(req: SourceUpdate):
    if req.kind not in ("camera", "video", "still", "switcher"):
        raise HTTPException(400, "kind must be camera, video, still or switcher")
    config["source"] = {"kind": req.kind, "file": req.file}
    save_config(config)
    # The feeder first, so the loopback has a format before the encoder
    # opens it - the other order is a race that looks like the media
    # source simply not working.
    stop_feeder()
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
          <div class="small mt-2" id="buffer-line" style="display:none">Buffer: &mdash;</div>
          <div id="buffer-graph" style="display:none;margin-top:4px">
            <div style="position:relative;height:14px;background:#0b0f1d;border:1px solid #0f3460;border-radius:4px;overflow:hidden">
              <div id="bb-fill" style="height:100%;width:0;background:#4ade80;transition:width .4s"></div>
              <div id="bb-min" title="lowest in the last minute"
                   style="position:absolute;top:0;bottom:0;width:2px;background:#fbbf24;left:0"></div>
            </div>
            <div id="bb-scale" style="position:relative;height:14px;font-size:.7em;color:#9fb3d1"></div>
            <canvas id="bb-hist" height="70" style="width:100%;display:block;margin-top:2px;background:#0b0f1d;border:1px solid #0f3460;border-radius:4px"></canvas>
          </div>
          <div class="small mt-2" id="libre-patch">LibreSDR script: checking&hellip;</div>
          <button class="btn btn-sm btn-outline-light mt-2" onclick="checkLibre()">Check LibreSDR</button>
          <a href="/monitor" class="btn btn-sm btn-outline-light mt-2 ms-1">&#x1F4C8; TX monitor</a>
        </div>
      </div>

      <div class="card">
        <div class="card-header">&#x1F3A5; Source</div>
        <div class="card-body">
          <div class="small text-muted mb-1">Programme</div>
          <div class="d-flex gap-2 mb-1">
            <button class="btn btn-lg btn-outline-light flex-fill" id="pgm-cam"
                    onclick="takeSrc('cam')">&#x1F4F9; Camera</button>
            <button class="btn btn-lg btn-outline-light flex-fill" id="pgm-med"
                    onclick="takeSrc('med')">&#x1F39E;&#xFE0F; Media</button>
          </div>
          <div class="small mb-1" id="pgm-info">&mdash;</div>
          <div class="text-muted small mb-2" id="hdmi-info">&mdash;</div>
          <div class="small text-muted mb-1">Media player &mdash; click a clip to load it</div>
          <div id="media-list"></div>
          <a href="/media" class="btn btn-sm btn-outline-light w-100 mt-2">&#x1F4C1; Manage media &mdash; upload, check, delete</a>
          <div class="text-muted small mt-2">
            Cuts are clean &mdash; the carrier stays up. Loading a clip while
            Media is on air changes the picture straight away, like a HyperDeck
            on an ATEM. Off air, these choose what starts at key-up.
            Files live in /srv/lynxdeck/media, shared with Lynx-Deck.
          </div>
        </div>
      </div>
    </div>

    <div class="col-md-4">
      <div class="card">
        <div class="card-header">&#x1F4FB; Modulation</div>
        <div class="card-body">
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

  <div class="row g-3 mt-1">
    <div class="col-12">
      <div class="card">
        <div class="card-header">&#x1F3F7;&#xFE0F; Identity &mdash; what receivers are told</div>
        <div class="card-body">
          <div class="small mb-3" style="color:#e8a33d">
            A TV remembers a channel by three numbers &mdash; <b>Original network ID</b>,
            <b>Transport stream ID</b> and <b>Service ID</b>. Change any of them and every
            viewer has to rescan. The names are only what it displays. Hierarchy:
            network &rarr; multiplex (carrier) &rarr; provider &rarr; service.
          </div>
          <div class="row g-3">
            <div class="col-md-4">
              <label class="small text-muted">Service name</label>
              <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-call" maxlength="32" onchange="saveIdent()">
              <div class="text-muted small">The channel viewers see, e.g. <i>GB3OO HD</i>.
                One per programme. Carried in the SDT.</div>
            </div>
            <div class="col-md-4">
              <label class="small text-muted">Provider name</label>
              <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-prov" maxlength="32" onchange="saveIdent()">
              <div class="text-muted small">Who makes the service, e.g. <i>GB3OO Folkestone</i>.
                In the SDT; many TVs only show it in channel info.</div>
            </div>
            <div class="col-md-4">
              <label class="small text-muted">Network name</label>
              <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-netname" maxlength="32" onchange="saveIdent()">
              <div class="text-muted small">The whole family of repeaters, e.g.
                <i>Kent DATV Repeaters</i> &mdash; the same on every site. In the NIT;
                what most TVs show as the network.</div>
            </div>

            <div class="col-md-3">
              <label class="small text-muted">Network ID</label>
              <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-netid" onchange="saveIdent()">
              <div class="text-muted small">Identifies the network in the NIT. Same on every
                site. 0xFF01&ndash;0xFFFF are for private use and never clash with a broadcaster.</div>
            </div>
            <div class="col-md-3">
              <label class="small text-muted">Original network ID <span style="color:#e8a33d">&#9679;</span></label>
              <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-onid" onchange="saveIdent()">
              <div class="text-muted small">Who originated the stream &mdash; normally the same as
                Network ID. Part of the triplet.</div>
            </div>
            <div class="col-md-3">
              <label class="small text-muted">Transport stream ID <span style="color:#e8a33d">&#9679;</span></label>
              <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-tsid" onchange="saveIdent()">
              <div class="text-muted small">One per carrier (multiplex), not per site: a second
                mux on the same site gets its own. Part of the triplet.</div>
            </div>
            <div class="col-md-3">
              <label class="small text-muted">Service ID <span style="color:#e8a33d">&#9679;</span></label>
              <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-sid" onchange="saveIdent()">
              <div class="text-muted small">One per programme, unique across the whole network.
                Part of the triplet. Also called Program ID.</div>
            </div>

            <div class="col-md-3">
              <label class="small text-muted">Service type</label>
              <select class="form-select form-select-sm bg-dark text-light border-secondary"
                      id="i-stype" onchange="saveIdent()">
                <option value="digital_tv">Digital TV (0x01)</option>
                <option value="advanced_codec_digital_hdtv">Advanced codec HD (0x19)</option>
                <option value="hevc_digital_hdtv">HEVC HD/UHD (0x1F)</option>
              </select>
              <div class="text-muted small">How the service is labelled. Most sets accept 0x01;
                a few only play HEVC labelled 0x1F.</div>
            </div>
            <div class="col-md-3">
              <label class="small text-muted">Logical channel number</label>
              <input type="number" min="0" max="1023"
                     class="form-control form-control-sm bg-dark text-light border-secondary"
                     id="i-lcn" onchange="saveIdent()">
              <div class="form-check form-switch mt-1">
                <input class="form-check-input" type="checkbox" id="i-lcnvis" onchange="saveIdent()">
                <label class="form-check-label small" for="i-lcnvis">Visible</label>
              </div>
              <div class="text-muted small">Position in the TV's channel list. In the NIT (EACEM
                descriptor). 0 sends none.</div>
            </div>
            <div class="col-md-3">
              <label class="small text-muted">PIDs &mdash; PMT / video / audio</label>
              <div class="d-flex gap-1">
                <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                       id="i-pmt" onchange="saveIdent()">
                <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                       id="i-vpid" onchange="saveIdent()">
                <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                       id="i-apid" onchange="saveIdent()">
              </div>
              <div class="text-muted small">Where each stream sits inside the multiplex. House
                standard 0x100 / 0x101 / 0x102. The PCR travels on the video PID.</div>
            </div>
            <div class="col-md-3">
              <div class="form-check form-switch mt-4">
                <input class="form-check-input" type="checkbox" id="i-nit" onchange="saveIdent()">
                <label class="form-check-label small" for="i-nit">Send NIT</label>
              </div>
              <div class="text-muted small">Network name, the carrier's own tuning parameters
                (from Modulation) and the LCN.</div>
              <div class="small mt-1" id="i-nit-status">&mdash;</div>
            </div>
          </div>
          <div class="small mt-2" style="color:#f87171" id="i-error"></div>
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
  const j = await r.json().catch(() => ({}));
  // A refusal must be seen: a key-up that silently does nothing is the
  // worst kind of fault.
  if (!r.ok) alert('Lynx-Tx: ' + (j.detail || ('error ' + r.status)));
  return j;
}

async function startTx() { await api('POST', '/api/start'); poll(); }
async function stopTx()  { await api('POST', '/api/stop');  poll(); }

async function setSource(kind, file) {
  await api('POST', '/api/source', {kind, file});
  poll();
}

async function takeSrc(src) {
  await api('POST', '/api/switcher/take', {src});
  setTimeout(poll, 200);
}

async function loadMedia(file) {
  await api('POST', '/api/switcher/load', {file});
  setTimeout(poll, 400);
}

function showMedia() {
  const el = document.getElementById('media-list');
  if (!cfg) return;
  const files = cfg.media || [];
  if (!files.length) {
    el.innerHTML = '<div class="text-muted small">Nothing in /srv/lynxdeck/media</div>';
    return;
  }
  const loaded = cfg.config.source.file;
  el.innerHTML = files.map(f =>
    `<button class="btn btn-sm ${f.name === loaded ? 'btn-light' : 'btn-outline-light'} w-100 mb-1 text-start"
             onclick="loadMedia('${f.name}')">
       ${f.kind === 'still' ? '&#x1F5BC;&#xFE0F;' : '&#x1F3AC;'} ${f.name}
       ${f.name === loaded ? '<span class="float-end small">loaded</span>' : ''}
     </button>`).join('');
}

function hex4(v) { return '0x' + Number(v).toString(16).toUpperCase().padStart(4, '0'); }
function num(id) {
  // Accepts 0x-prefixed hex or decimal.
  const v = document.getElementById(id).value.trim();
  return /^0x/i.test(v) ? parseInt(v, 16) : parseInt(v, 10);
}

async function saveIdent() {
  const r = await fetch('/api/ident', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      callsign: document.getElementById('i-call').value.trim(),
      provider: document.getElementById('i-prov').value.trim(),
      network_name: document.getElementById('i-netname').value.trim(),
      network_id: num('i-netid'),
      original_network_id: num('i-onid'),
      ts_id: num('i-tsid'),
      service_id: num('i-sid'),
      service_type: document.getElementById('i-stype').value,
      lcn: num('i-lcn') || 0,
      lcn_visible: document.getElementById('i-lcnvis').checked,
      nit: document.getElementById('i-nit').checked,
      pmt_pid: num('i-pmt'),
      video_pid: num('i-vpid'),
      audio_pid: num('i-apid'),
    })});
  const j = await r.json();
  document.getElementById('i-error').textContent = r.ok ? '' : (j.detail || 'Not saved');
  if (r.ok) fillIdent(j.identity, null);
  poll();
}

function fillIdent(i, c) {
  if (c) {
    document.getElementById('i-call').value = c.callsign;
    document.getElementById('i-prov').value = c.provider;
  }
  document.getElementById('i-netname').value = i.network_name;
  document.getElementById('i-netid').value = hex4(i.network_id);
  document.getElementById('i-onid').value = hex4(i.original_network_id);
  document.getElementById('i-tsid').value = hex4(i.ts_id);
  document.getElementById('i-sid').value = hex4(i.service_id);
  document.getElementById('i-stype').value = i.service_type;
  document.getElementById('i-lcn').value = i.lcn;
  document.getElementById('i-lcnvis').checked = i.lcn_visible;
  document.getElementById('i-nit').checked = i.nit;
  document.getElementById('i-pmt').value = hex4(i.pmt_pid);
  document.getElementById('i-vpid').value = hex4(i.video_pid);
  document.getElementById('i-apid').value = hex4(i.audio_pid);
}

async function checkLibre() {
  document.getElementById('libre-patch').textContent = 'LibreSDR script: checking\u2026';
  await api('POST', '/api/libre/check');
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

  // Programme bus: red = on air, blue = what starts at key-up.
  const SW = cfg.switcher, onAir = !!(cfg.transmitting && SW);
  const pgm = onAir ? SW.on : (cfg.config.source.program ||
              (cfg.config.source.kind === 'camera' ? 'cam' : 'med'));
  for (const b of ['cam', 'med'])
    document.getElementById('pgm-' + b).className = 'btn btn-lg flex-fill ' +
      (b === pgm ? (onAir ? 'btn-danger' : 'btn-primary') : 'btn-outline-light');
  const pi = document.getElementById('pgm-info');
  if (onAir) {
    let t = 'ON AIR: ' + (SW.on === 'cam' ? 'Camera' : 'Media');
    if (SW.camera === 'bars') t += ' \u00b7 no HDMI signal \u2014 Camera shows colour bars';
    if ((SW.media_state || '').startsWith('error')) t += ' \u00b7 media ' + SW.media_state;
    pi.textContent = t; pi.style.color = '#f87171';
  } else {
    pi.textContent = cfg.transmitting ? 'Single-source mode' : 'Off air \u2014 starts on ' +
      (pgm === 'cam' ? 'Camera' : 'Media');
    pi.style.color = '#9fb3d1';
  }
  showMedia();

  // Which of the two appears. The format controls only mean
  // anything for media, where the file is genuinely scaled on the
  // way in; with the camera the HDMI input decides and the page
  // should say so rather than pretend otherwise.
  // In switcher mode the programme follows the HDMI input, as the camera does.
  const isCam = cfg.use_switcher || cfg.config.source.kind === 'camera';
  document.getElementById('camera-format').style.display = isCam ? '' : 'none';
  document.getElementById('media-format').style.display = isCam ? 'none' : '';
  if (isCam) {
    const hf = cfg.hdmi || {};
    document.getElementById('camera-format-value').textContent =
      hf.width ? hf.width + '\u00d7' + hf.height : 'nothing detected';
  }

  const LP = cfg.libre_patch || {};
  // Buffer: how far ahead of need the pictures are sent. About 2 s at
  // rest; it drains when the encoder overshoots; at zero receivers glitch.
  const bl = document.getElementById('buffer-line'), B = cfg.buffer;
  drawBuffer(B, Number((cfg.config.encoder || {}).slack) || 0.6);
  bl.style.display = B ? 'block' : 'none';
  if (B) {
    const lo = B.min_60s ?? B.slack;
    const tr = cfg.switcher && cfg.switcher.trim ? ' \u00b7 trimming \u2212' + cfg.switcher.trim + '%' : '';
    bl.textContent = 'Buffer: ' + B.slack.toFixed(2) + ' s' + tr + '  (lowest in the last minute ' +
      lo.toFixed(2) + ' s' + (B.late ? ', ' + B.late + ' late frames since key-up' : '') + ')';
    bl.style.color = lo <= 0 ? '#f87171' : lo < 0.5 ? '#fbbf24' : '#4ade80';
  }
  const lp = document.getElementById('libre-patch');
  const lpColour = {ok: '#4ade80', rebooting: '#e8a33d', pending: '#e8a33d',
                    unknown: '#9fb3d1'}[LP.state] || '#f87171';
  lp.textContent = 'LibreSDR script: ' + (LP.detail || '\u2014');
  lp.style.color = lpColour;
  const N = cfg.nit || {};
  const ns = document.getElementById('i-nit-status');
  ns.textContent = 'Last build: ' + (N.detail || '\u2014');
  ns.style.color = N.ok ? '#4ade80' : '#9fb3d1';

  document.getElementById('sr-warning').hidden =
    cfg.config.modulation.symbol_rate_ks <= 2000;

  if (firstLoad) {
    const m = cfg.config.modulation, e = cfg.config.encoder;
    fillIdent(cfg.config.identity, cfg.config);
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
    firstLoad = false;
  }
}

poll();
// Buffer bar and two-minute history. Drawn here in the browser from the
// once-a-second reading the text line uses: no cost on the ROCK.
const bufHist = [];
function drawBuffer(B, target) {
  const g = document.getElementById('buffer-graph');
  g.style.display = B ? 'block' : 'none';
  if (!B) { bufHist.length = 0; return; }
  const lo = B.min_60s ?? B.slack, top = target * 1.15;
  const frac = v => Math.max(0, Math.min(1, v / top));
  const col = v => v <= 0 ? '#f87171' : v < target * 0.5 ? '#fbbf24' : '#4ade80';
  const step = top <= 0.5 ? 0.1 : top <= 1.2 ? 0.2 : 0.5;              // a round scale
  const fill = document.getElementById('bb-fill');
  fill.style.width = (frac(B.slack) * 100) + '%';
  fill.style.background = col(B.slack);
  document.getElementById('bb-min').style.left = (frac(lo) * 100) + '%';
  // The bar's scale: seconds of time in hand, the slack setting marked.
  const sc = document.getElementById('bb-scale');
  if (sc.dataset.top !== String(top)) {
    sc.dataset.top = String(top);
    let h = '';
    for (let v = 0; v <= top + 1e-9; v += step) {
      const pc = frac(v) * 100, align = pc < 3 ? '0' : pc > 97 ? '-100%' : '-50%';
      h += `<span style="position:absolute;left:${pc}%;transform:translateX(${align})">${v.toFixed(1)}${v === 0 ? ' s' : ''}</span>`;
    }
    const tp = frac(target) * 100;
    h += `<span style="position:absolute;left:${tp}%;top:-17px;height:15px;border-left:2px dashed #e0e0e0" title="the slack setting"></span>`;
    sc.innerHTML = h;
  }
  // The history: one bar per reading, the last two minutes, with its own axes.
  bufHist.push(B.slack); if (bufHist.length > 60) bufHist.shift();
  const dpr = devicePixelRatio, c = document.getElementById('bb-hist'),
        W = c.width = c.clientWidth * dpr, H = c.height = 70 * dpr, x = c.getContext('2d');
  const L = 34 * dpr, Bm = 14 * dpr, T = 4 * dpr;                       // margins for the labels
  const bottom = -target * 0.5;                                          // room below zero for late frames
  const Y = v => T + (top - Math.max(bottom, Math.min(top, v))) / (top - bottom) * (H - T - Bm);
  x.clearRect(0, 0, W, H);
  x.font = (10 * dpr) + 'px system-ui'; x.fillStyle = '#9fb3d1'; x.textBaseline = 'middle';
  x.textAlign = 'right';
  for (let v = 0; v <= top + 1e-9; v += step) {                          // seconds axis
    x.fillText(v.toFixed(1), L - 4 * dpr, Y(v));
    x.strokeStyle = v === 0 ? '#b91c1c' : '#1f2b44'; x.lineWidth = dpr;
    x.beginPath(); x.moveTo(L, Y(v)); x.lineTo(W, Y(v)); x.stroke();
  }
  x.fillText('late', L - 4 * dpr, Y(bottom * 0.6));
  x.setLineDash([4 * dpr, 4 * dpr]); x.strokeStyle = '#e0e0e0';          // the slack setting
  x.beginPath(); x.moveTo(L, Y(target)); x.lineTo(W, Y(target)); x.stroke(); x.setLineDash([]);
  x.textBaseline = 'bottom';                                             // time axis
  x.textAlign = 'left'; x.fillText('2 min ago', L, H);
  x.textAlign = 'center'; x.fillText('1 min', L + (W - L) / 2, H);
  x.textAlign = 'right'; x.fillText('now', W - 2 * dpr, H);
  const w = (W - L) / 60;
  bufHist.forEach((v, i) => {
    const X = W - (bufHist.length - i) * w, y0 = Y(0), y1 = Y(v);
    x.fillStyle = col(v); x.fillRect(X + w * 0.15, Math.min(y0, y1), w * 0.7, Math.max(1, Math.abs(y1 - y0)));
  });
}

setInterval(poll, 2000);
</script>
</body></html>
"""


MEDIA_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lynx-Tx - Media</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background: #1a1a2e; color: #e0e0e0; }
  .card { background: #16213e; border: 1px solid #0f3460; }
  .card-header { background: #0f3460; color: #fff; font-weight: 600; }
  .table { --bs-table-bg: transparent; --bs-table-color: #e0e0e0; }
  .text-muted { color: #9fb3d1 !important; }
  .mono { font-family: monospace; }
  .ok { color: #4ade80; } .warn { color: #e8a33d; }
  #drop { border: 2px dashed #3a4a63; border-radius: 8px; padding: 1.5em;
          text-align: center; color: #9fb3d1; cursor: pointer; }
  #drop.over { border-color: #4ade80; color: #e0e0e0; }
  .progress { background: #0f3460; height: 1.2em; }
</style>
</head><body>
<div class="container-fluid py-3">

  <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
    <h4 class="mb-0 me-3">&#x1F4C1; Lynx-Tx &mdash; Media</h4>
    <a href="/" class="btn btn-sm btn-outline-light">&larr; Back to the transmitter</a>
    <span class="ms-auto text-muted small" id="disk">&mdash;</span>
  </div>

  <div class="card mb-3">
    <div class="card-header">&#x2B06;&#xFE0F; Upload</div>
    <div class="card-body">
      <div id="drop" onclick="document.getElementById('pick').click()">
        Drop videos or stills here, or click to choose.<br>
        <span class="small">Large masters are fine &mdash; they stream straight to disk.</span>
      </div>
      <input type="file" id="pick" multiple hidden
             accept=".mp4,.mov,.mkv,.ts,.m4v,.png,.jpg,.jpeg,.bmp">
      <div id="uploads" class="mt-2"></div>
      <div class="text-muted small mt-2">
        House standard for anything going on air: <b>3840&times;2160 at 25 fps,
        standard range (Rec.709)</b>. Files that differ are marked below &mdash;
        they will still play, but a different frame rate judders and HDR
        looks flat on most receivers.
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">&#x1F39E;&#xFE0F; Library</div>
    <div class="card-body">
      <div class="table-responsive">
        <table class="table table-sm align-middle mb-0">
          <thead><tr class="text-muted small">
            <th>File</th><th>Picture</th><th>Rate</th><th>Codec</th>
            <th>Range</th><th>Audio</th><th>Length</th><th>Size</th><th></th>
          </tr></thead>
          <tbody id="lib"><tr><td colspan="9" class="text-muted">Loading&hellip;</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<script>
function gb(b) { return (b / 1e9).toFixed(b >= 1e10 ? 0 : 1) + ' GB'; }
function hms(t) {
  if (!t) return '&mdash;';
  t = Math.round(t);
  const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}
function esc(x) { const d = document.createElement('div'); d.textContent = x; return d.innerHTML; }

async function load() {
  const r = await fetch('/api/media'); const j = await r.json();
  document.getElementById('disk').textContent =
    gb(j.free) + ' free of ' + gb(j.total);
  const rows = j.files.map(f => {
    const i = f.info || {};
    const onAir = f.name === j.on_air;
    if (!i.ok) {
      return `<tr><td class="mono">${esc(f.name)}</td>
        <td colspan="6" class="warn">Could not read this file</td>
        <td>${gb(f.size)}</td><td>${delBtn(f.name, onAir)}</td></tr>`;
    }
    const is4k = i.width === 3840 && i.height === 2160;
    const still = f.kind === 'still';
    const rateOk = still || Math.abs(i.fps - 25) < 0.01;
    return `<tr>
      <td class="mono">${still ? '&#x1F5BC;&#xFE0F;' : '&#x1F3AC;'} ${esc(f.name)}
        ${onAir ? '<span class="badge bg-danger ms-1">ON AIR</span>' : ''}</td>
      <td class="${is4k ? 'ok' : 'warn'}">${i.width}&times;${i.height}</td>
      <td class="${rateOk ? 'ok' : 'warn'}">${still ? 'still' : i.fps + ' fps'}</td>
      <td>${esc(i.codec)}${still ? '' : ' ' + i.bits + '-bit'}</td>
      <td class="${i.hdr ? 'warn' : 'ok'}">${i.hdr ? 'HDR ' + i.hdr : 'SDR'}</td>
      <td>${esc(i.audio || (still ? '&mdash;' : 'none'))}</td>
      <td>${still ? '&mdash;' : hms(i.duration)}</td>
      <td>${gb(f.size)}</td>
      <td>${delBtn(f.name, onAir)}</td></tr>`;
  });
  document.getElementById('lib').innerHTML = rows.join('') ||
    '<tr><td colspan="9" class="text-muted">No media yet.</td></tr>';
}

function delBtn(name, onAir) {
  return onAir ? '' :
    `<button class="btn btn-sm btn-outline-danger"
       onclick="del('${encodeURIComponent(name)}')">Delete</button>`;
}

async function del(enc) {
  const name = decodeURIComponent(enc);
  if (!confirm('Delete ' + name + '? This cannot be undone.')) return;
  const r = await fetch('/api/media/' + enc, {method: 'DELETE'});
  if (!r.ok) alert((await r.json()).detail || 'Not deleted');
  load();
}

function upload(file) {
  return new Promise(resolve => {
    const box = document.createElement('div');
    box.className = 'mb-2';
    box.innerHTML = `<div class="small mono">${esc(file.name)} &mdash;
        <span class="pct">0%</span></div>
      <div class="progress"><div class="progress-bar bg-success" style="width:0%"></div></div>`;
    document.getElementById('uploads').appendChild(box);
    const bar = box.querySelector('.progress-bar'), pct = box.querySelector('.pct');
    const x = new XMLHttpRequest();
    x.open('PUT', '/api/media/upload?name=' + encodeURIComponent(file.name));
    x.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const p = Math.floor(e.loaded / e.total * 100);
      bar.style.width = p + '%'; pct.textContent = p + '%';
    };
    x.onload = () => {
      if (x.status === 200) { pct.textContent = 'done'; }
      else {
        let msg = 'failed';
        try { msg = JSON.parse(x.responseText).detail || msg; } catch (e) {}
        pct.textContent = msg; bar.classList.replace('bg-success', 'bg-danger');
      }
      load(); resolve();
    };
    x.onerror = () => { pct.textContent = 'connection lost';
      bar.classList.replace('bg-success', 'bg-danger'); resolve(); };
    x.send(file);
  });
}

async function uploadAll(files) {
  for (const f of files) await upload(f);   // one at a time, so the disk is not fighting itself
}

const drop = document.getElementById('drop');
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over');
  uploadAll([...e.dataTransfer.files]); });
document.getElementById('pick').addEventListener('change', e => uploadAll([...e.target.files]));
load();
</script>
</body></html>
"""

@app.get("/api/media")
def api_media():
    """Every playable file, with what it actually contains."""
    files = []
    if MEDIA_DIR.exists():
        for p in sorted(MEDIA_DIR.iterdir()):
            ext = p.suffix.lower()
            if not p.is_file() or ext not in VIDEO_EXT | STILL_EXT:
                continue
            files.append({"name": p.name, "size": p.stat().st_size,
                          "kind": "still" if ext in STILL_EXT else "video",
                          "info": probe_media(p)})
    du = shutil.disk_usage(MEDIA_DIR if MEDIA_DIR.exists() else "/")
    return {"files": files, "free": du.free, "total": du.total,
            "on_air": _on_air_file()}


@app.put("/api/media/upload")
async def api_media_upload(request: Request, name: str):
    """Stream a file straight to disk.

    The body is the file itself, not a form, so a 6 GB master needs no
    extra library and never sits in memory. Written beside the real name
    and renamed at the end, so a half-finished upload can never be put
    on air.
    """
    n = _media_name(name)
    if n == _on_air_file():
        raise HTTPException(409, "that file is on air - stop or change source first")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    length = int(request.headers.get("content-length") or 0)
    free = shutil.disk_usage(MEDIA_DIR).free
    if length and length > free - 1_000_000_000:
        raise HTTPException(507, "not enough disk space (keeping 1 GB spare)")
    part = MEDIA_DIR / f".{n}.part"
    written = 0
    try:
        with open(part, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                written += len(chunk)
        if length and written != length:
            raise HTTPException(400, "upload incomplete")
        os.replace(part, MEDIA_DIR / n)
    finally:
        if part.exists():
            part.unlink()
    print(f"[media] uploaded {n} ({written} bytes)")
    return {"success": True, "name": n, "size": written}


@app.delete("/api/media/{name}")
def api_media_delete(name: str):
    n = _media_name(name)
    if n == _on_air_file():
        raise HTTPException(409, "that file is on air - stop or change source first")
    p = MEDIA_DIR / n
    if not p.exists():
        raise HTTPException(404, "no such file")
    p.unlink()
    print(f"[media] deleted {n}")
    return {"success": True}


@app.get("/media", response_class=HTMLResponse)
def media_page():
    return MEDIA_PAGE


# The TX monitor: a live spectrum from the LibreSDR's own receiver
# (tx_monitor.py, beside this file). Optional - Lynx-Tx runs without it.
try:
    import tx_monitor
    def _save_monitor(on: bool) -> None:
        config.setdefault("monitor", {})["enabled"] = on
        save_config(config)
    tx_monitor.install(app, lambda: {**config, "libre": {**config["libre"], "host": libre_host()}},
                       _save_monitor)
except Exception as e:
    print(f"[monitor] not available: {e}")


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


if __name__ == "__main__":
    print("=== Lynx-Tx ===")
    print(f"    config: {CONFIG_PATH}")
    print(f"    media:  {MEDIA_DIR}")
    print(f"    LibreSDR: {libre_host()}")
    threading.Thread(target=libre_patch_watch, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
