#!/bin/bash
# SPDX-License-Identifier: MIT
# render-setup.sh - install the Blender LTS portable build for Cycles benchmarks.
set -euo pipefail
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
tools=$cache/tools; mkdir -p "$tools/blender"
if [ ! -x "$tools/blender/blender" ]; then
    curl -L --fail --max-time 600 -o "$tools/blender.tar.xz" \
      https://download.blender.org/release/Blender4.5/blender-4.5.16-linux-x64.tar.xz
    tar -xf "$tools/blender.tar.xz" -C "$tools/blender" --strip-components=1
fi
echo "== blender: $tools/blender/blender"
