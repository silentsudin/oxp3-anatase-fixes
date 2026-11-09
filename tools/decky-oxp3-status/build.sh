#!/bin/sh
# Build, verify and package the OXP3 Status Decky plugin.
#
#   ./build.sh            install deps (npm ci), typecheck, build, smoke test,
#                         run the backend --selftest, write build/*.zip
#   ./build.sh --offline  same, but `npm ci --offline` (warm npm cache only)
#   ./build.sh --no-install  reuse node_modules as-is
#
# Exit codes: 0 ok, 1 build/verify failure, 2 missing toolchain.
# Nothing here needs root, network (with --offline and a warm cache) or a
# Steam Deck: the smoke test and backend selftest use synthetic fixtures.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"

offline=0
install=1
for arg in "$@"; do
    case "$arg" in
        --offline) offline=1 ;;
        --no-install) install=0 ;;
        -h|--help)
            sed -n '2,11p' "$here/${0##*/}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "build: unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "build: missing tool: $1" >&2
        exit 2
    }
}

need node
need npm
need python3

node_version=$(node --version)
npm_version=$(npm --version)
echo "build: node $node_version, npm $npm_version, python $(python3 --version 2>&1)"

if [ "$install" -eq 1 ]; then
    if [ "$offline" -eq 1 ]; then
        echo "build: npm ci --offline (registry not contacted)"
        npm ci --offline --no-audit --no-fund
    elif [ -f package-lock.json ]; then
        echo "build: npm ci"
        npm ci --no-audit --no-fund
    else
        echo "build: npm install (no package-lock.json)"
        npm install --no-audit --no-fund
    fi
else
    echo "build: reusing existing node_modules"
fi

echo "build: typecheck"
npm run --silent typecheck

echo "build: rollup"
npm run --silent build

# Decky's loader evaluates dist/index.js as a classic script; rollup emits ESM.
echo "build: wrap the bundle as an IIFE expression"
node wrap-iife.mjs dist/index.js

echo "build: headless load/render smoke test"
npm run --silent smoke

echo "build: two-line overview render test"
npm run --silent test

echo "build: backend selftest (synthetic /sys, /proc, HOME)"
python3 main.py --selftest

echo "build: installing package"
python3 package.py

artifact=$(ls -1t build/oxp3-status-*.zip | head -n 1)
if [ -z "$artifact" ] || [ ! -f "$artifact" ]; then
    echo "build: no artifact produced" >&2
    exit 1
fi
size=$(wc -c < "$artifact")
if command -v sha256sum >/dev/null 2>&1; then
    hash=$(sha256sum "$artifact" | cut -d' ' -f1)
else
    hash=$(python3 - "$artifact" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
)
fi
echo "build: artifact $artifact ($size bytes)"
echo "build: sha256 $hash"
