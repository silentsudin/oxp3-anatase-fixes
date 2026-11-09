#!/usr/bin/python3
"""Create one virtual vendor HID device through /dev/uhid and speak to it.

Why this exists
---------------
The home-button daemon and the RGB tools had only ever been unit-tested
against fakes.  /dev/uhid lets a privileged process create a HID device in
the kernel with the vendor collection's report descriptor; the kernel then
exposes it as a normal ``/dev/hidrawN``.  Writes from our tools arrive here
as ``UHID_OUTPUT`` events, and reports injected here arrive at the readers
as device-to-host input reports.  That is enough to prove hidraw discovery,
the 0xB2 button parsing and the exact LED bytes without vendor hardware.

Safety rules (the harness in test_hid_integration.py relies on these)
---------------------------------------------------------------------
* The identity (vendor/product, name, phys) is deliberately test-specific;
  ``TEST_NAME`` and ``TEST_PHYS`` say "integration test" so nothing on a
  real machine can be confused with it.
* Only this process's own uhid device is created and addressed.  Tools are
  given the node explicitly (``--device``), never through a discovery scan,
  so a real 1a86:fe00 interface is never opened.
* Importing this module performs no I/O; the offline unit tests import it
  to check the uhid wire format and the report builders.

The uhid ABI is taken from the kernel UAPI header ``linux/uhid.h``
(``struct uhid_event``, packed).  Stdlib only.
"""

from __future__ import annotations

import os
import pathlib
import select
import struct
import sys
import time

_HERE = pathlib.Path(__file__).resolve()
REPO = _HERE.parents[3]
TOOLS = REPO / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import oxp3_hid as hid  # noqa: E402

# -- device identity --------------------------------------------------------
#: pid.codes-style test ids are wrong here: the tools identify the vendor
#: collection by 1a86:fe00, so the harness must use the same ids.  The name
#: and phys are what make it unmistakably a test device.
VID = 0x1A86
PID = 0xFE00
BUS_USB = 0x0003
VERSION = 0x0155
COUNTRY = 0
TEST_NAME = "OXP3 uhid integration test (virtual 1a86:fe00)"
TEST_PHYS = "phys-oxp3-uhid-integration-test"
TEST_UNIQ = ""

# -- uhid ABI (linux/uhid.h) ----------------------------------------------
UHID_DESTROY = 1
UHID_OUTPUT = 6
UHID_CREATE2 = 11
UHID_INPUT2 = 12

#: uhid_report_type values used in uhid_output_req.rtype (NOT the kernel's
#: internal HID_*_REPORT enum): FEATURE=0, OUTPUT=1, INPUT=2.
UHID_REPORT_OUTPUT = 1

UHID_DATA_MAX = 4096
_CREATE2_HEAD = 4 + 128 + 64 + 64 + 2 + 2 + 4 + 4 + 4 + 4  # type + payload
#: sizeof(struct uhid_event): the create2 payload is the largest union arm.
UHID_EVENT_SIZE = _CREATE2_HEAD + UHID_DATA_MAX
OUTPUT_DATA_OFFSET = 4
OUTPUT_SIZE_OFFSET = OUTPUT_DATA_OFFSET + UHID_DATA_MAX
OUTPUT_RTYPE_OFFSET = OUTPUT_SIZE_OFFSET + 2

_NAME_OFFSET = 4
_PHYS_OFFSET = _NAME_OFFSET + 128
_UNIQ_OFFSET = _PHYS_OFFSET + 64
_RD_SIZE_OFFSET = _UNIQ_OFFSET + 64
_BUS_OFFSET = _RD_SIZE_OFFSET + 2
_VENDOR_OFFSET = _BUS_OFFSET + 2
_PRODUCT_OFFSET = _VENDOR_OFFSET + 4
_VERSION_OFFSET = _PRODUCT_OFFSET + 4
_COUNTRY_OFFSET = _VERSION_OFFSET + 4
_RD_DATA_OFFSET = _COUNTRY_OFFSET + 4

# kernel struct input_event on an LP64 target: struct timeval + type/code/value
INPUT_EVENT = struct.Struct("@llHHi")
EV_SYN = 0x00
EV_KEY = 0x01
KEY_HOMEPAGE = 172


def build_create2_event(name: str = TEST_NAME, phys: str = TEST_PHYS,
                        uniq: str = TEST_UNIQ,
                        descriptor: bytes | None = None,
                        bus: int = BUS_USB, vendor: int = VID,
                        product: int = PID, version: int = VERSION,
                        country: int = COUNTRY) -> bytes:
    """One UHID_CREATE2 event, padded to sizeof(struct uhid_event).

    Sending the full struct is the portable path: the kernel accepts short
    events too (it zero-extends them), but a full-size write cannot be
    mistaken for a truncated struct.
    """
    rd = hid.vendor_report_descriptor() if descriptor is None else bytes(descriptor)
    if not rd or len(rd) > UHID_DATA_MAX:
        raise ValueError(f"report descriptor length {len(rd)} is out of range")
    buf = bytearray(UHID_EVENT_SIZE)
    struct.pack_into("<I", buf, 0, UHID_CREATE2)
    for offset, text, width in ((_NAME_OFFSET, name, 128),
                                (_PHYS_OFFSET, phys, 64),
                                (_UNIQ_OFFSET, uniq, 64)):
        raw = text.encode("utf-8")
        if len(raw) >= width:
            raise ValueError(f"identity field {text!r} does not fit {width} bytes")
        buf[offset:offset + len(raw)] = raw
    struct.pack_into("<HHIIII", buf, _RD_SIZE_OFFSET, len(rd), bus,
                     vendor, product, version, country)
    buf[_RD_DATA_OFFSET:_RD_DATA_OFFSET + len(rd)] = rd
    return bytes(buf)


def build_input2_event(data: bytes) -> bytes:
    """One UHID_INPUT2 event carrying a device-to-host input report."""
    if len(data) > UHID_DATA_MAX:
        raise ValueError("input report is larger than UHID_DATA_MAX")
    return struct.pack("<IH", UHID_INPUT2, len(data)) + bytes(data)


def build_destroy_event() -> bytes:
    return struct.pack("<I", UHID_DESTROY)


def parse_output_event(buf: bytes):
    """Return ``(report_bytes, rtype)`` for a UHID_OUTPUT event, else None.

    Short kernel events are zero-extended as the UAPI header prescribes, so a
    caller that read fewer bytes than sizeof(struct uhid_event) still gets the
    declared size/rtype instead of a struct error.
    """
    if len(buf) < 4:
        return None
    if struct.unpack_from("<I", buf, 0)[0] != UHID_OUTPUT:
        return None
    if len(buf) <= OUTPUT_RTYPE_OFFSET:
        buf = bytes(buf) + b"\x00" * (OUTPUT_RTYPE_OFFSET + 1 - len(buf))
    size = struct.unpack_from("<H", buf, OUTPUT_SIZE_OFFSET)[0]
    rtype = buf[OUTPUT_RTYPE_OFFSET]
    data = bytes(buf[OUTPUT_DATA_OFFSET:OUTPUT_DATA_OFFSET + size])
    return data, rtype


def button_report(button_id: int = 0x24, pressed: bool = True) -> bytes:
    """The documented device-to-host 0xB2 button report (64 bytes, no ID).

    ``byte 6`` is the button id and ``byte 12`` is 1 (press) / 0 (release);
    the rest is the same 64-byte v1 envelope the tools already validate.
    """
    payload = [0x00, 0x00, 0x00, int(button_id) & 0xFF, 0, 0, 0, 0, 0,
               1 if pressed else 0]
    return hid.v1_frame(hid.CID_BUTTON, payload)


# -- sysfs discovery -------------------------------------------------------
def parse_uevent(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip().upper()] = value.strip()
    return out


def expected_hid_id(vid: int = VID, pid: int = PID) -> str:
    return f"0003:{vid:08X}:{pid:08X}"


def find_hidraw_nodes(name: str = TEST_NAME, phys: str = TEST_PHYS,
                      vid: int = VID, pid: int = PID,
                      sysfs_root: str = "/sys/class/hidraw") -> list:
    """Every hidraw node whose HID identity is exactly this test device."""
    found = []
    try:
        names = sorted(os.listdir(sysfs_root))
    except OSError:
        return found
    for entry in names:
        if not entry.startswith("hidraw"):
            continue
        path = os.path.join(sysfs_root, entry, "device", "uevent")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                props = parse_uevent(fh.read())
        except OSError:
            continue
        if (props.get("HID_ID") == expected_hid_id(vid, pid)
                and props.get("HID_NAME") == name
                and props.get("HID_PHYS") == phys):
            found.append(f"/dev/{entry}")
    return found


def wait_hidraw_node(name: str = TEST_NAME, phys: str = TEST_PHYS,
                     vid: int = VID, pid: int = PID, timeout: float = 10.0,
                     sysfs_root: str = "/sys/class/hidraw") -> str:
    """Wait until exactly one matching hidraw node and its /dev file exist."""
    deadline = time.monotonic() + timeout
    last = "no matching hidraw node"
    while time.monotonic() < deadline:
        nodes = find_hidraw_nodes(name, phys, vid, pid, sysfs_root)
        if len(nodes) > 1:
            last = f"several matching nodes: {nodes}"
        elif len(nodes) == 1:
            node = nodes[0]
            if os.path.exists(node):
                return node
            last = f"{node} exists in sysfs but not in /dev"
        time.sleep(0.05)
    raise TimeoutError(last)


def wait_hidraw_gone(name: str = TEST_NAME, phys: str = TEST_PHYS,
                     vid: int = VID, pid: int = PID, timeout: float = 10.0,
                     sysfs_root: str = "/sys/class/hidraw") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not find_hidraw_nodes(name, phys, vid, pid, sysfs_root):
            return
        time.sleep(0.05)
    raise TimeoutError(f"hidraw node for {name!r}/{phys!r} did not disappear")


# -- the device ------------------------------------------------------------
class UhidDevice:
    """One /dev/uhid device, created and destroyed by this process."""

    def __init__(self, *, name: str = TEST_NAME, phys: str = TEST_PHYS,
                 uniq: str = TEST_UNIQ, descriptor: bytes | None = None,
                 vid: int = VID, pid: int = PID,
                 uhid_path: str = "/dev/uhid", sysfs_root: str = "/sys/class/hidraw",
                 timeout: float = 10.0):
        self.name = name
        self.phys = phys
        self.uniq = uniq
        self.descriptor = descriptor
        self.vid = vid
        self.pid = pid
        self.uhid_path = uhid_path
        self.sysfs_root = sysfs_root
        self.timeout = timeout
        self.fd: int | None = None
        self.node: str | None = None

    # -- lifetime ------------------------------------------------------
    def create(self) -> str:
        if self.fd is not None:
            raise RuntimeError("uhid device already created")
        fd = os.open(self.uhid_path, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(fd, build_create2_event(
                self.name, self.phys, self.uniq, self.descriptor,
                vendor=self.vid, product=self.pid))
            self.fd = fd
            self.node = wait_hidraw_node(self.name, self.phys, self.vid, self.pid,
                                         self.timeout, self.sysfs_root)
        except BaseException:
            try:
                os.close(fd)
            finally:
                self.fd = None
            raise
        return self.node

    def destroy(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.write(fd, build_destroy_event())
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        if self.node is not None:
            wait_hidraw_gone(self.name, self.phys, self.vid, self.pid,
                             self.timeout, self.sysfs_root)
            self.node = None

    close = destroy

    # -- traffic -------------------------------------------------------
    def inject(self, report: bytes) -> None:
        """Send one input report to the kernel (and thus to hidraw readers)."""
        if self.fd is None:
            raise RuntimeError("uhid device is not open")
        os.write(self.fd, build_input2_event(report))

    def _read_once(self, timeout: float) -> bytes | None:
        if self.fd is None:
            return None
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        try:
            return os.read(self.fd, UHID_EVENT_SIZE)
        except BlockingIOError:
            return None

    def drain_outputs(self) -> list:
        """Discard every queued event, returning the UHID_OUTPUT frames seen."""
        frames = []
        if self.fd is None:
            return frames
        while True:
            buf = self._read_once(0.0)
            if not buf:
                return frames
            parsed = parse_output_event(buf)
            if parsed is not None:
                frames.append(parsed)

    def collect_outputs(self, count: int, timeout: float = 10.0) -> list:
        """Collect up to ``count`` (report, rtype) output frames."""
        out = []
        deadline = time.monotonic() + timeout
        while len(out) < count:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            buf = self._read_once(min(0.2, left))
            if not buf:
                continue
            parsed = parse_output_event(buf)
            if parsed is not None:
                out.append(parsed)
        return out


# -- log / stdout helpers (also unit-tested offline) -----------------------
_LISTEN = "listening on "
_RECONNECT = "reconnect #"
_APPLIED = "applied "


def node_paths_in_log(text: str) -> list:
    """Every node path a daemon said it opened, in log order.

    Covers the contract lines from docs/DAEMONS.md: ``listening on
    /dev/hidrawN``, ``reconnect #N to /dev/hidrawN`` and ``applied N
    frame(s) to /dev/hidrawN``.  A harness asserts the set is exactly its
    own virtual node(s), which proves the tools never touched a real one.
    """
    import re

    pattern = re.compile(
        r"(?:listening on |reconnect #\d+ to |applied \d+ frame\(s\) to )(/\S+)")
    return [m.group(1) for m in pattern.finditer(text)]


def parse_report_lines(text: str) -> list:
    """64-byte frames printed by oxp3-led/rgbd ('send <hex>' / 'dry-run <hex>')."""
    frames = []
    for line in text.splitlines():
        line = line.strip()
        for prefix in ("send ", "dry-run "):
            if line.startswith(prefix):
                frames.append(bytes.fromhex(line[len(prefix):]))
                break
    return frames


def parse_input_events(data: bytes) -> list:
    """Decode kernel input_event records into ``(type, code, value)`` tuples."""
    out = []
    step = INPUT_EVENT.size
    for off in range(0, len(data) - step + 1, step):
        _sec, _usec, etype, code, value = INPUT_EVENT.unpack_from(data, off)
        out.append((etype, code, value))
    return out
