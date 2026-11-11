#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Keep the left-stick RGB zone (0x01) in sync with HHD's handheld colour.

HHD applies its handheld RGB to the five vendor zones, but when the emulated
Steam controller takes LED control it stops applying the handheld colour, and on
this device the left-stick zone keeps its previous firmware effect/colour while
the rest follow HHD. There is no LED class device and no HHD API for "apply to
zone 1 only", so this small reconciler reads HHD's live state (hhdctl, root
socket) and re-sends just the left-stick zone through the vendor HID protocol
when HHD's configured colour changes. It never writes the other four zones: HHD
stays their owner.

Only static 'solid' mode is mirrored; hardware effect modes are left untouched
(their per-zone temporal state cannot be reproduced by a static frame).
"""
from __future__ import annotations

import colorsys
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LED = os.path.join(HERE, "oxp3-led.py") if os.path.exists(os.path.join(HERE, "oxp3-led.py")) else "/usr/local/libexec/oxp3-led.py"
INTERVAL = 2.0


def hhd_state() -> dict[str, str]:
    try:
        out = subprocess.run(["hhdctl", "get"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    state = {}
    for line in out.splitlines():
        if "=" in line and not line.startswith("ERROR"):
            k, v = line.split("=", 1)
            state[k] = v
    return state


def solid_rgb(state: dict[str, str]):
    try:
        hue = float(state["rgb.handheld.mode.solid.hue"])
        sat = float(state["rgb.handheld.mode.solid.saturation"]) / 100.0
        val = float(state["rgb.handheld.mode.solid.brightness"]) / 100.0
    except (KeyError, ValueError):
        return None
    r, g, b = colorsys.hsv_to_rgb((hue % 360.0) / 360.0, sat, val)
    return round(r * 255), round(g * 255), round(b * 255)


def apply(frames_args: list[str]) -> None:
    try:
        subprocess.run([sys.executable, LED, *frames_args], capture_output=True, timeout=10)
    except Exception:
        pass


def main() -> int:
    prev = None
    while True:
        state = hhd_state()
        mode = state.get("rgb.handheld.mode.mode", "")
        if mode == "solid":
            rgb = solid_rgb(state)
            if rgb and rgb != prev:
                apply(["solid", "%02x%02x%02x" % rgb, "--sides", "left_stick"])
                prev = rgb
        elif mode == "disabled":
            if prev is not None:
                apply(["off", "--sides", "left_stick"])
                prev = None
        else:
            # effect/other modes: do not fight HHD, just forget the static colour
            prev = None
        time.sleep(INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
