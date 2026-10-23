# Lynx-Tx quiet mode for an SDR on Tezuka (v0.3.24 or later): a PlutoSky R2
# or a LibreSDR. Installed as /mnt/jffs2/autorun.sh, which Tezuka runs at
# every boot; it lives in the board's own flash, so it survives a card
# change. Remove it to get the full Tezuka back:  rm /mnt/jffs2/autorun.sh
#
# 1. Quiet: the board only has to take IQ, so Tezuka's extras are stopped
#    (they starved the CPU and combed ATSC 3.0). Nothing here reads the
#    flash: fw_printenv every few seconds glitched the transmitter.
# 2. The 10 MHz reference, held through a GNSS failure. The two boards do
#    it differently, and the script uses whichever this board has:
#    PlutoSky R2: switches between its own crystal and the external 10 MHz
#      (GPIO lines, /etc/refclk.conf). Instead of the FPGA's automatic mode,
#      whose once-a-second probe broke OFDM: loop closed while the 10 MHz is
#      there, open (holdover on the board's own oscillator) within 2 s of
#      losing it, closed again once it has been back for 10 s.
#    LibreSDR: a VCTCXO steered by a DAC (Christos SV1EIA's vctcxo_lock in
#      the FPGA; /usr/sbin/gpsdo_boot.sh acquires the 10 MHz at boot). After
#      that, the FPGA's own loop is left off (manual mode) and this script
#      steers the DAC itself from the FPGA's frequency error, gently and
#      only from readings that make sense. The FPGA loop, on losing the
#      10 MHz, acts on one nonsense reading before the board flags the loss
#      and throws the DAC 23000 codes (about 8 kHz at 23 cm: ATSC 3.0 lost
#      lock, bench 23 Oct 2026). Here a nonsense reading just stops the
#      steering, so the DAC stays where it was (holdover); steering resumes
#      once the reference has been back and on frequency for 10 s.
# 3. The reference state on MQTT (state/system/clkref/*) for Lynx-Tx's lamp.
# 4. Starvation mute: keyed but no IQ arriving for 2 s -> muted.
#
# Proven on the bench, 19 Oct 2026 (PlutoSky R2, Tezuka v0.3.24): no
# underruns at 2x with the extras stopped; ATSC 3.0 at 2x clean with the
# loop held closed, and clean in holdover with the reference pulled.

# SSH keys: Tezuka's /root is in RAM, so the ROCK's key is kept here in the
# board's flash and put back at every boot (no password prompts from Lynx-Tx).
if [ -s /mnt/jffs2/authorized_keys ]; then
  mkdir -p /root/.ssh && cp /mnt/jffs2/authorized_keys /root/.ssh/authorized_keys
  chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
fi

( sleep 20
  /etc/init.d/S97datv stop
  # Matched on the whole command line: Tezuka's shell scripts show up as
  # "{name} /bin/bash /root/..." and killall misses them. v0.3.5 and v0.3.24
  # names both. The MQTT broker (mosquitto) and udhcpc stay.
  # "sbin/gpsd ", not "gpsd": that would also match the LibreSDR's
  # gpsdo_boot.sh, which must be left to acquire the reference.
  EXTRAS='maia-httpd|api_controller|watchconsole|watchdatveasy|/update\.sh|inotifywait|zynqrx|pluto_stream|longmynd|pluto_mqtt_ctrl|mqtt_obs_ctrl|mqtt_pubd|bitrate_strateg|sbin/gpsd |udhcpd|mosquitto_sub'
  for pass in 1 2; do
    for p in $(ps | awk -v re="$EXTRAS" '$0 ~ re && !/awk/ {print $1}'); do
      kill "$p" 2>/dev/null
    done
    sleep 5
  done
  echo 2 > /sys/class/net/eth0/queues/rx-0/rps_cpus
  logger -t lynx "quiet mode on"

  # Which reference control this board has.
  if [ -x /usr/sbin/gpsdo_boot.sh ] && command -v devmem >/dev/null; then
    BOARD=libre
  elif [ -f /etc/refclk.conf ] || [ -x /etc/init.d/S22refclk ]; then
    BOARD=sky
  else
    BOARD=none
  fi
  logger -t lynx "reference control: $BOARD"

  # PlutoSky R2: EMIO lines, zynq_gpio chip (from /etc/refclk.conf, base 54).
  CHIP=gpiochip0
  PRESENT=87; LOCK=106; FORCE_EXT=86; FORCE_INT=88
  loop_close() { gpioset $CHIP $FORCE_INT=0 $FORCE_EXT=1; closed=1; bad=0; }
  loop_open()  { gpioset $CHIP $FORCE_EXT=0 $FORCE_INT=1; closed=0; good=0; }

  # LibreSDR: SV1EIA's vctcxo_lock registers (see /usr/sbin/gpsdo_boot.sh).
  R_CTRL=0x43C00000; R_SET=0x43C00004; R_DAC=0x43C00008; R_ST=0x43C00010; R_ERR=0x43C00018
  CTRL_LOOP=0x0001F1F0; CTRL_MANUAL=0x0001F1F1
  rd() { devmem "$1" 32; }
  wr() { devmem "$1" 32 "$2"; }
  l_err() { v=$(( $(rd $R_ERR) )); [ "$v" -ge 2147483648 ] && v=$((v - 4294967296)); echo "$v"; }
  l_dac() { echo $(( $(rd $R_DAC) & 0xFFFF )); }
  l_center() { echo $(( ($(rd $R_SET) >> 16) & 0xFFFF )); }
  l_manual() { echo $(( $(rd $R_CTRL) & 1 )); }
  # The DAC set by hand (manual mode), the centre register kept.
  l_set() { wr $R_SET "$(printf '0x%04X%04X' "$(l_center)" "$1")"; }
  l_take() { l_set "$1"; wr $R_CTRL $CTRL_MANUAL; }
  # Steering: DAC codes per Hz of error (at 40 MHz). 91 measured on a
  # LibreSDR Rev.5, 23 Oct 2026 (SV1EIA's script assumes 52). The loop
  # corrects 1/TC of the error each second, so a wrong slope only changes
  # how fast it settles; it stays stable up to about 2 x TC/1 out.
  SLOPE=91; TC=40
  [ -f /mnt/jffs2/lynx-ref.conf ] && . /mnt/jffs2/lynx-ref.conf

  pub() { mosquitto_pub -r -t "state/system/clkref/$1" -m "$2" 2>/dev/null; }

  # Starvation mute: if the IQ stops arriving (ROCK rebooted, crashed,
  # cable out) while the transmitter is keyed, the DAC would go on radiating
  # whatever it holds - junk in the channel, and with an SFN, junk on every
  # site's frequency. So: once a second, compare the bytes arriving on eth0
  # with what the sample rate needs (4 bytes a sample); under half for 2 s
  # with the TX LO on -> attenuator to the floor and the TX LO off. Lynx-Tx
  # powers both up again at its next key-up. Read-only sysfs counters only:
  # nothing here touches iiod's register access.
  devname() {
    for d in /sys/bus/iio/devices/iio:device*; do
      [ "$(cat "$d/name" 2>/dev/null)" = "$1" ] && { echo "${d##*/}"; return; }
    done
  }
  PHY=/sys/bus/iio/devices/$(devname ad9361-phy)
  RXB=/sys/class/net/eth0/statistics/rx_bytes
  lastrx=$(cat $RXB); short=0
  tx_mute() {
    echo -89.75 > "$PHY/out_voltage0_hardwaregain"
    echo 1 > "$PHY/out_altvoltage1_TX_LO_powerdown"
  }

  closed=0; good=0; bad=0; p=0; l=0; acq=0
  dac=0; frac=0; e1=""; e2=""; waited=0
  case $BOARD in
    sky)
      if [ "$(gpioget $CHIP $PRESENT)" = 1 ]; then loop_close; else loop_open; fi
      logger -t lynx "reference loop $([ $closed = 1 ] && echo closed || echo open)" ;;
    libre) : ;;
  esac
  n=0; last=""
  while true; do
    case $BOARD in
    sky)
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
      fi ;;
    libre)
      st=$(( $(rd $R_ST) )); p=$(( (st >> 1) & 1 )); l=$(( st & 1 ))
      e=$(l_err); ae=${e#-}
      acq=0
      if ps | grep -q "[g]psdo_boot"; then
        # SV1EIA's acquisition is running: hands off.
        closed=0; good=0; acq=1; dac=0
      elif [ "$dac" = 0 ]; then
        # Acquisition finished (or this script restarted): take the DAC
        # where it is and steer from here.
        dac=$(l_dac); frac=0; l_take "$dac"
        if [ "$p" = 1 ] && [ "$ae" -le 20 ]; then
          closed=1; logger -t lynx "reference: steering the VCTCXO from DAC $dac"
        else
          closed=0; good=0; logger -t lynx "reference: no 10 MHz, holding DAC $dac"
        fi
      elif [ "$closed" = 1 ]; then
        if [ "$p" = 1 ] && [ "$ae" -le 20 ]; then
          # Correct 1/TC of the error; the remainder carried, so a steady
          # 1 Hz error still gets corrected.
          frac=$(( frac - e * SLOPE )); step=$(( frac / TC )); frac=$(( frac - step * TC ))
          [ "$step" -gt 20 ] && step=20; [ "$step" -lt -20 ] && step=-20
          if [ "$step" != 0 ]; then
            dac=$(( dac + step ))
            [ "$dac" -lt 1 ] && dac=1; [ "$dac" -gt 65534 ] && dac=65534
            l_set "$dac"
          fi
        else
          # Gone, or a reading that makes no sense: stop steering. The DAC
          # does not move, so this is holdover.
          closed=0; good=0; e1=""; e2=""; waited=0; frac=0
          logger -t lynx "reference lost (present $p, error $e Hz): holdover, DAC held at $dac"
        fi
      else
        # Holdover. Back to steering after 10 s present and within 20 Hz.
        if [ "$p" = 1 ] && [ "$ae" -le 20 ]; then
          good=$((good + 1)); e1=""; e2=""
          if [ "$good" -ge 10 ]; then
            closed=1; logger -t lynx "reference back: steering from DAC $dac"
          fi
        elif [ "$p" = 1 ] && [ "$ae" -le 2000 ]; then
          # Back but well off (a long holdover): one full correction, once
          # three readings agree, then the gentle steering takes over.
          good=0
          if [ -n "$e2" ] && [ $(( e - e2 )) -le 3 ] && [ $(( e2 - e )) -le 3 ] \
              && [ $(( e - e1 )) -le 3 ] && [ $(( e1 - e )) -le 3 ]; then
            step=$(( -e * SLOPE ))
            dac=$(( dac + step ))
            [ "$dac" -lt 1 ] && dac=1; [ "$dac" -gt 65534 ] && dac=65534
            l_set "$dac"; e1=""; e2=""
            logger -t lynx "reference back but $e Hz off: DAC moved to $dac"
            sleep 2
          else
            e2=$e1; e1=$e
          fi
        else
          good=0; e1=""; e2=""
        fi
        # Present for a minute and still not usable: SV1EIA's acquisition.
        if [ "$p" = 1 ]; then waited=$((waited + 1)); else waited=0; fi
        if [ "$waited" -ge 60 ]; then
          logger -t lynx "reference present 60 s but not usable (error $e Hz): re-acquiring"
          waited=0; /usr/sbin/gpsdo_boot.sh >/dev/null 2>&1 & sleep 1
        fi
      fi ;;
    esac
    rx=$(cat $RXB)
    got=$(awk -v a="$rx" -v b="$lastrx" 'BEGIN { printf "%.0f", a - b }')
    lastrx=$rx
    need=$(( $(cat "$PHY/out_voltage_sampling_frequency") * 2 ))
    if [ "$(cat "$PHY/out_altvoltage1_TX_LO_powerdown")" = 0 ] && [ "$got" -lt "$need" ]; then
      short=$((short + 1))
    else
      short=0
    fi
    if [ "$short" -ge 2 ]; then
      tx_mute; short=0
      logger -t lynx "IQ stopped (${got} B/s, needs ${need}): transmitter muted"
    fi

    n=$((n + 1))
    if [ "$BOARD" != none ] && { [ "$n" -ge 5 ] || [ "$p$l$closed$acq" != "$last" ]; }; then
      n=0; last="$p$l$closed$acq"
      if [ "$acq" = 1 ]; then
        mode=external; state=acquire
      elif [ "$closed" = 1 ]; then
        mode=external; if [ "$l" = 1 ]; then state=locked; else state=acquire; fi
      else
        mode=internal; if [ "$p" = 1 ]; then state=returning; else state=holdover; fi
      fi
      pub lock "$l"; pub present "$p"; pub state "$state"; pub mode "$mode"
      pub board "$BOARD"
      if [ "$BOARD" = libre ]; then pub error_hz "$(l_err)"; pub dac "$(l_dac)"; fi
    fi
    sleep 1
  done
) >/dev/null 2>&1 &
