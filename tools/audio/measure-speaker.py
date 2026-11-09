#!/usr/bin/env python3
"""Speaker tuning measurement helper for the OneXPlayer 3 (our own tooling).

Two jobs:

  1. generate a stepped multi-tone test signal (default: 1/3-octave tones,
     125 Hz .. 16 kHz, 0.4 s tone + 0.25 s silence each, -12 dBFS)
  2. play it into a sink, capture the result, and report the measured gain per
     band next to the model from tools/audio/eq_response.py

Examples

  # pure offline: just write the test signal
  ./measure-speaker.py --gen /tmp/tones.wav

  # measure the reproduced EQ without touching the speakers: the filter chain
  # installed by this repo exposes the sink "oxp3_speaker_eq.sink", so route the
  # tones there and record the hardware monitor (or a mic)
  ./measure-speaker.py --run --sink oxp3_speaker_eq.sink --source alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink.monitor

  # acoustic measurement of the real speakers: play through the EQ sink, record
  # with a USB/phone mic (arecord or pw-record both work), then compare
  ./measure-speaker.py --analyze /tmp/oxp3-tones-rec.wav

`--analyze` needs the recorded file only; the reference is regenerated with the
same parameters, so keep the defaults.

Stdlib only (wave, struct, math, subprocess).
"""
import argparse
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_response  # noqa: E402  (same-directory helper)

RATE = 48000
TONE_SEC = 0.40
GAP_SEC = 0.25
LEVEL = 0.25            # -12 dBFS per tone


def third_octave_freqs(lo=125.0, hi=16000.0):
    f = lo
    out = []
    while f <= hi * 1.001:
        out.append(round(f, 1))
        f *= 2.0 ** (1.0 / 3.0)
    return out


def gen(path, freqs, rate=RATE, tone=TONE_SEC, gap=GAP_SEC, level=LEVEL):
    n_tone = int(tone * rate)
    n_gap = int(gap * rate)
    frames = bytearray()
    for f in freqs:
        for i in range(n_tone):
            # 2 ms raised-cosine edges so the tone does not click
            env = 1.0
            edge = int(0.002 * rate)
            if i < edge:
                env = 0.5 - 0.5 * math.cos(math.pi * i / edge)
            elif i > n_tone - edge:
                env = 0.5 - 0.5 * math.cos(math.pi * (n_tone - i) / edge)
            v = int(level * env * 32767.0 * math.sin(2 * math.pi * f * i / rate))
            frames += struct.pack("<hh", v, v)
        frames += b"\x00\x00\x00\x00" * n_gap
    w = wave.open(path, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(bytes(frames))
    w.close()
    return path


def read_mono(path):
    w = wave.open(path, "rb")
    rate = w.getframerate()
    ch = w.getnchannels()
    data = w.readframes(w.getnframes())
    w.close()
    s = struct.unpack("<%dh" % (len(data) // 2), data)
    if ch > 1:
        s = s[0::ch]
    return list(s), rate


def segments(samples, rate, win_ms=20):
    """Return [(start,end)] of contiguous regions above the noise floor."""
    win = max(1, int(rate * win_ms / 1000))
    env = []
    for i in range(0, len(samples) - win, win):
        acc = 0.0
        for x in samples[i:i + win]:
            acc += x * x
        env.append(math.sqrt(acc / win))
    if not env:
        return []
    peak = max(env)
    thr = max(peak * 0.08, 3.0)
    segs, start = [], None
    for idx, e in enumerate(env):
        if e > thr and start is None:
            start = idx
        elif e <= thr and start is not None:
            segs.append((start * win, idx * win))
            start = None
    if start is not None:
        segs.append((start * win, len(env) * win))
    # merge gaps shorter than a tone, drop very short fragments
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < rate * 0.1:
            merged[-1] = (merged[-1][0], s[1])
        elif s[1] - s[0] > rate * 0.1:
            merged.append(s)
    return merged


def rms(seq):
    if not seq:
        return 0.0
    return math.sqrt(sum(float(x) * x for x in seq) / len(seq))


def goertzel_db(seq, rate, freq):
    """Relative level of `freq` in `seq`, for tone-order verification."""
    n = len(seq)
    w = 2.0 * math.pi * freq / rate
    coeff = 2.0 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for x in seq:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return 10.0 * math.log10(max(power, 1e-12) / max(n * n, 1))


def analyze(rec_path, freqs, ref_path, verbose=True):
    rec, rate = read_mono(rec_path)
    ref, rrate = read_mono(ref_path)
    if rate != rrate:
        print("warning: recording is %d Hz, reference %d Hz" % (rate, rrate))
    segs = segments(rec, rate)
    ref_segs = segments(ref, rate)
    rows = []
    for i, f in enumerate(freqs):
        if i >= len(segs) or i >= len(ref_segs):
            break
        s, e = segs[i]
        mid = ref_segs[i]
        # middle 60 % of each segment avoids edges and any residual offset
        q = int((e - s) * 0.2)
        tone = rec[s + q:e - q]
        r_q = int((mid[1] - mid[0]) * 0.2)
        ref_tone = ref[mid[0] + r_q:mid[1] - r_q]
        g = 20.0 * math.log10(max(rms(tone), 1e-9) / max(rms(ref_tone), 1e-9))
        dom = goertzel_db(tone, rate, f)
        rows.append((f, g, dom))
    model = eq_response.curve([f for f, _, _ in rows])
    if verbose:
        print("%9s %10s %10s %8s" % ("Hz", "measured", "model", "diff"))
        for (f, g, _), m in zip(rows, model):
            print("%9.0f %+9.1f %+9.1f %+8.1f" % (f, g, m, g - m))
        if len(rows) > 1:
            d = [g - m for (_, g, _), m in zip(rows, model)]
            print("\nmean abs error %.2f dB, max %.2f dB over %d bands"
                  % (sum(abs(x) for x in d) / len(d), max(abs(x) for x in d), len(d)))
    return rows, model


def run(sink, source, freqs, keep=False):
    tmp = tempfile.mkdtemp(prefix="oxp3-eq-")
    ref = gen(os.path.join(tmp, "tones.wav"), freqs)
    rec = os.path.join(tmp, "rec.wav")
    env = dict(os.environ)
    play = subprocess.Popen(["pw-play", "--target", sink, ref],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    recp = subprocess.Popen(["pw-record", "--target", source, "--rate", str(RATE),
                             "--channels", "2", rec],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    play.wait()
    time.sleep(0.3)
    recp.terminate()
    recp.wait()
    if not os.path.exists(rec) or os.path.getsize(rec) < 1000:
        print("no recording captured - is %s a valid monitor/source?" % source)
        return None
    rows, model = analyze(rec, freqs, ref)
    if keep:
        print("kept %s and %s" % (ref, rec))
    return rows, model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", metavar="WAV")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--sink", default="oxp3_speaker_eq.sink")
    ap.add_argument("--source", default="")
    ap.add_argument("--analyze", metavar="WAV")
    ap.add_argument("--ref", metavar="WAV", help="reference tone file for --analyze")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    freqs = third_octave_freqs()
    if args.gen:
        gen(args.gen, freqs)
        print("wrote %s: %d tones, %d Hz, %d ch"
              % (args.gen, len(freqs), RATE, 2))
        return
    if args.analyze:
        ref = args.ref or gen(os.path.join(tempfile.mkdtemp(prefix="oxp3-ref-"),
                                           "tones.wav"), freqs)
        analyze(args.analyze, freqs, ref)
        return
    if args.run:
        if not args.source:
            print("--run needs --source (a monitor or a microphone source name;\n"
                  "  pw-record --list-targets / wpctl status shows them)")
            return 2
        run(args.sink, args.source, freqs, keep=args.keep)
        return
    ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
