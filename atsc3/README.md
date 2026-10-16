# ATSC 3.0 Beta: First Lock 16 October 2026

- Flowgraph: vv031_tx.grc (Ron W6RZ's vv031, 256QAM 9/15, 8K FFT, GI5_1024, SP3_4)
- IQ: complex_to_interleaved_short, scale 32767, via FIFO to iio_writedev
- LibreSDR: 557 MHz, 6.912 MS/s, rf_bandwidth 6 MHz, hardwaregain 0, cabled with 40-50 dB pad
- Receiver: HDHomeRun hdhomerun_dvr_atsc3, channel atsc3:557000000
- Result: lock=atsc3 ss=100 snq=100 seq=100 bps=21480128 pps=0
