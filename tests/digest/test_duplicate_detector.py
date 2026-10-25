#!/usr/bin/env python3
"""tests/digest/test_duplicate_delector.py — the two dead guards, proved live.

WHAT WAS ACTUALLY BROKEN ON THE EVENING OF 2026-10-13
----------------------------------------------------
Two pushes went to the same ntfy topic for the same slot:

    19:25:05  Actions `daily-news` run 38406244065   evening digest, pri 3
    19:40:38  Worker `deliverDigestPlan` re-fire #4  evening digest, pri 4

Both carried `X-Sequence-Id: dmn-2026-10-13-evening`. ntfy's documented
behaviour is "publish a new message with the same sequence ID and clients will
replace the previous notification with the new one", so the 19:40 push ERASED
the 19:25 one the owner had already read. Title, tags and priority all differed;
the sequence id was the sole cause.

TWO GUARDS WERE SUPPOSED TO PREVENT IT AND NEITHER COULD RUN.

**Guard 1 — the duplicate-detector never executed.** Both arms read the topic
back with `GET <server>/<topic>/json?poll=1&since=all` and then parsed the
whole body as one JSON document. ntfy answers that with
`Content-Type: application/x-ndjson; charset=utf-8` — newline-delimited JSON,
one message per line — and with a ZERO-BYTE body when the topic is empty.
Measured on 2026-10-13 against ntfy.sh with a generated, non-existent topic:
`HTTP 200`, `ctype=application/x-ndjson; charset=utf-8`, `bodybytes=0`.

So the parse threw on every single call: a populated topic is many objects
separated by newlines (not JSON), and an empty topic is `""` (not JSON either).
In Actions that was caught and logged verbatim as

    WARNING: the sequence_id read-back failed (JSONDecodeError) - publishing
             anyway; a possible duplicate is the accepted cost

and in the Worker `findDeliveredBySequenceId` returned `unknown`, whose one
consequence is "a push always happens". The guard has never once suppressed
anything.

**Guard 2 — the two arms could not see each other.** Actions recorded its
delivery in the committed `state/last_digest_sent.json`; the Worker's dedup
guard reads Cloudflare KV `digest:ledger`. An Actions push therefore left no
`ntfy_sent_at` in KV and the Worker's own ledger guard had nothing to suppress.

WHAT IS PROVED HERE, AND HOW
----------------------------
The workflow steps' OWN bytes are extracted and exec'd — `urllib.request.urlopen`
is poisoned, so neither ntfy, nor Cloudflare, nor GitHub is reached and no
credential is needed. The Worker's OWN code is exercised through the same node
harness `digest_delivery.test.js` uses, driven from
`worker_sees_actions_ledger.test.js`.

Nothing here reads or re-implements a decision. The guard is judged by whether a
second push did or did not leave the process.

THE ANTI-VACUOUS RULE, AND WHY IT IS ENFORCED MECHANICALLY
-----------------------------------------------------------
A guard test that passes because the code never ran is worse than no test: it
is green, it is in the suite, and the defect it names is still live. So every
case here first asserts the HAPPY PATH happened — one POST, a captured KV write,
a page on disk — and only then asserts the guard fired. `MutationProofTests` at
the bottom reverts each fix in a temp copy and requires RED.
"""

from __future__ import annotations

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

WORKFLOW = "daily-news.yml"
NTFY_STEP = "Publish to ntfy (primary)"
RENDER_STEP = "Render digest HTML"
ARCHIVE_STEP = "Archive digest copy"

#: Today, in Nepal. The slot whose duplicate delivery this file is about.
TODAY = "2026-10-13"
SLOT = "evening"
SEQUENCE_ID = "dmn-%s-%s" % (TODAY, SLOT)
FULL_SEQUENCE_ID = SEQUENCE_ID + "-full"

#: The two instants from the incident table, as ntfy reported them.
PUSH_1925 = {
    "id": "ohOaYDwxpqvq",
    "time": 1791951905,
    "event": "message",
    "topic": "redacted-in-tests",
    "sequence_id": SEQUENCE_ID,
    "title": "Evening Digest - 19:25 - 2026-10-13 - 3 items",
}
PUSH_1940 = {
    "id": "pMq3dJt2R7Xk",
    "time": 1791952838,
    "event": "message",
    "topic": "redacted-in-tests",
    "sequence_id": SEQUENCE_ID,
    "title": "Evening Digest - 18:44 - 2026-10-13 - 3 items",
}

NAMESPACE_ID = "abcdefabcdefabcdefabcdefabcdefab"
ACCOUNT_ID = "acc0000000000000000000000000000ab"
TOKEN = "kv-token-not-a-real-secret"
SERVER = "https://ntfy.example.invalid"
TOPIC = "dmn-test-topic-3f81c0a9"

WRANGLER = (
    '\ufeffname = "breaking-monitor"\n'
    'main = "cloudflare-worker.js"\n'
    "\n"
    "[[kv_namespaces]]\n"
    'binding = "KV"\n'
    'id = "%s"\n'
) % NAMESPACE_ID


def step_run(name):
    """The `run:` TEXT of one step, by its literal name.

    `iter_all_steps` yields the parsed step DICT as its third element, so the
    run block has to be pulled out of it -- `heredoc_bodies` wants text.
    """
    for _job, _step, step_dict in W.iter_all_steps(WORKFLOW):
        if _step == name:
            return step_dict["run"]
    raise AssertionError("%s has no step %r" % (WORKFLOW, name))


def ntfy_body():
    """The first Python heredoc of the ntfy step. Never returns an empty str.

    `test_render_honesty.py` slices this same step by its literal name and
    execs the same bytes, so a rename or a body swap breaks 26 tests there first
    and loudly. This raises rather than returning '' so the failure names the
    cause instead of surfacing as a confusing ValueError further down.
    """
    bodies = [b for _t, b in heredoc_bodies(step_run(NTFY_STEP))]
    if not bodies:
        raise AssertionError(
            "step %r has no Python heredoc: the step was renamed, or its body "
            "was replaced with something this suite can no longer execute. "
            "Update this suite deliberately rather than deleting the check."
            % NTFY_STEP)
    return bodies[0]


def last_heredoc(step_name, min_index=0):
    bodies = [b for _t, b in heredoc_bodies(step_run(step_name))]
    if len(bodies) <= min_index:
        raise AssertionError(
            "step %r carries %d Python heredoc(s); this suite reads index %d. "
            "A step that lost a heredoc has lost behaviour, not moved it."
            % (step_name, len(bodies), min_index))
    return bodies[min_index]


def exec_step(body, env, cwd, name, buf):
    """Run a step's OWN bytes and return its stdout.

    `SystemExit` is how these steps finish on purpose -- `sys.exit(0)` is the
    "nothing to compare, this build owns the URL" and "recorded, done" exits --
    so it is caught and printed rather than surfacing as an ERROR. Nothing else
    is caught: a step swallowing a real crash would hide it.
    """
    saved = dict(os.environ)
    prev = os.getcwd()
    try:
        os.chdir(str(cwd))
        os.environ.update({k: str(v) for k, v in env.items()})
        with contextlib.redirect_stdout(buf):
            try:
                exec(compile(body, "<%s>" % name, "exec"), {"__name__": name})
            except SystemExit as exc:
                print("%s exited %s" % (name, exc.code))
    finally:
        os.chdir(prev)
        os.environ.clear()
        os.environ.update(saved)


class _FakeResponse:
    def __init__(self, body="", status=200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ActionsHarness(unittest.TestCase):
    """Exec the real ntfy step with every outbound call captured."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dmn-dupe-detector-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "deploy").mkdir()
        (self.tmp / "deploy" / "wrangler.toml").write_text(
            WRANGLER, encoding="utf-8")
        (self.tmp / "state").mkdir()
        self.payload = {
            "digest_time": SLOT,
            "date": TODAY,
            "stories": [
                {"title": "BLEACH", "category": "gaming", "priority": 4,
                 "source_url": "https://example.invalid/bleach",
                 "summary": "A story from the 18:44 build."},
            ],
            "data_cutoff_npt": "2026-10-13 18:40",
            "generated_at_npt": "2026-10-13 18:44",
            "source_health": {},
        }
        (self.tmp / "state" / "stories.validated.json").write_text(
            json.dumps(self.payload), encoding="utf-8")
        self.posts = []
        self.kv_gets = []
        self.kv_puts = []
        self.readback_bodies = []

    # -- the fake network -------------------------------------------------
    def _urlopen(self, history_body=None, history_status=200,
                 kv_get_error=None, kv_put_error=None, ntfy_error=None):
        posts, kv_gets, kv_puts, bodies = self.posts, self.kv_gets, \
            self.kv_puts, self.readback_bodies
        state = {"kv": None}

        def fake(request, *args, **kwargs):
            url = str(getattr(request, "full_url", request))
            # `Request(..., data=...)` leaves `.method` unset and derives POST
            # from the presence of data, so `get_method()` is the only correct
            # reader here. Getting this wrong routes the publish through the
            # read-back branch and the test fails as a JSON error rather than
            # as the thing it is testing.
            getter = getattr(request, "get_method", None)
            method = (getter() if callable(getter)
                      else (getattr(request, "method", None) or "GET")).upper()
            if method == "PUT" and "api.cloudflare.com" in url:
                raw = getattr(request, "data", None)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                kv_puts.append({"url": url, "body": raw})
                if kv_put_error == "http":
                    raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
                if kv_put_error == "transport":
                    raise urllib.error.URLError("injected transport failure")
                state["kv"] = raw
                return _FakeResponse('{"success":true}')
            if method == "POST":
                posts.append({"url": url,
                              "headers": dict(getattr(request, "headers", {}) or {})})
                if ntfy_error:
                    raise urllib.error.URLError("injected ntfy failure")
                return _FakeResponse(json.dumps({"id": "ntfy-ok-1"}))
            if "api.cloudflare.com" in url:
                kv_gets.append(url)
                if kv_get_error == "http":
                    raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
                if kv_get_error == "transport":
                    raise urllib.error.URLError("injected transport failure")
                return _FakeResponse(state["kv"] if state["kv"] is not None else "")
            # Everything left is the ntfy topic read-back.
            bodies.append(history_body)
            if history_status != 200:
                raise urllib.error.HTTPError(url, history_status, "Bad", {}, None)
            return _FakeResponse(history_body if history_body is not None else "")
        return fake

    #: The variables the step needs to run at all. Every inline harness in this
    #: file spells out the same seven, so they are written down once here and an
    #: `env_overrides` mapping merges onto them. Without them the step has no
    #: topic, no date and no slot, and would exit on a missing variable before it
    #: ever reached the guard under test -- a green run that measured nothing.
    BASE_ENV = {
        "NTFY_TOPIC": TOPIC,
        "NTFY_SERVER": SERVER,
        "DRY_RUN": "false",
        "TODAY": TODAY,
        "DIGEST_TIME": SLOT,
        "CF_LEDGER_TOKEN": TOKEN,
        "CF_ACCOUNT_ID": ACCOUNT_ID,
    }

    def run_step(self, env_overrides=None, history_body=None, history_status=200,
                 kv_get_error=None, kv_put_error=None, ntfy_error=None):
        """The convenience form of the inline harness, same save/restore pair.

        `exec_step()` directly above is the reference for what this must do:
        snapshot the environment and the working directory, restore BOTH in a
        `finally`, and treat `SystemExit` as the step finishing on purpose. A
        missing restore leaks the temp directory's `NTFY_TOPIC` into every later
        test in the process, which is how a guard test can pass for the wrong
        reason.
        """
        env = dict(self.BASE_ENV)
        env.update(env_overrides or {})
        real = urllib.request.urlopen
        urllib.request.urlopen = self._urlopen(
            history_body, history_status, kv_get_error, kv_put_error, ntfy_error)
        buf = io.StringIO()
        saved = dict(os.environ)
        prev = os.getcwd()
        try:
            os.chdir(self.tmp)
            os.environ.update({k: str(v) for k, v in env.items()})
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(ntfy_body(), "<ntfy-step>", "exec"),
                         {"__name__": "ntfy_step"})
                except SystemExit as exc:
                    print("ntfy_step exited %s" % exc.code)
        finally:
            os.chdir(prev)
            os.environ.clear()
            os.environ.update(saved)
            urllib.request.urlopen = real
        return buf.getvalue()

    # -- assertions -------------------------------------------------------
    def ntfy_ok(self):
        flag = self.tmp / "state" / ".ntfy_ok"
        return flag.read_text(encoding="utf-8").strip() if flag.is_file() else None

    def posted_sequence_id(self):
        for p in self.posts:
            for key, value in p["headers"].items():
                if key.lower() == "x-sequence-id":
                    return value
        return None

    def kv_ledger(self):
        if not self.kv_puts:
            return None
        return json.loads(self.kv_puts[-1]["body"])

    def slot_entry(self):
        ledger = self.kv_ledger()
        if not ledger:
            return None
        day = (ledger.get("days") or {}).get(TODAY) or {}
        return day.get(SLOT)


def ndjson(*messages):
    """What ntfy actually serves: one JSON object per line."""
    return "\n".join(json.dumps(m) for m in messages)


# ==========================================================================
# 1. GUARD 1 — THE DUPLICATE-DETECTOR CAN NOW ACTUALLY SUPPRESS
# ==========================================================================


class ReadbackSuppressesTests(ActionsHarness):
    """THE RED CASE IS TODAY'S, EXACTLY AS IT HAPPENED."""

    def test_a_second_push_for_this_slot_is_not_published(self):
        """Two pushes for dmn-2026-10-13-evening, the second must not go out.

        The history carries BOTH of the evening pushes that reached the topic on
        2026-10-13, in the shape ntfy actually serves them: NDJSON, one message
        per line. Before the fix this body failed to parse, the step logged
        `JSONDecodeError ... publishing anyway`, and the duplicate left.
        """
        out = self.run_step(history_body=ndjson(PUSH_1940, PUSH_1925))
        self.assertEqual(
            0, len(self.posts),
            "the Actions arm published a SECOND notification for %s. The topic "
            "already carried %s and %s under that sequence id, so ntfy would "
            "have REPLACED the notification the owner had already read.\n%s"
            % (SEQUENCE_ID, PUSH_1925["id"], PUSH_1940["id"], out))
        self.assertIn(
            "already on the topic", out,
            "the step suppressed the push without saying so, so a silent "
            "no-op and a working guard look identical:\n%s" % out)
        self.assertNotIn(
            "JSONDecodeError", out,
            "the read-back is still failing to parse. The guard is dead again:\n%s"
            % out)
        # And the step must still report the channel as delivered: it was, by
        # the earlier push, and standing the backups down is the whole point of
        # reporting it true.
        self.assertEqual("true", self.ntfy_ok(), out)

    def test_the_history_is_read_in_ntfys_actual_ndjson_shape(self):
        """The happy path first: a body that is NOT NDJSON must still publish.

        Without this the test above could be green because nothing was ever
        read back — a guard that suppresses unconditionally would pass it.
        """
        out = self.run_step(history_body="")
        self.assertEqual(
            1, len(self.posts),
            "an EMPTY history published nothing, so the suppression test above "
            "proves nothing about parsing — it would pass for a guard that "
            "never sends anything:\n%s" % out)
        self.assertEqual(SEQUENCE_ID, self.posted_sequence_id())

    def test_a_single_line_history_is_read(self):
        out = self.run_step(history_body=ndjson(PUSH_1925))
        self.assertEqual(0, len(self.posts), out)
        self.assertIn("already on the topic", out)

    def test_a_history_without_this_slot_still_publishes(self):
        """A topic carrying OTHER slots must not suppress this one.

        Two messages, neither of them this slot, so the NDJSON shape under test
        is still multi-line. A history that ALSO carried this slot's sequence id
        would have to suppress -- that is Guard 1 working, and it is what
        `test_a_single_line_history_is_read` above pins.
        """
        other = dict(PUSH_1925, id="zzz", sequence_id="dmn-2026-10-13-morning")
        other2 = dict(PUSH_1940, id="yyy", sequence_id="dmn-2026-10-12-evening")
        out = self.run_step(history_body=ndjson(other, other2))
        self.assertEqual(1, len(self.posts), out)
        self.assertEqual(SEQUENCE_ID, self.posted_sequence_id())

    def test_an_older_single_document_array_is_still_accepted(self):
        """Deliberate tolerance, and it is not a second source of truth.

        ntfy serves NDJSON. A proxy, or a future tidy of the server, that
        pretty-printed one document would otherwise put this guard back to
        blind for exactly the reason it was blind on 2026-10-13.
        """
        out = self.run_step(
            history_body=json.dumps([PUSH_1925, PUSH_1940], indent=2))
        self.assertEqual(0, len(self.posts),
                         "a pretty-printed single document was not read, so a "
                         "server-side tidy would blind the guard again:\n%s" % out)

    def test_a_zero_byte_body_is_an_empty_history_and_not_an_error(self):
        """MEASURED SHAPE. This is what ntfy answers for an empty topic.

        Measured 2026-10-13 against ntfy.sh with a generated, non-existent
        topic: HTTP 200, ctype=application/x-ndjson, bodybytes=0. An empty body
        is the server saying "nothing cached", which is the ordinary first
        delivery of the day — so it must publish, and must NOT be reported as a
        parse failure, because a failure means `unknown` means "push anyway" and
        an ordinary delivery would then carry a permanent false warning.
        """
        out = self.run_step(history_body="")
        self.assertEqual(1, len(self.posts), out)
        self.assertNotIn(
            "JSONDecodeError", out,
            "an empty history was reported as a parse failure. It is not one — "
            "it is the answer for an empty topic:\n%s" % out)
        self.assertNotIn(
            "the sequence_id read-back failed", out,
            "an empty history was reported as an unreadable cache:\n%s" % out)

    def test_the_readback_keeps_poll_1(self):
        """`poll=1` STAYS, and this is why it is pinned rather than left alone.

        Dropping `poll=1` turns `GET /json?since=all` into an UNBOUNDED
        long-poll: ntfy holds the connection open until a message arrives. On a
        topic that receives a handful of messages a day that is not a cache
        read, it is a step that never finishes. Keeping it bounds the call by
        ntfy's own poll timeout.
        """
        seen = []

        real = urllib.request.urlopen
        base = self._urlopen(history_body="")

        def spy(request, *a, **k):
            url = str(getattr(request, "full_url", request))
            if "ntfy" in url:
                seen.append(url)
            return base(request, *a, **k)

        urllib.request.urlopen = spy
        buf = io.StringIO()
        saved = dict(os.environ)
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            os.environ.update({
                "NTFY_TOPIC": TOPIC, "NTFY_SERVER": SERVER, "DRY_RUN": "false",
                "TODAY": TODAY, "DIGEST_TIME": SLOT, "CF_LEDGER_TOKEN": TOKEN,
                "CF_ACCOUNT_ID": ACCOUNT_ID,
            })
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(ntfy_body(), "<ntfy-step>", "exec"),
                         {"__name__": "ntfy_step"})
                except SystemExit as exc:
                    print("ntfy_step exited %s" % exc.code)
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
            urllib.request.urlopen = real

        readbacks = [u for u in seen if "/json" in u]
        self.assertEqual(1, len(readbacks),
                         "expected exactly one topic read-back, got %r" % readbacks)
        self.assertIn("poll=1", readbacks[0],
                      "the read-back dropped poll=1, which turns it into an "
                      "unbounded long-poll: %s" % readbacks[0])
        self.assertIn("since=all", readbacks[0], readbacks[0])

    def test_an_unreachable_server_still_publishes(self):
        """Fail OPEN, and unchanged: a miss is worse than a duplicate.

        The one thing this guard must never do is turn a transport failure into
        a silent miss. A push always happens when we do not know.
        """
        out = self.run_step(history_body=None, history_status=502)
        self.assertEqual(1, len(self.posts), out)
        self.assertIn("read-back failed", out, out)

    def test_a_body_that_is_neither_shape_still_publishes(self):
        """A PARSE failure is "we do not know", so it must fail OPEN too.

        The transport case above covers a dead server. This covers the other
        half of the same rule: a body that is neither NDJSON nor one whole
        document -- an HTML error page behind a 200, a proxy's truncation, a
        server-side tidy nobody predicted. That is exactly what the topic read
        served in 2026-10-13 in spirit if not in letter, and this step's
        contract says the cost of guessing wrong here is a possible duplicate
        while the cost of failing closed is NO DIGEST.

        The half-finished 2026-10-13 refactor hoisted `parse_ntfy_history` into
        an `else:` clause, OUTSIDE the try. An unparseable body then escaped
        `main()` and landed on the FATAL handler, which writes ntfy_ok=false
        and publishes nothing -- a silent miss, reached by the same malformed
        input that used to produce the logged `JSONDecodeError` and publish
        anyway. Measured, reverting that placement in a temp copy:

            FATAL: unexpected ntfy step error (ntfy_ok=false): Expecting value:
            line 1 column 1 (char 0)

        and zero POSTs. So the parse has to stay inside the try, and this test
        is what holds it there.
        """
        out = self.run_step(history_body="<html>gateway says hello</html>")
        self.assertEqual(
            1, len(self.posts),
            "a malformed cache body suppressed the digest. Failing closed here "
            "costs the owner the digest itself, and a MISS is the outcome this "
            "module's delivery contract calls the worse one:\n%s" % out)
        self.assertIn("read-back failed", out,
                      "the malformed body was not reported as unreadable, so a "
                      "shape ntfy can still serve is indistinguishable from one "
                      "it cannot:\n%s" % out)
        self.assertNotIn("FATAL", out,
                         "the parse escaped main() instead of being handled as a "
                         "read-back failure:\n%s" % out)
        self.assertEqual("true", self.ntfy_ok(), out)


# ==========================================================================
# 2. GUARD 2 — THE TWO ARMS CAN NOW SEE EACH OTHER
# ==========================================================================


class LedgerHandoffTests(ActionsHarness):
    def test_a_successful_push_records_the_delivery_in_kv(self):
        """GUARD 2, and the 19:25 case: Actions pushed and KV learned nothing.

        The Worker's duplicate guard reads `digest:ledger`; until this write
        existed, an Actions delivery left no trace there, so the Worker's own
        guard had nothing to suppress on its very next run for the slot.
        """
        out = self.run_step(history_body="")
        self.assertEqual(1, len(self.posts),
                         "the happy path did not push, so a KV write below "
                         "would prove nothing:\n%s" % out)
        self.assertTrue(self.kv_puts,
                        "the push succeeded and NOTHING was written to the KV "
                        "delivery ledger, so the Worker's duplicate guard still "
                        "cannot see an Actions delivery:\n%s" % out)
        entry = self.slot_entry()
        self.assertIsNotNone(entry, "the ledger write carried no entry for %s %s: %r"
                             % (TODAY, SLOT, self.kv_ledger()))
        for field in ("ntfy_sent_at", "sequence_id", "kind"):
            self.assertIn(field, entry,
                          "the Actions arm records no %r, which is one of the "
                          "three fields the Worker's guard and the watchdog "
                          "read: %r" % (field, entry))
        self.assertEqual(SEQUENCE_ID, entry["sequence_id"],
                         "the recorded sequence_id does not match the id the push "
                         "was published under, so a later read-back would look "
                         "for a message that was never sent: %r" % entry)
        self.assertRegex(
            entry["ntfy_sent_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            "ntfy_sent_at is not the instant format the Worker's own writer "
            "emits, so the punctuality comparison between the two arms is "
            "between two string formats: %r" % entry)

    def test_the_record_is_written_after_the_push_not_before(self):
        """Write-ahead `pending` from THIS arm would cost a whole day's digest.

        A record written before the POST, by a run that then dies, suppresses
        the next run's real digest for that slot — a silent miss. The ledger
        write must follow a confirmed delivery.
        """
        order = []

        real = urllib.request.urlopen
        base = self._urlopen(history_body="")

        def spy(request, *a, **k):
            url = str(getattr(request, "full_url", request))
            getter = getattr(request, "get_method", None)
            method = (getter() if callable(getter)
                      else (getattr(request, "method", None) or "GET")).upper()
            if method == "POST":
                order.append("POST ntfy")
            elif method == "PUT" and "cloudflare" in url:
                order.append("PUT kv")
            return base(request, *a, **k)

        urllib.request.urlopen = spy
        buf = io.StringIO()
        cwd = os.getcwd()
        saved = dict(os.environ)
        try:
            os.chdir(self.tmp)
            os.environ.update({
                "NTFY_TOPIC": TOPIC, "NTFY_SERVER": SERVER, "DRY_RUN": "false",
                "TODAY": TODAY, "DIGEST_TIME": SLOT, "CF_LEDGER_TOKEN": TOKEN,
                "CF_ACCOUNT_ID": ACCOUNT_ID,
            })
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(ntfy_body(), "<ntfy-step>", "exec"),
                         {"__name__": "ntfy_step"})
                except SystemExit as exc:
                    print("ntfy_step exited %s" % exc.code)
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
            urllib.request.urlopen = real

        self.assertEqual(["POST ntfy", "PUT kv"], order,
                         "the KV record and the ntfy push happened in the order "
                         "%r. A record written BEFORE the push is a write-ahead "
                         "`pending` that, on a crash, suppresses the next run's "
                         "real digest for this slot.\n%s" % (order, buf.getvalue()))

    def test_the_worker_suppresses_when_it_reads_this_ledger(self):
        """THE ROUND TRIP, across both languages.

        The Python half runs the real Actions step and writes the ledger it
        really produced; a node harness then loads the real Worker, seeds a KV
        with exactly those bytes and runs the real `deliverDigest`. The Worker
        must publish nothing.

        A test in each language separately would agree with itself: the Python
        one cannot see the Worker's guard, and the JS one would be reading a
        fixture rather than a ledger anything actually wrote.
        """
        out = self.run_step(history_body="")
        self.assertEqual(1, len(self.posts), out)
        self.assertTrue(self.kv_puts, out)
        ledger_path = self.tmp / "actions-ledger.json"
        ledger_path.write_text(self.kv_puts[-1]["body"], encoding="utf-8")

        js = Path(_TESTS) / "digest" / "worker_sees_actions_ledger.test.js"
        proc = subprocess.run(
            ["node", "--test", str(js)],
            cwd=str(_ROOT), capture_output=True, text=True, errors="replace",
            env=dict(os.environ, ACTIONS_LEDGER_FILE=str(ledger_path)),
            timeout=300)
        self.assertEqual(
            0, proc.returncode,
            "the Worker, handed the ledger the Actions arm really wrote, did "
            "not suppress its own push. The two arms still cannot see each "
            "other.\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (proc.stdout[-4000:], proc.stderr[-2000:]))

    def test_an_ordinary_push_is_suppressed_by_a_recorded_delivery(self):
        """One delivery per (date, slot) — the Actions arm's own share of it.

        With KV write scope present, a second run for a slot KV already records
        must stand down even if the topic read-back cannot see the first push.
        """
        existing = {"schema_version": 1, "days": {TODAY: {SLOT: {
            "state": "sent", "kind": "delivered", "sequence_id": SEQUENCE_ID,
            "ntfy_sent_at": "2026-10-13T13:25:05Z",
        }}}}
        real = urllib.request.urlopen
        posted = []

        def fake(request, *a, **k):
            url = str(getattr(request, "full_url", request))
            getter = getattr(request, "get_method", None)
            method = (getter() if callable(getter)
                      else (getattr(request, "method", None) or "GET")).upper()
            if method == "POST":
                posted.append(url)
                return _FakeResponse(json.dumps({"id": "should-not-happen"}))
            if method == "PUT":
                return _FakeResponse('{"success":true}')
            if "cloudflare" in url:
                return _FakeResponse(json.dumps(existing))
            # The topic read-back sees NOTHING: the ledger is the only guard
            # standing here, which is the case this test exists for.
            return _FakeResponse("")
        urllib.request.urlopen = fake
        buf = io.StringIO()
        cwd = os.getcwd()
        saved = dict(os.environ)
        try:
            os.chdir(self.tmp)
            os.environ.update({
                "NTFY_TOPIC": TOPIC, "NTFY_SERVER": SERVER, "DRY_RUN": "false",
                "TODAY": TODAY, "DIGEST_TIME": SLOT, "CF_LEDGER_TOKEN": TOKEN,
                "CF_ACCOUNT_ID": ACCOUNT_ID,
            })
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(ntfy_body(), "<ntfy-step>", "exec"),
                         {"__name__": "ntfy_step"})
                except SystemExit as exc:
                    print("ntfy_step exited %s" % exc.code)
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
            urllib.request.urlopen = real
        text = buf.getvalue()
        self.assertEqual([], posted,
                         "the Actions arm published a second notification for a "
                         "slot the ledger already records as delivered:\n%s" % text)
        self.assertIn("ntfy_sent_at", text, text)

    def test_an_unwritable_ledger_does_not_fail_a_delivered_push(self):
        """The push already happened. Reporting it as failed would double it.

        This step's caller reads `state/.ntfy_ok` on the next line, so a
        non-zero exit here would discard a delivered digest's own result AND
        fire the Gmail backup against a push that already landed. The failure
        must be loud, named, and non-fatal.
        """
        out = self.run_step(history_body="", kv_put_error="http")
        self.assertEqual("true", self.ntfy_ok(),
                         "a delivered push was reported as NOT delivered, which "
                         "fires the backup ladder against a notification the "
                         "owner already has:\n%s" % out)
        self.assertEqual(1, len(self.posts), out)
        self.assertIn("KV write", out,
                      "the failed ledger write was not reported, so a "
                      "permanently mis-scoped token would look identical to a "
                      "healthy one for ever:\n%s" % out)
        self.assertIn("Write", out,
                      "the report does not name the permission needed:\n%s" % out)
        self.assertNotIn(TOKEN, out,
                         "the KV token reached the step log:\n%s" % out)

    def test_the_secret_never_reaches_the_log(self):
        out = self.run_step(history_body="")
        self.assertNotIn(TOKEN, out, out)
        self.assertNotIn(
            TOKEN, json.dumps([p for p in self.kv_puts]),
            "the KV token reached the ledger body")


# ==========================================================================
# 3. THE SEQUENCE-ID CONTRACT — D-003 HONOURED, NOT PAPERED OVER
# ==========================================================================


class SupersedeSequenceIdTests(ActionsHarness):
    """A repair push must NOT reuse the id of the push it replaces.

    This is the whole defect, in one line. `digestFullSequenceId` existed in
    deploy/cloudflare-worker.js and was unused on this path; digest-prebuild.yml
    already latches `supersede_sequence_id` under that value before dispatching
    here with `trigger_source=supersede`; and this step published under the BASE
    id anyway. ntfy replaces a message it has already seen under a sequence id,
    so the repair erased the notification instead of following it.
    """

    def test_a_supersede_push_uses_the_full_sequence_id(self):
        out = self.run_step(history_body="",
                            env_overrides={"TRIGGER_SOURCE": "supersede"})
        self.assertEqual(1, len(self.posts), out)
        self.assertEqual(
            FULL_SEQUENCE_ID, self.posted_sequence_id(),
            "the supersede published under %r. The partial push it replaces is "
            "already on the topic under %r, so this REPLACED it.\n%s"
            % (self.posted_sequence_id(), SEQUENCE_ID, out))

    def test_a_supersede_is_not_suppressed_by_the_push_it_replaces(self):
        """`ntfy_sent_at` is set by the degraded push; the repair must get past it.

        The three degraded kinds all set `ntfy_sent_at`, because a push happened.
        Guarding the repair on it makes the repair unreachable on precisely the
        slots it exists for, and a complete digest would sit sealed forever.
        This is the same asymmetry tests/digest/test_supersede_push.py pins on
        the dispatch side.
        """
        existing = {"schema_version": 1, "days": {TODAY: {SLOT: {
            "state": "sent", "kind": "partial", "sequence_id": SEQUENCE_ID,
            "ntfy_sent_at": "2026-10-13T13:30:00Z",
        }}}}
        real = urllib.request.urlopen
        posted, headers = [], []

        def fake(request, *a, **k):
            url = str(getattr(request, "full_url", request))
            getter = getattr(request, "get_method", None)
            method = (getter() if callable(getter)
                      else (getattr(request, "method", None) or "GET")).upper()
            if method == "POST":
                posted.append(url)
                headers.append(dict(getattr(request, "headers", {}) or {}))
                return _FakeResponse(json.dumps({"id": "full-1"}))
            if method == "PUT":
                return _FakeResponse('{"success":true}')
            if "cloudflare" in url:
                return _FakeResponse(json.dumps(existing))
            return _FakeResponse("")
        urllib.request.urlopen = fake
        buf = io.StringIO()
        cwd = os.getcwd()
        saved = dict(os.environ)
        try:
            os.chdir(self.tmp)
            os.environ.update({
                "NTFY_TOPIC": TOPIC, "NTFY_SERVER": SERVER, "DRY_RUN": "false",
                "TODAY": TODAY, "DIGEST_TIME": SLOT, "TRIGGER_SOURCE": "supersede",
                "CF_LEDGER_TOKEN": TOKEN, "CF_ACCOUNT_ID": ACCOUNT_ID,
            })
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(ntfy_body(), "<ntfy-step>", "exec"),
                         {"__name__": "ntfy_step"})
                except SystemExit as exc:
                    print("ntfy_step exited %s" % exc.code)
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
            urllib.request.urlopen = real
        self.assertEqual(1, len(posted),
                         "the supersede was suppressed by the partial push's own "
                         "ntfy_sent_at, so a complete digest that was built and "
                         "sealed would never reach the owner:\n%s"
                         % buf.getvalue())

    def test_the_supersede_records_ntfy_full_sent_at_and_not_ntfy_sent_at(self):
        existing = {"schema_version": 1, "days": {TODAY: {SLOT: {
            "state": "sent", "kind": "partial", "sequence_id": SEQUENCE_ID,
            "ntfy_sent_at": "2026-10-13T13:30:00Z",
        }}}}
        out = self.run_step(history_body=ndjson(PUSH_1925),
                            env_overrides={"TRIGGER_SOURCE": "supersede"})
        self.assertTrue(self.kv_puts, out)
        # The read-back above sees the PARTIAL under the base id, which does not
        # match the -full id, so the publish proceeds.
        self.assertEqual(1, len(self.posts), out)
        entry = self.slot_entry()
        self.assertIn("ntfy_full_sent_at", entry,
                      "the full push was not recorded, so the seal job would "
                      "supersede this slot again on its next prebuild: %r" % entry)

    def test_an_ordinary_push_is_not_labelled_a_full_digest(self):
        """The label is a CLAIM. A push that is not a replacement must not say it is."""
        out = self.run_step(history_body="",
                            env_overrides={"TRIGGER_SOURCE": "cron"})
        self.assertEqual(1, len(self.posts), out)
        self.assertNotIn("full digest", out,
                         "an ordinary push was labelled as a replacement:\n%s" % out)


# ==========================================================================
# 4. ARCHIVE INTEGRITY — TWO BUILDS OF ONE SLOT
# ==========================================================================


class ArchiveIntegrityTests(unittest.TestCase):
    """The page at the stable URL may only ever get NEWER.

    WHY NOT A SUFFIXED FILENAME: `render_index` (scripts/render_digest.py:2630)
    scans `out_dir` with

        ^(\\d{4}-\\d{2}-\\d{2})-(morning|evening)\\.html$

    and skips anything that does not match, so `2026-10-13-evening-1844.html`
    would silently vanish from the archive index the owner browses — and break
    every link already saved from a notification. One stable URL per
    (date, slot) is the property worth keeping.
    """

    FOOTER = "Generated by Personal News Informer \u00b7 %s NPT"

    #: The section header the REAL renderer emits, with the leading emoji the older
    #: pages carry and `section_label` strips. This string is load-bearing, not
    #: decoration: `parse_digest_categories` looks the header up in
    #: SECTION_TO_LABEL (scripts/render_digest.py:1098) under the plain-text key
    #: `"WORLD -- MAJOR EVENTS"`, and an invented `"WORLD"` is simply not in that
    #: map. A fixture that skipped it returned `{}` -- indistinguishable from
    #: "the page the owner is served carries no stories", which is the exact
    #: failure this class exists to catch.
    SECTION = "\U0001f4f0 WORLD — MAJOR EVENTS"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dmn-archive-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "docs" / "digests").mkdir(parents=True)
        (self.tmp / "state").mkdir()
        (self.tmp / "scripts").mkdir()
        (self.tmp / "scripts" / "render_digest.py").write_text(
            "MARKER = 'the real render_digest.py'\n"
            "def render_index(out_dir, index_path, current_date, current_time):\n"
            "    with open(index_path, 'w', encoding='utf-8') as fh:\n"
            "        fh.write('INDEX for ' + current_date + '\\n')\n"
            "    return True\n",
            encoding="utf-8")
        # The archive step's heredoc imports render_digest out of `scripts/`, so
        # that directory has to be importable for the swap to run at all.
        self.addCleanup(
            lambda: sys.path.__delitem__(sys.path.index(str(self.tmp / "scripts")))
            if str(self.tmp / "scripts") in sys.path else None)
        sys.path.insert(0, str(self.tmp / "scripts"))
        self.target = self.tmp / "docs" / "digests" / ("%s-%s.html" % (TODAY, SLOT))

    def page(self, built_at, marker):
        """A page shaped like the real one: the footer AND the scrapers' markup."""
        return (
            '<!DOCTYPE html><html><head><title>Evening</title></head><body>'
            '<h2 class="section-header">%s</h2>'
            '<article class="card">'
            '<img class="card-thumb" src="https://img.invalid/%s.png" alt="">'
            '<h3>%s</h3></article>'
            '<article class="card"><h3>%s second</h3></article>'
            '<footer>%s</footer></body></html>'
            % (self.SECTION, marker, marker, marker,
               self.FOOTER % built_at))

    def run_archive_step(self, backup_at, new_at):
        """Run the render step's real archive-decision heredoc.

        The step is taken apart at its own seam: the renderer is simulated by
        writing `new_at`'s page into the target (which is exactly what
        `render_digest.py:3002` does with its unconditional `open(.., "w")`),
        and the heredoc that decides which build owns the URL is exec'd verbatim.
        """
        if backup_at is None:
            for p in (self.tmp / "state").glob(".page-backup-*"):
                p.unlink()
        else:
            (self.tmp / "state" /
             (".page-backup-%s-%s.html" % (TODAY, SLOT))).write_text(
                self.page(backup_at, "old"), encoding="utf-8")
        self.target.write_text(self.page(new_at, "new"), encoding="utf-8")

        env = {
            "TARGET_PAGE": str(self.target),
            "BACKUP_PAGE": str(self.tmp / "state" /
                               (".page-backup-%s-%s.html" % (TODAY, SLOT))),
            "ARCHIVE_DIR": str(self.tmp / "state" / "digest-archive"),
            "TODAY": TODAY,
            "DIGEST_TIME": SLOT,
        }
        body = last_heredoc(RENDER_STEP, min_index=0)
        saved = dict(os.environ)
        cwd = os.getcwd()
        buf = io.StringIO()
        try:
            os.chdir(self.tmp)
            os.environ.update({k: str(v) for k, v in env.items()})
            with contextlib.redirect_stdout(buf):
                # `sys.exit(0)` is how this heredoc finishes a branch on purpose
                # -- "this build owns the URL", "REFUSED, the previously
                # published page is back in place" -- so it is caught and
                # printed, exactly as `exec_step()` does above. Letting it
                # escape is not a failure signal; it is the step saying it is
                # done, and `run_archive_step` docstring above names that.
                try:
                    exec(compile(body, "<render-archive>", "exec"),
                         {"__name__": "render_archive"})
                except SystemExit as exc:
                    print("render_archive exited %s" % exc.code)
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        return buf.getvalue()

    def archived(self):
        d = self.tmp / "state" / "digest-archive"
        return sorted(p.name for p in d.glob("*.html")) if d.is_dir() else []

    def test_the_first_build_of_a_slot_owns_the_url(self):
        out = self.run_archive_step(None, "2026-10-13 19:24")
        self.assertIn("2026-10-13 19:24", self.target.read_text(encoding="utf-8"),
                      "the first build of a slot did not reach the page:\n%s" % out)
        self.assertEqual([], self.archived(),
                         "there was no previous build to keep, yet something "
                         "was archived:\n%s" % out)

    def test_an_older_build_cannot_overwrite_a_newer_page(self):
        """THE 2026-10-13 CASE: the 19:40 re-fire delivered the 18:44 build.

        The page on disk was built at 19:24. This run built at 18:44. The older
        build may not take the URL, or the notification the owner read would
        point at content that does not match the notification beside it.
        """
        out = self.run_archive_step("2026-10-13 19:24", "2026-10-13 18:44")
        served = self.target.read_text(encoding="utf-8")
        self.assertIn("2026-10-13 19:24", served,
                      "the older build took the stable URL, which is the defect "
                      "this step exists to stop:\n%s" % out)
        self.assertIn("REFUSED", out,
                      "the refusal was silent, so a working guard and a broken "
                      "one look the same:\n%s" % out)
        self.assertIn("old", served,
                      "the page now on disk is not the older build either")

    def test_no_build_of_a_slot_is_destroyed(self):
        """Both builds still EXIST after the swap. That is the whole claim.

        The stable URL can only ever hold one of them — that is what a stable
        URL means — so the losing build has to be kept somewhere the owner can
        still reach it. On 2026-10-13 the 18:44 stories (BLEACH,
        Character.ai, the highway food-safety item) existed NOWHERE: the page
        had been overwritten and the email archive had been `mv`-ed over.
        """
        out = self.run_archive_step("2026-10-13 19:24", "2026-10-13 18:44")
        self.assertTrue(self.archived(),
                        "the displaced build was destroyed rather than kept, so "
                        "the stories it carried exist nowhere:\n%s" % out)
        kept = "".join((self.tmp / "state" / "digest-archive" / n).read_text(
            encoding="utf-8") for n in self.archived())
        self.assertIn("2026-10-13 18:44", kept,
                      "the archived copy is not the older build:\n%s" % out)

    def test_a_newer_build_takes_the_url_and_still_keeps_what_it_displaced(self):
        out = self.run_archive_step("2026-10-13 18:44", "2026-10-13 19:24")
        served = self.target.read_text(encoding="utf-8")
        self.assertIn("2026-10-13 19:24", served,
                      "the newer build did not take the URL:\n%s" % out)
        self.assertTrue(self.archived(),
                        "the build this one displaced was destroyed:\n%s" % out)

    def test_an_undateable_page_is_never_overwritten(self):
        """A parse failure must not be permission to erase a published page.

        The page already published is the one the owner may have open. If the
        footer cannot be read we do not know which build is newer, and the only
        safe answer is to keep what is there.
        """
        backup = self.tmp / "state" / (".page-backup-%s-%s.html" % (TODAY, SLOT))
        backup.write_text("<html>no footer here</html>", encoding="utf-8")
        self.target.write_text(self.page("2026-10-13 18:44", "new"), encoding="utf-8")
        env = {"TARGET_PAGE": str(self.target), "BACKUP_PAGE": str(backup),
               "ARCHIVE_DIR": str(self.tmp / "state" / "digest-archive"),
               "TODAY": TODAY, "DIGEST_TIME": SLOT}
        body = last_heredoc(RENDER_STEP, min_index=0)
        saved = dict(os.environ)
        cwd = os.getcwd()
        buf = io.StringIO()
        try:
            os.chdir(self.tmp)
            os.environ.update({k: str(v) for k, v in env.items()})
            with contextlib.redirect_stdout(buf):
                # `sys.exit(0)` is how this heredoc finishes a branch on purpose
                # -- "this build owns the URL", "REFUSED, the previously
                # published page is back in place" -- so it is caught and
                # printed, exactly as `exec_step()` does above. Letting it
                # escape is not a failure signal; it is the step saying it is
                # done, and `run_archive_step` docstring above names that.
                try:
                    exec(compile(body, "<render-archive>", "exec"),
                         {"__name__": "render_archive"})
                except SystemExit as exc:
                    print("render_archive exited %s" % exc.code)
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        out = buf.getvalue()
        self.assertIn("no footer here",
                      self.target.read_text(encoding="utf-8"),
                      "a page this step could not date was replaced anyway:\n%s"
                      % out)
        self.assertIn("REFUSED", out, out)

    def test_the_index_is_regenerated_after_a_swap(self):
        """render_index scraped the page that is no longer there otherwise.

        `render_digest.py` regenerates the index right after writing the page it
        wrote. Every path that puts the older page back therefore leaves an
        index whose story counts and thumbnail came from a page that has since
        been replaced — the same class of lie, one level up.
        """
        out = self.run_archive_step("2026-10-13 19:24", "2026-10-13 18:44")
        index = (self.tmp / "docs" / "index.html")
        self.assertTrue(index.is_file(), "no index was written:\n%s" % out)
        self.assertIn("index regenerated", out,
                      "the index was not regenerated after the swap, so it "
                      "describes a page that is no longer there:\n%s" % out)

    def test_the_archive_index_pattern_still_matches_the_page(self):
        """The reason a suffixed filename was rejected, asserted rather than claimed."""
        pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})-(morning|evening)\.html$")
        self.run_archive_step("2026-10-13 19:24", "2026-10-13 18:44")
        self.assertIsNotNone(
            pattern.match(self.target.name),
            "render_index would skip %r, so the page the owner opens is not in "
            "the archive index he browses" % self.target.name)


# ==========================================================================
# 5. THE ARCHIVE SCRAPERS STILL WORK AGAINST THE OUTPUT
# ==========================================================================


class ScraperCompatibilityTests(ArchiveIntegrityTests):
    """`parse_digest_categories` and `first_thumbnail` are load-bearing.

    `render_index` calls both for every page, and `digest-prebuild.yml`'s seal
    asserts the same markup on the page it hashes. A page that stopped carrying
    those selectors would empty its own index row and fail the seal — so the
    swap must not produce a page they cannot read.

    THE REAL MODULE IS LOADED FROM AN EXPLICIT PATH, under its own name. The
    base class puts a STUB `scripts/render_digest.py` in the temp tree so the
    archive step's own `import render_digest` resolves there; importing that
    stub by name here would either measure the stub (vacuous) or collide with
    whatever the module cache already holds (order-dependent).
    """

    def setUp(self):
        super().setUp()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "real_render_digest_%d" % id(self),
            str(Path(_ROOT) / "scripts" / "render_digest.py"))
        self.rd = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(Path(_ROOT) / "scripts"))
        self.addCleanup(lambda: sys.path.remove(str(Path(_ROOT) / "scripts")))
        spec.loader.exec_module(self.rd)

    #: What `parse_digest_categories` actually returns for `SECTION`, asserted
    #: against the real map rather than against a remembered string. The
    #: function maps the display header to its SHORT LABEL
    #: (`SECTION_TO_LABEL[scripts/render_digest.py:1098]`), so the world's key is
    #: `"world"` and never `"WORLD"` -- `"WORLD"` is the header half of the
    #: mapping, not the label half. The same convention is pinned by
    #: tests/render/test_render_readability.py:324.
    EXPECTED_COUNTS = {"world": 2}

    def test_the_served_page_still_yields_categories_and_a_thumbnail(self):
        """The page the owner is SERVED is the 19:24 build, so it says "old".

        The refusal put the newer page back, which
        `test_an_older_build_cannot_overwrite_a_newer_page` above already pins by
        asserting the served page contains "2026-10-13 19:24" and the marker
        "old". This test used to expect marker "new" here, which contradicts
        that one and is not reachable: the marker is a property of the BUILD,
        and the build that survives is the older-named fixture holding the
        newer instant. Asserting the thumb of BOTH pages is what pins the swap --
        one name per build, two readable builds, neither silently swapped.
        """
        out = self.run_archive_step("2026-10-13 19:24", "2026-10-13 18:44")
        counts = self.rd.parse_digest_categories(str(self.target))
        self.assertEqual(self.EXPECTED_COUNTS, counts,
                         "the archive scraper could not count the stories on the "
                         "page the owner is served, so its index row would be "
                         "empty:\n%s" % out)
        thumb = self.rd.first_thumbnail(str(self.target))
        self.assertEqual("https://img.invalid/old.png", thumb,
                         "the archive scraper found no thumbnail on the page "
                         "the owner is served, or found the DISPLACED build's\n%s"
                         % out)
        archived = self.archived()
        self.assertEqual(1, len(archived), out)
        kept = self.tmp / "state" / "digest-archive" / archived[0]
        self.assertEqual("https://img.invalid/new.png",
                         self.rd.first_thumbnail(str(kept)),
                         "the archived build is not the 18:44 build, so the "
                         "stories it carried are NOT the ones kept:\n%s" % out)
        self.assertIn("2026-10-13 18:44", kept.read_text(encoding="utf-8"), out)

    def test_both_builds_are_readable_by_the_scrapers(self):
        out = self.run_archive_step("2026-10-13 19:24", "2026-10-13 18:44")
        archived = self.archived()
        self.assertTrue(archived, out)
        for name in archived:
            path = self.tmp / "state" / "digest-archive" / name
            self.assertEqual(self.EXPECTED_COUNTS,
                             self.rd.parse_digest_categories(str(path)),
                             "an archived build cannot be read by the scrapers, so "
                             "the only copy of its stories is unreachable:\n%s"
                             % out)
            self.assertTrue(self.rd.first_thumbnail(str(path)), name)

    def test_the_real_render_index_would_list_the_page(self):
        """The reason a suffixed filename was rejected, executed not asserted.

        `render_index` is run against the real temp tree, so the claim "a
        suffixed page would vanish from the index" is measured by the index
        itself rather than by a regex copied out of a comment.
        """
        out = self.run_archive_step("2026-10-13 19:24", "2026-10-13 18:44")
        out_dir = self.tmp / "docs" / "digests"
        index = self.tmp / "docs" / "index-real.html"
        self.assertTrue(
            self.rd.render_index(str(out_dir), str(index), TODAY, "19:24"),
            "render_index refused the page:\n%s" % out)
        html = index.read_text(encoding="utf-8")
        self.assertIn(self.target.name, html,
                      "render_index did not list the page the owner is served, "
                      "so the archive the owner browses is missing it:\n%s" % out)
        # And the suffixed name the alternative would have used is invisible.
        suffixed = out_dir / ("%s-%s-1844.html" % (TODAY, SLOT))
        suffixed.write_text("<html></html>", encoding="utf-8")
        self.rd.render_index(str(out_dir), str(index), TODAY, "19:24")
        self.assertNotIn(
            suffixed.name, index.read_text(encoding="utf-8"),
            "render_index listed a build-suffixed page after all. If that ever "
            "changes, the reason this design keeps one stable URL per slot has "
            "gone with it and the trade-off should be re-opened deliberately.")


# ==========================================================================
# 6. THE EMAIL ARCHIVE HAD THE SAME COLLISION
# ==========================================================================


class EmailArchiveTests(ArchiveIntegrityTests):
    def test_two_builds_of_one_slot_keep_two_archived_emails(self):
        """`${DIGEST_TIME}-${TODAY}.html` is one name per (slot, date).

        On 2026-10-13 the 19:40 re-fire `mv`-d its email over the 19:25 one, so
        the archive lost the build at the same moment the duplicate push landed.
        Nothing reads these by name — they are a retained copy — so the fix is
        to stop destroying.
        """
        body = last_heredoc(ARCHIVE_STEP)
        self.assertNotIn(
            'state/digest-archive/${DIGEST_TIME}-${TODAY}.html', step_run(ARCHIVE_STEP),
            "the archive step still moves the email onto one name per "
            "(slot, date), which is the collision")
        saved = dict(os.environ)
        cwd = os.getcwd()
        names = []
        for built in ("2026-10-13 18:44", "2026-10-13 19:24"):
            (self.tmp / "state" / "email.html").write_text(
                self.page(built, "mail"), encoding="utf-8")
            buf = io.StringIO()
            os.environ.update({"ARCHIVE_DIR": str(self.tmp / "state" / "digest-archive"),
                               "DIGEST_TIME": SLOT, "TODAY": TODAY})
            try:
                os.chdir(self.tmp)
                with contextlib.redirect_stdout(buf):
                    exec(compile(body, "<email-archive>", "exec"),
                         {"__name__": "email_archive"})
            finally:
                os.chdir(cwd)
            names.append(sorted(p.name for p in
                                (self.tmp / "state" / "digest-archive").glob("*.html")))
        os.environ.clear()
        os.environ.update(saved)
        self.assertNotEqual(names[0], names[1],
                            "the second build of the slot overwrote the first "
                            "build's archived email: %r" % names)
        self.assertEqual(2, len(names[1]),
                         "two builds of one slot produced %d archived emails:\n%s"
                         % (len(names[1]), names[1]))


# ==========================================================================
# 7. MUTATION PROOF — each guard reverted in a TEMP COPY must go RED
# ==========================================================================


class MutationProofTests(unittest.TestCase):
    """A test never seen RED is decoration.

    Each case copies the repo to a temp directory, reverts ONE fix there, and
    requires the suite to fail. The real repository is never modified.
    """

    def _revert_in_copy(self, *args, **kw):
        """Copy the repo aside, revert ONE fix there, return the copy.

        The real repository is never modified. Two call shapes, because one
        revert genuinely needs more than one edit: a parse hoisted out of a
        `try` is restored by removing it from the `try` AND putting it back in
        an `else:`, and deleting only the first raises UnboundLocalError, which
        would prove a different thing.

            _revert_in_copy(old, new, target_rel)        # one edit
            _revert_in_copy(((old1, new1), (old2, new2)), target_rel)

        `mutate=` runs against the finished copy before it is handed back.
        An unrecognised keyword RAISES rather than being ignored: a silently
        dropped option here would leave a proof reverting less than it claims
        to, which is the failure this class exists to make impossible.
        """
        mutate = kw.pop("mutate", None)
        if kw:
            raise TypeError("_revert_in_copy got unknown keyword(s) %s"
                            % sorted(kw))
        if len(args) == 3:
            # (old, new, target_rel)
            pairs, target_rel = ((args[0], args[1]),), args[2]
        elif len(args) == 2:
            # EITHER a bare (old, new) pair, OR a tuple of such pairs, then the
            # path. Both are 2-tuples of strings, so the two are told apart by
            # what the first element is made of.
            first, target_rel = args
            if (isinstance(first, tuple) and len(first) == 2
                    and all(isinstance(x, str) for x in first)):
                pairs = (first,)
            else:
                pairs = tuple(first)
        else:
            raise TypeError(
                "_revert_in_copy takes (old, new, target_rel) or "
                "((old, new), ...), target_rel; got %d positional args" % len(args))
        src = Path(_ROOT)
        tmp = Path(tempfile.mkdtemp(prefix="dmn-mutation-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("deploy", ".github", "scripts", "tests", "templates",
                     "functions"):
            source = src / name
            if source.is_dir():
                shutil.copytree(source, tmp / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
        target = tmp / target_rel
        text = target.read_text(encoding="utf-8")
        for one_old, one_new in pairs:
            self.assertEqual(1, text.count(one_old),
                             "the mutation target %r matched %d times in %s, "
                             "not once. The workflow has drifted from the text "
                             "this proof was written against."
                             % (one_old[:60], text.count(one_old), target_rel))
            text = text.replace(one_old, one_new)
        target.write_text(text, encoding="utf-8")
        if mutate:
            mutate(tmp)
        return tmp

    def _run_child(self, tmp, dotted, env=None):
        return subprocess.run(
            [sys.executable, "-m", "unittest", dotted],
            cwd=str(tmp), capture_output=True, text=True, errors="replace",
            env=env or dict(os.environ), timeout=900)

    def test_reverting_the_ndjson_parse_brings_back_the_jsondecodeerror(self):
        """THE PROOF THIS FILE EXISTS FOR.

        Restoring `json.loads(whole_body)` must make the suppression test fail
        with the real exception class and the real log line from the incident.
        """
        tmp = self._revert_in_copy(
            "readback = parse_ntfy_history(readback_raw)",
            "readback = json.loads(readback_raw)",
            ".github/workflows/daily-news.yml")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest",
             "tests.digest.test_duplicate_detector.ReadbackSuppressesTests"],
            cwd=str(tmp), capture_output=True, text=True, errors="replace",
            timeout=900)
        self.assertNotEqual(
            0, proc.returncode,
            "reverting the NDJSON parse left the suite GREEN. The guard has "
            "never suppressed anything and this proof is not watching it.")
        blob = proc.stdout + proc.stderr
        self.assertIn(
            "JSONDecodeError", blob,
            "the revert did not reproduce the real failure. The incident log "
            "was `the sequence_id read-back failed (JSONDecodeError) - "
            "publishing anyway`.\n%s" % blob[-4000:])

    def test_reverting_the_kv_write_makes_the_handoff_fail(self):
        tmp = self._revert_in_copy(
            "kv_record_delivery(today, kind, {\n                                  'ntfy_sent_at': sent_at,",
            "_unused_suppress = (today, kind, {\n                                  'ntfy_sent_at': sent_at,",
            ".github/workflows/daily-news.yml")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest",
             "tests.digest.test_duplicate_detector.LedgerHandoffTests"],
            cwd=str(tmp), capture_output=True, text=True, errors="replace",
            timeout=900)
        self.assertNotEqual(
            0, proc.returncode,
            "reverting the KV record write left the suite GREEN, so nothing is "
            "holding the two arms together.")
        blob = proc.stdout + proc.stderr
        # THE REASON MARKER IS THE FAILING TEST'S OWN NAME, not a field name.
        # Asserting on "ntfy_sent_at" proved nothing: the copy fails first at
        # `assertTrue(self.kv_puts)`, whose reported source line is
        # `self.assertTrue(self.kv_puts,` and whose message never mentions the
        # field -- so the proof went red for an unrelated wording change. The
        # name is what says WHICH behaviour broke.
        self.assertIn(
            "test_a_successful_push_records_the_delivery_in_kv", blob,
            "the revert did not fail for the reason it was written -- the test "
            "that names the missing KV write did not fail:\n%s" % blob[-4000:])
        self.assertIn(
            "NOTHING was written to the KV delivery ledger", blob,
            "the KV write test failed for a reason other than the missing "
            "write:\n%s" % blob[-4000:])

    def test_reverting_the_full_sequence_id_makes_the_supersede_fail(self):
        tmp = self._revert_in_copy(
            "push_seq = slot_seq + '-full' if is_supersede else slot_seq",
            "push_seq = slot_seq",
            ".github/workflows/daily-news.yml")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest",
             "tests.digest.test_duplicate_detector.SupersedeSequenceIdTests"],
            cwd=str(tmp), capture_output=True, text=True, errors="replace",
            timeout=900)
        self.assertNotEqual(
            0, proc.returncode,
            "reverting the `-full` sequence id left the suite GREEN. That id is "
            "the entire reason the owner's read notification was replaced.")
        self.assertIn("-full", proc.stdout + proc.stderr,
                      "the revert did not fail for the reason it was written")

    def test_reverting_the_fail_open_placement_brings_back_a_silent_miss(self):
        """TWO EDITS, because this is a PLACEMENT revert and not a deletion.

        The 2026-10-13 refactor moved `parse_ntfy_history` out of the read-back
        `try` into an `else:` clause. That is correct-looking code and it is a
        MISS: an unparseable body now escapes `main()`, hits the FATAL handler,
        writes ntfy_ok=false and publishes nothing. Restoring the 2026-10-13
        shape in a copy must therefore fail `ReadbackSuppressesTests` on the
        fail-OPEN case, with the FATAL line in the output -- a digest lost
        silently, which is the one outcome the delivery contract forbids.
        """
        tmp = self._revert_in_copy(
            (("                  readback = parse_ntfy_history(readback_raw)\n"
              "              except Exception as exc:\n"
              "                  # Fail OPEN, deliberately.",
              "              except Exception as exc:\n"
              "                  # Fail OPEN, deliberately."),
             ("                        'cost' % exc.__class__.__name__)\n"
               "              if isinstance(readback, list):",
               "                        'cost' % exc.__class__.__name__)\n"
               "              else:\n"
               "                  readback = parse_ntfy_history(readback_raw)\n"
               "              if isinstance(readback, list):")),
            ".github/workflows/daily-news.yml")
        proc = self._run_child(
            tmp, "tests.digest.test_duplicate_detector.ReadbackSuppressesTests")
        self.assertNotEqual(
            0, proc.returncode,
            "hoisting the parse out of the try left the suite GREEN. A body this "
            "step cannot parse would then cost the owner the digest outright.")
        blob = proc.stdout + proc.stderr
        self.assertIn(
            "FATAL: unexpected ntfy step error", blob,
            "the revert did not fail by turning a parse failure into a silent "
            "miss, which is the whole shape of it.\n%s" % blob[-4000:])
        self.assertIn(
            "test_a_body_that_is_neither_shape_still_publishes", blob,
            "the malformed-body test is not the one that failed, so the suite is "
            "red for some other reason and this proof proves nothing.\n%s"
            % blob[-4000:])

    def test_reverting_the_archive_order_destroys_the_displaced_build(self):
        """The 18:44 loss, exactly.

        The refusal branch copied the winner back over the target and archived
        AFTERWARDS, so it archived the build it had just restored under the
        loser's name and destroyed the one it was supposed to keep. Restoring
        that order in a copy must fail `ArchiveIntegrityTests` on the displaced
        build, not on anything cosmetic.
        """
        tmp = self._revert_in_copy(
            ("              archive(target, 'stale-build')\n"
             "              shutil.copyfile(backup, target)\n"
             "              regenerate_index()\n",
             "              shutil.copyfile(backup, target)\n"
             "              regenerate_index()\n"
             "              archive(target, 'stale-build')\n"),
            ".github/workflows/daily-news.yml")
        proc = self._run_child(
            tmp, "tests.digest.test_duplicate_detector.ArchiveIntegrityTests")
        self.assertNotEqual(
            0, proc.returncode,
            "archiving after the copy left the suite GREEN, so the displaced "
            "build could be destroyed without anything noticing.")
        blob = proc.stdout + proc.stderr
        self.assertIn(
            "test_no_build_of_a_slot_is_destroyed", blob,
            "the displaced-build test is not the one that failed.\n%s"
            % blob[-4000:])

    def test_reverting_the_archive_makedirs_silently_loses_every_build(self):
        """The retention guarantee must not depend on a `mkdir` elsewhere.

        `archive()` warned and moved on when its destination directory was
        absent, which is a silent loss dressed as a log line. Measured on
        2026-10-13:
            WARNING: could not archive the displaced stale-build page
            ([Errno 2] No such file or directory: .../digest-archive/...)
        """
        tmp = self._revert_in_copy(
            ("              try:\n"
             "                  os.makedirs(os.environ['ARCHIVE_DIR'], exist_ok=True)\n"
             "                  stamp = (built_at(page) or 'unknown')",
             "              try:\n"
             "                  stamp = (built_at(page) or 'unknown')"),
            ".github/workflows/daily-news.yml")
        proc = self._run_child(
            tmp, "tests.digest.test_duplicate_detector.ArchiveIntegrityTests")
        self.assertNotEqual(
            0, proc.returncode,
            "without its own makedirs, archive() silently loses every displaced "
            "build and the suite stayed GREEN.")
        self.assertIn(
            "could not archive the displaced", proc.stdout + proc.stderr,
            "the revert did not fail through the warning it was written "
            "against.\n%s" % (proc.stdout + proc.stderr)[-4000:])

    def test_reverting_the_email_archive_makedirs_loses_the_build(self):
        """Same guarantee, second heredoc: `shutil.move` into a missing directory."""
        tmp = self._revert_in_copy(
            ("          os.makedirs(os.environ['ARCHIVE_DIR'], exist_ok=True)\n"
             "          # UNDATED IS STILL ARCHIVED",
             "          # UNDATED IS STILL ARCHIVED"),
            ".github/workflows/daily-news.yml")
        proc = self._run_child(
            tmp, "tests.digest.test_duplicate_detector.EmailArchiveTests")
        self.assertNotEqual(
            0, proc.returncode,
            "without its own makedirs the email archive step dies on "
            "FileNotFoundError and the suite stayed GREEN.\n%s"
            % (proc.stdout + proc.stderr)[-3000:])
        self.assertIn(
            "test_two_builds_of_one_slot_keep_two_archived_emails",
            proc.stdout + proc.stderr,
            "the collision test is not the one that failed.\n%s"
            % (proc.stdout + proc.stderr)[-3000:])

    def test_reverting_the_worker_ndjson_parse_blinds_the_worker_guard(self):
        """GUARD 1 ON THE WORKER SIDE, AND THE REGISTRATION IS NOT DECORATION.

        `DIGEST_DELIVERY_WORKER` is the same switch
        tests/digest/test_digest_delivery.py documents, so the committed Worker
        is never touched: the copy's own `deploy/cloudflare-worker.js` is judged
        instead. If the registered JS suite could not go RED here, then
        registering it proves nothing -- which is the failure this whole file
        exists to prevent.
        """
        tmp = self._revert_in_copy(
            ("    const parsed = parseNtfyHistory(await res.text());\n"
             "    messages = Array.isArray(parsed) ? parsed : [];",
             "    messages = JSON.parse(await res.text());"),
            "deploy/cloudflare-worker.js")
        worker = tmp / "deploy" / "cloudflare-worker.js"
        env = dict(os.environ, DIGEST_DELIVERY_WORKER=str(worker))
        proc = self._run_child(
            tmp, "tests.digest.test_duplicate_detector.WorkerSideReadbackTests",
            env=env)
        self.assertNotEqual(
            0, proc.returncode,
            "restoring `JSON.parse(await res.text())` in the Worker left the "
            "registered JS suite GREEN. The duplicate detector is blind again "
            "and the registration is decoration.")
        self.assertIn(
            "the 2026-10-13 evening duplicate is SUPPRESSED end to end",
            proc.stdout + proc.stderr,
            "the revert did not fail through the suppression test it was "
            "written against.\n%s" % (proc.stdout + proc.stderr)[-4000:])


# ==========================================================================
# 8. THE WORKER-SIDE HALF, REGISTERED
# ==========================================================================
#
# WHY THIS FILE IS INVOKED FROM HERE AND NOT LEFT ALONE
# ----------------------------------------------------
# `tests/digest/digest_duplicate_detector.test.js` existed with ZERO references
# anywhere in the repository, so it never ran: not in `tests/ci_local.py`, not in
# any workflow's `run_suite` block, not from any Python module. It is Guard 1
# measured on the WORKER side, which is the half this Python file cannot reach --
# the Actions arm is exec'd here, and the Worker is JavaScript loaded and
# executed against a fake KV, a fake fetch and a fake clock. A test that never
# executes is decoration, and commit 9eb1316 named the file as one of three it
# believed were unregistered.
#
# THE CONVENTION IS THE ONE THIS REPOSITORY ALREADY USES. See
# tests/digest/test_digest_delivery.py: a `node --test` JS suite reached from a
# `python3 -m unittest` entry in the CI inventory, because every entry of
# `drift-canary.yml`'s `test-suite` job is a `python3 -m unittest` invocation and
# that job has no node setup step. Same reason, same shape, same demand that a
# MISSING node be LOUD.
#
# `worker_sees_actions_ledger.test.js` needs a ledger handed to it by the
# Actions run above and is invoked from `LedgerHandoffTests`; this one is
# self-contained, so it is invoked from here.

WORKER_SIDE_JS = Path(_TESTS) / "digest" / "digest_duplicate_detector.test.js"

#: The behaviours the design mandates, as the JS suite's own test names. Used
#: only to prove the suite cannot quietly become empty -- never to judge
#: behaviour, which is the JS runner's job.
REQUIRED_WORKER_SIDE_BEHAVIOURS = (
    "a ZERO-BYTE history is an empty history, not a parse failure",
    "NDJSON with the sequence id in it IS found",
    "NDJSON without the sequence id is read and reports nothing found",
    "a single NDJSON line is found",
    "a single pretty-printed JSON array is still read",
    'a {"messages": [...]} body is still read',
    "a genuinely unparseable body stays UNKNOWN and still pushes",
    "an HTTP failure is UNKNOWN, never a definite not-found",
    "a transport failure is UNKNOWN",
    "the read-back keeps poll=1, or it becomes an unbounded long-poll",
    "the 2026-10-13 evening duplicate is SUPPRESSED end to end",
    "the HAPPY PATH still pushes, so the test above is not vacuous",
    "a topic carrying a DIFFERENT slot still publishes",
    "an unreadable read-back still PUSHES -- a miss is the worse outcome",
    "the topic value never appears in the read-back's result",
)

#: node's own TAP plan line. The only number in the output that cannot go stale.
TAP_PLAN_RE = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)


class WorkerSideReadbackTests(unittest.TestCase):
    """Run the JS suite that measures Guard 1 on the WORKER's own code.

    `test_duplicate_detector.py` above execs the Actions step and can prove that
    the Actions arm suppresses. It cannot prove the Worker does, and the Worker
    is where the 19:40 push came from. `findDeliveredBySequenceId` is reached
    here through a real `deliverDigest` call, so the assertion is whether a POST
    left the process -- not whether a helper returns a particular value.
    """

    def _node(self):
        node = shutil.which("node")
        if not node:
            self.fail(
                "node is required to execute the WORKER-side half of the "
                "duplicate detector and was not found on PATH. This FAILS rather "
                "than skips on purpose: a skipped duplicate guard is a green CI "
                "run with no duplicate guard, and this guard has already been "
                "dead once while reporting nothing. Install node, or put it on "
                "PATH -- do not delete the test.")
        return node

    def test_the_worker_side_suite_passes_against_the_committed_worker(self):
        node = self._node()
        proc = subprocess.run(
            # TAP, explicitly, because the plan line ("1..N") is the only
            # machine-readable statement of how many tests ran. Without it, a
            # suite that printed nothing and exited 0 is indistinguishable here
            # from a suite that ran nothing.
            [node, "--test", "--test-reporter=tap", str(WORKER_SIDE_JS)],
            cwd=str(_ROOT), capture_output=True, text=True, errors="replace",
            timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(
            0, proc.returncode,
            "tests/digest/digest_duplicate_detector.test.js FAILED against %s.\n"
            "This is the WORKER half of the 2026-10-13 duplicate delivery: the "
            "read-back that decides whether `deliverDigest` publishes at all. If "
            "it is blind again, the re-fire cron publishes over the top of the "
            "notification the owner has already read.\n"
            "---- node output ----\n%s\n------------------"
            % (os.environ.get("DIGEST_DELIVERY_WORKER")
               or "the committed deploy/cloudflare-worker.js", output))
        plan = TAP_PLAN_RE.search(proc.stdout or "")
        self.assertIsNotNone(
            plan,
            "node's own TAP plan line was absent, so this module cannot confirm "
            "the suite RAN anything. A wrapper that passes because the runner "
            "printed nothing is decoration.\n---- node output ----\n%s" % output)
        self.assertGreaterEqual(
            int(plan.group(1)), len(REQUIRED_WORKER_SIDE_BEHAVIOURS),
            "node's TAP plan says the suite ran only %s test(s), but %d mandated "
            "behaviours must be covered. Tests were probably deleted.\n%s"
            % (plan.group(1), len(REQUIRED_WORKER_SIDE_BEHAVIOURS), output))

    def test_the_worker_side_suite_still_names_every_mandated_behaviour(self):
        """A suite that can lose a test without noticing is not a suite.

        Structural on purpose: it reads the committed test names and asserts each
        mandated behaviour is still named. It does not decide whether they pass
        -- that is the test above.
        """
        self.assertTrue(WORKER_SIDE_JS.is_file(),
                        "%s is missing" % WORKER_SIDE_JS)
        text = WORKER_SIDE_JS.read_text(encoding="utf-8")
        missing = [b for b in REQUIRED_WORKER_SIDE_BEHAVIOURS if b not in text]
        self.assertEqual(
            [], missing,
            "tests/digest/digest_duplicate_detector.test.js no longer names %s. "
            "Each is a mutation-proved behaviour of the Worker's duplicate "
            "guard; deleting one must be a deliberate act recorded in the "
            "report, never a silent removal." % missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)