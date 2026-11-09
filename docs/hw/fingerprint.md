# ONEXPLAYER 3 fingerprint reader: what it is, and why Linux cannot use it yet

Status: hardware identified, Windows protocol recovered far enough to build a
frame-accurate probe, **no Linux support exists yet** - a new libfprint SPI
driver is the only path. The protocol now also exists as a tested userspace
library (`tools/fingerprint/fpbf63112/`), the recovered SPI/SPB transport
(`fpbf63112/spi.py`), a `_CRS` parser (`fpbf63112/acpi.py`) and a probe with
`--transport spi --spidev --allow-writes --dry-run` and `acpi_info`, plus a
driver that compiles against libfprint 1.94's internal driver API. The Python
suite is now 388 tests and all SPI/ACPI behaviour is exercised offline, but
**nothing has run against the sensor** (see
`tools/fingerprint/README.md`). The remaining unknowns are listed in
[Open points](#open-points); two of them need root on this unit.
Owner: silentsudin. Host: Anatase 20261026.3, kernel 7.2.7-an28.fc45,
libfprint 1.94.100, fprintd 1.94.5.

## Summary

On this unit the sensor is **not USB**: it is a Betterlife module on the Intel
LPSS SPI bus, enumerated by ACPI as `ACPI\BF63112` at `\_SB.PC00.SPI1.FPRT`
("FPRT" = fingerprint reader target). Linux boots, the SPI controller works,
and the sensor sits there with **no driver bound and no `spidev` node**, so
neither `fprintd` nor anything else can talk to it. Windows drives it with a
user-mode (UMDF2) WBDI driver that speaks a small framed command protocol; the
module is **match-in-sensor** (it stores and compares templates itself, and it
owns the matching algorithm).

The vendor driver is not SPI-only: the *same DLL* also implements a full USB
transport, selected at runtime by the device's hardware ID, for the SKU whose
module is wired to USB (`USB\VID_347D&PID_0302/0304`). Our board's ACPI device
never takes that branch. This matters for the protocol work below - see
[`fingerprint-transport.md`](fingerprint-transport.md) for the per-SKU verdict
and for the consequence that the framing recovered here belongs to the USB
branch.

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

| opcode | command | answer |
|---|---|---|
| `0xA0` | `CMD_QUERY_FIRMWARE_VER` | 7 B: state, project hi/lo, 4-byte version |
| `0xA1` | `CMD_QUERY_MODULE_SCHEME_INFORMATION` | scheme table |
| `0xA2` | `CMD_QUERY_ROOT_KEY` | ≥113 B: session handshake (SM2/SM3) |
| `0xB0` | `CMD_ENROLL_FINGERPRINT` | Start / Ing / Over / Cancel |
| `0xB1` | `CMD_IDENTIFY_FINGERPRINT` | Start / Cancel; 18-byte handle |
| `0xB2` | `CMD_REMOVE_FINGERPRINT` | |
| `0xB3` | `CMD_QUERY_FINGERPRINT_COUNT` | u32 count |
| `0xB4` | `CMD_GET_IMG_DATA` | 3 phases, 0x1FCC-byte chunks |
| `0xB5` | `CMD_DETECT_FINGERPRINT_PRESS` | |
| `0xB6` | `CMD_SLEEP` | |
| `0xC0` | `CMD_QUERY_MODULE_UID` | |
| `0xC1` | `CMD_QUERY_SENSOR_INFO` | 9 B: 4×u16 + flags |
| `0xC2` | `CMD_JTAG_CONTROL` | vendor only |
| `0xC3` | `CMD_RESTORE_MCU` | vendor only |
| `0xD0` | `CMD_UPDATE_FIRMWARE` | **never run this** |

> **Correction (2026-11-09).** This table used to be shifted by one (it had
> `0xA1` = firmware version, `0xB0` = root key, …) because it was reconstructed
> from the *order* of the driver's trace strings. The values above come from the
> driver's actual dispatch table at RVA `0x46F00` and are cross-checked against
> both the functions that emit each byte and the handlers the table binds to
> each row - see [Driver reverse-engineering](#driver-reverse-engineering-2026-11-09).

The driver also carries a project table ("BOE MOC", "HUAHONG MOUSE MOC",
"DYW MOH", "NATION TEST", "NATIONALCHIP") keyed by the firmware answer's
project bytes, and the hash names **SM3, MD5, SHA-1**. Treat template handling
as out of scope for a first driver: enrol storage stays on the module, the host
only needs the handle.

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

Device -> host: the answer comes back in the **same** shape. The driver's
answer dispatcher reads the opcode at offset 22, the phase at 23, the payload
length at 27 and hands the handler `frame + 0x1F` with that length, so the
answer body starts at offset 31 exactly like a request payload. The receive
path also checks byte 0 for `0xAA` and verifies the same CRC-32 over `L + 1`
bytes (everything before the CRC), so an answer is *not* unchecked.

The 16 bytes at offset 5 are the per-frame crypto nonce, not a fixed reserved
area: a request fills them with zeros for a plaintext command and with 16
random bytes (plus SM4-encrypted command block) for a secure one. See
[Driver reverse-engineering](#driver-reverse-engineering-2026-11-09).

### Image transfer (for reference; a normal driver does not need it)

`CMD_GET_IMG_DATA` runs in three phases: phase 0 returns a record with
`status:u8` at 0, `total_bytes:u32` at 4 and `byte_sum:u32` at 0x14 (the driver
consumes 0x18 bytes); phase 1 streams chunks that the driver appends and counts
(the chunk request's `arg:u16` is the running offset, the request's `len` is
`0x1FCC`); phase 2 cancels. Acceptance checks are `total_bytes == width * height`
and an **8-bit running sum** of the image equal to the announced `byte_sum`.
Each chunk is a complete `0xAA` frame and therefore carries the normal CRC-32;
there is no *additional* per-chunk sum. Per-attempt timeouts are 1000 ms
(2000 ms for cancel) with 2030/10010 ms totals.

### Timing / interrupts

After sending a frame the driver resets an event, sends, then
`WaitForSingleObject`s on it and re-sends the frame if the wait expires. The
completion is event-driven; the event is set from the driver's completion path
(several `SetEvent` sites, one per transfer state), and the INF's
`settings\ConnectInterrupt=1` plus the ACPI `GpioInt` resource are the static
evidence that the signal arrives as a GPIO interrupt. A Linux driver must
request that GPIO itself - `spi-intel`/`pxa2xx` will not do it - which is
exactly why the ACPI `_CRS` matters (see open points). Which edge/level and
whether the line is shared is **not** provable statically.

### Firmware in the driver package

`.rsrc` type `BIN` holds four blobs; `0x90` and `0x91` (242 116 / 242 124 B)
are N32G4FR images (`SP = 0x2001D538`, reset `0x0800A1AD` -> flash at
`0x08000000`), `0x95` is a different MCU (`reset 0x1001A405`), `0x8F` is 39 KiB
of something else. These are vendor firmware and are **not** copied into this
repo; nothing here writes firmware.

## Driver reverse-engineering (2026-11-09)

Static analysis of the shipped package only: no vendor code is reproduced here,
no firmware or DLL is in this repo, and nothing below was executed. Method:
`tools/ghidra-pod.sh` (private headless Ghidra project) dumping all 805 functions
of `BetterlifeFingerprintDevice.dll`, plus targeted byte searches for constants.
Addresses are RVAs of that DLL; `1800xxxxx` = `0x180000000 + RVA`.

### What the package actually is

| file (X64) | role | evidence |
|---|---|---|
| `BetterlifeFingerprintDevice.inf` | UMDF **2.15** function driver, no `.sys` | `UmdfLibraryVersion=2.15.0`, `ServiceBinary=%12%\UMDF\...`, `AddService=WUDFRd` |
| `BetterlifeFingerprintDevice.dll` | the UMDF driver: **both** transports (USB and SPI/SPB, chosen at `EvtDevicePrepareHardware`), framing, crypto, command state machines; see [`fingerprint-transport.md`](fingerprint-transport.md) | 7 sections, WPP tracing, imports only `ntdll/KERNEL32/ADVAPI32/WppRecorderUM` |
| `BetterlifeEngineAdapter.dll` | WBDI engine adapter (matching policy) | INF `EngineAdapterBinary` |
| `BetterlifeSensorAdapter.dll` | WBDI sensor adapter (capture) | INF `SensorAdapterBinary` |
| `BetterlifeStorageAdapter.dll` | WBDI storage adapter (template DB) | INF `StorageAdapterBinary`; also carries SM2/SM3 constants |
| `betterlifefingerprintdevice.cat` | signature catalogue | DER PKCS#7 |

The INF binds `USB\VID_347D&PID_0302/0304` via WinUSB and `ACPI\BF63112`,
`ACPI\BF61100`, `ACPI\BF63100` as SPB devices; it registers WinBio configuration
`{FD2B9BF2-F4AC-4ADB-82D4-E4661C439A40}` with database format
`82BD732E-4C65-4B4C-A6A6-9954769D32FF`, `BiometricType=0x8` (fingerprint). Its
registry keys are only `settings\ConnectInterrupt=1` and `settings\AdapterInfo=0`
plus power/idle policy - **there is no SPI clock, mode, bit width or GPIO number
anywhere in the package**. Those come from the ACPI `_CRS` of \_SB.PC00.SPI1.FPRT, so
the open point below is unchanged and still needs root on this host.

The driver never imports a crypto DLL: SM2/SM3/SM4 are statically linked from
**GmSSL** (path strings `...\encrypt\gmssl\sm2_alg.c`, and the SM2 curve table
exists in the image). That is the "SM3 layer".

### Framing (proven) - recovered from the **USB** implementation

> **Scope correction (2026-11-09).** Everything in this subsection is reachable
> only from the driver's USB branch: `FUN_180004358`'s only transfer primitive
> is `FUN_18001B6E4`, which writes to the USB out-pipe at device-context
> `+0x118` through `WdfUsbTargetPipeWriteSynchronously`, and `FUN_18001AC60` is
> the USB continuous-reader completion routine. The ACPI/SPI branch used on
> this host (`FUN_180015acc`) never calls either and references neither CRC
> table; it sends a one-byte selector plus payload and waits for its interrupt.
> So: **the frame format below is real, but it is not yet proven to be the SPI
> wire format.** See [`fingerprint-transport.md`](fingerprint-transport.md) §5.

| fact | provenance |
|---|---|
| one USB bulk write per frame; out-pipe handle at device-context `+0x118`; 200 ms transfer timeout (constant `-2000000` in 100 ns units) | `FUN_18001B6E4` |
| frame built from a descriptor: `+0x00` 16-byte nonce, `+0x10` opcode, `+0x11` phase, `+0x12` arg:u16, `+0x14` len:u32, `+0x18` data ptr, `+0x20` wait flag, `+0x24` secure-frame flag, `+0x2c` per-attempt ms, `+0x30` total ms | `FUN_1800028F8` (block builder), `FUN_180004358` (send/wait) |
| CRC-32 (reflected `0xEDB88320`) over everything before the CRC; table at `0x180046030` | `FUN_180004358` |
| retry loop: `ResetEvent`, send, `WaitForSingleObject(event, +0x2c)`, re-send until `+0x30` elapses | `FUN_180004358` |
| receive: requires byte 0 `== 0xAA`, reads `L` from `+1`, waits for `5 + L` bytes, CRC-32 over `L+1` bytes with a second table at `0x180047120`, reassembly cap `0x16829` B, 2000 ms reassembly gap timeout | `FUN_18001AC60` |
| answer dispatch: opcode `answer+0x16`, phase `+0x17`, length `+0x1B`, body `answer+0x1F` | `FUN_18001AADC` |
| command table at `0x46F00`, stride `0x20`, fields `{opcode:u64, name:char*, handler:fn, label:char*}` | table dump + `FUN_18001AADC` |

Per-command timeouts in the descriptor: 1000/2030 ms for ordinary queries,
5000/10010 ms for the GET_IMG data phase, 2000/4016 ms for the vendor restore.

### Session / root-key layer (proven)

The layer sits *above* the transport - the answer handler and the derive step
are shared by both branches - so the crypto result below is transport
independent. It exists, it is an ephemeral SM2 exchange, and it is now
recovered down to the byte: no fixed key, no DPAPI blob, and no ambiguity left
in the KDF or the tag. The Python implementation
(`tools/fingerprint/fpbf63112/crypto.py`) reproduces it step by step and is
vector-tested; the driver-side C equivalent is
`tools/fingerprint/bf63112_session_openssl.c`.  It is a standalone compile
check: the SPI driver does not wire it, because the handshake starts with a
0xA2 write and no write opcode is on the SPI allowlist.

1. At open the driver runs `0xA0` (fw version) -> `0xA1` (scheme) ->
   **key generation** -> `0xA2` (root key) -> `0xC0` (UID) -> `0xC1` (sensor
   info) (`FUN_180019764`).
2. `FUN_180007C28` generates an **SM2 keypair per open**: a random scalar `d`
   in `[1, n-1]` (retried while `d` is zero or `>= n`), `P = [d]G`; the key
   blob is `tag 0x04 @+0 | X @+1 | Y @+0x21 | d @+0x41`, all 32-byte
   big-endian (`FUN_180008024` serialises BIGNUM limbs big-endian), i.e. the
   tag byte sits one byte before the 64-byte public point.
3. `FUN_180001A3C` sends `0xA2` with a **65-byte** payload from that blob
   (`len = 0x41`) and sets the descriptor's **secure-frame flag** (`+0x24 = 1`).
   The descriptor's 16-byte header is zero-filled there and the sender
   (`FUN_180004358`) replaces it with 16 random bytes whenever the flag is set.
4. The answer handler `FUN_1800185A0` requires **>= 0x71 bytes** and copies
   `status:u8 @0 | device point 64 B @1 | SM3 tag 32 B @0x41 | klen:u32 LE
   @0x61 | wrapped @0x65` (the vendor answer carries `klen = 0x10`).
5. `FUN_180007AA0` loads the device point as a Jacobian point
   (`FUN_180008ED8`), computes `S = [d]P_dev` (`FUN_1800091E4`: 256-step
   double-and-add over the scalar's bits), converts to affine
   (`FUN_180008F34`) and serialises the shared point as `X || Y` big-endian
   (`FUN_1800093F8`) - a **64-byte** KDF input.
6. **KDF** (`FUN_18000945C`): for `ctr = 1, 2, ...` compute
   `SM3(S || BE32(ctr))`, take up to 32 bytes per block and stop at `klen`.
   That is exactly the GM/T 0003.4 counter KDF, with the counter a 32-bit
   big-endian integer starting at 1. An all-zero result is rejected
   (`FUN_180007D08` returns "all zero" -> error).
7. **Unwrap**: `key = mask xor wrapped`, in place (`FUN_180007A6C`), `klen`
   bytes.
8. **Tag**: `SM3(X || key || Y)` - X first, then the derived key, then Y (the
   three `FUN_18000B544` updates in `FUN_180007AA0`) - compared with the
   answer's 32-byte tag by `memcmp`. Only when it matches is the 16-byte key
   stored at device-context `+0x16A58`.

So the session key is an ephemeral ECDH result; there is no static secret to
extract, and the only long-lived key material is inside the MCU.

**What is proven vs. assumed.** The chain above, the offsets, the KDF
construction, the tag construction and the SM4 frame transform are proven from
the driver's code, and the primitives are pinned to published GB/T vectors by
the test suite (SM3 `abc` + the 64-byte vector, SM4 ECB, the GB/T 32918 sample
keypair, KDF definition, ECDH symmetry). What remains *assumed* is only what
static analysis cannot show:

* that the module accepts a **plaintext** `0xA2` - the vendor sends it as a
  secure frame and, with no session key yet, that frame is transformed with the
  all-zero initial key, which only works if the context is zeroed at open.
  A live run settles it in one attempt;
* that the device answers with `status = 0` and `klen = 16` (both are what the
  vendor code path implies; the C module refuses anything else on purpose);
* that the point it returns is on `sm2p256v1` - the Python decoder rejects
  anything else loudly, which is itself a useful diagnostic;
* whether the **SPI/SPB branch** applies the same secure-frame transform as the
  USB branch that this was recovered from (see
  [`fingerprint-transport.md`](fingerprint-transport.md) §5). A plaintext
  driver does not need the transform at all, so this only matters once the
  session is used for `0xB0`/`0xB1`.

### The frame cipher (proven for the USB branch)

`FUN_180004358` (send) and `FUN_18001AC60` (receive) are symmetric after all -
the earlier note that the two paths derived the per-frame value differently was
a misreading of `FUN_18000BFD0`, which is the **SM4 decryption key schedule**
(it writes the 32 round keys in reverse order), not an IV load:

```
H        = 16 random bytes per frame          (the header / nonce)
H'       = SM4_encrypt (K_session, H)         (one ECB block)
C_i      = SM4_encrypt_block (H', P_i)        for each 16-byte block
receive:   P_i = SM4_decrypt_block (H', C_i)
```

`P` is the *aligned command block* (`0x4C op phase status arg len data pad`,
`L - 20` bytes). The CRC-32 is computed over the **ciphertext**, which is why
the receiver checks the CRC before it decrypts and why a secure frame cannot be
structurally parsed until it has been deciphered. `FUN_18000CE30` is the
encryption key schedule, `FUN_18000B620` one block, `FUN_18000B3B4`/`B4EC`/
`B544` are SM3 final/init/update, and the module's S-box at `0x38550` matches
GB/T 32907's S-box byte for byte. Both functions belong to the USB branch, so
the SPI branch's use of the same transform is not proven (see above).

### Answer layouts (per command)

| command | handler | body layout (offsets inside the payload) |
|---|---|---|
| `0xA0` fw version | `0x1800181B0` | `+0` state:u8, `+1`/`+2` project, `+3` version:u32; min 7 |
| `0xA1` scheme | `0x1800183B0` | scheme table, no status byte |
| `0xA2` root key | `0x1800185A0` | `+0` status, `+1` point 64 B, `+0x41` tag 32 B, `+0x61` klen, `+0x65` wrapped; min `0x71` |
| `0xB0` enrol | `0x180018730` | phase 0: `+0` status, `+1` byte; phase 1: empty = ack; phase 2 ("Over"): `+0` status, `+1` u16, `+3` 16-byte handle; min 19 |
| `0xB1` identify | `0x1800189A0` | phase 0: `+0` status, `+1` u16, `+3` 16-byte handle; min 19 |
| `0xC1` sensor info | `0x180019420` | `+0` u16, `+2` u16, `+4` **width**, `+6` **height**, `+8` flags:u8; min 9 |
| `0xB4` get image | `0x180018DC0` | phase 0: `+0` status, `+4` total:u32, `+0x14` sum:u32, min `0x18`; chunks requested with arg = running offset, max `0x1FCC` |

The template "handle" is 18 bytes: a `u16` plus 16 opaque bytes; the host never
sees the template. Width/height are confirmed by the image path multiplying the
u16 at `+4` by the u16 at `+6` and comparing with the announced total.

**GET_IMG chunks do carry integrity**, contrary to an earlier note: each chunk is
a full `0xAA` frame, so it is covered by the per-frame CRC-32, and the whole
image is checked against the 8-bit sum from phase 0. There is no additional
per-chunk sum field.

## Verdict

* **Supported today: no.** No kernel driver, no libfprint driver, no udev
  binding, no `spidev` node. Biometric login on this unit is not possible and
  no amount of configuration changes that.
* **Supportable: yes, with real work.** The transport on this unit is plain
  SPI, the command set is known, and libfprint already has both the SPI
  transport and match-in-sensor driver archetypes (`fpi-spi-transfer.c`,
  `elanspi`, `egismoc`/`focaltech_moc`). The frame format is known but was
  recovered from the driver's **USB** branch and is not yet proven on the SPI
  link - see [`fingerprint-transport.md`](fingerprint-transport.md) §5, which is
  now a prerequisite, not a detail.
* **Blocked by, in order:**
  1. **No driver exists** and the vendor publishes nothing - a BF63112 driver
     has to be written from scratch (see `tools/fingerprint/libfprint-bf63112.c`).
  2. **SPI bus parameters unknown** - not in the driver package (checked: the INF
     carries no clock/mode/GPIO), so they are in the ACPI `_CRS`/`_DSD`. Root
     required to read them; wrong values return `0xFF` forever.
  3. **Reset/interrupt GPIOs unknown** - `GpioIo`/`GpioInt` entries in
     `\_SB.PC00.SPI1.FPRT._CRS`; without an IRQ the driver can only poll.
  4. **The module may insist on the session key for enrol/identify** - the
     vendor driver always establishes it at open and sets the secure-frame flag
     on `0xA2`/`0xB0`/`0xB1`. Whether the firmware *rejects* plaintext enrol is
     a live-hardware question (see next steps).
  5. **Enrol/identify payload semantics are only structurally known** - the
     handle (18 B) and the status byte are proven, the meaning of the u16 inside
     the handle and any score are not, because the driver hands results to WBDI
     rather than parsing them further.

The answer framing, command map and session design are no longer blockers: they
are decoded above. Cheapest useful next step is still step 1 below, reading the
ACPI resources with root, because without the clock/GPIO numbers even a perfect
driver cannot start.

## Artifacts in this repo

| file | what it is |
|---|---|
| `tools/fingerprint/oxp3-fp-probe.py` | bring-up probe. Default `--transport spi` sends the recovered selector + two-entry SPB transaction (`--spidev` and `--allow-writes` required for I/O); `--dry-run` prints exact plans with no I/O; `acpi_info --crs FILE` parses an extracted `_CRS` offline. `--transport frame` keeps the old `0xAA`/CRC experiment. Refuses destructive opcodes by opcode *and* selector. Stdlib only. |
| `tools/fingerprint/fpbf63112/matrix.py` | the `--try-matrix` planner/runner (reset polarity x settle x IRQ edge), the `--try-write-shape` planner/runner (selector bit 0 x padding x read length) and the `--try-link` planner/runner (SPI mode x clock x settle, with plausible-fw/plausible-info/byte-shift-detected/all-zeros/all-0xff/timeout/error/malformed verdicts), the `--compare-read-shapes` planner/runner (split vs cs-held with best -7..+7 bit-shift analysis), valid-answer parsers with CRCs and candidate-offset shift analysis, JSON reports; transport-agnostic so tests use a fake transport |
| `tools/fingerprint/fpbf63112/spi.py` | recovered SPI/SPB transport: selector computation, two `spi_ioc_transfer` entries per command, recovered 1000/2030 (5000/10010 image, 2000/4016 restore) timeouts, IRQ waiters (gpiod v1/v2, sysfs, poll), reset pulse, cancel settle, and the allowlist/destructive gates |
| `tools/fingerprint/fpbf63112/acpi.py` | raw `_CRS` resource-template parser for `SpiSerialBus`, `GpioInt`, `GpioIo`, I2C and IRQ items; validates lengths/offsets and reduces them to `SpiConnection` (speed, mode, bits, CS, IRQ, reset) |
| `tools/fingerprint/fpbf63112/aml.py` | finds and validates the FPRT `_CRS` resource template in the raw DSDT AML, so the runbook does not depend on `iasl` or on copying bytes out of ASL by hand |
| `tools/fingerprint/oxp3-fp-session.sh` | the guided, reversible root runbook: preconditions, `_CRS` extraction + parse, one confirmation per mutation, guarded read-only probe, rollback on exit/error/Ctrl-C with verification, transcript + `summary.json`; `--dry-run`, `--non-interactive`, `--resume`; docs in [`fingerprint-session.md`](fingerprint-session.md) |
| `tools/fingerprint/fpbf63112/` | protocol library (stdlib only): frame build/parse, CRC-32, offset arithmetic, command table, GET_IMG reassembly, retry state machine, `Session`/`open_session` |
| `tools/fingerprint/fpbf63112/crypto.py` | session crypto (stdlib only): SM3, SM4 (ECB/CBC), SM2 over sm2p256v1, GM/T 0003 KDF, the 0xA2 handshake, the secure-frame transform, a bounded variant search, vector self-test and benchmark |
| `tools/fingerprint/bf63112_session_openssl.c/.h` | the same handshake in C via OpenSSL (EVP SM3/SM4 + `EC_POINT_mul` for the shared point); standalone compile check, not wired into the SPI driver (0xA2 is a write and is not allowlisted) |
| `tools/fingerprint/tests/` | 388 unittest tests incl. fuzz/property round-trips, synthetic device transcripts, the SM2/SM3/SM4 vectors and a synthetic end-to-end handshake, `test_driver_derived.py` (layouts recovered from the driver), `test_spi.py` (spidev ABI, selectors, timeouts, IRQ/reset, gates, probe dry-run/refusals) and `test_acpi.py` (constructed `_CRS` buffers and malformed cases); shared frame vectors for the C implementation |
| `tools/fingerprint/bf63112_proto.h/.c` | dependency-free C11 protocol core shared with the driver |
| `tools/fingerprint/libfprint-bf63112.c` | libfprint driver: selector/two-entry SPI transport, open/close/suspend/resume, 0xC1 sensor info and 0xA0 firmware query, optional reset/IRQ from the environment; enrol/verify/identify refuse with `NOT_SUPPORTED` until the payloads are decoded. Syntax-checked (`-Wall -Wextra -Werror`) against libfprint v1.94.9 headers; see `tools/fingerprint/README.md` |
| `tools/fingerprint/README.md` | proven vs inferred vs unknown, test commands, privileged probe steps, upstreaming path |
| `docs/hw/fingerprint-transport.md` | USB-vs-SPI transport RE: how the driver chooses, both stacks, per-SKU verdict, what each path needs |
| `tools/fingerprint/usb_probe.py` | read-only 347D enumerator (pyusb if present, else sysfs); reports interfaces/endpoints/driver claim, sends no vendor command |
| `docs/hw/fingerprint.md` | this document |

The Python suite, the C tests and both driver syntax checks pass offline (see
`tools/fingerprint/README.md`), but the probe has **not** run against the
sensor: it needs root to bind `spidev`, which this session did not have. Treat
its first run as a bring-up experiment.

## Bringing it up by hand (privileged steps, exact commands)

> **Prefer the runbook.** `sudo tools/fingerprint/oxp3-fp-session.sh` performs
> steps 1-5 below, asks before each mutation, records what was bound before it
> changed anything, restores it on exit, on error and on Ctrl-C, verifies the
> restore and writes a transcript + `summary.json`. The manual sequence below
> remains as the explanation of what the runbook does, and as the fallback if
> the script cannot run. See [`fingerprint-session.md`](fingerprint-session.md).

Nothing below is executed by this repo; they are the commands to run yourself.
Steps 1, 2, 4 and 5 are read-only; step 3 is the only state change (the
`spidev` bind) and it is reversible.

1. **Read the ACPI resources** (the numbers a driver needs):

   ```bash
   sudo cat /sys/firmware/acpi/tables/DSDT > /tmp/dsdt.aml
   sudo dnf install acpica-tools          # for iasl
   iasl -d /tmp/dsdt.aml
   grep -n -A 40 'FPRT' /tmp/dsdt.dsl | less
   ```

   Look for `SpiSerialBus (... , SpiMode, SpiBitWidth, ConnectionSpeed ...)`
   and the `GpioInt`/`GpioIo` entries with their `PinIndex`/`GpioIo` numbers.
   Save the bytes of the matching `ResourceTemplate ()` as binary (the hex
   dump from `iasl`), e.g. `printf '%s' '8e 21 00 ...' | xxd -r -p >
   /tmp/fprt-crs.bin`; the offline parser in step 4 consumes that file.
   `ACPI\BF63112:00` may also carry a `_DSD` with `spi-max-frequency`.

   Checked: the kernel exposes **nothing** of this without root - the ACPI node
   has only `hid/modalias/path/physical_node/status/uevent`, `udevadm info`
   reports just `DEVPATH`/`MODALIAS`/`SUBSYSTEM`, and the SPI device has no
   `resource`/`properties` file. So step 1 is genuinely first.

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

4. **Parse `_CRS` offline, then inspect the plan** (no device access):

   ```bash
   python3 tools/fingerprint/oxp3-fp-probe.py acpi_info --crs /tmp/fprt-crs.bin
   python3 tools/fingerprint/oxp3-fp-probe.py --crs /tmp/fprt-crs.bin \
       --spidev /dev/spidev0.0 --dry-run fw info uid count
   ```

   `acpi_info` prints the `SpiSerialBus` speed/mode/bit length/CS and the
   `GpioInt`/`GpioIo` pins. The dry run prints the exact selector and
   two-entry transfer for each read; it never opens `/dev/spidev*` or a GPIO.

5. **Run the read-only probe** (explicit device path and `--allow-writes` are
   both mandatory; the SPI transport is write-gated even for reads):

   ```bash
   sudo python3 tools/fingerprint/oxp3-fp-probe.py --crs /tmp/fprt-crs.bin \
       --spidev /dev/spidev0.0 --allow-writes --gpio <GpioInt pin> \
       --irq-edge rising --irq-active-low fw info uid count
   ```

   Start with `fw` (`0xA0`) and `info` (`0xC1`); `0xA1` and the count/UID reads
   follow. A healthy `fw` answer is 7 bytes and `info` 9 bytes. All-`0xFF`/no
   answer almost always means wrong SPI mode or too high a clock, or that the
   device expects the IRQ first. `cat
   /sys/bus/spi/devices/spi-BF63112:00/statistics/bytes_tx` tells you whether
   the transfers happened at all, even when the answer is garbage. The reset
   line is only pulsed with `--reset-gpio N --allow-reset`, using the settled
   `--reset-polarity active-high --settle-ms 10`. After a shifted answer (the
   2026-11-09 run: `00 00 00 ff 7f 50 03` for `0xA0` vs `00 00 ff 7f 50 03`
   for `0xA1`), run the one-command write-shape experiment:
   `sudo tools/fingerprint/oxp3-fp-session.sh --try-write-shape --irq-pin 18
   --reset-pin 16 --gpio-chip gpiochip1 --reset-polarity active-high
   --settle-ms 10 --irq-edge rising --allow-reset` (guide:
   [fingerprint-transport.md §8.8](fingerprint-transport.md#88-the-try-write-shape-guide)).
   For the 2026-11-09 readable-but-implausible answers (`0x7F50`, `0xC1` =
   `32 32 ...`), sweep the link instead with the reset settings pinned:
   `sudo tools/fingerprint/oxp3-fp-session.sh --try-link --irq-pin 18
   --reset-pin 16 --gpio-chip gpiochip1 --reset-polarity active-high
   --irq-edge rising --allow-reset` (guide:
   [fingerprint-transport.md §8.10](fingerprint-transport.md#810-the-try-link-sweep-mode-x-clock-x-settle)).
   If the per-mode answers scale by two (a phase artefact), compare the split
   and cs-held transfer shapes directly:
   `sudo tools/fingerprint/oxp3-fp-session.sh --compare-read-shapes --irq-pin
   18 --reset-pin 16 --gpio-chip gpiochip1 --reset-polarity active-high
   --settle-ms 10 --irq-edge rising --allow-reset` (guide:
   [fingerprint-transport.md §8.11](fingerprint-transport.md#811-the-try-compare-read-shapes-run-split-vs-cs-held)).

## Safety

* **No firmware writes, no JTAG, no `RESTORE_MCU`.** `0xB0`/`0xB1`/`0xB2`/
  `0xC2`/`0xC3`/`0xD0`/`0xD1`/`0xFF` are refused unconditionally, both by
  opcode and by the selector they produce (the SPI selector keeps only six
  opcode bits, so `0x02` aliases JTAG `0xC2`). `--allow-op` can add one raw
  *read* opcode but can never override this.
* **No template deletion.** `0xB2`/`0xB1` are not allowlisted; fingers
  enrolled in Windows stay untouched.
* **Two independent opt-ins.** Even a read needs `--spidev /dev/spidevN.M`
  (no default path) and `--allow-writes`; no write opcode is allowlisted by
  default. `--dry-run` and `acpi_info` perform no device I/O and are safe
  unprivileged.
* **SPI is low risk by nature**: wrong mode/clock produces garbage reads rather
  than damage, and the module is powered by the platform (no relay/destructive
  side effects). The one non-read action, a reset pulse, needs
  `--allow-reset` explicitly.
* **Do not run the vendor's Windows firmware update** against this module from
  Linux under any wrapper.
* Binding `spidev` takes the device from any future in-tree driver; do it on a
  machine you are experimenting on, not one you depend on for login. It does
  not affect the touchpad, keyboard, display or audio.

## Rollback

`tools/fingerprint/oxp3-fp-session.sh` does this itself (from its `EXIT` and
`INT/TERM/HUP` traps) and then verifies it; the commands below are the manual
equivalent and what the runbook prints at the end of a plan.

```bash
echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/unbind
sudo rm -f /sys/bus/spi/devices/spi-BF63112:00/driver_override
sudo modprobe -r spidev
```

Nothing else on the host is modified: no packages, no config files, no
firmware, no boot parameters. Deleting `tools/fingerprint/` returns the repo to
its previous state.

## Open points

Status after the driver RE: items 2 and 4 below are now answered statically;
what is left needs the hardware.

1. **SPI clock, mode, bit width and the reset/IRQ GPIOs** - not in the driver
   package (the INF only has `ConnectInterrupt=1`), so they are in
   `\_SB.PC00.SPI1.FPRT._CRS`/`_DSD`; root-only on this host, so not read here.
   `acpi_info --crs` now parses them when supplied; without `_CRS` the probe
   passes no speed/mode/bits and leaves the controller defaults alone rather
   than guessing.
2. ~~Answer framing~~ - **closed.** Answer = same layout as a request: `0xAA`,
   `L`, 16-byte nonce, `4C`, opcode@22, phase@23, len@27, body@31; CRC-32 is
   verified by the driver on receive.
3. **Whether the module enforces the session** - the vendor driver always runs
   `0xA0 -> 0xA1 -> 0xA2 -> 0xC0 -> 0xC1` at open and marks `0xA2`/`0xB0`/`0xB1`
   as secure frames. A plaintext `0xB0` (enrol start) is the cheapest probe: a
   status byte other than 0 or a `0xAA` answer with an error body means the
   module wants the handshake first.
4. ~~Session design~~ - **closed, including the frame cipher.** SM2 ECDH with a
   per-open ephemeral driver key, GM/T 0003 KDF over the 64-byte shared point,
   `SM3(X || key || Y)` tag, `key = KDF xor wrapped`, and SM4-ECB under
   `H' = E_K(H)` for the secure frames. What stays open is only what needs the
   module (see the session experiment below).
5. **`BF63112:01` on SPI2** - present in ACPI, no controller instanced. Either
   an unpopulated second source or a phantom node; harmless, but a driver
   should match `BF63112:00`-style enumeration rather than assume one device.
6. **The probe has never run** - the SPI transport, ACPI parser and CLI are
   implemented and offline-tested (388 tests), and the first execution is the
   bring-up itself; expect to tune `--speed`/`--mode`/`--irq-edge` and to
   discover whether the device clocks the answer in the first transfer or
   raises `GpioInt` first.
7. **The driver has never talked to the sensor either** - it compiles against
   libfprint 1.94; its sensor-info and firmware offsets now match the vendor
   handler, but the retry timing and the real answers still need one hardware
   run. Enrol/identify stay `NOT_SUPPORTED` on purpose.

### Exact next steps for a maintainer (scope + probe)

All of these are privileged and read-only except the last, and none is run by
this repo.

1. **ACPI resources** (`sudo`, no state change):

   ```bash
   sudo cat /sys/firmware/acpi/tables/DSDT > /tmp/dsdt.aml
   iasl -d /tmp/dsdt.aml && grep -n -A40 'FPRT' /tmp/dsdt.dsl
   ```

   Record `SpiSerialBus (..., SpiMode, SpiBitWidth, ConnectionSpeed, ...)` and
   the `GpioInt`/`GpioIo` `PinIndex` values. Those four numbers are the only
   missing init data for a driver.

2. **Bind `spidev` and run the read-only probe** (see "Bringing it up by hand"),
   then `info`, `fw`, `uid`, `count`. This settles the geometry, the firmware
   version and the count, and proves the CRC/answer assumptions on real bytes.
   `bytes_tx` in the SPI device statistics confirms transfers happened even when
   the answer is garbage.

3. **Session probe** (`0xA2`), only if step 2 answers: send the 65-byte payload
   the vendor driver sends (tag `0x04` + a throwaway SM2 point) and confirm the
   ≥113-byte answer shape. A single attempt is harmless - it is a key exchange,
   not a write - but it does require an SM2 implementation to be useful.

4. **Enrol probe** (`0xB0`, phase 0, 1-byte payload): the decisive test for
   whether plaintext enrol is accepted. Expect a status byte; no template is
   created by a phase-0 start, and it can be abandoned without a cancel.

### The session experiment, step by step (what only hardware can settle)

Every step is read-only with respect to stored templates and firmware; `0xA2` is
a key exchange. Nothing in this repo has been run against the sensor.

1. **Prove the code offline first** (no device, no root):

   ```sh
   python3 tools/fingerprint/oxp3-fp-probe.py --crypto-selftest
   python3 -m unittest discover -s tools/fingerprint/tests
   ```

   The self-test checks SM3 (`abc`, the 64-byte vector), SM4 (GB/T 32907 ECB),
   the GB/T 32918 sample keypair and ECDH symmetry, then benchmarks 4 KiB.

2. **Plaintext `0xA2`** once the transport answers a normal query:

   ```sh
   sudo tools/fingerprint/oxp3-fp-probe.py --session --raw-info
   ```

   In the answer, look for opcode `0xA2` with **>= 0x71** payload bytes and
   check the 16-byte header: zero means the module answered in plaintext,
   non-zero means it enciphered the answer. The probe prints the raw answer and
   `Session` already tries to decipher a non-zero header with the initial key.

3. **If the tag does not verify**, the probe runs the bounded variant search
   (`crypto.search_variants`: 32 candidates - KDF over `X||Y` vs `X`, four tag
   layouts, `klen` 16 vs 32) against the captured answer and names the first
   matching parameter set. If nothing matches, keep the raw answer: that is
   evidence the exchange is not the recovered one.

4. **If a plaintext `0xA2` gets no answer at all**, try `--session-secure`: it
   seals the request with the initial zero key exactly as `FUN_180004358`
   would with an empty session slot.

5. **Then the decisive command test**: a plaintext `0xB0` start. A status byte
   other than 0 means the module wants the session (or an authenticated
   frame); that is the point where the derived key has to be used for real.

6. **Record** the raw answer, the firmware/sensor ids and the outcome. Any one
   of these observations is worth more than another static pass: whether the
   first `0xA2` had to be enciphered, the `klen` value, the tag variant, and
   whether answers carry a non-zero header.

Do **not** send `0xC2` (JTAG), `0xC3` (restore MCU) or `0xD0` (firmware
update): all three are in the driver's table as vendor-only operations.
