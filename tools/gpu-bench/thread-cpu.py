#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Attribute a run's CPU time to threads: which one is actually busy?

The DX11 gap is a per-frame CPU cost, and the candidates are the game's own thread, DXVK's
command-submission thread, DXVK's workers, and wineserver. Guessing between them is what this
avoids: with no perf on this machine, /proc still gives per-thread utime/stime, and the thread
names DXVK uses (`dxvk-cs`, `dxvk-worker`, ...) are readable in /proc/<pid>/task/<tid>/comm.

Usage:
    tools/gpu-bench/thread-cpu.py --marker GravityMark.exe --watch --out /tmp/threads.json
    tools/gpu-bench/thread-cpu.py --pid 1234 --seconds 20

`--marker` attaches to the process whose command line contains it (the game under Wine shows up
as GravityMark.exe even though it runs through proton). `--watch` samples until that process
exits, which is the mode the benchmark runner uses.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# gamescope and proton carry the game's name in their own command lines, so a cmdline match picks
# the wrapper instead of the game. The game's process name (comm) is the exe, so match that first
# and never accept a wrapper.
WRAPPERS = ("gamescope", "proton", "python3", "wineserver", "wine64", "wine-preloader")


def find_pid(marker: str) -> int | None:
    by_cmdline = None
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            comm = (d / "comm").read_text().strip()
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if not cmd or "thread-cpu" in cmd:
            continue
        if any(comm.startswith(w) for w in WRAPPERS):
            continue
        if marker in comm:
            return int(d.name)
        if by_cmdline is None and marker in cmd:
            by_cmdline = int(d.name)
    return by_cmdline


def snapshot(pid: int) -> dict[int, tuple[str, int]]:
    """thread id -> (name, cpu ticks) for every thread of the process."""
    out: dict[int, tuple[str, int]] = {}
    base = Path(f"/proc/{pid}/task")
    try:
        tids = list(base.iterdir())
    except OSError:
        return out
    for tid in tids:
        try:
            comm = (tid / "comm").read_text().strip()
            stat = (tid / "stat").read_text()
        except OSError:
            continue
        # the comm field can contain spaces and parentheses: split after the last ')'
        rest = stat[stat.rfind(")") + 2:].split()
        try:
            utime, stime = int(rest[11]), int(rest[12])
        except (IndexError, ValueError):
            continue
        out[int(tid.name)] = (comm, utime + stime)
    return out


def watch(pid: int, seconds: float | None) -> dict:
    first = snapshot(pid)
    t0 = time.time()
    while True:
        time.sleep(0.5)
        if seconds is not None and time.time() - t0 >= seconds:
            break
        if not Path(f"/proc/{pid}").exists():
            break
    last = snapshot(pid) or first
    elapsed = max(0.001, time.time() - t0)
    rows = []
    for tid, (name, ticks) in last.items():
        start = first.get(tid, (name, ticks))[1]
        pct = 100.0 * (ticks - start) / CLOCK_TICKS / elapsed
        rows.append({"tid": tid, "name": name, "cpu_pct": round(pct, 1)})
    rows.sort(key=lambda r: -r["cpu_pct"])
    total = sum(r["cpu_pct"] for r in rows)
    return {"pid": pid, "seconds": round(elapsed, 2), "threads": rows,
            "total_cpu_pct": round(total, 1), "cores_busy": round(total / 100.0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pid", type=int)
    ap.add_argument("--marker", help="substring of the target's command line")
    ap.add_argument("--seconds", type=float, help="sample for this long (default: until exit)")
    ap.add_argument("--watch", action="store_true", help="wait for the marker process, then watch")
    ap.add_argument("--wait", type=float, default=120, help="seconds to wait for the marker")
    ap.add_argument("--out")
    a = ap.parse_args()

    pid = a.pid
    if pid is None:
        if not a.marker:
            ap.error("--pid or --marker is required")
        deadline = time.time() + a.wait
        while pid is None and time.time() < deadline:
            pid = find_pid(a.marker)
            if pid is None:
                time.sleep(0.2)
        if pid is None:
            print(f"no process matching {a.marker!r} appeared", file=sys.stderr)
            return 1
    result = watch(pid, a.seconds if not a.watch else None)
    text = json.dumps(result, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n")
    print(f"{'thread':24s} {'cpu%':>7s}")
    for r in result["threads"][:12]:
        print(f"{r['name'][:24]:24s} {r['cpu_pct']:>7.1f}")
    print(f"-- {result['cores_busy']} cores busy over {result['seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
