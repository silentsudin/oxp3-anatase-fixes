"""GET_IMG_DATA: header parsing, chunk accounting, reassembly and checks."""

import unittest

import support
from fpbf63112 import protocol as P


def payload8x8() -> bytes:
    return bytes((x * 8 + y) % 256 for y in range(8) for x in range(8))


class TestImageHeader(unittest.TestCase):
    def test_round_trip(self):
        raw = P.ImageHeader.build(64, 0x1234, status=1)
        self.assertEqual(len(raw), P.ImageHeader.SIZE)
        header = P.ImageHeader.parse(raw)
        self.assertEqual(header.status, 1)
        self.assertEqual(header.total_bytes, 64)
        self.assertEqual(header.byte_sum, 0x1234)
        self.assertEqual(header.sum8_expected, 0x34)

    def test_short_payload(self):
        with self.assertRaises(P.ImageError):
            P.ImageHeader.parse(b"\x00" * 8)

    def test_build_validation(self):
        with self.assertRaises(ValueError):
            P.ImageHeader.build(-1, 0)
        with self.assertRaises(ValueError):
            P.ImageHeader.build(0x100000000, 0)


class TestImageAssembler(unittest.TestCase):
    def test_reassembles_and_checks(self):
        image = payload8x8()
        header = P.ImageHeader(0, len(image), P.sum8(image))
        asm = P.ImageAssembler(header)
        for i in range(0, len(image), 16):
            asm.add(image[i:i + 16])
            self.assertEqual(asm.received, min(i + 16, len(image)))
        self.assertEqual(asm.remaining, 0)
        self.assertEqual(asm.finish(), image)

    def test_one_byte_chunks(self):
        image = b"\x00\x01\x02\x03\xfe\xff"
        asm = P.ImageAssembler.from_total(len(image), P.sum8(image))
        for byte in image:
            asm.add(bytes([byte]))
        self.assertEqual(asm.finish(), image)

    def test_incomplete_image(self):
        image = payload8x8()
        asm = P.ImageAssembler.from_total(len(image), P.sum8(image))
        asm.add(image[:16])
        with self.assertRaises(P.ImageSizeMismatch):
            asm.finish()

    def test_bad_global_sum(self):
        image = payload8x8()
        asm = P.ImageAssembler.from_total(len(image), P.sum8(image) ^ 0x01)
        asm.add(image)
        with self.assertRaises(P.ImageSumMismatch):
            asm.finish()

    def test_rejects_overrun(self):
        asm = P.ImageAssembler.from_total(10, 0)
        asm.add(b"\x00" * 8)
        with self.assertRaises(P.ImageSizeMismatch):
            asm.add(b"\x00" * 3)

    def test_rejects_oversized_chunk(self):
        asm = P.ImageAssembler.from_total(100, 0, max_chunk=16)
        with self.assertRaises(P.ChunkTooLargeError):
            asm.add(b"\x00" * 17)
        asm.add(b"\x00" * 16)          # exactly the limit is allowed
        self.assertEqual(asm.received, 16)

    def test_rejects_empty_chunk(self):
        asm = P.ImageAssembler.from_total(4, 0)
        with self.assertRaises(P.ImageError):
            asm.add(b"")

    def test_optional_chunk_sum(self):
        asm = P.ImageAssembler.from_total(4, P.sum8(b"\x01\x02\x03\x04"))
        asm.add(b"\x01\x02", chunk_sum=P.sum8(b"\x01\x02"))
        asm.add(b"\x03\x04", chunk_sum=P.sum8(b"\x03\x04"))
        self.assertEqual(asm.finish(), b"\x01\x02\x03\x04")
        with self.assertRaises(P.ChunkSumMismatch):
            P.ImageAssembler.from_total(4, 0).add(b"\x01", chunk_sum=0x00)

    def test_default_max_chunk_is_windows_value(self):
        self.assertEqual(P.MAX_IMAGE_CHUNK, 8140)
        asm = P.ImageAssembler.from_total(P.MAX_IMAGE_CHUNK, 0)
        asm.add(b"\x00" * P.MAX_IMAGE_CHUNK)
        self.assertEqual(asm.received, P.MAX_IMAGE_CHUNK)

    def test_status_does_not_affect_assembly(self):
        image = b"\x10\x20\x30"
        header = P.ImageHeader(0x7F, len(image), P.sum8(image))
        asm = P.ImageAssembler(header)
        asm.add(image)
        self.assertEqual(asm.finish(), image)


if __name__ == "__main__":
    unittest.main()
