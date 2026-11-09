# OneXPlayer 3: home button and body RGB on Linux

Status: reverse-engineered + implemented as a userspace tool.
Target: ONEXPLAYER 3 (DMI `ONEXPLAYER 3`, vendor `ONE-NETBOOK`, BIOS 5.09),
Anatase (Fedora Silverblue style), kernel 7.2.7-an28, HHD 4.1.12.

The body RGB (stick rings + top strip) is **not** an LED class device and
the home button is **not** an evdev key. Both live on a vendor HID
collection of the WCH (QinHeng) USB device `1a86:fe00`, which the
in-box `hid-generic` driver exposes as a raw `/dev/hidrawN` node.

---

## 1. Hardware / software inventory (measured)

### USB devices

| sysfs | ID | what |
|---|---|---|
| `3-5` | `1a86:fe00` (serial `0123456789`, bcdDevice 0155) | WCH composite: keyboard + mouse/consumer + vendor HID |
| `3-7` | `045e:028e` | Xbox 360 pad (xpad), guide LED only |
| `3-4` | `1997:2433` | Riitek wireless mini keyboard (not the OXP MCU) |

`1a86:fe00` has three interfaces:

| interface | class | hidraw | input node | role |
|---|---|---|---|---|
| `3-5:1.0` | HID boot keyboard (03/01/01) | `hidraw2` | `input9` (`HID 1a86:fe00`) | keyboard, `KEY_HOMEPAGE` etc. advertised |
| `3-5:1.1` | HID boot mouse (03/01/02) | `hidraw3` | `input10/11/12` | mouse, consumer + system control |
| `3-5:1.2` | HID vendor (03/00/00) | `hidraw4` | **none** | **RGB + extra buttons** |

Vendor collection report descriptor (`/sys/class/hidraw/hidraw4/device/report_descriptor`):

```
06 00 ff              Usage Page (0xFF00)
09 01                 Usage (0x01)
a1 01                 Collection (Application)
09 02 15 00 26 00 ff 75 08 95 40 81 06   Input  (64 x 8 bit, no Report ID)
09 02 15 00 26 00 ff 75 08 95 40 91 06   Output (64 x 8 bit, no Report ID)
c0
```

So: **no report ID, 64-byte input and output reports, usage page 0xFF00 /
usage 0x0001**. `/dev/hidraw4` is `crw------- root root` by default, i.e.
userland needs a udev rule (section 5).

### LED class inventory

`ls /sys/class/leds`:

* `xpad0` – Xbox controller guide LED (`max_brightness` 255, only the
  guide light; **not** the stick/top RGB)
* `phy0-led` – WiFi
* `input4::`, `input5::`, `input9::` – keyboard lock LEDs

There is **no** LED class device for the body lighting, and no
`leds`/`multicolor` entry for it. That is why only "stick colours" look
addressable: they are driven out-of-band through the vendor HID channel.

### Input devices

`/proc/bus/input/devices` lists `HID 1a86:fe00` (kbd), `... Mouse`,
`... Consumer Control`, `... System Control`, `Microsoft X-Box 360 pad`
(`event16`/`js0`) and HHD's virtual controller. **No device emits a
home/guide key from the OXP MCU**: the home key is delivered as a vendor
report (section 2), and `input9`'s capability bitmap advertising
`KEY_HOMEPAGE` is just the boot-keyboard capability set.

### In-kernel driver

All three interfaces bind to `hid-generic` (`/sys/.../driver` →
`hid-generic`). No `hid-oxp`, no `xpad` claim on `1a86:fe00`; the vendor
interface has no input child, so nothing in the kernel interprets its
reports. Raw `/dev/hidraw4` writes therefore reach the MCU unmodified,
and the kernel does not consume the home-button reports. `hidraw` does
not allow exclusive opens, so several readers can coexist (important for
HHD, see section 8).

---

## 2. Protocol (exact report formats)

All frames are **64 bytes**, no report id, no checksum. Scalar bytes are
literal; RGB is 8-bit per channel.

### Envelope

```
host -> device:  [cid] [0x3F] [0x01] [payload ...] [00 ...] [0x3F] [cid]
device -> host:  [cid] [0x3F] [idx?]  [payload ...]          [0x3F] [cid]
```

Validation used by HHD and by this tool: `buf[1] == 0x3F`,
`buf[-2] == 0x3F`, `buf[-1] == buf[0]`. `cid` values:

| cid | direction | purpose |
|---|---|---|
| `0xB8` | host | RGB: brightness / solid colour / preset |
| `0xB2` | both | host: intercept switch; device: button reports |
| `0xB4` | host | button/key mapping table (vendor utility, HHD) |
| `0xB3` | host | rumble strength |
| `0xF5` | host | older init (X1 family) |

### RGB commands (cid `0xB8`)

Brightness / zone enable:

```
B8 3F 01 FD <side> 02 <enabled> 05 <level> 00 ... 3F B8
                      0/1               1=low 3=medium 4=high
```

Solid colour (18 identical RGB triplets + a trailing r,g pad):

```
B8 3F 01 FE <side> 02 [r g b] x18 [r g] 3F B8        (3+59+2 = 64 bytes)
```

Hardware preset (effect):

```
B8 3F 01 <preset> <side> 02 00 ... 3F B8
```

Preset codes (firmware `mc` byte): `01` aurora, `03` flowing, `05` neon,
`07` dreamy, `08` warm sun, `09` cyberpunk, `0B` sunset, `0C` colorful,
`0D` monster woke, `0E` AOK classic.  The vendor's 11 preset ids and the
full value->effect table are in **[rgb-coverage.md](rgb-coverage.md)**.

Zone/side ids on the OXP3 (resolved, superseding the earlier guess):
`0x01` left stick, `0x02` right stick, `0x05` G-key zone, `0x06` top
strip, `0x07` controller connector.  `0x03` (V-zone) and `0x04`
(keyboard) belong to other OneXPlayer models.  Evidence: the vendor
partition table in `app.asar` (`pcodeList` for the `"OXP3"` profile is
exactly `1,2,5,6,7`), HHD's independent side lists
(`rgb_sides=(1,2,7)`, `secondary=(5,6)`) and live frame captures.
There is no per-key addressing; see [rgb-coverage.md](rgb-coverage.md).

### Button reports (cid `0xB2`)

```
B2 3F ... <button id at byte 6> ... <pressed at byte 12> ... 3F B2
```

Observed button ids: `0x21` guide, `0x22` extra L1, `0x23` extra R1,
`0x24` **home** (OXP3: real press *and* release; other ids may only
send a press). Intercept switch (what the vendor utility sends):

```
B2 3F 01 03 01 02 ... 3F B2   (enable)
B2 3F 01 00 01 02 ... 3F B2   (disable - HHD leaves it disabled)
```

With intercept disabled the extra buttons still arrive as `0xB2`
reports; only the firmware's own key emission changes.

### Frames captured live from this machine

From HHD's own log of the commands it sent to `hidraw4` at boot (same
device, same firmware, so these are ground truth for the format):

```
b4 3f 01 02 38 02 01 01 01 01 01 00 00 00 02 01 02 00 ...    mapping table
b4 3f 01 02 38 02 02 01 0a 01 0a 00 00 00 0b 01 0b 00 ...    mapping table
b4 3f 01 02 38 02 03 01 24 02 02 05 00 00 00 25 01 21 ...    mapping table
b2 3f 01 00 01 02 00 ...                                    intercept off
b8 3f 01 fd 05 02 01 05 04 00 ...                            side 5 enable, high
b8 3f 01 fd 06 02 01 05 04 00 ...                            side 6 enable, high
b8 3f 01 fe 05 02 7f 70 00 7f 70 00 ... (x18) 3f b8          side 5 solid amber
b8 3f 01 fe 06 02 7f 70 00 7f 70 00 ... (x18) 3f b8          side 6 solid amber
b8 3f 01 fe 01 02 00 54 7f ... (x18) 3f b8                   side 1 solid blue
b8 3f 01 fe 02 02 00 54 7f ...                               side 2 solid blue
b8 3f 01 fe 07 02 00 54 7f ...                               side 7 solid blue
```

Note the asymmetry: HHD sends the **brightness/enable** command only for
sides `05/06`; sides `01/02/07` only ever receive the solid colour, never
the enable/brightness command. If a zone (e.g. the top strip) powers up
disabled, it stays dark. Our tool always sends brightness+enable before
the colour for every side it touches - that is the concrete difference
for the "top LEDs stay dark" symptom.

---

## 3. Windows side (reverse engineering notes)

* No vendor kernel driver ships for `1a86:fe00`; the driver package
  (`/var/home/holzr/Downloads/oxp3/OXP-Gi78P-ONEXPLAYER3-Drivers-1.1`)
  contains no INF matching `1A86`/`FE00`. Windows binds the in-box HID
  driver, so the LED protocol is **userland**.
* `OneXConsole_0.10.4-fix1.exe` is an NSIS wrapper around an Electron
  app (`app-64.7z` -> `app.asar`). Its hardware layer is the managed
  assembly `resources/resources/CompatLayerCT.exe`, which embeds
  `HidSharp`, `HidLibrary`, `HidApiAdapter` (Costura) and contains
  `HidRgbHelper`, `HidRgbPartitionHelper`, `PortRgbHelper`, `RGBService`,
  `SetRgbCmd`, `SetRgbColor`, `SetLedBrightness`, `ReadRGBStatus`,
  `LEDZone`, plus the device-path string `vid_1a86&pid_fe01`.
* `App.asar` exposes `/programhandle/rgb/setColor|setPreset|setOpen2`,
  `/rgbPartition/...`, `/handledHID/...` HTTP endpoints and the i18n
  partition names (`rgbPartition_top`, `_left_stick`, `_back`, `_v`,
  `_gkey`, `_controller_connector`).
* Limitation: the `CompatLayerCT.exe` metadata is obfuscated (method
  RVAs/names are scrambled - `dnfile` reports duplicate RVAs and
  invalid method headers), and no .NET decompiler is installed in this
  environment, so the IL of `SetRgbColor`/`SetRgbPartitionHelper` could
  not be dumped. The report format above is therefore established from
  the device's own traffic (section 2), not from decompiled vendor code.
  The obfuscated assembly was **not** executed and no vendor code is
  redistributed.
* Ghidra was used through `tools/ghidra-pod.sh` on the native helper
  DLLs; `MagpieRT.dll` ("1a86" hit) and `WCHBLEDLL.dll` are unrelated
  (graphics upscaler / WinRT BLE library). Nothing useful for the RGB
  path; the vendor protocol lives in the managed layer.

---

## 4. Does a kernel driver claim the device?

`hid-generic` claims all three interfaces. The vendor interface only
creates `/dev/hidraw4`; the kernel does not interpret the vendor reports
and does not expose the extra buttons as input. Consequences:

* **LEDs:** raw writes to `/dev/hidraw4` are the supported path.
* **Home button:** must be read from `/dev/hidraw4` (`0xB2` reports) and
  re-injected as a key (uinput) or turned into a command.
* No module can be unbound/rebound to change this; there is nothing to
  patch in the kernel for a userspace-only solution.

---

## 5. Install (privileged steps - run by you, not by the agent)

```bash
# 1. udev rule: give the logged-in seat access to the vendor hidraw node
sudo install -m 0644 \
  /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/oxp3-hid.rules \
  /etc/udev/rules.d/90-oxp3-hid.rules

# 2. reload and re-apply (no reboot needed)
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --action=change
sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput --action=change

# 3. verify the ACL (should show user:holzr:rw-)
getfacl /dev/hidraw4
```

Installing the rule is the only sudo step. Everything else runs as the
user. If `/dev/uinput` is not already accessible (some images do not
apply the seat ACL), the rule file also carries a `uinput` uaccess line.

Home-button daemon as a user unit:

```bash
mkdir -p ~/.config/systemd/user
install -m 0644 \
  /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/oxp3-homebtn.service \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now oxp3-homebtn.service
journalctl --user -u oxp3-homebtn -f
```

Rollback:

```bash
systemctl --user disable --now oxp3-homebtn.service
rm ~/.config/systemd/user/oxp3-homebtn.service
sudo rm /etc/udev/rules.d/90-oxp3-hid.rules
sudo udevadm control --reload-rules
```

---

## 6. Using the tools

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools

# what is where, and is the node accessible?
./oxp3-led.py status

# pin a specific node instead of discovering 1a86:fe00 (tests/debugging);
# oxp3-led.py, oxp3-rgbd.py and oxp3-homebtn.py all take --device PATH
./oxp3-led.py --device /dev/hidraw9 solid ff0000

# encoder self-check against frames captured from this machine
./oxp3-led.py selftest

# colour (all zones), brightness, preset effect, off
./oxp3-led.py solid ff6600
./oxp3-led.py solid 0080ff --sides 7          # only the "top" id
./oxp3-led.py brightness medium --sides all
./oxp3-led.py effect aurora --sides 1,2
./oxp3-led.py off

# resolve the physical zone map: lights one side at a time, 1.5 s each
./oxp3-led.py probe

# home button -> KEY_HOMEPAGE (default) or a command
./oxp3-homebtn.py --debug
./oxp3-homebtn.py --exec 'steam steam://open/bigpicture'
./oxp3-homebtn.py --button 0x21 --also-guide      # different MCU mapping
```

`--dry-run` prints the exact 64-byte frames without writing; use it to
compare against the capture appendix above.

---

## 7. Validation performed / to perform

Done without privileges:

* `./oxp3-led.py selftest` - encoder output byte-identical to the live
  frames captured from HHD on this machine (brightness, solid on two
  different sides). PASS.
* `./oxp3-led.py --dry-run solid/effect/brightness` - frame layout is
  64 bytes, envelope and pad correct.
* `./oxp3-homebtn.py` - creates the virtual keyboard successfully
  (`/sys/class/input/input*/name` = "OXP3 Home Button"), retries the
  hidraw open on `EACCES` until the udev rule is installed.
* Device/interface/descriptor inventory as in section 1.

After installing the rule:

1. `./oxp3-led.py solid ff0000` → sticks/body turn red.
2. `./oxp3-led.py probe` → writes sides 01,02,05,06,07 in turn; note
   which physical zone changes for each id and record the result here.
3. `sudo evtest` (or `wev`) on the new "OXP3 Home Button" device → press
   the home key, expect `KEY_HOMEPAGE` press/release. Alternative:
   `./oxp3-homebtn.py --debug` prints `button 0x24 pressed/released`.
4. `./oxp3-led.py off` → zones dark; `brightness high` + `solid` restores.

If the top strip still does not light after `probe`, the remaining
candidates are (a) a global enable the vendor utility sends during its
own init, (b) a different side id outside `01..07`, or (c) the strip
being wired to a separate MCU channel. `probe` distinguishes (b); (a)
would need a USB capture on Windows.

---

## 7a. Integration harness (virtual HID, opt-in, root)

`tests/integration/hid/` drives the **real** `tools/oxp3-homebtn.py`,
`tools/oxp3-led.py` and `tools/oxp3-rgbd.py` against a virtual HID device that
the harness creates through `/dev/uhid` with the exact descriptor from section 1
(usage page `0xFF00`, no report id, 64-byte input+output reports). It uses the
USB identity the tools look for (`1a86:fe00`) with an unmistakable test
name/phys (`OXP3 uhid integration test …` / `phys-oxp3-uhid-integration-test`),
so nothing on the real machine can be confused with it. The kernel exposes it
as a normal `/dev/hidrawN` without an input child - the same shape as interface
`1.2`.

```sh
sudo tests/integration/hid/run.sh                 # whole harness
sudo tests/integration/hid/run.sh --keep          # keep device/daemons for debugging
sudo tests/integration/hid/run.sh --timeout 180   # watchdog (default 120 s)
OXP3_REPO_TEST_INTEGRATION=1 tools/repo-test.sh --only hid-integration
```

It proves, end to end:

* **home button**: the daemon opens the pinned node, parses the documented
  `0xB2` report and emits `KEY_HOMEPAGE` press/release on the uinput keyboard it
  creates (unique test name `OXP3 Home Button (integration test)`, read back
  through evdev);
* **LED bytes**: `solid ff6600 --sides top --level medium` puts exactly the
  brightness frame and the 18-triplet solid frame on the wire (`UHID_OUTPUT`),
  byte-identical to the encoder and to the tool's own `send <hex>` output;
* **rgbd**: `--once` applies exactly the planned frames and `--check` reports
  the node;
* **reconnect**: destroying the uhid device produces `device lost:` /
  `retrying in`, recreating it gives `reconnect #1 to <new node> (identity …)`,
  and the button still works afterwards.

The tools are pinned with `--device`, so their discovery scan is never used
while a real `1a86:fe00` interface is present; the harness also checks that every
node path in the daemons' logs and every hidraw fd they hold is one of its own
virtual nodes. It never writes to a real hidraw node, never runs sudo itself and
tears everything down in a `finally` block (including on SIGTERM/SIGINT).
Missing access skips with exit 77 and prints the `sudo` command.

What still requires the real device: the physical home-button press and MCU
timing; the udev rule/ACL and USB sysfs identity (a uhid device has no USB port
or serial); real USB reset/suspend re-enumeration; and whether the firmware
actually accepts the frames (the harness proves what we send, not how the LEDs
look).

---

## 8. Risks, conflicts, rollback

* The vendor protocol is undocumented; only the frames in section 2 are
  known-good. Unknown `cid`/side values are **not** sent by the tools
  (side ids are limited to `0x00..0xFF`, but only the documented
  commands are generated). A malformed frame is ignored by the MCU in
  practice; a reboot restores the firmware defaults.
* **HHD conflict (important).** HHD's `rgb` plugin drives the same node
  (`0xB8` frames, sides `01,02,07,05,06`) every time its mode changes, at
  boot (two attempts 10 s apart) and after resume. Two consequences:
  * the last writer wins: after HHD updates its state, our colour is
    overwritten;
  * a zone that HHD does not manage (or that it disabled) stays in its
    current state, because HHD only re-sends brightness for its own
    sides while its own state changes.

  Choose one owner:
  * keep HHD for RGB: `hhdctl set rgb.handheld.mode.solid.hue=...`, and
    use `oxp3-led.py` only for test/probe; or run `tools/oxp3-rgbd.py`,
    which re-applies our config after every HHD write (see
    [rgb-coverage.md](rgb-coverage.md)); or
  * blacklist the RGB provider in `/etc/hhd/plugins.yml`
    (`blacklist: [rgb]`) and drive the LEDs with `oxp3-led.py`; or
  * stop the RGB plugin at runtime via HHD settings.
* **Home button conflict.** HHD maps `0x24` to its own "mode" action, so
  pressing home will trigger HHD *and* our daemon. If only one action is
  wanted, either blacklist the `onexplayer` provider or do not run
  `oxp3-homebtn.py`.
* Both tools open the node read/write; hidraw allows multiple readers,
  so no exclusive-open failure is expected. A suspend/resume or device
  reset re-enumerates `hidrawN`; the daemon re-opens automatically,
  the LED tool must be re-run.
  The reconnection contract (identity, backoff, journal lines, tests) is in
  [../DAEMONS.md](../DAEMONS.md).
* Rollback is in section 5; a Windows boot or a full power cycle clears
  any lighting state left in the MCU.

---

## 9. Open points

1. ~~Physical side→zone map~~ resolved in [rgb-coverage.md](rgb-coverage.md):
   `01/02` sticks, `05` G-key, `06` top, `07` connector; `probe` is only
   for confirming unused ids on a new firmware.
2. Whether the top strip needs an extra global enable that the Windows
   utility sends at start-up (needs a Windows-side USB capture).
3. Semantics of the `0xB4` mapping table (button id → HID usage); the
   frames captured at boot are known-good but the field layout is not.
4. `vid_1a86&pid_fe01` appears in the vendor layer while the device
   enumerates as `fe00`; possibly a child/composite path on Windows.
   Not needed for the Linux path (usage page + interface number are
   sufficient).
5. EC path: the body LEDs are on the HID MCU, not the EC. The EC side
   (`oxp-platform`) only exposes `tt_toggle` (turbo key) and the TDP
   mailbox used by `oxp3-tdp-sync`; no LED registers.
