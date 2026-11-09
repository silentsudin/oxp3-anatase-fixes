"""GPIO v2 character-device backend for the BF63112 reset/IRQ lines.

Everything here is synthetic: the chip is a fake ``/dev/gpiochipN`` driven
through monkeypatched ``os.open``/``os.close``/``os.read``/``fcntl.ioctl``/
``select.select``.  No test opens a real GPIO character device, touches
``/sys/class/gpio`` or needs root.

The tests pin the uAPI v2 ABI this repository believes (linux/gpio.h):
``GPIO_GET_CHIPINFO_IOCTL`` 0x8044B401 (68 B), ``GPIO_V2_GET_LINE_IOCTL``
0xC250B407 (592 B), ``GPIO_V2_LINE_GET/SET_VALUES_IOCTL`` 0xC010B40E/0F
(16 B), and a 48-byte ``struct gpio_v2_line_event``.  A wrong struct size
changes the ioctl number, so these assertions fail before a real kernel could
misread the buffer.
"""

import contextlib
import errno
import importlib.util
import io
import os
import pathlib
import struct
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[2]
FINGERPRINT = REPO / "tools" / "fingerprint"
if str(FINGERPRINT) not in sys.path:
    sys.path.insert(0, str(FINGERPRINT))

from fpbf63112 import spi  # noqa: E402


# ---------------------------------------------------------------------------
# Fake kernel: one /dev/gpiochipN per fake, every ioctl decoded
# ---------------------------------------------------------------------------

def parse_line_request(raw):
    """Decode the parts of struct gpio_v2_line_request the tests assert."""
    offsets = struct.unpack_from("<64I", raw, 0)
    consumer = struct.unpack_from("<32s", raw, 256)[0].split(b"\0", 1)[0]
    flags = struct.unpack_from("<Q", raw, 288)[0]
    num_attrs = struct.unpack_from("<I", raw, 296)[0]
    attrs = [struct.unpack_from("<IIQQ", raw, 320 + index * 24)
             for index in range(num_attrs)]
    num_lines = struct.unpack_from("<I", raw, 560)[0]
    event_buffer_size = struct.unpack_from("<I", raw, 564)[0]
    fd = struct.unpack_from("<i", raw, 588)[0]
    return {
        "offsets": offsets,
        "consumer": consumer,
        "flags": flags,
        "num_attrs": num_attrs,
        "attrs": attrs,
        "num_lines": num_lines,
        "event_buffer_size": event_buffer_size,
        "fd": fd,
    }


def line_event(timestamp_ns=123456, event_id=1, offset=18):
    return struct.pack("<QIIII6I", timestamp_ns, event_id, offset,
                       7, 8, 0, 0, 0, 0, 0, 0)


class FakeKernel:
    def __init__(self):
        self.chips = {}          # path -> {"name", "label", "lines"}
        self.fail_ioctl = {}     # ioctl request -> errno
        self.fail_open = {}      # path -> errno
        self.opened = []         # (path, flags)
        self.fd_path = {}
        self.closed = []
        self.ioctls = []         # (fd, request, bytes)
        self.requests = []       # raw struct gpio_v2_line_request
        self.set_values = []     # (bits, mask)
        self.reads = []          # (fd, count)
        self.read_data = b""
        self.read_errno = None
        self.select_ready = False
        self.select_interrupt = False
        self.select_timeouts = []
        self.next_fd = 10
        self.request_fd = 100

    # -- fake syscalls -----------------------------------------------------
    def add_chip(self, path, name="INTC10BC", label="", lines=32):
        self.chips[path] = {"name": name, "label": label, "lines": lines}
        return path

    def open(self, path, flags=0):
        self.opened.append((path, flags))
        if path in self.fail_open:
            raise OSError(self.fail_open[path],
                          os.strerror(self.fail_open[path]), path)
        if path not in self.chips:
            raise FileNotFoundError(errno.ENOENT,
                                    os.strerror(errno.ENOENT), path)
        fd = self.next_fd
        self.next_fd += 1
        self.fd_path[fd] = path
        return fd

    def close(self, fd):
        self.closed.append(fd)

    def ioctl(self, fd, request, arg, *rest):
        self.ioctls.append((fd, request, bytes(arg)))
        if request in self.fail_ioctl:
            code = self.fail_ioctl[request]
            raise OSError(code, os.strerror(code))
        info = self.chips.get(self.fd_path.get(fd))
        if request == spi.GPIO_GET_CHIPINFO_IOCTL:
            assert info is not None
            struct.pack_into("<32s32sI", arg, 0,
                             info["name"].encode(), info["label"].encode(),
                             info["lines"])
        elif request == spi.GPIO_V2_GET_LINE_IOCTL:
            self.requests.append(bytes(arg))
            struct.pack_into("<i", arg,
                             spi._GPIO_LINE_REQUEST_FD_OFFSET,
                             self.request_fd)
        elif request == spi.GPIO_V2_LINE_SET_VALUES_IOCTL:
            self.set_values.append(struct.unpack_from("<QQ", arg, 0))
        else:
            raise AssertionError(f"unexpected ioctl 0x{request:08x}")
        return 0

    def read(self, fd, count):
        self.reads.append((fd, count))
        if self.read_errno is not None:
            raise OSError(self.read_errno, os.strerror(self.read_errno))
        data, self.read_data = self.read_data[:count], self.read_data[count:]
        return data

    def select(self, rlist, wlist, xlist, timeout):
        self.select_timeouts.append(timeout)
        if self.select_interrupt:
            raise InterruptedError("EINTR")
        if self.select_ready:
            return (list(rlist), [], [])
        return ([], [], [])

    @contextlib.contextmanager
    def patched(self):
        with mock.patch.object(spi.os, "open", side_effect=self.open), \
                mock.patch.object(spi.os, "close", side_effect=self.close), \
                mock.patch.object(spi.os, "read", side_effect=self.read), \
                mock.patch.object(spi.fcntl, "ioctl",
                                  side_effect=self.ioctl), \
                mock.patch.object(spi.select, "select",
                                  side_effect=self.select):
            yield self


# ---------------------------------------------------------------------------
# ABI constants and request packing
# ---------------------------------------------------------------------------

class TestGpioV2Abi(unittest.TestCase):
    def test_ioctl_numbers_match_the_kernel_encoding(self):
        self.assertEqual(spi.GPIO_GET_CHIPINFO_IOCTL, 0x8044B401)
        self.assertEqual(spi.GPIO_V2_GET_LINE_IOCTL, 0xC250B407)
        self.assertEqual(spi.GPIO_V2_LINE_GET_VALUES_IOCTL, 0xC010B40E)
        self.assertEqual(spi.GPIO_V2_LINE_SET_VALUES_IOCTL, 0xC010B40F)

    def test_struct_sizes(self):
        self.assertEqual(spi._GPIOCHIP_INFO.size, 68)
        self.assertEqual(spi._GPIO_LINE_VALUES.size, 16)
        self.assertEqual(spi._GPIO_LINE_CONFIG_ATTR.size, 24)
        self.assertEqual(spi._GPIO_LINE_CONFIG_SIZE, 272)
        self.assertEqual(spi._GPIO_LINE_REQUEST_SIZE, 592)
        self.assertEqual(spi._GPIO_LINE_EVENT.size, 48)
        self.assertEqual(spi._GPIO_LINE_REQUEST_FD_OFFSET, 588)

    def test_request_packing(self):
        raw = spi.build_line_request(
            (18,), spi.GPIO_V2_LINE_FLAG_INPUT,
            consumer="fpbf63112", event_buffer_size=4)
        request = parse_line_request(raw)
        self.assertEqual(len(raw), 592)
        self.assertEqual(request["offsets"][0], 18)
        self.assertEqual(request["offsets"][1:], (0,) * 63)
        self.assertEqual(request["consumer"], b"fpbf63112")
        self.assertEqual(request["flags"], spi.GPIO_V2_LINE_FLAG_INPUT)
        self.assertEqual(request["num_attrs"], 0)
        self.assertEqual(request["num_lines"], 1)
        self.assertEqual(request["event_buffer_size"], 4)
        self.assertEqual(request["fd"], -1)
        self.assertEqual(raw[560:588],
                         struct.pack("<II5I", 1, 4, 0, 0, 0, 0, 0))
        self.assertEqual(raw[588:592], b"\xff" * 4)

    def test_request_validates_offsets_and_attributes(self):
        with self.assertRaises(spi.GpioLineOutOfRange):
            spi.build_line_request((-1,), spi.GPIO_V2_LINE_FLAG_INPUT)
        with self.assertRaises(spi.GpioInvalidRequest):
            spi.build_line_request((), spi.GPIO_V2_LINE_FLAG_INPUT)
        with self.assertRaises(spi.GpioInvalidRequest):
            spi.build_line_request(
                (1,), spi.GPIO_V2_LINE_FLAG_OUTPUT,
                attrs=[(spi.GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES, 0, 1)] * 11)

    def test_decode_line_event(self):
        event = spi.decode_line_event(line_event(999, 1, 18))
        self.assertEqual((event.timestamp_ns, event.event_id, event.offset),
                         (999, 1, 18))
        self.assertEqual(event.edge, "rising")
        self.assertEqual(spi.decode_line_event(line_event(1, 2, 16)).edge,
                         "falling")
        self.assertIsNone(spi.decode_line_event(line_event(1, 9, 16)).edge)
        with self.assertRaises(spi.GpioInvalidRequest):
            spi.decode_line_event(b"\0" * 47)
        with self.assertRaises(spi.GpioInvalidRequest):
            spi.decode_line_events(line_event() + b"\0")


# ---------------------------------------------------------------------------
# Output line (reset)
# ---------------------------------------------------------------------------

class TestChardevOutput(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel()
        self.kernel.add_chip("/dev/gpiochip1", label="GPI1", lines=32)

    def open_output(self, line=16, active_low=False):
        with self.kernel.patched():
            return spi._ChardevOutput(line, chip="gpiochip1",
                                      active_low=active_low)

    def test_output_high_request_and_values(self):
        with self.kernel.patched():
            out = spi._ChardevOutput(16, chip="gpiochip1", active_low=False)
            out.set(1)
            out.set(0)
            out.close()
        path, flags = self.kernel.opened[0]
        self.assertEqual(path, "/dev/gpiochip1")
        self.assertEqual(flags, os.O_RDWR | os.O_CLOEXEC)
        request = parse_line_request(self.kernel.requests[0])
        self.assertEqual(request["offsets"][0], 16)
        self.assertEqual(request["num_lines"], 1)
        self.assertEqual(request["consumer"], b"fpbf63112-reset")
        self.assertEqual(request["flags"], spi.GPIO_V2_LINE_FLAG_OUTPUT)
        self.assertEqual(request["num_attrs"], 1)
        self.assertEqual(request["attrs"][0],
                         (spi.GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES, 0, 0, 1))
        self.assertEqual(request["event_buffer_size"], 0)
        self.assertEqual(self.kernel.set_values, [(1, 1), (0, 1)])
        self.assertEqual(self.kernel.closed,
                         [self.kernel.request_fd, 10])

    def test_active_low_inverts_the_physical_value(self):
        with self.kernel.patched():
            out = spi._ChardevOutput(16, chip="gpiochip1", active_low=True)
            request = parse_line_request(self.kernel.requests[0])
            self.assertEqual(request["attrs"][0],
                             (spi.GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES, 0, 1, 1))
            self.assertFalse(request["flags"]
                             & spi.GPIO_V2_LINE_FLAG_ACTIVE_LOW)
            out.set(1)
            out.set(0)
            out.close()
        self.assertEqual(self.kernel.set_values, [(0, 1), (1, 1)])

    def test_line_out_of_range_never_requests(self):
        with self.kernel.patched():
            with self.assertRaises(spi.GpioLineOutOfRange):
                spi._ChardevOutput(32, chip="gpiochip1")
        self.assertEqual(self.kernel.requests, [])
        self.assertEqual(self.kernel.closed, [10])

    def test_einval_on_the_line_request(self):
        self.kernel.fail_ioctl[spi.GPIO_V2_GET_LINE_IOCTL] = errno.EINVAL
        with self.kernel.patched():
            with self.assertRaises(spi.GpioInvalidRequest):
                spi._ChardevOutput(16, chip="gpiochip1")
        self.assertEqual(self.kernel.closed, [10])

    def test_einval_on_set_values(self):
        self.kernel.fail_ioctl[spi.GPIO_V2_LINE_SET_VALUES_IOCTL] = errno.EINVAL
        with self.kernel.patched():
            out = spi._ChardevOutput(16, chip="gpiochip1")
            with self.assertRaises(spi.GpioInvalidRequest):
                out.set(1)
            out.close()

    def test_enotty_on_chipinfo(self):
        self.kernel.fail_ioctl[spi.GPIO_GET_CHIPINFO_IOCTL] = errno.ENOTTY
        with self.kernel.patched():
            with self.assertRaises(spi.GpioUnsupported) as caught:
                spi._ChardevOutput(16, chip="gpiochip1")
        self.assertIn("v2 uAPI", str(caught.exception))
        self.assertEqual(self.kernel.closed, [10])

    def test_enotty_on_the_line_request(self):
        self.kernel.fail_ioctl[spi.GPIO_V2_GET_LINE_IOCTL] = errno.ENOTTY
        with self.kernel.patched():
            with self.assertRaises(spi.GpioUnsupported) as caught:
                spi._ChardevOutput(16, chip="gpiochip1")
        self.assertIn("ENOTTY", str(caught.exception))
        self.assertEqual(self.kernel.closed, [10])

    def test_missing_chip(self):
        with self.kernel.patched():
            with self.assertRaises(spi.GpioChipNotFound):
                spi._ChardevOutput(16, chip="gpiochip9")

    def test_eacces_is_typed(self):
        self.kernel.fail_open["/dev/gpiochip1"] = errno.EACCES
        with self.kernel.patched():
            with self.assertRaises(spi.GpioPermissionDenied):
                spi._ChardevOutput(16, chip="gpiochip1")

    def test_path_and_label_lookup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        first = os.path.join(tmp.name, "gpiochip0")
        second = os.path.join(tmp.name, "gpiochip1")
        open(first, "wb").close()
        open(second, "wb").close()
        self.kernel.add_chip(first, name="INTC10BC", label="", lines=8)
        self.kernel.add_chip(second, name="INTC10BC", label="GPI1", lines=8)
        with self.kernel.patched():
            out = spi._ChardevOutput(6, chip="GPI1", dev_dir=tmp.name)
            out.close()
        self.assertEqual([p for p, _flags in self.kernel.opened],
                         [first, second, second])
        self.assertEqual(parse_line_request(self.kernel.requests[0])["offsets"][0],
                         6)
        # A /dev path is used verbatim.
        with self.kernel.patched():
            out = spi._ChardevOutput(6, chip="/dev/gpiochip1")
            out.close()
        self.assertEqual(self.kernel.opened[-1][0], "/dev/gpiochip1")


# ---------------------------------------------------------------------------
# Input/event line (interrupt)
# ---------------------------------------------------------------------------

class TestChardevWaiter(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel()
        self.kernel.add_chip("/dev/gpiochip1", label="GPI1", lines=32)

    def open_waiter(self, edge="rising", active_low=False, line=18):
        with self.kernel.patched():
            return spi._ChardevWaiter(line, "gpiochip1", edge,
                                      active_low=active_low)

    def test_edge_request_flags(self):
        expected = {
            "rising": spi.GPIO_V2_LINE_FLAG_EDGE_RISING,
            "falling": spi.GPIO_V2_LINE_FLAG_EDGE_FALLING,
            "both": (spi.GPIO_V2_LINE_FLAG_EDGE_RISING
                     | spi.GPIO_V2_LINE_FLAG_EDGE_FALLING),
        }
        for index, (edge, flag) in enumerate(expected.items()):
            self.kernel.requests.clear()
            chip_fd = self.kernel.next_fd
            with self.kernel.patched():
                waiter = spi._ChardevWaiter(18, "gpiochip1", edge)
                waiter.close()
            request = parse_line_request(self.kernel.requests[0])
            self.assertEqual(
                request["flags"],
                spi.GPIO_V2_LINE_FLAG_INPUT | flag, edge)
            self.assertEqual(request["num_attrs"], 0)
            self.assertEqual(request["consumer"], b"fpbf63112")
            self.assertEqual(request["offsets"][0], 18)
            self.assertEqual(request["event_buffer_size"], 0)
            self.assertFalse(request["flags"]
                             & spi.GPIO_V2_LINE_FLAG_ACTIVE_LOW)
            self.assertEqual(self.kernel.closed[-2:],
                             [self.kernel.request_fd, chip_fd],
                             f"iteration {index}")

    def test_event_decode_and_timestamp(self):
        self.kernel.select_ready = True
        self.kernel.read_data = line_event(424242, 1, 18)
        with self.kernel.patched():
            waiter = spi.GpioIrqWaiter(18, chip="gpiochip1",
                                       backend="chardev",
                                       clock=lambda: 0.0)
            event = waiter.wait(50)
            waiter.close()
        self.assertEqual((event.source, event.edge, event.observed),
                         ("chardev", "rising", True))
        self.assertEqual(event.timestamp_ns, 424242)
        self.assertEqual(self.kernel.select_timeouts, [0.05])
        self.assertEqual(self.kernel.reads, [(self.kernel.request_fd, 48)])

    def test_active_low_inverts_the_logical_edge(self):
        self.kernel.select_ready = True
        self.kernel.read_data = line_event(1, 1, 18)  # physical rising
        with self.kernel.patched():
            waiter = spi._ChardevWaiter(18, "gpiochip1", "both",
                                        active_low=True)
            self.assertEqual(waiter.wait(0.1), ("falling", None))
            waiter.close()
        self.kernel.read_data = line_event(2, 2, 18)  # physical falling
        with self.kernel.patched():
            waiter = spi._ChardevWaiter(18, "gpiochip1", "both",
                                        active_low=True)
            self.assertEqual(waiter.wait(0.1), ("rising", None))
            waiter.close()

    def test_timeout_is_clean(self):
        self.kernel.select_ready = False
        with self.kernel.patched():
            waiter = spi.GpioIrqWaiter(18, chip="gpiochip1",
                                       backend="chardev",
                                       clock=lambda: 0.0)
            with self.assertRaises(spi.SpiTimeout):
                waiter.wait(5)
            waiter.close()
        self.assertEqual(self.kernel.reads, [])

    def test_eintr_is_a_clean_timeout(self):
        self.kernel.select_interrupt = True
        with self.kernel.patched():
            waiter = spi.GpioIrqWaiter(18, chip="gpiochip1",
                                       backend="chardev",
                                       clock=lambda: 0.0)
            with self.assertRaises(spi.SpiTimeout):
                waiter.wait(5)
            waiter.close()

    def test_unknown_event_id_is_not_an_edge(self):
        self.kernel.select_ready = True
        self.kernel.read_data = line_event(1, 9, 18)
        with self.kernel.patched():
            waiter = spi.GpioIrqWaiter(18, chip="gpiochip1",
                                       backend="chardev",
                                       clock=lambda: 0.0)
            with self.assertRaises(spi.SpiTimeout):
                waiter.wait(5)
            waiter.close()

    def test_read_error_is_typed(self):
        self.kernel.select_ready = True
        self.kernel.read_errno = errno.EIO
        with self.kernel.patched():
            waiter = spi.GpioIrqWaiter(18, chip="gpiochip1",
                                       backend="chardev",
                                       clock=lambda: 0.0)
            with self.assertRaises(spi.GpioError):
                waiter.wait(5)
            waiter.close()


# ---------------------------------------------------------------------------
# Backend selection and wiring
# ---------------------------------------------------------------------------

class TestBackendSelection(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel()

    def test_forced_chardev_does_not_fall_back(self):
        self.kernel.fail_open["/dev/gpiochip9"] = errno.ENOENT
        with self.kernel.patched():
            waiter = spi.GpioIrqWaiter(18, chip="gpiochip9",
                                       backend="chardev")
            with self.assertRaises(spi.IrqUnavailable) as caught:
                waiter.wait(5)
        self.assertIn("chardev", str(caught.exception))
        self.assertIn("not found", str(caught.exception))
        self.assertEqual(self.kernel.select_timeouts, [])

    def test_auto_tries_gpiod_then_chardev(self):
        self.kernel.add_chip("/dev/gpiochip1", lines=32)
        self.kernel.select_ready = True
        self.kernel.read_data = line_event(7, 1, 18)
        with self.kernel.patched(), \
                mock.patch.object(spi, "_GpiodWaiter",
                                  side_effect=ImportError("no gpiod")):
            waiter = spi.GpioIrqWaiter(18, chip="gpiochip1", backend="auto",
                                       clock=lambda: 0.0)
            event = waiter.wait(50)
            waiter.close()
        self.assertEqual(event.source, "chardev")
        self.assertEqual(event.edge, "rising")

    def test_auto_falls_back_to_poll(self):
        self.kernel.fail_open["/dev/gpiochip9"] = errno.ENOENT
        clock = FakeClock()
        warned = []
        with self.kernel.patched(), \
                mock.patch.object(spi, "_GpiodWaiter",
                                  side_effect=ImportError("no gpiod")):
            waiter = spi.GpioIrqWaiter(
                18, chip="gpiochip9", backend="auto",
                sysfs_base="/nonexistent-oxp3-gpio",
                clock=clock.now, sleep=clock.sleep,
                warn=lambda *args, **kwargs: warned.append(args[0]))
            event = waiter.wait(25)
        self.assertEqual((event.source, event.observed), ("poll", False))
        self.assertEqual(clock.sleeps, [0.025])
        self.assertEqual(len(warned), 1)

    def test_environment_override_and_precedence(self):
        with mock.patch.dict(os.environ,
                             {"FPBF63112_GPIO_BACKEND": "chardev"}):
            self.assertEqual(spi.GpioIrqWaiter(1, backend="auto").backend,
                             "chardev")
            self.assertEqual(spi.GpioResetLine(1, backend="auto").backend,
                             "chardev")
            self.assertEqual(spi.GpioIrqWaiter(1, backend="sysfs").backend,
                             "sysfs")
        with mock.patch.dict(os.environ,
                             {"FPBF63112_GPIO_BACKEND": "not-a-backend"}):
            with self.assertRaises(ValueError):
                spi.GpioIrqWaiter(1, backend="auto")

    def test_irq_backend_order_constant(self):
        self.assertEqual(spi.IRQ_BACKENDS,
                         ("gpiod", "chardev", "sysfs", "poll"))
        self.assertEqual(spi.RESET_BACKENDS, ("gpiod", "chardev", "sysfs"))


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class TestResetLineChardev(unittest.TestCase):
    def test_pulse_uses_physical_levels_and_releases(self):
        kernel = FakeKernel()
        kernel.add_chip("/dev/gpiochip1", lines=32)
        clock = FakeClock()
        with kernel.patched():
            reset = spi.GpioResetLine(16, chip="gpiochip1",
                                      active_low=True, backend="chardev",
                                      sleep=clock.sleep)
            reset.pulse()
            reset.close()
        self.assertEqual(kernel.set_values, [(0, 1), (1, 1)])
        self.assertEqual(clock.sleeps, [0.005, 0.010])
        self.assertEqual(parse_line_request(kernel.requests[0])["offsets"][0],
                         16)
        self.assertEqual(kernel.closed,
                         [kernel.request_fd, 10])


# ---------------------------------------------------------------------------
# Probe CLI: --gpio-backend
# ---------------------------------------------------------------------------

def load_probe():
    path = FINGERPRINT / "oxp3-fp-probe.py"
    spec = importlib.util.spec_from_file_location("oxp3_fp_probe_gpio", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestProbeCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = load_probe()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.probe.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_help_lists_the_backend_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                self.probe.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--gpio-backend", out.getvalue())

    def test_poll_backend_refuses_the_reset_line(self):
        rc, _out, err = self.run_main(
            ["--spidev", "/dev/spidev-fake", "--allow-writes",
             "--reset-gpio", "16", "--allow-reset",
             "--gpio-backend", "poll", "fw"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot drive the reset line", err)

    def test_backend_flag_reaches_both_gpio_constructors(self):
        calls = {}

        class FakeIrq:
            def close(self):
                pass

        class FakeTransport:
            def __init__(self, *args, **kwargs):
                calls["transport"] = (args, kwargs)

            def describe(self):
                return "fake"

            def execute(self, plan):
                return b"\x00" * plan.answer_len

            def close(self):
                pass

        def make_irq(*args, **kwargs):
            calls["irq"] = (args, kwargs)
            return FakeIrq()

        def make_reset(*args, **kwargs):
            calls["reset"] = (args, kwargs)

            class FakeReset:
                def close(self):
                    pass
            return FakeReset()

        with mock.patch.object(spi, "SpiTransport", FakeTransport), \
                mock.patch.object(spi, "GpioIrqWaiter", side_effect=make_irq), \
                mock.patch.object(spi, "GpioResetLine", side_effect=make_reset):
            rc, _out, err = self.run_main(
                ["--spidev", "/dev/spidev-fake", "--allow-writes",
                 "--gpio", "18", "--reset-gpio", "16", "--allow-reset",
                 "--gpio-chip", "gpiochip1", "--gpio-backend", "chardev",
                 "fw"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["irq"][0], (18,))
        self.assertEqual(calls["irq"][1]["chip"], "gpiochip1")
        self.assertEqual(calls["irq"][1]["backend"], "chardev")
        self.assertEqual(calls["reset"][0], (16,))
        self.assertEqual(calls["reset"][1]["backend"], "chardev")


if __name__ == "__main__":
    unittest.main()
