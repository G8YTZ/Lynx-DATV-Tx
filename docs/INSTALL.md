# Installing Lynx-Tx

Two stages: put Radxa's operating system on the ROCK, then run one command.

## 0. The ROCK's operating system

**You need:**

- **A Radxa ROCK 5T,** 8 GB or more.
- **A 12 V DC supply** (the ROCK takes 9 to 20 V).
- **A microSD card,** 32 GB or more and A2-rated (SanDisk Extreme, Samsung PRO Plus). Cheap cards make the install painfully slow.
- **A network cable** from the ROCK's first Ethernet port to your router. The second port is for the SDR.
- **A computer** to write the card and to log in from.

**For a repeater, or anything left running unattended:** Use an NVMe drive instead of the card (see [Running from NVMe](#running-from-nvme)). Cards wear out and fail without warning.

1. **Download the image:** From Radxa's [ROCK 5T download page](https://docs.radxa.com/en/rock5/rock5t/download), the official Debian image: `rock-5t_bookworm_kde_r7.output_512.img.xz` at the time of writing (Debian 12 "Bookworm", KDE desktop). Take that one, not Android or Armbian: Lynx-Tx needs the video encoder and HDMI capture drivers that only Radxa's own image carries.
2. **Write it to the card** with [balenaEtcher](https://etcher.balena.io) (Mac, Windows or Linux): Flash from file, choose the image, Select target, choose the card (carefully), Flash. Etcher checks the card after writing; if it reports a failure, try again or use another card.
3. **First boot:** Card into the ROCK, network cable in, then power. It takes about 40 seconds. With a screen attached you get a desktop; without one, find the ROCK in your router's list of connected devices (it is called `rock-5t`).
4. **Log in** from your computer: `ssh radxa@<the ROCK's address>`, password `radxa`. Change it straight away with `passwd`.
5. **Bring it up to date,** then restart:

   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```

Log back in after a minute, and carry on with the one command below.

### Running from NVMe

Radxa's own method: set it up on a card first as above, then move it across.

1. **Update the boot loader** so the ROCK can start from NVMe: `sudo rsetup`, then System Maintenance, Update SPI Bootloader, choose the ROCK 5T one, and confirm.
2. **Power off, fit the NVMe drive, power on** from the card. `lsblk` should list `nvme0n1`.
3. **Copy the image onto it** (put the downloaded `.img.xz` on the ROCK first; `nvme0n1` must be the drive, as this erases it):

   ```bash
   xzcat rock-5t_bookworm_kde_r7.output_512.img.xz | sudo dd of=/dev/nvme0n1 bs=1M status=progress
   ```

4. **Power off, take the card out,** and power on. It now starts from the NVMe. Carry on from step 4 above.

## The one command

```bash
curl -sSL https://raw.githubusercontent.com/G8YTZ/Lynx-DATV-Tx/main/install.sh | bash
```

That's the whole install: every package, TSDuck, the video loopback for the media player, Ron Economos W6RZ's gr-atsc3, the patched HEVC encoder, Lynx-Tx itself, the direct link to the SDR (with DHCP, so the SDR needs no setting up) and the service that starts it at boot. About an hour on a fresh ROCK, most of it compiling. It finishes by printing the page's address, and lists anything that needs a look.

Running it again is safe: it only updates the code and fills in anything missing.

Then go to [6. First transmission](#6-first-transmission).

## By hand

What the script does, step by step, for anyone who wants to see inside or fix a step that failed.

## 1. Packages

```bash
sudo apt update
sudo apt install -y git curl jq python3-fastapi python3-uvicorn python3-pydantic python3-yaml \
    python3-numpy python3-gi ffmpeg v4l-utils gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav gir1.2-gstreamer-1.0 \
    gnuradio gnuradio-dev libiio-utils mosquitto-clients cmake build-essential pkg-config \
    pybind11-dev python3-pybind11 libspdlog-dev libgmp-dev liborc-0.4-dev network-manager dnsmasq-base
```

**Video loopback** for the built-in media player (`/dev/video10`, up to 4K):

```bash
sudo apt install -y linux-headers-$(uname -r) v4l2loopback-dkms
echo v4l2loopback | sudo tee /etc/modules-load.d/lynx-tx.conf
echo 'options v4l2loopback video_nr=10 max_width=3840 max_height=2160 card_label="Lynx Deck"' | \
    sudo tee /etc/modprobe.d/lynx-tx-v4l2loopback.conf
sudo modprobe v4l2loopback
```

**TSDuck** (`tsp`, `tstabcomp`) is not in Debian: install the arm64 `.deb` for Debian 12 from the [TSDuck releases](https://github.com/tsduck/tsduck/releases).

```bash
sudo apt install ./tsduck_*_arm64.deb
```

The hardware encoder comes from Radxa's image (`gst-inspect-1.0 mpph265enc` should list it). Hybrid rate control needs it rebuilt with the patch in `patches/`, which lets the quality floor change on the fly; without it Lynx-Tx falls back to plain CBR and says so on the page. The rebuild is step 6 of `install.sh`: it unpacks GStreamer's headers privately (installing them properly would disturb Radxa's graphics libraries), builds Radxa's own plugin source with the patch, and keeps the original as `libgstrockchipmpp.so.stock`.

## 2. The ATSC 3.0 modulator

Lynx-Tx is a multi-mode transmitter, and every mode is part of the install. Both modulators are Ron Economos W6RZ's work. DVB-S2 uses his blocks in gr-dtv, which ship with GNU Radio and are installed above. ATSC 3.0 uses his gr-atsc3, built from source (about half an hour on the ROCK):

```bash
sudo apt install -y gnuradio-dev cmake build-essential pybind11-dev python3-pybind11
git clone https://github.com/drmpeg/gr-atsc3 ~/src/gr-atsc3
cd ~/src/gr-atsc3 && mkdir -p build && cd build && cmake .. && make -j8 && sudo make install && sudo ldconfig
```

## 3. Lynx-Tx

```bash
git clone https://github.com/G8YTZ/Lynx-DATV-Tx ~/lynx-tx
cd ~/lynx-tx
mkdir -p config && cp lynx_tx.example.yaml config/lynx_tx.yaml
sudo mkdir -p /srv/lynxdeck/media && sudo chown radxa: /srv/lynxdeck/media
```

Edit `config/lynx_tx.yaml`: callsign and provider at least. Everything else can be set from the page.

**Restart and Shut down buttons:** One sudoers rule, allowing exactly these two commands:

```bash
echo 'radxa ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff' | sudo tee /etc/sudoers.d/lynx-tx
sudo chmod 440 /etc/sudoers.d/lynx-tx
sudo visudo -c
```

## 4. The direct link to the SDR

The supported connection: the SDR on its own Cat 6 cable to the ROCK's **second** Ethernet port (the RJ45 at the far edge of the board, next to the other one).

![ROCK 5T connections](img/rock5t-ports.jpg)

Find the port's name with the cable in and the SDR powered: it is the one showing `UP` that is not your LAN.

```bash
ip -br link
```

On the ROCK 5T it is usually `enP4p65s0`. The ROCK takes 192.168.254.1 on it and hands the SDR its address by DHCP, so the SDR needs no setting up. One address in the pool, so the SDR is always 192.168.254.2:

```bash
echo 'dhcp-range=192.168.254.2,192.168.254.2,12h' | \
    sudo tee /etc/NetworkManager/dnsmasq-shared.d/lynx-sdr.conf
sudo nmcli con add type ethernet ifname enP4p65s0 con-name lynx-sdr \
    ipv4.method shared ipv4.addresses 192.168.254.1/24 ipv6.method disabled
sudo nmcli con up lynx-sdr
```

If your port has another name, put it in the config too:

```yaml
libre:
  direct_if: enP4p65s0
  direct_host: 192.168.254.2
```

Then set the SDR to 192.168.254.2: see [SDR_SETUP.md](SDR_SETUP.md). Check from the ROCK:

```bash
ping -c 3 192.168.254.2
iio_attr -u ip:192.168.254.2 -C
```

The second command prints the board's model and firmware. That is what Lynx-Tx reads to find it.

## 5. Run it

By hand:

```bash
cd ~/lynx-tx && python3 lynx_tx.py
```

As a service that starts at boot, `/etc/systemd/system/lynx-tx.service`:

```ini
[Unit]
Description=Lynx-Tx
After=network-online.target
Wants=network-online.target

[Service]
User=radxa
WorkingDirectory=/home/radxa/lynx-tx
ExecStart=/usr/bin/python3 /home/radxa/lynx-tx/lynx_tx.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now lynx-tx
```

Open `http://<rock address>:8080`. The SDR should appear on the Transmit card with its model and firmware.

## 6. First transmission

On a cable, through a 30 to 40 dB attenuator into a receiver:

1. **Mode:** DVB-S2. **SDR:** your board. **Modulator:** ROCK.
2. **Modulation:** 1249 MHz, 2000 kS/s, QPSK, 3/4, pilots on, roll-off 0.35, power −20 dB.
3. **TX On.** The receiver should lock within a couple of seconds.

Media files to play out: Upload them on the page's **Manage media** link. [MEDIA.md](MEDIA.md) says how to prepare them.

No camera to hand? `tools/test_plutosky.sh` and `tools/test_libresdr.sh` send colour bars and a −18 dBFS tone without the rest of Lynx-Tx.

## Updating

Run the one command again, or for the code alone:

```bash
cd ~/lynx-tx && git pull && sudo systemctl restart lynx-tx
```
