/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * fpi-log.h - OFFLINE SYNTAX-CHECK SHIM.  NOT the libfprint header.
 * The driver only needs the debug macros; they map to a variadic stub.
 */

#ifndef BF63112_STUB_FPI_LOG_H
#define BF63112_STUB_FPI_LOG_H

#include "fpi-device.h"

void bf63112_stub_log (const char *format, ...);

#define fp_dbg(...)  bf63112_stub_log (__VA_ARGS__)
#define fp_info(...) bf63112_stub_log (__VA_ARGS__)
#define fp_warn(...) bf63112_stub_log (__VA_ARGS__)
#define fp_err(...)  bf63112_stub_log (__VA_ARGS__)

#endif /* BF63112_STUB_FPI_LOG_H */
