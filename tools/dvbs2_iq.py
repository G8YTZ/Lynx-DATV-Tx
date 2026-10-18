#!/usr/bin/env python3
"""DVB-S2 as IQ, for an SDR that has no DVB-S2 modulator of its own.

Bench tool, not part of the Web UI yet. The PlutoSky R2's Tezuka/Maia image
has no FPGA modulator, so the ROCK does the DVB-S2 (GNU Radio's gr-dtv) and
streams IQ to the SDR - the same split as ATSC 3.0 mode.

    tools/dvbs2_iq.py --uri ip:192.168.0.117 --freq 1304 --sr 2000 --ts clip.ts

The TS is looped and paced only by the SDR's DAC pulling IQ, so this is for
lock and MER, not for watching a programme: a file muxed at another rate will
stutter even with a perfect RF lock.

2x oversampled with a root-raised-cosine filter: the DAC runs at twice the
symbol rate, which moves its image out to where the AD9361's own analogue
filter removes it (the ATSC 3.0 lesson, 16 Oct 2026).

Ctrl-C stops it and mutes the SDR.
"""
import argparse
import os
import signal
import subprocess
import sys
import tempfile

FECS = ["1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "8/9", "9/10"]
MODS = {"qpsk": "MOD_QPSK", "8psk": "MOD_8PSK", "16apsk": "MOD_16APSK", "32apsk": "MOD_32APSK"}
ROLLOFFS = {"0.35": "RO_0_35", "0.25": "RO_0_25", "0.20": "RO_0_20"}
MUTE_DB = -89.75


def fec_const(dtv, fec):
    return getattr(dtv, "C" + fec.replace("/", "_"))


def build(args, out_path):
    from gnuradio import blocks, dtv, filter as grfilter, gr
    from gnuradio.filter import firdes

    fec = fec_const(dtv, args.fec)
    mod = getattr(dtv, MODS[args.mod])
    ro = getattr(dtv, ROLLOFFS[args.rolloff])
    frame = dtv.FECFRAME_NORMAL
    sps = 2
    sr = args.sr * 1e3

    tb = gr.top_block()
    src = blocks.file_source(gr.sizeof_char, args.ts, True)
    bbh = dtv.dvb_bbheader_bb(dtv.STANDARD_DVBS2, frame, fec, ro,
                              dtv.INPUTMODE_NORMAL, dtv.INBAND_OFF, 168, 4000000)
    scr = dtv.dvb_bbscrambler_bb(dtv.STANDARD_DVBS2, frame, fec)
    bch = dtv.dvb_bch_bb(dtv.STANDARD_DVBS2, frame, fec)
    ldpc = dtv.dvb_ldpc_bb(dtv.STANDARD_DVBS2, frame, fec, dtv.MOD_OTHER)
    il = dtv.dvbs2_interleaver_bb(frame, fec, mod)
    mapper = dtv.dvbs2_modulator_bc(frame, fec, mod, dtv.INTERPOLATION_OFF)
    pl = dtv.dvbs2_physical_cc(frame, fec, mod,
                               dtv.PILOTS_ON if args.pilots else dtv.PILOTS_OFF, 0)
    taps = firdes.root_raised_cosine(float(sps), sps * sr, sr,
                                     float(args.rolloff), 20 * sps + 1)
    rrc = grfilter.interp_fir_filter_ccf(sps, taps)
    amp = blocks.multiply_const_cc(args.level)
    to_s16 = blocks.complex_to_interleaved_short(False, 32767)
    sink = blocks.file_sink(gr.sizeof_short, out_path, False)
    sink.set_unbuffered(False)
    tb.connect(src, bbh, scr, bch, ldpc, il, mapper, pl, rrc, amp, to_s16, sink)
    return tb


def iio(uri, *a):
    subprocess.run(["iio_attr", "-u", uri, "-o", "-c", "ad9361-phy", *a],
                   check=True, stdout=subprocess.DEVNULL)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uri", default="ip:192.168.0.117")
    p.add_argument("--freq", type=float, default=1304.0, help="MHz")
    p.add_argument("--sr", type=float, default=2000, help="symbol rate, kS/s")
    p.add_argument("--fec", default="3/4", choices=FECS)
    p.add_argument("--mod", default="qpsk", choices=list(MODS))
    p.add_argument("--rolloff", default="0.20", choices=list(ROLLOFFS))
    p.add_argument("--no-pilots", dest="pilots", action="store_false")
    p.add_argument("--gain", type=float, default=-10.0,
                   help="AD9361 TX gain in dB (0 is full, -89.75 is mute)")
    p.add_argument("--level", type=float, default=0.5,
                   help="digital level, 0-1 of full scale")
    p.add_argument("--ts", required=True, help="transport stream file, looped")
    p.add_argument("--file", help="write IQ (int16 I/Q) here instead of the SDR")
    args = p.parse_args()

    if args.file:
        build(args, args.file).run()
        return

    dac = int(args.sr * 1e3 * 2)
    if dac < 2083334:
        sys.exit(f"DAC rate {dac} S/s is below the AD9361's minimum: raise --sr")
    iio(args.uri, "voltage0", "hardwaregain", f"{MUTE_DB:g}")
    iio(args.uri, "altvoltage1", "frequency", str(int(round(args.freq * 1e6))))
    iio(args.uri, "voltage0", "sampling_frequency", str(dac))
    iio(args.uri, "voltage0", "rf_bandwidth", str(max(dac, 2000000)))

    fifo = os.path.join(tempfile.mkdtemp(), "iq")
    os.mkfifo(fifo)
    writer = subprocess.Popen(
        f"iio_writedev -u {args.uri} -b 1048576 cf-ad9361-dds-core-lpc "
        f"voltage0 voltage1 < {fifo}", shell=True, start_new_session=True)
    tb = build(args, fifo)
    tb.start()
    iio(args.uri, "voltage0", "hardwaregain", f"{args.gain:g}")
    print(f"On air: {args.freq:g} MHz, {args.sr:g} kS/s, {args.mod.upper()} "
          f"{args.fec}, roll-off {args.rolloff}, pilots "
          f"{'on' if args.pilots else 'off'}, DAC {dac} S/s, gain {args.gain:g} dB. "
          f"Ctrl-C to stop.", flush=True)

    def stop(*_):
        try:
            iio(args.uri, "voltage0", "hardwaregain", f"{MUTE_DB:g}")
        finally:
            tb.stop()
            os.killpg(writer.pid, signal.SIGTERM)
            print("\nMuted and stopped.")
            os._exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    writer.wait()
    stop()


if __name__ == "__main__":
    main()
