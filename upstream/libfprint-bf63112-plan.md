# Plan draft — a libfprint driver for the Betterlife BF63112 SPI fingerprint sensor

Status: **plan + existing prototype, not submitted**. No account or merge-request permissions for
libfprint are available here, and the driver has **never talked to the sensor**: it is a
compile-checked prototype whose transport, framing, CRC and retry state machine are implemented and
tested offline, while enrol/verify/identify deliberately refuse. This document is the
maintainer-facing plan: what is proven, what is missing, how the driver would be shaped, and
exactly what evidence is needed before a merge request is worth opening.

Prepared: 2026-11-09. Device: ONEXPLAYER 3, ACPI `BF63112` at `\_SB.PC00.SPI1.FPRT`, SPI
`spi-BF63112:00`, no in-tree driver, libfprint 1.94.100 / fprintd 1.94.5 on the unit. Full hardware
evidence: [`docs/hw/fingerprint.md`](../docs/hw/fingerprint.md); prototype details:
[`tools/fingerprint/README.md`](../tools/fingerprint/README.md).

---

## 1. Summary

The sensor is a **match-in-sensor** module on the Intel LPSS SPI bus. It is not USB, has no vendor
Linux driver and no public protocol documentation. The Windows UMDF driver
(`BetterlifeFingerprintDevice.dll` 9.39.15.26) was reverse-engineered as untrusted data; the framing,
CRC, command table, image transfer and retry behaviour were recovered and reimplemented in our own
code. What is missing is everything above the transport: the answer-header layout, the SM3/session
layer, and the enrol/identify payloads. Those are exactly the pieces that cannot be invented; they
need one bring-up session against real hardware.

The proposed upstream shape is a normal libfprint driver: one `drivers/bf63112.c` plus a small
dependency-free protocol core, an entry in the existing `spi_drivers` / driver maps, a driver test
transcript, and a docs page. No kernel driver and no new kernel ABI are needed: the sensor is
`spidev`-shaped and libfprint already has the SPI transport and match-in-sensor archetypes
(`elanspi`, `egismoc`, `focaltech_moc`).

## 2. Protocol summary (what the driver implements today)

### 2.1 Host frame

Recovered byte-exactly from the Windows driver and reimplemented; this is proven against the
driver’s build/parse code:

```
off  len   field
0    1     0xAA                       start of frame
1    4     L, little endian           = 20 + align16(10 + data_len)
5    16    0x00 * 16                 reserved header (device answer area)
21   1     0x4C                       command magic 'L'
22   1     opcode                     command table below
23   1     phase (0 start, 1 data, 2 finish, 3 cancel)
24   1     0x00 in a request; status/parameter in an answer
25   2     arg, u16 LE
27   4     data_len, u32 LE           payload length, repeated
31   n     data
      pad   zeros to a 16-byte block boundary
end-4 4    CRC-32                       reflected 0xEDB88320, init 0xFFFFFFFF, final XOR
             (ISO-HDLC; identical to zlib.crc32, covers the whole frame incl. 0xAA)
```

A data-less query is 41 bytes with `L = 36`. `align16()` rounds the 10-byte command block plus
payload up to 16 bytes.

### 2.2 Commands

| opcode | command | role |
|---|---|---|
| `0xA1` | `CMD_QUERY_FIRMWARE_VER` | project code + 4 version bytes |
| `0xA2` | `CMD_QUERY_MODULE_SCHEME_INFORMATION` | module scheme |
| `0xB0` | `CMD_QUERY_ROOT_KEY` | chunked start/ing/over/cancel; crypto session |
| `0xB1` | `CMD_ENROLL_FINGERPRINT` | start / press / finish / cancel |
| `0xB2` | `CMD_IDENTIFY_FINGERPRINT` | module matches, returns handle + score |
| `0xB3` | `CMD_REMOVE_FINGERPRINT` | delete a template |
| `0xB4` | `CMD_QUERY_FINGERPRINT_COUNT` | number of stored templates |
| `0xB5` | `CMD_GET_IMG_DATA` | diagnostic image, 8-bit grayscale |
| `0xB6` | `CMD_DETECT_FINGERPRINT_PRESS` | press detection |
| `0xC0` | `CMD_SLEEP` | suspend |
| `0xC1` | `CMD_QUERY_MODULE_UID` | module UID |
| `0xC2` | `CMD_QUERY_SENSOR_INFO` | width, height, flags |
| `0xC3`, `0xD0`, `0xD1`, `0xFF` | JTAG / restore / firmware update / work-mode | **never sent by the driver**; `0xD1`/`0xC3`/`0xD0` are destructive |

The command names are the module’s own vocabulary; templates never leave the module.

### 2.3 Answer framing (inferred, must be confirmed on hardware)

The answer comes back in the same shape; the handlers see a body beginning with
`4C op phase status`. The **16-byte answer header is not decoded**. The prototype therefore exposes
both plausible body starts (`body_from_status`, `body_from_arg`) and checks the opcode only by
default, with phase checking optional. This is deliberately conservative: the plan is to record one
raw answer per opcode on hardware and then fix the layout, not to guess it in a merge request.

### 2.4 Image transfer (`0xB5`, diagnostics only)

* Phase 0 returns 9 bytes: `status`, `u32 total_bytes`, `u32 byte_sum`.
* Phase 1 streams chunks; the Windows driver asks for at most `0x1FCC` = **8140 bytes** per
  transfer and counts a `u16` chunk counter.
* Acceptance: `total_bytes == width * height` and an **8-bit running sum** over the image equal to
  `byte_sum`; there is no per-chunk CRC (a per-chunk sum is possible but unproven; the assembler
  supports either).
* Phase 2 cancels.

A normal driver does not need the image path; it is for diagnostics and for a future
`FP_DEVICE_TYPE_...` scan implementation.

### 2.5 Retry and timing

Recovered Windows behaviour: send one frame, wait for the answer, re-send on timeout, up to
**3 attempts**; the per-attempt bounds are `send` 1000 ms, `recv` 5000 ms, `total` 2030 ms,
`image_total` 8000 ms, `image_cancel_total` 10010 ms, `cancel` 2000 ms. The prototype implements
that state machine (`Transaction`: `READY → AWAITING_ANSWER → RESEND → SUCCESS/FAILED`) with
injectable time and frame scanners, so it is fully testable without hardware. Inter-attempt delays
are a call-site policy and are not assumed.

## 3. Test evidence (re-verified for this plan, no hardware)

Run from the repository root on 2026-11-09:

| check | command | result |
|---|---|---|
| Python protocol/transcript/fuzz suite | `python3 -m unittest discover -s tools/fingerprint/tests` | **85 tests, OK** |
| C protocol core against the same generated vectors | `tools/fingerprint/tests/run-c-tests.sh` | **324 checks, 10 vectors** |
| Driver vs real libfprint 1.94.9 headers | `LIBFPRINT_SRC=/tmp/libfprint-1.94.9 tools/fingerprint/tests/check-libfprint.sh` | **`syntax-real: OK`**, `-Wall -Wextra -Werror` |

Coverage in words: frame lengths/round-trips/padding; header junk scanning and resynchronisation;
every truncation and single-byte mutation; bad L/magic/data_len/CRC; 400 random valid-frame
round-trips; 400 random-buffer scans; image header/chunk accounting/overrun/global + optional
per-chunk sum; retry budgets and exhaustion over random event sequences; synthetic device
transcripts for clean/noisy/truncated/bad-CRC/wrong-opcode/retry-exhausted transcripts; shared
vectors generated from the Python implementation and rebuilt by the C implementation; probe/library
parity.

**Not tested:** anything on real hardware. No SPI transfer has ever been issued; the `sudo` probe
has never run (it needs root to bind `spidev`). The driver’s `open()` and answer parsing are
compile-checked, not validated against a device.

## 4. Remaining unknowns (in dependency order)

1. **SPI parameters**: clock, mode, bit width — in `\_SB.PC00.SPI1.FPRT._CRS`/`_DSD`, root-only on
   the unit (not read). Wrong mode/clock yields `0xFF` forever.
2. **Reset / IRQ GPIOs**: the module signals “answer ready” on a GPIO interrupt; `pxa2xx-spi` does
   not expose it, so the driver polls today. The `GpioInt`/`GpioIo` entries in `_CRS` are needed
   for a real IRQ-driven driver and for reset sequencing.
3. **Answer header**: the 16-byte device answer area and the exact body start. One raw query per
   opcode on hardware settles it.
4. **SM3 session / root key** (`0xB0`, `0xFF`): whether a plain-spoken driver is accepted at all, or
   whether the module insists on a key exchange before enrol/identify. A read-only `0xB0` attempt
   answers the first half.
5. **Enrol/identify payloads** (`0xB1`, `0xB2`): template handle, score, press counter, phase
   semantics and timing. This is the bulk of the remaining work; the host must only store the
   opaque handle (`FPI_PRINT_RAW`, `fpi_print_set_device_stored(TRUE)`).
6. **Sensor-info field offsets** (`0xC2`): the inferred `a:u16, b:u16, width:u16, height:u16,
   flags:u8` layout must be checked against one answer.
7. **Per-chunk sum vs global sum** for `0xB5`: affect diagnostics only.
8. **`BF63112:01` on SPI2**: present in ACPI, no controller instanced — a second source or a phantom
   node; a driver should match the enumeration rather than assume one device.
9. **Sleep/resume**: whether `0xC0` is required/answered, and whether the module needs a reset GPIO
   toggle on resume (no hardware evidence).
10. **Enumeration vs firmware**: the Windows driver carries firmware blobs (`0x90`/`0x91`, N32G4FR)
    and a firmware-update opcode. The Linux driver must never update firmware; identify the running
    version instead and document the mismatch risk.

## 5. Maintainer-facing plan (shape of the contribution)

### 5.1 Files

```
libfprint/drivers/bf63112.c          # driver: udev/spidev match, transport, open/close,
                                     # suspend/resume, SSM, enroll/verify/identify (later)
libfprint/drivers/bf63112_proto.c    # dependency-free frame/CRC/parse/retry core (optional split;
libfprint/drivers/bf63112_proto.h    #  keeps the driver readable and the tests independent)
tests/bf63112/device-spi            # transfer transcript, same format as tests/elanspi/device-spi
doc/drivers/bf63112.rst             # short driver page (see 5.4)
NEWS                                 # one line under the next release
```

Both C files carry `SPDX-License-Identifier: LGPL-2.1-or-later`, matching libfprint, and are our own
work (no vendor code, no decompiled listings).

### 5.2 Build system

libfprint 1.94.9 does not have per-driver booleans; it has a string `drivers` option
(`-Ddrivers=default|all|<list>`) and an explicit `spi_drivers` list. The change is:

1. top-level `meson.build`: add `'bf63112'` to `spi_drivers` (it is already gated by `have_spi`),
   and add `'bf63112' : [ 'udev' ]` to `driver_helper_mapping` like `elanspi`;
2. `libfprint/meson.build`: add
   `'bf63112' : [ 'drivers/bf63112.c', 'drivers/bf63112_proto.c' ]` to the driver source map;
3. `tests/meson.build`: add `'bf63112'` to the driver test list once a transcript exists;
4. consider adding `bf63112` to `endian_independent_drivers` (the protocol is explicit little-endian,
   so it should be independent of host byte order).

`-Ddrivers=bf63112` is then the existing “meson option”; a new boolean option would be redundant and
is not proposed. The udev rule is generated from the `FpIdEntry` id table
(`FPI_DEVICE_UDEV_SUBTYPE_SPIDEV`, `spi_acpi_id = "BF63112"`), so `70-libfprint-2.rules` binds
`spidev` for `acpi:BF63112:`; no USB IDs exist for this part.

### 5.3 Driver semantics

* `open`: get the spidev node from libfprint’s udev data, query sensor info (`0xC2`) and firmware
  version (`0xA1`), validate geometry.
* `close`, `suspend` (`0xC0`), `resume` (re-query sensor info).
* `enroll`/`verify`/`identify`: complete with `FP_DEVICE_ERROR_NOT_SUPPORTED` **until** §4.5 is
  decoded. This is honest and safe: the driver cannot delete or overwrite templates enrolled from
  Windows, and fprintd shows “not supported” rather than a broken device.
* Once decoded: match-in-sensor means a raw print that stores only the module handle; no host-side
  minutiae, no template data in `/var/lib/fprint`.
* Request state machine: build, write+read one `FpiSpiTransfer`, scan for an answer, resend up to 3
  times (polling until the IRQ GPIO is implemented), fail with a specific error on exhausted
  retries.
* No firmware update, no JTAG, no restore, no template deletion in the first version.

### 5.4 Docs page

In 1.94.9 the documentation is gtk-doc XML with no per-driver pages, so the realistic deliverables
are: (a) a short `doc/drivers/bf63112.rst` **if** the maintainers’ current tree has (or is moving
to) a per-driver docs directory; otherwise (b) a wiki page plus a thorough header comment in the
driver and a `README`/`NEWS` note. The content is the same: what the device is, the ACPI/`spidev`
binding, the supported operations (initially none of enrol/verify), and the safety limits.

## 6. Bring-up evidence needed before proposing this upstream

Ordered; steps 1–3 are read-only (except binding `spidev`, which is reversible), and no step writes
firmware or templates.

1. **Read the ACPI resources.** `acpidump`/`iasl` on the unit; extract from
   `\_SB.PC00.SPI1.FPRT._CRS` the `SpiSerialBus` (mode, bit width, connection speed) and the
   `GpioInt`/`GpioIo` (reset/IRQ) entries; check `_DSD` for `spi-max-frequency`. Record them in
   `docs/hw/fingerprint.md` and make them the driver defaults or module parameters.
2. **Bind `spidev` and run the read-only probe** (`tools/fingerprint/oxp3-fp-probe.py`):
   `info`, `fw`, `scheme`, `uid`, `count`; capture raw hexdumps and the controller’s
   `bytes_tx`/`bytes_rx` statistics. A healthy answer starts with `0xAA`; all `0xFF` means wrong
   mode/clock. This is the first time any code talks to the sensor.
3. **Record answers and fix the framing.** One query per opcode, including `0xC2` and `0xA1`;
   determine the 16-byte answer header, the body start, and whether opcode/phase are echoed. Turn
   the captures into `tests/bf63112/device-spi` transcripts and fixtures, and re-run the Python/C
   parity tests.
4. **Probe the session layer read-only.** Send `0xB0` (query root key) and observe whether the
   module accepts plain commands or demands a session. If it demands SM3, either implement it
   (significant crypto work, needs the algorithm/keys from the driver) or document the limitation
   and keep enrol/verify unsupported.
5. **Decode enrol/identify.** From the Windows driver’s handlers, extract the per-phase payloads
   for `0xB1`/`0xB2` and the handle/score fields; validate on hardware with loops: 10× enrol,
   20× verify (right and wrong finger), cancel at every phase, timeout at every phase, remove,
   count.
6. **IRQ/reset.** Request the `GpioInt` and reset line; compare IRQ-driven latency with polling;
   implement reset on open/resume if the module needs it.
7. **Robustness**: wrong clock/mode recovery, CRC-error injection, retry exhaustion, concurrent
   `fprintd` calls, suspend/resume cycles, multiple open/close, device unplug/replug (SPI is
   internal, so mostly controller rebind).
8. **Security review**: template handle integrity, no host-side template, no firmware write, wipe
   path (`0xB3`) only after the payload is decoded, and a documented “not a secure element”
   statement.
9. **CI/tests**: libfprint’s driver test harness (device transcript), `make check`, `-Wall
   -Wextra -Werror`, ASan/UBSan on the protocol core, and the emulated tests in
   `tests/meson.build`.
10. **Packaging**: confirm a distro can build with `-Ddrivers=all` (spi enabled) and that the udev
    rule lands; no new dependencies beyond GLib/GUsb and the spi/spidev kernel config.

Only after 1–5 (and ideally 6–8) is a merge request defensible. Steps 1–3 alone would let us replace
the “never run” caveats with measured facts and would already be a useful documentation
contribution.

## 7. Risks

* **Bricking**: `0xD1` (firmware update), `0xC3` (JTAG) and `0xD0` (restore MCU) can make the module
  unusable; the driver and tools refuse them by name. Never run the vendor’s firmware update from
  Linux.
* **Templates**: `0xB1`/`0xB2`/`0xB3` can delete or overwrite Windows-enrolled fingers. The first
  driver sends none of them.
* **Binding `spidev`** takes the device from a future in-tree driver and is a device-scoped change;
  do it on an experimental machine, with the documented unbind rollback.
* **Privacy/security**: match-in-sensor is good for privacy (templates stay on the module), but the
  transport is unauthenticated SPI; do not describe it as a secure element, and do not add a
  host-side template store.
* **Undocumented crypto**: SM3/MD5/SHA-1 names in the driver suggest challenge/response; implementing
  crypto from a binary without a spec is risky. Prefer a documented limitation over a guess.
* **Upstream fit**: libfprint may not want a driver that cannot enrol; the plan is to submit the
  transport + query support and the protocol core first **only if** maintainers prefer incremental
  drivers, otherwise wait for steps 4–5. This is an explicit question for the maintainers.

## 8. Open questions for the maintainers

1. Is an incremental driver (transport + queries, enrol/verify `NOT_SUPPORTED`) acceptable, or
   should the first submission be feature-complete?
2. Per-driver docs: `doc/drivers/*.rst`, the wiki, or only the in-tree comment (1.94.9 has neither a
   drivers doc directory nor per-driver pages)?
3. Is a separate `bf63112_proto.c` acceptable, or should the framing live in the driver file?
4. Does the project want the emulated device transcript (`tests/bf63112/device-spi`) even before
   hardware validation, or only with it?
5. Which libfprint baseline should the driver target — 1.94.x internal API (what the prototype is
   checked against) or current `main` (the `fpi_device_get_udev_data`/`spi_acpi_id` APIs may have
   moved)?
