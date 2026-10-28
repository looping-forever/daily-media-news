#!/usr/bin/env python3
"""tests/academic/test_notification_classes.py — WHICH KIND is this?

THE PROBLEM. On a collapsed ntfy notification the owner sees a title and a few
tags. Before this module:

    morning digest          priority 4   tags ['newspaper']
    non-CRITICAL breaking   priority 4   tags ['warning']
    RESULT-path self-check  priority 4   tags ['warning', 'mag']

Priority 4 was shared by a routine digest, an alert and an honesty-gate notice,
and ``warning`` was shared by alerts and gate notices. On a lock screen only the
title text told them apart -- and the title is the first thing that gets
truncated.

THE FIX, AND WHY IT IS A LITERAL. Every notification class gets ONE constant tag
from ``scripts/ntfy_classes.py``. It is never assembled from the notification's
own content, so no model and no fetched page can relabel an honesty signal. That
is the repo rule: the AI must never regain control of an honesty signal.

THE SHAPE IS THE PART THAT MUST NEVER REGRESS. ntfy answers a JSON STRING in
``Priority`` or ``Tags`` with HTTP 400 / code 40024. That is measured, recorded
at ``deploy/cloudflare-worker.js``, and it took production down on 2026-10-12.
So ``wire_shape`` is the one function the Python sites go through, and the tests
below assert on the SERIALISED bytes -- because the failure mode is invisible in
Python (a list serialises fine, and so does a string) and only appears on the
wire.

Offline throughout. The Worker test shells out to ``node`` on the committed
source, which is present on this machine; it skips rather than fails if node is
missing, because a missing toolchain is not a broken contract.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import check_academic_result as car  # noqa: E402
import ntfy_classes  # noqa: E402

WORKER_PATH = os.path.join(REPO_ROOT, "deploy", "cloudflare-worker.js")
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows",
                             "academic-result.yml")


def assert_wire_shape(case, body, why):
    """Assert the POST body can never be the shape ntfy answers with HTTP 400.

    This is the RED case, pinned. Everything else in this file is about which
    tag a notification carries; this is about whether it arrives at all.
    """
    encoded = json.dumps(body)
    case.assertIsInstance(body.get("tags"), list,
                          "%s: tags must be a list, got %r" % (why, type(body.get("tags"))))
    for tag in body.get("tags") or []:
        case.assertIsInstance(tag, str,
                              "%s: every tag must be a string, got %r"
                              % (why, tag))
    case.assertNotIsInstance(body.get("priority"), bool,
                             "%s: priority must not be a bool" % why)
    case.assertIsInstance(body.get("priority"), int,
                          "%s: priority must be a NUMBER, got %r"
                          % (why, body.get("priority")))
    # The literal-substring check is what a string would fail, and it is the
    # exact distinction the 2026-10-12 outage turned on.
    case.assertNotIn('"tags": "', encoded,
                     "%s: tags serialised as a STRING -> HTTP 400" % why)
    case.assertNotIn('"priority": "', encoded,
                     "%s: priority serialised as a STRING -> HTTP 400" % why)


class WireShapeContractTest(unittest.TestCase):
    """The 400 must be unrepresentable through the helper."""

    def test_the_happy_path_really_happens(self):
        body = ntfy_classes.wire_shape("t", "Title", "msg",
                                       ntfy_classes.DIGEST, ["newspaper"], 4)
        self.assertEqual(body["tags"], ["digest", "newspaper"])
        self.assertEqual(body["priority"], 4)
        self.assertEqual(body["topic"], "t")

    def test_a_string_tag_is_coerced_not_passed_through(self):
        body = ntfy_classes.wire_shape("t", "T", "m", ntfy_classes.BREAKING,
                                       "warning", 5)
        assert_wire_shape(self, body, "a bare string extra tag")
        self.assertEqual(body["tags"], ["breaking", "warning"])

    def test_a_string_priority_becomes_a_number(self):
        body = ntfy_classes.wire_shape("t", "T", "m", ntfy_classes.RESULT,
                                       ["x"], "4")
        assert_wire_shape(self, body, "a string priority")
        self.assertEqual(body["priority"], 4)
        self.assertIs(type(body["priority"]), int)

    def test_a_nonsense_priority_falls_back_rather_than_400ing(self):
        for junk in (None, "high", "", [], 99):
            body = ntfy_classes.wire_shape("t", "T", "m",
                                           ntfy_classes.PIPELINE, [], junk)
            assert_wire_shape(self, body, "priority=%r" % (junk,))
            self.assertEqual(body["priority"], 4)

    def test_class_tags_are_deduplicated_and_ordered(self):
        tags = ntfy_classes.with_class_tags(ntfy_classes.RESULT,
                                            ["result", "🎓", "result"])
        self.assertEqual(tags, ["result", "🎓"])

    def test_every_class_tag_is_one_of_the_four_declared(self):
        self.assertEqual(set(ntfy_classes.CLASS_TAGS),
                         {"digest", "breaking", "result", "pipeline"})


class ResultPayloadCarriesItsClass(unittest.TestCase):
    """check_academic_result._build_payload is driven directly -- real code."""

    def _payload(self, passed):
        state = {"roll_number": "13572468", "current_semester": 4,
                 "semesters": {}}
        notice = {"id": 15700, "title": "B.Sc.CSIT I Semester Exam Result",
                  "pdf_url": "https://portal.tu.edu.np/p/1.pdf",
                  "url": "https://iost.tu.edu.np/notices/15700"}
        return car._build_payload(state, 1, notice, passed, "2026-10-14")

    def test_a_passed_result_is_tagged_result(self):
        body = self._payload(True)["ntfy"]
        assert_wire_shape(self, body, "result PASSED payload")
        self.assertEqual(body["tags"][0], ntfy_classes.RESULT)
        # The pre-existing emoji tag is preserved, not replaced.
        self.assertIn("🎓", body["tags"])

    def test_a_failed_result_is_tagged_result(self):
        body = self._payload(False)["ntfy"]
        assert_wire_shape(self, body, "result FAILED payload")
        self.assertEqual(body["tags"][0], ntfy_classes.RESULT)

    def test_a_result_no_longer_shares_a_shape_with_the_digest(self):
        """THE ACTUAL DEFECT. These two used to be indistinguishable."""
        result = self._payload(True)["ntfy"]
        self.assertEqual(result["tags"][0], ntfy_classes.RESULT)
        self.assertNotEqual(result["tags"][0], ntfy_classes.DIGEST)


class SelfcheckWireShapeTest(unittest.TestCase):
    """The self-check's own POST body, captured off the real code path."""

    def _posted(self):
        import selfcheck_exam_sources as scs
        sent = {}

        class _Resp(object):
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=15):
            sent["body"] = req.data
            return _Resp()

        env = {"NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.invalid",
               "PATH": os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(scs.urllib.request, "urlopen", fake_urlopen):
            ok = scs.notify_ntfy("T", "M", False,
                                 class_tag=ntfy_classes.RESULT)
        self.assertTrue(ok, "the send was attempted -- control for the "
                            "assertions below")
        self.assertIn("body", sent)
        return json.loads(sent["body"].decode("utf-8"))

    def test_the_result_alert_is_posted_as_an_array_and_a_number(self):
        body = self._posted()
        assert_wire_shape(self, body, "self-check result alert")
        self.assertEqual(body["tags"][0], ntfy_classes.RESULT)

    def test_the_raw_bytes_never_carry_a_quoted_tags_field(self):
        import selfcheck_exam_sources as scs
        sent = {}

        class _Resp(object):
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.dict(os.environ, {
                "NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.invalid"},
                clear=True), \
                mock.patch.object(scs.urllib.request, "urlopen",
                                  lambda req, timeout=15: (sent.setdefault(
                                      "raw", req.data), _Resp())[1]):
            scs.notify_ntfy("T", "M", False, class_tag=ntfy_classes.PIPELINE)
        raw = sent["raw"].decode("utf-8")
        self.assertNotIn('"tags": "', raw)
        self.assertIn('"tags": [', raw)
        self.assertIn('"priority": 4', raw)


class PipelineAndBreakingSitesTest(unittest.TestCase):
    """The two remaining Python sites, driven with their transports mocked."""

    def test_the_canary_is_tagged_pipeline(self):
        import live_canary as lc
        captured = {}

        class _Resp(object):
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        with mock.patch.dict(os.environ, {
                "NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.invalid"},
                clear=True), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            lc.notify_ntfy("CANARY DEAD", "msg", priority=5, dry_run=False)
        self.assertIn("body", captured)
        assert_wire_shape(self, captured["body"], "live-canary alert")
        self.assertEqual(captured["body"]["tags"][0], ntfy_classes.PIPELINE)
        self.assertIn("canary", captured["body"]["tags"])

    def test_a_breaking_alert_is_tagged_breaking(self):
        import argparse
        import io
        import shutil
        import tempfile

        import breaking_monitor as bm
        captured = {}

        def fake_send(title, message, priority, tags, click="", http_fn=None):
            captured["priority"] = priority
            captured["tags"] = tags
            return True, None

        tmp = tempfile.mkdtemp()
        try:
            paths = {"ntfy_preview": os.path.join(tmp, "p.txt"),
                     "breaking_delivery": os.path.join(tmp, "d.json")}
            args = argparse.Namespace(dry_run=False)
            # The alert title carries an emoji, and this box's stdout is cp1252,
            # so the module's own confirmation print would raise. Capture it:
            # the test is about the tags, not about console encoding.
            with mock.patch.object(bm, "ntfy_configured", lambda: (True, "ok")), \
                    mock.patch.object(bm, "ntfy_send", fake_send), \
                    mock.patch("sys.stdout", io.StringIO()):
                state, _note = bm.notify_ntfy(
                    {"category_suggestion": "local_city"}, {}, "high",
                    "CRITICAL", paths, args, "https://example.test/a",
                    "rss", "2026-10-14T00:00:00+05:45")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(state, "delivered", "the control: it really sent")
        self.assertEqual(captured["tags"][0], ntfy_classes.BREAKING)
        assert_wire_shape(self, {"tags": captured["tags"],
                                 "priority": captured["priority"]},
                          "breaking alert")
        self.assertEqual(captured["priority"], 5)
        self.assertIn("rotating_light", captured["tags"])


class DegradedWorkflowPayloadTest(unittest.TestCase):
    """The one Python payload that lives inside the workflow, not the scripts."""

    def test_the_degraded_notice_is_tagged_pipeline(self):
        with open(WORKFLOW_PATH, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("'tags': ['pipeline', 'warning', 'rotating_light']", text,
                      "the degraded academic payload must carry `pipeline` so "
                      "it is not mistaken for a CRITICAL breaking alert")
        # The gate notice must not be confusable with the breaking alert.
        self.assertIn("'priority': 5", text)


def _node_eval_digest_payload(args_js, worker_path=None):
    """Run the COMMITTED digestPayload under node and return its output.

    The two helpers it closes over (`clipToNtfyLimit`, and that function's own
    `utf8Bytes` / `NTFY_BODY_MAX_BYTES`) are lifted out of the same source file
    rather than stubbed, so this evaluates the real coercion against the real
    budget. Stubbing them would let a broken coercion pass.

    Line endings are normalised before `grab` looks for its terminator: a CRLF
    working copy has no `\\n}\\n`, and the old search then extracted an empty
    string, so node reported `digestPayload is not defined` against a perfectly
    healthy worker. An unterminated function now throws instead of slicing empty.
    """
    preamble = (
        "const fs=require('fs');\n"
        "const src=fs.readFileSync(process.argv[1],'utf8')"
        ".replace(/\\r\\n/g,'\\n');\n"
        "function grab(name){\n"
        "  const s=src.indexOf('function '+name+'(');\n"
        "  if(s<0) throw new Error('missing '+name);\n"
        "  const at=src.indexOf('\\n}\\n', s);\n"
        "  if(at<0) throw new Error('unterminated '+name);\n"
        "  return src.slice(s,at+2);\n"
        "}\n"
        "const fns=['utf8Bytes','clipToNtfyLimit','digestPayload'].map(grab);\n"
        "const consts=src.match(/^const NTFY_BODY_MAX_BYTES[^\\n]*$/m);\n"
        "eval(fns.join('\\n')+'\\n'+(consts?consts[0]:''));\n"
    )
    return subprocess.run(
        ["node", "-e", preamble + args_js, worker_path or WORKER_PATH],
        capture_output=True, text=True, timeout=60)


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class WorkerDigestShapeTest(unittest.TestCase):
    """The Worker's own digest push, evaluated with node against real source.

    ``digestPayload`` coerces, so the committed worker has been safe even while
    its call site passed strings. The call site is what had to change: it now
    passes a literal array carrying `digest`, so the class is visible on the
    wire rather than inferred.
    """

    def test_the_digest_call_site_passes_the_digest_class_tag(self):
        with open(WORKER_PATH, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('["digest", "newspaper"]', text,
                      "the Worker's digest push must carry the `digest` tag")

    def test_digest_payload_produces_a_valid_wire_shape(self):
        out = _node_eval_digest_payload(
            "const p=digestPayload('t','Title','body','https://x.test',"
            "\"4\",[\"digest\",\"newspaper\"]);\n"
            "process.stdout.write(JSON.stringify({tags:p.tags,"
            "priority:p.priority}));\n")
        self.assertEqual(out.returncode, 0,
                         "node failed: %s" % (out.stderr or out.stdout))
        got = json.loads(out.stdout)
        assert_wire_shape(self, got, "worker digestPayload")
        self.assertEqual(got["tags"], ["digest", "newspaper"])
        self.assertEqual(got["priority"], 4)

    def test_the_string_form_still_coerces_so_the_worker_cannot_regress(self):
        """The documented 400 shape. If coercion ever broke, this goes red."""
        out = _node_eval_digest_payload(
            "process.stdout.write(JSON.stringify(digestPayload("
            "'t','T','b',null,'4','newspaper')));\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        assert_wire_shape(self, got, "worker string form")
        self.assertEqual(got["tags"], ["newspaper"])
        self.assertEqual(got["priority"], 4)

    def test_a_crlf_working_copy_still_evaluates(self):
        """A CRLF checkout must not look like a broken worker.

        Regression for the extraction bug, not for the Worker: `grab` searched
        for a bare `\\n}\\n`, found none in a CRLF copy, sliced an empty string,
        and node failed with `digestPayload is not defined` while the Worker was
        fine. A real `core.autocrlf` checkout, or any tool that rewrites the file
        in text mode, produces exactly that copy.
        """
        with open(WORKER_PATH, "r", encoding="utf-8") as fh:
            crlf = fh.read().replace("\r\n", "\n").replace("\n", "\r\n")
        self.assertIn("\r\n}\r\n", crlf, "fixture sanity: the copy is CRLF")
        handle, path = tempfile.mkstemp(suffix=".js")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(crlf)
        out = _node_eval_digest_payload(
            "process.stdout.write(JSON.stringify(digestPayload("
            "'t','T','b',null,'4','newspaper')));\n", worker_path=path)
        self.assertEqual(out.returncode, 0,
                         "a CRLF copy of the worker failed to evaluate: %s"
                         % (out.stderr or out.stdout))
        got = json.loads(out.stdout)
        assert_wire_shape(self, got, "worker digestPayload on a CRLF copy")
        self.assertEqual(got["tags"], ["newspaper"])


if __name__ == "__main__":
    unittest.main()