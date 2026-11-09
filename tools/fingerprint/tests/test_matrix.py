"""Offline tests for the ``--try-matrix`` planner, parser and runner.

No test here opens ``/dev/spidev*`` or a real ``/dev/gpiochip*``: the runner is
driven through its injected ``connect(cell)`` factory, and the probe test uses
``--dry-run`` (which the probe documents as performing no I/O).
"""

import contextlib
import io
import unittest

import support
from fpbf63112 import matrix, protocol, spi


VALID_FW = bytes([0x00, 0x02, 0x00, 0x01, 0x02, 0x03, 0x04])
VALID_INFO = bytes([0, 0, 0, 0, 0x40, 0, 0x40, 0, 0])
VALID_SCHEME = bytes([0x04, 0x00, 0x01, 0x02] + [0] * 60)


def read_result(payload, error="", irq="none"):
    return matrix.ReadResult(bytes(payload), error, irq)


class FakeSession:
    """A fake transport: returns canned bytes per (cell index, opcode)."""

    def __init__(self, cell, answers, closed):
        self.cell = cell
        self.answers = answers
        self.closed = closed

    def read(self, opcode, read_len):
        value = self.answers.get((self.cell.index, opcode))
        if value is None:
            value = b"\x00" * read_len
        if callable(value):
            return value(opcode, read_len)
        return read_result(bytes(value), irq="fired")

    def close(self):
        self.closed.append(self.cell.index)


def run_fake(answers, cells=None, closed=None):
    cells = tuple(cells or matrix.plan_matrix())
    closed = closed if closed is not None else []

    def connect(cell):
        return FakeSession(cell, answers, closed)

    return matrix.run_matrix(cells, connect=connect), closed


class TestPlanner(unittest.TestCase):
    def test_default_matrix_is_18_cells_in_documented_order(self):
        cells = matrix.plan_matrix()
        self.assertEqual(len(cells), 18)
        self.assertEqual([c.index for c in cells], list(range(1, 19)))
        self.assertEqual(cells[0].polarity, "active-low")
        self.assertEqual(cells[0].settle_ms, 10)
        self.assertEqual(cells[0].edge, "rising")
        self.assertTrue(cells[0].pulse)
        self.assertEqual((cells[3].settle_ms, cells[3].edge), (150, "rising"))
        self.assertEqual(cells[6].polarity, "active-high")
        self.assertEqual(cells[12].polarity, "hold")
        self.assertFalse(cells[12].pulse)
        self.assertIsNone(cells[12].active_low)
        # 3 x 2 x 3
        self.assertEqual({c.polarity for c in cells},
                         {"active-low", "active-high", "hold"})
        self.assertEqual({c.settle_ms for c in cells}, {10, 150})
        self.assertEqual({c.edge for c in cells}, {"rising", "falling", "both"})

    def test_planner_rejects_bad_dimensions(self):
        with self.assertRaises(ValueError):
            matrix.plan_matrix(polarities=())
        with self.assertRaises(ValueError):
            matrix.plan_matrix(settles_ms=(-1,))
        with self.assertRaises(ValueError):
            matrix.plan_matrix(edges=("sideways",))


class TestAnswerParser(unittest.TestCase):
    def test_all_zero_is_not_a_valid_answer(self):
        check = matrix.parse_valid_answer(0xA0, b"\x00" * 64)
        self.assertEqual(check.verdict, matrix.ZEROS)
        self.assertFalse(check.valid)

    def test_all_ff_is_not_a_valid_answer(self):
        check = matrix.parse_valid_answer(0xA0, b"\xff" * 64)
        self.assertEqual(check.verdict, matrix.ALL_FF)
        self.assertFalse(check.valid)

    def test_short_answer_is_not_a_valid_answer(self):
        check = matrix.parse_valid_answer(0xA0, b"\x00\x01")
        self.assertEqual(check.verdict, matrix.SHORT)
        self.assertFalse(check.valid)

    def test_bare_fw_answer_parses(self):
        check = matrix.parse_valid_answer(0xA0, VALID_FW + b"\x00" * 57)
        self.assertTrue(check.valid, check.detail)
        self.assertEqual(check.framing, "bare")
        self.assertEqual(check.fields["version"], "1.2.3.4")
        self.assertEqual(check.fields["project_name"], "HUAHONG MOUSE MOC")

    def test_unknown_project_is_malformed(self):
        bad = bytes([0x00, 0x99, 0x00, 0x01, 0x02, 0x03, 0x04]) + b"\x00" * 57
        check = matrix.parse_valid_answer(0xA0, bad)
        self.assertEqual(check.verdict, matrix.MALFORMED)
        self.assertFalse(check.valid)

    def test_framed_answer_requires_a_matching_crc(self):
        good = protocol.build_frame(0xA0, data=VALID_FW)
        check = matrix.parse_valid_answer(0xA0, good + b"\x00" * 20)
        self.assertTrue(check.valid, check.detail)
        self.assertEqual(check.framing, "frame")
        self.assertIn("crc=", check.detail)

        corrupt = bytearray(good)
        corrupt[-1] ^= 0xFF
        check = matrix.parse_valid_answer(0xA0, bytes(corrupt))
        self.assertEqual(check.verdict, matrix.MALFORMED)
        self.assertIn("CRC", check.detail)
        self.assertFalse(check.valid)

    def test_frame_opcode_must_match(self):
        framed = protocol.build_frame(0xC1, data=VALID_INFO)
        check = matrix.parse_valid_answer(0xC1, framed)
        self.assertTrue(check.valid, check.detail)
        wrong = protocol.build_frame(0xA1, data=VALID_SCHEME)
        check = matrix.parse_valid_answer(0xC1, wrong)
        self.assertEqual(check.verdict, matrix.MALFORMED)

    def test_info_geometry_is_checked(self):
        self.assertTrue(matrix.parse_valid_answer(0xC1, VALID_INFO).valid)
        bad = bytearray(VALID_INFO)
        bad[4:6] = (0).to_bytes(2, "little")
        check = matrix.parse_valid_answer(0xC1, bytes(bad))
        self.assertEqual(check.verdict, matrix.MALFORMED)

    def test_scheme_needs_a_plausible_nonzero_table(self):
        self.assertTrue(matrix.parse_valid_answer(0xA1, VALID_SCHEME).valid)
        # fewer bytes than the 64-byte scheme layout is a short read
        check = matrix.parse_valid_answer(0xA1, b"\x04\x00")
        self.assertEqual(check.verdict, matrix.SHORT)

    def test_parser_refuses_an_opcode_outside_the_matrix(self):
        with self.assertRaises(matrix.MatrixRefused):
            matrix.parse_valid_answer(0xB0, VALID_FW)
        with self.assertRaises(matrix.MatrixRefused):
            matrix.parse_valid_answer(0xC2, VALID_FW)


class TestRunner(unittest.TestCase):
    def test_first_attempt_success_stops_immediately(self):
        report, closed = run_fake({(1, 0xA0): VALID_FW})
        self.assertEqual(len(report.attempts), 1)
        self.assertIsNotNone(report.winning)
        self.assertEqual(report.winning.cell.index, 1)
        self.assertEqual(report.outcome, "valid-answer")
        self.assertEqual(closed, [1])

    def test_success_on_a_later_combination(self):
        # cell 5 = active-low / 150 ms / falling
        report, closed = run_fake({(5, 0xA0): VALID_FW})
        self.assertEqual(len(report.attempts), 5)
        self.assertIsNotNone(report.winning)
        self.assertEqual(report.winning.cell.index, 5)
        self.assertEqual(report.winning.cell.settle_ms, 150)
        self.assertEqual(report.winning.cell.edge, "falling")
        self.assertEqual(closed, [1, 2, 3, 4, 5])

    def test_all_zeros_reports_the_all_attempts_verdict(self):
        report, closed = run_fake({})
        self.assertEqual(len(report.attempts), 18)
        self.assertEqual(len(closed), 18)
        self.assertIsNone(report.winning)
        self.assertEqual(report.outcome, "no-valid-answer")
        self.assertIn("all 18 attempts returned zeros/timeouts",
                      report.verdict)
        self.assertIn("tried polarity active-low/active-high/hold", report.verdict)

    def test_all_timeouts_reports_timeouts(self):
        def timeout(opcode, read_len):
            return matrix.ReadResult(
                b"", "timeout: no interrupt on GPIO 18 within 1500 ms", "none")

        report, _closed = run_fake({(i, 0xA0): timeout for i in range(1, 19)})
        self.assertEqual(len(report.attempts), 18)
        self.assertIsNone(report.winning)
        self.assertIn("18 timeout", report.verdict)
        self.assertIn("zeros/timeouts", report.verdict)

    def test_malformed_short_answer_is_not_success(self):
        report, _closed = run_fake({(i, 0xA0): b"\x00\x01"
                                    for i in range(1, 19)})
        self.assertIsNone(report.winning)
        self.assertTrue(all(a.verdict == matrix.SHORT
                            for a in report.attempts))
        self.assertIn("short", report.verdict)

    def test_nonzero_but_unparsed_stops_and_is_reported(self):
        bad = bytes([0x00, 0x99, 0x00, 0x01, 0x02, 0x03, 0x04])
        report, closed = run_fake({(1, 0xA0): bad})
        self.assertIsNone(report.winning)
        self.assertEqual(report.attempts[0].verdict, matrix.MALFORMED)
        self.assertEqual(len(report.attempts), 1)  # no repeat per polarity
        self.assertEqual(closed, [1])
        self.assertIn("malformed", report.verdict)

    def test_nonzero_fw_escalates_to_scheme_and_info(self):
        # A non-zero 0xA0 must not stop escalation; a valid 0xC1 wins.
        report, _closed = run_fake({
            (1, 0xA0): b"\x00\x99\x00\x01\x02\x03\x04",
            (1, 0xA1): b"\x00" * 64,
            (1, 0xC1): VALID_INFO,
        })
        self.assertIsNotNone(report.winning)
        self.assertEqual(report.winning.primary.query.name, "fw")
        winning_queries = report.winning.queries
        self.assertEqual([q.query.name for q in winning_queries],
                         ["fw", "scheme", "info"])
        self.assertEqual(winning_queries[-1].check.verdict, matrix.VALID)

    def test_timeout_with_bytes_still_parses(self):
        def timeout_with_bytes(opcode, read_len):
            return matrix.ReadResult(
                VALID_FW + b"\x00" * (read_len - len(VALID_FW)),
                "timeout: no interrupt", "none")

        report, _closed = run_fake({(1, 0xA0): timeout_with_bytes})
        self.assertIsNotNone(report.winning,
                             "a parsed answer without an IRQ is still success")

    def test_allowlist_refusal(self):
        cells = matrix.plan_matrix()[:1]
        with self.assertRaises(matrix.MatrixRefused):
            matrix.run_matrix(
                cells, connect=lambda cell: FakeSession(cell, {}, []),
                queries=(matrix.MatrixQuery("enrol", 0xB0, 4, 4),))
        with self.assertRaises(matrix.MatrixRefused):
            matrix.run_matrix(
                cells, connect=lambda cell: FakeSession(cell, {}, []),
                queries=(matrix.MatrixQuery("jtag", 0xC2, 4, 4),))
        # even a read opcode outside {0xA0,0xA1,0xC1} is refused
        with self.assertRaises(matrix.MatrixRefused):
            matrix.run_matrix(
                cells, connect=lambda cell: FakeSession(cell, {}, []),
                queries=(matrix.MatrixQuery("count", 0xB3, 4, 4),))

    def test_report_json_is_serialisable_and_complete(self):
        report, _closed = run_fake({(1, 0xA0): VALID_FW})
        payload = report.to_dict()
        import json
        text = json.dumps(payload, sort_keys=True)
        self.assertIn("valid-answer", text)
        self.assertEqual(payload["schema"], matrix.MATRIX_SCHEMA)
        self.assertEqual(payload["winning"]["index"], 1)
        self.assertEqual(payload["attempts"][0]["queries"][0]["verdict"],
                         matrix.VALID)


class TestSpiMatrixSession(unittest.TestCase):
    """Only construction/gating is exercised; no device is opened."""

    def test_hold_cell_never_needs_a_reset_pin(self):
        cell = [c for c in matrix.plan_matrix() if c.polarity == "hold"][0]
        session = matrix.SpiMatrixSession(
            cell, spidev_path="/nonexistent/spidev0.0", irq_pin=18,
            gpio_chip="/nonexistent/gpiochip1", reset_pin=None)
        # read() catches the open failure and returns it as data, never raising
        result = session.read(0xA0, 7)
        self.assertTrue(result.error.startswith("error:"))
        session.close()

    def test_pulse_cell_without_a_reset_pin_fails_closed(self):
        cell = matrix.plan_matrix()[0]
        session = matrix.SpiMatrixSession(
            cell, spidev_path="/nonexistent/spidev0.0", irq_pin=18,
            gpio_chip="/nonexistent/gpiochip1", reset_pin=None)
        result = session.read(0xA0, 7)
        self.assertIn("reset pin", result.error)
        session.close()


class TestProbeMatrixIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = support.load_probe()

    def run_probe(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.probe.main(argv)
        return rc, out.getvalue()

    def test_probe_matrix_dry_run_prints_the_matrix_and_does_no_io(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "matrix.json")
            rc, text = self.run_probe([
                "--try-matrix", "--dry-run", "--spidev", "/dev/spidev0.0",
                "--gpio", "18", "--gpio-chip", "gpiochip1",
                "--reset-gpio", "16", "--allow-reset", "--allow-writes",
                "--matrix-json", path])
            self.assertEqual(rc, 0)
            self.assertIn("matrix: 18 cells", text)
            self.assertIn("active-low", text)
            self.assertIn("edge=rising", text)
            self.assertFalse(os.path.exists(path),
                             "a dry run must not write the matrix JSON")

    def test_probe_refuses_command_names_in_matrix_mode(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main(["--try-matrix", "fw"])
        self.assertEqual(rc, 2)

    def test_probe_refuses_matrix_without_allow_reset(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main([
                "--try-matrix", "--spidev", "/dev/spidev0.0", "--gpio", "18",
                "--reset-gpio", "16", "--allow-writes"])
        self.assertEqual(rc, 2)
        self.assertIn("--allow-reset", out.getvalue())


if __name__ == "__main__":
    unittest.main()
