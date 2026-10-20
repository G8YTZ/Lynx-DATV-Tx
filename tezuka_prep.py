"""Preparing a PlutoSky on Tezuka for Lynx-Tx, with nobody at a keyboard.

A punter flashes Tezuka onto a card, plugs the board into the ROCK and powers
it. Lynx-Tx then does the rest, off air only:

  1. its SSH key: Tezuka's root password is the factory "analog", used once
     (sshpass) to put the ROCK's key in the board's flash (/mnt/jffs2), from
     where tools/plutosky-autorun.sh restores it at every boot;
  2. tools/plutosky-autorun.sh as /mnt/jffs2/autorun.sh - quiet mode, the
     10 MHz reference supervisor and the starvation mute (see that file) -
     compared by checksum, so a newer Lynx-Tx updates it by itself;
  3. a reboot of the board to start it (about a minute).

Only boards on the direct link, or the one chosen on the page: a PlutoSky on
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
SCRIPT = APP_DIR / "tools" / "plutosky-autorun.sh"
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
                            "set up its SSH key by hand (Installation Guide, Section 11.4)")
        return False
    return _ssh(host, "true").returncode == 0


def prepare(host: str, on_air) -> str:
    """Bring one PlutoSky up to date. on_air() is checked before anything is
    written; nothing is ever done to a board while transmitting."""
    if not SCRIPT.is_file():
        return "no-script"
    with _lock:
        cur = state.get(host)
        if cur and cur["state"] == "rebooting" and time.time() - cur["t"] < 120:
            return "rebooting"
        try:
            if on_air() or not _ensure_key(host):
                return state.get(host, {}).get("state", "busy")
            r = _ssh(host, "[ -x /etc/init.d/S22refclk ] && echo ok; "
                           "sha256sum /mnt/jffs2/autorun.sh 2>/dev/null")
            out = r.stdout.decode(errors="replace").split()
            if not out or out[0] != "ok":
                _set(host, "error", "Tezuka is too old for Lynx-Tx: v0.3.24 or later needed")
                return "old"
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
