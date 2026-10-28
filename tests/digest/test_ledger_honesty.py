"""THE LEDGER MUST NOT CLAIM A DELIVERY THAT DID NOT HAPPEN.

DEFECT 1 (the most serious thing this repository exists to prevent). On
2026-10-14 and 2026-10-15 the delivery ledger recorded the evening slots as:

    state=sent  kind=delivered  ntfy_failed=true
    http_status=429  ntfy_quota_exhausted=true
    error="TimeoutError: The operation was aborted due to timeout"

The owner was told he had received two evening digests he did not receive. The
archive and the ledger both asserted success.

WHY IT WAS POSSIBLE. A slot entry is a MERGE -- `Object.assign` onto whatever
the entry already held -- and four arms write patches into it: the real digest,
the sequence_id read-back dedup, the fallback ladder, and the unavailable
notice. Each arm remembered to write `ntfy_failed` on both edges, and the merge
could still pair a `sent` from one arm with a `true` from another, because no
single call site ever sees both halves of the contradiction.

So the rule is enforced on the MERGED result at the one function every patch
must pass through. These tests execute that shipped function, they do not
re-implement it, and one of them pins that it is actually CALLED -- a defined
but uncalled invariant is decoration.

DEFECT 4 (the 429 quota). The same evening slots recorded
`ntfy_quota_exhausted: true` on 2026-10-12, 10-13 and 10-14 while mornings were
healthy (2026-10-16 landed 24s after schedule, `on_time: true`). ntfy's message
quota is per visitor and per UTC day, so a second and third attempt at a 429 are
not retries of a transient fault -- they are two more messages charged against
an allowance that is already empty, and they delay the escalation to the Actions
arm, which pushes from a different IP and holds a separate allowance. The retry
of that one terminal status is now cut; nothing else about the push is changed.

DEFECT 5 (the mislabelled bucket). The ledger held `2026-10-18 evening` with
`ntfy_sent_at: 2026-10-17T18:25:19Z` -- a send recorded nineteen hours BEFORE the
19:20 NPT slot it is filed under had begun -- with `ntfy_failed` absent entirely,
so the correction above could not see it: that rule fires on a verified failure
beside a delivery claim. A bucket is a claim about ONE slot, so an instant outside
that slot's own window is a mislabel whatever `state` and `kind` say. The
Actions writer now reports that shape both for the entry it is about to store and
for every bucket already stored in the ledger it just read, and it reports it
WITHOUT rewriting it: KV is authoritative for delivery, so a wrong historical
record is the owner's to retire, not this step's to edit.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
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
from tests.workflows.test_workflow_lint import heredoc_bodies  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "deploy" / "cloudflare-worker.js"

#: THE REAL 2026-10-14 EVENING ENTRY, verbatim from
#: state/digest_slot_ledger.json on origin/master. Not a shape invented for a
#: test: these are the exact fields and values the owner's ledger carried.
REAL_2026_10_14_EVENING = {
    "actions_dispatch_at": "2026-10-14T13:35:30Z",
    "actions_dispatch_reason": None,
    "attempts": 3,
    "build": "bd8d401",
    "error": "TimeoutError: The operation was aborted due to timeout",
    "first_ntfy_sent_at": "2026-10-14T13:39:48Z",
    "http_status": 429,
    "kind": "delivered",
    "ntfy_failed": True,
    "ntfy_quota_exhausted": True,
    "ntfy_sent_at": "2026-10-14T13:39:48Z",
    "pending_at": "2026-10-14T13:35:14Z",
    "scheduled_instant": "2026-10-14T13:35:00Z",
    "sequence_id": "dmn-2026-10-14-evening",
    "state": "sent",
}

#: THE REAL 2026-10-13 EVENING ENTRY: this one DID eventually deliver, so
#: `ntfy_failed` is false and `state` is honestly `sent` -- but it still carries
#: `ntfy_quota_exhausted: true` and `error: "ntfy HTTP 429"` from the 13:35
#: attempt that failed, forty minutes before the 13:55 attempt that worked. Those
#: are the same defect's quieter half: a delivered slot that tells the owner to go
#: and read a log about an outage that ended.
REAL_2026_10_13_EVENING = {
    "actions_dispatch_at": "2026-10-13T13:35:47Z",
    "attempts": 2,
    "error": "ntfy HTTP 429",
    "http_status": 200,
    "is_partial": False,
    "kind": "delivered",
    "ntfy_failed": False,
    "ntfy_quota_exhausted": True,
    "ntfy_sent_at": "2026-10-13T13:55:38Z",
    "first_ntfy_sent_at": "2026-10-13T13:55:38Z",
    "sequence_id": "dmn-2026-10-13-evening",
    "state": "sent",
}


def worker_source():
    return WORKER.read_text(encoding="utf-8")


def extract_js_function(name):
    """The source of one top-level `function <name>(...) { ... }` from the Worker.

    Brace-matched rather than regex-matched to the first `}`, because this file's
    bodies are full of object literals and template-free string concatenation that
    a naive extractor would cut in half.
    """
    src = worker_source()
    start = src.find("function %s(" % name)
    if start < 0:
        raise AssertionError(
            "deploy/cloudflare-worker.js has no `function %s(` -- the invariant "
            "this test executes is not in the shipped Worker" % name)
    # `async` sits BEFORE `function`, so a slice that starts at `function` would
    # silently turn an async function into a sync one and every `await` in its
    # body would become a SyntaxError. Carry the keyword across.
    if src[:start].endswith("async "):
        start -= len("async ")
    depth = 0
    seen_open = False
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
            seen_open = True
        elif ch == "}":
            depth -= 1
            if seen_open and depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces extracting %s" % name)


def run_node(script):
    tmp = tempfile.mkdtemp(prefix="ledger-honesty-node-")
    try:
        path = Path(tmp) / "harness.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True,
                              text=True, timeout=60, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0:
            raise AssertionError("node harness failed (exit %d):\n%s"
                                 % (proc.returncode, proc.stderr))
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: The ledger-state and ledger-kind constants `enforceLedgerHonesty` and the
#: `deliveryEdition` it now ends by calling reach for, stubbed exactly as the
#: Worker defines them.
HONESTY_PRELUDE = """
const STATE_SENT = "sent";
const STATE_FAILED = "failed";
const STATE_PENDING = "pending";
const KIND_DELIVERED = "delivered";
const KIND_PARTIAL = "partial";
const KIND_UNSUMMARISED = "unsummarised";
const KIND_FALLBACK_LAST_GOOD = "fallback_last_good";
const KIND_UNAVAILABLE = "unavailable";
const KIND_DUPLICATE_SUPPRESSED = "duplicate_suppressed";
const DELIVERY_KIND_STOOD_DOWN = "stood_down";
function isoUtc(ms) { return new Date(ms).toISOString().replace(/\\.\\d{3}Z$/, "Z"); }
"""


def call_honesty(entry):
    """Run the shipped `enforceLedgerHonesty` over `entry`; return the result.

    `deliveryEdition` comes along because the shipped function now ENDS by
    calling it: the derived `edition` is written by the same function that
    corrects the state, so a slice that left it out would fail with
    `deliveryEdition is not defined` rather than testing anything.
    """
    script = (HONESTY_PRELUDE
              + extract_js_function("deliveryEdition")
              + extract_js_function("enforceLedgerHonesty") + """
const entry = %s;
const out = enforceLedgerHonesty(entry, {date: "2026-10-14", slot: "evening"});
console.log(JSON.stringify(out));
""" % json.dumps(entry))
    return run_node(script)


class LedgerHonestyTests(unittest.TestCase):
    def test_a_failed_push_cannot_produce_a_SENT_delivered_record(self):
        """THE DEFECT, against the owner's actual 2026-10-14 entry.

        RED without the fix: `Object.assign` returned the entry untouched, so
        `state` stayed `sent`, `kind` stayed `delivered`, and the two push
        instants stayed in place -- a slot recorded as delivered while carrying
        a verified 429 failure.
        """
        out = call_honesty(dict(REAL_2026_10_14_EVENING))

        self.assertNotEqual(out.get("state"), "sent", (
            "THE DEFECT, LIVE. The entry still claims the slot was sent while "
            "carrying a verified ntfy failure: %r" % out))
        self.assertEqual(out.get("state"), "failed", (
            "a slot with a verified ntfy failure must read as `failed`: %r" % out))
        self.assertIs(out.get("ntfy_failed"), True, (
            "the verified-failure flag must not be cleared by a downgrade: %r"
            % out))
        self.assertNotIn("ntfy_sent_at", out, (
            "`ntfy_sent_at` is documented as 'a push happened'; it cannot survive "
            "beside a record that just declared the push did not happen: %r"
            % out))
        self.assertNotIn("first_ntfy_sent_at", out, (
            "the punctuality anchor cannot survive a downgrade either -- it is "
            "what scripts/delivery_ledger.py subtracts scheduled_instant from: %r"
            % out))
        # The diagnosis survives: this is a correction, not an erasure.
        self.assertEqual(out.get("http_status"), 429, out)
        self.assertEqual(out.get("attempts"), 3, out)
        self.assertIn("TimeoutError", str(out.get("error")), out)
        self.assertIs(out.get("ntfy_quota_exhausted"), True, out)
        self.assertEqual(out.get("kind"), "delivered", (
            "`kind` classifies what was BUILT and the digest did build, so it is "
            "deliberately left alone; only `state` is the delivery verdict: %r"
            % out))

    def test_a_degraded_kind_claiming_a_push_is_downgraded_too(self):
        """`kind` claims a push on a bad day, in three spellings, not one.

        THE GAP. The gate read `state === "sent" || kind === "delivered"`, and on
        a degraded day the Actions arm records `state: failed` with
        `kind: unsummarised` or `kind: partial` -- a digest that IS on the topic
        and whose push then fails on the retry. Neither half was true under the
        old test, so a failed push survived as an undelivered digest that nothing
        reported. `unavailable` is the one kind that claims no push and must stay
        untouched.
        """
        for kind in ("delivered", "partial", "unsummarised"):
            with self.subTest(kind=kind):
                out = call_honesty({"state": "failed", "kind": kind,
                                    "ntfy_failed": True, "http_status": 429,
                                    "attempts": 3})
                self.assertEqual(
                    "failed", out.get("state"),
                    "a %r entry carrying ntfy_failed=true was not downgraded: %r"
                    % (kind, out))
                self.assertIn(
                    "honesty_downgraded_at", out,
                    "the correction was not recorded for kind=%r, so nothing in "
                    "the stored entry says the slot was ever revised: %r"
                    % (kind, out))
                self.assertEqual(
                    kind, out.get("kind"),
                    "the downgrade rewrote `kind` for %r. `kind` classifies what "
                    "was BUILT and the digest did build; only `state` is the "
                    "delivery verdict: %r" % (kind, out))

    def test_the_kind_that_claims_nothing_is_left_alone(self):
        """`unavailable` is a non-delivery, so it must not be rewritten.

        The control for the test above: widening the gate must not turn every
        failed slot into a fake downgrade and lose the diagnosis that says the
        digest was never built.
        """
        out = call_honesty({"state": "failed", "kind": "unavailable",
                            "ntfy_failed": True, "http_status": 400,
                            "attempts": 3})
        self.assertNotIn("honesty_downgraded_at", out,
                         "an `unavailable` slot claims no push and was downgraded "
                         "anyway: %r" % out)

    def test_a_real_delivery_drops_the_failure_metadata_it_outranks(self):
        """The 2026-10-13 shape: delivered at 13:55 after a 429 at 13:35.

        RED without the fix: `ntfy_quota_exhausted: true` and
        `error: "ntfy HTTP 429"` survive on a slot that genuinely arrived, so the
        record tells the owner to go and read a log about an outage that ended.
        """
        out = call_honesty(dict(REAL_2026_10_13_EVENING))

        self.assertEqual(out.get("state"), "sent", (
            "a genuinely delivered slot must stay `sent`: %r" % out))
        self.assertNotIn("ntfy_quota_exhausted", out, (
            "a success outranks the quota refusal of an earlier attempt, and the "
            "stale flag sends the owner to read a log for an outage that is over: %r"
            % out))
        self.assertNotIn("error", out, (
            "the stale `error` of a superseded attempt must not survive a real "
            "delivery: %r" % out))

    def test_the_invariant_is_CALLED_by_the_writer_not_merely_defined(self):
        """A defined-but-uncalled invariant is decoration.

        This is the test that makes the other two mean something: it pins that
        the single function every ledger patch passes through is the one that
        applies the rule.
        """
        src = worker_source()
        self.assertIn("function enforceLedgerHonesty(", src,
                      "the invariant is not defined in the Worker at all")
        writer = extract_js_function("commitDigestLedger")
        self.assertIn(
            "enforceLedgerHonesty(", writer,
            "the ledger writer does not apply the invariant, so the definition is "
            "dead code and a failed push can still be recorded as delivered:\n%s"
            % writer)

    def test_both_ledger_writers_carry_the_same_rule(self):
        """Two writers, one ledger.

        The Worker writes `digest:ledger` from its own IP and the Actions arm
        writes the same key from another one. The Worker's invariant cannot
        reach a write the Actions heredoc makes, so that heredoc needs its own
        copy -- and a copy that has drifted is worse than none, because it looks
        like the rule is covered twice.
        """
        workflow = (REPO_ROOT / ".github" / "workflows" / "daily-news.yml").read_text(
            encoding="utf-8")
        self.assertIn("def enforce_ledger_honesty(", workflow,
                      "the Actions arm writes the same KV ledger and has no "
                      "honesty rule of its own")
        self.assertIn("enforce_ledger_honesty(entry, date, slot, now)", workflow,
                      "the Actions arm defines the rule but never applies it to "
                      "the entry it is about to write")


# ==========================================================================
# DEFECT 5 -- the mislabel the ntfy_failed rule structurally could not see
# ==========================================================================

#: The step that owns the Actions-side ledger write, its honesty rule and the
#: mislabel arm. The whole heredoc cannot be executed here (its module level
#: performs the real push), so the relevant shipped bytes are sliced out below.
NTFY_STEP = "Publish to ntfy (primary)"

#: THE REAL BUCKET, verbatim from state/digest_slot_ledger.json: the evening of
#: the 18th recorded as sent, with a send instant nineteen hours BEFORE that
#: slot's own 19:20 NPT due moment. Not a shape invented for a test.
REAL_2026_10_18_EVENING = {
    "first_ntfy_sent_at": "2026-10-17T18:25:19Z",
    "kind": "delivered",
    "ntfy_sent_at": "2026-10-17T18:25:19Z",
    "sequence_id": "dmn-2026-10-18-evening",
    "state": "sent",
}

#: The real 2026-10-18 morning bucket, also verbatim: the build half only, which
#: is what the seal step creates for the slot it is about to deliver.
REAL_2026_10_18_MORNING_BUILD_ONLY = {
    "built_at": "2026-10-18T01:03:40Z",
    "source_health_state": "ok",
    "story_count": 2,
}

#: The real 2026-10-12 morning bucket: a genuine non-delivery. `ntfy_sent_at` is
#: null, so it claims no push and must never be reported by this arm.
REAL_2026_10_12_MORNING_FAILED = {
    "attempts": 3,
    "first_ntfy_sent_at": None,
    "http_status": 400,
    "kind": "unavailable",
    "ntfy_failed": True,
    "ntfy_sent_at": None,
    "scheduled_instant": "2026-10-12T01:35:00Z",
    "state": "failed",
}


def step_dict(step_name):
    for _job, name, sd in W.iter_all_steps("daily-news.yml"):
        if name == step_name:
            return sd
    raise AssertionError("daily-news.yml has no step %r" % step_name)


def step_body(step_name):
    """The step's first Python heredoc body — its real bytes, not a copy."""
    bodies = [body for _tag, body in heredoc_bodies(step_dict(step_name)["run"])]
    if not bodies:
        raise AssertionError(
            "step %r has no Python heredoc. The step was renamed, or its body "
            "was replaced with something this suite can no longer execute — "
            "update the suite deliberately rather than deleting the check."
            % step_name)
    return bodies[0]


def shipped_constant(body, name):
    """One shipped top-level single-line assignment, verbatim from the step."""
    lines = [ln for ln in body.splitlines() if ln.startswith(name + " = ")]
    if len(lines) != 1:
        raise AssertionError(
            "the ntfy step no longer defines %s as one top-level line (%d "
            "matches at column 0). This suite executes the shipped bytes, so a "
            "reshape has to be followed deliberately rather than silently."
            % (name, len(lines)))
    return lines[0]


def shipped_def(body, name):
    """The shipped source of one top-level `def <name>(`, to its indent edge.

    Cut by INDENTATION rather than by a regex to the first blank line: these
    bodies hold blank lines, same-indent comments and nested defs, so a naive cut
    would truncate the very function it claims to execute.
    """
    lines = body.splitlines()
    starts = [i for i, ln in enumerate(lines)
              if ln.startswith("def %s(" % name)]
    if len(starts) != 1:
        raise AssertionError(
            "the ntfy step defines %r %d time(s) at top level, expected exactly "
            "1 -- the rule this test executes is not in the shipped step"
            % (name, len(starts)))
    start = starts[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i][:1].isspace():
            end = i
            break
    return "\n".join(lines[start:end]).rstrip()


def honesty_namespace():
    """The shipped mislabel arm, executed from the step's own source.

    Sliced rather than re-implemented, because a second copy of the rule in this
    file is exactly the divergence the suite exists to catch.
    """
    body = step_body(NTFY_STEP)
    source = "\n\n".join([
        "import datetime",
        shipped_constant(body, "NPT_OFFSET"),
        shipped_constant(body, "SLOT_DUE_UTC"),
        shipped_constant(body, "SLOT_EARLY_SLACK_S"),
        # The vocabulary the honesty gate reads. Sliced from the step like
        # everything else: a kind that claims a push must be added in one place
        # on both sides, and this harness fails loudly if the step's copy and
        # the names it is built from drift apart.
        shipped_constant(body, "KIND_DELIVERED"),
        shipped_constant(body, "KIND_PARTIAL"),
        shipped_constant(body, "KIND_UNSUMMARISED"),
        shipped_constant(body, "CLAIMING_KINDS"),
        shipped_def(body, "instant_or_none"),
        shipped_def(body, "slot_due_instant"),
        shipped_def(body, "slot_holding_instant"),
        shipped_def(body, "ledger_mislabel"),
        shipped_def(body, "flag_mislabelled_buckets"),
        shipped_def(body, "enforce_ledger_honesty"),
    ])
    namespace = {"__name__": "ntfy-step"}
    exec(compile(source, "<ntfy-step>", "exec"), namespace)
    return namespace


class LedgerMislabelTests(unittest.TestCase):
    """A send recorded before its own slot began is now a reportable lie."""

    @classmethod
    def setUpClass(cls):
        cls.ns = honesty_namespace()

    def mislabel(self, entry, date, slot):
        return self.ns["ledger_mislabel"](entry, date, slot)

    def test_the_real_nineteen_hours_early_bucket_is_reported(self):
        """THE DEFECT, against the owner's actual 2026-10-18 evening bucket.

        RED without the arm: the bucket passed every check, because it carried
        no `ntfy_failed` and nothing compared its send instant with the slot the
        bucket claims.
        """
        message = self.mislabel(dict(REAL_2026_10_18_EVENING),
                                "2026-10-18", "evening")

        self.assertTrue(message, "the nineteen-hours-early bucket passed the very "
                                 "check added to catch it")
        self.assertIn("2026-10-18 evening", message,
                      "the report must name the bucket it is about: %r" % message)
        self.assertIn("2026-10-17T18:25:19Z", message,
                      "the report must quote the instant it recorded: %r" % message)
        self.assertIn("2026-10-18T13:35:00Z", message,
                      "the report must name the due instant of the slot the "
                      "bucket claims: %r" % message)
        self.assertIn("2026-10-17 evening", message,
                      "the report must name the slot the instant actually falls "
                      "in, or the reader is told only the field that is wrong: %r"
                      % message)
        self.assertIn("19h09m", message,
                      "the size of the contradiction is the whole diagnosis: %r"
                      % message)
        self.assertIn("FLAGGED, NOT CORRECTED", message,
                      "the reader must be told this is a report and not an edit: "
                      "%r" % message)

    def test_every_real_delivered_bucket_passes(self):
        """The rule must be silent on the honest corpus, or it is noise.

        These are the committed ledger's own delivered buckets, including the
        ones that landed LATE -- lateness is measurement, and this arm is not
        allowed to call it dishonesty.
        """
        for entry, date, slot in (
                (REAL_2026_10_13_EVENING, "2026-10-13", "evening"),
                (REAL_2026_10_14_EVENING, "2026-10-14", "evening"),
                ({"ntfy_sent_at": "2026-10-15T13:43:16Z", "state": "sent"},
                 "2026-10-15", "evening"),
                ({"ntfy_sent_at": "2026-10-17T01:35:27Z", "state": "sent"},
                 "2026-10-17", "morning"),
                ({"ntfy_sent_at": "2026-10-16T13:55:36Z", "state": "sent"},
                 "2026-10-16", "evening"),
        ):
            self.assertEqual("", self.mislabel(dict(entry), date, slot),
                             "an honest bucket was reported as a mislabel: %s %s"
                             % (date, slot))

    def test_a_bucket_that_claims_no_send_is_never_reported(self):
        """FAIL-OPEN. Missing data is not a failure, and must not be made one."""
        for entry, date, slot in (
                (REAL_2026_10_12_MORNING_FAILED, "2026-10-12", "morning"),
                (REAL_2026_10_18_MORNING_BUILD_ONLY, "2026-10-18", "morning"),
                ({"ntfy_sent_at": None}, "2026-10-12", "evening"),
                ({"ntfy_sent_at": ""}, "2026-10-12", "evening"),
                ({}, "2026-10-12", "evening"),
                ({"ntfy_sent_at": "not-an-instant"}, "2026-10-12", "evening"),
                ({"ntfy_sent_at": "2026-10-12T01:35:00"}, "2026-10-12", "morning"),
        ):
            self.assertEqual("", self.mislabel(dict(entry), date, slot),
                             "a bucket claiming no readable send was reported: %r"
                             % entry)

    def test_an_unknown_slot_or_date_is_never_reported(self):
        """An arm that invents a due instant can invent a violation."""
        sent = {"ntfy_sent_at": "2026-10-12T13:55:39Z"}
        self.assertEqual("", self.mislabel(dict(sent), "2026-10-12", "midnight"))
        self.assertEqual("", self.mislabel(dict(sent), "not-a-date", "evening"))

    def test_the_early_slack_is_one_minute_and_needs_no_more(self):
        """Clock skew between the Worker and the runner is tolerated; a lie is not.

        The same 60 seconds scripts/delivery_ledger.py already calls on time.
        """
        # The morning slot is due 01:35:00Z.
        self.assertEqual("", self.mislabel({"ntfy_sent_at": "2026-10-12T01:34:30Z"},
                                           "2026-10-12", "morning"),
                         "a 30s-skewed record is not a mislabel")
        self.assertNotEqual("", self.mislabel({"ntfy_sent_at": "2026-10-12T01:33:30Z"},
                                              "2026-10-12", "morning"),
                            "a 90s-early record is a mislabel")

    def test_a_stored_lie_is_SEEN_while_reading_the_ledger(self):
        """The arm must find a lie that is already in KV, not only prevent one.

        This is the whole point of the scan: `enforce_ledger_honesty` sees one
        entry -- the one this run is about to write -- so the 2026-10-18 evening
        bucket would stay invisible until something rewrote that exact slot.
        """
        ledger = {"schema_version": 1, "as_of": "2026-10-18T14:00:00Z",
                  "days": {"2026-10-18": {
                      "evening": dict(REAL_2026_10_18_EVENING),
                      "morning": dict(REAL_2026_10_18_MORNING_BUILD_ONLY)}}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ns["flag_mislabelled_buckets"](ledger)
        text = buf.getvalue()

        self.assertEqual(1, text.count("::error::LEDGER HONESTY VIOLATION (stored)"),
                         "expected exactly the evening bucket to be reported, got:"
                         "\n%s" % text)
        self.assertIn("2026-10-18 evening", text)
        self.assertNotIn("2026-10-18 morning", text,
                         "the build-half bucket claims no push and must not be "
                         "reported:\n%s" % text)

    def test_the_scan_survives_a_ledger_that_is_not_shaped_as_expected(self):
        """A reader that raises inside the writer would cost a real delivery."""
        for ledger in (None, {}, {"days": None}, {"days": []},
                       {"days": {"2026-10-18": None}},
                       {"days": {"2026-10-18": {"evening": "not-a-dict"}}}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.ns["flag_mislabelled_buckets"](ledger)
            self.assertEqual("", buf.getvalue(), "reported on %r" % (ledger,))

    def test_the_writer_reports_and_does_NOT_rewrite_the_entry(self):
        """KV is authoritative for delivery, so the lie is flagged, not edited."""
        entry = dict(REAL_2026_10_18_EVENING)
        before = json.dumps(entry, sort_keys=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ns["enforce_ledger_honesty"](entry, "2026-10-18", "evening",
                                              "2026-10-20T00:00:00Z")

        self.assertIn("::error::LEDGER HONESTY VIOLATION (temporal)",
                      buf.getvalue(),
                      "the writer did not report the mislabel it accepted:\n%s"
                      % buf.getvalue())
        self.assertEqual(before, json.dumps(entry, sort_keys=True),
                         "the temporal arm changed the record. KV is authoritative "
                         "for delivery (D-008) and D-008 is not this arm's to "
                         "revise; the entry must be reported and left as it is.")

    def test_the_actions_arm_recognises_a_degraded_claim_as_a_claim(self):
        """The other copy of the rule, on the kind a no-AI day actually records.

        THE GAP, in the Actions arm this time. Its gate was
        `state != 'sent' and kind != 'delivered'`, so a slot recorded
        `state: failed, kind: unsummarised` beside `ntfy_failed=true` returned
        before it looked at anything -- and that is not a hypothetical shape: it
        is what this arm writes for an unsummarised edition, which is the only
        edition it can write on a day no model answered.
        """
        for kind in ("delivered", "partial", "unsummarised"):
            with self.subTest(kind=kind):
                entry = {"state": "failed", "kind": kind, "ntfy_failed": True,
                         "http_status": 429, "attempts": 3, "error": "429"}
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.ns["enforce_ledger_honesty"](
                        entry, "2026-10-28", "morning", "2026-10-28T12:00:00Z")
                self.assertIn("honesty_downgraded_at", entry,
                              "kind=%r kept a delivery claim beside a verified "
                              "failure without correction: %r" % (kind, entry))
                self.assertIn("::error::LEDGER HONESTY VIOLATION CORRECTED",
                              buf.getvalue(),
                              "the Actions arm corrected kind=%r silently, and a "
                              "silent correction is why 2026-10-14 went unnoticed"
                              % kind)
                self.assertEqual(kind, entry.get("kind"), entry)

    def test_the_kind_that_claims_nothing_is_left_alone_here_too(self):
        """Control: `unavailable` is a non-delivery in both copies of the rule."""
        entry = {"state": "failed", "kind": "unavailable", "ntfy_failed": True,
                 "http_status": 400, "attempts": 3}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ns["enforce_ledger_honesty"](entry, "2026-10-12", "morning",
                                              "2026-10-12T02:00:00Z")
        self.assertNotIn("honesty_downgraded_at", entry,
                         "an `unavailable` slot claims no push and was "
                         "corrected anyway: %r" % (entry,))

    def test_the_arm_is_CALLED_where_the_ledger_is_written(self):
        """A defined-but-uncalled arm is decoration, and this file has none."""
        body = step_body(NTFY_STEP)
        self.assertIn("ledger_mislabel(entry, date, slot)",
                      shipped_def(body, "enforce_ledger_honesty"),
                      "the writer defines the temporal rule but never applies it "
                      "to the entry it is about to store")
        self.assertIn("flag_mislabelled_buckets(ledger)",
                      shipped_def(body, "kv_record_delivery"),
                      "the writer never scans the ledger it just read, so a lie "
                      "already sitting in KV stays invisible")


class QuotaRetryTests(unittest.TestCase):
    """DEFECT 4: a daily-quota refusal must not be retried into a spent quota."""

    def _publish_harness(self, status, attempts_expected_note=""):
        """Run the shipped `publishToNtfy` against a fetch that always refuses."""
        fn = extract_js_function("publishToNtfy")
        script = """
// Every dependency `publishToNtfy` reaches for, stubbed to the smallest value
// that lets the retry loop run. The function body itself is the shipped one.
const DELIVERY_ATTEMPTS = 3;
const DELIVERY_BACKOFF_MS = [2000, 6000];
const DELIVERY_BUDGET_MS = 120000;
const DELIVERY_ATTEMPT_TIMEOUT_MS = 15000;
const NTFY_ERROR_BODY_MAX_CHARS = 200;
function sleepMs(ms) { return Promise.resolve(); }
function ntfyTopicOf() { return "test-topic"; }
function ntfyServerOf() { return "https://ntfy.invalid"; }
function scrubTopic(s) { return s; }
let CALLS = 0;
async function fetch() {
  CALLS += 1;
  return {
    ok: false,
    status: %d,
    async text() { return JSON.stringify({code: 429, error: "daily quota"}); },
  };
}
%s
(async () => {
  const res = await publishToNtfy({}, {topic: "t"}, "seq-1", "probe");
  console.log(JSON.stringify({calls: CALLS, ok: res.ok,
                              attempts: res.attempts,
                              http_status: res.http_status,
                              quota_exhausted: res.quota_exhausted}));
})();
""" % (status, fn)
        return run_node(script)

    def test_a_429_is_attempted_ONCE_and_not_three_times(self):
        """THE QUOTA BURN.

        RED without the fix: three POSTs per invocation into an allowance ntfy
        had already refused, which is what left the 2026-10-12, 10-13 and 10-14
        evenings with `ntfy_quota_exhausted: true`.
        """
        out = self._publish_harness(429)

        self.assertEqual(out.get("calls"), 1, (
            "a daily-quota refusal was POSTed %s time(s). ntfy's quota is per "
            "visitor and per UTC day, so attempts 2 and 3 are charged against an "
            "allowance that is already empty and only delay the escalation to "
            "the Actions arm, which holds its own separate quota: %r"
            % (out.get("calls"), out)))
        self.assertIs(out.get("quota_exhausted"), True, (
            "the quota marker must still be reported, because it is what tells "
            "the owner to wait for midnight UTC instead of reading a log: %r"
            % out))
        self.assertIs(out.get("ok"), False, out)

    def test_a_5xx_is_STILL_retried_in_full(self):
        """The other half, and the reason the first test is a fix and not a
        regression. A 5xx really is transient, so the full retry budget is
        untouched -- which is exactly the distinction the two statuses make.
        """
        out = self._publish_harness(503)

        self.assertEqual(out.get("calls"), 3, (
            "a 503 is a transient fault and must keep all %d attempts; cutting it "
            "would trade a quota problem for a reliability problem: %r"
            % (3, out)))
        self.assertIs(out.get("quota_exhausted"), False, (
            "a 5xx must never be reported as quota exhaustion -- the two ask the "
            "owner for opposite things: %r" % out))
        self.assertIs(out.get("ok"), False, out)

    def test_a_successful_push_is_untouched(self):
        """The 2026-10-16 morning case: healthy, on time, one attempt. The 429
        guard must not change what a working push does."""
        fn = extract_js_function("publishToNtfy")
        script = """
const DELIVERY_ATTEMPTS = 3;
const DELIVERY_BACKOFF_MS = [2000, 6000];
const DELIVERY_BUDGET_MS = 120000;
const DELIVERY_ATTEMPT_TIMEOUT_MS = 15000;
const NTFY_ERROR_BODY_MAX_CHARS = 200;
function sleepMs(ms) { return Promise.resolve(); }
function ntfyTopicOf() { return "test-topic"; }
function ntfyServerOf() { return "https://ntfy.invalid"; }
function scrubTopic(s) { return s; }
let CALLS = 0;
async function fetch() {
  CALLS += 1;
  return { ok: true, status: 200, async text() { return JSON.stringify({id: "m1"}); } };
}
%s
(async () => {
  const res = await publishToNtfy({}, {topic: "t"}, "seq-1", "probe");
  console.log(JSON.stringify({calls: CALLS, ok: res.ok, attempts: res.attempts,
                              http_status: res.http_status,
                              message_id: res.message_id}));
})();
""" % fn
        out = run_node(script)
        self.assertEqual(out.get("calls"), 1, out)
        self.assertIs(out.get("ok"), True, out)
        self.assertEqual(out.get("http_status"), 200, out)
        self.assertEqual(out.get("message_id"), "m1", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)