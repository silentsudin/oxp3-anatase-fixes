/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_discover.c - find the BF63112 spidev node by ACPI modalias.
 *
 * Sysfs-only, read-only, no /dev/spidev access.  See bf63112_discover.h.
 */

#include "bf63112_discover.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

#define BF63112_ACPI_MODALIAS "acpi:BF63112"

static int
modalias_matches (const char *path)
{
  char buf[128];
  FILE *file;
  size_t len;

  file = fopen (path, "r");
  if (file == NULL)
    return 0;
  if (fgets (buf, sizeof buf, file) == NULL)
    {
      fclose (file);
      return 0;
    }
  fclose (file);
  len = strlen (buf);
  while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r'))
    buf[--len] = '\0';
  if (strncmp (buf, BF63112_ACPI_MODALIAS,
               strlen (BF63112_ACPI_MODALIAS)) != 0)
    return 0;
  /* Linux emits "acpi:BF63112:"; accept the bare id too but not a longer
   * id that merely starts with the same characters. */
  return buf[strlen (BF63112_ACPI_MODALIAS)] == '\0' ||
         buf[strlen (BF63112_ACPI_MODALIAS)] == ':';
}

static int
scan_spidev_class (const char *sysfs_root, const char *dev_root,
                   char *out, size_t out_size)
{
  char class_dir[512];
  DIR *dir;
  struct dirent *entry;
  size_t matches = 0;
  char found[256] = "";

  if (snprintf (class_dir, sizeof class_dir, "%s/class/spidev", sysfs_root) >=
      (int) sizeof class_dir)
    return -ENAMETOOLONG;
  dir = opendir (class_dir);
  if (dir == NULL)
    return errno == ENOENT ? -ENOENT : -errno;

  while ((entry = readdir (dir)) != NULL)
    {
      char modalias[640];

      if (strncmp (entry->d_name, "spidev", 6) != 0)
        continue;
      if (snprintf (modalias, sizeof modalias, "%s/%s/device/modalias",
                    class_dir, entry->d_name) >= (int) sizeof modalias)
        continue;
      if (!modalias_matches (modalias))
        continue;
      matches++;
      if (matches == 1)
        snprintf (found, sizeof found, "%s", entry->d_name);
    }
  closedir (dir);

  if (matches == 0)
    return -ENOENT;
  if (matches > 1)
    return -ENOTUNIQ;
  if (snprintf (out, out_size, "%s/%s", dev_root, found) >= (int) out_size)
    return -ENAMETOOLONG;
  return 0;
}

int
bf63112_discover_spidev (const char *sysfs_root, const char *dev_root,
                         const char *override, char *out, size_t out_size)
{
  if (dev_root == NULL || out == NULL || out_size == 0)
    return -EINVAL;
  if (override != NULL && *override != '\0')
    {
      if (snprintf (out, out_size, "%s", override) >= (int) out_size)
        return -ENAMETOOLONG;
      return 0;
    }
  if (sysfs_root == NULL || *sysfs_root == '\0')
    sysfs_root = "/sys";
  return scan_spidev_class (sysfs_root, dev_root, out, out_size);
}
