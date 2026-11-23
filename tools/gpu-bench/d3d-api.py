#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measure one game under different graphics APIs: D3D11 (DXVK), D3D12 (VKD3D-Proton)
and native Vulkan.

Why a separate tool from gpu-bench.py: that one runs headless synthetic workloads, which
cannot answer "is DXVK-on-ANV slower than VKD3D/native on the same content". This one
launches a Steam title with the API flags the title understands, records frametimes with
MangoHud (no overlay, CSV to disk), and cuts the recording to the window the operator
marks, so the numbers are per-scene and repeatable without pixel automation.

Every run writes results/d3d-api-<stamp>/<title>-<api>.json with the fps/frametime
summary, the GPU/CPU load MangoHud recorded (which says whether the API path is CPU-bound
or GPU-bound), the DXVK/VKD3D logs, and the stack versions - so two runs are comparable
and a difference can be attributed to the API path instead of to the driver or the game
build.

Usage:
    tools/gpu-bench/d3d-api.py titles
    tools/gpu-bench/d3d-api.py run --title witcher3 --api dx11 [--note "..."] [--dry-run]
    tools/gpu-bench/d3d-api.py report <dir-or-json> ...
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STEAM = Path.home() / ".local/share/Steam"
COMMON = STEAM / "steamapps/common"
LOGS = Path.home() / ".cache/oxp3-fixes/gpu-bench/results"

# Per-title launch flags. `exe` is used only to notice the game came up and to stop it
# again; the API flags are the ones the title itself documents.
TITLES = {
    # Only titles actually installed on this machine: Satisfactory, The Witcher 3, Hogwarts
    # Legacy and No Man's Sky had install directories but no appmanifest, so they cannot be
    # launched by appid. Add a title here (appid, exe basename, per-API flags) after installing
    # one that exposes DX11 and DX12 in the same content.
    "palworld": {
        "appid": "1623730",
        "name": "Palworld",
        "exe": ["Palworld-Win64-Shipping.exe"],
        # UE5.1 keeps an SM5/DX11 path; if the game ignores -dx11 the run still produces
        # VKD3D logs instead of DXVK ones, which the result records.
        "apis": {"dx11": ["-dx11"], "dx12": ["-dx12"]},
    },
    "talos2": {
        "appid": "835960",
        "name": "The Talos Principle 2",
        "exe": ["Talos2.exe"],
        "apis": {"dx12": ["-dx12"], "vk": ["-vulkan"]},
    },
    "blackmyth": {
        "appid": "3132990",
        "name": "Black Myth Wukong Benchmark Tool",
        "exe": ["b1-Win64-Shipping.exe"],
        # built-in benchmark loop: the closest thing to a repeatable scene here
        "apis": {"dx12": ["-dx12"]},
    },
    "eldenring": {
        "appid": "1245620",
        "name": "ELDEN RING",
        "exe": ["eldenring.exe"],
        "apis": {"dx12": ["-dx12"]},
    },
}

# no metric names: MangoHud logs its available columns by default, and naming one the
# driver does not expose (gpu_load on this build) makes it discard the whole config
MANGOHUD_KEYS = ""
INTERVAL_MS = 100


def out(msg: str = "") -> None:
    print(msg, flush=True)


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def stack_versions() -> dict:
    v = {}
    try:
        v["kernel"] = sh(["uname", "-r"]).stdout.strip()
    except OSError:
        pass
    p = sh(["rpm", "-q", "--queryformat", "%{VERSION}-%{RELEASE}", "mesa-vulkan-drivers"])
    v["mesa_rpm"] = p.stdout.strip() or None
    f = Path("/usr/lib64/libvulkan_intel.so")
    if f.exists():
        s = sh(["strings", str(f)])
        m = re.search(r"Mesa \d+\.\d+\.\d+[^\n]*", s.stdout)
        v["mesa_driver"] = m.group(0) if m else None
        v["mesa_cm_injection"] = "ANV_CM_HELPER" in s.stdout
    for key, pat in (("dxvk", "files/lib*/wine/dxvk/version"),
                     ("vkd3d", "files/lib*/wine/vkd3d-proton/version")):
        hits = sorted(STEAM.glob(f"compatibilitytools.d/*/{pat}")) + \
            sorted(COMMON.glob(f"Proton*/{pat}"))
        v[key] = {}
        for h in hits:
            try:
                v[key][h.parts[-4] if "compatibilitytools.d" in str(h) else h.parts[-3]] = \
                    h.read_text().strip().split()[-1]
            except OSError:
                continue
    return v


def game_running(exes: list[str]) -> list[int]:
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if any(e.lower() in cmd.lower() for e in exes):
            pids.append(int(d.name))
    return pids


def detect_mangohud_csv(d: Path) -> Path | None:
    # output_folder: MangoHud writes its own timestamped name there
    cands = sorted(d.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True) + \
        sorted(Path.home().glob("*.csv"))
    for c in cands:
        try:
            head = c.read_text(errors="replace")[:400]
        except OSError:
            continue
        if "frametime" in head:
            return c
    return None


def parse_mangohud(csv: Path, window_s: float) -> dict:
    rows, header = [], None
    for line in csv.read_text(errors="replace").splitlines():
        if header is None:
            if line.startswith("fps,"):
                header = [h.strip() for h in line.split(",")]
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            row = {header[i]: float(parts[i]) for i in range(min(len(header), len(parts)))
                   if parts[i].strip() not in ("", "NA")}
        except ValueError:
            continue
        rows.append(row)
    if not rows:
        return {"samples": 0}
    # Take the tail: the measured window is the end of the run (the operator marks it, then quits
    # the game), and at a fixed log_interval the last window_s seconds are that many rows.
    # MangoHud's own `elapsed` column is not used on purpose: 0.8.4 writes it in *nanoseconds*
    # (101261773 for 0.1 s), which silently turns a seconds-based window into "everything".
    n = max(1, int(round(window_s * 1000 / INTERVAL_MS)))
    win = rows[-n:] if len(rows) > n else rows
    ft = [r["frametime"] for r in win if r.get("frametime")]
    fps = [r["fps"] for r in win if r.get("fps")]
    if not ft:
        return {"samples": len(win)}

    def pct(vals: list[float], q: float) -> float:
        s = sorted(vals)
        return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]

    def avg(*keys: str) -> float | None:
        # MangoHud's CSV header has used both cpu_load and cpuload across versions
        for key in keys:
            vals = [r[key] for r in win if r.get(key)]
            if vals:
                return round(sum(vals) / len(vals), 2)
        return None

    return {
        "samples": len(win),
        "window_s": round(window_s, 2),
        "fps_avg": round(sum(fps) / len(fps), 2) if fps else None,
        "frametime_ms_avg": round(sum(ft) / len(ft), 3),
        "frametime_ms_1pct_low": round(pct(ft, 0.99), 3),
        "frametime_ms_99pct": round(pct(ft, 0.99), 3),
        "fps_1pct_low": round(1000.0 / pct(ft, 0.99), 2),
        "cpu_load_avg": avg("cpu_load", "cpuload"),
        "gpu_load_avg": avg("gpu_load", "gpuload"),
        "gpu_clock_avg": avg("gpu_core_clock"),
        "cpu_mhz_avg": avg("cpu_mhz"),
    }


def cmd_titles(_a) -> int:
    out(f"{'title':14s} {'appid':9s} {'apis':22s} exe")
    for key, t in TITLES.items():
        exe = ", ".join(t["exe"])
        out(f"{key:14s} {t['appid']:9s} {','.join(t['apis']):22s} {exe}")
    return 0


def cmd_run(a) -> int:
    t = TITLES.get(a.title)
    if t is None:
        out(f"unknown title {a.title!r}; known: {', '.join(TITLES)}")
        return 2
    if a.api not in t["apis"]:
        out(f"{a.title} has no {a.api} profile (has: {', '.join(t['apis'])})")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    res = Path(a.out or LOGS / f"d3d-api-{stamp}") / f"{a.title}-{a.api}"
    res.mkdir(parents=True, exist_ok=True)
    csv = res / "mangohud.csv"
    env = os.environ.copy()
    env.update({
        "MANGOHUD": "1",
        "MANGOHUD_CONFIG": mangohud_config(res),
        "DXVK_LOG_LEVEL": "info",
        "DXVK_LOG_PATH": str(res),
        "DXVK_HUD": "0",
        "DXVK_STATE_CACHE": "1",
        "VKD3D_DEBUG": "warn",
        "VKD3D_SHADER_DEBUG": "none",
        "STEAM_COMPAT_APP_ID": t["appid"],
    })
    cmd = ["steam", "-applaunch", t["appid"], *t["apis"][a.api]]
    out(f"== {t['name']} / {a.api}")
    out(f"   launch: {' '.join(cmd)}")
    out(f"   result: {res}")
    out(f"   mangohud: {MANGOHUD_CONFIG_PREVIEW}")
    if a.dry_run:
        return 0

    logline = {"title": a.title, "api": a.api, "note": a.note, "appid": t["appid"],
               "flags": t["apis"][a.api], "started": datetime.now(timezone.utc).isoformat()}
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out("   waiting for the game to come up...")
    deadline = time.time() + a.timeout
    pids: list[int] = []
    while time.time() < deadline:
        pids = game_running(t["exe"])
        if pids:
            break
        time.sleep(2)
    if not pids:
        out("   game did not start within the timeout; not recording")
        return 3

    out("")
    out("   Load your benchmark scene/save.")
    out("   Press Enter when the measured scene starts, then Enter again when it ends.")
    input("   [Enter] start the window: ")
    t0 = time.time()
    input("   [Enter] end the window: ")
    t1 = time.time()
    out(f"   window {t1 - t0:.1f}s")

    # MangoHud writes its CSV when the process exits cleanly: killing it (SIGTERM) loses the
    # whole log, which is why the first measurements produced no file at all.
    if a.kill:
        for pid in game_running(t["exe"]):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    else:
        out("   Quit the game normally now (Steam > Exit, Alt+F4) - MangoHud only writes its log")
        out(f"   on a clean exit. Waiting up to {a.quit_timeout}s for it to close...")
        quit_deadline = time.time() + a.quit_timeout
        while time.time() < quit_deadline and game_running(t["exe"]):
            time.sleep(2)
        if game_running(t["exe"]):
            out("   still running: terminating it, the log may be missing (use a clean exit)")
            for pid in game_running(t["exe"]):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    time.sleep(3)
    found = detect_mangohud_csv(res)
    summary = parse_mangohud(found, t1 - t0) if found else {"error": "no mangohud csv"}
    if found and found != csv:
        shutil.copy2(found, csv)
    logs = sorted(p.name for p in res.iterdir()
                  if p.suffix in (".log", ".txt") or "dxvk" in p.name.lower() or "vkd3d" in p.name.lower())
    if not found:
        out("   no MangoHud CSV: it only writes on a clean exit, and the game did not exit by itself")
    logline.update({
        "finished": datetime.now(timezone.utc).isoformat(),
        "stack": stack_versions(),
        "mangohud_csv": found.name if found else None,
        "logs": logs,
        "summary": summary,
    })
    (res / "result.json").write_text(json.dumps(logline, indent=2) + "\n")
    out(f"   {json.dumps(summary)}")
    return 0


def cmd_report(a) -> int:
    rows = []
    for p in map(Path, a.paths):
        files = sorted(p.glob("*/result.json")) if p.is_dir() else [p]
        for f in files:
            d = json.loads(f.read_text())
            s = d.get("summary") or {}
            rows.append({
                "title": d.get("title"), "api": d.get("api"),
                "note": d.get("note") or "",
                "fps": s.get("fps_avg"), "low1": s.get("fps_1pct_low"),
                "cpu": s.get("cpu_load_avg"), "gpu": s.get("gpu_load_avg"),
            })
    if not rows:
        out("no results")
        return 1
    best = {}
    for r in rows:
        if r["fps"]:
            best[r["title"]] = max(best.get(r["title"], 0), r["fps"])
    out(f"{'title':14s} {'api':5s} {'fps':>7s} {'1%low':>7s} {'cpu%':>6s} {'gpu%':>6s} {'vs best':>8s}  note")
    for r in sorted(rows, key=lambda x: (x["title"] or "", -(x["fps"] or 0))):
        rel = f"{100 * r['fps'] / best[r['title']]:.1f}%" if r["fps"] and best.get(r["title"]) else "-"
        f = lambda v: "-" if v is None else f"{v:.1f}"
        out(f"{r['title'] or '-':14s} {r['api'] or '-':5s} {f(r['fps']):>7s} {f(r['low1']):>7s} "
            f"{f(r['cpu']):>6s} {f(r['gpu']):>6s} {rel:>8s}  {r['note']}")
    return 0


def mangohud_config(folder: Path) -> str:
    """MangoHud 0.8.4: logs go to output_folder (output_file is deprecated and ignored), and
    autostart_log needs log_duration (0 = until the process exits)."""
    return (f"output_folder={folder},log_interval={INTERVAL_MS},"
            f"autostart_log=1,log_duration=0")


MANGOHUD_CONFIG_PREVIEW = "output_folder=<result dir>,log_interval=100,autostart_log=1,log_duration=0"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("titles", help="list the titles with API profiles")
    s.set_defaults(func=cmd_titles)

    s = sub.add_parser("run", help="launch a title under one API and record a window")
    s.add_argument("--title", required=True)
    s.add_argument("--api", required=True)
    s.add_argument("--note", default="", help="what scene/settings were used")
    s.add_argument("--out", default=None)
    s.add_argument("--timeout", type=int, default=300, help="seconds to wait for the game")
    s.add_argument("--kill", action="store_true",
                   help="terminate the game instead of asking for a clean exit (loses the log)")
    s.add_argument("--quit-timeout", type=int, default=180,
                   help="seconds to wait for the game to close after the window")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("report", help="compare result directories")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_report)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
