"""The standalone sudo probe and the library must not drift apart.

``oxp3-fp-probe.py`` is deliberately kept as a single self-contained file (it
runs under ``sudo`` straight from the checkout), so it carries its own copy of
the frame builder.  These tests pin the two implementations to each other.
"""

import unittest

import support
from fpbf63112 import protocol as P


class TestProbeParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = support.load_probe()

    def test_build_frame_is_identical(self):
        cases = [
            (0xA1, 0, 0, b""),
            (0xC2, 0, 0x1234, b""),
            (0xB5, 1, 0x0001, bytes(range(7))),
            (0xB1, 2, 0xFFFF, bytes(range(32))),
            (0xB5, 1, 0, bytes(range(255))),
        ]
        for opcode, phase, arg, data in cases:
            self.assertEqual(self.probe.build_frame(opcode, phase, arg, data),
                             P.build_frame(opcode, phase, arg, data),
                             f"op=0x{opcode:02x} len={len(data)}")

    def test_parse_response_agrees_on_the_header(self):
        raw = P.build_frame(0xC2, 0, 0, b"\x01\x02\x03\x04")
        hdr, body = self.probe.parse_response(raw)
        frame = P.parse_frame(raw)
        self.assertEqual(hdr["len"], frame.length)
        self.assertEqual(hdr["magic"], frame.magic)
        self.assertEqual(hdr["op"], frame.opcode)
        self.assertEqual(hdr["phase"], frame.phase)
        self.assertEqual(hdr["status"], frame.status)
        # the probe's body view starts after the 4-byte command block and,
        # unlike Frame.body_from_arg, still carries the CRC trailer; it only
        # prints the first bytes, so this is a documented difference, not a
        # protocol disagreement.
        self.assertEqual(body, raw[25:])
        self.assertTrue(body.endswith(raw[-4:]))
        self.assertEqual(body[:len(frame.body_from_arg)],
                         frame.body_from_arg)

    def test_parse_response_rejects_non_frames(self):
        # empty / non-frame reads: no header, and the raw read is returned so
        # the caller can hexdump it
        for blob in (b"", b"\x00" * 32):
            hdr, body = self.probe.parse_response(blob)
            self.assertIsNone(hdr)
            self.assertEqual(body, blob)
        # a read that starts with 0xAA but is too short gets a length probe and
        # no body; the probe must not pretend it parsed a frame
        for blob in (b"\xAA", b"\xAA" + b"\x00" * 10):
            hdr, body = self.probe.parse_response(blob)
            self.assertEqual(body, b"")
            self.assertIsNotNone(hdr)
            self.assertIsNone(hdr.get("magic"))

    def test_command_table_matches(self):
        library_ops = {info.name: info.opcode for info in P.COMMANDS.values()}
        probe_ops = {name: opcode for name, (opcode, _) in self.probe.OPS.items()}
        self.assertEqual(len(probe_ops), 5)
        for short, opcode in probe_ops.items():
            self.assertIn(opcode, [i.opcode for i in P.COMMANDS.values()])
            self.assertEqual(library_ops[self.probe.OPS[short][1]], opcode)

    def test_destructive_set_matches(self):
        # The probe additionally refuses identify (0xB1) and the undocumented
        # 0xD1 mode switch, which the protocol table classifies as
        # read-only/unknown rather than template-destroying.
        self.assertTrue({int(c) for c in P.DESTRUCTIVE_COMMANDS}
                        <= set(self.probe.DESTRUCTIVE))
        self.assertEqual(self.probe.DESTRUCTIVE, {
            0xB0: "ENROLL",
            0xB1: "IDENTIFY",
            0xB2: "REMOVE",
            0xC2: "JTAG",
            0xC3: "RESTORE_MCU",
            0xD0: "UPDATE_FIRMWARE",
            0xD1: "FW_WORK_MODE_SWITCH",
            0xFF: "FW_WORK_MODE_SWITCH",
        })

    def test_probe_self_check_entrypoint_exists(self):
        self.assertTrue(callable(self.probe.query))
        self.assertTrue(callable(self.probe.show_info))
        self.assertTrue(callable(self.probe.show_fw))


if __name__ == "__main__":
    unittest.main()


class TestProbeSessionPaths(unittest.TestCase):
    """The probe's two new session entry points (``--crypto-selftest``,
    ``--session``), driven offline against the synthetic device."""

    @classmethod
    def setUpClass(cls):
        cls.probe = support.load_probe()

    def test_crypto_selftest_passes(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.probe.crypto_selftest()
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("all published vectors passed", text)
        self.assertIn("SM3", text)
        self.assertIn("ms", text)

    @staticmethod
    def _args(**kw):
        import argparse

        base = dict(session_rx=256, wait_ms=0, retries=4,
                    raw_info=False, session_secure=False, session_timeout=200)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_transport_reassembles_a_frame(self):
        import support as S

        frame = S.P.build_frame(0xA2, 0, 0, b"\x00" * 0xA1)
        half = len(frame) // 2

        class FakeSpi:
            def __init__(self):
                self.reads = 0

            def transfer(self, tx, rx_len=None):
                if rx_len is None:
                    return b""
                self.reads += 1
                if self.reads == 1:
                    return b"\xff\x00" + frame[:half]
                if self.reads == 2:
                    return frame[half:]
                return b"\x00" * (rx_len or 0)

        transport = self.probe.SpiTransport(FakeSpi(), rx_len=256, wait_ms=0,
                                            retries=4)
        self.assertEqual(transport.exchange(frame), frame)

    def test_session_fails_safe_on_junk(self):
        import contextlib
        import io

        class JunkSpi:
            def transfer(self, tx, rx_len=None):
                return b"\xff" * (rx_len or 64) if rx_len else b""

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.probe.session_attempt(JunkSpi(), self._args())
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", buf.getvalue())
        self.assertIn("nothing on the module was changed", buf.getvalue())

    def test_session_succeeds_against_the_synthetic_device(self):
        import contextlib
        import io

        device = support.FakeRootKeyDevice(session_key=b"\xc3" * 16)

        class DeviceSpi:
            def transfer(self, tx, rx_len=None):
                if rx_len is None:
                    self.pending = device.exchange(bytes(tx))
                    return b""
                out = getattr(self, "pending", b"")
                self.pending = b""
                return out

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.probe.session_attempt(DeviceSpi(), self._args())
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("established", text)
        self.assertIn("fingerprint", text)
        # the key itself must never be printed
        self.assertNotIn((b"\xc3" * 16).hex(), text)

    def test_session_reports_a_tag_mismatch_and_offers_no_key(self):
        import contextlib
        import io

        device = support.FakeRootKeyDevice(tamper_tag=True)

        class DeviceSpi:
            def transfer(self, tx, rx_len=None):
                if rx_len is None:
                    self.pending = device.exchange(bytes(tx))
                    return b""
                out = getattr(self, "pending", b"")
                self.pending = b""
                return out

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.probe.session_attempt(DeviceSpi(), self._args())
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", buf.getvalue())
