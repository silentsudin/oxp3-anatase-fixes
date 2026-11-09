# Bluetooth: the radio is dead because `btintel_pcie` probes before the root is
# mounted — the firmware is not missing

Status: controller identified, firmware pinned and hash-verified against
upstream `linux-firmware`, root cause proven from the boot log. **The radio
still does not work on this host**: the fix needs two privileged commands and a
dracut setting that this audit was not allowed to apply (see
[Install](#install-exact-privileged-commands) and [Verify](#verify)).
Owner: silentsudin. Host: Anatase 20261026.3, kernel `7.2.7-an28.fc45.x86_64`,
BlueZ `5.87+1.git8750129efca8` (`bluetoothd` pid 1223), `linux-firmware-20260916.an03-1.fc45.ludos`,
`oxp3-firmware-1-202610292312`.

## Summary

* The controller is **PCIe, not USB**: `0000:00:14.7` `[8086:e476]`, class
  `0x0d1100`, driven by out-of-tree `btintel_pcie` (from the local `kmod-oxp3`
  package). `lsusb` shows nothing for Bluetooth — the `gap-audit.md` assumption
  of a USB CNVi part is wrong.
* The blob `intel/ibt-00a0-0291-iml.sfi` **is not missing**. It is on disk in
  `/usr/lib/firmware/updates/intel/`, byte-identical to current upstream
  `linux-firmware`, and was already installed 2 minutes before this boot.
* The driver requests it **inside the initramfs**, where the ostree root (and
  therefore `/usr/lib/firmware`) is not mounted yet. It gets `-ENOENT`, retries
  once, gives up, and nothing re-probes after `switch_root` — so the radio is
  dead for the whole boot with a "missing firmware" message that is really a
  "wrong root" message.
* `dracut` cannot fix that by itself: `btintel_pcie` computes the file names at
  run time and ships **no** `MODULE_FIRMWARE()`/`modinfo firmware=` entries, so
  the initramfs builder has nothing to copy. Every other working firmware on
  this box (`xe`, `iwlwifi`) *is* declared that way.
* Fix: reload the module once for the current boot, and add the two files to
  the initramfs (or keep the driver out of it) for every boot afterwards.
  `tools/oxp3-bt-firmware.sh` does the download/verify/install and prints the
  exact `sudo` lines; nothing vendor-derived is stored in this repo.

## What the hardware is

| item | value |
|---|---|
| PCI address | `0000:00:14.7` |
| IDs | vendor `0x8086`, device `0xe476`, class `0x0d1100`, rev `0x01` |
| name | Intel Corporation Core Ultra Processors (Series 3) CNVi Bluetooth |
| driver | `btintel_pcie` ("Intel Bluetooth PCIe transport driver ver 0.1"), module `updates/drivers/bluetooth/btintel_pcie.ko.xz`, signed by "OXP3 module signing CA (holzr)" |
| hci node | `hci0` → `/sys/devices/pci0000:00/0000:00:14.7/bluetooth/hci0`, `rfkill0` soft/hard **unblocked** |
| userspace | `bluetooth.service` active and enabled, `bluetoothd` running |

Userspace is healthy; the transport is not:

```
$ bluetoothctl show
No default controller available
$ btmgmt info
Index list with 0 items
$ rfkill list
0: hci0: Bluetooth        Soft blocked: no        Hard blocked: no
```

`hci0` exists because the PCI device probed; it carries no firmware, so it never
registers as a usable controller.

## The evidence, and why "missing firmware" is the wrong diagnosis

Kernel log, this boot (`journalctl -k -b`, hostname is part of the evidence):

```
16:15:17.312493 anatase  kernel: Run /init as init process
16:15:17.312579 anatase  systemd[1]: Successfully made /usr/ read-only.      <- initramfs systemd
16:15:18.209157 anatase  kernel: btintel: loading out-of-tree module taints kernel.
16:15:18.233167 anatase  kernel: btintel_pcie 0000:00:14.7: enabling device (0000 -> 0002)
16:15:18.289363 anatase  kernel: Bluetooth: hci0: Minimum firmware build 1 week 10 2014
16:15:18.289384 anatase  kernel: Bluetooth: hci0: Bootloader timestamp 2023.43 buildtype 1 build 11631
16:15:18.289403 anatase  kernel: Bluetooth: hci0: Failed to load Intel firmware file intel/ibt-00a0-0291-iml.sfi (-2)
16:15:18.289420 anatase  kernel: Bluetooth: hci0: Firmware download retry count: 1
16:15:18.349280 anatase  kernel: Bluetooth: hci0: Failed to load Intel firmware file intel/ibt-00a0-0291-iml.sfi (-2)
16:15:18.349290 anatase  kernel: Bluetooth: hci0: Failed to read MSFT supported features (-56)
16:15:23        anatase-oxp3 ...                                          <- real root is up from here
```

Three independent facts pin the failure to the initramfs:

1. **The file exists on the real root.** `sha256sum
   /usr/lib/firmware/updates/intel/ibt-00a0-0291-iml.sfi` =
   `69beb773…8026`, identical to upstream (below). A file that exists cannot
   yield `-ENOENT` — unless the lookup ran in a different mount namespace, i.e.
   the initramfs, where `/usr/lib/firmware` is only what dracut packed.
2. **Timing.** The probe is at `16:15:18.2`, i.e. after `Run /init` but before
   the kernel/syslog hostname flips from the initramfs value `anatase` to the
   real root's `anatase-oxp3` at `16:15:23`.
3. **The package that carries the files was installed before the boot.**
   `oxp3-firmware-1-202610292312` INSTALLTIME `2026-11-08 16:13:23`, boot
   `16:15:16`. So this is not "install it and reboot later" — the blobs were
   already present and the boot still failed.

### Why dracut cannot include them

dracut only copies firmware that a module *declares*:

```
$ for m in btintel_pcie btintel iwlwifi xe; do printf '%s: ' $m; modinfo $m | grep -c '^firmware:'; done
btintel_pcie: 0
btintel: 4          # only the old USB names ibt-11-5.* / ibt-12-16.*
iwlwifi: 64
xe: 41
```

`btintel` builds the PCIe names at run time (`strings btintel.ko`:
`intel/ibt-%04x-%04x-iml.%s`, `intel/ibt-%04x-%04x-%s.%s`, `intel/ibt-%04x-%04x.%s`),
so no static list exists for `ibt-00a0-0291-*`. The same window proves the
mechanism from the other side: `xe` did load its DMC from the *updates*
directory at `16:15:18` (`Finished loading DMC firmware i915/xe3lpd_dmc.bin
(v2.20)`, the pinned `oxp3-firmware` version), because `xe` declares
`i915/xe3lpd_dmc.bin` and dracut resolved it with `updates/` priority.

This also means: on a normal distro kernel the same thing happens to every
`btintel_pcie` user whose initramfs includes the driver. It is worth an
upstream/dracut report (open point).

## The firmware set

Upstream `linux-firmware`, `WHENCE` group `Driver: btusb`, version
`BT_Scorpius_S_REL102163_24.80.26362.102163`, licence *Redistributable. See
`LICENCE.ibt_firmware` for details* (`SPDX-License-Identifier:
LicenseRef-LICENCE.ibt_firmware`; local copy
`/usr/share/licenses/linux-firmware/LICENCE.ibt_firmware` — Intel copyright,
"Redistribution and use in binary form, without modification, are permitted").
So redistribution is allowed; the blobs still stay **out of this repo**, the
installer fetches them.

| file | bytes | sha256 |
|---|---|---|
| `intel/ibt-00a0-0291-iml.sfi` | 63056 | `69beb77387a79a75ee741ff6f88f782a0fc44c8b2efcc7bcb41c413122928026` |
| `intel/ibt-00a0-0291-pci.sfi` | 998916 | `6ea03de60bdd6231387f561fbae53b1aaedfd906437b3c2216b45df1d4f01e45` |
| `intel/ibt-00a0-0291-pci.ddc` (optional, 4-byte stub `03 28 01 18`) | 4 | `e0fb10fe29f60607d6507ced3f697492d9611f62c4765feaee59c4bd4101d3e7` |

Sources, both serving the same bytes (verified 2026-11-09):

* kernel.org: `https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain/intel/…`
* GitLab mirror: `https://gitlab.com/kernel-firmware/linux-firmware/-/raw/main/intel/…`

History via `git log` on the cgit path: the pair came in with "linux-firmware:
Update firmware file for Intel Scorpius core" (`f6a848e7ddb764af397aff1cad1488b57daa5382`,
Kiran K, 2026-08-27), with later Scorpius updates including 2026-10-01; the
24.80 build is current `main`. Fedora's `linux-firmware` is the package that
would normally carry it (it already carries the whole `ibt-*` family, 122 entries in `/usr/lib/firmware/intel`),
but the packaged `-0291` pair is an **older build** than upstream
(`/usr/lib/firmware/intel/ibt-00a0-0291-pci.sfi.xz` decompresses to 1010692 bytes,
hash `e108f98d…0fb31`), which is exactly why `oxp3-firmware` pins the 24.80
build into `updates/`. The distro symlinks `ibt-00a0-0291-pci.ddc.xz →
ibt-0093-0041.ddc.xz`, which decompresses to the same 4-byte stub as upstream,
so the `.ddc` needs no override.

## Where the files live, and why `updates/`

The kernel searches `fw_path` in this order (Documentation/driver-api/firmware/
fw_search_path): `/lib/firmware/updates/<uname -r>/`, `/lib/firmware/updates/`,
`/lib/firmware/<uname -r>/`, `/lib/firmware/`. On this host `/lib` is a symlink
to `usr/lib`, so a file in `/usr/lib/firmware/updates/intel/` shadows the
packaged one in `/usr/lib/firmware/intel/` without touching package payload —
that is the documented place for a pinned build, and it is where
`oxp3-firmware` puts it.

**Correction to the working assumption:** `/usr` here is a composefs
(`overlay`, lowerdir `usr`, upperdir `.usr-ovl-upper`) and is read-only unless a
`rpm-ostree usroverlay` ("hotfix") is active; the current boot has one, which is
why a plain `sudo install` works *right now* but would be lost at reboot.
Durability on this host comes from the **layered local RPM** (`oxp3-firmware`,
`rpm-ostree status` → `LocalPackages`), not from writing into `/usr` by hand.
That is why the script's install path is: prefer the package, fall back to
`usroverlay` for a test, and always add the initramfs piece.

## Install (exact privileged commands)

Nothing below was executed during this audit (`NO sudo`). They are written to be
copy-pasted. `tools/oxp3-bt-firmware.sh` prints exactly these lines under
`--dry-run`.

### A. Fix the running boot (no reboot, ~5 s)

The blobs are already installed by `oxp3-firmware`; the driver just has to be
re-probed from the real root:

```sh
sudo systemctl stop bluetooth
sudo modprobe -r btintel_pcie btintel
sudo modprobe btintel_pcie
sudo systemctl start bluetooth
```

### B. Fix every boot (initramfs)

One of these two, then rebuild the initramfs and reboot. `items` is the default
and keeps the driver able to load early; `omit` keeps the initramfs ~1 MB
smaller by deferring the probe to after `switch_root`.

```sh
# B1 (default): carry the blobs inside the initramfs
sudo tee /etc/dracut.conf.d/60-oxp3-bt-firmware.conf >/dev/null <<'EOF'
install_items+=" /usr/lib/firmware/updates/intel/ibt-00a0-0291-iml.sfi /usr/lib/firmware/updates/intel/ibt-00a0-0291-pci.sfi "
EOF

# or B2 (alternative): keep btintel_pcie out of the initramfs, let udev probe later
# sudo tee /etc/dracut.conf.d/60-oxp3-bt-firmware.conf >/dev/null <<'EOF'
# omit_drivers+=" btintel_pcie "
# EOF

sudo rpm-ostree initramfs --enable      # ostree: regenerate on next deployment
# non-ostree hosts instead:  sudo dracut -f --kver "$(uname -r)"
# then reboot
```

### C. The script

```sh
tools/oxp3-bt-firmware.sh --check       # read-only diagnosis, no root
tools/oxp3-bt-firmware.sh --dry-run     # exact commands, no root
sudo tools/oxp3-bt-firmware.sh          # download -> verify sha256 -> install -> dracut conf -> reload -> verify
```

Options: `--from DIR|FILE` (use local copies instead of downloading), `--cache
DIR` (default `${XDG_CACHE_HOME:-~/.cache}/oxp3-bt-firmware`, always outside the
repo), `--with-ddc`, `--persist=items|omit|none`, `--no-reload`, `--url-base`.
It skips the copy when the installed file already matches the pinned hash (the
case on this machine), and it refuses to install anything whose size or SHA-256
differs from the pinned values.

## Verify

```sh
ls /sys/class/bluetooth/                  # expect hci0
btmgmt info                               # expect a controller block with a BD address
bluetoothctl show                         # expect "Controller <addr>" not "No default controller"
journalctl -k -b | grep -iE 'Bluetooth: hci0|Intel firmware'   # expect no "Failed to load"
tools/oxp3-bt-firmware.sh --check         # expect "radio looks healthy"
```

What a good load looks like, using the driver's own templates (`strings
btintel.ko`): `Intel Bluetooth firmware file: intel/ibt-00a0-0291-iml.sfi`, then
the `-pci.sfi` load, optionally `Applying Intel DDC parameters completed`, then
`Device booted in <n> usecs` and a `Firmware Version: 0x…` line. The `Failed to
read MSFT supported features (-56)` message should disappear with the firmware
in place.

## Rollback

```sh
sudo rm -f /usr/lib/firmware/updates/intel/ibt-00a0-0291-iml.sfi \
           /usr/lib/firmware/updates/intel/ibt-00a0-0291-pci.sfi
sudo rm -f /etc/dracut.conf.d/60-oxp3-bt-firmware.conf
sudo rpm-ostree initramfs --enable && sudo reboot      # drop the initramfs change
# or revert the whole pin:  sudo rpm-ostree uninstall oxp3-firmware && sudo reboot
```

If the files only live in a `usroverlay`, a reboot already reverts them. Note
`oxp3-firmware` also carries the pinned display DMC and the `06-cc-02`
microcode, so uninstalling it changes more than Bluetooth.

## Open points

* **The fix is not yet exercised.** This session had no `sudo`, so A and B are
  unverified on the machine. A is low risk and reversible (`modprobe` cycle);
  if the reload still fails, capture `journalctl -k -b | grep -i
  'Bluetooth: hci0'` — a version/handshake rejection looks different from
  `-ENOENT` and would point at the IML/pci split rather than the search path.
* **Version choice not proven necessary.** `updates/` carries the 24.80 build
  while the distro ships an older one; both may load fine. The pin exists to
  match the "Bluetooth 24.80" parity the package promises, not because 24.80 was
  shown to be required on this unit.
* **The `.ddc` question.** Upstream's `intel/ibt-00a0-0291-pci.ddc` is a 4-byte
  stub and the distro already supplies the same bytes via a symlink, so no
  override is installed by default. If the driver complains about DDC after the
  firmware loads, try `tools/oxp3-bt-firmware.sh --with-ddc`.
* **Upstream gap worth reporting.** `btintel_pcie` has no `MODULE_FIRMWARE()`
  entries although it only ever loads `ibt-%04x-%04x{-iml,-pci}.sfi`; adding
  them (or a dracut/BlueZ quirk) would make every early-probing initramfs work
  out of the box. Until then this dracut snippet is the portable workaround.
* **The initramfs could not be inspected directly** — `/boot/ostree/.../*.img`
  is mode `0600 root` on this host, so the "driver is inside the initramfs"
  conclusion rests on the boot ordering above, not on a file listing. Confirm
  with `sudo lsinitrd .../initramfs-$(uname -r).img | grep -E 'btintel|ibt-'`
  in the same session as the fix.
* **`kmod-oxp3` is out-of-tree.** A kernel or backport bump can change the
  requested file names (the log always prints the exact name); re-run
  `--check`/`journalctl -k -b | grep 'Intel firmware'` after such an update.
* **Initramfs cost.** Option B1 adds ~1.06 MB to the initramfs; B2 adds nothing
  but ties Bluetooth availability to a post-`switch_root` probe.
