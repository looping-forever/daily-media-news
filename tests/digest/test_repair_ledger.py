#!/usr/bin/env python3
"""tests/digest/test_repair_ledger.py -- the stored false row is retired in KV.

WHAT IS BEING GUARDED
---------------------
KV key `digest:ledger` holds `2026-10-26 evening` with `state=sent` and
`ntfy_sent_at=2026-10-26T12:34:19Z`, an instant that precedes the slot's own
13:35Z due moment by 1h00m41s. daily-news.yml reports it on every run as
`LEDGER HONESTY VIOLATION (stored)` and never rewrites it, because KV is
authoritative for delivery and retiring a historical record is the owner's
decision. Correcting the committed mirror cannot help: the seal step copies
`days` out of KV verbatim on every seal, so the next seal puts the false row
back.

`scripts/repair_ledger.py` is that owner's decision, applied once, in KV. The
tests below hold it to the four properties the repair claims:

  * IDEMPOTENT      -- a second run changes nothing, and says so;
  * DECLARATIVE     -- it edits only a row that still carries the exact false
                       claim the correction names;
  * NON-DESTRUCTIVE -- the original claim is kept beside the correction and
                       every other field is reproduced unchanged;
  * LOUD            -- absent rows exit 0 with the truth, a shape nobody has
                       read is refused, and a write that does not land is
                       verified and reported as FAILED.

The shipped mislabel arm is EXECUTED here, not re-implemented: the predicate
that reports the violation and the predicate the repair keys on must agree on
every shape in play, or the repair could keep a row the reporter still flags.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
for _path in (_TESTS, _ROOT, os.path.join(_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import repair_ledger as R  # noqa: E402
from tests.digest.test_ledger_honesty import honesty_namespace  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MIRROR_PATH = REPO_ROOT / "state" / "digest_slot_ledger.json"

NOW = "2026-11-01T05:19:42Z"
LATER = "2026-11-02T09:00:00Z"
DECLARATION = R.CORRECTIONS[0]

#: THE FALSE ROW, as KV holds it. Every field except the two the correction
#: touches is copied from the committed mirror, because the mirror is the same
#: record before its claim was retired.
FALSE_EVENING = {
    "actions_dispatch_at": "2026-10-26T13:35:25Z",
    "actions_dispatch_cause": None,
    "actions_dispatch_http_status": None,
    "actions_dispatch_reason": None,
    "attempts": 1,
    "build": "b56cbea",
    "first_ntfy_sent_at": None,
    "kind": "fallback_last_good",
    "last_good_shown": "2026-10-18",
    "late_build_dispatched": True,
    "ntfy_sent_at": "2026-10-26T12:34:19Z",
    "pending_at": "2026-10-26T13:55:26Z",
    "reject_plain": "no digest was built for evening 2026-10-26.",
    "reject_reason": "absent",
    "scheduled_instant": "2026-10-26T13:35:00Z",
    "sequence_id": "dmn-2026-10-26-evening",
    "state": "sent",
}

#: A second, unrelated day: present so the "only the affected row is printed"
#: assertion has something that must NOT appear in the output.
OTHER_DAY_EVENING = {
    "kind": "delivered",
    "ntfy_sent_at": "2026-10-25T13:36:02Z",
    "scheduled_instant": "2026-10-25T13:35:00Z",
    "sequence_id": "dmn-2026-10-25-evening",
    "state": "sent",
}


def ledger_with_false_row():
    return {
        "schema_version": 1,
        "as_of": "2026-10-26T14:00:00Z",
        "pruned_before": "2026-10-20",
        "days": {
            "2026-10-25": {"evening": copy.deepcopy(OTHER_DAY_EVENING)},
            "2026-10-26": {"evening": copy.deepcopy(FALSE_EVENING)},
        },
    }


def mirror_corrected_row():
    """The committed mirror's corrected row as (day, slot, row); None if pruned.

    Selected by PREDICATE, never by date: the mirror is a rolling window, so a
    hard-coded day is present today and gone within a week.
    """
    data = json.loads(MIRROR_PATH.read_text(encoding="utf-8"))
    days = data.get("days") or {}
    for day in sorted(days, reverse=True):
        for slot in sorted(days.get(day) or {}):
            row = (days.get(day) or {}).get(slot)
            if isinstance(row, dict) and row.get("corrected") is True:
                return day, slot, row
    return None


def second_run_is_clean(apply):
    """True when a second application, onto the first result, changes nothing.

    The result of the first run is what gets applied to and then compared, so
    an applier that stamps a fresh instant on every run is caught here: the
    stamp it writes the second time is not the stamp it wrote the first time.
    """
    once = apply(copy.deepcopy(ledger_with_false_row()))
    snapshot = copy.deepcopy(once)
    apply(once)
    return once == snapshot


_STAMPS = iter(range(999))


def non_idempotent_apply(ledger):
    """A deliberately WRONG applier: it re-stamps `corrected_at` every run.

    The stamp follows a counter because that is what a clock does: a repair
    that wrote "now" onto an already-corrected row would change KV on every
    dispatch and report a change that did not happen.
    """
    row = ledger["days"]["2026-10-26"]["evening"]
    row["state"] = "failed"
    row.pop("ntfy_sent_at", None)
    row["corrected"] = True
    row["corrected_at"] = "2099-01-01T00:%02d:00Z" % (next(_STAMPS) % 60)
    row["corrected_reason"] = DECLARATION["reason"]
    row["original_state"] = "sent"
    row["original_kind"] = row.get("kind")
    row["original_ntfy_sent_at"] = "2026-10-26T12:34:19Z"
    return ledger


class FakeKV:
    """An in-memory stand-in for the two KV calls, so no network is reached."""

    def __init__(self, ledger, drop_write=False):
        self.ledger = copy.deepcopy(ledger)
        self.writes = []
        self.reads = 0
        self.drop_write = drop_write

    def read(self):
        self.reads += 1
        return copy.deepcopy(self.ledger), ""

    def write(self, ledger):
        self.writes.append(copy.deepcopy(ledger))
        if not self.drop_write:
            self.ledger = copy.deepcopy(ledger)
        return True, "HTTP 200"


class CliCase(unittest.TestCase):
    """Shared plumbing: swap the transport, keep the credentials named."""

    TOKEN = "zzqqxxnotarealtokenvalue"
    ACCOUNT = "0123456789abcdef0123456789abcdef"
    NAMESPACE = "fedcba9876543210fedcba9876543210"
    ENV_KEYS = ("CF_LEDGER_TOKEN", "CF_ACCOUNT_ID", "CF_KV_NAMESPACE_ID")

    def set_env(self, env):
        saved = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(env)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def install(self, kv, env=None):
        self.addCleanup(setattr, R, "kv_read", R.kv_read)
        self.addCleanup(setattr, R, "kv_write", R.kv_write)
        R.kv_read = kv.read
        R.kv_write = kv.write
        self.set_env(env if env is not None else {
            "CF_LEDGER_TOKEN": self.TOKEN,
            "CF_ACCOUNT_ID": self.ACCOUNT,
            "CF_KV_NAMESPACE_ID": self.NAMESPACE,
        })
        return kv

    def run_cli(self, kv, argv, env=None):
        self.install(kv, env)
        return self.main(argv)

    def main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = R.main(argv)
        return code, buffer.getvalue()


class TheCorrectionShapeTests(unittest.TestCase):
    """What the corrected row must look like, field by field."""

    def setUp(self):
        self.fixed = R.corrected_row(dict(FALSE_EVENING), DECLARATION, NOW)

    def test_it_matches_the_committed_mirror_row(self):
        """The repaired KV row and the committed mirror must say the same thing.

        The mirror carries the correction already applied; this reconstructs
        the row's original claim from the mirror itself and asserts the script
        produces that same record, so the next seal (which copies KV over the
        mirror verbatim) changes nothing a reader has already seen.

        `corrected_at` is the ONE field that is allowed to differ, because it is
        volatile data rather than part of the contract: it records the instant
        the repair happened to run, and the mirror is free to carry a different
        one. Pinning that instant here made the test go red when the repair was
        re-run at a different second -- a test may not encode a value the data
        owns. What IS asserted is the contract the correction promises, plus the
        one property `corrected_at` must have: a real UTC instant that is not in
        the future relative to the fixture clock.
        """
        found = mirror_corrected_row()
        if found is None:
            days = sorted((json.loads(MIRROR_PATH.read_text(encoding="utf-8"))
                           .get("days") or {}))
            self.skipTest(
                "the committed mirror holds no corrected row: the 2026-10-26 "
                "evening correction has been pruned from the rolling window "
                "(present days: %s..%s), so there is nothing to compare"
                % (days[0] if days else "empty", days[-1] if days else "empty"))
        day, slot, mirror = found

        # Rebuild the pre-correction row from the mirror's own evidence, then
        # let the SCRIPT correct it. Comparing to the mirror pins the contract
        # for whichever row the correction touched, not one fixed date.
        original = {key: value for key, value in mirror.items()
                    if key not in R.CORRECTION_FIELDS and key != "ntfy_sent_at"}
        original["state"] = mirror.get("original_state")
        original["ntfy_sent_at"] = mirror.get("original_ntfy_sent_at")
        if "original_kind" in mirror:
            original["kind"] = mirror["original_kind"]
        fixed = R.corrected_row(
            original, {"reason": mirror.get("corrected_reason")},
            mirror.get("corrected_at"))
        self.assertEqual(fixed, mirror,
                         "the script's correction of %s %s does not reproduce "
                         "the committed mirror row" % (day, slot))

        # The original claim is the contradiction that made this row wrong: it
        # must not fall in the slot its bucket names.
        held = R.slot_holding_instant(
            day, R.instant_or_none(mirror.get("original_ntfy_sent_at")))
        self.assertNotEqual("%s %s" % (day, slot), held,
                            "the mirror row was corrected but its original claim "
                            "falls in the row's own slot: %r" % (mirror,))

        stamp = R.instant_or_none(mirror.get("corrected_at"))
        self.assertIsNotNone(stamp,
                             "the mirror's corrected_at is not an ISO-8601 "
                             "instant: %r" % (mirror.get("corrected_at"),))
        self.assertLessEqual(
            stamp, R.instant_or_none(LATER),
            "the mirror says it was corrected at %r, which is in the future "
            "relative to the fixture clock %s"
            % (mirror.get("corrected_at"), LATER))

    def test_the_false_claim_moves_aside_instead_of_disappearing(self):
        self.assertEqual("failed", self.fixed["state"])
        self.assertNotIn("ntfy_sent_at", self.fixed)
        self.assertEqual("sent", self.fixed["original_state"])
        self.assertEqual("2026-10-26T12:34:19Z", self.fixed["original_ntfy_sent_at"])
        self.assertEqual("fallback_last_good", self.fixed["original_kind"])
        self.assertTrue(self.fixed["corrected"])
        self.assertEqual(NOW, self.fixed["corrected_at"])
        self.assertEqual(DECLARATION["reason"], self.fixed["corrected_reason"])

    def test_every_other_field_is_reproduced_unchanged(self):
        kept = {key: value for key, value in FALSE_EVENING.items()
                if key not in ("state", "ntfy_sent_at")}
        for key, value in kept.items():
            self.assertEqual(value, self.fixed.get(key),
                             "the correction changed %r, which is not part of "
                             "the false claim" % key)
        self.assertEqual(set(FALSE_EVENING) - {"ntfy_sent_at", "state"},
                         set(self.fixed) - set(R.CORRECTION_FIELDS),
                         "a field other than the declared ones was added or "
                         "dropped: %r" % sorted(set(self.fixed)))

    def test_the_declared_reason_states_the_true_numbers(self):
        sent = R.instant_or_none(FALSE_EVENING["ntfy_sent_at"])
        due = R.slot_due_instant("2026-10-26", "evening")
        gap = int((due - sent).total_seconds())
        self.assertIn(R.iso(due), DECLARATION["reason"])
        self.assertIn(R.humanise_seconds(gap), DECLARATION["reason"],
                      "the declared reason claims %r but the gap from the "
                      "instant it quotes to the due instant is %s"
                      % (DECLARATION["reason"], R.humanise_seconds(gap)))
        self.assertEqual("2026-10-26 morning",
                         R.slot_holding_instant("2026-10-26", sent),
                         "the declared reason says the instant falls in the "
                         "morning window; the arithmetic must agree")
        self.assertIn("morning", DECLARATION["reason"])


class IdempotenceTests(unittest.TestCase):
    def test_a_second_run_changes_nothing_and_says_so(self):
        first, first_report = R.plan(ledger_with_false_row(), R.CORRECTIONS, NOW)
        self.assertEqual(1, len(first_report["corrected"]))
        second, second_report = R.plan(copy.deepcopy(first), R.CORRECTIONS, LATER)

        self.assertEqual(first, second,
                         "the second run changed the ledger, so this dispatch "
                         "is not safe to re-run")
        self.assertEqual([], second_report["corrected"])
        self.assertEqual(1, len(second_report["already"]))
        self.assertIn("already carries the correction",
                      second_report["already"][0]["why"])

    def test_the_second_run_check_rejects_a_non_idempotent_applier(self):
        """MUTATION PROOF: the check above must be able to go red."""
        self.assertTrue(second_run_is_clean(
            lambda ledger: R.plan(ledger, R.CORRECTIONS, NOW)[0]))
        self.assertFalse(
            second_run_is_clean(non_idempotent_apply),
            "a stub that re-stamps corrected_at on every run was accepted as "
            "clean, so the idempotence test above proves nothing")
        print("mutation proof: a re-stamping applier is caught by the "
              "second-run check")


class DeclarativeScopeTests(unittest.TestCase):
    def test_an_already_corrected_row_is_never_touched(self):
        ledger = ledger_with_false_row()
        corrected, _ = R.plan(ledger, R.CORRECTIONS, NOW)
        again, report = R.plan(copy.deepcopy(corrected), R.CORRECTIONS, LATER)

        self.assertEqual(corrected, again)
        self.assertEqual([], report["corrected"])
        self.assertEqual(1, len(report["already"]))
        self.assertEqual(2, report["untouched"],
                         "nothing was corrected, so both rows are untouched")

    def test_a_row_with_no_false_claim_is_never_touched(self):
        ledger = ledger_with_false_row()
        ledger["days"]["2026-10-26"]["evening"]["ntfy_sent_at"] = "2026-10-26T13:40:00Z"
        untouched, report = R.plan(ledger, R.CORRECTIONS, NOW)

        self.assertEqual(ledger, untouched)
        self.assertEqual([], report["corrected"])
        self.assertEqual(1, len(report["already"]))
        self.assertIn("carries no delivery claim", report["already"][0]["why"])

    def test_a_row_that_does_not_match_the_declared_shape_is_refused(self):
        ledger = ledger_with_false_row()
        ledger["days"]["2026-10-26"]["evening"]["ntfy_sent_at"] = "2026-10-26T11:00:00Z"
        untouched, report = R.plan(ledger, R.CORRECTIONS, NOW)

        self.assertEqual(ledger, untouched, "a refused row was edited anyway")
        self.assertEqual([], report["corrected"])
        self.assertEqual(1, len(report["refused"]))
        self.assertIn("11:00:00Z", report["refused"][0]["why"])

    def test_an_absent_row_is_reported_absent(self):
        ledger = ledger_with_false_row()
        del ledger["days"]["2026-10-26"]
        untouched, report = R.plan(ledger, R.CORRECTIONS, NOW)

        self.assertEqual(ledger, untouched)
        self.assertEqual([], report["corrected"])
        self.assertEqual(1, len(report["absent"]))

    def test_the_other_row_and_the_other_fields_are_untouched(self):
        fixed, report = R.plan(ledger_with_false_row(), R.CORRECTIONS, NOW)
        self.assertEqual(1, len(report["corrected"]))
        self.assertEqual(OTHER_DAY_EVENING, fixed["days"]["2026-10-25"]["evening"])
        self.assertEqual("2026-10-20", fixed["pruned_before"])
        self.assertEqual("2026-10-26T14:00:00Z", fixed["as_of"])


class ShippedArmAgreementTests(unittest.TestCase):
    """The repair keys on the SAME claim the reporter prints. Prove it."""

    @classmethod
    def setUpClass(cls):
        cls.ns = honesty_namespace()

    def test_the_false_claim_agrees_with_the_shipped_arm_on_every_shape(self):
        cases = [
            ("2026-10-26", "evening", FALSE_EVENING),
            ("2026-10-18", "evening",
             {"first_ntfy_sent_at": "2026-10-17T18:25:19Z", "kind": "delivered",
              "ntfy_sent_at": "2026-10-17T18:25:19Z", "state": "sent"}),
            ("2026-10-18", "morning",
             {"built_at": "2026-10-18T01:03:40Z", "story_count": 2}),
            ("2026-10-12", "morning",
             {"kind": "unavailable", "ntfy_failed": True, "ntfy_sent_at": None,
              "state": "failed"}),
            ("2026-10-26", "evening",
             R.corrected_row(dict(FALSE_EVENING), DECLARATION, NOW)),
        ]
        for date, slot, entry in cases:
            shipped = self.ns["ledger_mislabel"](copy.deepcopy(entry), date, slot)
            local = R.false_claim(copy.deepcopy(entry), date, slot)
            self.assertEqual(bool(shipped), bool(local),
                             "the shipped arm and the repair disagree about %s "
                             "%s: shipped=%r repair=%r"
                             % (date, slot, bool(shipped), bool(local)))

    def test_the_corrected_row_silences_the_stored_violation_report(self):
        ledger = ledger_with_false_row()

        before = io.StringIO()
        with contextlib.redirect_stdout(before):
            self.ns["flag_mislabelled_buckets"](copy.deepcopy(ledger))
        self.assertIn("LEDGER HONESTY VIOLATION (stored): 2026-10-26 evening",
                      before.getvalue(),
                      "the row this repair exists for was not reported before "
                      "the repair, so the test is not looking at the defect")

        fixed, _ = R.plan(ledger, R.CORRECTIONS, NOW)
        after = io.StringIO()
        with contextlib.redirect_stdout(after):
            self.ns["flag_mislabelled_buckets"](fixed)
        self.assertEqual("", after.getvalue(),
                         "the corrected ledger still reports a stored "
                         "violation: %r" % after.getvalue())


class CliTests(CliCase):
    def test_dry_run_prints_the_diff_and_writes_nothing(self):
        kv = FakeKV(ledger_with_false_row())
        code, out = self.run_cli(kv, [])

        self.assertEqual(0, code, out)
        self.assertEqual([], kv.writes, "a dry run wrote to KV")
        self.assertIn("rows corrected=1", out)
        self.assertIn("diff 2026-10-26 evening", out)
        self.assertIn("- state: sent", out)
        self.assertIn("+ state: failed", out)
        self.assertIn("- ntfy_sent_at: 2026-10-26T12:34:19Z", out)
        self.assertIn("+ ntfy_sent_at: <absent>", out)
        self.assertIn("dry run: nothing was written", out)
        self.assertNotIn("dmn-2026-10-25-evening", out,
                         "the diff printed a row it is not repairing")

    def test_the_write_applies_and_verifies_the_read_back(self):
        kv = FakeKV(ledger_with_false_row())
        code, out = self.run_cli(kv, ["--write"])

        self.assertEqual(0, code, out)
        self.assertEqual(2, kv.reads, "the write path must read KV again to "
                                      "verify what it stored")
        self.assertEqual(1, len(kv.writes))
        self.assertIn("verification: OK", out)
        row = kv.ledger["days"]["2026-10-26"]["evening"]
        self.assertEqual("failed", row["state"])
        self.assertNotIn("ntfy_sent_at", row)
        self.assertTrue(row["corrected"])
        self.assertEqual(OTHER_DAY_EVENING, kv.ledger["days"]["2026-10-25"]["evening"])

    def test_a_second_write_run_changes_nothing_and_does_not_put(self):
        kv = FakeKV(ledger_with_false_row())
        first_code, first_out = self.run_cli(kv, ["--write"])
        self.assertEqual(0, first_code, first_out)
        second_code, second_out = self.run_cli(kv, ["--write"])

        self.assertEqual(0, second_code, second_out)
        self.assertEqual(1, len(kv.writes),
                         "the second run PUT the ledger again; KV must not be "
                         "written when there is nothing to correct")
        self.assertIn("rows corrected=0", second_out)
        self.assertIn("nothing to write", second_out)

    def test_an_absent_violation_exits_0_with_the_truthful_line(self):
        ledger = ledger_with_false_row()
        del ledger["days"]["2026-10-26"]
        kv = FakeKV(ledger)
        code, out = self.run_cli(kv, ["--write"])

        self.assertEqual(0, code, out)
        self.assertEqual([], kv.writes)
        self.assertIn("absent: 2026-10-26 evening", out)
        self.assertIn("never present", out)

    def test_an_already_corrected_row_exits_0_and_says_already(self):
        corrected, _ = R.plan(ledger_with_false_row(), R.CORRECTIONS, NOW)
        kv = FakeKV(corrected)
        code, out = self.run_cli(kv, ["--write"])

        self.assertEqual(0, code, out)
        self.assertEqual([], kv.writes)
        self.assertIn("already honest: 2026-10-26 evening", out)
        self.assertIn("already carries the correction written at %s" % NOW, out)

    def test_a_conflicting_shape_is_refused_and_nothing_is_written(self):
        ledger = ledger_with_false_row()
        ledger["days"]["2026-10-26"]["evening"]["ntfy_sent_at"] = "2026-10-26T11:00:00Z"
        kv = FakeKV(ledger)
        code, out = self.run_cli(kv, ["--write"])

        self.assertEqual(2, code, out)
        self.assertEqual([], kv.writes, "a refused row was written anyway")
        self.assertIn("REFUSED: 2026-10-26 evening", out)
        self.assertIn("NOTHING was written", out)

    def test_a_write_that_does_not_land_fails_verification(self):
        kv = FakeKV(ledger_with_false_row(), drop_write=True)
        code, out = self.run_cli(kv, ["--write"])

        self.assertEqual(3, code, out)
        self.assertIn("verification: FAILED", out)
        self.assertIn("reports a violation", out)

    def test_it_refuses_to_run_without_credentials(self):
        """The REAL transport gate, with no credentials present and no network.

        kv_target() refuses before any request is built, so this reaches no
        endpoint: it proves the refusal, not a timeout.
        """
        self.set_env({})
        code, out = self.main(["--write"])

        self.assertEqual(1, code, out)
        self.assertIn("CF_LEDGER_TOKEN", out)
        self.assertIn("nothing was written", out)

    def test_the_placeholder_namespace_is_refused(self):
        self.set_env({
            "CF_LEDGER_TOKEN": self.TOKEN,
            "CF_ACCOUNT_ID": self.ACCOUNT,
            "CF_KV_NAMESPACE_ID": R.KV_PLACEHOLDER,
        })
        code, out = self.main(["--write"])

        self.assertEqual(1, code, out)
        self.assertIn("placeholder", out)

    def test_no_credential_value_reaches_the_output(self):
        kv = FakeKV(ledger_with_false_row())
        code, out = self.run_cli(kv, ["--write"])

        self.assertEqual(0, code, out)
        for value in (self.TOKEN, self.ACCOUNT, self.NAMESPACE):
            self.assertNotIn(value, out,
                             "a credential value reached the log: this "
                             "repository is public")


if __name__ == "__main__":
    unittest.main(verbosity=2)
