"""Lynx-Tx TX monitor - a live spectrum and waterfall of what is on air.

The LibreSDR's receive side has its own LO, so it can sit on the transmit
frequency, fed from a feeder probe, while the transmitter carries on. The
ROCK does the FFT and pushes each spectrum to the browser as Server-Sent
Events; the page draws a spectrum line and a scrolling waterfall.

SAFE WHILE TRANSMITTING: on the AD9361 receive and transmit share one
sample clock, so the sample rate is only ever READ. Only the receive LO and
the receive gain are set, and both belong to the receive side alone. The
span is therefore the transmitter's own sample rate.

Capture runs only while someone is watching: it starts when the first page
opens and stops a few seconds after the last one closes.

Gain is manual (a slider) or a slow automatic: the carrier is averaged over
a couple of seconds and the gain only moves when it is more than 3 dB from
-12 dBFS, the sweet spot between the receiver's floor and the converter
clipping on the signal's peaks. Clipping always backs off at once, and the
page says so instead of drawing misleading shoulders.

A SEPARATE RECEIVER for a wider view: the transmitting LibreSDR can only
ever show its own sample rate (both receive channels share that clock). A
second Pluto/LibreSDR has its own clock, so it can capture 16 MS/s or more.
In lynx_tx.yaml:
    monitor:
      uri: ip:192.168.2.1        # e.g. a Pluto on the ROCK's USB
      own_clock: true            # it may set its own sample rate
      sample_rate: 16000000
      # centre_mhz defaults to the transmit frequency

Wiring into Lynx-Tx (two lines):
    import tx_monitor
    tx_monitor.install(app, lambda: config)
Pages: /monitor   Stream: /api/monitor/stream
"""
import asyncio
import json
import os
import subprocess
import threading
import time

import numpy as np
from fastapi import Request
from fastapi.responses import HTMLResponse, StreamingResponse

try:
    import iio            # python3-libiio
except ImportError:
    iio = None

N_FFT = 4096
AVERAGE = 8                # FFTs averaged per update
BINS_OUT = 1024            # points sent to the browser
TARGET_DBFS = -12.0
GAIN_MIN, GAIN_MAX = 0.0, 60.0
SIM = os.environ.get("LYNX_MONITOR_SIM") == "1"

_state = {"watchers": 0, "running": False, "frame": None, "seq": 0, "error": "",
          "mode": "auto", "manual_gain": 10.0}
_lock = threading.Lock()


# ---- capture sources ----------------------------------------------------
class _IioSource:
    """libiio bindings: one buffer, refilled each update."""

    def __init__(self, uri, own=None):
        self.ctx = iio.Context(uri)
        self.phy = self.ctx.find_device("ad9361-phy")
        rx = self.ctx.find_device("cf-ad9361-lpc")
        self._rxch = self.phy.find_channel("voltage0", False)
        if own:                                   # a receiver of our own: set rate and tune
            fs, lo = own
            self._rxch.attrs["sampling_frequency"].value = str(int(fs))
            self._rxch.attrs["rf_bandwidth"].value = str(int(fs * 0.9))
            self.lo = lo
        else:                                     # the transmitter's: read, never set
            self.lo = float(self.phy.find_channel("altvoltage1", True).attrs["frequency"].value)
        self.fs = float(self._rxch.attrs["sampling_frequency"].value)
        self.phy.find_channel("altvoltage0", True).attrs["frequency"].value = str(int(self.lo))
        self._rxch.attrs["gain_control_mode"].value = "manual"
        for name in ("voltage0", "voltage1"):
            rx.find_channel(name).enabled = True
        self.buf = iio.Buffer(rx, N_FFT * AVERAGE, False)

    def set_gain(self, g):
        self._rxch.attrs["hardwaregain"].value = str(int(g))

    def read(self):
        self.buf.refill()
        return self.buf.read()

    def close(self):
        self.buf = None
        self.ctx = None


class _CliSource:
    """Fallback without the bindings: iio_attr / iio_readdev, slower."""

    def __init__(self, uri, own=None):
        self.uri = uri
        if own:
            fs, self.lo = own
            self._attr("-i", "-c", "ad9361-phy", "voltage0", "sampling_frequency", str(int(fs)))
            self._attr("-i", "-c", "ad9361-phy", "voltage0", "rf_bandwidth", str(int(fs * 0.9)))
        else:
            self.lo = float(self._attr("-o", "-c", "ad9361-phy", "altvoltage1", "frequency"))
        self.fs = float(self._attr("-i", "-c", "ad9361-phy", "voltage0", "sampling_frequency"))
        self._attr("-o", "-c", "ad9361-phy", "altvoltage0", "frequency", str(int(self.lo)))
        self._attr("-i", "-c", "ad9361-phy", "voltage0", "gain_control_mode", "manual")

    def _attr(self, *args):
        r = subprocess.run(["iio_attr", "-u", self.uri, *args],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "iio_attr failed")
        return r.stdout.strip()

    def set_gain(self, g):
        self._attr("-i", "-c", "ad9361-phy", "voltage0", "hardwaregain", str(int(g)))

    def read(self):
        n = N_FFT * AVERAGE
        r = subprocess.run(["iio_readdev", "-u", self.uri, "-b", str(n), "-s", str(n),
                            "cf-ad9361-lpc", "voltage0", "voltage1"],
                           capture_output=True, timeout=15)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(r.stderr.decode().strip() or "iio_readdev failed")
        return r.stdout

    def close(self):
        pass


class _SimSource:
    """Synthetic DVB-S2-shaped carrier, for testing without hardware."""

    def __init__(self, sr, fs=4e6):
        self.fs, self.lo, self.gain, self.sr = fs, 1304e6, 0.0, sr
        self.rng = np.random.default_rng(7)

    def set_gain(self, g):
        self.gain = g

    def read(self):
        n = N_FFT * AVERAGE
        x = self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)
        f = np.fft.fftfreq(n, 1 / self.fs)
        h = np.where(np.abs(f) < self.sr * 1.35 / 2, 1.0, 10 ** (-40 / 20))
        y = np.fft.ifft(np.fft.fft(x) * h) * 0.02 * 10 ** (self.gain / 20)
        y += (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)) * 2e-4
        y = np.clip(y.real, -0.999, 0.999) + 1j * np.clip(y.imag, -0.999, 0.999)
        s = np.empty(2 * n, dtype="<i2")
        s[0::2], s[1::2] = (y.real * 2047).astype("<i2"), (y.imag * 2047).astype("<i2")
        time.sleep(0.05)
        return s.tobytes()

    def close(self):
        pass


# ---- the analysis -----------------------------------------------------------
def _analyse(raw, fs, lo, sr, rolloff):
    s = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 2048.0
    clipped = float(np.mean(np.abs(s) > 0.97)) > 1e-4
    iq = s[0::2] + 1j * s[1::2]
    win = np.hanning(N_FFT)
    segs = max(1, len(iq) // N_FFT)
    psd = np.zeros(N_FFT)
    for k in range(segs):
        seg = iq[k * N_FFT:(k + 1) * N_FFT]
        if len(seg) < N_FFT:
            break
        psd += np.abs(np.fft.fft((seg - seg.mean()) * win)) ** 2
    psd = np.fft.fftshift(psd / segs / (win ** 2).sum())
    db = 10 * np.log10(psd + 1e-20)
    off = (np.arange(N_FFT) - N_FFT / 2) * fs / N_FFT
    edge = 0.5 * sr * (1 + rolloff)
    inband = np.abs(off) < 0.4 * sr
    shoulder_zone = (np.abs(off) > edge + 0.05 * sr) & (np.abs(off) < 0.45 * fs)
    floor = float(np.percentile(db, 10))
    carrier = float(db[inband].mean()) if inband.any() else float("nan")
    shoulder = float(db[shoulder_zone].mean()) if shoulder_zone.any() else None
    # Max-hold into the display bins, so narrow spurs are not averaged away.
    disp = db.reshape(BINS_OUT, N_FFT // BINS_OUT).max(axis=1)
    return {
        "db": [round(float(v), 1) for v in disp],
        "lo": float(lo), "fs": float(fs), "sr": float(sr), "edge": float(edge),
        "carrier": round(carrier, 1), "floor": round(floor, 1),
        "range": round(carrier - floor, 1),
        "shoulder": None if shoulder is None or shoulder - floor < 3 else round(carrier - shoulder, 1),
        "shoulder_visible": bool(shoulder_zone.any()),
        "clipped": bool(clipped),
    }


# ---- the capture loop ----------------------------------------------------------
def _run(get_config):
    cfg = get_config()
    m = cfg.get("modulation", {})
    sr = float(m.get("symbol_rate_ks", 2000)) * 1e3
    try:
        rolloff = float(m.get("rolloff", 0.35))
    except (TypeError, ValueError):
        rolloff = 0.35
    mon = cfg.get("monitor") or {}
    uri = mon.get("uri") or f"ip:{cfg.get('libre', {}).get('host', '192.168.0.172')}"
    own = None
    if mon.get("own_clock"):
        centre = float(mon.get("centre_mhz") or m.get("frequency_mhz", 1304)) * 1e6
        own = (float(mon.get("sample_rate", 16e6)), centre)
    try:
        if SIM:
            src = _SimSource(sr, own[0] if own else 4e6)
        elif iio is not None:
            src = _IioSource(uri, own)
        else:
            src = _CliSource(uri, own)
    except Exception as e:
        with _lock:
            _state.update(error=f"cannot reach the LibreSDR receiver: {e}", running=False)
        return
    gain = 10.0
    src.set_gain(gain)
    idle_since = None
    avg, last_step = None, 0.0
    try:
        while True:
            with _lock:
                watchers = _state["watchers"]
            if watchers == 0:
                idle_since = idle_since or time.time()
                if time.time() - idle_since > 5:
                    break
            else:
                idle_since = None
            try:
                frame = _analyse(src.read(), src.fs, src.lo, sr, rolloff)
            except Exception as e:
                with _lock:
                    _state["error"] = f"capture failed: {e}"
                time.sleep(1)
                continue
            with _lock:
                mode, manual = _state["mode"], _state["manual_gain"]
            new = gain
            if mode == "manual":
                new = manual
                avg = None
            elif frame["clipped"]:
                new = max(GAIN_MIN, gain - 6)                     # clipping: at once
                avg, last_step = None, time.time()
            elif frame["carrier"] == frame["carrier"]:            # not NaN
                # Slow automatic: average the carrier, move only when it is
                # well off target, at most every 2 s, in small steps.
                avg = frame["carrier"] if avg is None else avg + 0.1 * (frame["carrier"] - avg)
                err = TARGET_DBFS - avg
                if abs(err) > 3 and time.time() - last_step > 2:
                    new = min(GAIN_MAX, max(GAIN_MIN, gain + max(-3, min(3, round(err)))))
                    last_step = time.time()
                    avg = None                                    # re-measure after a step
            if new != gain:
                gain = new
                src.set_gain(gain)
            frame["gain"] = round(gain, 1)
            frame["mode"] = mode
            with _lock:
                _state["frame"] = frame
                _state["seq"] += 1
                _state["error"] = ""
    finally:
        src.close()
        with _lock:
            _state["running"] = False


def _ensure_running(get_config):
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    threading.Thread(target=_run, args=(get_config,), daemon=True).start()


# ---- web ---------------------------------------------------------------------------
def install(app, get_config):
    @app.get("/monitor", response_class=HTMLResponse)
    def monitor_page():
        return PAGE

    @app.post("/api/monitor/gain")
    async def monitor_gain(request: Request):
        body = await request.json()
        with _lock:
            if body.get("mode") in ("auto", "manual"):
                _state["mode"] = body["mode"]
            if "gain" in body:
                _state["manual_gain"] = float(min(GAIN_MAX, max(GAIN_MIN, float(body["gain"]))))
            return {"mode": _state["mode"], "gain": _state["manual_gain"]}

    @app.get("/api/monitor/stream")
    async def monitor_stream(request: Request):
        _state_change(+1)
        _ensure_running(get_config)

        async def events():
            last = -1
            try:
                while not await request.is_disconnected():
                    with _lock:
                        seq, frame, err = _state["seq"], _state["frame"], _state["error"]
                    if err:
                        yield f"event: problem\ndata: {json.dumps(err)}\n\n"
                        await asyncio.sleep(1)
                        _ensure_running(get_config)
                        continue
                    if seq != last and frame:
                        last = seq
                        yield f"data: {json.dumps(frame)}\n\n"
                    await asyncio.sleep(0.05)
            finally:
                _state_change(-1)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})



def _state_change(d):
    with _lock:
        _state["watchers"] = max(0, _state["watchers"] + d)


PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lynx-Tx - TX monitor</title>
<style>
 body{background:#1a1a2e;color:#e0e0e0;font-family:system-ui,sans-serif;margin:0;padding:12px}
 .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px}
 a.btn{color:#e0e0e0;border:1px solid #3a4a63;border-radius:6px;padding:4px 10px;text-decoration:none}
 .tile{background:#16213e;border:1px solid #0f3460;border-radius:6px;padding:4px 10px}
 .tile b{font-family:monospace;font-size:1.1em;display:inline-block;text-align:right}
 #lo{width:12ch} #car{width:10ch} #rng{width:7ch} #sh{width:21ch} #gn{width:5ch}
 #warn{display:none;background:#7f1d1d;border-radius:6px;padding:4px 10px}
 canvas{display:block;width:100%;background:#0b0f1d;border:1px solid #0f3460;border-radius:6px}
 .note{color:#9fb3d1;font-size:.85em;margin-top:6px}
</style></head><body>
<div class="bar">
 <b>&#x1F4C8; TX monitor</b>
 <a class="btn" href="/">&larr; Transmitter</a>
 <span class="tile">Centre <b id="lo">&mdash;</b></span>
 <span class="tile">Carrier <b id="car">&mdash;</b></span>
 <span class="tile">Range <b id="rng">&mdash;</b></span>
 <span class="tile">Shoulders <b id="sh">&mdash;</b></span>
 <span class="tile">RX gain <b id="gn">&mdash;</b>
   <select id="gmode" onchange="setGain()" style="margin-left:6px">
     <option value="auto">Auto (slow)</option><option value="manual">Manual</option></select>
   <input type="range" id="gslider" min="0" max="60" step="1" value="10"
          oninput="setGain()" style="vertical-align:middle;width:140px"></span>
 <span id="warn">&#x26A0;&#xFE0F; Receiver overloaded &mdash; reducing gain</span>
</div>
<canvas id="spec" height="1142"></canvas>
<canvas id="fall" height="260" style="margin-top:8px"></canvas>
<div class="note" id="note">A monitor, not a measurement: Watch the shape and changes. The span is the
transmitter's own sample rate, so the edges of the picture are the receiver's filters, not the signal.</div>
<script>
const spec = document.getElementById('spec'), fall = document.getElementById('fall');
const sc = spec.getContext('2d'), fc = fall.getContext('2d');
let lo = -100, hi = 0, img = null;
function fit(){ for (const c of [spec, fall]) c.width = c.clientWidth * devicePixelRatio; img = null; }
addEventListener('resize', fit); fit();
function colour(v){                       // 0..1 -> dark blue, cyan, yellow, red
  const s = [[11,15,29],[20,60,160],[0,200,220],[250,220,40],[230,40,30]];
  const x = Math.max(0, Math.min(.999, v)) * (s.length - 1), i = Math.floor(x), t = x - i;
  return s[i].map((a, k) => Math.round(a + (s[i + 1][k] - a) * t));
}
function draw(f){
  const W = spec.width, H = spec.height, n = f.db.length, L = 46 * devicePixelRatio, B = 22 * devicePixelRatio;
  const want_lo = Math.floor((f.floor - 10) / 10) * 10, want_hi = Math.ceil((Math.max(...f.db) + 5) / 10) * 10;
  lo += (want_lo - lo) * .2; hi += (want_hi - hi) * .2;                 // ease the scale
  const X = i => L + i / (n - 1) * (W - L - 4), Y = v => 6 + (hi - v) / (hi - lo) * (H - B - 6);
  sc.clearRect(0, 0, W, H); sc.font = (11 * devicePixelRatio) + 'px system-ui'; sc.fillStyle = '#9fb3d1';
  sc.strokeStyle = '#26324d'; sc.lineWidth = 1;
  for (let g = Math.ceil(lo / 10) * 10; g <= hi; g += 10) {
    sc.beginPath(); sc.moveTo(L, Y(g)); sc.lineTo(W, Y(g)); sc.stroke();
    sc.textAlign = 'left'; sc.fillText(g + ' dB', 2, Y(g) + 4);
  }
  for (let k = 0; k <= 8; k++) {
    const x = L + k / 8 * (W - L - 4), mhz = (f.lo + (k / 8 - .5) * f.fs) / 1e6;
    sc.beginPath(); sc.moveTo(x, 6); sc.lineTo(x, H - B); sc.stroke();
    sc.textAlign = k === 0 ? 'left' : k === 8 ? 'right' : 'center';
    sc.fillText(mhz.toFixed(3), x, H - 6);
  }
  sc.strokeStyle = '#e8a33d'; sc.setLineDash([4, 4]);                  // channel edges
  for (const e of [-f.edge, f.edge]) {
    const x = L + (e / f.fs + .5) * (W - L - 4);
    sc.beginPath(); sc.moveTo(x, 6); sc.lineTo(x, H - B); sc.stroke();
  }
  sc.setLineDash([]); sc.strokeStyle = '#ff5a4f'; sc.lineWidth = 1.5 * devicePixelRatio;
  sc.beginPath(); f.db.forEach((v, i) => i ? sc.lineTo(X(i), Y(v)) : sc.moveTo(X(i), Y(v))); sc.stroke();
  // waterfall: shift down one row, paint the new spectrum on top,
  // under the same frequency axis as the spectrum (same left margin).
  const FW = fall.width, FH = fall.height, PW = Math.round(W - L - 4);
  fc.drawImage(fall, 0, 0, FW, FH - 1, 0, 1, FW, FH - 1);
  const row = fc.createImageData(PW, 1);
  for (let x = 0; x < PW; x++) {
    const i = Math.min(n - 1, Math.floor(x / PW * n)), c = colour((f.db[i] - f.floor + 3) / (f.range + 6));
    row.data.set([c[0], c[1], c[2], 255], x * 4);
  }
  fc.putImageData(row, Math.round(L), 0);
  document.getElementById('lo').textContent = (f.lo / 1e6).toFixed(3) + ' MHz';
  document.getElementById('car').textContent = f.carrier.toFixed(1) + ' dBFS';
  document.getElementById('rng').textContent = f.range.toFixed(1) + ' dB';
  document.getElementById('sh').textContent = !f.shoulder_visible ? 'outside span'
    : f.shoulder === null ? 'below floor (\u2265' + f.range.toFixed(0) + ' dB)' : f.shoulder.toFixed(1) + ' dB down';
  document.getElementById('gn').textContent = f.gain.toFixed(0) + ' dB';
  if (!synced) {                       // pick up the server's mode once
    document.getElementById('gmode').value = f.mode;
    document.getElementById('gslider').value = f.gain;
    document.getElementById('gslider').disabled = f.mode !== 'manual';
    synced = true;
  }
  if (f.mode === 'auto') document.getElementById('gslider').value = f.gain;
  document.getElementById('warn').style.display = f.clipped ? 'inline-block' : 'none';
}
let gainTimer = null;
function setGain(){
  const mode = document.getElementById('gmode').value, g = +document.getElementById('gslider').value;
  document.getElementById('gslider').disabled = mode !== 'manual';
  clearTimeout(gainTimer);
  gainTimer = setTimeout(() => fetch('/api/monitor/gain', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode, gain: g})}), 120);
}
let synced = false;
const es = new EventSource('/api/monitor/stream');
es.onmessage = e => draw(JSON.parse(e.data));
es.addEventListener('problem', e => document.getElementById('note').textContent = JSON.parse(e.data));
</script></body></html>"""
