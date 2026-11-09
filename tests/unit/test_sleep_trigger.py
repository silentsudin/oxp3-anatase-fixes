"""Offline tests for the suspend *trigger* policy (files/logind-oxp3.conf,
files/49-oxp3-suspend.rules) and for the way it is installed and checked.

The trigger policy exists because nothing on this host ever asked the kernel to
suspend (docs/hw/sleep-power.md 2.5): the logind drop-in makes logind itself
sleep on the power key, and the polkit rule keeps login1.suspend reachable when
no session authentication agent is running (the Steam Gamemode session has
none).

Everything here is file parsing plus a regex over the shipped shell tools: no
root, no D-Bus, no suspend, no host path is written.
"""

from __future__ import annotations

import configparser
import re
import unittest

import support

LOGIND = support.REPO / "files" / "logind-oxp3.conf"
POLKIT = support.REPO / "files" / "49-oxp3-suspend.rules"
INSTALLER = support.REPO / "tools" / "oxp3-hw-install.sh"
VERIFY = support.REPO / "tools" / "oxp3-verify.sh"

LOGIND_DEST = "/etc/systemd/logind.conf.d/60-oxp3-sleep.conf"
POLKIT_DEST = "/etc/polkit-1/rules.d/49-oxp3-suspend.rules"


def strip_js_comments(text: str) -> str:
    """Drop /* ... */ and // ... so a comment cannot satisfy a code assertion."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


class LogindDropInTest(unittest.TestCase):
    def setUp(self):
        self.text = LOGIND.read_text(encoding="utf-8")
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.read_string(self.text)

    def test_parses_as_ini_with_one_login_section(self):
        self.assertEqual(self.cfg.sections(), ["Login"])

    def test_power_and_suspend_key_request_suspend(self):
        self.assertEqual(self.cfg["Login"]["HandlePowerKey"], "suspend")
        self.assertEqual(self.cfg["Login"]["HandleSuspendKey"], "suspend")

    def test_lid_is_sleep_on_battery_and_ac_but_not_docked(self):
        login = self.cfg["Login"]
        self.assertEqual(login["HandleLidSwitch"], "suspend")
        self.assertEqual(login["HandleLidSwitchExternalPower"], "suspend")
        self.assertEqual(login["HandleLidSwitchDocked"], "ignore")

    def test_long_press_stays_poweroff(self):
        self.assertEqual(self.cfg["Login"]["HandlePowerKeyLongPress"], "poweroff")

    def test_no_key_is_wired_to_hibernate(self):
        # /sys/power/disk is [disabled] under the integrity lockdown and there is
        # no swap: a key that asks for hibernate can only fail in the log.
        login = self.cfg["Login"]
        self.assertNotIn("HandleHibernateKey", login)
        self.assertNotIn("HandleSuspendKeyLongPress", login)

    def test_no_duplicate_keys(self):
        keys = [ln.split("=", 1)[0].strip() for ln in self.text.splitlines()
                if "=" in ln and not ln.lstrip().startswith("#")]
        self.assertEqual(sorted(keys), sorted(set(keys)))

    def test_documented_as_installed_by_the_sleep_component(self):
        self.assertIn(LOGIND_DEST, self.text)


class PolkitRuleTest(unittest.TestCase):
    def setUp(self):
        self.raw = POLKIT.read_text(encoding="utf-8")
        self.code = strip_js_comments(self.raw)

    def test_is_one_rule(self):
        self.assertEqual(self.code.count("polkit.addRule("), 1)

    def test_braces_balance(self):
        self.assertEqual(self.code.count("{"), self.code.count("}"))

    def test_grants_suspend_and_hibernate_actions(self):
        for action in (
            "org.freedesktop.login1.suspend",
            "org.freedesktop.login1.suspend-multiple-sessions",
            "org.freedesktop.login1.hibernate",
            "org.freedesktop.login1.hibernate-multiple-sessions",
        ):
            self.assertIn(action, self.code)

    def test_returns_yes_and_falls_through_otherwise(self):
        self.assertIn("polkit.Result.YES", self.code)
        self.assertIn("polkit.Result.NOT_HANDLED", self.code)

    def test_requires_a_local_active_session(self):
        self.assertIn("subject.local", self.code)
        self.assertIn("subject.active", self.code)

    def test_restricted_to_the_sudo_capable_group(self):
        self.assertIn('subject.isInGroup("wheel")', self.code)

    def test_does_not_grant_the_inhibit_bypass(self):
        self.assertNotIn("suspend-ignore-inhibit", self.code)

    def test_documented_as_installed_by_the_sleep_component(self):
        self.assertIn(POLKIT_DEST, self.raw)


class InstallerWiringTest(unittest.TestCase):
    def setUp(self):
        self.text = INSTALLER.read_text(encoding="utf-8")

    def test_both_trigger_files_are_declared_in_one_table(self):
        table = re.search(r"TRIGGER_FILES=\((.*?)\n\)", self.text, re.S)
        self.assertIsNotNone(table, "TRIGGER_FILES table is missing")
        body = table.group(1)
        self.assertIn(f"files/logind-oxp3.conf:{LOGIND_DEST}", body)
        self.assertIn(f"files/49-oxp3-suspend.rules:{POLKIT_DEST}", body)

    def test_sleep_install_and_uninstall_both_use_the_table(self):
        install = self.text.split("comp_sleep_install()", 1)[1].split("comp_sleep_uninstall()", 1)[0]
        self.assertIn("maybe_install_trigger_policy", install)
        uninstall = self.text.split("comp_sleep_uninstall()", 1)[1].split("\ncomp_", 1)[0]
        self.assertIn('for spec in "${TRIGGER_FILES[@]}"', uninstall)

    def test_status_reports_the_two_files(self):
        status = self.text.split("print_status()", 1)[1]
        self.assertIn(LOGIND_DEST, status)
        self.assertIn(POLKIT_DEST, status)

    def test_installer_does_not_restart_logind_behind_the_user(self):
        # Restarting logind from a running desktop session drops every inhibitor
        # lock; the policy is applied on the next logind start instead.
        self.assertNotIn("systemctl restart systemd-logind", self.text)


class VerifyWiringTest(unittest.TestCase):
    def setUp(self):
        self.text = VERIFY.read_text(encoding="utf-8")

    def test_declares_the_read_only_trigger_check(self):
        self.assertRegex(self.text, r"record sleep\.trigger sleep")

    def test_real_suspend_needs_three_separate_opt_ins(self):
        gate = re.search(
            r"if \[ \"\$FULL\" -ne 1 \] \|\| \[ \"\$WITH_CHANGES\" -ne 1 \] \|\| "
            r"\[ \"\$\{OXP3_ALLOW_SUSPEND:-0\}\" != 1 \]",
            self.text,
        )
        self.assertIsNotNone(gate, "sleep.suspend is not gated on --full/--with-changes/OXP3_ALLOW_SUSPEND")

    def test_only_one_check_can_suspend(self):
        # The gated sleep.suspend is the single place that asks for a real cycle.
        self.assertEqual(self.text.count('"$audit" suspend -w'), 1)

    def test_trigger_verdict_covers_every_outcome(self):
        verdict = self.text.split("trigger_verdict()", 1)[1].split("\ncheck_sleep()", 1)[0]
        for keyword in ("skip-policy", "fail-blocked", "fail-incomplete",
                        "skip-unreadable", "fail-keys", "fail-auth", "'pass'"):
            self.assertIn(keyword, verdict)


if __name__ == "__main__":
    unittest.main()
