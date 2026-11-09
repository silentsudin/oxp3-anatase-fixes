"""Locate the ``_CRS`` resource template of the fingerprint device in raw AML.

Why this exists
---------------
The root-time runbook (``tools/fingerprint/oxp3-fp-session.sh``) has to turn
``/sys/firmware/acpi/tables/DSDT`` into the byte buffer that
:mod:`fpbf63112.acpi` parses.  The procedure in
``docs/hw/fingerprint-transport.md`` §9 suggested ``iasl -d`` and copying the
``ResourceTemplate ()`` bytes out of the disassembly by hand.  That is both
error-prone and impossible on a host without ``iasl``: this host has
``acpica-tools`` absent, and the ASL text is a *decoded* form (named enums,
implicit defaults) that would have to be re-encoded before a byte parser can
look at it.

The byte buffer is already in the DSDT.  In AML, a ``ResourceTemplate ()``
inside ``Return (...)`` is

    A4            ReturnOp
    11            BufferOp
    <PkgLength>   length of the buffer object, including this field
    <size>        an integer term (0x0A/0x0B/0x0C prefix, or 0x00/0x01)
    <bytes>       the resource descriptors, usually ending with 79 00

and a method is

    14 <PkgLength> "_CRS" <flags> <term list>

so a bounded scan for the ``_CRS`` NameSeg, a forward look for ``Return`` +
``Buffer`` and :func:`fpbf63112.acpi.parse_resource_template` as the validator
is enough.  This module is that scan.  It never executes or writes anything,
allocates nothing proportional to the table beyond the bytes it returns, and
every candidate it reports has already been parsed by :mod:`fpbf63112.acpi`
and required to contain a ``SpiSerialBus`` descriptor - an unparseable or
non-SPI buffer is not reported at all.

Scope
-----
Only ``_CRS`` methods that return a buffer are considered.  A ``_CRS`` that
returns a named object, builds the template conditionally, or lives in an SSDT
that was not scanned is *not* found; the caller reports "no candidate" and the
runbook falls back to ``--crs FILE`` / a pasted hex dump.  That is deliberate:
guessing is worse than saying "not found".
"""

from __future__ import annotations

import dataclasses
import hashlib

from . import acpi

#: ``NameSeg`` bytes we correlate a ``_CRS`` with.  The ACPI device is
#: ``\_SB.PC00.SPI1.FPRT`` with ``_HID`` ``BF63112``; the ``_HID`` string and
#: the device name both appear ahead of the method in practice.
DEVICE_MARKERS: tuple[bytes, ...] = (b"FPRT", b"BF63112")

#: The controller the Linux SPI device is actually instanced on.  This host
#: has a second, unpopulated ``\_SB.PC00.SPI2.FPRT`` node (docs/hw/
#: fingerprint.md), so "the FPRT device" alone is not unique: the candidate
#: whose enclosing name mentions SPI1 is the one whose ``physical_node`` leads
#: to ``pxa2xx-spi.6`` / ``spi0``.
CONTROLLER_MARKERS: tuple[bytes, ...] = (b"SPI1",)

#: How far a ``_CRS`` may be from its device marker and still count as scoped.
SCOPE_WINDOW = 4096

#: How far after the ``_CRS`` NameSeg the ``Return(Buffer)`` may start.
FORWARD_WINDOW = 96

#: A resource template is at most a few hundred bytes; cap what we will hand
#: to the parser so a corrupt PkgLength cannot make us read the whole table.
MAX_TEMPLATE_BYTES = 4096

_REQUIRED_OP = 0xA4     # ReturnOp
_BUFFER_OP = 0x11       # BufferOp
_NAMESEG_CRS = b"_CRS"

_INT_PREFIXES = {
    0x0A: 1,            # BytePrefix
    0x0B: 2,            # WordPrefix
    0x0C: 4,            # DWordPrefix
    0x0E: 8,            # QWordPrefix
}


class AmlError(ValueError):
    """The AML buffer is truncated where a field was needed."""


@dataclasses.dataclass(frozen=True)
class CrsCandidate:
    """One ``_CRS`` buffer that parsed as a ``SpiSerialBus`` template.

    ``offset`` is the offset of the ``_CRS`` NameSeg in the scanned buffer,
    ``buffer_offset`` the offset of the ``BufferOp``, ``payload`` the exact
    resource-template bytes (what ``oxp3-fp-probe.py acpi_info --crs`` reads).
    ``marker``/``distance`` say which device marker was nearest ahead of the
    method (``None``/``None`` when the candidate is unscoped).
    """

    offset: int
    buffer_offset: int
    payload: bytes
    marker: bytes | None
    distance: int | None
    controller: bytes | None = None
    controller_distance: int | None = None

    @property
    def scoped(self) -> bool:
        return self.marker is not None

    @property
    def on_controller(self) -> bool:
        return self.controller is not None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def size(self) -> int:
        return len(self.payload)

    def describe(self) -> str:
        where = (f"{self.marker.decode('ascii', 'replace')}+{self.distance} B"
                 if self.marker is not None else "unscoped")
        if self.controller is not None:
            where += (f", {self.controller.decode('ascii', 'replace')}"
                      f"+{self.controller_distance} B")
        return (f"_CRS @{self.offset:#x} buffer @{self.buffer_offset:#x} "
                f"{self.size} B sha256 {self.sha256[:16]} ({where})")


# ---------------------------------------------------------------------------
# AML primitives (the two this scan needs)
# ---------------------------------------------------------------------------

def read_pkg_length(buf: bytes, off: int) -> tuple[int, int]:
    """Decode an AML ``PkgLength`` at ``off``.

    Returns ``(total, next_offset)`` where ``total`` includes the length bytes
    themselves (the AML definition: the value covers the PkgLength field plus
    the object it introduces).  Raises :class:`AmlError` when truncated.
    """
    if off >= len(buf):
        raise AmlError(f"PkgLength at {off:#x} is past the end")
    lead = buf[off]
    follow = lead >> 6
    if follow == 0:
        return lead & 0x3F, off + 1
    if off + 1 + follow > len(buf):
        raise AmlError(f"PkgLength at {off:#x} needs {follow} more byte(s)")
    value = lead & 0x0F
    for index in range(follow):
        value |= buf[off + 1 + index] << (4 + 8 * index)
    return value, off + 1 + follow


def read_integer(buf: bytes, off: int) -> tuple[int | None, int]:
    """Decode a constant integer term at ``off``; ``(None, off)`` if unknown.

    Only constant forms are decoded (``ZeroOp``/``OneOp``/``Byte``/``Word``/
    ``DWord``/``QWord``).  A ``NameString``/expression size is reported as
    unknown so the caller can fall back to the package length.
    """
    if off >= len(buf):
        raise AmlError(f"integer at {off:#x} is past the end")
    op = buf[off]
    if op == 0x00:
        return 0, off + 1
    if op == 0x01:
        return 1, off + 1
    width = _INT_PREFIXES.get(op)
    if width is not None:
        if off + 1 + width > len(buf):
            raise AmlError(f"integer at {off:#x} needs {width} byte(s)")
        return int.from_bytes(buf[off + 1:off + 1 + width], "little"), off + 1 + width
    return None, off


def _parse_buffer(buf: bytes, op_off: int) -> tuple[bytes, int]:
    """Parse ``BufferOp`` at ``op_off``; return ``(payload, next_offset)``."""
    total, after_length = read_pkg_length(buf, op_off + 1)
    end = op_off + 1 + total
    if end > len(buf):
        raise AmlError(f"Buffer at {op_off:#x} claims past the end of the table")
    size, payload_start = read_integer(buf, after_length)
    payload_end = end if size is None else min(end, payload_start + size)
    if payload_end < payload_start:
        raise AmlError(f"Buffer at {op_off:#x} has a negative size")
    if payload_end - payload_start > MAX_TEMPLATE_BYTES:
        raise AmlError(
            f"Buffer at {op_off:#x} is {payload_end - payload_start} B, over "
            f"the {MAX_TEMPLATE_BYTES} B template cap")
    return buf[payload_start:payload_end], end


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

#: ``SPI1`` occurs both as a ``NameSeg`` and inside the SerialBus
#: resource-source string ``\_SB.PC00.SPI1``.  Only the former is a scope, so
#: a hit is rejected when the preceding byte is a name separator (``.`` or
#: ``\``) or the following byte continues a longer name.  The preceding byte
#: of a real ``NameSeg`` is arbitrary AML (a ``PkgLength`` byte, for example),
#: which is why the check looks at separators and the trailing side only.
_SEPARATORS = b".\\"
_TRAILING_NAME = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.")


def _is_nameseg(aml: bytes, index: int, marker: bytes) -> bool:
    """True when ``marker`` at ``index`` is a standalone AML ``NameSeg``/string."""
    if index > 0 and aml[index - 1] in _SEPARATORS:
        return False
    after_index = index + len(marker)
    if after_index < len(aml) and aml[after_index] in _TRAILING_NAME:
        return False
    return True


def _markers(aml: bytes, markers: tuple[bytes, ...] = DEVICE_MARKERS
             ) -> list[tuple[int, bytes]]:
    found: list[tuple[int, bytes]] = []
    for marker in markers:
        start = 0
        while True:
            index = aml.find(marker, start)
            if index < 0:
                break
            if _is_nameseg(aml, index, marker):
                found.append((index, marker))
            start = index + 1
    found.sort()
    return found


def _nearest(offset: int, markers: list[tuple[int, bytes]],
             prefer: bytes | None = None):
    """Nearest in-window marker of ``markers``; ``prefer`` wins when in range."""
    best = None
    for index, marker in markers:
        if index > offset:
            break
        distance = offset - index
        if distance > SCOPE_WINDOW:
            continue
        if prefer is not None and marker == prefer:
            return (marker, distance)
        if best is None or distance < best[1]:
            best = (marker, distance)
    return best if best is not None else (None, None)


def _scope_for(offset: int, markers: list[tuple[int, bytes]],
               controllers: list[tuple[int, bytes]]):
    """Nearest in-window scope markers around a ``_CRS``.

    Returns ``(device_marker, device_distance, controller, controller_distance)``.
    The ``_HID`` string sits inside the device (closer to the method) while
    ``FPRT`` is the device ``NameSeg``, which is the name the runbook reports
    and the operator recognises, so ``FPRT`` is preferred when in range.
    """
    device, distance = _nearest(offset, markers, prefer=b"FPRT")
    controller, controller_distance = _nearest(offset, controllers)
    return device, distance, controller, controller_distance


def _candidates_at(aml: bytes, offset: int) -> list[tuple[int, bytes]]:
    """Every ``Return(Buffer)`` looking object shortly after a ``_CRS``."""
    out: list[tuple[int, bytes]] = []
    after = offset + len(_NAMESEG_CRS)
    limit = min(len(aml), after + FORWARD_WINDOW)
    # Name (_CRS, Buffer (...)) - the buffer starts immediately.
    starts = []
    if after < len(aml) and aml[after] == _BUFFER_OP:
        starts.append(after)
    position = after
    while position < limit:
        index = aml.find(bytes([_REQUIRED_OP, _BUFFER_OP]), position, limit)
        if index < 0:
            break
        starts.append(index + 1)
        position = index + 2
    for start in starts:
        try:
            payload, _end = _parse_buffer(aml, start)
        except AmlError:
            continue
        out.append((start, payload))
    return out


def _validate(payload: bytes) -> bool:
    """Accept only a template that parses and contains the SPI connection."""
    try:
        template = acpi.parse_resource_template(payload)
    except acpi.AcpiError:
        return False
    if template.trailing.strip(b"\x00"):
        return False
    return template.spi is not None


def find_crs_candidates(aml: bytes) -> tuple[CrsCandidate, ...]:
    """Every SPI ``_CRS`` resource template in ``aml``, best candidate first.

    Ordering: device-scoped candidates by increasing distance to their marker,
    then unscoped candidates by offset.  Duplicate payloads are reported once.
    """
    aml = bytes(aml)
    markers = _markers(aml)
    controllers = _markers(aml, CONTROLLER_MARKERS)
    seen: dict[bytes, CrsCandidate] = {}
    position = 0
    while True:
        index = aml.find(_NAMESEG_CRS, position)
        if index < 0:
            break
        position = index + 1
        marker, distance, controller, controller_distance = _scope_for(
            index, markers, controllers)
        for buffer_offset, payload in _candidates_at(aml, index):
            if not _validate(payload):
                continue
            digest = hashlib.sha256(payload).digest()
            candidate = CrsCandidate(
                offset=index, buffer_offset=buffer_offset, payload=payload,
                marker=marker, distance=distance, controller=controller,
                controller_distance=controller_distance)
            previous = seen.get(digest)
            if previous is None or _rank(candidate) < _rank(previous):
                seen[digest] = candidate
    return tuple(sorted(seen.values(), key=_rank))


def _rank(candidate: CrsCandidate) -> tuple[int, int, int, int]:
    """Sort key: controller-scoped, then device-scoped, then distance, offset."""
    if candidate.on_controller:
        return (0, 0, candidate.controller_distance or 0, candidate.offset)
    if candidate.scoped:
        return (1, 0, candidate.distance or 0, candidate.offset)
    return (2, 1, 0, candidate.offset)


def select_candidate(candidates: tuple[CrsCandidate, ...]):
    """Return ``(candidate, ambiguous)`` for the best candidate, or ``(None, False)``.

    Ambiguous means two candidates rank equally (same scope kind, same
    distance) while differing in payload, so the runbook refuses to pick one
    and asks for ``--crs FILE`` instead of guessing.
    """
    if not candidates:
        return None, False
    first = candidates[0]
    rest = candidates[1:]
    ambiguous = any(_rank(other)[:3] == _rank(first)[:3] for other in rest)
    return first, ambiguous


# ---------------------------------------------------------------------------
# Convenience for callers that have a directory of ACPI tables
# ---------------------------------------------------------------------------

def scan_tables(paths) -> tuple[list[str], tuple[CrsCandidate, ...]]:
    """Scan each table path; return ``(scanned_names, candidates)``.

    A missing or unreadable table is skipped rather than raising, because the
    runbook must report which tables it looked at, not fail on a stray file.
    """
    scanned: list[str] = []
    candidates: list[CrsCandidate] = []
    for path in paths:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        scanned.append(str(path))
        candidates.extend(find_crs_candidates(data))
    return scanned, tuple(sorted(candidates, key=_rank))
