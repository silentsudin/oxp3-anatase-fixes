# OXP3 Status — Decky Loader plugin

Live, **read-only** status of the OneXPlayer 3 ("OXP3") Anatase fixes for the
game that is running now, in the Steam Quick Access Menu. The headless CLI
(`tools/oxp3-status.sh`) runs the same checks and is the fallback whenever the
Steam UI or Decky is not available.

The plugin backend (`main.py`) does all the work: it finds the running game,
collects the configured launch environment, hashes the drivers, reads the
scheduler/power sysfs files and returns one JSON report. `src/index.tsx` only
polls `get_status` every 5 s and renders it.

## Indicators

| group | what it checks | expected |
|---|---|---|
| Mesa driver | `/usr/lib64/libvulkan_intel.so` sha256 | prefix `ec418f04e589965a` |
| Mesa driver | `VK_DRIVER_FILES` / `VK_ICD_FILENAMES` override | unset, or points at a known driver |
| XeSS XMX | `VK_DRIVER_FILES` → `~/.config/xess-xmx/icd-xmx.json` (or the driver directly) | XMX ICD |
| XeSS XMX | `ANV_CM_HELPER` | contains `xmx-cm-compile-nodgrf` (128-GRF) |
| XeSS XMX | `XMX_IGC_DIR` | directory with `neo/bin/ocloc` |
| XeSS XMX | `INTC_ALT_DRIVER_EXTENSIONS_PATH` | contains `igdext64.dll` |
| XeSS XMX | `VKD3D_DISABLE_EXTENSIONS` | contains `VK_EXT_descriptor_buffer` |
| XeSS XMX | `~/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so` | prefix `4b1d55ea9483417f` |
| XeSS XMX | `~/.cache/xess-xmx/kernels/*.cmk` | populated, no compile failures |
| XeSS XMX | `igdext_trace.log` in the game prefix | present (tracing runs only) |
| XeSS XMX | latest Cyberpunk `benchmarkResults/*/summary.json` | `upscalingType` / XeSS on |
| CPU scheduler | `/sys/kernel/sched_ext/state`, `scx_cosmos` process, `oxp3-gaming-sched.service` | `enabled`, running, active |
| Other fixes | patched `xe.ko`, `rapl-power.service`, `oxp3-rapl-pp1.service`, PL1, `platform_profile`, Heroic GameMode, Flatpak runtime override, Steam Flatpak XMX env | as recorded in `docs/WORKING-BUILD-2026-11-08.md` |
| System | kernel, per-policy governor/EPP, GPU/Mesa, temperatures, battery | `powersave` + `balance_power`, etc. |

States: `ok`, `warn` (configured but not as recorded, or absent), `fail`
(expected artefact missing/unreadable in a way that breaks the feature),
`unknown` (cannot be determined in this sandbox/user context), `na`
(not applicable to the current title — e.g. the XMX environment rows while a
non-XeSS game runs). Every check is defensive: a missing file, permission
error, timeout or unparseable value becomes `unknown`/`warn`, never an
exception.

## Build

Node 22 + pnpm (the versions on the target host). Dependencies are the standard
Decky ones (`@decky/ui`, `@decky/api`, `@decky/rollup`); there are no extra
runtime dependencies and the plugin makes no network requests.

```sh
cd tools/decky-oxp3-status
pnpm install        # needs network or a warm pnpm store
pnpm build          # rollup -> dist/index.js
pnpm typecheck      # tsc --noEmit (optional)
```

`pnpm build` produces `dist/index.js`, which is what Decky Loader loads. The
build output is **not committed** (it is generated from `src/`).

## Install

```sh
# copy the whole directory (plugin.json + main.py + dist/) onto the host
cp -r tools/decky-oxp3-status ~/homebrew/plugins/oxp3-status

# reload the loader (privileged; the loader runs as root)
sudo systemctl restart plugin_loader.service
```

Decky then shows **OXP3 Status** in the Quick Access Menu (plugin manager).
Without sudo, Decky's *Settings → Developer → Reload plugins* re-reads the
plugin list, but a changed `main.py` needs the service restart because the
backend module is already imported.

Uninstall: remove `~/homebrew/plugins/oxp3-status` and restart the loader.

## Headless CLI (same checks)

```sh
tools/oxp3-status.sh              # coloured, human readable
tools/oxp3-status.sh --json       # full report for scripts
tools/oxp3-status.sh --strict     # exit 2 if anything is warn/unknown
tools/oxp3-status.sh --watch 5    # refresh in a terminal
```

Exit codes: `0` no failures, `1` at least one `fail`, `2` `--strict` and
warn/unknown present, `3` backend could not start.

## Validation

```sh
# backend logic, no Steam UI needed
python3 -m py_compile main.py
python3 main.py --text
python3 main.py --json | python3 -m json.tool >/dev/null

# plugin manifests
python3 -m json.tool plugin.json >/dev/null
python3 -m json.tool package.json >/dev/null
python3 -m json.tool tsconfig.json >/dev/null

# wrapper
sh -n ../oxp3-status.sh
```

On the machine this plugin was written for, the report currently shows the XMX
stack, host driver, patched `xe`, RAPL/PL1 and the Flatpak runtime override as
`ok`; the scheduler group is `warn` because `oxp3-gaming-sched.service` is not
installed/active yet, and the `igdext_trace.log` row is `warn` because tracing
is off in a normal run. See `docs/decky-plugin.md` for the full indicator list
and the Gamemode/sandbox caveats.

## Files

| path | purpose |
|---|---|
| `plugin.json` | Decky manifest (`api_version` 1) |
| `package.json` | build metadata and dependencies |
| `tsconfig.json` | strict TypeScript config |
| `rollup.config.js` | `@decky/rollup` build |
| `src/index.tsx` | QAM panel: polling, grouping, detail expansion |
| `main.py` | backend: all checks + `Plugin.get_status()` + CLI |
| `README.md` | this file |

Our own code only; no vendor binaries, decompiled listings or firmware are
included.
