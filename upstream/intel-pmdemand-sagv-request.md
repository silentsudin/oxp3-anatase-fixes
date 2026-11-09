# RFC draft — `intel_pmdemand`: make the QCLK-GV (SAGV) point observable and pinnable on Panther Lake

Status: **draft, not submitted**, and deliberately **not a finished patch**. No account/permissions
for upstream here, no root on the unit and no reboot was performed. The code in §7 is our own sketch
and has not been compiled; §8 lists the points a maintainer must settle before a submission.

Prepared: 2026-11-09. Source references are to the 7.2.7 tree we have
(`drivers/gpu/drm/i915/display/`, shared by `xe` display); the unit runs
`7.2.7-an28.fc45.x86_64` with `8086:b080`, display version 30.00 stepping B0, `xe` bound.

Related local evidence: [`docs/hw/sagv.md`](../docs/hw/sagv.md) (vendor register pair, Linux
exposure matrix, measurement recipe), [`docs/hw/gap-audit.md`](../docs/hw/gap-audit.md) row 9 and
the 2026-11-09 follow-up, [`docs/hw/power-fan.md`](../docs/hw/power-fan.md) §2.1 (the `sagvPoint`
values in the profile file).

---

## 1. The ask

The vendor exposes a per-profile **QCLK-GV / SAGV point** (Quiet 3, Balanced 0, Performance 4) and
an “Auto SAGV” policy that moves it from GPU utilisation. Linux has no interface for the point, and
on display version ≥ 14 the kernel does not choose it either: `intel_pmdemand_atomic_check()` sends
`qclk_gv_index = 0` and lets the PCU firmware select. We propose:

1. a **read-only** debugfs state file that decodes the live PM DEMAND request plus the
   `SA_PERF_STATUS` QCLK ratio — cheap, reviewable, immediately useful to debug firmware behaviour;
2. a **development-only, root-only point override** (`sagv_force_point`, 0600 debugfs, also a
   read-only module parameter) that pins the QCLK-GV index in `intel_pmdemand` and selects the
   matching PM DEMAND bandwidth, **clamped to the display bandwidth the current mode needs**;
3. no MCHBAR pokes, no new uAPI and no policy in the kernel. Userspace (HHD/a daemon) may drive it
   once the mechanism is proven, but the point is a development knob until then.

The open technical question is whether the PM DEMAND `QCLK_GV_INDEX` field pins anything on PTL at
all, or whether the PCU ignores it exactly as the existing comment says. The override is how to find
out under controlled conditions; the read-only file is worth having either way.

## 2. Recovered vendor facts (what we know, from our own prior work)

All values below were extracted read-only from the OneXConsole 0.10.4 payload and decompiled with the
isolated wrapper; no vendor binary or listing is in this repository. Full trail:
[`docs/hw/sagv.md`](../docs/hw/sagv.md) §1.

| fact | value |
|---|---|
| MCHBAR base | PCI `0:0.0` config space `0x48`, 8-byte read, enable bit stripped |
| point register | MCHBAR + `0x5DA0` (32-bit) |
| apply / handshake | MCHBAR + `0x5DA4` = `0x80000122` (bit 31 = busy, polled ≤ 10 ms) |
| read handshake | MCHBAR + `0x5DA4` = `0x80000022`, then read `+0x5DA0` |
| memory-frequency status | MCHBAR + `0x5918` (`SA_PERF_STATUS`, QCLK ratio) |
| vendor points | Quiet 3 / Balanced 0 / Performance 4 (`sagvPoint` in `perf_conf.ini`) |
| auto policy | sample GPU utilisation; high if ≥ 90 %, low if < 80 %; interval 10 000 ms; `point = 3 + (PL1 >= 25 W ? 1 : 0)` |

The *meaning* of the point numbers (which QCLK/DCLK each selects) is **not decoded**; the plugin
carries a per-platform table it uses for `GetMemFreq`. Do not encode frequency assumptions in a
patch.

## 3. Why Linux has only a read-only `enable_sagv` today

1. **The only SAGV knob is a display parameter.** `/sys/module/xe/parameters/enable_sagv` is a
   read-only module parameter (observed `-r--------`, i.e. 0400) and can only *disable* SAGV. Note a
   discrepancy worth fixing in our own notes: the debugfs mirror created by
   `intel_display_debugfs_params.c` uses the mode declared in
   `INTEL_DISPLAY_PARAMS_FOR_EACH` — `0600` for `enable_sagv` in the 7.2.7 tree — not 0400 as
   `docs/hw/sagv.md` §2 states for the mirror. The unit’s 0400 observation matches the *module*
   parameter, not necessarily the debugfs file. Maintainer should confirm on the actual kernel build.
2. **On ≥ 14 the point is firmware’s.** `intel_pmdemand_atomic_check()` sets
   `new_pmdemand_state->params.qclk_gv_index = 0` with the comment “firmware will calculate the
   qclk_gv_index”; `icl_pcode_restrict_qgv_points()` returns 0 immediately for `DISPLAY_VER >= 14`,
   so the old PCODE QGV-mask path is dead.
3. **The vendor’s register pair is not modelled.** `include/drm/intel/mchbar_regs.h` says the
   mirror is read-only from Haswell and gone on MTL+, `intel_mchbar.h` exposes only read accessors,
   and `MCHBAR + 0x5DA0/0x5DA4` is not defined. Poking it from `/dev/mem` is both blocked by
   `CONFIG_STRICT_DEVMEM` and wrong (see §5).
4. **What the kernel does send** is a PM DEMAND request to
   `XELPDP_INITIATE_PMDEMAND_REQUEST(dword)` = `0x45230 + 4*dword`: dword 0 carries
   `QCLK_GV_BW` (bits 31:16), `VOLTAGE_INDEX` (14:12), `QCLK_GV_INDEX` (11:8) and the pipe mask;
   dword 1 carries CDCLK/DDICLK and the `REQ_ENABLE` trigger (bit 31), cleared by firmware on
   completion. `mtl_find_qgv_points()` already computes the *bandwidth* of the best QGV point
   (`qgv_point_peakbw`, in 100 Mb/s units) from the display requirements, but deliberately leaves
   the *index* to firmware.

So the hardware capability we want is exactly one field of an existing request, plus an index that
is currently constant. That is a much smaller and safer change than the vendor’s mailbox.

## 4. Proposed interface

### 4.1 Read-only: `sagv_state` (debugfs, 0600)

One file, `.../dri/<minor>/sagv_state`, prints:

* `qclk_gv_index`, `qclk_gv_bw`, `voltage_index`, `cdclk_freq_mhz`, `ddiclk_max`, `active_pipes`
  decoded from `XELPDP_INITIATE_PMDEMAND_REQUEST(0/1)` (the driver already has the field masks);
* `sa_perf_status` from `MCHBAR + 0x5918` if the read accessor is available on this display version
  (read-only accessor only — no write);
* the current `enable_sagv` display parameter and the forced point, if any.

This is useful on its own (it answers “what did firmware pick?”), has no policy, and gives the A/B
validation a readback. It should be the first patch even if the write path is never accepted.

### 4.2 Write: `sagv_force_point` (debugfs 0600, module parameter 0400)

* Type: `int`, default `-1` (off), range `-1 .. num_qgv_points-1`.
* Storage: a field in `display->pmdemand` (next to `lock`/`waitqueue`), initialized in
  `intel_pmdemand_init_early()`. The debugfs file writes it under no lock (a plain `int` written
  once by root; use `WRITE_ONCE`/`READ_ONCE`) and does **not** itself force an atomic commit.
* Application: on the next atomic commit, if a point is forced:
  * validate against `display->bw.max[0].num_qgv_points`;
  * find the bandwidth-table entry for the active plane configuration
    (`tgl_max_bw_index(display, num_active_planes, point)` in `intel_bw.c`) and require
    `deratedbw[point] >= required data_rate`; otherwise **reject with `-EINVAL`** rather than pin an
    under-bandwidth point;
  * set `qclk_gv_index = point` and `qclk_gv_bw = peakbw[point] / 100` so the pair is consistent
    (the request’s BW is the *peak bandwidth of the selected point*, in 100 Mb/s units — not the
    requirement itself);
  * apply only in the **post-plane** program (`old == NULL`), never in the pre-plane max-of-old/new
    step: lowering the point before the planes have switched is exactly the underrun case the
    pre-plane max exists to avoid;
  * honour the existing `REQ_ENABLE`/busy handshake and `intel_pmdemand_check_prev_transaction()`.

  The PM DEMAND request also carries the display-buffer (`DBUF`) fields and the pipe mask; those are
  unrelated to dma-buf objects. The “DMABUF” wording in the original task brief is read here as the
  request’s DBUF/QCLK_GV parameters: no dma-buf attachment or dma-buf ABI is involved, and this
  draft proposes none.
* Removing the override (`echo -1`) returns to firmware choice on the next commit.
* **Important interaction:** `intel_pmdemand_update_params()` uses
  `max3(old_val, new_val, current_val)` for every field, including `qclk_gv_index`. A forced value
  must bypass that max for the index (otherwise a lower point can never be programmed while the
  current register holds a higher one). This is the most likely place for a naive patch to be
  wrong.

### 4.3 Why not MCHBAR + 0x5DA0/0x5DA4

1. Not defined, not mapped, read-only accessors only; on ≥ 14 the mirror does not exist.
2. An undocumented PCU mailbox write would race the firmware that owns the PM DEMAND state; the
   vendor’s tool is a second owner on Windows, but on Linux the display driver is the owner.
3. It is unverifiable against this kernel: no public register definition, no trace, no error
   reporting (bit 31 busy only).
4. It cannot be clamped to display requirements, which is precisely the safety property we need.

The vendor path is good *evidence* that a point exists; it is not the interface to implement.

### 4.4 Why debugfs and not a stable ABI

The point numbering is not decoded, the effect is platform/PCU-specific, and an ABI would freeze a
guess. A root-only 0600 debugfs file (like the existing display parameters) is honest about its
status. If the mechanism proves out and someone wants policy, the next step is a driver-internal
policy or a userspace daemon over the debugfs file, not a new uAPI.

## 5. Risks

* **Display underruns / hangs** if a point is pinned below what the mode needs. The clamp in §4.2 is
  mandatory; `drm_err`/`drm_warn` on rejection, never silent.
* **PCU/firmware disagreement**: the PCU may ignore `QCLK_GV_INDEX` (the current comment says it
  calculates it), or may accept it only together with a matching BW. The A/B experiment in §6 is
  designed to detect both.
* **Locking/ordering**: pmdemand state is global; a write without a commit can be dropped or applied
  at an arbitrary later commit. The debugfs write therefore only sets intent; the effect is verified
  by reading `sagv_state` after an explicit modeset/commit.
* **Regression risk for other platforms**: gate the code to the display versions that use
  `intel_pmdemand` (≥ 14), keep default `-1`, and do not alter the firmware-choice path when the
  override is off.
* **Debugfs is not ABI**: no tool may rely on the file name or format; document that in the file’s
  header comment.

## 6. Validation plan (two boots + a within-boot proxy)

The measurement surface already exists in this repository: `files/rapl-power` publishes
`package-0`, `core`, `uncore`, **`dram`** and **`psys`** at 1 s/0.1 W; `tools/oxp3-sagv.sh measure`
averages them. The A/B protocol is in [`docs/hw/sagv.md`](../docs/hw/sagv.md) §5.2.

1. **Read-only baseline**: `sagv_state` at idle and under a 144 Hz full-screen animation. Expect the
   index to change with display demand; if it never changes, the PCU is not exposing the choice and
   the write path is not worth pursuing.
2. **Pin each point in one boot**: `echo 3 > .../sagv_force_point` (or 0/4), force a modeset, read
   back the request and `SA_PERF_STATUS`, measure `dram`/`psys` for 120 s at a fixed desktop. Check
   `dmesg` for underruns/FIFO errors. Repeat on battery, AC disconnected, brightness fixed.
3. **Two-boot A/B for the SAGV benefit**: boot A default, boot B `xe.enable_sagv=0` (the only
   existing switch), both idle, `tools/oxp3-sagv.sh measure -s 120`, discard the first minute,
   repeat. This bounds what the point is worth even before the override works. The expectation is
   ≈0.3–1 W on `dram`; report the number, not the expectation.
4. **Cross-check** `intel-rapl-mmio:0` (its own `dram` child) against `intel-rapl:0`, and battery
   draw (`BAT0/power_now` is 0 on this EC, so use energy deltas).
5. **Persistence**: read the index again after 10 minutes idle; if firmware overrides the pin, the
   experiment answers the ownership question (and the feature should not be exposed).

## 7. Patch sketch (our own code; **not compiled**, not a submission)

Layout, in the order a series would go:

```c
/* 1. intel_display_params.h: a dev knob with the same shape as enable_sagv.
 *    Module parameter stays read-only (0400 in intel_display_params.c);
 *    the debugfs mirror gets 0600 from this table. */
param(int, sagv_force_point, -1, 0600)

/* 2. intel_display_core.h, inside the existing pmdemand struct: */
struct {
	wait_queue_head_t waitqueue;
	struct mutex lock;		/* protects the programming sequence */
	int sagv_force_point;		/* -1 = firmware chooses; dev-only */
	struct intel_global_obj obj;
} pmdemand;

/* 3. intel_pmdemand.c: initialize once. */
void intel_pmdemand_init_early(struct intel_display *display)
{
	...
	display->pmdemand.sagv_force_point = -1;
}

/* 4. intel_bw.c: the point/bandwidth consistency helper.  Reuses the
 *    existing per-point tables; no new register access. */
int intel_bw_qgv_point_for_index(struct intel_display *display,
				 struct intel_bw_state *bw_state,
				 unsigned int point)
{
	unsigned int planes = ...;	/* same source mtl_find_qgv_points() uses */
	unsigned int idx;
	u16 peak;

	if (point >= display->bw.max[0].num_qgv_points)
		return -EINVAL;

	idx = tgl_max_bw_index(display, planes, point);
	if (idx >= ARRAY_SIZE(display->bw.max))
		return -EINVAL;

	/* Refuse to pin below the bandwidth this mode needs. */
	if (display->bw.max[idx].deratedbw[point] < required_data_rate(...))
		return -EINVAL;

	peak = display->bw.max[idx].peakbw[point] / 100;	/* 100 Mb/s units */
	bw_state->qgv_point_peakbw = peak;
	return point;
}

/* 5. intel_pmdemand.c: request path.  The value is read once per commit;
 *    when set, it wins over the firmware-choice 0 in atomic_check. */
static int intel_pmdemand_apply_force_point(struct intel_atomic_state *state,
					    struct intel_bw_state *bw_state,
					    struct intel_pmdemand_state *pm)
{
	struct intel_display *display = to_intel_display(state);
	int point = READ_ONCE(display->pmdemand.sagv_force_point);

	if (point < 0)
		return 0;	/* existing behaviour: index 0, firmware choice */

	point = intel_bw_qgv_point_for_index(display, bw_state, point);
	if (point < 0)
		return point;

	pm->params.qclk_gv_index = point;
	pm->params.qclk_gv_bw = intel_bw_qgv_point_peakbw(bw_state);
	return 0;
}

/* In intel_pmdemand_atomic_check(), after the bw state is available: */
	ret = intel_pmdemand_apply_force_point(state, (struct intel_bw_state *)new_bw_state,
					       new_pmdemand_state);
	if (ret)
		return ret;

/* 6. intel_pmdemand.c: honour a pin in the register update.  A pinned
 *    index must not go through max3(), which would keep a higher old or
 *    current point and make lower points unprogrammable.  Only apply on
 *    the post-plane pass (old == NULL), so the planes have switched. */
	if (old == NULL && pinned) {
		*reg1 &= ~XELPDP_PMDEMAND_QCLK_GV_INDEX_MASK;
		*reg1 |= REG_FIELD_PREP(XELPDP_PMDEMAND_QCLK_GV_INDEX_MASK,
					fixed_index);
	} else {
		update_reg(reg1, qclk_gv_index,
			   XELPDP_PMDEMAND_QCLK_GV_INDEX_MASK);
	}
	/* qclk_gv_bw: when pinned, write the pinned value instead of max3
	 * too, otherwise the pair can be inconsistent. */

/* 7. Read-only state file (debugfs, 0600), added next to the display
 *    params: decode XELPDP_INITIATE_PMDEMAND_REQUEST(0/1) with the
 *    existing masks, print sa_perf_status if intel_mchbar_read() is
 *    available, and the forced value.  No writes. */
```

A documentation note would go with it (our prose):

> `sagv_force_point` (debugfs, root only, development use): pin the QCLK-GV index used in the PM
> DEMAND request. `-1` (default) leaves the choice to the PCU firmware, which is the normal
> behaviour on display version 14 and later. A value in `0..num_qgv_points-1` is validated against
> the current display bandwidth; a point that cannot satisfy the mode is rejected and the commit
> fails. The numbering is platform-specific and not decoded; this is not a stable interface. A
> forced point is applied after the planes have switched, on the next atomic commit.

## 8. What a maintainer must verify

1. **Does `QCLK_GV_INDEX` actually pin anything on PTL?** The current comment says firmware
   calculates it. This is the experiment; if the answer is no, only the read-only state file is
   worth upstreaming.
2. **Bandwidth units/semantics**: confirm `qclk_gv_bw` in the request is the selected point’s peak
   bandwidth in 100 Mb/s (as `mtl_find_qgv_points()` builds it), and that the pair must match.
3. **`max3` interaction**: the existing pre/post-plane max logic must not defeat a lower pin; the
   sketch’s post-plane-only rule is a proposal, not a proven design.
4. **Commit triggering**: a debugfs write does not force an atomic commit. Decide whether the file
   should trigger one (and how) or whether “takes effect on the next modeset” is acceptable.
5. **Locking**: `display->pmdemand.lock` guards the register sequence; the force value is read
   outside it. Confirm the ordering and that a concurrent commit cannot observe a torn intent.
6. **Platform gating**: `DISPLAY_VER >= 14` only, default off, no change to the firmware-choice path
   when off; test on at least one other Xe3/PTL configuration if available.
7. **`num_qgv_points` source**: `display->bw.max[0].num_qgv_points` is initialized after bandwidth
   probing; the debugfs file must not be usable before then (or must return `-EAGAIN`).
8. **API fit**: `tgl_max_bw_index()` and the active-plane count are static to `intel_bw.c`; the
   helper must live there (as sketched), not in pmdemand.
9. **ABI discipline**: no sysfs/uAPI; debugfs is explicitly not stable. Do not add a module
   parameter that userspace is expected to set.

## 9. Open questions

1. Point number → QCLK/DCLK frequency mapping (the plugin’s 0x30-byte per-platform table is not
   decoded).
2. Does the vendor mailbox conflict with PM DEMAND if both are used? Untested; a Windows-side
   write-then-read-`SA_PERF_STATUS` experiment would answer it.
3. Is `sagvPoint` even effective on this panel/mode, or does the display watermark path reject SAGV
   most of the time? §6 step 1 answers it.
4. Should the eventual policy live in the kernel (like `AutoSAGVProc`) or userspace? We argue
   userspace; a 10 s GPU-utilisation policy is not a kernel concern.
5. Is there a public PCU/PM DEMAND specification for the `QCLK_GV_INDEX` field on Xe3 that would
   remove the guesswork? If so, the design should follow it instead of this RFC.
