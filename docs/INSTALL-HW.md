# Installing the OneXPlayer 3 hardware fixes — one guide

This is the single install guide for the **hardware** work in this repository: volume rocker, home
button + top LEDs, body RGB, suspend/resume wake policy, Bluetooth firmware, power envelope (PL1 /
PL2 / PL4), fan, battery charge limit / charge-inhibit ("bypass"), adaptive TDP, IMU blacklist and
the speaker EQ, plus the optional Decky status plugin. The driver stack (kernel modules, Mesa,
gamescope, XeSS, MangoHud, firmware RPMs) stays in
[`oxp3-fixes-fast.sh`](../oxp3-fixes-fast.sh) / [`oxp3-fixes.sh`](../oxp3-fixes.sh).

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
| volume | `tools/hhd-oxp3-voltap/install.sh` (**hhd plugin**, supported) + force-release hwdb; `--volume-daemon` uses the legacy `tools/oxp3-voltap/install.sh` daemon | root (`/usr/local`, `/etc`) | yes |
| LEDs / home button | orchestrator + `tools/oxp3-hid.rules`, `tools/oxp3-homebtn.service` | root rule + user unit | rule: yes, unit: yes |
| sleep | orchestrator + `files/oxp3-sleep-*` | root (`/etc`, `/usr/local`) | yes |
| Bluetooth | `tools/oxp3-bt-firmware.sh` | root (`/usr/lib/firmware/updates`) | files no, dracut conf yes |
| power / fan | `tools/oxp3-power-install.sh` | root (`/etc`, `/usr/local`) | yes |
| RGB | orchestrator + `tools/oxp3-hid.rules`, `tools/oxp3-rgbd.*` | root rule + user config/unit | yes |
| IMU | orchestrator + `files/oxp3-bmi160.conf` | root (`/etc/modprobe.d`) | yes |
| audio / EQ | `tools/audio/install-user.sh` | user (`~/.config/pipewire`) | yes |
| Decky plugin | `tools/lib/decky_plugin.py` + `tools/decky-oxp3-status/` | root (the Decky plugins directory is root-owned) | plugin: yes, reinstalled after the next Decky update |

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
* The installer runs [`tools/oxp3-preflight.sh`](../tools/oxp3-preflight.sh) for the selected
  components first: it proves every prerequisite (root, systemd, directories, tools, device
  nodes, kernel options, half-installs and conflicting daemons), refuses on a true blocker and
  prints the exact `fix:` command. Everything it checks, and why, is in
  [PREFLIGHT.md](PREFLIGHT.md).

## 2. The ordered command set

### 2.1 Full stack on a stock Anatase (recommended)

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

# 0. read-only: would an install complete, and what is missing? (no root needed)
tools/oxp3-preflight.sh

# 1. driver stack (kernel modules, firmware package, Mesa, gamescope, MangoHud, XeSS).
#    build as your user; install asks for sudo; after a kernel change run install twice
#    (the first run stages modules/firmware and asks for a reboot).
./oxp3-fixes-fast.sh build
./oxp3-fixes-fast.sh install

# 2. hardware fixes: one command. This adds what the driver installer does not cover:
#    sleep units, RGB keeper + config, speaker EQ, and (re)asserts LEDs + power units.
sudo tools/oxp3-hw-install.sh --sleep --leds --power --rgb --audio

# 2b. Decky status plugin (builds with node/npm, else reuses a zip verified against
#     tools/decky-oxp3-status/artifact.sha256; restarts no service unless asked).
sudo tools/oxp3-hw-install.sh --decky
#    then, so Decky discovers it (or Decky -> Settings -> Developer -> Reload plugins):
sudo systemctl restart plugin_loader.service

# 3. state report + verification (see section 5)
tools/oxp3-hw-install.sh --status
tools/oxp3-status.sh
```

`--volume` and `--bluetooth` are omitted above on purpose: `oxp3-fixes-fast.sh` installs the hhd
volume-rocker patch and layers the Bluetooth firmware package. The supported volume path is the
**hhd plugin** (`--volume`, the default), which replaces the hhd patch's tap policy at runtime; the
legacy `--volume-daemon` daemon is an *alternative* to the plugin, not an addition to it. A plugin
change needs `sudo systemctl restart hhd`, which drops the emulated Steam controller for a few
seconds ([volume-buttons.md](hw/volume-buttons.md), [DAEMONS.md §0](DAEMONS.md)).

### 2.2 Hardware fixes only (no driver stack)

```bash
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

sudo tools/oxp3-hw-install.sh --dry-run --all    # review the exact commands first
sudo tools/oxp3-hw-install.sh --all              # volume sleep bluetooth power imu leds rgb audio decky
```

`--all` includes the hhd volume plugin (the supported volume path), the standalone
Bluetooth-firmware script and the Decky status plugin.

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
sudo tools/oxp3-hw-install.sh --decky                                # rebuild the plugin for this checkout
```

### 2.5 After the reboot: one command for the whole evidence set

```bash
sudo systemctl restart systemd-logind   # the sleep drop-in is read at logind start
sudo systemctl reboot                   # Bluetooth initramfs + a clean pre/post comparison
sudo systemctl suspend                  # once, yourself: the only step that suspends
tools/oxp3-firstrun.sh --collect         # checklist + redacted evidence bundle
```

`tools/oxp3-firstrun.sh` prints the ordered checklist with the live state of every step and,
with `--collect`, writes `docs/state/firstrun-<timestamp>/` (verifier, status, units, udev,
firmware hashes, power, sleep, audio, display, XMX, Decky, bounded journals) for hand-back. It
is read-only and never runs the privileged steps above. The bundle format, the redaction rules
and the reading order are in [FIRSTRUN.md](FIRSTRUN.md).

## 3. Per-component opt-out

`--all` is just the union of the flags; to opt out, list the components you want instead.
Every flag is repeatable and idempotent.

| flag | installs | remove with |
|---|---|---|
| `--volume` | the **hhd plugin** (`/usr/local/lib/python3.15/site-packages/oxp3_hhd_voltap` + dist-info) and the force-release hwdb; after install `sudo systemctl restart hhd` (drops 28DE:12FF for a few seconds). `--volume-daemon` installs the legacy `oxp3-voltap` daemon + `/etc/oxp3-voltap.conf` + unit instead | `--uninstall --volume` |
| `--leds` | `90-oxp3-hid.rules`, user unit `oxp3-homebtn.service` | `--uninstall --leds` |
| `--sleep` | `oxp3-sleep-wakeup/pre/log` units + `/usr/local/libexec/oxp3-sleep-*` + `99-oxp3-sleep-wakeup.rules` + the trigger policy (`/etc/systemd/logind.conf.d/60-oxp3-sleep.conf`, `/etc/polkit-1/rules.d/49-oxp3-suspend.rules`); after installing, `sudo systemctl restart systemd-logind` (or reboot) so logind reads the drop-in | `--uninstall --sleep` |
| `--bluetooth` | upstream firmware in `/usr/lib/firmware/updates/intel`, dracut `install_items`, controller reload | `--uninstall --bluetooth` |
| `--power` | `oxp3-powerctl`, boot/resume units; `--fan-curve`, `--adaptive-tdp` add the opt-in units | `--uninstall --power` |
| `--rgb` | shared HID rule, `~/.config/oxp3-rgb.toml` (if absent), user unit `oxp3-rgbd.service` | `--uninstall --rgb [--purge-config]` |
| `--imu` | `/etc/modprobe.d/oxp3-bmi160.conf` (keeps BMI260 bound; read-only `oxp3-imu.py` needs no install) | `--uninstall --imu` |
| `--audio` | `~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf` | `--uninstall --audio` |
| `--decky` | the OXP3 Status plugin under `~/homebrew/plugins/oxp3-status` with an `.oxp3-managed` marker; the previous copy is backed up first | `--uninstall --decky` |

Other useful switches: `--dry-run` (print every command, change nothing), `--status` (read-only
matrix), `--root DIR` (offline simulation), `--user NAME` (whose user-scope files are written;
default `SUDO_USER`), `--bt-persist=items|omit|none`, `--bt-with-ddc`, `--bt-no-reload`,
`--bt-from DIR`, `--imu-reload`, `--purge-config`, `--decky-artifact PATH` (install a specific
plugin zip, verified against the recorded sha256), `--restart-plugin-loader` (restart
`plugin_loader.service` after `--decky`; never automatic, and only when the unit exists).

Known interaction: the hhd plugins must be loaded one way only. The plugin **replaces** the hhd
volume-tap policy per producer instance, so an already-applied `oxp3-fixes*.sh` hhd patch is
harmless; but do not also run the legacy daemon (`--volume-daemon`) while the plugin is installed -
the daemon would only observe (exit 4), so it adds nothing. `--uninstall --volume` removes the
plugin and any leftover legacy daemon together.

## 4. What gets written where

```
/usr/local/libexec/oxp3-voltap          /etc/systemd/system/oxp3-voltap.service
/usr/local/libexec/oxp3-sleep-wakeup    /etc/systemd/system/oxp3-sleep-{wakeup,pre,log}.service
/usr/local/libexec/oxp3-sleep-log       /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules
                                        /etc/systemd/logind.conf.d/60-oxp3-sleep.conf
                                        /etc/polkit-1/rules.d/49-oxp3-suspend.rules
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
~/.local/state/oxp3-fixes/decky-backups/<UTC stamp>/oxp3-status   (previous plugin copies)

~/homebrew/plugins/oxp3-status/          (root-owned: plugin.json, main.py, dist/, .oxp3-managed)
```

`/etc/oxp3/` (power profile, baseline, fan curve) and `/var/log/oxp3-adaptivetdp.jsonl` are created
by the tools when you save/run them, not by the installer.

## 5. Verification matrix

Run everything as your user unless marked root. `sudo` is only needed where shown.

<!-- BEGIN GENERATED: install-verify (source: docs/hw/components.yaml; regenerate: tools/gen-docs.py --write) -->
Start with the whole-system checks; then the per-component rows.

| Whole-system check | Command | Expected |
|---|---|---|
| Install state | `tools/oxp3-hw-install.sh --status` | one line per component; yes and enabled/active for what is installed; exit 0 |
| Verification harness | `tools/oxp3-verify.sh --json` | JSON report; exit 0 with at least one PASS, 1 on any FAIL, 2 when nothing passed, 3 on usage error |
| Repo status matrix | `tools/oxp3-status.sh --json` | schema 1 report with per-indicator ok/warn/fail/unknown and an overall state |
| Harness self-test | `tests/verify/run.sh` | every parser asserted against tests/verify/fixtures; exit 0 |
| Preflight validator | `tools/oxp3-preflight.sh` | OK/WARN/FAIL per prerequisite, a safe/unsafe verdict and the components that would be skipped; exit 0 safe, 1 unsafe |

| Component | Install |
|---|---|
| Volume keys | `sudo tools/oxp3-hw-install.sh --volume` |
| Home button and top LEDs | `sudo tools/oxp3-hw-install.sh --leds` |
| Body RGB zones | `sudo tools/oxp3-hw-install.sh --rgb` |
| Daemon lifecycle (suspend/hotplug) | `sudo tools/oxp3-hw-install.sh --all` |
| Suspend and wake policy | `sudo tools/oxp3-hw-install.sh --sleep` |
| Speaker tuning (Harman APO) and gain staging | `tools/oxp3-hw-install.sh --audio --audio-activate` |
| Internal microphone (ALC245 digital mic) | `sudo tools/audio/internal-mic.sh install` |
| Bluetooth firmware | `sudo tools/oxp3-hw-install.sh --bluetooth` |
| Power envelope (PL1/PL2/PL4) | `sudo tools/oxp3-hw-install.sh --power --power-profile balanced` |
| Fan curve | `sudo tools/oxp3-hw-install.sh --power --fan-curve` |
| Battery care | `sudo tools/oxp3-hw-install.sh --power --battery-limit 80 --battery-bypass on` |
| Adaptive TDP | `sudo tools/oxp3-hw-install.sh --power --adaptive-tdp` |
| SAGV / memory frequency | nothing is installed; the probe is read-only and needs no root |
| Display HDR / vibrance / colour | user-scope status/HDR/ICC/vibrance and per-game launch composition; the Gamemode flag is the only privileged part |
| IMU / gyro | nothing to install; --imu only writes the modprobe snippet that keeps BMI260 bound |
| Camera / IPU7 | nothing; not applicable to this unit |

Manual install steps, opt-out flags, uninstall and per-component rollback are in sections 2, 3, 6 and 7 below; the machine-readable form is [`docs/hw/REGISTRY.md`](hw/REGISTRY.md).

| Component | State | Verify | Expected | Root |
|---|---|---|---|---|
| Volume keys | awaiting-install | `hhd.unit` | hhd is installed and active | no |
| Volume keys | awaiting-install | `hhd.busy` | no unrecovered EBUSY/disconnect episode in this boot's hhd journal | no |
| Volume keys | awaiting-install | `hhd.grab` | no process of ours holds an fd or EVIOCGRAB on an hhd event device | no |
| Volume keys | awaiting-install | `vol.unit` | oxp3-voltap active, or installed-but-disabled on purpose (plugin path) | no |
| Volume keys | awaiting-install | `vol.grab` | a non-hhd device is grabbed, or the documented OBSERVE line (hhd owns the path) | no |
| Volume keys | awaiting-install | `vol.topology` | the free physical AT keyboard, or protected/observe (exit 4); never hhd's device | no |
| Volume keys | awaiting-install | `vol.holder` | the physical AT keyboard is held only by hhd (or by nobody); SKIP without root | no |
| Volume keys | awaiting-install | `vol.hwdb` | the force-release entry exists and hwdb.bin is newer | no |
| Volume keys | awaiting-install | `vol.hold` | a hold yields exactly one step, not a ramp | yes |
| Home button and top LEDs | awaiting-install | `hid.rule` | /etc/udev/rules.d/90-oxp3-hid.rules present | no |
| Home button and top LEDs | awaiting-install | `hid.access` | the vendor hidraw node is writable by the invoking user | no |
| Home button and top LEDs | awaiting-install | `hid.node` | the selected node is the one whose report descriptor claims usage page 0xFF00 | no |
| Home button and top LEDs | awaiting-install | `led.status` | oxp3-led.py status lists the device, zones and presets | no |
| Home button and top LEDs | awaiting-install | `led.zones` | all 5 zones and 11 presets are addressable | no |
| Home button and top LEDs | awaiting-install | `led.sides` | empty --sides encodes 0 frames and a real selection still encodes them (dry-run) | no |
| Home button and top LEDs | awaiting-install | `homebtn.unit` | oxp3-homebtn.service is active (running) | no |
| Home button and top LEDs | awaiting-install | `homebtn.mapping` | a press becomes KEY_HOMEPAGE | no |
| Home button and top LEDs | awaiting-install | `homebtn.press` | this boot's user journal has listening and pressed lines | no |
| Home button and top LEDs | awaiting-install | `led.write` | one solid frame is accepted by the device | no |
| Body RGB zones | awaiting-install | `rgbd.unit` | oxp3-rgbd.service active and ~/.config/oxp3-rgb.toml present | no |
| Body RGB zones | awaiting-install | `rgbd.check` | PASS config parses (5 zones, N frames) | no |
| Body RGB zones | awaiting-install | `rgbd.timing` | the hhd journal is readable (the settle/debounce watcher path) and the example config encodes frames | no |
| Daemon lifecycle (suspend/hotplug) | awaiting-install | `life.fd` | every inspectable active daemon holds its device fd (the root daemon degrades to SKIP) | no |
| Daemon lifecycle (suspend/hotplug) | awaiting-install | `life.reconnect` | this boot's journal has no device loss without a later successful open | no |
| Daemon lifecycle (suspend/hotplug) | awaiting-install | `life.sim` | the offline reconnect/backoff/suspend suite passes | no |
| Suspend and wake policy | awaiting-install | `sleep.mem` | /sys/power/mem_sleep selects [s2idle] | no |
| Suspend and wake policy | awaiting-install | `sleep.units` | oxp3-sleep-wakeup is active and reports the pre/log units | no |
| Suspend and wake policy | awaiting-install | `sleep.wakeup` | every listed USB id / PCI address has wakeup disabled | no |
| Suspend and wake policy | awaiting-install | `sleep.trigger` | logind drop-in parsed with HandlePowerKey/SuspendKey=suspend, polkit rule present, pkcheck says yes, no sleep block inhibitor | no |
| Suspend and wake policy | awaiting-install | `sleep.audit` | the capability table and the suspend record block | no |
| Suspend and wake policy | awaiting-install | `sleep.stats` | the counter tuple (success/fail/last_hw_sleep/total_hw_sleep/max_hw_sleep); PASS on a completed cycle, FAIL on a failed attempt or an abort, UNKNOWN (never PASS/FAIL) when success=0 meets a non-zero hardware-sleep number - max_hw_sleep is a counter ceiling, not a duration | no |
| Suspend and wake policy | awaiting-install | `sleep.mmcwake` | no armed mmc wakeup bit; the measured mmc0 is a virtual source (no device link, wakeup_count=0) and is parked with the SD host | no |
| Suspend and wake policy | awaiting-install | `sleep.sdcard` | the SD host is bound while awake and parked only across suspend; a leftover park (unbound, or state says unbound) fails, because the reader would be dead | no |
| Suspend and wake policy | awaiting-install | `sleep.aspm` | pcie_aspm.policy selects powersave (the storage link is allowed to idle) | no |
| Suspend and wake policy | awaiting-install | `sleep.nvmeacpi` | PASS in `tools/oxp3-verify.sh --json` | no |
| Suspend and wake policy | awaiting-install | `sleep.nvme` | nvme_core.default_ps_max_latency_us > 0 and force_apst != Y, so APST is not disabled | no |
| Suspend and wake policy | awaiting-install | `sleep.measure` | PASS at or below 1 %/h, WARN 1-2 %/h, FAIL above 2 %/h | no |
| Suspend and wake policy | awaiting-install | `sleep.suspend` | one real s2idle cycle (rtcwake) with suspend_stats/success incremented; needs --full --with-changes and OXP3_ALLOW_SUSPEND=1 | yes |
| Speaker tuning (Harman APO) and gain staging | working | `aud.scripts` | eq-on.sh and eq-off.sh are executable | no |
| Speaker tuning (Harman APO) and gain staging | working | `aud.server` | an audio session answers pactl info | no |
| Speaker tuning (Harman APO) and gain staging | working | `aud.eq` | oxp3_speaker_eq.sink is loaded | no |
| Speaker tuning (Harman APO) and gain staging | working | `aud.dummy` | the default sink is real, not auto_null | no |
| Speaker tuning (Harman APO) and gain staging | working | `aud.model` | the 8-band model table (loud profile, pre-gain -4 dB) | no |
| Speaker tuning (Harman APO) and gain staging | working | `aud.level` | the ALSA speaker control is at maximum and the report gives the net headroom | no |
| Speaker tuning (Harman APO) and gain staging | working | `aud.activate` | eq-on.sh activates the sink | no |
| Internal microphone (ALC245 digital mic) | partial | `aud.mic` | the ALC245 pin 0x12 path is exposed as an always-available capture source (SKIP while the opt-in profile is not installed) | no |
| Bluetooth firmware | awaiting-install | `bt.files` | both ibt-00a0-0291 iml/pci files in updates/intel | no |
| Bluetooth firmware | awaiting-install | `bt.dracut` | /etc/dracut.conf.d/60-oxp3-bt-firmware.conf present | no |
| Bluetooth firmware | awaiting-install | `bt.journal` | no 'Failed to load Intel firmware file' this boot | no |
| Bluetooth firmware | awaiting-install | `bt.controller` | btmgmt sees a controller address | no |
| Bluetooth firmware | awaiting-install | `bt.diag` | pinned hashes match and this boot loaded the firmware | no |
| Power envelope (PL1/PL2/PL4) | working | `pwr.zones` | both intel-rapl:0 and intel-rapl-mmio:0 report PL1/PL2/PL4 | no |
| Power envelope (PL1/PL2/PL4) | working | `pwr.pl1` | MSR PL1 equals MMIO PL1 (tdp-sync invariant) | no |
| Power envelope (PL1/PL2/PL4) | working | `pwr.profile` | the saved profile maps to the live platform_profile | no |
| Power envelope (PL1/PL2/PL4) | working | `pwr.order` | a dry-run profile switch orders raises ceiling-first and puts every lowering last | no |
| Fan curve | partial | `pwr.fan` | fan1_input 1-12000 rpm and pwm1_enable 1 or 2 | no |
| Fan curve | partial | `fan.curvedup` | a curve file with duplicate temperature points is accepted (dry-run, one step) | no |
| Fan curve | partial | `fan.savemode` | the state file records mode=manual after pwm1_enable was written | yes |
| Battery care | partial | `bat.threshold` | a readable charge_control_end_threshold (80 when limited) | no |
| Battery care | partial | `bat.behaviour` | charge_behaviour lists inhibit-charge | no |
| Battery care | partial | `bat.limit` | no argument resolves to 100 % ('off') in a dry run and the real call is refused without root | no |
| Adaptive TDP | awaiting-install | `pwr.adaptive` | inactive and disabled after install; FAIL when enabled but dead | no |
| Adaptive TDP | awaiting-install | `pwr.ac` | on_ac is detected (so the battery clamps are live); SKIP with no AC/Mains node | no |
| SAGV / memory frequency | blocked | cmd: `tools/oxp3-sagv.sh status` | display v30, enable_sagv 0400, no point interface, MCHBAR unreadable, RAPL domains listed | no |
| Display HDR / vibrance / colour | working | `dsp.hdr` | HDR/WCG/depth/VRR reported for eDP-1 (HDR off is a valid state) | no |
| Display HDR / vibrance / colour | working | `dsp.icc` | the ICC profile state is reported | no |
| Display HDR / vibrance / colour | working | `dsp.panel` | the EDID verdict has 10-bit, PQ/HDR10 and BT.2020 | no |
| Display HDR / vibrance / colour | working | `dsp.vibrance` | the saturation shader tooling is present | no |
| Display HDR / vibrance / colour | working | cmd: `tools/oxp3-gamewrap.sh --status --json` | one decision object per feature; HDR is skipped unless the panel reports HDR on and the app opted in | no |
| IMU / gyro | not-a-gap | `imu.selftest` | RESULT: PASS (0 warning(s)) - IMU usable, gyro exposed to Steam | no |
| IMU / gyro | not-a-gap | `imu.iio` | the ACPI-id/label device is selected, not the i2c in_anglvel_x_raw fallback | no |
| Camera / IPU7 | not-a-gap | cmd: `grep -H . /sys/bus/acpi/devices/OVTI*/status /sys/bus/acpi/devices/INT3472*/status` | 0 on every listed device (camera function disabled / absent) | no |

Verify rows with a backticked id are checks in `tools/oxp3-verify.sh` (`--json` for machines, `--full` for the invasive ones); `cmd:` rows are direct read-only commands. Every component below is installed through `tools/oxp3-hw-install.sh` unless its row in [`docs/hw/REGISTRY.md`](hw/REGISTRY.md) says otherwise.
<!-- END GENERATED: install-verify -->

The Decky status plugin is a diagnostics component, so it is not one of the hardware rows above.
Its two checks are `decky.dir` (expected `plugin.json`, `main.py` and `dist/index.js`) and
`decky.loaded` (this boot's `plugin_loader` journal has a load line for `oxp3-status`; UNKNOWN when
the journal is unreadable). `tools/oxp3-hw-install.sh --status` prints the same state as one
`decky:` line, and the offline `--root` suite is `tests/integration/decky/run.sh`
(`tools/repo-test.sh --only decky-install`).

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
`--purge-config` to drop the two user/config files as well. `--uninstall --decky` removes only the
plugin directory whose `.oxp3-managed` marker names our plugin; a foreign or unmarked
`oxp3-status` directory is refused, and the `decky-backups` copies are kept. What is *not*
automatic: layered rpms (`oxp3-firmware`, `kmod-oxp3`) are removed by
`oxp3-fixes-fast.sh uninstall` / `rpm-ostree uninstall`; the orchestrator warns if it sees them.

The files the installers **modify in place** (`~/heroic-launch.sh`, a Heroic GamesConfig JSON,
`/etc/dracut.conf.d/60-oxp3-bt-firmware.conf`, `/etc/modprobe.d/oxp3-bmi160.conf`, the hhd Python
files) are backed up before every edit and put back by one command:

```sh
tools/oxp3-rollback.sh adopt      # once: record edits made by hand (idempotent)
tools/oxp3-rollback.sh restore    # after the uninstall: byte-identical restore
```

`--uninstall --all` prints that command. What is backed up, how the hash check refuses to
clobber a later user edit, and what can *not* be undone (rpm-ostree history, an enrolled MOK,
an initramfs that already carries the firmware, manual EC limits) is in
[ROLLBACK.md](ROLLBACK.md).

## 7. Rollback per component (manual, if you do not want the orchestrator)

| component | rollback |
|---|---|
| volume | plugin: `sudo tools/hhd-oxp3-voltap/install.sh uninstall; sudo systemctl restart hhd` (removes exactly the package + dist-info). Legacy daemon: `sudo tools/oxp3-voltap/install.sh uninstall; sudo rm -f /etc/oxp3-voltap.conf`. Both: `sudo tools/oxp3-hw-install.sh --uninstall --volume`. Finish with `sudo systemd-hwdb update; sudo udevadm trigger --subsystem-match=input --action=change` |
| LEDs / home button | `systemctl --user disable --now oxp3-homebtn; rm ~/.config/systemd/user/oxp3-homebtn.service; sudo rm -f /etc/udev/rules.d/90-oxp3-hid.rules; sudo udevadm control --reload-rules` (keep the rule if RGB uses it) |
| RGB | `systemctl --user disable --now oxp3-rgbd; rm ~/.config/systemd/user/oxp3-rgbd.service ~/.config/oxp3-rgb.toml; sudo rm -f /etc/udev/rules.d/90-oxp3-hid.rules; sudo udevadm control --reload-rules` |
| sleep | `sudo tools/oxp3-hw-install.sh --uninstall --sleep` (or: `sudo systemctl disable --now oxp3-sleep-wakeup oxp3-sleep-pre oxp3-sleep-log; sudo rm -f /etc/systemd/system/oxp3-sleep-*.service /usr/local/libexec/oxp3-sleep-* /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules /etc/systemd/logind.conf.d/60-oxp3-sleep.conf /etc/polkit-1/rules.d/49-oxp3-suspend.rules; sudo systemctl daemon-reload; sudo udevadm control --reload-rules; sudo systemctl restart systemd-logind`) — wake-up defaults return after a reboot |
| Bluetooth | `sudo tools/oxp3-hw-install.sh --uninstall --bluetooth` (removes the dracut conf and the files we added); if the `oxp3-firmware` rpm is layered, `./oxp3-fixes-fast.sh uninstall`. Stock = the radio dead again, which is the pre-fix state |
| power / fan / battery | `sudo /usr/local/libexec/oxp3-powerctl restore-baseline` **before** uninstalling, or just reboot (firmware re-initialises the RAPL limits); `sudo systemctl disable --now oxp3-fan-curve` (the tool restores EC auto mode on SIGTERM); `sudo tools/oxp3-power-install.sh uninstall`; `/etc/oxp3/` is kept |
| battery limit / bypass | `sudo /usr/local/libexec/oxp3-powerctl battery limit off` and `battery bypass off`; the EC keeps the last saved limit until changed, so do this before uninstalling |
| adaptive TDP | `sudo systemctl disable --now oxp3-adaptivetdp` (SIGTERM restores the preset that was active when it started) |
| IMU | `sudo rm -f /etc/modprobe.d/oxp3-bmi160.conf; sudo reboot` (or `sudo modprobe -r bmi260_i2c; sudo modprobe bmi160_i2c` to see the old misdetection again) |
| audio | `tools/audio/eq-off.sh` (removes the drop-in, restores the hardware speaker as default) |
| Decky plugin | `sudo tools/oxp3-hw-install.sh --uninstall --decky` (marker-checked; the `decky-backups` copies stay under `~/.local/state/oxp3-fixes/`), then `sudo systemctl restart plugin_loader.service`; a specific earlier copy is restored with `cp -a` from its backup (docs/decky-plugin.md) |

## 8. Interaction with the driver installers

* `oxp3-fixes-fast.sh install` now runs `sudo tools/oxp3-hw-install.sh --leds --power` after its
  extras (and the matching `--uninstall`). `--skip-hw` skips it; `--skip-extras` does not.
* `oxp3-fixes.sh install` does the same and additionally installs the sleep units and the hhd
  volume patch — so for that script the orchestrator line only needs `--rgb --audio` (LEDs and the
  power units are already called by its extras).
* The orchestrator's preflight is per invocation: `--force` overrides a `FAIL`, `--dry-run`,
  `--root` and `--uninstall` skip it. A `WARN` never blocks (see [PREFLIGHT.md](PREFLIGHT.md)
  for the WARN/FAIL split).
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
  audio, decky), a second idempotent
  run, then `--uninstall`: the expected files appear, stay unchanged, and are removed; user config is
  kept. The component installers that write to `/` correctly report `[root-prefix] skipped`. The
  Decky half additionally covers install/status/reinstall-with-backup/uninstall, a missing artifact,
  a tampered sha256 and a foreign directory with the same name (`tests/integration/decky/run.sh`,
  suite `decky-install`).
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
