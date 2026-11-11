#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3-led - colour / brightness / preset control for OneXPlayer 3 body RGB.

The OXP3 body lighting (stick rings, G-key zone, top strip, controller
connector) is driven through the vendor HID collection of 1a86:fe00
(usage page 0xFF00), not through /sys/class/leds.  This tool speaks that
protocol directly.

Zone ids (firmware side ids, = vendor pcode values):
    01 left stick      02 right stick      05 G-key zone
    06 top strip       07 controller connector
    03 V-zone / 04 keyboard exist in other OneXPlayer models only.

Examples:
    oxp3-led.py status
    oxp3-led.py solid ff6600                  # all OXP3 zones
    oxp3-led.py solid 0080ff --sides top      # only the top strip
    oxp3-led.py breath 3050ff --sides 01,02   # breathing solid
    oxp3-led.py brightness medium --sides all
    oxp3-led.py effect aurora --sides 1,2,7
    oxp3-led.py effect 9 --sides all          # vendor id 9 = OXP classic
    oxp3-led.py off
    oxp3-led.py probe                         # light one id at a time
    oxp3-led.py selftest
    oxp3-led.py --device /dev/hidraw9 solid ff0000   # pin a node (tests/debug)

Permission: /dev/hidrawN of the vendor interface is root-only by default.
Install tools/oxp3-hid.rules once (sudo) as described in
docs/hw/home-button-leds.md; after that the logged-in user has access.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oxp3_hid as hid  # noqa: E402


def echo_frames(frames, dry):
    for f in frames:
        print(("dry-run " if dry else "send ") + f.hex())


def hhd_rgb_mode():
    try:
        out = subprocess.run(
            ["hhdctl", "get", "rgb.handheld.mode.mode", "--values", "--sep="],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def warn_hhd():
    mode = hhd_rgb_mode()
    if mode and mode != "disabled":
        print(f"note: HHD rgb plugin is active (mode={mode}); it writes the same "
              f"node and may overwrite this after a config change or resume. "
              f"Use tools/oxp3-rgbd.py for a persistent job.")


def level_value(text):
    t = str(text).lower()
    if t in hid.LEVELS:
        return hid.LEVELS[t]
    try:
        n = int(t, 0)
    except ValueError:
        raise SystemExit(f"bad level {text!r}; use {', '.join(hid.LEVELS)} or 1..4")
    if n not in hid.LEVEL_NAMES:
        raise SystemExit(f"bad level {n}; firmware levels are 1..4")
    return n


def cmd_status(args, _dry):
    dev = getattr(args, "device", None) or hid.find_vendor_hidraw()
    print(f"vendor interface : {dev or 'NOT FOUND'}")
    if dev:
        node = os.path.basename(dev)
        try:
            st = os.stat(dev)
            print(f"permissions      : {oct(st.st_mode & 0o777)} uid={st.st_uid} gid={st.st_gid}")
        except OSError as e:
            print(f"permissions      : {e}")
        try:
            rd = open(f"/sys/class/hidraw/{node}/device/report_descriptor", "rb").read()
            print(f"report descriptor: {rd[:28].hex()} ...")
        except OSError:
            pass
    print(f"ids              : USB {hid.VID:04x}:{hid.PID:04x}, "
          f"usage page {hid.USAGE_PAGE:04x}, usage {hid.USAGE:04x}, "
          f"{hid.REPORT_SIZE}-byte reports, no report id")
    print("zones (side ids) : " + ", ".join(f"{k}=0x{v:02x}" for k, v in hid.SIDE_NAMES.items()))
    print("other-model ids  : " + ", ".join(f"0x{v:02x}={k}" for v, k in hid.SIDE_UNUSED.items())
          + "  (probe to test)")
    print("brightness       : " + ", ".join(f"{k}={v}" for k, v in hid.LEVELS.items()))
    print("presets          : " + ", ".join(f"{k}={v}" for k, v in hid.PRESET_IDS.items()))
    mode = hhd_rgb_mode()
    if mode:
        print("hhd rgb mode     : " + mode)


def cmd_zones(_args, _dry):
    print("id  name             vendor partition    notes")
    for name, side in hid.SIDE_NAMES.items():
        print(f"{side:02x}  {name:15s}  {name:15s}    OXP3")
    for side, name in hid.SIDE_UNUSED.items():
        print(f"{side:02x}  {name:15s}  {name:15s}    other OneXPlayer models")
    print(f"{hid.PRESET_CLASSIC_RGB!r} = OXP classic (solid, id 9)")


def _emit(frames, dry, what, device=None):
    echo_frames(frames, dry)
    if dry:
        return 0
    warn_hhd()
    fd, _ = hid.open_report(dev=device)
    try:
        hid.send(fd, frames)
    finally:
        os.close(fd)
    print(what)
    return 0


def cmd_solid(args, dry):
    rgb = hid.parse_rgb(args.colour)
    sides = hid.parse_sides(args.sides)
    level = level_value(args.level)
    maker = hid.rgb_breath if args.breath else hid.rgb_solid
    frames = []
    for side in sides:
        frames.append(hid.rgb_brightness(side, True, level))
        frames.append(maker(side, rgb))
    return _emit(frames, dry, f"set {args.colour}"
                              f"{' (breathing)' if args.breath else ''} on sides {sides}",
                 getattr(args, "device", None))


def cmd_brightness(args, dry):
    sides = hid.parse_sides(args.sides)
    if args.level == "off":
        frames = [hid.rgb_off(s, hid.LEVELS["high"]) for s in sides]
    else:
        level = level_value(args.level)
        frames = [hid.rgb_brightness(s, True, level) for s in sides]
    return _emit(frames, dry, f"brightness {args.level} on sides {sides}",
                 getattr(args, "device", None))


def cmd_off(args, dry):
    sides = hid.parse_sides(args.sides)
    frames = [hid.rgb_off(s) for s in sides]
    return _emit(frames, dry, f"off on sides {sides}",
                 getattr(args, "device", None))


def cmd_effect(args, dry):
    sides = hid.parse_sides(args.sides)
    level = level_value(args.level)
    kind, value = hid.resolve_preset(args.name)
    frames = []
    for side in sides:
        frames.append(hid.rgb_brightness(side, True, level))
        frames.append(hid.rgb_solid(side, value) if kind == "solid" else hid.rgb_preset(side, value))
        if side in hid.SIDE_ALIASES["secondary"]:
            print(f"note: side 0x{side:02x} has no preset support in the vendor UI "
                  f"(pure colour only); firmware may ignore the preset frame.")
    label = args.name
    if kind == "solid":
        label += f" (solid {value[0]:02x}{value[1]:02x}{value[2]:02x})"
    return _emit(frames, dry, f"preset {label} on sides {sides}",
                 getattr(args, "device", None))


def cmd_probe(args, dry):
    """Light each side id with its own colour so the zone map can be identified."""
    sides = hid.parse_sides(args.sides, default="probe")
    palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255),
               (0, 255, 255), (255, 160, 0), (255, 255, 255),
               (128, 0, 255), (255, 255, 0)]
    if dry:
        for i, side in enumerate(sides):
            print(f"dry-run side {side}: {hid.rgb_solid(side, palette[i % len(palette)]).hex()}")
        return 0
    fd, _ = hid.open_report(dev=getattr(args, "device", None))
    try:
        for i, side in enumerate(sides):
            colour = palette[i % len(palette)]
            hid.send(fd, [hid.rgb_brightness(side, True, level_value(args.level)),
                          hid.rgb_solid(side, colour)])
            print(f"side 0x{side:02x} -> RGB{colour}  ({args.delay:.1f}s)")
            time.sleep(args.delay)
    finally:
        os.close(fd)
    print("probe done; note which physical zone changed for each side id")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="OneXPlayer 3 body RGB control")
    p.add_argument("--dry-run", action="store_true", help="print frames, do not write")
    p.add_argument("--device", metavar="PATH", default=None,
                   help="use this hidraw node instead of discovering 1a86:fe00")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="show device, ids and zone ids")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("zones", help="print the zone-id table")
    s.set_defaults(fn=cmd_zones)

    s = sub.add_parser("solid", help="solid colour on one or more zones")
    s.add_argument("colour")
    s.add_argument("--sides", action="append", help="side ids or names (default: all)")
    s.add_argument("--level", default="high", help="low|mid|medium|high or 1..4")
    s.add_argument("--breath", action="store_true", help="use the breathing animation (0xF0)")
    s.set_defaults(fn=cmd_solid)

    s = sub.add_parser("breath", help="breathing solid colour (alias of solid --breath)")
    s.add_argument("colour")
    s.add_argument("--sides", action="append")
    s.add_argument("--level", default="high")
    s.set_defaults(fn=lambda a, d: cmd_solid(argparse.Namespace(
        colour=a.colour, sides=a.sides, level=a.level, breath=True), d))

    s = sub.add_parser("brightness", help="enable and set brightness")
    s.add_argument("level", help="low|mid|medium|high|off (or 1..4)")
    s.add_argument("--sides", action="append")
    s.set_defaults(fn=cmd_brightness)

    s = sub.add_parser("off", help="disable the zones")
    s.add_argument("--sides", action="append")
    s.set_defaults(fn=cmd_off)

    s = sub.add_parser("effect", help="hardware preset effect by name or vendor id 1..11")
    s.add_argument("name", help=", ".join(hid.PRESET_IDS) + ", or 1..11")
    s.add_argument("--sides", action="append")
    s.add_argument("--level", default="high")
    s.set_defaults(fn=cmd_effect)

    s = sub.add_parser("probe", help="light each zone id separately to map them")
    s.add_argument("--sides", action="append")
    s.add_argument("--level", default="high")
    s.add_argument("--delay", type=float, default=1.5)
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("selftest", help="check encoders against captured frames")
    s.set_defaults(fn=lambda a, d: hid.selftest())

    args = p.parse_args(argv)
    return args.fn(args, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
