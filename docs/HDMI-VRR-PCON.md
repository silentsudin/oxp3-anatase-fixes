# LG TV VRR over USB4 DP→HDMI PCON

## Evidence

- Connector `DP-1` is a DP branch device with `subconnector=HDMI`.
- EDID: LG TV SSCR2, HDMI Forum VSDB VRR 40–120 Hz.
- DRM: `vrr_capable = 0`; KDE reports `Vrr: incapable`.
- Intel `intel_vrr_is_capable()` explicitly rejects `drm_dp_is_branch()` and its
  comment says PCON VRR is not implemented.
- 4K120 + HDR work, so the HDMI 2.1 link/PCON conversion is functional.

## Patch

`patches/kernel/0010-drm-i915-vrr-pcon.patch` (also used by Xe display):

- adds `as_sdp_v1_supported`;
- marks DP branch/PCON HDMI sinks as AS SDP v1 capable;
- permits VRR on a branch device when AS SDP support + HDMI sink are present;
- sends the legacy header-only AS SDP v1 to the PCON instead of AS SDP v2.

Compile check: the patched `xe.ko` objects build in
`~/.cache/oxp3-fixes/kernel/build-7.3.4-an02`; full modpost/link and module
loading still require a complete kernel build and root.

Status: **experimental, not boot-tested**. A PCON may require additional
HDMI-VRR/PCON setup beyond AS SDP v1; treat this as a test patch, not a
production fix.
