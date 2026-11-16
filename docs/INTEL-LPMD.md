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
