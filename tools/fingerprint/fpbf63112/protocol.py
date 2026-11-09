"""BF63112 fingerprint protocol: frames, CRC, image reassembly, retry state machine.

This module is a clean, dependency-free (stdlib only) reimplementation of the
wire protocol recovered from Betterlife's Windows UMDF driver
``BetterlifeFingerprintDevice.dll`` 9.39.15.26 for the sensor that ACPI
enumerates as ``ACPI\\BF63112`` (ONEXPLAYER 3, Intel SPI).  The byte-level
derivation and the evidence for every constant here are in
``docs/hw/fingerprint.md``; this file is our own code.

What is *proven* (matches the Windows driver byte for byte):

* the host frame layout ``AA | L:u32 | 16 x 0 | 4C op phase 00 arg:u16
  len:u32 data | pad16 | CRC-32``;
* ``L = 20 + align16(10 + data_len)`` and the CRC-32/ISO-HDLC over the whole
  frame including the ``0xAA`` (the driver uses the reflected 0xEDB88320
  polynomial, init 0xFFFFFFFF, final XOR - exactly ``zlib.crc32``);
* the opcode table (``0xA1``-``0xFF``);
* the GET_IMG three-phase handshake and the *global* 8-bit image sum.

What is inferred or unknown, and therefore modelled conservatively here:

* the device->host answer header.  The 16 reserved bytes are the module's
  answer area; only the 4-byte ``4C op phase status`` block at offset 21 is
  observed.  A response frame is parsed with the same field offsets as a
  request, and both plausible "body start" interpretations are exposed
  (:attr:`Frame.body_from_status` and :attr:`Frame.body_from_arg`).
* whether GET_IMG chunks carry a *per-chunk* 8-bit sum in the status byte.
  ``docs/hw/fingerprint.md`` says there is only a global sum; the recovered
  background notes mention a per-chunk check.  :meth:`ImageAssembler.add`
  accepts an optional ``chunk_sum`` so a driver can feed either model once the
  hardware answers, without this module pretending to know.
* enrol/identify payloads, the SM3 session layer and template handles.

Nothing here touches hardware; :mod:`fpbf63112` is also used by the offline
tests in ``tools/fingerprint/tests``.
"""

from __future__ import annotations

import enum
import struct
import zlib
from dataclasses import dataclass, field

__all__ = [
    # constants
    "SOF", "CMD_MAGIC", "HDR_RESERVED", "CMD_BLOCK_FIXED", "ALIGN",
    "FRAME_OVERHEAD", "FRAME_MIN_SIZE", "MAX_DATA_LEN",
    "MAX_IMAGE_CHUNK", "TIMEOUTS_MS",
    # arithmetic
    "align16", "command_block_len", "length_field", "frame_size",
    # crc
    "crc32", "sum8",
    # build / parse
    "build_frame", "parse_frame", "scan_frames", "Frame", "FrameScanError",
    # tables
    "Command", "CommandInfo", "COMMANDS", "READ_ONLY_COMMANDS",
    "DESTRUCTIVE_COMMANDS", "command_info", "Phase",
    # image
    "ImageHeader", "ImageAssembler", "MAX_IMAGE_CHUNK",
    # session
    "Transaction", "TransactionState", "RetryPolicy",
    # errors
    "ProtocolError", "FrameError", "TruncatedFrameError", "BadStartByteError",
    "LengthMismatchError", "BadLengthFieldError", "BadDataLengthError",
    "CRCMismatchError", "BadMagicError", "ImageError", "ImageSizeMismatch",
    "ImageSumMismatch", "ChunkTooLargeError", "ChunkSumMismatch",
    "SessionError", "ResponseMismatchError",
]

# --------------------------------------------------------------------------
# Constants recovered from the Windows driver
# --------------------------------------------------------------------------

SOF = 0xAA                 # start-of-frame byte, host -> device
CMD_MAGIC = 0x4C           # 'L', first byte of the command block
HDR_RESERVED = 16          # zero bytes between the L field and the command block
CMD_BLOCK_FIXED = 10       # 4C op phase <status/0> arg:u16 len:u32
ALIGN = 16                 # command block is zero-padded to 16-byte multiples
FRAME_OVERHEAD = 5         # SOF + L field + CRC-32 (L excludes the CRC)

#: Field offsets inside a frame (all proven on the host->device side).
FRAME_OFF_LEN = 1          # L:u32 LE
FRAME_OFF_RESERVED = 5     # 16 zero bytes in a request
FRAME_OFF_MAGIC = 21       # 0x4C
FRAME_OFF_OPCODE = 22      # command byte
FRAME_OFF_PHASE = 23       # 0 start, 1 data, 2 finish, 3 cancel
FRAME_OFF_STATUS = 24      # 0x00 in a request; status/parameter in an answer
FRAME_OFF_ARG = 25         # arg:u16 LE
FRAME_OFF_DATA_LEN = 27    # data_len:u32 LE (payload length, repeated)
FRAME_OFF_DATA = 31        # payload
FRAME_MIN_SIZE = 41        # data_len == 0 -> 25 + align16(10) = 41 bytes

#: Largest chunk the Windows driver asks for in one GET_IMG transfer.
MAX_IMAGE_CHUNK = 0x1FCC    # 8140 bytes

#: Per-attempt / total timeouts (ms) the Windows driver uses.  Windows-side
#: values, *not* validated on Linux hardware; kept here so a retry policy and a
#: driver can share one source of truth.  ``cancel`` has its own bound.
TIMEOUTS_MS = {
    "send": 1000,
    "recv": 5000,
    "total": 2030,
    "image_total": 8000,
    "image_cancel_total": 10010,
    "cancel": 2000,
}

#: Highest payload we will build/parse.  The wire field is u32, but a userspace
#: tool has no reason to allocate gigabytes; 64 KiB matches the default
#: ``/sys/module/spidev/parameters/bufsiz`` (4096) several times over and still
#: covers a whole image in one frame.
MAX_DATA_LEN = 64 * 1024


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ProtocolError(Exception):
    """Base class for every protocol-level failure."""


class FrameError(ProtocolError):
    """A candidate frame is not a well-formed BF63112 frame."""


class TruncatedFrameError(FrameError):
    """Fewer bytes than the header/length field demands."""


class BadStartByteError(FrameError):
    """The frame does not start with :data:`SOF`."""


class LengthMismatchError(FrameError):
    """The L field disagrees with the actual buffer length."""


class BadLengthFieldError(FrameError):
    """L is not ``20 + align16(10 + data_len)``."""


class BadDataLengthError(FrameError):
    """The data_len field does not fit inside the aligned command block."""


class CRCMismatchError(FrameError):
    """The trailing CRC-32 does not match the frame body."""

    def __init__(self, expected: int, actual: int):
        super().__init__(
            f"CRC mismatch: computed 0x{expected:08x}, frame carries "
            f"0x{actual:08x}")
        self.expected = expected
        self.actual = actual


class BadMagicError(FrameError):
    """The command block does not start with :data:`CMD_MAGIC`."""


class ImageError(ProtocolError):
    """An image transfer (GET_IMG) failed."""


class ImageSizeMismatch(ImageError):
    """Assembled byte count differs from the announced total."""


class ImageSumMismatch(ImageError):
    """The global 8-bit image sum differs from the announced byte_sum."""


class ChunkTooLargeError(ImageError):
    """A chunk is larger than the per-transfer maximum."""


class ChunkSumMismatch(ImageError):
    """An optional per-chunk 8-bit sum did not match the chunk bytes."""


class SessionError(ProtocolError):
    """A transaction state machine was driven out of order."""


class ResponseMismatchError(SessionError):
    """An answer frame does not belong to the request in flight."""


# --------------------------------------------------------------------------
# Arithmetic: the layout rules of docs/hw/fingerprint.md
# --------------------------------------------------------------------------

def align16(n: int) -> int:
    """Round *n* up to a multiple of 16 (the command-block alignment)."""
    if n < 0:
        raise ValueError("cannot align a negative length")
    return (n + (ALIGN - 1)) & ~(ALIGN - 1)


def command_block_len(data_len: int) -> int:
    """Aligned size of ``4C op phase 00 arg len data``: ``align16(10 + len)``."""
    if data_len < 0:
        raise ValueError("negative data_len")
    return align16(CMD_BLOCK_FIXED + data_len)


def length_field(data_len: int) -> int:
    """Value written to the L field: ``20 + align16(10 + data_len)``."""
    return 4 + HDR_RESERVED + command_block_len(data_len)


def frame_size(data_len: int) -> int:
    """Total frame size in bytes: ``L + 5`` (SOF, L field, CRC)."""
    return FRAME_OVERHEAD + length_field(data_len)


# --------------------------------------------------------------------------
# CRC-32 and the image sum
# --------------------------------------------------------------------------

def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC exactly as the Windows driver appends it.

    Reflected polynomial ``0xEDB88320``, initial value ``0xFFFFFFFF``, final
    XOR - i.e. the same function as :func:`zlib.crc32`.  The CRC covers every
    byte before it, **including** the ``0xAA`` start byte.
    """
    return zlib.crc32(data) & 0xFFFFFFFF


def sum8(data: bytes) -> int:
    """8-bit running sum used by GET_IMG (``byte_sum`` in the phase-0 answer)."""
    return sum(data) & 0xFF


# --------------------------------------------------------------------------
# Command / opcode table
# --------------------------------------------------------------------------

class Phase(enum.IntEnum):
    """Command block phase byte.

    The four values are the Windows driver's start/data/finish/cancel, used by
    every multi-step command (enrol, identify, get-image, root key).
    """

    START = 0
    DATA = 1
    FINISH = 2
    CANCEL = 3


class Command(enum.IntEnum):
    """Command bytes (descriptor+0x10 in the Windows driver)."""

    QUERY_FIRMWARE_VER = 0xA1
    QUERY_MODULE_SCHEME_INFORMATION = 0xA2
    QUERY_ROOT_KEY = 0xB0
    ENROLL_FINGERPRINT = 0xB1
    IDENTIFY_FINGERPRINT = 0xB2
    REMOVE_FINGERPRINT = 0xB3
    QUERY_FINGERPRINT_COUNT = 0xB4
    GET_IMG_DATA = 0xB5
    DETECT_FINGERPRINT_PRESS = 0xB6
    SLEEP = 0xC0
    QUERY_MODULE_UID = 0xC1
    QUERY_SENSOR_INFO = 0xC2
    JTAG_CONTROL = 0xC3
    RESTORE_MCU = 0xD0
    UPDATE_FIRMWARE = 0xD1
    FW_WORK_MODE_SWITCH = 0xFF


@dataclass(frozen=True)
class CommandInfo:
    """One row of the recovered command table, plus our safety classification."""

    opcode: int
    name: str
    summary: str
    read_only: bool
    destructive: bool
    multi_phase: bool = False


COMMANDS: dict[Command, CommandInfo] = {
    Command.QUERY_FIRMWARE_VER: CommandInfo(
        0xA1, "CMD_QUERY_FIRMWARE_VER",
        "project code + 4-byte firmware version", True, False),
    Command.QUERY_MODULE_SCHEME_INFORMATION: CommandInfo(
        0xA2, "CMD_QUERY_MODULE_SCHEME_INFORMATION",
        "module scheme/configuration information", True, False),
    Command.QUERY_ROOT_KEY: CommandInfo(
        0xB0, "CMD_QUERY_ROOT_KEY",
        "chunked root-key write (Start/Ing/Over/Cancel); session layer",
        True, False, True),
    Command.ENROLL_FINGERPRINT: CommandInfo(
        0xB1, "CMD_ENROLL_FINGERPRINT",
        "start/press/fini/cancel; module stores the template", False, True,
        True),
    Command.IDENTIFY_FINGERPRINT: CommandInfo(
        0xB2, "CMD_IDENTIFY_FINGERPRINT",
        "module matches and returns handle + score", True, False, True),
    Command.REMOVE_FINGERPRINT: CommandInfo(
        0xB3, "CMD_REMOVE_FINGERPRINT",
        "delete a template on the module", False, True),
    Command.QUERY_FINGERPRINT_COUNT: CommandInfo(
        0xB4, "CMD_QUERY_FINGERPRINT_COUNT",
        "number of templates stored on the module", True, False),
    Command.GET_IMG_DATA: CommandInfo(
        0xB5, "CMD_GET_IMG_DATA",
        "diagnostic 8-bit grayscale image, three phases", True, False, True),
    Command.DETECT_FINGERPRINT_PRESS: CommandInfo(
        0xB6, "CMD_DETECT_FINGERPRINT_PRESS",
        "finger-presence poll", True, False),
    Command.SLEEP: CommandInfo(
        0xC0, "CMD_SLEEP", "put the module to sleep", False, False),
    Command.QUERY_MODULE_UID: CommandInfo(
        0xC1, "CMD_QUERY_MODULE_UID", "module UID", True, False),
    Command.QUERY_SENSOR_INFO: CommandInfo(
        0xC2, "CMD_QUERY_SENSOR_INFO",
        "width, height, flags", True, False),
    Command.JTAG_CONTROL: CommandInfo(
        0xC3, "CMD_JTAG_CONTROL", "vendor JTAG only - never send",
        False, True),
    Command.RESTORE_MCU: CommandInfo(
        0xD0, "CMD_RESTORE_MCU", "vendor MCU restore - never send",
        False, True),
    Command.UPDATE_FIRMWARE: CommandInfo(
        0xD1, "CMD_UPDATE_FIRMWARE",
        "firmware update - never send from Linux, ever", False, True),
    Command.FW_WORK_MODE_SWITCH: CommandInfo(
        0xFF, "CMD_FW_WORK_MODE_SWITCH",
        "Boot <-> App ('SM3') mode switch; side effects unknown",
        False, True),
}

#: Opcodes that only read state and are safe to try during bring-up.
READ_ONLY_COMMANDS: frozenset[Command] = frozenset(
    c for c, i in COMMANDS.items() if i.read_only)

#: Opcodes that can erase templates, reflash or restart the MCU.  A tool must
#: refuse these by default; ``docs/hw/fingerprint.md`` lists the same six.
DESTRUCTIVE_COMMANDS: frozenset[Command] = frozenset(
    c for c, i in COMMANDS.items() if i.destructive)


def command_info(opcode: int) -> CommandInfo | None:
    """Look up an opcode byte (int), returning ``None`` for unknown values."""
    try:
        return COMMANDS[Command(opcode)]
    except ValueError:
        return None


def command_name(opcode: int) -> str:
    """Human-readable command name, or ``op 0xNN`` when unknown."""
    info = command_info(opcode)
    return info.name if info else f"op 0x{opcode:02x}"


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    """A parsed BF63112 frame, request or answer.

    ``raw`` is the complete frame including the CRC; the other fields are
    decoded copies.  For an answer the same offsets are used as for a request
    (the layout is assumed symmetric - see the module docstring), with
    :attr:`status` holding the answer's status/parameter byte where a request
    carries ``0x00``.
    """

    raw: bytes
    length: int
    reserved: bytes
    magic: int
    opcode: int
    phase: int
    status: int
    arg: int
    data_len: int
    data: bytes
    crc: int

    # ---- derived views ---------------------------------------------------
    @property
    def info(self) -> CommandInfo | None:
        return command_info(self.opcode)

    @property
    def command(self) -> Command | None:
        info = self.info
        return Command(info.opcode) if info else None

    @property
    def is_request(self) -> bool:
        """Heuristic: the driver always sends 0x00 in the status/param byte."""
        return self.status == 0

    @property
    def align(self) -> int:
        """Aligned command-block size this frame carries."""
        return command_block_len(self.data_len)

    @property
    def body_from_status(self) -> bytes:
        """Answer body starting at the status byte (offset 24)."""
        return self.raw[FRAME_OFF_STATUS: len(self.raw) - 4]

    @property
    def body_from_arg(self) -> bytes:
        """Answer body starting after the 4-byte command block (offset 25).

        This is the view ``oxp3-fp-probe.py`` prints; which one the module
        really means is open point 2 in ``docs/hw/fingerprint.md``.
        """
        return self.raw[FRAME_OFF_ARG: len(self.raw) - 4]

    def describe(self) -> str:
        """One-line debug rendering (no payload dump)."""
        name = self.info.name if self.info else f"op 0x{self.opcode:02x}"
        return (f"{name} phase={self.phase} status=0x{self.status:02x} "
                f"arg=0x{self.arg:04x} len={self.data_len} "
                f"frame={len(self.raw)}B crc=0x{self.crc:08x}")


def _check_opcode(opcode: int) -> int:
    if not isinstance(opcode, int) or not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode must be a byte, got {opcode!r}")
    return opcode


def build_frame(opcode: int, phase: int = 0, arg: int = 0,
                data: bytes | bytearray | memoryview = b"",
                *, status: int = 0, reserved: bytes | None = None) -> bytes:
    """Build one frame exactly as the Windows driver does.

    ``opcode``/``phase``/``status`` are bytes, ``arg`` is u16 LE, ``data`` is
    the payload (its length is written twice: as ``len`` and implied by ``L``).
    ``reserved`` defaults to 16 zero bytes (the request header).  Responses can
    be synthesised for tests with a non-zero ``status`` or a non-zero
    ``reserved`` block.

    Raises :class:`ValueError` on out-of-range input; never truncates silently.
    """
    _check_opcode(opcode)
    if not 0 <= phase <= 0xFF:
        raise ValueError(f"phase must be a byte, got {phase!r}")
    if not 0 <= status <= 0xFF:
        raise ValueError(f"status must be a byte, got {status!r}")
    if not 0 <= arg <= 0xFFFF:
        raise ValueError(f"arg must fit in u16, got {arg!r}")
    payload = bytes(data)
    if len(payload) > MAX_DATA_LEN:
        raise ValueError(f"payload {len(payload)}B exceeds MAX_DATA_LEN "
                         f"({MAX_DATA_LEN}B)")
    if reserved is None:
        reserved = b"\x00" * HDR_RESERVED
    elif len(reserved) != HDR_RESERVED:
        raise ValueError(f"reserved block must be {HDR_RESERVED} bytes, "
                         f"got {len(reserved)}")

    block = command_block_len(len(payload))
    frame = bytearray()
    frame.append(SOF)
    frame += struct.pack("<I", length_field(len(payload)))
    frame += reserved
    frame += bytes((CMD_MAGIC, opcode, phase, status))
    frame += struct.pack("<H", arg)
    frame += struct.pack("<I", len(payload))
    frame += payload
    frame += b"\x00" * (block - CMD_BLOCK_FIXED - len(payload))
    frame += struct.pack("<I", crc32(bytes(frame)))
    return bytes(frame)


def parse_frame(buf: bytes | bytearray | memoryview, *,
                require_magic: bool = True,
                require_padding_zero: bool = False) -> Frame:
    """Validate and decode one complete frame.

    Every structural rule recovered from the driver is enforced here, so a
    successful parse means: start byte, L field, 16-byte alignment, magic,
    data_len inside the block, and a matching CRC-32.

    ``require_magic=False`` is for the (inferred) device->host direction, in
    case an answer ever omits the ``0x4C`` block; ``require_padding_zero=True``
    additionally rejects non-zero alignment padding (our frames are zero-padded,
    but a device is not obliged to keep that area clear).
    """
    buf = bytes(buf)
    n = len(buf)
    if n < FRAME_MIN_SIZE:
        raise TruncatedFrameError(
            f"frame is {n}B, minimum is {FRAME_MIN_SIZE}B")
    if buf[0] != SOF:
        raise BadStartByteError(
            f"frame starts with 0x{buf[0]:02x}, expected 0x{SOF:02x}")

    length = struct.unpack_from("<I", buf, FRAME_OFF_LEN)[0]
    if length != n - FRAME_OVERHEAD:
        raise LengthMismatchError(
            f"L={length} implies {length + FRAME_OVERHEAD}B, got {n}B")

    block = length - 4 - HDR_RESERVED
    if block < ALIGN or block % ALIGN:
        raise BadLengthFieldError(
            f"L={length} is not 20 + align16(10 + data_len)")

    magic = buf[FRAME_OFF_MAGIC]
    opcode = buf[FRAME_OFF_OPCODE]
    phase = buf[FRAME_OFF_PHASE]
    status = buf[FRAME_OFF_STATUS]
    arg, data_len = struct.unpack_from("<HI", buf, FRAME_OFF_ARG)

    if require_magic and magic != CMD_MAGIC:
        raise BadMagicError(
            f"command block starts with 0x{magic:02x}, expected 0x{CMD_MAGIC:02x}")
    if data_len > block - CMD_BLOCK_FIXED:
        raise BadDataLengthError(
            f"data_len={data_len} does not fit in a {block}B command block")

    want_crc = crc32(buf[:-4])
    got_crc = struct.unpack_from("<I", buf, n - 4)[0]
    if want_crc != got_crc:
        raise CRCMismatchError(want_crc, got_crc)

    data_end = FRAME_OFF_DATA + data_len
    data = buf[FRAME_OFF_DATA:data_end]
    if require_padding_zero:
        pad = buf[data_end:n - 4]
        if any(pad):
            raise FrameError(f"{len(pad)}B of non-zero alignment padding")

    return Frame(raw=buf, length=length,
                 reserved=buf[FRAME_OFF_RESERVED:FRAME_OFF_MAGIC],
                 magic=magic, opcode=opcode, phase=phase, status=status,
                 arg=arg, data_len=data_len, data=data, crc=got_crc)


@dataclass(frozen=True)
class FrameScanError:
    """One malformed region found while scanning a byte stream."""

    offset: int
    error: FrameError

    def __str__(self) -> str:
        return f"@{self.offset}: {self.error}"


def scan_frames(buf: bytes | bytearray | memoryview, *,
                require_magic: bool = True,
                require_padding_zero: bool = False) \
        -> tuple[list[Frame], list[FrameScanError]]:
    """Find every valid frame in *buf*, resynchronising on errors.

    The device->host stream is not framed from userspace's point of view: a
    read can carry leading ``0xFF``/``0x00`` padding, a partial frame, or an
    answer whose CRC is bad.  This scanner walks the buffer, skips anything
    that is not :data:`SOF`, and tries to parse a frame of the length announced
    by its L field.  Failures are returned as :class:`FrameScanError` entries
    and the scan resumes one byte later, so a single corrupt answer does not
    hide a valid one behind it.

    A truncated final frame is reported as an error at its offset (it is a
    complete "candidate" whose bytes are incomplete).  Returns ``(frames,
    errors)``; both may be empty (e.g. an all-0xFF read).
    """
    buf = bytes(buf)
    frames: list[Frame] = []
    errors: list[FrameScanError] = []
    pos = 0
    n = len(buf)
    while pos < n:
        if buf[pos] != SOF:
            pos += 1
            continue
        if pos + FRAME_OVERHEAD > n:
            errors.append(FrameScanError(pos, TruncatedFrameError(
                f"only {n - pos}B at end of stream")))
            break
        total = struct.unpack_from("<I", buf, pos + FRAME_OFF_LEN)[0] + 5
        if total < FRAME_MIN_SIZE or total > frame_size(MAX_DATA_LEN):
            errors.append(FrameScanError(pos, BadLengthFieldError(
                f"implausible L={total - 5}")))
            pos += 1
            continue
        if pos + total > n:
            errors.append(FrameScanError(pos, TruncatedFrameError(
                f"need {total}B, have {n - pos}B")))
            break
        try:
            frames.append(parse_frame(
                buf[pos:pos + total], require_magic=require_magic,
                require_padding_zero=require_padding_zero))
            pos += total
        except FrameError as exc:
            errors.append(FrameScanError(pos, exc))
            pos += 1
    return frames, errors


# --------------------------------------------------------------------------
# GET_IMG_DATA: header, chunks, reassembly
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ImageHeader:
    """Phase-0 answer of ``CMD_GET_IMG_DATA``: 9 bytes.

    Layout (recovered): ``status:u8 | total_bytes:u32 LE | byte_sum:u32 LE``.
    ``byte_sum`` is only ever compared as an 8-bit value (the module announces
    a 32-bit field but the check the Windows driver performs is a running
    8-bit sum), so :attr:`sum8_expected` exposes it masked.
    """

    status: int
    total_bytes: int
    byte_sum: int

    SIZE = 9

    @classmethod
    def parse(cls, payload: bytes | bytearray | memoryview) -> "ImageHeader":
        payload = bytes(payload)
        if len(payload) < cls.SIZE:
            raise ImageError(
                f"image header needs {cls.SIZE}B, got {len(payload)}B")
        status, total, byte_sum = struct.unpack_from("<BII", payload, 0)
        return cls(status=status, total_bytes=total, byte_sum=byte_sum)

    @classmethod
    def build(cls, total_bytes: int, byte_sum: int, status: int = 0) -> bytes:
        if not 0 <= total_bytes <= 0xFFFFFFFF:
            raise ValueError("total_bytes must fit in u32")
        return struct.pack("<BII", status & 0xFF, total_bytes,
                           byte_sum & 0xFFFFFFFF)

    @property
    def sum8_expected(self) -> int:
        return self.byte_sum & 0xFF


@dataclass
class ImageAssembler:
    """Reassemble a GET_IMG image from phase-1 chunks and verify it.

    Usage::

        asm = ImageAssembler(ImageHeader.parse(header_payload))
        for chunk in chunks:
            asm.add(chunk)            # optional chunk_sum=...
        image = asm.finish()          # checks size and global 8-bit sum

    ``finish()`` raises :class:`ImageSizeMismatch` if fewer/more bytes arrived
    and :class:`ImageSumMismatch` if the global sum is wrong.  ``add()``
    enforces the Windows driver's per-transfer ceiling (:data:`MAX_IMAGE_CHUNK`)
    and refuses to run past the announced total, because a runaway chunk means
    the framing or the announced geometry is wrong.
    """

    header: ImageHeader
    max_chunk: int = MAX_IMAGE_CHUNK
    _parts: list[bytes] = field(default_factory=list)
    _received: int = 0
    _sum: int = 0

    @classmethod
    def from_total(cls, total_bytes: int, byte_sum: int = 0,
                   max_chunk: int = MAX_IMAGE_CHUNK) -> "ImageAssembler":
        return cls(ImageHeader(0, total_bytes, byte_sum), max_chunk=max_chunk)

    @property
    def received(self) -> int:
        return self._received

    @property
    def remaining(self) -> int:
        return self.header.total_bytes - self._received

    def add(self, chunk: bytes | bytearray | memoryview,
            chunk_sum: int | None = None) -> None:
        """Append one chunk.

        ``chunk_sum`` is optional: pass the frame's status byte here if a
        per-chunk 8-bit sum is confirmed on hardware (the docs say there is no
        per-chunk check, so a driver must not assume one).  When given it is
        compared against :func:`sum8` of the chunk.
        """
        chunk = bytes(chunk)
        if not chunk:
            raise ImageError("empty chunk")
        if len(chunk) > self.max_chunk:
            raise ChunkTooLargeError(
                f"chunk {len(chunk)}B exceeds per-transfer limit "
                f"{self.max_chunk}B")
        if chunk_sum is not None and sum8(chunk) != (chunk_sum & 0xFF):
            raise ChunkSumMismatch(
                f"chunk sum 0x{sum8(chunk):02x} != announced "
                f"0x{chunk_sum & 0xFF:02x}")
        if self._received + len(chunk) > self.header.total_bytes:
            raise ImageSizeMismatch(
                f"{self._received}+{len(chunk)}B exceeds announced "
                f"{self.header.total_bytes}B")
        self._parts.append(chunk)
        self._received += len(chunk)
        self._sum = (self._sum + sum(chunk)) & 0xFF

    def finish(self) -> bytes:
        """Return the assembled image after checking size and global sum."""
        if self._received != self.header.total_bytes:
            raise ImageSizeMismatch(
                f"announced {self.header.total_bytes}B, received "
                f"{self._received}B")
        if self._sum != self.header.sum8_expected:
            raise ImageSumMismatch(
                f"image sum 0x{self._sum:02x} != announced "
                f"0x{self.header.sum8_expected:02x}")
        return b"".join(self._parts)


# --------------------------------------------------------------------------
# Session / retry state machine
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    """How many times a frame may be re-sent before the transaction fails.

    The Windows driver re-sends on the answer timeout; the numbers in
    :data:`TIMEOUTS_MS` are its per-attempt and total bounds.  Inter-attempt
    delays are a call-site policy; they are not modelled here.
    """

    max_attempts: int = 3
    resend_on_timeout: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


class TransactionState(enum.Enum):
    """Lifecycle of one request/answer exchange."""

    READY = "ready"
    AWAITING_ANSWER = "awaiting-answer"
    RESEND = "resend"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class Transaction:
    """A single request/answer exchange with resend-on-timeout.

    This is the transport-independent half of the driver's session: it knows
    what to send, whether a frame is the answer to it, and when to give up.  A
    caller drives it with real events::

        tx = Transaction(Command.QUERY_SENSOR_INFO)
        spi.write(tx.start())               # attempts == 1
        while tx.state in (AWAITING_ANSWER, RESEND):
            frames, errors = scan_frames(spi.read(...))
            if frames:
                tx.handle_frame(frames[0])  # -> SUCCESS or raises
            elif timer_expired():
                more = tx.handle_timeout()  # bytes to re-send, or None
                if more is None:            # -> FAILED, retries exhausted
                    break
                spi.write(more)

    ``expected_opcode``/``expected_phase`` control how strict the answer match
    is.  The docs do not prove that the module echoes opcode and phase in its
    answers, so the default checks the opcode only, and the phase only when
    ``check_phase`` is set.  A CRC/length failure must be reported by the
    caller through :meth:`handle_error`, which counts as a failed attempt.
    """

    opcode: int
    phase: int = int(Phase.START)
    arg: int = 0
    data: bytes = b""
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    check_phase: bool = False
    reserved: bytes | None = None

    state: TransactionState = field(
        init=False, default=TransactionState.READY)
    attempts: int = field(init=False, default=0)
    last_error: ProtocolError | None = field(init=False, default=None)
    response: Frame | None = field(init=False, default=None)

    # ---- building --------------------------------------------------------
    def frame_bytes(self) -> bytes:
        """The request frame for the current attempt."""
        return build_frame(self.opcode, self.phase, self.arg, self.data,
                           reserved=self.reserved)

    @property
    def info(self) -> CommandInfo | None:
        return command_info(self.opcode)

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> bytes:
        """Begin the exchange and return the bytes to send."""
        if self.state is not TransactionState.READY:
            raise SessionError(f"transaction already {self.state.value}")
        self.attempts = 1
        self.state = TransactionState.AWAITING_ANSWER
        return self.frame_bytes()

    def handle_timeout(self) -> bytes | None:
        """No answer yet.

        Returns the frame to re-send, or ``None`` once the policy is
        exhausted (state becomes ``FAILED``).  Calling it after success is an
        error; a timeout after the last attempt records a
        :class:`ProtocolError` as ``last_error``.
        """
        if self.state not in (TransactionState.AWAITING_ANSWER,
                              TransactionState.RESEND):
            raise SessionError(
                f"timeout in state {self.state.value}, expected awaiting-answer")
        if self.policy.resend_on_timeout and \
                self.attempts < self.policy.max_attempts:
            self.attempts += 1
            self.state = TransactionState.RESEND
            return self.frame_bytes()
        self.state = TransactionState.FAILED
        self.last_error = ProtocolError(
            f"no answer after {self.attempts} attempt(s)")
        return None

    def handle_error(self, exc: ProtocolError) -> None:
        """Record a transport/parse failure for the current attempt.

        A malformed answer is retried like a timeout: the module is allowed to
        answer while the host is still reading, and the Windows driver re-sends
        on timeout, so a bad read is treated the same way.
        """
        if self.state not in (TransactionState.AWAITING_ANSWER,
                              TransactionState.RESEND):
            raise SessionError(f"error in state {self.state.value}")
        self.last_error = exc
        if self.policy.resend_on_timeout and \
                self.attempts < self.policy.max_attempts:
            self.attempts += 1
            self.state = TransactionState.RESEND
        else:
            self.state = TransactionState.FAILED

    def handle_frame(self, frame: Frame) -> Frame:
        """Validate and accept an answer frame; returns it.

        Raises :class:`ResponseMismatchError` when the frame is not an answer
        to this request (wrong magic, opcode, or phase when ``check_phase``).
        Raises :class:`SessionError` when the transaction is not waiting.
        """
        if self.state not in (TransactionState.AWAITING_ANSWER,
                              TransactionState.RESEND):
            raise SessionError(
                f"frame accepted in state {self.state.value}")
        if frame.magic != CMD_MAGIC:
            raise ResponseMismatchError(
                f"answer magic 0x{frame.magic:02x} != 0x{CMD_MAGIC:02x}")
        if frame.opcode != self.opcode:
            raise ResponseMismatchError(
                f"answer is {command_name(frame.opcode)} but the request was "
                f"{command_name(self.opcode)}")
        if self.check_phase and frame.phase != self.phase:
            raise ResponseMismatchError(
                f"answer phase {frame.phase} != request phase {self.phase}")
        self.response = frame
        self.state = TransactionState.SUCCESS
        return frame

    # ---- convenience -----------------------------------------------------
    @property
    def done(self) -> bool:
        return self.state in (TransactionState.SUCCESS,
                              TransactionState.FAILED)

    def clone(self) -> "Transaction":
        """A fresh transaction for the same request (state not carried over)."""
        return Transaction(opcode=self.opcode, phase=self.phase, arg=self.arg,
                           data=self.data, policy=self.policy,
                           check_phase=self.check_phase,
                           reserved=self.reserved)
