"""Automatic Drive Control: slow temperature compensation of the PA (25 Nov 2026).

A PA gives more output cold than hot. With a fixed drive it is over power
on a winter night and pushed towards compression on a summer afternoon.
This loop holds the forward power at a target by trimming the SDR's drive,
slowly:

  - The power slider is the MAXIMUM drive. The loop only ever reduces it,
    down to a floor (maximum minus range_db), and climbs back towards the
    maximum as the PA warms and gives less. It never goes above it, so the
    slider, set from a hot-PA shoulder measurement, stays the safe limit.
  - First-order, time constant tau_s (60 to 300 s, 120 by default). A 1 dB
    error moves the drive about 1 dB per tau. Changes go in the AD9361's
    own 0.25 dB steps, one step at a time, with a dead band of +/-0.2 dB
    around the target so it does not hunt.
  - It holds the drive, and says why, when it should not be trusted to
    chase the meter: the forward power suddenly falls by drop_db or more (a
    failed device, a tripped supply, a feeder fault: chasing that would
    just drive harder into the fault), no forward power reading for 10 s,
    or, where the PA has a reflected power probe, the return loss is worse
    than rl_min_db. A held loop stays held until Resume or the next key-up.
  - Each key-up starts from the last settled drive rather than the maximum,
    so a cold PA does not get a burst of full drive.

IQ mode only (DVB-S2 made on the ROCK, or ATSC 3.0): the board-mode
LibreSDR firmware has no fine, glitch-free power control.

This module is just the controller; lynx_tx.py reads the meter and sets
the drive. Config, under "drive_control" in lynx_tx.yaml:

  drive_control:
    enabled: true
    target_w: 40            # forward power to hold
    tau_s: 120              # time constant, 60 to 300 s
    range_db: 6             # lowest drive is the slider minus this
    deadband_db: 0.2
    drop_db: 2              # a fall this fast holds the loop
    rl_register: -1         # reflected power register, -1 for none
    rl_min_db: 14           # return loss below this holds the loop
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Optional

DEFAULTS = {
    "enabled": False,
    "target_w": 0.0,
    "tau_s": 120.0,
    "range_db": 6.0,
    "deadband_db": 0.2,
    "drop_db": 2.0,
    "rl_register": -1,
    "rl_min_db": 14.0,
    "last_db": None,              # the last settled drive, for the next key-up
}

STEP_DB = 0.25                    # the AD9361's attenuation step
MISSING_S = 10.0                  # no reading for this long holds the loop
REF_S = 20.0                      # the drop detector's reference averages over this


def cfg(config: dict) -> dict:
    c = {**DEFAULTS, **(config.get("drive_control") or {})}
    c["tau_s"] = max(60.0, min(300.0, float(c["tau_s"])))
    c["range_db"] = max(0.5, min(20.0, float(c["range_db"])))
    c["deadband_db"] = max(0.1, min(1.0, float(c["deadband_db"])))
    c["drop_db"] = max(0.5, min(10.0, float(c["drop_db"])))
    c["target_w"] = max(0.0, float(c["target_w"]))
    return c


def quant(db: float) -> float:
    return round(db / STEP_DB) * STEP_DB


def db(w: float) -> float:
    return 10.0 * math.log10(max(w, 0.01))


class Loop:
    """The controller's state for one key-up. step() is fed a reading every
    couple of seconds and returns a new drive to set, or None."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.running = False
        self.want: Optional[float] = None      # unquantised drive, dB
        self.applied: Optional[float] = None   # what the SDR is set to
        self.ref: Optional[float] = None       # slow average of the meter, dB
        self.hist = deque(maxlen=3)            # for a median: one bad read is ignored
        self.max_seen: Optional[float] = None
        self.missing_since: Optional[float] = None
        self.held = ""
        self.status = "off air"
        self.settled_since: Optional[float] = None

    def start_db(self, c: dict, max_db: float) -> float:
        """Where a key-up starts: the last settled drive, within today's limits."""
        last = c.get("last_db")
        if last is None:
            return quant(max_db)
        return quant(max(max_db - c["range_db"], min(max_db, float(last))))

    def start(self, drive_db: float, max_db: float):
        self.reset()
        self.running = True
        self.want = self.applied = drive_db
        self.max_seen = max_db
        self.status = "settling after key-up"

    def hold(self, why: str):
        self.held = why
        self.status = "held: " + why

    def resume(self):
        self.held = ""
        self.ref = None
        self.hist.clear()
        self.missing_since = None
        self.status = "tracking"

    def step(self, c: dict, max_db: float, fwd_w: Optional[float],
             rl_db: Optional[float], dt: float, now: float) -> Optional[float]:
        if not self.running:
            return None
        floor = max_db - c["range_db"]
        if self.max_seen is None or abs(max_db - self.max_seen) > 0.01:
            # The slider moved: a new ceiling, and the meter will step with
            # it, which is not a fault, so the drop detector starts afresh.
            self.max_seen = max_db
            self.ref = None
            self.hist.clear()
            if self.applied is not None and self.applied > max_db:
                self.want = self.applied = quant(max_db)
                return self.applied
            self.want = max(floor, min(max_db, self.want))
        if self.held:
            return None

        if fwd_w is None:
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since >= MISSING_S:
                self.hold(f"no forward power reading for {MISSING_S:g} s, drive kept at {self.applied:g} dB")
            return None
        self.missing_since = None

        if (int(c.get("rl_register", -1)) >= 0 and rl_db is not None
                and rl_db < float(c["rl_min_db"])):
            self.hold(f"return loss {rl_db:.1f} dB, worse than {c['rl_min_db']:g} dB")
            return None

        self.hist.append(db(fwd_w))
        med = statistics.median(self.hist)
        if self.ref is not None and self.ref - med >= c["drop_db"]:
            self.hold(f"forward power fell {self.ref - med:.1f} dB suddenly (now {fwd_w:g} W), "
                      f"drive kept at {self.applied:g} dB")
            return None
        self.ref = med if self.ref is None else self.ref + (med - self.ref) * min(1.0, dt / REF_S)

        target = c["target_w"]
        if target <= 0:
            self.status = "no target set"
            return None
        err = db(target) - med
        if abs(err) <= c["deadband_db"]:
            self.status = "on target"
            if self.settled_since is None:
                self.settled_since = now
            return None
        self.settled_since = None

        self.want = max(floor, min(max_db, self.want + err * dt / c["tau_s"]))
        new = quant(self.want)
        new = max(self.applied - STEP_DB, min(self.applied + STEP_DB, new))
        new = max(quant(floor), min(quant(max_db), new))
        if self.want >= max_db - 1e-9 and err > 0:
            self.status = f"at maximum drive, PA short of target by {err:.1f} dB"
        elif self.want <= floor + 1e-9 and err < 0:
            self.status = f"at minimum drive, PA over target by {-err:.1f} dB"
        else:
            self.status = "tracking " + ("up" if err > 0 else "down")
        if abs(new - self.applied) > 1e-9:
            self.applied = new
            return new
        return None
