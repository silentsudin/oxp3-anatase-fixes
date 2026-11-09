# ONEXPLAYER 3 volume rocker: stuck keys on Linux

Status: root cause localised; the userspace daemon now discovers the correct
topology around hhd (virtual volume keyboard first, physical AT keyboard only
as a free fallback) and degrades observably when every grab is refused. One
hardware measurement still open (needs root, see [Open points](#open-points)).
Owner: silentsudin. Host: Anatase 20261026.3, kernel 7.2.7-an28, hhd 4.1.12.

## Symptom

One tap on the volume rocker sometimes runs the mixer to 100 % (or 0 %), and
holding the rocker keeps ramping after the finger is gone. Windows, same
hardware, same presses: one step per tap. The repo's earlier mitigation
(`files/84-oxp3-volume-keys.hwdb` + `patches/hhd/0001,0002`) is installed on
this unit and the rocker still misbehaves, which is what this document
investigates.

## Devices involved (IDs measured on this unit)

| node | id | name | phys | why it matters |
|---|---|---|---|---|
| `event4` | bus 11, `0001:0001` | `AT Translated Set 2 keyboard` | `isa0060/serio0/input0` | the EC's keyboard interface; **carries the rocker** (`KEY=…` word1 has bits 114/115), has `EV_REP`; role `at-kbd`, grabbed by hhd, our fallback only when free |
| `event23` | bus 3, `0001:0001` | `Handheld Daemon Volume Keyboard` | `phys-hhd-vbtn` | hhd's uinput device; only keys 114/115, no `EV_REP`; **the only place the desktop sees the rocker while hhd runs**; role `hhd-virtual`, our preferred grab |
| `event5` | bus 3, `1997:2433` | `  mini keyboard` | `usb-0000:00:14.0-4/input0` | separate USB HID keyboard, also has 114/115 + `EV_REP`; not the rocker (fails the tests below) |
| `event11` | bus 3, `1a86:fe00` | `HID 1a86:fe00 Consumer Control` | `usb-0000:00:14.0-5/input1` | gamepad's consumer interface, 114/115, **no `EV_REP`**; not grabbed by hhd, not the rocker |
| `event9` | `1a86:fe00` | `HID 1a86:fe00` | `usb…-5:1.0` | hhd grabs this one with a `KEY_O` capability filter (turbo button), not volume |

```
physical rocker
  └─ EC ── PS/2 (i8042 serio0) ── atkbd ── /dev/input/event4  (EV_KEY 114/115, EV_REP on)
        └─ hhd: OxpAtKbd(vid=0x0001, pid=0x0001, grab=True)   ← EVIOCGRAB
             └─ hhd multiplexer ── UInputDevice("Handheld Daemon Volume Keyboard")
                  └─ /dev/input/event23  →  GNOME / gsd-media-keys / Steam
```

hhd is the only reader of `event4` while it runs (`grab=True`), so the desktop
never sees the AT keyboard's own volume events. That is why a fix that only
works "when hhd is running and patched" is fragile: three hhd restarts are in
this boot's journal, and `controller_mode.mode = disabled` stops the grab
altogether. The daemon therefore treats the two nodes as two roles of the same
physical key: `event23` when it exists (normal) and `event4` only as a free
fallback (hhd stopped/disabled). It never assumes either `eventN` number; see
[Topology](#topology-which-device-voltap-grabs).

## Evidence

Measured (read-only commands, this boot):

1. **The rocker is on the AT keyboard, not on a USB HID device.**
   hhd's own OXP support says so: `BTN_MAPPINGS` in `hhd/device/oxp/const.py`
   comments *"Volume buttons come from the same keyboard"* and maps
   `KEY_VOLUMEUP/KEY_VOLUMEDOWN`; the AT keyboard is opened with
   `KBD_VID = 0x0001 / KBD_PID = 0x0001` and `grab=True`. The vendor HID report
   (`hid_v2.py`, shared with the OXP X2) has no volume bits at all, and
   `/usr/lib/python3.15/site-packages/hhd/device/oxp/base.py:463` states
   *"X2 keeps volume keys on the AT keyboard"*. `journalctl -u hhd.service`
   confirms the OXP3 profile: `onexplayer@'ONEXPLAYER 3'`.
2. **atkbd's default keymap maps the standard media scancodes to volume**
   (kernel source, `drivers/input/keyboard/atkbd.c`, `atkbd_set2_keycode[]`):
   index `0xa1 → 114 (KEY_VOLUMEDOWN)`, `0xb2 → 115 (KEY_VOLUMEUP)`, while
   `0xae → 127` and `0xb0 → 159` — **not volume**. So the repo's hwdb pair
   `KEYBOARD_KEY_ae/b0` (the generic Dell/HP/Samsung pair from the kernel's
   `atkbd_volume_forced_release_keys[]`) only has an effect if the EC happens to
   send `0xae/0xb0`.
3. **The hwdb *is* applied** — `cat /sys/class/input/input4/device/force_release`
   returns `174,176,369-370` = `0xae, 0xb0` (plus `0x171/0x172`, which the
   kernel sets itself for HANGEUL/HANJA). `systemd-hwdb query
   'evdev:atkbd:dmi:bvn*:bvr*:bd*:svnONE-NETBOOK:pnONEXPLAYER3:*'` returns the
   entry, and `DMI` matches (`svn=ONE-NETBOOK`, `pn=ONEXPLAYER 3`).
   So the earlier fix is installed — it just may target the wrong scancodes.
4. **The AT keyboard advertises `EV_REP`** (`EV=0x120013`), i.e. the input core
   soft-repeats any key that never gets a release: 250 ms delay, 33 ms period.
   `softrepeat=0`, `softraw=1`, `set=2`, `err_count=0` (no unknown scancodes,
   no `dmesg` "Unknown key" warnings).
5. **force-release in atkbd is scancode-keyed** (`atkbd_event_work()`:
   `if (value && test_bit(code, atkbd->force_release_mask)) { …
   input_report_key(dev, keycode, 0); input_sync(dev); }`) — it emits press and
   release in the *same* read batch. That is why hhd's unpatched key handling
   loses the press, and why `patches/hhd/0001` had to synthesise taps.
6. **hhd drops repeats but never invents a release**
   (`hhd/controller/physical/evdev.py:451`: `elif e.value == 0 or e.value == 1`
   — `value == 2` is discarded). Every key it forwards is forwarded as-is, so a
   missing release stays missing all the way to the client.
7. **The vendor app does not handle volume at all.** `OneXConsole_0.10.4-fix1.exe`
   is an NSIS wrapper around `app-64.7z` → `resources/app.asar` (Electron 30,
   main `background.js`, 7.4 MB). Searching the main bundle and every renderer
   chunk for `VK_VOLUME`, `volumeup`, `volumeDown`, `VOLUME_UP`, `usagePage`,
   `0xFF00`, `reportId`, `hid_write` gives **zero** hits; the only "volume"
   strings are HTML5 `<video>`/`Audio` properties and UI labels. Its native
   addons (`fswin.node` x86/x64/arm64) import SetupAPI + `CreateFileW`/
   `DeviceIoControl` (device enumeration / raw I/O), and the global hook package
   `uiohook-napi` is only linked, never wired to volume keys.
   → On Windows the rocker is consumed by the OS keyboard/HID class stack and
   the shell mixer; no vendor userspace is in the path. The defect is on the
   Linux side of the EC/host boundary, not in the EC alone.
8. **Ghidra (isolated `tools/ghidra-pod.sh volume …`, headless, no shared
   bridge)** on the x64 native addon (`fswin.node`, 180 kB): 567 functions
   decompiled, all N-API wrappers. The device layer it exposes is *disk*
   enumeration, not HID — `SetupDiGetClassDevsA` + `CreateFileW` +
   `DeviceIoControl(0x2d1080 /* IOCTL_STORAGE_GET_DEVICE_NUMBER */, …, 12 B out)`
   and `DeviceIoControl(0x2d1400 /* IOCTL_STORAGE_QUERY_PROPERTY */, 8 B out)`,
   i.e. what `systeminformation` needs for drive serials. No HID report
   parsing, no consumer/volume usage page, no EC command anywhere in it.
   (Dump: `~/.cache/ghidra-agents/volume/out/decomp.txt`.)

Not measured here (no root in this session; `/dev/input/event*` is
`root:input 0660`, session uid 1000 is not in `input`): the live
press/release timing of the rocker. The command is in
[Validation](#validation) step 1 and decides the last open question.

## Mechanism (decided from the evidence above)

* **Device side:** the EC omits the break (release) scancode for the rocker at
  least sometimes. Its *make* codes are fine — the key arrives as
  `KEY_VOLUMEUP/DOWN` and never as an "unknown scancode" (`err_count=0`).
* **Kernel side:** because `event4` advertises `EV_REP`, the missing release is
  turned into **kernel-synthesised repeats** (`value == 2`) at 33 ms. This is
  the visible runaway: one tap, dozens of volume steps.
* **Not the mechanism:** device-side repeats (no `value==2` can come from
  hardware — that value only exists in the input core), and not a client
  repeat loop (the shim's `event23` has no `EV_REP`).
* **Why the existing fix does not hold:** the force-release only fires for
  `0xae/0xb0`, which are *not* the scancodes atkbd maps to volume by default,
  so on a unit whose EC emits `0xa1/0xb2` nothing is force-released; and the
  tap behaviour lives in a patched hhd module under `/usr`, which
  (a) disappears with any hhd or OSTree update, (b) is inactive during hhd
  restart windows and in `controller_mode.mode = disabled`, where the raw AT
  keyboard feeds the desktop again.

## Topology: which device voltap grabs

`tools/oxp3-voltap` never hardcodes an `eventN`. It reads
`/proc/bus/input/devices` plus `/sys/class/input` and classifies candidates by
name, `phys` and capabilities, then checks `/proc/*/fdinfo` for an existing
`EVIOCGRAB` (`grab: 1` in the fdinfo, the `fd` symlink names the device).

| role | identified by | score | use |
|---|---|---|---|
| `hhd-virtual` | name `Handheld Daemon Volume Keyboard` or phys `hhd-vbtn` | 100 | **preferred** whenever present |
| `at-kbd` | name `AT Translated Set 2 keyboard` or phys `isa0060/serio0` or bus `i8042` | 60 | fallback only when no hhd virtual device is selectable and nothing holds it |
| `volume-only` | volume keys, no `EV_REP`, no REL/ABS (e.g. a consumer control) | 40 | not auto-selected; explicit `--device` only |
| `other` | everything else | 10 | never auto-selected |
| `never` | our own shim (`OXP3 volume tap shim`) or hhd's controller | 0 | never grabbed |

Why this order is mandatory: hhd opens the AT keyboard with `grab=True` and
holds the **only** allowed grab on it. A second `EVIOCGRAB` returns `EBUSY`, so
grabbing the physical keyboard while hhd runs cannot work. hhd's uinput volume
keyboard is the same key pair after hhd's translation, is not grabbed by hhd,
and can be grabbed by us.

### A. hhd is running (the normal topology on this unit)

```
physical rocker -> EC -> i8042/atkbd -> event4  (EVIOCGRAB by hhd)
                                         |
                                         +-> hhd multiplexer -> uinput
                                               "Handheld Daemon Volume Keyboard"
                                               event23, phys-hhd-vbtn
                                                   |
                                                   +-> voltap grabs event23
                                                       -> OXP3 volume tap shim
                                                          -> GNOME / Steam
```

What changes when hhd is in the path:

* `event23` advertises only `EV_KEY` 114/115 and **no `EV_REP`**
  (`EV=0x200003`); hhd discards `value == 2` itself
  (`hhd/controller/physical/evdev.py`: `elif e.value == 0 or e.value == 1`).
  There is therefore normally no repeat pattern to suppress on this device.
* The physical kernel repeat pattern (`value == 2` at 33 ms on `event4`) never
  reaches us; hhd consumes it. If a future hhd forwards `value == 2`, the
  `Tapper` drops it exactly as it does on the physical device, so tap
  suppression is unchanged. `--diagnose` reports repeats separately for both
  devices so this can be re-measured after an hhd update.
* The hwdb force-release makes atkbd emit press+release in the same batch; hhd
  then forwards only the release (its press was collapsed), which the `Tapper`
  turns into one tap (`orphan_tap`). That is why the hhd source patch is not
  required for the rocker to work.

### B. hhd is not running, restarted, or `controller_mode.mode = disabled`

hhd's uinput device disappears with the process (and is not created at all in
disabled mode), so nothing grabs the AT keyboard. `voltap` then selects
`event4` as the physical fallback, grabs it, and proxies **all** keys through
its shim (not just volume), advertising `EV_REP` there so typing keeps its
key repeat. This is a full keyboard proxy by design; the alternative would be
losing the built-in keyboard.

When hhd restarts, `event23` disappears and reappears: the daemon logs
`device lost:`, releases any held keys, and re-grabs the recreated device on the
next rescan (`reconnect #N:`).

### C. Every grab is refused

If all ordered candidates are held (`EBUSY`) or unreadable, the daemon does not
exit silently and does not spin: it logs one `DEGRADED:` line, writes the JSON
status file (`/run/oxp3-voltap/status.json`, `state=degraded-busy`,
`exit_code=3`), and retries every `VOLTAP_RESCAN_S` seconds. `--once` returns
the documented exit code immediately; `--degrade-exit-after N` makes the daemon
exit with it after N seconds (leave it at 0 in the unit so systemd does not
restart-loop).

## Fix

Three layers, all ours, no vendor code, no kernel module, no `/usr` patching:

1. **Kernel: force-release the volume scancodes** —
   `tools/oxp3-voltap/84-oxp3-volume-keys-taps.hwdb` keeps the old
   `0xae/0xb0` pair and adds `0xa1/0xb2` (the pair atkbd actually maps to
   volume). Harmless if the EC uses only one of them. This is the primitive that
   makes a second press possible at all, because hhd cannot emit a press for a
   key that is still logically down. **This layer stays even with the daemon**:
   it is defence in depth and the only thing that recovers the second press
   during a `voltap` restart or grab-conflict window.
2. **Discovery: pick the only grabbable copy of the rocker** — prefer hhd's
   virtual volume keyboard, fall back to a *free* physical AT keyboard, and
   record every candidate's role, score, holder and decision. `check`,
   `explain`/`--probe`, `diagnose` and `status` expose that decision without
   root for the static parts.
3. **Userspace: `tools/oxp3-voltap`** — a python3-evdev daemon that grabs the
   chosen device (`EVIOCGRAB`), drops every `value == 2` for volume keys,
   drops duplicate presses and contact bounce, emits exactly one 30 ms down/up
   pair per press through its own uinput device, force-releases anything that
   somehow stays down, turns a *release without a press* into one tap,
   re-attaches when hhd recreates its device, releases all keys on SIGTERM, and
   reports a documented degraded state when it cannot grab. The daemon source
   is `tools/oxp3-voltap/oxp3-voltap`; the hwdb file is separate.

### Exit codes and status

| code | meaning |
|---|---|
| 0 | grabbed (or `check`: a candidate would be grabbed) |
| 1 | usage error / missing root for `run`/`probe`/`diagnose`/`selftest` |
| 2 | no volume event device candidate (`degraded-empty`) |
| 3 | every candidate refused (`degraded-busy` / `degraded-error`) |

`oxp3-voltap status` prints the daemon's last decision, holders, state and
counter snapshot from the JSON file; `oxp3-voltap check --kv` prints the same
choice as stable `KEY=value` lines for the verification harness.

### Expected journal lines

Topology A — hhd running (the normal case):

```
oxp3-voltap: grabbed /dev/input/event23 ('Handheld Daemon Volume Keyboard' phys='phys-hhd-vbtn' role=hhd-virtual id=0001:0001)
oxp3-voltap: created 'OXP3 volume tap shim' at /dev/input/eventNN with 2 key codes
```

Topology B — physical fallback (hhd stopped or controller mode disabled):

```
oxp3-voltap: grabbed /dev/input/event4 ('AT Translated Set 2 keyboard' phys='isa0060/serio0/input0' role=at-kbd id=0001:0001)
oxp3-voltap: created 'OXP3 volume tap shim' at /dev/input/eventNN with <N> key codes and EV_REP (physical keyboard fallback)
```

hhd restart / device recreation:

```
oxp3-voltap: device lost: /dev/input/event23 disappeared; releasing its keys and dropping it; retrying in 1s
oxp3-voltap: reconnect #2: grabbed /dev/input/event23 ('Handheld Daemon Volume Keyboard' phys='phys-hhd-vbtn' role=hhd-virtual id=0001:0001)
```

Grab refused (documented degraded state, not silence):

```
oxp3-voltap: DEGRADED: every candidate is held or refused (EBUSY): /dev/input/event4: [Errno 16] Device or resource busy; status=degraded-busy exit=3; retrying in 1s
```

No candidate at all:

```
oxp3-voltap: DEGRADED: no volume event device candidate (status=degraded-empty exit=2); retrying in 1s
```

The verification harness `vol.grab` check matches the stable prefix
`grabbed /dev/input/event`; both topologies therefore pass it.

## Install (root; these are the exact commands)

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
sudo tools/oxp3-voltap/install.sh install --hwdb
# installs /usr/local/libexec/oxp3-voltap, /etc/oxp3-voltap.conf,
# /etc/systemd/system/oxp3-voltap.service,
# /etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb; enables + starts the unit
```

Equivalent manual form:

```bash
sudo install -d -m 0755 /usr/local/libexec
sudo install -m 0755 tools/oxp3-voltap/oxp3-voltap /usr/local/libexec/
sudo install -m 0644 tools/oxp3-voltap/oxp3-voltap.conf /etc/
sudo install -m 0644 tools/oxp3-voltap/oxp3-voltap.service /etc/systemd/system/
sudo install -m 0644 tools/oxp3-voltap/84-oxp3-volume-keys-taps.hwdb /etc/udev/hwdb.d/
sudo systemd-hwdb update
sudo udevadm trigger --subsystem-match=input --action=change
sudo systemctl daemon-reload
sudo systemctl enable --now oxp3-voltap.service
```

Optional: once the daemon works, the hhd source patch can go (the hwdb must
stay — it is what delivers the second and later presses).  There is no
"uninstall-extras" subcommand; the two patches are reverted directly, newest
first, then hhd is restarted (its `__pycache__` must go too):

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
sudo git -C /usr/lib/python3.15/site-packages apply -R -p1 < patches/hhd/0002-oxp-volume-rocker-taps-in-controller-mode.patch
sudo git -C /usr/lib/python3.15/site-packages apply -R -p1 < patches/hhd/0001-oxp-volume-rocker-taps.patch
sudo find /usr/lib/python3.15/site-packages/hhd/device/oxp -name __pycache__ -prune -exec rm -rf {} +
sudo systemctl restart hhd.service
```

If you prefer the kernel-module form over hwdb (applies to every atkbd, no
per-device match, needs a reboot):

```bash
echo 'options atkbd forced_release_keys=0xa1,0xb2,0xae,0xb0' | sudo tee /etc/modprobe.d/oxp3-atkbd.conf
sudo systemctl reboot
```

## Validation

```bash
# 1. Which device would be grabbed, why, and who holds the physical one.
#    Read-only; no root needed (holder lines are complete only as root).
tools/oxp3-voltap/oxp3-voltap check
tools/oxp3-voltap/oxp3-voltap explain          # same with per-candidate detail
tools/oxp3-voltap/oxp3-voltap check --kv       # stable lines for scripts
#    expected with hhd active: TOPO=hhd-virtual, SEL=/dev/input/event23,
#    HHD_RUNNING=1, ATKBD_HELD=6752 (as root), PREDICTED_EXIT=0.
#    With hhd stopped: TOPO=physical, SEL=/dev/input/event4.

# 2. Watch both streams while pressing the rocker, then read the verdict.
#    Read-only (does not grab); root needed to open event4/event23.
sudo tools/oxp3-voltap/oxp3-voltap diagnose --duration 8
#    expected with hhd active: events only on event23, VERDICT grab
#    /dev/input/event23 (hhd-virtual). With hhd stopped: events on event4 and
#    VERDICT grab event4 (physical fallback). It also reports value=2 counts
#    per device, which is how "does the virtual device repeat?" is re-measured.

# 3. Which device really carries the rocker, and what the EC sends.
#    This needs the device free: stop hhd (or set controller_mode.mode =
#    disabled), then read event4; with hhd running, hhd owns event4 and the
#    probe sees nothing. Press the rocker a few times, Ctrl-C.
sudo systemctl stop hhd
sudo /usr/local/libexec/oxp3-voltap probe --device /dev/input/event4 --stuck-ms 400
#    expected with the hwdb active: one press + one release per tap, no value=2,
#    "longest press" a few ms. Without force-release: press but no release
#    ("VERDICT missing release events"), or value=2 ("kernel/client repeat").
sudo systemctl start hhd

# 4. uinput plumbing only (creates the shim, emits two taps).
sudo /usr/local/libexec/oxp3-voltap selftest
#    the mixer must move exactly two steps, and the journal must show the device.

# 5. End to end.
systemctl status oxp3-voltap
journalctl -u oxp3-voltap -b --no-pager
#    expect one of the two grabbed lines above, then: 3 quick taps -> 3 steps;
#    hold 2 s -> one step per press, no runaway.
#    Then exercise both fallbacks:
#      sudo systemctl stop hhd      -> expect "device lost:", then a
#                                      "reconnect #N: grabbed /dev/input/event4"
#                                      (physical fallback, keyboard keeps typing)
#      sudo systemctl start hhd     -> expect "device lost:", then a reconnect
#                                      to the recreated event23 within ~1 s
#      sudo systemctl stop hhd; systemctl stop oxp3-voltap; start a second
#      check --once as root while event4 is held by something else -> exit 3
#      and the DEGRADED line, never silence.

# 6. Counters/status (set VOLTAP_STATS=10 in /etc/oxp3-voltap.conf, restart):
#    press/repeat/duplicate/bounce/orphan_tap/late_release tell you which
#    mechanism was actually live on your unit. `oxp3-voltap status` prints the
#    last decision without root.
```

## Integration test: the daemon against a virtual device (proven end-to-end)

`tests/integration/volume/` is the first check that drives the **real daemon
process** with real kernel input events. It creates uinput devices, starts
`tools/oxp3-voltap` against them, injects the event sequences a rocker produces
and reads the result from the daemon's own uinput shim (`OXP3 volume tap shim`,
`phys-oxp3-voltap`) - never from the device the daemon grabbed.

```bash
# direct (uinput access required; SKIPs with instructions otherwise)
tests/integration/volume/run.sh

# through the repository runner (opt-in; skipped in the default offline run)
tools/repo-test.sh --integration
OXP3_REPO_TEST_INTEGRATION=1 tools/repo-test.sh --only voltap-integration
```

It needs write access to `/dev/uinput` and read access to the event nodes it
creates. Root is the simple case; as a user it works when `/dev/uinput` has a
uaccess ACL (Steam's udev rule grants one) and the created devices are
joystick-tagged, which the test arranges with `BTN_TRIGGER`/`BTN_A` so logind
also ACLs their event nodes. Without either it exits 77 with:

```
SKIP: no write access to /dev/uinput (...); run the harness as root:
sudo tests/integration/volume/run.sh  (or grant your user write access to
/dev/uinput with a udev rule/ACL)
```

The same applies to reading the created event nodes; there the exact
alternative is `sudo usermod -aG input "$USER"` (then log out and in).

Observed on Anatase (2026-11-09, kernel 7.2.7-an28; 37 assertions, ~18 s):

| driven sequence | downstream result |
|---|---|
| one tap | exactly `KEY_VOLUMEUP` down + up |
| three quick taps | exactly three down/up pairs (6 events, no extras) |
| 3 s hold with 76 `value==2` kernel-style repeats | exactly 2 events total; unchanged at 0.5 s, 1.5 s and after release (no ramp) |
| rapid bounce inside the 60 ms gap | bounced press/release disappear; a tap after the gap still works |
| duplicate/repeat/repeated-release storm | exactly one pair (the input core coalesces duplicate presses and up-releases before the daemon sees them) |
| release with the key already down, daemon attached afterwards (`orphan_tap`) | exactly one pair; counters `orphan_tap=1` |
| release arriving after the synthetic timeout (`late_release`) | dropped; no second tap; counters `late_release=1` |
| HHD-shaped device present (sandbox name, our `1209:7657` id) | grabbed with `role=hhd-virtual`, taps forwarded |
| preferred device removed | `device lost:`, fallback grabs the AT-shaped device `role=at-kbd`, shim recreated with `EV_REP`, taps forwarded |
| the test itself holds `EVIOCGRAB` first (complete holder scan) | `DEGRADED: every candidate is held or refused (EBUSY)`, status `degraded-busy`, exit 3 |

Hermeticity: every node is created by the test (or by the daemon under test)
and checked by name + `phys` + `vendor:product`; the automatic-topology phase
passes synthetic `--proc-root`/`--sys-root` trees that list only the sandbox
devices, so the host's real `event4`/`event23` are invisible to discovery. The
HHD-shaped stand-in reuses hhd's *name string* only, with our own vendor/product
and a `-integration-test` `phys`; the real hhd device is never opened. Teardown
SIGTERMs the daemon, closes every uinput device, verifies the nodes and the
shim are gone, and asserts no test process survived.

Two daemon fixes came out of this test:

1. `run` no longer refuses categorically when euid != 0. With an input-group
   membership or a logind uaccess ACL the daemon can read its device and write
   `/dev/uinput`; the real refusal (EACCES/EBUSY) is now reported from the
   open/grab path as the documented degraded state, which is both testable and
   more informative. The system unit still runs as root.
2. Automatic discovery with a *complete* holder scan used to report
   `degraded-empty`/exit 2 when every candidate was visibly held, because the
   attempt order was empty - contradicting the documented
   `degraded-busy`/exit 3. `GrabFSM.refused_held()` now produces the busy state
   from the holder list (unit-tested, and exercised by the last row above).

## Risks and rollback

* **Risk: volume keys stop completely** (e.g. hhd disabled, no virtual device,
  physical device unreadable). The daemon logs `DEGRADED:` with exit code 2/3
  and keeps retrying; the desktop then gets the raw rocker again (the bug, not
  a loss of function).
* **Risk: physical fallback steals the keyboard.** Grabbing the AT keyboard is
  safe only because we proxy every key through the shim (with `EV_REP`). If the
  shim cannot be created the daemon refuses to keep the grab and retries; if
  you prefer never to proxy, set `VOLTAP_ALLOW_PHYSICAL=0`.
* **Risk: hhd changes its virtual device name/phys.** Discovery fails to
  classify it; `check` then reports `physical` or `none`. Override with
  `VOLTAP_HHD_NAME`/`VOLTAP_HHD_PHYS` or `VOLTAP_DEVICES` (no code change).
* **Risk: hwdb `!` removes the "hold to ramp".** That is the intent (one step
  per press). Revert `84-oxp3-volume-keys-taps.hwdb` if you prefer ramping.
* **Risk: hard dependency on python3-evdev** (already pulled in by hhd;
  `rpm -q python3-evdev`).
* **Rollback, daemon only:** `sudo tools/oxp3-voltap/install.sh uninstall`
  (stops and disables the unit, removes binary/unit/hwdb, keeps
  `/etc/oxp3-voltap.conf`).
* **Rollback, everything:** the commands above plus
  `sudo rm /etc/oxp3-voltap.conf`; the hhd patches are reverted by
  `./oxp3-fixes.sh uninstall`.
* **No host state is touched by this repo copy:** installing is a root action
  documented here, never executed by the build scripts.

## Open points

1. **Exact scancode pair** the EC sends (`0xa1/0xb2` vs `0xae/0xb0` vs
   `0xe0`-prefixed). Decided by validation step 3; both pairs are
   force-released meanwhile, so the fix does not depend on the answer.
2. **Whether Windows ramps on hold** identically. The vendor app is out of the
   path (evidence 7), so the comparison is OS-vs-OS; not measured.
3. **hhd upstream**: `hhd-dev/hhd#342` describes the same lost release. If hhd
   ever emits a proper pair itself, the shim becomes redundant and can be
   stopped without changing anything else. If hhd ever starts grabbing its own
   uinput device, `voltap` will see it in `/proc/*/fdinfo` and report
   `degraded-busy` instead of failing silently.
4. **Physical-fallback repeat proxying** is now exercised end to end by
   `tests/integration/volume/` against an AT-shaped virtual device: the daemon
   grabs it after the preferred device disappears, recreates the shim with
   `EV_REP`, and taps still produce one pair. It is still not measured on the
   real `event4` with a real keyboard (needs root and hhd stopped); validation
   step 5 remains the on-hardware check.
5. **Diagnose verdict** is based on a single N-second window with no presses
   unless the operator presses during it; it deliberately falls back to the
   static preference instead of guessing.
