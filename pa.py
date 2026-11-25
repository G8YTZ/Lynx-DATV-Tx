"""PA power sequencing over Modbus TCP (25 Nov 2026).

The PA's DC is switched by relays on a Modbus relay board (a ComWinTop
controller at GB3OO and GB3JT, the same one Home Assistant reads). Lynx-Tx
drives them so the drive and the PA's supply always come and go in the
right order:

  TX On:  relays on in list order (driver, then PA), wait for the PA to
          settle, and only then is the SDR's power brought up (ramped).
  TX Off: SDR muted first, a short pause, then relays off in reverse order.

So the first amplifier never sees drive with its supply missing. The
config, under "pa" in lynx_tx.yaml:

  pa:
    enabled: true
    host: 192.168.40.75      # the Modbus TCP gateway
    port: 502
    unit: 1                  # slave id
    coils:                   # switched on in this order, off in reverse
      - {name: Drive, coil: 0}
      - {name: PA, coil: 1}
    on_gap_s: 0.3            # between one relay and the next
    settle_s: 1.0            # after the last relay on, before any drive
    off_delay_s: 0.3         # after the mute, before the first relay off
    ramp_s: 1.0              # the SDR's power brought up over this long

Plain Modbus TCP, functions 1 (read coils) and 5 (write a coil), so no
extra package is needed. Each call opens its own short connection, which
the gateway shares happily with Home Assistant's polling.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

DEFAULTS = {
    "enabled": False,
    "host": "",
    "port": 502,
    "unit": 1,
    "coils": [{"name": "Drive", "coil": 0}, {"name": "PA", "coil": 1}],
    "on_gap_s": 0.3,
    "settle_s": 1.0,
    "off_delay_s": 0.3,
    "ramp_s": 1.0,                # SDR power ramp at key-up (any SDR in IQ mode)
    "require_confirm": True,      # no drive unless the relays read back as on
    "timeout_s": 2.0,
    "retries": 3,
}

_lock = threading.Lock()          # one sequence at a time
_tid = 0
state = {"coils": {}, "ok": None, "error": "", "checked": 0.0, "last": ""}


def cfg(config: dict) -> dict:
    c = {**DEFAULTS, **(config.get("pa") or {})}
    c["coils"] = [{"name": str(x.get("name", f"coil {x.get('coil')}")), "coil": int(x["coil"])}
                  for x in (c.get("coils") or [])]
    return c


def enabled(config: dict) -> bool:
    c = cfg(config)
    return bool(c["enabled"] and c["host"] and c["coils"])


# ── Modbus TCP, the two functions needed ──────────────────────────────

def _request(c: dict, pdu: bytes) -> bytes:
    """Send one request, return the reply's PDU. Retries on any failure."""
    global _tid
    err = None
    for _ in range(max(1, int(c["retries"]))):
        _tid = (_tid + 1) & 0xFFFF
        frame = struct.pack(">HHHB", _tid, 0, len(pdu) + 1, int(c["unit"])) + pdu
        try:
            with socket.create_connection((c["host"], int(c["port"])),
                                          timeout=float(c["timeout_s"])) as s:
                s.settimeout(float(c["timeout_s"]))
                s.sendall(frame)
                head = _recv(s, 7)
                tid, _, length, _ = struct.unpack(">HHHB", head)
                body = _recv(s, length - 1)
            if tid != _tid:
                raise IOError("reply to a different request")
            if body[0] & 0x80:
                raise IOError(f"Modbus exception {body[1]}")
            return body
        except (OSError, IOError, struct.error, IndexError) as e:
            err = e
            time.sleep(0.2)
    raise IOError(f"{c['host']}:{c['port']}: {err}")


def _recv(s: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            raise IOError("connection closed")
        data += chunk
    return data


def write_coil(c: dict, coil: int, on: bool):
    pdu = struct.pack(">BHH", 5, coil, 0xFF00 if on else 0x0000)
    reply = _request(c, pdu)
    if reply != pdu:
        raise IOError(f"coil {coil}: unexpected reply")


def read_coils(c: dict, first: int, count: int) -> list:
    reply = _request(c, struct.pack(">BHH", 1, first, count))
    bits = reply[2:2 + reply[1]]
    return [bool(bits[i // 8] >> (i % 8) & 1) for i in range(count)]


# ── The sequences ──────────────────────────────────────────────────────

def refresh(config: dict) -> dict:
    """Read the relays' state for the page. Never raises."""
    if not enabled(config):
        state.update(ok=None, coils={}, error="")
        return state
    c = cfg(config)
    nums = [x["coil"] for x in c["coils"]]
    try:
        bits = read_coils(c, min(nums), max(nums) - min(nums) + 1)
        state["coils"] = {x["name"]: bits[x["coil"] - min(nums)] for x in c["coils"]}
        state.update(ok=True, error="")
    except Exception as e:
        state.update(ok=False, error=str(e))
    state["checked"] = time.time()
    return state


def all_on(config: dict) -> bool:
    s = refresh(config)
    return bool(s["ok"]) and bool(s["coils"]) and all(s["coils"].values())


def power_up(config: dict) -> str:
    """Relays on in order, then wait for the PA to settle. Skips the wait
    when they were already on (a restart, auto-recover). Returns "" or the
    reason it could not."""
    if not enabled(config):
        return ""
    c = cfg(config)
    with _lock:
        try:
            if all_on(config):
                return ""
            for i, x in enumerate(c["coils"]):
                write_coil(c, x["coil"], True)
                print(f"[pa] {x['name']} on")
                if i < len(c["coils"]) - 1:
                    time.sleep(float(c["on_gap_s"]))
            time.sleep(float(c["settle_s"]))
            state["last"] = "on"
            # Read the relays back: the drive only comes up once the gateway
            # confirms every one of them is on.
            if not all_on(config):
                off = [k for k, v in state["coils"].items() if not v]
                return ("the relays did not confirm: " + ", ".join(off) + " still off"
                        if state["ok"] else f"no read-back: {state['error']}")
            return ""
        except Exception as e:
            state.update(ok=False, error=str(e))
            print(f"[pa] power up failed: {e}")
            return str(e)


def power_down(config: dict, already_muted: bool = True) -> str:
    """Relays off in reverse order. Call only once the drive has gone."""
    if not enabled(config):
        return ""
    c = cfg(config)
    with _lock:
        try:
            if already_muted:
                time.sleep(float(c["off_delay_s"]))
            for i, x in enumerate(reversed(c["coils"])):
                write_coil(c, x["coil"], False)
                print(f"[pa] {x['name']} off")
                if i < len(c["coils"]) - 1:
                    time.sleep(float(c["on_gap_s"]))
            state["last"] = "off"
            refresh(config)
            return ""
        except Exception as e:
            state.update(ok=False, error=str(e))
            print(f"[pa] power down failed: {e}")
            return str(e)
