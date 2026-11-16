#!/bin/bash
# SPDX-License-Identifier: MIT
# neo-setup.sh - user-space Intel NEO/Level Zero GPU compute stack.
#
# Fedora's NEO 26.35 aborts on the B390 with preemptionSurfaceSize==0.  With
# compute-runtime 26.40 + IGC 2.42 + NEO_OverrideCsrAllocationSize=16M the
# device enumerates and OpenCL/OpenVINO/oneAPI execute.
set -euo pipefail
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
neo=$cache/tools/neo; mkdir -p "$neo"; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
base=https://github.com/intel/compute-runtime/releases/download/26.40.40196.8
if [ ! -f "$neo/compute/libze_intel_gpu.so.1" ]; then
    echo "== compute-runtime 26.40"
    for f in libze-intel-gpu1_26.40.40196.8-0_amd64.deb intel-opencl-icd_26.40.40196.8-0_amd64.deb \
             libigdgmm12_22.10.2_amd64.deb intel-ocloc_26.40.40196.8-0_amd64.deb; do
        curl -L --fail --max-time 180 -sO "$base/$f" --output-dir "$tmp"
    done
    for d in "$tmp"/*.deb; do dpkg-deb -x "$d" "$tmp/root"; done
    mkdir -p "$neo/compute"; cp -a "$tmp/root/usr/lib/x86_64-linux-gnu/." "$neo/compute/"
fi
if [ ! -f "$neo/igc/libigc.so.2" ]; then
    echo "== IGC 2.42"
    for f in intel-igc-core-2_2.42.6+23070_amd64.deb intel-igc-opencl-2_2.42.6+23070_amd64.deb; do
        curl -L --fail --max-time 180 -sO \
          "https://github.com/intel/intel-graphics-compiler/releases/download/v2.42.6/$f" --output-dir "$tmp"
    done
    mkdir -p "$neo/igc"
    for d in "$tmp"/intel-igc-*.deb; do dpkg-deb -x "$d" "$tmp/igcroot"; done
    cp -a "$tmp/igcroot/usr/local/lib/." "$neo/igc/"
fi
(cd "$neo/igc" && for x in libigc.so.2 libigdfcl.so.2 libiga64.so.2; do
    [ -e "$x" ] && ln -sf "$x" "${x%.2}.1" || true; done)
if [ ! -f "$neo/rt/libze_intel_gpu_raytracing.so" ]; then
    echo "== level-zero raytracing + tbb"
    (cd "$tmp" && dnf -q download intel-level-zero-gpu-raytracing tbb >/dev/null)
    mkdir -p "$neo/rt" "$neo/tbb"
    (cd "$tmp" && rpm2cpio intel-level-zero-gpu-raytracing-*.rpm | cpio -id --quiet)
    cp -a "$tmp"/usr/lib64/libze_intel_gpu_raytracing.so "$neo/rt/"
    (cd "$tmp" && rpm2cpio tbb-*.rpm | cpio -id --quiet)
    cp -a "$tmp"/usr/lib/libtbb* "$neo/tbb/"
fi
mkdir -p "$neo/vendors"
echo "$neo/compute/intel-opencl/libigdrcl.so" > "$neo/vendors/intel.icd"
cat > "$(dirname "$0")/neo-env.sh" <<ENV
export OXP3_NEO_DIR=$neo
export LD_LIBRARY_PATH=$neo/igc:$neo/compute:$neo/rt:$neo/tbb\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}
export NEOReadDebugKeys=1
export NEO_OverrideCsrAllocationSize=16777216
export OCL_ICD_VENDORS=$neo/vendors
ENV
echo "== neo env: source $(dirname "$0")/neo-env.sh"
