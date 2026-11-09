# Testing — one entrypoint for every offline check

Date: **2026-11-09**. Scope: `tools/repo-test.sh`, `Makefile`, this document.

The repository has two kinds of verification:

| what | entrypoint | needs root? | changes the host? |
|---|---|---|---|
| offline repository checks (syntax, unit tests, fixture drift, builds) | `tools/repo-test.sh` / `make test` | no | no (fast mode writes only to its log dir) |
| live hardware acceptance after a reboot | `tools/oxp3-verify.sh` | no for the default run; `--full` is invasive | reads only, unless `--full`/`--with-changes` |

This document covers the first one. The hardware harness is documented in
[VALIDATION.md](VALIDATION.md); the human evidence snapshot is in [QA.md](QA.md).

## 1. Quick start

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

tools/repo-test.sh                 # same as --fast; no network, no builds
tools/repo-test.sh --fast
tools/repo-test.sh --full          # adds the Decky build and package checks
tools/repo-test.sh --list          # inventory: mode, tier, timeout, description
tools/repo-test.sh --json          # machine-readable result
tools/repo-test.sh --only 'decky-*' # only matching suites (repeatable glob)
tools/repo-test.sh --integration   # opt-in integration suites (real daemons + uinput/uhid)

make test                          # == tools/repo-test.sh --fast
make test-full                     # == tools/repo-test.sh --full
make units                         # only the unit/udev/hwdb suites
make unit                          # only tests/unit (the Python unit suite)
```

Exit codes are `0` (nothing failed; skips are allowed and counted), `1` (at least
one FAIL or timeout) and `2` (usage/configuration error: bad option, no suite
matched, unwritable log directory, missing `timeout`). The runner prints the
failing command and its log path.

Recorded on this host (2026-11-09, no root):

```
== 34 suite(s): 28 pass, 0 fail, 6 skip -> exit 0      # --fast, ≈ 115 s (2026-11-09)
```

The inventory is the runner's, not a number copied here: `tools/repo-test.sh --list` prints the
current **34 suites**, of which **32** run in `--fast` and all 34 in `--full`, with each suite's
mode, tier and timeout. `--full` adds the two full-only rows and keeps the explained skips (§9).

## 2. Modes

| mode | suites executed | builds | network | writes |
|---|---|---|---|---|
| `--fast` (default) | 32 | none | none | only the log directory (`${XDG_CACHE_HOME:-/tmp}/oxp3-repo-test/...`) and `$TMPDIR` scratch |
| `--full` | 34 | Decky typecheck/rollup/smoke, Decky zip | none by default | also ignored build output inside the checkout: `tools/decky-oxp3-status/{node_modules,dist,build}/` |

`--full` still never installs host packages. No suite needs the network today;
the `OXP3_REPO_TEST_NETWORK=1` gate stays in the runner for a future
network-dependent suite, which would then report `SKIP` with the exact reason.

Features that don't fit a mode:

```sh
tools/repo-test.sh --full --json > result.json
tools/repo-test.sh --fast --only 'decky-*' --only 'verify-*' # OR of globs
```

Suite names are the identifiers in the table below. `--only` matches the name
with shell wildcards (`*`, `?`, `[...]`), not the description. No match is a
usage error (exit 2), never a silent success. `--list` does not create a log
directory or run anything.

## 3. Suite inventory

`mode` is when the suite runs; `tier` is the measured wall-clock class; `timeout`
is the per-suite limit enforced with `timeout --kill-after=10`.

| suite | mode | tier | timeout | needs | writes |
|---|---|---|---|---|---|
| `shell-syntax` | fast | fast | 60 s | `bash`, `sh`, `git` | – |
| `python-compile` | fast | fast | 120 s | `python3`, `git` | bytecode to `<log>/pycache` |
| `decky-build` | full | fast | 600 s | `node`, `npm`, `python3` | `node_modules/`, `dist/`, `build/` |
| `decky-package` | full | fast | 120 s | `python3`, `dist/index.js` | `build/*.zip` |
| `decky-install` | fast | fast | 120 s | `bash`, `python3`, a built plugin zip (otherwise SKIP) | private `$TMPDIR` |
| `decky-selftest` | fast | fast | 120 s | `python3` | – |
| `decky-cli` | fast | fast | 120 s | `python3` | `<log>/decky-cli.json` |
| `verify-selftest` | fast | fast | 120 s | `bash` | – (private `$TMPDIR`) |
| `verify-fixtures` | fast | fast | 120 s | `bash` | – |
| `installer-status` | fast | fast | 120 s | `bash` | – |
| `installer-dry-run` | fast | fast | 120 s | `bash`; unprivileged user context | – |
| `unit-verify` | fast | fast | 180 s | `systemd-analyze`, `sed` | `<log>/units/` |
| `udev-rules` | fast | fast | 60 s | `udevadm` | – |
| `hwdb-compile` | fast | fast | 60 s | `systemd-hwdb` | private `$TMPDIR` root |
| `shellcheck` | fast | fast | 180 s | `shellcheck` (optional) | – |
| `status-selftest` | fast | fast | 120 s | `python3`, `bash` | – |
| `led-selftest` | fast | fast | 60 s | `python3` | – |
| `hid-selftest` | fast | fast | 60 s | `python3` | – |
| `unit` | fast | fast | 120 s | `python3` | – (synthetic sysfs/config fixtures in `$TMPDIR`) |
| `voltap-integration` | fast | medium | 180 s | `python3-evdev`, `/dev/uinput` + readable event nodes | – (ephemeral uinput devices; opt-in `OXP3_REPO_TEST_INTEGRATION=1`, otherwise `SKIP`) |
| `hid-integration` | fast | medium | 240 s | `python3`, root (or ACL) on `/dev/uhid` + `/dev/uinput` | – (ephemeral uhid device; opt-in `OXP3_REPO_TEST_INTEGRATION=1`, otherwise `SKIP`; exit 77 without access) |
| `gamewrap` | fast | fast | 120 s | `python3` | – (synthetic HOME/config/panel state; stub commands) |
| `eq-model` | fast | fast | 60 s | `python3` | – |
| `firstrun` | fast | medium | 120 s | `python3`, `bash` | – (self-test in `$TMPDIR`; the dry run writes nothing) |
| `wizard` | fast | fast | 300 s | `python3`, `bash` | – (scripted stdin, `--simulate`, private state dir) |
| `triage` | fast | fast | 120 s | `python3` | – (rule selftest + 9 fixture bundles) |
| `preflight` | fast | fast | 120 s | `python3`, `bash` | – (fixtures in memory; the live `--json` is read-only) |
| `gen-docs` | fast | fast | 60 s | `python3`, `docs/hw/components.yaml` | – (registry/generated-block check; it runs the `unit` discovery for the HANDOVER block) |
| `cli-contracts` | fast | medium | 180 s | `python3`, `git` | – (probes run in a throw-away copy with `HOME`/XDG/`TMPDIR` redirected) |
| `rollback` | fast | fast | 120 s | `bash`, `python3` | – (`--root` prefix + private state dir in `$TMPDIR`) |
| `health` | fast | medium | 180 s | `python3`, `bash` | – (fixture selftest; the live runs are read-only and write only `<log>/health*.log`) |
| `acceptance` | fast | medium | 240 s | `python3`, `bash` | – (fixture selftest; one live read-only verify/health/repo-test run, the bundle goes to `<log>/acceptance-bundle.json`) |

Areas and what is actually proven:

* **preflight** — the read-only prerequisite validator (`tools/oxp3-preflight.sh`,
  `tools/oxp3_preflight.py`, `docs/PREFLIGHT.md`): parser assertions plus seven fixture
  verdicts (sane host, missing directory, old systemd, missing evdev, conflicting
  volume daemon, unprivileged) and a live schema-1 `--json` check. It runs before
  every real install and aborts on a true blocker.
* **wizard** — the interactive hardware-validation workflow
  (`tools/oxp3-wizard.sh`, [WIZARD.md](WIZARD.md)): its offline `--selftest`,
  a plan run that must write nothing, and `tests/wizard/run.sh`, which feeds
  scripted stdin through the real prompt/answer machine in `--simulate` mode and
  asserts the JSON schema, resume, the suspend gate and the restore registration.
* **shell syntax** — `bash -n` / `sh -n` on every tracked script, chosen by
  shebang (`bash` first, then `sh`, so `#!/usr/bin/env sh` is covered).
* **Python** — `py_compile` on every tracked `.py` plus the extensionless
  `tools/oxp3-voltap/oxp3-voltap`; bytecode goes to the log dir, never the
  checkout.
* **volume daemon end to end** — opt-in `voltap-integration`
  (`tests/integration/volume/`): creates ephemeral uinput devices, drives the
  real `oxp3-voltap` process through tap / three-tap / 3 s hold / bounce /
  duplicate storm / orphan / late-release sequences and reads its uinput shim,
  then exercises the HHD-preferred → AT-fallback topology and the
  `degraded-busy`/exit-3 grab refusal. It is `SKIP` unless
  `OXP3_REPO_TEST_INTEGRATION=1` and skips with the exact access command when
  `/dev/uinput` or the event nodes are not readable; it never touches a real
  input device (see [hw/volume-buttons.md](hw/volume-buttons.md)).
* **home button / LED end to end** — opt-in `hid-integration`
  (`tests/integration/hid/`): creates one virtual vendor HID device through
  `/dev/uhid` with the documented `0xFF00` descriptor and drives the real
  `oxp3-homebtn.py`, `oxp3-led.py` and `oxp3-rgbd.py` against it — `0xB2`
  press/release → `KEY_HOMEPAGE` on the daemon's uinput keyboard, exact
  `UHID_OUTPUT` bytes for a solid colour + brightness, and a destroy/recreate
  reconnect by identity. Tools are pinned with `--device` so a real
  `1a86:fe00` interface is never opened; root is needed and the suite skips
  with the exact command otherwise (see [VALIDATION.md](VALIDATION.md)).
* **Decky** — `build.sh` (typecheck, rollup, headless render smoke, backend
  `--selftest`), `package.py` run twice (the zip must be byte-identical), and
  the CLI's `--json` output must parse as JSON. The Decky CLI's live exit code
  (0/1/2) is reported but not asserted: it is a status verdict about the host,
  not a repository test. `make decky` runs the full offline build directly.
* **`oxp3-verify`** — the parser self-test (199 assertions over synthetic
  fixtures) through both documented entrypoints (`--selftest` and
  `tests/verify/run.sh`).
* **rollback** — `tests/rollback/run.sh`: installs into a `--root` prefix, adopts the
  hand-made launcher wiring, edits the launcher and a Heroic GamesConfig JSON, uninstalls and
  asserts a byte-identical restore (content and mode), idempotency of a second run and that the
  undo state stays inside the prefix. No root, no systemd, no udev. The unit library itself is
  covered by `tests/unit/test_backup.py` (15 tests in the `unit` suite).
* **installer** — `--status` and `--dry-run --all`, both read-only. A dry run
  still needs the unprivileged target user the installer wires user units for;
  a root container without `SUDO_USER` is reported as `SKIP` with that reason.
* **systemd/udev** — `systemd-analyze verify` on every unit with `Exec*=`
  stubbed to `/bin/true` (the real binaries only exist after install), the
  three user units with `--user`, `udevadm verify` on both `.rules` files, and
  `systemd-hwdb --strict` compilation of both `.hwdb` files.
* **tools** — the RGB/HID encoder self-tests (captured frames, no hardware),
  the status backend self-test, and the speaker EQ model table.
* **gamewrap** — the launch-composition decision table behind
  `tools/oxp3-gamewrap.sh` (45 tests): XMX opt-in/veto/missing-path refusal,
  HDR gating on the panel state, vibrance backend choice, MangoHud env-only
  behaviour, gamescope wrapping/inside-session handling, argv/JSON safety and
  the parsed-not-sourced XMX env file. The `unit` suite runs the same file.
* **unit** — the stdlib `unittest` suite in `tests/unit/` (the count is what `unittest` itself prints, currently 960 tests in ~5.6 s):
  HID/LED frame encoders, rgbd TOML config and frame planning, powerctl
  parsers/write ordering/readback and exit codes, adaptivetdp policy and
  hysteresis, the voltap tap state machine, IMU helpers, EDID/ICC parsers and
  colour maths, `oxp3-sagv.sh` exit codes and RAPL averaging, and the
  `install-common.sh` contract, and the launch wrapper's decisions. No root, no
  hardware, no network; synthetic sysfs trees and a synthetic HOME only. Inventory, coverage and the bugs found are in
  [UNIT-TESTS.md](UNIT-TESTS.md).
* **generated docs** — `tools/gen-docs.py --check` re-renders the three component tables from
  `docs/hw/components.yaml`, plus the `handover-state` count block in `docs/HANDOVER.md`, in
  memory and fails when any generated block in `README.md`, `docs/hw/README.md`,
  `docs/INSTALL-HW.md` or `docs/HANDOVER.md` is stale. Read-only: the suite never passes
  `--write`. The HANDOVER block derives the component counts, the harness check count, the
  runner's suite count and the unit-test count from their sources; its derivation table is in
  [hw/REGISTRY.md §7.1](hw/REGISTRY.md). The registry schema and the CI one-liner are in the
  same document.
* **CLI contracts** — `tools/cli-contracts.py` (the default run, not `--selftest`) discovers every
  tool with a help path from `git ls-files`, then runs `--help` and each advertised safe flag
  (`--selftest`, `--check`, `--dry-run`) in a throw-away copy of the tree with `HOME`, the XDG
  directories and `TMPDIR` redirected. A violation (`exit 1`) is a repository failure: nonzero or
  undocumented exit code, a traceback, missing usage text, or a write to the checkout. Documented
  deviations and denied probes live in the tool's `HELP_EXCEPTIONS`/`PROBE_DENY` tables, so an
  exception is visible instead of skipped. It prints its own discovered-tool and probe counts;
  those are not copied into this document. It automates the hand-run matrix in
  `docs/state/audit-2026-11-09.txt` (see also §11).
* **lint** — `shellcheck -x` on every shell script when installed. It is not
  installed on this host, so the suite is a `SKIP` with the install hint; it is
  never a silent pass.

## 4. Timing tiers

Measured per suite in the recorded `--fast` run (wall clock, this host):

| tier | definition | suites here |
|---|---|---|
| fast | a few seconds (< 5 s target; the timeout is the real bound) | the slowest are `audio-eq-isolation` ≈ 27.8 s, `python-compile` ≈ 7.7 s on a cold bytecode cache, `unit` ≈ 6.9 s, `unit-verify` ≈ 1.6 s (17 units) and `decky-selftest`/`status-selftest` ≈ 1.2 s; the total `--fast` run is ≈ 115 s, including the medium suites |
| medium | 5–60 s | `firstrun` ≈ 13.5 s (it runs `oxp3-verify` twice; timeout stays 120 s), `health` ≈ 7.7 s (one live verify), `cli-contracts` ≈ 6.7 s (all `--help`/flag probes) and `gen-docs` ≈ 4.9 s (it runs the unit discovery for the HANDOVER block); a cold `npm ci` or a first container `dnf install` would also land here |
| slow / network | > 60 s or requires the network | none today; the tier exists for a future network-dependent suite |

The tier is documentation in `--list`; the timeout is what actually bounds a
run. A timeout is a `FAIL` (`timeout after Ns`), not a skip.

## 5. Logs, JSON and failure reporting

Default log base:

```
${OXP3_REPO_TEST_LOG_DIR:-${XDG_CACHE_HOME:-/tmp}/oxp3-repo-test}/<UTC-run-id>-<pid>/
```

Each suite gets `<suite>.log` containing a small header (suite, mode, command,
path) and then its full stdout+stderr. `summary.txt` holds the machine-readable
one-line-per-suite summary. The directory is never pruned automatically; delete
old run directories yourself if they accumulate.

`--json` prints one object on stdout (no table):

```json
{
  "schema": 1,
  "version": "1.0.0",
  "mode": "fast",
  "repo": "/path/to/checkout",
  "log_dir": "/tmp/oxp3-repo-test/20261109T020957Z-546914",
  "counts": {"total": 24, "pass": 18, "fail": 0, "skip": 6},
  "exit_code": 0,
  "suites": [
    {"name": "shell-syntax", "mode": "fast", "tier": "fast",
     "state": "PASS", "duration_ms": 275, "command": "...",
     "log": "...", "reason": ""}
  ]
}
```

When something fails, the human table is followed by one block per failing
suite with its command, reason (`exit N` / `timeout after Ns`) and log path:

```
Failing suites (command and log):
  unit
    command: python3 -m unittest discover -s tests/unit -q
    reason:  exit 1
    log:     /tmp/oxp3-repo-test/.../unit.log
```

Skip reasons come from the suite itself (exit 77) and are shown in the table
(truncated there, complete in the log and in `--json`).

## 6. Guarantees: no root, no network, no checkout writes

`--fast` was written so it can run on a read-only checkout:

* no `sudo`/`root` is used by the runner or any fast suite;
* no command in the fast set contacts the network, and no suite needs it today;
* Python is run with `PYTHONDONTWRITEBYTECODE=1`; the explicit `py_compile`
  suite redirects bytecode with `PYTHONPYCACHEPREFIX` into the log dir;
* unit-verify stubs are written under the log dir, hwdb compilation uses a
  private `mktemp -d` root, and `oxp3-verify`/`status` self-tests use their own
  private temp directories;
* `--list` touches nothing at all.

`--full` is allowed to write the ignored build directories listed in §2. It
still does not modify tracked files. Verification on this host after a full
run: `git status --porcelain` shows no modified tracked file (only this round's
new untracked files).

## 7. Adding a suite

1. Write a function in `tools/repo-test.sh` named `suite_<name>` where `<name>`
   is the public suite name with `-` replaced by `_` (e.g. `suite_decky_build`
   for `decky-build`). Return 0 for pass, 77 for skip, anything else for fail.
   Use the helpers:
   * `need TOOL` — skip when a required tool is missing;
   * `skip "reason"` — explicit skip; the reason reaches the table and JSON;
   * `have TOOL` — optional tool probe.
   Start with `cd "$REPO" || exit 1` and never assume the caller's cwd.
2. Register it with `def_suite NAME MODE TIER TIMEOUT NETWORK DESC COMMAND`:

   ```sh
   def_suite my-check fast fast 120 0 \
       "one-line description for --list" \
       "the exact command to show when it fails"
   ```

   `MODE` is `fast` or `full`; `TIER` is `fast`, `medium` or `slow-network`;
   `NETWORK` is `0` or `1` (1 additionally requires
   `OXP3_REPO_TEST_NETWORK=1` in full mode).
3. Keep it offline in fast mode and put any writes under `$LOGDIR` or
   `$TMPDIR`. Never report a missing tool as success: use `skip`.
4. Update the inventory table in this document (suite count and the
   `--fast`/`--full` counts in §1 and in [QA.md](QA.md)), then run
   `tools/repo-test.sh --list` and the new suite in both modes.

The runner exports the suite function and its helpers into a child `bash -uc`
under `timeout`, so a suite must not rely on shell variables of the runner
other than `REPO`, `LOGDIR` and `MODE` (all exported).

## 8. CI readiness

What a container needs to run the whole suite without skips:

| area | package on a Fedora-style image | why |
|---|---|---|
| always | `bash coreutils git python3 sed findutils grep` | runner, syntax, Python suite |
| units/udev | `systemd` (`systemd-analyze`, `udevadm`, `systemd-hwdb`) | unit/udev/hwdb suites; without them those suites SKIP |
| lint | `ShellCheck` | `shellcheck` suite (optional) |
| Decky | `nodejs npm` + a warm npm cache (`npm ci --offline`) | `decky-build`; a cold cache is an explicit SKIP, not a failure |

Two container recipes were exercised for this round (both exit 0 on `--fast`):

```sh
# root container: units/udev and the installer dry-run SKIP (no systemd, no
# unprivileged user)
podman run --rm --security-opt label=disable \
  -v "$PWD":/w:z -w /w localhost/oxp3-fixes-build:f45 ./tools/repo-test.sh --fast

# non-root container: same, but the installer dry-run also runs
podman run --rm --userns=keep-id -u "$(id -u):$(id -g)" -e HOME=/tmp \
  --security-opt label=disable \
  -v "$PWD":/w:z -w /w localhost/oxp3-fixes-build:f45 ./tools/repo-test.sh --fast
```

A native CI runner with `node`, `systemd` and `shellcheck` installed can
run `make test-full` without podman. In a minimal container, expect the honest
SKIPs above; add the packages to remove them.

What cannot move into a generic CI container: the live acceptance checks in
[VALIDATION.md](VALIDATION.md) (real suspend, RAPL writes, hidraw,
Bluetooth controller), and the root-only installer paths. Those stay a
device-side step; the repository checks here are a regression net, not a
substitute for them.

CI job sketch:

```sh
tools/repo-test.sh --fast --json > fast.json     # gate: exit 0
tools/repo-test.sh --full --json > full.json     # gate: exit 0 (skips counted)
```

## 9. Recorded results (2026-11-09)

Host (`anatase`, kernel `7.2.7-an28`, no root, `make`/`gcc`/`shellcheck` absent,
`podman` and `localhost/oxp3-fixes-build:f45` present):

| run | result |
|---|---|
| `tools/repo-test.sh --fast` | 34 rows: **28 pass, 0 fail, 6 skip**, exit 0, ≈ 115 s (`audio-eq-isolation` 27.8 s, `acceptance` 18.8 s, `firstrun` 16.0 s, `health` 8.8 s, `python-compile` 7.7 s, `cli-contracts` 7.6 s) |
| `tools/repo-test.sh --only cli-contracts` | 1 row: 1 pass, exit 0, ≈ 6.7 s (`--only` selects only the matching suite) |
| `tools/repo-test.sh --list` | 34 suites; 32 run in `--fast`, 34 in `--full` |
| `tools/repo-test.sh --integration` | 2 rows: 1 pass, 1 skip, exit 0, ≈ 9.9 s (real volume daemon + ephemeral uinput devices PASS; `hid-integration` SKIPs without root on this host, with the `sudo tests/integration/hid/run.sh` hint) |
| `python3 -m unittest discover -s tests/unit -q` | `Ran 956 tests ... OK (skipped=1)`, ≈ 6.3 s |
| `tools/repo-test.sh --only shellcheck` | 0 pass / 1 skip, exit 0 (reason: not installed) |
| deliberately broken copy in `/tmp`, `--only unit` (earlier round) | 1 fail, exit 1, failing command + log path printed |
| root container `--fast` (earlier round) | 15 pass / 0 fail / 10 skip, exit 0 |
| non-root container `--fast` (earlier round) | 16 pass / 0 fail / 9 skip, exit 0 |

The six fast-mode skips are the two full-only suites (`decky-build`, `decky-package`),
`shellcheck`, the two opt-in integration suites and `sleep-cycles` (it needs root and an
explicit owner decision); they are enumerated in the table on every run, not implied.
`--full` was not re-run in this round. The authoritative current numbers are `tools/repo-test.sh --list`
and the generated `handover-state` block in [HANDOVER.md](HANDOVER.md), not any
count copied into this document.

## 10. Open points

* **Live tools are deliberately not in the offline suite.** `oxp3-imu.py
  selftest`, `oxp3-powerctl`, `display-mode.sh`, `check-working.sh` and
  `oxp3-bt-firmware.sh --check` depend on this unit's hardware, firmware or
  boot state; they belong to `tools/oxp3-verify.sh` (see
  [VALIDATION.md](VALIDATION.md)). The Bluetooth tool's `--dry-run` also
  downloads firmware to verify hashes, so it is not a fast/offline suite.
* **`make` is not installed on this host.** The Makefile was verified inside
  `localhost/oxp3-fixes-build:f45` (GNU Make 4.4.1): `make help`, `make`
  (defaults to help), `make units`, and `make -n verify decky install-dry-run
  clean` all expand correctly. On the host, use `tools/repo-test.sh` directly.
* **`shellcheck` is absent**, so the lint suite is always a SKIP here. Install
  `ShellCheck` to make it meaningful; no automatic install is attempted.
* **The Decky plugin uses npm, not pnpm.** The task's "pnpm" is not a
  dependency of this repository; `decky-build` checks `node`/`npm` and a warm
  npm cache.
* **`decky-cli` asserts structural validity, not host health.** Its exit code
  is the live status verdict (0/1/2) and is printed but not failed on; use
  `tools/oxp3-verify.sh` for the live verdict.
* **`installer-dry-run` needs an unprivileged target user** (the installer's
  own precondition). Root containers without `SUDO_USER`/`--user` SKIP; run the
  container as a normal user to exercise it.
* **The runner requires GNU `timeout` and `date +%s%N`.** Both are in
  coreutils; a busybox CI image would need them added.
* **Logs accumulate.** There is no automatic rotation; the path is printed on
  every run, and `summary.txt` makes old runs easy to diff.

## 11. Read-only probes added to the standard vocabulary (audit round)

`tests/unit/test_cli_contracts.py` is now part of the `unit` suite. It runs the
shipped `build.sh --help` and the installer's argument parser, and executes the
real `ensure_status_json()` from `tools/oxp3-verify.sh` against stubbed status
outputs (exit 1 with JSON is a verdict; a cached empty report is unavailable).
It pins the three defects fixed by the 2026-11-09 audit, so a future refactor
cannot silently reintroduce them.

These read-only probes are worth keeping in the vocabulary even though they stay
outside `repo-test.sh` (they depend on this unit's hardware or on a built tree):

```sh
tools/oxp3-flatpak-xmx.sh status --json      # sandbox-visible shim/ICD state
tools/oxp3-voltap/oxp3-voltap check          # which input device the daemon would grab
tools/display/oxp3-icc.py --out /tmp/p.icc && tools/display/oxp3-icc.py --check /tmp/p.icc
tools/oxp3-rollback.sh verify --json         # unbacked in-place edits, read-only
```

The audit matrix in `docs/state/audit-2026-11-09.txt` records the exact modes,
exit codes and verdicts for every entry point; use it as the checklist when a
tool changes its CLI or its state reporting.
