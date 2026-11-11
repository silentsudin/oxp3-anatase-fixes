# OneXPlayer 3 fixes for Anatase

Personal patches and a rebuild script that restore and improve OneXPlayer 3 (Intel Panther
Lake, Arc B390, xe) support on [Anatase](https://github.com/anatase-org/anatase) after an
update. Everything is rebuilt from Anatase's own packaging and kernel tree plus the patches
here, so `build` + `install` reapplies it to whatever Anatase ships next.

**New here, or resuming after a break? Read [docs/HANDOVER.md](docs/HANDOVER.md) first** — it is the
one-page orientation: what is installed, what is validated, what is blocked, the corrections we had
to make, and the owner's next actions.

**Start with one command:** `tools/oxp3-health.sh` (read-only, ~7 s) answers "can I install / is it
working / what is left" from preflight + verify + the firstrun checklist + status, prints only the
non-`PASS` rows with their fix and the next 1-3 commands. `--json` for machines, `--quiet` for one
line, `--full` to pass `--full` to verify; details in [docs/HEALTH.md](docs/HEALTH.md).

**Judge the goal, not the memory:** [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) has the six-item
acceptance table (implementation, proof, manual step, verdict) and
`tools/oxp3-acceptance.sh` reproduces it from `docs/hw/components.yaml` plus the live verifier,
health and repo-test evidence. It never calls an item device-proven from offline evidence alone;
`--json`, `--verbose`, `--selftest` and `make acceptance` are the machine/test entry points.

<!-- BEGIN GENERATED: readme-summary (source: docs/hw/components.yaml; regenerate: tools/gen-docs.py --write) -->
| Part | State | Fixes | How it is installed |
|---|---|---|---|
| Gamescope | working | PQ HDR on the internal SDC AMS881KB01 OLED with no black screens, real peak brightness and the HDR brightness slider; VRR frame-pacing fixes from gamescope 3.16.29/3.17.0 and 30-144 Hz modes, from patches/gamescope. | Anatase's gamescope rebuilt with patches/gamescope and installed into the writable /usr overlay (`./oxp3-fixes-fast.sh install`) |
| Mesa | working | Mesa main at the pinned commit (ea40598): Jay shader compiler (default on Xe2/Xe3), ANV compression/tiling, the Panther Lake hang fix, HPLOC TLAS, mesh fixes and fewer per-frame driver ioctls (patches/mesa 0200/0201/0205/0206/0207/0208/0211) - the tree every measured win used. | Anatase's Mesa package rebuilt at the pinned main commit with patches/mesa (x86_64 + i686), same overlay (`./oxp3-fixes-fast.sh install`) |
| XeSS XMX | partial | XeSS SR/FG on the Arc XMX units instead of DP4a for native Steam and Heroic; the shim, ControlLib and compiler install, but the driver CM patches are parked on Mesa 26.3 while the installer pins main. Flatpak Steam has its own deployment (docs/hw/flatpak-xmx.md) and is not trace-verified yet. | shim, ControlLib and IGC compiler installed by the driver installer into the Steam Flatpak environment (`./oxp3-fixes-fast.sh install`) |
| MangoHud | working | CPU power, GPU power and VRAM in the Gamemode overlay on Intel iGPUs. | Anatase's MangoHud rebuilt with patches/mangohud, same overlay (`./oxp3-fixes-fast.sh install`) |
| Kernel modules | working | xe (VBT VS/PE-O backport, 7.3 VRR/DSB flip timing, resume out-of-memory fix, 7.2.8-7.2.13 stable fixes, IBC v3 on PTL, eDP HDR switches) plus iwlwifi/iwlmld and btintel/btintel_pcie fixes, built for the installed kernel and layered as kmod-oxp3. | built for the installed kernel, signed with your MOK, layered as kmod-oxp3 with a regenerated initramfs (`./oxp3-fixes-fast.sh install`) |
| Firmware | working | Display DMC v2.20 (v2.36 caps VRR near 74 fps), Bluetooth 24.80 and CPU microcode 0x11c, layered in /usr/lib/firmware/updates. | layered as oxp3-firmware together with the modules, in /usr/lib/firmware/updates (`./oxp3-fixes-fast.sh install`) |
| rapl-power | working | CPU/GPU power for the overlay without opening the RAPL counters to users; also publishes dram and psys so the SAGV/memory-frequency effect is measurable without root. | /usr/local/libexec plus a system unit, installed by the driver installer (`./oxp3-fixes-fast.sh install`) |
| sched_ext (scx_cosmos) | awaiting-install | scx_cosmos measured +4.8% Cyberpunk average and +5.7% FPS/W over EEVDF with GravityMark unchanged; the gaming-scheduler unit is installed by the fast installer. | installed and enabled by the fast installer when scx_cosmos is present, with an EEVDF fallback (`./oxp3-fixes-fast.sh install`) |
| SAGV / memory frequency | blocked | No Linux control exists: Windows pins the SAGV/QCLK-GV point through an MCHBAR register pair, while Linux only has the boot-time xe.enable_sagv switch, which can only disable. | nothing is installed; the probe is read-only and needs no root |
| Decky status plugin | working | A Decky Loader plugin and a headless CLI over one backend, showing live Mesa, XMX, sched_ext and hardware-fix status in the Steam Quick Access Menu. | a deterministic plugin zip (built offline, or an existing one verified against the recorded sha256) installed into the root-owned Decky plugins directory, with the previous copy backed up (`sudo tools/oxp3-hw-install.sh --decky`) |
<!-- END GENERATED: readme-summary -->

Hardware fixes (volume rocker, home button/LEDs, body RGB, suspend policy, speaker EQ, Bluetooth,
power/fan/battery, display) have their own generated state matrix in
[docs/hw/README.md](docs/hw/README.md); `tools/oxp3-hw-install.sh` installs them.

You keep booting the stock Anatase kernel; only the modules above are replaced. Kernel
patches Anatase already carries are skipped one by one; firmware that Anatase ships in the
same or a newer version is skipped too.

## Performance: Windows vs Linux before vs Linux after

GravityMark 1.89, native Vulkan, 50k asteroids, 1280x720, fullscreen, vsync off. "Windows" is the
GravityMark leaderboard B390 row (Windows 11, driver 32.0.101.9033, same benchmark build). "Stock" is
Anatase's shipped Mesa **26.2.4**; "best config" is Mesa main-27 plus this patch set (HPLOC TLAS
`0205`, mesh `0206`/`0207`, corrected `0211`, and the `no-vrt` guidance).

| mode | Windows | Linux, stock Anatase (derived *) | Linux, best config | best vs Windows |
|---|---:|---:|---:|---:|
| hardware raster | 290.4 | ~223 | 268.2 | -7.7 % |
| compute raster (CS) | 202.4 | ~168 | 201.6 | -0.4 % (parity) |
| mesh shaders (MS) | 241.8 | ~173 | **243.5** | **+0.7 % (above)** |
| ray tracing (RT) | 187.7 | ~124 | **~180** | ~-4 % |

\* **Derived, not measured.** Stock 26.2.4 was never run at 50k on this machine; the only stock
measurement is the 1920x1200 primary pass, **60.9 FPS stock vs 73.1 FPS on main-27 (1.200x)**. The
stock column applies that measured ratio to each mode's main-27 **pre-patch** baseline (268.2 / 201.6
/ 208.0 / 149.3), so treat those four as an estimate. The direction is solid and is the point: stock
is far below Windows in every mode, and only after the Mesa upgrade plus these patches does Linux
reach parity (CS), exceed Windows (mesh) or close to ~4 % (RT). The 50k main-27 pre-patch baselines
themselves are measured; the biggest single step is the Mesa upgrade, e.g. CS primary 60.9 -> 73.1
(+19 %).

What moved, and with which patch:

* **mesh**: 208.0 -> 243.5 FPS (+17 %) from `0206` (full 16-byte URB slot writes) and `0207`
  (compact outputs stay dynamic). This is the one path now above Windows.
* **ray tracing**: 149.3 -> ~180 FPS (-20 % -> ~-4 %) from `0205` HPLOC TLAS builds, `0211`'s
  encode-subgroup tuning, and running with `ANV_DISABLE_VRT=1` (`0208` provides the switch). The
  `0211` subgroup half only applies with VRT enabled; its Morton hunk was dropped after the matrix
  measured upstream's 512 marginally ahead of 1024.
* **compute raster**: already at parity, unchanged by the patches.
* **hardware raster**: unchanged. Every driver-side and compiler-side lever tested was neutral or
  worse; this is documented in the out-of-tree `BEYOND-PARITY` notes, sections 137-153, and is the
  remaining 7.7 %.

Measurement provenance: the "after" figures were taken on a Mesa **main-27** tree at commit
`ea40598`, which was the tree the installer pinned at the time. The installer now pins main `62b0e52f` (2026-11-10); the figures above are what
the installer targets. The 26.3 branch was retired after it measured 4 % slower in raster and
8.6 % in mesh even with the patches ported.

Rejected patches are kept in `patches/rejected/` but are **not** applied: `0210` (wide ray-query
shadow copies) measured -1.87 % under HPLOC, and `0209` (VKD3D vrt-off default) measured worse under
Proton.

## Fast Mesa test install (`oxp3-fixes-fast.sh`)

A copy of the installer for testing the driver we benchmark with. Everything else is the same code
(kernel modules, firmware, gamescope, MangoHud, XeSS, extras); the Mesa step uses a podman/meson
build of the pinned main tree instead of the RPM path.

Verified shape of the fix: with the Steam-runtime expat (1.12.3, no `LIBEXPAT_2.9.0`) forced in the
search path, the statically linked driver loads, `vkCreateInstance` returns 0 and the loader
enumerates `Intel(R) Arc(tm) B390 (PTL)`; the older expat-dependent driver fails to load in the same
condition.

`build` compiles Mesa in the fedora:45 container (source tarball + Anatase `overrides.patch` +
`patches/mesa`, output in `~/.cache/oxp3-fixes/mesa-fast/usr/local`), and `install` copies that
driver plus the bundled libexpat into `/usr` and into the Steam Flatpak runtime overrides. Point
`OXP3_MESA_BUILD` at an existing prefix to reuse a tree instead of building.

```sh
# Mesa only, seconds (uses mesa-build/install-shiprt/usr/local by default):
./oxp3-fixes-fast.sh install --skip-gamescope --skip-mangohud --skip-kernel --skip-firmware --skip-xess --skip-extras

# or the full stack as usual; Mesa still comes from the prebuilt driver:
./oxp3-fixes-fast.sh build     # builds the non-Mesa parts only; Mesa is not rebuilt
./oxp3-fixes-fast.sh install

./oxp3-fixes-fast.sh status    # says whether the prebuilt driver is the one installed
./oxp3-fixes-fast.sh uninstall # restores the stock driver from the backup manifest
```

Steam games: the runtime's own libexpat is older than the one the earlier driver linked against
(`LIBEXPAT_2.9.0`). Overriding only `libvulkan_intel.so` made the Intel ICD fail to load inside the
sandbox, and Vulkan silently fell back to lavapipe - software rendering, single-digit FPS with the CPU
pegged. `build` now links expat statically (`-Dprefer_static=true`), so the driver has **no expat
dependency at all** and loads against whatever the host or the Steam runtime ships; the installer
only adds the bundled expat when a driver from an older build still needs it. `verify` tests both load
paths.

Pick another build with `OXP3_MESA_BUILD=/path/to/prefix ./oxp3-fixes-fast.sh install ...`.
The install goes into the writable `/usr` overlay (so it lasts until the next boot/deployment),
with the stock `libvulkan_intel.so`/`libexpat.so.1` backed up under
`/usr/local/share/oxp3-fixes-fast/stock`; GL stays stock, only the Intel Vulkan driver changes.
The Steam Flatpak runtime gets the same file through its overrides.

## Install and uninstall

The script is this repository; nothing to install for it. Clone it and run it from the checkout:

```sh
git clone https://github.com/silentsudin/oxp3-anatase-fixes   # or use your existing checkout
cd oxp3-anatase-fixes
```

Requirements: Anatase (Fedora-based, rpm-ostree), `podman`, and a normal user session. `build`
never uses sudo; `install`/`uninstall` ask for it.

**First time only**, if you want the kernel/firmware parts (secure boot machines):

```sh
./oxp3-fixes.sh mok-setup     # creates a MOK key and queues it for enrolment
# reboot, enrol the key in the blue MOK manager screen
```

**Install (one entry point, two runs):**

```sh
./oxp3-install.sh             # build + stage kernel modules/firmware (+ queues the MOK key)
systemctl reboot              # enrol the key in the blue MOK manager if it asked
./oxp3-install.sh             # overlay stack + hardware fixes + verification
```

`oxp3-install.sh` detects the phase from the live system (booted deployment, staged
deployment, MOK enrolment state), so re-running it is safe. `--status` shows what it
would do, `--dry-run` prints every command, `--uninstall` reverses the install, and
`--skip-build/--skip-hw/--skip-mok/--skip-verify` opt out of a step. Behind it are the
same scripts as below; run those directly if you want to drive each stage yourself.

**Install (manual, full stack):**

```sh
./oxp3-fixes.sh build         # builds gamescope, Mesa (x86_64+i686), MangoHud, XeSS, modules, firmware
./oxp3-fixes.sh install       # stages kernel modules + firmware (sudo)
systemctl reboot
./oxp3-fixes.sh install       # gamescope, Mesa, MangoHud, XeSS and extras into the /usr overlay
```

Then leave and re-enter Gamemode. If neither the kernel nor the firmware changed, the single
`install` is enough.

The **hardware fixes** (volume rocker, home button + LEDs, body RGB, suspend/wake policy, Bluetooth
firmware, power envelope/fan/battery, IMU, speaker EQ) have their own entry point and single guide:

```sh
tools/oxp3-preflight.sh                 # read-only: would an install complete? what is missing?
sudo tools/oxp3-hw-install.sh --all      # flags and opt-outs: --help; guide: docs/INSTALL-HW.md
```

The installer re-runs the preflight for the selected components and refuses on a true blocker
(`--force` overrides); every check and its remediation is in [docs/PREFLIGHT.md](docs/PREFLIGHT.md).

`install` above also calls it for the home-button/LED and power units; `--skip-hw` skips that.

Files the installers modify **in place** (the Heroic launcher, a Heroic GamesConfig JSON, the
Bluetooth dracut drop-in, the IMU blacklist, the hhd Python files) are backed up before every edit;
one command puts them back, byte-identical:

```sh
tools/oxp3-rollback.sh adopt             # once: record edits made by hand (writes only the state dir)
tools/oxp3-rollback.sh restore           # after an uninstall; --dry-run lists what it would do
```

What is touched, how the hash check refuses to overwrite a later user edit, and what cannot be
undone (ostree history, an enrolled MOK, kernel args, an initramfs that already carries the
firmware): [docs/ROLLBACK.md](docs/ROLLBACK.md).

After the post-install reboot, one command prints the ordered install checklist with the live
state of every step and writes a redacted evidence bundle for hand-back:

```sh
tools/oxp3-firstrun.sh --collect         # read-only; format/redaction: docs/FIRSTRUN.md
```

**Install only the Mesa/ANV performance patches** (what the figures above come from):

```sh
./oxp3-fixes.sh build --skip-kernel --skip-gamescope --skip-mangohud --skip-firmware --skip-xess --skip-extras
./oxp3-fixes.sh install --skip-kernel --skip-gamescope --skip-mangohud --skip-firmware --skip-xess --skip-extras
```

**Check what is active:**

```sh
./oxp3-fixes.sh status
```

**Uninstall (back to stock Anatase):**

```sh
./oxp3-fixes.sh uninstall     # asks for sudo; removes the overlay files, the kmod/firmware layer
systemctl reboot
```

`uninstall` reverts every part this repo installs: the `/usr` overlay additions (gamescope, Mesa,
MangoHud, XeSS, extras), the layered `kmod-oxp3` + `oxp3-firmware` packages and the regenerated
initramfs, and the `/etc` services/hooks. After the reboot you are booting Anatase's unmodified
deployment; `rpm-ostree status` should show no oxp3 layers left. Nothing outside the script is
touched, and rebuilding is just `build` + `install` again.

## After every Anatase update

```sh
./oxp3-fixes.sh build
./oxp3-fixes.sh install   # stages the kernel modules + firmware for the new kernel (sudo)
systemctl reboot
./oxp3-fixes.sh install   # gamescope, Mesa, MangoHud and the hhd patch into the /usr overlay
```

Then leave and re-enter Gamemode. Why two runs: xe loads from the initramfs, so the modules
and firmware are layered with rpm-ostree and the initramfs regenerated, which creates a new
deployment. gamescope, Mesa and MangoHud can't be layered that way (rpm-ostree can't replace
packages that exist for both x86_64 and i686 with local RPMs), so they go into the writable
`/usr` overlay of the booted deployment, which a new deployment doesn't keep. When neither
the kernel nor the firmware changed, a single `install` is enough.

The build runs in a Fedora container (podman). The first one takes about 1.5-2 hours (Mesa
x86_64 + i686 is most of it), later ones less. Check the result with `./oxp3-fixes.sh status`.

| Command | What it does |
|---|---|
| `build` | builds everything for the installed Anatase, into `~/.cache/oxp3-fixes/out` |
| `install` | stages modules + firmware (then reboot) or installs the overlay parts and services (sudo) |
| `all` | `build` + `install` (run `install` again after the reboot it asks for) |
| `status` | modules, firmware, packages, services, power limits, volume rocker state |
| `uninstall` | back to stock Anatase (sudo), then reboot |
| `mok-setup` | creates the module signing key and queues it for enrolment (once) |

Options: `--skip-kernel`, `--skip-firmware`, `--skip-gamescope`, `--skip-mesa`, `--skip-xess`,
`--skip-mangohud`, `--skip-extras`, `--stock-dmc` (keep Anatase's display DMC).

## Secure Boot key (one time)

The modules must be signed with a Machine Owner Key (MOK) that the kernel trusts for
modules; Secure Boot stays on. Anatase's kernel only puts MOK keys into its machine keyring
(the one that verifies modules) if they are **CA certificates with keyCertSign and without
digitalSignature** (`CONFIG_INTEGRITY_CA_MACHINE_KEYRING_MAX`). Other keys land in the
platform keyring, which can verify kernel images but not modules.

```sh
./oxp3-fixes.sh mok-setup   # creates ~/.local/share/oxp3-fixes/mok/MOK.{key,der} if needed
                            # and runs mokutil --import (choose a one-time password)
systemctl reboot            # MokManager: Enroll MOK -> Continue -> Yes -> password
```

Keep `MOK.key` private: anything signed with it is trusted on this machine. It is never
committed. `install` checks that the key really is in the kernel's machine keyring and skips
the kernel part if it isn't, so a wrongly signed module can't leave you without a display.

## Recovery

Every module/firmware install creates a new rpm-ostree deployment; the previous one stays
in the boot menu. Pick it there, or run `sudo rpm-ostree rollback`. The `/usr` overlay
(gamescope, Mesa, MangoHud, hhd patch) only exists in the booted deployment, so the previous
boot entry also gets you back to stock Mesa and gamescope. `./oxp3-fixes.sh uninstall`
removes everything.

## What is in the patches

### gamescope (`patches/gamescope`)

`0001-oxp3-hdr-pq.patch`, one self-contained patch on top of Anatase's `overrides.patch`
(its header lists every change and its author):

- Upstream Legion Go 2 support by Matthew Schwartz (Valve), backported: the known-display
  script for this panel (same panel as the Legion Go 2), EDID fallback for HDR luminance
  metadata, and the brightness slider emulated in software while in PQ.
- DisplayID extension blocks stripped from the EDID that games see. DXVK's bundled
  libdisplay-info rejects the panel's DisplayID 2.0 block and falls back to 1499 nits.
- Internal panel HDR metadata from the panel's EDID (476 nits, min 0.0007) instead of an
  empty block, which leaves the panel black after an HDR retoggle.
- VRR-aware HDR switching for displays whose script sets `hdr_requires_vrr` (this panel).

`0002-oxp3-vrr-pacing.patch`: upstream 3.16.29/3.17.0 VRR and pacing fixes by Matthew
Schwartz, several measured on the Legion Go 2 (uncapped VRR, frame limiter drift and vblank
pacing, cursor repaints under VRR, deep-idle wakeup, native gamut blend on HDR10), with the
conflicts against Anatase's VRR keepalive and frame generation resolved; and 30-47 Hz modes
(the EDID allows 30-144 Hz). The header lists what was not taken and why.

### Mesa (`patches/mesa`)

Mesa **main** at `MESA_COMMIT` (`ea40598`, 2026-11-05 - the tree every measured win used) with
Anatase's `overrides.patch` (minus its RADV Forza Horizon 5 hunk, which only concerns AMD) and:

- `0001`-`0005` are 26.3-branch backports (STC_CCS on CPB, companion RCS check, Jay uniform
  derivatives, L1/L2 flushes, compute buffer copies). They are **not applied on main** - main
  carries its own versions - and are kept in `patches/mesa-26.3-backports/` for anyone building
  the 26.3 tree.
- `0200`: do not issue `DRM_IOCTL_XE_EXEC_QUEUE_GET_PROPERTY` (queue-ban check) on every
  `vkGetFenceStatus`/timeline read; rate-limit it to 50 ms (a banned queue is still reported
  by the next submit). Verified on the device (stock 26.2.4 vs patched 27.0-dev): driver
  ioctls per frame 29.60 -> 27.70, ban queries 2.00 -> 0.13 per frame; vkmark score was
  unchanged within noise (27.8k/27.6k vs 27.8k), so this is a CPU-overhead/cleanliness fix,
  not an FPS one. CPU inside ioctls dropped 6.7% in a CPU-bound synthetic.
- `0205`: use the hierarchical PLOC builder for TLAS builds (GravityMark RT 50k: +6-13% versus
  LBVH; the traversal win outweighs the more expensive build).
- `0211`: tune the TLAS build path on Xe2+: encode subgroup 32 (the source TODO's intent, guarded
  off again whenever VRT is disabled, because the 128-GRF budget then makes the host dispatch
  geometry mismatch the shader and the encoded TLAS is wrong). The headline +1.55% (GravityMark RT
  50k, 170.8 -> 173.4 FPS, build time 1553 -> 1470 us attributed by the in-driver ANV_BVH_TS
  harness, image equivalent: A/B 37.2% differing / mean 3.8 against a 39.9-42.3% / 5.0-5.7 A/A
  floor) was bracketed A/B/A/B on **VRT-enabled** runs; under the recommended VRT-off profile the
  subgroup half is inert because the guard forces 16. The first version of this patch also raised
  `morton_sort_workgroup_size` to 1024 on Xe2+; that hunk has been **dropped**, because the later
  matrix measured upstream's 512 marginally ahead of 1024 (+0.3-0.4%, below the 1.5% bar) on the
  VRT-off profile, so the upstream default is kept. Only the acceleration-structure build path is
  touched, so raster and compute are unaffected.
- `0201`: reset all permanent fence payloads with one `vk_sync_reset_many()`
  (`SYNCOBJ_RESET` takes a list) instead of one ioctl per fence. No effect on a
  one-fence-per-call workload; games that reset several fences at once batch them.

The `0200`/`0201` pair was written for this device after measuring where a Vulkan frame's
driver CPU actually goes; the bigger remaining per-frame costs are `XE_EXEC` (the submit
itself), syncobj waits and dma-buf fence traffic.

Spec changes are source-driven: the anti-lag global is dropped only when the tarball lacks
`src/vulkan/layers/anti_lag`, the virtio global only when it lacks `src/virtio` (main still has
it), the RADV performance-conf file is added only when the source ships it, and rusticl/NVK are
disabled on i686. `INTEL_DEBUG=no-jay %command%` in a game's launch options switches that game
back to the old compiler (Jay is reportedly still slower for XeSS's own shaders). 26.3 was
retired after measuring 4 % slower in raster and 8.6 % in mesh even with the patches ported.

Games run under the Steam Flatpak, and Anatase builds its runtime (`org.anatase.Platform`) from the
composefs `/usr` plus `/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides`,
skipping the `/usr` overlay on purpose. So `install` also copies our Mesa into those overrides (GL/EGL/GBM,
the Intel Vulkan driver and its data; other vendors' Vulkan drivers and OpenCL stay stock to keep the
`/run` copy small) and restarts `anatase-flatpak-extensions.service` when no Flatpak app is running.
`status` shows whether the Steam runtime runs the same Intel driver as the host.

`0101`-`0105` are xess-xmx-linux's C-for-Metal kernel-injection series (the injection itself, its debug
tools, Large GRF on Xe2, the Jay divergent-barrier fix, and our fork's no-CM-retry-check change). They
were authored against 26.3, so they are **parked in `patches/xess-26.3/`** while the installer pins
main: the XeSS shim, ControlLib and compiler still install, but XMX acceleration is unavailable until
those five are ported to main. Without the shim the driver behaves like the stock one.

### XeSS on XMX (xess-xmx-linux)

[xess-xmx-linux](https://github.com/HHHHanasak1/xess-xmx-linux) was developed on a OneXPlayer 3. A shim
`igdext64.dll` answers XeSS's Intel D3D12 extension queries and hands its C-for-Metal kernels over as
placeholders; the patched ANV compiles them with Intel's IGC at run time (`tools/cm-compile.sh`) and runs
them in place of the placeholders. Upstream supports only a native Steam. Our fork
([silentsudin/xess-xmx-linux](https://github.com/silentsudin/xess-xmx-linux), branch `anatase`, pinned
as `XESS_COMMIT`) adds a system mode: with the patched system Mesa no package drivers or
`VK_DRIVER_FILES` (which Flatpak drops), only `ANV_CM_HELPER`, `VKD3D_DISABLE_EXTENSIONS` and
`force_vk_vendor=0` in a user Flatpak override for `org.anatase.Steam`. The Windows DLLs come from
upstream's release 20261029 (MSVC builds), checked by sha256.

- Only DX12 XeSS, on a Proton that bundles igdext (Proton Experimental/Hotfix, GE-Proton 11,
  proton-cachyos from 2026-10-14). `status` lists the Protons with and without the shim; the package's
  watcher puts the shim back after a Proton update.
- Launch options: `XESS=latest %command%` (Intel's newer XeSS for games older than 2.0.2.68, e.g.
  Hogwarts Legacy), `XESS=dp4a %command%` (A/B against the old path), `XESS_FG=3 %command%`,
  `XESS_LOG=1 %command%` (logs). Variables only work in front of `%command%`.
- Proof it runs on XMX: with `XESS_LOG=1` the prefix's `drive_c/igdext_trace.log` names
  `8086:b080 -> Xe3-LPG`, the passed self-test and `XMXEnabled=1` and has no `fallback:` line;
  `~/.cache/xess-xmx/compile.log` has `cm-compile ok` lines.
- DX11 games: a game's own DX11 XeSS cannot use XMX (its DLL calls private Intel driver functions).
  OptiScaler with `Dx11Upscaler=xess_12` runs XeSS's D3D12 path instead, which goes through the shim
  (super resolution only); set `[Spoofing] Dxgi=false`, or XeSS sees an NVIDIA GPU and uses DP4a.
- Vulkan games: XeSS on Vulkan has no XMX kernels at all, so there is no XMX path yet.

### kernel (`patches/kernel`)

- `0001`-`0009`: the VBT #57 VS/PE-O override series by Michał Grzelak (Intel, Linux 7.4),
  backported (kernel-anatase issue #7), plus our cap of the C10 table to the 10 DP presets.
- `0101`: xe resume out-of-memory fix (`kvmalloc` for the VMA ops array; large games fail
  with "out of memory" right after sleep, SteamOS #2962).
- `0102`-`0109`: from 7.3/7.4: DSB flip timing with the VRR timing generator
  (`f7140c7cef30`, `4a68c7516c57`, `b201029ca695`), ALPM check moved to compute_config_late,
  PIPEDMC_ERROR on display 30+, DPRx wake on eDP, engine cycle query without global
  forcewake, IBC v3 on PTL.
- `0110`-`0152`: the 7.2.8-7.2.13 stable fixes for xe, i915 display (selective fetch),
  btintel and btintel_pcie (shared HW reset, bounds checks).
- `0153`-`0160`: iwlmld fixes from 7.3/7.4 (firmware recovery, D3 resume, low-latency
  detection, HE ER off, leaks).
- `0161`: two boot-time eDP HDR switches, both off by default:
  `xe.edp_vsc_header_rewrite=1` (rewrite the VSC SDP header so its ECC is recomputed) and
  `xe.edp_msa_vsc_colorimetry=0` (no MSA colorimetry bit on eDP), from the OneXPlayer 3
  community fix pack's findings. Try with `sudo rpm-ostree kargs --append=xe.<switch>`.

The build feeds Anatase's `symvers` to modpost and fails if a module needs a symbol the
running kernel doesn't export.

### Firmware (`FIRMWARE` in the script)

Fetched from upstream linux-firmware / Intel's microcode repository with pinned checksums:
display DMC v2.20 (linux-firmware `d2e1085e36fb`; with v2.36 a late VRR frame only ends at
the 30 Hz maximum, which caps VRR at about 74 fps, onexplayer3-steamos-setup issue #1),
Bluetooth BE201 24.80.0.2 (`8a2941fa`), microcode 06-cc-02 revision 0x11c (Intel
microcode-20260811).

### hhd (`patches/hhd`) and the volume rocker

The EC sometimes drops the volume key release, so the key stays held (hhd #342). atkbd
force-release (`files/84-oxp3-volume-keys.hwdb`) fixes that in the kernel, but then press and
release reach hhd in one batch and it keeps only the release. The hhd patch turns every
press into a tap (down, up 60 ms later in a later batch, one queued press per key). Both go
into `/usr` together, so an Anatase update removes both and the rocker keeps working
(unfixed) until the next install.

### Power readings and CVE-2020-8694 (`files/rapl-power`)

Intel has no equivalent of AMD's `gpu_metrics`: every source of CPU/iGPU power is the RAPL
counter, root-only since CVE-2020-8694 (PLATYPUS). `rapl-power` (root, no capabilities,
sandboxed) is the only reader and publishes 1 s averages rounded to 0.1 W in
`/run/rapl-power/<domain>`; MangoHud reads those (CPU = package - uncore, GPU = uncore).
Domains published: `package-0`, `core`, `uncore`, plus `dram` (memory domain) and `psys` (whole
platform), which is what `tools/oxp3-sagv.sh measure` reads to quantify SAGV (the memory-frequency
switching Windows pins per profile, see [docs/hw/sagv.md](docs/hw/sagv.md)).
`oxp3-rapl-pp1` reloads `intel_rapl_msr` once at boot if it registered no uncore (GPU) zone.

### Extras (`files/`)

- `oxp3-tdp-sync`: hhd writes its TDP to both package power limit interfaces, but the MSR
  PL1 ends up at the 25 W firmware value and the lower one wins. Copies the MMIO PL1 (hhd's
  value) to the MSR every 2 s and logs each correction (`journalctl -u oxp3-tdp-sync`).
- `oxp3-audio-route` (+ resume unit): selects the Speaker profile again when the headphone
  jack is empty but a Headphones profile is active, or re-runs it after WirePlumber failed to
  enable the UCM speaker device (ALC245 jack misdetection at boot/resume).
  `52-oxp3-speakers.conf` names the sink "OneXPlayer 3 Speakers".
- `oxp3-xpad-sleep`: unbinds xpad from the built-in pad before sleep (it otherwise waits 5 s
  for an output URB the pad never completes) and binds it again after.
- `bmi160_i2c` blacklisted (the BMI260 IMU also matches its ACPI id; `bmi260` drives it),
  `thermald` disabled (this BIOS has no tables for it; `uninstall` enables it again).
- Not included: a battery percentage clamp. The gauge only over-reports right after a full
  charge, which hhd's 80 % charge limit never reaches.

## Licence and provenance

No repository-wide licence has been chosen yet - that is the owner's decision and this repository
does not assert one (until a `LICENSE` exists, default copyright applies). The provenance guarantee
is already in force: only our own code, documentation and derived data are tracked, with no vendor
binaries, firmware blobs or decompiled listings; vendor files are downloaded at build/install time
and pinned by SHA-256. [docs/PROVENANCE.md](docs/PROVENANCE.md) records the policy, the audit and
the evidence, and `tools/apply-license.py` applies the chosen licence in one reviewable commit.

## Updating the patches

If a build stops with "doesn't apply" or a missing anchor line, Anatase changed the package
or kernel. Rebase the patch in question onto the new Anatase version and regenerate it in
`patches/`. Kernel patches Anatase picks up are skipped automatically; the kernel part is
skipped entirely once Anatase carries all of them.
