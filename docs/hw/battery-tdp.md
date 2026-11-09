# Battery health, charge limit, "bypass power" and adaptive TDP on the OneXPlayer 3

What the OXP3 battery subsystem exposes under Linux, how far the vendor's
"battery protection" features can be reproduced without a kernel change, and the
opt-in adaptive-TDP daemon that moves the package power limits between the three
vendor presets on load, temperature and battery state.

Everything here is userspace: `power_supply` and `powercap` sysfs only. No MSR
pokes, no EC RAM writes, no kernel patch, nothing vendor-proprietary committed
(field names and behaviour only, from the untrusted vendor payload in
`/var/home/holzr/Downloads/oxp3`).

Related: [power-fan.md](power-fan.md) (PL1/PL2/PL4/tau, fan, `oxp3-powerctl`),
[gap-audit.md](gap-audit.md) items 5 (battery care) and 10 (adaptive TDP).

---

## TL;DR

| vendor feature | reachable on Linux? | how | status |
|---|---|---|---|
| Charge limit ("充电上限", EC `0xA3` percent) | **yes** | `charge_control_end_threshold` (ACPI `_BTP`/`oxpec`) | implemented: `oxp3-powerctl battery limit N` |
| Bypass power, always ("旁路供电", mode 2, EC `0xA4`=`0x03`) | **yes**, via the driver's EC write | `charge_behaviour=inhibit-charge` | implemented: `oxp3-powerctl battery bypass on` |
| Bypass power, awake only (mode 1, EC `0xA4`=`0x01`) | **yes**, via the driver's EC write | `charge_behaviour=inhibit-charge-awake` | implemented: `battery bypass on --mode inhibit-charge-awake` |
| Normal charging | yes | `charge_behaviour=auto` | `battery bypass off` |
| Battery health (capacity vs design) | yes | `energy_full` / `energy_full_design` | `battery status` |
| Cycle count | attribute exists, **EC returns 0** | `cycle_count` | reported as unusable |
| Charge *start* threshold / force-charge minimum | **no** | no sysfs attribute; EC `addrForceChargeMin` | documented gap |
| Power-supply mode / charger tier | **no** | only `ADP1.online` + `BAT0.status` | documented gap |
| Battery temperature | **no** | EC `addrBatteryTemp`, no `oxpec` attribute | documented gap |
| Third-party battery detect | **no** | EC `addrThirtyPartyBattery` | not needed here |
| Adaptive TDP ("自适应功耗") | **yes**, reimplemented | `oxp3-adaptivetdp` on `powercap` + `/proc/stat` + `coretemp` | implemented, default off |

Two real wins: the charge limit **is** the same knob the vendor UI drives (the
firmware stores it in the EC, and the vendor text says it works in the
powered-off state too), and charge-inhibit is a genuine, if approximate,
"bypass power": the battery stops charging and the machine runs from the
adapter.

---

## 1. What the machine exposes (read-only inventory)

Captured 2026-11-09, kernel `7.2.7-an28.fc45.x86_64`, unprivileged.

| interface | sysfs path | value now | mode | writable | note |
|---|---|---|---|---|---|
| charge limit | `/sys/class/power_supply/BAT0/charge_control_end_threshold` | **80** | `-rw-r--r-- root` | **yes (root)** | ACPI `_BTP`; a 0444 property would mean the firmware has no setter |
| charge start threshold | `…/BAT0/charge_control_start_threshold` | **absent** | — | — | this EC exposes no lower bound |
| charge behaviour | `…/BAT0/charge_behaviour` | `[auto] inhibit-charge inhibit-charge-awake` | `-rw-r--r-- root` | **yes (root)** | ACPI `_BCM` charge-inhibit bits |
| state of charge | `…/BAT0/capacity` | 80 % | `-r--r--r--` | no | |
| charging state | `…/BAT0/status` | `Charging` | `-r--r--r--` | no | held at 80 % by the limit above |
| energy now / full / design | `…/BAT0/energy_now` `energy_full` `energy_full_design` | 70.265 / 87.665 / 84.549 Wh | `-r--r--r--` | no | health = 103.7 % (see §5) |
| power | `…/BAT0/power_now` | **0 µW** | `-r--r--r--` | no | EC does not report it; use RAPL instead |
| voltage | `…/BAT0/voltage_now`, `voltage_min_design` | 12.661 / 11.670 V | `-r--r--r--` | no | |
| cycle count | `…/BAT0/cycle_count` | **0** | `-r--r--r--` | no | not implemented by the EC |
| identity | `manufacturer` `model_name` `serial_number` `technology` | `Intel SR 1`, `SR Real Battery`, `""`, `Unknown` | ro | no | OEM battery |
| AC adapter | `/sys/class/power_supply/ADP1/online` | 1 | `-rw-r--r-- root` | no (driver owns it) | the only charger signal Linux has |
| battery hwmon | `…/BAT0/hwmon3/in0_input`, `power1_input` | 12661 mV, 0 µW | ro | no | mirrors of the above |
| EC (`oxp_ec`) | `/sys/class/hwmon/hwmon6/{fan1_input,pwm1,pwm1_enable}` | 4278 rpm, 213, 2 | mixed | fan only | **no battery fields at all** |

Two things worth reading carefully:

- `charge_control_end_threshold` and `charge_behaviour` are **0644 root**, not
  0444. In `power_supply` that is the kernel telling you a write handler exists
  — `acpi_battery` only publishes these when the ACPI firmware implements
  `_BTP` / the charge-inhibit bits. So this is real, not a read-only stub.
- The limit is already **80 %** and the pack is sitting at **80 % with status
  `Charging`**: the EC is actively holding the pack at the ceiling rather than
  filling to 100. That is the vendor feature already live (it was set before
  Linux, i.e. it is EC-persisted), and it is the best evidence that this knob
  is the vendor's own.

Not exposed anywhere: battery temperature, charge current, the EC's bypass
byte, the charger tier, and any EC RAM window (`ec_sys` is not built in this
kernel — see [power-fan.md](power-fan.md) §5.2).

---

## 2. Vendor evidence (untrusted data, names and behaviour only)

Payload: `OneXConsole_0.10.4-fix1.exe` → NSIS → `$PLUGINSDIR/app-64.7z` →
`resources/resources/CompatLayerCT.exe` (8 759 656 bytes, PE32+ **Mono/.NET**
x86-64) and `resources/app.asar` (UI/i18n). Extracted to
`~/.cache/oxp3-battdp/`, never into this repo.

### 2.1 EC field names and values (CompatLayerCT.exe metadata)

```
EC_ADDR_BYPASS_POWER, EC_VALUE_BYPASS_POWER_MODE1, EC_VALUE_BYPASS_POWER_MODE2,
EC_VALUE_BYPASS_POWER_MODE_OFF
EC_ADDR_CHARGE_LIMIT,   addrChargeLimit,   batterySetChargeLimit / GetBatteryChargeLimit
EC_ADDR_FORCE_CHARGE_MIN, addrForceChargeMin
EC_ADDR_POWER_SUPPLY_MODE, batteryGetPowerSupplyMode, batteryInitPowerSupplyModeEC
EC_ADDR_THIRTY_PARTY_BATTERY, batteryIsThirtyPartyBattery
EC_ADDR_OXP_BATTERY_TEMP, EC_ADDR_OXP_BATTERY_CHARGE_CURRENT_H/_L
EC_ADDR_OXP_SET_TDP_ABLE, batterySetMaxTdpLimit, funcGetOXPSetTdpAble
EC_ADDR_STATUS_COMMAND_PORT, EC_ADDR_DATA_PORT
ChargerBarrel / ChargerUSB / BatteryDCIn / BatteryTypec65 / BatteryTypec100 / BatteryTypec140
battery/initEcTU, battery/getBatteryStatusTU, battery/getPowerSupplyModeTU, …
```

So the vendor model is: an EC byte holds the charge limit, an EC byte holds the
bypass mode (off / mode 1 / mode 2), an EC byte holds the charger tier, and a
`TE`-suffixed method set exposed over the app's own IPC (`…TU` command strings)
is what the UI calls. `PawnIOLib.dll` (`pawnio_open/…load/…execute`) and
`InpOutx64.dll` plus `forcePawnIOMode`/`isPawnIOMode` show the transport: raw
port I/O to the ACPI EC status/data ports through a signed kernel helper,
because Windows has no EC driver for this board.

**The addresses are not in this binary at all — and now we know why.** The managed assembly is
*stripped*, not packed: 2829 of its 3706 method bodies share one RVA whose content is
`throw new Exception("Runtime exception")`, 189 more share a trivial `.ctor` stub, and only 35 bodies
are distinct. Metadata (field names, method names, **parameter names**, P/Invoke imports) is intact,
but the named API surface is one `throw` stub, and no `ldc.i4` anywhere in the remaining CIL is one of
these addresses — so there is nothing to read and nothing to decrypt. The Ghidra run that
"decompiled zero functions" was therefore correct about the file.

The numbers are one layer up, in `app.asar`, where the per-OEM bootstrap passes them *into* the
layer as decimal arguments. Recovered map, provenance and confidence:
[`upstream/oxpec-ec-addresses.md`](../../upstream/oxpec-ec-addresses.md). Battery-relevant rows:

| vendor field | vendor value | EC offset | encoding |
|---|---:|---:|---|
| `addrChargeLimit` | `0x4A3` | `0xA3` | percent 0–100, the same byte as upstream `OXP_X1_CHARGE_LIMIT_REG` |
| `addrBypassPower` | `0x4A4` | `0xA4` | `0x00` off / `0x01` inhibit-while-awake / `0x03` inhibit-always |
| `addrForceChargeMin` | `0x4A5` | `0xA5` | percent (unit unproven) |
| `addrBatteryTemp` | `0x4A0` | `0xA0` | °C, 1 byte |
| charge current H/L | `0x4A1`/`0x4A2` | `0xA1`/`0xA2` | 16-bit pair, unit unknown |
| `addrPowerSupplyMode` | `0x4E3` | `0xE3` | `1` battery, `2` Type-C ≥100 W, `4` DC-in, `8` Type-C ≤65 W |

Call site: `batteryInitEc(addrChargeLimit, addrBypassPower, addrForceChargeMin, off, mode1, mode2)`
with `(1187, 1188, 1189, 0, 1, 3)` for the `ONEXPLAYER 3` branch; the parameter names come from
metadata token `0x0600043e`, so the argument order is not a guess. This also matches what the driver
in this kernel already writes (`OXP_X1_CHARGE_LIMIT_REG 0xA3`, `OXP_X1_CHARGE_INHIBIT_REG 0xA4`,
masks `0x01`/`0x02`/`0x03`), which is why `charge_control_end_threshold` exists and works today.

The `…TU` command-string set and `PawnIOLib.dll`/`InpOutx64.dll` are real, but the OXP3 branch sets
`ECAccessType::OxpWMI` (=2), so on *this* board the layer reaches the EC through an OXP ACPI-WMI
method, with port I/O and PawnIO as fallbacks for boards that lack it. That is why the protocol
question (§7, open point 1) is now a curiosity rather than a blocker.

0 functions: PE/CLI parses, but the body is CIL, not x86-64. Native neighbours
were checked too — `tools/intel/PowerPlugin.dll` is CPU/RAPL only ("Battery Info
Read" appears once, no EC constants), `UtilPlugin.dll` and `HWAPIExtDLL.dll`
carry no battery logic. The field *map* is what the managed assembly gives up,
and it is enough to describe the gap precisely.

### 2.2 UI semantics (app.asar i18n, zh-CN bundle)

| key | text | meaning |
|---|---|---|
| `battery_protection` | 电池保护 | "Battery protection" section |
| `battery_protection_chargelimit` | 充电上限 | "charge limit" |
| `battery_protection_chargelimit_hint` | 设置较低的充电上限能避免过度充电，可以延长电池的使用寿命。该设置在开机和关机状态下都会生效。 | lower limit avoids overcharge and extends life; **the setting takes effect in both the on and off state** (EC-stored) |
| `battery_protection_bypasspowermode` | 旁路供电模式 | "bypass power supply mode" |
| `battery_protection_bypasspower_effect_always` | 始终生效 | **mode 2** (`0x03`): always effective |
| `battery_protection_bypasspower_effect_when_screen_on` | 睡眠与关机状态无效 | **mode 1** (`0x01`): ineffective in sleep/shutdown |
| `battery_protection_bypasspowermode_hint` | 旁路供电开启后，插电状态下将会停止对电池充电，仅对设备供电，可以降低发热以及延长电池的使用寿命。 | "after enabling bypass, **while plugged in, charging the battery stops; only the device is powered**; reduces heat and extends battery life" |
| `battery_protection_normalpower` | 正常供电 | normal power supply (bypass off) |

That hint is the definition of what we implement: *stop charging while on the
adapter*. `inhibit-charge` does exactly that at the ACPI level.

### 2.3 Adaptive TDP

`resources/resources/tools/intel/perf_conf.ini`, section `[auto_config]`:

```ini
GPU_UTIL_THRESHOLD_LOW=80.00
GPU_UTIL_THRESHOLD_HIGH=90.00
PL1_THRESHOLD=25
TIME_INTERVAL=10000
```

UI keys: `tdpFollowFramerate` = 自适应功耗 ("adaptive power"), with
`…TdpMode` = 跟随帧数，动态Tdp ("follow the frame rate, dynamic TDP") and
`…GpuMode` = 上限功耗，动态Gpu ("power ceiling, dynamic GPU"), `…On` =
跟随锁帧自动, `…Off` = 手动, and a warning: 开启自适应功耗后，在极少数游戏中可能
会导致显卡驱动崩溃 ("with adaptive power on, in a few games it can crash the
graphics driver").

So the vendor polls GPU utilisation every 10 s and moves PL1 (threshold 25 W)
between the three presets of `[Quiet]/[Balanced]/[Performance]` (values in
[power-fan.md](power-fan.md) §2.1/§3), with a frame-rate cap as the input and
per-charger-tier presets. The daemon below is that idea, with a temperature
input the vendor does not have.

---

## 3. What we implement

### 3.1 `tools/oxp3-powerctl.py` — new `battery` command group

One code path with the existing power/fan tool, same exit codes (0 ok, 2 a
control is missing / not permitted, 3 readback mismatch, 4 partial).

```bash
oxp3-powerctl battery status [--json]
oxp3-powerctl battery limit 80|off [--save] [--dry-run] [--json]
oxp3-powerctl ec map                                       # recovered EC map (read-only)
oxp3-powerctl ec probe                                     # EC-facing interfaces (read-only)
oxp3-powerctl battery bypass {on|off|status} [--mode inhibit-charge|inhibit-charge-awake]
                                            [--save] [--dry-run] [--json]
```

- **Capability classification, not guessing.** Every control is classified
  without writing anything: `absent` (no such sysfs file), `read-only`
  (0444 = no kernel write handler), `root-only` (0644 = handler exists, needs
  root), `writable`. That distinction is why the tool can say *"this kernel/EC
  does not expose a charge limit; ACPI `_BTP` is missing"* instead of failing
  with a bare `EACCES`.
- **Readback.** `limit` re-reads the value and exits 3 on a mismatch. `bypass`
  parses the kernel's bracketed selection
  (`[inhibit-charge] auto inhibit-charge-awake`) and compares the selected
  token, not the whole line.
- **Refusals carry the reason and the missing piece.** If `charge_behaviour`
  is absent the tool names `_BCM` and the EC byte behind
  `EC_ADDR_BYPASS_POWER`; if the limit is absent it names `_BTP` and
  `EC_ADDR_CHARGE_LIMIT`. That is the documentation of what `oxpec`/the kernel
  would have to add, in the place a user will actually read it.
- **`--save`** stores the setting in `/etc/oxp3/power.json` under `battery`, so
  the existing `oxp3-power-profile` / `oxp3-power-resume` units re-apply it at
  boot and on resume through `restore` (implemented; battery re-apply failures
  are warnings, never fatal, so a desktop without the attribute still boots).
- **`status` now includes a one-line battery summary** and the full JSON has a
  `battery` object; both are additive.

`battery status` on this box:

```
battery BAT0
  status Charging, 80 % (level Normal), health 103.7 %
  energy: now 70.27 Wh, full 87.67 Wh, design 84.55 Wh
  now: 0.00 W, 12.661 V, 0.00 W (hwmon)
  cycles: 0
  ac: ADP1 online=True
  charge control (capability = what the kernel actually exposes):
    charge_control_end_threshold     80 [root-only]
    charge_control_start_threshold   absent
    charge_behaviour                 auto [root-only]
  bypass: off (normal charging (vendor 'normal power supply'))
    modes: auto inhibit-charge inhibit-charge-awake
  warning: cycle_count reads 0: …
```

### 3.2 Vendor → Linux mapping, and where it is only an approximation

| vendor | Linux | fidelity |
|---|---|---|
| `EC_ADDR_CHARGE_LIMIT` (`0xA3`, persisted in EC, effective in S5) | `charge_control_end_threshold` / ACPI `_BTP` | **high** — same byte, same unit (percent), firmware-stored, same persistence |
| bypass mode 2 (`0x03`, always) | `charge_behaviour=inhibit-charge` | **high** — same byte `0xA4` and same value `0x03` that upstream `oxpec` writes; the only unproven part is whether the EC also isolates the pack from the rail |
| bypass mode 1 (`0x01`, awake only) | `inhibit-charge-awake` | **high** for the byte/bit (`0x01`), **medium** for the wording: "awake" is a system-suspend boundary, the vendor says "sleep/shutdown" |
| bypass off (`0x00`) | `auto` | high — identical value |
| charge start threshold / `addrForceChargeMin` | — | none |
| charger tier (`mode1/2/4/8`) | `ADP1.online` only | none |
| `addrBatteryTemp` | — | none |

What a kernel change would need (not done, and not guessed):

1. **Bypass as a real `oxpec` attribute** — e.g.
   `charge_bypass` (`oxp_ec`), writing the EC byte behind
   `EC_ADDR_BYPASS_POWER` with the vendor's three values, through the driver's
   existing `write_to_ec()`. Blocked on the EC address, not on the design.
2. **Charge start threshold** — `charge_control_start_threshold` in `oxpec`, or
   `_BTP` support for the lower bound if the firmware has it (it exposes only
   one value today).
3. **Battery temperature** — an `oxpec` hwmon `tempN_input` fed from
   `EC_ADDR_OXP_BATTERY_TEMP`; needed to gate fast-charging by pack temperature.
4. Then `power_supply`'s own `charge_behaviour`/threshold plumbing can stay as
   the userspace surface; the tool needs no change beyond the capability probe
   already landing on `writable`.

To get the addresses (the one blocker): a Windows-side EC trace
(RWEverything/`inpoutx64` on ports 0x62/0x66 while toggling the toggle in
OneXConsole), or an ACPI/SSDT dump, or `CONFIG_ACPI_EC_DEBUGFS` on a debug
kernel. Same conclusion as the fan curves in
[power-fan.md](power-fan.md) §5.4.

---

## 4. Adaptive TDP daemon

`tools/oxp3-adaptivetdp.py` (+ `files/oxp3-adaptivetdp.service`). **Default
off** — installing the unit does not enable or start it.

### 4.1 Policy

A three-rung ladder over the vendor presets, target from load, then clamped:

```
target = performance  if load >= --up        (default 65 %)
       = balanced     else if load >= --down  (default 30 %)
       = quiet        else

temp >= --soft-temp (85 C)      -> at most balanced
temp >= --max-temp  (92 C)      -> quiet immediately (safety; bypasses dwell)
on battery                      -> at most balanced
capacity <= --bat-floor (20 %)  -> quiet
--ceiling (default performance) -> hard upper bound on the rung
```

Inputs: CPU busy from `/proc/stat` deltas; GPU busy from
`/sys/class/drm/*/device/gpu_busy_percent` **if the driver exposes it** (this
`xe` does not — see open point 6, so load is CPU-only today); package
temperature from `coretemp` "Package id 0" (override with `--temp-sensor`);
battery from `BAT0` + `ADP1`.

Hysteresis: a new rung must be wanted for `--dwell` consecutive samples
(default 3) **and** at least `--min-interval` seconds must have passed since
the last change (default 20 s — the vendor's 10 s interval is the floor, and
this is deliberately slower). The thermal-safety transition ignores the dwell
but still rate-limits to one step per `--min-interval` so a fan spin-up cannot
make it thrash.

Writes go through `oxp3-powerctl`'s own `apply_limits()` (both RAPL zones, same
raise/lower ordering, same readback), so the daemon adds no new privileged
surface. `--platform-profile` optionally writes the ACPI profile too (off by
default: hhd owns EPP and this daemon should not quietly change it).

### 4.2 Decision trail

One JSON object per sample: `ts`, `event` (`start`/`sample`/`change`/`error`/
`restore`/`stop`), `load_pct`, `load_cpu_pct`, `load_gpu_pct`, `temp_c`,
`on_ac`, `battery`, `want`, `policy` (the human-readable reason), `rung`,
`outcome` (`change`/`hold`), and on a change `from_profile`/`to_profile`/
`limits`/`platform_profile`/`applied`. Written to stderr (journal, only changes
unless `--verbose`) and to `/var/log/oxp3-adaptivetdp.jsonl` (`--log-file`,
override or `''` to disable). Every decision is reproducible from its log line.

`--dry-run` decides and logs without writing and works unprivileged; `--once`
takes one decision; `--json` prints each change as JSON on stdout.

### 4.3 Install

```bash
# tool only (battery + charge/binding), no daemon:
sudo tools/oxp3-power-install.sh install
# plus the daemon + unit, still INACTIVE:
sudo tools/oxp3-power-install.sh install --adaptive-tdp

systemctl status oxp3-adaptivetdp        # installed, inactive
sudo systemctl start oxp3-adaptivetdp    # opt in for this boot
sudo systemctl enable oxp3-adaptivetdp   # opt in permanently
```

Manual install without the script:

```bash
sudo install -m 0755 tools/oxp3-powerctl.py tools/oxp3-adaptivetdp.py /usr/local/libexec/
sudo install -m 0644 files/oxp3-adaptivetdp.service /etc/systemd/system/
sudo systemctl daemon-reload
```

The daemon imports `oxp3-powerctl.py` by path, so **both files must live in the
same directory** and keep these names. The unit runs it as root with
`ProtectSystem=strict` and `ReadWritePaths=/sys/devices/virtual/powercap
/sys/firmware/acpi /var/log` — the third path is only for the log file.

---

## 5. Validation

Verified in this repo without privileged access: everything in §5.1, the policy/hysteresis
unit checks in §5.3, and the whole daemon loop in `--dry-run` against real `/proc/stat`,
`coretemp` and `power_supply` input (a 100 % load run drives it `custom -> performance` and
back to `quiet` after the load stops). The write paths in §5.2 and the supervised stress run
in §5.4 need root and have **not** been executed on the device yet — they are the remaining
verification step, and the persistence questions in §8 (2) and (3) depend on them.

### 5.1 Read-only (safe, unprivileged)

```bash
tools/oxp3-powerctl.py battery status
tools/oxp3-powerctl.py battery status --json | python3 -m json.tool | head -40
tools/oxp3-powerctl.py battery limit 60 --dry-run
tools/oxp3-powerctl.py battery bypass on --dry-run
tools/oxp3-powerctl.py battery bypass status
tools/oxp3-powerctl.py status | tail -6            # battery line inside the full view

# the raw truth, no tool in the way:
ls -l /sys/class/power_supply/BAT0/
cat /sys/class/power_supply/BAT0/{capacity,status,charge_behaviour,cycle_count}
cat /sys/class/power_supply/BAT0/{energy_now,energy_full,energy_full_design,voltage_now}
```

Expected on this box: the table in §1, `charge_control_end_threshold=80`,
`[auto] inhibit-charge inhibit-charge-awake`, `cycle_count=0`.

### 5.2 First supervised writes (root)

```bash
# 1) charge limit: set, read back, and watch the EC honour it
sudo tools/oxp3-powerctl.py battery limit 60
cat /sys/class/power_supply/BAT0/charge_control_end_threshold     # 60
watch -n 5 'cat /sys/class/power_supply/BAT0/{capacity,status}'   # settles at <=60
#    with the pack above 60 % and AC plugged in, status should stop charging
#    ("Not charging"/"Full") rather than keep climbing.

# 2) does it survive a power cycle? (the vendor text claims it does)
#    set 60, shut down, unplug, boot: the value must still read 60. If it
#    reverts, the firmware's _BTP is volatile and the limit must be re-applied
#    at boot (already covered by `battery limit 60 --save` + the profile unit).

# 3) bypass / charge inhibit, on AC
sudo tools/oxp3-powerctl.py battery bypass on
cat /sys/class/power_supply/BAT0/charge_behaviour   # [inhibit-charge] ...
cat /sys/class/power_supply/BAT0/status             # Not charging (pack still 80 %)
cat /sys/class/power_supply/BAT0/power_now          # 0 here; the EC never reports it
sudo tools/oxp3-powerctl.py battery bypass off

# 4) restore the vendor default range
sudo tools/oxp3-powerctl.py battery limit 80
```

What to look for and what would prove a limitation:

- `bypass on` leaves `status=Discharging` **on AC** instead of `Not charging`:
  the EC is then feeding the board from the pack, i.e. `inhibit-charge` is not
  a real bypass on this firmware. Record it, do not pretend otherwise.
- `battery limit` exits 3 with a readback mismatch: the EC clamps to its own
  allowed range; use the value it reports.
- Any command exits 2 with `run this command as root (sudo)` when run
  unprivileged — that is the capability probe, not a failure of the feature.

### 5.3 Adaptive TDP, dry first

```bash
tools/oxp3-adaptivetdp.py --once --dry-run --verbose            # one decision, no writes
tools/oxp3-adaptivetdp.py --steps 10 --interval 2 --dry-run --json   # watch it think
```

Policy unit checks (no hardware side effects; run from the repo root):

```bash
python3 - <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ad", "tools/oxp3-adaptivetdp.py")
ad = importlib.util.module_from_spec(spec); sys.modules["ad"] = ad; spec.loader.exec_module(ad)
class A: pass
a = A(); a.up, a.down, a.soft_temp, a.max_temp = 65, 30, 85, 92
a.bat_floor, a.allow_battery_performance, a.ceiling = 20, False, "performance"
p = ad.Policy(a)
for label, (load, temp, ac, cap), want in [
    ("idle ac", (10, 50, True, 80), "quiet"),
    ("mid ac", (45, 60, True, 80), "balanced"),
    ("heavy ac", (90, 70, True, 80), "performance"),
    ("heavy warm", (90, 86, True, 80), "balanced"),
    ("heavy hot", (90, 93, True, 80), "quiet"),
    ("heavy battery", (90, 60, False, 80), "balanced"),
    ("low battery", (20, 60, False, 15), "quiet"),
]:
    got = ad.LADDER[p.target(load, temp, ac, cap)[0]]
    print(("ok  " if got == want else "FAIL"), label, "->", got)
PY
```

### 5.4 Stress run (root, supervised)

```bash
# terminal 1: watch the limits, the temperature and the decision log
watch -n 2 'grep . /sys/class/powercap/intel-rapl:0/constraint_{0,1}_power_limit_uw \
  /sys/class/hwmon/hwmon7/temp1_input'

# terminal 2: the daemon, verbose, no unit yet
sudo tools/oxp3-adaptivetdp.py --interval 2 --verbose --log-file /tmp/atdp.jsonl

# terminal 3: load. 30 s of all-core, then idle, then repeat.
stress-ng --cpu 0 --timeout 30s || for i in $(seq $(nproc)); do yes >/dev/null & done
sleep 45   # let it fall back
kill %1 2>/dev/null; pkill yes

# afterwards
python3 -c "import json;[print(json.loads(l)['event'], json.loads(l).get('from_profile'), '->', json.loads(l).get('to_profile'), json.loads(l).get('policy')) for l in open('/tmp/atdp.jsonl')]"
```

Accept: under load it reaches `performance` (PL1 35 W) and after the idle period it
falls back to `quiet` (10 W) — the bands mean it may skip `balanced` on the way, which
is intended, not a bug. At most one change per `--min-interval`, package temperature
below `--max-temp`, and Ctrl-C (SIGTERM) logs `event=restore` and puts the limits back
to whatever was applied before the daemon started (nothing, if the starting state was
`custom` and matches no preset).

Reject and roll back if: temperature crosses `--max-temp` without an immediate
drop to `quiet`, the log shows more than one change per 20 s window, or PL1
reads back a value the log did not claim.

### 5.5 Unit-level checks

```bash
sudo systemctl daemon-reload
systemd-analyze verify files/oxp3-adaptivetdp.service     # only complains the binary is not installed
sudo systemctl start oxp3-adaptivetdp
journalctl -u oxp3-adaptivetdp -b -f
sudo systemctl stop oxp3-adaptivetdp                       # restores on SIGTERM
grep -c . /var/log/oxp3-adaptivetdp.jsonl
```

---

## 6. Risks

**Thermal.** The daemon's highest step is the vendor `performance` preset
(PL1 35 W / PL2 80 W / PL4 239 W), which is the vendor's own ceiling, so it
cannot push past what OneXConsole allows. What is new is that it does this
*without a human watching the game*. The mitigations are the two temperature
clamps (85 °C soft, 92 °C hard, package sensor) and the fact that the EC fan
curve is never touched — auto mode stays in charge. If the EC curve is lazy
this can still spike; the honest remedy is to install the fan curve
(`oxp3-fan-curve.service`) or lower `--soft-temp`. Note the vendor's own
warning about adaptive power crashing the GPU driver in rare games: that is
about its frame-cap input, which this daemon does not have.

**Battery.** A charge limit of 80 % (already set) is the single biggest life
win and is safe. Charge inhibit is safe for the pack at any state of charge —
it is a standard ACPI mode — but the pack keeps buffering load spikes, so heavy
gaming in bypass still cycles it a little; the vendor mode behaves the same
way. Two cautions: do not leave a very low limit (e.g. 50 %) on if you need
runtime, and avoid toggling `charge_behaviour` in a loop (it is a mode change,
not a charge cycle, but a script that flaps it will produce noisy micro-cycles).

**Interaction with hhd / `oxp3-tdp-sync`.** hhd writes the MMIO RAPL zone;
`oxp3-tdp-sync` mirrors MMIO→MSR; the daemon writes both zones. The unit is
ordered after both, so the daemon wins while it runs — but if hhd writes a TDP
afterwards the log will claim a profile the registers no longer match. Symptom:
`oxp3-powerctl status` shows `custom` while the daemon log says `performance`.

**Two writers at boot.** `oxp3-power-profile.service` applies the saved static
profile at boot and the daemon then overrides it. That is intended (the static
profile is the starting point the daemon restores on exit), but do not enable
both a `--save`d profile and a `--no-restore` daemon and expect the saved
profile back.

---

## 7. Rollback

```bash
# adaptive TDP: stop it (restores the pre-daemon preset), then forget it
sudo systemctl stop oxp3-adaptivetdp
sudo systemctl disable oxp3-adaptivetdp
sudo tools/oxp3-power-install.sh install          # reinstall without the daemon

# charge side: back to the vendor default range
sudo tools/oxp3-powerctl.py battery bypass off
sudo tools/oxp3-powerctl.py battery limit 80
# (or 100 if you want no limit at all; 80 is what the machine shipped with)

# power limits: put back whatever the firmware had
sudo tools/oxp3-powerctl.py restore-baseline      # if a baseline was saved
sudo tools/oxp3-powerctl.py profile balanced --save
# or just reboot: the firmware reinitialises the RAPL limits.

# everything
sudo tools/oxp3-power-install.sh uninstall        # keeps /etc/oxp3 and the log
```

`charge_behaviour` is not EC-persisted in any documented way; if a reboot
leaves charging inhibited, `battery bypass off` (and `--save` so the boot unit
fixes it) is the way back. The charge *limit* is EC-persisted by design — it is
the vendor feature — so set it back explicitly rather than expecting a reboot
to clear it.

---

## 8. Open points

1. ~~**EC addresses are unknown.**~~ **Resolved 2026-11-09** for the charge path:
   `0xA3` charge limit, `0xA4` bypass, `0xA5` force-charge floor, `0xE3` power-supply mode, plus the
   telemetry block — see §2.1 and
   [`upstream/oxpec-ec-addresses.md`](../../upstream/oxpec-ec-addresses.md). The previous reasoning
   was wrong in an interesting way: the numbers are not IL immediates at all (the managed assembly
   is stripped) but decimal arguments the Electron layer passes into it.
   What is still unproven: the `0xA5`/telemetry **units**, and one device-side confirmation that a
   `charge_behaviour` write lands in `0xA4`. A single Windows EC trace, or `ec_sys` on a debug
   kernel, settles both.
2. **Persistence of the charge limit across S5** is claimed by the vendor and
   consistent with the value we found (80 %, set outside Linux), but a write
   from Linux through `_BTP` has not been power-cycled yet. Test in §5.2 step 2.
3. **Persistence of `charge_behaviour`** across reboot/suspend is undocumented.
   If it does not stick, the fix is a `--save`d setting plus the existing
   resume unit (already wired) — validate on the device.
4. **Is `inhibit-charge` a true bypass on this EC?** If `BAT0.status` reads
   `Discharging` on AC with bypass on, the pack is feeding the board and the
   claim "device fed from the adapter" does not hold for the Linux path.
   Measure in §5.2 step 3 and amend this document.
5. **Battery temperature is not exposed**, so charging cannot be gated on pack
   temperature the way the EC presumably does it. `addrBatteryTemp` exists; an
   `oxpec` hwmon channel would fix it.
6. **No GPU utilisation input.** `xe` exposes no `gpu_busy_percent` here and no
   `gt`/freq attributes under `/sys/class/drm/card1/device/`, so the daemon's
   `load_gpu_pct` is `null` and it runs on CPU load alone. A `drm-engine`-based
   reader (fdinfo counters from the active client) or a `xe` patch would give
   the vendor's actual signal; a frame-cap input does not exist on Linux.
7. **Charger tier is not exposed** (`ADP1.online` only), so the vendor's
   per-charger presets (`powersupplymode_mode1/2/4/8`) cannot be mirrored. The
   daemon treats "on battery" as the only tier.
8. **`cycle_count` is 0** — the EC does not implement it. Health must be
   trended from `energy_full`; note it currently reads **above** design
   (103.7 %), which is normal for a new pack because the design figure is
   nominal. Store samples over months, not one reading.
9. **`power_now` is 0** — no battery power telemetry, so "is bypass actually
   removing charge?" must be inferred from `status`/`capacity`/`energy_now`,
   not from power. RAPL (`/run/rapl-power/`) gives package power for the TDP
   side.
10. **Not wired into `oxp3-fixes.sh`.** Same status as the power/fan work: the
    installer (`tools/oxp3-power-install.sh`) is the supported path; the main
    script's `install_extras()` was left alone while other work is in flight.
11. **hhd** has no Intel TDP path today; if it gains one, prefer calling
    `oxp3-powerctl` instead of adding a third writer (same note as
    [power-fan.md](power-fan.md) open point 10).

---

## 9. Evidence / reproduction

```bash
# system side (read-only, unprivileged)
ls -l /sys/class/power_supply/BAT0/
cat /sys/class/power_supply/BAT0/charge_behaviour /sys/class/power_supply/BAT0/charge_control_end_threshold
cat /sys/class/power_supply/BAT0/{capacity,status,cycle_count,energy_full,energy_full_design,power_now,voltage_now}
cat /sys/class/power_supply/ADP1/online
ls -l /sys/class/hwmon/hwmon6/ /sys/class/power_supply/BAT0/hwmon3/
modinfo ec_sys 2>&1 | head -2      # not found on this kernel

# vendor side (untrusted data, outside the repo)
mkdir -p ~/.cache/oxp3-battdp && cd ~/.cache/oxp3-battdp
7z x -y /var/home/holzr/Downloads/oxp3/OneXConsole_0.10.4-fix1.exe -o./nsis
7z x -y ./nsis/\$PLUGINSDIR/app-64.7z -o./app
strings -a -n 6 app/resources/resources/CompatLayerCT.exe | grep -E '^(EC_|addr|battery)'
cat app/resources/resources/tools/intel/perf_conf.ini
# i18n semantics (the asar is a zh-CN bundle; grep the key, read the value)
python3 - <<'PY'
import re
d = open('/var/home/holzr/.cache/oxp3-battdp/app/resources/app.asar','rb').read().decode('utf8','replace')
for k in ('battery_protection_chargelimit_hint','battery_protection_bypasspowermode_hint',
          'battery_protection_bypasspower_effect_always','battery_protection_bypasspower_effect_when_screen_on',
          'tdpFollowFramerate','tdpFollowFramerateTdpMode'):
    m = re.search(r'%s:"((?:[^"\\]|\\.){0,300})"' % re.escape(k), d)
    print(k, '->', m.group(1) if m else None)
PY

# isolated Ghidra (managed assembly: expect 0 decompiled functions)
tools/ghidra-pod.sh battdp ~/.cache/oxp3-battdp/app/resources/resources/CompatLayerCT.exe '^(battery|addr|EC_)'
```
