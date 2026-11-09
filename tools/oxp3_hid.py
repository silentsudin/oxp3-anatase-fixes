"""OneXPlayer 3 (ONEXPLAYER 3, "OXP3") vendor-HID helpers.

The OXP3 exposes three USB HID interfaces on 1a86:fe00 (WCH/QinHeng
composite).  Interface 1.2 is a vendor collection
(usage page 0xFF00 / usage 0x0001, 64-byte input+output, no report ID).
That is the channel the Windows tooling uses for the body RGB lighting
and for the extra buttons (home / turbo / keyboard).

Protocol (all frames are exactly 64 bytes, no report ID):

    v1 frame:  [cid] [0x3F] [0x01] [payload ...] [pad 0x00] [0x3F] [cid]

Device -> host reports use the same envelope, which lets the reader
validate a report ([1] == 0x3F and [-2] == 0x3F and [-1] == [0]).

Commands:
    cid 0xB4  button/key mapping table (sent once by the vendor utility)
    cid 0xB2  button report (device -> host) / "intercept" switch (host -> device)
    cid 0xB8  RGB: preset / brightness / per-zone solid colour
    cid 0xB3  rumble strength

    RGB brightness:  B8 3F 01 FD <side> 02 <enabled> 05 <level>
                     level 1 = low, 3 = medium, 4 = high
    RGB solid:       B8 3F 01 FE <side> 02 [r g b] * 18 r g
    RGB preset:      B8 3F 01 <preset> <side> 02
                     preset: 01 aurora, 03 flowing, 05 neon, 07 dreamy,
                             08 sun, 09 cyberpunk, 0B sunset, 0C colorful,
                             0D "monster woke"
    Button report:   byte 6 = button id, byte 12 = 1 press / 0 release
                     ids seen on OXP3: 0x21 guide, 0x22 extra L1,
                     0x23 extra R1, 0x24 home (with real press/release)

Derived from observed traffic on this machine and from the vendor
requirement (1a86:fe00, usage page 0xFF00).  Protocol data only.
"""

from __future__ import annotations

import glob
import os
import sys

VID = 0x1A86
PID = 0xFE00
USAGE_PAGE = 0xFF00
USAGE = 0x0001
REPORT_SIZE = 64

CID_RGB = 0xB8
CID_BUTTON = 0xB2
CID_MAP = 0xB4
CID_RUMBLE = 0xB3

# Side / zone ids accepted by the vendor RGB command.  The OXP3 firmware
# accepts 0x01..0x07; which physical zone each id drives is model dependent,
# so the CLI exposes them by number and by the names below.
SIDE_NAMES = {
    "left_stick": 0x01,
    "right_stick": 0x02,
    "body_left": 0x05,
    "body_right": 0x06,
    "top": 0x07,
}
SIDE_ALIASES = {
    "sticks": [0x01, 0x02],
    "body": [0x05, 0x06],
    "all": [0x01, 0x02, 0x05, 0x06, 0x07],
}

PRESETS = {
    "aurora": 0x01,
    "flowing": 0x03,
    "neon": 0x05,
    "dreamy": 0x07,
    "sun": 0x08,
    "cyberpunk": 0x09,
    "sunset": 0x0B,
    "colorful": 0x0C,
    "monster_woke": 0x0D,
}

LEVELS = {"low": 0x01, "medium": 0x03, "high": 0x04}

BUTTON_NAMES = {
    0x21: "guide",
    0x22: "extra_l1",
    0x23: "extra_r1",
    0x24: "home",  # OXP3: physical home button, real press/release
}


def v1_frame(cid: int, payload) -> bytes:
    """Build one 64-byte v1 frame."""
    payload = bytes(payload)
    frame = bytes([cid, 0x3F, 0x01]) + payload
    if len(frame) + 2 > REPORT_SIZE:
        raise ValueError(f"payload too long: {len(payload)}")
    return frame + bytes(REPORT_SIZE - len(frame) - 2) + bytes([0x3F, cid])


def rgb_brightness(side: int, enabled: bool, level: int) -> bytes:
    return v1_frame(CID_RGB, [0xFD, side, 0x02, 1 if enabled else 0, 0x05, level])


def rgb_solid(side: int, rgb) -> bytes:
    r, g, b = rgb
    return v1_frame(CID_RGB, [0xFE, side, 0x02] + 18 * [r, g, b] + [r, g])


def rgb_preset(side: int, preset: int) -> bytes:
    return v1_frame(CID_RGB, [preset, side, 0x02])


def rgb_off(side: int) -> bytes:
    return v1_frame(CID_RGB, [0xFD, side, 0x02, 0x00, 0x05, 0x04])


def intercept(enabled: bool) -> bytes:
    """Ask the MCU to deliver (or stop delivering) extra buttons as 0xB2 reports."""
    return v1_frame(CID_BUTTON, [0x03 if enabled else 0x00, 0x01, 0x02])


def is_vendor_report(buf: bytes) -> bool:
    return (
        len(buf) >= 16
        and buf[1] == 0x3F
        and buf[-2] == 0x3F
        and buf[-1] == buf[0]
    )


def find_vendor_hidraw() -> str | None:
    """Return the /dev/hidrawN path of the 1a86:fe00 vendor collection."""
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*/device")):
        try:
            txt = open(os.path.join(path, "uevent")).read().upper()
        except OSError:
            continue
        if f"HID_ID=0003:{VID:08X}:{PID:08X}" not in txt:
            continue
        # The vendor interface is hidraw-only: no input child.
        if os.path.isdir(os.path.join(path, "input")):
            continue
        try:
            desc = open(os.path.join(path, "report_descriptor"), "rb").read()
        except OSError:
            desc = b""
        node = "/dev/" + os.path.basename(os.path.dirname(path))
        if b"\x06\x00\xff" in desc:
            return node
        return node
    return None


def open_report(write: bool = True) -> tuple[int, str]:
    dev = find_vendor_hidraw()
    if not dev:
        raise SystemExit("vendor HID interface (1a86:fe00, usage page 0xFF00) not found")
    flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_NONBLOCK
    try:
        fd = os.open(dev, flags)
    except PermissionError:
        raise SystemExit(
            f"{dev}: permission denied. Install tools/oxp3-hid.rules "
            "(see docs/hw/home-button-leds.md)."
        )
    return fd, dev


def send(fd: int, frames) -> int:
    total = 0
    for f in frames:
        if len(f) != REPORT_SIZE:
            raise ValueError("frame must be 64 bytes")
        total += os.write(fd, f)
    return total


def parse_rgb(text: str):
    t = text.strip().lstrip("#")
    if "," in t:
        parts = [int(x, 0) for x in t.split(",")]
        if len(parts) != 3:
            raise SystemExit("colour must be r,g,b")
        return tuple(parts)
    if len(t) == 6:
        return tuple(int(t[i : i + 2], 16) for i in (0, 2, 4))
    if len(t) == 3:
        return tuple(int(c * 2, 16) for c in t)
    named = {
        "red": (0xFF, 0, 0),
        "green": (0, 0xFF, 0),
        "blue": (0, 0, 0xFF),
        "white": (0xFF, 0xFF, 0xFF),
        "orange": (0xFF, 0x60, 0x00),
        "off": (0, 0, 0),
    }
    if t.lower() in named:
        return named[t.lower()]
    raise SystemExit(f"cannot parse colour {text!r}")


def parse_sides(values, default="all") -> list:
    if not values:
        values = [default]
    out = []
    for v in values:
        for part in str(v).split(","):
            part = part.strip()
            if not part:
                continue
            if part in SIDE_ALIASES:
                out.extend(SIDE_ALIASES[part])
            elif part in SIDE_NAMES:
                out.append(SIDE_NAMES[part])
            else:
                n = int(part, 0)
                if not 0 <= n <= 0xFF:
                    raise SystemExit(f"bad side {part}")
                out.append(n)
    seen, res = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            res.append(s)
    return res


def selftest() -> int:
    """Compare encoders with frames captured from this machine (see docs)."""
    ok = True
    cases = [
        # observed at boot: brightness side 5, enabled, 0x05, high
        (rgb_brightness(0x05, True, LEVELS["high"]), "b83f01fd0502010504"),
        # observed at boot: solid amber (hue 53) on side 5
        (rgb_solid(0x05, (0x7F, 0x70, 0x00)), "b83f01fe05027f70007f7000"),
        # observed after setting hue 200: solid blue on side 7
        (rgb_solid(0x07, (0x00, 0x54, 0x7F)), "b83f01fe070200547f0054"),
    ]
    for frame, want in cases:
        got = frame.hex()
        good = got.startswith(want) and len(frame) == REPORT_SIZE
        ok &= good
        print(("PASS " if good else "FAIL ") + got)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest())
