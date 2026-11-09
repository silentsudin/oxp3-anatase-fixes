"""BF63112 SPI (ACPI/SPB) transport - the wire format the sensor really uses.

Recovered from ``BetterlifeFingerprintDevice.dll`` 9.39.15.26 (the ACPI/SPB
branch, not the USB branch); the full evidence is in
``docs/hw/fingerprint-transport.md`` §4:

* every transaction starts with a one-byte **selector**:

      selector = (opcode & 0x3f) << 1              # host reads from device
      selector = 0x80 | ((opcode & 0x3f) << 1)     # host writes to device

  i.e. bit 7 is the direction (1 = write), bits 6..1 are the low six opcode
  bits, bit 0 is always zero.  This is ``FUN_180017020`` (read, selector
  ``(x & 0x3f) * 2``) and ``FUN_180017404`` (write, ``(x | 0xc0) * 2``);
  the two forms differ by the 0x80 bit.

  **Consequence that the safety gate must handle:** the selector only carries
  six opcode bits, so the wire cannot distinguish ``0xA0`` from ``0x20``, nor
  ``0xC2`` (JTAG) from ``0x02``.  :class:`SpiTransport` therefore refuses by
  *selector* as well as by opcode (see ``FORBIDDEN_SELECTORS`` below).

* a **read** is the driver's two-entry SPB sequence: write the 1-byte
  selector, then clock ``len`` bytes out of the device
  (``FUN_18001197c`` builds the two transfers, ``FUN_180017020`` issues them
  and waits).  There is no length field and no leading status byte on the
  wire: the caller passes the exact length of the register/data it wants.
* a **write** is the same two-entry sequence with the payload as the second
  transfer (``FUN_180017404``).
* completion is the WDF request completion event
  (``FUN_1800113a0`` -> ``SetEvent``); a separate GPIO interrupt (``GpioInt``
  in ``_CRS``) signals "device has status".  The driver's DPC
  (``FUN_180016450``) then *reads a one-byte status register over SPI* and
  compares it with an armed mask before signalling the higher layer.  The
  line's edge/level and polarity are not in the binary - they come from ACPI.
* the first command lazily opens a reset target and sleeps 50 ms
  (``FUN_180016260`` -> ``FUN_180011550``); recovery pulses the reset line
  with 5 ms/10 ms sleeps (``FUN_180016110``/``FUN_180016a10``).
* per-command timeouts are the descriptor values: 1000 ms per attempt /
  2030 ms total for ordinary queries, 5000/10010 ms for GET_IMG,
  2000/4016 ms for the vendor restore, and a 150 ms cancel settle
  (``FUN_180013b60``).  A single spidev ``ioctl`` is synchronous and cannot
  be interrupted by this module, so a kernel-level hang still blocks; the
  deadline checks are applied at every boundary we control.

Safety design
-------------
``SpiTransport`` does nothing at construction time.  It refuses to open the
device and send anything unless the caller passed ``allow_writes=True`` *and*
the opcode is on an explicit allowlist.  Enrol/identify/remove/JTAG/restore/
firmware-update opcodes are refused unconditionally - both by opcode and by
the selector they would put on the wire.  ``--dry-run`` in the probe uses
:func:`build_plan`/:class:`SpiPlan` only, which performs no I/O.
"""

from __future__ import annotations

import ctypes
import dataclasses
import errno
import fcntl
import os
import select
import struct
import time
import warnings

# ---------------------------------------------------------------------------
# Linux spidev ABI (linux/spi/spidev.h); duplicated here so the library has no
# dependency on python3-spidev.
# ---------------------------------------------------------------------------

SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_RD_MODE = 0x80016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_RD_BITS_PER_WORD = 0x80016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04
SPI_IOC_RD_MAX_SPEED_HZ = 0x80046B04

_SPI_IOC_TRANSFER = struct.Struct("<QQIIHBBBBBB")  # struct spi_ioc_transfer
assert _SPI_IOC_TRANSFER.size == 32


def spi_ioc_message(n: int) -> int:
    """``SPI_IOC_MESSAGE(n)``: _IOW('k', 0, char[n * sizeof(spi_ioc_transfer)])."""
    if not 1 <= n <= 0xFFFF:
        raise ValueError("n out of range")
    return (1 << 30) | ((n * _SPI_IOC_TRANSFER.size) << 16) | (0x6B << 8) | 0


SPI_IOC_MESSAGE_1 = spi_ioc_message(1)

# ---------------------------------------------------------------------------
# Linux GPIO character-device ABI v2 (linux/gpio.h) - stdlib only
#
# The controller nodes are /dev/gpiochipN; every request is one
# GPIO_V2_GET_LINE_IOCTL on the chip fd and every event read is a
# struct gpio_v2_line_event from the returned request fd.  No libgpiod and no
# /sys/class/gpio are involved.
#
# Struct sizes on a 64-bit kernel (the kernel builds these with explicit
# padding, so they are identical for 32/64-bit userspace):
#
#   struct gpiochip_info                68 B   (2 x 32 sName + u32 lines)
#   struct gpio_v2_line_values          16 B   (u64 bits, u64 mask)
#   struct gpio_v2_line_attribute       16 B   (u32 id, u32 pad, u64 union)
#   struct gpio_v2_line_config_attribute 24 B  (attribute + u64 mask)
#   struct gpio_v2_line_config         272 B   (u64 flags, u32 num_attrs,
#                                               5 x u32 pad, 10 x attr)
#   struct gpio_v2_line_request        592 B   (64 x u32 offsets,
#                                               32 sName consumer,
#                                               config 272, u32 num_lines,
#                                               u32 event_buffer_size,
#                                               5 x u32 pad, s32 fd)
#   struct gpio_v2_line_event           48 B   (u64 timestamp_ns, u32 id,
#                                               u32 offset, u32 seqno,
#                                               u32 line_seqno, 6 x u32 pad)
#
# The ioctl numbers are _IOC()-encoded from those sizes; a wrong size changes
# the number and the kernel answers ENOTTY instead of misreading the buffer.
# ---------------------------------------------------------------------------

GPIO_MAX_NAME_SIZE = 32
GPIO_V2_LINES_MAX = 64
GPIO_V2_LINE_NUM_ATTRS_MAX = 10

GPIO_V2_LINE_FLAG_ACTIVE_LOW = 1 << 1
GPIO_V2_LINE_FLAG_INPUT = 1 << 2
GPIO_V2_LINE_FLAG_OUTPUT = 1 << 3
GPIO_V2_LINE_FLAG_EDGE_RISING = 1 << 4
GPIO_V2_LINE_FLAG_EDGE_FALLING = 1 << 5

GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES = 2
GPIO_V2_LINE_EVENT_RISING_EDGE = 1
GPIO_V2_LINE_EVENT_FALLING_EDGE = 2

_GPIOCHIP_INFO = struct.Struct("<32s32sI")            # 68 B
assert _GPIOCHIP_INFO.size == 68
_GPIO_LINE_VALUES = struct.Struct("<QQ")              # 16 B
assert _GPIO_LINE_VALUES.size == 16
_GPIO_LINE_CONFIG_ATTR = struct.Struct("<IIQQ")       # 24 B
assert _GPIO_LINE_CONFIG_ATTR.size == 24
_GPIO_LINE_CONFIG_HEAD = struct.Struct("<QI5I")       # 32 B + 10 attrs
assert _GPIO_LINE_CONFIG_HEAD.size == 32
_GPIO_LINE_REQUEST_HEAD = struct.Struct("<64I32s")    # 288 B
assert _GPIO_LINE_REQUEST_HEAD.size == 288
_GPIO_LINE_REQUEST_TAIL = struct.Struct("<II5Ii")     # 32 B
assert _GPIO_LINE_REQUEST_TAIL.size == 32
_GPIO_LINE_EVENT = struct.Struct("<QIIII6I")          # 48 B
assert _GPIO_LINE_EVENT.size == 48

_GPIO_LINE_CONFIG_SIZE = 32 + 10 * _GPIO_LINE_CONFIG_ATTR.size   # 272
_GPIO_LINE_REQUEST_SIZE = 288 + _GPIO_LINE_CONFIG_SIZE + 32      # 592
_GPIO_LINE_CONFIG_ATTR_OFFSET = 288 + 32                         # 320
_GPIO_LINE_REQUEST_TAIL_OFFSET = 288 + _GPIO_LINE_CONFIG_SIZE    # 560
_GPIO_LINE_REQUEST_FD_OFFSET = _GPIO_LINE_REQUEST_SIZE - 4       # 588


def _gpio_ioc(direction: int, nr: int, size: int) -> int:
    """Linux ``_IOC(dir, 0xb4, nr, size)`` for the GPIO ioctl family."""
    return (direction << 30) | (size << 16) | (0xB4 << 8) | nr


GPIO_GET_CHIPINFO_IOCTL = _gpio_ioc(2, 0x01, _GPIOCHIP_INFO.size)
GPIO_V2_GET_LINE_IOCTL = _gpio_ioc(3, 0x07, _GPIO_LINE_REQUEST_SIZE)
GPIO_V2_LINE_GET_VALUES_IOCTL = _gpio_ioc(3, 0x0E, _GPIO_LINE_VALUES.size)
GPIO_V2_LINE_SET_VALUES_IOCTL = _gpio_ioc(3, 0x0F, _GPIO_LINE_VALUES.size)
assert GPIO_GET_CHIPINFO_IOCTL == 0x8044B401
assert GPIO_V2_GET_LINE_IOCTL == 0xC250B407
assert GPIO_V2_LINE_SET_VALUES_IOCTL == 0xC010B40F

#: Environment override for the GPIO backend, read when ``backend="auto"``
#: (or ``None``).  The probe's ``--gpio-backend`` passes the value explicitly.
GPIO_BACKEND_ENV = "FPBF63112_GPIO_BACKEND"

#: Backend names in the order they are tried by ``backend="auto"``.
IRQ_BACKENDS = ("gpiod", "chardev", "sysfs", "poll")
RESET_BACKENDS = ("gpiod", "chardev", "sysfs")

# ---------------------------------------------------------------------------
# Recovered wire constants
# ---------------------------------------------------------------------------

SELECTOR_READ = 0x00
SELECTOR_WRITE = 0x80


def selector(opcode: int, write: bool = False) -> int:
    """The one-byte selector for ``opcode`` (see the module docstring)."""
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode out of range: {opcode!r}")
    value = (opcode & 0x3F) << 1
    return (value | SELECTOR_WRITE) if write else value


def selector_low(opcode: int) -> int:
    """Selector with the direction bit cleared (the part that gates safety)."""
    return selector(opcode) & 0x7F


#: Recovered SPI register that the driver reads/writes for capture mode and
#: interrupt masking (``FUN_18000e588``: read 0x2d, set bit 0x10, write it
#: back, later restore it).
OP_STATUS_REG = 0x2D

#: Write-only command opcodes recovered from the same function (the value
#: written to the mode register is the *payload*, not the opcode).
#: Kept for documentation only; not allowlisted.
OP_MODE = 0x2D

#: Destructive / forbidden opcodes: never sent, even if a caller lists them.
#: ``0xB1`` (identify) is included because the task's safety contract names
#: identify alongside enrol.
DESTRUCTIVE_OPS = {
    0xB0: "ENROLL_FINGERPRINT",
    0xB1: "IDENTIFY_FINGERPRINT",
    0xB2: "REMOVE_FINGERPRINT",
    0xC2: "JTAG_CONTROL",
    0xC3: "RESTORE_MCU",
    0xD0: "UPDATE_FIRMWARE",
    0xD1: "FW_WORK_MODE_SWITCH",
    0xFF: "FW_WORK_MODE_SWITCH",
}

#: Low seven selector bits that must never reach the wire.  Because the
#: selector drops opcode bits 7..6, these also block aliases such as 0x02
#: (which would otherwise compute JTAG's selector 0x04 / 0x84).
FORBIDDEN_SELECTORS: dict[int, tuple[int, ...]] = {
    selector_low(opcode): tuple(
        other for other in DESTRUCTIVE_OPS
        if selector_low(other) == selector_low(opcode))
    for opcode in DESTRUCTIVE_OPS
}

#: Read-only commands that a probe may send.  These are the *USB* command
#: table opcodes; the SPI branch has its own, data-driven opcode set, so on
#: SPI they are deliberate read-only experiments (docs/hw/fingerprint-
#: transport.md §9 step 2) plus the one recovered SPI register read (0x2d).
SAFE_READ_OPS: dict[int, str] = {
    0xA0: "QUERY_FIRMWARE_VER",
    0xA1: "QUERY_MODULE_SCHEME_INFORMATION",
    0xB3: "QUERY_FINGERPRINT_COUNT",
    0xC0: "QUERY_MODULE_UID",
    0xC1: "QUERY_SENSOR_INFO",
    OP_STATUS_REG: "SPI status/mode register (recovered)",
}

#: No write opcode is allowlisted by default.  A caller that really means to
#: write must extend both ``write_allowlist`` and ``allow_writes=True``.
SAFE_WRITE_OPS: dict[int, str] = {}

# ---------------------------------------------------------------------------
# Recovered timings (milliseconds)
# ---------------------------------------------------------------------------

#: One SPI transfer / one command attempt in the Windows driver.
XFER_TIMEOUT_MS = 200
#: Ordinary query: per attempt, then total deadline (1000/2030).
CMD_TIMEOUT_MS = 1000
CMD_TOTAL_MS = 2030
#: GET_IMG: per attempt, then total deadline (5000/10010).
IMAGE_TIMEOUT_MS = 5000
IMAGE_TOTAL_MS = 10010
#: Vendor restore (0xC3): per attempt, then total deadline (2000/4016).
RESTORE_TIMEOUT_MS = 2000
RESTORE_TOTAL_MS = 4016
#: Cancel settle after resetting the request events (150).
CANCEL_SETTLE_MS = 150
#: Sleep after the reset target is opened (50).
RESET_OPEN_MS = 50
#: Reset pulse widths (5 ms asserted, 10 ms released).
RESET_ASSERT_MS = 5
RESET_RELEASE_MS = 10
#: Capture wait in FUN_18000e588: 3 x 100 ms, 50 ms between attempts.
IRQ_WAIT_MS = 100
IRQ_ATTEMPTS = 3
IRQ_RETRY_SLEEP_MS = 50

#: Opcodes whose recovered descriptor uses a longer timeout.
_IMAGE_OPCODE = 0xB4
_RESTORE_OPCODE = 0xC3


def default_timeouts(opcode: int) -> tuple[int, int]:
    """``(per_attempt_ms, total_ms)`` for ``opcode`` (recovered values)."""
    if opcode == _IMAGE_OPCODE:
        return IMAGE_TIMEOUT_MS, IMAGE_TOTAL_MS
    if opcode == _RESTORE_OPCODE:
        return RESTORE_TIMEOUT_MS, RESTORE_TOTAL_MS
    return CMD_TIMEOUT_MS, CMD_TOTAL_MS


SPI_ACPI_EXPECTATIONS = """\
The BF63112 SPI connection is described by _SB.PC00.SPI1.FPRT._CRS:
  * SpiSerialBus (type 2): ConnectionSpeed (Hz), DataBitLength (8/16),
    ClockPhase (0/1), ClockPolarity (0/1), DeviceSelection (CS index),
    ResourceSource (the SPI controller, e.g. \\_SB.PC00.SPI1).
  * GpioInt: the sensor interrupt line (PinIndex + triggering/polarity).
  * optional GpioIo: the reset line the Windows driver drives through the
    resource hub (if absent, the module must come up out of reset).
The driver package itself contains no clock, mode, bit width or GPIO number.
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SpiError(Exception):
    """Base class for every SPI-transport failure."""


class SpiRefused(SpiError):
    """The safety gate refused the operation."""


class CommandNotAllowed(SpiRefused):
    """The opcode is not on the caller's explicit allowlist."""


class UnsafeCommand(SpiRefused):
    """A destructive opcode (enrol/identify/remove/JTAG/restore/update)."""


class SpiTimeout(SpiError):
    """The per-command or IRQ wait deadline expired."""


class Cancelled(SpiError):
    """The cancel policy was applied while a transaction was in flight."""


class IrqUnavailable(SpiError):
    """No GPIO backend could be opened for the interrupt line."""


class GpioError(SpiError):
    """Base class for the GPIO character-device (uAPI v2) backend."""


class GpioChipNotFound(GpioError):
    """The requested /dev/gpiochipN node (or its name/label) does not exist."""


class GpioLineOutOfRange(GpioError):
    """The line offset is outside the chip's reported line count."""


class GpioUnsupported(GpioError):
    """ENOTTY: not a GPIO character device, or no uAPI v2 support."""


class GpioInvalidRequest(GpioError):
    """EINVAL: the kernel rejected the line request/values/event buffer."""


class GpioPermissionDenied(GpioError):
    """EACCES/EPERM: the process may not open or request this GPIO line."""


class GpioLineBusy(GpioError):
    """EBUSY: another consumer already owns the requested line."""


def _gpio_backend(backend: str | None) -> str:
    """Resolve ``auto``/``None`` through ``$FPBF63112_GPIO_BACKEND``."""
    if backend not in (None, "auto"):
        return backend
    env = os.environ.get(GPIO_BACKEND_ENV, "").strip()
    return env or "auto"


def _gpio_os_error(exc: OSError, what: str) -> GpioError:
    """Translate one GPIO errno into the typed error the caller can act on."""
    code = exc.errno
    detail = exc.strerror or str(exc)
    if code == errno.ENOTTY:
        return GpioUnsupported(
            f"{what}: {detail} (ENOTTY); the kernel's GPIO character device "
            f"does not support the v2 uAPI here (CONFIG_GPIO_CDEV?)")
    if code == errno.EINVAL:
        return GpioInvalidRequest(
            f"{what}: {detail} (EINVAL); the kernel rejected the line request "
            f"(flags/attributes/num_lines/line offset)")
    if code in (errno.EACCES, errno.EPERM):
        return GpioPermissionDenied(
            f"{what}: {detail} (errno {code}); opening a GPIO chip or "
            f"requesting a line normally needs root or a udev ACL")
    if code == errno.EBUSY:
        return GpioLineBusy(f"{what}: {detail} (EBUSY)")
    if code in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
        return GpioChipNotFound(f"{what}: {detail} (errno {code})")
    return GpioError(f"{what}: {detail} (errno {code})")


def _gpio_ioctl(fd: int, request: int, buf: bytearray, what: str) -> None:
    """One ioctl on a mutable buffer; raises a typed :class:`GpioError`.

    ``fcntl.ioctl`` with a ``bytearray`` mutates it in place and returns ``0``,
    which is what the kernel's _IOWR structs (request/fd, values) need.
    """
    try:
        fcntl.ioctl(fd, request, buf)
    except OSError as exc:
        raise _gpio_os_error(exc, what) from exc


# ---------------------------------------------------------------------------
# GPIO character device: line requests, values and events
# ---------------------------------------------------------------------------

def build_line_request(offsets, flags: int, *,
                       consumer: str = "fpbf63112",
                       attrs=(),
                       event_buffer_size: int = 0) -> bytearray:
    """Pack one ``struct gpio_v2_line_request`` (592 B, see above).

    ``attrs`` is an iterable of ``(attr_id, value, mask)`` tuples, packed as
    ``struct gpio_v2_line_config_attribute`` entries.  ``flags`` never contains
    ``GPIO_V2_LINE_FLAG_ACTIVE_LOW``: this backend inverts values/edges in
    software so the physical level is explicit on the wire.
    """
    offsets = tuple(int(o) for o in offsets)
    if not 1 <= len(offsets) <= GPIO_V2_LINES_MAX:
        raise GpioInvalidRequest(
            f"a line request needs 1..{GPIO_V2_LINES_MAX} offsets, "
            f"not {len(offsets)}")
    for line in offsets:
        if not 0 <= line <= 0xFFFFFFFF:
            raise GpioLineOutOfRange(f"GPIO line offset out of range: {line}")
    attrs = tuple(attrs)
    if len(attrs) > GPIO_V2_LINE_NUM_ATTRS_MAX:
        raise GpioInvalidRequest(
            f"at most {GPIO_V2_LINE_NUM_ATTRS_MAX} config attributes are "
            f"allowed, not {len(attrs)}")
    if not 0 <= event_buffer_size <= 0xFFFFFFFF:
        raise GpioInvalidRequest(
            f"event_buffer_size out of range: {event_buffer_size}")
    name = str(consumer).encode("utf-8")[:GPIO_MAX_NAME_SIZE - 1]
    buf = bytearray(_GPIO_LINE_REQUEST_SIZE)
    _GPIO_LINE_REQUEST_HEAD.pack_into(
        buf, 0, *(offsets + (0,) * (GPIO_V2_LINES_MAX - len(offsets))), name)
    _GPIO_LINE_CONFIG_HEAD.pack_into(
        buf, _GPIO_LINE_REQUEST_HEAD.size, flags, len(attrs), 0, 0, 0, 0, 0)
    for index, (attr_id, value, mask) in enumerate(attrs):
        _GPIO_LINE_CONFIG_ATTR.pack_into(
            buf, _GPIO_LINE_CONFIG_ATTR_OFFSET
            + index * _GPIO_LINE_CONFIG_ATTR.size,
            attr_id, 0, value, mask)
    _GPIO_LINE_REQUEST_TAIL.pack_into(
        buf, _GPIO_LINE_REQUEST_TAIL_OFFSET,
        len(offsets), event_buffer_size, 0, 0, 0, 0, 0, -1)
    return buf


@dataclasses.dataclass(frozen=True)
class GpioLineEvent:
    """One decoded ``struct gpio_v2_line_event`` (48 B)."""

    timestamp_ns: int
    event_id: int
    offset: int
    seqno: int
    line_seqno: int

    @property
    def edge(self) -> str | None:
        """``"rising"``/``"falling"``, or ``None`` for an unknown id."""
        if self.event_id == GPIO_V2_LINE_EVENT_RISING_EDGE:
            return "rising"
        if self.event_id == GPIO_V2_LINE_EVENT_FALLING_EDGE:
            return "falling"
        return None


def decode_line_event(raw) -> GpioLineEvent:
    """Decode the first ``struct gpio_v2_line_event`` from ``raw``."""
    if len(raw) < _GPIO_LINE_EVENT.size:
        raise GpioInvalidRequest(
            f"GPIO event is {len(raw)} B; need "
            f"{_GPIO_LINE_EVENT.size} B for one record")
    (timestamp_ns, event_id, offset, seqno, line_seqno,
     *_padding) = _GPIO_LINE_EVENT.unpack_from(bytes(raw[:48]), 0)
    return GpioLineEvent(timestamp_ns, event_id, offset, seqno, line_seqno)


def decode_line_events(raw) -> tuple[GpioLineEvent, ...]:
    """Decode every complete event in one ``read()`` from the request fd."""
    raw = bytes(raw)
    if len(raw) % _GPIO_LINE_EVENT.size:
        raise GpioInvalidRequest(
            f"GPIO event read returned {len(raw)} B, not a multiple of "
            f"{_GPIO_LINE_EVENT.size} B")
    return tuple(decode_line_event(raw[index:index + _GPIO_LINE_EVENT.size])
                 for index in range(0, len(raw), _GPIO_LINE_EVENT.size))


class GpioChardev:
    """One GPIO chip opened through ``/dev/gpiochipN`` (uAPI v2).

    ``chip`` may be ``"gpiochip1"`` (joined onto ``dev_dir``), an absolute path
    such as ``"/dev/gpiochip1"``, or a chip name/label (e.g. ``"INTC10BC"``):
    the last form scans ``dev_dir`` and matches ``struct gpiochip_info``'s name
    and label with ``GPIO_GET_CHIPINFO_IOCTL``.  Construction does no I/O;
    :meth:`open` resolves and opens the chip and reads its line count.
    """

    def __init__(self, chip: str | None = None, *, dev_dir: str = "/dev"):
        self.chip = chip or "gpiochip0"
        self.dev_dir = dev_dir
        self.path: str | None = None
        self.fd: int | None = None
        self.name: str | None = None
        self.label: str | None = None
        self.lines: int | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "GpioChardev":
        if self.fd is not None:
            return self
        path = self._resolve_path()
        try:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise GpioChipNotFound(
                f"GPIO chip {self.chip!r} not found at {path}") from exc
        except PermissionError as exc:
            raise GpioPermissionDenied(
                f"cannot open {path}: {exc.strerror or exc}; GPIO character "
                f"devices normally need root or a udev ACL") from exc
        except OSError as exc:
            raise _gpio_os_error(exc, f"open {path}") from exc
        self.fd = fd
        self.path = path
        try:
            name, label, lines = self._read_chipinfo(fd)
        except GpioError:
            self.close()
            raise
        self.name, self.label, self.lines = name, label, lines
        return self

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            finally:
                self.fd = None

    # -- chip lookup -------------------------------------------------------
    def _resolve_path(self) -> str:
        chip = str(self.chip)
        if chip.startswith("/"):
            return chip
        if chip.startswith("gpiochip") and chip[8:].isdigit():
            return os.path.join(self.dev_dir, chip)
        return self._resolve_by_name(chip)

    def _resolve_by_name(self, wanted: str) -> str:
        try:
            entries = sorted(os.listdir(self.dev_dir))
        except OSError as exc:
            raise GpioChipNotFound(
                f"cannot list {self.dev_dir}: {exc}") from exc
        seen = []
        for entry in entries:
            if not entry.startswith("gpiochip"):
                continue
            path = os.path.join(self.dev_dir, entry)
            try:
                fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                name, label, _lines = self._read_chipinfo(fd)
            except GpioError:
                continue
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            seen.append(f"{entry}={name!r}/{label!r}")
            if wanted in (name, label):
                return path
        raise GpioChipNotFound(
            f"no GPIO chip named or labelled {wanted!r} in {self.dev_dir}"
            + (f" (saw {', '.join(seen)})" if seen else ""))

    @staticmethod
    def _read_chipinfo(fd: int) -> tuple[str, str, int]:
        buf = bytearray(_GPIOCHIP_INFO.size)
        _gpio_ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, buf,
                    "GPIO_GET_CHIPINFO_IOCTL")
        name, label, lines = _GPIOCHIP_INFO.unpack_from(buf, 0)
        return (name.split(b"\0", 1)[0].decode("utf-8", "replace"),
                label.split(b"\0", 1)[0].decode("utf-8", "replace"),
                lines)

    def check_line(self, line: int) -> None:
        """Refuse an offset outside the chip before the request ioctl."""
        if not isinstance(line, int) or isinstance(line, bool) or line < 0:
            raise GpioLineOutOfRange(f"GPIO line offset must be >= 0: {line!r}")
        if self.lines is not None and line >= self.lines:
            raise GpioLineOutOfRange(
                f"line {line} is outside {self.path} "
                f"({self.lines} lines, offsets 0..{self.lines - 1})")

    def request_lines(self, request: bytearray | bytes) -> int:
        """Issue ``GPIO_V2_GET_LINE_IOCTL``; return the request fd."""
        if self.fd is None:
            raise GpioError("GPIO chip is not open")
        if len(request) != _GPIO_LINE_REQUEST_SIZE:
            raise GpioInvalidRequest(
                f"line request is {len(request)} B; need "
                f"{_GPIO_LINE_REQUEST_SIZE} B")
        buf = bytearray(request)
        _gpio_ioctl(self.fd, GPIO_V2_GET_LINE_IOCTL, buf,
                    f"GPIO_V2_GET_LINE_IOCTL on {self.path}")
        fd = struct.unpack_from("<i", buf, _GPIO_LINE_REQUEST_FD_OFFSET)[0]
        if fd < 0:
            raise GpioInvalidRequest(
                f"GPIO_V2_GET_LINE_IOCTL on {self.path} returned fd {fd}")
        return fd


class _ChardevOutput:
    """Reset line through the GPIO v2 character device."""

    name = "chardev"

    def __init__(self, line: int, chip: str | None = None,
                 active_low: bool = False, *, dev_dir: str = "/dev"):
        self._line = line
        self._active_low = bool(active_low)
        self._chardev = GpioChardev(chip, dev_dir=dev_dir)
        self._fd: int | None = None
        self._open()

    def _physical(self, logical) -> int:
        """Logical assertion -> physical level (active-low inverts)."""
        value = 1 if logical else 0
        return value ^ 1 if self._active_low else value

    def _open(self) -> None:
        dev = self._chardev.open()
        try:
            dev.check_line(self._line)
            request = build_line_request(
                (self._line,), GPIO_V2_LINE_FLAG_OUTPUT,
                consumer="fpbf63112-reset",
                attrs=((GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES,
                        self._physical(0), 1),))
            self._fd = dev.request_lines(request)
        except SpiError:
            self._chardev.close()
            raise

    def set(self, value: int) -> None:
        if self._fd is None:
            raise GpioError(f"GPIO output line {self._line} is closed")
        physical = self._physical(value)
        buf = bytearray(_GPIO_LINE_VALUES.size)
        _GPIO_LINE_VALUES.pack_into(buf, 0, physical, 1)
        _gpio_ioctl(self._fd, GPIO_V2_LINE_SET_VALUES_IOCTL, buf,
                    f"GPIO_V2_LINE_SET_VALUES_IOCTL line {self._line}")

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None
        self._chardev.close()


class _ChardevWaiter:
    """Interrupt line through the GPIO v2 character device."""

    name = "chardev"

    def __init__(self, line: int, chip: str | None = None,
                 edge: str = "rising", active_low: bool = False, *,
                 dev_dir: str = "/dev"):
        if edge not in ("rising", "falling", "both"):
            raise ValueError(f"bad edge {edge!r}")
        self._line = line
        self._edge = edge
        self._active_low = bool(active_low)
        self._chardev = GpioChardev(chip, dev_dir=dev_dir)
        self._fd: int | None = None
        self.last_event: GpioLineEvent | None = None
        self._open()

    def _open(self) -> None:
        dev = self._chardev.open()
        try:
            dev.check_line(self._line)
            edge_flags = {
                "rising": GPIO_V2_LINE_FLAG_EDGE_RISING,
                "falling": GPIO_V2_LINE_FLAG_EDGE_FALLING,
                "both": (GPIO_V2_LINE_FLAG_EDGE_RISING
                         | GPIO_V2_LINE_FLAG_EDGE_FALLING),
            }[self._edge]
            request = build_line_request(
                (self._line,), GPIO_V2_LINE_FLAG_INPUT | edge_flags,
                consumer="fpbf63112")
            self._fd = dev.request_lines(request)
        except SpiError:
            self._chardev.close()
            raise

    def wait(self, timeout_s: float):
        """Return ``(logical_edge, None)`` or ``(None, None)`` on timeout."""
        if self._fd is None:
            raise GpioError(f"GPIO event line {self._line} is closed")
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout_s)
        except InterruptedError:
            return None, None
        except (OSError, ValueError) as exc:
            raise _gpio_os_error(
                exc if isinstance(exc, OSError) else OSError(
                    errno.EBADF, str(exc)),
                f"select on GPIO event line {self._line}") from exc
        if not ready:
            return None, None
        try:
            # Exactly one event: any further queued events stay in the kernel
            # buffer for the next wait() instead of being decoded and dropped.
            raw = os.read(self._fd, _GPIO_LINE_EVENT.size)
        except InterruptedError:
            return None, None
        except BlockingIOError:
            return None, None
        except OSError as exc:
            raise _gpio_os_error(
                exc, f"read GPIO event from line {self._line}") from exc
        events = decode_line_events(raw)
        if not events:
            return None, None
        event = events[0]
        self.last_event = event
        edge = event.edge
        if self._active_low and edge is not None:
            # We did not ask the kernel for ACTIVE_LOW, so the event id is the
            # physical edge: invert it into the logical assertion edge.
            edge = "falling" if edge == "rising" else "rising"
        return edge, None

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None
        self._chardev.close()


# ---------------------------------------------------------------------------
# Plan: exactly what a transaction puts on the wire
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SpiTransfer:
    """One ``struct spi_ioc_transfer`` the plan will submit.

    ``tx`` is written (empty means a receive-only transfer, which clocks
    zeros) and ``rx_len`` bytes are captured.  One plan is submitted as a
    single ``SPI_IOC_MESSAGE(n)`` ioctl so the chip select stays asserted.
    """

    tx: bytes = b""
    rx_len: int = 0
    speed_hz: int = 0
    delay_us: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "tx", bytes(self.tx))
        if self.rx_len < 0:
            raise ValueError("rx_len must be >= 0")
        if not self.tx and not self.rx_len:
            raise ValueError("transfer has neither tx nor rx bytes")


@dataclasses.dataclass(frozen=True)
class ReadShape:
    """A non-standard *read* write shape, used by the ``--try-write-shape`` probe.

    The recovered driver always sends exactly one selector byte before clocking
    an answer.  The bring-up run suggests the sensor may instead expect the
    selector plus one or more command bytes (phase/arg/padding) and may also
    expect the read flag in selector bit 0.  This type keeps such a plan
    explicit and auditable: ``selector_variant`` is ``"read"`` (bit 0 clear,
    the recovered shape) or ``"read-bit0"`` (bit 0 set), and ``padding`` is
    written immediately after the selector byte.
    """

    selector_variant: str = "read"
    padding: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "padding", bytes(self.padding))
        if self.selector_variant not in ("read", "read-bit0"):
            raise ValueError(f"bad selector variant {self.selector_variant!r}")
        if len(self.padding) > 8:
            raise ValueError("write-shape padding is limited to 8 bytes")

    @property
    def selector_bit0(self) -> int:
        return 1 if self.selector_variant == "read-bit0" else 0


@dataclasses.dataclass(frozen=True)
class SpiPlan:
    """One transaction, fully determined before any I/O happens.

    ``transfers`` is the exact two-entry SPB sequence: selector first, then
    the payload (write) or the read clocking (read).  ``tx`` is the
    concatenation of the outgoing bytes and ``rx_len`` the total receive
    length; ``answer_len`` is the number of device bytes a read returns.
    """

    opcode: int
    operation: str            # "read" or "write"
    payload: bytes
    read_len: int
    selector: int
    transfers: tuple[SpiTransfer, ...]
    timeout_ms: int
    total_ms: int
    shape: ReadShape | None = None

    def __post_init__(self) -> None:
        if self.operation not in ("read", "write"):
            raise ValueError(f"bad operation {self.operation!r}")
        if self.shape is not None and self.operation != "read":
            raise ValueError("a write shape only applies to a read plan")
        if self.shape is None:
            if self.selector != selector(self.opcode,
                                         self.operation == "write"):
                raise ValueError("selector does not match opcode/direction")
        else:
            base = selector(self.opcode, write=False)
            if self.selector != base | self.shape.selector_bit0:
                raise ValueError("shape selector does not match its variant")
            if self.selector & 0x80:
                raise ValueError("a read-shape selector must not set bit 7")
        if not self.transfers:
            raise ValueError("plan has no transfers")
        first_tx = bytes([self.selector])
        if self.shape is not None:
            first_tx += self.shape.padding
        if self.transfers[0].tx != first_tx:
            raise ValueError("first transfer must be the selector byte")
        if self.operation == "read":
            if len(self.transfers) != 2:
                raise ValueError("read plan is selector + receive clock")
            if self.transfers[0].rx_len:
                raise ValueError("selector transfer must not read")
            if self.transfers[1].tx:
                raise ValueError("read plan's second transfer is receive-only")
            if self.transfers[1].rx_len != self.read_len:
                raise ValueError("read plan rx_len must equal read_len")
        else:
            if len(self.transfers) not in (1, 2):
                raise ValueError("write plan is selector (+ payload)")
            if any(t.rx_len for t in self.transfers):
                raise ValueError("write plan must not read")
            if self.read_len:
                raise ValueError("write plan has a read_len")
            if len(self.transfers) == 1 and self.payload:
                raise ValueError("write plan dropped its payload")
            if len(self.transfers) == 2 and self.transfers[1].tx != self.payload:
                raise ValueError("write plan payload mismatch")
        if self.segment_count > 0xFFFF:
            raise ValueError("too many transfers")
        if self.timeout_ms <= 0 or self.total_ms <= 0:
            raise ValueError("timeouts must be > 0")
        if self.total_ms < self.timeout_ms:
            raise ValueError("total timeout smaller than one attempt")

    @property
    def tx(self) -> bytes:
        return b"".join(t.tx for t in self.transfers)

    @property
    def rx_len(self) -> int:
        return sum(t.rx_len for t in self.transfers)

    @property
    def segment_count(self) -> int:
        return len(self.transfers)

    @property
    def answer_len(self) -> int:
        return self.read_len if self.operation == "read" else 0

    def hexdump(self) -> str:
        """The exact wire bytes, with receive-only regions marked."""
        parts = []
        for transfer in self.transfers:
            if transfer.tx:
                parts.append(transfer.tx.hex(" "))
            if transfer.rx_len:
                parts.append(f"<- {transfer.rx_len} B")
        return " | ".join(parts)


def build_plan(opcode: int, *, operation: str = "read",
               payload: bytes = b"", read_len: int | None = None,
               timeout_ms: int | None = None,
               total_ms: int | None = None) -> SpiPlan:
    """Build the exact transaction for ``opcode``, without touching hardware.

    * read : selector transfer, then a receive-only transfer of ``read_len``
      bytes (the device clocks its answer out during that second transfer).
    * write: selector transfer, then the payload as a write transfer.
    """
    payload = bytes(payload)
    if operation == "read":
        if payload:
            raise ValueError("a read plan takes no payload")
        n = read_len if read_len is not None else 0
        if n <= 0:
            raise ValueError("read_len must be > 0")
        if len(payload) > n:
            raise ValueError("read payload longer than read_len")
        default_timeout, default_total = default_timeouts(opcode)
        sel = selector(opcode, write=False)
        transfers = (
            SpiTransfer(bytes([sel]), 0),
            SpiTransfer(b"", n),
        )
        return SpiPlan(
            opcode, "read", payload, n, sel, transfers,
            timeout_ms if timeout_ms is not None else default_timeout,
            total_ms if total_ms is not None else default_total)
    if operation == "write":
        default_timeout, default_total = default_timeouts(opcode)
        sel = selector(opcode, write=True)
        transfers = (SpiTransfer(bytes([sel]), 0),)
        if payload:
            transfers += (SpiTransfer(payload, 0),)
        return SpiPlan(
            opcode, "write", payload, 0, sel, transfers,
            timeout_ms if timeout_ms is not None else default_timeout,
            total_ms if total_ms is not None else default_total)
    raise ValueError(f"bad operation {operation!r}")


def build_shape_plan(opcode: int, *, selector_variant: str = "read",
                     padding: bytes = b"", read_len: int | None = None,
                     timeout_ms: int | None = None,
                     total_ms: int | None = None) -> SpiPlan:
    """Build one read with an experimental selector/padding write shape.

    This is the *only* way to put a selector other than
    ``(opcode & 0x3f) << 1`` on the wire, and it stays read-only: the
    direction bit (bit 7) is never set, only bit 0 can vary, and the caller
    still has to pass the transport's read allowlist and destructive-opcode
    gate.  ``read_len`` bytes are clocked after the selector plus ``padding``.
    """
    n = read_len if read_len is not None else 0
    if n <= 0:
        raise ValueError("read_len must be > 0")
    shape = ReadShape(selector_variant, padding)
    base = selector(opcode, write=False)
    sel = base | shape.selector_bit0
    default_timeout, default_total = default_timeouts(opcode)
    transfers = (
        SpiTransfer(bytes([sel]) + shape.padding, 0),
        SpiTransfer(b"", n),
    )
    return SpiPlan(
        opcode, "read", b"", n, sel, transfers,
        timeout_ms if timeout_ms is not None else default_timeout,
        total_ms if total_ms is not None else default_total,
        shape=shape)


def plan_command(command: "SpiCommand", **kwargs) -> SpiPlan:
    """Plan a named read-only command (``COMMANDS``)."""
    timeout_ms = kwargs.pop("timeout_ms", command.timeout_ms)
    total_ms = kwargs.pop("total_ms", command.total_ms)
    return build_plan(command.opcode, operation="read",
                      read_len=command.read_len,
                      timeout_ms=timeout_ms, total_ms=total_ms,
                      **kwargs)


@dataclasses.dataclass(frozen=True)
class SpiCommand:
    """One named, read-only command and the number of bytes it answers."""

    name: str
    opcode: int
    read_len: int
    timeout_ms: int = CMD_TIMEOUT_MS
    total_ms: int = CMD_TOTAL_MS
    description: str = ""


#: Read lengths from docs/hw/fingerprint.md (the answer layouts).  On the SPI
#: link these lengths are the caller's fixed read size; there is no length
#: field on the wire.  Timeouts are the recovered descriptor values.
COMMANDS: dict[str, SpiCommand] = {
    "fw": SpiCommand("fw", 0xA0, 7, description="firmware version / project"),
    "scheme": SpiCommand("scheme", 0xA1, 64,
                         description="module scheme table"),
    "count": SpiCommand("count", 0xB3, 4, description="template count (u32)"),
    "uid": SpiCommand("uid", 0xC0, 16, description="module UID"),
    "info": SpiCommand("info", 0xC1, 9,
                       description="sensor geometry + flags"),
    "status": SpiCommand("status", OP_STATUS_REG, 1,
                         description="recovered SPI status register"),
}


# ---------------------------------------------------------------------------
# Low-level spidev wrapper
# ---------------------------------------------------------------------------

class LinuxSpiIO:
    """``os.open``/``ioctl`` based spidev access, injectable for tests.

    Only ``open``/``configure``/``message``/``close`` are part of the
    interface :class:`SpiTransport` uses; ``transfer`` is a convenience
    wrapper kept for small callers and probes.
    """

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None
        self.speed_hz = 0
        self.mode = 0
        self.bits_per_word = 8
        self.last_transfers: tuple[SpiTransfer, ...] = ()
        self.last_message: tuple[bytes, ...] = ()

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self.fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR)
        self.fd = fd

    def configure(self, *, speed_hz: int | None = None,
                  mode: int | None = None,
                  bits_per_word: int | None = None) -> None:
        if self.fd is None:
            raise SpiError("configure() before open()")
        if mode is not None:
            fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("<B", mode & 0xFF))
            self.mode = mode & 0xFF
        if bits_per_word is not None:
            fcntl.ioctl(self.fd, SPI_IOC_WR_BITS_PER_WORD,
                        struct.pack("<B", bits_per_word & 0xFF))
            self.bits_per_word = bits_per_word & 0xFF
        if speed_hz is not None:
            fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ,
                        struct.pack("<I", speed_hz & 0xFFFFFFFF))
            self.speed_hz = speed_hz & 0xFFFFFFFF

    def message(self, transfers: tuple[SpiTransfer, ...]) -> tuple[bytes, ...]:
        """Submit one ``SPI_IOC_MESSAGE(n)``; return each receive buffer."""
        if self.fd is None:
            raise SpiError("message() before open()")
        transfers = tuple(transfers)
        if not transfers:
            raise SpiError("empty SPI message")
        if len(transfers) > 0xFFFF:
            raise SpiError("too many SPI transfers")
        keep = []
        descriptors = []
        for transfer in transfers:
            length = max(len(transfer.tx), transfer.rx_len)
            if length <= 0:
                raise SpiError("zero-length SPI transfer")
            if transfer.tx:
                tx_buf = ctypes.create_string_buffer(transfer.tx, length)
                tx_addr = ctypes.addressof(tx_buf)
            else:
                tx_buf = None
                tx_addr = 0
            if transfer.rx_len:
                rx_buf = ctypes.create_string_buffer(length)
                rx_addr = ctypes.addressof(rx_buf)
            else:
                rx_buf = None
                rx_addr = 0
            keep.append((tx_buf, rx_buf))
            descriptors.append(_SPI_IOC_TRANSFER.pack(
                tx_addr, rx_addr, length,
                transfer.speed_hz & 0xFFFFFFFF,
                transfer.delay_us & 0xFFFF,
                self.bits_per_word & 0xFF, 0, 0, 0, 0, 0))
        msg = b"".join(descriptors)
        fcntl.ioctl(self.fd, spi_ioc_message(len(transfers)), msg)
        received = tuple(
            bytes(rx_buf.raw[:transfer.rx_len]) if rx_buf is not None else b""
            for transfer, (_tx_buf, rx_buf) in zip(transfers, keep))
        self.last_transfers = transfers
        self.last_message = received
        return received

    def transfer(self, tx: bytes, rx_len: int | None = None,
                 speed_hz: int = 0, delay_us: int = 0) -> bytes:
        """One full-duplex transfer; returns ``rx_len`` received bytes."""
        transfer = SpiTransfer(bytes(tx), rx_len or 0, speed_hz, delay_us)
        return self.message((transfer,))[0]

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            finally:
                self.fd = None


# ---------------------------------------------------------------------------
# GPIO interrupt waiting
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class IrqEvent:
    """One observed interrupt wait result."""

    source: str                 # "gpiod", "chardev", "sysfs", "poll"
    elapsed_ms: float
    edge: str | None = None     # "rising"/"falling" when known
    value: int | None = None    # line level when known
    observed: bool = True       # False only for the time-based fallback
    timestamp_ns: int | None = None  # chardev event timestamp, when available

    @property
    def asserted(self) -> bool | None:
        """``True`` when the edge/level represents line assertion."""
        if self.edge == "rising" or self.value == 1:
            return True
        if self.edge == "falling" or self.value == 0:
            return False
        return None


class GpioIrqWaiter:
    """Wait for the sensor's interrupt line without touching it at import.

    Backend order: ``gpiod`` (libgpiod v1 or v2 Python bindings) -> ``chardev``
    (the GPIO v2 character device, no libgpiod needed) -> sysfs edge ->
    polling.  The sysfs path writes ``export``/``direction``/``edge`` (and
    ``active_low`` when the controller exposes it) under ``/sys/class/gpio``
    (root, and it may conflict with another owner); the polling fallback
    cannot see an edge and only waits out the timeout, so it warns loudly and
    reports ``observed=False``.

    ``backend`` may be ``"auto"`` (the order above, with an environment
    override) or one of ``gpiod``/``chardev``/``sysfs``/``poll`` to force a
    single backend.  ``$FPBF63112_GPIO_BACKEND`` overrides ``"auto"``;
    the probe's ``--gpio-backend`` passes the same names explicitly.

    ``edge`` is the *logical* assertion edge after ``active_low`` is applied:
    for an active-low line, a rising logical edge is the physical falling
    edge.  ACPI ``GpioInt`` polarity therefore maps to ``active_low``, not to
    a different edge name.  Level-triggered lines cannot be represented by
    libgpiod's edge API; the caller should pass ``both`` and document that
    limitation.
    """

    def __init__(self, line: int, *, chip: str | None = None,
                 edge: str = "rising", active_low: bool = False,
                 backend: str = "auto",
                 sysfs_base: str = "/sys/class/gpio",
                 chardev_dir: str = "/dev",
                 gpiod_module=None,
                 warn=warnings.warn,
                 clock=time.monotonic, sleep=time.sleep):
        if edge not in ("rising", "falling", "both"):
            raise ValueError(f"bad edge {edge!r}")
        self.line = line
        self.chip = chip
        self.edge = edge
        self.active_low = active_low
        self.backend = _gpio_backend(backend)
        if self.backend not in (("auto",) + IRQ_BACKENDS):
            raise ValueError(
                f"unknown IRQ backend {self.backend!r}; choose auto, "
                f"{', '.join(IRQ_BACKENDS)}, or set ${GPIO_BACKEND_ENV}")
        self.sysfs_base = sysfs_base
        self.chardev_dir = chardev_dir
        self._gpiod_module = gpiod_module
        self._warn = warn
        self._clock = clock
        self._sleep = sleep
        self._impl = None
        self._closed = False

    # -- backend selection -------------------------------------------------
    def _open_impl(self):
        if self._impl is not None:
            return self._impl
        order = (list(IRQ_BACKENDS) if self.backend == "auto"
                 else [self.backend])
        errors = []
        for candidate in order:
            try:
                if candidate == "gpiod":
                    self._impl = _GpiodWaiter(
                        self.line, self.chip, self.edge, self.active_low,
                        self._gpiod_module)
                elif candidate == "chardev":
                    self._impl = _ChardevWaiter(
                        self.line, self.chip, self.edge, self.active_low,
                        dev_dir=self.chardev_dir)
                elif candidate == "sysfs":
                    self._impl = _SysfsWaiter(
                        self.line, self.edge, self.active_low,
                        self.sysfs_base)
                elif candidate == "poll":
                    self._impl = _PollWaiter(self._clock, self._sleep)
                else:
                    raise ValueError(f"unknown IRQ backend {candidate!r}")
                return self._impl
            except (ImportError, OSError, SpiError) as exc:
                errors.append(f"{candidate}: {exc}")
                continue
        raise IrqUnavailable("no GPIO backend available (" +
                             "; ".join(errors) + ")")

    def wait(self, timeout_ms: int | None = None) -> IrqEvent:
        """Wait up to ``timeout_ms`` for the line to fire."""
        timeout_ms = IRQ_WAIT_MS if timeout_ms is None else timeout_ms
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")
        impl = self._open_impl()
        started = self._clock()
        if isinstance(impl, _PollWaiter):
            self._warn(
                "no gpiod/chardev/sysfs GPIO backend: falling back to a "
                "time-based poll; the interrupt line cannot be observed, so a "
                "timeout here does not prove the device is idle",
                RuntimeWarning, stacklevel=2)
            impl.wait(timeout_ms / 1000.0)
            return IrqEvent(source="poll",
                            elapsed_ms=(self._clock() - started) * 1000.0,
                            observed=False)
        edge, value = impl.wait(timeout_ms / 1000.0)
        elapsed = (self._clock() - started) * 1000.0
        if edge is None and value is None:
            raise SpiTimeout(f"no interrupt on GPIO {self.line} within "
                             f"{timeout_ms} ms")
        last = getattr(impl, "last_event", None)
        return IrqEvent(source=impl.name, elapsed_ms=elapsed,
                        edge=edge, value=value,
                        timestamp_ns=getattr(last, "timestamp_ns", None))

    def close(self) -> None:
        if self._impl is not None and not self._closed:
            self._impl.close()
            self._closed = True


class _GpiodWaiter:
    name = "gpiod"

    def __init__(self, line: int, chip: str | None, edge: str,
                 active_low: bool, gpiod_module=None):
        if gpiod_module is None:
            import gpiod as gpiod_module
        self._gpiod = gpiod_module
        self._line = line
        self._chip = chip
        self._edge = edge
        self._active_low = active_low
        self._request = None
        # libgpiod v1 and v2 have incompatible Python APIs; decide by symbol.
        if hasattr(gpiod_module, "request_lines"):
            self._version = 2
            self._open_v2()
        elif (hasattr(gpiod_module, "Chip")
              and hasattr(gpiod_module, "LINE_REQ_EV_EDGE")):
            self._version = 1
            self._open_v1()
        else:
            raise SpiError("unrecognised gpiod Python binding")

    def _open_v1(self) -> None:
        gpiod = self._gpiod
        chip = gpiod.Chip(self._chip or "gpiochip0")
        line = chip.get_line(self._line)
        flags = gpiod.LINE_REQ_FLAG_ACTIVE_LOW if self._active_low else 0
        line.request(consumer="fpbf63112", type=gpiod.LINE_REQ_EV_EDGE,
                     flags=flags)
        self._request = (chip, line)

    def _open_v2(self) -> None:
        gpiod = self._gpiod
        edge = {
            "rising": gpiod.Edge.RISING,
            "falling": gpiod.Edge.FALLING,
            "both": gpiod.Edge.BOTH,
        }[self._edge]
        settings = gpiod.LineSettings(edge_detection=edge,
                                      active_low=self._active_low)
        self._request = gpiod.request_lines(
            self._chip or "gpiochip0", consumer="fpbf63112",
            config={self._line: settings})

    def wait(self, timeout_s: float):
        if self._version == 1:
            _chip, line = self._request
            if line.event_wait(timeout=timeout_s):
                ev = line.event_read()
                return ("rising"
                        if ev.type == self._gpiod.EdgeEventType.RISING_EDGE
                        else "falling"), None
            return None, None
        if self._request.wait_edge_events(timeout_s):
            events = self._request.read_edge_events()
            for ev in events:
                if ev.event_type == self._gpiod.EdgeEventType.RISING_EDGE:
                    return "rising", None
                if ev.event_type == self._gpiod.EdgeEventType.FALLING_EDGE:
                    return "falling", None
        return None, None

    def close(self) -> None:
        if self._request is not None:
            try:
                if self._version == 1:
                    _chip, line = self._request
                    line.release()
                    _chip.close()
                else:
                    self._request.release()
            finally:
                self._request = None


class _SysfsWaiter:
    """Edge wait through /sys/class/gpio (root; writes export/edge).

    The kernel's sysfs ABI does not report which edge woke the wait, so this
    backend samples the level before and after the wakeup and derives the
    transition.  That is enough to tell rising from falling for an
    edge-triggered line, but a level-triggered line (or a missed edge between
    the two samples) should be treated as best-effort.  ``active_low`` is
    written to the controller's ``active_low`` attribute when present and
    emulated in software otherwise.
    """

    name = "sysfs"

    def __init__(self, line: int, edge: str, active_low: bool = False,
                 base: str = "/sys/class/gpio"):
        if not os.path.isdir(base):
            raise SpiError(f"{base} is not available")
        self._base = base
        gpio = os.path.join(base, f"gpio{line}")
        self._exported_here = False
        if not os.path.isdir(gpio):
            self._write(os.path.join(base, "export"), str(line))
            self._exported_here = True
            if not _wait_path(gpio, 0.5):
                raise SpiError(f"gpio{line} did not appear after export")
        self._gpio = gpio
        self._value_path = os.path.join(gpio, "value")
        self._write(os.path.join(gpio, "direction"), "in")
        self._write(os.path.join(gpio, "edge"), edge)
        self._active_low = active_low
        self._hw_active_low = False
        active_low_path = os.path.join(gpio, "active_low")
        if os.path.exists(active_low_path):
            try:
                self._write(active_low_path, "1" if active_low else "0")
                self._hw_active_low = True
            except OSError:
                self._hw_active_low = False
        self._last = self._read_value()

    def _read_value(self) -> int:
        with open(self._value_path, "r", encoding="ascii") as fh:
            raw = fh.read().strip()
        if raw not in ("0", "1"):
            raise SpiError(f"unexpected sysfs value {raw!r}")
        value = int(raw)
        if self._active_low and not self._hw_active_low:
            value = 1 - value
        return value

    @staticmethod
    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(text + "\n")

    def wait(self, timeout_s: float):
        with open(self._value_path, "r", encoding="ascii") as fh:
            ready, _, _ = select.select([fh], [], [], timeout_s)
            if not ready:
                return None, None
        value = self._read_value()
        previous = self._last
        self._last = value
        if value == previous:
            # A level-triggered wake (or a missed edge): level is real, edge
            # type is not.
            return None, value
        return ("rising" if value > previous else "falling"), value

    def close(self) -> None:
        if self._exported_here:
            try:
                self._write(os.path.join(self._base, "unexport"),
                            str(os.path.basename(self._gpio)[4:]))
            except OSError:
                pass


class _PollWaiter:
    """Fallback: no line access; wait out the timeout and report nothing."""

    name = "poll"

    def __init__(self, clock, sleep):
        self._clock = clock
        self._sleep = sleep

    def wait(self, timeout_s: float):
        self._sleep(timeout_s)
        return None, None

    def close(self) -> None:
        pass


def _wait_path(path: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.01)
    return False


class GpioResetLine:
    """Output line used to pulse the sensor reset (only when configured).

    ``active_low`` is the electrical polarity of the reset input.  The
    backend receives the logical values 1 (asserted) and 0 (idle).  gpiod
    applies it through request flags, sysfs through ``active_low`` when the
    kernel exposes it, and the chardev backend in software (it never sets
    ``GPIO_V2_LINE_FLAG_ACTIVE_LOW``; it writes the physical inverted level).

    Backend order: ``gpiod`` -> ``chardev`` -> sysfs.  ``backend`` may force
    one, and ``$FPBF63112_GPIO_BACKEND`` overrides ``auto``.
    """

    def __init__(self, line: int, *, chip: str | None = None,
                 active_low: bool = True, backend: str = "auto",
                 sysfs_base: str = "/sys/class/gpio",
                 chardev_dir: str = "/dev",
                 gpiod_module=None, sleep=time.sleep):
        self.line = line
        self.chip = chip
        self.active_low = active_low
        self.backend = _gpio_backend(backend)
        if self.backend not in (("auto",) + RESET_BACKENDS):
            raise ValueError(
                f"unknown reset backend {self.backend!r}; choose auto, "
                f"{', '.join(RESET_BACKENDS)}, or set ${GPIO_BACKEND_ENV}")
        self.sysfs_base = sysfs_base
        self.chardev_dir = chardev_dir
        self._gpiod_module = gpiod_module
        self._sleep = sleep
        self._impl = None

    def _open(self):
        if self._impl is not None:
            return self._impl
        order = (list(RESET_BACKENDS) if self.backend == "auto"
                 else [self.backend])
        errors = []
        for candidate in order:
            try:
                if candidate == "gpiod":
                    self._impl = _GpiodOutput(
                        self.line, self.chip, self.active_low,
                        self._gpiod_module)
                elif candidate == "chardev":
                    self._impl = _ChardevOutput(
                        self.line, self.chip, self.active_low,
                        dev_dir=self.chardev_dir)
                elif candidate == "sysfs":
                    self._impl = _SysfsOutput(
                        self.line, self.active_low, self.sysfs_base)
                else:
                    raise ValueError(f"unknown reset backend {candidate!r}")
                return self._impl
            except (ImportError, OSError, SpiError) as exc:
                errors.append(f"{candidate}: {exc}")
        raise IrqUnavailable("no GPIO output backend (" + "; ".join(errors) +
                             ")")

    def pulse(self, assert_ms: int = RESET_ASSERT_MS,
              release_ms: int = RESET_RELEASE_MS) -> None:
        """Assert (logical 1), sleep ``assert_ms``, release, sleep."""
        impl = self._open()
        impl.set(1)
        self._sleep(assert_ms / 1000.0)
        impl.set(0)
        self._sleep(release_ms / 1000.0)

    def close(self) -> None:
        if self._impl is not None:
            self._impl.close()


class _GpiodOutput:
    def __init__(self, line: int, chip: str | None, active_low: bool,
                 gpiod_module=None):
        if gpiod_module is None:
            import gpiod as gpiod_module
        self._gpiod = gpiod_module
        self._line = line
        self._version = 2 if hasattr(gpiod_module, "request_lines") else 1
        if self._version == 1:
            gpiod = gpiod_module
            c = gpiod.Chip(chip or "gpiochip0")
            ln = c.get_line(line)
            flags = (gpiod.LINE_REQ_FLAG_ACTIVE_LOW if active_low else 0)
            ln.request(consumer="fpbf63112-reset", type=gpiod.LINE_REQ_DIR_OUT,
                       flags=flags)
            self._h = (c, ln)
        else:
            settings = gpiod_module.LineSettings(
                direction=gpiod_module.Direction.OUTPUT,
                output_value=gpiod_module.Value.INACTIVE,
                active_low=active_low)
            self._h = gpiod_module.request_lines(
                chip or "gpiochip0", consumer="fpbf63112-reset",
                config={line: settings})

    def set(self, value: int) -> None:
        if self._version == 1:
            self._h[1].set_value(1 if value else 0)
        else:
            self._h.set_value(
                self._line,
                self._gpiod.Value.ACTIVE if value
                else self._gpiod.Value.INACTIVE)

    def close(self) -> None:
        if self._version == 1:
            c, ln = self._h
            ln.release()
            c.close()
        else:
            self._h.release()


class _SysfsOutput:
    def __init__(self, line: int, active_low: bool = False,
                 base: str = "/sys/class/gpio"):
        if not os.path.isdir(base):
            raise SpiError(f"{base} is not available")
        self._base = base
        gpio = os.path.join(base, f"gpio{line}")
        self._exported_here = False
        if not os.path.isdir(gpio):
            _SysfsWaiter._write(os.path.join(base, "export"), str(line))
            self._exported_here = True
        self._gpio = gpio
        _SysfsWaiter._write(os.path.join(gpio, "direction"), "out")
        self._active_low = active_low
        self._hw_active_low = False
        active_low_path = os.path.join(gpio, "active_low")
        if os.path.exists(active_low_path):
            try:
                _SysfsWaiter._write(active_low_path,
                                    "1" if active_low else "0")
                self._hw_active_low = True
            except OSError:
                self._hw_active_low = False
        self._value = os.path.join(gpio, "value")

    def set(self, value: int) -> None:
        physical = 1 if value else 0
        if self._active_low and not self._hw_active_low:
            physical = 1 - physical
        _SysfsWaiter._write(self._value, "1" if physical else "0")

    def close(self) -> None:
        if self._exported_here:
            try:
                _SysfsWaiter._write(os.path.join(self._base, "unexport"),
                                    str(os.path.basename(self._gpio)[4:]))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

class SpiTransport:
    """Safe-by-default SPI transport (see the module docstring).

    Construction stores configuration only; no file is opened and no GPIO is
    touched.  :meth:`open` (called lazily from :meth:`execute`) opens the
    spidev node; :meth:`execute` refuses unless ``allow_writes=True`` and the
    opcode is on the allowlist.
    """

    def __init__(self, spidev_path: str | None = None, *,
                 speed_hz: int | None = None,
                 mode: int | None = None,
                 bits_per_word: int | None = None,
                 allow_writes: bool = False,
                 read_allowlist=None,
                 write_allowlist=None,
                 irq: GpioIrqWaiter | None = None,
                 reset: GpioResetLine | None = None,
                 allow_reset: bool = False,
                 reset_settle_ms: int | None = None,
                 spi_io=None,
                 clock=time.monotonic,
                 sleep=time.sleep):
        # Deliberately no I/O here.
        self.spidev_path = spidev_path
        self.speed_hz = speed_hz
        self.mode = mode
        self.bits_per_word = bits_per_word
        self.allow_writes = bool(allow_writes)
        self.read_allowlist = (frozenset(SAFE_READ_OPS) if read_allowlist is None
                               else frozenset(read_allowlist))
        self.write_allowlist = (frozenset(SAFE_WRITE_OPS) if write_allowlist is None
                                else frozenset(write_allowlist))
        self.irq = irq
        self.reset_line = reset
        self.allow_reset = bool(allow_reset)
        self.reset_settle_ms = (None if reset_settle_ms is None
                                else int(reset_settle_ms))
        self._io = spi_io
        self._clock = clock
        self._sleep = sleep
        self._opened = False
        self._configured = False
        self._cancel_requested = False
        self.last_irq: IrqEvent | None = None

    # -- introspection -----------------------------------------------------
    @property
    def opened(self) -> bool:
        return self._opened

    def describe(self) -> str:
        speed = self.speed_hz if self.speed_hz is not None else "from _CRS"
        mode = self.mode if self.mode is not None else "from _CRS"
        bits = (self.bits_per_word if self.bits_per_word is not None
                else "from _CRS")
        return (f"spidev={self.spidev_path or '(unset)'} speed={speed} "
                f"mode={mode} bits={bits} allow_writes={self.allow_writes} "
                f"irq={'yes' if self.irq else 'no'} "
                f"reset={'yes' if self.reset_line else 'no'}")

    # -- safety gates ------------------------------------------------------
    def _check(self, plan: SpiPlan) -> None:
        if plan.opcode in DESTRUCTIVE_OPS:
            raise UnsafeCommand(
                f"opcode 0x{plan.opcode:02x} is "
                f"{DESTRUCTIVE_OPS[plan.opcode]}: refusing unconditionally")
        # Read-shape plans may set selector bit 0, so gate on bit 0 cleared:
        # a variant selector must not alias a destructive opcode's selector
        # either (e.g. 0x05 is still JTAG's 0x04 with the read flag set).
        low = plan.selector & 0x7E
        if low in FORBIDDEN_SELECTORS:
            aliases = FORBIDDEN_SELECTORS[low]
            names = ", ".join(f"0x{op:02x} {DESTRUCTIVE_OPS[op]}"
                              for op in aliases)
            raise UnsafeCommand(
                f"selector 0x{low:02x} (opcode 0x{plan.opcode:02x}) is "
                f"reserved for {names}: refusing unconditionally")
        if not self.allow_writes:
            raise CommandNotAllowed(
                "refusing to send any command without allow_writes=True "
                "(the SPI transport is write-gated even for reads)")
        allowed = (self.read_allowlist if plan.operation == "read"
                   else self.write_allowlist)
        if plan.opcode not in allowed:
            raise CommandNotAllowed(
                f"opcode 0x{plan.opcode:02x} ({plan.operation}) is not on the "
                f"allowlist; allowed: "
                f"{', '.join(f'0x{b:02x}' for b in sorted(allowed)) or '(none)'}")

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self._opened:
            return
        if not self.spidev_path:
            raise SpiError("no spidev path configured; refusing to guess one")
        if self._io is None:
            self._io = LinuxSpiIO(self.spidev_path)
        try:
            self._io.open()
            self._io.configure(speed_hz=self.speed_hz, mode=self.mode,
                               bits_per_word=self.bits_per_word)
        except OSError as exc:
            try:
                self._io.close()
            except OSError:
                pass
            raise SpiError(f"cannot open/configure {self.spidev_path}: "
                           f"{exc}") from exc
        self._opened = True
        if self.reset_line is not None and self.allow_reset:
            self.reset_line.pulse()
            # FUN_180016260: Sleep(0x32) after the reset target is opened.
            # The bring-up run settled on 10 ms after an active-high pulse, so
            # an explicit value overrides the recovered 50 ms default.
            settle = (RESET_OPEN_MS if self.reset_settle_ms is None
                      else self.reset_settle_ms)
            if settle:
                self._sleep(settle / 1000.0)

    def close(self) -> None:
        if self._io is not None:
            try:
                self._io.close()
            except OSError:
                pass
        if self.irq is not None:
            self.irq.close()
        if self.reset_line is not None:
            self.reset_line.close()
        self._opened = False
        self._configured = False

    def __enter__(self) -> "SpiTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- planning ----------------------------------------------------------
    def plan(self, opcode: int, *, operation: str = "read",
             payload: bytes = b"", read_len: int | None = None,
             timeout_ms: int | None = None,
             total_ms: int | None = None) -> SpiPlan:
        return build_plan(opcode, operation=operation, payload=payload,
                          read_len=read_len, timeout_ms=timeout_ms,
                          total_ms=total_ms)

    def plan_command(self, name: str, **kwargs) -> SpiPlan:
        try:
            command = COMMANDS[name]
        except KeyError:
            raise SpiError(f"unknown command {name!r}; choose from "
                           f"{', '.join(COMMANDS)}") from None
        return plan_command(command, **kwargs)

    # -- cancel ------------------------------------------------------------
    def request_cancel(self) -> None:
        """Ask the next transaction stage to abort (checked at boundaries)."""
        self._cancel_requested = True

    def cancel(self, settle_ms: int = CANCEL_SETTLE_MS) -> None:
        """Apply the driver's cancel policy: abort, then settle 150 ms.

        ``FUN_180013b60`` resets the request events, signals completion and
        waits 150 ms before completing the pending request as cancelled.
        """
        self._cancel_requested = True
        self._sleep(settle_ms / 1000.0)

    def _check_cancel(self) -> None:
        if self._cancel_requested:
            self._cancel_requested = False
            raise Cancelled("transaction cancelled by the caller")

    # -- execution ---------------------------------------------------------
    def _elapsed_ms(self, started: float) -> float:
        return (self._clock() - started) * 1000.0

    def _check_deadline(self, plan: SpiPlan, started: float, stage: str) -> None:
        elapsed = self._elapsed_ms(started)
        # The total deadline is the outer bound; if it fired, report it even
        # when the (shorter) per-attempt bound would also have fired.
        if elapsed > plan.total_ms:
            raise SpiTimeout(
                f"opcode 0x{plan.opcode:02x} took {elapsed:.0f} ms > "
                f"{plan.total_ms} ms total timeout ({stage})")
        if elapsed > plan.timeout_ms:
            raise SpiTimeout(
                f"opcode 0x{plan.opcode:02x} took {elapsed:.0f} ms > "
                f"{plan.timeout_ms} ms per-attempt timeout ({stage})")

    def execute(self, plan: SpiPlan) -> bytes:
        """Run one plan and return the device's answer bytes.

        A read returns exactly ``plan.read_len`` bytes (the second transfer's
        receive buffer); a write returns ``b""``.  The per-attempt and total
        deadlines cover opening, the transfer and the optional IRQ wait.
        spidev itself performs a synchronous, kernel-bounded ioctl, so a
        kernel hang cannot be interrupted here.
        """
        self._check(plan)
        self._check_cancel()
        self.open()
        started = self._clock()
        self._check_deadline(plan, started, "before transfer")
        try:
            received = self._io.message(plan.transfers)
        except OSError as exc:
            raise SpiError(
                f"SPI transfer for opcode 0x{plan.opcode:02x} failed: "
                f"{exc}") from exc
        answer = b"".join(received)
        self._check_cancel()
        self._check_deadline(plan, started, "after transfer")
        if self.irq is not None:
            remaining = min(plan.timeout_ms, plan.total_ms) \
                - self._elapsed_ms(started)
            if remaining <= 0:
                raise SpiTimeout(
                    f"opcode 0x{plan.opcode:02x} timed out before the IRQ "
                    f"wait ({plan.timeout_ms} ms)")
            self.last_irq = self.irq.wait(int(remaining))
            self._check_deadline(plan, started, "after IRQ")
        if plan.operation == "read":
            if len(answer) < plan.read_len:
                raise SpiError(
                    f"short SPI read for opcode 0x{plan.opcode:02x}: "
                    f"{len(answer)} of {plan.read_len} bytes")
            return answer[:plan.read_len]
        return b""

    # -- command helpers ---------------------------------------------------
    def read(self, opcode: int, length: int, *,
             timeout_ms: int | None = None,
             total_ms: int | None = None) -> bytes:
        """Read ``length`` bytes for ``opcode`` (exactly the answer bytes)."""
        plan = self.plan(opcode, operation="read", read_len=length,
                         timeout_ms=timeout_ms, total_ms=total_ms)
        return self.execute(plan)

    def read_command(self, name: str) -> bytes:
        command = COMMANDS[name]
        return self.read(command.opcode, command.read_len,
                         timeout_ms=command.timeout_ms,
                         total_ms=command.total_ms)

    def write(self, opcode: int, payload: bytes, *,
              timeout_ms: int | None = None,
              total_ms: int | None = None) -> None:
        """Write ``payload`` for ``opcode`` (requires the write allowlist)."""
        plan = self.plan(opcode, operation="write", payload=payload,
                         timeout_ms=timeout_ms, total_ms=total_ms)
        self.execute(plan)

    def wait_irq(self, timeout_ms: int | None = None) -> IrqEvent:
        """Wait for the interrupt line (no SPI traffic)."""
        if self.irq is None:
            raise IrqUnavailable("no IRQ line configured")
        return self.irq.wait(timeout_ms)

    def capture_wait(self, timeout_ms: int = IRQ_WAIT_MS,
                     attempts: int = IRQ_ATTEMPTS) -> IrqEvent:
        """The driver's capture wait: up to 3 x 100 ms, 50 ms apart.

        Mirrors ``FUN_18000e588``'s ``ResetEvent`` + ``WaitForSingleObject
        (..., 100)`` loop with ``Sleep(0x32)`` between attempts.  A
        time-based poll result is not an observation, so it counts as a
        timeout here.
        """
        last: SpiTimeout | None = None
        for attempt in range(attempts):
            try:
                event = self.wait_irq(timeout_ms)
            except SpiTimeout as exc:
                last = exc
            else:
                if event.observed:
                    return event
                last = SpiTimeout(
                    f"no observable interrupt on {event.source} within "
                    f"{timeout_ms} ms")
            if attempt + 1 < attempts:
                self._sleep(IRQ_RETRY_SLEEP_MS / 1000.0)
        raise last or SpiTimeout("no interrupt")


# ---------------------------------------------------------------------------
# Convenience: a probe-safe transport built from parsed ACPI resources
# ---------------------------------------------------------------------------

def transport_from_acpi(connection, *, spidev_path: str,
                        allow_writes: bool = False,
                        irq: GpioIrqWaiter | None = None,
                        reset: GpioResetLine | None = None,
                        allow_reset: bool = False) -> SpiTransport:
    """Build a :class:`SpiTransport` using ``acpi.SpiConnection`` values.

    Missing values stay ``None`` so the caller (the probe) can print exactly
    which parameters still need a privileged ``_CRS`` read instead of
    silently guessing a clock or mode.
    """
    return SpiTransport(
        spidev_path,
        speed_hz=connection.speed_hz,
        mode=connection.mode,
        bits_per_word=connection.bits_per_word,
        allow_writes=allow_writes,
        irq=irq,
        reset=reset,
        allow_reset=allow_reset,
    )
