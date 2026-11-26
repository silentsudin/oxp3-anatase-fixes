# Unattended Black Myth: Wukong benchmark harness

A real UE5 / D3D12 workload for driver A/B tests, run off-screen in headless gamescope.

1. Steam must be running. Set the tool's Steam launch option once to
   `<repo>/tools/gpu-bench/wukong/wk-wrap.sh %command%` (or the copy in `~/.cache/oxp3-winre/`).
   Clear it again to run the benchmark normally - while it is set the game runs invisibly.
2. `wk-variant.sh <name> [K=V ...]` relaunches through Steam with that environment, does a warm
   run and one measured run; `wk-bench.sh <png>` does one more measured run in the same session;
   `wk-suite.sh` is an example batch.
3. Each measured run logs 120 s of per-frame times with MangoHud (started 10 s after Confirm);
   `mh-stats.py` prints frames, mean fps (frames / summed frame time) and 1 % / 0.1 % lows.

Why the pieces exist:
- The launcher has Steam DRM, so it must be started by Steam; Steam's launcher re-applies Steam's
  environment, so `wk-wrap.sh` injects gamescope's DISPLAY right before `proton`.
- gamescope's screenshots (`wk-shot.sh`, `GAMESCOPECTRL_REQUEST_SCREENSHOT`) land in the
  gamescope process's private /tmp and are read from `/proc/<pid>/root/tmp`.
- `wk-key.sh` / `wk-shot.sh` run xdotool/xprop in the `oxp3-prof` container (host network
  namespace, for gamescope's abstract X socket).
- The game reports integer fps (about 1.8 % steps); the MangoHud path resolves about 0.5 %.
  Baseline spread across five runs and three relaunches: 55.49-56.02 fps.
