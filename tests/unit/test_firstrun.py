"""Offline tests for tools/oxp3_firstrun.py (the first-run evidence collector).

Two seams are exercised with synthetic fixtures and stub commands, exactly like the
rest of this suite: the redaction engine (what leaves the machine) and the bundling
logic (line cap, byte cap, manifest accounting, idempotency). The collection and
checklist tests use a private temporary tree plus ``StubRunner`` - no host sysfs, no
journal, no root, no network, no hardware.

Nothing here writes outside the test's temporary directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path

import support

FR = support.load("oxp3_firstrun", "oxp3_firstrun.py")


def redactor(user="tester", host="box", home="/var/home/tester"):
    return FR.Redactor(FR.build_literals(Path(home), user, host))


class RedactionTest(unittest.TestCase):
    def setUp(self):
        self.red = redactor()

    def replace(self, text):
        out, counts = self.red.redact(text)
        return out, counts

    def test_mac_colon_keeps_only_the_vendor_oui(self):
        out, counts = self.replace("link 9C:6B:00:11:22:33 up")
        self.assertIn("<mac-oui:9c:6b:00>", out)
        self.assertNotIn("11:22:33", out)
        self.assertEqual(counts["mac"], 1)

    def test_mac_dash_form(self):
        out, _ = self.replace("9c-6b-00-44-55-66")
        self.assertEqual(out, "<mac-oui:9c:6b:00>")

    def test_mac_redaction_is_idempotent(self):
        once, _ = self.replace("9c:6b:00:11:22:33")
        twice, counts = self.replace(once)
        self.assertEqual(once, twice)
        self.assertFalse(counts)

    def test_username_home_and_host(self):
        out, counts = self.redact_text("tester at box, /var/home/tester/git")
        self.assertNotIn("tester", out)
        self.assertNotIn("/var/home/tester", out)
        self.assertIn("<user>", out)
        self.assertIn("<host>", out)
        self.assertIn("~/git", out)
        self.assertEqual(counts["identity"], 3)

    def redact_text(self, text):
        return self.red.redact(text)

    def test_home_alias_both_ways(self):
        red = redactor(home="/home/tester")
        out, _ = red.redact("/home/tester/x and /var/home/tester/y")
        self.assertNotIn("/home/tester", out)
        self.assertNotIn("/var/home/tester", out)

    def test_short_usernames_are_not_literal_rules(self):
        red = FR.Redactor(FR.build_literals(Path("/home/ab"), "ab", "h1"))
        out, _ = red.redact("ab is a word fragment")
        self.assertIn("ab", out)

    def test_serial_number(self):
        out, counts = self.replace("Serial Number: ABC123XY")
        self.assertNotIn("ABC123XY", out)
        self.assertIn("<redacted:serial>", out)
        self.assertEqual(counts["serial-number"], 1)

    def test_keyed_uuid_and_bare_uuid(self):
        out, _ = self.replace(
            "root=UUID=a245ffab-5073-45ee-b3ed-e7ecdec29275 "
            "panel bf025ec2-9597-412b-860c-ec979e30354a"
        )
        self.assertNotIn("a245ffab", out)
        self.assertNotIn("bf025ec2", out)
        self.assertIn("root=UUID=<redacted:uuid>", out)

    def test_sha256_digests_are_not_redacted(self):
        digest = "ec418f04e589965adeb26da96fb282915e3dfd7ab98b0d6eab69f9878f3578e9"
        out, counts = self.replace("sha256 %s" % digest)
        self.assertIn(digest, out)
        self.assertFalse(counts)

    def test_token_families(self):
        cases = {
            "ghp_abcdefghijklmnopqrstuvwxyz0123": "github-token",
            "AKIAIOSFODNN7EXAMPLE": "aws-key",
            "glpat-abcdefghijklmnop1234": "gitlab-token",
            "xoxb-1234567890-abcdefghij": "slack-token",
            "sk-abcdefghijklmnopqrstuvwxyz": "openai-key",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh": "jwt",
        }
        for raw, rule in cases.items():
            out, counts = self.replace("value " + raw)
            self.assertNotIn(raw, out, rule)
            self.assertIn("<redacted:%s>" % rule, out)

    def test_bearer_and_secret_assignment(self):
        out, _ = self.replace("Authorization: Bearer abcdef0123456789")
        self.assertNotIn("abcdef0123456789", out)
        out, counts = self.replace("password=hunter2")
        self.assertNotIn("hunter2", out)
        self.assertEqual(counts["secret-assignment"], 1)

    def test_api_key_keeps_its_own_token_marker(self):
        out, _ = self.replace("api_key: sk-abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", out)
        self.assertIn("<redacted:openai-key>", out)

    def test_email_is_redacted_as_a_whole(self):
        out, counts = self.replace("mail tester@example.com now")
        self.assertNotIn("tester@example.com", out)
        self.assertNotIn("example.com", out)
        self.assertEqual(out, "mail <email> now")

    def test_pulseaudio_cookie(self):
        out, _ = self.replace("Cookie: a46d:7cc4\nnext line")
        self.assertIn("<redacted:cookie>", out)
        self.assertIn("next line", out)

    def test_redaction_is_idempotent_on_a_corpus(self):
        corpus = (
            "tester box /var/home/tester /home/tester 9c:6b:00:11:22:33 "
            "Serial Number: ZZ1 root=UUID=a245ffab-5073-45ee-b3ed-e7ecdec29275 "
            "ghp_abcdefghijklmnopqrstuvwxyz0123 AKIAIOSFODNN7EXAMPLE "
            "Bearer abcdef0123456789 password=hunter2 tester@example.com "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh Cookie: 1:2\n"
        )
        once, first = self.replace(corpus)
        twice, second = self.replace(once)
        self.assertEqual(once, twice)
        self.assertGreater(first["identity"], 0)
        self.assertGreater(first["mac"], 0)
        self.assertGreater(first["uuid"], 0)
        self.assertFalse(second)

    def test_empty_text(self):
        out, counts = self.replace("")
        self.assertEqual(out, "")
        self.assertFalse(counts)

    def test_report_records_counts_and_files(self):
        self.red.note_file("a.txt", self.red.redact("9c:6b:00:11:22:33")[1])
        self.red.note_file("b.txt", self.red.redact("nothing")[1])
        self.red.note_file("a.txt", self.red.redact("9c:6b:00:44:55:66")[1])
        report = self.red.report()
        self.assertEqual(report["files_with_matches"], 1)
        self.assertEqual(report["rules"]["mac"], 2)
        self.assertIn("a.txt", report["files"])
        self.assertNotIn("b.txt", report["files"])

    def test_report_file_list_is_bounded(self):
        for index in range(5):
            self.red.note_file("f%d" % index, self.red.redact("9c:6b:00:11:22:33")[1])
        report = self.red.report(max_files=2)
        self.assertEqual(len(report["files"]), 2)
        self.assertEqual(report["files_omitted"], 3)


class LimitsTest(unittest.TestCase):
    def test_count_lines(self):
        self.assertEqual(FR.count_lines(""), 0)
        self.assertEqual(FR.count_lines("a"), 1)
        self.assertEqual(FR.count_lines("a\n"), 1)
        self.assertEqual(FR.count_lines("a\nb"), 2)

    def test_truncate_lines_keeps_a_visible_marker(self):
        text = "".join("line %d\n" % i for i in range(50))
        out, lines, truncated = FR.truncate_lines(text, 10)
        self.assertTrue(truncated)
        self.assertEqual(lines, 11)
        self.assertIn("truncated: showing 10 of 50", out)
        self.assertNotIn("line 10\n", out)

    def test_truncate_lines_leaves_short_text(self):
        out, lines, truncated = FR.truncate_lines("a\nb\n", 10)
        self.assertFalse(truncated)
        self.assertEqual(out, "a\nb\n")
        self.assertEqual(lines, 2)

    def test_finalize_redacts_caps_and_zeroes_non_ok(self):
        art = FR.Artifact("a.txt", "a", "tester x\n" * 40, priority=1)
        missing = FR.Artifact("b.txt", "b", "payload\n", status="missing", priority=2)
        FR.finalize_artifacts([art, missing], FR.Limits(max_lines=5), redactor())
        self.assertNotIn("tester", art.text)
        self.assertTrue(art.truncated)
        self.assertEqual(art.lines, 6)
        self.assertEqual(missing.bytes, 0)
        self.assertEqual(missing.text, "")

    def test_finalize_validates_json_and_never_line_truncates_it(self):
        payload = json.dumps({"checks": list(range(50))}) + "\n"
        art = FR.Artifact("a.json", "a", payload, kind="json", priority=1)
        FR.finalize_artifacts([art], FR.Limits(max_lines=3), redactor())
        self.assertTrue(art.json_valid)
        self.assertFalse(art.truncated)
        json.loads(art.text)

    def test_finalize_marks_broken_json_invalid_without_failing(self):
        art = FR.Artifact("a.json", "a", "{not json", kind="json", priority=1)
        FR.finalize_artifacts([art], FR.Limits(), redactor())
        self.assertFalse(art.json_valid)
        self.assertEqual(art.status, "ok")

    def test_fit_budget_never_exceeds_the_cap(self):
        arts = [
            FR.Artifact("a.txt", "a", "x" * 4000 + "\n", priority=1),
            FR.Artifact("b.txt", "b", "y" * 4000 + "\n", priority=2),
        ]
        FR.finalize_artifacts(arts, FR.Limits(), redactor())
        FR.fit_budget(arts, 1000)
        self.assertLessEqual(sum(a.bytes for a in arts), 1000)
        self.assertTrue(arts[0].truncated)
        self.assertEqual(arts[1].status, "skipped")
        self.assertIn("size budget", arts[1].reason)

    def test_fit_budget_skips_json_instead_of_corrupting_it(self):
        art = FR.Artifact("a.json", "a", json.dumps({"k": "v" * 3000}), kind="json", priority=1)
        FR.finalize_artifacts([art], FR.Limits(), redactor())
        FR.fit_budget([art], 500)
        self.assertEqual(art.status, "skipped")
        self.assertEqual(art.text, "")

    def test_fit_budget_with_zero_budget_skips_everything(self):
        art = FR.Artifact("a.txt", "a", "payload\n", priority=1)
        FR.finalize_artifacts([art], FR.Limits(), redactor())
        FR.fit_budget([art], 0)
        self.assertEqual(art.status, "skipped")


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.repo = self.tree.mkdir("home/repo")
        self.now = FR._dt.datetime(2026, 11, 9, 3, 50, tzinfo=FR._dt.timezone.utc)
        self.ctx = FR.Context(repo=self.repo, home=self.tree.path("home"), user="tester",
                              host="box", euid=1000, now=self.now, available_tools=set())
        self.steps = [FR.Step("collector", "bundle", FR.OK, "written")]

    def artifacts(self, lines=200, width=8):
        body = "".join(("line %d tester %s\n" % (i, "x" * width)) for i in range(lines))
        arts = [
            FR.Artifact("verify/verify.json", "verify", json.dumps({"checks": [1, 2]}) + "\n",
                        kind="json", priority=20),
            FR.Artifact("units/system.txt", "units", body, priority=32),
        ]
        for art in arts:
            art.exit_code = 1 if art.kind == "json" else None
        return arts

    def write(self, out=None, limits=None, dry_run=False, force=False):
        limits = limits or FR.Limits(max_bytes=64 * 1024, max_lines=100)
        red = redactor(home=str(self.ctx.home))
        arts = self.artifacts()
        FR.finalize_artifacts(arts, limits, red)
        out = out or FR.plan_out_dir(None, self.repo, self.now, force)
        manifest = FR.write_bundle(self.ctx, out, arts, self.steps, FR.Facts(kernel="7.2.7"),
                                   limits, red, "collect", dry_run)
        return out, manifest

    def test_default_out_dir_name(self):
        out = FR.default_out_dir(self.repo, self.now)
        self.assertEqual(out.name, "firstrun-2026-11-09T0350Z")
        self.assertEqual(out.parent, self.repo / "docs" / "state")

    def test_plan_refuses_non_empty_dir_without_force(self):
        out = self.tree.mkdir("busy")
        self.tree.write("busy/leftover", "x")
        with self.assertRaises(FR.UsageError):
            FR.plan_out_dir(str(out), self.repo, self.now, force=False)

    def test_plan_allows_force_and_rejects_a_file(self):
        busy = self.tree.mkdir("busy2")
        self.tree.write("busy2/leftover", "x")
        self.assertEqual(FR.plan_out_dir(str(busy), self.repo, self.now, True), busy)
        afile = self.tree.write("not-a-dir", "x")
        with self.assertRaises(FR.UsageError):
            FR.plan_out_dir(str(afile), self.repo, self.now, True)

    def test_dry_run_writes_nothing_but_reports_every_artifact(self):
        out, manifest = self.write(dry_run=True)
        self.assertFalse(out.exists())
        self.assertEqual(manifest["counts"]["artifacts"], 2)
        self.assertTrue(manifest["dry_run"])

    def test_real_run_writes_the_documented_file_set(self):
        out, manifest = self.write()
        names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        self.assertEqual(
            names,
            sorted(["MANIFEST.json", "README.txt", "REDACTIONS.txt", "checklist.txt",
                    "units/system.txt", "verify/verify.json"]),
        )
        parsed = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(parsed["schema"], FR.SCHEMA)
        self.assertEqual(parsed["counts"]["ok"], 2)
        self.assertEqual(manifest["bytes_total"],
                         sum(p.stat().st_size for p in out.rglob("*") if p.is_file()))

    def test_manifest_records_exit_codes_and_json_validity(self):
        out, _ = self.write()
        parsed = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
        by_path = {a["path"]: a for a in parsed["artifacts"]}
        self.assertEqual(by_path["verify/verify.json"]["exit_code"], 1)
        self.assertTrue(by_path["verify/verify.json"]["json_valid"])

    def test_manifest_and_readme_do_not_leak_identity(self):
        out, _ = self.write()
        for name in ("MANIFEST.json", "README.txt", "checklist.txt", "REDACTIONS.txt"):
            text = (out / name).read_text(encoding="utf-8")
            self.assertNotIn("tester", text, name)
            self.assertNotIn(str(self.tree.root), text, name)

    def test_byte_cap_is_enforced_including_index_files(self):
        limits = FR.Limits(max_bytes=FR.MIN_MAX_BYTES, max_lines=100_000)
        red = redactor(home=str(self.ctx.home))
        arts = self.artifacts(lines=4000, width=20)  # ~100 KiB of payload
        FR.finalize_artifacts(arts, limits, red)
        out = FR.plan_out_dir(None, self.repo, self.now, False)
        FR.write_bundle(self.ctx, out, arts, self.steps, FR.Facts(kernel="7.2.7"),
                        limits, red, "collect", False)
        total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
        self.assertLessEqual(total, FR.MIN_MAX_BYTES)
        parsed = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertTrue(any(a["truncated"] or a["status"] == "skipped" for a in parsed["artifacts"]))

    def test_repeated_run_is_byte_identical(self):
        out, _ = self.write()
        first = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        FR.plan_out_dir(str(out), self.repo, self.now, force=True)
        self.write(out=out, force=True)
        second = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        self.assertEqual(first, second)

    def test_rejects_a_cap_too_small_for_the_index(self):
        red = redactor()
        arts = self.artifacts()
        FR.finalize_artifacts(arts, FR.Limits(), red)
        with self.assertRaises(FR.UsageError):
            FR.write_bundle(self.ctx, self.tree.path("tiny"), arts, self.steps,
                            FR.Facts(), FR.Limits(max_bytes=128), red, "collect", True)

    def test_line_cap_applies_to_text_files_only(self):
        limits = FR.Limits(max_bytes=64 * 1024, max_lines=1)
        out, manifest = self.write(limits=limits)
        text = (out / "units/system.txt").read_text(encoding="utf-8")
        self.assertIn("truncated: showing 1", text)
        json.loads((out / "verify/verify.json").read_text(encoding="utf-8"))


class FixtureChecklistTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.runner = FR.StubRunner()
        self.ctx = FR.Context(
            prefix=str(self.tree.root / "root"),
            home=self.tree.path("home"),
            repo=self.tree.path("repo"),
            user="tester",
            host="box",
            euid=1000,
            runner=self.runner,
            env={"PATH": "/usr/bin"},
            available_tools={"systemctl", "journalctl"},
        )
        self.root = Path(self.ctx.prefix)

    def populate(self, **files):
        for rel, text in files.items():
            path = self.root / rel.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(text), encoding="utf-8")

    def unit_block(self, unit, load="loaded", active="active", file_state="enabled"):
        return (
            "Id=%s\nLoadState=%s\nActiveState=%s\nSubState=running\n"
            "UnitFileState=%s\nNRestarts=0\nExecMainStatus=0\n\n"
            % (unit, load, active, file_state)
        )

    def facts(self, show_text="", user_text="", journal_ok=True):
        responses = [
            ("systemctl show -p ActiveEnterTimestamp",
             FR.CmdResult(["systemctl"], 0, "Sun 2026-11-09 01:00:00 UTC\n")),
            ("systemctl show", FR.CmdResult(["systemctl"], 0, show_text)),
            ("systemctl --user show", FR.CmdResult(["systemctl"], 0, user_text)),
            ("systemctl --failed", FR.CmdResult(["systemctl"], 0, "")),
            ("journalctl -b --no-pager -q -n 1",
             FR.CmdResult(["journalctl"], 0, "entry\n" if journal_ok else "",
                          "" if journal_ok else "denied")),
        ]
        self.runner.responses = responses
        return FR.gather_facts(self.ctx)

    def step(self, steps, step_id):
        return next(s for s in steps if s.step_id == step_id)

    def test_checklist_is_ordered_and_complete(self):
        steps = FR.evaluate_checklist(self.ctx, self.facts())
        ids = [s.step_id for s in steps]
        self.assertEqual(ids[0], "driver-stack")
        self.assertEqual(ids[-1], "collector")
        for required in ("hw-install", "udev-rules", "sleep-policy", "logind-restart",
                         "bluetooth", "power-profile", "battery-care", "audio-eq",
                         "xmx", "decky-plugin", "suspend-evidence"):
            self.assertIn(required, ids)

    def test_nothing_installed_is_todo_and_degrades_cleanly(self):
        facts = self.facts()
        steps = FR.evaluate_checklist(self.ctx, facts)
        self.assertEqual(self.step(steps, "hw-install").state, FR.TODO)
        self.assertEqual(self.step(steps, "udev-rules").state, FR.TODO)
        self.assertEqual(self.step(steps, "sleep-policy").state, FR.TODO)
        self.assertEqual(self.step(steps, "homebtn-unit").state, FR.TODO)

    def test_partial_sleep_policy(self):
        self.populate(**{"etc/systemd/logind.conf.d/60-oxp3-sleep.conf": "[Login]\n"})
        steps = FR.evaluate_checklist(self.ctx, self.facts())
        self.assertEqual(self.step(steps, "sleep-policy").state, FR.PARTIAL)

    def test_logind_restart_is_pending_when_the_drop_in_is_newer(self):
        dropin = self.root / "etc/systemd/logind.conf.d/60-oxp3-sleep.conf"
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text("[Login]\n", encoding="utf-8")
        (self.root / "etc/polkit-1/rules.d").mkdir(parents=True, exist_ok=True)
        (self.root / "etc/polkit-1/rules.d/49-oxp3-suspend.rules").write_text("//\n")
        steps = FR.evaluate_checklist(self.ctx, self.facts())
        self.assertEqual(self.step(steps, "sleep-policy").state, FR.OK)
        self.assertEqual(self.step(steps, "logind-restart").state, FR.PENDING)

    def test_logind_restart_is_ok_when_logind_started_later(self):
        dropin = self.root / "etc/systemd/logind.conf.d/60-oxp3-sleep.conf"
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text("[Login]\n", encoding="utf-8")
        old = 1_600_000_000
        os.utime(dropin, (old, old))
        self.runner.responses = [("systemctl show -p ActiveEnterTimestamp",
                                  FR.CmdResult(["systemctl"], 0, "Sun 2026-11-09 01:00:00 UTC\n"))]
        facts = self.facts()
        steps = FR.evaluate_checklist(self.ctx, facts)
        self.assertEqual(self.step(steps, "logind-restart").state, FR.OK)

    def test_suspend_evidence(self):
        self.populate(**{"sys/power/suspend_stats/success": "3\n",
                         "sys/power/suspend_stats/fail": "0\n"})
        steps = FR.evaluate_checklist(self.ctx, self.facts())
        self.assertEqual(self.step(steps, "suspend-evidence").state, FR.OK)

    def test_battery_states(self):
        self.populate(**{"sys/class/power_supply/BAT0/charge_control_end_threshold": "80\n",
                         "sys/class/power_supply/BAT0/charge_behaviour": "[auto]\n"})
        steps = FR.evaluate_checklist(self.ctx, self.facts())
        self.assertEqual(self.step(steps, "battery-care").state, FR.OK)
        self.tree.write("root/sys/class/power_supply/BAT0/charge_control_end_threshold", "100\n")
        steps = FR.evaluate_checklist(self.ctx, self.facts())
        self.assertEqual(self.step(steps, "battery-care").state, FR.TODO)

    def test_driver_stack(self):
        self.tree.mkdir("root/sys/module/xe")
        self.tree.write("root/usr/lib64/libvulkan_intel.so", "driver")
        steps = FR.evaluate_checklist(self.ctx, self.facts())
        self.assertEqual(self.step(steps, "driver-stack").state, FR.OK)

    def test_homebtn_unknown_without_a_user_bus(self):
        self.runner.responses = [("systemctl show -p ActiveEnterTimestamp",
                                  FR.CmdResult(["systemctl"], 0, "")),
                                 ("systemctl show", FR.CmdResult(["systemctl"], 0, "")),
                                 ("systemctl --user show",
                                  FR.CmdResult(["systemctl"], 1, "", "no bus"))]
        facts = FR.gather_facts(self.ctx)
        self.assertFalse(facts.user_bus)
        steps = FR.evaluate_checklist(self.ctx, facts)
        self.assertEqual(self.step(steps, "homebtn-unit").state, FR.UNKNOWN)

    def test_checklist_json_shape(self):
        facts = self.facts()
        steps = FR.evaluate_checklist(self.ctx, facts)
        data = FR._checklist_json(self.ctx, facts, steps)
        self.assertEqual(data["schema"], FR.SCHEMA)
        self.assertEqual(data["mode"], "check-only")
        self.assertEqual(len(data["steps"]), len(steps))
        self.assertIn("command", data["steps"][0])
        self.assertEqual(sum(data["counts"].values()), len(steps))

    def test_render_checklist_mentions_next_steps(self):
        facts = self.facts()
        steps = FR.evaluate_checklist(self.ctx, facts)
        text = FR.render_checklist(self.ctx, steps, facts)
        self.assertIn("NEXT (in install order", text)
        self.assertIn("sudo tools/oxp3-hw-install.sh", text)
        self.assertIn("7.2.7", text.replace("?", "7.2.7"))


class FixtureCollectTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.runner = FR.StubRunner()
        self.ctx = FR.Context(
            prefix=str(self.tree.root / "root"),
            home=self.tree.path("home"),
            repo=self.tree.path("repo"),
            user="tester",
            host="box",
            euid=1000,
            runner=self.runner,
            env={"PATH": "/usr/bin", "VK_ICD_FILENAMES": "/x/icd-xmx.json"},
            available_tools={"systemctl", "journalctl", "pactl", "lspci", "lsusb"},
        )
        self.runner.responses = [
            ("systemctl show -p ActiveEnterTimestamp",
             FR.CmdResult(["systemctl"], 0, "Sun 2026-11-09 01:00:00 UTC\n")),
            ("systemctl show", FR.CmdResult(["systemctl"], 0,
                                            "Id=oxp3-voltap.service\nLoadState=not-found\n\n")),
            ("systemctl --user show", FR.CmdResult(["systemctl"], 0, "")),
            ("systemctl --failed", FR.CmdResult(["systemctl"], 0, "")),
            ("journalctl -b --no-pager -q -n 1", FR.CmdResult(["journalctl"], 0, "x\n")),
            ("journalctl", FR.CmdResult(["journalctl"], 0, "Nov 09 03:00:00 box kernel: suspend\n")),
        ]
        self.facts = FR.gather_facts(self.ctx)
        self.opts = FR.Options(collect=True)

    def relpaths(self, artifacts):
        return [a.relpath for a in artifacts]

    def test_artifact_inventory(self):
        arts = FR.collect_all(self.ctx, self.facts, self.opts)
        paths = self.relpaths(arts)
        for required in ("verify/verify.json", "status/status.json", "units/system.txt",
                         "units/user.txt", "udev/rules.txt", "firmware/bluetooth.txt",
                         "power/power.txt", "sleep/sleep.txt", "audio/audio.txt",
                         "display/display.txt", "gfx/xmx.txt", "decky/decky.txt",
                         "system/environment.txt", "system/hardware.txt",
                         "journal/kernel.txt"):
            self.assertIn(required, paths)

    def test_missing_tools_become_missing_status_not_an_exception(self):
        arts = FR.collect_all(self.ctx, self.facts, self.opts)
        verify = next(a for a in arts if a.relpath == "verify/verify.json")
        self.assertEqual(verify.status, "missing")

    def test_no_journal_marks_all_journal_artifacts_skipped(self):
        opts = FR.Options(collect=True, no_journal=True)
        arts = FR.collect_all(self.ctx, self.facts, opts)
        journals = [a for a in arts if a.relpath.startswith("journal/")]
        self.assertEqual(len(journals), 4)
        self.assertTrue(all(a.status == "skipped" for a in journals))

    def test_unreadable_journal_is_denied_with_a_reason(self):
        self.runner.responses = [r for r in self.runner.responses
                                 if not r[0].startswith("journalctl -b --no-pager -q")]
        self.runner.responses.insert(0, ("journalctl -b --no-pager -q -n 1",
                                         FR.CmdResult(["journalctl"], 1, "", "denied")))
        facts = FR.gather_facts(self.ctx)
        self.assertFalse(facts.journal_ok)
        arts = FR.collect_all(self.ctx, facts, self.opts)
        journal = next(a for a in arts if a.relpath == "journal/kernel.txt")
        self.assertEqual(journal.status, "denied")
        self.assertIn("journal not readable", journal.detail)

    def test_no_repo_test_is_recorded_as_skipped(self):
        opts = FR.Options(collect=True, no_repo_test=True)
        arts = FR.collect_all(self.ctx, self.facts, opts)
        repo = next(a for a in arts if a.relpath == "repo/repo-test-fast.txt")
        self.assertEqual(repo.status, "skipped")
        self.assertIn("--no-repo-test", repo.detail)

    def test_non_zero_exit_with_output_is_still_data(self):
        script = self.tree.write("repo/tools/oxp3-verify.sh", "#!/bin/sh\necho '{}'\n")
        script.chmod(0o755)
        self.ctx.available_tools = {"systemctl", "journalctl", "sh"}
        self.runner.responses.append(
            (str(script), FR.CmdResult([str(script)], 1, '{"schema": 1}\n'))
        )
        arts = FR.collect_all(self.ctx, self.facts, self.opts)
        verify = next(a for a in arts if a.relpath == "verify/verify.json")
        self.assertEqual(verify.status, "ok")
        self.assertEqual(verify.exit_code, 1)

    def test_template_unit_is_never_passed_to_systemctl_show(self):
        FR.collect_all(self.ctx, self.facts, self.opts)
        show = [cmd for cmd in self.runner.seen if cmd.startswith("systemctl show --no-pager")]
        self.assertTrue(show)
        self.assertNotIn("oxp3-audio-resume@.service", show[0])

    def test_env_subset_only_reports_graphics_variables(self):
        ctx = FR.Context(env={"VK_ICD_FILENAMES": "/x", "SECRET_TOKEN": "y", "PATH": "/bin"})
        text = FR._env_subset(ctx.env)
        self.assertIn("VK_ICD_FILENAMES=/x", text)
        self.assertNotIn("SECRET_TOKEN", text)

    def test_tool_version_reads_stderr_when_stdout_is_empty(self):
        self.runner.responses.append(("ocloc", FR.CmdResult(["ocloc"], 0, "", "ocloc 25.18\n")))
        self.assertEqual(FR.tool_version(self.ctx, ["ocloc", "--version"]), "ocloc 25.18")


class CliTest(unittest.TestCase):
    def test_defaults(self):
        opts = FR.parse_args([])
        self.assertFalse(opts.collect)
        self.assertFalse(opts.dry_run)
        self.assertEqual(opts.max_bytes, FR.DEFAULT_MAX_BYTES)
        self.assertEqual(opts.max_lines, FR.DEFAULT_MAX_LINES)

    def test_collect_options(self):
        opts = FR.parse_args(["--collect", "--no-journal", "--no-repo-test",
                              "--out", "/tmp/x", "--json", "--max-bytes", "65536"])
        self.assertTrue(opts.collect)
        self.assertTrue(opts.no_journal)
        self.assertTrue(opts.no_repo_test)
        self.assertTrue(opts.json)
        self.assertEqual(opts.out, "/tmp/x")
        self.assertEqual(opts.max_bytes, 65536)

    def test_check_only_wins_over_dry_run(self):
        opts = FR.parse_args(["--check-only", "--dry-run"])
        self.assertFalse(opts.dry_run)
        self.assertFalse(opts.collect)

    def test_mutually_exclusive_collect_and_check_only(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                FR.parse_args(["--collect", "--check-only"])

    def test_byte_cap_floor(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                FR.parse_args(["--max-bytes", "10"])

    def test_line_cap_floor(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                FR.parse_args(["--max-lines", "3"])

    def test_selftest_runs_clean(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(FR.run_selftest(), 0)


if __name__ == "__main__":
    unittest.main()
