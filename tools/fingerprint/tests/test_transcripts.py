"""Replay the synthetic device transcripts through the library's state machines."""

import unittest

import support


class TestCommandTranscripts(unittest.TestCase):
    def test_every_command_fixture_replays_as_expected(self):
        seen = 0
        for path in support.fixture_paths():
            fixture = support.load_fixture(path.stem)
            if fixture["kind"] != "command":
                continue
            seen += 1
            with self.subTest(fixture=fixture["name"]):
                result = support.play(fixture)
                expect = fixture["expect"]
                self.assertEqual(result["outcome"], expect["outcome"])
                self.assertEqual(result["attempts"], expect["attempts"])
                self.assertEqual(result["payload_hex"], expect["payload_hex"])
        self.assertGreaterEqual(seen, 8)

    def test_specific_outcomes_are_distinguished(self):
        # Guards against a fixture set that accidentally exercises one path.
        outcomes = {support.play(support.load_fixture(p.stem))["outcome"]
                    for p in support.fixture_paths()
                    if support.load_fixture(p.stem)["kind"] == "command"}
        self.assertTrue({"success", "failed", "mismatch"} <= outcomes, outcomes)

    def test_bad_crc_reports_a_crc_error(self):
        fixture = support.load_fixture("cmd_bad_crc_then_ok")
        result = support.play(fixture)
        self.assertIn("CRC mismatch", result["errors"][0])

    def test_truncated_read_is_reported_as_truncation(self):
        fixture = support.load_fixture("cmd_truncated_then_ok")
        result = support.play(fixture)
        self.assertIn("have 25B", result["errors"][0])


class TestImageTranscripts(unittest.TestCase):
    def test_every_image_fixture_replays_as_expected(self):
        seen = 0
        for path in support.fixture_paths():
            fixture = support.load_fixture(path.stem)
            if fixture["kind"] != "image":
                continue
            seen += 1
            with self.subTest(fixture=fixture["name"]):
                result = support.play(fixture)
                expect = fixture["expect"]
                self.assertEqual(result["outcome"], expect["outcome"])
                if "received" in expect:
                    self.assertEqual(result["received"], expect["received"])
                if "total" in expect:
                    self.assertEqual(result["total"], expect["total"])
                if "sha256" in expect:
                    self.assertEqual(result["sha256"], expect["sha256"])
                if "sum8" in expect:
                    self.assertEqual(result["sum8"], expect["sum8"])
                if "error" in expect:
                    self.assertEqual(result["error"], expect["error"])
        self.assertGreaterEqual(seen, 5)

    def test_outcome_variety(self):
        outcomes = set()
        for path in support.fixture_paths():
            fixture = support.load_fixture(path.stem)
            if fixture["kind"] == "image":
                outcomes.add(support.play(fixture)["outcome"])
        self.assertTrue({"success", "incomplete", "bad-sum", "error"}
                        <= outcomes, outcomes)

    def test_fixture_image_matches_its_own_hex(self):
        fixture = support.load_fixture("img_ok_8x8")
        result = support.play(fixture)
        self.assertEqual(result["image_hex"], fixture["image_hex"])
        self.assertEqual(len(bytes.fromhex(fixture["image_hex"])),
                         fixture["expect"]["received"])


if __name__ == "__main__":
    unittest.main()
