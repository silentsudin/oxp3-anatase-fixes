# OneXPlayer 3 fixes for Anatase

Personal patches and a rebuild script that restore and improve OneXPlayer 3 (Intel Panther
Lake, Arc B390, xe) support on [Anatase](https://github.com/anatase-org/anatase) after an
update. Everything is rebuilt from Anatase's own packaging and kernel tree plus the patches
here, so `build` + `install` reapplies it to whatever Anatase ships next.

| Part | Fixes | How it is installed |
|---|---|---|
| gamescope | PQ HDR on the internal SDC AMS881KB01 OLED (no black screens, real peak brightness, brightness slider in HDR); VRR frame pacing fixes from gamescope 3.16.29/3.17.0; 30-144 Hz modes | Anatase's gamescope rebuilt with `patches/gamescope`, installed into the writable `/usr` overlay (`rpm-ostree usroverlay --hotfix`) |
| Mesa | 26.3 branch: Jay shader compiler (default on Xe2/Xe3), ANV compression/tiling work, Panther Lake hang fix, Jay fixes, faster flushes/copies on Xe2+, and fewer per-frame driver ioctls (`patches/mesa` 0200/0201) | Anatase's Mesa package rebuilt at the pinned 26.3 commit with `patches/mesa` (x86_64 + i686), same overlay |
| XeSS XMX | XeSS SR/FG on the Arc's XMX units in DX12 games under Proton, instead of DP4a ([xess-xmx-linux](https://github.com/HHHHanasak1/xess-xmx-linux), our fork's `anatase` branch) | driver patches in our Mesa; shim, ControlLib, scripts and Intel's compiler in `~/.local/share/xess-xmx`; variables in a Flatpak override for `org.anatase.Steam` |
| MangoHud | CPU power, GPU power and VRAM in the Gamemode overlay on Intel iGPUs | Anatase's MangoHud rebuilt with `patches/mangohud`, same overlay |
| kernel modules | xe: VBT VS/PE-O backport (white dots, HDR retoggle black screen), 7.3 VRR/DSB flip timing, resume out-of-memory fix, 7.2.8-7.2.13 stable fixes, IBC v3 on PTL, eDP HDR switches. iwlwifi/iwlmld and btintel/btintel_pcie: 7.2.13 stable and 7.3/7.4 fixes | built for the installed Anatase kernel, signed with your MOK, layered as `kmod-oxp3` in `updates/` with a regenerated initramfs |
| firmware | display DMC v2.20 (v2.36 caps VRR at about 74 fps), Bluetooth 24.80, CPU microcode 0x11c | `oxp3-firmware`, layered with the modules, in `/usr/lib/firmware/updates` (searched first, also by dracut) |
| `rapl-power` | CPU/GPU power for the overlay without opening the RAPL counters to users | `/usr/local/libexec` + `/etc/systemd/system` |
| extras | TDP above 25 W actually applied, speakers after boot/resume, volume rocker no longer sticks, no 5 s wait on suspend, GPU power reading on every boot | services and configs in `/etc`; the hhd volume patch in the `/usr` overlay |

You keep booting the stock Anatase kernel; only the modules above are replaced. Kernel
patches Anatase already carries are skipped one by one; firmware that Anatase ships in the
same or a newer version is skipped too.

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

The 26.3 branch at `MESA_COMMIT` (after rc2) with Anatase's `overrides.patch` (minus its RADV
Forza Horizon 5 hunk, which doesn't apply to 26.3 and only concerns AMD) and:

- `0001`/`0002` (mesa!45498): stop using STC_CCS for CPB on Xe2+, fixes a Panther Lake GPU
  hang (Crimson Desert, mesa#16357).
- `0003` (mesa!45634): Jay, uniform derivatives without varying temporaries.
- `0004`/`0005` (mesa!45719, open MR, measured +2% in Cyberpunk 2077 on a OneXPlayer 3):
  cheaper write flushes and compute buffer copies on Xe2+.
- `0200`: do not issue `DRM_IOCTL_XE_EXEC_QUEUE_GET_PROPERTY` (queue-ban check) on every
  `vkGetFenceStatus`/timeline read; rate-limit it to 50 ms (a banned queue is still reported
  by the next submit). Measured on an ANV-only build of 26.3/main on the OneXPlayer 3:
  driver ioctls per frame 24.0 -> 22.0, ban queries 2.00 -> ~0.0007 per frame, CPU inside
  ioctls 22.3 -> 20.8 us per frame (-6.7%), vkmark full-suite score 26.3k -> 26.4k.
- `0201`: reset all permanent fence payloads with one `vk_sync_reset_many()`
  (`SYNCOBJ_RESET` takes a list) instead of one ioctl per fence. No effect on a
  one-fence-per-call workload; games that reset several fences at once batch them.

The `0200`/`0201` pair was written for this device after measuring where a Vulkan frame's
driver CPU actually goes; the bigger remaining per-frame costs are `XE_EXEC` (the submit
itself), syncobj waits and dma-buf fence traffic.

Spec changes for 26.3: no anti-lag layer (removed upstream), no virtio Vulkan driver (needs
venus-protocol from the network), no rusticl/NVK on i686. `INTEL_DEBUG=no-jay %command%` in
a game's launch options switches that game back to the old compiler (Jay is reportedly still
slower for XeSS's own shaders). Bump `MESA_COMMIT`/`MESA_VERSION` to `mesa-26.3.0` once it is
released.

Games run under the Steam Flatpak, and Anatase builds its runtime (`org.anatase.Platform`) from the
composefs `/usr` plus `/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides`,
skipping the `/usr` overlay on purpose. So `install` also copies our Mesa into those overrides (GL/EGL/GBM,
the Intel Vulkan driver and its data; other vendors' Vulkan drivers and OpenCL stay stock to keep the
`/run` copy small) and restarts `anatase-flatpak-extensions.service` when no Flatpak app is running.
`status` shows whether the Steam runtime runs the same Intel driver as the host.

`0101`-`0104` are xess-xmx-linux's `patches/mesa-26.3` (C-for-Metal kernel injection, its debug tools,
Large GRF on Xe2, the Jay divergent-barrier fix) and `0105` our fork's addition: no CM retry check in
programs outside Wine, so the compositor and desktop don't log about it. Without the shim (games on a
Proton without igdext, everything else) the patched driver behaves like the stock one.

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

## Updating the patches

If a build stops with "doesn't apply" or a missing anchor line, Anatase changed the package
or kernel. Rebase the patch in question onto the new Anatase version and regenerate it in
`patches/`. Kernel patches Anatase picks up are skipped automatically; the kernel part is
skipped entirely once Anatase carries all of them.
