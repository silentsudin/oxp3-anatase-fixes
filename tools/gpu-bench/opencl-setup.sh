#!/bin/bash
# SPDX-License-Identifier: MIT
# opencl-setup.sh - make rusticl usable without root on Fedora.
#
# Fedora's libclc-spirv installs libclc.spv under the new LLVM names, but Mesa
# rusticl looks for /usr/share/clc/spirv-mesa3d-.spv.  This extracts the SPIR-V
# from the RPM into the user cache and builds a tiny LD_PRELOAD path shim.
#
#   tools/gpu-bench/opencl-setup.sh
#   source tools/gpu-bench/opencl-env.sh    # then clinfo/clpeak see the device
set -euo pipefail
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
clc=$cache/share/clc; bin=$cache/bin
mkdir -p "$clc" "$bin"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
if [ ! -f "$clc/spirv-mesa3d-.spv" ] || [ ! -f "$clc/spirv64-mesa3d-.spv" ]; then
    echo "== downloading libclc-spirv"
    (cd "$tmp" && dnf -q download libclc-spirv >/dev/null)
    rpm=$(ls "$tmp"/libclc-spirv-*.rpm | head -1)
    (cd "$tmp" && rpm2cpio "$rpm" | cpio -id --quiet \
        './usr/lib/clang/*/lib/spirv32-unknown-unknown/libclc.spv' \
        './usr/lib/clang/*/lib/spirv64-unknown-unknown/libclc.spv')
    cp "$tmp"/usr/lib/clang/*/lib/spirv32-unknown-unknown/libclc.spv "$clc/spirv-mesa3d-.spv"
    cp "$tmp"/usr/lib/clang/*/lib/spirv64-unknown-unknown/libclc.spv "$clc/spirv64-mesa3d-.spv"
fi
gcc -shared -fPIC -O2 -o "$bin/libclc-shim.so" "$(dirname "$0")/libclc-shim.c" -ldl
cat > "$(dirname "$0")/opencl-env.sh" <<ENV
export OXP3_LIBCLC_DIR=$clc
export LD_PRELOAD=$bin/libclc-shim.so
export RUSTICL_ENABLE=iris
ENV
echo "== opencl env: source $(dirname "$0")/opencl-env.sh"
