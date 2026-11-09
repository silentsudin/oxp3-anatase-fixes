#!/usr/bin/env python3
"""oxp3-led - colour / brightness / preset control for OneXPlayer 3 body RGB.

The OXP3 body lighting (sticks + top strip) is driven through the vendor
HID collection of 1a86:fe00 (usage page 0xFF00), not through
/sys/class/leds.  This tool speaks that protocol directly.

Examples:
    oxp3-led.py status
    oxp3-led.py solid ff6600                 # all known zones
    oxp3-led.py solid 0080ff --sides 7      # only the "top" zone id
    oxp3-led.py brightness high --sides all
    oxp3-led.py off
    oxp3-led.py effect aurora --sides 1,2
    oxp3-led.py probe                        # light one side at a time

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
              f"node and may overwrite this. See docs/hw/home-button-leds.md.")


def cmd_status(_args, _dry):
    dev = hid.find_vendor_hidraw()
    print(f"vendor interface : {dev or 'NOT FOUND'}")
    if dev:
        node = os.path.basename(dev)
        try:
            perm = oct(os.stat(dev).st_mode & 0o777)
            uid, gid = os.stat(dev).st_uid, os.stat(dev).st_gid
            print(f"permissions      : {perm} uid={uid} gid={gid}")
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
    print("zones (side ids)  : " + ", ".join(f"{k}=0x{v:02x}" for k, v in hid.SIDE_NAMES.items()))
    print("presets           : " + ", ".join(sorted(hid.PRESETS)))
    # Optional: HHD state, because HHD also drives this device.
    mode = hhd_rgb_mode()
    if mode:
        print("hhd rgb mode      : " + mode)


def cmd_solid(args, dry):
    rgb = hid.parse_rgb(args.colour)
    sides = hid.parse_sides(args.sides)
    level = hid.LEVELS[args.level]
    frames = []
    for side in sides:
        frames.append(hid.rgb_brightness(side, True, level))
        frames.append(hid.rgb_solid(side, rgb))
    echo_frames(frames, dry)
    if dry:
        return 0
    warn_hhd()
    fd, _ = hid.open_report()
    try:
        hid.send(fd, frames)
    finally:
        os.close(fd)
    print(f"set {args.colour} on sides {sides}")
    return 0


def cmd_brightness(args, dry):
    sides = hid.parse_sides(args.sides)
    if args.level == "off":
        frames = [hid.rgb_off(s) for s in sides]
    else:
        frames = [hid.rgb_brightness(s, True, hid.LEVELS[args.level]) for s in sides]
    echo_frames(frames, dry)
    if dry:
        return 0
    warn_hhd()
    fd, _ = hid.open_report()
    try:
        hid.send(fd, frames)
    finally:
        os.close(fd)
    print(f"brightness {args.level} on sides {sides}")
    return 0


def cmd_off(args, dry):
    sides = hid.parse_sides(args.sides)
    frames = [hid.rgb_off(s) for s in sides]
    echo_frames(frames, dry)
    if dry:
        return 0
    warn_hhd()
    fd, _ = hid.open_report()
    try:
        hid.send(fd, frames)
    finally:
        os.close(fd)
    print(f"off on sides {sides}")
    return 0


def cmd_effect(args, dry):
    name = args.name.lower()
    if name not in hid.PRESETS:
        raise SystemExit(f"unknown preset {name!r}; known: {', '.join(sorted(hid.PRESETS))}")
    sides = hid.parse_sides(args.sides)
    frames = []
    for side in sides:
        frames.append(hid.rgb_brightness(side, True, hid.LEVELS[args.level]))
        frames.append(hid.rgb_preset(side, hid.PRESETS[name]))
    echo_frames(frames, dry)
    if dry:
        return 0
    warn_hhd()
    fd, _ = hid.open_report()
    try:
        hid.send(fd, frames)
    finally:
        os.close(fd)
    print(f"preset {name} on sides {sides}")
    return 0


def cmd_probe(args, dry):
    """Light each side with its own colour so the zone map can be identified."""
    sides = hid.parse_sides(args.sides, default="1,2,5,6,7")
    palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255),
               (0, 255, 255), (255, 160, 0), (255, 255, 255)]
    if dry:
        for i, side in enumerate(sides):
            print(f"dry-run side {side}: {hid.rgb_solid(side, palette[i % len(palette)]).hex()}")
        return 0
    fd, _ = hid.open_report()
    try:
        for i, side in enumerate(sides):
            colour = palette[i % len(palette)]
            hid.send(fd, [hid.rgb_brightness(side, True, hid.LEVELS[args.level]),
                          hid.rgb_solid(side, colour)])
            print(f"side {side} -> RGB{colour}  ({args.delay:.1f}s)")
            time.sleep(args.delay)
    finally:
        os.close(fd)
    print("probe done; note which physical zone changed for each side id")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="OneXPlayer 3 body RGB control")
    p.add_argument("--dry-run", action="store_true", help="print frames, do not write")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="show device, ids and zone ids")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("solid", help="solid colour on one or more zones")
    s.add_argument("colour")
    s.add_argument("--sides", action="append", help="side ids or names (default: all)")
    s.add_argument("--level", choices=list(hid.LEVELS), default="high")
    s.set_defaults(fn=cmd_solid)

    s = sub.add_parser("brightness", help="enable and set brightness")
    s.add_argument("level", choices=list(hid.LEVELS) + ["off"])
    s.add_argument("--sides", action="append")
    s.set_defaults(fn=cmd_brightness)

    s = sub.add_parser("off", help="disable the zones")
    s.add_argument("--sides", action="append")
    s.set_defaults(fn=cmd_off)

    s = sub.add_parser("effect", help="hardware preset effect")
    s.add_argument("name", help=", ".join(sorted(hid.PRESETS)))
    s.add_argument("--sides", action="append")
    s.add_argument("--level", choices=list(hid.LEVELS), default="high")
    s.set_defaults(fn=cmd_effect)

    s = sub.add_parser("probe", help="light each zone id separately to map them")
    s.add_argument("--sides", action="append")
    s.add_argument("--level", choices=list(hid.LEVELS), default="high")
    s.add_argument("--delay", type=float, default=1.5)
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("selftest", help="check encoders against captured frames")
    s.set_defaults(fn=lambda a, d: hid.selftest())

    args = p.parse_args(argv)
    return args.fn(args, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
