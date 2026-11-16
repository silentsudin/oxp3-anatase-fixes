#!/bin/bash
# SPDX-License-Identifier: MIT
# media-setup.sh - static FFmpeg + user-space Intel oneVPL GPU runtime.
#
# Fedora has libvpl but not intel-vpl-gpu-rt, so hevc_qsv fails with
# "Error creating a MFX session: -9" until the GPU runtime is installed.
# This extracts the RPM into the user cache and writes media-env.sh.
set -euo pipefail
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
tools=$cache/tools
mkdir -p "$tools"
if [ ! -x "$tools/ffmpeg/bin/ffmpeg" ]; then
    echo "== ffmpeg static build"
    curl -L --fail --max-time 300 -o "$tools/ffmpeg.tar.xz" \
      https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
    tar -xf "$tools/ffmpeg.tar.xz" -C "$tools" --strip-components=1 --one-top-level=ffmpeg
fi
if [ ! -f "$tools/vpl/usr/lib64/libmfx-gen.so.1.2" ]; then
    echo "== Intel oneVPL GPU runtime"
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    (cd "$tmp" && dnf -q download intel-vpl-gpu-rt >/dev/null)
    (cd "$tmp" && rpm2cpio intel-vpl-gpu-rt-*.rpm | cpio -id --quiet)
    mkdir -p "$tools/vpl/usr/lib64"
    cp -a "$tmp"/usr/lib64/libmfx-gen* "$tools/vpl/usr/lib64/"
fi
cat > "$(dirname "$0")/media-env.sh" <<ENV
export OXP3_VPL_DIR=$tools/vpl/usr/lib64
export LD_LIBRARY_PATH=$tools/vpl/usr/lib64\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}
ENV
echo "== media env: source $(dirname "$0")/media-env.sh"
