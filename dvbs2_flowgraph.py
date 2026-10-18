#!/usr/bin/env python3
"""DVB-S2 modulator flowgraph: transport stream in, int16 IQ out.

Ron Economos W6RZ's DVB-S2 blocks from GNU Radio's gr-dtv, run on the ROCK
so any Pluto-class SDR can be the transmitter (see dvbs2mod.py). Used by
Lynx-Tx's DVB-S2 mode and by tools/dvbs2_iq.py, so there is one modulator,
not two.

The framer (dvbs2_physical_cc) outputs each symbol followed by a zero.
Those zeros are dropped and the symbols interpolated to the DAC's rate in
one root-raised-cosine filter: `sps` samples per symbol, at least 2, more
for low symbol rates so the DAC stays above the AD9361's minimum.
"""
import argparse
import sys

FECS = ["1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "8/9", "9/10"]
CONSTELLATIONS = {"QPSK": "MOD_QPSK", "8PSK": "MOD_8PSK",
                  "16APSK": "MOD_16APSK", "32APSK": "MOD_32APSK"}
ROLLOFFS = {"0.35": "RO_0_35", "0.25": "RO_0_25", "0.20": "RO_0_20"}


def build(src_path, out_path, symbol_rate, fec="3/4", constellation="QPSK",
          rolloff="0.35", pilots=True, sps=2, level=0.35, repeat=False):
    """A top block reading a TS from src_path and writing IQ to out_path."""
    from gnuradio import blocks, dtv, filter as grfilter, gr
    from gnuradio.filter import firdes

    rate = getattr(dtv, "C" + fec.replace("/", "_"))
    mod = getattr(dtv, CONSTELLATIONS[constellation.upper()])
    ro = getattr(dtv, ROLLOFFS[rolloff])
    frame = dtv.FECFRAME_NORMAL

    tb = gr.top_block("lynx-dvbs2")
    src = blocks.file_source(gr.sizeof_char, src_path, repeat)
    bbh = dtv.dvb_bbheader_bb(dtv.STANDARD_DVBS2, frame, rate, ro,
                              dtv.INPUTMODE_NORMAL, dtv.INBAND_OFF, 168, 4000000)
    scr = dtv.dvb_bbscrambler_bb(dtv.STANDARD_DVBS2, frame, rate)
    bch = dtv.dvb_bch_bb(dtv.STANDARD_DVBS2, frame, rate)
    ldpc = dtv.dvb_ldpc_bb(dtv.STANDARD_DVBS2, frame, rate, dtv.MOD_OTHER)
    il = dtv.dvbs2_interleaver_bb(frame, rate, mod)
    mapper = dtv.dvbs2_modulator_bc(frame, rate, mod, dtv.INTERPOLATION_OFF)
    pl = dtv.dvbs2_physical_cc(frame, rate, mod,
                               dtv.PILOTS_ON if pilots else dtv.PILOTS_OFF, 0)
    # Symbol, zero, symbol, zero... keep_one_in_n keeps the last of each
    # pair, so skip the first item to make that last one the symbol.
    align = blocks.skiphead(gr.sizeof_gr_complex, 1)
    symbols = blocks.keep_one_in_n(gr.sizeof_gr_complex, 2)
    taps = firdes.root_raised_cosine(float(sps), float(sps * symbol_rate),
                                     float(symbol_rate), float(rolloff), 20 * sps + 1)
    rrc = grfilter.interp_fir_filter_ccf(sps, taps)
    amp = blocks.multiply_const_cc(level)
    to_s16 = blocks.complex_to_interleaved_short(False, 32767)
    sink = blocks.file_sink(gr.sizeof_short, out_path, False)
    sink.set_unbuffered(False)
    tb.connect(src, bbh, scr, bch, ldpc, il, mapper, pl, align, symbols, rrc, amp, to_s16, sink)
    return tb


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in", dest="src", required=True, help="TS file or FIFO")
    p.add_argument("--out", required=True, help="IQ file or FIFO (int16 I/Q)")
    p.add_argument("--sr", type=float, required=True, help="symbol rate, S/s")
    p.add_argument("--fec", default="3/4", choices=FECS)
    p.add_argument("--constellation", default="QPSK", choices=list(CONSTELLATIONS))
    p.add_argument("--rolloff", default="0.35", choices=list(ROLLOFFS))
    p.add_argument("--no-pilots", dest="pilots", action="store_false")
    p.add_argument("--sps", type=int, default=2)
    p.add_argument("--level", type=float, default=0.35)
    p.add_argument("--repeat", action="store_true", help="loop the input file")
    a = p.parse_args()
    tb = build(a.src, a.out, a.sr, a.fec, a.constellation, a.rolloff,
               a.pilots, a.sps, a.level, a.repeat)
    print(f"[dvbs2] {a.sr / 1e3:g} kS/s {a.constellation} {a.fec} roll-off {a.rolloff} "
          f"pilots {'on' if a.pilots else 'off'}, {a.sps} samples/symbol", flush=True)
    tb.run()


if __name__ == "__main__":
    sys.exit(main())
