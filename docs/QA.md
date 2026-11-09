# Repository QA — offline checks and evidence snapshot

Date: **2026-11-09** (Europe/London; commands run 01:32–01:41 UTC).
Repo revision at start: **4dc7366** (`fingerprint: tested protocol library + compile-checked libfprint
driver; decky: deterministic build`), author `silentsudin`.
Update (licensing round, same day, unprivileged): added [PROVENANCE.md](PROVENANCE.md),
`tools/apply-license.py` and `tools/provenance-audit.py`; validated the licence application on a
`/tmp` copy only. No licence was applied to this checkout.
No `sudo`, no reboot, no host configuration change, no privileged write was performed. Everything marked
"root" below is written out as a command, not executed.

## Environment

| fact | value |
|---|---|
| OS | Anatase `20261026.3`, rpm-ostree, kernel `7.2.7-an28.fc45.x86_64` |
| systemd | 262 (`262-3.fc45`) |
| Python | 3.15.0 (system `python3`; no venv in the repo) |
| Node / npm | v22.23.1 / 10.9.8 |
| C toolchain | **not installed on the host** (`gcc`, `cc`, `make` absent) |
| Containers | podman, local image `localhost/oxp3-fixes-build:f45` (gcc + make, no GLib/GUsb) |
| Other tools | `systemd-analyze`, `udevadm`, `systemd-hwdb`, `jq`, `7z` present; **`shellcheck` absent**; **`pytest` absent** (the suites use stdlib `unittest`) |
| Hardware state | no unit installed by this repo; `/usr/lib/firmware/updates` already carries the two BT files; PL1/PL2/PL4 = 35/37/122 W |

## Result summary

| result | count |
|---|---:|
| PASS | 50 |
| DECISION PENDING | 1 |
| FAIL | 0 |
| SKIP | 2 |

`PASS (expected exit N)` rows are checks whose tool deliberately exits non-zero on this host (SAGV probe,
Bluetooth diagnosis, `--strict` with warnings); the exit code was the documented, correct one.
The one pending item is the licence-header/top-level-licence gap (check 23): the repair is written,
validated and waiting for the owner's licence choice, so it is no longer a FAIL. No functional test
failed.

## Checks

| # | area | check | command (from repo root) | result | evidence / note |
|---:|---|---|---|---|---|
| 1 | shell syntax | `bash -n` on every bash script (25) | loop over tracked files whose shebang is bash, `bash -n "$f"` | **PASS** | 25/25, no output. Scripts in `files/` included even though the task asks for `tools/`+root. |
| 2 | shell syntax | `sh -n` on every POSIX-sh script (8) | same loop, `sh -n "$f"` | **PASS** | 8/8, no output. |
| 3 | Python | `py_compile` on every `.py` plus `tools/oxp3-voltap/oxp3-voltap` | `PYTHONPYCACHEPREFIX=/tmp/qa-pyc python3 -m py_compile <file>` | **PASS** | 26/26. `PYTHONPYCACHEPREFIX` kept `__pycache__` out of the checkout. |
| 4 | fingerprint | Python protocol suite | `python3 -m unittest discover -s tools/fingerprint/tests -v` | **PASS** | `Ran 85 tests ... OK`, 0.021 s. |
| 5 | fingerprint | C protocol suite | `tools/fingerprint/tests/run-c-tests.sh` | **PASS** | No host compiler → podman fallback, image `localhost/oxp3-fixes-build:f45`; `ok: 324 checks, 10 vectors`. |
| 6 | fingerprint | fixture/vector drift | `python3 tools/fingerprint/tests/generate_fixtures.py && git diff --exit-code -- tools/fingerprint/tests` | **PASS** | Regenerated 15 files; `git diff` empty, working tree unchanged. `test_generated_data.py` also enforces this in-memory. |
| 7 | fingerprint | driver vs real libfprint 1.94.9 | `tools/fingerprint/tests/check-libfprint.sh` (with `/tmp/libfprint-1.94.9` = `v1.94.9`) | **PASS** | Podman fallback installed `glib2-devel libgusb-devel` in the ephemeral container; `syntax-real: OK ... -Wall -Wextra -Werror`. |
| 8 | fingerprint | driver vs offline stub headers | `podman run ... make syntax` | **PASS** | `syntax: OK (stub headers, not libfprint)`. |
| 9 | Decky | offline dependency install + typecheck + bundle | `cd tools/decky-oxp3-status && ./build.sh --offline` | **PASS** | `npm ci --offline` (registry not contacted, 134 packages from cache), `tsc --noEmit`, rollup. |
| 10 | Decky | headless load/render smoke | `npm run smoke` (inside `build.sh`) | **PASS** | `dist/index.js loaded and rendered (483 bytes of markup, api handshake ok)`. |
| 11 | Decky | backend self-test | `python3 main.py --selftest` (inside `build.sh`) | **PASS** | `selftest: 60 checks passed (synthetic /sys, /proc, /run, HOME)`. |
| 12 | Decky | package + determinism | `python3 package.py` twice (`--offline` then `--no-install`) | **PASS** | `build/oxp3-status-1.0.0.zip`, 33,843 bytes, sha256 `896ccee7…70eaa` both times — matches `docs/decky-plugin.md`. |
| 13 | systemd | all system units | `systemd-analyze verify` on copies with `ExecStart`/`ExecStop` stubbed to `/bin/true` | **PASS** | 14/14. Stubbing is necessary only because `/usr/local/libexec/*` exists after install; without it verify fails exactly with `Command /usr/local/libexec/oxp3-powerctl is not executable`. Three units whose binaries already exist (`oxp3-audio-resume@test`, `oxp3-gaming-sched`, `oxp3-rapl-pp1`) were also verified unstubbed and passed, including `%i` expansion. |
| 14 | systemd | all user units (`graphical-session.target`) | `systemd-analyze --user verify` on stubbed copies | **PASS** | 3/3: `oxp3-audio-route`, `oxp3-homebtn`, `oxp3-rgbd`. |
| 15 | udev | every `.rules` file | `udevadm verify <file>` | **PASS** | 2 files (`files/99-oxp3-sleep-wakeup.rules`, `tools/oxp3-hid.rules`): Success 1, Fail 0 each. |
| 16 | udev | volume hwdb compilation | `systemd-hwdb --root=<tmp> --strict update` after copying each file to `<tmp>/etc/udev/hwdb.d/` | **PASS** | 2/2; non-empty `hwdb.bin` produced, strict mode, no diagnostics. |
| 17 | lint | `shellcheck` | `command -v shellcheck` | **SKIP** | Not installed on the host; no network install attempted. `bash -n` is the only shell lint available. |
| 18 | hygiene | tracked vendor binaries/firmware | `git ls-files` + magic-byte scan (`ELF`, `MZ`, `PK`, PNG, `%PDF`) + extension scan | **PASS** | 0 binaries. Largest tracked file is a 126,183-byte text patch; no `.dll/.exe/.sys/.bin/.sfi/.ddc/.rpm/.zip`. All 253 files tracked at the time were text. |
| 19 | hygiene | tracked build artifacts | `git ls-files` path scan for `node_modules/`, `dist/`, `build/`, `__pycache__`, `*.pyc`, `*.zip`, `*.o`, `*.so` | **PASS** | 0 tracked. `node_modules/` (50 MB), `dist/`, `build/` and `tools/fingerprint/build/test_proto` exist locally but are untracked/ignored. |
| 20 | hygiene | `.gitignore` coverage | `git check-ignore` on probe paths, then re-check | **PASS (fixed)** | Before: nested Decky `dist/build/node_modules` ignored, but a root `foo.zip`/`foo.pyc` was not. Added generic `dist/`, `build/`, `node_modules/`, `*.zip`, `*.pyc`, `*.pyo` to `.gitignore`; all probes now ignored. |
| 21 | hygiene | large files | `git ls-files` sizes | **PASS** | Max 126,183 B; no file > 128 KiB, no LFS pointers. |
| 22 | hygiene | secrets/tokens/keys | `git grep -nEI` for AWS/GitHub/Slack tokens, `BEGIN … PRIVATE KEY`, `password\|secret\|api_key = "…"`, `sk-…`; plus credential-in-URL/curl-auth scan | **PASS** | 0 matches across all tracked text, including docs, scripts and fixtures. |
| 23 | hygiene | licence headers / top-level licence | first 8 lines of every tracked source-like file; then `tools/apply-license.py --check …` | **DECISION PENDING (ready to apply)** | No `LICENSE`/`COPYING` and **60 of 71** source-like files had no `SPDX-License-Identifier` at the audit revision; the fingerprint subtree (12 files) already declares `LGPL-2.1-or-later`. No licence is asserted by this repository: [PROVENANCE.md](PROVENANCE.md) records the provenance guarantee and `tools/apply-license.py` applies the owner's choice in one reviewable commit. Its scope covers every hand-written source (81 files at HEAD, including the Java helpers, the `.fx` shader and the extensionless scripts) and exempts only the generated `tools/fingerprint/tests/vectors.h`. The command is `tools/apply-license.py --write --spdx <id> [--except …]`; see [Decisions pending](#decisions-pending-not-failures). |
| 24 | hygiene | line endings / executable bits | `git grep -Il $'\r'`; compare shebang to `git ls-files -s` mode | **PASS** | No CRLF. Every shebang file is `100755` except `files/oxp3-audio-route`, `files/oxp3-tdp-sync`, `files/oxp3-xpad-sleep` (100644; the installers use `install -m 0755`, so installed behaviour is unaffected) and `main.py`/`package.py` (invoked via `python3`). |
| 25 | docs | relative Markdown links | local link parser over all tracked `.md` plus `docs/QA.md` | **PASS** | 87 relative links, 0 missing. |
| 26 | docs | repo paths cited in docs | token scan for `tools/…`, `files/…`, `docs/…`, `patches/…`; existence check | **PASS (fixed)** | 120 unique paths. One genuine broken in-repo reference: README cited `docs/BEYOND-PARITY.md`, which is not in this repo; corrected to “out-of-tree `BEYOND-PARITY` notes” (no fabricated content). Remaining hits are shorthand (`files/oxp3-sleep-*`), vendor payload paths (`tools/intel/...`) or an external project’s `docs/debugging.md`. |
| 27 | docs | recorded counts/hashes still true | compare docs to live runs | **PASS** | 85 tests, 324 C checks/10 vectors, 60 Decky checks, 33,843 B / `896ccee7…` all match `docs/decky-plugin.md`, `tools/fingerprint/README.md` and `docs/hw/fingerprint.md`; libfprint tree is `v1.94.9`. |
| 28 | docs | privilege claims vs tools | review of “no root/unprivileged” statements | **PASS** | No tool that needs root is described as unprivileged: `oxp3-voltap selftest` is shown with `sudo`; installer/Bluetooth/reload paths say sudo; `--status`, `--dry-run`, `--check`, `status` and the non-root selftests were exercised without root. |
| 29 | status | machine-readable status | `tools/oxp3-status.sh --json` | **PASS** | Exit 0, valid JSON, schema 1, `overall: warn`, counts `ok=23 warn=5 fail=0 unknown=0`. Saved redacted to `docs/state/oxp3-status-2026-11-09.json`. |
| 30 | status | synthetic self-test | `tools/oxp3-status.sh --selftest` | **PASS** | `selftest: 60 checks passed`. |
| 31 | status | strict mode | `tools/oxp3-status.sh --strict` | **PASS (expected exit 2)** | Exit 2 because the live system has 5 warn rows (XMX trace absent, scheduler not loaded, gaming-sched unit not installed, Flatpak Steam env). Correct `--strict` behaviour. |
| 32 | state | host snapshot without root | `bash state-snapshot.sh` | **PASS** | Exit 0, no stderr; 81 lines captured to `docs/state/snapshot-2026-11-09.txt` (kernel, RAPL, driver hashes, scheduler, packages, fix state, BT files, battery, suspend). |
| 33 | state | recorded working build | `tools/check-working.sh` | **PASS** | 7/7 `OK`, “state matches the recorded working build” (host/runtime driver prefixes, expat, PL1 35 W, patched `xe.ko`, Heroic GameMode off). |
| 34 | installer | read-only status | `tools/oxp3-hw-install.sh --status` | **PASS** | Exit 0; honest “nothing installed” matrix plus live PL1/PL2/PL4, battery, BT file count, IMU blacklist. |
| 35 | installer | full dry-run | `tools/oxp3-hw-install.sh --dry-run --all` | **PASS** | Exit 0; 29 `[dry-run]`/`[root-prefix]` lines, no write attempted (no root). |
| 36 | installer | simulated install/idempotency/uninstall | `mkdir -p /tmp/oxp3-sim && tools/oxp3-hw-install.sh --root /tmp/oxp3-sim --sleep --leds --rgb --imu --audio` (twice, then `--uninstall`) | **PASS** | 13 files created under the prefix; second run byte-identical file list; uninstall removed all units/rules/scripts/EQ and kept `~/.config/oxp3-rgb.toml` as designed. |
| 37 | tool | volume daemon self-test | `tools/oxp3-voltap/oxp3-voltap selftest` | **SKIP** | Exits 1 with `selftest needs root (/dev/uinput)`; the documented command is `sudo … selftest`. Not run privileged. |
| 38 | tool | RGB encoder self-test | `tools/oxp3-led.py selftest` | **PASS** | 9/9 encoder frames match captured frames (brightness/preset/solid/breath/off). |
| 39 | tool | HID encoder self-test | `tools/oxp3_hid.py` | **PASS** | 9/9 frames (the script has no CLI; it always runs the self-test). |
| 40 | tool | RGB keeper config + state | `tools/oxp3-rgbd.py --config tools/oxp3-rgbd.toml.example --check` | **PASS** | 5 zones, 10 frames, `brightness=high`; no config at `~/.config` is an environment state, not a code failure (`--check` exits 1 there). |
| 41 | tool | RGB keeper dry-run | `tools/oxp3-rgbd.py --config tools/oxp3-rgbd.toml.example --once --dry-run` | **PASS** | Prints 10 64-byte frames, writes nothing. |
| 42 | tool | IMU self-test | `tools/oxp3-imu.py selftest` | **PASS** | 16 `[OK]`, `RESULT: PASS (0 warning(s))`; BMI260 at ~99 Hz, HHD gyro path live. |
| 43 | tool | adaptive TDP one decision | `tools/oxp3-adaptivetdp.py --once --dry-run --json` | **PASS** | Exit 0, one JSON decision (`custom → quiet`, `applied=false`); the non-root `/var/log` warning is expected and non-fatal. |
| 44 | tool | power control read-only | `tools/oxp3-powerctl.py status --json`; `battery status`; `profile performance --dry-run`; `fan curve --once --dry-run` | **PASS** | Both RAPL zones and all limits read; battery limit 80, behaviour `auto`; dry-run prints both-zone write order; curve picks pwm 124 at 61 °C; no writes. |
| 45 | tool | SAGV exposure | `tools/oxp3-sagv.sh status` | **PASS** | Exit 0; reports display v30, `enable_sagv` 0400, no point interface, MCHBAR unreadable, RAPL domains. |
| 46 | tool | SAGV interface probe | `tools/oxp3-sagv.sh probe` | **PASS (expected exit 3)** | Exit 3 by design: “nothing writable”; the scan and root commands are the product. |
| 47 | tool | Bluetooth firmware diagnosis | `tools/oxp3-bt-firmware.sh --check` | **PASS (expected exit 1)** | Both files present and pinned-hash verified; exit 1 because *this boot* logged `Failed to load Intel firmware file … (-2)`, i.e. the running initramfs predates the files. `docs/INSTALL-HW.md` expected-output row corrected. |
| 48 | tool | Bluetooth dry-run | `tools/oxp3-bt-firmware.sh --dry-run` | **PASS** | Exit 0; exact privileged plan printed. It **downloads** the two upstream files to the cache to verify hashes (network), so it is not a fully offline check even though it makes no system write. |
| 49 | tool | suspend audit | `tools/sleep-power-measure.sh audit` | **PASS** | Exit 0; `[s2idle] deep`, no suspend attempts this boot (success 0), wake-source table shown. Root residency/blockers not available. |
| 50 | tool | speaker EQ model | `tools/audio/eq_response.py` | **PASS** | 8 bands, pre-gain −8.0 dB, max −1.6 dB, min −15.0 dB — matches `docs/INSTALL-HW.md`. |
| 51 | tool | display status | `tools/display/display-mode.sh status` | **PASS** | Reads eDP-1: 1920×1200@144, HDR off, WCG off, 10-bit, VRR never. Read-only. |
| 52 | tool | vibrance status | `tools/display/vibrance.sh status` | **PASS** | Shader present (4,758 B), gamescope shader installed, vkBasalt absent; no writes. |
| 53 | tool | fingerprint probe CLI | `tools/fingerprint/oxp3-fp-probe.py --help` | **PASS** | Usage renders; probe itself needs `spidev` + root and was not run (no hardware access). |

## Decisions pending (not failures)

1. **Licensing/provenance — DECISION PENDING, ready to apply.** There is no top-level
   `LICENSE`/`COPYING`, and 60 of 71 source-like files had no SPDX header. Choosing a licence is the
   owner's call and this repository does not assert one. The groundwork is done and validated on a
   copy (no real-tree write): [PROVENANCE.md](PROVENANCE.md) records the no-vendor-code guarantee,
   the per-category policy, the audit evidence and the contributor rules; `tools/apply-license.py`
   writes `LICENSE` plus one `SPDX-License-Identifier` line per source file, preserving shebangs and
   module docstrings. The fingerprint subtree keeps its existing `LGPL-2.1-or-later` via `--except`.
   Run one of:

   ```sh
   tools/apply-license.py --write --spdx MIT --holder "silentsudin" --except 'tools/fingerprint/**=LGPL-2.1-or-later'
   tools/apply-license.py --write --spdx LGPL-2.1-or-later --holder "silentsudin"
   ```

   then `--check` with the same arguments (idempotent), review `git diff`, and commit. Patches keep
   their upstream licences and are out of scope. Copy validation: MIT+exception wrote 70 headers and
   `LICENSE`, both `--check` runs exited 0 with an unchanged tree, byte-diff showed only the added
   SPDX lines, shebang/docstring positions were preserved, all 85 fingerprint tests passed and the
   fixture-drift check stayed clean. Until the owner decides, default copyright applies and reuse
   terms are ambiguous even though every file is our own work.
2. **Root-only checks intentionally not executed.** RAPL writes/readback, `oxp3-fan-curve` manual PWM,
   real suspend (`sleep-power-measure.sh suspend`), `oxp3-voltap selftest`, `spidev` bind + fingerprint
   probe, Bluetooth module reload and the real installer are all unverified here by the no-sudo rule.
   They remain the device-side acceptance step; the commands are in `docs/INSTALL-HW.md` and the
   per-topic documents.

## Fixed in this round (low risk, documentation/hygiene only)

* `.gitignore`: added generic `dist/`, `build/`, `node_modules/`, `*.zip`, `*.pyc`, `*.pyo`; the
  per-plugin `.gitignore` is kept.
* `README.md`: removed the broken in-repo `docs/BEYOND-PARITY.md` reference (that document is
  out-of-tree; no content was invented).
* `docs/INSTALL-HW.md`: corrected the `oxp3-bt-firmware.sh --check` expected exit code (1 is correct
  while the running boot has no firmware) and noted that `--root DIR` must already exist.

## Maintainability concerns

* **Two near-duplicate installers.** `oxp3-fixes.sh` (1,061 lines) and `oxp3-fixes-fast.sh` (1,126
  lines) share 32 function names; **26 of those bodies are byte-identical** (`build_kmods`,
  `build_firmware`, `build_card`, `build_xess`, `install_xess`, `fetch_anatase`, `do_mok_setup`,
  `hhd_volume_unpatch`, RAPL/Mesa helpers, …). The unified diff is still ~500 lines. Any fix to the
  shared build/install path has to be made twice; the two `install_extras()`/`do_uninstall()` bodies
  already differ (the slow script adds the sleep units). Extract the common half into `tools/lib/` or
  make the fast script a mode of the slow one before the next driver-stack change.
  *Done in the later dedupe round:* the 37 identical helpers now live in
  `tools/lib/install-common.sh` (the 32/26 count here omits eleven one-line functions; see the
  complete classification, the contract and the validation in
  [MAINTAINABILITY.md](MAINTAINABILITY.md)).
* **Docs duplicated three ways.** `README.md` (hardware table), `docs/hw/README.md` (state matrix +
  manual order) and `docs/INSTALL-HW.md` (install/verify/rollback) repeat the same component facts.
  Drift already exists in miniature (the BT `--check` row, the missing `BEYOND-PARITY` reference, the
  `--root` prerequisite); every component change needs three edits. A single table generated from one
  source, or one canonical doc with links, would age better.
* **Host-specific snapshot tooling.** `state-snapshot.sh` hardcodes `/var/home/holzr`,
  `/sys/class/thermal/thermal_zone5`, the Mesa build tree and the Flatpak runtime path. It works on
  this unit (81 lines, exit 0) but is not portable and its driver list churns on every Mesa build, so
  the saved snapshot is noisy evidence rather than a stable comparison. It also mixes root-only facts
  (S0ix residency) with unprivileged ones; the output says which.
* **Privilege descriptions are mostly accurate but easy to get wrong.** `--status`, `--dry-run`,
  `--check`, `status`, `selftest` (except voltap) and the measurement tools were all run unprivileged
  here. The traps are `tools/oxp3-voltap/oxp3-voltap selftest` (root: `/dev/uinput`),
  `tools/oxp3-led.py status` (hidraw is 0600 until the udev rule is installed) and
  `oxp3-rgbd.py --check` (needs a config). Keep the “Needs root?” column in `docs/hw/README.md` and
  add one for every new tool.
* **`--dry-run` is not always offline.** `oxp3-bt-firmware.sh --dry-run` downloads the upstream files
  to verify the pinned hashes. That is defensible (the hashes are the point) but should be stated;
  “dry-run” currently implies “no network” to most readers.
* **Fingerprint suite is strong but hardware-free.** 85 Python + 324 C checks and two compile modes
  are a good regression net; the remaining risk is entirely the never-run SPI path. Keep the synthetic
  vectors authoritative and do not let a hardware tweak bypass the fixture-drift test.
* **Decky build is in good shape.** Offline `npm ci`, typecheck, smoke, 60-check selftest and a
  byte-identical, documented artifact make this the most reproducible part of the repo.
* **Local build artefacts.** `node_modules/` (~50 MB), `dist/`, `build/` and
  `tools/fingerprint/build/test_proto` are present but ignored; no tracked binary. A `git clean -Xdn`
  review before a release is still worthwhile.

## How to re-run

### One command (preferred)

`tools/repo-test.sh` runs every offline check this repository has, with a
per-suite timeout, captured logs and a compact PASS/FAIL/SKIP table. It needs no
root, no network and (in `--fast`) no build:

```sh
tools/repo-test.sh --fast   # default: shell/Python syntax, fingerprint Python
                            # + fixture drift, Decky selftest/CLI, verify
                            # fixtures, installer status/dry-run, units/udev/hwdb
tools/repo-test.sh --full   # adds the fingerprint C/stub-syntax builds and the
                            # Decky build + package-determinism check
tools/repo-test.sh --list   # suite inventory, mode, tier, timeout
tools/repo-test.sh --json   # machine-readable result
tools/repo-test.sh --only 'fp-*'   # one suite family (repeatable)

make test                   # == tools/repo-test.sh --fast
make test-full              # == tools/repo-test.sh --full
```

Logs and `summary.txt` go to `${XDG_CACHE_HOME:-/tmp}/oxp3-repo-test/<utc-run>/`;
the failing command and its log path are printed when something fails. Missing
optional tools (a C compiler/container image, `shellcheck`, a warm npm cache,
network) are reported as `SKIP` with a reason, never as a pass. On this host the
fast run is **18 pass / 0 fail / 6 skip** (five full-only suites plus
`shellcheck`); `--full` is **22 pass / 0 fail / 2 skip**. See
[TESTING.md](TESTING.md) for the suite inventory, timing tiers and CI notes.
*Added after this snapshot:* the `unit` suite (`tests/unit/`, 318 stdlib
unittest cases) raises those rows to 25 suites — **19 pass / 0 fail / 6 skip**
in `--fast` and **23 pass / 0 fail / 2 skip** in `--full`; see
[UNIT-TESTS.md](UNIT-TESTS.md).

The pre-`repo-test.sh` per-suite commands are preserved verbatim in
[Appendix A](#appendix-a--per-suite-commands-pre-repo-test) for debugging a
single suite by hand.

## Evidence files

* [`docs/state/oxp3-status-2026-11-09.json`](state/oxp3-status-2026-11-09.json) — `--json` output,
  12,377 bytes, valid JSON. Host `user`/`home`/`hostname` and all `/var/home/holzr` paths were
  replaced with `<redacted-user>`/`<redacted-home>`/`<redacted-host>`; `overall=warn`,
  `counts.ok=23`, `warn=5`, `fail=0`.
* [`docs/state/snapshot-2026-11-09.txt`](state/snapshot-2026-11-09.txt) — verbatim
  `state-snapshot.sh` output (81 lines). It is host-specific by design and contains paths and local
  driver hashes, not secrets.

## Appendix A — per-suite commands (pre-repo-test)

The one-command runner wraps these; they are kept so a single suite can still be run by
hand. All from the repository root; none needs root. The privileged device-side steps are
in `docs/INSTALL-HW.md` and the per-topic documents and were deliberately not run.

```sh
# 1. syntax
for f in $(git ls-files); do
  first=$(head -1 "$f")
  case "$first" in
    '#!'*bash*) bash -n "$f" || echo "FAIL $f";;
    '#!'*'/sh'*) sh -n "$f" || echo "FAIL $f";;
  esac
done
PYTHONPYCACHEPREFIX=/tmp/qa-pyc sh -c \
  'git ls-files "*.py" | while read -r f; do python3 -m py_compile "$f" || echo "FAIL $f"; done;
   python3 -m py_compile tools/oxp3-voltap/oxp3-voltap'

# 2. fingerprint
python3 -m unittest discover -s tools/fingerprint/tests -v
tools/fingerprint/tests/run-c-tests.sh
python3 tools/fingerprint/tests/generate_fixtures.py
git diff --exit-code -- tools/fingerprint/tests     # drift check
tools/fingerprint/tests/check-libfprint.sh          # needs /tmp/libfprint-1.94.9 (v1.94.9)

# 3. Decky (offline; warm npm cache). `--no-install` reuses node_modules.
(cd tools/decky-oxp3-status && ./build.sh --offline)
(cd tools/decky-oxp3-status && sha256sum build/oxp3-status-1.0.0.zip)

# 4. systemd units — stub install-only ExecStart binaries, then verify
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for f in files/*.service tools/oxp3-voltap/*.service \
         tools/oxp3-homebtn.service tools/oxp3-rgbd.service; do
  b=$(basename "$f"); case "$b" in *@.service) b=${b%@.service}@test.service;; esac
  sed -E 's#^(ExecStart|ExecStop|ExecStartPre|ExecStartPost|ExecReload)=.*#\1=/bin/true#' \
    "$f" > "$tmp/$b"
done
for u in oxp3-adaptivetdp oxp3-audio-resume@test oxp3-fan-curve oxp3-gaming-sched \
         oxp3-power-profile oxp3-power-resume oxp3-rapl-pp1 oxp3-sleep-log oxp3-sleep-pre \
         oxp3-sleep-wakeup oxp3-tdp-sync oxp3-xpad-sleep oxp3-voltap rapl-power; do
  systemd-analyze verify "$tmp/$u.service" || echo "FAIL $u"
done
for u in oxp3-audio-route oxp3-homebtn oxp3-rgbd; do
  systemd-analyze --user verify "$tmp/$u.service" || echo "FAIL user:$u"
done

# 5. udev + hwdb
udevadm verify files/99-oxp3-sleep-wakeup.rules
udevadm verify tools/oxp3-hid.rules
for f in files/84-oxp3-volume-keys.hwdb tools/oxp3-voltap/84-oxp3-volume-keys-taps.hwdb; do
  r=$(mktemp -d); mkdir -p "$r/etc/udev/hwdb.d"; cp "$f" "$r/etc/udev/hwdb.d/"
  systemd-hwdb --root="$r" --strict update && test -s "$r/etc/udev/hwdb.bin" \
    || echo "FAIL $f"; rm -rf "$r"
done

# 6. status + evidence snapshot (redact host.user/home/hostname after capture)
tools/oxp3-status.sh --json > docs/state/oxp3-status-$(date +%F).json
tools/oxp3-status.sh --selftest
bash state-snapshot.sh > docs/state/snapshot-$(date +%F).txt

# 7. installer dry-run + prefix simulation (no root, no host files)
tools/oxp3-hw-install.sh --dry-run --all
rm -rf /tmp/oxp3-sim && mkdir -p /tmp/oxp3-sim
tools/oxp3-hw-install.sh --root /tmp/oxp3-sim --sleep --leds --rgb --imu --audio
tools/oxp3-hw-install.sh --root /tmp/oxp3-sim --sleep --leds --rgb --imu --audio   # idempotent
tools/oxp3-hw-install.sh --root /tmp/oxp3-sim --uninstall --sleep --leds --rgb --imu --audio

# 8. hygiene
git ls-files -z | xargs -0 file --mime | grep -v 'text/'          # expect only text/mbox patches
git ls-files | grep -Ei 'node_modules|/dist/|/build/|__pycache__|\.(pyc|zip|o|so|dll|exe|sys|bin|sfi|ddc)$'
git grep -nEI 'AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|(password|secret|api[_-]?key)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{6,}'
```

The host has no `shellcheck`; install it (`rpm-ostree install ShellCheck`) to add
`shellcheck -x` over every `.sh` before release. The C suites need either host `gcc`/`make` or
podman with `localhost/oxp3-fixes-build:f45`; `check-libfprint.sh` additionally needs a
`v1.94.9` source tree at `/tmp/libfprint-1.94.9` (it falls back to podman, which installs GLib/GUsb
development packages into the ephemeral container).

