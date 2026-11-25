title: Lynx
subtitle: DATV Transmitter (Lynx-Tx)
tagline: Monitoring Guide: Alarms, SNMP and Home Assistant
version: Version 1.2  ·  G8YTZ / EI3IOB  ·  November 2026

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

Everything is in one ready-made package, `docs/home-assistant/lynx_tx.yaml`: all the sensors below and one alarm automation that sends to your phone.

- **Once,** in `configuration.yaml`, turn packages on:

```
homeassistant:
  packages: !include_dir_named packages
```

- **Copy** `lynx_tx.yaml` to `/config/packages/`.
- **Edit two things:** Replace `192.168.0.50` (every `host:`) with the ROCK's LAN address, and `notify.mobile_app_your_iphone` with your own phone's notify service.
- **Restart** Home Assistant (Settings, System, Restart).

The sensors it makes:

| Sensor | Shows |
|---|---|
| `sensor.lynx_tx_state` | idle, on air, muted by the SDR, fault, lockout |
| `sensor.lynx_tx_reference` | locked, acquiring, holdover, absent |
| `sensor.lynx_tx_camera` | live, colour bars, unstable |
| `sensor.lynx_tx_pa_relays` | on, off, not answering, not configured, partly on |
| `sensor.lynx_tx_forward_power` | W |
| `sensor.lynx_tx_drive_control` | off, standby, settling, tracking, on target, at maximum drive, at minimum drive, held |
| `sensor.lynx_tx_drive` | dB |
| `sensor.lynx_tx_power_lock` | locked, unlocked |
| `sensor.lynx_tx_pa_fault` | none, fault |
| `sensor.lynx_tx_rock_temperature`, `_fan`, `_uptime`, `_load`, `_free_space` | The ROCK's own health |

The alarms, and how long each has to hold before it is sent:

| Alarm | Waits | Why |
|---|---|---|
| Lockout | At once | It needs you |
| PA fault | At once | TX On was refused or the PA was shut down: relays did not confirm, or no forward power |
| Drive control held | At once | Sudden fall in power, lost reading or poor return loss |
| Muted or faulty | 90 s | Long enough for auto-recover to put it right first |
| Forward power low on air | 2 min | Under 30 W while on air: set it to about three quarters of your target |
| PA relay board not answering, or relays partly on | 2 min | |
| 10 MHz reference in holdover | 1 min | The GPS reference is lost |
| Camera unstable | 1 min | |
| At maximum drive | 30 min | The PA cannot reach its target: if it keeps happening, the PA is losing gain |
| ROCK too hot (over 60 °C) | 5 min | The fan is not coping |
| ROCK drive nearly full (under 2 GB) | At once | |
| ROCK flat out (load over 80%) | 10 min | |

Each message says which alarm it is, with the sensor's state, for example "PA fault: TX On refused or the PA shut down (relays or forward power) (Lynx-Tx PA fault: fault)".

> The forward power low alarm is the in-service check: Lynx-Tx checks the forward power at key-up, Home Assistant watches it the rest of the time.

Worth graphing: **Lynx-Tx drive** over months. At the same forward power, a slow rise is the PA losing gain, long before anything fails. The Automatic Drive Control Guide explains the PA objects in full.

# 6. Document Version History

| Version | Date | Changes |
|---|---|---|
| 1.0 | October 2026 | First edition, from the Markdown page: SNMP objects, auto-recover and lockout, Home Assistant sensors and alarm |
| 1.1 | 23 October 2026 | The ROCK's own health: fan speed, uptime, load and free space (objects 15 to 18), with Home Assistant sensors and alarms |
| 1.2 | 25 November 2026 | PA control and Automatic Drive Control (objects 19 to 24); Home Assistant as one ready-made package, `docs/home-assistant/lynx_tx.yaml`, with PA, drive control and in-service forward power alarms; updating the agent |
