"""The watchdog issue path and the Gmail/Telegram off-switch.

Two small contracts:
  * an alert arm must file an issue without depending on ntfy, and a healthy
    run must match none of the alert guards;
  * a channel disabled by decision must not look like a channel that failed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import textwrap
import unittest
from unittest import mock

from tests import workflows as W

WATCHDOG = W.load_workflow_text(W.raw_workflow_text("digest-watchdog.yml"))
DAILY = W.load_workflow_text(W.raw_workflow_text("daily-news.yml"))


def step_named(job, name):
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError("step %r not found" % name)


class WatchdogIssueStepTests(unittest.TestCase):
    def setUp(self):
        self.job = WATCHDOG["jobs"]["watchdog"]
        self.issue = step_named(self.job, "Open a GitHub issue on failure")

    def test_it_reuses_every_existing_alert_guard(self):
        for step_name in ("Alert on academic monitor health",
                          "Alert on worker heartbeat",
                          "Alert on breaking dispatch chain",
                          "Alert on missed digest"):
            alert = step_named(self.job, step_name)
            self.assertIn(alert["if"], self.issue["if"],
                          "the issue step must copy the %s guard verbatim" % step_name)
        self.assertIn("always()", self.issue["if"],
                      "it must still run after a failing alert step")

    def test_it_can_create_and_comment_but_never_close_or_delete(self):
        run = self.issue["run"]
        self.assertIn("gh issue create", run)
        self.assertIn("gh issue comment", run)
        self.assertIn("--state open", run)
        self.assertIn('echo "issue: $url', run)
        self.assertNotIn("gh issue close", run)
        self.assertNotIn("gh issue delete", run)

    def test_the_job_has_issue_permission(self):
        self.assertEqual("write", self.job["permissions"]["issues"])
        self.assertEqual("write", self.job["permissions"]["contents"])


class BackupChannelSwitchTests(unittest.TestCase):
    def setUp(self):
        self.steps = DAILY["jobs"]["generate-digest"]["steps"]

    def test_gmail_and_telegram_are_behind_one_constant(self):
        self.assertEqual("false", DAILY["env"]["BACKUP_CHANNELS_ENABLED"])
        for name in ("Send digest via Gmail (backup)",
                     "Send Telegram digest (backup)"):
            step = step_named(DAILY["jobs"]["generate-digest"], name)
            self.assertIn("env.BACKUP_CHANNELS_ENABLED == 'true'", step["if"])

    def test_the_step_names_are_unchanged(self):
        names = [step.get("name") for step in self.steps]
        self.assertIn("Publish to ntfy (primary)", names)
        self.assertIn("Send digest via Gmail (backup)", names)
        self.assertIn("Send Telegram digest (backup)", names)

    def test_disabled_channels_are_recorded_as_disabled_not_failed(self):
        marker = self.run_marker(backup_enabled="false")
        self.assertFalse(marker["channels"]["gmail"])
        self.assertFalse(marker["channels"]["telegram"])
        self.assertEqual("disabled by decision",
                         marker["channel_reasons"]["gmail"])
        self.assertEqual("disabled by decision",
                         marker["channel_reasons"]["telegram"])
        self.assertTrue(marker["delivered"],
                        "the primary channels still delivered the digest")

    def test_enabled_but_missing_flags_are_not_called_disabled(self):
        marker = self.run_marker(backup_enabled="true", flags=("website", "ntfy"))
        self.assertEqual("not delivered", marker["channel_reasons"]["gmail"])
        self.assertEqual("not delivered", marker["channel_reasons"]["telegram"])

    def run_marker(self, backup_enabled, flags=("website", "ntfy", "gmail", "telegram")):
        step = step_named(DAILY["jobs"]["generate-digest"],
                          "Record delivery status")
        match = re.search(r"python3 - <<'PY'\n(.*?)\nPY\s*$", step["run"],
                          re.DOTALL)
        self.assertIsNotNone(match, "Record delivery status has no Python body")
        code = match.group(1)
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "state"))
            with open(os.path.join(tmp, "state", "stories.validated.json"),
                      "w", encoding="utf-8") as fh:
                json.dump({"stories": [{"title": "s"}],
                           "stories_outcome": "produced"}, fh)
            for name in flags:
                with open(os.path.join(tmp, "state", ".%s_ok" % name),
                          "w", encoding="utf-8") as fh:
                    fh.write("true\n")
            env = {
                "DIGEST_TIME": "morning",
                "TODAY": "2026-11-15",
                "DRY_RUN": "false",
                "PHASE": "full",
                "BACKUP_CHANNELS_ENABLED": backup_enabled,
            }
            old = os.getcwd()
            try:
                os.chdir(tmp)
                with mock.patch.dict(os.environ, env, clear=False):
                    exec(compile(textwrap.dedent(code), "record_marker.py", "exec"),
                         {"__name__": "__main__"})
                with open("state/last_digest_sent.json", encoding="utf-8") as fh:
                    return json.load(fh)
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
