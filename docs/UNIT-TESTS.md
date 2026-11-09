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
python3 -m unittest discover -s tests/unit -q     -> Ran 318 tests ... OK (skipped=1), ~0.6 s
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
| `test_voltap.py` | `tools/oxp3-voltap/oxp3-voltap` | 17 | the `Tapper` state machine: repeats, duplicates, bounce, watchdog release, orphan-release taps (hhd case), orphan after a normal tap, `--no-orphan-tap`, release-all, per-key state, stats |
| `test_imu.py` | `tools/oxp3-imu.py` | 24 | HHD controller-mode matrix (`motion_exposed`), sysfs readers, IIO discovery by ACPI id/label/fallback, sample decoding, rate maths, `rate`/`watch`/`info` output and exit codes |
| `test_edid.py` | `tools/display/oxp3-edid.py` | 19 | synthetic EDID base block + CTA-861 extension: manufacturer id, chroma packing, detailed timing/refresh, descriptors, range limits, colourimetry, HDR static metadata, checksums, DisplayID/unknown extensions, gamut class, verdict, CLI exit 1 |
| `test_icc.py` | `tools/display/oxp3-icc.py` | 22 | `xy_to_xyz`, matrix multiply/inverse, Bradford adaptation D65↔D50, white-normalised sRGB RGB→XYZ matrix, s15Fixed16/u8Fixed8 tag round trips, ICC v2 build→read round trip (header, 9 tags, D50 wtpt, gamma), corrupt-profile rejection, CLI `--check`/`--out`/`--from-edid` errors |
| `test_sagv.py` | `tools/oxp3-sagv.sh` | 14 | usage exit 4, `set off/on/point` output and refusals (exit 3), `measure` minimum window, RAPL sample averaging against a fixture dir (stub `sleep`), `status`/`report` read-only exit 0 |
| `test_install_common.py` | `tools/lib/install-common.sh` | 18 | sourcing has no side effects, the `install_common_guard` contract (API version, every required variable, non-empty arrays, caller `build_mesa`), `ucode_rev`, `xmx_override_file`, `anatase_kver`, `hotfix_active`, `deploy_info`, `mok_cn`/`mok_usage_ok` against generated CA/non-CA certs |

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
  facts. Writes also depend on the udev rule (`tools/oxp3-hid.rules`).
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
  paths, untestable without a path override).
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

## 5. Deferred risks (not changed: need device validation or a design call)

| risk | why it was not changed | recommendation |
|---|---|---|
| `oxp3_hid.find_vendor_hidraw()` ignores the report descriptor (the `if b"\x06\x00\xff" in desc` branch is dead): the first input-less 1a86:fe00 node wins. | Choosing a node by descriptor could select a different `/dev/hidrawN` than the one the firmware uses; needs a check of all three interfaces on the unit. | skipped test `test_descriptor_is_authoritative`; verify `report_descriptor` for hidraw0/1/2 with `od -An -tx1`, then make the descriptor authoritative. |
| `oxp3-adaptivetdp`: `on_ac is None` (no AC/Mains node) skips **both** battery clamps, so performance stays allowed on battery. | A safer default ("unknown = battery") changes the live policy; validate on a unit with and without `ADP1`. | pinned test; consider `on_ac = False` when unknown, or refuse to raise above balanced with a warning. |
| `oxp3_hid.parse_sides([""])` selects nothing; `oxp3-led.py --sides ""` emits zero frames and exits 0. | Changing library semantics affects both callers; degenerate input only. | pinned test; raise `SystemExit("no sides selected")` when a non-empty operand yields no ids. |
| `oxp3-powerctl battery limit` with **no** percentage sets the limit to 100 ("off") instead of printing the current value. | Existing CLI contract; a change is user-visible. | require the argument, or make no-argument print the current threshold. |
| `oxp3-powerctl fan manual` (no speed) stores `mode="auto"` in the state file even though manual mode was written. | Not exercised by the documented flows; `restore` would then select auto. | store `mode="manual"` when `pwm1_enable` was written. |
| `oxp3-powerctl.apply_limits` mixed raise/lower: all raising levels are written before lowering ones, not per-level ceiling-first. | A profile switch is always all-raise or all-lower, so the mixed order is unreachable from the CLI today. | if arbitrary per-level writes are added, order by dependency (open the ceiling before raising any limit). |
| `curve_pwm()`'s `t1 == t0` branch is effectively unreachable for sorted points (the first matching pair wins). | Harmless duplicate-point handling; no incorrect output found. | pinned test; document duplicate points as "last slope wins above the duplicate". |
| `voltap.release_all()` marks the released codes as a synthetic tail, so a real release from a freshly re-grabbed device could be dropped once. | Device replacement mid-press is rare and the daemon re-scans; needs a real hhd restart to observe. | keep an eye on the `late_release` counter in `--stats` after a hhd restart. |
| `rgbd --settle`/debounce/HHD journal parsing. | Threads + external journal; a mock would not prove the real line format. | covered by `oxp3-verify.sh` with HHD running; the journal parser itself (`_check_frames`) only accepts `b83f01…` 64-byte envelopes. |
| `install-common.sh`: `sys_fw`, `host_mesa`, `flatpak_mesa` hard-code `/usr` paths. | They are host-inspection helpers by design; no seam to inject a root. | if they grow logic, add an override variable (`OXP3_USR_PREFIX`) and unit-test it. |
| `oxp3-imu.find_iio` fallback accepts any non-empty `in_anglvel_x_raw` (a `"0"` counts). | Not wrong on this unit; a different IIO device could match first. | check the `name`/`label` first (already the primary path); a stricter fallback could require a numeric parse. |

## 6. Integration

* `tools/repo-test.sh`: suite **`unit`** (`def_suite unit fast fast 120 0 …`),
  function `suite_unit()`, runs in `--fast` and in `--full`, logs to
  `<logdir>/unit.log`. Skips with a reason if `tests/unit` or `python3` is
  missing (it never silently passes).
* `Makefile`: `make unit` (and `make help` lists it); `make test` includes it
  through `repo-test.sh --fast`.
* `make unit` was verified in the local build container
  (`localhost/oxp3-fixes-build:f45`, the host has no `make`): `Ran 302 tests …
  OK (skipped=2)`. The difference from 318 is the container's missing
  `python3-evdev`: `test_voltap` then skips at import with that reason (one
  module-level skip instead of 17 tests). Install `python3-evdev` in a CI image
  to run them.
* `tests/unit` is untracked in this round (the working tree is left for review);
  `git add tests/unit docs/UNIT-TESTS.md` is deliberate step when committing.
  Until then the `python-compile` suite does not see the new files, but the
  `unit` suite imports every one of them, so a syntax error still fails the run.
* Fast-mode contract holds: no root, no network, no write outside `$TMPDIR`.
