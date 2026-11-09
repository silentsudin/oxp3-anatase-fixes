# OneXPlayer 3 hardware work under Linux

Status of every hardware gap we have investigated. Each topic's document holds the evidence (reverse-engineered from
the Windows driver stack in `/var/home/holzr/Downloads/oxp3`), the implementation and the validation steps.
Nothing vendor-proprietary is in this repo: only our own code and our own documentation.

Project-level orientation, install status, corrections and the owner's next actions: [`../HANDOVER.md`](../HANDOVER.md).

The matrix below is generated from [`components.yaml`](components.yaml); see [`REGISTRY.md`](REGISTRY.md) for the
schema, the state vocabulary and the rule that generated blocks are never hand-edited. The same data file renders the
install/verify matrix in [`../INSTALL-HW.md`](../INSTALL-HW.md) and the summary in [`../../README.md`](../../README.md).

<!-- BEGIN GENERATED: hw-state (source: docs/hw/components.yaml; regenerate: tools/gen-docs.py --write) -->
| Topic | State | Summary | Our implementation | Doc | Needs root? |
|---|---|---|---|---|---|
| Volume keys | awaiting-install | A hold on the rocker ramps instead of stepping: our daemon prefers hhd's virtual volume keyboard, falls back to the free physical AT keyboard, degrades observably on EBUSY, and keeps a force-release hwdb as defence in depth. | `tools/oxp3-voltap/`, `files/84-oxp3-volume-keys.hwdb`, `tools/lib/install-common.sh` | [volume-buttons.md](volume-buttons.md) | yes |
| Home button and top LEDs | awaiting-install | The home button maps to KEY_HOMEPAGE and the top LEDs speak the recovered vendor HID protocol (5 zones, 11 presets). | `tools/oxp3_hid.py`, `tools/oxp3-led.py`, `tools/oxp3-homebtn.py`, `tools/oxp3-hid.rules`, `tools/oxp3-homebtn.service` | [home-button-leds.md](home-button-leds.md) | yes |
| Body RGB zones | awaiting-install | 5 addressable body zones and 11 presets encoded from the captured frames, with a keeper daemon that holds our state over HHD. | `tools/oxp3-rgbd.py`, `tools/oxp3-rgbd.service`, `tools/oxp3-rgbd.toml.example`, `tools/oxp3-led.py` | [rgb-coverage.md](rgb-coverage.md) | yes |
| Daemon lifecycle (suspend/hotplug) | awaiting-install | oxp3-homebtn, oxp3-rgbd and oxp3-voltap rediscover their device by identity after a resume or hotplug, back off with a logged reason, recreate uinput, and let systemd restart them only as a last resort; the journal and two verify checks prove a live fd and every recovered loss. | `tools/oxp3_lifecycle.py`, `tools/oxp3-homebtn.py`, `tools/oxp3-rgbd.py`, `tools/oxp3-voltap/` | [DAEMONS.md](../DAEMONS.md) | no |
| Suspend and wake policy | awaiting-install | Suspend works here (nine real s2idle cycles in the journal) but nothing in the current boots asks for it: logind had no Handle* policy (power-key default poweroff), KDE PowerDevil owns handle-*-key, and hhd only forwards the key to Steam in Gamepad mode; a shipped logind drop-in plus polkit rule make sleep reachable without a desktop session. | `files/oxp3-sleep-wakeup`, `files/oxp3-sleep-wakeup.service`, `files/oxp3-sleep-pre.service`, `files/oxp3-sleep-log.service`, `files/99-oxp3-sleep-wakeup.rules`, `files/logind-oxp3.conf`, `files/49-oxp3-suspend.rules`, `tools/sleep-power-measure.sh` | [sleep-power.md](sleep-power.md) | yes |
| Speaker tuning (Harman APO) | working | The Harman speaker APO recovered as a PipeWire filter-chain sink: 8 bands, -8 dB pre-gain, 0.03 dB model error. | `tools/audio/`, `files/52-oxp3-speakers.conf`, `files/oxp3-audio-route.service`, `files/oxp3-audio-resume@.service` | [audio-harman-apo.md](audio-harman-apo.md) | no |
| Bluetooth firmware | awaiting-install | The radio was dead because btintel_pcie has no MODULE_FIRMWARE() entries: the pinned upstream files go into /usr/lib/firmware/updates and into the initramfs. | `tools/oxp3-bt-firmware.sh` | [bluetooth.md](bluetooth.md) | yes |
| Power envelope (PL1/PL2/PL4) | working | PL1/PL2/PL4 and tau settable on both RAPL zones without kernel patches, plus a saved boot/resume profile and the tdp-sync invariant. | `tools/oxp3-powerctl.py`, `tools/oxp3-power-install.sh`, `files/oxp3-power-profile.service`, `files/oxp3-power-resume.service`, `files/oxp3-tdp-sync`, `files/oxp3-tdp-sync.service`, `files/oxp3-rapl-pp1.service` | [power-fan.md](power-fan.md) | yes |
| Fan curve | partial | A manual PWM fan curve works through pwm1. The EC keeps no fan-curve table on this board (recovered 2026-11-09: its auto/manual, duty and tach registers are exactly what oxpec already drives), so userspace is the only place a curve can live. | `tools/oxp3-powerctl.py`, `files/oxp3-fan-curve.service` | [power-fan.md](power-fan.md) | yes |
| Battery care | partial | Charge limit and charge-inhibit ('bypass') are reachable through the driver's EC bytes, now identified: 0xA3 percent cap and 0xA4 bit mask (0x00/0x01/0x03). The charger tier (0xE3) and the charge floor (0xA5) have addresses but no ABI and no proven units. | `tools/oxp3-powerctl.py` | [battery-tdp.md](battery-tdp.md) | yes |
| Adaptive TDP | awaiting-install | An opt-in adaptive-TDP daemon that emits one decision object per interval and is installed inactive so it cannot change power policy by accident. | `tools/oxp3-adaptivetdp.py`, `files/oxp3-adaptivetdp.service` | [battery-tdp.md](battery-tdp.md) | yes |
| SAGV / memory frequency | blocked | No Linux control exists: Windows pins the SAGV/QCLK-GV point through an MCHBAR register pair, while Linux only has the boot-time xe.enable_sagv switch, which can only disable. | `tools/oxp3-sagv.sh`, `upstream/intel-pmdemand-sagv-request.md` | [sagv.md](sagv.md) | no |
| Display HDR / vibrance / colour | working | The internal OLED is 10-bit PQ/HDR10 wide gamut and Plasma toggles it; our ICC profile, vibrance shader and one per-game launch wrapper ship, while Gamemode needs the gamescope session flags. | `tools/display/`, `tools/oxp3-gamewrap.sh`, `tools/lib/oxp3_gamewrap.py`, `tools/oxp3-gamewrap.toml.example`, `patches/gamescope/` | [hdr-vibrance.md](hdr-vibrance.md) | partial |
| IMU / gyro | not-a-gap | The BMI260 works out of the box at 100 Hz and HHD syncs the gyro into the emulated Steam Controller; our tool is diagnostics only. | `tools/oxp3-imu.py`, `files/oxp3-bmi160.conf` | [camera-imu.md](camera-imu.md) | no |
| Camera / IPU7 | not-a-gap | There is no camera hardware on this unit: every OVTI/INT3472 device is _STA=0 and there is no IPU7 function, so this is not a Linux gap. | `tools/oxp3-imu.py` | [camera-imu.md](camera-imu.md) | n/a |
| Fingerprint reader | partial | Betterlife BF63112 on Intel LPSS SPI: the protocol is recovered byte-for-byte with a tested userspace library and a driver that compiles, but enrol/verify are unimplemented and there is no kernel driver. | `tools/fingerprint/` | [fingerprint.md](fingerprint.md) | partial |

Recorded caveats:

- **Volume keys**: do not combine with the hhd volume patch from oxp3-fixes.sh install; the standing alternative is the hhd patch, which is why the doc says pick one; device discovery is by name/phys/capabilities plus /proc/*/fdinfo holders, never by eventN; exit 2 = no candidate, 3 = every grab refused
- **Home button and top LEDs**: the user unit references the checkout path; moving the repo needs a re-run; hidraw is root-only until the udev rule is installed; a press is only observed after someone presses the button once
- **Body RGB zones**: needs ~/.config/oxp3-rgb.toml; the installer copies the example only when it is absent
- **Daemon lifecycle (suspend/hotplug)**: life.sim only runs with --full (it runs the offline test suite); life.fd needs root to inspect /proc/<pid>/fd of the root-run oxp3-voltap; systemd Restart=always is the safety net; normal resume/hotplug does not restart the daemon
- **Suspend and wake policy**: sleep.suspend is the only check that really suspends the machine; it needs --full --with-changes and OXP3_ALLOW_SUSPEND=1; no real suspend recorded since install: run sudo tools/sleep-power-measure.sh suspend -w 600, or sudo systemctl suspend; sleep.measure SKIPs on AC by design; the logind drop-in is read at logind start: restart systemd-logind (or reboot) after installing; PowerDevil keeps owning handle-power-key while Plasma runs, so HandlePowerKey=suspend only takes effect when no desktop claims the key
- **Speaker tuning (Harman APO)**: keep experiments in an isolated PIPEWIRE_RUNTIME_DIR; a throwaway instance can wedge the SOF card
- **Bluetooth firmware**: the running initramfs only gets the files after a rebuild and reboot; --check downloads the pinned upstream files to verify hashes, so it is not offline
- **Power envelope (PL1/PL2/PL4)**: the units are inert until a profile is saved; writes are root-only and were not executed in the offline round
- **Fan curve**: SIGTERM restores EC auto mode; the acpi_fan EIO in the fan node is a known cosmetic bug; there is no vendor EC curve table to expose: Devs*FanCurve are ASUS WMI device ids; see upstream/oxpec-ec-addresses.md; the EC tach is 0x58/0x59 (upstream oxpec, OXP_2_SENSOR_FAN_REG); older notes said 0x76
- **Battery care**: the EC keeps the last saved limit until it is changed again; bypass is charge-inhibit (EC 0xA4), the same byte and values the vendor writes; whether the EC also isolates the pack from the rail is unproven; addresses: upstream/oxpec-ec-addresses.md; mode 1 (0x01) is awake-only, mode 2 (0x03) is always
- **Adaptive TDP**: SIGTERM restores the preset that was active when it started; not enabled by --all
- **SAGV / memory frequency**: changing the point needs kernel work; the vendor path is recorded in upstream/intel-pmdemand-sagv-request.md; rapl-power's dram/psys values are what make the effect measurable
- **Display HDR / vibrance / colour**: HDR in the Gamemode session is the only part that needs root; the panel is wide gamut; an ICC profile is what keeps SDR colours sane; feature composition for a launch is tools/oxp3-gamewrap.sh (docs/LAUNCH.md): per-game opt-in, HDR never forced, MangoHud config untouched; Flatpak Steam gets the XMX variables via tools/oxp3-flatpak-xmx.sh plus OXP3_XMX_ENV_FILE in the launch options (docs/hw/flatpak-xmx.md)
- **IMU / gyro**: HHD must be running for the gyro-to-Steam path; the blacklist is only needed if the sensor is misdetected as BMI160
- **Camera / IPU7**: other OXP3 SKUs may populate the camera; the ACPI dump in the doc is this unit
- **Fingerprint reader**: never run against the sensor; every runtime claim is marked inferred or unknown; the probe needs spidev bound and root, and the driver needs libfprint's internal API
<!-- END GENERATED: hw-state -->

## Install

One entry point for the whole hardware set:

```sh
sudo tools/oxp3-hw-install.sh --all     # per-component flags: --volume --leds --sleep --bluetooth
                                        # --power --rgb --imu --audio; see --help
```

Prerequisites, the ordered single command set, per-component opt-outs, the verification matrix
(exact command + expected output for every fix), uninstall and per-component rollback are in
[../INSTALL-HW.md](../INSTALL-HW.md).

### Manual order (what the orchestrator runs, per component)

1. `sudo tools/oxp3-voltap/install.sh install --hwdb`
2. `sudo install -m 0644 tools/oxp3-hid.rules /etc/udev/rules.d/90-oxp3-hid.rules && sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw --action=change`
3. Sleep units: commands in [sleep-power.md](sleep-power.md); then `tools/sleep-power-measure.sh run` to prove a real suspend.
4. Bluetooth: `sudo tools/oxp3-bt-firmware.sh` (writes the dracut `install_items` config and enables the initramfs), then reload now with `sudo modprobe -r btintel_pcie btintel && sudo modprobe btintel_pcie`.
5. Power/fan: `sudo tools/oxp3-power-install.sh install` then `sudo /usr/local/libexec/oxp3-powerctl profile balanced --save`.
6. Battery care: `sudo tools/oxp3-powerctl.py battery limit 80 --save` and, if you want the
   vendor 'bypass' behaviour, `sudo tools/oxp3-powerctl.py battery bypass on --save`; adaptive
   TDP is opt-in: `sudo tools/oxp3-power-install.sh install --adaptive-tdp` (installs it inactive).
7. Audio: `tools/audio/eq-on.sh` (no root).
8. RGB keeper: `install -m 0644 tools/oxp3-rgbd.toml.example ~/.config/oxp3-rgb.toml` then
   `systemctl --user enable --now oxp3-rgbd.service` (after step 2; see [rgb-coverage.md](rgb-coverage.md)).
9. Display: `tools/display/display-mode.sh status` (nothing changes), then optionally `hdr on`; per-game
   saturation is `tools/display/vibrance.sh install && tools/display/vibrance.sh set 35` — no root. HDR in
   the Gamemode session is the only privileged part: [hdr-vibrance.md](hdr-vibrance.md) §5.2.
10. Status: build the Decky plugin (`tools/decky-oxp3-status/`) or run `tools/oxp3-status.sh` headless.

## Known traps (learned the hard way)

- A throwaway PipeWire instance can grab the SOF speaker PCM and wedge the card (`snd_sof` NULL-deref, D-state process,
  "Dummy Output" until reboot). Use `PIPEWIRE_RUNTIME_DIR` isolation for experiments.
- `/usr/lib/firmware` is part of the ostree image; only `/usr/lib/firmware/updates` is ours. Anything needed before
  `switch_root` must go into the initramfs explicitly, because `btintel_pcie` has no `MODULE_FIRMWARE()` entries.
- Building Mesa with `--prefix=/usr/local` silently loses the per-game workaround database; the driver must resolve
  `/usr/share/drirc.d` (see `../xess-xmx.md`).
