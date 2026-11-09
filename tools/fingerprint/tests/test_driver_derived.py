"""Tests for layouts recovered by reverse-engineering the Windows driver.

Provenance for every offset asserted here is in ``docs/hw/fingerprint.md``
(section "What the Windows driver actually does").  The vectors are *synthetic*:
they are built from the recovered layout with made-up values, not captured from
a sensor and not taken from vendor code.  Nothing here proves the hardware
behaves this way - it only pins the layout this repository believes, so a future
change has to be deliberate.
"""

import struct
import unittest

import support  # noqa: F401  (sets sys.path)
import fpbf63112.protocol as P


class TestAnswerFraming(unittest.TestCase):
    """The answer uses the request's offsets; the body starts at 31."""

    def test_body_offset_is_the_payload_offset(self):
        self.assertEqual(P.FRAME_OFF_BODY, 31)
        self.assertEqual(P.FRAME_OFF_BODY, P.FRAME_OFF_DATA)

    def test_frame_body_slices_the_payload(self):
        frame = P.parse_frame(P.build_frame(0xC1, 0, 0x1234, b"\x09\x08\x07"))
        self.assertEqual(frame.body, b"\x09\x08\x07")

    def test_body_is_inside_the_crc_covered_region(self):
        raw = P.build_frame(0xA0, 0, 0, b"\x01\x02")
        self.assertEqual(P.crc32(raw[:-4]), struct.unpack("<I", raw[-4:])[0])
        self.assertLess(P.FRAME_OFF_BODY, len(raw) - 4)

    def test_rx_limits_match_the_driver(self):
        # FUN_18001AC60 refuses a reassembly larger than 0x16829 bytes.
        self.assertEqual(P.RX_MAX_FRAME, 0x16829)


class TestSensorInfo(unittest.TestCase):
    def test_round_trip(self):
        raw = P.SensorInfo.build(64, 80, field0=0x1111, field2=0x2222,
                                 flags=0x05)
        info = P.SensorInfo.parse(raw)
        self.assertEqual((info.field0, info.field2), (0x1111, 0x2222))
        self.assertEqual((info.width, info.height), (64, 80))
        self.assertEqual(info.flags, 0x05)
        self.assertEqual(info.pixels, 64 * 80)

    def test_width_height_offsets(self):
        # FUN_180019420 stores u32 @0 and u32 @4, and the image path multiplies
        # the u16 at +4 by the u16 at +6: width then height.
        raw = P.SensorInfo.build(0x00C8, 0x012C)
        self.assertEqual(struct.unpack_from("<HH", raw, 4), (0x00C8, 0x012C))

    def test_record_is_nine_bytes(self):
        self.assertEqual(P.SensorInfo.SIZE, 9)
        self.assertEqual(len(P.SensorInfo.build(1, 2)), 9)

    def test_short_record_rejected(self):
        with self.assertRaises(P.ProtocolError):
            P.SensorInfo.parse(b"\x00" * 8)


class TestFirmwareInfo(unittest.TestCase):
    def test_layout_and_round_trip(self):
        raw = P.FirmwareInfo.build(0x01, 0x01, 0x00, 0x00010700)
        info = P.FirmwareInfo.parse(raw)
        self.assertEqual((info.state, info.project_hi, info.project_lo),
                         (0x01, 0x01, 0x00))
        self.assertEqual(info.version, 0x00010700)
        self.assertEqual(info.version_text, "1.1792")
        self.assertEqual(len(raw), 7)

    def test_short_record_rejected(self):
        with self.assertRaises(P.ProtocolError):
            P.FirmwareInfo.parse(b"\x00" * 6)


class TestTemplateHandle(unittest.TestCase):
    def test_round_trip_is_eighteen_bytes(self):
        payload = bytes(range(16))
        raw = P.TemplateHandle.build(0xBEEF, payload)
        self.assertEqual(len(raw), 18)
        self.assertEqual(P.TEMPLATE_HANDLE_SIZE, 18)
        handle = P.TemplateHandle.parse(raw)
        self.assertEqual(handle.prefix, 0xBEEF)
        self.assertEqual(handle.payload, payload)

    def test_wrong_payload_length_rejected(self):
        with self.assertRaises(ValueError):
            P.TemplateHandle.build(1, b"\x00" * 15)

    def test_short_handle_rejected(self):
        with self.assertRaises(P.ProtocolError):
            P.TemplateHandle.parse(b"\x00" * 17)

    def test_enrol_and_identify_carry_the_handle_after_a_status_byte(self):
        # status + 18-byte handle = 19 = the "> 0x12" check both handlers make.
        body = b"\x00" + P.TemplateHandle.build(7, bytes(16))
        self.assertEqual(len(body), 19)
        self.assertTrue(P.COMMANDS[P.Command.ENROLL_FINGERPRINT]
                        .answer_has_status)
        self.assertTrue(P.COMMANDS[P.Command.IDENTIFY_FINGERPRINT]
                        .answer_has_status)


class TestRootKeyAnswer(unittest.TestCase):
    def _answer(self, klen=16, status=0):
        return P.RootKeyAnswer.build(bytes(range(64)), b"\xAB" * 32,
                                     bytes(range(klen)), status=status)

    def test_round_trip(self):
        raw = self._answer()
        ans = P.RootKeyAnswer.parse(raw)
        self.assertEqual(ans.status, 0)
        self.assertEqual(ans.public_key, bytes(range(64)))
        self.assertEqual(ans.tag, b"\xAB" * 32)
        self.assertEqual(ans.key_len, 16)
        self.assertEqual(ans.wrapped_key, bytes(range(16)))

    def test_minimum_size(self):
        # FUN_1800185A0 rejects anything shorter than 0x71 bytes.
        self.assertEqual(P.RootKeyAnswer.MIN_SIZE, 0x71)
        with self.assertRaises(P.ProtocolError):
            P.RootKeyAnswer.parse(b"\x00" * 0x70)

    def test_key_length_must_match_the_data(self):
        raw = bytearray(self._answer(klen=16))
        struct.pack_into("<I", raw, 0x61, 32)
        with self.assertRaises(P.ProtocolError):
            P.RootKeyAnswer.parse(bytes(raw))

    def test_parse_does_not_authenticate(self):
        # A wrong tag still parses: this helper is a layout split only.
        ans = P.RootKeyAnswer.parse(self._answer())
        self.assertEqual(len(ans.tag), 32)


class TestImageHeaderOffsets(unittest.TestCase):
    def test_size_is_the_driver_read_length(self):
        self.assertEqual(P.ImageHeader.SIZE, 0x18)
        self.assertEqual(P.GETIMG_OFF_TOTAL, 4)
        self.assertEqual(P.GETIMG_OFF_SUM, 0x14)

    def test_round_trip(self):
        raw = P.ImageHeader.build(320 * 240, 0x1234, status=0)
        self.assertEqual(len(raw), 0x18)
        header = P.ImageHeader.parse(raw)
        self.assertEqual(header.status, 0)
        self.assertEqual(header.total_bytes, 320 * 240)
        self.assertEqual(header.byte_sum, 0x1234)
        self.assertEqual(header.sum8_expected, 0x34)

    def test_fields_are_not_packed(self):
        raw = P.ImageHeader.build(0x11223344, 0x55667788)
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], 0x11223344)
        self.assertEqual(struct.unpack_from("<I", raw, 0x14)[0], 0x55667788)
        # bytes 8..0x13 are not part of either field
        self.assertEqual(raw[8:0x14], b"\x00" * (0x14 - 8))

    def test_short_header_rejected(self):
        with self.assertRaises(P.ImageError):
            P.ImageHeader.parse(b"\x00" * 8)


class TestOpcodeTableConsistency(unittest.TestCase):
    """The table the driver dispatches on, plus our safety classification."""

    def test_expected_opcodes(self):
        expected = {
            P.Command.QUERY_FIRMWARE_VER: 0xA0,
            P.Command.QUERY_MODULE_SCHEME_INFORMATION: 0xA1,
            P.Command.QUERY_ROOT_KEY: 0xA2,
            P.Command.ENROLL_FINGERPRINT: 0xB0,
            P.Command.IDENTIFY_FINGERPRINT: 0xB1,
            P.Command.REMOVE_FINGERPRINT: 0xB2,
            P.Command.QUERY_FINGERPRINT_COUNT: 0xB3,
            P.Command.GET_IMG_DATA: 0xB4,
            P.Command.DETECT_FINGERPRINT_PRESS: 0xB5,
            P.Command.SLEEP: 0xB6,
            P.Command.QUERY_MODULE_UID: 0xC0,
            P.Command.QUERY_SENSOR_INFO: 0xC1,
            P.Command.JTAG_CONTROL: 0xC2,
            P.Command.RESTORE_MCU: 0xC3,
            P.Command.UPDATE_FIRMWARE: 0xD0,
        }
        for command, opcode in expected.items():
            self.assertEqual(int(command), opcode)
            self.assertEqual(P.COMMANDS[command].opcode, opcode)

    def test_opcodes_are_unique(self):
        opcodes = [i.opcode for i in P.COMMANDS.values()]
        self.assertEqual(len(opcodes), len(set(opcodes)))

    def test_answer_min_lengths_are_documented(self):
        self.assertEqual(
            P.COMMANDS[P.Command.QUERY_SENSOR_INFO].answer_min, 9)
        self.assertEqual(
            P.COMMANDS[P.Command.QUERY_FIRMWARE_VER].answer_min, 7)
        self.assertEqual(
            P.COMMANDS[P.Command.QUERY_ROOT_KEY].answer_min, 0x71)
        self.assertEqual(
            P.COMMANDS[P.Command.GET_IMG_DATA].answer_min, 0x18)

    def test_never_send_last_opcode(self):
        # 0xFF is not in the driver's table; it stays classified destructive.
        self.assertIn(P.Command.FW_WORK_MODE_SWITCH, P.DESTRUCTIVE_COMMANDS)


if __name__ == "__main__":
    unittest.main()
