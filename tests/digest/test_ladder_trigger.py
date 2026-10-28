#!/usr/bin/env python3
"""tests/digest/test_ladder_trigger.py — U7: the ladder fires on a FAILURE only.

WHAT IS BEING GUARDED
---------------------
U4 made `Fallback no-op check` read the Worker's ledger and answer
`arm` / `no_arm` / `unknown`. Two things were still missing, and they are the
difference between "the ladder exists" and "the ladder works".

1. NOTHING EVER WROTE `ntfy_failed`. The fallback step has read that field since
   U4, but no code path in `deploy/cloudflare-worker.js` has ever set it: a
   verified publish failure writes `state: "failed"` plus the error string and
   nothing else. So the `no_arm` branch was unreachable in production. The entry
   a real failure leaves has no `ntfy_sent_at` either, so the step fell through
   to `unknown` and then to the FILE rule — and a stale
   `state/last_digest_sent.json` from some earlier GitHub run says `delivered`,
   so the ladder stood down. The precise outcome this repository has already
   paid for once, reached by the same shape of bug.

   A failure is VERIFIED when `publishToNtfy` exhausted `DELIVERY_ATTEMPTS` and
   every attempt came back an error. That is deterministic arithmetic over HTTP
   statuses, so it is recorded as a fact and never as a verdict. The two facts
   that are NOT failures are recorded as `ntfy_failed: false` rather than left
   stale, because an entry carrying both `ntfy_sent_at` and a leftover
   `ntfy_failed: true` would arm the ladder on a slot that went out perfectly.

2. A FAILURE WAS INVISIBLE IN THE LOG. `ledger_verdict()` collects every way of
   failing to read the ledger into one bare `except Exception`, so a
   `CF_PAGES_API_TOKEN` whose KV-read scope does not cover the namespace, a 5xx,
   a timeout and a non-JSON value all reach the log as the same word. The
   behaviour is right — every one of them is `unknown`, which falls back to the
   file rule — but a mis-scoped token is then indistinguishable from a KV
   outage, which is how a permanently broken ledger read looks like a healthy
   one forever. The decision line now names the branch it took and the reason
   the ledger could not be consulted.

WHAT IS ASSERTED HERE
---------------------
The step's OWN bytes are executed (`first_heredoc` + `exec`), with
`urllib.request.urlopen` poisoned, so the Cloudflare KV endpoint is never
reached and no credential is needed. A re-implementation is what let a real
divergence survive a "parity" test before.

`ntfy_failed` is a Worker-side fact and its real behaviour is proved by
`tests/digest/digest_delivery.test.js` running the Worker's own code under
`node --test`. The checks here are the two that must survive in Python anyway:
that the field is still written at the failure patch and still cleared on
success, and that the ladder's own reading of it is unchanged.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
for _p in (_TESTS, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests import workflows as W  # noqa: E402
from tests.workflows.test_workflow_lint import heredoc_bodies  # noqa: E402

STEP = "Fallback no-op check"

#: Supplied through the CF_KV_NAMESPACE_ID variable. Built from two halves so no
#: 32-hex literal sits beside a NAMESPACE_ID name (test_deploy_config_ids.py).
NAMESPACE_ID = "0123456789abcdef" * 2
#: The id committed in deploy/wrangler.toml: a PLACEHOLDER that names no
#: namespace, written on disk here to prove the step does not read it.
KV_PLACEHOLDER = "abcdefabcdefabcdefabcdefabcdefab"
ACCOUNT_ID = "acc0000000000000000000000000000ab"
TOKEN = "kv-read-token-not-a-real-secret"

TODAY = "2026-10-09"
SLOT = "morning"

WRANGLER_TEMPLATE = (
    '\ufeffname = "breaking-monitor"\n'
    'main = "cloudflare-worker.js"\n'
    "\n"
    "[[kv_namespaces]]\n"
    'binding = "KV"\n'
    'id = "%s"\n'
) % KV_PLACEHOLDER

#: The four degraded kinds the supersede push and this ladder agree on.
#: `unsummarised` is the no-AI edition: a real digest sealed later the same day
#: has to be able to replace it. Kept on ONE line because a guard in
#: tests/digest/test_fallback_ledger.py reads both copies with a single-line
#: regex and compares the sets.
SUPERSEDED_KINDS = ("partial", "fallback_last_good", "unavailable", "unsummarised")


def step_dict(step_name):
    for _job, name, sd in W.iter_all_steps("daily-news.yml"):
        if name == step_name:
            return sd
    raise AssertionError("daily-news.yml has no step %r" % step_name)


def first_heredoc(step_name):
    bodies = [body for _tag, body in heredoc_bodies(step_dict(step_name)["run"])]
    if not bodies:
        raise AssertionError(
            "step %r has no Python heredoc. The step was renamed, or its body "
            "was replaced with something this suite can no longer execute — "
            "update the suite deliberately rather than deleting the check."
            % step_name)
    return bodies[0]


def exec_body(body, env, cwd):
    saved = dict(os.environ)
    os.environ.update({k: str(v) for k, v in env.items()})
    buf = io.StringIO()
    code = 0
    cwd_before = os.getcwd()
    try:
        os.chdir(str(cwd))
        with contextlib.redirect_stdout(buf):
            try:
                exec(compile(body, "<step>", "exec"), {"__name__": "step"})
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except BaseException as exc:  # noqa: BLE001 - a step crashing IS the result
                code = 1
                print("%s: %s" % (type(exc).__name__, exc))
    finally:
        os.chdir(cwd_before)
        os.environ.clear()
        os.environ.update(saved)
    return code, buf.getvalue()


def entry(**overrides):
    base = {
        "state": "sent",
        "kind": "delivered",
        "sequence_id": "dmn-%s-%s" % (TODAY, SLOT),
        "ntfy_sent_at": "2026-10-09T01:35:12Z",
        "first_ntfy_sent_at": "2026-10-09T01:35:12Z",
        "scheduled_instant": "2026-10-09T01:35:00Z",
        "ntfy_failed": False,
    }
    base.update(overrides)
    return base


def failed_entry(**overrides):
    """What the Worker must leave behind after a VERIFIED publish failure."""
    base = entry(state="failed", kind="delivered", ntfy_failed=True,
                 ntfy_sent_at=None, first_ntfy_sent_at=None,
                 error="ntfy publish failed after 3 attempt(s)")
    base.update(overrides)
    return base


def ledger_raw(slot_entry=None, when="2026-10-09T01:35:12Z"):
    days = {}
    if slot_entry is not None:
        days[TODAY] = {SLOT: slot_entry}
    return json.dumps({"schema_version": 1, "as_of": when, "days": days})


def marker(**overrides):
    """A `state/last_digest_sent.json` the file rule would treat as DELIVERED.

    DELIVERED BY DEFAULT, because that is the shape that costs the owner a
    digest: an earlier GitHub run wrote it, and it says the slot went out.
    """
    base = {
        "date": TODAY,
        "time": SLOT,
        "stories": 12,
        "empty_digest": False,
        "channels": {"website": True, "ntfy": True},
        "delivered": True,
    }
    base.update(overrides)
    return base


class _FakeResponse:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LadderHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ladder-trigger-")
        self.tmp = Path(self._tmp)
        self.addCleanup(shutil.rmtree, self._tmp, True)
        (self.tmp / "deploy").mkdir()
        (self.tmp / "deploy" / "wrangler.toml").write_text(
            WRANGLER_TEMPLATE, encoding="utf-8")
        (self.tmp / "state").mkdir()
        self.gh_env = self.tmp / "gh_env.txt"
        self.gh_env.write_text("", encoding="utf-8")
        self.summary = self.tmp / "summary.md"
        self.summary.write_text("", encoding="utf-8")
        self.requests = []

    def _run(self, kv_body=None, kv_error=None, marker_obj=None, mutate=None,
             env_overrides=None, write_marker=True, trigger="github-schedule-fallback"):
        real_urlopen = urllib.request.urlopen
        self.requests = []

        def fake_urlopen(request, *args, **kwargs):
            url = getattr(request, "full_url", request)
            self.requests.append(str(url))
            if kv_error == "http":
                raise urllib.error.HTTPError(str(url), 403, "Forbidden", {}, None)
            if kv_error == "transport":
                raise urllib.error.URLError("injected transport failure")
            if kv_error == "timeout":
                raise TimeoutError("injected read timeout")
            return _FakeResponse(kv_body if kv_body is not None else "{}")

        urllib.request.urlopen = fake_urlopen
        try:
            if write_marker and marker_obj is not None:
                (self.tmp / "state" / "last_digest_sent.json").write_text(
                    json.dumps(marker_obj), encoding="utf-8")
            body = first_heredoc(STEP)
            if mutate is not None:
                new_body, hits = mutate(body)
                self.assertEqual(1, hits,
                                 "the mutation target matched %d time(s) in the "
                                 "step's body, expected 1. The workflow has "
                                 "drifted from the text this proof was written "
                                 "against." % hits)
                body = new_body
            env = {
                "GITHUB_ENV": str(self.gh_env),
                "GITHUB_STEP_SUMMARY": str(self.summary),
                "SLOT": SLOT,
                "TODAY": TODAY,
                "CF_LEDGER_TOKEN": TOKEN,
                "CF_ACCOUNT_ID": ACCOUNT_ID,
                "CF_KV_NAMESPACE_ID": NAMESPACE_ID,
                "DRIGGER_SOURCE": trigger,
                "TRIGGER_SOURCE": trigger,
            }
            env.update(env_overrides or {})
            return exec_body(body, env, self.tmp)
        finally:
            urllib.request.urlopen = real_urlopen

    def _armed(self):
        return "FALLBACK_NOOP=true" in self.gh_env.read_text(encoding="utf-8")

    def _forget(self):
        self.gh_env.write_text("", encoding="utf-8")
        marker_path = self.tmp / "state" / "last_digest_sent.json"
        if marker_path.exists():
            marker_path.unlink()

    @staticmethod
    def _branch(out):
        match = re.search(r"branch=(\S+)", out)
        return match.group(1) if match else None

    @staticmethod
    def _ledger_reason(out):
        match = re.search(r"ledger=(?:arm|no_arm|unknown)\s*\(([^)]*)\)", out)
        return match.group(1) if match else None


# ==========================================================================
# 1. THE LADDER DOES NOT FIRE WHEN NTFY SUCCEEDED
# ==========================================================================


class SuccessDoesNotFireTheLadderTests(LadderHarness):
    def test_a_delivered_slot_stands_the_ladder_down(self):
        code, out = self._run(kv_body=ledger_raw(entry()), marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(), out)
        self.assertEqual("noop_ledger", self._branch(out),
                         "the run stood the ladder down but did not name the "
                         "branch, so the log cannot be read back to a "
                         "decision:\n%s" % out)

    def test_a_routine_duplicate_is_not_a_failure(self):
        """A SECOND PUSH IS NOT A FAILURE.

        The ledger guard suppresses a repeat send without writing anything, so
        the entry on disk still carries the original successful push. That is
        the shape the 07:50 run finds on every ordinary day, and it must stand
        the ladder down: firing here would email and Telegram the owner a second
        copy of a digest their phone already received.
        """
        code, out = self._run(
            kv_body=ledger_raw(entry(kind="duplicate_suppressed")),
            marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "a routine duplicate armed the email/Telegram ladder. A "
                        "second push is not a failure, and the ladder exists for "
                        "failures only.\n%s" % out)

    def test_a_superseded_push_is_not_a_routine_duplicate(self):
        """The control for the case above: `partial` IS a degraded push.

        A `partial` artifact was still PUSHED, so `ntfy_sent_at` is set and the
        owner did receive something — but it says on its face that it is
        incomplete. That is deliberately NOT the ladder's case: the ladder is
        for a slot that reached nobody, and the supersede push is what repairs
        a partial one.
        """
        code, out = self._run(
            kv_body=ledger_raw(entry(kind="partial", is_partial=True)),
            marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(), out)


# ==========================================================================
# 2. THE LADDER DOES FIRE ON A VERIFIED FAILURE
# ==========================================================================


class VerifiedFailureFiresTheLadderTests(LadderHarness):
    def test_a_recorded_ntfy_failure_runs_the_ladder(self):
        code, out = self._run(kv_body=ledger_raw(failed_entry()),
                              marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(),
                         "a verified ntfy failure stood the whole fallback "
                         "ladder down, with the marker saying delivered and the "
                         "job finishing green.\n%s" % out)
        self.assertEqual("verified_failure", self._branch(out),
                         "the ladder fired but the log does not say WHICH branch "
                         "fired, so a run cannot be read back:\n%s" % out)

    def test_an_unavailable_push_runs_the_ladder(self):
        """`ntfy_sent_at` IS set here — the "digest unavailable" notice went out.

        This is the case that makes "did anything get pushed" the wrong
        question: a push happened, and it told the owner nothing they can read.
        """
        code, out = self._run(kv_body=ledger_raw(entry(kind="unavailable")),
                              marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(),
                         "a slot whose only push was the `unavailable` notice "
                         "was treated as delivered and the ladder stood down.\n%s"
                         % out)

    def test_a_push_the_owner_CAN_read_does_not_run_the_ladder(self):
        """THE LINE THE LADDER MUST NOT CROSS.

        `partial` and `fallback_last_good` are both real pushes carrying
        `ntfy_sent_at`, and both reach the owner with something in them — a
        banner over half the stories, or yesterday's news labelled as such.
        The owner is not missing the day's news; they are getting a worse
        version of it, and emailing and Telegraming them on top of that is a
        duplicate, not a rescue.

        What repairs those two is the SUPERSEDE push (see test_supersede_push.py),
        which replaces the push instead of adding a third notification to it.
        The ladder is for a slot that reached nobody at all.

        THE CONTRACT IS ABOUT THE LADDER, AND ONLY THE LADDER. It is not a
        claim that the owner received today's edition, and it must never be read
        as one: `fallback_last_good` reaches him with yesterday's (or, on
        2026-10-27, nine-days-old) news. Saying so is the Worker's job, not this
        ladder's — `?health=1` now reports the derived `edition` of the last
        delivery (`fresh` / `fallback` / `partial` / `failed`) plus
        `fresh_edition`, `fallback_shown`, `last_good_shown` and `reason`, and a
        slot that served a fallback gets ONE recovery notice naming the edition
        actually shown. Those live in deploy/cloudflare-worker.js and are pinned
        in tests/digest/digest_delivery.test.js §5d, because a shell ladder
        runs once per slot and the alert has to be once per slot too. So the
        assertion below stays exactly as it is: arming this ladder on a readable
        push would be a regression, not a fix for the reporting gap.
        """
        for kind in ("partial", "fallback_last_good"):
            with self.subTest(kind=kind):
                self._forget()
                code, out = self._run(kv_body=ledger_raw(entry(kind=kind)),
                                      marker_obj=marker())
                self.assertEqual(0, code, out)
                self.assertTrue(self._armed(),
                                "a %r push ran the email/Telegram ladder. That "
                                "owner already received a digest; a second "
                                "notification is a duplicate, not a rescue.\n%s"
                                % (kind, out))

    #: The mutation that turns the ladder off, written against the PARSED
    #: heredoc body — YAML has already removed the step's indentation by the
    #: time these bytes exist, so a target written against the file's
    #: indentation would match nothing and the proof would be decoration.
    NTFY_FAILED_TERM_LOST = (
        "\n    if entry.get('ntfy_failed'):\n        return NO_ARM",
        "\n    if entry.get('ntfy_failed'):\n        return ARM",
    )

    def test_losing_the_ntfy_failed_term_would_silence_the_ladder(self):
        """A guard that is never seen to matter is decoration."""
        old, new = self.NTFY_FAILED_TERM_LOST

        def mutate(body):
            return body.replace(old, new), body.count(old)

        code, out = self._run(kv_body=ledger_raw(failed_entry()),
                              marker_obj=marker(), mutate=mutate)
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "with the `ntfy_failed` term mutated to `arm`, the "
                        "ladder did NOT arm. The mutation no longer demonstrates "
                        "the defect it was written for.\n%s" % out)


# ==========================================================================
# 3. `ntfy_failed` IS WRITTEN BY THE WORKER — and cleared when it stops
# ==========================================================================


class WorkerRecordsTheFailureTests(unittest.TestCase):
    """The producer side, in Python, because it must hold without node too.

    The real behavioural proof is `tests/digest/digest_delivery.test.js`, which
    runs the Worker's own code under `node --test` with a fake KV and a fake
    fetch. These checks are the ones that survive when that suite is not
    running, and they are deliberately about the two facts that cannot be got
    wrong quietly: the failure patch SETS the flag, and the success patch
    CLEARS it.
    """

    @classmethod
    def setUpClass(cls):
        cls.worker = (W.REPO_ROOT / "deploy" / "cloudflare-worker.js").read_text(
            encoding="utf-8", errors="replace")

    def test_a_verified_publish_failure_records_ntfy_failed(self):
        # The patch the Worker writes after publishToNtfy returned ok:false.
        match = re.search(
            r"state:\s*STATE_FAILED,(?P<body>.{0,600}?)\n\s*\},", self.worker, re.S)
        self.assertIsNotNone(
            match,
            "the publish-failure ledger patch is gone from "
            "deploy/cloudflare-worker.js. Without it a verified failure leaves "
            "no `ntfy_failed`, the 07:50 fallback reads `unknown`, and the file "
            "rule stands the ladder down.")
        self.assertIn(
            "ntfy_failed: true", match.group("body"),
            "the verified-failure patch does not record `ntfy_failed: true`. "
            "That is the field the fallback step's `no_arm` branch reads, and "
            "it is the only thing that makes the ladder fire on a real failure.")

    def test_a_successful_push_clears_a_previous_ntfy_failed(self):
        """The flag that is never cleared is a duplicate email every day.

        `commitDigestLedger` merges with `Object.assign`, so a success that omits
        the field leaves a stale `ntfy_failed: true` sitting next to a fresh
        `ntfy_sent_at`. `ntfy_failed` outranks `ntfy_sent_at` in the step's
        ledger verdict, so the next 07:50 run would fire the ladder at a slot
        that went out perfectly.

        All THREE success patches are checked, each anchored on something only
        that patch carries. Clearing one and not the others is the defect.
        """
        anchors = {
            "read-back dedup of the real digest": (
                'deduped_via: "sequence_id read-back"', "ntfy_failed: false"),
            "a successful real-digest push": (
                "is_partial: artifact.is_partial === true ? true : false,",
                "ntfy_failed: false"),
            # This patch records one flag for BOTH outcomes, because a successful
            # fallback push DID reach the owner and must clear an earlier
            # failure exactly as a successful real digest does.
            "the fallback ladder's outcome": (
                "state: published.ok ? STATE_SENT : STATE_FAILED,",
                "ntfy_failed: published.ok ? false : true"),
        }
        for label, (anchor, token) in anchors.items():
            with self.subTest(patch=label):
                indexes = [m.start() for m in re.finditer(re.escape(anchor), self.worker)]
                self.assertTrue(indexes, anchor)
                for index in indexes:
                    window = self.worker[max(0, index - 900): index + 900]
                    self.assertIn(
                        token, window,
                        "the %s success patch does not record `%s`. A stale true "
                        "would sit beside the fresh `ntfy_sent_at` and arm the "
                        "email/Telegram ladder on a slot that was delivered."
                        % (label, token))

    def test_a_pending_write_ahead_never_claims_a_failure(self):
        """`pending` is "we believe we sent it and cannot prove it".

        It is not a failure, and recording one would fire the ladder on every
        re-fire that is merely mid-flight.
        """
        pending_blocks = re.findall(
            r"state:\s*STATE_PENDING,(?P<body>.{0,600}?)\n\s*\},", self.worker, re.S)
        self.assertGreaterEqual(len(pending_blocks), 2,
                                "the write-ahead pending patches are gone")
        for body in pending_blocks:
            self.assertNotIn(
                "ntfy_failed", body,
                "a `pending` write-ahead patch records `ntfy_failed`. A push in "
                "flight is not a failure; only a publish that exhausted its "
                "attempts is.")

    def test_the_js_suite_still_names_the_recorded_failure(self):
        js = (W.REPO_ROOT / "tests" / "digest" / "digest_delivery.test.js").read_text(
            encoding="utf-8")
        self.assertIn(
            "a verified publish failure records ntfy_failed", js,
            "tests/digest/digest_delivery.test.js no longer covers the "
            "failure flag the whole ladder trigger depends on. Deleting that "
            "test must be a deliberate act recorded in the report.")


# ==========================================================================
# 4. `unknown` FAILS OPEN, AND THE LOG SAYS SO
# ==========================================================================


class UnknownFailsOpenTests(LadderHarness):
    def test_an_unreadable_ledger_fails_open_to_the_file_rule(self):
        for error in ("http", "transport", "timeout"):
            with self.subTest(error=error):
                self._forget()
                code, out = self._run(kv_error=error, marker_obj=marker())
                self.assertEqual(0, code,
                                 "a %s ledger read made the step exit %d; a KV "
                                 "outage must never fail the digest job.\n%s"
                                 % (error, code, out))
                self.assertTrue(self._armed(),
                                "a %s ledger read turned a delivered slot into a "
                                "fallback run, so the owner's digest arrives "
                                "twice.\n%s" % (error, out))
                self.assertIsNotNone(
                    self._branch(out),
                    "the decision line names no branch, so a run cannot be read "
                    "back to the decision it took:\n%s" % out)

                self._forget()
                code, out = self._run(kv_error=error, write_marker=False)
                self.assertEqual(0, code, out)
                self.assertFalse(self._armed(),
                                 "a %s ledger read with no marker suppressed the "
                                 "fallback anyway, which is the ladder being "
                                 "disabled by a problem it has nothing to do "
                                 "with:\n%s" % (error, out))

    def test_an_unreadable_ledger_fails_open_NOT_closed(self):
        """The direction that costs a digest.

        An unreadable ledger with a marker that claims delivery must NOT stand
        the ladder down on its own: `unknown` falls back to the file rule
        EXACTLY, and the file rule says delivered. This test pins that exact
        behaviour rather than a general "fail open", because "fail open" read
        the other way would be a fail-CLOSED.
        """
        code, out = self._run(kv_error="http", marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(), out)
        self.assertEqual("unknown", re.search(r"ledger=(\w+)", out).group(1),
                         "a failed KV read did not report `unknown`; it must "
                         "never report a verdict it could not reach.\n%s" % out)

    def test_the_decision_line_names_why_the_ledger_could_not_be_read(self):
        """A MIS-SCOPED TOKEN MUST BE DIAGNOSABLE FROM A RUN.

        `ledger_verdict()` collapses an unset secret, a namespace that will not
        parse, a 403 from a token without the storage scope, a 5xx, a timeout
        and a non-JSON value into one bare `except Exception`. Every one of them
        is `unknown` and every one of them is correct — but printed as the bare
        word, a `CF_PAGES_API_TOKEN` that can deploy Pages and cannot read KV
        looks exactly like a Cloudflare outage, forever.
        """
        cases = [
            ("no token", {"CF_LEDGER_TOKEN": ""}, None),
            ("no account", {"CF_ACCOUNT_ID": ""}, None),
            ("a 403", {}, "http"),
            ("a transport failure", {}, "transport"),
            ("a timeout", {}, "timeout"),
            ("a non-JSON value", {}, None),
        ]
        for name, overrides, error in cases:
            with self.subTest(case=name):
                self._forget()
                kv_body = "not json at all" if name == "a non-JSON value" else ledger_raw(entry())
                code, out = self._run(kv_body=kv_body, kv_error=error,
                                      marker_obj=marker(), env_overrides=overrides)
                self.assertEqual(0, code, out)
                self.assertEqual(
                    "unknown", re.search(r"ledger=(\w+)", out).group(1),
                    "%s did not produce `unknown`:\n%s" % (name, out))
                reason = self._ledger_reason(out)
                self.assertTrue(
                    reason,
                    "%s produced a decision line with no reason, so the run "
                    "cannot be told apart from any other failure to read the "
                    "ledger:\n%s" % (name, out))
                self.assertNotIn(
                    TOKEN, out,
                    "the KV token reached the step log; it travels in an "
                    "Authorization header and an exception string can quote a "
                    "request:\n%s" % out)

    def test_every_branch_is_named_distinctly(self):
        """NINE RUNS, EIGHT DECISIONS, EIGHT NAMES.

        Two of them sharing a name is a log that cannot answer the only question
        anyone reads it for: which decision did this run take. A prebuild and a
        dry run are the same DECISION (a non-pushing run no delivery record may
        suppress) for two different REASONS, and the reason is what the reader
        acts on — so they are named apart too.

        EVERY ENVIRONMENT HERE IS ONE THE WORKFLOW CAN PRODUCE. `ntfy_dry_run`
        is not an input: the phase step derives it at daily-news.yml:318-324 from
        exactly `phase == prebuild or dry_run`, so a dry run ALWAYS arrives with
        it true, and the older case that set DRY_RUN alone was testing a state
        the dispatch form cannot create — which is why it was the only thing
        keeping `dry_run_not_suppressed` reachable, and why the arm looked live
        while being dead. `dry_run` and `phase` are independent inputs, so
        `phase=prebuild` with `dry_run=true` is a real third state.
        """
        seen = {}
        cases = [
            ("delivered", ledger_raw(entry()), marker(), {}, "noop_ledger"),
            ("verified failure", ledger_raw(failed_entry()), marker(), {},
             "verified_failure"),
            ("unavailable", ledger_raw(entry(kind="unavailable")), marker(), {},
             "verified_failure"),
            ("ledger unreadable", ledger_raw(entry()), marker(),
             {"kv_error": "http"}, "noop_marker"),
            ("ledger unreadable, no marker", ledger_raw(entry()), None,
             {"kv_error": "http", "write_marker": False}, "fail_open"),
            ("dry run never suppressed", ledger_raw(entry()), marker(),
             {"env_overrides": {"DRY_RUN": "true", "NTFY_DRY_RUN": "true"}},
             "dry_run_not_suppressed"),
            ("prebuild never suppressed", ledger_raw(entry()), marker(),
             {"env_overrides": {"PHASE": "prebuild", "NTFY_DRY_RUN": "true"}},
             "prebuild_not_suppressed"),
            ("a dry prebuild", ledger_raw(entry()), marker(),
             {"env_overrides": {"PHASE": "prebuild", "DRY_RUN": "true",
                                "NTFY_DRY_RUN": "true"}},
             "dry_run_prebuild_not_suppressed"),
            ("supersede dispatch", ledger_raw(entry(kind="partial")), marker(),
             {"trigger": "supersede"}, "supersede_runs"),
        ]
        for name, kv_body, mark, extra, expect in cases:
            with self.subTest(case=name):
                self._forget()
                extra = dict(extra)
                error = extra.pop("kv_error", None)
                code, out = self._run(kv_body=kv_body, kv_error=error,
                                      marker_obj=mark, **extra)
                self.assertEqual(0, code, out)
                seen[name] = self._branch(out)
                self.assertEqual(
                    expect, seen[name],
                    "case %r took branch %r, expected %r.\n%s"
                    % (name, seen[name], expect, out))
        self.assertEqual(
                    len(set(seen.values())), 8,
                    "eight decisions share %d branch name(s), so the log line "
                    "cannot tell them apart: %r"
                    % (len(set(seen.values())), seen))


# ==========================================================================
# 5. THE SUPERSEDE RUN IS NOT SUPPRESSED BY THE FIRST PUSH'S RECORD
# ==========================================================================


class SupersedeRunTests(LadderHarness):
    def test_a_supersede_dispatch_is_not_armed_down_by_the_first_push(self):
        """`ntfy_sent_at` IS set — the degraded push went out.

        The supersede dispatch exists precisely to replace that push with the
        full digest, so arming the run on the first push's own record would make
        the supersede a green run that delivered nothing: the dispatcher would
        report a superseded slot forever while the owner kept the partial.
        """
        code, out = self._run(
            kv_body=ledger_raw(entry(kind="partial", is_partial=True)),
            marker_obj=marker(), trigger="supersede")
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(),
                         "the supersede dispatch stood itself down on the "
                         "record of the very push it exists to replace.\n%s" % out)

    def test_a_supersede_dispatch_is_not_armed_down_by_the_marker(self):
        code, out = self._run(kv_body=ledger_raw(entry(kind="unavailable")),
                              marker_obj=marker(), trigger="supersede")
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(), out)

    def test_a_supersede_dispatch_survives_an_unreadable_ledger(self):
        """Fail OPEN, as everywhere else in this step.

        The guard that stops the supersede from looping is
        `full_artifact_available_at`, and it is read by the seal job that
        dispatches it — not here. Standing a supersede down because KV was
        unreadable would turn a storage blip into a permanently superseded slot.
        """
        code, out = self._run(kv_error="http", marker_obj=marker(),
                              trigger="supersede")
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(), out)

    def test_an_ordinary_run_is_unaffected_by_the_supersede_term(self):
        """The control. Without it the term above would be vacuous, and worse:
        a term that fires for every run would restore the double push.

        `prebuild` used to sit in this loop, asserting that a PREBUILD is armed
        down by a delivered slot. That was the defect written down as a
        requirement: the trigger word alone says nothing about how the run
        pushes, and a real prebuild must never be armed down. Its real shape is
        tested separately, below.
        """
        for trigger in ("github-schedule-fallback", "worker", "manual"):
            with self.subTest(trigger=trigger):
                self._forget()
                code, out = self._run(kv_body=ledger_raw(entry()),
                                      marker_obj=marker(), trigger=trigger)
                self.assertEqual(0, code, out)
                self.assertTrue(self._armed(),
                                "a %s run was not armed down by a delivered "
                                "slot.\n%s" % (trigger, out))

    def test_a_real_prebuild_is_never_armed_down(self):
        """THE SHAPE THE SEAL DEPENDS ON, not the trigger word.

        A real prebuild arrives as PHASE=prebuild with NTFY_DRY_RUN=true and
        DRY_RUN still FALSE — it publishes the page for real and pushes nothing,
        because its notification path runs in payload-only mode. Arming it down
        skips the ntfy step, and that step's dry-run branch is the only place the
        payload the seal job reads is written: the artifact is never sealed, the
        owner never gets the full digest, and every job stays green.
        """
        for label, extra in (
            ("the real prebuild shape",
             {"env_overrides": {"PHASE": "prebuild", "NTFY_DRY_RUN": "true"}}),
            ("NTFY_DRY_RUN alone, with no phase",
             {"env_overrides": {"NTFY_DRY_RUN": "true"}}),
        ):
            with self.subTest(case=label):
                self._forget()
                code, out = self._run(kv_body=ledger_raw(entry()),
                                      marker_obj=marker(), trigger="prebuild",
                                      **extra)
                self.assertEqual(0, code, out)
                self.assertFalse(self._armed(),
                                 "%s was armed down by a delivered slot, so the "
                                 "seal never gets its payload.\n%s" % (label, out))


# ==========================================================================
# 6. the step still obeys the shell and YAML rules
# ==========================================================================


class StepShapeTests(LadderHarness):
    def test_the_step_body_compiles_and_its_heredoc_is_terminated(self):
        body = first_heredoc(STEP)
        compile(body, "<fallback>", "exec")
        run = step_dict(STEP)["run"]
        self.assertTrue(run.rstrip().endswith("PY"),
                        "the step's block scalar does not end on its heredoc "
                        "terminator, so bash never saw a column-0 PY:\n%s"
                        % run[-200:])

    def test_the_step_passes_bash_syntax_check(self):
        bash = W.bash_executable()
        self.assertIsNotNone(bash, "bash is required to syntax-check a run block")
        proc = W.bash_syntax_check(step_dict(STEP)["run"], bash)
        self.assertEqual(0, proc.returncode,
                         "`Fallback no-op check` is not valid bash:\n%s"
                         % (proc.stdout or "") + (proc.stderr or ""))

    def test_the_step_takes_the_trigger_source_from_the_dispatch_input(self):
        run = step_dict(STEP)["run"]
        self.assertIn(
            "TRIGGER_SOURCE", run,
            "the step cannot see who dispatched it, so it cannot tell a "
            "supersede run from an ordinary one.")


if __name__ == "__main__":
    unittest.main(verbosity=2)