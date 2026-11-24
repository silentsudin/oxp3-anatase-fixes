#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""sched-bench - rank sched_ext schedulers on a CPU-bound GravityMark run, unattended.

GravityMark D3D11 at 640x360 is bound by its main (submission) thread: 93.6 fps against 86.6 at
1280x720, that thread at 98 % of a core, and the GPU at 13 W instead of 22 W. That makes it a
repeatable stand-in for the question "does the scheduler put the hot thread on a fast core?". With
scx_cosmos the thread sat on E-cores (Smart caps 2.0 GHz) while both P-cores idled.

Each candidate scheduler is loaded live (sched_ext swaps in under a second), GravityMark runs headless
as the invoking user with tools/game-limit-capture.py alongside, and the order is mirrored
(A B C ... C B A) so drift (temperature, caches) cancels. A scheduler that the sched_ext watchdog
unloads mid-run is reported as invalid, not as a result. oxp3-gaming-sched.service (scx_cosmos)
is always restored at the end, also on Ctrl-C.

    sudo tools/sched-bench.py                      # every candidate, 2 rounds (~1 h)
    sudo tools/sched-bench.py --only cosmos,lavd   # a subset
    sudo tools/sched-bench.py --list

The CPU caps are left as they are (hhd's CPU Boost setting); they are recorded with each run.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT = "oxp3-gaming-sched.service"
BENCH_UNIT = "oxp3-sched-bench"
P_CORES = {0, 1}

# name -> (command, sched_ext ops name prefix or None for the kernel's EEVDF)
CANDIDATES = {
    "cosmos": (["scx_cosmos", "-c", "0", "-p", "0"], "cosmos"),
    "cosmos-perf": (["scx_cosmos", "-c", "0", "-p", "0", "-m", "performance",
                     "--primary-overload-us", "1000"], "cosmos"),
    "cosmos-perf-P": (["scx_cosmos", "-c", "0", "-p", "0", "-m", "performance",
                       "--primary-overload-us", "1000", "-P"], "cosmos"),
    # autopilot is LAVD's default. Never --performance here: sched-ext/scx #3340 (pegged CPU, leak)
    "lavd": (["scx_lavd"], "lavd"),
    # scx_lavd 1.1.4 rejects --balanced with --no-core-compaction ("conflicting options")
    "lavd-balanced": (["scx_lavd", "--balanced"], "lavd"),
    "beerland-perf": (["scx_beerland", "-m", "performance"], "beerland"),
    "p2dq": (["scx_p2dq"], "p2dq"),
    "bpfland": (["scx_bpfland"], "bpfland"),
    "eevdf": (None, None),
}


def sh(*cmd, check=False, **kw):
    return subprocess.run(list(cmd), capture_output=True, text=True, check=check, **kw)


def sched_ops() -> str:
    state = Path("/sys/kernel/sched_ext/state").read_text().strip() if Path(
        "/sys/kernel/sched_ext/state").exists() else "disabled"
    if state != "enabled":
        return ""
    try:
        return Path("/sys/kernel/sched_ext/root/ops").read_text().strip()
    except OSError:
        return ""


def stop_all():
    sh("systemctl", "stop", f"{BENCH_UNIT}.service")
    sh("systemctl", "stop", UNIT)
    for _ in range(50):
        if not sched_ops():
            return
        time.sleep(0.1)


def load(name: str) -> bool:
    cmd, ops = CANDIDATES[name]
    stop_all()
    if cmd is None:
        return not sched_ops()
    exe = sh("bash", "-c", f"command -v {cmd[0]}").stdout.strip()
    if not exe:
        print(f"   {name}: {cmd[0]} not installed", flush=True)
        return False
    sh("systemd-run", f"--unit={BENCH_UNIT}", "--collect", "--quiet", exe, *cmd[1:])
    for _ in range(100):
        if sched_ops().startswith(ops):
            return True
        time.sleep(0.1)
    return False


def restore():
    sh("systemctl", "stop", f"{BENCH_UNIT}.service")
    sh("systemctl", "start", UNIT)
    for _ in range(100):                   # the service is up before its scheduler is loaded
        if sched_ops():
            return
        time.sleep(0.1)


def caps() -> str:
    def khz(n, f):
        try:
            return int(Path(f"/sys/devices/system/cpu/cpu{n}/cpufreq/{f}").read_text()) / 1e6
        except (OSError, ValueError):
            return 0.0
    return " ".join(f"{c}{khz(n, 'scaling_max_freq'):.1f}" for c, n in (("P", 0), ("E", 2), ("LP", 10)))


def as_user(user: pwd.struct_passwd, *cmd, **kw):
    if os.geteuid() == user.pw_uid:            # --plumbing-test: already the user
        return subprocess.Popen(list(cmd), **kw)
    env = ["env", f"HOME={user.pw_dir}", f"XDG_RUNTIME_DIR=/run/user/{user.pw_uid}",
           f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{user.pw_uid}/bus",
           f"USER={user.pw_name}", f"LOGNAME={user.pw_name}", "LANG=C.UTF-8",
           f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}"]
    return subprocess.Popen(["sudo", "-u", user.pw_name, *env, *cmd], **kw)


def run_one(user, name: str, out: Path, seq: int, switch: bool = True) -> dict:
    label = f"{seq:02d}-{name}"
    loaded = load(name) if switch else sched_ops().startswith(CANDIDATES[name][1] or "~")
    row = {"seq": seq, "sched": name, "caps": caps(), "loaded": loaded, "valid": False}
    if not loaded:
        row["note"] = "did not load"
        return row
    tsv = out / f"{label}.threads.tsv"
    cap = as_user(user, sys.executable, "-I", str(REPO / "tools/game-limit-capture.py"),
                  "--exe", "GravityMark.exe", "--seconds", "110", "--wait", "180", "--skip", "25",
                  "--out", str(tsv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gm = as_user(user, sys.executable, "-I", str(REPO / "tools/gpu-bench/gravitymark.py"), "run",
                 "--api", "d3d11", "--width", "640", "--height", "360", "--label", label,
                 "--out", str(out), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    gm.communicate(timeout=900)
    cap.wait(timeout=300)
    still = sched_ops().startswith(CANDIDATES[name][1]) if CANDIDATES[name][1] else not sched_ops()
    res = next(out.glob(f"*-{label}/result.json"), None)
    if res:
        summ = json.loads(res.read_text()).get("summary", {})
        row.update(fps=summ.get("fps_mean"), low1=summ.get("fps_1pct_low"),
                   p99_ms=summ.get("frametime_ms_p99"))
    if tsv.exists():
        lines = [l.split("\t") for l in tsv.read_text().splitlines()]
        hdr, data = lines[0], lines[1:]
        if data:
            cpu = [int(r[hdr.index("top_cpu")]) for r in data]
            row["hot_pct"] = statistics.median(float(r[hdr.index("top_pct")]) for r in data)
            row["hot_on_p"] = round(100 * sum(c in P_CORES for c in cpu) / len(cpu))
            row["hot_cores"] = " ".join(f"cpu{c}:{n}" for c, n in Counter(cpu).most_common(3))
            row["pkg_w"] = statistics.median(float(r[hdr.index("pkg_w")]) for r in data)
    row["valid"] = bool(res and still and row.get("fps"))
    if not still:
        row["note"] = "scheduler unloaded during the run (sched_ext watchdog?)"
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated candidates (default: all)")
    ap.add_argument("--rounds", type=int, default=2, help="mirrored rounds (default 2)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--plumbing-test", action="store_true",
                    help="one run on the running scheduler, no root, no switching")
    a = ap.parse_args()
    if a.plumbing_test:
        user = pwd.getpwuid(os.getuid())
        out = Path(user.pw_dir) / ".cache/oxp3-fixes/gpu-bench/results" / f"sched-plumbing-{time.strftime('%Y%m%d-%H%M%S')}"
        out.mkdir(parents=True)
        row = run_one(user, "cosmos", out, 1, switch=False)
        print(json.dumps(row, indent=1))
        return 0 if row["valid"] else 1
    if a.list:
        for n, (cmd, _) in CANDIDATES.items():
            print(f"  {n:18s} {' '.join(cmd) if cmd else '(kernel EEVDF)'}")
        return 0
    if os.geteuid() != 0:
        sys.exit("run as root: sudo tools/sched-bench.py (GravityMark itself runs as you)")
    user = pwd.getpwnam(os.environ.get("SUDO_USER") or sys.exit("start it with sudo from your session"))
    names = a.only.split(",") if a.only else list(CANDIDATES)
    for n in names:
        if n not in CANDIDATES:
            sys.exit(f"unknown candidate {n} (see --list)")
    order = []
    for r in range(a.rounds):
        order += names if r % 2 == 0 else names[::-1]
    out = Path(user.pw_dir) / ".cache/oxp3-fixes/gpu-bench/results" / f"sched-bench-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    os.chown(out, user.pw_uid, user.pw_gid)
    print(f"{len(order)} runs (~{len(order) * 3.5:.0f} min), results in {out}", flush=True)
    rows = []
    try:
        for i, name in enumerate(order, 1):
            print(f"[{i}/{len(order)}] {name} ...", flush=True)
            row = run_one(user, name, out, i)
            rows.append(row)
            print(f"   fps {row.get('fps', '-')}  1% low {row.get('low1', '-')}  hot thread "
                  f"{row.get('hot_pct', '-')} % on P {row.get('hot_on_p', '-')} %  caps {row['caps']}"
                  f"{'' if row['valid'] else '  INVALID: ' + row.get('note', '')}", flush=True)
            (out / "runs.json").write_text(json.dumps(rows, indent=1))
    finally:
        restore()
        print(f"restored {UNIT}: {sched_ops() or 'EEVDF'}", flush=True)

    print(f"\n{'scheduler':18s} {'runs':>4s} {'fps':>7s} {'1% low':>7s} {'hot thr':>7s} {'on P':>5s} {'pkg W':>6s}")
    base = None
    table = []
    for n in names:
        ok = [r for r in rows if r["sched"] == n and r["valid"]]
        if not ok:
            bad = [r.get("note", "") for r in rows if r["sched"] == n]
            print(f"{n:18s} {0:4d}  invalid: {bad[0] if bad else 'not run'}")
            continue
        m = lambda k: statistics.mean(r[k] for r in ok if r.get(k) is not None)
        table.append((n, len(ok), m("fps"), m("low1"), m("hot_pct"), m("hot_on_p"), m("pkg_w")))
    for n, k, fps, low, hot, onp, w in sorted(table, key=lambda t: -t[2]):
        if n == "cosmos":
            base = fps
    for n, k, fps, low, hot, onp, w in sorted(table, key=lambda t: -t[2]):
        rel = f" ({(fps / base - 1) * 100:+.1f} % vs cosmos)" if base and n != "cosmos" else ""
        print(f"{n:18s} {k:4d} {fps:7.2f} {low:7.2f} {hot:6.0f}% {onp:4.0f}% {w:6.1f}{rel}")
    (out / "summary.txt").write_text("see runs.json\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
