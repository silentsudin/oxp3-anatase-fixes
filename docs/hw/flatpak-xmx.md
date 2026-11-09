# XeSS XMX for Flatpak Steam

Status: **feasible, no sandbox expansion needed — but not yet proven by an in-game trace on this host**, because the
Flatpak Steam on this machine has never been launched (see "What this host actually has"). The deployment is
implemented in `tools/oxp3-flatpak-xmx.sh`, and every path the launch needs was verified *inside* the sandbox.

## The gap this closes

The project wires XMX for native Steam and Heroic:

- `tools/xmx-enable.sh` writes `~/.config/xess-xmx/oxp3-env.sh` and an ICD json, and swaps the game's XeSS libraries.
- `oxp3-fixes-fast.sh install` (via `tools/lib/install-common.sh`: `install_xess`) runs the release `install.sh`, which
  installs the release's system-wide defaults into `~/.config/environment.d/50-xess-xmx.conf` and puts a
  `user_settings.py` hook into every **native** Proton.
- `install-common.sh` also writes a Flatpak *override* for Steam (`xmx_override_file`), currently holding
  `force_vk_vendor=0`, `ANV_CM_HELPER=…` and `VKD3D_DISABLE_EXTENSIONS=…`.

That combination cannot work for a Flatpak game, for three independent reasons:

1. **`environment.d` is not read inside the sandbox.** Flatpak builds the app's environment from its metadata and the
   user override only. So `VK_DRIVER_FILES` and the CM compiler variables never reach a Flatpak game: it runs the
   stock Mesa, the driver never injects a kernel, and XeSS falls back to DP4a even if everything else is correct.
2. **No shim in the Flatpak Proton/prefix.** The shim `igdext64.dll` is only ever deployed into *native* Protons by
   `install.sh`. XeSS finds the shim only if it sits in a Directory the game's prefix can see — the release's
   mechanism is `<prefix>/drive_c/windows/system32/driverstore/filerepository/igd_faux.inf_1/igdext64.dll` with
   `INTC_ALT_DRIVER_EXTENSIONS_PATH=C:\windows\system32\driverstore\filerepository\igd_faux.inf_1`
   (see the release's `enable.sh`). Copying into a Proton's `files/lib/wine/igdext/` only affects prefixes created
   *afterwards*, so an existing prefix does not pick it up.
3. **`user_settings.py` hooks do not run** for the Flatpak Protons (they are installed per Proton by `install.sh`, and
   even then they are what makes `XESS=`/`MESA=` launch-option shortcuts work; the long variables do not need them).

## What this host actually has (measured 2026-11-09)

This matters more than the general case, because the task assumes a Flatpak Steam with Proton prefixes:

| item | state |
|---|---|
| Flatpak Steam | `org.anatase.Steam` 1.0.0.87 (anatase remote), **not** `com.valvesoftware.Steam` (not installed) |
| `~/.var/app/org.anatase.Steam/data/Steam` | **empty** — never launched, no Proton, no `compatdata`, no prefixes |
| Steam actually in use | **native**: `/usr/bin/steam`, `~/.local/share/Steam` with Proton 11.0 / 9.0 / Experimental / Hotfix and `proton-cachyos` |
| shim already in native Protons | yes (`Proton - Experimental`, `Proton Hotfix` carry `igdext64.dll` + `.stock` backups) |
| `igdext_trace.log` anywhere | none |

Consequence: the mechanics below are implemented and dry-run/fixture verified, and sandbox visibility is verified
against the live sandbox — but an in-game trace from a *Flatpak* launch cannot exist on this host until a Flatpak
Steam Proton prefix exists. That is an environment gap, **not** a sandbox denial.

## Sandbox permissions (the decisive fact)

`flatpak info --show-permissions org.anatase.Steam` grants `filesystems=…;home;…` (plus `/mnt`, `/media`, `/run/media`,
`/home:ro`, `~/.steam:create`, `xdg-data/Steam:create`). Inside the sandbox `$HOME` is the same absolute path as on the
host (`/var/home/holzr`), so **every XMX path under `$HOME` is visible with the host's absolute path unchanged** — the
absolute paths in `~/.config/xess-xmx/oxp3-env.sh` stay valid inside the sandbox. Measured with
`flatpak run --command=sh org.anatase.Steam -c …`:

| path the launch needs | inside sandbox |
|---|---|
| `~/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so` (the CM driver) | VISIBLE |
| `~/.config/xess-xmx/*.json` (ICD) | visible (created by `install`) |
| `~/.local/share/xess-xmx-release/xess-xmx-linux-20261107/igdext64.dll` (the shim) | VISIBLE |
| `…/tools/xmx-cm-compile-nodgrf.sh` (the CM helper) | VISIBLE |
| `…/igc/neo/bin` (IGC/ocloc) | VISIBLE |
| `~/.cache/xess-xmx/kernels` (kernel cache) | **WRITABLE** |
| PCI id for the CM helper (`/sys/class/drm/…`) | `0xb080` (Panther Lake) readable |
| `ocloc-25.18.1` actually **executing** against the Flatpak runtime | **works** (`OCLOC-RUNS ok`) |

The last row is the one that could have blocked the whole approach (a bundled host binary against the runtime's libc);
it does not. `python3` also exists in the sandbox (`/usr/bin/python3`), which the helper needs on the packing path.

`/var/cache` is **not** the host's: each Flatpak app gets a private `/var` (`persistent=.`), so a "kernel cache in
`/var/cache`" would be invisible to host runs. The kernel cache therefore lives in `~/.cache/xess-xmx/kernels`, which is
shared with host runs — a warm cache is reused by both, and both can write it.

## What the shim needs, and where it lives for Flatpak

| requirement | for Flatpak Steam | notes |
|---|---|---|
| `igdext64.dll` reachable | copy into the **game prefix** DriverStore (user-owned, under `~/.var/app/<app>/data/Steam/…`) | per-prefix; Steam does not verify `compatdata` |
| `INTC_ALT_DRIVER_EXTENSIONS_PATH` | `C:/windows/system32/driverstore/filerepository/igd_faux.inf_1` (Windows path, forward slashes) | inside the Wine prefix, not a host path |
| driver `.so` + ICD json | under `$HOME`, visible | our own `icd-flatpak.json`, the host's `icd-xmx.json` is not touched |
| `ANV_CM_HELPER`, `XMX_IGC_DIR` | under `$HOME`, visible | `ocloc` runs in the sandbox |
| `ANV_CM_KERNEL_DIR` | `~/.cache/xess-xmx/kernels`, writable | not `/var/cache` (private to the app) |
| `VKD3D_DISABLE_EXTENSIONS` | launch option | overrides are per-app, launch options per-game |
| `force_vk_vendor=0` | launch option | already in the current override, repeated per-game for clarity |
| `~/.config/environment.d` defaults | **host-only, not visible** | replaced by launch options |

Nothing needs `/usr`, `/etc`, `/var/lib/flatpak` or another host-only path. No `flatpak override --filesystem` is
required.

## Usage

    tools/oxp3-flatpak-xmx.sh status                       # what the Flatpak Steam has, what is deployed
    tools/oxp3-flatpak-xmx.sh status --probe-sandbox       # + start the sandbox and check every path
    tools/oxp3-flatpak-xmx.sh install --appid <appid> -n   # dry run: print every action
    tools/oxp3-flatpak-xmx.sh install --appid <appid>      # deploy shim + env + ICD for that game
    tools/oxp3-flatpak-xmx.sh remove                       # restore backups, delete only our files
    tools/oxp3-flatpak-xmx.sh launch-options               # the exact launch-options line

Options: `--app <flatpak id>` (default: auto-detect), `--package <dir>` (default: newest
`~/.local/share/xess-xmx-release/xess-xmx-linux-*` that carries the shim *and* a runnable `ocloc`), `--driver-so <so>`
(default: what the live `icd-xmx.json` points at), `--mode prefix|proton|both` (default `prefix`; `proton` also puts the
shim into each Flatpak Proton's `files/lib/wine/igdext/`, which only affects prefixes created later), `--json`.
`OXP3_FLATPAK_DATA` points the tool at another data dir (used by the fixture test).

`install` runs the game once first: it needs `<data>/Steam/steamapps/compatdata/<appid>/pfx`. It writes
`~/.config/xess-xmx/flatpak-env.sh`, `icd-flatpak.json` and records every deployed path in
`flatpak-install.tsv` so `remove` can restore exactly that.

### Exact Steam launch options

Steam → *game* → Properties → General → Launch options (one line, spaces separate the assignments from `%command%`):

    XESS_LOG=1 VK_DRIVER_FILES=/var/home/holzr/.config/xess-xmx/icd-flatpak.json ANV_CM_KERNEL_DIR=/var/home/holzr/.cache/xess-xmx/kernels ANV_CM_HELPER=/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/xmx-cm-compile-nodgrf.sh XMX_IGC_DIR=/var/home/holzr/.local/share/xess-xmx-release/xess-xmx-linux-20261107/igc VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer,VK_EXT_descriptor_heap force_vk_vendor=0 INTC_ALT_DRIVER_EXTENSIONS_PATH=C:/windows/system32/driverstore/filerepository/igd_faux.inf_1 %command%

`tools/oxp3-flatpak-xmx.sh launch-options` prints this line for the current host (paths are absolute; none contains a
space). Drop `XESS_LOG=1` for a normal run — it is only there so the first launch produces the trace.

Notes:

- Valve's launch options are the documented per-game mechanism and need no override and no permission. The variables
  apply to the game process, which is where the Vulkan driver and the shim live.
- `XESS=latest`, `MESA=…`, `FSR`, `FSR_FG`, `OFLOW_FG` shortcuts need the `user_settings.py` hook; they are **not**
  available on this path. Use the long variables instead (`XESS_LOG=1`, and the release's
  `tools/fsr-fg.sh` for FSR-on-XeSS).
- Games with XeSS older than 2.0.2.68 (Cyberpunk 2077 ships 2.0.1.41) still need Intel SDK 3.0.2 libraries in the game
  folder; that is `tools/xmx-enable.sh`'s job and is unchanged by the Flatpak path — do it in the Steam library the
  Flatpak Steam uses.
- If a game's launch options already contain something (e.g. `gamemoderun %command%`), put the assignments first and
  keep the wrapper before `%command%`: `… VKD3D_DISABLE_EXTENSIONS=… gamemoderun %command%`.

## Verification (a trace, not a claim)

1. `tools/oxp3-flatpak-xmx.sh status --probe-sandbox` — every needed path VISIBLE/WRITABLE, `OCLOC-RUNS ok`.
2. Launch the game once with `XESS_LOG=1` in the launch options, enable XeSS in its menu.
3. The shim writes `C:\igdext_trace.log` inside the game's prefix:

       ~/.var/app/org.anatase.Steam/data/Steam/steamapps/compatdata/<appid>/pfx/drive_c/igdext_trace.log

   No file at all means the shim was not loaded. `tools/oxp3-flatpak-xmx.sh status` prints the trace's mtime and line
   count per prefix when it exists.
4. Kernel side: `~/.cache/xess-xmx/compile.log` gets one line per compiled kernel; failures say
   `cm-compile FAILED`/`ocloc not found`. First run compiles a handful of kernels (seconds), afterwards they load from
   `~/.cache/xess-xmx/kernels`.
5. Optional: `PROTON_LOG=1` additionally writes `~/steam-<appid>.log` with the game's stderr (`ANV CM:` lines;
   `ANV_CM_DEBUG=1` lists each injected kernel).

A trace proves the shim loaded and answered XeSS. It does not prove a performance win: compare XeSS on/off or add
`IGDEXT_FORCE_FALLBACK=1` for the DP4a path, as in `docs/xess-xmx.md`.

## Rollback

    tools/oxp3-flatpak-xmx.sh remove

restores every `igdext64.dll` from its `.stock` backup (or deletes the file when we created it), removes `ControlLib.dll`
we installed, deletes `flatpak-env.sh` and the state file, and leaves `icd-flatpak.json` in place because it is inert
without `VK_DRIVER_FILES`. Remove the launch options in Steam afterwards. Nothing outside `$HOME` was touched, so there
is no Flatpak state to undo; `flatpak uninstall --unused` is not involved.

## If it turns out to be blocked: the two alternatives and their risk

The recipe above needs no permission change. If a future Flatpak Steam build drops `home` (or the game lives on a path
outside `$HOME`), the fallback order is:

1. **`flatpak override --user --filesystem=home` (or a narrower `--filesystem=/path/to/xess-xmx`)**
   *Pro:* makes the shim, driver and compiler reachable; minimal and reversible (`--reset`).
   *Risk:* it widens **every** Flatpak Steam game to the whole home directory (read/write) — the sandbox stops
   protecting the user's files and SSH keys from the game's DRM/anti-cheat/updater code. Prefer the narrowest prefix
   that covers `~/.local/share/xess-xmx-release`, `~/.cache/xess-xmx` and `~/.config/xess-xmx` rather than `home`.
   Also note the existing project override already puts variables in that file; a filesystem entry is additive.
2. **Native Steam only (the project's current requirement)**
   *Pro:* exactly the configuration measured in `docs/xess-xmx.md` (65–86 FPS, trace-verified); no sandbox in the way.
   *Risk:* games that are only in the Flatpak Steam library do not get XMX; running two Steams against one library is
   not advisable, and the Flatpak remains second-class.

A third option — a custom Proton built as a Flatpak "compatibility tool" extension — was not pursued: it needs a
Flatpak SDK build and still does not solve the environment (`VK_DRIVER_FILES` must be set per game anyway).

## Security tradeoffs of the deployment we do use

- No permission is added. The tool only writes inside `$HOME` (it refuses anything else, including `/var/lib/flatpak`,
  `/app`, `/usr`, `/etc` and `.desktop` files) and it refuses to run as root.
- The shim is copied into a **user-owned prefix**; it is a Windows DLL loaded by the game through XeSS. It is the same
  binary the native path already uses, from the same release directory. `compatdata` is not covered by Steam's file
  verification, so the backup/restore discipline is what keeps "Verify integrity" and Steam updates clean.
- The launch options put the game on a **separate Vulkan driver** (the CM-patched ANV) via `VK_DRIVER_FILES`. Other
  games are unaffected because the variable is per-launch-option; the host's `icd-xmx.json` and the system default
  driver are not modified.
- The kernel cache in `~/.cache/xess-xmx/kernels` is shared with host runs; a Flatpak game can write there. That is
  the user's own cache directory, already used by the native path.

## Fixture test (what is proven without a Flatpak Steam library)

No Flatpak Proton exists on this host, so the mechanics were exercised against a synthetic `$HOME` fixture
(`OXP3_FLATPAK_DATA`/`HOME` redirection, real package as the source, nothing outside `/tmp`):

| check | result |
|---|---|
| `install` copies the shim into the prefix DriverStore + the Proton's igdext dir, backs both up as `.stock` | pass |
| `ControlLib.dll` installed once, left alone when a foreign one exists | pass |
| `install` twice is idempotent ("shim already current"), backups never overwritten | pass |
| `remove` restores both DLLs from `.stock`, deletes our `ControlLib.dll`, drops env/state, keeps the ICD | pass |
| `remove` twice is a no-op | pass |
| `--dry-run` performs no write at all (not even a `mkdir`) | pass |
| refusals: root, no `--appid`, unknown appid, missing package, bad `--mode`, unknown flag, write outside `$HOME` | pass |
| `status --json` is valid JSON | pass |

What the fixture cannot prove: that XeSS inside a Flatpak-launched game actually loads the shim (needs a real Flatpak
Proton prefix and a game) — the open item below.

## Open items

- **No in-game Flatpak trace yet.** Either launch the installed `org.anatase.Steam` once, install a Proton and a game
  there, then run `install --appid <appid>` and follow the verification above; or accept native Steam as the supported
  path. Until then the Flatpak route is "sandbox-verified, mechanics-verified, trace-pending".
- The existing override at `~/.local/share/flatpak/overrides/org.anatase.Steam` holds
  `ANV_CM_HELPER=/var/home/holzr/.local/share/xess-xmx/tools/cm-compile.sh` — the **old checkout's** helper, not the
  128-GRF `tools/xmx-cm-compile-nodgrf.sh` that the shipping configuration uses, and it has no `VK_DRIVER_FILES` or
  `INTC_ALT_DRIVER_EXTENSIONS_PATH`. It cannot make XMX work by itself and it would compile kernels with `-doubleGRF`
  if a driver were present. The launch options above override it per game; consider trimming that override.
- If a game lives in a Steam library outside `$HOME` (e.g. `/run/media/…`), the Flatpak has `/media` and `/run/media`
  mounted — the tool follows `libraryfolders.vdf` only for the app's own data dir today; add the library path if needed.
- `docs/xess-xmx.md` still says "Flatpak Steam is not supported by the project"; it now points here.
