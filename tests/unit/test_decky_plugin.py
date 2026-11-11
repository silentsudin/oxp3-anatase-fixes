# SPDX-License-Identifier: MIT
"""Unit tests for tools/lib/decky_plugin.py (path resolution, manifest/artifact validation,
ownership classification and the install/uninstall file moves).

Every test runs against a private temporary tree.  No Decky installation, systemd unit,
network, root or host path is touched; zips are built in the test from synthetic files.
"""

from __future__ import annotations

import hashlib
import json
import unittest
import zipfile
from pathlib import Path

import support

MOD = support.load("decky_plugin", "lib/decky_plugin.py")

MANIFEST = {
    "name": "OXP3 Status",
    "author": "silentsudin",
    "version": "1.0.0",
    "api_version": 1,
    "flags": [],
}


def manifest(**overrides):
    data = dict(MANIFEST)
    data.update(overrides)
    return data


def index_js() -> str:
    return "/* synthetic bundle */\n" + ("x" * 1080) + "\ndefinePlugin({});\n"


def write_plugin_tree(root: Path, manifest_data=None, *, with_marker=False,
                      index=True, marker=None) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps(manifest_data if manifest_data is not None else manifest(), indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text("# backend fixture\n", encoding="utf-8")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    if index:
        (root / "dist").mkdir(exist_ok=True)
        (root / "dist" / "index.js").write_text(index_js(), encoding="utf-8")
        (root / "dist" / "index.js.map").write_text("{}\n", encoding="utf-8")
    if with_marker:
        if marker is None:
            marker = MOD.marker_text(
                {"version": (manifest_data or MANIFEST)["version"]}, "0" * 64
            )
        (root / MOD.MARKER_NAME).write_text(marker, encoding="utf-8")
    return root


def make_zip(destination: Path, *, manifest_data=None, entries=None,
             index_text=None) -> Path:
    """Build the exact plugin-root zip layout (or the caller's entries dict)."""
    manifest_data = manifest_data if manifest_data is not None else manifest()
    files = {
        "plugin.json": json.dumps(manifest_data, indent=2) + "\n",
        "main.py": "# backend fixture\n",
        "README.md": "# fixture\n",
        "dist/index.js": index_text if index_text is not None else index_js(),
        "dist/index.js.map": "{}\n",
    }
    if entries is not None:
        files = entries
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return destination


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.home = Path("/home/tester")

    def test_default_is_below_home(self):
        self.assertEqual(
            str(MOD.resolve_plugins_dir(self.home)),
            "/home/tester/homebrew/plugins/oxp3-status",
        )

    def test_root_prefixes_the_default(self):
        self.assertEqual(
            str(MOD.resolve_plugins_dir(self.home, root="/tmp/prefix")),
            "/tmp/prefix/home/tester/homebrew/plugins/oxp3-status",
        )

    def test_a_home_already_under_the_root_is_not_doubled(self):
        home = Path("/tmp/prefix/home/tester")
        self.assertEqual(
            str(MOD.resolve_plugins_dir(home, root="/tmp/prefix")),
            "/tmp/prefix/home/tester/homebrew/plugins/oxp3-status",
        )

    def test_tilde_override_expands_against_home(self):
        self.assertEqual(
            str(MOD.resolve_plugins_dir(self.home, plugins_dir="~/custom/x")),
            "/home/tester/custom/x",
        )

    def test_home_variable_override(self):
        self.assertEqual(
            str(MOD.resolve_plugins_dir(self.home, plugins_dir="$HOME/foo")),
            "/home/tester/foo",
        )

    def test_relative_override_is_below_home(self):
        self.assertEqual(
            str(MOD.resolve_plugins_dir(self.home, plugins_dir="my/plugins/p")),
            "/home/tester/my/plugins/p",
        )

    def test_absolute_override_is_prefixed_by_root(self):
        self.assertEqual(
            str(MOD.resolve_plugins_dir(self.home, plugins_dir="/opt/decky/p",
                                        root="/tmp/prefix")),
            "/tmp/prefix/opt/decky/p",
        )

    def test_empty_override_is_rejected(self):
        with self.assertRaises(MOD.PluginError):
            MOD.resolve_plugins_dir(self.home, plugins_dir="   ")

    def test_unsafe_root_target_is_rejected(self):
        # A home of "/" would resolve to the filesystem root itself.
        with self.assertRaises(MOD.PluginError):
            MOD.resolve_plugins_dir(Path("/"), plugins_dir="/")


class StateDirTest(unittest.TestCase):
    def test_default_state_dir(self):
        self.assertEqual(
            str(MOD.resolve_state_dir(Path("/home/tester"))),
            "/home/tester/.local/state/oxp3-fixes",
        )

    def test_state_dir_is_prefixed_by_root(self):
        self.assertEqual(
            str(MOD.resolve_state_dir(Path("/home/tester"), root="/tmp/prefix")),
            "/tmp/prefix/home/tester/.local/state/oxp3-fixes",
        )

    def test_explicit_absolute_and_relative_state_dir(self):
        self.assertEqual(
            str(MOD.resolve_state_dir(Path("/home/tester"), state_dir="/tmp/state")),
            "/tmp/state",
        )
        self.assertEqual(
            str(MOD.resolve_state_dir(Path("/home/tester"), state_dir="mystate")),
            "/home/tester/mystate",
        )


class ManifestTest(unittest.TestCase):
    def test_expected_manifest(self):
        info = MOD.validate_manifest(manifest())
        self.assertEqual(info["version"], "1.0.0")

    def test_wrong_name(self):
        with self.assertRaises(MOD.PluginError):
            MOD.validate_manifest(manifest(name="Other Plugin"))

    def test_wrong_author(self):
        with self.assertRaises(MOD.PluginError):
            MOD.validate_manifest(manifest(author="someone"))

    def test_bool_api_version_is_not_an_int(self):
        with self.assertRaises(MOD.PluginError):
            MOD.validate_manifest(manifest(api_version=True))

    def test_missing_or_bad_version(self):
        for value in (None, "", "1.0", "1.0.0/evil"):
            data = manifest()
            if value is None:
                data.pop("version")
            else:
                data["version"] = value
            with self.subTest(version=value), self.assertRaises(MOD.PluginError):
                MOD.validate_manifest(data)

    def test_plugin_dir_requires_main_and_bundle(self):
        tree = support.TmpTree()
        self.addCleanup(tree.cleanup)
        good = write_plugin_tree(tree.path("good"))
        info = MOD.validate_plugin_dir(good)
        self.assertEqual(info["version"], "1.0.0")
        no_main = write_plugin_tree(tree.path("no-main"))
        (no_main / "main.py").unlink()
        with self.assertRaises(MOD.PluginError):
            MOD.validate_plugin_dir(no_main)
        no_dist = write_plugin_tree(tree.path("no-dist"))
        (no_dist / "dist").rename(no_dist / "elsewhere")
        with self.assertRaises(MOD.PluginError):
            MOD.validate_plugin_dir(no_dist)
        tiny = write_plugin_tree(tree.path("tiny"), index=False)
        (tiny / "dist").mkdir()
        (tiny / "dist/index.js").write_text("definePlugin({});\n", encoding="utf-8")
        with self.assertRaises(MOD.PluginError):
            MOD.validate_plugin_dir(tiny)


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def verify(self, zip_path, digest=None, require=True):
        exists = Path(zip_path).is_file()
        recorded = {
            "sha256": digest or (sha(zip_path) if exists else "0" * 64),
            "filename": Path(zip_path).name,
        }
        return MOD.verify_artifact(zip_path, support.REPO, recorded=recorded,
                                   require_recorded=require)

    def test_good_artifact(self):
        z = make_zip(self.tree.path("build/oxp3-status-1.0.0.zip"))
        info = self.verify(z)
        self.assertEqual(info["version"], "1.0.0")
        self.assertTrue(info["recorded_match"])

    def test_missing_artifact(self):
        with self.assertRaises(MOD.PluginError):
            self.verify(self.tree.path("build/none.zip"))

    def test_bad_digest_is_refused(self):
        z = make_zip(self.tree.path("build/oxp3-status-1.0.0.zip"))
        with self.assertRaises(MOD.PluginError):
            self.verify(z, digest="0" * 64)

    def test_extra_entry_is_refused(self):
        entries = {
            "plugin.json": json.dumps(manifest()),
            "main.py": "# x\n",
            "README.md": "# x\n",
            "dist/index.js": index_js(),
            "dist/index.js.map": "{}",
            "payload.sh": "# not part of the plugin root\n",
        }
        z = make_zip(self.tree.path("build/bad.zip"), entries=entries)
        with self.assertRaises(MOD.PluginError):
            self.verify(z)

    def test_traversal_entry_is_refused(self):
        entries = {
            "plugin.json": json.dumps(manifest()),
            "main.py": "# x\n",
            "README.md": "# x\n",
            "dist/index.js": index_js(),
            "dist/index.js.map": "{}",
            "../escape": "x",
        }
        z = make_zip(self.tree.path("build/traversal.zip"), entries=entries)
        with self.assertRaises(MOD.PluginError):
            self.verify(z)

    def test_duplicate_entry_is_refused(self):
        z = self.tree.path("build/dupe.zip")
        z.parent.mkdir(parents=True)
        with zipfile.ZipFile(z, "w") as zf:
            for name in sorted(MOD.EXPECTED_ENTRIES):
                zf.writestr(name, "{}")
            zf.writestr("plugin.json", json.dumps(manifest()))
        with self.assertRaises(MOD.PluginError):
            self.verify(z)

    def test_filename_version_must_match_manifest(self):
        z = make_zip(self.tree.path("build/oxp3-status-9.9.9.zip"))
        with self.assertRaises(MOD.PluginError):
            self.verify(z)

    def test_tiny_bundle_is_refused(self):
        z = make_zip(self.tree.path("build/tiny.zip"),
                     index_text="definePlugin({});\n")
        with self.assertRaises(MOD.PluginError):
            self.verify(z)

    def test_recorded_file_is_parsed(self):
        repo = self.tree.mkdir("repo")
        path = repo / MOD.RECORDED_SHA_REL
        path.parent.mkdir(parents=True)
        path.write_text("# comment\n" + "a" * 64 + "  oxp3-status-1.0.0.zip\n",
                        encoding="utf-8")
        info = MOD.read_recorded_sha(repo)
        self.assertEqual(info["sha256"], "a" * 64)
        self.assertEqual(info["filename"], "oxp3-status-1.0.0.zip")

    def test_recorded_file_rejects_garbage(self):
        repo = self.tree.mkdir("repo2")
        path = repo / MOD.RECORDED_SHA_REL
        path.parent.mkdir(parents=True)
        path.write_text("not-a-digest\n", encoding="utf-8")
        with self.assertRaises(MOD.PluginError):
            MOD.read_recorded_sha(repo)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def test_absent(self):
        self.assertEqual(MOD.classify(self.tree.path("none")), "absent")

    def test_ours(self):
        p = write_plugin_tree(self.tree.path("ours"), with_marker=True)
        self.assertEqual(MOD.classify(p), "ours")

    def test_adoptable_hand_copy(self):
        p = write_plugin_tree(self.tree.path("hand"))
        self.assertEqual(MOD.classify(p), "adoptable")

    def test_foreign_plugin(self):
        p = self.tree.mkdir("foreign")
        (p / "plugin.json").write_text(json.dumps({"name": "Other Plugin"}), encoding="utf-8")
        self.assertEqual(MOD.classify(p), "foreign")

    def test_broken_marker_is_foreign(self):
        p = write_plugin_tree(self.tree.path("tampered"))
        (p / MOD.MARKER_NAME).write_text("plugin=something-else\n", encoding="utf-8")
        self.assertEqual(MOD.classify(p), "foreign")


class CloneTest(unittest.TestCase):
    """Install/uninstall against a private HOME, plugins dir and state dir."""

    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.home = self.tree.mkdir("home")
        self.plugins = self.home / "homebrew/plugins/oxp3-status"
        self.state = self.tree.path("state")
        self.artifact = make_zip(self.tree.path("build/oxp3-status-1.0.0.zip"))

    def install(self, **kwargs):
        kwargs.setdefault("require_recorded", False)
        return MOD.install(self.artifact, self.home, state_dir=str(self.state),
                           repo=support.REPO, **kwargs)

    def test_first_install_writes_marker_and_expected_files(self):
        info = self.install()
        self.assertEqual(info["previous"], "absent")
        self.assertIsNone(info["backup"])
        for name in ("plugin.json", "main.py", "dist/index.js"):
            self.assertTrue((self.plugins / name).is_file(), name)
        marker = MOD.read_marker(self.plugins)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["plugin"], "oxp3-status")
        self.assertEqual(marker["version"], "1.0.0")
        self.assertEqual(marker["artifact_sha256"], sha(self.artifact))

    def test_reinstall_backs_up_the_previous_copy(self):
        self.install()
        # A re-install must preserve the previous directory before replacing it.
        info = self.install()
        self.assertEqual(info["previous"], "ours")
        self.assertIsNotNone(info["backup"])
        backup = Path(info["backup"])
        self.assertTrue((backup / "oxp3-status/plugin.json").is_file())
        self.assertTrue((backup / "backup.json").is_file())
        meta = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["origin"], str(self.plugins))

    def test_install_over_a_foreign_directory_is_refused(self):
        self.plugins.mkdir(parents=True)
        (self.plugins / "plugin.json").write_text(
            json.dumps({"name": "Other Plugin"}), encoding="utf-8"
        )
        with self.assertRaises(MOD.PluginRefused):
            self.install()
        self.assertTrue((self.plugins / "plugin.json").is_file())

    def test_install_over_an_unmarked_copy_adopts_and_backs_it_up(self):
        write_plugin_tree(self.plugins)
        info = self.install()
        self.assertEqual(info["previous"], "adoptable")
        self.assertIsNotNone(info["backup"])
        self.assertIsNotNone(MOD.read_marker(self.plugins))

    def test_uninstall_removes_only_the_marked_directory(self):
        self.install()
        result = MOD.uninstall(self.home)
        self.assertTrue(result["removed"])
        self.assertFalse(self.plugins.exists())
        self.assertTrue(self.plugins.parent.is_dir())

    def test_uninstall_is_idempotent(self):
        result = MOD.uninstall(self.home)
        self.assertFalse(result["removed"])
        self.assertEqual(result["state"], "absent")

    def test_uninstall_refuses_an_unmarked_copy(self):
        write_plugin_tree(self.plugins)
        with self.assertRaises(MOD.PluginRefused):
            MOD.uninstall(self.home)
        self.assertTrue((self.plugins / "plugin.json").is_file())

    def test_uninstall_refuses_a_foreign_directory(self):
        self.plugins.mkdir(parents=True)
        (self.plugins / "notes.txt").write_text("not ours\n", encoding="utf-8")
        with self.assertRaises(MOD.PluginRefused):
            MOD.uninstall(self.home)
        self.assertTrue((self.plugins / "notes.txt").is_file())

    def test_status_reports_installed_and_foreign_states(self):
        self.assertEqual(MOD.status(self.home)["state"], "absent")
        self.install()
        info = MOD.status(self.home)
        self.assertEqual(info["state"], "ok")
        self.assertTrue(info["installed"])
        self.assertEqual(info["version"], "1.0.0")
        self.assertTrue(info["marker"])

    def test_cli_exit_codes(self):
        # A foreign directory must make the CLI exit 3, not delete anything.
        self.plugins.mkdir(parents=True)
        (self.plugins / "plugin.json").write_text(json.dumps({"name": "Other"}),
                                                  encoding="utf-8")
        rc = MOD.main(["uninstall", "--home", str(self.home)])
        self.assertEqual(rc, 3)
        rc = MOD.main(["install", "--home", str(self.home), "--artifact",
                       str(self.tree.path("build/missing.zip"))])
        self.assertEqual(rc, 1)
        rc = MOD.main(["status", "--home", str(self.home)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
