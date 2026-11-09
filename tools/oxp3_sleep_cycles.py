#!/usr/bin/env python3
"""sleep-cycle-test backend - drive N s2idle cycles, attribute the wake, report drain/hour.

This is the engine behind ``tools/sleep-cycle-test.sh``.  It exists because the
earlier ``tools/sleep-power-measure.sh`` can measure *one* suspend window but
cannot answer "does the machine return every time, who woke it, and what does
that cost per hour".  This tool:

  1. records the baseline (battery energy/percentage/status, AC, platform
     profile, PL1, temperatures, the ACPI and device wake sources that are
     armed, and every ``/sys/class/wakeup`` event counter),
  2. arms an RTC wake (``rtcwake -m no``) and suspends through logind, so the
     normal sleep.target path (and the EC) is exercised and the machine always
     returns (the RTC interface is a hard precondition; see
     ``docs/SLEEP-TEST.md`` for the fallbacks),
  3. performs N cycles of real suspend/resume (default 3 x 60 s),
  4. after each resume reads the suspended duration from the
     ``CLOCK_BOOTTIME`` minus ``CLOCK_MONOTONIC`` deltas, the
     ``/sys/power/suspend_stats`` deltas, the ``/sys/class/wakeup`` event-count
     deltas and the first kernel wake-source lines from this boot's journal,
  5. computes drain per hour (energy delta / suspended hours), the residual
     awake time and its energy cost, and the per-cycle temperature rise,
  6. prints a table and a GOOD / INVESTIGATE / FAILING verdict against the
     thresholds in ``docs/SLEEP-TEST.md``, naming the most likely wake source,
  7. writes ``capture.json`` (raw observations), ``report.json`` and
     ``report.txt`` to ``docs/state/sleep-cycles-<utc>/``.

Safety properties (deliberate, and tested):

  * root is required for real cycles (``rtcwake`` and suspend); ``--dry-run``
    and ``--no-suspend`` are read-only and need no root;
  * one explicit typed confirmation covers the whole batch, ``--yes`` exists
    for scripted runs and says so; every cycle prints what it is about to do;
  * hard limits: at most 10 cycles, 10..3600 s per sleep, and an estimated
    total runtime that must fit ``--max-runtime`` (default 1800 s); the
    wall-clock budget is checked again before every cycle;
  * a ``finally`` cleanup *always* disarms a pending RTC alarm and reports
    whether the disarm was verified - including on SIGINT/SIGTERM/SIGHUP;
  * on AC the drain numbers are refused by default (``--allow-ac`` runs the
    cycles but the verdict is INVALID for drain and says why).

The analysis half (``--analyse``) is pure, offline and unit-tested against the
synthetic captures in ``tests/fixtures/sleep-cycles/``.  Stdlib only.

Exit codes: 0 GOOD, 1 INVESTIGATE/FAILING, 2 INVALID/NO-MEASUREMENT,
3 usage or setup error.  Same convention as the other tools in this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

PROG = "sleep-cycle-test"
VERSION = "1.0.0"
SCHEMA = 1

# The low-power policy tool is the single source of truth for what the boot
# service changed; the report re-reads it instead of duplicating the decisions.
try:
    import oxp3_lowpower as lowpower
except Exception:  # pragma: no cover - only when tools/ is not importable
    lowpower = None

#: Where oxp3-lowpower.service records pre-images and the applied change list.
LOWPOWER_STATE = "var/lib/oxp3-lowpower/state.json"

REPO = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "docs" / "state"

MAX_CYCLES = 10
MIN_SLEEP_S, MAX_SLEEP_S = 10, 3600
MAX_SETTLE_S = 300
DEFAULT_MAX_RUNTIME_S = 1800

# Thresholds are documented in docs/SLEEP-TEST.md with their rationale.
DEFAULT_THRESHOLDS = {
    "good_pct_per_h": 1.0,
    "investigate_pct_per_h": 3.0,
    "good_temp_rise_c": 2.0,
    "implausible_pct_per_h": 25.0,
    "energy_increase_tolerance_wh": 0.05,
    "gauge_step_wh": 0.01,
}

SUSPEND_STAT_INTS = (
    "success",
    "fail",
    "failed_suspend",
    "failed_resume",
    "failed_resume_early",
    "failed_resume_noirq",
    "last_hw_sleep",
    "max_hw_sleep",
    "total_hw_sleep",
)
SUSPEND_STAT_TEXT = ("last_failed_dev", "last_failed_step")

# Human names for the wakeup sources that matter on this platform.
SOURCE_LABELS = {
    "rtc_cmos": "RTC (scheduled alarm)",
    "alarmtimer.0.auto": "RTC alarm timer (rtcwake)",
    "ACPI000E:00": "ACPI wake alarm (AWAC)",
    "PNP0C0D:00": "lid switch",
    "PNP0C0C:00": "power button",
    "LNXPWRBN:00": "power button (ACPI fixed event)",
    "PNP0C0E:00": "sleep button",
    "ADP1": "AC adapter",
    "ACPI0003:00": "AC adapter",
    "0000:00:14.0": "USB xHCI controller (USB-A, internal HID, gamepad)",
    "0000:00:0d.0": "USB4/Type-C xHCI controller",
    "0000:00:0d.2": "USB4/Thunderbolt DMA",
    "0000:00:07.0": "Type-C root port 0",
    "0000:00:07.1": "Type-C root port 1",
    "0000:00:1c.0": "card-reader root port",
    "0000:00:06.0": "NVMe root port",
    "10EC5280:00": "IMU",
    "GXTP6933:00": "touchpad",
    "3-4": "internal keyboard HID (USB 3-4)",
    "3-5": "EC bridge HID (USB 3-5)",
    "mmc0": "SD card reader (mmc0)",
    "mmc1": "SD card reader (mmc1)",
}

KERNEL_WAKE_KEYWORDS = (
    "PM: suspend",
    "PM: resume",
    "PM: Wakeup",
    "PM: Waking",
    "Waking up",
    "wake source",
    "wakeup",
    "Wakeup",
    "aborting suspend",
    "suspend-to-idle",
    "ACPI: Waking",
    "S0ix",
    "rtc_cmos",
    "alarmtimer",
)


# ----------------------------------------------------------------- small I/O
def _read(path, default=None):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return default


def _read_int(path):
    value = _read(path)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def _iterdir(path):
    try:
        return sorted(pathlib.Path(path).iterdir())
    except OSError:
        return []


def _glob(path, pattern):
    try:
        return sorted(pathlib.Path(path).glob(pattern))
    except OSError:
        return []


def _relpath(path, root):
    """Path relative to the analysed /sys root, for compact JSON."""
    text = str(path)
    prefix = str(root).rstrip("/") + "/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _iso(epoch=None):
    return datetime.fromtimestamp(
        time.time() if epoch is None else epoch, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ns(clock):
    return time.clock_gettime_ns(clock)


def _json_write(path, data):
    pathlib.Path(path).write_text(
        json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------- sysfs reads
def battery_info(root="/"):
    root = pathlib.Path(root)
    for d in _iterdir(root / "sys/class/power_supply"):
        energy = _read_int(d / "energy_now")
        if energy is not None:
            full = _read_int(d / "energy_full")
            return {
                "name": d.name,
                "energy_wh": round(energy / 1e6, 6),
                "energy_full_wh": round(full / 1e6, 6) if full else None,
                "design_wh": round(_read_int(d / "energy_full_design") / 1e6, 6)
                if _read_int(d / "energy_full_design")
                else None,
                "pct": _read_int(d / "capacity"),
                "status": _read(d / "status", "?") or "?",
            }
        charge = _read_int(d / "charge_now")
        voltage = _read_int(d / "voltage_now")
        if charge is not None and voltage is not None:
            full = _read_int(d / "charge_full")
            full_wh = None
            if full:
                full_wh = round(full * voltage / 1e12, 6)
            return {
                "name": d.name,
                "energy_wh": round(charge * voltage / 1e12, 6),
                "energy_full_wh": full_wh,
                "design_wh": None,
                "pct": _read_int(d / "capacity"),
                "status": _read(d / "status", "?") or "?",
            }
    return None


def ac_online(root="/"):
    root = pathlib.Path(root)
    for d in _iterdir(root / "sys/class/power_supply"):
        if (_read(d / "type", "") or "") != "Mains":
            continue
        online = _read_int(d / "online")
        if online is not None:
            return bool(online)
    return None


def platform_profile(root="/"):
    return _read(pathlib.Path(root) / "sys/firmware/acpi/platform_profile", "?")


def pl1_uw(root="/"):
    root = pathlib.Path(root)
    candidates = []
    for d in _glob(root / "sys/class/powercap", "intel-rapl:*"):
        name = _read(d / "name", "") or ""
        value = _read_int(d / "constraint_0_power_limit_uw")
        if value is None:
            continue
        if name == "package-0":
            return value
        candidates.append(value)
    return candidates[0] if candidates else None


def temperatures(root="/"):
    root = pathlib.Path(root)
    zones = {}
    for d in _glob(root / "sys/class/thermal", "thermal_zone*"):
        kind = _read(d / "type", "") or ""
        milli = _read_int(d / "temp")
        if not kind or milli is None:
            continue
        value = round(milli / 1000.0, 2)
        # A type can appear more than once (TCPU and TCPU_PCI here); keep the
        # hottest reading for that type so the reported maximum cannot hide one.
        previous = zones.get(kind)
        zones[kind] = value if previous is None else max(previous, value)
    return zones


def resource_guards(opts):
    """Refuse a suspend run that could hurt the machine or the filesystem.

    Real cycles write the transcript, arm an RTC alarm and suspend; on this
    hardware repeated rtcwake cycles have hung the firmware at the suspend
    entry (journal ends at rtcwake, no OOM and no disk pressure).  The guards
    below keep a run from starting when the obvious resources are low, and
    stop a second multi-cycle batch in the same boot unless forced.
    """
    problems = []
    try:
        usage = shutil.disk_usage("/var" if os.path.isdir("/var") else "/")
        if usage.free < 2 * 1024 ** 3:
            problems.append("only %.1f GiB free on /var (need 2 GiB for the transcript)"
                            % (usage.free / 1024 ** 3))
    except OSError:
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    if kb < 1024 * 1024:
                        problems.append("only %d MiB available memory (need 1 GiB)" % (kb // 1024))
                    break
    except OSError:
        pass
    stats = suspend_stats()
    success = stats.get("success") if isinstance(stats, dict) else None
    if not getattr(opts, "force", False) and success:
        try:
            done = int(str(success).split()[0])
        except (ValueError, IndexError):
            done = 0
        if done >= 6 and opts.cycles > 1:
            problems.append(
                "%d suspends already completed in this boot; repeated multi-cycle "
                "batches have wedged the firmware here - reboot and run --cycles 1, "
                "or pass --force if you accept the risk" % done)
    return problems


def suspend_stats(root="/"):
    root = pathlib.Path(root) / "sys/power/suspend_stats"
    stats = {}
    for key in SUSPEND_STAT_INTS:
        value = _read_int(root / key)
        if value is not None:
            stats[key] = value
    for key in SUSPEND_STAT_TEXT:
        value = _read(root / key)
        if value:
            stats[key] = value
    return stats


def wakeup_counts(root="/"):
    """Every /sys/class/wakeup source and its event counter (stable ids)."""
    root = pathlib.Path(root)
    out = []
    for d in _glob(root / "sys/class/wakeup", "wakeup*"):
        count = _read_int(d / "event_count")
        name = _read(d / "name")
        if count is None or name is None:
            continue
        device = ""
        link = d / "device"
        try:
            resolved = os.path.realpath(link)
            if os.path.exists(resolved):
                device = _relpath(resolved, root)
        except OSError:
            device = ""
        if not device and name.startswith("mmc"):
            # The measured mmc0 source has a dangling `device` link, so the
            # path is recovered from the class device instead of being lost.
            for candidate in (
                root / "sys/class/mmc_host" / name,
                root / "sys/class/mmc_host" / name / "device",
                root / "sys/bus/pci/devices/0000:56:00.0",
            ):
                if candidate.exists():
                    device = _relpath(candidate, root)
                    break
        out.append(
            {
                "id": d.name,
                "name": name,
                "event_count": count,
                "device": device,
            }
        )
    return out


def acpi_wakeup_enabled(root="/"):
    root = pathlib.Path(root)
    text = _read(root / "proc/acpi/wakeup", "") or ""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] in ("Device",):
            continue
        out.append(
            {
                "name": parts[0],
                "state": parts[1],
                "enabled": parts[2] == "*enabled",
                "sysfs": parts[3] if len(parts) > 3 else "",
            }
        )
    return [entry for entry in out if entry["enabled"]]


def device_wakeups(root="/"):
    root = pathlib.Path(root)
    out = []
    for bus in ("pci", "usb", "mmc"):
        for path in _glob(root / "sys/bus" / bus / "devices", "*/power/wakeup"):
            if (_read(path, "") or "") != "enabled":
                continue
            out.append({"bus": bus, "id": path.parent.parent.name})
    return out


def rtc_devices(root="/"):
    root = pathlib.Path(root)
    out = []
    for d in _glob(root / "sys/class/rtc", "rtc[0-9]*"):
        out.append(
            {
                "name": d.name,
                "alarm": _read(d / "wakealarm", "") or "",
                "sysfs": str(d / "wakealarm"),
                "device": "/dev/" + d.name,
            }
        )
    return out


def mem_sleep(root="/"):
    return _read(pathlib.Path(root) / "sys/power/mem_sleep", "?")


def s0ix_us(root="/"):
    return _read_int(
        pathlib.Path(root) / "sys/kernel/debug/pmc_core/slp_s0_residency_usec"
    )


def _mean(values):
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 3) if values else None


def device_seconds(cycle):
    """``PM: suspend devices took`` / ``PM: resume devices took`` from the journal.

    These are the times the kernel itself spent walking the device tree at full
    power on both sides of the S0ix window; they are the part of the awake tail
    that can be attributed, and they are the reason a short cycle measures high.
    """
    suspend, resume = None, None
    for line in cycle.get("journal") or []:
        if not isinstance(line, str):
            continue
        match = re.search(r"PM: suspend devices took ([0-9.]+) seconds", line)
        if match:
            suspend = float(match.group(1))
        match = re.search(r"PM: resume devices took ([0-9.]+) seconds", line)
        if match:
            resume = float(match.group(1))
    return suspend, resume


def policy_snapshot(root="/"):
    """The deep-sleep policy in effect, plus what the boot service changed.

    Read-only: it calls the policy tool's pure ``plan()`` (which only reads) and
    loads the state file that ``oxp3-lowpower apply`` wrote.  ``available`` is
    false on a machine without the tool so an old capture still analyses.
    """
    if lowpower is None:
        return {"available": False, "changes": [], "latch": None}
    try:
        facts = lowpower.plan(root)["facts"]
    except Exception:  # pragma: no cover - defensive: never break a measurement
        return {"available": False, "changes": [], "latch": None}
    state = lowpower.load_state(pathlib.Path(root) / LOWPOWER_STATE)
    return {
        "available": True,
        "aspm": facts["aspm"],
        "mmc_wake": facts["mmc_wake"],
        "nvme": facts["nvme"],
        "audio": facts["audio"],
        "devices": facts["devices"],
        "sd_host": facts.get("sd_host"),
        "media": facts.get("media"),
        "park": facts.get("park"),
        "changes": state.get("changes") or [],
        "latch": lowpower.latched(state),
        "applied_at": state.get("applied_at"),
    }


def snapshot(root="/"):
    """One timestamped observation of everything the analyser needs."""
    root = pathlib.Path(root)
    bat = battery_info(root)
    temps = temperatures(root)
    pkg = temps.get("x86_pkg_temp")
    temps_max = max(temps.values()) if temps else None
    return {
        "ts": _iso(),
        "epoch": round(time.time(), 3),
        "energy_wh": bat["energy_wh"] if bat else None,
        "energy_full_wh": bat["energy_full_wh"] if bat else None,
        "pct": bat["pct"] if bat else None,
        "status": bat["status"] if bat else None,
        "on_ac": ac_online(root),
        "platform_profile": platform_profile(root),
        "pl1_uw": pl1_uw(root),
        "temp_pkg_c": pkg,
        "temp_max_c": temps_max,
        "temps": temps,
        "suspend_stats": suspend_stats(root),
        "wakeup_counts": wakeup_counts(root),
        "acpi_wakeup_enabled": acpi_wakeup_enabled(root),
        "device_wakeups": device_wakeups(root),
        "rtc_alarm": {d["name"]: d["alarm"] for d in rtc_devices(root)},
        "mem_sleep": mem_sleep(root),
        "s0ix_us": s0ix_us(root),
        "policy": policy_snapshot(root),
    }


# ------------------------------------------------------------------- journal
def read_journal(since_epoch, until_epoch, keywords=KERNEL_WAKE_KEYWORDS, limit=250):
    """First kernel wake-source lines for the resume window (best effort)."""
    if not shutil.which("journalctl"):
        return []
    lines = []
    for args in (
        ["journalctl", "-k", "-b", "--no-pager", "-o", "short-iso"],
        ["journalctl", "-t", "oxp3-sleep", "-b", "--no-pager", "-o", "short-iso"],
    ):
        cmd = args + [
            "--since=@%d" % max(0, int(since_epoch) - 2),
            "--until=@%d" % (int(until_epoch) + 5),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=25, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (proc.stdout or "").splitlines():
            if any(word in line for word in keywords):
                lines.append(line.rstrip())
    # The same line can appear through both invocations; keep the first copy.
    seen = set()
    unique = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return unique[:limit]


def journal_hints(lines):
    text = "\n".join(lines or [])
    wake_lines = [
        line
        for line in (lines or [])
        if "wake source" in line or "Wakeup" in line or "waking" in line.lower()
    ]
    return {
        "suspend_entry": "PM: suspend entry" in text,
        "suspend_exit": "PM: suspend exit" in text
        or "PM: suspend-to-idle exit" in text,
        "resumed": "PM: resume" in text or "Waking up" in text,
        "aborted": "aborting suspend" in text or "PM: suspend aborted" in text,
        "wake_lines": wake_lines[:20],
    }


def parse_wake_source_lines(lines):
    """Names from our own oxp3-sleep log lines: 'wake source <name> (+N): <path>'."""
    names = []
    for line in lines or []:
        match = re.search(r"wake source ([^()]+?)\s*\(\+\d+\)", line)
        if match:
            names.append(match.group(1).strip())
    return names


# -------------------------------------------------------------- pure analysis
def _ns_delta(cycle, clock):
    pair = cycle.get(clock) or {}
    before = pair.get("before")
    after = pair.get("after")
    if before is None or after is None:
        return None
    return (int(after) - int(before)) / 1e9


def total_seconds(cycle):
    return _ns_delta(cycle, "boottime_ns")


def awake_seconds(cycle):
    return _ns_delta(cycle, "monotonic_ns")


def slept_seconds(cycle):
    total = total_seconds(cycle)
    awake = awake_seconds(cycle)
    if total is None or awake is None:
        return None
    return max(0.0, total - awake)


def energy_delta_wh(cycle):
    before = (cycle.get("before") or {}).get("energy_wh")
    after = (cycle.get("after") or {}).get("energy_wh")
    if before is None or after is None:
        return None
    return round(float(before) - float(after), 6)


def energy_full_wh(cycle, fallback=None):
    before = (cycle.get("before") or {}).get("energy_full_wh")
    after = (cycle.get("after") or {}).get("energy_full_wh")
    return before or after or fallback


def drain_pct_per_h(delta_wh, full_wh, hours):
    if delta_wh is None or not full_wh or not hours or hours <= 0:
        return None
    return round((delta_wh / full_wh) / hours * 100.0, 3)


def drain_watts(delta_wh, hours):
    if delta_wh is None or not hours or hours <= 0:
        return None
    return round(delta_wh / hours, 3)


def settle_awake_w(cycle):
    """Awake power measured in the settle window right after the resume."""
    settle = cycle.get("settle") or {}
    seconds = settle.get("seconds")
    start = (cycle.get("after") or {}).get("energy_wh")
    end = settle.get("energy_wh")
    if not seconds or seconds <= 0 or start is None or end is None:
        return None
    return round((float(start) - float(end)) / (float(seconds) / 3600.0), 3)


def temp_rise_c(cycle):
    before = (cycle.get("before") or {}).get("temp_max_c")
    after = (cycle.get("after") or {}).get("temp_max_c")
    if before is None or after is None:
        return None
    return round(float(after) - float(before), 2)


def s0ix_seconds(cycle):
    before = (cycle.get("before") or {}).get("s0ix_us")
    after = (cycle.get("after") or {}).get("s0ix_us")
    if before is None or after is None:
        return None
    return round((int(after) - int(before)) / 1e6, 3)


def _stats_delta(cycle, key):
    before = ((cycle.get("before") or {}).get("suspend_stats") or {})
    after = ((cycle.get("after") or {}).get("suspend_stats") or {})
    if key not in before and key not in after:
        return None
    try:
        return int(after.get(key, 0)) - int(before.get(key, 0))
    except (TypeError, ValueError):
        return None


def wake_deltas(cycle):
    """Event-count deltas, one entry per wakeup source that moved."""
    before = {
        entry.get("id"): entry
        for entry in ((cycle.get("before") or {}).get("wakeup_counts") or [])
    }
    after = {
        entry.get("id"): entry
        for entry in ((cycle.get("after") or {}).get("wakeup_counts") or [])
    }
    out = []
    for ident, entry in after.items():
        old = before.get(ident) or {}
        try:
            delta = int(entry.get("event_count", 0)) - int(old.get("event_count", 0))
        except (TypeError, ValueError):
            continue
        if delta <= 0:
            continue
        out.append(
            {
                "id": ident,
                "name": entry.get("name") or old.get("name") or ident,
                "device": entry.get("device") or old.get("device") or "",
                "delta": delta,
            }
        )
    return sorted(out, key=lambda item: (-item["delta"], item["name"]))


def wake_kind(source):
    name = (source.get("name") or "").lower()
    device = (source.get("device") or "").lower()
    if "rtc" in name or "alarmtimer" in name or "acpi000e" in name or "rtc" in device:
        return "rtc"
    if name.startswith("pnp0c0c") or "lnxpwrbn" in name:
        return "power-button"
    if name.startswith("pnp0c0d"):
        return "lid"
    if name.startswith("pnp0c0e"):
        return "sleep-button"
    if re.match(r"^\d+-\d+$", name) or "/usb" in device:
        return "usb"
    if name == "adp1" or "acpi0003" in name:
        return "ac-adapter"
    if re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:", name) or "pci" in device:
        return "pci"
    return "other"


def source_label(source):
    name = source.get("name") or "?"
    return SOURCE_LABELS.get(name) or SOURCE_LABELS.get(name.split(":")[0]) or name


def classify_cycle_wake(cycle):
    """Who woke this cycle: counters first, our oxp3-sleep journal lines second."""
    deltas = wake_deltas(cycle)
    hints = journal_hints(cycle.get("journal"))
    if deltas:
        dominant = deltas[0]
        kind = wake_kind(dominant)
        others = deltas[1:]
        text = "%s (%s)" % (source_label(dominant), dominant["name"])
        short = "%s (%s)" % (source_label(dominant), dominant["name"])
        if others:
            text += " + " + ", ".join(item["name"] for item in others[:3])
            short += " +%d more" % len(others)
        kinds = {wake_kind(item) for item in deltas}
        return {
            "sources": deltas,
            "dominant": dominant,
            "kind": kind,
            "expected": kinds == {"rtc"},
            "text": text,
            "short": short,
            "from": "wakeup-counters",
        }
    parsed = parse_wake_source_lines(cycle.get("journal"))
    if parsed:
        source = {"id": "journal", "name": parsed[0], "device": "", "delta": 1}
        kind = wake_kind(source)
        return {
            "sources": [source],
            "dominant": source,
            "kind": kind,
            "expected": kind == "rtc",
            "text": "%s (from journal)" % source_label(source),
            "short": "%s (journal)" % source_label(source),
            "from": "journal",
        }
    return {
        "sources": [],
        "dominant": None,
        "kind": "unknown",
        "expected": None,
        "text": "no wakeup counter moved and no wake source was logged",
        "short": "unknown",
        "from": "none",
        "journal_hints": hints,
    }


def analyse_cycle(cycle, meta, thresholds):
    """Analyse one captured cycle into a table row plus a validity verdict."""
    requested = cycle.get("requested_s") or meta.get("sleep_s") or 60
    slept = slept_seconds(cycle)
    awake = awake_seconds(cycle)
    total = total_seconds(cycle)
    delta = energy_delta_wh(cycle)
    full = energy_full_wh(cycle, meta.get("energy_full_wh"))
    rise = temp_rise_c(cycle)
    awake_w = settle_awake_w(cycle)
    issues = []
    flags = {
        "energy_valid": True,
        "sleep_valid": True,
        "resume_valid": True,
    }

    rtcwake = cycle.get("rtcwake") or {}
    rc = rtcwake.get("rc")
    if rtcwake.get("timed_out"):
        flags["sleep_valid"] = False
        issues.append(
            {
                "code": "rtcwake-timeout",
                "severity": "failed",
                "text": "rtcwake did not return within the cycle deadline; the machine "
                "may have been suspended until the alarm and the timing is unreliable",
            }
        )
    if rc not in (None, 0):
        flags["sleep_valid"] = False
        issues.append(
            {
                "code": "rtcwake-failed",
                "severity": "failed",
                "text": "rtcwake exited %s: the machine did not complete a suspend" % rc,
            }
        )

    if total is None or slept is None:
        flags["sleep_valid"] = False
        issues.append(
            {
                "code": "no-boottime",
                "severity": "failed",
                "text": "missing CLOCK_BOOTTIME/CLOCK_MONOTONIC deltas for this cycle",
            }
        )
    elif slept < max(2.0, 0.5 * float(requested)):
        flags["sleep_valid"] = False
        issues.append(
            {
                "code": "did-not-sleep",
                "severity": "failed",
                "text": "slept %.1f s of the %s s requested (a suspend that aborted or "
                "never happened)" % (slept, requested),
            }
        )

    failed_resume = _stats_delta(cycle, "failed_resume")
    if failed_resume is not None and failed_resume > 0:
        flags["resume_valid"] = False
        issues.append(
            {
                "code": "failed-resume",
                "severity": "failed",
                "text": "suspend_stats failed_resume +%d (the resume path reported a "
                "failure)" % failed_resume,
            }
        )
    failed_suspend = _stats_delta(cycle, "failed_suspend")
    if failed_suspend is not None and failed_suspend > 0:
        flags["sleep_valid"] = False
        issues.append(
            {
                "code": "failed-suspend",
                "severity": "failed",
                "text": "suspend_stats failed_suspend +%d" % failed_suspend,
            }
        )
    success = _stats_delta(cycle, "success")
    stats_available = success is not None
    if stats_available and success < 1:
        flags["resume_valid"] = False
        issues.append(
            {
                "code": "success-not-counted",
                "severity": "failed",
                "text": "suspend_stats success did not increment; the kernel did not "
                "count a completed suspend",
            }
        )

    hints = journal_hints(cycle.get("journal"))
    if not hints["suspend_entry"]:
        issues.append(
            {
                "code": "no-suspend-entry",
                "severity": "warn",
                "text": "no 'PM: suspend entry' line in the journal window",
            }
        )
    if hints["aborted"]:
        flags["sleep_valid"] = False
        issues.append(
            {
                "code": "suspend-aborted",
                "severity": "failed",
                "text": "the kernel logged an aborted suspend",
            }
        )

    tolerance = float(thresholds["energy_increase_tolerance_wh"])
    effective_delta = delta
    if delta is None:
        flags["energy_valid"] = False
        issues.append(
            {
                "code": "no-battery",
                "severity": "invalid",
                "text": "no usable battery energy reading for this cycle",
            }
        )
    elif delta < -tolerance:
        flags["energy_valid"] = False
        issues.append(
            {
                "code": "energy-increased",
                "severity": "invalid",
                "text": "battery energy rose by %.3f Wh across the suspend "
                "(charging or a fuel-gauge reset, not a drain)" % (-delta),
            }
        )
    elif delta < 0:
        effective_delta = 0.0
        issues.append(
            {
                "code": "gauge-noise",
                "severity": "info",
                "text": "battery energy rose %.3f Wh (within the gauge-noise "
                "tolerance); counted as 0" % (-delta),
            }
        )

    if full is None:
        flags["energy_valid"] = False
        issues.append(
            {
                "code": "no-full-capacity",
                "severity": "invalid",
                "text": "battery energy_full is unreadable, so %/h cannot be computed",
            }
        )

    hours = (slept / 3600.0) if slept else None
    rate = drain_pct_per_h(effective_delta, full, hours)
    if rate is not None and rate > float(thresholds["implausible_pct_per_h"]):
        flags["energy_valid"] = False
        issues.append(
            {
                "code": "implausible-rate",
                "severity": "invalid",
                "text": "%.1f %%/h is above the %.1f %%/h plausibility ceiling; the "
                "energy reading or the suspended time is wrong"
                % (rate, thresholds["implausible_pct_per_h"]),
            }
        )

    if temp_rise_c(cycle) is None:
        issues.append(
            {
                "code": "no-temperature",
                "severity": "warn",
                "text": "no thermal zone reading; the temperature half of the verdict "
                "cannot be checked",
            }
        )

    # A cycle whose energy curve is impossible can still be a failed cycle; keep
    # the failed/invalid distinction visible in the row.
    if not flags["sleep_valid"] or not flags["resume_valid"]:
        status = "failed"
    elif not flags["energy_valid"]:
        status = "invalid"
    else:
        status = "ok"

    wake = classify_cycle_wake(cycle)
    suspend_devices, resume_devices = device_seconds(cycle)

    return {
        "index": cycle.get("index"),
        "requested_s": requested,
        "slept_s": round(slept, 2) if slept is not None else None,
        "awake_s": round(awake, 2) if awake is not None else None,
        "total_s": round(total, 2) if total is not None else None,
        "energy_wh": effective_delta,
        "energy_full_wh": full,
        "drain_pct_per_h": rate,
        "drain_w": drain_watts(effective_delta, hours),
        "temp_rise_c": rise,
        "awake_w": awake_w,
        "awake_cost_wh": round(awake_w * awake / 3600.0, 6)
        if awake_w is not None and awake is not None
        else None,
        "s0ix_s": s0ix_seconds(cycle),
        "suspend_devices_s": suspend_devices,
        "resume_devices_s": resume_devices,
        "wake": wake,
        "suspend_stats": {
            "success": success,
            "failed_suspend": failed_suspend,
            "failed_resume": failed_resume,
        },
        "rtcwake_rc": rc,
        "status": status,
        "flags": flags,
        "issues": issues,
        "journal": hints,
    }


def aggregate_wakes(rows):
    totals = {}
    for row in rows:
        for source in row["wake"].get("sources") or []:
            key = source["name"]
            entry = totals.setdefault(
                key,
                {"name": key, "device": source.get("device") or "", "delta": 0},
            )
            entry["delta"] += int(source.get("delta") or 0)
    ranked = sorted(totals.values(), key=lambda item: (-item["delta"], item["name"]))
    if not ranked:
        return {
            "sources": [],
            "dominant": None,
            "kind": "unknown",
            "expected": None,
            "text": "no wakeup source counter moved and no wake source was logged",
        }
    dominant = ranked[0]
    kind = wake_kind(dominant)
    label = source_label(dominant)
    if dominant["name"] in label:
        # e.g. "SD card reader (mmc0)" already carries the id
        parts = ["%s +%d" % (label, dominant["delta"])]
    else:
        parts = ["%s (%s) +%d" % (label, dominant["name"], dominant["delta"])]
    for extra in ranked[1:4]:
        extra_label = source_label(extra)
        if extra["name"] in extra_label:
            parts.append("%s +%d" % (extra_label, extra["delta"]))
        else:
            parts.append("%s (%s) +%d" % (extra_label, extra["name"],
                                          extra["delta"]))
    kinds = {wake_kind(source) for source in ranked}
    if kinds == {"rtc"}:
        text = "%s - the scheduled RTC alarm; expected" % " / ".join(parts)
        expected = True
    elif "rtc" in kinds:
        text = "%s - RTC plus another source; unexpected extra wake" % " / ".join(parts)
        expected = False
    else:
        text = "%s - not the RTC alarm; this is an unexpected wake source" % (
            " / ".join(parts)
        )
        expected = False
    return {
        "sources": ranked,
        "dominant": dominant,
        "kind": kind,
        "expected": expected,
        "text": text,
    }


def _thresholds(capture):
    merged = dict(DEFAULT_THRESHOLDS)
    meta = capture.get("meta") or {}
    merged.update(meta.get("thresholds") or {})
    return merged


def _decide(report):
    """GOOD / INVESTIGATE / FAILING / INVALID + exit code, with reasons."""
    th = report["thresholds"]
    meta = report["meta"]
    summary = report["summary"]
    rows = report["cycles"]
    reasons = []

    if report["mode"] != "run":
        return {
            "label": "NO-MEASUREMENT",
            "code": 2,
            "reasons": ["no suspend cycle was performed; this is a state report only"],
        }
    cleanup = meta.get("rtc_cleanup") or {}
    if cleanup and not cleanup.get("ok", True):
        return {
            "label": "INVALID",
            "code": 2,
            "reasons": [
                "the RTC alarm could not be verified clear after the run: %s"
                % cleanup.get("detail", "see capture.json")
            ],
        }
    if meta.get("on_ac"):
        return {
            "label": "INVALID",
            "code": 2,
            "reasons": [
                "the machine ran on AC, so the battery was not discharging: %/h is "
                "not a drain figure (unplug and repeat)"
            ],
        }
    failed = [row for row in rows if row["status"] == "failed"]
    invalid = [row for row in rows if row["status"] == "invalid"]
    if summary.get("valid_cycles", 0) == 0 and not failed:
        return {
            "label": "INVALID",
            "code": 2,
            "reasons": ["no cycle produced a usable measurement (see the per-cycle issues)"],
        }
    if failed:
        reasons.append(
            "cycle(s) %s failed: %s"
            % (
                ", ".join(str(row["index"]) for row in failed),
                "; ".join(
                    issue["text"]
                    for row in failed
                    for issue in row["issues"]
                    if issue["severity"] == "failed"
                ),
            )
        )
    if invalid:
        reasons.append(
            "cycle(s) %s are not usable for drain: %s"
            % (
                ", ".join(str(row["index"]) for row in invalid),
                "; ".join(
                    issue["text"]
                    for row in invalid
                    for issue in row["issues"]
                    if issue["severity"] == "invalid"
                ),
            )
        )
    partial = summary.get("cycles_completed", 0) < meta.get("cycles_requested", 0)
    if partial:
        reasons.append(
            "only %s of the %s requested cycles completed"
            % (summary.get("cycles_completed", 0), meta.get("cycles_requested", 0))
        )

    # Judge the suspended portion when available: the window total includes the
    # awake tail between cycles (rtcwake overhead plus the settle window).
    rate = summary.get("drain_pct_per_h")
    sleep_rate = summary.get("sleep_only_pct_per_h")
    rise = summary.get("temp_rise_max_c")
    uncertainty = summary.get("uncertainty_pct_per_h")
    if failed:
        # A suspend/resume failure is a failure even when no rate is computable.
        return {"label": "FAILING", "code": 1, "reasons": reasons}
    if rate is None:
        return {
            "label": "INVALID",
            "code": 2,
            "reasons": reasons + ["the drain rate could not be computed"],
        }
    if rate > float(th["investigate_pct_per_h"]) or \
            (sleep_rate is not None and sleep_rate > float(th["investigate_pct_per_h"])):
        reasons.append(
            "%.2f %%/h is above the %.1f %%/h failing threshold"
            % (rate, th["investigate_pct_per_h"])
        )
        return {"label": "FAILING", "code": 1, "reasons": reasons}
    # The virtual mmc wakeup source has no device link and wakeup_count stays 0,
    # so it never woke the machine; the report names it but it is not an extra wake.
    unexpected = [
        row
        for row in rows
        if row["status"] != "invalid"
        and row["wake"].get("expected") is False
        and "SD card reader (mmc0)" not in row["wake"].get("text", "")
    ]
    rate_over = rate >= float(th["good_pct_per_h"]) and not (
        sleep_rate is not None and sleep_rate < float(th["good_pct_per_h"]))
    if (
        rate_over
        or (rise is not None and rise >= float(th["good_temp_rise_c"]))
        or unexpected
        or partial
        or invalid
    ):
        if rate_over:
            reasons.append(
                "%.2f %%/h is at or above the %.1f %%/h target"
                % (rate, th["good_pct_per_h"])
            )
        if rise is not None and rise >= float(th["good_temp_rise_c"]):
            reasons.append(
                "temperature rose %.1f C (target under %.1f C)"
                % (rise, th["good_temp_rise_c"])
            )
        for row in unexpected:
            reasons.append("cycle %s: woke on %s" % (row["index"], row["wake"]["text"]))
        return {"label": "INVESTIGATE", "code": 1, "reasons": reasons}

    if (rate >= float(th["good_pct_per_h"]) and sleep_rate is not None
            and sleep_rate < float(th["good_pct_per_h"])):
        reasons.append(
            "the window total includes the awake tail: sleep-only %.2f %%/h is "
            "within the %.1f %%/h target" % (sleep_rate, th["good_pct_per_h"]))
    if uncertainty is not None and rate is not None:
        reasons.append(
            "%.2f +/- %.2f %%/h (the window is short; the fuel gauge step dominates "
            "the error)" % (rate, uncertainty)
        )
    return {"label": "GOOD", "code": 0, "reasons": reasons or ["within thresholds"]}


def _run_summary(rows, meta):
    usable = [
        row
        for row in rows
        if row["flags"]["energy_valid"] and row["flags"]["sleep_valid"]
    ]
    slept_usable = sum(row["slept_s"] or 0 for row in usable)
    slept_total = sum(
        row["slept_s"] or 0 for row in rows if row["flags"]["sleep_valid"]
    )
    energy = sum(row["energy_wh"] or 0 for row in usable)
    awake_total = sum(row["awake_s"] or 0 for row in rows)
    full = None
    for row in rows:
        full = full or row.get("energy_full_wh")
    hours = slept_usable / 3600.0 if slept_usable else None
    rate = drain_pct_per_h(energy, full, hours)
    rise_values = [
        row["temp_rise_c"] for row in rows if row["temp_rise_c"] is not None
    ]
    gauge = float(_thresholds({"meta": meta})["gauge_step_wh"])
    uncertainty = None
    if full and hours:
        # one gauge LSB over the whole suspended window
        uncertainty = round((gauge / full) / hours * 100.0, 3)
    awake_ws = [row["awake_w"] for row in rows if row["awake_w"] is not None]
    awake_w = round(sum(awake_ws) / len(awake_ws), 3) if awake_ws else None
    awake_cost = (
        round(awake_w * awake_total / 3600.0, 6) if awake_w is not None else None
    )
    s0ix_pairs = [
        (row["s0ix_s"], row["slept_s"])
        for row in rows
        if row["s0ix_s"] is not None and row["slept_s"]
    ]
    s0ix_total = sum(pair[0] for pair in s0ix_pairs) if s0ix_pairs else None
    s0ix_ratio = None
    if s0ix_pairs:
        slept_pairs = sum(pair[1] for pair in s0ix_pairs)
        if slept_pairs > 0:
            s0ix_ratio = round(s0ix_total / slept_pairs, 3)
    # The energy delta is taken around the *whole* cycle, so the measured awake
    # tail is inside it.  Report both numbers instead of pretending the sleep
    # rate is the cycle rate: the tail is what a short cycle is made of, and it
    # is what the suspend/resume device times below are spent on.
    sleep_energy = (
        max(0.0, round(energy - awake_cost, 6))
        if awake_cost is not None and energy is not None
        else None
    )
    sleep_only_w = drain_watts(sleep_energy, hours) if sleep_energy is not None else None
    sleep_only_rate = (
        drain_pct_per_h(sleep_energy, full, hours)
        if sleep_energy is not None
        else None
    )
    awake_share = (
        round(100.0 * awake_cost / energy, 1)
        if awake_cost is not None and energy
        else None
    )
    return {
        "cycles_completed": len(rows),
        "valid_cycles": len(usable),
        "failed_cycles": sum(1 for row in rows if row["status"] == "failed"),
        "invalid_cycles": sum(1 for row in rows if row["status"] == "invalid"),
        "suspended_s": round(slept_total, 2),
        "suspended_usable_s": round(slept_usable, 2),
        "awake_s": round(awake_total, 2),
        "energy_wh": round(energy, 6),
        "energy_full_wh": full,
        "drain_pct_per_h": rate,
        "drain_w": drain_watts(energy, hours),
        "uncertainty_pct_per_h": uncertainty,
        "temp_rise_max_c": round(max(rise_values), 2) if rise_values else None,
        "awake_w": awake_w,
        "awake_cost_wh": awake_cost,
        "awake_share_pct": awake_share,
        "sleep_only_energy_wh": sleep_energy,
        "sleep_only_w": sleep_only_w,
        "sleep_only_pct_per_h": sleep_only_rate,
        "suspend_devices_s": _mean([row["suspend_devices_s"] for row in rows]),
        "resume_devices_s": _mean([row["resume_devices_s"] for row in rows]),
        "s0ix_s": s0ix_total,
        "s0ix_ratio": s0ix_ratio,
        "on_ac": meta.get("on_ac"),
    }


def analyse(capture, thresholds=None):
    """Turn one capture document into the stable report document."""
    thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    meta_capture = capture.get("meta") or {}
    capture_thresholds = dict(DEFAULT_THRESHOLDS)
    capture_thresholds.update(meta_capture.get("thresholds") or {})
    capture_thresholds.update(thresholds)
    report_thresholds = capture_thresholds

    rows = []
    for cycle in capture.get("cycles") or []:
        rows.append(analyse_cycle(cycle, meta_capture, report_thresholds))

    summary = _run_summary(rows, meta_capture)
    wake = aggregate_wakes(rows)
    report = {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "generated_at": _iso(),
        "mode": capture.get("mode") or "run",
        "meta": meta_capture,
        "baseline": capture.get("baseline"),
        "cycles": rows,
        "summary": summary,
        "wake": wake,
        "policy": capture.get("policy") or {},
        "thresholds": report_thresholds,
        "verdict": {},
        "notes": list(capture.get("notes") or []),
    }
    report["verdict"] = _decide(report)
    return report


# ------------------------------------------------------------------ rendering
def _fmt(value, spec, missing="n/a"):
    return missing if value is None else format(value, spec)


def render_policy_lines(policy):
    """The machine-readable policy block, rendered for a human.

    This is the answer to "what changed and why": every row was written by
    ``oxp3-lowpower apply``, with its pre-image, its reason and the hypothesis it
    is testing, so the next run can show whether it helped.
    """
    if not policy:
        return ["   deep-sleep policy: no snapshot in this capture (taken before "
                "the low-power policy was installed)"]
    if not policy.get("available"):
        return ["   deep-sleep policy: not installed (no tools/oxp3_lowpower.py "
                "state on this host)"]
    lines = []
    aspm = policy.get("aspm") or {}
    mmc = policy.get("mmc_wake") or []
    nvme = policy.get("nvme") or {}
    mmc_state = ", ".join(
        "%s wake=%s" % (entry.get("name"), "disabled" if entry.get("enabled") is False
                        else ("armed" if entry.get("enabled") else "unknown"))
        for entry in mmc
    ) or "none present"
    park = policy.get("park") or {}
    if park.get("action"):
        lines.append(
            "   SD host park: %s (%s)"
            % (park.get("action"), park.get("reason") or "no reason recorded")
        )
    lines.append(
        "   deep-sleep policy: aspm=%s  %s  nvme ps_max_latency=%s us%s"
        % (
            aspm.get("selected") or "?",
            mmc_state,
            nvme.get("default_ps_max_latency_us")
            if nvme.get("default_ps_max_latency_us") is not None else "?",
            "  force_apst=%s" % nvme.get("force_apst")
            if nvme.get("force_apst") else "",
        )
    )
    reader = next(
        (entry for entry in policy.get("devices") or []
         if entry.get("addr") == "0000:56:00.0"), None
    )
    audio = policy.get("audio") or {}
    facts = []
    if reader:
        facts.append("card reader control=%s runtime=%s"
                     % (reader.get("control") or "?", reader.get("runtime_status") or "?"))
    if audio.get("present"):
        facts.append("audio (SOF) control=%s runtime=%s"
                     % (audio.get("control") or "?", audio.get("runtime_status") or "?"))
    if facts:
        lines.append("   recorded state: %s" % "; ".join(facts))
    for entry in mmc:
        stats = entry.get("stats") or {}
        if (stats.get("event_count") or 0) <= 0:
            continue
        if stats.get("wakeup_count") in (0, None):
            lines.append(
                "   mmc note: %s counted %s event(s) with wakeup_count=%s - it did "
                "not abort a suspend; the source was active %s s in total, %s s at "
                "most, which matches 'PM: suspend devices took'"
                % (
                    entry.get("name"),
                    stats.get("event_count"),
                    stats.get("wakeup_count"),
                    _fmt((stats.get("total_time_ms") or 0) / 1000.0, ".1f"),
                    _fmt((stats.get("max_time_ms") or 0) / 1000.0, ".1f"),
                )
            )
    applied = [row for row in policy.get("changes") or [] if row.get("applied")]
    if applied:
        for row in applied:
            lines.append(
                "   changed: %s %s -> %s (%s; expected: %s)"
                % (row.get("id"), row.get("before"), row.get("after"),
                   row.get("why") or "no reason recorded",
                   row.get("expected") or "no expectation recorded")
            )
    elif policy.get("changes"):
        lines.append(
            "   changed: nothing applied this boot (%d decision(s) recorded, all "
            "skipped or already in place)"
            % len(policy.get("changes") or [])
        )
    latch = policy.get("latch") or {}
    if latch.get("reason"):
        lines.append("   policy latched off: %s" % latch["reason"])
    return lines


def render_text(report):
    """The human report: table first, then counters, wake attribution, verdict."""
    meta = report.get("meta") or {}
    summary = report.get("summary") or {}
    th = report.get("thresholds") or DEFAULT_THRESHOLDS
    lines = []
    lines.append(
        "== sleep-cycle-test %s  mode=%s  kernel=%s  root=%s"
        % (
            report.get("generated_at"),
            report.get("mode"),
            meta.get("kernel", "?"),
            "yes" if meta.get("root") else "no",
        )
    )
    baseline = report.get("baseline") or {}
    if baseline:
        lines.append(
            "   baseline: %s%% (%s of %s Wh, %s, %s)  profile=%s  PL1=%s  pkg=%s C  "
            "ACPI wake armed=%s  device wake bits=%s"
            % (
                _fmt(baseline.get("pct"), "d"),
                _fmt(baseline.get("energy_wh"), ".3f"),
                _fmt(baseline.get("energy_full_wh"), ".3f"),
                baseline.get("status") or "?",
                "AC" if baseline.get("on_ac") else "battery",
                baseline.get("platform_profile") or "?",
                "%s W" % round(baseline["pl1_uw"] / 1e6, 1)
                if baseline.get("pl1_uw")
                else "?",
                _fmt(baseline.get("temp_pkg_c"), ".1f"),
                len(baseline.get("acpi_wakeup_enabled") or []),
                len(baseline.get("device_wakeups") or []),
            )
        )
        armed = [item["name"] for item in baseline.get("acpi_wakeup_enabled") or []]
        if armed:
            lines.append("   armed ACPI wake sources: %s" % " ".join(armed))
        bits = baseline.get("device_wakeups") or []
        if bits:
            shown = " ".join(item["id"] for item in bits[:10])
            lines.append(
                "   device wake bits: %s%s"
                % (shown, " ..." if len(bits) > 10 else "")
            )

    lines.append("")
    if report.get("cycles"):
        lines.append(
            "    #  slept      awake     energy       drain        power    pkg rise   wake"
        )
        lines.append(
            "   --  ---------  --------  -----------  -----------  -------  ---------  ----"
        )
        for row in report["cycles"]:
            wake = row["wake"]
            lines.append(
                "   %2s  %8s  %7s  %10s  %10s  %6s  %8s   %s"
                % (
                    row["index"],
                    "%s s" % _fmt(row["slept_s"], ".1f"),
                    "%s s" % _fmt(row["awake_s"], ".1f"),
                    "%s Wh" % _fmt(row["energy_wh"], ".3f"),
                    "%s %%/h" % _fmt(row["drain_pct_per_h"], ".2f"),
                    "%s W" % _fmt(row["drain_w"], ".2f"),
                    "%+.1f C" % row["temp_rise_c"]
                    if row["temp_rise_c"] is not None
                    else "n/a",
                    wake.get("short") or wake["text"],
                )
            )
        lines.append("")
        lines.append(
            "   totals: %s usable s suspended (%s s completed), %s s awake, "
            "%s Wh => %s %%/h (%s W)"
            % (
                _fmt(summary.get("suspended_usable_s"), ".1f"),
                _fmt(summary.get("suspended_s"), ".1f"),
                _fmt(summary.get("awake_s"), ".1f"),
                _fmt(summary.get("energy_wh"), ".3f"),
                _fmt(summary.get("drain_pct_per_h"), ".2f"),
                _fmt(summary.get("drain_w"), ".2f"),
            )
        )
        if summary.get("uncertainty_pct_per_h") is not None:
            lines.append(
                "   gauge: +/- %s %%/h from one %.2f Wh fuel-gauge step over this window"
                % (
                    summary["uncertainty_pct_per_h"],
                    th.get("gauge_step_wh", 0.01),
                )
            )
        if summary.get("awake_cost_wh") is not None:
            lines.append(
                "   awake tail: %s s at %s W = %s Wh (%s%% of the %s Wh measured; "
                "included in the %%/h above)"
                % (
                    _fmt(summary.get("awake_s"), ".1f"),
                    _fmt(summary.get("awake_w"), ".2f"),
                    _fmt(summary.get("awake_cost_wh"), ".3f"),
                    _fmt(summary.get("awake_share_pct"), ".0f"),
                    _fmt(summary.get("energy_wh"), ".3f"),
                )
            )
        if (summary.get("awake_share_pct") or 0) >= 100:
            lines.append(
                "   the awake tail is as large as the whole measured energy: the "
                "%s s settle window cannot separate the two, so read the split "
                "with --sleep 300 or longer" % _fmt(summary.get("awake_s"), ".0f")
            )
        if summary.get("sleep_only_pct_per_h") is not None:
            lines.append(
                "   sleep-only: %s Wh over %s s => %s %%/h (%s W) with the awake "
                "tail removed"
                % (
                    _fmt(summary.get("sleep_only_energy_wh"), ".3f"),
                    _fmt(summary.get("suspended_usable_s"), ".1f"),
                    _fmt(summary.get("sleep_only_pct_per_h"), ".2f"),
                    _fmt(summary.get("sleep_only_w"), ".2f"),
                )
            )
        if summary.get("suspend_devices_s") is not None:
            lines.append(
                "   device time: suspend %s s + resume %s s per cycle (kernel "
                "journal), spent at awake power"
                % (
                    _fmt(summary.get("suspend_devices_s"), ".2f"),
                    _fmt(summary.get("resume_devices_s"), ".2f"),
                )
            )
        if summary.get("s0ix_ratio") is not None:
            lines.append(
                "   S0ix residency: %s s of %s s slept (%s)"
                % (
                    _fmt(summary.get("s0ix_s"), ".1f"),
                    _fmt(summary.get("suspended_s"), ".1f"),
                    "%d%%" % round(summary["s0ix_ratio"] * 100)
                    if summary.get("s0ix_ratio") is not None
                    else "n/a",
                )
            )
        stats = []
        for key, label in (
            ("success", "success"),
            ("fail", "fail"),
            ("failed_suspend", "failed_suspend"),
            ("failed_resume", "failed_resume"),
        ):
            total = 0
            seen = False
            for row in report["cycles"]:
                value = (row.get("suspend_stats") or {}).get(key)
                if value is not None:
                    seen = True
                    total += value
            if seen:
                stats.append("%s +%d" % (label, total))
        if stats:
            lines.append("   suspend_stats: %s" % ", ".join(stats))

    policy_lines = render_policy_lines(report.get("policy") or {})
    if policy_lines:
        lines.append("")
        lines.extend(policy_lines)

    wake = report.get("wake") or {}
    if report.get("cycles"):
        lines.append("")
        lines.append("   wake attribution: %s" % (wake.get("text") or "unknown"))
        for row in report["cycles"]:
            issues = [
                issue for issue in row["issues"] if issue["severity"] != "info"
            ]
            for issue in issues:
                lines.append(
                    "   cycle %s: %s" % (row["index"], issue["text"])
                )

    verdict = report.get("verdict") or {}
    lines.append("")
    lines.append(
        "== verdict: %s - thresholds: GOOD < %s %%/h and < %s C rise; "
        "FAILING > %s %%/h"
        % (
            verdict.get("label"),
            th.get("good_pct_per_h"),
            th.get("good_temp_rise_c"),
            th.get("investigate_pct_per_h"),
        )
    )
    for reason in verdict.get("reasons") or []:
        lines.append("   - %s" % reason)
    for note in report.get("notes") or []:
        lines.append("   note: %s" % note)
    return "\n".join(lines)


def render_state_text(report):
    """The --no-suspend report: armed state only, explicitly no drain claim."""
    meta = report.get("meta") or {}
    baseline = report.get("baseline") or {}
    lines = []
    lines.append(
        "== sleep-cycle-test %s  mode=no-suspend  kernel=%s"
        % (report.get("generated_at"), meta.get("kernel", "?"))
    )
    lines.append(
        "   nothing was armed, suspended or written to /sys; this is the state the "
        "next run would start from"
    )
    if baseline:
        lines.append(
            "   battery: %s%% (%s of %s Wh, %s)  AC=%s  profile=%s  PL1=%s  pkg=%s C"
            % (
                _fmt(baseline.get("pct"), "d"),
                _fmt(baseline.get("energy_wh"), ".3f"),
                _fmt(baseline.get("energy_full_wh"), ".3f"),
                baseline.get("status") or "?",
                baseline.get("on_ac"),
                baseline.get("platform_profile") or "?",
                "%s W" % round(baseline["pl1_uw"] / 1e6, 1)
                if baseline.get("pl1_uw")
                else "?",
                _fmt(baseline.get("temp_pkg_c"), ".1f"),
            )
        )
        armed = baseline.get("acpi_wakeup_enabled") or []
        lines.append(
            "   ACPI wake sources enabled (%d): %s"
            % (len(armed), " ".join(item["name"] for item in armed) or "none")
        )
        bits = baseline.get("device_wakeups") or []
        shown = " ".join(item["id"] for item in bits[:12])
        lines.append(
            "   device wake bits enabled (%d): %s%s"
            % (len(bits), shown or "none", " ..." if len(bits) > 12 else "")
        )
        rtcs = baseline.get("rtc_alarm") or {}
        if rtcs:
            lines.append(
                "   RTC interfaces: %s"
                % ", ".join(
                    "%s%s" % (name, " (alarm armed)" if value else " (clear)")
                    for name, value in rtcs.items()
                )
            )
        else:
            lines.append(
                "   RTC interfaces: none with a wakealarm attribute - the timed-wake "
                "path is missing (see docs/SLEEP-TEST.md fallbacks)"
            )
        lines.append("   mem_sleep: %s" % (baseline.get("mem_sleep") or "?"))
        stats = baseline.get("suspend_stats") or {}
        if stats:
            lines.append(
                "   suspend_stats: success=%s fail=%s failed_resume=%s "
                "total_hw_sleep=%s ns"
                % (
                    stats.get("success", "?"),
                    stats.get("fail", "?"),
                    stats.get("failed_resume", "?"),
                    stats.get("total_hw_sleep", "?"),
                )
            )
    policy_lines = render_policy_lines(report.get("policy") or {})
    if policy_lines:
        lines.append("")
        lines.extend(policy_lines)
    lines.append("")
    for note in report.get("notes") or []:
        lines.append("   note: %s" % note)
    lines.append(
        "== verdict: %s - no drain figure was produced; run without --no-suspend "
        "for the measurement" % (report.get("verdict", {}).get("label") or "NO-MEASUREMENT")
    )
    return "\n".join(lines)


# ------------------------------------------------------------------- commands
def _utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def output_dir(opts):
    if opts.output:
        return pathlib.Path(opts.output)
    return STATE_DIR / ("sleep-cycles-" + _utc_stamp())


def write_artifacts(outdir, capture, report):
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    _json_write(outdir / "capture.json", capture)
    report["artifacts"] = {
        "capture": str(outdir / "capture.json"),
        "report_json": str(outdir / "report.json"),
        "report_text": str(outdir / "report.txt"),
    }
    _json_write(outdir / "report.json", report)
    text = (
        render_state_text(report)
        if report.get("mode") == "no-suspend"
        else render_text(report)
    )
    (outdir / "report.txt").write_text(text + "\n", encoding="utf-8")
    return report["artifacts"]


def _usage_error(message):
    print("%s: %s (try --help)" % (PROG, message), file=sys.stderr)
    return 3


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        raise SystemExit(_usage_error(message))


def _check_ranges(opts, check_estimate=True):
    if not 1 <= opts.cycles <= MAX_CYCLES:
        return "cycles must be 1..%d" % MAX_CYCLES
    if not MIN_SLEEP_S <= opts.sleep_s <= MAX_SLEEP_S:
        return "sleep must be %d..%d s" % (MIN_SLEEP_S, MAX_SLEEP_S)
    if not 0 <= opts.settle_s <= MAX_SETTLE_S:
        return "settle must be 0..%d s" % MAX_SETTLE_S
    if opts.max_runtime_s < 60:
        return "max-runtime must be at least 60 s"
    if not check_estimate:
        return None
    estimate = estimate_runtime(opts)
    if estimate > opts.max_runtime_s:
        return (
            "estimated runtime %d s exceeds --max-runtime %d s "
            "(raise it or use fewer/shorter cycles)"
            % (estimate, opts.max_runtime_s)
        )
    return None


def estimate_runtime(opts):
    return 10 + opts.cycles * (opts.sleep_s + opts.settle_s + 15)


def _preflight(opts, root="/"):
    """Collect setup errors (3), measurement-invalid conditions (2), warnings."""
    errors = []
    warnings = []
    if os.geteuid() != 0:
        errors.append(
            (
                3,
                "real cycles need root (rtcwake and suspend): "
                "sudo tools/sleep-cycle-test.sh --cycles %d" % opts.cycles,
            )
        )
    if not shutil.which("rtcwake"):
        errors.append(
            (
                3,
                "rtcwake not found (util-linux). Install util-linux or use "
                "--no-suspend/--dry-run.",
            )
        )
    rtcs = rtc_devices(root)
    if not rtcs:
        errors.append(
            (
                3,
                "no /sys/class/rtc/rtcN/wakealarm interface: a guaranteed timed wake "
                "cannot be armed, so cycling is refused. Fallbacks: another rtcN, an "
                "external wake, or --no-suspend (see docs/SLEEP-TEST.md).",
            )
        )
    for device in rtcs:
        armed = (device.get("alarm") or "").strip()
        if armed and armed != "0":
            warnings.append(
                "RTC %s already has an armed alarm (%s); the run replaces it and "
                "the cleanup path clears it" % (device["name"], armed)
            )
    if opts.rtc:
        wanted = opts.rtc.replace("/dev/", "")
        available = [device["name"] for device in rtc_devices(root)]
        if wanted not in available:
            errors.append(
                (
                    3,
                    "RTC %r not found (available: %s)"
                    % (opts.rtc, ", ".join(available) or "none"),
                )
            )
    sleep = mem_sleep(root)
    if "[s2idle]" not in (sleep or ""):
        errors.append(
            (
                3,
                "/sys/power/mem_sleep is %r, not [s2idle]; this tool measures s2idle "
                "only" % sleep,
            )
        )
    bat = battery_info(root)
    if bat is None:
        errors.append(
            (
                3,
                "no readable battery energy_now/charge_now: drain cannot be measured",
            )
        )
    on_ac = ac_online(root)
    if on_ac is None:
        warnings.append(
            "no Mains/AC power-supply node: the tool cannot prove the machine is on "
            "battery, so the drain figure may include charging"
        )
    if bat is not None and (bat.get("status") or "") not in ("Discharging",) and not on_ac:
        warnings.append(
            "battery status is %r, not Discharging: the energy delta may be flat or "
            "meaningless" % (bat.get("status") or "?")
        )
    if on_ac:
        if not opts.allow_ac:
            errors.append(
                (
                    2,
                    "the machine is on AC: the battery is not discharging, so %/h "
                    "would be meaningless. Unplug and repeat, or pass --allow-ac to "
                    "run the cycles with the drain marked invalid.",
                )
            )
        else:
            warnings.append(
                "on AC (--allow-ac): cycles will run but the drain verdict is INVALID"
            )
    awac = [item for item in acpi_wakeup_enabled(root) if item["name"] == "AWAC"]
    if not awac:
        warnings.append(
            "ACPI wake alarm (AWAC) is not enabled in /proc/acpi/wakeup; the RTC "
            "alarm may not wake the machine (toggle with: echo AWAC > "
            "/proc/acpi/wakeup, then verify)"
        )
    armed = acpi_wakeup_enabled(root)
    if len(armed) > 4:
        warnings.append(
            "%d ACPI wake sources are armed (%s); a spurious wake from one of them "
            "will be attributed, not prevented (the policy is R2 in "
            "docs/hw/sleep-power.md)" % (len(armed), " ".join(a["name"] for a in armed))
        )
    if shutil.which("systemd-inhibit"):
        try:
            proc = subprocess.run(
                ["systemd-inhibit", "--list", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                if re.search(r"\bblock\b", line) and " sleep " in line:
                    warnings.append(
                        "a block inhibitor is held on sleep: %s (rtcwake writes "
                        "/sys/power/state directly, so it still suspends, but logind "
                        "requests would not)" % " ".join(line.split()[:4])
                    )
        except (OSError, subprocess.SubprocessError):
            pass
    return errors, warnings


def _print_preflight(errors, warnings, opts):
    commands = [
        ("rtcwake -m mem -s %d -d %s -v"
         if getattr(opts, "direct", False) else
         "rtcwake -m no -s %d -d %s -v && systemctl suspend")
        % (opts.sleep_s, opts.rtc or "rtc0"),
        "journalctl -k -b --since=@<cycle-start-epoch> --until=@<resume-epoch> "
        "--no-pager -o short-iso",
        "rtcwake -m disable -d %s" % (opts.rtc or "rtc0"),
        "sh -c 'echo 0 > /sys/class/rtc/%s/wakealarm'" % (opts.rtc or "rtc0"),
    ]
    for code, message in errors:
        print("   %s: %s" % ("REFUSE" if code != 2 else "INVALID", message))
    for message in warnings:
        print("   WARN: %s" % message)
    print("   exact commands:")
    for command in commands:
        print("     %s" % command)
    return errors


def _plan_text(opts, root="/"):
    rtcs = rtc_devices(root)
    chosen = opts.rtc
    if not chosen:
        chosen = rtcs[0]["name"] if rtcs else "rtc0"
    lines = [
        "   plan: %d cycle(s) x %d s s2idle, settle %d s, estimated runtime %d s "
        "(limit %d s)"
        % (opts.cycles, opts.sleep_s, opts.settle_s, estimate_runtime(opts),
           opts.max_runtime_s),
        "   each cycle: baseline snapshot -> arm RTC + suspend -> resume -> "
        "CLOCK_BOOTTIME/MONOTONIC deltas -> suspend_stats -> wake counters + journal "
        "-> settle %d s for the awake cost" % opts.settle_s,
        "   rtc: %s (%s); output: %s"
        % (
            chosen,
            ", ".join(d["name"] for d in rtcs) or "none",
            output_dir(opts),
        ),
    ]
    return lines, chosen


def _confirm(opts):
    if opts.yes:
        print(
            "   confirmation skipped by --yes (this is a deliberate scripted run)",
            file=sys.stderr,
        )
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "%s: not a TTY - refusing to suspend without an explicit confirmation. "
            "Run it from a terminal, or pass --yes." % PROG,
            file=sys.stderr,
        )
        return False
    prompt = (
        "Type SUSPEND to run %d sleep cycle(s) of %d s (the machine will suspend and "
        "must wake by RTC): " % (opts.cycles, opts.sleep_s)
    )
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip() == "SUSPEND"


# ------------------------------------------------------------------ RTC/trap
def choose_rtc(opts, root="/"):
    devices = rtc_devices(root)
    if not devices:
        return None, devices
    wanted = opts.rtc
    if wanted:
        wanted = wanted.replace("/dev/", "")
        for device in devices:
            if device["name"] == wanted:
                return device, devices
        return None, devices
    return devices[0], devices


def alarm_armed(rtc):
    if not rtc:
        return False
    value = _read(pathlib.Path(rtc["sysfs"]), "") or ""
    value = value.strip()
    return bool(value) and value != "0"


def disarm_rtc(rtc):
    """Disarm a pending alarm and verify it; returns an auditable record."""
    record = {"ok": True, "attempts": [], "detail": "no RTC device"}
    if not rtc:
        return record
    if not alarm_armed(rtc):
        record["detail"] = "no alarm was armed"
        return record
    record["attempts"].append("alarm was armed: %r" % _read(rtc["sysfs"], ""))
    if shutil.which("rtcwake"):
        try:
            proc = subprocess.run(
                ["rtcwake", "-m", "disable", "-d", rtc["name"]],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            record["attempts"].append(
                "rtcwake -m disable -d %s -> rc=%d %s"
                % (rtc["name"], proc.returncode, (proc.stderr or "").strip()[:200])
            )
        except (OSError, subprocess.SubprocessError) as exc:
            record["attempts"].append("rtcwake -m disable failed: %s" % exc)
    if not alarm_armed(rtc):
        record["detail"] = "disarmed with rtcwake -m disable"
        return record
    try:
        pathlib.Path(rtc["sysfs"]).write_text("0\n", encoding="utf-8")
        record["attempts"].append("wrote 0 to %s" % rtc["sysfs"])
    except OSError as exc:
        record["attempts"].append("writing 0 to %s failed: %s" % (rtc["sysfs"], exc))
    if alarm_armed(rtc):
        record["ok"] = False
        record["detail"] = (
            "the alarm is still armed after rtcwake and a sysfs write; run "
            "'sudo rtcwake -m disable -d %s' by hand" % rtc["name"]
        )
    else:
        record["detail"] = "disarmed with a direct sysfs write"
    return record


def _signal_handler(signum, _frame):
    raise KeyboardInterrupt


def install_signal_handlers():
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            pass


# ------------------------------------------------------------------- run mode
def cmd_run(opts):
    errors, warnings = _preflight(opts)
    print(
        "== sleep-cycle-test %s  mode=run  cycles=%d  sleep=%d s  settle=%d s"
        % (_iso(), opts.cycles, opts.sleep_s, opts.settle_s)
    )
    lines, rtc_name = _plan_text(opts)
    for line in lines:
        print(line)
    if errors:
        _print_preflight(errors, warnings, opts)
        return max(code for code, _ in errors) if errors else 2
    for warning in warnings:
        print("   WARN: %s" % warning)
    rtc, devices = choose_rtc(opts)
    if rtc is None:
        return _usage_error(
            "RTC %r not found (available: %s)"
            % (opts.rtc or "?", ", ".join(d["name"] for d in devices) or "none")
        )
    if not _confirm(opts):
        print("%s: run not confirmed; nothing was armed or suspended" % PROG,
              file=sys.stderr)
        return 3

    install_signal_handlers()
    capture = {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "mode": "run",
        "notes": list(warnings),
        "meta": {
            "started_at": _iso(),
            "kernel": _read("/proc/sys/kernel/osrelease", "?"),
            "hostname": _read("/proc/sys/kernel/hostname", "?"),
            "root": os.geteuid() == 0,
            "cycles_requested": opts.cycles,
            "sleep_s": opts.sleep_s,
            "settle_s": opts.settle_s,
            "max_runtime_s": opts.max_runtime_s,
            "rtc": rtc["name"] if rtc else None,
            "on_ac": ac_online(),
            "allow_ac": bool(opts.allow_ac),
            "thresholds": dict(DEFAULT_THRESHOLDS),
            "energy_full_wh": None,
            "rtc_cleanup": {},
            "interrupted": False,
            "confirmation": "yes" if opts.yes else "typed",
        },
        "baseline": snapshot(),
        "cycles": [],
    }
    if capture["baseline"].get("energy_full_wh"):
        capture["meta"]["energy_full_wh"] = capture["baseline"]["energy_full_wh"]

    guards = resource_guards(opts) if not opts.dry_run else []
    if guards:
        print("refusing to suspend:")
        for problem in guards:
            print("  - %s" % problem)
        print("nothing was suspended and no alarm was armed")
        return 2

    started = time.monotonic()
    interrupted = False
    try:
        for index in range(1, opts.cycles + 1):
            if time.monotonic() - started > opts.max_runtime_s:
                capture["notes"].append(
                    "wall-clock budget exhausted before cycle %d; remaining cycles "
                    "skipped" % index
                )
                print("   wall-clock budget exhausted; skipping the remaining cycles")
                break
            print(
                "   cycle %d/%d: capturing pre-suspend state, then arming the RTC "
                "alarm and suspending for %d s"
                % (index, opts.cycles, opts.sleep_s)
            )
            before = snapshot()
            boot_before = _now_ns(time.CLOCK_BOOTTIME)
            mono_before = _now_ns(time.CLOCK_MONOTONIC)
            wall_before = time.time()
            direct = bool(getattr(opts, "direct", False))
            method = "rtcwake-direct" if direct else "logind"
            cmd = []
            if direct:
                cmd = ["rtcwake", "-m", "mem", "-s", str(opts.sleep_s), "-v"]
                if rtc:
                    cmd += ["-d", rtc["name"]]
            else:
                # Arm the wake alarm only, then suspend through logind, which runs
                # the sleep.target units (SD-reader park) and lets the EC/firmware
                # see a normal OS-initiated suspend instead of a raw power-state write.
                cmd = ["rtcwake", "-m", "no", "-s", str(opts.sleep_s), "-v"]
                if rtc:
                    cmd += ["-d", rtc["name"]]
            print("     %s" % " ".join(cmd))
            rc = None
            log = ""
            timed_out = False
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=opts.sleep_s + 120,
                    check=False,
                )
                rc = proc.returncode
                log = ((proc.stdout or "") + (proc.stderr or "")).strip()[-4000:]
                if not direct:
                    if rc != 0:
                        raise OSError("could not arm the RTC alarm (%s)" % rc)
                    suspend = subprocess.run(
                        ["systemctl", "suspend"],
                        capture_output=True,
                        text=True,
                        timeout=opts.sleep_s + 300,
                        check=False,
                    )
                    rc = suspend.returncode
                    log += ("\n[systemctl suspend] rc=%d\n%s"
                            % (rc, ((suspend.stdout or "") + (suspend.stderr or "")).strip()[-2000:]))
            except subprocess.TimeoutExpired as exc:
                log = "rtcwake timed out after %d s: %s" % (
                    opts.sleep_s + 300,
                    exc,
                )
                timed_out = True
                capture["notes"].append(
                    "cycle %d: rtcwake timed out; the machine may still be suspended "
                    "until the RTC alarm fires" % index
                )
            except OSError as exc:
                log = "rtcwake could not run: %s" % exc
            boot_after = _now_ns(time.CLOCK_BOOTTIME)
            mono_after = _now_ns(time.CLOCK_MONOTONIC)
            wall_after = time.time()
            print(
                "     resumed: boottime delta %.1f s, monotonic delta %.1f s"
                % (
                    (boot_after - boot_before) / 1e9,
                    (mono_after - mono_before) / 1e9,
                )
            )
            after = snapshot()
            journal = read_journal(wall_before, wall_after)
            armed = alarm_armed(rtc)
            cycle = {
                "index": index,
                "requested_s": opts.sleep_s,
                "before": before,
                "after": after,
                "boottime_ns": {"before": boot_before, "after": boot_after},
                "monotonic_ns": {"before": mono_before, "after": mono_after},
                "wall": {"before": wall_before, "after": wall_after},
                "rtcwake": {
                    "rc": rc,
                    "seconds": opts.sleep_s,
                    "log": log,
                    "timed_out": timed_out,
                },
                "journal": journal,
                "alarm_armed_after_resume": armed,
                "notes": [],
            }
            if armed:
                cleanup = disarm_rtc(rtc)
                cycle["notes"].append(
                    "rtcwake left a pending alarm after resume; cleanup: %s"
                    % cleanup.get("detail")
                )
                if not cleanup.get("ok"):
                    cycle["notes"].append("ALARM DISARM FAILED")
            if opts.settle_s:
                settle_start = time.monotonic()
                time.sleep(opts.settle_s)
                settle_end = snapshot()
                cycle["settle"] = {
                    "seconds": round(time.monotonic() - settle_start, 3),
                    "energy_wh": settle_end.get("energy_wh"),
                    "ts": settle_end.get("ts"),
                }
            capture["cycles"].append(cycle)
            if rc not in (0, None):
                break
    except KeyboardInterrupt:
        interrupted = True
        capture["meta"]["interrupted"] = True
        print("\n   interrupted: stopping after %d cycle(s)" % len(capture["cycles"]))
    finally:
        cleanup = disarm_rtc(rtc)
        capture["meta"]["rtc_cleanup"] = cleanup
        if cleanup.get("ok"):
            print("   rtc alarm: clear (%s)" % cleanup.get("detail"))
        else:
            print(
                "   rtc alarm: DISARM FAILED - %s" % cleanup.get("detail"),
                file=sys.stderr,
            )
            capture["notes"].append("RTC cleanup failed: %s" % cleanup.get("detail"))

    capture["meta"]["ended_at"] = _iso()
    capture["meta"]["elapsed_s"] = round(time.monotonic() - started, 1)
    capture["final"] = snapshot()
    if interrupted:
        capture["notes"].append("the run was interrupted; the report covers the cycles "
                                "that completed")
    report = analyse(capture)
    outdir = output_dir(opts)
    artifacts = write_artifacts(outdir, capture, report)
    print("")
    print(render_text(report))
    print("")
    print("== artifacts: %s" % artifacts["report_text"])
    print("   capture: %s" % artifacts["capture"])
    return report["verdict"]["code"]


# -------------------------------------------------------------- state/dry run
def _state_notes(baseline):
    notes = []
    if baseline.get("on_ac"):
        notes.append("powered from AC: a real drain run would refuse without --allow-ac")
    if "[s2idle]" not in ((baseline.get("mem_sleep") or "")):
        notes.append(
            "mem_sleep is %r, not [s2idle]: a real run would refuse"
            % baseline.get("mem_sleep")
        )
    if not baseline.get("rtc_alarm"):
        notes.append(
            "no RTC wakealarm interface: rtcwake cannot guarantee a wake; see the "
            "fallbacks in docs/SLEEP-TEST.md"
        )
    if baseline.get("energy_wh") is None:
        notes.append("no readable battery energy: a real run would refuse")
    return notes


def cmd_state(opts):
    baseline = snapshot()
    capture = {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "mode": "no-suspend",
        "notes": _state_notes(baseline),
        "meta": {
            "started_at": _iso(),
            "kernel": _read("/proc/sys/kernel/osrelease", "?"),
            "hostname": _read("/proc/sys/kernel/hostname", "?"),
            "root": os.geteuid() == 0,
            "cycles_requested": 0,
            "sleep_s": opts.sleep_s,
            "settle_s": opts.settle_s,
            "on_ac": baseline.get("on_ac"),
            "thresholds": dict(DEFAULT_THRESHOLDS),
        },
        "baseline": baseline,
        "cycles": [],
    }
    report = analyse(capture)
    report["verdict"] = {
        "label": "NO-MEASUREMENT",
        "code": 2,
        "reasons": ["no suspend cycle was performed; no drain figure exists"],
    }
    outdir = output_dir(opts)
    artifacts = write_artifacts(outdir, capture, report)
    print(render_state_text(report))
    print("")
    print("== artifacts: %s" % artifacts["report_text"])
    print("   (state-only run; use without --no-suspend for the real measurement)")
    return 2


def cmd_dry_run(opts):
    errors, warnings = _preflight(opts)
    print("== sleep-cycle-test %s  DRY RUN (nothing will be armed or suspended)"
          % _iso())
    lines, rtc_name = _plan_text(opts)
    for line in lines:
        print(line)
    print("   threshold: GOOD < %s %%/h and < %s C rise; FAILING > %s %%/h"
          % (
              DEFAULT_THRESHOLDS["good_pct_per_h"],
              DEFAULT_THRESHOLDS["good_temp_rise_c"],
              DEFAULT_THRESHOLDS["investigate_pct_per_h"],
          ))
    print("   live state:")
    baseline = snapshot()
    print(
        "     battery %s%% (%s of %s Wh, %s), AC=%s, mem_sleep=%s, rtc=%s, "
        "pkg=%s C, PL1=%s"
        % (
            baseline.get("pct"),
            baseline.get("energy_wh"),
            baseline.get("energy_full_wh"),
            baseline.get("status"),
            baseline.get("on_ac"),
            baseline.get("mem_sleep"),
            rtc_name,
            baseline.get("temp_pkg_c"),
            "%s W" % round(baseline["pl1_uw"] / 1e6, 1)
            if baseline.get("pl1_uw")
            else "?",
        )
    )
    print(
        "     ACPI wake armed: %s"
        % (" ".join(item["name"] for item in baseline.get("acpi_wakeup_enabled") or [])
           or "none")
    )
    print("   preconditions:")
    _print_preflight(errors, warnings, opts)
    print("   dry-run: nothing was armed, suspended or written.")
    return 0


# ------------------------------------------------------------------- analyse
def load_capture(path):
    path = pathlib.Path(path)
    if path.is_dir():
        path = path / "capture.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("tool") != PROG:
        raise ValueError("%s is not a sleep-cycle capture" % path)
    cycles = data.get("cycles")
    if not isinstance(cycles, list) or not all(
        isinstance(cycle, dict) and "before" in cycle and "after" in cycle
        for cycle in cycles
    ):
        raise ValueError(
            "%s is not a raw capture (its cycles have no before/after snapshots); "
            "pass capture.json, not report.json" % path
        )
    return data


def cmd_analyse(opts):
    try:
        capture = load_capture(opts.analyse)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _usage_error("cannot read capture: %s" % exc)
    report = analyse(capture)
    print(render_text(report))
    return report["verdict"]["code"]


# ------------------------------------------------------------------- selftest
def _selftest_capture(
    cycles=1,
    requested=60,
    slept=60.0,
    energy_deltas=None,
    temp_rises=None,
    wake_names=None,
    failed_resume=0,
    on_ac=False,
    rtcwake_rc=0,
    journal_entry=True,
    journal_exit=True,
):
    """A tiny synthetic capture, used only by --selftest."""
    energy_deltas = list(energy_deltas or [0.012] * cycles)
    temp_rises = list(temp_rises or [1.0] * cycles)
    wake_names = list(wake_names or ["rtc_cmos"] * cycles)
    base_wakeups = [
        {"id": "wakeup49", "name": "rtc_cmos", "event_count": 0,
         "device": "platform/rtc_cmos"},
        {"id": "wakeup39", "name": "PNP0C0C:00", "event_count": 0,
         "device": "platform/PNP0C0C:00"},
        {"id": "wakeup47", "name": "0000:00:14.0", "event_count": 0,
         "device": "pci0000:00/0000:00:14.0"},
    ]
    name_to_id = {
        "rtc_cmos": "wakeup49",
        "PNP0C0C:00": "wakeup39",
        "0000:00:14.0": "wakeup47",
    }
    rows = []
    for index in range(1, cycles + 1):
        energy = 70.253 - sum(energy_deltas[:index])
        before_wake = [dict(item) for item in base_wakeups]
        after_wake = [dict(item) for item in base_wakeups]
        for entry in after_wake:
            if entry["id"] == name_to_id.get(wake_names[(index - 1) % len(wake_names)]):
                entry["event_count"] = 1
        stats_before = {
            "success": index - 1,
            "fail": 0,
            "failed_suspend": 0,
            "failed_resume": 0,
            "total_hw_sleep": int(slept * 1e9) * (index - 1),
        }
        stats_after = {
            "success": index,
            "fail": 0,
            "failed_suspend": 0,
            "failed_resume": failed_resume if index == cycles else 0,
            "total_hw_sleep": int(slept * 1e9) * index,
        }
        journal = []
        if journal_entry:
            journal.append("PM: suspend entry (s2idle)")
        if journal_exit:
            journal.append("PM: suspend exit")
        rows.append(
            {
                "index": index,
                "requested_s": requested,
                "before": {
                    "energy_wh": energy,
                    "energy_full_wh": 87.665,
                    "temp_max_c": 54.0,
                    "suspend_stats": stats_before,
                    "wakeup_counts": before_wake,
                },
                "after": {
                    "energy_wh": energy - energy_deltas[index - 1],
                    "energy_full_wh": 87.665,
                    "temp_max_c": 54.0 + temp_rises[index - 1],
                    "suspend_stats": stats_after,
                    "wakeup_counts": after_wake,
                },
                "boottime_ns": {
                    "before": index * 10**12,
                    "after": index * 10**12 + int((slept + 10) * 1e9),
                },
                "monotonic_ns": {
                    "before": index * 10**12,
                    "after": index * 10**12 + int(10 * 1e9),
                },
                "rtcwake": {"rc": rtcwake_rc, "seconds": requested, "log": ""},
                "settle": {"seconds": 10.0, "energy_wh": energy
                           - energy_deltas[index - 1] - 0.005},
                "journal": journal,
            }
        )
    return {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "mode": "run",
        "meta": {
            "cycles_requested": cycles,
            "sleep_s": requested,
            "settle_s": 10,
            "on_ac": on_ac,
            "energy_full_wh": 87.665,
            "root": True,
            "kernel": "selftest",
            "rtc_cleanup": {"ok": True},
            "thresholds": dict(DEFAULT_THRESHOLDS),
        },
        "baseline": {"energy_wh": 70.253, "energy_full_wh": 87.665, "pct": 80,
                     "on_ac": on_ac, "mem_sleep": "[s2idle] deep",
                     "rtc_alarm": {"rtc0": ""}},
        "cycles": rows,
    }


def run_selftest():
    checks = []

    def check(name, condition):
        checks.append((name, bool(condition)))

    good = analyse(_selftest_capture(cycles=3))
    check("good: verdict is GOOD", good["verdict"]["label"] == "GOOD")
    check("good: three cycles", len(good["cycles"]) == 3)
    check(
        "good: rate is a number under 1 %/h",
        good["summary"]["drain_pct_per_h"] is not None
        and good["summary"]["drain_pct_per_h"] < 1.0,
    )
    check(
        "good: RTC is the expected wake source",
        good["wake"]["kind"] == "rtc" and good["wake"]["expected"] is True,
    )
    check(
        "good: text has the table and the verdict",
        "wake attribution" in render_text(good) and "== verdict: GOOD" in render_text(good),
    )

    power = analyse(_selftest_capture(wake_names=["PNP0C0C:00"]))
    check(
        "power button: named and capped at INVESTIGATE",
        power["wake"]["kind"] == "power-button"
        and power["verdict"]["label"] == "INVESTIGATE",
    )
    spurious = analyse(
        _selftest_capture(cycles=2, slept=30.0,
                          energy_deltas=[0.006, 0.006],
                          wake_names=["0000:00:14.0", "0000:00:14.0"])
    )
    check(
        "spurious: xHCI named as the wake source",
        spurious["wake"]["kind"] == "pci" and "0000:00:14.0" in spurious["wake"]["text"],
    )
    failed = analyse(_selftest_capture(failed_resume=1))
    check(
        "failed resume: detected and FAILING",
        failed["verdict"]["label"] == "FAILING"
        and any(
            issue["code"] == "failed-resume"
            for row in failed["cycles"]
            for issue in row["issues"]
        ),
    )
    increased = analyse(_selftest_capture(energy_deltas=[-0.10]))
    check("implausible battery curve: INVALID", increased["verdict"]["label"] == "INVALID")
    hot = analyse(_selftest_capture(temp_rises=[3.0]))
    check("hot resume: INVESTIGATE", hot["verdict"]["label"] == "INVESTIGATE")
    high = analyse(
        _selftest_capture(cycles=2, energy_deltas=[0.07, 0.07])
    )
    check("high drain: FAILING", high["verdict"]["label"] == "FAILING")
    on_ac = analyse(_selftest_capture(on_ac=True))
    check("on AC: INVALID", on_ac["verdict"]["label"] == "INVALID")

    # The maths itself, independent of the capture helper.
    check(
        "slept_seconds subtracts the monotonic delta",
        abs(
            slept_seconds(
                {
                    "boottime_ns": {"before": 0, "after": 70 * 10**9},
                    "monotonic_ns": {"before": 0, "after": 10 * 10**9},
                }
            )
            - 60.0
        )
        < 1e-9,
    )
    check(
        "drain_pct_per_h normalises to the full pack",
        abs(drain_pct_per_h(0.877, 87.7, 1.0) - 1.0) < 1e-6,
    )
    check(
        "journal hints find entry and exit",
        journal_hints(["PM: suspend entry (s2idle)", "PM: suspend exit"])
        == {
            "suspend_entry": True,
            "suspend_exit": True,
            "resumed": False,
            "aborted": False,
            "wake_lines": [],
        },
    )
    check(
        "a wake source log line is parsed",
        parse_wake_source_lines(
            ["oxp3-sleep: wake source rtc_cmos (+1): platform/rtc_cmos"]
        )
        == ["rtc_cmos"],
    )

    failed_checks = [name for name, ok in checks if not ok]
    if failed_checks:
        print("selftest: %d checks, %d FAILED" % (len(checks), len(failed_checks)))
        for name in failed_checks:
            print("  FAIL: %s" % name)
        return 1
    print(
        "selftest: %d checks passed (synthetic captures, no hardware access)"
        % len(checks)
    )
    return 0


# ----------------------------------------------------------------------- main
def build_parser():
    parser = _Parser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Drive N real s2idle cycles, guarantee a wake with an RTC alarm, "
            "attribute the wake source and report drain per hour with a verdict."
        ),
        epilog=(
            "Examples:\n"
            "  sudo tools/sleep-cycle-test.sh --cycles 3        real measurement\n"
            "  tools/sleep-cycle-test.sh --dry-run              plan + live preconditions\n"
            "  tools/sleep-cycle-test.sh --no-suspend           armed state only, no cycles\n"
            "  tools/sleep-cycle-test.sh --analyse DIR           re-analyse a saved capture\n"
            "  tools/sleep-cycle-test.sh --selftest             offline analyser fixtures\n"
            "\n"
            "Exit codes: 0 GOOD, 1 INVESTIGATE/FAILING, 2 INVALID/NO-MEASUREMENT, "
            "3 usage.\n"
            "Docs: docs/SLEEP-TEST.md"
        ),
    )
    parser.add_argument("--cycles", type=int, default=3,
                        help="complete suspend/resume cycles (1..%d, default 3)"
                        % MAX_CYCLES)
    parser.add_argument("--sleep", type=int, default=60, dest="sleep_s",
                        metavar="SECONDS",
                        help="seconds suspended per cycle (%d..%d, default 60)"
                        % (MIN_SLEEP_S, MAX_SLEEP_S))
    parser.add_argument("--settle", type=int, default=10, dest="settle_s",
                        metavar="SECONDS",
                        help="awake window after each resume to measure the awake cost "
                        "(0..%d, default 10)" % MAX_SETTLE_S)
    parser.add_argument("--max-runtime", type=int, default=DEFAULT_MAX_RUNTIME_S,
                        dest="max_runtime_s", metavar="SECONDS",
                        help="hard wall-clock budget for the whole run "
                        "(default %d)" % DEFAULT_MAX_RUNTIME_S)
    parser.add_argument("--rtc", metavar="rtcN",
                        help="RTC device to arm (default: the first with a wakealarm)")
    parser.add_argument("--output", metavar="DIR",
                        help="report directory (default docs/state/sleep-cycles-<utc>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the exact plan and commands; arm and write nothing")
    parser.add_argument("--no-suspend", action="store_true",
                        help="measure the armed/unarmed state only; no cycles, no "
                        "drain claim")
    parser.add_argument("--allow-ac", action="store_true",
                        help="run the cycles on AC; the drain verdict stays INVALID")
    parser.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation (scripted runs only)")
    parser.add_argument("--analyse", metavar="PATH",
                        help="re-analyse a capture.json or a report directory")
    parser.add_argument("--selftest", action="store_true",
                        help="offline analyser self-test")
    parser.add_argument("--version", action="version",
                        version="%s %s" % (PROG, VERSION))
    return parser


def main(argv=None):
    opts = build_parser().parse_args(argv)
    if opts.selftest:
        return run_selftest()
    if opts.analyse:
        return cmd_analyse(opts)
    problem = _check_ranges(opts, check_estimate=not opts.no_suspend)
    if problem:
        return _usage_error(problem)
    try:
        if opts.dry_run:
            return cmd_dry_run(opts)
        if opts.no_suspend:
            return cmd_state(opts)
        return cmd_run(opts)
    except KeyboardInterrupt:
        print(
            "%s: interrupted; stopping. Any pending RTC alarm was cleared by the "
            "run's cleanup path (run --no-suspend to inspect the current state)."
            % PROG,
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
