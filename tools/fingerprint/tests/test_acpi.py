"""ACPI ``_CRS`` resource-template parser: SpiSerialBus, GpioInt, malformed.

Every buffer here is constructed from the ACPI 6.x layout (large/small resource
item headers, ``amlresrc.h`` field order) with made-up values.  Nothing is
copied from firmware or from the vendor package; the tests pin the parser's
understanding of the spec, including the GpioIo IO-restriction bits (5-6, not
0-1) and the fact that descriptor offsets include the three-byte header.
"""

import struct
import unittest

import support  # noqa: F401  (sets sys.path)
from fpbf63112 import acpi


# ---------------------------------------------------------------------------
# Builders (ACPI 6.x field order)
# ---------------------------------------------------------------------------

def large_item(name, data):
    return bytes([0x80 | name]) + struct.pack("<H", len(data)) + data


def small_item(name, data):
    if len(data) > 7:
        raise ValueError("small item too long")
    return bytes([(name << 3) | len(data)]) + data


def end_tag(extra=b""):
    return b"\x79\x00" + extra


def serial_bus(serial_type=acpi.SERIAL_TYPE_SPI, speed=1_000_000,
               bit_length=8, phase=0, polarity=0, selection=0,
               source="\\_SB.PC00.SPI1", type_flags=0, revision=1,
               res_index=None, type_data_length=None, type_revision=1,
               extra=b""):
    if serial_type == acpi.SERIAL_TYPE_SPI:
        payload = (struct.pack("<I", speed)
                   + bytes([bit_length, phase, polarity])
                   + struct.pack("<H", selection))
    elif serial_type == acpi.SERIAL_TYPE_I2C:
        payload = struct.pack("<I", speed) + struct.pack("<H", selection)
    else:
        payload = struct.pack("<I", speed)
    payload += extra
    if type_data_length is None:
        type_data_length = len(payload)
    source_bytes = source.encode("ascii") + b"\0" if source else b""
    if res_index is None:
        res_index = 1 if source else 0
    data = (bytes([revision, res_index, serial_type, 0])
            + struct.pack("<H", type_flags)
            + bytes([type_revision])
            + struct.pack("<H", type_data_length)
            + payload + source_bytes)
    return large_item(acpi.LARGE_SERIAL_BUS, data)


def gpio(connection_type=acpi.GPIO_TYPE_INT, flags=0, pins=(5,),
         source="\\_SB.GPI0", pin_config=0, drive_strength=0,
         debounce=0, vendor=b"", revision=1, pin_table_offset=None,
         res_index=None, res_offset=None, vendor_offset=None):
    source_bytes = source.encode("ascii") + b"\0" if source else b""
    pin_table = b"".join(struct.pack("<H", pin) for pin in pins)
    if pin_table_offset is None:
        pin_table_offset = 3 + 20 if pin_table else 0
    if res_index is None:
        res_index = 1 if source else 0
    if res_offset is None:
        res_offset = (3 + 20 + len(pin_table)) if source else 0
    if vendor_offset is None:
        vendor_offset = (3 + 20 + len(pin_table) + len(source_bytes)) \
            if vendor else 0
    data = (bytes([revision, connection_type, 0, 0])
            + struct.pack("<H", flags)
            + bytes([pin_config])
            + struct.pack("<H", drive_strength)
            + struct.pack("<H", debounce)
            + struct.pack("<H", pin_table_offset)
            + bytes([res_index])
            + struct.pack("<H", res_offset)
            + struct.pack("<H", vendor_offset)
            + struct.pack("<H", len(vendor))
            + pin_table + source_bytes + vendor)
    return large_item(acpi.LARGE_GPIO, data)


def extended_irq(flags=0, irqs=(7, 9)):
    data = bytes([flags, len(irqs)]) + b"".join(
        struct.pack("<I", irq) for irq in irqs)
    return large_item(acpi.LARGE_EXTENDED_IRQ, data)


def small_irq(mask=0x0005, flags=None):
    data = struct.pack("<H", mask)
    if flags is not None:
        data += bytes([flags])
    return small_item(acpi.SMALL_IRQ, data)


# ---------------------------------------------------------------------------
# SerialBus
# ---------------------------------------------------------------------------

class TestSpiSerialBus(unittest.TestCase):
    def test_fields_and_offsets(self):
        buf = serial_bus(speed=24_000_000, bit_length=8, phase=1,
                         polarity=1, selection=2, type_flags=0x0001)
        template = acpi.parse_resource_template(buf + end_tag())
        self.assertEqual(len(template.resources), 1)
        r = template.spi
        self.assertIs(r, template.resources[0])
        self.assertTrue(r.is_spi)
        self.assertEqual((r.offset, r.length), (0, len(buf)))
        self.assertEqual(r.revision, 1)
        self.assertEqual(r.serial_type, acpi.SERIAL_TYPE_SPI)
        self.assertEqual(r.connection_speed_hz, 24_000_000)
        self.assertEqual(r.data_bit_length, 8)
        self.assertEqual((r.clock_phase, r.clock_polarity), (1, 1))
        self.assertEqual(r.device_selection, 2)
        self.assertEqual(r.resource_source, "\\_SB.PC00.SPI1")
        self.assertEqual(r.type_data_length, 9)
        self.assertEqual(r.type_specific_flags, 0x0001)
        self.assertTrue(r.wire_mode_three)
        self.assertFalse(r.device_polarity_high)

    def test_spi_mode_mapping(self):
        for phase in (0, 1):
            for polarity in (0, 1):
                expected = (polarity << 1) | phase
                with self.subTest(phase=phase, polarity=polarity):
                    template = acpi.parse_resource_template(
                        serial_bus(phase=phase, polarity=polarity)
                        + end_tag())
                    self.assertEqual(template.spi.spi_mode, expected)
                    self.assertEqual(template.spi.spi_mode_name,
                                     f"SPI mode {expected}")

    def test_descriptor_without_a_resource_source(self):
        template = acpi.parse_resource_template(
            serial_bus(source="") + end_tag())
        self.assertIsNone(template.spi.resource_source)
        self.assertEqual(template.spi.resource_source_index, 0)

    def test_describe(self):
        text = "\n".join(acpi.parse_resource_template(
            serial_bus() + end_tag()).describe())
        self.assertIn("SerialBus SPI", text)
        self.assertIn("connection speed: 1000000 Hz", text)
        self.assertIn("device selection: 0", text)
        self.assertIn("SPI mode 0", text)

    def test_i2c_is_decoded_but_not_spi(self):
        template = acpi.parse_resource_template(
            serial_bus(serial_type=acpi.SERIAL_TYPE_I2C, speed=400_000,
                       selection=0x50) + end_tag())
        r = template.resources[0]
        self.assertFalse(r.is_spi)
        self.assertEqual(r.connection_speed_hz, 400_000)
        self.assertEqual(r.device_selection, 0x50)
        self.assertIsNone(template.spi)


# ---------------------------------------------------------------------------
# GPIO
# ---------------------------------------------------------------------------

class TestGpio(unittest.TestCase):
    def test_gpio_int_edge_active_low(self):
        flags = acpi.GPIO_TRIGGERING_MASK | (1 << acpi.GPIO_POLARITY_SHIFT)
        template = acpi.parse_resource_template(
            gpio(flags=flags, pins=(19,)) + end_tag())
        r = template.gpio_interrupts[0]
        self.assertEqual(r.kind, "gpio")
        self.assertEqual(r.connection_type, acpi.GPIO_TYPE_INT)
        self.assertEqual(r.pins, (19,))
        self.assertIs(r.triggering, acpi.Triggering.EDGE)
        self.assertIs(r.polarity, acpi.Polarity.ACTIVE_LOW)
        self.assertFalse(r.shareable)
        self.assertFalse(r.wake_capable)
        self.assertEqual(r.resource_source, "\\_SB.GPI0")

    def test_gpio_int_level_active_high_both_edges(self):
        flags = (2 << acpi.GPIO_POLARITY_SHIFT) | acpi.GPIO_SHAREABLE \
            | acpi.GPIO_WAKE_CAPABLE
        template = acpi.parse_resource_template(
            gpio(flags=flags, pins=(3, 4)) + end_tag())
        r = template.gpio_interrupts[0]
        self.assertIs(r.triggering, acpi.Triggering.LEVEL)
        self.assertIs(r.polarity, acpi.Polarity.ACTIVE_BOTH)
        self.assertTrue(r.shareable)
        self.assertTrue(r.wake_capable)
        self.assertEqual(r.pins, (3, 4))

    def test_gpio_int_pin_config_drive_strength_debounce(self):
        r = acpi.parse_resource_template(
            gpio(flags=acpi.GPIO_TRIGGERING_MASK, pins=(1,),
                 pin_config=3, drive_strength=0x20, debounce=0x0A)
            + end_tag()).gpio_interrupts[0]
        self.assertEqual(r.pin_config, 3)
        self.assertEqual(r.drive_strength, 0x20)
        self.assertEqual(r.debounce_ms, 0x0A)

    def test_gpio_io_restriction_uses_bits_5_and_6(self):
        # Regression: these bits are NOT in the low two bits.  A polarity-like
        # 0x02 must not be read as "input-only".
        for value, expected in ((0, acpi.IoRestriction.NONE),
                                (1, acpi.IoRestriction.INPUT),
                                (2, acpi.IoRestriction.OUTPUT),
                                (3, acpi.IoRestriction.NONE_PRESERVE)):
            with self.subTest(value=value):
                flags = value << acpi.GPIO_IO_RESTRICTION_SHIFT
                r = acpi.parse_resource_template(
                    gpio(connection_type=acpi.GPIO_TYPE_IO, flags=flags,
                         pins=(2,)) + end_tag()).gpio_io[0]
                self.assertIs(r.io_restriction, expected)
        # Low bits alone must stay NONE (they are reserved for GpioIo).
        r = acpi.parse_resource_template(
            gpio(connection_type=acpi.GPIO_TYPE_IO, flags=0x0006,
                 pins=(2,)) + end_tag()).gpio_io[0]
        self.assertIs(r.io_restriction, acpi.IoRestriction.NONE)

    def test_gpio_without_pins_or_source(self):
        r = acpi.parse_resource_template(
            gpio(connection_type=acpi.GPIO_TYPE_IO, pins=(), source="")
            + end_tag()).gpio_io[0]
        self.assertEqual(r.pins, ())
        self.assertIsNone(r.resource_source)

    def test_gpio_vendor_data_is_skipped(self):
        r = acpi.parse_resource_template(
            gpio(pins=(7,), vendor=b"\x01\x02\x03") + end_tag()).gpio_interrupts[0]
        self.assertEqual(r.pins, (7,))
        self.assertIn("GpioInt", "\n".join(
            acpi.parse_resource_template(
                gpio(pins=(7,)) + end_tag()).describe()))


# ---------------------------------------------------------------------------
# Other resources and the template walker
# ---------------------------------------------------------------------------

class TestOtherResources(unittest.TestCase):
    def test_extended_irq(self):
        template = acpi.parse_resource_template(
            extended_irq(flags=5, irqs=(7, 9)) + end_tag())
        r = template.resources[0]
        self.assertEqual(r.kind, "extended-irq")
        self.assertEqual(r.irq_flags, 5)
        self.assertEqual(r.irqs, (7, 9))

    def test_small_irq_mask_and_optional_flags(self):
        template = acpi.parse_resource_template(
            small_irq(mask=0x0005, flags=0x0D) + end_tag())
        r = template.resources[0]
        self.assertEqual(r.kind, "irq")
        self.assertEqual(r.irqs, (0, 2))
        self.assertEqual(r.irq_flags, 0x0D)
        with_flags = acpi.parse_resource_template(
            small_irq(mask=0x0001) + end_tag()).resources[0]
        self.assertIsNone(with_flags.irq_flags)

    def test_unknown_items_are_opaque_but_keep_offsets(self):
        io_item = small_item(acpi.SMALL_IO, b"\x00" * 7)
        vendor = large_item(acpi.LARGE_VENDOR, b"abc")
        template = acpi.parse_resource_template(io_item + vendor + end_tag())
        self.assertEqual([r.kind for r in template.resources],
                         ["io", "vendor"])
        self.assertEqual(template.resources[0].offset, 0)
        self.assertEqual(template.resources[1].offset, len(io_item))
        self.assertEqual(template.resources[1].length, len(vendor))

    def test_bytes_after_end_tag_are_preserved(self):
        template = acpi.parse_resource_template(
            serial_bus() + end_tag(b"\xde\xad"))
        self.assertEqual(template.trailing, b"\xde\xad")
        self.assertEqual(len(template.resources), 1)

    def test_missing_end_tag_is_tolerated(self):
        template = acpi.parse_resource_template(serial_bus())
        self.assertEqual(len(template.resources), 1)
        self.assertEqual(template.trailing, b"")

    def test_empty_template(self):
        template = acpi.parse_resource_template(b"")
        self.assertEqual(template.resources, ())
        self.assertEqual(template.trailing, b"")


# ---------------------------------------------------------------------------
# SpiConnection reduction (used by the probe)
# ---------------------------------------------------------------------------

class TestSpiConnection(unittest.TestCase):
    def test_extraction(self):
        int_flags = (acpi.GPIO_TRIGGERING_MASK
                     | (1 << acpi.GPIO_POLARITY_SHIFT)
                     | acpi.GPIO_SHAREABLE)
        buf = (serial_bus(speed=12_000_000, phase=0, polarity=1,
                          selection=0)
               + gpio(flags=int_flags, pins=(42,))
               + gpio(connection_type=acpi.GPIO_TYPE_IO, flags=0, pins=(9,))
               + end_tag())
        connection = acpi.spi_connection(
            acpi.parse_resource_template(buf))
        self.assertEqual(connection.controller, "\\_SB.PC00.SPI1")
        self.assertEqual(connection.speed_hz, 12_000_000)
        self.assertEqual(connection.mode, 2)       # CPOL=1, CPHA=0
        self.assertEqual(connection.bits_per_word, 8)
        self.assertEqual(connection.chip_select, 0)
        self.assertEqual(connection.irq_pin, 42)
        self.assertIs(connection.irq_triggering, acpi.Triggering.EDGE)
        self.assertIs(connection.irq_polarity, acpi.Polarity.ACTIVE_LOW)
        self.assertTrue(connection.irq_shareable)
        self.assertTrue(connection.irq_active_low)
        self.assertEqual(connection.irq_edge, "rising")
        self.assertEqual(connection.reset_pin, 9)
        self.assertIsNone(connection.reset_active_low)

    def test_connection_without_spi_or_gpio(self):
        connection = acpi.spi_connection(
            acpi.parse_resource_template(end_tag()))
        self.assertIsNone(connection.controller)
        self.assertIsNone(connection.speed_hz)
        self.assertIsNone(connection.mode)
        self.assertIsNone(connection.irq_pin)
        self.assertIsNone(connection.irq_active_low)


# ---------------------------------------------------------------------------
# Malformed templates
# ---------------------------------------------------------------------------

class TestMalformed(unittest.TestCase):
    def assertMalformed(self, buf):
        with self.assertRaises(acpi.AcpiError):
            acpi.parse_resource_template(buf)

    def test_truncated_large_header(self):
        self.assertMalformed(b"\x8e\x01")

    def test_large_length_overruns_the_buffer(self):
        self.assertMalformed(b"\x8e\xff\xff\x00\x00")

    def test_truncated_small_item(self):
        self.assertMalformed(b"\x47\x00")

    def test_truncated_end_tag(self):
        self.assertMalformed(b"\x79")

    def test_serial_bus_type_data_too_short(self):
        self.assertMalformed(
            serial_bus(type_data_length=8) + end_tag())

    def test_serial_bus_body_too_short(self):
        # A short raw SPI descriptor: the outer large item is complete, but
        # the SPI type data does not fit.
        data = (bytes([1, 1, acpi.SERIAL_TYPE_SPI, 0])
                + struct.pack("<H", 0) + bytes([1])
                + struct.pack("<H", 9) + b"\x00" * 5)
        self.assertMalformed(large_item(acpi.LARGE_SERIAL_BUS, data))

    def test_serial_bus_source_offset_outside_descriptor(self):
        self.assertMalformed(serial_bus(source="", res_index=1) + end_tag())

    def test_gpio_body_too_short(self):
        self.assertMalformed(large_item(acpi.LARGE_GPIO, b"\x00" * 19))

    def test_gpio_pin_table_odd_length(self):
        data = (bytes([1, 0, 0, 0]) + struct.pack("<H", 0)
                + bytes([0]) + struct.pack("<H", 0) + struct.pack("<H", 0)
                + struct.pack("<H", 23) + bytes([0])
                + struct.pack("<H", 0) + struct.pack("<H", 0)
                + struct.pack("<H", 0) + b"\x01\x00\x02")
        self.assertMalformed(large_item(acpi.LARGE_GPIO, data))

    def test_gpio_pin_table_out_of_range(self):
        self.assertMalformed(
            gpio(pin_table_offset=0xFFFF) + end_tag())

    def test_gpio_source_out_of_range(self):
        self.assertMalformed(
            gpio(source="", res_index=1, res_offset=0xFFFF) + end_tag())

    def test_extended_irq_truncated(self):
        self.assertMalformed(
            large_item(acpi.LARGE_EXTENDED_IRQ, bytes([0, 2]))
            + b"\x00" * 4)

    def test_unknown_small_name_is_not_an_error(self):
        # 0x7F is a valid small item header (name 0x0F, length 7) only if the
        # bytes exist; an unknown name with a complete body is opaque.
        template = acpi.parse_resource_template(b"\x7f" + b"\x00" * 7)
        self.assertEqual(len(template.resources), 1)
        self.assertTrue(template.resources[0].kind.startswith("small-"))


if __name__ == "__main__":
    unittest.main()
