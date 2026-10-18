# Setting up the SDR

Lynx-Tx uses the SDR as a plain IQ transmitter: the ROCK makes DVB-S2 or ATSC 3.0 and the board only converts it to RF. Any Pluto-class board running iiod works. Two are proven: the **PlutoSky R2** and the **LibreSDR**.

**Recommended firmware: [Tezuka](https://github.com/F5OEO/tezuka_fw)** by Evariste F5OEO, on both boards. It keys PTT by itself whenever IQ is flowing, so the same setup works for either board.

## Making the SD card

1. **Open the releases page:** [github.com/F5OEO/tezuka_fw/releases](https://github.com/F5OEO/tezuka_fw/releases). The newest release is at the top.
2. **Find the files:** Scroll down past the release notes to **Assets**. Only the first few files show: click **Show all assets** to see the rest.
3. **Download the zip named after your board.** It must match the board: the wrong one drives the wrong pins.

   | Board | Zip |
   |---|---|
   | PlutoSky R2 | `plutoskyr2` |
   | LibreSDR | `libre` |

4. **Format a microSD card as FAT32** (on a Mac: Disk Utility, MS-DOS (FAT), Master Boot Record).
5. **Unzip, and copy the contents of the `sdimg` folder to the root of the card:** `BOOT.bin`, `devicetree.dtb`, `uImage` and the rest sit at the top level, not in a folder.
6. **Keep the board's original card:** Put it away as your way back.

Do not use the `.frm` flashing method: Tezuka boots from the card.

## First boot: setting the address

Tezuka keeps its settings in the board's own flash, not on the SD card, so they cannot be put on the card when you make it. You set them once, from a computer, through the board's USB drive.

1. **Card in, USB lead from the board to your computer.** The board powers up from USB. After a minute or so a drive called **PlutoSDR** appears.
2. **Check it is the right board:** Open `info.html` on the drive. It shows the model, the firmware version and the addresses.
3. **Open `config.txt` on the drive** in a plain text editor (TextEdit on a Mac, Notepad on Windows) and change these lines, leaving everything else alone:

   ```ini
   [USB_ETHERNET]
   ipaddr_eth = 192.168.254.2
   netmask_eth = 255.255.255.0
   gateway_eth =

   [SYSTEM]
   usb_ethernet_mode = ecm
   ```

   `gateway_eth` stays empty on purpose: the direct link goes nowhere else. `ecm` lets a Mac use the USB network; Windows and Linux work with it too.
4. **Save, then eject the drive.** Ejecting is what applies the settings: the drive disappears for a few seconds while the board writes them to flash, then comes back.
5. **Restart the board:** Unplug the USB lead and plug it in again. (Unplugging Ethernet or the 10 MHz does not restart it.)
6. **Check:** Open `info.html` again. **ETH IP** should now read 192.168.254.2.

Then connect the board's Ethernet to the ROCK's second port and check from the ROCK (see [Checking a board from the ROCK](#checking-a-board-from-the-rock)).

**Logging in:** If you ever need it, the board answers SSH on its USB address (`ssh root@192.168.2.1`), password `analog`.

**If your home network uses 192.168.2.x:** The board's USB address clashes with it and you will reach your router instead of the SDR. Move it in `config.txt` under `[NETWORK]`: `ipaddr = 192.168.12.1` and `ipaddr_host = 192.168.12.10`.

## PlutoSky R2 notes

Proven 18 October 2026 with Tezuka v0.3.5 (April 2026). v0.3.141592653 (July 2026) has been inspected but not yet run.

- **Ethernet on v0.3.5 receives but never transmits:** No DHCP lease, no ping, a 169.254.x.x address. The supplied `devicetree.dtb` has `phy-mode = "rgmii-rxid"`; this board needs `"rgmii-id"`. Fixed in v0.3.141592653. On v0.3.5, replace `devicetree.dtb` on the SD card with a corrected one (keep the original):

  ```bash
  dtc -I dtb -O dts devicetree.dtb -o dt.dts
  sed -i 's/rgmii-rxid/rgmii-id/' dt.dts
  dtc -I dts -O dtb dt.dts -o devicetree.dtb
  ```

- **Settings on v0.3.5 live on the SD card** (`uboot.env`), and `uEnv.txt` sets the USB address on every boot. A `config.txt` change can be lost if the power goes too soon. The reliable way: over SSH, `fw_setenv ...`, edit `/boot/uEnv.txt` if the USB address is changing, then `sync`, then unplug. Newer Tezuka keeps settings in the board's flash.
- **Internal reference:** 40 MHz, about 10 ppm high on one board (13 kHz at 1249 MHz). It locks without a reference, but feed it 10 MHz for an on-frequency signal: the board switches to an external 10 MHz by itself.
- **Password refused?** A password saved in the board's flash overrides `analog`. Add `revert_passwd = 1` under `[ACTIONS]` in `config.txt`, eject, then restart.
- **SD socket:** The push-push latch can let the card ride out. Push it fully home before powering up.

## LibreSDR notes

- **With Tezuka:** Set it up exactly as the PlutoSky above. Use a new card and keep the board's original, so its old firmware is a card swap away. Its device tree is already `rgmii-id`, and settings live in the board's flash, not on the card.
- **Overclock:** The card's `overclock` folder holds faster boot files. Leave them alone unless the Ethernet stalls at high symbol rates; the CPU950 one is the gentle first step.
- **With DL1JM's firmware:** Lynx-Tx can still use it two ways. As an IQ transmitter (Modulator: ROCK), Lynx-Tx stops DL1JM's modulator for the duration; **PTT is not keyed** in this mode. Or with DL1JM's own modulator (Modulator: LibreSDR on board), the original path, where the firmware keys PTT from the power setting.

## Checking a board from the ROCK

```bash
iio_attr -u ip:192.168.254.2 -C                       # model and firmware
iio_attr -u ip:192.168.254.2 -o -c ad9361-phy voltage0 sampling_frequency
```

## Bench test without Lynx-Tx

```bash
cd ~/lynx-tx
./tools/test_plutosky.sh            # or test_libresdr.sh
./tools/test_plutosky.sh --sr 4000  # any option overrides
python3 tools/dvbs2_iq.py --uri ip:192.168.254.2 --freq 1249 --tone 100   # a tone 100 kHz above, to zero-beat
```

These scripts assume the SDR addresses in the scripts themselves; edit `--uri` to suit.

## Receiving

The Picotuner (Serit tuner) locks to Lynx-Tx's DVB-S2 at roll-off 0.35, **not 0.20** (0.25 not yet tried). It also will not declare lock on a stream of nothing but null packets: always send a real programme.
