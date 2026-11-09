"""Transaction / retry state machine, independent of any transport."""

import random
import unittest

import support
from fpbf63112 import protocol as P


def response_to(tx: P.Transaction, data: bytes = b"\x00") -> P.Frame:
    """A plausible answer frame for *tx* (same opcode and phase)."""
    raw = P.build_frame(tx.opcode, tx.phase, 0, data)
    return P.parse_frame(raw)


class TestTransaction(unittest.TestCase):
    def test_start_builds_request(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO))
        sent = tx.start()
        self.assertEqual(tx.state, P.TransactionState.AWAITING_ANSWER)
        self.assertEqual(tx.attempts, 1)
        self.assertEqual(sent, P.build_frame(0xC2, 0, 0, b""))
        self.assertEqual(P.parse_frame(sent).opcode, 0xC2)

    def test_start_twice_fails(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO))
        tx.start()
        with self.assertRaises(P.SessionError):
            tx.start()

    def test_resend_until_exhausted(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO),
                           policy=P.RetryPolicy(max_attempts=3))
        tx.start()
        self.assertIsNotNone(tx.handle_timeout())
        self.assertEqual(tx.attempts, 2)
        self.assertIsNotNone(tx.handle_timeout())
        self.assertEqual(tx.attempts, 3)
        self.assertIsNone(tx.handle_timeout())
        self.assertEqual(tx.state, P.TransactionState.FAILED)
        self.assertIsNotNone(tx.last_error)
        self.assertTrue(tx.done)

    def test_resend_disabled(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO),
                           policy=P.RetryPolicy(max_attempts=5,
                                                resend_on_timeout=False))
        tx.start()
        self.assertIsNone(tx.handle_timeout())
        self.assertEqual(tx.state, P.TransactionState.FAILED)
        self.assertEqual(tx.attempts, 1)

    def test_handle_error_counts_attempt(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO))
        tx.start()
        tx.handle_error(P.CRCMismatchError(1, 2))
        self.assertEqual(tx.state, P.TransactionState.RESEND)
        self.assertEqual(tx.attempts, 2)
        tx.handle_error(P.TruncatedFrameError("short"))
        tx.handle_error(P.TruncatedFrameError("short"))
        self.assertEqual(tx.state, P.TransactionState.FAILED)
        self.assertEqual(tx.attempts, 3)

    def test_accepts_matching_answer(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO))
        tx.start()
        frame = response_to(tx, b"\x01\x02\x03")
        self.assertIs(tx.handle_frame(frame), frame)
        self.assertEqual(tx.state, P.TransactionState.SUCCESS)
        self.assertEqual(tx.response.data, b"\x01\x02\x03")

    def test_rejects_wrong_magic(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO))
        tx.start()
        raw = bytearray(P.build_frame(0xC2))
        raw[21] = 0x00
        import struct
        struct.pack_into("<I", raw, len(raw) - 4, P.crc32(bytes(raw[:-4])))
        frame = P.parse_frame(bytes(raw), require_magic=False)
        with self.assertRaises(P.ResponseMismatchError):
            tx.handle_frame(frame)
        self.assertEqual(tx.state, P.TransactionState.AWAITING_ANSWER)

    def test_rejects_wrong_opcode(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO))
        tx.start()
        with self.assertRaises(P.ResponseMismatchError):
            tx.handle_frame(P.parse_frame(P.build_frame(0xC1)))

    def test_phase_check_is_optional(self):
        tx = P.Transaction(int(P.Command.GET_IMG_DATA), phase=1)
        tx.start()
        # answers may not echo the phase; default is lenient
        tx.handle_frame(P.parse_frame(P.build_frame(0xB5, 0)))
        self.assertEqual(tx.state, P.TransactionState.SUCCESS)

        tx = P.Transaction(int(P.Command.GET_IMG_DATA), phase=1, check_phase=True)
        tx.start()
        with self.assertRaises(P.ResponseMismatchError):
            tx.handle_frame(P.parse_frame(P.build_frame(0xB5, 0)))

    def test_frame_in_wrong_state(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO))
        with self.assertRaises(P.SessionError):
            tx.handle_frame(P.parse_frame(P.build_frame(0xC2)))
        tx.start()
        tx.handle_frame(response_to(tx))
        with self.assertRaises(P.SessionError):
            tx.handle_timeout()

    def test_clone_resets_state(self):
        tx = P.Transaction(int(P.Command.QUERY_SENSOR_INFO), arg=7,
                           data=b"\x01", policy=P.RetryPolicy(max_attempts=2))
        tx.start()
        tx.handle_frame(response_to(tx))
        clone = tx.clone()
        self.assertEqual(clone.state, P.TransactionState.READY)
        self.assertEqual(clone.attempts, 0)
        self.assertIsNone(clone.response)
        self.assertEqual(clone.arg, 7)
        self.assertEqual(clone.data, b"\x01")
        self.assertEqual(clone.start(), tx.frame_bytes())

    def test_invariants_over_random_events(self):
        rng = random.Random(63112)
        for _ in range(100):
            policy = P.RetryPolicy(max_attempts=rng.randrange(1, 5))
            tx = P.Transaction(int(P.Command.QUERY_MODULE_UID), policy=policy)
            tx.start()
            for _ in range(rng.randrange(1, 10)):
                if tx.done:
                    break
                event = rng.choice(("timeout", "error", "answer"))
                if event == "timeout":
                    sent = tx.handle_timeout()
                    self.assertEqual(sent is None,
                                     tx.state == P.TransactionState.FAILED)
                elif event == "error":
                    tx.handle_error(P.ProtocolError("synthetic"))
                else:
                    tx.handle_frame(response_to(tx))
                self.assertLessEqual(tx.attempts, policy.max_attempts)
                if tx.state == P.TransactionState.FAILED:
                    self.assertEqual(tx.attempts, policy.max_attempts)
            if tx.state == P.TransactionState.SUCCESS:
                self.assertIsNotNone(tx.response)


class TestCommandTable(unittest.TestCase):
    def test_opcodes_match_recovered_table(self):
        self.assertEqual(int(P.Command.QUERY_FIRMWARE_VER), 0xA1)
        self.assertEqual(int(P.Command.QUERY_MODULE_SCHEME_INFORMATION), 0xA2)
        self.assertEqual(int(P.Command.QUERY_ROOT_KEY), 0xB0)
        self.assertEqual(int(P.Command.ENROLL_FINGERPRINT), 0xB1)
        self.assertEqual(int(P.Command.IDENTIFY_FINGERPRINT), 0xB2)
        self.assertEqual(int(P.Command.REMOVE_FINGERPRINT), 0xB3)
        self.assertEqual(int(P.Command.QUERY_FINGERPRINT_COUNT), 0xB4)
        self.assertEqual(int(P.Command.GET_IMG_DATA), 0xB5)
        self.assertEqual(int(P.Command.DETECT_FINGERPRINT_PRESS), 0xB6)
        self.assertEqual(int(P.Command.SLEEP), 0xC0)
        self.assertEqual(int(P.Command.QUERY_MODULE_UID), 0xC1)
        self.assertEqual(int(P.Command.QUERY_SENSOR_INFO), 0xC2)
        self.assertEqual(int(P.Command.JTAG_CONTROL), 0xC3)
        self.assertEqual(int(P.Command.RESTORE_MCU), 0xD0)
        self.assertEqual(int(P.Command.UPDATE_FIRMWARE), 0xD1)
        self.assertEqual(int(P.Command.FW_WORK_MODE_SWITCH), 0xFF)

    def test_safety_classes(self):
        self.assertEqual(
            P.DESTRUCTIVE_COMMANDS,
            frozenset({P.Command.ENROLL_FINGERPRINT,
                       P.Command.REMOVE_FINGERPRINT,
                       P.Command.JTAG_CONTROL, P.Command.RESTORE_MCU,
                       P.Command.UPDATE_FIRMWARE,
                       P.Command.FW_WORK_MODE_SWITCH}))
        self.assertIn(P.Command.QUERY_SENSOR_INFO, P.READ_ONLY_COMMANDS)
        self.assertIn(P.Command.GET_IMG_DATA, P.READ_ONLY_COMMANDS)
        self.assertNotIn(P.Command.UPDATE_FIRMWARE, P.READ_ONLY_COMMANDS)

    def test_lookup(self):
        self.assertEqual(P.command_name(0xC2), "CMD_QUERY_SENSOR_INFO")
        self.assertIsNone(P.command_info(0x00))
        self.assertEqual(P.command_name(0x00), "op 0x00")
        info = P.COMMANDS[P.Command.GET_IMG_DATA]
        self.assertTrue(info.multi_phase)
        self.assertTrue(info.read_only)

    def test_every_table_entry_marks_a_byte(self):
        for command, info in P.COMMANDS.items():
            self.assertEqual(int(command), info.opcode)
            self.assertTrue(0 <= info.opcode <= 0xFF)
            self.assertTrue(info.name.startswith("CMD_"))
            self.assertTrue(bool(info.summary))


if __name__ == "__main__":
    unittest.main()
