#!/bin/bash
# SPDX-License-Identifier: MIT
# build-xmx-driver-263.sh - build the XMX (C-for-Metal) ANV driver on Mesa 26.3.0 final.
#
# Recipe (the 4b1d55ea rc3 build, moved to the official 26.3.0 release):
#   Mesa 26.3.0 tag
#   + the xess-xmx 20261111 CM series, vendored in patches/mesa-26.3
#   + our five 26.3 backports (patches/mesa-26.3-backports; the one already upstream is skipped)
#   + our Intel perf patches that apply to 26.3: 0200, 0201, 0206 and the ported 0207
#     (0212 depends on main-only RT refactors and is not portable to 26.3)
#   + Anatase Mesa overrides (the radv-defaults hunk is stripped, as the host build does)
#
# Output: <out>/usr/local/lib64/libvulkan_intel.so (default ~/.cache/oxp3-fixes/xmx-driver-263)
# With --install it becomes ~/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so, the old one is
# kept in ~/.cache/oxp3-fixes/xmx-driver-rc3 (instant rollback).
#
# Everything is in this repo; no upstream release archive is needed.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
cache=${OXP3_CACHE:-$HOME/.cache/oxp3-fixes}
tag=mesa-26.3.0
tarball=${OXP3_MESA263_TARBALL:-$cache/mesa/$tag.tar.gz}
url=https://gitlab.freedesktop.org/mesa/mesa/-/archive/$tag/mesa-$tag.tar.gz
out=$cache/xmx-driver-263
release=""
install_drv=0
while [ $# -gt 0 ]; do
    case $1 in
        --release-dir) release=${2:?}; shift 2 ;;
        --mesa-tarball) tarball=${2:?}; shift 2 ;;
        --out) out=${2:?}; shift 2 ;;
        --install) install_drv=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
# The CM series is vendored in patches/mesa-26.3 (release 20261111); --release-dir is
# only needed to compare against a different upstream release.

command -v podman >/dev/null || { echo "error: podman is required" >&2; exit 2; }
mkdir -p "$cache/mesa"
[ -s "$tarball" ] || curl -sfL --retry 3 -o "$tarball" "$url" || { echo "download failed: $url" >&2; exit 1; }
work=$out/src
rm -rf "$work" "$out/usr"; mkdir -p "$work" "$out"
tar xzf "$tarball" -C "$work" --strip-components=1
cd "$work"
git init -q .; git add -A; git -c user.email=x@x -c user.name=x commit -qm base
apply() { git apply -p1 "$1" 2>/dev/null || { echo "error: patch does not apply: $1" >&2; exit 1; }; }
for f in "$repo"/patches/mesa-26.3-backports/*.patch; do
    if git apply --check -R -p1 "$f" 2>/dev/null; then echo "already in $tag: $(basename "$f")"; else apply "$f"; fi
done
for f in "$repo"/patches/mesa/0200*.patch "$repo"/patches/mesa/0201*.patch \
         "$repo"/patches/mesa/0206*.patch "$repo"/patches/mesa-26.3/*.patch; do apply "$f"; done
python3 - <<'PY'
import re
s=open("/var/home/holzr/git/linux_scratch/anatase/cards/drivers/mesa/overrides.patch").read()
parts=re.split(r'(?m)^(?=diff --git )', s)
keep=[x for x in parts if not x.startswith('diff --git a/src/amd/vulkan/00-radv-defaults.conf')]
open("/tmp/overrides-263.patch","w").write("".join(keep))
PY
apply /tmp/overrides-263.patch
echo "mesa: $tag + release CM + our backports + perf patches; building with podman"
podman run --rm -v "$work":/src:ro,z -v "$out":/out:z registry.fedoraproject.org/fedora:45 bash -lc '
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
  > /tmp/cfg.log 2>&1 || { tail -25 /tmp/cfg.log; exit 1; }
ninja -C /b -j"$(nproc)" > /tmp/build.log 2>&1 || { grep -m20 "error:" /tmp/build.log; exit 2; }
DESTDIR=/out ninja -C /b install > /tmp/inst.log 2>&1
' || { echo "build failed" >&2; exit 1; }
drv=$out/usr/local/lib64/libvulkan_intel.so
[ -x "$drv" ] || { echo "error: build produced no driver" >&2; exit 1; }
sha=$(sha256sum "$drv" | cut -d' ' -f1)
echo "driver: $drv"
echo "sha256: $sha"
if [ "$install_drv" = 1 ]; then
    mkdir -p "$cache/xmx-driver-rc3"
    [ -e "$cache/xmx-driver-rc3/libvulkan_intel.so" ] || cp -a "$cache/xmx-driver/libvulkan_intel.so" "$cache/xmx-driver-rc3/libvulkan_intel.so" 2>/dev/null || true
    install -m 0755 "$drv" "$cache/xmx-driver/libvulkan_intel.so"
    echo "installed: $cache/xmx-driver/libvulkan_intel.so (rc3 backup in xmx-driver-rc3)"
fi
