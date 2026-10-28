#!/usr/bin/env python3
"""tests/digest/test_simulated_push_guards.py — a proof push must not become a delivery.

THE INCIDENT, MEASURED ON 2026-10-28
------------------------------------
A proof run was dispatched against `slot_date=2026-10-29` so it could not touch
the day it ran on. It pushed a real notification under the slot's own sequence
id and wrote this into the delivery ledger:

    state: sent   ntfy_sent_at: 2026-10-28T14:44:00Z   on_time: false

2026-10-29's morning slot is due 01:35Z, so that instant is TEN HOURS FIFTY-ONE
MINUTES before the slot it is filed under. Every dedupe layer in the repository
asked "is there a row with a push instant?" and answered yes, which means at
07:20 on the 29th the ladder would have found "already sent" and delivered
NOTHING. The test meant to prove the delivery path worked would have been the
cause of the first silent missed digest.

THE FIX HAS TWO HALVES, AND THIS FILE TESTS BOTH
------------------------------------------------
1. THE MARKER. A simulated push's title begins with `TEST · `, and that marker is
   the row's identity: the title is the only ntfy field a later read-back can
   still see, so a test message is recognised by its title and skipped, not
   counted as the slot's delivery. The title is also measured AFTER the marker is
   reserved, which is why a 75-character `unsummarised edition (no AI today)`
   title is no longer sent at 82 characters with a prefix bolted on.

2. THE DRIFT RULE. A row only proves a delivery if its push instant is not
   earlier than its slot's own due instant by more than the early slack. That
   slack is 30 minutes, not seconds, because the repository already holds a real
   delivery 9m55s early (2026-10-13T13:25:05Z for the 13:35Z evening slot): a
   tight slack tells the truth about the proof row and tells a lie about the
   history, and `test_duplicate_detector` pins that history.
   That makes the already-written 2026-10-29 row INERT without rewriting
   production KV, which is the property test
   `test_the_2026_10_29_row_cannot_suppress_tomorrows_real_push` pins.

WHAT THIS FILE DOES NOT CLAIM
-----------------------------
It does not claim the phone received anything. The end-to-end half execs the
step's own bytes against a fake KV and a fake ntfy, so it proves what the step
POSTed and what it recorded; the live proof run is the only evidence about the
phone, and it is reported separately.
"""

import ast
import contextlib
import datetime
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
for _p in (_TESTS, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.digest.test_duplicate_detector import (  # noqa: E402
    ActionsHarness, ndjson)
from tests.workflows.test_workflow_lint import heredoc_bodies  # noqa: E402
from tests import workflows as W  # noqa: E402

WORKFLOW = "daily-news.yml"
NTFY_STEP = "Publish to ntfy (primary)"

#: The constants the extracted helpers read. Named, not guessed: a rename makes
#: `_helpers()` fail loudly instead of silently testing an empty namespace.
CONSTANTS = ("simulate", "TEST_TITLE_PREFIX", "SLOT_DUE_UTC", "SLOT_EARLY_SLACK_S")
FUNCTIONS = ("iso_utc", "instant_or_none", "slot_due_instant", "slot_was_served",
             "is_test_push", "fit_title", "provenance_patch")


def ntfy_body():
    """The ntfy step's first Python heredoc — its real bytes, never a copy."""
    for _job, name, step in W.iter_all_steps(WORKFLOW):
        if name == NTFY_STEP:
            bodies = [b for _tag, b in heredoc_bodies(step["run"])]
            if not bodies:
                raise AssertionError("%r has no Python heredoc" % NTFY_STEP)
            return bodies[0]
    raise AssertionError("%s has no step %r" % (WORKFLOW, NTFY_STEP))


def _helpers():
    """Exec ONLY the pure helpers out of the heredoc.

    The whole step cannot be exec'd here: its last statement is `main()`, which
    needs a topic, a payload and a KV. Slicing the AST keeps this file fast and
    still runs the repository's real bytes -- a copy of these functions could
    drift from the workflow, which is the failure this file exists to prevent.
    """
    tree = ast.parse(ntfy_body())
    picked = []
    seen = set()
    for node in tree.body:
        name = None
        if isinstance(node, ast.FunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            name = targets[0] if len(targets) == 1 else None
        if name in FUNCTIONS or name in CONSTANTS:
            picked.append(node)
            seen.add(name)
    missing = (set(FUNCTIONS) | set(CONSTANTS)) - seen
    if missing:
        raise AssertionError(
            "the ntfy step no longer defines %s. A helper that moved was not "
            "deleted by accident -- update this file deliberately."
            % ", ".join(sorted(missing)))
    module = {"__name__": "ntfy_helpers", "datetime": datetime, "os": os,
              "json": json}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "<ntfy-helpers>",
                 "exec"), module)
    return module


class FitTitleTests(unittest.TestCase):
    """`fit_title` is the arithmetic that makes the 72-character limit hold."""

    def setUp(self):
        self.m = _helpers()
        self.fit = self.m["fit_title"]
        self.prefix = self.m["TEST_TITLE_PREFIX"]

    def test_the_parenthetical_goes_first_and_whole(self):
        # The observed 75-character title, byte for byte (`\u2600\ufe0f` is the
        # two-character sun the push really uses, not a one-character lookalike).
        title = "\u2600\ufe0f Morning Digest \u00b7 07:20 \u00b7 2026-10-29 \u00b7 unsummarised edition (no AI today)"
        self.assertEqual(75, len(title))
        got = self.fit(title, 72)
        self.assertEqual(
            "\u2600\ufe0f Morning Digest \u00b7 07:20 \u00b7 2026-10-29 \u00b7 unsummarised edition",
            got)
        self.assertLessEqual(len(got), 72)
        self.assertNotIn("(no AI", got,
                         "half a parenthetical is worse than none of it")

    def test_after_the_parenthetical_the_last_clause_goes_whole(self):
        title = ("\u2600 Morning Digest \u00b7 07:20 \u00b7 2026-10-29 \u00b7 "
                 "unsummarised edition \u00b7 3 deadlines due")
        got = self.fit(title, 72)
        self.assertLessEqual(len(got), 72)
        self.assertTrue(got.endswith("unsummarised edition"), got)
        self.assertNotIn("deadlines", got)

    def test_no_label_and_no_date_can_push_the_title_past_the_limit(self):
        labels = ["Midnight", "Late Night", "Dawn", "Morning", "Noon",
                  "Afternoon", "Evening", "Night"]
        for label in labels:
            for date in ("2026-10-09", "2026-12-31", "2027-01-01"):
                body = ("\u2600 %s Digest \u00b7 23:59 \u00b7 %s \u00b7 "
                        "unsummarised edition (no AI today) \u00b7 12 deadlines due"
                        % (label, date))
                plain = self.fit(body, 72)
                self.assertLessEqual(len(plain), 72, (label, date, plain))
                marked = self.fit(self.prefix + self.fit(body, 72 - len(self.prefix)), 72)
                self.assertLessEqual(len(marked), 72, (label, date, marked))
                self.assertTrue(marked.startswith(self.prefix), marked)

    def test_the_marker_is_never_what_gets_cut(self):
        # A reason clause long enough to make the fit interesting.
        body = ("\u2600 Late Night Digest \u00b7 23:59 \u00b7 2026-10-29 \u00b7 "
                "12 items \u00b7 12 deadlines due")
        got = self.fit(self.prefix + self.fit(body, 72 - len(self.prefix)), 72)
        self.assertTrue(got.startswith("TEST \u00b7 "),
                        "the marker is the push's IDENTITY: dropping it is a "
                        "dedupe defect, not a cosmetic one: %r" % got)
        self.assertLessEqual(len(got), 72)

    def test_a_short_title_is_returned_unchanged(self):
        self.assertEqual("short", self.fit("  short  ", 72))


class SlotWasServedTests(unittest.TestCase):
    """The one predicate every dedupe in the workflow uses, on its own."""

    def setUp(self):
        self.m = _helpers()
        self.served = self.m["slot_was_served"]

    def test_a_real_ontime_push_is_a_delivery(self):
        self.assertTrue(self.served({
            "state": "sent", "kind": "delivered",
            "ntfy_sent_at": "2026-10-29T01:35:12Z",
            "scheduled_instant": "2026-10-29T01:35:00Z",
        }, "2026-10-29", "morning"))

    def test_a_simulated_row_is_never_a_delivery(self):
        for flag in ("simulated", "dry_run"):
            self.assertFalse(self.served({
                "state": "sent", "kind": "unsummarised",
                flag: True,
                "ntfy_sent_at": "2026-10-29T01:35:12Z",
                "scheduled_instant": "2026-10-29T01:35:00Z",
            }, "2026-10-29", "morning"),
                "%s: true is evidence about the CODE, not about the slot" % flag)

    def test_the_2026_10_29_row_is_inert_without_a_recorded_scheduled_instant(self):
        """The already-written row, with no flag and no scheduled_instant.

        Only the due table can show that 14:44Z the day before is not the 01:35Z
        slot's push. This is the property that makes a KV rewrite unnecessary.
        """
        row = {"state": "sent", "kind": "unsummarised",
               "ntfy_sent_at": "2026-10-28T14:44:00Z", "on_time": False}
        self.assertFalse(self.served(row, "2026-10-29", "morning"))

    def test_a_recorded_scheduled_instant_outranks_the_table(self):
        # A slot the table knows, filed under a day the table cannot judge.
        self.assertTrue(self.served({
            "ntfy_sent_at": "2026-10-29T13:35:10Z",
            "scheduled_instant": "2026-10-29T13:35:00Z",
        }, "2026-10-29", "evening"))
        self.assertFalse(self.served({
            "ntfy_sent_at": "2026-10-29T01:00:00Z",
            "scheduled_instant": "2026-10-29T01:35:00Z",
        }, "2026-10-29", "morning"))

    def test_inside_the_early_slack_still_counts(self):
        slack = self.m["SLOT_EARLY_SLACK_S"]
        self.assertEqual(
            30 * 60, slack,
            "30 minutes, not 60 seconds: 2026-10-13T13:25:05Z is a REAL delivery "
            "9m55s before its 13:35Z slot, and a 60s slack calls it a mislabel "
            "and pushes a second copy")

        def early(seconds):
            due = datetime.datetime(2026, 10, 29, 1, 35,
                                    tzinfo=datetime.timezone.utc)
            return (due - datetime.timedelta(seconds=seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        self.assertTrue(self.served({"ntfy_sent_at": early(slack - 1)},
                                    "2026-10-29", "morning"),
                        "a send inside the slack is this slot's delivery")
        self.assertTrue(self.served({"ntfy_sent_at": early(595)},
                                    "2026-10-29", "morning"),
                        "9m55s early is the measured real case, not a drift")
        self.assertFalse(self.served({"ntfy_sent_at": early(slack + 1)},
                                     "2026-10-29", "morning"),
                         "a send outside it is a drift, not this slot's push")

    def test_a_row_with_nothing_readable_is_not_evidence(self):
        self.assertFalse(self.served({"state": "sent"}, "2026-10-29", "morning"))
        self.assertFalse(self.served({"ntfy_sent_at": "not a date"},
                                     "2026-10-29", "morning"))
        self.assertFalse(self.served(None, "2026-10-29", "morning"))
        self.assertFalse(self.served("sent", "2026-10-29", "morning"))

    def test_the_field_argument_is_honoured(self):
        row = {"ntfy_sent_at": "2026-10-29T01:35:10Z",
               "ntfy_full_sent_at": "2026-10-29T01:40:00Z"}
        self.assertTrue(self.served(row, "2026-10-29", "morning"))
        self.assertTrue(self.served(row, "2026-10-29", "morning",
                                    "ntfy_full_sent_at"))
        self.assertFalse(self.served({"ntfy_sent_at": "2026-10-29T01:35:10Z"},
                                     "2026-10-29", "morning",
                                     "ntfy_full_sent_at"))

    def test_an_unknown_slot_cannot_be_judged_and_does_not_suppress(self):
        # No due instant to contradict the row: the historical behaviour, kept
        # on purpose, because refusing here would be a NEW double-push path.
        self.assertTrue(self.served({"ntfy_sent_at": "2026-10-29T01:35:10Z"},
                                    "2026-10-29", "afternoon"))


class ProvenancePatchTests(unittest.TestCase):
    """Who wrote the row, when the slot was due, and whether it was real."""

    def setUp(self):
        self.m = _helpers()

    def _patch(self, env, simulate):
        saved = dict(os.environ)
        try:
            os.environ.update(env)
            self.m["simulate"] = simulate
            return self.m["provenance_patch"]("2026-10-29", "morning")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_a_real_row_records_its_source_and_the_due_instant(self):
        patch = self._patch({"TRIGGER_SOURCE": "worker"}, False)
        self.assertEqual("worker", patch["trigger_source"])
        self.assertEqual("2026-10-29T01:35:00Z", patch["scheduled_instant"])
        self.assertNotIn("simulated", patch)
        self.assertNotIn(
            "phase", patch,
            "the payload must be identical at every phase, so this step is not "
            "given one and a row must not claim to know it")

    def test_a_simulated_row_is_flagged_and_nothing_else_is_added(self):
        patch = self._patch({}, True)
        self.assertIs(True, patch["simulated"])
        self.assertEqual("unknown", patch["trigger_source"])

    def test_the_due_instant_comes_from_the_table_not_the_clock(self):
        patch = self._patch({}, False)
        self.assertEqual("2026-10-29T01:35:00Z", patch["scheduled_instant"])


class WiringTests(unittest.TestCase):
    """Structural pins: the helpers are USED, not merely defined."""

    def setUp(self):
        self.body = ntfy_body()
        self.worker = _read_worker()

    def test_the_duplicate_guard_asks_slot_was_served(self):
        self.assertIn(
            "if not slot_was_served(previous, today, kind, guard_field):", self.body,
            "the duplicate guard went back to asking 'is there a push instant?'. "
            "That question is what read 2026-10-29's 14:44Z proof row as a "
            "delivery and would have silenced tomorrow's 07:20.")
        self.assertNotIn(
            "if entry.get('ntfy_sent_at'):", self.body,
            "a bare ntfy_sent_at test is back in the step; a simulated or "
            "early-dated row passes it")

    def test_the_read_back_skips_a_test_titled_message(self):
        self.assertIn("tests_only", self.body,
                      "the read-back no longer filters TEST titles")
        self.assertIn("is_test_push(", self.body)

    def test_the_worker_skips_a_test_titled_message_too(self):
        self.assertIn("function isTestPushTitle(", self.worker)
        self.assertIn("if (isTestPushTitle(message.title))", self.worker)

    def test_the_marker_is_reserved_before_anything_is_measured(self):
        reserved = self.body.index("title_budget = TITLE_MAX_CHARS - len(test_prefix)")
        parenthetical = self.body.index("suffix = ' (no AI today)'")
        self.assertLess(
            reserved, parenthetical,
            "the marker must be taken out of the budget BEFORE the optional "
            "clauses are measured, or the marker rides on top of a full title "
            "and the result is the 82-character push the row itself rejects")

    def test_the_final_title_is_clamped_last(self):
        self.assertIn(
            "title = fit_title(test_prefix + fit_title(title, title_budget),",
            self.body,
            "the clamp is the guarantee; without it every earlier check is a "
            "promise about a string that is then modified")

    def test_every_row_recording_carries_its_provenance(self):
        self.assertEqual(
            2, self.body.count("record_delivery(today, kind"),
            "both record_delivery call sites must carry the provenance patch")
        self.assertIn(
            "stale_simulated = () if simulate else ('simulated',)", self.body,
            "`clear` is applied AFTER the patch, so clearing `simulated` "
            "unconditionally deletes the marker a proof run just set: the row "
            "then looks like a real delivery to every reader.")


class SimulatedPushEndToEnd(ActionsHarness):
    """The step's own bytes, against a fake KV and a fake ntfy."""

    #: The payload is a 30-story unsummarised edition: what a pool refusal builds.
    def _unsummarised_payload(self):
        payload = dict(self.payload)
        payload["edition_kind"] = "unsummarised"
        payload["date"] = "2026-10-29"
        payload["digest_time"] = "morning"
        payload["stories"] = [dict(payload["stories"][0], title="STORY %02d" % i)
                              for i in range(1, 31)]
        return payload

    def _write_payload(self, payload):
        (self.tmp / "state" / "stories.validated.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def _env(self, **over):
        env = {"TODAY": "2026-10-29", "DIGEST_TIME": "morning"}
        env.update(over)
        return env

    def _row(self):
        """The stored row for THIS test's slot, not the parent module's default."""
        stored = self.kv_stored() or {}
        return ((stored.get("days") or {}).get("2026-10-29") or {}).get("morning") or {}

    def test_a_simulated_push_is_signed_and_fits_the_shade(self):
        self._write_payload(self._unsummarised_payload())
        out = self.run_step(env_overrides=self._env(SIMULATE_POOL_REFUSAL="true"),
                            history_body="")
        self.assertEqual(1, len(self.posts), out)
        payload = json.loads(self.posted_payload())
        title = payload["title"]
        self.assertTrue(title.startswith("TEST \u00b7 "),
                        "the push is not identifiable as a test: %r" % title)
        self.assertLessEqual(
            len(title), 72,
            "the title is %d characters, past the 72-character shade: %r"
            % (len(title), title))
        self.assertNotIn("(no AI today)", title,
                         "the parenthetical must go before the title goes over")
        self.assertIn("TEST", payload["message"],
                      "the body must carry the marker too: a collapsed "
                      "notification hides the title")
        entry = self._row()
        self.assertIs(True, entry.get("simulated"),
                      "the row a proof run writes must say it is one: %r" % entry)
        self.assertEqual("2026-10-29T01:35:00Z",
                         entry.get("scheduled_instant"))

    def test_the_2026_10_29_row_cannot_suppress_tomorrows_real_push(self):
        """The row exactly as run 40708709729 wrote it: no flag, no schedule."""
        self._write_payload(self._unsummarised_payload())
        row = {"state": "sent", "kind": "unsummarised",
               "sequence_id": "dmn-2026-10-29-morning",
               "ntfy_sent_at": "2026-10-28T14:44:00Z", "on_time": False}
        out = self.run_step(
            env_overrides=self._env(),
            history_body="",
            seed_ledger={"schema_version": 1, "days": {"2026-10-29": {"morning": row}}})
        self.assertEqual(
            1, len(self.posts),
            "the 14:44Z proof row suppressed the real 07:20 delivery. The row "
            "is 10h51m earlier than the slot it is filed under, so it cannot be "
            "that slot's push.\n%s" % out)

    def test_a_real_push_retires_an_inherited_simulated_flag(self):
        self._write_payload(self._unsummarised_payload())
        row = {"state": "sent", "kind": "unsummarised", "simulated": True,
               "ntfy_sent_at": "2026-10-29T01:35:05Z",
               "scheduled_instant": "2026-10-29T01:35:00Z"}
        out = self.run_step(
            env_overrides=self._env(),
            history_body="",
            seed_ledger={"schema_version": 1, "days": {"2026-10-29": {"morning": row}}})
        self.assertEqual(1, len(self.posts), out)
        entry = self._row()
        self.assertNotIn(
            "simulated", entry,
            "the real delivery kept `simulated: true`, so the NEXT fire would "
            "repeat it: %r" % entry)
        self.assertEqual("sent", entry.get("state"))


def _read_worker():
    path = os.path.join(_ROOT, "deploy", "cloudflare-worker.js")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class _PayloadCapture(ActionsHarness):
    """Records the POSTed ntfy payload, which the parent harness discards."""

    def setUp(self):
        super().setUp()
        self.payloads = []

    def posted_payload(self):
        if not self.payloads:
            raise AssertionError(
                "no ntfy payload was captured: the step did not publish, so a "
                "title assertion would pass on nothing")
        return self.payloads[-1]

    def _urlopen(self, *args, **kwargs):
        inner = super()._urlopen(*args, **kwargs)
        captured = self.payloads

        def recording(request, *a, **kw):
            getter = getattr(request, "get_method", None)
            method = (getter() if callable(getter) else "GET").upper()
            if method == "POST":
                raw = getattr(request, "data", None)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                captured.append(raw)
            return inner(request, *a, **kw)

        return recording


# `SimulatedPushEndToEnd` declares the harness; `_PayloadCapture` supplies the
# capture, and this rebase is the one line that puts them together.
SimulatedPushEndToEnd.__bases__ = (_PayloadCapture,)


if __name__ == "__main__":
    unittest.main(verbosity=2)
