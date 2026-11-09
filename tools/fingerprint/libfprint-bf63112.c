/* bf63112.c - skeleton libfprint driver for the Betterlife BF63112 SPI sensor
 * (ONEXPLAYER 3 "OXP3").  This is a TEMPLATE, not a working driver: it is not
 * wired into any build here and it will not compile on its own, because it
 * needs libfprint's internal headers (fpi-device.h, fpi-spi-transfer.h,
 * fpi-image.h, ...) and its meson build tree.  It exists to pin down where the
 * work goes and to carry the frame format we recovered from the Windows UMDF
 * driver into the place it will be needed.  See docs/hw/fingerprint.md.
 *
 * Deliberate scope: init/deinit/sensor-info plus the transport helpers.  The
 * enrol/identify state machines (match-in-sensor) are the open work; the
 * opcode numbers for them are known and are listed in the doc.
 *
 * SPDX-License-Identifier: LGPL-2.1-or-later  (same licence a driver would need
 * to be accepted by libfprint; this file is our own work, not copied from it)
 */

#include "fpi-device.h"
#include "fpi-spi-transfer.h"

#define BF63112_VID_PID_STR "BF63112"
#define BF63112_SOF        0xAA
#define BF63112_CMD_MAGIC  0x4C
#define BF63112_HDR_RESERVE 16

/* Command byte = descriptor+0x10 of the Windows driver.  The two we can use
 * without knowing the session state machines yet: */
#define BF63112_CMD_FW_VER      0xA1
#define BF63112_CMD_MODULE_SCHEME 0xA2
#define BF63112_CMD_SENSOR_INFO 0xC2
#define BF63112_CMD_MODULE_UID  0xC1
#define BF63112_CMD_FP_COUNT    0xB4
/* Known but unimplemented (see doc): 0xB1 ENROLL, 0xB2 IDENTIFY,
 * 0xB3 REMOVE, 0xB5 GET_IMG_DATA, 0xB6 DETECT_PRESS, 0xC0 SLEEP,
 * 0xC3 JTAG, 0xD0 RESTORE_MCU, 0xD1 UPDATE_FIRMWARE, 0xFF WORK_MODE_SWITCH */

struct _FpDeviceBf63112
{
  FpDevice parent;
  gint spi_fd;             /* /dev/spidevN.M, opened by the udev rule */
  guint16 width, height;   /* from CMD_SENSOR_INFO */
  guint8 flags;
};

G_DECLARE_FINAL_TYPE (FpDeviceBf63112, fp_device_bf63112, FP, DEVICE_BF63112,
                      FpDevice)
G_DEFINE_TYPE (FpDeviceBf63112, fp_device_bf63112, FP_TYPE_DEVICE)

/* ---- transport ---------------------------------------------------------- */

/* CRC-32/ISO-HDLC, the same table-driven CRC the Windows driver appends. */
static guint32
bf63112_crc32 (const guint8 *buf, gsize len)
{
  guint32 crc = 0xFFFFFFFF;

  for (gsize i = 0; i < len; i++)
    {
      crc ^= buf[i];
      for (int b = 0; b < 8; b++)
        crc = (crc >> 1) ^ (0xEDB88320 & (-(crc & 1)));
    }
  return ~crc;
}

/* Build one host frame:
 *   AA | L:u32 | 16 x 0 | 4C op phase 00 arg:u16 len:u32 data | pad16 | crc32
 *   L = 20 + align16 (10 + data_len)
 * Returns a newly allocated buffer and sets *out_len. */
static guint8 *
bf63112_build_frame (guint8 opcode, guint8 phase, guint16 arg,
                     const guint8 *data, gsize data_len, gsize *out_len)
{
  gsize block = 10 + data_len;
  gsize aligned = (block + 15) / 16 * 16;
  gsize total = 1 + 4 + BF63112_HDR_RESERVE + aligned + 4;
  guint8 *f = g_malloc0 (total);
  gsize off = 0;

  f[off++] = BF63112_SOF;
  /* L counts everything after the SOF except the CRC */
  f[off++] = (20 + aligned) & 0xFF;
  f[off++] = ((20 + aligned) >> 8) & 0xFF;
  f[off++] = ((20 + aligned) >> 16) & 0xFF;
  f[off++] = ((20 + aligned) >> 24) & 0xFF;
  off += BF63112_HDR_RESERVE;          /* zeros: request header area */

  f[off++] = BF63112_CMD_MAGIC;
  f[off++] = opcode;
  f[off++] = phase;
  f[off++] = 0x00;
  f[off++] = arg & 0xFF;
  f[off++] = (arg >> 8) & 0xFF;
  f[off++] = data_len & 0xFF;
  f[off++] = (data_len >> 8) & 0xFF;
  f[off++] = (data_len >> 16) & 0xFF;
  f[off++] = (data_len >> 24) & 0xFF;
  if (data_len)
    memcpy (f + off, data, data_len);
  off += data_len;
  /* the rest of the block stays zero (16-byte alignment padding) */

  {
    guint32 crc = bf63112_crc32 (f, total - 4);
    f[total - 4] = crc & 0xFF;
    f[total - 3] = (crc >> 8) & 0xFF;
    f[total - 2] = (crc >> 16) & 0xFF;
    f[total - 1] = (crc >> 24) & 0xFF;
  }

  *out_len = total;
  return f;
}

/* Send a frame and read the answer.  The sensor asserts its IRQ GPIO when the
 * answer is ready; a real driver must wait on that line (spi-intel does not
 * expose it, so the ACPI _CRS GPIO must be requested by the driver itself).
 * Without it we just re-read until the answer parses. */
static gboolean
bf63112_transceive (FpDeviceBf63112 *self, guint8 opcode, guint8 phase,
                    guint16 arg, const guint8 *data, gsize data_len,
                    guint8 *resp, gsize resp_len, GError **error)
{
  g_autofree guint8 *frame = NULL;
  gsize frame_len = 0;

  frame = bf63112_build_frame (opcode, phase, arg, data, data_len, &frame_len);
  if (!fpi_spi_transfer_send (self->spi_fd, frame, frame_len, error))
    return FALSE;

  /* TODO: wait for the IRQ GPIO instead of sleeping. */
  g_usleep (20 * 1000);

  if (!fpi_spi_transfer_recv (self->spi_fd, resp, resp_len, error))
    return FALSE;

  if (resp[0] != BF63112_SOF)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED,
                   "BF63112: no frame start byte (SPI mode/speed?)");
      return FALSE;
    }
  return TRUE;
}

/* ---- FpDevice glue ------------------------------------------------------ */

static void
bf63112_open (FpDevice *dev)
{
  /* 1. sensor_info -> width/height/flags, sanity check width*height < 64k
   * 2. module UID + firmware version into the device name / debug log
   * 3. leave the sensor awake; SLEEP (0xC0) only in ->suspend */
}

static void
bf63112_close (FpDevice *dev)
{
}

static void
bf63112_enroll (FpDevice *dev, FpPrint *print, FpFinger finger)
{
  /* not implemented: match-in-sensor.  Sequence recovered:
   *   0xB1 phase 0 (start) -> device waits for a finger
   *   0xB1 phase 1 (each press) with a 1-byte counter in the payload
   *   0xB1 phase 2 (fini) -> device returns a template handle
   *   0xB1 phase 3 (cancel)
   * Store only the handle; images never leave the module. */
  fpi_device_enroll_complete (dev, NULL,
                              FPI_DEVICE_ERROR_GENERAL);
}

static void
bf63112_verify (FpDevice *dev, FpPrint *print)
{
  /* 0xB2 IDENTIFY: device matches against its own template store and returns
   * a handle + score; the host only decides whether the handle matches the
   * enrolled one.  Template never crosses SPI. */
  fpi_device_verify_complete (dev, FPI_MATCH_ERROR_NONE);
}

static void
bf63112_class_init (FpDeviceBf63112Class *klass)
{
  FpDeviceClass *dev_class = FP_DEVICE_CLASS (klass);

  dev_class->id = "bf63112";
  dev_class->full_name = "Betterlife BF63112 (ONEXPLAYER 3)";
  dev_class->type = FP_DEVICE_TYPE_SPI;   /* only SPI, no USB IDs */
  dev_class->id_table = NULL;             /* matched by udev modalias instead */
  dev_class->scan_type = FP_SCAN_TYPE_PRESS;
  dev_class->temp_hot_seconds = -1;

  dev_class->open = bf63112_open;
  dev_class->close = bf63112_close;
  dev_class->enroll = bf63112_enroll;
  dev_class->verify = bf63112_verify;
}

static void
bf63112_init (FpDeviceBf63112 *self)
{
}

/* An out-of-tree build hook would live here; upstream would instead add the
 * device to libfprint's meson.build and to 70-libfprint-2.rules:

 *   ACTION=="add|change", SUBSYSTEM=="spi", ENV{MODALIAS}=="acpi:BF63112:",
 *     RUN{builtin}+="kmod load spi:spidev",
 *     RUN+="/bin/sh -c 'echo spidev > %S%p/driver_override && \
 *            echo %k > %S%p/subsystem/drivers/spidev/bind'"
 *
 * which is the same pattern libfprint already uses for ELAN7001/ELAN70A1.
 */
