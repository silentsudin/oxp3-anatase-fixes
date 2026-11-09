/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_spi.h - dependency-free BF63112 SPI (ACPI/SPB) transport.
 *
 * C mirror of tools/fingerprint/fpbf63112/spi.py, which was recovered from the
 * ACPI/SPB branch of BetterlifeFingerprintDevice.dll 9.39.15.26 and is
 * documented in docs/hw/fingerprint-transport.md §4/§8.  Only the C library
 * and Linux syscalls are used: no libgpiod, no libfprint, no spidev helper
 * library.  libgpiod v2/v1 IRQ/reset backends are compiled in only when
 * BF63112_HAVE_GPIOD_V2 / BF63112_HAVE_GPIOD_V1 is defined; the default
 * runtime fallback is the sysfs GPIO ABI (and, only as a last resort, a timed
 * poll that cannot observe the line).
 *
 * Wire shape (one transaction):
 *
 *   selector = (opcode & 0x3f) << 1            host reads from the device
 *   selector = 0x80 | ((opcode & 0x3f) << 1)   host writes to the device
 *
 *   read : SPI_IOC_MESSAGE(2) = { tx selector, 0 B } { rx N B }
 *   write: SPI_IOC_MESSAGE(2) = { tx selector, 0 B } { tx payload, 0 B }
 *
 * There is no length field and no status byte on the wire: a read clocks out
 * exactly the caller's read_len.  The selector loses opcode bits 7..6, so this
 * layer refuses destructive commands by opcode *and* by low selector (0x02 is
 * the write-visible alias of JTAG 0xC2, for example).  Nothing talks to
 * hardware until bf63112_spi_open()/bf63112_spi_execute(); the unit test
 * injects a fake I/O vtable and never opens /dev/spidev.
 */

#ifndef BF63112_SPI_H
#define BF63112_SPI_H

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Recovered timings, milliseconds (spi.py "Recovered timings"). */
#define BF63112_SPI_XFER_TIMEOUT_MS       200u
#define BF63112_SPI_CMD_TIMEOUT_MS       1000u
#define BF63112_SPI_CMD_TOTAL_MS         2030u
#define BF63112_SPI_IMAGE_TIMEOUT_MS     5000u
#define BF63112_SPI_IMAGE_TOTAL_MS      10010u
#define BF63112_SPI_RESTORE_TIMEOUT_MS   2000u
#define BF63112_SPI_RESTORE_TOTAL_MS     4016u
#define BF63112_SPI_CANCEL_SETTLE_MS      150u
#define BF63112_SPI_RESET_OPEN_MS          50u
#define BF63112_SPI_RESET_ASSERT_MS         5u
#define BF63112_SPI_RESET_RELEASE_MS       10u
#define BF63112_SPI_IRQ_WAIT_MS           100u
#define BF63112_SPI_IRQ_ATTEMPTS            3u
#define BF63112_SPI_IRQ_RETRY_SLEEP_MS     50u

/* Largest transfer count this transport builds (selector + payload/clock). */
#define BF63112_SPI_MAX_TRANSFERS 2u

/* Read-only opcodes a probe may use, mirroring SAFE_READ_OPS (USB table on
 * SPI are deliberate read-only experiments, plus recovered register 0x2d). */
#define BF63112_SPI_OP_FIRMWARE_VER    0xA0u
#define BF63112_SPI_OP_MODULE_SCHEME  0xA1u
#define BF63112_SPI_OP_FINGER_COUNT   0xB3u
#define BF63112_SPI_OP_MODULE_UID     0xC0u
#define BF63112_SPI_OP_SENSOR_INFO    0xC1u
#define BF63112_SPI_OP_STATUS_REG     0x2Du

/* Linux spidev ABI (linux/spi/spidev.h) used by the real backend, duplicated
 * here so the standalone test and the driver need no kernel UAPI headers.
 * These are the _IOW encodings for 32-bit-direction architectures; the unit
 * test pins them against the values spi.py uses. */
#define BF63112_SPI_IOC_WR_MODE          0x40016B01UL
#define BF63112_SPI_IOC_WR_BITS_PER_WORD 0x40016B03UL
#define BF63112_SPI_IOC_WR_MAX_SPEED_HZ  0x40046B04UL
#define BF63112_SPI_IOC_MESSAGE(n) \
  ((unsigned long) ((1UL << 30) | (((unsigned long) (n) * 32UL) << 16) | \
                    (0x6BUL << 8)))

struct bf63112_spi;            /* defined with the transport below */

/* ------------------------------------------------------------------ errors */

/* Negative errno-style values returned by this module. */
#define BF63112_SPI_ERR_INVALID   (-EINVAL)
#define BF63112_SPI_ERR_REFUSED   (-EPERM)   /* destructive opcode/selector */
#define BF63112_SPI_ERR_NOT_ALLOWED (-EACCES) /* not on the allowlist / no opt-in */
#define BF63112_SPI_ERR_IO        (-EIO)
#define BF63112_SPI_ERR_TIMEOUT   (-ETIMEDOUT)
#define BF63112_SPI_ERR_CANCELLED (-ECANCELED)
#define BF63112_SPI_ERR_NO_IRQ    (-ENODEV)
#define BF63112_SPI_ERR_NO_SPACE  (-ENOSPC)

/* --------------------------------------------------------------- selector */

/*! One-byte selector for @opcode.  @write sets bit 7 (host -> device). */
uint8_t bf63112_spi_selector (uint8_t opcode, bool write);

/*! Selector with the direction bit cleared; this is what the gate checks. */
uint8_t bf63112_spi_selector_low (uint8_t opcode);

/*! TRUE for enrol/identify/remove/JTAG/restore/firmware-update/mode-switch. */
bool bf63112_spi_opcode_is_destructive (uint8_t opcode);

/*! Destructive-command name ("ENROLL_FINGERPRINT", ...) or NULL. */
const char *bf63112_spi_destructive_name (uint8_t opcode);

/*! TRUE when the low seven selector bits are reserved for a destructive op. */
bool bf63112_spi_selector_is_forbidden (uint8_t selector);

/*! Comma-separated destructive aliases for a forbidden selector, or NULL. */
const char *bf63112_spi_selector_aliases (uint8_t selector);

/* ------------------------------------------------------------------ policy */

/*! 256-bit opcode allowlist plus the caller's explicit I/O opt-in. */
struct bf63112_spi_policy
{
  bool allow_io;                 /*: Python allow_writes; gates reads too */
  uint64_t read_allow[4];        /*: opcode bitmap, default read-only set */
  uint64_t write_allow[4];       /*: opcode bitmap, empty by default */
};

void bf63112_spi_policy_init (struct bf63112_spi_policy *policy);
void bf63112_spi_policy_allow_read (struct bf63112_spi_policy *policy,
                                    uint8_t opcode);
void bf63112_spi_policy_allow_write (struct bf63112_spi_policy *policy,
                                     uint8_t opcode);
bool bf63112_spi_policy_read_allowed (const struct bf63112_spi_policy *policy,
                                      uint8_t opcode);
bool bf63112_spi_policy_write_allowed (const struct bf63112_spi_policy *policy,
                                       uint8_t opcode);

/*!
 * The defence-in-depth gate.  Returns 0, BF63112_SPI_ERR_REFUSED for a
 * destructive opcode or a selector alias of one (even if explicitly
 * allowlisted), BF63112_SPI_ERR_NOT_ALLOWED when @policy->allow_io is false or
 * the opcode is not on the direction's allowlist, BF63112_SPI_ERR_INVALID for
 * a NULL policy.
 */
int bf63112_spi_policy_check (const struct bf63112_spi_policy *policy,
                              uint8_t opcode, bool write);

/* --------------------------------------------------------------- transfers */

struct bf63112_spi_transfer
{
  const uint8_t *tx;             /*: NULL for a receive-only transfer */
  size_t tx_len;
  uint8_t *rx;                   /*: NULL for a transmit-only transfer */
  size_t rx_len;
  uint32_t speed_hz;             /*: 0 = controller default */
  uint16_t delay_us;
};

/*! One fully-determined transaction; no I/O happens while building it. */
struct bf63112_spi_plan
{
  uint8_t  opcode;
  bool     write;
  uint16_t read_len;             /*: bytes the device clocks out (read only) */
  uint16_t timeout_ms;           /*: per-attempt deadline */
  uint16_t total_ms;             /*: outer deadline */
  uint8_t  selector;
  uint8_t  selector_byte;        /*: storage for transfer[0].tx */
  struct bf63112_spi_transfer transfers[BF63112_SPI_MAX_TRANSFERS];
  size_t   n_transfers;
};

/*! Recovered (per-attempt, total) timeouts for @opcode. */
void bf63112_spi_default_timeouts (uint8_t opcode,
                                   uint16_t *timeout_ms, uint16_t *total_ms);

/*!
 * Build a read plan.  Refuses (via bf63112_spi_policy_check) destructive and
 * non-allowlisted opcodes; @timeout_ms/@total_ms of 0 select the recovered
 * defaults.  The receive buffer is attached by bf63112_spi_execute().
 */
int bf63112_spi_build_read (const struct bf63112_spi_policy *policy,
                            uint8_t opcode, uint16_t read_len,
                            uint16_t timeout_ms, uint16_t total_ms,
                            struct bf63112_spi_plan *plan);

/*! Build a write plan; same gate, and the payload pointer is borrowed. */
int bf63112_spi_build_write (const struct bf63112_spi_policy *policy,
                             uint8_t opcode, const uint8_t *payload,
                             size_t payload_len,
                             uint16_t timeout_ms, uint16_t total_ms,
                             struct bf63112_spi_plan *plan);

/*! Structural validation of a plan (also used before submission). */
int bf63112_spi_plan_validate (const struct bf63112_spi_plan *plan);

/* --------------------------------------------------------------- deadline */

enum bf63112_spi_deadline
{
  BF63112_SPI_DEADLINE_OK = 0,
  BF63112_SPI_DEADLINE_ATTEMPT,  /*: elapsed > plan->timeout_ms */
  BF63112_SPI_DEADLINE_TOTAL,    /*: elapsed > plan->total_ms (wins) */
};

/*!
 * Pure timeout arithmetic, in milliseconds.  Mirrors spi.py
 * _check_deadline(): the total bound is reported first when both fired.
 */
enum bf63112_spi_deadline
bf63112_spi_deadline_check (const struct bf63112_spi_plan *plan,
                            uint64_t elapsed_ms);

/*! Remaining IRQ budget: min(per-attempt, total) - elapsed; <= 0 means none. */
int64_t bf63112_spi_irq_budget (const struct bf63112_spi_plan *plan,
                                uint64_t elapsed_ms);

/* ------------------------------------------------------------------- I/O */

/*! Injectable spidev operations; the unit test supplies a fake vtable. */
struct bf63112_spi_io
{
  int (*open) (void *priv);
  int (*close) (void *priv);
  /*! @speed_hz/@mode/@bits_per_word of -1 means "leave the kernel default". */
  int (*configure) (void *priv, int speed_hz, int mode, int bits_per_word);
  /*! Submit one SPI_IOC_MESSAGE; *bytes_done receives the byte count. */
  int (*message) (void *priv,
                  const struct bf63112_spi_transfer *transfers,
                  size_t n_transfers, size_t *bytes_done);
};

/*! Real /dev/spidev backend state (defined in bf63112_spi.c). */
struct bf63112_linux_spi
{
  char path[256];
  int fd;
};

/*!
 * Initialise @spi with the real spidev backend for @path.  No file is opened
 * here; bf63112_spi_open() does that.  @path must not be NULL/empty.
 */
int bf63112_spi_linux_init (struct bf63112_spi *spi,
                            struct bf63112_linux_spi *state,
                            const char *path);

/* --------------------------------------------------------------- IRQ line */

enum bf63112_spi_irq_edge
{
  BF63112_SPI_IRQ_RISING = 0,
  BF63112_SPI_IRQ_FALLING,
  BF63112_SPI_IRQ_BOTH,
};

enum bf63112_spi_irq_backend
{
  BF63112_SPI_IRQ_AUTO = 0,
  BF63112_SPI_IRQ_GPIOD,
  BF63112_SPI_IRQ_SYSFS,
  BF63112_SPI_IRQ_POLL,
};

enum bf63112_spi_irq_source
{
  BF63112_SPI_IRQ_SOURCE_NONE = 0,
  BF63112_SPI_IRQ_SOURCE_GPIOD,
  BF63112_SPI_IRQ_SOURCE_SYSFS,
  BF63112_SPI_IRQ_SOURCE_POLL,
};

struct bf63112_spi_irq_event
{
  enum bf63112_spi_irq_source source;
  bool observed;                 /*: false only for the timed-poll fallback */
  int edge;                      /*: -1 unknown, 0 falling, 1 rising */
  int value;                     /*: -1 unknown, else logical line level */
  uint64_t elapsed_ms;
};

struct bf63112_spi_irq
{
  int (*open) (struct bf63112_spi_irq *irq);
  int (*wait) (struct bf63112_spi_irq *irq, unsigned int timeout_ms,
               struct bf63112_spi_irq_event *event);
  void (*close) (struct bf63112_spi_irq *irq);
  void *priv;
};

/*! Sysfs edge backend (writes export/direction/edge, reads value). */
struct bf63112_sysfs_irq
{
  char base[256];
  int  line;
  int  edge;
  bool active_low;
  bool hw_active_low;
  bool exported_here;
  int  value_fd;
  int  last_value;
};

int bf63112_spi_irq_sysfs_init (struct bf63112_spi_irq *irq,
                                struct bf63112_sysfs_irq *state,
                                int line, int edge, bool active_low,
                                const char *sysfs_base);

/*! Timed-poll fallback that cannot observe the line (observed = false). */
struct bf63112_poll_irq
{
  void (*sleep_ms) (unsigned int ms, void *ctx);
  void *sleep_ctx;
};

int bf63112_spi_irq_poll_init (struct bf63112_spi_irq *irq,
                               struct bf63112_poll_irq *state,
                               void (*sleep_ms) (unsigned int, void *),
                               void *sleep_ctx);

/*
 * libgpiod backends are compiled in only when the build defines
 * BF63112_HAVE_GPIOD_V2 or BF63112_HAVE_GPIOD_V1 (and links -lgpiod).
 * BF63112_HAVE_GPIOD_V2 wins when both are defined.  @chip is a gpiochip
 * device path such as "/dev/gpiochip0".
 */
#if defined(BF63112_HAVE_GPIOD_V2) || defined(BF63112_HAVE_GPIOD_V1)
struct bf63112_gpiod_irq;
int bf63112_spi_irq_gpiod_init (struct bf63112_spi_irq *irq,
                                struct bf63112_gpiod_irq **state,
                                int line, int edge, bool active_low,
                                const char *chip);
#endif

/* ------------------------------------------------------------ reset line */

struct bf63112_spi_reset
{
  int (*open) (struct bf63112_spi_reset *reset);
  /*! Drive the logical value (1 = asserted/reset, 0 = idle). */
  int (*set) (struct bf63112_spi_reset *reset, int logical);
  void (*close) (struct bf63112_spi_reset *reset);
  void *priv;
  bool active_low;
};

/*! Sysfs output backend for the reset line. */
struct bf63112_sysfs_reset
{
  char base[256];
  int  line;
  bool active_low;
  bool hw_active_low;
  bool exported_here;
};

int bf63112_spi_reset_sysfs_init (struct bf63112_spi_reset *reset,
                                  struct bf63112_sysfs_reset *state,
                                  int line, bool active_low,
                                  const char *sysfs_base);

#if defined(BF63112_HAVE_GPIOD_V2) || defined(BF63112_HAVE_GPIOD_V1)
struct bf63112_gpiod_reset;
int bf63112_spi_reset_gpiod_init (struct bf63112_spi_reset *reset,
                                  struct bf63112_gpiod_reset **state,
                                  int line, bool active_low,
                                  const char *chip);
#endif

/* ----------------------------------------------------------------- transport */

struct bf63112_spi
{
  const struct bf63112_spi_io *io;
  void *io_priv;

  int  speed_hz;                 /*: -1 = leave the kernel default */
  int  mode;                     /*: -1 = leave */
  int  bits_per_word;            /*: -1 = leave */

  bool opened;
  bool configured;
  bool cancel_requested;

  struct bf63112_spi_irq *irq;
  struct bf63112_spi_reset *reset;
  bool allow_reset;

  uint64_t (*now_ms) (void *ctx);
  void *now_ctx;
  void (*sleep_ms) (unsigned int ms, void *ctx);
  void *sleep_ctx;

  bool have_last_irq;
  struct bf63112_spi_irq_event last_irq;
};

/*!
 * Initialise a transport.  @io/@io_priv are borrowed; @io may be NULL only
 * until bf63112_spi_linux_init() replaces it.  No I/O happens here.
 */
void bf63112_spi_init (struct bf63112_spi *spi,
                       const struct bf63112_spi_io *io, void *io_priv);

/*! Override the monotonic clock and sleep function (tests, mainly). */
void bf63112_spi_set_clock (struct bf63112_spi *spi,
                            uint64_t (*now_ms) (void *ctx), void *now_ctx,
                            void (*sleep_ms) (unsigned int ms, void *ctx),
                            void *sleep_ctx);

/*! Attach optional lines.  The transport never owns/frees them. */
void bf63112_spi_set_irq (struct bf63112_spi *spi,
                          struct bf63112_spi_irq *irq);
void bf63112_spi_set_reset (struct bf63112_spi *spi,
                            struct bf63112_spi_reset *reset,
                            bool allow_reset);

/*!
 * Open and configure the spidev node.  If a reset line is configured and
 * @allow_reset is set, pulse it (5 ms asserted, 10 ms released) and sleep the
 * recovered 50 ms after the reset target is opened.  Returns 0 or a negative
 * BF63112_SPI_ERR_*; on failure the backend is closed again.
 */
int bf63112_spi_open (struct bf63112_spi *spi);
void bf63112_spi_close (struct bf63112_spi *spi);

/*! Execute one plan.  A read returns exactly plan->read_len bytes in @answer. */
int bf63112_spi_execute (struct bf63112_spi *spi,
                         const struct bf63112_spi_plan *plan,
                         uint8_t *answer, size_t answer_size,
                         size_t *answer_len);

/*! Cancel policy: mark, then sleep the recovered 150 ms settle. */
void bf63112_spi_cancel (struct bf63112_spi *spi);
void bf63112_spi_request_cancel (struct bf63112_spi *spi);

/*! Wait for one interrupt (no SPI traffic). */
int bf63112_spi_wait_irq (struct bf63112_spi *spi, unsigned int timeout_ms,
                          struct bf63112_spi_irq_event *event);

/*!
 * Driver capture wait: up to 3 x 100 ms, 50 ms apart.  A timed-poll result is
 * not an observation and counts as a timeout, exactly like capture_wait().
 */
int bf63112_spi_capture_wait (struct bf63112_spi *spi,
                              unsigned int timeout_ms, unsigned int attempts,
                              struct bf63112_spi_irq_event *event);

/* ------------------------------------------------------------------ helpers */

/*! TRUE when an event represents line assertion (rising or level 1). */
bool bf63112_spi_irq_event_asserted (const struct bf63112_spi_irq_event *event);

const char *bf63112_spi_irq_source_name (enum bf63112_spi_irq_source source);
const char *bf63112_spi_strerror (int error);

#ifdef __cplusplus
}
#endif

#endif /* BF63112_SPI_H */
