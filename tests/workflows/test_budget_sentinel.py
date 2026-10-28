"""`budget-sentinel.yml` must obey the same ceiling switch as the static check.

WHY THIS FILE EXISTS
--------------------
`scripts/check_actions_budget.py` was changed to REPORT the hard ceiling instead
of failing it while `enforcement.enabled` is false: the repository is public, so
standard runners are free and a "budget" breach is information, not a bill. The
sentinel is the runtime half of the same policy and still exited 1 at 100% of the
ceiling, so the same month was red in one guard and green in the other.

The two states are the contract, and they pull in opposite directions:

  * DISARMED -> REPORT-ONLY AT EVERY LEVEL. At 80%, 90% and 100% of the ceiling
    it prints the measured figures and a `::warning::` annotation, adds the
    REPORT-ONLY line at 100%, files nothing, applies no `budget-freeze` label and
    sheds nothing, and exits 0. A freeze label for minutes nobody is billed for
    is a false alarm, and the 80/90 arms used to fall through to the issue.
  * ARMED -> the old behaviour, unchanged: an issue at 80%, an issue carrying the
    `budget-freeze` label at 90%, and ::error:: plus the issue plus exit 1 at 100%.

A BROKEN MEASUREMENT IS NOT A BUDGET BREACH. Unreadable run data and a malformed
config stay non-zero in BOTH states, because they say the number is UNKNOWN
rather than "high but harmless". Those two cases are tested here precisely so a
later edit cannot make the disarmed path swallow them too.

HOW IT DECIDES
--------------
The workflow's embedded Python is extracted from the YAML (between
`python3 - <<'PY'` and the closing `PY`), dedented, and run as a real script. The
ONE thing faked is the `subprocess` module: a prelude installs a stub in
`sys.modules` before the script's own `import subprocess`, so every `gh` call is
answered from a fixture and no real `gh`, issue, or notification is reachable.
Everything else -- the config parse, the per-job minute arithmetic, the level
thresholds, the prints and the exit codes -- is the shipped code.

A batch shim was the other option and was rejected: the script passes
`--created >=<iso-date>`, and an unquoted `>` is a redirection to cmd.exe, so the
stub would have failed for a reason unrelated to the thing under test.
"""

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "budget-sentinel.yml")

#: The prelude that runs before the extracted script. It must install the stub
#: BEFORE the script's `import subprocess` line, which `sys.modules` guarantees.
PRELUDE = r'''
import json as _json
import os as _os
import sys as _sys
import types as _types

_fixture = _json.load(open(_os.environ["DMNEWS_FAKE_GH"], encoding="utf-8"))
_log = _os.environ["DMNEWS_FAKE_LOG"]


class _Completed:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = "" if rc == 0 else "simulated gh failure"


def _run(args, **_kwargs):
    argv = [str(a) for a in args]
    with open(_log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(argv) + "\n")
    if argv[1:3] == ["run", "list"]:
        return _Completed(_fixture.get("run_list_rc", 0),
                          _json.dumps(_fixture["runs"]))
    if argv[1:2] == ["api"]:
        return _Completed(0, _json.dumps(_fixture["jobs"]))
    if argv[1:3] == ["issue", "list"]:
        return _Completed(0, "0")
    if argv[1:3] == ["issue", "create"]:
        return _Completed(0, "created")
    return _Completed(1)


_fake = _types.ModuleType("subprocess")
_fake.run = _run
_sys.modules["subprocess"] = _fake
'''

#: The level is a fraction of a 1800-minute ceiling, so the fixture is chosen by
#: arithmetic, not by picking a number:
#:   projected = runs * job_minutes / 7 * 30        (7-day window, 30-day month)
#:   6 x 60 = 360 min -> 1543 projected -> 86% of 1800 -> level 80
#:   7 x 55 = 385 min -> 1650 projected -> 92% of 1800 -> level 90
#:   8 x 60 = 480 min -> 2057 projected -> 114% of 1800 -> level 100
def make_runs(count):
    return [{"databaseId": 100 + i, "workflowName": "daily-news.yml",
             "createdAt": "2026-10-27T00:00:00Z"} for i in range(count)]


def make_jobs(minutes):
    started = datetime.datetime(2026, 10, 27, 0, 0, 0)
    finished = started + datetime.timedelta(minutes=minutes)
    return {"jobs": [{"started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "completed_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ")}]}


RUNS = make_runs(8)
JOBS = make_jobs(60)

BUDGET = {
    "included_minutes": 2000,
    "design_target_minutes": 1400,
    "hard_ceiling_minutes": 1800,
    "budget_reserve_minutes": 200,
    "other_repos_reserve_minutes": 600,
    "days_per_month": 30,
    "warn_ratio": 0.8,
    "freeze_ratio": 0.9,
    "workflows": {},
}


def embedded_python():
    """The `python3 - <<'PY' ... PY` block, dedented to column zero."""
    with open(WORKFLOW, encoding="utf-8") as handle:
        text = handle.read()
    opener = re.search(r"python3 - <<'PY'\s*\n", text)
    if not opener:
        raise AssertionError(
            "budget-sentinel.yml no longer carries a `python3 - <<'PY'` block, so "
            "this test cannot see the code it is guarding")
    rest = text[opener.end():]
    closer = re.search(r"^\s*PY\s*$", rest, re.M)
    if not closer:
        raise AssertionError("the heredoc in budget-sentinel.yml is not closed by "
                             "a `PY` line")
    return textwrap.dedent(rest[:closer.start()])


class SentinelCeilingTest(unittest.TestCase):
    """Both states of `enforcement.enabled`, driven through the real code."""

    def _budget(self, enabled, overrides=None):
        budget = dict(BUDGET)
        budget["enforcement"] = {
            "enabled": enabled,
            "reason": "public repository - standard runners are free",
        }
        budget.update(overrides or {})
        return budget

    def _run(self, budget, run_list_rc=0, config_text=None, runs=8,
             job_minutes=60):
        """Run the extracted script in a temp cwd; return (rc, stdout, gh calls)."""
        tmp = tempfile.mkdtemp(prefix="dmnews-sentinel-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        os.makedirs(os.path.join(tmp, "config"))

        if config_text is None:
            config_text = json.dumps(budget, indent=2)
        with open(os.path.join(tmp, "config", "actions-budget.json"), "w",
                  encoding="utf-8") as handle:
            handle.write(config_text)

        fixture_path = os.path.join(tmp, "gh.json")
        with open(fixture_path, "w", encoding="utf-8") as handle:
            json.dump({"runs": make_runs(runs), "jobs": make_jobs(job_minutes),
                       "run_list_rc": run_list_rc}, handle)
        log_path = os.path.join(tmp, "gh-calls.log")
        open(log_path, "w").close()

        script_path = os.path.join(tmp, "sentinel.py")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(PRELUDE)
            handle.write("\n")
            handle.write(embedded_python())

        env = os.environ.copy()
        env["DMNEWS_FAKE_GH"] = fixture_path
        env["DMNEWS_FAKE_LOG"] = log_path
        # A real ntfy endpoint must never be reachable from a test.
        env.pop("NTFY_TOPIC", None)
        env.pop("NTFY_SERVER", None)

        done = subprocess.run([sys.executable, script_path], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=120)
        with open(log_path, encoding="utf-8") as handle:
            calls = [line.strip() for line in handle if line.strip()]
        return done, calls

    def test_disarmed_over_the_ceiling_reports_and_exits_zero(self):
        """The whole point: a free minute cannot be "over budget"."""
        done, calls = self._run(self._budget(False))
        out = done.stdout

        self.assertEqual(
            0, done.returncode,
            "the sentinel failed while enforcement was OFF:\n%s\n%s"
            % (out, done.stderr))
        self.assertIn("REPORT-ONLY", out,
                      "the over-ceiling run did not say REPORT-ONLY:\n%s" % out)
        self.assertIn(
            "INFORMATIONAL, NOT A BLOCKER -- standard runners are free on a "
            "public repository", " ".join(out.split()),
            "the disarmed report must say, unmistakably, that standard runners are "
            "free on a public repository:\n%s" % out)
        self.assertNotIn("::error::", out,
                         "a disarmed over-ceiling run still emitted ::error:::\n%s"
                         % out)
        # It must still REPORT -- usage and ceiling -- not go quiet.
        self.assertIn("measured in window", out)
        self.assertIn("projected month", out)
        self.assertIn("hard ceiling:", out)
        self.assertIn("1800", out)
        self.assertFalse(
            [line for line in calls if line.startswith("gh issue create")],
            "an issue was filed for a ceiling that is not enforced:\n%s"
            % "\n".join(calls))

    def test_disarmed_at_eighty_percent_files_nothing(self):
        """80% filed an "information" issue. Disarmed, a warning is all it is."""
        done, calls = self._run(self._budget(False), runs=6, job_minutes=60)
        out = done.stdout

        self.assertEqual(
            0, done.returncode,
            "the sentinel failed at the 80%% level with enforcement OFF:\n%s\n%s"
            % (out, done.stderr))
        self.assertIn("80% of it", out,
                      "the run never reached the 80%% level, so this test proved "
                      "nothing:\n%s" % out)
        self.assertIn("REPORT-ONLY", out,
                      "a disarmed 80%% run did not say REPORT-ONLY:\n%s" % out)
        self.assertIn("::warning::", out,
                      "report-only must still warn; it must not go silent:\n%s" % out)
        self.assertFalse(
            [line for line in calls if line.startswith("gh issue")],
            "a disarmed 80%% run still talked to the issue API, so it is not "
            "report-only:\n%s" % "\n".join(calls))
        # Checked on the CALLS, not on stdout: the REPORT-ONLY sentence names the
        # label it is promising not to apply, so a stdout scan cannot tell
        # "explained" from "applied".
        joined = "\n".join(calls)
        self.assertNotIn(
            "--label", joined,
            "a disarmed run must label nothing, and `--label` reaches gh only "
            "when an issue is created:\n%s" % joined)
        self.assertNotIn(
            "budget-freeze", joined,
            "a disarmed 80%% run applied a freeze label:\n%s" % joined)
        # Notifying is an ACT too: the disarmed path must exit before it, and an
        # unset-topic run would say so on the way past.
        self.assertNotIn("NTFY secrets unset", out,
                         "the disarmed path reached the notification block:\n%s"
                         % out)

    def test_disarmed_at_ninety_percent_applies_no_freeze_label(self):
        """90% is the level that freezes. While disarmed it must not."""
        done, calls = self._run(self._budget(False), runs=7, job_minutes=55)
        out = done.stdout

        self.assertEqual(
            0, done.returncode,
            "the sentinel failed at the 90%% level with enforcement OFF:\n%s\n%s"
            % (out, done.stderr))
        self.assertIn("90% of it", out,
                      "the run never reached the 90%% level, so this test proved "
                      "nothing:\n%s" % out)
        self.assertIn("REPORT-ONLY", out, out)
        self.assertFalse(
            [line for line in calls if line.startswith("gh issue")],
            "a disarmed 90%% run still filed/queried issues:\n%s" % "\n".join(calls))
        joined = "\n".join(calls)
        self.assertNotIn(
            "--label", joined,
            "a disarmed run must label nothing:\n%s" % joined)
        self.assertNotIn(
            "budget-freeze", joined,
            "a disarmed 90%% run applied the freeze label, which is exactly the "
            "freeze it is not allowed to ask for:\n%s" % joined)
        self.assertNotIn("NTFY secrets unset", out,
                         "the disarmed path reached the notification block:\n%s"
                         % out)

    def test_armed_at_eighty_percent_files_a_plain_issue(self):
        """ARMED 80% is unchanged: the issue, with no freeze label on it."""
        done, calls = self._run(self._budget(True), runs=6, job_minutes=60)
        created = [line for line in calls if line.startswith("gh issue create")]
        # The body carries newlines, so the stub's one-line-per-call log splits
        # the create into several entries and `--label` is NOT on the first one.
        # Joining is what makes the label visible at all.
        joined = "\n".join(calls)

        self.assertEqual(
            0, done.returncode,
            "the armed 80%% level must not fail the job:\n%s\n%s"
            % (done.stdout, done.stderr))
        self.assertTrue(created,
                        "the armed 80%% run filed no issue:\n%s" % joined)
        self.assertIn("at 80% of the hard ceiling", created[0], created[0])
        self.assertIn("--label actions-budget", joined,
                      "the armed 80%% issue lost its plain label:\n%s" % joined)
        # The LABEL TOKEN, never the bare word: the 90% issue body itself says
        # "reports this issue's `budget-freeze` label", so a word search matches
        # prose and passes even when no label was applied.
        self.assertNotIn(
            "--label actions-budget,budget-freeze", joined,
            "the freeze label leaked down to the 80%% level:\n%s" % joined)

    def test_armed_at_ninety_percent_still_files_the_freeze_label(self):
        """ARMED 90% is unchanged: the issue the Worker reads for the freeze."""
        done, calls = self._run(self._budget(True), runs=7, job_minutes=55)
        created = [line for line in calls if line.startswith("gh issue create")]
        joined = "\n".join(calls)

        self.assertEqual(
            0, done.returncode,
            "the armed 90%% level must not fail the job:\n%s\n%s"
            % (done.stdout, done.stderr))
        self.assertTrue(created,
                        "the armed 90%% run filed no issue:\n%s" % joined)
        self.assertIn("at 90% of the hard ceiling", created[0], created[0])
        # The LABEL TOKEN, not the bare word: the body for 90% names the label
        # too, so this can only be proved by the argument gh was handed.
        self.assertIn(
            "--label actions-budget,budget-freeze", joined,
            "the armed 90%% issue lost the `budget-freeze` label the Worker reads "
            "on ?health=1:\n%s" % joined)

    def test_armed_over_the_ceiling_still_fails_unchanged(self):
        """Flipping the switch back must re-arm the old red, exactly as before."""
        done, calls = self._run(self._budget(True))
        out = done.stdout

        self.assertEqual(
            1, done.returncode,
            "the sentinel did not fail at 100%% with enforcement ARMED:\n%s\n%s"
            % (out, done.stderr))
        self.assertIn("::error::", out,
                      "the armed run did not emit ::error:::\n%s" % out)
        self.assertNotIn("REPORT-ONLY", out,
                         "the armed run reported instead of gating:\n%s" % out)
        self.assertTrue(
            [line for line in calls if line.startswith("gh issue create")],
            "the armed run did not file the budget issue:\n%s" % "\n".join(calls))

    def test_unreadable_run_data_is_non_zero_in_both_states(self):
        """"Unknown" is not "high but harmless", so the switch must not hide it."""
        for enabled in (False, True):
            with self.subTest(enforcement=enabled):
                done, _calls = self._run(self._budget(enabled), run_list_rc=1)
                self.assertNotEqual(
                    0, done.returncode,
                    "unreadable run data exited 0 with enforcement=%s, so a "
                    "measurement failure was swallowed:\n%s" % (enabled, done.stdout))
                self.assertIn("::error::gh run list", done.stdout,
                              "a failed measurement was not announced as an "
                              "::error:::\n%s" % done.stdout)

    def test_malformed_config_is_non_zero_in_both_states(self):
        for enabled in (False, True):
            with self.subTest(enforcement=enabled):
                done, _calls = self._run(self._budget(enabled),
                                         config_text="{ this is not json")
                self.assertNotEqual(
                    0, done.returncode,
                    "a malformed config exited 0 with enforcement=%s:\n%s"
                    % (enabled, done.stdout))
                self.assertIn("JSONDecodeError", done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
