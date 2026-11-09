# Rollback - what we touch, how undo works, and what stays behind

Date: **2026-11-09**. Scope: every side effect of `oxp3-fixes.sh`, `oxp3-fixes-fast.sh`,
`tools/oxp3-hw-install.sh` and its component installers, plus the host edits that were made by
hand before this document existed. The mechanism is `tools/lib/install-common.sh` (undo section)
and `tools/oxp3-rollback.sh`; the tests are `tests/unit/test_backup.py` and
`tests/rollback/run.sh`.

The short version:

```sh
# 1. record what was edited by hand (idempotent, writes only the state directory)
tools/oxp3-rollback.sh adopt

# 2. the normal uninstall paths
sudo tools/oxp3-hw-install.sh --uninstall --all      # hardware fixes, keeps user config
./oxp3-fixes-fast.sh uninstall                       # driver stack + layered packages

# 3. put every modified file back, byte-identical
tools/oxp3-rollback.sh restore                       # add --dry-run to look first
```

`tools/oxp3-rollback.sh restore` is *the* one command for the in-place edits. It is safe to
re-run: a second run reports "already restored" and changes nothing.

## 1. What the installers change

Every path below is created or modified by an installer in this repository. "Undo" is the
command that reverses it; `restore` means `tools/oxp3-rollback.sh restore`.

| # | what | where | undo |
|---|---|---|---|
| 1 | layered packages `gamescope`, `mangohud`, `mesa-vulkan-drivers`, `kmod-oxp3-*`, `oxp3-firmware` | rpm-ostree deployment | `do_uninstall` (`rpm-ostree uninstall`) |
| 2 | initramfs regeneration switch | `rpm-ostree initramfs --enable` | `do_uninstall` (`--disable`) |
| 3 | writable `/usr` overlay: Mesa libs, patched gamescope/MangoHud, patched hhd files, Flatpak runtime overrides + `oxp3-mesa.list`, `84-oxp3-volume-keys.hwdb` | `/usr` (`usroverlay`) | dropped by the next deployment; `hhd_volume_unpatch` + `restore` for the hhd files |
| 4 | firmware blobs `ibt-00a0-0291-{iml,pci}.sfi`, `-pci.ddc` | `/usr/lib/firmware/updates/intel/` | `--uninstall --bluetooth` (or the `oxp3-firmware` rpm) |
| 5 | 9 tools | `/usr/local/libexec/` (`rapl-power`, `oxp3-tdp-sync`, `oxp3-xpad-sleep`, `oxp3-audio-route`, `oxp3-sleep-wakeup`, `oxp3-sleep-log`, `oxp3-powerctl`, `oxp3-adaptivetdp`, `oxp3-voltap`) | uninstall paths remove them |
| 6 | 14 system units | `/etc/systemd/system/` (`rapl-power`, `oxp3-tdp-sync`, `oxp3-xpad-sleep`, `oxp3-rapl-pp1`, `oxp3-sleep-{wakeup,pre,log}`, `oxp3-audio-resume@`, `oxp3-gaming-sched`, `oxp3-power-{profile,resume}`, `oxp3-fan-curve`, `oxp3-adaptivetdp`, `oxp3-voltap`) | disabled and removed by the uninstallers |
| 7 | one system-wide user unit | `/etc/systemd/user/oxp3-audio-route.service` | `uninstall_extras` |
| 8 | 2 udev rules | `/etc/udev/rules.d/90-oxp3-hid.rules`, `99-oxp3-sleep-wakeup.rules` | removed (HID rule only when neither daemon needs it) |
| 9 | 2 hwdb files | `/etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb`, `/usr/lib/udev/hwdb.d/84-oxp3-volume-keys.hwdb` | uninstallers remove them + `systemd-hwdb update` |
| 10 | BMI260 blacklist (**modified in place**) | `/etc/modprobe.d/oxp3-bmi160.conf` | backed up + `restore`; a differing file is never deleted |
| 11 | speaker name | `/etc/wireplumber/wireplumber.conf.d/52-oxp3-speakers.conf` | `uninstall_extras` |
| 12 | logind drop-in (power key) | `/etc/systemd/logind.conf.d/60-oxp3-sleep.conf` | `--uninstall --sleep`; logind keeps the old policy until restarted |
| 13 | polkit rule (suspend) | `/etc/polkit-1/rules.d/49-oxp3-suspend.rules` | `--uninstall --sleep`; polkitd re-reads rules on its own |
| 14 | dracut drop-in (**modified in place**) | `/etc/dracut.conf.d/60-oxp3-bt-firmware.conf` | backed up + `restore`; falls back to removal when we created it |
| 15 | volume-daemon config | `/etc/oxp3-voltap.conf` | kept by design (`--purge-config` removes it) |
| 16 | power state files | `/etc/oxp3/{power.json,baseline,fan-curve}` | kept by design; live limits undone by the uninstaller |
| 17 | thermald marker + disable | `/etc/oxp3-fixes/thermald-disabled`, `thermald.service` | `uninstall_extras` re-enables it |
| 18 | adaptive-TDP decision log | `/var/log/oxp3-adaptivetdp.jsonl` | kept; delete by hand |
| 19 | 2 user units | `~/.config/systemd/user/{oxp3-homebtn,oxp3-rgbd}.service` | `--uninstall --leds/--rgb` |
| 20 | RGB config | `~/.config/oxp3-rgb.toml` | kept (`--purge-config` removes it) |
| 21 | speaker EQ drop-in | `~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf` | `eq-off.sh` / `--uninstall --audio` |
| 22 | game wrapper config (optional) | `~/.config/oxp3-gamewrap.toml` | user-owned example; delete by hand |
| 23 | **Heroic launcher (modified in place)** | `~/heroic-launch.sh` | `restore` (adopted from `~/heroic-launch.sh.bak-*`) |
| 24 | **Heroic GamesConfig (modified in place)** | `~/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig/*.json` | `restore` (per-game `envVars` cleanup) |
| 25 | XMX user environment | `~/.config/xess-xmx/{oxp3-env.sh,flatpak-env.sh,icd-*.json}` | `restore` removes the adopted copies |
| 26 | vibrance config | `~/.config/oxp3-vkbasalt.conf` | `restore` removes the adopted copy |
| 27 | XMX user units (created outside the repo) | `~/.config/systemd/user/xess-xmx-*` | `restore` removes them |
| 28 | XMX release tree / compiler | `~/.local/share/xess-xmx-release/` | kept (~300 MB); delete by hand |
| 29 | XMX driver-status cache | `~/.cache/xess-xmx/` | cache only; delete by hand |
| 30 | shim inside Proton prefixes | `<prefix>/drive_c/windows/system32/driverstore/.../igdext64.dll` | `tools/oxp3-flatpak-xmx.sh remove` (it keeps `.stock` copies) |
| 31 | Steam Flatpak overrides | `~/.local/share/flatpak/overrides/org.anatase.Steam` | remove by hand; XMX env is per-game now |
| 32 | MOK signing key + enrolment | `$MOK_DIR` (outside the repo), firmware MokList | keep the key; removing an enrolled MOK needs `mokutil` + MokManager in the firmware |
| 33 | runtime/EC state | RAPL PL1/PL2/PL4 (sysfs + MSR), `charge_control_end_threshold`, `charge_behaviour`, `platform_profile`, fan `pwm_enable`, wakeup sysfs, `udevadm trigger`, `systemd-hwdb update`, `systemctl restart` of hhd/bluetooth/rapl-power/logind/pipewire/wireplumber, default PipeWire sink | see §4; the uninstaller now undoes the power state first |

That is **33 families / about 64 concrete paths**, plus the runtime state in row 33. 28 rows are
files we create and remove ourselves; rows 10, 14, 23 and 24 are files we modify in place and
are the reason the undo state exists.

## 2. The undo state (backups + manifest)

A file is backed up *before* it is changed, by the same library the installers use
(`oxp3_backup_image`, `oxp3_manifest_record` in `tools/lib/install-common.sh`):

```
<state>/backups/<UTC stamp>[-N]/<key>   the previous content, cp -a (bytes, mode, mtime)
<state>/manifest.tsv                    one row per edit, append-only:
  ts  tool  event  key  logical  real  post_sha  note
```

* `event` is `backup` (file existed, previous content saved), `created` (we created it; undo
  removes it), `adopted` (a pre-existing edit of ours, pre-image from a sibling `.bak*` when
  one exists), `restored` or `removed`.
* `key` is `home/<rel>` under the target user's home, else `abs/<path>`. An absolute
  `OXP3_ROOT` prefix is stripped, so `--root DIR` simulations produce the same keys as the host.
* `<state>` is `${XDG_STATE_HOME:-~/.local/state}/oxp3-fixes`; with `sudo` it is the invoking
  user's state directory (so a root-run uninstall restores the same manifest), and
  `/var/lib/oxp3-fixes` only when there is no sudo session.
* `post_sha` is what we wrote. Restore compares the current file with it: equal means "undo it";
  different means *the user edited after us*, so it is left alone and reported (exit 3).
  `--force` first saves the user's version as a new backup, then restores the original.

Restoring a file we created removes it only while it still matches `post_sha`. Nothing is ever
restored over a file the user changed without that file being preserved first.

Commands:

```sh
tools/oxp3-rollback.sh status [--json]        # records + what each file holds now
tools/oxp3-rollback.sh list                   # backup directories
tools/oxp3-rollback.sh verify [--json]        # read-only: which of our edits have no backup
tools/oxp3-rollback.sh adopt                  # record hand-made edits retroactively
tools/oxp3-rollback.sh apply                  # install/refresh ~/heroic-launch.sh (backed up)
tools/oxp3-rollback.sh restore [--force] [--dry-run] [--only SUBSTRING]
tools/oxp3-rollback.sh restore-file PATH      # used by the Bluetooth uninstaller
```

Exit codes: `0` ok (nothing left to undo is ok), `1` failure, `2` usage, `3` something was left
in place because it changed after ours.

## 3. The hand-made host edits this adopts

Before this mechanism existed, an agent session edited two host files by hand:

* `~/heroic-launch.sh` - the Cyberpunk/Heroic wrapper. It is currently **identical** to
  `tools/heroic-launch.sh` (`f4dbd274…`) and `~/heroic-launch.sh.bak-1716` holds the launcher
  that was there before the wrapper.
* `~/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig/1423049311.json` - per-game
  XMX `envVars` were written by hand; `.bak-2053` holds the file from before that edit.

`adopt` records both (using the sibling `.bak*` as the pre-image) plus the user-owned XMX files
an earlier session created outside the repo (`~/.config/xess-xmx/*`, `~/.config/oxp3-vkbasalt.conf`,
`~/.config/systemd/user/xess-xmx-*`). `tools/oxp3-hw-install.sh` calls `adopt` itself whenever it
finds the wrapper wiring already present, so an install after this change captures the history
without any extra step. `adopt` never modifies the host files; it only copies and records them.

`tools/oxp3-rollback.sh verify` is the read-only alarm: it lists every known edit that has no
backup (`UNBACKED`), every manifest row whose image file is gone (`MISSING-IMAGE`) and every file
that changed after we wrote it (`edited-since`, informational). It exits 1 while anything is
unbacked. On this host it fails until `adopt` has been run once.

## 4. What an uninstall now does that it did not before

* **Battery and power baseline** - `--uninstall --power` clears the charge limit and
  charge-inhibit and re-applies the saved RAPL baseline *before* removing `oxp3-powerctl`, because
  the EC keeps the limit and the tool is the only way to clear it. If that fails the firmware
  re-initialises the RAPL limits at the next reboot; the EC limit must then be cleared by hand.
* **Dracut drop-in** - restored from its backup instead of being deleted, so a pre-existing
  file comes back byte-identical.
* **BMI260 blacklist** - restored from its backup; a file that differs from ours is kept with a
  warning instead of being deleted.
* **hhd Python files** - the reverse patch is still the primary undo, but every file a patch
  touches is copied first. If a reverse patch no longer applies (a newer hhd), the message points
  at `sudo tools/oxp3-rollback.sh restore`, which has the exact pre-patch bytes.
* **Heroic launcher and GamesConfig** - `restore` puts the adopted pre-edit content back; nothing
  in the installers owned these files before.
* **`--uninstall --all`** prints the `tools/oxp3-rollback.sh restore` command so the in-place
  edits are not forgotten.

## 5. What is NOT undone

| item | why | manual step |
|---|---|---|
| rpm-ostree deployment history / staged deployments | ostree keeps every deployment | `sudo rpm-ostree cleanup -p` (or -r) once the previous deployment is booted |
| kernel arguments (`/proc/cmdline`) | none of the installers touch `rpm-ostree kargs`; a hand-added argument is not ours | `sudo rpm-ostree kargs --delete=<arg>` then reboot |
| an enabled initramfs regeneration | undone by `do_uninstall`; a deployment that already regenerated keeps that initramfs until the next deployment | reboot after `rpm-ostree initramfs --disable` |
| firmware blobs already copied into an existing initramfs | the initramfs is immutable per deployment | rebuild it (`sudo rpm-ostree initramfs --enable` and reboot) or reboot into a new deployment |
| the `/usr` overlay itself | it is a boot-local mount; an unstaged deployment starts from the stock `/usr` | `rpm-ostree usroverlay --discard` or reboot |
| an enrolled MOK key | it lives in the firmware's MokList, not on disk | `mokutil --delete <MOK.der>`, then MokManager at the next boot |
| `/etc/oxp3/`, `/etc/oxp3-voltap.conf`, `~/.config/oxp3-rgb.toml`, `~/.local/share/xess-xmx-release/`, `/var/log/oxp3-adaptivetdp.jsonl` | deliberately kept (config/data, not state) | `--purge-config` for the first two/three, `rm -rf` for the rest |
| edits made in the Heroic GUI *after* our adoption | that is new user state, not ours | Heroic's own settings |
| Steam non-Steam shortcut entries (Target = `~/heroic-launch.sh`) | Steam library database | delete the shortcut in Steam |
| Flatpak Steam override file, per-game launch options | chosen by the user per game | Steam Properties / `flatpak override --unset-env` |
| thermald disable marker | restored by `uninstall_extras` | nothing |
| battery-cycle history / EC firmware counters | written by the EC itself | not reversible |

## 6. Verified offline

`tests/rollback/run.sh` installs into a `--root` prefix, adopts, edits `~/heroic-launch.sh`-style
files and a Heroic GamesConfig JSON, uninstalls and asserts a byte-identical restore (content
and mode), idempotency of a second run, and that the state directory stays inside the prefix.
`tests/unit/test_backup.py` covers the library and the tool directly: image bytes/mode, key
mapping, manifest rows, created-file removal, user-edit detection, `--force`, `restore-all` exit
codes, dry runs and `status --json`. Both run in `tools/repo-test.sh --fast` (`rollback`, `unit`).

## 7. Open points

* The adoptable artifact list in `tools/oxp3-rollback.sh` (`agent_created_paths`) is a fixed list
  of what earlier sessions created. A future session that creates more user files must add them
  there (or call `backup`/`record` directly) - `verify` cannot know about paths nobody recorded.
* `tools/oxp3-flatpak-xmx.sh` keeps its own `STATE` + `.stock` scheme for the Proton-prefix shim.
  It is redundant with this manifest but not merged: its `remove` command already restores the
  `.stock` copies, and merging would change a working tool for no functional gain.
* Restoring a system file needs root (`sudo tools/oxp3-rollback.sh restore`); without it the
  restore reports `failed … permissions? run with sudo` and changes nothing.
* `restore` removes adopted artifacts such as `~/.config/xess-xmx/oxp3-env.sh`. That is the
  honest undo (we created them), but it also removes the XMX environment - use
  `--only heroic-launch` (or `--dry-run` first) when only the launcher should go back.
