#!/usr/bin/env python3
"""tests/breaking/test_honesty_gate_recovery.py -- when may an all-clear be sent?

THE DEFECT THIS EXISTS FOR. On 2026-11-01 (run 41315549623, 14:18:52Z) the
honesty gate had been failing, and the owner was told "The breaking honesty gate
(AI ladder) recovered". Nothing had recovered: the run was a QUIET news hour --
the deterministic gate suppressed all 9 candidates, no model was asked anything,
and `state/breaking_log.json` recorded `"gate": "suppressed"`. The summary step
resolved the open condition on `elif last is not None`, i.e. on ANY row that was
not an `ai_fail_safe` row, so the absence of a failure was being read as the
presence of a verdict. The next genuinely broken run re-opened the condition,
and the owner received open/recover/open on alternate runs.

FOUR PROPERTIES, EACH PINNED HERE:

  1. A run that judged NOTHING is not evidence, in either direction. A
     suppressed sweep, a `--no-model` run and the hard-evidence fast path must
     leave an open honesty-gate failure OPEN.
  2. A recovery needs TWO CONSECUTIVE judged-healthy runs, and any failure in
     between resets the streak. One quiet run is never an all-clear.
  3. The recovery is sent ONCE. A third healthy run must not send a second one.
  4. The fallback channels are truthful: a Telegram delivery is recorded in the
     log row, and a delivered alert leaves no `pending_alert` marker behind.

Offline and stdlib-only. Property 4 uses the real `main()` with the same
injected fetch/model/SMTP/Telegram doubles the rest of this suite uses.

    py tests\\breaking\\test_honesty_gate_recovery.py
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import alert_dedupe as A  # noqa: E402
import breaking_monitor as bm  # noqa: E402

# The injected doubles and the run fixture live in the monitor suite that owns
# them, so this file exercises the real pipeline rather than a private copy.
from test_breaking_monitor import (  # noqa: E402
    QUAKE_VERDICT,
    TgRecorder,
    make_fetch,
    quake_body,
    read_json,
    run_main,
    telegram_env,
    verdict_runner,
)

WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "breaking-monitor.yml")
KEY = A.condition_key("breaking.honesty_gate")
NOW = datetime.datetime(2026, 11, 1, 14, 18, 52, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# The step under test is the workflow's OWN summary code, extracted verbatim.
# A Python re-implementation would agree with itself forever; this runs the
# exact bytes GitHub runs, against a temp state/ directory.
# ---------------------------------------------------------------------------


def summary_code():
    """The `python3 - <<'PY' ... PY` body of the summary step, dedented."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        src = fh.read()
    for match in re.finditer(r"<<\s*'PY'\s*\n", src):
        rest = src[match.end():]
        end = re.search(r"(?m)^[ \t]*PY[ \t]*$", rest)
        if end is None:
            continue
        body = textwrap.dedent(rest[:end.start()])
        if "honesty-gate RECOVERED" in body:
            return body
    raise AssertionError("breaking-monitor.yml has no summary step that can "
                         "announce an honesty-gate recovery")


def _suppressed_row():
    """What a quiet hour writes: gate suppressed, nothing judged."""
    return {"run_at": "2026-11-01T14:18:52Z", "checked": 9, "tripped": False,
            "gate": "suppressed", "judged": False,
            "gate_reason": "9 candidate(s) checked, none met the importance gate",
            "reason": ""}


def _judged_row():
    """What a working sweep writes: a model returned a verdict, no alert."""
    return {"run_at": "2026-11-01T14:18:52Z", "checked": 3, "tripped": True,
            "sent": False, "level": 1, "confidence": "LOW", "gate": "passed",
            "judged": True, "reason": "below_alert_level"}


def _failed_row(reason="ai_fail_safe: every model attempt failed"):
    return {"run_at": "2026-11-01T14:18:52Z", "checked": 3, "tripped": True,
            "sent": False, "level": 1, "gate": "passed", "judged": False,
            "unjudged_titles": ["Magnitude 6.8 earthquake near Example District"],
            "reason": reason}


class SummaryStep(unittest.TestCase):
    """Runs the workflow's summary step in a throwaway repository checkout."""

    CODE = summary_code()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="brk-summary-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "state"))
        self.ledger_path = os.path.join(self.tmp, "state", "alert_ledger.json")
        self.summary_path = os.path.join(self.tmp, "step-summary.md")

    # -- state helpers -----------------------------------------------------

    def seed_open_condition(self):
        """An honesty-gate failure that alerted one hour ago and is still open."""
        state = {"schema_version": A.SCHEMA_VERSION, "conditions": {}}
        A.record(state, KEY, NOW - datetime.timedelta(hours=1),
                 "ai_fail_safe: every model attempt timed out")
        A.save(state, self.ledger_path, now=NOW - datetime.timedelta(hours=1))

    def run_step(self, rows):
        """Write `rows` as the run's log, run the step, return (out, summary)."""
        with open(os.path.join(self.tmp, "state", "breaking_log.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rows, fh)
        if os.path.exists(self.summary_path):
            os.remove(self.summary_path)
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": SCRIPTS + (os.pathsep + env["PYTHONPATH"]
                                     if env.get("PYTHONPATH") else ""),
            "GITHUB_STEP_SUMMARY": self.summary_path,
            "PAYLOAD_TITLE": "",
            "TODAY_NPT": "",
            "NTFY_TOPIC": "",
            "NTFY_SERVER": "",
        })
        proc = subprocess.run([sys.executable, "-c", self.CODE], cwd=self.tmp,
                              env=env, capture_output=True, text=True,
                              timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(proc.returncode, 0, out)
        summary = ""
        if os.path.exists(self.summary_path):
            with open(self.summary_path, encoding="utf-8") as fh:
                summary = fh.read()
        return out, summary

    def ledger(self):
        return A.load(self.ledger_path)

    # -- 1. a run that judged nothing is not evidence ----------------------

    def test_quiet_hour_does_not_resolve_an_open_failure(self):
        self.seed_open_condition()
        out, summary = self.run_step([_suppressed_row()])
        self.assertNotIn("RECOVERED", out,
                         "a gate-suppressed sweep must never be read as a "
                         "recovery: nothing was judged, so nothing was proven")
        self.assertIn(KEY, self.ledger()["conditions"],
                      "the open condition must survive a run that judged nothing")
        self.assertIn("NOT resolved", summary)

    def test_two_quiet_hours_still_do_not_resolve(self):
        self.seed_open_condition()
        for _ in range(2):
            out, _summary = self.run_step([_suppressed_row()])
            self.assertNotIn("RECOVERED", out)
        entry = self.ledger()["conditions"][KEY]
        self.assertNotIn("healthy_streak", entry,
                         "a suppressed run must not advance the healthy streak")
        self.assertEqual(entry["runs"], 1)

    # -- 2. two consecutive judged runs, then a recovery -------------------

    def test_recovery_needs_two_consecutive_judged_healthy_runs(self):
        self.seed_open_condition()

        out, summary = self.run_step([_judged_row()])
        self.assertNotIn("RECOVERED", out,
                         "one judged-healthy run is not an all-clear")
        self.assertEqual(self.ledger()["conditions"][KEY]["healthy_streak"], 1)
        self.assertIn("judged-healthy run 1 of 2", out)
        self.assertIn("waiting for 1/2", summary)

        out, _summary = self.run_step([_judged_row()])
        self.assertEqual(out.count("honesty-gate RECOVERED"), 1)
        self.assertNotIn(KEY, self.ledger()["conditions"])

        # 3. ... and it is sent exactly once: a third healthy run is silent.
        out, _summary = self.run_step([_judged_row()])
        self.assertNotIn("RECOVERED", out,
                         "the recovery message must not be sent twice")

    def test_a_failure_between_healthy_runs_resets_the_streak(self):
        self.seed_open_condition()
        self.run_step([_judged_row()])
        out, _summary = self.run_step([_failed_row()])
        self.assertNotIn("RECOVERED", out)
        self.run_step([_judged_row()])
        self.assertEqual(
            self.ledger()["conditions"][KEY]["healthy_streak"], 1,
            "the streak must restart after an intervening failure, so recovery "
            "still needs two CONSECUTIVE judged-healthy runs")
        out, _summary = self.run_step([_judged_row()])
        self.assertEqual(out.count("honesty-gate RECOVERED"), 1)

    # -- 4. recovery_body states the rule the owner is relying on ----------

    def test_recovery_body_says_the_streak_was_measured(self):
        state = {"schema_version": A.SCHEMA_VERSION, "conditions": {}}
        A.record(state, KEY, NOW - datetime.timedelta(hours=2), "cause")
        recovery = A.resolve(state, KEY, NOW, required=2)
        self.assertFalse(recovery["recovery"])
        recovery = A.resolve(state, KEY, NOW, required=2)
        self.assertTrue(recovery["recovery"])
        body = A.recovery_body("The breaking honesty gate (AI ladder)", recovery)
        self.assertIn("Confirmed by 2 consecutive healthy runs", body)
        self.assertIn("single quiet run is never treated as a recovery", body)

        # The default keeps the one-call contract the digest arms rely on.
        state = {"schema_version": A.SCHEMA_VERSION, "conditions": {}}
        A.record(state, KEY, NOW - datetime.timedelta(hours=2), "cause")
        self.assertTrue(A.resolve(state, KEY, NOW)["recovery"])


class FallbackDelivery(unittest.TestCase):
    """A confirmed Telegram push is a delivery, and says so in the log row."""

    def setUp(self):
        os.environ.setdefault("GMAIL_USER", "test@example.invalid")
        os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")

    def test_telegram_delivery_is_recorded_and_leaves_no_pending_marker(self):
        sd = tempfile.mkdtemp(prefix="brk-tg-")
        self.addCleanup(shutil.rmtree, sd, True)
        alert_id = bm.stable_alert_id(QUAKE_VERDICT)
        state_path = os.path.join(sd, "breaking_state.json")
        bm.write_json_atomic(state_path, {"pending_alert": {alert_id: "2026-11-01"}})

        restore_env = telegram_env(token="123:OK", chat_id="777")
        saved = (os.environ.pop("NTFY_TOPIC", None), os.environ.pop("NTFY_SERVER", None))
        orig_http = bm.tg_http
        rec = TgRecorder(send_ok=True)
        bm.tg_http = rec
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            self.assertEqual(rc, 0)
            self.assertEqual(len(rec.sends()), 1)
            row = read_json(os.path.join(sd, "breaking_log.json"))[-1]
            self.assertIn("telegram_delivered", row["reason"],
                          "a confirmed Telegram send must be visible in the log "
                          "row, not silently indistinguishable from email")
            self.assertEqual(
                read_json(state_path)["pending_alert"], {},
                "a delivered alert must leave no pending_alert marker behind")
        finally:
            bm.tg_http = orig_http
            restore_env()
            for name, value in zip(("NTFY_TOPIC", "NTFY_SERVER"), saved):
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value


class BudgetArithmetic(unittest.TestCase):
    """The declared budget must contain the worst case it claims to cap."""

    def test_worst_case_is_inside_the_declared_total(self):
        # ONE 60s window per model. Attempts are capped inside that window, so a
        # retry can never widen the bound -- that is the whole change.
        worst_case = bm.AI_MAX_MODELS * bm.AI_PER_ATTEMPT_TIMEOUT_SEC
        self.assertEqual(worst_case, 480,
                         "8 models x one 60s window each is the production shape")
        self.assertEqual(bm.AI_TOTAL_BUDGET_SEC, worst_case,
                         "the budget must SUM the ladder it bounds; a total "
                         "smaller than the worst case bounds nothing")
        self.assertGreaterEqual(bm.AI_ATTEMPTS_PER_MODEL, 2,
                                "a fast transport failure must still get its "
                                "contracted retry inside the same window")
        self.assertLess(bm.AI_TOTAL_BUDGET_SEC, 15 * 60,
                        "the workflow's 15-minute job timeout must still have "
                        "room for collection and delivery after the ladder")


class ModelWindow(unittest.TestCase):
    """A retry is paid for out of the model's own 60s, never on top of it."""

    def test_a_real_timeout_is_not_re_asked_and_the_next_rung_is_reached(self):
        import time as _time

        calls = []

        def slow(model, prompt_text, env, timeout_sec):
            calls.append((model, timeout_sec))
            _time.sleep(timeout_sec)
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout_sec)

        sd = tempfile.mkdtemp(prefix="brk-window-")
        self.addCleanup(shutil.rmtree, sd, True)
        pending = os.path.join(sd, "breaking_pending.json")
        with open(pending, "w", encoding="utf-8") as fh:
            fh.write("{}")
        saved = (bm.AI_TOTAL_BUDGET_SEC, bm.AI_PER_ATTEMPT_TIMEOUT_SEC)
        bm.AI_TOTAL_BUDGET_SEC, bm.AI_PER_ATTEMPT_TIMEOUT_SEC = 30, 1
        try:
            bm.ai_classify(pending, runner=slow, models=["p/a", "p/b"])
        finally:
            bm.AI_TOTAL_BUDGET_SEC, bm.AI_PER_ATTEMPT_TIMEOUT_SEC = saved

        self.assertEqual([m for m, _t in calls], ["p/a", "p/b"],
                         "a timed-out model has already spent its window, so it "
                         "must not be re-asked -- and the next rung must still "
                         "be reached")
        self.assertTrue(all(t <= 1 for _m, t in calls), calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
