# Upstream drafts prepared from this repository

**No upstream submission has been made.** There are no upstream accounts and no issue/MR (GA)
permissions in this environment — no freedesktop GitLab, GitHub, kernel Bugzilla or mailing-list
access — and nothing here has been posted, merged or mailed. These files are our own prose and our
own patch sketches, written so a human with the right accounts can review, finish and submit them.
They contain no vendor code, decompiled listings, DLLs or firmware.

Both are **drafts**, not patches:

| draft | subsystem | the ask | evidence still missing |
|---|---|---|---|
| [`oxpec-fan-bypass-request.md`](../../upstream/oxpec-fan-bypass-request.md) | `platform/x86` (`oxpec`) | expose the EC fan curves as hwmon `pwmN_auto_pointM_{temp,pwm}` on this board and back `charge_behaviour` (`auto` / `inhibit-charge` / `inhibit-charge-awake`) with the EC bypass byte; no debugfs user knobs | the EC register map (addresses, table layout, point count, units, the three bypass values) — Ghidra cannot decompile the CIL assembly and no .NET decompiler is installed here |
| [`intel-pmdemand-sagv-request.md`](../../upstream/intel-pmdemand-sagv-request.md) | DRM (`intel_pmdemand`, shared by `xe`/i915 display) | a read-only debugfs `sagv_state` plus a root-only, development `sagv_force_point` that pins `QCLK_GV_INDEX` and selects the matching `QCLK_GV_BW`, clamped to the mode’s display bandwidth; no MCHBAR pokes, no new uAPI | whether the PM DEMAND index field actually pins anything on display version 30, and the point-number → frequency mapping; the A/B power delta was not measured (no root/reboots here) |

## What is already solid

* **A** is backed by the vendor field names/semantics, the live hwmon/power_supply inventory, and
  a concrete recovery plan (IL decompiler → Windows EC trace → ACPI dump → differential EC dump).
  It also flags a discrepancy in our own notes: the 7.2.7 `oxpec` source already maps `oxp_g1_i`
  to candidate charge registers `0xA3`/`0xA4`, so “no interface exists” must be re-checked on the
  unit before the RFC claims it.
* **B** is backed by the recovered MCHBAR pair (`0:0.0` cfg `0x48`, `+0x5DA0`, `+0x5DA4 =
  0x80000122`, busy bit 31 ≤ 10 ms, `+0x5918`), the vendor profile values (Quiet 3 / Balanced 0 /
  Performance 4; AutoSAGV 80/90 % at 10 s), and the 7.2.7 source showing `qclk_gv_index = 0` with
  the firmware-choice comment and the dead `icl_pcode_restrict_qgv_points()` path for ≥ 14.

## Rules used for these drafts

* No vendor material is copied: only field names, values and behaviour are cited as evidence, and
  the original binaries stay outside the repository.
* Every code block is labelled a sketch; none is claimed to compile against the real tree.
* Each draft ends with the points a maintainer must verify and the decisions we are not entitled to
  make alone (ABI shape, upstream fit, whether a incremental driver is acceptable).
* Nothing has been sent to any upstream project. If you submit one of these, update the status line
  in the file and this table.
