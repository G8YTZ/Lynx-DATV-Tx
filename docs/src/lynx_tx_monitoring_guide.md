title: Lynx
subtitle: DATV Transmitter (Lynx-Tx)
tagline: Monitoring Guide: Alarms, SNMP and Home Assistant
version: Version 1.0  ·  G8YTZ / EI3IOB  ·  October 2026

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
| `.12.0` | lynxCpuTemp | tenths of a °C | above 850 (85 °C) |
| `.13.0` | lynxCameraState | 1 live, 2 colour bars, 3 unstable, 4 unknown | 3 |
| `.14.0` | lynxAutoRecoveries | times auto-recover has restarted the transmitter | it goes up |

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

```bash
sudo apt install -y snmp
snmpwalk -v2c -c lynx -On localhost .1.3.6.1.4.1.8072.9999.1.1 2>/dev/null
```

Fourteen lines, by number. Debian's `snmpwalk` comes without the standard IETF MIB files, so names (`LYNX-MIB::lynxTx`) only work after `sudo apt install snmp-mibs-downloader && sudo download-mibs` and commenting out the `mibs :` line in `/etc/snmp/snmp.conf`; without them it prints pages of "Cannot find module" warnings, which are harmless. Home Assistant uses the numbers and needs none of this.

# 5. Home Assistant

Sensors, in `configuration.yaml` (put the ROCK's LAN address in `host`):

```yaml
sensor:
  - platform: snmp
    name: Lynx-Tx state
    unique_id: lynx_tx_state
    host: 192.168.0.50
    community: lynx
    version: "2c"
    baseoid: 1.3.6.1.4.1.8072.9999.1.1.1.0
    value_template: >-
      {{ {'1': 'idle', '2': 'on air', '3': 'muted by the SDR', '4': 'fault', '5': 'lockout'}.get(value, 'unknown') }}

  - platform: snmp
    name: Lynx-Tx reference
    unique_id: lynx_tx_reference
    host: 192.168.0.50
    community: lynx
    version: "2c"
    baseoid: 1.3.6.1.4.1.8072.9999.1.1.3.0
    value_template: >-
      {{ {'1': 'locked', '2': 'acquiring', '3': 'holdover', '4': 'absent'}.get(value, 'unknown') }}

  - platform: snmp
    name: Lynx-Tx camera
    unique_id: lynx_tx_camera
    host: 192.168.0.50
    community: lynx
    version: "2c"
    baseoid: 1.3.6.1.4.1.8072.9999.1.1.13.0
    value_template: >-
      {{ {'1': 'live', '2': 'colour bars', '3': 'unstable'}.get(value, 'unknown') }}

  - platform: snmp
    name: Lynx-Tx ROCK temperature
    unique_id: lynx_tx_temp
    host: 192.168.0.50
    community: lynx
    version: "2c"
    baseoid: 1.3.6.1.4.1.8072.9999.1.1.12.0
    unit_of_measurement: "°C"
    value_template: "{{ (value | int / 10) | round(1) }}"
```

The notification to your phone, in `automations.yaml` (use your own phone's notify service):

```yaml
- alias: Lynx-Tx alarm
  mode: queued
  trigger:
    - platform: state
      entity_id: sensor.lynx_tx_state
      to: "lockout"
    - platform: state
      entity_id: sensor.lynx_tx_state
      to: ["muted by the SDR", "fault"]
      for: "00:01:30"
    - platform: state
      entity_id: sensor.lynx_tx_reference
      to: "holdover"
      for: "00:01:00"
    - platform: state
      entity_id: sensor.lynx_tx_camera
      to: "unstable"
      for: "00:01:00"
  action:
    - service: notify.mobile_app_your_iphone
      data:
        title: Lynx-Tx
        message: "{{ trigger.to_state.name }}: {{ trigger.to_state.state }}"
```

The `for:` delays stop a momentary blip from waking you: an alarm has to hold for that long before it is sent. Muted and fault wait 90 seconds, long enough for auto-recover to put it right by itself; lockout is sent at once, because it needs you.

# 6. Document Version History

| Version | Date | Changes |
|---|---|---|
| 1.0 | October 2026 | First edition, from the Markdown page: SNMP objects, auto-recover and lockout, Home Assistant sensors and alarm |
