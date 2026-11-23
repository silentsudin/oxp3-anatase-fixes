# VMM6210 (Cayenne) firmware reverse engineering log

Working log for the firmware-RE route on the dock's DP-to-HDMI converter. Companion to
[HDMI-VRR-PCON.md](HDMI-VRR-PCON.md), which describes the current driver state.

Status (2026-11-22): this is a dated log. The patches it mentions after the root-cause
section (0201 stretch cap, 0202-0204 4:2:0 and MSA-ignore switches, and the
`pcon_vrr_*` module parameters they added) have been removed. All external VRR support
is now the single patch 0200, with the full VRR range and no cap. Nothing has been
flashed to the dock.

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

## String references resolved — the `override adaptive sync` mechanism

The reference analyzer produced no string xrefs because the image is position-independent:
strings are addressed `lui reg, 0x8005x; addi reg, reg, lo12` with the firmware mapped XIP
at **`0x80020000`** (base derived from the code-pointer table at bank `0x310e8+`, whose
entries are `0x8002_314C`-style absolute addresses). A static walk over `auipc`/`addi`
pairs found nothing; walking `lui+addi` pairs with the `0x80020000` base found them.

**Referencing sites found (bank-relative):**

| String | Reference site |
|---|---|
| `Invalid MSA found on RFRM%d` (`0x31440`) | `lui@0x0e93e addi@0x0e946` |
| `RX_PARSE_MSA state: No VSC received.` (`0x31494`) | `lui@0x0e9ee addi@0x0e9f2` |
| `timeout,force send MSGQ_RX_FRM_PARSE_MSA` (`0x314bc`) | `lui@0x0ea0c addi@0x0ea10` |
| `RX_PARSE_MSA state: Using MSA, not VSC.` (`0x314e8`) | `lui@0x0ea16 addi@0x0ea1a` |
| `override adaptive sync: %02x` (`0x342a0`) | `lui@0x1c01a addi@0x1c01e` (`lui a1,0x80054; addi a1,a1,0x2a0`) |

The `0x324bc`, `0x34360`, `0x34a08`, `0x34a24`, `0x36f6c` strings were not resolved this
way yet (separated pair, other base, or still unresolved) — the VTEMP path remains to be
traced.

**`FUN_ram_0001bd9a` (`0x1bd9a`, the DPCD AUX read handler)** — decompiled, this is a
chunked 16-byte AUX read with retries. Relevant branch: when the requested DPCD address
is `0x0007` (**DP_DOWNSTREAM_PORT_COUNT**) or `0x2207`, and MMIO config register
**`0x90000257` bit 6 (`0x40`) is clear**, the handler:

```c
FUN_ram_000057aa(1, 0x800542a0, *pbVar6);  // "override adaptive sync: %02x", value
*pbVar6 = *pbVar6 & 0xbf;                  // clear bit 6 = DP_IGNORE_MSA_TIMING_PARAM
```

i.e. the firmware **masks the MSA-timing-ignore capability bit out of DPCD 0x0007 reads**
under config control — the exact bit i915's `drm_dp_sink_can_do_video_without_timing_msa()`
and amdgpu's `allow_invalid_MSA_timing_param` depend on. The gate check is at bank
`0x1c006–0x1c012` (`lui a5,0x90000; lbu a5,0x257(a5); andi a5,a5,0x40`).

**`0x90000257` config bitfield (observed):**

- bit 6 `0x40` — gate for the `override adaptive sync` masking of DPCD 0x0007/0x2207 reads
  (clear ⇒ bit masked out of returned data).
- bit 5 `0x20` — set by both known writers: bank `0x3886` (`lbu 0x257,s10; ori a5,a5,0x20;
  sb`, gated by config byte `0x9000029e` bit 1) and bank `0x10e4e` (same `ori 0x20` pattern,
  with `0x90000246 |= 0x3` immediately before). **No writer of bit 6 was found in this
  image** — with the UGREEN build's config byte `0x257 = 0x00` (flash bank offset `0x257`),
  the masking path is active by default unless something sets it at runtime.
- bit 3 `0x8` — gates a second override in the same handler: for one more address, with
  `0x90000023 >= 0x10` or bit 3 set, the byte is passed through; otherwise it is zeroed
  with log `Clear the SST monitor DSC or FEC cap: %02x` (string `0x34270`).
- bit 7 — read as a **signed** byte at bank `0x2448a` (`lb a5,0x257(a5); blt …`): negative
  ⇒ different path.

**Practical meaning for the OXP3 failure:** the bridge's own firmware can hide the
MSA-ignore capability from the host, and its AS/MSA behavior is config-gated per bit. The
earlier AUX capture that read `0x007 = 0xc2` (bit 6 set) means the mask was *not* applied in
that state — so the gate state at any given moment depends on runtime conditions still to
be traced. What sets bit 6 (or prevents it being cleared) is the next question; if it is a
vendor config byte, JSAUX's config may differ from UGREEN's here.

## Open questions

1. What sets or clears `0x90000257` bit 6 at runtime (no static writer of bit 6 was found;
   boot loader or DPCD-side path?), and which flash config byte feeds the bitfield at
   init. If it is a vendor config byte, the OXP3 VRR failure may be fixable by config —
   no reflash of code, no kernel change.
2. How the RX_PARSE_MSA state machine reacts to i915's variable-timing stream, and what
   makes it drop the picture (black vs duplicated image vs full dock reset).
3. Per-vendor config differences (UGREEN vs JSAUX): diff a second vendor's fullrom config
   table against `cayenne_bankA_config.bin`.
4. Whether JSAUX ships a newer HB1201B firmware via support (6.05.130 exists for other
   boards).

## Next steps

1. Trace what initializes `0x90000257` (the `0x2448a` signed read sits in a likely
   boot/config path; also check callers of `FUN_ram_0001bd9a` and the `0x10e42`
   `0x90000246 |= 0x3` path), and locate the flash config byte that feeds it.
2. Resolve the remaining strings (`0x34a24` "Configuring VTEMP", `0x324bc`, `0x34360`,
   `0x34a08`, `0x36f6c`) — the pair may be separated by more than 3 instructions or use a
   different base register; widen the walk or decompile the RX/TX VTEMP functions found
   via the `0x0e93e–0x0ea1a` cluster (the RX_PARSE_MSA state machine lives in that
   function region).
3. Download Unisynk 6.05.130 (and/or i-tec) fullrom; diff config tables to isolate
   per-vendor bytes.
4. Ask JSAUX support for the HB1201B (HB1201BWSXS01) firmware package; a newer VMM6210
   build is the direct fix candidate if the code turns out to be the bug.

## Read-only firmware dump of the dock (in progress, 2026-11-22)

The dock's own firmware can be read out — no flashing, no writes to flash. The channel is
the same one the vendor updaters use, reverse-engineered from fwupd's `synaptics-vmm9`
plugin (protocol `com.synaptics.mst-hid`, the RC command set: `ENABLE_RC` with the `PRIUS`
token, `GET_ID`, `MEMORY_READ`, `READ_FLASH_DATA`; commands are 62-byte HID reports:
`id=01, type=00, size, {cap, state, ctrl|0x80, sts, offset u32le, length u32le, fifo[32]}`,
`checksum = 0x100 - sum8`). Only mode switches and reads are issued — no erase/write.

- The dock's MCU is a WCH QinHeng **`1a86:fe00`** (bus 3, dev 4) with three HID
  interfaces: `hidraw3`/`hidraw4` (root-only 0600 — the update interfaces, descriptors
  unreadable without root) and `hidraw5` (0666, vendor-page FF00 with 64-byte
  Input/Output reports — the RGB/OpenRGB interface; its descriptor has no feature
  reports and it does not answer the RC handshake, confirmed by probe).
- Script: `/var/home/holzr/git/linux_scratch/vmm6210-fw/vmm6210_hid_dump.py` —
  auto-detects the interface, verifies `flash@0x4000` starts with the firmware version
  header `06 05` before dumping, reads 1 MiB in 32-byte RC chunks, writes
  `dock_flashdump_<pid>.bin` + `.sha256`. Run once as root:
  `sudo python3 /var/home/holzr/git/linux_scratch/vmm6210-fw/vmm6210_hid_dump.py run`
- Sanity anchor for the dump: the UGREEN fullrom has the same layout (EDID at 0x0, bank A
  at 0x4000 starting `06 05`, identical bank B at 0x54000). The JSAUX dump is compared
  against it for the adaptive-sync config bytes (`0x90000257`-region init) and the
  re-timing/watchdog logic identified in the MSA trace.

Once the dump lands: locate JSAUX's customer/board config (GET_ID `cid`/`bid` vs
UGREEN's), diff the config table at bank `0x4000`, and check whether a newer
`6.05.x` build exists (Unisynk 6.05.130) that changes the vertical-total re-timing
behavior — that is the root-cause fix path.

- Fixed-refresh 4K120 DSC through this PCON is stable on i915/xe; every variable-timing
  signaling attempt (AS SDP v1/v2/AVT, FreeSync SPD, HDMI-Forum translation) broke the
  external output.
- The VMM6210 accepts AUX writes to `DP_DOWNSPREAD_CTRL` but does not latch
  `MSA_TIMING_PAR_IGNORE_EN` while the stream is up; commit-time writes (patch 0023) are
  structurally unable to work and were removed. Patch 0024 forces a full modeset on VRR
  transitions so the bit is programmed during training — tested, did not resolve the
  failure (2026-11-22 boot with kmod-oxp3 …-202611220207).

# No firmware changes: the fix is on the Intel side (2026-11-22, patch 0201)

Per hardware decision: nothing gets pushed or patched on the dock. The dock's converter
re-times its HDMI output whenever the MSA Vtotal field changes, and Panther Lake hardware
puts the **VMAX register value** into that field during variable VRR (converter-side
capture: 6750 announced at the 40 Hz stretch end while the driver programmed the real
2250) — so a full 40–120 Hz stretch (3x) desyncs the converter into duplicated rows or
black. amdgpu avoids the mechanism by announcing the fixed stream vtotal; Intel hardware
takes the MSA Vtotal from VMAX during variable VRR with no documented way to change the
source (our tree already carries the upstream "program TRANS_VTOTAL from mode timing"
fix — TRANS_VTOTAL is 2250 — and the converter still saw VMAX).

Patch `0201-drm-i915-bound-pcon-vrr-stretch.patch` therefore bounds the stretch itself:
on a FreeSync PCON, `intel_vrr_compute_vmax()` caps VMAX at
`vtotal + pcon_vrr_max_stretch_pct %` (module param, default **5**, runtime-tunable,
`100` = full range). The announced MSA Vtotal then stays within 5% of the mode timing,
the converter re-times once per VRR enable/disable like any mode change, and frame
delivery still varies — real VRR inside the band (120–114 Hz at 5%; below the band
frames hold at the band floor). HDR and VRR remain enabled throughout.

Tune without rebuilding:
`echo N | sudo tee /sys/module/xe/parameters/pcon_vrr_max_stretch_pct`
(take effect on the next VRR enable — leave and re-enter Gamemode). Start at 5; widen
(10, 20, 40…) while the picture stays clean to find the converter's tolerance; if any
stretch breaks it, 1 gives the minimum non-zero band.

# Bounded stretch result: any announced-vs-actual mismatch drifts (2026-11-22)

Test matrix on the real dock, all with VRR+HDR enabled, no firmware changes:

| Stretch cap | Announced MSA Vtotal | Actual frames | Result |
|---|---|---|---|
| off (fixed) | 2250 | 2250 | perfect |
| 100% (none) | 6750 | 2250–6750 | duplicated rows / black |
| 5% | 2362 | 2250–2362 | glitched |
| 2% | 2295 | 2250–2295 | **vertical shake (drift), no duplication** |

The 2% result is the decisive one. The converter emits output frames of the *announced*
length and takes content from input frames as they arrive; when an input frame is shorter
than the announced length, the next input frame's first lines are consumed to fill the
tail, so the active content's position drifts within the output window every frame —
that is the up/down shake. A mismatch of 3x multiplies rows; a mismatch of 45 lines
drifts them. **Any** announced-vs-actual mismatch glitches; only announced == actual for
every frame is clean.

Intel PTL hardware announces **VMAX** (the stretch ceiling) as the MSA Vtotal during
variable VRR (capture: 6750 while the driver programmed 2250; our tree already carries
upstream's "program TRANS_VTOTAL from mode timing" fix, and the converter still saw
VMAX). The actual frame length varies between vmin and vmax. Therefore announced == actual
for every frame is only possible with vmax == vmin — i.e. no band, no variable refresh.
amdgpu's advantage is that it announces the fixed stream vtotal (constant 2250) while
frames stretch above it: the converter waits for each input frame and delivers it as one
output frame at a variable interval — the TV sees 2250-line frames at 40–120 Hz, which is
exactly VRR. Intel hardware cannot produce that combination: the announced value is the
same register as the stretch ceiling (VMAX).

Consequence: no stretch width can fix this from the kernel side while PTL announces VMAX.
The parameter stays (`pcon_vrr_max_stretch_pct`) as the experiment carrier, but the two
remaining paths are:

1. **The decisive AMD-side capture**: run the same converter memory read on the Legion Go 2
   (same dock, same TV) while AMD VRR is active in a game. If AMD's announced MSA Vtotal is
   a constant 2250 during stretch, the Intel behavior is the sole difference and the
   conclusion is final; if AMD also announces 6750 and still works, the converter's behavior
   differs by signaling (FreeSync VSIF vs AS SDP handling) and AMD's exact packet set can be
   replicated. Either answer ends the search.
2. **The Intel bug report** (drafted from this doc): PTL hardware puts the VMAX register
   value into the MSA Vtotal field during variable VRR, breaking DP-to-HDMI converters that
   trust MSA per-frame timing, contrary to the DP adaptive-sync model where the sink uses
   the AS SDP and ignores per-frame MSA when MSA_TIMING_PAR_IGNORE is set. Upstream
   references: "hardware takes the MSA Vtotal from the VMAX register"
   (mail-archive.com/intel-gfx msg354316) and "DP transcoder uses TRANS_VTOTAL to derive MSA
   VTotal" (ratatoskr.run/intel-xe 2026/06/17144946).

# Windows works on this device — the wall is driver behavior, not hardware (2026-11-22)

The user confirms VRR works on this same OXP3 + dock + TV under Windows. That invalidates
any "Intel hardware cannot" framing: the Intel Windows driver presents the converter a
configuration our stack does not. Current runtime-tunable candidates, no rebuild needed:

1. `xe.pcon_vrr_max_stretch_pct=0` — announced 2251, frames pace at 2251: announced ==
   actual whenever content is below ~120 fps. Isolates whether constant-MSA + VRR
   signaling is stable. (Already testable with the installed 0201 kmod.)
2. `xe.pcon_as_sdp=1` — v1 AS SDP with AVT payload (the packet carrying the VRR range to
   the converter; a PCON translates AS SDP into HDMI VTEM per VESA).

Either outcome discriminates the next move: if (1) is stable, the converter is purely
MSA-driven and the search is for what register state Windows presents during stretch; if
(1) still glitches, the signaling packets themselves are a lever (isolate AS SDP vs
FreeSync SPD vs VTEM). A Windows-side capture of the converter's frame-header memory (the
same registers Astra read from Linux) is the decisive comparison target now that the
device dual-boots Windows.

# The 4K failure isolated to two source-side differences (2026-11-22, patch 0202)

The full-session RC capture (2026-11-22 17:20, ladder at 1080p) plus the 4K ladder
(pct 2/1/0, all black with UI flashes) and the MSA decode give the complete picture:

- VRR engages with the announced header at exactly `vtotal x cap` (1125 -> 1181 = +5%
  live-confirmed); the converter's saved copy follows within 0.1 s.
- `vendor_type` never leaves 0 through a whole session — the FreeSync VSIF does not
  engage a vendor path, and DPCD 0x300 ignores writes (read-back empty after i915's
  own init write and after a manual write). Classification-by-source is unreachable.
- FAVT parsing = false (0x2214 bit2), so the AS SDP cannot carry timing to the
  converter. AS-SDP v1 header-only is the only packet the converter parses.
- MSA decode: bytes 0-1 htotal, 2-3 vtotal, 8-9 hactive, 10-11 vactive,
  byte 19 = MISC1 (bit 6 = YCbCr 4:2:0).
- **Desktop 4K120 (no VRR, stable): MISC1 = 0x40 -> YCbCr 4:2:0.**
- **Gamemode 4K VRR (black at cap 2%, 1% and 0%): MISC1 = 0x00 -> RGB.**
- 1080p Gamemode VRR (no DSC on the link): works; a game ran 73 s with the watchdog
  re-timing as designed.

So the 4K black is not the retiming width (even cap 0 with announced 2251 blacks) —
it is the format/packet combination on the DSC link. The desktop proves 420+DSC
without VRR is stable on this converter. Patch 0202 adds two runtime knobs to
bisect it:

    xe.pcon_vrr_force_420     # prefer YCbCr 4:2:0 first in the VRR path (desktop parity)
    xe.pcon_vrr_no_msa_ignore # skip DP_MSA_TIMING_PAR_IGNORE_EN (whose ignore-MSA
                              # semantics can desync the converter's DSC decoder)

Test ladder at 4K (each = set param, leave and re-enter Gamemode):
1. `pcon_vrr_force_420=1` — 4:2:0 like the desktop;
2. `pcon_vrr_no_msa_ignore=1` — VRR signaling without the MSA-ignore bit;
3. both together.
`xe.pcon_vrr=0` remains the full off switch. Run the RC peek detached alongside;
its per-transition dumps now also record the converter HDMI link mode (0x3036,
changed 0x3f -> 0x3e across the modeset) and the source OUI state after each reset.

# force_420 was a silent no-op; patch 0203 makes it real (2026-11-22)

The bisection round's capture shows why the 420-first knob changed nothing: all three
steps ran at stretch 5 (announced 2363) with MISC1 = 0x00 (RGB) — the 420 attempt
never reached the wire. Two reasons stacked: 4:2:0 at 10 bpc needs ~15 Gbps (fails the
2-lane HBR3 budget), and `intel_dp_sink_format_valid()` rejects 4:2:0 for any mode the
TV's EDID does not declare as 4:2:0 — and Gamemode's generated modes are never
EDID-declared 4:2:0, unlike the desktop's VIC-97 4K120 mode.

Key bandwidth facts for this link (2 lanes HBR3, ~12.96 Gbps effective):

- 4K120, any format, any bpc: needs DSC (4:2:0@8bpc alone is 14.26 Gbps per the
  upstream comment) — there is no DSC-free 4K120.
- 4K60 4:2:0 @ 10 bpc: ~8.9 Gbps — **fits without DSC, HDR included**. This is the
  exact analog of the working 1080p120-RGB state, at 4K.

Patch 0203 makes `xe.pcon_vrr_force_420` skip the EDID 4:2:0 checks so the VRR path
actually runs 4:2:0. The decisive test is 4K60 with the knob on:

    echo 1 | sudo tee /sys/module/xe/parameters/pcon_vrr_force_420
    # modes.cfg: 3840x2160@60, leave and re-enter Gamemode

Outcomes:
- 4K60 4:2:0 VRR stable -> the converter's VRR machinery works on Intel whenever DSC
  is out of the picture. 4K60 VRR+HDR becomes the working 4K option, and 4K120 VRR is
  then provably blocked by DSC+VRR on this converter — the precise, evidenced Intel
  bug-report claim (Windows does 4K120 VRR+DSC through the same silicon, so the
  difference is in the DSC+VRR source presentation, not the hardware).
- 4K60 4:2:0 VRR still broken -> the failure tracks the VRR signaling itself at 4K,
  independent of DSC and format; the capture from that run is the report's core.

Also recorded: the bisection steps at 4K ran as RGB at announced 2363 (unstable /
black / TV "invalid format"); the transient `00 10 fa .. "HDMI"` source-OUI ghost
reappeared once after a reset (a second chip in the dock identifying itself through
the converter's DPCD 0x300 area — noted, not a lever).

# 4K60 round with 0203: RGB again, dock drop on the VRR flip (2026-11-22 18:04)

The 0203 run's capture shows Gamemode at 4K announcing 2363 (stretch 5, post-install
default) with MISC1 = 0x00 — RGB, the 420 path never reached the wire. The knob
defaulted off and needed a post-reboot echo, and its failure reason logged only at
debug level, so a missed echo and a failed 4:2:0 config were indistinguishable. Fixed
in 0204: `pcon_vrr_force_420` defaults to true (nothing to set after a reboot) and
every 4:2:0 fallback prints an `oxp3-vrr: 4:2:0 VRR config failed (<err>)` line in the
plain journal, plus an `oxp3-vrr: VRR path using YCbCr 4:2:0` confirmation when it
succeeds.

Also captured: at 4K the VRR flip (fixed 2250 -> VRR 2363 as a fastset ~25 ms apart)
was followed by the dock dropping off the bus ~5 s later ([Errno 6] No such device) —
the original hard failure, independent of the stretch width, at RGB. The MISC1 byte
of the next capture decides: 0x40 means 4:2:0 is on the wire and the DSC-free 4K
hypothesis is being tested for real; 0x00 with an `oxp3-vrr: ... failed (<err>)`
journal line names the exact gate that rejected 4:2:0.
