# CPU placement on the OneXPlayer 3 (Intel G3 Extreme / Panther Lake)

Status: research complete, **lab test pending**.  This note records why games can end up
on the wrong core, what is already fixed, and the tools/experiments added for the
P/E/LP-E placement work.  It complements `PERFORMANCE-CPU-SCHED.md` (the parked
scheduler test plan).

## Topology and current state

- P-cores cpu0-1 (4.7 GHz), E-cores cpu2-9 (3.6 GHz), LP-E cpu10-13 (3.1 GHz), no SMT.
- `intel_pstate` active, governor `powersave`, EPP `balance_power` on every CPU.
- `scx_cosmos 1.1.7`, loaded by `oxp3-gaming-sched.service` as
  `scx_cosmos -c 0 -p 0` (official scx_loader "gaming" profile).
- `intel-lpmd 0.1.0` runs, but the Panther Lake config sets
  `PerformanceDef/BalancedDef/PowersaverDef = -1` (never enter Low Power Mode), so it is
  not moving game threads to E/LP-E cores.
- Kernel 7.3.4-an02.

## What the research established

1. **Linux does not consume Intel Thread Director (ITD) hints in the kernel.**  The
   Hardware Feedback Interface is exposed to userspace; placement is left to the
   scheduler's capacity model (and optionally `intel-lpmd`).  Windows integrates ITD in
   its scheduler — that is the main reason P/E scheduling can feel better there.
   <https://unix.stackexchange.com/questions/806535/is-intel-thread-director-itd-actually-used-by-the-scheduler-on-linux>

2. **A real Intel hybrid scheduler bug existed and is fixed in 7.3.**  Cluster-aware
   load balancing (since 5.16) assumed uniform clusters and mis-distributed work on
   asymmetric P/E/LP-E topologies (Alder Lake through Panther Lake).  Ricardo Neri's
   fixes went into 7.3, plus a 7.3-rc2 fix to honour asymmetric packing over cache-aware
   scheduling on hybrid CPUs.
   <https://www.phoronix.com/news/Linux-7.3-rc2-Scheduler-Fixes>
   <https://www.pcgameshardware.de/Linux-Software-26761/News/Kernel-7-3-Intel-Scheduler-P-Cores-E-Cores-1550686/>

   **Our kernel is 7.3.4**, so these fixes are present.  The scheduler A/B results in
   `cpu-sched.md` were measured on 7.2.7 and need re-running.

3. **`scx_cosmos -c 0 -p 0` has no P-core preference.**  It keeps locality; a
   latency-critical main thread may stay on an E/LP-E core.  scx_cosmos supports
   `-m performance|turbo` (primary domain of fastest CPUs, with
   `--primary-overload-us` to spill when overloaded) and `-P` (prefer higher-ranked
   idle cores).  The official scx_loader "lowlatency" profile is gaming + fast-CPU
   preference.
   <https://github.com/sched-ext/scx/blob/main/rust/scx_utils/src/...> (see
   `scx_cosmos --help` on this machine)

4. **`scx_lavd --performance` is not safe on Intel hybrids**: it is reported to peg a
   CPU and leak ~34 GB on a Core Ultra 9 275HX (sched-ext/scx issue #3340).  Only
   `--autopower`/balanced LAVD is a candidate.
   <https://github.com/sched-ext/scx/issues/3340>

5. **Community workarounds for this symptom class** are P-core affinity or disabling
   E-cores (Elden Ring stutter), and spoofing the CPU topology to Wine instead of
   `taskset` when `taskset` breaks audio.
   <https://ngabbs.com/read.php?tid=40611808>
   <https://www.computerbase.de/forum/threads/fix-fuer-wot-unter-linux-2-fps-menue-microruckler-soundbug-bei-intel-p-e-cores.2266262/>

## Tools added

### Per-game P-core confinement (no root)

`tools/game-pcores.sh [--cpus 0-1] [--dry-run] -- COMMAND...`

Runs the whole process tree on the P-cores with `taskset`; children inherit it.
Steam Launch Options:

```
/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/game-pcores.sh -- %command%
```

Verified: `tools/game-pcores.sh -- sh -c 'grep Cpus_allowed_list /proc/self/status'`
reports `0-1`.

Caveat: only two P-cores, no SMT.  Use it for titles where the main thread must stay
on a P-core; heavily threaded games can lose throughput.

### Scheduler profile switch (root)

`sudo tools/oxp3-scx-profile.sh gaming|lowlat|lowlat-fast|eevdf|status`

- `gaming`: `scx_cosmos -c 0 -p 0` (current).
- `lowlat`: `scx_cosmos -c 0 -p 0 -m performance --primary-overload-us 1000`.
- `lowlat-fast`: adds `-P` (prefer higher-ranked idle cores).
- `eevdf`: disables the unit, kernel EEVDF (for the 7.3 A/B).

It writes `/etc/systemd/system/oxp3-gaming-sched.service.d/50-profile.conf` and
restarts the unit.

## Test matrix (one variable per run)

1. `gaming` (baseline) vs `lowlat` vs `eevdf` on a CPU-bound title (Brilliant Diamond
   recomp / a Unity port) and a GPU-bound title.
2. `game-pcores.sh` wrapper for the CPU-bound title.
3. `lowlat-fast`.
4. Per-cluster EPP (P `balance_performance`/`performance`, E/LP-E `balance_power`) on
   top of the best scheduler profile.
5. Temporary LP-E offline (`cpu10-13`) for the Elden-Ring-style cases.
6. Proton games: `WINE_CPU_TOPOLOGY` spoofing as an alternative to `taskset`.

Measure with MangoHud (FPS, 1% low), per-thread CPU + `psr` for the main thread, GPU
clock/util, and package power.  Brilliant Diamond re-runs its mod setup on launch, so
avoid casual relaunches; do the A/B on a title that starts cleanly.

## First results (2026-11-16)

`sudo tools/oxp3-scx-profile.sh lowlat` (scx_cosmos -m performance primary domain,
spill at 1000 us) produced a clearly higher frame rate in **Brilliant Diamond** (Unity
recomp, the CPU-bound case with the main thread previously on an E-core).  **Elden Ring
showed no change.**

That split is expected if Elden Ring is GPU-bound or frame-capped (60) rather than
waiting on a badly placed CPU thread: the primary domain can only help when the game's
critical thread is the limiter.  Diagnose next with MangoHud/nvtop while in game:

- `gpu_load` near 100 % and GPU frame time >= wall frame time -> GPU-bound; scheduler
  changes cannot raise FPS.
- GPU load well below 100 % with frame time >> GPU time -> CPU/driver bound; then try
  `tools/game-pcores.sh` (P-cores only) or `--cpus 0-9` (drop LP-E, keep E) and compare
  1 % lows, not only average FPS.
- Windows reports for Elden Ring are about **stutter** (1 % low / frame-time spikes) and
  are fixed by P-core affinity or disabling E-cores; average FPS usually moves little.

Other Elden Ring-specific tests: `WINE_CPU_TOPOLOGY` spoofing so the game does not see
the hybrid topology, and the P-core wrapper in Steam Launch Options.

## Intelligent placement: LPE engagement and ITD

Two separate observations:

### LP-E cores stay idle (measured: cpu10-13 at 0.4-1.4 % busy on the desktop)

That is expected: EEVDF and scx_cosmos rank CPUs by capacity, so the 3.1 GHz LP-E
cores are only chosen when nothing better is free, and the `-m performance` primary
domain deliberately excludes them for games.  LP-E cores are for idle/housekeeping,
not for game threads.  To make them useful they must be given work explicitly:
housekeeping, background services, or IRQs pinned there (see below), or the LAVD
scheduler's energy-model placement in powersave/balanced mode.

### Intel Thread Director is not available to the Linux scheduler

- The kernel exposes HFI (Hardware Feedback Interface) to userspace; EEVDF and
  sched_ext do **not** read it.  Windows integrates ITD in its scheduler, which is the
  behavioural gap.
- `intel-lpmd` is the HFI consumer on Linux, but the installed 0.1.0 only uses it for
  Low Power Mode selection, and this platform's config has every mode set to `-1`
  (never enter LPM).
- The HFI → sched_ext integration was presented at LPC 2026 ("HFI plug in to
  scx_ext"): converting HFI output into scheduler-readable CPU hints for LAVD.  It is
  not in the installed scx 1.1.x, so there is no mainline ITD-driven placement today.

### Remediation options, in order of practicality

1. **`scx_lavd` EMAS** (added as `sudo tools/oxp3-scx-profile.sh lavd`): LAVD creates a
   separate scheduling domain per LLC and per core type and uses an energy model, which
   is the closest thing available to "intelligent" hybrid placement.  Test it; do not
   use `--performance` (CPU peg + ~34 GB leak, sched-ext/scx #3340).  `lavd-nc` is the
   non-compacting balanced variant.
2. **`intel-lpmd` 0.1.1 per-process cpusets** (test release, not packaged): classifies
   processes (game, interactive, background, utility) and confines them to P/E/LP-E via
   `AllowedCPUs`, uclamp, and focus-PID promotion.  This is the most direct ITD-adjacent
   userspace policy; it needs building from intel/intel-lpmd or a Fedora package update.
3. **Static housekeeping policy** (works today): confine background work to LP-E and
   keep games on P/E.  Examples: IRQ affinity for `xe`/NVMe on cpu10-13, `systemd`
   `AllowedCPUs=10-13` for non-latency slices, and `tools/game-pcores.sh` for titles
   that must stay on P-cores.  This does not make the kernel smarter, but it stops
   background work from competing and gives LP-E a defined job.
4. **Per-game confinement** (`tools/game-pcores.sh`, `WINE_CPU_TOPOLOGY`) for the titles
   where the main thread must stay on a P-core.
5. **Kernel/HFI future work**: track the LPC 2026 HFI→sched_ext work; a mainline HFI
   scheduler plugin would be the real ITD equivalent.  No supported patch exists for
   kernel 7.3.4 today.
