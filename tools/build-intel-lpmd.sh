#!/bin/bash
# SPDX-License-Identifier: MIT
# build-intel-lpmd.sh - build Intel's current intel-lpmd (0.1.1-test) in a container.
#
# The Fedora package is 0.1.0 and has no per-process cpuset / game classification.
# Upstream 0.1.1-test adds it (the "Intel method": process classes confined to
# P/E/LP-E cores via systemd cpusets + sched_setaffinity + uclamp).
#
#   ./tools/build-intel-lpmd.sh            # build and stage into ~/.cache/oxp3-fixes/intel-lpmd
#   ./tools/install-intel-lpmd.sh          # then install as root
#
# Output: $OXP3_CACHE/intel-lpmd/stage/{bin,data,libexec}
set -euo pipefail
cache=${OXP3_CACHE:-$HOME/.cache/oxp3-fixes}
dir=$cache/intel-lpmd
src=$dir/src
stage=$dir/stage
repo=https://github.com/intel/intel-lpmd.git

mkdir -p "$dir"
if [ -d "$src/.git" ]; then
    git -C "$src" fetch -q --depth 1 origin && git -C "$src" reset -q --hard origin/HEAD
else
    git clone -q --depth 1 "$repo" "$src"
fi
echo "== source: $(git -C "$src" log --oneline -1)"
rm -rf "$stage"; mkdir -p "$stage/bin" "$stage/data" "$stage/libexec" "$stage/man"

podman run --rm -v "$src":/src:z -v "$stage":/out:z registry.fedoraproject.org/fedora:45 bash -lc '
set -e
dnf -y -q install gcc gcc-c++ make autoconf automake libtool autoconf-archive \
    gtk-doc glib2-devel libxml2-devel libnl3-devel systemd-devel upower-devel \
    pkgconf-pkg-config gettext-devel >/dev/null
cd /src
./autogen.sh --prefix=/usr --sysconfdir=/etc --localstatedir=/var >/tmp/conf.log 2>&1
make -j"$(nproc)" >/tmp/make.log 2>&1
cp intel_lpmd /out/bin/
cp tools/intel_lpmd_control /out/bin/
cp tools/intel_lpmd_focus_helper /out/libexec/ 2>/dev/null || true
cp data/*.xml /out/data/
cp man/*.8 man/*.5 /out/man/ 2>/dev/null || true
/out/bin/intel_lpmd --version
'
chmod +x "$stage/bin/"* "$stage/libexec/"* 2>/dev/null || true
echo "== staged: $stage"
ls -l "$stage/bin"
