# XeSS XMX (DPAS) on our Mesa driver - state and results

## What it is

XeSS' XMX path needs two things that stock Wine/Proton + Mesa do not provide:
1. a C-for-Metal (CM) kernel injection path in the ANV Vulkan driver, which compiles the kernels XeSS submits with
   Intel's IGC and runs them on the XMX units, and
2. `igdext64.dll`, the shim from https://github.com/HHHHanasak1/xess-xmx-linux that answers XeSS' driver queries.

The project provides both: a Mesa ANV patch series and the shim/compiler tooling. It was developed on this exact
machine (ONEXPLAYER 3, Arc B390).

## What we built

`tools/build-xmx-driver.sh` builds **Mesa 26.3 + the upstream CM patch series**
(`~/.local/share/xess-xmx/patches/mesa-26.3/0001-0005`) with our usual flags: release, `prefer_static` (no
libexpat dependency), `-Dplatforms=x11,wayland`, in podman. Output:

    ~/.cache/oxp3-fixes/mesa-xmx/usr/local/lib64/libvulkan_intel.so   (sha256 26b71d99...)

This is a **separate driver from the production one**: our main/tree driver (ec418f04) does not carry the CM
patches. It is selected per game via `VK_DRIVER_FILES` (an ICD json), so other games are untouched.

The shim is *not* installed into Proton. It is found through `INTC_ALT_DRIVER_EXTENSIONS_PATH` (supported by
XeSS 2.0.2.68), which avoids modifying Proton files. (Proton's own igdext mechanism lives in
`…/files/lib/wine/igdext/x86_64-windows/igdext64.dll` and is deployed into a prefix's DriverStore; Proton-CachyOS
does not ship it, and copying a DLL into the prefix is *not* enough - XeSS does not find it there.)

## Environment (set for the game)

    VK_DRIVER_FILES=~/.config/xess-xmx/icd-xmx.json           # the CM driver
    ANV_CM_KERNEL_DIR=~/.local/share/xess-xmx/kernels
    ANV_CM_HELPER=~/.local/share/xess-xmx/tools/cm-compile.sh
    VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer          # kernel binding needs the classic heap
    force_vk_vendor=0                                          # do not hide the Intel GPU from the game
    INTC_ALT_DRIVER_EXTENSIONS_PATH=~/.local/share/xess-xmx    # where XeSS finds igdext64.dll

Cyberpunk 2077 ships XeSS **2.0.1.41** (older than 2.0.2.68); such games are shown an AMD vendor id by DXVK's
workaround, so XeSS never loads the shim and runs DP4a. The game therefore needs Intel's SDK 3.0.2 libraries
(`xess/libxess*.dll` copied into `bin/x64`, originals kept in `bin/x64/.xmx-orig`). This is what the project's
`XESS=latest` does.

`tools/xmx-enable.sh enable --game <bin dir> --prefix <drive_c>` performs the library swap, writes
`~/.config/xess-xmx/oxp3-env.sh` and the ICD json; `disable` reverts. `tools/xmx-run.sh -- <cmd>` sources that env.

## Measured (Cyberpunk 2077 benchmark, 1920x1200, XeSS on, same driver, same prefix)

| path | avg FPS | min FPS | shim trace |
|---|---|---|---|
| XMX (`INTC_ALT` + CM driver) | **65.9** | **55.6** | 562 lines |
| DP4a (`IGDEXT_FORCE_FALLBACK=1`) | 63.7 | 54.6 | 0 |
| XeSS off (production driver, same session) | 49.8 | 43.3 | - |

XMX is +3.5 % over DP4a and +32 % over no upscaling. First run compiles the game's kernels (5 kernels, seconds;
`~/.cache/xess-xmx`), afterwards they load from cache. Kernel list: `~/.local/share/xess-xmx/kernels`; compile log
`~/.cache/xess-xmx/compile.log` (no failures observed). New kernels compile with `-doubleGRF` on this hardware.

## Artifacts: white dots on the trees in the final scene

Observed with XeSS on. Not observed with upscaling off. Root cause of the reproduction we chased: the game kept the
**SDK 3.0.2 libraries while the XMX environment was absent** (normal Heroic launch), i.e. DP4a with the new library
set - a mixed configuration. With the full XMX environment active, 200+ captured frames (burst capture, two runs,
XMX and DP4a) showed no speckles; the 4 diagnostics in the project's `docs/debugging.md` (a kernel failing to compile
and running as a no-op) do not apply either (`compile.log` has no failures).

To make normal launches use the same configuration, the environment is now set in Heroic's per-game envVars for
Cyberpunk (`~/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig/1423049311.json`), so a normal launch
runs XMX like our test runs. If artifacts reappear there, compare against a launch with
`IGDEXT_FORCE_FALLBACK=1` added, and check `compile.log` for a failed compile.

## Status / open items

- Driver built and verified loading (vkcube selects the B390 on the CM driver).
- XMX engaged and measured faster; kernels compile and cache.
- Heroic env wired; needs one normal-launch confirmation from the user that artifacts are gone.
- Frame generation (`XESS_FG=n`) needs `ControlLib.dll`; not tested yet.
- Flatpak Steam is not supported by the project; Steam games would need the env in the Flatpak override and the shim
  reachable from the sandbox.

## Update: CM series ported to our Mesa main build

The CM patch series no longer lives only in the 26.3 build. `patches/xess-main/0101-anv-cm-kernel-injection-for-main-27.patch`
is the upstream 26.3 series rebased onto main `ea40598`: the upstream patches apply with exactly one context-drift
reject (`anv_shader_compile.c`, where main refactored the branch into `use_jay`), fixed by placing the CM result check
before it:

    const enum anv_cm_result cm_result =
       shader_data->info->stage == MESA_SHADER_COMPUTE ?
       anv_cm_kernel_replace(device, mem_ctx, shader_data, nir) : ANV_CM_NOT_PLACEHOLDER;
    ...
    if (cm_result == ANV_CM_INJECTED) { params.error_str = NULL; }
    else if (use_jay) { ... }

`tools/build-xmx-driver.sh` now builds: Mesa main + Anatase overrides + our patches (0200 0201 0206 0207 0212) +
the ported CM patch, in podman with `-Dprefer_static=true` and `-Dplatforms=x11,wayland`. Result:
sha256 `8bcd5bfcae9eab22…` (`CM` hooks present, no libexpat dependency). The previous 26.3-based CM driver is kept at
`~/.cache/oxp3-fixes/mesa-xmx-263`.

Measured with the main-based CM driver (same prefix/proton/env as before):

| path | avg FPS | min FPS | shim |
|---|---|---|---|
| XMX | 65.8 | 54.1 | 562 trace lines |
| DP4a | 67.5 | 55.9 | none |

So XMX is engaged on main as well; on this driver the advantage over DP4a is within run-to-run noise (earlier 26.3
build: 65.9 vs 63.7).

## Open: white-dot artifacts with XeSS at the end of the benchmark

Still unresolved and reported by the user as obvious on trees and building edges at the end of the run, with **both**
XMX and DP4a, but **not** with upscaling off. Static screenshot captures of that scene (several hundred frames,
including 1-3 fps bursts of the final seconds) have not reproduced it, and `compile.log` shows no failed kernels, so
it is likely temporal (motion/upscaler history) rather than a persistently wrong frame.

Next step is ground truth from the running screen: a screenshot taken by the user at the moment the dots are visible
(PrintScreen/Spectacle while the benchmark shows them), plus one run with XeSS quality set to Performance to see
whether it depends on the upscale ratio.
