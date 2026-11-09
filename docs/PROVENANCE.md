# Provenance and licensing — what this repository is made of

Date: **2026-11-09**. Scope: every tracked file, the licence application tooling, and the evidence
that the "our own work only" rule has been enforced. This document records **provenance, not a
licence choice**. No repository-wide licence is asserted here; `LICENSE` does not exist in this
checkout and only the owner can choose one. Until then, default copyright applies and reuse terms
are ambiguous, which is exactly the decision this page prepares.

Related: [QA.md](QA.md) check 23 (the pending decision), [MAINTAINABILITY.md](MAINTAINABILITY.md)
section 8, `tools/apply-license.py` (apply one choice) and `tools/provenance-audit.py` (re-run the
audit below). This is engineering documentation, not legal advice.

## 1. The guarantee

**The repository contains only our own code, our own documentation, and generated/derived data.**
Concretely, none of the following is tracked, ever:

1. **Vendor binaries or libraries** — no `.dll`, `.exe`, `.sys`, `.efi`, `.o`, `.so`, `.a`, `.rpm`,
   `.zip`, no ELF/PE/Mach-O/PDF/PNG/ZIP/gzip magic. The full tree was text at the audit revision
   (294 files, largest 126,183 bytes, `git ls-files` + `file` + magic-byte scan).
2. **Firmware images or fragments** — display DMC, Bluetooth `ibt-*.sfi` and CPU microcode are fetched at build/install time from upstream locations with pinned SHA-256 values
   and installed under `/usr/lib/firmware/updates`; they are never written into the checkout.
3. **Vendor application payloads** — the vendor's Windows DLLs used for XeSS (`igdext64.dll`,
   `ControlLib.dll`) come from a pinned release archive, are hash-checked per file at download time
   and live under `$HOME/.local/share`, not in the tree. Only URLs and hashes are committed.
4. **Decompiled or disassembled listings** — Ghidra/objdump runs are local analysis work. Their
   output stays in the analyst's cache; no dump, pseudo-code body or instruction listing is
   committed. The helpers in `tools/ghidra-scripts/` and `tools/ghidra-pod.sh` are our own scripts,
   not captured output.
5. **Copied third-party source** — the only upstream-derived text is the **patch context** in
   `patches/**` (see category d). Each patch carries its upstream project's licence and is not
   relicensed by this repository. No vendor code, comment block, string table or document is pasted
   into our sources.

The audit tool checks the mechanical part of this list (magic bytes, vendor extensions, base64
blobs, decompiler pseudo-code markers, vendor-name references) and reports everything it finds with
a path and line. The policy and the exceptions are below.

## 2. What "derived facts" means here

Some behaviour was learned by observing a vendor binary or by capturing hardware traffic. Facts are
not the same as expression, and this repository stores facts:

* **Allowed**: a frame layout, opcode/phase byte, CRC polynomial, field width, retry rule, register
  address/bit, profile wattage, fan-curve point, timing — recorded as values, tables and prose that
  we wrote, with the observation method documented.
* **Allowed**: citing an observed identifier as an evidence pointer, e.g. a function name/address in
  a vendor DLL (`FUN_180004358`) or the vendor file name/version it came from. That is a reference,
  not a listing.
* **Not allowed**: copying the vendor's code, comments, strings, resource text or table formatting;
  storing any slice of the binary; reproducing a long verbatim quote; shipping a decompiler dump.

Examples in this tree, all fact-only:

| fact | where it is recorded as facts | evidence trail |
|---|---|---|
| BF63112 SPI frame: `AA \| len \| 16×0 \| 4C op phase arg len data \| pad16 \| CRC-32`, CRC-32/ISO-HDLC | `tools/fingerprint/bf63112_proto.c` / `.h`, `tools/fingerprint/fpbf63112/protocol.py`, `docs/hw/fingerprint.md` ("The Windows driver" section) | recovered from the Windows UMDF driver by observation; the doc gives the method and the byte checks |
| Body-RGB 64-byte HID frames and zone ids | `tools/oxp3-led.py`, `tools/oxp3_hid.py`, `docs/hw/rgb-frames-2026-11-09.txt` | frames captured from this unit with our own tooling, not vendor bytes |
| OneXConsole per-profile PL1/PL2/PL4/tau table and fan semantics | `tools/oxp3-powerctl.py` comments/data, `docs/hw/battery-tdp.md`, `docs/hw/power-fan.md` | values read read-only from the vendor payload; the docs record what was read, not a copy of it |
| SAGV/QCLK-GV register pair, `enable_sagv` semantics | `tools/oxp3-sagv.sh`, `docs/hw/sagv.md` | observation + Windows-tool behaviour; no MMIO dump or code |
| Harman AudioEFX speaker tuning (8 bands, pre-gain) | `tools/audio/oxp3-speaker-eq.conf`, `tools/audio/eq_response.py`, `docs/hw/audio-harman-apo.md` | measured/modeled values; the vendor APO is not included |

## 3. Per-category policy

Every tracked file falls into exactly one of these categories; `tools/provenance-audit.py --all`
prints the per-file assignment.

| cat. | meaning | examples in this tree | licence/header policy |
|---|---|---|---|
| **a** | our original code | `tools/*.py`, `tools/*.sh`, `files/oxp3-*` scripts, `tools/fingerprint/**` C/Python, Decky `src/`, `Makefile` | covered by the repository licence once chosen. One `SPDX-License-Identifier` line per file, applied by `tools/apply-license.py` (after the shebang/Python coding line, before existing headers/docstrings). Never add a vendor notice. |
| **b** | our documentation | `README.md`, `docs/**/*.md`, `upstream/*.md` | our own expression; no per-file SPDX. Third-party facts are described, quotes shortened to a phrase, and the observation source cited. A doc must not become a copy of a vendor document. |
| **c** | generated/derived data + authored configuration | `docs/hw/components.yaml`, generated tables, `tests/verify/fixtures/**`, the generated `tools/fingerprint/tests/vectors.h`/`.json` and fixtures, `docs/state/*`, CSV measurements, units/rules/hwdb/conf/TOML, `.gitignore` | covered by the repository licence; **no hand-written SPDX header**. Generated files say "Generated by …" and their generator is authoritative (`tools/fingerprint/tests/vectors.h` is exempt from header application for exactly this reason). Data must be derived from observation/measurement, never extracted from a vendor binary or firmware. |
| **d** | third-party licensed context / referenced facts | all 98 `patches/**` files; upstream API/identifier citations in docs | **not relicensed**. Patches keep the upstream project's licence and headers (kernel GPL-2.0, Mesa MIT, gamescope GPL, hhd, MangoHud); they are out of the header tool's scope. Referenced facts stay facts (§2) and are cited to their observation, not pasted. |
| **e** | unclear | none at the 2026-11-09 audit | blocks merge until resolved. Record the question and the missing evidence in this document; do not guess a provenance. |

## 4. How a contributor records provenance

1. **Write it yourself.** If behaviour is learned from a vendor artefact, record the facts (values,
   layout, method) in the matching `docs/hw/` topic document; do not paste code, comments, strings or a dump.
2. **Say where a change came from** in the commit body or pull request, using one of:
   `provenance: own`, `provenance: derived-facts (see the matching docs/hw/ topic document)`,
   `provenance: upstream-context (<project>, <licence>)`. A patch is always the last form.
3. **Constants get a trail.** If a new table or magic number drives code, the comment cites the doc
   section that explains how it was observed. The fingerprint module is the model: constants,
   derivation and "what is proven vs assumed" are in one place.
4. **Never commit bytes from a vendor artefact.** Record the file name, version, URL and SHA-256 in
   the document or script that downloads it; add the same hash check the existing firmware/XeSS
   paths use. If a file is proprietary, it does not belong in git even temporarily.
5. **New source file** gets its header from `tools/apply-license.py` (after the owner's licence
   choice), not a hand-written variant, so a later policy change stays one reviewable commit.
6. **Before review**, run `tools/provenance-audit.py` (must be `0 hard`) and
   `tools/apply-license.py --check --spdx <chosen> [...]` (must exit 0).

## 5. Audit snapshot (2026-11-09)

Command: `tools/provenance-audit.py` at revision `d407f98` (294 tracked files).

| category | files |
|---|---:|
| (a) our original code | 82 |
| (b) our documentation | 34 |
| (c) generated/derived data + authored config | 80 |
| (d) third-party licensed context (98 patches) + referenced facts | 98 |
| (e) unclear | **0** |

Result: **0 hard findings**, 106 informational hits, all expected and explained:

| informational hit | count | why it is not a problem |
|---|---:|---|
| vendor name referenced (`OneXConsole`, `Betterlife`, DLL names, `app.asar`) | 90 | file/protocol names in our prose and code comments; no vendor text or bytes |
| copyright/proprietary wording | 9 | mostly negations ("nothing vendor-proprietary is stored") and upstream patch licence headers |
| long hex run (≥200 hex digits) | 4 | our own captured RGB frames and generated test vectors, not firmware or a binary slice |
| decompiled-function identifier cited (`FUN_…`) | 3 | evidence pointers in `docs/hw/fingerprint.md`, `docs/hw/audio-harman-apo.md`, `tools/fingerprint/oxp3-fp-probe.py`; no listing |

The detector treats a **tracked binary/extension**, a **≥400-character mixed-case base64 token**, or
**three pseudo-code markers in one file** (`undefinedN`, `param_N`, `uVarN`, …) as hard failures.
`--json` gives the same result machine-readably; `--all` prints every file with its category, so the
classification can be reviewed file by file rather than trusted.

### Header scope (the licensing half of the audit)

`tools/apply-license.py` covers hand-written code: `*.py *.sh *.c *.h *.java *.ts *.tsx *.js *.mjs
*.fx`, both `Makefile`s, and extensionless scripts with a shebang. At HEAD that is **81 files**:
12 already carry `SPDX-License-Identifier: LGPL-2.1-or-later` (the whole fingerprint subtree),
69 do not. One generated file (`tools/fingerprint/tests/vectors.h`) is exempt because its generator
owns it; the two tools added in this round are picked up by the same command. QA's narrower
"source-like" count (71) did not include the Java helpers, the `.fx` shader, the second Makefile or
the extensionless scripts; the tool's scope is the complete one.

## 6. Evidence log — where the rule was enforced

| commit / artefact | evidence |
|---|---|
| `04ad31c` "chore: drop an accidentally committed vkd3d shader cache and ignore it" | removed a **1,404,368-byte** `vkd3d-proton.cache` binary from the tree and added it to `.gitignore`. `git show --stat 04ad31c` shows `Bin 1404368 -> 0 bytes`. |
| `5820611` "Kernel modules, pinned firmware and OXP3 extras" | firmware is described as "fetched from upstream with pinned checksums"; the `FIRMWARE=(...)` table in the installer configuration carries URL/path + SHA-256, and no firmware file is in `git ls-files`. |
| `5fcd8d4`, `8dd8f34`, `b33185a` (XeSS/XMX round) | the vendor Windows DLLs are used from a pinned release archive; `XESS_ZIP_URL` + `XESS_ZIP_SHA` + per-DLL `XESS_DLLS` hashes are committed, the archive is not. The binary-diff work was analysis output kept out of the tree. |
| `31d4659` "…isolated per-agent Ghidra wrapper" | the decompiler helpers are our scripts; the wrapper writes output under `$HOME/.cache/ghidra-agents`, never into the repository. |
| `docs/QA.md` 2026-11-09 checks 18, 19, 22 | 0 tracked binaries, 0 tracked build artefacts, 0 secrets across 253 files at that revision; the current audit extends the same scan. |
| `.gitignore` | signing keys (`*.key`, `*.pem`, `mok/`), build output, caches and the vkd3d cache; no vendor path is ever produced inside the checkout. |
| `docs/INSTALL-HW.md` §1 (prerequisites) | states that firmware is downloaded with pinned SHA-256 and "nothing vendor-proprietary is stored in this repo". |

## 7. The owner decision (one command each)

No licence is chosen by these commands; each applies a choice the owner makes. `LICENSE` is
written for the primary id, the per-file `SPDX-License-Identifier` lines are added, and the existing
fingerprint subtree keeps its declared `LGPL-2.1-or-later` (compatible with libfprint, where the
driver is intended to go). Patches are excluded and keep their upstream licences.

**Policy A — permissive tools/docs, copyleft fingerprint subtree**

```sh
tools/apply-license.py --write --spdx MIT --holder "silentsudin" --except 'tools/fingerprint/**=LGPL-2.1-or-later'
```

**Policy B — one copyleft licence for the whole repository**

```sh
tools/apply-license.py --write --spdx LGPL-2.1-or-later --holder "silentsudin"
```

Then verify and commit (idempotent; exits 0 only when every header matches):

```sh
tools/apply-license.py --check --spdx MIT --except 'tools/fingerprint/**=LGPL-2.1-or-later'
# or, for B:
tools/apply-license.py --check --spdx LGPL-2.1-or-later
git diff --stat && git diff -- LICENSE | head
```

If the host is offline, long licence texts are **never typed from memory**: add
`--license-file /path/to/mit-or-lgpl.txt` for the primary id and, for Policy A,
`--exception-license-file LGPL-2.1-or-later=/path/to/lgpl-2.1.txt`. Apache-2.0 and GPL-2.0-or-later
are also supported by the same mechanism (official URL fetch, or a supplied file). `--root DIR`
applies the same command to a copy; `--check` is a dry run.

## 8. Open points

* **Licence id, holder and year** — the owner's call; `--holder` defaults to
  "The oxp3-anatase-fixes authors" so the decision is explicit rather than assumed.
* **Whether documentation should also carry per-file SPDX** — current policy: no; `LICENSE` plus
  this page covers prose, and headers in generated Markdown blocks would be overwritten by
  `tools/gen-docs.py`.
* **Canonical texts for exceptions when offline** — Policy A wants
  `LICENSES/LGPL-2.1-or-later.txt`; the tool fetches it, otherwise `--exception-license-file`
  supplies it. Decide whether to commit that file once (recommended: yes, REUSE-style).
* **Patch provenance note** — patches carry upstream headers, but a `patches/README.md` listing
  project → licence would make that explicit for readers. Not written yet.
* **Contributor sign-off** — no DCO/CLA is used; the commit-message convention in §4 is the current
  mechanism.

## 9. Maintaining this page

Re-run `tools/provenance-audit.py --all` when files are added and update §5's numbers only if a
category actually changes. Any new category (e), any hard finding, or any new download path is a
review blocker until it is explained here. The date at the top is the audit date, not the last
formatting edit.
