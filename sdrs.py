"""Finding the SDRs Lynx-Tx can transmit through.

Any Pluto-class board running iiod will do: the PlutoSky R2 and the LibreSDR
(both proven 18 Oct 2026), an ADALM-Pluto, a spare. Each is asked for its
own model and firmware - the IIO context attributes - so the page can say
"PlutoSky Rev.2, tezuka-v0.3.5, 192.168.0.117" and nobody keeps a table of
boards up to date by hand.

Where to look, in order:
  1. the direct link: the ROCK's second Ethernet port, SDR at 192.168.254.2
     (the supported way to connect one - see README.md);
  2. addresses listed in the config (sdr.hosts), for an SDR on the LAN;
  3. whatever libiio's own network scan (mDNS) turns up.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

PROBE_TIMEOUT = 4      # seconds per board; an absent address costs this once
SCAN_TIMEOUT = 6

_lock = threading.Lock()
_found: dict = {}      # uri -> description, last good probe
_last_scan = 0.0


def _attrs(text: str) -> dict:
    """'hw_model: PlutoSky Rev.2 (Z7020/AD9361)' lines -> a dict."""
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            if re.fullmatch(r"[A-Za-z0-9_,.-]+", k):
                out[k] = v.strip()
    return out


def kind_of(model: str) -> str:
    m = model.lower()
    if "plutosky" in m:
        return "plutosky"
    if "libre" in m:
        return "libresdr"
    if "pluto" in m:
        return "pluto"
    return "other"


def probe(uri: str) -> dict | None:
    """Ask one board who it is. None if nothing answers as an IIO device
    with an AD936x on it."""
    if not shutil.which("iio_attr"):
        return None
    try:
        r = subprocess.run(["iio_attr", "-u", uri, "-C"], capture_output=True,
                           text=True, timeout=PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    a = _attrs(r.stdout)
    model = a.get("hw_model") or a.get("hw_model_variant") or "Unknown SDR"
    fw = a.get("fw_version") or a.get("local,kernel") or ""
    host = uri.split(":", 1)[1] if uri.startswith("ip:") else uri
    return {
        "uri": uri, "host": host, "model": model, "firmware": fw,
        "kind": kind_of(model), "tezuka": "tezuka" in fw.lower(),
        "serial": a.get("hw_serial", ""), "seen": time.time(),
    }


def _mdns_uris() -> list:
    """libiio's network scan, best effort: '... [ip:pluto.local]' lines."""
    if not shutil.which("iio_info"):
        return []
    try:
        r = subprocess.run(["iio_info", "-S", "ip"], capture_output=True,
                           text=True, timeout=SCAN_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return sorted(set(re.findall(r"\[(ip:[^\]\s]+)\]", r.stdout)))


def scan(hosts: list) -> list:
    """Probe the given hosts plus anything mDNS finds; return what answered,
    the given hosts first, in their order."""
    global _last_scan
    uris = [h if ":" in h else f"ip:{h}" for h in hosts if h]
    for u in _mdns_uris():
        if u not in uris:
            uris.append(u)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe, uris))
    good = [r for r in results if r]
    # The same board can answer at an mDNS name and an address: keep one.
    seen, unique = set(), []
    for r in good:
        key = r["serial"] or r["uri"]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    with _lock:
        _found.clear()
        _found.update({r["uri"]: r for r in unique})
        _last_scan = time.time()
    return unique


def found() -> list:
    with _lock:
        return list(_found.values())


def last_scan() -> float:
    return _last_scan


def describe(d: dict) -> str:
    fw = f", {d['firmware']}" if d.get("firmware") else ""
    return f"{d['model']}{fw} at {d['host']}"
