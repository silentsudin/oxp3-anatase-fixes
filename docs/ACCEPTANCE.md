# Acceptance — judging the six goal items from evidence

Date: **2026-11-09**. Scope: `tools/oxp3-acceptance.sh` / `tools/oxp3_acceptance.py`,
`tests/unit/test_acceptance.py`, `tests/fixtures/acceptance/`, the `acceptance`
suite in `tools/repo-test.sh`, and this page.

The work for the OneXPlayer 3 goal is spread over a component registry, a verifier,
a repository test runner, unit/C/integration suites and a handful of runs that
need the device. "Are we done?" should not depend on what the last session
remembered. This page and one command answer it from evidence:

```sh
tools/oxp3-acceptance.sh              # human report, read-only, ~16 s
tools/oxp3-acceptance.sh --json       # machine report (schema 1)
tools/oxp3-acceptance.sh --verbose    # every mapped check and manual step
tools/oxp3-acceptance.sh --full       # also the full-only suites and gen-docs --check
tools/oxp3-acceptance.sh --save F     # save the raw evidence bundle
tools/oxp3-acceptance.sh --evidence F # evaluate a saved bundle (no live run)
tools/oxp3-acceptance.sh --selftest   # offline aggregation tests (fixtures)
make acceptance                       # same as the default run
```

It composes the existing reporters and re-implements none of them:

| source | what it contributes |
|---|---|
| `docs/hw/components.yaml` | component state (cited, never copied), implementation paths, doc link, verify check ids, install entrypoint |
| `tools/oxp3-verify.sh --json` | the live `PASS`/`FAIL`/`SKIP`/`UNKNOWN` row for every mapped check id |
| `tools/oxp3-health.sh --json --no-verify` | the install checklist (`ok`/`todo`/`partial`/`pending`/`unknown` per step) and the preflight verdict |
| `tools/repo-test.sh --fast --json --only …` | the relevant offline suites (`unit`, `verify-selftest`, `triage`, `decky-selftest`, `decky-cli`, `led-selftest`, `hid-selftest`, `eq-model`, `sleep-cycles`) |
| `docs/state/` | the file artefact that proves device work (`sleep-cycles-*/report.json`) and the optional manual sign-offs |

Nothing is called proven from a source that did not run: a missing verifier,
health or repo-test report becomes a named evidence gap.

## 1. Verdict vocabulary (closed set)

| verdict | means |
|---|---|
| `implemented-and-proven-offline` | the implementation is complete and every offline suite/check we can run without the device is green. **This is an offline claim only.** It is never printed as, and never means, "proven on the device" |
| `awaiting-install` | the implementation exists and is offline-validated, but an install step from the firstrun checklist is still open (`todo`/`partial`/`pending`/`unknown`), so no device check can pass yet |
| `awaiting-hardware` | the install is not the blocker; the missing proof needs the device (a real press, a real suspend), or no automated check covers the item |
| `blocked` | `components.yaml` marks a component `blocked`, or a live required check / item-specific offline suite FAILs |
| `not-a-gap` | the investigation recorded that the unit has no gap for this component (camera, IMU); no action is expected |

The verdict is the furthest evidence stage reached, not a promise. A second,
independent field says whether the item is actually **complete**:

```
complete = verdict in (implemented-and-proven-offline, not-a-gap)
           and every required device check PASSes
           and every non-optional acceptance gate is satisfied
```

The acceptance gates are the manual steps the automated suites cannot decide:
a listening check (`audio.listen`), a real Decky session (`decky.qam`), a
sleep-cycle report with verdict `GOOD`. They are satisfied only by the artefacts
described in §4 — never by age, memory or a green offline suite.

### How a component state maps to a verdict

The component state is always cited from `docs/hw/components.yaml`; it is not
re-typed here. The evaluator applies the following rules in order:

1. all cited components `not-a-gap` → `not-a-gap`;
2. any cited component `blocked` → `blocked` (and any live FAIL is added to the reason);
3. any live required check or item-specific offline suite FAIL → `blocked`;
4. a firstrun install step is not `ok`/`n/a` → `awaiting-install`;
5. no component state can be cited → `awaiting-hardware` (offline proof cannot be claimed without the registry);
6. every required device check PASSes → `implemented-and-proven-offline`;
7. otherwise → `awaiting-hardware`.

`unit` and `verify-selftest` exercise repository-wide health; a failure there is
reported (`== repository-wide suite failures`) but does not by itself turn an
unrelated item's targeted evidence into a FAIL. The audit item treats them as
its own evidence.

## 2. The six-item table

Recorded run: [`state/acceptance-2026-11-09.txt`](state/acceptance-2026-11-09.txt)
(the file is the verbatim output; the table below is its summary). Component
states, paths and check ids are all read from
[`hw/components.yaml`](hw/components.yaml); the doc column links to the
per-topic evidence.

| # | goal item | implementation (from components.yaml) | automated proof | manual/device step still needed | verdict |
|---|---|---|---|---|---|
| 1 | Sticky volume buttons (`volume`) | `tools/oxp3-voltap`, `files/84-oxp3-volume-keys.hwdb`, `tools/lib/install-common.sh` | checks `vol.unit`, `vol.grab`, `vol.topology`, `vol.holder`, `vol.hwdb`, `vol.hold`; `tests/unit/test_voltap.py`; opt-in `voltap-integration`; `verify-selftest` | `sudo tools/oxp3-hw-install.sh --volume`, then `sudo tools/oxp3-verify.sh --full` (`vol.hold`) | `awaiting-install` |
| 2 | Home button and top RGB LEDs (`leds`, `rgb`) | `tools/oxp3_hid.py`, `tools/oxp3-led.py`, `tools/oxp3-homebtn.py`, `tools/oxp3-hid.rules`, `tools/oxp3-homebtn.service`, `tools/oxp3-rgbd.py`, `tools/oxp3-rgbd.service` | checks `hid.rule`, `hid.access`, `hid.node`, `led.status`, `led.zones`, `led.sides`, `homebtn.unit`, `homebtn.mapping`, `homebtn.press`, `led.write`, `rgbd.unit`, `rgbd.check`, `rgbd.timing`; `led-selftest`, `hid-selftest`, `tests/unit/test_{led,hid,rgbd,homebtn_lifecycle}.py`; captured frames | `sudo tools/oxp3-hw-install.sh --leds --rgb`; press the home button once; `sudo tools/oxp3-verify.sh --full` | `awaiting-hardware` |
| 3 | Sleep efficiency (`sleep`) | `files/oxp3-sleep-wakeup*`, `files/oxp3-sleep-pre.service`, `files/oxp3-sleep-log.service`, `files/99-oxp3-sleep-wakeup.rules`, `files/logind-oxp3.conf`, `files/49-oxp3-suspend.rules`, `tools/sleep-power-measure.sh` | checks `sleep.mem`, `sleep.units`, `sleep.wakeup`, `sleep.trigger`, `sleep.audit`, `sleep.stats`, `sleep.measure`, `sleep.suspend`; `sleep-cycles` fixtures; `tests/unit/test_{sleep_cycles,sleep_trigger}.py` | `sudo tools/oxp3-hw-install.sh --sleep` and restart logind/reboot; `sudo tools/sleep-cycle-test.sh --cycles 3` on battery (GOOD) | `blocked` |
| 4 | Speaker audio, Harman APO + gain staging (`audio`) | `tools/audio`, `files/52-oxp3-speakers.conf`, `files/oxp3-audio-route.service`, `files/oxp3-audio-resume@.service` | checks `aud.scripts`, `aud.server`, `aud.eq`, `aud.dummy`, `aud.model`, `aud.level`, `aud.activate`; `eq-model` suite; `tests/unit/test_eq_measure.py`, `tests/unit/test_level.py`, `tests/unit/test_audio_install.py`; isolated-PipeWire proof in [`audio-validation.md`](audio-validation.md) | `tools/audio/eq-on.sh` (loud) / `eq-on.sh --safe` / `eq-off.sh`, plus `gain-stage.sh undo` to revert the ALSA gain, while listening once on the internal speakers (`audio.listen` sign-off) | `implemented-and-proven-offline` |
| 5 | Decky status plugin (`decky`) | `tools/decky-oxp3-status`, `tools/oxp3-status.sh` | checks `decky.dir`, `decky.backend`, `decky.status`; `decky-selftest`, `decky-cli`, in `--full` `decky-build`/`decky-package` (byte-identical zip) | `tools/decky-oxp3-status/build.sh --offline`, copy into `~/homebrew/plugins/`, then open QAM in a real Decky session (`decky.qam` sign-off) | `blocked` |
| 6 | Wider hardware/compatibility audit and further gaps (18 components) | the driver stack plus the remaining hardware components; the audit itself is [`hw/gap-audit.md`](hw/gap-audit.md) | `triage` (8 fixture bundles), `verify-selftest`, `unit`, `gfx.*`/`pwr.*`/`bat.*`/`dsp.*`/`bt.*`/`imu.*` checks | `sudo tools/oxp3-bt-firmware.sh` then reboot (Bluetooth controller); opt-in sched-ext/adaptive-TDP; SAGV is upstream work, not installable | `blocked` |

Recorded counts: **6 items, 0 complete**, 1
`implemented-and-proven-offline`, 1 `awaiting-install`, 1 `awaiting-hardware`,
3 `blocked`; offline evidence green for 5/6 items. The live verifier at that
moment failed `decky.status`, `gfx.mesa`, `gfx.working`, `sleep.aspm` and
`sleep.sdcard`.

## 3. What would let us call each item done

These are the exact completion gates. An item is finished only when the command
in §2 passes on the device (or the cited artefact exists) **and** the verdict is
`implemented-and-proven-offline` with no pending acceptance gate.

* **Volume buttons** — `sudo tools/oxp3-hw-install.sh --volume`, then
  `vol.unit`, `vol.grab` and `vol.hold` PASS (the last one drives the real state
  machine through uinput). A green `voltap-integration` run is the optional
  end-to-end complement; one physical hold that steps and releases cleanly is
  the recommended extra check, not a gate the evaluator can require.
* **Home button / RGB** — install `--leds --rgb`, enable the user units, press
  the home button once (`homebtn.press`), and let `led.write` put one real frame
  on the strip; `homebtn.unit`, `homebtn.mapping`, `hid.rule`, `hid.access` and
  `rgbd.unit` PASS.
* **Sleep efficiency** — install `--sleep`, restart logind (or reboot), then one
  clean `sudo tools/sleep-cycle-test.sh --cycles 3` run on battery that prints
  **GOOD** (< 1 %/h while suspended, no unexpected wake source). `sleep.stats`
  must report a completed cycle; `sleep.suspend` is the harness equivalent.
* **Speaker audio** — the EQ sink loaded and default, `aud.*` PASS, and one
  owner listening check recorded as the `audio.listen` sign-off after toggling
  `eq-on.sh`/`eq-off.sh`. The software model cannot hear the drivers.
* **Decky plugin** — the packaged plugin installed into the Decky homebrew
  directory, `decky.dir`/`decky.backend`/`decky.status` PASS, and the OXP3 panel
  seen in a real Decky QAM (`decky.qam` sign-off).
* **Audit / further gaps** — no component is `blocked` without a written
  upstream reason. Concretely: Bluetooth controller visible after the initramfs
  fix and reboot; the opt-in scheduler/adaptive-TDP installed or explicitly
  declined; the power profile saved; SAGV accepted as upstream work or closed by
  kernel changes. `inst.backups` must be resolved by adopting/backing up the
  in-place edits (see [`ROLLBACK.md`](ROLLBACK.md)).

## 4. Recording the gates that a script cannot decide

The evaluator only accepts file evidence, never memory:

* **Sleep cycle** — `docs/state/sleep-cycles-<UTC>/report.json` with
  `verdict.label == "GOOD"` and `summary.valid_cycles > 0`. `--no-suspend` or
  `--dry-run` reports are explicitly not accepted.
* **Manual sign-offs** — optional file `docs/state/acceptance-signoffs.json`:

  ```json
  {
    "audio.listen": {"by": "owner", "date": "2026-11-09", "note": "EQ on/off audible on the internal speakers"},
    "decky.qam":  {"by": "owner", "date": "2026-11-09", "note": "OXP3 panel loaded, Refresh toasted"}
  }
  ```

  Present keys satisfy the matching gate; absent keys leave it pending. The
  evaluator reads the file and never writes it.

## 5. Output and exit codes

Each item prints: the cited component state, the implementation paths and
install entrypoint, the offline suite states, `N/M required checks PASS` with
the required ids, the pending manual step and command, and the done-when
statement. `--verbose` adds every mapped check and every manual step. The report
closes with the per-verdict count, how many items have green offline evidence,
how many are complete, the remaining manual commands and the failing live checks.

| exit | meaning |
|---|---|
| 0 | all automated evidence green; device/manual work may remain (check `complete`) |
| 1 | a required check/suite FAILed or an evidence source is missing |
| 2 | usage error |
| 3 | no evidence source could run (not even the verifier or the registry) |

## 6. Limits, and what this page is not

* It never runs a suspend, writes a frame, presses a button or asks for root; it
  runs the component tools in their documented read-only modes. The device steps
  in §2 are printed, not executed.
* It does not re-derive the protocol/RE evidence; that stays in the per-topic
  docs linked from `components.yaml`. It maps each item to the checks and
  artefacts that exist.
* `implemented-and-proven-offline` is deliberately not renamed to "done": the
  offline rung is real evidence, but it is not device proof. An item can sit
  there while `complete` is false, and the report says which gate is open.
* The `acceptance` repo-test suite asserts the fixtures and the live JSON/text
  contract; `--selftest` asserts the aggregation rules without touching the
  host. `gen-docs --check` keeps the generated tables and counts in this
  repository honest; never edit those blocks by hand.

Related: [`HANDOVER.md`](HANDOVER.md) (orientation), [`VALIDATION.md`](VALIDATION.md)
(what each verify id proves), [`FIRSTRUN.md`](FIRSTRUN.md) (install checklist),
[`TRIAGE.md`](TRIAGE.md) (recorded-bundle diagnosis), [`hw/REGISTRY.md`](hw/REGISTRY.md)
(state vocabulary and contribution rules).
