/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * fpi-device.h - OFFLINE SYNTAX-CHECK SHIM.  NOT the libfprint header.
 *
 * Only the declarations libfprint-bf63112.c uses, shaped after libfprint
 * 1.94.x (v1.94.9).  Never install or link this.  See bf63112_stub_prelude.h.
 */

#ifndef BF63112_STUB_FPI_DEVICE_H
#define BF63112_STUB_FPI_DEVICE_H

#include "bf63112_stub_prelude.h"

typedef struct _FpDevice       FpDevice;
typedef struct _FpDeviceClass  FpDeviceClass;
typedef struct _FpPrint        FpPrint;
typedef struct _FpIdEntry      FpIdEntry;

typedef enum
{
  FP_DEVICE_TYPE_VIRTUAL,
  FP_DEVICE_TYPE_UDEV,
  FP_DEVICE_TYPE_USB,
} FpDeviceType;

typedef enum
{
  FP_SCAN_TYPE_SWIPE,
  FP_SCAN_TYPE_PRESS,
} FpScanType;

typedef enum
{
  FP_DEVICE_ERROR_GENERAL,
  FP_DEVICE_ERROR_NOT_SUPPORTED,
  FP_DEVICE_ERROR_NOT_OPEN,
  FP_DEVICE_ERROR_ALREADY_OPEN,
  FP_DEVICE_ERROR_BUSY,
  FP_DEVICE_ERROR_PROTO,
} FpDeviceError;

typedef enum
{
  FPI_DEVICE_UDEV_SUBTYPE_SPIDEV = 1 << 0,
  FPI_DEVICE_UDEV_SUBTYPE_HIDRAW = 1 << 1,
} FpiDeviceUdevSubtypeFlags;

struct _FpIdEntry
{
  union
  {
    struct
    {
      guint pid;
      guint vid;
    };
    const gchar *virtual_envvar;
    struct
    {
      FpiDeviceUdevSubtypeFlags udev_types;
      const gchar              *spi_acpi_id;
      struct
      {
        guint pid;
        guint vid;
      } hid_id;
    };
  };
  unsigned long long driver_data;
};

struct _FpDevice
{
  GObject parent;
};

struct _FpDeviceClass
{
  GObjectClass parent_class;

  const gchar     *id;
  const gchar     *full_name;
  FpDeviceType     type;
  const FpIdEntry *id_table;
  guint            features;
  gint             nr_enroll_stages;
  FpScanType       scan_type;

  void (*open)     (FpDevice *device);
  void (*close)    (FpDevice *device);
  void (*enroll)   (FpDevice *device);
  void (*verify)   (FpDevice *device);
  void (*identify) (FpDevice *device);
  void (*suspend)  (FpDevice *device);
  void (*resume)   (FpDevice *device);
};

/* The parts of glib's G_DECLARE_FINAL_TYPE that the driver needs. */
#define G_DECLARE_FINAL_TYPE(TN, tn, NS, NAME, PARENT)                        \
  typedef struct _##TN TN;                                                    \
  typedef struct _##TN##Class TN##Class;                                      \
  GType tn##_get_type (void);                                                 \
  static inline TN *NS##_##NAME (void *ptr) { return (TN *) ptr; }            \
  static inline gboolean NS##_IS_##NAME (void *ptr) { return ptr != NULL; }

#define G_DEFINE_TYPE(TN, tn, PARENT) /* stub: no GType registration */

#define FP_TYPE_DEVICE 1
#define FP_DEVICE_CLASS(klass) ((FpDeviceClass *) (klass))

GError *fpi_device_error_new (FpDeviceError error);
GError *fpi_device_error_new_msg (FpDeviceError error, const gchar *msg, ...);

gpointer      fpi_device_get_udev_data (FpDevice *device,
                                        FpiDeviceUdevSubtypeFlags subtype);
GCancellable *fpi_device_get_cancellable (FpDevice *device);
void          fpi_device_class_auto_initialize_features (FpDeviceClass *device_class);

void fpi_device_open_complete (FpDevice *device, GError *error);
void fpi_device_close_complete (FpDevice *device, GError *error);
void fpi_device_enroll_complete (FpDevice *device, FpPrint *print,
                                 GError *error);
void fpi_device_verify_complete (FpDevice *device, GError *error);
void fpi_device_identify_complete (FpDevice *device, GError *error);
void fpi_device_suspend_complete (FpDevice *device, GError *error);
void fpi_device_resume_complete (FpDevice *device, GError *error);

#endif /* BF63112_STUB_FPI_DEVICE_H */
