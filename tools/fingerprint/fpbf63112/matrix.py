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
    phase: int = 0
    arg: int = 0


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

# ---------------------------------------------------------------------------
# Write-shape experiment (--try-write-shape)
# ---------------------------------------------------------------------------

WRITE_SHAPE_SCHEMA = 1

#: One short timeout per cell: the experiment must stay bounded even though it
#: sends many cells.  400 ms is long enough for a 64-byte transfer plus the IRQ
#: wait at 4 MHz and short enough that an all-fail table is still quick.
WRITE_SHAPE_TIMEOUT_MS = 400

#: The read-only commands and their recovered layout lengths, in escalation
#: order.  ``read_len`` is the recovered layout *or* 64 bytes, because the
#: current shifted answer might simply need a wider window.
WRITE_SHAPE_QUERIES: tuple[MatrixQuery, ...] = (
    MatrixQuery("fw", 0xA0, 7, 64, "firmware version / project"),
    MatrixQuery("scheme", 0xA1, 64, 64, "module scheme table"),
    MatrixQuery("info", 0xC1, 9, 64, "sensor geometry + flags"),
)

WRITE_SHAPE_ALLOWED_OPS: frozenset[int] = frozenset(
    q.opcode for q in WRITE_SHAPE_QUERIES)

#: Selector candidates: the recovered read selector (bit 0 clear) and the
#: "read flag lives in bit 0" hypothesis (bit 0 set).
WRITE_SHAPE_SELECTORS: tuple[tuple[str, int], ...] = (
    ("read", 0),
    ("read-bit0", 1),
)

#: Bytes written after the selector.  ``phase`` is the command's phase byte
#: preceded by two zero bytes (the "zeros + phase" hypothesis); for our
#: phase-0 reads it is byte-for-byte the 3-zero padding, but it stays a
#: distinct row so the table shows the hypothesis was tried.
WRITE_SHAPE_PADDINGS: tuple[str, ...] = ("0", "1", "2", "3", "4", "phase")

#: Additional verdicts: a layout that parses only after a non-zero offset, and
#: a 0xAA frame whose CRC-32 does not match.
SHIFTED = "shifted"
CRC_MISMATCH = "crc-mismatch"

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
# Write-shape planning and validation
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class WriteShapeCell:
    """One selector x padding x read-length cell for one read command."""

    index: int
    query: MatrixQuery
    selector_variant: str
    selector_bit0: int
    padding_label: str
    padding: bytes
    read_len: int
    selector: int

    def label(self) -> str:
        return (f"{self.index}: op=0x{self.query.opcode:02x} "
                f"selector=0x{self.selector:02x} "
                f"padding={self.padding_label} read={self.read_len}B")


def _write_shape_padding(query: MatrixQuery, label: str) -> bytes:
    if label == "phase":
        # "zeros + phase": two zero bytes, then the command's phase byte.
        return b"\x00\x00" + bytes((query.phase & 0xFF,))
    try:
        count = int(label)
    except ValueError:
        raise ValueError(f"unknown write-shape padding {label!r}") from None
    if not 0 <= count <= 8:
        raise ValueError("write-shape padding must be 0..8 bytes")
    return b"\x00" * count


def validate_write_shape_queries(queries) -> tuple[MatrixQuery, ...]:
    """Refuse any write-shape query outside the read-only allowlist."""
    queries = validate_queries(queries)
    for query in queries:
        if query.opcode not in WRITE_SHAPE_ALLOWED_OPS:
            raise MatrixRefused(
                f"opcode 0x{query.opcode:02x} is not allowlisted for the "
                f"write-shape experiment; allowed: "
                + ", ".join(f"0x{op:02x}"
                            for op in sorted(WRITE_SHAPE_ALLOWED_OPS)))
    return queries


def plan_write_shape(*, queries=WRITE_SHAPE_QUERIES,
                     selectors=WRITE_SHAPE_SELECTORS,
                     paddings=WRITE_SHAPE_PADDINGS) -> tuple[WriteShapeCell, ...]:
    """Return the deterministic write-shape matrix.

    Order: command (0xA0, then 0xA1, 0xC1) x selector (bit 0 clear, then set) x
    padding (0..4, then phase) x read length (layout, then 64).  That puts the
    most likely correct shape first and keeps a successful early stop short.
    """
    queries = validate_write_shape_queries(queries)
    selectors = tuple(selectors)
    paddings = tuple(paddings)
    if not selectors:
        raise ValueError("the write-shape matrix needs a selector candidate")
    if not paddings:
        raise ValueError("the write-shape matrix needs a padding candidate")
    for name, bit0 in selectors:
        if name not in ("read", "read-bit0") or bit0 not in (0, 1):
            raise ValueError(f"unknown selector variant {name!r}")
    cells = []
    for query in queries:
        read_lens = []
        for length in (query.layout_len, 64):
            if length > 0 and length not in read_lens:
                read_lens.append(length)
        for name, bit0 in selectors:
            selector = spi.selector(query.opcode) | bit0
            for padding_label in paddings:
                padding = _write_shape_padding(query, padding_label)
                for read_len in read_lens:
                    cells.append(WriteShapeCell(
                        len(cells) + 1, query, name, bit0, padding_label,
                        padding, read_len, selector))
    return tuple(cells)


@dataclasses.dataclass(frozen=True)
class ShapeCheck:
    """The explicit interpretation of one write-shape cell's answer."""

    verdict: str
    detail: str
    framing: str | None = None       # "frame" or "bare"
    payload: bytes = b""
    aa_offset: int | None = None
    aa_offsets: tuple[int, ...] = ()
    crc: str = "none"                # "ok", "crc", "short", "bad-length", "none"
    candidate_offset: int | None = None
    candidate_detail: str = ""
    candidate_offsets: tuple[int, ...] = ()
    fields: dict | None = None

    @property
    def valid(self) -> bool:
        return self.verdict == VALID

    @property
    def shifted(self) -> bool:
        return self.verdict == SHIFTED


def _frame_at(data: bytes, offset: int):
    """Try one 0xAA frame at *offset*; return ``(frame, verdict)``."""
    if len(data) - offset < protocol.FRAME_OVERHEAD:
        return None, "short"
    need = struct.unpack_from("<I", data, offset + 1)[0] + protocol.FRAME_OVERHEAD
    if need < protocol.FRAME_MIN_SIZE:
        return None, "bad-length"
    if offset + need > len(data):
        return None, "short"
    try:
        return protocol.parse_frame(data[offset:offset + need],
                                    require_magic=False), "ok"
    except protocol.CRCMismatchError:
        return None, "crc"
    except protocol.FrameError:
        return None, "bad-length"


def _aa_offsets(data: bytes, limit: int = 8) -> tuple[int, ...]:
    offsets = []
    pos = 0
    while len(offsets) < limit:
        found = data.find(b"\xaa", pos)
        if found < 0:
            break
        offsets.append(found)
        pos = found + 1
    return tuple(offsets)


def _all_byte(data: bytes, value: int) -> bool:
    return bool(data) and all(byte == value for byte in data)


def _candidate_offsets(opcode: int, data: bytes) -> tuple[int, ...]:
    """Every offset where the recovered layout's *plausible field* appears.

    This is diagnostic, not success: a shifted read is exactly the state that
    must be explained, so each cell reports where the field actually is.
    """
    found = []
    n = len(data)
    if opcode == 0xA0:
        for i in range(0, n - 6):
            project = data[i + 1]
            if project not in PROJECTS:
                continue
            version = data[i + 3:i + 7]
            if not any(version) or _all_byte(version, 0xFF):
                continue
            found.append(i)
    elif opcode == 0xA1:
        for i in range(0, n - 7):
            word = struct.unpack_from("<H", data, i)[0]
            if not 1 <= word <= 0x1000:
                continue
            found.append(i)
    elif opcode == 0xC1:
        for i in range(0, n - 8):
            width, height = struct.unpack_from("<HH", data, i + 4)
            if 1 <= width <= 4096 and 1 <= height <= 4096:
                found.append(i)
    return tuple(found[:8])


def _direct_layout(opcode: int, payload: bytes):
    """Validate the recovered layout anchored at offset 0."""
    if opcode == 0xA0:
        if len(payload) < 7:
            return None, f"fw body is {len(payload)}B; need 7"
        project = payload[1]
        if project not in PROJECTS:
            return None, (f"project byte 0x{project:02x} is not in the "
                          f"recovered table")
        version = ".".join(str(b) for b in payload[3:7])
        return ({"state": payload[0], "project": project,
                 "project_name": PROJECTS[project], "version": version},
                f"fw={version} project={PROJECTS[project]}")
    if opcode == 0xA1:
        if len(payload) < 8:
            return None, f"scheme body is {len(payload)}B; need at least 8"
        word = struct.unpack_from("<H", payload, 0)[0]
        if not 1 <= word <= 0x1000:
            return None, (f"first word 0x{word:04x} is not a plausible "
                          f"length/version (1..4096)")
        if _all_byte(payload, 0x00) or _all_byte(payload, 0xFF):
            return None, "scheme table is all zeros/0xFF"
        return ({"table_len": len(payload), "first_word": word},
                f"scheme table {len(payload)}B first word 0x{word:04x}")
    if opcode == 0xC1:
        if len(payload) < 9:
            return None, f"info body is {len(payload)}B; need 9"
        width, height = struct.unpack_from("<HH", payload, 4)
        if not 1 <= width <= 4096 or not 1 <= height <= 4096:
            return None, (f"geometry {width}x{height} is outside 1..4096")
        return ({"width": width, "height": height, "flags": payload[8]},
                f"sensor={width}x{height} flags=0x{payload[8]:02x}")
    raise MatrixRefused(f"opcode 0x{opcode:02x} has no write-shape layout")


def analyze_shape_answer(opcode: int, data: bytes, *,
                         layout_len: int | None = None) -> ShapeCheck:
    """Check one write-shape answer: framing, CRC, layout and shift.

    Success (:data:`VALID`) is either a complete 0xAA frame whose L field and
    CRC-32 validate at any offset, or the recovered bare layout anchored at
    read offset 0.  A layout that only appears at a non-zero offset is
    :data:`SHIFTED`: it is reported (with the offset) but does not stop the
    experiment, because finding the shape that brings it to offset 0 is the
    point.
    """
    if opcode not in WRITE_SHAPE_ALLOWED_OPS:
        raise MatrixRefused(
            f"opcode 0x{opcode:02x} is not a write-shape query; allowed: "
            + ", ".join(f"0x{op:02x}" for op in sorted(WRITE_SHAPE_ALLOWED_OPS)))
    query = {q.opcode: q for q in WRITE_SHAPE_QUERIES}[opcode]
    if layout_len is None:
        layout_len = query.layout_len
    data = bytes(data)
    if not data:
        return ShapeCheck(EMPTY, "no bytes were read back")

    offsets = _aa_offsets(data)
    crc = "none"
    frames = []
    for offset in offsets:
        frame, reason = _frame_at(data, offset)
        if frame is None:
            if crc == "none":
                crc = reason
        else:
            crc = "ok"
            frames.append((offset, frame))
    aa_offset = offsets[0] if offsets else None

    candidates = _candidate_offsets(opcode, data)
    candidate_offset = candidates[0] if candidates else None
    candidate_detail = (f"plausible field at read offset {candidate_offset}"
                        if candidate_offset is not None else "")

    if frames:
        offset, frame = frames[0]
        fields = {"frame_offset": offset, "opcode": frame.opcode,
                  "phase": frame.phase, "status": frame.status,
                  "crc": f"0x{frame.crc:08x}"}
        detail = (f"0xAA frame at offset {offset}: L={frame.length} "
                  f"crc=0x{frame.crc:08x}")
        return ShapeCheck(VALID, detail, "frame", frame.body, aa_offset,
                          offsets, "ok", offset, detail, (), fields)

    # A stream that starts with the frame magic but does not parse is a framing
    # failure, not a bare layout: do not mine its payload bytes for a
    # coincidental project/geometry field.
    if data[0] == protocol.SOF:
        verdict = CRC_MISMATCH if crc == "crc" else (
            SHORT if crc == "short" else MALFORMED)
        return ShapeCheck(verdict, f"0xAA at offset 0 but no valid frame "
                                   f"(crc={crc})", "frame", b"", aa_offset,
                          offsets, crc, None, "", ())

    if _all_byte(data, 0x00):
        return ShapeCheck(ZEROS, f"{len(data)}B of 0x00", "bare", data,
                          aa_offset, offsets, crc, candidate_offset,
                          candidate_detail, candidates)
    if _all_byte(data, 0xFF):
        return ShapeCheck(ALL_FF, f"{len(data)}B of 0xFF", "bare", data,
                          aa_offset, offsets, crc, candidate_offset,
                          candidate_detail, candidates)
    if len(data) < layout_len:
        return ShapeCheck(SHORT, f"{len(data)}B read; layout needs "
                                 f"{layout_len}B", "bare", data, aa_offset,
                          offsets, crc, candidate_offset, candidate_detail,
                          candidates)

    payload = data[:layout_len]
    fields, detail = _direct_layout(opcode, payload)
    if fields is not None:
        return ShapeCheck(VALID, detail, "bare", payload, aa_offset, offsets,
                          crc, 0, "layout anchored at read offset 0",
                          candidates, fields)
    if candidates:
        return ShapeCheck(SHIFTED,
                          f"{detail}; {candidate_detail}",
                          "bare", payload, aa_offset, offsets, crc,
                          candidate_offset, candidate_detail, candidates)
    return ShapeCheck(MALFORMED, detail, "bare", payload, aa_offset, offsets,
                      crc, candidate_offset, candidate_detail, candidates)


# ---------------------------------------------------------------------------
# Write-shape results and runner
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ShapeAttempt:
    cell: WriteShapeCell
    read: ReadResult
    check: ShapeCheck

    @property
    def verdict(self) -> str:
        return self.check.verdict

    def to_dict(self) -> dict:
        return {
            "index": self.cell.index,
            "command": self.cell.query.name,
            "opcode": f"0x{self.cell.query.opcode:02x}",
            "selector_variant": self.cell.selector_variant,
            "selector": f"0x{self.cell.selector:02x}",
            "padding": self.cell.padding_label,
            "padding_hex": self.cell.padding.hex(" "),
            "read_len": self.cell.read_len,
            "verdict": self.verdict,
            "valid": self.check.valid,
            "framing": self.check.framing,
            "bytes": len(self.read.data),
            "hex": self.read.data.hex(" "),
            "detail": self.check.detail,
            "aa_offset": self.check.aa_offset,
            "aa_offsets": list(self.check.aa_offsets),
            "crc": self.check.crc,
            "candidate_offset": self.check.candidate_offset,
            "candidate_detail": self.check.candidate_detail,
            "candidate_offsets": list(self.check.candidate_offsets),
            "fields": self.check.fields,
            "error": self.read.error,
            "irq": self.read.irq,
        }


@dataclasses.dataclass
class WriteShapeReport:
    """The complete write-shape result, printable and JSON-serialisable."""

    cells: tuple[WriteShapeCell, ...]
    attempts: list[ShapeAttempt]
    queries: tuple[MatrixQuery, ...]
    timeout_ms: int
    context: dict
    winning: ShapeAttempt | None = None

    @property
    def outcome(self) -> str:
        if self.winning is not None:
            return "valid-answer"
        if any(attempt.check.shifted for attempt in self.attempts):
            return "shifted-only"
        return "no-valid-answer"

    @property
    def verdict(self) -> str:
        total = len(self.cells)
        if self.winning is not None:
            cell = self.winning.cell
            return (f"VALID write shape at cell {cell.index}/{total} "
                    f"({cell.query.name} op=0x{cell.query.opcode:02x} "
                    f"selector=0x{cell.selector:02x} "
                    f"padding={cell.padding_label} read={cell.read_len}B): "
                    f"{self.winning.check.detail}")
        counts = _counts_text(self.attempts)
        shifted = [a for a in self.attempts if a.check.shifted]
        head = (f"no valid write shape in {len(self.attempts)}/{total} cells"
                if len(self.attempts) < total else
                f"all {total} write-shape cells failed")
        if shifted:
            mapping = _shift_summary(self.attempts)
            return f"{head}: {counts}; {mapping}"
        return f"{head}: {counts}"

    def to_dict(self) -> dict:
        return {
            "schema": WRITE_SHAPE_SCHEMA,
            "tool": "oxp3-fp-probe --try-write-shape",
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
                "read_lens": _read_lens(q),
            } for q in self.queries],
            "cells": [attempt.to_dict() for attempt in self.attempts],
            "winning": self.winning.to_dict() if self.winning else None,
            "shift_summary": _shift_rows(self.attempts),
        }

    def render_table(self) -> list[str]:
        lines = ["#  cmd sel  pad   rd  first 16 bytes"
                 "                    AA  crc   cand verdict"]
        for attempt in self.attempts:
            data = attempt.read.data
            shown = data[:16].hex(" ") if data else "(none)"
            if len(data) > 16:
                shown += f" (+{len(data) - 16}B)"
            aa = ("-" if attempt.check.aa_offset is None
                  else str(attempt.check.aa_offset))
            cand = ("-" if attempt.check.candidate_offset is None
                    else str(attempt.check.candidate_offset))
            lines.append(
                f"{attempt.cell.index:>2} {attempt.cell.query.name:<4} "
                f"0x{attempt.cell.selector:02x} {attempt.cell.padding_label:<5} "
                f"{attempt.cell.read_len:>3} {shown:<48} {aa:>3} "
                f"{attempt.check.crc:<5} {cand:>4} {attempt.verdict}")
        return lines


def _read_lens(query: MatrixQuery) -> list[int]:
    lengths = []
    for length in (query.layout_len, 64):
        if length > 0 and length not in lengths:
            lengths.append(length)
    return lengths


def _shift_rows(attempts) -> list[dict]:
    groups: dict[tuple, list] = {}
    for attempt in attempts:
        key = (attempt.cell.query.name, attempt.cell.query.opcode,
               attempt.cell.selector_variant, attempt.cell.read_len)
        groups.setdefault(key, []).append({
            "padding": attempt.cell.padding_label,
            "offset": attempt.check.candidate_offset,
        })
    return [{"command": key[0], "opcode": f"0x{key[1]:02x}",
             "selector_variant": key[2], "read_len": key[3],
             "candidates": rows}
            for key, rows in groups.items()]


def _shift_summary(attempts) -> str:
    parts = []
    for row in _shift_rows([a for a in attempts if a.check.shifted]):
        offsets = ", ".join(
            f"{item['padding']}:{item['offset']}"
            if item["offset"] is not None else f"{item['padding']}:-"
            for item in row["candidates"])
        parts.append(f"{row['opcode']} {row['selector_variant']} "
                     f"read={row['read_len']} ({offsets})")
    return "candidate offsets by padding " + "; ".join(parts[:4]) if parts else ""


def run_write_shape(cells, *, connect, queries=WRITE_SHAPE_QUERIES,
                    timeout_ms: int = WRITE_SHAPE_TIMEOUT_MS,
                    context: dict | None = None) -> WriteShapeReport:
    """Run every write-shape cell until one answer is :data:`VALID`.

    ``connect(cell)`` returns an object with
    ``read(opcode, read_len, selector_variant=..., padding=..., timeout_ms=...)``
    and an optional ``close()``.  Only that callable touches hardware; the
    unit tests inject a fake, so this function performs no I/O itself.
    """
    cells = tuple(cells)
    queries = validate_write_shape_queries(queries)
    attempts: list[ShapeAttempt] = []
    winning: ShapeAttempt | None = None
    for cell in cells:
        session = connect(cell)
        try:
            try:
                result = session.read(
                    cell.query.opcode, cell.read_len,
                    selector_variant=cell.selector_variant,
                    padding=cell.padding, timeout_ms=timeout_ms)
            except (spi.SpiError, OSError) as exc:
                result = ReadResult(b"", f"error: {exc}")
            if not isinstance(result, ReadResult):
                result = ReadResult(bytes(getattr(result, "data", b"")),
                                    str(getattr(result, "error", "")),
                                    str(getattr(result, "irq", "")))
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        data = bytes(result.data or b"")
        error = result.error or ""
        if error and not data:
            check = ShapeCheck(TIMEOUT if error.startswith("timeout")
                               else ERROR, error)
        else:
            check = analyze_shape_answer(cell.query.opcode, data)
        attempt = ShapeAttempt(cell, ReadResult(data, error, result.irq),
                               check)
        attempts.append(attempt)
        if check.valid:
            winning = attempt
            break
    return WriteShapeReport(cells, attempts, queries,
                            int(timeout_ms), dict(context or {}), winning)


class SpiWriteShapeSession:
    """One write-shape cell on the real GPIO/SPI backends.

    The reset line is pulsed once per cell with the owner-supplied polarity,
    then the settle delay is applied.  The IRQ waiter uses the owner-supplied
    edge.  :class:`~fpbf63112.spi.SpiTransport` still enforces the read
    allowlist and refuses destructive opcodes by opcode and selector.
    """

    def __init__(self, cell: WriteShapeCell, *, spidev_path: str,
                 reset_active_low: bool = False, settle_ms: int = 10,
                 edge: str = "rising", speed_hz: int | None = None,
                 mode: int | None = None,
                 bits_per_word: int | None = None,
                 gpio_chip: str | None = None,
                 reset_pin: int | None = None, irq_pin: int | None = None,
                 irq_active_low: bool = False,
                 timeout_ms: int = WRITE_SHAPE_TIMEOUT_MS,
                 gpio_backend: str | None = "auto",
                 sleep=time.sleep):
        self.cell = cell
        self.spidev_path = spidev_path
        self.reset_active_low = bool(reset_active_low)
        self.settle_ms = int(settle_ms)
        self.edge = edge
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

    def open(self) -> "SpiWriteShapeSession":
        if self._transport is not None:
            return self
        if self.reset_pin is None:
            raise spi.SpiError("the write-shape experiment pulses the reset "
                               "line, but no reset pin was configured")
        self._reset = spi.GpioResetLine(
            self.reset_pin, chip=self.gpio_chip,
            active_low=self.reset_active_low, backend=self.gpio_backend)
        self._reset.pulse()
        if self.settle_ms:
            self._sleep(self.settle_ms / 1000.0)
        if self.irq_pin is None:
            raise spi.SpiError("the write-shape experiment needs an interrupt "
                               "line")
        self._irq = spi.GpioIrqWaiter(
            self.irq_pin, chip=self.gpio_chip, edge=self.edge,
            active_low=self.irq_active_low, backend=self.gpio_backend)
        self._transport = spi.SpiTransport(
            self.spidev_path, speed_hz=self.speed_hz, mode=self.mode,
            bits_per_word=self.bits_per_word, allow_writes=True,
            read_allowlist=WRITE_SHAPE_ALLOWED_OPS, irq=self._irq)
        return self

    def read(self, opcode: int, read_len: int, *,
             selector_variant: str = "read", padding: bytes = b"",
             timeout_ms: int | None = None) -> ReadResult:
        try:
            self.open()
        except (spi.SpiError, OSError) as exc:
            return ReadResult(b"", f"error: {exc}", "error")
        limit = int(timeout_ms if timeout_ms is not None else self.timeout_ms)
        plan = spi.build_shape_plan(
            opcode, selector_variant=selector_variant, padding=padding,
            read_len=read_len, timeout_ms=limit, total_ms=limit)
        try:
            data = self._transport.execute(plan)
        except spi.SpiTimeout as exc:
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


def write_shape_report(report: WriteShapeReport, path: str) -> None:
    """Write the write-shape table as stable, pretty JSON."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")


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
    order = [VALID, SHIFTED, CRC_MISMATCH, ZEROS, ALL_FF, TIMEOUT, ERROR,
             SHORT, MALFORMED, NONZERO, EMPTY]
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
