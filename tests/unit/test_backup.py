"""Unit tests for the undo state in tools/lib/install-common.sh and tools/oxp3-rollback.sh.

The library documents that sourcing it has no side effects; every test here runs it (or the
rollback tool) in a throw-away process with a private HOME, XDG_STATE_HOME and OXP3_STATE_DIR,
so nothing outside the temporary tree is read or written. No root, no network, no host path.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import unittest

import support

LIB = support.REPO / "tools" / "lib" / "install-common.sh"
TOOL = support.REPO / "tools" / "oxp3-rollback.sh"


def current_user():
    return getpass.getuser()


class UndoCase(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.home = self.tree.mkdir("home")
        self.state = self.tree.path("state")
        self.xdg = self.tree.mkdir("xdg")

    # -- helpers ------------------------------------------------------------
    def env(self, **overrides):
        env = dict(os.environ)
        env.update(
            LC_ALL="C",
            HOME=str(self.home),
            XDG_STATE_HOME=str(self.xdg),
            OXP3_STATE_DIR=str(self.state),
        )
        for key in ("OXP3_ROOT", "OXP3_HOME", "SUDO_USER", "XDG_DATA_HOME"):
            env.pop(key, None)
        env.update({k: str(v) for k, v in overrides.items()})
        return env

    def bash(self, script, **env):
        body = f"set -euo pipefail\nsource {LIB}\n{script}\n"
        return subprocess.run(["bash", "-uc", body], capture_output=True, text=True,
                              timeout=60, env=self.env(**env))

    def tool(self, *args, **env):
        return subprocess.run([str(TOOL), *args], capture_output=True, text=True,
                              timeout=60, env=self.env(**env))

    def write(self, path, text, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def manifest(self):
        path = self.state / "manifest.tsv"
        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text().splitlines() if line]

    def sha(self, path):
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()


class LibraryTest(UndoCase):
    def test_backup_copies_bytes_and_mode(self):
        target = self.write(self.home / "a.conf", "original\n", 0o640)
        res = self.bash(f'oxp3_backup_image "{target}" test')
        self.assertEqual(res.returncode, 0, res.stderr)
        stamp, key, pre = res.stdout.strip().split("\t")
        self.assertEqual(key, "home/a.conf")
        self.assertEqual(pre, self.sha(target))
        image = self.state / "backups" / stamp / "home" / "a.conf"
        self.assertEqual(image.read_bytes(), b"original\n")
        self.assertEqual(image.stat().st_mode & 0o777, 0o640)

    def test_key_maps_home_abs_and_strips_the_simulation_prefix(self):
        res = self.bash('''
            printf 'home=%s\\n' "$(oxp3_key "$OXP3_HOME/x/y")"
            printf 'abs=%s\\n'  "$(oxp3_key /etc/x/y)"
        ''', OXP3_HOME="/srv/home/u")
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = dict(line.split("=", 1) for line in res.stdout.split())
        self.assertEqual(lines["home"], "home/x/y")
        self.assertEqual(lines["abs"], "abs/etc/x/y")
        res = self.bash('printf "%s\\n" "$(oxp3_key "$OXP3_ROOT/etc/x")"',
                        OXP3_ROOT="/tmp/sim", OXP3_HOME="/tmp/sim/home/u")
        self.assertEqual(res.stdout.strip(), "abs/etc/x")

    def test_manifest_row_records_event_and_post_hash(self):
        target = self.write(self.home / "b.conf", "old\n")
        res = self.bash(f'''
            stamp=$(oxp3_backup_image "{target}" mytool)
            printf 'ours\\n' > "{target}"
            oxp3_manifest_record "{target}" mytool "${{stamp%%$'\\t'*}}" auto "note here"
        ''')
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = self.manifest()
        self.assertEqual(len(rows), 1)
        stamp, tool, event, key, logical, real, post, note = rows[0]
        self.assertEqual((tool, event, key, note), ("mytool", "backup", "home/b.conf", "note here"))
        self.assertEqual(post, self.sha(target))
        self.assertEqual(real, str(target))

    def test_missing_file_is_recorded_as_created(self):
        target = self.home / "new.conf"
        res = self.bash(f'''
            stamp=$(oxp3_backup_image "{target}" mytool)
            printf 'new\\n' > "{target}"
            oxp3_manifest_record "{target}" mytool "${{stamp%%$'\\t'*}}" auto created
        ''')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.manifest()[0][2], "created")

    def test_restore_is_byte_identical_and_idempotent(self):
        target = self.write(self.home / "c.conf", "before\n", 0o600)
        before = target.read_bytes()
        res = self.bash(f'''
            stamp=$(oxp3_backup_image "{target}" t)
            printf 'after\\n' > "{target}"
            oxp3_manifest_record "{target}" t "${{stamp%%$'\\t'*}}" auto x
            oxp3_restore_file "{target}" || printf 'rc=%s\\n' "$?"
            oxp3_restore_file "{target}" || printf 'rc=%s\\n' "$?"
        ''')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertIn("rc=10", res.stdout)
        restored = [r for r in self.manifest() if r[2] == "restored"]
        self.assertEqual(len(restored), 1)

    def test_restore_removes_a_file_we_created(self):
        target = self.home / "made.conf"
        res = self.bash(f'''
            stamp=$(oxp3_backup_image "{target}" t)
            printf 'made\\n' > "{target}"
            oxp3_manifest_record "{target}" t "${{stamp%%$'\\t'*}}" auto
            oxp3_restore_file "{target}" || printf 'rc=%s\\n' "$?"
        ''')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(self.manifest()[-1][2], "removed")

    def test_user_edit_after_ours_is_skipped_then_forced(self):
        target = self.write(self.home / "d.conf", "original\n")
        res = self.bash(f'''
            stamp=$(oxp3_backup_image "{target}" t)
            printf 'ours\\n' > "{target}"
            oxp3_manifest_record "{target}" t "${{stamp%%$'\\t'*}}" auto first
            printf 'user edit\\n' > "{target}"
            oxp3_restore_file "{target}" || printf 'rc=%s\\n' "$?"
            cat "{target}"
            oxp3_restore_file "{target}" --force || printf 'forced=%s\\n' "$?"
            cat "{target}"
        ''')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("rc=12", res.stdout)
        self.assertIn("user edit", res.stdout.split("restored")[0])
        self.assertIn("original", res.stdout.split("restored")[-1])
        backups = list((self.state / "backups").iterdir())
        preserved = [b for b in backups if (b / "home" / "d.conf").read_bytes() == b"user edit\n"]
        self.assertEqual(len(preserved), 1, "the user's version must be preserved")

    def test_restore_all_reports_a_skip_and_returns_3(self):
        a = self.write(self.home / "e.conf", "a-before\n")
        b = self.write(self.home / "f.conf", "b-before\n")
        res = self.bash(f'''
            s1=$(oxp3_backup_image "{a}" t); printf 'a-ours\\n' > "{a}"
            oxp3_manifest_record "{a}" t "${{s1%%$'\\t'*}}" auto
            s2=$(oxp3_backup_image "{b}" t); printf 'b-ours\\n' > "{b}"
            oxp3_manifest_record "{b}" t "${{s2%%$'\\t'*}}" auto
            printf 'user\\n' > "{b}"
            oxp3_restore_all || printf 'all=%s\\n' "$?"
        ''')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("all=3", res.stdout)
        self.assertEqual(a.read_text(), "a-before\n")
        self.assertEqual(b.read_text(), "user\n")

    def test_dry_run_writes_nothing(self):
        target = self.write(self.home / "g.conf", "before\n")
        before_manifest = self.bash(f'''
            stamp=$(oxp3_backup_image "{target}" t); printf 'after\\n' > "{target}"
            oxp3_manifest_record "{target}" t "${{stamp%%$'\\t'*}}" auto
        ''')
        self.assertEqual(before_manifest.returncode, 0, before_manifest.stderr)
        rows = len(self.manifest())
        res = self.bash(f'oxp3_restore_file "{target}" --dry-run')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("would-restore", res.stdout)
        self.assertEqual(target.read_text(), "after\n")
        self.assertEqual(len(self.manifest()), rows)

    def test_state_dir_precedence(self):
        res = self.bash('printf "%s\\n" "$(oxp3_state_dir)"')
        self.assertEqual(res.stdout.strip(), str(self.state))
        res = self.bash('printf "%s\\n" "$(oxp3_state_dir)"', OXP3_STATE_DIR="")
        self.assertEqual(res.stdout.strip(), str(self.xdg / "oxp3-fixes"))


class ToolTest(UndoCase):
    def prefix(self):
        """A --root tree using this machine's real home path under the prefix."""
        import pwd
        home_real = pwd.getpwuid(os.getuid()).pw_dir.lstrip("/")
        root = self.tree.mkdir("root")
        home = root / home_real
        home.mkdir(parents=True, exist_ok=True)
        return root, home

    def test_apply_backs_up_and_restore_is_byte_identical(self):
        root, home = self.prefix()
        launcher = home / "heroic-launch.sh"
        launcher.write_text("#!/bin/sh\necho stock\n")
        launcher.chmod(0o755)
        before = launcher.read_bytes()
        res = self.tool("verify", "--root", str(root), "--user", current_user())
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("UNBACKED", res.stdout)
        res = self.tool("apply", "--root", str(root), "--user", current_user())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(launcher.read_bytes(), (support.REPO / "tools" / "heroic-launch.sh").read_bytes())
        state = home / ".local" / "state" / "oxp3-fixes"
        self.assertTrue((state / "manifest.tsv").exists())
        res = self.tool("verify", "--root", str(root), "--user", current_user())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        res = self.tool("restore", "--root", str(root), "--user", current_user())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(launcher.read_bytes(), before)
        self.assertEqual(launcher.stat().st_mode & 0o111, 0o111)

    def test_second_apply_is_idempotent(self):
        root, home = self.prefix()
        (home / "heroic-launch.sh").write_text("#!/bin/sh\necho stock\n")
        user = current_user()
        first = self.tool("apply", "--root", str(root), "--user", user)
        self.assertEqual(first.returncode, 0, first.stderr)
        rows = (home / ".local/state/oxp3-fixes/manifest.tsv").read_text().splitlines()
        second = self.tool("apply", "--root", str(root), "--user", user)
        self.assertEqual(second.returncode, 0, second.stderr)
        again = (home / ".local/state/oxp3-fixes/manifest.tsv").read_text().splitlines()
        self.assertEqual(len(rows), len(again), "a second apply must not add a backup")

    def test_adopt_uses_the_sibling_bak_and_restore_brings_it_back(self):
        root, home = self.prefix()
        launcher = home / "heroic-launch.sh"
        launcher.write_text((support.REPO / "tools" / "heroic-launch.sh").read_text())
        launcher.chmod(0o755)
        old = home / "heroic-launch.sh.bak-0101"
        old.write_text("#!/bin/sh\necho really old\n")
        user = current_user()
        res = self.tool("adopt", "--root", str(root), "--user", user)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("pre-edit image from heroic-launch.sh.bak-0101", res.stdout)
        res = self.tool("restore", "--root", str(root), "--user", user)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(launcher.read_bytes(), old.read_bytes())

    def test_restore_refuses_to_overwrite_a_user_edit(self):
        root, home = self.prefix()
        launcher = home / "heroic-launch.sh"
        launcher.write_text("#!/bin/sh\necho stock\n")
        user = current_user()
        self.assertEqual(self.tool("apply", "--root", str(root), "--user", user).returncode, 0)
        launcher.write_text("#!/bin/sh\necho user changed this\n")
        res = self.tool("restore", "--root", str(root), "--user", user)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("user changed this", launcher.read_text())
        res = self.tool("restore", "--root", str(root), "--user", user, "--force")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("echo stock", launcher.read_text())

    def test_status_json_is_valid_and_lists_the_edit(self):
        import json
        root, home = self.prefix()
        (home / "heroic-launch.sh").write_text("#!/bin/sh\necho stock\n")
        user = current_user()
        self.assertEqual(self.tool("apply", "--root", str(root), "--user", user).returncode, 0)
        res = self.tool("status", "--root", str(root), "--user", user, "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["schema"], 1)
        self.assertEqual([e["key"] for e in data["edits"]], ["home/heroic-launch.sh"])


if __name__ == "__main__":
    unittest.main()
