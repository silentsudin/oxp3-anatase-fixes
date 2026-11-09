#!/usr/bin/env python3
"""Biquad response of the reproduced OneXPlayer 3 speaker chain (our own code).

The band table below is what tools/audio/apo_extract.py reads out of the vendor
GA18v3.reg preset "Settings\\2" for the KSNODETYPE_SPEAKER endpoint (two
cascaded 10-band blocks, 17 audible bands after dropping the 0 dB ones and the
plain "gain" stages).  Values are (freq_Hz, gain_dB, Q) exactly as found.

  ./eq_response.py                 # table of gain at 1/3-octave points
  ./eq_response.py --csv out.csv   # freq,gain_dB for plotting
  ./eq_response.py --conf          # emit the PipeWire filter.graph nodes

Pure stdlib: RBJ cookbook peaking biquads, magnitude from the transfer function.
"""
import argparse
import math
import sys

# --- vendor preset (speaker endpoint, active setting 2) -----------------------
# stage 1 (two identical copies at param offsets 19/69 -> one stereo chain)
STAGE1 = [
    (80.0, -3.0, 0.7),
    (400.0, 3.0, 0.9),
    (140.0, 6.0, 2.0),
    (10000.0, 3.0, 3.0),
    (4500.0, -2.0, 2.0),
    (6500.0, -4.0, 2.0),
    (7500.0, -4.0, 2.0),
    (140.0, 3.0, 1.5),
]
# stage 2 (offset 1435): all-cut block, narrow Q -> resonance control
STAGE2 = [
    (95.0, -3.0, 0.7),
    (400.0, -3.0, 0.9),
    (650.0, -5.0, 3.0),
    (4500.0, -7.0, 4.5),
    (7000.0, -6.0, 4.0),
    (700.0, -2.0, 3.0),
    (250.0, -3.0, 4.0),
    (13000.0, -4.0, 7.0),
    (15000.0, -3.0, 0.7),
]
# The vendor preset runs a limiter after the EQ (threshold 0.607 = -4.3 dBFS).
# A filter-chain cannot do dynamics, so the default chain replaces it with static
# headroom: -8.0 dB == -(the +7.74 dB peak of stage 1), so the chain never clips.
# Raise it towards 0 dB (or use the EasyEffects preset, which has a real limiter).
PREGain_DB = -8.0
# filter-chain has no plain gain node, so the headroom is a low shelf parked at
# Nyquist (20 kHz): flat -8 dB across the audio band, 0 dB only above ~21 kHz.
HEADROOM_FREQ = 20000.0
HEADROOM_Q = 0.7

# Stage 1 is the output EQ: the vendor blob stores it twice, bit-identical
# (param offsets 19 and 69) == left and right.  Stage 2 (offset 1435) is a single
# all-cut block; it is documented as the optional resonance-control stage and is
# NOT in the default chain (see docs/hw/audio-harman-apo.md).
BANDS = STAGE1
FS = 48000.0


def peaking(f, gain_db, q, fs=FS):
    """RBJ peaking EQ -> (b0,b1,b2,a0,a1,a2) normalised later."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f / fs
    alpha = math.sin(w0) / (2.0 * q)
    b0, b1, b2 = 1 + alpha * A, -2 * math.cos(w0), 1 - alpha * A
    a0, a1, a2 = 1 + alpha / A, -2 * math.cos(w0), 1 - alpha / A
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def low_shelf(f, gain_db, q, fs=FS):
    """RBJ low shelf; used only for the static headroom stage (see PREGain)."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    sA = 2.0 * math.sqrt(A) * alpha
    b0 = A * ((A + 1) - (A - 1) * cw + sA)
    b1 = 2 * A * ((A - 1) - (A + 1) * cw)
    b2 = A * ((A + 1) - (A - 1) * cw - sA)
    a0 = (A + 1) + (A - 1) * cw + sA
    a1 = -2 * ((A - 1) + (A + 1) * cw)
    a2 = (A + 1) + (A - 1) * cw - sA
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def mag_db(coeffs, f, fs=FS):
    b0, b1, b2, a1, a2 = coeffs
    w = 2.0 * math.pi * f / fs
    z1 = complex(math.cos(-w), math.sin(-w))
    z2 = z1 * z1
    num = b0 + b1 * z1 + b2 * z2
    den = 1.0 + a1 * z1 + a2 * z2
    return 20.0 * math.log10(abs(num) / abs(den))


def curve(freqs, bands=BANDS, pre=PREGain_DB, fs=FS):
    """Full chain response: headroom shelf + peaking bands (+ any manual pre)."""
    cs = []
    if pre:
        cs.append(low_shelf(HEADROOM_FREQ, pre, HEADROOM_Q, fs=fs))
    cs += [peaking(*b, fs=fs) for b in bands]
    return [sum(mag_db(c, f, fs) for c in cs) for f in freqs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--conf", action="store_true")
    ap.add_argument("--no-pregain", action="store_true")
    ap.add_argument("--stage2", action="store_true",
                    help="add the optional all-cut resonance-control block")
    ap.add_argument("--bands", help="freq:gain:q,freq:gain:q,... override")
    args = ap.parse_args()
    pre = 0.0 if args.no_pregain else PREGain_DB
    bands = BANDS + STAGE2 if args.stage2 else BANDS
    if args.bands:
        bands = [tuple(float(x) for x in t.split(":")) for t in args.bands.split(",")]

    if args.stage2:
        pre = -5.0   # combined peak of stage 1 + 2 is +4.82 dB
    if args.conf:
        nodes = []
        names = []
        if pre:
            nodes.append('            { type = builtin name = headroom label = bq_lowshelf '
                         'control = { "Freq" = %g "Q" = %g "Gain" = %g } }'
                         % (HEADROOM_FREQ, HEADROOM_Q, pre))
            names.append("headroom")
        for i, (f, g, q) in enumerate(bands, 1):
            n = "band%02d" % i
            nodes.append('            { type = builtin name = %s label = bq_peaking '
                         'control = { "Freq" = %g "Q" = %g "Gain" = %g } }' % (n, f, q, g))
            names.append(n)
        print("filter.graph = {")
        print("    nodes = [")
        print("\n".join(nodes))
        print("    ]")
        print("    links = [")
        for a, b in zip(names, names[1:]):
            print('        { output = "%s:Out" input = "%s:In" }' % (a, b))
        print("    ]")
        print("}")
        return

    # 1/3-octave report
    import math as _m
    freqs = [20.0 * (2.0 ** (i / 3.0)) for i in range(0, 31)]
    vals = curve(freqs, bands=bands, pre=pre)
    peak, trough = max(vals), min(vals)
    if args.csv:
        with open(args.csv, "w") as fh:
            fh.write("freq_hz,gain_db\n")
            f = 10.0
            while f <= 24000.0:
                fh.write("%.4f,%.4f\n" % (f, curve([f], bands=bands, pre=pre)[0]))
                f *= 1.01
    print("chain: %d peaking bands, pre-gain %+.1f dB" % (len(bands), pre))
    print("max %+.1f dB   min %+.1f dB   (sanity: no runaway boost above ~+10 dB)"
          % (peak, trough))
    print("\n%8s %8s" % ("Hz", "dB"))
    for f, v in zip(freqs, vals):
        print("%8.0f %+8.1f" % (f, v))


if __name__ == "__main__":
    main()
