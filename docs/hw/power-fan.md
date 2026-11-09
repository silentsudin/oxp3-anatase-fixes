# Power envelope and fan control on the OneXPlayer 3 (PL1/PL2/PL4/τ and the fan)

Target: the vendor's three power presets (Quiet / Balanced / Performance, i.e. PL1 10/25/35 W with
PL2 25/45/80 W and PL4 90/160/239 W) and manual/curve fan control, **entirely from userspace, with no
kernel patch**. This file records what the machine actually exposes, what the vendor does, how to use
[`tools/oxp3-powerctl.py`](../../tools/oxp3-powerctl.py), how to validate it and how to roll back.

Measured on 2026-11-09 on kernel `7.2.7-an28.fc45.x86_64` (Anatase, 14 threads) with hhd 4.1.12
running. **Nothing in this document has been written to the host from this checkout**: all writes need
root, the `sudo` lines below are the exact commands, and the first real write test is §8.2.

- Tool: [`tools/oxp3-powerctl.py`](../../tools/oxp3-powerctl.py) (installed as `/usr/local/libexec/oxp3-powerctl`)
- Units: [`files/oxp3-power-profile.service`](../../files/oxp3-power-profile.service) (boot),
  [`files/oxp3-power-resume.service`](../../files/oxp3-power-resume.service) (resume),
  [`files/oxp3-fan-curve.service`](../../files/oxp3-fan-curve.service) (opt-in curve daemon)
- Installer: [`tools/oxp3-power-install.sh`](../../tools/oxp3-power-install.sh)
- Prior evidence: [`docs/hw/gap-audit.md`](gap-audit.md) §2.1 (perf_conf.ini, PerfMode.exe) and §2.2
  (CompatLayerCT EC field map), rows #2/#3 of the gap table.

## TL;DR

1. **The whole vendor power envelope is already reachable from Linux.** powercap exposes *both* RAPL
   package zones the vendor writes (`intel-rapl:0` = MSR, `intel-rapl-mmio:0` = MMIO), each with
   writable `constraint_0/1/2_power_limit_uw` (PL1/PL2/PL4) and `constraint_0_time_window_us` (τ).
   PerfMode.exe needs its kernel driver only because Windows has no powercap; it is the same MSRs
   (`MSR_PKG_POWER_LIMIT` 0x610, PL4 in `MSR_VR_CURRENT_CONFIG` 0x601) that `intel_rapl_msr` writes.
   **No kernel work is needed for PL1/PL2/PL4/τ.**
2. Today both zones read PL1 35 W (τ 27.98 s), MSR PL2 37 W, MMIO PL2 52 W, PL4 122 W — not one of the
   vendor presets (the 35 W sync in this repo is PL1-only). `oxp3-powerctl profile {quiet,balanced,performance}`
   writes all three limits to both zones and verifies the readback.
3. **The fan is controllable, but only crudely.** `hwmon6` (`oxp_ec`) gives `fan1_input`, `pwm1`
   (0–255, scaled by the driver to the EC's 0–184 on this board) and `pwm1_enable` (0 full / 1 manual /
   2 EC auto). Manual PWM requires `tt_toggle=1`, which hhd already sets. The vendor's three EC fan
   **curves** (`DevsCPUFanCurve`, `DevsGPUFanCurve`, `DevsMidFanCurve`) and the `addrFanAutomate` /
   `addrFanSpeed` fields have **no Linux interface**; a userspace curve daemon can approximate an
   intelligent curve, and exposing the real EC curves needs a small `oxpec` extension (EC offsets
   unknown, §5.4).
4. `acpi_fan`'s `fan1_input` returns `EIO` and `TFN1`'s `cur_state` returns `EIO` — both are known
   cosmetic/firmware issues, not the fan (use `oxp_ec`).

## 1. What the machine exposes (read-only inventory)

`ls -l` modes and values, 2026-11-09 00:2x UTC. "writable?" is what the file mode plus a
non-destructive `open(O_WRONLY)` probe says; the tool re-probes this every `status` run, so it never
assumes a control exists.

| interface | sysfs path | value now | mode | writable by root | note |
|---|---|---|---|---|---|
| MSR PL1 (long_term) | `/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw` | 35 000 000 | `-rw-r--r-- root` | yes | 1 µW units |
| MSR τ (PL1 window) | `…/intel-rapl:0/constraint_0_time_window_us` | 27 983 872 (27.98 s) | `-rw-r--r-- root` | yes | quantised, §2.4 |
| MSR PL2 (short_term) | `…/intel-rapl:0/constraint_1_power_limit_uw` | 37 000 000 | `-rw-r--r-- root` | yes | |
| MSR PL2 window | `…/intel-rapl:0/constraint_1_time_window_us` | 976 | `-rw-r--r-- root` | yes | minimum window = 1 time unit |
| MSR PL4 (peak_power) | `…/intel-rapl:0/constraint_2_power_limit_uw` | 122 000 000 | `-rw-r--r-- root` | yes | PL4 via `MSR_VR_CURRENT_CONFIG` |
| MSR PL4 window | `…/intel-rapl:0/constraint_2_time_window_us` | `ENODATA` on read | `-rw-r--r-- root` | n/a | not implemented for PL4 |
| MMIO PL1/τ/PL2/PL4 | `/sys/class/powercap/intel-rapl-mmio:0/constraint_*` | 35 / 27.98 s / 52 / 122 | same as above | yes | separate zone, same numbers |
| zone switch | `…/{intel-rapl:0,intel-rapl-mmio:0}/enabled` | 1 | `-rw-r--r-- root` | yes | leave 1 |
| energy counters | `…/*/energy_uj` | root-only (`-r--------`) | | no (by design) | published by `rapl-power` as `/run/rapl-power/*` |
| fan rpm | `/sys/class/hwmon/hwmon6/fan1_input` | 1397 | `-r--r--r-- root` | no | `oxp_ec` |
| fan duty | `…/hwmon6/pwm1` | 51 | `-rw-r--r-- root` | yes | 0–255 → EC 0–184 |
| fan mode | `…/hwmon6/pwm1_enable` | 2 (auto) | `-rw-r--r-- root` | yes | 0 full / 1 manual / 2 EC auto |
| turbo takeover | `/sys/devices/platform/oxp-platform/tt_toggle` | 1 | `-rw-r--r-- root` | yes | prerequisite for PWM (hhd sets 1) |
| ACPI platform profile | `/sys/firmware/acpi/platform_profile` | `balanced` | `-rw-r--r-- root` | yes | choices `low-power balanced performance` |
| CPU EPP | `/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference` | `balance_power` | `-rw-r--r-- root` | yes | hhd owns this, the tool does not touch it |
| `acpi_fan` rpm | `/sys/class/hwmon/hwmon1/fan1_input` | **`EIO`** | `-r--r--r-- root` | no | known cosmetic bug |
| `TFN1` cooling dev | `/sys/class/thermal/cooling_device2/cur_state` | **`EIO`** (max 50) | | | firmware bug, unusable |

Other read-only facts that matter:

- `constraint_0_max_power_uw` reports **25 W** and `constraint_1/2_max_power_uw` report **0**, yet PL1
  35 W is applied and sticks. powercap does not clamp writes to `max_power_uw` (the value is only
  informational, read from the firmware's MSR field), so it is not a ceiling for this tool.
- The kernel quantises the τ field as `2^y · (1 + f/4) · time_unit`, and `time_unit` here is **976 µs**
  (`constraint_1_time_window_us` reads exactly 976). A requested window is therefore rounded; the tool
  reads it back and prints the quantised value rather than claiming the request.
- PL4 is exposed because this CPU uses `rapl_defaults_core_pl4_pmu` (`msr_pl4_support = 1`) in
  `drivers/powercap/intel_rapl_msr.c`, which adds `POWER_LIMIT4` on `MSR_VR_CURRENT_CONFIG` (0x601).
  powercap **cannot** toggle the PL2/PL4 *enable* bits — only their limits and PL1's window. The
  firmware has those enables set (the current 122 W PL4 and 52 W MMIO PL2 are live), so this is not a
  limitation in practice; see open point 3.
- The RAPL energy counters stay root-only (CVE-2020-8694). `rapl-power` is the only reader and
  publishes 1 s/0.1 W averages in `/run/rapl-power/`; the dynamic test in §8.3 uses those.

## 2. Vendor evidence (what the Windows stack writes)

### 2.1 The preset table is a plain config file

`resources/resources/tools/intel/perf_conf.ini` inside OneXConsole 0.10.4
(`$PLUGINSDIR/app-64.7z`, 540 bytes; extracted to a scratch dir under `~/.cache`, **not** committed):

| key | Quiet | Balanced | Performance |
|---|---:|---:|---:|
| `pl1msr` / `pl1mmio` | 10 / 10 | 25 / 25 | 35 / 35 |
| `pl2msr` / `pl2mmio` | 25 / 25 | 45 / 45 | 80 / 80 |
| `pl1taumsr` / `pl1taummio` | 56 / 56 | 56 / 56 | 56 / 56 |
| `pl4` | 90 | 160 | 239 |
| `fanSpeed` | 0 | 0 | 200 |
| `sagvPoint` | 3 | 0 | 4 |

plus `[auto_config] GPU_UTIL_THRESHOLD_LOW=80.00`, `GPU_UTIL_THRESHOLD_HIGH=90.00`, `PL1_THRESHOLD=25`,
`TIME_INTERVAL=10000`. All limits are in **watts**, the same unit powercap wants converted to µW.

### 2.2 PerfMode.exe writes the same registers powercap exposes

From the earlier Ghidra run ([`gap-audit.md`](gap-audit.md) §2.1, output kept outside the repo):

- strings `MSR Access`, `MMIO Access`, `pl1msr`, `pl1mmio`, `pl2msr`, `pl2mmio`, `pl1taumsr`,
  `pl1taummio`, `pl1peci`, `GetCurrentPL1`, `PL4 Power read failed`, `PL4 writing failed`,
  `SAGVPointToSet=`, `SAGVPointCurrent=`, `AutoSAGVPolicy`.
- `HWAPIExtDLL.dll` (next to it) exports the generic hardware helpers it calls: `ReadMSR`, `WriteMSR`,
  `ReadPort`, `WritePort`, `PCIExRead`, `PCIExWrite`, `ReadMMIOStored`, `WriteMMIOStored`, `BindCPU` —
  and nothing power-policy-specific.
- `CCHWAPIExt.sys` is the kernel wrapper for those helpers.

So the vendor writes `MSR_PKG_POWER_LIMIT`/`MSR_VR_CURRENT_CONFIG` and the MMIO RAPL block directly.
`intel_rapl_msr` already owns exactly those two interfaces and exposes them through powercap, which is
why this tool needs no kernel component. (Windows needs its driver because raw `wrmsr` from user mode
does not exist there.)

### 2.3 Fan / EC field names (CompatLayerCT.exe, managed)

```
addrFanSpeed, addrFanAutomate, DevsCPUFan, DevsCPUFanCurve,
DevsGPUFan, DevsGPUFanCurve, DevsMidFanCurve, fanSetFanSpeed, fanGetFanSpeed,
FAN_MAX_SPEED_VALUE, fanSpeedHECAddr/fanSpeedLECAddr, addrHandlePower,
addrCpuTemp, addrBoardSensor1/2, addrBypassPower, addrPowerSupplyMode
```

and the UI strings `cooling_runmode_intelli` ("intelligent fan curve"), `cooling_runmode_fixspeed`,
`qs_fan_presetting1/2`, `qs_fan_lock`. That is: an auto/fixed mode switch, a fan-speed register and
**three** curve tables (CPU, GPU, mid) in EC RAM, plus a fan "lock".

Linux's `oxpec` driver (visible in the module build tree,
`drivers/platform/x86/oxpec.c`) only exposes the *speed* half of that: for `ONEXPLAYER 3` the DMI
table maps to board `oxp_g1_i`, which uses EC `0x76` (fan tach, 2 bytes), `0x4A` (mode: `0x00` auto /
`0x01` manual) and `0x4B` (duty, driver-scaled from 0–255 to 0–184). There is no attribute for the
curve tables, no per-partition (CPU/GPU/mid) control and no preset/lock API. hhd only writes
`tt_toggle` (`hhd/device/oxp/__init__.py`) — it has no powercap or TDP path on this model (grep of
`/usr/lib/python3.15/site-packages/hhd` finds no `powercap`/`intel-rapl` use).

### 2.4 The one unproven number: the τ unit

`pl1tau*=56` is the only vendor value without a stated unit. The same ini uses ms for
`TIME_INTERVAL=10000` (10 s), but a **56 ms PL1 window would make the vendor's own PL2 pointless**: PL1
is the averaged limit, so with τ = 56 ms the package would be clamped to PL1 after ~55 ms and the
PL2 = 80/160/239 W setting could never sustain a burst. Multi-second windows (5.6 s if the vendor
counted 0.1 s, or 56 s if seconds) are the physically sensible readings; 56 s is also exactly 2× the
firmware's current 27.98 s. **We therefore read 56 as seconds**, and:

- `profile` applies **PL1/PL2/PL4 only** by default (no guessing with the τ unit),
- `--with-tau` writes the vendor 56 s, `--tau 56ms` / `settau 56ms` write any explicit value,
- the tool always shows the quantised readback, so the experiment below is unambiguous.

Calibration (one command pair, reversible):

```bash
sudo oxp3-powerctl setpl1 35 --tau 56ms   # readback ~54.656 ms
cat /sys/class/powercap/intel-rapl:0/constraint_0_time_window_us
sudo oxp3-powerctl setpl1 35 --tau 56s    # readback ~55.97 s
cat /sys/class/powercap/intel-rapl:0/constraint_0_time_window_us
```

Then run the §8.3 stress test with each: with the 56 ms window, package power should sit at PL1 almost
immediately and PL2 never shows up in `/run/rapl-power/package-0`; with a multi-second window it should
ride PL2 for seconds first. Keep the one that matches the vendor's intent (bursts allowed, average at
PL1) — that is the whole practical difference.

## 3. Profiles

| profile | PL1 | PL2 | PL4 | vendor τ | vendor `fanSpeed` | platform_profile | intended use |
|---|---:|---:|---:|---:|---:|---|---|
| `quiet` | 10 W | 25 W | 90 W | 56 | 0 (leave EC) | `low-power` | battery / silent |
| `balanced` | 25 W | 45 W | 160 W | 56 | 0 (leave EC) | `balanced` | default |
| `performance` | 35 W | 80 W | 239 W | 56 | 200 (opt-in) | `performance` | docked / demanding games |

- Both zones are always written (MSR first, then MMIO), because either one can be the one the firmware
  honours and the lower of the two wins on this machine (that is also why `oxp3-tdp-sync` exists).
- Write order is chosen per value: raising writes PL4 → PL2 → PL1 (open the ceiling first), lowering
  writes PL1 → PL2 → PL4 (close the floor first).
- `--fan` additionally sets manual PWM to the vendor's `fanSpeed` (only `performance` has one, 200/255
  → EC 144 on this board). It is opt-in because it pins the fan in manual mode.
- `--platform-profile` (optionally with a name) also writes the ACPI platform profile; this is what the
  vendor's `intelPnPMode1/2/3` maps onto. A missing/unwritable `platform_profile` is a warning, never a
  failure.
- `--save` writes the profile name to `/etc/oxp3/power.json`, which is what the boot/resume units
  re-apply. `setpl1/2/4 ... --save` stores explicit overrides that `restore` applies after the profile.

## 4. Tool usage

```
oxp3-powerctl status [--json]
oxp3-powerctl profile {quiet|balanced|performance} [--tau T] [--with-tau]
                      [--platform-profile [NAME]] [--fan] [--save]
                      [--dry-run] [--json] [--allow-partial]
oxp3-powerctl setpl1|setpl2|setpl4 W [--tau T] [--save] [--dry-run] [--json]
oxp3-powerctl settau T                    # T = 56ms / 5.6s / 28000us / bare seconds
oxp3-powerctl fan status
oxp3-powerctl fan auto
oxp3-powerctl fan manual [PWM]
oxp3-powerctl fan speed PWM               # 0-255, forces manual mode
oxp3-powerctl fan curve [--file F] [--interval S] [--steps N] [--once]
                        [--sensor PATH] [--hysteresis N] [--no-restore] [--dry-run] [--json]
oxp3-powerctl snapshot [--save]           # print/save the current limits as a rollback baseline
oxp3-powerctl restore-baseline [--dry-run]
oxp3-powerctl restore [--dry-run]         # what the units run
```

Behaviour worth knowing:

- **It never assumes it can write.** Reads are unprivileged; every write path goes through
  `open(O_WRONLY)` and reports the kernel's errno. As a non-root user `profile`/`setpl1`/`fan auto`
  exit 2 with `run with sudo`; a BIOS-locked limit (RAPL lock bit) would exit 2 with the EACCES
  explanation even as root.
- **Missing controls are reported, not faked.** A constraint that this kernel/CPU does not expose is
  listed by `status`, and write commands fail with a clear message unless `--allow-partial` is given.
- **Readback verification.** Every write command reads the value back; a mismatch exits 3 and names the
  domain/level (`readback mismatch: wanted 239.000 W, reads …`). τ readback is shown quantised instead.
- **`--dry-run`** prints exactly what would be written (same order, both zones) without opening
  anything for write; it works as a normal user.
- **`--json`** prints one JSON document (`status`, `profile`, `setpl*`, `fan status`/`curve`); errors
  become `{"ok": false, "error": …, "hint": …}`. Exit codes: 0 ok, 2 missing control/privileges,
  3 readback mismatch, 4 partial status.

Examples:

```bash
tools/oxp3-powerctl.py status
sudo tools/oxp3-powerctl.py profile balanced --save
sudo tools/oxp3-powerctl.py profile performance --dry-run
sudo tools/oxp3-powerctl.py setpl2 60 --json
sudo tools/oxp3-powerctl.py fan speed 180
sudo tools/oxp3-powerctl.py fan curve --file /etc/oxp3/fan-curve --interval 2
```

`/etc/oxp3/fan-curve` is one `<degC> <pwm>` pair per line (`#` comments); missing → built-in
`[(40,60),(55,100),(70,160),(85,220),(95,255)]`. The curve interpolates on the CPU package temperature
(`coretemp` "Package id 0", fallback `TCPU`/`acpitz`), writes only on ≥ hysteresis change (default 3),
sets `pwm1_enable=1` while running and returns the EC to automatic (`pwm1_enable=2`) on Ctrl-C/SIGTERM
unless `--no-restore`.

## 5. The fan: what works, what does not, what a kernel patch would add

### 5.1 What works today (no kernel change)

- `fan1_input` — real rpm (1 388–2 414 rpm observed).
- `pwm1` — manual duty 0–255 (EC 0–184 after the driver's `(val * 184) / 255`).
- `pwm1_enable` — `1` manual, `2` EC automatic, `0` full speed (the driver enables manual and writes
  max duty).
- Curve *emulation*: `fan curve` is a userspace PID-free table loop; it is not the EC curve, but it is
  a real, observable fan curve.

Prerequisite: `tt_toggle=1`. The driver's ABI doc says PWM control NOOPs otherwise, and hhd sets it at
startup (`hhd/device/oxp/__init__.py`, also after a hibernation wakeup). The tool warns in `status`,
`fan` and `profile --fan` if it is 0, and the fix is `echo 1 | sudo tee /sys/devices/platform/oxp-platform/tt_toggle`.

Caveats:

- In automatic mode `pwm1` may read back a stale EC value and is not a duty you can trust; read
  `pwm1_enable` first.
- The EC has its own ramp/time constant (seconds to tens of seconds); a duty sweep needs ~30 s per step
  to be meaningful.
- The fan has a firmware minimum at low duty — 0 does not stop it.

### 5.2 What is not reachable

- The three EC **curve tables** (`DevsCPUFanCurve/GPUFanCurve/MidFanCurve`), the auto/fixed switch
  (`addrFanAutomate`), the fan "lock" and the per-partition fans: no sysfs attribute, no ACPI method,
  no hwmon channel. `oxpec` only has the three attributes above.
- The EC's own temperature/board sensors (`addrCpuTemp`, `addrBoardSensor1/2`) — not exposed either.
- Direct EC RAM from userspace: `ec_sys` is **not built** in this kernel (`modinfo ec_sys` → not
  found, `/sys/module/ec_sys` absent), and `/sys/kernel/debug` is root-only so the ACPI EC debugfs
  interface could not even be inspected. There is no supported userspace EC window here, and poking
  EC RAM behind the driver's back would race `acpi_ec` anyway.
- `acpi_fan.fan1_input` returns `EIO` and `TFN1` (`cooling_device2`) `cur_state` returns `EIO` with
  `max_state=50`; both are firmware/ACPI bugs unrelated to the `oxp_ec` fan (worth reporting upstream,
  not fixable in userspace).

### 5.3 Read-only monitoring path

```bash
cat /sys/class/hwmon/hwmon6/fan1_input                 # rpm
cat /sys/class/hwmon/hwmon6/pwm1 /sys/class/hwmon/hwmon6/pwm1_enable
cat /sys/class/hwmon/hwmon7/temp1_input                # coretemp package, mC
for z in /sys/class/thermal/thermal_zone*; do echo "$(cat $z/type) $(cat $z/temp)"; done
cat /run/rapl-power/package-0                          # 1 s average, W (rapl-power service)
```

`tools/oxp3-status.sh` and the Decky plugin already show rpm/temps; this tool's `status` adds the
powercap and writability view.

### 5.4 What kernel work would be needed (not done here)

To expose the vendor fan behaviour in `oxpec` (upstream `drivers/platform/x86/oxpec.c`), in increasing
order of effort:

1. **`fan1_target`/`pwm1` already exist** — nothing to do.
2. **EC curve tables**: add three binary/RW attributes (`fan_curve_cpu`, `fan_curve_gpu`, `fan_curve_mid`)
   plus a mode attribute, writing the EC through the driver's existing `write_to_ec()`/`read_from_ec()`
   helpers. The **EC offsets and the register layout are unknown**: `CompatLayerCT.exe` gives the field
   *names*, not the addresses (the native EC driver object is not in the NSIS payload). Needed first:
   - a Windows-side EC trace (RWEverything/`inpoutx64` port 0x62/0x66 trace), or the kernel's ACPI EC
     debugfs interface (`CONFIG_ACPI_EC_DEBUGFS`) on a debug kernel, or
   - an ACPI `_Qxx`/DSDT read on the Windows side (the EC methods may live in an SSDT behind the
     unbound WMI GUIDs — see gap-audit open points).
3. **Per-partition fans / fan lock / board sensors**: same EC-address problem; a driver change only
   after the map is known.
4. **`tt_toggle`-style prerequisites** are already handled.

Because the offsets are the blocker, this repo ships the userspace curve emulation instead of a
speculative driver patch, and this document records the gap rather than guessing registers.

## 6. systemd units and interaction with the existing stack

| unit | when | what it runs |
|---|---|---|
| `oxp3-power-profile.service` | boot, `After=hhd.service oxp3-tdp-sync.service`, only if `/etc/oxp3/power.json` exists | `oxp3-powerctl restore` |
| `oxp3-power-resume.service` | resume (`WantedBy=sleep.target`, `After=sleep.target`) | `oxp3-powerctl restore` |
| `oxp3-fan-curve.service` | opt-in, long-running | `oxp3-powerctl fan curve --interval 2` |

They are `oneshot` with a `ProtectSystem=strict` + `ProtectKernelTunables=yes` profile and explicit
`ReadWritePaths=/sys/devices/virtual/powercap /sys/devices/platform/oxp-platform /sys/firmware/acpi`
(the same pattern `oxp3-tdp-sync.service` already uses to write powercap).

Interaction with what is already installed:

- **hhd** — writes `tt_toggle=1` and owns EPP/governor (`balance_power`/`powersave`); it has no
  powercap/TDP path on the OXP3 (`grep` of the installed hhd finds none), so it does not fight the
  profile. It re-writes `tt_toggle` after a hibernation wakeup, which is why the fan units are ordered
  `After=hhd.service`. If hhd ever gains an Intel TDP path, the boot profile becomes a starting point
  and the tool should be re-run (or that path disabled) — noted as an open point.
- **`oxp3-tdp-sync`** — copies the **MMIO PL1** to the **MSR PL1** every 2 s when they differ. The tool
  always writes both, so in steady state the sync sees equal values and does nothing; if a user writes
  only the MMIO side by hand (e.g. with hhd's old path), the sync still does its job. Keep it enabled.
- **`rapl-power`** — read-only (`energy_uj`) publisher for MangoHud; the tool never touches energy
  counters. `oxp3-rapl-pp1` only reloads `intel_rapl_msr` once at boot; no interaction.
- **`thermald`** — the installer already disables it (no PSVT tables). Nothing here re-enables it.
- **Suspend** — firmware/EC re-initialise the RAPL limits on resume, hence the resume unit. If the
  resume unit is disabled, re-run `profile` after resume.

## 7. Install / uninstall

```bash
# from the repo root
tools/oxp3-power-install.sh install              # tool + boot + resume units (curve service not enabled)
tools/oxp3-power-install.sh install --fan-curve  # + the opt-in fan curve service
tools/oxp3-power-install.sh uninstall
```

Equivalent manual steps (root, in order):

```bash
sudo install -D -m 0755 tools/oxp3-powerctl.py /usr/local/libexec/oxp3-powerctl
sudo install -m 0644 files/oxp3-power-profile.service /etc/systemd/system/
sudo install -m 0644 files/oxp3-power-resume.service  /etc/systemd/system/
sudo install -m 0644 files/oxp3-fan-curve.service     /etc/systemd/system/   # optional
sudo systemctl daemon-reload
sudo systemctl enable oxp3-power-profile.service oxp3-power-resume.service
sudo systemctl enable --now oxp3-fan-curve.service    # optional
sudo /usr/local/libexec/oxp3-powerctl profile balanced --save   # creates /etc/oxp3/power.json
sudo systemctl start oxp3-power-profile.service                 # apply now, no reboot
```

## 8. Validation

### 8.1 Read-only (safe, no root)

```bash
tools/oxp3-powerctl.py status
tools/oxp3-powerctl.py status --json
tools/oxp3-powerctl.py profile performance --dry-run
tools/oxp3-powerctl.py fan status --json
tools/oxp3-powerctl.py fan curve --once --dry-run
```

Expect: both zones listed with PL1/PL2/PL4 and τ readbacks, `[ro (needs root)]` for each writable file
when not root, the fan line (`rpms, pwm/255 (ec /184), mode`), `tt_toggle=1`, the `acpi_fan` EIO warning,
and a `profile:` line that says `custom` for today's 35/37/122 state. `--dry-run` shows the exact write
order without touching anything.

### 8.2 First supervised write (root; this is the step that proves writability)

```bash
sudo /usr/local/libexec/oxp3-powerctl snapshot --save     # rollback baseline first
sudo /usr/local/libexec/oxp3-powerctl status              # record what it says now
sudo /usr/local/libexec/oxp3-powerctl profile balanced    # PL1 25 / PL2 45 / PL4 160, both zones
for d in intel-rapl:0 intel-rapl-mmio:0; do
  for c in 0 1 2; do printf '%s c%s = %s\n' "$d" "$c" \
    "$(cat /sys/class/powercap/$d/constraint_${c}_power_limit_uw 2>&1)"; done; done
sudo /usr/local/libexec/oxp3-powerctl profile performance --with-tau
cat /sys/class/powercap/intel-rapl:0/constraint_0_time_window_us
sudo /usr/local/libexec/oxp3-powerctl restore-baseline    # or profile balanced / reboot
```

The tool itself fails with exit 3 and a named mismatch if a value does not stick, so the `cat` loop is
a second, independent check. If *this* is where an `EACCES` appears as root, the BIOS locked the RAPL
limit (open point 2) — nothing in userspace can override that; fall back to `platform_profile` and the
fan controls.

### 8.3 Envelope under load (dynamic)

```bash
# sample package power + package temperature + fan rpm once per second for 60 s
tools/oxp3-powerctl.py status --json > /tmp/oxp3-power-before.json
( stress-ng --cpu 8 --timeout 60s >/dev/null 2>&1 & vkcube --c 0 >/dev/null 2>&1 & wait ) &
for i in $(seq 60); do
  printf '%s pkg=%s W cpu=%s C fan=%s rpm\n' "$(date +%T)" \
    "$(cat /run/rapl-power/package-0 2>/dev/null)" \
    "$(awk '{printf "%.1f", $1/1000}' /sys/class/hwmon/hwmon7/temp1_input)" \
    "$(cat /sys/class/hwmon/hwmon6/fan1_input)"; sleep 1; done
```

Expected, with `stress-ng` + `vkcube`: `quiet` ≈ 10 W package sustained, `balanced` ≈ 25 W,
`performance` ≈ 35 W sustained with a PL2 excursion at the start (up to 80/160/239 W briefly depending
on the profile). Temperatures should stay under the ~100 °C package limit; if the package pins at Tjmax
with `performance`, the EC/firmware is already limiting and the profile is not the bottleneck.
`/run/rapl-power/package-0` needs the `rapl-power` service (it is installed and active here); without it
use `sudo cat /sys/class/powercap/intel-rapl:0/energy_uj` deltas.

### 8.4 Fan sweep (root)

```bash
sudo /usr/local/libexec/oxp3-powerctl fan manual
for p in 0 64 128 192 255; do
  sudo /usr/local/libexec/oxp3-powerctl fan speed "$p"
  sleep 30
  printf 'pwm=%3d  %s rpm\n' "$p" "$(cat /sys/class/hwmon/hwmon6/fan1_input)"
done
sudo /usr/local/libexec/oxp3-powerctl fan auto
```

Expect rpm to rise monotonically with pwm (the EC floor prevents 0 from stopping the fan) and
`pwm1_enable=1` while sweeping, then `2` after `fan auto`. If rpm does not move at all, check
`tt_toggle` (must be 1) and that the write readback matched.

### 8.5 Curve daemon

```bash
sudo /usr/local/libexec/oxp3-powerctl fan curve --once --dry-run   # one decision, no writes
sudo /usr/local/libexec/oxp3-powerctl fan curve --steps 5          # ~8 s, then back to auto
sudo systemctl start oxp3-fan-curve.service                        # the long-running variant
```

### 8.6 Unit-level checks

```bash
systemctl status oxp3-power-profile.service oxp3-power-resume.service
journalctl -u oxp3-power-profile -u oxp3-power-resume -b
systemctl cat oxp3-power-profile.service | grep -E 'After|ReadWritePaths'
```

## 9. Rollback

1. **Limits:** `sudo /usr/local/libexec/oxp3-powerctl restore-baseline` (from the `snapshot --save`
   taken in §8.2), or `profile balanced`, or simply reboot — the firmware re-initialises the RAPL
   values (they were 35/37/52/122 at boot before any of this).
2. **Fan:** `sudo /usr/local/libexec/oxp3-powerctl fan auto` (`pwm1_enable=2` hands control back to the
   EC). Stopping `oxp3-fan-curve.service` does the same (SIGTERM handler restores auto).
3. **Units:** `sudo systemctl disable --now oxp3-power-profile.service oxp3-power-resume.service
   oxp3-fan-curve.service`, then `tools/oxp3-power-install.sh uninstall`. `/etc/oxp3/` is left in place
   by design; remove it by hand if you want a clean slate.
4. **platform_profile:** `echo balanced | sudo tee /sys/firmware/acpi/platform_profile`.

## 10. Open points

1. **τ unit (calibration).** `56` is read as seconds here because a sub-second PL1 window would make the
   vendor's PL2 values meaningless (§2.4). The §2.4/§8.3 experiment decides it; until then profiles do
   not touch the window unless `--with-tau`/`--tau` is given.
2. **RAPL BIOS lock.** Whether `rd->rpl[]` is locked cannot be read without root; the first real write
   reports `EACCES` if it is. Nothing in userspace can override a locked limit.
3. **PL2/PL4 enable bits** are not exposed by powercap (only limits + PL1 window). The firmware has them
   enabled today (52 W MMIO PL2 and 122 W PL4 are live). If a future BIOS disables them, a small kernel
   change would be needed to expose the enable/clamp bits (or a helper writing `MSR_PKG_POWER_LIMIT`
   bits 47/48 and the PL4 enable field by hand — deliberately not done here).
4. **`constraint_0_max_power_uw` = 25 W** is a firmware *hint* (the kernel does not clamp to it; 35 W
   sticks). Keep it in mind when validating a 35 W profile on a different BIOS.
5. **`fanSpeed=200` semantics** — assumed to be a 0–255 duty like `FAN_MAX_SPEED_VALUE`; the vendor may
   mean its EC 0–184 scale, in which case 200 would be clamped. The manual sweep in §8.4 is the check.
6. **Per-profile SAGV point (3/0/4)** is not implemented: there is no Linux interface for it (`xe` needs
   a small patch or debugfs, gap-audit #9). It is part of the vendor preset but not of the power
   envelope this tool controls.
7. **Adaptive TDP** (`GPU_UTIL_THRESHOLD_*`, `PL1_THRESHOLD=25`, `TIME_INTERVAL=10000`, i.e. adjust PL1
   from GPU utilisation every 10 s) is not implemented; a follow-up daemon could read `xe` fdinfo and
   call `setpl1`. The charger-tier (`powersupplymode_mode1/2/4/8`) input for it is also not exposed.
8. **EC fan curve offsets** are the blocker for real curve control (§5.4); needs a Windows-side EC trace
   or an SSDT dump behind the unbound WMI GUIDs.
9. **Wiring into the main installer:** `files/` and `tools/` are added here, but `oxp3-fixes.sh`'s
   `install_extras()` was deliberately not touched while other work is in flight; adding the three
   units + `oxp3-powerctl` there (and a README row) is the one remaining integration step.
10. **hhd**: it currently has no Intel TDP path, so no conflict. If upstream adds one (the audit
    suggests it as a natural contribution), prefer `oxp3-powerctl` as the backend instead of two
    competing writers.

## 11. Evidence / reproduction

```bash
# inventory (read-only, unprivileged)
ls -l /sys/class/powercap/intel-rapl*/constraint_* /sys/class/hwmon/hwmon6/*
for z in intel-rapl:0 intel-rapl-mmio:0; do for c in 0 1 2; do
  echo "$z c$c $(cat /sys/class/powercap/$z/constraint_${c}_name 2>/dev/null) \
$(cat /sys/class/powercap/$z/constraint_${c}_power_limit_uw 2>/dev/null) \
$(cat /sys/class/powercap/$z/constraint_${c}_time_window_us 2>&1)"; done; done
cat /sys/devices/platform/oxp-platform/tt_toggle
cat /sys/firmware/acpi/platform_profile /sys/firmware/acpi/platform_profile_choices
cat /sys/class/hwmon/hwmon6/{fan1_input,pwm1,pwm1_enable}
cat /sys/class/thermal/cooling_device2/{type,max_state,cur_state} 2>&1

# kernel side
journalctl -k -b | grep -iE 'intel_rapl|PL4|oxpec|tt_toggle'
modinfo ec_sys 2>&1                     # not built on Anatase
grep -rn 'powercap\|intel-rapl' /usr/lib/python3.15/site-packages/hhd | head   # hhd: none

# vendor side (untrusted data, outside the repo)
7z x OneXConsole_0.10.4-fix1.exe '$PLUGINSDIR/app-64.7z' -o~/.cache/powerfan
7z x ~/.cache/powerfan/'$PLUGINSDIR/app-64.7z' \
     'resources/resources/tools/intel/perf_conf.ini' -o~/.cache/powerfan
cat ~/.cache/powerfan/resources/resources/tools/intel/perf_conf.ini
objdump -p ~/.cache/powerfan/.../HWAPIExtDLL.dll | sed -n '/\[Ordinal\/Name Pointer\]/,$p'
```

Nothing in this document was executed as root, no package or config was changed, and no vendor binary,
DLL, firmware or decompiled listing is stored in this repository. The `perf_conf.ini` values were
re-read from the vendor payload for this document and transcribed here as data; the file itself stays
outside the repo.
