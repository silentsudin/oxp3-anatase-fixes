#!/bin/sh
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Build and run the standalone C protocol tests (tests/test_proto.c).
#
# The ONEXPLAYER 3 host has no C compiler, so this falls back to an ephemeral
# container (podman).  Nothing is installed on the host and the container is
# removed afterwards.
#
# Usage:  tools/fingerprint/tests/run-c-tests.sh
# Env:    PODMAN_IMAGE   image to use (default: the local oxp3 build image,
#                        else registry.fedoraproject.org/fedora:45)
#         CC            host compiler (default: cc/gcc/clang if present)

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname "$here")            # tools/fingerprint
repo=$(CDPATH= cd -- "$root/../.." && pwd)

if command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || \
   command -v clang >/dev/null 2>&1; then
  echo "== host compiler: building the C protocol tests" >&2
  exec make -C "$root" proto-test
fi

if command -v podman >/dev/null 2>&1; then
  img=${PODMAN_IMAGE:-}
  if [ -z "$img" ]; then
    if podman image exists localhost/oxp3-fixes-build:f45 2>/dev/null; then
      img=localhost/oxp3-fixes-build:f45
      pkg=""
    else
      img=registry.fedoraproject.org/fedora:45
      pkg="gcc make"
    fi
  else
    pkg=""
  fi

  echo "== no host compiler; running the C tests in $img" >&2
  exec podman run --rm --security-opt label=disable \
    -v "$repo":/w:z -w /w/tools/fingerprint "$img" \
    sh -c "if [ -n '$pkg' ]; then dnf -y -q install $pkg >/dev/null || exit 3; fi; \
           make proto-test; \
           if [ -f /usr/include/openssl/evp.h ]; then \
             make session-syntax; \
           else echo 'session-syntax: skipped (no openssl-devel in the image)'; fi"
fi

cat >&2 <<'EOF'
No C compiler and no podman found.
  install gcc (or clang) plus make and re-run, or
  set PODMAN_IMAGE=<an image with gcc and make>.
The Python suite (make py-test) needs neither.
EOF
exit 2
