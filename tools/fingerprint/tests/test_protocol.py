"""Frame arithmetic, CRC, framing rules, parsing and fuzz tests."""

import random
import struct
import unittest
import zlib

import support
from fpbf63112 import protocol as P


class TestArithmetic(unittest.TestCase):
    def test_align16(self):
        self.assertEqual(P.align16(0), 0)
        self.assertEqual(P.align16(1), 16)
        self.assertEqual(P.align16(16), 16)
        self.assertEqual(P.align16(17), 32)
        with self.assertRaises(ValueError):
            P.align16(-1)

    def test_command_block(self):
        self.assertEqual(P.command_block_len(0), 16)   # 10 -> 16
        self.assertEqual(P.command_block_len(6), 16)   # exactly 16
        self.assertEqual(P.command_block_len(7), 32)
        self.assertEqual(P.command_block_len(22), 32)
        with self.assertRaises(ValueError):
            P.command_block_len(-1)

    def test_documented_lengths(self):
        # docs/hw/fingerprint.md: a datum-only query is a 41-byte frame, L=36.
        self.assertEqual(P.length_field(0), 36)
        self.assertEqual(P.frame_size(0), 41)
        self.assertEqual(P.length_field(6), 36)
        self.assertEqual(P.frame_size(7), 57)
        self.assertEqual(P.length_field(32), 68)
        self.assertEqual(P.frame_size(32), 73)

    def test_formula_matches_built_frames(self):
        for data_len in list(range(0, 80)) + [255, 256, 512, 4096]:
            frame = P.build_frame(0xB5, 1, 0, b"\x5a" * data_len)
            self.assertEqual(len(frame), P.frame_size(data_len), data_len)
            self.assertEqual(struct.unpack_from("<I", frame, 1)[0],
                             P.length_field(data_len), data_len)


class TestCRCAndSum(unittest.TestCase):
    def test_crc32_check_value(self):
        # CRC-32/ISO-HDLC check value for "123456789"
        self.assertEqual(P.crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(P.crc32(b"123456789"), zlib.crc32(b"123456789"))

    def test_crc32_empty(self):
        self.assertEqual(P.crc32(b""), 0)

    def test_sum8_wraps(self):
        self.assertEqual(P.sum8(b""), 0)
        self.assertEqual(P.sum8(b"\x01\x02"), 3)
        self.assertEqual(P.sum8(b"\xff" * 3), 0xFD)
        self.assertEqual(P.sum8(b"\xff" * 17), 0xFF * 17 & 0xFF)


class TestBuild(unittest.TestCase):
    def test_minimal_frame_layout(self):
        frame = P.build_frame(0xA1)
        self.assertEqual(len(frame), 41)
        self.assertEqual(frame[0], 0xAA)
        self.assertEqual(struct.unpack_from("<I", frame, 1)[0], 36)
        self.assertEqual(frame[5:21], b"\x00" * 16)
        self.assertEqual(frame[21:25], bytes([0x4C, 0xA1, 0x00, 0x00]))
        self.assertEqual(frame[25:27], b"\x00\x00")
        self.assertEqual(frame[27:31], b"\x00\x00\x00\x00")
        self.assertEqual(frame[31:37], b"\x00" * 6)     # padding to 16
        self.assertEqual(struct.unpack_from("<I", frame, 37)[0],
                         P.crc32(frame[:37]))
        self.assertEqual(len(frame) - 4, 37)

    def test_fields_encoded(self):
        frame = P.build_frame(0xB5, phase=1, arg=0xBEEF,
                              data=b"abcdef", status=0x42,
                              reserved=b"\x11" * 16)
        parsed = P.parse_frame(frame, require_magic=True)
        self.assertEqual(parsed.opcode, 0xB5)
        self.assertEqual(parsed.phase, 1)
        self.assertEqual(parsed.arg, 0xBEEF)
        self.assertEqual(parsed.status, 0x42)
        self.assertEqual(parsed.data, b"abcdef")
        self.assertEqual(parsed.reserved, b"\x11" * 16)
        self.assertEqual(parsed.length, 36)
        self.assertEqual(len(frame), 41)

    def test_zero_padding_and_alignment(self):
        for data_len in range(0, 48):
            frame = P.build_frame(0xC2, 0, 0, b"\xaa" * data_len)
            parsed = P.parse_frame(frame, require_padding_zero=True)
            self.assertEqual(parsed.data_len, data_len)

    def test_rejects_out_of_range(self):
        bad = [
            dict(opcode=-1),
            dict(opcode=0x100),
            dict(opcode=0xA1, phase=0x100),
            dict(opcode=0xA1, status=0x100),
            dict(opcode=0xA1, arg=0x10000),
            dict(opcode=0xA1, reserved=b"\x00" * 15),
            dict(opcode=0xA1, data=b"\x00" * (P.MAX_DATA_LEN + 1)),
        ]
        for kwargs in bad:
            with self.assertRaises(ValueError, msg=str(kwargs)):
                P.build_frame(**kwargs)
        # bool is an int subclass, so it is accepted as opcode 0x01
        self.assertEqual(len(P.build_frame(True)), 41)

    def test_data_len_boundary_at_max(self):
        frame = P.build_frame(0x01, 0, 0, b"\x00" * P.MAX_DATA_LEN)
        self.assertEqual(len(frame), P.frame_size(P.MAX_DATA_LEN))
        self.assertEqual(P.parse_frame(frame).data_len, P.MAX_DATA_LEN)


class TestParse(unittest.TestCase):
    def setUp(self):
        self.frame = P.build_frame(0xC2, 0, 0x1234, b"\x09\x08\x07")

    def test_round_trip(self):
        parsed = P.parse_frame(self.frame)
        self.assertEqual(parsed.raw, self.frame)
        self.assertEqual(parsed.crc, P.crc32(self.frame[:-4]))
        self.assertEqual(parsed.data, b"\x09\x08\x07")
        self.assertIn("CMD_QUERY_SENSOR_INFO", parsed.describe())

    def test_body_views(self):
        parsed = P.parse_frame(self.frame)
        # request: status byte is zero, so both views start with it/after it
        self.assertEqual(parsed.body_from_status[:1], b"\x00")
        self.assertEqual(parsed.body_from_arg[:2], b"\x34\x12")

    def test_rejects_bad_start_byte(self):
        bad = bytearray(self.frame)
        bad[0] = 0x55
        with self.assertRaises(P.BadStartByteError):
            P.parse_frame(bytes(bad))

    def test_rejects_trailing_garbage(self):
        with self.assertRaises(P.LengthMismatchError):
            P.parse_frame(self.frame + b"\x00")

    def test_rejects_short_buffers(self):
        for cut in range(0, len(self.frame)):
            with self.assertRaises(P.FrameError, msg=f"cut={cut}"):
                P.parse_frame(self.frame[:cut])

    def test_rejects_bad_length_field(self):
        bad = bytearray(self.frame)
        struct.pack_into("<I", bad, 1, struct.unpack_from("<I", bad, 1)[0] + 16)
        struct.pack_into("<I", bad, len(bad) - 4, P.crc32(bytes(bad[:-4])))
        with self.assertRaises(P.LengthMismatchError):
            P.parse_frame(bytes(bad))

    def test_rejects_unaligned_length(self):
        # L consistent with the buffer, but the command block is 17 bytes:
        # not a multiple of 16, so it cannot be align16(10 + data_len).
        bad = bytearray(P.build_frame(0xA1))     # 41B, block 16
        struct.pack_into("<I", bad, 1, 20 + 17)  # L = 37
        bad = bad[:-4] + b"\x00" * 1 + b"\x00" * 4
        struct.pack_into("<I", bad, len(bad) - 4, P.crc32(bytes(bad[:-4])))
        self.assertEqual(len(bad), 42)
        with self.assertRaises(P.BadLengthFieldError):
            P.parse_frame(bytes(bad))

    def test_rejects_bad_magic(self):
        bad = bytearray(self.frame)
        bad[21] = 0x4D
        struct.pack_into("<I", bad, len(bad) - 4, P.crc32(bytes(bad[:-4])))
        with self.assertRaises(P.BadMagicError):
            P.parse_frame(bytes(bad))
        # ... unless the caller explicitly allows a magic-less answer
        self.assertEqual(P.parse_frame(bytes(bad), require_magic=False).magic,
                         0x4D)

    def test_rejects_data_len_past_block(self):
        bad = bytearray(self.frame)          # block is 32, capacity 22
        struct.pack_into("<I", bad, 27, 23)
        struct.pack_into("<I", bad, len(bad) - 4, P.crc32(bytes(bad[:-4])))
        with self.assertRaises(P.BadDataLengthError):
            P.parse_frame(bytes(bad))

    def test_rejects_bad_crc(self):
        bad = bytearray(self.frame)
        bad[-1] ^= 0xFF
        with self.assertRaises(P.CRCMismatchError):
            P.parse_frame(bytes(bad))

    def test_crc_error_carries_values(self):
        bad = bytearray(self.frame)
        bad[-1] ^= 0xFF
        try:
            P.parse_frame(bytes(bad))
        except P.CRCMismatchError as exc:
            self.assertNotEqual(exc.expected, exc.actual)
        else:                                  # pragma: no cover
            self.fail("expected CRCMismatchError")

    def test_rejects_nonzero_padding_when_asked(self):
        frame = P.build_frame(0xA1, 0, 0, b"\x01")     # 5 padding bytes
        parsed = P.parse_frame(frame)
        bad = bytearray(frame)
        bad[32] = 0x01                          # a padding byte
        struct.pack_into("<I", bad, len(bad) - 4, P.crc32(bytes(bad[:-4])))
        with self.assertRaises(P.FrameError):
            P.parse_frame(bytes(bad), require_padding_zero=True)
        self.assertEqual(P.parse_frame(bytes(bad)).data, b"\x01")
        self.assertEqual(parsed.data_len, 1)


class TestScan(unittest.TestCase):
    def test_finds_single_frame_behind_junk(self):
        frame = P.build_frame(0xA1)
        frames, errors = P.scan_frames(b"\xff" * 8 + frame + b"\x00" * 32)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].opcode, 0xA1)
        self.assertEqual(errors, [])

    def test_finds_back_to_back_frames(self):
        a = P.build_frame(0xA1)
        b = P.build_frame(0xC2, 0, 0, b"\x01\x02")
        frames, errors = P.scan_frames(a + b)
        self.assertEqual([f.opcode for f in frames], [0xA1, 0xC2])
        self.assertEqual(errors, [])

    def test_resynchronises_after_bad_crc(self):
        a = P.build_frame(0xA1)
        bad = bytearray(P.build_frame(0xA2))
        bad[-1] ^= 0xFF
        b = P.build_frame(0xC2)
        frames, errors = P.scan_frames(a + bytes(bad) + b)
        self.assertEqual([f.opcode for f in frames], [0xA1, 0xC2])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0].error, P.CRCMismatchError)

    def test_truncated_tail_is_reported(self):
        frame = P.build_frame(0xC2, 0, 0, b"\x00" * 20)
        frames, errors = P.scan_frames(frame[:len(frame) - 3])
        self.assertEqual(frames, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0].error, P.TruncatedFrameError)
        self.assertEqual(errors[0].offset, 0)

    def test_implausible_length_is_reported(self):
        bad = bytearray(P.build_frame(0xA1))
        struct.pack_into("<I", bad, 1, 0x100000)
        frames, errors = P.scan_frames(bytes(bad))
        self.assertEqual(frames, [])
        self.assertTrue(any(isinstance(e.error, P.BadLengthFieldError)
                            for e in errors))

    def test_all_junk(self):
        frames, errors = P.scan_frames(b"\xff" * 128 + b"\x00" * 16)
        self.assertEqual(frames, [])
        self.assertEqual(errors, [])

    def test_empty(self):
        self.assertEqual(P.scan_frames(b""), ([], []))

    def test_every_scanned_frame_round_trips(self):
        stream = b"".join(P.build_frame(op, 0, 0, b"\x33" * (i % 9))
                          for i, op in enumerate((0xA1, 0xC2, 0xB5)))
        frames, errors = P.scan_frames(stream)
        self.assertEqual(errors, [])
        for frame in frames:
            self.assertEqual(P.parse_frame(frame.raw), frame)

    def test_random_buffer_never_raises(self):
        rng = random.Random(20261109)
        for _ in range(300):
            blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 400)))
            frames, errors = P.scan_frames(blob)
            for frame in frames:
                self.assertIsInstance(frame, P.Frame)
                self.assertEqual(P.parse_frame(frame.raw), frame)
            for err in errors:
                self.assertIsInstance(err.error, P.FrameError)


class TestFuzzRoundTrip(unittest.TestCase):
    def test_random_frames_round_trip(self):
        rng = random.Random(0x63112)
        ops = [int(c) for c in P.Command]
        for _ in range(400):
            opcode = rng.choice(ops)
            phase = rng.randrange(0, 4)
            arg = rng.randrange(0, 0x10000)
            data_len = rng.choice([0, 1, 2, 5, 6, 7, 15, 16, 17, 31, 32,
                                   rng.randrange(0, 300)])
            data = bytes(rng.randrange(256) for _ in range(data_len))
            frame = P.build_frame(opcode, phase, arg, data)
            parsed = P.parse_frame(frame, require_padding_zero=True)
            self.assertEqual(parsed.opcode, opcode)
            self.assertEqual(parsed.phase, phase)
            self.assertEqual(parsed.arg, arg)
            self.assertEqual(parsed.data, data)
            self.assertEqual(len(frame), P.frame_size(data_len))
            self.assertEqual(P.build_frame(opcode, phase, arg, data), frame)

    def test_single_byte_mutations_are_rejected(self):
        allowed = []
        for data_len in (0, 1, 7, 32):
            frame = P.build_frame(0xB5, 1, 0x0102, b"\x7e" * data_len)
            for index in range(len(frame)):
                mutated = bytearray(frame)
                mutated[index] ^= 0xFF
                try:
                    P.parse_frame(bytes(mutated))
                except P.FrameError:
                    continue
                allowed.append((data_len, index))
        # Only a CRC-32 collision could be accepted; there must be none.
        self.assertEqual(allowed, [])

    def test_truncated_prefixes_never_parse(self):
        frame = P.build_frame(0xB5, 1, 0, b"\x42" * 20)
        for cut in range(len(frame)):
            with self.assertRaises(P.FrameError, msg=f"cut={cut}"):
                P.parse_frame(frame[:cut])


if __name__ == "__main__":
    unittest.main()
