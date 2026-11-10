"""Unit tests for the isolated-graph measurement maths.

The measurements in tools/audio/measure-in-isolation.py are pure Python
(radix-2 FFT, 1/3-octave energy averaging, the analytic model from
eq_response.py and the offline vendor-limiter model).  Those are exactly the
parts that can be wrong while still producing plausible numbers, so they are
tested here without PipeWire, audio hardware, or the network.
"""

from __future__ import annotations

import math
import random
import unittest

import support

eq = support.load("eq_measure", "audio/measure-in-isolation.py")


def naive_dft(values):
    n = len(values)
    out = []
    for k in range(n):
        acc = 0.0 + 0.0j
        for i, x in enumerate(values):
            acc += x * complex(math.cos(-2.0 * math.pi * k * i / n),
                               math.sin(-2.0 * math.pi * k * i / n))
        out.append(abs(acc))
    return out


class FftTest(unittest.TestCase):
    def test_fft_matches_naive_dft(self):
        rng = random.Random(7)
        n = 64
        vals = [rng.uniform(-1, 1) for _ in range(n)]
        got = [abs(x) for x in eq.fft(vals)[:n // 2 + 1]]
        want = naive_dft(vals)[:n // 2 + 1]
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, places=9)

    def test_parseval(self):
        rng = random.Random(11)
        n = 128
        vals = [rng.uniform(-1, 1) for _ in range(n)]
        spec = eq.fft(vals)
        time_energy = sum(x * x for x in vals)
        freq_energy = sum(abs(x) ** 2 for x in spec) / n
        self.assertAlmostEqual(time_energy, freq_energy, places=9)

    def test_magnitude_spectrum_zero_pads(self):
        spec = eq.magnitude_spectrum([1.0, -1.0], n=64)
        self.assertEqual(len(spec), 33)
        # A two-sample signal has the same |X| at DC as at Nyquist here.
        self.assertAlmostEqual(spec[0], 0.0, places=9)

    def test_fft_rejects_non_power_of_two(self):
        with self.assertRaises(ValueError):
            eq.fft([1.0, 2.0, 3.0])


class DbfsTest(unittest.TestCase):
    def test_full_scale_is_zero(self):
        self.assertAlmostEqual(eq.dbfs(1.0), 0.0, places=9)

    def test_half_scale(self):
        self.assertAlmostEqual(eq.dbfs(0.5), -6.0205999, places=5)

    def test_floor_is_finite(self):
        self.assertGreater(eq.dbfs(0.0), -200.0)
        self.assertLess(eq.dbfs(0.0), -150.0)


class WavTest(unittest.TestCase):
    def test_round_trip(self):
        tree = support.TmpTree()
        try:
            path = str(tree.path("x.wav"))
            samples = [0.0, 0.25, -0.5, 0.999, -1.0]
            eq.write_wav(path, samples, rate=48000)
            got, rate = eq.read_mono(path)
            self.assertEqual(rate, 48000)
            self.assertEqual(len(got), len(samples))
            for g, w in zip(got, samples):
                self.assertAlmostEqual(g, w, places=3)
        finally:
            tree.cleanup()

    def test_write_clips(self):
        tree = support.TmpTree()
        try:
            path = str(tree.path("clip.wav"))
            eq.write_wav(path, [2.0, -2.0])
            got, _ = eq.read_mono(path)
            self.assertLessEqual(max(abs(x) for x in got), 1.0)
        finally:
            tree.cleanup()


class SweepTest(unittest.TestCase):
    def test_length_and_level(self):
        s = eq.log_sweep()
        want = (int(eq.SWEEP_LEAD * eq.RATE) + int(eq.SWEEP_SEC * eq.RATE)
                + int(eq.SWEEP_TAIL * eq.RATE))
        self.assertEqual(len(s), want)
        self.assertLessEqual(max(abs(x) for x in s), eq.SWEEP_AMP + 1e-9)
        # Lead-in and tail are digital silence so no convolution wrap-around.
        self.assertEqual(s[0], 0.0)
        self.assertEqual(s[-1], 0.0)

    def test_hot_tone_hits_full_scale(self):
        s = eq.hot_tone(1000.0)
        self.assertAlmostEqual(max(abs(x) for x in s), 1.0, places=6)


class BandTest(unittest.TestCase):
    def test_band_bins_are_inside_the_band(self):
        n, rate = 4096, 48000
        df = rate / float(n)
        for centre in (50.0, 1000.0, 16000.0):
            k0, k1 = eq.band_bins(centre, rate, n)
            self.assertGreaterEqual(k0, 1)
            self.assertLessEqual(k1, n // 2)
            self.assertLess(k0, k1)
            self.assertGreaterEqual(k0 * df, centre / 2.0 ** (1 / 6.0) - df)
            self.assertLessEqual((k1 - 1) * df, centre * 2.0 ** (1 / 6.0) + df)

    def test_flat_spectrum_is_zero_db(self):
        mag = [1.0] * 32
        self.assertAlmostEqual(eq.band_level_db(mag, 1000.0, 48000, 64), 0.0,
                               places=9)

    def test_scaled_spectrum(self):
        gain = 6.0
        mag = [10.0 ** (gain / 20.0)] * 32
        self.assertAlmostEqual(eq.band_level_db(mag, 1000.0, 48000, 64), gain,
                               places=9)

    def test_compare_bands_recovers_zero_error(self):
        """A spectrum equal to the model must compare to 0.00 dB."""
        n, rate = 4096, 48000
        ref = [1.0] * (n // 2 + 1)
        meas = list(ref)
        df = rate / float(n)
        for centre in eq.BAND_CENTERS:
            k0, k1 = eq.band_bins(centre, rate, n)
            for k in range(k0, k1):
                meas[k] = 10.0 ** (eq.eq_response.curve([k * df])[0] / 20.0)
        rows = eq.compare_bands(meas, ref, rate=rate, n=n)
        self.assertGreaterEqual(len(rows), 20)
        for row in rows:
            self.assertAlmostEqual(row["diff_db"], 0.0, places=9)

    def test_compare_bands_reports_a_known_error(self):
        n, rate = 4096, 48000
        ref = [1.0] * (n // 2 + 1)
        meas = list(ref)
        df = rate / float(n)
        centres = (1000.0,)
        k0, k1 = eq.band_bins(1000.0, rate, n)
        # The model plus a manufactured +2 dB error in the 1 kHz band.
        for k in range(k0, k1):
            meas[k] = 10.0 ** ((eq.eq_response.curve([k * df])[0] + 2.0) / 20.0)
        # Other bands: exactly the model, so they compare at 0 dB.  Skip any
        # band whose bins overlap the 1 kHz band (1007.9 Hz does).
        for centre in eq.BAND_CENTERS:
            a, b = eq.band_bins(centre, rate, n)
            if a < k1 and b > k0:
                continue
            for k in range(a, b):
                meas[k] = 10.0 ** (eq.eq_response.curve([k * df])[0] / 20.0)
        rows = eq.compare_bands(meas, ref, centers=centres, rate=rate, n=n)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["diff_db"], 2.0, places=6)

    def test_model_band_is_an_energy_average(self):
        # Independent recomputation over the same bins must agree exactly.
        centre, n, rate = 1000.0, 4096, 48000
        k0, k1 = eq.band_bins(centre, rate, n)
        df = rate / float(n)
        acc = sum(10.0 ** (eq.eq_response.curve([k * df])[0] / 10.0)
                  for k in range(k0, k1))
        self.assertAlmostEqual(eq.model_band_db(centre, rate=rate, n=n),
                               10.0 * math.log10(acc / (k1 - k0)), places=9)

    def test_model_flat_shelf_reaches_zero_at_nyquist(self):
        # The headroom is a low shelf at 20 kHz: flat in the audio band and
        # approaching 0 dB at Nyquist.  The safe (-8 dB) profile is the old
        # chain; the loud default is +2.84 dB because it folds in the +6.84 dB
        # sink staging that used to make Gamemode's 100 % quieter, so it is
        # 10.84 dB above safe and clips at full scale in the bass.
        self.assertAlmostEqual(eq.eq_response.curve([24000.0])[0], 0.0,
                               places=6)
        safe = eq.eq_response.curve([1000.0], pre=eq.eq_response.PROFILES["safe"])[0]
        loud = eq.eq_response.curve([1000.0], pre=eq.eq_response.PROFILES["loud"])[0]
        self.assertLess(safe, -7.0)
        self.assertAlmostEqual(loud - safe, 10.84, places=3)
        self.assertGreater(loud, 0.0)

    def test_summarise(self):
        rows = [{"freq_hz": f, "diff_db": d, "measured_db": 0.0, "model_db": 0.0}
                for f, d in ((50.0, 5.0), (200.0, 0.2), (1000.0, -0.4),
                             (4000.0, 0.0), (20000.0, 3.0))]
        got = eq.summarise(rows, 100.0, 10000.0)
        self.assertEqual(got["bands"], 3)
        self.assertAlmostEqual(got["max_abs_db"], 0.4, places=9)
        self.assertAlmostEqual(got["mean_abs_db"], 0.2, places=9)


class LimiterModelTest(unittest.TestCase):
    @staticmethod
    def tone(seconds, amplitude=1.0, freq=1000.0):
        n = int(seconds * eq.RATE)
        return [amplitude * math.sin(2.0 * math.pi * freq * i / eq.RATE)
                for i in range(n)]

    def test_below_threshold_is_unity(self):
        s = self.tone(1.0, amplitude=0.1)
        out, _ = eq.limiter_model(s)
        ratio = (sum(x * x for x in s) / sum(x * x for x in out)) ** 0.5
        self.assertAlmostEqual(ratio, 1.0, places=2)

    def test_hot_tone_is_attenuated(self):
        s = self.tone(2.0, amplitude=1.0)
        out, _ = eq.limiter_model(s)
        rms_in = (sum(x * x for x in s) / len(s)) ** 0.5
        tail = out[-int(0.3 * eq.RATE):]
        rms_out = (sum(x * x for x in tail) / len(tail)) ** 0.5
        attenuation = 20.0 * math.log10(rms_in / rms_out)
        self.assertGreater(attenuation, 1.0)
        self.assertLess(attenuation, 4.0)

    def test_gain_recovers_after_the_tone(self):
        s = self.tone(2.0) + [0.0] * int(1.5 * eq.RATE)
        _, gains = eq.limiter_model(s)
        end = gains[-int(0.2 * eq.RATE):]
        self.assertLess(max(abs(g) for g in end), 0.5)

    def test_slow_attack_is_respected(self):
        s = self.tone(0.1)   # shorter than the 200 ms attack
        out, _ = eq.limiter_model(s)
        rms_in = (sum(x * x for x in s) / len(s)) ** 0.5
        rms_out = (sum(x * x for x in out) / len(out)) ** 0.5
        self.assertGreater(rms_out / rms_in, 0.9)


if __name__ == "__main__":
    unittest.main()
