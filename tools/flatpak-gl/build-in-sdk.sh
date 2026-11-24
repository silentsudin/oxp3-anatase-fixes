#!/bin/bash
# SPDX-License-Identifier: MIT
# Build our Mesa as a Flatpak GL driver extension (org.freedesktop.Platform.GL.<name>).
#
# Runs INSIDE org.freedesktop.Sdk//<branch> with org.freedesktop.Sdk.Extension.llvm22 installed,
# started by build_flatpak_gl() in tools/lib/install-common.sh:
#
#   flatpak run --filesystem=<work> --unshare=network --command=bash org.freedesktop.Sdk//<branch> \
#       <work>/build-in-sdk.sh <work> <branch> <name> <mesa-src>
#
# Everything is compiled against the runtime's own libraries, so the driver loads in any app on
# that runtime branch (a host-built driver does not: it needs libdisplay-info, libSPIRV-Tools and
# a newer libexpat than org.freedesktop.Platform ships).
#
# The SDK lacks SPIRV-Tools and SPIRV-LLVM-Translator, which Mesa's CLC needs to build the Intel
# drivers' internal OpenCL kernels; both are built here as static libraries, so the extension does
# not ship them. PyYAML (needed by Mesa's code generators) is used straight from its sdist. All
# sources are downloaded and checksummed on the host beforehand; this script has no network.
set -euo pipefail

work=$1 branch=$2 name=$3 mesa_src=$4
dl=$work/dl
src=$work/src-$branch
deps=$work/deps-$branch
stage=$work/stage-$branch
prefix=/usr/lib/x86_64-linux-gnu/GL/$name
llvm=/usr/lib/sdk/llvm22
jobs=$(nproc)

[[ -x $llvm/bin/llvm-config ]] ||
    { echo "error: $llvm missing - install org.freedesktop.Sdk.Extension.llvm22//$branch" >&2; exit 1; }

export PATH=$llvm/bin:$PATH
export PKG_CONFIG_PATH=$deps/lib/pkgconfig:$deps/lib64/pkgconfig:$deps/share/pkgconfig
export CMAKE_PREFIX_PATH=$deps
export PYTHONPATH=$src/pyyaml/lib

step() { printf '\n== [flatpak-gl %s] %s\n' "$branch" "$*"; }

unpack() { # <tarball> <dest dir>
    rm -rf "$2"; mkdir -p "$2"
    tar -xzf "$1" -C "$2" --strip-components=1
}

mkdir -p "$src" "$deps"

# --- sources (dependencies are cached across Mesa rebuilds; the stamp names their versions) -----
deps_stamp=$deps/.done-$(basename "$(ls "$dl"/SPIRV-Tools-*.tar.gz)" .tar.gz)-$(basename "$(ls "$dl"/SPIRV-LLVM-Translator-*.tar.gz)" .tar.gz)
if [[ ! -e $deps_stamp ]]; then
    rm -rf "$deps"; mkdir -p "$deps"
    step "unpack dependency sources"
    unpack "$dl"/SPIRV-Tools-*.tar.gz "$src/SPIRV-Tools"
    unpack "$dl/SPIRV-Headers-$(cat "$dl/spirv-tools-headers.rev").tar.gz" "$src/SPIRV-Tools/external/spirv-headers"
    unpack "$dl"/SPIRV-LLVM-Translator-*.tar.gz "$src/SPIRV-LLVM-Translator"
    unpack "$dl/SPIRV-Headers-$(cat "$dl/translator-headers.rev").tar.gz" "$src/SPIRV-Headers-translator"

    step "SPIRV-Tools (static)"
    cmake -S "$src/SPIRV-Tools" -B "$src/b-spirv-tools" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$deps" -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_SHARED_LIBS=OFF \
        -DSPIRV_SKIP_TESTS=ON -DSPIRV_SKIP_EXECUTABLES=ON -DSPIRV_WERROR=OFF >/dev/null
    ninja -C "$src/b-spirv-tools" -j"$jobs" install >/dev/null

    step "SPIRV-LLVM-Translator (static, against LLVM $(llvm-config --version))"
    cmake -S "$src/SPIRV-LLVM-Translator" -B "$src/b-translator" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$deps" -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_SHARED_LIBS=OFF \
        -DLLVM_DIR="$llvm/lib/cmake/llvm" -DLLVM_INCLUDE_TESTS=OFF -DLLVM_SPIRV_INCLUDE_TESTS=OFF \
        -DLLVM_EXTERNAL_SPIRV_HEADERS_SOURCE_DIR="$src/SPIRV-Headers-translator" >/dev/null
    ninja -C "$src/b-translator" -j"$jobs" install >/dev/null
    touch "$deps_stamp"
else
    step "SPIRV-Tools / SPIRV-LLVM-Translator: cached"
fi
unpack "$dl"/pyyaml-*.tar.gz "$src/pyyaml"
python3 -c 'import yaml, mako' || { echo "error: PyYAML/mako not importable" >&2; exit 1; }

# --- Mesa ----------------------------------------------------------------------------------------
step "Mesa $(cat "$mesa_src/VERSION") -> $prefix"
rm -rf "$src/b-mesa" "$stage"
# Intel Vulkan (ANV) + iris GL for the B390, llvmpipe/softpipe so software fallback still works
# through our libgallium. GL.default stays active next to this extension (FLATPAK_GL_DRIVERS=
# <name>:default) and supplies everything not built here: other vendors, lavapipe, OpenCL,
# 32-bit GL. Vulkan layers come from GL.default as well.
meson setup "$src/b-mesa" "$mesa_src" --wrap-mode=nodownload \
    --prefix="$prefix" --libdir=lib --datadir=share --sysconfdir=/etc \
    --buildtype=release -Db_ndebug=true \
    -Dplatforms=x11,wayland \
    -Dgallium-drivers=iris,llvmpipe,softpipe \
    -Dvulkan-drivers=intel \
    -Dvulkan-layers=[] \
    -Dglvnd=enabled -Degl=enabled -Dgbm=enabled -Dglx=dri -Dgles1=disabled -Dgles2=enabled \
    -Dllvm=enabled -Dshared-llvm=enabled \
    -Dmesa-clc=enabled -Dspirv-tools=enabled -Dmicrosoft-clc=disabled \
    -Dgallium-rusticl=false -Dgallium-va=disabled \
    -Ddisplay-info=disabled -Dlmsensors=disabled -Dlibunwind=disabled -Dvalgrind=disabled \
    -Dbuild-tests=false -Dzstd=enabled -Dexpat=enabled \
    > "$src/mesa-configure.log" 2>&1 || { tail -40 "$src/mesa-configure.log"; exit 1; }
ninja -C "$src/b-mesa" -j"$jobs" > "$src/mesa-build.log" 2>&1 ||
    { grep -m20 -E 'error|Error' "$src/mesa-build.log"; tail -20 "$src/mesa-build.log"; exit 1; }
DESTDIR="$stage" ninja -C "$src/b-mesa" install > "$src/mesa-install.log" 2>&1

# --- extension layout ----------------------------------------------------------------------------
# GL.default's layout: vulkan/icd.d and glvnd/egl_vendor.d at the extension root, which the
# runtime's merge-dirs pick up. The ICD json keeps its absolute library_path under $prefix.
step "extension layout"
root=$stage$prefix
for d in vulkan glvnd; do
    [[ -d $root/share/$d ]] && { rm -rf "${root:?}/$d"; mv "$root/share/$d" "$root/$d"; }
done
rm -rf "$root/include" "$root/lib/pkgconfig" "$root/share/doc" "$root/share/man"
# libgallium links libLLVM.so.22.1 from the SDK extension: ship it, so the driver does not depend
# on GL.default happening to carry the same LLVM.
cp -L "$llvm/lib/libLLVM.so.$(llvm-config --version | cut -d. -f1-2)" "$root/lib/"
# Anything libgallium/libvulkan_intel need that neither the Platform nor this tree provides?
missing=$(LD_LIBRARY_PATH=$root/lib ldd "$root"/lib/libgallium-*.so "$root/lib/libvulkan_intel.so" \
    | awk '/not found/ {print $1}' | sort -u)
[[ -z $missing ]] || { echo "error: unresolved libraries: $missing" >&2; exit 1; }
ls "$root" "$root/lib" "$root/vulkan/icd.d"
echo FLATPAK_GL_BUILD_DONE
