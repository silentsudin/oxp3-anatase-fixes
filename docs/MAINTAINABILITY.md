# Installer maintainability: one shared helper library

Date: **2026-11-09** (dedupe round; unprivileged, no host change).
Scope: `oxp3-fixes.sh`, `oxp3-fixes-fast.sh`, new `tools/lib/install-common.sh`.
This document is the contract for the shared library and the record of what was extracted, what
was deliberately left alone, and how the two installers are validated after a change.

## 1. Function classification

Measured at the previous revision (`0374df9`) with a brace-balanced extraction of every top-level
function (not a line regex):

| set | count |
|---|---:|
| functions in `oxp3-fixes.sh` | 44 |
| functions in `oxp3-fixes-fast.sh` | 49 |
| names defined in both | 43 |
| …byte-identical bodies (**moved to the library**) | 37 |
| …identical after whitespace/comment normalisation | 0 |
| …genuinely different (**kept local**) | 6 |
| defined only in the slow script | `mesa_rpms` |
| defined only in the fast script | `_mpick`, `install_mesa_fast`, `remove_mesa_fast`, `install_sched`, `refresh`, `verify` |

`docs/QA.md` recorded "32 shared functions, 26 byte-identical". That was the same set counted
without the eleven one-line helpers (`say`, `die`, `anatase_kver`, `ctr`, `ctr_root`,
`xmx_override_file`, `ucode_rev`, `sys_fw`, `hhd_site`, `host_mesa`, `hotfix_active`): 32 + 11 = 43
and 26 + 11 = 37. Both numbers describe the same code; this table is the complete one.

### Moved (37, now defined once in `tools/lib/install-common.sh`)

`say`, `die`, `anatase_kver`, `ctr`, `ctr_root`, `ensure_container`, `fetch_anatase`,
`build_card`, `build_xess`, `seed_xess_compiler`, `install_xess`, `uninstall_xess`, `status_xess`,
`xmx_override_file`, `apply_kernel_patches`, `build_kmods`, `ucode_rev`, `sys_fw`, `sys_fw_sha`,
`build_firmware`, `do_build`, `mok_cn`, `mok_usage_ok`, `mok_trusted`, `do_mok_setup`,
`install_rapl_power`, `remove_rapl_udev_rules`, `hhd_site`, `install_volume_fix`,
`hhd_volume_unpatch`, `host_mesa`, `flatpak_mesa`, `refresh_flatpak_runtime`,
`remove_flatpak_mesa`, `deploy_info`, `hotfix_active`, `do_status`.

The extracted bodies are byte-identical to the two originals (verified programmatically against
`git show HEAD:oxp3-fixes.sh` and `git show HEAD:oxp3-fixes-fast.sh`: 37/37), so this round cannot change their behaviour.
Their comments moved with them and each group has a section banner in the library.

## 2. What stayed local, and why

Each is marked in the source with a `# Kept local: …` / `# Local to this variant: …` comment.

| function | slow / fast lines | why it cannot be shared |
|---|---:|---|
| `build_mesa` | 109 / 46 | the point of the two scripts: RPM packaging from Anatase's spec vs a podman/meson build of the pinned tree; different inputs, outputs and failure modes |
| `install_flatpak_mesa` | 14 / 35 | slow copies the distro Mesa file list into the runtime overrides; fast installs its own built driver + expat and keeps a stock backup |
| `do_install` | 105 / 119 | the flow: slow layers Mesa RPMs; fast installs its podman build, the sched_ext unit and the Flatpak runtime, with `--skip-flatpak`/`--skip-sched` |
| `do_uninstall` | 28 / 32 | mirrors each variant's install set (slow removes layered RPMs; fast also restores the stock driver and removes `oxp3-gaming-sched`) |
| `install_extras` | 36 / 27 | slow also installs the sleep units; fast leaves them to `tools/oxp3-hw-install.sh` |
| `uninstall_extras` | 19 / 15 | same split |
| `mesa_rpms` (slow only) | 8 | maps installed Mesa packages to the RPMs built here |
| `_mpick`, `install_mesa_fast`, `remove_mesa_fast` (fast only) | 7 / 27 / 11 | locate, install and restore the podman Mesa build + backup manifest |
| `install_sched` (fast only) | 15 | sched_ext/`scx_cosmos` step, only in the fast variant |
| `refresh`, `verify` (fast only) | 12 / 25 | rebuild/verify the Flatpak runtime image for the podman driver |

`do_build` is shared because the build *order* is identical; its one variant step (`build_mesa`) is
the library's only caller hook.

## 3. The library contract (`tools/lib/install-common.sh`, interface version 1)

The full text is at the top of the library; the enforceable part is in `install_common_guard()`.

**Caller obligations before the first library call**

1. Bash, with `set -euo pipefail` already active (the library changes no shell option).
2. Set `OXP3_INSTALL_COMMON_API=1` before sourcing (the version guard).
3. Set the configuration variables the guard lists:
   `here CACHE OUT MOK_DIR ANATASE_REPO PATCHWORK_REPO FEDORA IMAGE CTR KMOD_NAME STAMP`,
   `SKIP_KERNEL SKIP_GAMESCOPE SKIP_MANGOHUD SKIP_FIRMWARE SKIP_DMC SKIP_MESA SKIP_XESS`,
   `XESS_COMMIT XESS_SRC_URL XESS_ZIP_URL XESS_ZIP_SHA XESS_DLLS XESS_PKG`,
   `KMODS FIRMWARE FW_NAME HHD_PATCHES VOLUME_HWDB`,
   `FLATPAK_RT FLATPAK_MESA_LIST FLATPAK_RT_FILES EXTRA_UNITS`.
   Values are read when a function runs, so the definitions may appear after the `source` line;
   `KMODS`, `FIRMWARE`, `HHD_PATCHES` and `EXTRA_UNITS` must be non-empty arrays.
4. Define `build_mesa()` - the only caller hook.
5. Call `install_common_guard` once, after its configuration and local functions and immediately
   before the command dispatch (both installers do this).

The guard refuses (exit 1, `error:` on stderr) on an API mismatch, a missing variable, an empty
array or a missing `build_mesa()`. Sourcing the library alone is safe: it defines functions and
nothing else - no variables, options, traps, umask, output or file access. That property is what
allows the caller to keep its own configuration in the script instead of in the library.

**Side effects of the functions**

| group | effect |
|---|---|
| `build_*`, `fetch_*`, `seed_*`, `ctr*`, `ensure_container`, `apply_kernel_patches` | network downloads into `$CACHE` (hash-verified), build trees in `$CACHE/rpmbuild`, packages in `$OUT`, podman image/container named by the caller; no root |
| `install_*`, `do_mok_setup`, `do_install`, `uninstall_*` | root writes through `sudo` to `/etc`, `/usr`, `/usr/local`, `/run`, plus `rpm-ostree` layering / `usroverlay` |
| `status_*`, `do_status`, `deploy_info`, `hotfix_active`, `mok_*` (queries) | read-only: `rpm`, `rpm-ostree --booted`, `systemctl is-*`, `modinfo`, `journalctl`, `/sys`, `/run` |

**Idempotency.** Every install path checks the installed state first, so re-running an install
changes nothing it already applied (the QA `--root` cycle depends on this). Builds overwrite their
own staging directories; `do_mok_setup` keeps a key the machine keyring accepts; uninstall paths
tolerate missing files and units.

**Locking.** None, as before the extraction. Concurrent runs would share `$CACHE`/`$OUT` and the
caller's podman container names, so run one build/install at a time. A caller that needs a lock
takes it itself; no function here creates, waits for or removes one.

**Logging.** `say()` prints `"\n== <message>"` to stdout as a phase heading; `die()` prints
`"error: <message>"` to stderr and exits 1; helpers print indented detail to stdout and warnings to
stderr. Nothing is written to a file or syslog from the library.

## 4. Before / after size

| metric | before | after |
|---|---:|---:|
| `oxp3-fixes.sh` lines | 1,061 | 520 |
| `oxp3-fixes-fast.sh` lines | 1,126 | 590 |
| `tools/lib/install-common.sh` lines | - | 695 |
| total lines for the installer code | 2,187 | 1,805 (-382, -17 %) |
| duplicated shared-function text | 37 functions × 548 lines per copy = 1,096 | 548 (one copy) + guard/banners |
| unified diff between the two installers | 500 lines (145 removed / 205 added) | 552 lines (157 / 222) |
| byte-identical function bodies present in both installers | 37 | 0 |

The unified diff between the two scripts did **not** shrink: a diff only ever showed their
differences, and those were already the six genuinely different functions. What changed is the
input to that diff - the 37 shared bodies no longer appear in either file, so the remaining diff is
now exactly the real divergence plus the comments that explain it (the added comments differ per
variant, which is why the line count grew slightly). The duplication metric that matters is on the
"duplicated shared-function text" row and in the verification below.

## 5. How to add a new component

1. Decide whether the component behaves the same in both variants.
   * **Same** - add it to `tools/lib/install-common.sh`. It may only read configuration through
     caller variables; if it needs a new variable, add it to `install_common_guard()` and to the
     caller contract in the header. Keep the body free of source-time actions.
   * **Different** - add it to the variant(s) that need it and write a one-line `# Kept local:`
     comment saying what differs. Never paste the same body into both installers.
2. Call it from the shared `do_build` (build steps) or from each variant's `do_install` /
   `do_uninstall` (install steps) in the correct order; install steps that stage a reboot go before
   the `/usr` overlay steps. Add the corresponding uninstall path in both variants if it is shared.
3. Add its configuration (pins, paths, unit names) in the section of the script that owns it. The
   library never defines configuration; if a shared function consumes a table, the caller must set
   it and the guard must list it.
4. New option/flag: parse it in **both** installers, mention it in the `--help` comment block, and
   thread it through as a variable (add it to the guard if a shared function reads it).
5. New subcommand: add it to both `case $cmd` dispatchers (a variant-only command, like
   `verify`/`refresh` in the fast script, stays in that dispatcher only).
6. Validate (all unprivileged): `bash -n` on the three files; compare no-argument usage,
   `status`, an unknown option and `build --dry-run` with the previous revision (byte-identical
   expected); `tools/oxp3-hw-install.sh --dry-run --all` and `--status`; a `--root` install twice
   then uninstall (same file list, 13 -> 13 -> 2 files for the sleep/leds/rgb/imu/audio set);
   `systemd-analyze verify` on every unit copied in with its `ExecStart` stubbed.
7. Update `docs/hw/components.yaml` (state, summary, implementation, `verify` checks, caveats) and
   run `python3 tools/gen-docs.py --write`; it regenerates the state matrix in
   `docs/hw/README.md`, the install/verify matrix in `docs/INSTALL-HW.md` and the summary in
   `README.md`. Commit the data change with the regenerated blocks - `tools/repo-test.sh` fails on a
   stale block. The uninstall/rollback prose in `docs/INSTALL-HW.md` sections 6-7 is still
   hand-written. Schema and rules: [hw/REGISTRY.md](hw/REGISTRY.md).

## 6. Duplication that remains (intentional or out of scope)

* **The six genuinely different functions** (section 2). They are the reason two installers exist;
  sharing them would need a mode switch inside every one and would hide the actual difference.
* **The variant-only helpers** (`mesa_rpms`, `_mpick`, `install_mesa_fast`, `remove_mesa_fast`,
  `install_sched`, `refresh`, `verify`). They are small and belong to one pipeline each.
* **Configuration tables in both scripts** (`MESA_*`, `XESS_*`, `KMODS`, `FIRMWARE`, `FLATPAK_*`,
  `HHD_PATCHES`, `EXTRA_UNITS`). This is the remaining ~110 lines of duplication. It is deliberate
  for now: the library is functions-only (no source-time state) so each caller stays the single
  place where its configuration is visible and overridable. If these drift, the next step is an
  `install_common_config` call (or a sourced data file) rather than copying the library header -
  note that this would change the "no source-time side effects" property and needs a decision.
* **`tools/oxp3-hw-install.sh`** keeps its own `say`/`warn`/`die`/`run`/prefix helpers. It
  orchestrates the component installers and must keep working as a standalone entry point (and
  with `--root`), so it is not a consumer of this library.
* **The three install documents** (`README.md`, `docs/hw/README.md`, `docs/INSTALL-HW.md`) no
  longer repeat the per-component tables: those are generated from `docs/hw/components.yaml` by
  `tools/gen-docs.py` (schema and rules in [`hw/REGISTRY.md`](hw/REGISTRY.md)). A component fact is
  one edit now. What remains hand-written is the install/uninstall/rollback prose in
  `docs/INSTALL-HW.md` sections 2, 3, 6 and 7 and the manual-order list in `docs/hw/README.md`;
  those still need a look when an installer flag changes.

## 7. Validation performed in this round

All commands from the repository root, unprivileged (`sudo` absent from every executed command):

| check | result |
|---|---|
| `bash -n` on the three changed files, plus every shebang-detected script in `git ls-files` | pass (0 failures) |
| `./oxp3-fixes.sh`, `./oxp3-fixes-fast.sh` (no args, usage) | exit 2, output and stderr byte-identical to the previous revision |
| `./oxp3-fixes.sh status`, `./oxp3-fixes-fast.sh status` | exit 0, structurally identical (72 / 69 lines); captured back to back they are byte-identical, only the live `rapl-power` wattages drift between runs |
| unknown option and `build --dry-run` (both scripts) | exit 2, identical |
| `tools/oxp3-hw-install.sh --dry-run --all` / `--status` | exit 0, byte-identical (50 / 12 lines) |
| `--root` cycle (`--sleep --leds --rgb --imu --audio`): install, re-install, uninstall | exit 0/0/0; 13 -> 13 -> 2 files, identical file lists; install output identical after normalising the `mktemp` names the orchestrator prints |
| `systemd-analyze verify` (units the installers reference, `ExecStart` stubbed) | 11/11 pass |
| library guard: pass, API mismatch, missing variable, empty array, missing `build_mesa()` | 6/6 as designed (refusals exit 1 with a contract message) |
| library source safety (`set -euo pipefail; source …` with no caller state) | pass, defines `install_common_guard` and returns 0 |
| duplicate bodies | 37/37 moved bodies byte-identical to HEAD; 0 still defined in either installer; 0 identical bodies shared by the two installers; lib defines 38 functions (37 + guard) |

Not run (root-only, per the no-sudo rule): a real `install`/`uninstall`, `mok-setup`, and the
rpm-ostree/`/usr`-overlay steps. They are exercised on the device; the code paths are unchanged
except for where the bodies are defined.

## 8. Open points

* `shellcheck` is not installed on the host (`docs/QA.md`), so only `bash -n` was possible. The
  library is written to pass `shellcheck -x` when it becomes available.
* Neither installer has `--dry-run` or `--root`; only the orchestrator does. Adding them would be a
  behaviour change and is out of scope for this round.
* The configuration-table duplication (section 6) is the remaining known drift risk.
