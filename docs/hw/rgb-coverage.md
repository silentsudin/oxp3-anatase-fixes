# OneXPlayer 3 RGB: zones, effects, and coexistence with HHD

Status: protocol extended to the full OXP3 zone/effect set; persistent daemon
written; on-device validation of the daemon needs the privileged udev rule.
Target: ONEXPLAYER 3 (DMI `ONEXPLAYER 3`, BIOS 5.09), Anatase, kernel
7.2.7-an28, HHD 4.1.12, OneXConsole 0.10.4 (vendor reference only).

This document supersedes the RGB sections of
[home-button-leds.md](home-button-leds.md): the side-id names there were a
guess (`05/06` are **not** "body halves"), and the effect set is now complete.

Nothing vendor-proprietary is in this repo. The vendor binaries were read as
data; only our own code, our own captured frames from *this* machine, and
protocol facts are recorded here.

---

## 1. TL;DR

* The OXP3 has **five** addressable RGB zones, not eight: left stick (1),
  right stick (2), G-key zone (5), top strip (6), controller connector (7).
  There is **no per-key addressing**.
* The firmware has **nine animated presets**, one solid "classic" preset, a
  static colour, a breathing colour, per-zone on/off and four brightness
  levels.
* `tools/oxp3-rgbd.py` keeps a user config applied on top of HHD: it watches
  HHD's own RGB writes in the journal, re-applies after them, after resume,
  after device re-enumeration and on config changes. `tools/oxp3-rgbd.service`
  is a **user** unit; the only privileged step is the existing hidraw udev
  rule.
* Clean takeover is possible without root through HHD's own API
  (`hhdctl set hhd.settings.rgb=false`), or with root by blacklisting the
  `rgb` provider in `/etc/hhd/plugins.yml`.

---

## 2. Zone map (verified)

The vendor UI keeps a partition table per device. In `app.asar`
(`background.js`, OneXConsole 0.10.4) the OXP3 branch is:

```
phImageSrcKey = "OXP3"  ...  rgbPartitionList = V
```

and `V` is exactly:

| vendor id | i18n key | `pcodeList` | effect support |
|---|---|---|---|
| `left_stick` | `rgbPartition_left_stick` | `[1]` | preset + static |
| `right_stick` | `rgbPartition_right_stick` | `[2]` | preset + static |
| `gkey` | `rgbPartition_gkey` | `[5]` | static + breathing only |
| `top` | `rgbPartition_top` | `[6]` | static + breathing only |
| `connector` | `rgbPartition_controller_connector` | `[7]` | preset + static |

`pcode` is the value the vendor layer passes as the firmware **side id** in
every `0xB8` frame. HHD's OXP3 driver independently uses
`rgb_sides = (0x01, 0x02, 0x07)` and `secondary_sides = (0x05, 0x06)` — the
same split — and captured frames confirm it (section 4).

### Other OneXPlayer models (do not assume they apply here)

The 5-partition table (`W`) is `left_stick=[1]`, `right_stick=[2]`,
`v=[3]`, `kb=[4]`, `back=[5]`. So side ids `0x03` (V-zone) and `0x04`
(keyboard) exist in the protocol but belong to other models. The OXP3 has no
V-zone or RGB keyboard. `pcodeList` is an array, so a logical partition can
map to several pcodes on models that need it; OXP3 uses one pcode per
partition.

`LEDZone` in the managed layer (`All=0, JoystickLeftSideLeft=1,
JoystickLeftSideRight=2, JoystickRightSideLeft=3, JoystickRightSideRight=4`)
is part of the ASUS Aura path (`AuraMode` enum next to it), **not** the OXP
path. It is not evidence for per-key zones on this device.

Candidate ids `0x00` (possible "all zones" broadcast), `0x03`, `0x04` and
`0x08+` can only be settled on the device:
`./oxp3-led.py probe` lights `0,1,2,3,4,5,6,7,8` in turn with distinct
colours (needs the udev rule).

---

## 3. Effect / preset map

The firmware takes a single `mc` byte for presets. The vendor UI has 11
preset titles (`presetTitle1..11` in the i18n bundle); HHD's OXP driver has
the name→byte table. Putting both next to each other gives the complete map,
and every reachable value was confirmed by capturing HHD's frames on this
machine:

| id | vendor title | HHD name | frame `mc` | notes |
|---|---|---|---|---|
| 1 | Monster Woke | `monster_woke` | `0x0D` | |
| 2 | Flowing Light | `flowing` | `0x03` | |
| 3 | Sunset Afterglow | `sunset` | `0x0B` | |
| 4 | Colorful Neon | `neon` | `0x05` | |
| 5 | Dreamy | `dreamy` | `0x07` | |
| 6 | Cyberpunk | `cyberpunk` | `0x09` | |
| 7 | Colorful | `colorful` | `0x0C` | |
| 8 | AOK Classic | `aok_classic` | `0x0E` | AOKZOE UI branch |
| 9 | OXP Classic | (= solid cherry red) | — | solid `B7 30 00`, not a preset frame |
| 10 | Aurora | `aurora` | `0x01` | |
| 11 | Warm Sun | `warm_sun` | `0x08` | |

Presets are only meaningful on `0x01`, `0x02`, `0x07` (the vendor's
`supportRgbPreset` is false for `gkey`/`top`, which is why they are always
sent static colours). The firmware may simply ignore a preset frame on
`0x05/0x06`; the tool warns.

The vendor bundle also contains `rog_le_mode_{always,breath,strobe,rainbow,
circle}` and the `AuraMode`/`LEDZone` enums. Those belong to the ASUS/ROG
and AOKZOE branches of the same UI (`apiMode: rog/msi`), not to the OXP3
path, and must not be sent to this MCU. The OXP3 effect set is exactly the
11 presets above plus static/breathing/off.

Other colour commands:

| effect | frame sub-command | availability |
|---|---|---|
| static solid | `0xFE` | all five zones |
| breathing / pulse | `0xF0` | vendor exposes it for `gkey`/`top`; same frame layout as solid |
| off | brightness frame with `enabled=0` | all five zones |

`0xF0` is confirmed as the "breathing" command in HHD's OXP driver (used on
X2 where `secondary_breathing` is enabled) and matches the vendor UI's
`rgbPureBreathing` flag. It has not been visually confirmed on an OXP3 yet;
`oxp3-led.py breath` is the one-line test.

Brightness: the vendor default is 4 and the UI can send 1..4. HHD uses
`low=1`, `medium=3`, `high=4` and never sends 2. Level 2 is accepted by the
vendor UI model but is unverified on this MCU (`oxp3-led.py brightness mid`).

---

## 4. Frames (all 64 bytes, no report id)

Envelope is unchanged:

```
host -> device:  [cid] [3F] [01] [payload ...] [00 ...] [3F] [cid]
```

### Brightness / enable / off

```
B8 3F 01 FD <side> 02 <enabled 0|1> 05 <level 1..4> 00 ... 3F B8
```

Observed from HHD on this machine (2026-11-09 00:46, `hhd.service` journal):

```
b83f01fd010201050300...3fb8   side 1, enabled, level 3 (medium)
b83f01fd070201050400...3fb8   side 7, enabled, level 4 (high)
b83f01fd050200050400...3fb8   side 5, disabled
```

### Static solid

```
B8 3F 01 FE <side> 02 [r g b] x18 r g 3F B8
```

```
b83f01fe0102b73000b73000...3fb8   side 1, OXP classic cherry red
b83f01fe0502007f00007f0000...3fb8 side 5, green (duality secondary)
```

### Breathing

```
B8 3F 01 F0 <side> 02 [r g b] x18 r g 3F B8
```

### Preset

```
B8 3F 01 <mc> <side> 02 00 ... 3F B8
```

```
b83f010d020200...3fb8   side 2, monster_woke (0x0D)
b83f0101060200...3fb8   side 6, aurora (0x01) - HHD does not send this on 05/06;
                        firmware behaviour unknown, do not rely on it
b83f0108010200...3fb8   side 1, warm sun (0x08)
```

### Zone independence (duality capture)

HHD's `duality` mode sets primary and secondary colours independently. The
captured burst shows exactly two groups:

```
sides 01,02,07 -> 7F 00 00 (red)
sides 05,06    -> 00 7F 00 (green)
```

That is the direct proof that 01/02/07 and 05/06 are separate hardware
groups, and that a per-zone colour write does not bleed into the other group.

### Raw capture appendix

The complete unique-frame dump from that capture is in
[rgb-frames-2026-11-09.txt](rgb-frames-2026-11-09.txt) (55 frames: the
brightness levels HHD uses (3/4), off, all nine presets on their sides, the
solid colours and the duality pair). It is our own journal data, not vendor
code.

### Encoder self-test

`tools/oxp3_hid.py` and `tools/oxp3-led.py selftest` compare our encoder
byte-for-byte with nine frames captured from HHD on this machine (brightness
medium/high/off, three presets, OXP classic, and both duality colours). All
PASS. This is the strongest verification possible without opening the node.

---

## 5. What the vendor layer does (and what could not be read)

* `app.asar` (JavaScript) contains the device profiles, the partition table
  above, preset titles and the HTTP/IPC surface
  (`rgbPartition/setColor/{pcode}/{r}/{g}/{b}/{breathing}`,
  `setOpen/{pcode}/{open}/{level}`, `setPreset/{pcode}/{mode}`,
  `rgb/setOpen2`). That is where the zone/effect semantics come from.
* `CompatLayerCT.exe` is the managed implementation (`HidRgbHelper`,
  `HidRgbPartitionHelper`, `OldHidRgbPartitionHelper`, `PortRgbHelper`,
  `RGBService`, `LEDZone`, `SetRgbColor`, `SetLedBrightness`,
  `ReadRGBStatus`, ...). Its metadata parses cleanly with `dnfile`, but the
  **method bodies are obfuscated**: 4942 methods share only 648 distinct
  RVAs, 2829 of them point at one decoy body, and 1236 have RVA 0. The real
  IL is produced at run time by the obfuscator (likely decrypted from an
  embedded blob), so `dncil` cannot recover the per-device command
  construction without executing vendor code (which we do not do).
* `tools/ghidra-pod.sh rgb CompatLayerCT.exe '^.*(Rgb|RGB|Led|LED).*$'`
  completed with `functions=0 decompiled=0`: the file is a pure CLR image
  with no native code for Ghidra to decompile. Ghidra is still the right
  tool for the native DLLs; for this managed layer the useful evidence is
  metadata names plus the JavaScript profile and the live frames.
* `ReadRGBStatus`/`ReadRGBStatusAck` exist as names. A read-back command
  would be nice (query current zone state); its bytes are not recoverable
  from the obfuscated IL. Open point for a future USB capture or a brute
  force with the udev rule installed.

---

## 6. The HHD interaction, precisely

HHD 4.1.12's OXP3 profile (`quirk: oxp3`, `protocol: hid_v2_x2`) uses the
same hidraw node. Facts that matter:

* It writes **only** sides `01,02,07` (primary) and `05,06` (secondary).
  Sides `03/04` are never touched.
* It sends a brightness frame before the colour, for both groups, whenever
  its enabled state or brightness changes.
* Its start-up sequence sends the LED state up to **2 times, 10 s apart**
  (`RGB_SET_TIMES=2`, `RGB_SET_INTERVAL=10`), plus a burst when the device
  is (re)opened. Resume re-application is not guaranteed, which is why the
  daemon has its own `CLOCK_BOOTTIME` resume detector.
* Any change to its RGB config triggers a re-send. Note a quirk we observed:
  changing a *nested* setting (`rgb.handheld.mode.oxp.mode=aurora`) is
  sometimes deduplicated because HHD compares a shallow copy of the mode
  container; toggling the top-level mode (`disabled` → `oxp`) always
  re-sends.
* It never stops on its own, so "last writer wins". A one-shot
  `oxp3-led.py solid …` will be overwritten the next time HHD re-asserts.

### Coexistence strategy (implemented)

`tools/oxp3-rgbd.py` treats our config as the desired state and HHD as a
disturbance:

1. **Journal watcher** — follows `journalctl -u hhd -f` (readable by the
   logged-in user on this image) and detects any `0xB8` frame HHD sends.
   After the burst goes quiet (0.5 s sliding, 3 s cap) it re-applies our
   config. This depends on HHD's INFO logging of `OXP C:` frames; if that
   is turned off, the poll fallback and the watchdog still cover the
   config-change and reset cases.
2. **hhdctl poll fallback** — if the journal is not readable, it polls
   `hhdctl get rgb.handheld.mode.mode` every 2 s and re-applies on change.
3. **Resume detection** — `CLOCK_BOOTTIME` advances across suspend while
   `CLOCK_MONOTONIC` does not; a jump > 1.5 s means we woke up. No root,
   no D-Bus policy needed.
4. **Device watcher** — re-applies when `/dev/hidrawN` appears (USB reset,
   suspend with re-enumeration). No exclusive open is used, so HHD and the
   daemon can both hold the node.
5. **Watchdog** — unconditional re-apply every 30 s (configurable, 0 = off)
   for resets we cannot observe.
6. **Scope** — only zones present in the config are written. Leave
   `[zones.*]` out to hand a zone back to HHD; `brightness = "keep"` writes
   colours/presets only and leaves HHD's brightness alone.

There is no way to prevent HHD from writing while it is enabled (hidraw is
not exclusive). The watcher makes the race harmless; a clean takeover removes
the second writer entirely.

### Clean takeover (privileged or HHD-API, choose one)

```bash
# 1. Disable HHD's RGB plugin through its own API (persistent, reversible).
hhdctl set hhd.settings.rgb=false
#    revert with: hhdctl set hhd.settings.rgb=true

# 2. Or keep HHD for buttons/gyro but stop its RGB output:
hhdctl set rgb.handheld.mode.mode=disabled

# 3. Or blacklist the provider at HHD start-up (root, read only at start):
sudoedit /etc/hhd/plugins.yml        # add "- rgb" under blacklist:
sudo systemctl restart hhd
```

Option 1/2 need no root because `/run/hhd/api` is world-writable on this
image. Option 3 is the only one that stops HHD from even opening the RGB
path.

---

## 7. Using the tools

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools

./oxp3-led.py status                 # device, zones, presets, HHD mode
./oxp3-led.py zones                  # zone id table
./oxp3-led.py selftest               # encoders vs captured frames

./oxp3-led.py solid ff6600
./oxp3-led.py solid ff6600 --sides top
./oxp3-led.py breath 3050ff --sides 5,6
./oxp3-led.py brightness medium --sides all
./oxp3-led.py effect aurora --sides 1,2,7
./oxp3-led.py effect 9 --sides all    # OXP classic (solid cherry red)
./oxp3-led.py probe                   # 0,1,2,3,4,5,6,7,8 in turn
./oxp3-led.py off
```

All of the above accept `--dry-run`, which prints the exact 64-byte frames.

### Persistent daemon

```bash
# config
install -m 0644 oxp3-rgbd.toml.example ~/.config/oxp3-rgb.toml
$EDITOR ~/.config/oxp3-rgb.toml

# user unit (no root)
mkdir -p ~/.config/systemd/user
install -m 0644 oxp3-rgbd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now oxp3-rgbd.service
journalctl --user -u oxp3-rgbd -f

# one-shot / diagnostics
./oxp3-rgbd.py --once --dry-run
./oxp3-rgbd.py --check
./oxp3-rgbd.py --takeover
```

Config semantics: `effect = solid|breath|preset|off`, `color = "#rrggbb"`,
`preset = <name|1..11>`, `level = keep|low|mid|medium|high`, global
`brightness` the same. The example file configures all five zones.

Daemon flags worth knowing: `--watchdog 0` disables the slow re-apply,
`--settle 0` skips the extra applies during HHD's start-up bursts,
`--no-journal` uses only the `hhdctl` poll fallback, `--once` applies and
exits, `--check` prints state, `--takeover` prints the HHD instructions.

The unit runs at session start (`WantedBy=default.target`), which is "boot"
for an autologin handheld. For a system-wide unit before login, copy the
same ExecStart into a system unit — that is a privileged change and is not
installed by this repo.

---

## 8. Validation

Done without opening the node:

* `oxp3_hid.py` / `oxp3-led.py selftest`: 9/9 frames byte-identical to
  frames captured from HHD on this machine.
* Zone map cross-checked twice: vendor partition table (`pcodeList` for the
  `"OXP3"` profile) and HHD's independent side lists; plus the duality
  capture showing two independent groups.
* Preset map cross-checked twice: vendor `presetTitle1..11` ordering and
  HHD's name→`mc` table; all nine OXP3 preset codes plus the solid
  "OXP classic" were captured live (the AOK `0x0E` code is in HHD's AOK
  branch, not reachable on this unit).
* `oxp3-rgbd.py --selftest`: encoder + config parser.
* `oxp3-rgbd.py --once --dry-run`: prints the expected 10 frames for the
  example config.
* Watcher test: with the daemon in `--dry-run`, toggling HHD's mode caused
  `re-apply requested (HHD wrote an RGB frame)` and a re-apply after the
  burst, proving the journal path works on this image.

Still to do on the device (needs the udev rule, then no root):

```bash
# 1. access
sudo install -m 0644 tools/oxp3-hid.rules /etc/udev/rules.d/90-oxp3-hid.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --action=change
getfacl /dev/hidraw4          # expect user:holzr:rw-

# 2. zone map / unused ids (watch the device)
./oxp3-led.py probe

# 3. effects and breathing
./oxp3-led.py effect aurora --sides 1,2,7
./oxp3-led.py breath 3050ff --sides 5,6

# 4. coexistence (after installing the config + unit from section 7)
systemctl --user start oxp3-rgbd.service
hhdctl set rgb.handheld.mode.mode=oxp     # HHD writes; daemon must restore
journalctl --user -u oxp3-rgbd -f

# 5. takeover variant (if wanted)
hhdctl set hhd.settings.rgb=false
```

Expected: zones respond per the map; after HHD changes its mode the daemon
re-sends our frames, so the top strip and G-key keep our colour/brightness
instead of flipping to HHD's secondary colour.

---

## 9. Rollback

```bash
systemctl --user disable --now oxp3-rgbd.service
rm ~/.config/systemd/user/oxp3-rgbd.service
rm -f ~/.config/oxp3-rgb.toml

# give the LEDs back to HHD
hhdctl set hhd.settings.rgb=true
hhdctl set rgb.handheld.mode.mode=solid

# remove device access (optional)
sudo rm /etc/udev/rules.d/90-oxp3-hid.rules
sudo udevadm control --reload-rules
```

A full power cycle always restores firmware defaults; nothing is written to
flash.

---

## 10. Open points

1. Visual confirmation of the five zone ids and of `0xF0` breathing on this
   exact unit (`probe`, `breath`).
2. Whether `0x00` is a broadcast side, whether `0x03/0x04` still respond on
   OXP3 firmware, and what `0x08+` does. `probe` answers all three.
3. Brightness level 2 semantics (`mid`).
4. `ReadRGBStatus` read-back command bytes: recoverable only with a USB
   capture on Windows or by brute force with the udev rule installed.
5. The managed layer's real IL is obfuscated (decoy RVAs). If a future
   vendor build ships unobfuscated metadata, the per-device command builder
   can be read directly; today the JS profile + live frames are sufficient.
