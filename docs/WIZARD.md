# The validation wizard — the checks that need eyes and hands

Date: **2026-11-09**. Scope: `tools/oxp3-wizard.sh`, `tests/wizard/run.sh`,
the `wizard` suite in `tools/repo-test.sh`.

`tools/oxp3-verify.sh` proves everything a program can observe. A handful of acceptance
checks cannot be automated: *does a 3 s volume hold ramp?*, *which LED zone lights up when the
tool writes zone 5?*, *does the home button do the configured action?*, *is the device still warm
after a real suspend?*, *is the fan audibly louder at pwm 255?*, *which speaker EQ variant sounds
better?*, *is XMX upscaling active in-game and artefact-free?* Until now those lived only as prose
in [VALIDATION.md](VALIDATION.md) and got lost between sessions.

The wizard walks the owner through them in dependency order, explains why each one matters, shows
the exact command or physical action, lets every item be skipped, and records the answer with the
verifier check id and timestamp. It is safe: no root, no reboot, no persistent configuration write,
and the one machine-wide action (suspend) runs only after the literal word `suspend` is typed.

Related: [VALIDATION.md](VALIDATION.md) (what each verifier row proves),
[INSTALL-HW.md](INSTALL-HW.md) §5 (install/verify matrix),
[FIRSTRUN.md](FIRSTRUN.md) (post-reboot evidence collector — read-only, automatable),
[HANDOVER.md](HANDOVER.md) §8 (open points),

## 1. When to run it

Run it **after** the hardware install and the post-install reboot, from the **logged-in desktop
session** (a root or SSH shell cannot see the user units, the RGB keeper or the audio session).
Run it again after any change to the volume daemon, LEDs/RGB, home button, sleep policy, fan,
battery, EQ, display or the XMX driver stack.

It is the human half of [INSTALL-HW.md](INSTALL-HW.md) §5: `oxp3-verify.sh` gives the machine
verdict, the wizard gives the observations the machine cannot make. A useful order is:

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
tools/oxp3-verify.sh                 # read-only, no root: what is installed and what fails
tools/oxp3-wizard.sh                 # the hands-on walkthrough (writes only its own record)
```

Do not run it over a serial console while the device is docked unless you intend to test the dock:
the battery/fan phases assume the normal handheld configuration, and the suspend phase needs the
machine to be able to wake.

## 2. Commands and modes

```sh
tools/oxp3-wizard.sh                     # every phase, interactive, resumable
tools/oxp3-wizard.sh --phase volume      # one phase; repeatable or comma-separated
tools/oxp3-wizard.sh --phase sleep,battery
tools/oxp3-wizard.sh --resume            # continue the newest record under docs/state
tools/oxp3-wizard.sh --resume docs/state/validation-<ts>.json
tools/oxp3-wizard.sh --non-interactive   # print the plan; no prompt, no command, no write
tools/oxp3-wizard.sh --dry-run           # plan + the exact commands; no command, no write
tools/oxp3-wizard.sh --selftest          # offline state-machine self-test
```

| flag | effect |
|---|---|
| (none) | interactive; every answer is appended to the journal immediately |
| `--phase NAME[,NAME]` | restrict to phases; validated against the ten phase names |
| `--resume [FILE]` | continue a run; already answered items are not asked again. Without `FILE` the newest `docs/state/validation-*.journal` is used |
| `--non-interactive` | prints phases, items, check ids, docs and the action text; writes nothing, runs nothing |
| `--dry-run` | like the plan, plus the exact command per item and the output paths; still writes/runs nothing |
| `--state-dir DIR` | put the record somewhere else (tests use this) |
| `--simulate` | internal test mode: every command is stubbed and logged; used by `tests/wizard/run.sh` |
| `--selftest` | offline assertions over the answer/state machine, journal, JSON writer, restore queue and item inventory |

Exit codes: `0` completed/planned (skips allowed), `1` at least one item failed,
`2` usage/state error, `130` interrupted (Ctrl-C runs the registered restores first).

Environment overrides: `OXP3_WIZARD_CMD_TIMEOUT` (20 s, every executed command),
`OXP3_WIZARD_VERIFY_TIMEOUT` (180 s), `OXP3_WIZARD_SUSPEND_TIMEOUT` (420 s),
`UNIQUE_NAME` (recorded in the JSON; default `wizard`).

## 3. The phases

The order is a dependency order: the preflight establishes what is installed, the physical checks
build on that, and the final free-text box closes the record. Each phase explains itself, offers
`s` to skip the phase and `q` to stop (answers so far stay recorded).

| # | phase | why it needs a human | gate before anything is written |
|---|---|---|---|
| 1 | `preflight` | install state, the preflight prerequisite verdict and the current verifier verdict are context for every later answer | read-only |
| 2 | `volume` | a hold must step once, not ramp; only the on-screen level shows it | read-only |
| 3 | `leds` | the 5-zone vendor HID map, brightness and one effect must be seen | phase word **LEDS** |
| 4 | `homebtn` | the journal proves a press, only the owner knows whether the action happened | read-only |
| 5 | `sleep` | one real s2idle cycle and whether the device cooled down | the literal word **suspend** |
| 6 | `battery` | the limit/bypass writes are root-only; the observed threshold/behaviour is the evidence | printed `sudo` commands only |
| 7 | `fan` | audibility of manual pwm steps and whether the curve follows load | printed `sudo` commands only |
| 8 | `audio` | which EQ variant actually sounds better | phase word **AUDIO** |
| 9 | `xmx` | upscaling active and artefact-free needs a launched game | read-only prerequisite probe |
| 10 | `final` | the one thing that is still wrong is usually in the owner's head, not in sysfs | none |

Invasive phases require the phase word once, then a per-item `[y/N]` for each command. The word is
deliberately different from the suspend word so a mistyped answer cannot start a suspend.

## 4. Item → verifier check → doc section

Every item records the verifier ids it is the human counterpart of (an empty cell means the item is
context or a preference, not an assertion). The ids are the stable `state` keys of
`tools/oxp3-verify.sh --json`; the verifier's current state for each id is copied into the item's
`evidence` when `preflight.verify` runs.

| item | what it asks | verify check(s) | doc |
|---|---|---|---|
| `preflight.install` | Install state | — | docs/INSTALL-HW.md §5 |
| `preflight.verify` | Verifier summary | — | docs/VALIDATION.md §1 |
| `preflight.preflight` | Preflight verdict | — | docs/PREFLIGHT.md |
| `preflight.session` | Validation session | — | docs/WIZARD.md §2 |
| `vol.hold_ramp` | Hold Volume-Up: ramp or step? | `vol.hwdb`, `vol.hold` | docs/VALIDATION.md §3 volume |
| `vol.taps` | Three taps | — | docs/VALIDATION.md §3 volume |
| `vol.down` | Hold Volume-Down | `vol.hold` | docs/VALIDATION.md §3 volume |
| `vol.daemon` | Volume daemon state | `vol.unit`, `vol.grab`, `vol.topology`, `vol.holder` | docs/VALIDATION.md §3 volume |
| `led.zone1` | Zone 1: left stick | `led.status`, `led.zones`, `led.write` | docs/hw/home-button-leds.md §6 |
| `led.zone2` | Zone 2: right stick | `led.status`, `led.zones`, `led.write` | docs/hw/home-button-leds.md §6 |
| `led.zone5` | Zone 5: G-key | `led.status`, `led.zones`, `led.write` | docs/hw/home-button-leds.md §6 |
| `led.zone6` | Zone 6: top strip | `led.status`, `led.zones`, `led.write` | docs/hw/home-button-leds.md §6 |
| `led.zone7` | Zone 7: controller connector | `led.status`, `led.zones`, `led.write` | docs/hw/home-button-leds.md §6 |
| `led.brightness` | Brightness levels 1-4 | — | docs/hw/home-button-leds.md §6 |
| `led.effect` | One effect | `led.zones` | docs/hw/home-button-leds.md §6 |
| `led.rgbd` | RGB keeper config | `rgbd.unit`, `rgbd.check` | docs/VALIDATION.md §3 rgbd |
| `homebtn.unit` | Home-button daemon | `homebtn.unit`, `homebtn.mapping`, `hid.access` | docs/VALIDATION.md §3 homebtn |
| `homebtn.press` | Press the home button once | — | docs/hw/home-button-leds.md §7 |
| `sleep.policy` | logind sleep policy | `sleep.units`, `sleep.trigger` | docs/hw/sleep-power.md |
| `sleep.pre` | Suspend baseline | `sleep.stats` | docs/VALIDATION.md §3 sleep |
| `sleep.suspend` | One real suspend | `sleep.stats`, `sleep.suspend` | docs/VALIDATION.md §3 sleep |
| `sleep.warm` | Warmth after wake | `sleep.measure` | docs/hw/sleep-power.md |
| `sleep.wake` | How did it wake? | — | docs/hw/sleep-power.md |
| `bat.limit` | Charge limit | `bat.threshold`, `bat.limit` | docs/hw/battery-tdp.md §5 |
| `bat.bypass` | Charge bypass | `bat.behaviour` | docs/hw/battery-tdp.md §5 |
| `bat.restore` | Reset battery state | `bat.threshold`, `bat.behaviour` | docs/hw/battery-tdp.md §5 |
| `fan.auto` | Fan auto baseline | `pwr.fan` | docs/hw/power-fan.md |
| `fan.manual` | Manual fan steps | `pwr.fan`, `fan.savemode` | docs/hw/power-fan.md |
| `fan.curve` | Fan curve under load | — | docs/hw/power-fan.md |
| `fan.restore` | Restore fan mode | — | docs/hw/power-fan.md |
| `aud.eq_on` | EQ ON | `aud.scripts`, `aud.eq`, `aud.dummy` | docs/hw/audio-harman-apo.md |
| `aud.eq_off` | EQ OFF | `aud.scripts`, `aud.eq` | docs/hw/audio-harman-apo.md |
| `aud.pref` | A/B preference | — | docs/hw/audio-harman-apo.md |
| `aud.model` | EQ model table | `aud.model` | docs/hw/audio-harman-apo.md |
| `xmx.launch` | XMX launch prerequisites | `gfx.xmx.icd`, `gfx.xmx.helper`, `gfx.xmx.igc` | docs/xess-xmx.md |
| `xmx.upscale` | Upscaling active | `gfx.xmx.icd`, `gfx.xmx.trace` | docs/xess-xmx.md |
| `xmx.artifacts` | Artefacts | `gfx.xmx.driver`, `gfx.xmx.kernels` | docs/xess-xmx.md |
| `xmx.note` | XMX notes | — | docs/xess-xmx.md |
| `misc.other` | Anything else broken? | — | docs/HANDOVER.md §8 |
| `misc.overall` | Overall verdict | — | docs/HANDOVER.md §8 |

## 5. What it changes — and what it never touches

**Never:** no `sudo`, no package/unit installation, no `systemctl daemon-reload`/restart,
no reboot, no udev/hwdb write, no kernel/EC poke, no `/etc` write, no generated-doc edit.
Privileged steps are *printed* and the owner runs them.

**Live, non-persistent writes by the wizard (both restored):**

* `leds`: `tools/oxp3-led.py solid/brightness/effect` frames over the vendor HID node. Restore is
  `systemctl --user restart oxp3-rgbd` when `~/.config/oxp3-rgb.toml` exists (the keeper
  re-applies the saved state), otherwise `tools/oxp3-led.py off`.
* `audio`: `tools/audio/eq-on.sh` / `eq-off.sh`, which install/remove the user-scope PipeWire
  drop-in and restart the user audio services. The state at phase start is captured and restored at
  the end (EQ on if it was on, off if it was off).

**Machine-wide action:** `systemctl suspend`, only in the `sleep` phase and only after the word
`suspend`. The wizard then polls `/sys/power/suspend_stats` until `success` increments (timeout
420 s) and records `success` delta and `last_hw_sleep`.

**Printed-only restores** (the wizard cannot run them without root, so the exact command is shown
and recorded as `manual` in the JSON `restore` array):

```sh
sudo tools/oxp3-powerctl.py fan auto                  # or: fan manual <pwm read at baseline>
sudo tools/oxp3-powerctl.py battery bypass off
sudo tools/oxp3-powerctl.py battery limit off
```

If a phase is skipped, nothing is changed and the restore list stays empty. Ctrl-C runs the
registered automatic restores before exiting.

## 6. The record

Two files plus a journal, all under `docs/state/`:

* `validation-<UTC timestamp>.json` — machine-readable; schema `1`.
* `validation-<UTC timestamp>.md` — the human summary (tables per phase, restore, suggestions).
* `validation-<UTC timestamp>.journal` — append-only source of truth for `--resume`; never edit
  by hand.

The JSON is derived from the journal and looks like this (abridged):

```json
{
  "schema": 1, "tool": "oxp3-wizard", "version": "1.0.0", "unique_name": "wizard",
  "generated_at": "2026-11-09T04:20:14Z", "started_at": "...", "mode": "interactive",
  "run_id": "20261109T042014Z", "resumed": false,
  "host": {"kernel": "7.2.7-an28.fc45.x86_64", "root": false, "repo": "~/git/.../oxp3-anatase-fixes"},
  "phases": ["preflight", "volume", "..."],
  "verifier": {"exit_code": "1", "counts": "total=72 pass=41 fail=4 skip=27 unknown=0",
               "failing": "bt.journal,bt.controller,bt.diag,inst.backups",
               "checks": {"vol.hold": "skip", "sleep.stats": "skip"}},
  "items": [
    {"phase": "volume", "id": "vol.hold_ramp", "title": "Hold Volume-Up: ramp or step?",
     "status": "pass", "answer": "no", "command": "tools/oxp3-voltap/oxp3-voltap check --kv",
     "checks": ["vol.hwdb", "vol.hold"],
     "evidence": ["cmd:...", "verify:vol.hold=skip"], "timestamp": "2026-11-09T04:20:31Z"}
  ],
  "counts": {"total": 40, "pass": 31, "fail": 1, "skip": 8, "blocked": 0},
  "phases_summary": {"volume": {"total": 4, "pass": 4, "fail": 0, "skip": 0, "blocked": 0}},
  "restore": [{"resource": "led", "status": "restored", "command": "tools/oxp3-led.py off"}],
  "last_validated_suggestions": [{"component": "volume", "date": "2026-11-09",
      "evidence": "docs/state/validation-<ts>.md: volume 4/4 pass (v1.0.0, 2026-11-09)",
      "one_line": "components.yaml: volume.last_validated.evidence = \"...\" (date: ...)"}],
  "open_points": ["..."]
}
```

Per item: `status` is `pass` (the expected observation/answer), `fail` (the observation
contradicts the fix, or a preference question was answered negatively), `skip` (owner skipped or
did not confirm), `blocked` (a required command failed or a prerequisite is missing). The user's
answer is verbatim; the command is what was run or printed; `checks` and `evidence` carry the
verifier ids and their state at run time.

### What a later session does with it

The wizard does **not** regenerate docs. Instead it prints the exact edit for
`docs/hw/components.yaml` `last_validated` (one line per component) and repeats it in the
Markdown; the source of truth stays `components.yaml`, and
`python3 tools/gen-docs.py --write` regenerates the tables in a later session:

```sh
# console / md output, verbatim:
components.yaml: volume.last_validated.evidence = "docs/state/validation-20261109T042014Z.md: volume 4/4 pass (v1.0.0, 2026-11-09)" (date: 2026-11-09)
```

Put that value under the component's existing `last_validated:` block and update its `date:`,
then run `tools/gen-docs.py --check` before `--write`. Do not hand-edit the generated blocks.

## 7. Resume and interrupted runs

* Every answered item is appended to the journal immediately, so a crash, Ctrl-C or a power loss
  keeps everything answered so far.
* `--resume` (newest) or `--resume FILE` continues that record, reuses its path, and does not ask
  answered items again. On resume the verifier map is reloaded from the record's JSON so the
  copied check states stay meaningful without re-running the verifier.
* The suspend item is recorded only after the machine wakes; if the device is powered off during
  suspend, resume re-asks that item (the baseline `sleep.pre` record is kept).
* A phase is only skipped automatically when all its items already have a recorded answer.

## 8. Safety model

1. **No privilege escalation.** The wizard never calls `sudo` or `pkexec`; privileged steps are
   printed with the exact command. The only file it writes is its own record under
   `docs/state/` (or `--state-dir`).
2. **No suspend by accident.** The suspend item requires the literal word `suspend`; an empty
   answer, a skip, a plan/dry-run/selftest/simulate mode, or the wrong word never suspend.
   `systemctl suspend` is run as the user, so a missing polkit permission degrades to a recorded
   `blocked` with the `sudo` hint.
3. **Typed confirmation for live writes.** The LED and audio phases require their phase word
   (`LEDS` / `AUDIO`) *and* a per-command `[y]`; a mistyped word skips the whole phase without
   running anything.
4. **Timeouts.** Every executed command runs under `timeout`; a timeout is `blocked`, never a
   silent success. The three windows are overridable (see §2).
5. **Restore.** LED/RGB and EQ are restored automatically at the end; fan/battery restores are
   printed and recorded as `manual`. Ctrl-C runs the automatic restores before exiting.
6. **Read-only by default.** `--non-interactive` and `--dry-run` execute nothing and create
   nothing, so they can be run over SSH or from a script.
7. **No secret capture.** Answers are single-line observations; `$HOME` is redacted in recorded
   paths and command output. Nothing is uploaded.

## 9. Tests and offline validation

```sh
tools/oxp3-wizard.sh --selftest     # 59 assertions: state machine, journal, JSON, restore queue,
                                    # inventory (10 phases / 40 items), check-id existence
tests/wizard/run.sh                 # 39 end-to-end checks, scripted stdin, --simulate
tools/repo-test.sh --only wizard    # the suite as the repo runs it
```

The end-to-end suite feeds scripted answers through the real prompt path and asserts the produced
JSON schema, per-item fields, counts, resume, the suspend gate (no run without the word, one
recorded simulated cycle with it), the LED phase word gate, the restore registration, and that
plan/dry-run write nothing and the tests write nothing under `docs/state`.

Offline validation recorded on 2026-11-09 (this host, no root, no host change):

* `bash -n tools/oxp3-wizard.sh` and `bash -n tests/wizard/run.sh`: clean.
* `--selftest`: **59 checks passed, 0 failed**.
* `tests/wizard/run.sh`: **39 checks, 0 failed**.
* `--non-interactive` and `--dry-run`: exit 0, list 10 phases / 40 items, create no file and run no
  command (the dry run prints the exact LED/suspend commands).
* `tools/repo-test.sh --only wizard`: PASS (~2 s).
* A simulate run of `--phase preflight,volume` produces schema-1 JSON: 8/8 pass, verifier check
  ids copied per item, journal/MD written next to it; `--resume` keeps all 7 items.
* A simulate run of `--phase leds` with the wrong phase word executes no LED command and skips the
  phase; with `LEDS` it records 8/8 pass and one `led` restore (`simulated`).
* A simulate run of `--phase sleep` with an empty suspend answer records `skip` and does not
  execute `systemctl suspend`; with the word it records one `success +1 / last_hw_sleep 8s` cycle.

## 10. Open points

* The answers are **observations, not measurements**: `sleep.warm` records the owner's warmth
  impression, not a temperature; `fan.manual` records audibility, not dB. That is intentional —
  the numeric counterparts are `sleep.measure` and `pwr.fan` in the verifier.
* Zone names in the LED phase (left/right stick, G-key, top strip, connector) come from
  [home-button-leds.md](hw/home-button-leds.md). If a unit lights a different zone, record
  `wrong zone` — the wizard stores the answer verbatim rather than assuming.
* The EQ A/B writes the user PipeWire drop-in for the duration of the phase and restores the prior
  state at the end; an interrupted run (Ctrl-C) also restores, but a `kill -9` leaves the drop-in
  in place. The exact fix is `tools/audio/eq-off.sh` (or `eq-on.sh`).
* Root-only steps (logind restart, battery limit/bypass, fan mode) are printed, not run. The owner
  stays responsible for the sudo command; the wizard records what was observed and the exact reset
  command, but it cannot prove the command ran.
* The wizard does not run the verifier with `--full` or `--with-changes`; the invasive automated
  rows stay in [VALIDATION.md](VALIDATION.md) §1. It reads the default-mode JSON only.
* `--simulate` exists for the offline test suite; it is not a substitute for a real run and is not
  offered in the normal help flow.
* If the repository moves, the home-button/RGB units still reference the old checkout path
  (documented in the components registry); the wizard records absolute commands but does not
  update units.
