# CPU scheduler work - OneXPlayer 3, Intel "G3 Extreme" (14 hybrid CPUs, no SMT)

## Current setup (audited 2026-11-08)

- Cores: P-cores cpu0-1 (max 4.7 GHz), E-cores cpu2-9 (3.6 GHz), LP-E cpu10-13 (3.1 GHz), one policy per CPU.
- `intel_pstate` active, governor `powersave`, EPP `balance_power` on every CPU.
- Scheduler: kernel EEVDF (default). Kernel 7.2.7-an28; `sched_ext` is compiled in
  (`/sys/kernel/sched_ext/`) and `scx_lavd`, `scx_bpfland`, `scx_rusty`, `scx_loader` (scx 1.1.3) are installed
  but nothing is loaded (`scx_loader`, `scx.service` inactive).
- No `irqbalance`, `thermald`, `power-profiles-daemon` or `tuned`; `hhd` runs the handheld controls and TDP.
- Kernel cmdline: only `xe.enable_panel_replay=0`; no scheduling/affinity options.

## Options to evaluate

1. `scx_lavd --balanced` and `--autopower` - LAVD is designed for latency-critical gaming and now has energy-model
   aware placement for hybrid CPUs (EMAS). Avoid `--performance`: there is a reported Intel-hybrid bug where it
   pegs a CPU and leaks memory (sched-ext/scx issue #3340), and it would also fight our 35 W budget.
2. `scx_bpfland` - interactive/gaming placement with a simple, stable policy; useful comparison point.
3. Per-cluster EPP: P-cores `performance`, E-cores `balance_performance`, LP-E `power` - keeps latency-critical work
   on P-cores at full clock while background work stays efficient on LP-E.
4. IRQ steering (later, if scheduler work shows wakeup churn): pin `xe` and NVMe IRQs away from cpu0-1.
5. Kernel: 7.2.7 predates the 7.3 hybrid scheduling fixes (Intel P/E stall reduction) and EEVDF "flatten the pick".
   A kernel bump is a separate, riskier track.

## Harness

`tools/cpu-sched-test.sh <tag>` applies one configuration, runs GravityMark (`-asteroids 100000`, 1280x720,
native Vulkan, CPU-heavy) and the Cyberpunk 2077 benchmark (direct Proton, `-benchmark`), then restores the previous
scheduler/EPP and appends a row to `docs/cpu-sched-results.csv`:

  tag,date,gravity_fps,cp_avg,cp_min,cp_max,scheduler,epp_pcores,epp_ecores,epp_lpecores

Tags: `baseline`, `lavd`, `lavd-autopower`, `bpfland`, `epp-perf`, `epp-perf-lavd`.

Rules: one variable per run, warm caches (the Cyberpunk harness warms itself), Steam closed, and record the
state (`./state-snapshot.sh`) alongside. Compare medians of at least two runs per tag before concluding.

## Candidates for this architecture (scx 1.1.3, kernel 7.2.7)

17 sched_ext schedulers are installed; the ones that matter for a hybrid gaming handheld:

| tag | what it is | why |
|---|---|---|
| `cake` | `scx_cake`, gaming-first, no knobs ("no task waits while a CPU sits free, warm cache, fair") | designed for exactly this; zero configuration |
| `cosmos-gaming` | `scx_cosmos -c 0 -p 0` (official scx_loader gaming profile) | locality-preserving, express lane for interactive tasks |
| `cosmos-lowlat` | `scx_cosmos -c 0 -p 0 -m performance` (official lowlatency profile) | same + prioritises the fastest CPUs on hybrid |
| `p2dq` | `scx_p2dq` pick-two, multi-layer queues | interactive task handling, good on multiple LLC/domains |
| `lavd-nc` | `scx_lavd --balanced --no-core-compaction` | purpose-built for handheld gaming; plain `--balanced` aborted with a runnable-task stall on this kernel |
| `bpfland` | `scx_bpfland` | stable interactive reference |

Harness v3 (`tools/cpu-sched-test.sh <tag>`) now also samples package power for the whole window and records
FPS/W, the applied scheduler/EPP state and a validity flag, and runs the game **without** affinity restrictions so
the scheduler decides placement.

## Round 1 results (2026-11-08 19:18-19:40, warm caches, EPP balance_power, no affinity restrictions)

| tag | GravityMark FPS | Cyberpunk avg | Cyberpunk min | avg pkg W | FPS/W | valid |
|---|---|---|---|---|---|---|
| baseline (EEVDF) | 182.8 | 49.8 | 43.3 | 25.6 | 1.945 | yes |
| cake | 182.8 | 50.8 | 44.7 | 25.5 | 1.992 | yes |
| cosmos-lowlat | 182.5 | 51.5 | 44.8 | 25.4 | 2.028 | yes |
| **cosmos-gaming** | 182.6 | **52.2** | **45.4** | 25.4 | **2.055** | yes |
| p2dq | 182.8 | 51.9 | 45.1 | 25.4 | 2.043 | yes |
| lavd-nc | 182.7 | - | - | 18.4 | - | **no** (Cyberpunk black screen) |

Conclusions:
- GravityMark is scheduler-insensitive at this workload (~182.8 everywhere) - it is geometry/GPU-bound, so it only
  guards against regressions, it does not discriminate between good schedulers.
- Every shipped sched_ext candidate beats EEVDF on Cyberpunk by 2-5 % with no measurable power cost:
  +4.8 % avg / +4.9 % min (cosmos-gaming), +4.2 %/+4.2 % (p2dq), +3.4 %/+3.5 % (cosmos-lowlat), +2.0 %/+3.2 % (cake).
- `scx_cosmos -c 0 -p 0` (official gaming profile) is the current best both on FPS and FPS/W (2.055 vs 1.945 baseline,
  +5.7 % efficiency).
- LAVD is not usable on this kernel+hardware: `--balanced` aborts with a runnable-task stall (rapl-power starved),
  and `--balanced --no-core-compaction` black-screens Cyberpunk. Excluded.
- The old invalid CSV rows (18:30-18:43, before the harness fixes) are preserved in
  `docs/cpu-sched-results-invalid.csv`; the live CSV now carries efficiency and validity columns.

Next: repeat cosmos-gaming/p2dq to confirm, then pair the winner with per-cluster EPP (P=performance,
E=balance_performance, LP-E=power) and compare FPS and FPS/W.
