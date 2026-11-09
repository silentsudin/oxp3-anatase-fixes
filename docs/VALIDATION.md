# Validating the OneXPlayer 3 fixes — one command

`tools/oxp3-verify.sh` is the acceptance harness for everything in
[INSTALL-HW.md](INSTALL-HW.md): it turns the verification matrix into one run with
`PASS` / `FAIL` / `SKIP` / `UNKNOWN` per check, an exit code, a remediation hint per
check and a machine-readable `--json` mode. The default run is read-only and needs no
root; `--full` adds the functional tests that are invasive or need root (volume
tap/hold, one LED write, a real suspend, a battery-drain measurement).

It also carries the *deferred-risk probes* from
[UNIT-TESTS.md §5](UNIT-TESTS.md#5-deferred-risks-device-validated-or-still-deferred):
ten checks that turn "needs on-device validation" into an observable row (HID report
descriptor, unknown-AC battery clamp, empty `--sides`, battery-limit default, fan save
mode, mixed write order, duplicate curve points, rgbd journal timing, install-common
host-path helpers, IMU IIO fallback). All but `fan.savemode` are read-only and run in
the default mode; `fan.savemode` writes live/persistent state and therefore only runs
with `--full --with-changes`, restoring everything it touched.

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

# the opt-in persistent changes: speaker EQ drop-in + one saved fan-mode write
# (fan.savemode snapshots and restores /etc/oxp3/power.json and pwm1_enable)
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

Default mode runs **72 checks** (measured 2026-11-09, including `preflight.verdict`).
`--full` adds four (**76**); `--with-changes` adds two more (**78**). The opt-in rows
(`vol.hold`, `led.write`, `life.sim`, `aud.activate` and `fan.savemode`) run only with
`--full` / `--with-changes`; the harness declares **78** ids in total, which `--selftest`
asserts. The ids are stable (they are the `state` keys in `--json`).

### preflight (1)

| id | proves | how |
|---|---|---|
| `preflight.verdict` | the host can take an install: every prerequisite in [PREFLIGHT.md](PREFLIGHT.md) is OK or a handled WARN; `safe: false` names the components that would be skipped | `tools/oxp3-preflight.sh --json`; `PASS` for `safe: true`, `FAIL` for `safe: false`, `SKIP` when the tool is absent |

### volume (6; `vol.hold` only in `--full`)

| id | proves | how |
|---|---|---|
| `vol.unit` | `oxp3-voltap` is running | `systemctl show` LoadState/ActiveState; `SKIP` when not installed (e.g. the hhd-patch path) |
| `vol.grab` | the daemon holds the rocker device (EVIOCGRAB) | this boot's journal contains `grabbed /dev/input/eventN` (the "for probing" line does not count) |
| `vol.topology` | which device the daemon would grab and why (hhd virtual first, physical fallback only when free) | `oxp3-voltap check --kv`: role/selection/holder fields from `/proc/bus/input/devices` + `/sys/class/input`; `PASS` for a hhd-virtual or free at-kbd choice, `FAIL` on a conflict prediction |
| `vol.holder` | the physical AT keyboard is held only by hhd (or nobody) | `/proc/*/fdinfo` `grab: 1` + `fd` symlink; `SKIP` without root, `UNKNOWN` for a non-hhd holder |
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

### daemon lifecycle (3; `life.sim` only in `--full`)

The contract and the manual suspend/hotplug procedures are in
[DAEMONS.md](DAEMONS.md).

| id | proves | how |
|---|---|---|
| `life.fd` | each inspectable active daemon currently holds a live device fd | `/proc/<MainPID>/fd` symlinks: `hidraw` for `oxp3-homebtn`, `event`+`uinput` for `oxp3-voltap`; `SKIP` when only the root daemon is active and this user cannot read its fds; `oxp3-rgbd` opens per apply and is reported, not failed |
| `life.reconnect` | no device loss this boot was left unrecovered | this boot's journal, per daemon: every `device lost:` must be followed by `listening on`/`grabbed`/`applied` (the `watchdog:` heartbeat is excluded); `SKIP` with no lifecycle journal |
| `life.sim` (gated) | the reconnect/backoff/suspend logic survives its simulation | `python3 -m unittest discover -s tests/unit -p 'test_*lifecycle.py' -q`; only with `--full` (it runs the offline suites) |

### IMU (1)

| id | proves | how |
|---|---|---|
| `imu.selftest` | BMI260 works and HHD exposes gyro to Steam | `tools/oxp3-imu.py selftest` |

### sleep (6 + 2 in `--full`)

| id | proves | how |
|---|---|---|
| `sleep.mem` | s2idle is the selected sleep state | `/sys/power/mem_sleep` contains `[s2idle]` |
| `sleep.stats` | a real suspend happened, not a 3 s abort | `success > 0` **and** `last_hw_sleep > 0`; `FAIL` when attempts failed or when `success > 0` with `last_hw_sleep == 0` (the original never-suspended bug); `SKIP` before the first attempt |
| `sleep.units` | the wake policy is applied at boot | `oxp3-sleep-wakeup` active (and reports pre/log state) |
| `sleep.wakeup` | the devices that must not wake the machine are disarmed | for each USB id / PCI address in `files/99-oxp3-sleep-wakeup.rules`: `…/power/wakeup == disabled` |
| `sleep.trigger` | a suspend *request* has a permitted path that does not need a desktop session | read-only: our logind drop-in present and parsed (`systemd-analyze cat-config systemd/logind.conf` → `HandlePowerKey=suspend`, `HandleSuspendKey=suspend`), the polkit rule present and `pkcheck --action-id org.freedesktop.login1.suspend --process $$` says yes, and no `block` inhibitor of type `sleep` exists (`systemd-inhibit --list`). A `block` on `handle-*` (PowerDevil, hhd) is reported but does not fail: it only stops logind's own key handling, not an explicit `Suspend` call. `SKIP` while the policy is not installed |
| `sleep.audit` | the audit tooling reads the suspend record | `tools/sleep-power-measure.sh audit` produces the capability + record blocks (its exit code is incidental) |
| `sleep.measure` (long) | the machine does not drain like a laptop in a bag | `sleep-power-measure.sh run -w N`: `PASS` ≤ 1 %/h, `WARN` 1–2 %/h, `FAIL` > 2 %/h; `SKIP` (exit 2) on AC or unreadable battery |
| `sleep.suspend` (root, long, triple opt-in) | a real s2idle sleep/resume cycle works now | the **only** check that suspends the machine: requires `--full`, `--with-changes` *and* `OXP3_ALLOW_SUSPEND=1`, then runs `sleep-power-measure.sh suspend -w N` via `rtcwake`, and requires `suspend_stats/success` to increment (same thresholds, `FAIL` when the machine refuses to suspend). Without all three opt-ins it is `SKIP` with the exact command |

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
| `aud.activate` (persistent) | `eq-on.sh` really activates the sink | runs it, then checks the sink appears; one of the two persistent changes the harness can make |

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

### deferred tooling risks (10, from UNIT-TESTS §5)

| id | proves | how | gate |
|---|---|---|---|
| `hid.node` | the selected `/dev/hidrawN` is the node whose report descriptor advertises usage page 0xFF00, not just the first input-less 1a86:fe00 node | scans every candidate, `od`s its `report_descriptor` for `06 00 ff`, compares with `oxp3_hid.find_vendor_hidraw()` | default; `SKIP` with no candidate node |
| `led.sides` | an empty `--sides` encodes nothing and never widens to "all zones"; a real selection still encodes | `oxp3-led.py --dry-run solid 0000ff --sides ''` (0 frames) vs `--sides 6` (>0 frames) | default; no device access |
| `rgbd.timing` | the hhd journal the settle/debounce watcher depends on is readable, and the example config encodes frames | `oxp3-rgbd.py --config tools/oxp3-rgbd.toml.example --check` | default; `SKIP` when the node is absent or the journal is not readable (poll fallback) |
| `imu.iio` | the ACPI-id/label device is selected instead of the permissive `i2c-*` + non-empty `in_anglvel_x_raw` fallback | compares `oxp3-imu.py -j info` `sysfs` with every `iio:device*` name/label | default; `SKIP` when only fallback candidates exist |
| `pwr.ac` | `read_ac_online()` is not `None`, so both battery clamps in `Policy.target()` are live (or, with no AC node, performance is not allowed on battery) | one unprivileged `oxp3-adaptivetdp.py --dry-run --once --json` sample + the battery's own `status` | default; `SKIP` with no AC node and a safe rung |
| `pwr.order` | a mixed raise/lower switch orders writes ceiling-first and puts every lowering last | `oxp3-powerctl.py profile balanced --dry-run --json` (the live 35/37/122 vs 35/52/122 limits make this genuinely mixed) | default; read-only |
| `fan.curvedup` | a curve file **with duplicate temperature points** is accepted and yields a defined pwm | `oxp3-powerctl.py fan curve --file <dup> --dry-run --once --json` | default; `SKIP` when no fan node/temperature sensor |
| `bat.limit` | `battery limit` with no argument resolves to 100 % ("off") only as a dry run, and the real no-argument call is refused without root | `battery limit --dry-run --json` + the real no-argument call as the invoking user | default; `SKIP` without a readable charge limit |
| `inst.paths` | `install-common.sh`'s `sys_fw`, `host_mesa`, `flatpak_mesa` resolve against the live `/usr` paths | sources the library and calls the three helpers | default |
| `fan.savemode` | the state file records `mode=manual` after `pwm1_enable` was written | `fan manual --save`, read `/etc/oxp3/power.json`, then restore the fan mode and the state file | `--full --with-changes` only (writes live + persistent state); `SKIP` without root or fan node |

`rgbd.timing` covers the *precondition* of the daemon-timing risk; the settle/debounce
re-apply itself is event-driven and is only visible as `HHD settle +Ns` / re-apply
lines in `journalctl --user -u oxp3-rgbd` after a resume. The `voltap` release-all tail
remains a manual step (see §9).

### undo state (1)

| id | proves | how |
|---|---|---|
| `inst.backups` | every in-place host edit of ours has a timestamped backup image, so an uninstall can put the previous content back | `tools/oxp3-rollback.sh verify` (read-only); `FAIL` lists the unbacked paths and points at `adopt` | default; see [ROLLBACK.md](ROLLBACK.md) |

`inst.backups` is read-only and cross-cutting: it sources nothing and runs
`tools/oxp3-rollback.sh verify`, which compares the files we edit in place against the
manifest and backup images. A `FAIL` is actionable (`adopt`), not a hardware defect;
[ROLLBACK.md](ROLLBACK.md) explains the state and the restore semantics.

## 4. Fix → check map

| fix (INSTALL-HW.md §) | checks |
|---|---|
| (preflight, every component) | `preflight.verdict` |
| 1 volume rocker | `vol.unit`, `vol.grab`, `vol.topology`, `vol.holder`, `vol.hwdb`, (`vol.hold`) |
| 2 LEDs / home button | `hid.rule`, `led.status`, `led.zones`, `hid.access`, `homebtn.unit`, `homebtn.mapping`, `homebtn.press`, (`led.write`) |
| 2 RGB keeper | `rgbd.unit`, `rgbd.check` |
| daemon lifecycle (DAEMONS.md) | `life.fd`, `life.reconnect`, (`life.sim`) |
| 3 sleep / wake policy | `sleep.mem`, `sleep.stats`, `sleep.units`, `sleep.wakeup`, `sleep.trigger`, `sleep.audit`, (`sleep.measure`), (`sleep.suspend`) |
| 4 Bluetooth firmware | `bt.files`, `bt.dracut`, `bt.journal`, `bt.controller`, `bt.diag` |
| 5 power envelope / fan | `pwr.zones`, `pwr.pl1`, `pwr.profile`, `pwr.fan`, `pwr.rapl` |
| 5 adaptive TDP (opt-in) | `pwr.adaptive` |
| 6 battery care | `bat.threshold`, `bat.behaviour` |
| 7 speaker EQ | `aud.scripts`, `aud.server`, `aud.eq`, `aud.dummy`, `aud.model`, (`aud.activate`) |
| 8 display HDR / ICC / vibrance | `dsp.hdr`, `dsp.icc`, `dsp.panel`, `dsp.vibrance` |
| 9 Mesa / XMX / sched_ext / xe | `gfx.mesa`, `gfx.xmx.driver`, `gfx.xmx.helper`, `gfx.xmx.igc`, `gfx.xmx.kernels`, `gfx.xmx.icd`, `gfx.xmx.trace`, `gfx.xe`, `gfx.working`, `sched.state`, `sched.scx`, `sched.unit` |
| 10 Decky plugin | `decky.dir`, `decky.backend`, `decky.status` |
| (bonus) IMU | `imu.selftest`, `imu.iio` |
| (deferred, UNIT-TESTS §5) | `hid.node`, `led.sides`, `rgbd.timing`, `pwr.ac`, `pwr.order`, `fan.curvedup`, `bat.limit`, `inst.paths`, (`fan.savemode`) |

## 5. Expected output on a healthy machine

Reference install = [INSTALL-HW.md §2.1](INSTALL-HW.md) (`oxp3-fixes-fast.sh` + `hw-install
--sleep --leds --power --rgb --audio`, RGB keeper configured, Decky plugin installed,
gaming scheduler and adaptive TDP *not* enabled, no igdext trace):

```
oxp3-verify 1.0.0  mode=default  kernel=7.2.7-an28.fc45.x86_64  root=no

[PASS ] volume     ...
...
== summary: 67 checks: 56 pass, 0 fail, 11 skip, 0 unknown -> exit 0
```

(Recorded before `vol.topology`/`vol.holder`: the current harness adds two
rows, normally +1 `PASS` and +1 `SKIP` without root, so 69 checks on this
machine. The pass/skip split above is otherwise unchanged.)

* 12 `SKIP` rows are the documented opt-ins/root-only rows: `vol.unit`, `vol.grab`,
  `vol.hwdb` (3, only when the hhd volume patch is used instead of the standalone
  daemon), `vol.holder` (1, needs root to read other users' `/proc/*/fdinfo`),
  `pwr.adaptive` (1), `gfx.xmx.trace` (1), `sched.*` (3), and `life.*` (3, only when
  the daemons are installed; `life.sim` additionally needs `--full`). `vol.topology`
  is the one volume row that `PASS`es without root on this host.
* Without the Decky plugin, `decky.dir`/`decky.backend` also `SKIP`: 45 pass / 10 skip.
* `--full` on the same machine: `76 checks` (adds `vol.hold`, `led.write`,
  `sleep.measure`, `sleep.suspend`; `life.sim` then runs its offline suite instead of
  being gated; `sleep.suspend` stays `SKIP` until `OXP3_ALLOW_SUSPEND=1` is also set).
  Run it on battery; `sleep.measure` is `SKIP` on AC by design.
* `--full --with-changes`: **78 checks** — adds two rows over `--full`, namely
  `aud.activate` and `fan.savemode`, the only checks that may change persistent state.
  Adding `OXP3_ALLOW_SUSPEND=1` changes no count: it turns `sleep.suspend` from `SKIP`
  into a real 60 s suspend.

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
| `sleep.stats` "no suspend attempt since boot" | accurate: nothing to prove yet | `sudo OXP3_ALLOW_SUSPEND=1 tools/oxp3-verify.sh --full --with-changes`, or `sudo tools/sleep-power-measure.sh suspend -w 60`, or the shortest proof: `sudo systemctl suspend` and re-run |
| `sleep.trigger` "trigger policy not installed" | the logind drop-in and the polkit rule are not on this host | `sudo tools/oxp3-hw-install.sh --sleep`, then `sudo systemctl restart systemd-logind` (or reboot), then re-run |
| `sleep.suspend` "gated" | by design: this is the only check that suspends the machine | `sudo OXP3_ALLOW_SUSPEND=1 tools/oxp3-verify.sh --full --with-changes` |
| `sleep.measure` "measurement invalid" | on AC, or the battery is unreadable; the tool refuses to fake a number | unplug, re-run |
| `bt.dracut` | no standalone dracut drop-in; a layered firmware rpm may cover it | `bt.journal`/`bt.controller` carry the verdict |
| `aud.eq` "EQ not installed" | the drop-in is missing | `tools/audio/eq-on.sh` |
| `aud.dummy` `UNKNOWN` | only `auto_null` is visible: this shell has no audio seat, or the SOF card is wedged | `pactl list sinks`; `journalctl --user -u pipewire -n 50`; the trap in [hw/README.md](hw/README.md) |
| `gfx.xmx.trace` | no igdext trace for the current game launch (expected unless tracing is enabled) | enable tracing for one launch, re-run |
| `sched.*` | the gaming scheduler is opt-in and not installed/loaded | `sudo systemctl enable --now oxp3-gaming-sched` |
| `pwr.adaptive` | adaptive TDP is opt-in and not installed | `sudo tools/oxp3-power-install.sh install --adaptive-tdp` |
| `pwr.profile` | no `/etc/oxp3/power.json`, so no profile is asserted | `sudo /usr/local/libexec/oxp3-powerctl profile balanced --save` |
| `decky.*` | the Decky plugin was not installed (the `decky.status` row still passes) | `cp -r tools/decky-oxp3-status ~/homebrew/plugins/oxp3-status` |
| `hid.node` | no 1a86:fe00 hidraw node without an input child: the vendor device (or the udev rule/install) is not present | `tools/oxp3-led.py status`; `od -An -tx1 /sys/class/hidraw/hidraw*/device/report_descriptor` and look for `06 00 ff` |
| `rgbd.timing` | the vendor node is absent, or the hhd journal is not readable so the daemon would poll `hhdctl` instead of following frames | `journalctl -u hhd -n 5`; `tools/oxp3-rgbd.py --config tools/oxp3-rgbd.toml.example --check` |
| `imu.iio` | no IIO device matches 10EC5280 / `accel-display`, so only the `i2c-*` fallback could match | `tools/oxp3-imu.py -j info`; `cat /sys/bus/iio/devices/iio:device*/name` |
| `pwr.ac` | there is no AC/Mains node, so `on_ac` is unknown and both battery clamps are skipped (the deferred policy risk) | this row FAILs if the policy ever allows performance on battery in that state |
| `fan.curvedup` | no oxp_ec fan node or no temperature sensor to evaluate the curve against | `tools/oxp3-powerctl.py fan status` |
| `fan.savemode` | not root, no fan node, or not `--with-changes` (it writes `pwm1_enable` and `/etc/oxp3/power.json`) | `sudo tools/oxp3-verify.sh --full --with-changes` |

## 7. Recorded result on this host (2026-11-09, stock/partial install)

Default mode, no root, in an SSH/desktop session on `anatase-oxp3`, kernel
`7.2.7-an28.fc45.x86_64`:

```
== summary: 67 checks: 39 pass, 3 fail, 25 skip, 0 unknown -> exit 1
   failing: bt.journal bt.controller bt.diag
```

(Recorded before `vol.topology`/`vol.holder`; the current default is 69
checks, adding one `PASS` and one `SKIP` here.)

The exact pass/skip split moves with what is installed at the moment (optional pieces
were being installed while this run was taken); the three failures and the ten new
rows' outcomes are stable.

The ten deferred-risk rows measured on this host: `hid.node`, `led.sides`,
`rgbd.timing`, `imu.iio`, `pwr.ac` (`on_ac=true`), `pwr.order` (both domains are a
genuinely mixed raise/lower switch), `fan.curvedup` (duplicate 60 C point accepted),
`bat.limit` and `inst.paths` **PASS**; `fan.savemode` **SKIP** (needs root and
`--with-changes`).

This is the honest state of this machine, not a harness problem:

* the three Bluetooth failures are the known boot-time gap: both firmware files are in
  `/usr/lib/firmware/updates/intel` (`bt.files` PASS, `bt.diag` sees `files ok=2/2`),
  but this boot's initramfs predates them, so the kernel logged
  `Failed to load Intel firmware file … (-2)`, `hci0` exists without firmware and
  `btmgmt` lists 0 controllers. That is exactly what `--check` reports with exit 1
  (see [QA.md](QA.md) row 47); the fix is the initramfs step + reboot
  (`sudo tools/oxp3-bt-firmware.sh`), which this repository does not run automatically.
* 22 `SKIP`: the hardware fixes/units that are not installed on this stock host (volume,
  LEDs, home button, RGB keeper, sleep units, adaptive TDP, Decky plugin, no saved power
  profile), plus the documented opt-ins (trace, gaming scheduler, EQ drop-in).
* 1 `UNKNOWN`: `aud.dummy` — this shell sees only `auto_null`, so "no audio seat here"
  and "SOF card wedged" cannot be distinguished; the hint lists the two commands.
* 39 `PASS`: IMU and its IIO selection, s2idle/mem_sleep, suspend audit, power
  zones/PL1/fan/RAPL/ordering, adaptive AC detection, battery limit/behaviour/default,
  audio server/model/scripts, display HDR/ICC/EDID/vibrance, Mesa/XMX/xe/working-build,
  HID node/empty-sides, rgbd timing, install-common helpers, status overview.
* `--full` here (non-root, 1 s measurement) = 76 checks: adds `vol.hold` SKIP
  (`needs root`), `led.write` SKIP (hidraw 0600), `sleep.measure` SKIP (on AC),
  `sleep.suspend` SKIP (gated: `OXP3_ALLOW_SUSPEND` unset); `life.sim` runs the
  offline suite and PASSes.
  Still exit 1 for the same three Bluetooth rows.
  `--full --with-changes` = 78 checks and additionally SKIPs `fan.savemode` (`needs
  root`); `aud.activate` was attempted as the documented opt-in.
  The root-only paths (`vol.hold`, `sleep.suspend`) were **not** executed here: this
  repository's rules forbid `sudo`, so they are device-side acceptance steps.

Verbatim evidence (never overwritten, regenerate when the machine changes):

* [`docs/state/verify-default-2026-11-09.txt`](state/verify-default-2026-11-09.txt) — 108 lines, the run above.
* [`docs/state/verify-default-2026-11-09.json`](state/verify-default-2026-11-09.json) — `--json`, 55 checks (pre-deferred-risk inventory), schema 1, exit 1.
* [`docs/state/verify-selftest-2026-11-09.txt`](state/verify-selftest-2026-11-09.txt) — `selftest: 52 checks passed, 0 failed` (before the deferred-risk fixtures; the same command now prints 157).

The snapshots above predate the three `life.*` rows: a host without the daemons now
reports 72 checks in the default mode and adds the opt-in rows with `--full`/
`--with-changes`; the three lifecycle rows are `SKIP` without the daemons (`life.sim` is gated
until `--full`).

## 8. Offline validation performed (no root, no host change)

* `bash -n tools/oxp3-verify.sh` and `sh -n tests/verify/run.sh`: clean.
* `tools/oxp3-verify.sh --selftest`: **157 assertions / 0 failures** over the parsers
  (`display-mode` ANSI output, `powerctl` domains/fan/profile, `status` indicators,
  `suspend_stats`, `bt --check`, `led status`, `rgbd --check`, `pactl` sinks,
  the two journals, `sleep-audit`), the ten deferred-risk probes -- HID candidate map
  and verdicts, dry-run frame counts, adaptive decision/AC verdict, per-domain limit
  ordering, curve summary, battery-limit dry run, install-common helper output, IIO
  primary/fallback map, rgbd timing -- the exit-code mapping, and a fixed inventory
  check that the 78 declared check ids are exactly the documented set (no typos, no
  drops). New fixtures live under `tests/verify/fixtures/` (`hidraw/`, `iio/`,
  `adaptive-*.json`, `hid-verdict-*.txt`, `led-dry-*.txt`, `battery-limit-dry.json`,
  `powerctl-profile-*.json`, `curve-dup.json`, `rgbd-check-nojournal.txt`,
  `inst-paths*.txt`, `logind-cat-config-{default,oxp3}.txt`, `inhibitors-{plasma,sleep-block}.txt`,
  `polkit-rule-bad.rules`), including the SKIP paths.
* `tools/oxp3-verify.sh`: 64 checks, exit 1, ~4 s wall clock. Two consecutive
  `--json` runs were identical apart from `generated_at` (volatile values such as fan
  rpm can legitimately differ between runs).
* `tools/oxp3-verify.sh --json | python3 -m json.tool`: valid; `counts`, `exit_code`,
  lowercase states, unique ids and a non-empty `remediation` for every non-pass row.
* `tools/oxp3-verify.sh --with-changes` without `--full`: exit 3 with the reason.
* `tools/oxp3-verify.sh --help`: renders the whole header.
* `--full` (non-root): 68 checks, exit 1; the four functional checks degrade to `SKIP`
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
* `--with-changes` has exactly two effects: speaker EQ activation (`aud.activate`) and
  the saved fan-mode write (`fan.savemode`, which snapshots and restores
  `/etc/oxp3/power.json` and `pwm1_enable`). If a fix ever needs a persistent install
  step in the harness, add it there, not in `--full`.
* **The `voltap` release-all tail is still a manual step.** Verifying that a real
  release from a freshly re-grabbed device is not dropped needs an HHD restart and one
  physical rocker press while the daemon is re-scanning; synthesising that would race
  the daemon, so no check was added. Command and expected output are in
  [UNIT-TESTS.md §5](UNIT-TESTS.md#5-deferred-risks-device-validated-or-still-deferred).
* `fan.savemode` cannot be exercised in the default or plain `--full` run by design: it
  writes a live fan mode and a persistent state file. It restores both, and FAILs
  loudly (with the exact `tee` command) if a restore did not take.
* The deferred-risk rows are pinned to today's *documented* behaviour: for
  `bat.limit` (no argument = 100 %), `fan.curvedup` (duplicate points: first slope
  wins) and `fan.savemode` (mode=manual) the docs and the check must change together,
  otherwise the row FAILs. That is intentional -- it is how a behaviour change becomes
  visible on real hardware.
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
