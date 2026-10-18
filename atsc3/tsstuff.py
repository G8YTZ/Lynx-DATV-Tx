#!/usr/bin/env python3
"""Feed a constant-rate modulator from a variable-rate TS (go_live.sh only).

Real packets when any are queued, null packets otherwise; the modulator's
pull sets the pace and the encoder is never blocked. The Web UI's ATSC 3.0
mode uses tsprio.py for this instead, which also writes the PCR.
"""
import collections
import sys
import threading

NULL = b'\x47\x1f\xff\x10' + b'\xff' * 184
q = collections.deque()


def reader():
    buf = b''
    src = sys.stdin.buffer
    while True:
        d = src.read1(65536)
        if not d:
            break
        buf += d
        while len(buf) >= 188:
            if buf[0] != 0x47:                  # resync on the 0x47 sync byte
                i = buf.find(b'\x47', 1)
                buf = buf[i:] if i > 0 else b''
                continue
            q.append(buf[:188])
            buf = buf[188:]


threading.Thread(target=reader, daemon=True).start()
with open(sys.argv[1], 'wb', buffering=0) as out:
    while True:
        out.write(b''.join(q.popleft() if q else NULL for _ in range(32)))
