# ATSC 3.0 in Lynx-Tx (Beta)

Lynx-Tx has two modes, chosen on the Transmit card:

- **DVB-S2**: DL1JM's modulator on the LibreSDR, exactly as before.
- **ATSC 3.0**: Ron Economos W6RZ's [gr-atsc3](https://github.com/drmpeg/gr-atsc3) runs on the ROCK, and the SDR chosen on the Transmit card (LibreSDR, PlutoSky R2 or any Pluto-class board) transmits the IQ.

Everything up to the remux (switcher, encoder, hybrid rate control, sound priority) is shared. In ATSC 3.0 the last stage is different:

```
switcher.py | ffmpeg (IDs) | tsprio.py | tsp (buffer tap)
    -> /tmp/lynx-atsc3-ts -> gr-atsc3 flowgraph -> /tmp/lynx-atsc3-iq
    -> iio_writedev -> LibreSDR
```

The modulator pulls the stream at exactly the PLP rate, paced by the LibreSDR's DAC clock, and tsprio fills every slot it cannot use with a null packet. That is the same back-pressure `tsp regulate` provides in DVB-S2.

## Settings

- **Constellation:** QPSK, 16QAM, 64QAM or 256QAM.
- **Code rate:** 2/15 to 13/15.
- **Frequency and power:** Kept separately from DVB-S2's. Both are changed live on air, with no break.
- **Output filter:** 2x oversample (DAC at 13.824 MS/s, the default) or Off (6.912 MS/s). The DAC's first image sits at the sample rate less the band edge, ±4.0 MHz from the carrier, which is exactly where the IC-9700 showed it. At 2x it moves to about ±10.9 MHz, where the AD9361's analogue filter removes it. Proven on air on 16 October: nothing to be seen out to ±25 MHz, and the HDHomeRun still locked.
- **Frame structure:** Fixed for now at the one proven on air: 6 MHz, 8K FFT, GI5_1024, SP3_4, 72 data and 2 preamble symbols.

Rates come from Ron's `atsc3rate` (dtv-utils) for that structure: QPSK 13/15 is 7,720,144 b/s.

**Identity:** Names, IDs and PIDs all apply. The NIT, network ID and LCN are DVB tables, so they are not sent in ATSC 3.0 mode.

**Changing mode** takes the transmitter off air. Going back to DVB-S2 reboots the LibreSDR (about a minute) to restart DL1JM's modulator. Nothing on the board is changed in ATSC 3.0 mode; its scripts are only stopped.

## Install on the ROCK

```bash
sudo apt install -y gnuradio gnuradio-dev cmake build-essential pybind11-dev python3-pybind11 libiio-utils
git clone https://github.com/drmpeg/gr-atsc3 ~/src/gr-atsc3
cd ~/src/gr-atsc3 && mkdir -p build && cd build && cmake .. && make -j8 && sudo make install && sudo ldconfig
```

**Restart and Shut down buttons:** These need one sudoers rule for the account Lynx-Tx runs as:

```bash
echo 'radxa ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff' | sudo tee /etc/sudoers.d/lynx-tx
sudo chmod 440 /etc/sudoers.d/lynx-tx
sudo visudo -c
```

## Safety: PTT in ATSC 3.0 mode

In DVB-S2, DL1JM's firmware drives PTT from the power setting. In ATSC 3.0 that firmware is stopped, so **PTT is not yet handled**. TX Off attenuates the DAC fully (-89.75 dB), but check what the LibreSDR's PTT line does before putting a PA or sequencer behind it.

## Fallback

`atsc3/go_live.sh` runs the original command-line chain (HDMI camera, QPSK 13/15, 437 MHz by default) without the Web UI.

## First lock: 16 October 2026

- Flowgraph: vv031 (256QAM 9/15), then QPSK 13/15 with a live HDMI camera and sound
- LibreSDR at 557 and 437 MHz, 6.912 MS/s, cabled through a 40-50 dB pad
- HDHomeRun (hdhomerun_dvr_atsc3): lock=atsc3 ss=100 snq=100 seq=100, tunes any frequency
- Picture and sound in VLC via the HDHomeRun's UDP output
