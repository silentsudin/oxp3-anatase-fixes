#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""End-to-end integration test for the OXP3 home button and LED tools.

Why this exists
---------------
``tests/unit/test_hid.py`` checks the frame encoders against captured bytes and
``tests/unit/test_homebtn_lifecycle.py`` drives the daemon with fake fds, but
nothing had ever run ``tools/oxp3-homebtn.py`` or ``tools/oxp3-led.py`` against
a device.  This harness closes that gap without owning the hardware:

  * it creates one virtual HID device through ``/dev/uhid`` with the *documented*
    vendor report descriptor (usage page 0xFF00, no report id, 64-byte input and
    output reports) on the same USB identity the tools look for (1a86:fe00),
  * the kernel exposes it as a normal ``/dev/hidrawN``; writes from our tools
    arrive here as ``UHID_OUTPUT`` events and injected reports arrive at the
    readers as device-to-host input reports,
  * therefore the real tools can be driven end to end: discovery-by-identity,
    the 0xB2 home-button parsing, KEY_HOMEPAGE on the uinput keyboard they
    create, and the exact LED bytes that go on the wire,
  * and the daemon's reconnect path can be exercised for real by destroying and
    recreating the uhid device.

Safety and hermeticity
----------------------
* Every device touched is created by this process.  The virtual identity is
  unmistakably a test device: name ``"OXP3 uhid integration test (virtual
  1a86:fe00)"``, phys ``phys-oxp3-uhid-integration-test``.
* The tools are pinned with their ``--device`` option, so they never fall back
  to a discovery scan.  On a real OXP3 the actual vendor interface is present
  while this runs, and a scan could pick it.  The harness additionally asserts
  every node path in the tools' logs and every hidraw fd of the daemon under
  test is one of its own virtual nodes.
* ``trap``-equivalent cleanup in ``finally``: the uhid device is destroyed, the
  daemons are SIGTERMed (then killed), readers are closed, and the absence of
  leftovers is verified.  ``--keep`` is the explicit debug exception: it leaves
  the virtual device alive (a small holder process keeps the uhid fd open) and
  the daemons running, and prints the exact cleanup commands.
* A whole-run SIGALRM watchdog (``--timeout``, default 120 s) bounds the run.

Opt-in and access
-----------------
Needs write access to ``/dev/uhid`` and ``/dev/uinput`` (root by default:
``crw------- root root``) and read access to the event node of the uinput
keyboard the daemon creates.  When either is missing it exits **77** with the
exact ``sudo`` command.  It never runs sudo, never writes to a real hidraw node
and never touches the host's persistent state.

Exit codes: 0 = all scenarios passed, 1 = a scenario failed, 77 = skipped
(missing access/dependency).
"""

from __future__ import annotations

import argparse
import errno
import os
import pathlib
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import uhid_harness as uh  # noqa: E402

REPO = HERE.parents[2]
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))
import oxp3_hid as hid  # noqa: E402
import oxp3_lifecycle as life  # noqa: E402

HOME_BUTTON_KEY = "OXP3 Home Button (integration test)"  # unmistakable
HOME_BUTTON = 0x24                        # physical home button id
HID_SYSFS = pathlib.Path("/sys/class/hidraw")
INPUT_SYSFS = pathlib.Path("/sys/class/input")


class Skip(Exception):
    """Environment cannot run the harness (mapped to exit 77 by main())."""


class Failure(Exception):
    """A scenario assertion failed (mapped to exit 1 by main())."""


class Interrupted(Exception):
    """SIGTERM/SIGINT during the run."""


def say(line: str) -> None:
    print(line, flush=True)


def scenario(name: str, detail: str) -> None:
    say(f"PASS {name}: {detail}")


def check(name: str, condition: bool, detail: str) -> None:
    if not condition:
        raise Failure(f"{name}: {detail}")
    scenario(name, detail)


# --------------------------------------------------------------------------
# environment checks
# --------------------------------------------------------------------------
def _probe_write(path: str, flags: int, missing_hint: str = "") -> None:
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise Skip(f"{path} does not exist ({exc.strerror}); {missing_hint}") from exc
        raise Skip(
            f"no access to {path} ({exc.strerror}); run the harness as root: "
            "sudo tests/integration/hid/run.sh") from exc
    os.close(fd)


def preflight() -> None:
    for tool in ("oxp3_hid.py", "oxp3-led.py", "oxp3-rgbd.py", "oxp3-homebtn.py"):
        if not (TOOLS / tool).is_file():
            raise Skip(f"tools/{tool} is missing")
    _probe_write("/dev/uhid", os.O_RDWR | os.O_NONBLOCK,
                 "load the module with: sudo modprobe uhid")
    _probe_write("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK,
                 "load the uinput module with: sudo modprobe uinput")


def input_event_info() -> dict:
    """Snapshot /sys/class/input: event name -> identity fields."""
    out = {}
    try:
        devdirs = list(INPUT_SYSFS.glob("event*/device"))
    except OSError:
        return out
    for devdir in devdirs:
        event = devdir.parent.name
        try:
            name = (devdir / "name").read_text(encoding="utf-8").strip()
            phys = (devdir / "phys").read_text(encoding="utf-8").strip()
            vendor = int((devdir / "id" / "vendor").read_text().strip(), 16)
            product = int((devdir / "id" / "product").read_text().strip(), 16)
            bustype = int((devdir / "id" / "bustype").read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        out[event] = {"path": f"/dev/input/{event}", "name": name, "phys": phys,
                      "vendor": vendor, "product": product, "bustype": bustype,
                      "devdir": devdir}
    return out


def key_capable(devdir: pathlib.Path, code: int) -> bool:
    """True when the input device advertises KEY ``code`` in its key bitmap."""
    try:
        text = (devdir / "capabilities" / "key").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    words = text.split()
    index, bit = divmod(code, 64)
    if index >= len(words):
        return False
    try:
        return bool((int(words[index], 16) >> bit) & 1)
    except ValueError:
        return False


def wait_new_uinput(before: dict, timeout: float = 10.0) -> dict:
    """The uinput event node the daemon under test has just created."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event, info in input_event_info().items():
            if event in before:
                continue
            if info["name"] == HOME_BUTTON_KEY:
                return info
        time.sleep(0.05)
    raise Failure(f"the daemon did not create a uinput device named "
                  f"{HOME_BUTTON_KEY!r} within {timeout:g}s")


def event_node_gone(event: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (INPUT_SYSFS / event).exists():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------
# event reading (uinput keyboard created by the daemon)
# --------------------------------------------------------------------------
class EventReader:
    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def read_key_events(self, want: int, timeout: float = 5.0) -> list:
        """Read exactly-typed EV_KEY (code, value) pairs, ignoring SYN/MSC."""
        out = []
        deadline = time.monotonic() + timeout
        while len(out) < want and time.monotonic() < deadline:
            left = deadline - time.monotonic()
            ready, _, _ = select.select([self.fd], [], [], max(0.0, min(0.2, left)))
            if not ready:
                continue
            try:
                data = os.read(self.fd, uh.INPUT_EVENT.size * 64)
            except BlockingIOError:
                continue
            except OSError:
                return out
            for etype, code, value in uh.parse_input_events(data):
                if etype == uh.EV_KEY:
                    out.append((code, value))
        return out

    def drain(self) -> None:
        """Consume whatever is queued without blocking."""
        while True:
            try:
                data = os.read(self.fd, uh.INPUT_EVENT.size * 64)
            except (BlockingIOError, OSError):
                return
            if not data:
                return

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


# --------------------------------------------------------------------------
# child processes
# --------------------------------------------------------------------------
class Daemon:
    """A long-running tool (oxp3-homebtn.py) with captured output."""

    def __init__(self, args, log_path):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("OXP3_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.log_path = pathlib.Path(log_path)
        self.log = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, *[str(a) for a in args]],
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            env=env, close_fds=True)

    def text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def wait_for(self, pattern: str, timeout: float = 10.0, what: str = ""):
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

    def stop(self, timeout: float = 8.0) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3.0)
        try:
            self.log.close()
        except OSError:
            pass
        return self.proc.returncode


def run_tool(args, timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("OXP3_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.run([sys.executable, *[str(a) for a in args]],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, env=env,
                              close_fds=True)
    except subprocess.TimeoutExpired as exc:
        raise Failure(f"{' '.join(str(a) for a in args)} timed out after "
                      f"{timeout:g}s") from exc


def hidraw_fd_targets(pid: int) -> list:
    """Every hidraw node the process has open (read fds the tools use)."""
    out = []
    fddir = f"/proc/{pid}/fd"
    try:
        names = os.listdir(fddir)
    except OSError:
        return out
    for name in names:
        try:
            target = os.readlink(os.path.join(fddir, name))
        except OSError:
            continue
        base = os.path.basename(target).split(" (deleted)", 1)[0]
        if base.startswith("hidraw"):
            out.append("/dev/" + base)
    return out


# --------------------------------------------------------------------------
# harness state
# --------------------------------------------------------------------------
class Harness:
    def __init__(self, tmp: pathlib.Path, keep: bool = False):
        self.tmp = tmp
        self.keep = keep
        self.uhid: uh.UhidDevice | None = None
        self.daemons: list[Daemon] = []
        self.readers: list[EventReader] = []
        self.before_input: dict = {}
        self.created_input: list[str] = []
        self.nodes: set[str] = set()
        self.kept = False

    # -- cleanup -------------------------------------------------------
    def cleanup(self) -> None:
        for reader in self.readers:
            reader.close()
        self.readers.clear()
        for daemon in self.daemons:
            try:
                daemon.stop()
            except Exception:  # noqa: BLE001 - cleanup must not mask the verdict
                pass
        self.daemons.clear()
        if self.uhid is not None:
            try:
                self.uhid.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.uhid = None

    def keep_for_debugging(self) -> None:
        """Leave the virtual device and daemons alive for inspection."""
        if self.kept:
            return
        self.kept = True
        holder = None
        if self.uhid is not None and self.uhid.fd is not None:
            pid = os.fork()
            if pid == 0:  # pragma: no cover - child never returns
                try:
                    while True:
                        time.sleep(3600)
                finally:
                    os._exit(0)
            holder = pid
        pids = [d.proc.pid for d in self.daemons if d.proc.poll() is None]
        say("KEEP: debug state left running")
        if holder:
            say(f"KEEP: uhid device {self.uhid.node} kept alive by pid {holder}")
        if pids:
            say("KEEP: daemons " + " ".join(str(p) for p in pids))
        say(f"KEEP: logs and scratch in {self.tmp}")
        say("KEEP: clean up with: kill "
            + " ".join(str(p) for p in ([holder] if holder else []) + pids)
            + f" && rm -rf {self.tmp}")
        self.daemons.clear()
        self.uhid = None
        self.readers.clear()


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------
def create_virtual_device(h: Harness) -> str:
    h.uhid = uh.UhidDevice()
    try:
        node = h.uhid.create()
    except (OSError, TimeoutError) as exc:
        raise Failure(f"could not create the virtual uhid device: {exc}") from exc
    h.nodes.add(node)
    check("uhid device created",
          node.startswith("/dev/hidraw"),
          f"{node} appeared in /sys/class/hidraw with identity 0003:"
          f"{uh.VID:04x}:{uh.PID:04x}, name {uh.TEST_NAME!r}, phys {uh.TEST_PHYS!r}")
    check("virtual device is unmistakable",
          "integration test" in uh.TEST_NAME and "integration-test" in uh.TEST_PHYS,
          f"name={uh.TEST_NAME!r} phys={uh.TEST_PHYS!r}")

    # The tools identify the vendor collection by 1a86:fe00 plus "no input
    # child".  A virtual device must present the same shape; an input child
    # would make their discovery skip it.
    devdir = HID_SYSFS / pathlib.Path(node).name / "device"
    check("virtual device has no input child", not (devdir / "input").is_dir(),
          "the vendor collection is hidraw-only, like interface 1.2 on real hardware")
    descriptor = (devdir / "report_descriptor").read_bytes()
    check("kernel exposes the documented report descriptor",
          descriptor == hid.vendor_report_descriptor(),
          f"{len(descriptor)} bytes, usage page 0xFF00, no report id, 64-byte "
          "input+output")

    # Identity-based rediscovery is the safe path that the daemon uses after a
    # reset.  It must pick the virtual node by name+phys, never a same-vid/pid
    # real interface that may be present.
    ident = life.hidraw_identity(node)
    check("identity captured from sysfs",
          ident is not None and ident.vid == uh.VID and ident.pid == uh.PID
          and ident.name == uh.TEST_NAME and ident.phys == uh.TEST_PHYS,
          f"identity {ident}")
    check("identity-based rediscovery selects the virtual node",
          life.find_hidraw(ident) == node,
          f"find_hidraw({ident}) -> {node}")

    # Discovery order is not controllable while a real 1a86:fe00 interface is
    # present, so the harness does not depend on it: it pins --device.  Report
    # the outcome honestly instead of pretending discovery was exercised.
    found = hid.find_vendor_hidraw()
    if found == node:
        scenario("vendor discovery", f"find_vendor_hidraw() selects {node}")
    else:
        real = life.hidraw_identity(found) if found else None
        say(f"NOTE discovery: find_vendor_hidraw() selects {found!r} "
            f"({real}); a real 1a86:fe00 interface or another vendor node is "
            "present, so the tools under test are pinned with --device and the "
            "harness only asserts identity-based rediscovery")
    return node


def start_homebtn(h: Harness, node: str) -> Daemon:
    daemon = Daemon([TOOLS / "oxp3-homebtn.py", "--device", node,
                     "--uinput-name", HOME_BUTTON_KEY,
                     "--health", "2", "--debug"],
                    h.tmp / "oxp3-homebtn.log")
    h.daemons.append(daemon)
    daemon.wait_for(rf"listening on {re.escape(node)} \(", 10.0,
                    f"a listen on {node}")
    return daemon


def scenario_home_button(h: Harness, node: str) -> tuple[Daemon, EventReader]:
    daemon = start_homebtn(h, node)
    info = wait_new_uinput(h.before_input)
    h.created_input.append(pathlib.Path(info["path"]).name)
    check("daemon created its uinput keyboard",
          info["vendor"] == 0x0001 and info["product"] == 0x0001
          and info["bustype"] == uh.BUS_USB,
          f"{info['path']} id {info['bustype']:04x}:{info['vendor']:04x}:"
          f"{info['product']:04x}")
    check("uinput keyboard advertises KEY_HOMEPAGE",
          key_capable(info["devdir"], uh.KEY_HOMEPAGE),
          f"capabilities/key has bit {uh.KEY_HOMEPAGE}")

    reader = EventReader(info["path"])
    h.readers.append(reader)

    h.uhid.inject(uh.button_report(HOME_BUTTON, True))
    press = reader.read_key_events(1, 5.0)
    check("0xB2 press becomes KEY_HOMEPAGE down", press == [(uh.KEY_HOMEPAGE, 1)],
          f"injected {HOME_BUTTON:#04x} press, read {press}")
    h.uhid.inject(uh.button_report(HOME_BUTTON, False))
    release = reader.read_key_events(1, 5.0)
    check("0xB2 release becomes KEY_HOMEPAGE up", release == [(uh.KEY_HOMEPAGE, 0)],
          f"injected {HOME_BUTTON:#04x} release, read {release}")
    daemon.wait_for(r"button 0x24 pressed", 3.0, "the button press log line")

    fds = hidraw_fd_targets(daemon.proc.pid)
    check("daemon holds only the virtual node", bool(fds) and set(fds) <= {node},
          f"hidraw fds {fds}, virtual node {node}")
    paths = uh.node_paths_in_log(daemon.text())
    check("daemon log only names the virtual node", set(paths) <= {node},
          f"node paths {sorted(set(paths))}")
    return daemon, reader


def scenario_led(h: Harness, node: str) -> None:
    expected = [
        hid.rgb_brightness(0x06, True, "medium"),
        hid.rgb_solid(0x06, (0xFF, 0x66, 0x00)),
    ]
    h.uhid.drain_outputs()
    result = run_tool([TOOLS / "oxp3-led.py", "--device", node, "solid",
                       "ff6600", "--sides", "top", "--level", "medium"])
    check("oxp3-led.py exits cleanly", result.returncode == 0,
          f"exit {result.returncode}; stderr={result.stderr.strip()!r}")
    sent = uh.parse_report_lines(result.stdout)
    check("tool printed exactly the planned frames", sent == expected,
          f"{len(sent)} frame(s): {[f.hex() for f in sent]}")
    received = h.uhid.collect_outputs(len(expected), 10.0)
    check("device received exactly the planned frames",
          [frame for frame, _rtype in received] == expected,
          f"{len(received)} UHID_OUTPUT event(s)")
    check("frames arrived as HID output reports",
          all(rtype == uh.UHID_REPORT_OUTPUT for _frame, rtype in received),
          f"rtype values {[rtype for _frame, rtype in received]}")
    check("tool's own hex output matches the wire bytes",
          sent == [frame for frame, _rtype in received],
          "the 'send <hex>' lines are byte-identical to UHID_OUTPUT")


def write_rgb_config(path: pathlib.Path) -> bytes:
    text = ('[global]\n'
            'brightness = "medium"\n'
            '\n'
            '[zones.top]\n'
            'effect = "solid"\n'
            'color = "#ff6600"\n')
    path.write_text(text, encoding="utf-8")
    return text.encode()


def scenario_rgbd(h: Harness, node: str) -> None:
    cfg_path = h.tmp / "oxp3-rgb.toml"
    write_rgb_config(cfg_path)
    expected = [
        hid.rgb_brightness(0x06, True, "medium"),
        hid.rgb_solid(0x06, (0xFF, 0x66, 0x00)),
    ]

    h.uhid.drain_outputs()
    once = run_tool([TOOLS / "oxp3-rgbd.py", "--once", "--device", node,
                     "--config", str(cfg_path), "--no-journal"])
    check("oxp3-rgbd.py --once exits cleanly", once.returncode == 0,
          f"exit {once.returncode}; stderr={once.stderr.strip()!r}")
    match = re.search(r"applied (\d+) frame\(s\) to (\S+) \(one-shot\)", once.stderr)
    check("rgbd reported the node it applied to",
          match is not None and match.group(2) == node
          and int(match.group(1)) == len(expected),
          f"log line {match.group(0) if match else None!r}")
    received = h.uhid.collect_outputs(len(expected), 10.0)
    check("rgbd daemon wrote exactly the planned frames",
          [frame for frame, _rtype in received] == expected,
          f"{len(received)} UHID_OUTPUT event(s) for side 0x06")

    check_run = run_tool([TOOLS / "oxp3-rgbd.py", "--check", "--device", node,
                          "--config", str(cfg_path)])
    check("oxp3-rgbd.py --check exits cleanly", check_run.returncode == 0,
          f"exit {check_run.returncode}; stdout={check_run.stdout.strip()!r}")
    check("rgbd --check names the virtual node",
          f"device        : {node}" in check_run.stdout,
          f"device line is {node}")


def scenario_reconnect(h: Harness, daemon: Daemon, reader: EventReader,
                       node: str) -> str:
    old = node
    h.uhid.destroy()
    daemon.wait_for(r"device lost: ", 10.0, "the device-lost line")
    daemon.wait_for(r"retrying in ", 10.0, "the backoff line")

    h.uhid = uh.UhidDevice()
    try:
        new_node = h.uhid.create()
    except (OSError, TimeoutError) as exc:
        raise Failure(f"could not recreate the virtual uhid device: {exc}") from exc
    h.nodes.add(new_node)
    match = daemon.wait_for(rf"reconnect #1 to (/\S+) \(identity ([^\n]*)",
                            25.0, "a reconnect by identity")
    reopened, ident_text = match.group(1), match.group(2)
    check("daemon reopened the recreated device by identity",
          reopened == new_node and uh.TEST_NAME in ident_text
          and uh.TEST_PHYS in ident_text,
          f"destroyed {old}, recreated {new_node}, reopened {reopened} "
          f"with identity {ident_text!r}")

    reader.drain()
    h.uhid.inject(uh.button_report(HOME_BUTTON, True))
    press = reader.read_key_events(1, 5.0)
    h.uhid.inject(uh.button_report(HOME_BUTTON, False))
    release = reader.read_key_events(1, 5.0)
    check("button reports work after the reconnect",
          press == [(uh.KEY_HOMEPAGE, 1)] and release == [(uh.KEY_HOMEPAGE, 0)],
          f"press={press} release={release}")

    fds = hidraw_fd_targets(daemon.proc.pid)
    check("daemon holds only the recreated virtual node",
          bool(fds) and set(fds) <= {new_node},
          f"hidraw fds {fds}, recreated node {new_node}")
    paths = uh.node_paths_in_log(daemon.text())
    check("every node the daemon ever opened is a virtual node",
          set(paths) <= {old, new_node},
          f"node paths {sorted(set(paths))}")
    return new_node


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def run(args) -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="oxp3-uhid-integration-"))
    h = Harness(tmp, keep=args.keep)
    h.before_input = input_event_info()
    say("# integration: OXP3 home button + LED/RGB tools on a virtual vendor HID")
    say(f"# repo: {REPO}")
    say(f"# scratch: {tmp}")
    try:
        preflight()
        node = create_virtual_device(h)
        daemon, reader = scenario_home_button(h, node)
        scenario_led(h, node)
        scenario_rgbd(h, node)
        node = scenario_reconnect(h, daemon, reader, node)

        rc = daemon.stop()
        check("home-button daemon exits cleanly on SIGTERM", rc == 0,
              f"exit {rc}")
        gone = all(event_node_gone(event) for event in h.created_input)
        check("daemon removed its uinput keyboard", gone,
              f"{h.created_input} disappeared from /sys/class/input")
        if not args.keep:
            h.cleanup()
            check("cleanup left no virtual hidraw node",
                  not uh.find_hidraw_nodes(),
                  "no /sys/class/hidraw node with the test identity remains")
        say("== integration: all scenarios passed")
        return 0
    finally:
        signal.alarm(0)
        if args.keep:
            h.keep_for_debugging()
        else:
            h.cleanup()
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="end-to-end integration test for the OXP3 home button and "
                    "LED tools against a virtual /dev/uhid device")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="whole-run watchdog in seconds (default 120)")
    parser.add_argument("--keep", action="store_true",
                        help="on exit, leave the virtual device and daemons "
                             "running for debugging (prints cleanup commands)")
    args = parser.parse_args(argv)

    def _alarm(_signum, _frame):
        raise Failure(f"integration harness exceeded its {args.timeout:g}s watchdog")

    def _interrupt(_signum, _frame):
        raise Interrupted()

    signal.signal(signal.SIGALRM, _alarm)
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    signal.alarm(int(max(1, args.timeout)))
    try:
        return run(args)
    except Skip as exc:
        say(f"SKIP: {exc}")
        return 77
    except Failure as exc:
        say(f"FAIL: {exc}")
        return 1
    except Interrupted:
        say("FAIL: interrupted")
        return 1
    except Exception:  # noqa: BLE001 - report and fail, never mask
        say("FAIL: unexpected exception\n" + traceback.format_exc())
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
