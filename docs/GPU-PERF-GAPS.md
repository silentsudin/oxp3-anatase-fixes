# GPU driver-stack performance: gaps, unpulled levers, next work (2026-11-26)

Handoff brief for an agent continuing GPU performance work on the OneXPlayer 3 (Panther Lake,
Arc B390 iGPU, Xe3, `xe` kernel driver, Mesa ANV). Each task below says what to do, why, how to
measure it, what counts as success, and whether it needs root. Work top to bottom unless the user
picks something else.

**Read section 4 first.** It is the later round (2026-11-26 afternoon: Windows driver comparison,
measurement corrections). Where it contradicts a number in sections 1-2, section 4 is right: the
native-Vulkan "1280x720" figures before it were rendered at 1600x900.

## 0. Rules you must follow

1. **End users run only `./oxp3-install.sh`.** Anything you ship must be built and installed by
   that script. Never hand the user `sudo …` one-liners or `source` steps for a shipped change.
2. **Do not make the user run repeated test cycles.** Mine existing logs and results first
   (`~/.cache/oxp3-fixes/gpu-bench/results/`, the journal). If a measurement needs root or a reboot,
   batch every such measurement into **one** script/session and ask once.
3. **Measure before you claim.** The noise floor of the GravityMark harness is about ±0.6 % per
   session (see `docs/DX11-PERFORMANCE.md` Result 5). Use interleaved A/B order (A B B A), keep a
   baseline at the start and the end, and discard the first run after any driver change (it
   rebuilds the shader cache: `docs/BEST-CONFIG.md`, last section).
4. Commit only when the user asks. Never commit `tools/game-fps.sh` (the user's file). Never put
   the user's e-mail address or name into anything that leaves the machine.
5. There is no passwordless sudo in agent sessions. Plan for that (rule 2).

## 1. What ships today

| layer | what | where |
|---|---|---|
| Mesa | **26.3.1** (`MESA_COMMIT=fec8830e…`, `oxp3-fixes.sh:103`), one build for host x86_64/i686 and the Flatpak GL extension | `oxp3-fixes.sh` `build_mesa`, `tools/lib/install-common.sh:1686-1860` |
| Mesa patches applied | `patches/mesa/0200` (rate-limit queue-ban query), `0201` (fence batch resets), `0206` (mesh URB full-slot writes); all of `patches/mesa-26.3/` (C-for-Metal kernel injection for XeSS XMX 0001-0005, coopmat var lowering 0006, Jay helper predication 0007, mesh compact outputs 0207, +17 % GravityMark mesh); all of `patches/mesa-26.3-backports/` (5 upstream commits) | globs at `oxp3-fixes.sh:118-120,466` and `install-common.sh:1751-1756,1809-1811` |
| Mesa patches **not** applied | `patches/mesa/0212` (RT TLAS tuning, main-only), `0213`/`0214` (coopmat 16x8x16), `0215` (iris fp16 for rusticl); `patches/subsumed/0205`, `0208`, `0211`; `patches/rejected/0209`, `0210` | `docs/xess-xmx.md:23`, `docs/BEST-CONFIG.md:121` |
| Kernel `xe` | 7.3.4-an02 plus upstream stable fixes (`patches/kernel/0101-0161`): correctness, shrinker, display. **No** frequency, GuC SLPC or power-sharing tuning | `patches/kernel/` |
| Firmware | GuC 70.72.1, HuC 10.3.3, GSC 105.0.2.1397 (from the boot log) | — |
| Runtime env | `~/.config/environment.d/50-xess-xmx.conf`: `VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer,VK_EXT_descriptor_heap,VK_KHR_present_wait2`, `force_vk_vendor=0`, `CM_MAX_GRF=128`, `ANV_CM_HELPER=…` | `tools/xmx-write-env.sh`, `docs/xess-xmx.md` |
| GPU clocks | gt0 (render) 450-2300 MHz, `power_profile=base`, no floor raised in games | `/sys/class/drm/card1/device/tile0/gt0/freq0/` |
| Memory (SAGV) | firmware-chosen QGV point; Linux exposes only the boot param `xe.enable_sagv` (can only disable) | `docs/hw/sagv.md` |

Current measurements (GravityMark 1.89, 1280x720, raster, Mesa 26.3.1, 2026-11-24,
`~/.cache/oxp3-fixes/gpu-bench/results/`): D3D11 86-96 fps, D3D12 89-91 fps, **1 % lows 45-52 fps in
every run, under every CPU scheduler** (about half the mean). There is **no RT, mesh or native-Vulkan
number for the 26.3.1 build**, and no Windows comparison for it.

## 2. Tasks, ranked by value / effort

### T1. Ship the ray-tracing patches - **measured +12.7 % RT, shipped (2026-11-26)**

`0205` and `0211` apply to `fec8830e` and measured 173.0 -> 194.9 fps RT (A B B A, warm, no overlap)
with raster unchanged at -0.14 %. They now ship as `patches/mesa-26.3/0010` and `0011`; `0212` stays
out. Table: `docs/BEST-CONFIG.md`.

The original brief follows.

**Why.** The RT gains were measured on Mesa main: `0205` HPLOC for TLAS builds, **+6-13 % RT**, and
`0211` TLAS encode at subgroup 32 with a larger morton workgroup, **+1.55 % RT**. They were folded
into `0212`, and `0212` was declared "not portable to 26.3" because of main-only refactors. But
**`0205` and `0211` each apply cleanly to the 26.3.1 source** (`git apply --check` against the
`fec8830e` tarball, 2026-11-26). So the shipped driver carries none of the RT work. Last recorded
figures: Windows 187.7 fps RT, Linux without the patches 149.4, with them about 180 (commit `0ec5246`).

**Steps.**
1. Baseline first (T2 step 1): native-Vulkan RT on the current build:
   `python3 tools/gpu-bench/gpu-bench.py --quick --gravitymark --gravitymark-rt --tag base-263`.
2. Copy `patches/subsumed/0205-anv-use-hploc-for-tlas-builds.patch` and
   `patches/subsumed/0211-anv-tune-tlas-build-encode-subgroup-and-sort-workgroup.patch` into
   `patches/mesa-26.3/` with new numbers that sort after `0007` (e.g. `0010-…`, `0011-…`). That
   directory is globbed by every build list and cache key, so no script edit is needed. Do **not**
   take `0208` (debug env, does not apply) or `0212`.
3. Build: `./oxp3-fixes.sh build` (user session, no root), install through the user's normal
   `./oxp3-install.sh` run, then run step 1 again with `--tag rt-263`.
4. Correctness: one CTS slice for acceleration structures if the harness has it
   (`gpu-bench.py --cts`), and a visual check in an RT game.
5. Optional: re-measure `patches/rejected/0210` (+1.4 % without HPLOC, −1.87 % with HPLOC on main)
   on top of 0205/0211. Ship it only if it is a measured win on 26.3.1.

**Success.** RT fps up by more than the noise floor, no CTS regressions. Document it in
`docs/BEST-CONFIG.md` and fix the README (T2).

### T2. Re-baseline the shipped build and fix the stale README claims (high value, small, no root)

**Why.** `README.md` (Performance section) still says "Mesa main + performance patches", "≈268
raster / ≈180 RT" and "Gaming CPU scheduler (`scx_cosmos`)". All three describe an earlier build:
Mesa is 26.3.1, the RT patches are not in it, and the scheduler is EEVDF (commit `cdd2866`).

**Steps.**
1. `python3 tools/gpu-bench/gpu-bench.py --quick --gravitymark --gravitymark-rt --tag baseline-263`
   (native Vulkan raster + RT, 50k asteroids, 1280x720), twice with a gap.
2. `python3 tools/gpu-bench/gravitymark.py matrix …` for d3d11/d3d12/vk-win (see `--help`).
3. Update the README figures with the date and the stack (`result.json` → `stack`), and keep the
   Windows reference figures labelled as Windows.

### T3. Jay vs brw shader compiler A/B (medium-high value, tiny effort, no root)

**Why.** Mesa 26.3.1 uses the new **Jay** backend by default on Xe2/Xe3
(`src/intel/dev/intel_debug.c:304-325`). `INTEL_DEBUG=no-jay` selects the older brw backend. Nobody
has compared them here. Raster is still about 7.7 % behind Windows, and nothing has explained that.

**Steps.** For raster, RT and one real game, run A/B/B/A with and without `INTEL_DEBUG=no-jay`
(gravitymark.py: `--env INTEL_DEBUG=no-jay`). Clear or separate the shader cache between
variants: the cache key includes the backend, but the first run of each variant compiles. If brw
wins a workload, report per-stage (`INTEL_JAY=fs,vs,…` can force Jay per stage on supported
platforms).

**Success.** A table per workload. A shippable lever only if one backend wins consistently. Then a
driconf/env default, applied through the installer's environment (`tools/xmx-write-env.sh`
writes `50-xess-xmx.conf`).

### T4. Measure the cost of `VKD3D_DISABLE_EXTENSIONS` on every D3D12 game (medium value, small, no root)

**Why.** XeSS's C-for-Metal kernel injection needs the classic descriptor heap, so
`VK_EXT_descriptor_buffer` and `VK_EXT_descriptor_heap` are disabled for **every** Proton game
through `environment.d`, including the many D3D12 games that never use XeSS. The cost was never
measured.

**Steps.** `gravitymark.py run --api d3d12` A/B with `--env VKD3D_DISABLE_EXTENSIONS=` (empty, so
both extensions are allowed) against the default. Then one real D3D12 game with `game-limit-capture.py`.

**Success / follow-up.** If the cost is above noise, scope the variable to XeSS games only. The
installer already marks XMX games (`tools/xmx-steam-enable.sh`, launch options). The alternative is
to make the CM injection (`patches/mesa-26.3/0001`) handle descriptor buffers.

### T5. GPU frequency floor and the 1 % lows - **measured, negative (2026-11-26)**

Result (`tools/gpu-bench/gpu-clock-floor.sh`, table in `docs/BEST-CONFIG.md`): `min_freq` at
450/1200/1800/2300 MHz raised the active clock 1845 -> 1968 MHz but moved fps only 83.1 -> 83.6
(and `power_saving` 84.0), inside the noise floor. `throttle/reasons` is `pl2` in 81-87 % of samples
at a constant 30.2 W package draw, so the device is power-limited, not clock-limited. Do not ship a
clock floor, and do not re-open this without a change to the power limit itself.

The original brief follows.

**Why.** Every recorded run has 1 % lows at about half the mean, under all eight CPU schedulers, so
it is not CPU scheduling. GT0 idles at 450 MHz with SLPC choosing the clock. A ramp delay after
light frames is a classic cause of iGPU frame-time spikes. `docs/PERF-OPTIMIZATION-LOOP.md`
"Blocked by privileges" lists "a higher GPU minimum frequency" as a candidate that was never run.

**Steps (one root session).**
1. Write a script that, for each floor in {450 (default), 1200, 1800, 2300}: writes
   `/sys/class/drm/card1/device/tile0/gt0/freq0/min_freq`, runs gravitymark d3d11 1280x720, and
   samples `act_freq` and `throttle/reasons` at 10 Hz. Restore 450 at the end and on every exit
   path (trap).
2. The same for `power_profile` `base` vs `power_saving` (sanity check; expect `base` to win).
3. Compare mean, 1 % low and package power (`/run/rapl-power`, see `files/rapl-power`).

**Success / follow-up.** If a floor lifts the 1 % lows, ship it as a game-time floor next to hhd's
Smart CPU boost (`patches/hhd`, commit `7c98a01`). It already runs as root and knows when a game
runs. Install it through the installer.

### T6. CPU ↔ GPU power sharing under Smart CPU boost (medium value, small, root via hhd)

**Why.** The package shares one PL1/PL2 budget. Smart CPU boost raises core clocks (level 2: P-cores
4.7 GHz, E-cores 3.6 GHz) when a game thread is busy. In a GPU-bound game, that power may come out of
the GPU's share. GT throttle reasons are exported (`gt0/freq0/throttle/reason_pl1`, and kernel patch
0119 reports power brake). Nobody has correlated them with fps.

**Steps.** In one GPU-bound title at a fixed spot, sample `act_freq`, `throttle/reasons`, package
and core RAPL, with the boost capped at level 0 vs left on auto (hhd setting). Use
`tools/game-limit-capture.py --exe <game.exe>` for the samples.

**Success.** If GT shows `reason_pl1` while the boost is high and fps drops, make the boost
GPU-aware: don't raise the CPU when GT is power-throttled and the CPU isn't the limiter.

### T7. SAGV / memory frequency in games (potentially large, medium effort, **two reboots**)

**Why.** iGPU performance is usually memory-bandwidth bound. `docs/hw/sagv.md` §5.1 rules out a
gaming effect with reasoning ("in a game the point is already pinned high by display bandwidth
demand"), not a measurement. Windows' performance-mode tool pins the SAGV point (MCHBAR
0x5DA0/0x5DA4), which suggests the dynamic point is not always high in games.

**Steps.** One prepared script, run by the user once per boot:
1. Boot A (default): GravityMark raster + RT + one game spot, plus `dram`/`psys` power from
   `/run/rapl-power` (`tools/oxp3-sagv.sh measure`).
2. `sudo rpm-ostree kargs --append-if-missing=xe.enable_sagv=0`, reboot.
3. Boot B: the same runs.
4. `sudo rpm-ostree kargs --delete-if-present=xe.enable_sagv=0`, reboot.

**Success / follow-up.** If boot B is faster, the cheapest shippable lever is an opt-in "performance
memory" kernel argument applied by the installer (with a clear battery cost). The real fix is the
`intel_pmdemand` work already drafted (`docs/HANDOVER.md` §6 row 7).

### T8. Upstream backlog scans (medium value, small-medium, no root)

1. **Mesa.** List `src/intel` and `src/compiler/nir` commits on `main` since the 26.3 branch point
   with performance in the subject or body (Xe2/Xe3, Jay, ANV). Only 5 are backported now
   (`patches/mesa-26.3-backports/`). Try each candidate with `git apply --check` against
   `fec8830e`, then A/B the ones that apply.
2. **Kernel.** Scan `drm-xe-next` / `drm-intel-next` after 7.3.4 for PTL performance and frequency
   commits (GuC SLPC, TLB invalidation, page tables, LRC). Check linux-firmware for a newer
   `xe/ptl_guc_70.bin` than 70.72.1.
3. **DXVK.** The remaining 7.6 % D3D11 deficit is inside DXVK (`docs/DX11-PERFORMANCE.md`
   Conclusion). Nothing local recovers it. The deliverable is an upstream report with that doc's
   evidence, not a patch.

### T9. Gamescope composition cost (low-medium value, medium effort, no root)

**Why.** HDR LUTs, the vibrance/look LUTs and scaling force gamescope to composite each frame
instead of scanning the game's buffer out directly. That composite is GPU time taken from the game
on a shared iGPU. Never measured.

**Steps.** In one game, compare fps with the colour features at default vs off (gamescope convars).
Check whether direct scanout happens: gamescope logs and `fdinfo` engine time per process. Report
the ms per frame spent in gamescope.

### T10. Compatibility-only patches (low performance value)

- `0213`/`0214` (cooperative matrix 16x8x16 f16): apply cleanly to 26.3.1. They add the shape that
  Longshot needs. The measured matrix throughput is unchanged (`docs/PERF-OPTIMIZATION-LOOP.md`).
  Ship them only if a title needs the shape.
- `0215` (iris fp16 for rusticl): skip. NEO is already the faster OpenCL path
  (`docs/PERF-OPTIMIZATION-LOOP.md` "Accepted: NEO").

## 3. How to build, measure and ship

- **Build.** `./oxp3-fixes.sh build` (user, hash-cached: a changed patch rebuilds Mesa). The user's
  `./oxp3-install.sh` installs it. Patches in `patches/mesa-26.3/` and
  `patches/mesa-26.3-backports/` are picked up automatically. `patches/mesa/` is **not** globbed;
  only 0200/0201/0206 are named there.
- **Harness.** `tools/gpu-bench/gravitymark.py` (`run`, `matrix`, `report`; `--env K=V`, `--label`;
  results in `~/.cache/oxp3-fixes/gpu-bench/results/<run>/<label>/result.json`, with the stack
  recorded). `tools/gpu-bench/gpu-bench.py --gravitymark --gravitymark-rt` gives native-Vulkan
  raster and RT. `tools/game-limit-capture.py` captures a real game.
- **Traps** (`docs/DX11-PERFORMANCE.md` last section): a killed run wedges the Wine prefix
  (`--fresh-prefix`), and an orphaned client renders at 100 % and pollutes later runs. The first run
  after a driver change is a shader-cache rebuild. MangoHud writes its log only on a clean exit.
- **Docs.** Record results in `docs/BEST-CONFIG.md`. Facts per component live in
  `docs/hw/components.yaml`; regenerate the tables with `tools/gen-docs.py --write`. End a round
  with `tools/gen-docs.py --check && tools/repo-test.sh --fast`.

## 4. Round 2 (2026-11-26 afternoon): Windows driver comparison and measurement corrections

Sources: the Windows driver `gfx_win_101.9175.exe` (Arc B390 / PTL, `PCI\VEN_8086&DEV_B080`),
unpacked with `unrar` in a container (it is a RAR5 SFX; 7-Zip and bsdtar on the host cannot open it)
to `~/.cache/oxp3-winre/x/Graphics/`. The Vulkan driver is `igvk64.dll`, D3D12 on Xe3 is
`igd12um64xe3.dll`, D3D11 is `igd10umt64xe.dll`, the kernel driver is `igdkmdnd64.sys`. objdump
(`objdump -d -M intel --start-address=… --stop-address=…`) reads these PE files directly and is
enough for targeted questions; a full Ghidra analysis of a 30-80 MB binary is not needed.

All measurements: GravityMark 1.89, gamescope headless, native Vulkan unless stated, **no harness
sampler and no `-times`**, A B B A or better, warm shader cache. Scripts and raw logs:
`~/.cache/oxp3-winre/runs/` (`knob-sweep.sh`, `pipeline*.sh`, `sweep720/summary.txt`); copy the
scripts into `tools/gpu-bench/` if you keep using them.

### 4.1 Measurement corrections (read before quoting any number)

| finding | evidence | consequence |
|---|---|---|
| `gpu-bench.py` rendered at **1600x900**, labelled 1280x720 | GravityMark log: `Starting 1600x900 Vulkan Benchmark` / `Resolution: 1600x900`; gamescope `-W/-H` only size the output | fixed: `bench_gravity` now passes `-width 1280 -height 720`. The 219 raster / 173 / 195 RT in README and BEST-CONFIG are 1600x900 numbers. `gravitymark.py` (D3D11/D3D12) was always right |
| `-times` (per-frame time file) costs **~8 %** | same driver, 1600x900: raster 237.4/236.1 bare vs 219 harness; RT 188.1 bare vs 173.1 harness | relative A/B inside the harness stays valid; never compare a harness number with Windows |
| the `nvtop -s` sampler costs nothing | 238.2 with a 4 Hz `nvtop -s` loop vs 238.7/238.6 without | keep it |
| **true 1280x720 raster today: 278.1 / 277.6 fps** (installed 24 Nov build) | `-width 1280 -height 720`, headless | Windows leaderboard 290.4 → **-4.3 %**, and that still includes gamescope's own composite of every frame. There is no 26.3.1 regression; the old 268.2 (Mesa main, fullscreen) is consistent |
| the installed Mesa is the **24 Nov** build | `rpm -q mesa-vulkan-drivers` → `…202611241043`; the TLAS build `…202611260945` sits in `~/.cache/oxp3-fixes/out/` | the +12.7 % RT patches are built but **not installed**: the user needs one `./oxp3-install.sh` run |
| direct windows on KWin are too noisy to benchmark | 257.6 / 271.5 / 218.8 fps for three identical runs; the desktop is 1097x686 logical, so a 1280x720 window is scaled and composited | measure headless |
| two GravityMark processes from the morning session were still alive (2 h 20 min) | 0 GPU cycles in 2 s (`drm-cycles-rcs` in fdinfo), blocked in `drm_syncobj_array_wait` | they did not pollute results, but check `pgrep -a GravityMark` before every session |

To do (small, no root): re-measure the README/BEST-CONFIG raster and RT rows with the fixed
`gpu-bench.py`, label them 1280x720 headless, and say that `-times` is on in the harness.

### 4.2 Power: in D3D11 the CPU takes the GPU's watts (measured, morning floor session)

From the raw 10 Hz samples of `gpu-clock-floor.sh` (`/tmp/oxp3-clock.dr6qzp/floor-450.log`, columns
time, act_freq, reasons, package, core, uncore(=GT), dram; samples with package > 20 W):

| | package | CPU cores | GT (RAPL uncore) | DRAM | GT throttle reason |
|---|---:|---:|---:|---:|---|
| GravityMark D3D11 (DXVK) 1280x720 | 33.4 W | **14.7 W** | **13.0 W** | 1.9 W | `pl2` in 98 % of samples |

The package sits at its limit. More CPU power in a DXVK/vkd3d game means fewer GPU clocks (1845 MHz
mean here, against 2280 MHz in native Vulkan, where the GPU is not throttled). This is where
"Linux is lighter" can actually win or lose. The levers are on the CPU side: P-core clock caps or
EPP while a game is GPU-bound, Smart CPU boost backing off when GT reports `pl2`/`pl1` (task T6),
DXVK/Wine CPU overhead. Nothing in Mesa recovers it.

### 4.3 Driver knobs, measured (no rebuild; driconf options can be set as environment variables)

An environment variable with the option name beats both the default and the per-engine entries in
`00-anv-defaults.conf` (`xmlconfig.c`: "option value … ignored" when `os_get_option(name)` is set).

| knob | default | result | verdict |
|---|---|---|---|
| `anv_state_cache_perf_fix=true` (BTP+BTI RCC keying, no flush on RT change) | **on for every `vkd3d` engine**, off otherwise | native Vulkan 1280x720: **226.4 / 226.8 vs 278.1 / 277.6 (−18.5 %)**, warm (it is in the shader cache key); D3D12 and D3D11: no effect (4.4) | keep Mesa's defaults; never force it on for native Vulkan |
| `intel_force_sampler_prefetch=true` | off | 238.5 vs 238.7 (1600x900) | neutral |
| `anv_dynamic_stack_id_control=true` (Xe3 dynamic StackID) | off | RT 188.3 vs 188.1/188.0 | neutral |
| `anv_stack_ids=1024` | 512 | RT 188.2 | neutral |
| `INTEL_DEBUG=no-jay` | Jay | RT −33.8 % (morning) | keep Jay |

### 4.4 D3D12/D3D11 and the state-cache fix

The −18.5 % above made the per-engine default suspicious, because Mesa turns the option on for
every vkd3d game. Measured through Proton with `gravitymark.py` (env var beats the engine entry):

| API, resolution | option on | option off |
|---|---:|---:|
| D3D12 (vkd3d) 1280x720 | 95.23 / 95.10 | 95.10 / 95.17 |
| D3D12 (vkd3d) 1920x1200 | 80.25 / 80.45 | 80.47 / 80.56 |
| D3D11 (DXVK) 1280x720 | 84.26 / 84.92 | 84.90 / 85.07 |

No effect in either translation layer. Plausible reason: vkd3d already reaches its descriptors
through the bindless heap, so "binding table only for render targets" changes nothing there, while
native GravityMark loses its binding-table access path. Nothing to ship. Do not reopen this
without a GPU-bound native-Vulkan game that sets the option.

### 4.5 TBIMR (tile-based immediate-mode rendering): Windows uses it on Xe3, Mesa does not

- Mesa computes TBIMR tiling only for `GFX_VERx10 == 125` (DG2/MTL) in 26.3.1 and in main
  (`genX_gfx_state.c` `update_tbimr_info`). The commands and the 3DPRIMITIVE enable bit exist on
  Xe2/Xe3 (`xe2.xml` imports `gen125.xml` without excluding them).
- The Windows **Xe3** D3D12 driver emits it: `igd12um64xe3.dll` VA `0x1800c6338`
  `mov dword [rax], 0x786e0002` (3DSTATE_TBIMR_TILE_PASS_INFO), then DW1/DW2 tile size and count, and
  DW3 `0x2c`, i.e. TileBoxCheck=1 and TBIMR batch size field **5** (Mesa's 12.5 formula gives 3 for
  one slice). `igvk64.dll` and `igd10umt64xe.dll` contain the same header (4 and 5 sites), plus an
  app-profile knob `DisableTbimrForMeshShading`, so TBIMR is on for mesh draws too by default.
- `patches/experimental/0300-anv-tbimr-xe2-xe3.patch` (not globbed, not shipped) ports it. The
  naive port crashed on the first draw: Xe2+ has no L3 partition table (`empty_l3_list`), so
  `device->l3_config` is NULL. The patch sizes the tile cache from `ANV_TBIMR_TC_KB` (default 4096)
  instead, and `ANV_TBIMR_BATCH` overrides the batch field. Toggle with `intel_tbimr=false|true`.
- Result, GravityMark raster 1280x720, side driver: off 277.6 / 278.3 / 278.4, on 278.2 / 278.5,
  batch 5: 278.1, 1 MB: 278.2, 8 MB: 277.9. RT (20k asteroids, one run each): off 262.2, on 258.9
  (−1.3 %, repeat before reading anything into it). **Neutral in GravityMark**, which is geometry-bound
  (50k asteroids, little overdraw). TBIMR pays in bandwidth-bound passes: G-buffers, heavy overdraw,
  MSAA, high resolution.
- Next (no root): A/B one deferred-renderer game at 1080p+ with `tools/game-limit-capture.py`,
  `intel_tbimr=false` vs `true`, on the side driver (`VK_DRIVER_FILES=…/icd.json`; build with
  `~/.cache/oxp3-winre/anv-tbimr/build.sh`, about 2 min incremental). Then run the VK-CTS
  renderpass, draw and dynamic_rendering groups (there is no CTS binary on the machine yet). Ship
  only after both, and leave the Xe3 `CHICKEN_RASTER_2` programming alone: bit 5 means "Disable Any
  MCTR response fix" on Xe3 (it was TBIMR fast clip on 12.5), and the Windows KMD skips that write
  for Xe3.

### 4.6 Windows kernel driver: register programming (partial, next steps listed)

- `igdkmdnd64.sys` function `0x1403d7930` (size 0x475) is a render-context init that issues masked
  register writes through helper `0x1403cac20` (`r8d` = MMIO offset, `r9d` = mask|value), each guarded
  by a platform field. `[rbx+0x740]` is a GFX core family compared as a range
  (`sub 0xc09; cmp 0x11f6`). The CHICKEN_RASTER_2 bit-5 write is excluded at ≥ 0x1e00, which matches
  Xe3 redefining that bit.
- The 0x1403c0000-0x140400000 region holds 131 such writes over 90 registers
  (`~/.cache/oxp3-winre/ghidra/kmd-writes.txt`). About 30 offsets are not in `xe`'s register headers at
  all (`0x7014`, `0x7000`, `0x7038`, `0x6200`, `0x5584`, `0x20c0`, `0x20d4`, `0x20e4`, `0x20ec`,
  `0xb194-0xb1ac`, `0xe100`, `0xe184`, `0xe7cc`, …). Others are in the headers but never programmed
  (`HALF_SLICE_CHICKEN5`, `UNSLCGCTL9430/9440/9444`, `XEHP_L3NODEARBCFG`, `XEHP_MERT_MOD_CTRL`).
- Not done: which writes apply to PTL. Next step: decode the platform fields (the family values come
  from Intel's open-source gmmlib `igfxfmid.h`, `GFXCORE_FAMILY`/`PRODUCT_FAMILY`), list the writes
  that apply to Xe3, then diff them against `xe`'s `register-save-restore` debugfs output on this
  device (root, read-only). Anything Windows sets and Linux does not is a kernel `xe_tuning.c`
  candidate. Expect mostly workarounds, few performance settings.

### 4.7 Windows Vulkan app-profile table: hints about what Windows does by default

`igvk64.dll` carries plain-text app overrides (`AIL info: name / value / reason`; 186 entries, 105
with reason `performance`). An override that disables something means the default has it on:

- `ForceTile64OptimizationDisabled`: Windows uses Tile64 for some surfaces by default. ISL picks
  Tile4 for everything except sparse (`isl.c` `CHOOSE(ISL_TILING_64*)` only under `SPARSE_BIT`).
  Candidate experiment: Tile64 for large colour/depth render targets on Xe2+.
- `DisableTbimrForMeshShading`: TBIMR on for mesh draws (4.5).
- `HiZInAllLayouts`, `ForceDepthWriteThru*`, `ForceNonMSAADepthE2ECompressionEnabled`,
  `ForceRHWOOptimization`, `DisableBlendToFillDiscardOptimization`, `EnableD3DVertexFetchGeometryOpt`,
  `DisableVertexReordering`, `ForceUboToCrOptimizationDisabled` (UBO→push promotion on by default:
  Mesa already promotes for `vkd3d|DXVK` via `anv_promote_cbv_to_push_buffers`).

### 4.8 Ranked next tasks

1. **Install the built TLAS driver** (user: one `./oxp3-install.sh`), then re-baseline with the fixed
   `gpu-bench.py` and correct README/BEST-CONFIG (4.1).
2. **TBIMR in a real bandwidth-heavy game** (4.5). It is the one hardware feature Windows uses on
   Xe3 that Mesa leaves off.
3. **CPU power in DXVK/vkd3d games** (4.2 + T6): measure a P-core cap / EPP sweep in one D3D11 game
   at the package limit and watch GT MHz and fps. This is the biggest lever for translated games.
4. Tile64 for render targets (4.7), KMD register decode (4.6).

## 5. Round 3 (2026-11-26 evening): driver overhead and GPU work per clock

Goal set by the user: the hardware's limit, not Windows, is the target. A lever that beats Windows
counts. Scripts and raw data: `~/.cache/oxp3-winre/runs/round3/`.

### 5.1 How to profile here without root (this took a while to find)

- perf/strace are not on the host. `~/.cache/oxp3-winre/perfhost/{perf,strace}.sh` run Fedora 45's
  binaries on the host with their libraries copied out (from the `oxp3-prof` container). Profiling
  from inside a container does not work: SELinux denies access to host processes.
- Yama `ptrace_scope=1` blocks attaching to non-children. `LD_PRELOAD` of
  `round3/src/ptracer.c` (built as `/tmp/claude-1000/prof/libptracer.so`; rebuild with gcc if /tmp was
  cleared) calls `prctl(PR_SET_PTRACER, ANY)` in every process, Wine's included. Then
  `perf record -e cpu_core/cycles/u -e cpu_atom/cycles/u -p <pid>` works (paranoid=2 allows user
  space). The CPU is hybrid: always record both PMUs and read both report sections.
- Use the side-built ANV (`~/.cache/oxp3-winre/anv-tbimr/icd.json`, `intel_tbimr=false` = the
  shipped code) for symbols. `round3/prof.sh <vk|d3d11|d3d12> <label>` does all of this.
- GPU side: `INTEL_MEASURE=draw,file=…,start=3000,count=10` (per-event GPU time; it adds a stall per
  event, so gaps are inflated) and `INTEL_DEBUG=pc` (every PIPE_CONTROL / RESOURCE_BARRIER with its
  reason). OA hardware counters need `observation_paranoid=0` (root): see 5.6.
- Shader quality of real games without running them: `fossilize_replay` (Steam's,
  `~/.local/share/Steam/ubuntu12_64/`) over `steamapps/shadercache/*/fozpipelinesv6/*.foz` with
  `--enable-pipeline-stats out.csv` and `VK_DRIVER_FILES=<side icd>`; `round3/foz-agg.py`
  summarises it.
- A real game in headless gamescope can be seen and driven: the X property
  `GAMESCOPECTRL_REQUEST_SCREENSHOT` on gamescope's Xwayland root writes `/tmp/gamescope.png`, and
  `xdotool` (in the container, `DISPLAY=:N` from the gamescope log) sends input. Steam games need
  the Steam client running (the Wukong benchmark stops at "Unable to initialize SteamAPI").

### 5.2 CPU: where the driver stack spends its time

| path (GravityMark 1280x720) | finding |
|---|---|
| native Vulkan | **ANV is 57 % of the process's user CPU** (the engine 27 %). Top: `anv_AllocateDescriptorSets` 6.4 %, buffer surface-state fill 3.4 % + 1.5 %, `emit_binding_table` 2.9 %, descriptor writes 2.5 %, compute walker 2.5 %. In `anv_AllocateDescriptorSets`, 78 % of its samples sit on `lock addl $1,0x70(layout)`, the shared `vk_descriptor_set_layout` refcount taken per set, a cache line bounced between recording threads |
| D3D11 (DXVK) | ANV ~1 %. The main thread pins a P-core (87e9 cycles in 20 s): DXVK's front-end 73 %, and **~25 % spinning on `vkGetEventStatus`** through Wine's unix-call thunk (an app event-query spin). `dxvk-cs` runs on E-cores and is mostly idle |
| D3D12 (vkd3d) | not CPU-bound: the main thread is mostly asleep on E-cores |

Levers (beyond Windows, pure Mesa/stack work):
1. **One layout reference per (pool, layout), not per set** (ANV): drop the per-set atomic in
   `anv_descriptor_set_create` / free path; the pool keeps a small set of referenced layouts and
   unrefs them on reset/destroy. Measure with `prof.sh vk` (the self time of
   `anv_AllocateDescriptorSets`) and a CPU-bound native title.
2. Cheaper buffer descriptors: `isl_gfx30_buffer_fill_state_s` + `anv_fill_buffer_surface_state`
   are ~5 %; cache the packed state for repeated (address, range, format) or write templates.
3. The D3D11 spin: a spinning P-core costs GPU watts at the package limit (4.2). Worth an upstream
   DXVK/Wine look (back off in `GetData` polling after N misses), not an ANV change.

### 5.3 GPU: what a GravityMark frame is made of

- `INTEL_MEASURE` per frame: three `draw indexed indirect count` (the asteroids) = 57 % of GPU busy
  time; GravityMark's own 74 small `vkCmdDispatchIndirect` (2 µs each) with large gaps before them.
- `INTEL_DEBUG=pc`: **~154 `RESOURCE_BARRIER`s per frame** (929k over ~6k frames), almost all
  `Signal=GPGPU,Geometry Wait=Top` with L1-DC/texture/constant invalidation, from the app's
  compute→ALL_COMMANDS barriers. Each drains the compute pipe and leaves caches cold.
  `resource_barrier_signal_stage()` maps a src stage of TOP_OF_PIPE/HOST to GEOM, which Vulkan
  defines as no execution dependency. Dropping that when another stage is present is spec-correct
  and saves a geometry drain whenever draws are in flight. Barrier batching (merge back-to-back
  barriers with no work between) is the bigger idea. Both need CTS sync tests.
- Shader quality is not the limit in GravityMark: 62 shaders, no spills, 8-10 threads/EU.
  Forcing SIMD32 or SIMD16 fragment shaders: 277.2-278.5 vs base 276.7-278.3, neutral.

### 5.4 Real games' shaders (Fossilize replay, 4 titles, ~25k executables)

- **No spills in any title.** Jay plus Xe3's variable register file handle pressure.
- Heaviest title (`shadercache/3758894876`, 2,843 fragment pipelines, median 1,697 instructions):
  only **36 % keep a SIMD32 variant**. Jay discards SIMD32 when it needs more than 96 GRF and SIMD16
  needs fewer (`jay_from_nir.c`, `vrt_threshold = 96`). The SIMD16 variants then run at 128-192 GRF
  (5-8 threads). Whether SIMD32 at 4-5 threads beats SIMD16 at 8 is the per-clock question, and
  GravityMark cannot answer it. Test with `INTEL_DEBUG=fs32` (forces SIMD32 only) or a
  `vrt_threshold` env override in a real game.
- Another title (`3198397224`): vertex shaders mostly at 160-256 GRF, i.e. **4-6 threads/EU** for the
  geometry stage. Candidate: a VS-specific occupancy target (recompile at a lower GRF budget with
  more scheduling pressure), measured in that title.

### 5.5 Platform levers beyond Windows' defaults

- **Power**: OneXConsole has a 40 W / PL2 41 W "unlock" (`docs/hw/power-fan.md` §2). We stop at 35
  W, and translated games sit at the package limit with the GT pl2-throttled. Offer it as an opt-in
  profile and measure GT MHz and fps in one D3D11 title.
- **Memory**: SAGV pinned high (T7), never measured in a game.
- **CPU share**: cap P-core clocks while GT is power-throttled (T6); the 25 % spin above makes this
  sharper in DXVK titles.

### 5.6 What is needed to go further (decisions for the user)

1. **A real-game harness needs Steam running.** The Wukong benchmark (UE5, D3D12) can be launched
   headless, screenshotted and clicked (5.1), but only with the Steam client up. With that, every
   GPU-side candidate (TBIMR, SIMD32 threshold, VS occupancy, barrier trimming) gets a real
   bandwidth-heavy A/B instead of GravityMark, which is geometry-bound and insensitive to all of them.
2. **One root session** for OA counters (EU active/stall, sampler, L3/memory bandwidth per frame):
   build IGT's `xe_perf_recorder` in the container, `sysctl dev.xe.observation_paranoid=0`, record
   GravityMark and one game, restore. Batch the 40 W and SAGV tests into the same session.

## 6. Round 4 (2026-11-26, root window): hardware counters, power, a real-game harness

Tools (all in `~/.cache/oxp3-winre/`, scripts and data in `runs/round4/`):
- `tools/gpu-bench/perf-window.sh` (repo, needs sudo once): opens OA counters/perf for N minutes;
  measurement scripts switch `pl35`/`pl40`/`stop` through `/run/oxp3-perf-window/cmd`; thermal guard;
  restores everything on exit.
- `xeperf/xe-perf-{recorder,reader}`: IGT's xe OA tools built in the container and patched to run
  without debugfs and to read signal-stopped recordings (`lib/xe/xe_wa.c`, `lib/igt_debugfs.c`,
  `lib/xe/xe_oa_data_reader.c`). Metric sets on PTL: RenderBasic, RenderPipeProfile,
  VectorEngineProfile/Stalls, MemoryProfile, DeviceCacheProfile, RTProfile, …
  `oa-capture.sh`, `oa-agg.py` (sum counts, GPU-time-weight percentages).
- Unattended Wukong benchmark: Steam launch option `wk-wrap.sh %command%` (set by the user) runs it in
  headless gamescope; Steam's launcher re-applies Steam's environment, so the wrapper injects
  gamescope's DISPLAY right before `proton`. `wk-shot.sh` (screenshot via
  `GAMESCOPECTRL_REQUEST_SCREENSHOT`, read from the gamescope process's private `/tmp`),
  `wk-key.sh` (xdotool), `wk-bench.sh` (run/rerun, wait for results), `wk-variant.sh <name> K=V…`
  (relaunch with env, warm + measured run). The launcher has Steam DRM: it must be launched by Steam;
  `steam_appid.txt` does not help. Results are integer fps (1 fps ≈ 1.8 %).

### 6.1 Where the GPU's clocks go (OA counters)

| | GravityMark raster 1280x720 | Wukong 1280x720 Medium (UE5 D3D12) |
|---|---:|---:|
| GPU busy / clock | 99.96 % / 2279 MHz | 99.7 % / 2271 MHz |
| **EU (XVE) active** | **24.5 %** | **33 %** |
| EU thread occupancy | 65 % | 77-81 % |
| EU stall (of active) | 48 %: SBID 44, ALU-WR 22, barrier 13 | 43 %: SBID 42, ALU-WR 17, barrier 13 |
| compute threadgroup dispatch resource stall | 30 % | 18-19 % |
| memory read + write | 22.8 + 14.5 GB/s, queue-full 0 % | 22.2 + 9.5 GB/s, queue-full 0 % |
| sampler L1 miss / sampler busy | 25 % / 7 % | 24 % / 15 % |
| compression (256 B writes in → out) | 2.02 G → 1.09 G | 2.20 G → 1.08 G |

GravityMark timeline at ~213 µs resolution: compute-only phases 56 % of GPU time at 25 % EU-active with
72 % occupancy (its N-body compute is SLM/latency-bound: 1.6 TB SLM reads in 20 s, 11 G bank
conflicts), mixed 37 %, raster-only 7 %.

Reading: the GPU runs at its maximum clock, memory bandwidth is nowhere near a limit, and the
EUs are idle two-thirds of the time while threads sit resident **waiting on send results** (SBID). The
remaining per-clock headroom is latency hiding (more resident threads at lower GRF, fewer waits),
not bandwidth features. Wukong also shows ~35 % of load/store-cache accesses as partial writes
(36 G in 20 s): read-modify-write traffic from how its shaders store.

### 6.2 Measured levers

| lever | GravityMark | Wukong | D3D11 GravityMark |
|---|---:|---:|---:|
| **40 W / 41 W unlock** (OneXConsole preset) | - | 57 → 57 (package 30.4 W, GT at max clock, never throttled) | **84.5 → 86.6 (+2.6 %)**: GT 2180 → 2295 MHz, package 35 → 37 W, max temp 89 °C both |
| TBIMR on Xe3 (`patches/experimental/0300`) | neutral | 57 → 57 (no hang) | - |
| SIMD32-only fragment (`INTEL_DEBUG=fs32`) | neutral | 57 → 57 | - |
| compute SIMD16 / SIMD32 forced | 277.7-278.0 / 277.9-278.3 vs 277.1-278.7 | - | - |
| brw for compute, Jay elsewhere | **−6 %** (258-262) | - | - |

Conclusions:
1. **Ship the 40 W profile as an opt-in "performance" TDP** (vendor's own unlock): +2.6 % where the
   package is the limit (translated, CPU-heavy titles); nothing to gain where the GPU is already at
   its 2.3 GHz clock ceiling. Keep the thermal guard.
2. GPU-bound native D3D12 is **clock-bound with idle EUs**. The next real gains are compiler work on
   latency hiding: lower-GRF compilation for occupancy (the VRT threshold, 5.4) measured with the
   Wukong harness at finer resolution (add MangoHud frame-time logging through `wk-env`), and
   scheduling of sends (SBID waits). TBIMR/SIMD32 toggles do not move it.
3. GravityMark is a poor proxy for games: its compute phase dominates. Use the Wukong harness.
