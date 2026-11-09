"""Matrix bring-up for the BF63112: reset polarity x settle x IRQ edge.

The single-setting probe can only test one hypothesis per run.  This module
turns the ambiguous parts of the bring-up (ACPI ``GpioIo`` carries no reset
polarity, the interrupt may be rising or falling, and the module may need a
settling delay after reset) into one bounded, read-only experiment:

* reset polarity: ``active-low``, ``active-high`` and ``hold`` (never pulse the
  line - the control),
* settle delay after the reset is released (or before the first query for
  ``hold``): 10 ms and 150 ms,
* interrupt edge requested from the GPIO backend: ``rising``, ``falling`` and
  ``both``,

i.e. 3 x 2 x 3 = 18 cells.  Every cell sends the allowlisted read-only query
``0xA0`` (firmware version); only a non-zero ``0xA0`` answer escalates to
``0xA1`` and ``0xC1``.  Nothing outside ``{0xA0, 0xA1, 0xC1}`` can be planned or
executed here, and the :class:`~fpbf63112.spi.SpiTransport` gate still checks
the opcode against its read allowlist and refuses destructive opcodes by opcode
*and* selector.

A cell is a success only when the answer **parses**, never merely when it is
non-zero: :func:`parse_valid_answer` accepts either

* a complete ``0xAA | L:u32 | ... | CRC-32`` frame (the framing recovered from
  the vendor driver, including the CRC check) whose body matches the recovered
  per-command layout, or
* the bare fixed-size SPI answer (the format the selector transport currently
  reads) with the same recovered layout.

``0x00``/``0xFF`` payloads, a short read, an unknown project id or an
implausible sensor geometry are reported as their own verdict and never as
success.

The runner is transport-agnostic: :func:`run_matrix` takes a ``connect(cell)``
factory returning an object with ``read(opcode, read_len) -> ReadResult``.  The
probe wires that to :class:`SpiMatrixSession` (the real GPIO/SPI backends); the
unit tests wire it to a fake transport, so no test opens ``/dev/spidev*`` or a
real ``/dev/gpiochip*``.
"""

from __future__ import annotations

import dataclasses
import json
import struct
import time

from . import protocol, spi

# ---------------------------------------------------------------------------
# Matrix shape and allowlist
# ---------------------------------------------------------------------------

MATRIX_SCHEMA = 1

#: (label, active_low) - ``None`` means "do not pulse the reset line at all".
POLARITIES: tuple[tuple[str, bool | None], ...] = (
    ("active-low", True),
    ("active-high", False),
    ("hold", None),
)

#: Delay in ms after the reset is released (or before the first query for
#: ``hold``).  The short value covers the conventional 5/10 ms pulse; the long
#: one covers a module that needs more time to boot after reset.
SETTLES_MS: tuple[int, ...] = (10, 150)

#: Requested interrupt edges.
EDGES: tuple[str, ...] = ("rising", "falling", "both")

#: Default per-attempt timeout.  The matrix is meant to be a bounded experiment,
#: so it deliberately does not use the driver's 1000/2030 ms query budget.
DEFAULT_TIMEOUT_MS = 1500


@dataclasses.dataclass(frozen=True)
class MatrixQuery:
    """One allowlisted query used inside a matrix cell."""

    name: str
    opcode: int
    layout_len: int
    read_len: int
    description: str = ""


#: The only opcodes a matrix attempt may send, in escalation order.  The read
#: lengths are at least 64 bytes so a ``0xAA`` framed answer (41 B for a 7-byte
#: payload) is not truncated; the bare-answer parser only consumes
#: ``layout_len`` bytes of it.
MATRIX_QUERIES: tuple[MatrixQuery, ...] = (
    MatrixQuery("fw", 0xA0, 7, 64, "firmware version / project"),
    MatrixQuery("scheme", 0xA1, 64, 64, "module scheme table"),
    MatrixQuery("info", 0xC1, 9, 64, "sensor geometry + flags"),
)

#: Hard allowlist for the matrix.  Checked here *and* again by SpiTransport.
MATRIX_ALLOWED_OPS: frozenset[int] = frozenset(q.opcode for q in MATRIX_QUERIES)

#: Verdicts.  Only :data:`VALID` is success.
VALID = "valid"
ZEROS = "zeros"
ALL_FF = "all-ff"
SHORT = "short"
MALFORMED = "malformed"
NONZERO = "nonzero-unparsed"
EMPTY = "no-answer"
TIMEOUT = "timeout"
ERROR = "error"

_FAILURE_VERDICTS = frozenset({ZEROS, ALL_FF, TIMEOUT, ERROR, EMPTY})
_UNPARSEABLE_VERDICTS = frozenset({MALFORMED, NONZERO, SHORT})

#: Project ids recovered from the vendor driver (see docs/hw/fingerprint.md).
PROJECTS = {
    1: "BOE MOC",
    2: "HUAHONG MOUSE MOC",
    3: "DYW MOH",
    4: "NATION TEST",
    6: "NATIONALCHIP",
}


class MatrixRefused(spi.SpiRefused):
    """The matrix planner/runner refused an opcode or a configuration."""


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class MatrixCell:
    """One combination of reset polarity, settle delay and interrupt edge."""

    index: int
    polarity: str
    active_low: bool | None
    settle_ms: int
    edge: str

    @property
    def pulse(self) -> bool:
        """``True`` when the reset line is actually pulsed (not ``hold``)."""
        return self.active_low is not None

    def label(self) -> str:
        reset = ("pulse" if self.pulse else "hold")
        return (f"{self.index}: polarity={self.polarity} ({reset}) "
                f"settle={self.settle_ms}ms edge={self.edge}")


def plan_matrix(*, polarities=POLARITIES, settles_ms=SETTLES_MS,
                edges=EDGES) -> tuple[MatrixCell, ...]:
    """Return the deterministic matrix, ``polarity x settle x edge``.

    The order is the one the probe prints: active-low first (the previously
    assumed polarity), short settle first, rising first.  That makes the first
    attempt the least surprising one and keeps a successful early cell short.
    """
    polarities = tuple(polarities)
    settles_ms = tuple(int(ms) for ms in settles_ms)
    edges = tuple(edges)
    if not polarities:
        raise ValueError("the matrix needs at least one reset polarity")
    if not settles_ms or any(ms < 0 for ms in settles_ms):
        raise ValueError("settle delays must be a non-empty list of ms >= 0")
    if not edges:
        raise ValueError("the matrix needs at least one interrupt edge")
    for name, active_low in polarities:
        if name not in ("active-low", "active-high", "hold"):
            raise ValueError(f"unknown reset polarity {name!r}")
        if name == "hold" and active_low is not None:
            raise ValueError("the hold control must not carry a polarity")
        if name != "hold" and not isinstance(active_low, bool):
            raise ValueError(f"{name} needs an explicit active_low bool")
    for edge in edges:
        if edge not in ("rising", "falling", "both"):
            raise ValueError(f"unknown interrupt edge {edge!r}")
    cells = []
    for name, active_low in polarities:
        for settle in settles_ms:
            for edge in edges:
                cells.append(MatrixCell(len(cells) + 1, name, active_low,
                                        settle, edge))
    return tuple(cells)


# ---------------------------------------------------------------------------
# Valid-answer parsing
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class AnswerCheck:
    """The explicit success criterion for one answer."""

    verdict: str
    detail: str
    framing: str | None = None       # "frame" or "bare"
    payload: bytes = b""
    fields: dict | None = None

    @property
    def valid(self) -> bool:
        return self.verdict == VALID

    @property
    def unparseable_nonzero(self) -> bool:
        return self.verdict in _UNPARSEABLE_VERDICTS


def _all_byte(data: bytes, value: int) -> bool:
    return bool(data) and all(byte == value for byte in data)


def parse_valid_answer(opcode: int, data: bytes) -> AnswerCheck:
    """Check one answer against the documented framing and layout.

    Success requires a complete framed answer with a matching CRC-32 (when the
    answer starts with ``0xAA``) or a long enough bare answer, **and** a body
    that matches the recovered layout for ``opcode``.  A non-zero blob that
    does not parse is not success.
    """
    if opcode not in MATRIX_ALLOWED_OPS:
        raise MatrixRefused(
            f"opcode 0x{opcode:02x} is not a matrix query; allowed: "
            + ", ".join(f"0x{op:02x}" for op in sorted(MATRIX_ALLOWED_OPS)))
    data = bytes(data)
    if not data:
        return AnswerCheck(EMPTY, "no bytes were read back")

    layout_len = {q.opcode: q.layout_len for q in MATRIX_QUERIES}[opcode]
    framing = "bare"
    payload = data
    detail = ""

    if data[0] == protocol.SOF:
        framing = "frame"
        if len(data) < protocol.FRAME_OVERHEAD:
            return AnswerCheck(
                SHORT, f"0xAA answer is {len(data)}B; a frame needs at least "
                       f"{protocol.FRAME_OVERHEAD}B")
        need = struct.unpack_from("<I", data, 1)[0] + protocol.FRAME_OVERHEAD
        if need < protocol.FRAME_MIN_SIZE:
            return AnswerCheck(
                MALFORMED, f"L field implies a {need}B frame, minimum is "
                           f"{protocol.FRAME_MIN_SIZE}B", framing)
        if len(data) < need:
            return AnswerCheck(
                SHORT, f"framed answer announces {need}B, read back "
                       f"{len(data)}B", framing)
        try:
            frame = protocol.parse_frame(data[:need])
        except protocol.FrameError as exc:
            return AnswerCheck(MALFORMED, f"frame does not parse: {exc}",
                               framing)
        if frame.opcode != opcode:
            return AnswerCheck(
                MALFORMED,
                f"frame carries opcode 0x{frame.opcode:02x}, expected "
                f"0x{opcode:02x}", framing)
        payload = frame.body
        detail = f"frame L={frame.length} crc=0x{frame.crc:08x}"
    else:
        if len(data) < layout_len:
            return AnswerCheck(
                SHORT, f"{len(data)}B read; layout needs {layout_len}B",
                framing)
        payload = data[:layout_len]
        detail = f"bare {len(payload)}B"

    if not payload:
        return AnswerCheck(EMPTY, "the answer has an empty body", framing)
    if _all_byte(payload, 0x00):
        return AnswerCheck(ZEROS, f"{len(payload)}B of 0x00", framing, payload)
    if _all_byte(payload, 0xFF):
        return AnswerCheck(ALL_FF, f"{len(payload)}B of 0xFF", framing, payload)

    if opcode == 0xA0:
        if len(payload) < 7:
            return AnswerCheck(SHORT, f"fw body is {len(payload)}B; need 7",
                               framing, payload)
        state = payload[0]
        project = payload[1]
        version = ".".join(str(b) for b in payload[3:7])
        if project not in PROJECTS:
            return AnswerCheck(
                MALFORMED,
                f"fw project byte 0x{project:02x} is not in the recovered "
                f"table ({', '.join(str(k) for k in sorted(PROJECTS))})",
                framing, payload)
        return AnswerCheck(
            VALID,
            f"fw={version} project={PROJECTS[project]} state={state} "
            f"({detail})",
            framing, payload,
            {"state": state, "project": project,
             "project_name": PROJECTS[project], "version": version})

    if opcode == 0xA1:
        if len(payload) < 8:
            return AnswerCheck(SHORT, f"scheme body is {len(payload)}B; need "
                                      f"at least 8", framing, payload)
        if not any(payload):
            return AnswerCheck(ZEROS, "scheme table is all zeros", framing,
                               payload)
        return AnswerCheck(
            VALID,
            f"scheme table {len(payload)}B, first bytes "
            f"{payload[:8].hex(' ')} ({detail})",
            framing, payload, {"table_len": len(payload)})

    # 0xC1 sensor info: +0 u16, +2 u16, +4 width, +6 height, +8 flags.
    if len(payload) < 9:
        return AnswerCheck(SHORT, f"info body is {len(payload)}B; need 9",
                           framing, payload)
    a, b, width, height = struct.unpack_from("<HHHH", payload, 0)
    flags = payload[8]
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        return AnswerCheck(
            MALFORMED,
            f"info geometry {width}x{height} is outside the plausible "
            f"1..4096 range", framing, payload)
    return AnswerCheck(
        VALID,
        f"sensor={width}x{height} image={width * height}B flags=0x{flags:02x} "
        f"({detail})",
        framing, payload,
        {"a": a, "b": b, "width": width, "height": height, "flags": flags})


# ---------------------------------------------------------------------------
# Results and runner
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ReadResult:
    """One transport read: bytes, an error string and the IRQ observation."""

    data: bytes = b""
    error: str = ""
    irq: str = ""       # "fired", "none", "error" (or "" when unknown)


@dataclasses.dataclass(frozen=True)
class QueryResult:
    query: MatrixQuery
    read: ReadResult
    check: AnswerCheck


@dataclasses.dataclass(frozen=True)
class AttemptResult:
    cell: MatrixCell
    queries: tuple[QueryResult, ...]
    verdict: str

    @property
    def primary(self) -> QueryResult:
        return self.queries[0]

    def to_dict(self) -> dict:
        return {
            "index": self.cell.index,
            "polarity": self.cell.polarity,
            "settle_ms": self.cell.settle_ms,
            "edge": self.cell.edge,
            "pulse": self.cell.pulse,
            "verdict": self.verdict,
            "bytes_hex": self.primary.read.data.hex(" "),
            "queries": [{
                "command": q.query.name,
                "opcode": f"0x{q.query.opcode:02x}",
                "layout_len": q.query.layout_len,
                "read_len": q.query.read_len,
                "bytes": len(q.read.data),
                "hex": q.read.data.hex(" "),
                "verdict": q.check.verdict,
                "detail": q.check.detail,
                "framing": q.check.framing,
                "fields": q.check.fields,
                "error": q.read.error,
                "irq": q.read.irq,
            } for q in self.queries],
        }


def validate_queries(queries) -> tuple[MatrixQuery, ...]:
    """Refuse any query outside the read-only matrix allowlist."""
    queries = tuple(queries)
    if not queries:
        raise MatrixRefused("the matrix needs at least one query")
    for query in queries:
        if not isinstance(query, MatrixQuery):
            raise MatrixRefused(f"not a MatrixQuery: {query!r}")
        if query.opcode in spi.DESTRUCTIVE_OPS:
            raise MatrixRefused(
                f"opcode 0x{query.opcode:02x} is "
                f"{spi.DESTRUCTIVE_OPS[query.opcode]}: refusing unconditionally")
        low = spi.selector_low(query.opcode)
        if low in spi.FORBIDDEN_SELECTORS:
            raise MatrixRefused(
                f"selector 0x{low:02x} (opcode 0x{query.opcode:02x}) is "
                f"reserved for a destructive command: refusing")
        if query.opcode not in MATRIX_ALLOWED_OPS:
            raise MatrixRefused(
                f"opcode 0x{query.opcode:02x} is not allowlisted for the "
                f"matrix; allowed: "
                + ", ".join(f"0x{op:02x}" for op in sorted(MATRIX_ALLOWED_OPS)))
        if query.read_len <= 0 or query.layout_len <= 0:
            raise MatrixRefused("query lengths must be positive")
        if query.read_len < query.layout_len:
            raise MatrixRefused(
                f"{query.name}: read_len {query.read_len} is shorter than the "
                f"layout length {query.layout_len}")
    return queries


def _read_query(session, query: MatrixQuery) -> QueryResult:
    if query.opcode not in MATRIX_ALLOWED_OPS:
        raise MatrixRefused(
            f"opcode 0x{query.opcode:02x} is not a matrix query")
    try:
        result = session.read(query.opcode, query.read_len)
    except spi.SpiError as exc:
        result = ReadResult(b"", f"error: {exc}")
    except OSError as exc:
        result = ReadResult(b"", f"error: {exc}")
    if not isinstance(result, ReadResult):
        result = ReadResult(bytes(getattr(result, "data", b"")),
                            str(getattr(result, "error", "")),
                            str(getattr(result, "irq", "")))
    data = bytes(result.data or b"")
    error = result.error or ""
    if error and not data:
        verdict = TIMEOUT if error.startswith("timeout") else ERROR
        check = AnswerCheck(verdict, error)
    else:
        check = parse_valid_answer(query.opcode, data)
    return QueryResult(query, ReadResult(data, error, result.irq), check)


def _run_cell(session, queries: tuple[MatrixQuery, ...]) -> tuple[QueryResult, ...]:
    results = []
    for position, query in enumerate(queries):
        result = _read_query(session, query)
        results.append(result)
        if result.check.valid:
            break
        if position == 0 and result.check.verdict in _FAILURE_VERDICTS:
            # Zeros/0xFF/timeouts/short: escalating cannot help and would only
            # spend more time on the same failing bus configuration.
            break
        if position == 0 and result.check.verdict in (MALFORMED, NONZERO):
            # Non-zero but unparsed: transport works; try the other layouts.
            continue
    return tuple(results)


def _cell_verdict(results: tuple[QueryResult, ...]) -> str:
    if any(result.check.valid for result in results):
        return VALID
    verdicts = [result.check.verdict for result in results]
    for verdict in (NONZERO, MALFORMED, TIMEOUT, ERROR, SHORT, ALL_FF, ZEROS):
        if verdict in verdicts:
            return verdict
    return EMPTY


def _counts_text(attempts) -> str:
    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.verdict] = counts.get(attempt.verdict, 0) + 1
    order = [VALID, ZEROS, ALL_FF, TIMEOUT, ERROR, SHORT, MALFORMED, NONZERO,
             EMPTY]
    return ", ".join(f"{counts[v]} {v}" for v in order if counts.get(v))


def _tried_text(cells) -> str:
    polarities = []
    for cell in cells:
        if cell.polarity not in polarities:
            polarities.append(cell.polarity)
    settles = sorted({cell.settle_ms for cell in cells})
    edges = []
    for cell in cells:
        if cell.edge not in edges:
            edges.append(cell.edge)
    return (f"polarity {'/'.join(polarities)} x settle "
            f"{'/'.join(str(ms) for ms in settles)} ms x edge "
            f"{'/'.join(edges)}")


@dataclasses.dataclass
class MatrixReport:
    """The complete matrix result, printable and JSON-serialisable."""

    cells: tuple[MatrixCell, ...]
    attempts: list[AttemptResult]
    queries: tuple[MatrixQuery, ...]
    timeout_ms: int
    context: dict
    winning: AttemptResult | None = None

    @property
    def outcome(self) -> str:
        return "valid-answer" if self.winning else "no-valid-answer"

    @property
    def verdict(self) -> str:
        total = len(self.attempts)
        if self.winning is not None:
            cell = self.winning.cell
            return (f"VALID answer at attempt {cell.index}/{len(self.cells)} "
                    f"(polarity={cell.polarity}, settle={cell.settle_ms}ms, "
                    f"edge={cell.edge}): {self.winning.primary.check.detail}")
        counts = _counts_text(self.attempts)
        if all(attempt.verdict in (ZEROS, ALL_FF, TIMEOUT, EMPTY)
               for attempt in self.attempts):
            head = f"all {total} attempts returned zeros/timeouts"
        else:
            head = f"all {total} attempts failed (no valid answer)"
        return f"{head}: {counts}; tried {_tried_text(self.cells)}"

    def to_dict(self) -> dict:
        return {
            "schema": MATRIX_SCHEMA,
            "tool": "oxp3-fp-probe --try-matrix",
            "outcome": self.outcome,
            "verdict": self.verdict,
            "timeout_ms": self.timeout_ms,
            "planned": len(self.cells),
            "attempted": len(self.attempts),
            "context": dict(self.context),
            "queries": [{
                "command": q.name,
                "opcode": f"0x{q.opcode:02x}",
                "layout_len": q.layout_len,
                "read_len": q.read_len,
            } for q in self.queries],
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "winning": self.winning.to_dict() if self.winning else None,
        }

    def render_table(self) -> list[str]:
        """Compact one-line-per-attempt table for the console."""
        lines = ["#  polarity     settle  edge    0xA0 bytes received"
                 "               verdict"]
        for attempt in self.attempts:
            data = attempt.primary.read.data
            if not data:
                shown = "(none)"
            elif len(data) <= 24:
                shown = data.hex(" ")
            else:
                shown = data[:24].hex(" ") + f" ... (+{len(data) - 24}B)"
            lines.append(
                f"{attempt.cell.index:>2} {attempt.cell.polarity:<12} "
                f"{attempt.cell.settle_ms:>4}ms {attempt.cell.edge:<7} "
                f"{shown:<32} {attempt.verdict}")
        return lines


def run_matrix(cells, *, connect, queries=MATRIX_QUERIES,
               timeout_ms: int = DEFAULT_TIMEOUT_MS,
               stop_on_nonzero: bool = True,
               context: dict | None = None) -> MatrixReport:
    """Run every cell until one answer parses.

    ``connect(cell)`` must return an object with
    ``read(opcode, read_len) -> ReadResult`` and an optional ``close()``.
    The callable is the only place that touches hardware; the unit tests pass a
    fake one, so this function itself performs no I/O.
    """
    cells = tuple(cells)
    queries = validate_queries(queries)
    attempts: list[AttemptResult] = []
    winning: AttemptResult | None = None
    for cell in cells:
        session = connect(cell)
        try:
            results = _run_cell(session, queries)
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        attempt = AttemptResult(cell, results, _cell_verdict(results))
        attempts.append(attempt)
        if attempt.verdict == VALID:
            winning = attempt
            break
        if stop_on_nonzero and attempt.verdict in (NONZERO, MALFORMED):
            # A non-zero but unparseable cell means the bus worked in this
            # setting; repeating the other polarities would not add information.
            break
    return MatrixReport(cells=cells, attempts=attempts, queries=queries,
                        timeout_ms=int(timeout_ms), context=dict(context or {}),
                        winning=winning)


# ---------------------------------------------------------------------------
# Real transport wiring (only the probe constructs this)
# ---------------------------------------------------------------------------

class SpiMatrixSession:
    """One matrix cell on the real GPIO/SPI backends.

    The reset line is pulsed only for the two polarity combos; the ``hold``
    control never requests it.  The interrupt line is re-requested for every
    cell because the edge is part of the matrix.  ``SpiTransport`` keeps the
    read allowlist and the destructive-opcode/selector gate.
    """

    def __init__(self, cell: MatrixCell, *, spidev_path: str,
                 speed_hz: int | None = None, mode: int | None = None,
                 bits_per_word: int | None = None,
                 gpio_chip: str | None = None,
                 reset_pin: int | None = None, irq_pin: int | None = None,
                 irq_active_low: bool = False,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 gpio_backend: str | None = "auto",
                 sleep=time.sleep):
        self.cell = cell
        self.spidev_path = spidev_path
        self.speed_hz = speed_hz
        self.mode = mode
        self.bits_per_word = bits_per_word
        self.gpio_chip = gpio_chip
        self.reset_pin = reset_pin
        self.irq_pin = irq_pin
        self.irq_active_low = bool(irq_active_low)
        self.timeout_ms = int(timeout_ms)
        self.gpio_backend = gpio_backend
        self._sleep = sleep
        self._reset = None
        self._irq = None
        self._transport = None

    def open(self) -> "SpiMatrixSession":
        if self._transport is not None:
            return self
        if self.cell.pulse:
            if self.reset_pin is None:
                raise spi.SpiError("this matrix cell pulses a reset line, but "
                                   "no reset pin was configured")
            self._reset = spi.GpioResetLine(
                self.reset_pin, chip=self.gpio_chip,
                active_low=bool(self.cell.active_low),
                backend=self.gpio_backend)
            self._reset.pulse()
        if self.cell.settle_ms:
            self._sleep(self.cell.settle_ms / 1000.0)
        if self.irq_pin is None:
            raise spi.SpiError("the matrix needs an interrupt line")
        self._irq = spi.GpioIrqWaiter(
            self.irq_pin, chip=self.gpio_chip, edge=self.cell.edge,
            active_low=self.irq_active_low, backend=self.gpio_backend)
        self._transport = spi.SpiTransport(
            self.spidev_path, speed_hz=self.speed_hz, mode=self.mode,
            bits_per_word=self.bits_per_word, allow_writes=True,
            read_allowlist=MATRIX_ALLOWED_OPS, irq=self._irq)
        return self

    def read(self, opcode: int, read_len: int) -> ReadResult:
        try:
            self.open()
        except (spi.SpiError, OSError) as exc:
            return ReadResult(b"", f"error: {exc}", "error")
        plan = spi.build_plan(opcode, read_len=read_len,
                              timeout_ms=self.timeout_ms,
                              total_ms=self.timeout_ms)
        try:
            data = self._transport.execute(plan)
        except spi.SpiTimeout as exc:
            # The transfer already happened; execute() failed only because the
            # interrupt never fired.  Keep the bytes - a valid answer without
            # an IRQ is still a success (and an important result).
            return ReadResult(self._received(), f"timeout: {exc}", "none")
        except spi.SpiError as exc:
            return ReadResult(self._received(), f"error: {exc}", "error")
        irq = "fired" if self._transport.last_irq is not None else "unknown"
        return ReadResult(data, "", irq)

    def _received(self) -> bytes:
        io = getattr(self._transport, "_io", None)
        message = getattr(io, "last_message", ()) if io is not None else ()
        return b"".join(message)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._reset is not None:
            try:
                self._reset.close()
            finally:
                self._reset = None


def write_report(report: MatrixReport, path: str) -> None:
    """Write the matrix table as stable, pretty JSON."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
