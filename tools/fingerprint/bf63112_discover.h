/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_discover.h - resolve the BF63112 spidev node without udev data.
 *
 * libfprint usually hands a driver an spidev path through its udev data, but
 * that depends on libfprint's 70-libfprint-2.rules binding spidev.  When the
 * udev data is missing (or a bring-up build wants an explicit node), this
 * helper finds the single SPI device whose modalias starts with
 * "acpi:BF63112".  It reads sysfs only and never opens /dev/spidev.
 */

#ifndef BF63112_DISCOVER_H
#define BF63112_DISCOVER_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*!
 * Resolve a spidev path.
 *
 * @sysfs_root: sysfs mount point, normally "/sys".
 * @dev_root:   devtmpfs mount point, normally "/dev".
 * @override:   explicit path (e.g. from BF63112_SPIDEV); used verbatim when
 *              non-NULL and non-empty.
 * @out/@out_size: receives e.g. "/dev/spidev0.0".
 *
 * Returns 0 on success, -ENOENT when no device matches, -ENOTUNIQ when more
 * than one does (refusing to guess), -ENAMETOOLONG or -EINVAL otherwise.
 * A match is a /sys/class/spidev/spidevX.Y entry whose device/modalias is
 * "acpi:BF63112" (a trailing ':' is accepted, as Linux emits it).
 */
int bf63112_discover_spidev (const char *sysfs_root, const char *dev_root,
                             const char *override,
                             char *out, size_t out_size);

#ifdef __cplusplus
}
#endif

#endif /* BF63112_DISCOVER_H */
