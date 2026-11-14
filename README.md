# OneXPlayer 3 fixes for Anatase

Everything needed to make a **OneXPlayer 3** (Intel Panther Lake, Arc B390, `xe`) run properly on
[Anatase Linux](https://github.com/anatase-org/anatase): working HDR, the performance the hardware
can actually deliver, usable controls/audio/sleep, real XeSS XMX upscaling, and a live status panel.

The repo rebuilds Anatase's own packages from source and adds a small, reviewable patch set; no
vendor binaries are vendored and every change is reversible. It is validated on the **Anatase
Stable** channel for OXP3 (see the required version below).

**Install in two commands:**

```sh
./oxp3-install.sh        # build everything, stage kernel modules/firmware, queue the signing key
systemctl reboot         # enrol the key in the blue MOK screen if it asks
./oxp3-install.sh        # finish: driver stack, hardware fixes, verification
```

<small>First build is long (about 1.5–2 h; Mesa for x86_64 and i686 is most of it). Later builds are
much faster and you normally run the script only after an Anatase update.</small>

---

## What this fixes and improves

### Display and HDR
- **PQ HDR on the internal OLED** with no black screens, real peak brightness and a working HDR
  brightness slider (patched gamescope; the stock build leaves HDR black or washed out).
- **VRR and frame pacing** fixes, 30–144 Hz modes, and HDR metadata taken from the panel's EDID.
- Correct behaviour when toggling HDR while VRR is active.

### Performance
- **Mesa main + performance patches**, rebuilt for the exact kernel/driver stack: faster mesh
  shading, ray tracing close to Windows, fewer driver ioctls per frame.
- **Gaming CPU scheduler** (`scx_cosmos`) where available, with EEVDF fallback.
- GravityMark 1.89 (50k asteroids, 1280×720): stock Anatase ≈223 FPS raster / ≈124 FPS RT;
  after this stack ≈268 / ≈180, with mesh shading slightly **above** the Windows reference.
  Details and caveats: [docs/BEST-CONFIG.md](docs/BEST-CONFIG.md).

### XeSS XMX (Arc matrix units)
- Real **XeSS super resolution and frame generation on the XMX units** instead of the slow DP4a
  fallback, per game, in native Steam and Heroic.
- Dedicated Mesa 26.3.0 XMX driver built with the upstream CM series plus our performance patches;
  the desktop/Gamescope stay on Mesa main so HDR keeps working.
- Proton shim, compiler and kernel cache handled automatically; first launch compiles kernels once.

### Controls, lighting, audio
- **Volume rocker** reliable (hhd plugin + kernel key fix).
- **Home button and LEDs**, body RGB owned by hhd, and a reconciler that keeps the **left-stick
  LED in sync** with hhd's colour.
- **Harman speaker EQ**, full speaker loudness at 100 %, and a **working internal microphone**
  (real default source + unmuted codec capture switch).

### Power, sleep, thermals
- Suspend/wake policy that actually reaches s2idle, NVMe suspend fix, battery/fan/power limits
  synchronised with hhd's TDP, and power readings exposed to the overlay without opening RAPL.
- Patched kernel modules (xe display/VRR/resume, iwlwifi, btintel) and firmware (DMC, Bluetooth,
  CPU microcode) layered as a signed `kmod-oxp3` + `oxp3-firmware`, without replacing your kernel.

### Status and diagnostics
- **Decky plugin / CLI**: live Mesa, XMX, scheduler, power and hardware-fix state in the Steam
  Quick Access Menu, plus one-click JSON reports.
- `tools/oxp3-health.sh` — “can I install / is it working / what is left” in one read-only command.

### Known gaps
SAGV/memory-frequency switching has no Linux control; Flatpak Steam has its own XMX deployment;
bluetooth/IMU edge cases are documented. See [docs/hw/README.md](docs/hw/README.md) and
[docs/HANDOVER.md](docs/HANDOVER.md) for the honest state matrix.

---

## Install

### Required Anatase version

Use **Anatase Stable `20261109.7`** (release `stable-20261109.7`, published 2026-11-12), image:

```
i.anatase.org/anatase:stable
sha256:8069bddc50d12483cf25a3bb2e1b04c2d628912aa1864f18b0604d4b8c1c3c86
```

This is the version the current kernel/firmware and package patches are verified against. A newer
Stable release should work, but re-run the installer after updating and check
`tools/check-working.sh`.

Check what you are on:

```sh
cat /etc/os-release | grep -E '^(NAME|VERSION)='
rpm-ostree status --booted | grep -E 'Version|Digest|Unverified'
```

#### Install Anatase (new device)

1. Download the **Stable installation ISO** from <https://anatase.org/#download> (8 GB+ USB stick).
2. Flash it with balenaEtcher/Rufus (Windows), KDE ISO Image Writer (Linux), or Ventoy.
3. Boot the stick, enrol the **Anatase MOK key** when Secure Boot asks (a keyboard is required on
   OXP3), then run **Install Anatase**. Use *Share Disk* for dual-boot or *Use the entire drive* for
   a dedicated install.
4. After the first boot, run the commands above and make sure the Stable channel/version matches.

#### Update an existing Anatase install

```sh
sudo bootc update
systemctl reboot
```

To move a different channel/rebase onto Stable:

```sh
sudo bootc switch i.anatase.org/anatase:stable
systemctl reboot
```

Do not use the Testing/Rolling channel with this repo; the exact kernel and package pins matter.

### Requirements
- A **OneXPlayer 3** running the Stable Anatase version above (rpm-ostree/bootc, Fedora-based),
  fully updated and rebooted.
- `podman`, `git`, ~8 GB free disk and network for the first build.
- A normal user account with `sudo` (run the script as that user, not as root).
- Optional: Decky Loader (for the status plugin), Steam (for XMX launch options and the plugin).
- Secure Boot: the script queues a **Machine Owner Key** and asks you to choose a one-time MOK password and enrol it once.

### Step 1 — first run

```sh
git clone https://github.com/silentsudin/oxp3-anatase-fixes
cd oxp3-anatase-fixes
./oxp3-install.sh
```

It checks the machine, creates/queues the MOK key, builds gamescope/Mesa/MangoHud/XMX/kernel
modules/firmware, and stages the kernel modules + firmware.

### Step 2 — set the MOK password, reboot and enrol

The first run creates and queues a **Machine Owner Key (MOK)** for signing the kernel modules. If
the key is not enrolled yet, `mok-setup` prints:

```text
Enrolling: choose a one-time password, then reboot and in MokManager pick
Enroll MOK -> Continue -> Yes -> enter the password.
input password:
input password again:
```

**This password is new and chosen by you here** — it is not your Linux login password and it is not
stored anywhere. Type the same one twice; remember it for the reboot. If that prompt never appears,
the key is already enrolled and you can skip this enrolment.

```sh
systemctl reboot
```

At the blue **MOK Manager** screen:

1. *Enroll MOK*
2. select the OXP3 key (`CN = OXP3 module signing CA (<your user>)`)
3. *Continue* → *Yes*
4. enter the **same one-time password you set above**
5. reboot

Secure Boot stays enabled after this. If the MOK Manager does not appear, Secure Boot is off or the
key was already enrolled. The private key stays at
`~/.local/share/oxp3-fixes/mok/MOK.key` — keep it private and never commit it.

Verify after logging in:

```sh
mokutil --test-key ~/.local/share/oxp3-fixes/mok/MOK.der   # "is already enrolled"
tools/check-working.sh                                     # kernel module/driver rows
```

If you lose the password, the key is still usable for a future module build but cannot be enrolled
in that boot: run `sudo mokutil --import ~/.local/share/oxp3-fixes/mok/MOK.der` to queue it again
with a new one-time password.

### Step 3 — second run

```sh
./oxp3-install.sh
```

This installs the overlay stack (gamescope, Mesa, MangoHud, XMX env), applies the hardware fixes
(volume/LEDs/RGB/sleep/Bluetooth/power/IMU/audio/Decky), and runs the verification suite.

#### Harman APO speaker tuning (the only prompt)

During the audio step the installer asks:

```text
Path to the Harman APO files (folder, .zip or GA18v3.reg) [~/Downloads/Harmon APO]:
leave blank to skip the vendor EQ:
```

Point it at the **Harman APO files you downloaded** — the extracted vendor folder, a `.zip` of that
folder, or just `GA18v3.reg`. **No Windows installation is required.** The installer:

1. SHA-256-checks the files against the validated OneXPlayer 3 package:
   `GA18v3.reg 32cfd82c…`, `AudioEFXService64.exe e4c28d17…`, `AudioEFXAPO64.dll 72e65578…`;
2. extracts them only on your machine under
   `~/.local/state/oxp3/audio/harman/`;
3. generates the speaker EQ from your own extracted preset and installs it.

Leave the prompt blank to keep the recorded reproduction. For automation, add
`--harman PATH` to the **second** run (or to the first, which remembers it for the second);
`--harman-force` accepts a different vendor revision, and `--no-harman` skips without prompting.

Then leave and re-enter **Gamemode** once.

### Post-install

```sh
tools/oxp3-health.sh          # one-line / detailed health
tools/check-working.sh        # recorded working build checks
tools/oxp3-verify.sh          # full 220-check report
```

Start Steam once, then make XMX available to its XeSS games:

```sh
tools/xmx-steam-enable.sh --all    # per-game launch options; restart Steam afterwards
```

The **first game launch after a Mesa install is CPU-bound** while shader caches rebuild (a few
minutes); later launches are normal. Enable XeSS in the game's own graphics menu to use XMX.

### Options

| Option | Meaning |
|---|---|
| `--status` | show the detected phase and current state, change nothing |
| `--dry-run` | print every command without running it |
| `--skip-build` | reuse existing build artifacts |
| `--skip-hw` | driver stack only, no hardware fixes |
| `--skip-mok` | install without kernel modules (no signing key) |
| `--uninstall` | remove everything and restore backed-up files |
| `--force` | recreate/queue the MOK key |
| `--harman PATH` | use downloaded Harman APO folder/zip/reg (remembered across runs) |
| `--harman-force` | accept an unvalidated Harman vendor revision |
| `--no-harman` | skip the vendor EQ prompt |

Prefer driving each stage yourself? The underlying commands are still documented in
[docs/INSTALL-HW.md](docs/INSTALL-HW.md) and `./oxp3-fixes.sh --help`.

---

## After an Anatase update

```sh
./oxp3-install.sh
```

The script detects whether the kernel changed. If it did, it stages modules/firmware and asks for
the same reboot + second run; if not, one run is enough. The `/usr` overlay is re-applied on every
boot by keep units, and the patched kernel/firmware deployment stays until you change kernels.

## Uninstall / rollback

```sh
./oxp3-install.sh --uninstall
systemctl reboot
```

Every module/firmware install creates a new rpm-ostree deployment; the previous one stays in the
boot menu (`sudo rpm-ostree rollback` also works). In-place edits (Heroic config, dracut, hhd) are
backed up and restored by `tools/oxp3-rollback.sh restore`. Details: [docs/ROLLBACK.md](docs/ROLLBACK.md).

## Troubleshooting

| Symptom | What to do |
|---|---|
| Script stops at the MOK screen | Enrol the key, reboot, run the script again |
| First game is slow / CPU pinned | Shader-cache warm-up; wait once, then relaunch |
| HDR is black or washed out | Re-enter Gamemode; verify `gfx.gamescope` and the HDR override in `tools/oxp3-verify.sh` |
| XeSS looks like DP4a | Check `gfx.xmx.*` in verify, `XESS=latest %command%` for old XeSS SDKs, and that Steam was restarted |
| Mic appears disabled | `aud.source`/`aud.capture` in verify; re-run `tools/audio/install-user.sh` |
| Decky plugin missing | Decky Loader installed? `sudo tools/oxp3-hw-install.sh --decky --restart-plugin-loader` |
| Anything unclear | `tools/oxp3-health.sh`, then [docs/HANDOVER.md](docs/HANDOVER.md) |

## What gets installed

<!-- BEGIN GENERATED: readme-summary (source: docs/hw/components.yaml; regenerate: tools/gen-docs.py --write) -->
| Part | State | Fixes | How it is installed |
|---|---|---|---|
| Gamescope | working | PQ HDR on the internal SDC AMS881KB01 OLED with no black screens, real peak brightness and the HDR brightness slider; VRR frame-pacing fixes from gamescope 3.16.29/3.17.0 and 30-144 Hz modes, from patches/gamescope. | Anatase's gamescope rebuilt with patches/gamescope and installed into the writable /usr overlay (`./oxp3-install.sh`) |
| Mesa | working | Mesa main at the current pin (62b0e52f): Jay shader compiler (default on Xe2/Xe3), ANV compression/tiling, the Panther Lake hang fix, HPLOC TLAS, mesh fixes and fewer per-frame driver ioctls (patches/mesa 0200/0201/0206/0207/0212). | Anatase's Mesa package rebuilt at the pinned main commit with patches/mesa (x86_64 + i686), same overlay (`./oxp3-install.sh`) |
| XeSS XMX | working | XeSS SR/FG on the Arc XMX units instead of DP4a for native Steam and Heroic, with a dedicated Mesa 26.3.0 driver (release CM patches + our perf patches) selected per game; the host stays on Mesa main so gamescope/HDR use the fast driver. The package shim, ControlLib and IGC compiler are installed per game. Flatpak Steam has its own deployment (docs/hw/flatpak-xmx.md). | Mesa 26.3.0 XMX driver built and installed per game, shim/ControlLib/IGC staged, per-game ICD and launch options written (`./oxp3-install.sh (hardware step runs tools/oxp3-hw-install.sh --xmx)`) |
| MangoHud | working | CPU power, GPU power and VRAM in the Gamemode overlay on Intel iGPUs. | Anatase's MangoHud rebuilt with patches/mangohud, same overlay (`./oxp3-install.sh`) |
| Kernel modules | working | xe (VBT VS/PE-O backport, 7.3 VRR/DSB flip timing, resume out-of-memory fix, 7.2.8-7.2.13 stable fixes, IBC v3 on PTL, eDP HDR switches) plus iwlwifi/iwlmld and btintel/btintel_pcie fixes, built for the installed kernel and layered as kmod-oxp3. | built for the installed kernel, signed with your MOK, layered as kmod-oxp3 with a regenerated initramfs (`./oxp3-install.sh`) |
| Firmware | working | Display DMC v2.20 (v2.36 caps VRR near 74 fps), Bluetooth 24.80 and CPU microcode 0x11c, layered in /usr/lib/firmware/updates. | layered as oxp3-firmware together with the modules, in /usr/lib/firmware/updates (`./oxp3-install.sh`) |
| rapl-power | working | CPU/GPU power for the overlay without opening the RAPL counters to users; also publishes dram and psys so the SAGV/memory-frequency effect is measurable without root. | /usr/local/libexec plus a system unit, installed by the driver installer (`./oxp3-install.sh`) |
| sched_ext (scx_cosmos) | working | scx_cosmos measured +4.8% Cyberpunk average and +5.7% FPS/W over EEVDF with GravityMark unchanged; the gaming-scheduler unit is installed by the fast installer. | installed and enabled by the fast installer when scx_cosmos is present, with an EEVDF fallback (`./oxp3-install.sh`) |
| SAGV / memory frequency | blocked | No Linux control exists: Windows pins the SAGV/QCLK-GV point through an MCHBAR register pair, while Linux only has the boot-time xe.enable_sagv switch, which can only disable. | nothing is installed; the probe is read-only and needs no root |
| Decky status plugin | working | A Decky Loader plugin and a headless CLI over one backend, showing live Mesa, XMX, sched_ext and hardware-fix status in the Steam Quick Access Menu. | a deterministic plugin zip (built offline, or an existing one verified against the recorded sha256) installed into the root-owned Decky plugins directory, with the previous copy backed up (`sudo tools/oxp3-hw-install.sh --decky`) |
<!-- END GENERATED: readme-summary -->

## How it works (short version)

- **Built, not downloaded:** Anatase's gamescope, Mesa, MangoHud and the kernel modules are
  rebuilt from source with `patches/` applied; firmware and vendor DLLs are fetched at build time
  and pinned by SHA-256. No vendor binaries are committed.
- **Two layers, both reversible:** kernel modules + firmware are layered as
  `kmod-oxp3`/`oxp3-firmware` (a new rpm-ostree deployment); gamescope/Mesa/MangoHud/XMX live in
  the writable `/usr` overlay and are restored by keep units after each boot.
- **XMX is per game:** a separate Mesa 26.3.0 driver is selected through an ICD/launch options only
  for XeSS titles, so Gamescope and non-XeSS games keep the fast host driver and PQ HDR.
- **Decky/CLI share one backend** and read state without changing it; reports are redacted JSON.

## Documentation map

| Need | Read |
|---|---|
| Orientation, current state, open items | [docs/HANDOVER.md](docs/HANDOVER.md) |
| Every hardware fix, per component | [docs/hw/README.md](docs/hw/README.md) |
| One-command install / health | [docs/HEALTH.md](docs/HEALTH.md) |
| Hardware installer details | [docs/INSTALL-HW.md](docs/INSTALL-HW.md) |
| Preflight, rollback, first run | [docs/PREFLIGHT.md](docs/PREFLIGHT.md), [docs/ROLLBACK.md](docs/ROLLBACK.md), [docs/FIRSTRUN.md](docs/FIRSTRUN.md) |
| Performance and Mesa patches | [docs/BEST-CONFIG.md](docs/BEST-CONFIG.md), [patches/mesa](patches/mesa) |
| XeSS XMX | [docs/xess-xmx.md](docs/xess-xmx.md) |
| Why no vendor code is shipped | [docs/PROVENANCE.md](docs/PROVENANCE.md) |

## Licence and provenance

The repository is **MIT** licensed (see [LICENSE](LICENSE)) for our own code, scripts, units, tests
and documentation. Patches under `patches/` are derived from other projects and stay under their
upstream licences (kernel GPL-2.0, hhd LGPL-2.1+, gamescope BSD-2-Clause, Mesa/MangoHud/xess-xmx
MIT); [NOTICE](NOTICE) lists the exceptions. The provenance rule is unchanged: only our own code,
documentation and derived data are tracked, with **no vendor binaries, firmware blobs or
decompiled listings**; vendor files are fetched at build/install time and pinned by SHA-256. See
[docs/PROVENANCE.md](docs/PROVENANCE.md).
