# Installing the OneXPlayer 3 hardware fixes — one guide

This is the single install guide for the **hardware** work in this repository: volume rocker, home
button + top LEDs, body RGB, suspend/resume wake policy, Bluetooth firmware, power envelope (PL1 /
PL2 / PL4), fan, battery charge limit / charge-inhibit ("bypass"), adaptive TDP, IMU blacklist and
the speaker EQ. The driver stack (kernel modules, Mesa, gamescope, XeSS, MangoHud, firmware RPMs)
stays in [`oxp3-fixes-fast.sh`](../oxp3-fixes-fast.sh) / [`oxp3-fixes.sh`](../oxp3-fixes.sh).

Nothing here was executed on the host while writing it. Every privileged command below is copy-paste
material for you.

## 0. Entry point (decided)

```
tools/oxp3-hw-install.sh        <- the one hardware entry point
```

* `oxp3-fixes.sh` / `oxp3-fixes-fast.sh` remain **the OS/driver installers** (kernel modules,
  firmware packages, Mesa, gamescope, XeSS, MangoHud). Both now call
  `tools/oxp3-hw-install.sh --leds --power` in `install` (and the matching `--uninstall`) so the
  home-button/LED and power units are not forgotten there either.
* Every hardware component keeps its **own** installer as the implementation:
  `tools/oxp3-voltap/install.sh`, `tools/oxp3-power-install.sh`, `tools/oxp3-bt-firmware.sh`,
  `tools/audio/install-user.sh`. The orchestrator only sequences them, adds the pieces that had no
  installer (LEDs/home button, RGB keeper wiring, sleep units, IMU blacklist) and wires the
  user-scope parts to the invoking user.

Why a separate entry point:

* hardware fixes live in `/etc` and `/usr/local` (`/var/usrlocal`), which survive rpm-ostree updates;
  the driver stack lives in the `/usr` overlay or a layered deployment, which does not. Mixing them
  into one script would couple a reboot-staged kernel module install to a udev rule install.
* each component is independently useful and independently reversible; the orchestrator gives you a
  per-component flag and a per-component rollback instead of one all-or-nothing script.
* it can be tested offline with `--dry-run` and `--root DIR`.

| component | implementation | scope | survives an Anatase update |
|---|---|---|---|
| volume | `tools/oxp3-voltap/install.sh` (daemon + hwdb) | root (`/usr/local`, `/etc`) | yes |
| LEDs / home button | orchestrator + `tools/oxp3-hid.rules`, `tools/oxp3-homebtn.service` | root rule + user unit | rule: yes, unit: yes |
| sleep | orchestrator + `files/oxp3-sleep-*` | root (`/etc`, `/usr/local`) | yes |
| Bluetooth | `tools/oxp3-bt-firmware.sh` | root (`/usr/lib/firmware/updates`) | files no, dracut conf yes |
| power / fan | `tools/oxp3-power-install.sh` | root (`/etc`, `/usr/local`) | yes |
| RGB | orchestrator + `tools/oxp3-hid.rules`, `tools/oxp3-rgbd.*` | root rule + user config/unit | yes |
| IMU | orchestrator + `files/oxp3-bmi160.conf` | root (`/etc/modprobe.d`) | yes |
| audio / EQ | `tools/audio/install-user.sh` | user (`~/.config/pipewire`) | yes |

## 1. Prerequisites

* Anatase (`rpm-ostree`) host, KDE Plasma Wayland session or a console — but **run the install from
  the desktop session of the user who games**, because the user units are enabled for that user and
  a root shell has no user manager bus.
* The checkout at a path **without whitespace** (the user units reference it in `ExecStart`).
* Root for the hardware components (`sudo`). The audio part alone needs no root.
* `hhd` running for the volume-rocker device and the IMU (the repo's `oxp3-fixes*.sh` extras expect
  the same). `python3-evdev` is already a hhd dependency.
* PipeWire / WirePlumber for the speaker EQ.
* Network **only** for the Bluetooth step (the script downloads the upstream `linux-firmware` files
  and verifies pinned SHA-256 sums; nothing vendor-proprietary is stored in this repo).
* `runuser` (util-linux, always present) for the user-scope half.
* No BIOS/kernel-cmdline change is required. `mem_sleep` already selects `[s2idle]`.

## 2. The ordered command set

### 2.1 Full stack on a stock Anatase (recommended)

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

# 1. driver stack (kernel modules, firmware package, Mesa, gamescope, MangoHud, XeSS).
#    build as your user; install asks for sudo; after a kernel change run install twice
#    (the first run stages modules/firmware and asks for a reboot).
./oxp3-fixes-fast.sh build
./oxp3-fixes-fast.sh install

# 2. hardware fixes: one command. This adds what the driver installer does not cover:
#    sleep units, RGB keeper + config, speaker EQ, and (re)asserts LEDs + power units.
sudo tools/oxp3-hw-install.sh --sleep --leds --power --rgb --audio

# 3. state report + verification (see section 5)
tools/oxp3-hw-install.sh --status
tools/oxp3-status.sh
```

`--volume` and `--bluetooth` are omitted above on purpose: `oxp3-fixes-fast.sh` already installs the
hhd volume-rocker patch and layers the Bluetooth firmware package. The `oxp3-voltap` daemon is the
*alternative* to the hhd patch ([volume-buttons.md](hw/volume-buttons.md)), not an addition to it.

### 2.2 Hardware fixes only (no driver stack)

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

sudo tools/oxp3-hw-install.sh --dry-run --all    # review the exact commands first
sudo tools/oxp3-hw-install.sh --all              # volume sleep bluetooth power imu leds rgb audio
```

`--all` includes the `oxp3-voltap` volume daemon and the standalone Bluetooth-firmware script.

### 2.3 Optional policy steps (not applied by `--all`)

The power installer deliberately installs units that are inert until a profile is saved; apply what
you want, in this order:

```bash
# power envelope: quiet|balanced|performance (writes /etc/oxp3/power.json, re-applied at boot/resume)
sudo /usr/local/libexec/oxp3-powerctl profile balanced --save
sudo systemctl start oxp3-power-profile.service

# battery care
sudo /usr/local/libexec/oxp3-powerctl battery limit 80 --save
sudo /usr/local/libexec/oxp3-powerctl battery bypass on --save        # approximate vendor "bypass"

# opt-in fan curve and adaptive TDP are installed but inactive
sudo systemctl enable --now oxp3-fan-curve.service
sudo systemctl start oxp3-adaptivetdp.service                        # enable only after validating

# speaker EQ: make the filter sink the default and restart the user PipeWire
tools/audio/eq-on.sh
```

Or pass them to the installer instead (same tool calls):

```bash
sudo tools/oxp3-hw-install.sh --power --power-profile balanced --battery-limit 80 \
     --battery-bypass on --fan-curve --adaptive-tdp
sudo tools/oxp3-hw-install.sh --audio --audio-activate
```

### 2.4 Both scripts after every Anatase update

```bash
./oxp3-fixes-fast.sh install          # driver stack for the new deployment (twice after a kernel change)
sudo tools/oxp3-hw-install.sh --sleep --leds --power --rgb --audio   # idempotent re-assert
```

## 3. Per-component opt-out

`--all` is just the union of the flags; to opt out, list the components you want instead.
Every flag is repeatable and idempotent.

| flag | installs | remove with |
|---|---|---|
| `--volume` | `oxp3-voltap` daemon, `/etc/oxp3-voltap.conf`, unit, force-release hwdb | `--uninstall --volume` |
| `--leds` | `90-oxp3-hid.rules`, user unit `oxp3-homebtn.service` | `--uninstall --leds` |
| `--sleep` | `oxp3-sleep-wakeup/pre/log` units + `/usr/local/libexec/oxp3-sleep-*` + `99-oxp3-sleep-wakeup.rules` | `--uninstall --sleep` |
| `--bluetooth` | upstream firmware in `/usr/lib/firmware/updates/intel`, dracut `install_items`, controller reload | `--uninstall --bluetooth` |
| `--power` | `oxp3-powerctl`, boot/resume units; `--fan-curve`, `--adaptive-tdp` add the opt-in units | `--uninstall --power` |
| `--rgb` | shared HID rule, `~/.config/oxp3-rgb.toml` (if absent), user unit `oxp3-rgbd.service` | `--uninstall --rgb [--purge-config]` |
| `--imu` | `/etc/modprobe.d/oxp3-bmi160.conf` (keeps BMI260 bound; read-only `oxp3-imu.py` needs no install) | `--uninstall --imu` |
| `--audio` | `~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf` | `--uninstall --audio` |

Other useful switches: `--dry-run` (print every command, change nothing), `--status` (read-only
matrix), `--root DIR` (offline simulation), `--user NAME` (whose user-scope files are written;
default `SUDO_USER`), `--bt-persist=items|omit|none`, `--bt-with-ddc`, `--bt-no-reload`,
`--bt-from DIR`, `--imu-reload`, `--purge-config`.

Known interaction: **do not run `--volume` together with the hhd volume-rocker patch** that
`oxp3-fixes*.sh install` applies — both normalise the same key. Pick one; the orchestrator's
`--volume` is the standalone path, the hhd patch is the driver-installer path.

## 4. What gets written where

```
/usr/local/libexec/oxp3-voltap          /etc/systemd/system/oxp3-voltap.service
/usr/local/libexec/oxp3-sleep-wakeup    /etc/systemd/system/oxp3-sleep-{wakeup,pre,log}.service
/usr/local/libexec/oxp3-sleep-log       /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules
/usr/local/libexec/oxp3-powerctl        /etc/systemd/system/oxp3-power-{profile,resume}.service
/usr/local/libexec/oxp3-adaptivetdp     /etc/systemd/system/oxp3-fan-curve.service
                                        /etc/systemd/system/oxp3-adaptivetdp.service
/etc/udev/rules.d/90-oxp3-hid.rules     /etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb
/etc/modprobe.d/oxp3-bmi160.conf        /etc/oxp3-voltap.conf (created once, kept)
/etc/dracut.conf.d/60-oxp3-bt-firmware.conf
/usr/lib/firmware/updates/intel/ibt-00a0-0291-{iml,pci}.sfi

~/.config/systemd/user/oxp3-homebtn.service
~/.config/systemd/user/oxp3-rgbd.service
~/.config/oxp3-rgb.toml
~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf
```

`/etc/oxp3/` (power profile, baseline, fan curve) and `/var/log/oxp3-adaptivetdp.jsonl` are created
by the tools when you save/run them, not by the installer.

## 5. Verification matrix

Run everything as your user unless marked root. `sudo` is only needed where shown.

| fix | command | expected |
|---|---|---|
| install state (all) | `tools/oxp3-hw-install.sh --status` | one line per component; `yes` / `enabled/active` for what you installed |
| full repo matrix | `tools/oxp3-status.sh` | human report, exit 0; `--json` for machines, `--strict` fails on warn/unknown |
| volume rocker | `systemctl status oxp3-voltap` | `active (running)` |
| | `journalctl -u oxp3-voltap -b \| grep grabbed` | `grabbed /dev/input/eventN (Handheld Daemon Volume Keyboard)` |
| | press volume up 3× | one step each (no ramp); `sudo systemctl stop oxp3-voltap` brings the bug back |
| home button | `systemctl --user status oxp3-homebtn` | `active (running)` |
| | `journalctl --user -u oxp3-homebtn -b` | unit started; a press produces `KEY_HOMEPAGE` |
| LEDs | `tools/oxp3-led.py status` | `device:`, `zones (side ids) :`, `presets :`, `hhd rgb mode :` |
| | `tools/oxp3-led.py selftest` | encoder self-test passes against the captured frames |
| | `getfacl /dev/hidraw4` | `user:holzr:rw-` from `TAG+="uaccess"` |
| RGB keeper | `systemctl --user status oxp3-rgbd` | `active (running)` once `~/.config/oxp3-rgb.toml` exists |
| | `tools/oxp3-rgbd.py --check` | `PASS config parses (5 zones, N frames)` (or `FAIL config parses` with the parse error) |
| | `tools/oxp3-rgbd.py --once --dry-run` | prints the 64-byte frames, writes nothing |
| suspend policy | `systemctl status oxp3-sleep-wakeup` | `active (exited)`; journal has `policy applied` or `policy already in place` |
| | `tools/sleep-power-measure.sh audit` | `mem_sleep : [s2idle] deep`, wake-source table; no `enabled` for pad/storage/USB4 |
| | `sudo tools/sleep-power-measure.sh suspend -w 600` | a real suspend; PASS/WARN/FAIL against the %/h thresholds |
| Bluetooth | `tools/oxp3-bt-firmware.sh --check` | files present with the pinned hashes; exit 0 only when this boot also loaded the firmware, exit 1 with the fix commands when the running boot still has no firmware |
| | `ls /sys/class/bluetooth` | `hci0` |
| | `bluetoothctl show` | a `Controller …` block with a `Powered: yes`-able radio |
| | `journalctl -k -b \| grep 'Bluetooth: hci0'` | `Firmware timestamp …`; **no** `Failed to load Intel firmware file` |
| power units | `systemctl status oxp3-power-profile oxp3-power-resume` | `enabled`; active only once `/etc/oxp3/power.json` exists |
| power limits | `/usr/local/libexec/oxp3-powerctl status` | `PL1/PL2/PL4` for **both** `intel-rapl:0` and `intel-rapl-mmio:0`; `profile: balanced` after `--power-profile balanced` |
| | `cat /sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw` | `45000000` for balanced (`80000000` performance, `25000000` quiet) |
| battery limit | `/usr/local/libexec/oxp3-powerctl battery status` | `charge_control_end_threshold 80 [root-only]`; `bypass: off` or `bypass: on` |
| | `cat /sys/class/power_supply/BAT0/charge_control_end_threshold` | `80` |
| analog bypass | `cat /sys/class/power_supply/BAT0/charge_behaviour` | `[inhibit-charge] auto inhibit-charge-awake` while bypass is on |
| adaptive TDP | `systemctl status oxp3-adaptivetdp` | `inactive (dead)` after install (opt-in, not enabled) |
| | `sudo /usr/local/libexec/oxp3-adaptivetdp --once --dry-run --json` | one decision object, no writes, no `/var/log` entry |
| fan | `systemctl status oxp3-fan-curve` | active only if you enabled it; stopping it restores EC auto mode |
| | `/usr/local/libexec/oxp3-powerctl fan status` | `pwm1_enable=2` (auto) normally, `1` while the curve runs; `fan1_input` in rpm |
| IMU | `tools/oxp3-imu.py selftest` | `RESULT: PASS (0 warning(s)) - IMU usable, gyro exposed to Steam` |
| | `cat /sys/bus/iio/devices/iio:device0/in_anglvel_x_raw` | a changing raw value |
| audio | `pactl list sinks short \| grep oxp3_speaker_eq` | the `oxp3_speaker_eq.sink` line (after `eq-on.sh`) |
| | `tools/audio/eq_response.py` | the 8-band model table (max −1.6 dB, min −15.0 dB) |

## 6. Uninstall

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

# hardware fixes only:
sudo tools/oxp3-hw-install.sh --dry-run --uninstall --all   # review
sudo tools/oxp3-hw-install.sh --uninstall --all

# driver stack (also removes the LEDs/power units its install added):
./oxp3-fixes-fast.sh uninstall
```

Order: hardware first, driver stack second, then reboot. `--uninstall --all` keeps the user
configuration (`~/.config/oxp3-rgb.toml`, `/etc/oxp3-voltap.conf`, `/etc/oxp3/`); add
`--purge-config` to drop the two user/config files as well. What is *not* automatic: layered rpms
(`oxp3-firmware`, `kmod-oxp3`) are removed by `oxp3-fixes-fast.sh uninstall` / `rpm-ostree uninstall`;
the orchestrator warns if it sees them.

## 7. Rollback per component (manual, if you do not want the orchestrator)

| component | rollback |
|---|---|
| volume | `sudo tools/oxp3-voltap/install.sh uninstall; sudo rm -f /etc/oxp3-voltap.conf; sudo systemd-hwdb update; sudo udevadm trigger --subsystem-match=input --action=change` |
| LEDs / home button | `systemctl --user disable --now oxp3-homebtn; rm ~/.config/systemd/user/oxp3-homebtn.service; sudo rm -f /etc/udev/rules.d/90-oxp3-hid.rules; sudo udevadm control --reload-rules` (keep the rule if RGB uses it) |
| RGB | `systemctl --user disable --now oxp3-rgbd; rm ~/.config/systemd/user/oxp3-rgbd.service ~/.config/oxp3-rgb.toml; sudo rm -f /etc/udev/rules.d/90-oxp3-hid.rules; sudo udevadm control --reload-rules` |
| sleep | `sudo systemctl disable --now oxp3-sleep-wakeup oxp3-sleep-pre oxp3-sleep-log; sudo rm -f /etc/systemd/system/oxp3-sleep-*.service /usr/local/libexec/oxp3-sleep-* /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules; sudo systemctl daemon-reload; sudo udevadm control --reload-rules` — wake-up defaults return after a reboot |
| Bluetooth | `sudo tools/oxp3-hw-install.sh --uninstall --bluetooth` (removes the dracut conf and the files we added); if the `oxp3-firmware` rpm is layered, `./oxp3-fixes-fast.sh uninstall`. Stock = the radio dead again, which is the pre-fix state |
| power / fan / battery | `sudo /usr/local/libexec/oxp3-powerctl restore-baseline` **before** uninstalling, or just reboot (firmware re-initialises the RAPL limits); `sudo systemctl disable --now oxp3-fan-curve` (the tool restores EC auto mode on SIGTERM); `sudo tools/oxp3-power-install.sh uninstall`; `/etc/oxp3/` is kept |
| battery limit / bypass | `sudo /usr/local/libexec/oxp3-powerctl battery limit off` and `battery bypass off`; the EC keeps the last saved limit until changed, so do this before uninstalling |
| adaptive TDP | `sudo systemctl disable --now oxp3-adaptivetdp` (SIGTERM restores the preset that was active when it started) |
| IMU | `sudo rm -f /etc/modprobe.d/oxp3-bmi160.conf; sudo reboot` (or `sudo modprobe -r bmi260_i2c; sudo modprobe bmi160_i2c` to see the old misdetection again) |
| audio | `tools/audio/eq-off.sh` (removes the drop-in, restores the hardware speaker as default) |

## 8. Interaction with the driver installers

* `oxp3-fixes-fast.sh install` now runs `sudo tools/oxp3-hw-install.sh --leds --power` after its
  extras (and the matching `--uninstall`). `--skip-hw` skips it; `--skip-extras` does not.
* `oxp3-fixes.sh install` does the same and additionally installs the sleep units and the hhd
  volume patch — so for that script the orchestrator line only needs `--rgb --audio` (LEDs and the
  power units are already called by its extras).
* Re-running the orchestrator is safe: files are replaced only with identical content, units are
  re-enabled, the Bluetooth script skips files whose pinned hash already matches, and
  `oxp3-power-install.sh` re-installs the same units.
* The `--root DIR` mode installs into `DIR` (all paths prefixed, systemd/udev/modprobe skipped, the
  component installers that hard-code `/` are skipped with a message). It exists for offline
  validation, not for real use.

## 9. Validation performed (offline, no host changes)

* `bash -n` on `tools/oxp3-hw-install.sh`, both `oxp3-fixes*.sh`, `state-snapshot.sh`,
  `tools/oxp3-voltap/install.sh`, `tools/oxp3-power-install.sh`, `tools/oxp3-bt-firmware.sh`,
  `tools/audio/*.sh`, `tools/sleep-power-measure.sh`; `sh -n tools/oxp3-status.sh`.
* `python3 -m py_compile` on `oxp3_hid.py`, `oxp3-led.py`, `oxp3-homebtn.py`, `oxp3-rgbd.py`,
  `oxp3-imu.py`, `oxp3-powerctl.py`, `oxp3-adaptivetdp.py`, `tools/audio/*.py`.
* `systemd-analyze verify` on every unit in `files/` and `tools/` (system units with `ExecStart`
  stubbed to `/bin/true`, because the real `/usr/local/libexec/*` binaries only exist after install;
  user units after rendering the checkout path). Clean.
* `udevadm verify` on `tools/oxp3-hid.rules` and `files/99-oxp3-sleep-wakeup.rules`: 1 file each,
  Success 1, Fail 0.
* `systemd-hwdb --root=<tmp> update` on the volume hwdb: compiles to `hwdb.bin`.
* `tools/oxp3-hw-install.sh --dry-run --all`: prints the full command plan, exit 0, writes nothing.
* Simulated install into `--root /tmp/oxp3-sim` (create the directory first; sleep, leds, rgb, imu,
  audio), a second idempotent
  run, then `--uninstall`: the expected files appear, stay unchanged, and are removed; user config is
  kept. The component installers that write to `/` correctly report `[root-prefix] skipped`.
* `shellcheck` is not installed on this host; the scripts pass `bash -n` and the runtime paths above.

## 10. Open points

* The orchestrator cannot uninstall rpm-ostree layered packages (`oxp3-firmware`, `kmod-oxp3`); it
  warns and points at `oxp3-fixes-fast.sh uninstall`. Mixing the standalone Bluetooth script with the
  layered firmware package is harmless (same pinned files) but redundant.
* User units are per-user and reference the checkout path; moving the repo requires re-running
  `--leds`/`--rgb` (or editing `~/.config/systemd/user/*.service`).
* `systemctl --user` needs a live session: if you install over SSH without a seat, the units are
  installed but not started; logging in starts them (`WantedBy=default.target`).
* No EC fan-curve, no real bypass byte, no SAGV, no camera: those need kernel work, see
  [gap-audit.md](hw/gap-audit.md).
* `oxp3-fixes-fast.sh install` now depends on `tools/oxp3-hw-install.sh` being present and executable;
  `--skip-hw` or `OXP3_SKIP_HW` is the escape hatch if it is not.
