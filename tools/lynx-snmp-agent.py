#!/usr/bin/env python3
"""Lynx-Tx's SNMP agent: serves mibs/LYNX-MIB.txt for snmpd (pass_persist).

snmpd starts this once and talks to it on stdin/stdout. The values come from
Lynx-Tx's own /api/health (cached 5 s), so a monitoring system polling every
few seconds never reaches the transmitter itself. If Lynx-Tx does not answer,
lynxTxState reads fault(4) and everything else unknown: an alarm, as it
should be.

/etc/snmp/snmpd.conf.d/lynx.conf (install.sh writes it):
  pass_persist .1.3.6.1.4.1.8072.9999.1.1 /usr/bin/python3 /usr/local/lib/lynx-tx/lynx-snmp-agent.py

Until the repeater group has its own Private Enterprise Number, this lives on
Net-SNMP's test branch (netSnmpPlaypen, .1.3.6.1.4.1.8072.9999); change BASE
and the MIB together when it arrives.
"""
import json
import sys
import time
import urllib.request

BASE = ".1.3.6.1.4.1.8072.9999.1.1"          # lynxTx
URL = "http://127.0.0.1:8080/api/health"

# column: (health key, pass_persist type, value when Lynx-Tx is down)
OBJECTS = {
    1: ("txState", "integer", 4),
    2: ("sdrReachable", "integer", 2),
    3: ("refState", "integer", 5),
    4: ("mixerRunning", "integer", 2),
    5: ("hdmiSignal", "integer", 2),
    6: ("frequencyKHz", "gauge", 0),
    7: ("powerCentiDb", "integer", 0),
    8: ("mode", "string", "unknown"),
    9: ("sdrMutes", "counter", 0),
    10: ("memAnonMB", "gauge", 0),
    11: ("cmaFreeMB", "gauge", 0),
    12: ("cpuTempDeciC", "integer", 0),
    13: ("cameraState", "integer", 4),
    14: ("autoRecoveries", "counter", 0),
}
OIDS = [f"{BASE}.{n}.0" for n in sorted(OBJECTS)]

_cache = {"t": 0.0, "v": None}


def health():
    if time.time() - _cache["t"] > 5:
        try:
            with urllib.request.urlopen(URL, timeout=3) as r:
                _cache["v"] = json.loads(r.read())
        except Exception:
            _cache["v"] = None
        _cache["t"] = time.time()
    return _cache["v"]


def value(oid):
    n = int(oid[len(BASE) + 1:].split(".")[0])
    key, typ, down = OBJECTS[n]
    h = health()
    v = down if h is None else h.get(key, down)
    if typ == "counter":
        v = int(v) % (1 << 32)
    elif typ == "gauge":
        v = max(0, int(v))
    return typ, v


def oid_key(oid):
    return [int(x) for x in oid.strip(".").split(".")]


def reply(*lines):
    sys.stdout.write("\n".join(str(x) for x in lines) + "\n")
    sys.stdout.flush()


def main():
    while True:
        cmd = sys.stdin.readline()
        if not cmd:
            return
        cmd = cmd.strip().lower()
        if cmd == "ping":
            reply("PONG")
        elif cmd in ("get", "getnext"):
            oid = sys.stdin.readline().strip()
            if not oid.startswith("."):
                oid = "." + oid
            if cmd == "get":
                target = oid if oid in OIDS else None
            else:
                k = oid_key(oid)
                target = next((o for o in OIDS if oid_key(o) > k), None)
            if target is None:
                reply("NONE")
            else:
                typ, v = value(target)
                reply(target, typ, v)
        elif cmd == "set":
            sys.stdin.readline()
            sys.stdin.readline()
            reply("not-writable")
        else:
            reply("NONE")


if __name__ == "__main__":
    main()
