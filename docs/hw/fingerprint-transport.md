# BF63112 transport: USB vs SPI/ACPI, and what each path needs

Status: **answered for this unit - the sensor is SPI-only here.** The Windows
driver does have a full USB transport, in the same DLL, selected at runtime by
the device's hardware ID. It is used on the SKU whose module is wired to USB;
it is not reachable on our board. This document records the reverse-engineering
evidence, the per-SKU verdict, and the exact next steps for both paths.
Owner: silentsudin. Host: Anatase 20261026.3, kernel 7.2.7-an28.fc45.
Companion: [`docs/hw/fingerprint.md`](fingerprint.md) (protocol, command set,
session layer). Method: isolated headless Ghidra through
`tools/ghidra-pod.sh fpusb ...`, plus host sysfs / ACPI inspection. Nothing here
was executed against the sensor, and no vendor binary is committed.

## Verdict

* **Same hardware, two transports, one binary.** The UMDF driver decides at
  `EvtDeviceAdd` by testing whether its own hardware ID starts with `ACPI\`:
  `ACPI\*` -> the SPI/SPB implementation, `USB\VID_347D&PID_0302/0304` -> the
  USB implementation. There is no second driver, no firmware switch read, no
  strap check in software.
* **Our unit is the ACPI one.** `\_SB.PC00.SPI1.FPRT` is the only fingerprint
  node; 347D does not enumerate on any bus; every USB interface on the machine
  is claimed. A "not on USB on this unit" verdict is therefore firm, see
  [Measurements](#6-measurements-on-this-unit).
* **The USB variant would be easy on Linux.** Vendor class 0xFF, one interface,
  two bulk endpoints (IN `0x81`, OUT `0x02`), 64 B full-speed / 512 B
  high-speed packets, 100 mA (480 mA on the HS image). libusb can claim it with
  a udev `uaccess` rule; no kernel driver, no WinUSB equivalent needed.
* **But the protocol recovered so far is the USB one.** The `0xAA` / CRC-32
  framing in `docs/hw/fingerprint.md` is reachable only from the USB branch
  (see [Corrections](#5-corrections-to-fingerprintmd)). The ACPI branch has its
  own transfer path. This is the single most important open item for the SPI
  work and is called out in [Next steps](#8-next-steps).

## 1. How the driver picks a transport

Two functions, both with no static callers (they are WDF callbacks registered
from `EvtDeviceAdd`):

```
FUN_180005a2c   EvtDeviceAdd
    ...  WdfDeviceCreate / device-context init, ctx+0x14 = 0 (default = USB)
    hwid = <device property: hardware ID string>
    iVar4 = _wcsnicmp(hwid, L"ACPI\\", 5);
    if (iVar4 == 0)  ctx+0x14 = 1;          // ACPI hardware ID -> SPI/SPB
```

```
FUN_180006a60   EvtDevicePrepareHardware(Device, ResourcesRaw, ResourcesTranslated)
    ctx = WdfDeviceGetDeviceContext(Device)
    if (ctx+0x14 == 0)  return FUN_180019d90(Device);              // USB
    else                return FUN_180015acc(Device, Raw, Trans);  // SPI/SPB
```

`FUN_180006a60` takes exactly the three parameters of
`EvtDevicePrepareHardware` and hands the *resource lists* to the ACPI branch -
which is what makes the assignment unambiguous. The mirror functions on close
are `FUN_180006b50` -> `FUN_18001aa5c` (USB) / `FUN_180016040` (SPB).

Consequences:

* the transport is a property of **how the module is wired and what the
  firmware's ACPI/INF exposes**, not of the driver version;
* Windows on a USB-wired unit binds `USB\VID_347D&PID_0302` (WinUSB) and the
  same DLL takes the USB branch - so the USB transport is not dead code;
* **the module's MCU firmware implements USB on every build shipped in this
  package** (see [§3](#3-usb-path-what-is-there-and-what-a-linux-driver-needs)):
  all four embedded firmware images carry a `347D` device descriptor.

## 2. The USB branch (`FUN_180019d90`, `EvtDevicePrepareHardware` path)

The driver talks to the device through the generic WDF USB API: every call is
an indirect call through the version-bound WDF function table (the DLL statically
links `WdfDriverEntry.lib` - export `FxDriverEntryUm`; imports are only
`ntdll/KERNEL32/ADVAPI32/WppRecorderUM`). Table slot offsets are therefore the
only static handle on which API is called; the identification below rests on
slot + argument shape + the struct the driver fills in. Reproduce with
`objdump -d --start-address=0x180019d90 --stop-address=0x18001a560`.

| slot | args observed | call | evidence |
|---|---|---|---|
| `+0x3d8` | (obj, type-descriptor) | `WdfObjectGetTypedContextWorker` | used 64x for context fetch |
| `+0x650` | (Device, NULL, &ctx+0x108) | `WdfUsbTargetDeviceCreate` | new handle stored at `ctx+0x108` |
| `+0x668` | (usbdev, &descriptor) | `WdfUsbTargetDeviceGetDeviceDescriptor` | then logs u16 at desc+8, u16 at desc+10 = **idVendor/idProduct** |
| `+0x690` | (usbdev) -> byte | `WdfUsbTargetDeviceGetNumInterfaces` | |
| `+0x698` | (usbdev, NULL, &params) | `WdfUsbTargetDeviceSelectConfig` | params = {Size `0x20`, Type **3** = `MultiInterface`}; the Type is then logged through the enum-name helper `FUN_1800057f8` |
| `+0x760` | (usbdev, 0) | `WdfUsbTargetDeviceGetInterface` | NULL -> `0xC0000001` |
| `+0x738` | (iface, 0) -> byte | `WdfUsbInterfaceGetNumEndpoints` | logged |
| `+0x770` | (iface) -> byte | `WdfUsbInterfaceGetNumConfiguredPipes` | loop bound |
| `+0x778` | (iface, index, &pipeinfo) | `WdfUsbInterfaceGetConfiguredPipe` | `pipeinfo.Size = 0x14`; logs maxpacket, endpoint address, pipe type |
| `+0x708` | (pipe, &config) | `WdfUsbTargetPipeConfigContinuousReader` | config `{Size 0x48, TransferLength 0x2000, ... EvtReadComplete = 0x18001AC60}` on **ctx+0x110** |
| `+0x6e8` | (pipe, 0, &opts, &memdesc, &written) | `WdfUsbTargetPipeWriteSynchronously` | 200 ms send timeout, see below |

Flow, in words:

1. create the USB target on the device (`ctx+0x108`);
2. read the device descriptor and log VID/PID (this is why the VID is not a
   constant in the driver - it is whatever the device reports);
3. select configuration type **MultiInterface**, i.e. let the driver enumerate
   interfaces itself rather than accept a single default interface;
4. take interface 0, count endpoints and configured pipes, and for each
   configured pipe create a `WDF_USB_PIPE_INFORMATION`; the first pipe accepted
   by one predicate goes to `ctx+0x110`, the second to `ctx+0x118`;
5. configure a **continuous reader on `ctx+0x110`** with 8 KiB transfers whose
   completion routine is `FUN_18001AC60` - the same `0xAA`-frame reassembler and
   answer dispatcher described in `fingerprint.md`;
6. the frame writer `FUN_18001B6E4` builds a `WDF_MEMORY_DESCRIPTOR` (Type =
   Buffer, the freshly built frame) plus a `WDF_REQUEST_SEND_OPTIONS`
   (`Size 0x10`, `Flags 1` = timeout, `Timeout = -2000000` = **200 ms**) and
   writes it to `ctx+0x118` with the 5-argument synchronous pipe write.

`ctx+0x110` carries the reader, `ctx+0x118` the writer, so the pair is the
in/out pipe pair. The driver never hard-codes an endpoint address or packet
size: it takes them from the descriptor, which is consistent with the firmware
below and means a Linux driver should do the same (read descriptors, pick the
bulk IN/OUT pair) rather than assume.

### Wire format on USB (this is the documented protocol)

`FUN_180004358` builds the frame (`0xAA`, length, 16-byte header, optional SM4
transform, CRC-32 from the table at `0x180046030`), calls `FUN_18001B6E4` for
the single transfer, then `WaitForSingleObject(ctx+0x188, per-attempt-ms)` and
re-sends until the total deadline. `FUN_18001AC60` reassembles answers with the
second CRC table (`0x180047120`) and dispatches to the handler table at
`0x46F00`. That is exactly the protocol in `fingerprint.md`.

## 3. USB path: what is there, and what a Linux driver needs

### Descriptors encoded in the package

The DLL's `.rsrc` carries four MCU firmware images (RCDATA, 39.5 kB / 242 kB /
242 kB / 222 kB). **All four contain a complete USB device/configuration
descriptor set with VID `0x347D`** - the module's own USB stack (the same
firmware blob also contains `usb_control_transfer`, `usbFifoAppend`, `USBC`,
`IS_RCC_USBCLK_SRC`). Decoded from image #2 (identical in #3, file offset
`0x844d4` / `0xbf69c`):

```
12 01 00 02 00 00 00 40  7d 34  02 03  00 02  01 02 03 01
   bLength 18, DEVICE, bcdUSB 2.00, class/subclass/protocol 0/0/0,
   bMaxPacketSize0 64, idVendor 0x347D, idProduct 0x0302, bcdDevice 2.00,
   iManufacturer 1, iProduct 2, iSerialNumber 3, bNumConfigurations 1
09 02 20 00 01 01 00 A0 32
   CONFIG, wTotalLength 32, 1 interface, bmAttributes 0xA0, bMaxPower 100 mA
09 04 00 00 02 FF 00 00 00
   INTERFACE 0 alt 0, 2 endpoints, class 0xFF (vendor specific)
07 05 81 02 40 00 20       EP 0x81 IN,  BULK, wMaxPacketSize 64, bInterval 32
07 05 02 02 40 00 20       EP 0x02 OUT, BULK, wMaxPacketSize 64, bInterval 32
```

Image #4 (file offset `0xf81fe`) is the high-speed variant of the same
`347D:0302`: same interface, bulk `0x81`/`0x02`, but `wMaxPacketSize 0x0200`
(512 B) and `bmAttributes 0xE0` / `bMaxPower 0xF0` (480 mA). Image #1
(offset `0x521d0`) is `347D:0304` with its own string descriptors (`"Blestech"`,
`"Betterlife Finger..."`). So the second PID is a second product/firmware
flavour, not a different transport.

Practical reading: **bulk, not interrupt**; one vendor-class interface; the
outbound endpoint is `0x02` and the inbound `0x81`, but a Linux driver should
still parse the descriptor instead of trusting these numbers.

### INF: the USB path is WinUSB, not a vendor driver

`SignedDrivers_20250813_0939/X64/BetterlifeFingerprintDevice.inf`
(sha256 `323d04d8...d8c0`), USB install section only:

```
[UsbDevice_Install.NT]         Include=WINUSB.INF / Needs=WINUSB.NT
[UsbDevice_Install.NT.hw]      BtlWinUsbDevice_AddReg:
                                 LowerFilters        = REG_MULTI_SZ "WinUsb"
                                 WinUsbPowerPolicyOwnershipDisabled = 1
[UsbDevice_Install.NT.Services] AddService=WUDFRd (0x1fa), AddService=WinUsb (0x1f8)
[UsbDevice_Install.NT.Wdf]     UmdfDispatcher=WinUsb, UmdfService=BetterlifeFingerprintDevice
```

So: `WUDFRd` + `WinUsb` as a lower filter, and the UMDF driver is handed a
WinUSB handle (`UmdfDispatcher=WinUsb`) which is what makes
`WdfUsbTargetDeviceCreate` legal. **There is no USB device driver of the
vendor's own**; on Linux the equivalent is plain usbfs/libusb on a vendor-class
interface.

Power and exclusivity keys (identical for both install sections, from
`BetterlifeFingerprintDevice_AddReg`):

| value | meaning |
|---|---|
| `Exclusive=1` | single open, no sharing |
| `SystemWakeEnabled=1` | device may wake the system |
| `DeviceIdleEnabled=1`, `UserSetDeviceIdleEnabled=1`, `DefaultIdleState=1`, `DefaultIdleTimeout=5000` | WDF power-idle: D3 after **5 s** idle |
| `DeviceCharacteristics=0x100` | `FILE_DEVICE_SECURE_OPEN` |
| `Security=D:P(A;;GA;;;BA)(A;;GA;;;SY)` | admin + SYSTEM only |

There is no `SelectiveSuspendEnabled` and no `EnhancedPowerManagementEnabled`:
`WinUsbPowerPolicyOwnershipDisabled=1` keeps power policy with the UMDF driver,
so the idle behaviour is WDF's (5 s to D3, system wake armed). Both sections
also write the WinBio plugin/database keys (`EngineAdapterBinary`,
`SensorAdapterBinary`, `StorageAdapterBinary`, database
`{FD2B9BF2-...}`, `BiometricType=0x8`); irrelevant to Linux.

### A Linux USB driver would need

* **Transport**: libusb (or a tiny usbfs wrapper) on the vendor interface,
  claim interface 0, find the bulk IN/OUT pair from the descriptor, and reuse
  the userspace protocol core (`tools/fingerprint/fpbf63112/`, `bf63112_proto.c`)
  unchanged - framing, CRC and the command layer are transport-agnostic.
* **Permissions**: udev rule matching `idVendor=347d` with
  `TAG+="uaccess"` (or a `plugdev`-style group); the device is exclusive-open in
  Windows, so a single owner is fine.
* **Power**: bus powered at 100 mA (FS image) / 480 mA (HS image) - within a
  normal port budget; no explicit reset line is handled by the USB branch (the
  resource-hub GPIO code is on the SPB side only).
* **Risks**:
  1. *Wire format*: same open question as SPI - enrol/identify may require the
     SM2/SM4 session (the driver sets the secure-frame flag there) and the
     firmware may refuse plaintext;
  2. *No hardware to test on*: no 347D unit exists here, so a USB backend would
     be untestable on this machine;
  3. *Firmware mode*: the descriptors exist in every image, but whether a given
     board brings up its USB pins is a board question - the ACPI/INF split is
     the only evidence we have of which wiring a unit shipped with.

## 4. The ACPI/SPB branch (`FUN_180015acc`, the path our unit uses)

This is a separate implementation, not a port of the USB one:

1. **Resources.** It walks the translated ACPI resource list (the
   `EvtDevicePrepareHardware` argument) and accepts exactly two kinds of
   descriptor:
   * `CM_PARTIAL_RESOURCE_DESCRIPTOR.Type == 2` (interrupt) - remembered, count
     stored at `ctx+0x18`;
   * `Type == 0x84`, with byte `+4 == 2` (**serial bus type SPI**; `1` = I2C is
     handled into a second slot for another module) and byte `+5 == 2`
     (general flags). Two 32-bit words at `+8` and `+0xC` are copied to
     `ctx+0x20` / `ctx+0x24` - the ACPI SPI descriptor payload, i.e.
     **data-bit-length / phase / polarity / device-selection** and
     **connection speed**.
2. **Interrupt.** `FUN_18001402c` builds a 0x60-byte `WDF_INTERRUPT_CONFIG`
   with five callbacks and calls `WdfInterruptCreate(Device, &cfg, NULL,
   ctx+0x08)`. The field at config offset `+8` is `2` in the normal path but
   forced to `0` when `ctx+0x14 == 1` (SPB); the exact field name was not
   resolved, but a WDF interrupt is DIRQL unless `PassiveHandling` is set, so
   the SPI IRQ is serviced at DIRQL.
3. **Queue.** `FUN_180016450` (an `EvtIo*` callback) is registered with a
   0x68-byte `WDF_IO_QUEUE_CONFIG`; the queue handle lands at `ctx+0x28`.
4. **Reset GPIO.** `FUN_180011550` formats `\\.\RESOURCE_HUB\%0*I64x` (a 64-bit
   resource-hub id) and opens it with `DesiredAccess=0xC0000000`,
   `ShareAccess=0`, `CreateDisposition=FILE_OPEN`; success sets `ctx+0x60=1`
   and stores the target at `ctx+0x38`. The caller then `Sleep(0x32)` = **50 ms**
   (`FUN_180016260`). `FUN_1800112f4` closes it. **This is the Windows
   equivalent of a reset line, and it exists only on the ACPI path.**
   Where the 64-bit id comes from (a device property / `_DSD`) is not yet
   recovered - flagged as an open point.
5. **Command transfer.** `FUN_180017020` is the per-command transport:
   ensure reset target (`FUN_180016260`), `ResetEvent(ctx+0x48->event+0x18)`,
   write a **one-byte selector `(x & 0x3f) << 1`** followed by the payload via
   `FUN_18001197C`, then `WaitForSingleObject(event, timeout)` where the
   timeout is the per-command value from the command descriptor
   (**1000/2030 ms** for ordinary queries, **5000/10010 ms** for GET_IMG,
   **2000/4016 ms** for the vendor restore). A timeout maps to `0x102`.
   `FUN_18001197C` builds a two-entry SPB transfer list (write header, write
   payload) and issues it through the target at `ctx+0x20`/`ctx+0x28`.
6. **Cancel / recover.** `FUN_180013b60` (only when `ctx+0x14 == 1`): resets the
   request events, sets the completion event at `ctx+0x78`, and waits
   **150 ms** (`Sleep(0x96)` or `WaitForSingleObject(..., 0x96)`) before
   completing the pending request with `0xC0000120` (`STATUS_CANCELLED`).
7. **Registry knobs.** `EvtDeviceAdd` reads the driver's own
   `settings\ConnectInterrupt` and `settings\AdapterInfo` values (the only two
   the INF writes). `ConnectInterrupt=1` (the INF default) is what enables the
   interrupt-driven path; with 0 the driver falls back to polling
   (`FUN_18001B594` polls `ctx+0x188` with `WaitForSingleObject(..., 0)`).

### What the SPI driver needs, precisely

* **SPI mode/clock/word size are *not* in the driver package** - confirmed
  again: the INF and DLL carry no clock, mode, bit width or GPIO number; the
  SPB branch takes data-bit-length, phase, polarity, device-selection and
  connection-speed from the ACPI `_CRS` serial-bus descriptor. A Linux driver
  must read `\_SB.PC00.SPI1.FPRT._CRS` (root) and program the controller with
  those values.
* **Interrupt**: the `_CRS` `GpioInt`/IRQ entry; the ACPI branch requires one
  (it refuses to proceed without it) and services it at DIRQL - on Linux this
  is a normal `request_threaded_irq`/`spi_irq` on the SPI slave device.
* **Reset**: the ACPI path drives a reset line through the Windows resource
  hub. On Linux the equivalent is a `GpioIo` in `_CRS` (or a `reset-gpios`
  property) toggled from the driver; if the platform exposes none, the module
  must come up already out of reset.
* **Wire format**: *open* - see next section.
* **Timings**: 200 ms per transfer / 1000-2030 ms per ordinary command /
  5000-10010 ms for GET_IMG / 2000-4016 ms for restore / 150 ms cancel settle /
  50 ms after reset / 5 s device idle.

## 5. Corrections to fingerprint.md

The "Transport (proven)" table in `docs/hw/fingerprint.md` was derived from
`FUN_180004358` / `FUN_18001B6E4` and labelled as the SPB write. It is the
**USB** write:

* `FUN_18001B6E4` passes a `WDF_MEMORY_DESCRIPTOR` and a
  `WDF_REQUEST_SEND_OPTIONS` (200 ms) to the target at `ctx+0x118` in the exact
  five-argument shape of `WdfUsbTargetPipeWriteSynchronously`; `ctx+0x118` is
  written **only** by the USB initialiser (as part of the in/out pipe pair).
* `FUN_180004358` (frame build + CRC table `0x180046030` + retry) is called
  only from the `FUN_1800012xx`-`FUN_180001fxx` command family, which is called
  only from the USB session open (`FUN_180019764`), which is only reachable via
  `EvtDevicePrepareHardware` when the hardware ID is **not** `ACPI\`.
* The frame builder `FUN_1800028F8` has exactly one caller
  (`FUN_180004358`); the answer reassembler `FUN_18001AC60` is the USB
  continuous-reader completion routine; the second CRC table `0x180047120` is
  referenced only there.
* The ACPI branch (`FUN_18000dxx`-`FUN_180017xx`) references **neither CRC
  table** and never calls `FUN_1800028F8`.

So on this unit the transport is SPI, but the wire format on that SPI link is
**not yet proven** and is probably not the `0xAA` frame: the SPB transfer sends
a one-byte selector `(x & 0x3f) << 1` plus a payload and waits for an IRQ.
Concrete consequence for the existing work: `tools/fingerprint/oxp3-fp-probe.py`
and `libfprint-bf63112.c` currently encode the USB framing as the SPI wire
format. Do not spend root-time on a sensor that answers nothing until this is
resolved - either the payload really is the `0xAA` frame behind that selector
(then nothing changes), or the SPI protocol is a different, smaller one (then
the framed protocol is still useful, but only for USB SKUs).

## 6. Measurements on this unit

```
$ lsusb -d 347d:                     # (no output, exit 1)
$ lsusb -t
/:  Bus 001 ... xhci_hcd/1p  480M      root hub only
/:  Bus 002 ... xhci_hcd/4p  20000M    root hub only
/:  Bus 003 ... xhci_hcd/10p 480M      Port 4 keyboard (usbhid x2)
                                       Port 5 1a86:fe00 gamepad HID (x3)
                                       Port 7 045e:028e Xbox pad (xpad)
/:  Bus 004 ... xhci_hcd/2p  20000M    root hub only
```

* Four devices total besides root hubs, all with every interface bound; the
  "interfaces without a driver" scan over `/sys/bus/usb/devices/*:*` returned
  none. There is **no unused/unclaimed interface** anywhere, so the "composite
  device with a spare interface" idea is ruled out.
* Bus 3 has 10 ports and uses 3; a USB-wired fingerprint module would have
  enumerated into one of the seven free ports. It did not.
* `grep -ril 347d /sys/bus/usb/devices/` - nothing. `usb-devices` - no
  vendor-class device with two bulk endpoints; the only vendor class device is
  the Xbox pad (class `ff/5d/01`, two **interrupt** endpoints), which is not it.
* ACPI view (the only fingerprint node on the machine):

```
/sys/bus/acpi/devices/BF63112:00/path    \_SB_.PC00.SPI1.FPRT
/sys/bus/acpi/devices/BF63112:00/hid     BF63112
/sys/bus/acpi/devices/BF63112:00/status  15
.../physical_node -> .../0000:00:1e.3/pxa2xx-spi.6/spi_master/spi0/spi-BF63112:00
/sys/bus/spi/devices/spi-BF63112:00/uevent   MODALIAS=acpi:BF63112:
```

  i.e. the ACPI device has a `physical_node` link **into the pxa2xx SPI
  controller** and no USB companion device; `BF63112:01`
  (`\_SB_.PC00.SPI2.FPRT`) is the second (unused) instance. Kernel log shows
  `pxa2xx-spi pxa2xx-spi.6: no DMA channels available, using PIO` and no
  fingerprint driver binding. `_CRS` itself is root-only
  (`/sys/firmware/acpi/tables/DSDT` is `0400 root`), so the SPI parameters still
  need a privileged read - unchanged from `fingerprint.md`.

## 7. Per-SKU verdict

| unit / SKU | what Windows binds | transport used by this DLL | Linux path |
|---|---|---|---|
| **ours**: `ACPI\BF63112` on `\_SB.PC00.SPI1.FPRT` | `SpbDevice_Install` (WUDFRd only) | **SPI/SPB** (`FUN_180015acc`) | new SPI driver + ACPI resources; USB not available |
| `USB\VID_347D&PID_0302` | `UsbDevice_Install` (WUDFRd + WinUSB) | **USB** (`FUN_180019d90`) | libusb / libfprint USB backend, same protocol core |
| `USB\VID_347D&PID_0304` | ditto | ditto | ditto (second firmware flavour) |
| `ACPI\BF61100`, `ACPI\BF63100` | `SpbDevice_Install` | SPI/SPB | as ours |

The same DLL covers all of them; nothing in the package distinguishes firmware
or strap beyond the hardware-ID prefix, and all four embedded MCU images carry
USB descriptors. The USB variant is a wiring/product decision, not something a
driver update can switch on for our board.

## 8. Next steps

**If the goal is this unit (SPI-only, the realistic path):**

1. Privileged read of `\_SB.PC00.SPI1.FPRT._CRS`: `acpidump -b` + `iasl -d`
   (or `cat /sys/firmware/acpi/tables/DSDT` with root), extract
   `SpiSerialBus` (device-selection, connection-speed, data-bit-length, phase,
   polarity) and `GpioInt`/`GpioIo`. This is still the first blocker.
2. Decide the wire format question (see §5) *before* writing a driver: the
   cheapest experiment is a `spidev`-based probe that sends the documented
   `0xAA` query frame **and** the `(opcode & 0x3f) << 1` + payload form recovered
   here, on a read-only opcode (`0xA0` firmware version), and compares.
3. Then the libfprint SPI driver (`tools/fingerprint/libfprint-bf63112.c`) with
   an IRQ-driven wait instead of polling; keep the 1000/2030 ms timeouts.
4. Optionally mirror the reset behaviour only if `_CRS` really exposes a GPIO -
   Windows itself drives it through the resource hub, so the line exists on the
   ACPI side of some units.

**If a USB unit ever shows up (or for completeness):**

1. `tools/fingerprint/usb_probe.py` - enumerates 347D, prints interfaces,
   endpoints, driver claim; sends **no** vendor command. Run it first on any
   new unit; a 347D hit flips the whole plan to libusb.
2. If it hits: claim interface 0, take bulk `0x81`/`0x02`, feed the existing
   frame library through a `bulk_transfer` transport, add a udev rule
   (`idVendor=347d`, `TAG+="uaccess"`). Expect the same secure-frame question
   for enrol.

## 9. Provenance

| item | value |
|---|---|
| `BetterlifeFingerprintDevice.dll` (X64) | sha256 `826177bfe1cdc2058842dde6a0dd3ee2c89fd2d5d183c3a7d87ac7eaded0d93d`, 9.39.15.26, 2025-08-13 |
| `BetterlifeFingerprintDevice.inf` | sha256 `323d04d84fe37b7d3acda6c32c18840085f8205643e43ae833a07bdf3e87d8c0` |
| `betterlifefingerprintdevice.cat` | sha256 `9081e8c7f7bec9452627ab106205a76ad4f9e84dcbc82ae78e48ff192a38158d` (DER PKCS#7, catalogues both hardware-ID sets) |
| decompiler dump | `tools/ghidra-pod.sh fpusb <dll>` -> `~/.cache/ghidra-agents/fpusb/out/decomp.txt` (805 functions) |
| key functions | `FUN_180005a2c` EvtDeviceAdd / `FUN_180006a60` PrepareHardware / `FUN_180019d90` USB init / `FUN_180015acc` SPB init / `FUN_18001B6E4` USB frame write / `FUN_180004358` frame+CRC+retry / `FUN_18001AC60` answer reassembly / `FUN_180011550` resource-hub GPIO / `FUN_180017020`+`FUN_18001197C` SPB transfer / `FUN_180013b60` SPB cancel |
| firmware descriptors | DLL file offsets `0x521d0` (0304), `0x844d4` and `0xbf69c` (0302 FS), `0xf81fe` (0302 HS) |

No vendor binary, decompiled listing or vendor string beyond short quoted
identifiers is stored in the repository.
