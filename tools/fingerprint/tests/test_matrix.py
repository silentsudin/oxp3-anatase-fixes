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


# ---------------------------------------------------------------------------
# --try-link: link sweep planner, analysis, runner and probe wiring
# ---------------------------------------------------------------------------

class LinkFakeSession:
    """A fake link transport: canned bytes per (mode, speed, settle, opcode)."""

    def __init__(self, cell, answers, closed):
        self.cell = cell
        self.answers = answers
        self.closed = closed

    def read(self, opcode, read_len):
        value = self.answers.get(
            (self.cell.mode, self.cell.speed_hz, self.cell.settle_ms, opcode))
        if value is None:
            value = b"\x00" * read_len
        if callable(value):
            return value(opcode, read_len)
        return matrix.ReadResult(bytes(value), irq="fired")

    def close(self):
        self.closed.append(self.cell.index)


def run_link_fake(answers, cells=None, closed=None, timeout_ms=400):
    cells = tuple(cells or matrix.plan_link())
    closed = closed if closed is not None else []

    def connect(cell):
        return LinkFakeSession(cell, answers, closed)

    return matrix.run_link(cells, connect=connect, timeout_ms=timeout_ms), closed


class TestLinkPlanner(unittest.TestCase):
    def test_default_sweep_is_16_cells_in_documented_order(self):
        cells = matrix.plan_link()
        self.assertEqual(len(cells), 16)
        self.assertEqual([c.index for c in cells], list(range(1, 17)))
        self.assertEqual([c.mode for c in cells[:4]], [0, 1, 2, 3])
        self.assertEqual(cells[0].speed_hz, 1_000_000)
        self.assertEqual(cells[0].settle_ms, 10)
        self.assertEqual(cells[4].speed_hz, 4_000_000)
        self.assertEqual(cells[8].settle_ms, 150)
        self.assertEqual({c.mode for c in cells}, {0, 1, 2, 3})
        self.assertEqual({c.speed_hz for c in cells}, {1_000_000, 4_000_000})
        self.assertEqual({c.settle_ms for c in cells}, {10, 150})

    def test_optional_300ms_settle_triples_the_grid(self):
        cells = matrix.plan_link(settles_ms=(10, 150, 300))
        self.assertEqual(len(cells), 24)
        self.assertEqual({c.settle_ms for c in cells}, {10, 150, 300})

    def test_planner_rejects_bad_dimensions(self):
        with self.assertRaises(ValueError):
            matrix.plan_link(modes=())
        with self.assertRaises(ValueError):
            matrix.plan_link(modes=(0, 4))
        with self.assertRaises(ValueError):
            matrix.plan_link(modes=(0, 0))
        with self.assertRaises(ValueError):
            matrix.plan_link(speeds_hz=())
        with self.assertRaises(ValueError):
            matrix.plan_link(speeds_hz=(0,))
        with self.assertRaises(ValueError):
            matrix.plan_link(settles_ms=())
        with self.assertRaises(ValueError):
            matrix.plan_link(settles_ms=(-1,))

    def test_queries_are_the_dll_exact_layout_lengths(self):
        self.assertEqual([(q.name, q.opcode, q.read_len)
                          for q in matrix.LINK_QUERIES],
                         [("fw", 0xA0, 7), ("info", 0xC1, 9),
                          ("scheme", 0xA1, 64)])

    def test_allowlist_refuses_anything_else(self):
        self.assertEqual(matrix.LINK_ALLOWED_OPS, {0xA0, 0xA1, 0xC1})
        for bad in (0x2D, 0xB3, 0xC0):
            with self.assertRaises(matrix.MatrixRefused):
                matrix.validate_link_queries(
                    (matrix.MatrixQuery("bad", bad, 1, 1),))
        with self.assertRaises(matrix.MatrixRefused):
            matrix.analyze_link_answer(0xB0, b"\x00" * 7)
        with self.assertRaises(matrix.MatrixRefused):
            matrix.analyze_link_answer(0x2D, b"\x00")


class TestLinkAnalysis(unittest.TestCase):
    def test_plausible_firmware(self):
        check = matrix.analyze_link_answer(0xA0, VALID_FW)
        self.assertEqual(check.verdict, matrix.PLAUSIBLE_FW)
        self.assertEqual(check.fields["project"], 2)
        self.assertEqual(check.fields["version"], 0x04030201)

    def test_zero_version_and_bad_state_are_not_plausible(self):
        blank = bytes([0x00, 0x02, 0x00, 0, 0, 0, 0])
        self.assertEqual(matrix.analyze_link_answer(0xA0, blank).verdict,
                         matrix.MALFORMED)
        bad_state = bytes([0xFF, 0x02, 0x00, 1, 2, 3, 4])
        self.assertEqual(matrix.analyze_link_answer(0xA0, bad_state).verdict,
                         matrix.MALFORMED)

    def test_implausible_project_is_malformed(self):
        observed = bytes([0xFF, 0x7F, 0x50, 0x03, 0, 0, 0])
        check = matrix.analyze_link_answer(0xA0, observed)
        self.assertEqual(check.verdict, matrix.MALFORMED)
        self.assertIn("project", check.detail)

    def test_plausible_sensor_info_and_absurd_geometry(self):
        self.assertEqual(matrix.analyze_link_answer(0xC1, VALID_INFO).verdict,
                         matrix.PLAUSIBLE_INFO)
        absurd = bytes([0, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF, 0])
        self.assertEqual(matrix.analyze_link_answer(0xC1, absurd).verdict,
                         matrix.MALFORMED)
        huge = bytes([0, 0, 0, 0, 0x00, 0x10, 0x00, 0x10, 0])  # 4096x4096
        self.assertEqual(matrix.analyze_link_answer(0xC1, huge).verdict,
                         matrix.MALFORMED)

    def test_constant_and_empty_answers(self):
        self.assertEqual(matrix.analyze_link_answer(0xA0, b"\x00" * 7).verdict,
                         matrix.LINK_ZEROS)
        self.assertEqual(matrix.analyze_link_answer(0xA0, b"\xff" * 7).verdict,
                         matrix.LINK_ALL_FF)
        self.assertEqual(matrix.analyze_link_answer(0xA0, b"").verdict,
                         matrix.EMPTY)

    def test_shift_detection_is_one_bit_and_not_degenerate(self):
        reference = b"\x64" * 9
        right = b"\x32" * 9
        self.assertEqual(matrix._shifted_by_one(reference, right), "right")
        left = bytes((((reference[i] << 1) | (reference[i + 1] >> 7)) & 0xFF)
                     for i in range(8)) + b"\x00"
        self.assertEqual(matrix._shifted_by_one(reference, left), "left")
        self.assertIsNone(matrix._shifted_by_one(b"\x00" * 9, b"\x00" * 9))
        self.assertIsNone(matrix._shifted_by_one(b"\xff" * 9, b"\x7f" * 9))
        self.assertIsNone(matrix._shifted_by_one(reference, reference))
        self.assertIsNone(matrix._shifted_by_one(b"\x64" * 9, b"\x55" * 9))


class TestLinkRunner(unittest.TestCase):
    def test_plausible_fw_at_a_non_default_mode(self):
        answers = {(3, 1_000_000, 150, 0xA0): VALID_FW}
        report, closed = run_link_fake(answers)
        self.assertEqual(report.outcome, matrix.PLAUSIBLE_FW)
        winning = [a for a in report.attempts
                   if a.verdict.verdict == matrix.PLAUSIBLE_FW]
        self.assertEqual(len(winning), 1)
        self.assertEqual((winning[0].cell.mode, winning[0].cell.speed_hz,
                          winning[0].cell.settle_ms), (3, 1_000_000, 150))
        self.assertIn("PLAUSIBLE FW", report.verdict)
        self.assertEqual(len(closed), 16)

    def test_plausible_info_without_firmware(self):
        answers = {(2, 4_000_000, 10, 0xA0):
                       b"\x7f\x50\x03\x00\x00\x00\x00",
                   (2, 4_000_000, 10, 0xC1): VALID_INFO}
        report, _closed = run_link_fake(answers)
        self.assertEqual(report.outcome, matrix.PLAUSIBLE_INFO)
        self.assertIn("PLAUSIBLE INFO", report.verdict)

    def test_byte_shift_between_mode_0_and_mode_3(self):
        answers = {
            (0, 1_000_000, 10, 0xC1): b"\x64" * 9,
            (3, 1_000_000, 10, 0xC1): b"\x32" * 9,
        }
        report, _closed = run_link_fake(answers)
        self.assertEqual(report.outcome, matrix.BYTE_SHIFT)
        shifted = [a for a in report.attempts
                   if a.verdict.verdict == matrix.BYTE_SHIFT]
        self.assertEqual(len(shifted), 1)
        self.assertEqual(shifted[0].cell.mode, 3)
        self.assertEqual(shifted[0].shift["matches"][0]["direction"], "right")
        self.assertIn("sampling edge is wrong", report.verdict)

    def test_all_zeros_and_all_ff(self):
        zeros, _ = run_link_fake({})
        self.assertEqual(zeros.outcome, "no-plausible-answer")
        self.assertTrue(all(a.verdict.verdict == matrix.LINK_ZEROS
                            for a in zeros.attempts))
        self.assertIn("all-zero", zeros.verdict)
        all_ff = {(mode, speed, settle, query.opcode):
                      b"\xff" * query.read_len
                  for mode in matrix.LINK_MODES
                  for speed in matrix.LINK_SPEEDS_HZ
                  for settle in matrix.LINK_SETTLES_MS
                  for query in matrix.LINK_QUERIES}
        report, _ = run_link_fake(all_ff)
        self.assertTrue(all(a.verdict.verdict == matrix.LINK_ALL_FF
                            for a in report.attempts))
        self.assertIn("0xFF", report.verdict)

    def test_all_timeouts(self):
        answers = {}
        for mode in matrix.LINK_MODES:
            for speed in matrix.LINK_SPEEDS_HZ:
                for settle in matrix.LINK_SETTLES_MS:
                    for query in matrix.LINK_QUERIES:
                        answers[(mode, speed, settle, query.opcode)] = (
                            lambda opcode, read_len: matrix.ReadResult(
                                b"", "timeout: deadline", "none"))
        report, _ = run_link_fake(answers)
        self.assertTrue(all(a.verdict.verdict == matrix.LINK_TIMEOUT
                            for a in report.attempts))
        self.assertIn("timed out", report.verdict)

    def test_all_malformed_reports_the_not_initialised_reading(self):
        answers = {(mode, speed, settle, opcode): b"\x11\x22\x33\x44\x55\x66\x77"
                   for mode in matrix.LINK_MODES
                   for speed in matrix.LINK_SPEEDS_HZ
                   for settle in matrix.LINK_SETTLES_MS
                   for opcode in matrix.LINK_ALLOWED_OPS}
        # Pad the bytes to the query length so analysis sees the full answer.
        for key in list(answers):
            opcode = key[3]
            read_len = {0xA0: 7, 0xC1: 9, 0xA1: 64}[opcode]
            answers[key] = (answers[key] * read_len)[:read_len]
        report, _ = run_link_fake(answers)
        self.assertEqual(report.outcome, "all-malformed")
        self.assertIn("device not being initialised", report.verdict)

    def test_json_is_complete_and_serialisable(self):
        import json
        report, _ = run_link_fake({(3, 1_000_000, 150, 0xA0): VALID_FW})
        blob = json.dumps(report.to_dict())
        data = json.loads(blob)
        self.assertEqual(data["schema"], 1)
        self.assertEqual((data["planned"], data["attempted"]), (16, 16))
        self.assertEqual(data["outcome"], matrix.PLAUSIBLE_FW)
        self.assertEqual(data["cells"][11]["mode"], 3)
        self.assertEqual(data["cells"][11]["verdict"], matrix.PLAUSIBLE_FW)
        self.assertEqual(data["cells"][0]["answers"]["fw"]["read_len"], 7)

    def test_report_table_shows_mode_speed_settle_and_all_three_hex(self):
        report, _ = run_link_fake({(0, 1_000_000, 10, 0xA0): VALID_FW,
                                   (0, 1_000_000, 10, 0xC1): VALID_INFO,
                                   (0, 1_000_000, 10, 0xA1):
                                       bytes([4, 0, 1] + [0] * 61)})
        table = "\n".join(report.render_table())
        self.assertIn("mode", table)
        self.assertIn("0xA0", table)
        self.assertIn("0xC1", table)
        self.assertIn("0xA1", table)
        self.assertIn("plausible-fw", table)
        self.assertIn("00 02 00 01 02 03 04", table)


class TestSpiLinkSession(unittest.TestCase):
    def test_without_a_reset_pin_it_fails_closed(self):
        cell = matrix.plan_link()[0]
        session = matrix.SpiLinkSession(cell, spidev_path="/dev/null")
        result = session.read(0xA0, 7)
        self.assertIn("error", result.error)
        self.assertIn("reset", result.error)
        self.assertEqual(result.data, b"")


class TestProbeLinkIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = support.load_probe()

    def run_probe(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.probe.main(argv)
        return rc, out.getvalue()

    def test_probe_link_dry_run_plans_16_cells_and_does_no_io(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "link.json")
            rc, text = self.run_probe([
                "--try-link", "--dry-run", "--spidev", "/dev/spidev0.0",
                "--gpio", "18", "--gpio-chip", "gpiochip1",
                "--reset-gpio", "16", "--allow-reset", "--allow-writes",
                "--link-json", path])
            self.assertEqual(rc, 0)
            self.assertIn("link: 16 cells", text)
            self.assertIn("mode=3", text)
            self.assertIn("settle=150ms", text)
            self.assertFalse(os.path.exists(path),
                             "a dry run must not write the link JSON")

    def test_probe_link_dry_run_accepts_the_optional_300ms_settle(self):
        rc, text = self.run_probe([
            "--try-link", "--dry-run", "--spidev", "/dev/spidev0.0",
            "--link-settles", "10,150,300"])
        self.assertEqual(rc, 0)
        self.assertIn("link: 24 cells", text)
        self.assertIn("settle=300ms", text)

    def test_probe_refuses_command_names_in_link_mode(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main(["--try-link", "fw"])
        self.assertEqual(rc, 2)

    def test_probe_refuses_link_without_allow_reset(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main([
                "--try-link", "--spidev", "/dev/spidev0.0", "--gpio", "18",
                "--reset-gpio", "16", "--allow-writes"])
        self.assertEqual(rc, 2)
        self.assertIn("--allow-reset", out.getvalue())

    def test_probe_link_writes_the_table_even_when_every_cell_fails(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            spidev = os.path.join(tmp, "spidev0.0")
            open(spidev, "w").close()
            path = os.path.join(tmp, "link.json")
            rc, text = self.run_probe([
                "--try-link", "--spidev", spidev, "--gpio", "18",
                "--gpio-chip", os.path.join(tmp, "gpiochip1"),
                "--reset-gpio", "16", "--allow-reset", "--allow-writes",
                "--reset-polarity", "active-high", "--irq-edge", "rising",
                "--link-json", path])
            self.assertEqual(rc, 1)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual((data["planned"], data["attempted"]), (16, 16))
            self.assertEqual(data["outcome"], "no-plausible-answer")
            self.assertIn("link:", text)



# ---------------------------------------------------------------------------
# Read-shape comparison (--compare-read-shapes)
# ---------------------------------------------------------------------------

class ReadShapeFakeSession:
    """Fake transport for the read-shape runner: keyed by (mode, shape, op)."""

    def __init__(self, cell, answers, closed):
        self.cell = cell
        self.answers = answers
        self.closed = closed

    def read(self, opcode, read_len, shape):
        value = self.answers.get((self.cell.mode, shape, opcode))
        if value is None:
            value = b"\x00" * read_len
        if callable(value):
            return value(opcode, read_len, shape)
        return read_result(bytes(value))

    def close(self):
        self.closed.append((self.cell.index, self.cell.mode))


def run_read_shape_fake(answers, cells=None, closed=None):
    cells = tuple(cells or matrix.plan_read_shapes())
    closed = closed if closed is not None else []

    def connect(cell):
        return ReadShapeFakeSession(cell, answers, closed)

    return matrix.run_read_shapes(cells, connect=connect), closed


class TestShiftDetector(unittest.TestCase):
    def test_every_shift_in_range_is_recovered_exactly(self):
        reference = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0,
                           0x11, 0x22])
        for shift in range(-7, 8):
            candidate = matrix._shift_stream(reference, shift)
            match = matrix.bit_shift_match(reference, candidate)
            self.assertIsNotNone(match.best_shift, shift)
            self.assertEqual(match.best_shift, shift,
                             (shift, match.to_dict()))
            self.assertTrue(match.exact, (shift, match.to_dict()))
            self.assertEqual(match.reference_len, len(reference))

    def test_best_match_and_tie_handling(self):
        repeated = b"\x64" * 9
        # 0x32 = 0x64 >> 1, but the repeating byte also makes +7 equivalent;
        # both must be reported and the smallest |shift| wins.
        match = matrix.bit_shift_match(repeated, b"\x32" * 9,
                                       shifts=(-1, 7))
        self.assertEqual(match.best_shift, -1)
        self.assertEqual(match.ties, (-1, 7))
        self.assertTrue(match.exact)
        self.assertEqual(match.best_matches, match.compared)

    def test_degenerate_streams(self):
        short = matrix.bit_shift_match(b"\x01", b"\x02")
        self.assertIsNone(short.best_shift)
        self.assertEqual(short.reason, "too short")
        constant = matrix.bit_shift_match(b"\x00" * 9, b"\x00" * 9)
        self.assertIsNone(constant.best_shift)
        self.assertEqual(constant.reason, "constant stream")
        other = matrix.bit_shift_match(b"\xff" * 9, b"\x7f" * 9)
        self.assertIsNone(other.best_shift)
        equal = matrix.bit_shift_match(b"\x10\x20\x30\x40", b"\x10\x20\x30\x40")
        self.assertEqual(equal.best_shift, 0)
        self.assertTrue(equal.exact)
        different = matrix.bit_shift_match(b"\x10\x20\x30\x40",
                                           b"\x55\xaa\x55\xaa")
        self.assertIsNone(different.best_shift)
        self.assertEqual(different.reason, "no byte matched at any shift")

    def test_shift_candidates_are_bounded(self):
        with self.assertRaises(ValueError):
            matrix.bit_shift_match(b"\x01\x02", b"\x01\x02", shifts=(8,))
        with self.assertRaises(ValueError):
            matrix.bit_shift_match(b"\x01\x02", b"\x01\x02", shifts=(-8,))


class TestTransportReceived(unittest.TestCase):
    def test_cs_held_diagnostics_skip_the_tx_prefix(self):
        class FakeIO:
            last_message = (b"\x00" + b"\xaa" * 7,)

        class FakeTransport:
            _io = FakeIO()

        plan = matrix.spi.build_plan(0xA0, read_len=7)
        self.assertEqual(matrix._transport_received(FakeTransport(), plan),
                         b"\xaa" * 7)
        split = matrix.spi.build_plan(0xA0, read_len=7, read_shape="split")
        self.assertEqual(matrix._transport_received(FakeTransport(), split),
                         b"\x00" + b"\xaa" * 7)
        self.assertEqual(matrix._transport_received(FakeTransport(), None),
                         b"\x00" + b"\xaa" * 7)


class TestReadShapePlanner(unittest.TestCase):
    def test_default_plan_is_four_cells_and_five_rows(self):
        cells = matrix.plan_read_shapes()
        self.assertEqual(len(cells), 4)
        first = cells[0]
        self.assertEqual((first.mode, first.speed_hz, first.settle_ms),
                         (0, matrix.READ_SHAPE_SPEED_HZ,
                          matrix.READ_SHAPE_SETTLE_MS))
        self.assertEqual(first.shapes, ("split", "cs-held"))
        self.assertEqual([cell.mode for cell in cells[1:]], [1, 2, 3])
        self.assertTrue(all(cell.shapes == ("cs-held",) for cell in cells[1:]))
        report, _closed = run_read_shape_fake({})
        self.assertEqual(len(report.attempts), 5)
        self.assertEqual([(a.cell.mode, a.shape) for a in report.attempts],
                         [(0, "split"), (0, "cs-held"),
                          (1, "cs-held"), (2, "cs-held"), (3, "cs-held")])

    def test_planner_rejects_bad_dimensions(self):
        with self.assertRaises(ValueError):
            matrix.plan_read_shapes(speed_hz=0)
        with self.assertRaises(ValueError):
            matrix.plan_read_shapes(settle_ms=-1)
        with self.assertRaises(ValueError):
            matrix.plan_read_shapes(modes=())
        with self.assertRaises(ValueError):
            matrix.plan_read_shapes(modes=(1, 2))
        with self.assertRaises(ValueError):
            matrix.plan_read_shapes(modes=(0, 0))

    def test_queries_are_the_three_layout_lengths(self):
        self.assertEqual([(q.name, q.opcode, q.read_len)
                          for q in matrix.READ_SHAPE_QUERIES],
                         [("fw", 0xA0, 7), ("info", 0xC1, 9),
                          ("scheme", 0xA1, 64)])


class TestReadShapeRunner(unittest.TestCase):
    def test_shapes_agree_and_cs_held_is_plausible(self):
        answers = {
            (0, "split", 0xA0): VALID_FW,
            (0, "cs-held", 0xA0): VALID_FW,
            (0, "split", 0xC1): VALID_INFO,
            (0, "cs-held", 0xC1): VALID_INFO,
            (0, "split", 0xA1): VALID_SCHEME,
            (0, "cs-held", 0xA1): VALID_SCHEME,
        }
        report, closed = run_read_shape_fake(answers)
        self.assertEqual(report.outcome, matrix.PLAUSIBLE_FW)
        self.assertTrue(all(item.agree is True for item in report.comparisons()))
        self.assertIn("both shapes agree", report.verdict)
        self.assertEqual(len(closed), 4)
        self.assertEqual(closed[0], (1, 0),
                         "both mode-0 shapes share one session/reset")

    def test_split_was_the_bug_when_only_cs_held_parses(self):
        shifted = matrix._shift_stream(VALID_FW, -1)
        answers = {
            (0, "split", 0xA0): shifted,
            (0, "cs-held", 0xA0): VALID_FW,
            (0, "split", 0xC1): matrix._shift_stream(VALID_INFO, -1),
            (0, "cs-held", 0xC1): VALID_INFO,
        }
        report, _ = run_read_shape_fake(answers)
        self.assertEqual(report.outcome, matrix.PLAUSIBLE_FW)
        fw = report.comparisons()[0]
        self.assertIs(fw.agree, False)
        self.assertEqual(fw.shift.best_shift, -1)
        self.assertTrue(fw.shift.exact)
        self.assertIn("split segment boundary was the bug", report.verdict)

    def test_phase_artefact_without_a_plausible_answer(self):
        first = b"\x12\x34\x56\x78\x9a\xbc\xde\xf0\x11\x22"
        right = matrix._shift_stream(first, -2)
        answers = {
            (0, "split", 0xC1): right,
            (0, "cs-held", 0xC1): first,
                 }
        report, _ = run_read_shape_fake(answers)
        self.assertEqual(report.outcome, "cs-held-divergent")
        comparison = report.comparisons()[1]
        self.assertIs(comparison.agree, False)
        self.assertEqual(comparison.shift.best_shift, -2)
        self.assertTrue(comparison.shift.exact)
        self.assertIn("phase artefact", report.verdict)

    def test_mode_sensitivity_is_reported(self):
        answers = {
            (0, "cs-held", 0xC1): b"\x64" * 9,
            (1, "cs-held", 0xC1): b"\x32" * 9,
            (2, "cs-held", 0xC1): b"\x32" * 9,
            (3, "cs-held", 0xC1): b"\x32" * 9,
        }
        report, _ = run_read_shape_fake(answers)
        self.assertTrue(report.mode_sensitive)
        rows = [row.to_dict() for row in report.mode_comparisons()
                if row.query.opcode == 0xC1 and row.mode == 1]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["same_as_mode0"])
        self.assertEqual(rows[0]["shift"]["best_shift"], -1)

    def test_json_and_table_are_complete(self):
        import json
        answers = {
            (0, "split", 0xA0): VALID_FW,
            (0, "cs-held", 0xA0): VALID_FW,
            (0, "cs-held", 0xC1): VALID_INFO,
        }
        report, _ = run_read_shape_fake(answers)
        data = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(data["schema"], matrix.READ_SHAPE_SCHEMA)
        self.assertEqual((data["planned"], data["attempted"]), (4, 5))
        self.assertEqual(len(data["rows"]), 5)
        self.assertEqual(len(data["comparisons"]), 3)
        table = "\n".join(report.render_table())
        for needle in ("shape", "mode", "cs-held", "0xA0", "0xC1",
                       "0xA1", "agree", "verdict"):
            self.assertIn(needle, table)

    def test_without_a_reset_pin_the_session_fails_closed(self):
        cell = matrix.plan_read_shapes()[0]
        session = matrix.SpiReadShapeSession(cell, spidev_path="/dev/null")
        result = session.read(0xA0, 7, "cs-held")
        self.assertIn("error", result.error)
        self.assertIn("reset", result.error)
        self.assertEqual(result.data, b"")


class TestProbeReadShapeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = support.load_probe()

    def run_probe(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.probe.main(argv)
        return rc, out.getvalue()

    def test_probe_dry_run_plans_the_comparison_and_does_no_io(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "read-shapes.json")
            rc, text = self.run_probe([
                "--compare-read-shapes", "--dry-run",
                "--spidev", "/dev/spidev0.0", "--gpio", "18",
                "--gpio-chip", "gpiochip1", "--reset-gpio", "16",
                "--allow-reset", "--allow-writes",
                "--read-shapes-json", path])
            self.assertEqual(rc, 0)
            self.assertIn("compare-read-shapes: 4 cells", text)
            self.assertIn("mode=0", text)
            self.assertIn("mode=3", text)
            self.assertIn("shapes=split+cs-held", text)
            self.assertFalse(os.path.exists(path),
                             "a dry run must not write the comparison JSON")

    def test_probe_refuses_command_names_in_compare_mode(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main(["--compare-read-shapes", "fw"])
        self.assertEqual(rc, 2)

    def test_probe_refuses_compare_without_allow_reset(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main([
                "--compare-read-shapes", "--spidev", "/dev/spidev0.0",
                "--gpio", "18", "--reset-gpio", "16", "--allow-writes"])
        self.assertEqual(rc, 2)
        self.assertIn("--allow-reset", out.getvalue())

    def test_probe_compare_refuses_a_bad_timeout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main(["--compare-read-shapes", "--dry-run",
                                  "--read-shapes-timeout-ms", "50"])
        self.assertEqual(rc, 2)
        self.assertIn("--read-shapes-timeout-ms", out.getvalue())

    def test_probe_compare_refuses_op_and_commands(self):
        for argv, needle in ((["--compare-read-shapes", "--op", "0xA0"],
                              "--op"),
                             (["--compare-read-shapes", "fw"],
                              "do not name commands")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                rc = self.probe.main(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(needle, out.getvalue())

    def test_probe_compare_refuses_a_missing_spidev(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main([
                "--compare-read-shapes", "--gpio", "18",
                "--reset-gpio", "16", "--allow-reset", "--allow-writes"])
        self.assertEqual(rc, 2)
        self.assertIn("--spidev", out.getvalue())

    def test_probe_compare_refuses_a_missing_json_directory(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            spidev = os.path.join(tmp, "spidev0.0")
            open(spidev, "w").close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                rc = self.probe.main([
                    "--compare-read-shapes", "--spidev", spidev,
                    "--gpio", "18", "--reset-gpio", "16",
                    "--gpio-chip", os.path.join(tmp, "gpiochip1"),
                    "--allow-reset", "--allow-writes",
                    "--read-shapes-json", os.path.join(tmp, "missing",
                                                       "out.json")])
            self.assertEqual(rc, 2)
            self.assertIn("does not exist", out.getvalue())

    def test_probe_refuses_compare_with_another_experiment(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = self.probe.main([
                "--compare-read-shapes", "--try-link", "--allow-reset"])
        self.assertEqual(rc, 2)

    def test_probe_writes_the_table_even_when_every_cell_fails(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            spidev = os.path.join(tmp, "spidev0.0")
            open(spidev, "w").close()
            path = os.path.join(tmp, "read-shapes.json")
            rc, text = self.run_probe([
                "--compare-read-shapes", "--spidev", spidev,
                "--gpio", "18",
                "--gpio-chip", os.path.join(tmp, "gpiochip1"),
                "--reset-gpio", "16", "--allow-reset", "--allow-writes",
                "--reset-polarity", "active-high", "--irq-edge", "rising",
                "--read-shapes-json", path])
            self.assertEqual(rc, 1)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual((data["planned"], data["attempted"]), (4, 5))
            self.assertEqual(data["outcome"], "no-answer")
            self.assertIn("compare-read-shapes:", text)
            self.assertIn("verdict:", text)


if __name__ == "__main__":
    unittest.main()
