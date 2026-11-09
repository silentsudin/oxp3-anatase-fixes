#!/usr/bin/env python3
"""oxp3-adaptivetdp - opt-in adaptive TDP for the OneXPlayer 3 ("OXP3").

Moves the package power limits between the three vendor presets (quiet /
balanced / performance) based on measured load, package temperature and
battery state, by calling tools/oxp3-powerctl.py's own apply path.  It is
off by default, refuses to run without root unless --dry-run, and logs every
decision so a bad policy is reviewable after the fact.

What the vendor does
--------------------
OneXConsole's own adaptive mode lives in
``resources/resources/tools/intel/perf_conf.ini`` [auto_config]:

    GPU_UTIL_THRESHOLD_LOW=80.00
    GPU_UTIL_THRESHOLD_HIGH=90.00
    PL1_THRESHOLD=25
    TIME_INTERVAL=10000

i.e. "自适应功耗" (adaptive power) with two UI modes -- "跟随帧数，动态Tdp"
(follow the frame cap, dynamic TDP) and "上限功耗，动态Gpu" (cap the power,
dynamic GPU) -- polled every 10 s, plus the warning "开启自适应功耗后，在极
少数游戏中可能会导致显卡驱动崩溃" (it can crash the GPU driver in a few
games).  The vendor also switches presets per charger tier
(``powersupplymode_mode1/2/4/8`` = battery / Type-C >=100 W / DC-in /
Type-C <=65 W), which Linux does not expose (only ``ADP1.online``).

This daemon is the "dynamic TDP" half, done conservatively:
  * load from /proc/stat (always) and the GPU busy percentage when the driver
    exposes one -- the vendor's frame-rate/frame-cap signal has no Linux
    equivalent here;
  * temperature from coretemp ("Package id 0"), which the vendor does not use
    at all -- this is the safety addition;
  * battery state from power_supply, which the vendor uses only as a charger
    tier.

Policy
------
A three-level ladder, target chosen from load then clamped by temperature and
battery:

    target = performance  if load >= --up            (default 65 %)
           = balanced     else if load >= --down     (default 30 %)
           = quiet        else

    temp >= --soft-temp (85 C)   -> target at most balanced
    temp >= --max-temp  (92 C)   -> target quiet immediately (safety, no dwell)
    on battery                   -> target at most balanced
    capacity <= --bat-floor (20 %) -> target quiet

Load has to stay above/below the band for --dwell samples before a change is
committed (default 3), and at least --min-interval seconds must pass between
two changes (default 20 s), which is the hysteresis that stops it oscillating
around a threshold.  The 10 s vendor interval is respected as a floor: the
default --interval is 2 s (so it can react to a thermal event) but the
--min-interval default of 20 s is the effective change rate.

Usage
-----
    tools/oxp3-adaptivetdp.py --once --dry-run          # what would it do now
    tools/oxp3-adaptivetdp.py --dry-run --once --json
    sudo tools/oxp3-adaptivetdp.py                      # run (Ctrl-C restores)
    sudo tools/oxp3-adaptivetdp.py --allow-battery-performance
    sudo systemctl start oxp3-adaptivetdp               # after installing the unit

Exit codes: 0 ok, 2 cannot run (no powercap, no root, nothing to do),
3 readback verification failed (from the powerctl write path).

Nothing here touches MSRs, MMIO or the EC directly; it only writes the same
powercap sysfs files ``oxp3-powerctl profile`` writes.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POWERCTL = os.path.join(HERE, "oxp3-powerctl.py")
DEFAULT_LOG = "/var/log/oxp3-adaptivetdp.jsonl"

# Ladder order, quiet < balanced < performance.  Values come from the vendor
# preset table via oxp3-powerctl.PROFILES, not from this file.
LADDER = ("quiet", "balanced", "performance")
LEVEL_OF = {name: idx for idx, name in enumerate(LADDER)}

VERSION = "1.0"


def load_powerctl(path=POWERCTL):
    """Import the sibling oxp3-powerctl.py by path (the filename is not a
    valid module name, and this keeps the daemon and the CLI on one code path)."""
    if not os.path.exists(path):
        raise SystemExit(f"oxp3-adaptivetdp: cannot find {path}")
    spec = importlib.util.spec_from_file_location("oxp3_powerctl", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["oxp3_powerctl"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# sampling
# --------------------------------------------------------------------------- #

def read_first_line(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return default


def read_int(path, default=None):
    text = read_first_line(path)
    if text is None:
        return default
    try:
        return int(text)
    except ValueError:
        return default


class CpuSampler:
    """/proc/stat delta -> busy percentage (no root needed)."""

    def __init__(self):
        self.prev = self._read()

    @staticmethod
    def _read():
        line = read_first_line("/proc/stat", "") or ""
        parts = line.split()
        if not parts or parts[0] != "cpu":
            return None
        values = [int(v) for v in parts[1:11] if v.isdigit()]
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        total = sum(values)
        return total, idle

    def sample(self):
        cur = self._read()
        if not cur or not self.prev:
            self.prev = cur
            return None
        dt = cur[0] - self.prev[0]
        di = cur[1] - self.prev[1]
        self.prev = cur
        if dt <= 0:
            return None
        busy = 100.0 * (dt - di) / dt
        return max(0.0, min(100.0, busy))


def gpu_busy_paths():
    drm = "/sys/class/drm"
    if not os.path.isdir(drm):
        return []
    return sorted(path for path in (
        os.path.join(drm, name, "device", "gpu_busy_percent")
        for name in os.listdir(drm)
    ) if os.path.exists(path))


def read_gpu_busy():
    """Best-effort: i915/xe may not expose gpu_busy_percent at all."""
    for path in gpu_busy_paths():
        value = read_int(path)
        if value is not None:
            return max(0, min(100, value))
    return None


def read_ac_online():
    for name in sorted(os.listdir("/sys/class/power_supply")
                       if os.path.isdir("/sys/class/power_supply") else []):
        base = os.path.join("/sys/class/power_supply", name)
        if read_first_line(os.path.join(base, "type")) in ("Mains", "USB"):
            return read_first_line(os.path.join(base, "online")) == "1"
    return None


def read_battery():
    base = os.path.join("/sys/class/power_supply", "BAT0")
    if not os.path.isdir(base):
        return {}
    return {
        "capacity_pct": read_int(os.path.join(base, "capacity")),
        "status": read_first_line(os.path.join(base, "status")),
        "power_now_w": (lambda v: None if v is None else v / 1e6)(
            read_int(os.path.join(base, "power_now"))),
    }


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #

class Policy:
    """Turns one sample into a target rung of the ladder.

    Pure function of (load, temp, on_ac, capacity) plus thresholds, so the
    decision is reproducible from the log line alone.
    """

    def __init__(self, args):
        self.up = args.up
        self.down = args.down
        self.soft = args.soft_temp
        self.hard = args.max_temp
        self.floor = args.bat_floor
        self.allow_bat_perf = args.allow_battery_performance
        self.ceiling = LEVEL_OF[args.ceiling]

    def target(self, load, temp, on_ac, capacity):
        """Return (rung_index, reason)."""
        if load is None:
            load = 0.0
        if load >= self.up:
            want, why = LEVEL_OF["performance"], f"load {load:.0f} % >= {self.up:g} %"
        elif load >= self.down:
            want, why = LEVEL_OF["balanced"], f"load {load:.0f} % >= {self.down:g} %"
        else:
            want, why = LEVEL_OF["quiet"], f"load {load:.0f} % < {self.down:g} %"

        if temp is not None and temp >= self.hard:
            return LEVEL_OF["quiet"], (f"SAFETY: {temp:.1f} C >= max-temp "
                                       f"{self.hard:g} C, forcing quiet")
        if temp is not None and temp >= self.soft and want > LEVEL_OF["balanced"]:
            want, why = LEVEL_OF["balanced"], (f"{why}; clamped to balanced: "
                                               f"{temp:.1f} C >= soft-temp {self.soft:g} C")
        if on_ac is False and not self.allow_bat_perf and want > LEVEL_OF["balanced"]:
            want, why = LEVEL_OF["balanced"], f"{why}; clamped: on battery"
        if capacity is not None and on_ac is False and capacity <= self.floor:
            want, why = LEVEL_OF["quiet"], (f"clamped to quiet: battery "
                                            f"{capacity} % <= floor {self.floor} %")
        if want > self.ceiling:
            want, why = self.ceiling, f"{why}; clamped to ceiling {LADDER[self.ceiling]}"
        return want, why


class Hysteresis:
    """Commits a rung change only after it is wanted for N samples and the
    minimum interval since the last change has elapsed."""

    def __init__(self, dwell, min_interval, now=None):
        self.dwell = max(1, dwell)
        self.min_interval = min_interval
        self.current = None
        self.pending = None
        self.count = 0
        self.last_change = None if now is None else now

    def update(self, want, now, *, immediate=False):
        """Return ('hold'|'change', rung) -- never changes more than once."""
        if self.current is None:
            self.current = want
            self.last_change = now
            return "change", want
        if want == self.current:
            self.pending, self.count = None, 0
            return "hold", self.current
        if self.pending != want:
            self.pending, self.count = want, 1
        else:
            self.count += 1
        cooled = (self.last_change is None
                  or (now - self.last_change) >= self.min_interval)
        if immediate or (self.count >= self.dwell and cooled):
            self.current = want
            self.pending, self.count = None, 0
            self.last_change = now
            return "change", want
        return "hold", self.current


# --------------------------------------------------------------------------- #
# daemon
# --------------------------------------------------------------------------- #

class AdaptiveTdp:
    def __init__(self, args, pc):
        self.args = args
        self.pc = pc
        self.cpu = CpuSampler()
        self.policy = Policy(args)
        self.hyst = Hysteresis(args.dwell, args.min_interval, now=time.time())
        self.domains, self.missing = pc.require_domains(allow_partial=True)
        self.temp_sensor = pc.find_temp_sensor(args.temp_sensor)
        self.initial = self._current_profile_name()
        # Start out "holding" whatever is already applied so the first sample
        # does not rewrite an identical preset (a "custom" reading still gets
        # replaced, because no preset matches it).
        self.applied = self.initial if self.initial in pc.PROFILES else None
        self.stopped = False
        self.log_fh = None
        if args.log_file:
            self._open_log()

    # -- logging ---------------------------------------------------------
    def _open_log(self):
        path = self.args.log_file
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.log_fh = open(path, "a", encoding="utf-8")
        except OSError as exc:
            print(f"oxp3-adaptivetdp: cannot open log {path}: {exc.strerror} "
                  f"(continuing with stderr only)", file=sys.stderr)
            self.log_fh = None

    def log(self, record):
        record = dict(record)
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        record["dry_run"] = self.args.dry_run
        text = json.dumps(record, sort_keys=True)
        if self.log_fh:
            self.log_fh.write(text + "\n")
            self.log_fh.flush()
        if self.args.json and record.get("event") in ("change", "error", "restore"):
            print(text, flush=True)
        if self.args.verbose or record.get("event") == "change":
            detail = " ".join(f"{k}={v}" for k, v in record.items()
                              if k not in ("ts", "event", "dry_run") and v is not None)
            print(f"[adaptivetdp] {record.get('event', 'sample')}: {detail}",
                  file=sys.stderr, flush=True)

    # -- power actions ---------------------------------------------------
    def _current_profile_name(self):
        try:
            prof = self.pc.current_profile(self.pc.collect_status())
        except Exception:  # noqa: BLE001 - status is advisory here
            return None
        return prof and prof.get("name")

    def apply(self, name):
        preset = self.pc.PROFILES[name]
        values = {level: preset[level] for level, _ in self.pc.LEVELS}
        rep = self.pc.apply_limits(self.domains, values,
                                   dry_run=self.args.dry_run, allow_partial=True)
        platform = None
        if self.args.platform_profile:
            platform = self.pc.write_platform_profile(
                preset["platform_profile"], dry_run=self.args.dry_run)
        self.applied = name
        return values, rep, platform

    def sample(self):
        load_cpu = self.cpu.sample()
        load_gpu = read_gpu_busy()
        load = load_cpu if load_gpu is None else max(load_cpu or 0.0, load_gpu)
        temp = self.pc.read_temp(self.temp_sensor) if self.temp_sensor else None
        battery = read_battery()
        on_ac = read_ac_online()
        return {"load_pct": None if load is None else round(load, 1),
                "load_cpu_pct": None if load_cpu is None else round(load_cpu, 1),
                "load_gpu_pct": load_gpu,
                "temp_c": temp,
                "on_ac": on_ac,
                "battery": battery}

    def step(self):
        s = self.sample()
        now = time.time()
        capacity = s["battery"].get("capacity_pct")
        want, why = self.policy.target(s["load_pct"], s["temp_c"], s["on_ac"], capacity)
        safety = (s["temp_c"] is not None and s["temp_c"] >= self.policy.hard)
        outcome, rung = self.hyst.update(want, now, immediate=safety)
        name = LADDER[rung]
        record = {"event": "sample", **s, "want": LADDER[want], "policy": why,
                  "rung": name, "outcome": outcome}
        if outcome == "change":
            if self.applied != name:
                # Capture "before" first: apply() updates self.applied, and the
                # log has to describe the transition, not the result.
                previous = self.applied or self.initial
                try:
                    values, rep, platform = self.apply(name)
                except self.pc.ControlError as exc:
                    record.update(event="error", error=str(exc), hint=exc.hint)
                    self.log(record)
                    return 2
                record.update(event="change", from_profile=previous,
                              to_profile=name, limits=values,
                              platform_profile=platform,
                              applied=not self.args.dry_run,
                              errors=rep.get("errors") or None)
            else:
                record.update(event="change", to_profile=name, noop=True)
        self.log(record)
        return 0

    def restore(self):
        if self.args.no_restore or self.args.dry_run:
            return
        name = self.initial
        if name in self.pc.PROFILES and name != self.applied:
            try:
                self.apply(name)
                self.log({"event": "restore", "to_profile": name})
            except self.pc.ControlError as exc:
                print(f"oxp3-adaptivetdp: restore to {name} failed: {exc}", file=sys.stderr)

    def run(self):
        self.log({"event": "start", "version": VERSION, "initial_profile": self.initial,
                  "interval_s": self.args.interval, "up": self.args.up,
                  "down": self.args.down, "soft_temp_c": self.args.soft_temp,
                  "max_temp_c": self.args.max_temp, "dwell": self.args.dwell,
                  "min_interval_s": self.args.min_interval,
                  "allow_battery_performance": self.args.allow_battery_performance,
                  "ceiling": self.args.ceiling,
                  "temp_sensor": self.temp_sensor,
                  "gpu_busy": bool(gpu_busy_paths()),
                  "missing_limits": self.missing})
        try:
            while not self.stopped:
                if self.args.steps is not None and self.args.steps <= 0:
                    break
                rc = self.step()
                if self.args.steps is not None:
                    self.args.steps -= 1
                if rc != 0:
                    return rc
                if self.args.steps is not None and self.args.steps <= 0:
                    break
                time.sleep(self.args.interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.restore()
            self.log({"event": "stop", "applied": self.applied})
            if self.log_fh:
                self.log_fh.close()
        return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="oxp3-adaptivetdp.py",
        description="Opt-in adaptive TDP daemon for the OneXPlayer 3: moves between the "
                    "vendor quiet/balanced/performance presets on load, temperature "
                    "and battery state.",
        epilog="Default OFF. Needs root to write; --dry-run works unprivileged. "
               "See docs/hw/battery-tdp.md.",
    )
    parser.add_argument("--version", action="version", version=f"oxp3-adaptivetdp {VERSION}")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between samples (default 2)")
    parser.add_argument("--up", type=float, default=65.0,
                        help="load %% at which performance is wanted (default 65)")
    parser.add_argument("--down", type=float, default=30.0,
                        help="load %% below which quiet is wanted (default 30)")
    parser.add_argument("--soft-temp", type=float, default=85.0,
                        help="degC at which performance is clamped to balanced (default 85)")
    parser.add_argument("--max-temp", type=float, default=92.0,
                        help="degC at which quiet is forced immediately (default 92)")
    parser.add_argument("--bat-floor", type=int, default=20,
                        help="battery %% on battery below which quiet is forced (default 20)")
    parser.add_argument("--dwell", type=int, default=3,
                        help="consecutive samples a new rung must be wanted (default 3)")
    parser.add_argument("--min-interval", type=float, default=20.0,
                        help="minimum seconds between two preset changes (default 20)")
    parser.add_argument("--ceiling", choices=LADDER, default="performance",
                        help="highest preset this daemon may ever apply (default performance)")
    parser.add_argument("--temp-sensor", help="temperature input path (default: coretemp "
                                              "'Package id 0')")
    parser.add_argument("--allow-battery-performance", action="store_true",
                        help="let it use performance on battery (default: cap at balanced)")
    parser.add_argument("--platform-profile", action="store_true",
                        help="also write /sys/firmware/acpi/platform_profile with each change")
    parser.add_argument("--log-file", default=DEFAULT_LOG,
                        help=f"JSONL decision trail (default {DEFAULT_LOG}; '' disables)")
    parser.add_argument("--no-restore", action="store_true",
                        help="leave the last preset applied on exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and log, write nothing (works unprivileged)")
    parser.add_argument("--once", action="store_true", help="one decision, then exit")
    parser.add_argument("--steps", type=int, default=None, help="stop after N samples")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON decision document per sample to stdout")
    parser.add_argument("--verbose", action="store_true",
                        help="log every sample to stderr, not just changes")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.once:
        args.steps = 1
    if args.steps is not None and args.steps < 1:
        args.steps = 1
    if not args.log_file:
        args.log_file = None
    pc = load_powerctl()

    if not args.dry_run:
        try:
            pc.check_root("adaptive TDP (it writes the powercap limits)")
        except pc.ControlError as exc:
            print(f"oxp3-adaptivetdp: {exc}", file=sys.stderr)
            if exc.hint:
                print(f"  hint: {exc.hint}", file=sys.stderr)
            return 2

    try:
        daemon = AdaptiveTdp(args, pc)
    except pc.ControlError as exc:
        print(f"oxp3-adaptivetdp: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        return 2

    def stop(_signum, _frame):
        daemon.stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
