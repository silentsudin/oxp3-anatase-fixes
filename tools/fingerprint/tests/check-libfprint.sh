#!/bin/sh
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Syntax-check libfprint-bf63112.c against a *real* libfprint 1.94.x source
# tree (internal driver headers + GLib/GUsb).  This is the authoritative API
# check; `make syntax` only uses our offline shim headers.
#
# Usage:  tools/fingerprint/tests/check-libfprint.sh
# Env:    LIBFPRINT_SRC  libfprint source checkout (default /tmp/libfprint-1.94.9)
#         PODMAN_IMAGE   container image for the fallback (default as run-c-tests)
#
# Get the source tree with:
#   git clone --depth 1 --branch v1.94.9 \
#     https://gitlab.freedesktop.org/libfprint/libfprint.git /tmp/libfprint-1.94.9
#
# Note: meson generates fp-enums.h/fpi-enums.h from the public headers.  A
# syntax check only needs them to exist (nothing in them is used by the
# driver), so this script creates empty placeholders - they are *not* part of
# libfprint and are never installed.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname "$here")            # tools/fingerprint
repo=$(CDPATH= cd -- "$root/../.." && pwd)
src=${LIBFPRINT_SRC:-/tmp/libfprint-1.94.9}

if [ ! -f "$src/libfprint/fpi-device.h" ]; then
  cat >&2 <<EOF
LIBFPRINT_SRC does not look like a libfprint source tree: $src
Fetch one with:
  git clone --depth 1 --branch v1.94.9 \\
    https://gitlab.freedesktop.org/libfprint/libfprint.git $src
EOF
  exit 2
fi

have_host_deps() {
  command -v make >/dev/null 2>&1 &&
  { command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1; } &&
  pkg-config --exists glib-2.0 gio-2.0 gusb 2>/dev/null
}

if have_host_deps; then
  enums=$(mktemp -d)
  trap 'rm -rf "$enums"' EXIT
  : > "$enums/fp-enums.h"
  : > "$enums/fpi-enums.h"
  echo "== host build: syntax-check against $src" >&2
  exec make -C "$root" syntax-real \
    LIBFPRINT_SRC="$src" ENUMS_INCLUDE="$enums"
fi

if command -v podman >/dev/null 2>&1; then
  img=${PODMAN_IMAGE:-}
  if [ -z "$img" ]; then
    if podman image exists localhost/oxp3-fixes-build:f45 2>/dev/null; then
      img=localhost/oxp3-fixes-build:f45
      pkg="glib2-devel libgusb-devel"
    else
      img=registry.fedoraproject.org/fedora:45
      pkg="gcc make glib2-devel libgusb-devel"
    fi
  else
    pkg="glib2-devel libgusb-devel"
  fi

  echo "== no host GLib/libfprint headers; checking in $img" >&2
  exec podman run --rm --security-opt label=disable \
    -v "$repo":/w:z -v "$src":/libfprint:ro -w /w/tools/fingerprint "$img" \
    sh -c "dnf -y -q install $pkg >/dev/null 2>&1 || exit 3; \
           mkdir -p /tmp/enums && : > /tmp/enums/fp-enums.h && \
           : > /tmp/enums/fpi-enums.h; \
           make syntax-real LIBFPRINT_SRC=/libfprint ENUMS_INCLUDE=/tmp/enums"
fi

cat >&2 <<'EOF'
No host GLib/GUsb development files and no podman found.
  install glib2-devel, libgusb-devel, gcc, make and pkg-config, or
  set PODMAN_IMAGE=<an image with them>.
EOF
exit 2
