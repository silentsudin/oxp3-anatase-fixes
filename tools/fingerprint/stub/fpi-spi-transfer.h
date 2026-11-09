/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * fpi-spi-transfer.h - OFFLINE SYNTAX-CHECK SHIM.  NOT the libfprint header.
 * Shaped after libfprint 1.94.x; never install or link.  See
 * bf63112_stub_prelude.h.
 */

#ifndef BF63112_STUB_FPI_SPI_TRANSFER_H
#define BF63112_STUB_FPI_SPI_TRANSFER_H

#include "fpi-device.h"

typedef struct _FpiSpiTransfer FpiSpiTransfer;
typedef struct _FpiSsm         FpiSsm;

typedef void (*FpiSpiTransferCallback) (FpiSpiTransfer *transfer,
                                        FpDevice       *dev,
                                        gpointer        user_data,
                                        GError         *error);

struct _FpiSpiTransfer
{
  FpDevice *device;
  FpiSsm   *ssm;

  gssize  length_wr;
  gssize  length_rd;
  guchar *buffer_wr;
  guchar *buffer_rd;

  int spidev_fd;
};

FpiSpiTransfer *fpi_spi_transfer_new (FpDevice *device, int spidev_fd);

void fpi_spi_transfer_write_full (FpiSpiTransfer *transfer,
                                  guint8         *buffer,
                                  gsize           length,
                                  GDestroyNotify  free_func);
void fpi_spi_transfer_read_full (FpiSpiTransfer *transfer,
                                 guint8         *buffer,
                                 gsize           length,
                                 GDestroyNotify  free_func);
void fpi_spi_transfer_submit (FpiSpiTransfer         *transfer,
                              GCancellable           *cancellable,
                              FpiSpiTransferCallback  callback,
                              gpointer                user_data);

#endif /* BF63112_STUB_FPI_SPI_TRANSFER_H */
