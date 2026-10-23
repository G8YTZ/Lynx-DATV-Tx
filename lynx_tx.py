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
import ipaddress
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

import atsc3mod
import dvbs2mod
import sdrs
import tezuka_prep

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
    "libre": {"host": "192.168.254.2", "user": "root"},
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
        "audio": True,
        "audio_bitrate": 192000,
        # The tweaks (Source and Encoder tweak cards): the values proven on
        # air on 6 Oct 2026 - QPSK 3/4 2000 kS/s, 16 minutes of Save Tonight,
        # nothing late. TESTED_TWEAKS below is the same set, for the page's
        # "Reset to tested defaults".
        "content": "balanced",
        "rate_control": "hybrid",
        "slack": 0.9,
        "qp_floor": 30,
        "gop": 50,
        "rc_target": 0.3,
        "look_ahead": 0.5,
        "hybrid_aim": 0.97,
        "picture_share": 0.92,
        "sound_priority": True,
    },
    "source": {"kind": "camera", "file": ""},
    # Transmission system: "dvbs2" or "atsc3". Everything up to the remux
    # is shared; see atsc3mod.py and dvbs2mod.py.
    "mode": "dvbs2",
    # Who makes DVB-S2: "rock" (Ron W6RZ's gr-dtv on the ROCK, any SDR as
    # an IQ transmitter - dvbs2mod.py) or "board" (DL1JM's modulator on the
    # LibreSDR, the original path, kept as a fallback).
    "dvbs2_engine": "rock",
    # The SDR transmitting the IQ. uri "" = automatic: the first board found
    # on the direct link, else the LibreSDR under "libre". hosts = other
    # addresses to look for one at, e.g. a PlutoSky on the LAN; the direct
    # link and mDNS are always searched. See sdrs.py.
    "sdr": {"uri": "", "hosts": []},
    "atsc3": dict(atsc3mod.DEFAULTS),
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


# ── Tweaks: the trade-offs, adjustable from the page ──────────────────
# Each: (lowest, highest) - anything outside is clamped, never refused, so
# a typo cannot stop a key-up. The defaults are the tested values above.
TWEAK_LIMITS = {
    "slack": (0.3, 1.0),
    "qp_floor": (20, 40),
    "gop": (25, 250),
    "rc_target": (0.1, 0.6),
    "look_ahead": (0.0, 1.0),
    "hybrid_aim": (0.85, 1.0),
    "picture_share": (0.5, 0.97),
}
TWEAK_CHOICES = {
    "rate_control": ("hybrid", "cbr"),
    "content": ("talking", "balanced", "action", "custom"),
}
TESTED_TWEAKS = {k: DEFAULT_CONFIG["encoder"][k] for k in
                 ("content", "rate_control", "slack", "qp_floor", "gop", "rc_target",
                  "look_ahead", "hybrid_aim", "picture_share", "sound_priority")}
# The Content presets fill these three; everything else is left alone.
CONTENT_PRESETS = {
    "talking":  {"qp_floor": 26, "gop": 100, "rc_target": 0.4},
    "balanced": {"qp_floor": 30, "gop": 50,  "rc_target": 0.3},
    "action":   {"qp_floor": 32, "gop": 50,  "rc_target": 0.2},
}


def clean_tweaks(d: dict) -> dict:
    """Clamp and type the tweak values; drop anything unknown."""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        if k in TWEAK_LIMITS:
            lo, hi = TWEAK_LIMITS[k]
            v = max(lo, min(hi, float(v)))
            out[k] = int(round(v)) if k in ("qp_floor", "gop") else round(v, 3)
        elif k in TWEAK_CHOICES:
            if v in TWEAK_CHOICES[k]:
                out[k] = v
        elif k == "sound_priority":
            out[k] = bool(v)
    return out


_hybrid_capable = None


def hybrid_capable() -> bool:
    """Is the patched encoder plugin installed? Hybrid steers the QP floor
    live; the stock plugin ignores that (it never sets MPP's frame-level
    limits), so hybrid on a stock plugin would be plain CBR aimed too high.
    The patch adds the string rc:fqp_min_p - look for it."""
    global _hybrid_capable
    if _hybrid_capable is None:
        _hybrid_capable = False
        for d in ("/usr/lib/aarch64-linux-gnu/gstreamer-1.0", "/lib/aarch64-linux-gnu/gstreamer-1.0"):
            try:
                if b"rc:fqp_min_p" in Path(d, "libgstrockchipmpp.so").read_bytes():
                    _hybrid_capable = True
                    break
            except OSError:
                pass
    return _hybrid_capable


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


# One SSH connection, kept open and reused. Every new connection costs the
# LibreSDR's 667 MHz ARM a key exchange - the same CPU that runs the DVB-S2
# modulator. Polled every 2 s from each open web page, those handshakes
# stole enough time from pluto_dvb to make the picture flicker.
SSH_KEEP = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/lynx-ssh-%r@%h:%p",
            "-o", "ControlPersist=120"]


def libre_ssh(cmd: str, timeout: int = 8, host: Optional[str] = None) -> str:
    """Run a command on the LibreSDR over SSH.

    Key-based, non-interactive: a transmitter that stops to ask for a
    password is no use. If this fails, check the board's /root is not
    world-writable - OpenSSH silently refuses key auth when it is, and
    says nothing useful about why.
    """
    lib = config["libre"]
    try:
        r = subprocess.run(
            ["ssh", *SSH_KEEP, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{lib['user']}@{host or libre_host()}", cmd],
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


_libre_cache = {"t": 0.0, "v": None, "off": 0, "under": 0}
_libre_lock = threading.Lock()


def libre_status() -> dict:
    """What the board says about itself, for the status strip.

    At most once every 10 s, however many pages poll, and cheap on the
    board: its log grows all the time it runs, and grepping the whole of
    it every 2 s (twice) grew more costly by the hour - work taken from the
    modulator. Now only the bytes added since last time are read."""
    with _libre_lock:
        c = _libre_cache
        if c["v"] is not None and time.time() - c["t"] < 10:
            return c["v"]
        out = libre_ssh(
            "f=/tmp/udpts.log; s=$(wc -c < $f 2>/dev/null || echo 0); echo $s; "
            f"if [ $s -ge {c['off']} ]; then tail -c +{c['off'] + 1} $f | grep -ac Underflow; "
            "else echo RESET; fi; "
            "tail -c 4000 $f 2>/dev/null | grep -a TsBitrate | tail -1; "
            "pgrep -c pluto_dvb 2>/dev/null")
        parts = (out or "").strip().split("\n")

        def _int(i):
            try:
                return int(parts[i].strip().split()[-1])
            except (IndexError, ValueError):
                return None

        size, new_under = _int(0), None
        if len(parts) > 1 and parts[1].strip() == "RESET":
            c["off"], c["under"] = 0, 0          # the board restarted: log is new
        else:
            new_under = _int(1)
        if size is not None and new_under is not None:
            c["under"] += new_under
            c["off"] = size
        v = {
            "reachable": bool(out),
            "ts_bitrate": _int(2),
            "underflows": c["under"] if out else None,
            "modulator_running": bool(_int(3)),
        }
        c["t"], c["v"] = time.time(), v
        return v


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
            ["ssh", *SSH_KEEP, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
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
            # Only DL1JM's path uses the board's udpts.sh. With the SDR as an
            # IQ transmitter there is nothing to patch, and a corrective
            # reboot would only take the board away.
            if not iq_mode():
                ensure_libre_patched(apply=True)
        except Exception as e:
            _set_libre("error", str(e))
        time.sleep(600)


# ── ATSC 3.0 mode: the LibreSDR as a plain IQ transmitter ─────
#
# In ATSC 3.0 the modulation is done on the ROCK (atsc3mod.py), so DL1JM's
# modulator must let go of the transmitter: its scripts and pluto_dvb are
# stopped for the duration. Nothing on the board is changed, so a reboot
# brings DVB-S2 back exactly as it was - done automatically on switching
# back to DVB-S2.

def is_atsc3() -> bool:
    return config.get("mode") == "atsc3"


def dvbs2_rock() -> bool:
    """DVB-S2 made on the ROCK (dvbs2mod.py) rather than by DL1JM's
    modulator on the LibreSDR."""
    # DL1JM's modulator exists only on the LibreSDR: any other SDR is
    # always driven from the ROCK, whatever the saved choice says.
    return not is_atsc3() and (config.get("dvbs2_engine", "rock") != "board"
                               or not sdr_is_libre())


def iq_mode() -> bool:
    """The SDR is a plain IQ transmitter: ATSC 3.0, or DVB-S2 from the ROCK."""
    return is_atsc3() or dvbs2_rock()


def libre_uri() -> str:
    return f"ip:{libre_host()}"


def sdr_uri() -> str:
    """The SDR chosen on the Transmit card; with none chosen, the first board
    the last scan found on the direct link (so a new SDR just works),
    else the LibreSDR."""
    chosen = (config.get("sdr") or {}).get("uri")
    if chosen:
        return chosen
    direct = set(sdrs.direct_hosts(config["libre"].get("direct_if", "enP4p65s0")))
    for d in sdrs.found():
        if d["host"] in direct:
            return d["uri"]
    return libre_uri()


def sdr_identity() -> Optional[dict]:
    """What the chosen SDR says it is: from the last scan, else asked now."""
    uri = sdr_uri()
    for d in sdrs.found():
        if d["uri"] == uri:
            return d
    return sdrs.probe(uri)


def sdr_is_libre() -> bool:
    """Could the chosen SDR be running DL1JM's firmware - the only board the
    "on board" modulator exists on, and the only one it must be stopped on?

    Decided by what the board says it is, not by its address: the direct
    link is for any SDR, and the LibreSDR can be reached by several names.
    A board that answers as a PlutoSky, or runs Tezuka, is not. One that
    cannot be asked is judged by address, as before."""
    d = sdr_identity()
    if d:
        return d["kind"] == "libresdr" and not d["tezuka"]
    u = (config.get("sdr") or {}).get("uri") or ""
    return not u or u == libre_uri() or u.split(":", 1)[-1] in (
        config["libre"].get("host"), config["libre"].get("direct_host", "192.168.254.2"))


def sdr_host() -> str:
    u = sdr_uri()
    return u.split(":", 1)[1] if u.startswith("ip:") else libre_host()


def sdr_hosts() -> list:
    """Where to look for SDRs: the direct link first, then the LAN."""
    lib = config["libre"]
    hosts = [lib.get("direct_host", "192.168.254.2")]
    hosts += sdrs.direct_hosts(lib.get("direct_if", "enP4p65s0"))
    hosts.append(lib.get("host"))
    hosts += list((config.get("sdr") or {}).get("hosts") or [])
    u = (config.get("sdr") or {}).get("uri") or ""
    if u:
        hosts.insert(0, u)
    out = []
    for h in hosts:
        if h and h not in out:
            out.append(h)
    return out


# ── Alarms: what the page and SNMP report ────────────────────
# muted: on air, but the SDR has turned its transmitter off by itself (the
# PlutoSky's starvation mute, tools/plutosky-autorun.sh: its IQ stopped).
# fault: the transmit chain ended without TX Off. Both clear at the next TX On.
tx_alarm = {"muted": False, "fault": "", "sdr_ok": True, "checked": 0.0,
            "wanted": False, "locked_out": False, "recoveries": 0,
            "recoveries_recent": [], "settled": 0.0, "recovering": ""}

# Auto-recover (config "auto_recover"): like a power network's auto-reclose.
# If you wanted to be on air and the SDR muted itself or the chain stopped,
# once the SDR has answered for settle_s seconds the transmitter is
# restarted - at most max_tries times in window_min minutes, then it locks
# out until someone presses TX On.
AUTO_RECOVER = {"enabled": True, "settle_s": 30, "max_tries": 3, "window_min": 60}


def auto_recover_cfg() -> dict:
    return {**AUTO_RECOVER, **(config.get("auto_recover") or {})}


def auto_recover_check():
    """Called every 5 s by tx_alarm_watch."""
    ar = auto_recover_cfg()
    down = tx_alarm["muted"] or (not tx_running() and bool(tx_alarm["fault"]))
    if not (ar["enabled"] and tx_alarm["wanted"] and down) or tx_alarm["locked_out"]:
        tx_alarm["settled"] = 0.0
        tx_alarm["recovering"] = ""
        return
    if not sdrs.probe(sdr_uri()):
        tx_alarm["settled"] = 0.0
        tx_alarm["recovering"] = "waiting for the SDR"
        return
    now = time.time()
    if not tx_alarm["settled"]:
        tx_alarm["settled"] = now
    left = ar["settle_s"] - (now - tx_alarm["settled"])
    if left > 0:
        tx_alarm["recovering"] = f"SDR back: restarting in {int(left) + 1} s"
        return
    recent = [t for t in tx_alarm["recoveries_recent"] if now - t < ar["window_min"] * 60]
    if len(recent) >= ar["max_tries"]:
        tx_alarm["recoveries_recent"] = recent
        tx_alarm["locked_out"] = True
        tx_alarm["recovering"] = ""
        print(f"[recover] {len(recent)} restarts in {ar['window_min']} min - lockout "
              "until TX On is pressed")
        return
    recent.append(now)
    tx_alarm["recoveries_recent"] = recent
    tx_alarm["recoveries"] += 1
    tx_alarm["settled"] = 0.0
    tx_alarm["recovering"] = ""
    why = "the SDR had muted itself" if tx_alarm["muted"] else tx_alarm["fault"]
    print(f"[recover] restarting the transmitter ({why}); "
          f"{len(recent)} of {ar['max_tries']} in {ar['window_min']} min")
    try:
        start_tx()
    except Exception as e:
        tx_alarm["fault"] = f"auto-recover could not restart: {e}"
        print(f"[recover] failed: {e}")


def tx_alarm_watch():
    """Every 5 s on air: is the SDR's transmit LO still on? Read-only, and in
    a thread of its own so the page never waits on the SDR."""
    while True:
        time.sleep(5)
        try:
            auto_recover_check()
        except Exception as e:
            print(f"[recover] {e}")
        try:
            if not (tx_running() and iq_mode()) or time.time() - tx_started_at < 10:
                continue
            r = subprocess.run(["iio_attr", "-u", sdr_uri(), "-o", "-c", "ad9361-phy",
                                "altvoltage1", "powerdown"],
                               capture_output=True, text=True, timeout=4)
            tx_alarm["sdr_ok"] = r.returncode == 0
            muted = r.returncode == 0 and r.stdout.strip() == "1"
            if muted and not tx_alarm["muted"]:
                print("[alarm] the SDR has muted itself (its IQ stopped)")
                tx_alarm["mutes"] = tx_alarm.get("mutes", 0) + 1
            tx_alarm["muted"] = muted
            tx_alarm["checked"] = time.time()
        except subprocess.TimeoutExpired:
            tx_alarm["sdr_ok"] = False
        except Exception as e:
            print(f"[alarm] {e}")


def sdr_scan_watch():
    """Look for SDRs at start-up and every minute while off air. Not on air:
    the board in use is plainly there, and a scan costs it a connection.
    A PlutoSky on Tezuka found on the direct link (or chosen on the page) is
    then prepared for Lynx-Tx if it is not already: tezuka_prep.py."""
    while True:
        if not tx_running():
            try:
                found = sdrs.scan(sdr_hosts())
                direct = set(sdrs.direct_hosts(config["libre"].get("direct_if", "enP4p65s0")))
                for d in found:
                    if (d.get("kind") == "plutosky" and d.get("tezuka")
                            and (d["host"] in direct or d["uri"] == sdr_uri())):
                        tezuka_prep.prepare(d["host"], tx_running)
            except Exception as e:
                print(f"[sdr] scan: {e}")
        time.sleep(60)


sdr_ref: dict = {}   # host -> sdrs.refclk() result, for the page


def _ref_now():
    """The chosen board's reference, if heard from in the last 20 s."""
    r = sdr_ref.get(sdr_host())
    return r if r and time.time() - r["seen"] < 20 else None


def sdr_ref_watch():
    """The chosen board's 10 MHz reference lock, every 5 s, from Tezuka's
    own MQTT state. Boards without it (DL1JM's firmware) are not asked."""
    while True:
        try:
            d = sdr_identity()
            if d and d.get("tezuka"):
                r = sdrs.refclk(d["host"])
                if r:
                    sdr_ref[d["host"]] = r
        except Exception as e:
            print(f"[sdr] ref: {e}")
        time.sleep(5)


def atsc3_release_libre():
    """Stop DL1JM's modulator (and the scripts that would restart it) on the
    chosen SDR - wherever it was reached."""
    libre_ssh("killall rtmppluto.sh udpts.sh pluto_dvb 2>/dev/null; sleep 1; "
              "killall -9 pluto_dvb 2>/dev/null; true", timeout=10, host=sdr_host())


def _iio(cmd: list) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "iio_attr failed").strip())
    return r.stdout.strip()


def atsc3_set_radio(live_only: bool = False, hold_power: bool = False):
    """Frequency, sample rate, filter and power, straight to the AD9361 of
    the chosen SDR - for ATSC 3.0 or ROCK-made DVB-S2, whichever is on.
    live_only: just frequency and power, changed on air with no break."""
    if not shutil.which("iio_attr"):
        raise HTTPException(500, "iio_attr is not installed on the ROCK: "
                                 "sudo apt install libiio-utils")
    uri = sdr_uri()
    if is_atsc3():
        cmds, what = atsc3mod.iio_commands(uri, config["atsc3"]), "ATSC 3.0"
        if live_only:
            cmds = [c for c in cmds if c[-2] in ("frequency", "hardwaregain")]
    else:
        m = config["modulation"]
        cmds = (dvbs2mod.radio_live_commands(uri, m) if live_only
                else dvbs2mod.iio_commands(uri, m))
        what = "DVB-S2"
    if hold_power:
        # Key-up: everything but the power. It stays at the mute level until
        # IQ is actually flowing (see iq_guard), so the DAC never sits at
        # operating power on its DDS or a stale buffer.
        cmds = [c for c in cmds if c[-2] != "hardwaregain"]
    try:
        for cmd in cmds:
            _iio(cmd)
    except Exception as e:
        raise HTTPException(500, f"The SDR at {uri} would not take the {what} settings: {e}")


def iq_power_db() -> float:
    return float((config["atsc3"] if is_atsc3() else config["modulation"])["power_db"])


def iq_bytes_needed() -> int:
    """Bytes per second on the wire when the IQ is flowing in full: 16-bit I
    and Q, so 4 bytes a sample at the DAC rate. Half of that counts as
    flowing, the same test the PlutoSky's own starvation mute uses."""
    fs = atsc3mod.dac_rate(config["atsc3"]) if is_atsc3() else dvbs2mod.dac_rate(config["modulation"])
    return int(fs) * 4


def _tx_bytes(port: str) -> Optional[int]:
    try:
        return int(Path(f"/sys/class/net/{port}/statistics/tx_bytes").read_text())
    except (OSError, ValueError):
        return None


def on_port(uri: str, port: str) -> bool:
    """Is the SDR at this URI on the port's own subnet (the direct link)?
    By address, not by name: the ROCK hands out whichever address it likes."""
    try:
        host = ipaddress.ip_address(uri.split(":", 1)[1])
        out = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", port],
                             capture_output=True, text=True, timeout=2).stdout
        return any(host in ipaddress.ip_interface(w).network
                   for w in out.split() if "/" in w and w[0].isdigit())
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return False


def iq_guard(proc: subprocess.Popen):
    """Bring the power up once IQ is flowing, and mute if the chain dies.

    Up: only once the IQ is actually reaching the SDR at its full rate, for
    a second. Not merely once iio_writedev is running: it starts at once
    and then waits for samples, while the flowgraph can take several
    seconds to load on a freshly booted ROCK. Keyed before then, a prepared
    PlutoSky saw its transmitter on with almost nothing arriving and muted
    itself 2 s later (clean install, 20 Oct 2026). On the direct link the
    rate is measured on the ROCK's second port; elsewhere, where other
    traffic shares the port, it falls back to the old timing.
    Down: if this pipeline ends without TX Off - a crashed flowgraph, a dead
    link - the SDR is muted rather than left keyed with nothing sensible to
    send."""
    port = config["libre"].get("direct_if", "enP4p65s0")
    direct = on_port(sdr_uri(), port) and _tx_bytes(port) is not None
    deadline = time.time() + 30
    if direct:
        need, good, last = iq_bytes_needed() // 2, 0, _tx_bytes(port)
        while time.time() < deadline and proc.poll() is None and good < 2:
            time.sleep(0.5)
            now = _tx_bytes(port)
            rate = (now - last) * 2 if now is not None and last is not None else 0
            last = now
            good = good + 1 if rate >= need else 0
        if good < 2 and proc.poll() is None and tx_proc is proc:
            print("[iq] the IQ never reached full rate in 30 s - not keying up")
            tx_alarm["fault"] = "the IQ never reached full rate"
    else:
        while time.time() < deadline and proc.poll() is None:
            if subprocess.run(["pgrep", "-f", "iio_writedev"], capture_output=True).returncode == 0:
                time.sleep(2)
                break
            time.sleep(0.5)
    if not tx_alarm["fault"] and proc.poll() is None and tx_proc is proc:
        try:
            _iio(tx_lo_command(sdr_uri(), True))
            _iio(atsc3mod.mute_command(sdr_uri())[:-1] + [f"{iq_power_db():g}"])
            print(f"[iq] on air at {iq_power_db():g} dB")
        except Exception as e:
            print(f"[iq] power up: {e}")
    while proc.poll() is None:
        time.sleep(1)
    if tx_proc is proc:
        # Ended by itself, not by TX Off (which clears tx_proc first).
        print("[iq] pipeline ended on its own - muting")
        tx_alarm["fault"] = "the transmit chain stopped by itself"
        atsc3_mute()


def tx_lo_command(uri: str, on: bool) -> list:
    """The AD9361's transmit LO. Tezuka's keyer powers it up and down with
    the IQ stream; with that keyer stopped (quiet mode on the PlutoSky) it
    is Lynx-Tx's job, so it is set on key-up and cleared on mute, on every
    board (harmless where something else already does it)."""
    return ["iio_attr", "-u", uri, "-o", "-c", "ad9361-phy",
            "altvoltage1", "powerdown", "0" if on else "1"]


def atsc3_mute():
    """Most attenuation the AD9361 has, then the transmit LO off. Best
    effort: never stops a TX Off."""
    uri = sdr_uri()
    for cmd in (atsc3mod.mute_command(uri), tx_lo_command(uri, False)):
        try:
            _iio(cmd)
        except Exception as e:
            print(f"[iq] mute: {e}")


def libre_restore_dvbs2():
    """After ATSC 3.0, reboot the board if its modulator is not running,
    which starts DL1JM's scripts again. About a minute."""
    out = libre_ssh("pgrep -c pluto_dvb 2>/dev/null || echo 0")
    try:
        running = int((out or "0").strip().split()[-1]) > 0
    except ValueError:
        running = False
    if running or not out:
        return
    libre_ssh_full("nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 &", timeout=8)
    _libre_cache["v"] = None
    _set_libre("rebooting", "Back to DVB-S2 - LibreSDR restarting (about a minute)")


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


_hdmi_cache = {"t": 0.0, "v": {}}


def hdmi_format() -> dict:
    """Cached for 5 s: the page asks every 2 s, and each ask is a query to
    the HDMI receiver while it may be capturing on air."""
    if time.time() - _hdmi_cache["t"] < 5:
        return _hdmi_cache["v"]
    v = _hdmi_format()
    _hdmi_cache.update(t=time.time(), v=v)
    return v


def _hdmi_format() -> dict:
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


# ── The mixer: always running ─────────────────────────────────
# switcher.py (camera | media -> one HEVC encoder) used to be the first stage
# of the TX pipeline, so every key-up, Apply or mode change tore it down and
# built it again - re-opening HDMI in and grabbing fresh 4K frame buffers
# each time. That teardown is what set off the ROCK's memory corruption on
# 19 Oct 2026. Now it starts with Lynx-Tx and keeps running, like a vision
# mixer, sending its stream to a local UDP port; TX On/Off starts and stops
# only the chain behind it. It is rebuilt only when what it is built with
# changes (picture size, GOP, the rate after a modulation change).
MIXER_PORT = 5800
mixer_plan: dict = {}          # filled by build_pipeline: "core" and "start"
mixer_proc = None
mixer_core = ""
mixer_lock = threading.RLock()


def mixer_running() -> bool:
    return mixer_proc is not None and mixer_proc.poll() is None


def stop_mixer():
    global mixer_proc
    with mixer_lock:
        if mixer_proc is not None:
            try:
                os.killpg(os.getpgid(mixer_proc.pid), signal.SIGTERM)
                mixer_proc.wait(timeout=8)
            except Exception:
                try:
                    os.killpg(os.getpgid(mixer_proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            mixer_proc = None
        subprocess.run(["pkill", "-f", "switcher.py"], capture_output=True)


def ensure_mixer() -> bool:
    """Start the mixer, or rebuild it if what it is built with has changed.
    True if it was (re)started - give it a moment before relying on it."""
    global mixer_proc, mixer_core
    if not config.get("use_switcher", True):
        stop_mixer()
        return False
    with mixer_lock:
        if not mixer_plan:
            try:
                build_pipeline()                  # fills mixer_plan
            except Exception as e:
                print(f"[mixer] cannot plan: {e}")
                return False
        if mixer_running() and mixer_core == mixer_plan["core"]:
            return False
        if mixer_running():
            was, now = mixer_core.split(" --"), mixer_plan["core"].split(" --")
            diff = [f"--{x}" for x in now if x not in was]
            print(f"[mixer] settings changed - rebuilding: {' '.join(diff)[:200]}")
        stop_mixer()
        mixer_core = mixer_plan["core"]
        mixer_proc = subprocess.Popen(
            mixer_plan["start"], shell=True, preexec_fn=os.setsid,
            stdout=subprocess.DEVNULL, stderr=open("/tmp/lynx-mixer.log", "wb"))
        print(f"[mixer] started: {mixer_core[:100]}...")
        return True


def mixer_watch():
    """Start the mixer with Lynx-Tx, and bring it back if it ever dies."""
    time.sleep(3)
    while True:
        try:
            if config.get("use_switcher", True) and not mixer_running():
                if mixer_proc is not None:
                    print(f"[mixer] stopped (exit {mixer_proc.returncode}) - restarting; "
                          "see /tmp/lynx-mixer.log")
                mixer_plan.clear()                # fresh plan: HDMI may have changed
                ensure_mixer()
        except Exception as e:
            print(f"[mixer] watch: {e}")
        time.sleep(5)


def mixer_keyframe(delay: float):
    """Ask for a keyframe once the chain behind the mixer is listening, so
    the receiver locks at once rather than at the next one in the GOP."""
    time.sleep(delay)
    switcher_call("POST", "/keyframe")


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
    # In ATSC 3.0 the modulator is ours, and its rate is exact (atsc3mod).
    atsc3 = is_atsc3()
    iq = iq_mode()
    if atsc3:
        rate = atsc3mod.ts_rate(config["atsc3"])
    elif iq:
        # DVB-S2 from the ROCK: our own modulator, so its rate is exact too.
        rate = dvbs2mod.ts_rate(m)
    else:
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
    if rate_control == "hybrid" and not hybrid_capable():
        rate_control = "cbr"            # stock plugin: see hybrid_capable()
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
    stream_in = "-"
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
        fmt = _hdmi_format()          # fresh at key-up, never the page's cache
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
        fmt = _hdmi_format()          # fresh at key-up, never the page's cache
        w, h = (fmt["width"], fmt["height"]) if fmt.get("width") else (3840, 2160)
        clip = src.get("file") or ""
        if clip and not (MEDIA_DIR / clip).is_file():
            clip = ""
        start = src.get("program") or ("med" if src["kind"] in ("video", "still") and clip else "cam")
        # The mixer runs all the time (see ensure_mixer): what it is BUILT
        # with is compared, not which clip or input it started on, so a cut
        # or a clip change never rebuilds it - only a new size, GOP or rate.
        core = (f"python3 {shlex.quote(str(APP_DIR / 'switcher.py'))} "
                f"--media-dir {shlex.quote(str(MEDIA_DIR))} "
                f"--udp 127.0.0.1:{MIXER_PORT} --bps {video_bps} "
                f"--gop {enc['gop']} --qp-floor {int(enc.get('qp_floor', 34))} --audio-bitrate {enc['audio_bitrate']} --slack {slack} "
                f"--rc {rate_control} "
                f"--rc-rate {picture_channel_bps} "
                f"--look {float(enc.get('look_ahead', 0.5))} "
                f"--rc-target {float(enc.get('rc_target', 0.3))} "
                f"--width {w} --height {h} "
                f"--video-device {shlex.quote(config['video_device'])} "
                f"--audio-device {shlex.quote(config['audio_device'])}")
        mixer_plan.update(core=core, start=core
                          + (f" --media {shlex.quote(clip)}" if clip else "")
                          + f" --start {start}")
        parts = []
        # The reserve between mixer and chain: 2 s of stream at most (in
        # 188-byte packets), so a slow start can never build a backlog that
        # delays everything on air - before, a pipe held a fraction of that.
        fifo = max(2000, int(rate * 2 / 8 / 188))
        stream_in = (f"'udp://127.0.0.1:{MIXER_PORT}?fifo_size={fifo}"
                     f"&overrun_nonfatal=1&buffer_size=4194304'")
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
    # Always in ATSC 3.0: tsprio builds the constant-rate stream at the
    # modulator's pull. ffmpeg's own -muxrate pads by the stream's clock
    # instead, and against the modulator's FIFO that backed up into the
    # camera capture: 13 frames in 10 s, 96% stuffing (16 Oct 2026).
    use_prio = (iq or bool(enc.get("sound_priority", True))) and prio.exists()
    # With priority, ffmpeg also passes each piece of sound on at once
    # (-pes_payload_size 0: every 21 ms) instead of gathering ~107 ms
    # blocks: sound that sets off 0.1 s behind arrives late whenever a
    # picture burst has used up the slack.
    muxrate = "-pes_payload_size 0 " if use_prio else f"-muxrate {rate} "
    parts.append(
        (f"ffmpeg -loglevel warning -analyzeduration 1000000 -f mpegts -i {stream_in}"
         if not parts else " | ffmpeg -loglevel warning -analyzeduration 1000000 -f mpegts -i -")
        + f" -map 0:v:0 -map '0:a:0?' -c copy -f mpegts {muxrate}"
        f"-metadata service_provider={shlex.quote(config['provider'])} "
        f"-metadata service_name={shlex.quote(config['callsign'])} "
        f"-mpegts_original_network_id {ident['original_network_id']} "
        f"-mpegts_transport_stream_id {ident['ts_id']} "
        f"-mpegts_service_id {ident['service_id']} "
        f"-mpegts_service_type {ident['service_type']} "
        f"-mpegts_pmt_start_pid {ident['pmt_pid']} "
        f"{streamids} -max_delay {int(slack * 1e6)} -pat_period 0.200 pipe:1")
    if use_prio:
        # --pace: how far tsprio may run ahead of real time to catch up. In
        # DVB-S2 1.002 (its default) stops tsp regulate's catch-up bursts
        # racing the clock. In ATSC 3.0 the DAC pulls at exactly real time,
        # and 0.2% meant a 100 ms upstream gap took 50 s to make good: the
        # buffers in front of the modulator sat near empty and the next gap
        # underran the DAC (stutter, lost packets at 4K, 16 Oct 2026). 5%
        # refills them in about 2 s; the DAC's pull and the buffers' size
        # still stop it running more than a fraction of a second ahead.
        pace = " --pace 1.05" if iq else ""
        parts.append(
            f" | python3 {shlex.quote(str(prio))} --rate {rate} --slack {slack} "
            f"--video-pid {ident['video_pid']} --audio-pid {ident['audio_pid']}{pace}")

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
    #
    # Not in ATSC 3.0: the NIT is a DVB table (its delivery descriptor is
    # DVB-S2's), and an ATSC 3.0 receiver has no use for it.
    nit_stage = ""
    nit = None
    if atsc3:
        nit_status.update(ok=False, detail="Not sent in ATSC 3.0 mode (a DVB table)")
    else:
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
    if atsc3:
        # No regulate: the modulator's pull is the pace (see atsc3mod.py),
        # and tsprio above has already made the stream constant-rate.
        flowgraph = atsc3mod.build_flowgraph(config["atsc3"], APP_DIR)
        parts.append(
            f" | tsp --buffer-size-mb 0.05 -I file - "
            f"{tap}-O file {atsc3mod.TS_FIFO}")
        return atsc3mod.modulator_shell(sdr_uri(), flowgraph) + "".join(parts)
    if iq:
        # DVB-S2 from the ROCK: as ATSC 3.0 (the DAC's pull is the pace),
        # but the NIT still rides along - it is a DVB table.
        parts.append(
            f" | tsp --buffer-size-mb 0.05 -I file - "
            f"{nit_stage}{tap}-O file {dvbs2mod.TS_FIFO}")
        return dvbs2mod.modulator_shell(sdr_uri(), APP_DIR, m) + "".join(parts)

    parts.append(
        f" | tsp --realtime --buffer-size-mb 0.05 -I file - "
        f"{nit_stage}{tap}-P regulate --bitrate {paced} "
        f"-O ip {libre_host()}:8282")

    return "".join(parts)


def start_tx() -> dict:
    global tx_proc, tx_started_at, tx_last_command
    with tx_lock:
        if iq_mode() and tx_running():
            # A restart: mute while the old IQ is still flowing.
            atsc3_mute()
        stop_tx_locked()
        # Whatever the source is, make sure it is actually being fed.
        # Starting the encoder against a loopback nothing is writing to
        # gives a pipeline that opens and then sits there.
        src = config["source"]
        # Nothing uses the old loopback feeder any more: stills are
        # frozen in GStreamer and video is read by ffmpeg inside the
        # pipeline (see build_pipeline). Stop any left from before.
        stop_feeder()
        if iq_mode():
            # DL1JM's modulator (if the LibreSDR has it) lets go, then the
            # AD9361 is set up directly. Both are quick and idempotent, so
            # every key-up does them: a board rebooted behind our back is
            # simply taken over. A Tezuka board has nothing to stop.
            if not is_atsc3():
                try:
                    dvbs2mod.check(config["modulation"])
                except ValueError as e:
                    raise HTTPException(400, f"DVB-S2: {e}")
            if sdr_is_libre():
                atsc3_release_libre()
            atsc3_mute()
            atsc3_set_radio(hold_power=True)
            try:
                cmd = build_pipeline()
            except RuntimeError as e:
                atsc3_mute()
                raise HTTPException(500, f"{'ATSC 3.0' if is_atsc3() else 'DVB-S2'} modulator: {e}")
        else:
            write_libre_settings()
            # The board's watcher restarts the modulator on the settings
            # change; give it a moment to be listening before the stream
            # arrives, or the first seconds go nowhere.
            time.sleep(2)
            cmd = build_pipeline()
        tx_last_command = cmd
        tx_alarm.update(muted=False, fault="", sdr_ok=True)
        fresh = ensure_mixer() if config.get("use_switcher", True) else False
        tx_proc = subprocess.Popen(
            cmd, shell=True, preexec_fn=os.setsid,
            stdout=subprocess.DEVNULL, stderr=open("/tmp/lynx-tx-pipeline.log", "ab"))
        tx_started_at = time.time()
        if iq_mode():
            threading.Thread(target=iq_guard, args=(tx_proc,), daemon=True).start()
        if config.get("use_switcher", True):
            threading.Thread(target=mixer_keyframe, args=(4.0 if fresh else 1.0,),
                             daemon=True).start()
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
                    "tsp --realtime", "tsprio.py", "tsbuffer.py",
                    f"file {atsc3mod.TS_FIFO}", "iio_writedev",
                    f"{atsc3mod.FLOWGRAPH_ID}.py",
                    f"file {dvbs2mod.TS_FIFO}", dvbs2mod.FLOWGRAPH_ID):
        subprocess.run(["pkill", "-f", pattern], capture_output=True)


def stop_tx() -> dict:
    with tx_lock:
        if iq_mode():
            # Attenuate first, while the IQ stream is still flowing, so the
            # DAC is never left at full output once it stops.
            atsc3_mute()
            stop_tx_locked()
            return {"success": True}
        stop_tx_locked()
        # Mute rather than leave a bare carrier: PTT follows the power
        # setting on this firmware, and below -40 dB it drops out.
        libre_ssh("sed -i 's/^power .*/power -41/' /www/settings.txt")
        return {"success": True}


def tx_running() -> bool:
    return tx_proc is not None and tx_proc.poll() is None


# ── API ───────────────────────────────────────────────────────

app = FastAPI(title="Lynx-Tx", docs_url="/api/docs")


@app.on_event("shutdown")
def mute_on_shutdown():
    """The ROCK restarting or shutting down: mute the SDR while the IQ is
    still flowing, so it is never left radiating whatever its DAC holds.
    Needs KillMode=mixed in the service, so systemd stops this process
    first and the pipeline only afterwards. (If the ROCK dies outright, the
    supervisor on the SDR mutes it: tools/plutosky-autorun.sh.)"""
    if iq_mode() and tx_running():
        print("[iq] shutting down - muting the SDR")
        atsc3_mute()
    stop_mixer()


def dump(model) -> dict:
    """A request body as a dict, on Pydantic 2 (model_dump) or Pydantic 1
    (dict), which is what Debian 12 packages: the dev ROCK had 2 and a
    clean install found the difference (20 Oct 2026)."""
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


class ModulationUpdate(BaseModel):
    frequency_mhz: Optional[float] = None
    symbol_rate_ks: Optional[int] = None
    constellation: Optional[str] = None
    fec: Optional[str] = None
    pilots: Optional[bool] = None
    rolloff: Optional[str] = None
    power_db: Optional[float] = None


class EncoderUpdate(BaseModel):
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    audio: Optional[bool] = None
    audio_bitrate: Optional[int] = None


class TweaksUpdate(BaseModel):
    content: Optional[str] = None
    rate_control: Optional[str] = None
    slack: Optional[float] = None
    qp_floor: Optional[int] = None
    gop: Optional[int] = None
    rc_target: Optional[float] = None
    look_ahead: Optional[float] = None
    hybrid_aim: Optional[float] = None
    picture_share: Optional[float] = None
    sound_priority: Optional[bool] = None


class SourceUpdate(BaseModel):
    kind: str
    file: str = ""


def _safe(f, default=None):
    """A status figure that must never take the whole status down."""
    try:
        return f()
    except Exception:
        return default


def _read_int(path: str, key: str = "") -> Optional[int]:
    """A number from a /proc or /sys file: the whole file, or the line
    starting with key."""
    try:
        with open(path) as f:
            for line in f:
                if not key:
                    return int(line.split()[0])
                if line.startswith(key):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


REF_STATES = {"locked": 1, "acquire": 2, "holdover": 3, "returning": 3, "absent": 4}
CAMERA_STATES = {"live": 1, "bars": 2, "unstable": 3}


THERMAL_SYSFS = os.environ.get("LYNX_THERMAL_SYSFS", "/sys/class")
_board_cache = {"t": 0.0, "v": None}


def read_board() -> Optional[dict]:
    """Processor temperature and fan speed, from the kernel: the same
    reading as the Lynx Receiver's board badge. Prefers the RK3588's
    soc-thermal sensor; the fan is the pwm-fan PWM that lynx-fan sets
    (tools/lynx_fan.py). No RPM on the ROCK 5T: its fan header has no
    tachometer pin. Cached for 3 s, so the page and SNMP cost nothing."""
    if time.time() - _board_cache["t"] < 3:
        return _board_cache["v"]
    import glob

    def rd(p):
        with open(p) as f:
            return f.read().strip()
    info = None
    zones = {}
    for z in sorted(glob.glob(f"{THERMAL_SYSFS}/thermal/thermal_zone*")):
        try:
            zones[rd(f"{z}/type")] = z
        except OSError:
            pass
    if zones:
        sensor = next((n for n in ("soc-thermal", "cpu-thermal") if n in zones), next(iter(zones)))
        try:
            info = {"temp_c": round(int(rd(f"{zones[sensor]}/temp")) / 1000.0, 1), "sensor": sensor,
                    "fan_pwm": None, "fan_pct": None, "fan_rpm": None}
        except (OSError, ValueError):
            info = None
    if info:
        for h in sorted(glob.glob(f"{THERMAL_SYSFS}/hwmon/hwmon*")):
            try:
                if rd(f"{h}/name") not in ("pwmfan", "pwm-fan", "cooling_fan"):
                    continue
                pwm = int(rd(f"{h}/pwm1"))
                info.update(fan_pwm=pwm, fan_pct=round(pwm * 100 / 255))
                try:
                    info["fan_rpm"] = int(rd(f"{h}/fan1_input"))
                except (OSError, ValueError):
                    pass
                break
            except (OSError, ValueError):
                continue
    _board_cache.update(t=time.time(), v=info)
    return info


def read_host() -> dict:
    """The ROCK's own health, for SNMP: how long since it started (an
    unexpected restart shows as a drop), how busy it is over the last
    minute (load average as a share of all eight cores), and the space
    left on the system drive (media uploads and logs live there; a full
    card stops everything)."""
    out = {"uptime_s": 0, "load_pct": 0, "disk_free_mb": 0}
    try:
        out["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
        out["load_pct"] = int(round(os.getloadavg()[0] * 100 / (os.cpu_count() or 1)))
        out["disk_free_mb"] = shutil.disk_usage("/").free // (1 << 20)
    except (OSError, ValueError):
        pass
    return out


@app.get("/api/health")
def api_health():
    """Everything the SNMP agent serves (tools/lynx-snmp-agent.py, MIB in
    mibs/LYNX-MIB.txt), as plain numbers. Never waits on the SDR: every
    value is cached or local."""
    on = tx_running()
    if tx_alarm["locked_out"]:
        state = 5                                   # lockout
    elif tx_alarm["muted"] and on:
        state = 3                                   # mutedBySdr
    elif not on and tx_alarm["fault"]:
        state = 4                                   # fault
    else:
        state = 2 if on else 1
    ref = _safe(lambda: _ref_now())
    ref_state = REF_STATES.get((ref or {}).get("state", ""), 1 if (ref or {}).get("locked") else 5) \
        if ref else 5
    sw = (switcher_call("GET", "/status") or {}) if mixer_running() else {}
    found = any(d["uri"] == sdr_uri() for d in sdrs.found())
    c = config["atsc3"] if is_atsc3() else config["modulation"]
    anon = _read_int("/sys/fs/cgroup/system.slice/lynx-tx.service/memory.stat", "anon ")
    cma = _read_int("/proc/meminfo", "CmaFree:")
    board = read_board() or {}
    host = read_host()
    return {
        "txState": state,
        "sdrReachable": 1 if (tx_alarm["sdr_ok"] if on else found) else 2,
        "refState": ref_state,
        "mixerRunning": 1 if mixer_running() else 2,
        "hdmiSignal": 1 if hdmi_format() else 2,
        "frequencyKHz": int(round(float(c["frequency_mhz"]) * 1000)),
        "powerCentiDb": int(round(iq_power_db() * 100)),
        "mode": "ATSC 3.0" if is_atsc3() else "DVB-S2",
        "sdrMutes": tx_alarm.get("mutes", 0),
        "memAnonMB": (anon or 0) // (1 << 20),
        "cmaFreeMB": (cma or 0) // 1024,
        "cpuTempDeciC": int(round((board.get("temp_c") or 0) * 10)),
        "cameraState": CAMERA_STATES.get(sw.get("camera", ""), 4),
        "autoRecoveries": tx_alarm["recoveries"],
        "fanPercent": board.get("fan_pct") or 0,
        "uptimeS": host["uptime_s"],
        "cpuLoadPct": host["load_pct"],
        "diskFreeMB": host["disk_free_mb"],
    }


@app.get("/api/status")
def api_status():
    m = config["modulation"]
    if is_atsc3():
        theoretical = atsc3mod.ts_rate(config["atsc3"])
    elif dvbs2_rock():
        theoretical = _safe(lambda: dvbs2mod.ts_rate(m), 0)
    else:
        theoretical = ts_bitrate(m["symbol_rate_ks"], m["constellation"],
                                 m["fec"], m["pilots"])
    return {
        "mode": "atsc3" if is_atsc3() else "dvbs2",
        "dvbs2_engine": ("board" if config.get("dvbs2_engine") == "board"
                         and sdr_is_libre() else "rock"),
        "iq": iq_mode(),
        "sdr": {"uri": sdr_uri(), "chosen": (config.get("sdr") or {}).get("uri", ""),
                "is_libre": sdr_is_libre(), "found": sdrs.found(),
                "last_scan": sdrs.last_scan(),
                "ref": _safe(lambda: _ref_now()),
                "prep": _safe(lambda: tezuka_prep.status(sdr_host())),
                "dac_rate": _safe(lambda: dvbs2mod.dac_rate(m)) if dvbs2_rock() else None},
        "atsc3_choices": {"constellations": atsc3mod.CONSTELLATIONS,
                          "rates": atsc3mod.CODE_RATES, "ts_rates": atsc3mod.TS_RATES},
        "transmitting": tx_running(),
        "alarm": {"muted": tx_alarm["muted"] and tx_running(),
                  "fault": tx_alarm["fault"],
                  "sdr_ok": tx_alarm["sdr_ok"],
                  "locked_out": tx_alarm["locked_out"],
                  "recovering": tx_alarm["recovering"],
                  "recoveries": tx_alarm["recoveries"]},
        "uptime": int(time.time() - tx_started_at) if tx_running() else 0,
        "config": config,
        "hdmi": hdmi_format(),
        "board": read_board(),
        "media": media_files(),
        "ts_bitrate": theoretical,
        "libre": libre_status(),
        "libre_patch": libre_patch,
        "buffer": read_buffer() if tx_running() else None,
        "switcher": (switcher_call("GET", "/status")
                     if mixer_running() and config.get("use_switcher", True) else None),
        "use_switcher": config.get("use_switcher", True),
        "nit": nit_status,
        "tweaks": {"hybrid_capable": hybrid_capable(), "tested": TESTED_TWEAKS,
                   "presets": CONTENT_PRESETS, "limits": TWEAK_LIMITS},
    }


@app.post("/api/start")
def api_start():
    # A person pressed TX On: they want to be on air, and any lockout is over.
    tx_alarm.update(wanted=True, locked_out=False)
    tx_alarm["recoveries_recent"].clear()
    return start_tx()


@app.post("/api/stop")
def api_stop():
    tx_alarm["wanted"] = False
    return stop_tx()


class ModeUpdate(BaseModel):
    mode: str


@app.post("/api/mode")
def api_mode(req: ModeUpdate):
    """DVB-S2 or ATSC 3.0. Changing it takes the transmitter off air: the
    two use the LibreSDR quite differently, and going back to DVB-S2 means
    restarting the board's own modulator."""
    if req.mode not in ("dvbs2", "atsc3"):
        raise HTTPException(400, "mode must be dvbs2 or atsc3")
    if req.mode == config.get("mode", "dvbs2"):
        return {"success": True, "mode": req.mode}
    if tx_running():
        stop_tx()
    config["mode"] = req.mode
    save_config(config)
    if req.mode == "dvbs2" and not dvbs2_rock():
        threading.Thread(target=libre_restore_dvbs2, daemon=True).start()
    return {"success": True, "mode": req.mode}


class EngineUpdate(BaseModel):
    engine: str


@app.post("/api/dvbs2_engine")
def api_dvbs2_engine(req: EngineUpdate):
    """Who makes DVB-S2: the ROCK ("rock", any SDR) or DL1JM's modulator on
    the LibreSDR ("board"). Off air first; going to "board" restarts the
    LibreSDR's own modulator (about a minute)."""
    if req.engine not in ("rock", "board"):
        raise HTTPException(400, "engine must be rock or board")
    if req.engine == "board" and not sdr_is_libre():
        raise HTTPException(400, "DL1JM's modulator is only on the LibreSDR: choose it as the SDR first")
    if req.engine == config.get("dvbs2_engine", "rock"):
        return {"success": True, "engine": req.engine}
    if tx_running():
        stop_tx()
    config["dvbs2_engine"] = req.engine
    save_config(config)
    if req.engine == "board" and not is_atsc3():
        threading.Thread(target=libre_restore_dvbs2, daemon=True).start()
    return {"success": True, "engine": req.engine}


class SdrUpdate(BaseModel):
    uri: str


@app.post("/api/sdr")
def api_sdr(req: SdrUpdate):
    """Choose the SDR that transmits. "" = automatic. Off
    air first: the IQ stream cannot move boards mid-transmission."""
    uri = req.uri.strip()
    if uri and not uri.startswith(("ip:", "usb:")):
        uri = "ip:" + uri
    if uri and not sdrs.probe(uri):
        raise HTTPException(400, f"No SDR answering at {uri}")
    if uri == (config.get("sdr") or {}).get("uri", ""):
        return {"success": True, "uri": uri}
    if tx_running():
        stop_tx()
    sd = config.setdefault("sdr", {})
    sd["uri"] = uri
    # Remember an address typed in, so later scans look there too.
    host = uri.split(":", 1)[-1] if uri else ""
    if host and host not in sdr_hosts():
        sd["hosts"] = list(sd.get("hosts") or []) + [host]
    # DL1JM's modulator lives on the LibreSDR only.
    if not sdr_is_libre():
        config["dvbs2_engine"] = "rock"
    save_config(config)
    return {"success": True, "uri": uri}


@app.post("/api/sdr/scan")
def api_sdr_scan():
    """Look for SDRs now (the direct link, configured hosts and mDNS)."""
    return {"success": True, "found": sdrs.scan(sdr_hosts())}


class Atsc3Update(BaseModel):
    frequency_mhz: Optional[float] = None
    constellation: Optional[str] = None
    rate: Optional[str] = None
    power_db: Optional[float] = None
    oversample: Optional[int] = None


@app.post("/api/atsc3")
def api_atsc3(req: Atsc3Update):
    """The ATSC 3.0 Modulation card. Frequency and power go straight to the
    AD9361 on air, with no break; constellation and code rate change the
    modulator and the stream's rate, so they restart the transmitter."""
    old = atsc3mod.clean(config.get("atsc3"))
    new = dict(old)
    for k, v in dump(req).items():
        if v is not None:
            new[k] = v
    new = atsc3mod.clean(new)
    if new["constellation"] != req.constellation and req.constellation is not None:
        raise HTTPException(400, f"unknown constellation {req.constellation}")
    if new["rate"] != req.rate and req.rate is not None:
        raise HTTPException(400, f"unknown code rate {req.rate}")
    config["atsc3"] = new
    save_config(config)
    if tx_running() and is_atsc3():
        if ((new["constellation"], new["rate"], new["oversample"]) !=
                (old["constellation"], old["rate"], old["oversample"])):
            start_tx()
        elif (new["frequency_mhz"], new["power_db"]) != (old["frequency_mhz"], old["power_db"]):
            atsc3_set_radio(live_only=True)
    return {"success": True, "atsc3": new, "ts_rate": atsc3mod.ts_rate(new)}


@app.post("/api/system/{action}")
def api_system(action: str):
    """Restart or shut down the ROCK itself, off air first.

    Needs one sudoers rule for the account the service runs as, allowing
    exactly these two commands and nothing else:
        radxa ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff,
            /usr/bin/systemctl restart lynx-tx   (the last for Update)
    """
    verb = {"restart": "reboot", "shutdown": "poweroff"}.get(action)
    if not verb:
        raise HTTPException(400, "action must be restart or shutdown")
    if tx_running():
        stop_tx()
    r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", verb],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise HTTPException(500, "The ROCK refused: " + (r.stderr.strip() or "no permission") +
                            ". Add the sudoers rule in /etc/sudoers.d/lynx-tx (see README).")
    return {"success": True, "action": action}


# ── Updates ───────────────────────────────────────────────────
# Like the Lynx Receiver's: checking is manual (a ROCK at a site may have
# no internet, and nothing should reach out on its own), and an update is
# a fast-forward pull of the branch this ROCK is on, then a restart of
# Lynx-Tx. Off air only.

update_state = {"version": None, "branch": None, "checked_at": None,
                "error": None, "behind": 0, "commits": []}


def git(*args, timeout: int = 20) -> tuple:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git", "-C", str(APP_DIR), *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
        return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).strip()
    except Exception as e:
        return False, str(e)


def update_version():
    ok, v = git("describe", "--tags", "--always")
    update_state["version"] = v if ok else "unknown"
    ok, b = git("rev-parse", "--abbrev-ref", "HEAD")
    update_state["branch"] = b if ok and b != "HEAD" else "main"


@app.get("/api/update/status")
def api_update_status():
    if update_state["version"] is None:
        update_version()
    return update_state


@app.post("/api/update/check")
def api_update_check():
    update_version()
    update_state["checked_at"] = time.strftime("%d %b %Y %H:%M")
    ok, err = git("fetch", "--quiet", "origin")
    if not ok:
        update_state.update(error=f"Could not reach GitHub: {err}", behind=0, commits=[])
        return update_state
    b = update_state["branch"]
    ok, n = git("rev-list", "--count", f"HEAD..origin/{b}")
    behind = int(n) if ok and n.isdigit() else 0
    ok, log = git("log", "--oneline", "--max-count=20", f"HEAD..origin/{b}")
    update_state.update(error=None, behind=behind,
                        commits=log.splitlines() if ok and log else [])
    return update_state


@app.post("/api/update/apply")
def api_update_apply():
    if tx_running():
        raise HTTPException(409, "Take the transmitter off air first (TX Off), then update.")
    b = update_state["branch"] or "main"
    ok, out = git("pull", "--ff-only", "origin", b, timeout=60)
    if not ok:
        raise HTTPException(502, f"Update failed, nothing was changed: {out}")
    update_state.update(behind=0, commits=[], version=None)

    def restart():
        time.sleep(1)   # let the reply reach the browser first
        # The installer's sudoers rule allows restarting Lynx-Tx; an older
        # install allows only reboot, which does the same job, slower.
        for cmd in (["systemctl", "restart", "lynx-tx"], ["systemctl", "reboot"]):
            if subprocess.run(["sudo", "-n", "/usr/bin/" + cmd[0], *cmd[1:]],
                              capture_output=True, timeout=30).returncode == 0:
                return
        print("[update] the code IS updated - restart Lynx-Tx to run it")
    threading.Thread(target=restart, daemon=True).start()
    return {"success": True, "pull": out}


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
    d = dump(req)
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
    old = dict(config["modulation"])
    new = dict(old)
    for k, v in dump(req).items():
        if v is not None:
            new[k] = v
    if "rolloff" in new:
        new["rolloff"] = dvbs2mod.norm_rolloff(new["rolloff"])
    if dvbs2_rock():
        # Refuse what the ROCK's modulator cannot make (8PSK 1/2, say)
        # before it reaches the config, with the reason.
        try:
            dvbs2mod.check(new)
        except ValueError as e:
            raise HTTPException(400, str(e))
    config["modulation"] = new
    save_config(config)
    if tx_running():
        # Power is always live. Frequency too, unless the NIT is on: it
        # carries the frequency, so a live change would leave it telling
        # receivers the old one - restart and rebuild it instead.
        live = ("power_db",) if config["identity"].get("nit", True) else ("frequency_mhz", "power_db")
        if dvbs2_rock() and all(new[k] == old.get(k) for k in new if k not in live):
            # Frequency and power go straight to the AD9361, no break.
            atsc3_set_radio(live_only=True)
        else:
            start_tx()   # restart so the change takes effect
    return {"success": True, "config": config["modulation"],
            "ts_rate": dvbs2mod.ts_rate(new) if dvbs2_rock() else None}


@app.post("/api/encoder")
def api_encoder(req: EncoderUpdate):
    for k, v in dump(req).items():
        if v is not None:
            config["encoder"][k] = v
    save_config(config)
    if tx_running():
        start_tx()
    return {"success": True, "config": config["encoder"]}


@app.post("/api/tweaks")
def api_tweaks(req: TweaksUpdate):
    """The trade-off settings, all at once. Applied by a restart if on air
    - one restart for the whole card, not one per control."""
    t = clean_tweaks(dump(req))
    config["encoder"].pop("video_share", None)          # long unused
    config["encoder"].update(t)
    save_config(config)
    if tx_running():
        start_tx()
    return {"success": True, "config": config["encoder"]}


@app.post("/api/tweaks/reset")
def api_tweaks_reset():
    config["encoder"].update(TESTED_TWEAKS)
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
    live = mixer_running() and switcher_call("POST", f"/take?src={req.src}") is not None
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
    live = mixer_running() and switcher_call("POST", f"/load?file={quote(n)}") is not None
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
  /* Tweak cards: each control explains its trade-off and shows the tested value. */
  .help { font-size: .78em; color: #9fb3d1; line-height: 1.35; }
  .tested { font-size: .72em; color: #6b7c99; }
  .tweaks label.small { display: flex; justify-content: space-between; width: 100%; }
  .tv { color: #4ade80; font-family: monospace; }
  .tweaks .form-range { margin-bottom: 0; }
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
    <span class="btn btn-sm" id="ref-badge"
          style="background:#3a4a63;color:#fff;cursor:default;display:none">10 MHz Reference</span>
    <span class="btn btn-sm" id="sdr-badge"
          style="background:#3a4a63;color:#fff;cursor:default">SDR idle</span>
    <span class="btn btn-sm" id="hdmi-badge"
          style="background:#3a4a63;color:#fff;cursor:default">HDMI ?</span>
    <span class="btn btn-sm" id="board-badge" title="Processor temperature and fan"
          style="background:#3a4a63;color:#fff;cursor:default;display:none">--</span>
    <span class="ms-auto d-flex gap-2 align-items-center">
      <span class="btn btn-sm" id="version-badge" title="Not yet checked: click Check updates"
            style="background:#3a4a63;color:#fff;cursor:default">version ?</span>
      <button class="btn btn-sm btn-outline-light" id="update-check-btn" onclick="checkUpdates()">Check updates</button>
      <button class="btn btn-sm btn-warning" id="update-apply-btn" onclick="applyUpdate()" style="display:none">&#x2B06;&#xFE0F; Update</button>
    </span>
  </div>

  <div class="row g-3">

    <div class="col-lg-4">
      <div class="card mb-3">
        <div class="card-header">&#x25B6;&#xFE0F; Transmit</div>
        <div class="card-body">
          <div class="small text-muted mb-1">Mode</div>
          <div class="btn-group w-100 mb-3" role="group">
            <button class="btn btn-outline-light" id="mode-dvbs2" onclick="setMode('dvbs2')">DVB-S2</button>
            <button class="btn btn-outline-light" id="mode-atsc3" onclick="setMode('atsc3')">ATSC 3.0</button>
          </div>
          <div class="small text-muted mb-1">SDR</div>
          <div class="d-flex gap-2 mb-1">
            <select class="form-select form-select-sm bg-dark text-light border-secondary"
                    id="sdr-select" onchange="setSdr(this.value)"></select>
            <button class="btn btn-sm btn-outline-light" onclick="scanSdr()">Scan</button>
          </div>
          <div class="d-flex gap-2 mb-1">
            <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                   id="sdr-add" placeholder="Another address, e.g. 192.168.1.50">
            <button class="btn btn-sm btn-outline-light" onclick="addSdr()">Use</button>
          </div>
          <div class="small text-muted mb-3" id="sdr-info">&mdash;</div>
          <div class="d-grid gap-2 mb-3">
            <button class="btn btn-secondary big-btn" id="btn-txon" onclick="startTx()">TX On</button>
            <button class="btn btn-secondary big-btn" id="btn-txoff" onclick="stopTx()">TX Off</button>
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
            <canvas id="bb-hist" height="150" style="width:100%;display:block;margin-top:2px;background:#0b0f1d;border:1px solid #0f3460;border-radius:4px"></canvas>
          </div>
          <div class="small mt-2" id="libre-patch">LibreSDR script: checking&hellip;</div>
          <button class="btn btn-sm btn-outline-light mt-2" id="libre-check" onclick="checkLibre()">Check LibreSDR</button>
          <a href="/monitor" class="btn btn-sm btn-outline-light mt-2 ms-1">&#x1F4C8; TX monitor</a>
          <div class="d-flex gap-2 mt-3 pt-2" style="border-top:1px solid #0f3460">
            <button class="btn btn-sm btn-outline-warning flex-fill" onclick="system('restart')">&#x1F504; Restart ROCK</button>
            <button class="btn btn-sm btn-outline-danger flex-fill" onclick="system('shutdown')">&#x23FB; Shut down ROCK</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">&#x1F4FB; Modulation</div>
        <div class="card-body">
          <label class="small text-muted">Frequency (MHz)</label>
          <input type="number" step="0.001" class="form-control form-control-sm bg-dark text-light border-secondary mb-2"
                 id="m-freq" onchange="saveMod()">
          <div id="mod-atsc3" style="display:none">
            <div class="row g-2 mb-2">
              <div class="col-6">
                <label class="small text-muted">Constellation</label>
                <select class="form-select form-select-sm bg-dark text-light border-secondary"
                        id="a-const" onchange="saveMod()">
                  <option>QPSK</option><option>16QAM</option>
                  <option>64QAM</option><option>256QAM</option>
                </select>
              </div>
              <div class="col-6">
                <label class="small text-muted">Code rate</label>
                <select class="form-select form-select-sm bg-dark text-light border-secondary"
                        id="a-rate" onchange="saveMod()">
                  <option>2/15</option><option>3/15</option><option>4/15</option>
                  <option>5/15</option><option>6/15</option><option>7/15</option>
                  <option>8/15</option><option>9/15</option><option>10/15</option>
                  <option>11/15</option><option>12/15</option><option>13/15</option>
                </select>
              </div>
            </div>
            <div class="small mb-2">TS rate: <span class="status-value" id="a-tsrate">&mdash;</span></div>
            <label class="small text-muted">Output filter</label>
            <select class="form-select form-select-sm bg-dark text-light border-secondary mb-1"
                    id="a-os" onchange="saveMod()">
              <option value="2">2&times; oversample &mdash; DAC at 13.824 MS/s (recommended)</option>
              <option value="1">Off &mdash; DAC at 6.912 MS/s</option>
            </select>
            <div class="text-muted small mb-2">
              The DAC's first image sits at the sample rate less the band edge,
              &plusmn;4.0 MHz from the carrier. Oversampling moves it out to
              about &plusmn;10.9 MHz, where the LibreSDR's own filter removes it.
            </div>
            <div class="text-muted small mb-2">
              6 MHz channel, 8K FFT, guard interval 1024, pilots SP3_4: the structure
              proven on air. Ron Economos W6RZ's gr-atsc3 on the ROCK; the LibreSDR
              transmits the IQ.
            </div>
          </div>
          <div id="mod-dvbs2">
          <label class="small text-muted">Modulator</label>
          <select class="form-select form-select-sm bg-dark text-light border-secondary mb-2"
                  id="m-engine" onchange="setEngine(this.value)">
            <option value="rock">ROCK: Ron Economos W6RZ's gr-dtv (any SDR)</option>
            <option value="board">LibreSDR on board: DL1JM's firmware</option>
          </select>
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
          <div id="m-rock-only">
            <label class="small text-muted">Roll-off</label>
            <select class="form-select form-select-sm bg-dark text-light border-secondary mb-1"
                    id="m-rolloff" onchange="saveMod()">
              <option>0.35</option><option>0.25</option><option>0.20</option>
            </select>
            <div class="text-muted small mb-1">The Picotuner's Serit tuner does not take 0.20.</div>
            <div class="small mb-2">TS rate: <span class="status-value" id="m-tsrate">&mdash;</span>
              &middot; DAC <span class="status-value" id="m-dac">&mdash;</span></div>
          </div>
          </div>
          <label class="small text-muted">Power (dB)</label>
          <input type="range" class="form-range" id="m-power" min="-40" max="0"
                 step="0.5" oninput="document.getElementById('m-power-v').textContent=this.value+' dB'"
                 onchange="saveMod()">
          <div class="text-end status-value" id="m-power-v">—</div>
          <div class="text-muted small" id="m-power-note">
            Below &minus;40 dB this firmware drops PTT, which is how TX Off
            leaves the carrier off.
          </div>
        </div>
      </div>
    </div>

    <div class="col-lg-4">
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
      <div class="card mt-3 tweaks">
        <div class="card-header">&#x1F39A;&#xFE0F; Source tweaks</div>
        <div class="card-body">
          <label class="small text-muted">Content &mdash; what you are sending</label>
          <div class="btn-group w-100 mb-1" role="group">
            <button class="btn btn-sm btn-outline-light" id="c-talking" onclick="preset('talking')">&#x1F5E3;&#xFE0F; Talking heads</button>
            <button class="btn btn-sm btn-outline-light" id="c-balanced" onclick="preset('balanced')">&#x2696;&#xFE0F; Balanced</button>
            <button class="btn btn-sm btn-outline-light" id="c-action" onclick="preset('action')">&#x1F3B8; Action</button>
          </div>
          <div class="help mb-3" id="c-help">&mdash;</div>

          <label class="small text-muted">Look-ahead <span class="tv" id="v-look"></span></label>
          <input type="range" class="form-range" id="t-look" min="0" max="1" step="0.1" oninput="tweakChanged()">
          <div class="help mb-1">The controller scores each picture this far ahead of the encoder, so it
            can ease off <i>before</i> a strobe or a burst of motion arrives. Costs the same delay end to
            end &mdash; fine for playout, noticeable on a live camera with talkback. 0 = react only.</div>
          <div class="tested">Tested: <span id="d-look"></span></div>

          <div class="d-flex gap-2 mt-3">
            <button class="btn btn-sm btn-warning flex-fill apply-btn" onclick="applyTweaks()" disabled>Apply</button>
            <button class="btn btn-sm btn-outline-light" onclick="resetTweaks()">Reset to tested</button>
          </div>
          <div class="help mt-1 apply-note"></div>
        </div>
      </div>
    </div>

    <div class="col-lg-4">
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
      <div class="card mt-3 tweaks">
        <div class="card-header">&#x1F527; Encoder tweaks &mdash; very advanced</div>
        <div class="card-body">
          <div class="help mb-3" style="color:#e8a33d">Every setting here is a compromise. The tested
            values are what ran 16 minutes of the hardest clip at QPSK 3/4 with nothing late.</div>

          <label class="small text-muted">Rate control</label>
          <select class="form-select form-select-sm bg-dark text-light border-secondary mb-1"
                  id="t-rc" onchange="tweakChanged()">
            <option value="hybrid">Hybrid &mdash; encoder&rsquo;s own control, guarded floor</option>
            <option value="cbr">CBR &mdash; encoder alone</option>
          </select>
          <div class="help mb-1" id="rc-help"></div>
          <div class="tested mb-3">Tested: <span id="d-rc"></span></div>

          <label class="small text-muted">Receiver buffer <span class="tv" id="v-slack"></span></label>
          <input type="range" class="form-range" id="t-slack" min="0.3" max="1.0" step="0.05" oninput="tweakChanged()">
          <div class="help mb-1">How far ahead of need pictures are sent. More rides out bigger bursts,
            but every receiver must hold it: the spec limit is 1.0 s; the Sony KD-65X850B is happy at
            0.9 s. Keep 0.6 s for an unknown receiver.</div>
          <div class="tested mb-3">Tested: <span id="d-slack"></span></div>

          <label class="small text-muted">Finest QP <span class="tv" id="v-qp"></span></label>
          <input type="range" class="form-range" id="t-qp" min="20" max="40" step="1" oninput="tweakChanged()">
          <div class="help mb-1">The sharpest the encoder may go. Lower = crisper stills, faces and
            text, using spare channel in calm scenes &mdash; but bigger keyframes (QP 30: ~0.27 s of
            channel each). Hard scenes are unaffected: the floor rises there anyway.</div>
          <div class="tested mb-3">Tested: <span id="d-qp"></span></div>

          <label class="small text-muted">Keyframe interval</label>
          <select class="form-select form-select-sm bg-dark text-light border-secondary mb-1"
                  id="t-gop" onchange="tweakChanged()">
            <option value="25">1 s</option><option value="50">2 s</option>
            <option value="75">3 s</option><option value="100">4 s</option>
            <option value="150">6 s</option>
          </select>
          <div class="help mb-1">Longer spends fewer bits on keyframes and more on the pictures
            between them &mdash; but a receiver tuning in, or recovering from a dropout, waits up to
            this long for a picture.</div>
          <div class="tested mb-3">Tested: <span id="d-gop"></span></div>

          <div id="hy-only">
            <label class="small text-muted">Floor reacts <span class="tv" id="v-tgt"></span></label>
            <input type="range" class="form-range" id="t-tgt" min="0.1" max="0.6" step="0.05" oninput="tweakChanged()">
            <div class="d-flex justify-content-between help"><span>&larr; early: softer, smooth</span><span>late: sharper, riskier &rarr;</span></div>
            <div class="help mb-1">How full the buffer may get before the floor starts rising.</div>
            <div class="tested mb-3">Tested: <span id="d-tgt"></span></div>

            <label class="small text-muted">Encoder aim <span class="tv" id="v-aim"></span></label>
            <input type="range" class="form-range" id="t-aim" min="0.85" max="1.0" step="0.01" oninput="tweakChanged()">
            <div class="help mb-1">The encoder&rsquo;s own target, as a share of the picture&rsquo;s real
              channel. Keep it under 100%: above, the encoder and the floor fight and the floor sticks high.</div>
            <div class="tested mb-3">Tested: <span id="d-aim"></span></div>
          </div>

          <div id="cbr-only">
            <label class="small text-muted">Picture share <span class="tv" id="v-share"></span></label>
            <input type="range" class="form-range" id="t-share" min="0.5" max="0.97" step="0.01" oninput="tweakChanged()">
            <div class="help mb-1">CBR only: the encoder&rsquo;s target as a share of what sound and tables
              leave. The rest is headroom for its overshoots &mdash; with nothing to guard them, keep it
              at 0.85 or below.</div>
            <div class="tested mb-3">Tested: <span id="d-share"></span></div>
          </div>

          <div class="form-check form-switch mb-1">
            <input class="form-check-input" type="checkbox" id="t-prio" onchange="tweakChanged()">
            <label class="form-check-label small" for="t-prio">Sound priority</label>
          </div>
          <div class="help mb-3">Sound always goes before pictures in the multiplex, so a burst of
            picture can never make the sound drop out. Leave on.</div>

          <div class="d-flex gap-2">
            <button class="btn btn-sm btn-warning flex-fill apply-btn" onclick="applyTweaks()" disabled>Apply</button>
            <button class="btn btn-sm btn-outline-light" onclick="resetTweaks()">Reset to tested</button>
          </div>
          <div class="help mt-1 apply-note"></div>
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

let mediaKey = null;
function showMedia() {
  const el = document.getElementById('media-list');
  if (!cfg) return;
  const files = cfg.media || [];
  const key = JSON.stringify([files, cfg.config.source.file]);
  if (key === mediaKey) return;                 // nothing changed: leave it alone
  mediaKey = key;
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
  const freq = parseFloat(document.getElementById('m-freq').value);
  const power = parseFloat(document.getElementById('m-power').value);
  if (cfg && cfg.mode === 'atsc3') {
    await api('POST', '/api/atsc3', {
      frequency_mhz: freq, power_db: power,
      constellation: document.getElementById('a-const').value,
      rate: document.getElementById('a-rate').value,
      oversample: parseInt(document.getElementById('a-os').value, 10),
    });
  } else {
    await api('POST', '/api/modulation', {
      frequency_mhz: freq,
      symbol_rate_ks: parseInt(document.getElementById('m-sr').value, 10),
      constellation: document.getElementById('m-const').value,
      fec: document.getElementById('m-fec').value,
      pilots: document.getElementById('m-pilots').checked,
      rolloff: document.getElementById('m-rolloff').value,
      power_db: power
    });
  }
  poll();
}

// The Modulation card's values for the current mode. Each mode keeps its
// own frequency and power: DVB-S2 at 1304 MHz and ATSC 3.0 on 70cm into
// the HDHomeRun are both normal.
function fillMod(c, mode) {
  const m = c.modulation, a = c.atsc3 || {};
  const at = mode === 'atsc3';
  document.getElementById('m-freq').value = at ? a.frequency_mhz : m.frequency_mhz;
  const p = at ? a.power_db : m.power_db;
  document.getElementById('m-power').value = p;
  document.getElementById('m-power-v').textContent = p + ' dB';
  document.getElementById('m-sr').value = m.symbol_rate_ks;
  document.getElementById('m-const').value = m.constellation;
  document.getElementById('m-fec').value = m.fec;
  document.getElementById('m-pilots').checked = m.pilots;
  document.getElementById('m-rolloff').value = m.rolloff || '0.35';
  document.getElementById('a-const').value = a.constellation;
  document.getElementById('a-rate').value = a.rate;
  document.getElementById('a-os').value = String(a.oversample || 2);
}

async function setSdr(uri) {
  const sel = document.getElementById('sdr-select');
  sel.blur(); sdrKey = null;
  if (cfg && cfg.transmitting &&
      !confirm('Changing the SDR takes the transmitter off air. Carry on?')) { poll(); return; }
  await api('POST', '/api/sdr', {uri});
  sdrKey = null;
  poll();
}

async function addSdr() {
  const v = document.getElementById('sdr-add').value.trim();
  if (!v) return;
  if (cfg && cfg.transmitting &&
      !confirm('Changing the SDR takes the transmitter off air. Carry on?')) return;
  setText('sdr-info', 'Asking ' + v + '...');
  const j = await api('POST', '/api/sdr', {uri: v});
  if (j.success) { document.getElementById('sdr-add').value = ''; await api('POST', '/api/sdr/scan'); }
  sdrKey = null;
  poll();
}

async function scanSdr() {
  setText('sdr-info', 'Looking for SDRs on the direct link, the LAN and mDNS...');
  await api('POST', '/api/sdr/scan');
  sdrKey = null;
  poll();
}

async function setEngine(engine) {
  if (cfg && engine === cfg.dvbs2_engine) return;
  if (cfg && cfg.transmitting &&
      !confirm('Changing the modulator takes the transmitter off air. Carry on?')) { poll(); return; }
  await api('POST', '/api/dvbs2_engine', {engine});
  poll();
}

// The SDR list: the configured LibreSDR, then whatever the scan found.
// Rebuilt only when what was found changes, so an open list is not shut.
let sdrKey = null;
function fillSdr(S) {
  const sel = document.getElementById('sdr-select');
  const found = S.found || [];
  const key = JSON.stringify([S.chosen, S.uri, found.map(d => d.uri + d.firmware)]);
  if (key === sdrKey || document.activeElement === sel) return;
  sdrKey = key;
  sel.innerHTML = '';
  const add = (value, label) => {
    const o = document.createElement('option');
    o.value = value; o.textContent = label; sel.appendChild(o);
  };
  const label = d => d.model + (d.firmware ? ', ' + d.firmware : '') + ' at ' + d.host;
  const auto = found.find(d => d.uri === S.uri);
  add('', 'Automatic: ' + (auto ? label(auto) : 'none found yet, press Scan'));
  for (const d of found) add(d.uri, label(d));
  if (S.chosen && !found.some(d => d.uri === S.chosen)) add(S.chosen, S.chosen + ' (not answering)');
  sel.value = S.chosen || '';
}

let shownMode = null;
async function setMode(mode) {
  if (cfg && mode === cfg.mode) return;
  if (cfg && cfg.transmitting &&
      !confirm('Changing mode takes the transmitter off air. Carry on?')) return;
  await api('POST', '/api/mode', {mode});
  poll();
}

async function system(action) {
  const what = action === 'restart' ? 'Restart the ROCK' : 'Shut down the ROCK';
  const more = action === 'shutdown'
    ? '\\n\\nIt will stay off until it is powered again: at a remote site that means a visit, or the mains relay.'
    : '\\n\\nThe page comes back in about a minute.';
  if (!confirm(what + '? The transmitter goes off air first.' + more)) return;
  const j = await api('POST', '/api/system/' + action);
  if (j.success) setText('tx-badge', action === 'restart' ? 'RESTARTING' : 'SHUTTING DOWN');
}

function renderUpdate(u) {
  const b = $('version-badge'), apply = $('update-apply-btn');
  b.textContent = (u.version || '?') + (u.branch && u.branch !== 'main' ? ' (' + u.branch + ')' : '');
  apply.style.display = 'none';
  if (u.error) {
    b.style.background = '#b45309'; b.title = u.error;
  } else if (u.behind > 0) {
    b.style.background = '#b45309';
    b.title = u.behind + ' update' + (u.behind > 1 ? 's' : '') + ' available:\\n' + u.commits.join('\\n');
    b.textContent += ': update available';
    apply.style.display = '';
  } else if (u.checked_at) {
    b.style.background = '#1a9850'; b.title = 'Up to date. Last checked ' + u.checked_at;
  }
}

async function checkUpdates() {
  const btn = $('update-check-btn'); btn.disabled = true; btn.textContent = 'Checking...';
  try { renderUpdate(await api('POST', '/api/update/check')); }
  finally { btn.disabled = false; btn.textContent = 'Check updates'; }
}

async function applyUpdate() {
  if (cfg && cfg.transmitting) { alert('Take the transmitter off air first (TX Off), then update.'); return; }
  const list = $('version-badge').title;
  if (!confirm('Update Lynx-Tx now? It restarts, and the page comes back in about 20 seconds.\\n\\n' + list)) return;
  const btn = $('update-apply-btn'); btn.disabled = true; btn.textContent = 'Updating...';
  const j = await api('POST', '/api/update/apply');
  if (!j.success) { btn.disabled = false; btn.innerHTML = '&#x2B06;&#xFE0F; Update'; return; }
  btn.textContent = 'Restarting...';
  setTimeout(function wait() {
    fetch('/api/status').then(r => r.ok ? location.reload() : setTimeout(wait, 2000))
                        .catch(() => setTimeout(wait, 2000));
  }, 5000);
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

// ---- Tweaks ---------------------------------------------------------------
// Both tweak cards edit one set of settings, sent together by Apply - one
// restart on air, not one per control.
const $ = id => document.getElementById(id);
let PRESETS = {}, TESTED = {}, twDirty = false;
const PRESET_HELP = {
  talking: 'Presenters, slides, test cards, stills: finest QP 26 for crisp faces and text, ' +
           'keyframes every 4 s, floor reacts late.',
  balanced: 'The tested all-rounder: finest QP 30, keyframes every 2 s, floor reacts at 30%.',
  action: 'Fast motion, strobes, concerts: finest QP 32 and the floor reacts early, ' +
          'so pans stay smooth at the cost of some softness.',
  custom: 'Custom \u2014 your own mix of finest QP, keyframe interval and floor reaction.',
};
const gopText = g => (g / 25) + ' s';
const pc = v => Math.round(v * 100) + '%';

function currentContent() {
  const q = +$('t-qp').value, g = +$('t-gop').value, t = +$('t-tgt').value;
  for (const [k, p] of Object.entries(PRESETS))
    if (p.qp_floor === q && p.gop === g && Math.abs(p.rc_target - t) < 1e-6) return k;
  return 'custom';
}

function showTweakValues() {
  $('v-look').textContent = (+$('t-look').value).toFixed(1) + ' s';
  $('v-slack').textContent = (+$('t-slack').value).toFixed(2) + ' s';
  $('v-qp').textContent = $('t-qp').value;
  $('v-tgt').textContent = pc(+$('t-tgt').value) + ' of the buffer';
  $('v-aim').textContent = pc(+$('t-aim').value);
  $('v-share').textContent = pc(+$('t-share').value);
  const c = currentContent();
  for (const k of ['talking', 'balanced', 'action'])
    $('c-' + k).className = 'btn btn-sm ' + (k === c ? 'btn-light' : 'btn-outline-light');
  $('c-help').textContent = PRESET_HELP[c];
  const hy = $('t-rc').value === 'hybrid';
  $('hy-only').style.display = hy ? '' : 'none';
  $('cbr-only').style.display = hy ? 'none' : '';
  const capable = cfg && cfg.tweaks && cfg.tweaks.hybrid_capable;
  const rh = $('rc-help');
  if (hy && !capable) {
    rh.innerHTML = '<span style="color:#f87171">The patched encoder plugin is not installed, so ' +
      'Hybrid runs as plain CBR. See patches/ in the repository.</span>';
  } else {
    rh.textContent = hy
      ? 'The encoder keeps its own rate control and adaptive quantisation; a controller with ' +
        'look-ahead raises its QP floor frame by frame only when the receiver\u2019s buffer is threatened.'
      : 'The encoder alone. It overshoots its target by 25\u201340% for seconds at a time, so it ' +
        'needs a low picture share for headroom. For comparison, or a stock plugin.';
  }
}

function fillTweaks(e) {
  $('t-look').value = e.look_ahead;
  $('t-rc').value = e.rate_control === 'cbr' ? 'cbr' : 'hybrid';
  $('t-slack').value = e.slack;
  $('t-qp').value = e.qp_floor;
  if (![...$('t-gop').options].some(o => +o.value === +e.gop))
    $('t-gop').add(new Option(gopText(e.gop), e.gop));
  $('t-gop').value = e.gop;
  $('t-tgt').value = e.rc_target;
  $('t-aim').value = e.hybrid_aim;
  $('t-share').value = e.picture_share;
  $('t-prio').checked = e.sound_priority !== false;
  showTweakValues();
  setDirty(false);
}

function showTested() {
  const T = TESTED;
  $('d-look').textContent = T.look_ahead + ' s';
  $('d-rc').textContent = T.rate_control === 'hybrid' ? 'Hybrid' : 'CBR';
  $('d-slack').textContent = T.slack + ' s';
  $('d-qp').textContent = T.qp_floor;
  $('d-gop').textContent = gopText(T.gop);
  $('d-tgt').textContent = pc(T.rc_target);
  $('d-aim').textContent = pc(T.hybrid_aim);
  $('d-share').textContent = pc(T.picture_share);
}

function setDirty(d) {
  twDirty = d;
  document.querySelectorAll('.apply-btn').forEach(b => b.disabled = !d);
  const note = !d ? '' : (cfg && cfg.transmitting
    ? 'Not applied yet. On air, Apply restarts the transmitter \u2014 a few seconds off air.'
    : 'Not applied yet. Takes effect at the next Start.');
  document.querySelectorAll('.apply-note').forEach(n => n.textContent = note);
}

function tweakChanged() { showTweakValues(); setDirty(true); }

function preset(k) {
  const p = PRESETS[k];
  $('t-qp').value = p.qp_floor; $('t-gop').value = p.gop; $('t-tgt').value = p.rc_target;
  tweakChanged();
}

function readTweaks() {
  return {
    content: currentContent(), rate_control: $('t-rc').value,
    slack: +$('t-slack').value, qp_floor: +$('t-qp').value, gop: +$('t-gop').value,
    rc_target: +$('t-tgt').value, look_ahead: +$('t-look').value,
    hybrid_aim: +$('t-aim').value, picture_share: +$('t-share').value,
    sound_priority: $('t-prio').checked,
  };
}

async function applyTweaks() {
  const j = await api('POST', '/api/tweaks', readTweaks());
  if (j.config) fillTweaks(j.config);
  poll();
}

async function resetTweaks() {
  const j = await api('POST', '/api/tweaks/reset');
  if (j.config) fillTweaks(j.config);
  poll();
}

// Redraw only what changed. The page polls every 2 s; rewriting every
// element (and rebuilding the media list) each time made the whole page
// flicker, and is needless work for the browser.
function setText(id, v) {
  const e = typeof id === 'string' ? document.getElementById(id) : id;
  v = String(v);
  if (e && e.textContent !== v) e.textContent = v;
}
function setStyle(e, k, v) {
  if (typeof e === 'string') e = document.getElementById(e);
  if (e && e.dataset['s_' + k] !== v) { e.dataset['s_' + k] = v; e.style[k] = v; }
}
function setAttr(e, k, v) {
  if (typeof e === 'string') e = document.getElementById(e);
  if (e && e.getAttribute(k) !== v) e.setAttribute(k, v);
}
function setClass(e, v) {
  if (typeof e === 'string') e = document.getElementById(e);
  if (e && e.className !== v) e.className = v;
}

// Processor temperature and fan, as on the Lynx Receiver: "38°C fan 50%".
// Amber from 60 C (where the kernel starts stepping the fan), red from 80 C
// (the CPU throttles at 85), red with STALLED if a fan with a speed sensor
// is asked to run but reports 0 rpm.
function renderBoardBadge(b) {
  const el = document.getElementById('board-badge');
  if (!el) return;
  if (!b || b.temp_c === null || b.temp_c === undefined) { el.style.display = 'none'; return; }
  el.style.display = '';
  const deg = String.fromCharCode(176);
  let stalled = false;
  let txt = Math.round(b.temp_c) + deg + 'C';
  let tip = 'Processor ' + b.temp_c + deg + 'C (' + b.sensor + ')';
  if (b.fan_pct !== null && b.fan_pct !== undefined) {
    txt += b.fan_pct > 0 ? ' fan ' + b.fan_pct + '%' : ' fan off';
    tip += '. Fan ' + (b.fan_pct > 0 ? b.fan_pct + '% (PWM ' + b.fan_pwm + ')' : 'off');
    if (b.fan_pct > 0 && b.fan_rpm !== null && b.fan_rpm !== undefined) {
      if (b.fan_rpm === 0) { stalled = true; txt += ' STALLED'; tip += ', but not turning (0 rpm)'; }
      else { txt += ' ' + b.fan_rpm + 'rpm'; tip += ', ' + b.fan_rpm + ' rpm'; }
    }
  }
  tip += '. Held at 40' + deg + 'C by lynx-fan.';
  el.textContent = txt;
  el.title = tip;
  el.style.background = (stalled || b.temp_c >= 80) ? '#b91c1c' : b.temp_c >= 60 ? '#d97706' : '#3a4a63';
}

async function poll() {
  try {
    cfg = await (await fetch('/api/status')).json();
  } catch (e) { return; }

  // Muted by the SDR: on air as far as Lynx-Tx knows, but the board has
  // turned its transmitter off (its IQ stopped). Fault: the chain stopped
  // by itself. Both clear at the next TX On.
  const A = cfg.alarm || {};
  const txText = A.locked_out ? 'LOCKOUT' : A.muted ? 'MUTED BY THE SDR'
               : (!cfg.transmitting && A.fault) ? 'FAULT'
               : cfg.transmitting ? 'ON AIR' : 'IDLE';
  setText('tx-badge', txText);
  // Whichever state the transmitter is in is lit: green on air, red off.
  setClass('btn-txon', 'btn big-btn ' + (cfg.transmitting ? 'btn-success' : 'btn-secondary'));
  setClass('btn-txoff', 'btn big-btn ' + (cfg.transmitting ? 'btn-secondary' : 'btn-danger'));
  setStyle('tx-badge', 'background', A.locked_out ? '#b91c1c'
           : (A.muted || (!cfg.transmitting && A.fault)) ? '#d97706'
           : cfg.transmitting ? '#16a34a' : '#3a4a63');
  setAttr('tx-badge', 'title', A.locked_out ? 'Lockout: auto-recover gave up after repeated faults. Find the cause, then press TX On.'
           : A.recovering ? 'Auto-recover: ' + A.recovering
           : A.muted ? 'The SDR stopped receiving IQ and muted itself. Auto-recover restarts it once the SDR has been back for 30 s; or press TX Off, then TX On.'
           : A.fault ? 'The transmit chain stopped by itself: ' + A.fault + '. Auto-recover will restart it, or press TX On.' : '');
  setText('call-badge', cfg.config.callsign);

  const L = cfg.libre || {};
  const AT = cfg.mode === 'atsc3';
  // Mode: the switch, which Modulation controls show, and their values.
  for (const md of ['dvbs2', 'atsc3'])
    setClass('mode-' + md, 'btn ' + (md === cfg.mode ? 'btn-primary' : 'btn-outline-light'));
  setStyle('mod-atsc3', 'display', AT ? '' : 'none');
  setStyle('mod-dvbs2', 'display', AT ? 'none' : '');
  if (shownMode !== cfg.mode) {
    if (!firstLoad) fillMod(cfg.config, cfg.mode);
    shownMode = cfg.mode;
  }
  // IQ: the SDR is a plain transmitter (ATSC 3.0, or DVB-S2 from the ROCK).
  const IQ = !!cfg.iq, S = cfg.sdr || {};
  setText('m-power-note', IQ
    ? 'Transmit attenuation in the SDR: 0 dB is full output. TX Off sets the most attenuation it has (−89.75 dB).'
    : 'Below −40 dB this firmware drops PTT, which is how TX Off leaves the carrier off.');
  if (AT) setText('a-tsrate', (cfg.ts_bitrate/1e6).toFixed(3) + ' Mb/s');

  // The SDR: the list, and which modulator can drive it.
  fillSdr(S);
  const me = document.getElementById('m-engine');
  if (document.activeElement !== me) me.value = cfg.dvbs2_engine;
  me.options[1].disabled = !S.is_libre;
  setStyle('m-rock-only', 'display', (!AT && IQ) ? '' : 'none');
  if (!AT && IQ) {
    setText('m-tsrate', (cfg.ts_bitrate/1e6).toFixed(3) + ' Mb/s');
    setText('m-dac', S.dac_rate ? (S.dac_rate/1e6).toFixed(3) + ' MS/s' : '—');
  }
  const here = (S.found || []).find(d => d.uri === S.uri);
  const name = here ? here.model.split(' (')[0] : (S.is_libre ? 'LibreSDR' : 'SDR');
  // The list already names the board in use; this line only speaks up
  // when something is wrong.
  // A PlutoSky being prepared for Lynx-Tx (tezuka_prep.py) says so here.
  const P = S.prep;
  setText('sdr-info', (P && P.state !== 'ready') ? 'SDR: ' + P.detail
    : here ? ''
    : 'Using ' + S.uri + (S.last_scan ? ' (not found in the last scan)' : ''));
  // 10 MHz reference: green when the SDR's loop is locked, red when it is
  // not (no lock, or no 10 MHz feed), grey when the board cannot say
  // (DL1JM's firmware has no way to report it). Always shown, so its
  // absence is never mistaken for an answer.
  const R = S.ref;
  setText('ref-badge', R ? '10 MHz Reference ' + (R.locked ? 'Locked' : 'Unlocked')
                         : '10 MHz Reference: not reported');
  setStyle('ref-badge', 'background', R ? (R.locked ? '#16a34a' : '#b91c1c') : '#3a4a63');
  setStyle('ref-badge', 'display', '');

  // Alarm lamps, always shown: the SDR (keyed and fed, muted itself, not
  // answering, or idle) and the HDMI input as the mixer sees it.
  const AL = cfg.alarm || {};
  const sdrLamp = !cfg.transmitting ? ['SDR idle', '#3a4a63', 'Off air.']
    : AL.muted ? [AL.recovering ? 'SDR MUTED: ' + AL.recovering.replace('SDR back: ', '') : 'SDR MUTED', '#d97706',
                  'The SDR stopped receiving IQ and muted itself. Auto-recover restarts it once the SDR has been back for 30 s.']
    : AL.sdr_ok === false ? ['SDR NOT REACHED', '#b91c1c', 'Lynx-Tx cannot reach the SDR.']
    : ['SDR OK', '#16a34a', 'On air: the SDR is keyed and taking IQ.'];
  setText('sdr-badge', sdrLamp[0]); setStyle('sdr-badge', 'background', sdrLamp[1]);
  setAttr('sdr-badge', 'title', sdrLamp[2]);
  const camState = (cfg.switcher || {}).camera;
  const hdmiLamp = camState === 'live' ? ['HDMI OK', '#16a34a', 'Camera input live.']
    : camState === 'unstable' ? ['HDMI UNSTABLE', '#d97706', 'The HDMI signal keeps dropping: check the lead or restart the source. Colour bars meanwhile.']
    : camState === 'bars' ? ['HDMI: no signal', '#3a4a63', 'No usable HDMI signal: the Camera input shows colour bars.']
    : ['HDMI ?', '#3a4a63', 'The mixer is not reporting.'];
  setText('hdmi-badge', hdmiLamp[0]); setStyle('hdmi-badge', 'background', hdmiLamp[1]);
  setAttr('hdmi-badge', 'title', hdmiLamp[2]);
  renderBoardBadge(cfg.board);

  // As an IQ transmitter DL1JM's modulator is stopped on purpose (or absent),
  // so "idle" would read as a fault: the badge shows the SDR instead.
  if (IQ) {
    setText('libre-badge', here ? name + ' IQ' : name + ' not found');
    setStyle('libre-badge', 'background', here || cfg.transmitting ? '#16a34a' : '#b91c1c');
  } else {
    setText('libre-badge', !L.reachable ? 'LibreSDR unreachable'
      : (L.modulator_running ? 'LibreSDR ready' : 'LibreSDR idle'));
    setStyle('libre-badge', 'background', !L.reachable ? '#b91c1c'
      : L.modulator_running ? '#16a34a' : '#3a4a63');
  }

  setText('s-ts', (cfg.ts_bitrate/1e6).toFixed(3) + ' Mb/s' + (AT ? ' (ATSC 3.0)' : IQ ? ' (DVB-S2 from the ROCK)' : ''));
  setText('s-want', IQ ? (cfg.ts_bitrate/1e6).toFixed(3) + ' Mb/s'
    : (L.ts_bitrate ? (L.ts_bitrate/1e6).toFixed(3) + ' Mb/s' : '—'));
  setText('s-under', IQ ? '—' : (L.underflows === null || L.underflows === undefined) ? '—' : L.underflows);
  setStyle('s-under', 'color', (!IQ && L.underflows) ? '#f87171' : '#4ade80');
  setText('s-up', cfg.transmitting ? cfg.uptime + ' s' : '—');

  const h = cfg.hdmi || {};
  setText('hdmi-info', h.width ? `HDMI in: ${h.width}×${h.height}` : 'HDMI in: nothing detected');

  // Programme bus: red = on air, blue = what starts at key-up.
  const SW = cfg.switcher, onAir = !!(cfg.transmitting && SW);
  const pgm = onAir ? SW.on : (cfg.config.source.program ||
              (cfg.config.source.kind === 'camera' ? 'cam' : 'med'));
  for (const b of ['cam', 'med'])
    setClass('pgm-' + b, 'btn btn-lg flex-fill ' +
      (b === pgm ? (onAir ? 'btn-danger' : 'btn-primary') : 'btn-outline-light'));
  if (onAir) {
    let t = 'ON AIR: ' + (SW.on === 'cam' ? 'Camera' : 'Media');
    if (SW.camera === 'bars') t += ' · no HDMI signal — Camera shows colour bars';
    if (SW.camera === 'unstable') t += ' · HDMI signal UNSTABLE — Camera shows colour bars; check the lead or restart the source';
    if ((SW.media_state || '').startsWith('error')) t += ' · media ' + SW.media_state;
    setText('pgm-info', t); setStyle('pgm-info', 'color', '#f87171');
  } else {
    const cam = SW && SW.camera === 'unstable' ? ' · HDMI signal UNSTABLE: check the lead or restart the source'
              : SW && SW.camera === 'bars' ? ' · no HDMI signal' : '';
    setText('pgm-info', (cfg.transmitting ? 'Single-source mode' : 'Off air — starts on ' +
      (pgm === 'cam' ? 'Camera' : 'Media')) + cam);
    setStyle('pgm-info', 'color', (SW && SW.camera === 'unstable') ? '#f59e0b' : '#9fb3d1');
  }
  showMedia();

  // In switcher mode the programme follows the HDMI input, as the camera does.
  const isCam = cfg.use_switcher || cfg.config.source.kind === 'camera';
  setStyle('camera-format', 'display', isCam ? '' : 'none');
  setStyle('media-format', 'display', isCam ? 'none' : '');
  if (isCam) {
    const hf = cfg.hdmi || {};
    setText('camera-format-value', hf.width ? hf.width + '×' + hf.height : 'nothing detected');
  }

  const LP = cfg.libre_patch || {};
  // Buffer: how far ahead of need the pictures are sent.
  const B = cfg.buffer;
  drawBuffer(B, Number((cfg.config.encoder || {}).slack) || 0.6);
  setStyle('buffer-line', 'display', B ? 'block' : 'none');
  if (B) {
    const lo = B.min_60s ?? B.slack;
    const tr = cfg.switcher && cfg.switcher.trim ? ' · trimming −' + cfg.switcher.trim + '%' : '';
    setText('buffer-line', 'Buffer: ' + B.slack.toFixed(2) + ' s' + tr + '  (lowest in the last minute ' +
      lo.toFixed(2) + ' s' + (B.late ? ', ' + B.late + ' late frames since key-up' : '') + ')');
    setStyle('buffer-line', 'color', lo <= 0 ? '#f87171' : lo < 0.5 ? '#fbbf24' : '#4ade80');
  }
  setStyle('libre-patch', 'display', IQ ? 'none' : '');
  setStyle('libre-check', 'display', IQ ? 'none' : '');
  setText('libre-patch', 'LibreSDR script: ' + (LP.detail || '—'));
  setStyle('libre-patch', 'color', {ok: '#4ade80', rebooting: '#e8a33d', pending: '#e8a33d',
                                    unknown: '#9fb3d1'}[LP.state] || '#f87171');
  const N = cfg.nit || {};
  setText('i-nit-status', AT
    ? 'Not sent in ATSC 3.0 mode: the NIT, network ID and LCN are DVB tables an ATSC 3.0 receiver ignores. Names, IDs and PIDs still apply.'
    : 'Last build: ' + (N.detail || '—'));
  setStyle('i-nit-status', 'color', (!AT && N.ok) ? '#4ade80' : '#9fb3d1');

  const srw = document.getElementById('sr-warning'), hide = IQ || cfg.config.modulation.symbol_rate_ks <= 2000;
  if (srw.hidden !== hide) srw.hidden = hide;

  if (firstLoad) {
    const m = cfg.config.modulation, e = cfg.config.encoder;
    fillIdent(cfg.config.identity, cfg.config);
    fillMod(cfg.config, cfg.mode);
    document.getElementById('e-format').value = e.width + 'x' + e.height;
    document.getElementById('e-fps').value = e.fps;
    document.getElementById('e-audio').checked = e.audio;
    document.getElementById('e-abr').value = e.audio_bitrate;
    PRESETS = cfg.tweaks.presets; TESTED = cfg.tweaks.tested;
    showTested();
    fillTweaks(e);
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
        W = c.clientWidth * dpr, H = 150 * dpr, x = c.getContext('2d');
  if (c.width !== W) c.width = W;
  if (c.height !== H) c.height = H;
  const L = 34 * dpr, Bm = 14 * dpr, T = 4 * dpr;                       // margins for the labels
  const bottom = -target * 0.35;                                         // room below zero for late frames
  const Y = v => T + (top - Math.max(bottom, Math.min(top, v))) / (top - bottom) * (H - T - Bm);
  x.clearRect(0, 0, W, H);
  // The under-run zone: below zero, a picture arrives after it is needed.
  x.fillStyle = 'rgba(185, 28, 28, 0.22)';
  x.fillRect(L, Y(0), W - L, Y(bottom) - Y(0));
  x.font = (10 * dpr) + 'px system-ui'; x.fillStyle = '#9fb3d1'; x.textBaseline = 'middle';
  x.textAlign = 'right';
  for (let v = 0; v <= top + 1e-9; v += step) {                          // seconds axis
    x.fillText(v.toFixed(1), L - 4 * dpr, Y(v));
    x.strokeStyle = v === 0 ? '#b91c1c' : '#1f2b44'; x.lineWidth = dpr;
    x.beginPath(); x.moveTo(L, Y(v)); x.lineTo(W, Y(v)); x.stroke();
  }
  x.fillStyle = '#f87171'; x.fillText('late', L - 4 * dpr, Y(bottom * 0.5)); x.fillStyle = '#9fb3d1';
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
api('GET', '/api/update/status').then(renderUpdate);
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
    threading.Thread(target=sdr_scan_watch, daemon=True).start()
    threading.Thread(target=sdr_ref_watch, daemon=True).start()
    threading.Thread(target=mixer_watch, daemon=True).start()
    threading.Thread(target=tx_alarm_watch, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
