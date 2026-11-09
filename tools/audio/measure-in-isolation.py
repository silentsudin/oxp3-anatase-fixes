#!/usr/bin/env python3
"""Measure the OneXPlayer 3 Harman EQ filter chain in a throw-away PipeWire graph.

This is the measurement half of ``tools/audio/test-isolation.sh``.  The shell
script owns the isolated daemon (private XDG_RUNTIME_DIR, private PipeWire
config that loads only module-null-sink/-filter-chain plus a hardware-free
WirePlumber) and calls this program with the environment variables

    OXP3_AUDIO_TEST_RUNTIME   private runtime dir (the daemon socket lives here)
    OXP3_AUDIO_TEST_WORK      private dir for the generated WAVs
    OXP3_AUDIO_TEST_LOGS      private dir for logs/JSON
    OXP3_AUDIO_TEST_NODE      filter-chain capture node name to play into
    OXP3_AUDIO_TEST_MODE      ``eq`` or ``crossover``

Nothing here talks to the live session: every pw-* command gets
XDG_RUNTIME_DIR/PIPEWIRE_RUNTIME_DIR pointing at the private runtime, and the
graph contains only a null sink and the filter chain under test.

Modes

  eq          log-sweep transfer function vs the model in eq_response.py,
             1/3-octave per-band error, plus the hot-tone (limiter) checks
  crossover   log-sweep transfer function of the LR4 crossover graph
             (tools/audio/test-xover.conf) - the bands must sum flat

Reference run: the same sweep is played straight into the null sink and the
same monitor recorded, so playback/record converters cancel.  The transfer
function is |Y_filter(f)| / |Y_reference(f)| from one FFT of each recording; a
constant latency between the runs only adds a linear phase term and does not
change the magnitude.

Pure stdlib.  numpy is used when importable (container image) but never
required; the radix-2 FFT below is fast enough for 2^19 points in CPython.

Author: silentsudin <25103587+silentsudin@users.noreply.github.com>
Licence: GPL-2.0-or-later - our own code, no vendor code involved.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import signal
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import eq_response  # noqa: E402  (same-directory model)

RATE = 48000
FULL_SCALE = 32768.0

# Log sweep: 50 Hz .. 16 kHz, 3.5 s + 1.0 s lead + 0.5 s tail == 5.0 s.
SWEEP_F1 = 50.0
SWEEP_F2 = 16000.0
SWEEP_SEC = 3.5
SWEEP_LEAD = 1.0
SWEEP_TAIL = 0.5
SWEEP_AMP = 0.30

# Analysis window: 2^19 samples == 10.9 s, comfortably larger than a
# recording and a power of two for the radix-2 FFT.
FFT_N = 1 << 19

# One-third-octave band centres from 50 Hz to 16 kHz.
BAND_CENTERS = [50.0 * (2.0 ** (i / 3.0)) for i in range(0, 26)]

# Pass/fail thresholds (real graph, s16 I/O, 48 kHz, no resampling).
EQ_MAX_BAND_ERROR_DB = 1.0     # per band, 100 Hz .. 10 kHz
EQ_MAX_MEAN_ERROR_DB = 0.5     # mean |error| over that range
XOVER_MAX_DEVIATION_DB = 2.0   # summed LR4 vs reference, 100 Hz .. 16 kHz
LIMITER_MIN_ATTENUATION_DB = 3.0
LIMITER_MAX_SILENCE_DBFS = -60.0

# Hot-tone test signal: 0.5 s silence, 2.0 s at 0 dBFS, 1.5 s silence.
TONE_LEAD = 0.5
TONE_SEC = 2.0
TONE_TAIL = 1.5


# --------------------------------------------------------------------------
# WAV helpers (s16le stereo for playback, mono read-back)
# --------------------------------------------------------------------------
def write_wav(path, samples, rate=RATE):
    """Write mono float samples [-1,1] as a stereo s16le WAV (both channels)."""
    frames = bytearray()
    for x in samples:
        v = int(max(-1.0, min(1.0, x)) * 32767.0)
        frames += struct.pack("<hh", v, v)
    import wave

    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def read_mono(path):
    """Read a WAV, return (float samples of channel 0, rate)."""
    import wave

    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        data = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit("unexpected WAV sample width %d in %s" % (width, path))
    pcm = array.array("h")
    pcm.frombytes(data)
    if sys.byteorder == "big":  # array is native endian; WAV is little
        pcm.byteswap()
    if ch > 1:
        del pcm[1::ch]
    return [x / 32768.0 for x in pcm], rate


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------
def log_sweep():
    """Exponential sine sweep with raised-cosine ends, embedded in silence."""
    n = int(SWEEP_SEC * RATE)
    lead = int(SWEEP_LEAD * RATE)
    tail = int(SWEEP_TAIL * RATE)
    ratio = SWEEP_F2 / SWEEP_F1
    k = SWEEP_SEC / math.log(ratio)
    out = [0.0] * lead
    edge = int(0.02 * RATE)
    for i in range(n):
        t = i / RATE
        phase = 2.0 * math.pi * SWEEP_F1 * k * (math.exp(t / k) - 1.0)
        env = 1.0
        if i < edge:
            env = 0.5 - 0.5 * math.cos(math.pi * i / edge)
        elif i > n - edge:
            env = 0.5 - 0.5 * math.cos(math.pi * (n - i) / edge)
        out.append(SWEEP_AMP * env * math.sin(phase))
    out.extend([0.0] * tail)
    return out


def hot_tone(freq, amplitude=1.0):
    """0.5 s silence, 2 s tone at ``amplitude`` (0 dBFS == 1.0), 1.5 s silence."""
    lead = int(TONE_LEAD * RATE)
    n = int(TONE_SEC * RATE)
    tail = int(TONE_TAIL * RATE)
    edge = int(0.01 * RATE)
    out = [0.0] * lead
    for i in range(n):
        env = 1.0
        if i < edge:
            env = 0.5 - 0.5 * math.cos(math.pi * i / edge)
        elif i > n - edge:
            env = 0.5 - 0.5 * math.cos(math.pi * (n - i) / edge)
        out.append(amplitude * env * math.sin(2.0 * math.pi * freq * i / RATE))
    out.extend([0.0] * tail)
    return out


# --------------------------------------------------------------------------
# FFT
# --------------------------------------------------------------------------
def fft(values):
    """In-place iterative radix-2 FFT.  len(values) must be a power of two."""
    n = len(values)
    if n & (n - 1):
        raise ValueError("FFT length must be a power of two")
    a = [complex(x, 0.0) for x in values]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        half = size >> 1
        step = -2.0 * math.pi / size
        wm = complex(math.cos(step), math.sin(step))
        for start in range(0, n, size):
            w = 1.0 + 0.0j
            for k in range(start, start + half):
                u = a[k]
                v = a[k + half] * w
                a[k] = u + v
                a[k + half] = u - v
                w *= wm
        size <<= 1
    return a


def magnitude_spectrum(samples, n=FFT_N):
    """|FFT| of the first n samples (zero padded), numpy if available."""
    if len(samples) >= n:
        buf = samples[:n]
    else:
        buf = list(samples) + [0.0] * (n - len(samples))
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        return abs(np.fft.rfft(np.asarray(buf, dtype=np.float64)))
    spec = fft(buf)
    return [abs(spec[k]) for k in range(n // 2 + 1)]


# --------------------------------------------------------------------------
# Transfer function / band comparison
# --------------------------------------------------------------------------
def band_bins(center, rate=RATE, n=FFT_N):
    lo = center / (2.0 ** (1.0 / 6.0))
    hi = center * (2.0 ** (1.0 / 6.0))
    df = rate / float(n)
    k0 = max(1, int(math.ceil(lo / df)))
    k1 = max(k0 + 1, int(math.floor(hi / df)) + 1)
    return k0, min(k1, n // 2)


def band_level_db(mag, center, rate=RATE, n=FFT_N):
    """Energy-average level in a 1/3-octave band."""
    k0, k1 = band_bins(center, rate, n)
    acc = 0.0
    for k in range(k0, k1):
        acc += mag[k] * mag[k]
    if k1 <= k0 or acc <= 0.0:
        return float("-inf")
    return 10.0 * math.log10(acc / (k1 - k0))


def model_band_db(center, bands=eq_response.BANDS, pre=eq_response.PREGain_DB,
                  rate=RATE, n=FFT_N):
    """Energy-average of the analytic model over the same FFT bins as the band.

    Energy (not amplitude) averaging is required because the measured band
    level is also an energy average; using the same bins makes the comparison
    exact for a spectrum that equals the model.
    """
    k0, k1 = band_bins(center, rate, n)
    df = rate / float(n)
    acc = 0.0
    for k in range(k0, k1):
        f = k * df
        acc += 10.0 ** (eq_response.curve([f], bands=bands, pre=pre)[0] / 10.0)
    if k1 <= k0:
        return float("-inf")
    return 10.0 * math.log10(acc / (k1 - k0))


def compare_bands(meas_mag, ref_mag, centers=None, model_bands=None,
                  model_pre=None, rate=RATE, n=FFT_N):
    """Per-band measured-vs-model table.

    ``meas_mag``/``ref_mag`` are magnitude spectra of the filtered and the
    reference recording.  Returns a list of dicts with the band centre, the
    measured gain, the model gain and the difference.  Pure function: used by
    the unit tests in tests/unit/test_eq_measure.py.
    """
    centers = BAND_CENTERS if centers is None else centers
    bands = eq_response.BANDS if model_bands is None else model_bands
    pre = eq_response.PREGain_DB if model_pre is None else model_pre
    rows = []
    for f in centers:
        m = band_level_db(meas_mag, f, rate, n)
        r = band_level_db(ref_mag, f, rate, n)
        if not (math.isfinite(m) and math.isfinite(r)):
            # No reference energy (below the sweep's range or a dead run).
            continue
        measured = m - r
        model = model_band_db(f, bands=bands, pre=pre, rate=rate, n=n)
        rows.append({"freq_hz": round(f, 2), "measured_db": measured,
                     "model_db": model, "diff_db": measured - model})
    return rows


def summarise(rows, lo=100.0, hi=10000.0):
    """Mean/max |diff| over a frequency range; a dict for JSON."""
    sel = [r for r in rows if lo <= r["freq_hz"] <= hi]
    if not sel:
        return {"bands": 0}
    diffs = [abs(r["diff_db"]) for r in sel]
    return {
        "bands": len(sel),
        "mean_abs_db": sum(diffs) / len(diffs),
        "max_abs_db": max(diffs),
        "max_at_hz": sel[diffs.index(max(diffs))]["freq_hz"],
    }


# --------------------------------------------------------------------------
# Isolated-graph plumbing (every command pinned to the private runtime)
# --------------------------------------------------------------------------
def pw_env():
    env = dict(os.environ)
    rt = env.get("OXP3_AUDIO_TEST_RUNTIME")
    if not rt:
        raise SystemExit("measure-in-isolation.py: OXP3_AUDIO_TEST_RUNTIME is not set")
    # Strip anything that could point a client at the live session.
    env.pop("PIPEWIRE_REMOTE", None)
    env.pop("PIPEWIRE_CONFIG_DIR", None)
    env["XDG_RUNTIME_DIR"] = rt
    env["PIPEWIRE_RUNTIME_DIR"] = rt
    return env


def run(cmd, timeout=30, check=False):
    return subprocess.run(cmd, env=pw_env(), timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, check=check)


def wait_ports(pattern, direction="-o", timeout=10.0):
    """Wait until ``pw-link -o/-i`` lists a port matching ``pattern``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = run(["pw-link", direction], timeout=5).stdout
        if pattern in out:
            return True
        time.sleep(0.05)
    return False


def link(out_port, in_port):
    res = run(["pw-link", "-L", out_port, in_port], timeout=10)
    if res.returncode != 0:
        raise RuntimeError("pw-link %s -> %s failed: %s"
                           % (out_port, in_port, res.stderr.strip()))
    return True


def unlink_all(pattern):
    """Remove links whose ``pw-link -l`` block mentions pattern (best effort)."""
    out = run(["pw-link", "-l"], timeout=5).stdout
    stack = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("|") and line:
            stack.append(line)
            continue
        if line.startswith("|") and stack:
            src = line.split("<-")[-1].strip() if "<-" in line else None
            dst = line.split("->")[-1].strip() if "->" in line else None
            if src and dst and pattern in (src + dst):
                run(["pw-link", "-d", src, dst], timeout=5)


def record_to(path, seconds_extra=0.8):
    """Start an unlinked pw-record; caller links the monitor in, then stops it."""
    rec = subprocess.Popen(
        ["pw-record", "--target", "0", "--rate", str(RATE), "--channels", "2",
         "--format", "s16", "-P", '{"node.name":"audiotest-rec"}', path],
        env=pw_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not wait_ports("audiotest-rec:input_FL", "-i"):
        rec.terminate()
        raise RuntimeError("pw-record did not publish input ports")
    # Record from the null-sink monitor: the only sink in the private graph.
    link("iso_null:monitor_FL", "audiotest-rec:input_FL")
    link("iso_null:monitor_FR", "audiotest-rec:input_FR")
    return rec


def play_to(node, path):
    return subprocess.Popen(
        ["pw-play", "--target", node, path],
        env=pw_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def capture(target, wav_in, wav_out):
    """Play ``wav_in`` into ``target`` and record the null-sink monitor.

    Returns the recorded mono samples.  ``target`` is a node name; the session
    manager (private WirePlumber) links the stream to that node by name.
    """
    rec = record_to(wav_out)
    try:
        player = play_to(target, wav_in)
        try:
            player.wait(timeout=30)
        finally:
            if player.poll() is None:
                player.terminate()
                player.wait(timeout=5)
        if player.returncode not in (0, None):
            raise RuntimeError("pw-play failed: %s"
                               % (player.stderr.read().decode(errors="replace")))
        time.sleep(0.5)
    finally:
        rec.send_signal(signal.SIGINT)
        try:
            rec.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rec.kill()
            rec.wait()
    if not os.path.exists(wav_out) or os.path.getsize(wav_out) < 4096:
        raise RuntimeError("no audio captured to %s" % wav_out)
    samples, rate = read_mono(wav_out)
    if rate != RATE:
        raise RuntimeError("recorded %d Hz, expected %d" % (rate, RATE))
    return samples


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def dbfs(amplitude):
    """dBFS for a linear amplitude in [0, 1] (full scale == 1.0)."""
    return 20.0 * math.log10(max(amplitude, 1e-9))


def window_rms(samples, start, length):
    seg = samples[start:start + length]
    if not seg:
        return 0.0
    return math.sqrt(sum(x * x for x in seg) / len(seg))


def peak_rms_db(samples, n=50, hop=10):
    """[(rms_dbfs)] in 50 ms windows, 10 ms hop."""
    win = int(RATE * n / 1000)
    step = int(RATE * hop / 1000)
    out = []
    for i in range(0, max(0, len(samples) - win), step):
        out.append(dbfs(window_rms(samples, i, win)))
    return out


def limiter_model(samples, threshold=0.607, attack=0.200, release=0.360,
                  ratio=2.0, rate=RATE):
    """Offline model of the vendor limiter (feed-forward peak limiter).

    PipeWire's module-filter-chain has no dynamics builtin (verified: the only
    builtin labels are bq_*, copy, dcblock, delay, gain, invert, mixer), so the
    vendor limiter can only be *modelled* here.  Threshold/attack/release are
    the values recovered from the vendor preset; ``ratio`` is the
    above-threshold compression.  The detector is a peak hold (fast attack,
    release time constant) and the gain slews with the attack/release times;
    this is the usual reading of the vendor fields, and it is exactly
    testable: a hot tone is attenuated and the gain comes back afterwards.
    """
    a_att = math.exp(-1.0 / (attack * rate))
    a_rel = math.exp(-1.0 / (release * rate))
    env = 0.0
    gain = 1.0
    out = []
    gains = []
    for x in samples:
        ax = abs(x)
        env = ax if ax > env else env * a_rel
        if env <= threshold or env <= 1e-9:
            target = 1.0
        else:
            target = (threshold + (env - threshold) / ratio) / env
        coeff = a_att if target < gain else a_rel
        gain = coeff * gain + (1.0 - coeff) * target
        out.append(x * gain)
        gains.append(20.0 * math.log10(max(gain, 1e-9)))
    return out, gains


def limiter_checks(node, work):
    """Hot 0 dBFS tones through the real graph + the offline vendor model."""
    report = {}
    for label, freq in (("1khz", 1000.0), ("142hz", 142.0)):
        wav = os.path.join(work, "hot-%s.wav" % label)
        write_wav(wav, hot_tone(freq))
        rec = capture(node, wav, os.path.join(work, "hot-%s-rec.wav" % label))
        tone_start = int(TONE_LEAD * RATE)
        steady_start = tone_start + int(1.5 * RATE)
        steady = window_rms(rec, steady_start, int(0.4 * RATE))
        peak = max(abs(x) for x in rec)
        clipped = sum(1 for x in rec if abs(x) >= 32700 / 32768.0)
        tail = window_rms(rec, len(rec) - int(0.4 * RATE), int(0.35 * RATE))
        model = eq_response.curve([freq])[0]
        report[label] = {
            "freq_hz": freq,
            "input_dbfs": 0.0,
            "steady_out_dbfs": dbfs(steady),
            "model_dbfs": model,
            "diff_db": dbfs(steady) - model,
            "peak_dbfs": dbfs(peak),
            "clipped_samples": clipped,
            "tail_dbfs": dbfs(tail),
        }
        # The model curve is an amplitude gain; a sine of amplitude ``a`` has
        # RMS a/sqrt(2), i.e. -3.01 dB relative to its peak.  Compare the
        # measured peak (the transfer gain) with the model, and report both.
        report[label]["peak_minus_model_db"] = dbfs(peak) - model
    # Offline vendor limiter on the same 1 kHz 0 dBFS tone (model only).
    tone, _ = read_mono(os.path.join(work, "hot-1khz.wav"))
    lim, gains = limiter_model(tone)
    tone_start = int(TONE_LEAD * RATE)
    in_rms = window_rms(tone, tone_start + int(1.0 * RATE), int(0.4 * RATE))
    steady = window_rms(lim, tone_start + int(1.5 * RATE), int(0.4 * RATE))
    after = window_rms(lim, tone_start + int(TONE_SEC * RATE) + int(0.9 * RATE),
                       int(0.3 * RATE))
    post_gain = max(gains[tone_start + int(TONE_SEC * RATE) + int(0.9 * RATE):
                          tone_start + int(TONE_SEC * RATE) + int(1.2 * RATE)])
    report["vendor_limiter_model"] = {
        "threshold": 0.607, "attack_s": 0.200, "release_s": 0.360,
        "ratio": 2.0,
        "input_rms_dbfs": dbfs(in_rms),
        "steady_out_rms_dbfs": dbfs(steady),
        "attenuation_db": dbfs(in_rms) - dbfs(steady),
        "post_tone_rms_dbfs": dbfs(after),
        "residual_gain_reduction_db": post_gain,
    }
    return report


def eq_mode(args):
    node = args.node
    work = args.work
    sweep = os.path.join(work, "sweep.wav")
    write_wav(sweep, log_sweep())
    ref = capture("iso_null", sweep, os.path.join(work, "ref.wav"))
    meas = capture(node, sweep, os.path.join(work, "eq.wav"))
    ref_mag = magnitude_spectrum(ref)
    meas_mag = magnitude_spectrum(meas)
    rows = compare_bands(meas_mag, ref_mag)
    summary = summarise(rows)
    ok = (summary.get("bands", 0) >= 10
          and summary.get("max_abs_db", 999) <= EQ_MAX_BAND_ERROR_DB
          and summary.get("mean_abs_db", 999) <= EQ_MAX_MEAN_ERROR_DB)
    result = {"mode": "eq", "node": node, "bands": rows,
              "summary": summary, "pass": ok}
    if not args.skip_limiter:
        lim = limiter_checks(node, work)
        result["limiter"] = lim
        hot = lim["1khz"]
        vendor = lim["vendor_limiter_model"]
        hot142 = lim["142hz"]
        lim_ok = (hot["peak_minus_model_db"] <= 0.5
                  and hot["clipped_samples"] == 0
                  and hot["tail_dbfs"] <= LIMITER_MAX_SILENCE_DBFS
                  and hot142["peak_dbfs"] <= 0.0
                  and hot142["clipped_samples"] == 0
                  and vendor["attenuation_db"] >= 1.0
                  and abs(vendor["residual_gain_reduction_db"]) <= 0.5)
        result["limiter_pass"] = lim_ok
        ok = ok and lim_ok
        result["pass"] = ok
    return result


def crossover_mode(args):
    """LR4 crossover: each branch -6 dB at the corner, low+high flat."""
    work = args.work
    sweep = os.path.join(work, "sweep.wav")
    write_wav(sweep, log_sweep())
    ref = capture("iso_null", sweep, os.path.join(work, "ref.wav"))
    ref_mag = magnitude_spectrum(ref)

    mags = {}
    for key, node in (("sum", "audiotest_xover.in"),
                      ("low", "audiotest_low.in"),
                      ("high", "audiotest_high.in")):
        samples = capture(node, sweep, os.path.join(work, "%s.wav" % key))
        mags[key] = magnitude_spectrum(samples)

    def gain(key, centre):
        m = band_level_db(mags[key], centre)
        r = band_level_db(ref_mag, centre)
        if not (math.isfinite(m) and math.isfinite(r)):
            return float("-inf")
        return m - r

    rows = []
    for f in BAND_CENTERS:
        v = gain("sum", f)
        if math.isfinite(v):
            rows.append({"freq_hz": round(f, 2), "sum_db": v,
                         "low_db": gain("low", f), "high_db": gain("high", f)})
    sel = [r["sum_db"] for r in rows if 100.0 <= r["freq_hz"] <= 16000.0]
    dev = max(abs(x) for x in sel) if sel else 999.0

    # Theory: each LR4 branch is -6.02 dB at the corner, the low branch passes
    # well below it, the high branch passes well above it.
    corner = min(BAND_CENTERS, key=lambda f: abs(f - 10120.0))
    points = {"100hz": 100.0, "1khz": 1000.0,
              "corner": corner, "16khz": 16000.0}
    table = {}
    for label, f in points.items():
        table[label] = {"freq_hz": round(f, 2), "low_db": gain("low", f),
                        "high_db": gain("high", f), "sum_db": gain("sum", f)}

    checks = {
        "sum_flat": bool(sel) and dev <= XOVER_MAX_DEVIATION_DB,
        "low_passes_1khz": abs(table["1khz"]["low_db"]) <= 1.0,
        "low_rejects_16khz": table["16khz"]["low_db"] <= -20.0,
        "high_passes_16khz": abs(table["16khz"]["high_db"]) <= 1.0,
        "high_rejects_1khz": table["1khz"]["high_db"] <= -20.0,
        "bands_minus6_at_corner":
            abs(table["corner"]["low_db"] + 6.02) <= 2.0
            and abs(table["corner"]["high_db"] + 6.02) <= 2.0,
    }
    result = {
        "mode": "crossover",
        "node": args.node,
        "corner_hz": 10120.0,
        "branches": table,
        "bands": rows,
        "max_deviation_db": dev,
        "checks": checks,
        "pass": all(checks.values()),
        "notes": ("LR4 at 10.12 kHz built from bq_lowpass/bq_highpass "
                  "(Q=0.7071) and summed with the builtin mixer; a flat sum "
                  "plus -6 dB branches is the sensible-crossover check"),
    }
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("eq", "crossover"), required=True)
    ap.add_argument("--node", default=os.environ.get("OXP3_AUDIO_TEST_NODE", ""))
    ap.add_argument("--work", default=os.environ.get("OXP3_AUDIO_TEST_WORK", ""))
    ap.add_argument("--json", help="write the full result as JSON here")
    ap.add_argument("--skip-limiter", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="short sweep (1.5 s) and no hot-tone checks")
    args = ap.parse_args()
    if args.quick:
        global SWEEP_SEC, SWEEP_LEAD, SWEEP_TAIL
        SWEEP_SEC, SWEEP_LEAD, SWEEP_TAIL = 1.5, 0.6, 0.3
        args.skip_limiter = True
    if not args.node:
        raise SystemExit("missing --node")
    if not args.work:
        raise SystemExit("missing --work")
    os.makedirs(args.work, exist_ok=True)

    if args.mode == "eq":
        result = eq_mode(args)
    else:
        result = crossover_mode(args)

    print(json.dumps(result, indent=2, sort_keys=True))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
