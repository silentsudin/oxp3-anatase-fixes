# Validating the OneXPlayer 3 fixes — one command

`tools/oxp3-verify.sh` is the acceptance harness for everything in
[INSTALL-HW.md](INSTALL-HW.md): it turns the verification matrix into one run with
`PASS` / `FAIL` / `SKIP` / `UNKNOWN` per check, an exit code, a remediation hint per
check and a machine-readable `--json` mode. The default run is read-only and needs no
root; `--full` adds the functional tests that are invasive or need root (volume
tap/hold, one LED write, a real suspend, a battery-drain measurement).

Nothing here changes the host: default mode only reads sysfs/procfs/units/journals and
runs the repository's own read-only tools; `--full` changes live state but never
persistent configuration; only `--with-changes` touches persistent files (currently
exactly one thing: activating the speaker EQ). No reboot is ever performed.

## 1. Commands

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

# quick, read-only, no root — this is the post-reboot one-liner
tools/oxp3-verify.sh

# machine-readable (same checks, JSON on stdout)
tools/oxp3-verify.sh --json

# also the functional tests: volume tap/hold (root), LED write, real suspend (root),
# battery-drain measurement. Run it from the desktop session; suspend part uses sudo.
sudo tools/oxp3-verify.sh --full

# the opt-in activation that changes persistent user config (speaker EQ drop-in)
tools/oxp3-verify.sh --full --with-changes

# offline unit tests of the parsers (no hardware; synthetic fixtures)
tests/verify/run.sh          # == tools/oxp3-verify.sh --selftest
```

A 60 s suspend is long by default; override the two long windows:

```sh
sudo OXP3_VERIFY_SUSPEND_S=30 OXP3_VERIFY_MEASURE_S=120 tools/oxp3-verify.sh --full
```

`--with-changes` requires `--full` (exit 3 otherwise): the harness refuses to make a
persistent change as a side effect of a "just look" run.

## 2. Exit codes and states

| exit | meaning |
|---|---|
| 0 | no `FAIL` and at least one `PASS`. `SKIP`/`UNKNOWN` alongside passes are normal: a machine can be healthy without the optional pieces |
| 1 | at least one `FAIL` (something is installed but broken, or an invariant does not hold) |
| 2 | nothing passed: only `SKIP`/`UNKNOWN` (nothing installed / wrong context) |
| 3 | usage error or the harness could not start (e.g. `--with-changes` without `--full`); same convention as the other tools |

| state | meaning |
|---|---|
| `PASS` | the fix is present and the probe proved it |
| `FAIL` | present but broken, or the probe contradicted the fix |
| `SKIP` | not installed, not applicable, not privileged, or nothing to observe yet (no suspend attempt, no button press). The hint says how to install/trigger it |
| `UNKNOWN` | readable but not assertable here (e.g. only `auto_null` is visible, so "no audio seat" cannot be told apart from a wedged card) |

`PASS`+`SKIP` is exit 0 on purpose: `--all` does not install the opt-in gaming
scheduler or adaptive TDP, and a plain game launch leaves no igdext trace.

## 3. Check inventory — what each check proves

Default mode runs **55 checks**; `--full` adds 4 (**59**); `--with-changes` adds
`aud.activate` (**60**). The ids are stable (they are the `state` keys in `--json`).

### volume (4; `vol.hold` only in `--full`)

| id | proves | how |
|---|---|---|
| `vol.unit` | `oxp3-voltap` is running | `systemctl show` LoadState/ActiveState; `SKIP` when not installed (e.g. the hhd-patch path) |
| `vol.grab` | the daemon holds the rocker device (EVIOCGRAB) | this boot's journal contains `grabbed /dev/input/eventN` (the "for probing" line does not count) |
| `vol.hwdb` | the kernel releases the rocker key at press time, so a hold cannot ramp | `/etc/udev/hwdb.d/84-oxp3-volume-keys*.hwdb` exists, has a `KEYBOARD_KEY_*=<key>` force-release entry, and `/etc/udev/hwdb.bin` is newer than it (compiled) |
| `vol.hold` (root) | a hold yields exactly one step, not a ramp | `tools/oxp3-voltap/oxp3-voltap selftest`: creates the uinput device and emits taps through the real state machine |

### home button / LEDs / RGB (9 + `led.write` in `--full`)

| id | proves | how |
|---|---|---|
| `hid.rule` | the vendor hidraw node gets user access via udev | `/etc/udev/rules.d/90-oxp3-hid.rules` present |
| `led.status` | our tool speaks the vendor HID protocol | `oxp3-led.py status` reads the report descriptor + zone table |
| `led.zones` | all 5 OXP3 zones and 11 presets are addressable | the status output lists them |
| `hid.access` | `/dev/hidrawN` is writable by the invoking user | `-w` on the node reported by `oxp3-led.py`; `SKIP` when the rule is not installed, `FAIL` when the rule is installed but the node is still root-only |
| `homebtn.unit` | the home-button daemon runs | `systemctl --user is-active oxp3-homebtn` |
| `homebtn.mapping` | a press becomes `KEY_HOMEPAGE` | the unit's `ExecStart` uses `--key homepage` and no `--exec` |
| `homebtn.press` | a real press was decoded | this boot's user journal has `listening on /dev/hidrawN` and a `button 0x.. pressed` line; `SKIP` until the button is pressed once |
| `rgbd.unit` | the RGB keeper holds our state | user unit active and `~/.config/oxp3-rgb.toml` present |
| `rgbd.check` | the config parses and encodes | `oxp3-rgbd.py --check --config <cfg>` (dry run: no frames written) |
| `led.write` (invasive) | a write frame is actually accepted by the device | `oxp3-led.py solid 0000ff --sides 6`, then re-applies `~/.config/oxp3-rgb.toml` when present, otherwise turns zone 06 off again |

### IMU (1)

| id | proves | how |
|---|---|---|
| `imu.selftest` | BMI260 works and HHD exposes gyro to Steam | `tools/oxp3-imu.py selftest` |

### sleep (5 + 2 in `--full`)

| id | proves | how |
|---|---|---|
| `sleep.mem` | s2idle is the selected sleep state | `/sys/power/mem_sleep` contains `[s2idle]` |
| `sleep.stats` | a real suspend happened, not a 3 s abort | `success > 0` **and** `last_hw_sleep > 0`; `FAIL` when attempts failed or when `success > 0` with `last_hw_sleep == 0` (the original never-suspended bug); `SKIP` before the first attempt |
| `sleep.units` | the wake policy is applied at boot | `oxp3-sleep-wakeup` active (and reports pre/log state) |
| `sleep.wakeup` | the devices that must not wake the machine are disarmed | for each USB id / PCI address in `files/99-oxp3-sleep-wakeup.rules`: `…/power/wakeup == disabled` |
| `sleep.audit` | the audit tooling reads the suspend record | `tools/sleep-power-measure.sh audit` produces the capability + record blocks (its exit code is incidental) |
| `sleep.measure` (long) | the machine does not drain like a laptop in a bag | `sleep-power-measure.sh run -w N`: `PASS` ≤ 1 %/h, `WARN` 1–2 %/h, `FAIL` > 2 %/h; `SKIP` (exit 2) on AC or unreadable battery |
| `sleep.suspend` (root, long) | a real s2idle sleep/resume cycle works now | `sleep-power-measure.sh suspend -w N` via `rtcwake`, same thresholds; `FAIL` when the machine refuses to suspend |

### Bluetooth (5)

| id | proves | how |
|---|---|---|
| `bt.files` | the pinned firmware is where the driver looks first | both `ibt-00a0-0291-{iml,pci}.sfi` in `/usr/lib/firmware/updates/intel` |
| `bt.dracut` | the initramfs gets the firmware (the actual root cause) | `/etc/dracut.conf.d/60-oxp3-bt-firmware.conf` present; `SKIP` when a layered firmware rpm covers it instead |
| `bt.journal` | this boot loaded the firmware | no `Failed to load Intel firmware file` in `journalctl -k -b` |
| `bt.controller` | the radio answers, not just `hci0` | `btmgmt info` has an address (or `bluetoothctl show` has a `Controller`); `FAIL` on `Index list with 0 items` / `No default controller` |
| `bt.diag` | the pinned-hash diagnosis agrees | `tools/oxp3-bt-firmware.sh --check` exit code and verdict |

### power / fan (6)

| id | proves | how |
|---|---|---|
| `pwr.zones` | both RAPL package zones are readable | `oxp3-powerctl status --json`: `intel-rapl:0` (MSR) and `intel-rapl-mmio:0` (MMIO) with PL1/PL2/PL4 |
| `pwr.pl1` | the TDP sync invariant holds (MSR PL1 == MMIO PL1) | equal limits; notes the recorded 35 W baseline; `FAIL` when they differ (the lower one caps the package) |
| `pwr.profile` | the saved profile is applied | `/etc/oxp3/power.json` profile (read from the tool's `saved_state`) maps to the live `platform_profile`; `SKIP` when no profile was saved |
| `pwr.fan` | the EC fan node is sane | `fan1_input` 1–12000 rpm, `pwm1_enable` 1/2, `pwm` 0–255 (the `acpi_fan` EIO is a known cosmetic bug, reported in the detail) |
| `pwr.adaptive` | adaptive TDP is inert unless opted in | `PASS` for inactive+disabled, active+enabled, and a manually started unit; `FAIL` for enabled-but-dead |
| `pwr.rapl` | `rapl-power.service` publishes power values | the `rapl.service` indicator from `oxp3-status.sh --json` |

### battery (2)

| id | proves | how |
|---|---|---|
| `bat.threshold` | the charge limit is reachable (reports the value) | readable `BAT*/charge_control_end_threshold` (writes need root) |
| `bat.behaviour` | charge-inhibit ("bypass") is reachable (reports the state) | readable `BAT*/charge_behaviour` |

### audio (5 + `aud.activate` with `--with-changes`)

| id | proves | how |
|---|---|---|
| `aud.scripts` | the EQ can be turned on/off | `tools/audio/eq-on.sh` and `eq-off.sh` executable |
| `aud.server` | an audio session exists | `pactl info` |
| `aud.eq` | the filter-chain sink is loaded | drop-in installed **and** `oxp3_speaker_eq.sink` in `pactl list short sinks`; `SKIP` when the drop-in is not installed |
| `aud.dummy` | no "Dummy Output" fallback | default sink is a real one; `UNKNOWN` when only `auto_null` exists (no seat here, or the card is wedged — the hint lists the checks) |
| `aud.model` | the recovered 8-band model is intact | `tools/audio/eq_response.py` |
| `aud.activate` (persistent) | `eq-on.sh` really activates the sink | runs it, then checks the sink appears; the only persistent change the harness can make |

### display (4)

| id | proves | how |
|---|---|---|
| `dsp.hdr` | the compositor state is readable (HDR/WCG/depth/VRR reported) | `tools/display/display-mode.sh status` → `kscreen-doctor -o`; HDR off is a valid reported state |
| `dsp.icc` | an ICC profile is applied or not | the same output; ours is installed only after `display-mode.sh icc panel` |
| `dsp.panel` | the panel claims what we rely on | the EDID verdict contains 10-bit + PQ/HDR10 + BT.2020 |
| `dsp.vibrance` | the saturation shader tooling is present | `tools/display/vibrance.sh status` |

### graphics / XMX / scheduler (12)

| id | proves | how |
|---|---|---|
| `gfx.mesa` | the host Mesa build is the recorded one | prefix `ec418f04e589965a` (`mesa.host`) |
| `gfx.xmx.driver` | the XMX driver is the recorded one | prefix `4b1d55ea9483417f` (`xmx.driver`) |
| `gfx.xmx.helper` | the nodgrf compile helper is in the game env | `xmx.helper` |
| `gfx.xmx.igc` | IGC/ocloc is present | `xmx.igc` |
| `gfx.xmx.kernels` | the XMX kernel cache is populated | `xmx.kernels` (count > 0) |
| `gfx.xmx.icd` | the per-game XMX ICD/env resolves | `xmx.icd` |
| `gfx.xmx.trace` | an igdext trace was produced | `xmx.trace`; `SKIP` when absent, which is expected unless tracing was enabled for that launch |
| `gfx.xe` | the patched `xe.ko` is the one loaded | `modinfo -F filename xe` is under `updates/` (`xe.module`) |
| `gfx.working` | the recorded working build still matches | `tools/check-working.sh` (driver prefixes, no expat override, PL1 35 W, patched xe, Heroic GameMode off) |
| `sched.state` | sched_ext is enabled | `sched.state` == `enabled`; gated on the unit: not installed → opt-in `SKIP` |
| `sched.scx` | `scx_cosmos` is actually the running scheduler | `sched.scx_cosmos` |
| `sched.unit` | the gaming-scheduler unit is installed and active | `sched.unit` |

### Decky (3)

| id | proves | how |
|---|---|---|
| `decky.dir` | the plugin is installed (files needed by the loader) | `~/homebrew/plugins/oxp3-status` or `…/OXP3 Status` with `plugin.json`, `main.py`, `dist/` |
| `decky.backend` | the backend imports | `python3 -c 'import main'` in the plugin directory |
| `decky.status` | the status backend agrees the machine is usable | `tools/oxp3-status.sh --json` `overall`; `warn` is reported, `fail` fails this check |

## 4. Fix → check map

| fix (INSTALL-HW.md §) | checks |
|---|---|
| 1 volume rocker | `vol.unit`, `vol.grab`, `vol.hwdb`, (`vol.hold`) |
| 2 LEDs / home button | `hid.rule`, `led.status`, `led.zones`, `hid.access`, `homebtn.unit`, `homebtn.mapping`, `homebtn.press`, (`led.write`) |
| 2 RGB keeper | `rgbd.unit`, `rgbd.check` |
| 3 sleep / wake policy | `sleep.mem`, `sleep.stats`, `sleep.units`, `sleep.wakeup`, `sleep.audit`, (`sleep.measure`), (`sleep.suspend`) |
| 4 Bluetooth firmware | `bt.files`, `bt.dracut`, `bt.journal`, `bt.controller`, `bt.diag` |
| 5 power envelope / fan | `pwr.zones`, `pwr.pl1`, `pwr.profile`, `pwr.fan`, `pwr.rapl` |
| 5 adaptive TDP (opt-in) | `pwr.adaptive` |
| 6 battery care | `bat.threshold`, `bat.behaviour` |
| 7 speaker EQ | `aud.scripts`, `aud.server`, `aud.eq`, `aud.dummy`, `aud.model`, (`aud.activate`) |
| 8 display HDR / ICC / vibrance | `dsp.hdr`, `dsp.icc`, `dsp.panel`, `dsp.vibrance` |
| 9 Mesa / XMX / sched_ext / xe | `gfx.mesa`, `gfx.xmx.driver`, `gfx.xmx.helper`, `gfx.xmx.igc`, `gfx.xmx.kernels`, `gfx.xmx.icd`, `gfx.xmx.trace`, `gfx.xe`, `gfx.working`, `sched.state`, `sched.scx`, `sched.unit` |
| 10 Decky plugin | `decky.dir`, `decky.backend`, `decky.status` |
| (bonus) IMU | `imu.selftest` |

## 5. Expected output on a healthy machine

Reference install = [INSTALL-HW.md §2.1](INSTALL-HW.md) (`oxp3-fixes-fast.sh` + `hw-install
--sleep --leds --power --rgb --audio`, RGB keeper configured, Decky plugin installed,
gaming scheduler and adaptive TDP *not* enabled, no igdext trace):

```
oxp3-verify 1.0.0  mode=default  kernel=7.2.7-an28.fc45.x86_64  root=no

[PASS ] volume     ...
...
== summary: 55 checks: 47 pass, 0 fail, 8 skip, 0 unknown -> exit 0
```

* 8 `SKIP` rows are the documented opt-ins: `vol.*` (3, only when the hhd volume
  patch is used instead of the standalone daemon), `pwr.adaptive` (1), `gfx.xmx.trace`
  (1), `sched.*` (3).
* Without the Decky plugin, `decky.dir`/`decky.backend` also `SKIP`: 45 pass / 10 skip.
* `--full` on the same machine: `59 checks: 51 pass, 0 fail, 8 skip` (adds
  `vol.hold`, `led.write`, `sleep.measure`, `sleep.suspend`). Run it on battery;
  `sleep.measure` is `SKIP` on AC by design.

Representative passing lines:

```
[PASS ] power      pwr.pl1                PL1 35 W on both zones (tdp-sync invariant holds)
[PASS ] sleep      sleep.stats            last s2idle sleep 8.4s (success=1, fail=0)
[PASS ] bluetooth  bt.controller          btmgmt sees the controller
[PASS ] audio      aud.dummy              default sink: oxp3_speaker_eq.sink
[PASS ] graphics   gfx.mesa               ec418f04e589965a (match)
[PASS ] decky      decky.status           overall=ok (ok=25 warn=0 fail=0 unknown=0)
```

## 6. Interpreting skips

| skip you see | means | how to verify |
|---|---|---|
| `vol.*` "not installed" | you use the hhd volume-rocker patch (INSTALL-HW §2.1), not the standalone `oxp3-voltap` daemon | play with the rocker: one step per press and no ramp; `journalctl -b | grep -i hhd` for the hhd patch. Not covered by this harness (different implementation) |
| `homebtn.*` / `rgbd.*` "no user systemd instance" | the shell has no user manager bus (root shell, SSH without a seat) | re-run inside the logged-in desktop session |
| `homebtn.press` "no press since boot" | the daemon is listening but nobody pressed the button | press the home button once, re-run |
| `rgbd.*` "no user config" | `~/.config/oxp3-rgb.toml` was never installed | install `tools/oxp3-rgbd.toml.example` and enable the unit |
| `sleep.stats` "no suspend attempt since boot" | accurate: nothing to prove yet | `--full` (root) performs a real suspend, or `sudo tools/sleep-power-measure.sh suspend -w 60` |
| `sleep.measure` "measurement invalid" | on AC, or the battery is unreadable; the tool refuses to fake a number | unplug, re-run |
| `bt.dracut` | no standalone dracut drop-in; a layered firmware rpm may cover it | `bt.journal`/`bt.controller` carry the verdict |
| `aud.eq` "EQ not installed" | the drop-in is missing | `tools/audio/eq-on.sh` |
| `aud.dummy` `UNKNOWN` | only `auto_null` is visible: this shell has no audio seat, or the SOF card is wedged | `pactl list sinks`; `journalctl --user -u pipewire -n 50`; the trap in [hw/README.md](hw/README.md) |
| `gfx.xmx.trace` | no igdext trace for the current game launch (expected unless tracing is enabled) | enable tracing for one launch, re-run |
| `sched.*` | the gaming scheduler is opt-in and not installed/loaded | `sudo systemctl enable --now oxp3-gaming-sched` |
| `pwr.adaptive` | adaptive TDP is opt-in and not installed | `sudo tools/oxp3-power-install.sh install --adaptive-tdp` |
| `pwr.profile` | no `/etc/oxp3/power.json`, so no profile is asserted | `sudo /usr/local/libexec/oxp3-powerctl profile balanced --save` |
| `decky.*` | the Decky plugin was not installed (the `decky.status` row still passes) | `cp -r tools/decky-oxp3-status ~/homebrew/plugins/oxp3-status` |

## 7. Recorded result on this host (2026-11-09, stock/partial install)

Default mode, no root, in an SSH/desktop session on `anatase-oxp3`, kernel
`7.2.7-an28.fc45.x86_64`:

```
== summary: 55 checks: 28 pass, 3 fail, 23 skip, 1 unknown -> exit 1
   failing: bt.journal bt.controller bt.diag
```

This is the honest state of this machine, not a harness problem:

* the three Bluetooth failures are the known boot-time gap: both firmware files are in
  `/usr/lib/firmware/updates/intel` (`bt.files` PASS, `bt.diag` sees `files ok=2/2`),
  but this boot's initramfs predates them, so the kernel logged
  `Failed to load Intel firmware file … (-2)`, `hci0` exists without firmware and
  `btmgmt` lists 0 controllers. That is exactly what `--check` reports with exit 1
  (see [QA.md](QA.md) row 47); the fix is the initramfs step + reboot
  (`sudo tools/oxp3-bt-firmware.sh`), which this repository does not run automatically.
* 23 `SKIP`: none of the hardware fixes/units are installed on this stock host (volume,
  LEDs, home button, RGB keeper, sleep units, adaptive TDP, Decky plugin, no saved power
  profile), plus the documented opt-ins (trace, gaming scheduler, EQ drop-in).
* 1 `UNKNOWN`: `aud.dummy` — this shell sees only `auto_null`, so "no audio seat here"
  and "SOF card wedged" cannot be distinguished; the hint lists the two commands.
* 28 `PASS`: IMU, s2idle/mem_sleep, suspend audit, power zones/PL1/fan/RAPL, battery
  limit and behaviour, audio server/model/scripts, display HDR/ICC/EDID/vibrance,
  Mesa/XMX/xe/working-build, status overview.
* `--full` here (non-root, 1 s measurement) = 59 checks: adds `vol.hold` SKIP
  (`needs root`), `led.write` SKIP (hidraw 0600), `sleep.measure` SKIP (on AC),
  `sleep.suspend` SKIP (`needs root`); still exit 1 for the same three Bluetooth rows.
  The root-only paths (`vol.hold`, `sleep.suspend`) were **not** executed here: this
  repository's rules forbid `sudo`, so they are device-side acceptance steps.

Verbatim evidence (never overwritten, regenerate when the machine changes):

* [`docs/state/verify-default-2026-11-09.txt`](state/verify-default-2026-11-09.txt) — 108 lines, the run above.
* [`docs/state/verify-default-2026-11-09.json`](state/verify-default-2026-11-09.json) — `--json`, 55 checks, schema 1, exit 1.
* [`docs/state/verify-selftest-2026-11-09.txt`](state/verify-selftest-2026-11-09.txt) — `selftest: 52 checks passed, 0 failed`.

## 8. Offline validation performed (no root, no host change)

* `bash -n tools/oxp3-verify.sh` and `sh -n tests/verify/run.sh`: clean.
* `tools/oxp3-verify.sh --selftest`: **52 assertions / 0 failures** over the parsers
  (`display-mode` ANSI output, `powerctl` domains/fan/profile, `status` indicators,
  `suspend_stats`, `bt --check`, `led status`, `rgbd --check`, `pactl` sinks,
  the two journals, `sleep-audit`), the exit-code mapping, and a fixed inventory check
  that the 60 declared check ids are exactly the documented set (no typos, no drops).
* `tools/oxp3-verify.sh`: 55 checks, exit 1, 3.7 s wall clock. Two consecutive
  `--json` runs were identical apart from `generated_at` (volatile values such as fan
  rpm can legitimately differ between runs).
* `tools/oxp3-verify.sh --json | python3 -m json.tool`: valid; `counts`, `exit_code`,
  lowercase states, unique ids and a non-empty `remediation` for every non-pass row.
* `tools/oxp3-verify.sh --with-changes` without `--full`: exit 3 with the reason.
* `tools/oxp3-verify.sh --help`: renders the whole header.
* `--full` (non-root): 59 checks, exit 1; the four functional checks degrade to `SKIP`
  with actionable hints instead of failing. The LED write command shape was validated
  without device access via `tools/oxp3-led.py --dry-run solid 0000ff --sides 6`
  (two 64-byte frames, exit 0).
* No writes outside the private temp dir: after a default run `git status` shows only
  the new files, and no `__pycache__` was created (the harness exports
  `PYTHONDONTWRITEBYTECODE=1`; the reused tools only read).
* `shellcheck` is not installed on this host, so the script is covered by `bash -n`
  and the runtime paths above only (same limitation as the rest of the repo, see
  [QA.md](QA.md) row 17).

## 9. Open points

* **The hhd volume-rocker patch is not covered.** If you use the hhd patch instead of
  `oxp3-voltap` (the recommended path in INSTALL-HW §2.1), the three `vol.*` checks
  `SKIP`; the functional rocker test stays manual. Adding a check would need hhd's
  internals or a real key press, neither of which is read-only here.
* **Root-only functional paths were not executed in this round** (no `sudo` policy):
  `vol.hold` and `sleep.suspend`. They are exercised as `SKIP` with the exact command;
  `--full` with `sudo` is the device-side acceptance step.
* `--with-changes` intentionally has exactly one effect (speaker EQ activation). If a
  fix ever needs a persistent install step in the harness, add it there, not in
  `--full`.
* `sleep.measure` refuses to report on AC (by design of `sleep-power-measure.sh`);
  the harness surfaces that as `SKIP`, so a docked verification run cannot prove the
  drain figure.
* The harness reads the live files rather than parsing
  `tools/oxp3-hw-install.sh --status`: that output is a human matrix and not a stable
  interface, so parsing it would duplicate the install knowledge instead of reusing it.
  `oxp3-status.sh --json`, `oxp3-powerctl status --json` and the other tools *are*
  structured interfaces and are reused directly.
* `gfx.xmx.trace`, `sched.*` and `pwr.adaptive` are opt-in by design, so a healthy
  machine still shows those `SKIP`s; the exit code treats that as healthy.
* Zero-padded RGB side ids are not accepted by the current tooling: the LED write check
  must use `--sides 6`, because `tools/oxp3_hid.py:parse_sides()` parses with
  `int(part, 0)` and `--sides 01` raises `ValueError` (the examples in
  `tools/oxp3-led.py` and `docs/hw/home-button-leds.md` show `01,02`). Reported here,
  not changed: it is a pre-existing tooling inconsistency outside this harness's scope.
