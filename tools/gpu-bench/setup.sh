#!/bin/bash
# SPDX-License-Identifier: MIT
# setup.sh - install the headless GPU benchmark tools into the user cache.
#
#   tools/gpu-bench/setup.sh
#
# vkpeak/clpeak come from upstream release zips; vkmark is built from source in a
# Fedora container (host needs only the runtime Vulkan/wayland/xcb libs).
set -euo pipefail
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
bin=$cache/bin; share=$cache/share
mkdir -p "$bin" "$share"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

echo "== vkpeak"
if [ ! -x "$bin/vkpeak" ]; then
    curl -L --fail --max-time 120 -o "$tmp/vkpeak.zip" \
      https://github.com/nihui/vkpeak/releases/download/20260527/vkpeak-20260527-linux.zip
    unzip -q "$tmp/vkpeak.zip" -d "$tmp/vkpeak"
    cp "$tmp"/vkpeak-*/vkpeak "$bin/vkpeak"
fi

echo "== clpeak"
if [ ! -x "$bin/clpeak" ]; then
    curl -L --fail --max-time 120 -o "$tmp/clpeak.zip" \
      https://github.com/krrishnarraj/clpeak/releases/download/3.0.5/clpeak-3.0.5-linux-x86_64.zip
    unzip -q "$tmp/clpeak.zip" -d "$tmp/clpeak"
    cp "$tmp"/clpeak-*/bin/clpeak "$bin/clpeak"
fi

echo "== vkmark (container build)"
if [ ! -x "$bin/vkmark" ] || [ ! -e "$bin/headless.so" ]; then
    git clone -q --depth 1 https://github.com/vkmark/vkmark.git "$tmp/vkmark-src"
    mkdir -p "$tmp/vkmark-out"
    podman run --rm -v "$tmp/vkmark-src":/src:z -v "$tmp/vkmark-out":/out:z \
      registry.fedoraproject.org/fedora:45 bash -lc '
        set -e
        dnf -y -q install meson ninja-build gcc gcc-c++ pkgconf-pkg-config \
          vulkan-loader-devel glm-devel assimp-devel libxcb-devel xcb-util-wm-devel \
          wayland-devel wayland-protocols-devel libdrm-devel mesa-libgbm-devel >/dev/null
        cd /src
        meson setup build --prefix=/usr >/tmp/meson.log 2>&1
        ninja -C build >/tmp/ninja.log 2>&1
        cp build/src/vkmark /out/
        cp build/src/*.so /out/
      '
    cp "$tmp/vkmark-out/vkmark" "$tmp/vkmark-out"/*.so "$bin/"
    rm -rf "$share/vkmark-data"
    cp -r "$tmp/vkmark-src/data" "$share/vkmark-data"
fi
chmod +x "$bin/vkpeak" "$bin/clpeak" "$bin/vkmark"
echo "== ready: $bin"
ls -l "$bin"
