# Working build — 2026-11-10 (host Mesa bump + XMX on 26.3.0)

Supersedes the Mesa rows of `WORKING-BUILD-2026-11-08.md`; everything else there still applies.

| piece | build | where |
|---|---|---|
| host `/usr/lib64/libvulkan_intel.so` | Mesa main **62b0e52f** (2026-11-10 15:36 UTC), `27.0.0~devel.20261110git62b0e52f`, sha256 `5a75b6a44d285fd1…` | `~/.cache/oxp3-fixes/mesa-fast/usr/local/lib64/`, known-good `known-good/host-5a75b6a4-libvulkan_intel.so` |
| Flatpak runtime (unchanged) | `4d043cc2851a11fd…` | `/run/anatase/flatpak/.../files/lib64/` |
| XMX driver | Mesa **26.3.0** final + release CM + our patches, sha256 `d5e7d767…` | `~/.cache/oxp3-fixes/xmx-driver/`, rc3 rollback in `xmx-driver-rc3/` |
| Decky plugin | 1.3.1 | `tools/decky-oxp3-status/build/oxp3-status-1.3.1.zip` |

Host patch set (all apply to 62b0e52f): `0200 0201 0206 0207 0212` + Anatase overrides (radv hunk stripped).
Rebuild: `OXP3_MESA_REBUILD=1 ./oxp3-fixes-fast.sh build`; install: `sudo ./oxp3-fixes-fast.sh install` (add
`--skip-flatpak` to keep the runtime at `4d043cc2`).

At boot `oxp3-mesa-keep.service` restores `host-5a75b6a4-libvulkan_intel.so` when the overlay cleared it; the previous
`host-ec418f04` file remains as fallback/rollback.
