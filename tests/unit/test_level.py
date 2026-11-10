"""Unit tests for tools/audio/level-report.py and the EQ headroom profiles.

Everything is a pure function over synthetic `amixer`/`pactl`/conf text, so no
card, no PipeWire session and no audio is needed.  The cases pin exactly what
`aud.level` depends on: the ALSA dB conversion, which controls count as "at
maximum", the EQ headroom parsed from the installed drop-in, the net gain versus
the old chain, and the clipping margin of each profile.
"""

from __future__ import annotations

import unittest

import support

level = support.load("audio_level", "audio/level-report.py")
eq = support.load("audio_eq", "audio/eq_response.py")

SPEAKER_82 = """Simple mixer control 'Speaker',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 87
  Mono:
  Front Left: Playback 82 [94%] [-3.75dB] [on]
  Front Right: Playback 82 [94%] [-3.75dB] [on]
"""

SPEAKER_87 = """Simple mixer control 'Speaker',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 87
  Mono:
  Front Left: Playback 87 [100%] [0.00dB] [on]
  Front Right: Playback 87 [100%] [0.00dB] [on]
"""

SPEAKER_NO_DB = """Simple mixer control 'Speaker',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 87
  Mono:
  Front Left: Playback 82 [94%] [on]
  Front Right: Playback 82 [94%] [on]
"""

MONO_87 = """Simple mixer control 'Mono',0
  Capabilities: pvolume pswitch
  Playback channels: Mono
  Limits: Playback 0 - 87
  Mono: Playback 87 [100%] [0.00dB] [on]
"""

LOUD_CONF = """
filter.graph = {
    nodes = [
        { type = builtin name = headroom label = bq_lowshelf
          control = { "Freq" = 20000 "Q" = 0.7 "Gain" = 2.84 } }
        { type = builtin name = b1  label = bq_peaking
          control = { "Freq" = 80 "Q" = 0.7 "Gain" = -3 } }
    ]
}
"""

SAFE_CONF = LOUD_CONF.replace('"Gain" = 2.84', '"Gain" = -8')
NO_CONF = LOUD_CONF.replace("name = headroom", "name = not_headroom")


def control(name="Speaker", value=87, maximum=87, db=0.0, at_max=True):
    return {"name": name, "max": maximum, "values": [value] * 2, "value": value,
            "balanced": True, "percent": 100 if at_max else 94, "db": db,
            "at_max": at_max}


class AlsaMathTest(unittest.TestCase):
    def test_db_from_steps(self):
        self.assertEqual(level.alsa_db(87, 87), 0.0)
        self.assertEqual(level.alsa_db(82, 87), -3.75)
        self.assertEqual(level.alsa_db(0, 87), -65.25)

    def test_db_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            level.alsa_db(88, 87)
        with self.assertRaises(ValueError):
            level.alsa_db(-1, 87)
        with self.assertRaises(ValueError):
            level.alsa_db(0, 0)

    def test_parse_at_82(self):
        got = level.parse_amixer_sget(SPEAKER_82)
        self.assertEqual(got["name"], "Speaker")
        self.assertEqual(got["max"], 87)
        self.assertEqual(got["value"], 82)
        self.assertEqual(got["percent"], 94)
        self.assertEqual(got["db"], -3.75)
        self.assertFalse(got["at_max"])

    def test_parse_at_max(self):
        got = level.parse_amixer_sget(SPEAKER_87)
        self.assertEqual(got["values"], [87, 87])
        self.assertTrue(got["at_max"])
        self.assertEqual(got["db"], 0.0)

    def test_parse_without_db_annotation_falls_back_to_steps(self):
        got = level.parse_amixer_sget(SPEAKER_NO_DB)
        self.assertEqual(got["db"], -3.75)

    def test_parse_mono_control(self):
        got = level.parse_amixer_sget(MONO_87)
        self.assertEqual(got["name"], "Mono")
        self.assertEqual(got["values"], [87])
        self.assertTrue(got["at_max"])

    def test_parse_rejects_non_control_text(self):
        self.assertIsNone(level.parse_amixer_sget("amixer: Unable to find simple control"))


class SinkParsersTest(unittest.TestCase):
    def test_volume(self):
        got = level.parse_sink_volume(
            "Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB\n")
        self.assertEqual(got, {"percent": 100, "db": 0.0})

    def test_volume_absent(self):
        self.assertIsNone(level.parse_sink_volume("Connection failure"))

    def test_mute(self):
        self.assertIs(level.parse_sink_mute("Mute: no\n"), False)
        self.assertIs(level.parse_sink_mute("Mute: yes\n"), True)
        self.assertIsNone(level.parse_sink_mute("n/a"))


class HeadroomParsersTest(unittest.TestCase):
    def test_shipped_template_is_loud(self):
        text = (support.REPO / "tools" / "audio" / "oxp3-speaker-eq.conf").read_text()
        self.assertEqual(level.parse_eq_headroom(text), eq.PROFILES["loud"])

    def test_safe_node(self):
        self.assertEqual(level.parse_eq_headroom(SAFE_CONF), -8.0)

    def test_missing_node(self):
        self.assertIsNone(level.parse_eq_headroom(NO_CONF))
        self.assertIsNone(level.parse_eq_headroom(""))

    def test_profile_names(self):
        self.assertEqual(level.profile_for(eq.PROFILES["loud"]), "loud")
        self.assertEqual(level.profile_for(-8.0), "safe")
        self.assertEqual(level.profile_for(-6.0), "custom")
        self.assertEqual(level.profile_for(None), "off")

    def test_marker(self):
        text = "# oxp3-profile: safe (headroom -8 dB)\n" + SAFE_CONF
        self.assertEqual(level.parse_profile_marker(text), "safe")
        self.assertIsNone(level.parse_profile_marker(SAFE_CONF))


class ProfileModelTest(unittest.TestCase):
    def test_profiles_are_the_documented_numbers(self):
        self.assertEqual(eq.PROFILES, {"loud": 2.84, "safe": -8.0})
        self.assertEqual(eq.DEFAULT_PROFILE, "loud")
        self.assertEqual(eq.PREGain_DB, 2.84)
        self.assertEqual(eq.headroom_db("safe"), -8.0)

    def test_loud_peak_and_clipping(self):
        peak, trough, hz = eq.curve_extremes(pre=eq.PROFILES["loud"])
        # +7.74 dB stage-1 peak plus the folded +2.84 dB staging.
        self.assertAlmostEqual(peak, 10.58, places=1)
        self.assertLess(trough, -3.0)
        self.assertTrue(130.0 < hz < 155.0)

    def test_safe_profile_cannot_clip(self):
        peak, _trough, _hz = eq.curve_extremes(pre=eq.PROFILES["safe"])
        self.assertLess(peak, 0.0)          # a 0 dBFS input stays below full scale
        self.assertGreater(peak, -0.5)

    def test_unknown_profile_raises(self):
        with self.assertRaises(KeyError):
            eq.headroom_db("nope")


class ReportTest(unittest.TestCase):
    def test_loud_at_max(self):
        rep = level.build_report(0, [control()], {"percent": 100, "muted": False},
                                 LOUD_CONF)
        self.assertEqual(rep["control"], "Speaker")
        self.assertTrue(rep["at_max"])
        self.assertEqual(rep["alsa_db"], 0.0)
        self.assertEqual(rep["eq"]["profile"], "loud")
        self.assertEqual(rep["eq"]["headroom_db"], 2.84)
        self.assertEqual(rep["net_db"], 2.84)
        self.assertEqual(rep["net_vs_old_db"], 14.59)
        self.assertLess(rep["eq"]["clip_margin_db"], 0.0)
        self.assertFalse(rep["eq"]["installed"] is False)

    def test_safe_at_82(self):
        rep = level.build_report(0, [control(value=82, db=-3.75, at_max=False)],
                                 {"percent": 100, "muted": False}, SAFE_CONF)
        self.assertFalse(rep["at_max"])
        self.assertEqual(rep["eq"]["profile"], "safe")
        self.assertEqual(rep["net_db"], -11.75)     # the old chain, exactly
        self.assertEqual(rep["net_vs_old_db"], 0.0)
        self.assertGreater(rep["eq"]["clip_margin_db"], 0.0)

    def test_two_controls_are_summed_and_all_must_be_max(self):
        a = control("Front", maximum=87, value=87)
        b = control("Mono", maximum=87, value=82, db=-3.75, at_max=False)
        rep = level.build_report(0, [a, b], {}, LOUD_CONF)
        self.assertFalse(rep["at_max"])
        self.assertEqual(rep["alsa_db"], -3.75)
        self.assertEqual(rep["net_db"], -0.91)

    def test_no_eq_installed(self):
        rep = level.build_report(0, [control(value=87)], {}, None)
        self.assertEqual(rep["eq"]["profile"], "off")
        self.assertFalse(rep["eq"]["installed"])
        self.assertIsNone(rep["eq"]["headroom_db"])
        self.assertEqual(rep["net_db"], 0.0)

    def test_old_default_constant(self):
        self.assertEqual(level.OLD_DEFAULT_NET_DB, -11.75)


if __name__ == "__main__":
    unittest.main()
