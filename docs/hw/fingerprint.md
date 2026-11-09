# ONEXPLAYER 3 fingerprint reader: what it is, and why Linux cannot use it yet

Status: hardware identified, Windows protocol recovered far enough to build a
frame-accurate probe, **no Linux support exists and none is close** - a new
libfprint SPI driver is the only path. The remaining unknowns are listed in
[Open points](#open-points); two of them need root on this unit.
Owner: silentsudin. Host: Anatase 20261026.3, kernel 7.2.7-an28.fc45,
libfprint 1.94.100, fprintd 1.94.5.

## Summary

The sensor is **not USB at all**: it is a Betterlife module on the Intel LPSS
SPI bus, enumerated by ACPI as `ACPI\BF63112` at `\_SB.PC00.SPI1.FPRT`
("FPRT" = fingerprint reader target). Linux boots, the SPI controller works,
and the sensor sits there with **no driver bound and no `spidev` node**, so
neither `fprintd` nor anything else can talk to it. Windows drives it with a
user-mode (UMDF2) WBDI driver that speaks a small framed command protocol over
SPI; the module is **match-in-sensor** (it stores and compares templates
itself, and it owns the matching algorithm).

The good news: the protocol is simple and we recovered its framing exactly
(start byte, length, CRC-32, command block, 16-byte alignment), the full
command table, and the image-transfer handshake including its checksum. The
bad news: nobody has written a driver, the vendor documents nothing publicly,
and the two settings a driver must have before it may touch the bus (SPI clock
and the reset/IRQ GPIO numbers) live in the ACPI `_CRS` that only root can
read on this host.

**Don't be misled by two decoys:**

* `0018:27C6:0133` / `GXTP6933:00` in the HID bus is the **Goodix touchpad**,
  not the fingerprint reader. Same vendor family, different device.
* The driver pack ships *two* fingerprint drivers. `other/ftwbioumdfdriverv2`
  is **FocalTech** and binds `ACPI\FTE3600` / `USB\VID_2808&PID_9338` - not
  our hardware. `10_FingerPrint/SignedDrivers_20250813_0939` is **Betterlife**
  and its INF matches `ACPI\BF63112`, `BF61100`, `BF63100`,
  `USB\VID_347D&PID_0302`, `PID_0304`. Our device is the Betterlife one.

## Device identity (measured on this unit)

| fact | value | source |
|---|---|---|
| ACPI device | `\_SB_.PC00.SPI1.FPRT`, HID **`BF63112`**, `_STA` = 15 | `/sys/bus/acpi/devices/BF63112:00/{path,hid,status}` |
| second node | `\_SB_.PC00.SPI2.FPRT`, HID `BF63112`, no controller instanced | `/sys/bus/acpi/devices/BF63112:01` |
| SPI device | `spi-BF63112:00`, modalias `acpi:BF63112:` | `/sys/bus/spi/devices/spi-BF63112:00/uevent` |
| SPI host | `pxa2xx-spi.6` -> `spi0`, CS0, PIO (no DMA channels) | journal: `pxa2xx-spi pxa2xx-spi.6: no DMA channels available, using PIO` |
| PCI function | `0000:00:1e.3` `[8086:e430]`, BAR0 4 KiB | journal + `/sys/devices/pci0000:00/0000:00:1e.3` |
| bound driver | **none** (no `driver` symlink, no `/dev/spidev*`) | `/sys/bus/spi/devices/spi-BF63112:00/` |
| Windows ID | `ACPI\BF63112` (SPI), `USB\VID_347D&PID_0302/0304` (USB variant) | `BetterlifeFingerprintDevice.inf` |
| module MCU | Nations **N32G4FR** (Cortex-M4), firmware in the driver's `.rsrc` | firmware strings + vector table |

`lsusb` has no fingerprint device at all (only the Riitek keyboard, the
`1a86:fe00` gamepad HID, and the Xbox pad), which is the first thing that
misleads: the reader is on SPI, so it never appears there.

## Existing Linux support: none

| piece | state on this host |
|---|---|
| `libfprint` | 1.94.100 installed; strings contain **zero** hits for `betterlife`, `347d`, `BF63112`; no Betterlife driver, no `347d` USB id |
| `fprintd` | 1.94.5, `static` enabled, inactive; `fprintd-enroll/list` exist, find no device |
| udev | `70-libfprint-2.rules` binds `spidev` only for `acpi:ELAN7001:` and `acpi:ELAN70A1:`; no rule mentions `BF63112` |
| kernel | no `bf63112` driver in tree; `spidev.ko` is available as a module but not loaded |
| `spidev` support in libfprint | **yes** - `drivers/elanspi.c`, `fpi-spi-transfer.c`, `/sys/module/spidev/parameters/bufsiz` handling |

So the plumbing for an SPI fingerprint driver exists upstream (Elan's
`elanspi` proves the architecture), but nothing matches this part.

## The Windows driver, and what it tells us

`BetterlifeFingerprintDevice.dll` 9.39.15.26 (2025-08-13, UMDF2, WBDI):
`BetterlifeFingerprintDevice.dll` + `Betterlife{Sensor,Engine,Storage}Adapter.dll`,
`UmdfDirectHardwareAccess=AllowDirectHardwareAccess`, exclusive access,
`SystemWakeEnabled`. It links only `ntdll`, `KERNEL32`, `ADVAPI32`,
`WppRecorderUM` - all WDF calls go through the version-bound function table, so
the SPI access is indirect and shows up as `(*guard_dispatch_icall)(...)`.

**Match-in-sensor.** The command names are the module's own vocabulary; the
host never sees a template:

| opcode | command | notes |
|---|---|---|
| `0xA1` | `CMD_QUERY_FIRMWARE_VER` | response carries project code + 4-byte version |
| `0xA2` | `CMD_QUERY_MODULE_SCHEME_INFORMATION` | |
| `0xB0` | `CMD_QUERY_ROOT_KEY` | chunked write path (Start/Ing/Over/Cancel) |
| `0xB1` | `CMD_ENROLL_FINGERPRINT` | start / press / fini / cancel |
| `0xB2` | `CMD_IDENTIFY_FINGERPRINT` | module matches, returns handle + score |
| `0xB3` | `CMD_REMOVE_FINGERPRINT` | |
| `0xB4` | `CMD_QUERY_FINGERPRINT_COUNT` | |
| `0xB5` | `CMD_GET_IMG_DATA` | only for diagnostics; 8-bit grayscale, w*h |
| `0xB6` | `CMD_DETECT_FINGERPRINT_PRESS` | |
| `0xC0` | `CMD_SLEEP` | |
| `0xC1` | `CMD_QUERY_MODULE_UID` | |
| `0xC2` | `CMD_QUERY_SENSOR_INFO` | returns width, height, flags |
| `0xC3` | `CMD_JTAG_CONTROL` | vendor only |
| `0xD0` | `CMD_RESTORE_MCU` | vendor only |
| `0xD1` | `CMD_UPDATE_FIRMWARE` | **never run this** |
| `0xFF` | `CMD_FW_WORK_MODE_SWITCH` | Boot vs App ("SM3" mode) |

The driver also carries a project table ("BOE MOC", "HUAHONG MOUSE MOC",
"DYW MOH", "NATION TEST", "NATIONALCHIP") and the hash names **SM3, MD5,
SHA-1** - the module's challenge/response and template protection are
crypto-heavy and undocumented. Treat template handling as out of scope for a
first driver: enrol storage can stay on the module, the host only needs the
handle.

### Frame format (recovered, byte-exact)

Host -> device, built by `FUN_180004358` + `FUN_1800028f8`:

```
off  len   field
0    1     0xAA                       start of frame
1    4     L, little endian           = 20 + align16(10 + data_len)
5    16    0x00 * 16                 reserved header (device answer area)
21   1     0x4C                       command magic 'L'
22   1     opcode                     from the table above
23   1     phase (0 start, 1 data, 2 finish, 3 cancel)
24   1     0x00
25   2     arg, u16 LE
27   4     data_len, u32 LE           (the payload length, repeated)
31   n     data
      pad   zeros to a 16-byte block boundary
end-4 4    CRC-32 (reflected 0xEDB88320, init 0xFFFFFFFF, final XOR)
```

`L` counts the bytes after `0xAA`: 4 length + 16 reserved + the aligned
command block. A datum-only query (`data_len = 0`) is therefore a 41-byte
frame with `L = 36`. The CRC covers everything before it, including `0xAA`.

Device -> host: the answer comes back in the same shape (the handlers receive
a body whose first byte is status/parameter, i.e. the 4-byte `4C op phase
status` block is stripped before the handler sees it). The 16 reserved bytes
are the device's answer header; their contents were not fully decoded.

### Image transfer (for reference; a normal driver does not need it)

`CMD_GET_IMG_DATA` runs in three phases: phase 0 returns 9 bytes
(`status`, `u32 total_bytes`, `u32 byte_sum`); phase 1 streams chunks that the
driver appends and counts (chunk counter `u16`); phase 2 cancels. Acceptance
checks are `total_bytes == width * height` and an **8-bit running sum** of the
image equal to the announced `byte_sum` - no per-chunk CRC. The driver requests
at most `0x1FCC` (8140) bytes per chunk and uses 1000/5000 ms per-attempt
timeouts (2000 ms for cancel) with 2030/8000/10010 ms totals.

### Timing / interrupts

After sending a frame the driver resets an event, sends, then
`WaitForSingleObject`s on it: the module signals "answer ready" on a **GPIO
interrupt**, and the driver re-sends the frame if the wait expires. A Linux
driver must therefore request that GPIO itself - `spi-intel`/`pxa2xx` will not
do it - which is exactly why the ACPI `_CRS` matters (see open points).

### Firmware in the driver package

`.rsrc` type `BIN` holds four blobs; `0x90` and `0x91` (242 116 / 242 124 B)
are N32G4FR images (`SP = 0x2001D538`, reset `0x0800A1AD` -> flash at
`0x08000000`), `0x95` is a different MCU (`reset 0x1001A405`), `0x8F` is 39 KiB
of something else. These are vendor firmware and are **not** copied into this
repo; nothing here writes firmware.

## Verdict

* **Supported today: no.** No kernel driver, no libfprint driver, no udev
  binding, no `spidev` node. Biometric login on this unit is not possible and
  no amount of configuration changes that.
* **Supportable: yes, with real work.** The transport is plain SPI, the frame
  format is known, the command set is known, and libfprint already has both the
  SPI transport and match-in-sensor driver archetypes (`fpi-spi-transfer.c`,
  `elanspi`, `egismoc`/`focaltech_moc`).
* **Blocked by, in order:**
  1. **No driver exists** and the vendor publishes nothing - a BF63112 driver
     has to be written from scratch (see `tools/fingerprint/libfprint-bf63112.c`).
  2. **SPI bus parameters unknown** - clock and mode are in the ACPI `_DSD`
     (or fixed by the vendor driver); wrong values return `0xFF` forever.
     Root required to read them.
  3. **Reset/interrupt GPIOs unknown** - they are `GpioIo`/`GpioInt` entries in
     `\_SB.PC00.SPI1.FPRT._CRS`; without an IRQ the driver can only poll.
  4. **Answer framing partly decoded** - enough to see status and payload for
     queries, not enough to guarantee the enrol/identify state machines.
  5. **Crypto/session layer** (SM3/root-key) undocumented - a first driver
     should avoid it and rely on module-side templates only.

Cheapest useful next step is not code: it is step 1 below, reading the ACPI
resources with root, because without the clock/GPIO numbers even a perfect
driver cannot start.

## Artifacts in this repo

| file | what it is |
|---|---|
| `tools/fingerprint/oxp3-fp-probe.py` | read-only userspace handshake: binds to `spidev`, builds the exact 41-byte query frame, sends the read-only opcodes (`fw`, `scheme`, `uid`, `info`, `count`), hexdumps the answer, refuses destructive opcodes. Stdlib only. |
| `tools/fingerprint/libfprint-bf63112.c` | **template only** (not built, not compiled): driver skeleton with the CRC/frame helpers, FpDevice glue, and the enrol/identify sequences documented in place |
| `docs/hw/fingerprint.md` | this document |

The probe is self-checked (`py_compile`, frame/CRC round-trip) but has **not**
run against the sensor: it needs root to bind `spidev`, which this session did
not have. Treat its first run as a bring-up experiment.

## Bringing it up by hand (privileged steps, exact commands)

Nothing below is executed by this repo; they are the commands to run yourself.
Steps 1-2 are read-only. Step 3 is the only state change and it is reversible.

1. **Read the ACPI resources** (the numbers a driver needs):

   ```bash
   sudo cat /sys/firmware/acpi/tables/DSDT > /tmp/dsdt.aml
   sudo dnf install acpica-tools          # for iasl
   iasl -d /tmp/dsdt.aml
   grep -n -A 40 'FPRT' /tmp/dsdt.dsl | less
   ```

   Look for `SpiSerialBus (... , SpiMode, SpiBitWidth, ConnectionSpeed ...)`
   and the `GpioInt`/`GpioIo` entries with their `PinIndex`/`GpioIo` numbers.
   `ACPI\BF63112:00` may also carry a `_DSD` with `spi-max-frequency`.

2. **Confirm the controller sees the device** (already true here):

   ```bash
   ls /sys/bus/spi/devices/                 # -> spi-BF63112:00
   cat /sys/bus/spi/devices/spi-BF63112:00/uevent
   ```

3. **Bind `spidev` so userspace can reach it:**

   ```bash
   sudo modprobe spidev
   echo spidev | sudo tee /sys/bus/spi/devices/spi-BF63112:00/driver_override
   echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/bind
   ls -l /dev/spidev0.0
   ```

4. **Run the probe** (read-only queries; add `--speed`/`--mode` once step 1
   told you the real values, and `--raw-info` to see bytes):

   ```bash
   sudo tools/fingerprint/oxp3-fp-probe.py info
   sudo tools/fingerprint/oxp3-fp-probe.py fw scheme uid count
   ```

   A healthy answer starts with `0xAA` and shows sensor width x height. All
   `0xFF`/no answer almost always means wrong SPI mode or too high a clock.

## Safety

* **No firmware writes, no JTAG, no `RESTORE_MCU`.** `0xD1`/`0xC3`/`0xD0` brick
  the module if misused; the probe refuses them by name.
* **No template deletion.** `0xB3`/`0xB1` are not wired up; fingers enrolled in
  Windows stay untouched.
* **SPI is low risk by nature**: the frame has a CRC, wrong parameters produce
  garbage reads rather than damage, and the module is powered by the platform
  (no relay/destructive side effects).
* **Do not run the vendor's Windows firmware update** against this module from
  Linux under any wrapper.
* Binding `spidev` takes the device from any future in-tree driver; do it on a
  machine you are experimenting on, not one you depend on for login. It does
  not affect the touchpad, keyboard, display or audio.

## Rollback

```bash
echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/unbind
sudo rm -f /sys/bus/spi/devices/spi-BF63112:00/driver_override
sudo modprobe -r spidev
```

Nothing else on the host is modified: no packages, no config files, no
firmware, no boot parameters. Deleting `tools/fingerprint/` returns the repo to
its previous state.

## Open points

1. **SPI clock, mode, bit width and the reset/IRQ GPIOs** - in
   `\_SB.PC00.SPI1.FPRT._CRS`/`_DSD`; root-only on this host, so not read here.
   Without them the probe can only guess (defaults used: mode 0, 1 MHz).
2. **Device -> host answer framing** - the 16-byte header the host sends as
   zeros is where the module answers; its field layout is only partly decoded.
   One `--raw-info` run on real hardware would settle it.
3. **Enrol/identify state machines** - the opcodes and phases are known, the
   per-phase payloads (template handles, scores, timeouts, the `SM3` mode
   switch) are not. This is the bulk of a real driver.
4. **Session/crypto layer** (`CMD_QUERY_ROOT_KEY`, SM3/MD5/SHA-1): unknown
   whether a plain-spoken driver is accepted at all or whether the module
   insists on a key exchange first. Sending `0xB0` read-only would answer that.
5. **`BF63112:01` on SPI2** - present in ACPI, no controller instanced. Either
   an unpopulated second source or a phantom node; harmless, but a driver
   should match `BF63112:00`-style enumeration rather than assume one device.
6. **The probe has never run** - written and self-checked here, first execution
   is the bring-up itself; expect to tune `--speed`/`--mode`/`--wait-ms`.
