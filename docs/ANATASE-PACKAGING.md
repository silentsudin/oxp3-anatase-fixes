# Which Anatase packaging the installer builds from

## Rule

The installer rebuilds Anatase's own packages (gamescope, MangoHud, Mesa) with our patches. It
builds them from the **anatase-org/anatase commit the booted image was built from**, not from the
tip of the repository.

`fetch_anatase` reads that commit from the image's OCI label
`org.opencontainers.image.revision`, via `rpm-ostree status --booted --json`. For Stable
`20261109.7` that commit is `1adb102`. When an image carries no such label, the installer falls
back to the tip and prints a warning.

## Why

On 2026-11-21 the packaging tip moved gamescope from 3.16.23.3 to Anatase's fork `an0.8`, which
links the system wlroots 0.20 and libliftoff. The Stable image still ships 3.16.23.3 and has no
wlroots 0.20. Built from the tip:

- `install` stopped: `gamescope.spec` no longer had the `overrides.patch` line our patches were
  anchored to;
- even once rebased, the RPM fails rpm's dependency check
  (`libwlroots-0.20.so()(64bit) is needed`);
- `oxp3-gamescope-keep` extracts cached RPMs with cpio at every boot. It would have put that
  gamescope into `/usr` anyway, so Gamemode would no longer start.

## Safeguards

| where | what |
|---|---|
| `fetch_anatase` | builds from the booted image's packaging revision |
| `build_card <name> <patch dir>` | takes `<patch dir>/<spec Version>/*.patch` when that directory exists; with neither a matching directory nor flat patches it stops and names the versions it has |
| `spec_add_patches` | adds our `Patch2000+` lines after the spec's last Patch line; refuses a `%prep` that does not apply patches through `%autosetup`/`%autopatch` |
| `rpms_fit_host` (after every gamescope/MangoHud/Mesa RPM build) | `rpm -U --test` against a copy of the host rpmdb; RPMs that would not install are deleted from `out/` and the build stops |
| `oxp3-gamescope-keep`, `oxp3-mangohud-keep` | run `rpm -U --test` before extracting at boot; on failure they keep the stock package and log why |

The build container is shared and keeps whatever `dnf builddep` installed into it. That is how a
test build of `an0.8` made the 3.16.23.3 build pick up system wlroots 0.20 and enable gamescope's
tests, which are broken at `1adb102`. `rpms_fit_host` turns a contaminated container into a clear
build error instead of a broken install.

## When Anatase updates gamescope

After `bootc update` + reboot, the image ships a new gamescope. The next `./oxp3-install.sh` builds
from the new image's packaging:

- If `patches/gamescope/<new version>/` exists, it is used. `an0.8/` is already rebased and
  compile-tested; see the patch headers for what changed.
- Otherwise the build stops with
  `gamescope: Anatase's packaging is gamescope X, but patches/gamescope has patches only for: ...`.
  Rebase the newest set into `patches/gamescope/X/`.

Notes on the `an0.8` rebase (2026-11-21):

- `0001` (PQ HDR): Matthew Schwartz's EDID luminance fallback is already in an0.8, so it was
  dropped from the patch. The rest was ported, and the HDR-aware VRR choice now starts from an0.8's
  "no VRR while the Steam window is focused".
- `0002`: an0.8 already carries five of its six upstream backports. What remains is upstream
  `055902ca` (deep-idle pre-wake) and the 30-47 Hz modes for the SDC AMS881KB01.
