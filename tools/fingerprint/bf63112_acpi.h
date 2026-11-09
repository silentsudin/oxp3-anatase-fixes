/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_acpi.h - ACPI _CRS resource-template parser for the BF63112 SPI
 *                  connection (C mirror of fpbf63112/acpi.py).
 *
 * The Windows driver never hard-codes the SPI clock, mode, word size, chip
 * select or GPIOs: it takes them from the translated resources the firmware
 * built from \_SB.PC00.SPI1.FPRT._CRS.  On Linux the same bytes live in the
 * raw _CRS template (root-only on this host), so a driver/probe extracts the
 * buffer once and feeds it here.  Only the descriptors this device needs are
 * decoded (SpiSerialBus, GpioInt/GpioIo, small/extended IRQ); everything else
 * is preserved as an opaque item with its offset so extra resources do not
 * break the parse.
 *
 * Dependency-free (stdint/stddef only).  Every access is bounds-checked and a
 * malformed or truncated template returns an error instead of crashing.
 */

#ifndef BF63112_ACPI_H
#define BF63112_ACPI_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BF63112_ACPI_MAX_RESOURCES 32
#define BF63112_ACPI_MAX_PINS      16
#define BF63112_ACPI_MAX_IRQS      16
#define BF63112_ACPI_MAX_SOURCE    128

/* Resource item names (ACPI 6.x resource type bytes). */
#define BF63112_ACPI_SMALL_IRQ             0x04
#define BF63112_ACPI_SMALL_DMA             0x05
#define BF63112_ACPI_SMALL_START_DEPENDENT 0x06
#define BF63112_ACPI_SMALL_END_DEPENDENT   0x07
#define BF63112_ACPI_SMALL_IO              0x08
#define BF63112_ACPI_SMALL_FIXED_IO        0x09
#define BF63112_ACPI_SMALL_FIXED_DMA       0x0A
#define BF63112_ACPI_SMALL_VENDOR          0x0E
#define BF63112_ACPI_SMALL_END_TAG         0x0F

#define BF63112_ACPI_LARGE_MEMORY24         0x01
#define BF63112_ACPI_LARGE_GENERIC_REGISTER 0x02
#define BF63112_ACPI_LARGE_VENDOR           0x03
#define BF63112_ACPI_LARGE_MEMORY32         0x04
#define BF63112_ACPI_LARGE_FIXED_MEMORY32   0x05
#define BF63112_ACPI_LARGE_ADDRESS32        0x06
#define BF63112_ACPI_LARGE_ADDRESS16        0x07
#define BF63112_ACPI_LARGE_EXTENDED_IRQ     0x08
#define BF63112_ACPI_LARGE_ADDRESS64        0x09
#define BF63112_ACPI_LARGE_EXTENDED_ADDRESS64 0x0A
#define BF63112_ACPI_LARGE_EXTENDED_SPACE   0x0B
#define BF63112_ACPI_LARGE_GPIO             0x0C
#define BF63112_ACPI_LARGE_PIN_FUNCTION     0x0D
#define BF63112_ACPI_LARGE_SERIAL_BUS       0x0E
#define BF63112_ACPI_LARGE_PIN_CONFIG       0x0F
#define BF63112_ACPI_LARGE_PIN_GROUP        0x10
#define BF63112_ACPI_LARGE_PIN_GROUP_FUNCTION 0x11
#define BF63112_ACPI_LARGE_PIN_GROUP_CONFIG 0x12
#define BF63112_ACPI_LARGE_CLOCK_INPUT      0x13

#define BF63112_ACPI_SERIAL_I2C  1
#define BF63112_ACPI_SERIAL_SPI  2
#define BF63112_ACPI_SERIAL_UART 3
#define BF63112_ACPI_SERIAL_CSI2 4

#define BF63112_ACPI_SPI_FLAG_WIRE_MODE_3        (1u << 0)
#define BF63112_ACPI_SPI_FLAG_DEVICE_POLARITY_HIGH (1u << 1)

#define BF63112_ACPI_GPIO_TYPE_INT 0
#define BF63112_ACPI_GPIO_TYPE_IO  1

/* GPIO InterruptAndIoFlags bit fields (ACPI 6.x). */
#define BF63112_ACPI_GPIO_TRIGGERING_MASK      0x1u
#define BF63112_ACPI_GPIO_POLARITY_SHIFT       1
#define BF63112_ACPI_GPIO_POLARITY_MASK        0x3u
#define BF63112_ACPI_GPIO_SHAREABLE            (1u << 3)
#define BF63112_ACPI_GPIO_WAKE_CAPABLE         (1u << 4)
#define BF63112_ACPI_GPIO_IO_RESTRICTION_SHIFT 5
#define BF63112_ACPI_GPIO_IO_RESTRICTION_MASK  (0x3u << 5)

enum bf63112_acpi_kind
{
  BF63112_ACPI_KIND_OTHER = 0,
  BF63112_ACPI_KIND_SERIAL_BUS,
  BF63112_ACPI_KIND_GPIO,
  BF63112_ACPI_KIND_IRQ,
  BF63112_ACPI_KIND_EXTENDED_IRQ,
  BF63112_ACPI_KIND_OPAQUE,
};

enum bf63112_acpi_triggering
{
  BF63112_ACPI_TRIGGERING_UNKNOWN = 0,
  BF63112_ACPI_TRIGGERING_LEVEL,
  BF63112_ACPI_TRIGGERING_EDGE,
};

enum bf63112_acpi_polarity
{
  BF63112_ACPI_POLARITY_UNKNOWN = 0,
  BF63112_ACPI_POLARITY_ACTIVE_HIGH,
  BF63112_ACPI_POLARITY_ACTIVE_LOW,
  BF63112_ACPI_POLARITY_ACTIVE_BOTH,
};

enum bf63112_acpi_io_restriction
{
  BF63112_ACPI_IO_UNKNOWN = 0,
  BF63112_ACPI_IO_NONE,
  BF63112_ACPI_IO_INPUT,
  BF63112_ACPI_IO_OUTPUT,
  BF63112_ACPI_IO_NONE_PRESERVE,
};

enum bf63112_acpi_error
{
  BF63112_ACPI_OK = 0,
  BF63112_ACPI_ERR_TRUNCATED,
  BF63112_ACPI_ERR_BAD_SOURCE,
  BF63112_ACPI_ERR_BAD_PIN_TABLE,
  BF63112_ACPI_ERR_TOO_MANY,
  BF63112_ACPI_ERR_INVALID,
};

struct bf63112_acpi_resource
{
  enum bf63112_acpi_kind kind;
  char   name[24];               /*: "serial-bus", "gpio", "io", ... */
  size_t offset;                 /*: byte offset in the template */
  size_t length;                 /*: total item bytes incl. header */

  /* Serial bus */
  int    serial_type;
  bool   has_serial_type;
  int    revision;
  bool   has_revision;
  int    resource_source_index;
  bool   has_resource_source_index;
  char   resource_source[BF63112_ACPI_MAX_SOURCE];
  bool   has_resource_source;
  int    general_flags;
  bool   has_general_flags;
  int    type_specific_flags;
  bool   has_type_specific_flags;
  int    type_revision;
  bool   has_type_revision;
  int    type_data_length;
  bool   has_type_data_length;
  uint32_t connection_speed_hz;
  bool   has_connection_speed_hz;
  int    data_bit_length;
  bool   has_data_bit_length;
  int    clock_phase;
  bool   has_clock_phase;
  int    clock_polarity;
  bool   has_clock_polarity;
  int    device_selection;
  bool   has_device_selection;
  bool   wire_mode_three;
  bool   device_polarity_high;

  /* GPIO */
  int    connection_type;
  bool   has_connection_type;
  uint16_t pins[BF63112_ACPI_MAX_PINS];
  size_t n_pins;
  enum bf63112_acpi_triggering triggering;
  bool   has_triggering;
  enum bf63112_acpi_polarity polarity;
  bool   has_polarity;
  bool   shareable;
  bool   has_shareable;
  bool   wake_capable;
  bool   has_wake_capable;
  enum bf63112_acpi_io_restriction io_restriction;
  bool   has_io_restriction;
  int    pin_config;
  bool   has_pin_config;
  int    drive_strength;
  bool   has_drive_strength;
  int    debounce_ms;
  bool   has_debounce_ms;

  /* IRQ */
  uint32_t irqs[BF63112_ACPI_MAX_IRQS];
  size_t n_irqs;
  int    irq_flags;
  bool   has_irq_flags;
};

struct bf63112_acpi_template
{
  struct bf63112_acpi_resource resources[BF63112_ACPI_MAX_RESOURCES];
  size_t n_resources;
  size_t end_tag_offset;         /*: SIZE_MAX when no EndTag was present */
  bool   has_end_tag;
  size_t trailing_offset;        /*: first byte after the EndTag */
  size_t trailing_length;        /*: bytes after the EndTag, preserved */
};

/*! True when the resource is a SpiSerialBus descriptor. */
bool bf63112_acpi_is_spi (const struct bf63112_acpi_resource *resource);

/*! Linux SPI_MODE_*: (CPOL << 1) | CPHA, or -1 when the fields are absent. */
int bf63112_acpi_spi_mode (const struct bf63112_acpi_resource *resource);

/*! Decode one raw _CRS resource template.  Returns 0 or -1 + @error. */
int bf63112_acpi_parse (const uint8_t *buf, size_t len,
                        struct bf63112_acpi_template *out,
                        enum bf63112_acpi_error *error);

const char *bf63112_acpi_strerror (enum bf63112_acpi_error error);

const struct bf63112_acpi_resource *
bf63112_acpi_first_spi (const struct bf63112_acpi_template *tmpl);

size_t bf63112_acpi_gpio_count (const struct bf63112_acpi_template *tmpl,
                                int connection_type);

const struct bf63112_acpi_resource *
bf63112_acpi_first_gpio (const struct bf63112_acpi_template *tmpl,
                         int connection_type);

/*! Everything an SPI driver needs, reduced from one template. */
struct bf63112_acpi_spi_connection
{
  char   controller[BF63112_ACPI_MAX_SOURCE];
  bool   has_controller;
  uint32_t speed_hz;
  bool   has_speed_hz;
  int    mode;
  bool   has_mode;
  int    bits_per_word;
  bool   has_bits_per_word;
  int    chip_select;
  bool   has_chip_select;

  int    irq_pin;
  bool   has_irq_pin;
  enum bf63112_acpi_triggering irq_triggering;
  bool   has_irq_triggering;
  enum bf63112_acpi_polarity irq_polarity;
  bool   has_irq_polarity;
  bool   irq_shareable;
  bool   has_irq_shareable;

  int    reset_pin;
  bool   has_reset_pin;

  /*! -1 unknown, 0 active high, 1 active low. */
  int    irq_active_low;
  /*! 0 rising, 1 falling, 2 both (the logical assertion edge). */
  int    irq_edge;
};

void bf63112_acpi_spi_connection (
  const struct bf63112_acpi_template *tmpl,
  struct bf63112_acpi_spi_connection *connection);

#ifdef __cplusplus
}
#endif

#endif /* BF63112_ACPI_H */
