# ONEXPLAYER 3 volume rocker: stuck keys on Linux

Status: root cause localised and the design corrected after a real outage.
The supported fix is an **hhd plugin** (`tools/hhd-oxp3-voltap`) that
normalises taps *inside* hhd and never opens or grabs a device. The standalone
daemon remains as a legacy path: by default it never touches an hhd-owned device
and only takes a free physical AT keyboard, otherwise it stays in observable
observe mode (exit 4). One hardware measurement still open (needs root, see
[Open points](#open-points)).
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
| `event23` | bus 3, `0001:0001` | `Handheld Daemon Volume Keyboard` | `phys-hhd-vbtn` | hhd's uinput device; only keys 114/115, no `EV_REP`; **the only place the desktop sees the rocker while hhd runs**; role `hhd-virtual` - hhd's own device, **never grabbed by us** (a second EVIOCGRAB made hhd drop the controller; see [The outage](#the-outage-never-grab-an-hhd-device)) |
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

## Topology: which device the daemon may grab

`tools/oxp3-voltap` never hardcodes an `eventN`. It reads
`/proc/bus/input/devices` plus `/sys/class/input` and classifies candidates by
name, `phys` and capabilities, then checks `/proc/*/fdinfo` for an existing
`EVIOCGRAB` (`grab: 1` in the fdinfo, the `fd` symlink names the device).

| role | identified by | score | use |
|---|---|---|---|
| `hhd-virtual` | name `Handheld Daemon Volume Keyboard` or phys `hhd-vbtn` | 100 | **never grabbed by default**; last resort only with `--allow-hhd-grab` |
| `at-kbd` | name `AT Translated Set 2 keyboard` or phys `isa0060/serio0` or bus `i8042` | 60 | default: grabbed only when it is free (hhd stopped/disabled) |
| `volume-only` | volume keys, no `EV_REP`, no REL/ABS (e.g. a consumer control) | 40 | not auto-selected; explicit `--device` only |
| `other` | everything else | 10 | never auto-selected |
| `never` | our own shim (`OXP3 volume tap shim`) or hhd's controller | 0 | never grabbed |

### The outage: never grab an hhd device

On 2026-11-09 the previous default (hhd's virtual volume keyboard first) took an
`EVIOCGRAB` on `event23`. hhd's own OXP keyboard producer
(`hhd/device/oxp/base.py`, `OxpAtKbd`, `VID:PID 0001:0001`, no name filter)
matches that uinput device too, so hhd's own `grab()` then failed with
`OSError: [Errno 16] Device or resource busy`. hhd logged
`Assuming controllers disconnected, restarting after 3s`, dropped its virtual
devices and the emulated Steam controller (`28DE:12FF`) disappeared, so
Gamemode had no controller. The immediate remedy was
`sudo systemctl disable --now oxp3-voltap && sudo systemctl restart hhd`.

Two rules follow, and they are not negotiable:

1. **A tool of ours never grabs (or opens) a device hhd owns.** hhd's uinput
   volume keyboard is part of hhd, not a spare copy of the rocker.
2. The normalisation belongs **inside hhd's event flow** - the plugin
   `tools/hhd-oxp3-voltap` - not in a second reader of the same device.

### A. hhd is running (the normal topology on this unit)

```
physical rocker -> EC -> i8042/atkbd -> event4  (EVIOCGRAB by hhd)
                                         |
                                         +-> hhd multiplexer -> OxpAtKbd[volume-tap filter]
                                               (the plugin, in-process)
                                               -> uinput "Handheld Daemon Volume Keyboard"
                                               -> event23 -> GNOME / Steam
```

* The plugin wraps hhd's OXP keyboard producer and normalises the translated
  `key_volumeup`/`key_volumedown` buttons there. It opens no event node, takes
  no `EVIOCGRAB` and creates no uinput device.
* The legacy daemon, if installed, sees `event4` held by hhd (root's holder
  scan) and refuses `event23` by rule: it logs one
  `OBSERVE: hhd owns the volume path ...` line, writes JSON status
  `state=degraded-hhd-protected`, returns exit code **4** with `--once`, and
  attempts no grab at all.
* If hhd does not run, or `controller_mode.mode = disabled`, `event4` is free:
  the daemon grabs it and proxies the whole keyboard through its shim (see B).

### B. hhd is not running, restarted, or `controller_mode.mode = disabled`

hhd's uinput device disappears with the process (and is not created at all in
disabled mode), so nothing grabs the AT keyboard. The daemon then selects
`event4` as the physical fallback, grabs it, and proxies **all** keys through
its shim (not just volume), advertising `EV_REP` there so typing keeps its key
repeat. This is a full keyboard proxy by design; the alternative would be
losing the built-in keyboard.

When hhd restarts, `event4` becomes held again; the daemon drops it, logs the
loss, and stays in observe mode while hhd owns the path.

### C. No device may be taken

* **hhd running (normal):** observe mode: one `OBSERVE:` line, status
  `degraded-hhd-protected`, exit code **4**, no grab. This is the expected
  state of the legacy daemon on this unit.
* **A grab is refused (EBUSY/EACCES):** one `DEGRADED:` line, status
  `degraded-busy`/`degraded-error`, exit code **3**, retry every
  `VOLTAP_RESCAN_S`.
* **No candidate at all:** `degraded-empty`, exit code **2**.

`--once` returns the documented code immediately; `--degrade-exit-after N` makes
the daemon exit with it after N seconds (leave it at 0 in the unit so systemd
does not restart-loop).

## Fix

The supported design has one layer now, with the kernel hwdb as defence in
depth; no vendor code, no kernel module, no `/usr` patching:

1. **hhd plugin (supported): `tools/hhd-oxp3-voltap`.** A distribution
   registered in the `hhd.plugins` entry-point group. At `open()` it
   defensively wraps hhd's OXP keyboard producer
   (`hhd.device.oxp.base.OxpAtKbd.produce`) and runs the tap state machine on
   `key_volumeup`/`key_volumedown` only: one clean tap per physical press,
   minimum hold, bounce gap, duplicate/orphan/late-release handling, bounded
   synthetic release. If the hhd API shape differs it logs and no-ops; if the
   filter raises it logs once, restores hhd's own handling and passes hhd's
   events through. It never opens, reads or grabs an event device.
   API study with file/line references: [`hhd-plugin.md`](hhd-plugin.md).
2. **Kernel: force-release the volume scancodes** -
   `tools/oxp3-voltap/84-oxp3-volume-keys-taps.hwdb` keeps the old
   `0xae/0xb0` pair and adds `0xa1/0xb2` (the pair atkbd actually maps to
   volume). Harmless if the EC uses only one of them. This is what makes a
   second press possible at all; it is installed by both the plugin and the
   daemon component.
3. **Legacy daemon (opt-in, `--volume-daemon`): `tools/oxp3-voltap`.** Same
   state machine in front of a device, but with the corrected topology: never
   hhd's device; the free physical AT keyboard only; otherwise observable
   observe mode (exit 4). It refuses `event23` even when explicitly configured
   unless `--allow-hhd-grab` is given - and that flag exists only for a
   deliberate experiment, never for the supported install.

### Exit codes and status

| code | meaning |
|---|---|
| 0 | grabbed (or `check`: a candidate would be grabbed) |
| 1 | usage error / missing root for `run`/`probe`/`diagnose`/`selftest` |
| 2 | no volume event device candidate (`degraded-empty`) |
| 3 | every candidate was attempted and refused (`degraded-busy` / `degraded-error`) |
| 4 | hhd owns the volume path and grabbing its device is forbidden; nothing opened or grabbed (`degraded-hhd-protected`, observe mode) |

`oxp3-voltap status` prints the daemon's last decision, holders, state and
counter snapshot from the JSON file; `oxp3-voltap check --kv` prints the same
choice as stable `KEY=value` lines (`ALLOW_HHD_GRAB`, `HHD_PROTECTED`,
`OBSERVE`, ...) for the verification harness.

### Expected journal lines

Plugin path (the supported one) - the plugin's own hhd log lines, no
`oxp3-voltap` unit at all:

```
vltp  INFO     oxp3-voltap: installed the volume-tap filter on
               hhd.device.oxp.base.OxpAtKbd.produce (hold=60 ms, gap=60 ms,
               max_hold=500 ms); no input device is opened or grabbed by this plugin.
```

No-op when the hhd shape is unknown:

```
vltp  WARNING  oxp3-voltap: no-op, this HHD build does not expose the OXP
               keyboard producer we know how to hook (...). Volume buttons are
               left exactly as HHD handles them.
```

Legacy daemon, hhd running (the expected state):

```
oxp3-voltap: OBSERVE: hhd owns the volume path; not opening or grabbing its device
             (status=degraded-hhd-protected exit=4). The supported path is the hhd
             plugin (tools/hhd-oxp3-voltap); see docs/DAEMONS.md. Retrying in 1s.
```

Legacy daemon, hhd stopped (physical fallback):

```
oxp3-voltap: grabbed /dev/input/event4 ('AT Translated Set 2 keyboard' phys='isa0060/serio0/input0' role=at-kbd id=0001:0001)
oxp3-voltap: created 'OXP3 volume tap shim' at /dev/input/eventNN with <N> key codes and EV_REP (physical keyboard fallback)
```

Grab refused (documented degraded state, not silence):

```
oxp3-voltap: DEGRADED: every candidate is held or refused (EBUSY): /dev/input/event4: [Errno 16] Device or resource busy; status=degraded-busy exit=3; retrying in 1s
```

The verification harness `vol.grab` check accepts a grab of a non-hhd device
**or** the `OBSERVE: hhd owns the volume path` line; it fails on any grab of
hhd's device.

## Install (root; these are the exact commands)

Supported path - the hhd plugin (`/usr/local` is `/var/usrlocal` on Anatase, so
it survives rpm-ostree updates):

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
sudo tools/hhd-oxp3-voltap/install.sh install
sudo systemctl restart hhd      # imports the plugin; drops 28DE:12FF for a few seconds
```

That installs exactly two directories and nothing else:

```
/usr/local/lib/python3.15/site-packages/oxp3_hhd_voltap/
/usr/local/lib/python3.15/site-packages/oxp3_hhd_voltap-0.1.0.dist-info/
```

Uninstall removes exactly those (`sudo tools/hhd-oxp3-voltap/install.sh uninstall`)
and then needs `sudo systemctl restart hhd` to unload the code from the running
daemon. Through the orchestrator: `sudo tools/oxp3-hw-install.sh --volume`
(plugin, default) and `--uninstall --volume`.

Legacy daemon (only if you deliberately do not want the plugin):

```bash
sudo tools/oxp3-hw-install.sh --volume-daemon
# equivalent: sudo tools/oxp3-voltap/install.sh install --hwdb
```

It installs `/usr/local/libexec/oxp3-voltap`, `/etc/oxp3-voltap.conf`,
`/etc/systemd/system/oxp3-voltap.service` and the hwdb file, then enables and
starts the unit. With hhd running it will sit in observe mode (exit 4): that is
the correct, safe behaviour, not an error.

The hwdb can also be applied as a kernel-module option instead (applies to
every atkbd, no per-device match, needs a reboot):

```bash
echo 'options atkbd forced_release_keys=0xa1,0xb2,0xae,0xb0' | sudo tee /etc/modprobe.d/oxp3-atkbd.conf
sudo systemctl reboot
```

## Validation

```bash
# 1. Supported path: is the plugin installed and is hhd healthy?
tools/hhd-oxp3-voltap/install.sh status     # files + entry point, read-only
tools/hhd-oxp3-voltap/install.sh check      # can this hhd build be hooked?
systemctl status hhd
journalctl -u hhd -b --no-pager | tail -40
#    expected: hhd active; after the restart that loads the plugin, one
#    "oxp3-voltap: installed the volume-tap filter ..." line and no
#    "Device or resource busy" / "Assuming controllers disconnected".
#    Press: 3 quick taps -> 3 mixer steps; hold -> no runaway.

# 2. Read-only invariant checks for both paths (tools/oxp3-verify.sh):
tools/oxp3-verify.sh | grep -E 'hhd\.|vol\.'
#    hhd.unit   hhd active
#    hhd.busy   no EBUSY/disconnect since the last successful init
#    hhd.grab   no process of ours holds an fd or EVIOCGRAB on an hhd device
#    vol.unit / vol.topology / vol.holder / vol.grab   the daemon's decision:
#               with hhd running and the daemon installed it is
#               "observe: hhd owns the volume path" (or the unit is disabled
#               on purpose); it must never show a grab of event23.

# 3. Legacy daemon only: which device would be grabbed, why, who holds event4.
#    Read-only; no root needed (holder lines are complete only as root).
tools/oxp3-voltap/oxp3-voltap check
tools/oxp3-voltap/oxp3-voltap explain          # same with per-candidate detail
tools/oxp3-voltap/oxp3-voltap check --kv       # stable lines for scripts
#    expected with hhd active: TOPO=none, SEL=, HHD_PROTECTED=1, OBSERVE=1,
#    PREDICTED_EXIT=4 (as root: ATKBD_HELD=<hhd pid>). With hhd stopped:
#    TOPO=physical, SEL=/dev/input/event4, PREDICTED_EXIT=0.
#    --allow-hhd-grab changes that to TOPO=hhd-virtual only when asked.

# 4. Legacy daemon: watch the streams (read-only, no grab; root for event4/23).
sudo tools/oxp3-voltap/oxp3-voltap diagnose --duration 8
#    expected with hhd active: events on event23, and the verdict says do NOT
#    grab it; use the plugin. With hhd stopped: events on event4 and the
#    verdict recommends the physical fallback.

# 5. Which device really carries the rocker, and what the EC sends.
#    This needs the device free: stop hhd (or set controller_mode.mode =
#    disabled), then read event4. Press the rocker a few times, Ctrl-C.
sudo systemctl stop hhd
sudo /usr/local/libexec/oxp3-voltap probe --device /dev/input/event4 --stuck-ms 400
#    expected with the hwdb active: one press + one release per tap, no value=2,
#    "longest press" a few ms. Without force-release: press but no release
#    ("VERDICT missing release events"), or value=2 ("kernel/client repeat").
sudo systemctl start hhd        # drops the controller for a few seconds

# 6. Legacy daemon: observe mode (the expected state with hhd running) and the
#    physical fallback.
sudo /usr/local/libexec/oxp3-voltap run --once ; echo "exit=$?"
#    expected with hhd running: OBSERVE line and exit=4, no grab.
journalctl -u oxp3-voltap -b --no-pager
#    with hhd stopped: "grabbed /dev/input/event4 (... role=at-kbd ...)" and
#    "created 'OXP3 volume tap shim' ... EV_REP"; 3 taps -> 3 steps, typing
#    keeps its repeat. Holding a key never ramps. Starting hhd again makes the
#    daemon drop event4 and log OBSERVE.

# 7. Counters/status (legacy daemon; set VOLTAP_STATS=10, restart):
#    press/repeat/duplicate/bounce/orphan_tap/late_release tell you which
#    mechanism was actually live on your unit. `oxp3-voltap status` prints the
#    last decision without root, including observe/protected state.
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
| HHD-shaped + AT-shaped present, AT free | AT-shaped device grabbed `role=at-kbd`; the hhd-shaped device is `refused`, never grabbed |
| outage topology: the test holds `EVIOCGRAB` on the AT-shaped device (hhd owns it) | `OBSERVE: hhd owns the volume path`, status `degraded-hhd-protected`, exit 4, **no grab and no fd** on the hhd-shaped device |
| same, retrying daemon | the OBSERVE line appears once across several rescans |
| `--allow-hhd-grab` while the AT device is held | hhd-shaped device grabbed as the explicit last resort (`decision=last-resort`) |
| AT-shaped device held without an hhd device | `DEGRADED: every candidate is held or refused (EBUSY)`, status `degraded-busy`, exit 3 |

Hermeticity: every node is created by the test (or by the daemon under test)
and checked by name + `phys` + `vendor:product`; the automatic-topology phase
passes synthetic `--proc-root`/`--sys-root` trees that list only the sandbox
devices, so the host's real `event4`/`event23` are invisible to discovery. The
HHD-shaped stand-in reuses hhd's *name string* only, with our own vendor/product
and a `-integration-test` `phys`; the real hhd device is never opened. Teardown
SIGTERMs the daemon, closes every uinput device, verifies the nodes and the
shim are gone, and asserts no test process survived.

Three rules came out of this harness:

1. `run` does not refuse categorically when euid != 0. With an input-group
   membership or a logind uaccess ACL the daemon can read its device and write
   `/dev/uinput`; the real refusal (EACCES/EBUSY) is reported from the
   open/grab path as the documented degraded state.
2. Automatic discovery with a *complete* holder scan used to report
   `degraded-empty`/exit 2 when every candidate was visibly held, because the
   attempt order was empty; `GrabFSM.refused_held()` now produces the
   documented `degraded-busy`/exit 3.
3. **An hhd-owned device is never a default candidate.** The harness now proves
   the outage topology explicitly: hhd-shaped present + AT held means observe
   mode, exit 4, no fd and no grab on hhd's device; only the explicit
   `--allow-hhd-grab` can select it, and then only as `last-resort`.

## Risks and rollback

* **The regression this document exists for:** grabbing hhd's own virtual
  volume keyboard drops the emulated Steam controller. The daemon now refuses
  it by default (exit 4/observe) and `tools/oxp3-verify.sh` fails if any
  process of ours holds an hhd-* event device or if this boot's hhd journal
  shows the EBUSY/disconnect signature without a later successful init.
* **Risk: the plugin no-ops after an hhd update.** That is deliberate: a
  changed `OxpAtKbd` shape logs a warning and leaves hhd's own handling in
  place. Check with `tools/hhd-oxp3-voltap/install.sh check`; reinstall after
  adapting the hook if upstream changes. The controller is never at risk.
* **Risk: the legacy daemon cannot normalise while hhd runs** (observe mode).
  That is intended: use the plugin. The daemon only takes the free AT keyboard,
  and then it proxies every key through its shim (with `EV_REP`); if the shim
  cannot be created it releases the grab and retries. Set
  `VOLTAP_ALLOW_PHYSICAL=0` to forbid the physical fallback entirely.
* **Risk: hhd changes its virtual device name/phys.** Discovery fails to
  classify it; the daemon then reports `physical`/`none`. Override with
  `VOLTAP_HHD_NAME`/`VOLTAP_HHD_PHYS`; never work around it by grabbing hhd's
  device.
* **Risk: hwdb `!` removes "hold to ramp".** That is the intent (one step per
  press). Revert `84-oxp3-volume-keys-taps.hwdb` if you prefer ramping.
* **Risk: hard dependency on python3-evdev** for the daemon (already pulled in
  by hhd; `rpm -q python3-evdev`). The plugin needs no extra dependency.
* **Rollback, plugin (supported):**
  `sudo tools/hhd-oxp3-voltap/install.sh uninstall && sudo systemctl restart hhd`
  removes exactly the package and dist-info directories; nothing else is
  installed. The restart drops the controller for a few seconds.
* **Rollback, legacy daemon:** `sudo tools/oxp3-voltap/install.sh uninstall`
  (stops and disables the unit, removes binary/unit/hwdb, keeps
  `/etc/oxp3-voltap.conf`). `sudo tools/oxp3-hw-install.sh --uninstall --volume`
  removes both paths.
* **Rollback, everything:** the commands above plus
  `sudo rm /etc/oxp3-voltap.conf`. See [`docs/ROLLBACK.md`](../ROLLBACK.md).
* **No host state is touched by this repo copy:** installing is a root action
  documented here, never executed by the build scripts.

## Open points

1. **Exact scancode pair** the EC sends (`0xa1/0xb2` vs `0xae/0xb0` vs
   `0xe0`-prefixed). Decided by validation step 3; both pairs are
   force-released meanwhile, so the fix does not depend on the answer.
2. **Whether Windows ramps on hold** identically. The vendor app is out of the
   path (evidence 7), so the comparison is OS-vs-OS; not measured.
3. **hhd upstream**: `hhd-dev/hhd#342` describes the same lost release. The
   installed hhd carries the in-tree `OxpAtKbd` tap policy (our patches), and
   the plugin replaces it with the richer state machine (hold/gap/max_hold/
   orphan). If hhd upstream grows a general input filter, the shim can move
   there and this plugin becomes a thin wrapper. The plugin is registered as an
   `hhd.plugins` entry point, so upstream can adopt or replace it without any
   device-grabbing code.
4. **Physical-fallback repeat proxying** is exercised end to end by
   `tests/integration/volume/` against an AT-shaped virtual device: with no hhd
   device present the daemon grabs it, creates the shim with `EV_REP`, and taps
   still produce one pair. It is still not measured on the real `event4` with a
   real keyboard (needs root and hhd stopped); validation step 5 remains the
   on-hardware check.
5. **Diagnose verdict** is based on a single N-second window with no presses
   unless the operator presses during it; it deliberately falls back to the
   static decision instead of guessing.
6. **Plugin tap feel on hardware.** The plugin's minimum hold is 60 ms (hhd's
   own tap constant) and the state machine is unit-tested and exercised through
   the fake-`OxpAtKbd` hooks, but the final "does one press always equal one
   mixer step with hhd 4.1.12" measurement needs the owner: install, restart
   hhd, press/hold the rocker, and compare against the daemon's numbers.
