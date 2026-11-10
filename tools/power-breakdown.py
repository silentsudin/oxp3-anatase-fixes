#!/usr/bin/env python3
"""Power breakdown: platform total, package, and the "everything else" rest.

The RAPL energy files are root-only, so run this with sudo. It samples for N
seconds and separates:

    psys                     whole platform at the input (incl. VRM losses)
    package-0                the SoC package (core + uncore + dram)
    core / uncore / dram     RAPL subdomains of package-0
    rest = psys - package    display, SSD, WiFi/BT, USB, audio, fan, RGB,
                             regulators and everything else
    battery power_now        total draw when discharging (a second opinion)

It also snapshots the device state that can explain the rest (backlight,
fan RPM, NVMe power state, WiFi power save, USB runtime status, RGB config)
and writes JSON next to the Decky reports:

    sudo tools/power-breakdown.py --seconds 30 --label gaming
    sudo tools/power-breakdown.py --seconds 30 --label idle
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pwd
import subprocess
import sys
import time
from datetime import datetime, timezone


def report_dir() -> str:
    """~ of the human who ran sudo, so the JSON is readable without root."""
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    home = None
    try:
        home = pwd.getpwnam(user).pw_dir if user else None
    except KeyError:
        home = None
    if not home:
        home = os.path.expanduser("~")
    return os.path.join(home, "oxp3-reports")


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def read_int(path):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def rapl_zones():
    zones = {}
    for d in sorted(glob.glob("/sys/class/powercap/intel-rapl*")):
        name = read(os.path.join(d, "name"))
        if name:
            zones[d] = name
    return zones


def sample(zones, seconds):
    first = {}
    skipped = []
    for d in zones:
        e = read_int(os.path.join(d, "energy_uj"))
        if e is None:
            skipped.append(zones[d])
            continue
        first[d] = e
    if "psys" not in [zones[d] for d in first] or "package-0" not in [zones[d] for d in first]:
        return None, ("cannot read psys/package-0 energy_uj (run with sudo); skipped: %s"
                      % ", ".join(skipped or ["none"]))
    t0 = time.monotonic()
    time.sleep(seconds)
    dt = time.monotonic() - t0
    watts = {}
    for d, e0 in first.items():
        e1 = read_int(os.path.join(d, "energy_uj"))
        if e1 is None:
            continue
        watts[zones[d]] = round((e1 - e0) / dt / 1e6, 2)
    return watts, None


def nvme_state():
    out = {}
    for d in glob.glob("/sys/class/nvme/nvme*"):
        out[os.path.basename(d)] = {
            "model": read(os.path.join(d, "model")),
            "ps_max_latency_us": read("/sys/module/nvme_core/parameters/default_ps_max_latency_us"),
            "force_apst": read("/sys/module/nvme_core/parameters/force_apst"),
        }
    if subprocess.run(["sh", "-c", "command -v nvme"], capture_output=True).returncode == 0:
        try:
            ps = subprocess.run(["nvme", "get-feature", "-f", "0x02", "/dev/nvme0"], capture_output=True, text=True, timeout=5).stdout
            out.setdefault("nvme0", {})["power_state_feature"] = ps.strip().splitlines()[-1] if ps.strip() else ""
        except Exception:
            pass
    return out


def pci_runtime():
    rows = {}
    for d in glob.glob("/sys/bus/pci/devices/*"):
        name = os.path.basename(d)
        status = read(os.path.join(d, "power/runtime_status"))
        if status in (None, "active"):
            continue
        rows[name] = status
    return rows


def usb_runtime():
    rows = {}
    for d in glob.glob("/sys/bus/usb/devices/*"):
        ctrl = read(os.path.join(d, "power/control"))
        if ctrl is None:
            continue
        rows[os.path.basename(d)] = {
            "id": "%s:%s" % (read(os.path.join(d, "idVendor")), read(os.path.join(d, "idProduct"))),
            "control": ctrl,
            "runtime": read(os.path.join(d, "power/runtime_status")),
        }
    return rows


def wifi():
    out = {}
    for d in glob.glob("/sys/class/net/wl*"):
        iface = os.path.basename(d)
        ps = subprocess.run(["sh", "-c", "iw dev %s get power_save" % iface], capture_output=True, text=True).stdout.strip()
        out[iface] = {"state": read(os.path.join(d, "operstate")), "power_save": ps}
    return out


def rgb_state():
    try:
        out = subprocess.run(["hhdctl", "get"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    keys = ("rgb.handheld.mode.mode", "rgb.handheld.mode.solid.hue",
            "rgb.handheld.mode.solid.saturation", "rgb.handheld.mode.solid.brightness")
    return {k: v for k, v in (line.split("=", 1) for line in out.splitlines() if "=" in line) if k in keys}


def aspm_active(text):
    """The bracketed token in the ASPM policy file is the active policy."""
    if not text:
        return None
    for tok in text.replace(",", " ").split():
        if tok.startswith("[") and tok.endswith("]"):
            return tok[1:-1]
    return None


def snapshot():
    bl = glob.glob("/sys/class/backlight/*")
    backlight = None
    if bl:
        b = bl[0]
        backlight = {"brightness": read_int(os.path.join(b, "brightness")),
                     "max": read_int(os.path.join(b, "max_brightness"))}
    fan = None
    try:
        sensors = subprocess.run(["sensors"], capture_output=True, text=True, timeout=5).stdout
        for line in sensors.splitlines():
            if line.startswith("fan1:"):
                fan = line.split(":", 1)[1].split()[0]
                break
    except Exception:
        pass
    aspm_text = read("/sys/module/pcie_aspm/parameters/policy")
    return {
        "aspm_policy": aspm_text,
        "aspm_active": aspm_active(aspm_text),
        "backlight": backlight,
        "fan_rpm": fan,
        "nvme": nvme_state(),
        "pci_suspended": pci_runtime(),
        "usb": usb_runtime(),
        "wifi": wifi(),
        "rgb": rgb_state(),
    }


def nvme_ps_series(n: int):
    """Current NVMe power state over a few seconds (root: nvme admin passthrough)."""
    if subprocess.run(["sh", "-c", "command -v nvme"], capture_output=True).returncode != 0:
        return []
    states = []
    for _ in range(n):
        try:
            out = subprocess.run(["nvme", "get-feature", "-f", "0x02", "/dev/nvme0"],
                                 capture_output=True, text=True, timeout=5).stdout
            line = out.strip().splitlines()[-1] if out.strip() else ""
            if "Current value:" in line:
                val = line.split("Current value:")[-1].strip()
                if val and not val.lower().startswith("0x"):
                    val = "0x" + val
                states.append(val)
        except Exception:
            return states
        time.sleep(1)
    return states


def battery_energy():
    """(energy_wh, status, monotonic) for the first discharging-capable battery."""
    for d in glob.glob("/sys/class/power_supply/BAT*"):
        e = read_int(os.path.join(d, "energy_now"))
        if e is None:
            continue
        return (e / 1e6, read(os.path.join(d, "status")) or "", time.monotonic())
    return None


def battery():
    out = {}
    for d in glob.glob("/sys/class/power_supply/BAT*"):
        out[os.path.basename(d)] = {
            "status": read(os.path.join(d, "status")),
            "energy_now_wh": (read_int(os.path.join(d, "energy_now")) or 0) / 1e6,
            "power_now_w": (read_int(os.path.join(d, "power_now")) or 0) / 1e6,
        }
    for d in glob.glob("/sys/class/power_supply/ADP*"):
        out["ac_online"] = read(os.path.join(d, "online"))
    return out


def hints(watts, snap):
    notes = []
    if watts.get("psys") and watts.get("package-0") and watts["psys"] < watts["package-0"] - 0.5:
        notes.append("psys reads below package-0 here: that RAPL domain is not the platform input on this machine; "
                     "the rest figure comes from battery power_now (run on battery)")
    bl = snap.get("backlight") or {}
    if bl.get("brightness") and bl.get("max"):
        pct = 100.0 * bl["brightness"] / bl["max"]
        notes.append("backlight at %.0f%% of max: panel power scales with this on an OLED" % pct)
    if "pcie_aspm_policy" in snap:
        pass
    if snap.get("aspm_active") == "default":
        notes.append("PCIe ASPM is active at 'default' (firmware); a powersave A/B is possible but sub-W and needs identical load")
    if snap.get("fan_rpm"):
        notes.append("fan at %s RPM: EC/fan curve, not a load indicator on its own" % snap["fan_rpm"])
    ac = bat.get("ac_online")
    status = (bat.get("BAT0") or {}).get("status")
    if ac == "1" and status != "Discharging":
        notes.append("on AC: battery power_now is 0 and any 'total' here excludes the adapter; "
                     "a USB-C meter's 20 W would include charging and VRM losses")
    elif status == "Charging":
        notes.append("battery is CHARGING: wall/input power = system + charge power; do not read it as system draw")
    usb = snap.get("usb") or {}
    active = [v for v in usb.values() if v.get("runtime") == "active" and not v.get("id", "").startswith("1d6b:")]
    if active:
        notes.append("%d USB device(s) runtime-active (keyboard receiver / vendor HID / pad)" % len(active))
    return notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--label", default="run")
    args = ap.parse_args()
    zones = rapl_zones()
    if not zones:
        print("no RAPL zones found", file=sys.stderr)
        return 3
    bat_before = battery_energy()
    watts, err = sample(zones, args.seconds)
    bat_after = battery_energy()
    if err:
        print("power-breakdown: " + err, file=sys.stderr)
        return 3
    snap = snapshot()
    bat = battery()
    total_w = None
    if bat_before is not None and bat_after is not None and bat_after[1] == "Discharging":
        dt = max(bat_after[2] - bat_before[2], 1e-6)
        # energy_now is Wh, dt is seconds: convert to W.
        total_w = round((bat_before[0] - bat_after[0]) * 3600.0 / dt, 2)
    bat_now = battery()
    pn = [v.get("power_now_w") for v in bat_now.values()
          if isinstance(v, dict) and v.get("power_now_w")]
    battery_power_now = round(sum(pn), 2) if pn else None
    psys = watts.get("psys")
    package = watts.get("package-0")
    psys_invalid = psys is not None and package is not None and psys < package - 0.5
    rep = {
        "schema": "oxp3-power-breakdown/1",
        "battery_total_w": total_w,
        "battery_power_now_w": battery_power_now,
        "psys_invalid": psys_invalid,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "label": args.label,
        "seconds": args.seconds,
        "watts": watts,
        "rest_psys_minus_package": (None if psys_invalid or psys is None or package is None
                                    else round(psys - package, 2)),
        "rest_battery_minus_package": (None if (battery_power_now or total_w) is None or package is None
                                       else round((battery_power_now or total_w) - package, 2)),
        "gpu_est_w": (None if package is None else
                      round(package - sum(watts.get(k, 0) for k in ("core", "uncore", "dram")), 2)),
        "battery": bat,
        "snapshot": snap,
        "nvme_power_states": nvme_ps_series(8),
        "hints": hints(watts, snap),
    }
    os.makedirs(report_dir(), exist_ok=True)
    path = os.path.join(report_dir(), "power-%s-%s.json" % (args.label, datetime.now().strftime("%Y%m%dT%H%M%S")))
    with open(path, "w") as f:
        json.dump(rep, f, indent=2)
    print("== power breakdown (%ss, %s)" % (args.seconds, args.label))
    for k in ("psys", "package-0", "core", "uncore", "dram"):
        if k in watts:
            print("  %-14s %6.2f W" % (k, watts[k]))
    if rep["gpu_est_w"] is not None:
        print("  %-14s %6.2f W  (package - core - uncore - dram)" % ("gpu (est)", rep["gpu_est_w"]))
    if rep["psys_invalid"]:
        print("  psys is IMPLAUSIBLE on this platform (below package): ignore psys/rest-from-psys")
    if total_w is not None:
        print("  %-14s %6.2f W  (energy delta; coarse, BMS quantised)" % ("battery total", total_w))
    if battery_power_now is not None:
        print("  %-14s %6.2f W  (power_now, used for rest)" % ("battery", battery_power_now))
    if rep["rest_battery_minus_package"] is not None:
        print("  %-14s %6.2f W  (battery - package)" % ("rest", rep["rest_battery_minus_package"]))
    ps = rep["nvme_power_states"]
    if ps:
        print("  nvme PS samples: %s" % ", ".join(str(x) for x in ps))
    for b, v in bat.items():
        if isinstance(v, dict) and v.get("power_now_w"):
            print("  battery %s   %6.2f W (%s)" % (b, v["power_now_w"], v.get("status")))
    for h in rep["hints"]:
        print("  - " + h)
    print("  report: " + path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
