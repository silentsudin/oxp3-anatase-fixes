# WORKING BUILD - 2026-11-08 (recorded as known-good)

Recorded before further changes. This is the configuration that ran: Steam games in Gamemode, Cyberpunk 2077 at
51-53 FPS without upscaling, and the Wukong benchmark at full speed once its shaders were warm.

Machine: OneXPlayer 3, Arc B390 (8086:b080), Panther Lake, Anatase Linux, kernel 7.2.7-an28.fc45.x86_64.

## Drivers (bit-exact copies preserved)

| role | sha256 | source | preserved copy |
|---|---|---|---|
| host `/usr/lib64/libvulkan_intel.so` | `ec418f04e589965adeb2...` | `~/.cache/oxp3-fixes/mesa-fast/usr/local/lib64/` | `~/.cache/oxp3-fixes/known-good/host-ec418f04-libvulkan_intel.so` |
| Flatpak runtime | `4d043cc2851a11fd6637...` | `~/.cache/oxp3-fixes/mesa-fast-prev/usr/local/lib64/` | `~/.cache/oxp3-fixes/known-good/runtime-4d043cc2-libvulkan_intel.so` |

Both are 64-bit only, self-contained (no libexpat dependency), Meso main `ea40598` + Anatase overrides, built with
`-Dbuildtype=release -Dprefer_static=true -Dvulkan-drivers=intel -Dgallium-drivers=iris,llvmpipe
-Dplatforms=x11,wayland -Dglx=dri -Dllvm=enabled`.

- Host build `ec418f04`: patches `0200 0201 0206 0207 0212`.
- Runtime build `4d043cc2`: patches `0200 0201 0205 0206 0207 0208 0211` (the pre-0212 set; `0205`, `0208` and
  `0211` are in `patches/subsumed/`). It differs only by RT tuning patch layering and measured equivalently on
  Cyberpunk within run-to-run noise.

To rebuild the host build: `./oxp3-fixes-fast.sh build --skip-gamescope --skip-mangohud --skip-kernel
--skip-firmware --skip-xess --skip-extras`. To restore the exact recorded binaries instead of rebuilding, copy the
preserved files back (host: `/usr/lib64`, runtime:
`/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides/lib64/`), keep
`lib64/libvulkan_intel.so` in the override list, and restart `anatase-flatpak-extensions.service`.

## System state

- Kernel modules: layered `kmod-oxp3` + `oxp3-firmware` for 7.2.7-an28 (`modinfo xe` -> `.../updates/.../xe.ko.xz`).
- Power: `platform_profile=balanced`, MSR and MMIO PL1 35 W, `oxp3-tdp-sync.service` active.
- `gamescope-3.16.23.3-1.fc45.oxp3.202610292302` (x86_64 + i686), `mangohud-0.8.4` same build date.
- Steam Flatpak `org.anatase.Steam` override:
  `force_vk_vendor=0`, `ANV_CM_HELPER=/var/home/holzr/.local/share/xess-xmx/tools/cm-compile.sh`,
  `VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer,VK_EXT_descriptor_heap`.
- No `libexpat.so.1` in the Flatpak runtime overrides (the drivers do not need it).

## Game-side settings that are part of the working configuration

- Heroic: **GameMode off for every game** (`useGameMode` false everywhere). `gamemoderun` forced the CPU governor to
  performance and took 12-15 W from the GPU on this 35 W package (41.2 -> 53.2 FPS when disabled).
- Non-Steam titles in Gamemode: `/var/home/holzr/heroic-launch.sh` (sha256 `cdd8f6399b5cfedd...`) routes
  `appName=1423049311` (Cyberpunk) to `tools/cp2077-direct.sh` (sha256 `4d60e50da2ca8e7b...`) via `flatpak-spawn
  --host`, so Proton runs without Heroic/umu stacked inside Steam's sandbox. Other Heroic apps unchanged.
- Steam launch options: no `gamemoderun`, no MangoHud, no debug variables left set.
- Wukong benchmark tool: `Dx12=0` (DX11/DXVK path), 1920x1200, vsync off, no upscaling.

## Benchmark results to compare against

- Cyberpunk 2077 (Heroic prefix, direct Proton, 1920x1200, upscaling OFF, texture Medium): 51.5 and 53.2 FPS
  (`avgFps` in benchmarkResults summaries), stock baseline 28.9.
- Wukong benchmark: full speed in Gamemode once warm; **first run after any driver change is single-digit FPS
  because Mesa's disk shader cache is keyed per driver build** (~30k cache files rewritten per swap).

## Verify the recorded state

    tools/check-working.sh

Prints OK/MISMATCH for: host driver hash, runtime driver hash, absence of the expat override, PL1, patched kernel
module, and Heroic GameMode being off.

## Rollback

- Stock host driver: `./oxp3-fixes-fast.sh uninstall` (backup in `/usr/local/share/oxp3-fixes-fast/stock/`).
- Stock Steam runtime driver: remove `libvulkan_intel.so` from the Flatpak override dir and restart
  `anatase-flatpak-extensions.service`.
- Git: tag `working-2026-11-08` marks this record.

## CPU scheduler addition (validated 2026-11-08 19:18-19:55)

Best CPU scheduling configuration for this hybrid CPU (2 P + 8 E + 4 LP-E, no SMT), validated with the harness in
`tools/cpu-sched-test.sh` (Cyberpunk 2077 direct-Proton benchmark + GravityMark, package power sampled for the whole
window):

    scheduler: scx_cosmos -c 0 -p 0        # official scx_loader "gaming" profile
    EPP:       balance_power (firmware default, unchanged)
    governor:  powersave (intel_pstate default)
    affinity: no restriction (the scheduler decides placement)

Measured (warm caches, same working driver/kernel state as above):

| config | Cyberpunk avg / min | GravityMark | avg package W | FPS/W |
|---|---|---|---|---|
| EEVDF (baseline) | 49.8 / 43.3 | 182.8 | 25.6 | 1.945 |
| scx_cake | 50.8 / 44.7 | 182.8 | 25.5 | 1.992 |
| scx_cosmos lowlatency | 51.5 / 44.8 | 182.5 | 25.4 | 2.028 |
| **scx_cosmos gaming** | **52.2 / 45.4**, repeat **54.0 / 46.6** | 182.6-189.6 | 25.0-25.4 | **2.055-2.160** |
| scx_p2dq | 51.9 / 45.1, repeat 53.1 / 46.2 | 182.8-187.5 | 25.3-25.4 | 2.043-2.099 |
| scx_cosmos gaming + per-cluster EPP | 50.9 / 44.4 | 187.5 | 26.3 | 1.935 |

Decisions:
- Ship `scx_cosmos -c 0 -p 0`; it is +4.8 % Cyberpunk average and +5.7 % FPS/W over EEVDF, GravityMark unchanged.
- Do NOT pair it with per-cluster EPP: `P=performance / E=balance_performance / LP-E=power` cost ~3 FPS and ~1 W.
- Exclude LAVD: `scx_lavd --balanced` aborts (`runnable task stall (rapl-power ...)`), `--no-core-compaction`
  black-screens Cyberpunk.
- GravityMark is scheduler-insensitive (geometry/GPU-bound); use it only as a regression guard.

Activation: shipped as `oxp3-gaming-sched.service` (`files/oxp3-gaming-sched.service`, installed by
`./oxp3-fixes-fast.sh install`, opt-out `--skip-sched`). As of this record the unit is **not yet active on the
machine** (state=disabled), so the live system is still on EEVDF; activate with:

    ./oxp3-fixes-fast.sh install --skip-mesa --skip-kernel --skip-firmware --skip-gamescope --skip-mangohud --skip-xess

and verify with `./state-snapshot.sh | grep sched_ext` (expect `state=enabled`) and
`systemctl status oxp3-gaming-sched`. Opt out at any time with `systemctl stop oxp3-gaming-sched` (the kernel returns
to EEVDF immediately; no reboot).
