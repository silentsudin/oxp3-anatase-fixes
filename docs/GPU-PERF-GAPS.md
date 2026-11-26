# GPU driver-stack performance: gaps, unpulled levers, next work (2026-11-26)

Handoff brief for an agent continuing GPU performance work on the OneXPlayer 3 (Panther Lake,
Arc B390 iGPU, Xe3, `xe` kernel driver, Mesa ANV). Each task below says what to do, why, how to
measure it, what counts as success, and whether it needs root. Work top to bottom unless the user
picks something else.

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
