# OXP3 Status — Decky Loader plugin

Live, **read-only** status of the OneXPlayer 3 ("OXP3") Anatase fixes for the
game that is running now, in the Steam Quick Access Menu, plus a **Run
verification** action that executes the acceptance harness
(`tools/oxp3-verify.sh`) on demand. The headless CLI (`tools/oxp3-status.sh`)
runs the same status checks and is the fallback whenever the Steam UI or Decky
is not available.

The plugin backend (`main.py`) does all the work: it finds the running game,
collects the configured launch environment, hashes the drivers, reads the
scheduler/power sysfs files and returns one JSON report. `src/index.tsx` only
polls `get_status` every 5 s and renders it.

## Two-line overview (1.1.0)

The first two rows of the panel answer the two questions first; the full
matrix described below is unchanged underneath them:

1. **Fix health** — the backend runs `tools/oxp3-verify.sh --json` (quick mode,
   bounded timeout, cached for 90 s) and derives one headline. With no FAIL it
   is green: *"All fixes applied and working"*. Otherwise it is amber/red
   *"Needs attention: id, id +N more"*. Activate the row to expand the top five
   issues with their message and remediation hint. SKIP/UNKNOWN checks are
   never reported as working; an UNKNOWN-only report says *"Needs attention"*,
   and a report where nothing passed says *"Nothing verified"*.
2. **XeSS path of the running game** — *"XeSS XMX: active (Game)"* (green),
   *"XeSS: detected, XMX shim NOT active (Game)"* (amber),
   *"XeSS: current game does not use it"*, or *"No game running"*. Detection
   reads the game process tree's `/proc/<pid>/maps`/`map_files` and `environ`
   for `libxess*`/`xess*.so`, a Vulkan layer/ICD referencing XeSS, the per-game
   XMX ICD (`icd-xmx.json`/`icd-flatpak.json`), the XMX driver, or the
   helper/shim variables. Unreadable `/proc` entries are skipped. Activate the
   row to see the evidence string.

The two lines are rendered by `OverviewLines` in `src/index.tsx` from the
`get_overview()` backend method; the verification section's *Run quick
verification* refreshes them with the fresh result.

## Indicators

| group | what it checks | expected |
|---|---|---|
| Mesa driver | `/usr/lib64/libvulkan_intel.so` sha256 | prefix `5a75b6a44d285fd1` (previous `ec418f04e589965a` accepted) |
| Mesa driver | `VK_DRIVER_FILES` / `VK_ICD_FILENAMES` override | unset, or points at a known driver |
| XeSS XMX | `VK_DRIVER_FILES` → `~/.config/xess-xmx/icd-xmx.json` (or the driver directly) | XMX ICD |
| XeSS XMX | `ANV_CM_HELPER` | contains `xmx-cm-compile-nodgrf` (128-GRF) |
| XeSS XMX | `XMX_IGC_DIR` | directory with `neo/bin/ocloc` |
| XeSS XMX | `INTC_ALT_DRIVER_EXTENSIONS_PATH` | contains `igdext64.dll` |
| XeSS XMX | `VKD3D_DISABLE_EXTENSIONS` | contains `VK_EXT_descriptor_buffer` |
| XeSS XMX | `~/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so` | prefix `d5e7d7677cc29882` (26.3.0; rc3 `4b1d55ea9483417f` accepted) |
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

Verified on the target host: node **v22.23.1**, npm **10.9.8**, python 3.15,
npm registry reachable. Decky-side packages: `@decky/rollup` 1.0.2, rollup
4.68.0, typescript 5.9.3, `@decky/api` 1.1.3 (bundled), `@decky/ui` 4.16.5
(types only; the loader provides the runtime `DFL`). React 18.3.1 is a dev
dependency solely for the headless smoke test.

```sh
cd tools/decky-oxp3-status
./build.sh                 # npm ci, typecheck, rollup, smoke, selftest, zip
./build.sh --offline       # same with `npm ci --offline` (warm npm cache)
./build.sh --no-install    # rebuild from existing node_modules
```

`build.sh` produces `build/oxp3-status-1.1.0.zip` and prints its size and
sha256. The zip is deterministic (fixed entry timestamps, sorted entries), so
the hash is reproducible from the same sources; it is intentionally not
hard-coded here because this README is itself inside the archive. Individual
steps: `npm run typecheck`, `npm run build`
(rollup → `dist/index.js`), `node wrap-iife.mjs dist/index.js`, `npm run
smoke` (evaluates and renders the real IIFE with stubbed loader globals),
`npm run test` (renders the two overview lines in every state), `npm run
selftest`, `npm run package`.

`dist/` and `build/` are generated and not committed. There is no vendored
`@decky/*` runtime in this repo.

### Offline caveat

The npm registry is reachable from this machine, so no shim or vendor copy is
needed. With no network, `./build.sh --offline` installs the exact locked
versions from the local npm cache; a cold cache cannot work because
third-party `@decky/*` code is deliberately not committed. Build once on a
connected machine (or copy `~/.npm` over) and then use `--offline`. The bundle
keeps `react`, `react-dom` and `@decky/ui` as loader globals; only the
BSD-licensed `@decky/api` client is inlined, exactly as the official template
does.

## Install

Preferred: Decky → Settings → General → **Install Plugin from File** → select
`build/oxp3-status-1.1.0.zip`. The zip root is the plugin root:

```
plugin.json          manifest, api_version 1
main.py              backend imported by the loader
dist/index.js        frontend bundle (ESM, loader globals)
dist/index.js.map    source map
README.md            these notes
```

Manual install (privileged steps are listed, not run, by this repo's rules):

```sh
rm -rf ~/homebrew/plugins/oxp3-status
cp -r tools/decky-oxp3-status ~/homebrew/plugins/oxp3-status
sudo systemctl restart plugin_loader.service
```

Only `plugin.json`, `main.py` and `dist/` are needed at runtime. Without sudo,
Decky's *Settings → Developer → Reload plugins* re-reads the directory, but a
changed `main.py` needs the service restart because the module is already
imported in the loader process. Uninstall: remove
`~/homebrew/plugins/oxp3-status` and restart the loader.

## Verification action

The panel's **Verification** row runs `tools/oxp3-verify.sh --json` (quick),
`--full --json` or `--full --with-changes --json` and shows `overall`, the
counts and the top failing/skipped checks. Full and full+changes always need a
second confirmation click. The backend runs the harness as the desktop user
with a minimal environment, one run at a time, a hard timeout (180/900/1200 s),
stdout capped at 1 MiB / 20 000 lines and stderr at 8 KiB; errors are returned
as `{ok: false, error: {code, message}}` and never raised into the loader.
Root-only steps stay SKIP — use `sudo tools/oxp3-verify.sh --full` in a
terminal for those. It never reboots, installs or asks for sudo.

## Headless CLI (same checks)

```sh
tools/oxp3-status.sh              # coloured, human readable
tools/oxp3-status.sh --json       # full report for scripts
tools/oxp3-status.sh --strict     # exit 2 if anything is warn/unknown
tools/oxp3-status.sh --watch 5    # refresh in a terminal
tools/oxp3-status.sh --selftest   # synthetic /sys, /proc, HOME; no hardware
```

Exit codes: `0` no failures, `1` at least one `fail`, `2` `--strict` and
warn/unknown present, `3` backend could not start. Secret/socket environment
variables are scrubbed from the report's `game.env` (allowlisted XMX/Steam/VK
keys only, `env_omitted` counts the rest); the backend still uses the full
environment internally.

## Validation

```sh
python3 -m py_compile main.py
python3 main.py --selftest          # 137 checks, synthetic fixtures, exit 0
./build.sh                          # typecheck + build + smoke + selftest + zip
python3 -m json.tool plugin.json >/dev/null
sh -n ../oxp3-status.sh
```

Last recorded run on this host: `--selftest` 137/137, `npm run smoke` ok, `npm run test` ok,
`--json` valid, `--strict` exit 2, `overall=warn` with
`23 ok / 5 warn / 0 fail / 0 unknown`. The five warnings are the machine's
honest state: `sched_ext` disabled / no `scx_cosmos` / `oxp3-gaming-sched.service`
not installed, `igdext_trace.log` absent (tracing off), and the Steam Flatpak
override still pointing at `cm-compile.sh` instead of the 128-GRF helper.
See `docs/decky-plugin.md` for the full indicator list and the
Gamemode/sandbox caveats.

## Files

| path | purpose |
|---|---|
| `plugin.json` | Decky manifest (`api_version` 1) |
| `package.json` / `package-lock.json` | build metadata and locked deps |
| `tsconfig.json` | strict TypeScript config |
| `rollup.config.js` | `@decky/rollup` build |
| `src/index.tsx` | QAM panel: polling, grouping, detail expansion |
| `main.py` | backend: all checks, `Plugin.get_status()`, CLI, `--selftest` |
| `build.sh` | install → typecheck → build → smoke → selftest → zip |
| `package.py` | deterministic zip writer, validates the artifact layout |
| `test/smoke.mjs` | evaluates/renders the IIFE `dist/index.js` with loader globals |
| `test/overview.mjs` | renders the two overview lines in every health/XeSS state |
| `README.md` | this file |

Our own code only; no vendor binaries, decompiled listings or firmware are
included. Fixture files used by `--selftest` are generated at runtime in a
temporary directory.
