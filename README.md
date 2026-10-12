# OneXPlayer 3 fixes for Anatase

Personal patches and a rebuild script that restore OneXPlayer 3 (Intel Panther Lake, xe)
support on [Anatase](https://github.com/anatase-org/anatase) after an update.

| Part | Fixes | How it is installed |
|---|---|---|
| gamescope | PQ HDR on the internal SDC AMS881KB01 OLED: no black screen when HDR turns on with VRR off or on an HDR retoggle; games see the real peak brightness (~1100 nits instead of 1499); brightness slider works in HDR | Anatase's gamescope package rebuilt with `patches/gamescope`, `rpm-ostree override replace` |
| MangoHud | CPU power, GPU power and VRAM in the Gamemode performance overlay on Intel iGPUs | Anatase's MangoHud package rebuilt with `patches/mangohud`, `rpm-ostree override replace` |
| RAPL udev rule | lets the overlay read the CPU/GPU energy counters (root-only since CVE-2020-8694) | `/etc/udev/rules.d/70-oxp3-rapl.rules` |
| kernel (xe) | VBT VS/PE-O backport for the eDP link: random white dots, and the black screen on an HDR retoggle | `xe.ko` rebuilt for the installed Anatase kernel, signed with your MOK, layered as `kmod-oxp3-xe` in `updates/` |

You keep booting the stock Anatase kernel; only its `xe` module is replaced. If Anatase
ships the VS/PE-O backport itself (kernel-anatase issue #7), the script notices and skips
the kernel part.

## After every Anatase update

```sh
./oxp3-fixes.sh all     # build, then install (asks for sudo)
systemctl reboot
```

The build runs in a Fedora container (podman) and takes about 30-40 minutes the first
time, less afterwards. Check the result with `./oxp3-fixes.sh status`.

Commands:

| Command | What it does |
|---|---|
| `build` | builds everything for the installed Anatase, into `~/.cache/oxp3-fixes/out` |
| `install` | installs the built packages (sudo), then reboot |
| `all` | `build` + `install` |
| `status` | shows what is installed, overridden and layered |
| `uninstall` | back to stock Anatase (sudo), then reboot |

Options: `--skip-kernel`, `--skip-gamescope`, `--skip-mangohud`.

## Secure Boot key (one time)

The xe module must be signed with a Machine Owner Key (MOK) that is enrolled in the
firmware; Secure Boot stays on. The script looks for `MOK.key` and `MOK.der` in
`~/.local/share/oxp3-fixes/mok` (or `OXP3_MOK_DIR`). Create and enrol one:

```sh
d=~/.local/share/oxp3-fixes/mok; install -d -m 700 $d
openssl req -new -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
    -subj "/CN=OXP3 local modules ($(id -un))/" -addext "extendedKeyUsage=codeSigning" \
    -keyout $d/MOK.key -out $d/MOK.crt
openssl x509 -in $d/MOK.crt -outform DER -out $d/MOK.der
sudo mokutil --import $d/MOK.der    # choose a one-time password
systemctl reboot                    # MokManager: Enroll MOK -> Continue -> Yes -> password
```

Keep `MOK.key` private: anything signed with it is trusted on this machine. It is never
committed (`.gitignore`). `install` checks that the key is in the kernel's machine keyring
and skips the kernel part if it isn't, so a wrongly signed module can't leave you without
a display.

## Recovery

Every install creates a new rpm-ostree deployment; the previous one stays in the boot
menu. Pick it there, or run `sudo rpm-ostree rollback`. `./oxp3-fixes.sh uninstall`
removes everything.

## What is in the patches

### gamescope (`patches/gamescope/0001-oxp3-hdr-pq.patch`)

One self-contained patch on top of Anatase's gamescope `overrides.patch`, squashed from the
local branch `oxp3-hdr-min2` (not published); its header lists every change and its author.
It contains:

- Upstream Legion Go 2 support by Matthew Schwartz (Valve), backported: the known-display
  script for this panel (it is the same panel as the Legion Go 2), EDID fallback for HDR
  luminance metadata, and emulating the brightness slider in software while in PQ.
- DisplayID extension blocks stripped from the EDID that games see. DXVK's bundled
  libdisplay-info rejects the panel's DisplayID 2.0 block and falls back to 1499 nits.
- Internal panel HDR metadata from the panel's EDID (mastering max / MaxCLL / MaxFALL
  476 nits, min 0.0007) instead of an empty block. Zero luminance metadata leaves the panel
  black after an HDR retoggle.
- VRR-aware HDR switching, only for displays whose script sets `hdr_requires_vrr` (this
  panel): HDR is entered from SDR with VRR on, HDR and VRR modesets are spaced out, VRR
  stays on while in HDR (the frame limiter still applies), and no HDR metadata is sent in
  SDR. Without VRR running this panel stays black in PQ.

### MangoHud (`patches/mangohud`)

From [silentsudin/MangoHud `xe-igpu-metrics`](https://github.com/silentsudin/MangoHud):
RAPL CPU power fixes (float math, wraparound, prefer `intel-rapl` over mmio), Intel iGPU
power from the RAPL uncore domain, and xe integrated memory reported as VRAM.

### kernel (`patches/kernel`)

The VBT #57 VS/PE-O override series by Michał Grzelak (Intel, in drm-intel / Linux 7.4),
backported to Anatase 7.2, plus a fix capping the C10 PHY table to the 10 DP presets.
Requested upstream in Anatase as kernel-anatase issue #7.

## Updating the patches

If a build stops with "patches don't apply" or a missing anchor line, Anatase changed the
package or kernel series. Rebase the patches (or the source branches above) onto the new
Anatase version and regenerate the files in `patches/`. The kernel patches stop being needed
once Anatase ships the VS/PE-O series; the script then skips the kernel part by itself.
