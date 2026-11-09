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




# ---------------------------------------------------------------------------
# --try-write-shape: planner, validator and runner
# ---------------------------------------------------------------------------

class ShapeFakeSession:
    """A fake write-shape transport: canned bytes per cell index."""

    def __init__(self, cell, answers, closed):
        self.cell = cell
        self.answers = answers
        self.closed = closed

    def read(self, opcode, read_len, *, selector_variant="read", padding=b"",
             timeout_ms=None):
        value = self.answers.get(self.cell.index)
        if value is None:
            value = b"\x00" * read_len
        if callable(value):
            return value(opcode, read_len, selector_variant, padding,
                         timeout_ms)
        return matrix.ReadResult(bytes(value), irq="fired")

    def close(self):
        self.closed.append(self.cell.index)


class TestWriteShapePlanner(unittest.TestCase):
    def test_default_matrix_is_60_cells_in_documented_order(self):
        cells = matrix.plan_write_shape()
        self.assertEqual(len(cells), 60)
        self.assertEqual([c.index for c in cells], list(range(1, 61)))
        first = cells[0]
        self.assertEqual((first.query.opcode, first.selector,
                          first.padding_label, first.read_len),
                         (0xA0, 0x40, "0", 7))
        self.assertEqual(cells[1].read_len, 64)
        self.assertEqual(cells[12].selector, 0x41)       # bit 0 set
        self.assertEqual(cells[12].padding_label, "0")
        self.assertEqual(cells[11].padding_label, "phase")
        self.assertEqual(cells[-1].query.opcode, 0xC1)
        self.assertEqual(cells[-1].selector, 0x03)
        self.assertEqual(cells[-1].read_len, 64)
        # every query appears with both selector variants
        self.assertEqual({c.query.opcode for c in cells}, {0xA0, 0xA1, 0xC1})
        self.assertEqual({c.selector_variant for c in cells},
                         {"read", "read-bit0"})
        self.assertEqual({c.padding_label for c in cells},
                         {"0", "1", "2", "3", "4", "phase"})
        # the phase padding is the phase byte preceded by two zeros
        phase = [c for c in cells if c.padding_label == "phase"][0]
        self.assertEqual(phase.padding, b"\x00\x00\x00")

    def test_phase_padding_carries_the_command_phase(self):
        query = matrix.MatrixQuery("fw", 0xA0, 7, 64, "x", phase=0x05)
        cells = matrix.plan_write_shape(queries=(query,))
        phase = [c for c in cells if c.padding_label == "phase"][0]
        self.assertEqual(phase.padding, b"\x00\x00\x05")

    def test_planner_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            matrix.plan_write_shape(selectors=())
        with self.assertRaises(ValueError):
            matrix.plan_write_shape(paddings=())
        with self.assertRaises(ValueError):
            matrix.plan_write_shape(selectors=(("bogus", 0),))
        with self.assertRaises(matrix.MatrixRefused):
            matrix.plan_write_shape(queries=(
                matrix.MatrixQuery("enrol", 0xB0, 7, 64),))
        with self.assertRaises(matrix.MatrixRefused):
            matrix.plan_write_shape(queries=(
                matrix.MatrixQuery("jtag-alias", 0x02, 7, 64),))

    def test_experiment_is_limited_to_a0_a1_c1(self):
        query = matrix.MatrixQuery("uid", 0xC0, 16, 64)
        with self.assertRaises(matrix.MatrixRefused):
            matrix.plan_write_shape(queries=(query,))


class TestWriteShapeValidator(unittest.TestCase):
    def test_valid_aa_frame_at_offset_zero(self):
        frame = protocol.build_frame(0xA0, 0, 0, VALID_FW)
        check = matrix.analyze_shape_answer(0xA0, frame)
        self.assertTrue(check.valid)
        self.assertEqual((check.framing, check.aa_offset, check.crc),
                         ("frame", 0, "ok"))
        self.assertEqual(check.payload, VALID_FW)

    def test_valid_aa_frame_at_a_non_zero_offset(self):
        frame = protocol.build_frame(0xA1, 0, 0, VALID_SCHEME)
        check = matrix.analyze_shape_answer(0xA1, b"\x00\x00" + frame)
        self.assertTrue(check.valid)
        self.assertEqual(check.aa_offset, 2)
        self.assertEqual(check.crc, "ok")

    def test_valid_bare_fw_layout_at_offset_zero(self):
        check = matrix.analyze_shape_answer(0xA0, VALID_FW + b"\x00" * 57)
        self.assertTrue(check.valid)
        self.assertEqual((check.framing, check.candidate_offset), ("bare", 0))
        self.assertEqual(check.fields["project"], 2)

    def test_valid_bare_layout_at_a_non_zero_offset_is_shifted(self):
        data = b"\x00\x00" + VALID_FW + b"\x00" * 55
        check = matrix.analyze_shape_answer(0xA0, data)
        self.assertFalse(check.valid)
        self.assertEqual(check.verdict, matrix.SHIFTED)
        self.assertEqual(check.candidate_offset, 2)
        self.assertEqual(check.candidate_offsets[0], 2)

    def test_valid_sensor_info_layout(self):
        check = matrix.analyze_shape_answer(0xC1, VALID_INFO + b"\x00" * 55)
        self.assertTrue(check.valid)
        self.assertEqual((check.fields["width"], check.fields["height"]),
                         (0x40, 0x40))

    def test_observed_shifted_scheme_pattern_reports_an_offset(self):
        # The 2026-11-09 hardware answer: 00 00 ff 7f 50 03 ...
        data = bytes.fromhex("00 00 ff 7f 50 03 00 00") + b"\x00" * 56
        check = matrix.analyze_shape_answer(0xA1, data)
        self.assertFalse(check.valid)
        self.assertEqual(check.verdict, matrix.SHIFTED)
        self.assertIn(4, check.candidate_offsets)

    def test_zeros_ff_short_and_empty_are_not_valid(self):
        for payload, verdict in (
                (b"\x00" * 64, matrix.ZEROS),
                (b"\xff" * 64, matrix.ALL_FF),
                (b"\x01\x02", matrix.SHORT),
                (b"", matrix.EMPTY)):
            with self.subTest(verdict=verdict):
                check = matrix.analyze_shape_answer(0xA0, payload)
                self.assertEqual(check.verdict, verdict)
                self.assertFalse(check.valid)

    def test_crc_mismatch_is_its_own_verdict(self):
        frame = bytearray(protocol.build_frame(0xA0, 0, 0, VALID_FW))
        frame[-1] ^= 0xFF
        check = matrix.analyze_shape_answer(0xA0, bytes(frame))
        self.assertEqual(check.verdict, matrix.CRC_MISMATCH)
        self.assertEqual(check.crc, "crc")
        self.assertFalse(check.valid)

    def test_allowlist_refusal(self):
        with self.assertRaises(matrix.MatrixRefused):
            matrix.analyze_shape_answer(0xB0, b"\x00" * 64)
        with self.assertRaises(matrix.MatrixRefused):
            matrix.analyze_shape_answer(0xC0, b"\x00" * 64)


class TestWriteShapeRunner(unittest.TestCase):
    def run_fake(self, answers, cells=None):
        cells = tuple(cells or matrix.plan_write_shape())
        closed = []

        def connect(cell):
            return ShapeFakeSession(cell, answers, closed)

        report = matrix.run_write_shape(cells, connect=connect)
        return report, closed

    def test_all_cells_failing_still_produce_a_full_table(self):
        report, closed = self.run_fake({})
        self.assertEqual(report.outcome, "no-valid-answer")
        self.assertEqual(report.attempted if hasattr(report, "attempted")
                         else len(report.attempts), 60)
        self.assertEqual(len(report.cells), 60)
        self.assertEqual(report.winning, None)
        self.assertEqual(len(closed), 60)
        table = report.render_table()
        self.assertEqual(len(table), 61)
        self.assertIn("cand", table[0])
        data = report.to_dict()
        self.assertEqual((data["planned"], data["attempted"]), (60, 60))
        self.assertIsNone(data["winning"])
        self.assertEqual(len(data["shift_summary"]), 10)  # 3 ops x 2 selectors x lens

    def test_stops_at_the_first_valid_cell(self):
        cells = matrix.plan_write_shape()
        answers = {3: VALID_FW}
        report, closed = self.run_fake(answers, cells)
        self.assertEqual(report.outcome, "valid-answer")
        self.assertIsNotNone(report.winning)
        self.assertEqual(report.winning.cell.index, 3)
        self.assertEqual(len(report.attempts), 3)
        self.assertEqual(closed, [1, 2, 3])
        self.assertIn("VALID write shape at cell 3/60", report.verdict)

    def test_shifted_only_outcome_reports_the_offsets(self):
        cells = matrix.plan_write_shape(
            queries=(matrix.MatrixQuery("scheme", 0xA1, 64, 64),))
        shifted = bytes.fromhex("00 00 ff 7f 50 03 00 00") + b"\x00" * 56
        report, _closed = self.run_fake(
            {index: shifted for index in range(1, len(cells) + 1)}, cells)
        self.assertEqual(report.outcome, "shifted-only")
        self.assertIn("candidate offsets by padding", report.verdict)
        self.assertTrue(all(a.check.candidate_offset is not None
                            for a in report.attempts))
        row = report.to_dict()["cells"][0]
        self.assertEqual(row["candidate_offsets"][0], 4)
        self.assertFalse(row["valid"])

    def test_json_shape_is_stable_and_complete(self):
        import json
        report, _closed = self.run_fake({})
        text = json.dumps(report.to_dict(), sort_keys=True)
        data = json.loads(text)
        self.assertEqual(data["schema"], matrix.WRITE_SHAPE_SCHEMA)
        self.assertEqual(data["tool"], "oxp3-fp-probe --try-write-shape")
        first = data["cells"][0]
        for key in ("selector", "padding", "read_len", "aa_offset", "crc",
                    "candidate_offset", "verdict", "valid"):
            self.assertIn(key, first)


class TestProbeWriteShapeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = support.load_probe()

    def run_probe(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.probe.main(argv)
        return rc, out.getvalue()

    def test_probe_write_shape_dry_run_plans_60_cells_and_does_no_io(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "write-shape.json")
            rc, text = self.run_probe([
                "--try-write-shape", "--dry-run", "--spidev", "/dev/spidev0.0",
                "--gpio", "18", "--gpio-chip", "gpiochip1",
                "--reset-gpio", "16", "--allow-reset", "--allow-writes",
                "--reset-polarity", "active-high", "--settle-ms", "10",
                "--irq-edge", "rising", "--write-shape-json", path])
            self.assertEqual(rc, 0)
            self.assertIn("write-shape: 60 cells", text)
            self.assertIn("selector=0x41", text)
            self.assertIn("padding=phase", text)
            self.assertFalse(os.path.exists(path),
                             "a dry run must not write the write-shape JSON")

    def test_probe_refuses_command_names_in_write_shape_mode(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main(["--try-write-shape", "fw"])
        self.assertEqual(rc, 2)

    def test_probe_refuses_write_shape_without_allow_reset(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main([
                "--try-write-shape", "--spidev", "/dev/spidev0.0",
                "--gpio", "18", "--reset-gpio", "16", "--allow-writes"])
        self.assertEqual(rc, 2)
        self.assertIn("--allow-reset", out.getvalue())

    def test_probe_write_shape_writes_the_table_even_when_every_cell_fails(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            spidev = os.path.join(tmp, "spidev0.0")
            open(spidev, "w").close()
            path = os.path.join(tmp, "write-shape.json")
            rc, text = self.run_probe([
                "--try-write-shape", "--spidev", spidev,
                "--gpio", "18", "--gpio-chip", os.path.join(tmp, "gpiochip1"),
                "--reset-gpio", "16", "--allow-reset", "--allow-writes",
                "--reset-polarity", "active-high", "--settle-ms", "10",
                "--irq-edge", "rising", "--write-shape-json", path])
            self.assertEqual(rc, 1)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual((data["planned"], data["attempted"]), (60, 60))
            self.assertEqual(data["outcome"], "no-valid-answer")
            self.assertIn("write-shape", text)


if __name__ == "__main__":
    unittest.main()
