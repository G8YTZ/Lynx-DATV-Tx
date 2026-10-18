#!/bin/bash
# Lynx-Tx ATSC 3.0 fallback: HDMI camera > HEVC > QPSK 13/15 > LibreSDR.
# Independent of the Web UI - the chain as first proven on 16 October 2026.
# Usage: atsc3/go_live.sh [frequency_hz]     (default 437 MHz)
# Then: hdhomerun_config <ip> set /tuner0/channel atsc3:<frequency_hz>
FREQ=${1:-437000000}
U=ip:192.168.254.2
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN=/tmp/lynx-golive
mkdir -p $RUN

sudo systemctl stop lynx-tx
pkill -f gst-launch-1.0; pkill -f tsstuff.py; pkill -f live_tx.py; pkill iio_writedev; sleep 1
ssh root@192.168.254.2 'killall rtmppluto.sh udpts.sh pluto_dvb 2>/dev/null; true'

sed -e "s/MOD_256QAM/MOD_QPSK/g" -e "s/C9_15/C13_15/g" -e "s/id: vv031_tx/id: live_tx/" \
    -e "s|file: advatsc3.ts|file: $RUN/ts.fifo|" -e "s/repeat: 'True'/repeat: 'False'/" \
    -e "s|file: /tmp/atsc3.iq|file: $RUN/iq.fifo|" \
    "$DIR/vv031_tx.grc" > $RUN/live_tx.grc
grcc $RUN/live_tx.grc -o $RUN > /dev/null || exit 1

iio_attr -u $U -o -c ad9361-phy altvoltage1 frequency $FREQ > /dev/null
iio_attr -u $U -o -c ad9361-phy voltage0 sampling_frequency 6912000 > /dev/null
iio_attr -u $U -o -c ad9361-phy voltage0 rf_bandwidth 6000000 > /dev/null
iio_attr -u $U -o -c ad9361-phy voltage0 hardwaregain 0 > /dev/null
rm -f $RUN/ts.fifo $RUN/iq.fifo && mkfifo $RUN/ts.fifo $RUN/iq.fifo
iio_writedev -u $U -b 1048576 cf-ad9361-dds-core-lpc voltage0 voltage1 < $RUN/iq.fifo &
python3 $RUN/live_tx.py > $RUN/mod.log 2>&1 &
echo "ATSC 3.0 live on $FREQ Hz. Ctrl-C to stop."
gst-launch-1.0 -e -q \
  v4l2src device=/dev/video0 ! video/x-raw,format=NV16,width=1920,height=1080,framerate=50/1 ! \
  videorate drop-only=true ! video/x-raw,framerate=25/1 ! \
  mpph265enc rc-mode=cbr bps=6500000 gop=25 header-mode=each-idr ! h265parse config-interval=-1 ! \
  queue max-size-buffers=0 max-size-bytes=0 max-size-time=2000000000 ! mux. \
  alsasrc device=hw:CARD=rockchiphdmiin,DEV=0 buffer-time=200000 latency-time=10000 provide-clock=false ! \
  audio/x-raw,rate=48000,channels=2 ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! \
  avenc_aac bitrate=192000 ! aacparse ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=2000000000 ! mux. \
  mpegtsmux name=mux latency=1000000000 ! fdsink fd=1 | python3 "$DIR/tsstuff.py" $RUN/ts.fifo
iio_attr -u $U -o -c ad9361-phy voltage0 hardwaregain -89.75 > /dev/null
pkill -f live_tx.py; pkill iio_writedev
