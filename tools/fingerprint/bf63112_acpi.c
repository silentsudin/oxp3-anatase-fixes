/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * bf63112_acpi.c - ACPI _CRS resource-template parser (C mirror of acpi.py).
 *
 * Layouts are ACPI 6.x, cross-checked against drivers/acpi/acpica/amlresrc.h
 * and rsserial.c as documented in docs/hw/fingerprint-transport.md.  Only the
 * descriptors the BF63112 needs are decoded; anything else is preserved as an
 * opaque item.  Every read is bounds-checked.  This is our own code and it
 * contains no ACPICA or firmware bytes.
 */

#include "bf63112_acpi.h"

#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------- helpers */

static int
u16_at (const uint8_t *buf, size_t len, size_t off, uint16_t *out,
        enum bf63112_acpi_error *error)
{
  if (off + 2 > len || off + 2 < off)
    {
      if (error != NULL)
        *error = BF63112_ACPI_ERR_TRUNCATED;
      return -1;
    }
  *out = (uint16_t) (buf[off] | ((uint16_t) buf[off + 1] << 8));
  return 0;
}

static int
u32_at (const uint8_t *buf, size_t len, size_t off, uint32_t *out,
        enum bf63112_acpi_error *error)
{
  if (off + 4 > len || off + 4 < off)
    {
      if (error != NULL)
        *error = BF63112_ACPI_ERR_TRUNCATED;
      return -1;
    }
  *out = (uint32_t) buf[off] | ((uint32_t) buf[off + 1] << 8) |
         ((uint32_t) buf[off + 2] << 16) | ((uint32_t) buf[off + 3] << 24);
  return 0;
}

static int
cstring_at (const uint8_t *buf, size_t len, size_t off, char *out,
            size_t out_size, enum bf63112_acpi_error *error)
{
  size_t end;

  if (out_size == 0)
    return -1;
  out[0] = '\0';
  if (off <= 0 || off >= len)
    {
      if (error != NULL)
        *error = BF63112_ACPI_ERR_BAD_SOURCE;
      return -1;
    }
  end = off;
  while (end < len && buf[end] != 0)
    end++;
  if (end - off >= out_size)
    end = off + out_size - 1;
  memcpy (out, buf + off, end - off);
  out[end - off] = '\0';
  return 0;
}

static const char *
kind_name (enum bf63112_acpi_kind kind)
{
  switch (kind)
    {
    case BF63112_ACPI_KIND_SERIAL_BUS:
      return "serial-bus";
    case BF63112_ACPI_KIND_GPIO:
      return "gpio";
    case BF63112_ACPI_KIND_IRQ:
      return "irq";
    case BF63112_ACPI_KIND_EXTENDED_IRQ:
      return "extended-irq";
    case BF63112_ACPI_KIND_OPAQUE:
      return "opaque";
    case BF63112_ACPI_KIND_OTHER:
    default:
      return "other";
    }
}

static struct bf63112_acpi_resource *
new_resource (struct bf63112_acpi_template *tmpl, enum bf63112_acpi_kind kind,
              size_t offset, size_t length,
              enum bf63112_acpi_error *error)
{
  struct bf63112_acpi_resource *resource;

  if (tmpl->n_resources >= BF63112_ACPI_MAX_RESOURCES)
    {
      if (error != NULL)
        *error = BF63112_ACPI_ERR_TOO_MANY;
      return NULL;
    }
  resource = &tmpl->resources[tmpl->n_resources++];
  memset (resource, 0, sizeof *resource);
  resource->kind = kind;
  resource->offset = offset;
  resource->length = length;
  resource->irq_flags = 0;
  resource->io_restriction = BF63112_ACPI_IO_UNKNOWN;
  resource->triggering = BF63112_ACPI_TRIGGERING_UNKNOWN;
  resource->polarity = BF63112_ACPI_POLARITY_UNKNOWN;
  snprintf (resource->name, sizeof resource->name, "%s", kind_name (kind));
  return resource;
}

static void
set_opaque_name (struct bf63112_acpi_resource *resource, const char *name)
{
  snprintf (resource->name, sizeof resource->name, "%s", name);
}

/* -------------------------------------------------------- descriptor decode */

static int
decode_serial_bus (const uint8_t *data, size_t data_len, size_t total,
                   size_t offset, struct bf63112_acpi_template *tmpl,
                   enum bf63112_acpi_error *error)
{
  struct bf63112_acpi_resource *resource;
  uint8_t revision, res_index, serial_type, general, type_revision;
  uint16_t type_flags, type_data_length;
  uint16_t value16;
  uint32_t value32;
  size_t source_rel;

  if (data_len < 9)
    {
      *error = BF63112_ACPI_ERR_TRUNCATED;
      return -1;
    }
  revision = data[0];
  res_index = data[1];
  serial_type = data[2];
  general = data[3];
  if (u16_at (data, data_len, 4, &type_flags, error) < 0 ||
      u16_at (data, data_len, 7, &type_data_length, error) < 0)
    return -1;
  type_revision = data[6];

  resource = new_resource (tmpl, BF63112_ACPI_KIND_SERIAL_BUS, offset, total,
                           error);
  if (resource == NULL)
    return -1;
  resource->revision = revision;
  resource->has_revision = true;
  resource->resource_source_index = res_index;
  resource->has_resource_source_index = true;
  resource->serial_type = serial_type;
  resource->has_serial_type = true;
  resource->general_flags = general;
  resource->has_general_flags = true;
  resource->type_specific_flags = type_flags;
  resource->has_type_specific_flags = true;
  resource->type_revision = type_revision;
  resource->has_type_revision = true;
  resource->type_data_length = type_data_length;
  resource->has_type_data_length = true;

  if (res_index)
    {
      source_rel = 9 + type_data_length;
      if (source_rel >= data_len)
        {
          *error = BF63112_ACPI_ERR_BAD_SOURCE;
          return -1;
        }
      if (cstring_at (data, data_len, source_rel, resource->resource_source,
                      sizeof resource->resource_source, error) < 0)
        return -1;
      resource->has_resource_source = true;
    }

  if (serial_type == BF63112_ACPI_SERIAL_SPI)
    {
      if (type_data_length < 9)
        {
          *error = BF63112_ACPI_ERR_TRUNCATED;
          return -1;
        }
      if (data_len < 18 || u32_at (data, data_len, 9, &value32, error) < 0 ||
          u16_at (data, data_len, 16, &value16, error) < 0)
        return -1;
      resource->connection_speed_hz = value32;
      resource->has_connection_speed_hz = true;
      resource->data_bit_length = data[13];
      resource->has_data_bit_length = true;
      resource->clock_phase = data[14];
      resource->has_clock_phase = true;
      resource->clock_polarity = data[15];
      resource->has_clock_polarity = true;
      resource->device_selection = value16;
      resource->has_device_selection = true;
      resource->wire_mode_three =
        (type_flags & BF63112_ACPI_SPI_FLAG_WIRE_MODE_3) != 0;
      resource->device_polarity_high =
        (type_flags & BF63112_ACPI_SPI_FLAG_DEVICE_POLARITY_HIGH) != 0;
    }
  else if (serial_type == BF63112_ACPI_SERIAL_I2C)
    {
      if (type_data_length < 6)
        {
          *error = BF63112_ACPI_ERR_TRUNCATED;
          return -1;
        }
      if (data_len < 15 || u32_at (data, data_len, 9, &value32, error) < 0 ||
          u16_at (data, data_len, 13, &value16, error) < 0)
        return -1;
      resource->connection_speed_hz = value32;
      resource->has_connection_speed_hz = true;
      resource->device_selection = value16;
      resource->has_device_selection = true;
    }
  return 0;
}

static int
decode_gpio (const uint8_t *data, size_t data_len, size_t total,
             size_t offset, struct bf63112_acpi_template *tmpl,
             enum bf63112_acpi_error *error)
{
  struct bf63112_acpi_resource *resource;
  uint16_t int_flags, pin_table_off, res_off, vendor_off;
  uint16_t drive_strength, debounce;
  uint8_t revision, connection_type, res_index, pin_config;
  uint16_t value16;
  int start, end, end_abs;
  size_t i;

  if (data_len < 20)
    {
      *error = BF63112_ACPI_ERR_TRUNCATED;
      return -1;
    }
  revision = data[0];
  connection_type = data[1];
  pin_config = data[6];
  if (u16_at (data, data_len, 4, &int_flags, error) < 0 ||
      u16_at (data, data_len, 7, &drive_strength, error) < 0 ||
      u16_at (data, data_len, 9, &debounce, error) < 0 ||
      u16_at (data, data_len, 11, &pin_table_off, error) < 0 ||
      u16_at (data, data_len, 14, &res_off, error) < 0 ||
      u16_at (data, data_len, 16, &vendor_off, error) < 0)
    return -1;
  res_index = data[13];

  resource = new_resource (tmpl, BF63112_ACPI_KIND_GPIO, offset, total, error);
  if (resource == NULL)
    return -1;
  resource->revision = revision;
  resource->has_revision = true;
  resource->connection_type = connection_type;
  resource->has_connection_type = true;
  resource->pin_config = pin_config;
  resource->has_pin_config = true;
  resource->drive_strength = drive_strength;
  resource->has_drive_strength = true;
  resource->debounce_ms = debounce;
  resource->has_debounce_ms = true;

  /* Descriptor offsets are absolute, i.e. they include the 3-byte header;
   * data starts at absolute 3. */
  if (pin_table_off != 0)
    {
      start = (int) pin_table_off - 3;
      end_abs = res_off != 0 ? res_off : vendor_off;
      if (end_abs == 0)
        end_abs = (int) total;
      end = end_abs - 3;
      if (start < 0 || end > (int) data_len || end < start)
        {
          *error = BF63112_ACPI_ERR_BAD_PIN_TABLE;
          return -1;
        }
      if ((end - start) % 2 != 0)
        {
          *error = BF63112_ACPI_ERR_BAD_PIN_TABLE;
          return -1;
        }
      if ((size_t) (end - start) / 2 > BF63112_ACPI_MAX_PINS)
        {
          *error = BF63112_ACPI_ERR_TOO_MANY;
          return -1;
        }
      for (i = 0; i < (size_t) (end - start) / 2; i++)
        {
          if (u16_at (data, data_len, (size_t) start + 2 * i, &value16,
                      error) < 0)
            return -1;
          resource->pins[resource->n_pins++] = value16;
        }
    }
  if (res_index)
    {
      if (res_off < 3 || (size_t) (res_off - 3) >= data_len)
        {
          *error = BF63112_ACPI_ERR_BAD_SOURCE;
          return -1;
        }
      if (cstring_at (data, data_len, (size_t) (res_off - 3),
                      resource->resource_source,
                      sizeof resource->resource_source, error) < 0)
        return -1;
      resource->has_resource_source = true;
    }

  if (connection_type == BF63112_ACPI_GPIO_TYPE_INT)
    {
      resource->triggering =
        (int_flags & BF63112_ACPI_GPIO_TRIGGERING_MASK)
        ? BF63112_ACPI_TRIGGERING_EDGE : BF63112_ACPI_TRIGGERING_LEVEL;
      resource->has_triggering = true;
      switch ((int_flags >> BF63112_ACPI_GPIO_POLARITY_SHIFT) &
              BF63112_ACPI_GPIO_POLARITY_MASK)
        {
        case 1:
          resource->polarity = BF63112_ACPI_POLARITY_ACTIVE_LOW;
          break;
        case 2:
          resource->polarity = BF63112_ACPI_POLARITY_ACTIVE_BOTH;
          break;
        case 0:
        default:
          resource->polarity = BF63112_ACPI_POLARITY_ACTIVE_HIGH;
          break;
        }
      resource->has_polarity = true;
      resource->shareable =
        (int_flags & BF63112_ACPI_GPIO_SHAREABLE) != 0;
      resource->has_shareable = true;
      resource->wake_capable =
        (int_flags & BF63112_ACPI_GPIO_WAKE_CAPABLE) != 0;
      resource->has_wake_capable = true;
    }
  else
    {
      switch ((int_flags & BF63112_ACPI_GPIO_IO_RESTRICTION_MASK)
              >> BF63112_ACPI_GPIO_IO_RESTRICTION_SHIFT)
        {
        case 1:
          resource->io_restriction = BF63112_ACPI_IO_INPUT;
          break;
        case 2:
          resource->io_restriction = BF63112_ACPI_IO_OUTPUT;
          break;
        case 3:
          resource->io_restriction = BF63112_ACPI_IO_NONE_PRESERVE;
          break;
        case 0:
        default:
          resource->io_restriction = BF63112_ACPI_IO_NONE;
          break;
        }
      resource->has_io_restriction = true;
    }
  return 0;
}

static int
decode_extended_irq (const uint8_t *data, size_t data_len, size_t total,
                     size_t offset, struct bf63112_acpi_template *tmpl,
                     enum bf63112_acpi_error *error)
{
  struct bf63112_acpi_resource *resource;
  uint8_t flags, count;
  uint32_t value;
  size_t i;

  if (data_len < 2)
    {
      *error = BF63112_ACPI_ERR_TRUNCATED;
      return -1;
    }
  flags = data[0];
  count = data[1];
  if (data_len < 2 + (size_t) count * 4)
    {
      *error = BF63112_ACPI_ERR_TRUNCATED;
      return -1;
    }
  if (count > BF63112_ACPI_MAX_IRQS)
    {
      *error = BF63112_ACPI_ERR_TOO_MANY;
      return -1;
    }
  resource = new_resource (tmpl, BF63112_ACPI_KIND_EXTENDED_IRQ, offset,
                           total, error);
  if (resource == NULL)
    return -1;
  resource->irq_flags = flags;
  resource->has_irq_flags = true;
  for (i = 0; i < count; i++)
    {
      if (u32_at (data, data_len, 2 + 4 * i, &value, error) < 0)
        return -1;
      resource->irqs[resource->n_irqs++] = value;
    }
  return 0;
}

static int
decode_small_irq (const uint8_t *data, size_t data_len, size_t total,
                  size_t offset, struct bf63112_acpi_template *tmpl,
                  enum bf63112_acpi_error *error)
{
  struct bf63112_acpi_resource *resource;
  uint16_t mask;
  size_t i;

  if (data_len < 2)
    {
      *error = BF63112_ACPI_ERR_TRUNCATED;
      return -1;
    }
  if (u16_at (data, data_len, 0, &mask, error) < 0)
    return -1;
  resource = new_resource (tmpl, BF63112_ACPI_KIND_IRQ, offset, total, error);
  if (resource == NULL)
    return -1;
  for (i = 0; i < 16; i++)
    if (mask & (1u << i))
      {
        if (resource->n_irqs >= BF63112_ACPI_MAX_IRQS)
          {
            *error = BF63112_ACPI_ERR_TOO_MANY;
            return -1;
          }
        resource->irqs[resource->n_irqs++] = (uint32_t) i;
      }
  if (data_len > 2)
    {
      resource->irq_flags = data[2];
      resource->has_irq_flags = true;
    }
  return 0;
}

/* ------------------------------------------------------------- item names */

static const char *
large_item_name (uint8_t name)
{
  switch (name)
    {
    case BF63112_ACPI_LARGE_MEMORY24:
      return "memory24";
    case BF63112_ACPI_LARGE_GENERIC_REGISTER:
      return "generic-register";
    case BF63112_ACPI_LARGE_VENDOR:
      return "vendor";
    case BF63112_ACPI_LARGE_MEMORY32:
      return "memory32";
    case BF63112_ACPI_LARGE_FIXED_MEMORY32:
      return "fixed-memory32";
    case BF63112_ACPI_LARGE_ADDRESS32:
      return "address32";
    case BF63112_ACPI_LARGE_ADDRESS16:
      return "address16";
    case BF63112_ACPI_LARGE_ADDRESS64:
      return "address64";
    case BF63112_ACPI_LARGE_EXTENDED_ADDRESS64:
      return "extended-address64";
    case BF63112_ACPI_LARGE_EXTENDED_SPACE:
      return "extended-space";
    case BF63112_ACPI_LARGE_PIN_FUNCTION:
      return "pin-function";
    case BF63112_ACPI_LARGE_PIN_CONFIG:
      return "pin-config";
    case BF63112_ACPI_LARGE_PIN_GROUP:
      return "pin-group";
    case BF63112_ACPI_LARGE_PIN_GROUP_FUNCTION:
      return "pin-group-function";
    case BF63112_ACPI_LARGE_PIN_GROUP_CONFIG:
      return "pin-group-config";
    case BF63112_ACPI_LARGE_CLOCK_INPUT:
      return "clock-input";
    default:
      return NULL;
    }
}

static const char *
small_item_name (uint8_t name)
{
  switch (name)
    {
    case BF63112_ACPI_SMALL_DMA:
      return "dma";
    case BF63112_ACPI_SMALL_START_DEPENDENT:
      return "start-dependent";
    case BF63112_ACPI_SMALL_END_DEPENDENT:
      return "end-dependent";
    case BF63112_ACPI_SMALL_IO:
      return "io";
    case BF63112_ACPI_SMALL_FIXED_IO:
      return "fixed-io";
    case BF63112_ACPI_SMALL_FIXED_DMA:
      return "fixed-dma";
    case BF63112_ACPI_SMALL_VENDOR:
      return "vendor-small";
    default:
      return NULL;
    }
}

/* ------------------------------------------------------------ the walker */

int
bf63112_acpi_parse (const uint8_t *buf, size_t len,
                    struct bf63112_acpi_template *out,
                    enum bf63112_acpi_error *error)
{
  enum bf63112_acpi_error local_error = BF63112_ACPI_OK;
  size_t offset = 0;

  if (error == NULL)
    error = &local_error;
  *error = BF63112_ACPI_OK;
  if (out == NULL || (buf == NULL && len > 0))
    {
      *error = BF63112_ACPI_ERR_INVALID;
      return -1;
    }
  memset (out, 0, sizeof *out);
  out->end_tag_offset = SIZE_MAX;
  out->trailing_offset = len;

  while (offset < len)
    {
      uint8_t tag = buf[offset];

      if (tag == 0x79)   /* EndTag: small item 0x0F, length 1 (checksum) */
        {
          if (offset + 2 > len)
            {
              *error = BF63112_ACPI_ERR_TRUNCATED;
              return -1;
            }
          out->has_end_tag = true;
          out->end_tag_offset = offset;
          offset += 2;
          out->trailing_offset = offset;
          out->trailing_length = len - offset;
          return 0;
        }
      if (tag & 0x80)
        {
          uint8_t name = (uint8_t) (tag & 0x7Fu);
          uint16_t length;
          size_t total;

          if (offset + 3 > len)
            {
              *error = BF63112_ACPI_ERR_TRUNCATED;
              return -1;
            }
          if (u16_at (buf, len, offset + 1, &length, error) < 0)
            return -1;
          total = 3 + (size_t) length;
          if (offset + total > len || offset + total < offset)
            {
              *error = BF63112_ACPI_ERR_TRUNCATED;
              return -1;
            }
          if (name == BF63112_ACPI_LARGE_SERIAL_BUS)
            {
              if (decode_serial_bus (buf + offset + 3, length, total, offset,
                                     out, error) < 0)
                return -1;
            }
          else if (name == BF63112_ACPI_LARGE_GPIO)
            {
              if (decode_gpio (buf + offset + 3, length, total, offset, out,
                               error) < 0)
                return -1;
            }
          else if (name == BF63112_ACPI_LARGE_EXTENDED_IRQ)
            {
              if (decode_extended_irq (buf + offset + 3, length, total,
                                       offset, out, error) < 0)
                return -1;
            }
          else
            {
              struct bf63112_acpi_resource *resource;
              const char *label = large_item_name (name);

              resource = new_resource (out, BF63112_ACPI_KIND_OPAQUE, offset,
                                       total, error);
              if (resource == NULL)
                return -1;
              if (label != NULL)
                set_opaque_name (resource, label);
              else
                snprintf (resource->name, sizeof resource->name,
                          "large-0x%02x", name);
            }
          offset += total;
          continue;
        }

      {
        uint8_t name = (uint8_t) (tag >> 3);
        size_t length = (size_t) (tag & 0x07u);
        size_t total = 1 + length;

        if (offset + total > len || offset + total < offset)
          {
            *error = BF63112_ACPI_ERR_TRUNCATED;
            return -1;
          }
        if (name == BF63112_ACPI_SMALL_IRQ)
          {
            if (decode_small_irq (buf + offset + 1, length, total, offset,
                                  out, error) < 0)
              return -1;
          }
        else
          {
            struct bf63112_acpi_resource *resource;
            const char *label = small_item_name (name);

            resource = new_resource (out, BF63112_ACPI_KIND_OPAQUE, offset,
                                     total, error);
            if (resource == NULL)
              return -1;
            if (label != NULL)
              set_opaque_name (resource, label);
            else
              snprintf (resource->name, sizeof resource->name,
                        "small-0x%02x", name);
          }
        offset += total;
      }
    }
  /* No EndTag is tolerated (some firmware omits it). */
  out->trailing_offset = len;
  out->trailing_length = 0;
  return 0;
}

const char *
bf63112_acpi_strerror (enum bf63112_acpi_error error)
{
  switch (error)
    {
    case BF63112_ACPI_OK:
      return "ok";
    case BF63112_ACPI_ERR_TRUNCATED:
      return "truncated resource descriptor";
    case BF63112_ACPI_ERR_BAD_SOURCE:
      return "resource-source string outside the descriptor";
    case BF63112_ACPI_ERR_BAD_PIN_TABLE:
      return "GPIO pin table out of range or odd length";
    case BF63112_ACPI_ERR_TOO_MANY:
      return "too many resources, pins or IRQs";
    case BF63112_ACPI_ERR_INVALID:
      return "invalid argument";
    default:
      return "unknown ACPI parse error";
    }
}

bool
bf63112_acpi_is_spi (const struct bf63112_acpi_resource *resource)
{
  return resource != NULL &&
         resource->kind == BF63112_ACPI_KIND_SERIAL_BUS &&
         resource->has_serial_type &&
         resource->serial_type == BF63112_ACPI_SERIAL_SPI;
}

int
bf63112_acpi_spi_mode (const struct bf63112_acpi_resource *resource)
{
  if (resource == NULL || !resource->has_clock_phase ||
      !resource->has_clock_polarity)
    return -1;
  return ((resource->clock_polarity & 1) << 1) |
         (resource->clock_phase & 1);
}

const struct bf63112_acpi_resource *
bf63112_acpi_first_spi (const struct bf63112_acpi_template *tmpl)
{
  size_t i;

  if (tmpl == NULL)
    return NULL;
  for (i = 0; i < tmpl->n_resources; i++)
    if (bf63112_acpi_is_spi (&tmpl->resources[i]))
      return &tmpl->resources[i];
  return NULL;
}

size_t
bf63112_acpi_gpio_count (const struct bf63112_acpi_template *tmpl,
                         int connection_type)
{
  size_t i, count = 0;

  if (tmpl == NULL)
    return 0;
  for (i = 0; i < tmpl->n_resources; i++)
    if (tmpl->resources[i].kind == BF63112_ACPI_KIND_GPIO &&
        tmpl->resources[i].has_connection_type &&
        tmpl->resources[i].connection_type == connection_type)
      count++;
  return count;
}

const struct bf63112_acpi_resource *
bf63112_acpi_first_gpio (const struct bf63112_acpi_template *tmpl,
                         int connection_type)
{
  size_t i;

  if (tmpl == NULL)
    return NULL;
  for (i = 0; i < tmpl->n_resources; i++)
    if (tmpl->resources[i].kind == BF63112_ACPI_KIND_GPIO &&
        tmpl->resources[i].has_connection_type &&
        tmpl->resources[i].connection_type == connection_type)
      return &tmpl->resources[i];
  return NULL;
}

void
bf63112_acpi_spi_connection (const struct bf63112_acpi_template *tmpl,
                             struct bf63112_acpi_spi_connection *connection)
{
  const struct bf63112_acpi_resource *spi;
  const struct bf63112_acpi_resource *gpio_int;
  const struct bf63112_acpi_resource *gpio_io;

  if (connection == NULL)
    return;
  memset (connection, 0, sizeof *connection);
  connection->irq_active_low = -1;
  connection->irq_edge = 0;      /* rising is the documented default */
  connection->has_irq_triggering = false;
  connection->has_irq_polarity = false;
  if (tmpl == NULL)
    return;

  spi = bf63112_acpi_first_spi (tmpl);
  if (spi != NULL)
    {
      if (spi->has_resource_source)
        {
          memcpy (connection->controller, spi->resource_source,
                  sizeof connection->controller);
          connection->controller[sizeof connection->controller - 1] = '\0';
          connection->has_controller = true;
        }
      if (spi->has_connection_speed_hz)
        {
          connection->speed_hz = spi->connection_speed_hz;
          connection->has_speed_hz = true;
        }
      connection->mode = bf63112_acpi_spi_mode (spi);
      connection->has_mode = connection->mode >= 0;
      if (spi->has_data_bit_length)
        {
          connection->bits_per_word = spi->data_bit_length;
          connection->has_bits_per_word = true;
        }
      if (spi->has_device_selection)
        {
          connection->chip_select = spi->device_selection;
          connection->has_chip_select = true;
        }
    }

  gpio_int = bf63112_acpi_first_gpio (tmpl, BF63112_ACPI_GPIO_TYPE_INT);
  if (gpio_int != NULL)
    {
      if (gpio_int->n_pins > 0)
        {
          connection->irq_pin = (int) gpio_int->pins[0];
          connection->has_irq_pin = true;
        }
      if (gpio_int->has_triggering)
        {
          connection->irq_triggering = gpio_int->triggering;
          connection->has_irq_triggering = true;
        }
      if (gpio_int->has_polarity)
        {
          connection->irq_polarity = gpio_int->polarity;
          connection->has_irq_polarity = true;
        }
      if (gpio_int->has_shareable)
        {
          connection->irq_shareable = gpio_int->shareable;
          connection->has_irq_shareable = true;
        }
      if (gpio_int->has_polarity)
        {
          if (gpio_int->polarity == BF63112_ACPI_POLARITY_ACTIVE_LOW)
            connection->irq_active_low = 1;
          else if (gpio_int->polarity == BF63112_ACPI_POLARITY_ACTIVE_HIGH)
            connection->irq_active_low = 0;
        }
      connection->irq_edge = 2;   /* level-triggered: request both edges */
      if (gpio_int->has_triggering &&
          gpio_int->triggering == BF63112_ACPI_TRIGGERING_EDGE)
        {
          connection->irq_edge =
            (gpio_int->has_polarity &&
             gpio_int->polarity == BF63112_ACPI_POLARITY_ACTIVE_BOTH)
            ? 2 : 0;
        }
    }

  gpio_io = bf63112_acpi_first_gpio (tmpl, BF63112_ACPI_GPIO_TYPE_IO);
  if (gpio_io != NULL && gpio_io->n_pins > 0)
    {
      connection->reset_pin = (int) gpio_io->pins[0];
      connection->has_reset_pin = true;
    }
}
