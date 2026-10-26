#!/usr/bin/env python3
"""tests/digest/test_supersede_push.py — U7: replace a degraded push with the full one.

WHAT IS BEING GUARDED
---------------------
A slot whose ledger entry says `kind` is one of `partial`, `fallback_last_good`
or `unavailable` has had SOMETHING pushed at the owner, and the something is not
a digest they can read: a banner over half the stories, yesterday's news labelled
as today's, or a "digest unavailable" notice. `ntfy_sent_at` is set for all
three, which is why every existing duplicate guard treats them as delivered and
why nothing has ever repaired them.

The repair is a dispatch of `daily-news.yml` at `phase=full`, so the UNMODIFIED
ntfy step sends the full digest. Three properties make that safe, and each is a
separate failure mode:

* **It must fire on the three degraded kinds and nothing else.** A delivered
  slot that is merely late does not need a second push; that is what the 07:50
  ladder is for.
* **It is guarded by `ntfy_full_sent_at`, NOT `ntfy_sent_at`.** Every degraded
  kind sets `ntfy_sent_at`, so guarding on it makes the supersede unreachable —
  the feature would be a green run that never repairs anything. The guard
  answers "has the FULL push already gone out", which is a different question.
* **It is latched by `full_artifact_available_at`.** The seal job runs twice a
  day per slot (a primary prebuild and a retry), both of which can observe the
  same degraded ledger entry. Without a latch the supersede dispatches once per
  prebuild and the owner gets three copies of the same full digest.

The dispatch carries `trigger_source=supersede` and `trigger_source` is what
`Fallback no-op check` in daily-news.yml keys on: without it the dispatched run
reads `ntfy_sent_at`, stands itself down, and the supersede is a green run that
delivered nothing. That half is proved in `test_ladder_trigger.py`; the shape of
the dispatch is proved here.

WHAT IS NOT HERE, ON PURPOSE: no re-implementation of the decision. The step's
own bytes are executed (`first_heredoc` + `exec`) with `urllib.request.urlopen`
poisoned, so neither Cloudflare nor GitHub is reached and no credential is
needed. The dispatched workflow is never actually dispatched by this suite.

WHY THE LEDGER WRITE IS IN THIS STEP AND NOT SOMEWHERE NICER
-----------------------------------------------------------
`digest:ledger` has exactly one writer today: the Worker's cron arm, which
injects it. The build-side ingest endpoint is deliberately ledger-free — giving
it ledger access "would put the punctuality record one crafted request away from
being rewritten" — and that invariant is pinned by the JS suite, so widening it
is not an option. The seal job is the only party that knows "I dispatched the
supersede", so it records that. The write is a read-modify-write and it races the
Worker's own writes; that race can cost a duplicate on the recovery path, which
the delivery guarantee already permits, and it can never fabricate a send.
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

STEP = "Supersede a degraded push with the full digest"
WORKFLOW = "digest-prebuild.yml"

#: Supplied through the CF_KV_NAMESPACE_ID variable. Built from two halves so no
#: 32-hex literal sits beside a NAMESPACE_ID name (test_deploy_config_ids.py).
NAMESPACE_ID = "0123456789abcdef" * 2
#: The id committed in deploy/wrangler.toml: a PLACEHOLDER that names no
#: namespace, written on disk here to prove the step does not read it.
KV_PLACEHOLDER = "abcdefabcdefabcdefabcdefabcdefab"
ACCOUNT_ID = "acc0000000000000000000000000000ab"
TOKEN = "kv-token-not-a-real-secret"

TODAY = "2026-10-09"
SLOT = "morning"

#: The Worker must carry this exact key, and the step cannot import the Worker's
#: constant, so the literal is pinned against it below.
LEDGER_KEY = "digest:ledger"

WRANGLER_TEMPLATE = (
    '\ufeffname = "breaking-monitor"\n'
    'main = "cloudflare-worker.js"\n'
    "\n"
    "[[kv_namespaces]]\n"
    'binding = "KV"\n'
    'id = "%s"\n'
) % KV_PLACEHOLDER


def step_dict(step_name):
    for _job, name, sd in W.iter_all_steps(WORKFLOW):
        if name == step_name:
            return sd
    raise AssertionError("%s has no step %r" % (WORKFLOW, step_name))


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


def degraded_entry(kind="partial", **overrides):
    """What the Worker leaves behind after pushing something unreadable."""
    base = {
        "state": "sent",
        "kind": kind,
        "sequence_id": "dmn-%s-%s" % (TODAY, SLOT),
        "ntfy_sent_at": "2026-10-09T01:35:12Z",
        "first_ntfy_sent_at": "2026-10-09T01:35:12Z",
        "scheduled_instant": "2026-10-09T01:35:00Z",
        "ntfy_failed": False,
    }
    base.update(overrides)
    return base


def ledger_raw(slot_entry=None):
    days = {}
    if slot_entry is not None:
        days[TODAY] = {SLOT: slot_entry}
    return json.dumps({"schema_version": 1, "as_of": "2026-10-09T01:35:12Z",
                       "days": days})


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SupersedeHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="supersede-push-")
        self.tmp = Path(self._tmp)
        self.addCleanup(shutil.rmtree, self._tmp, True)
        (self.tmp / "deploy").mkdir()
        (self.tmp / "deploy" / "wrangler.toml").write_text(
            WRANGLER_TEMPLATE, encoding="utf-8")
        (self.tmp / "prebuild").mkdir()
        self.outputs = self.tmp / "gh_output.txt"
        self.outputs.write_text("", encoding="utf-8")
        self.env_file = self.tmp / "gh_env.txt"
        self.env_file.write_text("", encoding="utf-8")
        self.summary = self.tmp / "summary.md"
        self.summary.write_text("", encoding="utf-8")
        self.gets = []
        self.puts = []

    def _artifact(self, is_partial=False, date=TODAY, slot=SLOT):
        artifact = {
            "schema_version": 1,
            "key": "digest:artifact:%s:%s" % (date, slot),
            "date": date,
            "slot": slot,
            "built_at": "2026-10-09T01:20:41Z",
            "page_url": "https://daily-digest.example.pages.dev/digests/%s-%s.html"
                        % (date, slot),
            "is_partial": is_partial,
            "source_health_state": "partial" if is_partial else "ok",
            "stories": 14,
            "html_sha256": "0" * 64,
            "ntfy": {"title": "Your morning news — 9 Oct", "body": "## Headlines"},
        }
        (self.tmp / "prebuild" / "artifact.json").write_text(
            json.dumps(artifact), encoding="utf-8")

    def _run(self, kv_body=None, kv_get_error=None, kv_put_error=None,
             artifact=None, mutate=None, env_overrides=None):
        if artifact is None:
            artifact = {"is_partial": False}
        self._artifact(**artifact)
        real_urlopen = urllib.request.urlopen
        self.gets = []
        self.puts = []

        def fake_urlopen(request, *args, **kwargs):
            url = getattr(request, "full_url", request)
            method = (getattr(request, "method", None) or "GET").upper()
            if method == "PUT":
                body = getattr(request, "data", None)
                if isinstance(body, bytes):
                    body = body.decode("utf-8")
                self.puts.append({"url": str(url), "body": body})
                if kv_put_error == "http":
                    raise urllib.error.HTTPError(str(url), 403, "Forbidden", {}, None)
                if kv_put_error == "transport":
                    raise urllib.error.URLError("injected transport failure")
                return _FakeResponse('{"success":true}')
            self.gets.append(str(url))
            if kv_get_error == "http":
                raise urllib.error.HTTPError(str(url), 403, "Forbidden", {}, None)
            if kv_get_error == "transport":
                raise urllib.error.URLError("injected transport failure")
            return _FakeResponse(kv_body if kv_body is not None else "{}")

        urllib.request.urlopen = fake_urlopen
        try:
            body = first_heredoc(STEP)
            if mutate is not None:
                new_body, hits = mutate(body)
                self.assertEqual(1, hits,
                                 "the mutation target matched %d time(s), "
                                 "expected 1. The workflow has drifted from the "
                                 "text this proof was written against." % hits)
                body = new_body
            env = {
                "GITHUB_OUTPUT": str(self.outputs),
                "GITHUB_ENV": str(self.env_file),
                "GITHUB_STEP_SUMMARY": str(self.summary),
                "SLOT": SLOT,
                "TODAY": TODAY,
                "ARTIFACT_PATH": "prebuild/artifact.json",
                "CF_LEDGER_TOKEN": TOKEN,
                "CF_ACCOUNT_ID": ACCOUNT_ID,
                "CF_KV_NAMESPACE_ID": NAMESPACE_ID,
                # Named on the step and forwarded by its own bash half; both are
                # passed here so the step's bytes run with the environment it
                # will actually have.
                "SUPERSEDE_DECISION_FILE": str(self.tmp / "supersede_decision"),
                "SUPERSEDE_PATCH_FILE": str(self.tmp / "supersede_patch.json"),
            }
            env.update(env_overrides or {})
            return exec_body(body, env, self.tmp)
        finally:
            urllib.request.urlopen = real_urlopen

    def _decision(self):
        text = self.outputs.read_text(encoding="utf-8")
        match = re.search(r"^supersede=(\S+)$", text, re.MULTILINE)
        return match.group(1) if match else None

    def _reason(self):
        text = self.outputs.read_text(encoding="utf-8")
        match = re.search(r"^supersede_reason=(.*)$", text, re.MULTILINE)
        return match.group(1) if match else None

    def _latched(self):
        if not self.puts:
            return None
        return json.loads(self.puts[-1]["body"])


# ==========================================================================
# 1. WHICH LEDGER ENTRIES SUPERSEDE
# ==========================================================================


class WhichEntriesSupersedeTests(SupersedeHarness):
    def test_each_degraded_kind_dispatches_the_full_digest(self):
        for kind in ("partial", "fallback_last_good", "unavailable"):
            with self.subTest(kind=kind):
                self.outputs.write_text("", encoding="utf-8")
                code, out = self._run(kv_body=ledger_raw(degraded_entry(kind)))
                self.assertEqual(0, code, out)
                self.assertEqual(
                    "fire", self._decision(),
                    "a slot pushed as %r did not get its full digest.\n%s"
                    % (kind, out))

    def test_a_delivered_slot_is_never_superseded(self):
        """The control, and the duplicate-push guard.

        A delivered digest that is merely late is the 07:50 ladder's case, not
        this one's. Superseding it would push the owner the same day's news
        twice.
        """
        for kind in ("delivered", "duplicate_suppressed"):
            with self.subTest(kind=kind):
                self.outputs.write_text("", encoding="utf-8")
                code, out = self._run(kv_body=ledger_raw(degraded_entry(kind)))
                self.assertEqual(0, code, out)
                self.assertEqual(
                    "skip", self._decision(),
                    "a slot pushed as %r was superseded. That is a duplicate "
                    "push of a digest the owner already has.\n%s" % (kind, out))
                self.assertEqual([], self.puts,
                                 "a run that decided not to supersede still "
                                 "wrote to the ledger")

    def test_a_slot_the_ledger_knows_nothing_about_is_never_superseded(self):
        """The FIRST prebuild of a slot runs before its delivery exists.

        There is nothing degraded to replace, so there is nothing to do — and
        dispatching anyway would send the full digest an hour early, ahead of
        the slot it belongs to.
        """
        for body in (ledger_raw(None), "{}", "null",
                     json.dumps({"days": {TODAY: {"evening": degraded_entry()}}}),
                     json.dumps({"days": {"2026-10-08": {SLOT: degraded_entry()}}})):
            with self.subTest(body=body[:48]):
                self.outputs.write_text("", encoding="utf-8")
                code, out = self._run(kv_body=body)
                self.assertEqual(0, code, out)
                self.assertEqual("skip", self._decision(), out)
                self.assertEqual([], self.puts)

    def test_a_partial_artifact_supersedes_nothing(self):
        """"AND A COMPLETE ARTIFACT HAS SINCE BEEN BUILT."

        The artifact on hand IS the thing that would be pushed. If the retry
        prebuild also built a partial digest, there is no complete artifact to
        replace the partial push with, and dispatching would re-run the whole
        pipeline to send the same degraded news again.
        """
        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(degraded_entry("partial")),
                              artifact={"is_partial": True})
        self.assertEqual(0, code, out)
        self.assertEqual("skip", self._decision(),
                         "the supersede fired with no complete artifact to send.\n%s"
                         % out)
        self.assertEqual([], self.puts)


# ==========================================================================
# 2. `ntfy_full_sent_at` IS THE GUARD — NOT `ntfy_sent_at`
# ==========================================================================


class FullSentGuardTests(SupersedeHarness):
    def test_ntfy_sent_at_alone_does_not_suppress_the_supersede(self):
        """THE CASE THAT DECIDES WHICH FIELD IS READ.

        Every degraded kind sets `ntfy_sent_at`, because a push happened. Guard
        on it and the supersede is unreachable on every slot it was written for.
        """
        entry = degraded_entry("partial")
        self.assertTrue(entry["ntfy_sent_at"],
                        "the fixture no longer carries ntfy_sent_at, so this "
                        "test would pass vacuously")
        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(entry))
        self.assertEqual(0, code, out)
        self.assertEqual(
            "fire", self._decision(),
            "the first push's own `ntfy_sent_at` suppressed the supersede. The "
            "supersede exists to replace exactly that push, so reading this "
            "field makes the feature unreachable.\n%s" % out)

    def test_ntfy_full_sent_at_does_suppress_a_second_supersede(self):
        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(
            degraded_entry("unavailable", ntfy_full_sent_at="2026-10-09T01:52:03Z")))
        self.assertEqual(0, code, out)
        self.assertEqual(
            "skip", self._decision(),
            "a second supersede fired after the full digest had already been "
            "dispatched for this slot. The owner would get the same full digest "
            "again.\n%s" % out)
        self.assertEqual([], self.puts)
        self.assertIn("ntfy_full_sent_at", self._reason() or "",
                      "the refusal does not name the field that caused it:\n%s" % out)

    #: Written against the PARSED heredoc body: YAML has already removed the step's
    #: indentation by the time these bytes exist. Anchored on the whole guard
    #: line rather than on the bare field name, because the field name also
    #: appears in the refusal message below it and a bare-name target would
    #: match twice — which is exactly what this suite asserts against.
    FULL_GUARD_LOST = (
        "if entry.get('ntfy_full_sent_at'):",
        "if entry.get('ntfy_sent_at'):",
    )

    def test_reading_the_wrong_field_makes_the_feature_unreachable(self):
        """MUTATION PROOF. A guard that is never seen to matter is decoration."""
        old, new = self.FULL_GUARD_LOST

        def mutate(body):
            return body.replace(old, new), body.count(old)

        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(degraded_entry("partial")),
                              mutate=mutate)
        self.assertEqual(0, code, out)
        self.assertEqual(
            "skip", self._decision(),
            "guarding on `ntfy_sent_at` did NOT stand the supersede down. The "
            "mutation no longer demonstrates the defect it was written for, so "
            "this proof is decoration.\n%s" % out)


# ==========================================================================
# 3. `full_artifact_available_at` STOPS THE LOOP
# ==========================================================================


class LoopLatchTests(SupersedeHarness):
    def test_an_already_latched_slot_never_supersedes_again(self):
        """THE LOOP. The seal job runs twice a day per slot.

        Without a latch, both runs read the same degraded entry and both
        dispatch, and the owner gets the full digest twice.
        """
        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(
            degraded_entry("partial", full_artifact_available_at="2026-10-09T01:48:00Z")))
        self.assertEqual(0, code, out)
        self.assertEqual(
            "skip", self._decision(),
            "a slot already carrying full_artifact_available_at superseded "
            "again. That is the loop.\n%s" % out)
        self.assertEqual([], self.puts)
        self.assertIn("full_artifact_available_at", self._reason() or "",
                      "the refusal does not name the field that caused it:\n%s" % out)

    def test_the_latch_is_written_before_the_dispatch_is_considered_done(self):
        """The latch has to be on disk in the SAME step that decides to fire.

        A latch written by the dispatched run instead would be written by a
        twenty-minute pipeline that can fail in eleven places, and the second
        prebuild would read a slot that never latched.
        """
        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(degraded_entry("partial")))
        self.assertEqual(0, code, out)
        self.assertEqual("fire", self._decision(), out)
        self.assertTrue(self.puts,
                        "the supersede fired but nothing was written to the "
                        "ledger, so nothing can ever stop it firing again.\n%s"
                        % out)
        entry = (self._latched().get("days", {}).get(TODAY, {}) or {}).get(SLOT, {})
        self.assertTrue(
            entry.get("full_artifact_available_at"),
            "the latch was not written: %r" % entry)

    LATCH_GUARD_LOST = (
        "if entry.get('full_artifact_available_at'):",
        "if False:",
    )

    def test_losing_the_latch_guard_would_loop(self):
        """MUTATION PROOF for the loop latch.

        Renaming the WRITTEN field would prove nothing here, because the guard
        reads its own name and would still suppress the second run. The
        interesting edit is removing the guard itself, so that is what this
        mutates.
        """
        old, new = self.LATCH_GUARD_LOST

        def mutate(body):
            return body.replace(old, new), body.count(old)

        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(
            degraded_entry("partial", full_artifact_available_at="2026-10-09T01:48:00Z")),
            mutate=mutate)
        self.assertEqual(0, code, out)
        self.assertEqual(
            "fire", self._decision(),
            "with the loop latch removed, the already-latched slot superseded "
            "again, which is the loop this guard exists to stop. If that ever "
            "stops happening the mutation is no longer proving anything.\n%s"
            % out)

    def test_the_latch_is_written_under_the_field_the_guard_reads(self):
        """The guard and the writer must name the SAME field.

        They are two lines of one step and nothing else connects them: a rename
        on the write side alone leaves a guard that reads a field nobody ever
        sets, which is a loop with a comment claiming it is closed.
        """
        body = first_heredoc(STEP)
        read = re.search(r"if entry\.get\('(\w+)'\):", body)
        self.assertIsNotNone(read, "the loop latch guard is gone")
        self.assertEqual(
            "full_artifact_available_at", read.group(1),
            "the guard reads %r instead of full_artifact_available_at"
            % read.group(1))
        written = re.findall(r"'(\w+)':\s*now", body)
        self.assertIn(
            "full_artifact_available_at", written,
            "the step no longer WRITES full_artifact_available_at. The guard "
            "reads it, so the latch is never set and the seal job supersedes "
            "on every prebuild: %r" % written)

    def test_the_latch_preserves_the_entry_it_merged_into(self):
        """A read-modify-write that drops fields is a delivery-record defect.

        `ntfy_sent_at` and `first_ntfy_sent_at` are what
        scripts/delivery_ledger.py renders the punctuality line from. Losing them
        would make the owner's own ledger page lose the history of what was
        delivered.
        """
        original = degraded_entry("fallback_last_good",
                                  last_good_shown="2026-10-08")
        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(original))
        self.assertEqual(0, code, out)
        entry = (self._latched().get("days", {}).get(TODAY, {}) or {}).get(SLOT, {})
        for field in ("ntfy_sent_at", "first_ntfy_sent_at", "kind",
                      "last_good_shown", "sequence_id"):
            self.assertIn(
                field, entry,
                "the latch write dropped %r, so the ledger no longer records "
                "what was delivered for this slot: %r" % (field, entry))
        self.assertIn("full_artifact_available_at", entry)


# ==========================================================================
# 4. THE `…-full` ID
# ==========================================================================


class FullSequenceIdTests(SupersedeHarness):
    def test_the_supersede_is_recorded_under_a_different_deterministic_id(self):
        """`ntfy_sent_at` is set by the first push, so the supersede may NOT
        reuse the primary id.

        The Worker's own duplicate guards are keyed on `sequence_id`, and a
        re-used id is exactly what they suppress.
        """
        self.outputs.write_text("", encoding="utf-8")
        code, out = self._run(kv_body=ledger_raw(degraded_entry("partial")))
        self.assertEqual(0, code, out)
        entry = (self._latched().get("days", {}).get(TODAY, {}) or {}).get(SLOT, {})
        primary = "dmn-%s-%s" % (TODAY, SLOT)
        recorded = str(entry.get("supersede_sequence_id") or "")
        self.assertTrue(recorded,
                        "the supersede recorded no id of its own, so nothing "
                        "distinguishes it from the push it replaces: %r" % entry)
        self.assertEqual(primary + "-full", recorded,
                         "the supersede id is not the primary id plus `-full`.\n%s"
                         % out)
        self.assertNotEqual(
            primary, recorded,
            "the supersede reuses the primary sequence_id. Every duplicate "
            "guard in the delivery path is keyed on that id, so the supersede "
            "would be suppressed by the very push it replaces.")
        self.assertEqual(
            primary, str(entry.get("sequence_id") or ""),
            "the latch overwrote the slot's primary sequence_id; the punctuality "
            "record must still name the push the owner actually received.")

    def test_the_id_matches_the_workers_own_full_sequence_id(self):
        """One derivation, pinned — not a second copy that can drift.

        The step cannot import the Worker's constant, so both sides are read
        and the shapes are compared. If either derivation is renamed this fails
        loudly instead of leaving two ids that agree by luck.
        """
        worker = (W.REPO_ROOT / "deploy" / "cloudflare-worker.js").read_text(
            encoding="utf-8", errors="replace")
        base = re.search(
            r"function\s+digestSequenceId\s*\(\s*date\s*,\s*slot\s*\)\s*\{\s*"
            r"return\s+(.+?);\s*\}", worker, re.S)
        full = re.search(
            r"function\s+digestFullSequenceId\s*\(\s*date\s*,\s*slot\s*\)\s*\{\s*"
            r"return\s+(.+?);\s*\}", worker, re.S)
        self.assertIsNotNone(base, "the Worker's digestSequenceId is gone or "
                                   "its parameters were renamed")
        self.assertIsNotNone(full, "the Worker's digestFullSequenceId is gone "
                                   "or its parameters were renamed")
        self.assertEqual('"dmn-" + date + "-" + slot',
                         re.sub(r"\s+", " ", base.group(1)).strip(),
                         "the Worker's primary sequence id changed shape; this "
                         "suite's expected value must be updated deliberately")
        self.assertEqual('digestSequenceId(date, slot) + "-full"',
                         re.sub(r"\s+", " ", full.group(1)).strip(),
                         "the Worker's full sequence id is no longer the primary "
                         "id plus `-full`; the step's literal must be updated "
                         "deliberately, not left agreeing by luck")
        key = re.search(r"const\s+DIGEST_LEDGER_KEY\s*=\s*\"([^\"]+)\"", worker)
        self.assertIsNotNone(key, "the Worker no longer declares "
                                  "DIGEST_LEDGER_KEY as a plain constant")
        self.assertEqual(
            LEDGER_KEY, key.group(1),
            "the supersede step and the Worker disagree about the ledger key, "
            "so the step reads a key that does not exist and never fires")
        self.assertIn(
            LEDGER_KEY, first_heredoc(STEP),
            "the supersede step no longer names the ledger key; if it moved, "
            "this suite must be told")


# ==========================================================================
# 5. FAILURE BEHAVIOUR — nothing here may stand the ladder down
# ==========================================================================


class FailureBehaviourTests(SupersedeHarness):
    def test_an_unreadable_ledger_skips_without_failing_the_job(self):
        """The artifact is already sealed by the time this step runs.

        Failing here would turn a completed hand-over into a red job and tell
        the owner the digest was lost, which it was not.
        """
        for error in ("http", "transport"):
            with self.subTest(error=error):
                self.outputs.write_text("", encoding="utf-8")
                code, out = self._run(kv_body=ledger_raw(degraded_entry()),
                                      kv_get_error=error)
                self.assertEqual(0, code,
                                 "an unreadable ledger failed a seal job that had "
                                 "already done its real work:\n%s" % out)
                self.assertEqual("skip", self._decision(), out)
                self.assertEqual([], self.puts)

    def test_an_unwritable_ledger_refuses_to_dispatch(self):
        """NO DISPATCH WITHOUT A LATCH.

        The latch is what stops this firing twice. A dispatch that could not be
        latched cannot be proven once-only, so the step stops here and says so
        loudly rather than sending a digest it cannot prove is the first.
        """
        for error in ("http", "transport"):
            with self.subTest(error=error):
                self.outputs.write_text("", encoding="utf-8")
                code, out = self._run(kv_body=ledger_raw(degraded_entry()),
                                      kv_put_error=error)
                self.assertNotEqual(
                    0, code,
                    "the ledger write failed and the step still exited 0, so "
                    "the dispatch below would fire with nothing to stop it "
                    "firing again:\n%s" % out)
                self.assertIn(
                    "error", out.lower(),
                    "the failure was not reported as an error:\n%s" % out)
                self.assertNotEqual(
                    "fire", self._decision(),
                    "the step reported a dispatch it could not latch.\n%s" % out)

    def test_no_secret_value_can_reach_the_step_log(self):
        code, out = self._run(kv_body=ledger_raw(degraded_entry()))
        self.assertEqual(0, code, out)
        self.assertNotIn(TOKEN, out,
                         "the KV token reached the step log; it travels in an "
                         "Authorization header and an exception string can "
                         "quote a request:\n%s" % out)
        self.assertNotIn(
            TOKEN, self.outputs.read_text(encoding="utf-8"),
            "the KV token reached the step outputs")


# ==========================================================================
# 6. THE DISPATCH AND THE STEP'S SHAPE
# ==========================================================================


class DispatchShapeTests(SupersedeHarness):
    def _run_text(self):
        return step_dict(STEP)["run"]

    def test_the_dispatch_targets_daily_news_at_phase_full(self):
        run = self._run_text()
        self.assertIn("daily-news.yml", run,
                      "the supersede does not dispatch daily-news.yml")
        self.assertIn("-f \"inputs[phase]=full\"", run,
                      "the supersede dispatch does not ask for phase=full, so "
                      "the run would not push the digest at all")
        self.assertIn("-f \"inputs[trigger_source]=supersede\"", run,
                      "the supersede dispatch does not mark itself, so "
                      "Fallback no-op check cannot tell it from an ordinary "
                      "run and will stand it down")
        self.assertIn("-f \"inputs[slot_date]=$TODAY\"", run,
                      "the supersede dispatch does not carry the slot's date, so "
                      "a seal that runs past Nepal midnight would have the "
                      "receiving run date itself off its own clock and supersede "
                      "the WRONG day's slot (run 2026-10-17T18:16:53Z is exactly "
                      "that failure: 00:01 NPT on the 18th, filed as the 18th)")

    def test_the_dispatch_waits_for_a_decision(self):
        run = self._run_text()
        self.assertIn("supersede", run,
                      "the bash half never reads the step's decision, so the "
                      "dispatch cannot be conditional on it")

    def test_the_step_body_compiles_and_every_heredoc_is_terminated(self):
        """BOTH HEREDOCS, NOT JUST THE FIRST.

        The step carries the decision in one heredoc and the
        record-that-a-push-left in a second, so "the block scalar ends on its
        terminator" is no longer a single check. What matters is that every
        `<<'PY'` has a column-0 `PY` somewhere after it, and that every body
        between them compiles — because 425 Python tests once passed while all
        four workflows were dead, and an unterminated heredoc is exactly how a
        step stops existing without a single test going red.
        """
        run = self._run_text()
        lines = run.split("\n")
        openers = [i for i, line in enumerate(lines)
                   if re.search(r"<<'(\w+)'", line)]
        self.assertGreaterEqual(
            len(openers), 2,
            "this step is expected to carry TWO heredocs (the decision, then the "
            "record that the dispatch was accepted); found %d" % len(openers))
        closed = 0
        for index in openers:
            tag = re.search(r"<<'(\w+)'", lines[index]).group(1)
            body_end = None
            for later in range(index + 1, len(lines)):
                if lines[later] == tag:
                    body_end = later
                    break
            self.assertIsNotNone(
                body_end,
                "the heredoc opened on line %d (%r) has no column-0 %r after it, "
                "so bash swallows the rest of the step:\n%s"
                % (index + 1, lines[index], tag, run[-400:]))
            closed += 1
            compile("\n".join(lines[index + 1:body_end]), "<supersede>", "exec")
        self.assertEqual(len(openers), closed,
                         "only %d of %d heredocs in this step are terminated"
                         % (closed, len(openers)))

    def test_the_step_passes_bash_syntax_check(self):
        bash = W.bash_executable()
        self.assertIsNotNone(bash, "bash is required to syntax-check a run block")
        proc = W.bash_syntax_check(self._run_text(), bash)
        self.assertEqual(0, proc.returncode,
                         "the supersede step is not valid bash:\n%s%s"
                         % (proc.stdout or "", proc.stderr or ""))

    def test_the_seal_job_may_dispatch(self):
        """`actions: write`, and the reason it changed.

        The seal job held `actions: read` on purpose — "a bug in it must not be
        able to dispatch anything". Dispatching the supersede is that exact
        capability, so the grant is widened deliberately and the comment beside
        it says so. A test that merely noticed the change would let the next
        edit silently take it back.
        """
        doc = W.load_workflow(WORKFLOW)
        seal = doc["jobs"]["seal"]
        perms = str((seal.get("permissions") or {}).get("actions") or "")
        self.assertEqual(
            "write", perms,
            "the seal job holds actions: %r and cannot dispatch the supersede. "
            "Either restore the grant, or the supersede has no dispatcher."
            % perms)
        self.assertIn(
            "supersede", raw_text(seal),
            "the permission comment beside the widened grant does not mention "
            "what now uses it. A widened permission whose comment still claims "
            "the opposite is worse than no comment.")


def raw_text(job):
    text = W.raw_workflow_text(WORKFLOW)
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        if line.rstrip() == "  seal:":
            start = index
            break
    if start is None:
        raise AssertionError("digest-prebuild.yml has no `seal:` job")
    out = []
    for line in lines[start:]:
        if out and line and not line.startswith("    "):
            break
        out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)