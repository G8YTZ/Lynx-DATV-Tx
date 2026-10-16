"""
Lynx-Tx ATSC 3.0 mode - the modulator side.

The programme chain is the DVB-S2 one, unchanged up to tsprio.py:

    switcher.py | ffmpeg (IDs) | tsprio.py (CBR, sound first) | tsp (buffer tap)
        -> TS FIFO -> gr-atsc3 flowgraph (on the ROCK) -> IQ FIFO
        -> iio_writedev -> LibreSDR, used as a plain IQ transmitter

The modulator pulls the transport stream at exactly the PLP rate, paced by
the LibreSDR's DAC clock; tsprio fills every slot it cannot use with a null
packet, so its output is constant-rate at whatever speed the modulator takes
it. That is the same back-pressure tsp regulate gives it in DVB-S2.

The flowgraph is Ron Economos W6RZ's gr-atsc3 (GPL-3.0), built here from his
validation flowgraph vv031 (atsc3/vv031_tx.grc) with only the constellation
and code rate changed. The frame structure is held at the one proven on air
on 16 October 2026: 8K FFT, GI5_1024, pilot pattern SP3_4, 72 data and 2
preamble symbols, 6.912 MS/s, 6 MHz. FFT, guard interval and pilots become
choices once each combination has been checked against a receiver.

Rates: Ron's atsc3rate (dtv-utils) for that structure, computed once:
    atsc3rate 8 5 72 2 <rate> <mod> 0 1 0 1 1 0 4 0
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

SAMPLE_RATE = 6912000          # 384000 * (16 + 2): the 6 MHz channel
RF_BANDWIDTH = 6000000
MUTE_GAIN = -89.75             # the AD9361's most attenuation: "TX Off"

CONSTELLATIONS = ("QPSK", "16QAM", "64QAM", "256QAM")
CODE_RATES = tuple(f"{n}/15" for n in range(2, 14))

# TS bitrate, b/s, for the fixed frame structure above.
TS_RATES = {
    "QPSK": {"2/15": 1163430, "3/15": 1759495, "4/15": 2355560, "5/15": 2951625,
             "6/15": 3547689, "7/15": 4143754, "8/15": 4739819, "9/15": 5335884,
             "10/15": 5931949, "11/15": 6528014, "12/15": 7124079, "13/15": 7720144},
    "16QAM": {"2/15": 2326860, "3/15": 3518990, "4/15": 4711120, "5/15": 5903250,
              "6/15": 7095379, "7/15": 8287509, "8/15": 9479639, "9/15": 10671769,
              "10/15": 11863899, "11/15": 13056028, "12/15": 14248158, "13/15": 15440288},
    "64QAM": {"2/15": 3490291, "3/15": 5278485, "4/15": 7066680, "5/15": 8854875,
              "6/15": 10643069, "7/15": 12431264, "8/15": 14219459, "9/15": 16007654,
              "10/15": 17795848, "11/15": 19584043, "12/15": 21372238, "13/15": 23160432},
    "256QAM": {"2/15": 4653721, "3/15": 7037981, "4/15": 9422240, "5/15": 11806500,
               "6/15": 14190759, "7/15": 16575019, "8/15": 18959279, "9/15": 21343538,
               "10/15": 23727798, "11/15": 26112057, "12/15": 28496317, "13/15": 30880577},
}

DEFAULTS = {
    "frequency_mhz": 437.0,
    "constellation": "QPSK",
    "rate": "13/15",
    "power_db": 0.0,
    # 2: interpolated 2x on the ROCK through a sharp filter, the DAC at
    # 13.824 MS/s. 1: the DAC at the channel's own 6.912 MS/s. The DAC's
    # first image sits at the sample rate less the band edge: 6.912 - 2.9 =
    # 4.0 MHz either side of the carrier, exactly where the "spurs" were
    # measured on the IC-9700 (16 Oct 2026). At 2x it moves to about 10.9
    # MHz, where the AD9361's analogue filter removes it: on air, nothing
    # to be seen out to +/-25 MHz, and the HDHomeRun still locked.
    "oversample": 2,
}
OVERSAMPLE = (1, 2)
# The interpolation filter's passband as a fraction of the input rate. GNU
# Radio's default (0.4) would roll off from 2.76 MHz - inside the 2.9 MHz
# band edge. 0.45 is flat to 3.11 MHz, stopband from 3.46 MHz (Kaiser,
# beta 7, about 70 dB), so the whole channel passes and the image does not.
INTERP_FBW = 0.45

TS_FIFO = "/tmp/lynx-atsc3-ts"
IQ_FIFO = "/tmp/lynx-atsc3-iq"
FLOWGRAPH_ID = "lynx_atsc3"
MOD_LOG = "/tmp/lynx-atsc3-mod.log"


def clean(cfg: dict) -> dict:
    """Valid settings only; anything unknown falls back to the defaults."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    if out["constellation"] not in CONSTELLATIONS:
        out["constellation"] = DEFAULTS["constellation"]
    if out["rate"] not in CODE_RATES:
        out["rate"] = DEFAULTS["rate"]
    out["frequency_mhz"] = float(out["frequency_mhz"])
    out["power_db"] = max(-40.0, min(0.0, float(out["power_db"])))
    try:
        out["oversample"] = int(out["oversample"])
    except (TypeError, ValueError):
        out["oversample"] = DEFAULTS["oversample"]
    if out["oversample"] not in OVERSAMPLE:
        out["oversample"] = DEFAULTS["oversample"]
    return out


def dac_rate(cfg: dict) -> int:
    return SAMPLE_RATE * clean(cfg)["oversample"]


def ts_rate(cfg: dict) -> int:
    c = clean(cfg)
    return TS_RATES[c["constellation"]][c["rate"]]


def build_flowgraph(cfg: dict, app_dir: Path) -> Path:
    """The flowgraph for these settings, as a runnable Python file.

    Made from the proven template by GNU Radio's own compiler, so the block
    calls always match the installed gr-atsc3. Rebuilt only when the
    settings or the template change (grcc takes a couple of seconds).
    """
    c = clean(cfg)
    template = app_dir / "atsc3" / "vv031_tx.grc"
    run_dir = app_dir / "run" / "atsc3"
    run_dir.mkdir(parents=True, exist_ok=True)
    out_py = run_dir / f"{FLOWGRAPH_ID}.py"
    stamp = run_dir / "settings.sha1"

    tpl = template.read_text()
    key = hashlib.sha1(
        (tpl + c["constellation"] + c["rate"] + str(c["oversample"]) +
         str(INTERP_FBW) + TS_FIFO + IQ_FIFO).encode()).hexdigest()
    if out_py.exists() and stamp.exists() and stamp.read_text() == key:
        return out_py

    g = yaml.safe_load(tpl)
    opts = g["options"]["parameters"]
    opts.update(id=FLOWGRAPH_ID, generate_options="no_gui", run_options="run")
    rate = "C" + c["rate"].replace("/", "_")             # 13/15 -> C13_15
    mod = "MOD_" + c["constellation"]
    for b in g["blocks"]:
        p = b.setdefault("parameters", {})
        if "rate" in p:
            p["rate"] = rate
        if "constellation1" in p:
            p["constellation1"] = mod
        if b["id"] == "blocks_file_source":
            p.update(file=TS_FIFO, repeat="False")
        if b["id"] == "blocks_file_sink":
            p.update(file=IQ_FIFO, unbuffered="False", append="False")
    if c["oversample"] > 1:
        # Between the bootstrap (the finished baseband) and the IQ writer.
        g["blocks"].append({
            "name": "interp_0", "id": "rational_resampler_xxx",
            "parameters": {"type": "ccf", "interp": str(c["oversample"]), "decim": "1",
                           "taps": "[]", "fbw": str(INTERP_FBW)},
            "states": {"coordinate": [0, 0], "rotation": 0, "state": "enabled"}})
        conns = []
        for cn in g["connections"]:
            if cn[0] == "blocks_multiply_const_xx_0" and cn[2] == "c2s_0":
                conns += [["blocks_multiply_const_xx_0", "0", "interp_0", "0"],
                          ["interp_0", "0", "c2s_0", "0"]]
            else:
                conns.append(cn)
        g["connections"] = conns
    grc = run_dir / f"{FLOWGRAPH_ID}.grc"
    grc.write_text(yaml.safe_dump(g, sort_keys=False))
    r = subprocess.run(["grcc", str(grc), "-o", str(run_dir)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not out_py.exists():
        raise RuntimeError("grcc failed: " + (r.stderr or r.stdout).strip()[-400:])
    stamp.write_text(key)
    return out_py


def iio_commands(uri: str, cfg: dict) -> list:
    """iio_attr calls that set the LibreSDR up as a 6 MHz IQ transmitter."""
    c = clean(cfg)
    base = ["iio_attr", "-u", uri, "-o", "-c", "ad9361-phy"]
    return [
        base + ["altvoltage1", "frequency", str(int(round(c["frequency_mhz"] * 1e6)))],
        base + ["voltage0", "sampling_frequency", str(dac_rate(c))],
        base + ["voltage0", "rf_bandwidth", str(RF_BANDWIDTH)],
        base + ["voltage0", "hardwaregain", f"{c['power_db']:g}"],
    ]


def mute_command(uri: str) -> list:
    return ["iio_attr", "-u", uri, "-o", "-c", "ad9361-phy",
            "voltage0", "hardwaregain", f"{MUTE_GAIN:g}"]


def modulator_shell(uri: str, flowgraph: Path) -> str:
    """The modulator half of the shell pipeline: FIFOs, IQ writer, flowgraph.

    Both run in the background inside the transmitter's process group, so
    stopping the transmitter stops them too. Each opens its FIFO and waits
    for the other end, so start order does not matter.
    """
    return (f"rm -f {TS_FIFO} {IQ_FIFO}; mkfifo {TS_FIFO} {IQ_FIFO}; "
            f"iio_writedev -u {uri} -b 1048576 cf-ad9361-dds-core-lpc voltage0 voltage1 "
            f"< {IQ_FIFO} & "
            f"python3 {flowgraph} > {MOD_LOG} 2>&1 & ")
