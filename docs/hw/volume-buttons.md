# ONEXPLAYER 3 volume rocker: stuck keys on Linux

Status: root cause localised, userspace fix shipped in `tools/oxp3-voltap/`,
one hardware measurement still open (needs root, see [Open points](#open-points)).
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
| `event4` | bus 11, `0001:0001` | `AT Translated Set 2 keyboard` | `isa0060/serio0/input0` | the EC's keyboard interface; **carries the rocker** (`KEY=…` word1 has bits 114/115), has `EV_REP` |
| `event23` | bus 3, `0001:0001` | `Handheld Daemon Volume Keyboard` | `phys-hhd-vbtn` | hhd's uinput device; only keys 114/115; **the only place the desktop sees the rocker** |
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
altogether.

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

## Fix

Two layers, both ours, no vendor code, no kernel module, no `/usr` patching:

1. **Kernel: force-release the volume scancodes** —
   `tools/oxp3-voltap/84-oxp3-volume-keys-taps.hwdb` keeps the old
   `0xae/0xb0` pair and adds `0xa1/0xb2` (the pair atkbd actually maps to
   volume). Harmless if the EC uses only one of them. This is the primitive that
   makes a second press possible at all, because hhd cannot emit a press for a
   key that is still logically down.
2. **Userspace: `tools/oxp3-voltap`** — a ~650-line python3-evdev daemon that
   grabs `event23` (`EVIOCGRAB`), drops every `value == 2`, drops duplicate
   presses and contact bounce, emits exactly one 30 ms down/up pair per press
   through its own uinput device, force-releases anything that somehow stays
   down (500 ms watchdog), re-attaches when hhd recreates its device, and
   releases all keys on SIGTERM. It also turns a *release without a press* into
   one tap, which is exactly what hhd emits when the kernel already
   force-released the scancode — so `patches/hhd/0001,0002` are no longer
   needed for the rocker to work.

Why this layer: `event23` is the only place the desktop ever receives the
rocker, it exists whatever hhd version is installed, it is key-only (no axes,
no `EV_REP`, so nothing else has to be proxied), and a grab there can be
dropped at any moment with no side effects. `/usr/local` and `/etc` are on the
mutable side of rpm-ostree (`/usr/local -> /var/usrlocal`), so the fix survives
OS updates.

### Install (root; these are the exact commands)

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
# 1. Which device really carries the rocker, and what the EC sends.
#    Read-only; press the rocker a few times, Ctrl-C, read the verdict lines.
sudo /usr/local/libexec/oxp3-voltap probe --device /dev/input/event4 --stuck-ms 400
#    expected with the hwdb active: one press + one release per tap, no value=2,
#    "longest press" a few ms. Without force-release: press but no release
#    ("VERDICT missing release events"), or value=2 ("kernel/client repeat").

# 2. uinput plumbing only (creates the shim, emits two taps).
sudo /usr/local/libexec/oxp3-voltap selftest
#    the mixer must move exactly two steps, and the journal must show the device.

# 3. End to end.
systemctl status oxp3-voltap
journalctl -u oxp3-voltap -b --no-pager
#    expect: grabbed /dev/input/event23 (Handheld Daemon Volume Keyboard)
#    then: 3 quick taps -> 3 steps; hold 2 s -> one step per press, no runaway;
#    repeat with hhd stopped (sudo systemctl stop hhd) to see the failure return
#    and with hhd started again to see the daemon re-grab within ~1 s.

# 4. Counters (set VOLTAP_STATS=10 in /etc/oxp3-voltap.conf, restart the unit):
#    press/repeat/duplicate/bounce/orphan_tap/late_release tell you which
#    mechanism was actually live on your unit.
```

## Risks and rollback

* **Risk: volume keys stop completely** (e.g. hhd disabled and nothing feeds
  `event23`). The daemon logs "nothing grabbed yet" and does nothing; the
  desktop then gets the raw rocker again (the bug, not a loss of function).
* **Risk: grab races with hhd.** hhd recreates its uinput device on restart;
  the daemon rescans once a second, and an `EBUSY` grab is retried, logged
  once. Worst case one press is lost during the ~1 s window.
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
   `0xe0`-prefixed). Decided by validation step 1; both pairs are force-released
   meanwhile, so the fix does not depend on the answer.
2. **Whether Windows ramps on hold** identically. The vendor app is out of the
   path (evidence 7), so the comparison is OS-vs-OS; not measured.
3. **hhd upstream**: `hhd-dev/hhd#342` describes the same lost release. If hhd
   ever emits a proper pair itself, the shim becomes redundant and can be
   stopped without changing anything else.
4. The daemon was written and syntax/logic-tested here (`python3 -m
   py_compile`, offline state-machine tests: pair, repeats, duplicate press,
   orphan release, bounce, late release, stuck press) but **not executed as
   root** in this session — validation steps 2–4 are the first real run.
