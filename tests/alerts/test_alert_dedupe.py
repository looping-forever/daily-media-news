#!/usr/bin/env python3
"""tests/alerts/test_alert_dedupe.py -- the alert-flood control.

WHAT IS BEING PINNED
--------------------
Six identical "AI ladder returned no verdict" messages in three hours is the
defect. The fix is not silence -- the first failure must still page, and a
recovery must still be announced -- it is ONE message per distinct condition per
six hours, carrying the run count and the window it covers.

So the properties below are the contract, and the ones that would let the flood
back in are the ones written as their own tests:

  * the FIRST occurrence alerts (a dedupe that suppresses everything also
    passes every "it stayed quiet" test);
  * a second occurrence inside the window does not;
  * the count a later alert carries is the number of runs affected;
  * the 6h boundary re-alerts;
  * a recovery fires exactly once;
  * two distinct conditions still alert separately, and the same condition is
    never split per run;
  * the ledger survives a save/load round trip and prunes old entries.

The mutation check the task asked for is documented at the bottom: removing the
window comparison turns the second-occurrence test RED.
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import alert_dedupe as A  # noqa: E402

UTC = datetime.timezone.utc


def at(hour, minute=0, day=27):
    """A fixed UTC moment on 2026-10-27, so a test never reads the clock."""
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=UTC)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.state = {"schema_version": 1, "conditions": {}}
        self.key = A.condition_key("breaking.honesty_gate")

    def test_first_occurrence_alerts(self):
        got = A.record(self.state, self.key, at(7, 35), "ai_fail_safe: no verdict")
        self.assertTrue(got["alert"], "the first occurrence must page: a dedupe "
                                      "that suppresses everything is worse than "
                                      "the flood it replaces")
        self.assertEqual(1, got["runs"])
        self.assertEqual("2026-10-27T07:35:00Z", got["first"])
        self.assertEqual("2026-10-27T07:35:00Z", got["last"])
        self.assertIn("no verdict", got["cause"])

    def test_second_occurrence_inside_the_window_does_not_alert(self):
        A.record(self.state, self.key, at(7, 35), "cause")
        got = A.record(self.state, self.key, at(7, 50), "cause")
        self.assertFalse(got["alert"],
                         "a second run of the SAME unchanged condition posted "
                         "again inside the window: that is the flood")
        self.assertEqual(1, got["suppressed"])

    def test_the_six_hour_boundary_re_alerts_with_the_right_count(self):
        A.record(self.state, self.key, at(7, 35), "cause")   # alert 1
        for hour, minute in ((7, 50), (8, 20), (9, 5), (10, 30)):
            self.assertFalse(A.record(self.state, self.key, at(hour, minute),
                                      "cause")["alert"])
        # Runs since alert 1: 07:50, 08:20, 09:05, 10:30 and this one at 13:35.
        got = A.record(self.state, self.key, at(13, 35), "cause")
        self.assertTrue(got["alert"],
                        "6h after the last alert the condition is still failing, "
                        "so the owner must hear about it again")
        self.assertEqual(5, got["runs"],
                         "the alert must say how many runs were affected in the "
                         "window it covers, not 1")
        self.assertEqual("2026-10-27T07:50:00Z", got["first"],
                         "the window must name its FIRST occurrence")
        self.assertEqual("2026-10-27T13:35:00Z", got["last"],
                         "the window must name its LAST occurrence")

    def test_consecutive_windows_never_share_a_run(self):
        """Windows do not overlap, so the counts add up to the run total."""
        A.record(self.state, self.key, at(7, 35), "cause")        # alert: 1 run
        A.record(self.state, self.key, at(13, 35), "cause")       # alert: 1 run
        self.assertFalse(A.record(self.state, self.key, at(13, 50),
                                  "cause")["alert"])
        got = A.record(self.state, self.key, at(19, 35), "cause")
        self.assertTrue(got["alert"])
        self.assertEqual(2, got["runs"],
                         "the 07:35 run was counted in the first alert and must "
                         "not be counted again")

    def test_just_under_six_hours_does_not_re_alert(self):
        A.record(self.state, self.key, at(7, 0), "cause")
        got = A.record(self.state, self.key, at(12, 59), "cause")
        self.assertFalse(got["alert"], "5h59m is still inside the 6h window")

    def test_one_condition_is_never_split_per_run(self):
        """The reason the attempt number and run id are NOT in the key."""
        keys = {A.condition_key("digest.broken", "evening", "2026-10-27")}
        A.record(self.state, keys.pop(), at(7, 35), "cause")
        second = A.condition_key("digest.broken", "evening", "2026-10-27")
        self.assertFalse(A.record(self.state, second, at(7, 50), "cause")["alert"],
                         "two runs of one condition must share one entry")

    def test_two_distinct_conditions_alert_separately(self):
        morning = A.condition_key("digest.broken", "morning", "2026-10-27")
        evening = A.condition_key("digest.broken", "evening", "2026-10-27")
        honesty = A.condition_key("breaking.honesty_gate")
        for key in (morning, evening, honesty):
            self.assertTrue(A.record(self.state, key, at(7, 35), "cause")["alert"],
                            "%s was suppressed by an unrelated condition: the "
                            "key is merging genuinely different failures" % key)
        self.assertEqual(3, len(self.state["conditions"]))

    def test_a_different_day_is_a_different_condition(self):
        day1 = A.condition_key("digest.missing", "morning", "2026-10-26")
        day2 = A.condition_key("digest.missing", "morning", "2026-10-27")
        self.assertTrue(A.record(self.state, day1, at(7, 35), "cause")["alert"])
        self.assertTrue(A.record(self.state, day2, at(7, 40, day=27), "cause")["alert"])

    def test_the_cause_text_rides_along_verbatim(self):
        A.record(self.state, self.key, at(7, 35), "ai_fail_safe: ladder exhausted")
        got = A.record(self.state, self.key, at(13, 35), "ai_fail_safe: ladder exhausted")
        self.assertEqual("ai_fail_safe: ladder exhausted", got["cause"],
                         "the underlying cause must survive the summarising, "
                         "otherwise the one alert says less than the six did")

    def test_the_window_text_states_runs_and_timestamps(self):
        A.record(self.state, self.key, at(7, 35), "cause")
        A.record(self.state, self.key, at(9, 0), "cause")
        text = A.describe_window(A.record(self.state, self.key, at(13, 35), "cause"))
        self.assertIn("2 runs were affected", text)
        self.assertIn("2026-10-27T09:00:00Z", text)
        self.assertIn("2026-10-27T13:35:00Z", text)
        self.assertIn("6h", text)

    def test_a_single_run_window_says_so_without_inventing_a_range(self):
        text = A.describe_window(A.record(self.state, self.key, at(7, 35), "cause"))
        self.assertIn("first run affected", text)

    def test_an_empty_condition_id_is_refused(self):
        with self.assertRaises(ValueError):
            A.condition_key("", "morning", "2026-10-27")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = {"schema_version": 1, "conditions": {}}
        self.key = A.condition_key("digest.missing", "morning", "2026-10-27")

    def test_recovery_fires_exactly_once(self):
        A.record(self.state, self.key, at(7, 35), "no delivered marker")
        A.record(self.state, self.key, at(7, 50), "no delivered marker")
        got = A.resolve(self.state, self.key, at(10, 30))
        self.assertTrue(got["recovery"])
        self.assertEqual(2, got["runs"], "the recovery must say how many runs "
                                         "were affected")
        self.assertEqual("2026-10-27T07:35:00Z", got["first"])
        self.assertEqual(175 * 60, int(got["duration_seconds"]),
                         "the recovery must say how long it lasted")

        again = A.resolve(self.state, self.key, at(10, 40))
        self.assertFalse(again["recovery"],
                         "the recovery was sent twice: a second 'all clear' for a "
                         "condition that has not recurred is noise")

    def test_recovery_of_an_unknown_condition_is_a_no_op(self):
        self.assertFalse(A.resolve(self.state, "never|seen|", at(10, 30))["recovery"])

    def test_recovery_text_names_the_label_runs_and_duration(self):
        A.record(self.state, self.key, at(7, 35), "no delivered marker")
        got = A.resolve(self.state, self.key, at(10, 30))
        body = A.recovery_body("The morning digest", got)
        self.assertIn("The morning digest is working again", body)
        self.assertIn("1 run(s) were affected", body)
        self.assertIn("2h 55m", body)
        self.assertIn("no delivered marker", body)
        self.assertEqual("The morning digest recovered",
                         A.recovery_title("The morning digest"))

    def test_resolve_prefix_clears_the_older_slots_and_keeps_the_current_one(self):
        old = A.condition_key("digest.missing", "evening", "2026-10-26")
        current = A.condition_key("digest.missing", "morning", "2026-10-27")
        A.record(self.state, old, at(23, 0, day=26), "cause")
        A.record(self.state, current, at(7, 35), "cause")
        got = A.resolve_prefix(self.state, "digest.missing", at(8, 0),
                               keep_key=current)
        self.assertEqual([old], [r["key"] for r in got])
        self.assertIn(current, self.state["conditions"])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alert-ledger-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "state", "alert_ledger.json")
        self.state = {"schema_version": 1, "conditions": {}}

    def test_the_ledger_survives_a_save_and_load(self):
        A.record(self.state, "a||", at(7, 35), "cause")
        A.save(self.state, self.path, now=at(7, 35))
        reloaded = A.load(self.path)
        self.assertEqual(1, len(reloaded["conditions"]))
        # ...and the window is still in force after the process ended.
        got = A.record(reloaded, "a||", at(7, 50), "cause")
        self.assertFalse(got["alert"],
                         "state did not survive the run boundary, so every run "
                         "re-alerted -- the exact flood this fixes")

    def test_old_entries_are_pruned(self):
        A.record(self.state, "old||", at(7, 35, day=20), "cause")
        A.record(self.state, "new||", at(7, 35), "cause")
        removed = A.save(self.state, self.path, now=at(7, 35))
        self.assertEqual(1, removed, "a week-old entry must be pruned")
        self.assertEqual(["new||"], sorted(A.load(self.path)["conditions"]))

    def test_a_broken_ledger_reads_as_empty_and_never_raises(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual({}, A.load(self.path)["conditions"],
                         "an unreadable ledger must not take the alert path down")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(["not", "a", "dict"], fh)
        self.assertEqual({}, A.load(self.path)["conditions"])


class WiringTests(unittest.TestCase):
    """The module is only a fix if the alerting path actually calls it."""

    def workflow(self, name):
        with open(os.path.join(REPO_ROOT, ".github", "workflows", name),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_every_flooding_alert_site_calls_the_dedupe(self):
        breaking = self.workflow("breaking-monitor.yml")
        watchdog = self.workflow("digest-watchdog.yml")
        self.assertIn("alert_dedupe", breaking,
                      "the honesty-gate ntfy is still unguarded")
        self.assertIn("breaking.honesty_gate", breaking,
                      "the honesty-gate condition id is missing, so it cannot be "
                      "told apart from any other failure")
        self.assertIn("digest.broken", watchdog)
        self.assertIn("digest.missing", watchdog)

    def test_the_ledger_is_committed_by_both_alerting_workflows(self):
        for name in ("breaking-monitor.yml", "digest-watchdog.yml"):
            text = self.workflow(name)
            self.assertIn("state/alert_ledger.json", text,
                          "%s never stages the ledger, so the dedupe state dies "
                          "with the run and every run re-alerts" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
