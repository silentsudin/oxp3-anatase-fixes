# Decky plugin: live OXP3 fix status

`tools/decky-oxp3-status/` is a Decky Loader plugin that shows, in the Steam
Quick Access Menu, whether the OXP3 "Anatase" fixes are actually in effect for
the game that is running **right now**. `tools/oxp3-status.sh` is a thin
wrapper that runs the same Python backend headlessly, so the panel and the CLI
cannot drift apart.

Design rules:

- **read-only** — nothing is written, started, stopped or reconfigured;
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

The frontend calls `call("get_status")` every 5 s while the panel is mounted and
stops on unmount; there is no background/global polling.

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

Host toolchain: node 22, pnpm 10 (both present on the target machine).

```sh
cd tools/decky-oxp3-status
pnpm install
pnpm build                 # -> dist/index.js
pnpm typecheck             # optional, tsc --noEmit
```

Install (privileged steps are listed, not run, by this repo's rules):

```sh
rm -rf ~/homebrew/plugins/oxp3-status
cp -r tools/decky-oxp3-status ~/homebrew/plugins/oxp3-status
sudo systemctl restart plugin_loader.service
```

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
# backend
python3 -m py_compile tools/decky-oxp3-status/main.py
tools/oxp3-status.sh                 # human readable
tools/oxp3-status.sh --json | python3 -m json.tool >/dev/null
tools/oxp3-status.sh --strict        # exit code 2 while anything is warn

# manifests / wrapper
python3 -m json.tool tools/decky-oxp3-status/plugin.json >/dev/null
python3 -m json.tool tools/decky-oxp3-status/package.json >/dev/null
python3 -m json.tool tools/decky-oxp3-status/tsconfig.json >/dev/null
sh -n tools/oxp3-status.sh

# after installing: the loader logs the plugin load/failure
sudo journalctl -u plugin_loader.service -b --no-pager | grep -i -E "oxp3|error"
```

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
- The plugin cannot fix anything: every action it offers is read-only. The
  repair commands stay in `README.md`, `docs/WORKING-BUILD-2026-11-08.md` and
  the scripts, and require sudo once at install time.

## Open points

- `dist/index.js` is not committed and cannot be built in this environment
  (no network and no pnpm store for `@decky/*`). `pnpm install && pnpm build`
  must be run once on a machine with network access; the README says so.
  Consequently `tsc --noEmit`/rollup were not executed here — the Python
  backend, manifests and wrapper were validated, the TSX was reviewed against
  the loader's real component/prop usage found in installed plugins.
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
