#!/usr/bin/env python3
"""oxp3-rgbd - keep the OneXPlayer 3 body RGB at *our* settings.

Why this exists
---------------
The OXP3 RGB zones are written through the vendor HID collection of
1a86:fe00 (usage page 0xFF00), not through /sys/class/leds.  Handheld
Daemon (HHD) also drives the same node: its OXP driver writes sides
01/02/07 (left stick, right stick, controller connector) and 05/06
(G-key, top) whenever its RGB state changes, when it (re)connects to the
device, twice during its own start-up (10 s apart), and after resume.
That means "last writer wins", and HHD wins whenever it does anything.

This daemon applies a user config once, then re-applies it
  * when HHD writes an RGB frame (journal watcher on hhd.service),
  * when HHD's RGB config value changes (hhdctl poll fallback),
  * after resume (CLOCK_BOOTTIME jump, no root needed),
  * when the hidraw node (re)appears,
  * when the config file changes,
  * and on a slow watchdog (default 30 s) as a last resort.

It only touches the zones listed in the config.  A side that is not in
the config is left exactly as HHD (or the firmware) set it, which makes
"apply our top strip, keep HHD's sticks" possible.

Permission: the vendor hidraw node is root-only by default; install
tools/oxp3-hid.rules once (sudo).  Everything else is a user service:

    mkdir -p ~/.config/systemd/user
    install -m 0644 tools/oxp3-rgbd.service ~/.config/systemd/user/
    install -m 0644 tools/oxp3-rgbd.toml.example ~/.config/oxp3-rgb.toml
    systemctl --user daemon-reload
    systemctl --user enable --now oxp3-rgbd.service

Coexistence with HHD and clean takeover are described in
docs/hw/rgb-coverage.md.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oxp3_hid as hid  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

DEFAULT_CONFIG = os.path.expanduser("~/.config/oxp3-rgb.toml")
EFFECTS = ("solid", "breath", "preset", "off")

# HHD init sequence for the RGB provider: up to two attempts, 10 s apart
# (HHD plugins/rgb: RGB_SET_TIMES=2, RGB_SET_INTERVAL=10).
HHD_SETTLE_DELAYS = (3.0, 12.0, 25.0)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _zone_side(key: str) -> int:
    k = str(key).strip()
    low = k.lower()
    if low in hid.SIDE_NAMES:
        return hid.SIDE_NAMES[low]
    if low in hid.SIDE_ALIASES_COMPAT:
        return hid.SIDE_ALIASES_COMPAT[low]
    try:
        side = int(k, 0)
    except ValueError:
        raise ValueError(f"unknown zone {key!r}; use {', '.join(hid.SIDE_NAMES)} or an id")
    if side not in hid.SIDE_NAMES.values() and side not in hid.SIDE_UNUSED:
        raise ValueError(f"zone 0x{side:02x} is not a known OXP3 zone")
    return side


def load_config(path: str) -> dict:
    if tomllib is None:
        raise SystemExit("Python >= 3.11 is required (tomllib)")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    g = raw.get("global", {}) or {}
    brightness = str(g.get("brightness", "high")).lower()
    if brightness not in ("keep",) and brightness not in hid.LEVELS:
        raise ValueError(f"global.brightness must be keep or one of {', '.join(hid.LEVELS)}")
    zones = raw.get("zones", {}) or {}
    if not zones:
        raise ValueError("config has no [zones.*] entries")
    parsed = {"brightness": brightness, "zones": []}
    for key, z in zones.items():
        z = z or {}
        effect = str(z.get("effect", "solid")).lower()
        if effect not in EFFECTS:
            raise ValueError(f"zone {key}: effect must be one of {', '.join(EFFECTS)}")
        side = _zone_side(key)
        level = str(z.get("level", g.get("level", brightness))).lower()
        if level == "keep":
            level = brightness
        if level == "keep":
            level = "high"  # only used for the off frame
        if level not in hid.LEVELS:
            raise ValueError(f"zone {key}: level must be one of {', '.join(hid.LEVELS)}")
        entry = {"side": side, "name": key, "effect": effect, "level": level}
        if effect in ("solid", "breath"):
            try:
                entry["color"] = hid.parse_rgb(str(z.get("color", "#ffffff")))
            except SystemExit as e:
                raise ValueError(f"zone {key}: {e}")
        if effect == "preset":
            preset = z.get("preset")
            if preset is None:
                raise ValueError(f"zone {key}: preset effect needs preset = <name|1..11>")
            try:
                entry["preset_kind"], entry["preset_value"] = hid.resolve_preset(preset)
            except SystemExit as e:
                raise ValueError(f"zone {key}: {e}")
            entry["preset"] = preset
        parsed["zones"].append(entry)
    if not parsed["zones"]:
        raise ValueError("config has no usable zones")
    return parsed


def build_frames(cfg: dict, only_side: int | None = None) -> list:
    frames = []
    for z in cfg["zones"]:
        if only_side is not None and z["side"] != only_side:
            continue
        side, level = z["side"], hid.LEVELS[z["level"]]
        if z["effect"] == "off":
            frames.append(hid.rgb_brightness(side, False, level))
            continue
        if cfg["brightness"] != "keep":
            frames.append(hid.rgb_brightness(side, True, level))
        if z["effect"] == "solid":
            frames.append(hid.rgb_solid(side, z["color"]))
        elif z["effect"] == "breath":
            frames.append(hid.rgb_breath(side, z["color"]))
        elif z["effect"] == "preset":
            value = z["preset_value"]
            frames.append(hid.rgb_solid(side, value) if z["preset_kind"] == "solid"
                          else hid.rgb_preset(side, value))
    return frames


# ---------------------------------------------------------------------------
# HHD state helpers
# ---------------------------------------------------------------------------

def hhdctl(*args: str) -> str | None:
    try:
        r = subprocess.run(["hhdctl", *args], capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except Exception:
        return None


def hhd_rgb_mode() -> str | None:
    out = hhdctl("get", "rgb.handheld.mode.mode", "--values", "--sep=")
    return out or None


def journal_readable(unit: str = "hhd") -> bool:
    try:
        r = subprocess.run(
            ["journalctl", "-u", unit, "-n", "0", "--no-pager", "-q"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

class RgbDaemon:
    def __init__(self, args):
        self.args = args
        self.config_path = args.config
        self.cfg = None
        self.cfg_mtime = 0.0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.last_apply = 0.0
        self.last_error = None
        self.dev_present = hid.find_vendor_hidraw() is not None
        self.hhd_journal_ok = False
        self._last_hhd_mode = None
        self._last_sched_log = 0.0
        self._boot_delta = self._boottime_delta()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _boottime_delta() -> float:
        try:
            return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
        except (AttributeError, OSError):
            return 0.0

    def reload_config(self) -> bool:
        try:
            mtime = os.path.getmtime(self.config_path)
            if mtime == self.cfg_mtime and self.cfg is not None:
                return False
            cfg = load_config(self.config_path)
            with self.lock:
                self.cfg = cfg
                self.cfg_mtime = mtime
            log(f"config loaded: {len(cfg['zones'])} zone(s), brightness={cfg['brightness']}")
            return True
        except Exception as e:
            msg = f"config error: {e}"
            if msg != self.last_error:
                log(msg)
                self.last_error = msg
            return False

    def schedule(self, why: str) -> None:
        now = time.time()
        if now - self._last_sched_log > 1.0:
            log(f"re-apply requested ({why})")
            self._last_sched_log = now
        self.wake.set()

    def apply(self, why: str) -> None:
        with self.lock:
            cfg = self.cfg
        if cfg is None:
            return
        frames = build_frames(cfg)
        dev = hid.find_vendor_hidraw()
        if not dev:
            if self.dev_present or self.last_apply == 0:
                log("vendor hidraw node not found; will retry when it appears")
            self.dev_present = False
            return
        self.dev_present = True
        if self.args.dry_run:
            log(f"dry-run: would apply {len(frames)} frame(s) to {dev} ({why})")
            for f in frames:
                print(f.hex())
            self.last_apply = time.time()
            return
        try:
            fd = os.open(dev, os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError:
            # The node exists; this is an access problem, not a presence one.
            if self.last_error != "permission":
                log(f"{dev}: permission denied - install tools/oxp3-hid.rules (see docs/hw/rgb-coverage.md)")
                self.last_error = "permission"
            return
        try:
            hid.send(fd, frames)
            self.last_apply = time.time()
            self.last_error = None
            log(f"applied {len(frames)} frame(s) to {dev} ({why})")
        except OSError as e:
            log(f"write to {dev} failed: {e}")
            self.last_error = str(e)
        finally:
            os.close(fd)

    # -- watchers --------------------------------------------------------
    def journal_watcher(self) -> None:
        """Follow hhd.service and trigger on any 0xB8 frame it sends."""
        cmd = ["journalctl", "-u", self.args.hhd_unit, "-f", "-o", "cat",
               "--since=now", "-n", "0"]
        while not self.stop.is_set():
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
            except FileNotFoundError:
                log("journalctl not found; using hhdctl poll fallback")
                return
            self.hhd_journal_ok = True
            buf = None
            try:
                for line in p.stdout:
                    if self.stop.is_set():
                        break
                    if "OXP C:" in line:
                        if buf:
                            self._check_frames(buf)
                        # HHD 4.1.12 wraps the frame onto following lines, but
                        # older/newer builds may log it on the marker line.
                        buf = re.sub(r"[^0-9a-fA-F]", "", line.split("OXP C:", 1)[1])
                        if len(buf) >= 128:
                            self._check_frames(buf)
                            buf = ""
                        continue
                    if buf is None:
                        continue
                    t = line.strip()
                    if t and re.fullmatch(r"[0-9a-fA-F]+", t):
                        buf += t
                    elif buf:
                        self._check_frames(buf)
                        buf = ""
                    if buf and len(buf) >= 128:
                        self._check_frames(buf)
                        buf = ""
            finally:
                self.hhd_journal_ok = False
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    pass
            if self.stop.is_set():
                return
            log("journal watcher ended; restarting in 3 s")
            time.sleep(3)

    def _check_frames(self, text: str) -> None:
        for i in range(0, len(text) - 127, 128):
            f = text[i:i + 128]
            if not f.startswith("b83f01"):
                continue
            try:
                raw = bytes.fromhex(f)
            except ValueError:
                continue
            if raw[1] == 0x3F and raw[-2] == 0x3F and raw[-1] == raw[0]:
                self.schedule("HHD wrote an RGB frame")

    def hhd_poll_watcher(self) -> None:
        """Fallback: re-apply when HHD's RGB mode changes."""
        while not self.stop.is_set():
            if self.hhd_journal_ok:
                time.sleep(2)
                continue
            mode = hhd_rgb_mode()
            if mode != self._last_hhd_mode:
                if self._last_hhd_mode is not None:
                    self.schedule(f"HHD rgb mode changed to {mode!r}")
                self._last_hhd_mode = mode
            time.sleep(2)

    def resume_watcher(self) -> None:
        """CLOCK_BOOTTIME advances across suspend, CLOCK_MONOTONIC does not."""
        while not self.stop.is_set():
            time.sleep(1)
            delta = self._boottime_delta()
            if delta - self._boot_delta > 1.5:
                self._boot_delta = delta
                # HHD usually redraws on wake; give it a moment, then apply.
                time.sleep(2)
                self.schedule("resume detected")
            else:
                self._boot_delta = delta

    def device_watcher(self) -> None:
        while not self.stop.is_set():
            dev = hid.find_vendor_hidraw()
            present = dev is not None
            if present != self.dev_present:
                self.dev_present = present
                self.schedule("vendor hidraw node appeared" if present else "vendor hidraw node disappeared")
            time.sleep(1)

    # -- main ------------------------------------------------------------
    def run_once(self) -> int:
        if not self.reload_config():
            return 1
        self.apply("one-shot")
        return 0 if self.last_error is None else 1

    def run(self) -> int:
        if not self.reload_config():
            return 1
        log(f"starting: config={self.config_path} hhd_rgb_mode={hhd_rgb_mode()!r}")
        use_journal = (not self.args.no_journal) and journal_readable(self.args.hhd_unit)
        if not use_journal:
            log("hhd journal not used; relying on hhdctl poll + watchdog")

        threads = [
            threading.Thread(target=self.device_watcher, daemon=True),
            threading.Thread(target=self.resume_watcher, daemon=True),
            threading.Thread(target=self.hhd_poll_watcher, daemon=True),
        ]
        if use_journal:
            threads.append(threading.Thread(target=self.journal_watcher, daemon=True))
        for t in threads:
            t.start()

        # Apply now, then after HHD's own start-up bursts, then on demand.
        self.apply("startup")
        prev = 0.0
        for delay in (t for t in HHD_SETTLE_DELAYS if t <= (self.args.settle or 0)):
            if self.stop.wait(delay - prev):
                break
            prev = delay
            self.apply(f"HHD settle +{delay:.0f}s")

        last_watchdog = time.time()
        last_cfg_check = 0.0
        while not self.stop.is_set():
            woke = self.wake.wait(timeout=0.5)
            if woke:
                self.wake.clear()
                # Sliding debounce: wait until the burst is quiet, cap 3 s.
                deadline = time.time() + 3.0
                while time.time() < deadline and self.wake.wait(self.args.reapply_delay):
                    self.wake.clear()
                if not self.stop.is_set():
                    self.apply("after HHD change")
                last_watchdog = time.time()
                continue
            now = time.time()
            if now - last_cfg_check > 2:
                last_cfg_check = now
                if self.reload_config():
                    self.apply("config changed")
            if self.args.watchdog and now - last_watchdog > self.args.watchdog:
                last_watchdog = now
                self.apply("watchdog")
        log("stopping")
        return 0

    def check(self) -> int:
        unit = self.args.hhd_unit
        ok = True
        dev = hid.find_vendor_hidraw()
        print(f"device        : {dev or 'NOT FOUND'}")
        if dev:
            st = os.stat(dev)
            print(f"permissions   : {oct(st.st_mode & 0o777)} uid={st.st_uid} gid={st.st_gid}")
        print(f"journal       : {'readable' if journal_readable(unit) else 'NOT readable (poll fallback)'}")
        print(f"hhd rgb mode  : {hhd_rgb_mode()!r}")
        try:
            cfg = load_config(self.config_path)
        except Exception as e:
            print(f"config        : ERROR {e}")
            return 1
        print(f"config        : {self.config_path} brightness={cfg['brightness']}")
        for z in cfg["zones"]:
            extra = ""
            if "color" in z:
                extra = " color=%02x%02x%02x" % z["color"]
            if "preset" in z:
                extra = f" preset={z['preset']}"
            print(f"  zone {z['name']:12s} side=0x{z['side']:02x} effect={z['effect']:6s}"
                  f" level={z['level']}{extra}")
        if not dev:
            ok = False
        print(f"frames        : {len(build_frames(cfg))} (dry-run with --once --dry-run)")
        return 0 if ok else 1


def takeover_text() -> str:
    return """\
Clean takeover (pick ONE; each is a privileged change to HHD, not to the kernel):

  1. Runtime + persistent HHD setting (recommended, reversible):
       hhdctl set hhd.settings.rgb=false
     Re-enable with:
       hhdctl set hhd.settings.rgb=true

  2. Disable just the OXP3 RGB output, keep HHD for buttons/gyro:
       hhdctl set rgb.handheld.mode.mode=disabled
     (HHD still re-sends "off" frames on changes; our daemon reapplies.)

  3. Blacklist the whole RGB provider at HHD start-up (needs root):
       sudoedit /etc/hhd/plugins.yml      # add "- rgb" under blacklist:
       sudo systemctl restart hhd
     Remove the line and restart to restore.

With HHD's RGB disabled, run oxp3-rgbd as a user service and use
`systemctl --user restart oxp3-rgbd` after editing the config.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="keep OneXPlayer 3 RGB at our settings")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="TOML config path")
    ap.add_argument("--once", action="store_true", help="apply once and exit")
    ap.add_argument("--check", action="store_true", help="print state and exit")
    ap.add_argument("--dry-run", action="store_true", help="print frames, never write")
    ap.add_argument("--watchdog", type=float, default=30.0,
                    help="seconds between unconditional re-applies (0 = off)")
    ap.add_argument("--reapply-delay", type=float, default=0.5,
                    help="quiet time after an HHD burst before re-applying")
    ap.add_argument("--settle", type=float, default=25.0,
                    help="also re-apply during this many seconds after start (HHD init bursts)")
    ap.add_argument("--no-journal", action="store_true",
                    help="do not follow hhd.service; use hhdctl polling only")
    ap.add_argument("--hhd-unit", default="hhd",
                    help="systemd unit to follow for HHD RGB frames (default: hhd)")
    ap.add_argument("--takeover", action="store_true", help="print HHD takeover instructions")
    ap.add_argument("--selftest", action="store_true", help="encoder + config self test")
    args = ap.parse_args(argv)

    if args.takeover:
        print(takeover_text())
        return 0
    if args.selftest:
        rc = hid.selftest()
        import tempfile
        sample = """[global]\nbrightness = "medium"\n
[zones.left_stick]\neffect = "solid"\ncolor = "#ff6600"\n
[zones.gkey]\neffect = "preset"\npreset = "aurora"\n
[zones.top]\neffect = "off"\n"""
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
                f.write(sample)
                tmp = f.name
            cfg = load_config(tmp)
            frames = build_frames(cfg)
            ok = len(cfg["zones"]) == 3 and len(frames) == 5
            print(("PASS " if ok else "FAIL ") + f"config parses ({len(cfg['zones'])} zones, {len(frames)} frames)")
            rc |= 0 if ok else 1
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return rc

    daemon = RgbDaemon(args)
    if args.check:
        return daemon.check()
    if args.once:
        return daemon.run_once()

    def _stop(_sig, _frm):
        daemon.stop.set()
        daemon.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
