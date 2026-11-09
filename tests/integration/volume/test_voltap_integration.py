#!/usr/bin/python3
"""End-to-end integration test for tools/oxp3-voltap against virtual devices.

Why this exists
---------------
tests/unit/test_voltap.py drives the Tapper state machine, discovery and the
grab FSM as *pure objects*.  Nothing in this repository had ever run the real
daemon process against real kernel input events, so the claim "three taps give
exactly three mixer steps, holding does not ramp" was only ever simulated.
This script closes that gap:

  * it creates ephemeral uinput devices (one clearly-named test keyboard, plus
    HHD-shaped and AT-shaped stand-ins for the topology decision),
  * it runs the real daemon as a child process, pointed at those devices,
  * it writes real EV_KEY sequences into them (tap, three taps, 3 s hold with
    value==2 autorepeat, bounce, duplicate storm, orphan release, late
    release), and
  * it reads the *downstream* events from the daemon's own uinput shim
    ("OXP3 volume tap shim", phys phys-oxp3-voltap), never from the device the
    daemon grabbed.

Hermeticity and safety rules
----------------------------
* It never opens, grabs or writes a real input device.  Every node it touches
  is created by this process (uinput) or by the daemon under test (the shim),
  and its identity is checked (name + phys + vendor:product) before use.
* The HHD-shaped stand-in deliberately reuses only the *name string* of hhd's
  virtual keyboard ("Handheld Daemon Volume Keyboard") so the daemon's
  documented *protection* rule can be exercised without hhd and without
  hardware: the hhd-shaped node must never be grabbed by default, only with
  --allow-hhd-grab, and when it is the only rocker path the daemon must enter
  observable degraded/observe mode (exit 4) without opening it.  It has our own
  vendor/product (0x1209:0x7657) and a "-integration-test" phys; the real hhd
  device is never opened.  This is a sandbox test only: nothing shipped uses
  that name except the discovery regex.
* The automatic-topology scenarios pass synthetic --proc-root/--sys-root
  trees, so the host's real devices are invisible to discovery.  The trees list
  the real /dev/input/eventN paths of the ephemeral devices created here.
* Every uinput device is destroyed (and the node disappearance verified) in a
  finally block; the daemon is SIGTERMed, waited for and, if needed, killed.
  Nothing is left running.

Access requirements
-------------------
The test needs to write /dev/uinput and read the event node of the devices it
creates.  With root that is automatic.  As a user it works when /dev/uinput has
a uaccess ACL (Steam's rule does that on this host) and the created devices are
joystick-tagged (the test includes BTN_TRIGGER/BTN_A, so logind grants a
uaccess ACL to the event node too).  When either is missing the test skips with
the exact command to enable it; it never runs sudo itself.

Exit codes: 0 = every scenario passed, 1 = a scenario failed, 77 = skipped
(missing access/dependency).
"""

from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback

REPO = pathlib.Path(__file__).resolve().parents[3]
DAEMON_SRC = REPO / "tools" / "oxp3-voltap" / "oxp3-voltap"

# The virtual "volume keyboard" this test drives.  Vendor 0x1209 is pid.codes
# (test/open hardware); product 0x7654 is deliberately unambiguous.
TEST_NAME = "OXP3 voltap integration test keyboard"
TEST_PHYS = "phys-oxp3-voltaptest"
TEST_VENDOR = 0x1209
TEST_PRODUCT = 0x7654

# Sandbox-only stand-ins for the two real roles.  The names/phys are the
# discovery identifiers the daemon documents; the ids are unmistakably ours.
HHD_NAME = "Handheld Daemon Volume Keyboard"
HHD_PHYS = "hhd-vbtn-integration-test"
HHD_PRODUCT = 0x7657
# The AT stand-in uses a unique *real* uinput name/phys so that no sysfs
# lookup can ever collide with the host's real event4; only the synthetic
# --proc-root entry claims the documented AT identity.
AT_LABEL_NAME = "AT Translated Set 2 keyboard"
AT_LABEL_PHYS = "isa0060/serio0/input0"
AT_REAL_NAME = "OXP3 voltap integration AT stand-in"
AT_REAL_PHYS = "phys-oxp3-voltaptest-at"
AT_PRODUCT = 0x7658

# BTN_TRIGGER/BTN_A make udev's input_id classify the synthetic device as a
# joystick, which is what grants an unprivileged logind session a uaccess ACL
# on the event node.  The daemon only cares about the volume keys; see
# VirtualDevice for the capability set.


class Skip(Exception):
    """Environment cannot run the test (mapped to exit 77 by main())."""


class Failure(Exception):
    """A scenario assertion failed (mapped to exit 1)."""


def _say(line: str) -> None:
    print(line, flush=True)


def scenario(name: str, detail: str) -> None:
    _say(f"PASS {name}: {detail}")


def check(name: str, condition: bool, detail: str) -> None:
    if not condition:
        raise Failure(f"{name}: {detail}")
    scenario(name, detail)


# --------------------------------------------------------------------------
# evdev / daemon imports and preflight
# --------------------------------------------------------------------------
def import_evdev():
    try:
        import evdev  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dependency present on Anatase
        raise Skip(f"python3-evdev is missing ({exc}); install the rpm "
                   f"python3-evdev") from exc
    from evdev import InputDevice, UInput, ecodes

    return InputDevice, UInput, ecodes


def load_daemon():
    loader = importlib.machinery.SourceFileLoader(
        "oxp3_voltap_integration", str(DAEMON_SRC))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def preflight_uinput():
    """Fail with a Skip (not a test failure) when /dev/uinput is not writable."""
    try:
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise Skip(
                "/dev/uinput does not exist (uinput module not loaded?); "
                "load it with: sudo modprobe uinput") from exc
        raise Skip(
            "no write access to /dev/uinput "
            f"({exc.strerror}); run the harness as root: "
            "sudo tests/integration/volume/run.sh  (or grant your user write "
            "access to /dev/uinput with a udev rule/ACL)") from exc
    os.close(fd)


# --------------------------------------------------------------------------
# sysfs helpers: find a device we created by its name + phys, never by number
# --------------------------------------------------------------------------
def sysfs_devdirs():
    for devdir in sorted(pathlib.Path("/sys/class/input").glob("event*/device")):
        try:
            name = (devdir / "name").read_text(encoding="utf-8").strip()
            phys = (devdir / "phys").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        yield devdir, name, phys


def find_node(name: str, phys: str):
    for devdir, dev_name, dev_phys in sysfs_devdirs():
        if dev_name == name and dev_phys == phys:
            return f"/dev/input/{devdir.parent.name}"
    return None


def wait_node(name: str, phys: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node = find_node(name, phys)
        if node is not None:
            return node
        time.sleep(0.05)
    return None


def read_id(path: str):
    base = pathlib.Path("/sys/class/input") / pathlib.Path(path).name / "device"
    vendor = int((base / "id" / "vendor").read_text().strip(), 16)
    product = int((base / "id" / "product").read_text().strip(), 16)
    return vendor, product


def open_readable(path: str, timeout: float = 5.0):
    """Open an event node once the logind uaccess ACL has landed."""
    InputDevice, _UInput, _ecodes = import_evdev()
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return InputDevice(path)
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    try:
        st = os.stat(path)
        import subprocess as _sp
        diag = (f"mode={st.st_mode:o} uid={st.st_uid} gid={st.st_gid} "
                + _sp.run(["udevadm", "info", "-q", "property", "-n", path],
                          capture_output=True, text=True).stdout.replace("\n", "; ")[:400])
    except OSError as exc:
        diag = f"stat failed: {exc}"
    raise Skip(
        f"no read access to the event node {path} created for this test "
        f"({last}; {diag}); run the harness as root: "
        "sudo tests/integration/volume/run.sh  (or join the input group: "
        "sudo usermod -aG input \"$USER\" then log out and in)") from None


# --------------------------------------------------------------------------
# virtual device lifetime
# --------------------------------------------------------------------------
class VirtualDevice:
    """One ephemeral uinput device created and destroyed by this test."""

    def __init__(self, name, phys, product, rep, timeout=5.0,
                 proc_name=None, proc_phys=None):
        _InputDevice, UInput, ecodes = import_evdev()
        self.keys = [ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN,
                     ecodes.KEY_MUTE, ecodes.BTN_TRIGGER, ecodes.BTN_A]
        caps = {ecodes.EV_KEY: self.keys}
        if rep:
            caps[ecodes.EV_REP] = []
        self.name = name
        self.phys = phys
        self.proc_name = proc_name if proc_name is not None else name
        self.proc_phys = proc_phys if proc_phys is not None else phys
        self.rep = rep
        self.path = None
        self.ui = None
        self.closed = False
        try:
            try:
                self.ui = UInput(caps, name=name, phys=phys, version=0x0001,
                                 vendor=TEST_VENDOR, product=product,
                                 bustype=ecodes.BUS_USB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    raise Skip(
                        f"cannot create a uinput device ({exc}); run the "
                        "harness as root: "
                        "sudo tests/integration/volume/run.sh  (or grant your "
                        "user write access to /dev/uinput with a udev "
                        "rule/ACL)") from exc
                raise
            self.path = wait_node(name, phys, timeout)
            if self.path is None:
                raise Failure(
                    f"uinput device {name!r} did not appear under "
                    "/sys/class/input")
            vendor, product_read = read_id(self.path)
            if (vendor, product_read) != (TEST_VENDOR, product):
                raise Failure(
                    f"created device identity is {vendor:04x}:"
                    f"{product_read:04x}, expected {TEST_VENDOR:04x}:"
                    f"{product:04x}")
            self.reader = open_readable(self.path)
            self.reader.close()
            self.vendor = vendor
            self.product = product_read
        except BaseException:
            # Skip/Failure must not leak the uinput device.
            try:
                self.close()
            except Exception:
                pass
            raise

    # -- event injection ---------------------------------------------------
    def write(self, code, value):
        self.ui.write(1, code, value)  # EV_KEY == 1
        self.ui.syn()

    def release(self, code):
        self.write(code, 0)

    def grab(self):
        """Open the node and take EVIOCGRAB (used to provoke EBUSY)."""
        _InputDevice, _UInput, _ecodes = import_evdev()
        dev = _InputDevice(self.path)
        dev.grab()
        return dev

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.ui is not None:
            try:
                self.ui.close()
            except OSError:
                pass
        if self.path is None:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if find_node(self.name, self.phys) is None:
                return
            time.sleep(0.05)
        raise Failure(f"virtual device {self.name!r} was not removed by close()")


# --------------------------------------------------------------------------
# downstream event reader (the daemon's shim)
# --------------------------------------------------------------------------
class ShimReader:
    def __init__(self, path, daemon_module):
        _InputDevice, _UInput, ecodes = import_evdev()
        self.ecodes = ecodes
        dev = open_readable(path)
        if dev.name != daemon_module.SHIM_NAME or dev.phys != daemon_module.SHIM_PHYS:
            dev.close()
            raise Failure(
                f"downstream node {path} is {dev.name!r}/{dev.phys!r}, not the "
                "daemon shim")
        if (dev.info.vendor, dev.info.product) != (
                daemon_module.SHIM_VID, daemon_module.SHIM_PID):
            dev.close()
            raise Failure(
                f"downstream shim id is {dev.info.vendor:04x}:"
                f"{dev.info.product:04x}, expected "
                f"{daemon_module.SHIM_VID:04x}:{daemon_module.SHIM_PID:04x}")
        self.path = path
        self.dev = dev
        os.set_blocking(dev.fd, False)

    def drain(self):
        """Return every queued EV_KEY as (code, value); never blocks."""
        out = []
        while True:
            try:
                events = list(self.dev.read())
            except (BlockingIOError, OSError):
                break
            if not events:
                break
            for ev in events:
                if ev.type == self.ecodes.EV_KEY:
                    out.append((ev.code, ev.value))
        return out

    def close(self):
        try:
            self.dev.close()
        except OSError:
            pass


def find_shim(daemon_module, timeout=8.0):
    """Locate + open the daemon's shim by its documented name and phys."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        node = find_node(daemon_module.SHIM_NAME, daemon_module.SHIM_PHYS)
        if node is not None:
            try:
                return ShimReader(node, daemon_module)
            except Skip as exc:
                # The shim is created by the daemon under test, and the test
                # device was already proven readable; a shim that stays
                # unreadable is a failure, not an environment skip.
                last = exc
            except OSError as exc:
                last = exc
        time.sleep(0.05)
    raise Failure(f"daemon shim {daemon_module.SHIM_NAME!r} never became "
                  f"readable ({last})")


# --------------------------------------------------------------------------
# daemon child process
# --------------------------------------------------------------------------
class Daemon:
    def __init__(self, args, log_path):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("VOLTAP_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.log_path = pathlib.Path(log_path)
        self.log = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(DAEMON_SRC)] + list(args),
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            env=env, close_fds=True)

    def text(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def wait_for(self, pattern, timeout=10.0, what=""):
        rx = re.compile(pattern, re.M)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = rx.search(self.text())
            if match:
                return match
            if self.proc.poll() is not None:
                time.sleep(0.05)
                match = rx.search(self.text())
                if match:
                    return match
                break
            time.sleep(0.05)
        raise Failure(f"daemon did not log {what or pattern!r} within "
                      f"{timeout:g}s; log:\n{self.text()}")

    def grabbed_paths(self):
        return re.findall(r"grabbed (\S+) \(", self.text())

    def stop(self, timeout=8.0):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3.0)
        self.log.close()
        return self.proc.returncode


# --------------------------------------------------------------------------
# synthetic /proc/bus/input/devices (hermetic automatic topology)
# --------------------------------------------------------------------------
def _real_caps(device):
    """The exact EV/KEY bitmaps of the ephemeral device, straight from sysfs.

    The daemon copies the source's KEY bitmap into its shim, and udev's
    joystick tagging (hence the unprivileged uaccess ACL on the shim node)
    depends on non-volume keys such as BTN_TRIGGER.  A synthetic proc entry
    must therefore advertise the real capability set, not a hand-written one.
    """
    event = pathlib.Path(device.path).name
    base = pathlib.Path("/sys/class/input") / event / "device" / "capabilities"
    ev = (base / "ev").read_text(encoding="utf-8").strip()
    key = (base / "key").read_text(encoding="utf-8").strip()
    return ev, key


def proc_entry(device, rep):
    event = pathlib.Path(device.path).name
    ev, key = _real_caps(device)
    return (f"I: Bus=0003 Vendor={device.vendor:04x} "
            f"Product={device.product:04x} Version=0001\n"
            f'N: Name="{device.proc_name}"\n'
            f"P: Phys={device.proc_phys}\n"
            f"H: Handlers=kbd {event} \n"
            f"B: EV={ev}\n"
            f"B: KEY={key}\n\n")


def write_proc(root: pathlib.Path, devices):
    target = root / "bus" / "input" / "devices"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text("".join(proc_entry(dev, dev.rep) for dev in devices),
                   encoding="utf-8")
    os.replace(tmp, target)
    return target


def add_fake_holder(proc_root: pathlib.Path, dev, grab_fd):
    """Mirror what /proc/<pid>/fdinfo says while this test holds EVIOCGRAB."""
    pid_dir = proc_root / str(os.getpid())
    (pid_dir / "fdinfo").mkdir(parents=True, exist_ok=True)
    (pid_dir / "fd").mkdir(parents=True, exist_ok=True)
    (pid_dir / "fdinfo" / str(grab_fd)).write_text(
        "pos: 0\nflags: 02\ngrab: 1\n", encoding="utf-8")
    link = pid_dir / "fd" / str(grab_fd)
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(dev.path)


def wait_status(path: pathlib.Path, timeout: float = 10.0):
    """Read the daemon's JSON status once it has been written for this scan."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                last = exc
        time.sleep(0.05)
    raise Failure(f"daemon did not write a readable status to {path} "
                  f"within {timeout:g}s ({last})")


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------
UP = 115           # KEY_VOLUMEUP
DOWN = 114         # KEY_VOLUMEDOWN


def tap(device, code, hold=0.010, settle=0.10):
    device.write(code, 1)
    time.sleep(hold)
    device.write(code, 0)
    time.sleep(settle)


def expect(name, actual, wanted):
    if actual != wanted:
        raise Failure(f"{name}: downstream events {actual} != expected {wanted}")
    scenario(name, f"downstream exactly {wanted}")


def run_tap_scenarios(device, shim):
    """Drive the real daemon through five tap/hold event sequences."""
    shim.drain()

    # 1. one tap -> one down/up pair
    tap(device, UP)
    expect("single tap", shim.drain(), [(UP, 1), (UP, 0)])

    # 2. three quick taps -> exactly three pairs, not more
    time.sleep(0.10)
    shim.drain()
    for _ in range(3):
        tap(device, UP, hold=0.010, settle=0.12)
    expect("three quick taps", shim.drain(),
           [(UP, 1), (UP, 0)] * 3)

    # 3. 3 s hold with kernel-style value==2 auto-repeat -> no ramp
    time.sleep(0.15)
    shim.drain()
    device.write(UP, 1)
    time.sleep(0.50)
    early = shim.drain()
    deadline = time.monotonic() + 2.50
    sent = 0
    while time.monotonic() < deadline:
        device.write(UP, 2)
        sent += 1
        time.sleep(0.033)
    middle = shim.drain()
    device.write(UP, 0)
    time.sleep(0.20)
    late = shim.drain()
    if early != [(UP, 1), (UP, 0)]:
        raise Failure(f"3 s hold: first 0.5 s emitted {early}, expected the "
                      "single synthetic pair")
    if middle or late:
        raise Failure(f"3 s hold ramped: {len(middle) + len(late)} downstream "
                      f"events after the press (middle={middle}, late={late})")
    scenario("3 s hold with auto-repeat",
             f"{sent} value==2 repeats sent, downstream stayed at exactly 2 "
             "events (no ramp)")

    # 4. rapid bounce -> the second, in-window press and its release vanish
    time.sleep(0.15)
    shim.drain()
    tap(device, UP, hold=0.010, settle=0.020)
    device.write(UP, 1)
    time.sleep(0.010)
    device.write(UP, 0)
    time.sleep(0.20)
    expect("rapid bounce", shim.drain(), [(UP, 1), (UP, 0)])
    time.sleep(0.15)                      # outside the 60 ms gap again
    tap(device, UP, hold=0.010, settle=0.10)
    expect("tap after bounce recovery", shim.drain(), [(UP, 1), (UP, 0)])

    # 5. duplicate + repeat + repeated-release storm -> one pair
    time.sleep(0.15)
    shim.drain()
    device.write(DOWN, 1)
    for _ in range(8):
        device.write(DOWN, 1)             # EC duplicate presses
    for _ in range(10):
        device.write(DOWN, 2)             # autorepeat
    device.write(DOWN, 0)
    for _ in range(8):
        device.write(DOWN, 0)             # repeated / late releases
    time.sleep(0.25)
    expect("duplicate/repeat storm", shim.drain(), [(DOWN, 1), (DOWN, 0)])


def run_orphan_scenarios(tmp: pathlib.Path, source, daemon_module):
    """Exercise the orphan-release and late-release branches end to end.

    Linux's input core drops a release for a key that is not logically down on
    that device, so a bare release cannot be injected into a uinput device (the
    unit suite can, because it calls Tapper.handle() directly).  The shape the
    real daemon can see is a key that went down while nobody was reading - the
    hhd/voltap restart window: press first with no daemon attached, start the
    daemon, then release.  It reads only the release and must turn it into one
    tap.  A release arriving after the synthetic timeout must not add a second.
    """
    source.write(UP, 0)                   # start from a known-up kernel state
    source.write(DOWN, 0)
    time.sleep(0.05)
    source.write(UP, 1)                   # down, with nobody reading the device
    time.sleep(0.05)
    status = tmp / "status-orphan.json"
    daemon = Daemon([
        "run", "--device", source.path,
        "--status-file", str(status),
        "--rescan", "0.5", "--hold", "0.05", "--gap", "0.060",
        "--max-hold", "0.500",
    ], tmp / "daemon-orphan.log")
    shim = None
    try:
        daemon.wait_for(rf"grabbed {re.escape(source.path)} \(", 10.0,
                        f"a grab of {source.path}")
        shim = find_shim(daemon_module)
        shim.drain()
        source.write(UP, 0)               # the only event the daemon ever sees
        time.sleep(0.20)
        expect("orphan release becomes one tap", shim.drain(),
               [(UP, 1), (UP, 0)])

        # A release that arrives after our synthetic release is the tail of
        # that same press: it must not become a second tap.
        time.sleep(0.15)
        shim.drain()
        source.write(UP, 1)
        time.sleep(0.30)                  # synthetic release after 50 ms
        synthetic = shim.drain()
        source.write(UP, 0)               # late real release
        time.sleep(0.15)
        late = shim.drain()
        if synthetic != [(UP, 1), (UP, 0)]:
            raise Failure(f"late-release press emitted {synthetic}, expected "
                          "one synthetic pair")
        if late:
            raise Failure(f"late release after the timeout produced {late}; "
                          "it must be dropped, not counted as a second tap")
        scenario("late release after timeout", "dropped, no second tap")

        rc = daemon.stop()
        check("orphan daemon exits cleanly", rc == 0, f"exit {rc}")
        stray = [path for path in daemon.grabbed_paths()
                 if path != source.path]
        check("orphan run never grabbed a real device", not stray,
              f"only {source.path} was grabbed")
        data = wait_status(status)
        counters = data["counters"]
        check("orphan counters prove both branches",
              counters["orphan_tap"] >= 1 and counters["late_release"] >= 1,
              f"orphan_tap={counters['orphan_tap']} "
              f"late_release={counters['late_release']}")
        return data
    finally:
        if shim is not None:
            shim.close()
        try:
            daemon.stop()
        except Exception:
            pass


def group_a(tmp: pathlib.Path, daemon_module):
    """Explicit-device run: the real daemon against our virtual keyboard."""
    _InputDevice, _UInput, ecodes = import_evdev()
    source = VirtualDevice(TEST_NAME, TEST_PHYS, TEST_PRODUCT, rep=True)
    daemons = []
    shim = None
    try:
        status = tmp / "status-explicit.json"
        daemon = Daemon([
            "run", "--device", source.path,
            "--status-file", str(status),
            "--rescan", "0.5", "--hold", "0.05", "--gap", "0.060",
            "--max-hold", "0.500",
        ], tmp / "daemon-explicit.log")
        daemons.append(daemon)
        daemon.wait_for(
            rf"grabbed {re.escape(source.path)} \(", 10.0,
            f"a grab of {source.path}")
        line = daemon.wait_for(r"grabbed .*\n", 3.0, "the grab decision line").group(0)
        if f"id={source.vendor:04x}:{source.product:04x}" not in line:
            raise Failure(f"daemon grabbed a different identity: {line!r}")
        if "role=" not in line:
            raise Failure(f"grab line has no role: {line!r}")
        scenario("explicit device chosen",
                 f"grabbed {source.path} ({TEST_NAME!r}) id="
                 f"{source.vendor:04x}:{source.product:04x}")

        shim = find_shim(daemon_module)
        scenario("downstream shim identified",
                 f"{shim.path} is {daemon_module.SHIM_NAME!r} "
                 f"{daemon_module.SHIM_PHYS!r}")

        run_tap_scenarios(source, shim)

        rc = daemon.stop()
        check("daemon exits cleanly on SIGTERM", rc == 0,
              f"exit {rc}")
        log = daemon.text()
        check("daemon logged exit stats", "exit stats" in log,
              "counter line present in the daemon log")
        stray = [path for path in daemon.grabbed_paths()
                 if path != source.path]
        check("never grabbed a real device", not stray,
              f"every grab line points at the test device ({source.path})")
        check("no degraded state", "DEGRADED" not in log,
              "no DEGRADED line in the explicit-device run")

        data = wait_status(status)
        counters = data["counters"]
        check("status: state stopped", data["state"] == "stopped",
              f"state={data['state']}, exit_code={data['exit_code']}")
        selected = data.get("selected") or {}
        check("status: device identity",
              selected.get("path") == source.path
              and selected.get("name") == TEST_NAME
              and selected.get("phys") == TEST_PHYS,
              f"selected={selected.get('path')} {selected.get('name')!r}")
        # The input core coalesces a second press for a key already down and
        # a release for a key already up, so the duplicate-press and
        # repeated-release rules can only be exercised by the unit suite; the
        # end-to-end storm proves they are irrelevant (one pair downstream).
        check("state machine counters prove the kernel-visible mechanisms",
              counters["repeat"] >= 20 and counters["bounce"] >= 2
              and counters["forwarded"] == 0,
              f"repeat={counters['repeat']} bounce={counters['bounce']} "
              f"duplicate={counters['duplicate']} (kernel-coalesced) "
              f"forwarded={counters['forwarded']}")

        # Restart-shaped phase: the key goes down with no reader attached, so
        # the daemon reads only the release (orphan tap) and later sees the
        # tail of a press it already released (late release).
        run_orphan_scenarios(tmp, source, daemon_module)
    finally:
        if shim is not None:
            shim.close()
        for daemon in daemons:
            try:
                daemon.stop()
            except Exception:
                pass
        source.close()


def group_b(tmp: pathlib.Path, daemon_module):
    """Automatic topology: never grab hhd's device; AT only when free; observe."""
    hhd = VirtualDevice(HHD_NAME, HHD_PHYS, HHD_PRODUCT, rep=False)
    at = None
    daemons = []
    shim = None
    holder = None
    grabbed_hhd = []
    try:
        at = VirtualDevice(AT_REAL_NAME, AT_REAL_PHYS, AT_PRODUCT, rep=True,
                           proc_name=AT_LABEL_NAME, proc_phys=AT_LABEL_PHYS)
        proc_root = tmp / "proc-auto"
        sys_root = tmp / "sys-auto"
        sys_root.mkdir()
        write_proc(proc_root, [hhd, at])

        # -- phase 1: hhd device present, physical keyboard free ------------
        # The only device the daemon may take is the free AT keyboard. hhd's
        # own volume keyboard must be refused (that grab caused the outage).
        status = tmp / "status-auto.json"
        daemon = Daemon([
            "run", "--proc-root", str(proc_root), "--sys-root", str(sys_root),
            "--status-file", str(status), "--rescan", "0.3",
            "--hold", "0.05", "--gap", "0.060",
        ], tmp / "daemon-auto.log")
        daemons.append(daemon)
        line = daemon.wait_for(
            rf"grabbed {re.escape(at.path)} \([^\n]*", 12.0,
            "a grab of the AT-shaped fallback with role=at-kbd").group(0)
        check("physical device chosen when it is free", "role=at-kbd" in line,
              line)
        scenario("hhd device present, physical free",
                 f"grabbed {at.path} role=at-kbd; hhd's device was refused")
        check("hhd's own device is not grabbed by default",
              hhd.path not in daemon.grabbed_paths(),
              f"grab lines: {daemon.grabbed_paths()}")

        # The physical path must still carry events downstream.
        shim = find_shim(daemon_module)
        shim.drain()
        tap(at, UP)
        expect("physical path forwards taps", shim.drain(),
               [(UP, 1), (UP, 0)])

        data = wait_status(status)
        check("status: physical topology, hhd protected",
              data["topology"] == "physical"
              and data["selected"]["path"] == at.path
              and data["hhd_protected"] is True
              and data["allow_hhd_grab"] is False
              and data["observe"] is False,
              f"topology={data['topology']} hhd_protected="
              f"{data['hhd_protected']} observe={data['observe']}")
        rc = daemon.stop()
        check("auto-topology daemon exits cleanly", rc == 0, f"exit {rc}")
        shim.close()
        shim = None

        # -- phase 2: the outage topology, hhd holds the AT keyboard --------
        # Simulate a running hhd: it owns event4, so the only rocker path is
        # hhd's virtual keyboard. Default must observe, never open or grab it.
        holder = at.grab()
        add_fake_holder(proc_root, at, holder.fd)
        observe_status = tmp / "status-observe.json"
        observe = Daemon([
            "run", "--once",
            "--proc-root", str(proc_root), "--sys-root", str(sys_root),
            "--status-file", str(observe_status),
        ], tmp / "daemon-observe.log")
        daemons.append(observe)
        try:
            rc = observe.proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            observe.proc.kill()
            observe.proc.wait()
            rc = observe.proc.returncode
        log = observe.text()
        check("hhd-protected run exits with the documented code 4", rc == 4,
              f"--once exit {rc} (expected 4)")
        check("hhd-protected run logs a clear OBSERVE line",
              "OBSERVE: hhd owns the volume path" in log,
              "the documented OBSERVE line is present")
        check("hhd-protected run grabs nothing", observe.grabbed_paths() == [],
              f"grab lines: {observe.grabbed_paths()}")
        data = wait_status(observe_status)
        check("status: degraded-hhd-protected exit 4",
              data["state"] == "degraded-hhd-protected"
              and data["exit_code"] == 4
              and data["selected"] is None
              and data["observe"] is True
              and data["hhd_protected"] is True,
              f"state={data['state']} exit_code={data['exit_code']} "
              f"observe={data.get('observe')}")
        check("status: reason names the plugin path",
              "hhd plugin" in (data.get("reason") or "")
              and "--allow-hhd-grab" in (data.get("reason") or ""),
              data.get("reason"))
        scenario("outage topology (hhd holds the AT keyboard)",
                 "observe mode, exit 4, nothing opened or grabbed")

        # The daemon keeps retrying the observe state but logs it once, not
        # once per rescan.
        live = Daemon([
            "run", "--proc-root", str(proc_root),
            "--sys-root", str(sys_root), "--rescan", "0.25",
            "--status-file", str(tmp / "status-observe-live.json"),
        ], tmp / "daemon-observe-live.log")
        daemons.append(live)
        live.wait_for(r"OBSERVE: hhd owns the volume path", 8.0,
                      "the OBSERVE line from the retrying daemon")
        # Nothing of ours may hold the device hhd needs: inspect the live
        # daemon's own fds (same user as this test, so /proc is readable).
        held_fds = []
        try:
            for entry in pathlib.Path(f"/proc/{live.proc.pid}/fd").iterdir():
                try:
                    target = os.readlink(entry)
                except OSError:
                    continue
                if target == hhd.path or target.endswith(
                        os.path.basename(hhd.path) + " (deleted)"):
                    held_fds.append(target)
        except OSError as exc:
            raise Failure(f"cannot inspect the observe daemon's fds: {exc}")
        check("observe daemon holds no fd on hhd's device",
              not held_fds,
              f"fds on hhd's device: {held_fds}")
        time.sleep(1.1)               # several rescans
        count = live.text().count("OBSERVE:")
        check("observe state is logged once, not per rescan", count == 1,
              f"{count} OBSERVE line(s) across several rescans")
        rc_live = live.stop()
        check("retrying observe daemon stops cleanly", rc_live == 0,
              f"exit {rc_live}")

        # -- phase 3: --allow-hhd-grab is a deliberate last resort ----------
        allowed_status = tmp / "status-allowed.json"
        allowed = Daemon([
            "run", "--proc-root", str(proc_root), "--sys-root", str(sys_root),
            "--allow-hhd-grab", "--status-file", str(allowed_status),
            "--rescan", "0.25",
        ], tmp / "daemon-allowed.log")
        daemons.append(allowed)
        allowed.wait_for(rf"grabbed {re.escape(hhd.path)} \(", 12.0,
                         "a grab of the hhd device with --allow-hhd-grab")
        grabbed_hhd = allowed.grabbed_paths()
        check("--allow-hhd-grab makes hhd's device the last resort",
              grabbed_hhd == [hhd.path],
              f"grab lines: {grabbed_hhd} (the AT keyboard is held by the test)")
        data = wait_status(allowed_status)
        check("status: explicit hhd last-resort topology",
              data["topology"] == "hhd-virtual"
              and data["selected"]["decision"] == "last-resort"
              and data["allow_hhd_grab"] is True,
              f"topology={data['topology']} decision="
              f"{(data.get('selected') or {}).get('decision')}")
        scenario("explicit --allow-hhd-grab",
                 f"grabbed {hhd.path} only because the flag was given")
        allowed.stop()

        # -- phase 4: a free-device EBUSY conflict is still exit 3 ----------
        # No hhd device here, so the protection rule cannot apply: a held AT
        # keyboard is the documented degraded-busy state.
        write_proc(proc_root, [at])
        busy_status = tmp / "status-busy.json"
        busy = Daemon([
            "run", "--once",
            "--proc-root", str(proc_root), "--sys-root", str(sys_root),
            "--status-file", str(busy_status),
        ], tmp / "daemon-busy.log")
        daemons.append(busy)
        try:
            rc = busy.proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            busy.proc.kill()
            busy.proc.wait()
            rc = busy.proc.returncode
        log = busy.text()
        check("grab refused exits with the documented code 3", rc == 3,
              f"--once exit {rc} (expected 3)")
        check("grab refused logs DEGRADED busy",
              "DEGRADED: every candidate is held or refused (EBUSY)" in log,
              "the documented DEGRADED busy line is present")
        data = wait_status(busy_status)
        check("grab refused status is degraded-busy exit 3",
              data["state"] == "degraded-busy"
              and data["exit_code"] == 3 and data["selected"] is None,
              f"state={data['state']} exit_code={data['exit_code']}")
        check("grab refused records the holder",
              bool(data["refused"])
              and data["refused"][0]["kind"] == "busy"
              and str(os.getpid()) in data["refused"][0]["detail"],
              f"refused={data['refused']}")
        scenario("held physical device without hhd",
                 "degraded-busy exit 3, holder recorded")

        # Nothing may ever have grabbed hhd's device except the explicit flag.
        every = []
        for item in daemons:
            every.extend(item.grabbed_paths())
        strays = sorted({path for path in every
                         if path not in (at.path, hhd.path)})
        check("auto topology never grabbed a real device", not strays,
              f"every grab line points at a sandbox device ({strays})")
        check("hhd's device only appears in the explicit-flag run",
              every.count(hhd.path) == 1 and grabbed_hhd == [hhd.path],
              f"grabs: {sorted(set(every))}")
    finally:
        if holder is not None:
            try:
                holder.ungrab()
            except OSError:
                pass
            holder.close()
            holder = None
        if shim is not None:
            shim.close()
        for daemon in daemons:
            try:
                daemon.stop()
            except Exception:
                pass
        if at is not None:
            at.close()
        if not hhd.closed:
            hhd.close()


# --------------------------------------------------------------------------
# cleanup verification
# --------------------------------------------------------------------------
def assert_no_leftovers():
    ours = {(TEST_NAME, TEST_PHYS), (HHD_NAME, HHD_PHYS),
            (AT_REAL_NAME, AT_REAL_PHYS)}
    found = [(name, phys) for _devdir, name, phys in sysfs_devdirs()
             if (name, phys) in ours]
    if found:
        raise Failure(f"virtual test devices left behind: {found}")
    shim = find_node("OXP3 volume tap shim", "phys-oxp3-voltap")
    if shim is not None:
        raise Failure(f"daemon shim left behind at {shim}")
    scenario("cleanup", "no test device and no daemon shim left in /sys/class/input")


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="end-to-end integration test for oxp3-voltap")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="whole-run watchdog in seconds (default 120)")
    args = parser.parse_args(argv)

    def _alarm(_signum, _frame):
        raise Failure(f"integration test exceeded its {args.timeout:g}s watchdog")

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(max(1, args.timeout)))

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="oxp3-voltap-integration-"))
    _say("# integration: oxp3-voltap against a virtual input device")
    _say(f"# repo: {REPO}")
    try:
        import_evdev()
        daemon_module = load_daemon()
        preflight_uinput()
        group_a(tmp, daemon_module)
        group_b(tmp, daemon_module)
        assert_no_leftovers()
        _say("== integration: all scenarios passed")
        return 0
    except Skip as exc:
        _say(f"SKIP: {exc}")
        return 77
    except Failure as exc:
        _say(f"FAIL: {exc}")
        return 1
    except Exception:  # noqa: BLE001 - report and fail, never mask
        _say("FAIL: unexpected exception\n" + traceback.format_exc())
        return 1
    finally:
        signal.alarm(0)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
