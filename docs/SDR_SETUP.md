# Setting up the SDR

Lynx-Tx uses the SDR as a plain IQ transmitter: the ROCK makes DVB-S2 or ATSC 3.0 and the board only converts it to RF. Any Pluto-class board with Gigabit Ethernet and running iiod works (not the original ADALM-Pluto, which has only USB). Two are proven: the **PlutoSky R2** and the **LibreSDR**.

**Preferred: the PlutoSky R2 with [Tezuka](https://github.com/F5OEO/tezuka_fw) firmware** by Evariste F5OEO, v0.3.24 or later. It locks the board's crystal to a 10 MHz reference and reports that lock to the page's **10 MHz Reference** lamp. You only make the card and plug the board in: Lynx-Tx prepares it by itself the first time it sees it (see [What Lynx-Tx does to a PlutoSky](#what-lynx-tx-does-to-a-plutosky)).

## Making the SD card

1. **Open the releases page:** [github.com/F5OEO/tezuka_fw/releases](https://github.com/F5OEO/tezuka_fw/releases). The newest release is at the top; it must be **v0.3.24 or later** (the card supplied with the board is older and will not do).
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

## Connecting it

Nothing to set on the board. Out of the box Tezuka asks for an address by DHCP, and the ROCK's second port answers with 192.168.254.2 (see [INSTALL.md](INSTALL.md)).

1. **Card in.**
2. **Ethernet** from the board to the ROCK's second port.
3. **Power:** A USB power supply into the board's USB socket. Not a computer: it is only power.
4. **Check:** Within a minute or so the board appears on the Transmit card with its model and firmware. Or from the ROCK, see [Checking a board from the ROCK](#checking-a-board-from-the-rock).
5. **First time only:** Lynx-Tx prepares a PlutoSky as soon as it finds it, off air. The Transmit card says "SDR: Lynx-Tx script installed - the SDR is restarting (about a minute)", the board restarts once, and it is ready. Nothing to type.

**If you need to get into the board:** Plugged into a computer's USB, it appears as a drive called **TEZUKA** (**PlutoSDR** on older firmware) holding `info.html` (model, firmware, addresses) and `config.txt` (settings: edit, save, then eject the drive to apply). It also answers SSH on 192.168.2.1, login `root`, password `analog`. On a Mac, set `usb_ethernet_mode = ecm` under `[SYSTEM]` for SSH over USB to work.

## What Lynx-Tx does to a PlutoSky

Tezuka is a do-everything SDR: receiver, waterfall, web control, DATV modes. As a dedicated wideband transmitter three of those got in the way of ATSC 3.0 (found 19 October 2026), so Lynx-Tx installs one script in the board's own flash (`/mnt/jffs2/autorun.sh`, from `tools/plutosky-autorun.sh`). It survives a card change, and a newer Lynx-Tx updates it by itself. What it does:

- **Quiet mode:** Stops Tezuka's extras, which starved the board's processor at ATSC 3.0 rates (the DAC ran out of samples: combing, lost lock).
- **The 10 MHz reference, held:** Tezuka's automatic mode re-checks the reference once a second and each check nudges the oscillator, which ATSC 3.0 cannot tolerate. The script holds the loop locked while the 10 MHz is there, lets go within 2 s if it disappears (holdover on the board's own oscillator: still on air, a few hundred hertz adrift), and locks again once it has been back for 10 s.
- **Starvation mute:** If the IQ stops arriving (the ROCK restarting or failing, a cable out) the board turns its transmitter off within 2 s, rather than radiating junk. Lynx-Tx turns it back on at the next TX On.
- **The ROCK's SSH key,** so Lynx-Tx never needs a password. It is put there once with Tezuka's factory password (`analog`).

To give the board back to plain Tezuka: over SSH, `rm /mnt/jffs2/autorun.sh`, then restart it.

**Power for ATSC 3.0: −20 dB or lower** on the PlutoSky. At −12 dB the transmitter squashes the OFDM peaks: the receiver locks but cannot decode.

## PlutoSky R2 notes

Proven 19 October 2026 with Tezuka v0.3.24: ATSC 3.0 at 2× oversampling clean, DVB-S2 to 4000 kS/s.

- **The supplied card (Tezuka v0.3.5) is too old:** Make a new one (above). On v0.3.5 the Ethernet also receives but never transmits: No DHCP lease, no ping, a 169.254.x.x address. The supplied `devicetree.dtb` has `phy-mode = "rgmii-rxid"`; this board needs `"rgmii-id"`. Fixed in v0.3.141592653. On v0.3.5, replace `devicetree.dtb` on the SD card with a corrected one (keep the original):

  ```bash
  dtc -I dtb -O dts devicetree.dtb -o dt.dts
  sed -i 's/rgmii-rxid/rgmii-id/' dt.dts
  dtc -I dts -O dtb dt.dts -o devicetree.dtb
  ```

- **Settings on v0.3.5 live on the SD card** (`uboot.env`), and `uEnv.txt` sets the USB address on every boot. A `config.txt` change can be lost if the power goes too soon. The reliable way: over SSH, `fw_setenv ...`, edit `/boot/uEnv.txt` if the USB address is changing, then `sync`, then unplug. Newer Tezuka keeps settings in the board's flash.
- **Internal reference:** 40 MHz, about 10 ppm high on one board (13 kHz at 1249 MHz). It works without a reference, but feed it 10 MHz for an on-frequency signal; the Lynx-Tx script locks to it by itself.
- **Password refused?** A password saved in the board's flash overrides `analog`. Add `revert_passwd = 1` under `[ACTIONS]` in `config.txt`, eject, then restart.
- **SD socket:** The push-push latch can let the card ride out. Push it fully home before powering up.

## LibreSDR notes

- **With Tezuka (preferred):** Set it up exactly as the PlutoSky above. Use a new card and keep the board's original, so its old firmware is a card swap away. Its device tree is already `rgmii-id`, and settings live in the board's flash, not on the card.
- **Overclock:** The card's `overclock` folder holds faster boot files. Leave them alone unless the Ethernet stalls at high symbol rates; the CPU950 one is the gentle first step.
- **Coming from DL1JM's firmware:** DL1JM's firmware leaves `mode=2r2t` (two transmit channels) in the board's flash, and Tezuka obeys it: the signal comes out twice as wide and never locks. Fix it once over SSH: `fw_setenv mode 1r1t`, `sync`, `reboot`.
- **ATSC 3.0 on Tezuka:** Solved on the PlutoSky (see [What Lynx-Tx does to a PlutoSky](#what-lynx-tx-does-to-a-plutosky)). The LibreSDR on Tezuka has the same three problems, but Lynx-Tx does not yet prepare it (its reference control differs): for ATSC 3.0 on a LibreSDR use DL1JM's firmware (`mode=2r2t`), which is clean.
- **With DL1JM's firmware:** Supported, for a LibreSDR that also works with Portsdown, which uses DL1JM's firmware. Keep each on its own card and swap as needed. On DL1JM's firmware the 10 MHz Reference lamp reads "not reported", because the firmware cannot say. Lynx-Tx finds it on the direct link like any other (it lists as "LibreSDR (DL1JM firmware)") and uses it two ways. As an IQ transmitter (Modulator: ROCK, the recommended way), Lynx-Tx stops DL1JM's modulator for the duration; **PTT is not keyed** in this mode, so key your PA another way. Or with DL1JM's own modulator (Modulator: LibreSDR on board), the original path, where the firmware keys PTT from the power setting.

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

Both scripts send to 192.168.254.2. If the ROCK gave your board another address (the Transmit card shows it), add `--uri ip:<address>`.

## Receiving

The Picotuner (Serit tuner) locks to Lynx-Tx's DVB-S2 at roll-off 0.35, **not 0.20** (0.25 not yet tried). It also will not declare lock on a stream of nothing but null packets: always send a real programme.
