"""Preparing an SDR on Tezuka (PlutoSky R2 or LibreSDR) for Lynx-Tx, with
nobody at a keyboard.

A punter flashes Tezuka onto a card, plugs the board into the ROCK and powers
it. Lynx-Tx then does the rest, off air only:

  1. its SSH key: Tezuka's root password is the factory "analog", used once
     (sshpass) to put the ROCK's key in the board's flash (/mnt/jffs2), from
     where tools/tezuka-autorun.sh restores it at every boot;
  2. tools/tezuka-autorun.sh as /mnt/jffs2/autorun.sh - quiet mode, the
     10 MHz reference supervisor and the starvation mute (see that file) -
     compared by checksum, so a newer Lynx-Tx updates it by itself;
  3. a reboot of the board to start it (about a minute).

Only boards on the direct link, or the one chosen on the page: a board on
the LAN may be somebody else's, and is never touched uninvited.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "tools" / "tezuka-autorun.sh"
FACTORY_PASSWORD = "analog"
KEY = Path.home() / ".ssh" / "id_ed25519"
# Tezuka makes a new host key at every boot: never remember it.
OPTS = ["-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]

state: dict = {}          # host -> {"state", "detail", "t"}
_lock = threading.Lock()


def _set(host: str, st: str, detail: str):
    state[host] = {"state": st, "detail": detail, "t": time.time()}
    print(f"[prep] {host}: {st} - {detail}")


def _ssh(host: str, cmd: str, stdin: bytes | None = None, password: bool = False,
         timeout: int = 20) -> subprocess.CompletedProcess:
    if password:
        args = ["sshpass", "-p", FACTORY_PASSWORD, "ssh", *OPTS,
                "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
    else:
        args = ["ssh", *OPTS, "-o", "BatchMode=yes"]
    return subprocess.run(args + [f"root@{host}", cmd], input=stdin,
                          capture_output=True, timeout=timeout)


def _ensure_local_key() -> bool:
    if KEY.exists() and KEY.with_suffix(".pub").exists():
        return True
    KEY.parent.mkdir(mode=0o700, exist_ok=True)
    r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(KEY)],
                       capture_output=True)
    return r.returncode == 0


def _ensure_key(host: str) -> bool:
    """Key login works, or is made to work with the factory password."""
    if _ssh(host, "true").returncode == 0:
        return True
    if not shutil.which("sshpass"):
        _set(host, "error", "sshpass is not installed (sudo apt install sshpass)")
        return False
    if not _ensure_local_key():
        _set(host, "error", "could not make the ROCK's SSH key")
        return False
    pub = KEY.with_suffix(".pub").read_bytes()
    r = _ssh(host, "cat > /mnt/jffs2/authorized_keys && mkdir -p /root/.ssh && "
                   "cp /mnt/jffs2/authorized_keys /root/.ssh/authorized_keys && "
                   "chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys",
             stdin=pub, password=True)
    if r.returncode != 0:
        _set(host, "error", "the board refused the factory password - "
                            "set up its SSH key by hand (Installation Guide, Section 11.5)")
        return False
    return _ssh(host, "true").returncode == 0


MIN_TEZUKA = (0, 3, 24)


def tezuka_new_enough(version: str):
    """True or False for a Tezuka version such as "v0.3.24"; None if it
    cannot be read."""
    v = version.strip().lstrip("vV").split("-")[0]
    try:
        parts = tuple(int(x) for x in v.split("."))
    except ValueError:
        return None
    if not parts:
        return None
    return parts + (0,) * (3 - len(parts)) >= MIN_TEZUKA


def prepare(host: str, on_air) -> str:
    """Bring one board (PlutoSky R2 or LibreSDR, on Tezuka) up to date.
    on_air() is checked before anything is written; nothing is ever done to
    a board while transmitting."""
    if not SCRIPT.is_file():
        return "no-script"
    with _lock:
        cur = state.get(host)
        if cur and cur["state"] == "rebooting" and time.time() - cur["t"] < 120:
            return "rebooting"
        try:
            if on_air() or not _ensure_key(host):
                return state.get(host, {}).get("state", "busy")
            # v0.3.24 or later, read from the board's own release file. The
            # reference controls (the PlutoSky R2's S22refclk, the LibreSDR's
            # gpsdo_boot.sh) used to stand in for the version, but not every
            # board has one: Ian G8XZD's FISHBall PlutoSky on v0.3.24 has no
            # 10 MHz reference hardware and was refused as "too old" (14 Nov
            # 2026). Such a board runs on its own crystal (autorun.sh: board
            # "none"). The old test stays as a fallback for a release file
            # with no version in it.
            r = _ssh(host, "sed -n 's/^VERSION=//p' /etc/os-release 2>/dev/null | tr -d '\"' | head -1; "
                           "{ [ -x /etc/init.d/S22refclk ] || [ -x /usr/sbin/gpsdo_boot.sh ]; } "
                           "&& echo ok || echo -; sha256sum /mnt/jffs2/autorun.sh 2>/dev/null")
            lines = r.stdout.decode(errors="replace").splitlines()
            version = lines[0].strip() if lines else ""
            refclk = len(lines) > 1 and lines[1].strip() == "ok"
            sums = lines[2].split() if len(lines) > 2 else []
            ok = tezuka_new_enough(version)
            if ok is None:
                ok = refclk
            if not ok:
                _set(host, "error", f"Tezuka is too old for Lynx-Tx ({version or 'version unknown'}): "
                                    "v0.3.24 or later needed")
                return "old"
            out = ["ok"] + sums[:1]
            want = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
            if len(out) > 1 and out[1] == want:
                if not cur or cur["state"] != "ready":
                    _set(host, "ready", "prepared for Lynx-Tx")
                return "ready"
            if on_air():
                return "busy"
            r = _ssh(host, "cat > /mnt/jffs2/autorun.sh && sync && sha256sum /mnt/jffs2/autorun.sh",
                     stdin=SCRIPT.read_bytes())
            got = r.stdout.decode(errors="replace").split()
            if r.returncode != 0 or not got or got[0] != want:
                _set(host, "error", "could not write the Lynx-Tx script to the board")
                return "error"
            _ssh(host, "(sleep 1; reboot) >/dev/null 2>&1 &", timeout=10)
            _set(host, "rebooting", "Lynx-Tx script installed - the SDR is restarting "
                                    "(about a minute)")
            return "rebooting"
        except (subprocess.TimeoutExpired, OSError) as e:
            _set(host, "error", f"could not reach the board: {e}")
            return "error"


def status(host: str) -> dict | None:
    return state.get(host)
