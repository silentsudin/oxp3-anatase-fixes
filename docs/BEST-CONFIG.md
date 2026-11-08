# Best-known configuration and benchmark harness (2026-11-08)

This file exists because the best-measured configuration was nearly lost. Whenever a benchmark number is
quoted, capture `./state-snapshot.sh` (driver hashes, PL1, profile, kernel module, temps) with it.

## Best known state (~04:10-04:14 on 2026-11-08)

- Driver: `mesa-build/install-shiprt/usr/local` (Mesa 27.0.0-deve; main + 0205 HPLOC + 0211 with the tuning
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
