#!/usr/bin/env python3
"""DVB-S2 as IQ, for an SDR that has no DVB-S2 modulator of its own.

Bench tool, not part of the Web UI yet. The PlutoSky R2's Tezuka/Maia image
has no FPGA modulator, so the ROCK does the DVB-S2 (GNU Radio's gr-dtv) and
streams IQ to the SDR - the same split as ATSC 3.0 mode.

    tools/dvbs2_iq.py --uri ip:192.168.0.117 --freq 1304 --sr 2000 --ts clip.ts

The TS is looped and paced only by the SDR's DAC pulling IQ, so this is for
lock and MER, not for watching a programme: a file muxed at another rate will
stutter even with a perfect RF lock.

2x oversampled with a root-raised-cosine filter (gr-dtv's framer already
zero-stuffs to 2 samples per symbol): the DAC runs at twice the symbol
rate, which moves its image out to where the AD9361's own analogue
filter removes it (the ATSC 3.0 lesson, 16 Oct 2026).

Use a real programme stream. A TS of nothing but null packets is perfectly
valid DVB-S2, but the Picotuner (Serit) will not declare lock on it. Bars
and tone, 20 s, muxed to fit QPSK 3/4 at 2 MS/s:

    ffmpeg -f lavfi -i smptehdbars=size=1280x720:rate=25 \
      -f lavfi -i "aevalsrc=0.125893*sin(2*PI*1000*t)|0.125893*sin(2*PI*1000*t):s=48000:c=stereo" \
      -t 20 -c:v libx264 -b:v 1500k -maxrate 1500k -bufsize 1500k -g 50 \
      -c:a aac -b:a 128k -f mpegts -muxrate 2800000 /tmp/testcard.ts

The tone is stereo at -18 dBFS on each leg, UK alignment level. (ffmpeg's
sine source is mono at -18 dBFS, which reads -21 once spread over two.)

Ctrl-C stops it and mutes the SDR.
"""
import argparse
import os
import signal
import subprocess
import sys
import tempfile

# The modulator lives beside lynx_tx.py, one level up.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FECS = ["1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "8/9", "9/10"]
MODS = {"qpsk": "MOD_QPSK", "8psk": "MOD_8PSK", "16apsk": "MOD_16APSK", "32apsk": "MOD_32APSK"}
ROLLOFFS = {"0.35": "RO_0_35", "0.25": "RO_0_25", "0.20": "RO_0_20"}
MUTE_DB = -89.75


def fec_const(dtv, fec):
    return getattr(dtv, "C" + fec.replace("/", "_"))


def build_tone(args, out_path):
    """A single tone at +offset from the carrier: for zero-beating against a
    trusted receiver. Offset, so the LO leakage cannot be mistaken for it."""
    from gnuradio import analog, blocks, gr
    dac = args.sr * 1e3 * _sps(args)
    tb = gr.top_block()
    src = analog.sig_source_c(dac, analog.GR_COS_WAVE, args.tone * 1e3, args.level, 0)
    to_s16 = blocks.complex_to_interleaved_short(False, 32767)
    sink = blocks.file_sink(gr.sizeof_short, out_path, False)
    tb.connect(src, to_s16, sink)
    return tb


def _sps(args):
    """Samples per symbol, from the same rule Lynx-Tx uses (dvbs2mod)."""
    import dvbs2mod
    return dvbs2mod.sps({"symbol_rate_ks": args.sr})


def build(args, out_path):
    if args.tone is not None:
        return build_tone(args, out_path)
    # One modulator for Lynx-Tx and this tool: dvbs2_flowgraph.py.
    import dvbs2_flowgraph
    return dvbs2_flowgraph.build(args.ts, out_path, args.sr * 1e3, args.fec,
                                 args.mod.upper(), args.rolloff, args.pilots,
                                 _sps(args), args.level, repeat=True)


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
    p.add_argument("--rolloff", default="0.35", choices=list(ROLLOFFS))
    p.add_argument("--no-pilots", dest="pilots", action="store_false")
    p.add_argument("--gain", type=float, default=-10.0,
                   help="AD9361 TX gain in dB (0 is full, -89.75 is mute)")
    p.add_argument("--level", type=float, default=0.35,
                   help="digital level, 0-1 of full scale")
    p.add_argument("--ts", help="transport stream file, looped")
    p.add_argument("--tone", type=float, metavar="KHZ",
                   help="send a plain tone this many kHz above --freq instead")
    p.add_argument("--buffer", type=int, default=1048576,
                   help="iio_writedev buffer, samples (default 1048576)")
    p.add_argument("--file", help="write IQ (int16 I/Q) here instead of the SDR")
    args = p.parse_args()
    if args.tone is None and not args.ts:
        p.error("--ts is required (or --tone for a test tone)")

    if args.file:
        build(args, args.file).run()
        return

    dac = int(args.sr * 1e3 * _sps(args))
    iio(args.uri, "voltage0", "hardwaregain", f"{MUTE_DB:g}")
    iio(args.uri, "altvoltage1", "frequency", str(int(round(args.freq * 1e6))))
    iio(args.uri, "voltage0", "sampling_frequency", str(dac))
    iio(args.uri, "voltage0", "rf_bandwidth", str(max(dac, 2000000)))

    fifo = os.path.join(tempfile.mkdtemp(), "iq")
    os.mkfifo(fifo)
    writer = subprocess.Popen(
        f"iio_writedev -u {args.uri} -b {args.buffer} cf-ad9361-dds-core-lpc "
        f"voltage0 voltage1 < {fifo}", shell=True, start_new_session=True)
    tb = build(args, fifo)
    tb.start()
    iio(args.uri, "voltage0", "hardwaregain", f"{args.gain:g}")
    if args.tone is not None:
        print(f"Tone on air at {args.freq * 1e3 + args.tone:.3f} kHz "
              f"(carrier {args.freq:g} MHz + {args.tone:g} kHz), gain {args.gain:g} dB. "
              f"Ctrl-C to stop.", flush=True)
    else:
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
