# Unit tests for the Python tooling

Date: **2026-11-09**. Scope: `tests/unit/`, the `unit` suite in
`tools/repo-test.sh`, `make unit`, and the small fixes listed in §6.

`tests/unit/` is a stdlib-`unittest` suite for the parts of the tools that can
be decided without the OneXPlayer 3: parsers, protocol encoders, policy and
hysteresis maths, config files, sysfs readers (against synthetic trees) and
the documented exit codes. It needs **no root, no hardware, no network and no
package installation**, and it writes only to a private temporary directory
(`$TMPDIR`). It is part of `tools/repo-test.sh --fast`, so `make test` runs it.

Recorded on this host (Python 3.15, no root):

```
python3 -m unittest discover -s tests/unit -q     -> Ran 470 tests ... OK (skipped=1), ~1.3 s
tools/repo-test.sh --fast  -> 25 suites: 19 pass, 0 fail, 6 skip, exit 0
tools/repo-test.sh --full  -> 25 suites: 23 pass, 0 fail, 2 skip, exit 0
```

## 1. How to run it

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

make unit                                        # all of tests/unit
python3 -m unittest discover -s tests/unit -q    # same, without make
tools/repo-test.sh --fast --only unit            # the suite through the runner
tools/repo-test.sh --list                        # confirms the "unit" row (120 s)
```

One test / one module (discovery puts `tests/unit` on `sys.path`, so
`support.py` imports; run from `tests/unit` when naming modules directly):

```sh
cd tests/unit
python3 -m unittest test_powerctl.ExitCodeTest.test_write_failure_is_exit_2_not_3 -v
python3 -m unittest test_voltap -v
python3 -m unittest discover -s . -k readback -v   # every test whose name matches
```

The suite is deterministic: no sleeps (the SAGV measurement stubs `sleep`), no
clock dependence, no host sysfs access in any assertion. Tests that must touch
the host's real `/sys` at all do so only through patched paths; the SAGV and
install-common tests run the shell tools in a stub `PATH` and a private
`$TMPDIR`.

## 2. What is covered

| module | tool under test | tests | main seams |
|---|---|---:|---|
| `test_hid.py` | `tools/oxp3_hid.py` | 40 | v1 frame envelope, 64-byte length, payload limit, solid/breath/preset/off/intercept encoders, all 9 captured HHD frames byte-for-byte, report validation, colour/side/preset parsers, hidraw discovery on a synthetic sysfs tree, partial `os.write` handling |
| `test_led.py` | `tools/oxp3-led.py` | 16 | level validation, per-command frame order, `--dry-run` output, side aliases, preset note for secondary zones, probe palette, open/send/close path, exit paths |
| `test_rgbd.py` | `tools/oxp3-rgbd.py` | 23 | TOML schema matrix (effects, levels, `brightness=keep`, presets, defaults), unknown zone/colour/preset errors, zero-padded and hex zone ids, frame planning, `--check` report and exit codes, `--once` device/permission/config failures |
| `test_powerctl.py` | `tools/oxp3-powerctl.py` | 86 | `parse_tau`, `fmt_w`/`fmt_tau`, `parse_charge_behaviour`, `probe_state` classification, fan curve parsing/interpolation/sorting, RAPL domain discovery and partial limits, write ordering (raise = PL4→PL2→PL1, lower = PL1→PL2→PL4), readback verification and tolerance, battery health/bypass classification, charge-limit and charge-behaviour success/failure/read-only/absent paths, state JSON, snapshot/restore, **exit codes 0/2/3/4** |
| `test_adaptivetdp.py` | `tools/oxp3-adaptivetdp.py` | 39 | policy ladder and all clamps (soft/hard temperature, battery, floor, ceiling, opt-in), hysteresis dwell/min-interval/immediate/revert, `/proc/stat` delta maths and iowait, GPU/AC/battery sampling, daemon step logging (`change`/`noop`/`error`), restore, dry-run |
| `test_voltap.py` | `tools/oxp3-voltap/oxp3-voltap` | 49 | the `Tapper` state machine (repeats, duplicate press, bounce, watchdog release, orphan-release taps, `--no-orphan-tap`, release-all, per-key state, stats, EV_REP proxy); `/proc/bus/input/devices` + sysfs discovery and bitmap parsing; holder detection from `/proc/*/fdinfo`; hhd process detection; candidate classification/scoring and topology selection (hhd preferred, free physical fallback, explicit override, never the shim); the `GrabFSM` degraded/reconnect transitions and exit codes |
| `test_imu.py` | `tools/oxp3-imu.py` | 24 | HHD controller-mode matrix (`motion_exposed`), sysfs readers, IIO discovery by ACPI id/label/fallback, sample decoding, rate maths, `rate`/`watch`/`info` output and exit codes |
| `test_edid.py` | `tools/display/oxp3-edid.py` | 19 | synthetic EDID base block + CTA-861 extension: manufacturer id, chroma packing, detailed timing/refresh, descriptors, range limits, colourimetry, HDR static metadata, checksums, DisplayID/unknown extensions, gamut class, verdict, CLI exit 1 |
| `test_icc.py` | `tools/display/oxp3-icc.py` | 22 | `xy_to_xyz`, matrix multiply/inverse, Bradford adaptation D65↔D50, white-normalised sRGB RGB→XYZ matrix, s15Fixed16/u8Fixed8 tag round trips, ICC v2 build→read round trip (header, 9 tags, D50 wtpt, gamma), corrupt-profile rejection, CLI `--check`/`--out`/`--from-edid` errors |
| `test_sagv.py` | `tools/oxp3-sagv.sh` | 14 | usage exit 4, `set off/on/point` output and refusals (exit 3), `measure` minimum window, RAPL sample averaging against a fixture dir (stub `sleep`), `status`/`report` read-only exit 0 |
| `test_install_common.py` | `tools/lib/install-common.sh` | 18 | sourcing has no side effects, the `install_common_guard` contract (API version, every required variable, non-empty arrays, caller `build_mesa`), `ucode_rev`, `xmx_override_file`, `anatase_kver`, `hotfix_active`, `deploy_info`, `mok_cn`/`mok_usage_ok` against generated CA/non-CA certs |
| `test_sleep_trigger.py` | `files/logind-oxp3.conf`, `files/49-oxp3-suspend.rules`, `tools/oxp3-hw-install.sh`, `tools/oxp3-verify.sh` | 23 | the drop-in parses as one `[Login]` section with the documented `Handle*` values (power/suspend key = suspend, lid = suspend AC+battery, docked = ignore, long-press = poweroff), no key wired to hibernate, no duplicate keys; the polkit rule is one `addRule`, balanced, limited to the four suspend/hibernate actions, guarded on `subject.local && subject.active && isInGroup("wheel")`, and never grants `suspend-ignore-inhibit` (comments stripped first); installer wiring (TRIGGER_FILES table used by both install and uninstall, status lines, no logind restart behind the user's session); verify wiring (`record sleep.trigger`, the three-way `--full`/`--with-changes`/`OXP3_ALLOW_SUSPEND` gate, one and only one suspend call site, every `trigger_verdict` outcome present) |

Shared fixtures live in `tests/unit/support.py`: module-by-path loader,
`TmpTree`, and builders for synthetic powercap zones, batteries, AC nodes and
oxpec hwmon fans. The EDID/ICC tests build their EDID blob from scratch (our
own bytes), not from a vendor binary.

### Exit codes asserted

* `oxp3-powerctl`: 2 for a missing/refused control or invalid argument, **3 for
  a readback mismatch (and only for that)**, 4 for a partial `status`.
* `oxp3-rgbd`: 1 for a bad/missing config or a device that could not be
  reached; `--check` returns 1 when the device is absent.
* `oxp3-icc`: 1 for an unreadable/corrupt profile, bad EDID or unwritable
  `--out` (never a traceback).
* `oxp3-edid`: 1 for a file that is not an EDID base block.
* `oxp3-sagv.sh`: 3 for "no interface / refused / no samples", 4 for usage.
* `install-common.sh`: 1 with a named variable/API/`build_mesa` message.

## 3. What cannot be tested offline, and why

These need the device (and most are already covered by
[tools/oxp3-verify.sh](../tools/oxp3-verify.sh), see
[VALIDATION.md](VALIDATION.md)):

* **hidraw and uinput I/O.** Frame *encoding* is proven against captured HHD
  frames; whether the firmware accepts a frame, whether the node is really
  usage page 0xFF00, and whether uinput delivers `KEY_HOMEPAGE` are device
  facts. Writes also depend on the udev rule (`tools/oxp3-hid.rules`). The
  descriptor question is now observed read-only by `hid.node`, and the empty
  `--sides` no-op by `led.sides`.
* **RAPL and hwmon writes.** The tests prove the write order, the quantisation
  tolerance and the classification of a refused write, but not that the
  kernel/BIOS accepts a value or that `pwm1` moves the EC fan. Kernel
  quantisation, the RAPL lock bit, `tt_toggle` and the EC's own scaling are
  observable only on hardware.
* **Battery charge control.** Success/failure/readback classification is
  tested on plain files; the real `charge_behaviour` mode list rendered by the
  ACPI battery driver, the EC's accepted range and `EINVAL` behaviour are
  device facts.
* **Daemon timing and races.** The rgbd HHD journal watcher (journal line
  format, re-connect, resume via `CLOCK_BOOTTIME`), the settle/debounce window,
  and the voltap re-scan/grab loop are threaded and event-driven; they are
  exercised by `--check`/`--once` logic tests and by `oxp3-verify.sh --full`,
  not here. A mock journal would only test the mock.
* **SAGV.** `status` reads host sysfs/PCI config and the journal; only the
  argument parsing, exit codes and the RAPL averaging are offline. `probe`
  scans the live `/sys` (up to 60 s) and is not run in the suite.
* **install-common build/install paths.** Downloads, `rpm-ostree`, `podman`,
  `sudo` writes and MOK enrolment are not reachable without changing the host.
  The contract, the parsing helpers and the certificate classification are
  tested; `sys_fw`, `host_mesa` and `flatpak_mesa` are not (hard-coded host
  paths, untestable without a path override) — `inst.paths` now sources the
  library on the live host and calls all three.
* **Display integration.** `display-mode.sh` and `vibrance.sh` wrap
  `kscreen-doctor`/vulkan state; the EDID and ICC *inputs* they consume are
  tested, the compositor side is not.
* **powerctl `probe_state("root-only")`** cannot be produced without root; the
  test mocks `os.access` for that branch.

## 4. Risks found and fixed in this round

Each fix has a regression test in the module named; the docstrings say why.

| # | where | bug / risk | fix |
|---:|---|---|---|
| 1 | `oxp3_hid.parse_rgb` | named colours `red`, `off` (3 chars) and `orange` (6 chars) were **unreachable**: the hex-length heuristics ran first and raised `SystemExit` on `int("rr", 16)`. | check `NAMED_COLORS` before the hex/numeric heuristics; components outside 0–255 now raise a clean `SystemExit` instead of a `ValueError` from `bytes()`. |
| 2 | `oxp3_hid.send` | `os.write()` return value was only summed; a short hidraw write silently dropped the rest of the frame. | `_write_frame()` loops until the whole frame is written and raises `OSError` on a zero-byte write. |
| 3 | `oxp3-rgbd._zone_side` | a zero-padded TOML key (`[zones.05]`) was rejected (`int("05", 0)` is invalid), although `oxp3-led.py --sides 05` accepts it. | accept decimal digits after the base-0 attempt. |
| 4 | `oxp3-rgbd` `--once` | exited **0** when the vendor hidraw node was absent or the open was denied: a silent success that applied nothing. | `apply()` returns whether frames reached a device; `run_once()` exits 1 otherwise. |
| 5 | `oxp3-powerctl.apply_limits` | readback verification also ran for writes that had **failed**, so `EACCES` was reported as a readback mismatch and the tool exited **3** instead of the documented 2. | verify only `(domain, level)` pairs present in `report["written"]`. |
| 6 | `oxp3-powerctl.Domain.get`/`get_window` | non-numeric/truncated sysfs content escaped as an uncaught `ValueError`. | treat it as unreadable (`None`), like a read error. |
| 7 | `oxp3-powerctl.load_curve` | a malformed curve line or an unreadable file escaped `main()` as a traceback (exit 1) instead of a `ControlError` (exit 2 + hint). | wrap `OSError`/`ValueError` in `ControlError`; pwm is rounded rather than truncated. |
| 8 | `oxp3-voltap Tapper` | after any normal tap, `origin[]` stayed `"press"`, so the **next orphan release** (the hhd case this feature exists for) was counted as a late release and silently dropped. | replaced `origin` with a `synthetic_tail` set: only the tail of a release we synthesised is dropped; a new accepted press clears it. `stats["forwarded"]` no longer counts dropped repeats. |
| 9 | `oxp3-imu` `watch` | `int(read(...))` raised `ValueError` on a transient empty/garbled sysfs read and killed the display. | use `_read_int()` (already used by `rate`). |
| 10 | `oxp3-icc` CLI | `--check` on a corrupt profile, `--from-edid` on a bad EDID and `--out` into a missing directory raised tracebacks; the doc says exit 1. | catch `OSError`/`ValueError` and return 1 with a message. |
| 11 | `install-common.sh ucode_rev` | a missing file made `od` fail; with the caller's `set -e`/`pipefail` the `have=$(ucode_rev "$(sys_fw …)")` in `build_firmware` aborted the install whenever the installed file did not exist yet (the normal fresh-system case). | return empty for a missing file; the caller already guards with `[[ -n $have ]]`. |
| 12 | `oxp3_hid` (hygiene) | `find_vendor_hidraw()` leaked file handles (ResourceWarning); the selftest's prefix fallback was dead code that would accept a truncated future vector. | context-managed reads; exact 64-byte comparison in `selftest()`. |

Behaviour change for the device team to re-validate: **#8** changes which
releases produce a tap after a previous tap (the rocker should now react to a
press sequence that used to be swallowed). It is unit-proven and strictly
closer to the documented rule, but re-run `sudo tools/oxp3-voltap/oxp3-voltap
probe` / one tap test on the unit.

## 5. Deferred risks (device-validated or still deferred)

Each row now says how the risk is validated on the unit. The harness checks are in
[tools/oxp3-verify.sh](../tools/oxp3-verify.sh); read-only ones run in the default
mode, `fan.savemode` only with `--full --with-changes` because it writes live and
persistent state (and restores both).

| risk | status | how it is validated / what remains |
|---|---|---|
| `oxp3_hid.find_vendor_hidraw()` ignores the report descriptor (the `if b"\x06\x00\xff" in desc` branch is dead): the first input-less 1a86:fe00 node wins. | **covered: `hid.node`** (default) | reads the `report_descriptor` of every 1a86:fe00 node without an input child, checks for usage page `0xFF00` (`06 00 ff`), and compares it with the tool's choice. PASS on this unit (only `/dev/hidraw4` is a candidate); FAIL if the heuristic ever picks a node without the vendor page. The fix (make the descriptor authoritative) is still a design call. |
| `oxp3-adaptivetdp`: `on_ac is None` (no AC/Mains node) skips **both** battery clamps, so performance stays allowed on battery. | **covered: `pwr.ac`** (default) | one unprivileged `--dry-run --once` sample plus the battery's own `status`: PASS when `on_ac` is readable (this unit: `on_ac=true`), FAIL if performance is allowed while the battery is discharging on a unit without `ADP1`, SKIP otherwise. A safer default is still a design call. |
| `oxp3_hid.parse_sides([""])` selects nothing; `oxp3-led.py --sides ""` emits zero frames and exits 0. | **covered: `led.sides`** (default) | `--dry-run` proves the empty selection encodes 0 frames (never "all zones") while `--sides 6` still encodes 2; no device access. Raising on a non-empty operand that selects nothing is still a design call. |
| `oxp3-powerctl battery limit` with **no** percentage sets the limit to 100 ("off") instead of printing the current value. | **covered: `bat.limit`** (default) | `--dry-run --json` shows `requested=100` with the live threshold unchanged, and the real no-argument call is refused without root (exit 2). If the CLI is ever changed to print the current value the row must change with it. |
| `oxp3-powerctl fan manual` (no speed) stores `mode="auto"` in the state file even though manual mode was written. | **covered: `fan.savemode`** (`--full --with-changes`) | writes manual mode with `--save`, reads `fan.mode` back (PASS only for `manual`), then restores `pwm1_enable`, the previous `pwm` and `/etc/oxp3/power.json` (verified). FAILs loudly if a restore does not take. |
| `oxp3-powerctl.apply_limits` mixed raise/lower: all raising levels are written before lowering ones, not per-level ceiling-first. | **covered: `pwr.order`** (default) | `profile balanced --dry-run --json` against the live limits; both domains here are genuinely mixed (MMIO PL2 52 W > 45 W), so the row checks the real order: raises ceiling-first, every lowering last. The CLI still cannot emit an arbitrary per-level mixed write. |
| `curve_pwm()`'s `t1 == t0` branch is effectively unreachable for sorted points (the first matching pair wins). | **covered: `fan.curvedup`** (default) | a curve file with a duplicate 60 C point is accepted and yields a defined pwm in one dry-run step. Duplicate points are documented as "the first matching slope wins". |
| `voltap.release_all()` marks the released codes as a synthetic tail, so a real release from a freshly re-grabbed device could be dropped once. | **still deferred: manual step** | needs an HHD restart plus one physical rocker press while the daemon re-scans; synthesising it would race the daemon. Procedure below. |
| `rgbd --settle`/debounce/HHD journal parsing. | **covered: `rgbd.timing`** for the precondition; re-apply still event-driven | proves the vendor node is openable and the hhd journal is readable (without it the daemon silently polls `hhdctl`), and that the example config encodes frames. The settle/debounce re-apply itself is only visible as `HHD settle +Ns` lines in `journalctl --user -u oxp3-rgbd` after a resume. |
| `install-common.sh`: `sys_fw`, `host_mesa`, `flatpak_mesa` hard-code `/usr` paths. | **covered: `inst.paths`** (default) | sources the library the way its callers do and calls all three against the live host (this unit: `tgl_dmc_ver2_12.bin.xz`, Mesa `26.2.4`, Flatpak "same as the host"). |
| `oxp3-imu.find_iio` fallback accepts any non-empty `in_anglvel_x_raw` (a `"0"` counts). | **covered: `imu.iio`** (default) | enumerates every `iio:device*` and proves the ACPI-id/label device (`iio:device0`, `10EC5280`/`accel-display`) is what `oxp3-imu.py -j info` selected; FAILs if a fallback device wins while a primary match exists. |

### 5.1 The one remaining manual step: voltap release-all tail

This is the only deferred item that no read-only probe can settle. On the unit, with
the standalone daemon installed and the rocker working:

```sh
# 1. restart HHD (the device the daemon re-grabs) and watch our daemon re-scan
sudo systemctl restart hhd
journalctl -u oxp3-voltap -b -n 20 | grep 'grabbed /dev/input/event'

# 2. press the rocker volume-up exactly once, then check one step happened
journalctl -u oxp3-voltap -b | tail -n 5

# 3. read the counters (printed on daemon stop, or with --stats <seconds>)
sudo systemctl restart oxp3-voltap
journalctl -u oxp3-voltap -b | grep 'exit stats'
```

Expected: exactly one OSD step for the press, and `late_release` stays 0 (or grows by
at most 1 for the press whose own release we synthesised). A second tap for one press,
or `late_release` growing on *every* press after a restart, is the dropped-release tail
the risk describes; report it with the journal lines.

## 6. Integration

* `tools/repo-test.sh`: suite **`unit`** (`def_suite unit fast fast 120 0 …`),
  function `suite_unit()`, runs in `--fast` and in `--full`, logs to
  `<logdir>/unit.log`. Skips with a reason if `tests/unit` or `python3` is
  missing (it never silently passes).
* `Makefile`: `make unit` (and `make help` lists it); `make test` includes it
  through `repo-test.sh --fast`.
* `make unit` was verified in the local build container
  (`localhost/oxp3-fixes-build:f45`, the host has no `make`): `Ran 302 tests …
  OK (skipped=2)`. The difference from 470 is the container's missing
  `python3-evdev`: `test_voltap` then skips at import with that reason (one
  module-level skip instead of 17 tests). Install `python3-evdev` in a CI image
  to run them.
* `tests/unit` is untracked in this round (the working tree is left for review);
  `git add tests/unit docs/UNIT-TESTS.md` is deliberate step when committing.
  Until then the `python-compile` suite does not see the new files, but the
  `unit` suite imports every one of them, so a syntax error still fails the run.
* The ten deferred-risk probes added their own parser fixtures under
  `tests/verify/fixtures/`; `tools/oxp3-verify.sh --selftest` went from 52 to **95**
  assertions and the declared check inventory from 60 to **70 ids** (64 rows in the
  default mode, 68 with `--full`, 70 with `--with-changes`). `tools/repo-test.sh` runs
  the selftest through the `verify-selftest` and `verify-fixtures` suites, so the
  offline `--fast` run covers every new parser and every SKIP path.
* Fast-mode contract holds: no root, no network, no write outside `$TMPDIR`.
