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
* **Wire format**: the selector form is implemented in `fpbf63112/spi.py` (§8) and in the C/libfprint driver (`bf63112_spi.c`, §8.5) but is still unproven on hardware; see §9.
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

## 8. Implemented transport and probe (2026-11-09)

Status: **code and offline tests only; this has still never run against the
sensor.** The selector framing and the two-entry transfer shape are recovered
from the driver; whether the module answers that shape is the experiment in
§9. What exists now:

### 8.1 `fpbf63112/spi.py`

* **Selection.** `selector(opcode, write)` is `(opcode & 0x3f) << 1`, with bit
  7 set for a host write. Because only six opcode bits reach the wire, the
  implementation refuses destructive operations by *selector* as well as by
  opcode: `0x02` aliases JTAG `0xC2` (selector `0x04`), `0x03` aliases restore
  `0xC3` (selector `0x06`), and so on. `FORBIDDEN_SELECTORS` lists every such
  alias.
* **Transaction.** One plan is two `struct spi_ioc_transfer` entries submitted
  as a single `SPI_IOC_MESSAGE(2)` ioctl (chip select stays asserted): write
  the selector byte, then either a receive-only clock of `read_len` bytes
  (read) or the payload (write). There is no length field and no status byte
  on the wire. `LinuxSpiIO` implements the ABI with `os.open`/`fcntl.ioctl`
  only, so no `python3-spidev` package is needed.
* **Timeouts.** The descriptor values are used: 1000 ms per attempt /
  2030 ms total for ordinary queries, 5000/10010 for GET_IMG (`0xB4`),
  2000/4016 for the vendor restore (`0xC3`), 150 ms cancel settle and 50 ms
  after a reset. A blocking spidev ioctl cannot be preempted from userspace;
  the deadline is checked before, after and around the IRQ wait, and a kernel
  hang is explicitly out of scope.
* **IRQ.** `GpioIrqWaiter` tries libgpiod v1/v2, then the GPIO v2 character
  device (`chardev`, §8.6), then sysfs, then a timed poll. It takes the
  *logical* edge (`rising` = assertion after `active_low`), so ACPI `GpioInt`
  polarity maps to `active_low`, not to a different edge name. The sysfs
  backend writes `export`/`direction`/`edge` (and `active_low` when
  available), tracks the level across the wakeup to derive rising/falling, and
  unexports a line it exported. The poll fallback warns and reports
  `observed=False`; `capture_wait` treats that as a timeout instead of a
  successful interrupt. Level-triggered `GpioInt` cannot be expressed by the
  edge API; the probe requests both edges and documents the limitation.
* **Reset.** `GpioResetLine` drives logical 1 (assert) for 5 ms, logical 0 for
  10 ms, then `SpiTransport.open` sleeps 50 ms. It is only pulsed with
  `allow_reset=True`; the polarity is applied by gpiod flags, the sysfs
  `active_low` attribute, or in software by the chardev backend (which writes
  the inverted physical level and never requests `ACTIVE_LOW`).
* **Safety gate.** Every transfer needs `allow_writes=True`; for this probe
  that flag is the explicit opt-in. Read opcodes must also be on the explicit
  allowlist (`SAFE_READ_OPS`: `0xA0`, `0xA1`, `0xB3`, `0xC0`, `0xC1` and the
  recovered status register `0x2D`). No write opcode is allowlisted. Enrol
  (`0xB0`), identify (`0xB1`), remove (`0xB2`), JTAG (`0xC2`), restore
  (`0xC3`), firmware update (`0xD0`) and the mode switch are refused
  unconditionally. Construction performs no I/O, and opening also refuses an
  unset device path rather than guessing one.

### 8.2 `fpbf63112/acpi.py`

Parses a raw `_CRS` resource template and decodes only what the transport
needs: `SpiSerialBus` (speed, data bit length, phase, polarity, device
selection, resource source, SPI type flags), `GpioInt`
(triggering/polarity/pins/shareable/wake), `GpioIo` (pins and IO restriction;
the restriction is bits 5-6, not 0-1), I2C serial bus, small/extended IRQ, and
opaque items so a template with extra resources still parses. It validates
lengths and offsets and raises `TruncatedResource` for malformed or truncated
buffers. `spi_connection(template)` reduces it to the exact values a driver
needs and derives the Linux SPI mode (`(CPOL << 1) | CPHA`) and the logical IRQ
edge. It does not parse `_DSD` properties.

### 8.3 Exact CLI

All of these are offline except the ones with `--spidev`:

```sh
python3 tools/fingerprint/oxp3-fp-probe.py --help
python3 tools/fingerprint/oxp3-fp-probe.py --dry-run --spidev /dev/spidev0.0 fw info
python3 tools/fingerprint/oxp3-fp-probe.py --dry-run --crs /tmp/fprt-crs.bin \
    --spidev /dev/spidev0.0 fw info uid count
python3 tools/fingerprint/oxp3-fp-probe.py acpi_info --crs /tmp/fprt-crs.bin
# the matrix (dry run first, then the real one; --allow-reset is mandatory)
python3 tools/fingerprint/oxp3-fp-probe.py --try-matrix --dry-run \
    --spidev /dev/spidev0.0 --gpio 18 --gpio-chip gpiochip1 \
    --reset-gpio 16 --allow-reset --allow-writes
sudo python3 tools/fingerprint/oxp3-fp-probe.py --try-matrix \
    --spidev /dev/spidev0.0 --gpio 18 --gpio-chip gpiochip1 \
    --reset-gpio 16 --allow-reset --allow-writes \
    --matrix-json /tmp/oxp3-fp-matrix.json
```

A real transfer requires **both** an explicit device path and `--allow-writes`
(the transport is write-gated even for reads):

```sh
sudo python3 tools/fingerprint/oxp3-fp-probe.py --crs /tmp/fprt-crs.bin \
    --spidev /dev/spidev0.0 --allow-writes --gpio 42 --irq-edge rising \
    fw info uid count
```

`--gpio`/`--irq-edge`/`--irq-active-low` are optional; `--crs` fills in speed,
mode, bits and the IRQ/reset pins when the CLI does not override them.
`--gpio-chip` accepts `gpiochipN`, a `/dev/gpiochipN` path, or a chip
name/label; `--gpio-backend auto|gpiod|chardev|sysfs|poll` (or
`$FPBF63112_GPIO_BACKEND`) forces one backend instead of the auto order.
`--allow-op` extends the read allowlist with one `--op` byte, and even then a
destructive selector is refused. Reset additionally needs `--reset-gpio N
--allow-reset`. `--dry-run` never opens `/dev/spidev*` or any GPIO; it prints
the selector and transfer shape for each command. `--transport frame` is the
old `0xAA`/CRC experiment and is not the SPI protocol.

### 8.4 Safety model in one line

Read-only commands are an explicit allowlist; destructive commands are refused
by opcode *and* selector; no write opcode is allowlisted; a device path and
`--allow-writes` are both mandatory for I/O; `--dry-run` and `acpi_info` do no
I/O at all; nothing here installs a driver, writes firmware, enrols, deletes a
template or touches the module's stored data.

### 8.5 C implementation in the libfprint driver (2026-11-09)

Status: **code and offline tests only; like §8.1 this has never run against the
sensor.** It is a C mirror of `spi.py`/`acpi.py`, written so the libfprint
driver does not need libgpiod or a spidev helper library.

| file | contents |
|---|---|
| `bf63112_spi.h/.c` | selector `(opcode & 0x3f) << 1` (bit 7 = write), the destructive/alias gate, the 256-bit read/write allowlists, the two-entry `SPI_IOC_MESSAGE` plan, `SPI_IOC_WR_MODE`/`WR_BITS_PER_WORD`/`WR_MAX_SPEED_HZ`, the recovered timeouts and deadline arithmetic, cancel/settle, sysfs IRQ + reset backends, optional libgpiod v2/v1 IRQ/reset backends, and a timed-poll fallback that is reported as unobserved |
| `bf63112_acpi.h/.c` | `_CRS` walker: SpiSerialBus (speed, bits, phase/polarity, CS, source, SPI type flags), GpioInt/GpioIo (pins, triggering/polarity, IO restriction bits 5-6), small/extended IRQ, opaque items; every read is bounds-checked and malformed input returns a typed error |
| `bf63112_discover.h/.c` | resolves `/dev/spidevX.Y` from the single `/sys/class/spidev/*` whose `device/modalias` is `acpi:BF63112` (an explicit path/udev path wins); refuses to guess when several match |
| `libfprint-bf63112.c` | opens/configures the spidev node, optionally pulses reset (5 ms/10 ms, then 50 ms), runs 0xA0 then 0xC1 on open (vendor query order, session steps omitted), re-queries 0xC1 on resume, closes the transport on suspend/close, and keeps enrol/verify/identify `FP_DEVICE_ERROR_NOT_SUPPORTED` |
| `tests/test_spi.c` | standalone unit test: frame builder, selector, allowlist refusal, timeout arithmetic, plan/execute, cancel, reset timing, IRQ budget, ACPI parser and discovery; injects the I/O vtable, clock, sleep, IRQ and reset line, so it never opens `/dev/spidev` or a GPIO |

The read length is the caller's fixed answer size - there is no length field
and no leading status byte on the wire: 0xA0 = 7, 0xC1 = 9 (what the driver
uses), and 0xA1 = 64, 0xB3 = 4, 0xC0 = 16, 0x2D = 1 for the other allowlisted
reads.  The C driver never builds a write plan: the session handshake (0xA2)
and every other write are outside the allowlist, so the OpenSSL session module
remains a standalone compile check rather than an open()-time handshake. Reads
still require an explicit caller opt-in (`policy.allow_io`, set only by the
driver), exactly like the Python `allow_writes` gate; destructive opcodes and
their selector aliases are refused even if a caller allowlists them.

**Build and test commands** (all offline; the driver itself is never loaded):

```sh
# C protocol + SPI/ACPI unit tests; on a host without a compiler this script
# runs the same targets in the documented ephemeral podman image
tools/fingerprint/tests/run-c-tests.sh
# equivalent:
make -C tools/fingerprint proto-test spi-test
make -C tools/fingerprint syntax        # stub headers, not libfprint
make -C tools/fingerprint gpiod-syntax  # optional libgpiod v2 compile check

# authoritative API check against a real libfprint 1.94.9 source tree
tools/fingerprint/tests/check-libfprint.sh
# equivalent (the script also creates the meson-generated fp-enums.h /
# fpi-enums.h placeholders that a bare make syntax-real needs):
mkdir -p /tmp/enums && : > /tmp/enums/fp-enums.h && : > /tmp/enums/fpi-enums.h
make -C tools/fingerprint syntax-real LIBFPRINT_SRC=/tmp/libfprint-1.94.9 \
    ENUMS_INCLUDE=/tmp/enums
```

**Actual results on this host (2026-11-09, no host C compiler):**

* `spi-test`: `test_spi: 225 checks, 0 failures` (gcc 16.2.1 in
  `localhost/oxp3-fixes-build:f45`).
* `proto-test`: `ok: 324 checks, 10 vectors` (unchanged).
* Python suite: `Ran 289 tests ... OK`.
* `syntax` (stub): OK; `syntax-real` against v1.94.9: `syntax-real: OK`
  (only `fp-enums.h`/`fpi-enums.h` empty placeholders, as the script documents).
* `gpiod-syntax`: `OK (libgpiod v2)` with libgpiod-devel 2.3.1. The v1
  backend is compiled only when `BF63112_HAVE_GPIOD_V1` is defined and was not
  available in this image, so it is source-reviewed but not compile-checked
  here.
* No test opened `/dev/spidev`, requested a GPIO, or sent anything to the
  sensor.

**What still requires the hardware:**

* whether a read really is "selector, then clock the answer in the same
  `SPI_IOC_MESSAGE(2)`" (what both implementations do) or the module raises
  `GpioInt` first and expects the read only after the interrupt. **This is the
  main remaining risk:** the current implementation performs the IRQ wait
  *after* the transfer and does not gate the read on it, so if the module
  clocks the answer only after the IRQ, the second transfer will read
  `0xFF`/zeros and the plan must be split into "selector write, wait, read";
* the real SPI opcode set: 0xA0/0xA1/0xB3/0xC0/0xC1 are read-only experiments
  borrowed from the USB dispatch table, and 0xC1's 9-byte answer is not proven
  on SPI;
* the `_CRS` values (speed, mode, bits, chip select) and whether the PXA2xx PIO
  path at that clock behaves; the `_CRS` is root-only and this session forbids
  privilege escalation, so it was not read here;
* whether the module needs the `GpioInt` at all, and in which order: the
  controller/line mapping is now resolved (GPI1/`gpiochip1`, IRQ 18, reset 16;
  §8.6), but no run has yet proven that the first answer depends on the
  interrupt;
* the reset pulse polarity: `_CRS`/ACPI `GpioIo` carries no polarity, so it is
  a measured property - the 2026-11-09 run settled on **active-high** with a
  10 ms settle (§8.7); `--reset-polarity` still allows active-low for another
  unit;
* suspend/resume semantics: the driver closes the spidev node instead of
  sending the unproven 0xB6 SLEEP selector;
* the answer length/record layouts for `0xA1` and the UID/count queries.

### 8.6 GPIO v2 character-device backend and this board's lines (2026-11-09)

This host has **no `/sys/class/gpio`** (the kernel is built with
`CONFIG_GPIO_SYSFS` off) and the Python `gpiod` module is not installed, so
both backends the probe had before are unusable here. `fpbf63112/spi.py` now
has a third, pure-stdlib backend that talks to `/dev/gpiochipN` with
`fcntl.ioctl`/`struct` only. It is registered as `chardev`:

| ioctl | number | struct | size |
|---|---|---|---|
| `GPIO_GET_CHIPINFO_IOCTL` | `0x8044B401` | `gpiochip_info` | 68 B |
| `GPIO_V2_GET_LINE_IOCTL` | `0xC250B407` | `gpio_v2_line_request` | 592 B |
| `GPIO_V2_LINE_GET_VALUES_IOCTL` | `0xC010B40E` | `gpio_v2_line_values` | 16 B |
| `GPIO_V2_LINE_SET_VALUES_IOCTL` | `0xC010B40F` | `gpio_v2_line_values` | 16 B |
| `read()` from the request fd | - | `gpio_v2_line_event` | 48 B |

`struct gpio_v2_line_config` is 272 B (32-byte header + 10 x 24-byte
`gpio_v2_line_config_attribute`); inside the request the offsets are at 0,
`consumer` at 256, the config at 288, `num_lines` at 560,
`event_buffer_size` at 564 and the returned `fd` at 588. The ioctl numbers are
`_IOC()`-encoded from exactly those sizes, so a size mistake changes the number
and the kernel answers `ENOTTY` instead of misreading the buffer.

* **Chip lookup.** `gpiochip1`, an absolute path such as `/dev/gpiochip1`, or
  a chip name/label (`GPI1`, `INTC10BC`) resolved by scanning `/dev` and
  matching `gpiochip_info.name`/`label`.
* **Reset (output).** The line is requested as `OUTPUT` with one
  `OUTPUT_VALUES` attribute (id 2) carrying the initial physical level;
  `set(1)/set(0)` issues `GPIO_V2_LINE_SET_VALUES_IOCTL` with `mask=1`.
  `active_low` is applied in software (the physical bit is inverted); the
  request never sets `GPIO_V2_LINE_FLAG_ACTIVE_LOW`, so the level on the wire
  is explicit and testable.
* **IRQ (input/event).** Request `INPUT | EDGE_RISING`, `EDGE_FALLING` or both
  (`--irq-edge`); `select()` with the caller's timeout; `read()` whole 48-byte
  events; event id 1/2 -> `rising`/`falling`, `timestamp_ns` is carried in
  `IrqEvent.timestamp_ns`, and `active_low` inverts the logical edge. `EINTR`
  from `select`/`read` is a clean timeout; a clean timeout is what
  `GpioIrqWaiter.wait` turns into `SpiTimeout`.
* **Typed failures.** `GpioUnsupported` (`ENOTTY`: not a GPIO v2 chardev),
  `GpioInvalidRequest` (`EINVAL`: kernel rejected the request/values),
  `GpioPermissionDenied` (`EACCES`/`EPERM`), `GpioLineBusy` (`EBUSY`),
  `GpioChipNotFound`, `GpioLineOutOfRange`. The auto chain catches them and
  tries the next backend; a forced backend reports the typed reason instead of
  falling back.

**Backend order and forcing.** IRQ: `gpiod -> chardev -> sysfs -> poll`;
reset: `gpiod -> chardev -> sysfs`. `--gpio-backend chardev` in the probe (and
in `oxp3-fp-session.sh`, which forwards it) or `FPBF63112_GPIO_BACKEND=chardev`
forces it deterministically. `poll` is IRQ-only and the probe refuses it when a
reset line is configured. Unit coverage lives in
`tests/fingerprint/test_gpio_chardev.py` (31 tests, all ioctls and errors
monkeypatched; no real `/dev/gpiochip` is opened).

#### Resolved chip and lines on this unit

| item | value | evidence |
|---|---|---|
| controller | `\_SB.GPI1` = ACPI `INTC10BC:01` = `/dev/gpiochip1` | device `GPI1` has `_HID INTC10BC` (via `GHID`), `_CID INTC105F` and `_UID = 1` (DSDT `DeviceOp` at `0x120c5`); live sysfs `/sys/bus/acpi/devices/INTC10BC:01/path` = `\_SB_.GPI1`, `uid` = `1`, and `/sys/bus/platform/devices/INTC10BC:01/gpiochip1` exists. Platform enumeration `INTC10BC:00..04` yields `gpiochip0..4` (`:01` -> `gpiochip1`), which is also the debugfs `/dev/gpiochipN` order. |
| IRQ | line **18** | FPRT `_INI` computes `INT1 = GNUM(0x001A0412)`; `GNMB(arg)` is `arg & 0x7f` = `0x12` = 18 (the controller base from `GINF` is 0 here). |
| reset | line **16** | the same `_INI` computes `TRST = GNUM(0x001A0410)`; `arg & 0x7f` = `0x10` = 16. |

The ACPICA/AML evidence, decoded from the DSDT captured in
`docs/state/fp-session-20261109T110322Z/dsdt.aml` (sha256
`df64f6828a93b8309a7c552c03dd0d4e367cf3229e244b3878c96fbb11500921`):

```asl
Device (FPRT) {                         // DeviceOp 0x02bbea
    Method (_INI, 0, NotSerialized) {   // 0x02bc9a, body 0x02bca2
        LDGP = 0x001A0412
        LRGP = 0x001A0410
        INT1 = GNUM (LDGP)              // -> 18
        TRST = GNUM (LRGP)              // -> 16
        SHPOLDGP = One
        SHPOLRGP = One
        Return (Zero)
    }
    Method (_CRS, 0, NotSerialized) {   // 0x02bce4
        Return (ConcatenateResTemplate (SBFB, SBFG))
    }
}

Method (GNUM, 1, NotSerialized) {       // DSDT 0x0137fd
    Local0 = GNMB (Arg0)                // Arg0 & 0x7f
    Local1 = GGRP (Arg0)                // (Arg0 >> 7) & 7
    Local2 = GCOM (Arg0)                // (Arg0 >> 10) & 7
    Return (GINF (Local2, Local1, 7) + Local0)
}
```

Two consequences matter for the probe:

1. The static template in `SBFG` carries a `GpioInt` (edge-triggered,
   active-high, wake-capable) and a `GpioIo`, but **both pin tables are 0**;
   the real line numbers only exist as the `INT1`/`TRST` objects that `_INI`
   creates. That is why `--irq-pin 18 --reset-pin 16` is required.
2. `_CRS` is `ConcatenateResTemplate(SBFB, SBFG)`, and the runbook's AML scan currently
   accepts the first `Return(Buffer)` that parses as an SPI template, i.e. it
   extracts `SBFB` only (38 B). Its `acpi-info.txt` therefore says "no
   `GpioInt`" even though `SBFG` has one. Until the scanner is taught to
   concatenate the two named buffers, the CLI overrides are the reliable way
   to pass the GPIOs; this is a known limitation, not a hardware fact.

### 8.7 The try-matrix interpretation guide

`--try-matrix` (also reachable as the runbook's `--try-matrix`) exists because
the single-setting run can only test one hypothesis per boot: after the
2026-11-09 run there were three at once (reset polarity, settling time,
interrupt edge). One invocation walks all of them:

* **reset polarity** - active-low, active-high and `hold` (never pulse the
  line; the control),
* **settle after release** - 10 ms and 150 ms,
* **interrupt edge** - rising, falling and both,

18 cells in a fixed order (active-low/10 ms/rising first). Every cell sends
only the allowlisted read-only `0xA0`; `0xA1` and `0xC1` follow only when
`0xA0` is non-zero. The per-attempt timeout is 1.5 s. The matrix stops at the
first cell whose answer **parses** - a complete `0xAA | L:u32 | ... | CRC-32`
frame (CRC verified) or the bare fixed-size answer, checked against the
recovered `0xA0` (7 B: state/project/version), `0xA1` (table) and `0xC1` (9 B:
geometry/flags) layouts. Non-zero but unparseable is *not* success; it stops
the matrix and is reported as malformed, because the bus clearly worked in
that setting.

The console table and `matrix.json` have one row/record per cell with the
polarity, settle, edge, the `0xA0` bytes and the verdict; `summary.json`
embeds the same object under `matrix`. Read the outcomes like this:

| what the table says | interpretation | next move |
|---|---|---|
| every cell `error` with no bytes (backend/open failure) | the command itself failed: no GPIO backend, bad `--gpio-chip` path, or spidev open/ioctl failure | fix the device/line selection first; nothing about the sensor was tested |
| every cell `timeout` and `(none)` bytes | no transfer completed, or the device is not running: wrong interrupt pin/edge, module held in reset, or ACPI power state | check `_PS0`/power, the `GpioInt` line 18 mapping and `bytes_tx`; try the IRQ-first ordering |
| `zeros` (or `00` bytes on every cell) | the SPI bus clocked but the sensor never drove MISO: wrong chip select/controller, SPI mode/clock, or the module needs the IRQ before it answers | fix CS/mode/clock from `_CRS`; then test the IRQ-first read order (a code change §8.5 lists as the main risk) |
| all `ff` | nothing on the bus at all: wrong chip select/controller or the module is held in reset | verify the spidev node really belongs to this device; check reset wiring/polarity |
| `timeout` with **non-zero** bytes | the answer came back but no edge was seen: the interrupt pin/edge/polarity is wrong (data is still useful) | read the bytes; if they parse, the matrix already reports `valid` and the remaining problem is only the IRQ |
| `short` / `malformed` non-zero | the transport works but the framing/layout does not match: wrong answer length, a `0xAA` frame the parser rejects, or these are not the right SPI opcodes | capture the full bytes from `matrix.json`; check the frame `L`/CRC and the command table |
| one cell `valid`, everything before it failed | the correct combination was found: polarity/settle/edge are in the winning row, and the query that parsed is named in `verdict` | repeat that exact single setting in a normal `--reads` run, then keep it for the driver |

The matrix is still strictly read-only: only `0xA0`, `0xA1` and `0xC1` can be
sent, `SpiTransport` re-checks the allowlist and the destructive
opcode/selector gate, no write opcode is reachable, and the reset pulse itself
requires `--allow-reset`. The probe's JSON records the context (spidev, chip,
IRQ/reset pins, timeout) and every query's bytes/verdict, so an all-failed
verdict names exactly which combinations were tried and the next session does
not repeat them.

### 8.8 The try-write-shape guide

The matrix run on 2026-11-09 settled the electrical side: reset **active-high**,
10 ms settle, **rising** edge. Attempt 7/18 then answered over SPI for the
first time - `0xA0` returned 64 bytes starting `00 00 00 ff 7f 50 03`, and
`0xA1` returned `00 00 ff 7f 50 03`: the same payload with a **different
number of leading zero bytes**. The payload is shifted relative to our read
window, so the next unknown is the write shape, not the wiring.

`--try-write-shape` (runbook: `--try-write-shape`) holds the electrical
settings pinned and varies only what the host writes before clocking MISO:

* **selector bit 0** - the recovered read selector has bit 0 clear; the
  alternative hypothesis is that bit 0 is the read flag,
* **write padding after the selector** - 0, 1, 2, 3, 4 zero bytes, plus the
  "two zeros then the command's phase byte" hypothesis,
* **read length** - the recovered layout length and a full 64 bytes,

for `0xA0`, then `0xA1`, then `0xC1`, 2 x 6 x (2 + 1 + 2) = **60 cells**, each
with a 400 ms timeout. The probe stops at the first **valid** answer. Success
is a CRC-valid `0xAA | L:u32 | ... | CRC-32` frame at any offset, or the bare
recovered layout anchored at read offset 0 (`0xA0` project in
`{1,2,3,4,6}`, `0xC1` geometry `1..4096`, `0xA1` first word `1..4096`). A bare
layout found only at a non-zero offset is `shifted`, not success: it is
reported with its offset so the padding-to-shift relationship is visible.

The console table and `write-shape.json` have one record per cell:

| field | meaning |
|---|---|
| `selector` | the actual byte written, e.g. `0x40` (read, bit 0 clear) or `0x41` (bit 0 set) |
| `padding` / `padding_hex` | how many bytes followed the selector and what they were |
| `read_len` | bytes clocked back |
| `hex` | the complete answer |
| `aa_offset` / `aa_offsets` | first (and all) `0xAA` positions, `null` when none |
| `crc` | `ok`, `crc`, `short`, `bad-length` or `none` |
| `candidate_offset` | best offset where the recovered field appears - the shift evidence |
| `verdict` | `valid`, `shifted`, `zeros`, `all-ff`, `short`, `crc-mismatch`, `timeout`, `error`, ... |

All-fail outcomes still carry the information:

* **all `zeros`/`all-ff`** - the write shape did not get MISO driven at all;
  the selector/padding matrix is exhausted and the next hypothesis is the
  IRQ-first ordering (§8.5), not another padding count.
* **`timeout` with bytes** - the data path works; only the IRQ edge/pin is
  wrong. Those bytes are still scored for shift.
* **`shifted` everywhere** - compare `candidate_offset` across paddings. If it
  drops by one for every added padding byte, the device consumes one command
  byte per padding byte; if it moves the other way (or not at all), the read
  clock itself is being consumed and the next shape needs a longer command
  prefix, not more padding.
* **`crc-mismatch` at offset 0** - the 0xAA framing is present (USB branch
  shape) but the L field/CRC does not validate; the answer is truncated or
  preceded by a status byte.
* **one `valid` cell** - its selector/padding/read length is the new write
  shape; repeat it in a normal read before changing the driver.

Only `0xA0`, `0xA1` and `0xC1` can be sent, `SpiTransport` re-checks the read
allowlist and the destructive-opcode/selector gate (including with selector bit
0 set, so `0x05` still aliases JTAG's `0x04`), and the reset pulse itself needs
`--allow-reset`. `write-shape.json` is embedded in `summary.json` under
`write_shape`.

## 8.9 The recovered read frame, per command (2026-11-09)

Status: **the byte the driver writes is determined exactly; the lead bytes
between the selector and the payload are a measured property, not a DLL
field.** This subsection supersedes the "missing command bytes" reading in
§8.8: the DLL does not build a longer frame, and the 0xAA frame is not on this
link at all.

### What the DLL writes on the ACPI/SPB path

| command | DLL function | selector byte | read clocks | frame bytes on the wire |
|---|---|---|---|---|
| read (any opcode) | `FUN_180017020` @ `0x180017020` | `(opcode & 0x3f) << 1` | `param_4` | `selector` then `N` receive clocks |
| write | `FUN_180017404` @ `0x180017404` | `0x80 \| ((opcode & 0x3f) << 1)` | - | `selector` + `param_4` payload bytes |
| module id probe | `FUN_18000efa8` @ `0x18000efa8` | reads 1 byte each from registers `0x37` and `0x36` | 1 + 1 | two selector-only reads |

`FUN_180017020` is three lines of interest: `local_268[0] = (param_2 & 0x3f) *
'\x02'`, then `FUN_18001197c(puVar5, local_268, 1, param_3, param_4)`, then
`WaitForSingleObject(event, param_5)`. `FUN_18001197c` @ `0x18001197c` builds
one `SPB_TRANSFER_LIST` with **two entries** - `ToDevice` 1 byte (the selector)
and `FromDevice` `param_4` bytes - and submits it as a single request, so chip
select is asserted across both. There is no length field, status byte, phase,
argument or CRC. `FUN_180017404` @ `0x180017404` is the same shape with the
payload as the second entry and bit 7 set in the selector.

The 0xA0/0xA1/0xC1 **opcodes do not exist on this link**: the selector carries
only six opcode bits, so the driver's own register protocol numbers them
`0x20`, `0x21`, `0x01` and puts `0x40`, `0x42`, `0x02` on the wire. That is the
same aliasing the safety gate already blocks for JTAG/restore.

### Where the 0xA0/0xA1/0xC1 command frames actually live (USB only)

| opcode | dispatch row @ RVA `0x46F00` | request wrapper | frame builder |
|---|---|---|---|
| `0xA0` `CMD_QUERY_FIRMWARE_VER` | `{opcode, "CMD_QUERY_FIRMWARE_VER", 0x1800181b0, "FwVer"}` | `FUN_180001808` -> `FUN_180004874(dev, 0xa0, 0, 0, 1)` | `FUN_180004358` |
| `0xA1` `CMD_QUERY_MODULE_SCHEME_INFORMATION` | `{..., 0x1800183b0, "Scheme"}` | `FUN_1800018c4` -> `FUN_180004874(dev, 0xa1, 0, 0, 1)` | `FUN_180004358` |
| `0xC1` `CMD_QUERY_SENSOR_INFO` | `{..., 0x180019420, "SensorInfo"}` | `FUN_180001b8c` -> `FUN_180004874(dev, 0xc1, 0, 1, 1)` | `FUN_180004358` |

The dispatch table is stride `0x20`: `{opcode:u64, name:char*, handler:fn,
label:char*}`; the 0xA0 row is at file offset `0x45900` (RVA `0x46F00`),
0xA1 at `+0x20`, 0xC1 at `+0x160` (file `0x45a60`). The wrapper chain is:
`FUN_180001808` -> `FUN_180004874` @ `0x180004874` fills a 24-byte descriptor
(`+0x10` opcode, `+0x11` phase/arg, `+0x14` data_len, `+0x20` wait flag,
`+0x24` secure flag, `+0x2c` 1000 ms, `+0x30` 2030 ms) and calls
`FUN_180004358` @ `0x180004358`, which builds and sends the 0xAA frame through
`FUN_18001b6e4` (USB pipe at device context `+0x118`). The answer parsers
`FUN_1800181b0` (7-byte fw record), `FUN_1800183b0` (scheme) and
`FUN_180019420` (9-byte sensor info) are reached only through the USB
continuous-reader reassembler `FUN_18001ac60`/`FUN_18001aadc`; the SPB session
`FUN_180015acc` -> `FUN_1800151cc` -> `FUN_18000f644` never references them.

For completeness, the USB frame these wrappers send is byte-exact: `0xAA`,
`L:u32 LE` = `20 + align16(10 + data_len)`, 16 zero header bytes, `0x4C`,
opcode, phase, `0x00`, `arg:u16 LE`, `data_len:u32 LE`, data, zero padding to
a 16-byte block, then CRC-32 (reflected `0xEDB88320`, init `0xFFFFFFFF`, final
XOR) little-endian over everything before it. For a datum-only query
(`data_len = 0`) that is `AA 24 00 00 00` + 16 zeros + `4C <op> 00 00 00 00 00
00 00 00` + 6 pad zeros + 4 CRC bytes = 41 bytes; 0xC1 is sent with the secure
flag set (`FUN_180004874`'s `param_4 = 1`, descriptor `+0x24`), so its command
block is SM4-transformed before the CRC.

### Reconciling the shift

The 2026-11-09 write-shape run (`docs/state/fp-session-20261109T121445Z/
write-shape.json`, cells 1-12 and 25-29) read the same selector with 0..4 zero
bytes appended to the write phase. The payload's position in the read buffer
moved one byte left per appended byte:

| padding | `0x40` (0xA0) read | `0x42` (0xA1) read |
|---|---|---|
| 0 | `00 00 00 ff 7f 50 03 ...` | `00 00 ff 7f 50 03 ...` |
| 1 | `00 00 ff 7f 50 03 ...` | `00 ff 7f 50 03 ...` |
| 2 | `00 ff 7f 50 03 ...` | `ff 7f 50 03 ...` |
| 3 | `ff 7f 50 03 ...` | `7f 50 03 ...` |

The device streams its answer continuously from the clock after the selector;
the selector transfer is transmit-only in the DLL (`FUN_18001197c` entry 0 has
no receive buffer), so every extra byte clocked in that phase consumes one
answer byte. Padding `P` therefore starts the read window `P` bytes into the
answer.

Two readings fit this data and are indistinguishable from the DLL alone:
(a) the device waits `3` (0xA0) / `2` (0xA1) bytes of command arguments and the
zeros are placeholders; or (b) those bytes are the beginning of the answer and
the padding throws them away. The DLL evidence favours (b): its read is one
selector byte, and its register write/read verification (`FUN_18000f504`
writes a byte, reads one byte back and compares) requires the first answer byte
to arrive on the first read clock. Either way the *wire bytes* for a read that
puts the payload at offset 0 are the same: **selector + 3 zero bytes for 0xA0,
selector + 2 zero bytes for 0xA1**. 0xC1's lead was never measured - the run
stopped at the first aligned scheme answer - so the builder leaves it empty
rather than inventing it.

The payloads are `ff 7f 50 03 ...` in both cases. For 0xA0 the USB layout says
`[state:u8][project_hi:u8][project_lo:u8][version:u32 LE]`, i.e. state `0xFF`,
project bytes `0x7F`/`0x50` (project `0x7F50`, not in the recovered
`{1,2,3,4,6}` table) and version `3`; the scheme table has no status byte, so
`ff 7f 50 03` there is only known to be the first four bytes of the table.
Whether the leading zeros belong to the device's answer or are arguments the
device consumes cannot be decided statically, and is the one open point.

### Code and tests

`fpbf63112/spi.py` now has `READ_LEAD_BYTES` (`0xA0: 00 00 00`, `0xA1: 00 00`),
`ReadFrame`, `build_read_frame()`, `send_read()`, `SpiTransport.read_frame()`
and `parse_read_answer()` (bare answer + optional 0xAA/CRC-32 frame; rejects
short answers, non-zero lead bytes and a corrupt CRC). The probe defaults to
`--read-shape frame` (selector + measured lead); `--read-shape selector` keeps
the DLL-exact one-byte frame for comparison. Unit coverage is
`tests/fingerprint/test_spi.py::TestReadFrame` and `TestReadAnswer`.

## 9. Next steps

**If the goal is this unit (SPI-only, the realistic path).** This is the exact
root-time order; steps 1-3 are read-only, step 4 changes only the `spidev` bind,
and every step is reversible. **All of it is automated, with confirmations,
write-ahead logging and a verified rollback, by
`tools/fingerprint/oxp3-fp-session.sh`** (docs:
[`fingerprint-session.md`](fingerprint-session.md)) - the steps below stay as the
explanation and as the fallback if the script cannot run.

1. **Extract the `_CRS` (root, read-only).**

   ```sh
   sudo cat /sys/firmware/acpi/tables/DSDT > /tmp/dsdt.aml
   iasl -d /tmp/dsdt.aml
   grep -n -A 60 'FPRT' /tmp/dsdt.dsl
   ```

   Find the `ResourceTemplate ()` of the matching `_CRS` and save exactly the
   bytes between the braces as binary, e.g. from the hex dump:

   ```sh
   printf '%s' '8e 21 00 01 01 02 ...' | xxd -r -p > /tmp/fprt-crs.bin
   python3 tools/fingerprint/oxp3-fp-probe.py acpi_info --crs /tmp/fprt-crs.bin
   ```

   `acpi_info` prints the `SpiSerialBus` (device selection, speed, bit length,
   phase, polarity), the `GpioInt` pin/triggering/polarity, any `GpioIo` reset
   pin, and the derived Linux SPI mode. No device is opened.

2. **Inspect the plan offline** with the real `_CRS` values:

   ```sh
   python3 tools/fingerprint/oxp3-fp-probe.py --crs /tmp/fprt-crs.bin \
       --spidev /dev/spidev0.0 --dry-run fw info uid count
   ```

   This prints the selector and the two-entry transfer for each read, plus the
   recovered 1000/2030 ms deadlines, without opening anything.

3. **Bind `spidev` (the only state change; reversible):**

   ```sh
   sudo modprobe spidev
   echo spidev | sudo tee /sys/bus/spi/devices/spi-BF63112:00/driver_override
   echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/bind
   ls -l /dev/spidev0.0
   ```

4. **Read-only attempt** on the recovered query opcodes (`0xA0`, `0xA1`,
   `0xC1`; `0xB3`/`0xC0` are also allowlisted). Start without the IRQ/reset and
   add them only if `_CRS` supplies the pins:

   ```sh
   sudo python3 tools/fingerprint/oxp3-fp-probe.py --crs /tmp/fprt-crs.bin \
       --spidev /dev/spidev0.0 --allow-writes --gpio <GpioInt pin> \
       --irq-edge rising --irq-active-low fw info
   ```

   A healthy answer is 7 bytes for `fw` and 9 for `info`; all-`0xFF` or no
   answer usually means wrong SPI mode/clock or a device that expects a
   different transaction order. `cat
   /sys/bus/spi/devices/spi-BF63112:00/statistics/bytes_tx` shows whether the
   transfers happened at all. Stop after the first answer or first failure;
   do not retry destructive opcodes (the probe cannot send them anyway).

5. **Roll back:** `echo spi-BF63112:00 | sudo tee
   /sys/bus/spi/drivers/spidev/unbind`, remove `driver_override`, and
   `sudo modprobe -r spidev`.

6. **Only after a read answers**, the libfprint SPI driver
   (`tools/fingerprint/libfprint-bf63112.c`, §8.5) can be validated. It already
   carries the selector/two-entry transfer, the IRQ waiter, the optional reset
   pulse and the 1000/2030 ms deadlines; the first hardware question is whether
   the answer arrives in the same message or only after the IRQ (see §8.5).
   Mirror the reset behaviour only if `_CRS` really exposes a `GpioIo` -
   Windows itself drives it through the resource hub, so the line exists on
   some units.

**Still unknown until hardware answers:**

* whether a read is really "selector, then clock the answer in the same SPB
  message" or the module raises `GpioInt` first and expects a second
  transaction (the code implements the former; the latter is the first thing
  to test if the first read is all-`0xFF`);
* the SPI opcode set on the wire - the SPI branch is data-driven, so the
  `0xA0`/`0xA1`/`0xB3`/`0xC0`/`0xC1` bytes are deliberate read-only
  experiments borrowed from the USB dispatch table, not proven SPI opcodes;
* the real answer length for `0xA1` and whether `0xC1` is the same 9-byte
  record as on USB;
* whether the module raises the interrupt before it answers, and whether the
  50 ms reset settle is needed. The lines themselves are resolved (§8.6): the
  static `SBFG` `GpioInt` is edge/active-high/wake-capable, but the real
  numbers (IRQ 18, reset 16) come from `_INI`/`GNUM` and must be passed with
  `--irq-pin`/`--reset-pin`;
* the reset polarity - ACPI `GpioIo` has no polarity field, so it is a
  measured property: the 2026-11-09 run settled on **active-high**, 10 ms
  settle and a rising edge, and normal reads now default to that
  (`--reset-polarity`, `--settle-ms`); `--try-matrix` (8.7) still sweeps the
  older hypotheses for a different unit;
* the controller-side values (speed/mode/bits) and whether the PXA2xx PIO path
  with no DMA channels at 1 MHz behaves; and
* what the `00 00 00` / `00 00` lead really is. The wire frame that lands the
  payload at offset 0 is settled (§8.9: selector + 3 zero bytes for `0xA0`,
  + 2 for `0xA1`), but whether those bytes are a command argument the device
  waits for or the head of its answer cannot be decided from the DLL; the DLL
  itself writes only the selector. `0xC1`'s lead was never measured (the
  write-shape run stopped at the first aligned `0xA1` answer), so the probe
  sends no lead for it until a run measures one;

**If a USB unit ever shows up (or for completeness):**

1. `tools/fingerprint/usb_probe.py` - enumerates 347D, prints interfaces,
   endpoints, driver claim; sends **no** vendor command. Run it first on any
   new unit; a 347D hit flips the whole plan to libusb.
2. If it hits: claim interface 0, take bulk `0x81`/`0x02`, feed the existing
   frame library through a `bulk_transfer` transport, add a udev rule
   (`idVendor=347d`, `TAG+="uaccess"`). Expect the same secure-frame question
   for enrol.

## 10. Provenance

| item | value |
|---|---|
| `BetterlifeFingerprintDevice.dll` (X64) | sha256 `826177bfe1cdc2058842dde6a0dd3ee2c89fd2d5d183c3a7d87ac7eaded0d93d`, 9.39.15.26, 2025-08-13 |
| `BetterlifeFingerprintDevice.inf` | sha256 `323d04d84fe37b7d3acda6c32c18840085f8205643e43ae833a07bdf3e87d8c0` |
| `betterlifefingerprintdevice.cat` | sha256 `9081e8c7f7bec9452627ab106205a76ad4f9e84dcbc82ae78e48ff192a38158d` (DER PKCS#7, catalogues both hardware-ID sets) |
| decompiler dump | `tools/ghidra-pod.sh fpusb <dll>` -> `~/.cache/ghidra-agents/fpusb/out/decomp.txt` (805 functions) |
| key functions | `FUN_180005a2c` EvtDeviceAdd / `FUN_180006a60` PrepareHardware / `FUN_180019d90` USB init / `FUN_180015acc` SPB init / `FUN_18001B6E4` USB frame write / `FUN_180004358` frame+CRC+retry / `FUN_18001AC60` answer reassembly / `FUN_180011550` resource-hub GPIO / `FUN_180017020`+`FUN_18001197C` SPB read / `FUN_180017404` SPB write / `FUN_180013b60` SPB cancel / `FUN_180016260` reset-target open + 50 ms / `FUN_180016110` 5/10 ms reset pulse |
| firmware descriptors | DLL file offsets `0x521d0` (0304), `0x844d4` and `0xbf69c` (0302 FS), `0xf81fe` (0302 HS) |

No vendor binary, decompiled listing or vendor string beyond short quoted
identifiers is stored in the repository.
