# Launching a game: one wrapper for XMX, HDR, vibrance and MangoHud

`tools/oxp3-gamewrap.sh` is the single entry point that composes every graphics
feature this repository supports for a game launch. Launchers (Heroic, Steam,
the direct-Proton shortcut) no longer carry feature flags or XMX environment
variables themselves: they call the wrapper, and the wrapper decides what the
game gets.

Design rules:

* **per-game opt-in** - a feature is never enabled for a title that did not opt
  in in `~/.config/oxp3-gamewrap.toml`;
* **HDR is never forced** - the gamescope HDR flags are added only when the app
  opted in *and* the panel currently reports HDR enabled; an unknown panel state
  means "no flags";
* **no shell, no eval** - the wrapper is Python behind a small `sh` shim. The
  XMX env file is parsed as `export KEY=VALUE` data (quotes, `#` comments and
  `$HOME` only); command substitution, arbitrary variables, redirection and
  `;` make XMX fail closed instead of being executed. The final argv is a list
  executed with `execvpe`, so spaces, quotes and leading dashes are preserved;
* **pass-through** - DXVK, VKD3D, Proton and Steam variables are not touched
  except the XMX keys the env file defines; MangoHud's config file is never
  read or written.

Files: `tools/oxp3-gamewrap.sh` (entry point), `tools/lib/oxp3_gamewrap.py`
(logic), `tools/oxp3-gamewrap.toml.example` (config template),
`tests/unit/test_gamewrap.py` (offline tests).

## 1. Install and per-game enable

```sh
install -m 0644 tools/oxp3-gamewrap.toml.example ~/.config/oxp3-gamewrap.toml
tools/oxp3-gamewrap.sh --status            # explains every decision, runs nothing
```

The config is one `[defaults]` table plus one `[apps.<name>]` table per title.
Only `cyberpunk2077` is shipped opted in; `[defaults].xmx = "auto"` keeps the
legacy `OXP3_XMX=1` Heroic shortcut working before you install this file. To
enable another game:

```toml
[apps.my-game]
match = ["MyGame.exe", "*1234567*"]   # argv basename, OXP3_APP, SteamAppId, STEAM_COMPAT_APP_ID
xmx = "on"        # off | on | auto      (auto = only OXP3_XMX=1 arms it)
hdr = "on"        # off | on             (still requires the panel to report HDR on)
vibrance = "auto" # off | auto | gamescope | vkbasalt
mangohud = "keep" # keep | on | off      (environment variable only)
gamescope = "auto" # auto | on | off     (auto wraps only when a feature needs it)
```

`match` entries are case-insensitive shell wildcards compared with `--app`, the
`OXP3_APP`/`STEAM_COMPAT_APP_ID`/`SteamAppId`/`SteamGameId` values and the
basenames of every command argument. The first matching section wins.

### The decision matrix as shipped

| game | XMX | HDR | vibrance | MangoHud | gamescope |
|---|---|---|---|---|---|
| Cyberpunk 2077 (GOG `1423049311`) | **on** (validated XMX env) | **on** but gated on the panel | auto: gamescope when wrapping, else vkBasalt, else off | keep (Steam/MangoHud env decides) | auto: wraps only when HDR or the shader needs it |
| any other game without an `[apps.*]` section | auto: off unless `OXP3_XMX=1` (the legacy Heroic shortcut) | off | auto (needs a wrap or vkBasalt) | keep | auto (normally no wrap, so no change) |
| game section with `xmx = "off"` | off, even if `OXP3_XMX=1` | per section | per section | per section | per section |

### Precedence rules

| setting | highest precedence | lowest |
|---|---|---|
| XMX | `[apps.<name>].xmx = "off"` (hard veto) | `OXP3_XMX=0` > `OXP3_XMX=1` > `xmx = "on"` > `xmx = "auto"` (the default: only the override arms it) |
| HDR | `[apps.<name>].hdr = "off"` | app `hdr = "on"` **and** panel reports on **and** the wrapper starts gamescope |
| vibrance | section value | `auto` (gamescope if wrapping with the shader installed, else vkBasalt if installed, else off) |
| MangoHud | section value | `keep` = the inherited `MANGOHUD` is passed through unchanged |

`OXP3_XMX=1` is still accepted because `tools/heroic-launch.sh` exports it for
the Cyberpunk shortcut (and older checkouts only knew that variable). The
configured veto above it is deliberate: a game can be pinned off without
editing the launcher.

## 2. What each feature does

### XeSS XMX (`xmx = "on"`)

The wrapper parses `~/.config/xess-xmx/oxp3-env.sh` (written by
`tools/xmx-enable.sh`) and adds exactly the variables it defines:
`VK_DRIVER_FILES`, `ANV_CM_KERNEL_DIR`, `ANV_CM_HELPER`, `XMX_IGC_DIR`,
`VKD3D_DISABLE_EXTENSIONS`, `force_vk_vendor`,
`INTC_ALT_DRIVER_EXTENSIONS_PATH`. The 128-GRF helper
(`tools/xmx-cm-compile-nodgrf.sh`) is what the file already points at.

Before arming XMX the wrapper checks the required keys are present and, with
`verify_paths = true`, that the ICD file and the helper/IGC/shim paths exist. A
missing or unparseable env file **disables XMX with a warning** rather than
launching the game on a broken ICD; the game then runs on the system driver.
DXVK/VKD3D variables that are not in the env file are passed through untouched.

### HDR (`hdr = "on"`)

HDR is a compositor state, not a client flag, so this only applies where the
wrapper starts gamescope itself (Plasma desktop or a nested session):

```
gamescope --hdr-enabled --hdr-pq-internal-enable --hdr-sdr-content-nits 200 -- %command%
```

The flags are added only if **all** of these hold: the app opted in, the panel
currently reports HDR enabled, and the wrapper is starting gamescope. The panel
probe order is `OXP3_PANEL_HDR` (tests/headless) → `OXP3_PANEL_HDR_FILE` →
`kscreen-doctor -j` → the recorded `~/.config/kwinoutputconfig.json`. Unknown
means no flags, with the reason in `--status`.

Inside the Gamemode session the wrapper cannot add anything: gamescope is
already running as the session compositor, so HDR comes from the session
launcher flags (`docs/hw/hdr-vibrance.md` §5.2,
`tools/display/display-mode.sh gamemode` prints the exact ones). The wrapper
reports `hdr: session` and never nests a second gamescope.

Turn the panel on with `tools/display/display-mode.sh hdr on`, then the game's
`hdr = "on"` only decides whether *that* game may use it. Nothing is forced.

### Vibrance (`vibrance = "gamescope" | "vkbasalt" | "auto"`)

* `gamescope`: adds `--reshade-effect oxp3-vibrance.fx` when the shader exists
  (`tools/display/vibrance.sh install`); only possible when the wrapper starts
  gamescope.
* `vkbasalt`: adds `ENABLE_VKBASALT=1`, `VK_ADD_IMPLICIT_LAYER_PATH` and
  `VKBASALT_CONFIG_FILE` when the layer and config exist. Works inside the
  Gamemode gamescope session too.
* `auto`: gamescope when wrapping and the shader is installed, otherwise
  vkBasalt when installed, otherwise off with a reason.

The uniform values live inside the installed `.fx`
(`tools/display/vibrance.sh set <vibrance> [saturation]`); the wrapper does not
rewrite shader files.

### MangoHud (`mangohud = "keep" | "on" | "off"`)

`on` exports `MANGOHUD=1`, `off` exports `MANGOHUD=0`, `keep` leaves whatever
Steam/Heroic/the shell already exported. The wrapper never reads or writes
`~/.config/MangoHud/MangoHud.conf` - configure FPS limits, GPU stats and the
rest there or through MangoHud's own variables. That is the documented scope:
MangoHud configuration stays user-owned, the wrapper only decides whether the
overlay is requested for this launch.

## 3. Dry-run and status

```sh
# What would Cyberpunk get right now? Exact argv + env diff, nothing executes.
tools/oxp3-gamewrap.sh --dry-run --app cyberpunk2077 -- \
    "$HOME/.local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/proton" \
    run "$HOME/Games/Heroic/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe"

# Machine-readable for scripts/editors.
tools/oxp3-gamewrap.sh --status --json | python3 -m json.tool

# A plain DX12 title: no XMX, no HDR, pass-through only.
tools/oxp3-gamewrap.sh --dry-run -- /usr/bin/gamescope -- /path/to/proton run game.exe
```

Recorded shape of `--dry-run` (panel on, gamescope available, XMX env valid):

```
oxp3-gamewrap 1.0.0
config   : /var/home/holzr/.config/oxp3-gamewrap.toml
app      : cyberpunk2077  (match '1423049311')
panel HDR: on  [kscreen-doctor]
gamescope: wrap (not inside a gamescope session)
features :
  xmx       on       env /var/home/holzr/.config/xess-xmx/oxp3-env.sh (7 keys)
  hdr       added    panel HDR on (kscreen-doctor) and app opted in; gamescope flags: ...
  vibrance  added    gamescope --reshade-effect oxp3-vibrance.fx
  mangohud  keep     MANGOHUD passed through; .../MangoHud/MangoHud.conf never read or written
env diff : +7
  + ANV_CM_HELPER=/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/xmx-cm-compile-nodgrf.sh
  + VK_DRIVER_FILES=/var/home/holzr/.config/xess-xmx/icd-xmx.json
  ...
command  :
  gamescope --hdr-enabled --hdr-pq-internal-enable --hdr-sdr-content-nits 200 \
    --reshade-effect oxp3-vibrance.fx -- '/path/proton' run '/path/Cyberpunk2077.exe'
```

`--status` prints the same decisions plus the applied environment and any
`OXP3_*` overrides in effect. Exit codes: `0` ok, `1` exec failure, `2` usage or
config error. An invalid config or an unsupported XMX env-file line is never
guessed around: `--status`/`--dry-run` report it, and a normal launch refuses
with exit `2` instead of starting the game with half the intended features
(fix the config, or move it aside to launch with the built-in defaults).

## 4. Wiring

### 4.1 Heroic, Cyberpunk 2077 (the existing non-Steam shortcut)

`tools/heroic-launch.sh` (also installed as `~/heroic-launch.sh`) still forwards
Steam's app ids and routes the GOG app to `tools/cp2077-direct.sh`, which now
ends in:

```sh
exec "$gamewrap" --app cyberpunk2077 -- "$PROTON" run "$GAME" "$@"
```

So the feature set is composed once, in the wrapper. `tools/heroic-launch.sh`
also exports `OXP3_APP=cyberpunk2077` and keeps `OXP3_XMX=1` as the explicit
override/legacy fallback. Steam shortcut values stay:

| field | value |
|---|---|
| Target | `/var/home/holzr/heroic-launch.sh` |
| Launch Options | `"heroic://launch?appName=1423049311&runner=gog"` |
| Game arguments | append after the URL, e.g. `-benchmark` |

If the wrapper is ever missing, `cp2077-direct.sh` falls back to exactly the
old behaviour (`OXP3_XMX=1` sources the env file; otherwise plain Proton).

### 4.2 Heroic, other GOG/Epic titles

Per-game **Advanced → Wrapper command** (Heroic prepends it to the runner
command):

```
/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/oxp3-gamewrap.sh --app heroic:APPNAME --
```

Then add `[apps."heroic:APPNAME"]` with the features you want. Delete the old
per-game XMX `envVars` from Heroic's `GamesConfig/*.json` once the wrapper works:
duplicating them is harmless (the wrapper's values win) but defeats the single
source of truth. Keep `OXP3_XMX=0` there for an A/B fallback if you like.

### 4.3 Steam (Flatpak `org.anatase.Steam`) - exact launch option

A Flatpak launch needs the variables in the game process, because the sandbox
does not read `~/.config/environment.d`; the sandbox's `$HOME` is the host path,
so the XMX files are visible unchanged (measured in
[`hw/flatpak-xmx.md`](hw/flatpak-xmx.md)). Deploy the shim and the Flatpak env
file once with the companion tool:

```sh
tools/oxp3-flatpak-xmx.sh install --appid <steam-appid>
tools/oxp3-flatpak-xmx.sh status --probe-sandbox   # VISIBLE/WRITABLE + OCLOC-RUNS ok
```

Then, per game, **Properties → Launch Options** (one line; keep `%command%`
last). `OXP3_XMX_ENV_FILE` makes the wrapper parse the Flatpak env file instead
of the host `oxp3-env.sh`; the wrapper still owns HDR/vibrance/MangoHud and the
argv:

```
OXP3_XMX_ENV_FILE=/var/home/holzr/.config/xess-xmx/flatpak-env.sh /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/oxp3-gamewrap.sh -- %command%
```

Add a section with `match = ["*1091500*"]` and `xmx = "on"` (the appid comes from `SteamAppId`).
The raw equivalent, without the wrapper, is
`tools/oxp3-flatpak-xmx.sh launch-options`; use that only for titles where you
do not want HDR/vibrance/MangoHud composition. If a title already has
`gamemoderun`, keep it after the wrapper's `--` (it becomes an argv token, not a
shell fragment).

Caveats: `python3` exists in the sandbox (`/usr/bin/python3`, verified), but the
wrapper path is checked at launch time; HDR inside Gamemode remains the
session's gamescope flags, so the wrapper reports `hdr: session` there. The old
per-game `envVars` approach is no longer needed for XMX once the Flatpak file
exists; keep it only as a fallback.

### 4.4 Steam (host install) and other launchers

Same launch option on a host Steam install:

```
/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/oxp3-gamewrap.sh -- %command%
```

A plain command works too, which is the form the direct-Proton shortcut uses:

```sh
tools/oxp3-gamewrap.sh -- "$PROTON" run "$GAME.exe" "$@"
```

### 4.5 Steam non-Steam shortcuts

| shortcut | Target | Launch Options |
|---|---|---|
| Cyberpunk direct (no Heroic/umu) | `/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/cp2077-direct.sh` | `-benchmark` (anything else the game takes) |
| Heroic app | `/var/home/holzr/heroic-launch.sh` | `"heroic://launch?appName=1423049311&runner=gog"` |
| any other game/launcher | `/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/tools/oxp3-gamewrap.sh` | `--app <section> -- /path/to/program [args]` |

Non-Steam shortcuts have no `%command%`; anything in the options field is passed
to the target as arguments, which is exactly what the third row relies on.

## 5. How the Decky status reflects it

The wrappers set the *real process environment* before `exec`, so the existing
Decky indicators read the same values the game got:

* `xmx.icd`, `xmx.helper`, `xmx.igc`, `xmx.shim`, `xmx.vkd3d` come from
  `/proc/<pid>/environ` when the loader can read it, otherwise from the
  configured launcher environment. When the wrapper arms XMX they report `ok`;
  when it refuses XMX (missing key/path) and nothing else supplies the variables,
  they report `warn`/`fail`, which is the intended early warning;
* MangoHud (and the vkBasalt vibrance route) is environment-only, so a
  `MANGOHUD=1`/`ENABLE_VKBASALT=1` set by the wrapper is visible in the panel's
  `game.env` data; a gamescope `--reshade-effect` flag is argv and therefore not
  visible. There is no dedicated Decky row for either today;
* HDR/vibrance state is not a Decky indicator: the panel does not probe the
  compositor. For those use `tools/oxp3-gamewrap.sh --status`,
  `tools/display/display-mode.sh status` or `tools/oxp3-verify.sh` (`dsp.*`
  rows) in a terminal.

`--status --json` is the stable machine-readable view if a future plugin row
wants to show "what this game will actually get".

## 6. Verification

```sh
# Offline decision table, argv safety, HDR gating, config veto (45 tests).
tools/repo-test.sh --only gamewrap
python3 -m unittest discover -s tests/unit -p test_gamewrap.py -q

# No side effects, no game launched:
tools/oxp3-gamewrap.sh --dry-run -- /bin/echo hello
tools/oxp3-gamewrap.sh --status

# Real launch through the launcher path (game starts only if you run this):
OXP3_PANEL_HDR=off tools/oxp3-gamewrap.sh --status --app cyberpunk2077
```

`tools/repo-test.sh` also compiles the Python, checks the shell syntax and runs
the whole `tests/unit` suite, so the wrapper is covered by `make test`.

## 7. Open points

* **Flatpak Steam is not exercised end to end here.** The wrappers are wired
  and documented; the sandbox visibility and `ocloc` execution were measured,
  but no Flatpak Proton prefix exists on this host yet, so no in-game trace has
  been produced (see `hw/flatpak-xmx.md`). The raw launch-options line from
  `tools/oxp3-flatpak-xmx.sh launch-options` remains the fallback if a future
  sandbox update blocks the wrapper's `python3`.
* **Session HDR stays root-owned.** The wrapper deliberately cannot flip HDR on
  for a session it did not start; the Gamemode flags are still the
  `/usr/libexec/anatase-gamemode` edit from `docs/hw/hdr-vibrance.md` §5.2.
* **The panel probe is a heuristic.** Under Gamemode there is no KWin to ask,
  so the wrapper only trusts `kscreen-doctor`/the recorded KWin config outside a
  gamescope session; inside one it reports `hdr: session` instead of guessing.
* **vkBasalt is not installed on this image.** `vibrance = "vkbasalt"` reports
  the missing layer instead of failing the launch; install it as documented in
  `docs/hw/hdr-vibrance.md` §5.3.
* **No automatic config install.** The example is copied by the operator so an
  existing per-game file is never overwritten.
* **Only Cyberpunk is shipped opted in.** Other titles need an `[apps.*]`
  section; "known compatible" is a config fact, not a filename list.
* **`hdr_args` can be overridden** in `[gamescope]`; the defaults are exactly
  the two flags plus `200` nits from `docs/hw/hdr-vibrance.md`.
