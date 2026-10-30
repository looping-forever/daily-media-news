#!/usr/bin/env python3
"""tests/digest/test_dispatch_slot_date.py -- a manual dispatch with no date.

WHAT IS BEING GUARDED
---------------------
Run 41064520752 (2026-10-30) was `gh workflow run daily-news.yml` with the
workflow's own defaults. It died in its FIRST step:

    FATAL: the slot date could not be established: this run names no slot_date
    and has no scheduled firing to be dated from; a dispatch must say which day
    its slot belongs to

That refusal was written for the DISPATCHER, which always sends `slot_date`, and
it made the documented manual entry point unusable. The replacement is a rule,
not a tolerance: an undated dispatch means "build this slot now", so it is dated
to TODAY in Nepal (UTC+05:45).

WHY THE CLOCK IS INJECTED
-------------------------
The two defects this must not reintroduce both turn on which side of Nepal
midnight a run happens to start:

  * 2026-10-26: a 14:00 NPT dispatch was dated to the PREVIOUS day, because the
    only date the code could find was the last firing that had already passed.
  * 2026-10-17T18:16:53Z: an `evening` dispatch at 00:01 NPT on the 18th took
    the 18th from the wall clock, 19 hours before its own slot.

A test that reads this machine's clock can only be green at some hours and red
at others, so the step takes `SLOT_DATE_NOW_UTC` (the convention
digest-watchdog.yml already uses for `WATCHDOG_NOW_UTC`). Production never sets
it.

WHY THIS IS EXECUTED, NOT READ
------------------------------
The step is shell plus a Python heredoc. Reading it cannot prove that
`today=$(...)` captured the date rather than the NOTE this change prints next to
it, and it cannot prove the scheduled arm still walks back to its firing. Both
are run here, against the real step body.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
for _p in (_TESTS, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests import workflows as W  # noqa: E402

WORKFLOW = "daily-news.yml"
STEP = "Determine date, digest time and trigger source"


def step_run():
    for _job, name, sd in W.iter_all_steps(WORKFLOW):
        if name == STEP:
            return sd["run"]
    raise AssertionError(
        "%s has no step %r. The step was renamed, so this suite can no longer "
        "tell you what a dateless dispatch is dated to -- update it "
        "deliberately rather than deleting it." % (WORKFLOW, STEP))


def python3_on_path(tmp):
    """A PATH carrying a `python3` shim onto this interpreter.

    The step runs a real Python heredoc, and this platform has no `python3`
    (`python` is a Store shim that exits 9009). A skip instead of a shim would
    leave the whole step unexercised while the suite still reported green.
    """
    bin_dir = Path(tmp) / "bin"
    W.install_stub_on_path(
        bin_dir, '#!/usr/bin/env bash\nexec "%s" "$@"\n' % sys.executable,
        name="python3")
    return "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))


def gh_env(path):
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if "=" in line:
                key, _sep, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


class DispatchSlotDate(unittest.TestCase):
    """The step's real shell, run on a pinned clock."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="slot-date-")
        self.tmp = Path(self._tmp)
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.out = self.tmp / "gh_output.txt"
        self.out.write_text("", encoding="utf-8")
        self.env_file = self.tmp / "gh_env.txt"
        self.env_file.write_text("", encoding="utf-8")

    def run_step(self, now_utc, **overrides):
        env = {
            # A manual dispatch: no `github.event.schedule` at all.
            "EVENT_NAME": "workflow_dispatch",
            "EVENT_SCHEDULE": "",
            "INPUT_DIGEST_TIME": "evening",
            # The input the workflow binds from github.event.inputs.slot_date.
            # Absent means absent: an empty string, exactly as Actions passes it.
            "INPUT_SLOT_DATE": "",
            "INPUT_PHASE": "full",
            "INPUT_DRY_RUN": "",
            "INPUT_RETRY_SLOT": "",
            "INPUT_RETRY_OF": "",
            "INPUT_TRIGGER_SOURCE": "",
            "GITHUB_OUTPUT": str(self.out),
            "GITHUB_ENV": str(self.env_file),
            "SLOT_DATE_NOW_UTC": now_utc,
            "PATH": python3_on_path(self.tmp),
        }
        env.update(overrides)
        proc = W.run_bash_script(step_run(), env_overrides=env, cwd=str(self.tmp))
        return (proc.returncode,
                (proc.stdout or "") + (proc.stderr or ""),
                gh_env(self.env_file))

    def test_an_undated_dispatch_is_dated_to_today_in_nepal(self):
        """The defect: this used to exit 1 before doing anything at all."""
        code, out, env = self.run_step("2026-10-30T12:00:00Z")
        self.assertEqual(0, code,
                         "a dateless manual dispatch still failed:\n" + out)
        self.assertEqual("2026-10-30", env.get("TODAY"),
                         "a dateless dispatch was not dated to today: %r\n%s"
                         % (env, out))
        self.assertEqual("evening", env.get("DIGEST_TIME"))

    def test_after_nepal_midnight_the_date_is_still_nepal_today(self):
        """18:20Z on the 29th is 00:05 NPT on the 30th: today is the 30th.

        Taking the UTC date here is the 2026-10-17T18:16:53Z defect, which filed
        an evening digest 19 hours before its own slot.
        """
        code, out, env = self.run_step("2026-10-29T18:20:00Z")
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-30", env.get("TODAY"),
                         "an undated dispatch after Nepal midnight was dated %r, "
                         "not the Nepal date:\n%s" % (env.get("TODAY"), out))

    def test_it_says_out_loud_that_it_guessed_today(self):
        code, out, _env = self.run_step("2026-10-30T12:00:00Z")
        self.assertEqual(0, code, out)
        self.assertIn("NOTE: no slot_date input", out,
                      "the run claimed a date without saying where it came "
                      "from:\n" + out)
        # And the NOTE must not be what `today=$(...)` captured.
        self.assertNotIn("NOTE", _env.get("TODAY", ""),
                         "the NOTE leaked into TODAY:\n" + out)

    def test_an_explicit_slot_date_still_wins_outright(self):
        """The dispatcher's own path: the named date, even when the clock has
        already moved past midnight in Nepal."""
        code, out, env = self.run_step("2026-10-18T00:16:00Z",
                                       INPUT_SLOT_DATE="2026-10-17")
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-17", env.get("TODAY"),
                         "an explicit slot_date was overridden:\n" + out)
        self.assertNotIn("NOTE: no slot_date input", out)

    def test_the_scheduled_path_is_unchanged(self):
        """A cron firing is still dated from the firing, not from the clock.

        18:20Z on the 17th is 00:05 NPT on the 18th, and the 14:05Z firing is
        the 17th's: delay moves the job, it does not renumber the slot.
        """
        code, out, env = self.run_step(
            "2026-10-17T18:20:00Z",
            EVENT_NAME="schedule",
            EVENT_SCHEDULE="5 14 * * *",
            INPUT_DIGEST_TIME="",
        )
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-17", env.get("TODAY"),
                         "the scheduled path no longer derives its date from "
                         "the firing:\n" + out)
        self.assertEqual("evening", env.get("DIGEST_TIME"),
                         "the slot name no longer comes from the schedule:\n" + out)
        self.assertNotIn("NOTE: no slot_date input", out)

    def test_a_named_slot_date_that_is_not_a_date_is_still_fatal(self):
        """A typo must stop the run: 2026-02-31 is not a day anyone can build."""
        code, out, _env = self.run_step("2026-10-30T12:00:00Z",
                                        INPUT_SLOT_DATE="2026-02-31")
        self.assertNotEqual(0, code, "an impossible slot_date was accepted:\n" + out)
        self.assertIn("FATAL", out)


if __name__ == "__main__":
    unittest.main()
