#!/bin/bash
# FAST variant: identical to oxp3-fixes.sh except Mesa. Instead of rebuilding Mesa RPMs it installs
# the already-built podman/meson Intel Vulkan driver that the benchmarks were measured with
# (default: mesa-build/install-shiprt/usr/local; override with OXP3_MESA_BUILD), into the writable
# /usr overlay with a backup manifest, so 'uninstall' restores the stock driver. GL stays stock.
# Restore OneXPlayer 3 support on Anatase after an update:
#   - gamescope: PQ HDR on the SDC AMS881KB01 OLED (no black screens, correct peak brightness)
#   - Mesa:      26.3 branch (Jay shader compiler on Xe3, ANV fixes) from Anatase's packaging
#   - XeSS XMX:  xess-xmx-linux (our fork): XeSS SR/FG on the XMX units in DX12 games under Proton
#   - MangoHud:  CPU power, GPU power and VRAM in the Gamemode overlay on Intel iGPUs,
#                with power from the rapl-power service (counters stay root-only)
#   - kernel:    MOK-signed xe, iwlwifi and btintel modules for the installed Anatase kernel:
#                VBT VS/PE-O backport (white dots, HDR retoggle black screen), VRR/DSB timing,
#                resume out-of-memory fix, 7.2.13 stable and 7.3/7.4 Wi-Fi/Bluetooth fixes
#   - firmware:  display DMC v2.20 (VRR cap with v2.36), Bluetooth 24.80, CPU microcode 0x11c
#   - extras:    MSR PL1 kept at hhd's TDP, speaker route check after boot/resume and speaker
#                name, volume rocker taps (hhd patch + atkbd force-release), no 5 s xpad wait on
#                suspend, GPU power zone check at boot, thermald off (no tables in this BIOS)
#
#   ./oxp3-fixes.sh build       build everything for the installed Anatase (as your user)
#   ./oxp3-fixes.sh install     install (asks for sudo). Twice after a kernel change: the first
#                               run stages the kernel modules + firmware (reboot), the second puts
#                               gamescope and MangoHud into the writable /usr overlay
#   ./oxp3-fixes.sh all         build + install
#   ./oxp3-fixes.sh status      what is installed / layered / overridden
#   ./oxp3-fixes.sh uninstall   back to stock Anatase (asks for sudo), then reboot
#   ./oxp3-fixes.sh mok-setup   create a module signing key and queue it for enrolment (once)
#
# Options (after the command): --skip-kernel --skip-gamescope --skip-mangohud --skip-firmware
#                              --skip-mesa --skip-xess --skip-extras (TDP sync, speakers, volume rocker,
#                              suspend)
#                              --stock-dmc (keep Anatase's display DMC firmware)
# Environment: OXP3_MOK_DIR (default ~/.local/share/oxp3-fixes/mok, needs MOK.key + MOK.der)
#              OXP3_CACHE   (default ~/.cache/oxp3-fixes)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
CACHE="${OXP3_CACHE:-$HOME/.cache/oxp3-fixes}"
MOK_DIR="${OXP3_MOK_DIR:-$HOME/.local/share/oxp3-fixes/mok}"
OUT="$CACHE/out"
ANATASE_REPO=https://github.com/anatase-org/anatase.git
PATCHWORK_REPO=https://github.com/anatase-org/patchwork.git
FEDORA="$(rpm -E %fedora)"
IMAGE="localhost/oxp3-fixes-build:f$FEDORA"
CTR="oxp3-fixes-build-f$FEDORA"
KMOD_NAME="kmod-oxp3"
STAMP=$(date +%Y%m%d%H%M)

SKIP_KERNEL=0 SKIP_GAMESCOPE=0 SKIP_MANGOHUD=0 SKIP_FIRMWARE=0 SKIP_DMC=0 SKIP_EXTRAS=0 SKIP_MESA=0 SKIP_XESS=0
cmd="${1:-}"; shift || true
for a in "$@"; do
    case $a in
        --skip-kernel) SKIP_KERNEL=1 ;;
        --skip-gamescope) SKIP_GAMESCOPE=1 ;;
        --skip-mangohud) SKIP_MANGOHUD=1 ;;
        --skip-firmware) SKIP_FIRMWARE=1 ;;
        --stock-dmc) SKIP_DMC=1 ;;
        --skip-extras) SKIP_EXTRAS=1 ;;
        --skip-mesa) SKIP_MESA=1 ;;
        --skip-xess) SKIP_XESS=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The Anatase kernel in the current deployment (not necessarily the booted one).
anatase_kver() { rpm -q kernel-core --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' | head -1; }

# ---------------------------------------------------------------------------------------------
# build container
# ---------------------------------------------------------------------------------------------
ctr() { podman exec -w "$CACHE" "$CTR" "$@"; }
ctr_root() { podman exec -u 0 -w "$CACHE" "$CTR" "$@"; }

ensure_container() {
    mkdir -p "$CACHE" "$OUT"
    # rebuild the image when the Containerfile changes
    local sum; sum=$(sha256sum "$here/container/Containerfile" | cut -c1-12)
    if [[ $(podman image inspect "$IMAGE" --format '{{index .Labels "oxp3.containerfile"}}' 2>/dev/null) != "$sum" ]]; then
        podman rm -f "$CTR" >/dev/null 2>&1 || true
        podman rmi -f "$IMAGE" >/dev/null 2>&1 || true
    fi
    if ! podman image exists "$IMAGE"; then
        say "creating build image $IMAGE (once, a few minutes)"
        podman build --build-arg FEDORA="$FEDORA" --label oxp3.containerfile="$sum" -t "$IMAGE" \
            -f "$here/container/Containerfile" "$here/container"
    fi
    # recreate the container if it was made with another MOK directory
    if podman container exists "$CTR" &&
       ! podman inspect "$CTR" --format '{{range .Mounts}}{{.Destination}} {{end}}' | grep -qF "$MOK_DIR"; then
        podman rm -f "$CTR" >/dev/null
    fi
    if ! podman container exists "$CTR"; then
        podman run -d --name "$CTR" --userns=keep-id --security-opt label=disable \
            -v "$CACHE:$CACHE" -v "$here:$here:ro" -v "$MOK_DIR:$MOK_DIR:ro" \
            "$IMAGE" sleep infinity >/dev/null 2>&1 || {
            # MOK dir may not exist when only userspace parts are built
            podman run -d --name "$CTR" --userns=keep-id --security-opt label=disable \
                -v "$CACHE:$CACHE" -v "$here:$here:ro" "$IMAGE" sleep infinity >/dev/null; }
    fi
    podman start "$CTR" >/dev/null
}

# ---------------------------------------------------------------------------------------------
# gamescope / MangoHud: Anatase's packaging + our patches, rebuilt as RPMs
# ---------------------------------------------------------------------------------------------
fetch_anatase() {
    if [[ -d $CACHE/anatase/.git ]]; then
        git -C "$CACHE/anatase" fetch -q --depth 1 origin HEAD
        git -C "$CACHE/anatase" reset -q --hard FETCH_HEAD
    else
        git clone -q --depth 1 "$ANATASE_REPO" "$CACHE/anatase"
    fi
    echo "anatase packaging: $(git -C "$CACHE/anatase" log -1 --format='%h %cd %s' --date=short)"
}

# build_card <name> <patch-line-regex> <patch files...>
build_card() {
    local name=$1 anchor=$2; shift 2
    local top="$CACHE/rpmbuild/$name" spec
    rm -rf "$top"; mkdir -p "$top/SOURCES"
    cp -r "$CACHE/anatase/cards/gaming/$name/." "$top/SOURCES/"
    spec="$top/SOURCES/$name.spec"
    # add our patches right after Anatase's overrides.patch, in order
    local n=2000 lines="" p
    for p in "$@"; do
        cp "$p" "$top/SOURCES/"
        lines+="Patch$n: $(basename "$p")\n"; n=$((n + 1))
    done
    grep -qE "$anchor" "$spec" || die "$name.spec: no '$anchor' line - Anatase packaging changed"
    sed -i -E "0,/$anchor/s//&\n${lines%\\n}/" "$spec"
    local ver; ver=$(sed -n 's/^Version:[[:space:]]*//p' "$spec")
    local inst; inst=$(rpm -q "$name" --qf '%{VERSION}\n' 2>/dev/null | head -1 || true)
    [[ -z $inst || $inst == "$ver" ]] || echo "note: installed $name $inst, Anatase packaging has $ver"

    say "$name $ver: sources"
    ctr spectool -g -C "$top/SOURCES" "$spec" >/dev/null
    ctr_root dnf -y -q builddep "$spec" >/dev/null 2>&1 || ctr_root dnf -y builddep --skip-unavailable "$spec" | tail -3
    say "$name $ver: x86_64"
    ctr rpmbuild --define "_topdir $top" --define "dist .fc$FEDORA.oxp3.$STAMP" -bb "$spec" > "$top/build.log" 2>&1 ||
        { tail -30 "$top/build.log"; die "$name x86_64 build failed (full log: $top/build.log)"; }
    say "$name $ver: i686"
    ctr bash -c "export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig; setarch i686 rpmbuild --target i686 --nodeps --define '_topdir $top' --define 'dist .fc$FEDORA.oxp3.$STAMP' -bb '$spec'" \
        > "$top/build-i686.log" 2>&1 ||
        { tail -30 "$top/build-i686.log"; die "$name i686 build failed (full log: $top/build-i686.log)"; }
    rm -f "$OUT/$name"-*.rpm
    find "$top/RPMS" -name "$name-$ver-*.rpm" ! -name '*debug*' -exec cp {} "$OUT/" \;
    ls "$OUT/$name"-*.rpm
}

# ---------------------------------------------------------------------------------------------
# Mesa 26.3 (Jay shader compiler on Xe2/Xe3, compression/tiling work) from Anatase's packaging:
# the 26.3 branch at MESA_COMMIT plus patches/mesa, x86_64 + i686, all installed subpackages
# ---------------------------------------------------------------------------------------------
MESA_COMMIT=ea40598a68bab5690e0d08f845e2776ef9be2db9     # Mesa main, 2026-11-05 (the tree every measured win used)
MESA_VERSION="27.0.0~devel.20261105git${MESA_COMMIT:0:8}"  # no "^": Cargo.toml needs semver
MESA_URL=https://gitlab.freedesktop.org/mesa/mesa/-/archive/$MESA_COMMIT/mesa-$MESA_COMMIT.tar.gz
# i686 -devel packages the 32-bit build needs beyond the container's (dnf builddep is x86_64 only)
MESA_I686_DEVEL="llvm-devel.i686 clang-devel.i686 spirv-llvm-translator-devel.i686 spirv-tools-devel.i686
    libdisplay-info-devel.i686 elfutils-libelf-devel.i686 libva-devel.i686 lm_sensors-devel.i686
    libzstd-devel.i686 libXdamage-devel.i686 libunwind-devel.i686 valgrind-devel.i686
    libselinux-devel.i686 zlib-ng-compat-devel.i686 expat-devel.i686 libglvnd-devel.i686
    DirectX-Headers-devel.i686 libedit-devel.i686 libpciaccess-devel.i686 libxml2-devel.i686
    xz-devel.i686 ncurses-devel.i686 libuuid-devel.i686 libclc22-devel.i686"

_built="$HOME/.cache/oxp3-fixes/mesa-fast/usr/local"
_legacy="$HOME/git/linux_scratch/mesa-build/install-shiprt/usr/local"
if [[ -n ${OXP3_MESA_BUILD:-} ]]; then MESA_BUILD_DIR=$OXP3_MESA_BUILD
elif [[ -x $_built/lib64/libvulkan_intel.so ]]; then MESA_BUILD_DIR=$_built
else MESA_BUILD_DIR=$_legacy; fi
MESA_BACKUP=/usr/local/share/oxp3-fixes-fast

build_mesa() {
    # Reuse an explicitly selected tree, otherwise build with podman (the recipe the benchmarks used)
    if [[ -n ${OXP3_MESA_BUILD:-} && -x $OXP3_MESA_BUILD/lib64/libvulkan_intel.so ]]; then
        MESA_BUILD_DIR=$OXP3_MESA_BUILD
        say "mesa: using $MESA_BUILD_DIR (OXP3_MESA_BUILD)"
        return 0
    fi
    local out="$CACHE/mesa-fast/usr/local" src="$CACHE/mesa-fast/src"
    if [[ -x $out/lib64/libvulkan_intel.so ]]; then
        MESA_BUILD_DIR=$out
        say "mesa: reusing the podman build in $out"
        return 0
    fi
    command -v podman >/dev/null || die "podman is required to build Mesa"
    local tarball="$CACHE/mesa/mesa-$MESA_COMMIT.tar.gz"
    mkdir -p "$CACHE/mesa" "$CACHE/mesa-fast"
    [[ -s $tarball ]] || curl -sfL --retry 3 -o "$tarball" "$MESA_URL" || die "download failed: $MESA_URL"

    say "mesa: source $MESA_COMMIT + patches/mesa"
    rm -rf "$src"; mkdir -p "$src"
    tar xzf "$tarball" -C "$src" --strip-components=1
    # Anatase's overrides.patch, minus the RADV Forza Horizon hunk (AMD only)
    python3 - "$CACHE/anatase/cards/drivers/mesa/overrides.patch" "$CACHE/mesa-fast/overrides.patch" <<'PYEOF'
import re, sys
s = open(sys.argv[1]).read()
parts = re.split(r'(?m)^(?=diff --git )', s)
keep = [x for x in parts if not x.startswith('diff --git a/src/amd/vulkan/00-radv-defaults.conf')]
open(sys.argv[2], 'w').write(''.join(keep))
PYEOF
    git -C "$src" apply -p1 "$CACHE/mesa-fast/overrides.patch" || die "overrides.patch does not apply"
    local f
    for f in "$here"/patches/mesa/*.patch; do
        git -C "$src" apply -p1 "$f" || die "$(basename "$f") does not apply to $MESA_COMMIT"
    done

    say "mesa: podman meson build (fedora:45)"
    podman run --rm -v "$src":/src:ro,z -v "$CACHE/mesa-fast":/out:z registry.fedoraproject.org/fedora:45 bash -lc '
set -e
dnf -y -q install meson ninja-build gcc gcc-c++ bison flex pkgconf-pkg-config python3-mako \
  python3-yaml python3-packaging python3-setuptools llvm22-devel llvm22-libs clang22 clang22-devel \
  zlib-devel expat-devel libdrm-devel libxcb-devel libX11-devel xorg-x11-proto-devel libXext-devel \
  libXrandr-devel libXfixes-devel libxshmfence-devel wayland-devel wayland-protocols-devel \
  libzstd-devel glslang-devel spirv-llvm-translator22-devel spirv-tools-devel libXxf86vm-devel > /tmp/dnf.log 2>&1
# mesa asks pkg-config for lowercase 'llvmspirvlib'; Fedora ships 'LLVMSPIRVLib.pc'
ln -sf /usr/lib64/pkgconfig/LLVMSPIRVLib.pc /usr/lib64/pkgconfig/llvmspirvlib.pc 2>/dev/null || true
rm -rf /b /out/usr; mkdir -p /out/usr/local
meson setup /b /src --prefix=/usr/local --buildtype=release \
  -Dvulkan-drivers=intel -Dgallium-drivers=iris,llvmpipe -Dplatforms=x11 -Dglx=dri -Dllvm=enabled \
  > /tmp/cfg.log 2>&1 || { tail -25 /tmp/cfg.log; exit 1; }
ninja -C /b -j"$(nproc)" > /tmp/build.log 2>&1 || { grep -m15 "error:" /tmp/build.log; exit 2; }
DESTDIR=/out ninja -C /b install > /tmp/inst.log 2>&1
cp /usr/lib64/libexpat.so.1 /out/usr/local/lib64/ 2>/dev/null || true
echo MESA_PODMAN_BUILD_DONE
' || die "Mesa build failed (see the podman output above)"
    [[ -x $out/lib64/libvulkan_intel.so ]] || die "build produced no libvulkan_intel.so"
    MESA_BUILD_DIR=$out
    say "mesa: built $out ($(strings -a "$out/lib64/libvulkan_intel.so" | grep -m1 -oE 'Mesa [0-9][^ ]*'))"
}

install_mesa_fast() {
    local src="$MESA_BUILD_DIR/lib64" manifest="$MESA_BACKUP/mesa.manifest" f target
    sudo mkdir -p "$MESA_BACKUP"
    : | sudo tee "$manifest" >/dev/null
    for f in libvulkan_intel.so libexpat.so.1; do
        [[ -e $src/$f ]] || continue
        target=/usr/lib64/$f
        if [[ ! -e $MESA_BACKUP/stock/$f ]]; then
            sudo mkdir -p "$MESA_BACKUP/stock"
            [[ -e $target ]] && sudo cp -a "$target" "$MESA_BACKUP/stock/$f"
        fi
        sudo install -m 0755 "$src/$f" "$target"
        echo "/usr/lib64/$f" | sudo tee -a "$manifest" >/dev/null
    done
    echo "  $(grep -c . "$manifest") Mesa file(s) into /usr/lib64 (stock backed up in $MESA_BACKUP/stock)"
}

remove_mesa_fast() {
    local manifest="$MESA_BACKUP/mesa.manifest" f
    [[ -r $manifest ]] || return 0
    while read -r f; do
        [[ -z $f ]] && continue
        local base=${f##*/}
        if [[ -e $MESA_BACKUP/stock/$base ]]; then
            sudo cp -a "$MESA_BACKUP/stock/$base" "$f"
        else
            sudo rm -f "$f"
        fi
    done < <(sudo cat "$manifest")
    sudo rm -f "$manifest"
    echo "  stock driver restored"
}

# The Steam Flatpak runtime keeps its own copy; same list, our file only
install_flatpak_mesa_fast() {
    local src="$MESA_BUILD_DIR/lib64"
    [[ -d $FLATPAK_RT/overrides ]] || { echo "  no Anatase Flatpak runtime overrides - Steam keeps Anatase's Mesa"; return 0; }
    [[ -e $src/libvulkan_intel.so ]] || return 0
    sudo mkdir -p "$FLATPAK_RT/overrides/lib64" "$MESA_BACKUP/flatpak-stock"
    # The runtime's own expat is older than the one the driver links against
    # (LIBEXPAT_2.9.0 missing), so overriding only libvulkan_intel.so makes the Intel ICD
    # fail to load inside the sandbox and Vulkan silently falls back to lavapipe (software
    # rendering: single-digit FPS, CPU pegged). Ship the same expat the host install uses.
    if [[ ! -e $MESA_BACKUP/flatpak-stock/libexpat.so.1 ]]; then
        [[ -e $FLATPAK_RT/files/lib64/libexpat.so.1 ]] &&
            sudo cp -a "$FLATPAK_RT/files/lib64/libexpat.so.1" "$MESA_BACKUP/flatpak-stock/libexpat.so.1"
    fi
    sudo install -m 0755 "$src/libvulkan_intel.so" "$FLATPAK_RT/overrides/lib64/libvulkan_intel.so"
    local list=$'lib64/libvulkan_intel.so
'
    if [[ -e $src/libexpat.so.1 ]]; then
        sudo install -m 0755 "$src/libexpat.so.1" "$FLATPAK_RT/overrides/lib64/libexpat.so.1"
        list+=$'lib64/libexpat.so.1
'
    fi
    printf '%s' "$list" | sudo tee "$FLATPAK_MESA_LIST" >/dev/null
    echo "  Intel Vulkan driver + bundled expat in the Steam Flatpak runtime overrides"
    refresh_flatpak_runtime
}

# ---------------------------------------------------------------------------------------------
# XeSS on XMX (xess-xmx-linux, our fork with Anatase support): the Mesa patches are in
# patches/mesa (0101-0105); here the shim, ControlLib, scripts and compiler for the games
# ---------------------------------------------------------------------------------------------
XESS_COMMIT=9532c79cebd55b8e3bac6f6498a49575a63d249b      # silentsudin/xess-xmx-linux, branch anatase
XESS_SRC_URL=https://codeload.github.com/silentsudin/xess-xmx-linux/tar.gz/$XESS_COMMIT
# the Windows DLLs from upstream's release (built there with MSVC)
XESS_ZIP_URL=https://github.com/HHHHanasak1/xess-xmx-linux/releases/download/20261029/xess-xmx-linux-20261029.zip
XESS_ZIP_SHA=b195b7a9247e429ae2583528110b033de26f97e99d561b6a8b586b50497843d2
XESS_DLLS="igdext64.dll|580ddcce0929fb8942062cd2a7413d9c6ec44ebfb2eacd40ff2f2a3ab16b39ec ControlLib.dll|b57c70cf7cbfc38d2f929a552dab69195c9ca04d249f1201960a752c5c53598c"
# under home: the Steam Flatpak and the game's container both see it
XESS_PKG=${OXP3_XESS_DIR:-$HOME/.local/share/xess-xmx}

build_xess() {
    local dl="$CACHE/xess" stage="$OUT/xess-xmx" zip src entry name sha
    mkdir -p "$dl"
    say "XeSS XMX: fork ${XESS_COMMIT:0:8} + release DLLs"
    zip="$dl/${XESS_ZIP_URL##*/}"
    if [[ $(sha256sum "$zip" 2>/dev/null | cut -d' ' -f1) != "$XESS_ZIP_SHA" ]]; then
        curl -sfL --retry 3 -o "$zip.part" "$XESS_ZIP_URL" || die "download failed: $XESS_ZIP_URL"
        [[ $(sha256sum "$zip.part" | cut -d' ' -f1) == "$XESS_ZIP_SHA" ]] || die "checksum mismatch: $XESS_ZIP_URL"
        mv "$zip.part" "$zip"
    fi
    src="$dl/xess-xmx-linux-$XESS_COMMIT.tar.gz"
    [[ -s $src ]] || curl -sfL --retry 3 -o "$src" "$XESS_SRC_URL" || die "download failed: $XESS_SRC_URL"
    rm -rf "$stage"; mkdir -p "$stage"
    tar -xzf "$src" --strip-components=1 -C "$stage"
    for entry in $XESS_DLLS; do
        name=${entry%%|*}; sha=${entry#*|}
        unzip -p "$zip" "*/$name" > "$stage/$name"
        [[ $(sha256sum "$stage/$name" | cut -d' ' -f1) == "$sha" ]] || die "$name in the release zip has an unexpected checksum"
    done
    echo "  $stage"
}

# Intel's compiler as get-igc.sh would fetch it (IGC 2.10.10, ocloc 25.18.1), from an earlier package
# download, instead of 230 MB again
seed_xess_compiler() {
    [[ -x $(ls "$XESS_PKG"/igc/neo/bin/ocloc* 2>/dev/null | head -1) ]] && return 0
    local d
    for d in "$HOME"/Downloads/xess-xmx-linux-*/igc; do
        [[ -x $d/neo/bin/ocloc-25.18.1 ]] && ls "$d"/igc/local/lib/libigc.so.2.10.10* >/dev/null 2>&1 || continue
        mkdir -p "$XESS_PKG/igc"; cp -a "$d"/. "$XESS_PKG/igc/"
        echo "  compiler bundle copied from $d"
        return 0
    done
}

install_xess() {
    [[ -d $OUT/xess-xmx ]] || die "missing $OUT/xess-xmx - run '$0 build' first"
    if ! grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so; then
        echo "  the installed Mesa has no CM kernel injection (built before patches/mesa 0101?) - XeSS XMX skipped"
        return 0
    fi
    mkdir -p "$XESS_PKG"
    # the package's own state (compiler, XeSS SDK, kernels, ICD file) stays
    rsync -a --delete --exclude /igc/ --exclude /xess/ --exclude /kernels/ --exclude /run/ --exclude /lib/ \
        "$OUT/xess-xmx/" "$XESS_PKG/"
    seed_xess_compiler
    bash "$XESS_PKG/install.sh" | sed 's/^/  /' || echo "  WARNING: xess-xmx-linux's install.sh failed"
    echo "  Restart Steam (leave and re-enter Gamemode) so games get the new environment."
}

uninstall_xess() {
    [[ -x $XESS_PKG/uninstall.sh ]] || return 0
    bash "$XESS_PKG/uninstall.sh" | sed 's/^/  /' || true
    echo "  $XESS_PKG (compiler, kernel cache) left in place; delete it to free about 300 MB"
}

status_xess() {
    local f; f=$(xmx_override_file)
    echo "  XeSS XMX: $(head -c 200 "${XDG_CACHE_HOME:-$HOME/.cache}/xess-xmx/driver-status" 2>/dev/null || echo 'no driver check yet')"
    grep -q '^ANV_CM_HELPER=' "$f" 2>/dev/null && echo "    Steam Flatpak override: $(grep -E '^(ANV_CM_HELPER|VKD3D_DISABLE_EXTENSIONS|force_vk_vendor)=' "$f" | tr '\n' ' ')"
    local p n=0 no=""
    for p in "$HOME/.local/share/Steam/steamapps/common/Proton"* "$HOME/.local/share/Steam/compatibilitytools.d/"*; do
        [[ -f $p/proton ]] || continue
        if grep -aq IGDEXT_ "$p/files/lib/wine/igdext/x86_64-windows/igdext64.dll" 2>/dev/null; then n=$((n + 1)); else no+=" ${p##*/};"; fi
    done
    echo "    Protons with the shim: $n${no:+; without:${no%;}}"
}
xmx_override_file() { echo "${XDG_DATA_HOME:-$HOME/.local/share}/flatpak/overrides/org.anatase.Steam"; }

# ---------------------------------------------------------------------------------------------
# kernel: xe, iwlwifi and btintel modules with our patches, for the installed Anatase kernel
# ---------------------------------------------------------------------------------------------
# <directory in the kernel tree>|<modules built there that we ship>. iwlwifi ships with all its
# op modes, since the patches touch its shared core; btintel's patches only change function
# bodies, so the stock btusb/hci_uart keep working with ours.
KMODS=(
    "drivers/gpu/drm/xe|xe"
    "drivers/net/wireless/intel/iwlwifi|iwlwifi dvm/iwldvm mvm/iwlmvm mld/iwlmld"
    "drivers/bluetooth|btintel btintel_pcie"
)

# Apply patches/kernel/*.patch in order. A patch Anatase already carries is skipped, one that
# neither applies nor is already there stops the build. Prints the number applied.
apply_kernel_patches() {
    local src=$1 p n=0 skipped=()
    for p in "$here"/patches/kernel/*.patch; do
        if git -C "$src" -c user.name=oxp3-fixes -c user.email=oxp3-fixes@localhost am -q -3 "$p" >/dev/null 2>&1; then
            # git am succeeds without a commit when the change is already there
            if [[ $(git -C "$src" log -1 --format=%s) == "$(sed -n 's/^Subject: \(\[PATCH[^]]*\] \)\{0,1\}//p' "$p" | head -1)"* ]]; then
                n=$((n + 1))
            else
                skipped+=( "$(basename "$p")" )
            fi
        else
            git -C "$src" am --abort >/dev/null 2>&1 || true
            if git -C "$src" apply --check -R "$p" >/dev/null 2>&1; then
                skipped+=( "$(basename "$p")" )
            else
                die "kernel patch $(basename "$p") doesn't apply to this kernel - it needs updating"
            fi
        fi
    done
    (( ${#skipped[@]} )) && printf '  already in Anatase, skipped: %s\n' "${skipped[@]}" >&2
    echo "$n"
}

build_kmods() {
    local kver tag src kb cfg hash
    kver=$(anatase_kver)
    tag=${kver%%.fc*}                              # 7.2.7-an19.fc45.x86_64 -> 7.2.7-an19
    cfg=/usr/lib/modules/$kver/config
    [[ -r $cfg ]] || die "no kernel config at $cfg"
    [[ -r $MOK_DIR/MOK.key && -r $MOK_DIR/MOK.der ]] ||
        die "MOK key not found in $MOK_DIR (see README: Secure Boot key), or use --skip-kernel"
    src="$CACHE/kernel/src-$tag"; kb="$CACHE/kernel/build-$tag"

    say "kernel $kver: source (anatase-org/patchwork tag $tag)"
    if [[ ! -d $src/.git ]]; then
        git ls-remote --exit-code --tags "$PATCHWORK_REPO" "refs/tags/$tag" >/dev/null ||
            die "no tag $tag in anatase-org/patchwork (or GitHub unreachable)"
        local try
        for try in 1 2 3; do
            git clone -q --depth 1 --branch "$tag" "$PATCHWORK_REPO" "$src" && break
            rm -rf "$src"
            (( try < 3 )) || die "cloning anatase-org/patchwork $tag failed 3 times (network?)"
            echo "clone failed, retrying ($try/3)"; sleep 10
        done
    fi
    git -C "$src" reset -q --hard "$tag" 2>/dev/null || git -C "$src" reset -q --hard HEAD
    git -C "$src" clean -qfdx
    local applied; applied=$(apply_kernel_patches "$src")
    if (( applied == 0 )); then
        echo "kernel $tag already contains every patch - no modules needed"
        rm -f "$OUT/$KMOD_NAME"-*.rpm; touch "$OUT/.kernel-not-needed"; return 0
    fi
    echo "  $applied kernel patches applied"
    rm -f "$OUT/.kernel-not-needed"
    # out-of-tree drivers Anatase adds as submodules: stub their Kconfig, our modules don't use them
    local k
    for k in $(sed -n 's/^source "\(drivers\/custom\/[^"]*\)"/\1/p' "$src/drivers/custom/Kconfig" 2>/dev/null); do
        [[ -e $src/$k ]] || { mkdir -p "$(dirname "$src/$k")"; : > "$src/$k"; }
    done

    say "kernel $kver: building modules (15-30 min)"
    rm -rf "$kb"; mkdir -p "$kb"; cp "$cfg" "$kb/.config"
    local entry dir mods m make_m=""
    for entry in "${KMODS[@]}"; do
        make_m+=" && make -s O='$kb' KERNELRELEASE='$kver' KBUILD_MODPOST_WARN=1 -j\$(nproc) M=${entry%%|*} modules"
    done
    # Anatase's symvers lets modpost resolve the kernel's exports, so a symbol our modules need
    # but the running kernel lacks shows up here instead of as a module that won't load.
    [[ -r /usr/lib/modules/$kver/symvers.xz ]] || die "no /usr/lib/modules/$kver/symvers.xz"
    xz -dc "/usr/lib/modules/$kver/symvers.xz" > "$kb/Module.symvers"
    ctr bash -c "cd '$src' && make -s O='$kb' KERNELRELEASE='$kver' olddefconfig &&
        make -s O='$kb' KERNELRELEASE='$kver' -j\$(nproc) modules_prepare $make_m" \
        > "$kb/build.log" 2>&1 || { tail -30 "$kb/build.log"; die "module build failed (full log: $kb/build.log)"; }
    local undef; undef=$(grep -E 'modpost: ".*" \[.*\] undefined!' "$kb/build.log" || true)
    [[ -z $undef ]] || { echo "$undef" | head -20; die "modules need kernel symbols $kver doesn't export"; }

    say "kernel $kver: signing with $MOK_DIR/MOK.der"
    hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"/\1/p' "$cfg"); hash=${hash:-sha512}
    local stage="$CACHE/kernel/stage-$tag" ko rel_path files=()
    rm -rf "$stage"; mkdir -p "$stage"
    for entry in "${KMODS[@]}"; do
        dir=${entry%%|*}; mods=${entry#*|}
        for m in $mods; do
            ko="$src/$dir/$m.ko"
            [[ -f $ko ]] || die "$dir/$m.ko not produced"
            [[ $(ctr modinfo -F vermagic "$ko") == "$kver "* ]] || die "$m.ko vermagic doesn't match $kver"
            rel_path="$dir/$m.ko"
            mkdir -p "$stage/$(dirname "$rel_path")"
            ctr strip --strip-debug -o "$stage/$rel_path" "$ko"
            ctr "$kb/scripts/sign-file" "$hash" "$MOK_DIR/MOK.key" "$MOK_DIR/MOK.der" "$stage/$rel_path"
            ctr xz -f --check=crc32 --lzma2=dict=1MiB "$stage/$rel_path"
            files+=( "$rel_path.xz" )
        done
    done
    echo "  signed by: $(ctr modinfo -F signer "$stage/drivers/gpu/drm/xe/xe.ko.xz")"

    say "kernel $kver: packaging $KMOD_NAME"
    local top="$CACHE/rpmbuild/$KMOD_NAME" rel f; rel=$(date +%Y%m%d%H%M)
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$top/SPECS"
    tar -C "$stage" -cf "$top/SOURCES/modules.tar" "${files[@]}"
    {
        cat <<EOF
Name:           $KMOD_NAME
Version:        ${kver//-/_}
Release:        $rel
Summary:        xe, iwlwifi and btintel with the OneXPlayer 3 fixes
License:        GPL-2.0-only
Source0:        modules.tar
Requires:       kernel-core = ${kver%.*}
Obsoletes:      kmod-oxp3-xe < 999
%global debug_package %{nil}
%global __os_install_post %{nil}
%description
xe (VBT VS/PE-O backport, VRR/DSB timing, resume ENOMEM fix, 7.2.13 stable fixes, eDP HDR
switches), iwlwifi/iwlmld and btintel/btintel_pcie (7.2.13 stable and 7.3/7.4 fixes) for
kernel $kver. Signed with a local MOK. Installed in updates/, so they take priority over the
stock modules.
%prep
%setup -c -T
tar -xf %{SOURCE0}
%install
mkdir -p %{buildroot}/usr/lib/modules/$kver/updates
cp -a drivers %{buildroot}/usr/lib/modules/$kver/updates/
%files
EOF
        for f in "${files[@]}"; do echo "/usr/lib/modules/$kver/updates/$f"; done
        cat <<EOF
%post
/usr/sbin/depmod -a $kver || :
%postun
/usr/sbin/depmod -a $kver || :
EOF
    } > "$top/SPECS/$KMOD_NAME.spec"
    ctr rpmbuild --define "_topdir $top" -bb "$top/SPECS/$KMOD_NAME.spec" > "$top/build.log" 2>&1 ||
        { tail -20 "$top/build.log"; die "kmod packaging failed"; }
    rm -f "$OUT"/kmod-oxp3-*.rpm
    find "$top/RPMS" -name "$KMOD_NAME-*.rpm" -exec cp {} "$OUT/" \;
    ls "$OUT/$KMOD_NAME"-*.rpm
}

# ---------------------------------------------------------------------------------------------
# firmware: pinned files from upstream, packaged under /usr/lib/firmware/updates (searched first
# by the kernel and by dracut, so the initramfs gets them too)
# ---------------------------------------------------------------------------------------------
LF=https://gitlab.com/kernel-firmware/linux-firmware/-/raw
# <installed path under /usr/lib/firmware/updates>|<url>|<sha256>|<what>
FIRMWARE=(
    # Display DMC v2.20: v2.36 (Anatase) ends a late VRR frame only at the 30 Hz maximum, which
    # caps VRR at about 74 fps (onexplayer3-steamos-setup issue #1). --stock-dmc keeps v2.36.
    "i915/xe3lpd_dmc.bin|$LF/d2e1085e36fb/i915/xe3lpd_dmc.bin|fc585fc5758fd68e1d4ba8397bfb5311ae9fa1f287b6a23b96369e5bad06584f|dmc"
    # Bluetooth BE201 24.80.0.2 (Anatase: 24.70)
    "intel/ibt-00a0-0291-pci.sfi|$LF/8a2941fae0d02364d8ae6542ba890fd496681b55/intel/ibt-00a0-0291-pci.sfi|6ea03de60bdd6231387f561fbae53b1aaedfd906437b3c2216b45df1d4f01e45|bt"
    "intel/ibt-00a0-0291-iml.sfi|$LF/8a2941fae0d02364d8ae6542ba890fd496681b55/intel/ibt-00a0-0291-iml.sfi|69beb77387a79a75ee741ff6f88f782a0fc44c8b2efcc7bcb41c413122928026|bt"
    # CPU microcode 06-cc-02 0x11c (Intel microcode-20260811; Anatase: 0x11b)
    "intel-ucode/06-cc-02|https://raw.githubusercontent.com/intel/Intel-Linux-Processor-Microcode-Data-Files/microcode-20260811/intel-ucode/06-cc-02|4e43bb4d23c3638f8c16967d61e8f6456ae0da2ddd1da82ab579a50715147bb1|ucode"
)
FW_NAME=oxp3-firmware

# revision of the first microcode header in a file
ucode_rev() { od -An -t x4 -j 4 -N 4 "$1" 2>/dev/null | tr -d ' '; }

# installed (Anatase) copy of a firmware file, compressed or not
sys_fw() { local f; for f in /usr/lib/firmware/$1{,.xz,.zst}; do [[ -e $f ]] && { echo "$f"; return; }; done; }
sys_fw_sha() {
    local f; f=$(sys_fw "$1"); [[ -n $f ]] || return 0
    case $f in *.xz) xz -dc "$f" ;; *.zst) zstd -dcq "$f" ;; *) cat "$f" ;; esac | sha256sum | cut -d' ' -f1
}

build_firmware() {
    local stage="$CACHE/firmware/stage" entry path url sha kind f files=()
    rm -rf "$stage"; mkdir -p "$stage" "$CACHE/firmware/dl"
    say "firmware: pinned files"
    for entry in "${FIRMWARE[@]}"; do
        IFS='|' read -r path url sha kind <<<"$entry"
        (( SKIP_DMC )) && [[ $kind == dmc ]] && { echo "  $path: --stock-dmc, keeping Anatase's"; continue; }
        f="$CACHE/firmware/dl/$sha"
        if [[ $(sha256sum "$f" 2>/dev/null | cut -d' ' -f1) != "$sha" ]]; then
            curl -sfL --retry 3 -o "$f.part" "$url" || die "download failed: $url"
            [[ $(sha256sum "$f.part" | cut -d' ' -f1) == "$sha" ]] || die "checksum mismatch: $url"
            mv "$f.part" "$f"
        fi
        # skip what Anatase already ships in the same or a newer version
        if [[ $(sys_fw_sha "$path") == "$sha" ]]; then
            echo "  $path: Anatase ships the same file"; continue
        fi
        if [[ $kind == ucode ]]; then
            local have want
            have=$(ucode_rev "$(sys_fw "$path")"); want=$(ucode_rev "$f")
            if [[ -n $have ]] && (( 16#$have >= 16#$want )); then
                echo "  $path: Anatase ships revision 0x$have (>= 0x$want)"; continue
            fi
        fi
        install -D -m 0644 "$f" "$stage/$path"
        files+=( "$path" ); echo "  $path"
    done
    rm -f "$OUT/$FW_NAME"-*.rpm
    if (( ${#files[@]} == 0 )); then echo "  nothing newer than Anatase's firmware"; return 0; fi

    local top="$CACHE/rpmbuild/$FW_NAME" rel; rel=$(date +%Y%m%d%H%M)
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$top/SPECS"
    tar -C "$stage" -cf "$top/SOURCES/firmware.tar" "${files[@]}"
    {
        cat <<EOF
Name:           $FW_NAME
Version:        1
Release:        $rel
Summary:        Pinned firmware for the OneXPlayer 3 (display DMC, Bluetooth, CPU microcode)
License:        Redistributable, no modification permitted
BuildArch:      noarch
Source0:        firmware.tar
%global debug_package %{nil}
%description
Firmware files that take priority over linux-firmware / microcode_ctl through
/usr/lib/firmware/updates: display DMC v2.20 (VRR cap with v2.36), Bluetooth 24.80,
CPU microcode 06-cc-02 0x11c.
%prep
%setup -c -T
tar -xf %{SOURCE0}
%install
mkdir -p %{buildroot}/usr/lib/firmware/updates
cp -a * %{buildroot}/usr/lib/firmware/updates/
%files
EOF
        for f in "${files[@]}"; do echo "/usr/lib/firmware/updates/$f"; done
    } > "$top/SPECS/$FW_NAME.spec"
    ctr rpmbuild --define "_topdir $top" -bb "$top/SPECS/$FW_NAME.spec" > "$top/build.log" 2>&1 ||
        { tail -20 "$top/build.log"; die "firmware packaging failed"; }
    find "$top/RPMS" -name "$FW_NAME-*.rpm" -exec cp {} "$OUT/" \;
    ls "$OUT/$FW_NAME"-*.rpm
}

do_build() {
    command -v podman >/dev/null || die "podman is required"
    ensure_container
    if (( !SKIP_GAMESCOPE || !SKIP_MANGOHUD || !SKIP_MESA )); then fetch_anatase; fi
    (( SKIP_GAMESCOPE )) || build_card gamescope '^Patch:[[:space:]]+overrides\.patch' "$here"/patches/gamescope/*.patch
    (( SKIP_MANGOHUD )) || build_card mangohud '^Patch1000:[[:space:]]+overrides\.patch' "$here"/patches/mangohud/*.patch
    (( SKIP_MESA )) || build_mesa
    (( SKIP_XESS )) || build_xess
    (( SKIP_KERNEL )) || build_kmods
    (( SKIP_FIRMWARE )) || build_firmware
    say "build done: $OUT"
    ls "$OUT"
}

# ---------------------------------------------------------------------------------------------
# install / uninstall (root)
# ---------------------------------------------------------------------------------------------
mok_cn() {
    openssl x509 -in "$MOK_DIR/MOK.der" -inform der -noout -subject -nameopt multiline |
        sed -n 's/^ *commonName *= *//p'
}

# Anatase's kernel only loads MOK keys into the machine keyring (the one that verifies
# modules) if they are CA certs with keyCertSign and without digitalSignature
# (CONFIG_INTEGRITY_CA_MACHINE_KEYRING_MAX); anything else lands in the platform keyring,
# which can verify kernel images but not modules.
mok_usage_ok() {
    local ext
    ext=$(openssl x509 -in "$MOK_DIR/MOK.der" -inform der -noout -ext basicConstraints,keyUsage 2>/dev/null)
    grep -q 'CA:TRUE' <<<"$ext" && grep -q 'Certificate Sign' <<<"$ext" && ! grep -q 'Digital Signature' <<<"$ext"
}

# Is the MOK in the kernel's .machine keyring right now? keyctl can't find that keyring by
# name from a normal process, so look its id up in /proc/keys and list it by id.
mok_trusted() {
    local cn kr; cn=$(mok_cn)
    kr=$(sudo awk '$8 == "keyring" && $9 == ".machine:" { print $1; exit }' /proc/keys)
    [[ -n $cn && -n $kr ]] && sudo keyctl list "0x$kr" 2>/dev/null | grep -qF "$cn"
}

# Create a module signing key the machine keyring accepts and queue it for enrolment.
do_mok_setup() {
    if [[ -e $MOK_DIR/MOK.key ]] && mok_usage_ok; then
        echo "$MOK_DIR already has a suitable key."
    else
        if [[ -e $MOK_DIR/MOK.key ]]; then
            local old; old="$MOK_DIR.old-$(date +%Y%m%d%H%M%S)"
            echo "The key in $MOK_DIR isn't accepted for modules (needs CA + keyCertSign only); keeping it as $old"
            mv "$MOK_DIR" "$old"
        fi
        install -d -m 700 "$MOK_DIR"
        local cfg; cfg=$(mktemp)
        printf '%s\n' '[req]' 'distinguished_name = dn' 'x509_extensions = mok' 'prompt = no' \
            '[dn]' "CN = OXP3 module signing CA ($(id -un))" \
            '[mok]' 'basicConstraints = critical, CA:TRUE' 'keyUsage = critical, keyCertSign' \
            'subjectKeyIdentifier = hash' > "$cfg"
        (umask 077; openssl req -new -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 -config "$cfg" \
            -keyout "$MOK_DIR/MOK.key" -out "$MOK_DIR/MOK.crt" 2>/dev/null)
        rm -f "$cfg"
        openssl x509 -in "$MOK_DIR/MOK.crt" -outform DER -out "$MOK_DIR/MOK.der"
        echo "Created $MOK_DIR/MOK.key (keep it private) and MOK.der."
    fi
    if mokutil --test-key "$MOK_DIR/MOK.der" 2>&1 | grep -q 'already enrolled'; then
        echo "MOK already enrolled."
    else
        echo "Enrolling: choose a one-time password, then reboot and in MokManager pick"
        echo "Enroll MOK -> Continue -> Yes -> enter the password."
        sudo mokutil --import "$MOK_DIR/MOK.der"
    fi
}

# MangoHud's CPU/GPU power: a root service publishes 1 s / 0.1 W averages in /run/rapl-power;
# the raw RAPL counters stay root-only (CVE-2020-8694).
install_rapl_power() {
    sudo install -D -m 0755 "$here/files/rapl-power" /usr/local/libexec/rapl-power
    sudo install -m 0644 "$here/files/rapl-power.service" /etc/systemd/system/rapl-power.service
    sudo systemctl daemon-reload
    sudo systemctl enable rapl-power.service >/dev/null
    sudo systemctl restart rapl-power.service
    remove_rapl_udev_rules
}

# Earlier versions opened the counters to all users with a udev rule: remove it and make
# the counters root-only again right away (they are re-created root-only at boot anyway).
remove_rapl_udev_rules() {
    local r changed=0
    for r in /etc/udev/rules.d/70-oxp3-rapl.rules /etc/udev/rules.d/70-anatase-rapl.rules; do
        [[ -e $r ]] && { sudo rm -f "$r"; changed=1; }
    done
    if (( changed )); then
        sudo udevadm control --reload
        sudo chmod 0400 /sys/class/powercap/intel-rapl*/energy_uj 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------------------------
# OneXPlayer 3 extras: services and settings in /etc and /usr/local (they survive updates)
# ---------------------------------------------------------------------------------------------
EXTRA_UNITS=(oxp3-tdp-sync.service oxp3-xpad-sleep.service oxp3-rapl-pp1.service)
EXTRA_STATE=/etc/oxp3-fixes

install_extras() {
    local s u me; me=$(id -un)
    for s in oxp3-tdp-sync oxp3-xpad-sleep oxp3-audio-route; do
        sudo install -D -m 0755 "$here/files/$s" "/usr/local/libexec/$s"
    done
    for u in "${EXTRA_UNITS[@]}" oxp3-audio-resume@.service; do
        sudo install -m 0644 "$here/files/$u" "/etc/systemd/system/$u"
    done
    sudo install -D -m 0644 "$here/files/oxp3-audio-route.service" /etc/systemd/user/oxp3-audio-route.service
    sudo install -D -m 0644 "$here/files/52-oxp3-speakers.conf" \
        /etc/wireplumber/wireplumber.conf.d/52-oxp3-speakers.conf
    # the BMI260 IMU also matches bmi160's ACPI id; bmi160 loads but never binds
    echo 'blacklist bmi160_i2c' | sudo tee /etc/modprobe.d/oxp3-bmi160.conf >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable oxp3-xpad-sleep.service oxp3-rapl-pp1.service "oxp3-audio-resume@$me.service" >/dev/null 2>&1
    sudo systemctl enable oxp3-tdp-sync.service >/dev/null 2>&1
    sudo systemctl restart oxp3-tdp-sync.service
    sudo systemctl --global enable oxp3-audio-route.service >/dev/null 2>&1
    systemctl --user daemon-reload
    # thermald exits at boot here: the BIOS has no PSVT tables for it ("THD engine init failed")
    if systemctl is-enabled -q thermald.service 2>/dev/null; then
        sudo systemctl disable --now thermald.service >/dev/null 2>&1
        sudo install -d "$EXTRA_STATE"; sudo touch "$EXTRA_STATE/thermald-disabled"
    fi
    echo "  TDP sync, speaker route check (+ resume), speaker name, xpad sleep, GPU power zone check"
    echo "  (the speaker name shows after WirePlumber restarts: next login, or systemctl --user restart wireplumber)"
}

uninstall_extras() {
    local u me; me=$(id -un)
    sudo systemctl disable --now "${EXTRA_UNITS[@]}" "oxp3-audio-resume@$me.service" >/dev/null 2>&1 || true
    sudo systemctl --global disable oxp3-audio-route.service >/dev/null 2>&1 || true
    for u in "${EXTRA_UNITS[@]}" oxp3-audio-resume@.service; do sudo rm -f "/etc/systemd/system/$u"; done
    sudo rm -f /etc/systemd/user/oxp3-audio-route.service \
        /etc/wireplumber/wireplumber.conf.d/52-oxp3-speakers.conf /etc/modprobe.d/oxp3-bmi160.conf \
        /usr/local/libexec/oxp3-tdp-sync /usr/local/libexec/oxp3-xpad-sleep /usr/local/libexec/oxp3-audio-route
    if [[ -e $EXTRA_STATE/thermald-disabled ]]; then
        sudo systemctl enable thermald.service >/dev/null 2>&1 || true
        sudo rm -f "$EXTRA_STATE/thermald-disabled"
    fi
    sudo rmdir "$EXTRA_STATE" 2>/dev/null || true
    sudo systemctl daemon-reload
}

# Volume rocker (needs the /usr overlay): hhd patch and atkbd force-release go in together, since
# force-release without the patch makes hhd lose every press. Both live in /usr, so an Anatase
# update drops both and the rocker keeps working (just unfixed) until the next install.
HHD_PATCHES=("$here"/patches/hhd/*.patch)
VOLUME_HWDB=/usr/lib/udev/hwdb.d/84-oxp3-volume-keys.hwdb
hhd_site() { python3 -c 'import hhd, os; print(os.path.dirname(os.path.dirname(hhd.__file__)))' 2>/dev/null; }

install_volume_fix() {
    local site p changed=0; site=$(hhd_site)
    [[ -n $site ]] || { echo "  hhd not found, volume rocker fix skipped"; return 0; }
    # each patch: already there, or applied now; one that does neither leaves the rocker stock
    for p in "${HHD_PATCHES[@]}"; do
        if git -C "$site" apply --check -R -p1 "$p" >/dev/null 2>&1; then
            continue
        elif git -C "$site" apply --check -p1 "$p" >/dev/null 2>&1; then
            sudo git -C "$site" apply -p1 "$p"; changed=1
        else
            echo "  WARNING: ${p##*/} doesn't apply to this hhd - volume rocker left stock"
            hhd_volume_unpatch "$site"
            [[ -e $VOLUME_HWDB ]] && { sudo rm -f "$VOLUME_HWDB"; sudo systemd-hwdb update; }
            sudo systemctl restart hhd.service
            return 0
        fi
    done
    sudo find "$site/hhd/device/oxp" -name '__pycache__' -prune -exec rm -rf {} +
    echo "  hhd: volume rocker presses become taps$( ((changed)) || echo ' (already patched)')"
    if ! cmp -s "$here/files/84-oxp3-volume-keys.hwdb" "$VOLUME_HWDB"; then
        sudo install -m 0644 "$here/files/84-oxp3-volume-keys.hwdb" "$VOLUME_HWDB"
        sudo systemd-hwdb update
        sudo udevadm trigger --subsystem-match=input --action=change
    fi
    sudo systemctl restart hhd.service
}

# take our hhd patches out again, newest first (only those that are fully applied)
hhd_volume_unpatch() { # <site>
    local i p
    for (( i=${#HHD_PATCHES[@]} - 1; i >= 0; i-- )); do
        p=${HHD_PATCHES[$i]}
        git -C "$1" apply --check -R -p1 "$p" >/dev/null 2>&1 && sudo git -C "$1" apply -R -p1 "$p"
    done
    sudo find "$1/hhd/device/oxp" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
}

# Mesa for the Steam Flatpak. Anatase builds the org.anatase.Platform runtime from the composefs /usr
# (anatase-flatpak-extensions skips the /usr overlay on purpose) plus its overrides directory, so
# games under the Flatpak Steam would keep Anatase's Mesa. Our Mesa files go into those overrides,
# inside the /usr overlay, so an Anatase update drops them together with the overlay Mesa. The
# overrides are copied to /run at boot: other vendors' Vulkan drivers and OpenCL stay stock to keep
# that small (each Vulkan driver is self-contained).
FLATPAK_RT=/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable
FLATPAK_MESA_LIST=$FLATPAK_RT/oxp3-mesa.list
FLATPAK_RT_FILES=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files
FLATPAK_MESA_SKIP='libvulkan_(radeon|panfrost|nouveau|freedreno|asahi|powervr_mesa|broadcom|dzn|virtio|intel_hasvk|lvp)\.so|/icd\.d/(radeon|panfrost|nouveau|freedreno|asahi|powervr_mesa|broadcom|dzn|virtio|intel_hasvk|lvp)_icd|RusticlOpenCL|/OpenCL/|^/usr/share/(doc|licenses|man)/'

# Mesa version of the host (from its libgallium-<version>.so) and whether the Steam Flatpak runtime
# runs the same Intel Vulkan driver
host_mesa() { local g; g=$(ls /usr/lib64/libgallium-*.so 2>/dev/null | head -1); g=${g##*/libgallium-}; echo "${g%.so}"; }
flatpak_mesa() {
    if cmp -s /usr/lib64/libvulkan_intel.so "$FLATPAK_RT_FILES/lib64/libvulkan_intel.so"; then
        echo "same as the host"
    else
        echo "Anatase's (not ours)"
    fi
}

refresh_flatpak_runtime() {
    if [[ -n $(flatpak ps --columns=application 2>/dev/null) ]]; then
        echo "  Flatpak apps are running (Steam?): the Steam runtime picks this up after a reboot,"
        echo "  or close them and run: sudo systemctl restart anatase-flatpak-extensions.service"
        return 0
    fi
    sudo systemctl restart anatase-flatpak-extensions.service
    echo "  Steam Flatpak runtime Mesa: $(flatpak_mesa)"
}

remove_flatpak_mesa() {
    [[ -r $FLATPAK_MESA_LIST ]] || return 0
    (cd "$FLATPAK_RT/overrides" && grep -v '/$' "$FLATPAK_MESA_LIST" | sudo xargs -r -d '\n' rm -f --)
    sudo rm -f "$FLATPAK_MESA_LIST"
}

install_flatpak_mesa() {
    [[ -d $FLATPAK_RT/overrides ]] || { echo "  no Anatase Flatpak runtime overrides - Steam keeps Anatase's Mesa"; return 0; }
    local pkgs list
    pkgs=$(rpm -qa 'mesa-*' | grep -v -- '-devel-')
    # shellcheck disable=SC2086
    list=$(rpm -ql $pkgs | grep '^/usr/' | grep -vE "$FLATPAK_MESA_SKIP" | sort -u)
    remove_flatpak_mesa
    # /usr/<path> -> overrides/<path> (the runtime is mounted at /usr in the sandbox)
    sed 's,^/,,' <<<"$list" | sudo tar -C / --no-recursion -cf - -T - |
        sudo tar -C "$FLATPAK_RT/overrides" -xf - --transform 's,^usr/,,'
    sed 's,^/usr/,,' <<<"$list" | sudo tee "$FLATPAK_MESA_LIST" >/dev/null
    echo "  $(wc -l <<<"$list") Mesa files in the Steam Flatpak runtime overrides"
    refresh_flatpak_runtime
}

# State of the deployments: is one staged, and which layered local packages does the booted
# one have?
deploy_info() {
    rpm-ostree status --json | python3 -c 'import json,sys
ds=json.load(sys.stdin)["deployments"]
b=[x for x in ds if x.get("booted")][0]
print("staged" if any(x.get("staged") for x in ds) else "none")
print(" ".join(b.get("requested-local-packages",[])))'
}

hotfix_active() { rpm-ostree status --booted | grep -q 'Unlocked: hotfix'; }

do_install() {
    local rpms=() kmod=() kver info staged locals p un=()
    (( SKIP_GAMESCOPE )) || rpms+=( "$OUT"/gamescope-*.x86_64.rpm "$OUT"/gamescope-*.i686.rpm )
    (( SKIP_MANGOHUD )) || rpms+=( "$OUT"/mangohud-*.x86_64.rpm "$OUT"/mangohud-*.i686.rpm )
    local f; for f in "${rpms[@]}"; do [[ -f $f ]] || die "missing $f - run '$0 build' first"; done
    kver=$(anatase_kver)
    info=$(deploy_info); staged=$(sed -n 1p <<<"$info"); locals=$(sed -n 2p <<<"$info")

    say "power readings service (sudo)"
    install_rapl_power
    if (( !SKIP_EXTRAS )); then
        say "OneXPlayer 3 services (sudo)"
        install_extras
    fi

    # 1. kernel modules + firmware: xe loads from the initramfs, so both are layered with
    #    rpm-ostree and the initramfs regenerated. That makes a new deployment, which doesn't keep
    #    the writable /usr overlay, so this goes first; gamescope/MangoHud follow on the next run.
    local layer=() want=() pkg
    if (( !SKIP_KERNEL )) && [[ ! -e $OUT/.kernel-not-needed ]]; then
        kmod=( "$OUT/$KMOD_NAME"-*.rpm )
        [[ -f ${kmod[0]} ]] || die "no $KMOD_NAME package - run '$0 build' first"
        rpm -qp --qf '%{VERSION}\n' "${kmod[0]}" | grep -qF "${kver//-/_}" ||
            die "$KMOD_NAME was built for another kernel than $kver - rebuild"
        if ! mok_trusted; then
            echo "WARNING: the MOK in $MOK_DIR is not in the kernel's machine keyring."
            echo "         The signed modules would be rejected and the screen stay dark at boot."
            mok_usage_ok || echo "         The key isn't a CA with keyCertSign only: run '$0 mok-setup'."
            echo "         Skipping the kernel part (README: Secure Boot key)."
        else
            layer+=( "${kmod[0]}" )
        fi
    fi
    if (( !SKIP_FIRMWARE )); then
        local fw=( "$OUT/$FW_NAME"-*.rpm )
        [[ -f ${fw[0]} ]] && layer+=( "${fw[0]}" )
    fi
    for pkg in "${layer[@]}"; do want+=( "$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' "$pkg")" ); done
    # our layered packages that are not wanted any more (older builds, kmod-oxp3-xe, or firmware
    # Anatase now ships itself)
    for p in $locals; do
        [[ $p == kmod-oxp3-* || $p == "$FW_NAME"-* ]] || continue
        (( SKIP_KERNEL )) && [[ $p == kmod-oxp3-* ]] && continue
        (( SKIP_FIRMWARE )) && [[ $p == "$FW_NAME"-* ]] && continue
        [[ " ${want[*]} " == *" $p "* ]] || un+=( "$p" )
    done
    local missing=() i
    for i in "${!layer[@]}"; do grep -qwF "${want[$i]}" <<<"$locals" || missing+=( "${layer[$i]}" ); done
    if (( ${#missing[@]} || ${#un[@]} )); then
        say "staging kernel modules / firmware for $kver (sudo)"
        printf '  layer:  %s\n' "${missing[@]##*/}"
        printf '  remove: %s\n' "${un[@]}"
        if (( ${#missing[@]} )); then
            sudo rpm-ostree install "${missing[@]}" "${un[@]/#/--uninstall=}"
        else
            sudo rpm-ostree uninstall "${un[@]}"
        fi
        rpm-ostree initramfs 2>/dev/null | grep -q 'Initramfs regeneration: enabled' ||
            sudo rpm-ostree initramfs --enable
        say "reboot now, then run '$0 install' again for gamescope and MangoHud"
        echo "(If the screen stays dark after the reboot, pick the previous entry in the boot menu.)"
        return 0
    elif (( ${#layer[@]} )); then
        echo "kernel modules / firmware for $kver already in this deployment"
    fi

    # 2. gamescope / MangoHud / hhd volume fix into the writable /usr overlay of the booted deployment
    if (( ${#rpms[@]} || !SKIP_EXTRAS || !SKIP_XESS || !SKIP_MESA )); then
        if [[ $staged == staged ]]; then
            echo "A deployment is staged (an update or a pending change): reboot into it first,"
            echo "then run '$0 install' again - changes to the current /usr would be lost."
            return 0
        fi
        hotfix_active || sudo rpm-ostree usroverlay --hotfix
        if (( ${#rpms[@]} )); then
            say "gamescope / MangoHud / Mesa into the writable /usr overlay (sudo)"
            sudo rpm -Uvh --replacepkgs --oldpackage --nosignature "${rpms[@]}"
            rpm -q gamescope mangohud mesa-vulkan-drivers | sed 's/^/  /'

        fi
        if (( !SKIP_MESA )); then
            say "Mesa (prebuilt driver) into the writable /usr overlay (sudo)"
            install_mesa_fast
            say "Mesa for the Steam Flatpak (sudo)"
            install_flatpak_mesa_fast
        fi
        if (( !SKIP_EXTRAS )); then
            say "volume rocker (sudo)"
            install_volume_fix
        fi
        if (( !SKIP_XESS )); then
            say "XeSS on XMX (xess-xmx-linux)"
            install_xess
        fi
        say "done - leave and re-enter Gamemode (no reboot needed)"
        echo "This lasts until the next Anatase update; run '$0 all' after updating."
    fi
}

do_uninstall() {
    say "back to stock Anatase (sudo)"
    local p un=()
    for p in $(sed -n 2p <<<"$(deploy_info)"); do [[ $p == kmod-oxp3-* || $p == "$FW_NAME"-* ]] && un+=( "$p" ); done
    (( ${#un[@]} )) && sudo rpm-ostree uninstall "${un[@]}"
    if rpm-ostree initramfs 2>/dev/null | grep -q 'Initramfs regeneration: enabled'; then
        sudo rpm-ostree initramfs --disable
    fi
    sudo systemctl disable --now rapl-power.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/rapl-power.service /usr/local/libexec/rapl-power
    sudo systemctl daemon-reload
    remove_rapl_udev_rules
    uninstall_extras
    uninstall_xess
    if [[ -r $FLATPAK_MESA_LIST ]]; then remove_flatpak_mesa; refresh_flatpak_runtime; fi
    remove_mesa_fast
    if [[ -e $VOLUME_HWDB ]]; then
        sudo rm -f "$VOLUME_HWDB"; sudo systemd-hwdb update
    fi
    local site; site=$(hhd_site)
    if [[ -n $site ]]; then hhd_volume_unpatch "$site"; sudo systemctl restart hhd.service; fi
    echo "Reboot to finish. The patched gamescope/MangoHud live in the writable /usr overlay of"
    echo "the current deployment; any new deployment (the reboot above, or the next Anatase"
    echo "update) starts from the stock packages again."
}

do_status() {
    echo "kernel (deployment): $(anatase_kver)   booted: $(uname -r)"
    rpm -q gamescope mangohud mesa-vulkan-drivers kmod-oxp3-xe "$KMOD_NAME" "$FW_NAME" 2>&1 | grep -v 'is not installed' | sed 's/^/  /' || true
    rpm-ostree status --booted | grep -E 'Version|LocalPackages|Initramfs|Unlocked' | sed 's/^/  /' || true
    echo "  rapl-power service: $(systemctl is-active rapl-power.service 2>/dev/null)"
    local f; for f in /run/rapl-power/*; do [[ -e $f ]] && echo "    $(basename "$f"): $(cat "$f") W"; done
    ls /etc/udev/rules.d/70-*rapl.rules >/dev/null 2>&1 && echo "  WARNING: old RAPL udev rule still present (run install)"
    stat -c '  RAPL counters: %A %n' /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null
    echo "  Mesa: host $(host_mesa)$(grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so && echo ' with the XeSS XMX patches'), Steam Flatpak: $(flatpak_mesa)"
    local rtx=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files/lib64
    if [[ -e $rtx/libexpat.so.1 ]]; then
        if nm -D --with-symbol-versions "$rtx/libexpat.so.1" 2>/dev/null | grep -q LIBEXPAT_2.9.0; then
            echo "  Steam runtime: expat has LIBEXPAT_2.9.0 (Intel ICD can load)"
        else
            echo "  Steam runtime: expat LACKS LIBEXPAT_2.9.0 -> Intel ICD fails, games fall back to"
            echo "                 lavapipe (single-digit FPS). Run 'install' to fix the override."
        fi
    fi
    if cmp -s "$MESA_BUILD_DIR/lib64/libvulkan_intel.so" /usr/lib64/libvulkan_intel.so 2>/dev/null; then
        echo "  Mesa: PREBUILT driver installed ($MESA_BUILD_DIR)"
    else
        echo "  Mesa: stock (prebuilt driver at $MESA_BUILD_DIR is not installed)"
    fi
    local m; for m in xe iwlwifi iwlmld btintel btintel_pcie; do
        printf '  %-13s %s\n' "$m:" "$(modinfo -F filename "$m" 2>/dev/null)"
    done
    journalctl -k -b 0 --no-pager 2>/dev/null | sed -n \
        -e 's/.*\(Finished loading DMC firmware .*\)/  \1/p' \
        -e 's/.*microcode: \(Current revision: .*\)/  microcode \1/p' \
        -e 's/.*Bluetooth: hci0: \(Firmware timestamp .*\)/  Bluetooth \1/p' | sort -u
    local u st; for u in "${EXTRA_UNITS[@]}" "oxp3-audio-resume@$(id -un).service" user:oxp3-audio-route.service; do
        if [[ $u == user:* ]]; then st=$(systemctl --user is-enabled "${u#user:}" 2>/dev/null || true)
        else st=$(systemctl is-enabled "$u" 2>/dev/null || true); fi
        printf '  %-34s %s\n' "${u#user:}:" "${st:-missing}"
    done
    echo "  package PL1: MSR $(( $(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 )) W," \
         "MMIO $(( $(cat /sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 )) W"
    local site; site=$(hhd_site)
    local hp n=0; for hp in "${HHD_PATCHES[@]}"; do
        [[ -n $site ]] && git -C "$site" apply --check -R -p1 "$hp" >/dev/null 2>&1 && n=$((n + 1))
    done
    echo "  volume rocker: $n/${#HHD_PATCHES[@]} hhd patches applied$( [[ -e $VOLUME_HWDB ]] && echo ', force-release on')"
    status_xess
    [[ -d $OUT ]] && { echo "built packages in $OUT:"; ls "$OUT" | sed 's/^/  /'; }
}

case $cmd in
    build) do_build ;;
    install) do_install ;;
    all) do_build; do_install ;;
    status) do_status ;;
    uninstall) do_uninstall ;;
    mok-setup) do_mok_setup ;;
    *) sed -n '2,/^set -euo pipefail/{/^#/p}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
