# VMM6210 (Cayenne) firmware reverse engineering log

Working log for the firmware-RE route on the dock's DP-to-HDMI converter. Companion to
[HDMI-VRR-PCON.md](HDMI-VRR-PCON.md); that file still says "VMM7100" in several places —
the chip identification below supersedes it.

Status: firmware obtained, image mapped, loaded in Ghidra, decompiler verified. The
`override adaptive sync` logic and the VTEMP configuration path are the active targets.
Nothing has been flashed to the dock and no kernel or display configuration has been
changed for this route.

## Chip identification (correction)

The dock's DP branch device is a **Synaptics VMM6210**, not a VMM7100.

- Evidence: DPCD `0x507`–`0x508` = `62 10` in the saved AUX capture. The Synaptics fwupd
  plugin reads exactly those two bytes as a big-endian chip ID and formats the name as
  `VMM%04x` → `VMM6210`.
- The branch reports firmware **6.5** (`0x0605`; vendor builds name the same family
  `6.05.x`).
- Dock board: **JSAUX HB1201B**, version string **HB1201BWSXS01** (12-in-1 RGB docking
  station, 4K@120 HDMI + DP, "VRR Supported" per the retail listing).
- The 0x3036 (HDMI link mode) / 0x303B (HDMI TX status) / 0x3050–0x3052 (PCON control)
  registers observed in earlier AUX captures are still the ones of interest; a blackout
  capture contained no read of 0x303B or 0x3036, so converter-side HDMI link state during
  the failure is still unobserved.

## Files and sources

All firmware artifacts live in `/var/home/holzr/git/linux_scratch/vmm6210-fw/`.

| File | What it is |
|---|---|
| `15534_VMM6210_1219.zip` | UGREEN 15534 Revodok Pro updater package, VMM6210 fw **6.05.106**. SHA-256 `605971314f015b67e06df5d0cc64b75cd10ed5b446a74e6fa0611a29780f69fb` (verified against oemdrivers.com). |
| `ug/15534 VMM6210 升级程序/cayenne_firmware_ug2L6100_6.05.106.fullrom` | 1 MiB full flash image. `file(1)` misdetects it as "EDID data" because the image starts with a 128-byte EDID. |
| `ug/.../ver8.05.002/VmmDPTool*.exe` + `vmmUsb.dll`, `AtUsbHid.dll` | Synaptics flashing/parameter tools (Windows PE). Flash ops go over USB HID. |
| `cayenne_bankA_firmware_6.05.106.bin` | Bank A extracted: fullrom `[0x4000:0x43000]` (0x3F000 bytes). This is what is loaded in Ghidra. |
| `cayenne_bankA_config.bin` | Config/EDID tail: fullrom `[0x4fc00:0x50800]` (3072 bytes). |
| `VMM6210-Chipset.pdf` | 21-page datasheet (mslforces.com). Plain curl gets 403; needs a browser User-Agent + referer. |
| `unisynk-fw-instructions.pdf` | Unisynk 12-port Cayenne 2L hub update instructions. |

Other vendors' VMM6210 builds (diff/flash-update candidates, not yet downloaded):

- Unisynk: `12-port Cayenne_2L_6.05.130_6210.fullrom` from `unisynk.se/downloads/` — a
  newer build than our dock's 6.5.
- i-tec: `cayenne_firmware_LX_2L6210_ITEC_2L_V1212.fullrom` (i-tec drivers page; their
  "how to select correct firmware.pdf" documents chip selection).
- JSAUX has no published package for HB1201B; support contact is the remaining source for
  an exact-board image.

## Fullrom layout (verified)

Two firmware banks are **byte-identical** (1.000 fraction over the full 0x3F000 span):

```
0x00000–0x01000   EDID (also copied at 0x50000 inside the config area)
0x04000–0x43000   bank A firmware (0x3F000)
0x4f000–0x51000   config area; nonzero at 0x4fc00–0x50800
0x54000–0x93000   bank B = exact copy of bank A
0x9f000–0xa0000   second config tail (0x9fc00–0xa0000)
```

The bank A header at `0x4000` begins `06 05 6a 00 …` (version 6.05) followed by
offset/value pair records (`xx 00 34 20` repeating) and three near-identical 60-byte
records at 0x4058/0x4088/0x40b8. This is almost certainly the **"Firmware
Configuration (Cayenne)"** table that VmmDPTool's config editor reads and writes.

## CPU architecture

The datasheet confirms: embedded **32-bit RISC-V** CPU, single/quad SPI master, 24 MHz
XTAL, I2C master, GPIO-to-MCU, secure boot/OTP. Ghidra language `RISCV:LE:32:default`
decodes the image cleanly (see below). Ghidra flags a variant ambiguity
(`default` vs `AndeStar_v5` — Andes RISC-V custom extension); decoding consistency should
be checked if any function looks wrong.

## Ghidra state

- Flatpak `org.ghidra_sre.Ghidra` 12.1.3, MCP bridge at `127.0.0.1:8089`.
- Program: `/cayenne_bankA_firmware_6.05.106.bin`, selected with
  `switch_program(program="cayenne_bankA_firmware_6.05.106.bin")`.
- Imported via `import_file(language="RISCV:LE:32:default")` — lowercase `riscv:…` IDs are
  rejected; language IDs are uppercase.
- 385 functions auto-discovered, analysis complete. Decompiler verified:
  `FUN_ram_00000d7e` decompiles as a clean `memmove`.

## Key strings in bank A (addresses relative to bank base 0)

| Address | String |
|---|---|
| `0x31440` | `Invalid MSA found on RFRM%d: %08x %08x` |
| `0x31494` | `RX_PARSE_MSA state: No VSC received.` |
| `0x314bc` | `timeout,force send MSGQ_RX_FRM_PARSE_MSA` |
| `0x314e8` | `RX_PARSE_MSA state: Using MSA, not VSC.\tsend MSGQ_RX_FRM_PARSE_MSA` |
| `0x324bc` | `rx%d MSA changed on index %d: %08lx %08lx` |
| `0x342a0` | `override adaptive sync: %02x` ← active target |
| `0x34360` | `tx_hdmi_enable_vtemp_packet` |
| `0x34a08` | `%s: Update VTEMP packet` |
| `0x34a24` | `Configuring VTEMP with vrr_enable = %d fva_m1 = %d, base_vfront = %d, base_rr = %d, rb = %d` |
| `0x36f6c` | `TMDS clock over, force to 420 down-conversion` |

The firmware contains the complete HDMI 2.1 VRR stack on the chip side: an RX-side MSA
parse state machine that distinguishes MSA from VSC, inter-core MSGQ notifications
(`MSGQ_RX_FRM_PARSE_MSA`), and a TX-side VTEM builder with explicit VRR parameters.

## VmmDPTool intelligence (strings from the PE binaries)

- Chip name: `Cayenne (DP1.4 MST RX & MST TX)`.
- DPCD configuration names: `{2214h}:ADAPTIVE_SYNC_CAPABILITY` and
  `[0]:ADAPTIVE_SYNC_SDP_SUPPORTED` — the same DPCD space Intel reads for AS SDP caps.
- `CNMVRR` (unexplained string, likely a VRR-related mode/config token).
- `Active flash bank: %d`, `Flash Sharing Configuration`, `Firmware Configuration (Cayenne)`
  menus; `.fullrom`/`.eeprom` file selection; flashing via USB HID
  (`AtUsbHid.dll`, `vmmUsb.dll`).

## Open questions

1. What sets the `override adaptive sync` byte, and what do its values mean? If it is a
   config-table entry or a DPCD-writable vendor register, the OXP3 VRR failure may be
   fixable at runtime — no reflash, no kernel change.
2. How the RX_PARSE_MSA state machine reacts to i915's variable-timing stream, and what
   makes it drop the picture (black vs duplicated image vs full dock reset).
3. Per-vendor config differences (UGREEN vs JSAUX): diff a second vendor's fullrom config
   table against `cayenne_bankA_config.bin`.
4. Whether JSAUX ships a newer HB1201B firmware via support (6.05.130 exists for other
   boards).

## Next steps

1. Find the function referencing `0x342a0` ("override adaptive sync"). Code is
   PC-relative: 718 `auipc` instructions, **zero** absolute `lui` references to strings,
   and the RISCV Constant Reference Analyzer produced no string references. Route: walk
   `auipc+addi` arithmetic (target = `P + (imm20<<12) + sext(addi)`), or decompile
   candidates and let the decompiler fold the pairs.
2. Decompile the VTEMP configuration path around `0x34a24` and the `RX_PARSE_MSA` MSGQ
   state machine.
3. Download Unisynk 6.05.130 (and/or i-tec) fullrom; diff config tables to isolate
   per-vendor bytes.
4. Ask JSAUX support for the HB1201B (HB1201BWSXS01) firmware package; a newer VMM6210
   build is the direct fix candidate if the code turns out to be the bug.

## Prior kernel-route conclusions (kept for context)

- Fixed-refresh 4K120 DSC through this PCON is stable on i915/xe; every variable-timing
  signaling attempt (AS SDP v1/v2/AVT, FreeSync SPD, HDMI-Forum translation) broke the
  external output.
- The VMM6210 accepts AUX writes to `DP_DOWNSPREAD_CTRL` but does not latch
  `MSA_TIMING_PAR_IGNORE_EN` while the stream is up; commit-time writes (patch 0023) are
  structurally unable to work and were removed. Patch 0024 forces a full modeset on VRR
  transitions so the bit is programmed during training — tested, did not resolve the
  failure (2026-11-22 boot with kmod-oxp3 …-202611220207).
