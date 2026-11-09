# Decky plugin: live OXP3 fix status

`tools/decky-oxp3-status/` is a Decky Loader plugin that shows, in the Steam
Quick Access Menu, whether the OXP3 "Anatase" fixes are actually in effect for
the game that is running **right now**. `tools/oxp3-status.sh` is a thin
wrapper that runs the same Python backend headlessly, so the panel and the CLI
cannot drift apart.

Design rules:

- **read-only by default** — status collection writes nothing, starts/stops
  nothing and reconfigures nothing. The one action, *Run verification*, is
  opt-in, runs `tools/oxp3-verify.sh` as the desktop user, and keeps its
  state-changing `full`/`changes` modes behind a confirmation step;
- **no network** — no HTTP, no store lookups, no update checks;
- **defensive** — every probe may fail; a failed probe becomes `unknown`/`warn`
  with an explanation, never an exception in the panel;
- **one implementation** — `main.py` is the backend for both consumers;
  `src/index.tsx` only renders the returned report.

## Data contract

`Plugin.get_status()` (and `python3 main.py --json`) return one object:

```json
{
  "schema": 1,
  "version": "1.0.0",
  "generated_at": "2026-11-09T00:11:59Z",
  "overall": "warn",
  "counts": {"ok": 23, "warn": 5, "fail": 0, "unknown": 0, "na": 0},
  "host": {"user": "holzr", "home": "/var/home/holzr", "kernel": "7.2.7-an28.fc45.x86_64", "hostname": "..."},
  "game": {
    "detected": true, "name": "Cyberpunk 2077", "pid": 12345,
    "exe": "...", "appid": "1423049311", "prefix": "...",
    "env": {"...": "..."}, "env_readable": true, "cmdline": "..."
  },
  "env_sources": [{"name": "Heroic config 1423049311", "live": false, "keys": 9}],
  "groups": [
    {"id": "xmx", "title": "XeSS XMX", "state": "warn", "indicators": [
      {"id": "xmx.driver", "label": "XMX driver build", "state": "ok",
       "value": "4b1d55ea9483417f (match)", "detail": "path: ...", "source": "..."}
    ]}
  ],
  "notes": ["no live game process visible; ..."]
}
```

`state` is one of `ok`, `warn`, `fail`, `unknown`, `na`. `na` means "this check
does not apply to the current title" (for example the XMX environment rows while
a non-XeSS game is running). A group's state is the worst of its indicators
(`na` ranks with `ok`). `overall` is the worst group state with
`fail` > `warn` > `unknown` > `ok`.

Decky methods exposed by `Plugin`:

| method | returns |
|---|---|
| `get_status()` | the report above (runs off the loader event loop) |
| `get_info()` | version, resolved home, recorded expected hashes/PL1 |
| `run_verification(mode, confirm)` | compact result of one `tools/oxp3-verify.sh` run (see below) |
| `get_verification()` | the last compact result, so the panel keeps it across a remount |

The frontend calls `call("get_status")` every 5 s while the panel is mounted and
stops on unmount; there is no background/global polling.

## Run verification from the panel

The QAM panel has a **Verification** row with *Run quick verification* and —
behind *More verification modes…* — *full* and *full + changes*. It calls
`run_verification(mode, confirm)`, which runs `tools/oxp3-verify.sh` and returns
a compact result: `ok`, `mode`, `overall` (`pass`/`fail`/`skip`/`error`), the
`counts`, the process `exit_code`, up to eight failing and eight skipped check
ids with their summaries and remediation hints, `duration_s` and
`output_truncated`. `get_verification()` returns the same object for the last
run, and the panel refreshes `get_status` once the run finishes.

| mode | command | hard timeout |
|---|---|---|
| `quick` | `tools/oxp3-verify.sh --json` | 180 s |
| `full` | `tools/oxp3-verify.sh --full --json` | 900 s |
| `changes` | `tools/oxp3-verify.sh --full --with-changes --json` | 1200 s |

Safety model:

- **desktop user, not root.** The loader backend normally runs as root; this
  action drops privileges to `DECKY_USER`, `SUDO_USER` or the owner of the
  resolved home (`user=`/`group=`), and refuses with `no-deck-user` when no
  unprivileged identity can be resolved. Root-only checks therefore report
  SKIP with their normal hints instead of being run.
- **no shell.** The harness is executed from an argv list with a minimal
  environment (`PATH`, `HOME`, `USER`, `LOGNAME`, `LC_ALL`, `XDG_RUNTIME_DIR`)
  and the checkout as `cwd`; nothing is interpolated into a shell.
- **bounded.** One run at a time (a concurrent call returns `busy`), a hard
  per-mode timeout, stdout capped at 1 MiB / 20 000 lines and stderr at 8 KiB.
  Excess bytes are read and discarded, so a hung or chatty tool cannot block on
  a full pipe or grow the loader's memory.
- **never raises.** Failures come back as `{ok: false, error: {code,
  message}}` with a code: `busy`, `timeout`, `bad-json`, `missing-verifier`,
  `no-deck-user`, `escalation-required`, `verifier-error`, `spawn-failed`,
  `bad-mode` or `internal`. The panel toasts the message and keeps rendering.
- **escalation gate.** `full` and `changes` are refused unless the UI sent
  `confirm=True` after its second click or the operator set
  `OXP3_STATUS_ALLOW_ESCALATION=1`. `changes` also implies `--full`.
- **checkout discovery.** The backend uses the checkout containing `main.py`,
  then `OXP3_STATUS_REPO`, then `~/git/linux_scratch/oxp3-anatase-fixes`,
  `~/oxp3-anatase-fixes` and `~/.local/share/oxp3-anatase-fixes`. An installed
  plugin copy without the repository returns `missing-verifier` with a hint.

What it does **not** do: it never reboots or suspends the machine itself (the
real-suspend step needs root and stays SKIP; the battery measurement is
read-only and may run), never installs
packages, firmware or services, never writes system configuration, never asks
for sudo and never runs `--with-changes` without the confirmation step. The one
persistent effect it can have — `changes` re-applying the speaker EQ drop-in —
is the documented purpose of that mode, and `tools/audio/eq-off.sh` removes it.
For the root-only checks use a terminal:
`sudo tools/oxp3-verify.sh --full`.

## Indicators

Recorded expectations come from `docs/WORKING-BUILD-2026-11-08.md` and
`docs/xess-xmx.md`.

### Mesa driver

| id | probe | ok when |
|---|---|---|
| `mesa.host` | sha256 of `/usr/lib64/libvulkan_intel.so` | prefix `ec418f04e589965a` (our podman build) |
| `mesa.env` / `mesa.env.vk_driver_files` | `VK_DRIVER_FILES` / `VK_ICD_FILENAMES` from the effective env | unset (system default), or resolves to the host or XMX driver |

`fail` when the file is missing/unreadable; `warn` when a hash differs from the
recorded build (stock or third-party driver).

### XeSS XMX

Cyberpunk 2077 is the recorded XeSS reference title (Heroic appid `1423049311`,
or a detected name containing "cyberpunk"). While any **other** game is running,
the XMX environment rows are `na` rather than `warn`: that stack is intentionally
not armed for other titles, and reporting a failure there would be a false
alarm. The driver/kernel rows stay meaningful because they describe the machine.
With no game running, the configured machine stack is reported as before.

| id | probe | ok when |
|---|---|---|
| `xmx.icd` | `VK_DRIVER_FILES` / `VK_ICD_FILENAMES` | value mentions `icd-xmx.json` or resolves to the XMX driver |
| `xmx.helper` | `ANV_CM_HELPER` | value contains `xmx-cm-compile-nodgrf` **and** the file exists (128-GRF kernels) |
| `xmx.igc` | `XMX_IGC_DIR` | directory exists and contains `neo/bin/ocloc*` (IGC 2.10.10) |
| `xmx.shim` | `INTC_ALT_DRIVER_EXTENSIONS_PATH` | `igdext64.dll` present |
| `xmx.vkd3d` | `VKD3D_DISABLE_EXTENSIONS` | contains `VK_EXT_descriptor_buffer` |
| `xmx.driver` | `~/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so` | prefix `4b1d55ea9483417f` |
| `xmx.kernels` | `~/.cache/xess-xmx/kernels/*.cmk` + `~/.cache/xess-xmx/compile.log` | at least one kernel, no `FAILED` lines |
| `xmx.trace` | `igdext_trace.log` under the known prefixes | present (freshness only downgrades to `warn`) |
| `xmx.benchmark` | newest Cyberpunk `benchmarkResults/*/summary.json` | XeSS on in the summary |

`detail`/`value` carry the actionable numbers: helper basename, ocloc path,
kernel count and newest age, compile failures, `upscalingType`, average FPS,
resolution and preset.

### CPU scheduler

| id | probe | ok when |
|---|---|---|
| `sched.state` | `/sys/kernel/sched_ext/state` | `enabled` |
| `sched.scx_cosmos` | `comm`/`cmdline` of processes | a `scx_cosmos` process exists |
| `sched.unit` | `systemctl show oxp3-gaming-sched.service` | unit active; "not installed" is reported, not crashed |

### Other fixes

`fixes` group: patched `xe.ko` (`modinfo -F filename xe` contains `/updates/`),
`rapl-power.service` active, `oxp3-rapl-pp1.service` state and `uncore` zone,
PL1 from `intel-rapl:0` and `intel-rapl-mmio:0` (expected 35 W), ACPI
`platform_profile`, Heroic `useGameMode`/`gamemoderun` for every configured
game, the Flatpak runtime override driver (prefix `4d043cc2851a11fd`) and
absence of `libexpat.so.1`, plus the Steam Flatpak XMX env (`ANV_CM_HELPER`).

### System

Kernel and OS release, one governor/EPP line per cpufreq policy (recorded good
config: `powersave` + `balance_power`), GPU PCI id/driver and Mesa version
(cheap scan for the version string, no driver load), CPU/GPU temperatures from
hwmon/thermal and battery capacity/status/power.

## Environment resolution

The plugin backend runs under `plugin_loader.service`, normally as root with
`HOME=/root`, while the games and configuration belong to the desktop user. The
backend therefore resolves the desktop home in this order: `OXP3_STATUS_HOME`,
`OXP3_HOME`, `DECKY_USER_HOME`, `$HOME`, `SUDO_USER`/`DECKY_USER` passwd homes,
then a scan of `/var/home/*` and `/home/*`; it scores candidates by the presence
of `~/.config/xess-xmx`, `~/.cache/oxp3-fixes`, Heroic configs and
`homebrew/plugins`. `--home PATH` overrides everything.

The effective launch environment is layered, first hit wins, live process env
first:

1. the running game process (`/proc/<pid>/environ`, if readable);
2. the Heroic per-game config (`envVars`, `enviromentOptions`, wrappers), matched
   by appid or by the prefix directory name (exact component, never a raw
   substring, so a title called "game" cannot match `.../Games/...`);
3. the Steam Flatpak override file's `[Environment]` section;
4. `~/.config/xess-xmx/oxp3-env.sh`.

Absolute or `~` paths are expanded against the resolved home. An empty
variable (`ANV_CM_HELPER=`) is treated as unset so an inherited blank value from
a process environment cannot shadow the configured launcher value. Nothing is
sourced or executed — the files are parsed as data.

## Game detection

`/proc` is scanned for the desktop user's processes; a `.exe` argument naming
`cyberpunk2077` scores highest, other paths containing `/steamapps/common/` or
`/games/` next, then wine/proton/wineserver process names. The strongest match
wins (newest pid breaks ties); its `SteamAppId`/`SteamGameId` and
`STEAM_COMPAT_DATA_PATH` become the report's `appid`/`prefix`, and its environ
the live env layer. When nothing is found the report says so and falls back to
the configured launcher environment (`live: false`).

Prefixes searched for benchmarks and the shim trace: the live game's prefix,
Heroic `winePrefix` entries, `~/Games/Heroic/Prefixes/*`, and
`~/.local/share/Steam/steamapps/compatdata/*/pfx`.

## Build and install

The build was verified on the target host. Toolchain and package versions
actually used:

| component | version |
|---|---|
| node / npm | v22.23.1 / 10.9.8 (npm registry reachable: HTTP 200) |
| @decky/rollup | 1.0.2 |
| rollup / typescript | 4.68.0 / 5.9.3 |
| @decky/api | 1.1.3 (bundled into `dist/index.js`, as the template does) |
| @decky/ui | 4.16.5 (types only; the loader supplies the runtime `DFL`) |
| react / @types/react | 18.3.1 (dev only, for the headless smoke test) / 18.3.3 |

One command builds, verifies and packages:

```sh
cd tools/decky-oxp3-status
./build.sh                 # npm ci + typecheck + rollup + smoke + selftest + zip
./build.sh --offline       # same with `npm ci --offline` (warm npm cache)
./build.sh --no-install    # reuse node_modules
```

`build.sh` writes `build/oxp3-status-<version>.zip` and prints its size and
sha256. The current artifact is **43,395 bytes**, sha256
`be58c260b52986360df19bcd87cddb309357e9d0111ccc68133923044cdbd539`
(deterministic: fixed zip timestamps, sorted entries; rebuilding from the same
`src/` reproduces the same hash). `tools/decky-oxp3-status/artifact.sha256`
records that digest for the installer's reuse path. The individual steps are also available as
`npm run typecheck`, `npm run build`, `npm run smoke`, `npm run selftest` and
`npm run package`.

The zip contains exactly the plugin root, which is the layout Decky Loader's
*Install Plugin from File* expects:

| entry | purpose |
|---|---|
| `plugin.json` | manifest, `api_version: 1`, `version: 1.0.0` |
| `main.py` | backend; the loader imports `Plugin` from it |
| `dist/index.js` | rollup bundle loaded by the frontend (ESM, loader globals) |
| `dist/index.js.map` | source map for debugging the panel |
| `README.md` | operator notes |

### Install through the hardware installer (supported path)

`--decky` is a first-class component of `tools/oxp3-hw-install.sh` (included in
`--all`) and is the only supported install path for a managed copy. The Decky
plugins directory is root-owned because `plugin_loader.service` runs as root,
so a plain user-level copy fails with `EACCES`; the installer writes it as root
and leaves no root-owned file in the repo.

```sh
# build (node+npm) or reuse + verify the zip, back up the previous copy, install
sudo tools/oxp3-hw-install.sh --decky

# same, and restart plugin_loader.service now (only when the unit exists)
sudo tools/oxp3-hw-install.sh --decky --restart-plugin-loader

# no node/npm: install an existing zip, but only if it matches artifact.sha256
sudo tools/oxp3-hw-install.sh --decky \
  --decky-artifact tools/decky-oxp3-status/build/oxp3-status-1.0.0.zip
```

Behaviour:

* **directory** — default `~/homebrew/plugins/oxp3-status`, resolved against the
  invoking user's home (`/var/home/holzr/homebrew/plugins/oxp3-status` on the
  validated device). `OXP3_DECKY_PLUGINS_DIR` overrides it (absolute, `~/...`,
  `$HOME/...` or a path relative to the home); `--root DIR`/`OXP3_ROOT` places
  it inside a simulation prefix.
* **artifact** — if `node` and `npm` exist, `tools/decky-oxp3-status/build.sh
  --offline` runs and its deterministic zip is installed (a digest difference
  from the record is reported, because a changed source legitimately changes
  it). Otherwise the newest `build/oxp3-status-*.zip` is used and **must** match
  the sha256 in `tools/decky-oxp3-status/artifact.sha256`; a missing artifact or
  a bad digest aborts before anything is written.
* **validation** — the zip layout is exactly the plugin root, and the extracted
  directory is checked for `plugin.json` (`name = "OXP3 Status"`,
  `author = "silentsudin"`, `api_version = 1`, `version = x.y.z`), `main.py` and
  `dist/index.js` (non-trivial and containing `definePlugin`) before success is
  declared.
* **backup** — an existing `oxp3-status` directory is copied to
  `~/.local/state/oxp3-fixes/decky-backups/<UTC stamp>/oxp3-status` first; the
  install then replaces it atomically. A directory carrying our
  `.oxp3-managed` marker is ours; an unmarked hand copy of this plugin is
  adopted (backed up first). A directory that is not this plugin is refused,
  never overwritten.
* **restart** — never automatic. Without `--restart-plugin-loader` the exact
  command is printed; with it the unit is restarted only when
  `plugin_loader.service` exists (and only as root). Decky's
  *Settings → Developer → Reload plugins* re-reads the directory, but a changed
  `main.py` still needs the service restart.

An unmarked copy can also be installed from Decky: *Settings → General →
Install Plugin from File* → `build/oxp3-status-1.0.0.zip`. That copy has no
ownership marker, so the installer treats it as an adoptable hand copy (backed
up, then replaced); `--uninstall --decky` deliberately refuses to delete it.

### Verify and roll back

```sh
# installed? version, digest, marker and whether plugin_loader logged it
tools/oxp3-hw-install.sh --status        # decky: installed=yes state=ok ... loader-log=loaded|not-logged|unreadable
tools/oxp3-verify.sh                     # decky.dir, decky.backend, decky.loaded, decky.status
sudo journalctl -u plugin_loader.service -b --no-pager | grep -i oxp3

# remove exactly our directory (the marker is the ownership proof); backups stay
sudo tools/oxp3-hw-install.sh --uninstall --decky
# Decky -> Settings -> Developer -> Reload plugins, or:
sudo systemctl restart plugin_loader.service
```

To roll back to the copy a reinstall replaced, use the newest backup (the
directory name is the UTC stamp printed as `backup:` by the install):

```sh
backup=$(ls -1dt ~/.local/state/oxp3-fixes/decky-backups/*/oxp3-status | head -n 1)
sudo rm -rf ~/homebrew/plugins/oxp3-status
sudo cp -a "$backup" ~/homebrew/plugins/oxp3-status
sudo systemctl restart plugin_loader.service
```

The offline `--root` simulation (`tests/integration/decky/run.sh`, suite
`decky-install`) covers install, the backup on reinstall, uninstall, a missing
artifact, a tampered digest and a foreign directory with the same name.

`@decky/rollup` rewrites the `@decky/ui` imports of `src/index.tsx` to the
loader-provided `DFL`/`SP_REACT`/`SP_JSX` globals (the same shape every
installed plugin ships) and inlines `@decky/api`; the panel uses
`definePlugin`, `PanelSection`, `PanelSectionRow`, `Field`, `ButtonItem`,
`Focusable`, `staticClasses`, `call` and `toaster`.

### Offline caveat

The registry is reachable from this host, so the normal path (`npm ci`) needs
no shim or vendor copy: `dist/index.js` contains only our TSX plus the
BSD-licensed `@decky/api` client that the template always bundles, and it
imports `react`/`react-dom`/`@decky/ui` from the loader globals. If the registry
is unreachable, `./build.sh --offline` installs from the local npm cache; that
only works on a host whose cache already has the exact `package-lock.json`
tarballs. There is deliberately no vendored `@decky/*` runtime in this repo
(third-party code is not committed): a cold cache on an air-gapped host needs
the npm cache copied over, or a one-time `npm ci` on a connected machine. If a
build is ever produced with hand-written API shims instead, the adaptation is
two lines in `src/index.tsx`: import `call`/`toaster` from the shim module and
keep the loader's `window.__DECKY_SECRET_INTERNALS...deckyLoaderAPIInit`
handshake object name — but that path is not used or needed here.

The loader on the validated machine is Decky `v3.3.0`
(`/var/home/holzr/homebrew/services/.loader.version`), manifest
`api_version: 1`, `@decky/ui ^4.7.2`. `@decky/rollup` rewrites the
`@decky/ui` imports of `src/index.tsx` to the loader-provided `DFL`/`SP_REACT`
globals (the same shape every installed plugin ships) and inlines
`@decky/api`; the panel uses `definePlugin`, `PanelSection`, `PanelSectionRow`,
`Field`, `ButtonItem`, `Focusable`, `staticClasses`, `call` and `toaster`.

Without sudo, Decky's *Settings → Developer → Reload plugins* re-reads the
plugin directory; a changed `main.py` still needs the service restart because
the module is already imported in the loader process.

## Validation

```sh
# backend + synthetic fixture suite (no hardware, no root)
python3 -m py_compile tools/decky-oxp3-status/main.py
tools/oxp3-status.sh --selftest      # 90 checks against a temp /sys,/proc,HOME
tools/oxp3-status.sh                 # human readable
tools/oxp3-status.sh --json | python3 -m json.tool >/dev/null
tools/oxp3-status.sh --strict        # exit code 2 while anything is warn

# frontend: build, then import and render the real bundle with stubbed
# loader globals (react is a dev dependency only for this check)
cd tools/decky-oxp3-status && npm run smoke

# installer: resolution/validation unit tests, then the --root simulation
python3 -m unittest discover -s tests/unit -p 'test_decky_plugin.py' -q
tests/integration/decky/run.sh

# manifests / wrapper
python3 -m json.tool tools/decky-oxp3-status/plugin.json >/dev/null
python3 -m json.tool tools/decky-oxp3-status/package.json >/dev/null
python3 -m json.tool tools/decky-oxp3-status/tsconfig.json >/dev/null
sh -n tools/oxp3-status.sh

# after installing: the loader logs the plugin load/failure
sudo journalctl -u plugin_loader.service -b --no-pager | grep -i -E "oxp3|error"
```

The last recorded run: `--selftest` 90/90 checks (including the stub-verifier
fixtures for `run_verification_once`: pass, fail, skip, timeout, bad JSON,
capped output, escalation refusal and busy), `--json` valid JSON, `--strict`
exit 2, `smoke` loaded and rendered `dist/index.js` with the api handshake and
drove the 30 backend verification checks, the zip hash above was reproduced by
the `--offline` build, `test_decky_plugin.py` passed its 43 unit tests and
`tests/integration/decky/run.sh` passed its 43 `--root` assertions.

### `--selftest`

`main.py --selftest` (also exposed by `oxp3-status.sh --selftest`) builds a
throw-away machine under a temporary directory and runs every check against it:
synthetic `/sys` (sched_ext state, RAPL/PL1, platform_profile, cpufreq, DRM,
hwmon/thermal, battery), synthetic `/proc` (`wine64` running a Cyberpunk exe,
plus a `scx_cosmos` process), synthetic `/etc` and `/run`, fake
`systemctl`/`modinfo`/`rpm` scripts, and a fixture HOME with the XMX driver,
ICD, helper, IGC, shim, kernel cache, trace log, benchmark summary, Heroic
config and Flatpak override. It asserts the exact verdict per indicator,
redaction of client/secret environment variables, `na` for a non-XeSS title,
survival of corrupt JSON inputs and the CLI exit codes. The roots can also be
pointed at a fixture by hand with `OXP3_SYS_ROOT`, `OXP3_PROC_ROOT`,
`OXP3_ETC_ROOT`, `OXP3_RUN_ROOT`, `OXP3_HOST_DRIVER` and
`OXP3_RUNTIME_OVERRIDES` (read at import time; `--selftest` sets them
in-process). Recorded driver prefixes are cryptographic hashes, so the fixture
deliberately reports synthetic drivers as `warn` mismatches rather than
pretending they match the recorded builds.

Sanity checks of the panel itself, once installed: open QAM in Game Mode, the
panel should show the game name, `overall`, then the five groups; *Refresh now*
should toast the counts; expanding a row should reveal `detail`/`source`; the
problem filter should hide `ok` rows.

Expected result on this machine at the time of writing (the checks were run):
`overall = warn` with `23 ok / 5 warn / 0 fail / 0 unknown`. The warnings are
the honest state of the machine, not plugin errors: `sched_ext` is `disabled`
because `oxp3-gaming-sched.service` is not installed yet, `igdext_trace.log` is
absent (tracing off), and the Steam Flatpak override still uses
`cm-compile.sh` instead of the 128-GRF helper.

## Gamemode and sandbox limits

- The panel lives in the Steam UI, so it is only reachable in **Game Mode**. In
  desktop mode or over SSH use `tools/oxp3-status.sh`.
- Decky's backend runs on the host as root, so sysfs, `systemctl`, `modinfo`
  and hashing work the same in Game Mode. It does **not** run inside the
  game's flatpak/pressure-vessel sandbox.
- If the game's process is not visible from the loader (different PID
  namespace, different user, `/proc/<pid>/environ` restricted), detection falls
  back to the configured launcher environment and the report says so. The XMX
  rows then describe what a normal launch *will* do, not what this process got.
- `/proc/<pid>/environ` is only readable by the process owner or root; a
  `live: false` source means the row came from a config file, not from the
  running process.
- Clipboard access in gamescope is unreliable, so *Copy report JSON* may toast
  that the clipboard is unavailable; the CLI `--json` is the reliable export.
- Battery/temperature/PL1 rows depend on host sensors and can be `unknown`
  when a sensor is missing or restricted; they never fail the panel.
- The plugin cannot fix anything: status collection is read-only and *Run
  verification* only reports the harness result. The repair commands stay in
  `README.md`, `docs/WORKING-BUILD-2026-11-08.md` and the scripts, and require
  sudo once at install time.
- The verification button needs a repository checkout on the same host (see
  *checkout discovery* above). The zip alone does not contain the harness: it
  would duplicate the repository tools in the bundle, and `main.py` is the only
  shared implementation.
- The privilege drop to the desktop user is only exercised on the device: in a
  non-root checkout the backend runs as the current user, and the offline
  fixtures run with `run_as_deck_user=False`. The refusal path
  (`no-deck-user`, no unprivileged identity found while root) is unit-tested;
  the actual `user=`/`group=` switch needs the installed loader.
- `full` and `changes` are covered by stub fixtures only (flag assembly, the
  escalation gate and the parsers). Their live effects — one LED frame, a real
  suspend attempt and the speaker-EQ drop-in — are not run by the build; the
  first two stay SKIP because the harness runs as the deck user. Validate them
  once on the unit with `sudo tools/oxp3-verify.sh --full` in a terminal.

## Open points

- `dist/index.js` and `build/*.zip` are generated and not committed (the zip
  bundles the BSD-licensed `@decky/api` client). The build was verified on the
  target host with node 22/npm 10 and a reachable registry; on a host with a
  cold npm cache and no network the artifact must be built elsewhere or the
  cache copied (see *Offline caveat*).
- `@decky/ui` is a types-only package: the loader supplies the runtime `DFL`
  object, so a newer/older `@decky/ui` on a build host changes type checking,
  not the installed plugin. The panel only uses components present in Decky
  v3.3.0.
- The plugin is not published to the Decky store; `plugin.json`'s `publish`
  block is informational (tags + description, no image URL).
- Heroic parsing covers `envVars`, `enviromentOptions` and wrappers; Lutris,
  Bottles and hand-written launch scripts are only visible through the Steam
  Flatpak override / `oxp3-env.sh` fallback.
- `igdext_trace.log` and benchmark discovery use the prefix heuristics above; a
  custom prefix outside them shows `warn`/`unknown` instead of a false `ok`.
- The Steam Flatpak override and `~/.config/xess-xmx/oxp3-env.sh` are global:
  they apply to every Flatpak Steam title. A non-Cyberpunk title can therefore
  show `xmx.helper` as `warn` because the override still points at the release
  `cm-compile.sh`; that is a real observation about the machine, not a bug in
  the per-title logic.
- Game detection is heuristic: a `.exe` under `/steamapps/common/` or a path
  containing `/games/` is recognised, a bare renamed `argv[0]` is not. When in
  doubt the report says "no game detected" rather than guessing.
- The scheduler group will stay `warn` until `oxp3-gaming-sched.service` is
  installed and active (see `docs/WORKING-BUILD-2026-11-08.md`); the panel does
  not start it.
