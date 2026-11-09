"""Offline tests for tools/hhd-oxp3-voltap (the HHD plugin).

Two layers, no HHD and no hardware required:

* the tap state machine (``oxp3_hhd_voltap.tap``) with synthetic clocks;
* the defensive HHD hook (``oxp3_hhd_voltap.hhd_hook``) against a fake
  ``hhd.device.oxp.base`` module - including the wrong-API-shape no-op cases
  and the "an exception in the filter must never take the controller down"
  contract;
* the installer into a scratch site directory (``install.sh --site``), which
  also proves the ``hhd.plugins`` entry point resolves.

The real ``hhd`` package is never modified: tests inject fake modules into
``sys.modules`` and restore them.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest

import support

PLUGIN_ROOT = support.TOOLS / "hhd-oxp3-voltap"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import oxp3_hhd_voltap  # noqa: E402
from oxp3_hhd_voltap import hhd_hook, tap  # noqa: E402

UP = "key_volumeup"
DOWN = "key_volumedown"
A = "a"


def press(code=UP):
    return {"type": "button", "code": code, "value": True}


def release(code=UP):
    return {"type": "button", "code": code, "value": False}


class TapNormaliserTest(unittest.TestCase):
    def make(self, **kw):
        kw.setdefault("hold", 0.060)
        kw.setdefault("gap", 0.060)
        return tap.TapNormaliser(**kw)

    def test_one_clean_tap_with_minimum_hold(self):
        n = self.make()
        self.assertEqual(n.handle(0.0, UP, True), [(UP, True)])
        # A release before the minimum hold is deferred, not zero-length.
        self.assertEqual(n.handle(0.010, UP, False), [])
        self.assertEqual(n.stats["deferred_release"], 1)
        self.assertEqual(n.poll(0.059), [])
        self.assertEqual(n.poll(0.060), [(UP, False)])
        self.assertEqual(n.stats["press"], 1)

    def test_long_press_release_passes_through(self):
        n = self.make()
        n.handle(0.0, UP, True)
        self.assertEqual(n.poll(0.2), [(UP, False)])
        self.assertEqual(n.down, {})

    def test_lost_release_is_synthesised(self):
        n = self.make(hold=0.060)
        n.handle(0.0, UP, True)
        self.assertEqual(n.poll(0.5), [(UP, False)])
        self.assertEqual(n.down, {})

    def test_repeat_value_is_never_forwarded(self):
        n = self.make()
        self.assertEqual(n.handle(0.0, UP, 2), [])
        self.assertEqual(n.stats["repeat"], 1)

    def test_duplicate_press_is_dropped(self):
        n = self.make()
        n.handle(0.0, UP, True)
        self.assertEqual(n.handle(0.001, UP, True), [])
        self.assertEqual(n.stats["duplicate"], 1)

    def test_bounce_window(self):
        n = self.make(gap=0.060, hold=0.0)
        n.handle(0.0, UP, True)
        n.handle(0.001, UP, False)
        self.assertEqual(n.handle(0.050, UP, True), [])
        self.assertEqual(n.stats["bounce"], 1)
        self.assertEqual(n.handle(0.070, UP, True), [(UP, True)])

    def test_orphan_release_becomes_one_tap(self):
        # The EC/atkbd force_release shape: only the release arrives.
        n = self.make(hold=0.060)
        self.assertEqual(n.handle(1.0, UP, False), [(UP, True)])
        self.assertEqual(n.stats["orphan_tap"], 1)
        self.assertEqual(n.poll(1.060), [(UP, False)])

    def test_late_release_after_our_own_synthetic_release_is_dropped(self):
        n = self.make(hold=0.060)
        n.handle(0.0, UP, True)
        n.poll(0.060)
        self.assertEqual(n.handle(0.200, UP, False), [])
        self.assertEqual(n.stats["late_release"], 1)
        self.assertEqual(n.stats["orphan_tap"], 0)

    def test_per_key_state(self):
        n = self.make(hold=0.0)
        n.handle(0.0, UP, True)
        self.assertEqual(n.handle(0.001, DOWN, True), [(DOWN, True)])
        self.assertEqual(n.handle(0.002, UP, False), [(UP, False)])
        self.assertEqual(n.handle(0.003, DOWN, False), [(DOWN, False)])

    def test_max_hold_caps_a_bad_hold(self):
        n = self.make(hold=10.0, max_hold=0.500)
        self.assertEqual(n.hold, 0.500)
        n.handle(0.0, UP, True)
        self.assertEqual(n.poll(0.499), [])
        self.assertEqual(n.poll(0.500), [(UP, False)])

    def test_release_all(self):
        n = self.make()
        n.handle(0.0, UP, True)
        n.handle(0.0, DOWN, True)
        self.assertEqual(sorted(n.release_all(1.0)), [(DOWN, False), (UP, False)])
        self.assertEqual(n.down, {})

    def test_next_deadline(self):
        n = self.make()
        self.assertIsNone(n.next_deadline())
        n.handle(1.0, UP, True)
        self.assertAlmostEqual(n.next_deadline(), 1.060)

    def test_non_volume_and_garbage_are_ignored(self):
        n = self.make()
        self.assertEqual(n.handle(0.0, A, True), [])
        self.assertEqual(n.handle(0.0, UP, "nonsense"), [])

    def test_guard_disables_after_failure_and_reraises(self):
        n = self.make()

        def boom():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            n.guard(boom)
        self.assertTrue(n.failed)
        self.assertEqual(n.handle(0.0, UP, True), [])


class _FakeAtKbd:
    """Stand-in for hhd.device.oxp.base.OxpAtKbd (the shape we hook)."""

    def __init__(self, volume_taps=True, events=None):
        self.volume_taps = volume_taps
        self.events = list(events or [])
        self.own_taps = 0
        self.produced = 0

    def _volume_taps(self, evs, curr):
        self.own_taps += 1
        return evs

    def produce(self, fds):
        self.produced += 1
        if self.volume_taps:
            return self._volume_taps(list(self.events), 0.0)
        return list(self.events)


class _FakeModule(types.ModuleType):
    pass


class HookTest(unittest.TestCase):
    MODULE = "hhd.device.oxp.base"

    def setUp(self):
        self.saved = {
            name: sys.modules.get(name)
            for name in (self.MODULE, "hhd.device.oxp", "hhd.device")
        }
        # A fresh subclass per test: install() patches the class it is given,
        # and that must never leak into the next test.
        self.kbd = type("FakeAtKbd", (_FakeAtKbd,), {})
        self.module = _FakeModule(self.MODULE)
        self.module.OxpAtKbd = self.kbd
        sys.modules[self.MODULE] = self.module

    def tearDown(self):
        for name, old in self.saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    def test_probe_sees_a_usable_shape(self):
        info = hhd_hook.probe(self.MODULE, "OxpAtKbd")
        self.assertTrue(info["available"], info)
        self.assertTrue(info["has_volume_taps"])
        self.assertEqual(info["reason"], "ok")

    def test_install_wraps_and_normalises_without_touching_other_events(self):
        result = hhd_hook.install(self.MODULE, "OxpAtKbd", hold=0.0, gap=0.0)
        self.assertTrue(result["installed"], result)
        other = {"type": "button", "code": A, "value": True}
        dev = self.kbd(volume_taps=True, events=[press(UP), other])
        out = dev.produce([])
        self.assertEqual(dev.own_taps, 0)  # HHD's fixed taps were switched off
        self.assertFalse(dev.volume_taps)
        self.assertIn(other, out)          # non-volume events untouched
        # hold=0.0: the synthetic release is due immediately, in the same batch.
        self.assertEqual(out, [press(UP), other, release(UP)])

    def test_install_is_idempotent(self):
        first = hhd_hook.install(self.MODULE, "OxpAtKbd")
        second = hhd_hook.install(self.MODULE, "OxpAtKbd")
        self.assertTrue(first["installed"])
        self.assertTrue(second["installed"])
        self.assertIn("already", second["reason"])

    def test_uninstall_restores_the_original(self):
        original = self.kbd.produce
        hhd_hook.install(self.MODULE, "OxpAtKbd")
        self.assertIsNot(self.kbd.produce, original)
        removed = hhd_hook.uninstall(self.MODULE, "OxpAtKbd")
        self.assertTrue(removed["removed"])
        self.assertIs(self.kbd.produce, original)

    def test_missing_module_is_a_noop(self):
        result = hhd_hook.install("no.such.module", "OxpAtKbd")
        self.assertFalse(result["installed"])
        self.assertIn("cannot import", result["reason"])

    def test_missing_class_is_a_noop(self):
        result = hhd_hook.install(self.MODULE, "NoSuchKeyboard")
        self.assertFalse(result["installed"])
        self.assertIn("is missing", result["reason"])

    def test_unexpected_produce_signature_is_refused(self):
        class Weird:
            def produce(fds):  # no self: a shape we do not understand
                return []

        self.module.OxpAtKbd = Weird
        info = hhd_hook.probe(self.MODULE, "OxpAtKbd")
        self.assertFalse(info["available"])
        self.assertIn("signature", info["reason"])
        result = hhd_hook.install(self.MODULE, "OxpAtKbd")
        self.assertFalse(result["installed"])

    def test_non_callable_produce_is_refused(self):
        class Broken:
            produce = None

        self.module.OxpAtKbd = Broken
        result = hhd_hook.install(self.MODULE, "OxpAtKbd")
        self.assertFalse(result["installed"])
        self.assertIn("not callable", result["reason"])

    def test_a_broken_filter_never_breaks_the_producer(self):
        hhd_hook.install(self.MODULE, "OxpAtKbd", hold=0.0, gap=0.0)
        dev = self.kbd(volume_taps=True, events=[press(UP)])
        # A first produce installs the normaliser; then corrupt it so the next
        # batch raises inside apply_filter.
        dev.produce([])
        dev._oxp3_voltap_norm = object()  # no .guard/.handle
        dev.events = [press(DOWN)]
        with self.assertLogs("oxp3_hhd_voltap.hhd_hook", level="ERROR"):
            out = dev.produce([])
        self.assertEqual(out, [press(DOWN)])  # HHD's event, unchanged
        self.assertTrue(getattr(dev, "_oxp3_voltap_failed", False))
        self.assertTrue(dev.volume_taps)      # HHD's own taps restored
        # Subsequent batches pass through without retrying the filter, and
        # HHD's own taps stay enabled (they are not switched off again).
        dev.events = [press(UP)]
        self.assertEqual(dev.produce([]), [press(UP)])
        self.assertTrue(dev.volume_taps)

    def test_producer_errors_still_propagate(self):
        class Failing(_FakeAtKbd):
            def produce(self, fds):
                raise OSError(19, "No such device")

        self.module.OxpAtKbd = Failing
        hhd_hook.install(self.MODULE, "OxpAtKbd")
        dev = Failing()
        with self.assertRaises(OSError):
            dev.produce([])

    def test_apply_filter_handles_plain_events(self):
        n = tap.TapNormaliser(hold=0.06, gap=0.06)
        axis = {"type": "axis", "code": "lx", "value": 0.5}
        out = hhd_hook.apply_filter(n, [axis, press(UP)], now=0.0)
        self.assertEqual(out, [axis, press(UP)])
        self.assertEqual(hhd_hook.apply_filter(n, [], now=0.03), [])
        self.assertEqual(hhd_hook.apply_filter(n, [], now=0.06), [release(UP)])


class PluginContractTest(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("hhd.plugins")

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("hhd.plugins", None)
        else:
            sys.modules["hhd.plugins"] = self.saved

    def test_autodetect_returns_the_plugin(self):
        plugins = oxp3_hhd_voltap.autodetect([])
        self.assertEqual(len(plugins), 1)
        plugin = plugins[0]
        self.assertEqual(plugin.name, "oxp3_voltap")
        self.assertIsInstance(plugin.priority, int)
        self.assertEqual(plugin.settings(), {})
        self.assertFalse(plugin.validate([], {}, None))
        # Lifecycle methods are all present and callable; no exceptions.
        plugin.prepare({})
        plugin.update({})
        plugin.notify([])
        plugin.close()

    def test_autodetect_can_be_disabled(self):
        old = os.environ.get("OXP3_HHD_VOLTAP")
        os.environ["OXP3_HHD_VOLTAP"] = "0"
        try:
            self.assertEqual(oxp3_hhd_voltap.autodetect([]), [])
        finally:
            if old is None:
                os.environ.pop("OXP3_HHD_VOLTAP", None)
            else:
                os.environ["OXP3_HHD_VOLTAP"] = old

    def test_open_noops_when_the_hhd_shape_is_unknown(self):
        old = sys.modules.get("hhd.device.oxp.base")
        sys.modules["hhd.device.oxp.base"] = types.ModuleType(
            "hhd.device.oxp.base")  # no OxpAtKbd
        try:
            plugin = oxp3_hhd_voltap.VolumeTapPlugin()
            with self.assertLogs("oxp3_hhd_voltap", level="WARNING"):
                plugin.open(None, None)   # must not raise
            self.assertFalse(plugin.hook["installed"])
            self.assertIn("is missing", plugin.hook["reason"])
            hook = plugin.describe()["hook"]
            self.assertFalse(hook["installed"])
        finally:
            if old is None:
                sys.modules.pop("hhd.device.oxp.base", None)
            else:
                sys.modules["hhd.device.oxp.base"] = old


class InstallerTest(unittest.TestCase):
    """install.sh into a scratch site dir; no root, no host change."""

    def run_installer(self, *args):
        return subprocess.run(
            [str(PLUGIN_ROOT / "install.sh"), *args],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_install_status_uninstall_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="oxp3-plugin-install-") as tmp:
            site = pathlib.Path(tmp) / "site-packages"
            sentinel = pathlib.Path(tmp) / "keep-me.txt"
            sentinel.write_text("not ours\n", encoding="utf-8")

            res = self.run_installer("--site", str(site), "install")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("ENTRY=ok", res.stdout)
            self.assertIn("restarting hhd drops the emulated steam controller",
                          res.stdout.lower())
            pkg = site / "oxp3_hhd_voltap"
            infos = list(site.glob("oxp3_hhd_voltap-*.dist-info"))
            self.assertTrue(pkg.is_dir())
            self.assertEqual(len(infos), 1)
            self.assertIn("hhd.plugins",
                          (infos[0] / "entry_points.txt").read_text())
            self.assertIn("oxp3_voltap",
                          (infos[0] / "entry_points.txt").read_text())

            res = self.run_installer("--site", str(site), "status")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("package: present", res.stdout)
            self.assertIn("ENTRY=ok", res.stdout)

            # Same files, remove exactly ours, leave everything else alone.
            res = self.run_installer("--site", str(site), "uninstall")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(pkg.exists())
            self.assertEqual(list(site.glob("oxp3_hhd_voltap*")), [])
            self.assertTrue(sentinel.exists())

            res = self.run_installer("--site", str(site), "uninstall")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("nothing was installed", res.stdout)


if __name__ == "__main__":
    unittest.main()
