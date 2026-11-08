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

### Last-five-seconds capture (XMX, desktop, 2026-11-08 21:1x)

A continuous capture of the final ~5 s of the run (frames 44-59, ~0.33 s apart) was analysed frame by frame:
isolated-white-speckle counts were 0-2 per frame; no bright clusters on foliage or building edges. Static and burst
captures on the desktop therefore do not reproduce the artifact.

The artifact is reported by the user as obvious while *running* the game, and the Gamemode path is how it is normally
played. `tools/cp2077-direct.sh` now sources `~/.config/xess-xmx/oxp3-env.sh` when `OXP3_XMX=1`, and the
`heroic-launch.sh` Cyberpunk shortcut sets that, so Gamemode launches use the CM driver + shim as well. Next
discriminator: a screenshot taken on the Gamemode screen at the moment the dots appear (composited desktop captures
stay clean), which distinguishes a gamescope/WSI presentation artifact from an upscaler-data artifact.

## RESOLVED: artifacts were our own driver build

A/B with the project's prebuilt release driver (`xess-xmx-linux-20261107`, Mesa 26.3.0-rc3 + its CM series) shows
**no artifacts**, while every driver we built ourselves from the same series (Mesa main + our patches, main + CM only,
26.3 + CM from our local checkout) shows the white dots on trees/building edges with both XMX and DP4a. The upscaler,
the SDK 3.0.2 libraries, vkd3d flags, sharpness and quality preset were all eliminated as causes by lever tests
(descriptor-heap disable, `no_upload_hvv`, `single_queue`, sharpness 0/0.5, XeSS Performance/Auto - artifacts in all).

Conclusion: **ship the prebuilt release driver for XMX** rather than our podman build. Our local checkout of the
project predates the release (20261107), so the release contains newer CM fixes than the patch set we built from.

Configuration now live (known-good):
- `~/.config/xess-xmx/icd-xmx.json` -> `~/.local/share/xess-xmx-release/xess-xmx-linux-20261107/lib/mesa-26.3.0-rc3/libvulkan_intel.so`
- `~/.config/xess-xmx/oxp3-env.sh` and Heroic envVars -> the release's `kernels/`, `tools/cm-compile.sh`, `igdext64.dll`
  (`INTC_ALT_DRIVER_EXTENSIONS_PATH`), plus `VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer` and `force_vk_vendor=0`.
- Measured on the release driver: 86.0 avg / 70.7 min with the XeSS Performance preset; 49.5/42.4 in the other
  (XeSS quality Auto, heavier) run.

`tools/build-xmx-driver.sh` remains for reproducing our own build, but it is not the shipping driver while it shows
the artifact; update the local checkout to the release's patch revision before using it again.

## Known-good configuration (working, verified)

- Driver: the project's prebuilt release `20261107`, Mesa **26.3.0-rc3** + its six CM patches
  (`~/.local/share/xess-xmx-release/xess-xmx-linux-20261107/lib/mesa-26.3.0-rc3/libvulkan_intel.so`).
- `VK_DRIVER_FILES` -> an ICD json pointing at that binary; `ANV_CM_KERNEL_DIR`/`ANV_CM_HELPER` -> the release's
  `kernels/` and `tools/cm-compile.sh`; shim via `INTC_ALT_DRIVER_EXTENSIONS_PATH` -> the release directory.
- **`XMX_IGC_DIR` is required** (`~/.local/share/xess-xmx/igc`): the release archive contains no `neo/bin/ocloc`
  compiler runtime. Without it every CM compile fails (`cm-compile: ocloc not found`), XeSS aborts at init and the
  game silently flips upscaling back to Off - this exact failure cost an hour; `tools/xmx-enable.sh` now refuses to
  run without a working ocloc.
- Game needs Intel SDK XeSS libraries in `bin/x64` (Cyberpunk ships 2.0.1.41; keep `bin/x64/.xmx-orig` backup).

### Our own builds - still not usable for XMX

- main + ported six-patch CM: game aborts XeSS init (no shim trace, no summary), independent of IGC.
- rc2 + the six-patch CM + our 26.3 backports + overrides (`mesa-merged`, sha `6536a067`): same failure.
- The release binary works; both are patch-compatible on paper, so the difference is inside the release's patch
  revision/compiler interface, not our build flags. Until that is understood, the release driver is the XMX driver and
  our patched driver remains the system default for every non-XeSS game.

## FINAL WORKING CONFIGURATION (verified clean + fast)

Cyberpunk 2077, XeSS on (Auto), 1920x1200: **no artifacts, performance restored** after switching the kernel compiler
to IGC 2.10.10.

Three distinct faults were found and fixed, in this order:

1. **Missing CM patches.** Our local checkout predated the release: it had five patches, the release has six. The
   missing ones are `0005-anv-cm-sampler-descriptor-table` (white speckles on foliage/edges) and
   `0006-anv-cmat-vars-before-jay`.
2. **Wrong kernel compiler.** Our checkout's IGC was `ocloc 25.18.1` with a newer `libigc`; the release pins IGC
   **2.10.10** ("newer IGC releases fail on some XeSS kernels"). Kernels compiled with the newer compiler both
   artifacted and ran slower - that was the apparent "performance regression".
3. **No compiler at all in the release archive.** `XMX_IGC_DIR` (or `<package>/igc`) must contain
   `igc/igc` (2.10.10) and `neo/bin/ocloc`; `tools/get-igc.sh` fetches them (~95 MB). Without it every CM compile
   fails, XeSS aborts and the game silently resets upscaling to Off.

Shipping configuration:

    driver:   ~/.local/share/xess-xmx-release/xess-xmx-linux-20261107/lib/mesa-26.3.0-rc3/libvulkan_intel.so
              (Mesa 26.3.0-rc3 + the release's six CM patches)
    compiler: ~/.local/share/xess-xmx-release/xess-xmx-linux-20261107/igc  (IGC 2.10.10 + ocloc 25.18.1)
    shim:     the release directory via INTC_ALT_DRIVER_EXTENSIONS_PATH
    env:      ~/.config/xess-xmx/oxp3-env.sh, Heroic per-game envVars, cp2077-direct.sh (OXP3_XMX=1)
    game:     Cyberpunk's bin/x64 carries Intel SDK 3.0.2 XeSS libs (originals in bin/x64/.xmx-orig)

Everything else stays on our patched driver (system default); only XeSS titles use the release driver.

Our own builds (`mesa-xmx`, `mesa-merged`, main port) are NOT used for XMX: they still fail XeSS initialisation. The
release binary is built from the project branch, which carries CM changes beyond the patched tarball's patch set;
reproducing it needs that branch, not our patch port.

## Proof that the shipped release binary cannot be rebuilt from published sources

Bisect and rebuild results (all measured on Cyberpunk, XeSS on, IGC 2.10.10):

| build | source | patches | flags | sha256 | result |
|---|---|---|---|---|---|
| release binary | — | — | their CI | (shipped) | clean, 74.2 FPS |
| `mesa-rc3` | rc3 git archive | CM6 + 5 backports + overrides | /usr/local, prefer_static | `3f5216e7` | artifacts, 74.5 |
| `mesa-no0004` | rc3 git archive | CM6 + 4 backports | /usr/local | `14dfaf3a` | artifacts, 68.0 |
| `mesa-only0001` | rc3 git archive | CM6 + 0001 | /usr/local | `dcc0f2c4` | artifacts, 67.6 |
| `mesa-drc` | rc3 git archive | CM6 + 5 backports + overrides | release-exact (prefix=/usr, no GL, intel-rt) | `4b1d55ea` | artifacts, 67.7 |
| `mesa-ctl` | rc3 git archive | CM6 + overrides, no backports | release-exact | `b775803a` | artifacts, 67.9 |
| `mesa-pure` | rc3 git archive | CM6 only | release-exact | `b775803a` | artifacts (identical binary to control) |
| `mesa-rel` | **rc3 release tarball** | CM6 + 5 backports + overrides | release-exact | `4b1d55ea` | (identical binary to `mesa-drc`) |

Conclusions:
- The source tarball is irrelevant: the git archive and the official release tarball produce the **same binary hash** (`4b1d55ea`).
- The backports and the Anatase overrides are irrelevant: removing them changes nothing (`b775803a` control == pure build);
  the overrides only touch DRI3/radv files and a Containerfile.
- Mesa's drirc path was a real but secondary defect (fixed: `DRIRC_OK`), it did not cure the artifact.
- Therefore the release binary contains CM code that is **not** in `patches/` in the release archive: rebuilding from
  the published patch series cannot reproduce it. This is the definitive reason our own builds cannot be used for XMX.

Shipping state: the release binary is the XMX driver, wired through `~/.config/xess-xmx/oxp3-env.sh`, the ICD json and
Heroic envVars (IGC 2.10.10 required). Our patched driver remains the system default for all non-XeSS titles.

## Source/toolchain reproduction attempts (all artifact)

Release `20261107` maps to commit `96958b8` on `main`; its `patches/mesa-26.3/` are byte-identical to the release
archive's. The release driver is built in `archlinux:latest` pinned to the Arch Linux Archive snapshot `2026/10/14`.

| build | toolchain | `CMK2H` marker | size | result |
|---|---|---|---|---|
| shipped release binary | arch snapshot | present | 29,934,512 | clean, 74.2 |
| our Fedora 45 build (`4b1d55ea`/`b775803a`) | gcc 16.2.1, Fedora 45 | **absent** | 29,714,032-29,714,040 | artifacts |
| our Arch-snapshot build (`b354bd5a`) | same Arch snapshot + package list + meson flags as CI | **present** | 29,925,304 | **artifacts** |

Conclusions:
- The Fedora-vs-Arch toolchain difference is real (it changes the binary and introduces the `CMK2H` string), so the
  toolchain does matter, but matching it does **not** cure the artifact.
- With identical source commit, identical patch bytes, identical meson flags and the same pinned Arch toolchain, our
  binary still differs from the shipped one (29,925,304 vs 29,934,512 bytes) and still artifacts.
- Therefore the shipped binary was produced from a revision that is **not** the tagged source (an unreleased/CI-only
  branch state); no public recipe reproduces it.

Consequence: the only remaining route to "our driver with working XMX" is binary-level reverse engineering of the
release driver's CM functions (goal #2), or upstream providing the exact build revision. Working configuration is the
release binary (restored), our patched driver remains default elsewhere.

## Binary diff result: release and our builds are the same program

- Symbol sets: 20,737 defined symbols each, identical apart from one local jump-table constant.
- All 75 CM-related functions (`anv_cm_*`, `cmk*`) have **identical instruction counts** in the release binary and in our
  Arch-toolchain build; the instruction differences are constants derived from the build's source path strings, plus the
  `CMK2H` data marker.
- Linkage differs only by `libdisplay-info.so.3` (monitor metadata for WSI) - unrelated to XeSS compute.

Therefore no CM code delta exists between the shipped driver and ours: the earlier "artifacts only with our builds"
pattern was not a code difference.

## Control experiment that closes the loop

Release driver, kernel cache deleted, same environment: the run shows a **slight** residual shimmer on foliage
("barely noticeable but there"), while the same binary with the earlier warm cache was categorically clean.

Conclusion: output quality varies with the freshly compiled CM kernels (IGC 2.10.10 -> .cmk), not with the Mesa driver
binary. The earlier severe speckles cost us the old warm cache; on a fresh compile both drivers show the same faint
residual. Chasing a driver-code fix was therefore the wrong axis.

Practical consequence: the release driver remains the XMX driver (best performance, 74.2 avg / 58.1 min at XeSS Auto,
86 avg at Performance), our patched driver remains default for all non-XeSS games, and no further gain is available
from owning the driver build - the code is equivalent.

## ROOT CAUSE FOUND: `-doubleGRF` kernel codegen (and the fix is also faster)

The CM kernels are compiled with whatever ocloc flags XeSS passes. When `-doubleGRF` is honoured, the kernels use the
256-register codegen on Xe3 and produced the white speckles; the driver's own 128-GRF path is correct.

Fix: filter `-doubleGRF` out in the CM compiler helper (`tools/xmx-cm-compile-nodgrf.sh`, a copy of the release
`cm-compile.sh` with one line changed), then recompile the kernels (clear `~/.cache/xess-xmx/kernels`).

Measured (Cyberpunk, XeSS Auto, IGC 2.10.10, fresh kernel cache):

| driver | kernels | artifacts | avg | min |
|---|---|---|---|---|
| release binary | `-doubleGRF` | faint residual | 74.2 | 58.1 |
| release binary | **no `-doubleGRF`** | **none** | **76.5** | **62.1** |
| our podman build from the release commit (`b354bd5a`) | **no `-doubleGRF`** | **none** | see summary | |

So the artifact was never our Mesa code (proved equivalent to the release binary by symbol/instruction comparison); it
was the 256-GRF CM kernel codegen, and disabling it is a performance win as well.

Shipping configuration (all wired):
- driver: our podman build `~/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so` (from the release commit's sources)
- compiler: `tools/xmx-cm-compile-nodgrf.sh` (128-GRF)
- IGC: `~/.local/share/xess-xmx-release/.../igc` (2.10.10), shim via `INTC_ALT`
- env: `~/.config/xess-xmx/oxp3-env.sh`, Heroic envVars, `cp2077-direct.sh` (OXP3_XMX=1)
