#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3-imu.py - read-only IMU (Bosch BMI260 on ACPI 10EC5280) diagnostics for the OneXPlayer 3.

The IMU is an IIO device (`i2c-10EC5280:00`, driver `bmi260_core`, label
`accel-display`).  HHD already streams it at `imu_hz` (default 100 Hz) through
an hrtimer trigger called `hhd` and feeds the samples into its emulated
controller (`hori_steam` / `dualsense` modes sync gyro into the HID reports,
`uinput` mode does not).  This tool only *observes*: it reads world-readable
IIO sysfs attributes and HHD's read-only API socket, and never writes a
trigger, a buffer, a module parameter or a config file.

Modes
  info      device, scales, mount matrix, sampling rate, trigger, HHD state, verdict
  rate      effective sensor update rate measured by polling the raw channels
  watch     live accel/gyro in SI units (good for checking axis signs)
  selftest  pass/fail checks for "is the IMU usable and exposed to Steam"

Examples
  tools/oxp3-imu.py info
  tools/oxp3-imu.py rate -s 3
  tools/oxp3-imu.py watch -s 10
  tools/oxp3-imu.py selftest

No root needed.  Exit code 0 = ok / selftest pass, 1 = selftest failure,
2 = IMU not found.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import statistics
import sys
import time

IIO = "/sys/bus/iio/devices"
CH_A = ("in_accel_x_raw", "in_accel_y_raw", "in_accel_z_raw")
CH_G = ("in_anglvel_x_raw", "in_anglvel_y_raw", "in_anglvel_z_raw")
HHD_SOCKET = "/run/hhd/api"
HHD_TIMEOUT = 2.0
G = 9.80665
RAD2DEG = 57.29577951308232

OK, WARN, FAIL, INFO = "OK  ", "WARN", "FAIL", "INFO"


# ---------------------------------------------------------------- helpers
def read(path: str, default: str | None = None) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_float(path: str) -> float | None:
    v = read(path)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def find_iio() -> str | None:
    """Return the sysfs path of the IMU IIO device, if present."""
    try:
        names = sorted(os.listdir(IIO))
    except OSError:
        return None
    candidates = []
    for name in names:
        if not name.startswith("iio:device"):
            continue
        path = os.path.join(IIO, name)
        dev = read(os.path.join(path, "name")) or ""
        label = read(os.path.join(path, "label")) or ""
        if "10EC5280" in dev or label == "accel-display":
            return path
        if dev.startswith("i2c-") and read(os.path.join(path, "in_anglvel_x_raw")):
            candidates.append(path)
    return candidates[0] if candidates else None


def driver_of(iio: str) -> str:
    dev = os.path.dirname(os.path.realpath(iio))
    link = os.path.join(dev, "driver")
    try:
        return os.path.basename(os.readlink(link))
    except OSError:
        return "?"


def i2c_client(iio: str) -> str:
    return read(os.path.join(iio, "name")) or "?"


def hidraw_steam_controller() -> list[str]:
    """hidraw nodes whose HID name is HHD's emulated Steam Controller."""
    out = []
    base = "/sys/class/hidraw"
    try:
        nodes = sorted(os.listdir(base))
    except OSError:
        return out
    for n in nodes:
        uevent = read(os.path.join(base, n, "device", "uevent")) or ""
        if "Steam Controller" in uevent or "000028DE" in uevent.upper():
            out.append(n)
    return out


def hhd_state() -> dict | None:
    """GET /api/v1/state from HHD's unix socket (read-only)."""
    if not os.path.exists(HHD_SOCKET):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(HHD_TIMEOUT)
    try:
        s.connect(HHD_SOCKET)
        s.sendall(
            b"GET /api/v1/state HTTP/1.1\r\nHost: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError:
        return None
    finally:
        s.close()
    try:
        head, _, body = buf.partition(b"\r\n\r\n")
        if b" 200 " not in head.split(b"\r\n", 1)[0]:
            return None
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def hhd_oxp_conf() -> dict:
    st = hhd_state() or {}
    return (st.get("controllers") or {}).get("oxp") or {}


def motion_exposed(hhd: dict) -> tuple[bool, str]:
    """True when the selected emulated controller can carry gyro to Steam."""
    cm = hhd.get("controller_mode") or {}
    mode = cm.get("mode", "?")
    if mode in ("hori_steam", "sd"):
        sync = (cm.get("hori_steam") or {}).get("sync_gyro", True)
        return bool(sync), f"{mode} (sync_gyro={sync})"
    if mode == "dualsense":
        sync = (cm.get("dualsense") or {}).get("sync_gyro", True)
        return bool(sync), f"dualsense (sync_gyro={sync})"
    return False, f"{mode} (no motion in this mode)"


def version() -> str:
    return "1.0"


# ---------------------------------------------------------------- info
def cmd_info(args) -> int:
    iio = find_iio()
    if iio is None:
        print(f"{FAIL} no IIO device with ACPI id 10EC5280 / label accel-display")
        print("     check: ls /sys/bus/iio/devices; dmesg | grep -i bmi260")
        return 2

    dev, drv = i2c_client(iio), driver_of(iio)
    label = read(os.path.join(iio, "label")) or "-"
    sa = read_float(os.path.join(iio, "in_accel_scale"))
    sg = read_float(os.path.join(iio, "in_anglvel_scale"))
    fa = read_float(os.path.join(iio, "in_accel_sampling_frequency"))
    fg = read_float(os.path.join(iio, "in_anglvel_sampling_frequency"))
    trig = read(os.path.join(iio, "trigger", "current_trigger")) or "none"
    buf = read(os.path.join(iio, "buffer", "enable")) or "?"
    trig_f = read(os.path.join(IIO, "trigger0", "sampling_frequency"))
    hhd = hhd_oxp_conf()
    exp, how = motion_exposed(hhd)

    if args.json:
        print(json.dumps({
            "device": dev, "driver": drv, "label": label, "sysfs": iio,
            "accel_scale_ms2_per_lsb": sa, "gyro_scale_rads_per_lsb": sg,
            "sampling_hz": {"accel": fa, "gyro": fg},
            "trigger": trig, "trigger_hz": trig_f, "buffer_enable": buf,
            "mount_matrix": read(os.path.join(iio, "in_mount_matrix")),
            "accel_available_hz": read(os.path.join(
                iio, "in_accel_sampling_frequency_available")),
            "gyro_available_hz": read(os.path.join(
                iio, "in_anglvel_sampling_frequency_available")),
            "hhd": {"imu": hhd.get("imu"), "imu_hz": hhd.get("imu_hz"),
                    "imu_axis_mode": (hhd.get("imu_axis") or {}).get("mode"),
                    "controller_mode": (hhd.get("controller_mode") or {}).get("mode"),
                    "motion_exposed": exp, "detail": how},
            "steam_controller_hidraw": hidraw_steam_controller(),
        }, indent=2))
        return 0

    print(f"oxp3-imu {version()} - Bosch BMI260 IMU on the OneXPlayer 3")
    print("=" * 66)
    print(f"IIO device      {dev}  (label {label})")
    print(f"kernel driver   {drv}")
    print(f"accel scale     {sa} m/s^2/LSB   (range "
          f"{read(os.path.join(iio, 'in_accel_scale_available'))})")
    print(f"gyro scale      {sg} rad/s/LSB   (range "
          f"{read(os.path.join(iio, 'in_anglvel_scale_available'))})")
    print(f"sampling        accel {fa} Hz, gyro {fg} Hz")
    print(f"                accel steps: "
          f"{read(os.path.join(iio, 'in_accel_sampling_frequency_available'))}")
    print(f"                gyro steps:  "
          f"{read(os.path.join(iio, 'in_anglvel_sampling_frequency_available'))}")
    print(f"mount matrix    {read(os.path.join(iio, 'in_mount_matrix'))}")
    print(f"trigger/buffer  current_trigger={trig} (hz={trig_f}) buffer_enable={buf}")
    print()
    print("HHD (reads /run/hhd/api, read-only):")
    if not hhd:
        print(f"  {WARN} HHD state unavailable - is hhd.service running?")
    else:
        print(f"  motion support (imu)   {hhd.get('imu')}")
        print(f"  motion hz (imu_hz)     {hhd.get('imu_hz')}")
        print(f"  axis mode              {(hhd.get('imu_axis') or {}).get('mode')}")
        print(f"  controller mode        {(hhd.get('controller_mode') or {}).get('mode')}")
        print(f"  gyro reaches Steam?    {'yes' if exp else 'no'} - {how}")
        hid = hidraw_steam_controller()
        if hid:
            print(f"  emulated HID           {', '.join('/dev/' + h for h in hid)}"
                  " (VID 28DE, Steam Controller)")
    print()
    if hhd and hhd.get("imu") and exp:
        print(f"{OK} IMU is usable today and its gyro is carried into the "
              "emulated Steam Controller (Steam Input sees motion).")
    elif hhd and hhd.get("imu"):
        print(f"{WARN} IMU is streamed but the current controller mode drops motion.")
        print("     pick 'hori_steam' or 'dualsense' in HHD -> Controllers -> "
              "OneXPlayer, or see docs/hw/camera-imu.md section 8.")
    else:
        print(f"{WARN} HHD motion is disabled (controllers.oxp.imu=false).")
    return 0


# ---------------------------------------------------------------- rate
def sample_once(iio: str) -> tuple[int, ...]:
    vals = []
    for ch in CH_A + CH_G:
        v = read(os.path.join(iio, ch), "0")
        try:
            vals.append(int(v))
        except ValueError:
            vals.append(0)
    return tuple(vals)


def _read_int(path: str) -> int:
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return 0


def measure_changes(iio: str, seconds: float,
                    channel: str) -> tuple[float, float, float]:
    """Poll one raw channel and measure its change rate.

    Returns (observed_hz, polls_per_s, median_interval_ms).  Polling sysfs
    does not consume the IIO buffer (HHD keeps its samples); it only observes
    when the latest converted value changes.  The observed rate is therefore a
    lower bound limited by how fast we can poll - one sysfs read costs an i2c
    transfer.
    """
    path = os.path.join(iio, channel)
    prev = _read_int(path)
    t0 = time.perf_counter()
    changes = 0
    last = t0
    intervals: list[float] = []
    polls = 0
    while time.perf_counter() - t0 < seconds:
        cur = _read_int(path)
        polls += 1
        now = time.perf_counter()
        if cur != prev:
            changes += 1
            intervals.append(now - last)
            last = now
            prev = cur
    dt = max(time.perf_counter() - t0, 1e-9)
    med = statistics.median(intervals) if intervals else 0.0
    return changes / dt, polls / dt, med * 1000.0


def measure_rate(iio: str, seconds: float) -> dict:
    est, polls, med = measure_changes(iio, seconds, "in_accel_x_raw")
    est_iv = (1000.0 / med) if med > 0 else 0.0
    # slower pass over every channel, only indicative (each poll reads 6 files)
    prev = sample_once(iio)
    t0 = time.perf_counter()
    changes = [0] * len(prev)
    n = 0
    while time.perf_counter() - t0 < min(seconds, 1.0):
        cur = sample_once(iio)
        n += 1
        for i, (a, b) in enumerate(zip(prev, cur)):
            if a != b:
                changes[i] += 1
        prev = cur
    dt = max(time.perf_counter() - t0, 1e-9)
    per_ch = {name: c / dt for name, c in zip(CH_A + CH_G, changes)}
    return {
        "seconds": seconds,
        "polls_per_s": polls,
        "changes_per_s": per_ch,
        "est_rate_hz": est,
        "est_rate_from_interval_hz": est_iv,
        "median_interval_ms": med,
    }


def cmd_rate(args) -> int:
    iio = find_iio()
    if iio is None:
        print(f"{FAIL} IMU not found")
        return 2
    configured = read_float(os.path.join(iio, "in_accel_sampling_frequency"))
    trig = read(os.path.join(iio, "trigger", "current_trigger")) or "none"
    r = measure_rate(iio, args.seconds)
    est_best = max(r["est_rate_hz"], r["est_rate_from_interval_hz"])
    if args.json:
        print(json.dumps({**r, "configured_hz": configured, "trigger": trig}, indent=2))
        return 0
    print(f"configured sampling frequency : {configured} Hz (trigger '{trig}')")
    print(f"measured over                 : {r['seconds']:.2f} s "
          f"({r['polls_per_s']:.0f} sysfs polls/s, single-channel estimate)")
    print(f"observed update rate (>=)    : {r['est_rate_hz']:.1f} Hz "
          "(change counting; 16-bit quantisation can hide repeats)")
    print(f"rate from median interval    : "
          f"{r['est_rate_from_interval_hz']:.1f} Hz "
          f"(median interval {r['median_interval_ms']:.2f} ms)")
    print("per-channel change rate (Hz)  : "
          + ", ".join(f"{n.replace('in_', '').replace('_raw', '')}={v:.1f}"
                      for n, v in r["changes_per_s"].items()))
    if configured and est_best < configured * 0.5:
        print(f"{WARN} measured rate is well below the configured rate - the "
              "sensor or the i2c bus may be stalled, or another reader is "
              "consuming the buffer.")
    else:
        print(f"{OK} samples are flowing")
    print("note: heavy buffered consumers (HHD) keep their own copy; this "
          "measurement is non-intrusive.")
    return 0


# ---------------------------------------------------------------- watch
def cmd_watch(args) -> int:
    iio = find_iio()
    if iio is None:
        print(f"{FAIL} IMU not found")
        return 2
    sa = read_float(os.path.join(iio, "in_accel_scale")) or 0.0
    sg = read_float(os.path.join(iio, "in_anglvel_scale")) or 0.0
    print(f"# accel m/s^2, gyro rad/s and deg/s; {args.seconds:.0f}s, "
          f"Ctrl-C to stop")
    print("# accel      ax      ay      az   |  gyro      gx      gy      gz"
          "   (deg/s)")
    t0 = time.perf_counter()
    peak = [0.0, 0.0, 0.0]
    try:
        while time.perf_counter() - t0 < args.seconds:
            # _read_int() turns a transient empty/garbled sysfs read into 0
            # instead of raising ValueError in the middle of the display.
            a = [_read_int(os.path.join(iio, c)) * sa for c in CH_A]
            g = [_read_int(os.path.join(iio, c)) * sg for c in CH_G]
            for i in range(3):
                peak[i] = max(peak[i], abs(g[i]))
            print(f"  {a[0]:8.2f}{a[1]:8.2f}{a[2]:8.2f}   |"
                  f"  {g[0]:7.3f}{g[1]:7.3f}{g[2]:7.3f}"
                  f"  ({(g[0]*RAD2DEG):6.1f}{(g[1]*RAD2DEG):6.1f}"
                  f"{(g[2]*RAD2DEG):6.1f})")
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    print(f"# peak |gyro| rad/s: x={peak[0]:.3f} y={peak[1]:.3f} z={peak[2]:.3f}"
          " (yaw the device to see which axis moves)")
    return 0


# ---------------------------------------------------------------- selftest
def cmd_selftest(args) -> int:
    failures = 0
    warns = 0
    iio = find_iio()
    print(f"oxp3-imu {version()} selftest")
    print("-" * 66)

    def check(ok: bool, text: str, warn_only: bool = False) -> None:
        nonlocal failures, warns
        tag = OK if ok else (WARN if warn_only else FAIL)
        if not ok:
            if warn_only:
                warns += 1
            else:
                failures += 1
        print(f"[{tag}] {text}")

    check(iio is not None, "IIO device present (ACPI 10EC5280 / label accel-display)")
    if iio is None:
        print("\nRESULT: FAIL (no IMU)")
        return 2

    drv = driver_of(iio)
    check(drv in ("bmi260_i2c", "bmi260_core", "bmi260"),
          f"kernel driver bound: {drv}")
    sa = read_float(os.path.join(iio, "in_accel_scale"))
    sg = read_float(os.path.join(iio, "in_anglvel_scale"))
    check(sa in (0.000598, 0.001197, 0.002394, 0.004788),
          f"accel scale {sa} m/s^2/LSB is a BMI260 range step")
    check(sg is not None and sg > 0, f"gyro scale {sg} rad/s/LSB is valid")
    check(bool(read(os.path.join(iio, "in_mount_matrix"))),
          "mount matrix provided (orientation usable for Steam)")

    r = measure_rate(iio, 1.0)
    est_best = max(r["est_rate_hz"], r["est_rate_from_interval_hz"])
    check(est_best >= 20, f"data is flowing: ~{est_best:.0f} Hz observed")
    cfg = read_float(os.path.join(iio, "in_accel_sampling_frequency"))
    if cfg:
        check(est_best > cfg * 0.5,
              f"observed rate >= half of configured {cfg:.0f} Hz", warn_only=True)

    a = [int(read(os.path.join(iio, c), "0") or 0) * (sa or 0) for c in CH_A]
    mag = sum(v * v for v in a) ** 0.5
    check(7.0 <= mag <= 12.5, f"accel magnitude {mag:.2f} m/s^2 ~= 1 g "
                              "(device at rest on a table)", warn_only=True)

    trig = read(os.path.join(iio, "trigger", "current_trigger")) or "none"
    buf = read(os.path.join(iio, "buffer", "enable"))
    check(trig != "none", f"buffered consumer attached: trigger '{trig}', "
                          f"buffer_enable={buf}", warn_only=True)

    hhd = hhd_oxp_conf()
    if not hhd:
        check(False, "HHD state readable via /run/hhd/api", warn_only=True)
    else:
        check(bool(hhd.get("imu")), f"HHD motion support enabled (imu="
                                    f"{hhd.get('imu')})")
        exp, how = motion_exposed(hhd)
        check(exp, f"HHD controller mode carries gyro to Steam: {how}")
        check((hhd.get("imu_hz") or 0) >= 100,
              f"HHD imu_hz={hhd.get('imu_hz')} (100 Hz default; 200/400 Hz "
              "possible)", warn_only=True)
        axis = (hhd.get("imu_axis") or {}).get("mode")
        check(axis == "default", f"HHD imu_axis mode '{axis}' (set by HHD from "
                                 "the device mapping)", warn_only=True)
        hid = hidraw_steam_controller()
        check(bool(hid), "emulated Steam Controller hidraw present"
                         + (f": /dev/{hid[0]}" if hid else ""), warn_only=True)

    if os.path.exists("/run/systemd/system"):
        active = os.system("systemctl is-active --quiet iio-sensor-proxy") == 0
        check(True, f"iio-sensor-proxy active={active} (shares the "
                    "accelerometer for orientation; harmless, mask it with "
                    "'sudo systemctl mask --now iio-sensor-proxy' if the gyro "
                    "ever stutters)", warn_only=True)

    print("-" * 66)
    if failures:
        print(f"RESULT: FAIL ({failures} failure(s), {warns} warning(s))")
        return 1
    print(f"RESULT: PASS ({warns} warning(s)) - IMU usable, gyro exposed to Steam")
    return 0


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="OneXPlayer 3 IMU (BMI260) diagnostics - read-only")
    ap.add_argument("-j", "--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("info", help="device + HHD/Steam exposure (default)")
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("rate", help="measure the effective sensor update rate")
    p.add_argument("-s", "--seconds", type=float, default=2.0)
    p.set_defaults(func=cmd_rate)
    p = sub.add_parser("watch", help="live accel/gyro in SI units")
    p.add_argument("-s", "--seconds", type=float, default=10.0)
    p.set_defaults(func=cmd_watch)
    p = sub.add_parser("selftest", help="pass/fail usability checks")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        args = ap.parse_args(["info"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
