#!/bin/bash
# SPDX-License-Identifier: MIT
# ai-setup.sh - OpenVINO + Intel NPU driver 1.38 in the user cache.
#
# Fedora ships intel-npu-driver 1.32, whose compiler fails every model with
# ZE_RESULT_ERROR_UNSUPPORTED_FEATURE.  Intel's 1.38 release works.
set -euo pipefail
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
tools=$cache/tools; npu=$tools/npu; mkdir -p "$npu" "$tools"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
if [ ! -f "$npu/usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1.38.0" ]; then
    echo "== Intel NPU driver 1.38"
    curl -L --fail --max-time 300 -o "$tmp/npu.tar.gz" \
      https://github.com/intel/linux-npu-driver/releases/download/v1.38.0/linux-npu-driver-v1.38.0.20260910-34487311128-ubuntu2404.tar.gz
    tar -xf "$tmp/npu.tar.gz" -C "$tmp"
    (cd "$tmp" && dpkg-deb -x intel-level-zero-npu_*.deb root && \
        dpkg-deb -x intel-driver-compiler-npu_*.deb root)
    cp -a "$tmp/root/." "$npu/"
fi
if [ ! -f "$npu/ze/usr/lib64/libze_loader.so.1" ]; then
    echo "== Level Zero loader"
    (cd "$tmp" && dnf -q download oneapi-level-zero >/dev/null)
    (cd "$tmp" && rpm2cpio oneapi-level-zero-*.rpm | cpio -id --quiet)
    mkdir -p "$npu/ze"; cp -a "$tmp/usr/." "$npu/ze/"
fi
if [ ! -x "$tools/ov/bin/benchmark_app" ]; then
    echo "== OpenVINO venv"
    uv venv --python 3.12 "$tools/ov" >/dev/null
    uv pip install --python "$tools/ov/bin/python" openvino onnx >/dev/null
fi
if [ ! -f "$tools/ov-models/add.xml" ]; then
    echo "== models"
    "$tools/ov/bin/python" "$(dirname "$0")/ai-models.py" "$tools/ov-models"
fi
cat > "$(dirname "$0")/ai-env.sh" <<ENV
export OXP3_NPU_DIR=$npu/usr/lib/x86_64-linux-gnu
export OXP3_ZE_DIR=$npu/ze/lib64
export PATH=$tools/ov/bin:\$PATH
export LD_LIBRARY_PATH=$npu/usr/lib/x86_64-linux-gnu:$npu/ze/lib64\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}
ENV
echo "== ai env: source $(dirname "$0")/ai-env.sh"
