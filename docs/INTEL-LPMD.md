# intel-lpmd 0.1.1-test with per-process CPU affinity

Status: built and installer-tested (fake root); **root install + game test pending**.

## Why this instead of the packaged 0.1.0

Fedora ships `intel-lpmd 0.1.0`, which only drives Low Power Mode (`ON/OFF/AUTO`) and
has no per-process policy.  Upstream `0.1.1-test` adds the "Intel method" of hybrid
scheduling that we want:

- classifies processes (`background`, `utility`, `user_initiated`, `user_interactive`,
  `realtime`, `game_profile_cpu|gpu|mixed`, custom profiles),
- confines each class to P-cores / E-cores / LP-E cores (or literal CPU lists) using
  systemd cgroup cpusets for services and `sched_setaffinity` for session processes,
- applies uclamp per class, and supports focus-PID promotion.

Default class → cores (from `data/process_cpuset.xml`):
`background` → LP-E, `utility` → E + LP-E, `user_initiated` → P + E,
`user_interactive` → P + E + LP-E, `realtime` → P,
`game_profile_cpu` → P, `game_profile_gpu`/`mixed` → P + E.

That finally gives the LP-E cores a defined job (background work) instead of sitting
idle, and keeps games off LP-E.

## Build

```sh
./tools/build-intel-lpmd.sh
```

Clones `intel/intel-lpmd` (HEAD, version `0.1.1-test`), builds in a Fedora 45
container and stages to `~/.cache/oxp3-fixes/intel-lpmd/stage/{bin,data,libexec}`.

## Install / uninstall

```sh
sudo ./tools/install-intel-lpmd.sh
sudo ./tools/install-intel-lpmd.sh --uninstall
```

The installer:

- backs up `/etc/intel_lpmd` to `~/.local/state/oxp3-fixes/intel-lpmd/conf.tar`,
- installs the new binary to `/usr/local/bin` and a systemd drop-in
  (`intel_lpmd.service.d/90-oxp3-test.conf`) pointing the packaged unit at it,
- installs the upstream configs and sets `<UseProcessCPUSet>1</UseProcessCPUSet>`,
- seeds `process_cpuset_user.xml` with `udpre` / `uDPRE.exe` / `Dpr.exe` as
  `game_profile_cpu` and `eldenring.exe` as `game_profile_gpu`
  (`AllowSession=1`, `AffinityAllThreads=1`).

`--uninstall` restores the backup, removes the drop-in and the `/usr/local` binaries.

## Verify

```sh
sudo intel_lpmd_control STATUS
sudo intel_lpmd_control LIST-BOUND
sudo intel_lpmd_control GET-PROC-CLASSIFICATION udpre
ps -eo pid,comm,psr,ni | grep -E 'udpre|dolphin|kwin'
```

While a game runs, its threads should be in the P-core mask (cpu0-1 for
`game_profile_cpu`, cpu0-9 for `game_profile_gpu`), and background daemons should show
up on cpu10-13.

## Changing a game's class

Edit `/etc/intel_lpmd/process_cpuset_user.xml` (persistent; the daemon never touches
the system file) or at runtime:

```sh
sudo intel_lpmd_control ADD-PROCESS <comm> GameProfileMixed 1
sudo intel_lpmd_control ADD-PROCESS <comm> GameProfileCPU 1
```

`comm` is `/proc/PID/comm`, truncated to 15 characters (e.g. `eldenring.exe`).

## Caveats

- 0.1.1 is labelled a test release upstream ("do not include in any distro release").
  Keep the backup; `--uninstall` restores the packaged 0.1.0.
- `game_profile_cpu` hard-pins to the two P-cores.  If a title loses throughput,
  change it to `GameProfileMixed` (P + E) or `GameProfileGPU`.
- This does **not** give the kernel Thread Director; it is Intel's userspace
  classification/cpuset mechanism, the closest supported equivalent today.

## First live test (2026-11-16): why cpusets were not applied

The game (`Dpr.exe` under Proton, 81 threads) still had `Cpus_allowed_list: 0-13` on
every thread even though `/etc/intel_lpmd/process_cpuset_user.xml` had a `Dpr.exe`
entry.  Cause: lpmd's periodic process rescan is **skipped while the LPM state is
`OFF`** (`src/lpmd_proc.c`: `use_process_cpuset && get_lpmd_state() != LPMD_OFF`),
and this platform's config sets every mode to `-1`, so the state never leaves `OFF`.
`UseProcessCPUSet=1` alone only attaches PIDs present at daemon start.

Fix: activate per-process mode explicitly:

```sh
sudo intel_lpmd_control PROCESS-PRECONFIG
```

The installer now also enables a oneshot `intel_lpmd-preconfig.service`
(`ExecStart=/usr/local/bin/intel_lpmd_control PROCESS-PRECONFIG`, `PartOf=intel_lpmd`)
so this is re-applied on every boot and on lpmd restart.

Default game classes were changed to `game_profile_mixed` (P+E, cpu0-9) for
`udpre`/`uDPRE.exe`/`Dpr.exe`: the BD process has 81 threads, and hard-pinning all of
them to the two P-cores (`game_profile_cpu`) is likely to lose throughput.

Measured while the game ran with lpmd still in `OFF`:

- 81 threads, all affinity 0-13.
- Main `Dpr.exe` ~63 % of one core, scheduled on **cpu10 (LP-E)**.
- `UnityGfxDeviceW` ~9 %, `dxvk-cs` ~9 %, 12 `Job.Worker` ~3 % each.
- A transient burst at level load had `Dpr.exe:disk$0..3` at ~100 % each, then they
  went back to `futex_do_wait`.
- GPU 33 % at 1133 MHz -> not GPU-bound, and the main thread is not saturated, so the
  frame rate is limited by the engine/frame cap (or the load burst), not by CPU
  placement alone.

## Second live test: cpusets attach, but P-cores are not detected

After switching lpmd into `PROCESS-PRECONFIG` (the debug log showed
`proc-connector listener active` and `rescan attached 138 new PIDs`), the game became
bound — but to **cpu2-13**, not the P-cores:

```text
process_cpuset: tuning: game_profile_cpu   mask=0x00 <none>
process_cpuset: tuning: game_profile_mixed mask=0x50 ...
pid ... current affinity list: 2-13
```

`game_profile_cpu` with no CPUs means lpmd's automatic P-core detection returns an
empty set on this Panther Lake (F6/M204) platform, so `game_profile_mixed`
(`ActivePcores, ActiveEcores`) degraded to E + LP-E only.

Fix: declare the groups explicitly in the user config; the parser takes them from
`<CpuGroups>` regardless of position:

```xml
<CpuGroups>
    <ActivePcores>0-1</ActivePcores>
    <ActiveEcores>2-9</ActiveEcores>
    <ActiveLcores>10-13</ActiveLcores>
</CpuGroups>
```

`tools/install-intel-lpmd.sh` now seeds this block into
`/etc/intel_lpmd/process_cpuset_user.xml` alongside the game entries.  Re-run it, then
`PROCESS-PRECONFIG` rebinds the running processes; the game should show
`Cpus_allowed_list: 0-9` (mixed) or `0-1` (cpu), and background PIDs `10-13`.

The D-Bus control interface rate-limits repeated privileged calls
("too many privileged requests"); space them out, the boot-time
`intel_lpmd-preconfig.service` avoids the manual call.

## Third test: the shipped ClassDefaults are the surprise

Even with `<CpuGroups>` seeded, lpmd classified the game as
`game_profile_mixed groups=Ecores+LPEcores`.  The shipped `process_cpuset.xml`
`<ClassDefaults>` block actually maps:

```text
GameProfileCPU   -> ActivePcores
GameProfileGPU   -> ActiveLcores          (not P+E as the comments claim)
GameProfileMixed -> ActiveEcores,ActiveLcores  (not P+E)
```

So "mixed" was never P+E in this release.  The installer now overrides them in
`process_cpuset_user.xml`:

```xml
<ClassDefaults>
    <GameProfileCPU><Cores>ActivePcores</Cores></GameProfileCPU>
    <GameProfileGPU><Cores>ActivePcores,ActiveEcores</Cores></GameProfileGPU>
    <GameProfileMixed><Cores>ActivePcores,ActiveEcores</Cores></GameProfileMixed>
    <Background><Cores>ActiveLcores</Cores></Background>
</ClassDefaults>
```

It also restarts `intel_lpmd-preconfig.service` after restarting lpmd (the oneshot is
already active/exited, so `enable --now` alone did not re-enter PROCESS-PRECONFIG).

## Fourth test: the model config wins over the user overlay

Even with the user `<ClassDefaults>` override and explicit `<CpuGroups>`, the game
still attached to `2-13`.  The precedence chain is:

```text
system process_cpuset.xml  <  user process_cpuset_user.xml  <  model intel_lpmd_config_F6_M204.xml
```

and the model file contains its own `<ClassDefaults>`:

```xml
<GameProfileMixed><Cores>ActiveEcores, ActiveLcores</Cores></GameProfileMixed>
<GameProfileGPU>  <Cores>ActiveEcores, ActiveLcores</Cores></GameProfileGPU>
```

which overrides both lower layers.  `tools/install-intel-lpmd.sh` now rewrites those
two model entries to `ActivePcores, ActiveEcores` after installing the configs (the
installer re-copies the upstream data on every run, so this has to happen after the
copy).

Expected end state for a `game_profile_mixed` title: `Cpus_allowed_list: 0-9`.

## Persistence

Everything the installer writes is on persistent storage and enabled:

- `/etc/intel_lpmd/` - configs, `process_cpuset.xml`, the user overlay with
  `<CpuGroups>` + game entries, and the patched `intel_lpmd_config_F6_M204.xml`
  (model `ClassDefaults` = P+E for `GameProfileMixed`/`GameProfileGPU`).
- `/usr/local/bin/intel_lpmd` - `/usr/local` is `../var/usrlocal` on this ostree
  system, so it survives OS updates.
- `/etc/systemd/system/intel_lpmd.service.d/90-oxp3-test.conf` - points the packaged
  unit at the new binary.
- `/etc/systemd/system/intel_lpmd-preconfig.service` - `enabled`, runs
  `intel_lpmd_control PROCESS-PRECONFIG` after lpmd starts (retries through the
  D-Bus rate limit).
- `oxp3-gaming-sched.service` remains enabled separately for the scheduler.

So a reboot or a switch into Gamemode keeps the behaviour: lpmd starts at
`multi-user.target`, the preconfig oneshot puts it in `PROCESS-PRECONFIG`, and the
proc-connector/rescan binds `Dpr.exe` to `0-9` when it launches.

Caveats:

- A distro update that reinstalls `intel-lpmd` (or a full `oxp3-install.sh` run) can
  restore the packaged `/etc/intel_lpmd` files; re-run
  `sudo tools/install-intel-lpmd.sh` afterwards.
- A game launched *before* the preconfig oneshot finishes can keep the old mask until
  the next rescan (up to 60 s); the proc-connector usually catches it immediately.
- If the oneshot ever fails, `sudo systemctl restart intel_lpmd-preconfig` re-applies
  the state; `PROCESS-PRECONFIG` is not persisted in lpmd itself, only in the unit.

## Confirmed result on Brilliant Diamond (2026-11-16)

User report after the `CpuGroups` + model `ClassDefaults` fix: **massive performance
improvement** in the Brilliant Diamond recomp.

One caveat observed on the next launch: lpmd was demonstrably active (background
daemons such as `dbus-broker`, `accounts-daemon`, `chronyd` were on `10-13`), but the
new game instance (`Dpr.exe`, 80 threads) still had `Cpus_allowed_list: 0-13`, with the
main thread at 66 % on cpu5 and GPU at 47 %/683 MHz.  So per-launch auto-attach is not
yet reliable for this user-service process, even though the class/cpuset config is
correct and a restart rebinds it.

If a launch comes up unbound:

```sh
sudo intel_lpmd_control ADD-PROCESS Dpr.exe GameProfileMixed 1
taskset -pc "$(pgrep -x Dpr.exe)"
```

or restart `intel_lpmd` after the game is running (the init scan binds existing PIDs).
The remaining work is making the proc-connector/periodic-rescan path catch this
process reliably on every launch.

## Covering every game (there is no game database)

`intel-lpmd` does **not** ship a game database.  `data/process_cpuset.xml` contains only
commented examples (`Spaceship*`, `Strange*`, `ShadowOfTheTomb*`, `_hitman*`), and the
classification is purely `comm`-name based.  So the coverage strategy is:

1. **Explicit entries** for titles we want a specific class/mask for, currently
   `udpre`/`uDPRE.exe`/`Dpr.exe` → `game_profile_mixed`, `eldenring.exe` →
   `game_profile_gpu`.
2. **A catch-all `<DefaultProcess>`** for every unmatched user-session process.  It is
   classified as `custom_profile_0`, whose `<ClassDefaults>` entry is
   `ActivePcores, ActiveEcores` (cpu0-9), so any game not named in the file still avoids
   LP-E and stays on P+E.

`user_initiated` was deliberately not used for the catch-all: with no focus helper
present, lpmd makes `user_initiated` mirror `user_interactive`, which this platform's
model config maps to E+LP-E.  `custom_profile_0` with an explicit `<Cores>` avoids that
and carries no game-specific tuning.

The catch-all also resets other unmatched user apps (compositor, browser, terminal) to
P+E; system-slice daemons are not touched by `<DefaultProcess>` and keep the named
`background` → LP-E mapping.

If a specific game later needs a different mask (e.g. P-only for a single-threaded
title), add a `<Process>` entry with `game_profile_cpu`; otherwise the catch-all covers
it.

## Catch-all parsing order bug

`<DefaultProcess>` is parsed in the first pass, before the overlay
`<ClassDefaults>` block is applied, so its mask was resolved from the **system**
`custom_profile_0` default (all CPUs) and then never re-resolved.  The catch-all
therefore appeared inert.  Fix: give it an explicit
`<ActiveCores>ActivePcores,ActiveEcores</ActiveCores>` (groups are resolved at attach
time from `<CpuGroups>`), which is independent of the ClassDefaults ordering.

## Catch-all verified (2026-11-16)

After the explicit-mask fix, a normal user shell and every unmatched Wine process
resolved to P+E:

```text
shell            0-9  custom_profile_0
Dpr.exe          0-9  game_profile_mixed   (explicit entry)
UnityCrashHandl  0-9  custom_profile_0
services.exe / winedevice.exe / explorer.exe ...  0-9  custom_profile_0
```

`tools/lpmd-game-status.sh` reports both the affinity and the lpmd classification, so a
future launch can be checked at a glance.  Coverage is therefore:

- named games -> their explicit class (currently also 0-9),
- **any other game** -> catch-all `custom_profile_0` 0-9,
- desktop/Wine helper processes -> catch-all 0-9 (intentional),
- system daemons -> 10-13.

## Elden Ring crash and anti-cheat exemptions

After the catch-all became active, Elden Ring started crashing at startup.  Two
risky behaviours were removed:

1. The catch-all attached `eldenring.exe`, `start_protected_game.exe` and the EAC
   processes to cpu0-9.  Anti-cheat + affinity changes are fragile; they now have
   explicit `<Process>` entries with `<ActiveCores>0-13</ActiveCores>` so they are
   left on all CPUs (the fragment lives in `tools/lpmd-exempt.xml` and is appended by
   the installer).
2. The model `GameProfileGPU` block had
   `<OverrideGlobalCPUSettings>1</OverrideGlobalCPUSettings>`, which lets the class
   rewrite the `system.slice`/`user.slice`/`machine.slice` cpusets while attached.
   That is far too broad for a game class; the installer removes the tag and
   `eldenring.exe` is no longer classified as `game_profile_gpu`.

`eldenring.exe` is now `custom_profile_0` with all CPUs, i.e. deliberately not
managed by lpmd until the crash is understood.

Diagnosis if it still crashes:

```sh
sudo systemctl stop intel_lpmd intel_lpmd-preconfig   # no lpmd at all
# launch Elden Ring; if it runs, lpmd is implicated
sudo systemctl start intel_lpmd
sudo systemctl restart intel_lpmd-preconfig
```

Also try without MangoHud: `mangoapp` aborted (SIGABRT) around the same period.
