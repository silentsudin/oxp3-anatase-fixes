# SPDX-License-Identifier: MIT
"""Unit tests for tools/oxp3-imu.py: the pure SI/HHD-exposure helpers and the
sysfs readers, using a synthetic IIO device instead of the BMI260."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

imu = support.load("oxp3_imu", "oxp3-imu.py")


def make_iio(tree, name="iio:device0", dev_name="10EC5280:00", label="accel-display"):
    base = f"iio/{name}"
    tree.write(f"{base}/name", dev_name)
    tree.write(f"{base}/label", label)
    tree.write(f"{base}/in_accel_scale", "0.000598")
    tree.write(f"{base}/in_anglvel_scale", "0.001065")
    tree.write(f"{base}/in_accel_sampling_frequency", "100")
    tree.write(f"{base}/in_accel_x_raw", 10)
    tree.write(f"{base}/in_accel_y_raw", -20)
    tree.write(f"{base}/in_accel_z_raw", 1000)
    tree.write(f"{base}/in_anglvel_x_raw", 1)
    tree.write(f"{base}/in_anglvel_y_raw", 2)
    tree.write(f"{base}/in_anglvel_z_raw", 3)
    tree.write(f"{base}/in_mount_matrix", "1, 0, 0; 0, 1, 0; 0, 0, 1")
    tree.write(f"{base}/trigger/current_trigger", "hhd")
    return tree.path(base)


class MotionExposedTest(unittest.TestCase):
    def test_empty_state(self):
        exposed, detail = imu.motion_exposed({})
        self.assertFalse(exposed)
        self.assertIn("no motion", detail)

    def test_hori_steam_and_sd_default_to_synced(self):
        for mode in ("hori_steam", "sd"):
            exposed, _ = imu.motion_exposed({"controller_mode": {"mode": mode}})
            self.assertTrue(exposed, mode)

    def test_hori_steam_sync_disabled(self):
        exposed, detail = imu.motion_exposed(
            {"controller_mode": {"mode": "hori_steam",
                                 "hori_steam": {"sync_gyro": False}}})
        self.assertFalse(exposed)
        self.assertIn("sync_gyro=False", detail)

    def test_dualsense(self):
        exposed, _ = imu.motion_exposed({"controller_mode": {"mode": "dualsense"}})
        self.assertTrue(exposed)
        exposed, _ = imu.motion_exposed(
            {"controller_mode": {"mode": "dualsense",
                                 "dualsense": {"sync_gyro": False}}})
        self.assertFalse(exposed)

    def test_other_modes_never_carry_motion(self):
        for mode in ("uinput", "xbox", "?", None):
            exposed, detail = imu.motion_exposed({"controller_mode": {"mode": mode}})
            self.assertFalse(exposed, mode)


class SysfsHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.iio = make_iio(self.tree)

    def test_read_and_read_float(self):
        self.assertEqual(imu.read(str(self.iio / "name")), "10EC5280:00")
        self.assertIsNone(imu.read(str(self.iio / "absent")))
        self.assertEqual(imu.read(str(self.iio / "absent"), "x"), "x")
        self.assertAlmostEqual(imu.read_float(str(self.iio / "in_accel_scale")),
                               0.000598)
        bad = self.tree.write("bad", "NaN-ish\n")
        self.assertIsNone(imu.read_float(str(bad)))

    def test_read_int_tolerates_missing_and_garbage(self):
        self.assertEqual(imu._read_int(str(self.iio / "in_accel_x_raw")), 10)
        self.assertEqual(imu._read_int(str(self.tree.path("absent"))), 0)
        self.assertEqual(imu._read_int(str(self.tree.write("junk", "nope"))), 0)

    def test_find_iio_by_acpi_name_and_label(self):
        with mock.patch.object(imu, "IIO", str(self.tree.path("iio"))):
            self.assertEqual(imu.find_iio(), str(self.iio))

    def test_find_iio_by_label(self):
        make_iio(self.tree, name="iio:device0", dev_name="unrelated",
                 label="gyro")
        other = make_iio(self.tree, name="iio:device1", dev_name="unrelated2",
                         label="accel-display")
        with mock.patch.object(imu, "IIO", str(self.tree.path("iio"))):
            self.assertEqual(imu.find_iio(), str(other))

    def test_find_iio_fallback_for_unusual_names(self):
        tree = support.TmpTree()
        self.addCleanup(tree.cleanup)
        tree.write("iio/iio:device0/name", "i2c-something")
        tree.write("iio/iio:device0/in_anglvel_x_raw", 1)
        with mock.patch.object(imu, "IIO", str(tree.path("iio"))):
            self.assertEqual(imu.find_iio(), str(tree.path("iio/iio:device0")))

    def test_find_iio_absent(self):
        with mock.patch.object(imu, "IIO", str(self.tree.path("absent"))):
            self.assertIsNone(imu.find_iio())

    def test_driver_of_resolves_symlink(self):
        # Real sysfs: /sys/bus/iio/devices/iio:deviceN -> .../i2c-client/iio/
        # iio:deviceN, and the driver symlink is a sibling of the iio/ dir.
        client = self.tree.mkdir("i2c/10EC5280:00")
        dev = self.tree.mkdir("i2c/10EC5280:00/iio:device0")
        driver = self.tree.mkdir("drivers/bmi260_i2c")
        (client / "driver").symlink_to(driver)
        self.assertEqual(imu.driver_of(str(dev)), "bmi260_i2c")

    def test_sample_once(self):
        self.assertEqual(imu.sample_once(str(self.iio)),
                         (10, -20, 1000, 1, 2, 3))
        (self.iio / "in_accel_y_raw").write_text("garbage")
        self.assertEqual(imu.sample_once(str(self.iio))[1], 0)


class RateMeasurementTest(unittest.TestCase):
    def test_measure_changes_with_a_fake_clock(self):
        values = iter([0, 1, 2, 3, 3, 4])
        clock = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5])
        with mock.patch.object(imu, "_read_int",
                               side_effect=lambda _p: next(values)), \
             mock.patch.object(imu.time, "perf_counter",
                               side_effect=lambda: next(clock)):
            hz, polls_hz, median_ms = imu.measure_changes(str("/x"), 0.25, "ch")
        self.assertGreater(hz, 0)
        self.assertGreater(polls_hz, 0)
        self.assertGreater(median_ms, 0)

    def test_measure_rate_zero_seconds(self):
        with mock.patch.object(imu, "measure_changes", return_value=(0.0, 0.0, 0.0)), \
             mock.patch.object(imu, "sample_once", return_value=(0,) * 6):
            summary = imu.measure_rate(str("/x"), 0.0)
        self.assertEqual(summary["est_rate_hz"], 0.0)
        self.assertEqual(summary["est_rate_from_interval_hz"], 0.0)
        self.assertEqual(set(summary["changes_per_s"]), set(imu.CH_A + imu.CH_G))

    def test_measure_rate_interval_maths(self):
        with mock.patch.object(imu, "measure_changes", return_value=(99.0, 500.0, 4.0)), \
             mock.patch.object(imu, "sample_once", return_value=(1,) * 6):
            summary = imu.measure_rate(str("/x"), 0.0)
        self.assertEqual(summary["est_rate_from_interval_hz"], 250.0)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.iio = make_iio(self.tree)

    def test_rate_without_imu_is_exit_2(self):
        args = mock.Mock(seconds=0.0)
        with mock.patch.object(imu, "find_iio", return_value=None), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(imu.cmd_rate(args), 2)

    def test_rate_warning_when_far_below_configured(self):
        args = mock.Mock(seconds=0.0, json=False)
        result = {"seconds": 0.0, "polls_per_s": 1.0, "changes_per_s": {},
                  "est_rate_hz": 1.0, "est_rate_from_interval_hz": 1.0,
                  "median_interval_ms": 0.0}
        buf = io.StringIO()
        with mock.patch.object(imu, "find_iio", return_value=str(self.iio)), \
             mock.patch.object(imu, "measure_rate", return_value=result), \
             redirect_stdout(buf):
            self.assertEqual(imu.cmd_rate(args), 0)
        self.assertIn("WARN", buf.getvalue())
        self.assertIn("configured sampling frequency : 100.0 Hz", buf.getvalue())

    def test_rate_ok(self):
        args = mock.Mock(seconds=0.0, json=False)
        result = {"seconds": 0.0, "polls_per_s": 1.0, "changes_per_s": {},
                  "est_rate_hz": 99.0, "est_rate_from_interval_hz": 99.0,
                  "median_interval_ms": 0.0}
        buf = io.StringIO()
        with mock.patch.object(imu, "find_iio", return_value=str(self.iio)), \
             mock.patch.object(imu, "measure_rate", return_value=result), \
             redirect_stdout(buf):
            self.assertEqual(imu.cmd_rate(args), 0)
        self.assertIn("OK", buf.getvalue())

    def test_watch_survives_a_garbled_channel(self):
        (self.iio / "in_accel_x_raw").write_text("garbage")
        args = mock.Mock(seconds=0.1)
        clock = iter([0.0, 0.0, 1.0])
        buf = io.StringIO()
        with mock.patch.object(imu, "find_iio", return_value=str(self.iio)), \
             mock.patch.object(imu.time, "perf_counter",
                               side_effect=lambda: next(clock)), \
             mock.patch.object(imu.time, "sleep"), redirect_stdout(buf):
            self.assertEqual(imu.cmd_watch(args), 0)
        self.assertIn("peak |gyro|", buf.getvalue())
        self.assertGreaterEqual(buf.getvalue().count("0.00"), 1)

    def test_watch_without_imu_is_exit_2(self):
        with mock.patch.object(imu, "find_iio", return_value=None), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(imu.cmd_watch(mock.Mock(seconds=0.0)), 2)

    def test_info_without_imu_is_exit_2(self):
        with mock.patch.object(imu, "find_iio", return_value=None), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(imu.cmd_info(mock.Mock(json=False)), 2)

    def test_info_json_shape(self):
        buf = io.StringIO()
        with mock.patch.object(imu, "find_iio", return_value=str(self.iio)), \
             mock.patch.object(imu, "hhd_oxp_conf", return_value={}), \
             mock.patch.object(imu, "hidraw_steam_controller", return_value=[]), \
             redirect_stdout(buf):
            self.assertEqual(imu.cmd_info(mock.Mock(json=True)), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["device"], "10EC5280:00")
        self.assertEqual(data["trigger"], "hhd")
        self.assertEqual(data["hhd"]["motion_exposed"], False)
        self.assertEqual(data["sampling_hz"], {"accel": 100.0, "gyro": None})

    def test_version(self):
        self.assertEqual(imu.version(), "1.0")


if __name__ == "__main__":
    unittest.main()
