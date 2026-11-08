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
  libzstd-devel expat-static glslang-devel spirv-llvm-translator22-devel spirv-tools-devel \
  libXxf86vm-devel > /tmp/dnf.log 2>&1
# mesa asks pkg-config for lowercase 'llvmspirvlib'; Fedora ships 'LLVMSPIRVLib.pc'
ln -sf /usr/lib64/pkgconfig/LLVMSPIRVLib.pc /usr/lib64/pkgconfig/llvmspirvlib.pc 2>/dev/null || true
rm -rf /b /out/usr; mkdir -p /out/usr/local
meson setup /b /src --prefix=/usr/local --buildtype=release -Dprefer_static=true \
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
    local need_expat=0
    readelf -d "$src/libvulkan_intel.so" 2>/dev/null | grep -q libexpat && need_expat=1
    for f in libvulkan_intel.so libexpat.so.1; do
        [[ -e $src/$f ]] || continue
        [[ $f == libexpat.so.1 && $need_expat == 0 ]] && continue
        target=/usr/lib64/$f
        if [[ ! -e $MESA_BACKUP/stock/$f ]]; then
            sudo mkdir -p "$MESA_BACKUP/stock"
            [[ -e $target ]] && sudo cp -a "$target" "$MESA_BACKUP/stock/$f"
        fi
        sudo install -m 0755 "$src/$f" "$target"
        echo "/usr/lib64/$f" | sudo tee -a "$manifest" >/dev/null
    done
    if [[ $need_expat == 0 ]]; then
        # self-contained driver: if an older install replaced the host expat, put the stock one back
        if [[ -e $MESA_BACKUP/stock/libexpat.so.1 ]] && [[ -e $MESA_BACKUP/mesa.manifest.old-expat ]]; then
            sudo cp -a "$MESA_BACKUP/stock/libexpat.so.1" /usr/lib64/libexpat.so.1
            echo "  driver needs no expat: restored the stock /usr/lib64/libexpat.so.1"
        fi
    elif [[ $need_expat == 1 ]]; then
        sudo touch "$MESA_BACKUP/mesa.manifest.old-expat"
    fi
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
    local src="$MESA_BUILD_DIR/lib64" list
    [[ -d $FLATPAK_RT/overrides ]] || { echo "  no Anatase Flatpak runtime overrides - Steam keeps Anatase's Mesa"; return 0; }
    [[ -e $src/libvulkan_intel.so ]] || return 0
    sudo mkdir -p "$FLATPAK_RT/overrides/lib64" "$MESA_BACKUP/flatpak-stock"
    if [[ ! -e $MESA_BACKUP/flatpak-stock/libexpat.so.1 && -e $FLATPAK_RT/files/lib64/libexpat.so.1 ]]; then
        sudo cp -a "$FLATPAK_RT/files/lib64/libexpat.so.1" "$MESA_BACKUP/flatpak-stock/libexpat.so.1"
    fi
    sudo install -m 0755 "$src/libvulkan_intel.so" "$FLATPAK_RT/overrides/lib64/libvulkan_intel.so"
    list=$'lib64/libvulkan_intel.so\n'
    if readelf -d "$src/libvulkan_intel.so" 2>/dev/null | grep -q libexpat; then
        [[ -e $src/libexpat.so.1 ]] || die "driver needs expat but $src/libexpat.so.1 is missing"
        sudo install -m 0755 "$src/libexpat.so.1" "$FLATPAK_RT/overrides/lib64/libexpat.so.1"
        list+=$'lib64/libexpat.so.1\n'
        echo "  Intel Vulkan driver + bundled expat in the Steam Flatpak runtime overrides"
    else
        sudo rm -f "$FLATPAK_RT/overrides/lib64/libexpat.so.1"
        echo "  driver is self-contained (static expat): no runtime expat override needed"
    fi
    printf '%s' "$list" | sudo tee "$FLATPAK_MESA_LIST" >/dev/null
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

verify() {
    local rtf=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files/lib64
    echo "host:"
    if LD_LIBRARY_PATH=/usr/lib64 python3 -c "import ctypes;l=ctypes.CDLL('/usr/lib64/libvulkan_intel.so');assert l.vk_icdGetInstanceProcAddr" 2>/dev/null; then
        echo "  /usr/lib64/libvulkan_intel.so loads (Intel ICD usable)"
    else
        echo "  /usr/lib64/libvulkan_intel.so FAILS to load -> Vulkan falls back to lavapipe"
    fi
    if [[ -d $HOME/.local/share/Steam || -d $HOME/.steam ]]; then
        echo "steam (native, Steam Linux Runtime):"
        echo "  pressure-vessel maps the host /usr/lib64/libvulkan_intel.so into the container but"
        echo "  resolves libexpat from the container's own older copy - so the host line above is"
        echo "  what decides whether a Steam game gets the Intel driver or lavapipe."
    fi
    echo "steam flatpak runtime:"
    local ov=$FLATPAK_RT/overrides/lib64/libvulkan_intel.so
    if [[ ! -e $rtf/libvulkan_intel.so ]]; then
        echo "  no runtime driver present"
    elif [[ -e $ov ]] && ! cmp -s "$ov" "$rtf/libvulkan_intel.so"; then
        echo "  runtime image is STALE (overrides differ from /run): close Flatpak apps and run"
        echo "    sudo systemctl restart anatase-flatpak-extensions.service   (or reboot)"
    elif LD_LIBRARY_PATH=$rtf python3 -c "import ctypes;l=ctypes.CDLL('$rtf/libvulkan_intel.so');assert l.vk_icdGetInstanceProcAddr" 2>/dev/null; then
        echo "  runtime driver loads (Flatpak Steam gets the Intel driver)"
    else
        echo "  runtime driver FAILS to load -> Flatpak Steam falls back to lavapipe (software)"
        echo "  fix: sudo ./oxp3-fixes-fast.sh install"
    fi
}

case $cmd in
    build) do_build ;;
    install) do_install ;;
    all) do_build; do_install ;;
    status) do_status ;;
    verify) verify ;;
    uninstall) do_uninstall ;;
    mok-setup) do_mok_setup ;;
    *) sed -n '2,/^set -euo pipefail/{/^#/p}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
