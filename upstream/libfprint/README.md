# libfprint BF63112 patch series

A three-patch series that adds our driver for the Betterlife BF63112
fingerprint module (ONEXPLAYER 3, `ACPI\BF63112`, Intel LPSS SPI) to
libfprint. The driver is **ours**; no libfprint source has been copied into
it and no libfprint tree is stored in this repository.

Status: **built and tested against the series base, never run against the
sensor.** The series is a reviewable first step, not a finished driver.

## Base

* Project: libfprint
* Version: v1.94.9 (tag)
* Base commit: `dc8b05f0a30e93174e861977cede8427c22f8f76`

## Files in the series

| file | subject | contents |
|---|---|---|
| `0000-cover-letter.patch` | MR text | not applied with `git am`; use it as the merge-request description |
| `0001-drivers-add-BF63112-protocol-and-SPI-transport.patch` | `drivers: add BF63112 protocol and SPI transport` | `bf63112_proto.[ch]`, `bf63112_spi.[ch]`, `bf63112_acpi.[ch]`, `bf63112_discover.[ch]` |
| `0002-drivers-wire-BF63112-into-the-build.patch` | `drivers: wire BF63112 into the build` | `bf63112.c`, `libfprint/meson.build`, `meson.build` |
| `0003-docs-describe-BF63112.patch` | `docs: describe BF63112` | `doc/drivers/bf63112.md` |

The udev rule is **generated** by libfprint from the driver's `FpIdEntry`
table (`spi_acpi_id = "BF63112"`); the build produces

```
ACTION=="add|change", SUBSYSTEM=="spi", ENV{MODALIAS}=="acpi:BF63112:",
  RUN{builtin}+="kmod load spi:spidev",
  RUN+="/bin/sh -c 'echo spidev > %S%p/driver_override && echo %k > %S%p/subsystem/drivers/spidev/bind'"
```

so the series installs no static rule file. The generated
`fprint-list-supported-devices` entry is `BF63112 | - | Betterlife BF63112
(ONEXPLAYER 3)`.

## Apply

```sh
git clone https://gitlab.freedesktop.org/libfprint/libfprint.git
cd libfprint
git checkout dc8b05f0a30e93174e861977cede8427c22f8f76
git am /path/to/oxp3-anatase-fixes/upstream/libfprint/000[1-9]-*.patch
```

Do not `git am 0000-cover-letter.patch`: a cover letter has no diff and is
meant to be the MR description. `git am --abort` backs the series out.

## Build and test

Verified in a Fedora 45 container with GCC 16.2.1, meson 1.12.1, ninja
1.13.2, GLib 2.90.1 and GUsb 0.4.9. Build the driver alone (the full default
driver set is not needed for this check):

```sh
meson setup build -Ddrivers=bf63112 -Dintrospection=false -Ddoc=false \
      -Dinstalled-tests=false
ninja -C build
meson test -C build --print-errorlogs
```

Results on a fresh clone with the series applied:

* `meson setup` and `ninja -C build` succeed; the five new C files compile
  with no warning from our code. The only warning in the whole build comes
  from upstream `libfprint/nbis/mindtct/remove.c` (pre-existing).
* `meson test`: **2 passed, 0 failed, 29 skipped**. The two unit tests are
  `fpi-ssm` and `fpi-device`; the driver-emulation tests skip because
  introspection/umockdev is disabled in this configuration.
* The generated udev rule and the supported-device list both contain
  BF63112, confirming the id table is wired correctly.

Fedora note: this container's `systemd-devel` installs `libudev.pc` but no
`udev.pc`, while libfprint 1.94.9 asks pkg-config for `udev` when installing
udev rules. A container-local `/usr/lib64/pkgconfig/udev.pc` shim
(`udevdir=/usr/lib/udev`) was used to configure; that is a build-environment
detail, not part of the series.

## Licence TODO

Every new file starts with a clearly marked line instead of an SPDX header:

```c
/* TODO(licence): SPDX-License-Identifier to be set before submission;
 * the project owner has not chosen the licence for this subtree yet. */
```

The owner has not chosen a licence for our fingerprint subtree, so the
series must not assert one. libfprint is LGPL-2.1-or-later, so
`SPDX-License-Identifier: LGPL-2.1-or-later` is the expected final value, but
only the owner can set it. The patch context copied from libfprint stays
under libfprint's licence; the added code is unlicensed until the TODO is
resolved.

## Submission checklist

Must be true before opening an MR:

1. **Hardware transcript.** At least one captured open + `0xC1` + `0xA0`
   exchange from the OXP3, with valid CRC-32 and a plausible geometry, pasted
   into the MR. No driver behaviour has been observed on hardware yet.
2. **Licence line set** in every new file (see above).
3. **`_CRS` values read and recorded** (SPI clock, mode, word size, IRQ and
   reset GPIO polarity) and the environment overrides either removed or
   justified as a bring-up facility.
4. **Maintainer review of the session layer.** The `0xA2` SM2/SM3/SM4
   handshake and the `0xB0`/`0xB1` enrol/verify payloads are not in this
   series; they must be reviewed together with a maintainer before
   enrol/verify is enabled. The current driver refuses those actions.
5. **IRQ strategy agreed.** The driver polls because the SPI controller does
   not expose the `_CRS` GpioInt; decide with the maintainers whether polling
   is acceptable for a first version or a requested line is required.
6. **Style review** against `HACKING.md` (clang-format, commit message style)
   and a check that no opcode outside the read allowlist can be reached.
7. **Emulation test** (like `tests/elanspi/`) once the answer framing is
   confirmed; this series adds no libfprint tests.
8. **Cover letter** used as the MR description and the base commit updated if
   the MR is opened after 1.94.9.

## What is deliberately not in the series

* `bf63112_session_openssl.[ch]` (SM2/SM3/SM4 session) stays in
  `tools/fingerprint/`. It is not compiled by the driver, its SPI wire
  assumptions are unconfirmed, and it would need its own licence and review.
* Our Python/C protocol test suite and bring-up tools stay in
  `tools/fingerprint/`; they are standalone and are not part of the libfprint
  tree.
* Enrol, verify, identify, template management and the `0xB4` image path.

## Provenance and mirrored patterns

`provenance: derived-facts` — the protocol facts were recovered by observing
the vendor Windows UMDF driver (frame layout, timings, opcodes, `_CRS` use);
no vendor code, strings, binaries or decompiler output are in the patches.
See `docs/PROVENANCE.md` and `docs/hw/fingerprint*.md` in this repository.

The patches use only public libfprint headers and the patterns its other SPI
drivers follow: the `drivers_api.h` include, the `FpDeviceClass` vfunc table,
an `FpIdEntry` id table, `driver_sources`/`spi_drivers`/`driver_helper_mapping`
Meson wiring, and the generated udev rule. No libfprint implementation code
was copied into our files.
