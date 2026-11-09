#!/usr/bin/env python3
"""oxp3-homebtn - turn the OneXPlayer 3 home button into KEY_HOMEPAGE.

The OXP3 home button is *not* part of any evdev keyboard on Linux: the
MCU delivers it as a vendor HID report on the 1a86:fe00 interface with
usage page 0xFF00 (same collection that carries the RGB commands).
A press looks like

    B2 3F ... <button id> ...  1        (byte 6 = id, byte 12 = 1)
    B2 3F ... <button id> ...  0        (release)

with 0x24 being the physical home button (0x21 is "guide", 0x22/0x23 are
the extra shoulder buttons).

This daemon reads those reports and either
  * creates a virtual keyboard and emits KEY_HOMEPAGE (default), or
  * runs a command on press (--exec), e.g. to launch Steam Big Picture.

Permission: the vendor hidraw node and /dev/uinput are root-only by
default.  Install tools/oxp3-hid.rules once (sudo); the logged-in user
then gets access through the uaccess ACL.  See
docs/hw/home-button-leds.md.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oxp3_hid as hid  # noqa: E402

# uinput ioctl numbers (linux/uinput.h)
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503
EV_SYN = 0x00
EV_KEY = 0x01

KEYS = {
    "homepage": 172,   # KEY_HOMEPAGE
    "back": 158,       # KEY_BACK
    "search": 217,     # KEY_SEARCH
    "f20": 193,        # KEY_F20
    "playpause": 164,  # KEY_PLAYPAUSE
}


class UInputKeyboard:
    def __init__(self, name: str, keycode: int):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, keycode)
        # struct uinput_setup { input_id id; char name[80]; __u32 ff_effects_max; }
        ident = struct.pack("HHHH", 0x0003, 0x0001, 0x0001, 0x0001)  # BUS_USB
        setup = ident + name.encode()[:79].ljust(80, b"\x00") + struct.pack("I", 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.keycode = keycode

    def emit(self, value: int):
        now = time.time()
        ev = struct.pack("qqHHi", int(now), int((now % 1) * 1_000_000),
                         EV_KEY, self.keycode, value)
        os.write(self.fd, ev)
        os.write(self.fd, struct.pack("qqHHi", int(now), int((now % 1) * 1_000_000),
                                      EV_SYN, 0, 0))

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)


def read_reports(fd: int):
    """Yield validated vendor reports from the hidraw node."""
    while True:
        try:
            buf = os.read(fd, hid.REPORT_SIZE)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise
        if len(buf) >= 16 and hid.is_vendor_report(buf):
            yield buf


def main(argv=None):
    p = argparse.ArgumentParser(description="OXP3 home button -> key/command")
    p.add_argument("--button", type=lambda s: int(s, 0), default=0x24,
                   help="button id to react to (default 0x24 = home)")
    p.add_argument("--also-guide", action="store_true",
                   help="also react to 0x21 (guide)")
    p.add_argument("--key", choices=sorted(KEYS), default="homepage")
    p.add_argument("--exec", dest="exec_cmd", default=None,
                   help="run this command on press instead of emitting a key")
    p.add_argument("--hold", type=float, default=0.0,
                   help="max press length in s; 0 = wait for the device release")
    p.add_argument("--intercept", action="store_true",
                   help="ask the MCU to intercept extra buttons (experimental)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    buttons = {args.button}
    if args.also_guide:
        buttons.add(0x21)

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    kbd = None
    if not args.exec_cmd:
        kbd = UInputKeyboard("OXP3 Home Button", KEYS[args.key])

    fd = None
    dev = None
    pressed = {}
    press_deadline = {}
    try:
        while running:
            if fd is None:
                try:
                    fd, dev = hid.open_report(write=args.intercept)
                    print(f"listening on {dev}", flush=True)
                    if args.intercept:
                        os.write(fd, hid.intercept(True))
                except SystemExit as e:
                    print(str(e), file=sys.stderr, flush=True)
                    time.sleep(2)
                    continue
            try:
                r, _, _ = select.select([fd], [], [], 0.25)
            except OSError:
                r = []
            if r:
                try:
                    for buf in read_reports(fd):
                        code = buf[6]
                        value = buf[12]
                        if code not in buttons:
                            continue
                        down = value == 1
                        old = pressed.get(code, False)
                        if down and not old:
                            pressed[code] = True
                            press_deadline[code] = time.time() + args.hold
                            print(f"button 0x{code:02x} pressed", flush=True)
                            if args.exec_cmd:
                                subprocess.Popen(args.exec_cmd, shell=True)
                            else:
                                kbd.emit(1)
                        elif not down and old:
                            pressed[code] = False
                            print(f"button 0x{code:02x} released", flush=True)
                            if not args.exec_cmd:
                                kbd.emit(0)
                except OSError as e:
                    print(f"read error: {e}", file=sys.stderr, flush=True)
                    os.close(fd)
                    fd = None
            # optional max-hold safety net for buttons that never send a release
            if args.hold > 0:
                now = time.time()
                for code, is_down in list(pressed.items()):
                    if is_down and now >= press_deadline.get(code, 0):
                        pressed[code] = False
                        if not args.exec_cmd:
                            kbd.emit(0)
                        print(f"button 0x{code:02x} auto-released", flush=True)
    finally:
        if fd is not None:
            os.close(fd)
        if kbd is not None:
            kbd.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
