# SPDX-License-Identifier: MIT
"""Offline tests for tools/oxp3-gamewrap.sh and its decision core.

Everything runs against a synthetic HOME, config, panel-state file and stub
commands: no game, no gamescope, no compositor, no root and no network. The
point of these tests is the *decision table* - which env keys and argv tokens
one launch gets - and the safety rules (no eval of the XMX env file, HDR never
forced, argv preserved verbatim).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

import support

GW = support.load("oxp3_gamewrap", "lib/oxp3_gamewrap.py")
WRAP = support.REPO / "tools" / "oxp3-gamewrap.sh"
EXAMPLE = support.REPO / "tools" / "oxp3-gamewrap.toml.example"

CYBERPUNK = """
[apps.cyberpunk2077]
match = ["Cyberpunk2077.exe", "*1423049311*"]
xmx = "on"
hdr = "on"
vibrance = "auto"
gamescope = "auto"
"""

BASE = "schema = 1\n"


class GamewrapTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.home = self.tree.mkdir("home")
        self.bin = self.tree.mkdir("bin")
        self.cfg_dir = self.tree.mkdir("home/.config")
        self.data_dir = self.tree.mkdir("home/.local/share")
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "XDG_CONFIG_HOME": str(self.cfg_dir),
            "XDG_DATA_HOME": str(self.data_dir),
            "LC_ALL": "C",
        }

    # -- helpers ------------------------------------------------------------
    def env_with(self, **extra):
        env = dict(self.env)
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def write_config(self, text: str):
        path = self.cfg_dir / "oxp3-gamewrap.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return GW.read_config(str(path))

    def plan(self, argv, env=None, app=None, config=None):
        if config is None:
            cfg, _ = self.write_config(BASE + CYBERPUNK)
        else:
            cfg, _ = self.write_config(BASE + config)
        return GW.build_plan(cfg, list(argv), env or self.env, self.home, app)

    def stub_gamescope(self):
        path = self.bin / "gamescope"
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def xmx_fixture(self, *, drop=None, missing_path=False):
        # Deliberately the configured default path: the wrapper must find the
        # file written by tools/xmx-enable.sh without any per-test config.
        base = self.home / ".config" / "xess-xmx"
        base.mkdir(parents=True, exist_ok=True)
        icd = base / "icd-xmx.json"
        icd.write_text("{}\n", encoding="utf-8")
        helper = base / "helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        igc = base / "igc"
        igc.mkdir(exist_ok=True)
        shim = base / "shim"
        shim.mkdir(exist_ok=True)
        values = {
            "VK_DRIVER_FILES": icd,
            "ANV_CM_HELPER": helper,
            "XMX_IGC_DIR": igc,
            "INTC_ALT_DRIVER_EXTENSIONS_PATH": shim,
        }
        if missing_path:
            values["VK_DRIVER_FILES"] = base / "does-not-exist.json"
        lines = [
            "export VK_DRIVER_FILES=%s" % values["VK_DRIVER_FILES"],
            "export ANV_CM_HELPER=%s" % helper,
            "export XMX_IGC_DIR=%s" % igc,
            "export INTC_ALT_DRIVER_EXTENSIONS_PATH=%s" % shim,
            "export VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer",
            "export force_vk_vendor=0",
        ]
        if drop:
            lines = [line for line in lines if drop not in line]
        envf = base / "oxp3-env.sh"
        envf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return envf, icd

    def proc_env(self, path_extra=None):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("OXP3_", "STEAM_", "GAMESCOPE", "GAMESCOPECTRL")):
                env.pop(key)
        env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.cfg_dir),
            "XDG_DATA_HOME": str(self.data_dir),
            "LC_ALL": "C",
        })
        python_dir = os.path.dirname(shutil.which("python3") or sys.executable)
        parts = [str(self.bin)]
        if path_extra:
            parts.append(str(path_extra))
        parts += [python_dir, "/usr/bin", "/bin"]
        env["PATH"] = ":".join(parts)
        return env

    def run_wrap(self, *args, env=None):
        return subprocess.run(
            [str(WRAP), *map(str, args)], capture_output=True, text=True,
            timeout=60, env=env or self.proc_env())

    # -- config and env-file parsing ---------------------------------------
    def test_example_config_parses_and_opts_cyberpunk_in(self):
        cfg, exists = GW.read_config(str(EXAMPLE))
        self.assertTrue(exists)
        self.assertEqual(cfg["schema"], 1)
        self.assertIn("cyberpunk2077", cfg["apps"])
        self.assertEqual(cfg["apps"]["cyberpunk2077"]["xmx"], "on")
        self.assertEqual(cfg["apps"]["cyberpunk2077"]["hdr"], "on")
        self.assertEqual(cfg["gamescope"]["hdr_args"][:2],
                         ["--hdr-enabled", "--hdr-pq-internal-enable"])

    def test_invalid_config_enum_is_rejected(self):
        with self.assertRaises(GW.ConfigError):
            self.write_config(BASE + '[defaults]\nhdr = "sometimes"\n')

    def test_unknown_app_key_is_rejected(self):
        with self.assertRaises(GW.ConfigError):
            self.write_config(BASE + '[apps.x]\nmatch = ["x"]\nhdrr = "on"\n')

    def test_env_file_parses_quotes_comments_and_home(self):
        envf = self.tree.write("e.sh", "\n".join([
            "export A=plain",
            "export B='single $HOME $(not executed)'",
            'export C="$HOME/with space"',
            "export D=~/suffix",
            "export E=value # trailing comment",
            "",
        ]))
        parsed = GW.parse_env_file(str(envf), self.home, self.env)
        self.assertEqual(parsed["A"], "plain")
        self.assertEqual(parsed["B"], "single $HOME $(not executed)")
        self.assertEqual(parsed["C"], str(self.home / "with space"))
        self.assertEqual(parsed["D"], str(self.home / "suffix"))
        self.assertEqual(parsed["E"], "value")

    def test_env_file_rejects_command_substitution(self):
        for text in ("export X=$(id)\n", "export X=`id`\n"):
            envf = self.tree.write("bad.sh", text)
            with self.assertRaises(GW.ConfigError):
                GW.parse_env_file(str(envf), self.home, self.env)

    def test_env_file_rejects_other_variables_and_sourcing(self):
        for text in ("export X=$USER\n", "source /etc/profile\n",
                     "export X=one; rm -rf /\n", "export X=a \\\n"):
            envf = self.tree.write("bad.sh", text)
            with self.assertRaises(GW.ConfigError):
                GW.parse_env_file(str(envf), self.home, self.env)

    # -- XMX ---------------------------------------------------------------
    def test_xmx_opt_in_composes_env_and_keeps_dxvk(self):
        envf, icd = self.xmx_fixture()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(
            cfg, ["proton", "run", "Cyberpunk2077.exe"], {},
            self.home, "cyberpunk2077")
        self.assertEqual(plan.features["xmx"].state, "on")
        self.assertEqual(plan.added_env["VK_DRIVER_FILES"], str(icd))
        self.assertEqual(plan.added_env["VKD3D_DISABLE_EXTENSIONS"],
                         "VK_EXT_descriptor_buffer")
        self.assertIn("force_vk_vendor", plan.added_env)

    def test_xmx_does_not_touch_passthrough_env(self):
        self.xmx_fixture()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        env = dict(self.env, DXVK_HUD="fps", VKD3D_DEBUG="warn")
        plan = GW.build_plan(
            cfg, ["proton"], env, self.home, "cyberpunk2077")
        self.assertEqual(plan.env["DXVK_HUD"], "fps")
        self.assertEqual(plan.env["VKD3D_DEBUG"], "warn")
        changed = {item["key"] for item in plan.env_diff}
        self.assertNotIn("DXVK_HUD", changed)
        self.assertNotIn("VKD3D_DEBUG", changed)

    def test_xmx_app_off_vetoes_env_override(self):
        self.xmx_fixture()
        cfg, _ = self.write_config(BASE + """
[apps.cyberpunk2077]
match = ["*"]
xmx = "off"
""")
        plan = GW.build_plan(cfg, ["proton"], dict(self.env, OXP3_XMX="1"),
                             self.home, "cyberpunk2077")
        self.assertEqual(plan.features["xmx"].state, "off")
        self.assertIn("veto", plan.features["xmx"].detail)
        self.assertNotIn("VK_DRIVER_FILES", plan.added_env)

    def test_xmx_env_override_zero_disables(self):
        self.xmx_fixture()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(cfg, ["proton"], dict(self.env, OXP3_XMX="0"),
                             self.home, "cyberpunk2077")
        self.assertEqual(plan.features["xmx"].state, "off")
        self.assertIn("OXP3_XMX=0", plan.features["xmx"].detail)

    def test_xmx_missing_required_key_is_refused(self):
        self.xmx_fixture(drop="XMX_IGC_DIR")
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home, "cyberpunk2077")
        self.assertEqual(plan.features["xmx"].state, "off")
        self.assertIn("missing required keys", plan.features["xmx"].detail)
        self.assertNotIn("VK_DRIVER_FILES", plan.added_env)

    def test_xmx_accepts_a_windows_prefix_path_without_host_check(self):
        base = self.home / ".config" / "xess-xmx"
        envf, _ = self.xmx_fixture()
        envf.write_text(
            envf.read_text(encoding="utf-8").replace(
                "export INTC_ALT_DRIVER_EXTENSIONS_PATH=%s" % (base / "shim"),
                "export INTC_ALT_DRIVER_EXTENSIONS_PATH=C:/windows/system32/"
                "driverstore/filerepository/igd_faux.inf_1"),
            encoding="utf-8")
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home, "cyberpunk2077")
        self.assertEqual(plan.features["xmx"].state, "on")
        self.assertEqual(plan.added_env["INTC_ALT_DRIVER_EXTENSIONS_PATH"],
                         "C:/windows/system32/driverstore/filerepository/"
                         "igd_faux.inf_1")

    def test_xmx_missing_driver_path_is_refused(self):
        self.xmx_fixture(missing_path=True)
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home, "cyberpunk2077")
        self.assertEqual(plan.features["xmx"].state, "off")
        self.assertIn("does not exist", plan.features["xmx"].detail)

    def test_xmx_default_off_stays_off(self):
        cfg, _ = self.write_config(BASE + '[defaults]\nxmx = "off"\n')
        plan = GW.build_plan(cfg, ["proton"],
                             dict(self.env, OXP3_XMX="1"), self.home, None)
        self.assertEqual(plan.features["xmx"].state, "off")

    def test_xmx_legacy_override_works_before_a_config_exists(self):
        self.xmx_fixture()
        cfg = GW.validate_config(GW.builtin_config())
        plan = GW.build_plan(cfg, ["proton"], dict(self.env, OXP3_XMX="1"),
                             self.home, None)
        self.assertEqual(plan.features["xmx"].state, "on")

    def test_xmx_auto_without_override_stays_off(self):
        self.xmx_fixture()
        cfg = GW.validate_config(GW.builtin_config())
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home, None)
        self.assertEqual(plan.features["xmx"].state, "off")
        self.assertIn("needs OXP3_XMX=1", plan.features["xmx"].detail)

    # -- HDR gating --------------------------------------------------------
    def test_hdr_flags_only_when_panel_on_and_app_opted_in(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(
            cfg, ["proton", "run", "x.exe"],
            dict(self.env, OXP3_PANEL_HDR="on"), self.home, "cyberpunk2077")
        self.assertTrue(plan.gamescope["wrap"])
        self.assertEqual(plan.argv[0], "gamescope")
        self.assertIn("--hdr-enabled", plan.argv)
        self.assertIn("--hdr-pq-internal-enable", plan.argv)
        idx = plan.argv.index("--hdr-sdr-content-nits")
        self.assertEqual(plan.argv[idx + 1], "200")
        self.assertEqual(plan.features["hdr"].state, "added")

    def test_hdr_not_added_when_panel_reports_off(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_PANEL_HDR="off"),
            self.home, "cyberpunk2077")
        self.assertFalse(plan.gamescope["wrap"])
        self.assertNotIn("--hdr-enabled", plan.argv)
        self.assertEqual(plan.features["hdr"].state, "skipped")
        self.assertIn("not forcing", plan.features["hdr"].detail)

    def test_hdr_not_added_when_panel_state_unknown(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home,
                             "cyberpunk2077")
        self.assertEqual(plan.panel.state, "unknown")
        self.assertNotIn("--hdr-enabled", plan.argv)
        self.assertIn("refusing to force", plan.features["hdr"].detail)

    def test_hdr_is_session_managed_inside_gamescope(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        env = dict(self.env, OXP3_PANEL_HDR="on",
                   STEAM_GAMESCOPE_HDR_SUPPORTED="1")
        plan = GW.build_plan(cfg, ["proton"], env, self.home, "cyberpunk2077")
        self.assertTrue(plan.gamescope["inside"])
        self.assertFalse(plan.gamescope["wrap"])
        self.assertNotIn("--hdr-enabled", plan.argv)
        self.assertEqual(plan.features["hdr"].state, "session")
        self.assertTrue(any("session flags" in note for note in plan.notes))

    def test_hdr_app_opt_out_wins_over_panel(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE + """
[apps.cyberpunk2077]
match = ["*"]
hdr = "off"
gamescope = "auto"
""")
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_PANEL_HDR="on"),
            self.home, "cyberpunk2077")
        self.assertEqual(plan.features["hdr"].state, "off")
        self.assertNotIn("--hdr-enabled", plan.argv)

    def test_hdr_args_are_configurable(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE + CYBERPUNK + """
[gamescope]
hdr_args = ["--hdr-enabled", "--hdr-sdr-content-nits", "300"]
""")
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_PANEL_HDR="on"),
            self.home, "cyberpunk2077")
        self.assertIn("300", plan.argv)
        self.assertNotIn("--hdr-pq-internal-enable", plan.argv)

    def test_panel_state_file_override(self):
        state = self.tree.write("panel", "on\n")
        cfg, _ = self.write_config(BASE)
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_PANEL_HDR_FILE=str(state)),
            self.home)
        self.assertEqual(plan.panel.state, "on")
        self.assertEqual(plan.panel.source, "OXP3_PANEL_HDR_FILE")

    def test_recorded_kwin_state_is_a_fallback(self):
        self.tree.write("home/.config/kwinoutputconfig.json", json.dumps(
            [{"data": [{"connectorName": "eDP-1", "highDynamicRange": True},
                       {"connectorName": "DP-1", "highDynamicRange": False}]}]))
        cfg, _ = self.write_config(BASE)
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home)
        self.assertEqual(plan.panel.state, "on")
        self.assertEqual(plan.panel.source, "kwinoutputconfig")

    # -- vibrance ----------------------------------------------------------
    def test_vibrance_uses_gamescope_reshade_when_wrapping(self):
        self.stub_gamescope()
        self.tree.write(
            "home/.local/share/gamescope/reshade/Shaders/oxp3-vibrance.fx",
            "// fx\n")
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_PANEL_HDR="on"),
            self.home, "cyberpunk2077")
        self.assertEqual(plan.features["vibrance"].state, "added")
        self.assertIn("--reshade-effect", plan.argv)
        idx = plan.argv.index("--reshade-effect")
        self.assertEqual(plan.argv[idx + 1], "oxp3-vibrance.fx")

    def test_vibrance_falls_back_to_vkbasalt(self):
        self.tree.write("home/.local/share/oxp3-vkbasalt/layer/libvkbasalt.so", "")
        self.tree.write("home/.config/oxp3-vkbasalt.conf", "effects = oxp3\n")
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_PANEL_HDR="off"),
            self.home, "cyberpunk2077")
        self.assertEqual(plan.features["vibrance"].state, "added")
        self.assertEqual(plan.added_env["ENABLE_VKBASALT"], "1")
        self.assertIn("VK_ADD_IMPLICIT_LAYER_PATH", plan.added_env)
        self.assertIn("VKBASALT_CONFIG_FILE", plan.added_env)

    def test_vibrance_auto_with_nothing_installed_is_off(self):
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home,
                             "cyberpunk2077")
        self.assertEqual(plan.features["vibrance"].state, "off")
        self.assertIn("auto found nothing", plan.features["vibrance"].detail)

    # -- MangoHud ----------------------------------------------------------
    def test_mangohud_is_env_only_and_never_writes_config(self):
        conf = self.cfg_dir / "MangoHud" / "MangoHud.conf"
        cfg, _ = self.write_config(BASE + """
[apps.cyberpunk2077]
match = ["*"]
mangohud = "on"
""")
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home,
                             "cyberpunk2077")
        self.assertEqual(plan.added_env["MANGOHUD"], "1")
        self.assertFalse(conf.exists(), "the wrapper must not create a MangoHud config")

    def test_mangohud_keep_preserves_inherited_value(self):
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        env = dict(self.env, MANGOHUD="1")
        plan = GW.build_plan(cfg, ["proton"], env, self.home, "cyberpunk2077")
        self.assertEqual(plan.features["mangohud"].state, "keep")
        self.assertNotIn("MANGOHUD", {item["key"] for item in plan.env_diff})

    def test_mangohud_off_sets_zero(self):
        cfg, _ = self.write_config(BASE + """
[apps.cyberpunk2077]
match = ["*"]
mangohud = "off"
""")
        plan = GW.build_plan(cfg, ["proton"], dict(self.env, MANGOHUD="1"),
                             self.home, "cyberpunk2077")
        self.assertEqual(plan.added_env["MANGOHUD"], "0")

    # -- gamescope shapes --------------------------------------------------
    def test_gamescope_auto_does_not_wrap_without_a_feature(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE)
        plan = GW.build_plan(cfg, ["proton", "run"], self.env, self.home)
        self.assertFalse(plan.gamescope["wrap"])
        self.assertEqual(plan.argv, ["proton", "run"])

    def test_gamescope_on_wraps_without_hdr(self):
        self.stub_gamescope()
        cfg, _ = self.write_config(BASE + """
[defaults]
gamescope = "on"
[gamescope]
width = 1920
height = 1200
refresh = 144
""")
        plan = GW.build_plan(cfg, ["proton", "run"], self.env, self.home)
        self.assertTrue(plan.gamescope["wrap"])
        self.assertEqual(plan.argv[:7], ["gamescope", "-W", "1920", "-H", "1200",
                                         "-r", "144"])
        self.assertEqual(plan.argv[7], "--")
        self.assertEqual(plan.argv[8:], ["proton", "run"])
        self.assertNotIn("--hdr-enabled", plan.argv)

    def test_gamescope_on_inside_session_does_not_nest(self):
        cfg, _ = self.write_config(BASE + '[defaults]\ngamescope = "on"\n')
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_IN_GAMESCOPE="1"), self.home)
        self.assertFalse(plan.gamescope["wrap"])
        self.assertTrue(any("not nesting" in note for note in plan.notes))

    def test_hdr_request_without_gamescope_binary_is_skipped_with_a_note(self):
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, OXP3_PANEL_HDR="on"),
            self.home, "cyberpunk2077")
        self.assertFalse(plan.gamescope["wrap"])
        self.assertEqual(plan.features["hdr"].state, "skipped")
        self.assertTrue(any("not on PATH" in note for note in plan.notes))

    def test_gamescope_hdr_flag_zero_is_not_a_session_marker(self):
        inside, reason = GW.detect_gamescope(
            dict(self.env, STEAM_GAMESCOPE_HDR_SUPPORTED="0"))
        self.assertFalse(inside, reason)

    # -- app resolution ----------------------------------------------------
    def test_app_matching_by_steam_appid(self):
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        plan = GW.build_plan(
            cfg, ["proton"], dict(self.env, SteamAppId="1423049311"), self.home)
        self.assertEqual(plan.app_name, "cyberpunk2077")

    def test_unknown_explicit_app_warns(self):
        cfg, _ = self.write_config(BASE)
        plan = GW.build_plan(cfg, ["proton"], self.env, self.home, "nope")
        self.assertIsNone(plan.app_name)
        self.assertTrue(any("not configured" in note for note in plan.notes))

    # -- dry-run / status / exec through the shell entry point --------------
    def test_dry_run_json_preserves_argv_verbatim(self):
        res = self.run_wrap(
            "--dry-run", "--json", "--", "/bin/echo", "a b", "c'd", "--flag")
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["command"], ["/bin/echo", "a b", "c'd", "--flag"])
        self.assertFalse(doc["gamescope"]["wrap"])
        self.assertEqual(doc["mode"], "dry-run")

    def test_dry_run_does_not_execute_the_command(self):
        marker = self.tree.path("executed")
        script = self.tree.write(
            "bin/runner", "#!/bin/sh\ntouch %s\n" % marker)
        script.chmod(0o755)
        res = self.run_wrap("--dry-run", "--", str(script))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("command", res.stdout)

    def test_status_json_has_the_documented_shape(self):
        res = self.run_wrap("--status", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(res.stdout)
        for key in ("schema", "version", "mode", "config", "app", "panel_hdr",
                    "gamescope", "features", "env_diff", "command", "notes"):
            self.assertIn(key, doc)
        self.assertEqual(set(doc["features"]),
                         {"xmx", "hdr", "vibrance", "mangohud"})

    def test_bad_config_exits_2(self):
        (self.cfg_dir / "oxp3-gamewrap.toml").write_text(
            "[defaults]\nhdr = [unclosed\n", encoding="utf-8")
        res = self.run_wrap("--status")
        self.assertEqual(res.returncode, 2)
        self.assertIn("invalid TOML", res.stderr)

    def test_unknown_option_exits_2(self):
        self.assertEqual(self.run_wrap("--frobnicate").returncode, 2)

    def test_json_without_dry_run_or_status_exits_2(self):
        self.assertEqual(self.run_wrap("--json", "--", "/bin/true").returncode, 2)

    def test_exec_applies_the_composed_environment(self):
        self.xmx_fixture()
        cfg, _ = self.write_config(BASE + CYBERPUNK)
        res = self.run_wrap(
            "--app", "cyberpunk2077", "--",
            "/bin/sh", "-c", 'printf %s "$VK_DRIVER_FILES"')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.startswith(str(self.home / ".config" / "xess-xmx")))

    def test_expand_path_rejects_unsupported_tilde_prefix(self):
        with self.assertRaises(GW.ConfigError):
            GW.expand_path("~other/file", self.home, self.env)


if __name__ == "__main__":
    unittest.main()
