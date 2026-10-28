#!/usr/bin/env python3
"""tests/notifications/test_ntfy_wire_shape_guard.py — the 400, on every site.

THE OUTAGE THIS EXISTS TO PREVENT. On 2026-10-12 the digest push went out with
``"tags": "newspaper"`` and ntfy answered HTTP 400 / code 40024. The digest did
not arrive, and nothing said so. Measured against a throwaway topic and recorded
at ``deploy/cloudflare-worker.js``:

    {"priority":"4","tags":"newspaper"}     -> HTTP 400
    {"priority":4,  "tags":["newspaper"]}   -> HTTP 200

``tests/academic/test_notification_classes.py`` already pins the helper
(``ntfy_classes.wire_shape``) and each call site's ARGS. This file pins the
thing that file structurally cannot: the bytes as they leave each site.

WHY THE BYTES AND NOT THE DICT. A Python-level check cannot see this failure --
a list and a string both survive ``payload["tags"]`` and both serialise without
error. The defect only exists after ``json.dumps``. So every assertion here
inspects the SERIALISED form, because that is the only place the two shapes
differ.

WHY THIS IS NOT A DUPLICATE. ``payload_io.payload_to_ntfy_body`` performs NO
type coercion at all::

    "tags": ntfy.get("tags") or [],
    "priority": ntfy.get("priority") or 3,

So a payload whose ``ntfy`` block ever carried a string would pass straight
through to HTTP 400 with nothing in the repo able to notice. Every current
caller happens to pass a list and an int, which is exactly the kind of "correct
by accident" this repository has been burned by repeatedly. This file drives the
REAL senders rather than their helpers, so it fails at the boundary instead of
trusting every call site to have done its part.

Offline throughout: no network, no topic is read, no secret is touched. Each
site is driven with the transport replaced, so what is asserted is what WOULD
have been POSTed.
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
import live_canary as lc  # noqa: E402
import payload_io  # noqa: E402
import selfcheck_exam_sources as scs  # noqa: E402

WORKER_PATH = os.path.join(REPO_ROOT, "deploy", "cloudflare-worker.js")

#: The two literal strings that ARE the HTTP 400. A JSON string in either field
#: is enough on its own to make ntfy reject the whole message.
HTTP_400_TAGS = '"tags": "'
HTTP_400_PRIORITY = '"priority": "'


def assert_ntfy_accepts(case, encoded, where):
    """Assert the POSTED BYTES cannot be the shape ntfy answers with HTTP 400.

    Takes the serialised text rather than the dict on purpose -- see the module
    docstring. Every check here is something a string would fail and a correct
    body passes.
    """
    case.assertNotIn(HTTP_400_TAGS, encoded,
                     "%s: tags serialised as a STRING -> ntfy answers HTTP 400 "
                     "(code 40024). This is the 2026-10-12 production outage. "
                     "Body: %s" % (where, encoded))
    case.assertNotIn(HTTP_400_PRIORITY, encoded,
                     "%s: priority serialised as a STRING -> ntfy answers HTTP "
                     "400 (code 40024). Body: %s" % (where, encoded))

    # Re-read the bytes as ntfy would, so the check is on the wire form and not
    # on whatever object the caller happened to be holding.
    decoded = json.loads(encoded)
    tags = decoded.get("tags")
    case.assertIsInstance(tags, list,
                          "%s: tags must be an array, got %r -- ntfy answers "
                          "HTTP 400" % (where, type(tags).__name__))
    for tag in tags:
        case.assertIsInstance(tag, str,
                              "%s: every tag must be a string, got %r"
                              % (where, tag))
    priority = decoded.get("priority")
    # bool is an int subclass, and True would serialise as `true`; ntfy wants a
    # number, so the type is asserted exactly rather than by isinstance.
    case.assertIs(type(priority), int,
                  "%s: priority must be a NUMBER, got %r -- ntfy answers HTTP "
                  "400" % (where, priority))
    case.assertTrue(1 <= priority <= 5,
                    "%s: priority %r is outside ntfy's 1-5 range" % (where,
                                                                      priority))


class Http400ShapeIsDetectable(unittest.TestCase):
    """The RED case itself, proven without waiting for a real regression.

    A guard is only worth having if it actually goes red. This feeds the
    checker the exact 2026-10-12 shapes and requires it to reject them, so a
    later 'simplification' of ``assert_ntfy_accepts`` cannot quietly turn this
    file into decoration.
    """

    def test_a_string_tags_is_rejected(self):
        body = {"topic": "t", "title": "T", "message": "m",
                "tags": "newspaper", "priority": 4}
        with self.assertRaises(AssertionError) as caught:
            assert_ntfy_accepts(self, json.dumps(body), "the 2026-10-12 shape")
        self.assertIn("HTTP 400", str(caught.exception))

    def test_a_string_priority_is_rejected(self):
        body = {"topic": "t", "title": "T", "message": "m",
                "tags": ["newspaper"], "priority": "4"}
        with self.assertRaises(AssertionError) as caught:
            assert_ntfy_accepts(self, json.dumps(body), "a string priority")
        self.assertIn("HTTP 400", str(caught.exception))

    def test_a_malformed_tags_container_is_rejected(self):
        for junk in ("", {}, 5, None):
            body = {"topic": "t", "title": "T", "message": "m",
                    "tags": junk, "priority": 4}
            with self.assertRaises(AssertionError):
                assert_ntfy_accepts(self, json.dumps(body),
                                    "tags=%r" % (junk,))

    def test_a_correct_body_passes(self):
        body = {"topic": "t", "title": "T", "message": "m",
                "tags": ["digest", "newspaper"], "priority": 4}
        assert_ntfy_accepts(self, json.dumps(body), "the correct shape")


class PayloadToNtfyBodyBoundaryTest(unittest.TestCase):
    """`payload_to_ntfy_body` is where the payload-driven sites reach the wire.

    It coerces NEITHER field, so this pins what the real payloads actually
    produce rather than assuming the helper would save them.
    """

    def _real_result_payload(self, passed):
        state = {"roll_number": "13572468", "current_semester": 4,
                 "semesters": {}}
        notice = {"id": 15700, "title": "B.Sc.CSIT I Semester Exam Result",
                  "pdf_url": "https://portal.tu.edu.np/p/1.pdf",
                  "url": "https://iost.tu.edu.np/notices/15700"}
        return car._build_payload(state, 1, notice, passed, "2026-10-14")

    def test_a_passed_result_reaches_the_wire_as_an_array(self):
        payload = self._real_result_payload(True)
        body = payload_io.payload_to_ntfy_body(payload, "topic")
        assert_ntfy_accepts(self, json.dumps(body), "the PASSED result push")

    def test_a_failed_result_reaches_the_wire_as_an_array(self):
        payload = self._real_result_payload(False)
        body = payload_io.payload_to_ntfy_body(payload, "topic")
        assert_ntfy_accepts(self, json.dumps(body), "the FAILED result push")

    def test_the_fitted_body_is_still_an_array(self):
        """`fit_ntfy_body` rewrites the body before it goes out, so the LAST
        thing ntfy sees is its return value, not the input."""
        payload = self._real_result_payload(True)
        body = payload_io.payload_to_ntfy_body(payload, "topic")
        fitted, _truncated = payload_io.fit_ntfy_body(body)
        assert_ntfy_accepts(self, json.dumps(fitted),
                            "the fitted result push")


class BreakingAlertSiteTest(unittest.TestCase):
    """The breaking monitor, driven through its REAL `ntfy_send`.

    The sibling suite mocks `ntfy_send` and therefore only ever sees its
    arguments. This drives the real one and captures the body handed to the
    transport, which is the last Python-side shape before serialisation.
    """

    def _drive(self, label):
        import argparse
        import io
        import shutil as _shutil
        import tempfile

        import breaking_monitor as bm

        captured = {}

        def fake_http(url, body):
            captured["body"] = body
            return 200, "{}"

        tmp = tempfile.mkdtemp()
        paths = {"ntfy_preview": os.path.join(tmp, "p.txt"),
                 "breaking_delivery": os.path.join(tmp, "d.json")}
        env = {"NTFY_TOPIC": "dummy-topic", "NTFY_SERVER": "https://ntfy.test"}
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(bm, "ntfy_configured",
                                      lambda: (True, "ok")), \
                    mock.patch("sys.stdout", io.StringIO()):
                state, _note = bm.notify_ntfy(
                    {"category_suggestion": "local_city"}, {}, "high",
                    label, paths, argparse.Namespace(dry_run=False),
                    "https://example.test/a", "rss",
                    "2026-10-14T00:00:00+05:45", http_fn=fake_http)
        finally:
            _shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(state, "delivered",
                         "the control: the alert really reached the transport")
        self.assertIn("body", captured, "the transport was never called")
        return captured["body"]

    def test_the_breaking_alert_wire_body_is_acceptable(self):
        assert_ntfy_accepts(self, json.dumps(self._drive("CRITICAL")),
                            "the CRITICAL breaking alert")

    def test_the_non_critical_breaking_alert_wire_body_is_acceptable(self):
        assert_ntfy_accepts(self, json.dumps(self._drive("HIGH")),
                            "the non-CRITICAL breaking alert")


class _FakeResponse:
    """Enough of a urlopen response for the sites' `with` blocks to close.

    Both senders wrap their POST in `try/except` and return False on ANY
    exception, so a capture that raised would be swallowed and reported as a
    failed send rather than as the bytes it intercepted. It must return.
    """

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return b"{}"


class _CaptureUrlopen:
    """Stand-in for urllib.request.urlopen that keeps the bytes it was given."""

    def __init__(self):
        self.bodies = []

    def __call__(self, req, timeout=None):
        self.bodies.append(req.data)
        return _FakeResponse()

    def only(self, case, where):
        case.assertEqual(len(self.bodies), 1,
                         "%s: expected exactly one POST, got %d"
                         % (where, len(self.bodies)))
        return self.bodies[0].decode("utf-8")


class SelfCheckAndCanarySiteTest(unittest.TestCase):
    """The two best-effort sites, asserted on the exact bytes they would POST."""

    def test_the_self_check_alert_bytes_are_acceptable(self):
        capture = _CaptureUrlopen()
        env = {"NTFY_TOPIC": "dummy-topic", "NTFY_SERVER": "https://ntfy.test"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("urllib.request.urlopen", capture), \
                mock.patch("sys.stdout", mock.MagicMock()):
            self.assertTrue(scs.notify_ntfy("T", "M", False,
                                            class_tag="result"))
        assert_ntfy_accepts(self, capture.only(self, "the self-check alert"),
                            "the self-check RESULT alert")

    def test_the_pipeline_gate_notice_bytes_are_acceptable(self):
        capture = _CaptureUrlopen()
        env = {"NTFY_TOPIC": "dummy-topic", "NTFY_SERVER": "https://ntfy.test"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("urllib.request.urlopen", capture), \
                mock.patch("sys.stdout", mock.MagicMock()):
            self.assertTrue(scs.notify_ntfy("T", "M", False,
                                            class_tag="pipeline"))
        assert_ntfy_accepts(self, capture.only(self, "the pipeline notice"),
                            "the pipeline gate notice")

    def test_the_live_canary_bytes_are_acceptable(self):
        capture = _CaptureUrlopen()
        env = {"NTFY_TOPIC": "dummy-topic", "NTFY_SERVER": "https://ntfy.test"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("urllib.request.urlopen", capture), \
                mock.patch("sys.stdout", mock.MagicMock()):
            lc.notify_ntfy("canary up", "the canary is alive", 4,
                            dry_run=False)
        assert_ntfy_accepts(self, capture.only(self, "the live canary"),
                            "the live canary alert")


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class WorkerDigestSiteTest(unittest.TestCase):
    """The Worker's own digest push, evaluated under node against real source.

    A missing node is not a broken contract, so this skips rather than fails --
    the same call the sibling suite makes, for the same reason.
    """

    def _eval(self, args_js, worker_path=None):
        # Line endings are normalised before `grab` looks for its terminator: a
        # CRLF working copy has no `\n}\n` and the old search sliced an empty
        # string, so node said `digestPayload is not defined` about a healthy
        # worker. An unterminated function now throws instead of slicing empty.
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
            "const fns=['utf8Bytes','clipToNtfyLimit','digestPayload']"
            ".map(grab);\n"
            "const consts=src.match(/^const NTFY_BODY_MAX_BYTES[^\\n]*$/m);\n"
            "eval(fns.join('\\n')+'\\n'+(consts?consts[0]:''));\n"
        )
        return subprocess.run(["node", "-e", preamble + args_js,
                               worker_path or WORKER_PATH],
                              capture_output=True, text=True, timeout=60)

    def test_the_digest_push_bytes_are_acceptable(self):
        out = self._eval(
            "const p=digestPayload('t','Title','body','https://x.test',"
            "\"4\",[\"digest\",\"newspaper\"]);\n"
            "process.stdout.write(JSON.stringify({topic:'t',tags:p.tags,"
            "priority:p.priority}));\n")
        self.assertEqual(out.returncode, 0,
                         "node failed: %s" % (out.stderr or out.stdout))
        assert_ntfy_accepts(self, out.stdout, "the worker digest push")

    def test_the_string_call_site_would_still_be_coerced(self):
        """A call site that passes the bare string form must not reach ntfy
        as a string -- `digestPayload` is the last chance to fix it."""
        out = self._eval(
            "process.stdout.write(JSON.stringify(digestPayload("
            "'t','T','b',null,'4','newspaper')));\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        assert_ntfy_accepts(self, out.stdout, "the worker string form")

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
        out = self._eval(
            "process.stdout.write(JSON.stringify(digestPayload("
            "'t','T','b',null,'4','newspaper')));\n", worker_path=path)
        self.assertEqual(out.returncode, 0,
                         "a CRLF copy of the worker failed to evaluate: %s"
                         % (out.stderr or out.stdout))
        assert_ntfy_accepts(self, out.stdout,
                            "the worker string form on a CRLF copy")


if __name__ == "__main__":
    unittest.main()
