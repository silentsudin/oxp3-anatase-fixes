#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
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
    cid 0xB8  RGB: preset / brightness / per-zone solid colour / breathing
    cid 0xB3  rumble strength

    RGB brightness:  B8 3F 01 FD <side> 02 <enabled> 05 <level>
                     level 1 = low, 3 = medium, 4 = high (2 = mid-low,
                     accepted by the vendor UI but not used by HHD)
    RGB solid:       B8 3F 01 FE <side> 02 [r g b] * 18 r g
    RGB breathing:   B8 3F 01 F0 <side> 02 [r g b] * 18 r g
                     (same layout as solid; the vendor UI calls this
                     "pure breathing" and only offers it for gkey/top)
    RGB preset:      B8 3F 01 <mc> <side> 02
    Button report:   byte 6 = button id, byte 12 = 1 press / 0 release
                     ids seen on OXP3: 0x21 guide, 0x22 extra L1,
                     0x23 extra R1, 0x24 home (with real press/release)

Zones / side ids on the OXP3 (verified against the vendor partition
table in OneXConsole app.asar, where each partition's ``pcodeList`` is
exactly the firmware side id, and against frames captured from HHD):

    0x01  left stick          (vendor "left_stick",  pcode 1)
    0x02  right stick         (vendor "right_stick", pcode 2)
    0x05  G-key zone          (vendor "gkey",        pcode 5)
    0x06  top strip           (vendor "top",         pcode 6)
    0x07  controller connector(vendor "connector",   pcode 7)

    0x03 / 0x04 are the V-zone and keyboard of *other* OneXPlayer
    models (vendor table for the 5-partition variant).  They are not
    part of the OXP3 partition list; ``oxp3-led.py probe`` can check
    whether this firmware still reacts to them.

Effects / presets: the firmware takes one "mc" byte.  The vendor UI's
11 preset titles (app.asar i18n ``presetTitle1..11``) map onto the
codes below, matching HHD's OXP driver name->code table:

    1  monster-woke  0x0D     7  colorful      0x0C
    2  flowing       0x03     8  AOK classic   0x0E
    3  sunset        0x0B     9  OXP classic   solid B7 30 00
    4  neon          0x05    10  aurora        0x01
    5  dreamy        0x07    11  warm sun      0x08
    6  cyberpunk     0x09

Derived from observed traffic on this machine, HHD's OXP driver and the
vendor UI config.  Protocol data only; no vendor code is included.
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

# Report descriptor of the vendor collection (interface 1.2), byte for byte the
# shape documented in docs/hw/home-button-leds.md: usage page 0xFF00, usage
# 0x0001, 64-byte input and output reports, no report ID.  It is also what the
# uhid integration harness (tests/integration/hid/) presents to the kernel, so
# keep this list and the documentation in sync.
VENDOR_REPORT_DESCRIPTOR = bytes.fromhex(
    "0600ff0901a101090215002600ff750895408106"
    "090215002600ff750895409106"
    "c0"
)


def vendor_report_descriptor() -> bytes:
    """A fresh copy of the documented 0xFF00 vendor report descriptor."""
    # bytes(bytes) may return the same immutable object; go through bytearray
    # so callers really get a private copy.
    return bytes(bytearray(VENDOR_REPORT_DESCRIPTOR))

# Side / zone ids accepted by the OXP3 RGB command.
SIDE_NAMES = {
    "left_stick": 0x01,
    "right_stick": 0x02,
    "gkey": 0x05,
    "top": 0x06,
    "connector": 0x07,
}
# Older aliases from the first protocol write-up (the physical mapping of
# 0x05/0x06 turned out to be G-key zone and top strip, not body halves).
SIDE_ALIASES_COMPAT = {
    "body_left": 0x05,
    "body_right": 0x06,
}
# Present in the vendor protocol but only used by other OneXPlayer models.
SIDE_UNUSED = {
    0x03: "v_zone",
    0x04: "keyboard",
}
SIDE_ALIASES = {
    "sticks": [0x01, 0x02],
    "primary": [0x01, 0x02, 0x07],
    "secondary": [0x05, 0x06],
    "body": [0x05, 0x06],  # compatibility alias
    "all": [0x01, 0x02, 0x05, 0x06, 0x07],
    "probe": [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08],
}

# Vendor preset id -> firmware "mc" byte.  id 9 is not a preset frame: the
# vendor's "OXP classic" is a solid cherry red.
PRESET_IDS = {
    "monster_woke": 1,
    "flowing": 2,
    "sunset": 3,
    "neon": 4,
    "dreamy": 5,
    "cyberpunk": 6,
    "colorful": 7,
    "aok_classic": 8,
    "oxp_classic": 9,
    "aurora": 10,
    "warm_sun": 11,
}
PRESET_ALIASES = {
    "monster": "monster_woke",
    "monsterwoke": "monster_woke",
    "flow": "flowing",
    "sun": "warm_sun",
    "warm": "warm_sun",
    "classic": "oxp_classic",
    "aok": "aok_classic",
}
PRESET_MC = {
    1: 0x0D,
    2: 0x03,
    3: 0x0B,
    4: 0x05,
    5: 0x07,
    6: 0x09,
    7: 0x0C,
    8: 0x0E,
    10: 0x01,
    11: 0x08,
}
PRESET_CLASSIC_RGB = (0xB7, 0x30, 0x00)  # vendor "OXP classic"

# Name -> mc, kept for the original API and for scripts that predate ids.
PRESETS = {name: mc for name, mc in (
    ("aurora", 0x01), ("flowing", 0x03), ("neon", 0x05), ("dreamy", 0x07),
    ("sun", 0x08), ("cyberpunk", 0x09), ("sunset", 0x0B),
    ("colorful", 0x0C), ("monster_woke", 0x0D), ("aok_classic", 0x0E),
)}

LEVELS = {
    "low": 0x01,
    "mid": 0x02,     # vendor UI level 2; HHD skips it, firmware accepts it
    "medium": 0x03,
    "high": 0x04,
}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}

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


def rgb_brightness(side: int, enabled: bool, level) -> bytes:
    if isinstance(level, str):
        level = LEVELS[level]
    return v1_frame(CID_RGB, [0xFD, side, 0x02, 1 if enabled else 0, 0x05, level])


def rgb_solid(side: int, rgb) -> bytes:
    r, g, b = rgb
    return v1_frame(CID_RGB, [0xFE, side, 0x02] + 18 * [r, g, b] + [r, g])


def rgb_breath(side: int, rgb) -> bytes:
    """Solid colour with the firmware's breathing animation (vendor: pure breathing)."""
    r, g, b = rgb
    return v1_frame(CID_RGB, [0xF0, side, 0x02] + 18 * [r, g, b] + [r, g])


def rgb_preset(side: int, preset: int) -> bytes:
    """Preset by raw mc byte (legacy API)."""
    return v1_frame(CID_RGB, [preset, side, 0x02])


def rgb_preset_id(side: int, preset_id: int) -> bytes:
    """Preset by vendor id 1..11; id 9 (OXP classic) is emitted as a solid."""
    if preset_id == 9:
        return rgb_solid(side, PRESET_CLASSIC_RGB)
    mc = PRESET_MC[preset_id]
    return rgb_preset(side, mc)


def rgb_off(side: int, level: int = LEVELS["high"]) -> bytes:
    return v1_frame(CID_RGB, [0xFD, side, 0x02, 0x00, 0x05, level])


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
            with open(os.path.join(path, "uevent"), encoding="utf-8",
                      errors="replace") as fh:
                txt = fh.read().upper()
        except OSError:
            continue
        if f"HID_ID=0003:{VID:08X}:{PID:08X}" not in txt:
            continue
        # The vendor interface is hidraw-only: no input child.
        if os.path.isdir(os.path.join(path, "input")):
            continue
        try:
            with open(os.path.join(path, "report_descriptor"), "rb") as fh:
                desc = fh.read()
        except OSError:
            desc = b""
        node = "/dev/" + os.path.basename(os.path.dirname(path))
        if b"\x06\x00\xff" in desc:
            return node
        return node
    return None


def open_report(write: bool = True, dev: str | None = None) -> tuple[int, str]:
    """Open the vendor hidraw node, or ``dev`` when one is given explicitly.

    An explicit node is used verbatim and never replaced by a discovery scan.
    The uhid integration harness pins its virtual node this way so the tools
    can never open a real 1a86:fe00 interface by accident.
    """
    if dev:
        node = dev
        if not os.path.exists(node):
            raise SystemExit(f"{node}: explicit --device node does not exist")
    else:
        node = find_vendor_hidraw()
        if not node:
            raise SystemExit(
                "vendor HID interface (1a86:fe00, usage page 0xFF00) not found")
    flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_NONBLOCK
    try:
        fd = os.open(node, flags)
    except PermissionError:
        raise SystemExit(
            f"{node}: permission denied. Install tools/oxp3-hid.rules "
            "(see docs/hw/home-button-leds.md)."
        )
    return fd, node


def _write_frame(fd: int, frame: bytes) -> int:
    """Write one frame completely: hidraw accepts short writes under load."""
    view = memoryview(frame)
    written = 0
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(f"short write to hidraw: {written}/{len(frame)} bytes")
        view = view[n:]
        written += n
    return written


def send(fd: int, frames) -> int:
    total = 0
    for f in frames:
        f = bytes(f)
        if len(f) != REPORT_SIZE:
            raise ValueError("frame must be 64 bytes")
        total += _write_frame(fd, f)
    return total


NAMED_COLORS = {
    "red": (0xFF, 0, 0),
    "green": (0, 0xFF, 0),
    "blue": (0, 0, 0xFF),
    "white": (0xFF, 0xFF, 0xFF),
    "orange": (0xFF, 0x60, 0x00),
    "off": (0, 0, 0),
}


def parse_rgb(text: str):
    t = text.strip().lstrip("#")
    # Names are checked first: "red" is three characters, so the hex shortcut
    # below used to consume it and fail on int("rr", 16), making the named
    # colours with 3 or 6 letters unreachable.
    if t.lower() in NAMED_COLORS:
        return NAMED_COLORS[t.lower()]
    rgb = None
    try:
        if "," in t:
            parts = [int(x, 0) for x in t.split(",")]
            if len(parts) != 3:
                raise SystemExit("colour must be r,g,b")
            rgb = tuple(parts)
        elif len(t) == 6:
            rgb = tuple(int(t[i : i + 2], 16) for i in (0, 2, 4))
        elif len(t) == 3:
            rgb = tuple(int(c * 2, 16) for c in t)
    except ValueError:
        rgb = None
    if rgb is None:
        raise SystemExit(f"cannot parse colour {text!r} (use #rrggbb, rrggbb or r,g,b)")
    if any(not 0 <= c <= 255 for c in rgb):
        # bytes() in v1_frame() would otherwise raise a bare ValueError.
        raise SystemExit(f"cannot parse colour {text!r}: each component must be 0..255")
    return rgb


def parse_sides(values, default="all") -> list:
    if not values:
        values = [default]
    out = []
    for v in values:
        for part in str(v).split(","):
            part = part.strip()
            if not part:
                continue
            low = part.lower()
            if low in SIDE_ALIASES:
                out.extend(SIDE_ALIASES[low])
            elif low in SIDE_NAMES:
                out.append(SIDE_NAMES[low])
            elif low in SIDE_ALIASES_COMPAT:
                out.append(SIDE_ALIASES_COMPAT[low])
            else:
                try:
                    if low.startswith("0x"):
                        n = int(part, 16)
                    elif part.isdigit():
                        n = int(part, 10)   # accept zero-padded ids such as 01/02
                    else:
                        raise ValueError
                except ValueError:
                    raise SystemExit(f"bad side {part}")
                if not 0 <= n <= 0xFF:
                    raise SystemExit(f"bad side {part}")
                out.append(n)
    seen, res = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            res.append(s)
    return res


def resolve_preset(text):
    """Return ('mc', code) or ('solid', rgb) for a preset name or vendor id."""
    t = str(text).strip().lower()
    t = PRESET_ALIASES.get(t, t)
    if t in PRESET_IDS:
        pid = PRESET_IDS[t]
    else:
        try:
            pid = int(t, 0)
        except ValueError:
            raise SystemExit(
                "unknown preset %r; names: %s (or ids 1..11)"
                % (text, ", ".join(PRESET_IDS))
            )
    if pid not in PRESET_IDS.values():
        raise SystemExit(f"bad preset id {pid}; expected 1..11")
    if pid == 9:
        return ("solid", PRESET_CLASSIC_RGB)
    return ("mc", PRESET_MC[pid])


def preset_frames(side: int, preset) -> list:
    kind, value = resolve_preset(preset)
    return [rgb_solid(side, value) if kind == "solid" else rgb_preset(side, value)]


# ---------------------------------------------------------------------------
# Self test against frames captured on this machine (2026-11-09, HHD 4.1.12).
# Each case is (description, our frame, captured 64-byte frame prefix).
CAPTURED = {
    # (our encoder output, exact 64-byte frame observed from HHD)
    "brightness medium side 1": (
        rgb_brightness(0x01, True, "medium"),
        "b83f01fd010201050300000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003fb8",
    ),
    "brightness high side 7": (
        rgb_brightness(0x07, True, "high"),
        "b83f01fd070201050400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003fb8",
    ),
    "off side 5": (
        rgb_off(0x05),
        "b83f01fd050200050400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003fb8",
    ),
    "preset monster_woke side 2": (
        rgb_preset_id(0x02, 1),
        "b83f010d020200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003fb8",
    ),
    "preset aurora side 7": (
        rgb_preset_id(0x07, 10),
        "b83f0101070200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003fb8",
    ),
    "preset warm sun side 1": (
        rgb_preset_id(0x01, 11),
        "b83f0108010200000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003fb8",
    ),
    "oxp classic = solid cherry red side 1": (
        rgb_preset_id(0x01, 9),
        "b83f01fe0102b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b73000b7303fb8",
    ),
    "duality primary red side 1": (
        rgb_solid(0x01, (0x7F, 0x00, 0x00)),
        "b83f01fe01027f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f003fb8",
    ),
    "duality secondary green side 5": (
        rgb_solid(0x05, (0x00, 0x7F, 0x00)),
        "b83f01fe0502007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f00007f3fb8",
    ),
}


def selftest() -> int:
    ok = True
    for name, (frame, want) in CAPTURED.items():
        # Every vector is a full 64-byte frame; require an exact match so a
        # truncated future vector fails instead of silently comparing a prefix.
        got = frame.hex()
        good = len(frame) == REPORT_SIZE and len(want) == REPORT_SIZE * 2 and got == want
        ok &= good
        print(("PASS " if good else "FAIL ") + name + ("  " + got if not good else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest())
