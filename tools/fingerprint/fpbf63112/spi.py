"""BF63112 SPI (ACPI/SPB) transport - the wire format the sensor really uses.

Recovered from ``BetterlifeFingerprintDevice.dll`` 9.39.15.26 (the ACPI/SPB
branch, not the USB branch):

* every transaction starts with a one-byte **selector**:

      selector = (opcode & 0x3f) << 1              # host reads from device
      selector = 0x80 | ((opcode & 0x3f) << 1)     # host writes to device

  i.e. bit 7 is the direction (1 = write), bits 6..1 are the low six opcode
  bits, bit 0 is always zero.  This is ``FUN_180017020`` (read, selector
  ``(x & 0x3f) * 2``) and ``FUN_180017404`` (write, ``(x | 0xc0) * 2``);
  the two forms differ by the 0x80 bit.
* a **read** is a two-entry SPB sequence: write the 1-byte selector, then read
  ``len`` bytes into the caller's buffer (``FUN_18001197c`` builds the two
  transfers, ``FUN_180017020`` issues and waits).  There is no length field
  and no leading status byte on the wire: the caller passes the exact length
  of the register/data it wants.
* a **write** is one transfer: the selector followed by ``len`` payload bytes
  (``FUN_180017404``).
* completion is the WDF request completion event
  (``FUN_1800113a0`` -> ``SetEvent``), waited with 200 ms (bulk/data) or
  300 ms (small register) timeouts in the driver.  A separate GPIO interrupt
  (``GpioInt`` in ``_CRS``) signals "device has status"; the driver's DPC
  (``FUN_180016450``) then *reads a one-byte status register over SPI* and
  compares it with an armed mask before signalling the higher layer.  The
  line's edge/level and polarity are not in the binary - they come from ACPI.
* the first command lazily opens a reset target and sleeps 50 ms
  (``FUN_180016260`` -> ``FUN_180011550``); recovery pulses the reset line
  with 5 ms/10 ms sleeps (``FUN_180016110``/``FUN_180016a10``).

Safety design
-------------
``SpiTransport`` does nothing at construction time.  It refuses to open the
device and send anything unless the caller passed ``allow_writes=True`` *and*
the opcode is on an explicit allowlist.  Enrol/identify/remove/JTAG/restore/
firmware-update opcodes are refused unconditionally.  ``--dry-run`` in the
probe uses :func:`build_plan` only, which performs no I/O.
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


#: Recovered SPI register that the driver reads/writes for capture mode and
#: interrupt masking (``FUN_18000e588``: read 0x2d, set bit 0x10, write it
#: back, later restore it).
OP_STATUS_REG = 0x2D

#: Write-only command opcodes recovered from the same function (the value
#: written to the mode register is the *payload*, not the opcode).
#: Kept for documentation only; not allowlisted.
OP_MODE = 0x2D

#: Destructive / forbidden opcodes: never sent, even if a caller lists them.
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

#: Read-only commands that a probe may send.  These are the *USB* command
#: table opcodes; the SPI branch has its own, data-driven opcode set, so on
#: SPI they are deliberate read-only experiments (docs/hw/fingerprint-
#: transport.md §8 step 2) plus the one recovered SPI register read (0x2d).
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


@dataclasses.dataclass(frozen=True)
class SpiCommand:
    """One named, read-only command and the number of bytes it answers."""

    name: str
    opcode: int
    read_len: int
    timeout_ms: int = 300
    description: str = ""


#: Read lengths from docs/hw/fingerprint.md (the answer layouts).  On the SPI
#: link these lengths are the caller's fixed read size; there is no length
#: field on the wire.
COMMANDS: dict[str, SpiCommand] = {
    "fw": SpiCommand("fw", 0xA0, 7, 300, "firmware version / project"),
    "scheme": SpiCommand("scheme", 0xA1, 64, 300, "module scheme table"),
    "count": SpiCommand("count", 0xB3, 4, 300, "template count (u32)"),
    "uid": SpiCommand("uid", 0xC0, 16, 300, "module UID"),
    "info": SpiCommand("info", 0xC1, 9, 300, "sensor geometry + flags"),
    "status": SpiCommand("status", OP_STATUS_REG, 1, 300,
                         "recovered SPI status register"),
}

#: Recovered driver timings (milliseconds).
XFER_TIMEOUT_MS = 300          # FUN_180017020 / FUN_180017404 small transfers
DATA_TIMEOUT_MS = 200          # FUN_180017020 bulk image read
IRQ_WAIT_MS = 100              # FUN_18000e588 WaitForSingleObject(..., 100)
IRQ_ATTEMPTS = 3               # retry loop in FUN_18000e588
IRQ_RETRY_SLEEP_MS = 50        # Sleep(0x32) between attempts
RESET_ASSERT_MS = 5            # FUN_180016110: Sleep(5)
RESET_RELEASE_MS = 10          # FUN_180016110: Sleep(10)
RESET_OPEN_MS = 50             # FUN_180016260: Sleep(0x32) after target open
CANCEL_SETTLE_MS = 150         # FUN_180013b60: Sleep(0x96)/Wait(...,0x96)

SPI_ACPI_EXPECTATIONS = """\
The BF63112 SPI connection is described by _SB.PC00.SPI1.FPRT._CRS:
  * SpiSerialBus (type 2): ConnectionSpeed (Hz), DataBitLength (8/16),
    ClockPhase (0/1), ClockPolarity (0/1), DeviceSelection (CS index),
    ResourceSource (the SPI controller, e.g. \\_SB.PC00.SPI1).
  * GpioInt: the sensor interrupt line (PinIndex + triggering/polarity).
  * optional GpioIo: the reset line the Windows driver drives through the
    resource hub (if absent, the module must come up out of reset).
The driver package itself contains no clock, mode, bit width or GPIO number."""


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


# ---------------------------------------------------------------------------
# Plan: exactly what a transaction puts on the wire
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SpiPlan:
    """One transaction, fully determined before any I/O happens.

    ``tx`` is the byte string handed to spidev; ``rx_len`` is the size of the
    full-duplex receive buffer (always ``len(tx)`` for a single spidev
    transfer; a read plan pads the selector with zeros so the device clocks
    out its answer in the same transfer).
    """

    opcode: int
    operation: str            # "read" or "write"
    payload: bytes
    read_len: int
    selector: int
    tx: bytes
    rx_len: int
    timeout_ms: int

    def __post_init__(self) -> None:
        if self.operation not in ("read", "write"):
            raise ValueError(f"bad operation {self.operation!r}")
        if self.selector != selector(self.opcode, self.operation == "write"):
            raise ValueError("selector does not match opcode/direction")
        if self.rx_len < len(self.tx):
            raise ValueError("rx_len smaller than tx")
        if self.operation == "read" and self.tx[0] != self.selector:
            raise ValueError("read plan must start with the selector")
        if self.operation == "write" and self.tx[0] != self.selector:
            raise ValueError("write plan must start with the selector")

    def hexdump(self) -> str:
        return self.tx.hex(" ")


def build_plan(opcode: int, *, operation: str = "read",
               payload: bytes = b"", read_len: int | None = None,
               timeout_ms: int | None = None) -> SpiPlan:
    """Build the exact bytes for one transaction, without touching hardware.

    * read : ``selector`` then ``read_len`` zero bytes (full-duplex); the
      answer is the corresponding receive bytes.
    * write: ``selector | 0x80`` then ``payload``.
    """
    payload = bytes(payload)
    if operation == "read":
        n = len(payload) if read_len is None else read_len
        if n < 0:
            raise ValueError("read_len must be >= 0")
        sel = selector(opcode, write=False)
        tx = bytes([sel]) + bytes(n)
        return SpiPlan(opcode, "read", payload, n, sel, tx,
                       len(tx), timeout_ms if timeout_ms is not None
                       else XFER_TIMEOUT_MS)
    if operation == "write":
        sel = selector(opcode, write=True)
        tx = bytes([sel]) + payload
        return SpiPlan(opcode, "write", payload, len(payload), sel, tx,
                       len(tx), timeout_ms if timeout_ms is not None
                       else XFER_TIMEOUT_MS)
    raise ValueError(f"bad operation {operation!r}")


def plan_command(command: SpiCommand, **kwargs) -> SpiPlan:
    """Plan a named read-only command (``COMMANDS``)."""
    return build_plan(command.opcode, operation="read",
                      read_len=command.read_len,
                      timeout_ms=kwargs.pop("timeout_ms", command.timeout_ms),
                      **kwargs)


# ---------------------------------------------------------------------------
# Low-level spidev wrapper
# ---------------------------------------------------------------------------

class LinuxSpiIO:
    """``os.open``/``ioctl`` based spidev access, injectable for tests.

    Only ``open``/``configure``/``transfer``/``close`` are part of the
    interface :class:`SpiTransport` uses.
    """

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None
        self.speed_hz = 0
        self.mode = 0
        self.bits_per_word = 8

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

    def transfer(self, tx: bytes, rx_len: int | None = None,
                 speed_hz: int = 0, delay_us: int = 0) -> bytes:
        """One full-duplex transfer; returns ``rx_len`` received bytes."""
        if self.fd is None:
            raise SpiError("transfer() before open()")
        tx = bytes(tx)
        n = max(len(tx), rx_len or 0, 1)
        tx_buf = bytearray(tx) + bytearray(n - len(tx))
        rx_buf = bytearray(n)
        tx_addr = ctypes.addressof((ctypes.c_char * n).from_buffer(tx_buf))
        rx_addr = ctypes.addressof((ctypes.c_char * n).from_buffer(rx_buf))
        msg = _SPI_IOC_TRANSFER.pack(tx_addr, rx_addr, n, speed_hz, delay_us,
                                     self.bits_per_word, 0, 0, 0, 0, 0)
        fcntl.ioctl(self.fd, SPI_IOC_MESSAGE_1, msg)
        return bytes(rx_buf[:rx_len or n])

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


# ---------------------------------------------------------------------------
# GPIO interrupt waiting
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class IrqEvent:
    """One observed interrupt wait result."""

    source: str                 # "gpiod", "sysfs", "poll", "unavailable"
    elapsed_ms: float
    edge: str | None = None     # "rising"/"falling" when known
    value: int | None = None    # line level when known


class GpioIrqWaiter:
    """Wait for the sensor's interrupt line without touching it at import.

    Backend order: ``gpiod`` (libgpiod v1 or v2 Python bindings) -> sysfs
    edge -> polling.  The sysfs path writes ``export``/``direction``/``edge``
    under ``/sys/class/gpio`` (root, and it may conflict with another owner);
    the polling fallback cannot see an edge and only waits out the timeout,
    so it warns loudly.
    """

    def __init__(self, line: int, *, chip: str | None = None,
                 edge: str = "rising", active_low: bool = False,
                 backend: str = "auto",
                 warn=warnings.warn,
                 clock=time.monotonic, sleep=time.sleep):
        if edge not in ("rising", "falling", "both"):
            raise ValueError(f"bad edge {edge!r}")
        self.line = line
        self.chip = chip
        self.edge = edge
        self.active_low = active_low
        self.backend = backend
        self._warn = warn
        self._clock = clock
        self._sleep = sleep
        self._impl = None
        self._closed = False

    # -- backend selection -------------------------------------------------
    def _open_impl(self):
        if self._impl is not None:
            return self._impl
        order = (["gpiod", "sysfs", "poll"] if self.backend == "auto"
                 else [self.backend])
        errors = []
        for candidate in order:
            try:
                if candidate == "gpiod":
                    self._impl = _GpiodWaiter(self.line, self.chip, self.edge,
                                              self.active_low)
                elif candidate == "sysfs":
                    self._impl = _SysfsWaiter(self.line, self.edge)
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
                "no gpiod/sysfs GPIO backend: falling back to a time-based "
                "poll; the interrupt line cannot be observed, so a timeout "
                "here does not prove the device is idle", RuntimeWarning,
                stacklevel=2)
        edge, value = impl.wait(timeout_ms / 1000.0)
        elapsed = (self._clock() - started) * 1000.0
        if edge is None and value is None and impl.name != "poll":
            raise SpiTimeout(f"no interrupt on GPIO {self.line} within "
                             f"{timeout_ms} ms")
        return IrqEvent(source=impl.name, elapsed_ms=elapsed,
                        edge=edge, value=value)

    def close(self) -> None:
        if self._impl is not None and not self._closed:
            self._impl.close()
            self._closed = True


class _GpiodWaiter:
    name = "gpiod"

    def __init__(self, line: int, chip: str | None, edge: str,
                 active_low: bool):
        import gpiod  # noqa: F401  (probe for the binding)
        self._gpiod = gpiod
        self._line = line
        self._chip = chip
        self._edge = edge
        self._active_low = active_low
        self._request = None
        # libgpiod v1 and v2 have incompatible Python APIs; decide by symbol.
        if hasattr(gpiod, "Chip") and hasattr(gpiod, "LINE_REQ_EV_EDGE"):
            self._version = 1
            self._open_v1()
        elif hasattr(gpiod, "request_lines"):
            self._version = 2
            self._open_v2()
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
                return ("rising" if ev.type == self._gpiod.EdgeEventType.RISING_EDGE
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
    """Edge wait through /sys/class/gpio (root; writes export/edge)."""

    name = "sysfs"

    def __init__(self, line: int, edge: str):
        base = "/sys/class/gpio"
        gpio = os.path.join(base, f"gpio{line}")
        if not os.path.isdir(base):
            raise SpiError("/sys/class/gpio is not available")
        self._exported_here = False
        if not os.path.isdir(gpio):
            self._write(os.path.join(base, "export"), str(line))
            self._exported_here = True
            if not _wait_path(gpio, 0.5):
                raise SpiError(f"gpio{line} did not appear after export")
        self._dir = os.path.join(gpio, "direction")
        self._write(self._dir, "in")
        self._write(os.path.join(gpio, "edge"), edge)
        self._value = os.path.join(gpio, "value")
        self._gpio = gpio

    @staticmethod
    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(text + "\n")

    def wait(self, timeout_s: float):
        with open(self._value, "r", encoding="ascii") as fh:
            ready, _, _ = select.select([fh], [], [], timeout_s)
            if not ready:
                return None, None
            fh.seek(0)
            value = fh.read().strip()
        return ("edge" if value in ("0", "1") else None,
                int(value) if value in ("0", "1") else None)

    def close(self) -> None:
        if self._exported_here:
            try:
                self._write(os.path.join("/sys/class/gpio", "unexport"),
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
    """Output line used to pulse the sensor reset (only when configured)."""

    def __init__(self, line: int, *, chip: str | None = None,
                 active_low: bool = True, backend: str = "auto",
                 sleep=time.sleep):
        self.line = line
        self.chip = chip
        self.active_low = active_low
        self.backend = backend
        self._sleep = sleep
        self._impl = None

    def _open(self):
        if self._impl is not None:
            return self._impl
        order = (["gpiod", "sysfs"] if self.backend == "auto"
                 else [self.backend])
        errors = []
        for candidate in order:
            try:
                if candidate == "gpiod":
                    self._impl = _GpiodOutput(self.line, self.chip,
                                              self.active_low)
                else:
                    self._impl = _SysfsOutput(self.line)
                return self._impl
            except (ImportError, OSError, SpiError) as exc:
                errors.append(f"{candidate}: {exc}")
        raise IrqUnavailable("no GPIO output backend (" + "; ".join(errors) +
                             ")")

    def pulse(self, assert_ms: int = RESET_ASSERT_MS,
              release_ms: int = RESET_RELEASE_MS) -> None:
        impl = self._open()
        active = 0 if self.active_low else 1
        idle = 1 - active
        impl.set(active)
        self._sleep(assert_ms / 1000.0)
        impl.set(idle)
        self._sleep(release_ms / 1000.0)

    def close(self) -> None:
        if self._impl is not None:
            self._impl.close()


class _GpiodOutput:
    def __init__(self, line: int, chip: str | None, active_low: bool):
        import gpiod
        self._gpiod = gpiod
        self._line = line
        self._version = 1 if hasattr(gpiod, "Chip") else 2
        if self._version == 1:
            c = gpiod.Chip(chip or "gpiochip0")
            ln = c.get_line(line)
            flags = (gpiod.LINE_REQ_FLAG_ACTIVE_LOW if active_low else 0)
            ln.request(consumer="fpbf63112-reset", type=gpiod.LINE_REQ_DIR_OUT,
                       flags=flags)
            self._h = (c, ln)
        else:
            settings = gpiod.LineSettings(direction=gpiod.Direction.OUTPUT,
                                          output_value=gpiod.Value.INACTIVE,
                                          active_low=active_low)
            self._h = gpiod.request_lines(chip or "gpiochip0",
                                          consumer="fpbf63112-reset",
                                          config={line: settings})

    def set(self, value: int) -> None:
        if self._version == 1:
            self._h[1].set_value(value)
        else:
            self._h.set_value(self._line, self._gpiod.Value.ACTIVE if value
                              else self._gpiod.Value.INACTIVE)

    def close(self) -> None:
        if self._version == 1:
            c, ln = self._h
            ln.release()
            c.close()
        else:
            self._h.release()


class _SysfsOutput:
    def __init__(self, line: int):
        base = "/sys/class/gpio"
        gpio = os.path.join(base, f"gpio{line}")
        if not os.path.isdir(base):
            raise SpiError("/sys/class/gpio is not available")
        self._exported_here = False
        if not os.path.isdir(gpio):
            _SysfsWaiter._write(os.path.join(base, "export"), str(line))
            self._exported_here = True
        _SysfsWaiter._write(os.path.join(gpio, "direction"), "out")
        self._value = os.path.join(gpio, "value")
        self._gpio = gpio

    def set(self, value: int) -> None:
        _SysfsWaiter._write(self._value, "1" if value else "0")

    def close(self) -> None:
        if self._exported_here:
            try:
                _SysfsWaiter._write(os.path.join("/sys/class/gpio", "unexport"),
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
        self._io = spi_io
        self._clock = clock
        self._sleep = sleep
        self._opened = False
        self._configured = False
        self._cancel_requested = False

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
        self._io.open()
        self._io.configure(speed_hz=self.speed_hz, mode=self.mode,
                           bits_per_word=self.bits_per_word)
        self._opened = True
        if self.reset_line is not None and self.allow_reset:
            self.reset_line.pulse()
            # FUN_180016260: Sleep(0x32) after the reset target is opened.
            self._sleep(RESET_OPEN_MS / 1000.0)

    def close(self) -> None:
        if self._io is not None:
            self._io.close()
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
             timeout_ms: int | None = None) -> SpiPlan:
        return build_plan(opcode, operation=operation, payload=payload,
                          read_len=read_len, timeout_ms=timeout_ms)

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
    def execute(self, plan: SpiPlan) -> bytes:
        """Run one plan and return the received bytes.

        For a read the answer starts after the selector byte; the full
        received buffer is returned (the caller takes ``[1:]``).  The
        per-command timeout covers the transfer plus the IRQ wait; spidev
        itself performs a synchronous, kernel-bounded ioctl.
        """
        self._check(plan)
        self._check_cancel()
        self.open()
        started = self._clock()
        rx = self._io.transfer(plan.tx, rx_len=plan.rx_len)
        self._check_cancel()
        if self.irq is not None:
            remaining = plan.timeout_ms - (self._clock() - started) * 1000.0
            if remaining <= 0:
                raise SpiTimeout(
                    f"opcode 0x{plan.opcode:02x} timed out before the IRQ "
                    f"wait ({plan.timeout_ms} ms)")
            self.irq.wait(int(remaining))
        elapsed_ms = (self._clock() - started) * 1000.0
        if elapsed_ms > plan.timeout_ms:
            raise SpiTimeout(f"opcode 0x{plan.opcode:02x} took "
                             f"{elapsed_ms:.0f} ms > {plan.timeout_ms} ms")
        return rx

    # -- command helpers ---------------------------------------------------
    def read(self, opcode: int, length: int, *,
             timeout_ms: int | None = None) -> bytes:
        """Read ``length`` bytes for ``opcode`` (answer only, no selector)."""
        plan = self.plan(opcode, operation="read", read_len=length,
                         timeout_ms=timeout_ms)
        rx = self.execute(plan)
        return rx[1:1 + length]

    def read_command(self, name: str) -> bytes:
        command = COMMANDS[name]
        return self.read(command.opcode, command.read_len,
                         timeout_ms=command.timeout_ms)

    def write(self, opcode: int, payload: bytes, *,
              timeout_ms: int | None = None) -> None:
        """Write ``payload`` for ``opcode`` (requires the write allowlist)."""
        plan = self.plan(opcode, operation="write", payload=payload,
                         timeout_ms=timeout_ms)
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
        (..., 100)`` loop with ``Sleep(0x32)`` between attempts.
        """
        last: SpiTimeout | None = None
        for attempt in range(attempts):
            try:
                return self.wait_irq(timeout_ms)
            except SpiTimeout as exc:
                last = exc
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
