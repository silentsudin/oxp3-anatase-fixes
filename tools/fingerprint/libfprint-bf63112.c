/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112.c - libfprint driver for the Betterlife BF63112 fingerprint sensor
 *             (ONEXPLAYER 3, Intel LPSS SPI, ACPI\BF63112).
 *
 * Transport: the ACPI/SPB branch recovered from BetterlifeFingerprintDevice.dll
 * 9.39.15.26 - a one-byte selector ((opcode & 0x3f) << 1, bit 7 = host write)
 * followed by the payload or by a receive-only clock, submitted as one
 * two-entry SPI_IOC_MESSAGE.  See docs/hw/fingerprint-transport.md §4/§8 and
 * bf63112_spi.c, which is a dependency-free C mirror of fpbf63112/spi.py.
 *
 * Status: open/close/suspend/resume and the two plaintext queries (sensor info
 * 0xC1, firmware version 0xA0) are implemented.  Enrol/verify/identify
 * deliberately refuse with FP_DEVICE_ERROR_NOT_SUPPORTED because the
 * match-in-sensor payloads (template handle, score, phase semantics, SM3
 * session) are not decoded yet.  Nothing here has run against the sensor.
 *
 * This file is written against libfprint 1.94.x internal driver API
 * (fpi-device.h, fpi-ssm.h, fpi-log.h) as shipped in the v1.94.9 source tree.
 * It is our own work, not copied from libfprint, and carries the licence a
 * driver needs to be accepted upstream.
 *
 * Bring-up configuration (environment only; libfprint drivers have no config
 * file).  Every missing piece degrades to a clear error, never a crash:
 *
 *   BF63112_SPIDEV        explicit /dev/spidevX.Y (else udev data, else the
 *                         single spidev class node whose modalias is
 *                         acpi:BF63112)
 *   BF63112_SPI_SPEED     controller max speed in Hz (else _CRS if BF63112_CRS)
 *   BF63112_SPI_MODE      SPI mode 0..3 (else _CRS)
 *   BF63112_SPI_BITS      bits per word (else _CRS)
 *   BF63112_CRS           path to a raw _CRS resource-template file
 *   BF63112_IRQ_GPIO      Linux GPIO line number for GpioInt (no default)
 *   BF63112_IRQ_ACTIVE_LOW 1/0 (else _CRS polarity)
 *   BF63112_IRQ_EDGE      rising|falling|both (default rising for edge)
 *   BF63112_GPIO_CHIP     gpiochip device path (default /dev/gpiochip0)
 *   BF63112_IRQ_BACKEND   auto|gpiod|sysfs|poll
 *   BF63112_GPIO_SYSFS    sysfs GPIO base (default /sys/class/gpio)
 *   BF63112_RESET_GPIO    Linux GPIO line number for the reset line
 *   BF63112_RESET_ACTIVE_HIGH 1 = active high (default active low)
 *   BF63112_ALLOW_RESET   1 = actually pulse the reset line on open
 *
 * ACPI pin indices are controller-relative, so the driver does not pretend
 * they are Linux line numbers: set BF63112_IRQ_GPIO/BF63112_RESET_GPIO.
 */

#define FP_COMPONENT "bf63112"

#include "fpi-device.h"
#include "fpi-ssm.h"
#include "fpi-log.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <string.h>

#include "bf63112_acpi.h"
#include "bf63112_discover.h"
#include "bf63112_spi.h"

/* Recovered Windows-driver retry behaviour on an answer timeout. */
#define BF63112_MAX_ATTEMPTS 3
#define BF63112_RETRY_DELAY_MS 40

/* Plausibility bound for the image geometry.  The recovered reassembler
 * refuses a whole image above 0x16829 bytes, so no valid width*height can
 * exceed that either (docs/hw/fingerprint.md). */
#define BF63112_MAX_PIXELS 0x16829u

/* ------------------------------------------------------------------ types */

struct _FpDeviceBf63112
{
  FpDevice parent;

  struct bf63112_spi spi;
  struct bf63112_linux_spi linux_spi;
  struct bf63112_spi_policy policy;

  struct bf63112_spi_irq irq;
  struct bf63112_sysfs_irq sysfs_irq;
  struct bf63112_poll_irq poll_irq;
  gboolean have_irq;

  struct bf63112_spi_reset reset;
  struct bf63112_sysfs_reset sysfs_reset;
  gboolean have_reset;

  /* CMD_QUERY_SENSOR_INFO result (docs/hw/fingerprint.md) */
  guint16 field0;
  guint16 field2;
  guint16 width;
  guint16 height;
  guint8 sensor_flags;

  /* CMD_QUERY_FIRMWARE_VER result */
  guint8 fw_state;
  guint8 fw_project_hi;
  guint8 fw_project_lo;
  guint32 fw_version;

  /* polarity/edge recovered from _CRS, used only when the environment does
   * not override them (-1 = unknown) */
  int crs_irq_active_low;
  int crs_irq_edge;

  FpiSsm *active_ssm;
  guint8 active_opcode;
};

G_DECLARE_FINAL_TYPE (FpDeviceBf63112, fp_device_bf63112, FP, DEVICE_BF63112,
                      FpDevice)
G_DEFINE_TYPE (FpDeviceBf63112, fp_device_bf63112, FP_TYPE_DEVICE)

typedef struct
{
  gint refcount;
  FpDeviceBf63112 *self;
  FpiSsm *ssm;

  guint8 opcode;
  guint16 read_len;

  guint8 *answer;
  size_t answer_len;

  guint failures;
  guint max_attempts;
} Bf63112Request;

typedef void (*Bf63112RequestDone) (FpiSsm *ssm, FpDevice *dev, GError *error);

static const char *
bf63112_op_name (guint8 opcode)
{
  const char *name = bf63112_spi_destructive_name (opcode);

  if (name != NULL)
    return name;
  switch (opcode)
    {
    case 0xA0:
      return "QUERY_FIRMWARE_VER";
    case 0xA1:
      return "QUERY_MODULE_SCHEME_INFORMATION";
    case 0xA2:
      return "QUERY_ROOT_KEY";
    case 0xB3:
      return "QUERY_FINGERPRINT_COUNT";
    case 0xB4:
      return "GET_IMG_DATA";
    case 0xB5:
      return "DETECT_FINGERPRINT_PRESS";
    case 0xB6:
      return "SLEEP";
    case 0xC0:
      return "QUERY_MODULE_UID";
    case 0xC1:
      return "QUERY_SENSOR_INFO";
    case 0x2D:
      return "SPI status register";
    default:
      return "unknown opcode";
    }
}

/* ---------------------------------------------------------------- request */

static Bf63112Request *
bf63112_request_ref (Bf63112Request *req)
{
  g_atomic_int_inc (&req->refcount);
  return req;
}

static void
bf63112_request_unref (Bf63112Request *req)
{
  if (req == NULL)
    return;
  if (g_atomic_int_dec_and_test (&req->refcount))
    {
      g_free (req->answer);
      g_free (req);
    }
}

static Bf63112Request *
bf63112_request_new (FpDeviceBf63112 *self, guint8 opcode, guint16 read_len)
{
  Bf63112Request *req = g_new0 (Bf63112Request, 1);

  req->refcount = 1;
  req->self = self;
  req->opcode = opcode;
  req->read_len = read_len;
  req->max_attempts = BF63112_MAX_ATTEMPTS;
  req->answer = g_malloc0 (read_len > 0 ? read_len : 1);
  return req;
}

/* -------------------------------------------------------------- worker IO */

static void
bf63112_transfer_thread (GTask *task, gpointer source_object,
                         gpointer task_data, GCancellable *cancellable)
{
  Bf63112Request *req = task_data;
  struct bf63112_spi_plan plan;
  int rc;

  (void) source_object;
  (void) cancellable;

  if (g_cancellable_is_cancelled (
        fpi_device_get_cancellable ((FpDevice *) req->self)))
    {
      g_task_return_int (task, BF63112_SPI_ERR_CANCELLED);
      return;
    }

  rc = bf63112_spi_build_read (&req->self->policy, req->opcode,
                               req->read_len, 0, 0, &plan);
  if (rc == 0)
    rc = bf63112_spi_execute (&req->self->spi, &plan, req->answer,
                              req->read_len, &req->answer_len);
  g_task_return_int (task, rc);
}

static void
bf63112_request_attempt_failed (FpiSsm *ssm, Bf63112Request *req, int rc)
{
  gboolean retryable = rc == BF63112_SPI_ERR_TIMEOUT ||
                       rc == BF63112_SPI_ERR_IO;

  req->failures++;
  if (!retryable || req->failures >= req->max_attempts)
    {
      fpi_ssm_mark_failed (
        ssm,
        fpi_device_error_new_msg (
          rc == BF63112_SPI_ERR_CANCELLED ? FP_DEVICE_ERROR_GENERAL
                                          : FP_DEVICE_ERROR_PROTO,
          "BF63112: %s failed after %u attempt(s): %s",
          bf63112_op_name (req->opcode), req->failures,
          bf63112_spi_strerror (rc)));
      return;
    }
  fp_dbg ("BF63112 %s: attempt %u/%u failed (%s), re-sending",
          bf63112_op_name (req->opcode), req->failures + 1,
          req->max_attempts, bf63112_spi_strerror (rc));
  fpi_ssm_jump_to_state_delayed (ssm, 0, BF63112_RETRY_DELAY_MS);
}

static void
bf63112_transfer_done (GObject *source_object, GAsyncResult *res,
                       gpointer user_data)
{
  FpDevice *dev = (FpDevice *) source_object;
  GTask *task = G_TASK (res);
  Bf63112Request *req = g_task_get_task_data (task);
  FpiSsm *ssm = req->ssm;
  GError *error = NULL;
  int rc;

  (void) user_data;
  rc = g_task_propagate_int (task, &error);
  if (ssm == NULL)
    {
      /* close() detached the SSM while the blocking transfer was in flight */
      if (error != NULL)
        g_error_free (error);
      return;
    }
  if (error != NULL)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }
  if (rc == 0)
    {
      fpi_ssm_mark_completed (ssm);
      return;
    }
  if (rc == BF63112_SPI_ERR_CANCELLED ||
      g_cancellable_is_cancelled (fpi_device_get_cancellable (dev)))
    {
      fpi_ssm_mark_failed (
        ssm, g_error_new_literal (G_IO_ERROR, G_IO_ERROR_CANCELLED,
                                  "BF63112: operation cancelled"));
      return;
    }
  bf63112_request_attempt_failed (ssm, req, rc);
}

enum
{
  BF63112_XFER_SEND = 0,
  BF63112_XFER_RETRY,
  BF63112_XFER_NSTATES,
};

static void
bf63112_xfer_ssm (FpiSsm *ssm, FpDevice *dev)
{
  Bf63112Request *req = fpi_ssm_get_data (ssm);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case BF63112_XFER_SEND:
      {
        GTask *task = g_task_new (G_OBJECT (dev),
                                  fpi_device_get_cancellable (dev),
                                  bf63112_transfer_done, NULL);

        g_task_set_check_cancellable (task, FALSE);
        g_task_set_return_on_cancel (task, FALSE);
        g_task_set_task_data (task, bf63112_request_ref (req),
                              (GDestroyNotify) bf63112_request_unref);
        g_task_run_in_thread (task, bf63112_transfer_thread);
        g_object_unref (task);
        return;
      }
    case BF63112_XFER_RETRY:
      fpi_ssm_jump_to_state_delayed (ssm, BF63112_XFER_SEND,
                                     BF63112_RETRY_DELAY_MS);
      return;
    default:
      g_assert_not_reached ();
    }
}

static void
bf63112_request_start (FpDevice *dev, Bf63112Request *req,
                       Bf63112RequestDone done)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  FpiSsm *ssm;

  g_return_if_fail (self->active_ssm == NULL);

  ssm = fpi_ssm_new (dev, bf63112_xfer_ssm, BF63112_XFER_NSTATES);
  req->ssm = ssm;
  fpi_ssm_set_data (ssm, req, (GDestroyNotify) bf63112_request_unref);
  self->active_ssm = ssm;
  self->active_opcode = req->opcode;
  fpi_ssm_start (ssm, done);
}

/* -------------------------------------------------------------- responses */

static guint16
le16 (const guint8 *p)
{
  return (guint16) (p[0] | (p[1] << 8));
}

static guint32
le32 (const guint8 *p)
{
  return (guint32) p[0] | ((guint32) p[1] << 8) |
         ((guint32) p[2] << 16) | ((guint32) p[3] << 24);
}

/* CMD_QUERY_SENSOR_INFO (0xC1): u16 @0, u16 @2, width:u16 @4, height:u16 @6,
 * flags:u8 @8; the module rejects anything shorter than 9 bytes.  Width and
 * height are confirmed by the image path multiplying +4 by +6. */
static gboolean
bf63112_parse_sensor_info (FpDeviceBf63112 *self, const guint8 *body,
                           size_t len, GError **error)
{
  guint32 pixels;

  if (len < 9)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                   "BF63112: sensor info is %zu bytes, the module's answer "
                   "must be at least 9", len);
      return FALSE;
    }
  self->field0 = le16 (body);
  self->field2 = le16 (body + 2);
  self->width = le16 (body + 4);
  self->height = le16 (body + 6);
  self->sensor_flags = body[8];
  pixels = (guint32) self->width * self->height;

  if (self->width < 8 || self->height < 8 || pixels > BF63112_MAX_PIXELS)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                   "BF63112: implausible geometry %ux%u (%u pixels); the "
                   "sensor-info field layout may be wrong",
                   self->width, self->height, pixels);
      return FALSE;
    }
  fp_info ("BF63112: sensor %ux%u, flags 0x%02x, fields 0x%04x/0x%04x",
           self->width, self->height, self->sensor_flags, self->field0,
           self->field2);
  return TRUE;
}

/* CMD_QUERY_FIRMWARE_VER (0xA0): state:u8, project hi:u8, project lo:u8,
 * version:u32 LE @3; the Windows handler rejects anything shorter than 7. */
static void
bf63112_parse_firmware (FpDeviceBf63112 *self, const guint8 *body, size_t len)
{
  guint16 major, minor;

  if (len < 7)
    {
      fp_warn ("BF63112: firmware answer is %zu bytes, expected 7", len);
      return;
    }
  self->fw_state = body[0];
  self->fw_project_hi = body[1];
  self->fw_project_lo = body[2];
  self->fw_version = le32 (body + 3);
  major = (guint16) (self->fw_version >> 16);
  minor = (guint16) (self->fw_version & 0xFFFFu);
  fp_info ("BF63112: firmware %u.%u (state %u, project %u/%u)",
           major, minor, self->fw_state, self->fw_project_hi,
           self->fw_project_lo);
}

/* --------------------------------------------------------- configuration */

static gboolean
env_int (const char *name, int min, int max, int *out)
{
  const char *value = g_getenv (name);
  gchar *end = NULL;
  gint64 parsed;

  if (value == NULL || *value == '\0')
    return FALSE;
  parsed = g_ascii_strtoll (value, &end, 0);
  if (end == value || (end != NULL && *end != '\0') ||
      parsed < min || parsed > max)
    {
      fp_warn ("BF63112: ignoring invalid %s=%s", name, value);
      return FALSE;
    }
  *out = (int) parsed;
  return TRUE;
}

static gboolean
env_bool (const char *name, gboolean *out)
{
  const char *value = g_getenv (name);

  if (value == NULL || *value == '\0')
    return FALSE;
  if (g_ascii_strcasecmp (value, "1") == 0 ||
      g_ascii_strcasecmp (value, "true") == 0 ||
      g_ascii_strcasecmp (value, "yes") == 0)
    {
      *out = TRUE;
      return TRUE;
    }
  if (g_ascii_strcasecmp (value, "0") == 0 ||
      g_ascii_strcasecmp (value, "false") == 0 ||
      g_ascii_strcasecmp (value, "no") == 0)
    {
      *out = FALSE;
      return TRUE;
    }
  fp_warn ("BF63112: ignoring invalid boolean %s=%s", name, value);
  return FALSE;
}

static int
edge_from_string (const char *value)
{
  if (value == NULL || g_ascii_strcasecmp (value, "rising") == 0)
    return BF63112_SPI_IRQ_RISING;
  if (g_ascii_strcasecmp (value, "falling") == 0)
    return BF63112_SPI_IRQ_FALLING;
  if (g_ascii_strcasecmp (value, "both") == 0)
    return BF63112_SPI_IRQ_BOTH;
  fp_warn ("BF63112: unknown BF63112_IRQ_EDGE=%s, using rising", value);
  return BF63112_SPI_IRQ_RISING;
}

static gboolean
load_crs_file (FpDeviceBf63112 *self, struct bf63112_acpi_template *tmpl,
               GError **error)
{
  const char *path = g_getenv ("BF63112_CRS");
  g_autofree gchar *contents = NULL;
  gsize length = 0;
  enum bf63112_acpi_error parse_error = BF63112_ACPI_OK;
  struct bf63112_acpi_spi_connection connection;

  if (path == NULL || *path == '\0')
    return FALSE;
  if (!g_file_get_contents (path, &contents, &length, error))
    return FALSE;
  if (bf63112_acpi_parse ((const guint8 *) contents, length, tmpl,
                          &parse_error) < 0)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                   "BF63112: cannot parse %s: %s", path,
                   bf63112_acpi_strerror (parse_error));
      return FALSE;
    }
  bf63112_acpi_spi_connection (tmpl, &connection);
  self->crs_irq_active_low = connection.irq_active_low;
  self->crs_irq_edge = connection.irq_edge;
  if (connection.has_speed_hz && self->spi.speed_hz < 0)
    {
      self->spi.speed_hz = (int) connection.speed_hz;
      fp_info ("BF63112: _CRS speed %u Hz", connection.speed_hz);
    }
  if (connection.has_mode && self->spi.mode < 0)
    {
      self->spi.mode = connection.mode;
      fp_info ("BF63112: _CRS SPI mode %d", connection.mode);
    }
  if (connection.has_bits_per_word && self->spi.bits_per_word < 0)
    {
      self->spi.bits_per_word = connection.bits_per_word;
      fp_info ("BF63112: _CRS %d bits per word",
               connection.bits_per_word);
    }
  if (connection.has_irq_pin || connection.has_reset_pin)
    fp_info ("BF63112: _CRS GPIO pin indices irq=%s%d reset=%s%d are "
             "controller-relative; set BF63112_IRQ_GPIO/BF63112_RESET_GPIO "
             "to the Linux line numbers",
             connection.has_irq_pin ? "" : "?",
             connection.has_irq_pin ? connection.irq_pin : -1,
             connection.has_reset_pin ? "" : "?",
             connection.has_reset_pin ? connection.reset_pin : -1);
  return TRUE;
}

static int
configure_spi_from_env_and_crs (FpDeviceBf63112 *self, GError **error)
{
  struct bf63112_acpi_template tmpl;
  int value;

  if (env_int ("BF63112_SPI_SPEED", 1, INT_MAX, &value))
    self->spi.speed_hz = value;
  if (env_int ("BF63112_SPI_MODE", 0, 3, &value))
    self->spi.mode = value;
  if (env_int ("BF63112_SPI_BITS", 4, 32, &value))
    self->spi.bits_per_word = value;
  if (!load_crs_file (self, &tmpl, error))
    return *error != NULL ? -EINVAL : 0;
  return 0;
}

/* ------------------------------------------------------------ IRQ/reset */

static gboolean
setup_irq (FpDeviceBf63112 *self, GError **error)
{
  const char *backend = g_getenv ("BF63112_IRQ_BACKEND");
  const char *chip = g_getenv ("BF63112_GPIO_CHIP");
  const char *sysfs = g_getenv ("BF63112_GPIO_SYSFS");
  gboolean active_low = FALSE;
  int line, edge, rc;

  if (!env_int ("BF63112_IRQ_GPIO", 0, INT_MAX, &line))
    return TRUE;      /* no IRQ line configured: polling retry loop only */
  if (chip == NULL || *chip == '\0')
    chip = "/dev/gpiochip0";
  if (sysfs == NULL || *sysfs == '\0')
    sysfs = "/sys/class/gpio";

  if (!env_bool ("BF63112_IRQ_ACTIVE_LOW", &active_low) &&
      self->crs_irq_active_low >= 0)
    active_low = self->crs_irq_active_low == 1;
  if (g_getenv ("BF63112_IRQ_EDGE") != NULL)
    edge = edge_from_string (g_getenv ("BF63112_IRQ_EDGE"));
  else
    edge = self->crs_irq_edge >= 0 ? self->crs_irq_edge
                                   : BF63112_SPI_IRQ_RISING;

  if (backend == NULL || *backend == '\0')
    backend = "auto";

#if defined(BF63112_HAVE_GPIOD_V2) || defined(BF63112_HAVE_GPIOD_V1)
  if (g_ascii_strcasecmp (backend, "gpiod") == 0 ||
      g_ascii_strcasecmp (backend, "auto") == 0)
    {
      struct bf63112_gpiod_irq *state = NULL;

      rc = bf63112_spi_irq_gpiod_init (&self->irq, &state, line, edge,
                                       active_low, chip);
      if (rc == 0)
        rc = self->irq.open (&self->irq);
      if (rc == 0)
        {
          bf63112_spi_set_irq (&self->spi, &self->irq);
          self->have_irq = TRUE;
          fp_info ("BF63112: IRQ on %s line %d (gpiod, edge %s%s)",
                   chip, line, edge == BF63112_SPI_IRQ_BOTH ? "both" :
                   edge == BF63112_SPI_IRQ_FALLING ? "falling" : "rising",
                   active_low ? ", active-low" : "");
          return TRUE;
        }
      if (g_ascii_strcasecmp (backend, "gpiod") == 0)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND,
                       "BF63112: cannot request GPIO %d via %s", line, chip);
          return FALSE;
        }
      fp_warn ("BF63112: gpiod IRQ unavailable (%s), trying sysfs",
               bf63112_spi_strerror (rc));
    }
#else
  if (g_ascii_strcasecmp (backend, "gpiod") == 0)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
                   "BF63112: BF63112_IRQ_BACKEND=gpiod but this build has no "
                   "libgpiod support");
      return FALSE;
    }
#endif

  if (g_ascii_strcasecmp (backend, "sysfs") == 0 ||
      g_ascii_strcasecmp (backend, "auto") == 0)
    {
      rc = bf63112_spi_irq_sysfs_init (&self->irq, &self->sysfs_irq, line,
                                       edge, active_low, sysfs);
      if (rc == 0)
        rc = self->irq.open (&self->irq);
      if (rc == 0)
        {
          bf63112_spi_set_irq (&self->spi, &self->irq);
          self->have_irq = TRUE;
          fp_info ("BF63112: IRQ on sysfs GPIO %d (edge %s%s)", line,
                   edge == BF63112_SPI_IRQ_BOTH ? "both" :
                   edge == BF63112_SPI_IRQ_FALLING ? "falling" : "rising",
                   active_low ? ", active-low" : "");
          return TRUE;
        }
      if (g_ascii_strcasecmp (backend, "sysfs") == 0)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND,
                       "BF63112: cannot open sysfs GPIO %d (export failed): "
                       "%s", line, bf63112_spi_strerror (rc));
          return FALSE;
        }
      fp_warn ("BF63112: sysfs IRQ unavailable (%s), falling back to a timed "
               "poll; the interrupt line cannot be observed",
               bf63112_spi_strerror (rc));
    }

  if (g_ascii_strcasecmp (backend, "poll") == 0 ||
      g_ascii_strcasecmp (backend, "auto") == 0)
    {
      rc = bf63112_spi_irq_poll_init (&self->irq, &self->poll_irq, NULL, NULL);
      if (rc != 0)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED,
                       "BF63112: cannot set up the IRQ fallback");
          return FALSE;
        }
      bf63112_spi_set_irq (&self->spi, &self->irq);
      self->have_irq = TRUE;
      fp_warn ("BF63112: using a timed poll for GPIO %d; a timeout does not "
               "prove the device is idle", line);
      return TRUE;
    }
  g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
               "BF63112: unknown BF63112_IRQ_BACKEND=%s", backend);
  return FALSE;
}

static gboolean
setup_reset (FpDeviceBf63112 *self, GError **error)
{
  const char *backend = g_getenv ("BF63112_IRQ_BACKEND");
  const char *chip = g_getenv ("BF63112_GPIO_CHIP");
  const char *sysfs = g_getenv ("BF63112_GPIO_SYSFS");
  gboolean active_high = FALSE;
  gboolean allow = FALSE;
  int line, rc;

  if (!env_int ("BF63112_RESET_GPIO", 0, INT_MAX, &line))
    return TRUE;
  if (!env_bool ("BF63112_ALLOW_RESET", &allow) || !allow)
    {
      fp_warn ("BF63112: BF63112_RESET_GPIO is set but BF63112_ALLOW_RESET "
               "is not; not driving the reset line (the module must come up "
               "out of reset)");
      return TRUE;
    }
  if (chip == NULL || *chip == '\0')
    chip = "/dev/gpiochip0";
  if (sysfs == NULL || *sysfs == '\0')
    sysfs = "/sys/class/gpio";
  (void) env_bool ("BF63112_RESET_ACTIVE_HIGH", &active_high);
  if (backend == NULL || *backend == '\0')
    backend = "auto";

#if defined(BF63112_HAVE_GPIOD_V2) || defined(BF63112_HAVE_GPIOD_V1)
  if (g_ascii_strcasecmp (backend, "gpiod") == 0 ||
      g_ascii_strcasecmp (backend, "auto") == 0)
    {
      struct bf63112_gpiod_reset *state = NULL;

      rc = bf63112_spi_reset_gpiod_init (&self->reset, &state, line,
                                         !active_high, chip);
      if (rc == 0)
        {
          bf63112_spi_set_reset (&self->spi, &self->reset, TRUE);
          self->have_reset = TRUE;
          fp_info ("BF63112: reset on %s line %d (gpiod, %s)", chip, line,
                   active_high ? "active-high" : "active-low");
          return TRUE;
        }
      if (g_ascii_strcasecmp (backend, "gpiod") == 0)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND,
                       "BF63112: cannot configure reset GPIO %d via %s",
                       line, chip);
          return FALSE;
        }
    }
#endif
  rc = bf63112_spi_reset_sysfs_init (&self->reset, &self->sysfs_reset, line,
                                     !active_high, sysfs);
  if (rc == 0)
    {
      bf63112_spi_set_reset (&self->spi, &self->reset, TRUE);
      self->have_reset = TRUE;
      fp_info ("BF63112: reset on sysfs GPIO %d (%s)", line,
               active_high ? "active-high" : "active-low");
      return TRUE;
    }
  g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND,
               "BF63112: cannot configure reset GPIO %d: %s", line,
               bf63112_spi_strerror (rc));
  return FALSE;
}

static gboolean
resolve_spidev (FpDevice *dev, char *buf, size_t buf_size, GError **error)
{
  const char *override = g_getenv ("BF63112_SPIDEV");
  const char *udev_path;
  int rc;

  if (override != NULL && *override != '\0')
    {
      if (g_strlcpy (buf, override, buf_size) >= buf_size)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
                       "BF63112: BF63112_SPIDEV is too long");
          return FALSE;
        }
      return TRUE;
    }
  udev_path = fpi_device_get_udev_data (dev, FPI_DEVICE_UDEV_SUBTYPE_SPIDEV);
  if (udev_path != NULL && *udev_path != '\0')
    {
      if (g_strlcpy (buf, udev_path, buf_size) >= buf_size)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
                       "BF63112: udev spidev path is too long");
          return FALSE;
        }
      return TRUE;
    }
  rc = bf63112_discover_spidev ("/sys", "/dev", NULL, buf, buf_size);
  if (rc == 0)
    {
      fp_info ("BF63112: discovered spidev node %s (modalias acpi:BF63112)",
               buf);
      return TRUE;
    }
  if (rc == -ENOTUNIQ)
    g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
                 "BF63112: more than one spidev device matches acpi:BF63112; "
                 "set BF63112_SPIDEV to the intended /dev/spidevX.Y");
  else if (rc == -ENAMETOOLONG)
    g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
                 "BF63112: discovered spidev path is too long");
  else
    g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND,
                 "BF63112: no spidev node for acpi:BF63112 (bind the spidev "
                 "driver to the SPI device, or set BF63112_SPIDEV)");
  return FALSE;
}

/* --------------------------------------------------------------- FpDevice */

static void
bf63112_open_info_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  Bf63112Request *req = fpi_ssm_get_data (ssm);
  g_autoptr(GError) local_error = NULL;

  self->active_ssm = NULL;
  if (error != NULL)
    {
      bf63112_spi_close (&self->spi);
      fpi_device_open_complete (dev, error);
      return;
    }
  if (!bf63112_parse_sensor_info (self, req->answer, req->answer_len,
                                  &local_error))
    {
      bf63112_spi_close (&self->spi);
      fpi_device_open_complete (dev, g_steal_pointer (&local_error));
      return;
    }
  fpi_device_open_complete (dev, NULL);
}

static void
bf63112_open_fw_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  Bf63112Request *req = fpi_ssm_get_data (ssm);

  self->active_ssm = NULL;
  if (error != NULL)
    {
      /* informational only; the device is usable without it */
      fp_warn ("BF63112: firmware query failed: %s", error->message);
      g_error_free (error);
    }
  else
    {
      bf63112_parse_firmware (self, req->answer, req->answer_len);
    }
  /* The vendor driver queries the firmware first; sensor info is the
   * mandatory one, so ask for it last. */
  bf63112_request_start (dev, bf63112_request_new (self, 0xC1, 9),
                         bf63112_open_info_done);
}

static void
bf63112_open (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  g_autoptr(GError) error = NULL;
  char spidev_path[256];
  int rc;

  if (!resolve_spidev (dev, spidev_path, sizeof spidev_path, &error))
    {
      fpi_device_open_complete (dev, g_steal_pointer (&error));
      return;
    }
  rc = bf63112_spi_linux_init (&self->spi, &self->linux_spi, spidev_path);
  if (rc != 0)
    {
      fpi_device_open_complete (
        dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                       "BF63112: cannot configure transport "
                                       "for %s: %s", spidev_path,
                                       bf63112_spi_strerror (rc)));
      return;
    }
  self->spi.speed_hz = -1;
  self->spi.mode = -1;
  self->spi.bits_per_word = -1;
  if (configure_spi_from_env_and_crs (self, &error) != 0)
    {
      fpi_device_open_complete (
        dev, error != NULL ? g_steal_pointer (&error)
                           : fpi_device_error_new_msg (
                               FP_DEVICE_ERROR_GENERAL,
                               "BF63112: bad _CRS configuration"));
      return;
    }
  if (!setup_irq (self, &error) || !setup_reset (self, &error))
    {
      bf63112_spi_close (&self->spi);
      fpi_device_open_complete (dev, g_steal_pointer (&error));
      return;
    }
  rc = bf63112_spi_open (&self->spi);
  if (rc != 0)
    {
      fpi_device_open_complete (
        dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                       "BF63112: cannot open %s: %s",
                                       spidev_path,
                                       bf63112_spi_strerror (rc)));
      return;
    }
  fp_info ("BF63112: SPI transport open on %s (speed %d, mode %d, bits %d)",
           spidev_path, self->spi.speed_hz, self->spi.mode,
           self->spi.bits_per_word);

  bf63112_request_start (dev, bf63112_request_new (self, 0xA0, 7),
                         bf63112_open_fw_done);
}

static void
bf63112_close (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);

  if (self->active_ssm != NULL)
    {
      Bf63112Request *req = fpi_ssm_get_data (self->active_ssm);

      /* The worker may still be inside the (non-interruptible) spidev ioctl;
       * detach its completion so it cannot touch a freed SSM.  Closing the fd
       * under it only makes that ioctl return EBADF, which is then dropped. */
      if (req != NULL)
        req->ssm = NULL;
      fpi_ssm_mark_failed (
        self->active_ssm,
        fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                  "BF63112: device closed"));
      self->active_ssm = NULL;
    }
  bf63112_spi_close (&self->spi);
  fpi_device_close_complete (dev, NULL);
}

static void
bf63112_suspend (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);

  /* The SLEEP opcode is not on the read allowlist and its SPI form is not
   * proven; closing the spidev node is the safe equivalent. */
  bf63112_spi_close (&self->spi);
  fp_dbg ("BF63112: suspended (spidev closed)");
  fpi_device_suspend_complete (dev, NULL);
}

static void
bf63112_resume_info_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  Bf63112Request *req = fpi_ssm_get_data (ssm);
  g_autoptr(GError) local_error = NULL;

  self->active_ssm = NULL;
  if (error != NULL)
    {
      bf63112_spi_close (&self->spi);
      fpi_device_resume_complete (dev, error);
      return;
    }
  if (!bf63112_parse_sensor_info (self, req->answer, req->answer_len,
                                  &local_error))
    {
      bf63112_spi_close (&self->spi);
      fpi_device_resume_complete (dev, g_steal_pointer (&local_error));
      return;
    }
  fpi_device_resume_complete (dev, NULL);
}

static void
bf63112_resume (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  int rc;

  rc = bf63112_spi_open (&self->spi);
  if (rc != 0)
    {
      fpi_device_resume_complete (
        dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                       "BF63112: cannot reopen SPI: %s",
                                       bf63112_spi_strerror (rc)));
      return;
    }
  bf63112_request_start (dev, bf63112_request_new (self, 0xC1, 9),
                         bf63112_resume_info_done);
}

/*
 * Enrol / verify / identify are match-in-sensor commands.  The recovered
 * Windows sequences are 0xB1 phases 0..3 (enrol), 0xB2 (identify/remove) and
 * 0xB3 (remove/count), but the per-phase payloads, handle encoding, score
 * field and the SM2/SM3/SM4 session are undecoded.  Rather than guess at
 * biometric state - and risk destroying templates enrolled from Windows - the
 * actions refuse cleanly.
 */
static void
bf63112_enroll (FpDevice *dev)
{
  fpi_device_enroll_complete (
    dev, NULL,
    fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                              "BF63112: enrol needs the undecoded 0xB0/0xB1 "
                              "payload and handle format (see docs)"));
}

static void
bf63112_verify (FpDevice *dev)
{
  fpi_device_verify_complete (
    dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                   "BF63112: verify needs the undecoded 0xB1 "
                                   "handle/score format (see docs)"));
}

static void
bf63112_identify (FpDevice *dev)
{
  fpi_device_identify_complete (
    dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                   "BF63112: identify needs the undecoded "
                                   "0xB1 handle/score format (see docs)"));
}

/* --------------------------------------------------------------- GObject */

static const FpIdEntry bf63112_id_table[] = {
  /* SPI-attached ACPI device; libfprint generates the matching
   * 70-libfprint-2.rules entry that binds spidev (same as ELAN7001). */
  { .udev_types = FPI_DEVICE_UDEV_SUBTYPE_SPIDEV,
    .spi_acpi_id = "BF63112" },
  { .udev_types = 0 },
};

static void
fp_device_bf63112_class_init (FpDeviceBf63112Class *klass)
{
  FpDeviceClass *dev_class = FP_DEVICE_CLASS (klass);

  dev_class->id = "bf63112";
  dev_class->full_name = "Betterlife BF63112 (ONEXPLAYER 3)";
  dev_class->type = FP_DEVICE_TYPE_UDEV;
  dev_class->id_table = bf63112_id_table;
  dev_class->scan_type = FP_SCAN_TYPE_PRESS;

  dev_class->open = bf63112_open;
  dev_class->close = bf63112_close;
  dev_class->enroll = bf63112_enroll;
  dev_class->verify = bf63112_verify;
  dev_class->identify = bf63112_identify;
  dev_class->suspend = bf63112_suspend;
  dev_class->resume = bf63112_resume;

  /* Derives VERIFY/IDENTIFY from the vfuncs above; STORAGE is intentionally
   * not advertised because list/delete/clear are not implemented. */
  fpi_device_class_auto_initialize_features (dev_class);
}

static void
fp_device_bf63112_init (FpDeviceBf63112 *self)
{
  memset (&self->spi, 0, sizeof self->spi);
  memset (&self->linux_spi, 0, sizeof self->linux_spi);
  memset (&self->irq, 0, sizeof self->irq);
  memset (&self->sysfs_irq, 0, sizeof self->sysfs_irq);
  memset (&self->poll_irq, 0, sizeof self->poll_irq);
  memset (&self->reset, 0, sizeof self->reset);
  memset (&self->sysfs_reset, 0, sizeof self->sysfs_reset);
  bf63112_spi_init (&self->spi, NULL, NULL);
  bf63112_spi_policy_init (&self->policy);
  /* The driver is the trusted caller: it opts in to I/O, but only the
   * read-only opcodes above are allowlisted and no write is ever allowed. */
  self->policy.allow_io = TRUE;
  self->crs_irq_active_low = -1;
  self->crs_irq_edge = -1;
  self->active_ssm = NULL;
  self->have_irq = FALSE;
  self->have_reset = FALSE;
}
