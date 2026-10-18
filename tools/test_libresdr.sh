#!/bin/sh
# DVB-S2 test transmission through the LibreSDR - put Lynx-Tx in ATSC 3.0 mode with TX off first; switch back to DVB-S2 after.
# 1249 MHz, 2000 kS/s, QPSK 3/4, roll-off 0.35, gain -10 dB. Ctrl-C mutes and stops.
# Extra options pass straight through, e.g. ./test_libresdr.sh --gain -20
cd "$(dirname "$0")/.." || exit 1
TS=/tmp/testcard.ts
if [ ! -s "$TS" ]; then
  echo "Making $TS (bars and stereo tone at -18 dBFS)..."
  ffmpeg -loglevel error -y -f lavfi -i smptehdbars=size=1280x720:rate=25 \
    -f lavfi -i "aevalsrc=0.125893*sin(2*PI*1000*t)|0.125893*sin(2*PI*1000*t):s=48000:c=stereo" \
    -t 20 -c:v libx264 -b:v 1500k -maxrate 1500k -bufsize 1500k -g 50 \
    -c:a aac -b:a 128k -f mpegts -muxrate 2800000 "$TS" || exit 1
fi
exec python3 tools/dvbs2_iq.py --uri ip:192.168.254.2 --freq 1249 --sr 2000 --fec 3/4 \
  --rolloff 0.35 --gain -10 --ts "$TS" "$@"
