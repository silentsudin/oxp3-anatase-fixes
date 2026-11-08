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
