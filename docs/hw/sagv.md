# SAGV / memory-frequency switching (System Agent Geyserville, QCLK-GV points)

Target: what the vendor's Windows stack does with the per-profile `sagvPoint` (0/3/4), what Linux
exposes for it on Panther Lake at kernel `7.2.7-an28.fc45.x86_64`, and whether anything can be closed
from userspace. **Verdict up front: nothing can be closed from userspace.** There is no writable
interface for the SAGV/QCLK-GV point at this kernel version, and the only SAGV knob that exists at all
is the boot-time display parameter `xe.enable_sagv`, which can only *disable* SAGV. The vendor's
per-profile point pinning is a Windows-only path through an MCHBAR PCU register pair that the Linux
driver does not map and does not own.

Measured on 2026-11-09 (one boot, no reboots, nothing written to the host; every privileged command is
written out, none executed). Tool shipped: [`tools/oxp3-sagv.sh`](../../tools/oxp3-sagv.sh) (read-only).
Vendor material in `~/Downloads/oxp3/` was treated as untrusted data, read only.

---

## TL;DR

1. **Windows**: `PerfMode.exe` + `SAGVPlugin.dll` pin the SAGV point by writing **MCHBAR + 0x5DA0**
   (the point) and **MCHBAR + 0x5DA4** = `0x80000122` (apply, bit31 = busy), where MCHBAR comes from
   **PCI 0:0.0 config space 0x48**. Reads use `0x80000022` (latch) + 0x5DA0, and the memory-frequency
   readout comes from **MCHBAR + 0x5918 = SA_PERF_STATUS** (QCLK ratio). The per-profile values are
   Quiet 3 / Balanced 0 / Performance 4 (`sagvPoint` in `perf_conf.ini`), plus an `AutoSAGVProc`
   worker that moves the point between 0 and 3/4 from GPU utilisation.
2. **Linux (`xe`)**: has only `xe.enable_sagv` — a display parameter with mode **0400**, i.e.
   root-readable and *not writable at runtime*; it is a boot/cmdline switch and it can only disable
   SAGV. There is no sysfs, debugfs, module parameter, ioctl or DRM property for the point. The only
   node a `/sys` name scan finds (depth 7, `tools/oxp3-sagv.sh probe`) is that parameter file.
3. On this platform the driver does not even choose a point: with display version 30 (≥ 14)
   `intel_pmdemand_atomic_check()` sends a PM DEMAND request with `qclk_gv_index = 0` and the comment
   *"firmware will calculate the qclk_gv_index"*, and `icl_pcode_restrict_qgv_points()` returns early
   for ≥ 14. So the QCLK-GV point (the thing the Windows tool pins) is selected inside the PCU
   firmware, out of reach of both the kernel and userspace.
4. **What is worth doing instead**: measure it. `tools/oxp3-sagv.sh measure` (plus the `dram` and
   `psys` domains now published by `files/rapl-power`) gives the memory-rail and platform power so the
   SAGV benefit can be quantified with an A/B against `xe.enable_sagv=0`, and `tools/sleep-power-measure.sh`
   already covers suspend windows. §5 has the protocol.
5. **If it is ever to be fixed**, the missing piece is a *point override in the PM DEMAND path*, not an
   MCHBAR poke (§6). No kernel patch is written here, by instruction.

## 1. Vendor evidence: what the Windows tool actually writes

Extracted read-only from `OneXConsole_0.10.4-fix1.exe` → `$PLUGINSDIR/app-64.7z` →
`resources/resources/tools/intel/` into `~/.cache/sagv/` (nothing committed):

| file | role |
|---|---|
| `perf_conf.ini` | the three profiles; `sagvPoint` = 3 / 0 / 4 for Quiet / Balanced / Performance |
| `PerfMode.exe` | profile engine; strings `SAGVPointToSet=`, `SAGVPointCurrent=`, `AutoSAGVProc`, `AutoSAGVPolicy`, `SAGV_Point GPU PL1`, `StartAutoSAGVPolicy`, `QCLK Operations` |
| `SAGVPlugin.dll` | the actual point I/O; exports `InitializePlugin`, `CleanupPlugin`, **`SetSAGVPoint`**, **`GetSAGVPoint`**, `GetSAGVPointConf`, **`GetMemFreq`** |
| `HWAPIExtDLL.dll` / `CCHWAPIExt.sys` | generic helpers: `ReadMSR`, `WriteMSR`, `PCIRead`, `ReadMMIOStored`, `WriteMMIOStored`, `ReadPort`, `WritePort`, … |

Decompilation used the isolated wrapper (`tools/ghidra-pod.sh sagv PerfMode.exe …` for the engine and
`sagv-plug SAGVPlugin.dll …` for the plugin; dumps in `~/.cache/ghidra-agents/{sagv,sagv-plug}/out/`).

### 1.1 The register pair

`SAGVPlugin.dll!SetSAGVPoint(point)` (0x1800077E0 → body 0x180007AA0) after `HWAPIInitialize`:

```
MCHBAR = PCI config 0:0.0 offset 0x48, 8-byte read, enable bit stripped   ("Failed to get MCHBAR address")
wait:  read  MCHBAR+0x5DA4, bit31 must be clear (≤10 ms, "MMIO Ops busy" otherwise)
write  MCHBAR+0x5DA0 = point                      (32-bit, the sagvPoint number)
write  MCHBAR+0x5DA4 = 0x80000122                 (apply; bit31 = busy, bit8 set)
log    "Set SAGV Point <n>"
```

`GetSAGVPoint()` (0x1800083E0):

```
write  MCHBAR+0x5DA4 = 0x80000022                 (latch/select for read-back)
read   MCHBAR+0x5DA0 → current point
```

`GetMemFreq()` (0x18000A0F0):

```
read   MCHBAR+0x5918 → SA_PERF_STATUS             (QCLK ratio; kernel name below)
decode with a per-platform 0x30-byte-entry table indexed by CPUID+PCI ID  ("Cannot get memory frequency")
```

Evidence trail: strings `MSR Access Error`, `MMIO Access Error`, `PCI Config Error`,
`Cannot get memory frequency`, `Failed to get MCHBAR address`, `Error in calling WriteMMIOStored.
Please make sure the bar address, offset is present in Allowlist …`; disassembly at 0x180003491
(`mov $0x48,%edx` PCI read), 0x1800034C2 (`dec %rax` = strip the enable bit), 0x180004BA0 (write
helper), 0x180004850 (read helper). The plugin resolves `PCIRead`, `ReadMSR`/`WriteMSR`,
`ReadMMIOStored`/`WriteMMIOStored` from `HWAPIExtDLL.dll`, and the SAGV path uses the **MMIO** pair —
no MSR is written for SAGV (MSRs are used for the PL1/PL2/PL4 limits, see
[power-fan.md](power-fan.md)).

### 1.2 The auto policy (reconstructed, not executed)

`AutoSAGVProc` samples the GPU utilisation three times per `TIME_INTERVAL` (10 s from
`perf_conf.ini`), then:

* if no sample is below `GPU_UTIL_THRESHOLD_LOW` (80 %) and more than two are at/above
  `GPU_UTIL_THRESHOLD_HIGH` (90 %): `point = 3 + (PL1 >= PL1_THRESHOLD ? 1 : 0)` → **4** at 35 W,
  **3** at 10/25 W;
* otherwise **0**.

That matches the static per-profile values (Quiet 3, Balanced 0, Performance 4) and is applied with
the same `SetSAGVPoint` call; the UI toggle is the "Auto SAGV" policy switch found earlier in
[gap-audit.md](gap-audit.md) §2.1. The *meaning* of the numbering (which point is which QCLK/frequency)
is not decoded — see open points.

## 2. Linux exposure matrix (measured this boot)

`xe` is the driver (`/sys/class/drm/card1/device/driver → /sys/bus/pci/drivers/xe`), device
`8086:b080`, display version **30.00 stepping B0** (`journalctl -k`: *"Found pantherlake (device ID
b080) integrated display version 30.00 stepping B0"*), DMC `xe3lpd_dmc.bin` v2.20.

| interface | path | present | writable | note |
|---|---|---|---|---|
| SAGV enable (driver param) | `/sys/module/xe/parameters/enable_sagv` | **yes** | **no** (`-r--------`, mode 0400) | boot/cmdline only; can only *disable* SAGV |
| SAGV point / QCLK-GV node | — | **absent** | — | whole-`/sys` name scan found only the parameter above |
| debugfs display params | `/sys/kernel/debug/dri/<N>/xe_params/` | created by the driver (`intel_display_debugfs_params.c`), root-only | no | each file gets the parameter's own mode (0400); `/sys/kernel/debug` itself is `0700 root`, so this could not be listed from userspace |
| debugfs SAGV/QGV state | — | **absent** | — | no SAGV/QGV file in `intel_display_debugfs.c` or `xe_debugfs.c` (source review) |
| sysfs QoS / pm_qos | `/dev/cpu_dma_latency` | yes | `0600 root` | CPU DMA latency; unrelated to SAGV |
| intel_pstate | `/sys/devices/system/cpu/intel_pstate/*` | yes | `no_turbo` etc. | CPU frequency only; no SAGV relation |
| intel_uncore_frequency | `…/intel_uncore_frequency/package_00_die_00/{min,max}_freq_khz` | yes | root | **MSR 0x620/0x621 uncore ratio** (ring/fabric), *not* QCLK/memory |
| RAPL zones | `intel-rapl:0` (package-0, core, uncore, dram), `intel-rapl:1` (psys), `intel-rapl-mmio:0` (+dram) | yes | energy `0400 root` | the measurement surface (§5) |
| MCHBAR | PCI 0:0.0 config 0x48 | config readable by user, **but only 64 bytes** | — | the 0x48 dword read returns EOF for an unprivileged reader; host-bridge `resource` file has no mapped BAR |
| platform profile | `/sys/firmware/acpi/platform_profile` | yes (`balanced`) | root | DPTF/EPP policy, no SAGV effect |
| GT frequency | `…/tile0/gt0/freq0/{min,max}_freq` | yes (e.g. 650–2300 MHz at this profile) | root | adjacent knob, not SAGV |

`tools/oxp3-sagv.sh probe` reproduces this table and returns nonzero because there is nothing writable.

## 3. Why the point cannot be set: the kernel path on PTL

Read from the matching source tree (`kbuild-an19-src`, `VERSION 7.2.7`):

* `HAS_SAGV()` is true (display version ≥ 9, not BXT/GLK), and the SAGV block time on ≥ 14 comes from
  `MTL_LATENCY_SAGV` (0x4578C, bits 12:0) rather than the PCODE mailbox
  (`drivers/gpu/drm/i915/display/skl_watermark.c`).
* `intel_crtc_can_enable_sagv()` gates on `display->params.enable_sagv` and on per-plane watermarks;
  the result becomes `pipe_sagv_reject` in the bandwidth state. When SAGV is rejected,
  `icl_find_qgv_points()` sets `qgv_point_peakbw = U16_MAX` (*"disable the pcode SAGV by passing all
  1's for qgv peak bw in PM Demand request"*). That is how `xe.enable_sagv=0` actually reaches the
  hardware.
* `icl_pcode_restrict_qgv_points()` — the pre-MTL way of masking QGV points via
  `ICL_PCODE_SAGV_DE_MEM_SS_CONFIG` — starts with `if (DISPLAY_VER(display) >= 14) return 0;`.
* Point selection on ≥ 14 lives in `intel_pmdemand.c`: a PM DEMAND request is written to
  `XELPDP_INITIATE_PMDEMAND_REQUEST(dword)` = **0x45230 + 4·dword**. dword 0 carries the payload
  (`QCLK_GV_BW` = bits 31:16, `VOLTAGE_INDEX` = bits 14:12, `QCLK_GV_INDEX` = bits 11:8, pipe mask);
  dword 1 carries `CDCLK`/`DDICLK` and the `REQ_ENABLE` trigger in bit 31, and is cleared by the
  firmware when the transaction completes. `intel_pmdemand_atomic_check()` sets
  `params.qclk_gv_index = 0` with the comment *"firmware will calculate the qclk_gv_index"* — the
  kernel never pins a point; it only states a bandwidth requirement.
* MCHBAR is read-only for the driver and the mirror is gone on ≥ 14: `include/drm/intel/mchbar_regs.h`
  (*"Starting from Haswell, you can't write registers using the MCHBAR mirror, just read. On MTL+ the
  mirror no longer exists"*), and `intel_mchbar.h` exposes only `intel_mchbar_read*()`, gated by
  `has_mchbar_mirror()` (`DISPLAY_VER < 14`). `SA_PERF_STATUS_0_0_0_MCHBAR_PC` is
  `MCHBAR + 0x5918` — exactly the register `GetMemFreq` reads.

Consequence: the vendor's write target (MCHBAR + 0x5DA0/0x5DA4) is neither mapped nor defined in
Linux, and the point the vendor pins is chosen by firmware on Linux. There is nothing to write from
userspace, and nothing to *read* either without root (MCHBAR is not exposed).

## 4. What this repo ships

### 4.1 `tools/oxp3-sagv.sh` (read-only)

```
tools/oxp3-sagv.sh status              # driver, display version, SAGV exposure, RAPL, adjacent knobs
tools/oxp3-sagv.sh probe               # exhaustive scan for any writable SAGV/QGV/QCLK interface
tools/oxp3-sagv.sh set <0-7> --dry-run # explains the register facts and the missing ABI; writes nothing
tools/oxp3-sagv.sh set off|on          # prints the only real knob: xe.enable_sagv=0 boot parameter
tools/oxp3-sagv.sh measure [-s SEC]    # avg/min/max W for package-0/core/uncore/dram/psys
tools/oxp3-sagv.sh report              # status + a 10 s measurement
```

No root is needed for anything; the tool never writes. `set` exits 3 for a point (no interface) and 0
for the boot-parameter explanation. `SAGV_RAPL_DIR=<dir>` overrides where it reads the 1 Hz averages
(used for testing).

### 4.2 `files/rapl-power` now publishes `dram` and `psys`

`tools/sleep-power-measure.sh` already reads psys and package-0, but `/run/rapl-power` only carried
`package-0`, `core`, `uncore`, so unprivileged measurement of the **memory rail** was impossible. The
publisher now also emits `dram` (memory domain, the rail SAGV modulates) and `psys` (whole platform),
unchanged 1 s / 0.1 W averaging, same service sandbox (`RuntimeDirectory=rapl-power`, 0755). Existing
consumers (MangoHud) are unaffected. Reinstall on the host (privileged, not run here):

```bash
sudo install -D -m 0755 files/rapl-power /usr/local/libexec/rapl-power
sudo systemctl restart rapl-power.service
tools/oxp3-sagv.sh measure -s 60      # now includes dram and psys
```

`RAPL_POWER_OUT` / `RAPL_POWER_ROOT` exist only so the loop can be tested off-host; the unit's
`ProtectSystem=strict` means the real path must stay `/run/rapl-power`.

## 5. Quantifying SAGV: measurement recipe

### 5.1 What SAGV affects, and when

SAGV lets the PCU drop the system-agent/QCLK (memory + fabric) operating point while the display can
tolerate the added memory latency. It therefore moves the **`dram`** domain first, then **`uncore`**
and **`psys`**; it does **not** move `core` (CPU) directly, and it is *disabled by the display driver's
own watermark rules* whenever the current mode/pipe configuration needs the bandwidth. Practical
consequence: any effect is an idle/light-desktop effect (static desktop, video, low-refresh content),
not a gaming effect — in a game the point is already pinned high by display bandwidth demand.

### 5.2 A/B protocol (two boots, because there is no runtime switch)

Same physical scenario both times: AC disconnected, battery ≥ 50 %, screen brightness fixed, KDE
idle with no browser/compositor animation, Wi-Fi associated but idle, keyboard backlight off.

```bash
# boot A -- default (SAGV enabled by the driver/firmware)
tools/oxp3-sagv.sh measure --seconds 120 | tee /tmp/sagv-A.txt

# boot B -- SAGV forced off (privileged, then reboot; undo with --delete-if-present)
sudo rpm-ostree kargs --append-if-missing=xe.enable_sagv=0
sudo systemctl reboot
tools/oxp3-sagv.sh measure --seconds 120 | tee /tmp/sagv-B.txt
sudo rpm-ostree kargs --delete-if-present=xe.enable_sagv=0   # back to A, then reboot
```

`B − A` on `dram` is the memory-rail power SAGV is worth in that scenario; `psys` gives the whole-machine
figure to compare against battery draw (`/sys/class/power_supply/BAT0/power_now`, or
`tools/sleep-power-measure.sh snapshot`). Run each boot's measurement twice and discard the first
minute (page cache, radios settling).

Shortcut if a reboot is not convenient: a *within-boot* proxy for "the point is moving" is to change
display demand and watch the `dram`/`uncore` domains — a static desktop versus a full-screen 144 Hz
animation at the same brightness. That does not give the SAGV-on/off delta, but it shows the memory
rail responding to the same mechanism.

### 5.3 Expected magnitude, honestly

The delta is bounded by the difference between the highest and lowest QCLK-GV point on the memory
rail. On a 1920×1200 LPDDR5X handheld the `dram` domain at idle is typically a few watts and the
per-point step is a fraction of a watt to about a watt; `psys` will show less than the sum because the
rest of the SoC absorbs part of it. Convert to battery life with the pack this unit reports
(`energy_full` 87.665 Wh, see [battery-tdp.md](battery-tdp.md)): **1 W ≈ 1.14 %/h**, so a 0.5 W SAGV
benefit is ≈0.6 %/h — i.e. at or below the 1 %/h PASS line of
[`tools/sleep-power-measure.sh`](../../tools/sleep-power-measure.sh), which is why the suspend path
([sleep-power.md](sleep-power.md)) matters far more than SAGV for real drain.

Treat "≈0.3–1 W at idle" as an **expectation to verify** with §5.2, not a measured result: this session
had no reboots and no root, so the A/B was not run. Use the `intel-rapl-mmio:0` zone (package-0, with
its own `dram` child) as a cross-check that the MSR-domain numbers are not stale. Also note the vendor's
Quiet profile is dominated by its 10 W PL1 (see [power-fan.md](power-fan.md)) — the SAGV point is the
smaller half of that profile.

### 5.4 Suspend windows

For "asleep" drain use the existing tool (it already reads psys + package-0 + battery and validates a
real s2idle window):

```bash
tools/sleep-power-measure.sh run -w 600      # 10 min window, PASS < 1.0 %/h
```

SAGV does not apply in s2idle; that measurement is only the sanity check that the idle baseline is not
polluted by a machine that did not actually suspend (see [sleep-power.md](sleep-power.md) §2.5 for the
measurement bug that once produced that impression).

## 6. If this is ever to be fixed (documented only — no patch here)

The missing upstream interface is: **a way to pin or bound the QCLK-GV (SAGV) point.** Nothing in
`drm`, `xe`, `powercap` or `platform/x86` offers it. The minimal reviewed design would be:

1. **Where**: `drivers/gpu/drm/i915/display/intel_pmdemand.c` (shared by `xe` display), plus a small
   debugfs file next to the other display state under `/sys/kernel/debug/dri/<minor>/` (0600), and/or
   a module parameter `sagv_force_point` (int, default -1 = off) so it can be set at boot.
2. **What it must do**: override `new_pmdemand_state->params.qclk_gv_index` with the requested point and
   clamp `qclk_gv_bw` to that point's `peakbw` from `display->bw.max[].peakbw` (the driver already has
   the per-point table from `intel_read_qgv_point_info()`); force a full atomic commit so the PM
   DEMAND request is re-sent; honour `intel_pmdemand_check_prev_transaction()` and `REQ_ENABLE`
   (bit 31) handshaking; refuse (or log loudly) if the requested point's bandwidth is below the
   current mode requirement, because that is exactly the case i915 avoids by only restricting QGV
   points when watermarks allow it.
3. **What it must not do**: poke MCHBAR + 0x5DA0/0x5DA4. That register pair is not defined in
   `include/drm/intel/mchbar_regs.h`, the MCHBAR mirror does not exist for display version ≥ 14, the
   driver has read-only MCHBAR accessors only, and an undocumented PCU mailbox write would fight both
   the PM DEMAND path and the PCODE owner. It is also unverifiable without a Windows-side trace.
4. **`oxpec` is the wrong layer**: the OXP EC carries fan/battery/charge fields
   ([power-fan.md](power-fan.md), [battery-tdp.md](battery-tdp.md)); SAGV is SoC/PCU policy. No
   `oxpec` change would help.
5. **Cheapest useful upstream piece first**: a read-only debugfs dump of the live PM DEMAND parameters
   (`qclk_gv_bw`, `qclk_gv_index`, voltage index, pipes, cdclk) — that makes the firmware's point
   choice observable and is reviewable on its own. Only then a write path.

## 7. Risks

* **Display underruns / hangs** if a point is pinned below what the current mode needs. This is why
  mainline restricts QGV points in the watermark path instead of exposing a knob.
* **`/dev/mem` is not a workaround**: `CONFIG_STRICT_DEVMEM=y` and `CONFIG_IO_STRICT_DEVMEM=y` are set,
  MCHBAR is in MMIO space, and any write would bypass the driver's bookkeeping and could corrupt
  PM DEMAND state. Not shipped, not recommended.
* **`xe.enable_sagv=0` raises idle power** — it is a measurement reference, not a power setting.
  It is a boot parameter (0400), so changing it means a `rpm-ostree kargs` edit and a reboot; rollback
  is `--delete-if-present=xe.enable_sagv=0`.
* **Counter trust**: `dram`/`psys` RAPL domains are model-specific. Verify they advance before
  believing them: `sudo sh -c 'a=$(cat /sys/class/powercap/intel-rapl:0:2/energy_uj); sleep 5; b=$(cat …); echo $((b-a))'`.
* **Attribution**: `dram` also moves with memory *traffic*, not only frequency; keep the workload
  identical between A and B, and prefer the delta over absolute values.

## 8. Open points

1. **Point numbering → frequency** is not decoded. `GetSAGVPoint`/`SetSAGVPoint` move a bare 0–7 index;
   which QCLK/DCLK each index selects would need either a Windows-side run of `GetMemFreq` per point or
   a full decode of the 0x30-byte per-platform table at `0x180017D28` in `SAGVPlugin.dll`.
2. **Does the vendor mailbox conflict with Linux's PM DEMAND?** Untested. If both are active, the PCU
   sees two owners of the same point; a Windows measurement (write point, read `SA_PERF_STATUS`) would
   answer it without touching this host.
3. **The A/B power delta was not measured here** — no reboots and no root in this session. §5.2 is the
   exact protocol; the numbers are still open.
4. **`dram` domain liveness on this SKU** is assumed from the powercap directory naming, not verified
   (root-only counters). If it is dead, `psys` + battery remain.
5. **Why the host-bridge config read stops at 0x40** for an unprivileged reader is unexplained (a
   normal PCIe config file yields 256 bytes). Root tools (`setpci`/`lspci -xxx`) are unaffected; the
   tool prints the exact root command instead of guessing.
6. **`enable_sagv` on this kernel is a display parameter, not an i915 parameter file**; on an
   `i915`-bound system the same switch is `i915.enable_sagv`. The tool keys off the bound driver.

## 9. Reproduction

```bash
# exposure
tools/oxp3-sagv.sh status; tools/oxp3-sagv.sh probe
ls -l /sys/module/xe/parameters/enable_sagv            # 0400 -> not writable
journalctl -k -b | grep -E 'integrated display version' # 30.00 stepping B0
ls /sys/class/powercap; for z in /sys/class/powercap/*; do echo "$z $(cat $z/name)"; done

# vendor evidence (read-only, untrusted data; nothing committed)
7z e -so ~/Downloads/oxp3/OneXConsole_0.10.4-fix1.exe '$PLUGINSDIR/app-64.7z' > /tmp/app-64.7z
7z x /tmp/app-64.7z 'resources/resources/tools/intel/*' -o/tmp/intel
cat /tmp/intel/resources/resources/tools/intel/perf_conf.ini
tools/ghidra-pod.sh sagv-plug /tmp/intel/resources/resources/tools/intel/SAGVPlugin.dll '.*'
grep -n "0x5da0\|0x5da4\|0x5918" ~/.cache/ghidra-agents/sagv-plug/out/decomp.txt

# kernel-side source of truth (matching 7.2.7 tree)
grep -n "sagv\|qclk_gv_index\|PMDEMAND" \
    drivers/gpu/drm/i915/display/intel_pmdemand.c \
    drivers/gpu/drm/i915/display/skl_watermark.c \
    drivers/gpu/drm/i915/display/intel_display_params.c
grep -n "0x5918\|MCHBAR mirror" include/drm/intel/mchbar_regs.h

# measurement
tools/oxp3-sagv.sh measure --seconds 60
```

Nothing in this document was executed as root, no reboot was performed, and no vendor binary, DLL,
decompiled listing or firmware is stored in this repository.
