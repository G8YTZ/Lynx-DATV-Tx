"""DVB-S2 made on the ROCK, any Pluto-class SDR as the transmitter.

The same split as ATSC 3.0 mode (atsc3mod.py): everything up to the remux
is shared, tsprio builds a constant-rate stream, and the modulator - Ron
Economos W6RZ's gr-dtv blocks, in dvbs2_flowgraph.py - turns it into IQ
that iio_writedev hands to the SDR. The SDR's DAC pulls the IQ at exactly
the symbol rate times the samples per symbol, so the DAC clock is the pace
and the stream's rate below is exact, not estimated.

Proven 18 Oct 2026: PlutoSky R2 and LibreSDR, 1249 MHz, QPSK 3/4 at
2000 and 4000 kS/s, 31.4 dB MER on the Picotuner, 75% of one A76 core.

Settings are the DVB-S2 Modulation card's (config["modulation"]); this
module only adds what the ROCK-made path needs.
"""
from __future__ import annotations

import math
import shlex
from pathlib import Path

import atsc3mod   # big_cores() and the mute level are shared

TS_FIFO = "/tmp/lynx-dvbs2-ts"
IQ_FIFO = "/tmp/lynx-dvbs2-iq"
FLOWGRAPH_ID = "dvbs2_flowgraph.py"
MOD_LOG = "/tmp/lynx-dvbs2-mod.log"

# The AD9361's lowest DAC rate without its FIR, with a little margin.
MIN_DAC = 2_100_000

# BCH-protected bits per normal (64800-bit) FECFRAME, by code rate. The
# baseband header takes 80 of them; the rest is transport stream.
KBCH = {
    "1/4": 16008, "1/3": 21408, "2/5": 25728, "1/2": 32208,
    "3/5": 38688, "2/3": 43040, "3/4": 48408, "4/5": 51648,
    "5/6": 53840, "8/9": 57472, "9/10": 58192,
}
BITS_PER_SYMBOL = {"QPSK": 2, "8PSK": 3, "16APSK": 4, "32APSK": 5}
# Not every code rate exists for every constellation (EN 302 307 table 12).
ALLOWED = {
    "QPSK": set(KBCH),
    "8PSK": {"3/5", "2/3", "3/4", "5/6", "8/9", "9/10"},
    "16APSK": {"2/3", "3/4", "4/5", "5/6", "8/9", "9/10"},
    "32APSK": {"3/4", "4/5", "5/6", "8/9", "9/10"},
}
ROLLOFFS = ("0.35", "0.25", "0.20")


def plframe_symbols(constellation: str, pilots: bool) -> int:
    """Symbols in one normal PLFRAME: header, 90-symbol slots, pilot blocks."""
    slots = 64800 // BITS_PER_SYMBOL[constellation] // 90
    pilot_blocks = (slots - 1) // 16 if pilots else 0
    return 90 + slots * 90 + 36 * pilot_blocks


def ts_rate(m: dict) -> int:
    """The exact transport stream rate, b/s (QPSK 3/4 pilots 2000 kS/s:
    2,904,147)."""
    c = m["constellation"].upper()
    sr = int(m["symbol_rate_ks"]) * 1000
    return int(sr * (KBCH[m["fec"]] - 80) / plframe_symbols(c, bool(m["pilots"])))


def sps(m: dict) -> int:
    """Samples per symbol: 2 (the image well clear of the channel), more
    when the symbol rate is low enough to put the DAC below its minimum."""
    sr = max(1, int(m["symbol_rate_ks"])) * 1000
    return max(2, math.ceil(MIN_DAC / sr))


def dac_rate(m: dict) -> int:
    return int(m["symbol_rate_ks"]) * 1000 * sps(m)


def norm_rolloff(v) -> str:
    """0.2, "0.2" and "0.20" are all the same roll-off."""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def check(m: dict):
    """Refuse settings the modulator cannot make, with the reason."""
    try:
        sr_ks = float(m["symbol_rate_ks"])
    except (TypeError, ValueError, KeyError):
        raise ValueError("symbol rate must be a number of kS/s")
    if sr_ks < 25:
        raise ValueError("symbol rate must be at least 25 kS/s")
    c = m["constellation"].upper()
    if c not in BITS_PER_SYMBOL:
        raise ValueError(f"unknown constellation {m['constellation']}")
    if m["fec"] not in ALLOWED[c]:
        raise ValueError(f"{c} has no FEC {m['fec']} in DVB-S2 "
                         f"(it takes {', '.join(sorted(ALLOWED[c]))})")
    if norm_rolloff(m.get("rolloff", "0.35")) not in ROLLOFFS:
        raise ValueError(f"roll-off must be one of {', '.join(ROLLOFFS)}")
    if dac_rate(m) > 30_000_000:
        raise ValueError("symbol rate too high for the IQ link")


def iio_commands(uri: str, m: dict) -> list:
    """iio_attr calls that set the SDR up for this channel."""
    sr = int(m["symbol_rate_ks"]) * 1000
    occupied = sr * (1 + float(norm_rolloff(m.get("rolloff", "0.35"))))
    bw = int(min(max(occupied, 1_500_000), 40_000_000))
    base = ["iio_attr", "-u", uri, "-o", "-c", "ad9361-phy"]
    return [
        base + ["altvoltage1", "frequency", str(int(round(float(m["frequency_mhz"]) * 1e6)))],
        base + ["voltage0", "sampling_frequency", str(dac_rate(m))],
        base + ["voltage0", "rf_bandwidth", str(bw)],
        base + ["voltage0", "hardwaregain", f"{float(m['power_db']):g}"],
    ]


def radio_live_commands(uri: str, m: dict) -> list:
    """Just frequency and power: changed on air with no break."""
    return [c for c in iio_commands(uri, m) if c[-2] in ("frequency", "hardwaregain")]


def mute_command(uri: str) -> list:
    return atsc3mod.mute_command(uri)


def flowgraph_command(app_dir: Path, m: dict) -> str:
    fg = Path(app_dir) / "dvbs2_flowgraph.py"
    args = [
        "--in", TS_FIFO, "--out", IQ_FIFO,
        "--sr", str(int(m["symbol_rate_ks"]) * 1000),
        "--fec", m["fec"], "--constellation", m["constellation"].upper(),
        "--rolloff", norm_rolloff(m.get("rolloff", "0.35")), "--sps", str(sps(m)),
    ]
    if not m.get("pilots", True):
        args.append("--no-pilots")
    return "python3 " + shlex.quote(str(fg)) + " " + " ".join(shlex.quote(a) for a in args)


def modulator_shell(uri: str, app_dir: Path, m: dict) -> str:
    """FIFOs, IQ writer and flowgraph, in the background of the TX process
    group, both on the fast cores (as ATSC 3.0, for the same reason: a
    stall on a slow core goes out on air)."""
    cores = atsc3mod.big_cores()
    pin = f"taskset -c {cores} " if cores else ""
    # ~0.25 s of IQ per buffer at 2000 kS/s: big enough to ride a stall,
    # small enough not to add noticeable delay.
    buf = max(262144, min(2097152, dac_rate(m) // 4))
    return (f"rm -f {TS_FIFO} {IQ_FIFO}; mkfifo {TS_FIFO} {IQ_FIFO}; "
            f"{pin}iio_writedev -u {uri} -b {buf} cf-ad9361-dds-core-lpc voltage0 voltage1 "
            f"< {IQ_FIFO} & "
            f"{pin}{flowgraph_command(app_dir, m)} > {MOD_LOG} 2>&1 & ")
