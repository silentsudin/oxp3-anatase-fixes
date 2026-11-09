# SPDX-License-Identifier: LGPL-2.1-or-later
#
# BF63112 protocol library, its C tests and the libfprint driver.
#
# Quick use:
#   make check          Python suite + C protocol tests
#   make proto-test     build & run tests/test_proto.c (needs a C compiler)
#   make syntax         syntax-check the driver against tools/fingerprint/stub/
#                       (no libfprint/GLib needed; offline, compile-only),
#                       with and without BF63112_ENABLE_SESSION
#   make session-syntax syntax-check the OpenSSL session module (needs
#                       openssl-devel; runtime SM support is a different thing)
#   make syntax-real    syntax-check against a real libfprint source tree:
#                       make syntax-real LIBFPRINT_SRC=/path/to/libfprint
#   make clean
#
# The ONEXPLAYER 3 host has Python but no C compiler and no libfprint headers;
# tests/run-c-tests.sh runs the C targets inside an ephemeral container instead
# (see README.md).  Nothing here is ever installed.

CC           ?= cc
PYTHON       ?= python3
BUILD_DIR    ?= build
LIBFPRINT_SRC ?=
# directory holding the meson-generated fp-enums.h/fpi-enums.h (optional)
ENUMS_INCLUDE ?=

CFLAGS  ?= -O2 -g
WARN     = -std=gnu11 -Wall -Wextra -Werror

PROTO_SRC  = bf63112_proto.c
PROTO_HDR  = bf63112_proto.h
DRIVER_SRC = libfprint-bf63112.c
SESSION_SRC = bf63112_session_openssl.c
TEST_SRC   = tests/test_proto.c

.PHONY: all check py-test proto-test syntax session-syntax syntax-real clean

all: check

check: py-test proto-test syntax
	@echo "bf63112: all checks passed"

py-test:
	$(PYTHON) -m unittest discover -s tests -v

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

proto-test: $(BUILD_DIR)
	$(CC) $(WARN) -I. -Itests -o $(BUILD_DIR)/test_proto $(TEST_SRC) $(PROTO_SRC)
	$(BUILD_DIR)/test_proto

# Offline syntax check against our own shim headers (stub/, clearly marked
# "not libfprint").  Proves our C is well-formed; not API compatibility.
syntax: $(BUILD_DIR)
	$(CC) $(WARN) -Istub -c -fsyntax-only $(DRIVER_SRC)
	$(CC) $(WARN) -Istub -DBF63112_ENABLE_SESSION -c -fsyntax-only $(DRIVER_SRC)
	@echo "syntax: OK (stub headers, not libfprint; plain + session build)"

# The session crypto module is the only file that needs OpenSSL headers.  This
# is a compile check, not a capability check: a build without SM2/SM3/SM4 in its
# provider still compiles and returns -ENOTSUP at run time.
session-syntax: $(BUILD_DIR)
	$(CC) $(WARN) -DBF63112_ENABLE_SESSION -c -fsyntax-only $(SESSION_SRC)
	@echo "session-syntax: OK (OpenSSL headers present)"

# The authoritative check: libfprint 1.94.x source tree + GLib/GUsb headers.
syntax-real: $(BUILD_DIR)
	@echo "syntax-check: $(DRIVER_SRC) against $(LIBFPRINT_SRC)"
	@test -n "$(LIBFPRINT_SRC)" || \
	  { echo "usage: make syntax-real LIBFPRINT_SRC=/path/to/libfprint" >&2; exit 2; }
	$(CC) $(WARN) $(if $(ENUMS_INCLUDE),-I$(ENUMS_INCLUDE)) \
	  -I$(LIBFPRINT_SRC)/libfprint \
	  $(shell pkg-config --cflags glib-2.0 gio-2.0 gusb) \
	  -c -fsyntax-only $(DRIVER_SRC)
	@echo "syntax-real: OK ($(DRIVER_SRC) vs $(LIBFPRINT_SRC))"

clean:
	rm -rf $(BUILD_DIR)
