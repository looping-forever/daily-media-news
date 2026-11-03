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
    ntfy_env,
    quake_body,
    read_json,
    run_main,
    telegram_env,
    verdict_runner,
)

WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "breaking-monitor.yml")
KEY = A.condition_key("breaking.honesty_gate")
NOW = datetime.datetime(2026, 11, 1, 14, 18, 52, tzinfo=datetime.timezone.utc)

# Nepal is UTC+05:45 all year, so a quiet-hours assertion is about the OWNER's
# wall clock, not about UTC.
NPT = datetime.timezone(datetime.timedelta(hours=5, minutes=45))


def npt(day, hour, minute=0):
    """The aware NPT instant for a 2026-11-<day> HH:MM Nepal wall clock."""
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=NPT)


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


def run_summary_step(code, cwd, rows, summary_path, now_utc):
    """Run the workflow's summary code against a temp state/ directory.

    The clock is PINNED through alert_dedupe's test seam, never left to the wall
    clock: the step now holds a notice during quiet hours, so an unpinned run at
    23:00 NPT would change what every test in this file asserts.
    """
    with open(os.path.join(cwd, "state", "breaking_log.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rows, fh)
    if os.path.exists(summary_path):
        os.remove(summary_path)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": SCRIPTS + (os.pathsep + env["PYTHONPATH"]
                                 if env.get("PYTHONPATH") else ""),
        "GITHUB_STEP_SUMMARY": summary_path,
        "PAYLOAD_TITLE": "",
        "TODAY_NPT": "",
        "NTFY_TOPIC": "",
        "NTFY_SERVER": "",
        "ALERT_DEDUPE_NOW_UTC": now_utc.astimezone(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    proc = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    out = (proc.stdout or "") + (proc.stderr or "")
    summary = ""
    if os.path.exists(summary_path):
        with open(summary_path, encoding="utf-8") as fh:
            summary = fh.read()
    return proc.returncode, out, summary


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

    def run_step(self, rows, now_utc=None):
        """Write `rows` as the run's log, run the step, return (out, summary).

        The clock is PINNED, never left to the wall clock: this step now holds
        a notice during the owner's quiet hours, so a real run at 23:00 NPT
        would otherwise change what every test here asserts. NOW is 20:03 NPT,
        outside quiet hours, so the existing expectations are the daytime ones.
        """
        code, out, summary = run_summary_step(
            self.CODE, self.tmp, rows, self.summary_path, now_utc or NOW)
        self.assertEqual(code, 0, out)
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


class QuietHours(unittest.TestCase):
    """The summary step inside the owner's sleep window (22:00-07:00 NPT).

    A notice that would wake the owner is HELD, not dropped: it is written to
    state/quiet_queue.json and delivered once, in the morning batch, by
    scripts/breaking_monitor.py.
    """

    CODE = summary_code()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="brk-quiet-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "state"))
        self.ledger_path = os.path.join(self.tmp, "state", "alert_ledger.json")
        self.queue_path = os.path.join(self.tmp, "state", "quiet_queue.json")
        self.summary_path = os.path.join(self.tmp, "step-summary.md")

    def run_step(self, rows, now_utc):
        code, out, summary = run_summary_step(
            self.CODE, self.tmp, rows, self.summary_path, now_utc)
        self.assertEqual(code, 0, out)
        return out, summary

    def seed_open_condition(self):
        state = {"schema_version": A.SCHEMA_VERSION, "conditions": {}}
        A.record(state, KEY, npt(3, 22, 30), "ai_fail_safe: every model "
                 "attempt timed out")
        A.save(state, self.ledger_path, now=npt(3, 22, 30))

    def queue(self):
        return A.load_quiet_queue(self.queue_path)

    def test_a_failure_at_2300_npt_is_held_not_pushed(self):
        out, summary = self.run_step([_failed_row()], npt(3, 23, 0))
        self.assertIn("quiet hours, held 1 item(s) until 07:00 NPT", out,
                      "the run must SAY it held the notice: %s" % out[-800:])
        self.assertNotIn("ntfy_not_configured", out,
                         "the quiet branch must be taken BEFORE any push attempt")
        self.assertIn("held 1 item(s) until 07:00 NPT", summary)
        items = self.queue()["items"]
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0]["kind"], "honesty_gate")
        self.assertEqual(items[0]["condition"], KEY,
                         "the held item belongs to the same condition the "
                         "ledger tracks, so a later report supersedes it")
        self.assertTrue(items[0]["title"].startswith(
            "Breaking honesty gate FAILED"), items[0]["title"])

    def test_the_held_notice_is_delivered_by_the_monitor_at_0700(self):
        self.run_step([_failed_row()], npt(3, 23, 0))
        self.addCleanup(ntfy_env())
        sent = []

        def recorder(url, payload):
            sent.append(payload)
            return 200, "{}"

        note = bm.flush_quiet_queue({"quiet_queue": self.queue_path},
                                    now_utc=npt(4, 7, 0), http_fn=recorder)
        self.assertIn("delivered", note)
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("Held overnight", sent[0]["title"])
        self.assertIn("2026-11-03 23:00", sent[0]["message"])
        self.assertIn("Breaking honesty gate FAILED", sent[0]["message"])
        self.assertEqual(self.queue()["items"], [])

    def test_a_held_recovery_is_superseded_by_a_later_failure(self):
        self.seed_open_condition()
        # Two judged-healthy runs at 23:00 produce the recovery -- and it is
        # HELD, because it would otherwise wake the owner.
        self.run_step([_judged_row()], npt(3, 23, 0))
        out, _summary = self.run_step([_judged_row()], npt(3, 23, 10))
        self.assertNotIn("recovery notice delivered", out)
        self.assertIn("quiet hours, held", out)
        self.assertEqual(self.queue()["items"][0]["kind"],
                         "honesty_gate_recovery")

        # ... and the ladder breaks again at 05:00, the same night.
        self.run_step([_failed_row()], npt(4, 5, 0))
        doc = self.queue()
        self.assertEqual(len(doc["items"]), 1,
                         "one condition holds ONE item: %r" % (doc["items"],))
        self.assertEqual(doc["items"][0]["kind"], "honesty_gate")
        self.assertEqual(doc["superseded"], 1)

        self.addCleanup(ntfy_env())
        sent = []

        def recorder(url, payload):
            sent.append(payload)
            return 200, "{}"

        bm.flush_quiet_queue({"quiet_queue": self.queue_path},
                             now_utc=npt(4, 7, 0), http_fn=recorder)
        message = sent[0]["message"]
        self.assertIn("FAILED", message)
        self.assertNotIn("recovered", message.lower(),
                         "the morning batch must not claim a recovery that a "
                         "later failure revoked")
        self.assertIn("superseded", message.lower())


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
    """The declared budget must be USABLE and still fit the job's clock."""

    def test_the_window_and_the_total_are_the_agreed_numbers(self):
        # A free rung needs ~150s: measured 127.53s to a whole answer and
        # 137.63s to a FIRST token. At 60s the 2026-11-01 ladder timed out on
        # every rung; at 120s the slower rung could not start.
        self.assertEqual(bm.AI_PER_ATTEMPT_TIMEOUT_SEC, 150,
                         "the free models do not answer inside 120s")
        self.assertEqual(bm.AI_TOTAL_BUDGET_SEC, 720,
                         "the stage wall is 720s (four rungs x 150s + slack)")
        self.assertGreaterEqual(bm.AI_ATTEMPTS_PER_MODEL, 2,
                                "a fast transport failure must still get its "
                                "contracted retry inside the same window")
        # The total is a FIXED wall, not AI_MAX_MODELS x per-attempt: a
        # product-shaped budget would have to grow with the catalogue, and the
        # job's 900s cannot.
        self.assertNotEqual(
            bm.AI_TOTAL_BUDGET_SEC,
            bm.AI_MAX_MODELS * bm.AI_PER_ATTEMPT_TIMEOUT_SEC,
            "a product-shaped budget cannot fit the 15-minute job")
        # 720s of ladder + ~63s of collection/delivery/commit < the job's 900s.
        self.assertLess(bm.AI_TOTAL_BUDGET_SEC + 63, 15 * 60,
                        "the workflow's 15-minute job timeout must still have "
                        "room for collection and delivery after the ladder")

    def test_four_windows_fit_the_wall_and_the_nine_hundred_second_job(self):
        """4 x 150 = 600 < 720 < 900.

        The pool size is what makes the wall honest: with six rungs the fifth
        and sixth could never get a full window, so the arithmetic would be a
        coincidence rather than a bound.
        """
        self.assertEqual(len(bm.AI_LADDER), 4, bm.AI_LADDER)
        self.assertEqual(bm.AI_MAX_MODELS, 4,
                         "the cap must equal the pool the wall was sized for")
        self.assertEqual(tuple(bm.AI_LADDER), tuple(bm.AI_MODELS[:4]),
                         "the pool must be the head of the reachable ladder")
        spent = len(bm.AI_LADDER) * bm.AI_PER_ATTEMPT_TIMEOUT_SEC
        self.assertEqual(spent, 600)
        self.assertLess(spent, bm.AI_TOTAL_BUDGET_SEC,
                        "four full windows must fit inside the stage wall")
        self.assertLess(bm.AI_TOTAL_BUDGET_SEC, 15 * 60,
                        "the stage wall must fit inside the job")
        self.assertGreaterEqual(bm.AI_TOTAL_BUDGET_SEC - spent, 60,
                                "discovery and slack need room after the pool")


class ModelWindow(unittest.TestCase):
    """A retry is paid for out of the model's own 150s, never on top of it."""

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

    def test_a_fast_transport_failure_still_gets_its_retry(self):
        """The retry is for a dropped connection, and it must still happen.

        A model that fails in a second has 149s of its window left, so the
        second attempt is affordable and gets the FULL allowance. This is the
        half of the contract the timeout test cannot show -- and the half a
        future "simplify the loop" edit would quietly delete.
        """
        calls = []

        def flaky(model, prompt_text, env, timeout_sec):
            calls.append((model, timeout_sec))
            raise ConnectionResetError("connection reset by peer")

        sd = tempfile.mkdtemp(prefix="brk-fastfail-")
        self.addCleanup(shutil.rmtree, sd, True)
        pending = os.path.join(sd, "breaking_pending.json")
        with open(pending, "w", encoding="utf-8") as fh:
            fh.write("{}")

        bm.ai_classify(pending, runner=flaky, models=["p/only"],
                       attempts_per_model=2)
        self.assertEqual([m for m, _t in calls], ["p/only", "p/only"],
                         "a fast transport failure must be retried once")
        self.assertEqual([t for _m, t in calls],
                         [bm.AI_PER_ATTEMPT_TIMEOUT_SEC] * 2,
                         "both attempts are inside the same 150s window")


if __name__ == "__main__":
    unittest.main(verbosity=2)
