# SPDX-License-Identifier: MIT
"""The session CPU-topology drop-in must pin exactly the L3-sharing cores.

The file is installed as /etc/systemd/user/anatase-gamemode.service.d/oxp3-cpus.conf and applies
to gamescope, Steam and therefore every Proton game. Two things have to agree or the setting does
the opposite of what it is for: the affinity range, and the topology Wine is told about. If they
drift apart (a CPU added to one and not the other), Wine sizes thread pools for cores the session
cannot use, or leaves usable cores unreported.
"""
from __future__ import annotations

import unittest
from pathlib import Path

CONF = Path(__file__).resolve().parents[2] / "files" / "anatase-gamemode-cpus.conf"
L3 = Path("/sys/devices/system/cpu/cpu0/cache/index3/shared_cpu_list")


def expand(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


class CpuTopologyConfTest(unittest.TestCase):
    def setUp(self):
        self.text = CONF.read_text()
        self.keys = {}
        for line in self.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                self.keys[k.strip()] = v.strip()

    def test_affinity_is_the_l3_range(self):
        self.assertIn("CPUAffinity", self.keys)
        self.assertEqual(expand(self.keys["CPUAffinity"]), set(range(10)))

    def test_wine_topology_matches_the_affinity(self):
        env = self.keys.get("Environment", "")
        self.assertIn("WINE_CPU_TOPOLOGY=", env)
        spec = env.split("WINE_CPU_TOPOLOGY=", 1)[1].split()[0]
        count, _, cpus = spec.partition(":")
        self.assertEqual(int(count), 10)
        self.assertEqual(expand(cpus), expand(self.keys["CPUAffinity"]))

    @unittest.skipUnless(L3.exists(), "no L3 topology on this host")
    def test_affinity_is_the_hardware_l3_list(self):
        self.assertEqual(expand(self.keys["CPUAffinity"]), expand(L3.read_text().strip()))

    def test_it_is_a_service_drop_in(self):
        self.assertIn("[Service]", self.text)


if __name__ == "__main__":
    unittest.main()
