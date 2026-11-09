/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * fpi-ssm.h - OFFLINE SYNTAX-CHECK SHIM.  NOT the libfprint header.
 * Shaped after libfprint 1.94.x; never install or link.  See
 * bf63112_stub_prelude.h.
 */

#ifndef BF63112_STUB_FPI_SSM_H
#define BF63112_STUB_FPI_SSM_H

#include "fpi-device.h"

typedef struct _FpiSsm FpiSsm;

typedef void (*FpiSsmCompletedCallback) (FpiSsm   *ssm,
                                         FpDevice *dev,
                                         GError   *error);
typedef void (*FpiSsmHandlerCallback) (FpiSsm   *ssm,
                                       FpDevice *dev);

FpiSsm *fpi_ssm_new_full (FpDevice             *dev,
                          FpiSsmHandlerCallback handler,
                          int                   nr_states,
                          int                   start_cleanup,
                          const char           *machine_name);

#define fpi_ssm_new(dev, handler, nr_states) \
  fpi_ssm_new_full (dev, handler, nr_states, nr_states, #nr_states)

void fpi_ssm_start (FpiSsm *ssm, FpiSsmCompletedCallback callback);
void fpi_ssm_next_state (FpiSsm *machine);
void fpi_ssm_jump_to_state (FpiSsm *machine, int state);
void fpi_ssm_jump_to_state_delayed (FpiSsm *machine, int state, int delay);
void fpi_ssm_mark_completed (FpiSsm *machine);
void fpi_ssm_mark_failed (FpiSsm *machine, GError *error);
void fpi_ssm_set_data (FpiSsm *machine, gpointer data,
                       GDestroyNotify destroy);
gpointer fpi_ssm_get_data (FpiSsm *machine);
int      fpi_ssm_get_cur_state (FpiSsm *machine);

#endif /* BF63112_STUB_FPI_SSM_H */
