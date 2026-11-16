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
