"""Regression: _CRS may be a method over named ResourceTemplate buffers.

The OneXPlayer 3 firmware declares

    Name (SBFB, ResourceTemplate () { SpiSerialBusV2 (...) })
    Method (_CRS) { Return (ConcatenateResTemplate (SBFB, SBFG)) }

so the resource bytes precede the ``_CRS`` nameseg.  The scanner must find
them by scanning buffers, not only by looking immediately after ``_CRS``.

The bytes below are constructed here from the documented ACPI layout; no
firmware data is stored in the repository.
"""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fpbf63112 import acpi, aml  # noqa: E402


def spi_serial_bus_v2(speed_hz=4_000_000, bits=8, source="\\_SB.PC00.SPI1"):
    data = bytearray()
    data += bytes([0x02, 0x01, 0x02, 0x02])      # revision, index, SPI, flags
    data += struct.pack("<H", 0)                  # type-specific flags
    data += bytes([0x01])                         # type revision
    data += struct.pack("<H", 9)                  # type data length
    data += struct.pack("<I", speed_hz)           # connection speed
    data += bytes([bits, 0, 0])                   # bit length, phase, polarity
    data += struct.pack("<H", 0)                  # device selection
    data += source.encode() + b"\x00"
    body = bytes(data)
    return b"\x8e" + struct.pack("<H", len(body)) + body


def buffer_wrap(resource_bytes):
    payload = resource_bytes + b"\x79\x00"
    # PkgLength counts the length bytes themselves plus the fields after them:
    # 1 (this byte) + 1 (BytePrefix) + 1 (size) + payload.
    assert 3 + len(payload) < 0x40
    return b"\x11" + bytes([3 + len(payload)]) + b"\x0a" + bytes([len(payload)]) + payload


class NamedBufferCrsTest(unittest.TestCase):
    def test_named_template_before_crs_nameseg_is_found(self):
        desc = spi_serial_bus_v2()
        table = b"FPRT" + buffer_wrap(desc) + b"_CRS"
        cands = aml.find_crs_candidates(table)
        self.assertTrue(cands, "no candidate found for a named-buffer _CRS")
        template = acpi.parse_resource_template(cands[0].payload)
        spi = template.spi
        self.assertIsNotNone(spi)
        self.assertEqual(spi.connection_speed_hz, 4_000_000)
        self.assertEqual(spi.data_bit_length, 8)
        self.assertEqual(spi.spi_mode, 0)


if __name__ == "__main__":
    unittest.main()
