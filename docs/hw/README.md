# OneXPlayer 3 hardware work under Linux

Status of every hardware gap we have investigated. Each topic's document holds the evidence (reverse-engineered from
the Windows driver stack in `/var/home/holzr/Downloads/oxp3`), the implementation and the validation steps.
Nothing vendor-proprietary is in this repo: only our own code and our own documentation.

| Topic | State | Our implementation | Doc | Needs root? |
|---|---|---|---|---|
| Volume keys sticking | fixed in software, awaiting on-device check | `tools/oxp3-voltap/` (EVIOCGRAB daemon + hwdb + unit) | [volume-buttons.md](volume-buttons.md) | yes (install) |
| Home button + top LEDs | protocol recovered, tooling ready | `tools/oxp3_hid.py`, `oxp3-led.py`, `oxp3-homebtn.py`, `oxp3-hid.rules`, unit | [home-button-leds.md](home-button-leds.md) | yes (udev rule) |
| Body RGB zones + effects | 5 zones / 11 presets mapped, daemon keeps our state over HHD | `tools/oxp3_hid.py`, `oxp3-led.py`, `oxp3-rgbd.py` + user unit | [rgb-coverage.md](rgb-coverage.md) | yes (udev rule) |
| Sleep / battery drain | **the device never suspended at all**; fix shipped | `files/oxp3-sleep-*` units/rules, `tools/sleep-power-measure.sh` | [sleep-power.md](sleep-power.md) | yes (units) |
| Speaker tuning (Harman APO) | recovered + reproduced, 0.03 dB model error | `tools/audio/` (PipeWire filter-chain, measure/extract scripts) | [audio-harman-apo.md](audio-harman-apo.md) | no |
| Fingerprint reader | unsupported; protocol recovered, driver skeleton shipped | `tools/fingerprint/` (probe + libfprint skeleton) | [fingerprint.md](fingerprint.md) | later (driver) |
| Bluetooth dead | **firmware was present**; initramfs never got it | `tools/oxp3-bt-firmware.sh` | [bluetooth.md](bluetooth.md) | yes |
| Power envelope + fan | PL2/PL4/tau now settable without kernel patches; EC fan curves not exposed | `tools/oxp3-powerctl.py`, installer + units | [power-fan.md](power-fan.md) | yes |
| Battery health, charge limit, bypass, adaptive TDP | charge limit + charge-inhibit ('bypass' approximation) reachable; EC bypass byte + charger tier not exposed; adaptive TDP daemon shipped (opt-in) | `tools/oxp3-powerctl.py battery`, `tools/oxp3-adaptivetdp.py`, unit | [battery-tdp.md](battery-tdp.md) | yes |
| Camera / IPU7 | **no camera hardware on this unit** (all OVTI/INT3472 `_STA=0`, no IPU7 function); not a Linux gap | `tools/oxp3-imu.py` (sensor diagnostics only) | [camera-imu.md](camera-imu.md) | n/a |
| IMU / gyro | **works** (BMI260 on i2c-10EC5280:00, IIO 100 Hz, HHD `sync_gyro` into the emulated Steam Controller) | `tools/oxp3-imu.py`, `files/oxp3-bmi160.conf` | [camera-imu.md](camera-imu.md) | no |
| SAGV / memory-frequency point | **no Linux interface exists** (boot-time `xe.enable_sagv` only, and it only disables); vendor path recovered (MCHBAR+0x5DA0/0x5DA4); read-only status + power-measurement tooling shipped | `tools/oxp3-sagv.sh`, `files/rapl-power` (now publishes `dram`/`psys`) | [sagv.md](sagv.md) | no to measure; yes to refresh `rapl-power` |
| Display HDR / vibrance / colour | **panel is 10-bit PQ/HDR10 wide gamut** and Plasma 6.7 toggles it; Gamemode needs two gamescope flags; our shader + ICC shipped | `tools/display/` (display-mode.sh, vibrance.sh, oxp3-edid.py, oxp3-icc.py, oxp3-vibrance.fx) | [hdr-vibrance.md](hdr-vibrance.md) | only for the Gamemode flag |
| Remaining gaps | ranked audit (SAGV, EC fan curves, charging bypass byte, camera only on other SKUs) | — | [gap-audit.md](gap-audit.md) | mixed |

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
