# OXP3 RecompLauncher CPU/hybrid-scheduler investigation (parked checklist)

## Decision 2026-11-24: the kernel's EEVDF replaces scx_cosmos

On kernel 7.3.4, which has the kernel's hybrid placement fixes, EEVDF is the only scheduler that
keeps the thread limiting the frame rate on a P-core. The install now retires
`oxp3-gaming-sched.service` (`use_kernel_scheduler` in `tools/lib/install-common.sh`, and
hw-install's `sched` component). `--skip-sched` leaves the scheduler as it is, and
`tools/oxp3-scx-profile.sh` still switches by hand.

Measured with `tools/sched-bench.py`, unattended:
- workload: GravityMark D3D11 at 640x360, CPU-bound on its main thread (98 % of a core);
- 2 mirrored rounds (A B C … C B A);
- hhd Smart caps P 3.0 / E 2.0 / LP 1.5 GHz;
- "hot thread on P" is the share of 1 s samples where that thread ran on cpu0-1.

| scheduler | fps | 1% low | hot thread on P | package W |
|---|---|---|---|---|
| **EEVDF** | **96.37** | **51.74** | **100 %** | 23.9 |
| scx_beerland -m performance | 94.88 | 49.75 | 10 % | 22.2 |
| scx_cosmos -m performance -P | 94.78 | 49.55 | 6 % | 22.2 |
| scx_p2dq | 94.75 | 49.82 | 18 % | 22.4 |
| scx_cosmos -m performance | 94.69 | 49.75 | 6 % | 22.1 |
| scx_cosmos -c 0 -p 0 (was the default) | 94.66 | 49.83 | 7 % | 22.0 |
| scx_bpfland | 94.59 | 49.56 | 10 % | 22.0 |
| scx_lavd (autopilot) | 93.09 | 49.34 | 10 % | 22.1 |

What the table shows:
- Even the "prefer the fastest CPUs" modes (`-m performance`) left the hot thread on the E-cores.
- In this benchmark a 50 % faster core buys only +1.8 % fps. Its main thread mostly waits on the
  GPU, so GravityMark ranks placement well and performance weakly. CPU-heavy games such as Elden
  Ring should gain more from the right core.
- The Nov 8 result below (cosmos +4.8 % over EEVDF in Cyberpunk) was on kernel 7.2.7, before
  those fixes. Cyberpunk is no longer installed, so it could not be repeated.
- `scx_lavd --balanced --no-core-compaction` (the Nov 8 recipe) is rejected outright by scx_lavd
  1.1.4 as conflicting options. The bench now uses plain `--balanced`.

Status: **parked for a later date**. This is the measurement plan for the CPU side of the
RecompLauncher low-FPS investigation. The Mesa/ANV cooperative-matrix work is tracked separately.

## Why this is parked

Two games show different limits:

- **Longshot**: 31–33 fps gameplay, GPU 2250–2300 MHz at 57–63%, ~19 ms GPU time, ~14 ms host GPU
  wait, ~16 ms replay, about 2.4 CPU cores. No single CPU thread is saturated. Its own source
  deliberately falls back to software matrix multiplies because Intel ANV does not expose the
  console/PTX cooperative-matrix layout.
- **Pokémon Brilliant Diamond (uDPRE, Unity + GE-Proton)**: target 144 fps, measured
  `111.8 fps`; main thread **59% on E-core CPU 3**; whole process about **one core**; GPU only
  **650 MHz / 59%**. The game also renders borderless at **1129x706** because it inherits the
  scaled desktop logical size.

The Brilliant Diamond result is the one that could be a hybrid P/E scheduling problem. The
Longshot result is not: it is GPU/replay bound, with several CPU workers active and no hot thread.

Do not treat this as a proven scheduler bug. It is a test plan to confirm or reject that idea.

## Baseline observations to reproduce

Record these before changing anything:

```sh
cat /sys/kernel/sched_ext/state
cat /sys/kernel/sched_ext/root/ops
systemctl status scx_cosmos --no-pager
scxctl get
```

Per-thread CPU and the CPU each thread is actually on:

```sh
ps -L -p "$GAME_PID" -o pid,tid,pcpu,psr,comm | sort -k3 -nr | head -20
```

For each thread, `psr` is the last processor. Relevant CPUs on this machine:

- P-cores: **0–1**, max 4.7 GHz
- E-cores: **2–9**, max ~3.6 GHz
- LPE/low-power cores: **10–13**, max ~3.1 GHz

Read frequencies, EPP and governor:

```sh
for c in 0 1 2 3 4 5 6 7 8 9 10 11 12 13; do
  printf 'cpu%s %s MHz epp=%s gov=%s\n' "$c" \
    "$(( $(cat /sys/devices/system/cpu/cpu$c/cpufreq/scaling_cur_freq) / 1000 ))" \
    "$(cat /sys/devices/system/cpu/cpu$c/cpufreq/energy_performance_preference 2>/dev/null)" \
    "$(cat /sys/devices/system/cpu/cpu$c/cpufreq/scaling_governor 2>/dev/null)"
done
```

Game-side measurement for Brilliant Diamond:

```sh
grep -a 'frame pacing measured' \
  "$HOME/.local/share/com.recomplauncher.app/proton/pfx/drive_c/users/steamuser/AppData/LocalLow/DefaultCompanyLumi/Dpr/Player.log" | tail
```

## Test matrix

Run each variant on the same save/scene, for at least 60 seconds after the scene settles. Record
FPS, GPU clock, GPU utilisation, the main thread's CPU and `psr`, and package power if available.

### 1. EEVDF baseline

Stop the sched_ext scheduler and run the game on the kernel default. Confirm
`/sys/kernel/sched_ext/state` is disabled before measuring. This is the control for all
scheduler comparisons.

### 2. scx_cosmos gaming (current default)

`scx_cosmos -c 0 -p 0`, as installed by the repo's gaming-scheduler service. This is the
currently validated winner for GPU-bound Cyberpunk; do not assume it is best for a one-hot
Unity main thread.

### 3. scx_lavd

Try the LAVD variants, starting with the non-compacting one because plain `--balanced` aborted on
this kernel in the earlier repo notes:

```sh
scx_lavd --performance --no-core-compaction
scx_lavd --balanced --no-core-compaction
```

Use `scxctl`/the installed `scx_loader` if it is managing the scheduler; otherwise start it
directly and restore the normal service afterwards. Confirm which scheduler is actually loaded
from `/sys/kernel/sched_ext/root/ops`.

### 4. EPP / governor

While keeping the scheduler constant, compare:

- current `powersave` / HHD defaults,
- P-cores only at `performance` EPP and `performance` governor,
- all cores at `performance`.

Watch package power and temperature as well as FPS. The point of this test is the main thread's
P-core frequency and scheduling latency, not a blanket “more power” change.

### 5. Affinity

Three separate runs:

1. no affinity restriction,
2. whole game `taskset -c 0-1` (P-cores only),
3. main thread only pinned to CPU 0 with `taskset -pc 0 "$GAME_PID"`.

The earlier main-thread-only test on Brilliant Diamond changed the sampled main-thread CPU from
~59% to ~47%, but GPU utilisation stayed ~59–60% and no valid FPS reading was taken. That result
is therefore **inconclusive**, not a win.

### 6. IRQ steering

If frame pacing remains uneven, inspect `/proc/interrupts` and move `xe` and NVMe interrupts away
from the CPU(s) used by the main thread. This was already listed as a later step in
`docs/cpu-sched.md`; treat it as a tail-latency experiment, not the first suspect.

## Measurement protocol

Use MangoHud (or the Steam overlay) for FPS and clocks, plus the per-thread sampler above. For
Brilliant Diamond prefer the game's own `frame pacing measured` line when it is emitted, but note
that it currently appears only once per run, so MangoHud is the practical FPS source.

Record for every run:

- game/scene and duration
- scheduler name from `/sys/kernel/sched_ext/root/ops`
- FPS average and 1% low if available
- main-thread CPU %, CPU id, P/E class
- GPU clock/util
- package power if readable (RAPL energy is root-only on this install)
- whether the game window is borderless 1129x706 or native 1920x1200

## Acceptance criteria

A scheduler change is worth keeping only if it:

1. moves the hot game thread onto a P-core,
2. materially raises FPS toward the game's cap,
3. does not regress the GPU-bound games that `scx_cosmos` was measured on,
4. does not require per-title hacks that break other RecompLauncher games.

Otherwise keep `scx_cosmos` and record the result here.

## Pitfalls

- RecompLauncher re-runs the Luminescent Platinum mod setup on every launch. It can be forced to
  reinstall its mod files; do not launch Brilliant Diamond casually just for a measurement.
- Global scheduler/EPP changes affect the whole desktop. Restore `scx_cosmos` and the HHD
  governor after each experiment.
- The borderless 1129x706 window is a separate issue from scheduling. Test fullscreen/native
  1920x1200 independently, otherwise GPU utilisation changes can be mistaken for scheduler wins.
- `scx_lavd --balanced` has previously aborted with a runnable-task/rapl stall on this kernel;
  start with `--no-core-compaction` and verify the scheduler actually stays loaded.

## Related material

- `docs/PERFORMANCE-CPU-PLACEMENT.md` - research, P-core wrapper and scheduler profile
  switch (the actionable follow-up to this parked plan).

- `docs/PERFORMANCE-RECOMP.md` — desktop scaling and gamescope wrapper.
- `docs/cpu-sched.md` — earlier scheduler comparison for the GPU-bound case.
