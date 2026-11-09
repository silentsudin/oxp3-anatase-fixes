# Component registry: one source of truth for component status

Date: **2026-11-09** (registry round; unprivileged, no host change).

`docs/hw/components.yaml` is the only place where a component's state, summary, implementation
paths, doc link, root requirement, install/verify steps, dependencies, caveats and last validation
are written down. The tables and roll-ups that used to repeat those facts by hand are now rendered
by `tools/gen-docs.py`:

| generated block | file | what it shows |
|---|---|---|
| `hw-state` | `docs/hw/README.md` | state matrix of the hardware components plus a caveat list |
| `install-verify` | `docs/INSTALL-HW.md` | whole-system checks, install invocations, and one row per verify check |
| `readme-summary` | `README.md` | short summary of the headline components |
| `handover-state` | `docs/HANDOVER.md` | component counts by state plus the harness/suite/unit-test counts that used to be retyped there (section 7.1) |

Why: `docs/QA.md` ("Docs duplicated three ways") and `docs/MAINTAINABILITY.md` section 6 recorded
that every component change needed three edits and had already drifted in miniature. Now a component
change is one edit to `components.yaml` plus `tools/gen-docs.py --write`.

## 1. Files

| path | role |
|---|---|
| `docs/hw/components.yaml` | the data file (our own; no vendor material) |
| `tools/gen-docs.py` | stdlib-only validator, renderer, `--check` and `--write` |
| `docs/hw/REGISTRY.md` | this document: schema, vocabulary, contribution rules |
| `tools/repo-test.sh` | `gen-docs` suite runs `--check` with the other offline checks |

## 2. Schema (version 1)

Top level:

| key | type | notes |
|---|---|---|
| `schema` | integer | must be `1` |
| `global_checks` | list of mappings | optional; whole-system rows for the install/verify block (`label`, `command`, `expect`) |
| `unmapped_checks` | mapping | optional; `check-id: reason` for a `tools/oxp3-verify.sh` check that deliberately belongs to no component |
| `components` | list of mappings | required, non-empty; **file order is table order** |

One component:

| field | type | required | meaning / validation |
|---|---|---|---|
| `id` | string | yes | `^[a-z][a-z0-9-]*$`, unique; used by `dependencies` and caveats |
| `title` | string | yes | table cell; plain text |
| `state` | enum | yes | one of the five fixed states (section 3) |
| `group` | enum | yes | `driver-stack`, `hardware` or `diagnostics`; decides which blocks show it |
| `headline` | boolean | yes | `true` also puts the component in the README summary table |
| `summary` | string | yes | one sentence, single line, at most 400 characters; no trailing period is required |
| `implementation` | list of strings | yes | repository-relative paths or globs; **every entry must match at least one existing path** |
| `doc` | string | yes | `docs/*.md` path that exists; rendered as a relative link from each block's file |
| `root_required` | enum | yes | `yes`, `no`, `partial`, `not-applicable`; rendered as `yes`/`no`/`partial`/`n/a` |
| `install` | mapping | yes | `summary` (required, one line), `entrypoint` (optional command), `steps` (optional list) |
| `verify` | list of mappings | yes | non-empty; each item is `id` (a check in `tools/oxp3-verify.sh`) **or** `command` (a read-only command), exactly one of the two; `expect` (required for `command`, optional for `id`); `root` (optional boolean) |
| `dependencies` | list of strings | yes | other component ids; no self-reference, no unknown ids |
| `caveats` | list of strings | yes | shown under the state matrix; may be empty |
| `last_validated` | mapping | yes | `date` (ISO `YYYY-MM-DD`) and `evidence` (where the evidence lives; do not paste measurements that can drift) |

`verify` invariants enforced by the validator:

* every `id` is declared in `tools/oxp3-verify.sh` - the generator extracts the ids with the same
  rules the harness uses for its own inventory (`record <id>` / `simple_ind <id>`);
* each id is used by **exactly one** component, so a check maps to one owner;
* every id declared by the harness is either mapped to a component or listed in `unmapped_checks`.
  A new check in `tools/oxp3-verify.sh` therefore fails `--check` until it is owned - that is the
  intended drift alarm.

`unmapped_checks` exists for the honest exception (for example a future whole-machine check that is
not a component fact, or the cross-cutting `inst.backups` undo alarm in
[ROLLBACK.md](../ROLLBACK.md)). It needs a reason. Because check ids contain dots, the YAML
subset accepts `.` in keys; only `unmapped_checks` uses such keys today.

## 3. State vocabulary (fixed)

State strings are a closed set on purpose: they are the join key between the docs, the Decky plugin
discussion and future automation, and free prose in that column is exactly how the three tables
drifted. The summary/caveat fields carry the nuance.

| state | use it when | examples in this repo |
|---|---|---|
| `working` | the implementation is complete and validated by recorded evidence; no known functional gap | Mesa, gamescope, audio EQ, display HDR, power envelope |
| `partial` | something useful works and is validated, but a documented part of the goal is missing | XeSS XMX (driver patches parked), fan (EC curves not exposed), battery (EC bypass byte not exposed) |
| `blocked` | progress needs upstream/kernel/vendor work; nothing can be installed that closes the gap | SAGV (no Linux interface at all) |
| `not-a-gap` | the investigation showed there is no gap on this unit | IMU (works out of the box), camera (no camera hardware) |
| `awaiting-install` | the code is finished and offline-validated; the remaining step is installing it and running the verify check on the device | volume keys, LEDs, RGB, suspend policy, Bluetooth firmware, adaptive TDP, sched_ext |

Rules of thumb when a component sits between two states:

* `awaiting-install` is not a quality statement and is not "unknown": the implementation exists and
  its checks are defined. Use it when the only missing evidence is device-side installation.
* `partial` requires a **named missing piece** in the summary or caveats. "Not tested yet" alone is
  `awaiting-install`, not `partial`.
* `blocked` requires that no local implementation can close the gap; tooling that only *measures*
  or *documents* the gap (SAGV) still counts as blocked for the fix itself.
* never invent a sixth state; see section 6 for changing the vocabulary.

`root_required` uses the same discipline: `yes` (installing the component writes to `/etc`,
`/usr/local` or `/usr`), `no` (user scope only), `partial` (only one part needs root - say which in
a caveat), `not-applicable` (nothing is installed at all).

## 4. How to add a component

1. Add an entry to `components.yaml` in the position where it should appear in the tables. Keep the
   file grouped as it is: driver stack, platform, hardware, diagnostics.
2. Fill every required field. For `verify`, prefer an existing or new check in
   `tools/oxp3-verify.sh`; add a `command` only when no check exists (read-only, no root).
3. If the component is not `group: hardware`, set `headline: true` - otherwise it is in the registry
   but in no generated table and the validator refuses it.
4. Run `python3 tools/gen-docs.py --write`, then `python3 tools/gen-docs.py --check`.
5. Run the offline suite that now includes it: `tools/repo-test.sh --fast --only gen-docs`
   (or `make test`).
6. If you added a check to `tools/oxp3-verify.sh`, its declared count changes on purpose: map it
   here. The count quoted in `docs/HANDOVER.md` is regenerated by the `handover-state` block (section
   7.1); other documents that quote it by hand, such as `docs/VALIDATION.md`, are not generated and
   need their own edit.

## 5. How to change an existing component

| change | what to do |
|---|---|
| new verdict or state | edit `state` and `summary`; add a caveat if the reason is not obvious; bump `last_validated` if new evidence exists |
| implementation moved | update `implementation`; the validator fails on a path that no longer exists |
| doc renamed | update `doc`; the validator fails on a missing file |
| installer flag renamed | update `install.entrypoint`/`steps`; `docs/INSTALL-HW.md` prose (sections 2, 3, 6, 7) is hand-written and still needs a look - it is not generated |
| check renamed in the harness | update the `verify[].id`; `--check` fails until the id resolves |
| new dependency | add it to `dependencies` (an id, not a path) |

Never edit a generated block to "fix" a table. Edit the data and regenerate; `--check` in
`tools/repo-test.sh` fails otherwise, which is the point.

## 6. Changing the vocabulary or the schema

Changing `state` values or field names is a schema change. Do it in one commit and update: this
document, the `STATES`/`ROOT_VALUES`/`COMPONENT_KEYS` constants in `tools/gen-docs.py`, every entry
in `components.yaml`, and any text in the docs that quotes the old words. The generator validates
against those constants, so a half-finished change fails loudly instead of silently rendering an
empty table.

## 7. Generated blocks: markers and the rules

Each block is delimited by exactly one pair of markers:

```markdown
<!-- BEGIN GENERATED: install-verify (source: docs/hw/components.yaml; regenerate: tools/gen-docs.py --write) -->
...generated table(s)...
<!-- END GENERATED: install-verify -->
```

* The generator only replaces the text **between** the markers. Prose before and after, heading
  levels, and every other line of the file are byte-preserved (verified with `git diff`).
* Duplicate or unbalanced markers are a validation error, not a silent overwrite.
* Inside a block every character is generated: do not hand-edit a table row, do not add a row to a
  generated table, do not "quickly fix" a typo there. The next `--check` will call it stale and the
  next `--write` will discard the edit.
* A missing marker pair for a block is an error from `--write` with the exact lines to add; the
  generator never appends blocks at the end of a document.

Commands:

```sh
python3 tools/gen-docs.py            # validate + report stale blocks (the safe default)
python3 tools/gen-docs.py --check    # same; exit 1 when stale, 2 on a data error
python3 tools/gen-docs.py --write    # rewrite the blocks; prints what changed
```

Exit codes: `0` in sync/written, `1` stale block, `2` usage/validation error. Output is
deterministic (no dates, no environment values), so `--check` is safe in CI and gives a byte-identical
result on repeated runs.

CI / test integration (already wired): `tools/repo-test.sh` runs the `gen-docs` suite, so
`make test` and `tools/repo-test.sh --fast` include it, and that one suite therefore covers all
four blocks. The one-line form for any other harness is:

```sh
python3 tools/gen-docs.py --check || fail "generated docs are stale (run tools/gen-docs.py --write)"
```

### 7.1 Derived volatile counts: the `handover-state` block

`docs/HANDOVER.md` used to repeat numbers that drift. It now carries the fourth block, with the
same marker rules; only the text between the markers is generated. Each number is derived from its
source and the block states the derivation:

| number | source of truth | how the generator obtains it |
|---|---|---|
| components per state and total | `docs/hw/components.yaml` | counts the validated `state:` values over the five fixed states |
| checks declared by the harness | `tools/oxp3-verify.sh` | the same `record`/`simple_ind` extraction that validates the registry (`declared_checks`) |
| suites in `tools/repo-test.sh` | the runner's `def_suite` registrations | parses each registration's name and `fast`/`full` mode, then reports the total and the `--fast` subset |
| tests in `tests/unit` | the unit suite itself | runs `python3 -m unittest discover -s tests/unit -q` with bytecode disabled and parses `Ran N tests`; only when discovery cannot run does it count `def test*` methods statically, and the block then says so |

That last derivation runs the unit suite, so `tools/gen-docs.py --check` takes roughly as long as the
`unit` suite (a few seconds). Do not type one of these numbers into HANDOVER prose: point at the
block, or at the command whose output it quotes. `gen-docs --write` regenerates this block together
with the three component tables, and `--check` exits 1 as soon as any of them drifts.

## 8. YAML subset

The data file uses a small, strictly validated YAML subset (stdlib only - no PyYAML):

* block mappings and block sequences, nested by two-space indentation;
* `key: value`, `key:` followed by an indented block, `- value`, `- key: value` plus further keys
  at the same indentation;
* plain scalars, `true`/`false`, integers, double-quoted strings (JSON escapes) and single-quoted
  strings;
* single-line flow sequences of scalars (`[a, b]`) and the empty `{}` / `[]`;
* full-line comments and ` #` trailing comments outside quotes.

`root_required` values `yes`/`no` are written **quoted** (`"yes"`, `"no"`): YAML 1.1 parsers such
as PyYAML read unquoted `yes`/`no` as booleans, and quoting keeps the file interoperable. The
generator does not care (it compares strings either way), but a generic consumer would otherwise
disagree with the schema. The subset parser and `yaml.safe_load` were compared on this file and
produce the same tree apart from the date typing (dates stay strings here, dates in PyYAML).

Rejected with a line number: tabs for indentation, anchors/tags, block scalars (`|`, `>`),
multi-line flow collections, duplicate keys. Keep strings single-line; use `summary`/`caveats`
rather than long prose in one value.

## 9. What changed when the tables were first generated

This is the discrepancy record: the generated blocks were compared against the hand-written tables
in `docs/hw/README.md`, `docs/INSTALL-HW.md` and `README.md` before the markers were added.

| # | discrepancy | resolution and why |
|---|---|---|
| 1 | `docs/hw/README.md` had a **Remaining gaps** row | dropped: it is a ranked audit (`docs/hw/gap-audit.md`), not a component with a state, install or verify step. Its content is carried by the `fan`, `battery`, `sagv` and `camera` entries |
| 2 | "Power envelope + fan" was one row, and "battery health / charge limit / bypass / adaptive TDP" was one row | split into `power`, `fan`, `battery`, `adaptive-tdp` (plus the already separate `rapl-power`): `docs/INSTALL-HW.md` already verified them separately, and one component per verify unit keeps the check mapping exact |
| 3 | state cells were prose ("fixed in software, awaiting on-device check", "protocol recovered, tooling ready", "fix shipped", ...) | mapped to the fixed vocabulary (`awaiting-install`, `working`, `partial`, `blocked`, `not-a-gap`); the prose survives as `summary`/`caveats`, so no fact was dropped. The mapping rules are section 3 |
| 4 | "Home button + top LEDs: protocol recovered, tooling ready" | `awaiting-install`, not `partial`: the encoders are complete and self-tested (9/9 frames), the only missing step is installation |
| 5 | "Bluetooth dead: firmware was present" / "the device never suspended at all" | `awaiting-install`: the fix is complete but the QA host had not installed it (BT: the running initramfs still logged the load failure) |
| 6 | root column mixed scope words: "yes (install)", "yes (udev rule)", "only for the Gamemode flag", "later (driver)", "no to measure; yes to refresh rapl-power", "n/a" | normalised to `yes`/`no`/`partial`/`n/a`; the nuance moved into caveats, and the root requirement was split with the component split (`sagv`: no, `rapl-power`: yes) |
| 7 | `README.md` had one aggregate **extras** row (volume, speakers, TDP, suspend, GPU power, wake policy) | removed as an aggregate: those facts now live in the individual hardware entries. A hand-written pointer sentence after the generated table sends readers to `docs/hw/README.md`, and the registry is linked from it |
| 8 | `README.md` did not list sched_ext or the Decky plugin | added as headline components (`sched_ext`, `decky`): both are our fixes, and the Decky plugin reports sched_ext, Mesa and XMX live, so they belong in the same registry |
| 9 | the verification matrix was per-command (`journalctl ... | grep grabbed`, `cat /sys/...`) | replaced by the stable `tools/oxp3-verify.sh` check ids with a short expected result, plus explicit `cmd:` rows where no check exists (SAGV probe, camera ACPI status). The two whole-system rows are kept as `global_checks` |
| 10 | `docs/INSTALL-HW.md` had a "install state (all)" / "full repo matrix" pair at the top of the matrix | kept verbatim in meaning as `global_checks` (install status, `oxp3-verify.sh`, `oxp3-status.sh`, harness self-test) |

Nothing in these blocks was executed on the host: all validation is offline. The root-only checks
remain device-side acceptance steps, now named by check id.

## 10. Validation performed in this round

| check | result |
|---|---|
| `python3 -m py_compile tools/gen-docs.py` | pass |
| `python3 tools/gen-docs.py --check` twice, with `sha256sum -c` around them | exit 0 both times; all three files byte-identical before/after |
| `python3 tools/gen-docs.py --write` twice | first run updated 3 blocks; second run printed "all 3 generated blocks are up to date" and changed nothing |
| stale-data test: one `summary` changed in `components.yaml` | `--check` exit 1 and printed the first differing block line for `readme-summary`; restoring the file returned exit 0 |
| negative validation (18 mutations over two runs) | duplicate id, bad state, missing doc/implementation path, undeclared check id, duplicate check id, unmapped check, bad root, bad date, unknown key, both id+command, command without expect, component in no block - all rejected with exit 2 and a pointed message |
| marker handling | duplicate/unbalanced markers rejected; prose outside the markers untouched (`git diff` reviewed line by line) |
| parser equivalence | the stdlib subset parser and PyYAML's `safe_load` produce the same tree for `components.yaml` (dates normalised); the `root_required` values are quoted so YAML 1.1 boolean coercion cannot disagree |
| generated-block stability | quoting `root_required` changed no rendered byte: `--check` stayed exit 0 |
| `bash -n tools/repo-test.sh`; `tools/repo-test.sh --fast --only gen-docs` | pass / `PASS 0.037s`, exit 0 |
| `git diff` on the three documents | only the table bodies changed; headings, instalment prose and the other sections are unchanged |

Not run: nothing privileged was needed. The generated content itself is data, not a runtime claim.

## 11. Open points

* `docs/INSTALL-HW.md` sections 2, 3, 6 and 7 (the ordered command set, the flag/opt-out table,
  uninstall, per-component rollback) are still hand-written prose. They share vocabulary with the
  generated tables, so they can drift in wording; the flag names and install entry points are now in
  `components.yaml` and a future round could render the section-3 install column from it
  (`install.entrypoint` is already there; a `remove` field would be needed for the "remove with"
  column).
* `docs/hw/README.md` "Manual order (what the orchestrator runs)" is prose with the same risk. It is
  deliberately left alone: it is a narrative of the orchestrator, not a per-component matrix.
* Nothing checks the reverse direction: a component path that exists but is no longer used is
  invisible. Only paths/files/docs/ids are validated.
* The registry does not attempt to encode benchmark numbers; they stay in `docs/BEST-CONFIG.md`,
  `docs/cpu-sched.md` and the CSVs, with `last_validated.evidence` pointing at them.
