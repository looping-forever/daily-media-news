#!/usr/bin/env python3
"""tests/digest/test_fallback_ledger.py — the 07:50 no-op check consults the ledger.

WHAT IS BEING GUARDED
---------------------
Unit U5 makes the Worker deliver the digest itself, from KV, at the scheduled
instant. Nothing on the GitHub side writes `state/last_digest_sent.json` on that
path any more — and it never could: that file is written by a GitHub run, so the
07:20 Worker push is invisible in it. The marker is also SINGLE-SLOT and
overwritten by every run, which is why the watchdog cannot use it to judge the
preceding slot.

So the `Fallback no-op check` that decides whether the 07:50 run stands the
whole email/Telegram ladder down has to read the Worker's ledger as well:

    07:20  Worker pushes, records ntfy_sent_at in digest:ledger
    07:50  fallback reads the file  -> stale / missing
    07:50  fallback reads the LEDGER -> "this slot went out"
           -> FALLBACK_NOOP=true, ladder stood down, correctly

AND THE OPPOSITE CASE IS THE ONE THAT COSTS THE OWNER A DIGEST. A slot whose
ntfy send FAILED, or whose only push was the "digest unavailable" notice, has
still written `ntfy_sent_at`. A check that reads only "did anything go out"
would stand the ladder down precisely when the ladder is the thing that has to
fire. So `ntfy_failed` and `kind == "unavailable"` outrank it and the ladder
runs.

AND EVERY WAY OF FAILING TO READ THE LEDGER IS `unknown`, NEVER `arm` AND NEVER
`no_arm`. The ledger is an addition to a decision that already worked; a KV
outage, a token without the storage scope, or a malformed value must produce
exactly today's file-based behaviour. Anything else would let a Cloudflare
problem silently disable the fallback ladder — which is the failure mode of the
single-slot marker this unit exists to reduce, repeated one layer up.

OFFLINE BY CONSTRUCTION
-----------------------
`urllib.request.urlopen` is poisoned before the step's real Python runs, so the
Cloudflare KV endpoint is never reached and no credential is needed. The step's
own bytes are executed (`first_heredoc` + `exec`), not a re-implementation: a
proxy is what let a real divergence survive a "parity" test before.
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

#: The step under test, by its committed name.
STEP = "Fallback no-op check"

#: The KV key the Worker's own DIGEST_LEDGER_KEY constant names. Pinned
#: against that constant at the bottom of this file — a literal in a workflow
#: is a second place to correct, and a wrong one is a fallback that silently
#: reads an absent key and never arms.
LEDGER_KEY = "digest:ledger"

#: A plausible namespace id, supplied through the CF_KV_NAMESPACE_ID variable.
#: The step must read the repository variable and NEVER deploy/wrangler.toml,
#: and the test asserts the URL it built uses THIS one. Built from two halves so
#: no 32-hex literal sits beside a NAMESPACE_ID name (test_deploy_config_ids.py
#: rejects those, and a fixture is not a place to carve an exception).
NAMESPACE_ID = "0123456789abcdef" * 2
#: The id actually committed in deploy/wrangler.toml. It is a PLACEHOLDER that
#: names no namespace -- Cloudflare answers 10013 -- which is why the step may
#: never read that file.
KV_PLACEHOLDER = "abcdefabcdefabcdefabcdefabcdefab"
ACCOUNT_ID = "acc0000000000000000000000000000ab"
TOKEN = "kv-read-token-not-a-real-secret"

TODAY = "2026-10-09"
SLOT = "morning"

#: A miniature of deploy/wrangler.toml, BOM and all. It carries the PLACEHOLDER
#: on purpose: the step is given this file and must still use the variable, so
#: a regression to reading the config turns the namespace assertion red instead
#: of silently querying a namespace that does not exist.
WRANGLER_TEMPLATE = (
    '\ufeffname = "breaking-monitor"\n'
    'main = "cloudflare-worker.js"\n'
    "\n"
    "[triggers]\n"
    'crons = ["*/2 * * * *", "*/3 * * * *"]\n'
    "\n"
    "[[kv_namespaces]]\n"
    'binding = "KV"\n'
    'id = "%s"\n'
) % KV_PLACEHOLDER


def step_dict(step_name):
    for _job, name, sd in W.iter_all_steps("daily-news.yml"):
        if name == step_name:
            return sd
    raise AssertionError("daily-news.yml has no step %r" % step_name)


def first_heredoc(step_name):
    """The step's first Python heredoc body — its real bytes, not a copy."""
    bodies = [body for _tag, body in heredoc_bodies(step_dict(step_name)["run"])]
    if not bodies:
        raise AssertionError(
            "step %r has no Python heredoc. The step was renamed, or its body "
            "was replaced with something this suite can no longer execute — "
            "update the suite deliberately rather than deleting the check."
            % step_name)
    return bodies[0]


def exec_body(body, env, cwd):
    """Run the step's Python. An uncaught exception is a FAILED step, as in bash.

    The exception is caught rather than propagated so the mutation proofs can
    assert on the exit code the way Actions would see it, and so its text
    lands in the captured output where a failure message can quote it.
    """
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
    """A ledger slot entry, defaulted to a completed send."""
    base = {
        "state": "sent",
        "kind": "delivered",
        "sequence_id": "dmn-%s-%s" % (TODAY, SLOT),
        "ntfy_sent_at": "2026-10-09T01:35:12Z",
        "first_ntfy_sent_at": "2026-10-09T01:35:12Z",
        "scheduled_instant": "2026-10-09T01:35:00Z",
    }
    base.update(overrides)
    return base


def ledger_raw(slot_entry=None, when="2026-10-09T01:35:12Z"):
    days = {}
    if slot_entry is not None:
        days[TODAY] = {SLOT: slot_entry}
    return json.dumps({"schema_version": 1, "as_of": when, "days": days})


def marker(**overrides):
    """A `state/last_digest_sent.json` the file rule would treat as DELIVERED."""
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
    """The context manager `urlopen` returns for a successful KV read."""

    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FallbackLedgerTests(unittest.TestCase):
    """The fallback decision, executed against the real step body."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fallback-ledger-")
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

    # -- harness -----------------------------------------------------------

    def _run(self, kv_body=None, kv_error=None, marker_obj=None,
             mutate=None, env_overrides=None, write_marker=True):
        """Run the step with a poisoned urlopen. Returns (code, output)."""
        real_urlopen = urllib.request.urlopen
        self.requests = []

        def fake_urlopen(request, *args, **kwargs):
            url = getattr(request, "full_url", request)
            self.requests.append({
                "url": str(url),
                "headers": {k.lower(): v for k, v in
                            (dict(getattr(request, "headers", None) or {})).items()},
            })
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
                                 "the mutation target matched %d time(s) in "
                                 "the step's body, expected 1. The workflow "
                                 "has drifted from the text this proof was "
                                 "written against." % hits)
                body = new_body
            env = {
                "GITHUB_ENV": str(self.gh_env),
                "GITHUB_STEP_SUMMARY": str(self.summary),
                "SLOT": SLOT,
                "TODAY": TODAY,
                "CF_LEDGER_TOKEN": TOKEN,
                "CF_ACCOUNT_ID": ACCOUNT_ID,
                "CF_KV_NAMESPACE_ID": NAMESPACE_ID,
            }
            env.update(env_overrides or {})
            return exec_body(body, env, self.tmp)
        finally:
            urllib.request.urlopen = real_urlopen

    def _armed(self):
        return "FALLBACK_NOOP=true" in self.gh_env.read_text(encoding="utf-8")

    def _forget(self):
        """Start a run from nothing: no decision carried, no marker on disk.

        Without this a second `_run` in one test inherits the first one's
        `FALLBACK_NOOP=true` and its marker file, and the assertion that was
        meant to prove "the fallback still ran" proves nothing at all.
        """
        self.gh_env.write_text("", encoding="utf-8")
        marker_path = self.tmp / "state" / "last_digest_sent.json"
        if marker_path.exists():
            marker_path.unlink()

    # -- 1. a slot the ledger says went out --------------------------------

    def test_the_fallback_arms_when_the_ledger_records_a_push_for_this_slot(self):
        """THE U4 CASE. The file cannot know; the ledger can.

        The marker here is explicitly NOT delivered — which is the shape the
        07:50 run actually finds once U5 is live, because no GitHub run wrote
        it for this slot at all.
        """
        code, out = self._run(kv_body=ledger_raw(entry()),
                              marker_obj=marker(delivered=False, stories=12))
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "the ledger records ntfy_sent_at for this slot and the "
                        "fallback still ran: the owner's digest would be "
                        "delivered a second time, by email and Telegram, thirty "
                        "minutes after the push that already succeeded.\n%s"
                        % out)

    def test_the_record_the_stand_down_writes_stands_the_ladder_down(self):
        """THE 2026-10-26 CASE, through the real decision step, both halves.

        The slot was delivered to the topic by a run whose KV write failed, so
        the stored entry read `pending` with the earlier attempt's 429 and
        `ntfy_failed=true` -- and this step armed, because `ntfy_failed`
        outranks every other fact here. The stand-down now writes the same
        record a real push writes: state=sent, kind=delivered, an instant from
        the read-back, and no failure markers. The verdict must flip.
        """
        pending = entry(state="pending", kind="fallback_last_good",
                        ntfy_sent_at=None, first_ntfy_sent_at=None,
                        ntfy_failed=True, http_status=429)
        code, out = self._run(kv_body=ledger_raw(pending),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertFalse(
            self._armed(),
            "state=pending with ntfy_failed=true must still stand the ladder "
            "UP -- that is the state the stand-down repairs, and if it stopped "
            "arming the repair would be invisible:\n%s" % out)

        self._forget()
        delivered = entry(state="sent", kind="delivered",
                          ntfy_sent_at="2026-10-26T13:52:07Z",
                          first_ntfy_sent_at="2026-10-26T13:52:07Z")
        code, out = self._run(kv_body=ledger_raw(delivered),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertTrue(
            self._armed(),
            "the record the stand-down writes did not stand the ladder down, so "
            "the owner still gets a digest the topic already carries, by email "
            "and Telegram:\n%s" % out)

    def test_the_ledger_is_read_from_the_committed_namespace_and_account(self):
        code, out = self._run(kv_body=ledger_raw(entry()),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertEqual(1, len(self.requests),
                         "the step must read the ledger exactly once, and "
                         "exactly once is what keeps this step cheap enough to "
                         "sit in front of every fallback run:\n%s" % out)
        url = self.requests[0]["url"]
        self.assertIn(NAMESPACE_ID, url,
                      "the KV read did not use the CF_KV_NAMESPACE_ID variable, "
                      "so it would read a key that does not exist and the "
                      "fallback would never arm: " + url)
        self.assertNotIn(KV_PLACEHOLDER, url,
                         "the KV read used the published placeholder id: " + url)
        self.assertIn(ACCOUNT_ID, url, url)
        self.assertTrue(url.endswith("/" + LEDGER_KEY), url)
        self.assertEqual("bearer " + TOKEN,
                         self.requests[0]["headers"].get("authorization", "").lower())

    # -- 2. the slot the ledger says did NOT reach the owner ---------------

    def test_a_failed_ntfy_push_in_the_ledger_does_not_arm_the_fallback(self):
        """The file alone would arm here, and arming is the wrong answer.

        The marker says this slot was delivered (an earlier GitHub run wrote
        it); the ledger says today's send failed, so the email/Telegram ladder
        is the only thing left that can reach the owner.
        """
        code, out = self._run(
            kv_body=ledger_raw(entry(state="failed", kind="delivered",
                                     ntfy_failed=True,
                                     ntfy_sent_at=None,
                                     first_ntfy_sent_at=None)),
            marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(),
                         "a ledger entry with ntfy_failed stood the whole "
                         "fallback ladder down. That is precisely the case the "
                         "ladder exists for: the primary channel failed, and "
                         "the backups were silenced by a file that describes an "
                         "earlier run.\n%s" % out)

    def test_an_unavailable_push_in_the_ledger_does_not_arm_the_fallback(self):
        """`ntfy_sent_at` IS set here — the "digest unavailable" notice went out.

        This is the case that makes "did anything get pushed" the wrong
        question: a push happened, and it told the owner nothing they can read.
        """
        code, out = self._run(
            kv_body=ledger_raw(entry(kind="unavailable")),
            marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(),
                         "a slot whose only push was the `unavailable` notice "
                         "was treated as delivered and the ladder stood down. "
                         "The owner would be told 'the digest is unavailable' "
                         "and then nothing else.\n%s" % out)

    def test_a_ledger_entry_with_neither_fact_leaves_the_file_rule_alone(self):
        """`pending` is not `sent`. It must not arm, and must not veto either."""
        code, out = self._run(
            kv_body=ledger_raw(entry(state="pending", kind="delivered",
                                     ntfy_sent_at=None,
                                     first_ntfy_sent_at=None)),
            marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(), out)

    # -- 3. FAIL OPEN ------------------------------------------------------

    def test_a_kv_read_error_fails_open_to_the_file_rule(self):
        """A KV outage must reproduce today's behaviour EXACTLY.

        Both directions are asserted, because a fail-open that only works when
        the answer is "run the fallback" is a fail-CLOSED with extra steps.
        """
        for error in ("http", "transport", "timeout"):
            with self.subTest(error=error):
                self._forget()
                code, out = self._run(kv_error=error, marker_obj=marker())
                self.assertEqual(0, code,
                                 "a %s ledger read made the step exit %d; a KV "
                                 "outage must never fail the digest job:\n%s"
                                 % (error, code, out))
                self.assertTrue(self._armed(),
                                "a %s ledger read turned a delivered slot into "
                                "a fallback run, so the owner's digest arrives "
                                "twice:\n%s" % (error, out))

                self._forget()
                code, out = self._run(kv_error=error, write_marker=False)
                self.assertEqual(0, code, out)
                self.assertFalse(self._armed(),
                                 "a %s ledger read with no marker suppressed the "
                                 "fallback anyway, which is the ladder being "
                                 "disabled by a problem it has nothing to do "
                                 "with:\n%s" % (error, out))

    def test_an_unreadable_ledger_value_fails_open_too(self):
        code, out = self._run(kv_body="this is not json at all",
                              marker_obj=marker())
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(), out)

    def test_an_absent_secret_fails_open_without_making_a_request(self):
        code, out = self._run(marker_obj=marker(),
                              env_overrides={"CF_LEDGER_TOKEN": ""})
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(), out)
        self.assertEqual([], self.requests,
                         "the step tried to reach Cloudflare without a token")
        self._forget()
        code, out = self._run(marker_obj=marker(delivered=False),
                              env_overrides={"CF_ACCOUNT_ID": ""})
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(), out)

    def test_an_absent_namespace_id_fails_open_too(self):
        # The variable is the ONLY source of the id. Unset, no request is made
        # -- and the committed config sitting on disk must not rescue it.
        code, out = self._run(marker_obj=marker(),
                              env_overrides={"CF_KV_NAMESPACE_ID": ""})
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(), out)
        self.assertEqual([], self.requests)

    def test_the_committed_placeholder_id_is_rejected_loudly(self):
        """deploy/wrangler.toml's id is published and names no namespace.

        The step is handed a config carrying it AND the placeholder through the
        variable, and must refuse both without a request -- querying it is what
        produced Cloudflare 10013 and a silent 404.
        """
        code, out = self._run(marker_obj=marker(),
                              env_overrides={"CF_KV_NAMESPACE_ID": KV_PLACEHOLDER})
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(), out)
        self.assertEqual([], self.requests, out)
        self.assertIn("PLACEHOLDER", out)

    def test_an_empty_ledger_leaves_the_step_behaviour_identical(self):
        """U4 alone is inert. This is what "the current state" has to mean."""
        cases = [
            ("empty ledger, file delivers", ledger_raw(), marker(), True),
            ("empty ledger, file silent", ledger_raw(), marker(delivered=False),
             False),
            ("bare object", "{}", marker(), True),
            ("no days", '{"schema_version": 1, "days": {}}', marker(), True),
            ("another date's entry",
             json.dumps({"schema_version": 1, "as_of": "2026-10-09T01:35:12Z",
                         "days": {"2026-10-08": {SLOT: entry()}}}), marker(), True),
            ("another slot's entry",
             json.dumps({"schema_version": 1, "as_of": "2026-10-09T01:35:12Z",
                         "days": {TODAY: {"evening": entry()}}}), marker(), True),
            ("an empty value", "null", marker(), True),
        ]
        for name, kv_body, mark, expect_arm in cases:
            with self.subTest(case=name):
                self._forget()
                code, out = self._run(kv_body=kv_body, marker_obj=mark)
                self.assertEqual(0, code, out)
                self.assertEqual(expect_arm, self._armed(), out)

    def test_a_dry_run_is_never_suppressed_by_a_delivery_record(self):
        """The ledger's `arm` must not cancel drift-canary.yml's own run.

        The canary dispatches a dry run for the SAME slot twice a day (06:15 and
        09:02 NPT). Once the Worker delivers at 07:20 the ledger records the
        slot, so at 09:02 the check would arm and the canary's end-to-end run
        would finish green having done nothing — an end-to-end check retired
        without a failure. The marker already had this term; the ledger needs
        it for the opposite reason.
        """
        for source in ("ledger", "marker"):
            with self.subTest(recorded_by=source):
                self._forget()
                code, out = self._run(
                    kv_body=ledger_raw(entry()),
                    marker_obj=marker() if source == "marker" else marker(delivered=False),
                    env_overrides={"DRY_RUN": "true"})
                self.assertEqual(0, code, out)
                self.assertFalse(self._armed(),
                                 "a dry run was suppressed by the %s's delivery "
                                 "record; drift-canary.yml's end-to-end check "
                                 "would silently stop running.\n%s"
                                 % (source, out))
        # And the control: with no dry run, the very same ledger DOES arm.
        self._forget()
        code, out = self._run(kv_body=ledger_raw(entry()),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "the ledger no longer arms a real run, so the dry-run "
                        "guard above is vacuous:\n%s" % out)

    DRY_RUN_TERM_LOST = ("(armed_by_file or verdict == ARM) and not dry_run",
                          "(armed_by_file or verdict == ARM)")

    def test_dropping_the_dry_run_term_would_silence_the_canary(self):
        """MUTATION PROOF for the term above, which is only load-bearing if
        removing it is visible."""
        old, new = self.DRY_RUN_TERM_LOST

        def mutate(body):
            return body.replace(old, new), body.count(old)

        self._forget()
        code, out = self._run(kv_body=ledger_raw(entry()),
                              marker_obj=marker(delivered=False),
                              env_overrides={"DRY_RUN": "true"},
                              mutate=mutate)
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "with the dry-run term removed, the dry run did NOT "
                        "arm, so the term is not what keeps drift-canary.yml "
                        "running and this proof is decoration.\n%s" % out)

    # -- 3b. a prebuild is not a dry run, and neither may be stood down -----

    def test_a_prebuild_is_never_suppressed_by_a_delivery_record(self):
        """The payload the seal job reads is written by the step this skips.

        A prebuild is NOT a dry run. It publishes the page for real, so DRY_RUN
        stays FALSE; NTFY_DRY_RUN carries the payload-only signal instead. Read
        through DRY_RUN alone a prebuild therefore looks exactly like an ordinary
        delivered run and gets armed down -- and the ntfy step, whose dry-run
        branch is the ONLY writer of the payload digest-prebuild.yml's seal
        reads, never runs. The artifact is never sealed, the owner never gets the
        full digest, and every job stays green.
        """
        for label, overrides in (
            ("the real prebuild shape",
             {"PHASE": "prebuild", "NTFY_DRY_RUN": "true"}),
            ("NTFY_DRY_RUN with no phase at all", {"NTFY_DRY_RUN": "true"}),
        ):
            with self.subTest(case=label):
                self._forget()
                code, out = self._run(kv_body=ledger_raw(entry()),
                                      marker_obj=marker(delivered=False),
                                      env_overrides=overrides)
                self.assertEqual(0, code, out)
                self.assertFalse(self._armed(),
                                 "%s was stood down by a delivery record, so "
                                 "the seal job never receives a payload to "
                                 "seal.\n%s" % (label, out))

    def test_the_prebuild_guard_does_not_suppress_an_ordinary_run(self):
        """The control. Without it a term that suppressed NOTHING would pass the
        two cases above, and they would prove nothing.
        """
        self._forget()
        code, out = self._run(kv_body=ledger_raw(entry()),
                              marker_obj=marker(delivered=False),
                              env_overrides={"PHASE": "full",
                                             "NTFY_DRY_RUN": "false"})
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "an ordinary full run is no longer armed down by a "
                        "delivered slot, so the prebuild guard proves "
                        "nothing:\n%s" % out)

    PREBUILD_TERM_LOST = (" and not dry_run and not prebuild and not ntfy_dry_run",
                          " and not dry_run")

    def test_dropping_the_prebuild_term_would_starve_the_seal(self):
        """MUTATION PROOF for the two terms above, which only mean something if
        removing them brings the defect back.
        """
        old, new = self.PREBUILD_TERM_LOST

        def mutate(body):
            return body.replace(old, new), body.count(old)

        self._forget()
        code, out = self._run(kv_body=ledger_raw(entry()),
                              marker_obj=marker(delivered=False),
                              env_overrides={"PHASE": "prebuild",
                                             "NTFY_DRY_RUN": "true"},
                              mutate=mutate)
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "with the prebuild terms removed a prebuild was NOT "
                        "armed down, so those terms are not what keeps the seal "
                        "fed and this proof is decoration.\n%s" % out)

    def test_the_unavailable_kind_is_the_same_literal_everywhere(self):
        """THREE COPIES, ONE KIND, PINNED RATHER THAN HOPED FOR.

        `unavailable` is a kind in this step's arm rule, in digest-prebuild.yml's
        SUPERSEDED_KINDS, and in tests/digest/test_ladder_trigger.py. A heredoc
        cannot import from another workflow, so a rename in one place would
        quietly split one kind into two: the ladder would stand itself down for a
        push the seal calls degraded, or the reverse, and no test would notice.
        """
        step_body = first_heredoc(STEP)
        step_match = re.search(r"KIND_UNAVAILABLE = '([^']+)'", step_body)
        self.assertIsNotNone(step_match,
                             "the no-op check no longer defines KIND_UNAVAILABLE")
        step_kind = step_match.group(1)

        seal = (W.REPO_ROOT / ".github" / "workflows" /
                "digest-prebuild.yml").read_text(encoding="utf-8")
        seal_match = re.search(r"^ *SUPERSEDED_KINDS = \((.*)\)$", seal, re.M)
        self.assertIsNotNone(seal_match,
                             "the seal no longer declares SUPERSEDED_KINDS")
        seal_kinds = tuple(re.findall(r"'([^']+)'", seal_match.group(1)))

        ladder = (W.REPO_ROOT / "tests" / "digest" /
                  "test_ladder_trigger.py").read_text(encoding="utf-8")
        ladder_match = re.search(r'^SUPERSEDED_KINDS = \((.*)\)$', ladder, re.M)
        self.assertIsNotNone(ladder_match,
                             "the ladder test no longer declares SUPERSEDED_KINDS")
        ladder_kinds = tuple(re.findall(r'"([^"]+)"', ladder_match.group(1)))

        self.assertIn(step_kind, seal_kinds,
                      "this step treats %r as a kind the owner cannot read, but "
                      "the seal does not list it in SUPERSEDED_KINDS." % step_kind)
        self.assertIn(step_kind, ladder_kinds,
                      "this step treats %r as a kind the owner cannot read, but "
                      "the ladder test does not." % step_kind)
        self.assertEqual(set(seal_kinds), set(ladder_kinds),
                         "the seal and the ladder test disagree about which "
                         "kinds are degraded: %r vs %r"
                         % (seal_kinds, ladder_kinds))

    # -- 3c. a stamp is not a send ------------------------------------------

    def test_an_instant_that_predates_the_runs_own_pending_write_is_not_a_send(self):
        """The second half of the same record: 12:34:19Z against pending 13:55:26Z.

        `pending_at` is written BEFORE the first byte goes to ntfy, so a send
        stamped earlier than it cannot be this run's send. It also cannot be
        the slot's, since the slot was not due yet. Either comparison alone
        catches 2026-10-26; both are asserted here so removing one still leaves
        the other, and a regression in one is visible on its own.
        """
        impossible = entry(state="sent",
                           ntfy_sent_at="2026-10-09T00:34:19Z",
                           first_ntfy_sent_at=None,
                           pending_at="2026-10-09T01:55:26Z",
                           scheduled_instant="2026-10-09T01:35:00Z",
                           http_status=None)
        code, out = self._run(kv_body=ledger_raw(impossible),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertFalse(
            self._armed(),
            "an ntfy_sent_at earlier than the run's own pending_at (and "
            "earlier than the slot was even due) stood the ladder down. That "
            "is a stale instant from an earlier attempt being read as this "
            "slot's delivery:\n%s" % out)

    def test_a_stamp_before_the_slots_scheduled_instant_does_not_arm(self):
        """Same gate, from `scheduled_instant` alone — no `pending_at` needed.

        The instant is the one fact the Worker and this step can both read, so
        the check must not depend on the write-ahead record being present.
        """
        impossible = entry(ntfy_sent_at="2026-10-09T01:20:00Z",
                           first_ntfy_sent_at=None,
                           scheduled_instant="2026-10-09T01:35:00Z")
        code, out = self._run(kv_body=ledger_raw(impossible),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(),
                         "a send 15 minutes before the slot was due stood the "
                         "ladder down:\n%s" % out)

    def test_an_unreadable_instant_is_not_evidence_of_a_send(self):
        """A stamp nothing can parse is not a delivery, and must not be one."""
        broken = entry(ntfy_sent_at="last night", first_ntfy_sent_at=None)
        code, out = self._run(kv_body=ledger_raw(broken),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertFalse(self._armed(),
                         "the string 'last night' armed the fallback:\n%s" % out)

    def test_a_real_send_inside_the_early_slack_still_arms(self):
        """The gate must not overshoot: a cron firing 30s early is normal.

        Without this, "no instant arms" would pass every test above and break
        the one behaviour the step exists for — not re-sending a slot that
        really did go out.
        """
        early = entry(ntfy_sent_at="2026-10-09T01:34:30Z",
                      first_ntfy_sent_at="2026-10-09T01:34:30Z",
                      pending_at="2026-10-09T01:34:02Z",
                      scheduled_instant="2026-10-09T01:35:00Z")
        code, out = self._run(kv_body=ledger_raw(early),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "a send 30s before its scheduled instant — inside the "
                        "60s slack this repository already allows a cron — was "
                        "rejected, so the owner would be sent the digest "
                        "twice:\n%s" % out)

    # -- 4. the file rule is untouched --------------------------------------

    def test_every_term_of_the_original_file_rule_still_stands(self):
        cases = [
            ("zero stories", marker(stories=0), False),
            ("a dry run", marker(dry_run=True), False),
            ("delivered=false", marker(delivered=False), False),
            ("another slot", marker(time="evening"), False),
            ("another day", marker(date="2026-10-08"), False),
            ("the legacy per-slot form",
             {"morning": TODAY, "stories": 12, "delivered": True}, True),
            ("a legacy marker with no delivered field",
             {"date": TODAY, "time": SLOT, "stories": 12}, True),
        ]
        for name, mark, expect_arm in cases:
            with self.subTest(case=name):
                self._forget()
                code, out = self._run(kv_body="{}", marker_obj=mark)
                self.assertEqual(0, code, out)
                self.assertEqual(expect_arm, self._armed(), out)

    def test_a_corrupt_marker_is_reset_and_the_ledger_still_decides(self):
        (self.tmp / "state" / "last_digest_sent.json").write_text(
            "{not json", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(entry()))
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "a corrupt marker suppressed a slot the ledger says was "
                        "already sent:\n%s" % out)
        self.assertEqual({},
                         json.loads((self.tmp / "state" /
                                     "last_digest_sent.json").read_text(
                                         encoding="utf-8")),
                         "the corrupt marker was not reset, so every later run "
                         "re-reads it")

    # -- 5. MUTATION PROOFS --------------------------------------------------

    #: Narrowing the ledger read's except clause is the smallest edit that turns
    #: a fail-open into a fail-hard: a 403 from a token without the storage
    #: scope, or a dropped socket, would then propagate out of the step and kill
    #: the digest job instead of falling back to the file rule.
    #:
    #: Four leading spaces, because the target is the PARSED heredoc body --
    #: YAML's block scalar has already removed the step's indentation by the
    #: time these bytes exist, and a mutation written against the file's
    #: indentation would silently match nothing.
    FAIL_OPEN_LOST = (
        "\n    except Exception as exc:\n",
        "\n    except ValueError as exc:\n",
    )

    #: The two ledger facts that must outrank `ntfy_sent_at`, each with the
    #: ledger entry that triggers it. Mutated ONE AT A TIME below: a single
    #: `return NO_ARM -> return ARM` string replacement would hit both branches
    #: and prove nothing about either.
    NO_ARM_TERMS = (
        ("ntfy_failed",
         "\n    if entry.get('ntfy_failed'):\n        return NO_ARM",
         lambda: entry(state="failed", ntfy_failed=True, ntfy_sent_at=None,
                       first_ntfy_sent_at=None)),
        ("kind unavailable",
         "\n    if str(entry.get('kind') or '') == KIND_UNAVAILABLE:\n        return NO_ARM",
         lambda: entry(kind="unavailable")),
    )

    #: The false-green gate, as the exact block it is. Mutating it back to the
    #: pre-2026-10-27 shape ("any ntfy_sent_at is a send") must re-create the
    #: defect: the ladder stands down on a stale instant.
    INSTANT_GATE_DROPPED = (
        "\n    if entry.get('ntfy_sent_at'):\n"
        "        if not send_instant_fits_slot(entry, entry.get('ntfy_sent_at')):\n"
        "            return NO_ARM\n"
        "        return ARM",
        "\n    if entry.get('ntfy_sent_at'):\n        return ARM",
    )

    def test_removing_the_fail_open_handler_would_break_the_ladder(self):
        """A check never seen red is decoration."""
        old, new = self.FAIL_OPEN_LOST

        def mutate(body):
            return body.replace(old, new), body.count(old)

        code, out = self._run(kv_error="http", marker_obj=marker(),
                              mutate=mutate)
        self.assertNotEqual(0, code,
                            "narrowing the ledger read's except clause did NOT "
                            "break the step. The fail-open branch is therefore "
                            "not what is keeping this job alive through a KV "
                            "outage, and the guard is decoration.\n%s" % out)

    def test_losing_a_no_arm_term_would_silence_the_ladder(self):
        """The other direction: if a `no_arm` term ever degrades to `arm`, the
        backups are silenced on exactly the days they are needed."""
        for name, target, fixture in self.NO_ARM_TERMS:
            with self.subTest(term=name):
                def mutate(body, t=target):
                    return body.replace(t, t.replace("NO_ARM", "ARM")), body.count(t)

                self._forget()
                code, out = self._run(kv_body=ledger_raw(fixture()),
                                      marker_obj=marker(), mutate=mutate)
                self.assertEqual(0, code, out)
                # The mutation is EXPECTED to arm the fallback: that arming IS
                # the defect the real term prevents. If it ever stopped arming
                # the mutation would no longer demonstrate anything, so that is
                # an assertion failure here too, not a pass.
                self.assertTrue(self._armed(),
                                "with the `%s` term mutated to `arm`, the "
                                "fallback did NOT arm. The mutation no longer "
                                "demonstrates the defect it was written for, so "
                                "this proof is decoration.\n%s" % (name, out))

    def test_dropping_the_instant_gate_would_recreate_the_false_green(self):
        """The 2026-10-27 gate, proved load-bearing.

        Restores the pre-fix shape — every truthy `ntfy_sent_at` is a send —
        and requires the impossible-instant case to arm again. If it does not,
        the gate is not what is blocking that verdict and the tests above are
        passing for some other reason.
        """
        old, new = self.INSTANT_GATE_DROPPED

        def mutate(body):
            return body.replace(old, new), body.count(old)

        impossible = entry(ntfy_sent_at="2026-10-09T00:34:19Z",
                           first_ntfy_sent_at=None,
                           pending_at="2026-10-09T01:55:26Z",
                           scheduled_instant="2026-10-09T01:35:00Z")
        code, out = self._run(kv_body=ledger_raw(impossible),
                              marker_obj=marker(delivered=False), mutate=mutate)
        self.assertEqual(0, code, out)
        self.assertTrue(self._armed(),
                        "with the instant gate removed, the stale instant did "
                        "NOT arm — so the gate is not what stops it, and the "
                        "proof above is decoration:\n%s" % out)

    # -- 6. the step still obeys the shell and YAML rules --------------------

    def test_the_step_body_compiles_and_its_heredoc_is_terminated(self):
        body = first_heredoc(STEP)
        compile(body, "<fallback>", "exec")
        run = step_dict(STEP)["run"]
        self.assertTrue(run.rstrip().endswith("PY"),
                        "the step's block scalar does not end on its heredoc "
                        "terminator, so bash never saw a column-0 PY:\n%s"
                        % run[-200:])

    def test_the_ledger_key_is_the_workers_own_constant(self):
        """One key, one place to correct.

        The step cannot import the Worker's constant, so the literal is pinned
        here instead of trusted.
        """
        worker = (W.REPO_ROOT / "deploy" / "cloudflare-worker.js").read_text(
            encoding="utf-8", errors="replace")
        match = re.search(
            r"const\s+DIGEST_LEDGER_KEY\s*=\s*\"([^\"]+)\"\s*;", worker)
        self.assertIsNotNone(match,
                             "the Worker no longer declares DIGEST_LEDGER_KEY "
                             "as a plain constant")
        self.assertEqual(LEDGER_KEY, match.group(1),
                         "the fallback step and the Worker disagree about the "
                         "ledger key, so the fallback reads an absent key and "
                         "never arms")
        self.assertIn(LEDGER_KEY, first_heredoc(STEP),
                      "the fallback step no longer names the ledger key at "
                      "all; if the key moved, this suite must be told")

    def test_no_secret_value_can_reach_the_step_log(self):
        code, out = self._run(kv_body=ledger_raw(entry()),
                              marker_obj=marker(delivered=False))
        self.assertEqual(0, code, out)
        self.assertNotIn(TOKEN, out,
                         "the KV token reached the step log; it travels in an "
                         "Authorization header, and an exception string can "
                         "quote a request:\n%s" % out)


if __name__ == "__main__":
    unittest.main(verbosity=2)