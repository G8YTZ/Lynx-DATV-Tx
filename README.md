# Lynx-Tx

A 4K DATV transmitter you drive from a web page. A Radxa ROCK 5T mixes, encodes and modulates; a Pluto-class SDR on its own cable puts it on air. DVB-S2 and ATSC 3.0 from one box, with DVB-T2 coming soon.

Companion to the [Lynx DATV Receiver](https://github.com/G8YTZ/lynx-datv-receiver): same family, opposite direction.

**Status: Beta.** In use on the bench and on air. Feedback and bug reports welcome.

![Lynx-Tx wiring](docs/wiring.svg)

## Features

- **Vision mixer:** Camera (HDMI, up to 4K) and the built-in 4K media player run all the time; the cut happens in front of the encoder, with no break on air
- **Hardware HEVC:** The RK3588's encoder does 4K25 at about a quarter of one core, with frame-by-frame rate control and sound always ahead of pictures
- **DVB-S2 and ATSC 3.0** made on the ROCK by Ron Economos W6RZ's gr-dtv and gr-atsc3; DVB-T2 coming soon
- **Any Pluto-class SDR with Ethernet,** found automatically on its own cable; a PlutoSky on Tezuka is prepared by itself, with its 10 MHz reference held through a GNSS failure
- **Broadcast habits:** The SDR mutes itself if the ROCK stops, auto-recover with lockout, alarm lamps on the page, and SNMP alarms to Home Assistant
- **One page** for source, encoder, identity, frequency, power, TX on and off, restart and shut down

## Getting started

On a ROCK 5T running Radxa's Debian 12 image, one command installs everything:

```bash
curl -sSL https://raw.githubusercontent.com/G8YTZ/Lynx-DATV-Tx/main/install.sh | bash
```

The [Installation Guide](https://github.com/G8YTZ/Lynx-DATV-Tx/raw/main/docs/lynx_tx_install_guide.docx) takes you from a blank SD card to a first transmission, including the SDR.

## Documentation

- [`lynx_tx_install_guide.docx`](https://github.com/G8YTZ/Lynx-DATV-Tx/raw/main/docs/lynx_tx_install_guide.docx): Blank SD card to working transmitter, the SDR, troubleshooting, NVMe, PlutoSky from flash, bill of materials
- [`lynx_tx_monitoring_guide.docx`](https://github.com/G8YTZ/Lynx-DATV-Tx/raw/main/docs/lynx_tx_monitoring_guide.docx): Alarms, auto-recover and lockout, SNMP and Home Assistant
- [`lynx_tx_media_guide.docx`](https://github.com/G8YTZ/Lynx-DATV-Tx/raw/main/docs/lynx_tx_media_guide.docx): Preparing clips to play out, with a DaVinci Resolve walkthrough
- [`LYNX-MIB.txt`](mibs/LYNX-MIB.txt): The SNMP MIB

## Requirements

- Radxa ROCK 5T (8 GB or more), running Radxa's own Debian 12 image
- A PlutoSky R2 or LibreSDR on [Tezuka firmware](https://github.com/F5OEO/tezuka_fw/releases) v0.3.24 or later, on its own Cat 6 lead to the ROCK's second Ethernet port
- Ideally a GNSS 10 MHz reference
- A receiver: a [BATC PicoTuner](https://wiki.batc.org.uk/PicoTuner_Ethernet_Interface) for DVB-S2, an [HDHomeRun FLEX 4K](https://shop.silicondust.com/shop/product/hdfx-4k/) for ATSC 3.0

## Credits

- **Ron Economos W6RZ:** gr-dtv (DVB-S2) and gr-atsc3, and the rate utilities
- **Evariste F5OEO:** Tezuka firmware
- **DL1JM:** The LibreSDR DVB-S2 firmware this project started on
- **TSDuck** for the transport stream tools, and **Radxa** for the ROCK 5T

## Licence

GPL-3.0. See [LICENSE](LICENSE).

Justin Cockett, G8YTZ
