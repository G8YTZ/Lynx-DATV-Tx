title: Lynx
subtitle: DATV Transmitter (Lynx-Tx)
tagline: Monitoring Guide: Alarms, SNMP and Home Assistant
version: Version 1.3  ·  G8YTZ / EI3IOB  ·  November 2026

# 1. Overview

Lynx-Tx answers SNMP, so Home Assistant (or any monitoring system) can watch the transmitter and send you an alarm. It is read-only, and nothing on the transmit path depends on it: if monitoring stops, the transmitter carries on.

`install.sh` sets it all up: `snmpd` on the ROCK, the agent (`tools/lynx-snmp-agent.py`) and the MIB (`mibs/LYNX-MIB.txt`).

| Setting | Value |
|---|---|
| Version | SNMP v2c |
| Community | `lynx` (read-only) |
| Port | UDP 161 on the ROCK's LAN address |
| Branch | `1.3.6.1.4.1.8072.9999.1.1` (lynxTx), each value at `.0` |

The branch is Net-SNMP's test branch until the repeater group has its own Private Enterprise Number from IANA; then the numbers change in one place.

# 2. What It Reports

| OID (under `…8072.9999.1.1`) | Object | Values | Alarm when |
|---|---|---|---|
| `.1.0` | lynxTxState | 1 idle, 2 on air, 3 muted by the SDR, 4 fault, 5 lockout | 5 at once; 3 or 4 if they last |
| `.2.0` | lynxSdrReachable | 1 yes, 2 no | 2 |
| `.3.0` | lynxRefState | 1 locked, 2 acquire, 3 holdover, 4 absent, 5 unknown | 3 (the GPS reference is lost) |
| `.4.0` | lynxMixerRunning | 1 yes, 2 no | 2 |
| `.5.0` | lynxHdmiSignal | 1 yes, 2 no | 2, if a camera should be there |
| `.6.0` | lynxTxFrequency | kHz | |
| `.7.0` | lynxTxPower | hundredths of a dB (−2000 = −20.00 dB) | |
| `.8.0` | lynxMode | DVB-S2 or ATSC 3.0 | |
| `.9.0` | lynxSdrMutes | times the SDR has muted itself | it goes up |
| `.10.0` | lynxMemAnonMB | real memory held by Lynx-Tx, MB | above about 1500 |
| `.11.0` | lynxCmaFreeMB | free 4K buffer memory, MB | (informational: the cache borrows it) |
| `.12.0` | lynxCpuTemp | tenths of a °C (held at about 400 by the fan control) | above 600 (60 °C): the fan is not coping; the CPU throttles at 85 °C |
| `.13.0` | lynxCameraState | 1 live, 2 colour bars, 3 unstable, 4 unknown | 3 |
| `.14.0` | lynxAutoRecoveries | times auto-recover has restarted the transmitter | it goes up |
| `.15.0` | lynxFanPercent | fan speed, % (0 off, 50 slowest, 100 full) | 100 while the temperature is normal: the fan control has stopped |
| `.16.0` | lynxUptime | seconds since the ROCK started | it drops: the ROCK restarted |
| `.17.0` | lynxCpuLoad | one-minute load, % of all cores (about 20 on air at 4K) | 80 or more for 10 minutes |
| `.18.0` | lynxDiskFreeMB | free space on the system drive, MB | below 2000 |
| `.19.0` | lynxPaRelays | 1 on, 2 off, 3 not answering, 4 not configured, 5 partly on | 3 or 5 if they last |
| `.20.0` | lynxFwdPower | forward power, tenths of a W (405 = 40.5 W); 0 when not read | low while on air |
| `.21.0` | lynxDriveState | 1 off, 2 standby, 3 settling, 4 tracking, 5 on target, 6 at maximum, 7 at minimum, 8 held | 8 at once; 6 if it lasts |
| `.22.0` | lynxDrive | the SDR's drive, hundredths of a dB (−1250 = −12.50 dB) | (trend: a slow rise is the PA losing gain) |
| `.23.0` | lynxPowerLock | 1 locked, 2 unlocked | |
| `.24.0` | lynxPaFault | 1 none, 2 fault (relays did not confirm, or no forward power at key-up) | 2 |

**Muted by the SDR:** Lynx-Tx thinks it is on air, but the SDR has turned its transmitter off because its IQ stopped arriving (see the Installation Guide, Section 5.4). TX Off then TX On restores it. **Fault:** The transmit chain stopped by itself, or Lynx-Tx is not answering at all.

# 3. Auto-recover and Lockout

If you pressed TX On and the transmitter then goes muted or faulty, Lynx-Tx restarts it by itself once the SDR has answered for 30 seconds, like a power network's auto-reclose. After 3 restarts within 60 minutes it gives up: **lockout** (red on the page) until someone presses TX On. TX Off always means off. To change the numbers, in `config/lynx_tx.yaml`:

```yaml
auto_recover:
  enabled: true
  settle_s: 30
  max_tries: 3
  window_min: 60
```

# 4. Checking It from the ROCK

**After an update:** The installer copies the agent and the MIB into place, so a `git pull` alone does not bring new objects. Run the installer again, or just:

```
cd ~/lynx-tx
sudo install -D -m 755 tools/lynx-snmp-agent.py /usr/local/lib/lynx-tx/lynx-snmp-agent.py
sudo install -D -m 644 mibs/LYNX-MIB.txt /usr/share/snmp/mibs/LYNX-MIB.txt
sudo systemctl restart snmpd
```


```bash
sudo apt install -y snmp
snmpwalk -v2c -c lynx -On localhost .1.3.6.1.4.1.8072.9999.1.1 2>/dev/null
```

Twenty-four lines, by number. Debian's `snmpwalk` comes without the standard IETF MIB files, so names (`LYNX-MIB::lynxTx`) only work after `sudo apt install snmp-mibs-downloader && sudo download-mibs` and commenting out the `mibs :` line in `/etc/snmp/snmp.conf`; without them it prints pages of "Cannot find module" warnings, which are harmless. Home Assistant uses the numbers and needs none of this.

# 5. Home Assistant

Three ready-made files, in `docs/home-assistant/`, are GB3OO's own working Home Assistant set-up, so they are tested on air rather than written for the guide:

| File | What it is | Where it goes |
|---|---|---|
| `lynx_tx_configuration.yaml` | The sensors: Lynx-Tx over SNMP, the PA's Modbus relay board, and optionally the Lynx Deck and an APC UPS; the forward power in watts; and every state in words, for the tiles and alarms | Merged into `configuration.yaml` |
| `lynx_tx_dashboard.yaml` | One complete dashboard page (below) | A view, in the dashboard's raw configuration editor |
| `lynx_tx_alarms.yaml` | One automation with every alarm, sent to your phone | Settings, Automations, Edit in YAML |

![GB3OO's Lynx-Tx page in Home Assistant](img/ha_dashboard.png){6.3}

## 5.1 Making Them Yours

- **Search for** `CHANGE-ME` **in all three files.** Every value specific to GB3OO is marked with it: the addresses of the ROCK (192.168.60.100), the Modbus relay board (.75), the Lynx Deck (.117) and the UPS (.11); the Modbus register and coil numbers; the forward power detector's calibration; the gauge ranges; the low-power threshold; and your phone's notify service.
- **Change the callsign** with search and replace across all three files, matching case: `GB3OO` to yours in capitals, and `gb3oo` to yours in lower case. Home Assistant makes the entity IDs from the names (`sensor.gb3oo_lynx_tx_state`), and the page and the alarms use those IDs, so all three must agree.
- **Merge, do not paste over:** If your `configuration.yaml` already has `modbus:`, `sensor:` or `template:`, put these entries under your existing ones. Each key may appear only once.
- **Leave out what you do not have:** The Lynx Deck and UPS blocks are optional. Delete them from the configuration, and their sections from the page.
- **Then:** Developer tools, YAML, Check configuration, and Settings, System, Restart. The SNMP and Modbus sensors load only when Home Assistant starts. Automations and the page take effect as soon as they are saved.

## 5.2 The Page

Three columns:

- **Lynx-Tx:** Transmitter, 10 MHz reference, SDR, camera, mixer and HDMI input as tiles that change colour with their state (green, amber, red, grey); mode, frequency and power setting; then **PA and drive control**: forward power, drive, drive control, PA fault and power lock; the ROCK's temperature, fan and load; the counters; a 24-hour history of the states; and a 48-hour graph of forward power, drive and the PA heatsink together. That last one is Automatic Drive Control at a glance: as the heatsink warms, the drive rises and the forward power stays flat.
- **Lynx Deck:** What is playing, what the schedule says, the player's health and counters.
- **PA and case:** Buttons for the PA and ATEM relays (with a confirmation), gauges for the PA case, heatsink, drive case, forward power, the ROCK's core and fan, and the UPS.

## 5.3 The Alarms

| Alarm | Waits | Notification |
|---|---|---|
| ROCK not answering | 3 min | Service affected |
| Off air (left On Air and stayed off) | 1 min | Service affected |
| Lockout | At once | Service affected |
| Muted or faulty | 90 s | Service affected |
| PA fault | At once | Service affected |
| Drive control held | At once | Service affected |
| Forward power low on air (under 30 W) | 2 min | Service affected |
| PA relay board not answering, or relays partly on | 2 min | Service affected |
| ROCK at 75 °C | 1 min | Service affected |
| 10 MHz reference in holdover | 1 min | Warning |
| Camera HDMI unstable | 1 min | Warning |
| Drive control at maximum drive | 30 min | Warning |
| ROCK over 60 °C | 5 min | Warning |
| ROCK drive nearly full (under 2 GB) | At once | Warning |
| ROCK flat out (load over 80%) | 10 min | Warning |

"Service affected" arrives as a time-sensitive notification, which gets through Focus on an iPhone; a warning as an ordinary one. Each alarm has its own tag, so a repeat replaces the last rather than piling up. The message says what happened, with the figures, for example "Drive control held at −13.25 dB: sudden fall in power, lost reading or poor return loss. Forward power 22.1 W."

> The forward power low alarm is the in-service check: Lynx-Tx checks the forward power at key-up, Home Assistant watches it the rest of the time. It reads the Modbus board directly, so it works even if Lynx-Tx's own PA settings are wrong.

> A development or bench transmitter alarms every time you key down. Keep a second copy of the automation for it, and disable it except when that transmitter is in service.

## 5.4 The Word Sensors

The tiles and alarms work from words rather than numbers. Each SNMP number is turned into a word by a template sensor in `lynx_tx_configuration.yaml`, for example `sensor.gb3oo_lynx_tx_state`: Idle, On Air, Muted by SDR, Fault or Lockout. The healthy PA fault state is "OK", not "None": Home Assistant reads a template that outputs "None" as having no value at all.

# 6. Document Version History

| Version | Date | Changes |
|---|---|---|
| 1.0 | October 2026 | First edition, from the Markdown page: SNMP objects, auto-recover and lockout, Home Assistant sensors and alarm |
| 1.1 | 23 October 2026 | The ROCK's own health: fan speed, uptime, load and free space (objects 15 to 18), with Home Assistant sensors and alarms |
| 1.2 | 25 November 2026 | PA control and Automatic Drive Control (objects 19 to 24); Home Assistant as one ready-made package, `docs/home-assistant/lynx_tx.yaml`, with PA, drive control and in-service forward power alarms; updating the agent |
| 1.3 | 27 November 2026 | Home Assistant from GB3OO's working set-up: the configuration, a complete dashboard page and the alarms, as three files with `CHANGE-ME` marking every value to change |
