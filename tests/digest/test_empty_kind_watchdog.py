#!/usr/bin/env python3
"""tests/digest/test_empty_kind_watchdog.py — the third empty kind pages.

Run:
    python -m unittest discover -s tests/digest -t . -v

WHAT IS PINNED HERE
-------------------
`scripts/validate_stories.py` can now emit `validator_rejected_all`: the agent
produced stories and the validator discarded every one of them on its own
schema checks. `derive_empty_outcome` and its own tests prove the KIND is
computed correctly. This file proves the kind is ACTED ON, because a
classification nothing consumes is a string, not a page.

Two things must hold, and the second is the one that bites:

  * SEVERITY. `validator_rejected_all` is a priority-5 `::error::` from the
    first attempt. It is not a quiet day (the news was real) and it must not
    wait for the third attempt before it becomes loud -- on 2026-10-30 three
    usable stories were filed as `quiet_day` and nobody was told.
  * CLOSURE. Both hard kinds share ONE dedupe condition key, because the
    recovery step resolves exactly `digest.broken`. A second key would record
    an entry that nothing ever resolves, so the next real outage of the same
    slot+date would be silently suppressed for the whole window.

The alert body is EXECUTED, not grepped, with ntfy unset: the step then prints
its GitHub annotation and exits 0 without touching the network. That is how the
branch is chosen for real.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_TESTS, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from tests import workflows as W  # noqa: E402
from tests.workflows.test_workflow_lint import heredoc_bodies  # noqa: E402

WORKFLOW = "digest-watchdog.yml"
ALERT_STEP = "Alert on the empty digest"
RECOVERY_STEP = "Alert on the recovered digest"
KIND = "validator_rejected_all"
#: The condition both hard kinds are recorded under. The recovery step below
#: resolves this exact string.
CONDITION = "digest.broken"


def step_run(name):
    for _job, _step, step_dict in W.iter_all_steps(WORKFLOW):
        if _step == name:
            return step_dict["run"]
    raise AssertionError("%s has no step %r" % (WORKFLOW, name))


def step_dict(name):
    """The whole parsed step, so `env:` can be checked as well as `run:`."""
    for _job, _step, sd in W.iter_all_steps(WORKFLOW):
        if _step == name:
            return sd
    raise AssertionError("%s has no step %r" % (WORKFLOW, name))


def alert_body():
    bodies = [b for _t, b in heredoc_bodies(step_run(ALERT_STEP))]
    if not bodies:
        raise AssertionError(
            "%s carries no Python heredoc: the step was renamed or its body was "
            "replaced. Update this suite deliberately rather than deleting the "
            "check." % ALERT_STEP)
    return bodies[0]


def run_alert(env_extra):
    """Exec the alert heredoc with ntfy unset; return (exit_code, output).

    The step writes state/alert_ledger.json relative to the CWD, so it runs in
    a temp directory: a test must never touch the repository's own ledger.
    """
    tmp = tempfile.mkdtemp(prefix="empty-kind-watchdog-")
    try:
        os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("NTFY_TOPIC", "NTFY_SERVER")}
        env.update({
            "SLOT": "morning", "DATE": "2026-10-30",
            "ATTEMPT": "1", "MAX_ATTEMPTS": "3",
            "GITHUB_STEP_SUMMARY": os.path.join(tmp, "summary.md"),
        })
        env.update(env_extra)

        saved = dict(os.environ)
        prev = os.getcwd()
        buf = io.StringIO()
        code = 0
        try:
            os.chdir(tmp)
            os.environ.clear()
            os.environ.update(env)
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(alert_body(), "<alert-on-the-empty-digest>", "exec"),
                         {"__name__": "__main__"})
                except SystemExit as exc:
                    code = exc.code or 0
        finally:
            os.chdir(prev)
            os.environ.clear()
            os.environ.update(saved)
        return code, buf.getvalue()
    finally:
        shutil.rmtree(tmp, True)


def annotation(output):
    """The `::error::` / `::notice::` line a GitHub run summary would show."""
    for line in output.splitlines():
        if line.startswith("::error::") or line.startswith("::notice::"):
            return line
    return ""


class ValidatorRejectedAllPagesAtOnceTests(unittest.TestCase):
    def test_it_is_an_error_on_the_first_attempt(self):
        code, output = run_alert({"KIND": KIND})
        line = annotation(output)

        self.assertEqual(0, code, output)
        self.assertTrue(line, "the alert step printed no annotation at all:\n%s"
                        % output)
        self.assertTrue(line.startswith("::error::"),
                        "a content defect on attempt 1 was reported as %r. Every "
                        "produced story was thrown away; that is never a notice"
                        % line)

    def test_the_message_says_the_stories_were_real(self):
        _code, output = run_alert({"KIND": KIND})

        self.assertIn("DID produce", output)
        self.assertIn("defect in the pipeline", output)

    def test_the_kind_reaches_the_step_from_the_stored_ladder(self):
        """The chain the branch depends on: state -> output -> env -> body."""
        decision = step_run("Decide whether to retry")
        env = step_dict(ALERT_STEP).get("env") or {}

        self.assertIn("kind=kind", decision,
                      "the retry decision no longer publishes the stored "
                      "last_empty_kind, so every kind arrives as the default")
        self.assertIn("steps.retry.outputs.kind", str(env.get("KIND") or ""))
        self.assertIn("os.environ.get('KIND'", alert_body())

    def test_a_quiet_day_is_still_only_a_notice(self):
        """The fix must not turn a genuinely quiet day into an error."""
        _code, output = run_alert({"KIND": "quiet_day"})

        self.assertTrue(annotation(output).startswith("::notice::"),
                        "a quiet day now pages the owner: %r" % annotation(output))

    def test_a_broken_digest_keeps_its_own_title(self):
        _code, output = run_alert({"KIND": "broken"})

        self.assertIn("sources returned nothing usable", output)

    def test_the_final_attempt_still_outranks_the_cause(self):
        """On attempt 3 the fallback is firing; that leads, whatever the kind."""
        _code, output = run_alert({"KIND": KIND, "ATTEMPT": "3"})

        self.assertIn("FALLING BACK NOW", output)


class TheConditionIsClosedTests(unittest.TestCase):
    def test_both_hard_kinds_share_the_condition_the_recovery_resolves(self):
        body = alert_body()
        recovery = step_run(RECOVERY_STEP)

        self.assertIn("if kind in ('broken', %r)" % KIND, body,
                      "the new kind is not recorded by the dedupe, so a repeat "
                      "inside the 6h window pages twice")
        self.assertIn("condition_key(%r" % CONDITION, body)
        self.assertIn("condition_key(%r" % CONDITION, recovery,
                      "the recovery resolves a different key from the one the "
                      "alert records, so the ledger entry is never closed")

    def test_no_second_condition_key_is_invented(self):
        source = step_run(ALERT_STEP) + step_run(RECOVERY_STEP)
        keys = set(re.findall(r"condition_key\('([^']+)'", source))

        self.assertEqual({CONDITION}, keys)


if __name__ == "__main__":
    unittest.main()
