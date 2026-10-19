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
# 4. Starvation mute (OFF for now, see the loop): IQ stopped -> muted.
#
# Proven on the bench, 19 Oct 2026 (Tezuka v0.3.24): no underruns at 2x with
# the extras stopped; ATSC 3.0 at 2x clean with the loop held
# closed, and clean in holdover with the reference pulled.

( sleep 20
  /etc/init.d/S97datv stop
  # Matched on the whole command line: Tezuka's shell scripts show up as
  # "{name} /bin/bash /root/..." and killall misses them. v0.3.5 and v0.3.24
  # names both. The MQTT broker (mosquitto) and udhcpc stay.
  EXTRAS='maia-httpd|api_controller|watchconsole|watchdatveasy|/update\.sh|inotifywait|zynqrx|pluto_stream|longmynd|pluto_mqtt_ctrl|mqtt_obs_ctrl|bitrate_strateg|gpsd|udhcpd|mosquitto_sub'
  for pass in 1 2; do
    for p in $(ps | awk -v re="$EXTRAS" '$0 ~ re && !/awk/ {print $1}'); do
      kill "$p" 2>/dev/null
    done
    sleep 5
  done
  echo 2 > /sys/class/net/eth0/queues/rx-0/rps_cpus
  logger -t lynx "quiet mode on"

  # EMIO lines, zynq_gpio chip (from /etc/refclk.conf, base 54).
  CHIP=gpiochip0
  PRESENT=87; LOCK=106; FORCE_EXT=86; FORCE_INT=88

  loop_close() { gpioset $CHIP $FORCE_INT=0 $FORCE_EXT=1; closed=1; bad=0; }
  loop_open()  { gpioset $CHIP $FORCE_EXT=0 $FORCE_INT=1; closed=0; good=0; }
  pub() { mosquitto_pub -r -t "state/system/clkref/$1" -m "$2" 2>/dev/null; }

  # Starvation mute: if the IQ stops for 5 s (ROCK rebooted, crashed, cable
  # out), the DAC would go on radiating whatever it holds. So: attenuator to
  # the floor and the transmit LO off. Lynx-Tx powers both up again at its
  # next key-up. Fed = the DAC buffer enabled and no underflow in the last
  # second (the AXI DAC status register 0x88 latches it: bit 0, write 1 to
  # clear; memory-mapped, so reading it costs nothing).
  devname() {
    for d in /sys/bus/iio/devices/iio:device*; do
      [ "$(cat "$d/name" 2>/dev/null)" = "$1" ] && { echo "${d##*/}"; return; }
    done
  }
  PHY=/sys/bus/iio/devices/$(devname ad9361-phy)
  DDS=$(devname cf-ad9361-dds-core-lpc)
  REG=/sys/kernel/debug/iio/$DDS/direct_reg_access
  BUFEN=/sys/bus/iio/devices/$DDS/buffer/enable
  starved=0; muted=0
  STARVE_MUTE=0
  tx_mute() {
    echo -89.75 > "$PHY/out_voltage0_hardwaregain"
    echo 1 > "$PHY/out_altvoltage1_TX_LO_powerdown"
  }

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
    # Starvation mute: OFF for now (19 Oct). Polling the DAC register
    # through debugfs every second shares direct_reg_access with iiod; the
    # board falsely muted a live transmission and then stopped taking IQ.
    # To be redone without touching that file. Set STARVE_MUTE=1 to test.
    if [ "$STARVE_MUTE" = 1 ]; then
      fed=0
      if [ "$(cat "$BUFEN" 2>/dev/null)" = 1 ]; then
        echo 0x88 > "$REG"; u=$(cat "$REG"); echo "0x88 0x3" > "$REG"
        [ $(( u & 1 )) = 0 ] && fed=1
      fi
      if [ "$fed" = 1 ]; then starved=0; muted=0; else starved=$((starved + 1)); fi
      if [ "$starved" -ge 5 ] && [ "$muted" = 0 ]; then
        tx_mute; muted=1; logger -t lynx "IQ stopped: transmitter muted"
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
