# Upstream submission notes (prepared, NOT submitted)

Measured on an OneXPlayer 3 (Panther Lake, Arc B390 `8086:b080`), Mesa main at `ea40598`, Jay
compiler, GravityMark 1.89. All effects are bracketed A/B/A/B with the environment fixed and images
checked for equivalence; absolute FPS is device-specific and should be read as relative only.

## 0205 - anv: use the hierarchical PLOC builder for TLAS builds
* effect: GravityMark RT 50k **+6-13 %** versus the LBVH builder (traversal win outweighs the more
  expensive build). Build cost rises but is paid once per frame at the 50k tier.
* conditions: TLAS builds with HPLOC support; tested VRT on and off.
* risk: build-time increase could regress workloads dominated by many small builds; needs upstream
  numbers on other Xe parts. Patch touches only the acceleration-structure build path.
* evidence: BEYOND-PARITY 154-156; patch applies to pristine `ea40598`.

## 0206 - anv: mesh shaders use full 16-byte URB slot writes
* effect: part of the mesh-path fix pair that brought mesh to parity or above (241.8 vs 243.5 on
  this device in the last campaign).
* conditions: mesh shaders; verified with GravityMark mesh mode.
* risk: URB write sizing is shared with other stages; upstream review needed for non-mesh fallout.
* evidence: sections on mesh (0080-0090 in the log).

## 0207 - anv: keep mesh compact outputs dynamic
* effect: companion to 0206; together they avoid the stage-mask/URB interaction that produced wrong
  mesh output. Measured as part of the same mesh pair.
* risk: stage-mask handling is core; needs upstream CI and other hardware.
* evidence: same sections as 0206.

## 0211 - anv: encode subgroup 32 on Xe2+ when VRT is enabled
* effect: GravityMark RT 50k **170.8 -> 173.4 FPS (+1.55 %)** bracketed on **VRT-enabled** runs,
  build time 1553 -> 1470 us (ANV_BVH_TS in-driver counters).
* conditions: **inert when VRT is disabled** - the guard forces subgroup 16 because under the
  128-GRF budget the shader's effective SIMD no longer matches the host dispatch geometry and the
  encoded TLAS is wrong (observed as the asteroid field disappearing).
* risk: subgroup choice is encoded into the built shader; any future VRT/budget change must keep the
  guard. The morton-workgroup hunk that shipped in the first version was removed: the matrix measured
  upstream's 512 slightly ahead of 1024 on the VRT-off profile, so upstream's default stands.
* evidence: sections 156-162, 143-144.

## no-vrt guidance (not a patch)
* effect: enabling VRT costs ~4 % on RT workloads on this device (177.7 vs ~170 without it in the
  50k series). Documenting `ANV_DISABLE_VRT` / `INTEL_DEBUG=no-vrt` as a recommendation (with the
  0205 series) gives users the choice without changing defaults.
* risk: VRT is beneficial for heavy register pressure elsewhere; keep it opt-in, not default.
* evidence: sections 158-162.
