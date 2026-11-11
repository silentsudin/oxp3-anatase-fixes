#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate the synthetic sleep-cycle captures under this directory.

The captures mirror what ``tools/oxp3_sleep_cycles.py`` writes as
``capture.json``: a meta block, one baseline snapshot, and one entry per cycle
with before/after sysfs snapshots, CLOCK_BOOTTIME/CLOCK_MONOTONIC deltas, RTC
wake output, and the filtered journal excerpt.

They are synthetic on purpose: the analyser and its tests must run offline, with
no suspend, no root and no hardware.  Every number is plausible for the
OneXPlayer 3 (87.665 Wh pack, 0.01 Wh fuel-gauge step) but none was measured.
Run this script after editing a scenario; the JSON files are checked in.

    python3 tests/fixtures/sleep-cycles/build-fixtures.py
"""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
FULL_WH = 87.665
GAUGE_STEP_WH = 0.01
BASE_ENERGY_WH = 70.253

# /sys/class/wakeup sources: (id, name, device path).  The ids are stable on
# this host; the counters start at zero in the fixtures.
WAKE_SOURCES = [
    ("wakeup35", "PNP0C0D:00", "pci0000:00/0000:00:1f.0/PNP0C09:00/PNP0C0D:00"),
    ("wakeup39", "PNP0C0C:00", "platform/PNP0C0C:00"),
    ("wakeup47", "0000:00:14.0", "pci0000:00/0000:00:14.0"),
    ("wakeup49", "rtc_cmos", "platform/rtc_cmos"),
    ("wakeup50", "alarmtimer.0.auto", "platform/rtc_cmos/rtc/rtc0/alarmtimer.0.auto"),
    ("wakeup51", "3-4", "pci0000:00/0000:00:14.0/usb3/3-4"),
    ("wakeup23", "0000:00:1c.0", "pci0000:00/0000:00:1c.0"),
]

SUSPEND_STATS_ZERO = {
    "success": 0,
    "fail": 0,
    "failed_suspend": 0,
    "failed_resume": 0,
    "last_hw_sleep": 0,
    "max_hw_sleep": 0,
    "total_hw_sleep": 0,
    "last_failed_dev": "",
    "last_failed_step": "",
}


def wakeup_snapshot(counts):
    return [
        {
            "id": ident,
            "name": name,
            "event_count": counts.get(name, 0),
            "device": device,
        }
        for ident, name, device in WAKE_SOURCES
    ]


def snapshot(energy_wh, temp_max_c, stats, counts, s0ix_us):
    return {
        "ts": "2026-11-09T08:00:00Z",
        "epoch": 1762675200.0,
        "energy_wh": round(energy_wh, 6),
        "energy_full_wh": FULL_WH,
        "pct": 80,
        "status": "Discharging",
        "on_ac": False,
        "platform_profile": "balanced",
        "pl1_uw": 35000000,
        "temp_pkg_c": temp_max_c - 2.0,
        "temp_max_c": temp_max_c,
        "temps": {"x86_pkg_temp": temp_max_c - 2.0, "TCPU": temp_max_c},
        "suspend_stats": dict(stats),
        "wakeup_counts": wakeup_snapshot(counts),
        "acpi_wakeup_enabled": [
            {"name": "LID0", "state": "S3", "enabled": True,
             "sysfs": "platform:PNP0C0D:00"},
            {"name": "PWRB", "state": "S3", "enabled": True,
             "sysfs": "platform:PNP0C0C:00"},
            {"name": "SLPB", "state": "S3", "enabled": True,
             "sysfs": "platform:PNP0C0E:00"},
            {"name": "AWAC", "state": "S4", "enabled": True,
             "sysfs": "platform:ACPI000E:00"},
        ],
        "device_wakeups": [
            {"bus": "pci", "id": "0000:00:14.0"},
            {"bus": "pci", "id": "0000:00:0d.0"},
            {"bus": "usb", "id": "3-4"},
            {"bus": "usb", "id": "3-5"},
        ],
        "rtc_alarm": {"rtc0": "", "rtc1": ""},
        "mem_sleep": "[s2idle] deep",
        "s0ix_us": s0ix_us,
    }


def journal_lines(slept_s, entry=True, exit_=True, extra=None, start_minute=0):
    """A filtered journalctl -k excerpt, as the driver would store it."""
    lines = []
    hour = 8 + start_minute // 60
    minute = start_minute % 60
    stamp = "2026-11-09T%02d:%02d:00+0000" % (hour, minute)
    if entry:
        lines.append("%s anatase kernel: PM: suspend entry (s2idle)" % stamp)
    if exit_:
        lines.append(
            "%s anatase kernel: PM: suspend exit (%d.%d s later)"
            % (stamp, int(slept_s), int(round((slept_s % 1) * 10)))
        )
    if extra:
        lines.extend(extra)
    return lines


def make_capture(
    *,
    cycles,
    name,
    journal_extra=None,
    wake_from_journal=False,
):
    """Build one capture from a list of per-cycle scenario dicts."""
    meta = {
        "started_at": "2026-11-09T08:00:00Z",
        "ended_at": "2026-11-09T08:10:00Z",
        "kernel": "7.2.7-an28.fc45.x86_64",
        "hostname": "anatase-oxp3",
        "root": True,
        "cycles_requested": len(cycles),
        "cycles_completed": len(cycles),
        "sleep_s": cycles[0]["requested"],
        "settle_s": 10,
        "max_runtime_s": 1800,
        "rtc": "rtc0",
        "on_ac": False,
        "allow_ac": False,
        "thresholds": {
            "good_pct_per_h": 1.0,
            "investigate_pct_per_h": 3.0,
            "good_temp_rise_c": 2.0,
            "implausible_pct_per_h": 25.0,
            "energy_increase_tolerance_wh": 0.05,
            "gauge_step_wh": GAUGE_STEP_WH,
        },
        "energy_full_wh": FULL_WH,
        "rtc_cleanup": {"ok": True, "attempts": [], "detail": "no alarm was armed"},
        "interrupted": False,
        "confirmation": "typed",
    }
    counts = {}
    stats = dict(SUSPEND_STATS_ZERO)
    energy = BASE_ENERGY_WH
    s0ix_us = 0
    rows = []
    for index, scenario in enumerate(cycles, start=1):
        sleeps = scenario["slept"]
        awake = scenario.get("awake", 10.0)
        temp_before = scenario.get("temp_before", 54.0)
        temp_after = temp_before + scenario.get("temp_rise", 1.0)
        before = snapshot(energy, temp_before, stats, counts, s0ix_us)
        energy = round(energy - scenario["energy_delta_wh"], 6)
        stats = dict(stats)
        stats["success"] = stats.get("success", 0) + (0 if scenario.get("no_success") else 1)
        stats["total_hw_sleep"] = int(sleeps * 1e9) + stats.get("total_hw_sleep", 0)
        stats["last_hw_sleep"] = int(sleeps * 1e9)
        if scenario.get("failed_resume"):
            stats["failed_resume"] = stats.get("failed_resume", 0) + 1
        if not wake_from_journal:
            for source in scenario.get("wake", ["rtc_cmos"]):
                counts[source] = counts.get(source, 0) + 1
        s0ix_us += int(sleeps * 1e6)
        after = snapshot(energy, temp_after, stats, counts, s0ix_us)
        journal = journal_lines(
            sleeps,
            entry=scenario.get("journal_entry", True),
            exit_=scenario.get("journal_exit", True),
            extra=scenario.get("journal", journal_extra),
            start_minute=index * 3,
        )
        rows.append(
            {
                "index": index,
                "requested_s": scenario["requested"],
                "before": before,
                "after": after,
                "boottime_ns": {
                    "before": index * 10**12,
                    "after": index * 10**12 + int((sleeps + awake) * 1e9),
                },
                "monotonic_ns": {
                    "before": index * 10**12,
                    "after": index * 10**12 + int(awake * 1e9),
                },
                "wall": {
                    "before": 1762675200.0 + index * 60,
                    "after": 1762675200.0 + index * 60 + sleeps + awake,
                },
                "rtcwake": {
                    "rc": scenario.get("rtcwake_rc", 0),
                    "seconds": scenario["requested"],
                    "log": scenario.get("rtcwake_log", ""),
                },
                "journal": journal,
                "alarm_armed_after_resume": False,
                "notes": [],
                "settle": {
                    "seconds": 10.0,
                    "energy_wh": round(energy - scenario.get("awake_wh", 0.019), 6),
                    "ts": "2026-11-09T08:00:10Z",
                },
            }
        )
    baseline = snapshot(
        BASE_ENERGY_WH,
        54.0,
        dict(SUSPEND_STATS_ZERO),
        {},
        0,
    )
    return {
        "schema": 1,
        "tool": "sleep-cycle-test",
        "version": "1.0.0",
        "mode": "run",
        "notes": [],
        "meta": meta,
        "baseline": baseline,
        "cycles": rows,
        "name": name,
    }


SCENARIOS = {}


def scenario(slug, **kwargs):
    SCENARIOS[slug] = make_capture(name=slug, **kwargs)


scenario(
    "three-good-rtc",
    cycles=[
        {"requested": 60, "slept": 60.1, "awake": 10.4, "energy_delta_wh": 0.012,
         "temp_rise": 1.0, "wake": ["rtc_cmos", "alarmtimer.0.auto"]},
        {"requested": 60, "slept": 60.2, "awake": 10.2, "energy_delta_wh": 0.013,
         "temp_rise": 0.8, "wake": ["rtc_cmos", "alarmtimer.0.auto"]},
        {"requested": 60, "slept": 60.0, "awake": 10.6, "energy_delta_wh": 0.011,
         "temp_rise": 1.2, "wake": ["rtc_cmos", "alarmtimer.0.auto"]},
    ],
)

scenario(
    "power-button-wake",
    cycles=[
        {"requested": 60, "slept": 47.8, "awake": 11.0, "energy_delta_wh": 0.010,
         "temp_rise": 0.8, "wake": ["PNP0C0C:00"],
         "journal": ["2026-11-09T08:03:47+0000 anatase kernel: "
                     "ACPI: Waking up from system sleep state"]},
    ],
)

scenario(
    "spurious-xhci",
    cycles=[
        {"requested": 60, "slept": 30.2, "awake": 10.0, "energy_delta_wh": 0.006,
         "temp_rise": 0.6, "wake": ["0000:00:14.0"]},
        {"requested": 60, "slept": 30.4, "awake": 10.1, "energy_delta_wh": 0.006,
         "temp_rise": 0.5, "wake": ["0000:00:14.0"]},
    ],
)

scenario(
    "failed-resume",
    cycles=[
        {"requested": 60, "slept": 60.0, "awake": 14.0, "energy_delta_wh": 0.012,
         "temp_rise": 1.4, "wake": ["rtc_cmos"], "failed_resume": True,
         "journal_exit": False,
         "journal": ["2026-11-09T08:03:00+0000 anatase kernel: "
                     "PM: resume of devices complete after 1900.1 msecs"]},
    ],
)

scenario(
    "battery-increase",
    cycles=[
        # The gauge reports more energy after the suspend: a reset or charging,
        # never a drain.  0.10 Wh is well outside the 0.05 Wh noise tolerance.
        {"requested": 60, "slept": 60.0, "awake": 10.0, "energy_delta_wh": -0.10,
         "temp_rise": 0.4, "wake": ["rtc_cmos"]},
    ],
)

scenario(
    "high-drain",
    cycles=[
        {"requested": 60, "slept": 60.0, "awake": 10.0, "energy_delta_wh": 0.070,
         "temp_rise": 3.2, "wake": ["rtc_cmos", "alarmtimer.0.auto"]},
        {"requested": 60, "slept": 60.0, "awake": 10.2, "energy_delta_wh": 0.070,
         "temp_rise": 3.0, "wake": ["rtc_cmos", "alarmtimer.0.auto"]},
    ],
)

scenario(
    "journal-fallback",
    wake_from_journal=True,
    cycles=[
        # The wakeup event counters did not move (a firmware/ACPI quirk), but our
        # oxp3-sleep log line names the source.  The analyser must use the journal.
        {"requested": 60, "slept": 60.0, "awake": 10.0, "energy_delta_wh": 0.011,
         "temp_rise": 0.7, "wake": [],
         "journal": ["2026-11-09T08:03:00+0000 anatase oxp3-sleep: resume "
                     "(post): success +1 fail +0 hw_sleep +60.0s",
                     "2026-11-09T08:03:00+0000 anatase oxp3-sleep: wake source "
                     "rtc_cmos (+1): platform/rtc_cmos"]},
    ],
)


def main():
    for slug, capture in sorted(SCENARIOS.items()):
        path = HERE / (slug + ".json")
        path.write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")
        print("wrote %s" % path.relative_to(HERE.parent.parent.parent))


if __name__ == "__main__":
    main()
