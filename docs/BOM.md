# Bill of materials

What a Lynx-Tx transmitter is built from. Exact part numbers and suppliers marked **TBC** are still to be filled in.

## Core

| Item | Qty | Notes |
|---|---|---|
| Radxa ROCK 5T (RK3588) | 1 | 8 GB used for development; 4 GB is enough for the transmitter alone. Two Ethernet ports: one for the LAN, one for the SDR |
| ROCK 5T power supply | 1 | RS PRO ECD-65-12, 12 V 5 A (60 W) desktop adaptor, C14 mains inlet. RS stock no. 304-023 |
| Heatsink or fan for the ROCK | 1 | TBC. Runs at about 35 °C on the bench at 4 MS/s |
| NVMe SSD (optional) | 1 | For media and logs; a 500 GB WD Blue is in use. Saves wear on the eMMC or SD card |
| SDR: PlutoSky R2 **or** LibreSDR | 1 | Tezuka firmware recommended on either |
| microSD card for the SDR | 1 | 8 GB or more, FAT32. Keep a spare with a known-good image |
| Cat 6 patch lead | 2 | One ROCK to SDR (the direct link), one ROCK to the LAN |
| 5 V supply for the SDR | 1 | TBC model. A USB power supply, or in a cabinet a 5 V frame PSU with a USB flylead. The SDR is powered from its USB socket |

## Reference

| Item | Qty | Notes |
|---|---|---|
| Leo Bodnar LBE-1420 GPSDO | 1 | GNSS-disciplined 10 MHz reference, complete kit with antenna. Puts the carrier on frequency and holds it there; the PlutoSky's own crystal was 10 ppm high |
| GNSS antenna | 1 | Included in the LBE-1420 kit. Needs a view of the sky |
| 10 MHz lead: MMCX male to SMA male, RG316 | 1 | 15 cm (6 inch) used here; length to suit. From the LBE-1420's SMA output to the SDR's MMCX 10 MHz input (PlutoSky R2 and LibreSDR). A panel SMA on the SDR's case keeps it tidy |

## Source

| Item | Qty | Notes |
|---|---|---|
| HDMI camera or source | 1 | Up to 3840 × 2160 at 25 fps |
| HDMI lead | 1 | Into the ROCK's HDMI input |

## RF (for on-air use)

| Item | Qty | Notes |
|---|---|---|
| Band-pass filter for the band in use | 1 | TBC. Recommended after the SDR |
| Power amplifier | 1 | TBC. About 1 mW drive |
| PTT sequencer | 1 | Keyed by the SDR's PTT line (Tezuka) |
| Antenna and feeder | 1 | TBC |

## Bench testing

| Item | Qty | Notes |
|---|---|---|
| Attenuator, 30 to 40 dB | 1 | SDR straight to a receiver, never without one |
| DVB-S2 receiver: [BATC PicoTuner (Ethernet)](https://wiki.batc.org.uk/PicoTuner_Ethernet_Interface) with Lynx Rx | 1 | To check lock and MER |
| ATSC 3.0 receiver: [SiliconDust HDHomeRun FLEX 4K (HDFX-4K)](https://shop.silicondust.com/shop/product/hdfx-4k/) | 1 | For ATSC 3.0 mode |
