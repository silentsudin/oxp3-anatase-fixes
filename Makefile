# Front end for the repository checks described in docs/TESTING.md.
# Plain POSIX make: every target is a one-line recipe, no GNU functions.
#
#   make help            this list
#
# The suites themselves live in tools/repo-test.sh; this file only provides
# short, discoverable entry points. Nothing here needs root or installs host
# packages.

REPO_TEST = tools/repo-test.sh

.PHONY: help test test-full verify preflight decky install-dry-run units unit clean

help:
	@printf '%s\n' \
		'make test            - every offline fast check (no network, no builds; default run)' \
		'make test-full       - test + fingerprint C/Decky builds + libfprint syntax check' \
		'make verify          - post-reboot hardware acceptance harness (read-only, no root)' \
		'make preflight       - read-only install-prerequisite verdict (docs/PREFLIGHT.md)' \
		'make decky           - build, smoke-test and package the Decky plugin (offline)' \
		'make install-dry-run - print every hardware-installer command, change nothing' \
		'make units           - verify systemd units, udev rules and hwdb files' \
		'make unit            - offline Python unit suite (tests/unit, no root, no hardware)' \
		'make clean           - remove our build output (Decky node_modules/dist/build, C build)'

test:
	$(REPO_TEST) --fast

test-full:
	$(REPO_TEST) --full

verify:
	tools/oxp3-verify.sh

preflight:
	tools/oxp3-preflight.sh

decky:
	cd tools/decky-oxp3-status && ./build.sh --offline

install-dry-run:
	tools/oxp3-hw-install.sh --dry-run --all

units:
	$(REPO_TEST) --fast --only unit-verify --only udev-rules --only hwdb-compile

unit:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/unit -q

clean:
	rm -rf -- tools/decky-oxp3-status/node_modules
	rm -rf -- tools/decky-oxp3-status/dist
	rm -rf -- tools/decky-oxp3-status/build
	rm -rf -- tools/fingerprint/build
	find tools tests -type d -name __pycache__ -prune -exec rm -rf -- {} +
