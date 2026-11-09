/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_stub_prelude.h - OFFLINE SYNTAX-CHECK SHIM.  NOT GLib, NOT libfprint.
 *
 * This header exists only so `make syntax` can type-check
 * tools/fingerprint/libfprint-bf63112.c on a machine that has neither
 * libfprint's internal headers nor GLib development files (the ONEXPLAYER 3
 * host is exactly such a machine).  It declares the small API surface the
 * driver uses and nothing else; the declarations are compatible in shape with
 * libfprint 1.94.x but are *not* the real ABI and must never be installed,
 * linked or shipped.  The authoritative check is `make syntax-real` against a
 * libfprint source tree (see tools/fingerprint/README.md).
 */

#ifndef BF63112_STUB_PRELUDE_H
#define BF63112_STUB_PRELUDE_H

#include <stddef.h>
#include <stdint.h>

typedef int gboolean;
typedef int gint;
typedef unsigned int guint;
typedef unsigned char guint8;
typedef unsigned short guint16;
typedef unsigned int guint32;
typedef unsigned long long guint64;
typedef long long gint64;
typedef long gssize;
typedef unsigned long gsize;
typedef char gchar;
typedef unsigned char guchar;
typedef void *gpointer;
typedef unsigned long GType;
typedef unsigned long GQuark;
typedef unsigned int GIOErrorEnum;

typedef void (*GDestroyNotify) (gpointer data);

typedef struct _GError
{
  GQuark domain;
  gint   code;
  gchar *message;
} GError;

typedef struct _GCancellable GCancellable;

typedef struct _GObject
{
  int stub_parent;
} GObject;

typedef struct _GObjectClass
{
  int stub_parent_class;
} GObjectClass;

typedef struct _GTask GTask;
typedef struct _GAsyncResult GAsyncResult;

typedef void (*GTaskThreadFunc) (GTask        *task,
                                 gpointer      source_object,
                                 gpointer      task_data,
                                 GCancellable *cancellable);
typedef void (*GAsyncReadyCallback) (GObject      *source_object,
                                     GAsyncResult *res,
                                     gpointer      user_data);

typedef enum
{
  G_IO_ERROR_FAILED = 0,
  G_IO_ERROR_NOT_FOUND,
  G_IO_ERROR_INVALID_DATA,
  G_IO_ERROR_CANCELLED,
  G_IO_ERROR_NOT_SUPPORTED,
  G_IO_ERROR_INVALID_ARGUMENT,
} StubGIOError;

#define TRUE  1
#define FALSE 0

#define g_new0(T, N) ((T *) g_malloc0 (sizeof (T) * (gsize) (N)))

#define g_assert(expr)          ((void) 0)
#define g_assert_not_reached()  ((void) 0)
#define g_return_if_fail(expr)  ((void) 0)
#define g_warn_if_fail(expr)    ((void) 0)

/* autocleanup is a no-op here; g_steal_pointer just yields the pointer */
#define g_autoptr(T) T *
#define g_autofree
#define g_steal_pointer(pp) (*(pp))

#define G_OBJECT(obj) ((GObject *) (obj))
#define G_TASK(obj)   ((GTask *) (obj))

gpointer g_malloc (gsize size);
gpointer g_malloc0 (gsize size);
void     g_free (gpointer mem);
void     g_set_error (GError **error, GQuark domain, gint code,
                      const gchar *format, ...);
GError  *g_error_new (GQuark domain, gint code, const gchar *format, ...);
void     g_error_free (GError *error);
GError  *g_error_copy (const GError *error);
gboolean g_error_matches (const GError *error, GQuark domain, gint code);
const gchar *g_strerror (gint errnum);
GIOErrorEnum g_io_error_from_errno (gint errnum);
GQuark g_io_error_quark (void);

#define G_IO_ERROR (g_io_error_quark ())

/* GLib/GIO calls the driver uses for the async transfer and configuration. */
gpointer g_object_ref (gpointer object);
void     g_object_unref (gpointer object);
GTask   *g_task_new (gpointer           source_object,
                     GCancellable      *cancellable,
                     GAsyncReadyCallback callback,
                     gpointer           user_data);
void     g_task_set_check_cancellable (GTask *task, gboolean check);
void     g_task_set_return_on_cancel (GTask *task, gboolean value);
void     g_task_set_task_data (GTask *task, gpointer task_data,
                               GDestroyNotify task_data_destroy);
void     g_task_run_in_thread (GTask *task, GTaskThreadFunc task_func);
void     g_task_return_int (GTask *task, gssize result);
gssize   g_task_propagate_int (GTask *task, GError **error);
gpointer g_task_get_task_data (GTask *task);

gboolean g_cancellable_is_cancelled (GCancellable *cancellable);
void     g_atomic_int_inc (gint *atomic);
gboolean g_atomic_int_dec_and_test (gint *atomic);
GError  *g_error_new_literal (GQuark domain, gint code, const gchar *message);

const gchar *g_getenv (const gchar *variable);
gboolean g_file_get_contents (const gchar *filename, gchar **contents,
                              gsize *length, GError **error);
gsize    g_strlcpy (gchar *dest, const gchar *src, gsize dest_size);
gint64   g_ascii_strtoll (const gchar *nptr, gchar **endptr, guint base);
gint     g_ascii_strcasecmp (const gchar *s1, const gchar *s2);

#endif /* BF63112_STUB_PRELUDE_H */
