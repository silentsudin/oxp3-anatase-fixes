# SPDX-License-Identifier: MIT
"""Offline tests for the s2idle low-power policy (tools/oxp3_lowpower.py).

Every decision is a pure function over a synthetic sysfs tree, so the policy can
be proven without touching the host: what it decides, what it writes, what it
records, what it restores, and that it refuses to write when it cannot.

No root, no suspend, no /sys, only a private temporary directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import os
import unittest

import support

mod = support.load("oxp3_lowpower", "oxp3_lowpower.py")

#: The kernel's documented parameter format; [default] is what this host reports.
DEFAULT_ASPM = "default performance powersave [default] powersupersave"


class Tree:
    """A writable synthetic sysfs root."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="oxp3-lowpower-test-")
        self.root = pathlib.Path(self._tmp.name)
        self.state = self.root / "state" / "state.json"

    def close(self):
        self._tmp.cleanup()

    # -- builders ---------------------------------------------------------
    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
        return path

    def read(self, relative):
        try:
            return (self.root / relative).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def aspm(self, text=DEFAULT_ASPM, writable=True):
        path = self.write("sys/module/pcie_aspm/parameters/policy", text)
        if not writable:
            path.chmod(0o444)
        return path

    def wakeup(self, source="wakeup65", name="mmc0", events=0, enabled="enabled",
               dev_target=None):
        base = "sys/class/wakeup/%s" % source
        self.write(base + "/name", name)
        self.write(base + "/event_count", str(events))
        if dev_target is not None:
            link = self.root / base / "device"
            os.symlink(dev_target, link)
        self.write(base + "/power/wakeup", enabled)
        return self.root / base

    def mmc_host(self, name="mmc0", enabled="enabled"):
        return self.write("sys/class/mmc_host/%s/power/wakeup" % name, enabled)

    def pci(self, addr, control="on", runtime="active", d3cold="0",
            wakeup="disabled"):
        self.write("sys/bus/pci/devices/%s/power/control" % addr, control)
        self.write("sys/bus/pci/devices/%s/power/runtime_status" % addr, runtime)
        self.write("sys/bus/pci/devices/%s/power/d3cold_allowed" % addr, d3cold)
        self.write("sys/bus/pci/devices/%s/power/wakeup" % addr, wakeup)
        return self.root / "sys/bus/pci/devices" / addr

    def sd_host(self, bound=True, unbind_writable=True):
        driver = self.root / "sys/bus/platform/drivers/rtsx_pci_sdmmc"
        self.write("sys/bus/platform/drivers/rtsx_pci_sdmmc/unbind", "")
        self.write("sys/bus/platform/drivers/rtsx_pci_sdmmc/bind", "")
        if not unbind_writable:
            (driver / "unbind").chmod(0o444)
        self.write("sys/bus/platform/devices/rtsx_pci_sdmmc.0/power/wakeup",
                   "disabled")
        if bound:
            # sysfs exposes a driver-bound device as a symlink in the driver dir
            driver.mkdir(parents=True, exist_ok=True)
            os.symlink(self.root / "sys/bus/platform/devices/rtsx_pci_sdmmc.0",
                       driver / "rtsx_pci_sdmmc.0")
        return self.root / "sys/bus/platform"

    def sd_card(self, mounted=False):
        self.write("sys/block/mmcblk0/size", "1024")
        if mounted:
            self.write("proc/mounts", "/dev/mmcblk0p1 /run/media/card vfat rw 0 0")

    def wakeup_stats(self, source="wakeup65", events=3, wakeups=0,
                     active=3, total_ms=15946, max_ms=5495):
        base = "sys/class/wakeup/%s/" % source
        for key, value in (("event_count", events), ("wakeup_count", wakeups),
                           ("active_count", active), ("total_time_ms", total_ms),
                           ("max_time_ms", max_ms),
                           ("active_time_ms", 0), ("prevent_suspend_time_ms", 0),
                           ("expire_count", 3)):
            self.write(base + key, str(value))

    def nvme(self, latency=100000, force="N"):
        self.write("sys/class/nvme/nvme0/model", "Predator SSD GM7 1TB")
        self.write("sys/module/nvme_core/parameters/default_ps_max_latency_us",
                   str(latency))
        self.write("sys/module/nvme_core/parameters/force_apst", force)

    def suspend_stats(self, success=3, failed_resume=0, fail=0):
        base = "sys/power/suspend_stats"
        for key, value in (("success", success), ("fail", fail),
                           ("failed_suspend", 0), ("failed_resume", failed_resume),
                           ("failed_resume_early", 0), ("failed_resume_noirq", 0)):
            self.write(base + "/" + key, str(value))
        self.write(base + "/last_failed_dev", "")


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.tree = Tree()
        self.addCleanup(self.tree.close)


class AspmParseTest(unittest.TestCase):
    def test_bracketed_selection(self):
        selected, options = mod.parse_aspm_policy(
            "default performance [powersave] powersupersave")
        self.assertEqual(selected, "powersave")
        self.assertEqual(options,
                         ["default", "performance", "powersave", "powersupersave"])

    def test_single_word_is_selected(self):
        self.assertEqual(mod.parse_aspm_policy("powersave")[0], "powersave")

    def test_empty_and_missing(self):
        self.assertEqual(mod.parse_aspm_policy(""), (None, []))
        self.assertEqual(mod.parse_aspm_policy(None), (None, []))


# ASPM is opt-in in the tool; these tests exercise the mechanism, so opt in.
os.environ.setdefault("OXP3_LOWPOWER_ASPM", "powersave")


class AspmDecisionTest(unittest.TestCase):
    def facts(self, selected="default", options=None, writable=True,
              present=True):
        return {"selected": selected,
                "options": options or ["default", "performance", "powersave",
                                       "powersupersave"],
                "writable": writable, "present": present}

    def test_set_when_default(self):
        decision = mod.aspm_decision(self.facts(), None, target="powersave")
        self.assertEqual((decision["action"], decision["value"]),
                         ("set", "powersave"))

    def test_keep_when_already_powersave(self):
        decision = mod.aspm_decision(self.facts(selected="powersave"), None, target="powersave")
        self.assertEqual(decision["action"], "keep")

    def test_skip_when_not_writable(self):
        decision = mod.aspm_decision(self.facts(writable=False), None, target="powersave")
        self.assertEqual(decision["action"], "skip")
        self.assertIn("not writable", decision["reason"])

    def test_skip_when_absent(self):
        decision = mod.aspm_decision(self.facts(present=False), None, target="powersave")
        self.assertEqual(decision["action"], "skip")

    def test_skip_when_target_not_offered(self):
        decision = mod.aspm_decision(
            self.facts(options=["default", "performance"]), None)
        self.assertEqual(decision["action"], "skip")

    def test_latch_wins(self):
        decision = mod.aspm_decision(
            self.facts(), {"reason": "failed_resume +1"})
        self.assertEqual(decision["action"], "skip")
        self.assertIn("failed_resume", decision["reason"])


class MmcWakeTest(TreeCase):
    def test_dangling_device_link_is_reported_as_virtual_not_disarmed(self):
        # The measured host has exactly this shape: name=mmc0 and NO device link
        # (/sys/class/wakeup/wakeup65 has only counters).  The parent attribute is
        # recorded, but it is not the source's enable bit, so no write is claimed.
        self.tree.wakeup(dev_target="/nonexistent/mmc0")
        self.tree.mmc_host()
        self.tree.wakeup_stats()
        source = mod.mmc_wake_sources(self.tree.root)[0]
        self.assertEqual(source["name"], "mmc0")
        self.assertFalse(source["own_attribute"])
        self.assertTrue(source["wakeup_path"].endswith("mmc_host/mmc0/power/wakeup"))
        self.assertEqual(source["stats"]["wakeup_count"], 0)
        self.assertEqual(source["stats"]["total_time_ms"], 15946)
        decision = mod.mmc_wake_decision(source)
        self.assertEqual(decision["action"], "skip")
        self.assertIn("virtual", decision["reason"])
        self.assertIn("park", decision["reason"])

    def test_own_wakeup_attribute_that_is_armed_is_disarmed(self):
        device = self.tree.pci("0000:56:00.0", wakeup="enabled")
        self.tree.wakeup(source="wakeup70", name="mmc1", dev_target=str(device),
                         enabled="enabled")
        source = mod.mmc_wake_sources(self.tree.root)[0]
        self.assertTrue(source["own_attribute"])
        decision = mod.mmc_wake_decision(source)
        self.assertEqual((decision["action"], decision["value"]),
                         ("set", "disabled"))

    def test_resolved_device_link_is_used(self):
        device = self.tree.pci("0000:56:00.0")
        self.tree.wakeup(dev_target=str(device), enabled="enabled")
        self.tree.mmc_host()  # must not win: the resolved device is first
        source = mod.mmc_wake_sources(self.tree.root)[0]
        self.assertIn("0000:56:00.0", source["wakeup_path"])

    def test_own_attribute_already_disabled_is_keep(self):
        device = self.tree.pci("0000:56:00.0", wakeup="disabled")
        self.tree.wakeup(dev_target=str(device), enabled="disabled")
        source = mod.mmc_wake_sources(self.tree.root)[0]
        self.assertFalse(source["enabled"])
        self.assertEqual(mod.mmc_wake_decision(source)["action"], "keep")

    def test_fully_unresolvable_source_is_skipped_not_guessed(self):
        self.tree.wakeup(dev_target="/nonexistent/mmc0")
        source = mod.mmc_wake_sources(self.tree.root)[0]
        self.assertEqual(source["wakeup_path"], "")
        decision = mod.mmc_wake_decision(source)
        self.assertEqual(decision["action"], "skip")
        self.assertTrue(decision["reason"])

    def test_non_mmc_sources_are_ignored(self):
        self.tree.wakeup(source="wakeup49", name="rtc_cmos")
        self.assertEqual(mod.mmc_wake_sources(self.tree.root), [])


class PlanTest(TreeCase):
    def build(self):
        self.tree.aspm()
        self.tree.wakeup(dev_target="/nonexistent/mmc0")
        self.tree.wakeup_stats()
        self.tree.mmc_host()
        self.tree.pci("0000:56:00.0", control="on")
        self.tree.pci("0000:00:1f.3", control="on")
        self.tree.nvme()
        self.tree.sd_host()
        self.tree.suspend_stats()

    def test_plan_reads_only_and_lists_the_changes(self):
        self.build()
        before = self.tree.read("sys/module/pcie_aspm/parameters/policy")
        planned = mod.plan(self.tree.root)
        ids = [row["id"] for row in planned["changes"]]
        self.assertIn("aspm.policy", ids)
        self.assertIn("sdcard.runtime_pm", ids)
        # the virtual mmc0 source has no attribute to write: it is parked instead
        self.assertNotIn("mmc.mmc0.wakeup", ids)
        self.assertEqual(planned["facts"]["park"]["action"], "set")
        # the pure plan wrote nothing
        self.assertEqual(
            self.tree.read("sys/module/pcie_aspm/parameters/policy"), before)
        self.assertEqual(mod.read_text(
            self.tree.root / "sys/class/mmc_host/mmc0/power/wakeup"), "enabled")

    def test_every_change_says_why_and_what_it_expects(self):
        self.build()
        for row in mod.plan(self.tree.root)["changes"]:
            if row.get("after") is None:
                continue
            self.assertTrue(row["why"], row["id"])
            self.assertTrue(row["expected"], row["id"])
            self.assertTrue(row["evidence"], row["id"])

    def test_absent_hardware_produces_no_changes_and_no_crash(self):
        planned = mod.plan(self.tree.root)
        self.assertEqual([row for row in planned["changes"]
                          if row.get("after") is not None], [])
        self.assertFalse(planned["facts"]["aspm"]["present"])

    def test_audio_is_recorded_never_written(self):
        self.build()
        rows = [row for row in mod.plan(self.tree.root)["changes"]
                if row["id"] == "audio.runtime_pm"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["after"])
        self.assertFalse(rows[0]["applied"])

    def test_nvme_knobs_are_never_written(self):
        self.build()
        planned = mod.plan(self.tree.root)
        self.assertEqual(planned["facts"]["nvme"]["default_ps_max_latency_us"],
                         100000)
        for row in planned["changes"]:
            self.assertNotIn("nvme", row["path"])


class ApplyUndoTest(TreeCase):
    def build(self):
        self.tree.aspm()
        self.tree.wakeup(dev_target="/nonexistent/mmc0")
        self.tree.wakeup_stats()
        self.tree.mmc_host()
        self.tree.pci("0000:56:00.0", control="on")
        self.tree.suspend_stats()
        self.tree.nvme()
        self.tree.sd_host()

    def test_apply_writes_the_policy_and_records_the_pre_image(self):
        self.build()
        state = mod.apply(self.tree.root, self.tree.state)
        self.assertEqual(self.tree.read("sys/module/pcie_aspm/parameters/policy"),
                         "powersave")
        self.assertEqual(
            self.tree.read("sys/class/mmc_host/mmc0/power/wakeup"), "enabled")
        self.assertEqual(
            self.tree.read("sys/bus/pci/devices/0000:56:00.0/power/control"),
            "auto")
        saved = json.loads(self.tree.state.read_text(encoding="utf-8"))
        rows = {row["id"]: row for row in saved["changes"]}
        self.assertEqual(rows["aspm.policy"]["before"], "default")
        # the virtual mmc0 source is reported, never written
        self.assertNotIn("mmc.mmc0.wakeup", rows)
        self.assertTrue(saved["sd_host"])
        self.assertTrue(all(row["applied"] for row in saved["changes"]
                            if row.get("after") is not None))
        self.assertEqual(saved["suspend_stats"]["success"], 3)

    def test_second_apply_is_idempotent(self):
        self.build()
        mod.apply(self.tree.root, self.tree.state)
        state = mod.apply(self.tree.root, self.tree.state)
        applied = [row for row in state["changes"] if row.get("applied")]
        self.assertEqual(applied, [])

    def test_dry_run_writes_nothing(self):
        self.build()
        mod.apply(self.tree.root, self.tree.state, dry_run=True)
        self.assertFalse(self.tree.state.exists())
        self.assertEqual(self.tree.read("sys/module/pcie_aspm/parameters/policy"),
                         DEFAULT_ASPM)

    def test_undo_restores_every_value(self):
        self.build()
        mod.apply(self.tree.root, self.tree.state)
        result = mod.undo(self.tree.root, self.tree.state)
        self.assertTrue(all(row["restored"] for row in result["restored"]))
        # sysfs policy parameters take the value name, so the pre-image is the
        # selected word ("default"), not the whole menu.
        self.assertEqual(
            mod.parse_aspm_policy(
                self.tree.read("sys/module/pcie_aspm/parameters/policy"))[0],
            "default")
        self.assertEqual(
            self.tree.read("sys/class/mmc_host/mmc0/power/wakeup"), "enabled")
        self.assertEqual(
            self.tree.read("sys/bus/pci/devices/0000:56:00.0/power/control"), "on")

    def test_undo_refuses_to_clobber_a_third_party_value(self):
        self.build()
        mod.apply(self.tree.root, self.tree.state)
        # someone else changed it after us: put back the current value only
        self.tree.write("sys/module/pcie_aspm/parameters/policy", "performance")
        result = mod.undo(self.tree.root, self.tree.state)
        row = next(row for row in result["restored"]
                   if row["id"] == "aspm.policy")
        self.assertFalse(row["restored"])
        self.assertEqual(self.tree.read("sys/module/pcie_aspm/parameters/policy"),
                         "performance")

    def test_apply_never_raises_on_read_only_attributes(self):
        self.tree.aspm(writable=False)
        self.tree.pci("0000:56:00.0")
        state = mod.apply(self.tree.root, self.tree.state)
        self.assertEqual(state["schema"], mod.SCHEMA)
        self.assertEqual(self.tree.read("sys/module/pcie_aspm/parameters/policy"),
                         DEFAULT_ASPM)


class SdHostParkTest(TreeCase):
    """The measured mmc0 stall is a wakelock during suspend, handled by parking."""

    def build(self):
        self.tree.aspm()
        self.tree.wakeup(dev_target="/nonexistent/mmc0")
        self.tree.wakeup_stats()
        self.tree.pci("0000:56:00.0", control="on")
        self.tree.suspend_stats()
        self.tree.sd_host()
        self.tree.write("sys/bus/platform/drivers/rtsx_pci_sdmmc/bind", "")

    def simulate_unbound(self):
        # sysfs removes the driver link when a device is unbound
        (self.tree.root / "sys/bus/platform/drivers/rtsx_pci_sdmmc/"
         "rtsx_pci_sdmmc.0").unlink()

    def test_pre_park_records_and_binds_back_on_post(self):
        self.build()
        result = mod.pre(self.tree.root, self.tree.state)
        self.assertTrue(result["unbound"])
        self.assertIn("held active", result["decision"]["reason"])
        state = json.loads(self.tree.state.read_text(encoding="utf-8"))
        self.assertTrue(state["sd_host"]["unbound"])
        self.simulate_unbound()
        self.assertFalse(mod.sd_host_facts(self.tree.root)["bound"])
        posted = mod.post(self.tree.root, self.tree.state)
        self.assertTrue(posted["rebind"]["bound"])
        state = json.loads(self.tree.state.read_text(encoding="utf-8"))
        self.assertFalse(state["sd_host"]["unbound"])
        # the kernel re-creates the driver link when the name is written to bind
        self.assertIn(mod.SD_HOST_DEVICE,
                      self.tree.read("sys/bus/platform/drivers/rtsx_pci_sdmmc/bind"))

    def test_pre_never_parks_a_card_in_use(self):
        self.build()
        self.tree.sd_card(mounted=True)
        result = mod.pre(self.tree.root, self.tree.state)
        self.assertFalse(result["unbound"])
        self.assertIn("never park", result["decision"]["reason"])
        self.assertEqual(
            self.tree.read("sys/bus/platform/drivers/rtsx_pci_sdmmc/unbind"), "")

    def test_pre_skips_without_the_driver(self):
        self.tree.aspm()
        result = mod.pre(self.tree.root, self.tree.state)
        self.assertFalse(result["unbound"])
        self.assertEqual(result["decision"]["action"], "skip")

    def test_latch_stops_the_park(self):
        self.build()
        mod.save_state(self.tree.state, {"latch": {"reason": "failed_resume +1"}})
        result = mod.pre(self.tree.root, self.tree.state)
        self.assertFalse(result["unbound"])
        self.assertIn("latched", result["decision"]["reason"])

    def test_apply_recovers_a_leftover_park(self):
        self.build()
        mod.pre(self.tree.root, self.tree.state)
        self.simulate_unbound()
        # a crash before post: the next boot's apply must rebind and say so
        state = mod.apply(self.tree.root, self.tree.state)
        self.assertTrue(state["recovered"]["bound"])
        self.assertIn(mod.SD_HOST_DEVICE,
                      self.tree.read("sys/bus/platform/drivers/rtsx_pci_sdmmc/bind"))
        self.assertFalse(json.loads(
            self.tree.state.read_text(encoding="utf-8"))["sd_host"]["unbound"])

    def test_undo_rebinds(self):
        self.build()
        mod.pre(self.tree.root, self.tree.state)
        self.simulate_unbound()
        result = mod.undo(self.tree.root, self.tree.state)
        self.assertTrue(result["recovered"]["bound"])
        self.assertIn(mod.SD_HOST_DEVICE,
                      self.tree.read("sys/bus/platform/drivers/rtsx_pci_sdmmc/bind"))

    def test_park_decision_when_card_device_exists_but_not_mounted(self):
        self.build()
        self.tree.sd_card(mounted=False)
        decision = mod.park_decision(
            mod.sd_host_facts(self.tree.root),
            mod.sd_media_facts(self.tree.root), None)
        self.assertEqual(decision["action"], "skip")


class PostTest(TreeCase):
    def build(self, failed_resume=0, control="on"):
        self.tree.aspm()
        self.tree.pci("0000:56:00.0", control=control)
        self.tree.suspend_stats(failed_resume=failed_resume)

    def test_post_without_failure_does_not_latch(self):
        self.build()
        mod.apply(self.tree.root, self.tree.state)
        result = mod.post(self.tree.root, self.tree.state)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["latch"])
        self.assertEqual(result["failed"], {})

    def test_post_latches_aspm_off_after_a_failed_resume(self):
        self.build()
        mod.apply(self.tree.root, self.tree.state)
        self.tree.suspend_stats(failed_resume=1)
        result = mod.post(self.tree.root, self.tree.state)
        self.assertTrue(result["latch"]["reason"])
        self.assertIn("failed_resume", result["latch"]["reason"])
        # the next boot's apply must leave ASPM alone
        self.tree.aspm()
        state = mod.apply(self.tree.root, self.tree.state)
        self.assertEqual(self.tree.read("sys/module/pcie_aspm/parameters/policy"),
                         DEFAULT_ASPM)
        ids = [row["id"] for row in state["changes"] if row.get("applied")]
        self.assertNotIn("aspm.policy", ids)

    def test_post_without_state_is_harmless(self):
        result = mod.post(self.tree.root, self.tree.state)
        self.assertEqual(result["status"], "no-policy-installed")


class StatusTest(TreeCase):
    def test_status_before_install(self):
        data = mod.status(self.tree.root, self.tree.state)
        self.assertFalse(data["installed"])
        self.assertIn("facts", data)

    def test_status_after_apply_carries_the_change_list(self):
        self.tree.aspm()
        self.tree.pci("0000:56:00.0")
        mod.apply(self.tree.root, self.tree.state)
        data = mod.status(self.tree.root, self.tree.state)
        self.assertTrue(data["installed"])
        self.assertTrue(data["apply"]["changes"])


class CliTest(TreeCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mod.main(argv)
        return code, out.getvalue()

    def test_dry_run_json_is_machine_readable(self):
        self.tree.aspm()
        code, out = self.run_cli(
            ["--root", str(self.tree.root), "--state", str(self.tree.state),
             "--json", "apply", "--dry-run"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["tool"], "oxp3-lowpower")
        self.assertTrue(data["changes"])

    def test_status_without_state_exits_zero(self):
        code, out = self.run_cli(
            ["--root", str(self.tree.root), "--state", str(self.tree.state),
             "status"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["installed"])

    def test_unknown_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                mod.main(["--root", str(self.tree.root), "nonsense"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
