# Sleep-cycle measurement and wake attribution

Date: **2026-11-09**. Scope: `tools/sleep-cycle-test.sh`,
`tools/oxp3_sleep_cycles.py`, `tests/unit/test_sleep_cycles.py`,
`tests/fixtures/sleep-cycles/`, the `sleep-cycles` suite in
`tools/repo-test.sh`, and this page.

The owner's report is "the device stays warm and drains" — an impression, not a
number. This harness turns it into a repeatable figure and a named cause: it
drives N real s2idle cycles, guarantees that each one returns, measures the
suspended time from the kernel's own clocks, attributes the wake source, and
prints drain per hour with a verdict.

Related: [hw/sleep-power.md](hw/sleep-power.md) (the platform findings and the
wake-source policy), [VALIDATION.md](VALIDATION.md) (`sleep.*` checks),
[tools/sleep-power-measure.sh](../tools/sleep-power-measure.sh) (the
single-window measurement this replaces for the "does it come back every time"
question).

## TL;DR for the owner

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
# 1. unplug the charger, leave the desktop alone, screen off, nothing heavy running
sudo tools/sleep-cycle-test.sh --cycles 3
#    it prints the plan and asks for the word SUSPEND; ~4.5 minutes total
# 2. hand back the directory it names, for example:
docs/state/sleep-cycles-<utc>/report.txt   # plus report.json and capture.json
```

A **GOOD** run means: three real s2idle cycles completed, the machine woke only
on our RTC alarm, the battery drained **under 1 %/h while suspended**, and the
package temperature rose **under 2 °C** across each sleep. Anything else is
printed with its cause; the report never invents a number.

## 1. What it measures, and why each number is trustworthy

Per cycle, in order:

| measurement | source | why it is the right one |
|---|---|---|
| **suspended time** | `CLOCK_BOOTTIME` delta **minus** `CLOCK_MONOTONIC` delta | `BOOTTIME` keeps counting through suspend, `MONOTONIC` stops; the difference is real s2idle time, not wall-clock time that includes the resume |
| **cross-check** | `/sys/power/suspend_stats` `success` / `failed_suspend` / `failed_resume`, `last_hw_sleep`, `total_hw_sleep` | if the kernel did not count a successful suspend, the energy delta is not a sleep number |
| **wake source** | `/sys/class/wakeup/*/event_count` deltas (name + device path), plus the first `PM: suspend`/`Wakeup`/`wake source` lines from this boot's journal | the counter moves for the source that actually fired; the journal is the fallback and the human-readable evidence |
| **drain** | `energy_now` before/after, normalised to `energy_full` and to suspended hours | `%/h` is comparable across battery levels and run lengths; the watt figure is the same number at this pack size |
| **temperature** | `x86_pkg_temp` and the maximum of every thermal zone, before/after | a warm chassis is a package that kept executing; the delta says whether suspend was real |
| **awake cost** | energy delta over a settle window right after resume | separates "drain while asleep" from "drain because it sat awake for 10 s at each resume" |
| **S0ix residency** | `intel_pmc_core/slp_s0_residency_usec` delta (root only) | informational: did the package reach the low-power state, not just "suspend entry" |
| **baseline state** | battery %, status, AC, `platform_profile`, PL1, temperatures, armed ACPI wake sources and device wake bits | a drain figure without the machine's state is not reproducible |

Nothing is inferred from a wall clock: the harness reads the two clocks around
the `rtcwake` call and records both raw nanosecond values in `capture.json`, so
anyone can recompute the numbers.

## 2. How to run it

### 2.1 The measured run

```sh
sudo tools/sleep-cycle-test.sh --cycles 3
```

* **Real cycles need root** (`rtcwake` and suspend do). `--dry-run`,
  `--no-suspend`, `--analyse` and `--selftest` do not.
* **Unplug the charger first.** On AC the battery is not discharging, so the
  drain figure is meaningless; as root the tool refuses with exit 2 and says
  so (without root it stops earlier, at the root check).
  `--allow-ac` exists only to exercise the cycle machinery: it runs, but the
  verdict is INVALID for drain.
* **Leave the machine alone** for the run: no game, no compile, no updater, no
  other agent workload, screen off. An awake process during the settle windows
  raises the measured awake cost, not the suspended rate — but it can also hold
  a wake source busy and turn a clean run into a noisy one.
* Type **`SUSPEND`** at the prompt. The prompt names the cycle count and sleep
  length; every cycle then prints the `rtcwake` command it is about to run.
  `--yes` skips the prompt for a deliberately scripted run and says so in the
  report.
* Default: 3 cycles × 60 s (estimated 265 s including settle and snapshots).
  Use `--sleep 300` for a better drain figure (see §3.2), or
  `--cycles 2 --sleep 1800` for a one-hour-class measurement.

### 2.2 The read-only modes

```sh
tools/sleep-cycle-test.sh --dry-run        # the exact plan, commands and preconditions
tools/sleep-cycle-test.sh --no-suspend     # armed state only; writes a state report
tools/sleep-cycle-test.sh --analyse DIR    # re-analyse a saved capture.json/report dir
tools/sleep-cycle-test.sh --selftest       # offline analyser fixtures
```

`--dry-run` prints the exact `rtcwake`, `journalctl` and disarm commands, the
live battery/AC/mem_sleep/RTC state, and every precondition it would enforce.
It arms nothing, suspends nothing and writes nothing.

`--no-suspend` takes one snapshot and writes a report with
`mode: no-suspend` and verdict `NO-MEASUREMENT` (exit 2): the armed wake
sources, the RTC alarm state, `suspend_stats`, battery, profile, PL1 and
temperatures. It is the offline way to prove the tool reads this host
correctly and to show what the next real run would start from.

### 2.3 Exit codes

| exit | meaning |
|---|---|
| 0 | **GOOD**: under 1 %/h and under 2 °C rise, all cycles completed, RTC alarm only |
| 1 | **INVESTIGATE** or **FAILING**: a threshold crossed, an unexpected wake source, a failed or invalid cycle, or only part of the requested cycles |
| 2 | **INVALID / NO-MEASUREMENT**: on AC, an implausible energy curve that leaves no usable cycle, RTC cleanup failed, or no cycle performed |
| 3 | usage or setup error (no root, no `rtcwake`, no readable battery, no RTC interface, not s2idle, limits exceeded, confirmation declined) |

The same convention as `oxp3-verify.sh` and `sleep-power-measure.sh`; `1` is a
result, not a crash.

## 3. Thresholds and why they are these numbers

### 3.1 The verdict

| label | condition |
|---|---|
| **GOOD** | rate **< 1.0 %/h** and max rise **< 2.0 °C**, every requested cycle completed and usable, and the wake source was the RTC alarm |
| **INVESTIGATE** | rate **1.0–3.0 %/h**, or rise **≥ 2.0 °C**, or an unexpected wake source, a partial run, or an invalid cycle among usable ones |
| **FAILING** | rate **> 3.0 %/h**, or any cycle failed (refused/aborted suspend, failed resume, `rtcwake` timeout, no suspend at all) |
| **INVALID** | on AC, a cycle whose battery capacity is unreadable (so %/h cannot be computed), an energy curve that rose instead of falling in every cycle, RTC cleanup failure, or no cycle at all |

The binary idea is simple: **under 1 %/h and under 2 °C is asleep; over 3 %/h
is not**. The band between is where the machine still wakes for something but
the drain has not yet become a bag problem.

### 3.2 Why 1 %/h and 3 %/h

On the 87.665 Wh pack:

* 1 %/h = **0.877 W** — the healthy s2idle target.
* 3 %/h = **2.63 W** — the failing limit: a handheld drawing this while
  "asleep" will flatten in a bag overnight.
* The recorded awake-idle package power on this host is **7.8 W**
  ([hw/sleep-power.md §1](hw/sleep-power.md)). 3 %/h is therefore already a
  third of an awake desktop; above it the machine is not really sleeping, it is
  idling.

The earlier single-window tool used 1.0/2.0 %/h. This harness keeps 1 %/h as
the good line and uses 3 %/h as the failing line (the thresholds requested for
this round). The 1–3 %/h band is the **investigate** zone: in practice the wake
attribution often explains the number there — a source that fired mid-sleep is
a better lead than 0.3 %/h of gauge noise (§3.4).

### 3.3 Why 2 °C

`x86_pkg_temp` on this host idles in the 50–60 °C range and the chassis is
warm only because the package is executing. A real s2idle window lets the
package fall or stay flat; a **2 °C rise** across one sleep means work
happened, which is exactly the "warm in the bag" symptom. The test uses the
maximum over every thermal zone, not just the package, so a hot Wi-Fi or NVMe
zone is visible too.

### 3.4 Fuel-gauge resolution (why the window length matters)

`energy_now` resolves **0.01 Wh** on this pack. One gauge step over the whole
suspended window is the floor of the measurement:

| window | one-step uncertainty |
|---|---|
| 3 × 60 s (3 min) | ± 0.23 %/h |
| 3 × 300 s (15 min) | ± 0.046 %/h |
| 1 h | ± 0.011 %/h |

The report prints `rate +/- uncertainty`. The default 3 × 60 s run is a
**screening test**: it can prove "≈ 0.8 %/h" or "> 3 %/h", it cannot prove
0.90 vs 0.95. For a decision-grade number use `--sleep 300` or longer; the
verdict logic is the same, and the report says so when the window is short.

### 3.5 Why an unexpected wake caps the verdict

A cycle that was ended by the power button, the lid, a USB device or the
charger did not complete the sleep we asked for. The drain rate for that short
window may still look low, but it is not a measurement of a full sleep. Such a
run is reported as **INVESTIGATE** at best with the source named — never
GOOD — even if the number happens to be under 1 %/h. That is deliberate: the
verdict is about sleep behaviour, not just about one ratio.

## 4. Interpreting the wake attribution

The primary evidence is the per-source event-count delta from
`/sys/class/wakeup`; the journal is the fallback and the explanation. The
harness names the dominant source and lists the others.

| counter name / device | harness label | meaning here |
|---|---|---|
| `rtc_cmos`, `alarmtimer.0.auto`, `ACPI000E:00` (AWAC) | RTC / alarm timer / wake alarm | **expected**: this is the alarm `rtcwake` armed. Several of these may increment for the same wake; all-RTC is treated as one expected RTC wake |
| `PNP0C0C:00`, `LNXPWRBN:00` | power button | someone (or something pressing the button in a bag) woke it |
| `PNP0C0D:00` | lid switch | lid opened, or a lid sensor event |
| `PNP0C0E:00` | sleep button | EC sleep button |
| `0000:00:14.0` | USB xHCI controller | USB-A, both internal HID endpoints and the gamepad hang off it; a nudge is enough |
| `0000:00:0d.0`, `0000:00:0d.2`, `0000:00:07.0/1` | USB4/Type-C | cable, dock or a Type-C device |
| `3-4`, `3-5` | internal keyboard HID / EC bridge | internal, fixed-mount endpoints; wake means the controller reported activity |
| `ADP1`, `ACPI0003:00` | AC adapter | charger plugged in |
| `0000:00:1c.0`, `0000:00:06.0` | card-reader / NVMe root port | storage link woke the machine (its PME can fire while the link retrains) |

Rules of interpretation:

* **RTC only** = clean measurement: the wake was ours.
* **RTC + something else** = the machine slept, then another source fired
  before or at the alarm; the report says "unexpected extra wake" and the
  verdict is at best INVESTIGATE. Find that source; the shipped policy
  ([R2](hw/sleep-power.md#r2---disarm-the-wake-sources-that-should-not-wake-it-shipped-here))
  is how it gets disarmed.
* **No RTC at all** = the alarm did not fire or cannot be attributed. Check
  that `AWAC` is enabled in `/proc/acpi/wakeup` and that `rtcwake` exited 0.
* **No counter moved and no journal line** = *unknown*, never "nothing woke
  it". The journal excerpt in `capture.json` is the next thing to read.
* The tool reports what the kernel counted. A wake caused inside the EC or
  firmware that never raises a kernel-visible wakeup event cannot be attributed
  here; it shows up as a short `slept` time or as `unknown`.

## 5. Reading the report

`report.txt` starts with the baseline line and the table:

```
    #  slept      awake     energy       drain        power    pkg rise   wake
    1    60.1 s   10.4 s    0.012 Wh    0.82 %/h     0.72 W   +1.0 C     RTC alarm timer (rtcwake)
   ...
   totals: 180.3 usable s suspended (180.3 s completed), 31.2 s awake, 0.036 Wh => 0.82 %/h (0.72 W)
   gauge: +/- 0.23 %/h from one 0.01 Wh fuel-gauge step over this window
   residual awake cost: 31.2 s at 6.84 W = 0.059 Wh (not part of %/h)
   S0ix residency: 180.3 s of 180.3 s slept (100%)
   suspend_stats: success +3, failed_suspend +0, failed_resume +0
   wake attribution: ... - the scheduled RTC alarm; expected
== verdict: GOOD - thresholds: GOOD < 1.0 %/h and < 2.0 C rise; FAILING > 3.0 %/h
```

* **slept** is the `BOOTTIME − MONOTONIC` figure; **awake** is the monotonic
  (really awake) part of the same interval. `slept + awake` equals the elapsed
  time.
* **energy** is the *suspend* delta; the **residual awake cost** is reported
  separately and is **not** part of %/h. A machine that wakes and sits awake
  for minutes pays that cost too, and the report makes it visible.
* **usable s suspended** excludes cycles whose energy curve or sleep time was
  unusable; the completed total is shown next to it so the loss is visible.
* **S0ix residency** is informational (root only): a low ratio while the drain
  looks good means the platform is spending time outside S0ix, which is the
  next thing to investigate with `pmc_core`.

## 6. What a passing run proves — and what it deliberately does not

A **GOOD** run proves, for the measured windows:

1. the machine entered and left real s2idle **N times** and the kernel counted
   each suspend (`suspend_stats success` grew);
2. it woke **only** on the RTC alarm the harness armed (no PWRB/LID/USB/PCI
   source moved);
3. while suspended it drew **under 1 %/h** of the pack, with a stated
   measurement uncertainty;
4. the package did not heat up by **2 °C or more** across a sleep;
5. the awake time around the cycles cost the reported small number of Wh,
   measured rather than assumed.

It does **not** claim:

* **a 12-hour bag result.** Three 60 s windows are a screen, not a soak test;
  for a decision use `--sleep 300`+ or repeat, and note that behaviour can
  depend on what is connected.
* **an exact %/h below the gauge resolution.** The uncertainty line is part of
  the result; 0.8 ± 0.23 is "about 1 %/h", not 0.80.
* **that AC numbers mean anything.** It refuses them by default and marks them
  invalid with `--allow-ac`.
* **anything about hibernate or S3** (`deep`). `/sys/power/disk` is
  `[disabled]` under the integrity lockdown, and S3 is not a usable PTL state;
  this tool measures s2idle only and refuses if `mem_sleep` is not `[s2idle]`.
* **which device inside a composite source woke it.** The counters attribute
  to the kernel's wakeup source (for example the xHCI controller), not to the
  individual USB port or HID report.
* **firmware/EC-internal power states.** It reads what Linux exposes
  (S0ix residency where available, wake counters, battery, thermal zones); it
  cannot see an EC sleep mode that no kernel interface reports.
* **a per-device power breakdown.** Agreement with `rapl` or `turbostat` is not
  attempted; the battery is the meter.
* **causality between a setting and the number.** Change the wake policy, then
  run again: the harness reports, it does not A/B test for you.
* **that the machine was never left suspended.** Every normal exit path
  disarms the alarm and verifies it; a `SIGKILL` (or a crash) cannot be caught,
  in which case the armed alarm is the safety net — the machine wakes at the
  alarm at the latest.

## 7. Safety model

* **Root is required for real cycles** and checked before anything is armed.
* **One explicit confirmation** names the cycle count and sleep length and
  requires the word `SUSPEND`; `--yes` is the documented scripted override and
  is recorded in the report.
* **Hard limits**: 1–10 cycles, 10–3600 s per sleep, settle 0–300 s, and the
  estimated runtime must fit `--max-runtime` (default 1800 s). The wall clock
  is checked again before every cycle; the run stops and reports a partial
  result if it is exhausted.
* **The trap** (`finally`, plus SIGINT/SIGTERM/SIGHUP handlers) disarms a
  pending RTC alarm with `rtcwake -m disable -d rtcN`, falls back to writing
  `0` to `/sys/class/rtc/rtcN/wakealarm`, verifies by reading the attribute
  back, and prints/records whether the disarm was **verified**. If it cannot
  verify, the report is INVALID and names the manual command.
* **No mutation is made by `--dry-run` or `--no-suspend`**; `--dry-run` writes
  nothing at all, and the repo suite asserts that.
* **On AC the drain number is refused** (exit 2) and the reason is printed;
  `--allow-ac` runs cycles but cannot produce a valid drain verdict.
* The tool **never reboots, never hibernates, never changes `mem_sleep`** and
  never disarms a wake source — attribution only.

### Fallbacks if the RTC interface is missing

The timed wake is a hard precondition. If `/sys/class/rtc/rtcN/wakealarm`
does not exist, the tool refuses with exit 3 (it will not suspend without a
guaranteed wake). In order:

1. **Another RTC**: `ls /sys/class/rtc/*/wakealarm` and pass its name with
   `--rtc rtcN` (rtcwake accepts `rtc0`, `rtc1`, ...). `rtc_cmos` must be
   loaded; check `dmesg | grep rtc`.
2. **Device node missing but sysfs present**: verify `/dev/rtcN` exists or is
   created by udev, then retry with `--rtc`.
3. **No RTC at all**: there is no guaranteed timed wake on this machine. Do not
   run this harness; use `sudo tools/sleep-power-measure.sh suspend -w N` with
   a person present to wake it, or `systemctl suspend` and wake it by hand.
   `deep`/S3 is not a fallback (see the note above), and `rtcwake -m no` is
   only a way to arm/disarm an alarm, not to sleep safely.
4. **Cleanup only**: `sudo rtcwake -m disable -d rtcN` (or
   `echo 0 > /sys/class/rtc/rtcN/wakealarm`) clears a pending alarm.

## 8. What to hand back

The whole report directory is the evidence. It is deliberately self-contained
and machine-local (`docs/state/sleep-cycles-*/` is gitignored):

| file | what it is |
|---|---|
| `report.txt` | the human table, wake attribution and verdict |
| `report.json` | the same as schema 1 (cycles, summary, wake, verdict, notes) |
| `capture.json` | the raw observations: every snapshot, both clocks in ns, `suspend_stats`, every wake counter, the journal excerpt, the `rtcwake` output |

Also useful, when present:

* `journalctl -t oxp3-sleep -b` — the installed wake logger's own per-resume
  lines (independent confirmation of the same counters);
* what was connected (charger, USB, monitor) and whether the R2 wake policy
  was installed (`systemctl status oxp3-sleep-wakeup.service`);
* the `--dry-run`/`--no-suspend` output if the run refused for a setup reason.

Review `capture.json` before sharing it: it contains the battery state and the
wake-source inventory of the machine (not secrets, but owner-specific).

## 9. Offline validation performed (no root, no suspend)

Recorded on 2026-11-09 on this host, kernel `7.2.7-an28.fc45.x86_64`, as an
unprivileged user, with the machine on AC. The machine was **not** suspended;
none of the commands below arm an alarm.

| check | result |
|---|---|
| `tests/unit/test_sleep_cycles.py` | **41 tests, OK** (fixtures, maths, thresholds, attribution, journal, render, sysfs snapshot, safety) |
| `python3 -m unittest discover -s tests/unit -q` | **723 tests, OK** (skipped=1) — the new file included |
| `tools/sleep-cycle-test.sh --selftest` | **16/16 checks passed** |
| `--dry-run` | exit 0, plan + exact commands printed, nothing written |
| `--no-suspend --output …` | exit 2 (`NO-MEASUREMENT`), `report.txt`/`report.json`/`capture.json` valid JSON |
| `--cycles 1 --yes` as the unprivileged user | exit 3, the root REFUSE line, **no output directory created** (the root gate runs before anything is armed) |
| `--analyse` on a saved capture directory | reads `capture.json`, prints the state report, exit 2 (`NO-MEASUREMENT`) |
| `--analyse` on the 7 fixtures | the verdict and exit code in the table below |
| `tools/repo-test.sh --only sleep-cycles --only unit` | `unit` PASS, `sleep-cycles` SKIP (no root) with the offline half passing; exit 0 |
| `sh -n` / `bash -n` on the wrapper, `bash -n` on `repo-test.sh`, `py_compile` on the Python files | clean |
| `tools/gen-docs.py --check` | after regenerating `docs/HANDOVER.md`: all 4 blocks ok |

| fixture | verdict | exit |
|---|---|---|
| `three-good-rtc` | GOOD | 0 |
| `journal-fallback` | GOOD | 0 |
| `power-button-wake` | INVESTIGATE | 1 |
| `spurious-xhci` | INVESTIGATE | 1 |
| `failed-resume` | FAILING | 1 |
| `high-drain` | FAILING | 1 |
| `battery-increase` | INVALID | 2 |

The raw outputs are kept on this machine under
`docs/state/sleep-cycles-20261109T083601Z/` (`no-suspend.txt`, `dry-run.txt`,
`selftest.txt`, `fixture-verdicts.txt`, `unit-tests.txt`, `repo-test.txt`, and
the no-suspend `report.*`/`capture.json`). That directory is gitignored by
design (machine-local reports); the key lines are reproduced here so this page
does not depend on it:

```
$ tools/sleep-cycle-test.sh --selftest
selftest: 16 checks passed (synthetic captures, no hardware access)

$ tools/sleep-cycle-test.sh --no-suspend --output docs/state/sleep-cycles-…
== sleep-cycle-test 2026-11-09T08:36:01Z  mode=no-suspend  kernel=7.2.7-an28.fc45.x86_64
   nothing was armed, suspended or written to /sys; this is the state the next run would start from
   battery: 80% (70.311 of 87.665 Wh, Charging)  AC=True  profile=balanced  PL1=35.0 W  pkg=54.0 C
   ACPI wake sources enabled (9): LID0 RP03 RP09 AWAC XHCI TXHC TDM0 TRP0 TRP1
   device wake bits enabled (9): 0000:00:06.0 0000:00:07.0 0000:00:07.1 0000:00:0d.0 0000:00:0d.2 0000:00:14.0 0000:00:1c.0 3-4 3-5
   RTC interfaces: rtc0 (clear), rtc1 (clear)
   mem_sleep: [s2idle] deep
   suspend_stats: success=0 fail=0 failed_resume=0 total_hw_sleep=0 ns
   note: powered from AC: a real drain run would refuse without --allow-ac
== verdict: NO-MEASUREMENT - no drain figure was produced; run without --no-suspend for the measurement

$ tools/repo-test.sh --only sleep-cycles --only unit
unit                fast  PASS     4.864s
sleep-cycles        fast  SKIP     0.231s  real suspend cycles need root and an explicit owner decision; the off...
== 2 suite(s): 1 pass, 0 fail, 1 skip -> exit 0
```

`--dry-run` (exit 0) prints the same live state plus the exact commands:

```
   plan: 3 cycle(s) x 60 s s2idle, settle 10 s, estimated runtime 265 s (limit 1800 s)
   rtc: rtc0 (rtc0, rtc1); output: …/docs/state/sleep-cycles-<utc>
   preconditions:
   REFUSE: real cycles need root (rtcwake and suspend): sudo tools/sleep-cycle-test.sh --cycles 3
   INVALID: the machine is on AC: the battery is not discharging, so %/h would be meaningless. …
   exact commands:
     rtcwake -m mem -s 60 -d rtc0 -v
     journalctl -k -b --since=@<cycle-start-epoch> --until=@<resume-epoch> --no-pager -o short-iso
     rtcwake -m disable -d rtc0
     sh -c 'echo 0 > /sys/class/rtc/rtc0/wakealarm'
   dry-run: nothing was armed, suspended or written.
```

Two honest limitations of this round:

1. **The real-cycle path was not executed.** This repository's rules forbid
   `sudo`, and the machine was on AC (the tool refuses a drain run on AC by
   design). The logic that needs root — `rtcwake`, the disarm trap, the
   per-cycle journal/settle path — is covered by the plan/dry-run code, the
   command-shape tests and the offline capture schema, but the first real proof
   is the owner's `sudo tools/sleep-cycle-test.sh --cycles 3`.
2. **`tools/cli-contracts.py` discovers tools from `git ls-files`**, so the two
   new files (untracked at validation time) are not in its probe list yet. Their
   contract was checked by hand: `--help` exit 0 with usage text, `--selftest`
   exit 0, `--dry-run` exit 0 and no writes, no traceback. They will be covered
   automatically once the files are tracked.

## 10. Files and maintenance

| path | role |
|---|---|
| `tools/sleep-cycle-test.sh` | the entry point named in the owner command; a thin `sh` wrapper that execs the backend (same pattern as `oxp3-firstrun.sh`) |
| `tools/oxp3_sleep_cycles.py` | the engine: sysfs readers, cycle runner, RTC trap, analyser, renderer, `--selftest` |
| `tests/unit/test_sleep_cycles.py` | the 41 offline tests |
| `tests/fixtures/sleep-cycles/*.json` | synthetic captures, one per failure mode; `build-fixtures.py` regenerates them |
| `tools/repo-test.sh` → `sleep-cycles` | the offline suite; SKIPs without root with the exact command |
| `docs/state/sleep-cycles-*/` | machine-local reports (gitignored) |

Thresholds and plausibility limits live in `DEFAULT_THRESHOLDS` at the top of
`tools/oxp3_sleep_cycles.py` and are **pinned by tests**: changing a number
means changing this page, the tests and the fixtures together, otherwise the
disagreement is the bug. Add a new failure mode by adding a scenario to
`build-fixtures.py`, regenerating, and asserting its verdict in
`test_sleep_cycles.py`.

## 11. Open points

* **This harness has not cycled the hardware yet.** The owner run above is the
  acceptance step; the first report should replace the "not yet measured" state
  honestly.
* **`max_hw_sleep` inconsistency from [hw/sleep-power.md §8](hw/sleep-power.md)**:
  the first real run will show whether it is stale. The harness records the
  counter per cycle but does not use it for the verdict (BOOTTIME/MONOTONIC is
  the primary clock).
* **60 s is short for a bag claim.** The default is a screen. If it passes,
  repeat with `--sleep 300` (or longer) before treating the number as final.
* **S0ix residency is root-only** and its ratio is informational; it is not
  part of the verdict because a platform can account residency in ways that do
  not map 1:1 to drain.
* **Hibernate and `deep` are out of scope** on this platform (lockdown/S3);
  the tool refuses a non-s2idle `mem_sleep` rather than measuring something it
  cannot guarantee to wake from.
* **A `SIGKILL` cannot run the disarm trap.** The armed alarm is the safety
  net; the worst case is a wake at the configured sleep deadline, not a
  machine that stays suspended forever.
