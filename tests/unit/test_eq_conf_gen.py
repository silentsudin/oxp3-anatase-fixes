# SPDX-License-Identifier: MIT
"""The generated speaker EQ must preserve the user's extracted bands."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "tools" / "audio" / "eq-conf-gen.py"


class EqConfGenTest(unittest.TestCase):
    def test_generates_chain_from_extracted_bands(self):
        data = {"eq_blocks": [{"offset_1based": 19, "bands": [
            {"freq_hz": 80, "gain_db": -3.0, "q": 0.7, "enable": 1},
            {"freq_hz": 140, "gain_db": 6.0, "q": 2.0, "enable": 1},
            {"freq_hz": 16000, "gain_db": 0.0, "q": 1.0, "enable": 1},
        ]}]}
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "apo.json"
            out = Path(d) / "eq.conf"
            src.write_text(json.dumps(data))
            subprocess.run(["python3", str(GEN), "--json", str(src), "--out", str(out),
                            "--profile", "safe"], check=True)
            text = out.read_text()
        self.assertIn('"Freq" = 80 "Q" = 0.7 "Gain" = -3', text)
        self.assertIn('"Freq" = 140 "Q" = 2 "Gain" = 6', text)
        self.assertNotIn("16000", text)       # zero-gain bands are omitted
        self.assertIn('"Gain" = -8', text)    # safe headroom
        self.assertIn('node.target  = "__SINK__"', text)


if __name__ == "__main__":
    unittest.main()
