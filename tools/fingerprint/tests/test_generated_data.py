"""The checked-in fixtures/vectors must be current, small, and self-consistent."""

import json
import unittest

import support
import generate_fixtures
from fpbf63112 import protocol as P


class TestGeneratedDataCurrent(unittest.TestCase):
    def test_artifacts_match_the_generator(self):
        artifacts = generate_fixtures.build_artifacts()
        self.assertGreaterEqual(len(artifacts), 15)
        for path, expected in artifacts.items():
            with self.subTest(path=str(path)):
                self.assertTrue(path.exists(), f"missing generated file {path}")
                actual = path.read_text(encoding="utf-8")
                self.assertEqual(actual, expected,
                                 f"{path} is stale: re-run "
                                 f"tools/fingerprint/tests/generate_fixtures.py")

    def test_vectors_json_matches_vectors_h(self):
        vectors = json.loads((support.HERE / "vectors.json").read_text())
        header = (support.HERE / "vectors.h").read_text()
        self.assertEqual(len(vectors), 10)
        for vec in vectors:
            self.assertIn(vec["frame_hex"], header)
            self.assertIn(f'"{vec["name"]}"', header)
            self.assertIn(f"0x{vec['crc']:08x}u", header)


class TestFixtureHygiene(unittest.TestCase):
    def test_every_fixture_is_small_and_named_after_its_file(self):
        for path in support.fixture_paths():
            fixture = support.load_fixture(path.stem)
            with self.subTest(fixture=path.name):
                self.assertEqual(fixture["name"], path.stem)
                self.assertEqual(fixture["schema"], 1)
                self.assertIn(fixture["kind"], ("command", "image"))
                self.assertLessEqual(path.stat().st_size, 4096,
                                     "fixtures must stay small")
                self.assertTrue(fixture["description"])
                self.assertTrue(fixture["expect"])

    def test_requests_build_and_every_read_scans_cleanly_or_fails_loudly(self):
        for path in support.fixture_paths():
            fixture = support.load_fixture(path.stem)
            if fixture["kind"] == "command":
                req = fixture["request"]
                opcode = int(P.Command[req["op"]])
                frame = P.build_frame(opcode, req.get("phase", 0),
                                      req.get("arg", 0),
                                      bytes.fromhex(req.get("data_hex", "")))
                self.assertEqual(P.parse_frame(frame).opcode, opcode)
            for read in fixture["reads"]:
                # every byte string in a fixture must be valid hex, and the
                # scanner must never raise on it
                raw = bytes.fromhex(read["hex"])
                frames, errors = P.scan_frames(raw)
                for frame in frames:
                    self.assertEqual(P.parse_frame(frame.raw), frame)
                for err in errors:
                    self.assertIsInstance(err.error, P.FrameError)

    def test_no_vendor_or_binary_artifacts(self):
        for path in support.fixture_paths():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("MZ", text[:8])
            self.assertLessEqual(len(text), 4096)


class TestVectorConsistency(unittest.TestCase):
    def test_vectors_round_trip(self):
        vectors = json.loads((support.HERE / "vectors.json").read_text())
        for vec in vectors:
            raw = bytes.fromhex(vec["frame_hex"])
            parsed = P.parse_frame(raw, require_padding_zero=True)
            self.assertEqual(parsed.opcode, vec["opcode"], vec["name"])
            self.assertEqual(parsed.phase, vec["phase"], vec["name"])
            self.assertEqual(parsed.status, vec["status"], vec["name"])
            self.assertEqual(parsed.arg, vec["arg"], vec["name"])
            self.assertEqual(parsed.data, bytes.fromhex(vec["data_hex"]))
            self.assertEqual(len(raw), vec["frame_len"])
            self.assertEqual(parsed.length, vec["length_field"])
            self.assertEqual(parsed.crc, vec["crc"])

    def test_vector_names_are_unique_and_c_identifiers(self):
        vectors = json.loads((support.HERE / "vectors.json").read_text())
        names = [v["name"] for v in vectors]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r"^[a-z][a-z0-9_]+$")


if __name__ == "__main__":
    unittest.main()
