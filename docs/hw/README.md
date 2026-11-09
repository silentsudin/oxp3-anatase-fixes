# OneXPlayer 3 hardware work under Linux

Status of every hardware gap we have investigated. Each topic's document holds the evidence (reverse-engineered from
the Windows driver stack in `/var/home/holzr/Downloads/oxp3`), the implementation and the validation steps.
Nothing vendor-proprietary is in this repo: only our own code and our own documentation.

| Topic | State | Our implementation | Doc | Needs root? |
|---|---|---|---|---|
| Volume keys sticking | fixed in software, awaiting on-device check | `tools/oxp3-voltap/` (EVIOCGRAB daemon + hwdb + unit) | [volume-buttons.md](volume-buttons.md) | yes (install) |
| Home button + top LEDs | protocol recovered, tooling ready | `tools/oxp3_hid.py`, `oxp3-led.py`, `oxp3-homebtn.py`, `oxp3-hid.rules`, unit | [home-button-leds.md](home-button-leds.md) | yes (udev rule) |
| Sleep / battery drain | **the device never suspended at all**; fix shipped | `files/oxp3-sleep-*` units/rules, `tools/sleep-power-measure.sh` | [sleep-power.md](sleep-power.md) | yes (units) |
| Speaker tuning (Harman APO) | recovered + reproduced, 0.03 dB model error | `tools/audio/` (PipeWire filter-chain, measure/extract scripts) | [audio-harman-apo.md](audio-harman-apo.md) | no |
| Fingerprint reader | unsupported; protocol recovered, driver skeleton shipped | `tools/fingerprint/` (probe + libfprint skeleton) | [fingerprint.md](fingerprint.md) | later (driver) |
| Bluetooth dead | **firmware was present**; initramfs never got it | `tools/oxp3-bt-firmware.sh` | [bluetooth.md](bluetooth.md) | yes |
| Power envelope + fan | PL2/PL4/tau now settable without kernel patches; EC fan curves not exposed | `tools/oxp3-powerctl.py`, installer + units | [power-fan.md](power-fan.md) | yes |
| Remaining gaps | ranked audit (camera/IPU7, RGB zones, gyro, battery bypass, SAGV, HDR) | — | [gap-audit.md](gap-audit.md) | mixed |

## Recommended install order

1. `sudo tools/oxp3-voltap/install.sh install --hwdb`
2. `sudo install -m 0644 tools/oxp3-hid.rules /etc/udev/rules.d/90-oxp3-hid.rules && sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw --action=change`
3. Sleep units: commands in [sleep-power.md](sleep-power.md); then `tools/sleep-power-measure.sh run` to prove a real suspend.
4. Bluetooth: `sudo tools/oxp3-bt-firmware.sh` (writes the dracut `install_items` config and enables the initramfs), then reload now with `sudo modprobe -r btintel_pcie btintel && sudo modprobe btintel_pcie`.
5. Power/fan: `sudo tools/oxp3-power-install.sh install` then `sudo /usr/local/libexec/oxp3-powerctl profile balanced --save`.
6. Audio: `tools/audio/eq-on.sh` (no root).
7. Status: build the Decky plugin (`tools/decky-oxp3-status/`) or run `tools/oxp3-status.sh` headless.

## Known traps (learned the hard way)

- A throwaway PipeWire instance can grab the SOF speaker PCM and wedge the card (`snd_sof` NULL-deref, D-state process,
  "Dummy Output" until reboot). Use `PIPEWIRE_RUNTIME_DIR` isolation for experiments.
- `/usr/lib/firmware` is part of the ostree image; only `/usr/lib/firmware/updates` is ours. Anything needed before
  `switch_root` must go into the initramfs explicitly, because `btintel_pcie` has no `MODULE_FIRMWARE()` entries.
- Building Mesa with `--prefix=/usr/local` silently loses the per-game workaround database; the driver must resolve
  `/usr/share/drirc.d` (see `../xess-xmx.md`).
