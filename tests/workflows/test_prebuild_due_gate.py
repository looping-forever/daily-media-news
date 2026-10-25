"""THE PREBUILD ARM MUST NOT DEMAND AN ARTIFACT BEFORE IT IS DUE.

DEFECT (one email/day, indefinitely). `.github/workflows/digest-watchdog.yml`'s
"Check prebuild artifact" arm resolved its deadline by taking the prebuild
cron's UTC TIME-OF-DAY and pasting it onto the wall clock's OWN utc date:

    today_utc = now_utc.replace(hour=utc_hour_of_day, minute=utc_minute_of_day)
    due_utc   = today_utc + PREBUILD_GRACE_MIN

GitHub-hosted scheduled runs on this repo measure 4.5-7h late (which is why the
Cloudflare Worker, not Actions, dispatches the prebuild now). The evening
prebuild cron is `55 12 * * *`, i.e. 18:40 NPT. Push that 7h late and the run
starts at 19:55 UTC, which is 01:40 NPT the FOLLOWING day: the NPT date has
already rolled over while the deadline is still being resolved on the PREVIOUS
utc date. `due_utc` lands in the past, `now_utc >= due_utc` is true on arrival,
and the arm walks straight past its own due gate to demand an evening artifact
for a slot that is not due for another ~18.5 hours.

The delivery arm was fixed for exactly this on 2026-10-05 and documents the
pattern at its `sched_utc` block; this test exists so the prebuild arm cannot
quietly lose the same guard.

WHAT IS PINNED, AND WHY IT IS TWO TESTS AND NOT ONE. A test that only proves
the arm stands down proves nothing on its own -- an arm that never judges
anything would pass it. So the second test drives a genuinely LATE run and
requires the arm to judge. Together they say: not early, and still loud.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = REPO_ROOT / ".github" / "workflows" / "digest-watchdog.yml"
SCHEDULE = REPO_ROOT / "deploy" / "schedule.json"

#: The committed prebuild crons, which the arm reads rather than any number
#: written in the test. `55 0 * * *` = 06:40 NPT, `55 12 * * *` = 18:40 NPT.
PREBUILD_CRON = {"morning": "55 0 * * *", "evening": "55 12 * * *"}
PREBUILD_GRACE_MIN = 90

#: THE DRIFTED INSTANT. 2026-10-15T19:55Z is the 18:40 NPT evening prebuild
#: running 7h 00m late: 19:55 UTC is 01:40 NPT on 2026-10-16, so the NPT date
#: has rolled and the evening slot for 2026-10-16 is due at 12:55+90m UTC on
#: 2026-10-16 -- roughly 18.5 hours after this run starts.
DRIFTED_NOW = "2026-10-15T19:55:00Z"

#: A run that is late but has NOT crossed midnight NPT. The evening prebuild is
#: due 12:55 UTC + 90 min = 14:25 UTC, so 14:40 is 15 minutes genuinely late
#: and the arm must judge it.
LATE_BUT_DUE_NOW = "2026-10-15T14:40:00Z"


def prebuild_arm_python():
    """The arm's own Python body, sliced out of the committed workflow.

    Sliced rather than re-implemented, for the same reason every other arm test
    in this suite slices: a private copy of the body would agree with itself
    forever while the shipped arm drifted. This runs the repository's own code.
    """
    import yaml

    with WATCHDOG.open(encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    steps = wf["jobs"]["watchdog"]["steps"]
    run = None
    for step in steps:
        name = str(step.get("name") or "")
        if name.startswith("Check prebuild artifact"):
            run = step.get("run")
            break
    if run is None:
        raise AssertionError(
            "digest-watchdog.yml has no 'Check prebuild artifact' step, so the "
            "prebuild arm this test judges no longer exists")
    lines = run.split("\n")
    start = None
    for i, line in enumerate(lines):
        if "<<'PY'" in line:
            start = i + 1
            break
    if start is None:
        raise AssertionError("the prebuild arm has no python3 heredoc to execute")
    for j in range(start, len(lines)):
        if lines[j].rstrip() == "PY":
            return "\n".join(lines[start:j])
    raise AssertionError("the prebuild arm's heredoc has no column-0 terminator")


class PrebuildDueGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prebuild-due-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        with SCHEDULE.open(encoding="utf-8") as fh:
            self.schedule = json.load(fh)

    def _run_arm(self, now, slot="evening", health_body="{}"):
        """Execute the arm at `now`; return (exit_code, stdout)."""
        work = Path(self.tmp) / "workspace"
        (work / "deploy").mkdir(parents=True, exist_ok=True)
        sched = dict(self.schedule)
        sched["dispatcher_crons"] = dict(sched.get("dispatcher_crons") or {})
        sched["dispatcher_crons"]["digest_prebuild"] = PREBUILD_CRON
        sched["health_url"] = "https://example.invalid/?health=1"
        (work / "deploy" / "schedule.json").write_text(
            json.dumps(sched, indent=2), encoding="utf-8")
        health = Path(self.tmp) / "health.json"
        health.write_text(health_body, encoding="utf-8")
        summary = Path(self.tmp) / "summary.md"
        summary.write_text("", encoding="utf-8")

        saved = dict(os.environ)
        cwd = os.getcwd()
        os.environ.update({
            "PREBUILD_SLOT": slot,
            "PREBUILD_HEALTH_URL": sched["health_url"],
            "PREBUILD_HEALTH_CODE": "200",
            "PREBUILD_HEALTH_CURL_RC": "0",
            "PREBUILD_HEALTH_FILE": str(health),
            "PREBUILD_SCHEDULE_FILE": "deploy/schedule.json",
            "PREBUILD_EVENT_NAME": "schedule",
            "WATCHDOG_NOW_UTC": now,
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_WORKSPACE": str(work),
            "CF_LEDGER_TOKEN": "",
            "CF_ACCOUNT_ID": "",
        })
        buf = io.StringIO()
        code = 0
        try:
            os.chdir(str(work))
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(prebuild_arm_python(), "<prebuild_arm>", "exec"),
                         {"__name__": "prebuild_arm"})
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        return code, buf.getvalue()

    def test_a_drifting_cron_does_NOT_demand_an_artifact_before_it_is_due(self):
        """THE DEFECT. The evening prebuild, dispatched on time and started 7h
        late, must stand down rather than demand the NEXT day's artifact.

        RED against the shipped arm: the old `today_utc = now_utc.replace(...)`
        resolved `due_utc` to 2026-10-15T14:25Z, `now_utc` was already
        2026-10-15T19:55Z, the gate passed, and the arm reported the 2026-10-16
        evening artifact ABSENT -- an email demanding a digest roughly 18.5
        hours before its slot was due.
        """
        code, out = self._run_arm(DRIFTED_NOW, slot="evening", health_body="{}")

        self.assertEqual(code, 0, (
            "the prebuild arm FAILED at %s, on the drifting cron schedule. The "
            "evening slot it is judging is due at %s UTC -- about 18.5 hours "
            "later -- so this arm must have no verdict at all.\n--- arm output ---\n%s"
            % (DRIFTED_NOW,
               "2026-10-16T14:25:00Z", out)))
        self.assertIn("is not due until", out, (
            "the arm did not take its due gate at all, so the guard is not the "
            "thing that stopped it:\n%s" % out))
        self.assertNotIn("artifact is ABSENT", out, (
            "THE DEFECT, LIVE. The arm demanded the evening artifact for a slot "
            "that is not due for another ~18.5 hours, because the deadline was "
            "resolved on the wall clock's utc date instead of the NPT date "
            "being judged. Cron drift pushed the run past 18:15 UTC and the NPT "
            "date rolled to the next day.\n--- arm output ---\n%s" % out))

    def test_a_genuinely_late_run_is_STILL_judged(self):
        """The other half, and the reason the first test means anything.

        An arm that never judges anything would pass the test above. 14:40 UTC
        is 15 minutes past the evening prebuild's 14:25 UTC deadline on the
        SAME NPT date, so this run really is late and the arm must say so --
        loudly, with a non-zero exit, which is what this arm does and what the
        other watchdog arms deliberately do not.
        """
        code, out = self._run_arm(LATE_BUT_DUE_NOW, slot="evening",
                                  health_body="{}")

        self.assertEqual(code, 1, (
            "a prebuild that IS due and sealed nothing must fail the job. An arm "
            "that cannot fail is decoration, and standing this down would trade "
            "a false page for a silent outage.\n--- arm output ---\n%s" % out))
        self.assertNotIn("is not due until", out, (
            "the arm stood down on a slot that was genuinely due at "
            "2026-10-15T14:25:00Z and judged at %s:\n%s"
            % (LATE_BUT_DUE_NOW, out)))


if __name__ == "__main__":
    unittest.main(verbosity=2)