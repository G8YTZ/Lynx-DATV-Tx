# Installing Lynx-Tx

On a Radxa ROCK 5T running Radxa's Debian 12 image, logged in as `radxa`. About an hour, most of it compiling.

## 1. Packages

```bash
sudo apt update
sudo apt install -y git python3-fastapi python3-uvicorn python3-yaml python3-numpy \
    ffmpeg gnuradio libiio-utils mosquitto-clients
```

**TSDuck** (`tsp`, `tstabcomp`) is not in Debian: install the arm64 `.deb` for Debian 12 from the [TSDuck releases](https://github.com/tsduck/tsduck/releases).

```bash
sudo apt install ./tsduck_*_arm64.deb
```

The hardware encoder comes from Radxa's image (`gst-inspect-1.0 mpph265enc` should list it). For hybrid rate control, apply the encoder patch in `patches/`; without it Lynx-Tx falls back to plain CBR and says so on the page.

## 2. ATSC 3.0 (optional)

DVB-S2 needs nothing more: its modulator is GNU Radio's own. ATSC 3.0 needs Ron Economos W6RZ's gr-atsc3:

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

No camera to hand? `tools/test_plutosky.sh` and `tools/test_libresdr.sh` send colour bars and a −18 dBFS tone without the rest of Lynx-Tx.

## Updating

```bash
cd ~/lynx-tx && git pull && sudo systemctl restart lynx-tx
```
