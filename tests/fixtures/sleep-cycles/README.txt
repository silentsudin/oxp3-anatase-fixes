Synthetic sleep-cycle captures (offline analyser fixtures)
==========================================================

These are *not* measurements.  Each <scenario>.json mirrors the capture.json
that `tools/oxp3_sleep_cycles.py` writes during a real run: a meta block, one
baseline sysfs snapshot, and one entry per cycle with before/after snapshots,
CLOCK_BOOTTIME/CLOCK_MONOTONIC deltas, suspend_stats, RTC wake output and the
filtered journal excerpt.

They exist so the analyser can be tested without root, without suspending the
machine and without a battery.  `tests/unit/test_sleep_cycles.py` reads them
directly:

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/unit \
        -p 'test_sleep_cycles.py' -q

`tools/sleep-cycle-test.sh --selftest` is the tool's own, self-contained check
(minimal in-memory captures); it does not need these files.

Regenerate after editing build-fixtures.py:

    python3 tests/fixtures/sleep-cycles/build-fixtures.py

| fixture | encodes | expected verdict |
|---|---|---|
| three-good-rtc.json | 3 x 60 s, RTC alarm only, ~0.82 %/h, +1.2 C max | GOOD |
| power-button-wake.json | woke at 47.8 s on PNP0C0C:00 (the power button) | INVESTIGATE (unexpected wake) |
| spurious-xhci.json | two 30 s sleeps ended by 0000:00:14.0 (USB xHCI) | INVESTIGATE (unexpected wake) |
| failed-resume.json | suspend_stats failed_resume +1, no "PM: suspend exit" | FAILING |
| battery-increase.json | energy rose 0.10 Wh across the suspend (gauge reset/charging) | INVALID (no drain figure) |
| high-drain.json | 2 x 60 s at 4.79 %/h and a +3.2 C rise | FAILING |
| journal-fallback.json | wakeup counters did not move; the oxp3-sleep line names rtc_cmos | GOOD (attributed from the journal) |

The thresholds behind the verdicts are fixed in `tools/oxp3_sleep_cycles.py`
(`DEFAULT_THRESHOLDS`) and explained in `docs/SLEEP-TEST.md`.

The values are plausible for the OneXPlayer 3 (87.665 Wh pack, fuel gauge
resolving 0.01 Wh) but no number here was measured on hardware, and a real
capture has far more wakeup entries and journal lines.
