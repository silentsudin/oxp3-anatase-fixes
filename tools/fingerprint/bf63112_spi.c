/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_spi.c - dependency-free BF63112 SPI (ACPI/SPB) transport.
 *
 * See bf63112_spi.h for the wire shape and the safety model.  This file is a
 * C mirror of tools/fingerprint/fpbf63112/spi.py; it uses libc/syscalls only
 * (no libgpiod, no libfprint, no spidev helper).  Optional libgpiod v2/v1
 * backends are compiled only when BF63112_HAVE_GPIOD_V2/_V1 is defined.
 *
 * Nothing here is claimed to have run against the sensor.  The unit test
 * (tests/test_spi.c) drives the whole file through an injected I/O vtable and
 * a fake clock, so it never opens /dev/spidev or any GPIO.
 */

#include "bf63112_spi.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#if defined(BF63112_HAVE_GPIOD_V2) || defined(BF63112_HAVE_GPIOD_V1)
#include <gpiod.h>
#endif

struct bf63112_spi_ioc_transfer
{
  uint64_t tx_buf;
  uint64_t rx_buf;
  uint32_t len;
  uint32_t speed_hz;
  uint16_t delay_usecs;
  uint8_t  bits_per_word;
  uint8_t  cs_change;
  uint8_t  tx_nbits;
  uint8_t  rx_nbits;
  uint8_t  word_delay_usecs;
  uint8_t  pad;
};

/* 32 bytes is what SPI_IOC_MESSAGE(n) encodes; a change here breaks the ABI. */
_Static_assert (sizeof (struct bf63112_spi_ioc_transfer) == 32,
                "spi_ioc_transfer must be 32 bytes");

/* ------------------------------------------------------------- small utils */

static uint64_t
default_now_ms (void *ctx)
{
  struct timespec ts;

  (void) ctx;
  clock_gettime (CLOCK_MONOTONIC, &ts);
  return (uint64_t) ts.tv_sec * 1000u + (uint64_t) ts.tv_nsec / 1000000u;
}

static void
default_sleep_ms (unsigned int ms, void *ctx)
{
  struct timespec ts;

  (void) ctx;
  ts.tv_sec = (time_t) (ms / 1000u);
  ts.tv_nsec = (long) (ms % 1000u) * 1000000L;
  while (nanosleep (&ts, &ts) < 0 && errno == EINTR)
    ;
}

static uint64_t
spi_now_ms (struct bf63112_spi *spi)
{
  uint64_t (*now) (void *) = spi->now_ms ? spi->now_ms : default_now_ms;

  return now (spi->now_ctx);
}

static void
spi_sleep_ms (struct bf63112_spi *spi, unsigned int ms)
{
  void (*sleep_ms) (unsigned int, void *) =
    spi->sleep_ms ? spi->sleep_ms : default_sleep_ms;

  if (ms > 0)
    sleep_ms (ms, spi->sleep_ctx);
}

static int
neg_errno (void)
{
  return errno != 0 ? -errno : BF63112_SPI_ERR_IO;
}

static int
write_all (int fd, const char *text)
{
  size_t left = strlen (text);

  while (left > 0)
    {
      ssize_t written = write (fd, text, left);

      if (written < 0)
        {
          if (errno == EINTR)
            continue;
          return neg_errno ();
        }
      text += written;
      left -= (size_t) written;
    }
  return 0;
}

static int
write_sysfs (const char *path, const char *text)
{
  int fd = open (path, O_WRONLY | O_CLOEXEC);
  int rc;

  if (fd < 0)
    return neg_errno ();
  rc = write_all (fd, text);
  if (close (fd) < 0 && rc == 0)
    rc = neg_errno ();
  return rc;
}

static int
write_sysfs_int (const char *path, int value)
{
  char buf[24];

  snprintf (buf, sizeof buf, "%d", value);
  return write_sysfs (path, buf);
}

static int
read_sysfs_int (const char *path)
{
  char buf[32];
  int fd = open (path, O_RDONLY | O_CLOEXEC);
  ssize_t got;

  if (fd < 0)
    return neg_errno ();
  do
    got = read (fd, buf, sizeof buf - 1);
  while (got < 0 && errno == EINTR);
  close (fd);
  if (got < 0)
    return neg_errno ();
  if (got == 0)
    return BF63112_SPI_ERR_IO;
  buf[got] = '\0';
  if (buf[0] != '0' && buf[0] != '1')
    return BF63112_SPI_ERR_IO;
  return buf[0] == '1' ? 1 : 0;
}

static bool
path_is_dir (const char *path)
{
  int fd = open (path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);

  if (fd < 0)
    return false;
  close (fd);
  return true;
}

static bool
wait_for_path (const char *path, unsigned int timeout_ms)
{
  unsigned int slept = 0;

  while (!path_is_dir (path))
    {
      struct timespec ts = { 0, 10 * 1000 * 1000L };

      if (slept >= timeout_ms)
        return false;
      while (nanosleep (&ts, &ts) < 0 && errno == EINTR)
        ;
      slept += 10;
    }
  return true;
}

/* ---------------------------------------------------------------- selector */

uint8_t
bf63112_spi_selector (uint8_t opcode, bool write)
{
  uint8_t value = (uint8_t) ((opcode & 0x3Fu) << 1);

  return write ? (uint8_t) (value | 0x80u) : value;
}

uint8_t
bf63112_spi_selector_low (uint8_t opcode)
{
  return (uint8_t) (bf63112_spi_selector (opcode, false) & 0x7Fu);
}

struct destructive_op
{
  uint8_t     opcode;
  const char *name;
};

static const struct destructive_op destructive_ops[] = {
  { 0xB0, "ENROLL_FINGERPRINT" },
  { 0xB1, "IDENTIFY_FINGERPRINT" },
  { 0xB2, "REMOVE_FINGERPRINT" },
  { 0xC2, "JTAG_CONTROL" },
  { 0xC3, "RESTORE_MCU" },
  { 0xD0, "UPDATE_FIRMWARE" },
  { 0xD1, "FW_WORK_MODE_SWITCH" },
  { 0xFF, "FW_WORK_MODE_SWITCH" },
};

#define DESTRUCTIVE_OP_COUNT \
  (sizeof destructive_ops / sizeof destructive_ops[0])

bool
bf63112_spi_opcode_is_destructive (uint8_t opcode)
{
  size_t i;

  for (i = 0; i < DESTRUCTIVE_OP_COUNT; i++)
    if (destructive_ops[i].opcode == opcode)
      return true;
  return false;
}

const char *
bf63112_spi_destructive_name (uint8_t opcode)
{
  size_t i;

  for (i = 0; i < DESTRUCTIVE_OP_COUNT; i++)
    if (destructive_ops[i].opcode == opcode)
      return destructive_ops[i].name;
  return NULL;
}

bool
bf63112_spi_selector_is_forbidden (uint8_t selector)
{
  size_t i;
  uint8_t low = (uint8_t) (selector & 0x7Fu);

  for (i = 0; i < DESTRUCTIVE_OP_COUNT; i++)
    if (bf63112_spi_selector_low (destructive_ops[i].opcode) == low)
      return true;
  return false;
}

const char *
bf63112_spi_selector_aliases (uint8_t selector)
{
  static char buf[192];
  size_t i, used = 0;
  uint8_t low = (uint8_t) (selector & 0x7Fu);

  buf[0] = '\0';
  for (i = 0; i < DESTRUCTIVE_OP_COUNT; i++)
    {
      int written;

      if (bf63112_spi_selector_low (destructive_ops[i].opcode) != low)
        continue;
      written = snprintf (buf + used, sizeof buf - used, "%s0x%02x %s",
                          used ? ", " : "", destructive_ops[i].opcode,
                          destructive_ops[i].name);
      if (written < 0 || (size_t) written >= sizeof buf - used)
        break;
      used += (size_t) written;
    }
  return used ? buf : NULL;
}

/* ---------------------------------------------------------------- policy */

void
bf63112_spi_policy_init (struct bf63112_spi_policy *policy)
{
  if (policy == NULL)
    return;
  memset (policy, 0, sizeof *policy);
  policy->allow_io = false;
  bf63112_spi_policy_allow_read (policy, BF63112_SPI_OP_FIRMWARE_VER);
  bf63112_spi_policy_allow_read (policy, BF63112_SPI_OP_MODULE_SCHEME);
  bf63112_spi_policy_allow_read (policy, BF63112_SPI_OP_FINGER_COUNT);
  bf63112_spi_policy_allow_read (policy, BF63112_SPI_OP_MODULE_UID);
  bf63112_spi_policy_allow_read (policy, BF63112_SPI_OP_SENSOR_INFO);
  bf63112_spi_policy_allow_read (policy, BF63112_SPI_OP_STATUS_REG);
}

static void
allow_op (uint64_t bitmap[4], uint8_t opcode)
{
  bitmap[opcode >> 6] |= 1ULL << (opcode & 63u);
}

static bool
op_allowed (const uint64_t bitmap[4], uint8_t opcode)
{
  return (bitmap[opcode >> 6] & (1ULL << (opcode & 63u))) != 0;
}

void
bf63112_spi_policy_allow_read (struct bf63112_spi_policy *policy,
                               uint8_t opcode)
{
  if (policy != NULL)
    allow_op (policy->read_allow, opcode);
}

void
bf63112_spi_policy_allow_write (struct bf63112_spi_policy *policy,
                                uint8_t opcode)
{
  if (policy != NULL)
    allow_op (policy->write_allow, opcode);
}

bool
bf63112_spi_policy_read_allowed (const struct bf63112_spi_policy *policy,
                                 uint8_t opcode)
{
  return policy != NULL && op_allowed (policy->read_allow, opcode);
}

bool
bf63112_spi_policy_write_allowed (const struct bf63112_spi_policy *policy,
                                  uint8_t opcode)
{
  return policy != NULL && op_allowed (policy->write_allow, opcode);
}

int
bf63112_spi_policy_check (const struct bf63112_spi_policy *policy,
                          uint8_t opcode, bool write)
{
  if (policy == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (bf63112_spi_opcode_is_destructive (opcode))
    return BF63112_SPI_ERR_REFUSED;
  if (bf63112_spi_selector_is_forbidden (bf63112_spi_selector_low (opcode)))
    return BF63112_SPI_ERR_REFUSED;
  if (!policy->allow_io)
    return BF63112_SPI_ERR_NOT_ALLOWED;
  if (write)
    return bf63112_spi_policy_write_allowed (policy, opcode)
           ? 0 : BF63112_SPI_ERR_NOT_ALLOWED;
  return bf63112_spi_policy_read_allowed (policy, opcode)
         ? 0 : BF63112_SPI_ERR_NOT_ALLOWED;
}

/* ---------------------------------------------------------------- plans */

void
bf63112_spi_default_timeouts (uint8_t opcode,
                              uint16_t *timeout_ms, uint16_t *total_ms)
{
  uint16_t per = (uint16_t) BF63112_SPI_CMD_TIMEOUT_MS;
  uint16_t total = (uint16_t) BF63112_SPI_CMD_TOTAL_MS;

  if (opcode == 0xB4)   /* GET_IMG_DATA */
    {
      per = (uint16_t) BF63112_SPI_IMAGE_TIMEOUT_MS;
      total = (uint16_t) BF63112_SPI_IMAGE_TOTAL_MS;
    }
  else if (opcode == 0xC3)   /* RESTORE_MCU */
    {
      per = (uint16_t) BF63112_SPI_RESTORE_TIMEOUT_MS;
      total = (uint16_t) BF63112_SPI_RESTORE_TOTAL_MS;
    }
  if (timeout_ms != NULL)
    *timeout_ms = per;
  if (total_ms != NULL)
    *total_ms = total;
}

static int
resolve_timeouts (uint8_t opcode, uint16_t timeout_ms, uint16_t total_ms,
                  uint16_t *out_timeout, uint16_t *out_total)
{
  if (timeout_ms == 0 || total_ms == 0)
    bf63112_spi_default_timeouts (opcode, out_timeout, out_total);
  else
    {
      *out_timeout = timeout_ms;
      *out_total = total_ms;
    }
  if (*out_timeout == 0 || *out_total == 0 || *out_total < *out_timeout)
    return BF63112_SPI_ERR_INVALID;
  return 0;
}

int
bf63112_spi_build_read (const struct bf63112_spi_policy *policy,
                        uint8_t opcode, uint16_t read_len,
                        uint16_t timeout_ms, uint16_t total_ms,
                        struct bf63112_spi_plan *plan)
{
  int rc;

  if (plan == NULL || read_len == 0)
    return BF63112_SPI_ERR_INVALID;
  rc = bf63112_spi_policy_check (policy, opcode, false);
  if (rc != 0)
    return rc;
  memset (plan, 0, sizeof *plan);
  plan->opcode = opcode;
  plan->write = false;
  plan->read_len = read_len;
  rc = resolve_timeouts (opcode, timeout_ms, total_ms,
                         &plan->timeout_ms, &plan->total_ms);
  if (rc != 0)
    return rc;
  plan->selector = bf63112_spi_selector (opcode, false);
  plan->selector_byte = plan->selector;
  plan->transfers[0].tx = &plan->selector_byte;
  plan->transfers[0].tx_len = 1;
  plan->transfers[0].rx_len = 0;
  plan->transfers[1].tx = NULL;
  plan->transfers[1].tx_len = 0;
  plan->transfers[1].rx = NULL;
  plan->transfers[1].rx_len = read_len;
  plan->n_transfers = 2;
  return bf63112_spi_plan_validate (plan);
}

int
bf63112_spi_build_write (const struct bf63112_spi_policy *policy,
                         uint8_t opcode, const uint8_t *payload,
                         size_t payload_len,
                         uint16_t timeout_ms, uint16_t total_ms,
                         struct bf63112_spi_plan *plan)
{
  int rc;

  if (plan == NULL || (payload == NULL && payload_len > 0))
    return BF63112_SPI_ERR_INVALID;
  if (payload_len > 0xFFFFFFFFu)
    return BF63112_SPI_ERR_INVALID;
  rc = bf63112_spi_policy_check (policy, opcode, true);
  if (rc != 0)
    return rc;
  memset (plan, 0, sizeof *plan);
  plan->opcode = opcode;
  plan->write = true;
  plan->read_len = 0;
  rc = resolve_timeouts (opcode, timeout_ms, total_ms,
                         &plan->timeout_ms, &plan->total_ms);
  if (rc != 0)
    return rc;
  plan->selector = bf63112_spi_selector (opcode, true);
  plan->selector_byte = plan->selector;
  plan->transfers[0].tx = &plan->selector_byte;
  plan->transfers[0].tx_len = 1;
  plan->transfers[0].rx_len = 0;
  plan->n_transfers = 1;
  if (payload_len > 0)
    {
      plan->transfers[1].tx = payload;
      plan->transfers[1].tx_len = payload_len;
      plan->transfers[1].rx_len = 0;
      plan->n_transfers = 2;
    }
  return bf63112_spi_plan_validate (plan);
}

int
bf63112_spi_plan_validate (const struct bf63112_spi_plan *plan)
{
  if (plan == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (plan->n_transfers < 1 || plan->n_transfers > BF63112_SPI_MAX_TRANSFERS)
    return BF63112_SPI_ERR_INVALID;
  if (plan->selector != bf63112_spi_selector (plan->opcode, plan->write))
    return BF63112_SPI_ERR_INVALID;
  if (plan->transfers[0].tx == NULL || plan->transfers[0].tx_len != 1 ||
      plan->transfers[0].tx[0] != plan->selector ||
      plan->transfers[0].rx_len != 0)
    return BF63112_SPI_ERR_INVALID;
  if (plan->timeout_ms == 0 || plan->total_ms < plan->timeout_ms)
    return BF63112_SPI_ERR_INVALID;
  if (plan->write)
    {
      if (plan->read_len != 0)
        return BF63112_SPI_ERR_INVALID;
      if (plan->n_transfers == 2 && plan->transfers[1].tx_len == 0)
        return BF63112_SPI_ERR_INVALID;
      if (plan->transfers[0].rx_len || plan->transfers[1].rx_len)
        return BF63112_SPI_ERR_INVALID;
    }
  else
    {
      if (plan->n_transfers != 2 || plan->read_len == 0)
        return BF63112_SPI_ERR_INVALID;
      if (plan->transfers[1].tx != NULL || plan->transfers[1].tx_len != 0 ||
          plan->transfers[1].rx_len != plan->read_len)
        return BF63112_SPI_ERR_INVALID;
    }
  return 0;
}

enum bf63112_spi_deadline
bf63112_spi_deadline_check (const struct bf63112_spi_plan *plan,
                            uint64_t elapsed_ms)
{
  if (plan == NULL)
    return BF63112_SPI_DEADLINE_OK;
  if (elapsed_ms > plan->total_ms)
    return BF63112_SPI_DEADLINE_TOTAL;
  if (elapsed_ms > plan->timeout_ms)
    return BF63112_SPI_DEADLINE_ATTEMPT;
  return BF63112_SPI_DEADLINE_OK;
}

int64_t
bf63112_spi_irq_budget (const struct bf63112_spi_plan *plan,
                        uint64_t elapsed_ms)
{
  uint64_t limit;

  if (plan == NULL)
    return 0;
  limit = plan->timeout_ms < plan->total_ms ? plan->timeout_ms
                                            : plan->total_ms;
  return (int64_t) limit - (int64_t) elapsed_ms;
}

static int
plan_deadline_rc (const struct bf63112_spi_plan *plan, uint64_t elapsed_ms)
{
  switch (bf63112_spi_deadline_check (plan, elapsed_ms))
    {
    case BF63112_SPI_DEADLINE_TOTAL:
    case BF63112_SPI_DEADLINE_ATTEMPT:
      return BF63112_SPI_ERR_TIMEOUT;
    case BF63112_SPI_DEADLINE_OK:
    default:
      return 0;
    }
}

/* ------------------------------------------------------------- linux spidev */

static int
linux_spi_open (void *priv)
{
  struct bf63112_linux_spi *state = priv;
  int fd;

  if (state == NULL || state->path[0] == '\0')
    return BF63112_SPI_ERR_INVALID;
  if (state->fd >= 0)
    return 0;
  fd = open (state->path, O_RDWR | O_CLOEXEC);
  if (fd < 0)
    return neg_errno ();
  state->fd = fd;
  return 0;
}

static int
linux_spi_close (void *priv)
{
  struct bf63112_linux_spi *state = priv;
  int rc = 0;

  if (state == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (state->fd >= 0)
    {
      if (close (state->fd) < 0)
        rc = neg_errno ();
      state->fd = -1;
    }
  return rc;
}

static int
linux_spi_configure (void *priv, int speed_hz, int mode, int bits_per_word)
{
  struct bf63112_linux_spi *state = priv;

  if (state == NULL || state->fd < 0)
    return BF63112_SPI_ERR_INVALID;
  if (mode >= 0)
    {
      uint8_t value = (uint8_t) (mode & 0xFF);

      if (ioctl (state->fd, BF63112_SPI_IOC_WR_MODE, &value) < 0)
        return neg_errno ();
    }
  if (bits_per_word >= 0)
    {
      uint8_t value = (uint8_t) (bits_per_word & 0xFF);

      if (ioctl (state->fd, BF63112_SPI_IOC_WR_BITS_PER_WORD, &value) < 0)
        return neg_errno ();
    }
  if (speed_hz >= 0)
    {
      uint32_t value = (uint32_t) speed_hz;

      if (ioctl (state->fd, BF63112_SPI_IOC_WR_MAX_SPEED_HZ, &value) < 0)
        return neg_errno ();
    }
  return 0;
}

static int
linux_spi_message (void *priv,
                   const struct bf63112_spi_transfer *transfers,
                   size_t n_transfers, size_t *bytes_done)
{
  struct bf63112_linux_spi *state = priv;
  struct bf63112_spi_ioc_transfer xfer[BF63112_SPI_MAX_TRANSFERS];
  size_t i;
  int rc;

  if (state == NULL || state->fd < 0 || transfers == NULL ||
      n_transfers == 0 || n_transfers > BF63112_SPI_MAX_TRANSFERS)
    return BF63112_SPI_ERR_INVALID;
  memset (xfer, 0, sizeof xfer);
  for (i = 0; i < n_transfers; i++)
    {
      size_t len = transfers[i].tx_len > transfers[i].rx_len
                   ? transfers[i].tx_len : transfers[i].rx_len;

      if (len == 0 || len > 0xFFFFFFFFu)
        return BF63112_SPI_ERR_INVALID;
      xfer[i].tx_buf = (uint64_t) (uintptr_t) transfers[i].tx;
      xfer[i].rx_buf = (uint64_t) (uintptr_t) transfers[i].rx;
      xfer[i].len = (uint32_t) len;
      xfer[i].speed_hz = transfers[i].speed_hz;
      xfer[i].delay_usecs = transfers[i].delay_us;
    }
  rc = ioctl (state->fd, BF63112_SPI_IOC_MESSAGE (n_transfers), xfer);
  if (rc < 0)
    return neg_errno ();
  if (bytes_done != NULL)
    *bytes_done = (size_t) rc;
  return 0;
}

static const struct bf63112_spi_io linux_spi_ops = {
  .open = linux_spi_open,
  .close = linux_spi_close,
  .configure = linux_spi_configure,
  .message = linux_spi_message,
};

int
bf63112_spi_linux_init (struct bf63112_spi *spi,
                        struct bf63112_linux_spi *state,
                        const char *path)
{
  size_t len;

  if (spi == NULL || state == NULL || path == NULL || *path == '\0')
    return BF63112_SPI_ERR_INVALID;
  len = strlen (path);
  if (len >= sizeof state->path)
    return BF63112_SPI_ERR_INVALID;
  memset (state, 0, sizeof *state);
  memcpy (state->path, path, len + 1);
  state->fd = -1;
  bf63112_spi_init (spi, &linux_spi_ops, state);
  return 0;
}

/* --------------------------------------------------------------- sysfs IRQ */

static const char *
edge_name (int edge)
{
  switch (edge)
    {
    case BF63112_SPI_IRQ_FALLING:
      return "falling";
    case BF63112_SPI_IRQ_BOTH:
      return "both";
    case BF63112_SPI_IRQ_RISING:
    default:
      return "rising";
    }
}

static int
sysfs_irq_open (struct bf63112_spi_irq *irq)
{
  struct bf63112_sysfs_irq *state = irq->priv;
  char path[320];
  int rc;

  if (state == NULL || state->line < 0)
    return BF63112_SPI_ERR_INVALID;
  if (state->value_fd >= 0)
    return 0;
  if (!path_is_dir (state->base))
    return BF63112_SPI_ERR_IO;
  snprintf (path, sizeof path, "%s/gpio%d", state->base, state->line);
  if (!path_is_dir (path))
    {
      snprintf (path, sizeof path, "%s/export", state->base);
      rc = write_sysfs_int (path, state->line);
      if (rc != 0)
        return rc;
      state->exported_here = true;
      snprintf (path, sizeof path, "%s/gpio%d", state->base, state->line);
      if (!wait_for_path (path, 500))
        return BF63112_SPI_ERR_TIMEOUT;
    }
  snprintf (path, sizeof path, "%s/gpio%d/direction", state->base,
            state->line);
  rc = write_sysfs (path, "in");
  if (rc != 0)
    return rc;
  snprintf (path, sizeof path, "%s/gpio%d/edge", state->base, state->line);
  rc = write_sysfs (path, edge_name (state->edge));
  if (rc != 0)
    return rc;

  state->hw_active_low = false;
  snprintf (path, sizeof path, "%s/gpio%d/active_low", state->base,
            state->line);
  if (access (path, F_OK) == 0)
    {
      rc = write_sysfs (path, state->active_low ? "1" : "0");
      if (rc == 0)
        state->hw_active_low = true;
    }

  snprintf (path, sizeof path, "%s/gpio%d/value", state->base, state->line);
  state->value_fd = open (path, O_RDONLY | O_CLOEXEC);
  if (state->value_fd < 0)
    return neg_errno ();
  state->last_value = read_sysfs_int (path);
  return state->last_value < 0 ? state->last_value : 0;
}

static int
sysfs_irq_wait (struct bf63112_spi_irq *irq, unsigned int timeout_ms,
                struct bf63112_spi_irq_event *event)
{
  struct bf63112_sysfs_irq *state = irq->priv;
  struct pollfd pfd;
  char path[320];
  int raw, value, rc;
  uint64_t started;

  if (state == NULL || state->value_fd < 0 || event == NULL)
    return BF63112_SPI_ERR_INVALID;
  memset (event, 0, sizeof *event);
  event->source = BF63112_SPI_IRQ_SOURCE_SYSFS;
  event->edge = -1;
  event->value = -1;
  event->observed = false;

  started = default_now_ms (NULL);
  pfd.fd = state->value_fd;
  pfd.events = POLLPRI | POLLERR;
  pfd.revents = 0;
  do
    rc = poll (&pfd, 1, (int) timeout_ms);
  while (rc < 0 && errno == EINTR);
  event->elapsed_ms = default_now_ms (NULL) - started;
  if (rc < 0)
    return neg_errno ();
  if (rc == 0 || pfd.revents == 0)
    return BF63112_SPI_ERR_TIMEOUT;

  snprintf (path, sizeof path, "%s/gpio%d/value", state->base, state->line);
  raw = read_sysfs_int (path);
  if (raw < 0)
    return raw;
  value = raw;
  if (state->active_low && !state->hw_active_low)
    value = 1 - value;

  event->observed = true;
  event->value = value;
  if (value != state->last_value)
    event->edge = value > state->last_value ? 1 : 0;
  state->last_value = value;
  return 0;
}

static void
sysfs_irq_close (struct bf63112_spi_irq *irq)
{
  struct bf63112_sysfs_irq *state = irq->priv;
  char path[320];

  if (state == NULL)
    return;
  if (state->value_fd >= 0)
    {
      close (state->value_fd);
      state->value_fd = -1;
    }
  if (state->exported_here)
    {
      snprintf (path, sizeof path, "%s/unexport", state->base);
      (void) write_sysfs_int (path, state->line);
      state->exported_here = false;
    }
}

int
bf63112_spi_irq_sysfs_init (struct bf63112_spi_irq *irq,
                            struct bf63112_sysfs_irq *state,
                            int line, int edge, bool active_low,
                            const char *sysfs_base)
{
  size_t len;

  if (irq == NULL || state == NULL || line < 0 ||
      (edge != BF63112_SPI_IRQ_RISING && edge != BF63112_SPI_IRQ_FALLING &&
       edge != BF63112_SPI_IRQ_BOTH))
    return BF63112_SPI_ERR_INVALID;
  if (sysfs_base == NULL)
    return BF63112_SPI_ERR_INVALID;
  len = strlen (sysfs_base);
  if (len >= sizeof state->base)
    return BF63112_SPI_ERR_INVALID;
  memset (state, 0, sizeof *state);
  memcpy (state->base, sysfs_base, len + 1);
  state->line = line;
  state->edge = edge;
  state->active_low = active_low;
  state->value_fd = -1;
  state->last_value = -1;
  irq->open = sysfs_irq_open;
  irq->wait = sysfs_irq_wait;
  irq->close = sysfs_irq_close;
  irq->priv = state;
  return 0;
}

/* ---------------------------------------------------------------- poll IRQ */

static int
poll_irq_open (struct bf63112_spi_irq *irq)
{
  (void) irq;
  return 0;
}

static int
poll_irq_wait (struct bf63112_spi_irq *irq, unsigned int timeout_ms,
               struct bf63112_spi_irq_event *event)
{
  struct bf63112_poll_irq *state = irq->priv;

  if (state == NULL || event == NULL)
    return BF63112_SPI_ERR_INVALID;
  memset (event, 0, sizeof *event);
  event->source = BF63112_SPI_IRQ_SOURCE_POLL;
  event->observed = false;
  event->edge = -1;
  event->value = -1;
  event->elapsed_ms = timeout_ms;
  if (state->sleep_ms != NULL)
    state->sleep_ms (timeout_ms, state->sleep_ctx);
  else
    default_sleep_ms (timeout_ms, NULL);
  return 0;
}

static void
poll_irq_close (struct bf63112_spi_irq *irq)
{
  (void) irq;
}

int
bf63112_spi_irq_poll_init (struct bf63112_spi_irq *irq,
                           struct bf63112_poll_irq *state,
                           void (*sleep_ms) (unsigned int, void *),
                           void *sleep_ctx)
{
  if (irq == NULL || state == NULL)
    return BF63112_SPI_ERR_INVALID;
  memset (state, 0, sizeof *state);
  state->sleep_ms = sleep_ms;
  state->sleep_ctx = sleep_ctx;
  irq->open = poll_irq_open;
  irq->wait = poll_irq_wait;
  irq->close = poll_irq_close;
  irq->priv = state;
  return 0;
}

/* -------------------------------------------------------------- sysfs reset */

static int
sysfs_reset_open (struct bf63112_spi_reset *reset)
{
  struct bf63112_sysfs_reset *state = reset->priv;
  char path[320];
  int rc;

  if (state == NULL || state->line < 0)
    return BF63112_SPI_ERR_INVALID;
  if (!path_is_dir (state->base))
    return BF63112_SPI_ERR_IO;
  snprintf (path, sizeof path, "%s/gpio%d", state->base, state->line);
  if (!path_is_dir (path))
    {
      snprintf (path, sizeof path, "%s/export", state->base);
      rc = write_sysfs_int (path, state->line);
      if (rc != 0)
        return rc;
      state->exported_here = true;
      snprintf (path, sizeof path, "%s/gpio%d", state->base, state->line);
      if (!wait_for_path (path, 500))
        return BF63112_SPI_ERR_TIMEOUT;
    }
  snprintf (path, sizeof path, "%s/gpio%d/direction", state->base,
            state->line);
  rc = write_sysfs (path, "out");
  if (rc != 0)
    return rc;
  state->hw_active_low = false;
  snprintf (path, sizeof path, "%s/gpio%d/active_low", state->base,
            state->line);
  if (access (path, F_OK) == 0)
    {
      rc = write_sysfs (path, state->active_low ? "1" : "0");
      if (rc == 0)
        state->hw_active_low = true;
    }
  return 0;
}

static int
sysfs_reset_set (struct bf63112_spi_reset *reset, int logical)
{
  struct bf63112_sysfs_reset *state = reset->priv;
  char path[320];
  int physical = logical ? 1 : 0;

  if (state == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (state->active_low && !state->hw_active_low)
    physical = 1 - physical;
  snprintf (path, sizeof path, "%s/gpio%d/value", state->base, state->line);
  return write_sysfs (path, physical ? "1" : "0");
}

static void
sysfs_reset_close (struct bf63112_spi_reset *reset)
{
  struct bf63112_sysfs_reset *state = reset->priv;
  char path[320];

  if (state == NULL)
    return;
  if (state->exported_here)
    {
      snprintf (path, sizeof path, "%s/unexport", state->base);
      (void) write_sysfs_int (path, state->line);
      state->exported_here = false;
    }
}

int
bf63112_spi_reset_sysfs_init (struct bf63112_spi_reset *reset,
                              struct bf63112_sysfs_reset *state,
                              int line, bool active_low,
                              const char *sysfs_base)
{
  size_t len;

  if (reset == NULL || state == NULL || line < 0 || sysfs_base == NULL)
    return BF63112_SPI_ERR_INVALID;
  len = strlen (sysfs_base);
  if (len >= sizeof state->base)
    return BF63112_SPI_ERR_INVALID;
  memset (state, 0, sizeof *state);
  memcpy (state->base, sysfs_base, len + 1);
  state->line = line;
  state->active_low = active_low;
  reset->open = sysfs_reset_open;
  reset->set = sysfs_reset_set;
  reset->close = sysfs_reset_close;
  reset->priv = state;
  reset->active_low = active_low;
  return 0;
}

/* --------------------------------------------------------- libgpiod v2 */

#if defined(BF63112_HAVE_GPIOD_V2)

struct bf63112_gpiod_irq
{
  char chip_path[256];
  unsigned int offset;
  int edge;
  bool active_low;
  struct gpiod_chip *chip;
  struct gpiod_line_request *request;
  struct gpiod_edge_event_buffer *events;
};

struct bf63112_gpiod_reset
{
  char chip_path[256];
  unsigned int offset;
  bool active_low;
  struct gpiod_chip *chip;
  struct gpiod_line_request *request;
};

static enum gpiod_line_edge
gpiod_edge (int edge)
{
  switch (edge)
    {
    case BF63112_SPI_IRQ_FALLING:
      return GPIOD_LINE_EDGE_FALLING;
    case BF63112_SPI_IRQ_BOTH:
      return GPIOD_LINE_EDGE_BOTH;
    case BF63112_SPI_IRQ_RISING:
    default:
      return GPIOD_LINE_EDGE_RISING;
    }
}

static void
gpiod_irq_release (struct bf63112_gpiod_irq *state)
{
  if (state->request != NULL)
    {
      gpiod_line_request_release (state->request);
      state->request = NULL;
    }
  if (state->events != NULL)
    {
      gpiod_edge_event_buffer_free (state->events);
      state->events = NULL;
    }
  if (state->chip != NULL)
    {
      gpiod_chip_close (state->chip);
      state->chip = NULL;
    }
}

static int
gpiod_irq_open (struct bf63112_spi_irq *irq)
{
  struct bf63112_gpiod_irq *state = irq->priv;
  struct gpiod_line_settings *settings = NULL;
  struct gpiod_line_config *line_cfg = NULL;
  struct gpiod_request_config *req_cfg = NULL;

  if (state == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (state->request != NULL)
    return 0;
  state->chip = gpiod_chip_open (state->chip_path);
  if (state->chip == NULL)
    goto fail;
  settings = gpiod_line_settings_new ();
  if (settings == NULL)
    goto fail;
  if (gpiod_line_settings_set_direction (settings,
                                         GPIOD_LINE_DIRECTION_INPUT) < 0)
    goto fail;
  if (gpiod_line_settings_set_edge_detection (settings,
                                              gpiod_edge (state->edge)) < 0)
    goto fail;
  gpiod_line_settings_set_active_low (settings, state->active_low);
  line_cfg = gpiod_line_config_new ();
  if (line_cfg == NULL)
    goto fail;
  if (gpiod_line_config_add_line_settings (line_cfg, &state->offset, 1,
                                           settings) < 0)
    goto fail;
  req_cfg = gpiod_request_config_new ();
  if (req_cfg == NULL)
    goto fail;
  gpiod_request_config_set_consumer (req_cfg, "bf63112");
  state->request = gpiod_chip_request_lines (state->chip, req_cfg, line_cfg);
  if (state->request == NULL)
    goto fail;
  state->events = gpiod_edge_event_buffer_new (16);
  if (state->events == NULL)
    goto fail;
  gpiod_line_settings_free (settings);
  gpiod_line_config_free (line_cfg);
  gpiod_request_config_free (req_cfg);
  return 0;

fail:
  if (settings != NULL)
    gpiod_line_settings_free (settings);
  if (line_cfg != NULL)
    gpiod_line_config_free (line_cfg);
  if (req_cfg != NULL)
    gpiod_request_config_free (req_cfg);
  gpiod_irq_release (state);
  return BF63112_SPI_ERR_IO;
}

static int
gpiod_irq_wait (struct bf63112_spi_irq *irq, unsigned int timeout_ms,
                struct bf63112_spi_irq_event *event)
{
  struct bf63112_gpiod_irq *state = irq->priv;
  struct gpiod_edge_event *edge_event;
  int64_t timeout_ns;
  uint64_t started;
  int rc, n;

  if (state == NULL || state->request == NULL || event == NULL)
    return BF63112_SPI_ERR_INVALID;
  memset (event, 0, sizeof *event);
  event->source = BF63112_SPI_IRQ_SOURCE_GPIOD;
  event->edge = -1;
  event->value = -1;
  event->observed = false;
  timeout_ns = (int64_t) timeout_ms * 1000000;
  started = default_now_ms (NULL);
  do
    rc = gpiod_line_request_wait_edge_events (state->request, timeout_ns);
  while (rc < 0 && errno == EINTR);
  event->elapsed_ms = default_now_ms (NULL) - started;
  if (rc < 0)
    return neg_errno ();
  if (rc == 0)
    return BF63112_SPI_ERR_TIMEOUT;
  n = gpiod_line_request_read_edge_events (state->request, state->events, 16);
  if (n <= 0)
    return n < 0 ? neg_errno () : BF63112_SPI_ERR_IO;
  edge_event = gpiod_edge_event_buffer_get_event (state->events, 0);
  if (edge_event == NULL)
    return BF63112_SPI_ERR_IO;
  event->edge =
    gpiod_edge_event_get_event_type (edge_event) ==
    GPIOD_EDGE_EVENT_RISING_EDGE ? 1 : 0;
  event->observed = true;
  return 0;
}

static void
gpiod_irq_close (struct bf63112_spi_irq *irq)
{
  struct bf63112_gpiod_irq *state = irq->priv;

  if (state != NULL)
    gpiod_irq_release (state);
}

int
bf63112_spi_irq_gpiod_init (struct bf63112_spi_irq *irq,
                            struct bf63112_gpiod_irq **state_out,
                            int line, int edge, bool active_low,
                            const char *chip)
{
  struct bf63112_gpiod_irq *state;
  size_t len;

  if (irq == NULL || state_out == NULL || line < 0 || chip == NULL)
    return BF63112_SPI_ERR_INVALID;
  len = strlen (chip);
  if (len >= sizeof state->chip_path)
    return BF63112_SPI_ERR_INVALID;
  state = calloc (1, sizeof *state);
  if (state == NULL)
    return BF63112_SPI_ERR_NO_SPACE;
  memcpy (state->chip_path, chip, len + 1);
  state->offset = (unsigned int) line;
  state->edge = edge;
  state->active_low = active_low;
  irq->open = gpiod_irq_open;
  irq->wait = gpiod_irq_wait;
  irq->close = gpiod_irq_close;
  irq->priv = state;
  *state_out = state;
  return 0;
}

static int
gpiod_reset_open (struct bf63112_spi_reset *reset)
{
  struct bf63112_gpiod_reset *state = reset->priv;
  struct gpiod_line_settings *settings = NULL;
  struct gpiod_line_config *line_cfg = NULL;
  struct gpiod_request_config *req_cfg = NULL;

  if (state == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (state->request != NULL)
    return 0;
  state->chip = gpiod_chip_open (state->chip_path);
  if (state->chip == NULL)
    goto fail;
  settings = gpiod_line_settings_new ();
  if (settings == NULL)
    goto fail;
  if (gpiod_line_settings_set_direction (settings,
                                         GPIOD_LINE_DIRECTION_OUTPUT) < 0)
    goto fail;
  if (gpiod_line_settings_set_output_value (settings,
                                            GPIOD_LINE_VALUE_INACTIVE) < 0)
    goto fail;
  gpiod_line_settings_set_active_low (settings, state->active_low);
  line_cfg = gpiod_line_config_new ();
  if (line_cfg == NULL)
    goto fail;
  if (gpiod_line_config_add_line_settings (line_cfg, &state->offset, 1,
                                           settings) < 0)
    goto fail;
  req_cfg = gpiod_request_config_new ();
  if (req_cfg == NULL)
    goto fail;
  gpiod_request_config_set_consumer (req_cfg, "bf63112-reset");
  state->request = gpiod_chip_request_lines (state->chip, req_cfg, line_cfg);
  if (state->request == NULL)
    goto fail;
  gpiod_line_settings_free (settings);
  gpiod_line_config_free (line_cfg);
  gpiod_request_config_free (req_cfg);
  return 0;

fail:
  if (settings != NULL)
    gpiod_line_settings_free (settings);
  if (line_cfg != NULL)
    gpiod_line_config_free (line_cfg);
  if (req_cfg != NULL)
    gpiod_request_config_free (req_cfg);
  if (state->request != NULL)
    {
      gpiod_line_request_release (state->request);
      state->request = NULL;
    }
  if (state->chip != NULL)
    {
      gpiod_chip_close (state->chip);
      state->chip = NULL;
    }
  return BF63112_SPI_ERR_IO;
}

static int
gpiod_reset_set (struct bf63112_spi_reset *reset, int logical)
{
  struct bf63112_gpiod_reset *state = reset->priv;

  if (state == NULL || state->request == NULL)
    return BF63112_SPI_ERR_INVALID;
  return gpiod_line_request_set_value (
           state->request, state->offset,
           logical ? GPIOD_LINE_VALUE_ACTIVE : GPIOD_LINE_VALUE_INACTIVE) < 0
         ? neg_errno () : 0;
}

static void
gpiod_reset_close (struct bf63112_spi_reset *reset)
{
  struct bf63112_gpiod_reset *state = reset->priv;

  if (state == NULL)
    return;
  if (state->request != NULL)
    {
      gpiod_line_request_release (state->request);
      state->request = NULL;
    }
  if (state->chip != NULL)
    {
      gpiod_chip_close (state->chip);
      state->chip = NULL;
    }
}

int
bf63112_spi_reset_gpiod_init (struct bf63112_spi_reset *reset,
                              struct bf63112_gpiod_reset **state_out,
                              int line, bool active_low,
                              const char *chip)
{
  struct bf63112_gpiod_reset *state;
  size_t len;

  if (reset == NULL || state_out == NULL || line < 0 || chip == NULL)
    return BF63112_SPI_ERR_INVALID;
  len = strlen (chip);
  if (len >= sizeof state->chip_path)
    return BF63112_SPI_ERR_INVALID;
  state = calloc (1, sizeof *state);
  if (state == NULL)
    return BF63112_SPI_ERR_NO_SPACE;
  memcpy (state->chip_path, chip, len + 1);
  state->offset = (unsigned int) line;
  state->active_low = active_low;
  reset->open = gpiod_reset_open;
  reset->set = gpiod_reset_set;
  reset->close = gpiod_reset_close;
  reset->priv = state;
  reset->active_low = active_low;
  *state_out = state;
  return 0;
}

#endif /* BF63112_HAVE_GPIOD_V2 */

/* --------------------------------------------------------- libgpiod v1 */

#if !defined(BF63112_HAVE_GPIOD_V2) && defined(BF63112_HAVE_GPIOD_V1)

struct bf63112_gpiod_irq
{
  char chip_path[256];
  unsigned int offset;
  int edge;
  bool active_low;
  struct gpiod_chip *chip;
  struct gpiod_line *line;
};

struct bf63112_gpiod_reset
{
  char chip_path[256];
  unsigned int offset;
  bool active_low;
  struct gpiod_chip *chip;
  struct gpiod_line *line;
};

static int
gpiod_irq_open (struct bf63112_spi_irq *irq)
{
  struct bf63112_gpiod_irq *state = irq->priv;
  int flags = state != NULL && state->active_low
              ? GPIOD_LINE_REQUEST_FLAG_ACTIVE_LOW : 0;

  if (state == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (state->line != NULL)
    return 0;
  state->chip = gpiod_chip_open (state->chip_path);
  if (state->chip == NULL)
    return BF63112_SPI_ERR_IO;
  state->line = gpiod_chip_get_line (state->chip, state->offset);
  if (state->line == NULL)
    return BF63112_SPI_ERR_IO;
  if (state->edge == BF63112_SPI_IRQ_BOTH)
    {
      if (gpiod_line_request_both_edges_events_flags (
            state->line, "bf63112", flags) < 0)
        return BF63112_SPI_ERR_IO;
    }
  else if (state->edge == BF63112_SPI_IRQ_FALLING)
    {
      if (gpiod_line_request_falling_edge_events_flags (
            state->line, "bf63112", flags) < 0)
        return BF63112_SPI_ERR_IO;
    }
  else if (gpiod_line_request_rising_edge_events_flags (
             state->line, "bf63112", flags) < 0)
    return BF63112_SPI_ERR_IO;
  return 0;
}

static int
gpiod_irq_wait (struct bf63112_spi_irq *irq, unsigned int timeout_ms,
                struct bf63112_spi_irq_event *event)
{
  struct bf63112_gpiod_irq *state = irq->priv;
  struct gpiod_line_event ev;
  struct timespec ts;
  uint64_t started;
  int rc;

  if (state == NULL || state->line == NULL || event == NULL)
    return BF63112_SPI_ERR_INVALID;
  memset (event, 0, sizeof *event);
  event->source = BF63112_SPI_IRQ_SOURCE_GPIOD;
  event->edge = -1;
  event->value = -1;
  event->observed = false;
  ts.tv_sec = (time_t) (timeout_ms / 1000u);
  ts.tv_nsec = (long) (timeout_ms % 1000u) * 1000000L;
  started = default_now_ms (NULL);
  do
    rc = gpiod_line_event_wait (state->line, &ts);
  while (rc < 0 && errno == EINTR);
  event->elapsed_ms = default_now_ms (NULL) - started;
  if (rc < 0)
    return neg_errno ();
  if (rc == 0)
    return BF63112_SPI_ERR_TIMEOUT;
  if (gpiod_line_event_read (state->line, &ev) < 0)
    return neg_errno ();
  event->edge = ev.event_type == GPIOD_LINE_EVENT_RISING_EDGE ? 1 : 0;
  event->observed = true;
  return 0;
}

static void
gpiod_irq_close (struct bf63112_spi_irq *irq)
{
  struct bf63112_gpiod_irq *state = irq->priv;

  if (state == NULL)
    return;
  if (state->line != NULL)
    {
      gpiod_line_release (state->line);
      state->line = NULL;
    }
  if (state->chip != NULL)
    {
      gpiod_chip_close (state->chip);
      state->chip = NULL;
    }
}

int
bf63112_spi_irq_gpiod_init (struct bf63112_spi_irq *irq,
                            struct bf63112_gpiod_irq **state_out,
                            int line, int edge, bool active_low,
                            const char *chip)
{
  struct bf63112_gpiod_irq *state;
  size_t len;

  if (irq == NULL || state_out == NULL || line < 0 || chip == NULL)
    return BF63112_SPI_ERR_INVALID;
  len = strlen (chip);
  if (len >= sizeof state->chip_path)
    return BF63112_SPI_ERR_INVALID;
  state = calloc (1, sizeof *state);
  if (state == NULL)
    return BF63112_SPI_ERR_NO_SPACE;
  memcpy (state->chip_path, chip, len + 1);
  state->offset = (unsigned int) line;
  state->edge = edge;
  state->active_low = active_low;
  irq->open = gpiod_irq_open;
  irq->wait = gpiod_irq_wait;
  irq->close = gpiod_irq_close;
  irq->priv = state;
  *state_out = state;
  return 0;
}

static int
gpiod_reset_open (struct bf63112_spi_reset *reset)
{
  struct bf63112_gpiod_reset *state = reset->priv;
  int flags = state != NULL && state->active_low
              ? GPIOD_LINE_REQUEST_FLAG_ACTIVE_LOW : 0;

  if (state == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (state->line != NULL)
    return 0;
  state->chip = gpiod_chip_open (state->chip_path);
  if (state->chip == NULL)
    return BF63112_SPI_ERR_IO;
  state->line = gpiod_chip_get_line (state->chip, state->offset);
  if (state->line == NULL)
    return BF63112_SPI_ERR_IO;
  if (gpiod_line_request_output_flags (state->line, "bf63112-reset", flags,
                                       0) < 0)
    return BF63112_SPI_ERR_IO;
  return 0;
}

static int
gpiod_reset_set (struct bf63112_spi_reset *reset, int logical)
{
  struct bf63112_gpiod_reset *state = reset->priv;

  if (state == NULL || state->line == NULL)
    return BF63112_SPI_ERR_INVALID;
  return gpiod_line_set_value (state->line, logical ? 1 : 0) < 0
         ? neg_errno () : 0;
}

static void
gpiod_reset_close (struct bf63112_spi_reset *reset)
{
  struct bf63112_gpiod_reset *state = reset->priv;

  if (state == NULL)
    return;
  if (state->line != NULL)
    {
      gpiod_line_release (state->line);
      state->line = NULL;
    }
  if (state->chip != NULL)
    {
      gpiod_chip_close (state->chip);
      state->chip = NULL;
    }
}

int
bf63112_spi_reset_gpiod_init (struct bf63112_spi_reset *reset,
                              struct bf63112_gpiod_reset **state_out,
                              int line, bool active_low,
                              const char *chip)
{
  struct bf63112_gpiod_reset *state;
  size_t len;

  if (reset == NULL || state_out == NULL || line < 0 || chip == NULL)
    return BF63112_SPI_ERR_INVALID;
  len = strlen (chip);
  if (len >= sizeof state->chip_path)
    return BF63112_SPI_ERR_INVALID;
  state = calloc (1, sizeof *state);
  if (state == NULL)
    return BF63112_SPI_ERR_NO_SPACE;
  memcpy (state->chip_path, chip, len + 1);
  state->offset = (unsigned int) line;
  state->active_low = active_low;
  reset->open = gpiod_reset_open;
  reset->set = gpiod_reset_set;
  reset->close = gpiod_reset_close;
  reset->priv = state;
  reset->active_low = active_low;
  *state_out = state;
  return 0;
}

#endif /* BF63112_HAVE_GPIOD_V1 (without v2) */

/* ------------------------------------------------------------- transport */

void
bf63112_spi_init (struct bf63112_spi *spi,
                  const struct bf63112_spi_io *io, void *io_priv)
{
  if (spi == NULL)
    return;
  memset (spi, 0, sizeof *spi);
  spi->io = io;
  spi->io_priv = io_priv;
  spi->speed_hz = -1;
  spi->mode = -1;
  spi->bits_per_word = -1;
}

void
bf63112_spi_set_clock (struct bf63112_spi *spi,
                       uint64_t (*now_ms) (void *ctx), void *now_ctx,
                       void (*sleep_ms) (unsigned int ms, void *ctx),
                       void *sleep_ctx)
{
  if (spi == NULL)
    return;
  spi->now_ms = now_ms;
  spi->now_ctx = now_ctx;
  spi->sleep_ms = sleep_ms;
  spi->sleep_ctx = sleep_ctx;
}

void
bf63112_spi_set_irq (struct bf63112_spi *spi,
                     struct bf63112_spi_irq *irq)
{
  if (spi != NULL)
    spi->irq = irq;
}

void
bf63112_spi_set_reset (struct bf63112_spi *spi,
                       struct bf63112_spi_reset *reset, bool allow_reset)
{
  if (spi == NULL)
    return;
  spi->reset = reset;
  spi->allow_reset = allow_reset;
}

static int
reset_pulse (struct bf63112_spi *spi)
{
  int rc;

  if (spi->reset == NULL)
    return 0;
  rc = spi->reset->open (spi->reset);
  if (rc != 0)
    return rc;
  rc = spi->reset->set (spi->reset, 1);
  if (rc != 0)
    return rc;
  spi_sleep_ms (spi, BF63112_SPI_RESET_ASSERT_MS);
  rc = spi->reset->set (spi->reset, 0);
  if (rc != 0)
    return rc;
  spi_sleep_ms (spi, BF63112_SPI_RESET_RELEASE_MS);
  return 0;
}

int
bf63112_spi_open (struct bf63112_spi *spi)
{
  int rc;

  if (spi == NULL || spi->io == NULL || spi->io->open == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (spi->opened)
    return 0;
  rc = spi->io->open (spi->io_priv);
  if (rc != 0)
    return rc;
  if (spi->io->configure != NULL)
    {
      rc = spi->io->configure (spi->io_priv, spi->speed_hz, spi->mode,
                               spi->bits_per_word);
      if (rc != 0)
        {
          spi->io->close (spi->io_priv);
          return rc;
        }
    }
  spi->opened = true;
  spi->configured = true;
  if (spi->reset != NULL && spi->allow_reset)
    {
      rc = reset_pulse (spi);
      if (rc != 0)
        {
          spi->io->close (spi->io_priv);
          spi->opened = false;
          spi->configured = false;
          return rc;
        }
      /* FUN_180016260: Sleep(0x32) after the reset target is opened. */
      spi_sleep_ms (spi, BF63112_SPI_RESET_OPEN_MS);
    }
  return 0;
}

void
bf63112_spi_close (struct bf63112_spi *spi)
{
  if (spi == NULL)
    return;
  if (spi->io != NULL && spi->io->close != NULL)
    (void) spi->io->close (spi->io_priv);
  if (spi->irq != NULL && spi->irq->close != NULL)
    spi->irq->close (spi->irq);
  if (spi->reset != NULL && spi->reset->close != NULL)
    spi->reset->close (spi->reset);
  spi->opened = false;
  spi->configured = false;
}

static int
check_cancel (struct bf63112_spi *spi)
{
  if (!spi->cancel_requested)
    return 0;
  spi->cancel_requested = false;
  return BF63112_SPI_ERR_CANCELLED;
}

void
bf63112_spi_request_cancel (struct bf63112_spi *spi)
{
  if (spi != NULL)
    spi->cancel_requested = true;
}

void
bf63112_spi_cancel (struct bf63112_spi *spi)
{
  if (spi == NULL)
    return;
  spi->cancel_requested = true;
  /* FUN_180013b60: reset the events and settle for 150 ms. */
  spi_sleep_ms (spi, BF63112_SPI_CANCEL_SETTLE_MS);
}

static size_t
expected_bytes (const struct bf63112_spi_plan *plan)
{
  size_t i, total = 0;

  for (i = 0; i < plan->n_transfers; i++)
    total += plan->transfers[i].tx_len > plan->transfers[i].rx_len
             ? plan->transfers[i].tx_len : plan->transfers[i].rx_len;
  return total;
}

int
bf63112_spi_execute (struct bf63112_spi *spi,
                     const struct bf63112_spi_plan *plan,
                     uint8_t *answer, size_t answer_size,
                     size_t *answer_len)
{
  struct bf63112_spi_plan local;
  size_t bytes_done = 0;
  uint64_t started;
  int rc;

  if (spi == NULL || plan == NULL || spi->io == NULL ||
      spi->io->message == NULL)
    return BF63112_SPI_ERR_INVALID;
  rc = bf63112_spi_plan_validate (plan);
  if (rc != 0)
    return rc;
  if (!plan->write &&
      (answer == NULL || answer_size < (size_t) plan->read_len))
    return BF63112_SPI_ERR_INVALID;
  rc = check_cancel (spi);
  if (rc != 0)
    return rc;
  rc = bf63112_spi_open (spi);
  if (rc != 0)
    return rc;

  started = spi_now_ms (spi);
  rc = plan_deadline_rc (plan, spi_now_ms (spi) - started);
  if (rc != 0)
    return rc;

  local = *plan;
  if (!local.write)
    {
      local.transfers[1].rx = answer;
      local.transfers[1].rx_len = plan->read_len;
    }
  rc = spi->io->message (spi->io_priv, local.transfers, local.n_transfers,
                         &bytes_done);
  if (rc != 0)
    return rc;
  if (!local.write && bytes_done < expected_bytes (plan))
    return BF63112_SPI_ERR_IO;

  rc = check_cancel (spi);
  if (rc != 0)
    return rc;
  rc = plan_deadline_rc (plan, spi_now_ms (spi) - started);
  if (rc != 0)
    return rc;

  if (spi->irq != NULL)
    {
      struct bf63112_spi_irq_event event;
      int64_t remaining = bf63112_spi_irq_budget (
        plan, spi_now_ms (spi) - started);

      if (remaining <= 0)
        return BF63112_SPI_ERR_TIMEOUT;
      rc = spi->irq->open (spi->irq);
      if (rc != 0)
        return rc;
      rc = spi->irq->wait (spi->irq, (unsigned int) remaining, &event);
      if (rc != 0)
        return rc;
      spi->last_irq = event;
      spi->have_last_irq = true;
      rc = plan_deadline_rc (plan, spi_now_ms (spi) - started);
      if (rc != 0)
        return rc;
    }

  if (answer_len != NULL)
    *answer_len = local.write ? 0 : (size_t) plan->read_len;
  return 0;
}

int
bf63112_spi_wait_irq (struct bf63112_spi *spi, unsigned int timeout_ms,
                      struct bf63112_spi_irq_event *event)
{
  int rc;

  if (spi == NULL || spi->irq == NULL || event == NULL)
    return BF63112_SPI_ERR_NO_IRQ;
  rc = spi->irq->open (spi->irq);
  if (rc != 0)
    return rc;
  rc = spi->irq->wait (spi->irq, timeout_ms, event);
  if (rc == 0)
    {
      spi->last_irq = *event;
      spi->have_last_irq = true;
    }
  return rc;
}

int
bf63112_spi_capture_wait (struct bf63112_spi *spi,
                          unsigned int timeout_ms, unsigned int attempts,
                          struct bf63112_spi_irq_event *event)
{
  unsigned int attempt;
  int last = BF63112_SPI_ERR_TIMEOUT;

  if (spi == NULL || event == NULL)
    return BF63112_SPI_ERR_INVALID;
  if (attempts == 0)
    attempts = 1;
  for (attempt = 0; attempt < attempts; attempt++)
    {
      struct bf63112_spi_irq_event current;

      last = bf63112_spi_wait_irq (spi, timeout_ms, &current);
      if (last == 0 && current.observed)
        {
          *event = current;
          return 0;
        }
      if (last == 0)
        last = BF63112_SPI_ERR_TIMEOUT;   /* timed poll is not an observation */
      else if (last != BF63112_SPI_ERR_TIMEOUT)
        return last;
      if (attempt + 1 < attempts)
        spi_sleep_ms (spi, BF63112_SPI_IRQ_RETRY_SLEEP_MS);
    }
  return last;
}

bool
bf63112_spi_irq_event_asserted (const struct bf63112_spi_irq_event *event)
{
  if (event == NULL)
    return false;
  if (event->edge == 1 || event->value == 1)
    return true;
  return false;
}

const char *
bf63112_spi_irq_source_name (enum bf63112_spi_irq_source source)
{
  switch (source)
    {
    case BF63112_SPI_IRQ_SOURCE_GPIOD:
      return "gpiod";
    case BF63112_SPI_IRQ_SOURCE_SYSFS:
      return "sysfs";
    case BF63112_SPI_IRQ_SOURCE_POLL:
      return "poll";
    case BF63112_SPI_IRQ_SOURCE_NONE:
    default:
      return "none";
    }
}

const char *
bf63112_spi_strerror (int error)
{
  switch (error)
    {
    case 0:
      return "ok";
    case BF63112_SPI_ERR_INVALID:
      return "invalid argument";
    case BF63112_SPI_ERR_REFUSED:
      return "refused: destructive opcode or selector";
    case BF63112_SPI_ERR_NOT_ALLOWED:
      return "refused: opcode not allowlisted or I/O not enabled";
    case BF63112_SPI_ERR_IO:
      return "I/O error";
    case BF63112_SPI_ERR_TIMEOUT:
      return "timeout";
    case BF63112_SPI_ERR_CANCELLED:
      return "cancelled";
    case BF63112_SPI_ERR_NO_IRQ:
      return "no interrupt line configured";
    case BF63112_SPI_ERR_NO_SPACE:
      return "out of memory";
    default:
      return strerror (error < 0 ? -error : error);
    }
}
