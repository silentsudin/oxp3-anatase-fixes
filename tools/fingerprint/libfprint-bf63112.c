/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112.c - libfprint driver for the Betterlife BF63112 fingerprint sensor
 *             (ONEXPLAYER 3, Intel LPSS SPI, ACPI\BF63112).
 *
 * Status: transport, open/close and suspend/resume are implemented against the
 * recovered frame protocol; enrol/verify/identify deliberately refuse with
 * FP_DEVICE_ERROR_NOT_SUPPORTED because the match-in-sensor payloads (template
 * handle, score, phase semantics, SM3 session) are not decoded yet.  See
 * docs/hw/fingerprint.md and tools/fingerprint/README.md for what is proven,
 * what is inferred, and what a maintainer must adapt.
 *
 * It is written against libfprint 1.94.x internal driver API (fpi-device.h,
 * fpi-spi-transfer.h, fpi-ssm.h, fpi-log.h) as shipped in the v1.94.9 source
 * tree.  This file is our own work, not copied from libfprint; it carries the
 * same licence a driver needs to be accepted upstream.
 *
 * Hardware facts this file relies on:
 *   - the device is udev/spidev backed, matched by ACPI id "BF63112"
 *     (FpIdEntry.udev_types = FPI_DEVICE_UDEV_SUBTYPE_SPIDEV,
 *      FpIdEntry.spi_acpi_id = "BF63112");
 *   - every host frame is AA | L:u32 | 16x0 | 4C op phase 00 arg:u16 len:u32
 *     data | pad16 | CRC-32, L = 20 + align16 (10 + data_len);
 *   - the module signals "answer ready" on a GPIO the ACPI _CRS describes.
 *     libfprint's fpi_spi_transfer has no GPIO support, so this driver falls
 *     back to re-reading after a delay (the Windows driver instead waits on the
 *     GPIO event and re-sends on timeout).  Getting the GPIO is future work -
 *     see the docs - and the only thing that turns the retry loop below from
 *     "poll" into the vendor behaviour.
 */

#define FP_COMPONENT "bf63112"

#include "fpi-device.h"
#include "fpi-spi-transfer.h"
#include "fpi-ssm.h"
#include "fpi-log.h"

#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>

#include "bf63112_proto.h"

/* One spidev readback.  The answer to a query fits easily; GET_IMG chunks can
 * be up to 8140 bytes and would need a larger buffer (and a spidev bufsiz
 * change), which this driver does not use. */
#define BF63112_READ_LEN 512

/* Recovered Windows-driver retry behaviour: re-send on an answer timeout.
 * The per-attempt bound is 1000/5000 ms there; 40 ms is the gap the probe uses
 * between polls and is plenty for a query answer. */
#define BF63112_MAX_ATTEMPTS 3
#define BF63112_RETRY_DELAY_MS 40

/* ------------------------------------------------------------------ types */

struct _FpDeviceBf63112
{
  FpDevice parent;

  int spi_fd;

  /* CMD_QUERY_SENSOR_INFO result (field layout inferred, see docs) */
  guint16 width;
  guint16 height;
  guint8 sensor_flags;

  /* CMD_QUERY_FIRMWARE_VER result (informational only) */
  guint8 fw_mode;
  guint8 fw_project;
  guint8 fw_variant;
  guint8 fw_version[4];

  /* bookkeeping for the in-flight command, if any */
  FpiSsm *active_ssm;
  guint8 active_opcode;
};

G_DECLARE_FINAL_TYPE (FpDeviceBf63112, fp_device_bf63112, FP, DEVICE_BF63112,
                      FpDevice)
G_DEFINE_TYPE (FpDeviceBf63112, fp_device_bf63112, FP_TYPE_DEVICE)

typedef struct
{
  FpDeviceBf63112 *self;

  guint8 opcode;
  guint8 phase;
  guint16 arg;
  guint8 *payload;
  size_t payload_len;

  guint failures;
  guint max_attempts;

  /* answer, copied out of the transfer buffer before it is freed */
  guint8 *answer;
  size_t answer_len;
  guint8 answer_status;
  guint8 answer_phase;
  guint16 answer_arg;
} Bf63112Request;

typedef void (*Bf63112RequestDone) (FpiSsm *ssm, FpDevice *dev, GError *error);

static const char *
bf63112_op_name (guint8 opcode)
{
  const char *name = bf63112_opcode_name (opcode);
  return name != NULL ? name : "unknown opcode";
}

/* ------------------------------------------------------------- request I/O */

enum
{
  BF63112_XFER_SEND = 0,
  BF63112_XFER_RETRY,
  BF63112_XFER_NSTATES,
};

static void
bf63112_request_free (Bf63112Request *req)
{
  if (req == NULL)
    return;
  g_free (req->payload);
  g_free (req->answer);
  g_free (req);
}

static Bf63112Request *
bf63112_request_new (FpDeviceBf63112 *self, guint8 opcode, guint8 phase,
                     guint16 arg, const guint8 *payload, size_t payload_len)
{
  Bf63112Request *req = g_new0 (Bf63112Request, 1);

  req->self = self;
  req->opcode = opcode;
  req->phase = phase;
  req->arg = arg;
  req->max_attempts = BF63112_MAX_ATTEMPTS;
  if (payload_len > 0)
    {
      req->payload = g_malloc (payload_len);
      memcpy (req->payload, payload, payload_len);
      req->payload_len = payload_len;
    }
  return req;
}

/* A failed attempt: retry until the budget is gone, then fail the SSM. */
static void
bf63112_request_attempt_failed (FpiSsm *ssm, Bf63112Request *req,
                                GError *error)
{
  req->failures++;
  if (req->failures >= req->max_attempts)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }
  fp_dbg ("BF63112 %s: attempt %u/%u failed (%s), re-sending",
          bf63112_op_name (req->opcode),
          req->failures + 1, req->max_attempts, error->message);
  g_error_free (error);
  fpi_ssm_jump_to_state (ssm, BF63112_XFER_RETRY);
}

static void
bf63112_xfer_cb (FpiSpiTransfer *transfer, FpDevice *dev, gpointer user_data,
                 GError *error)
{
  Bf63112Request *req = user_data;
  FpiSsm *ssm = transfer->ssm;
  struct bf63112_frame frame;
  size_t offset = 0;
  enum bf63112_scan_result scan;

  (void) dev;
  g_assert (ssm != NULL);
  g_assert (req != NULL);

  if (error != NULL)
    {
      if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        {
          fpi_ssm_mark_failed (ssm, error);
          return;
        }
      bf63112_request_attempt_failed (ssm, req, error);
      return;
    }

  scan = bf63112_scan_frame (transfer->buffer_rd,
                             (size_t) transfer->length_rd, &offset, &frame);
  if (scan != BF63112_SCAN_OK)
    {
      const char *why = scan == BF63112_SCAN_TRUNCATED ? "truncated frame" :
                        scan == BF63112_SCAN_CORRUPT   ? "corrupt frame" :
                                                         "no frame";
      bf63112_request_attempt_failed (
        ssm, req,
        fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                  "BF63112: no valid answer (%s at %zu, "
                                  "SPI mode/clock or IRQ timing?)",
                                  why, offset));
      return;
    }

  if (frame.opcode != req->opcode)
    {
      bf63112_request_attempt_failed (
        ssm, req,
        fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                  "BF63112: answer is %s, expected %s",
                                  bf63112_op_name (frame.opcode),
                                  bf63112_op_name (req->opcode)));
      return;
    }

  /* The docs do not prove that an answer echoes the phase; accept either. */
  g_free (req->answer);
  req->answer = g_malloc (frame.data_len > 0 ? frame.data_len : 1);
  if (frame.data_len > 0)
    memcpy (req->answer, frame.data, frame.data_len);
  req->answer_len = frame.data_len;
  req->answer_status = frame.status;
  req->answer_phase = frame.phase;
  req->answer_arg = frame.arg;

  fpi_ssm_mark_completed (ssm);
}

static void
bf63112_xfer_ssm (FpiSsm *ssm, FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  Bf63112Request *req = fpi_ssm_get_data (ssm);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case BF63112_XFER_SEND:
      {
        size_t frame_len = bf63112_frame_size (req->payload_len);
        guint8 *frame = g_malloc0 (frame_len);
        guint8 *rx = g_malloc0 (BF63112_READ_LEN);
        FpiSpiTransfer *xfer;

        if (bf63112_build_frame (req->opcode, req->phase, 0, req->arg,
                                 req->payload, req->payload_len,
                                 frame, frame_len) == 0)
          {
            g_free (frame);
            g_free (rx);
            fpi_ssm_mark_failed (ssm,
                                 fpi_device_error_new_msg (
                                   FP_DEVICE_ERROR_GENERAL,
                                   "BF63112: cannot build %s frame",
                                   bf63112_op_name (req->opcode)));
            return;
          }

        if (self->spi_fd < 0)
          {
            g_free (frame);
            g_free (rx);
            fpi_ssm_mark_failed (ssm,
                                 fpi_device_error_new_msg (
                                   FP_DEVICE_ERROR_NOT_OPEN,
                                   "BF63112: spidev is not open"));
            return;
          }

        xfer = fpi_spi_transfer_new (dev, self->spi_fd);
        if (xfer == NULL)
          {
            g_free (frame);
            g_free (rx);
            fpi_ssm_mark_failed (ssm,
                                 fpi_device_error_new_msg (
                                   FP_DEVICE_ERROR_GENERAL,
                                   "BF63112: cannot allocate SPI transfer"));
            return;
          }

        /* write the frame, then clock in the answer in the same transfer */
        fpi_spi_transfer_write_full (xfer, frame, frame_len, g_free);
        fpi_spi_transfer_read_full (xfer, rx, BF63112_READ_LEN, g_free);
        xfer->ssm = ssm;
        fpi_spi_transfer_submit (xfer, fpi_device_get_cancellable (dev),
                                 bf63112_xfer_cb, req);
        return;
      }

    case BF63112_XFER_RETRY:
      /* No GPIO IRQ support yet: wait a little, then re-send the frame. */
      fpi_ssm_jump_to_state_delayed (ssm, BF63112_XFER_SEND,
                                     BF63112_RETRY_DELAY_MS);
      return;
    }
}

/* Start one command; @done receives the request data via fpi_ssm_get_data(). */
static void
bf63112_request_start (FpDevice *dev, Bf63112Request *req,
                       Bf63112RequestDone done)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  FpiSsm *ssm;

  g_return_if_fail (self->active_ssm == NULL);

  ssm = fpi_ssm_new (dev, bf63112_xfer_ssm, BF63112_XFER_NSTATES);
  fpi_ssm_set_data (ssm, req, (GDestroyNotify) bf63112_request_free);
  self->active_ssm = ssm;
  self->active_opcode = req->opcode;
  fpi_ssm_start (ssm, done);
}

/* -------------------------------------------------------------- responses */

/* CMD_QUERY_SENSOR_INFO payload.  The 9-byte layout (a:u16, b:u16, w:u16,
 * h:u16, flags:u8) is what the Windows handler reads; it is *inferred* and the
 * first hardware run must confirm it. */
static gboolean
bf63112_parse_sensor_info (FpDeviceBf63112 *self, const guint8 *body,
                           size_t len, GError **error)
{
  guint32 pixels;

  if (len < 8)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                   "BF63112: sensor info is %zu bytes, expected >= 8", len);
      return FALSE;
    }

  self->width = (guint16) (body[4] | (body[5] << 8));
  self->height = (guint16) (body[6] | (body[7] << 8));
  self->sensor_flags = len >= 9 ? body[8] : 0;
  pixels = (guint32) self->width * self->height;

  if (self->width < 8 || self->height < 8 || pixels > 65536)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                   "BF63112: implausible geometry %ux%u (%u pixels); the "
                   "sensor-info field layout may be wrong",
                   self->width, self->height, pixels);
      return FALSE;
    }

  fp_info ("BF63112: sensor %ux%u, flags 0x%02x", self->width, self->height,
           self->sensor_flags);
  return TRUE;
}

/* CMD_QUERY_FIRMWARE_VER payload: mode, project, variant, 4 version bytes. */
static void
bf63112_parse_firmware (FpDeviceBf63112 *self, const guint8 *body, size_t len)
{
  if (len < 7)
    {
      fp_warn ("BF63112: firmware answer is %zu bytes, expected 7", len);
      return;
    }
  self->fw_mode = body[0];
  self->fw_project = body[1];
  self->fw_variant = body[2];
  memcpy (self->fw_version, body + 3, 4);
  fp_info ("BF63112: firmware %u.%u.%u.%u, mode %u, project %u, variant %u",
           self->fw_version[0], self->fw_version[1], self->fw_version[2],
           self->fw_version[3], self->fw_mode, self->fw_project,
           self->fw_variant);
}

/* --------------------------------------------------------------- FpDevice */

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
  fpi_device_open_complete (dev, NULL);
}

static void
bf63112_open_info_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  Bf63112Request *req = fpi_ssm_get_data (ssm);
  g_autoptr(GError) local_error = NULL;

  self->active_ssm = NULL;
  if (error != NULL)
    {
      fpi_device_open_complete (dev, error);
      return;
    }
  if (!bf63112_parse_sensor_info (self, req->answer, req->answer_len,
                                  &local_error))
    {
      fpi_device_open_complete (dev, g_steal_pointer (&local_error));
      return;
    }

  bf63112_request_start (dev,
                         bf63112_request_new (self,
                                              BF63112_OP_QUERY_FIRMWARE_VER,
                                              BF63112_PHASE_START, 0, NULL, 0),
                         bf63112_open_fw_done);
}

static void
bf63112_open (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  const gchar *spidev_path;
  int fd;

  spidev_path = fpi_device_get_udev_data (dev, FPI_DEVICE_UDEV_SUBTYPE_SPIDEV);
  if (spidev_path == NULL)
    {
      fpi_device_open_complete (
        dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                       "BF63112: no spidev node; the udev rule "
                                       "must bind spidev to ACPI BF63112"));
      return;
    }

  fd = open (spidev_path, O_RDWR | O_CLOEXEC);
  if (fd < 0)
    {
      fpi_device_open_complete (dev,
                                g_error_new (G_IO_ERROR,
                                             g_io_error_from_errno (errno),
                                             "BF63112: cannot open %s: %s",
                                             spidev_path, g_strerror (errno)));
      return;
    }
  self->spi_fd = fd;

  bf63112_request_start (dev,
                         bf63112_request_new (self,
                                              BF63112_OP_QUERY_SENSOR_INFO,
                                              BF63112_PHASE_START, 0, NULL, 0),
                         bf63112_open_info_done);
}

static void
bf63112_close (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);

  if (self->spi_fd >= 0)
    {
      close (self->spi_fd);
      self->spi_fd = -1;
    }
  fpi_device_close_complete (dev, NULL);
}

/*
 * Enrol / verify / identify are match-in-sensor commands: the module stores,
 * compares and protects the templates, the host only exchanges opaque handles.
 * The recovered Windows sequences are:
 *
 *   0xB1 phase 0 (start)          -> device waits for a finger
 *   0xB1 phase 1 (each press)     -> 1-byte press counter in the payload
 *   0xB1 phase 2 (fini)           -> device returns a template handle
 *   0xB1 phase 3 (cancel)
 *   0xB2 (identify)               -> module matches, returns handle + score
 *   0xB3 (remove), 0xB4 (count), 0xB6 (press detect)
 *
 * The per-phase payloads, the handle encoding, the score field and whether the
 * module requires the SM3/root-key session first are all undecoded.  Rather
 * than guess at biometric state - and risk destroying templates enrolled from
 * Windows - the actions refuse cleanly.  A store-only print is created with
 * fpi_print_set_type (print, FPI_PRINT_RAW) and
 * fpi_print_set_device_stored (print, TRUE) once the handle can be parsed.
 */
static void
bf63112_enroll (FpDevice *dev)
{
  fpi_device_enroll_complete (
    dev, NULL,
    fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                              "BF63112: enrol needs the undecoded 0xB1 "
                              "payload/handle format (see docs)"));
}

static void
bf63112_verify (FpDevice *dev)
{
  fpi_device_verify_complete (
    dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                   "BF63112: verify needs the undecoded 0xB2 "
                                   "handle/score format (see docs)"));
}

static void
bf63112_identify (FpDevice *dev)
{
  fpi_device_identify_complete (
    dev, fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                   "BF63112: identify needs the undecoded 0xB2 "
                                   "handle/score format (see docs)"));
}

static void
bf63112_suspend_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);

  (void) ssm;
  self->active_ssm = NULL;
  if (error != NULL)
    {
      /* A failed SLEEP is not fatal: the bus is idle anyway. */
      fp_warn ("BF63112: SLEEP failed: %s", error->message);
      g_error_free (error);
      error = NULL;
    }
  fpi_device_suspend_complete (dev, error);
}

static void
bf63112_suspend (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);

  bf63112_request_start (dev,
                         bf63112_request_new (self, BF63112_OP_SLEEP,
                                              BF63112_PHASE_START, 0, NULL, 0),
                         bf63112_suspend_done);
}

static void
bf63112_resume_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);
  Bf63112Request *req = fpi_ssm_get_data (ssm);
  g_autoptr(GError) local_error = NULL;

  self->active_ssm = NULL;
  if (error != NULL)
    {
      fpi_device_resume_complete (dev, error);
      return;
    }
  if (!bf63112_parse_sensor_info (self, req->answer, req->answer_len,
                                  &local_error))
    {
      fpi_device_resume_complete (dev, g_steal_pointer (&local_error));
      return;
    }
  fpi_device_resume_complete (dev, NULL);
}

static void
bf63112_resume (FpDevice *dev)
{
  FpDeviceBf63112 *self = FP_DEVICE_BF63112 (dev);

  bf63112_request_start (dev,
                         bf63112_request_new (self,
                                              BF63112_OP_QUERY_SENSOR_INFO,
                                              BF63112_PHASE_START, 0, NULL, 0),
                         bf63112_resume_done);
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
  self->spi_fd = -1;
  self->active_ssm = NULL;
}
