# Best-known configuration and benchmark harness (2026-11-08)

This file exists because the best-measured configuration was nearly lost. Whenever a benchmark number is
quoted, capture `./state-snapshot.sh` (driver hashes, PL1, profile, kernel module, temps) with it.

## Best known state (~04:10-04:14 on 2026-11-08)

- Driver: `mesa-build/install-shiprt/usr/local` at the time (historical; current builds live in
  `~/.cache/oxp3-fixes/mesa-fast/usr/local`) (Mesa 27.0.0-deve; main + 0205 HPLOC + 0211 with the tuning
  knobs ANV_BVH_TS / ANV_ENCODE_* / ANV_MORTON_KVS), sha256 of libvulkan_intel.so
  `2517930b40d9093486705d84880ebe9b50f4e18f86a5d524dfe3830f25516e5c`, 24066304 bytes. Installed into the host
  `/usr/lib64` overlay AND the org.anatase.Platform Flatpak runtime overrides together with the bundled
  `libexpat.so.1` (the driver needs LIBEXPAT_2.9.0; the stock copies are 1.12.3).
- Kernel: layered `kmod-oxp3` + `oxp3-firmware` for 7.2.7-an28 (modinfo xe -> `.../updates/.../xe.ko.xz`).
- Power: `platform_profile=balanced`, MSR and MMIO PL1 35 W (`oxp3-tdp-sync` copies hhd's MMIO value to MSR).
- Rest of the stack: patched gamescope 3.16.23.3, MangoHud 0.8.4, XeSS-XMX shim, Steam flatpak env
  (`force_vk_vendor=0`, `ANV_CM_HELPER=...`, `VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer,VK_EXT_descriptor_heap`),
  `oxp3-rapl-pp1`, extras.

## Direct-Proton benchmark harness (verified 16:29-16:45)

Removes the launcher and lets the driver be selected with `VK_DRIVER_FILES`:

    P="$HOME/.local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/proton"
    EXE="$HOME/Games/Heroic/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe"
    export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
    export STEAM_COMPAT_DATA_PATH="$HOME/Games/Heroic/Prefixes/Cyberpunk 2077"
    "$P" run "$EXE" -benchmark

`-benchmark` runs automatically, closes the window and writes
`.../drive_c/users/steamuser/Documents/CD Projekt Red/Cyberpunk 2077/benchmarkResults/benchmark_<date>/summary.json`
(averageFps/minFps/maxFps + every setting).

Results (1920x1200, windowed, vsync off, upscaling off, texture Medium):

| launch path | environment | avg FPS |
|---|---|---|
| direct Proton, host driver | - | 47.9 |
| direct Proton + `INTEL_JAY=all` | as Heroic | 48.6 |
| direct Proton + `WINEDEBUG=+fixme DXVK_LOG_LEVEL=info VKD3D_DEBUG=fixme` | as Heroic | 48.9 |
| Heroic Flatpak (its launches 15:37-16:17) | gamemoderun + INTEL_JAY=all | 35.9-41.4 |
| 04:12 Heroic | upscaling type 2 ON | 49.4 |
| 03:14 Heroic | upscaling OFF, before install | 28.9 |

Findings:
- The Mesa stack itself delivers ~48 FPS at these settings; stock gives ~29 (the 03:14 run).
- The Heroic Flatpak path is ~15 % slower than the same Proton launched directly. Remaining difference in
  Heroic's launch line is `--wrapper "/app/bin/gamemoderun <proton> waitforexitandrun"` (per-game
  `useGameMode: true`), which forces the CPU governor to performance - matching the "CPU ~15 W, GPU starved"
  symptom (see /tmp/prof2.log: core 11.9 W + uncore 19.8 W, GPU at 2300 MHz, GameThread ~4.5 cores).
- `INTEL_JAY=all` and the Wine/vkd3d debug logging Heroic sets are measured neutral.
- Last night's 49.4 was recorded with `upscalingType: 2` (upscaling on), not off; the 28.9 baseline was off.

## Tools

- `./state-snapshot.sh` - driver hashes, PL1, profile, kernel module path, temps, flatpak overrides.
- `/tmp/prof2.sh` - instantaneous per-process CPU (delta of /proc stat) + RAPL core/uncore/package + GPU/CPU MHz.
- `/tmp/bench-watch.sh` - waits for a game process then logs the same for 90 s.

## Root cause of the 41 FPS regression (confirmed 2026-11-08 16:43)

Heroic launches Proton wrapped in the Flatpak's `gamemoderun`
(`--wrapper "/app/bin/gamemoderun <proton> waitforexitandrun"`, per-game `useGameMode: true`), which forces the
CPU governor to *performance*. On this 35 W package the CPU then takes ~12-15 W and starves the GPU.

Measured, same settings (1920x1200, windowed, vsync off, upscaling off, texture Medium):

| run | avg FPS |
|---|---|
| Heroic with GameMode ON | 41.2 |
| direct Proton from a terminal | 51.5 |
| Heroic with GameMode OFF | **53.2** |

So the best configuration is: host driver = `install-shiprt` (+ bundled expat), patched kernel modules, PL1 35 W,
and **GameMode disabled in Heroic**. 53.2 beats the stock baseline (28.9) by 84 % and last night's upscaled
49.4 without any upscaling.

For Steam: Steam launches are not wrapped in gamemoderun (no such entry in any `localconfig.vdf` launch
options), so the same CPU-governor penalty does not apply there. The Mesa patches reach Steam games through the
`org.anatase.Platform` Flatpak runtime override, which currently carries `install-shiprt` + the bundled
`libexpat.so.1` (the combination that ran Cyberpunk through Steam at 04:11).

## Gamemode and non-Steam games (2026-11-08 17:1x)

Steam games in Gamemode were fixed by building Mesa with `-Dplatforms=x11,wayland`: gamescope presents through a
Wayland surface, so a driver built with `-Dplatforms=x11` only crashed in `wsi_GetPhysicalDeviceSurfaceSupportKHR`
(DX12 titles crashed, DXVK titles went black) while the desktop XWayland path worked. Runtime override with the
wayland build runs Steam games in Gamemode with good performance.

Non-Steam titles launched from Heroic are a different problem in Gamemode: a non-Steam shortcut brings up the
Heroic flatpak inside Steam's sandbox, stacking Heroic + umu + Steam containers. CPU sits ~15 W and the desktop
numbers (51-53 FPS) are not reached. Launch Proton directly instead:

    tools/cp2077-direct.sh            # add as a Steam non-Steam shortcut
    tools/cp2077-direct.sh -benchmark # unattended benchmark

This keeps the Steam environment (and the runtime driver) but removes the Heroic/umu layer between Steam and
Proton. Undisturbed direct-Proton runs measured 47.9-53.2 FPS at 1920x1200, upscaling off, texture Medium.

## Final reproducible configuration (2026-11-08 18:0x)

Everything the working system needs is now produced by the podman build; no hand-built trees are required.

Build (one command, no sudo):

    ./oxp3-fixes-fast.sh build --skip-gamescope --skip-mangohud --skip-kernel --skip-firmware --skip-xess --skip-extras

Recipe: Mesa main `ea40598`, Anatase overrides, patches `0200 0201 0206 0207 0212`, meson flags
`-Dbuildtype=release -Dprefer_static=true -Dvulkan-drivers=intel -Dgallium-drivers=iris,llvmpipe
-Dplatforms=x11,wayland -Dglx=dri -Dllvm=enabled`.

Why those details matter:
- `platforms=x11,wayland`: gamescope presents through a Wayland surface. With x11 only, every Steam game crashed
  or black-screened in Gamemode at `wsi_GetPhysicalDeviceSurfaceSupportKHR`, while desktop (XWayland) worked.
- `0212` (from the rt-ref worktree, now a proper patch): VRT env gate, RT drirc knobs, TLAS encode subgroup 32,
  morton experiment knobs, AS sort workgroup. This is the content the hand-built `install-shiprt` had, which is
  why podman builds without it stopped at 48.6 while the hand build hit 51.5-53.2.
- `prefer_static` + expat-static: the driver has no libexpat dependency, so no library override is needed in the
  Steam Flatpak runtime and it can't break unrelated sandboxed processes.

`0205`, `0208` and `0211` are subsumed by `0212` (the rt-ref branch contains them) and live in `patches/subsumed/`.

Install (host and Flatpak runtime get the same driver):

    ./oxp3-fixes-fast.sh install --skip-gamescope --skip-mangohud --skip-kernel --skip-firmware --skip-xess --skip-extras
    sudo systemctl restart anatase-flatpak-extensions.service

Verified state: host `5a75b6a44d285fd1…` (2026-11-10 bump), runtime still `4d043cc2…`, no `libexpat.so.1` in the runtime overrides, PL1 35 W,
patched xe module loaded, `platform_profile=balanced`.

Benchmark protocol: single runs of the Cyberpunk benchmark vary by ~10 % (47.9-53.2 today on identical builds), so
quote the summary.json average together with `./state-snapshot.sh`, and compare medians of two runs before
concluding that a build regressed.

## Single-digit FPS on the first run after a driver change is a shader-cache rebuild, not a regression

Every driver build has its own Mesa disk-cache key (and DXVK has its own pipeline state cache). After installing a
different libvulkan_intel.so, the first launch of a title recompiles its shaders; during that window the game shows
single-digit FPS and the CPU sits at 10-20 W while the GPU is mostly idle. Once the caches are warm the same game
runs at full speed with no changes.

Evidence (2026-11-08, Wukong benchmark tool, DX11/DXVK path):
- After each install (5a75b6a4, 4d043cc2): first Gamemode run single-digit FPS.
- A desktop run right after hit full speed (GPU 2300 MHz, core 3.8 W, pkg 25.6 W, GameThread ~300 %).
- The next Gamemode run was fast again.
- ~30,000 files under ~/.cache/mesa_shader_cache were rewritten within the preceding two hours.

So: after any driver install, launch each title once and let it warm, then benchmark. Judge performance only on a
warm run, and quote state-snapshot.sh with the number. Do not re-bisect patches for a cold first run.

## Ray tracing on 26.3.1: the two "not portable" patches are a 12.7% win (2026-11-26)

Measured with the automated harness, no install needed: the RT-patched build was extracted from its RPMs into a
side tree with a private ICD (`tools/gpu-bench/driver-ab.sh`), so the two drivers could be interleaved in one
session. GravityMark 1.89, native Vulkan, headless, warm shader cache, order A B B A. **Resolution correction
(2026-11-26 afternoon):** the harness passed only gamescope's `-W 1280 -H 720`, so GravityMark rendered at its
1600x900 default, with `-times` on (about −8 %). The deltas below are valid; the absolute numbers are 1600x900.
At a true 1280x720 without `-times`, raster is 278 fps (`docs/GPU-PERF-GAPS.md` §4.1).

| workload | installed 26.3.1 (no RT patches) | 26.3.1 + `0205`/`0211` | delta |
|---|---:|---:|---:|
| **ray tracing** (20k asteroids) | 173.1 / 172.7 fps | **194.7 / 195.1 fps** | **+12.7%** |
| raster (50k asteroids) | 219.1 / 219.1 fps | 218.7 / 218.9 fps | −0.14% |

The groups do not overlap and each variant's spread is 0.4 fps. The patches only touch TLAS builds, which is why
raster is unchanged. They now live in `patches/mesa-26.3/` as `0010`/`0011`, globbed by both the host and the
Flatpak GL build, so `./oxp3-install.sh` ships them.

This corrects the earlier claim that `0205`/`0211` were "not portable to 26.3": both apply to `fec8830e` and both
pay off. `0212` (which folded them together with main-only refactors) stays out.

### Two traps this measurement paid for

- **An exported `VK_DRIVER_FILES` outlives the segment that set it.** The first attempt kept it for the whole
  shell, so the "installed" repeat ran the patched driver and the two looked identical (+0.3%). Every variant now
  runs in its own scope, and the installed one runs under `env -u VK_DRIVER_FILES -u LD_LIBRARY_PATH`.
- **The first harness run of a session is a cold run** (shader cache plus thermal state): 176.6 fps RT versus
  194.3 later with no change. Warm both variants, then interleave, then trust the delta, not the absolute.

## Shader compiler: Jay is load-bearing, `INTEL_DEBUG=no-jay` is not a lever

GravityMark RT, same method, both variants warmed:

| backend | runs | mean |
|---|---|---:|
| Jay (default on Xe2/Xe3) | 172.9 | **172.9 fps** |
| brw (`INTEL_DEBUG=no-jay`) | 114.5 / 114.4 | 114.5 fps |

**−33.8% for brw.** There is nothing to choose here; the 7.7% raster gap against Windows is not the compiler
backend.

## `VKD3D_DISABLE_EXTENSIONS` costs nothing on D3D12

XeSS's kernel injection needs the classic descriptor heap, so `VK_EXT_descriptor_buffer`/`VK_EXT_descriptor_heap`
are disabled for every Proton game. Measured, GravityMark D3D12, 1280x720, A B B A:

| setting | runs | mean |
|---|---|---:|
| both disabled (what games get today) | 87.70 / 87.88 | 87.79 fps |
| both allowed | 87.02 / 86.91 | 86.97 fps |

The global setting is **not** costing performance here (it is marginally ahead, inside noise), so there is no
reason to scope it per game or to change the CM injection for descriptor buffers.

## GPU clock floor: a real control, and no performance (2026-11-26, root session)

`tools/gpu-bench/gpu-clock-floor.sh` raised GT0's `min_freq` (450/1200/1800/2300 MHz) and sampled
`act_freq`, `throttle/reasons` and RAPL at 10 Hz during a GravityMark D3D11 run at 1280x720.

| floor | fps | mean MHz (active) | p10 | p90 | gated | throttled | pl2 | pkg W | core W |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 450 (default) | 83.11 | 1845 | 1700 | 2050 | 11% | 87% | 87% | 30.3 | 13.5 |
| 1200 | 83.52 | 1922 | 1750 | 2100 | 11% | 87% | 87% | 30.2 | 13.4 |
| 1800 | 83.56 | 1936 | 1750 | 2100 | 11% | 85% | 85% | 30.2 | 13.4 |
| 2300 | 83.52 | 1968 | 1800 | 2150 | 11% | 83% | 83% | 30.2 | 13.3 |
| `power_saving` | 84.02 | 2053 | 2000 | 2100 | 11% | 81% | 81% | 30.1 | 13.4 |

Three conclusions:

- **`min_freq` is a working control**: the active clock rises 1845 → 1968 MHz with the floor, so the
  write takes effect and SLPC honours it.
- **It buys no frames**: 83.1-84.0 fps across all five runs, a 0.9% spread that includes the noise
  floor. Nothing to ship from a clock floor, and no MangoHud/gamescope work needed here.
- **The limit is power, not clock**: `throttle/reasons` is `pl2` in 81-87% of samples at a constant
  30.1-30.3 W package draw. The device is at its burst-power ceiling, so shifting the clock floor
  only redistributes the same watts.
- `act_freq` reads 0 in ~11% of samples under every floor: that is the GT parking between frames,
  not a low clock, and it explains the earlier "p10 = 0" artefact (the table now separates gated
  samples from active ones).

Consequence: the 1% lows at about half the mean are **not** a clock-ramp problem and cannot be fixed
by a frequency floor. The remaining headroom is power-per-frame efficiency, not more MHz — which is
what the RT TLAS patches delivered (+12.7%) and what a GPU-aware CPU boost would target (~13.4 W of
the 30 W package goes to the CPU while the GPU is `pl2`-throttled; that test is the next candidate
and needs hhd's boost level changed, not another frequency knob).
