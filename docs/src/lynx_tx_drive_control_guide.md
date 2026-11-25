title: Lynx
subtitle: DATV Transmitter (Lynx-Tx)
tagline: Automatic Drive Control Guide: PA Sequencing, Forward Power and Constant Output
version: Version 1.1  ·  G8YTZ / EI3IOB  ·  November 2026

# 1. Overview

Lynx-Tx looks after the power amplifier as well as the exciter. Through a Modbus relay board it:

- **Sequences the PA:** Supplies on before any drive, drive off before the supplies go, every time.
- **Proves the PA at key-up:** It reads the forward power and shuts the PA down if it is not producing.
- **Holds the output constant (Automatic Drive Control):** It trims the SDR's drive to keep the forward power at a target as the PA warms and cools, and holds still rather than chase a fault.
- **Protects against a bad feeder:** With a reflected power probe, it never raises the drive into a poor return loss.

All of it is optional and off until you switch it on, and all of it is set from the **PA control** card on the page.

![PA control and Automatic Drive Control](img/pa_drive_control.png){4.2}

# 2. Why Automatic Drive Control

A solid-state PA's gain falls as it warms. Set the drive on a cold day and the PA is under power in summer; set it on a hot day and a cold PA is over power, pushed further up its curve towards compression.

Compression is the real enemy. As the PA runs out of linearity the signal's intermodulation products spread either side of the channel: the **shoulders** rise (spectral regrowth), the neighbouring channels get the spill, and the MER at the receivers falls. With DVB-S2 at a tight roll-off, a couple of dB too much drive is the difference between clean shoulders and dirty ones.

Broadcast transmitters solve this with an **automatic level control** (ALC) loop: a coupler samples the forward power and the transmitter trims its own drive to hold the licensed output, whatever the temperature, mains voltage or age of the devices. Automatic Drive Control is the same principle, engineered for a repeater: slow, cautious and fault-aware.

# 3. What You Need

| Item | Notes |
|---|---|
| Lynx-Tx | Main or Beta, November 2026 or later |
| IQ mode | DVB-S2 made on the ROCK (gr-dtv), or ATSC 3.0. Not the LibreSDR's own DL1JM modulator |
| A Modbus TCP relay board | On the LAN, for example a ComWinTop board. Home Assistant can read the same board |
| Relays | Switching the driver's and the PA's DC supplies (coils 0 and 1 by default) |
| A forward power detector | Into one of the board's 0 to 10 V analogue inputs (input register 4 by default) |
| A reflected power detector | Optional, for return loss protection (Section 7) |

![Lynx-Tx wiring](img/wiring.png){6.3}

The relays and detectors share one board, reached over the LAN by Modbus TCP. Lynx-Tx opens a short connection for each request, so it shares the board happily with Home Assistant's polling.

# 4. PA Sequencing

## 4.1 TX On

1. The SDR is muted (its most attenuation: −89.75 dB).

2. The driver relay switches on, a short pause (0.3 s), then the PA relay.

3. A settle time for the PA's supply and bias to steady (Settle, for example 2 s).

4. Lynx-Tx **reads the relays back**. With "No drive unless the relays confirm" on, TX On stops here, drive still muted, unless every relay says it is on, and the page says why.

5. Once the IQ is flowing, the drive is **ramped up** over a second from 30 dB below its level, rather than one jump from mute to full power.

If the relays are already on (a restart, or auto-recover), the settle time is skipped.

## 4.2 TX Off

1. The drive is muted first, while the IQ is still flowing.

2. A short pause (0.3 s).

3. The PA relay off, then the driver relay: the reverse order.

A ROCK shutting down or restarting does the same.

> The sequencing is not a hardware interlock. If the ROCK fails while on air, the SDR mutes itself within 2 seconds, but the relays stay as they were until someone switches them. For a true fail-safe, interlock the PA in hardware as well.

## 4.3 Forward Power and the Key-up Check

The forward power is read every 2 seconds and shown on the Transmit card. The detector's 0 to 10 V arrives as 0 to 65535, and:

```
volts = raw × 10 / 65535 − zero
watts = volts² × W per V²
```

For example, a detector with zero 0.0375 V and 200 W per V². Use the same calibration as Home Assistant, so the two agree. Calibrate yours against a power meter.

Five seconds after key-up, Lynx-Tx checks the forward power three times. Under the **Minimum** (for example 10 W) means something is wrong: a contactor that did not pull in, a failed device, a fuse. Lynx-Tx mutes the drive, switches the PA off and reports **PA fault**. It does not auto-recover from this one: it needs a person to look. If the reading simply cannot be had, it reports that and leaves the transmitter on air.

## 4.4 PA Settings

| Setting | Example | Meaning |
|---|---|---|
| Modbus gateway address, Port, Unit | 192.168.1.75, 502, 1 | The relay board |
| Drive coil, PA coil | 0, 1 | On in that order, off in reverse |
| Settle (s) | 2 | After the PA relay, before any drive |
| Register | 4 | Forward power input register |
| Zero (V), W per V² | 0.0375, 200 | Detector calibration |
| Minimum (W) | 10 | Key-up check threshold |
| After key-up, check the forward power | On | The key-up check |
| No drive unless the relays confirm | On | Refuse TX On otherwise |

**Save and check the gateway** saves and reads the relays back at once: green if the board answered, red with the reason if not.

# 5. Automatic Drive Control

## 5.1 How It Works

- You set a **target forward power** (for example 40 W).
- The power slider becomes the **maximum drive**. The loop only ever trims below it, never above, down to the slider minus the **Range** (for example 4 dB).
- It is **slow on purpose:** a first-order loop with a time constant of 60 to 300 s (120 s by default). A 1 dB error moves the drive about 1 dB per time constant. Temperature drift takes minutes, so a fast loop gains nothing and could hunt.
- Changes go in the AD9361's own **0.25 dB steps**, one step at a time, with a **±0.2 dB dead band** around the target.
- Each key-up starts from the **last settled drive** (saved after a minute on target, and at TX Off), not the maximum, so a cold PA does not get a burst of full drive. The very first key-up starts at the maximum.
- Lowering the slider below the drive brings the drive straight down with it. Raising the slider only lifts the ceiling.
- The loop starts 10 seconds after key-up, once the key-up check has passed.

## 5.2 It Will Not Chase a Fault

A naive power loop turns the drive up whenever the output falls. If the output fell because a device failed, a supply tripped or the feeder is damaged, that is exactly the wrong thing to do. So the loop **holds the drive where it is**, and says why in red on the card, when:

- **The forward power falls suddenly** by the Hold on drop setting (2 dB) or more, measured against its average over the last 20 seconds. Temperature drift is slow; a sudden drop is a fault.
- **No forward power reading** for 10 seconds (the board has stopped answering).
- **The return loss is worse than the minimum** (Section 7), when a reflected power register is set.

One bad reading is ignored: the loop works from the median of the last three. A held loop shows a **Resume** button and clears itself at the next key-up. Look at the PA before pressing Resume.

## 5.3 What the Card Says

| Message | Meaning |
|---|---|
| Ready | Enabled, off air |
| Settling after key-up | The first 10 seconds |
| Tracking up / Tracking down | Trimming towards the target |
| On target | Within ±0.2 dB |
| At maximum drive, PA short of target by n dB | The PA cannot reach the target at the slider's setting. Occasionally on a hot day is normal; often means the PA is losing gain |
| At minimum drive, PA over target by n dB | Over target even at the bottom of the range: widen the range or lower the slider |
| Held: ... | Stopped for one of the reasons in 5.2 |
| Board mode: not available | DL1JM's modulator is in use: no fine power control |
| Needs forward power configured | Switch on PA control and set the register first |

The Transmit card shows **Forward power** and **Drive** (the drive now against its maximum) while on air.

![The Transmit card on air](img/transmit_card.png){3.6}

## 5.4 Drive Control Settings

| Setting | Default | Example | Meaning |
|---|---|---|---|
| Target (W) | none | 40 | Forward power to hold |
| Time constant (s) | 120 | 60 | 60 to 300 |
| Range (dB) | 6 | 4 | Lowest drive is the slider minus this |
| Hold on drop (dB) | 2 | 2 | A fall this fast holds the loop |
| Reflected register | −1 | −1 | −1: no reflected probe |
| Min return loss (dB) | 14 | 14 | Holds the loop below this |

# 6. Setting It Up

1. **Set up PA control first** (Section 4.4), and check the forward power on the Transmit card agrees with a power meter and with Home Assistant.

2. **Find the maximum drive on a hot PA.** Run the PA to its working temperature, then raise the drive while watching the shoulders on a spectrum analyser. Stop where they are still clean (for example, shoulders well over 30 dB down). That drive is your slider setting.

3. **Lock the slider** with the padlock, so the ceiling cannot be moved by accident.

4. **Set the target** a little under what the PA gives at that drive when hot, so the loop has headroom on a summer afternoon.

5. **Choose the range** so that the slider minus the range still gives the target from a cold PA. 4 to 6 dB is typical.

6. **Save drive control** and key up. The card says "settling", then trims down over a few minutes as the PA warms, and settles "on target".

7. **Test the hold:** On a bench or a quiet moment, switch the PA's relay off from Home Assistant while on air. The loop should say "held" rather than turn the drive up. Switch it back on, then Resume.

> The forward power is only as good as its detector. A diode detector drifts with temperature too: calibrate it at the working temperature, and check it against a meter now and then.

# 7. Return Loss Protection

When the antenna and feeder are not a good match, some forward power comes back down the feeder. **Return loss** is how far below the forward power that reflection is:

| Return loss | Reflected | VSWR | Verdict |
|---|---|---|---|
| 20 dB | 1% | 1.22:1 | Healthy |
| 14 dB | 4% | 1.5:1 | Time to look at it |
| 10 dB | 10% | 1.9:1 | Something is wrong |

The lost power is minor. The damage is to the radiated quality: a PA into the wrong load compresses earlier, and the reflection comes back up the feeder as a delayed echo, which caps the MER (an echo 25 dB down limits it to about 25 dB). A receiver can never see a better MER than the transmitter sent, so a poor feeder eats straight into the viewers' fade margin.

With a reflected power detector on the board, set **Reflected register** to its input register. It uses the forward power's calibration. Lynx-Tx then works out the return loss, and if it is worse than **Min return loss** the loop holds the drive, so the drive is never raised into a bad feeder.

Without a reflected detector, leave it off (−1). A proper fold-back and trip, broadcast style, will follow once there are real reflected readings to work with.

# 8. Monitoring

Lynx-Tx reports all of this over SNMP (objects 19 to 24), so Home Assistant can alarm on it:

| Object | Values |
|---|---|
| 19 lynxPaRelays | 1 on, 2 off, 3 not answering, 4 not configured, 5 partly on |
| 20 lynxFwdPower | Tenths of a watt |
| 21 lynxDriveState | 1 off, 2 standby, 3 settling, 4 tracking, 5 on target, 6 at maximum, 7 at minimum, 8 held |
| 22 lynxDrive | Hundredths of a dB |
| 23 lynxPowerLock | 1 locked, 2 unlocked |
| 24 lynxPaFault | 1 none, 2 fault |

The Monitoring Guide has the details, and `docs/home-assistant/lynx_tx.yaml` is a ready-made Home Assistant package with every sensor and alarm. Worth graphing: **Drive** over months. At the same forward power, a slow rise is the PA losing gain, long before anything fails.

# 9. In the Configuration File

The card writes these to `config/lynx_tx.yaml`; you need not edit them by hand.

```
pa:
  enabled: true
  host: 192.168.1.75
  port: 502
  unit: 1
  coils:
    - {name: Drive, coil: 0}
    - {name: PA, coil: 1}
  settle_s: 2.0
  require_confirm: true
  fwd_register: 4
  fwd_zero_v: 0.0375
  fwd_w_per_v2: 200.0
  fwd_check: true
  fwd_min_w: 10.0

drive_control:
  enabled: true
  target_w: 40
  tau_s: 60
  range_db: 4
  deadband_db: 0.2
  drop_db: 2
  rl_register: -1
  rl_min_db: 14
  last_db: -12.5        # the last settled drive, kept by Lynx-Tx
```

# 10. Troubleshooting

## 10.1 "Not keyed up: the PA relays did not confirm"

The board answered, but a relay still read off after the settle time, or the board did not answer. Check the gateway address on the card (Save and check the gateway), the board's power, and that the coil numbers match the wiring. Home Assistant switching the same relays at the same moment can also cause it.

## 10.2 The gateway does not answer

Check the address and port, and that the ROCK can reach the board's subnet. Some gateways accept only a few connections at once: if Home Assistant polls very often, slow it to 5 to 10 seconds.

## 10.3 Forward power reads 0 or nonsense

Check the register number, then the calibration: with the PA off the reading should be 0 W. A negative zero error shows as a small reading with no RF; raise **Zero**.

## 10.4 PA fault after every key-up

The forward power is under the Minimum five seconds after key-up. Either the PA really is not producing, or the Minimum is too high for the power you run. Check with a meter.

## 10.5 Always "at maximum drive"

The PA cannot reach the target at the slider's setting. Lower the target, or, if the shoulders allow, raise the slider. If it creeps in over months, look at the PA.

## 10.6 The loop held on a cold start

A PA whose output falls fast in the first minute can look like a fault. Raise **Hold on drop** to 3 dB, or start from a settled drive (it learns one after the first minute on target).

## 10.7 Drive control does nothing

It works only in IQ mode (Section 3), with PA control on, the forward power register set and a target given. The card's status line says which is missing.

# 11. Document Version History

| Version | Date | Changes |
|---|---|---|
| 1.0 | 25 November 2026 | First edition: PA sequencing, forward power and the key-up check, Automatic Drive Control, return loss protection, monitoring, troubleshooting |
| 1.1 | 25 November 2026 | Example values given as examples rather than one repeater's settings |
