# Triage — turn an evidence bundle into a diagnosis

Date: **2026-11-09**. Scope: `tools/oxp3-triage.py`, the fixtures in
`tests/fixtures/firstrun/`, `tests/unit/test_triage.py`, the `triage` suite in
`tools/repo-test.sh`.

`tools/oxp3-firstrun.sh --collect` produces the evidence drop; this tool *reads* it and
answers the question the drop exists for: **what is wrong, how sure are we, and what is
the next command?** One evidence drop should yield conclusions, not a directory someone
has to read by hand.

```sh
tools/oxp3-triage.py docs/state/firstrun-2026-11-09T0410Z          # human report
tools/oxp3-triage.py bundle.tar.gz --json                          # machine report
tools/oxp3-triage.py bundle/ --out-dir /tmp/triage                 # both, to files
tools/oxp3-triage.py --selftest                                    # offline self-test
tools/oxp3-triage.py --list-rules                                  # the catalogue below
```

Related: [FIRSTRUN.md](FIRSTRUN.md) (how the bundle is produced and what each file is),
[VALIDATION.md](VALIDATION.md) (what every verifier row proves — the triage never
re-runs these, it reads their recorded result), [HANDOVER.md](HANDOVER.md) (the session
guide this tool shortens).

## 1. What it is, and what it is not

It is a **rules engine over recorded evidence**. Every conclusion is derived from files
inside the input; every finding cites the file and the line it read, so it can be
checked. It never runs a probe, never asks for root, never restarts a unit, never
suspends, never touches the network, and never writes outside an explicitly requested
`--out-dir`.

Consequences worth knowing:

* **Missing evidence is `unknown`, never `pass`.** A rule whose declared file is absent
  says so; a bundle with almost nothing in it produces *no* conclusions (and one
  `bundle.gaps` finding that lists what is missing). This is the property the fixture
  `missing-files` pins down.
* **Not installed ≠ broken.** A `SKIP` row from the verifier whose summary says "not
  installed" is an install step, not a defect; it is reported as `not-installed` and
  the precondition rules do not fire. The pre-install fixture asserts this.
* **The pinned numbers live here too.** `ec418f04e589965a` (host Mesa),
  `4b1d55ea9483417f` (XMX driver), `xmx-cm-compile-nodgrf` (the 128-GRF helper) and
  `7.2.7-an28` (the kernel) are the recorded values from
  [VALIDATION.md](VALIDATION.md). A digest that differs *is* the finding.
* **It is not a substitute for the verifier.** The verifier proves presence on the live
  host; the triage explains a recorded snapshot and carries the knowledge about *why* a
  row reads the way it does. Run both when you have the machine.

### Inputs it accepts

| input | detected as | notes |
|---|---|---|
| a bundle directory (`MANIFEST.json` present) | `bundle` | the normal case |
| a `.tar.gz` / `.tgz` / `.tar.xz` / `.tar.bz2` | `bundle` | extracted to a private temp dir; absolute paths, `..`, links and device nodes are refused |
| a directory of loose `docs/state/` files (`verify-*.json`, `oxp3-status-*.json`, `snapshot-*.txt`) | `state-dir` | older snapshots; file-level rules cannot fire, so they report `unknown` |

### Exit codes

| exit | meaning |
|---|---|
| 0 | report produced, no `critical` finding |
| 1 | report produced, at least one `critical` finding (`--strict` also for `high`) |
| 2 | usage error (no input, unreadable output path) |
| 3 | the input is not a readable bundle/tarball |

`1` is data, not a crash: the same convention as `oxp3-verify` (a FAIL is a result).

## 2. The report

The text report has six blocks, in the order a person should read them:

```
oxp3 triage - <input>
  environment summary : kernel, OS, session (inferred), GPU, Mesa digest, XMX digest,
                        bundle counts, verifier summary
FIXES                  : per component, ok | partial | not-installed | broken | unknown
                         with the pass/fail/skip split and the skip categories
FINDINGS               : one block per triggered rule: severity, rule id, confidence,
                         conclusion, evidence (file:line + excerpt), exact commands
ACTIONS                : deduplicated and ranked; each with why, severity, root needed
RULES                  : how many evaluated/triggered/clear/unknown, plus the unknowns
EVIDENCE GAPS          : what is missing and therefore what cannot be concluded
notes                  : limits of the bundle (e.g. powerdevilrc not collected)
```

`--json` emits the same data as a stable document:

```jsonc
{
  "schema": 1,
  "tool": "oxp3-triage", "version": "1.0.0",
  "generated_at": "2026-11-09T04:20:00Z",
  "input":       { "path": "...", "kind": "bundle|state-dir", "root": "...", "files": 26 },
  "bundle":      { "tool": "oxp3-firstrun", "version": "1.0.0", "generated_at": "...",
                   "mode": "collect", "root": false, "kernel": "...", "os": "...",
                   "counts": {...}, "journal": {...}, "partial_data": false,
                   "limits": {...},
                   "verify": { "counts": {...}, "exit_code": 1, "present": true } },
  "environment": { "kernel": "...", "os": "...",
                   "session": { "type": "wayland|x11|unknown",
                                "desktop": "KDE Plasma (kscreen output)|unknown",
                                "gamescope": "no evidence|mentioned in <file>:<line>",
                                "confidence": "inferred|unknown",
                                "sources": ["display/display.txt (...)", ...] },
                   "gpu": "...", "mesa": "ec418f04e589965a (match)",
                   "xmx_driver": "4b1d55ea9483417f (match)",
                   "cpu": "...", "battery": "...", "thermal": "...",
                   "boot": { "uptime_s": 42696.63, "boot_wall_utc": "...",
                             "journal_available": true, "git": {...},
                             "cmdline": "...", "dmi": {...} } },
  "fixes": [ { "id": "volume", "title": "...",
               "checks": {"total": 5, "pass": 1, "fail": 0, "skip": 4, "unknown": 0},
               "installed": false, "state": "not-installed", "why": "...",
               "checks_present": ["vol.topology"], "checks_missing": ["vol.unit", ...],
               "notes": ["skips: needs-root=1, not-installed=3"],
               "evidence": [ { "file": "...", "line": 20, "excerpt": "..." } ] } ],
  "findings": [ { "id": "F1", "rule": "bt.initramfs-stale", "title": "...",
                  "severity": "critical", "priority": 90, "category": "bluetooth",
                  "conclusion": "...", "confidence": "high",
                  "evidence": [ { "file": "...", "line": 110, "excerpt": "..." } ],
                  "actions": [ { "command": "...", "why": "...",
                                 "privileged": true, "note": "" } ] } ],
  "actions": [ { "rank": 1, "command": "...", "why": "...", "privileged": true,
                 "note": "", "priority": 90, "severity": "critical",
                 "rules": ["bt.initramfs-stale"] } ],
  "rules": [ { "id": "...", "title": "...", "state": "triggered|clear|unknown|not-applicable",
               "severity": "...", "category": "...", "reads": ["..."],
               "reads_present": {"firmware": true, "journal_kernel": false},
               "conclusion": "...", "confidence": "...",
               "evidence": [...], "actions": [...] } ],
  "gaps": [ { "artifact": "verify_json", "file": "verify/verify.json",
              "detail": "missing; rules that read it report unknown" } ],
  "notes": [ "...", "..." ]
}
```

`rules` contains **every** rule and its outcome, including `clear`/`unknown`: that is how
a reader can see what was checked and what could not be. `reads_present` is the
machine-readable answer to "did this rule have its evidence?".

### The fix verdicts

| state | meaning |
|---|---|
| `ok` | the checks that prove this component is installed pass, and nothing in its set fails |
| `partial` | installed, but part of the set is not installed or cannot be asserted |
| `not-installed` | no install row passes; only read-only capability rows do (e.g. `vol.topology`) or all rows SKIP with "not installed" |
| `broken` | the component is installed and the verifier contradicts it |
| `unknown` | no rows for it in this bundle (old harness, or a `docs/state` snapshot) |

`installed` is separate on purpose: `volume` can be `not-installed` while
`vol.topology` passes, and `decky` can be `not-installed` while `decky.status` passes
(that row proves the *status backend*, not the plugin). Only the rows that prove
installation count.

## 3. Rule catalogue

All 31 rules, `id` · severity · what it reads → what it concludes. `--list-rules` prints
the same table from the code, so this section cannot drift for long.

**Bluetooth** — the root cause is the initramfs, never the files.

| rule | sev | reads → conclusion |
|---|---|---|
| `bt.initramfs-stale` | critical | `firmware/bluetooth.txt`, `journal/kernel.txt`, `verify/verify.json`, `udev/rules.txt` → both pinned blobs are on disk but the kernel logged `Failed to load Intel firmware file …(-ENOENT)` and no controller came up ⇒ the running initramfs predates the files (`btintel_pcie` probes inside it); rebuild + reboot; if the drop-in predates the boot and it still failed, the initramfs was not regenerated |
| `bt.firmware-missing` | high | `firmware`, `verify_json` → a blob is missing from `updates/intel` ⇒ install the pinned firmware |
| `bt.no-controller` | high | `firmware`, `verify_json`, `journal_kernel` → no controller and no load failure ⇒ reload the driver once, then treat it as firmware |
| `bt.dracut-missing` | medium | `firmware`, `udev` → the dracut drop-in is missing while the blobs exist ⇒ the next rebuild will not carry them; the initramfs is the cause, not the files |

**Sleep / power key** — the platform does suspend; the defects are in the trigger.

| rule | sev | reads → conclusion |
|---|---|---|
| `power.power-key-poweroff` | critical | `sleep/sleep.txt` inhibitors, `udev/rules.txt`, `verify_json` → PowerDevil holds a `block` on `handle-*` and there is no logind policy ⇒ the power key's fallback is `poweroff`; with the drop-in present the KDE action decides instead (and `powerdevilrc` is **not** in the bundle — stated in the finding) |
| `sleep.trigger-uninstalled` | high | `udev`, `verify_json`, `checklist` → no logind drop-in / polkit rule ⇒ no suspend path that works without a desktop session |
| `sleep.logind-not-restarted` | high | `checklist` (`logind-restart`), `manifest`, `udev` → the drop-in is newer than logind's start ⇒ logind has not read it; restart it (cross-checked against the file evidence, so a contradictory checklist reports `unknown`) |
| `sleep.no-attempt` | info/medium | `sleep`, `checklist`, `verify_json` → `success=0` ⇒ nothing asked the kernel to sleep; **not proof** that suspend is broken. `max_hw_sleep≈524 s` with `success=0` is stale; `journalctl -k` implies `-b`, so history needs `-b all` |
| `sleep.attempt-failed` | high | `sleep`, `verify_json` → `fail`/`failed_*` moved, or `success>0` with `last_hw_sleep=0` (the abort-in-3 s pattern) ⇒ read `last_failed_dev`/`last_failed_step` |
| `sleep.block-inhibitor` | medium | `sleep` → a `block` on `sleep` from something other than PowerDevil ⇒ logind will not act on session/idle requests |

**XMX / graphics** — the failure mode is silent, so the rule carries the "Off" story.

| rule | sev | reads → conclusion |
|---|---|---|
| `xmx.ocloc-missing` | critical | `gfx/xmx.txt`, `status/status.json`, `verify_json` → no `ocloc` ⇒ every CM compile fails, XeSS aborts, the game silently resets upscaling to **Off**; `tools/xmx-enable.sh` refuses without it |
| `xmx.driver-mismatch` | high | `status_json`, `verify_json`, `xmx` → the per-game driver is not `4b1d55ea9483417f` ⇒ XeSS init fails/artefacts and falls back to Off |
| `xmx.helper-no-nodgrf` | high | `status_json`, `xmx`, `verify_json` → `ANV_CM_HELPER` is not the `xmx-cm-compile-nodgrf` helper ⇒ `-doubleGRF` (256-GRF) kernels, white-dot artefacts; clear the kernel cache after fixing |
| `xmx.flatpak-helper` | medium | `status_json`, `xmx` → the Steam Flatpak override uses the old helper and has no `VK_DRIVER_FILES`/`INTC_ALT` ⇒ XMX cannot work through it (per-game launch options override it) |
| `xmx.trace-absent` | low/medium | `status_json`, `verify_json`, `xmx` → no `igdext_trace.log` ⇒ the shim never loaded (or tracing was off); if `INTC_ALT_DRIVER_EXTENSIONS_PATH` is not in the collected env, the shim is only reachable through the launcher's per-game environment |
| `env.mesa-mismatch` | medium | `status_json`, `verify_json`, `xmx` → the host driver is not `ec418f04e589965a` ⇒ the `/usr` overlay stack was wiped or re-layered (every Anatase update does this) |

**Audio**

| rule | sev | reads → conclusion |
|---|---|---|
| `audio.wedged` | high | `audio/audio.txt`, `verify_json` → a SOF card exists but the only sink is `auto_null`/Dummy Output ⇒ the card is wedged (the throwaway-PipeWire trap); reboot. No server ⇒ `unknown` (no seat vs wedged cannot be told apart); no card at all ⇒ the SOF driver did not bind |
| `audio.eq-inactive` | medium | `audio`, `verify_json` → the filter-chain drop-in exists but `oxp3_speaker_eq` is not loaded ⇒ `tools/audio/eq-on.sh` |

**Volume**

| rule | sev | reads → conclusion |
|---|---|---|
| `volume.degraded-busy` | high | `journal/oxp3-units.txt`, `journal/oxp3-user-units.txt`, `verify_json` → `DEGRADED: … (EBUSY)` ⇒ every candidate is held; hhd grabbed the AT keyboard, so the topology assumption does not hold — pick one path (hhd patch *or* `oxp3-voltap`) |
| `volume.not-installed` | info | `verify_json`, `checklist` → no install row passes ⇒ the rocker still ramps (stock bug); this is an install step |
| `udev.hwdb-not-compiled` | medium | `udev/rules.txt` → the `.hwdb` source is newer than `hwdb.bin` ⇒ the force-release rule is not compiled in, a hold can still ramp |

**Verification, units, environment, bundle**

| rule | sev | reads → conclusion |
|---|---|---|
| `verify.failures` | high | `verify_json` → FAIL rows that no specific rule explains; the row's `remediation` is the fix |
| `verify.not-installed` | medium | `verify_json`, `checklist` → N SKIPs because components are not installed ⇒ run the installer; `SKIP` is not a failure |
| `verify.unknown` | low | `verify_json` → UNKNOWN rows (readable but not assertable), outside the ones a rule already explains |
| `verify.optins` | info | `verify_json` → opt-in components off by design; explicitly **not** a finding |
| `decky.not-installed` | low | `verify_json`, `decky/decky.txt` → no `~/homebrew/plugins/oxp3-status` |
| `units.failed` | high | `units/system.txt`, `journal_units` → `systemctl --failed` is non-empty; a unit that failed once can stay dead |
| `power.pl1-mismatch` | high | `verify_json` (`pwr.pl1`), `power/power.txt` → MSR and MMIO PL1 disagree ⇒ the lower one caps the package, the saved profile is not in force |
| `power.profile-unsaved` | low | `checklist`, `verify_json`, `power` → no `/etc/oxp3/power.json` ⇒ the boot profile is whatever the last writer left |
| `env.kernel-mismatch` | low | `system/environment.txt`, `manifest`, `verify_json` → the kernel is not `7.2.7-an28` ⇒ re-assert the driver stack before trusting any graphics row |
| `bundle.gaps` | low | `manifest`, `verify_json`, `status_json` → artifacts are missing/denied/truncated or the input is a `docs/state` snapshot ⇒ some conclusions are limited; this is the one finding a near-empty bundle still emits |

## 4. How to add a rule

A rule is a decorated function; nothing else changes (no registry edit, no docs table
update required for it to work, but the catalogue above should gain a line).

```python
@rule(
    "audio.sink-suspended",                 # stable id: <component>.<failure mode>
    "the filter sink is suspended mid-stream",   # title, one line, states the symptom
    "medium",                               # critical | high | medium | low | info
    "audio",                                # category (matches the fix verdicts)
    ("audio", "verify_json"),               # bundle keys this rule reads
)
def audio_sink_suspended(f: Facts) -> Outcome:
    if not f.audio.get("sinks"):
        return Outcome("unknown", "no sink list in this bundle")   # never a pass
    if "SUSPENDED" not in f.bundle.text("audio"):
        return Outcome("clear", "the sink is running")
    return Outcome(
        "triggered",
        "…what it means, in the words you would use on the phone…",
        [f.bundle.evidence_at("audio", "SUSPENDED")],   # file + line + excerpt
        [Action("systemctl --user restart wireplumber", "…why…", privileged=False)],
    )
```

Rules to follow:

1. **Declare your evidence** in `reads`. The engine skips the function and reports
   `unknown` when none of those files exist, and the report records
   `reads_present`, so the "no evidence ⇒ no conclusion" property holds by construction.
2. **Return `unknown` with a reason** whenever the specific fact is absent — not
   `clear`. `clear` means "I looked and it is fine".
3. **Cite `(file, line, excerpt)`** for everything the conclusion depends on; the test
   suite verifies the file exists and the line is in range.
4. **Give the exact command**, mark `privileged` honestly, and put the *why* in
   `Action.why` (it is printed in the report, so a future reader does not need this
   file).
5. **Do not invent thresholds**: if a digest or version is pinned, use the constant at
   the top of the tool and add it to this document.
6. Add the fixture case: extend `tests/fixtures/firstrun/build-fixtures.py` with a
   scenario (or adjust an existing one), regenerate, extend `EXPECTATIONS` in the tool's
   `selftest()` and the expectations in `tests/unit/test_triage.py`.

## 5. How a future session should use it

1. Get the drop: `docs/state/firstrun-<timestamp>/` or an archive from the owner.
2. `tools/oxp3-triage.py <drop>` — read `FIXES` first (what is installed), then
   `FINDINGS` (what is wrong and why), then `ACTIONS` (the next commands).
3. For each finding the owner will act on, keep the evidence pointer: it is the thing
   to quote in the report/commit ("`journal/kernel.txt:110` shows `-ENOENT` while
   `firmware/bluetooth.txt:3` shows the blob present").
4. Check `EVIDENCE GAPS` before drawing a negative conclusion. `unknown` in `RULES` is
   the tool telling you it could not check, not that the thing is fine.
5. After the fix + reboot, collect a second bundle and triage it the same way; compare
   `fixes[*].state` and the finding ids. A finding that disappears is the proof the fix
   took; a finding that stays has the same evidence pointers as before.
6. If a rule is wrong or a new failure mode appears, add the rule (section 4) rather
   than writing the diagnosis into a chat message: the next drop should produce it.

Worked example (the pre-install state of this host, `docs/state` + a bundle):

```
FIXES:   volume not-installed · leds not-installed · sleep not-installed ·
         bluetooth broken · power partial · audio/display/graphics ok
FINDINGS: F1 critical bt.initramfs-stale   F2 critical power.power-key-poweroff
          F3 high sleep.trigger-uninstalled  F4 medium bt.dracut-missing …
ACTIONS: 1. sudo tools/oxp3-bt-firmware.sh  2. sudo systemctl reboot
         3. sudo tools/oxp3-hw-install.sh --sleep  4. sudo systemctl restart systemd-logind
```

That is the same conclusion [HANDOVER.md §5](HANDOVER.md) reaches by hand — which is the
point.

## 6. Fixtures and tests

`tests/fixtures/firstrun/` holds eight synthetic bundles; `build-fixtures.py` in that
directory generates them (run it after editing a scenario). They are internally
consistent: the manifest checklist, the verifier rows and the file-level evidence agree,
so a verdict cannot be satisfied by one file alone.

| fixture | encodes | expected headline |
|---|---|---|
| `healthy` | a fully installed machine, after a traced launch | no findings; every fix `ok` (sched `not-installed`, opt-in) |
| `preinstall` | the current host: driver stack in, hardware fixes not | `bluetooth` broken; volume/leds/rgb/sleep/decky `not-installed`; no invented failures |
| `bt-not-loaded` | blobs + drop-in present, kernel `-ENOENT`, no controller | one critical: the initramfs predates the files |
| `xmx-no-trace` | wrong driver hash, `cm-compile.sh`, no ocloc, no trace | five XMX findings, `ocloc` first (critical), `graphics` broken |
| `audio-wedged` | SOF card present, only `auto_null`, EQ sink missing | `audio.wedged` + `audio.eq-inactive`, `audio` broken |
| `sleep-never-triggered` | policy installed, drop-in newer than logind, `success=0` | `sleep.logind-not-restarted`, `sleep.no-attempt`, power-key warning |
| `volume-degraded` | `DEGRADED: … (EBUSY)` in this boot's unit journal | `volume.degraded-busy`, `volume` broken |
| `missing-files` | a manifest and a checklist, nothing else | only `bundle.gaps`; every fix `unknown`; no rule says `clear` without its file |

Checks:

```sh
tools/oxp3-triage.py --selftest                    # 258 assertions over the fixtures
python3 -m unittest discover -s tests/unit -p 'test_triage.py' -q   # 37 unit tests
tools/repo-test.sh --only triage                   # the suite in the repo runner
```

The self-test asserts, for each fixture, the *exact* finding set, the fix verdicts, the
priority ordering, contiguous ids/ranks, that every evidence pointer is a real line in a
real file, tarball/directory parity, that a real `docs/state` snapshot is read without
over-claiming, and that a bundle with no evidence produces `unknown` rather than `pass`.

## 7. Open points

* **`powerdevilrc` is not in the bundle.** The power-key/lid rule can see the logind
  policy and the inhibitor, but not the KDE action (`PowerButtonAction`, `LidAction`,
  `AutoSuspendAction`), so it states that limit instead of guessing. Adding
  `~/.config/powerdevilrc` (and maybe `~/.config/kscreen/`) to the collector would close
  it; that is a `oxp3_firstrun.py` change, deliberately not made here.
* **No complete firstrun bundle was committed under `docs/state/` yet** — that directory
  currently holds the earlier loose snapshots (`verify-*.json`, `oxp3-status-*.json`,
  `snapshot-*.txt`), which the tool reads as a `state-dir`. The first `--collect` drop
  should be triaged and (optionally) kept out of git, as `.gitignore` already does for
  `docs/state/firstrun-*/`.
* **The rules pin today's documented behaviour.** `4b1d55ea9483417f`,
  `ec418f04e589965a`, the `xmx-cm-compile-nodgrf` marker, `7.2.7-an28`,
  `s2idle`/`HandlePowerKey` semantics: if a recorded value or a documented behaviour
  changes, the constant, [VALIDATION.md](VALIDATION.md) and the fixture must change
  together, otherwise the finding is wrong. That is intentional — it is how a
  behaviour change becomes visible.
* **Session type is inferred.** The bundle does not record `XDG_SESSION_TYPE`, so the
  report derives Wayland/KDE from the `kscreen-doctor` output and the user session bus,
  and says `inferred`. The same collector change as `powerdevilrc` would make it exact.
* **The hhd volume-rocker patch is only partly visible.** The rules can see
  `DEGRADED:` and the verifier's `vol.*` rows, but not hhd's own state; the "pick one
  path" conclusion stays a human decision (docs/hw/volume-buttons.md).
