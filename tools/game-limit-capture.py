#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""game-limit-capture - what limits a game's frame rate, from one ordinary play session.

Start it, then play: it waits for the game, samples once per second for --seconds, and ends with a
verdict. No root, no game restart, no A/B runs. Read your fps off the overlay while it runs.

    tools/game-limit-capture.py                      # Trails in the Sky 2nd Chapter (sora_2nd.exe)
    tools/game-limit-capture.py --exe Cyberpunk2077.exe --seconds 180

Per second it records:
  GPU   actual clock vs its maximum, busy % (from xe's gtidle residency), throttle reasons
  power package, CPU-core and GPU (uncore) watts from /run/rapl-power, the PL1 limit
  CPU   load of the P-cores (cpu0-1), E-cores (cpu2-9) and LP E-cores (cpu10-13)
  game  its busiest thread: % of one core and which core it ran on
plus, once, the XeSS environment the game process got (which helper, CM_MAX_GRF).

Verdicts:
  GPU-bound at full clock       GPU busy, clock at max: more fps only from less GPU work
  GPU-bound, power-limited      GPU busy, clock held down by PL1/PL2: CPU caps or a higher TDP help
  CPU-bound (one thread)        GPU waiting, one game thread at a full core: CPU clock/placement
  waiting (cap/sync)            GPU waiting, no thread saturated: a frame cap, vsync or sync stalls

Output: ~/.local/state/oxp3-reports/limit-<exe>-<time>.tsv and the summary on stdout.
"""
from __future__ import annotations

import argparse
import glob
import os
import statistics
import sys
import time
from pathlib import Path

CLUSTERS = {"P": range(0, 2), "E": range(2, 10), "LP": range(10, 14)}
XESS_KEYS = ("ANV_CM_HELPER", "CM_MAX_GRF", "ANV_CM_KERNEL_DIR", "INTC_ALT_DRIVER_EXTENSIONS_PATH",
             "VKD3D_DISABLE_EXTENSIONS")


def read(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def num(path: str, default: float = 0.0) -> float:
    try:
        return float(read(path))
    except ValueError:
        return default


def gt_dir() -> str:
    for d in sorted(glob.glob("/sys/class/drm/card*/device/tile0/gt0")):
        if os.path.isdir(d + "/freq0"):
            return d
    sys.exit("no xe GT found under /sys/class/drm")


def find_game(exe: str) -> int | None:
    comm = exe[:15]          # the kernel keeps 15 characters of a process name
    best, threads = None, -1
    for d in glob.glob("/proc/[0-9]*"):
        if read(d + "/comm") != comm:
            continue
        n = len(os.listdir(d + "/task")) if os.path.isdir(d + "/task") else 0
        if n > threads:
            best, threads = int(d[6:]), n
    return best


def cpu_times() -> dict[int, tuple[int, int]]:
    out = {}
    for line in read("/proc/stat").splitlines():
        if line.startswith("cpu") and line[3:4].isdigit():
            f = line.split()
            vals = list(map(int, f[1:]))
            idle = vals[3] + vals[4]
            out[int(f[0][3:])] = (sum(vals), idle)
    return out


def thread_times(pid: int) -> dict[int, tuple[str, int, int]]:
    out = {}
    for t in glob.glob(f"/proc/{pid}/task/*/stat"):
        s = read(t)
        if not s:
            continue
        name = s[s.find("(") + 1:s.rfind(")")]
        f = s[s.rfind(")") + 2:].split()
        out[int(t.split("/")[4])] = (name, int(f[11]) + int(f[12]), int(f[36]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--exe", default="sora_2nd.exe", help="game executable (default: sora_2nd.exe)")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--wait", type=int, default=1800, help="how long to wait for the game to start")
    a = ap.parse_args()

    gt = gt_dir()
    hz = os.sysconf("SC_CLK_TCK")
    print(f"waiting for {a.exe} (start the game and get into gameplay) ...", flush=True)
    t0 = time.time()
    pid = None
    while time.time() - t0 < a.wait:
        pid = find_game(a.exe)
        if pid:
            break
        time.sleep(1)
    if not pid:
        print("the game did not start", file=sys.stderr)
        return 1
    print(f"found {a.exe} (pid {pid}); skipping 20 s of loading, then {a.seconds} s of samples", flush=True)
    time.sleep(20)

    env = {}
    for kv in read(f"/proc/{pid}/environ").split("\0"):
        k, _, v = kv.partition("=")
        if k in XESS_KEYS:
            env[k] = v

    out_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "oxp3-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    tsv = out_dir / f"limit-{a.exe.split('.')[0]}-{time.strftime('%Y%m%d-%H%M%S')}.tsv"
    cols = ["t", "gpu_mhz", "gpu_max", "gpu_busy", "throttle", "pkg_w", "gpu_w", "core_w", "pl1_w",
            "P", "E", "LP", "top_thread", "top_pct", "top_cpu"]
    rows = []
    max_mhz = num(gt + "/freq0/max_freq") or num(gt + "/freq0/rp0_freq")
    pl1 = num("/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw") / 1e6

    prev_idle, prev_t = num(gt + "/gtidle/idle_residency_ms"), time.monotonic()
    prev_cpu, prev_thr = cpu_times(), thread_times(pid)
    with open(tsv, "w") as f:
        f.write("\t".join(cols) + "\n")
        for i in range(a.seconds):
            time.sleep(1)
            if not os.path.isdir(f"/proc/{pid}"):
                print("the game exited", flush=True)
                break
            now = time.monotonic()
            idle = num(gt + "/gtidle/idle_residency_ms")
            dt_ms = (now - prev_t) * 1000
            busy = max(0.0, min(100.0, 100 - (idle - prev_idle) / dt_ms * 100))
            cpu, thr = cpu_times(), thread_times(pid)
            load = {}
            for c, rng in CLUSTERS.items():
                tot = sum(cpu[n][0] - prev_cpu[n][0] for n in rng if n in cpu and n in prev_cpu)
                idl = sum(cpu[n][1] - prev_cpu[n][1] for n in rng if n in cpu and n in prev_cpu)
                load[c] = 100 * (1 - idl / tot) if tot else 0.0
            top = ("-", 0.0, -1)
            for tid, (name, ticks, core) in thr.items():
                if tid in prev_thr:
                    pct = (ticks - prev_thr[tid][1]) / hz / (now - prev_t) * 100
                    if pct > top[1]:
                        top = (name, pct, core)
            reasons = read(gt + "/freq0/throttle/reasons", "?").replace(" ", ",") or "none"
            row = [i + 1, num(gt + "/freq0/act_freq"), max_mhz, round(busy, 1), reasons,
                   num("/run/rapl-power/package-0"), num("/run/rapl-power/uncore"),
                   num("/run/rapl-power/core"), pl1, round(load["P"]), round(load["E"]), round(load["LP"]),
                   top[0], round(top[1]), top[2]]
            rows.append(row)
            f.write("\t".join(map(str, row)) + "\n")
            f.flush()
            prev_idle, prev_t, prev_cpu, prev_thr = idle, now, cpu, thr

    if len(rows) < 10:
        print("too few samples for a verdict (play at least 30 s past loading)")
        return 1
    med = lambda k: statistics.median(r[cols.index(k)] for r in rows)
    busy, mhz, pkg, top_pct = med("gpu_busy"), med("gpu_mhz"), med("pkg_w"), med("top_pct")
    throttled = sum(1 for r in rows if r[cols.index("throttle")] not in ("none", "?")) / len(rows)
    names = [r[cols.index("top_thread")] for r in rows]
    main_thread = max(set(names), key=names.count)

    print(f"\n{a.exe}: {len(rows)} s sampled -> {tsv}")
    print("XeSS environment the game got:")
    for k in XESS_KEYS:
        print(f"  {k:34s} {env.get(k, '<unset>')}")
    print(f"GPU    busy {busy:.0f} %, clock {mhz:.0f} of {max_mhz:.0f} MHz, throttled {throttled:.0%} of the time")
    print(f"power  package {pkg:.1f} W (PL1 {pl1:.0f} W), GPU {med('gpu_w'):.1f} W, CPU cores {med('core_w'):.1f} W")
    print(f"CPU    P {med('P'):.0f} %, E {med('E'):.0f} %, LP {med('LP'):.0f} %; "
          f"busiest game thread '{main_thread}' {top_pct:.0f} % of a core")
    near_max = mhz >= 0.9 * max_mhz
    power_bound = throttled > 0.3 or pkg >= 0.9 * pl1
    if busy >= 90 and near_max:
        verdict = "GPU-bound at full clock: more fps only from less GPU work (XeSS mode, settings, driver)"
    elif busy >= 90:
        verdict = ("GPU-bound, power-limited: the GPU is busy but held below its clock"
                   + (" by the power limit" if power_bound else "") + " - CPU caps or a higher TDP would help")
    elif top_pct >= 85:
        verdict = f"CPU-bound on one thread ('{main_thread}'): CPU clock or core placement, not the GPU"
    else:
        verdict = "waiting: GPU and CPU both have headroom - a frame cap, vsync or a sync stall sets the rate"
    print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
