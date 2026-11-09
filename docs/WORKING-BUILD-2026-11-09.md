# WORKING BUILD - 2026-11-09 (recorded as known-good)

Recorded after the owner confirmed, in Gamemode: **performance back to the October level** and **PQ HDR working again**.
This is the configuration that produced those results. `tools/check-working.sh` verifies it.

Machine: OneXPlayer 3 (DMI `ONEXPLAYER 3`, BIOS 5.09), Arc B390 (8086:b080) Panther Lake, Anatase, kernel
7.2.7-an28.fc45.x86_64, HHD 4.1.12, Decky loader with oxp3-status 1.2.0.

## What makes performance and PQ HDR work

| piece | value / path | verification |
|---|---|---|
| host Mesa (patched, XMX overrides) | sha256 `ec418f04e589965adeb26da96fb282915e3dfd7ab98b0d6eab69f9878f3578e9` `/usr/lib64/libvulkan_intel.so` | `check-working.sh` |
| Flatpak runtime Mesa | sha256 `4d043cc2851a11fd6637feaf2748e99211dc132b46710f72a1e49fa5bea30106` under `…/org.anatase.Platform/…/files/lib64/`, with the override present | `check-working.sh` |
| **gamescope (PQ HDR patch)** | sha256 `9205b50c33e96f60915dc121d04df38ae3f0ef50e75ecae888450e978cc8588f` `/usr/bin/gamescope`, i.e. the `gamescope-3.16.23.3-1.fc45.oxp3.202610292302` build from `patches/gamescope/0001-oxp3-hdr-pq.patch` | `check-working.sh`, `gfx.gamescope` |
| **Gamemode HDR/PQ launcher** | patched copy `/usr/local/libexec/anatase-gamemode-hdr` (`--hdr-enabled --hdr-pq-internal-enable --hdr-sdr-content-nits 200`) + drop-in `/etc/systemd/user/anatase-gamemode.service.d/90-oxp3-hdr.conf` | `dsp.gamemodehdr` |
| keep units (reboot-safe) | `oxp3-mesa-keep`, `oxp3-gamescope-keep`, `oxp3-mangohud-keep` enabled, re-applied before the display manager | `check-working.sh` |
| XMX / XeSS | per-game only (`tools/xmx-run.sh`, `tools/oxp3-flatpak-xmx.sh install --appid`); **no session-wide `VK_DRIVER_FILES`** - that put gamescope on the CM driver and broke PQ HDR | `gfx.xmx.*` |
| scheduler | `oxp3-gaming-sched` (scx_cosmos) active; with the patched gamescope this is the measured-good state. `sudo systemctl disable --now oxp3-gaming-sched` returns to the stock scheduler for A/B | `systemctl is-active` |
| power | PL1 35 W both zones, `platform_profile=balanced`, fan auto | `pwr.*` |
| audio | ALSA `Speaker` 87/87, sink `oxp3_speaker_eq.sink` at 130 %, loud EQ profile | `aud.level`, `aud.model` |
| Bluetooth | pinned `ibt-00a0-0291-*.sfi` in `/usr/lib/firmware/updates/intel/`, initramfs items + boot reload | `bt.*` |
| suspend | `nvme.noacpi=1` for the Predator GM7 (`1dee:1602`), s2idle policy units | `sleep.nvmeacpi` |

## How this state is installed from scratch

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
sudo tools/oxp3-nvme-suspend.sh apply
sudo tools/oxp3-hw-install.sh --all --audio-activate --restart-plugin-loader
sudo reboot
```

`--all` covers: volume (HHD plugin), sleep policy, Bluetooth firmware + boot reload, power/fan, IMU blacklist, LEDs and
home button, RGB (HHD-owned), speaker EQ + gain staging, scheduler, host Mesa restore + keep unit, **gamescope PQ HDR
build + keep unit**, **Gamemode HDR launcher override**, XMX env/per-game wiring, MangoHud patched build + config (Intel
iGPU power/temperature), and Decky 1.1/1.2. The gamescope build is extracted from the cached
`~/.cache/oxp3-fixes/out/gamescope-*.oxp3*.rpm`; if those are missing, rebuild with `./oxp3-fixes-fast.sh build`.

Reboot is safe now: gamescope, Mesa and MangoHud are restored by keep units, and the HDR launcher override lives in `/etc`
and `/usr/local` - nothing in this list depends on the volatile `/usr` overlay surviving.

## What went wrong before (so it is not repeated)

* The gamescope PQ patch and the HDR launcher flags were applied **only** through the rpm-ostree `/usr` overlay. Any reboot
  wiped both, and `--all` did not reinstall gamescope, so Gamemode fell back to the stock `lodos` build: black screen or
  washed-out HDR. Keep units + the `/usr/local` launcher copy remove that failure mode.
* A session-wide `VK_DRIVER_FILES=icd-xmx.json` (from `tools/xmx-session.sh`) also applied to gamescope itself; the XMX/CM
  driver does not implement the PQ composition path. XMX is per-game again, and that session file is gone.
* The scheduler is the one performance-affecting toggle: scx_cosmos is active in this recorded state; the A/B is one
  `systemctl disable --now oxp3-gaming-sched` away.

## Verifying and reporting

```sh
tools/check-working.sh          # this file's table, hashes and units
tools/oxp3-verify.sh            # full 80+ check matrix
```

In Gamemode, the Decky panel's **Save report JSON** button now writes
`~/oxp3-reports/oxp3-report-<UTC>.json` (and `~/oxp3-reports/latest.json`) instead of using the clipboard, so the report can
be handed to an agent or attached to an issue.
