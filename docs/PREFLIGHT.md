# Preflight — refuse a half-install and say exactly what to fix

Date: **2026-11-09**. Scope: `tools/oxp3-preflight.sh`, `tools/oxp3_preflight.py`,
`tests/unit/test_preflight.py`, `tests/fixtures/preflight/`, the `preflight` suite in
`tools/repo-test.sh`, and the wiring into the installer, verifier, wizard and first-run
checklist.

The hardware installers write to `/etc`, `/usr/local`, udev, polkit, logind, dracut and
the invoking user's systemd instance. If one prerequisite is missing — no Python evdev,
an old systemd, no `/dev/uinput`, an already-applied hhd volume patch — the command
still creates *some* files and leaves a component that cannot work. That is the
half-install this tool refuses.

It is **strictly read-only**: no file write, no unit start/stop, no `udevadm` reload, no
sysfs write, no package manager, no network (except the read-only journal/status queries
of local tools). Every privileged repair is a printed `fix:` command.

```sh
tools/oxp3-preflight.sh                 # OK/WARN/FAIL per prerequisite + verdict
tools/oxp3-preflight.sh --json          # schema 1 report for machines
tools/oxp3-preflight.sh --quiet         # only problems + verdict
tools/oxp3-preflight.sh --strict        # exit 1 while any WARN remains
tools/oxp3-preflight.sh --component volume
tools/oxp3-preflight.sh --user holzr    # whose session/units are checked (default: SUDO_USER)
tools/oxp3-preflight.sh --selftest      # parser checks + seven fixture verdicts
```

Exit codes: `0` safe, `1` unsafe (or `--strict` with a warning), `2` usage or unknown
component, `3` the probe engine could not start. The installer calls it with `--quiet`
for the selected components and aborts on `1` unless `--force`.

## 1. Deriving the prerequisites (what the installers actually need)

Every check comes from an installer or unit in this repository, not from a generic
checklist:

| source | derived prerequisite |
|---|---|
| `tools/oxp3-hw-install.sh` | root for every component except `--audio`; a non-root owner for the user units; a checkout path without whitespace; `install(1)`, `systemctl`, `udevadm`, `runuser`; the target directories |
| `tools/oxp3-voltap/install.sh`, `tools/oxp3-voltap` | `python3` + `evdev`, `systemd-hwdb`, `/dev/input/event*`, `/dev/uinput` |
| `tools/oxp3-homebtn.py`, `tools/oxp3-rgbd.py` | the 1a86:fe00 vendor hidraw node, `/dev/uinput` (home button), `tomllib`/Python 3.11+ (RGB), a user manager bus |
| `files/logind-oxp3.conf`, `files/49-oxp3-suspend.rules` | `/etc/systemd/logind.conf.d`, `/etc/polkit-1/rules.d`, a polkit agent for non-local callers, no conflicting `Handle*` policy |
| `tools/oxp3-bt-firmware.sh`, `tools/oxp3-power-install.sh` | `dracut` or `rpm-ostree`, curl/wget (or a warm cache), RAPL zones, `oxp-platform` hwmon |
| `tools/oxp3-powerctl.py`, `tools/oxp3-adaptivetdp.py` | `python3`, `intel-rapl:*`, the battery charge interfaces |
| `files/*.service`, `tools/*.service` | systemd >= 247 (`ProtectProc=invisible`), user units, `Restart=` in `[Service]` |
| `tools/oxp3-flatpak-xmx.sh`, `tools/lib/install-common.sh` | `flatpak`, and no override/env/user-unit pointing at an older checkout |
| `files/oxp3-bmi160.conf` | `CONFIG_BMI260` (Anatase custom) and `/etc/modprobe.d` |
| the hhd volume patches (`patches/hhd/`) | hhd >= 4.1.12, and **not** both the hhd patch and `oxp3-voltap` |

Kernel options are read from `/boot/config-*` when present and from
`/usr/lib/modules/<release>/config` (the Anatase path) otherwise. If neither is
readable the individual kernel rows say "not asserted" and one `kernel.config` WARN
points at the cause.

## 2. States, verdict and skipped components

| state | meaning | effect |
|---|---|---|
| `OK` | prerequisite satisfied | component can install |
| `WARN` | a degraded path the inline installers already handle (no user bus → unit starts at next login; missing logind drop-in → `--sleep` creates it), a diagnostics-only tool (`kreadconfig6`, `flatpak`, `btmgmt`), an optional device (`spidev`), or a half-install a re-run completes | component still installs; the installer prints it and continues |
| `FAIL` | a true blocker: a required tool/directory/device is absent, systemd is too old, a kernel option is off, or two implementations of one feature are active | the installer aborts; that component is listed as **skipped** |

Verdict:

* **SAFE** — zero `FAIL`. Warnings are listed; the install completes, possibly with a
  documented degraded path.
* **UNSAFE** — at least one `FAIL`. `skipped_components` names the paths that would not
  work. Nothing is written by the preflight itself; the installer refuses with exit 1.
* `--strict` keeps `safe` true but exits 1 while a warning remains, for release screens.

A component is *blocked* when a `FAIL` check applies to it (explicitly, or a universal
`*` blocker such as a missing systemd). A universal *warning* (for example a missing
`spidev` that only the future fingerprint work needs) never degrades every component.

## 3. Check inventory and remediation

`tools/oxp3-preflight.sh` declares 71 checks in eight groups. The id is stable and is
what `--json` reports.

### core

| check | verifies | fix |
|---|---|---|
| `core.root` | `EUID == 0`; otherwise a WARN asking for sudo (the installer re-runs it as root) | `sudo tools/oxp3-hw-install.sh <components>` |
| `core.user` | a non-root user exists for the user-scope half | run from the desktop session, or `--user NAME` |
| `core.repo-path` | no whitespace in the checkout path (unquoted `ExecStart`) | move the checkout |
| `core.repo` | 10 required installer/doc files exist | re-clone/restore the checkout |
| `core.sudo` | root, or `sudo` on PATH | run as root or install sudo |

### directories (all are created by `install -D`; a missing one is a WARN, except `/etc/systemd/system`)

| check | directory | fix |
|---|---|---|
| `dir.system-units` | `/etc/systemd/system` (FAIL if missing) | `sudo install -d -m 0755 /etc/systemd/system`; check systemd is installed |
| `dir.udev-rules` | `/etc/udev/rules.d` | `sudo install -d -m 0755 /etc/udev/rules.d` |
| `dir.udev-hwdb` | `/etc/udev/hwdb.d` | `sudo install -d -m 0755 /etc/udev/hwdb.d` |
| `dir.polkit-rules` | `/etc/polkit-1/rules.d` | `sudo install -d -m 0755 /etc/polkit-1/rules.d` |
| `dir.logind-conf-d` | `/etc/systemd/logind.conf.d` | `sudo install -d -m 0755 /etc/systemd/logind.conf.d` |
| `dir.local-libexec` | `/usr/local/libexec` | `sudo install -d -m 0755 /usr/local/libexec` |
| `dir.dracut-conf-d` | `/etc/dracut.conf.d` | `sudo install -d -m 0755 /etc/dracut.conf.d` |
| `dir.modprobe-d` | `/etc/modprobe.d` | `sudo install -d -m 0755 /etc/modprobe.d` |
| `dir.user-systemd` | `~/.config/systemd/user` | `install -d -m 0755 ~/.config/systemd/user` |
| `dir.pipewire` | `~/.config/pipewire/pipewire.conf.d` | `install -d -m 0755 ~/.config/pipewire/pipewire.conf.d` |

### systemd

| check | verifies | fix |
|---|---|---|
| `systemd.present` | `systemctl --version` works | this must be a systemd host |
| `systemd.version` | >= 247 (`ProtectProc=invisible`) | update the OS, or install the units by hand without the sandboxing options |
| `systemd.analyze` | `systemd-analyze` present | install the systemd tools; the verifier/documentation use it |
| `systemd.user-bus` | `/run/user/<uid>/bus` exists (WARN: unit starts at next login) | log in on the device desktop |
| `systemd.restart` | every shipped `Restart=` is inside `[Service]` | move it into `[Service]`; systemd ignores it in `[Unit]` |

### tools

| check | tool | fix |
|---|---|---|
| `tool.install` | `install(1)` | install coreutils |
| `tool.systemctl` | `systemctl` | install systemd |
| `tool.udevadm` | `udevadm` | install systemd-udev |
| `tool.systemd-hwdb` | `systemd-hwdb` | install systemd (the volume hwdb must compile) |
| `tool.python3` | `python3` | install python3 (daemons + powerctl) |
| `tool.runuser` | `runuser` (only when root) | install util-linux |
| `tool.dracut` | `dracut` or `rpm-ostree` | install dracut / use the ostree path |
| `tool.downloader` | curl/wget **or** `~/.cache/oxp3-bt-firmware` files | install curl, or pre-download and pass `--bt-from DIR` |
| `tool.evdev` | `python3 -c 'import evdev'` | `sudo rpm-ostree install python3-evdev` |
| `tool.tomllib` | Python >= 3.11 TOML parser | install Python >= 3.11 |
| `tool.pipewire`, `tool.wireplumber`, `tool.pactl` | the audio stack (WARN; the drop-in still installs) | install pipewire / wireplumber / pulseaudio-utils |
| `tool.kreadconfig6` | KDE power config reader (WARN, diagnostics only) | install kf6-kconfig |
| `tool.btmgmt` | Bluetooth controller verification (WARN) | install bluez |
| `tool.flatpak` | Flatpak XMX path (WARN) | install flatpak |
| `tool.modprobe` | kmod (WARN) | install kmod |
| `tool.polkit-agent` | a session polkit agent (WARN; our rule covers the local active wheel seat) | install/start `polkit-kde-authentication-agent-1` |

### devices

| check | verifies | fix |
|---|---|---|
| `dev.hidraw` | `/dev/hidraw*` exists (WARN) | check `lsusb -d 1a86:fe00`; the rule applies when it appears |
| `dev.vendor-hid` | the 1a86:fe00 node with **no input child** and usage page 0xFF00 | same; the daemons wait |
| `dev.input-events` | `/dev/input/event*` (FAIL: the volume daemon has nothing to grab) | check `CONFIG_INPUT_EVDEV` and udev |
| `dev.uinput` | `/dev/uinput` (FAIL for volume + home button) | `sudo modprobe uinput`; enable `CONFIG_INPUT_UINPUT` |
| `dev.spidev` | `/dev/spidev*` (WARN; future fingerprint work) | informational |
| `dev.rapl` | `intel-rapl:<n>` zones (FAIL for power) | check `CONFIG_INTEL_RAPL` / BIOS |
| `dev.hwmon-oxp` | `oxp-platform` hwmon (WARN; fan curve is opt-in) | check the `oxp_ec` driver |
| `dev.battery` | a charge-limit interface (WARN) | battery care is optional |
| `dev.sched-ext` | `/sys/kernel/sched_ext/state` (WARN; opt-in scheduler) | enable `CONFIG_SCHED_CLASS_EXT` |

### kernel (read from `/boot/config-*` or `/usr/lib/modules/*/config`)

| check | option | fix |
|---|---|---|
| `kernel.config` | a readable config (WARN if absent) | install `kernel-core` or mount `/boot` |
| `kernel.hidraw` | `CONFIG_HIDRAW` (FAIL for LEDs/RGB) | enable it in the kernel |
| `kernel.uinput` | `CONFIG_INPUT_UINPUT` (FAIL for volume/home button) | enable it |
| `kernel.rapl` | `CONFIG_INTEL_RAPL` + `_CORE` (FAIL for power) | enable them |
| `kernel.bmi260` | `CONFIG_BMI260` (WARN; Anatase custom) | use the Anatase kernel |
| `kernel.sched-ext` | `CONFIG_SCHED_CLASS_EXT` (WARN) | enable it |
| `kernel.btintel` | `CONFIG_BT_INTEL_PCIE` (WARN) | check the Bluetooth module |

### state (half-install detection)

| check | verifies | fix |
|---|---|---|
| `state.volume` | 4 artefacts: 0 = not installed, all = installed, between = half | `sudo tools/oxp3-hw-install.sh --volume` |
| `state.leds` | udev rule + user unit | `--leds` |
| `state.rgb` | rule + config + user unit | `--rgb` |
| `state.sleep` | 8 units/rules/drop-ins | `--sleep` |
| `state.bluetooth` | firmware 2/2 + dracut drop-in | `--bluetooth` (the firmware alone is a half-install) |
| `state.power` | powerctl + profile/resume units | `--power` |
| `state.imu` | modprobe blacklist | `--imu` |
| `state.audio` | EQ drop-in | `--audio` |
| `state.bt-initramfs` | firmware present without the dracut drop-in, or a load failure this boot | `sudo tools/oxp3-bt-firmware.sh`, then reboot |
| `state.ostree-staged` | a staged rpm-ostree deployment (WARN: overlay writes would be lost) | reboot into it first |

### conflicts

| check | verifies | fix |
|---|---|---|
| `conflict.hhd` | hhd active, version and what it owns (volume keyboard, RGB plugin, grab state) | `sudo systemctl enable --now hhd`, or use the standalone path |
| `conflict.hhd-version` | hhd >= 4.1.12 | update hhd or use `oxp3-voltap` |
| `conflict.volume-path` | **both** the hhd patch and `oxp3-voltap` installed (FAIL) | pick one: `sudo tools/oxp3-voltap/install.sh uninstall` **or** remove the hhd patch |
| `conflict.logind` | another logind drop-in already sets `Handle*`, or ours is overridden | keep one policy, then `sudo systemctl restart systemd-logind` |
| `conflict.polkit` | `49-oxp3-suspend.rules` exists with different content | `sudo diff -u files/49-oxp3-suspend.rules /etc/polkit-1/rules.d/49-oxp3-suspend.rules` |
| `conflict.flatpak` | a Flatpak override/env/user unit references another checkout | re-run the installers from this checkout |
| `conflict.stale-units` | an installed `oxp3-*.service` points at another checkout | `--leds --rgb` re-renders them |
| `conflict.rapl-rule` | the legacy `70-*rapl.rules` is still present | `./oxp3-fixes-fast.sh install` removes it |

## 4. Wiring

* **Installer** (`tools/oxp3-hw-install.sh`): before a real install it runs
  `tools/oxp3-preflight.sh --quiet --component <selected…> --user <target>`; `FAIL`
  aborts with exit 1 unless `--force`; `WARN` prints and continues. Dry-run, `--root`
  and `--uninstall` skip it (they must not depend on the live host).
* **Verifier**: `preflight.verdict` runs `tools/oxp3-preflight.sh --json` and records
  `PASS` for `safe: true`, `FAIL` for `safe: false`, `SKIP` if the tool is absent.
* **Wizard**: the Preflight phase runs the tool as a read-only item.
* **First-run checklist**: a `preflight` row (`ok` / `partial` on warnings / `todo` on
  blockers) uses the same JSON. Fixture mode never calls the live host.
* **Repo test**: the `preflight` suite runs `--selftest` (parser + seven fixtures) and
  checks a live `--json` is valid schema 1.

## 5. Tests

* `tools/oxp3-preflight.sh --selftest` — 25 parser/probe assertions and seven fixture
  verdicts: sane host, missing directory, old systemd, missing evdev, conflicting volume
  daemon, Bluetooth half-install, unprivileged host.
* `python3 -m unittest discover -s tests/unit -p 'test_preflight.py' -q` — the same
  parsers and fixtures through `FixtureHost` (no subprocess, no host).
* `tools/repo-test.sh --fast --only preflight` — the suite.

## 6. Recorded state on this host (2026-11-09)

See [`docs/state/preflight-2026-11-09.txt`](state/preflight-2026-11-09.txt). Summary:
**SAFE, 0 blockers, 5 warnings**; 66 OK. The warnings are the honest stock state:

* `core.root` — the snapshot was taken as the user, not root.
* `dir.logind-conf-d` — the drop-in directory does not exist yet (`--sleep` creates it).
* `dev.spidev` — the fingerprint reader's SPI device is not bound; nothing installs it.
* `state.bluetooth` — the two firmware files are present but the dracut drop-in is not
  (`2/3`): the driver stack shipped the blobs, the initramfs step never ran.
* `state.bt-initramfs` — this boot logged `Failed to load Intel firmware file`, the
  documented stale-initramfs defect.

It also reports hhd 4.1.12 active (volume keyboard + RGB plugin), 25 input event nodes,
`/dev/uinput`, `/dev/hidraw4` as the vendor collection, systemd 262, RAPL both zones,
and that `volume/leds/rgb/sleep/power` are not installed while `imu` and `audio` are
(their two artefacts are the only hardware-fix files this host already has).

## 7. Open points

* `core.root` is a WARN, not a FAIL: the tool is also run before `sudo`, and the
  installer re-runs it as root. A direct `sudo tools/oxp3-preflight.sh` turns it OK;
  `--strict` is the release gate if you want a non-root run to fail.
* The hhd grab state needs root (`/proc/<pid>/fdinfo`); an unprivileged run reports it
  as unknown instead of guessing.
* `state.bluetooth` counts the two firmware blobs as part of the installer's output even
  when a driver-stack package put them there. The detail names the missing dracut
  drop-in, and `state.bt-initramfs` is the row that proves the boot-time effect.
* The fingerprint path is only the `dev.spidev` warning today: nothing is installed, so
  a missing SPI device cannot block an install.
