#!/bin/bash
# SPDX-License-Identifier: MIT
# build-xmx-driver.sh - build the CM-enabled (XeSS XMX) Mesa driver used by the xess-xmx path.
# Mesa main (ea40598) + our perf patches + the C-for-Metal series ported to main
# (patches/xess-main/0101-anv-cm-kernel-injection-for-main-27.patch), Wayland WSI, static expat.
# Output: ~/.cache/oxp3-fixes/mesa-xmx/usr/local/lib64/libvulkan_intel.so
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
pkg=${XMX_PKG:-$HOME/.local/share/xess-xmx}
cache=${OXP3_CACHE:-$HOME/.cache/oxp3-fixes}
tarball=${OXP3_MESA_TARBALL:-$cache/mesa/mesa-ea40598a68bab5690e0d08f845e2776ef9be2db9.tar.gz}
overrides=${OXP3_OVERRIDES:-$cache/anatase/cards/drivers/mesa/overrides.patch}
out=$cache/mesa-xmx; src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
[ -e "$tarball" ] || { echo "error: Mesa tarball not found at $tarball" >&2; exit 1; }
tar xzf "$tarball" -C "$src" --strip-components=1
# Anatase's overrides, minus the radv-defaults file (we do not build radv)
python3 - "$overrides" /tmp/oxp3-ov.patch <<'PY2'
import re, sys
s = open(sys.argv[1]).read()
parts = re.split(r'(?m)^(?=diff --git )', s)
keep = [x for x in parts if not x.startswith('diff --git a/src/amd/vulkan/00-radv-defaults.conf')]
open(sys.argv[2], 'w').write(''.join(keep))
PY2
git -C "$src" apply -p1 /tmp/oxp3-ov.patch
for p in "$here"/patches/mesa/0*.patch; do
    git -C "$src" apply -p1 "$p" || { echo "error: $(basename "$p") does not apply" >&2; exit 1; }
done
for p in "$here"/patches/xess-main/*.patch; do
    git -C "$src" apply -p1 "$p" || { echo "error: $(basename "$p") does not apply" >&2; exit 1; }
done
mkdir -p "$out"
podman run --rm -v "$src":/src:ro,z -v "$out":/out:z registry.fedoraproject.org/fedora:45 bash -lc '
set -e
dnf -y -q install meson ninja-build gcc gcc-c++ bison flex pkgconf-pkg-config python3-mako \
  python3-yaml python3-packaging python3-setuptools llvm22-devel llvm22-libs clang22 clang22-devel \
  zlib-devel expat-devel expat-static libdrm-devel libxcb-devel libX11-devel xorg-x11-proto-devel \
  libXext-devel libXrandr-devel libXfixes-devel libXxf86vm-devel libxshmfence-devel wayland-devel \
  wayland-protocols-devel libzstd-devel glslang-devel spirv-llvm-translator22-devel spirv-tools-devel \
  > /tmp/dnf.log 2>&1
ln -sf /usr/lib64/pkgconfig/LLVMSPIRVLib.pc /usr/lib64/pkgconfig/llvmspirvlib.pc 2>/dev/null || true
rm -rf /b; mkdir -p /out/usr/local
meson setup /b /src --prefix=/usr/local --buildtype=release -Dprefer_static=true \
  -Dvulkan-drivers=intel -Dgallium-drivers=iris,llvmpipe -Dplatforms=x11,wayland -Dglx=dri -Dllvm=enabled \
  > /tmp/cfg.log 2>&1 || { tail -20 /tmp/cfg.log; exit 1; }
ninja -C /b -j"$(nproc)" > /tmp/build.log 2>&1 || { grep -m15 "error:" /tmp/build.log; exit 2; }
DESTDIR=/out ninja -C /b install > /tmp/inst.log 2>&1
echo XMX_DRIVER_BUILT
'
[ -e "$out/usr/local/lib64/libvulkan_intel.so" ] || { echo "error: build produced no driver" >&2; exit 1; }
echo "driver: $out/usr/local/lib64/libvulkan_intel.so"
