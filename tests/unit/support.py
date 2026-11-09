"""Shared helpers for the offline unit suite in tests/unit/.

Everything here is hardware-free: modules are loaded straight from tools/ by
path (their filenames are not importable module names), and synthetic sysfs
trees live in a private temporary directory that is removed by the test case.

No file outside the temporary directory is written by these tests.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"

# tools/oxp3-led.py and friends do `import oxp3_hid` after extending sys.path
# themselves; putting tools/ on sys.path here makes that resolve to the same
# module object the tests patch.
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

_LOADED: dict[str, object] = {}


def load(module_name: str, relative_path: str):
    """Import a tool by path (cached) even when the filename is not a module."""
    if module_name in _LOADED:
        return _LOADED[module_name]
    path = TOOLS / relative_path
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    _LOADED[module_name] = module
    return module


class TmpTree:
    """A throw-away directory used as a synthetic /sys or config root."""

    def __init__(self, prefix: str = "oxp3-unit-"):
        self._tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self.root = pathlib.Path(self._tmp.name)

    # -- path/write helpers -------------------------------------------------
    def path(self, *parts) -> pathlib.Path:
        return self.root.joinpath(*parts)

    def write(self, relative: str, text: object) -> pathlib.Path:
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(text), encoding="utf-8")
        return p

    def write_bytes(self, relative: str, data: bytes) -> pathlib.Path:
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def mkdir(self, relative: str) -> pathlib.Path:
        p = self.root / relative
        p.mkdir(parents=True, exist_ok=True)
        return p

    def cleanup(self) -> None:
        self._tmp.cleanup()


def make_powercap(tree: TmpTree, zone: str = "intel-rapl:0",
                  limits=(35.0, 37.0, 122.0), window_us: int = 27_980_000,
                  name: str = "package-0") -> pathlib.Path:
    """Build one RAPL package-0 zone with long_term/short_term/peak_power."""
    base = f"powercap/{zone}"
    tree.write(f"{base}/name", name)
    tree.write(f"{base}/enabled", 1)
    for idx, (level, watts) in enumerate(zip(("long_term", "short_term", "peak_power"),
                                             limits)):
        tree.write(f"{base}/constraint_{idx}_name", level)
        tree.write(f"{base}/constraint_{idx}_power_limit_uw",
                   int(round(watts * 1_000_000)))
        tree.write(f"{base}/constraint_{idx}_time_window_us", window_us)
    # constraint_3..4 intentionally absent: Domain.__init__ stops at the first
    # missing *_name, exactly like a real package zone.
    return tree.path(base)


def make_battery(tree: TmpTree, name: str = "BAT0", *,
                 capacity: int = 80, status: str = "Discharging",
                 end_threshold=None, behaviour=None,
                 energy_now: int = 40_000_000,
                 energy_full: int = 50_000_000,
                 energy_design: int = 54_000_000,
                 cycle_count: int = 0) -> pathlib.Path:
    base = f"power_supply/{name}"
    tree.write(f"{base}/type", "Battery")
    tree.write(f"{base}/present", 1)
    tree.write(f"{base}/status", status)
    tree.write(f"{base}/capacity", capacity)
    tree.write(f"{base}/capacity_level", "Normal")
    tree.write(f"{base}/manufacturer", "UnitTest")
    tree.write(f"{base}/model_name", "Synthetic pack")
    tree.write(f"{base}/technology", "Li-ion")
    tree.write(f"{base}/energy_now", energy_now)
    tree.write(f"{base}/energy_full", energy_full)
    tree.write(f"{base}/energy_full_design", energy_design)
    tree.write(f"{base}/power_now", 7_500_000)
    tree.write(f"{base}/voltage_now", 15_400_000)
    tree.write(f"{base}/voltage_min_design", 15_400_000)
    tree.write(f"{base}/cycle_count", cycle_count)
    if end_threshold is not None:
        tree.write(f"{base}/charge_control_end_threshold", end_threshold)
    if behaviour is not None:
        tree.write(f"{base}/charge_behaviour", behaviour)
    return tree.path(base)


def make_mains(tree: TmpTree, name: str = "ADP1", online: int = 1) -> pathlib.Path:
    base = f"power_supply/{name}"
    tree.write(f"{base}/type", "Mains")
    tree.write(f"{base}/online", online)
    return tree.path(base)


def make_fan(tree: TmpTree, name: str = "oxp_ec", *, pwm: int = 124,
             pwm_enable: int = 2, rpm: int = 2400,
             directory: str = "hwmon/hwmon6") -> pathlib.Path:
    tree.write(f"{directory}/name", name)
    tree.write(f"{directory}/pwm1", pwm)
    tree.write(f"{directory}/pwm1_enable", pwm_enable)
    tree.write(f"{directory}/fan1_input", rpm)
    return tree.path(directory)
