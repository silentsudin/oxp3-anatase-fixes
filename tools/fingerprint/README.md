# BF63112 fingerprint: protocol library, tests, and a compile-ready libfprint driver

The ONEXPLAYER 3 fingerprint reader is a Betterlife BF63112 module on the Intel LPSS SPI bus
(`ACPI\BF63112`, `spi-BF63112:00`) with no Linux driver. This directory now holds the recovered
protocol as a tested userspace library, synthetic device transcripts, and a driver that compiles
against libfprint's real internal driver API. It has **never run against the sensor** - every claim
below is marked proven / inferred / unknown.

See [`docs/hw/fingerprint.md`](../../docs/hw/fingerprint.md) for the hardware evidence and
[`docs/hw/README.md`](../../docs/hw/README.md) for the overall hardware status.

## Layout

| path | what it is |
|---|---|
| `fpbf63112/protocol.py` | Python protocol library (stdlib only): frame build/parse, CRC-32, length/offset arithmetic, chunk sums, command table, image reassembly, retry state machine |
| `fpbf63112/spi.py` | recovered SPI/SPB transport (stdlib only): selector `(opcode & 0x3f) << 1`, two-entry `spi_ioc_transfer` sequence, 1000/2030 (5000/10010 image, 2000/4016 restore) deadlines, gpiod/sysfs/poll IRQ waiters, reset pulse, cancel settle, allowlist/destructive gates |
| `fpbf63112/acpi.py` | raw `_CRS` resource-template parser for `SpiSerialBus`, `GpioInt`, `GpioIo`, I2C and IRQ items; validates lengths/offsets and reduces them to the driver parameters |
| `fpbf63112/aml.py` | locates the FPRT `_CRS` in raw DSDT/SSDT AML (`14 <PkgLength> "_CRS"` + `A4 11 <PkgLength>` buffer), validates the result with `acpi.py`, prefers the `SPI1` scope, and reports ambiguity instead of guessing |
| `fpbf63112/crypto.py` | session crypto (stdlib only): SM3, SM4 (ECB/CBC), SM2 over sm2p256v1, GM/T 0003 KDF, the 0xA2 handshake, the secure-frame transform, a bounded variant search, `selftest()`/`benchmark()` |
| `tests/` | unittest suite, synthetic fixtures, generated frame vectors, replay helpers, the SM vector + handshake tests, `test_spi.py` + `test_acpi.py` for the transport/parser, and `test_aml.py` for the `_CRS` AML scan |
| `tests/fixtures/*.json` | small device transcripts: clean answers, noise, truncation, bad CRC, wrong opcode, retry exhaustion, GET_IMG good/partial/bad-sum/retried/oversized |
| `tests/vectors.json`, `tests/vectors.h` | one set of frame vectors shared by the Python and C implementations |
| `tests/generate_fixtures.py` | regenerates all fixtures/vectors (checked in; a test fails if they drift) |
| `bf63112_proto.h/.c` | dependency-free C11 protocol core (no allocation, `<stdint.h>` only) used by the driver and its tests |
| `tests/test_proto.c` | C tests against the same vectors + rejection/scan/image paths |
| `bf63112_spi.h/.c` | dependency-free C11 SPI/SPB transport: selector, destructive/alias gate, read/write allowlists, two-entry `SPI_IOC_MESSAGE`, spidev ioctls, recovered deadlines/cancel/settle, sysfs IRQ/reset + optional libgpiod v2/v1, timed-poll fallback |
| `bf63112_acpi.h/.c` | C11 `_CRS` parser: SpiSerialBus, GpioInt/GpioIo (IO restriction bits 5-6), small/extended IRQ, opaque items; bounds-checked |
| `bf63112_discover.h/.c` | resolves the unique `/sys/class/spidev/*` with modalias `acpi:BF63112` (explicit/udev path wins) |
| `tests/test_spi.c` | standalone C tests for the frame builder, selector, allowlist, deadlines, execute/cancel/reset/IRQ budget, ACPI parser and discovery; injects the I/O vtable and never opens `/dev/spidev` |
| `libfprint-bf63112.c` | libfprint driver: SPI open/close/suspend/resume, 0xC1 sensor info + 0xA0 firmware query, optional reset/IRQ from environment; enrol/verify refuse with `NOT_SUPPORTED` (no write opcode is allowlisted) |
| `bf63112_session_openssl.c/.h` | the driver-side session crypto (OpenSSL EVP SM3/SM4 + `EC_POINT_mul`), compile-checked but not enabled by default |
| `stub/` | **offline syntax-check shim only** - fake `fpi-*.h` + minimal GLib declarations, clearly marked "not libfprint"; never installed or linked |
| `Makefile` | `py-test`, `proto-test`, `spi-test`, `syntax`, `gpiod-syntax`, `syntax-real` targets |
| `tests/run-c-tests.sh`, `tests/check-libfprint.sh` | run the C targets here without a host compiler (ephemeral podman) |
| `oxp3-fp-session.sh` | the guided root runbook: preconditions, `_CRS` extraction/parse, confirmation before each mutation, guarded read-only probe, trap-driven rollback with verification, `--dry-run`/`--non-interactive`/`--resume`, `summary.json` transcript. `--selftest` is offline; `--fixture-init` builds fake `/sys` trees for the tests |
| `oxp3-fp-probe.py` | the standalone `sudo` bring-up probe: `--transport spi` (default) or the old `--transport frame`; `--dry-run` and `acpi_info --crs` do no I/O; a real transfer needs `--spidev PATH` and `--allow-writes`; destructive opcodes are refused by opcode and selector |

## What is proven, inferred, and unknown

**Proven** (matches `BetterlifeFingerprintDevice.dll` 9.39.15.26 byte for byte; recovered from the
Windows UMDF driver, no vendor code in this repo):

* Host frame: `AA | L:u32 LE | 16x0 | 4C op phase 00 arg:u16 len:u32 data | pad16 | CRC-32`,
  `L = 20 + align16(10 + data_len)`, CRC-32/ISO-HDLC over everything before it including `0xAA`.
  A data-less query is 41 bytes with `L = 36`.
* The opcode table `0xA0`-`0xD0`, read from the driver's dispatch table (RVA `0x46F00`) and
  cross-checked against the functions that emit each byte. Names and roles in
  `fpbf63112.protocol.COMMANDS`.
* Answer framing: the answer is byte-for-byte the request's layout - opcode at 22, phase at 23,
  length at 27, body at **31** - and the driver checks byte 0 for `0xAA` and verifies the same
  CRC-32 over `L + 1` bytes on receive.
* The 16 bytes at offset 5 are a per-frame **nonce**: zero for a plaintext command, 16 random
  bytes (plus an SM4-encrypted command block) when the secure-frame flag is set.
* The session layer: `0xA2` carries a 65-byte SM2 public key, the answer (>= `0x71` bytes) carries
  the device's point + SM3 tag + wrapped 16-byte SM4 key; the driver derives it with SM2 ECDH and
  the GM/T 0003 KDF. Nothing is hardcoded or DPAPI-backed.
* `CMD_GET_IMG_DATA` (`0xB4`): phase 0 has `status:u8 @0`, `total:u32 @4`, `sum:u32 @0x14`
  (>= `0x18` bytes); phase 1 streams 8-bit pixels, each chunk a full `0xAA`/CRC frame; acceptance
  needs `total == width*height` and an **8-bit running sum**; at most `0x1FCC` (8140) bytes per
  transfer, re-sent on answer timeout (1000/5000 ms per attempt).
* `CMD_QUERY_SENSOR_INFO` is `0xC1`: `a:u16, b:u16, width:u16 @4, height:u16 @6, flags:u8 @8`
  (>= 9 bytes). `CMD_QUERY_FIRMWARE_VER` is `0xA0`: `state, project hi/lo, version:u32` (>= 7 B).
* The device is SPI-mode udev hardware (`spidev`), not USB; no driver is bound today.

**Inferred** (implemented conservatively, must be confirmed on hardware):

* Whether the module *enforces* the session for `0xB0`/`0xB1`: the vendor driver always sets the
  secure-frame flag there, but `0xA0`/`0xA1` go out in plaintext, so plaintext framing is at least
  understood by the device for queries. A plaintext `0xB0` start is the decisive probe.
* The exact SM4 chaining on the wire (the transmit and receive paths derive the per-frame value
  differently in this build). A plaintext driver does not need it.
* Answers may or may not echo `phase`; the library checks the opcode and leaves the phase check
  optional (`Transaction(check_phase=...)`).
* GET_IMG has **no** extra per-chunk sum field: chunks are covered by the per-frame CRC-32 and the
  image by the phase-0 8-bit sum. `ImageAssembler.add(..., chunk_sum=)` still accepts one if a
  future capture shows otherwise.

**Unknown / out of scope**

* Whether a plain-spoken driver is accepted for enrol/identify (see above); `0xFF` is not in the
  driver's table at all and is never sent.
* Enrol/identify result semantics (`0xB0` phase 2 / `0xB1` phase 0): the 18-byte handle and the
  status byte are proven, the meaning of the u16 inside the handle and any score are not.
* SPI clock, mode, bit width and the reset/IRQ GPIOs - they live in `\_SB.PC00.SPI1.FPRT._CRS`
  and are root-only on this host.
* Whether the second ACPI node (`BF63112:01` on SPI2) is real.
* Anything about hardware behaviour: **no code here has talked to the sensor.**

## SPI bring-up probe (implemented, not yet run on hardware)

The probe now speaks the recovered SPI/SPB transport by default instead of
putting the USB `0xAA` frame on the wire:

```sh
# offline: exact selector + transfer shape for each read, no device opened
python3 tools/fingerprint/oxp3-fp-probe.py --dry-run --spidev /dev/spidev0.0 fw info
# offline: parse an extracted _CRS resource template
python3 tools/fingerprint/oxp3-fp-probe.py acpi_info --crs /tmp/fprt-crs.bin
# hardware: explicit path + opt-in are both required; fw=0xA0, info=0xC1
sudo python3 tools/fingerprint/oxp3-fp-probe.py --crs /tmp/fprt-crs.bin \
    --spidev /dev/spidev0.0 --allow-writes --gpio 42 --irq-edge rising \
    fw info
```

The whole root sequence above - preconditions, `_CRS` extraction and parse, the
`driver_override`/bind step, the dry run, the guarded reads and the rollback -
is automated with confirmations and a verified restore by
`tools/fingerprint/oxp3-fp-session.sh`
([docs/hw/fingerprint-session.md](../../docs/hw/fingerprint-session.md)); the
commands here are what it runs, and the manual fallback.

`--crs` fills in speed, mode, bits and the IRQ/reset pins when the CLI does not
override them. `--allow-op` can add one raw *read* byte; no destructive opcode
can be enabled. `--transport frame` remains only as the old experiment. See
`docs/hw/fingerprint-transport.md` §8-§9 for the safety model and the exact
root-time procedure.

## Run the tests

The host has Python 3 but no C compiler and no `make`, so:

```sh
# Python protocol + transcript + fuzz tests (this is the full behavioural suite)
python3 -m unittest discover -s tools/fingerprint/tests -v
#   -> Ran 289 tests ... OK

# C protocol tests (same vectors, independent implementation)
tools/fingerprint/tests/run-c-tests.sh
#   -> ok: 324 checks, 10 vectors

# Driver against the real libfprint 1.94.9 headers (fetches nothing into the repo;
# needs a source checkout and podman or local glib2/gusb devel):
git clone --depth 1 --branch v1.94.9 \
  https://gitlab.freedesktop.org/libfprint/libfprint.git /tmp/libfprint-1.94.9
LIBFPRINT_SRC=/tmp/libfprint-1.94.9 tools/fingerprint/tests/check-libfprint.sh
#   -> exit 0, -fsyntax-only with -Wall -Wextra -Werror

# Driver and transport modules against the offline shim headers
# (no libfprint/GLib at all):
podman run --rm -v "$PWD":/w:z -w /w/tools/fingerprint \
  localhost/oxp3-fixes-build:f45 make syntax

# The OpenSSL session module (needs openssl-devel; the image above has it):
podman run --rm -v "$PWD":/w:z -w /w/tools/fingerprint \
  localhost/oxp3-fixes-build:f45 make session-syntax
#   -> session-syntax: OK (OpenSSL headers present)

# Session crypto vectors + benchmark, no device involved:
python3 tools/fingerprint/oxp3-fp-probe.py --crypto-selftest
```

Locally (a machine with gcc + make): `make -C tools/fingerprint check` runs Python, C and the shim
syntax check. `make syntax-real LIBFPRINT_SRC=...` is the authoritative API check.

Test coverage, in words:

* **Frames**: documented lengths (41/57/73...), per-length formula check, field round-trip, padding,
  header junk scanning, resynchronisation after a bad CRC.
* **Rejection**: every truncation, every single-byte mutation, trailing bytes, bad L, unaligned
  block, bad magic, `data_len` past the block, CRC mismatch (with values).
* **Fuzz/property**: 400 random valid frames round-trip (`build -> parse -> build` identical),
  truncated prefixes never parse, random buffers never crash the scanner.
* **Image**: header round-trip, chunk accounting, overrun/oversize/empty, incomplete image, global
  and optional per-chunk sum.
* **Session**: resend budget, exhaustion, hint-free phase matching, mismatch rejection, invariants
  over random event sequences.
* **Transcripts**: each fixture has a hand-written expected outcome, replayed through the same
  transaction/assembler code a driver uses.
* **Cross-language**: `vectors.h` is generated from the Python implementation; `test_proto.c`
  rebuilds every frame from its inputs and compares bytes, then parses them.
* **Probe parity**: the `sudo` probe's frame builder/parser/command table are asserted equal to the
  library so the two cannot drift.

Regenerate fixtures/vectors after a protocol change (tests fail until you do):

```sh
python3 tools/fingerprint/tests/generate_fixtures.py
```

## Using the library

```python
import sys; sys.path.insert(0, "tools/fingerprint")
from fpbf63112 import protocol as P

frame = P.build_frame(P.Command.QUERY_SENSOR_INFO)   # 41-byte read-only query
frames, errors = P.scan_frames(spi_readback)          # junk/padding tolerant
tx = P.Transaction(P.Command.QUERY_SENSOR_INFO)       # resend-on-timeout state machine
```

Everything is stdlib-only; there is no hardware access in the library, which is why the tests can
run anywhere. `P.DESTRUCTIVE_COMMANDS` is the set a tool must never send by accident.

## The session layer (SM2/SM3/SM4)

The module refuses nothing at the frame level, but enrol/identify are sent as "secure frames" by the
vendor driver, so a driver eventually needs the session. It is fully implemented in
`fpbf63112/crypto.py` (Python) and `bf63112_session_openssl.c` (C, opt-in):

```python
from fpbf63112 import protocol as P
session = P.open_session(transport)           # 0xA2: SM2-ECDH -> GM/T 0003 KDF -> SM3 tag
sealed = session.seal_frame(P.build_frame(P.Command.ENROLL_FINGERPRINT))
```

Recovered from `BetterlifeFingerprintDevice.dll` (proven; every step is in `docs/hw/fingerprint.md`):

* per-open SM2 keypair, `0xA2` payload `04 || X || Y` (65 bytes);
* answer `status:u8 | P_dev:64 | tag:32 | klen:u32 LE | wrapped` (>= `0x71` bytes);
* shared point `S = d * P_dev` serialised as `X || Y` (64 bytes, big-endian);
* `mask = KDF(S, klen)` with `SM3(S || BE32(ctr))`, `ctr = 1, 2, ...` (GM/T 0003.4), all-zero
  rejected; `key = mask xor wrapped`;
* `tag == SM3(X || key || Y)`;
* secure frames: `H' = SM4_encrypt(key, H)` then blockwise `SM4` under `H'` (ECB, not CBC - the
  receiver uses the SM4 *decryption* key schedule under the same `H'`).

**Proven by the test suite** (no device):

| vector | value |
|---|---|
| SM3 `abc` | `66c7f0f4…8f4ba8e0` (GB/T 32905) |
| SM3 `abcd` x16 | `debe9ff9…9c0c5732` (GB/T 32905 appendix) |
| SM4 ECB, key = plaintext | `681edf34…536e4246` (GB/T 32907 A.1) |
| SM2 keypair `d -> P` | the published SM2 sample key (`09f9df31…2da9ad13`) |
| KDF | equals the definition `SM3(S || BE32(1)) || SM3(S || BE32(2)) || …` |
| handshake | synthetic device built from the same primitives: key + tag round-trip |

Performance (this host, CPython 3.15, best of 8, 4 KiB): **SM3 4.8 ms** (0.86 MB/s),
**SM4-ECB 2.1 ms** (1.9 MB/s), **SM2 keygen 1.3 ms**. `--crypto-selftest` prints these.

**Assumed, not proven** (what a live session must settle): whether the module accepts a plaintext
`0xA2` or wants the vendor's secure form (sealed with the all-zero initial key); that `klen` is 16
and `status` 0; and whether the SPI/SPB branch uses the same secure-frame transform as the USB
branch this was recovered from. `crypto.search_variants()` retries 32 bounded KDF/tag variants
against one captured answer, and `oxp3-fp-probe.py --session` runs the whole experiment and prints
the raw answer (never the key).

### The C side and this host's OpenSSL

`bf63112_session_openssl.c` uses `EVP_MD_fetch("SM3")` + `EVP_Digest*` for the hash and KDF,
`EVP_CIPHER_fetch("SM4-ECB")` + `EVP_CIPHER_CTX_set_padding(0)` for the cipher, and the EC layer
(`EC_GROUP_new_by_curve_name(NID_sm2)`, `EC_POINT_set_affine_coordinates`, `EC_POINT_mul`,
`EC_POINT_get_affine_coordinates`, `BN_bn2binpad`) for the shared point - **not** `EVP_PKEY_derive`,
because OpenSSL's SM2 ECDH returns only the 32-byte x coordinate while the vendor KDF and tag need
all 64 bytes of `X || Y`. It is compile-checked with `-Wall -Wextra -Werror` against openssl-devel
(`make session-syntax`), and it degrades to `-ENOTSUP` at run time on a provider without SM.

**This host cannot run it**: OpenSSL 4.0.3 here is built with `OPENSSL_NO_SM2` and its default
provider lists no SM3/SM4/SM2 (`openssl list -digest-algorithms` has no `SM3`, the `ecparam` list
has no `sm2` curve). That is why the Python implementation is the reference and why the driver flag
stays off: on Anatase the C path would need a provider that ships SM, or the Python-equivalent code
in the driver.

## The libfprint driver

`libfprint-bf63112.c` targets **libfprint 1.94.x internal driver API** (verified against the
`v1.94.9` source tree with GLib 2.90.1 and GUsb 0.4.9). It is an in-tree-style driver: the
`fpi-*` headers are private to libfprint and are not installed by distributions, so it must be built
inside a libfprint checkout (or against one with `LIBFPRINT_SRC`).

On the ONEXPLAYER 3 host itself this was checked: `/usr/include/libfprint-2` does not exist,
`pkg-config --modversion libfprint-2` fails, and no libfprint source tree is present - only the
runtime `libfprint-2.so.2` (package `libfprint-1.94.100-1.fc45`, `fprintd` 1.94.5). Hence the two
check paths: the offline `stub/` shim, and `syntax-real` against a fetched v1.94.9 tree.

What it implements:

* `open`: opens the spidev node libfprint provides for the udev match, queries sensor geometry
  (`0xC1`) and firmware version (`0xA0`), then completes.
* `close`, `suspend` (`0xB6 SLEEP`), `resume` (re-query sensor info).
* A generic request state machine: build frame, write+read one `FpiSpiTransfer`, scan for the
  answer, re-send up to 3 times on timeout/bad frame (the Windows driver's resend behaviour,
  polling instead of the not-yet-available IRQ GPIO).
* `enroll`/`verify`/`identify` complete with `FP_DEVICE_ERROR_NOT_SUPPORTED` and a message naming
  the undecoded piece. They do **not** send `0xB1`/`0xB2`/`0xB3`, so templates enrolled from
  Windows cannot be touched.

Exact libfprint API surface used (adapt these first if the version changes):
`FpDeviceClass` (`id`, `full_name`, `type = FP_DEVICE_TYPE_UDEV`, `id_table`, `scan_type`,
`open/close/enroll/verify/identify/suspend/resume`), `FpIdEntry` (`udev_types =
FPI_DEVICE_UDEV_SUBTYPE_SPIDEV`, `spi_acpi_id = "BF63112"`), `fpi_device_class_auto_initialize_features`,
`fpi_device_get_udev_data`, `fpi_device_get_cancellable`, `fpi_spi_transfer_new/write_full/read_full/
submit`, `FpiSpiTransfer.{ssm,buffer_wr,buffer_rd,length_wr,length_rd}`, `fpi_ssm_new_full`/`fpi_ssm_*`,
`fpi_device_{open,close,enroll,verify,identify,suspend,resume}_complete`,
`fpi_device_error_new_msg`, `fp_dbg/fp_info/fp_warn`, `G_DECLARE_FINAL_TYPE`/`G_DEFINE_TYPE`.

What a maintainer must adapt to turn this into an upstreamable driver:

1. **Build system**: libfprint 1.94.9 lists drivers in `libfprint/meson.build` (source path) and
   `meson.build` / `tests/meson.build` (driver name, `udev` backend). Add `bf63112` in all three
   places; the udev rule is generated from the id table, so `70-libfprint-2.rules` then binds
   spidev for `acpi:BF63112:`. No USB ids exist for this device.
2. **SPI parameters**: the driver inherits whatever `spidev` was configured with. Until the ACPI
   `_CRS`/`_DSD` values are read (see below), a wrong mode/clock yields `0xFF` bytes forever. A
   finished driver should parse/require the controller default or expose a module parameter.
3. **IRQ**: the module raises a GPIO when an answer is ready; `pxa2xx-spi` does not expose it, so a
   real driver should request the `GpioInt` from `_CRS` (or use `fpi_device_add_timeout` polling as
   this one does - correctness still unproven without hardware).
4. **SM3/session**: confirm whether `0xA2` must precede normal commands. If yes, implement
   the handshake before enabling enroll/verify.
5. **Enrol/verify payloads**: decode `0xB0`/`0xB1` (handle, score, press counter) and store only the
   opaque handle in the print (`fpi_print_set_type (print, FPI_PRINT_RAW)`,
   `fpi_print_set_device_stored (print, TRUE)`); templates stay on the module.
6. **spidev bufsiz**: default 4096. Queries fit; GET_IMG chunks (up to 8140 B + frame overhead)
   would need `/sys/module/spidev/parameters/bufsiz` raised and a larger read buffer. The shipped
   driver never uses GET_IMG.
7. **`fpi_device_get_udev_data` / `spi_acpi_id`** are 1.94-era APIs; if they move, the udev binding
   changes with them.

## Privileged bring-up (commands only; nothing here was run)

The sensor has no driver and no `/dev/spidev*`. Steps 1-2 are read-only; step 3 is the only state
change and is reversible. **Do not** run anything that writes firmware, JTAG or removes templates.

1. Read the ACPI resources (the numbers a driver needs):

   ```sh
   sudo cat /sys/firmware/acpi/tables/DSDT > /tmp/dsdt.aml
   sudo dnf install acpica-tools          # or run iasl in a toolbox
   iasl -d /tmp/dsdt.aml
   grep -n -A 40 'FPRT' /tmp/dsdt.dsl
   ```

   Look for `SpiSerialBus (..., ConnectionSpeed, SpiMode, SpiBitWidth, ...)` and the
   `GpioInt`/`GpioIo` entries (`PinIndex`, `GpioIo` numbers). `ACPI\BF63112:00` may also carry a
   `_DSD` with `spi-max-frequency`. The kernel exposes none of this without root, which is why it
   is step 1.

2. Confirm the controller sees the device (already true on this unit):

   ```sh
   ls /sys/bus/spi/devices/                  # -> spi-BF63112:00
   cat /sys/bus/spi/devices/spi-BF63112:00/uevent
   ```

3. Bind `spidev` so userspace can reach it (reversible, takes the device from any future driver):

   ```sh
   sudo modprobe spidev
   echo spidev | sudo tee /sys/bus/spi/devices/spi-BF63112:00/driver_override
   echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/bind
   ls -l /dev/spidev*                        # typically /dev/spidev0.0
   ```

   Only if you plan to use GET_IMG diagnostic chunks (not needed for the probe) raise the spidev
   transfer ceiling, either at load time or at runtime:

   ```sh
   sudo modprobe spidev bufsiz=8192
   echo 8192 | sudo tee /sys/module/spidev/parameters/bufsiz
   ```

4. Run the **read-only** probe (add `--speed`/`--mode` from step 1; `--raw-info` to dump frames):

   ```sh
   sudo python3 tools/fingerprint/oxp3-fp-probe.py info
   sudo python3 tools/fingerprint/oxp3-fp-probe.py fw scheme uid count
   sudo python3 tools/fingerprint/oxp3-fp-probe.py --speed 1000000 --mode 0 --raw-info info
   ```

   The probe only sends `0xA0`, `0xA1`, `0xB3`, `0xC0`, `0xC1` and refuses the destructive opcodes
   by name. A healthy answer starts with `0xAA` and reports sensor width x height. All `0xFF`/no
   answer almost always means wrong SPI mode or too high a clock. If
   `/sys/bus/spi/devices/spi-BF63112:00/statistics/bytes_tx` exists, it tells you whether the
   transfer happened at all even when the answer is garbage.

### Rollback

```sh
echo spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/unbind
sudo rm -f /sys/bus/spi/devices/spi-BF63112:00/driver_override
sudo modprobe -r spidev
```

Nothing else is modified: no packages, no config, no firmware, no boot parameters, no reboot.

## Safety

* **No firmware writes, no JTAG, no MCU restore.** `0xD0` (UPDATE_FIRMWARE), `0xC3`
  (RESTORE_MCU), `0xC3` (JTAG) are refused by both the probe and the Python/C command tables.
* **No template deletion.** `0xB0`/`0xB2` are not wired up; fingers enrolled in Windows stay
  untouched. The driver's enrol/verify paths refuse before sending anything.
* **No firmware/vendor binaries in this repo.** Fixtures and vectors are synthetic, generated from
  the frame rules; no vendor DLL, blob or decompiled listing is stored.
* **SPI is low risk by nature**: the frame has a CRC, wrong parameters give garbage reads, and the
  module is platform-powered. Binding spidev still takes the device from any future in-tree driver -
  do it on a machine you are experimenting on, not one you depend on for login.
* The tests and the driver compile but require no hardware; running them changes nothing.

## Path to upstreaming

1. Land the protocol library + tests (this directory) upstream-agnostic; they document the wire
   format better than any commit message.
2. Get the ACPI `_CRS` values (clock/mode/bit width/GPIOs) with root and put them in the commit
   message and the driver's defaults.
3. Confirm the read-only query answers on hardware, then the answer framing (open point 2 in
   `docs/hw/fingerprint.md`). Adjust `parse_sensor_info` field offsets if needed.
4. Decode `0xB1`/`0xB2` payloads (with the module's own templates, on a scratch unit), implement
   enrol/verify/identify, and store only handles.
5. Decide the GPIO strategy: request the `_CRS` IRQ instead of polling.
6. Submit to libfprint as `drivers/bf63112.c` + meson entries + the generated udev rule; add a
   `tests/` device description like `tests/elanspi/device-spi` if emulation is possible.
7. Only then discuss the SM3/session layer - a first driver should avoid it entirely and rely on
   module-side templates.

## Open points (explicit)

1. Nothing in this directory has run against the sensor; the probe's first run is still the first
   execution.
2. SPI clock/mode/bit width and the reset/IRQ GPIOs are unread (root-only `_CRS`).
3. Device-to-host answer framing: the 16-byte answer header and the exact body start are inferred.
4. Sensor-info/firmware payload field layouts are inferred from handler reads, not verified.
5. Whether answers echo phase/opcode is unverified (library defaults to opcode-only).
6. Enrol/identify payloads and template handle/score format: unknown. Whether the module *enforces*
   the session for `0xB0`/`0xB1` is still a hardware question, but the session itself is implemented
   and vector-tested now; see "The session layer" above.
7. The session's assumed parts (plaintext vs secure `0xA2`, `klen`, the SPI branch's frame
   transform) are listed in "The session layer" and in `docs/hw/fingerprint.md`.
8. Whether GET_IMG has per-chunk checks besides the global 8-bit sum: conflicting notes.
9. `BF63112:01` on SPI2: harmless phantom or second source; untested.
10. The driver's retry timing (3 x 40 ms poll) is a guess; the Windows values are 1000/5000 ms per
    attempt with GPIO-triggered completion.
