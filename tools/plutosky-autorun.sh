# Lynx-Tx quiet mode for a PlutoSky R2 on Tezuka (v0.3.24 or later).
# Installed as /mnt/jffs2/autorun.sh, which Tezuka runs at every boot; it
# lives in the board's own flash, so it survives a card change.
# Remove it to get the full Tezuka back:  rm /mnt/jffs2/autorun.sh
#
# 1. Quiet: the board only has to take IQ, so Tezuka's extras are stopped
#    (they starved the CPU and combed ATSC 3.0). Nothing here reads the
#    flash: fw_printenv every few seconds glitched the transmitter.
# 2. Reference supervisor, instead of the FPGA's automatic mode (whose
#    once-a-second probe broke OFDM): loop closed while the 10 MHz is there,
#    open (holdover on the board's own oscillator) within 2 s of losing it,
#    closed again once it has been back for 10 s. GPIO only, never the flash,
#    so the stored setting stays at Tezuka's default.
# 3. The reference state on MQTT (state/system/clkref/*) for Lynx-Tx's lamp.
#
# Proven on the bench, 19 Oct 2026: ATSC 3.0 at 2x clean with the loop held
# closed, and clean in holdover with the reference pulled.

( sleep 20
  /etc/init.d/S97datv stop
  killall maia-httpd api_controller.sh watchconsoletx.sh watchdatveasy.sh \
          watchconsolefreq.sh inotifywait zynqrx pluto_stream \
          mqtt_longmynd.sh mosquitto_sub 2>/dev/null
  for p in $(ps | awk '/[u]pdate\.sh/{print $1}'); do kill "$p"; done
  echo 2 > /sys/class/net/eth0/queues/rx-0/rps_cpus
  logger -t lynx "quiet mode on"

  # EMIO lines, zynq_gpio chip (from /etc/refclk.conf, base 54).
  CHIP=gpiochip0
  PRESENT=87; LOCK=106; FORCE_EXT=86; FORCE_INT=88

  loop_close() { gpioset $CHIP $FORCE_INT=0 $FORCE_EXT=1; closed=1; bad=0; }
  loop_open()  { gpioset $CHIP $FORCE_EXT=0 $FORCE_INT=1; closed=0; good=0; }
  pub() { mosquitto_pub -r -t "state/system/clkref/$1" -m "$2" 2>/dev/null; }

  if [ "$(gpioget $CHIP $PRESENT)" = 1 ]; then loop_close; else loop_open; fi
  logger -t lynx "reference loop $([ $closed = 1 ] && echo closed || echo open)"
  n=0; last=""
  while true; do
    set -- $(gpioget $CHIP $PRESENT $LOCK)
    p=$1; l=$2
    if [ "$closed" = 1 ]; then
      if [ "$p" = 0 ]; then bad=$((bad + 1)); else bad=0; fi
      if [ "$bad" -ge 2 ]; then
        loop_open; logger -t lynx "reference lost: holdover on internal oscillator"
      fi
    else
      if [ "$p" = 1 ]; then good=$((good + 1)); else good=0; fi
      if [ "$good" -ge 10 ]; then
        loop_close; logger -t lynx "reference back: loop closed"
      fi
    fi
    n=$((n + 1))
    if [ "$n" -ge 5 ] || [ "$p$l$closed" != "$last" ]; then
      n=0; last="$p$l$closed"
      if [ "$closed" = 1 ]; then
        mode=external; if [ "$l" = 1 ]; then state=locked; else state=acquire; fi
      else
        mode=internal; if [ "$p" = 1 ]; then state=returning; else state=holdover; fi
      fi
      pub lock "$l"; pub present "$p"; pub state "$state"; pub mode "$mode"
    fi
    sleep 1
  done
) >/dev/null 2>&1 &
