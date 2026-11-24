# SPDX-License-Identifier: MIT
"""tools/fsr4-xmx/make-packs.py: index an FSR 4 XMX upscaler's kernel packs in data.bin.

Builds a fake upscaler DLL with Compression API pack headers and the SHA-256 strings the real one
verifies, plus the matching data.bin, and checks the index and the refusals.
"""

import hashlib
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support

TOOL = support.REPO / "tools" / "fsr4-xmx" / "make-packs.py"
MAGIC = bytes.fromhex("0a51e5c0")


def header(raw: int) -> bytes:
    # magic, algorithm/flags, unpacked size (u64 at +8), the rest of the 24-byte header
    return MAGIC + b"\x18\x00\x92\x05" + struct.pack("<Q", raw) + b"\x00\x00\x01\x00" + b"\x00" * 4


class MakePacksTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="oxp3-fsr4-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))
        self.raw = [b"A" * 5000, b"B" * 3000, b"C" * 700]
        body = b"MZ" + b"\x00" * 100
        for chunk in self.raw:
            body += hashlib.sha256(chunk).hexdigest().encode() + b"\x00"
        self.offsets = []
        for i, chunk in enumerate(self.raw):
            self.offsets.append(len(body))
            body += header(len(chunk)) + bytes([i]) * (40 + i * 10)   # fake compressed payload
        (self.dir / "up.dll").write_bytes(body)
        (self.dir / "data.bin").write_bytes(b"".join(self.raw))

    def run_tool(self, data="data.bin"):
        out = self.dir / "packs.txt"
        res = subprocess.run([sys.executable, "-I", str(TOOL), str(self.dir / "up.dll"),
                              str(self.dir / data), str(out)], capture_output=True, text=True)
        return res, out

    def test_index_lists_every_pack_in_order(self):
        res, out = self.run_tool()
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = [l.split() for l in out.read_text().splitlines() if not l.startswith("#")]
        self.assertEqual([int(r[1]) for r in rows], [len(c) for c in self.raw])
        self.assertEqual([int(r[2]) for r in rows], [0, 5000, 8000])
        # compressed size runs to the next header
        self.assertEqual(int(rows[0][0]), self.offsets[1] - self.offsets[0])

    def test_data_from_another_build_is_refused(self):
        (self.dir / "other.bin").write_bytes(b"A" * 5000 + b"X" * 3000 + b"C" * 700)
        res, out = self.run_tool("other.bin")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not match any SHA-256", res.stderr)
        self.assertFalse(out.exists())

    def test_wrong_size_is_refused(self):
        (self.dir / "short.bin").write_bytes(b"A" * 5000)
        res, _ = self.run_tool("short.bin")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("another build's data.bin", res.stderr)

    def test_key_is_the_same_for_the_original_and_the_patched_import(self):
        # one cached data.bin serves OptiClick's original (Cabinet.dll) and the patched (fsr4dec.dll) DLL
        dll = (self.dir / "up.dll").read_bytes()
        (self.dir / "orig.dll").write_bytes(dll + b"Cabinet.dll\x00")
        (self.dir / "patched.dll").write_bytes(dll + b"fsr4dec.dll\x00")
        keys = [subprocess.run([sys.executable, "-I", str(TOOL), "--key", str(self.dir / n)],
                               capture_output=True, text=True).stdout.strip() for n in ("orig.dll", "patched.dll")]
        self.assertEqual(len(keys[0]), 16)
        self.assertEqual(keys[0], keys[1])
        # another pack layout is another build
        (self.dir / "other.dll").write_bytes(dll.replace(header(5000), header(5001)))
        other = subprocess.run([sys.executable, "-I", str(TOOL), "--key", str(self.dir / "other.dll")],
                               capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(other, keys[0])

    def test_apply_script_parses(self):
        script = support.REPO / "tools" / "fsr4-xmx" / "apply.sh"
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)

    def test_decoder_exports_keep_the_bundle_ordinals(self):
        # the patched upscaler imports by ordinal: 40 CreateDecompressor, 43 Decompress, 45
        text = (support.REPO / "tools" / "fsr4-xmx" / "fsr4dec.def").read_text()
        for name, ordinal in (("CreateDecompressor", 40), ("Decompress", 43), ("ResetDecompressor", 45),
                              ("CloseDecompressor", 46)):
            self.assertRegex(text, rf"\n\s*{name}\s+@{ordinal}\b")


if __name__ == "__main__":
    unittest.main()
