#!/usr/bin/env python3
"""Lynx-Tx buffer monitor - how much slack the stream has, live.

Reads a transport stream on stdin (TSDuck's fork stage hands it a copy) and,
once a second, writes /tmp/lynx-buffer.json:

    {"slack": 1.84, "min_60s": 0.62, "late": 0, "time": ...}

slack = a video frame's decode time (DTS) minus the stream clock (PCR) at the
moment it is sent: how far ahead of need the frame travels. ffmpeg's
-max_delay sets the ceiling (2 s). When the encoder overshoots, the slack
drains; at zero the frame arrives too late ("dts < pcr") and receivers drop
sound and pictures. late counts frames sent with no slack left.

It only reads and counts, and reads in large blocks, so it keeps up easily
(a 3 Mb/s stream is about 2,000 packets a second).
"""
import json
import os
import sys
import time
from collections import deque

OUT = os.environ.get("LYNX_BUFFER_JSON", "/tmp/lynx-buffer.json")
VIDEO_PID = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x101
WRAP = 1 << 33                      # 90 kHz timestamps are 33 bits


def ts90(b):
    """A 33-bit PTS/DTS from its 5-byte PES form."""
    return (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | \
           (b[3] << 7) | (b[4] >> 1)


def main():
    pcr = None                      # latest PCR, in 90 kHz units
    recent = deque()                # (wall time, slack) for the last minute
    late = 0
    last_write = 0.0
    slack = None
    buf = b""
    stdin = sys.stdin.buffer
    while True:
        chunk = stdin.read(188 * 512)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // 188
        for i in range(n):
            p = buf[i * 188:(i + 1) * 188]
            if p[0] != 0x47:
                continue
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid != VIDEO_PID:
                continue
            afc = (p[3] >> 4) & 0x3
            pos = 4
            if afc & 0x2:                                   # adaptation field
                alen = p[4]
                if alen >= 7 and (p[5] & 0x10):             # carries a PCR
                    base = (p[6] << 25) | (p[7] << 17) | (p[8] << 9) | (p[9] << 1) | (p[10] >> 7)
                    pcr = base
                pos = 5 + alen
            if (p[1] & 0x40) and (afc & 0x1) and pcr is not None and pos + 14 <= 188:
                pes = p[pos:]
                if pes[0:3] == b"\x00\x00\x01":
                    flags = pes[7] >> 6
                    if flags == 3:
                        dts = ts90(pes[14:19])
                    elif flags == 2:
                        dts = ts90(pes[9:14])
                    else:
                        continue
                    d = (dts - pcr) % WRAP
                    if d > WRAP // 2:
                        d -= WRAP                           # behind the clock: late
                    slack = d / 90000.0
                    if slack <= 0:
                        late += 1
                    recent.append((time.time(), slack))
        buf = buf[n * 188:]
        now = time.time()
        while recent and now - recent[0][0] > 60:
            recent.popleft()
        if now - last_write >= 1 and slack is not None:
            last_write = now
            state = {"slack": round(slack, 3),
                     "min_60s": round(min(s for _, s in recent), 3) if recent else None,
                     "late": late, "time": now}
            tmp = OUT + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, OUT)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
