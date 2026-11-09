# OXP3 hardware/Linux gap audit — ranked, with reverse-engineering evidence

Status: audit complete; no host changes made. Every privileged step is written
out as a command, nothing was executed as root.
Target: ONEXPLAYER 3 (`ONE-NETBOOK` / `ONEXPLAYER 3`, BIOS 5.09, board
"ONEXPLAYER 3"), Anatase `20261026.3`, kernel `7.2.7-an28.fc45.x86_64`.
Vendor material (untrusted data, read-only): `~/Downloads/oxp3/`
(OneXConsole 0.10.4 NSIS → Electron `app.asar`, `OXP-Gi78P-ONEXPLAYER3-Drivers-1.1`).

Baseline assumed solved and **not** re-litigated here: custom Mesa, XeSS XMX
128-GRF kernels, `scx_cosmos`, PL1 35 W sync, patched `xe.ko`, RAPL powercap,
Heroic direct-Proton launch — plus this repo's own audio/volume/sleep/home-LED
work. Where a fix already exists it is marked **[covered]** in the table.

---

## 1. What the machine actually exposes (measured, this boot)

| area | evidence |
|---|---|
| PCI | `00:02.0` Arc B390 `[8086:b080]`, `00:04.0` DTT `[8086:b01d]`, `00:0b.0` NPU `[8086:b03e]`, `00:14.3` CNVi Wi-Fi `[8086:e440]`, `00:14.7` CNVi BT `[8086:e476]`, `1f.3` HD Audio `[8086:e428]`, `56:00.0` RTS525A card reader `[10ec:525a]` |
| USB | `1a86:fe00` WCH vendor composite (3 HID interfaces → `hidraw2/3/4`), `045e:028e` Xbox 360 pad (`xpad`), `1997:2433` Riitek mini keyboard. **No camera, no fingerprint, no second controller** on the bus |
| ACPI devices | `GXTP6933:00` (Goodix I²C-HID touchpad → `hid-multitouch`, `hidraw5`), `10EC5280:00` (IMU), `10EC1308:00`, `MCHP1940:00-07` (Microchip EC), **`BF63112:00/01` = SPI fingerprint (Betterlife, no driver bound)**, `TXNW3643:00-05`, `OVTI00AB/01AF/08F4/13B1` (OmniVision sensors) + **12× `INT3472`** camera PMICs, 4 unbound WMI GUIDs |
| IMU | `iio:device0`, driver `bmi260_core`, label `accel-display`, full accel+gyro (`in_accel_scale` 0.002394, `in_anglvel_scale` 0.001065, sampling 0.78–1600 Hz, mount matrix `0,1,0;1,0,0;0,0,-1`) — **works today** |
| Fan/EC | `hwmon6` (`oxp-platform`/`oxpec`): `fan1_input` 1388–2414 rpm, `pwm1`=51, `pwm1_enable`=2 (EC auto); `/sys/devices/platform/oxp-platform/tt_toggle`=1 |
| Thermal | `int3400`, `TCPU`, `TCPU_PCI`, `acpitz`, `x86_pkg_temp`, `iwlwifi_1`; `cooling_device2`=`TFN1` (max 50, `cur_state` unreadable); `acpi_fan` hwmon exists but `fan1_input` returns `EIO` (cosmetic `sensors` error) |
| RAPL | MSR domain `PL1 35 W / tau 27.98 s`, `PL2 37 W`, `constraint_2` (PL4) 122 W; MMIO domain `PL1 35 W / PL2 52 W` |
| Battery | `BAT0`: `charge_control_end_threshold`=80 (**no start threshold**), `charge_behaviour`=[auto] inhibit-charge inhibit-charge-awake, `energy_full` 87.665 Wh vs design 84.549 Wh, `cycle_count`=0 |
| Display | `card1-eDP-1` connected, **only** `1920x1200` mode advertised, `card1-DP-1..4` present; `drm_info`/`modetest`/`edid-decode` not installed, `/sys/kernel/debug/dri` not readable |
| BT | `hci0` exists (device alive) but **firmware missing**: `Bluetooth: hci0: Failed to load Intel firmware file intel/ibt-00a0-0291-iml.sfi (-2)`; `linux-firmware-20260916.an03` ships 122 `ibt-*` files, all `ibt-0040-*` — no `ibt-00a0-0291*` |
| Camera | **no** `/dev/video*`, no `/dev/media*`, no IPU driver bound; only `ipu3-fw.bin.xz` in firmware |
| Fingerprint | Betterlife on **SPI**: ACPI `BF63112:00/01` → `spi-BF63112:00`, **no driver bound**; vendor `BetterlifeFingerprintDevice.dll` (v9.39.15.26, UMDF) is in the package; `libfprint-1.94.100` has no Betterlife udev ID. Bring-up work already started in this repo (`tools/fingerprint/`) |
| WMI | 7 `wmi` devices: 3 `wmi_bmof` (`05901221-…`), 4 unbound: `1F13AB7F-6220-4210-8F8E-8BB5E71EE969`, `2BC49DEF-7B15-4F05-8BB7-EE37B9547C0B`, `43B5A593-AD62-4257-8546-91B0797BEC1B`, `A6FEA33E-DABF-46F5-BFC8-460D961BEC9F` |
| Userspace | HHD 4.1.12 (`hhd.service` enabled) with an OneXPlayer plugin (`hhd/device/oxp`: `hid_v1`, `hid_v2`, `serial`, `tt_toggle` takeover, `tt_led`), `rgb`/`power`/`gyro` plugin scaffolding; PipeWire 1.6.9 |

---

## 2. What Windows can do — reverse-engineered evidence

All vendor binaries were treated as data. Decompilation used the isolated
wrapper (`tools/ghidra-pod.sh gapaudit …`), output in
`~/.cache/ghidra-agents/gapaudit/out/decomp.txt`.

### 2.1 Power: the vendor profile table is a plain config file

`resources/resources/tools/intel/perf_conf.ini` (from OneXConsole 0.10.4):

| profile | PL1 MSR/MMIO | PL2 MSR/MMIO | τ | PL4 | fanSpeed | SAGV point |
|---|---|---|---|---|---|---|
| Quiet | 10 / 10 W | 25 / 25 W | 56 | 90 W | 0 | 3 |
| Balanced | 25 / 25 W | 45 / 45 W | 56 | 160 W | 0 | 0 |
| Performance | 35 / 35 W | 80 / 80 W | 56 | 239 W | 200 | 4 |

plus an adaptive section: `GPU_UTIL_THRESHOLD_LOW=80.00`,
`GPU_UTIL_THRESHOLD_HIGH=90.00`, `PL1_THRESHOLD=25`, `TIME_INTERVAL=10000`.

`PerfMode.exe` (Ghidra, 649 functions) shows how it is applied: separate
**MSR and MMIO** writes for the same limit (`"MSR Access"`, `"MMIO Access"`,
`"pl1msr"`, `"pl1mmio"`, `"pl1peci"`, `"GetCurrentPL1"`, `"SAGVPointToSet="`,
`"SAGVPointCurrent="`), a PECI path, an `AutoSAGVProc`/`AutoSAGVPolicy` worker,
and access through the bundled kernel driver `tools/intel/CCHWAPIExt.sys`
(`HWAPIExtDLL.dll`) — i.e. the Windows tool needs a kernel component only
because it pokes MSR/MMIO/PECI directly.

UI surface (`app.asar` i18n keys): `gameapp_adv_setting_unlock_max_tdp`,
`tdpFollowFramerate*` ("follow framerate → dynamic TDP", GPU vs TDP mode),
`intelPnPMode1/2/3` (energy saver / balanced / performance),
`gameapp_game_setting_preset_tdp`, `…_tdp_dcin`, `…_tdp_plugin`,
`…_tdp_dcincooling`, `powersupplymode_mode1/2/4/8` = battery / Type-C ≥100 W /
DC-in / Type-C ≤65 W and `…_mode4_2` Type-C ≥140 W.

### 2.2 Fan, battery, RGB: vendor EC field map

`CompatLayerCT.exe` (managed, 8.7 MB) carries the EC abstraction names:

```
addrFanSpeed, addrFanAutomate, DevsCPUFan, DevsCPUFanCurve,
DevsGPUFan, DevsGPUFanCurve, DevsMidFanCurve, fanSetFanSpeed, fanGetFanSpeed,
FAN_MAX_SPEED_VALUE, fanSpeedHECAddr/fanSpeedLECAddr, addrHandlePower,
addrBatteryTemp, addrCpuTemp, addrBoardSensor1/2, addrChargeLimit,
addrForceChargeMin, addrBypassPower, addrPowerSupplyMode,
batterySetChargeLimit, batterySetMaxTdpLimit, batterySetByPassPowerMode,
batteryGetPowerSupplyMode, batteryIsThirtyPartyBattery, batteryInitEc,
ChargerBarrel / ChargerUSB / BatteryDCIn / BatteryTypec65/100(/140),
ConvertGyroVector, ConvertGyroThumbValue, active_gyro
```

and the UI confirms the semantics: `cooling_runmode_intelli` "intelligent fan
curve", `cooling_runmode_fixspeed`, `qs_fan_presetting1/2`, `qs_fan_lock`,
`battery_protection_chargelimit`, `battery_protection_bypasspower`
("bypass power: battery inserted but not charged, device fed from the adapter"),
`cooling_rgb_settings`, `rgbPartition_{left_stick,right_stick,top,back,v,kb,gkey,controller_connector}`,
`rgbLightEffect` + `rog_le_mode_{always,breath,strobe,rainbow,circle}`.

### 2.3 Controller, gyro, display, camera, misc

* `XInputInterface.dll`, `tools/xinputplus/*` (XInputPlus injector), HidHide +
  ViGEmBus strings, `gameapp_ph_*` (stick→mouse/keyboard/macro mapping,
  "burn mode", second-mode key), `gyroscope_*` (simulate mouse / Xbox / DS4,
  trigger gating, steering-wheel modes, axis inversion, sensitivity).
* `HDRController.dll`, `tools/intel/SAGVPlugin.dll`, `qs_hdr_switcher`,
  `screen_vibrance`, `resolution` — display HDR/vibrance/refresh management.
* `resources/tools/intel/{PerfMode,IntelPowerPlugin,PowerPlugin,SAGVPlugin,HWAPIExtDLL}`,
  `PawnIOLib.dll` + `PawnIO_setup.exe`, `inpoutx64.dll` (port I/O → the EC
  path), `PresentMon`, `RTSS` (overlay/frame-limit, used by `tdpFollowFramerate`).
* `tools/intel/perf_conf.ini` + `CCHWAPIExt.sys` is the whole "DTT-like"
  policy; `other/ipfppm_sw…/configuration/PPM-*.ppkg` are Intel's policy
  packages (opaque, installed but not needed by the values above).
* Suspend/session extras: `handykit_sleep_to_hibernate`,
  `handykit_downloadmode*` (screen off + auto power-off), OCuLink eGPU driver
  hot-swap (`handykit_external_gpu_hotswap_disabled`).

---

## 3. Ranked gap table

Severity: **S1** = feature dead / data-loss risk, **S2** = clear functional or
performance loss, **S3** = quality-of-life, **S4** = cosmetic/polish.
"Kind": **U** = userspace only, **K** = kernel patch, **FW** = firmware/package,
**B** = blocked (upstream/proprietary).

| # | area | Windows capability | current Linux state + evidence | sev | proposed solution | effort/risk | validation command | kind |
|---|---|---|---|---|---|---|---|---|
| 1 | Bluetooth | full BT via Intel `ibtpci` | device present, **radio unusable**: `hci0: Failed to load Intel firmware file intel/ibt-00a0-0291-iml.sfi (-2)`; `rfkill` has only `phy0` (wlan); `linux-firmware-20260916` has no `ibt-00a0-0291*` | **S1** | ship `ibt-00a0-0291-iml.sfi/.ddc` (+`-sfi`) in `linux-firmware` (upstream file exists in newer upstream releases) | low / low | `ls /lib/firmware/intel/ibt-00a0-0291*; rfkill list; bluetoothctl show` | FW |
| 2 | Fan control | `fanSetFanSpeed` + `fanAutomate` + 3 curve tables (CPU/GPU/mid) + per-profile `fanSpeed`, presets, lock | `oxp_ec` hwmon gives `pwm1`/`pwm1_enable`/`fan1_input` but `pwm1_enable`=2 (EC auto), no curve, no per-partition control, no preset API; `TFN1` cooling device unusable (`cur_state` read error) | **S2** | userspace: `pwm1_enable=1` + a curve daemon reading `coretemp`/`TCPU`; kernel: expose the EC curve registers (`DevsCPUFanCurve`) once RE'd | med / med | `echo 1 \| sudo tee /sys/class/hwmon/hwmon6/pwm1_enable; echo 180 > …/pwm1; cat …/fan1_input` | U + K |
| 3 | Power envelope | PL1+PL2+PL4+τ per profile, MSR **and** MMIO, PECI readback, Quiet/Balanced/Perf presets | only PL1 35 W synced **[covered]**: MSR PL1 35 W / PL2 37 W, MMIO PL1 35 W / PL2 52 W, `constraint_2` 122 W (likely PL4), `constraint_0_time_window_us` 27.98 s — vendor wants 10/25, 25/45, 35/80 W with PL4 90/160/239 W and τ=56 | **S2** | userspace `powercap` writer for `constraint_0/1` (+`time_window_us`) in **both** domains + profile presets; τ unit in the ini is not proven (56 ms vs 5.6 s — verify against `constraint_0_time_window_us` behaviour) | low / med (thermal) | `for d in intel-rapl:0 intel-rapl-mmio:0; do cat /sys/class/powercap/$d/constraint_1_power_limit_uw; done` | U |
| 4 | RGB zones / effects | 8 partitions (`left_stick,right_stick,top,back,v,kb,gkey,controller_connector`) + effect engine (always/breath/strobe/rainbow/circle), per-zone apply | `tools/oxp3_hid.py` covers 5 zone ids (0x01,0x02,0x05,0x06,0x07) + brightness/solid/preset on cid `0xB8`; **keyboard/gkey/V/controller-connector zones and the effect engine are missing** | S3 | extend the HID tool with the remaining zone ids and the effect/`rgbLightEffect` frames (capture on Windows or brute-force cid `0xB8` sub-commands) | low / low | `sudo tools/oxp3_hid.py zone 0x08 255 0 0` (after udev rule) | U |
| 5 | Battery care | charge limit + **bypass power** mode + third-party-battery detection | `charge_control_end_threshold`=80 works (writable by root), no start threshold, `charge_behaviour` has `inhibit-charge`; **no bypass-power**; EC fields `addrBypassPower`, `addrPowerSupplyMode` exist | S3 | add bypass + start-threshold attributes to `oxpec` (needs EC addr/cmd RE); meanwhile `charge_behaviour=inhibit-charge` approximates bypass | med / low | `cat /sys/class/power_supply/BAT0/charge_behaviour; echo inhibit-charge > …` | K |
| 6 | Gyro / IMU | gyro → mouse / virtual Xbox / DS4, trigger gating, steering modes, calibration | hardware **already works** (`iio:device0`, bmi260, full-rate accel+gyro). HHD 4.1.12 already names this exact node: `controller/physical/imu.py` `IMU_NAMES` contains `i2c-10EC5280:00`, and `device/oxp` uses `CombinedImu` + `HrtimerTrigger` + `gen_gyro_state(… anglvel_x→gyro_z …)` ⇒ path exists, but it is not enabled/verified on this box and `hhd-ui-bin` keeps dying on `int3` (journal) | S3 | turn on HHD's `imu`/`imu_hz`/`imu_axis` for the oxp profile and verify end-to-end; fallback is a small IIO→uinput bridge | low / low | `cat /sys/bus/iio/devices/iio:device0/{in_accel_x_raw,in_anglvel_x_raw}`; `journalctl -b \| grep hhd-ui` | U |
| 7 | Camera + IR | OV sensors + `INT3472` PMICs, Windows Hello IR | **no** `/dev/video*`; no IPU7 driver, no `ipu6/7` module, only `ipu3-fw.bin.xz`; sensors are raw MIPI-CSI behind `INT3472` | **S2** | blocked: needs IPU7 kernel support + libcamera pipeline + firmware; track upstream `intel-ipu7`/`ivsc` | high / high | `ls /dev/video* 2>&1; dmesg \| grep -iE 'ipu\|ovti'` | B |
| 8 | Fingerprint | Betterlife sensor (Windows Hello) | hardware **present on SPI** (`BF63112:00`, unbound), protocol recovered from the vendor UMDF driver; `libfprint-1.94.100` has no Betterlife ID and there is no in-tree SPI driver | S3 | out-of-tree bring-up already started in `tools/fingerprint/` (spidev probe → libfprint driver); long-term an upstream `spi`+libfprint driver | high / high | `ls -d /sys/bus/acpi/devices/BF63112:*; readlink /sys/bus/spi/devices/spi-BF63112:00/driver; grep -i betterlife /usr/lib/udev/rules.d/70-libfprint-2.rules` | K |
| 9 | Display HDR/colour/SAGV | `qs_hdr_switcher`, vibrance, per-profile SAGV point 0/3/4, refresh control | eDP advertises a single `1920x1200` mode; `drm_info`/`modetest` absent; SAGV point has no Linux interface at all (`PerfMode` writes it via MMIO) | S3 | userspace for HDR/colourmgmt once `drm_info` shows the props; SAGV needs a small xe patch (or i915-style `sagv` debugfs) | med / med | `drm_info \| grep -iE 'hdr\|color'; cat /sys/class/drm/card1-eDP-1/modes` | U + K |
| 10 | Adaptive TDP | `tdpFollowFramerate` (GPU-util 80/90 % thresholds, 10 s interval), per-charger-tier TDP | static PL1 only; charger tier (`ChargerBarrel/USB/Typec65/100/140`) not exposed (`ACPI0003`+`BAT0.status` only) | S3 | small daemon: RAPL write + `power_supply`/EC tier detection; frame-limit input from `PresentMon`-equivalent (MangoHud/`nvidia`-less: read `/proc` GPU busy or `xe` fdinfo) | med / low | `cat /sys/class/power_supply/{ADP1,BAT0}/{online,status}` | U |
| 11 | Touchpad features | precision-touchpad settings, palm rejection, haptic/click tuning (`hid-multitouch` on Windows via registry) | `GXTP6933:00` on `hid-multitouch`/`hidraw5`, input16/17 in place; no libinput quirk, no vendor settings | S4 | libinput quirk / udev property for the Goodix ID; verify clickpad + 2-finger scroll/palm | low / low | `libinput list-devices \| grep -A6 GXTP6933; libinput debug-events` | U |
| 12 | Suspend extras | `sleep_to_hibernate` timer, "download mode" (screen off, auto power-off), OCuLink eGPU driver swap | suspend fixed **[covered]** (`docs/hw/sleep-power.md`); no s2h timer, no download mode, no eGPU path | S4 | userspace: `systemd` `HibernateDelaySec=` in `sleep.conf`, a small "download mode" unit (backlight 0 + idle inhibit off) | low / low | `cat /sys/power/sleep; systemctl status systemd-suspend` | U |
| 13 | Thermal telemetry | vendor sensor set + fan curve temps (`addrCpuTemp`, `addrBoardSensor1/2`) | `coretemp`, `TCPU`, `acpitz`, `nvme`, `iwlwifi` OK; `acpi_fan: fan1_input` → `EIO` (breaks `sensors`), `TFN1.cur_state` unreadable | S4 | hide/ignore `acpi_fan` (`sensors -A` config) or fix `acpi_fan` read; report `TFN1` bug upstream | low / low | `sensors 2>&1 \| head -3` | U |
| 14 | DTT/DPTF policy | Intel DTT policies (`PPM-*.ppkg`, `DptfPolicy*`), EPP/energy-perf hints | `int3400` + `processor_thermal_*` loaded; no DPTF policy engine, no EPP knob exposed by default | S4 | userspace `platform_profile` (balanced/low-power/performance already present) + `energy_performance_preference` per CPU; full DPTF is proprietary | low / low | `cat /sys/firmware/acpi/platform_profile{,_choices}; cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference` | U |

Already covered by this repo (not gaps): audio APO/EQ (`docs/hw/audio-harman-apo.md`),
home button + base RGB (`docs/hw/home-button-leds.md`), volume rocker
(`docs/hw/volume-buttons.md`), suspend/resume (`docs/hw/sleep-power.md`),
PL1 35 W + RAPL energy (`files/rapl-power`), XMX/Mesa.

---

## 4. Top 5 next steps

1. **Bluetooth firmware (S1, 10 min).** Get `ibt-00a0-0291-iml.sfi` +
   `ibt-00a0-0291.sfi` (+`.ddc`) into `/lib/firmware/intel/` via the
   `linux-firmware` package (Anatase) rather than a manual copy, then
   `sudo modprobe -r btintel_pcie btintel bluetooth; sudo modprobe btintel_pcie`.
   First action: confirm upstream `linux-firmware` carries the file
   (`git log --all --oneline -- intel/ibt-00a0-0291*` in a linux-firmware
   clone) and file the Anatase/`ludos` packaging request.
2. **Power-capability writer (S2, ~1 h).** One small tool in `tools/` that
   writes `constraint_0/1_power_limit_uw` and `constraint_0_time_window_us` in
   *both* `intel-rapl:0` and `intel-rapl-mmio:0`, with Quiet/Balanced/Perf
   presets matching §2.1. First action: dump current values read-only for all
   four files, then a `--dry-run` writer with a `--restore` path.
3. **Fan mode + curve (S2, ~half day).** First action: prove manual control
   works (`pwm1_enable=1`, sweep `pwm1` 0→255, log `fan1_input`/`coretemp`),
   then a userspace curve daemon; open the EC-curve question (kernel) only if
   manual PWM proves insufficient.
4. **RGB completion (S3, ~2 h).** Extend `tools/oxp3_hid.py` with the missing
   zone ids (`kb`, `gkey`, `v`, `controller_connector` = vendor partitions)
   and the effect frames; validate each zone visually on the device.
5. **Gyro enablement (S3, ~1 h).** HHD already ships the exact IIO node in
   `IMU_NAMES` and a `CombinedImu`/`HrtimerTrigger` path, so first *enable* and
   verify it (`imu`, `imu_hz`, `imu_axis` in the oxp profile) instead of
   writing a new daemon; only fall back to a standalone IIO→uinput bridge if
   HHD's virtual-controller path stays broken. First action:
   `journalctl -b | grep -E 'hhd|imu|iio'` plus a rotation test while watching
   `evdev` output.

Kernel-patch / upstream items to flag (not closable in userspace):
SAGV point control (#9), EC bypass-power + fan-curve attributes in `oxpec`
(#2/#5), camera/IPU7 (#7), `acpi_fan`/`TFN1` telemetry bugs (#13).

---

## 5. Open points / limits of this audit

* `DSDT`/`SSDT` and `/sys/kernel/debug/*` are root-only, so the ACPI method
  bodies behind the four **unbound WMI GUIDs** could not be read. Next probe:
  `sudo acpidump -b -o /tmp/acpi && acpixtract -a /tmp/acpi/*.dat && iasl -d *.dsl`
  then grep `_WDG`/`_WED` for `1F13AB7F…`, `2BC49DEF…`, `43B5A593…`,
  `A6FEA33E…`. Those GUIDs are the most likely vendor channel besides the
  WCH HID device.
* Windows-side EC *command bytes* (as opposed to field names) live in native
  code paths not present in the NSIS payload we hold (`inpoutx64` port I/O
  wrapper is there, the EC driver object is not). The register **names** and
  the profile **values** above are direct evidence of capability; the exact
  EC offsets would need a Windows-side USB/port trace or a `CompatLayerCT`
  IL decompile (ILSpy/dnSpy under Wine) — flagged, not done.
* `iio:device0` was read but not streamed; gyro sign/scale verification needs
  a physical rotation test.
* RAPL writes were **not** attempted (no root). Writable-ness is inferred from
  `-rw-r--r-- root` mode and standard `intel-rapl` behaviour; a `--dry-run`
  tool plus one supervised test is step 2.
* HHD 4.1.12 already covers controller emulation, `tt_toggle` turbo takeover and
  the IMU, but its `plugins/power` backend has **no Intel TDP path** (it only
  does hibernate/reboot), so the `powercap` writer in step 2 is net-new — a
  natural upstream contribution to HHD afterwards. `hhd-ui-bin` also dies
  repeatedly with `trap int3` (kernel log, 18:04–18:18) — worth its own look,
  and it may be why gyro/TDP settings never stuck.
* `/tmp` on this host has a per-user tmpfs quota — a 600 MB extraction filled
  it and broke tooling; all extraction for this audit was redone under
  `~/.cache/gapaudit` (outside the repo, nothing committed).

## 6. Reproduction / evidence paths

```bash
# inventory
lspci -nn; lsusb -v 2>/dev/null | head; ls /sys/class/{leds,backlight,thermal,hwmon,power_supply,input,hidraw,rfkill}
for z in /sys/class/thermal/thermal_zone*; do echo "$z $(cat $z/type) $(cat $z/temp)"; done
sensors; cat /proc/cmdline; grep -c ^processor /proc/cpuinfo

# Windows material (read-only, untrusted)
7z x OneXConsole_0.10.4-fix1.exe '$PLUGINSDIR/app-64.7z'   # then asar header parse → app.asar
cat app/resources/resources/tools/intel/perf_conf.ini
strings CompatLayerCT.exe | grep -E '^addr|^Devs|^battery|^fan'   # EC field names
tools/ghidra-pod.sh gapaudit …/tools/intel/PerfMode.exe '.*'      # MSR/MMIO/PECI, AutoSAGV

# kernel-side evidence
journalctl -k -b | grep -iE 'hci0|ibt|ovti|ipu|oxpec|bmi'
cat /sys/class/powercap/intel-rapl:0/constraint_*; ls /sys/class/bluetooth
```

Nothing in this document was executed as root, no package or config was
changed, and no vendor binary, DLL, firmware or decompiled listing is stored
in this repository.
