# OneXPlayer 3: camera (IPU7 / OVTI / INT3472) and IMU (BMI260) enablement

Status: inventory + RE complete, no host changes, no module loads, no root.
Target: ONEXPLAYER 3 (`ONEXPLAYER 3`, BIOS 5.09), Anatase `20261026.3`,
kernel `7.2.7-an28.fc45.x86_64`, `linux-firmware-20260916.an03`, HHD
`4.1.12+git.154`.
Vendor material (untrusted data, read-only): `~/Downloads/oxp3/`.

**TL;DR**

| | verdict |
|---|---|
| Camera | **No camera exists/enabled on this unit.** No IPU7 PCI function is enumerated (`8086:b05d` absent), all 4 `OVTI*` sensors **and** all 12 `INT3472` devices report ACPI `_STA = 0` ("not present"), the vendor Windows package contains **no camera driver at all**, and OneXConsole has no camera UI. Nothing userspace can do — this is not a Linux driver gap on this box. |
| IMU | **Works today and is already exposed to Steam.** Bosch BMI260 on `i2c-10EC5280:00` (`bmi260_core`), IIO at 100 Hz with mount matrix; HHD streams it through an hrtimer trigger named `hhd` and syncs gyro into its emulated Steam Controller (`hidraw6`, VID `28DE:12FF`). Shipped: `tools/oxp3-imu.py` diagnostics + this document; no config change was needed. |

---

## 1. Environment and method

All inventory was read-only (`lspci`, `lsusb`, `/sys`, `journalctl`, `modinfo`,
`rpm -ql`, `hhdctl get`, HHD's read-only UNIX-socket API). Nothing was
modprobed, no ACPI table was dumped (root-only), no BIOS setting touched.
The kernel source used for driver cross-checks is a local 7.2.x tree
(`kbuild-an19-src/`) with the same media configuration as the running
`kernel-core-7.2.7-an28`; driver/HID coverage was read from there.

Kernel configuration relevant to this document (from `/proc/config.gz`):

```
CONFIG_MEDIA_SUPPORT=m  CONFIG_MEDIA_CONTROLLER=y  CONFIG_VIDEO_V4L2_SUBDEV_API=y
CONFIG_IPU_BRIDGE=m     CONFIG_VIDEO_INTEL_IPU6=m   CONFIG_VIDEO_INTEL_IPU7=m
CONFIG_INTEL_MEI=m      CONFIG_INTEL_MEI_VSC=m      CONFIG_INTEL_SKL_INT3472=m
CONFIG_VIDEO_OV08X40=m  CONFIG_VIDEO_OV13B10=m      CONFIG_VIDEO_OV01A10=m
CONFIG_FW_LOADER_COMPRESS_XZ=y
```

## 2. Camera: measured inventory

### 2.1 PCI — no imaging function at all

30 PCI functions, none with vendor/device `8086:b05d` (IPU7P5, the Panther Lake
IPU) and none in PCI class `0x0480` (multimedia controller):

```
00:00.0 host bridge     00:02.0 VGA (B390)      00:04.0 DTT      00:06.0/00:07.0/00:07.1 bridges
00:0a.0 crashlog/telemetry (class 1180)         00:0b.0 NPU (class 1200)
00:0d.x xHCI/TBT DMA    00:14.x USB/WiFi/BT/SRAM
00:15.0-3 I2C #0-3      00:19.0 I2C #4          00:1e.0 UART     00:1e.3 GSPI #1
00:16.0/00:18.0 CSME HECI                       00:1f.0 eSPI     00:1f.3 HD Audio
00:1f.4 SMBus           00:1f.5 SPI flash       01:00.0 NVMe     56:00.0 RTS525A
```

There is **no `00:05.0`**. On a Panther Lake design with a MIPI camera the IPU is
`8086:b05d` (called "IPU7P5" in the kernel driver, shared by ARL/PTL) and its
`intel_ipu7` driver registers the CSI-2 host and the I2C adapters the sensors
sit on. Without the PCI function, no media graph can exist.

### 2.2 USB — no UVC either

```
1a86:fe00  WCH vendor composite (3 HID interfaces -> hidraw2/3/4)
045e:028e  Xbox 360 pad (xpad)
1997:2433  Riitek mini keyboard/touchpad
```

No camera, no IR camera, no USB bridge.

### 2.3 ACPI — the whole camera block is "not present" (`_STA = 0`)

| HID | ACPI path | status | meaning |
|---|---|---|---|
| `OVTI00AB:00` | `\_SB.LNK1` | **0** | sensor slot 1, not present |
| `OVTI08F4:00` | `\_SB.LNK2` | **0** | sensor slot 2 (ov08x40 family), not present |
| `OVTI01AF:00` | `\_SB.LNK3` | **0** | sensor slot 3, not present |
| `OVTI01AF:01` | `\_SB.LNK4` | **0** | sensor slot 4, not present |
| `OVTI01AF:02` | `\_SB.LNK5` | **0** | sensor slot 5, not present |
| `OVTI13B1:00` | `\_SB.LNK0` | **0** | sensor slot 0 (ov13b10 family), not present |
| `INT3472:00` … `INT3472:0b` | `\_SB.DSC0` / `\_SB.DSC1` for `:00`/`:01`, rest siblings in the same block | **0** | 12 discrete sensor PMICs, not present |
| `10EC5280:00` | `\_SB.PC00.I2C1.SPBA` | 15 | **IMU, present** (see §6) |

The sensor and PMIC devices are declared directly under `\_SB` with the
Intel reference-design names `LNK0..LNK5` ("camera link") and `DSC0/1`
("discrete sensor controller"), not under an I2C adapter scope. `_STA = 0`
means the firmware reports them as not present, so the Linux ACPI core never
creates platform/I2C devices for them: `intel_skl_int3472_discrete` cannot
bind, no sensor is probed, and nothing provides the camera power-enable,
reset, privacy-LED or 19.2 MHz clock GPIOs.

For comparison, the working IMU is `_STA = 15` and does get instantiated below
its `I2C1.SPBA` scope — the mechanism works, the camera devices are simply
disabled.

### 2.4 I2C buses and clients — nothing camera-related

```
i2c-0..4   Synopsys DesignWare (LPSS)         i2c-5..9   i915 gmbus dpa..e
i2c-10..13 i915 gmbus tc1..tc4                i2c-14..18 AUX A / TC1..TC4
i2c-19     SMBus I801
clients:   i2c-10EC5280:00 -> bmi260_i2c      i2c-GXTP6933:00 -> i2c_hid_acpi
```

No `i2c-OVTI*`, no `i2c-INT3472*`. The IPU7 driver would create its own I2C
adapters (for sensor control) under `intel_ipu7.isys.N`; they do not exist.

### 2.5 Media / V4L2 devices — none

`/dev/media*`, `/dev/video*`, `/dev/v4l-subdev*`, `/dev/v4l-by-index` do not
exist. `journalctl -k -b` contains **no** `ipu7`/`ipu6`/`isp`/`ivsc`/`ovti`/
`int3472` line at all: no driver ever probed, so there is nothing to fix in
kernel logs.

### 2.6 Modules present in the kernel tree (none loaded)

```
drivers/staging/media/ipu7/intel-ipu7.ko.xz         (CONFIG_VIDEO_INTEL_IPU7=m)
drivers/staging/media/ipu7/intel-ipu7-isys.ko.xz    (auxiliary bus: intel_ipu7.isys)
drivers/media/pci/intel/ipu6/intel-ipu6*.ko.xz
drivers/media/pci/intel/ipu3/ipu3-cio2.ko.xz
drivers/media/pci/intel/ipu-bridge.ko.xz
drivers/media/pci/intel/ivsc/ivsc-{ace,csi}.ko.xz
drivers/platform/x86/intel/int3472/intel_skl_int3472_{common,discrete,tps68470}.ko.xz
drivers/media/i2c/ov08x40.ko.xz, ov13b10.ko.xz, ov01a10.ko.xz, ov02*10, …
```

`modinfo intel-ipu7`:

```
description: Intel ipu7 pci driver
alias:       pci:v00008086d0000B05Dsv*sd*bc*sc*i*   /* IPU7P5, ARL / PTL */
alias:       pci:v00008086d0000645Dsv*sd*bc*sc*i*   /* IPU7, MTL / LNL */
```

### 2.7 Firmware — present, not the blocker

The `intel-ipu7` driver asks for `intel/ipu/ipu7ptl_fw.bin` on IPU7P5
(`ipu7.h:23`), and the installed `linux-firmware` **has it**:

```
/usr/lib/firmware/intel/ipu/ipu7ptl_fw.bin.xz   123 KiB xz -> 260 KiB (266240 B)
/usr/lib/firmware/intel/ipu/ipu7_fw.bin.xz      121 KiB xz -> 260 KiB
/usr/lib/firmware/intel/ipu/ipu6*_fw.bin.xz, ipu3-fw.bin -> ipu/irci_…
```

266240 bytes is the size of the upstream product-signed `ipu7ptl_fw.bin`
(version `master_release_20250515_1232_ProductSigned`), and
`CONFIG_FW_LOADER_COMPRESS_XZ=y`, so the `.xz` name resolution works. An
earlier audit note ("only ipu3-fw.bin") was wrong; that was a bad glob.

### 2.8 Userspace — camera stack absent

* no `libcamera` package at all (`rpm -qa | grep -i camera` → nothing; only
  `libv4l-1.32.0`), so no IPU7/softISP pipeline handler;
* no `v4l-utils` (`v4l2-ctl`, `media-ctl`), no `cam`/`qcam`;
* PipeWire's libcamera SPA plugin is missing, hence the harmless
  `wireplumber: SPA handle 'api.libcamera.enum.manager' could not be loaded`
  warnings on every session start.

## 3. Camera: what exactly is missing, layer by layer

| layer | state | consequence |
|---|---|---|
| BIOS/firmware: IPU7 PCI function `8086:b05d` | **absent** from bus | no CSI-2 host, no IPU I2C adapters, no `/dev/media*` |
| ACPI: `OVTI*` sensors + `INT3472` PMICs | **`_STA = 0`** | no I2C clients, no power/reset/clock/privacy-LED, no VCM |
| sensor drivers | in-tree for `OVTI08F4` (ov08x40), `OVTI13B1` (ov13b10), `OVTI01A0/01AB` (ov01a10), `OVTI02C1` (ov02c10), `OVTI02E1` (ov02e10); **none** for `OVTI00AB` or `OVTI01AF` (and none for `OVTI05C1`/`OVTI08A1` either) | even with ACPI enabled, the sensors this DSDT names would not all probe; new drivers needed |
| `ipu-bridge` sensor table | `OVTI01A0`, `OVTI01AS`, `OVTI02C1`, `OVTI02E1`, `OVTI05C1`, `OVTI08A1`, `OVTI08F4`, `OVTI13B1`, `OVTIDB10`, `OVTI2680`, `OVTI5675`, `OVTI8856` — **no `OVTI00AB`, no `OVTI01AF`** | sensor→CSI-2 link configuration (lane count/link frequency) is unknown for those |
| `intel_skl_int3472_discrete` | module present, cannot bind (`_STA=0`) | no regulator/GPIO/clock provider for any sensor |
| IPU7 kernel driver | staging `intel-ipu7` (`CONFIG_VIDEO_INTEL_IPU7=m`), PTL fw ABI bug known (see §5) | would need a working, non-buggy driver for the exact firmware |
| firmware | **present**, product-signed | not a blocker |
| libcamera pipeline | **absent** (package not installed, and IPU7 needs the IPU7/softISP support that landed via the ipu6 driver patches, see §5) | no capture API even with a working kernel graph |

## 4. Reverse engineering the Windows side: there is no vendor camera stack

The vendor driver package `OXP-Gi78P-ONEXPLAYER3-Drivers-1.1` contains:

```
1_chipset  2_Graphics  3_ME  4_SST  5_TF  6_Bluetooth  7_WIFI  8_Audio
9_BMI (BMI260 + BMI323 UMDF drivers)  10_FingerPrint  other/  Setup.bat
```

* 152 `*.inf` files, **zero** matching `OVTI|INT3472|IPU7|IMAGING`
  (`grep -ril … --include=*.inf`), and no camera/IPU/imaging directory or
  file name anywhere in the package.
* Conclusion: on Windows the camera (if the SKU had one) is driven by the
  **Windows-inbox Intel IPU7 driver plus inbox/Windows-Update sensor drivers**;
  the OEM ships nothing camera-specific. There is therefore **no vendor camera
  binary to reverse** — that is a finding, not a task we skipped.
* OneXConsole `0.10.4` (`/tmp/oxp3-onex/app/resources/app.asar`, 61 MB):
  the 50 `camera` matches are all icon names (`CameraFilled`, …), Electron's
  USB device-type classifier, and a bundled USB vendor-ID database
  ("Impossible Camera GmbH"); `摄像头`, `webcam`, `红外` (infrared) and
  "Windows Hello" each appear 0 times. No camera control, no camera page, no
  camera setting exists in the console app. (The gyro/IMU
  hits in the same file are the controller-gyro feature already covered by
  `gap-audit.md`.)
* The `other/issei.inf` and `ipf_*.inf` modules are Intel sensor/DTF
  framework helpers (no `OVTI`/`INT3472` HIDs); they do not add camera
  support.

Which IPU generation and sensors the platform *declares* (from the kernel
driver and the DSDT names, not from vendor binaries):

* **IPU7.5 / IPU7P5** — PCI `8086:b05d`, firmware `intel/ipu/ipu7ptl_fw.bin`
  (the driver comment says "ARL / PTL"). This matches Panther Lake.
* Sensors: `OVTI00AB`, `OVTI01AF` (×3), `OVTI08F4`, `OVTI13B1` — an Intel
  reference-design camera SSDT (6 links `LNK0..5` + 2 discrete sensor
  controllers `DSC0/1`), not a populated camera bill of materials.
* **IVSC is not involved.** `ivsc-ace` and `ivsc-csi` bind MEI clients with
  GUIDs `5db76cf6-0a68-4ed6-9b78-0361635e2447` / `92335fcf-3203-4472-af93-7b4453ac29da`;
  the nine MEI clients on `00:16.0` expose neither (only `mei_gsc_proxy` is
  bound), so there is no IVSC device. Panther Lake camera designs wire sensors
  directly to the IPU7 CSI-2 receiver and use `INT3472` for power/reset/clock
  — exactly the topology the DSDT names, and exactly what `_STA=0` disables.

Ghidra was run on the only sensor-adjacent binary, `9_BMI/BMI260/BMI160.dll`
(see §7); nothing in the package is camera-related, so no Ghidra work applies
to the camera.

## 5. Camera: feasibility verdict and what would have to be true

**Verdict for this unit: not feasible because the hardware is not exposed —
and most likely not fitted.** No camera is listed in the OneXPlayer 3
specification ([onexplayer.com product/spec table](https://m.onexplayer.com/product/OneXPlayer%203),
[user manual PDF](https://m.media-amazon.com/images/I/61wEXaI6nAL.pdf)):
the device advertises an 8.8" AMOLED, fingerprint power button (Windows Hello
via fingerprint, not IR face), Wi-Fi 7/BT 5.4, 85 Wh — no camera.

If a differently-configured unit (or a BIOS option) exposes the camera, this
is the complete requirement list:

1. **Firmware/BIOS**: a "Camera"/"MIPI camera" setup option (or an EC/GPIO
   camera-enable) that makes `8086:b05d` enumerate and flips `OVTI*`/`INT3472`
   `_STA` to `0x0f`. Verify with the commands in §8.
2. **Kernel**: `CONFIG_VIDEO_INTEL_IPU7=m` + `IPU_BRIDGE` + `MEDIA_CONTROLLER`
   + `VIDEO_V4L2_SUBDEV_API` + `IOMMU_IOVA` — all already in `7.2.7-an28`.
   Caveat: this driver is **staging** and the PTL firmware had an ABI break
   that makes streaming fail with `stream start time out`; Intel's
   `ipu7-drivers` PV release candidate carries an internal workaround, and
   upstream is moving IPU7 support into the **ipu6** driver instead
   ([Ubuntu bug 2138721](https://bugs.launchpad.net/ubuntu/+source/linux-firmware/+bug/2138721),
   [LKML Sept 2026 IPU7-on-ipu6 series](https://lkml.org/lkml/2026/9/29/728)).
   Pick the driver generation that matches the sensor patches.
3. **Firmware files**: `intel/ipu/ipu7ptl_fw.bin` (present, product-signed).
4. **Sensor drivers**: `ov08x40` / `ov13b10` exist for `OVTI08F4`/`OVTI13B1`;
   `OVTI00AB` and `OVTI01AF` have **no in-tree driver** and no `ipu-bridge`
   entry (link frequencies/CSI lanes unknown) — new drivers would be needed.
5. **`INT3472` power sequencing**: `intel_skl_int3472_discrete` provides the
   enable/reset GPIO, privacy LED and 19.2 MHz clock from ACPI, but the
   sensor driver must name the supplies it expects (the VD55G1 patch series
   is the reference example of that mapping).
6. **Userspace**: `libcamera` with an IPU7-capable pipeline (softISP),
   `v4l-utils`, and PipeWire's libcamera SPA plugin. Fedora 45 currently
   ships none of these installed here.

**What userspace can do today: nothing.** There are no device nodes, no media
graph and no firmware-load attempt. The only actionable user step is to check
BIOS/firmware exposure as in §8.

## 6. IMU: measured inventory and verdict

### 6.1 Hardware and kernel

```
ACPI     10EC5280:00   path \_SB.PC00.I2C1.SPBA   _STA=15
i2c      i2c-10EC5280:00 -> driver bmi260_i2c (library module bmi260_core)
IIO      iio:device0   name "i2c-10EC5280:00"  label "accel-display"
```

| attribute | value |
|---|---|
| `in_accel_scale` | `0.002394` m/s²/LSB (steps `0.000598/0.001197/0.002394/0.004788` = ±2/4/8/16 g) |
| `in_anglvel_scale` | `0.001065` rad/s/LSB (steps down to `0.000066` = ±125…2000 dps) |
| `in_accel_sampling_frequency` | `100.0` Hz (available `0.78125 … 1600`) |
| `in_anglvel_sampling_frequency` | `100.0` Hz (available `25 … 3200`) |
| `in_mount_matrix` | `0, 1, 0; 1, 0, 0; 0, 0, -1` |
| `trigger/current_trigger` | `hhd` (HHD's hrtimer, `100.000000` Hz) |
| `buffer/enable` | `1` |
| measured | `|a| = 9.76 m/s²` at rest, median sample interval `10.11 ms` (≈98.9 Hz) |

The IIO sysfs attributes are world-readable (`0644`); `/dev/iio:device0` is
`0600 root`, which is why the shipped tool measures through sysfs (it never
competes with HHD for the buffer).

Kernel prerequisites (already met on this machine):

* a **BMI260** driver that claims ACPI `10EC5280`. The Anatase kernel builds
  one as `CONFIG_BMI260` from `drivers/custom/bmi260` — HHD's own out-of-tree
  driver, [github.com/hhd-dev/bmi260](https://github.com/hhd-dev/bmi260) —
  packaged as the `kernel-modules-akmods-7.2.7-an28` module
  (`…/kernel/drivers/custom/bmi260/bmi260_i2c.ko.xz`, aliases
  `acpi*:10EC5280:*`). It is a third-party GPL-2.0 kernel module, not vendored
  in this repo.
* `/etc/modprobe.d/oxp3-bmi160.conf` → `blacklist bmi160_i2c`. The in-tree
  `bmi160_i2c` also matches `10EC5280` (it carries a DSDT workaround comment
  about broken firmware) and would otherwise bind first, leaving a
  misdetected/dead sensor. The drop-in is shipped as
  [`files/oxp3-bmi160.conf`](../../files/oxp3-bmi160.conf).

### 6.2 HHD and Steam exposure — already working

Live HHD state (`hhdctl get`, read-only):

```
controllers.oxp.imu=true
controllers.oxp.imu_hz=100
controllers.oxp.imu_axis.mode=default
controllers.oxp.controller_mode.mode=hori_steam
controllers.oxp.controller_mode.hori_steam.sync_gyro=true
controllers.oxp.controller_mode.hori_steam.flip_z=true
```

`hori_steam` emulates a **Steam Controller (HHD)**, VID `28DE` PID `12FF`, which
appears as `hidraw6`: Steam Input sees a controller that carries motion, so
gyro is usable in Steam today. HHD's code path is
`device/oxp/base.py` → `CombinedImu` + `HrtimerTrigger` (`IMU_NAMES` contains
`i2c-10EC5280:00`) → `get_outputs(..., motion=conf["imu"], ...)` →
`Dualsense/SteamdeckController(sync_gyro=...)`. Mapping (from
`device/oxp/const.py`, applied on top of the kernel mount matrix):

```
accel:   accel_x -> accel_z(+), accel_y -> accel_x(-), accel_z -> accel_y(-)
gyro:    anglvel_x -> gyro_z(+), anglvel_y -> gyro_x(-), anglvel_z -> gyro_y(-)
```

So: **the IMU is usable, at full rate, and already routed to Steam.** The
previous gap-audit entry ("path exists, not enabled/verified") is now resolved:
it is enabled and verified (see `tools/oxp3-imu.py selftest`).

### 6.3 Tuning knobs (only if the default is not good enough)

* `imu_hz`: `100` (default) / `200` / `400` / `800` in HHD's oxp settings.
  100 Hz is enough for gyro aiming; 200–400 Hz reduces latency at the cost of
  ~0.3 % CPU per HHD's own hint.
* `imu_axis`: HHD's per-device remap (`x/y/z` + invert). Use
  `tools/oxp3-imu.py watch` and yaw/pitch/roll the device to check signs
  before changing it.
* controller mode: `hori_steam` (current, Steam Controller + motion),
  `dualsense` (DualSense HID + motion, `sync_gyro=true`), `uinput` (plain
  Xbox pad — **drops motion**). If a game behaves badly with the Steam
  Controller emulation, `dualsense` is the alternative that still carries gyro.
* `iio-sensor-proxy` (active, used by `monitor-sensor` for orientation) reads
  the same accelerometer. It is harmless, but if the gyro ever stutters,
  `sudo systemctl mask --now iio-sensor-proxy` removes the second reader.

## 7. RE evidence for the IMU (and why Ghidra added little)

`9_BMI/BMI260/BMI160.inf` (same driver family as the "BMI260" folder):

```
%BMI160_DevDesc% = BMI160_Inst, ACPI\10EC5280     <-- our device
%BMI160_DevDesc% = BMI160_Inst, ACPI\BMI0160 / BMI0260 / BOSC0160 / BOSC0260
UmdfLibraryVersion = 2.15.0 ; UmdfExtensions = SensorsCx0102
ServiceBinary = %12%\UMDF\BMI160.dll
```

`9_BMI/BMI323/.../BoschAccelerometer.inf` matches only `ACPI\BOSC0200`, so the
BMI323 package is **not** our part. `BMI160.dll` is PE32+ x86-64 (2022,
`SensorsUtilsV2.dll`, "Bosch Sensortec GmbH", project pdb `UMDF2.0_BMI160`),
i.e. a UMDF **Windows Sensor API** driver — the Windows analogue of the IIO
device we use here.

Ghidra (`tools/ghidra-pod.sh camimu …/BMI160.dll '^(.*(Bmi|BMI|Sensor|Gyro|
Accel|Report|Rate|Fifo|Init).*)$'`) recovered exactly one symbol
(`InitPropVariantFromFloat`, an inlined CRT helper): the DLL is stripped C++
and the interesting logic lives in `SensorsUtilsV2.dll`. Decompilation
therefore added nothing beyond the INF; the decisive identification is the
INF HID mapping plus the kernel's own chip-id probe (the `bmi260_core` driver
accepted the part, giving the ±2/4/8/16 g and ±125…2000 dps ranges reported
above). Documented so nobody repeats the dead end.

## 8. Privileged verification commands (not run here)

No `sudo` was used and no host state was changed. These are the exact commands
to settle the remaining open points on a machine that might have a camera:

```bash
# 1. Does the IPU7 PCI function exist at all (also bypasses sysfs hiding)?
sudo lspci -nn -d 8086:b05d
sudo lspci -A intel-conf1 -s 00:05.0 -xxx

# 2. Are the camera ACPI devices permanently disabled, or gated by EC/GPIO?
sudo acpidump -b -o /tmp/acpi && cd /tmp/acpi && iasl -d dsdt.dat
grep -n -E "LNK[0-5]|DSC[01]|OVTI|INT3472" dsdt.dsl | head -40
#   look at each Device (_STA) -- if _STA reads a GPIO/EC field the camera is
#   switchable; if it returns a constant 0 the firmware never exposes it.

# 3. Did anything try to probe?
sudo dmesg | grep -iE "ipu7|ipu6|ivsc|int3472|ovti|10EC5280|bmi260"

# 4. Force the staging IPU7 driver to load against a present function
sudo modprobe intel-ipu7 && sudo dmesg | tail -20   # fails cleanly with no 8086:b05d

# 5. IMU chip-id / probe detail
sudo dmesg | grep -i bmi260   # driver name, i2c address, chip id accepted

# 6. HHD settings (read as user works, write as root to be safe)
hhdctl get controllers.oxp.imu controllers.oxp.imu_hz controllers.oxp.controller_mode.mode
sudo hhdctl set controllers.oxp.imu_hz=200
sudo hhdctl set controllers.oxp.controller_mode.mode=dualsense
#   /etc/hhd/state.yml is 0600 root; back it up before changing anything
#   (sudo cp -a /etc/hhd/state.yml /root/state.yml.bak).
```

BIOS: look for a `Camera` / `MIPI Camera` / `IPU` option. If it exists and
`lspci` + `_STA` still say "absent", the unit has no camera hardware.

## 9. What we ship

| file | purpose |
|---|---|
| `tools/oxp3-imu.py` | read-only IMU diagnostics: `info` (device, scales, mount matrix, sampling, trigger, HHD state, Steam exposure), `rate` (measured sample rate), `watch` (live SI values for axis checks), `selftest` (PASS/FAIL, exit code). No root, no writes, `--json`. |
| `files/oxp3-bmi160.conf` | the `blacklist bmi160_i2c` drop-in that keeps the custom `bmi260` driver bound (already installed on this box; documented so a reinstall/two-kernel setup can reproduce it). |
| `docs/hw/camera-imu.md` | this document. |
| `docs/hw/README.md` | index row. |

Verified on this machine:

```
$ tools/oxp3-imu.py selftest
[OK] IIO device present (ACPI 10EC5280 / label accel-display)
[OK] kernel driver bound: bmi260_i2c
[OK] accel scale 0.002394 m/s^2/LSB is a BMI260 range step
[OK] gyro scale 0.001065 rad/s/LSB is valid
[OK] mount matrix provided (orientation usable for Steam)
[OK] data is flowing: ~99 Hz observed
[OK] accel magnitude 9.76 m/s^2 ~= 1 g (device at rest on a table)
[OK] buffered consumer attached: trigger 'hhd', buffer_enable=1
[OK] HHD motion support enabled (imu=True)
[OK] HHD controller mode carries gyro to Steam: hori_steam (sync_gyro=True)
[OK] HHD imu_hz=100, imu_axis 'default'
[OK] emulated Steam Controller hidraw present: /dev/hidraw6
[OK] iio-sensor-proxy active=True
RESULT: PASS (0 warning(s)) - IMU usable, gyro exposed to Steam
```

Camera verification (read-only, the "nothing exists" check):

```
$ ls /dev/media* /dev/video* 2>&1
$ ls -d /sys/bus/acpi/devices/{OVTI,INT3472}* | wc -l           # 18 devices (6 OVTI + 12 INT3472)
$ for d in OVTI00AB:00 OVTI08F4:00 INT3472:00; do cat /sys/bus/acpi/devices/$d/status; done  # 0 0 0
$ lspci -nn -d 8086:b05d                                        # empty
```

## 10. Open points / limitations

* **DSDT not read** (root-only): we cannot prove whether the camera block's
  `_STA` is a constant 0 or gated by an EC/GPIO; §8 command 2 settles it.
  Everything observable from userspace says "no camera", and OXP3 spec sheets
  list none.
* **Steam in-game gyro not exercised** in this session (no game launched).
  The chain is verified up to and including the emulated Steam Controller HID
  node with `sync_gyro=true`; the remaining step is a Steam Input check in a
  real session (`Steam → Controller settings → gyro`).
* **Camera SKU uncertainty**: if some OXP3 units ship a camera, the
  requirement list in §5 applies, and the two unknown sensor HIDs
  (`OVTI00AB`, `OVTI01AF`) would need drivers; effort is high (new sensor
  driver + `ipu-bridge` entry + INT3472 supply mapping + libcamera).
* **IPU7 upstream churn**: staging `intel-ipu7` vs the IPU7-in-ipu6 effort;
  PTL firmware ABI mismatch is a known streaming blocker upstream. Not
  actionable without hardware.
* **iio-sensor-proxy/HHD buffer sharing** cannot be inspected without root;
  the mask in §6.3 is the fallback if gyro quality drops.
