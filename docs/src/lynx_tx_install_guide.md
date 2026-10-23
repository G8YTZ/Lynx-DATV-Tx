title: Lynx
subtitle: DATV Transmitter (Lynx-Tx)
tagline: Installation Guide: From Blank SD Card to Working Transmitter
version: Version 1.1  ·  G8YTZ / EI3IOB  ·  October 2026

# 1. Before You Start

## 1.1 What You'll Need

- **A Radxa ROCK 5T,** 8 GB or more. It mixes, encodes and modulates: the whole transmitter except the RF.
- **A 12 V DC supply** for the ROCK (it takes 9 to 20 V). RS PRO ECD-65-12, 12 V 5 A, is the one in use.
- **A heatsink for the ROCK:** Radxa Heatsink 6240B (with fan) or 4025A (fanless). In a closed cabinet, choose the fan.
- **A microSD card** for the ROCK, 32 GB or more and A2-rated (SanDisk Extreme, Samsung PRO Plus). Cheap cards make the install painfully slow. For a repeater, an NVMe drive instead (Appendix A).
- **An SDR:** A PlutoSky R2 (preferred) or a LibreSDR, running Evariste F5OEO's [Tezuka firmware](https://github.com/F5OEO/tezuka_fw/releases) v0.3.24 or later, with a 5 V USB-C supply of its own (a Raspberry Pi 5 supply is ideal) and its own microSD card.
- **Two Cat 6 leads:** One from the ROCK to your LAN, one from the ROCK to the SDR.
- **A computer** on the same network, to write the card, log in and use the web page.
- **A 30 to 40 dB attenuator** and a receiver for the first test on a cable: a [BATC PicoTuner](https://wiki.batc.org.uk/PicoTuner_Ethernet_Interface) for DVB-S2, a [SiliconDust HDHomeRun FLEX 4K](https://shop.silicondust.com/shop/product/hdfx-4k/) for ATSC 3.0.
- **Optionally:** An HDMI camera or source (up to 3840 × 2160 at 25 fps), and a GNSS 10 MHz reference such as the Leo Bodnar LBE-1420, which puts the carrier on frequency and holds it there.

The full list, with part numbers, is in Appendix C.

> A GitHub account is not needed. The installer fetches everything itself.

## 1.2 What You'll End Up With

A ROCK 5T that boots straight to a working transmitter, controlled from one web page on any computer, tablet or phone on your network:

- **A vision mixer:** Camera (HDMI, up to 4K) and the built-in 4K media player run all the time, and the cut happens in front of the encoder, with no break on air.
- **Hardware HEVC:** The RK3588's encoder does 4K25 at about a quarter of one core, with frame-by-frame rate control so the receiver's buffer is never late, and sound always ahead of pictures.
- **Three modes from one box:** DVB-S2 (from 333 kS/s, QPSK to 32APSK) and ATSC 3.0 (6 MHz), both made on the ROCK by Ron Economos W6RZ's modulators, with DVB-T2 coming soon.
- **Any Pluto-class SDR with Ethernet,** found automatically on its own cable, with its model, firmware and reference lock shown on the page.
- **Broadcast habits:** The SDR mutes itself if the ROCK stops, auto-recover restarts the transmitter after a fault (with a lockout after three in an hour), and SNMP alarms reach your phone through Home Assistant (see the Monitoring Guide).

![Lynx-Tx wiring](img/wiring.png){6.3}

# 2. Flash the ROCK's Operating System

Lynx-Tx runs on Radxa's own Debian 12 image for the ROCK 5T. It must be Radxa's: only their image carries the video encoder and HDMI capture drivers Lynx-Tx needs.

- Download the official Debian image from Radxa's [ROCK 5T download page](https://docs.radxa.com/en/rock5/rock5t/download): `rock-5t_bookworm_kde_r7.output_512.img.xz` at the time of writing (Debian 12 "Bookworm", KDE desktop). Not Android, and not Armbian.
- Download and install [balenaEtcher](https://etcher.balena.io) on your computer (Mac, Windows or Linux).
- Insert the microSD card into your computer. In Etcher: **Flash from file**, choose the image, **Select target**, choose the card (carefully: it will be erased), **Flash**.
- Etcher checks the card after writing. If it reports a failure, try again or use another card.
- Put the card in the ROCK, connect the network cable to its **first** Ethernet port (the second is for the SDR), and power it up.

> First boot takes about 40 seconds. With a screen attached you get a desktop; Lynx-Tx does not need one.

# 3. Initial Connection

Find the ROCK's address: Look in your router's list of connected devices for one called `rock-5t`. Then, from a terminal on your computer (Terminal on a Mac, PowerShell or Windows Terminal on Windows):

```
ssh radxa@192.168.1.XXX
```

The password is `radxa`. Accept the host key prompt on first connection. Change the password straight away:

```
passwd
```

Bring the system up to date, and restart:

```
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

> This may take several minutes on first run. Wait a minute after the restart, then connect again with ssh.

# 4. Installing Lynx-Tx

## 4.1 Quick Install (Recommended)

One command installs everything:

```
curl -sSL https://raw.githubusercontent.com/G8YTZ/Lynx-DATV-Tx/main/install.sh | bash
```

It installs every package, TSDuck, the video loopback for the media player, Ron Economos W6RZ's gr-atsc3, the patched HEVC encoder, Lynx-Tx itself, the direct link to the SDR (with DHCP, so the SDR needs no setting up), SNMP monitoring, quiet fan control, and the service that starts it at boot. It asks for your password once at the start.

> About 15 minutes on a fresh ROCK, most of it compiling. Leave it running; if your ssh connection drops, run the command again: it carries on where it left off.

It finishes by printing the page's address, and lists anything that needs a look. On a first install it then asks **Restart now? [Y/n]**: press Enter. The memory setting for 4K only takes effect after a restart, and transmitting before it can freeze the ROCK.

Running it again later is safe, and is how you update: it only updates the code and fills in anything missing.

## 4.2 Manual Install

What the installer does, step by step, for anyone who wants to see inside or fix a step that failed. Each step is a section of `install.sh`, which is the reference.

**1. Packages:**

```
sudo apt update
sudo apt install -y git curl jq python3-fastapi python3-uvicorn python3-pydantic \
    python3-yaml python3-numpy python3-gi ffmpeg v4l-utils gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-libav gir1.2-gstreamer-1.0 gnuradio gnuradio-dev libiio-utils \
    mosquitto-clients sshpass cmake build-essential pkg-config pybind11-dev \
    python3-pybind11 libspdlog-dev libgmp-dev liborc-0.4-dev network-manager dnsmasq-base
```

Check the hardware encoder is there: `gst-inspect-1.0 mpph265enc` should describe it. If not, the image is not Radxa's.

**2. Lynx-Tx:**

```
git clone https://github.com/G8YTZ/Lynx-DATV-Tx ~/lynx-tx
mkdir -p ~/lynx-tx/config
cp ~/lynx-tx/lynx_tx.example.yaml ~/lynx-tx/config/lynx_tx.yaml
sudo mkdir -p /srv/lynxdeck/media && sudo chown radxa: /srv/lynxdeck/media
```

**3. TSDuck** (`tsp`, `tstabcomp`) is not in Debian, and its newer releases no longer build for Debian 12: use 3.39-3956, the last that does, from the [TSDuck releases](https://github.com/tsduck/tsduck/releases) (`tsduck_3.39-3956.debian12_arm64.deb`), with `sudo apt install ./tsduck_3.39-3956.debian12_arm64.deb`.

**4. Video loopback** for the built-in media player (`/dev/video10`, up to 4K):

```
sudo apt install -y linux-headers-$(uname -r) v4l2loopback-dkms
echo v4l2loopback | sudo tee /etc/modules-load.d/lynx-tx.conf
echo 'options v4l2loopback video_nr=10 max_width=3840 max_height=2160 card_label="Lynx Deck"' | \
    sudo tee /etc/modprobe.d/lynx-tx-v4l2loopback.conf
sudo modprobe v4l2loopback
```

**Memory for 4K:** Radxa's image reserves 256 MB of contiguous memory, shared by HDMI in, the decoder, the encoder and the scaler. After hours on air that is not enough, and the ROCK can freeze. Add `cma=1024M` to the end of the kernel command line in both `/etc/kernel/cmdline` and the `append` lines of `/boot/extlinux/extlinux.conf`, then restart. Also switch off swap, as a live 4K chain must never be paged out: `sudo systemctl disable --now zramswap`.

**5. The ATSC 3.0 modulator,** Ron Economos W6RZ's gr-atsc3, built from source (a few minutes). DVB-S2 uses his gr-dtv blocks, which come with GNU Radio.

```
git clone https://github.com/drmpeg/gr-atsc3 ~/src/gr-atsc3
cd ~/src/gr-atsc3 && mkdir -p build && cd build
cmake .. && make -j8 && sudo make install && sudo ldconfig
```

**6. The patched HEVC encoder:** Hybrid rate control needs Radxa's encoder plugin rebuilt with the patch in `patches/`, which lets the quality floor change on the fly. Without it Lynx-Tx falls back to plain CBR and says so on the page. The build is involved (GStreamer's headers are unpacked privately, so as not to disturb Radxa's graphics libraries); run that section of `install.sh` rather than typing it. Radxa's original is kept as `libgstrockchipmpp.so.stock`.

**7. Restart and Shut down buttons:** One sudoers rule, allowing exactly those two commands:

```
echo 'radxa ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff' | \
    sudo tee /etc/sudoers.d/lynx-tx
sudo chmod 440 /etc/sudoers.d/lynx-tx && sudo visudo -c
```

**8. The direct link to the SDR:** See Section 5.2 for the commands. The installer also makes the ROCK an SSH key (`ssh-keygen -t ed25519`), which Lynx-Tx puts on a PlutoSky by itself.

**9. Fan control:** Radxa's image runs the fan flat out even when the ROCK is cool. `tools/lynx_fan.py` (the same as the Lynx Receiver's) holds the processor at a steady 40 °C instead: on at 35 °C at its slowest reliable speed, faster as needed, off below 30 °C. The kernel's own fan step at 60 °C and throttling at 85 °C stay as the safety net. The installer puts it in `/usr/local/sbin/lynx-fan` with a service, `lynx-fan.service`; the settings are in `/etc/default/lynx-fan`.

**10. Monitoring and the service:** SNMP is set up as in the Monitoring Guide, and the service as in Section 7.

# 5. Connecting the SDR

## 5.1 The SDR's Card

The SDR is used purely as an IQ transmitter: the ROCK makes the signal and the board only converts it to RF.

- Open the [Tezuka releases page](https://github.com/F5OEO/tezuka_fw/releases). It must be **v0.3.24 or later**: the card supplied with a PlutoSky is older and will not do.
- Under **Assets**, click **Show all assets**, and download the zip named after your board: `plutoskyr2` for the PlutoSky R2, `libre` for the LibreSDR. The wrong one drives the wrong pins.
- Format a microSD card as FAT32 (on a Mac: Disk Utility, MS-DOS (FAT), Master Boot Record).
- Unzip, and copy the **contents** of the `sdimg` folder to the root of the card: `BOOT.bin`, `devicetree.dtb`, `uImage` and the rest at the top level, not in a folder.
- Keep the board's original card as your way back.

> A PlutoSky R2 can run Tezuka from its own flash instead, with no card at all. See Appendix B.

## 5.2 The Direct Link

The supported connection: the SDR on its own Cat 6 lead to the ROCK's **second** Ethernet port, the RJ45 at the far edge of the board. Nothing else shares that cable, so the IQ stream (about 130 Mb/s at 2 MS/s, 260 Mb/s at 4 MS/s) never meets your other traffic.

![ROCK 5T connections](img/rock5t-ports.jpg){4.5}

| Port | Address |
|---|---|
| ROCK, second port | 192.168.254.1 / 24 |
| SDR | 192.168.254.2, given by the ROCK (DHCP). A replacement board gets another address for a while; the Transmit card shows it |

The installer sets this up. By hand, the port is usually `enP4p65s0` (check with `ip -br link`: the one showing `UP` that is not your LAN):

```
sudo nmcli con add type ethernet ifname enP4p65s0 con-name lynx-sdr \
    ipv4.method shared ipv4.addresses 192.168.254.1/24 ipv6.method disabled
echo 'dhcp-host=*:*:*:*:*:*,192.168.254.2' | sudo tee /etc/NetworkManager/dnsmasq-shared.d/lynx-sdr.conf
sudo nmcli con up lynx-sdr
```

If Radxa's automatic "Wired connection 2" is on the same port, delete it: `sudo nmcli con delete "Wired connection 2"`.

> The SDR on your ordinary LAN also works (type its address into the Transmit card), but is not supported: expect trouble on a busy or Wi-Fi-bridged network.

## 5.3 Plugging It In

- Card in the SDR (pushed fully home: the PlutoSky's push-push latch can let it ride out), unless it runs from flash.
- Ethernet from the SDR to the ROCK's second port.
- Power: The SDR's own 5 V supply into its USB socket. Not a computer: it is only power.
- If you have a 10 MHz reference, into the SDR's 10 MHz input (MMCX on both boards).
- Within a minute or so the board appears on the page's Transmit card with its model and firmware.

**First time only, a PlutoSky or LibreSDR on Tezuka:** Lynx-Tx prepares it as soon as it finds it, off air. The Transmit card says "SDR: Lynx-Tx script installed - the SDR is restarting (about a minute)", the board restarts once, and it is ready. Nothing to type. Section 5.4 says what the script does.

## 5.4 What Lynx-Tx Does to a PlutoSky or LibreSDR

Tezuka is a do-everything SDR: receiver, waterfall, web control, DATV modes. As a dedicated wideband transmitter three of those got in the way of ATSC 3.0, so Lynx-Tx installs one script in the board's own flash (`/mnt/jffs2/autorun.sh`). It survives a card change, and a newer Lynx-Tx updates it by itself.

- **Quiet mode:** Stops Tezuka's extras, which starved the board's processor at ATSC 3.0 rates (combing, lost lock).
- **The 10 MHz reference, held:** ATSC 3.0 cannot tolerate the oscillator being nudged, so the script takes charge of the reference. The page's **10 MHz Reference** lamp shows the state: Green locked, amber acquiring or in holdover, red unlocked.
  - **PlutoSky:** Tezuka's automatic mode re-checks the reference once a second, and each check nudges the oscillator. The script holds the loop locked while the 10 MHz is there, lets go within 2 s if it disappears (holdover on the board's own oscillator: still on air, a few hundred hertz adrift), and locks again once it has been back for 10 s.
  - **LibreSDR:** The board steers its own oscillator to the 10 MHz (Christos SV1EIA's disciplining, which acquires the reference at start-up). After that the script does the steering, gently, and ignores any reading that makes no sense, so pulling the 10 MHz lead moves nothing: the board carries on where it was (holdover) and picks up again 10 s after the reference returns. No glitch either way (bench, 23 Oct 2026).

> Losing GNSS (the antenna, jamming, weather) never reaches the SDR: the LBE-1420 carries on giving 10 MHz from its own oscillator, so the lamp stays green and nothing on air changes. Holdover here is for the 10 MHz itself going (a lead, the GPSDO's power).

- **Starvation mute:** If the IQ stops arriving (the ROCK restarting or failing, a cable out) the board turns its transmitter off within 2 s rather than radiating junk. Lynx-Tx turns it back on at the next TX On.
- **The ROCK's SSH key,** so Lynx-Tx never needs a password. It is put there once with Tezuka's factory password (`analog`).

To give the board back to plain Tezuka: over SSH, `rm /mnt/jffs2/autorun.sh`, then restart it.

## 5.5 LibreSDR Notes

- **With Tezuka:** Set it up exactly as the PlutoSky; Lynx-Tx prepares it by itself (Section 5.4), and DVB-S2 and ATSC 3.0 are both clean. Its settings live in the board's flash, not on the card. Allow a minute and a half after it starts for the 10 MHz to be acquired: the lamp is amber "acquiring" until then.
- **Coming from DL1JM's firmware:** It leaves `mode=2r2t` in the board's flash, and Tezuka obeys it: the signal comes out twice as wide and never locks. Fix it once over SSH: `fw_setenv mode 1r1t`, `sync`, `reboot`.
- **With DL1JM's firmware:** Supported, for a LibreSDR that also serves Portsdown. Keep each firmware on its own card. As an IQ transmitter (Modulator: ROCK) Lynx-Tx stops DL1JM's modulator for the duration, and **PTT is not keyed**: key your PA another way. The 10 MHz Reference lamp reads "not reported".

# 6. First Transmission

Open the page at `http://<the ROCK's address>:8080`. On a cable, through a 30 to 40 dB attenuator into a receiver, never straight into a tuner:

- **Mode:** DVB-S2. **SDR:** your board. **Modulator:** ROCK.
- **Modulation:** 1249 MHz, 2000 kS/s, QPSK, 3/4, pilots on, roll-off 0.35, power −20 dB.
- **TX On.** The receiver should lock within a couple of seconds.

> A BATC PicoTuner locks at roll-off 0.35, not 0.20. It also will not declare lock on a stream of nothing but null packets: always send a real programme.

For **ATSC 3.0,** set the power to **−20 dB or lower** on the PlutoSky: at −12 dB the transmitter squashes the OFDM peaks, and the receiver locks but cannot decode.

Media to play out: Upload clips on the page's **Manage media** link. The Media Guide says how to prepare them. No camera? The Camera input shows colour bars until one is plugged in.

**Your licence:** Frequency, bandwidth and power are your responsibility. A 2000 kS/s DVB-S2 signal at roll-off 0.35 is 2.7 MHz wide.

# 7. Running Automatically

The installer sets Lynx-Tx up as a service that starts at boot, so there is nothing to do. For reference, `/etc/systemd/system/lynx-tx.service`:

```
[Unit]
Description=Lynx-DATV-Tx
After=network-online.target
Wants=network-online.target

[Service]
User=radxa
WorkingDirectory=/home/radxa/lynx-tx
ExecStart=/usr/bin/python3 /home/radxa/lynx-tx/lynx_tx.py
Restart=on-failure
KillMode=mixed
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

`KillMode=mixed` matters: Lynx-Tx stops first and mutes the SDR while IQ is still flowing, and only then the pipeline, so the SDR is never left radiating a stale buffer.

```
sudo systemctl daemon-reload && sudo systemctl enable --now lynx-tx
```

**Updating:** Run the installer's one command again. For the code alone: `cd ~/lynx-tx && git pull && sudo systemctl restart lynx-tx`.

# 8. Modes

| Mode | Modulator | SDR | Receiver |
|---|---|---|---|
| DVB-S2 | ROCK: gr-dtv (default) | Any Pluto-class with Ethernet, Tezuka preferred | BATC PicoTuner, MiniTiouner, satellite receivers |
| DVB-S2 | LibreSDR on board: DL1JM's firmware | LibreSDR with DL1JM's firmware only | As above |
| ATSC 3.0 | ROCK: gr-atsc3 | Any Pluto-class with Ethernet | HDHomeRun FLEX 4K (HDFX-4K) |
| DVB-T2 (coming soon) | ROCK: gr-dtv | Any Pluto-class with Ethernet | DVB-T2 receivers, Lynx DVB-T2 Receiver |

# 9. Safety

- **Test on a cable first:** A 30 to 40 dB attenuator into a receiver.
- **PTT:** Tezuka keys its own PTT line whenever IQ is flowing. DL1JM's firmware keys from the power setting. A LibreSDR on DL1JM's firmware used as an IQ transmitter does **not** key PTT: check before putting a PA behind it.
- **TX Off** sets the SDR's most attenuation (−89.75 dB) before the stream stops, and powers its transmitter down.
- **If the ROCK stops,** a prepared PlutoSky mutes itself within 2 seconds.

# 10. Verifying the Install

From the ROCK:

```
systemctl is-active lynx-tx lynx-fan
gst-inspect-1.0 mpph265enc | head -3
python3 -c "import atsc3; print('gr-atsc3 ok')"
ls /dev/video10
grep -o 'cma=[^ ]*' /proc/cmdline
iio_attr -u ip:192.168.254.2 -C
```

In order: `active` twice (Lynx-Tx and the fan control); the encoder's description; `gr-atsc3 ok`; `/dev/video10`; `cma=1024M`; and the SDR's model and firmware (use the address the Transmit card shows if it is not .2).

On the page: the SDR shows its model and firmware, the **SDR** and **HDMI** lamps are green (HDMI only with a source plugged in), and with a 10 MHz reference connected the **10 MHz Reference** lamp goes green within a few seconds of the board starting.

# 11. Troubleshooting

## 11.1 The installer stopped partway

Run it again: each step checks whether it is already done. If it ends with warnings, each names the step and the fix. `journalctl -u lynx-tx -n 50` shows why the service did not start.

## 11.2 ssh refuses: "REMOTE HOST IDENTIFICATION HAS CHANGED"

After re-flashing the ROCK, your computer still remembers the old one at that address. Forget it, then connect again: `ssh-keygen -R 192.168.1.XXX` (the ROCK's address).

## 11.3 "The Rockchip hardware encoder (mpph265enc) is missing"

The ROCK is not running Radxa's own Debian 12 image. Re-flash with the image in Section 2.

## 11.4 The SDR does not appear on the Transmit card

- Check the cable is in the ROCK's **second** port, and the SDR is powered from a supply, not a computer.
- `ip -br link` on the ROCK: the SDR port should be `UP`. If the installer warned "No port called enP4p65s0", re-run it with `LYNX_SDR_PORT=<name>` in front of `bash`.
- `iio_attr -u ip:192.168.254.2 -C` should print the board's model.
- **PlutoSky on the supplied card (Tezuka v0.3.5):** Its Ethernet receives but never transmits: no DHCP lease, a 169.254 address. Make a v0.3.24 card (Section 5.1).

## 11.5 Password refused on the SDR

A password saved in the board's flash overrides `analog`. Plug the board into a computer's USB: it appears as a drive called **TEZUKA**. Add `revert_passwd = 1` under `[ACTIONS]` in `config.txt`, eject, then restart the board.

## 11.6 ATSC 3.0 locks but will not decode, or combs

- Power at −20 dB or lower (Section 6).
- On a PlutoSky, check the Lynx-Tx script is installed: the Transmit card says so, or `ssh root@192.168.254.2 ls /mnt/jffs2/autorun.sh`.
- On a LibreSDR on DL1JM's firmware, which does not have the Lynx-Tx script: use Tezuka (Section 5.5).

## 11.7 The signal is twice as wide and never locks (LibreSDR)

DL1JM's firmware left `mode=2r2t` in the board's flash: `fw_setenv mode 1r1t`, `sync`, `reboot` (Section 5.5).

## 11.8 The page shows LOCKOUT

Auto-recover restarted the transmitter three times within an hour and has given up until someone looks. Find the cause (the SDR's power and cable first), then press **TX On**. The Monitoring Guide explains auto-recover.

## 11.9 Off frequency

The PlutoSky's own crystal can be 10 ppm or more out (13 kHz at 1249 MHz on one board). Feed it a 10 MHz reference: Lynx-Tx locks to it by itself. Or use the tone test from the ROCK to zero-beat it: `python3 tools/dvbs2_iq.py --uri ip:192.168.254.2 --freq 1249 --tone 100` puts a tone 100 kHz above.

## 11.10 Checking logs generally

```
journalctl -u lynx-tx -f
```

Leave it running while you reproduce the problem.

# 12. What's Not Yet Covered By This Guide

- DVB-T2 mode (coming soon).
- PTT keyed by Lynx-Tx itself.
- Exact part numbers for filters, amplifiers and antennas (Appendix C, marked TBC).

# 13. Document Version History

| Version | Date | Changes |
|---|---|---|
| 1.0 | October 2026 | First edition, replacing the Markdown pages: OS install, quick and manual install, the SDR, first transmission, troubleshooting, NVMe, PlutoSky from flash, bill of materials |

# Appendix A. Moving from a microSD Card to NVMe

For a repeater, or anything left running unattended. Cards wear out and fail without warning; an NVMe drive does not.

## A.1 What You'll Need

- An M.2 2280 NVMe drive (a 500 GB WD Blue is in use).
- The working microSD card from Section 2, and the image file you wrote to it.

## A.2 Update the Boot Loader

So the ROCK can start from NVMe: `sudo rsetup`, then **System Maintenance**, **Update SPI Bootloader**, choose the ROCK 5T one, and confirm.

## A.3 Fit the Drive and Check It

Power off, fit the drive, power on from the card. `lsblk` should list `nvme0n1`.

## A.4 Copy the Image onto It

Put the downloaded `.img.xz` on the ROCK (`scp` from your computer), then, checking `nvme0n1` really is the drive, as this erases it:

```
xzcat rock-5t_bookworm_kde_r7.output_512.img.xz | sudo dd of=/dev/nvme0n1 bs=1M status=progress
```

## A.5 Remove the Card and Start Again

Power off, take the card out, and power on. It now starts from the NVMe: carry on from Section 3 as for a new ROCK.

# Appendix B. PlutoSky R2: Running Tezuka from Flash

Proven 20 October 2026. Tezuka, its boot loader and its settings all live in the board's own flash, so the card slot is out of the picture. The Lynx-Tx script in `/mnt/jffs2` is untouched by any of this and carries on working.

> **The trap:** Writing only Tezuka's firmware (`pluto.frm`, the usual Pluto update) is not enough. The factory boot loader and settings stay in flash and cannot start Tezuka: the board shows red then solid amber, no blue. Even booting from the card then fails, because the card's boot loader takes its settings from the flash. The cure is to write Tezuka's boot loader and settings as well, by DFU, as below.

## B.1 What You'll Need

- The `plutoskyr2` zip: its `flash` folder holds `boot.dfu`, `boot.frm`, `pluto.dfu` and `pluto.frm`.
- `dfu-util` on a computer: `brew install dfu-util` on a Mac, `sudo apt install dfu-util` on the ROCK.
- A USB **data** lead (many USB-C leads carry power only).

> On an Apple Silicon Mac, click **Allow** when it asks about a new accessory, or the board stays invisible.

## B.2 Back Up the Flash

While the board runs Tezuka from the card, off air (reading the flash glitches the transmitter), from the ROCK:

```
mkdir -p ~/sky-backup && cd ~/sky-backup
for n in 0 1 2 3; do ssh root@192.168.254.2 "cat /dev/mtd$n" > sky-mtd$n.bin; done
ls -l
```

Four files: 1 MB, 128 KB, 896 KB and 30 MB. Keep them somewhere safe.

## B.3 Write Tezuka's Firmware

Still booted from the card. The board has no `scp`, so copy `flash/pluto.frm` across with ssh, then update:

```
ssh root@192.168.254.2 'cat > /tmp/pluto.frm' < pluto.frm
ssh root@192.168.254.2 update_frm.sh /tmp/pluto.frm
```

It takes about 15 minutes: wait for `Done`.

## B.4 Into DFU

Power off. Boot switches to **QSPI**: SW1 up, SW2 down (ON is grounded, so it reads back to front). Card out. Connect the board's main USB-C socket to the computer. It comes up in DFU, and `dfu-util -l` lists `boot.dfu`, `firmware.dfu`, `uboot-env.dfu` and others.

## B.5 Make the Two Files

In the `flash` folder: the boot loader without its DFU tag (the tag carries the board's normal USB ID, which a board in DFU does not answer to), and Tezuka's settings, cut out of `boot.frm`:

```
head -c 786216 boot.dfu > boot.bin
dd if=boot.frm of=uboot-env.bin bs=1 skip=786216 count=131072
strings uboot-env.bin | grep '^fit_size='
```

The last line should print `fit_size=0x1E00000`, confirming the right block. The sizes are for v0.3.24; for another release, `boot.frm` is the boot loader, then 131072 bytes of settings, then 1057 bytes of trailer.

## B.6 Write Them

```
dfu-util -d 0456:b674 -a boot.dfu -D boot.bin
dfu-util -d 0456:b674 -a uboot-env.dfu -D uboot-env.bin
dfu-util -d 0456:b674 -a uboot-env.dfu -e
```

The "invalid DFU suffix" warnings are expected. Each write ends `Download done`. Within 20 seconds the blue light comes on: Tezuka is running from flash.

## B.7 Pin the Ethernet Address

Tezuka's settings define none, so the board takes a new one and the ROCK gives it a new address (the Transmit card shows it). Off air, from the ROCK, using the address the board has now:

```
ssh root@192.168.254.3 'fw_setenv ethaddr $(cat /sys/class/net/eth0/address) && fw_printenv ethaddr modeboot'
```

`modeboot=qspiboot` confirms it booted from flash.

## B.8 Recovery

In QSPI mode a board that cannot boot falls back to DFU, so B.6 (or `dfu-util -d 0456:b674 -a firmware.dfu -D pluto.dfu` for the firmware) always gets it back, and the backup can be written the same way. The middle USB-C socket is the serial console (115200 baud, the first of its two serial ports), showing the boot messages if you ever need to see where a boot stops.

# Appendix C. Bill of Materials

Part numbers marked **TBC** are still to be filled in.

## C.1 Core

| Item | Qty | Notes |
|---|---|---|
| Radxa ROCK 5T (RK3588) | 1 | 8 GB or more. Two Ethernet ports: one for the LAN, one for the SDR |
| ROCK 5T power supply | 1 | RS PRO ECD-65-12, 12 V 5 A (60 W), C14 mains inlet. RS stock no. 304-023 |
| Heatsink for the ROCK | 1 | Radxa Heatsink 6240B (PWM fan) or 4025A (fanless). The ROCK ran at about 35 °C on the bench at 4 MS/s; in a closed cabinet, choose the fan |
| microSD card or NVMe SSD | 1 | Card: 32 GB+, A2. NVMe for a repeater (Appendix A); a 500 GB WD Blue is in use |
| SDR: PlutoSky R2 or LibreSDR | 1 | Tezuka firmware v0.3.24 or later |
| microSD card for the SDR | 1 | 8 GB or more, FAT32. Not needed on a PlutoSky running from flash (Appendix B) |
| Cat 6 patch lead | 2 | ROCK to SDR (the direct link), ROCK to the LAN |
| 5 V supply for the SDR | 1 | Raspberry Pi 5 USB-C supply (27 W, 5.1 V). Never a computer |

## C.2 Reference

| Item | Qty | Notes |
|---|---|---|
| Leo Bodnar LBE-1420 GPSDO | 1 | GNSS-disciplined 10 MHz, complete kit with antenna. The antenna needs a view of the sky |
| 10 MHz lead, MMCX male to SMA male, RG316 | 1 | From the LBE-1420's SMA output to the SDR's MMCX 10 MHz input |

## C.3 Source

| Item | Qty | Notes |
|---|---|---|
| HDMI camera or source | 1 | Up to 3840 × 2160 at 25 fps |
| HDMI lead | 1 | Into the ROCK's HDMI input |

## C.4 RF (for on-air use)

| Item | Qty | Notes |
|---|---|---|
| Band-pass filter for the band in use | 1 | TBC. Recommended after the SDR |
| Power amplifier | 1 | TBC. About 1 mW drive |
| PTT sequencer | 1 | Keyed by the SDR's PTT line (Tezuka) |
| Antenna and feeder | 1 | TBC |

## C.5 Bench Testing

| Item | Qty | Notes |
|---|---|---|
| Attenuator, 30 to 40 dB | 1 | SDR to a receiver, never without one |
| BATC PicoTuner (Ethernet) with the Lynx DATV Receiver | 1 | DVB-S2: lock and MER |
| SiliconDust HDHomeRun FLEX 4K (HDFX-4K) | 1 | ATSC 3.0 |
