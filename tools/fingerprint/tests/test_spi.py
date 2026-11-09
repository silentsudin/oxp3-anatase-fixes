"""SPI transport: selector framing, spidev ABI, timeouts, IRQ, reset, gates.

The vectors here are synthetic: they pin the layout this repository believes
(``docs/hw/fingerprint-transport.md`` §4), not a captured sensor transcript.
No test opens a real ``/dev/spidev*`` node or touches GPIO: the spidev layer is
driven through a monkeypatched ``os.open``/``fcntl.ioctl`` pair and every GPIO
backend is a fake module or a temporary sysfs tree.
"""

import contextlib
import ctypes
import io
import os
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

import support
from fpbf63112 import spi


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """Deterministic monotonic clock whose sleep also advances time."""

    def __init__(self, start=0.0):
        self.value = start
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds

    def advance_ms(self, milliseconds):
        self.value += milliseconds / 1000.0


class RecordingIO:
    """``LinuxSpiIO``-shaped recorder, injection point for ``SpiTransport``."""

    def __init__(self, answer=b"", advance_ms=0, clock=None):
        self.answer = answer
        self.advance_ms = advance_ms
        self.clock = clock
        self.opened = False
        self.closed = False
        self.configured = None
        self.messages = []
        self.on_message = None

    def open(self):
        self.opened = True

    def configure(self, **kwargs):
        self.configured = kwargs

    def message(self, transfers):
        self.messages.append(tuple(transfers))
        if self.on_message is not None:
            self.on_message()
        if self.clock is not None and self.advance_ms:
            self.clock.advance_ms(self.advance_ms)
        received = []
        for transfer in transfers:
            if not transfer.rx_len:
                received.append(b"")
            elif len(transfers) == 1:
                # Combined full-duplex shape: the prefix (selector [+ lead])
                # clocks zeros before the answer bytes.
                pad = max(0, transfer.rx_len - len(self.answer))
                body = self.answer[:transfer.rx_len - pad]
                received.append(b"\x00" * pad + body)
            else:
                received.append(self.answer[:transfer.rx_len].ljust(
                    transfer.rx_len, b"\x00"))
        return tuple(received)

    def close(self):
        self.closed = True


class FakeIrq:
    def __init__(self, events=None, clock=None, advance_ms=0):
        self.events = list(events or [])
        self.timeouts = []
        self.closed = False
        self.clock = clock
        self.advance_ms = advance_ms

    def wait(self, timeout_ms=None):
        self.timeouts.append(timeout_ms)
        if self.clock is not None and self.advance_ms:
            self.clock.advance_ms(self.advance_ms)
        event = self.events.pop(0) if self.events else spi.IrqEvent(
            "gpiod", 0.0, "rising", None)
        if isinstance(event, Exception):
            raise event
        return event

    def close(self):
        self.closed = True


def make_gpiod_v1(events, requested=None):
    """Minimal libgpiod v1 Python binding: Chip/Line/EdgeEventType."""
    events = list(events)

    class Line:
        def request(self, **kwargs):
            self.requested = kwargs
            module.requests.append(kwargs)

        def event_wait(self, timeout=None):
            return bool(events)

        def event_read(self):
            class Ev:
                pass
            ev = Ev()
            ev.type = events.pop(0)
            return ev

        def release(self):
            pass

        def set_value(self, value):
            self.values.append(value)

    class Chip:
        def __init__(self, name):
            self.name = name
            self.lines = {}

        def get_line(self, line):
            if line not in self.lines:
                self.lines[line] = Line()
                self.lines[line].values = []
            return self.lines[line]

        def close(self):
            pass

    module = types.SimpleNamespace(
        Chip=Chip,
        LINE_REQ_EV_EDGE=1,
        LINE_REQ_DIR_OUT=2,
        LINE_REQ_FLAG_ACTIVE_LOW=4,
        EdgeEventType=types.SimpleNamespace(RISING_EDGE=10,
                                            FALLING_EDGE=11),
        requests=[],
    )
    return module


def make_gpiod_v2(events):
    """Minimal libgpiod v2 Python binding: request_lines/Edge/LineSettings."""
    events = list(events)
    state = {"requested": None, "released": False, "values": []}

    class Edge:
        RISING = "rising"
        FALLING = "falling"
        BOTH = "both"

    class Value:
        ACTIVE = 1
        INACTIVE = 0

    class Direction:
        OUTPUT = "out"

    class EdgeEventType:
        RISING_EDGE = 10
        FALLING_EDGE = 11

    class LineSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            state["settings"] = kwargs

    class Request:
        def wait_edge_events(self, timeout):
            return bool(events)

        def read_edge_events(self):
            class Ev:
                pass
            ev = Ev()
            ev.event_type = events.pop(0)
            return [ev]

        def set_value(self, line, value):
            state["values"].append(value)

        def release(self):
            state["released"] = True

    def request_lines(chip, consumer=None, config=None):
        state["requested"] = (chip, consumer, config)
        return Request()

    module = types.SimpleNamespace(
        request_lines=request_lines,
        Edge=Edge,
        Value=Value,
        Direction=Direction,
        LineSettings=LineSettings,
        EdgeEventType=EdgeEventType,
    )
    module.state = state
    return module


# ---------------------------------------------------------------------------
# Selector / plans
# ---------------------------------------------------------------------------

class TestSelector(unittest.TestCase):
    def test_read_selector_drops_direction_bit(self):
        self.assertEqual(spi.selector(0xA0), 0x40)
        self.assertEqual(spi.selector(0xA1), 0x42)
        self.assertEqual(spi.selector(0xC0), 0x00)
        self.assertEqual(spi.selector(0xC1), 0x02)
        self.assertEqual(spi.selector(0x2D), 0x5A)

    def test_write_selector_sets_bit_7(self):
        self.assertEqual(spi.selector(0xA0, write=True), 0xC0)
        self.assertEqual(spi.selector(0x00, write=True), 0x80)

    def test_only_the_low_six_bits_reach_the_wire(self):
        # The driver masks with 0x3f, so these opcodes alias on the wire.
        for a, b in ((0xA0, 0x20), (0xC2, 0x02), (0xC3, 0x03),
                     (0xD0, 0x10), (0xFF, 0x3F)):
            self.assertEqual(spi.selector(a), spi.selector(b), (a, b))
            self.assertEqual(spi.selector(a, True), spi.selector(b, True))

    def test_out_of_range(self):
        for bad in (-1, 0x100):
            with self.assertRaises(ValueError):
                spi.selector(bad)


class TestPlans(unittest.TestCase):
    def test_default_read_plan_is_one_cs_held_transfer(self):
        plan = spi.build_plan(0xA0, read_len=7)
        self.assertEqual(plan.operation, "read")
        self.assertEqual(plan.read_shape, "cs-held")
        self.assertEqual(plan.selector, 0x40)
        self.assertEqual(len(plan.transfers), 1)
        self.assertEqual(plan.transfers[0].tx, b"\x40" + b"\x00" * 7)
        self.assertEqual(plan.transfers[0].rx_len, 8)
        self.assertEqual(plan.answer_len, 7)
        self.assertEqual(plan.answer_offset, 1)
        self.assertEqual(plan.prefix_tx, b"\x40")
        self.assertEqual(plan.tx, b"\x40" + b"\x00" * 7)
        self.assertEqual(plan.rx_len, 8)

    def test_split_read_plan_is_selector_then_receive_clock(self):
        plan = spi.build_plan(0xA0, read_len=7, read_shape="split")
        self.assertEqual(plan.read_shape, "split")
        self.assertEqual(len(plan.transfers), 2)
        self.assertEqual(plan.transfers[0].tx, b"\x40")
        self.assertEqual(plan.transfers[0].rx_len, 0)
        self.assertEqual(plan.transfers[1].tx, b"")
        self.assertEqual(plan.transfers[1].rx_len, 7)
        self.assertEqual(plan.answer_len, 7)
        self.assertEqual(plan.answer_offset, 0)
        self.assertEqual(plan.tx, b"\x40")
        self.assertEqual(plan.rx_len, 7)

    def test_bad_read_shape_is_refused(self):
        with self.assertRaises(ValueError):
            spi.build_plan(0xA0, read_len=7, read_shape="full-duplex")
        with self.assertRaises(ValueError):
            spi.build_shape_plan(0xA0, read_len=7, read_shape="nope")

    def test_malformed_cs_held_plans_are_refused(self):
        # rx_len must equal the tx length (one full-duplex buffer).
        with self.assertRaises(ValueError):
            spi.SpiPlan(0xA0, "read", b"", 7, 0x40,
                        (spi.SpiTransfer(b"\x40" + b"\x00" * 7, 7),),
                        400, 400, read_shape="cs-held")
        # Everything after the selector prefix must be zero clocks.
        with self.assertRaises(ValueError):
            spi.SpiPlan(0xA0, "read", b"", 7, 0x40,
                        (spi.SpiTransfer(b"\x40" + b"\x01" * 7, 8),),
                        400, 400, read_shape="cs-held")
        # Two transfers is the split shape, not cs-held.
        with self.assertRaises(ValueError):
            spi.SpiPlan(0xA0, "read", b"", 7, 0x40,
                        (spi.SpiTransfer(b"\x40", 0),
                         spi.SpiTransfer(b"", 7)),
                        400, 400, read_shape="cs-held")
        # A read shape does not apply to writes.
        with self.assertRaises(ValueError):
            spi.SpiPlan(0x2D, "write", b"", 0, 0xDA,
                        (spi.SpiTransfer(b"\xda", 0),),
                        400, 400, read_shape="cs-held")

    def test_write_plan_is_selector_then_payload(self):
        plan = spi.build_plan(0x2D, operation="write", payload=b"\x10")
        self.assertEqual(plan.selector, 0xDA)   # 0x80 | 0x5A
        self.assertEqual(plan.transfers[0].tx, b"\xda")
        self.assertEqual(plan.transfers[1].tx, b"\x10")
        self.assertEqual(plan.transfers[1].rx_len, 0)
        self.assertEqual(plan.tx, b"\xda\x10")
        self.assertEqual(plan.rx_len, 0)

    def test_write_with_empty_payload_has_one_transfer(self):
        plan = spi.build_plan(0x2D, operation="write")
        self.assertEqual(len(plan.transfers), 1)
        self.assertEqual(plan.tx, b"\xda")

    def test_bad_plans_rejected(self):
        with self.assertRaises(ValueError):
            spi.build_plan(0xA0, read_len=0)
        with self.assertRaises(ValueError):
            spi.build_plan(0xA0, operation="bogus", read_len=1)
        with self.assertRaises(ValueError):
            spi.build_plan(0xA0, read_len=1, timeout_ms=0)
        with self.assertRaises(ValueError):
            spi.build_plan(0xA0, read_len=1, timeout_ms=500,
                           total_ms=100)

    def test_recovered_command_timeouts(self):
        self.assertEqual((spi.CMD_TIMEOUT_MS, spi.CMD_TOTAL_MS), (1000, 2030))
        self.assertEqual((spi.IMAGE_TIMEOUT_MS, spi.IMAGE_TOTAL_MS),
                         (5000, 10010))
        self.assertEqual((spi.RESTORE_TIMEOUT_MS, spi.RESTORE_TOTAL_MS),
                         (2000, 4016))
        self.assertEqual(spi.CANCEL_SETTLE_MS, 150)
        self.assertEqual(spi.RESET_OPEN_MS, 50)
        for command in spi.COMMANDS.values():
            plan = spi.plan_command(command)
            self.assertEqual((plan.timeout_ms, plan.total_ms), (1000, 2030),
                             command.name)
        image = spi.build_plan(0xB4, read_len=0x1FCC)
        self.assertEqual((image.timeout_ms, image.total_ms), (5000, 10010))
        restore = spi.build_plan(0xC3, operation="write", payload=b"\x01")
        self.assertEqual((restore.timeout_ms, restore.total_ms), (2000, 4016))

    def test_command_table_matches_recovered_read_lengths(self):
        self.assertEqual(spi.COMMANDS["fw"].read_len, 7)
        self.assertEqual(spi.COMMANDS["info"].read_len, 9)
        self.assertEqual(spi.COMMANDS["count"].read_len, 4)
        self.assertEqual(spi.COMMANDS["uid"].read_len, 16)
        self.assertEqual(spi.COMMANDS["status"].opcode, 0x2D)


# ---------------------------------------------------------------------------
# Low-level spidev ABI (monkeypatched open/ioctl)
# ---------------------------------------------------------------------------

class TestLinuxSpiIO(unittest.TestCase):
    def test_configure_and_two_entry_message(self):
        calls = []
        answer = b"\x01\x02\x03\x04\x05\x06\x07"

        def fake_open(path, flags):
            calls.append(("open", path, flags))
            return 9

        def fake_close(fd):
            calls.append(("close", fd))

        def fake_ioctl(fd, request, arg):
            calls.append(("ioctl", fd, request, bytes(arg)))
            if request == spi.spi_ioc_message(2):
                # Fill the second transfer's receive buffer from the answer.
                for index in range(2):
                    base = index * spi._SPI_IOC_TRANSFER.size
                    _tx, rx, length = struct.unpack_from("<QQI", arg, base)
                    if rx and index == 1:
                        ctypes.memmove(rx, answer, min(length, len(answer)))

        io_obj = spi.LinuxSpiIO("/dev/spidev-test")
        with mock.patch.object(spi.os, "open", side_effect=fake_open), \
                mock.patch.object(spi.os, "close", side_effect=fake_close), \
                mock.patch.object(spi.fcntl, "ioctl", side_effect=fake_ioctl):
            io_obj.open()
            io_obj.configure(speed_hz=1_000_000, mode=3, bits_per_word=8)
            rx = io_obj.message(spi.build_plan(
                0xA0, read_len=7, read_shape="split").transfers)
            io_obj.close()

        self.assertEqual(rx, (b"", answer))
        ioctls = [c for c in calls if c[0] == "ioctl"]
        self.assertEqual([c[2] for c in ioctls],
                         [spi.SPI_IOC_WR_MODE, spi.SPI_IOC_WR_BITS_PER_WORD,
                          spi.SPI_IOC_WR_MAX_SPEED_HZ, spi.spi_ioc_message(2)])
        self.assertEqual(ioctls[0][3], b"\x03")
        self.assertEqual(ioctls[1][3], b"\x08")
        self.assertEqual(ioctls[2][3], struct.pack("<I", 1_000_000))

        msg = ioctls[3][3]
        self.assertEqual(len(msg), 2 * spi._SPI_IOC_TRANSFER.size)
        tx0, rx0, len0 = struct.unpack_from("<QQI", msg, 0)
        tx1, rx1, len1 = struct.unpack_from("<QQI", msg, 32)
        self.assertEqual((len0, rx0), (1, 0))
        self.assertEqual((len1, tx1), (7, 0))
        self.assertNotEqual(rx1, 0)
        self.assertEqual(ctypes.string_at(tx0, 1), b"\x40")
        self.assertEqual(io_obj.last_transfers,
                         spi.build_plan(0xA0, read_len=7,
                                        read_shape="split").transfers)
        self.assertEqual(io_obj.last_message, (b"", answer))

    def test_combined_read_is_one_full_duplex_ioctl(self):
        calls = []
        answer = b"\x01\x02\x03\x04\x05\x06\x07"

        def fake_open(path, flags):
            return 9

        def fake_close(fd):
            pass

        def fake_ioctl(fd, request, arg):
            calls.append((request, bytes(arg)))
            if request == spi.spi_ioc_message(1):
                tx, rx, length = struct.unpack_from("<QQI", arg, 0)
                self.assertEqual(ctypes.string_at(tx, length),
                                 b"\x40" + b"\x00" * 7)
                ctypes.memmove(rx, b"\x00" + answer, 8)

        io_obj = spi.LinuxSpiIO("/dev/spidev-test")
        with mock.patch.object(spi.os, "open", side_effect=fake_open), \
                mock.patch.object(spi.os, "close", side_effect=fake_close), \
                mock.patch.object(spi.fcntl, "ioctl", side_effect=fake_ioctl):
            io_obj.open()
            rx = io_obj.message(spi.build_plan(0xA0, read_len=7).transfers)

        self.assertEqual(rx, (b"\x00" + answer,))
        messages = [item for item in calls
                    if item[0] == spi.spi_ioc_message(1)]
        self.assertEqual(len(messages), 1, "one xfer2-style ioctl, no split")
        _request, msg = messages[0]
        self.assertEqual(len(msg), spi._SPI_IOC_TRANSFER.size)
        fields = struct.unpack_from("<QQIIHBBBBBB", msg, 0)
        tx, rx, length = fields[:3]
        self.assertEqual((length, rx != 0), (8, True))
        # cs_change is the 7th field and must stay 0, so CS is held for the
        # whole combined transfer (never deasserted after the selector).
        self.assertEqual(fields[6], 0)

    def test_single_transfer_wrapper(self):
        seen = {}

        def fake_ioctl(fd, request, arg):
            seen["request"] = request
            tx, rx, length = struct.unpack_from("<QQI", arg, 0)
            seen["tx"] = ctypes.string_at(tx, length)
            ctypes.memmove(rx, b"\xaa\xbb", 2)

        io_obj = spi.LinuxSpiIO("/dev/fake")
        io_obj.fd = 3
        with mock.patch.object(spi.fcntl, "ioctl", side_effect=fake_ioctl):
            out = io_obj.transfer(b"\x55", rx_len=2)
        self.assertEqual(out, b"\xaa\xbb")
        self.assertEqual(seen["request"], spi.SPI_IOC_MESSAGE_1)
        self.assertEqual(seen["tx"], b"\x55\x00")

    def test_operations_before_open_fail(self):
        io_obj = spi.LinuxSpiIO("/dev/fake")
        with self.assertRaises(spi.SpiError):
            io_obj.configure(mode=0)
        with self.assertRaises(spi.SpiError):
            io_obj.message(spi.build_plan(0xA0, read_len=1).transfers)
        with self.assertRaises(spi.SpiError):
            io_obj.message(())


# ---------------------------------------------------------------------------
# Transport gates, execution, deadlines, cancel
# ---------------------------------------------------------------------------

class TestSafetyGates(unittest.TestCase):
    def test_construction_does_no_io(self):
        io_obj = RecordingIO()
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        self.assertFalse(io_obj.opened)
        self.assertFalse(transport.opened)

    def test_all_reads_need_the_opt_in(self):
        transport = spi.SpiTransport("/dev/fake", spi_io=RecordingIO())
        with self.assertRaises(spi.CommandNotAllowed):
            transport.read(0xA0, 7)
        self.assertFalse(transport._io.opened)

    def test_no_device_path_refused_before_io(self):
        transport = spi.SpiTransport(None, allow_writes=True)
        with self.assertRaises(spi.SpiError):
            transport.open()
        self.assertFalse(transport.opened)

    def test_destructive_opcode_refused_unconditionally(self):
        for opcode in (0xB0, 0xB1, 0xB2, 0xC2, 0xC3, 0xD0, 0xD1, 0xFF):
            transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                         read_allowlist=[opcode],
                                         write_allowlist=[opcode],
                                         spi_io=RecordingIO())
            with self.subTest(opcode=hex(opcode)):
                with self.assertRaises(spi.UnsafeCommand):
                    transport.read(opcode, 1)

    def test_selector_alias_of_a_destructive_opcode_is_refused(self):
        # 0x02 computes JTAG's selector 0x04 and must be refused even though
        # the caller explicitly allowlisted it.
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0x02],
                                     spi_io=RecordingIO())
        with self.assertRaises(spi.UnsafeCommand):
            transport.read(0x02, 1)
        # 0x20 is the read-safe alias of 0xA0; explicit allowlisting is needed.
        with self.assertRaises(spi.CommandNotAllowed):
            transport.read(0x20, 7)

    def test_opcode_not_on_the_allowlist(self):
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0xA0],
                                     spi_io=RecordingIO())
        with self.assertRaises(spi.CommandNotAllowed):
            transport.read(0xC1, 9)

    def test_writes_are_not_allowlisted_by_default(self):
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=RecordingIO())
        with self.assertRaises(spi.CommandNotAllowed):
            transport.write(0x2D, b"\x10")

    def test_explicit_write_allowlist_still_respects_destructive_ops(self):
        io_obj = RecordingIO()
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     write_allowlist=[0x2D], spi_io=io_obj)
        transport.write(0x2D, b"\x10")
        self.assertEqual(io_obj.messages[0][0].tx, b"\xda")
        self.assertEqual(io_obj.messages[0][1].tx, b"\x10")


class TestExecute(unittest.TestCase):
    def make(self, **kwargs):
        io_obj = kwargs.pop("io_obj", None) or RecordingIO(b"\x10" * 32)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj, **kwargs)
        return transport, io_obj

    def test_read_returns_exactly_the_answer_length(self):
        transport, io_obj = self.make()
        self.assertEqual(transport.read(0xA0, 7), b"\x10" * 7)
        self.assertEqual(io_obj.configured, {"speed_hz": None, "mode": None,
                                             "bits_per_word": None})

    def test_execute_returns_the_receive_transfer_only(self):
        transport, _ = self.make()
        plan = transport.plan(0xA0, read_len=3)
        self.assertEqual(transport.execute(plan), b"\x10" * 3)

    def test_cs_held_execute_returns_the_slice_after_the_prefix(self):
        io_obj = RecordingIO(b"\xde\xad\xbe\xef")
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        self.assertEqual(transport.read(0xA0, 4), b"\xde\xad\xbe\xef")
        self.assertEqual(len(io_obj.messages), 1, "one combined message")
        transfers = io_obj.messages[0]
        self.assertEqual(len(transfers), 1, "one transfer, not split")
        self.assertEqual(transfers[0].tx, b"\x40" + b"\x00" * 4)
        self.assertEqual(transfers[0].rx_len, 5)
        self.assertEqual(transfers[0].tx[0], 0x40)
        self.assertEqual(transfers[0].tx[1:], b"\x00" * 4)

    def test_split_execute_still_uses_the_two_segment_shape(self):
        io_obj = RecordingIO(b"\xde\xad\xbe\xef")
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        self.assertEqual(transport.read(0xA0, 4, read_shape="split"),
                         b"\xde\xad\xbe\xef")
        transfers = io_obj.messages[0]
        self.assertEqual(len(transfers), 2)
        self.assertEqual(transfers[0].tx, b"\x40")
        self.assertEqual(transfers[0].rx_len, 0)
        self.assertEqual(transfers[1].tx, b"")
        self.assertEqual(transfers[1].rx_len, 4)

    def test_backend_without_combined_transfer_fails_closed(self):
        class NoCombined(RecordingIO):
            supports_combined_transfer = False

        io_obj = NoCombined(b"\x00" * 7)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        with self.assertRaises(spi.TransferShapeUnsupported) as caught:
            transport.read(0xA0, 7)
        self.assertIn("cs-held", str(caught.exception))
        self.assertIn("refusing to fall back", str(caught.exception))
        self.assertEqual(io_obj.messages, [], "no ioctl, and no split fallback")

    def test_not_implemented_combined_transfer_fails_closed(self):
        class NoCombinedIO(RecordingIO):
            def message(self, transfers):
                self.messages.append(tuple(transfers))
                raise NotImplementedError("no full-duplex ioctl here")

        io_obj = NoCombinedIO()
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        with self.assertRaises(spi.TransferShapeUnsupported):
            transport.read(0xA0, 7)
        self.assertEqual(len(io_obj.messages), 1)
        self.assertEqual(len(io_obj.messages[0]), 1,
                         "the fallback must not add a second call")

    def test_kernel_rejected_combined_transfer_is_typed(self):
        class EINVALIO(RecordingIO):
            def message(self, transfers):
                self.messages.append(tuple(transfers))
                raise OSError(spi.errno.EINVAL, "Invalid argument")

        io_obj = EINVALIO()
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        with self.assertRaises(spi.TransferShapeUnsupported):
            transport.read(0xA0, 7)
        self.assertEqual(len(io_obj.messages), 1)

    def test_extra_receive_segments_are_a_short_combined_read(self):
        class TwoBuffers(RecordingIO):
            def message(self, transfers):
                self.messages.append(tuple(transfers))
                return (b"\x00\x10\x10\x10\x10\x10\x10\x10", b"")

        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=TwoBuffers())
        with self.assertRaises(spi.SpiError) as caught:
            transport.read(0xA0, 7)
        self.assertIn("short cs-held SPI read", str(caught.exception))

    def test_other_oserror_is_a_plain_spi_error(self):
        class BrokenIO(RecordingIO):
            def message(self, transfers):
                self.messages.append(tuple(transfers))
                raise OSError(spi.errno.EIO, "I/O error")

        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=BrokenIO())
        with self.assertRaises(spi.SpiError) as caught:
            transport.read(0xA0, 7)
        self.assertNotIsInstance(caught.exception,
                                 spi.TransferShapeUnsupported)

    def test_short_read_is_rejected(self):
        class ShortIO(RecordingIO):
            def message(self, transfers):
                self.messages.append(tuple(transfers))
                return (b"\x00\x01",)

        io_obj = ShortIO()
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        with self.assertRaises(spi.SpiError) as caught:
            transport.read(0xA0, 7)
        self.assertIn("short cs-held SPI read", str(caught.exception))

    def test_write_returns_no_bytes(self):
        transport, io_obj = self.make(write_allowlist=[0x2D])
        plan = transport.plan(0x2D, operation="write", payload=b"\x00")
        self.assertEqual(transport.execute(plan), b"")
        self.assertEqual(io_obj.messages[0][1].tx, b"\x00")

    def test_configure_values_are_passed_through(self):
        transport, io_obj = self.make(speed_hz=2_000_000, mode=3,
                                      bits_per_word=8)
        transport.read(0xA0, 7)
        self.assertEqual(io_obj.configured, {"speed_hz": 2_000_000,
                                             "mode": 3,
                                             "bits_per_word": 8})

    def test_transfer_timeout_uses_per_attempt_deadline(self):
        clock = FakeClock()
        io_obj = RecordingIO(b"\x00" * 7, advance_ms=1001, clock=clock)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj, clock=clock.now,
                                     sleep=clock.sleep)
        with self.assertRaises(spi.SpiTimeout) as caught:
            transport.read(0xA0, 7)
        self.assertIn("1000 ms per-attempt", str(caught.exception))

    def test_total_deadline_is_checked(self):
        clock = FakeClock()
        io_obj = RecordingIO(b"\x00" * 7, advance_ms=2031, clock=clock)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj, clock=clock.now,
                                     sleep=clock.sleep)
        with self.assertRaises(spi.SpiTimeout) as caught:
            transport.read(0xA0, 7)
        self.assertIn("2030 ms total", str(caught.exception))

    def test_irq_wait_gets_the_remaining_deadline(self):
        clock = FakeClock()
        io_obj = RecordingIO(b"\x00" * 7, advance_ms=100, clock=clock)
        irq = FakeIrq([spi.IrqEvent("gpiod", 1.0, "rising")], clock=clock)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj, irq=irq,
                                     clock=clock.now, sleep=clock.sleep)
        transport.read(0xA0, 7)
        self.assertEqual(irq.timeouts, [900])

    def test_irq_wait_is_not_started_after_the_deadline(self):
        clock = FakeClock()
        io_obj = RecordingIO(b"\x00" * 7, advance_ms=1000, clock=clock)
        irq = FakeIrq([], clock=clock)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj, irq=irq,
                                     clock=clock.now, sleep=clock.sleep)
        with self.assertRaises(spi.SpiTimeout):
            transport.read(0xA0, 7)
        self.assertEqual(irq.timeouts, [])

    def test_irq_timeout_propagates(self):
        clock = FakeClock()
        irq = FakeIrq([spi.SpiTimeout("no edge")], clock=clock)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=RecordingIO(b"\x00" * 7),
                                     irq=irq, clock=clock.now,
                                     sleep=clock.sleep)
        with self.assertRaises(spi.SpiTimeout):
            transport.read(0xA0, 7)

    def test_cancel_before_execute_does_not_open(self):
        io_obj = RecordingIO(b"\x00" * 32)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj)
        transport.request_cancel()
        with self.assertRaises(spi.Cancelled):
            transport.read(0xA0, 7)
        self.assertFalse(io_obj.opened)
        # the flag was consumed, so the next attempt is allowed to proceed
        transport.read(0xA0, 7)

    def test_cancel_during_transfer_aborts_before_irq(self):
        clock = FakeClock()
        io_obj = RecordingIO(b"\x00" * 7)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj,
                                     irq=FakeIrq([]), clock=clock.now,
                                     sleep=clock.sleep)
        io_obj.on_message = transport.request_cancel
        with self.assertRaises(spi.Cancelled):
            transport.read(0xA0, 7)
        self.assertEqual(len(io_obj.messages), 1)

    def test_cancel_policy_waits_150_ms(self):
        clock = FakeClock()
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=RecordingIO(),
                                     clock=clock.now, sleep=clock.sleep)
        transport.cancel()
        self.assertEqual(clock.sleeps, [0.150])
        with self.assertRaises(spi.Cancelled):
            transport.read(0xA0, 7)

    def test_open_failure_is_wrapped_and_no_io_leaks(self):
        class FailingIO:
            def __init__(self):
                self.closed = False

            def open(self):
                raise OSError(19, "No such device")

            def close(self):
                self.closed = True

        io_obj = FailingIO()
        transport = spi.SpiTransport("/dev/missing", allow_writes=True,
                                     spi_io=io_obj)
        with self.assertRaises(spi.SpiError) as caught:
            transport.read(0xA0, 7)
        self.assertIn("/dev/missing", str(caught.exception))
        self.assertTrue(io_obj.closed)


# ---------------------------------------------------------------------------
# IRQ backends
# ---------------------------------------------------------------------------

class TestGpiodWaiter(unittest.TestCase):
    def test_v1_rising_edge_and_active_low_flag(self):
        gpiod = make_gpiod_v1([10])
        waiter = spi.GpioIrqWaiter(42, chip="gpiochip1",
                                   edge="rising", active_low=True,
                                   backend="gpiod",
                                   gpiod_module=gpiod)
        event = waiter.wait(100)
        self.assertEqual((event.source, event.edge, event.observed),
                         ("gpiod", "rising", True))
        self.assertEqual(gpiod.requests[0]["type"], gpiod.LINE_REQ_EV_EDGE)
        self.assertEqual(gpiod.requests[0]["flags"],
                         gpiod.LINE_REQ_FLAG_ACTIVE_LOW)
        waiter.close()

    def test_v2_falling_edge(self):
        gpiod = make_gpiod_v2([11])
        waiter = spi.GpioIrqWaiter(7, backend="gpiod", edge="falling",
                                   active_low=True, gpiod_module=gpiod)
        event = waiter.wait(100)
        self.assertEqual((event.source, event.edge), ("gpiod", "falling"))
        self.assertEqual(gpiod.state["requested"][0], "gpiochip0")
        self.assertIs(gpiod.state["settings"]["edge_detection"],
                      gpiod.Edge.FALLING)
        self.assertTrue(gpiod.state["settings"]["active_low"])

    def test_timeout(self):
        gpiod = make_gpiod_v1([])
        waiter = spi.GpioIrqWaiter(42, backend="gpiod", gpiod_module=gpiod)
        with self.assertRaises(spi.SpiTimeout):
            waiter.wait(5)


class TestSysfsWaiter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.gpio = os.path.join(self.base, "gpio42")
        os.mkdir(self.gpio)
        self.value = os.path.join(self.gpio, "value")
        self._write(self.value, "0")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="ascii") as fh:
            fh.write(text + "\n")

    def _ready_select(self):
        def fake_select(rlist, wlist, xlist, timeout):
            return (list(rlist), [], [])
        return mock.patch.object(spi.select, "select", side_effect=fake_select)

    def test_rising_and_falling_are_derived_from_the_level(self):
        waiter = spi.GpioIrqWaiter(42, backend="sysfs",
                                   sysfs_base=self.base)
        waiter._open_impl()
        with self._ready_select():
            self._write(self.value, "1")
            rising = waiter.wait(10)
            self._write(self.value, "0")
            falling = waiter.wait(10)
        self.assertEqual((rising.edge, rising.value), ("rising", 1))
        self.assertEqual((falling.edge, falling.value), ("falling", 0))
        waiter.close()

    def test_active_low_is_emulated_when_the_kernel_has_no_attribute(self):
        waiter = spi.GpioIrqWaiter(42, backend="sysfs",
                                   sysfs_base=self.base, active_low=True)
        waiter._open_impl()
        with self._ready_select():
            # raw 0 is the asserted level for an active-low line
            self._write(self.value, "1")   # deasserted
            event = waiter.wait(10)
        self.assertEqual(event.value, 0)
        self.assertEqual(event.edge, "falling")
        waiter.close()

    def test_timeout_returns_no_edge(self):
        waiter = spi.GpioIrqWaiter(42, backend="sysfs",
                                   sysfs_base=self.base)
        with mock.patch.object(spi.select, "select",
                               side_effect=lambda *a: ([], [], [])):
            with self.assertRaises(spi.SpiTimeout):
                waiter.wait(10)
        waiter.close()

    def test_exported_line_is_unexported_on_close(self):
        base = os.path.join(self.tmp.name, "fresh")
        os.mkdir(base)
        written = []

        # export is simulated: creating gpio43 when export is written.
        def fake_write(path, text):
            written.append((path, text))
            if path.endswith("/export"):
                gpio = os.path.join(base, "gpio" + text.strip())
                os.mkdir(gpio)
                with open(os.path.join(gpio, "value"), "w",
                          encoding="ascii") as fh:
                    fh.write("0\n")
            else:
                with open(path, "w", encoding="ascii") as fh:
                    fh.write(text + "\n")

        with mock.patch.object(spi._SysfsWaiter, "_write",
                               side_effect=fake_write):
            waiter = spi._SysfsWaiter(43, "rising", False, base)
            self.assertTrue(waiter._exported_here)
            waiter.close()
        self.assertIn((os.path.join(base, "unexport"), "43"), written)


class TestPollWaiter(unittest.TestCase):
    def test_poll_reports_unobserved_and_warns(self):
        clock = FakeClock()
        warnings_seen = []
        waiter = spi.GpioIrqWaiter(1, backend="poll", clock=clock.now,
                                   sleep=clock.sleep,
                                   warn=lambda message, *a, **k:
                                   warnings_seen.append(message))
        event = waiter.wait(100)
        self.assertEqual(event.source, "poll")
        self.assertFalse(event.observed)
        self.assertEqual(clock.sleeps, [0.100])
        self.assertEqual(len(warnings_seen), 1)

    def test_capture_wait_rejects_unobserved_poll_results(self):
        clock = FakeClock()
        transport = spi.SpiTransport(
            "/dev/fake", allow_writes=True, spi_io=RecordingIO(),
            irq=spi.GpioIrqWaiter(1, backend="poll", clock=clock.now,
                                  sleep=clock.sleep, warn=lambda *a, **k: None),
            clock=clock.now, sleep=clock.sleep)
        with self.assertRaises(spi.SpiTimeout):
            transport.capture_wait(timeout_ms=100, attempts=3)
        self.assertEqual(clock.sleeps, [0.100, 0.050, 0.100, 0.050, 0.100])

    def test_capture_wait_retries_then_succeeds(self):
        clock = FakeClock()
        irq = FakeIrq([spi.SpiTimeout("no edge"),
                       spi.IrqEvent("gpiod", 1.0, "rising")])
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=RecordingIO(), irq=irq,
                                     clock=clock.now, sleep=clock.sleep)
        event = transport.capture_wait(timeout_ms=100, attempts=3)
        self.assertEqual(event.edge, "rising")
        self.assertEqual(clock.sleeps, [0.050])


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset(unittest.TestCase):
    def test_pulse_is_assert_5_ms_release_10_ms_with_logical_values(self):
        gpiod = make_gpiod_v1([])
        requested = {}
        real_chip = gpiod.Chip

        def fake_chip(name):
            chip = real_chip(name)
            line = chip.get_line(1)

            def request(**kwargs):
                requested.update(kwargs)
                requested["line"] = line
            line.request = request
            return chip

        gpiod.Chip = fake_chip
        clock = FakeClock()
        reset = spi.GpioResetLine(1, active_low=True,
                                  gpiod_module=gpiod, sleep=clock.sleep)
        reset.pulse()
        self.assertEqual(requested["flags"], gpiod.LINE_REQ_FLAG_ACTIVE_LOW)
        self.assertEqual(requested["line"].values, [1, 0])
        self.assertEqual(clock.sleeps, [0.005, 0.010])

    def test_transport_resets_after_open_then_sleeps_50_ms(self):
        events = []
        clock = FakeClock()

        class RecordingReset:
            def pulse(self):
                events.append("pulse")

            def close(self):
                events.append("close")

        class RecordingIO2(RecordingIO):
            def open(self):
                events.append("open")
                self.opened = True

            def configure(self, **kwargs):
                events.append("configure")

        io_obj = RecordingIO2(b"\x00" * 7)
        transport = spi.SpiTransport(
            "/dev/fake", allow_writes=True, spi_io=io_obj,
            reset=RecordingReset(), allow_reset=True,
            clock=clock.now, sleep=clock.sleep)
        transport.read(0xA0, 7)
        self.assertEqual(events, ["open", "configure", "pulse"])
        self.assertEqual(clock.sleeps, [0.050])
        self.assertIn(0.050, clock.sleeps)

    def test_no_reset_without_allow_reset(self):
        class BoomReset:
            def pulse(self):
                raise AssertionError("reset must not be pulsed")

            def close(self):
                pass

        io_obj = RecordingIO(b"\x00" * 7)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     spi_io=io_obj, reset=BoomReset(),
                                     allow_reset=False)
        transport.read(0xA0, 7)
        self.assertTrue(io_obj.opened)

    def test_sysfs_output_applies_active_low(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gpio = os.path.join(tmp.name, "gpio5")
        os.mkdir(gpio)
        out = spi._SysfsOutput(5, active_low=True, base=tmp.name)
        out.set(1)
        with open(os.path.join(gpio, "value"), encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), "0")  # active_low emulated
        out.set(0)
        with open(os.path.join(gpio, "value"), encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), "1")


# ---------------------------------------------------------------------------
# Probe CLI (offline)
# ---------------------------------------------------------------------------

def synthetic_crs():
    """A minimal valid SpiSerialBus + GpioInt template (see test_acpi)."""
    source = b"\\_SB.PC00.SPI1\0"
    data = (bytes([1, 1, 2, 0]) + struct.pack("<H", 0x0002)
            + bytes([1]) + struct.pack("<H", 9)
            + struct.pack("<I", 1_000_000)
            + bytes([8, 1, 1]) + struct.pack("<H", 1) + source)
    serial = bytes([0x8E]) + struct.pack("<H", len(data)) + data

    pin_table = struct.pack("<H", 5)
    gpio_data = (bytes([1, 0, 0, 0]) + struct.pack("<H", 0x1B)
                 + bytes([0]) + struct.pack("<H", 0)
                 + struct.pack("<H", 0)
                 + struct.pack("<H", 3 + 20)
                 + bytes([0]) + struct.pack("<H", 0)
                 + struct.pack("<H", 0) + struct.pack("<H", 0)
                 + pin_table)
    gpio = bytes([0x8C]) + struct.pack("<H", len(gpio_data)) + gpio_data
    return serial + gpio + bytes([0x79, 0x00])


class TestProbeCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = support.load_probe()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.probe.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_help(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_dry_run_never_opens_a_device(self):
        with mock.patch.object(spi.LinuxSpiIO, "open",
                               side_effect=AssertionError("no open")), \
                mock.patch.object(self.probe, "SpiDev",
                                  side_effect=AssertionError("no SpiDev")):
            rc, out, err = self.run_main(
                ["--dry-run", "--spidev", "/dev/spidev-fake", "fw", "info"])
        self.assertEqual(rc, 0, err)
        self.assertIn("selector 0x40", out)
        self.assertIn("selector 0x02", out)
        self.assertIn("read_shape=cs-held", out)
        self.assertIn("40 | <- 7 B (cs held)", out)
        self.assertIn("02 | <- 9 B (cs held)", out)
        self.assertIn("timeout 1000/2030 ms", out)

    def test_dry_run_selector_only_comparison_shape(self):
        rc, out, err = self.run_main(
            ["--dry-run", "--read-shape", "selector",
             "--spidev", "/dev/spidev-fake", "fw"])
        self.assertEqual(rc, 0, err)
        self.assertIn("read_shape=selector", out)
        self.assertIn("40 | <- 7 B (cs held)", out)
        self.assertIn("lead 0 B", out)

    def test_dry_run_frame_shape_keeps_the_measured_lead(self):
        rc, out, err = self.run_main(
            ["--dry-run", "--read-shape", "frame",
             "--spidev", "/dev/spidev-fake", "fw"])
        self.assertEqual(rc, 0, err)
        self.assertIn("read_shape=frame", out)
        self.assertIn("lead 3 B: 00 00 00", out)
        self.assertIn("40 00 00 00 | <- 7 B (cs held)", out)

    def test_dry_run_uses_crs_values(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(synthetic_crs())
            path = fh.name
        self.addCleanup(os.unlink, path)
        rc, out, err = self.run_main(
            ["--dry-run", "--crs", path, "--spidev", "/dev/spidev-fake",
             "fw"])
        self.assertEqual(rc, 0, err)
        self.assertIn("speed=1000000", out)
        self.assertIn("mode=3", out)
        self.assertIn("gpio=5", out)

    def test_hardware_run_needs_allow_writes(self):
        with mock.patch.object(spi.LinuxSpiIO, "open",
                               side_effect=AssertionError("no open")):
            rc, out, err = self.run_main(["--spidev", "/dev/spidev-fake",
                                          "fw"])
        self.assertEqual(rc, 2)
        self.assertIn("--allow-writes", err)

    def test_hardware_run_needs_an_explicit_device(self):
        with mock.patch.object(spi.LinuxSpiIO, "open",
                               side_effect=AssertionError("no open")):
            rc, out, err = self.run_main(["--allow-writes", "fw"])
        self.assertEqual(rc, 2)
        self.assertIn("--spidev", err)

    def test_destructive_opcode_is_refused(self):
        with mock.patch.object(spi.LinuxSpiIO, "open",
                               side_effect=AssertionError("no open")):
            rc, out, err = self.run_main(
                ["--spidev", "/dev/spidev-fake", "--allow-writes",
                 "--op", "0xC2"])
        self.assertEqual(rc, 2)
        self.assertIn("JTAG_CONTROL", err)

    def test_destructive_alias_is_refused(self):
        with mock.patch.object(spi.LinuxSpiIO, "open",
                               side_effect=AssertionError("no open")):
            rc, out, err = self.run_main(
                ["--spidev", "/dev/spidev-fake", "--allow-writes",
                 "--allow-op", "--op", "0x02"])
        self.assertEqual(rc, 2)
        self.assertIn("reserved", err)

    def test_reset_needs_allow_reset(self):
        with mock.patch.object(spi.LinuxSpiIO, "open",
                               side_effect=AssertionError("no open")):
            rc, out, err = self.run_main(
                ["--spidev", "/dev/spidev-fake", "--allow-writes",
                 "--reset-gpio", "9", "fw"])
        self.assertEqual(rc, 2)
        self.assertIn("--allow-reset", err)

    def test_acpi_info_with_a_synthetic_crs(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(synthetic_crs())
            path = fh.name
        self.addCleanup(os.unlink, path)
        rc, out, err = self.run_main(["acpi_info", "--crs", path])
        self.assertEqual(rc, 0, err)
        self.assertIn("SerialBus SPI", out)
        self.assertIn("connection speed: 1000000 Hz", out)
        self.assertIn("GpioInt", out)
        self.assertIn("pins [5]", out)
        self.assertIn("irq active_low", out)

    def test_acpi_info_without_crs_prints_the_extraction_recipe(self):
        rc, out, err = self.run_main(["acpi_info"])
        self.assertEqual(rc, 0)
        self.assertIn("iasl -d", out)
        self.assertIn("/sys/firmware/acpi/tables/DSDT", out)

    def test_acpi_info_malformed_buffer(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(b"\x8e\xff\xff\x00")
            path = fh.name
        self.addCleanup(os.unlink, path)
        rc, out, err = self.run_main(["acpi_info", "--crs", path])
        self.assertEqual(rc, 1)
        self.assertIn("malformed", err)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Experimental read write shapes (--try-write-shape)
# ---------------------------------------------------------------------------

class TestReadShapes(unittest.TestCase):
    def test_shape_plan_is_selector_plus_padding_then_receive(self):
        plan = spi.build_shape_plan(0xA0, selector_variant="read-bit0",
                                    padding=b"\x00\x00", read_len=64,
                                    timeout_ms=400, total_ms=400)
        self.assertEqual(plan.selector, 0x41)
        self.assertEqual(plan.read_shape, "cs-held")
        self.assertEqual(plan.shape.selector_variant, "read-bit0")
        self.assertEqual(plan.transfers[0].tx,
                         b"\x41\x00\x00" + b"\x00" * 64)
        self.assertEqual(plan.transfers[0].rx_len, 67)
        self.assertEqual(plan.prefix_tx, b"\x41\x00\x00")
        self.assertEqual(plan.answer_offset, 3)
        self.assertEqual(plan.answer_len, 64)

    def test_split_shape_plan_is_kept_for_comparison(self):
        plan = spi.build_shape_plan(0xA0, selector_variant="read-bit0",
                                    padding=b"\x00\x00", read_len=64,
                                    timeout_ms=400, total_ms=400,
                                    read_shape="split")
        self.assertEqual(plan.read_shape, "split")
        self.assertEqual(plan.transfers[0].tx, b"\x41\x00\x00")
        self.assertEqual(plan.transfers[0].rx_len, 0)
        self.assertEqual(plan.transfers[1].rx_len, 64)

    def test_shape_plan_rejects_writes_and_bad_input(self):
        with self.assertRaises(ValueError):
            spi.build_shape_plan(0xA0, read_len=0)
        with self.assertRaises(ValueError):
            spi.build_shape_plan(0xA0, selector_variant="write", read_len=7)
        with self.assertRaises(ValueError):
            spi.build_shape_plan(0xA0, padding=b"\x00" * 9, read_len=7)

    def test_shape_executes_and_keeps_the_recovered_gate(self):
        io_obj = RecordingIO(b"\x00" * 64)
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0xA0], spi_io=io_obj)
        plan = spi.build_shape_plan(0xA0, selector_variant="read-bit0",
                                    padding=b"\x00", read_len=64,
                                    timeout_ms=400, total_ms=400)
        self.assertEqual(transport.execute(plan), b"\x00" * 64)
        self.assertEqual(io_obj.messages[0][0].tx[:2], b"\x41\x00")
        self.assertEqual(io_obj.messages[0][0].tx[2:], b"\x00" * 64)

    def test_shape_selector_alias_of_a_destructive_opcode_is_refused(self):
        # 0x02 (the read alias of JTAG's 0xC2) with selector bit 0 set is
        # 0x05; masking bit 0 still yields JTAG's selector 0x04.
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0x02],
                                     spi_io=RecordingIO())
        plan = spi.build_shape_plan(0x02, selector_variant="read-bit0",
                                    read_len=7, timeout_ms=400, total_ms=400)
        with self.assertRaises(spi.UnsafeCommand):
            transport.execute(plan)

    def test_shape_plan_not_on_allowlist_is_refused(self):
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0xA1],
                                     spi_io=RecordingIO())
        plan = spi.build_shape_plan(0xA0, read_len=7, timeout_ms=400,
                                    total_ms=400)
        with self.assertRaises(spi.CommandNotAllowed):
            transport.execute(plan)


# ---------------------------------------------------------------------------
# Recovered read frame (selector + observed lead bytes) and answer parsing
# ---------------------------------------------------------------------------

from fpbf63112 import protocol as _protocol  # noqa: E402  (test-only import)


class TestReadFrame(unittest.TestCase):
    def test_fw_frame_is_selector_plus_three_zero_lead_bytes(self):
        frame = spi.build_read_frame(0xA0)
        self.assertEqual(frame.selector, 0x40)
        self.assertEqual(frame.read_len, 7)
        self.assertEqual(frame.lead, b"\x00\x00\x00")
        self.assertEqual(frame.tx, b"\x40\x00\x00\x00")
        self.assertEqual(frame.transfers[0].tx,
                         b"\x40\x00\x00\x00" + b"\x00" * 7)
        self.assertEqual(frame.transfers[0].rx_len, 11)
        self.assertEqual(len(frame.transfers), 1)
        self.assertEqual(frame.hexdump(),
                         "40 00 00 00 | <- 7 B (cs held)")

    def test_scheme_frame_is_selector_plus_two_zero_lead_bytes(self):
        frame = spi.build_read_frame(0xA1)
        self.assertEqual(frame.selector, 0x42)
        self.assertEqual(frame.read_len, 64)
        self.assertEqual(frame.tx, b"\x42\x00\x00")

    def test_info_lead_is_unknown_so_it_stays_selector_only(self):
        # The 2026-11-09 write-shape run stopped at 0xA1; 0xC1's lead is not
        # measured, so the builder must not invent one.
        frame = spi.build_read_frame(0xC1)
        self.assertEqual(frame.selector, 0x02)
        self.assertEqual(frame.lead, b"")
        self.assertEqual(frame.tx, b"\x02")
        self.assertEqual(frame.read_len, 9)

    def test_explicit_empty_lead_is_the_dll_selector_only_frame(self):
        frame = spi.build_read_frame(0xA0, 7, lead=b"")
        self.assertEqual(frame.tx, b"\x40")

    def test_explicit_lead_and_selector_variant(self):
        frame = spi.build_read_frame(0xA0, 7, lead=b"\xaa",
                                     selector_variant="read-bit0")
        self.assertEqual(frame.tx, b"\x41\xaa")

    def test_bad_frame_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            spi.build_read_frame(0x7E, None)  # unknown opcode, no length
        with self.assertRaises(ValueError):
            spi.build_read_frame(0xA0, 0)
        with self.assertRaises(ValueError):
            spi.build_read_frame(0xA0, 7, lead=b"\x00" * 9)

    def test_send_read_builds_and_executes_the_frame(self):
        io_obj = RecordingIO(b"\xff\x7f\x50\x03\x00\x00\x00")
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0xA0], spi_io=io_obj)
        answer = spi.send_read(transport, 0xA0)
        self.assertEqual(answer, b"\xff\x7f\x50\x03\x00\x00\x00")
        self.assertEqual(io_obj.messages[0][0].tx[:4],
                         b"\x40\x00\x00\x00")
        self.assertEqual(io_obj.messages[0][0].tx[4:], b"\x00" * 7)

    def test_read_frame_keeps_gates(self):
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0xA1],
                                     spi_io=RecordingIO(b"\x00" * 7))
        with self.assertRaises(spi.CommandNotAllowed):
            transport.read_frame(0xA0)
        no_write = spi.SpiTransport("/dev/fake", spi_io=RecordingIO())
        with self.assertRaises(spi.CommandNotAllowed):
            no_write.read_frame(0xA0)

    def test_transport_read_command_frame(self):
        io_obj = RecordingIO(b"\xff\x7f\x50\x03\x00\x00\x00")
        transport = spi.SpiTransport("/dev/fake", allow_writes=True,
                                     read_allowlist=[0xA0], spi_io=io_obj)
        self.assertEqual(transport.read_command_frame("fw"),
                         b"\xff\x7f\x50\x03\x00\x00\x00")
        self.assertEqual(io_obj.messages[0][0].tx[:4],
                         b"\x40\x00\x00\x00")
        self.assertEqual(io_obj.messages[0][0].tx[4:], b"\x00" * 7)


class TestReadAnswer(unittest.TestCase):
    def test_cs_held_answer_has_no_lead_bytes_to_skip(self):
        answer = b"\xff\x7f\x50\x03\x00\x00\x00"
        parsed = spi.parse_read_answer(0xA0, answer, read_shape="cs-held")
        self.assertEqual(parsed.framing, "bare")
        self.assertEqual(parsed.skipped, 0)
        self.assertEqual(parsed.payload, answer)
        self.assertEqual(parsed.fields.version, 3)
        with self.assertRaises(ValueError):
            spi.parse_read_answer(0xA0, answer, read_shape="nope")

    def test_bare_fw_answer_with_lead(self):
        answer = b"\xff\x7f\x50\x03\x00\x00\x00"
        parsed = spi.parse_read_answer(0xA0, answer, lead=b"\x00\x00\x00")
        self.assertEqual(parsed.framing, "bare")
        self.assertEqual(parsed.skipped, 0)
        self.assertEqual(parsed.payload, answer)
        self.assertIsInstance(parsed.fields, _protocol.FirmwareInfo)
        self.assertEqual(parsed.fields.state, 0xFF)
        self.assertEqual(parsed.fields.project_hi, 0x7F)
        self.assertEqual(parsed.fields.project_lo, 0x50)
        self.assertEqual(parsed.fields.version, 3)

    def test_bare_fw_answer_from_the_selector_only_frame_is_shifted(self):
        raw = b"\x00\x00\x00\xff\x7f\x50\x03\x00\x00\x00"
        parsed = spi.parse_read_answer(0xA0, raw, lead=b"")
        self.assertEqual(parsed.skipped, 3)
        self.assertEqual(parsed.payload, b"\xff\x7f\x50\x03\x00\x00\x00")
        self.assertEqual(parsed.fields.version, 3)
        # The recovered 7-byte read window loses 3 payload bytes when the
        # selector-only frame is used, so the strict parse of that run fails.
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xA0, raw[:7], lead=b"")

    def test_non_zero_lead_bytes_are_rejected(self):
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xA0, b"\x01\x00\x00\xff\x7f\x50\x03",
                                  lead=b"")

    def test_short_bare_answers_are_rejected(self):
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xA0, b"\x01\x02")
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xC1, b"\x00" * 8)

    def test_bare_sensor_info(self):
        payload = _protocol.SensorInfo.build(width=0x01F4, height=0x03E8,
                                             flags=3)
        parsed = spi.parse_read_answer(0xC1, payload, lead=b"")
        self.assertEqual(parsed.fields.width, 0x01F4)
        self.assertEqual(parsed.fields.height, 0x03E8)
        self.assertEqual(parsed.fields.flags, 3)

    def test_bare_scheme_table_has_no_decoded_fields(self):
        parsed = spi.parse_read_answer(0xA1, b"\xff\x7f\x50\x03" + b"\x00" * 60,
                                       lead=b"\x00\x00")
        self.assertIsNone(parsed.fields)

    def test_empty_answer_is_rejected(self):
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xA0, b"", lead=b"")

    def test_crc32_valid_frame_is_accepted(self):
        body = _protocol.FirmwareInfo.build(0xFF, 0x7F, 0x50, 3)
        frame = _protocol.build_frame(0xA0, data=body)
        parsed = spi.parse_read_answer(0xA0, frame)
        self.assertEqual(parsed.framing, "frame")
        self.assertEqual(parsed.payload, body)
        self.assertEqual(parsed.fields.version, 3)

    def test_crc32_mismatch_is_rejected(self):
        body = _protocol.FirmwareInfo.build(0xFF, 0x7F, 0x50, 3)
        frame = bytearray(_protocol.build_frame(0xA0, data=body))
        frame[-1] ^= 0xFF  # corrupt the last CRC-32 byte
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xA0, bytes(frame))

    def test_truncated_frame_and_wrong_opcode_are_rejected(self):
        body = _protocol.FirmwareInfo.build(0xFF, 0x7F, 0x50, 3)
        frame = _protocol.build_frame(0xA0, data=body)
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xA0, frame[:5])
        with self.assertRaises(spi.AnswerError):
            spi.parse_read_answer(0xC1, _protocol.build_frame(0xA0, data=body))
