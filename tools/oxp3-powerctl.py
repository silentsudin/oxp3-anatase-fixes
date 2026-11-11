#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3-powerctl - power envelope and fan control for the OneXPlayer 3 ("OXP3").

Everything this tool does is userspace and reversible: it writes the same
interfaces the kernel already exposes (powercap sysfs for the RAPL package
power limits, hwmon sysfs for the oxpec fan) and never touches MSRs, MMIO or
the EC directly.  No kernel patch is needed for any *active* control it
offers; one thing it cannot do is program the three vendor fan curves that
live in EC RAM (see docs/hw/power-fan.md).

Why these files
---------------
The vendor's OneXConsole (0.10.4) ships
``resources/resources/tools/intel/perf_conf.ini`` with the exact per-profile
values, which PerfMode.exe then writes through MSR + MMIO (its
``CCHWAPIExt.sys`` only exists because Windows has no powercap):

    [Quiet]      pl1msr=10  pl1mmio=10  pl2msr=25  pl2mmio=25  pl1taumsr/mmio=56  pl4=90   fanSpeed=0
    [Balanced]   pl1msr=25  pl1mmio=25  pl2msr=45  pl2mmio=45  pl1taumsr/mmio=56  pl4=160  fanSpeed=0
    [Performance]pl1msr=35  pl1mmio=35  pl2msr=80  pl2mmio=80  pl1taumsr/mmio=56  pl4=239  fanSpeed=200

Linux exposes both interfaces as separate powercap zones, ``intel-rapl:0``
(MSR) and ``intel-rapl-mmio:0`` (MMIO), each with writable
``constraint_N_power_limit_uw`` (long_term = PL1, short_term = PL2,
peak_power = PL4) and ``constraint_0_time_window_us``.  This box reports

    intel-rapl:0        PL1 35 W / tau 27.98 s / PL2 37 W / PL4 122 W
    intel-rapl-mmio:0   PL1 35 W / tau 27.98 s / PL2 52 W / PL4 122 W

The fan is ``hwmon6`` (``oxp_ec``, driver drivers/platform/x86/oxpec.c):
``fan1_input`` (rpm), ``pwm1`` (0-255, scaled by the driver to the EC's
0-184 for this board = ``oxp_g1_i``), ``pwm1_enable`` (0 = full speed,
1 = manual, 2 = EC automatic).  PWM only takes effect when
``/sys/devices/platform/oxp-platform/tt_toggle`` is 1 (the driver's turbo
button takeover); hhd normally sets that.

Privileges
----------
Writes need root (all these files are 0644 root:root and the kernel checks
CAP_SYS_ADMIN via sysfs).  Reads are world-readable; the only privileged
read it uses is the writability probe (open(O_WRONLY) without writing).

Exit codes: 0 ok, 2 a control is missing/not permitted (or nothing to do),
3 readback verification failed, 4 partial (some controls unavailable but
the tool was told to continue).

Examples
--------
    tools/oxp3-powerctl.py status
    tools/oxp3-powerctl.py status --json
    sudo tools/oxp3-powerctl.py profile performance --save
    sudo tools/oxp3-powerctl.py setpl1 25 --tau 56ms
    sudo tools/oxp3-powerctl.py setpl4 160
    sudo tools/oxp3-powerctl.py fan speed 180
    sudo tools/oxp3-powerctl.py fan curve --file /etc/oxp3/fan-curve
    sudo tools/oxp3-powerctl.py snapshot --save       # rollback baseline
    sudo tools/oxp3-powerctl.py restore-baseline
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import os
import re
import signal
import sys
import time

VERSION = "1.0"

POWERCAP = "/sys/class/powercap"
HWMON = "/sys/class/hwmon"
PLATFORM = "/sys/devices/platform/oxp-platform"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
STATE_DIR = "/etc/oxp3"
STATE_FILE = os.path.join(STATE_DIR, "power.json")
BASELINE_FILE = os.path.join(STATE_DIR, "power-baseline.json")
CURVE_FILE = os.path.join(STATE_DIR, "fan-curve")

# Human-facing names for the three powercap constraints, in the order the
# vendor ini uses them.  The kernel calls them long_term / short_term /
# peak_power; Intel calls the same things PL1 / PL2 / PL4.
LEVELS = (("pl1", "long_term"), ("pl2", "short_term"), ("pl4", "peak_power"))

# Vendor presets (perf_conf.ini, OneXConsole 0.10.4).  tau_s is the vendor's
# ``pl1taumsr/pl1taummio=56`` read as seconds: the ini does not state a unit,
# and only a multi-second window leaves the vendor's PL2=80/160/239 W any room
# to burst above PL1 (see docs/hw/power-fan.md section 2.4).  Profiles leave
# the window alone unless --tau/--with-tau is given because of that ambiguity.
# fan is the vendor fanSpeed on a 0-255 scale (0 = "leave the EC alone").
PROFILES = {
    "quiet": {
        "pl1": 10.0, "pl2": 25.0, "pl4": 90.0, "tau_s": 56,
        "fan": 0, "sagv": 3, "platform_profile": "low-power",
    },
    "balanced": {
        "pl1": 25.0, "pl2": 45.0, "pl4": 160.0, "tau_s": 56,
        "fan": 0, "sagv": 0, "platform_profile": "balanced",
    },
    "performance": {
        "pl1": 35.0, "pl2": 80.0, "pl4": 239.0, "tau_s": 56,
        "fan": 200, "sagv": 4, "platform_profile": "performance",
    },
}
PROFILE_ALIASES = {"perf": "performance", "low": "quiet", "eco": "quiet"}

# Default fan curve: CPU package temperature (degC) -> pwm (0-255).
# Deliberately gentle: 40 degC = 60, 90 degC = 255, quiet until ~45 degC.
DEFAULT_CURVE = [(40, 60), (55, 100), (70, 160), (85, 220), (95, 255)]

# Powers of ten for --tau parsing.
TAU_UNITS = {
    "s": 1e6, "sec": 1e6,
    "ms": 1e3,
    "us": 1.0, "µs": 1.0, "usec": 1.0,
}

W10 = 0.125  # power unit on this box: 1e6 >> 3 uW = 0.125 W


class ControlError(Exception):
    """A control is missing, unreadable or not writable."""

    def __init__(self, message, *, path=None, errno=None, hint=None):
        super().__init__(message)
        self.path = path
        self.errno = errno
        self.hint = hint


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError as exc:
        raise ControlError(f"cannot read {path}: {exc.strerror}", path=path, errno=exc.errno) from exc


def write_text(path, value):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(value))
    except OSError as exc:
        hint = None
        if exc.errno == 13:  # EACCES
            if os.geteuid() != 0:
                hint = "run this command as root (sudo)"
            else:
                hint = ("the kernel or the BIOS locked this limit "
                        "(intel_rapl returns -EACCES when the RAPL lock bit is set)")
        raise ControlError(f"cannot write {value!r} to {path}: {exc.strerror}",
                           path=path, errno=exc.errno, hint=hint) from exc


def probe_writable(path):
    """Open(O_WRONLY) but never write: tells whether a sysfs file would accept
    a write from the current uid (root) without changing any state."""
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        return False
    else:
        os.close(fd)
        return True


def fmt_w(uw):
    return f"{uw / 1_000_000:.3f} W"


def write_platform_profile(name, *, dry_run=False):
    """Optional ACPI platform_profile write; never fatal.

    Shared by `profile --platform-profile` and oxp3-adaptivetdp so both go
    through one place.  Returns a dict the caller can log.
    """
    if dry_run:
        return {"set": name, "dry_run": True}
    try:
        write_text(PLATFORM_PROFILE, name)
        return {"set": name, "actual": read_text(PLATFORM_PROFILE)}
    except ControlError as exc:
        return {"error": str(exc), "hint": exc.hint}


def fmt_tau(us):
    if us >= 1_000_000:
        return f"{us / 1_000_000:.3f} s"
    if us >= 1_000:
        return f"{us / 1_000:.3f} ms"
    return f"{us} us"


# --------------------------------------------------------------------------- #
# powercap
# --------------------------------------------------------------------------- #

class Domain:
    """One RAPL package zone (MSR or MMIO) with its constraints."""

    def __init__(self, path):
        self.path = path
        self.name = read_text(os.path.join(path, "name"))
        self.kind = "mmio" if "-mmio" in os.path.basename(path) else "msr"
        self.constraints = {}  # level -> dict(index, name, limit, window)
        for idx in range(5):
            name_path = os.path.join(path, f"constraint_{idx}_name")
            if not os.path.exists(name_path):
                break
            cname = read_text(name_path)
            for level, want in LEVELS:
                if cname == want:
                    self.constraints[level] = {
                        "index": idx,
                        "name": cname,
                        "limit": os.path.join(path, f"constraint_{idx}_power_limit_uw"),
                        "window": os.path.join(path, f"constraint_{idx}_time_window_us"),
                    }
        self.enabled_path = os.path.join(path, "enabled")

    @property
    def label(self):
        return "MMIO" if self.kind == "mmio" else "MSR"

    def enabled(self):
        try:
            return read_text(self.enabled_path) == "1"
        except ControlError:
            return None

    def get(self, level):
        c = self.constraints.get(level)
        if not c:
            return None
        try:
            return int(read_text(c["limit"]))
        except (ControlError, ValueError):
            # A truncated/garbled sysfs attribute is unreadable, not fatal.
            return None

    def get_window(self, level="pl1"):
        c = self.constraints.get(level)
        if not c:
            return None
        try:
            return int(read_text(c["window"]))
        except (ControlError, ValueError):
            return None

    def set(self, level, uw):
        c = self.constraints.get(level)
        if not c:
            raise ControlError(
                f"PL{level[-1]} ({c and c['name'] or level}) is not exposed in {self.path}",
                path=self.path,
                hint="this kernel's intel_rapl zone has no such constraint",
            )
        write_text(c["limit"], int(round(uw)))

    def set_window(self, uw, level="pl1"):
        c = self.constraints.get(level)
        if not c:
            raise ControlError(f"no PL1 constraint in {self.path}", path=self.path)
        write_text(c["window"], int(round(uw)))


def discover_domains():
    """All RAPL package-0 zones, MSR first.  Subzones (core/uncore/dram) are
    skipped: they have no writable package limit and are not what the vendor
    presets program."""
    domains = []
    for path in sorted(glob.glob(os.path.join(POWERCAP, "intel-rapl*"))):
        if not os.path.isdir(path):
            continue
        try:
            name = read_text(os.path.join(path, "name"))
        except ControlError:
            continue
        if name != "package-0":
            continue
        try:
            domains.append(Domain(path))
        except ControlError:
            continue
    domains.sort(key=lambda d: d.kind)  # "mmio" < "msr" alphabetically
    # MSR first: the vendor writes the MSR mirror first as well.
    domains.sort(key=lambda d: 0 if d.kind == "msr" else 1)
    return domains


def require_domains(levels=None, allow_partial=False):
    domains = discover_domains()
    if not domains:
        raise ControlError(
            "no intel-rapl package-0 zone found",
            hint="intel_rapl_msr is not loaded, or this is not an Intel RAPL machine",
        )
    have = {level for d in domains for level in d.constraints}
    wanted = [level for level, _ in LEVELS] if levels is None else list(levels)
    missing = [level for level in wanted if level not in have]
    if missing and not allow_partial:
        raise ControlError(
            "powercap does not expose " + ", ".join(m.upper() for m in missing),
            hint="re-run with --allow-partial to write only the limits that exist",
        )
    return domains, missing


def check_root(what="changing the power limits or fan speed"):
    if os.geteuid() != 0:
        raise ControlError(
            f"{what} needs root",
            hint="run with sudo; every control lives in a 0644 root-owned sysfs file",
        )


def apply_limits(domains, values, *, dry_run=False, allow_partial=False):
    """Write {level: watts} to every domain, ordering the writes by dependency.

    The invariant (docs/UNIT-TESTS.md section 5) is that a lowering write must
    never precede a raising one: raising a floor needs the ceiling open first,
    and lowering a ceiling needs the floor closed first.  So every raise is
    emitted before *any* lowering, per zone: raises highest-ceiling-first
    (PL4 -> PL2 -> PL1), then lowers lowest-floor-first (PL1 -> PL2 -> PL4).
    Direction is decided per (domain, level) against the value read from that
    same domain, so one level may raise on the MSR zone while it lowers on the
    MMIO one without either zone getting a bad order.  An unreadable current
    value is treated as a raise (open the ceiling before touching anything).

    Each planned entry records ``before_uw``/``direction``: the pre-write value
    and the decision derived from it.  Auditors (tools/oxp3-verify.sh pwr.order)
    re-check the plan against that recorded state, not against a separately
    sampled one that a live daemon may have changed in between.
    """
    report = {"written": [], "skipped": [], "errors": [], "readback": []}
    idx = {"pl1": 0, "pl2": 1, "pl4": 2}
    plan = []
    for domain_index, domain in enumerate(domains):
        for level, _ in LEVELS:
            if level not in values:
                continue
            if level not in domain.constraints:
                report["skipped"].append({"domain": domain.path, "level": level,
                                          "why": "not exposed"})
                continue
            want = int(round(values[level] * 1_000_000))
            before = domain.get(level)
            if before is None or want > before:
                phase, direction = 0, "raise"
            elif want < before:
                phase, direction = 1, "lower"
            else:
                phase, direction = 2, "same"
            # raises: PL4(-2), PL2(-1), PL1(0); lowering/unchanged: PL1, PL2, PL4
            rank = -idx[level] if phase == 0 else idx[level]
            plan.append((phase, rank, domain_index, domain, level, want, before,
                         direction))
    # A flat sort keeps every domain's subsequence raises-then-lowers, while
    # also never letting a lowering on one zone precede a raise on another.
    plan.sort(key=lambda entry: (entry[0], entry[1], entry[2]))

    for _phase, _rank, _di, domain, level, uw, before, direction in plan:
        entry = {"domain": domain.path, "level": level, "value_uw": uw,
                 "before_uw": before, "direction": direction}
        if dry_run:
            entry["dry_run"] = True
            report["written"].append(entry)
            continue
        try:
            domain.set(level, uw)
        except ControlError as exc:
            report["errors"].append({"domain": domain.path, "level": level,
                                     "error": str(exc), "hint": exc.hint})
            continue
        report["written"].append(entry)

    if dry_run:
        return report

    # Readback verification: the kernel quantises to its power unit
    # (0.125 W here) and may clamp/reject a value, so verify rather than trust.
    # Only pairs whose write succeeded are verified: a failed write would
    # otherwise also read back the old value and be misreported as a readback
    # mismatch (exit 3) instead of a refused control (exit 2).
    failed = {(e.get("domain"), e.get("level")) for e in report["errors"]}
    for domain in domains:
        for level, _ in LEVELS:
            if level not in values or level not in domain.constraints:
                continue
            if (domain.path, level) in failed:
                continue
            actual = domain.get(level)
            want = int(round(values[level] * 1_000_000))
            ok = actual is not None and abs(actual - want) <= max(2 * 125_000, abs(want) // 1000)
            report["readback"].append({
                "domain": domain.path, "level": level,
                "want_uw": want, "actual_uw": actual, "ok": ok,
            })
            if not ok:
                report["errors"].append({
                    "domain": domain.path, "level": level,
                    "error": f"readback mismatch: wanted {fmt_w(want)}, reads "
                             f"{fmt_w(actual) if actual is not None else 'unreadable'}",
                })
    return report


def parse_tau(text):
    """'56ms' / '5.6s' / '28000us' / bare number (= seconds) -> microseconds."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Zµ]*)\s*", str(text))
    if not m:
        raise ControlError(f"cannot parse time window {text!r}", hint="use e.g. 56ms, 5.6s, 28000us")
    value = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit not in TAU_UNITS:
        raise ControlError(f"unknown time unit {unit!r}", hint="s, ms or us")
    return int(round(value * TAU_UNITS[unit]))


# --------------------------------------------------------------------------- #
# fan (oxpec hwmon)
# --------------------------------------------------------------------------- #

class Fan:
    def __init__(self, path):
        self.path = path
        self.name = read_text(os.path.join(path, "name"))
        self.fan_input = os.path.join(path, "fan1_input")
        self.pwm = os.path.join(path, "pwm1")
        self.pwm_enable = os.path.join(path, "pwm1_enable")

    @property
    def available(self):
        return all(os.path.exists(p) for p in (self.pwm, self.pwm_enable))

    def rpm(self):
        try:
            return int(read_text(self.fan_input))
        except (ControlError, ValueError):
            return None

    def duty(self):
        try:
            return int(read_text(self.pwm))
        except (ControlError, ValueError):
            return None

    def mode(self):
        try:
            raw = read_text(self.pwm_enable)
        except ControlError:
            return "unavailable", None
        names = {"0": "full", "1": "manual", "2": "auto"}
        return names.get(raw, f"unknown({raw})"), raw

    def ec_duty(self, pwm=None):
        """oxpec scales pwm 0-255 to the EC's 0-184 for the ONEXPLAYER 3
        (board oxp_g1_i in oxp_pwm_input_write())."""
        value = self.duty() if pwm is None else pwm
        if value is None:
            return None
        return round(value * 184 / 255)

    def set_mode(self, mode):
        value = {"full": 0, "manual": 1, "auto": 2}[mode]
        write_text(self.pwm_enable, value)

    def set_duty(self, pwm):
        if not 0 <= int(pwm) <= 255:
            raise ControlError(f"pwm {pwm} outside 0-255")
        write_text(self.pwm, int(pwm))


def find_fan():
    for path in sorted(glob.glob(os.path.join(HWMON, "hwmon*"))):
        try:
            name = read_text(os.path.join(path, "name"))
        except ControlError:
            continue
        if name == "oxp_ec":
            return Fan(path)
    # Fallback: any hwmon with a pwm1 + fan1_input (the tool still explains
    # that the EC curve is not reachable).
    for path in sorted(glob.glob(os.path.join(HWMON, "hwmon*"))):
        if os.path.exists(os.path.join(path, "pwm1")) and os.path.exists(os.path.join(path, "fan1_input")):
            return Fan(path)
    return None


def tt_toggle_state():
    path = os.path.join(PLATFORM, "tt_toggle")
    if not os.path.exists(path):
        return None
    try:
        return read_text(path)
    except ControlError:
        return None


def fan_status(fan):
    mode, raw = fan.mode()
    return {
        "hwmon": fan.path,
        "name": fan.name,
        "rpm": fan.rpm(),
        "pwm": fan.duty(),
        "pwm_enable": raw,
        "mode": mode,
        "ec_duty_0_184": fan.ec_duty(),
        "tt_toggle": tt_toggle_state(),
    }


def apply_fan(fan, *, mode=None, duty=None, dry_run=False, allow_partial=False):
    out = {"actions": [], "warnings": [], "errors": [], "readback": {}}
    tt = tt_toggle_state()
    if tt is not None and tt != "1":
        out["warnings"].append(
            "tt_toggle=%s: PWM writes are ignored unless the turbo-button takeover is "
            "enabled (echo 1 | sudo tee %s)" % (tt, os.path.join(PLATFORM, "tt_toggle"))
        )
    if mode is not None:
        if dry_run:
            out["actions"].append({"set": "pwm1_enable", "value": mode})
        else:
            try:
                fan.set_mode(mode)
            except ControlError as exc:
                out["errors"].append({"set": "pwm1_enable", "value": mode,
                                      "error": str(exc), "hint": exc.hint})
    if duty is not None:
        if dry_run:
            out["actions"].append({"set": "pwm1", "value": int(duty)})
        else:
            try:
                # Manual mode is a prerequisite: in auto mode the EC owns pwm1.
                if mode not in ("manual", "full"):
                    fan.set_mode("manual")
                    out["actions"].append({"set": "pwm1_enable", "value": 1})
                fan.set_duty(duty)
                out["actions"].append({"set": "pwm1", "value": int(duty)})
            except ControlError as exc:
                out["errors"].append({"set": "pwm1", "value": int(duty),
                                      "error": str(exc), "hint": exc.hint})
    if not dry_run:
        out["readback"] = fan_status(fan)
        if duty is not None and out["readback"].get("pwm") is not None:
            if abs(int(out["readback"]["pwm"]) - int(duty)) > 1:
                out["errors"].append({
                    "set": "pwm1",
                    "error": f"readback mismatch: wanted {duty}, reads {out['readback']['pwm']}",
                })
    return out


def find_temp_sensor(explicit=None):
    """CPU package temperature: coretemp 'Package id 0' first, then TCPU, acpitz."""
    if explicit:
        return explicit
    for path in sorted(glob.glob(os.path.join(HWMON, "hwmon*"))):
        try:
            if read_text(os.path.join(path, "name")) != "coretemp":
                continue
        except ControlError:
            continue
        for label in sorted(glob.glob(os.path.join(path, "temp*_label"))):
            try:
                if read_text(label) == "Package id 0":
                    return label.replace("_label", "_input")
            except ControlError:
                continue
    for zone in ("TCPU", "TCPU_PCI", "acpitz", "x86_pkg_temp"):
        for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            try:
                if read_text(os.path.join(path, "type")) == zone:
                    return os.path.join(path, "temp")
            except ControlError:
                continue
    return None


def read_temp(path):
    if not path:
        return None
    try:
        raw = int(read_text(path))
    except (ControlError, ValueError):
        return None
    return raw / 1000.0 if abs(raw) > 1000 else float(raw)


def load_curve(path):
    if path and not os.path.exists(path):
        if path == CURVE_FILE:
            return list(DEFAULT_CURVE), "built-in default"
        raise ControlError(f"curve file {path} does not exist")
    if path:
        points = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    parts = re.split(r"[\s,:=]+", line)
                    if len(parts) < 2:
                        raise ControlError(f"bad curve line: {line!r}",
                                           hint="expected: <tempC> <pwm>")
                    points.append((float(parts[0]), float(parts[1])))
        except ControlError:
            raise
        except (OSError, ValueError) as exc:
            # `curve --file` is user input: report it like every other control
            # error instead of escaping as a traceback.
            raise ControlError(f"cannot parse curve file {path}: {exc}",
                               hint="expected: <tempC> <pwm>, one point per line") from exc
        points = [(t, int(round(pwm))) for t, pwm in points]
        if not points:
            raise ControlError(f"curve file {path} is empty")
        source = path
    else:
        points = list(DEFAULT_CURVE)
        source = "built-in default"
    points.sort()
    for temp, pwm in points:
        if not 0 <= pwm <= 255:
            raise ControlError(f"curve pwm {pwm} at {temp} degC outside 0-255")
    return points, source


def curve_pwm(points, temp):
    if temp is None:
        return None
    if temp <= points[0][0]:
        return points[0][1]
    if temp >= points[-1][0]:
        return points[-1][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return p1
            return int(round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0)))
    return points[-1][1]


def run_curve(fan, points, *, interval, steps, sensor, dry_run, restore_auto,
              quiet=False, hysteresis=3, source="?"):
    """Userspace fan curve: the EC curve itself is not programmable from Linux
    (see docs/hw/power-fan.md); this is a software approximation."""
    temp_path = find_temp_sensor(sensor)
    if not temp_path:
        raise ControlError("no CPU temperature sensor found",
                           hint="pass --sensor /sys/class/hwmon/hwmonN/temp1_input")
    if not quiet:
        print(f"curve: {source} ({len(points)} points), sensor {temp_path}, "
              f"interval {interval:g}s", file=sys.stderr)
    if not dry_run:
        check_root("the fan curve")
        if not fan:
            raise ControlError("no oxp_ec hwmon fan control found")
        fan.set_mode("manual")

    def _terminate(signum, frame):  # systemctl stop -> restore auto in finally
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    last = None
    step = 0
    try:
        while steps is None or step < steps:
            temp = read_temp(temp_path)
            pwm = curve_pwm(points, temp)
            if pwm is None:
                raise ControlError(f"cannot read {temp_path}")
            changed = last is None or abs(pwm - last) >= hysteresis
            if changed:
                if not quiet:
                    print(f"{time.strftime('%H:%M:%S')} {temp:5.1f} degC -> pwm {pwm:3d} "
                          f"(ec {round(pwm * 184 / 255):3d}/184)"
                          + ("  [dry-run]" if dry_run else ""), flush=True)
                if not dry_run:
                    fan.set_duty(pwm)
                last = pwm
            step += 1
            if steps is not None and step >= steps:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        if not quiet:
            print("\ninterrupted", file=sys.stderr)
    finally:
        if restore_auto and not dry_run:
            try:
                fan.set_mode("auto")
                if not quiet:
                    print("fan returned to automatic (pwm1_enable=2)", file=sys.stderr)
            except ControlError as exc:
                print(f"WARNING: could not restore automatic fan control: {exc}", file=sys.stderr)
    return {"ok": True, "steps": step, "last_pwm": last, "sensor": temp_path,
            "curve": source, "dry_run": dry_run}


# --------------------------------------------------------------------------- #
# battery: health reporting, charge limit, charge-inhibit ("bypass") 
# --------------------------------------------------------------------------- #
# The ACPI battery driver (drivers/acpi/battery.c) publishes the OEM's own
# charge-control knobs when the firmware implements them, and this box does:
#
#   charge_control_end_threshold   <- ACPI _BTP ("battery trip point")
#   charge_behaviour               <- ACPI _BCM charge-inhibit bits
#
# Both are mode 0644 root (so the kernel has a write handler; a read-only
# property would be 0444), which is why the charge limit and a good
# approximation of the vendor's "bypass power" are reachable from Linux with
# no kernel work -- see docs/hw/battery-tdp.md.
BATTERY_GLOB = "/sys/class/power_supply/BAT*"

# Vendor OneXConsole semantics recovered from app.asar i18n + CompatLayerCT
# strings, and how they map onto Linux:
#   battery_protection_chargelimit          "charge limit"  -> charge_control_end_threshold
#   battery_protection_bypasspower_effect_always             -> inhibit-charge
#   battery_protection_bypasspower_effect_when_screen_on     -> inhibit-charge-awake
#   battery_protection_normalpower                            -> auto
BYPASS_MODES = ("inhibit-charge", "inhibit-charge-awake")
BYPASS_ON_DEFAULT = "inhibit-charge"


def find_battery(explicit=None):
    """First BAT* whose type is Battery (BAT0 on this box)."""
    if explicit:
        return explicit
    for path in sorted(glob.glob(BATTERY_GLOB)):
        try:
            if read_text(os.path.join(path, "type")) == "Battery":
                return path
        except ControlError:
            continue
    return None


def probe_state(path):
    """Classify a sysfs control without touching any state.

    "absent"   - the kernel/BIOS does not expose it at all
    "read-only"- exposed 0444: reading works, writing is not implemented
    "root-only"- exposed 0644/0600: a write handler exists, needs root
    "writable" - this uid could write it right now
    """
    if not os.path.exists(path):
        return "absent"
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return "absent"
    if not mode & 0o222:
        return "read-only"
    return "writable" if os.access(path, os.W_OK) else "root-only"


def read_int(path):
    try:
        return int(read_text(path))
    except (ControlError, ValueError):
        return None


def parse_charge_behaviour(text):
    """'[auto] inhibit-charge inhibit-charge-awake' -> ('auto', [...])

    power_supply renders the selected mode in brackets.  Some drivers (and any
    plain-file stand-in) instead render or echo a single bare token, so fall
    back to the only token when there is exactly one -- otherwise a correct
    write would look like a readback mismatch.
    """
    modes, selected = [], None
    for token in (text or "").split():
        if token.startswith("[") and token.endswith("]"):
            selected = token[1:-1]
            modes.append(selected)
        else:
            modes.append(token)
    if selected is None and len(modes) == 1:
        selected = modes[0]
    return selected, modes


def battery_status(explicit=None):
    """Everything BAT0 says, plus the capability classification per control.

    health_pct is energy_full / energy_full_design, i.e. what the pack holds
    now versus its design figure. It reads >100 % on a new pack (this one is
    103.7 %) because the design number is a nominal, not a measured, value;
    trend it over months rather than reading one sample as capacity loss.
    """
    path = find_battery(explicit)
    info = {"path": path, "name": os.path.basename(path) if path else None,
            "present": False, "capabilities": {}, "warnings": []}
    if not path:
        info["warnings"].append("no /sys/class/power_supply/BAT* battery device")
        return info

    def get(attr):
        try:
            return read_text(os.path.join(path, attr))
        except ControlError:
            return None

    info["present"] = get("present") == "1"
    info["status"] = get("status")
    info["capacity_pct"] = read_int(os.path.join(path, "capacity"))
    info["capacity_level"] = get("capacity_level")
    info["manufacturer"] = get("manufacturer")
    info["model_name"] = get("model_name")
    info["technology"] = get("technology")

    energy_now = read_int(os.path.join(path, "energy_now"))
    energy_full = read_int(os.path.join(path, "energy_full"))
    energy_design = read_int(os.path.join(path, "energy_full_design"))
    info["energy_now_wh"] = None if energy_now is None else energy_now / 1e6
    info["energy_full_wh"] = None if energy_full is None else energy_full / 1e6
    info["energy_design_wh"] = None if energy_design is None else energy_design / 1e6
    if energy_full and energy_design:
        info["health_pct"] = round(100.0 * energy_full / energy_design, 1)
    else:
        info["health_pct"] = None

    power_now = read_int(os.path.join(path, "power_now"))
    info["power_now_w"] = None if power_now is None else power_now / 1e6
    volt = read_int(os.path.join(path, "voltage_now"))
    info["voltage_now_v"] = None if volt is None else volt / 1e6
    vmin = read_int(os.path.join(path, "voltage_min_design"))
    info["voltage_min_design_v"] = None if vmin is None else vmin / 1e6

    cycles = read_int(os.path.join(path, "cycle_count"))
    info["cycle_count"] = cycles
    if cycles == 0:
        info["warnings"].append(
            "cycle_count reads 0: the EC does not implement it, so wear has to be "
            "tracked from energy_full drift instead")

    # --- charge control capabilities -------------------------------------
    for attr in ("charge_control_end_threshold", "charge_control_start_threshold",
                 "charge_behaviour", "cycle_count"):
        info["capabilities"][attr] = probe_state(os.path.join(path, attr))

    end_path = os.path.join(path, "charge_control_end_threshold")
    info["charge_control_end_threshold"] = read_int(end_path)
    start_path = os.path.join(path, "charge_control_start_threshold")
    info["charge_control_start_threshold"] = read_int(start_path)

    beh_path = os.path.join(path, "charge_behaviour")
    selected, modes = parse_charge_behaviour(get("charge_behaviour"))
    info["charge_behaviour"] = selected
    info["charge_behaviour_available"] = modes
    # "bypass" is our name for the vendor feature; on Linux it is charge
    # inhibit, which is exactly what the vendor's own hint text describes:
    # "when plugged in, stop charging the battery, power the device only".
    info["bypass"] = selected in BYPASS_MODES
    info["bypass_vendor_equivalent"] = {
        "inhibit-charge": "bypass, always effective (vendor mode 2, EC 0xA4=0x03)",
        "inhibit-charge-awake": "bypass, ineffective while asleep/off (vendor mode 1, EC 0xA4=0x01)",
        "auto": "normal charging (vendor 'normal power supply')",
    }.get(selected)
    if info["capabilities"]["charge_behaviour"] == "absent":
        info["warnings"].append(
            "charge_behaviour is absent: this kernel/EC does not expose charge "
            "inhibit, so the vendor bypass mode is not reachable in userspace")
    elif "inhibit-charge" not in modes:
        info["warnings"].append(
            "charge_behaviour lists no inhibit-charge mode: bypass is not available")

    # ACPI battery hwmon sibling: on this kernel it is BAT0/hwmonN (a sibling
    # of the attributes, not BAT0/device/hwmon/), reporting mV + uW mirrors.
    try:
        for hw in glob.glob(os.path.join(path, "hwmon", "hwmon*")) + \
                glob.glob(os.path.join(path, "hwmon*")):
            if not os.path.isdir(hw):
                continue
            v = read_int(os.path.join(hw, "in0_input"))
            p = read_int(os.path.join(hw, "power1_input"))
            if v is not None:
                info["hwmon_voltage_v"] = v / 1e6
            if p is not None:
                info["hwmon_power_now_w"] = p / 1e6
    except OSError:
        pass

    ac = None
    for cand in sorted(glob.glob("/sys/class/power_supply/A*")):
        try:
            if read_text(os.path.join(cand, "type")) in ("Mains", "USB"):
                ac = {"name": os.path.basename(cand),
                      "online": read_text(os.path.join(cand, "online")) == "1"}
                break
        except ControlError:
            continue
    info["ac"] = ac
    return info


def set_charge_threshold(value, *, battery=None, dry_run=False):
    """Write charge_control_end_threshold and verify.

    value is a percentage 50-100 (or None for "no limit" = 100).  The kernel
    has no way to read back the EC's allowed range, so we validate the
    documented power_supply contract (0-100) and warn outside 50-100.
    """
    path = find_battery(battery)
    if not path:
        raise ControlError("no battery device",
                           hint="looked for /sys/class/power_supply/BAT*")
    target = os.path.join(path, "charge_control_end_threshold")
    state = probe_state(target)
    if state == "absent":
        raise ControlError(
            f"{target} does not exist",
            path=target,
            hint=("this kernel/EC does not expose a charge limit; ACPI _BTP is missing. "
                  "oxpec would need a charge-limit attribute backed by EC 0xA3 "
                  "(the vendor's EC_ADDR_CHARGE_LIMIT; see upstream/oxpec-ec-addresses.md)"))
    if value is not None and not 0 <= value <= 100:
        raise ControlError(f"charge limit {value} out of range", hint="use 50-100, or 'off'")
    if value is not None and not 50 <= value <= 100:
        print(f"oxp3-powerctl: warning: {value} % is outside the usual 50-100 % range",
              file=sys.stderr)
    if value is None:
        value = 100
    rep = {"path": target, "requested": value, "applied": None, "dry_run": dry_run,
           "capability": state}
    if dry_run:
        rep["applied"] = read_int(target)
        return rep
    if state == "read-only":
        raise ControlError(f"{target} is read-only on this kernel", path=target,
                           hint="the firmware exposes _BTP for reading only")
    if state == "root-only":
        raise ControlError(f"cannot write {target}: permission denied", path=target,
                           hint="run this command as root (sudo)")
    write_text(target, value)
    got = read_int(target)
    if got != value:
        raise ControlError(f"readback mismatch: wanted {value}, reads {got}", path=target)
    rep["applied"] = got
    return rep


def set_charge_bypass(on, *, battery=None, mode=None, dry_run=False):
    """Turn the vendor's 'bypass power' approximation on/off.

    on=True writes `inhibit-charge` (stop charging, run from the adapter);
    mode='inhibit-charge-awake' selects the vendor's screen-on-only variant.
    on=False writes `auto`.  The kernel prints the selection in brackets, so
    the readback compares the bracketed token, not the whole line.
    """
    path = find_battery(battery)
    if not path:
        raise ControlError("no battery device",
                           hint="looked for /sys/class/power_supply/BAT*")
    target = os.path.join(path, "charge_behaviour")
    state = probe_state(target)
    if state == "absent":
        raise ControlError(
            f"{target} does not exist", path=target,
            hint=("this kernel/EC exposes no charge-inhibit mode, so the vendor bypass "
                  "is not reachable. It needs ACPI _BCM bits or an oxpec attribute over "
                  "EC 0xA4 (the vendor's EC_ADDR_BYPASS_POWER, 0x00/0x01/0x03)"))
    current, available = parse_charge_behaviour(read_text(target))
    wanted = "auto" if not on else (mode or BYPASS_ON_DEFAULT)
    if on and wanted not in BYPASS_MODES:
        raise ControlError(f"{wanted!r} is not one of the bypass modes",
                           hint="use inhibit-charge or inhibit-charge-awake")
    # Only trust the advertised list when the driver actually renders one.  A
    # single-token rendering means "current value only", not "no other modes";
    # there the write itself decides (the kernel answers EINVAL for a mode it
    # does not implement).
    if len(available) > 1 and wanted not in available:
        raise ControlError(
            f"charge_behaviour does not offer {wanted!r}", path=target,
            hint="available: " + ", ".join(available))
    rep = {"path": target, "requested": wanted, "previous": current,
           "available": available, "applied": None, "dry_run": dry_run,
           "capability": state}
    if dry_run:
        rep["applied"] = current
        return rep
    if state == "read-only":
        raise ControlError(f"{target} is read-only on this kernel", path=target)
    if state == "root-only":
        raise ControlError(f"cannot write {target}: permission denied", path=target,
                           hint="run this command as root (sudo)")
    try:
        write_text(target, wanted)
    except ControlError as exc:
        if exc.errno == 22:  # EINVAL: this driver does not implement that mode
            raise ControlError(
                f"{target} does not support {wanted!r}", path=target,
                hint="available: " + (", ".join(available) or "unknown")) from exc
        raise
    got, _ = parse_charge_behaviour(read_text(target))
    if got != wanted:
        raise ControlError(f"readback mismatch: wanted {wanted}, reads {got}", path=target)
    rep["applied"] = got
    return rep


def print_battery(info):
    name = info.get("name") or "no battery"
    print(f"battery {name}" + ("" if info["present"] else "  (not present)"))
    if not info.get("path"):
        for warning in info["warnings"]:
            print(f"  warning: {warning}")
        return
    bits = []
    if info["status"]:
        bits.append(f"status {info['status']}")
    if info["capacity_pct"] is not None:
        bits.append(f"{info['capacity_pct']} % (level {info['capacity_level']})")
    if info["health_pct"] is not None:
        bits.append(f"health {info['health_pct']} %")
    print("  " + ", ".join(bits))
    energy = []
    for key, label in (("energy_now_wh", "now"), ("energy_full_wh", "full"),
                       ("energy_design_wh", "design")):
        if info.get(key) is not None:
            energy.append(f"{label} {info[key]:.2f} Wh")
    if energy:
        print("  energy: " + ", ".join(energy))
    live = []
    if info.get("power_now_w") is not None:
        live.append(f"{info['power_now_w']:.2f} W")
    if info.get("voltage_now_v") is not None:
        live.append(f"{info['voltage_now_v']:.3f} V")
    if info.get("hwmon_power_now_w") is not None:
        live.append(f"{info['hwmon_power_now_w']:.2f} W (hwmon)")
    if live:
        print("  now: " + ", ".join(live))
    print(f"  cycles: {info['cycle_count']}")
    if info.get("ac"):
        print(f"  ac: {info['ac']['name']} online={info['ac']['online']}")
    print("  charge control (capability = what the kernel actually exposes):")
    for attr in ("charge_control_end_threshold", "charge_control_start_threshold",
                 "charge_behaviour"):
        cap = info["capabilities"].get(attr)
        value = {"charge_control_end_threshold": info.get("charge_control_end_threshold"),
                 "charge_control_start_threshold": info.get("charge_control_start_threshold"),
                 "charge_behaviour": info.get("charge_behaviour")}[attr]
        shown = "absent" if cap == "absent" else f"{value} [{cap}]"
        print(f"    {attr:<32} {shown}")
    print(f"  bypass: {'ON' if info['bypass'] else 'off'}"
          + (f" ({info['bypass_vendor_equivalent']})" if info.get("bypass_vendor_equivalent") else ""))
    if len(info["charge_behaviour_available"]) > 1:
        print("    modes: " + " ".join(info["charge_behaviour_available"]))
    for warning in info["warnings"]:
        print(f"  warning: {warning}")


# --------------------------------------------------------------------------- #
# status / state
# --------------------------------------------------------------------------- #

def collect_status():
    status = {"version": VERSION, "uid": os.geteuid(), "root": os.geteuid() == 0,
              "domains": [], "fan": None, "thermal": {}, "warnings": [], "errors": []}
    try:
        for domain in discover_domains():
            entry = {
                "path": domain.path, "kind": domain.kind, "zone": domain.name,
                "enabled": domain.enabled(), "constraints": {},
            }
            for level, cname in LEVELS:
                c = domain.constraints.get(level)
                if not c:
                    continue
                limit = domain.get(level)
                entry["constraints"][level] = {
                    "name": cname,
                    "limit_uw": limit,
                    "time_window_us": domain.get_window(level),
                    "writable": probe_writable(c["limit"]),
                }
            status["domains"].append(entry)
    except ControlError as exc:
        status["errors"].append(str(exc))

    fan = find_fan()
    status["fan"] = fan_status(fan) if fan else None
    if fan is None:
        status["warnings"].append("no oxp_ec hwmon fan node (oxpec not loaded?)")

    tt = tt_toggle_state()
    if tt == "0":
        status["warnings"].append("tt_toggle=0: manual PWM will not take effect")

    for label, path in (("package", find_temp_sensor()),
                        ("TCPU", "/sys/class/thermal/thermal_zone3/temp")):
        value = read_temp(path) if path and os.path.exists(path) else None
        if value is not None:
            status["thermal"][label] = value

    try:
        status["platform_profile"] = read_text(PLATFORM_PROFILE)
    except ControlError:
        status["platform_profile"] = None

    try:
        status["rapl_power_w"] = {
            os.path.basename(p): float(read_text(p))
            for p in sorted(glob.glob("/run/rapl-power/*"))
        }
    except (ControlError, ValueError):
        status["rapl_power_w"] = {}

    # This one is a known cosmetic bug on the device; surface it so a
    # "sensors" error is not mistaken for a fan failure.
    acpi_fan = None
    for path in sorted(glob.glob(os.path.join(HWMON, "hwmon*"))):
        try:
            if read_text(os.path.join(path, "name")) == "acpi_fan":
                try:
                    acpi_fan = int(read_text(os.path.join(path, "fan1_input")))
                except ControlError:
                    acpi_fan = "EIO"
        except ControlError:
            continue
    if acpi_fan is not None:
        status["acpi_fan_fan1_input"] = acpi_fan
        if acpi_fan == "EIO":
            status["warnings"].append(
                "acpi_fan fan1_input returns EIO (known cosmetic bug, not the fan; "
                "use oxp_ec fan1_input)")
    status["saved_state"] = load_state()
    status["curve_file"] = CURVE_FILE if os.path.exists(CURVE_FILE) else None
    try:
        status["battery"] = battery_status()
    except ControlError as exc:
        status["battery"] = {"path": None, "warnings": [str(exc)], "present": False}
    return status


def current_profile(status):
    """Name the profile whose PL1/PL2/PL4 match best, or 'custom'."""
    if not status["domains"]:
        return None
    msr = next((d for d in status["domains"] if d["kind"] == "msr"), status["domains"][0])
    got = {}
    for level, _ in LEVELS:
        c = msr["constraints"].get(level)
        got[level] = None if not c or c["limit_uw"] is None else c["limit_uw"] / 1e6
    if any(v is None for v in got.values()):
        return {"name": "custom", "matches": got}
    for name, preset in PROFILES.items():
        if all(abs(got[level] - preset[level]) <= 0.2 for level, _ in LEVELS):
            return {"name": name, "matches": got}
    return {"name": "custom", "matches": got}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        raise ControlError(f"cannot save {STATE_FILE}: {exc.strerror}", hint="run as root") from exc


def print_status(status):
    print(f"oxp3-powerctl {status['version']}  (uid {status['uid']}"
          f"{', root' if status['root'] else ', read-only'})")
    if status["errors"]:
        for err in status["errors"]:
            print(f"  ! {err}")
    print("power domains (powercap):")
    if not status["domains"]:
        print("  none - intel_rapl_msr not loaded?")
    for domain in status["domains"]:
        print(f"  {domain['path'].rsplit('/', 1)[-1]:<20} {domain['zone']:<10} "
              f"enabled={domain['enabled']}")
        for level, cname in LEVELS:
            c = domain["constraints"].get(level)
            if not c:
                print(f"    {level.upper():<4} {cname:<10} not exposed")
                continue
            window = ""
            if c["time_window_us"] is not None:
                window = f"  window {fmt_tau(c['time_window_us'])}"
            if c["writable"]:
                writable = "rw"
            elif os.geteuid() == 0:
                writable = "ro (write refused: BIOS lock?)"
            else:
                writable = "ro (needs root)"
            print(f"    {level.upper():<4} {cname:<10} "
                  f"{fmt_w(c['limit_uw']) if c['limit_uw'] is not None else 'unreadable':>9}"
                  f"{window}  [{writable}]")
    prof = current_profile(status)
    if prof:
        print(f"profile: {prof['name']}  "
              f"(PL1 {prof['matches'].get('pl1')}, PL2 {prof['matches'].get('pl2')}, "
              f"PL4 {prof['matches'].get('pl4')} W)")
    print("fan (oxpec):")
    fan = status["fan"]
    if not fan:
        print("  no fan control node")
    else:
        print(f"  {fan['hwmon'].rsplit('/', 1)[-1]} {fan['name']}: "
              f"{fan['rpm']} rpm, pwm {fan['pwm']}/255 (ec {fan['ec_duty_0_184']}/184), "
              f"mode {fan['mode']} (pwm1_enable={fan['pwm_enable']}), "
              f"tt_toggle={fan['tt_toggle']}")
    if status["thermal"]:
        print("temperatures: " + ", ".join(f"{k} {v:.1f} degC" for k, v in status["thermal"].items()))
    if status.get("rapl_power_w"):
        print("power now (1 s averages from rapl-power): "
              + ", ".join(f"{k} {v} W" for k, v in status["rapl_power_w"].items()))
    print(f"platform_profile: {status.get('platform_profile')}")
    if status.get("battery"):
        bat = status["battery"]
        if bat.get("path"):
            print(f"battery: {bat['name']} {bat.get('capacity_pct')} % "
                  f"({bat.get('status')}), health {bat.get('health_pct')} %, "
                  f"charge limit {bat.get('charge_control_end_threshold')} "
                  f"[{bat['capabilities'].get('charge_control_end_threshold')}], "
                  f"bypass {'on' if bat.get('bypass') else 'off'} "
                  f"({bat.get('charge_behaviour')})")
        else:
            print("battery: none detected")
    if status.get("acpi_fan_fan1_input") is not None:
        print(f"acpi_fan.fan1_input: {status['acpi_fan_fan1_input']}")
    saved = status.get("saved_state") or {}
    if saved:
        print(f"saved state ({STATE_FILE}): {json.dumps(saved, sort_keys=True)}")
    if status.get("curve_file"):
        print(f"fan curve file: {status['curve_file']}")
    for warning in status["warnings"]:
        print(f"  warning: {warning}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# ec: the recovered vendor register map, and what this kernel exposes
# --------------------------------------------------------------------------- #
# Recovered 2026-11-09 from OneXConsole's own per-OEM bootstrap (the managed
# CompatLayerCT.exe is stripped, so the numbers are arguments the Electron
# layer passes *into* it, not IL immediates). Every row with an independent
# implementation in upstream oxpec.c was checked against it; see
# upstream/oxpec-ec-addresses.md for provenance, offsets and confidence.
#
# The vendor writes an EC address as 0x400 + <ACPI-EC offset>; `ec` below is
# the offset the EC actually sees.
EC_MAP = (
    # what, ec offset, vendor value, vendor field, encoding, confidence, linux
    ("fan mode", "0x4A", "0x44A", "addrFanAutomate", "0x00 auto / 0x01 manual",
     "confirmed", "hwmon pwm1_enable"),
    ("fan duty", "0x4B", "0x44B", "addrFanSpeed", "0-184 raw (max 184 here)",
     "confirmed", "hwmon pwm1 (driver scales 0-255)"),
    ("fan tach H", "0x58", "0x458", "fanSpeedHECAddr", "rpm high byte",
     "confirmed", "hwmon fan1_input"),
    ("fan tach L", "0x59", "0x459", "fanSpeedLECAddr", "rpm low byte",
     "confirmed", "hwmon fan1_input"),
    ("charge limit", "0xA3", "0x4A3", "addrChargeLimit", "percent 0-100",
     "confirmed", "charge_control_end_threshold"),
    ("bypass power", "0xA4", "0x4A4", "addrBypassPower",
     "0x00 off / 0x01 awake / 0x03 always", "confirmed", "charge_behaviour"),
    ("charge floor", "0xA5", "0x4A5", "addrForceChargeMin",
     "percent (unit unproven)", "candidate", "-"),
    ("battery temp", "0xA0", "0x4A0", "addrBatteryTemp", "degC (scale unproven)",
     "candidate", "-"),
    ("charge current H", "0xA1", "0x4A1", "addrBatteryChargeCurrentH",
     "16-bit high (unit unproven)", "candidate", "-"),
    ("charge current L", "0xA2", "0x4A2", "addrBatteryChargeCurrentL",
     "16-bit low (unit unproven)", "candidate", "-"),
    ("board sensor 1", "0x60", "0x460", "addrBoardSensor1", "vendor scale (unknown)",
     "candidate", "-"),
    ("board sensor 2", "0x61", "0x461", "addrBoardSensor2", "vendor scale (unknown)",
     "candidate", "-"),
    ("cpu temp (EC)", "0x70", "0x470", "addrCpuTemp", "degC (scale unproven)",
     "candidate", "-"),
    ("power supply mode", "0xE3", "0x4E3", "addrPowerSupplyMode",
     "1 battery / 2 TypeC>=100W / 4 DC-in / 8 TypeC<=65W", "candidate", "-"),
    ("app function en", "0xEB", "0x4EB", "appFunENECAddr", "vendor takeover flag",
     "candidate", "-"),
    ("handle power", "0x2D", "0x42D", "addrHandlePower", "0x01 on / 0x00 off",
     "candidate", "-"),
)

# Raw EC windows this kernel would have to provide before the tool could ever
# touch a register itself. Both are debugfs interfaces and neither is built on
# the stock Anatase kernel; the supported path is the driver's own attributes.
EC_WINDOW_CANDIDATES = (
    "/sys/kernel/debug/ec/ec0/io",       # CONFIG_ACPI_EC_DEBUGFS
    "/sys/devices/virtual/misc/ec_sys/io",  # ec_sys module
)

EC_WRITE_REFUSAL = (
    "oxp3-powerctl never pokes EC RAM directly: the addresses above come from "
    "vendor code and have not been confirmed against this device. Use the driver "
    "attributes (`battery limit`, `battery bypass`, `fan speed`), which write the "
    "same registers through oxpec/acpi_ec with the kernel's locking."
)


def ec_interfaces():
    """Report the EC-facing interfaces that exist, without writing anything."""
    info = {"raw_ec_window": None, "fan": {}, "battery": {}, "warnings": []}
    fan = find_fan()
    if fan:
        info["fan"] = {"path": fan.path, "name": fan.name}
        info["fan"]["pwm1"] = probe_state(fan.pwm)
        info["fan"]["pwm1_enable"] = probe_state(fan.pwm_enable)
        info["fan"]["fan1_input"] = probe_state(fan.fan_input)
    else:
        info["warnings"].append("no oxp_ec fan hwmon node (oxpec not bound?)")
    battery = find_battery()
    if battery:
        info["battery"] = {"path": battery}
        for name in ("charge_control_end_threshold", "charge_behaviour"):
            info["battery"][name] = probe_state(os.path.join(battery, name))
    else:
        info["warnings"].append("no battery device")
    for candidate in EC_WINDOW_CANDIDATES:
        state = probe_state(candidate)
        if state != "absent":
            info["raw_ec_window"] = {"path": candidate, "state": state}
            break
    if info["raw_ec_window"] is None:
        info["warnings"].append(
            "no raw EC window (CONFIG_ACPI_EC_DEBUGFS/ec_sys absent); the vendor "
            "map is informational only on this kernel")
    return info


def cmd_ec(args):
    if args.ec_action == "map":
        rows = [dict(zip(("what", "ec_offset", "vendor_value", "vendor_field",
                          "encoding", "confidence", "linux"), row)) for row in EC_MAP]
        if args.json:
            print(json.dumps({"ok": True, "registers": rows}, indent=2, sort_keys=True))
            return 0
        print(f"{'what':18s} {'ec':5s} {'vendor':7s} {'confidence':10s} "
              f"{'encoding':44s} linux")
        for row in rows:
            print(f"{row['what']:18s} {row['ec_offset']:5s} {row['vendor_value']:7s} "
                  f"{row['confidence']:10s} {row['encoding']:44s} {row['linux']}")
        print("\nprovenance: upstream/oxpec-ec-addresses.md "
              "(vendor app.asar + upstream oxpec.c cross-check)")
        return 0
    if args.ec_action == "probe":
        info = ec_interfaces()
        if args.json:
            print(json.dumps(info, indent=2, sort_keys=True))
        else:
            print("EC-facing interfaces")
            for name, state in (info.get("fan") or {}).items():
                if name != "path":
                    print(f"  fan {name:26s} {state}")
            for name, state in (info.get("battery") or {}).items():
                if name != "path":
                    print(f"  battery {name:22s} {state}")
            window = info.get("raw_ec_window")
            print("  raw EC window            " +
                  (f"{window['path']} ({window['state']})" if window else "absent"))
            for warning in info["warnings"]:
                print(f"  warning: {warning}")
        return 0 if (info.get("fan") or info.get("battery")) else 2
    raise ControlError("refusing to write EC RAM directly",
                       hint=EC_WRITE_REFUSAL)


def cmd_status(args):
    status = collect_status()
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print_status(status)
    return 0 if not status["errors"] else 4


def cmd_battery(args):
    action = args.battery_action
    if action == "status":
        info = battery_status()
        if args.json:
            print(json.dumps(info, indent=2, sort_keys=True))
        else:
            print_battery(info)
        return 0 if info.get("path") else 2

    if action == "limit":
        target = None
        if args.percent is not None:
            if str(args.percent).lower() in ("off", "none", "no", "0"):
                target = None
            else:
                try:
                    target = int(args.percent)
                except ValueError:
                    raise ControlError(f"charge limit {args.percent!r} is not a number",
                                       hint="use 50-100, or 'off' to disable the limit")
        if not args.dry_run:
            check_root("changing the battery charge limit")
        rep = set_charge_threshold(target, dry_run=args.dry_run)
        rep["ok"] = True
        if args.save and not args.dry_run:
            state = load_state()
            state.setdefault("battery", {})["end_threshold"] = rep["applied"]
            save_state(state)
            rep["saved"] = STATE_FILE
        if args.json:
            print(json.dumps(rep, indent=2, sort_keys=True))
        else:
            verb = "would set" if args.dry_run else "set"
            print(f"{verb} charge limit to {rep['requested']} % "
                  f"(reads back {rep['applied']}, capability {rep['capability']})")
            if rep.get("saved"):
                print(f"saved to {rep['saved']} (boot/resume units re-apply it)")
        return 0

    if action == "bypass":
        if args.bypass_action == "status":
            info = battery_status()
            if args.json:
                print(json.dumps({k: info.get(k) for k in
                                  ("path", "name", "bypass", "charge_behaviour",
                                   "charge_behaviour_available", "bypass_vendor_equivalent",
                                   "capabilities", "warnings")},
                                 indent=2, sort_keys=True))
            else:
                print(f"bypass: {'ON' if info.get('bypass') else 'off'}"
                      f"  charge_behaviour={info.get('charge_behaviour')}"
                      f"  available={' '.join(info.get('charge_behaviour_available') or [])}")
                if info.get("bypass_vendor_equivalent"):
                    print(f"  vendor equivalent: {info['bypass_vendor_equivalent']}")
                for warning in info.get("warnings") or []:
                    print(f"  warning: {warning}")
            return 0 if info.get("path") else 2
        on = args.bypass_action == "on"
        if not args.dry_run:
            check_root("changing the battery charge behaviour")
        rep = set_charge_bypass(on, mode=args.mode, dry_run=args.dry_run)
        rep["ok"] = True
        if args.save and not args.dry_run:
            state = load_state()
            state.setdefault("battery", {})["charge_behaviour"] = rep["applied"]
            save_state(state)
            rep["saved"] = STATE_FILE
        if args.json:
            print(json.dumps(rep, indent=2, sort_keys=True))
        else:
            verb = "would set" if args.dry_run else "set"
            print(f"{verb} charge_behaviour={rep['requested']} "
                  f"(was {rep['previous']}, reads back {rep['applied']})")
            if rep["requested"] != "auto":
                print("  bypass: charging stops while plugged in; the device runs from "
                      "the adapter. The EC does not bypass battery discharge, so the "
                      "pack still buffers load spikes.")
            if rep.get("saved"):
                print(f"saved to {rep['saved']} (boot/resume units re-apply it)")
        return 0

    raise ControlError(f"unknown battery action {action!r}")  # pragma: no cover


def resolve_profile(name):
    key = name.lower()
    key = PROFILE_ALIASES.get(key, key)
    if key not in PROFILES:
        raise ControlError(
            f"unknown profile {name!r}",
            hint="known: " + ", ".join(PROFILES) + " (aliases: "
                 + ", ".join(f"{k}->{v}" for k, v in PROFILE_ALIASES.items()) + ")",
        )
    return key, PROFILES[key]


def cmd_profile(args):
    key, preset = resolve_profile(args.name)
    values = {level: preset[level] for level, _ in LEVELS}

    result = {"profile": key, "values": values, "dry_run": args.dry_run,
              "domain_reports": [], "errors": [], "warnings": [], "readback": {}}

    if not args.dry_run:
        check_root()
    domains, missing = require_domains(allow_partial=args.allow_partial)

    reports = []
    for domain in domains:
        # Each domain is written independently so one missing MMIO copy does
        # not stop the MSR one (and vice versa).
        reports.append(apply_limits([domain], values, dry_run=args.dry_run,
                                    allow_partial=True))
    result["domain_reports"] = reports
    for rep in reports:
        result["errors"].extend(rep["errors"])

    tau_written = None
    if args.with_tau or args.tau is not None:
        tau_us = parse_tau(args.tau) if args.tau is not None else int(preset["tau_s"] * 1_000_000)
        tau_written = {"requested_us": tau_us, "domains": [], "errors": []}
        for domain in domains:
            if "pl1" not in domain.constraints:
                continue
            if args.dry_run:
                tau_written["domains"].append({"path": domain.path, "value_us": tau_us,
                                               "dry_run": True})
                continue
            try:
                domain.set_window(tau_us)
            except ControlError as exc:
                tau_written["errors"].append({"path": domain.path, "error": str(exc),
                                              "hint": exc.hint})
                continue
            actual = domain.get_window()
            tau_written["domains"].append({"path": domain.path, "actual_us": actual})
            if actual is None:
                tau_written["errors"].append({"path": domain.path,
                                              "error": "time window unreadable after write"})
        result["tau"] = tau_written

    # platform_profile is a real ACPI knob (and the vendor's intelPnPMode
    # maps onto it); it is optional and never fatal.
    if args.platform_profile is not None:
        wanted = args.platform_profile or preset["platform_profile"]
        try:
            if args.dry_run:
                result["platform_profile"] = {"set": wanted, "dry_run": True}
            else:
                write_text(PLATFORM_PROFILE, wanted)
                result["platform_profile"] = {"set": wanted,
                                              "actual": read_text(PLATFORM_PROFILE)}
        except ControlError as exc:
            result["warnings"].append(f"platform_profile not set: {exc}"
                                      + (f" ({exc.hint})" if exc.hint else ""))

    # Vendor per-profile fan speed is opt-in: 0 means "leave the EC alone".
    if args.fan and preset["fan"]:
        fan = find_fan()
        if fan is None:
            result["warnings"].append("no fan control node; fan speed not applied")
        else:
            result["fan"] = apply_fan(fan, mode="manual", duty=preset["fan"],
                                      dry_run=args.dry_run,
                                      allow_partial=args.allow_partial)
            if not args.dry_run:
                result["fan"]["readback_mode"] = fan_status(fan)
    elif args.fan and not preset["fan"]:
        result["warnings"].append(f"profile {key} has fanSpeed=0 (leave automatic)")

    result["ok"] = not result["errors"]
    if not args.dry_run and result["ok"] and args.save:
        state = load_state()
        state["profile"] = key
        if args.platform_profile is not None:
            state["platform_profile"] = result.get("platform_profile", {}).get("set") \
                or preset["platform_profile"]
        save_state(state)
        result["saved"] = STATE_FILE

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        tag = " [dry-run]" if args.dry_run else ""
        print(f"profile {key}{tag}: " + ", ".join(f"{l.upper()} {v:g} W" for l, v in values.items()))
        for rep in reports:
            for w in rep["written"]:
                print(f"  wrote {w['value_uw'] / 1e6:g} W -> "
                      f"{w['domain'].rsplit('/', 1)[-1]} {w['level'].upper()}")
        if tau_written:
            for d in tau_written["domains"]:
                print(f"  tau -> {d['path'].rsplit('/', 1)[-1]}: "
                      f"requested {fmt_tau(tau_written['requested_us'])}"
                      + (f", reads {fmt_tau(d['actual_us'])}" if d.get("actual_us") else ""))
        for err in result["errors"]:
            if isinstance(err, dict):
                print(f"  error: {err.get('error')}"
                      + (f" ({err['hint']})" if err.get("hint") else ""), file=sys.stderr)
            else:
                print(f"  error: {err}", file=sys.stderr)
        for warning in result["warnings"]:
            print(f"  warning: {warning}", file=sys.stderr)
        if result.get("saved"):
            print(f"  saved as the boot profile in {result['saved']}")

    if result["errors"]:
        return 3 if any("readback" in json.dumps(e) for e in result["errors"]) else 2
    return 0


def cmd_set(args):
    level = args.level
    if args.watts <= 0:
        raise ControlError(f"{level.upper()} must be > 0 W")
    if not args.dry_run:
        check_root()
    values = {level: args.watts}
    domains, missing = require_domains(levels=[level], allow_partial=args.allow_partial)
    result = {"level": level, "watts": args.watts, "dry_run": args.dry_run,
              "errors": [], "warnings": [], "reports": []}
    for domain in domains:
        result["reports"].append(apply_limits([domain], values, dry_run=args.dry_run,
                                              allow_partial=True))
    for rep in result["reports"]:
        result["errors"].extend(rep["errors"])

    tau_arg = getattr(args, "tau", None)
    if tau_arg is not None and level == "pl1":
        tau_us = parse_tau(tau_arg)
        result["tau"] = {"requested_us": tau_us, "domains": [], "errors": []}
        for domain in domains:
            if "pl1" not in domain.constraints:
                continue
            try:
                if args.dry_run:
                    result["tau"]["domains"].append({"path": domain.path, "dry_run": True})
                    continue
                domain.set_window(tau_us)
                result["tau"]["domains"].append({"path": domain.path,
                                                 "actual_us": domain.get_window()})
            except ControlError as exc:
                result["tau"]["errors"].append({"path": domain.path, "error": str(exc),
                                                "hint": exc.hint})
        result["errors"].extend(result["tau"]["errors"])

    result["ok"] = not result["errors"]
    if not args.dry_run and result["ok"] and args.save:
        state = load_state()
        state.setdefault("limits", {})[level] = args.watts
        if tau_arg is not None and level == "pl1":
            state["limits"]["tau_us"] = parse_tau(tau_arg)
        save_state(state)
        result["saved"] = STATE_FILE

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        tag = " [dry-run]" if args.dry_run else ""
        print(f"{level.upper()} = {args.watts:g} W{tag}")
        for rep in result["reports"]:
            for rb in rep["readback"]:
                print(f"  {rb['domain'].rsplit('/', 1)[-1]} {rb['level'].upper()}: "
                      f"{fmt_w(rb['want_uw'])} -> {fmt_w(rb['actual_uw']) if rb['actual_uw'] is not None else 'unreadable'}"
                      f" {'ok' if rb['ok'] else 'MISMATCH'}")
        tau_res = result.get("tau")
        if tau_res:
            print(f"  tau -> requested {fmt_tau(tau_res['requested_us'])}")
            for d in tau_res["domains"]:
                if d.get("actual_us"):
                    print(f"    {d['path'].rsplit('/', 1)[-1]} reads {fmt_tau(d['actual_us'])} (quantised)")
        for err in result["errors"]:
            print(f"  error: {err}", file=sys.stderr)
    if result["errors"]:
        return 3 if any("readback" in json.dumps(e) for e in result["errors"]) else 2
    return 0


def cmd_tau(args):
    tau_us = parse_tau(args.window)
    check_root()
    domains, missing = require_domains(levels=["pl1"], allow_partial=args.allow_partial)
    result = {"requested_us": tau_us, "dry_run": args.dry_run, "domains": [], "errors": []}
    for domain in domains:
        if "pl1" not in domain.constraints:
            result["errors"].append({"path": domain.path, "error": "no PL1 constraint"})
            continue
        if args.dry_run:
            result["domains"].append({"path": domain.path, "value_us": tau_us, "dry_run": True})
            continue
        try:
            domain.set_window(tau_us)
        except ControlError as exc:
            result["errors"].append({"path": domain.path, "error": str(exc), "hint": exc.hint})
            continue
        actual = domain.get_window()
        # The kernel encodes the window as 2^y * (1+f/4) * time_unit (976 us
        # here), so the readback is quantised; accept 5 % and report it.
        ok = actual is not None and abs(actual - tau_us) <= max(2 * 976, tau_us // 20)
        result["domains"].append({"path": domain.path, "actual_us": actual, "ok": ok})
        if not ok:
            result["errors"].append({"path": domain.path,
                                     "error": f"readback {actual} us does not match {tau_us} us"})
    result["ok"] = not result["errors"]
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"PL1 time window -> {fmt_tau(tau_us)}"
              + (" [dry-run]" if args.dry_run else ""))
        for d in result["domains"]:
            if d.get("actual_us"):
                print(f"  {d['path'].rsplit('/', 1)[-1]}: reads {fmt_tau(d['actual_us'])}"
                      f" (quantised)")
        for err in result["errors"]:
            print(f"  error: {err}", file=sys.stderr)
    if result["errors"]:
        return 3
    return 0


def cmd_fan(args):
    fan = find_fan()
    if fan is None:
        raise ControlError("no fan control node found", hint="is the oxpec driver loaded?")
    if args.fan_action == "status":
        status = fan_status(fan)
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            print(f"{fan.name} ({fan.path}): {status['rpm']} rpm, "
                  f"pwm {status['pwm']}/255 (ec {status['ec_duty_0_184']}/184), "
                  f"mode {status['mode']} (pwm1_enable={status['pwm_enable']}), "
                  f"tt_toggle={status['tt_toggle']}")
        return 0

    if args.fan_action != "curve" and not args.dry_run:
        check_root()
    if args.fan_action == "auto":
        result = apply_fan(fan, mode="auto", dry_run=args.dry_run)
    elif args.fan_action == "manual":
        result = apply_fan(fan, mode="manual", duty=args.speed, dry_run=args.dry_run)
    elif args.fan_action == "speed":
        result = apply_fan(fan, mode="manual", duty=args.speed, dry_run=args.dry_run)
    elif args.fan_action == "curve":
        points, source = load_curve(args.file)
        summary = run_curve(fan, points, interval=args.interval,
                            steps=args.steps if not args.once else 1,
                            sensor=args.sensor, dry_run=args.dry_run,
                            restore_auto=not args.no_restore, quiet=args.json,
                            hysteresis=args.hysteresis, source=source)
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    else:  # pragma: no cover - argparse restricts this
        raise ControlError(f"unknown fan action {args.fan_action}")

    result["dry_run"] = args.dry_run
    result["ok"] = not result["errors"]
    if not args.dry_run and result["ok"] and getattr(args, "save", False):
        state = load_state()
        state["fan"] = {"mode": "manual" if args.speed is not None else "auto"}
        if args.speed is not None:
            state["fan"]["pwm"] = args.speed
        save_state(state)
        result["saved"] = STATE_FILE
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        tag = " [dry-run]" if args.dry_run else ""
        for action in result["actions"]:
            print(f"  {action['set']} = {action.get('value')}{tag}")
        rb = result.get("readback") or {}
        if rb:
            print(f"  now: {rb.get('rpm')} rpm, pwm {rb.get('pwm')}/255 "
                  f"(ec {rb.get('ec_duty_0_184')}/184), mode {rb.get('mode')}")
        for warning in result["warnings"]:
            print(f"  warning: {warning}", file=sys.stderr)
        for err in result["errors"]:
            if isinstance(err, dict):
                print(f"  error: {err.get('error')}"
                      + (f" ({err['hint']})" if err.get("hint") else ""), file=sys.stderr)
            else:
                print(f"  error: {err}", file=sys.stderr)
    if result["errors"]:
        return 3 if any("readback" in json.dumps(e) for e in result["errors"]) else 2
    return 0


def cmd_snapshot(args):
    status = collect_status()
    snap = {"version": VERSION, "taken": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "domains": {}, "platform_profile": status.get("platform_profile")}
    for domain in status["domains"]:
        snap["domains"][domain["path"]] = {
            level: c["limit_uw"] for level, c in domain["constraints"].items()
            if c["limit_uw"] is not None
        }
        if "pl1" in domain["constraints"]:
            snap["domains"][domain["path"]]["pl1_time_window_us"] = \
                domain["constraints"]["pl1"]["time_window_us"]
    if args.json or not args.save:
        print(json.dumps(snap, indent=2, sort_keys=True))
    if args.save:
        if os.geteuid() != 0 and not os.path.isdir(STATE_DIR):
            raise ControlError(f"cannot create {STATE_DIR}", hint="run as root")
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(BASELINE_FILE, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, indent=2, sort_keys=True)
                fh.write("\n")
        except OSError as exc:
            raise ControlError(f"cannot save {BASELINE_FILE}: {exc.strerror}") from exc
        if not args.json:
            print(f"baseline saved to {BASELINE_FILE}", file=sys.stderr)
    return 0


def cmd_restore_baseline(args):
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ControlError(f"cannot read {BASELINE_FILE}", hint="run 'snapshot --save' first") from exc
    if not args.dry_run:
        check_root()
    domains, _ = require_domains(allow_partial=True)
    result = {"dry_run": args.dry_run, "written": [], "errors": []}
    for domain in domains:
        saved = snap.get("domains", {}).get(domain.path, {}) or {}
        values = {level: uw / 1e6 for level, uw in saved.items() if level in dict(LEVELS)}
        if values:
            rep = apply_limits([domain], values, dry_run=args.dry_run, allow_partial=True)
            result["written"].extend(rep["written"])
            result["errors"].extend(rep["errors"])
        window = saved.get("pl1_time_window_us")
        if window and "pl1" in domain.constraints:
            try:
                if not args.dry_run:
                    domain.set_window(int(window))
                result["written"].append({"domain": domain.path, "level": "pl1_time_window_us",
                                          "value_us": int(window), "dry_run": args.dry_run})
            except ControlError as exc:
                result["errors"].append({"domain": domain.path, "error": str(exc)})
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"restored {len(result['written'])} limit(s) from {BASELINE_FILE}")
        for err in result["errors"]:
            print(f"  error: {err}", file=sys.stderr)
    return 0 if not result["errors"] else 3


def cmd_restore(args):
    """Apply the saved profile / limits / fan mode (called by the systemd units)."""
    state = load_state()
    if not state:
        raise ControlError(f"no saved state in {STATE_FILE}",
                           hint="run 'profile <name> --save' once (sudo)")
    if not args.dry_run:
        check_root()
    result = {"restored": [], "errors": [], "warnings": [], "dry_run": args.dry_run}
    if state.get("profile"):
        ns = argparse.Namespace(name=state["profile"], dry_run=args.dry_run, json=False,
                                with_tau=state.get("with_tau", False), tau=None,
                                platform_profile=(args.platform_profile
                                                  if args.platform_profile is not None
                                                  else state.get("platform_profile")),
                                fan=False, save=False, allow_partial=True)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_profile(ns)
            if not args.json:
                print(buf.getvalue(), end="")
            if rc != 0:
                result["errors"].append(f"profile {state['profile']} did not apply cleanly (exit {rc})")
            else:
                result["restored"].append(f"profile {state['profile']}")
        except ControlError as exc:
            result["errors"].append(str(exc))
    limits = state.get("limits") or {}
    if limits:
        domains, _ = require_domains(allow_partial=True)
        values = {level: limits[level] for level, _ in LEVELS if level in limits}
        for level in values:
            rep = apply_limits(domains, {level: values[level]}, dry_run=args.dry_run,
                               allow_partial=True)
            result["errors"].extend(rep["errors"])
        if values:
            result["restored"].append("limits " + ", ".join(f"{k.upper()}={v:g}"
                                                            for k, v in values.items()))
        if limits.get("tau_us"):
            for domain in domains:
                if "pl1" in domain.constraints and not args.dry_run:
                    try:
                        domain.set_window(limits["tau_us"])
                    except ControlError as exc:
                        result["errors"].append(str(exc))
    fan = state.get("fan") or {}
    if fan and not args.fan:
        node = find_fan()
        if node:
            mode = fan.get("mode", "auto")
            duty = fan.get("pwm") if mode == "manual" else None
            rep = apply_fan(node, mode=mode, duty=duty, dry_run=args.dry_run)
            result["errors"].extend(rep["errors"])
            result["warnings"].extend(rep["warnings"])
            result["restored"].append(f"fan {mode}" + (f" pwm={duty}" if duty is not None else ""))
    battery_saved = state.get("battery") or {}
    if battery_saved:
        try:
            if battery_saved.get("end_threshold") is not None:
                rep = set_charge_threshold(battery_saved["end_threshold"],
                                           dry_run=args.dry_run)
                result["restored"].append(
                    f"charge limit {rep['applied']} %" if not args.dry_run
                    else f"would set charge limit {rep['requested']} %")
            if battery_saved.get("charge_behaviour"):
                rep = set_charge_bypass(
                    battery_saved["charge_behaviour"] != "auto",
                    mode=(None if battery_saved["charge_behaviour"] == "auto"
                          else battery_saved["charge_behaviour"]),
                    dry_run=args.dry_run)
                result["restored"].append(
                    f"charge_behaviour {rep['applied']}" if not args.dry_run
                    else f"would set charge_behaviour {rep['requested']}")
        except ControlError as exc:
            result["warnings"].append(f"battery settings not re-applied: {exc}")
    result["ok"] = not result["errors"]
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("restored: " + (", ".join(result["restored"]) or "nothing"))
        for err in result["errors"]:
            print(f"  error: {err}", file=sys.stderr)
        for warning in result["warnings"]:
            print(f"  warning: {warning}", file=sys.stderr)
    return 0 if result["ok"] else 2


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser():
    parser = argparse.ArgumentParser(
        prog="oxp3-powerctl.py",
        description="OneXPlayer 3 power envelope (PL1/PL2/PL4/tau) and fan control, "
                    "userspace only.",
        epilog="Reads work unprivileged; writes need root (sudo). "
               "See docs/hw/power-fan.md.",
    )
    parser.add_argument("--version", action="version", version=f"oxp3-powerctl {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show what exists, what is writable and what is applied")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("profile", help="apply a vendor preset (quiet/balanced/performance)")
    p.add_argument("name")
    p.add_argument("--tau", help="also set the PL1 time window, e.g. 56ms / 5.6s")
    p.add_argument("--with-tau", action="store_true",
                   help="also set the vendor time window (profiles otherwise leave it alone)")
    p.add_argument("--platform-profile", nargs="?", const="", default=None,
                   metavar="NAME",
                   help="also write /sys/firmware/acpi/platform_profile "
                        "(no value = the preset's choice)")
    p.add_argument("--fan", action="store_true",
                   help="also apply the preset's vendor fanSpeed (manual PWM)")
    p.add_argument("--save", action="store_true", help="remember this profile for restore")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--allow-partial", action="store_true")
    p.set_defaults(func=cmd_profile)

    for level in ("pl1", "pl2", "pl4"):
        p = sub.add_parser(f"set{level}", help=f"set {level.upper()} in W (both domains)")
        p.add_argument("watts", type=float)
        if level == "pl1":
            p.add_argument("--tau", help="also set the PL1 time window (e.g. 56ms)")
        p.add_argument("--save", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--json", action="store_true")
        p.add_argument("--allow-partial", action="store_true")
        p.set_defaults(func=cmd_set, level=level)

    p = sub.add_parser("settau", help="set the PL1 time window (tau)")
    p.add_argument("window", help="e.g. 56ms, 5.6s, 28000us (bare number = seconds)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--allow-partial", action="store_true")
    p.set_defaults(func=cmd_tau)

    p = sub.add_parser("battery",
                       help="battery health, charge limit and charge-inhibit ('bypass')")
    bsub = p.add_subparsers(dest="battery_action", required=True)
    bp = bsub.add_parser("status", help="health/energy/charge-control capability report")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_battery)
    bp = bsub.add_parser("limit", help="set the charge limit (percent) or 'off'")
    bp.add_argument("percent", nargs="?", help="50-100, or 'off' (= 100, no limit)")
    bp.add_argument("--save", action="store_true", help="persist for the boot/resume units")
    bp.add_argument("--dry-run", action="store_true")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_battery)
    bp = bsub.add_parser("bypass",
                         help="charge inhibit: stop charging while on the adapter")
    bp.add_argument("bypass_action", choices=("on", "off", "status"))
    bp.add_argument("--mode", choices=BYPASS_MODES, default=None,
                    help="bypass variant: inhibit-charge (always = vendor mode 2) or "
                         "inhibit-charge-awake (ineffective asleep/off = vendor mode 1)")
    bp.add_argument("--save", action="store_true", help="persist for the boot/resume units")
    bp.add_argument("--dry-run", action="store_true")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_battery)

    p = sub.add_parser("fan", help="fan control (auto/manual/speed/curve/status)")
    p.add_argument("fan_action", choices=("auto", "manual", "speed", "curve", "status"))
    p.add_argument("speed", nargs="?", type=int, help="pwm 0-255 for 'speed'/'manual'")
    p.add_argument("--file", default=CURVE_FILE, help=f"curve file (default {CURVE_FILE})")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between curve steps")
    p.add_argument("--steps", type=int, default=None, help="stop after N steps (default: forever)")
    p.add_argument("--once", action="store_true", help="shortcut for --steps 1")
    p.add_argument("--sensor", help="temperature input path")
    p.add_argument("--hysteresis", type=int, default=3, help="minimum pwm change to write")
    p.add_argument("--no-restore", action="store_true", help="leave manual mode on exit")
    p.add_argument("--save", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fan)

    p = sub.add_parser("ec", help="recovered EC register map and EC-facing interfaces")
    p.add_argument("ec_action", choices=("map", "probe", "write"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ec)

    p = sub.add_parser("snapshot", help="print (or --save) the current limits as a rollback baseline")
    p.add_argument("--save", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("restore-baseline", help="re-apply the saved baseline")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_restore_baseline)

    p = sub.add_parser("restore", help="apply the saved profile/limits/fan (systemd units)")
    p.add_argument("--platform-profile", nargs="?", const="", default=None)
    p.add_argument("--fan", action="store_true", help="skip the saved fan mode")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_restore)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "once", False):
        args.steps = 1
    if args.command == "fan":
        if args.fan_action == "speed" and args.speed is None:
            print("oxp3-powerctl: 'fan speed' needs a pwm value 0-255", file=sys.stderr)
            return 2
        if args.fan_action == "manual" and args.speed is not None and not 0 <= args.speed <= 255:
            print("oxp3-powerctl: pwm must be 0-255", file=sys.stderr)
            return 2
        if args.fan_action == "speed" and not 0 <= args.speed <= 255:
            print("oxp3-powerctl: pwm must be 0-255", file=sys.stderr)
            return 2
    try:
        return args.func(args)
    except ControlError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc), "hint": exc.hint,
                              "path": exc.path}, indent=2, sort_keys=True))
        else:
            print(f"oxp3-powerctl: {exc}", file=sys.stderr)
            if exc.hint:
                print(f"  hint: {exc.hint}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # `... | head`
        return 0


if __name__ == "__main__":
    sys.exit(main())
