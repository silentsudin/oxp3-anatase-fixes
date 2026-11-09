/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * test_spi.c - standalone unit tests for the C BF63112 SPI/ACPI transport.
 *
 * No libfprint, no GLib, no hardware: the spidev I/O vtable, the monotonic
 * clock, the sleep function, the IRQ line and the reset line are all fakes.
 * Compile and run with:
 *
 *   cc -std=gnu11 -Wall -Wextra -Werror -I. -Itests \
 *      -o build/test_spi tests/test_spi.c bf63112_spi.c bf63112_acpi.c \
 *      bf63112_proto.c bf63112_discover.c
 *   build/test_spi
 *
 * Only temporary directories are created (by the discovery test).
 */

#include "bf63112_spi.h"
#include "bf63112_acpi.h"
#include "bf63112_proto.h"
#include "bf63112_discover.h"

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static unsigned int checks;
static unsigned int failures;

#define CHECK(cond, ...)                                                     \
  do                                                                         \
    {                                                                        \
      checks++;                                                              \
      if (!(cond))                                                           \
        {                                                                    \
          failures++;                                                        \
          fprintf (stderr, "FAIL %s:%d: ", __FILE__, __LINE__);              \
          fprintf (stderr, __VA_ARGS__);                                     \
          fprintf (stderr, "\n");                                            \
        }                                                                    \
    }                                                                        \
  while (0)

/* --------------------------------------------------------------- fake clock */

struct fake_clock
{
  uint64_t now;
  unsigned int sleeps[64];
  unsigned int n_sleeps;
};

static uint64_t
fake_now (void *ctx)
{
  return ((struct fake_clock *) ctx)->now;
}

static void
fake_sleep (unsigned int ms, void *ctx)
{
  struct fake_clock *clock = ctx;

  if (clock->n_sleeps < sizeof clock->sleeps / sizeof clock->sleeps[0])
    clock->sleeps[clock->n_sleeps++] = ms;
  clock->now += ms;
}

/* ------------------------------------------------------------------ fake IO */

struct fake_io
{
  int opened;
  int closed;
  int configure_calls;
  int configure_speed;
  int configure_mode;
  int configure_bits;
  int messages;
  size_t last_n_transfers;
  uint8_t last_selector;
  size_t last_payload_len;
  const uint8_t *last_payload;
  uint8_t answer[512];
  size_t answer_len;
  size_t advance_ms;
  size_t short_by;
  int fail_open;
  int fail_message;
  struct fake_clock *clock;
  struct bf63112_spi *spi;
  int cancel_on_message;
};

static int
fake_open (void *priv)
{
  struct fake_io *io = priv;

  if (io->fail_open)
    return -ENODEV;
  io->opened++;
  return 0;
}

static int
fake_close (void *priv)
{
  struct fake_io *io = priv;

  io->closed++;
  return 0;
}

static int
fake_configure (void *priv, int speed_hz, int mode, int bits_per_word)
{
  struct fake_io *io = priv;

  io->configure_calls++;
  io->configure_speed = speed_hz;
  io->configure_mode = mode;
  io->configure_bits = bits_per_word;
  return 0;
}

static size_t
expected_for (const struct bf63112_spi_transfer *transfers, size_t n)
{
  size_t i, total = 0;

  for (i = 0; i < n; i++)
    total += transfers[i].tx_len > transfers[i].rx_len
             ? transfers[i].tx_len : transfers[i].rx_len;
  return total;
}

static int
fake_message (void *priv, const struct bf63112_spi_transfer *transfers,
              size_t n_transfers, size_t *bytes_done)
{
  struct fake_io *io = priv;
  size_t i, total;

  io->messages++;
  io->last_n_transfers = n_transfers;
  io->last_selector = transfers[0].tx_len ? transfers[0].tx[0] : 0;
  io->last_payload_len = n_transfers > 1 ? transfers[1].tx_len : 0;
  io->last_payload = n_transfers > 1 ? transfers[1].tx : NULL;
  for (i = 0; i < n_transfers; i++)
    {
      if (transfers[i].rx != NULL && transfers[i].rx_len > 0)
        {
          size_t copy = io->answer_len < transfers[i].rx_len
                        ? io->answer_len : transfers[i].rx_len;

          memcpy (transfers[i].rx, io->answer, copy);
          if (copy < transfers[i].rx_len)
            memset (transfers[i].rx + copy, 0, transfers[i].rx_len - copy);
        }
    }
  total = expected_for (transfers, n_transfers);
  if (bytes_done != NULL)
    *bytes_done = total > io->short_by ? total - io->short_by : 0;
  if (io->clock != NULL)
    io->clock->now += io->advance_ms;
  if (io->cancel_on_message && io->spi != NULL)
    bf63112_spi_request_cancel (io->spi);
  return io->fail_message ? -EIO : 0;
}

/* ----------------------------------------------------------------- fake IRQ */

struct fake_irq
{
  struct bf63112_spi_irq irq;
  int opens;
  int closes;
  int waits;
  unsigned int last_timeout;
  int result;
  int observed;
  struct fake_clock *clock;
  size_t advance_ms;
};

static int
fake_irq_open_cb (struct bf63112_spi_irq *irq)
{
  struct fake_irq *fake = irq->priv;

  fake->opens++;
  return 0;
}

static int
fake_irq_wait_cb (struct bf63112_spi_irq *irq, unsigned int timeout_ms,
                  struct bf63112_spi_irq_event *event)
{
  struct fake_irq *fake = irq->priv;

  fake->waits++;
  fake->last_timeout = timeout_ms;
  memset (event, 0, sizeof *event);
  event->source = BF63112_SPI_IRQ_SOURCE_GPIOD;
  event->edge = -1;
  event->value = -1;
  event->observed = false;
  if (fake->clock != NULL)
    fake->clock->now += fake->advance_ms;
  if (fake->result != 0)
    return fake->result;
  event->observed = fake->observed;
  event->edge = 1;
  return 0;
}

static void
fake_irq_close_cb (struct bf63112_spi_irq *irq)
{
  struct fake_irq *fake = irq->priv;

  fake->closes++;
}

static void
fake_irq_init (struct fake_irq *fake)
{
  memset (fake, 0, sizeof *fake);
  fake->irq.open = fake_irq_open_cb;
  fake->irq.wait = fake_irq_wait_cb;
  fake->irq.close = fake_irq_close_cb;
  fake->irq.priv = fake;
}

/* --------------------------------------------------------------- fake reset */

struct fake_reset
{
  struct bf63112_spi_reset reset;
  int opens;
  int closes;
  int values[8];
  unsigned int n_values;
};

static int
fake_reset_open_cb (struct bf63112_spi_reset *reset)
{
  struct fake_reset *fake = reset->priv;

  fake->opens++;
  return 0;
}

static int
fake_reset_set_cb (struct bf63112_spi_reset *reset, int logical)
{
  struct fake_reset *fake = reset->priv;

  if (fake->n_values < sizeof fake->values / sizeof fake->values[0])
    fake->values[fake->n_values++] = logical;
  return 0;
}

static void
fake_reset_close_cb (struct bf63112_spi_reset *reset)
{
  struct fake_reset *fake = reset->priv;

  fake->closes++;
}

static void
fake_reset_init (struct fake_reset *fake)
{
  memset (fake, 0, sizeof *fake);
  fake->reset.open = fake_reset_open_cb;
  fake->reset.set = fake_reset_set_cb;
  fake->reset.close = fake_reset_close_cb;
  fake->reset.priv = fake;
  fake->reset.active_low = true;
}

/* --------------------------------------------------------------- tests: SPI */

static void
test_selector (void)
{
  CHECK (bf63112_spi_selector (0xA0, false) == 0x40, "A0 read");
  CHECK (bf63112_spi_selector (0xA1, false) == 0x42, "A1 read");
  CHECK (bf63112_spi_selector (0xC0, false) == 0x00, "C0 read");
  CHECK (bf63112_spi_selector (0xC1, false) == 0x02, "C1 read");
  CHECK (bf63112_spi_selector (0x2D, false) == 0x5A, "2D read");
  CHECK (bf63112_spi_selector (0xA0, true) == 0xC0, "A0 write");
  CHECK (bf63112_spi_selector (0x00, true) == 0x80, "00 write");
  CHECK (bf63112_spi_selector (0xA0, false) ==
         bf63112_spi_selector (0x20, false), "alias A0/20");
  CHECK (bf63112_spi_selector (0xC2, false) ==
         bf63112_spi_selector (0x02, false), "alias C2/02");
  CHECK (bf63112_spi_selector (0xC3, false) ==
         bf63112_spi_selector (0x03, false), "alias C3/03");
  CHECK (bf63112_spi_selector (0xD0, false) ==
         bf63112_spi_selector (0x10, false), "alias D0/10");
  CHECK (bf63112_spi_selector (0xFF, false) ==
         bf63112_spi_selector (0x3F, false), "alias FF/3F");
  CHECK (BF63112_SPI_IOC_WR_MODE == 0x40016B01UL, "SPI_IOC_WR_MODE ABI");
  CHECK (BF63112_SPI_IOC_WR_BITS_PER_WORD == 0x40016B03UL,
         "SPI_IOC_WR_BITS_PER_WORD ABI");
  CHECK (BF63112_SPI_IOC_WR_MAX_SPEED_HZ == 0x40046B04UL,
         "SPI_IOC_WR_MAX_SPEED_HZ ABI");
  CHECK (BF63112_SPI_IOC_MESSAGE (1) == 0x40206B00UL, "MESSAGE(1) ABI");
  CHECK (BF63112_SPI_IOC_MESSAGE (2) == 0x40406B00UL, "MESSAGE(2) ABI");
  CHECK (bf63112_spi_selector_low (0xC2) == 0x04, "C2 low selector");
  CHECK (bf63112_spi_selector_is_forbidden (0x04), "04 forbidden");
  CHECK (bf63112_spi_selector_is_forbidden (0x06), "06 forbidden");
  CHECK (bf63112_spi_selector_is_forbidden (0x20), "20 forbidden (D0)");
  CHECK (!bf63112_spi_selector_is_forbidden (0x40), "40 allowed");
  CHECK (bf63112_spi_selector_aliases (0x04) != NULL, "04 aliases");
  CHECK (strstr (bf63112_spi_selector_aliases (0x04), "0xc2") != NULL,
         "C2 named");
}

static void
test_policy (void)
{
  struct bf63112_spi_policy policy;
  struct bf63112_spi_plan plan;
  uint8_t payload[1] = { 0x10 };

  bf63112_spi_policy_init (&policy);
  CHECK (!policy.allow_io, "allow_io defaults off");
  CHECK (bf63112_spi_policy_read_allowed (&policy, 0xA0), "A0 allowlisted");
  CHECK (!bf63112_spi_policy_write_allowed (&policy, 0x2D), "no write default");
  CHECK (bf63112_spi_policy_check (&policy, 0xA0, false) ==
         BF63112_SPI_ERR_NOT_ALLOWED, "reads need the opt-in");
  CHECK (bf63112_spi_policy_check (&policy, 0xB0, false) ==
         BF63112_SPI_ERR_REFUSED, "enrol refused");
  CHECK (bf63112_spi_policy_check (&policy, 0x02, false) ==
         BF63112_SPI_ERR_REFUSED, "JTAG alias refused");
  policy.allow_io = true;
  CHECK (bf63112_spi_policy_check (&policy, 0xA0, false) == 0, "A0 allowed");
  CHECK (bf63112_spi_policy_check (&policy, 0xA1, false) == 0, "A1 allowed");
  CHECK (bf63112_spi_policy_check (&policy, 0xB3, false) == 0, "B3 allowed");
  CHECK (bf63112_spi_policy_check (&policy, 0xC0, false) == 0, "C0 allowed");
  CHECK (bf63112_spi_policy_check (&policy, 0xC1, false) == 0, "C1 allowed");
  CHECK (bf63112_spi_policy_check (&policy, 0x2D, false) == 0, "2D allowed");
  CHECK (bf63112_spi_policy_check (&policy, 0xA2, false) ==
         BF63112_SPI_ERR_NOT_ALLOWED, "A2 not allowlisted");
  CHECK (bf63112_spi_policy_check (&policy, 0x2D, true) ==
         BF63112_SPI_ERR_NOT_ALLOWED, "writes need explicit allow");
  CHECK (bf63112_spi_build_read (&policy, 0xB0, 1, 0, 0, &plan) ==
         BF63112_SPI_ERR_REFUSED, "build refuses enrol");
  CHECK (bf63112_spi_build_write (&policy, 0x2D, payload, 1, 0, 0,
                                  &plan) == BF63112_SPI_ERR_NOT_ALLOWED,
         "build refuses unlisted write");
  bf63112_spi_policy_allow_write (&policy, 0x2D);
  CHECK (bf63112_spi_build_write (&policy, 0x2D, payload, 1, 0, 0,
                                  &plan) == 0, "allowlisted write builds");
  CHECK (plan.selector == 0xDA, "write selector");
  CHECK (plan.n_transfers == 2 && plan.transfers[1].tx == payload,
         "write payload attached");
  CHECK (bf63112_spi_plan_validate (&plan) == 0, "write plan valid");
}

static void
test_plans_and_timeouts (void)
{
  struct bf63112_spi_policy policy;
  struct bf63112_spi_plan plan;
  uint16_t per, total;

  bf63112_spi_policy_init (&policy);
  policy.allow_io = true;

  CHECK (bf63112_spi_build_read (&policy, 0xA0, 0, 0, 0, &plan) ==
         BF63112_SPI_ERR_INVALID, "read_len 0 invalid");
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 500, 100, &plan) ==
         BF63112_SPI_ERR_INVALID, "total < per invalid");
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "A0 plan builds");
  CHECK (plan.opcode == 0xA0 && !plan.write && plan.read_len == 7,
         "A0 plan fields");
  CHECK (plan.selector == 0x40 && plan.selector_byte == 0x40, "A0 selector");
  CHECK (plan.n_transfers == 2, "read has two transfers");
  CHECK (plan.transfers[0].tx_len == 1 && plan.transfers[0].tx[0] == 0x40 &&
         plan.transfers[0].rx_len == 0, "transfer 0 is the selector");
  CHECK (plan.transfers[1].tx == NULL && plan.transfers[1].tx_len == 0 &&
         plan.transfers[1].rx_len == 7, "transfer 1 is the receive clock");
  CHECK (bf63112_spi_plan_validate (&plan) == 0, "read plan valid");

  bf63112_spi_default_timeouts (0xA0, &per, &total);
  CHECK (per == 1000 && total == 2030, "ordinary 1000/2030");
  bf63112_spi_default_timeouts (0xB4, &per, &total);
  CHECK (per == 5000 && total == 10010, "GET_IMG 5000/10010");
  bf63112_spi_default_timeouts (0xC3, &per, &total);
  CHECK (per == 2000 && total == 4016, "restore 2000/4016");
  CHECK (BF63112_SPI_CANCEL_SETTLE_MS == 150, "cancel settle 150");
  CHECK (BF63112_SPI_RESET_OPEN_MS == 50, "reset open 50");
  CHECK (BF63112_SPI_RESET_ASSERT_MS == 5 && BF63112_SPI_RESET_RELEASE_MS == 10,
         "reset pulse 5/10");

  CHECK (bf63112_spi_deadline_check (&plan, 1000) ==
         BF63112_SPI_DEADLINE_OK, "elapsed == per is not a timeout");
  CHECK (bf63112_spi_deadline_check (&plan, 1001) ==
         BF63112_SPI_DEADLINE_ATTEMPT, "per-attempt wins");
  CHECK (bf63112_spi_deadline_check (&plan, 2031) ==
         BF63112_SPI_DEADLINE_TOTAL, "total wins");
  CHECK (bf63112_spi_irq_budget (&plan, 100) == 900, "irq budget 900");
  CHECK (bf63112_spi_irq_budget (&plan, 1000) == 0, "irq budget 0");
  CHECK (bf63112_spi_irq_budget (&plan, 1500) == -500, "irq budget negative");

  bf63112_spi_policy_allow_read (&policy, 0xB4);
  CHECK (bf63112_spi_build_read (&policy, 0xB4, 0x1FCC, 0, 0, &plan) == 0,
         "image plan builds");
  CHECK (plan.timeout_ms == 5000 && plan.total_ms == 10010, "image timeouts");
  CHECK (bf63112_spi_deadline_check (&plan, 2031) ==
         BF63112_SPI_DEADLINE_OK, "image 2031 is still inside the attempt");
  CHECK (bf63112_spi_deadline_check (&plan, 5001) ==
         BF63112_SPI_DEADLINE_ATTEMPT, "image per-attempt");
  CHECK (bf63112_spi_deadline_check (&plan, 10011) ==
         BF63112_SPI_DEADLINE_TOTAL, "image total");
  CHECK (bf63112_spi_irq_budget (&plan, 4000) == 1000, "image budget 1000");
}

static void
test_frame_builder (void)
{
  uint8_t frame[128];
  const uint8_t payload[3] = { 0x11, 0x22, 0x33 };
  struct bf63112_frame parsed;
  size_t built;

  built = bf63112_build_frame (0xC1, 0, 0, 0x1234, payload, sizeof payload,
                               frame, sizeof frame);
  CHECK (built == bf63112_frame_size (sizeof payload), "frame size");
  CHECK (built > sizeof payload && built <= sizeof frame, "frame fits");
  CHECK (frame[0] == BF63112_SOF, "SOF");
  CHECK (bf63112_parse_frame (frame, built, &parsed) == BF63112_PARSE_OK,
         "built frame parses");
  CHECK (parsed.opcode == 0xC1 && parsed.phase == 0, "opcode/phase");
  CHECK (parsed.arg == 0x1234, "arg");
  CHECK (parsed.data_len == sizeof payload, "data length");
  CHECK (parsed.data != NULL &&
         memcmp (parsed.data, payload, sizeof payload) == 0, "payload");
  CHECK (parsed.crc == bf63112_crc32 (frame, built - 4), "CRC matches");
  CHECK (bf63112_build_frame (0xC1, 0, 0, 0, payload, sizeof payload,
                              frame, 4) == 0, "short output refused");
}

static void
make_transport (struct bf63112_spi *spi, struct fake_io *io,
                struct fake_clock *clock,
                struct bf63112_spi_policy *policy)
{
  static const struct bf63112_spi_io ops = {
    .open = fake_open,
    .close = fake_close,
    .configure = fake_configure,
    .message = fake_message,
  };

  memset (io, 0, sizeof *io);
  memset (clock, 0, sizeof *clock);
  io->clock = clock;
  bf63112_spi_init (spi, &ops, io);
  bf63112_spi_set_clock (spi, fake_now, clock, fake_sleep, clock);
  bf63112_spi_policy_init (policy);
  policy->allow_io = true;
}

static void
test_execute (void)
{
  struct bf63112_spi spi;
  struct fake_io io;
  struct fake_clock clock;
  struct bf63112_spi_policy policy;
  struct bf63112_spi_plan plan;
  struct fake_irq irq;
  size_t answer_len = 0;
  uint8_t answer[16];
  unsigned int i;

  make_transport (&spi, &io, &clock, &policy);
  for (i = 0; i < sizeof io.answer; i++)
    io.answer[i] = (uint8_t) (0x10 + i);
  io.answer_len = sizeof io.answer;
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "plan for execute");
  spi.speed_hz = 2000000;
  spi.mode = 3;
  spi.bits_per_word = 8;
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer,
                              &answer_len) == 0, "execute read");
  CHECK (answer_len == 7, "answer length");
  CHECK (memcmp (answer, io.answer, 7) == 0, "answer bytes");
  CHECK (io.opened == 1 && io.messages == 1, "opened and messaged once");
  CHECK (io.configure_calls == 1 && io.configure_speed == 2000000 &&
         io.configure_mode == 3 && io.configure_bits == 8,
         "configure passed through");
  CHECK (io.last_n_transfers == 2 && io.last_selector == 0x40,
         "two-entry selector message");

  /* short read */
  make_transport (&spi, &io, &clock, &policy);
  io.answer_len = 2;
  io.short_by = 1;
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "short plan");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) ==
         BF63112_SPI_ERR_IO, "short read rejected");

  /* per-attempt timeout after the transfer; no IRQ wait may start */
  make_transport (&spi, &io, &clock, &policy);
  io.advance_ms = 1001;
  fake_irq_init (&irq);
  bf63112_spi_set_irq (&spi, &irq.irq);
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "timeout plan");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) ==
         BF63112_SPI_ERR_TIMEOUT, "per-attempt timeout");
  CHECK (irq.waits == 0, "no IRQ wait after the attempt deadline");

  /* total timeout wins */
  make_transport (&spi, &io, &clock, &policy);
  io.advance_ms = 2031;
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "total plan");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) ==
         BF63112_SPI_ERR_TIMEOUT, "total timeout");

  /* IRQ gets the remaining budget */
  make_transport (&spi, &io, &clock, &policy);
  io.advance_ms = 100;
  fake_irq_init (&irq);
  irq.observed = 1;
  bf63112_spi_set_irq (&spi, &irq.irq);
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "irq plan");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) == 0,
         "execute with IRQ");
  CHECK (irq.waits == 1 && irq.last_timeout == 900, "IRQ budget 900");
  CHECK (spi.have_last_irq && spi.last_irq.observed, "last IRQ recorded");

  /* IRQ is not started when the deadline already expired */
  make_transport (&spi, &io, &clock, &policy);
  io.advance_ms = 1000;
  fake_irq_init (&irq);
  bf63112_spi_set_irq (&spi, &irq.irq);
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "deadline plan");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) ==
         BF63112_SPI_ERR_TIMEOUT, "no budget");
  CHECK (irq.waits == 0, "IRQ not started with zero budget");

  /* cancel before execute: no open, flag consumed */
  make_transport (&spi, &io, &clock, &policy);
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "cancel plan");
  bf63112_spi_request_cancel (&spi);
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) ==
         BF63112_SPI_ERR_CANCELLED, "cancel before execute");
  CHECK (io.opened == 0, "cancelled transport did not open");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) == 0,
         "flag consumed, next execute works");

  /* cancel during the transfer: abort before the IRQ wait */
  make_transport (&spi, &io, &clock, &policy);
  fake_irq_init (&irq);
  bf63112_spi_set_irq (&spi, &irq.irq);
  io.cancel_on_message = 1;
  io.spi = &spi;
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "cancel-during plan");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) ==
         BF63112_SPI_ERR_CANCELLED, "cancel during transfer");
  CHECK (io.messages == 1 && irq.waits == 0, "aborted before IRQ");

  /* cancel policy sleeps the 150 ms settle */
  make_transport (&spi, &io, &clock, &policy);
  bf63112_spi_cancel (&spi);
  CHECK (clock.n_sleeps == 1 && clock.sleeps[0] == 150, "cancel settle 150 ms");

  /* open failure is reported */
  make_transport (&spi, &io, &clock, &policy);
  io.fail_open = 1;
  CHECK (bf63112_spi_build_read (&policy, 0xA0, 7, 0, 0, &plan) == 0,
         "open-fail plan");
  CHECK (bf63112_spi_execute (&spi, &plan, answer, sizeof answer, NULL) ==
         -ENODEV, "open failure propagates");
}

static void
test_reset_and_irq_helpers (void)
{
  struct bf63112_spi spi;
  struct fake_io io;
  struct fake_clock clock;
  struct bf63112_spi_policy policy;
  struct fake_reset reset;
  struct bf63112_spi_irq poll_irq;
  struct bf63112_poll_irq poll_state;
  struct bf63112_spi_irq_event event;

  make_transport (&spi, &io, &clock, &policy);
  fake_reset_init (&reset);
  bf63112_spi_set_reset (&spi, &reset.reset, true);
  CHECK (bf63112_spi_open (&spi) == 0, "open with reset");
  CHECK (reset.opens == 1, "reset line opened");
  CHECK (reset.n_values == 2 && reset.values[0] == 1 && reset.values[1] == 0,
         "reset asserted then released");
  CHECK (clock.n_sleeps == 3 && clock.sleeps[0] == 5 &&
         clock.sleeps[1] == 10 && clock.sleeps[2] == 50,
         "reset timing 5/10/50 ms");
  bf63112_spi_close (&spi);
  CHECK (reset.closes == 1 && io.closed == 1, "close releases lines");

  /* reset is not pulsed without allow_reset */
  make_transport (&spi, &io, &clock, &policy);
  fake_reset_init (&reset);
  bf63112_spi_set_reset (&spi, &reset.reset, false);
  CHECK (bf63112_spi_open (&spi) == 0, "open without reset");
  CHECK (reset.opens == 0 && reset.n_values == 0, "reset gated by allow_reset");

  /* timed poll is not an observation: capture_wait times out 3 x 100 ms */
  make_transport (&spi, &io, &clock, &policy);
  memset (&poll_irq, 0, sizeof poll_irq);
  memset (&poll_state, 0, sizeof poll_state);
  CHECK (bf63112_spi_irq_poll_init (&poll_irq, &poll_state, fake_sleep,
                                    &clock) == 0, "poll IRQ init");
  bf63112_spi_set_irq (&spi, &poll_irq);
  CHECK (bf63112_spi_capture_wait (&spi, 100, 3, &event) ==
         BF63112_SPI_ERR_TIMEOUT, "poll capture_wait times out");
  CHECK (clock.n_sleeps == 5 && clock.sleeps[0] == 100 &&
         clock.sleeps[1] == 50 && clock.sleeps[2] == 100 &&
         clock.sleeps[3] == 50 && clock.sleeps[4] == 100,
         "3 x 100 ms with 50 ms gaps");
}

/* -------------------------------------------------------------- test: ACPI */

static size_t
append_large (uint8_t *buf, uint8_t name, const uint8_t *data, size_t len)
{
  buf[0] = (uint8_t) (0x80 | name);
  buf[1] = (uint8_t) (len & 0xFF);
  buf[2] = (uint8_t) ((len >> 8) & 0xFF);
  if (len > 0 && data != NULL)
    memcpy (buf + 3, data, len);
  return 3 + len;
}

static void
put16 (uint8_t *p, uint16_t value)
{
  p[0] = (uint8_t) (value & 0xFF);
  p[1] = (uint8_t) ((value >> 8) & 0xFF);
}

static void
put32 (uint8_t *p, uint32_t value)
{
  p[0] = (uint8_t) (value & 0xFF);
  p[1] = (uint8_t) ((value >> 8) & 0xFF);
  p[2] = (uint8_t) ((value >> 16) & 0xFF);
  p[3] = (uint8_t) ((value >> 24) & 0xFF);
}

static size_t
build_serial_bus (uint8_t *buf, int serial_type, uint32_t speed,
                  int bitlen, int phase, int polarity, int selection,
                  uint16_t type_flags, const char *source,
                  int type_data_length_override)
{
  uint8_t data[256];
  size_t source_len = source != NULL ? strlen (source) + 1 : 0;
  size_t td = serial_type == BF63112_ACPI_SERIAL_SPI ? 9 : 6;
  size_t pos;

  memset (data, 0, sizeof data);
  data[0] = 1;                                  /* revision */
  data[1] = source != NULL ? 1 : 0;             /* resource source index */
  data[2] = (uint8_t) serial_type;
  data[3] = 0;                                  /* general flags */
  put16 (data + 4, type_flags);
  data[6] = 1;                                  /* type revision */
  put16 (data + 7, (uint16_t) (type_data_length_override >= 0
                               ? type_data_length_override : (int) td));
  put32 (data + 9, speed);
  if (serial_type == BF63112_ACPI_SERIAL_SPI)
    {
      data[13] = (uint8_t) bitlen;
      data[14] = (uint8_t) phase;
      data[15] = (uint8_t) polarity;
      put16 (data + 16, (uint16_t) selection);
    }
  else
    put16 (data + 13, (uint16_t) selection);
  pos = 9 + td;
  if (source_len > 0)
    {
      memcpy (data + pos, source, source_len);
      pos += source_len;
    }
  return append_large (buf, BF63112_ACPI_LARGE_SERIAL_BUS, data, pos);
}

static size_t
build_gpio (uint8_t *buf, int connection_type, uint16_t flags,
            const uint16_t *pins, size_t n_pins, const char *source,
            int pin_config, uint16_t drive, uint16_t debounce,
            const uint8_t *vendor, size_t vendor_len,
            int pin_table_offset_override)
{
  uint8_t data[256];
  size_t source_len = source != NULL ? strlen (source) + 1 : 0;
  size_t pos = 20;
  size_t pin_table_off = pin_table_offset_override >= 0
                         ? (size_t) pin_table_offset_override : 3 + 20;
  size_t res_off, vendor_off = 0;
  size_t i;

  memset (data, 0, sizeof data);
  data[0] = 1;
  data[1] = (uint8_t) connection_type;
  put16 (data + 4, flags);
  data[6] = (uint8_t) pin_config;
  put16 (data + 7, drive);
  put16 (data + 9, debounce);
  put16 (data + 11, (uint16_t) pin_table_off);
  data[13] = source != NULL ? 1 : 0;
  for (i = 0; i < n_pins; i++)
    {
      put16 (data + pos, pins[i]);
      pos += 2;
    }
  res_off = source != NULL ? 3 + pos : 0;
  if (source_len > 0)
    {
      memcpy (data + pos, source, source_len);
      pos += source_len;
    }
  if (vendor != NULL && vendor_len > 0)
    {
      vendor_off = 3 + pos;
      memcpy (data + pos, vendor, vendor_len);
      pos += vendor_len;
    }
  put16 (data + 14, (uint16_t) res_off);
  put16 (data + 16, (uint16_t) vendor_off);
  put16 (data + 18, (uint16_t) vendor_len);
  return append_large (buf, BF63112_ACPI_LARGE_GPIO, data, pos);
}

static size_t
append_end_tag (uint8_t *buf)
{
  buf[0] = 0x79;
  buf[1] = 0x00;
  return 2;
}

static void
test_acpi_serial (void)
{
  uint8_t buf[512];
  size_t len = 0;
  struct bf63112_acpi_template tmpl;
  enum bf63112_acpi_error error;
  const struct bf63112_acpi_resource *spi;
  int mode;

  len += build_serial_bus (buf + len, BF63112_ACPI_SERIAL_SPI, 24000000,
                           8, 1, 1, 2, 0x0001, "\\_SB.PC00.SPI1", -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "serial parses");
  CHECK (tmpl.n_resources == 1 && tmpl.has_end_tag, "one resource + EndTag");
  spi = bf63112_acpi_first_spi (&tmpl);
  CHECK (spi != NULL, "SPI descriptor found");
  CHECK (spi != NULL && spi->offset == 0 && spi->length == len - 2,
         "offset/length");
  CHECK (spi != NULL && spi->connection_speed_hz == 24000000, "speed");
  CHECK (spi != NULL && spi->data_bit_length == 8, "bit length");
  CHECK (spi != NULL && spi->clock_phase == 1 && spi->clock_polarity == 1,
         "phase/polarity");
  CHECK (spi != NULL && spi->device_selection == 2, "device selection");
  CHECK (spi != NULL && strcmp (spi->resource_source, "\\_SB.PC00.SPI1") == 0,
         "resource source");
  CHECK (spi != NULL && spi->wire_mode_three && !spi->device_polarity_high,
         "SPI type flags");
  mode = bf63112_acpi_spi_mode (spi);
  CHECK (mode == 3, "SPI mode 3");
  CHECK (spi != NULL && bf63112_acpi_spi_mode (spi) == 3, "mode helper");

  /* no resource source */
  len = build_serial_bus (buf, BF63112_ACPI_SERIAL_SPI, 1000000, 8, 0, 0, 0,
                          0, NULL, -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "no-source parse");
  spi = bf63112_acpi_first_spi (&tmpl);
  CHECK (spi != NULL && !spi->has_resource_source, "no source reported");
  CHECK (spi != NULL && bf63112_acpi_spi_mode (spi) == 0, "SPI mode 0");

  /* I2C is decoded but is not SPI */
  len = build_serial_bus (buf, BF63112_ACPI_SERIAL_I2C, 400000, 0, 0, 0,
                          0x50, 0, NULL, -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "I2C parse");
  CHECK (tmpl.n_resources == 1 &&
         tmpl.resources[0].kind == BF63112_ACPI_KIND_SERIAL_BUS,
         "I2C kind");
  CHECK (tmpl.resources[0].serial_type == BF63112_ACPI_SERIAL_I2C,
         "I2C type");
  CHECK (tmpl.resources[0].connection_speed_hz == 400000, "I2C speed");
  CHECK (tmpl.resources[0].device_selection == 0x50, "I2C address");
  CHECK (bf63112_acpi_first_spi (&tmpl) == NULL, "I2C is not SPI");
}

static void
test_acpi_gpio (void)
{
  uint8_t buf[512];
  size_t len;
  struct bf63112_acpi_template tmpl;
  enum bf63112_acpi_error error;
  const struct bf63112_acpi_resource *gpio;
  uint16_t pins[2];
  size_t i;

  pins[0] = 19;
  len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_INT,
                    BF63112_ACPI_GPIO_TRIGGERING_MASK |
                    (1u << BF63112_ACPI_GPIO_POLARITY_SHIFT),
                    pins, 1, "\\_SB.GPI0", 0, 0, 0, NULL, 0, -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "gpio int parses");
  gpio = bf63112_acpi_first_gpio (&tmpl, BF63112_ACPI_GPIO_TYPE_INT);
  CHECK (gpio != NULL, "GpioInt found");
  CHECK (gpio != NULL && gpio->n_pins == 1 && gpio->pins[0] == 19, "pin 19");
  CHECK (gpio != NULL && gpio->triggering == BF63112_ACPI_TRIGGERING_EDGE,
         "edge triggered");
  CHECK (gpio != NULL && gpio->polarity == BF63112_ACPI_POLARITY_ACTIVE_LOW,
         "active low");
  CHECK (gpio != NULL && !gpio->shareable && !gpio->wake_capable,
         "share/wake false");
  CHECK (gpio != NULL && strcmp (gpio->resource_source, "\\_SB.GPI0") == 0,
         "gpio source");

  /* level, active-both, shareable + wake; two pins */
  pins[0] = 3;
  pins[1] = 4;
  len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_INT,
                    (2u << BF63112_ACPI_GPIO_POLARITY_SHIFT) |
                    BF63112_ACPI_GPIO_SHAREABLE |
                    BF63112_ACPI_GPIO_WAKE_CAPABLE,
                    pins, 2, "\\_SB.GPI0", 0, 0, 0, NULL, 0, -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "level parse");
  gpio = bf63112_acpi_first_gpio (&tmpl, BF63112_ACPI_GPIO_TYPE_INT);
  CHECK (gpio != NULL && gpio->triggering == BF63112_ACPI_TRIGGERING_LEVEL,
         "level");
  CHECK (gpio != NULL && gpio->polarity == BF63112_ACPI_POLARITY_ACTIVE_BOTH,
         "both");
  CHECK (gpio != NULL && gpio->shareable && gpio->wake_capable, "share/wake");
  CHECK (gpio != NULL && gpio->n_pins == 2 && gpio->pins[1] == 4, "two pins");

  /* pin config / drive / debounce */
  pins[0] = 1;
  len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_INT,
                    BF63112_ACPI_GPIO_TRIGGERING_MASK, pins, 1, NULL, 3,
                    0x20, 0x0A, NULL, 0, -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "attrs parse");
  gpio = bf63112_acpi_first_gpio (&tmpl, BF63112_ACPI_GPIO_TYPE_INT);
  CHECK (gpio != NULL && gpio->pin_config == 3 && gpio->drive_strength == 0x20 &&
         gpio->debounce_ms == 0x0A, "pin config/drive/debounce");

  /* GpioIo restriction uses bits 5-6, never 0-1 */
  pins[0] = 2;
  for (i = 0; i < 4; i++)
    {
      struct bf63112_acpi_spi_connection connection;

      len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_IO,
                        (uint16_t) (i << BF63112_ACPI_GPIO_IO_RESTRICTION_SHIFT),
                        pins, 1, "\\_SB.GPI0", 0, 0, 0, NULL, 0, -1);
      len += append_end_tag (buf + len);
      CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0,
             "gpio io parse");
      gpio = bf63112_acpi_first_gpio (&tmpl, BF63112_ACPI_GPIO_TYPE_IO);
      CHECK (gpio != NULL && gpio->io_restriction ==
             (enum bf63112_acpi_io_restriction) (BF63112_ACPI_IO_NONE + i),
             "io restriction %zu", i);
      memset (&connection, 0, sizeof connection);
      bf63112_acpi_spi_connection (&tmpl, &connection);
      CHECK (connection.has_reset_pin && connection.reset_pin == 2,
             "GpioIo reset pin");
    }
  /* Low two bits must stay NONE for GpioIo. */
  len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_IO, 0x0006, pins, 1, NULL, 0,
                    0, 0, NULL, 0, -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "low-bit parse");
  gpio = bf63112_acpi_first_gpio (&tmpl, BF63112_ACPI_GPIO_TYPE_IO);
  CHECK (gpio != NULL && gpio->io_restriction == BF63112_ACPI_IO_NONE,
         "low flag bits are not the restriction");

  /* vendor data after the source string is skipped */
  pins[0] = 7;
  {
    const uint8_t vendor[3] = { 1, 2, 3 };

    len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_INT,
                      BF63112_ACPI_GPIO_TRIGGERING_MASK, pins, 1,
                      "\\_SB.GPI0", 0, 0, 0, vendor, sizeof vendor, -1);
  }
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "vendor parse");
  gpio = bf63112_acpi_first_gpio (&tmpl, BF63112_ACPI_GPIO_TYPE_INT);
  CHECK (gpio != NULL && gpio->n_pins == 1 && gpio->pins[0] == 7,
         "vendor skipped");
}

static void
test_acpi_other_and_malformed (void)
{
  uint8_t buf[512];
  size_t len;
  struct bf63112_acpi_template tmpl;
  enum bf63112_acpi_error error;
  uint8_t data[64];
  uint16_t pins[1] = { 2 };

  /* Extended IRQ */
  data[0] = 5;
  data[1] = 2;
  put32 (data + 2, 7);
  put32 (data + 6, 9);
  len = append_large (buf, BF63112_ACPI_LARGE_EXTENDED_IRQ, data, 10);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "ext irq parses");
  CHECK (tmpl.n_resources == 1 &&
         tmpl.resources[0].kind == BF63112_ACPI_KIND_EXTENDED_IRQ,
         "ext irq kind");
  CHECK (tmpl.resources[0].irq_flags == 5 && tmpl.resources[0].n_irqs == 2 &&
         tmpl.resources[0].irqs[0] == 7 && tmpl.resources[0].irqs[1] == 9,
         "ext irq fields");

  /* Small IRQ */
  put16 (data, 0x0005);
  data[2] = 0x0D;
  len = 0;
  buf[len++] = (uint8_t) ((BF63112_ACPI_SMALL_IRQ << 3) | 3);
  memcpy (buf + len, data, 3);
  len += 3;
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "small irq parses");
  CHECK (tmpl.resources[0].kind == BF63112_ACPI_KIND_IRQ &&
         tmpl.resources[0].n_irqs == 2 && tmpl.resources[0].irqs[0] == 0 &&
         tmpl.resources[0].irqs[1] == 2 && tmpl.resources[0].irq_flags == 0x0D,
         "small irq fields");

  /* Opaque items keep offsets and names; bytes after EndTag are preserved. */
  len = 0;
  buf[len++] = (uint8_t) ((BF63112_ACPI_SMALL_IO << 3) | 7);
  memset (buf + len, 0, 7);
  len += 7;
  len += append_large (buf + len, BF63112_ACPI_LARGE_VENDOR,
                       (const uint8_t *) "abc", 3);
  len += append_end_tag (buf + len);
  buf[len++] = 0xDE;
  buf[len++] = 0xAD;
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "opaque parses");
  CHECK (tmpl.n_resources == 2, "two opaque resources");
  CHECK (strcmp (tmpl.resources[0].name, "io") == 0 &&
         tmpl.resources[0].offset == 0, "io name/offset");
  CHECK (strcmp (tmpl.resources[1].name, "vendor") == 0 &&
         tmpl.resources[1].offset == 8, "vendor name/offset");
  CHECK (tmpl.has_end_tag && tmpl.trailing_length == 2 &&
         tmpl.trailing_offset == len - 2, "trailing preserved");

  /* Missing EndTag is tolerated. */
  len = build_serial_bus (buf, BF63112_ACPI_SERIAL_SPI, 1000000, 8, 0, 0, 0,
                          0, NULL, -1);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "no EndTag");
  CHECK (!tmpl.has_end_tag && tmpl.trailing_length == 0 &&
         tmpl.n_resources == 1, "missing EndTag state");

  /* Empty template. */
  CHECK (bf63112_acpi_parse (buf, 0, &tmpl, &error) == 0, "empty template");
  CHECK (tmpl.n_resources == 0, "empty resource list");

  /* Truncated large header. */
  buf[0] = 0x8E;
  buf[1] = 0x01;
  CHECK (bf63112_acpi_parse (buf, 2, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "truncated large header");
  /* Large item length overruns the buffer. */
  buf[0] = 0x8E;
  buf[1] = 0x05;
  buf[2] = 0x00;
  CHECK (bf63112_acpi_parse (buf, 4, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "large length overrun");
  /* Truncated small item. */
  buf[0] = (uint8_t) ((BF63112_ACPI_SMALL_IO << 3) | 7);
  CHECK (bf63112_acpi_parse (buf, 4, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "small item truncated");
  /* Truncated EndTag. */
  buf[0] = 0x79;
  CHECK (bf63112_acpi_parse (buf, 1, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "EndTag truncated");

  /* SPI type-data too short. */
  len = build_serial_bus (buf, BF63112_ACPI_SERIAL_SPI, 1, 8, 0, 0, 0, 0,
                          NULL, 8);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "SPI type data too short");
  /* SerialBus body shorter than the common header. */
  len = append_large (buf, BF63112_ACPI_LARGE_SERIAL_BUS, data, 5);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "SerialBus body short");
  /* Resource-source offset outside the descriptor. */
  memset (data, 0, sizeof data);
  data[0] = 1;                                  /* revision */
  data[1] = 1;                                  /* resource source index */
  data[2] = BF63112_ACPI_SERIAL_SPI;
  data[6] = 1;                                  /* type revision */
  put16 (data + 7, 0x00FF);                     /* type data length */
  len = append_large (buf, BF63112_ACPI_LARGE_SERIAL_BUS, data, 18);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_BAD_SOURCE, "source outside");

  /* GPIO body too short. */
  len = append_large (buf, BF63112_ACPI_LARGE_GPIO, data, 5);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "GPIO body short");
  /* GPIO pin table odd length: point the table at one byte before the end. */
  len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_INT,
                    BF63112_ACPI_GPIO_TRIGGERING_MASK, pins, 1, NULL, 0, 0, 0,
                    NULL, 0, 3 + 21);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_BAD_PIN_TABLE, "odd pin table");
  /* GPIO pin table out of range. */
  len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_INT,
                    BF63112_ACPI_GPIO_TRIGGERING_MASK, pins, 1, NULL, 0, 0, 0,
                    NULL, 0, 3 + 200);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_BAD_PIN_TABLE, "pin table out of range");
  /* GPIO resource-source out of range. */
  len = build_gpio (buf, BF63112_ACPI_GPIO_TYPE_INT,
                    BF63112_ACPI_GPIO_TRIGGERING_MASK, pins, 1, "S", 0, 0, 0,
                    NULL, 0, 0);
  put16 (buf + 3 + 14, 0xFF);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_BAD_SOURCE, "GPIO source outside");

  /* Extended IRQ truncated. */
  data[0] = 0;
  data[1] = 3;
  put32 (data + 2, 1);
  len = append_large (buf, BF63112_ACPI_LARGE_EXTENDED_IRQ, data, 6);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) < 0 &&
         error == BF63112_ACPI_ERR_TRUNCATED, "ext irq truncated");
}

static void
test_acpi_connection (void)
{
  uint8_t buf[512];
  size_t len = 0;
  struct bf63112_acpi_template tmpl;
  enum bf63112_acpi_error error;
  struct bf63112_acpi_spi_connection connection;
  uint16_t pins[1];

  pins[0] = 42;
  len += build_serial_bus (buf + len, BF63112_ACPI_SERIAL_SPI, 12000000, 8,
                           0, 1, 0, 0, "\\_SB.PC00.SPI1", -1);
  len += build_gpio (buf + len, BF63112_ACPI_GPIO_TYPE_INT,
                     BF63112_ACPI_GPIO_TRIGGERING_MASK |
                     (1u << BF63112_ACPI_GPIO_POLARITY_SHIFT) |
                     BF63112_ACPI_GPIO_SHAREABLE,
                     pins, 1, "\\_SB.GPI0", 0, 0, 0, NULL, 0, -1);
  pins[0] = 9;
  len += build_gpio (buf + len, BF63112_ACPI_GPIO_TYPE_IO, 0, pins, 1, NULL,
                     0, 0, 0, NULL, 0, -1);
  len += append_end_tag (buf + len);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "connection parse");
  bf63112_acpi_spi_connection (&tmpl, &connection);
  CHECK (connection.has_controller &&
         strcmp (connection.controller, "\\_SB.PC00.SPI1") == 0, "controller");
  CHECK (connection.has_speed_hz && connection.speed_hz == 12000000, "speed");
  CHECK (connection.has_mode && connection.mode == 2, "mode 2");
  CHECK (connection.has_bits_per_word && connection.bits_per_word == 8,
         "bits 8");
  CHECK (connection.has_chip_select && connection.chip_select == 0, "CS 0");
  CHECK (connection.has_irq_pin && connection.irq_pin == 42, "IRQ pin 42");
  CHECK (connection.irq_active_low == 1, "IRQ active low");
  CHECK (connection.irq_edge == 0, "logical edge rising");
  CHECK (connection.has_irq_shareable && connection.irq_shareable, "shareable");
  CHECK (connection.has_reset_pin && connection.reset_pin == 9, "reset pin 9");

  /* Empty template: everything stays unset, active_low unknown. */
  len = append_end_tag (buf);
  CHECK (bf63112_acpi_parse (buf, len, &tmpl, &error) == 0, "empty connection");
  bf63112_acpi_spi_connection (&tmpl, &connection);
  CHECK (!connection.has_controller && !connection.has_speed_hz &&
         !connection.has_mode && connection.irq_active_low == -1,
         "empty connection fields");
  CHECK (bf63112_acpi_gpio_count (&tmpl, BF63112_ACPI_GPIO_TYPE_INT) == 0,
         "gpio count 0");
}

/* ---------------------------------------------------------- test: discovery */

static int
make_dir (const char *path)
{
  return mkdir (path, 0700);
}

static int
write_text (const char *path, const char *text)
{
  FILE *file = fopen (path, "w");

  if (file == NULL)
    return -1;
  fputs (text, file);
  fclose (file);
  return 0;
}

static void
test_discovery (void)
{
  char root[] = "/tmp/bf63112-test-XXXXXX";
  char path[512];
  char out[256];
  int rc;

  if (mkdtemp (root) == NULL)
    {
      CHECK (0, "mkdtemp failed");
      return;
    }
  snprintf (path, sizeof path, "%s/class", root);
  CHECK (make_dir (path) == 0, "mkdir class");
  snprintf (path, sizeof path, "%s/class/spidev", root);
  CHECK (make_dir (path) == 0, "mkdir spidev class");
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.0", root);
  CHECK (make_dir (path) == 0, "mkdir spidev0.0");
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.0/device", root);
  CHECK (make_dir (path) == 0, "mkdir device");
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.0/device/modalias",
            root);
  CHECK (write_text (path, "acpi:BF63112:\n") == 0, "write modalias");

  rc = bf63112_discover_spidev (root, "/dev", NULL, out, sizeof out);
  CHECK (rc == 0 && strcmp (out, "/dev/spidev0.0") == 0, "discover one");
  rc = bf63112_discover_spidev (root, "/dev", "/dev/spidev9.9", out,
                                sizeof out);
  CHECK (rc == 0 && strcmp (out, "/dev/spidev9.9") == 0, "override wins");
  rc = bf63112_discover_spidev ("/tmp/definitely-not-here", "/dev", NULL, out,
                                sizeof out);
  CHECK (rc == -ENOENT, "missing class dir -> -ENOENT");

  /* A second matching device makes the lookup ambiguous. */
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.1", root);
  CHECK (make_dir (path) == 0, "mkdir spidev0.1");
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.1/device", root);
  CHECK (make_dir (path) == 0, "mkdir device 2");
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.1/device/modalias",
            root);
  CHECK (write_text (path, "acpi:BF63112:\n") == 0, "write modalias 2");
  rc = bf63112_discover_spidev (root, "/dev", NULL, out, sizeof out);
  CHECK (rc == -ENOTUNIQ, "two matches -> -ENOTUNIQ");

  /* Clean up. */
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.1/device/modalias",
            root);
  unlink (path);
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.1/device", root);
  rmdir (path);
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.1", root);
  rmdir (path);
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.0/device/modalias",
            root);
  unlink (path);
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.0/device", root);
  rmdir (path);
  snprintf (path, sizeof path, "%s/class/spidev/spidev0.0", root);
  rmdir (path);
  snprintf (path, sizeof path, "%s/class/spidev", root);
  rmdir (path);
  snprintf (path, sizeof path, "%s/class", root);
  rmdir (path);
  rmdir (root);
}

int
main (void)
{
  test_selector ();
  test_policy ();
  test_plans_and_timeouts ();
  test_frame_builder ();
  test_execute ();
  test_reset_and_irq_helpers ();
  test_acpi_serial ();
  test_acpi_gpio ();
  test_acpi_other_and_malformed ();
  test_acpi_connection ();
  test_discovery ();

  printf ("test_spi: %u checks, %u failures\n", checks, failures);
  return failures == 0 ? 0 : 1;
}
