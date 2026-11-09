"""AML scan for the fingerprint ``_CRS``: synthetic tables, no firmware read.

Every byte here is built by the helpers below from the AML specification's
object encoding (PkgLength, ``DeviceOp``, ``MethodOp``, ``ReturnOp``,
``BufferOp``, constant integer prefixes).  Nothing is copied from a real DSDT
and no test reads ``/sys/firmware``: the point is to pin the *scan* the
privileged runbook performs, including the ways a table can be malformed.

The resource-template bytes themselves are built with the same constructors as
``test_acpi.py``, and every accepted candidate is re-parsed by
:mod:`fpbf63112.acpi`, which is the validator the scan relies on.
"""

import pathlib
import struct
import tempfile
import unittest

import support  # noqa: F401  (sets sys.path)
from fpbf63112 import acpi, aml


# ---------------------------------------------------------------------------
# AML builders
# ---------------------------------------------------------------------------

def encode_pkg_length(content_len):
    """Encode a PkgLength whose object body is ``content_len`` bytes."""
    for extra in (1, 2, 3, 4):
        total = content_len + extra
        if extra == 1:
            if total < 0x40:
                return bytes([total])
            continue
        limit = 1 << (4 + 8 * (extra - 1))
        if total < limit:
            first = ((extra - 1) << 6) | (total & 0x0F)
            return bytes([first]) + (total >> 4).to_bytes(extra - 1, "little")
    raise ValueError("object too large for a PkgLength")


def encode_int(value):
    if value == 0:
        return b"\x00"
    if value == 1:
        return b"\x01"
    if value <= 0xFF:
        return b"\x0a" + bytes([value])
    if value <= 0xFFFF:
        return b"\x0b" + struct.pack("<H", value)
    return b"\x0c" + struct.pack("<I", value)


def device(name, body, ext=True):
    assert len(name) == 4
    header = b"\x5b\x82" if ext else b"\x82"
    return header + encode_pkg_length(4 + len(body)) + name.encode() + body


def method(name, body):
    assert len(name) == 4
    content = name.encode() + b"\x00" + body
    return b"\x14" + encode_pkg_length(len(content)) + content


def name_object(name, value):
    assert len(name) == 4
    return b"\x08" + name.encode() + value


def return_buffer(payload):
    content = encode_int(len(payload)) + payload
    return b"\xa4\x11" + encode_pkg_length(len(content)) + content


def hid_string(hid):
    raw = hid.encode() + b"\x00"
    return name_object("_HID", b"\x0d" + raw)


# ---------------------------------------------------------------------------
# Resource-template builders (ACPI 6.x field order; same as test_acpi.py)
# ---------------------------------------------------------------------------

def large_item(name, data):
    return bytes([0x80 | name]) + struct.pack("<H", len(data)) + data


def end_tag():
    return b"\x79\x00"


def spi_template(speed=1_000_000, bit_length=8, phase=0, polarity=0,
                 selection=0, source="\\_SB.PC00.SPI1"):
    payload = (struct.pack("<I", speed)
               + bytes([bit_length, phase, polarity])
               + struct.pack("<H", selection))
    data = (bytes([1, 1, acpi.SERIAL_TYPE_SPI, 0])
            + struct.pack("<H", 0)
            + bytes([1])
            + struct.pack("<H", len(payload))
            + payload
            + source.encode() + b"\x00")
    return large_item(acpi.LARGE_SERIAL_BUS, data) + end_tag()


def i2c_template(speed=400_000, address=0x50):
    payload = struct.pack("<I", speed) + struct.pack("<H", address)
    data = (bytes([1, 1, acpi.SERIAL_TYPE_I2C, 0])
            + struct.pack("<H", 0)
            + bytes([1])
            + struct.pack("<H", len(payload))
            + payload
            + b"\\_SB.PC00.I2C0\x00")
    return large_item(acpi.LARGE_SERIAL_BUS, data) + end_tag()


def fingerprint_device(template=None, speed=1_000_000):
    template = spi_template(speed=speed) if template is None else template
    return device("FPRT", hid_string("BF63112") + method("_CRS",
                                                        return_buffer(template)))


# ---------------------------------------------------------------------------
# PkgLength / integer primitives
# ---------------------------------------------------------------------------

class PkgLengthTest(unittest.TestCase):
    def test_single_byte(self):
        for size in (0, 1, 0x3D):
            raw = encode_pkg_length(size)
            self.assertEqual(len(raw), 1)
            total, next_off = aml.read_pkg_length(raw, 0)
            self.assertEqual((total, next_off), (size + 1, 1))

    def test_multi_byte_round_trip(self):
        for size in (0x3E, 0x40, 0x100, 0x1000 - 2, 0x4000):
            with self.subTest(size=size):
                raw = encode_pkg_length(size)
                total, next_off = aml.read_pkg_length(raw, 0)
                self.assertEqual(total, size + len(raw))
                self.assertEqual(next_off, len(raw))

    def test_truncated_raises(self):
        with self.assertRaises(aml.AmlError):
            aml.read_pkg_length(b"", 0)
        with self.assertRaises(aml.AmlError):
            aml.read_pkg_length(bytes([0x40]), 0)  # 1 extra byte, absent
        with self.assertRaises(aml.AmlError):
            aml.read_pkg_length(bytes([0x80]), 0)  # 2 extra bytes, absent


class IntegerTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(aml.read_integer(b"\x00", 0), (0, 1))
        self.assertEqual(aml.read_integer(b"\x01", 0), (1, 1))
        self.assertEqual(aml.read_integer(b"\x0a\x2a", 0), (0x2A, 2))
        self.assertEqual(aml.read_integer(b"\x0b\x34\x12", 0), (0x1234, 3))
        self.assertEqual(aml.read_integer(b"\x0c\x78\x56\x34\x12", 0),
                         (0x12345678, 5))
        self.assertEqual(
            aml.read_integer(b"\x0e" + bytes(range(8)), 0),
            (int.from_bytes(bytes(range(8)), "little"), 9))

    def test_unknown_and_truncated(self):
        self.assertEqual(aml.read_integer(b"\x5c_SB", 0), (None, 0))
        with self.assertRaises(aml.AmlError):
            aml.read_integer(b"\x0c\x01", 0)
        with self.assertRaises(aml.AmlError):
            aml.read_integer(b"", 0)


# ---------------------------------------------------------------------------
# Finding the candidate
# ---------------------------------------------------------------------------

class FindCrsTest(unittest.TestCase):
    def test_single_device(self):
        template = spi_template()
        table = fingerprint_device(template)
        candidates = aml.find_crs_candidates(table)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.payload, template)
        self.assertTrue(candidate.scoped)
        self.assertEqual(candidate.marker, b"FPRT")
        self.assertEqual(candidate.size, len(template))
        self.assertEqual(candidate.sha256,
                         __import__("hashlib").sha256(template).hexdigest())
        # The validator agrees with the parser the probe uses.
        parsed = acpi.parse_resource_template(candidate.payload)
        self.assertEqual(parsed.spi.connection_speed_hz, 1_000_000)

    def test_speed_is_carried_through(self):
        table = fingerprint_device(spi_template(speed=24_000_000, phase=1,
                                                polarity=1))
        candidate, ambiguous = aml.select_candidate(
            aml.find_crs_candidates(table))
        self.assertIsNotNone(candidate)
        self.assertFalse(ambiguous)
        connection = acpi.spi_connection(
            acpi.parse_resource_template(candidate.payload))
        self.assertEqual(connection.speed_hz, 24_000_000)
        self.assertEqual(connection.mode, 3)

    def test_name_object_form_is_found(self):
        template = spi_template()
        table = device("FPRT", hid_string("BF63112")
                       + name_object("_CRS", b"\x11"
                                     + encode_pkg_length(
                                         len(encode_int(len(template)))
                                         + len(template))
                                     + encode_int(len(template))
                                     + template))
        candidates = aml.find_crs_candidates(table)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].payload, template)

    def test_device_scope_disambiguates_a_decoy(self):
        good = spi_template(speed=1_000_000)
        decoy = spi_template(speed=50_000_000)
        table = (b"\x0c" + b"DSDT" + b"\x00" * 8
                 + device("TCHP", hid_string("ELAN0001")
                          + method("_CRS", return_buffer(decoy)))
                 + fingerprint_device(good))
        candidates = aml.find_crs_candidates(table)
        self.assertEqual(len(candidates), 2)
        # The decoy device has no FPRT/BF63112 marker ahead of it, so it is
        # reported unscoped and ranked last - never silently preferred.
        self.assertFalse(candidates[-1].scoped)
        best, ambiguous = aml.select_candidate(candidates)
        self.assertEqual(best.payload, good)
        self.assertEqual(best.marker, b"FPRT")
        self.assertFalse(ambiguous)

    def test_fprt_name_alone_is_enough(self):
        template = spi_template()
        table = device("FPRT", method("_CRS", return_buffer(template)))
        candidates = aml.find_crs_candidates(table)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].marker, b"FPRT")

    def test_non_spi_template_is_not_reported(self):
        table = fingerprint_device(i2c_template())
        self.assertEqual(aml.find_crs_candidates(table), ())

    def test_garbage_buffer_is_not_reported(self):
        table = device("FPRT", hid_string("BF63112")
                       + method("_CRS", return_buffer(b"\xde\xad\xbe\xef")))
        self.assertEqual(aml.find_crs_candidates(table), ())

    def test_duplicate_payloads_are_merged(self):
        template = spi_template()
        table = device("FPRT", hid_string("BF63112")
                       + method("_CRS", return_buffer(template))
                       + method("_CRS", return_buffer(template)))
        self.assertEqual(len(aml.find_crs_candidates(table)), 1)

    def test_truncated_table_is_not_found_and_does_not_raise(self):
        table = fingerprint_device()
        for cut in (2, 6, 12, len(table) - 20, len(table) - 2):
            with self.subTest(cut=cut):
                self.assertEqual(aml.find_crs_candidates(table[:cut]), ())

    def test_empty_and_random_inputs_do_not_raise(self):
        self.assertEqual(aml.find_crs_candidates(b""), ())
        self.assertEqual(aml.find_crs_candidates(b"_CRS"), ())
        self.assertEqual(aml.find_crs_candidates(b"\xa4\x11" * 64), ())

    def test_no_crs_at_all(self):
        table = device("FPRT", hid_string("BF63112"))
        self.assertEqual(aml.find_crs_candidates(table), ())

    def test_second_controller_does_not_steal_scope(self):
        # This unit has \_SB.PC00.SPI1.FPRT and an unpopulated
        # \_SB.PC00.SPI2.FPRT; the SPI2 one must not be chosen, and the
        # resource-source *string* "\_SB.PC00.SPI1" inside the first
        # template must not be read as a controller NameSeg.
        spi1 = spi_template(speed=1_000_000)
        spi2 = spi_template(speed=8_000_000, source="\\_SB.PC00.SPI2")
        table = (device("SPI1", hid_string("BF63112")
                        + device("FPRT", hid_string("BF63112")
                                 + method("_CRS", return_buffer(spi1))))
                 + device("SPI2", hid_string("BF63112")
                          + device("FPRT", hid_string("BF63112")
                                   + method("_CRS", return_buffer(spi2)))))
        candidates = aml.find_crs_candidates(table)
        self.assertEqual(len(candidates), 2)
        best, ambiguous = aml.select_candidate(candidates)
        self.assertEqual(best.payload, spi1)
        self.assertEqual(best.controller, b"SPI1")
        self.assertFalse(ambiguous)

    def test_controller_string_alone_is_not_a_scope(self):
        table = device("FPRT", hid_string("BF63112")
                       + method("_CRS",
                                return_buffer(spi_template(
                                    source="\\_SB.PC00.SPI1"))))
        candidates = aml.find_crs_candidates(table)
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0].controller)
        self.assertEqual(candidates[0].marker, b"FPRT")


class SelectCandidateTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(aml.select_candidate(()), (None, False))

    def test_ambiguous_identical_distance(self):
        # Two unscoped candidates: the ranking cannot tell them apart.
        a = aml.CrsCandidate(offset=10, buffer_offset=20, payload=b"a",
                             marker=None, distance=None)
        b = aml.CrsCandidate(offset=10, buffer_offset=40, payload=b"b",
                             marker=None, distance=None)
        chosen, ambiguous = aml.select_candidate((a, b))
        self.assertIs(chosen, a)
        self.assertTrue(ambiguous)

    def test_not_ambiguous_when_distance_differs(self):
        near = aml.CrsCandidate(offset=10, buffer_offset=20, payload=b"a",
                                marker=b"FPRT", distance=4)
        far = aml.CrsCandidate(offset=90, buffer_offset=99, payload=b"b",
                               marker=b"FPRT", distance=64)
        self.assertEqual(aml.select_candidate((near, far)), (near, False))

    def test_describe_is_stable(self):
        candidate = aml.CrsCandidate(offset=0x10, buffer_offset=0x20,
                                     payload=spi_template(),
                                     marker=b"FPRT", distance=8)
        text = candidate.describe()
        self.assertIn("_CRS @0x10", text)
        self.assertIn("buffer @0x20", text)
        self.assertIn("FPRT+8 B", text)


class ScanTablesTest(unittest.TestCase):
    def test_scans_only_readable_tables(self):
        table = fingerprint_device()
        with tempfile.TemporaryDirectory() as tmp:
            good = pathlib.Path(tmp) / "DSDT.dat"
            good.write_bytes(table)
            missing = pathlib.Path(tmp) / "SSDT9.dat"
            scanned, candidates = aml.scan_tables([good, missing])
        self.assertEqual(scanned, [str(good)])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].payload, spi_template())

    def test_empty_list(self):
        self.assertEqual(aml.scan_tables([]), ([], ()))


class TableSweepTest(unittest.TestCase):
    """A table with every construct around the candidate must still work."""

    def test_candidate_after_unrelated_methods(self):
        noise = (method("_STA", b"\xa4\x0a\x0f")
                 + method("_DSM", b"\xa4\x00")
                 + name_object("FILN", b"\x0a\x03"))
        table = (device("SPI1", noise)
                 + device("FPRT", hid_string("BF63112") + noise
                          + method("_CRS", return_buffer(spi_template()))))
        candidates = aml.find_crs_candidates(table)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].scoped)


if __name__ == "__main__":
    unittest.main()
