#!/usr/bin/env python3
"""FROZEN CONTRACTS 1c and 1d, asserted against the REAL shipped code.

WHY THIS FILE EXISTS
--------------------
The semester-1 PASS (roll 13572468) was announced by a run that delivered
NOTHING and still exited 0. The chain, all of it verified live:

  * ``emit_notification`` printed every payload with ``indent=2``;
  * the workflow ``tee``'d the concatenation into one file, so four payloads
    produced a file that was not one JSON document;
  * ``payload_check`` asked only whether the file was non-empty;
  * the ntfy step called ``json.load`` on the blob, got
    ``JSONDecodeError: Extra data: line 33``, and its ``try/except`` printed
    "No notification payload available - ntfy skipped";
  * ntfy, the render step and the Pages deploy all keyed off that skip, so the
    whole delivery half no-opped;
  * the job exited 0.

And a second, quieter bug sat in the same loop: ``payload = data[-1]`` would
have dropped payloads 1..N-1 even if the file had parsed.

DESIGN RULES FOR THIS FILE (all deliberate)
-------------------------------------------
  * IT TESTS THE SHIPPED CODE, NOT A COPY. The academic ``payload_check``,
    ``Assert academic delivery``, ``Fallback no-op check``, ``Record
    delivery status``, ``Assert digest is not empty`` and the watchdog's
    retry/alert steps live inside workflow heredocs, so their Python bodies
    are EXTRACTED from the parsed YAML (the same
    ``iter_all_steps`` + ``heredoc_bodies`` plumbing the workflow lint uses)
    and ``exec``'d in a temp directory. A rewrite of the shell that breaks
    the contract fails HERE, not in production at 3am.
  * FULLY OFFLINE. No network, no GitHub API, no ntfy, no git. The two steps
    that would send (ntfy publish, the empty-digest alert) get a poisoned
    ``urllib.request.urlopen`` that records the POST instead of performing
    it, and the retired-ladder step is asserted on its TEXT because it is
    shell. Nothing here can reach the network even by accident.
  * NO SILENT-SKIP ASSERTION IS OPTIONAL. The malformed-line cases are the
    point of the whole contract, so each one asserts both that
    ``PayloadFormatError`` is raised AND that the healthy lines around the
    broken one were NOT quietly returned.
  * The exact ``::error::`` strings are asserted verbatim, because a loud
    failure is only useful if the text says what went wrong.
"""

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(REPO_ROOT / "scripts")
if REPO_ROOT_STR := str(REPO_ROOT):
    sys.path.insert(0, REPO_ROOT_STR)
sys.path.insert(0, SCRIPTS)

import payload_io  # noqa: E402
import validate_stories as vs  # noqa: E402
import check_academic_result as academic  # noqa: E402

try:
    from tests import workflows as W
    from tests.workflows.test_workflow_lint import heredoc_bodies
except ImportError:  # pragma: no cover - direct execution of this file
    from tests import workflows as W
    from tests.workflows.test_workflow_lint import heredoc_bodies


ACADEMIC = "academic-result.yml"
DAILY = "daily-news.yml"
WATCHDOG = "digest-watchdog.yml"


# ---------------------------------------------------------------------------
# Shared plumbing: payload fixtures + the workflow-step harness
# ---------------------------------------------------------------------------


def payload(title, semester=1, passed=True):
    """One notification payload, shaped like the real emit_notification output."""
    return {
        "kind": "academic_result",
        "semester": semester,
        "roll_number": "13572468",
        "passed": passed,
        "status": "passed" if passed else "failed",
        "result_date": "2026-09-29",
        "title": title,
        "pdf_url": "https://iost.tu.edu.np/result.pdf",
        "ntfy": {
            "title": "🎓 TU Result: Sem %d %s" % (semester, "PASSED" if passed else "NOT FOUND"),
            "message": "Roll 13572468 | Sem %d | %s" % (semester, title),
            "tags": ["trophy"] if passed else ["warning"],
            "priority": 5 if passed else 3,
            # The Pages ORIGIN, which is what _worker_base() defaults to. It
            # used to be the deleted `digest-auth` Worker's workers.dev host.
            "click": "https://daily-digest.example.pages.dev/",
            "actions": [],
        },
        "website": {"path": "docs/academic/2026-09-29-sem%d.html" % semester},
    }


def dumps_line(obj):
    """The exact producer form: compact, one line, no indent."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def step_heredoc(workflow, step_name, tag="PY"):
    """The python heredoc body of one named step, read from the real YAML."""
    for _job, step, step_dict in W.iter_all_steps(workflow):
        if step == step_name and "run" in step_dict:
            for _tag, body in heredoc_bodies(step_dict["run"], (tag,)):
                return body
    raise AssertionError("no python heredoc in %s / %r" % (workflow, step_name))


def step_run_text(workflow, step_name):
    for _job, step, step_dict in W.iter_all_steps(workflow):
        if step == step_name and "run" in step_dict:
            return step_dict["run"]
    raise AssertionError("no run: block in %s / %r" % (workflow, step_name))


class _FakeResponse:
    """Minimal urlopen result: what the ntfy steps actually read."""

    def __init__(self, payload_bytes):
        self._body = payload_bytes

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


class WorkflowStepHarness(unittest.TestCase):
    """exec a workflow step's real Python body against a temp directory."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="payload-io-step-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._postings = []

    # -- filesystem helpers ------------------------------------------------
    def path(self, rel):
        return os.path.join(self._tmp, rel)

    def write(self, rel, content):
        full = self.path(rel)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if isinstance(content, (dict, list)):
            with open(full, "w", encoding="utf-8") as fh:
                json.dump(content, fh, ensure_ascii=False)
        else:
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        return full

    def read_json(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return json.load(fh)

    def read_text(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    def outputs(self, tag="gh_output.txt"):
        """Parsed GITHUB_OUTPUT / GITHUB_ENV writes of the last run."""
        parsed = {}
        with open(self.path(tag), encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    key, _, value = line.rstrip("\n").partition("=")
                    parsed[key.strip()] = value
        return parsed

    # -- the network poison ------------------------------------------------
    def capture_posts(self):
        """Replace urlopen with a recorder. Nothing can leave the process."""
        postings = self._postings

        def fake_urlopen(req, timeout=None):
            body = req.data.decode("utf-8") if req.data else ""
            postings.append(json.loads(body))
            return _FakeResponse(b'{"id":"fake-id"}')

        self._real_urlopen = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        self.addCleanup(self._restore_urlopen)
        return postings

    def _restore_urlopen(self):
        urllib.request.urlopen = self._real_urlopen

    # -- the harness -------------------------------------------------------
    def run_step(self, workflow, step_name, env=None):
        """exec the step body; return (exit_code, stdout).

        exit_code is the value the step passed to ``sys.exit`` (0 for a step
        that ran to completion), which is exactly what GitHub Actions turns
        into a red job.
        """
        body = step_heredoc(workflow, step_name)
        for name in ("gh_output.txt", "gh_summary.md", "gh_env.txt"):
            self.write(name, "")
        full_env = {
            "GITHUB_OUTPUT": self.path("gh_output.txt"),
            "GITHUB_STEP_SUMMARY": self.path("gh_summary.md"),
            "GITHUB_ENV": self.path("gh_env.txt"),
        }
        full_env.update(env or {})

        saved = dict(os.environ)
        cwd = os.getcwd()
        buf = io.StringIO()
        code = 0
        os.environ.update(full_env)
        try:
            os.chdir(self._tmp)
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(body, "<%s:%s>" % (workflow, step_name), "exec"),
                         {"__name__": "wf_step"})
                except SystemExit as exc:
                    code = 0 if exc.code is None else exc.code
                    if not isinstance(code, int):
                        code = 1
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        return code, buf.getvalue()


# ==========================================================================
# Contract 1c, part 1 -- the parse contract (scripts/payload_io.py)
# ==========================================================================


class PayloadIOFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="payload-io-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # The live writer truncates on the first call of a process; give every
        # test its own "first call" so a previous test's file cannot leak in.
        academic._LIVE_WRITTEN["started"] = False

    def write(self, text):
        full = os.path.join(self.tmp, "payloads.jsonl")
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return full

    # -- round trip --------------------------------------------------------
    def test_jsonl_round_trip_preserves_order_and_content(self):
        items = [payload("Sem 1 PASS"), payload("Sem 2 pending", 2, False),
                 payload("Sem 3 PASS", 3)]
        path = self.write("".join(dumps_line(p) + "\n" for p in items))
        got = payload_io.read_payloads(path)
        self.assertEqual(3, len(got))
        self.assertEqual(items, got, "a round trip must not reorder, drop or "
                                     "mutate a single payload")

    def test_one_payload_file_parses_without_a_trailing_newline(self):
        path = self.write(dumps_line(payload("Sem 1 PASS")))
        self.assertEqual(1, len(payload_io.read_payloads(path)))

    def test_producer_output_is_readable_by_the_consumer(self):
        """The real emit_notification, executed, piped into the real reader.

        This is the end-to-end proof of the whole format contract: the exact
        function the monitor runs must produce a file the delivery steps can
        parse. Extracted from the shipped source (not re-implemented) so a
        regression to `indent=2` fails here.

        CONTRACT CHANGE (2026-10-27): stdout is NO LONGER the delivery source.
        It used to be `| tee state/academic_live_payload.jsonl`, so the payload
        -- roll number included -- was printed into a public Actions log. The
        function now writes the live file itself and prints a redacted copy, so
        this asserts the FILE carries the payloads intact and that stdout still
        parses as one JSON object per line. Both halves are load-bearing: a
        redacted log is worthless if the file stopped parsing.
        """
        source = (REPO_ROOT / "scripts" / "check_academic_result.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "emit_notification")
        segment = ast.get_source_segment(source, fn)
        self.assertIsNotNone(segment)

        live = os.path.join(self.tmp, "live.jsonl")
        namespace = {"json": json, "save_state": lambda *a, **k: None,
                     "NOTIFY_PREVIEW_PATH": os.path.join(self.tmp, "preview.json"),
                     "NOTIFY_LIVE_PATH": live,
                     "redacted_for_log": academic.redacted_for_log}
        namespace["_append_live_line"] = academic._append_live_line
        exec(compile(segment, "<emit_notification>", "exec"), namespace)
        emit = namespace["emit_notification"]

        items = [payload("Sem %d" % n, n) for n in (1, 2, 3, 4)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for item in items:
                emit(item, dry_run=False)
        printed = buf.getvalue()
        self.assertEqual(4, len(printed.strip().split("\n")),
                         "each payload must be exactly one line on stdout too")

        self.assertEqual(items, payload_io.read_payloads(live),
                         "the live file the delivery steps read must carry the "
                         "payloads verbatim, in order, with nothing redacted")

        path = os.path.join(self.tmp, "printed.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(printed)
        self.assertEqual(4, len(payload_io.read_payloads(path)),
                         "the redacted stdout must still be one JSON object per "
                         "line, so the log stays readable")

    def test_producer_pins_compact_separators_and_no_indent(self):
        """The one-line contract, asserted on the AST, not on a copy."""
        source = (REPO_ROOT / "scripts" / "check_academic_result.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.parse(source).body
                  if isinstance(n, ast.FunctionDef) and n.name == "emit_notification")
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "print"]
        self.assertEqual(1, len(calls),
                         "emit_notification must print exactly once, in the "
                         "live branch only")
        self.assertEqual(1, len(calls[0].args),
                         "the live print takes the serialised payload and "
                         "nothing else")
        dumps_call = calls[0].args[0]
        self.assertIsInstance(dumps_call, ast.Call)
        self.assertIsInstance(dumps_call.func, ast.Attribute)
        self.assertEqual("dumps", dumps_call.func.attr)
        keywords = {k.arg: k.value for k in dumps_call.keywords}
        self.assertIn("separators", keywords,
                      "the live print must pin separators=(',', ':') or a "
                      "payload spans lines and the whole file stops parsing")
        self.assertNotIn("indent", keywords,
                         "indent must be GONE: indent=2 is what fused four "
                         "payloads into an unparseable blob")
        separators = keywords["separators"]
        self.assertIsInstance(separators, ast.Tuple)
        self.assertEqual([",", ":"],
                         [elt.value for elt in separators.elts],
                         "separators must be the compact (',', ':') pair")

    # -- blank lines -------------------------------------------------------
    def test_blank_and_whitespace_only_lines_are_skipped(self):
        path = self.write(
            "\n"
            + dumps_line(payload("a")) + "\n"
            + "   \n"
            + "\t\n"
            + dumps_line(payload("b")) + "\n"
            + "\n")
        got = payload_io.read_payloads(path)
        self.assertEqual(["a", "b"], [p["title"] for p in got])

    def test_present_but_blank_file_is_not_an_error(self):
        self.assertEqual([], payload_io.read_payloads(self.write("\n \n\t\n")))

    # -- malformed lines MUST raise ---------------------------------------
    def test_malformed_line_raises_and_healthy_lines_are_not_returned(self):
        path = self.write(
            dumps_line(payload("good-1")) + "\n"
            + "{not json at all\n"
            + dumps_line(payload("good-2")) + "\n")
        with self.assertRaises(payload_io.PayloadFormatError) as ctx:
            payload_io.read_payloads(path)
        self.assertEqual(2, ctx.exception.line)
        self.assertEqual("invalid json", ctx.exception.reason)
        self.assertIn(path, str(ctx.exception))

    def test_pretty_printed_concatenated_payloads_raise(self):
        """THE PRODUCTION FAILURE, reproduced as a test.

        Four payloads printed with indent=2 and concatenated: the file is not
        one JSON document, and the old reader's json.load raised "Extra data"
        at line 33. It must now be a named, located PayloadFormatError.
        """
        text = "".join(json.dumps(payload("Sem %d" % n, n), ensure_ascii=False,
                                  indent=2) for n in (1, 2, 3, 4))
        with self.assertRaises(payload_io.PayloadFormatError) as ctx:
            payload_io.read_payloads(self.write(text))
        self.assertEqual("invalid json", ctx.exception.reason)
        self.assertEqual(1, ctx.exception.line,
                         "the concatenated blob opens with a bare '{', so line "
                         "1 is already unparseable -- the old json.load said "
                         "'Extra data: line 33' because it only complained "
                         "AFTER the first document parsed")

    def test_non_dict_line_raises_not_an_object(self):
        for bad in ("[1, 2]", "123", '"a string"', "null", "true"):
            with self.subTest(line=bad):
                path = self.write(dumps_line(payload("a")) + "\n" + bad + "\n")
                with self.assertRaises(payload_io.PayloadFormatError) as ctx:
                    payload_io.read_payloads(path)
                self.assertEqual("not an object", ctx.exception.reason)
                self.assertEqual(2, ctx.exception.line)

    def test_non_dict_line_does_not_discard_the_earlier_payloads(self):
        """A half-accepted file is the failure mode, not a missing payload.

        There is no way to observe "returned the good ones and dropped the
        bad one" through the public API, because read_payloads either returns
        a whole file or raises -- so the guarantee is asserted structurally:
        the reader is a single pass that has already appended the first
        payload when it hits line 2, and the only thing it can do next is
        raise.
        """
        good = dumps_line(payload("a"))
        path = self.write(good + "\n" + "[1,2]\n" + good + "\n")
        with self.assertRaises(payload_io.PayloadFormatError) as ctx:
            payload_io.read_payloads(path)
        self.assertEqual(2, ctx.exception.line)
        self.assertEqual("not an object", ctx.exception.reason)
        self.assertEqual("not an object", payload_io.PayloadFormatError(
            path, 2, "not an object").reason)

    # -- legacy JSON list (the dry-run preview) ---------------------------
    def test_legacy_json_list_file_is_accepted(self):
        items = [payload("preview-1"), payload("preview-2", 2)]
        path = self.write(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(items, payload_io.read_payloads(path))

    def test_legacy_list_with_a_non_dict_element_raises(self):
        path = self.write(json.dumps([payload("a"), "nope"]))
        with self.assertRaises(payload_io.PayloadFormatError) as ctx:
            payload_io.read_payloads(path)
        self.assertEqual("not an object", ctx.exception.reason)
        self.assertEqual(2, ctx.exception.line)

    def test_broken_legacy_list_raises_invalid_json(self):
        with self.assertRaises(payload_io.PayloadFormatError) as ctx:
            payload_io.read_payloads(self.write('[{"a": 1},'))
        self.assertEqual("invalid json", ctx.exception.reason)

    # -- absence -----------------------------------------------------------
    def test_missing_file_returns_empty_list(self):
        self.assertEqual([], payload_io.read_payloads(
            os.path.join(self.tmp, "nope.jsonl")))

    def test_count_payloads_is_minus_one_for_absent(self):
        self.assertEqual(-1, payload_io.count_payloads(
            os.path.join(self.tmp, "nope.jsonl")))

    def test_count_payloads_counts_present_files(self):
        four = self.write("".join(dumps_line(payload("Sem %d" % n, n)) + "\n"
                                  for n in (1, 2, 3, 4)))
        self.assertEqual(4, payload_io.count_payloads(four))
        self.assertEqual(0, payload_io.count_payloads(self.write("\n\n")))
        self.assertEqual(2, payload_io.count_payloads(
            self.write(json.dumps([payload("a"), payload("b", 2)]))))

    def test_count_payloads_propagates_a_format_error(self):
        with self.assertRaises(payload_io.PayloadFormatError):
            payload_io.count_payloads(self.write("{oops\n"))

    # -- the data[-1] regression -------------------------------------------
    def test_four_payloads_yield_four_and_none_is_dropped(self):
        items = [payload("Sem %d" % n, n) for n in (1, 2, 3, 4)]
        path = self.write("".join(dumps_line(p) + "\n" for p in items))
        got = payload_io.read_payloads(path)
        self.assertEqual(4, len(got), "the whole point: N in, N out")
        self.assertEqual(["Sem %d" % n for n in (1, 2, 3, 4)],
                         [p["title"] for p in got])
        self.assertNotEqual(items[-1], got[0],
                            "data[-1] semantics would make the first payload "
                            "the last one")
        self.assertEqual({p["website"]["path"] for p in got},
                         {"docs/academic/2026-09-29-sem%d.html" % n for n in (1, 2, 3, 4)},
                         "every payload keeps its own page identity")


class NtfyBodyTests(unittest.TestCase):
    def test_body_mapping_is_moved_verbatim(self):
        p = payload("Sem 1")
        body = payload_io.payload_to_ntfy_body(p, topic="secret-topic")
        self.assertEqual("secret-topic", body["topic"])
        self.assertEqual(p["ntfy"]["title"], body["title"])
        self.assertEqual(p["ntfy"]["message"], body["message"])
        self.assertEqual(p["ntfy"]["tags"], body["tags"])
        self.assertEqual(p["ntfy"]["priority"], body["priority"])
        self.assertEqual(p["ntfy"]["click"], body["click"])
        self.assertEqual(p["ntfy"]["actions"], body["actions"])

    def test_missing_ntfy_block_degrades_to_an_empty_body_not_an_exception(self):
        body = payload_io.payload_to_ntfy_body({"kind": "degraded"}, topic="t")
        self.assertEqual("", body["title"])
        self.assertEqual(3, body["priority"], "the historical default")

    def _bytes(self, body):
        return len(json.dumps(body).encode("utf-8"))

    def test_body_within_budget_is_untouched(self):
        body = payload_io.payload_to_ntfy_body(payload("Sem 1"), "t")
        fitted, truncated = payload_io.fit_ntfy_body(body)
        self.assertFalse(truncated)
        self.assertEqual(body, fitted)
        self.assertLessEqual(self._bytes(fitted), payload_io.MAX_NTFY_BYTES)

    def test_oversized_body_truncates_the_message_and_never_the_title(self):
        p = payload("Sem 1")
        p["ntfy"]["message"] = "x" * 9000
        body = payload_io.payload_to_ntfy_body(p, "t")
        self.assertGreater(self._bytes(body), payload_io.MAX_NTFY_BYTES,
                           "the fixture must actually exceed the budget")
        fitted, truncated = payload_io.fit_ntfy_body(body)
        self.assertTrue(truncated)
        self.assertEqual(body["title"], fitted["title"],
                         "the title is what says WHICH semester this is; it "
                         "is never shortened")
        self.assertLessEqual(self._bytes(fitted), payload_io.MAX_NTFY_BYTES)
        self.assertLess(len(fitted["message"]), len(body["message"]))
        self.assertIn(payload_io.TRUNCATION_MARKER, fitted["message"])

    def test_budget_is_per_payload_not_aggregate(self):
        """Two payloads of ~3k each both fit: 6k total, 4096 each."""
        a, b = payload("Sem 1"), payload("Sem 2", 2)
        a["ntfy"]["message"] = "a" * 3000
        b["ntfy"]["message"] = "b" * 3000
        for one in (a, b):
            body = payload_io.payload_to_ntfy_body(one, "t")
            fitted, truncated = payload_io.fit_ntfy_body(body)
            self.assertFalse(truncated, "a single 3k payload must fit whole")
            self.assertEqual(body, fitted)
        total = self._bytes(payload_io.payload_to_ntfy_body(a, "t")) \
            + self._bytes(payload_io.payload_to_ntfy_body(b, "t"))
        self.assertGreater(total, payload_io.MAX_NTFY_BYTES,
                           "the fixture must exceed the budget IN AGGREGATE, "
                           "which is the case the old single-message design "
                           "could not express")

    def test_truncation_never_splits_a_multibyte_character(self):
        p = payload("Sem 1")
        p["ntfy"]["message"] = "🎓" * 4000
        body = payload_io.payload_to_ntfy_body(p, "t")
        fitted, truncated = payload_io.fit_ntfy_body(body)
        self.assertTrue(truncated)
        self.assertLessEqual(self._bytes(fitted), payload_io.MAX_NTFY_BYTES)
        self.assertNotIn("\ufffd", fitted["message"],
                         "a half-written emoji is corruption, not truncation")
        self.assertTrue(fitted["message"].startswith("🎓"))


# ==========================================================================
# Contract 1c, part 2 -- the academic pipeline, from the REAL workflow steps
# ==========================================================================


class AcademicPayloadCheckTests(WorkflowStepHarness):
    LIVE = "state/academic_live_payload.jsonl"

    def test_two_payload_emission_reaches_the_delivery_assert_inputs(self):
        self.write(self.LIVE, "".join(dumps_line(p) + "\n"
                                      for p in (payload("Sem 1 PASS"),
                                                payload("Sem 2 FAIL", 2, False))))
        code, out = self.run_step(ACADEMIC, "Check for emitted notification payload",
                                  env={"DRY_RUN": "false"})
        self.assertEqual(0, code, out)
        got = self.outputs()
        self.assertEqual("2", got["count"], "delivery_assert is handed the COUNT")
        self.assertEqual("true", got["found"], "found is now count > 0")
        self.assertIn("Found 2 notification payload(s)", out)

    def test_found_is_false_when_nothing_was_emitted(self):
        code, _out = self.run_step(ACADEMIC, "Check for emitted notification payload",
                                   env={"DRY_RUN": "false"})
        self.assertEqual(0, code)
        got = self.outputs()
        self.assertEqual("0", got["count"])
        self.assertEqual("false", got["found"])

    def test_a_malformed_payload_file_is_an_error_not_a_skip(self):
        self.write(self.LIVE, dumps_line(payload("Sem 1")) + "\n{oops\n")
        code, out = self.run_step(ACADEMIC, "Check for emitted notification payload",
                                  env={"DRY_RUN": "false"})
        self.assertEqual(1, code, "a payload file that cannot be parsed must "
                                  "turn the job RED, not print 'skipped'")
        self.assertIn("::error::", out)
        self.assertIn("BROKEN, not skipped", out)
        self.assertEqual("false", self.outputs()["found"])

    def test_dry_run_reads_the_preview_list(self):
        self.write("state/academic_notify_preview.json",
                   json.dumps([payload("preview")]))
        code, _out = self.run_step(ACADEMIC, "Check for emitted notification payload",
                                   env={"DRY_RUN": "true"})
        self.assertEqual(0, code)
        self.assertEqual("1", self.outputs()["count"])


class AcademicNtfyDeliveryTests(WorkflowStepHarness):
    LIVE = "state/academic_live_payload.jsonl"

    def test_two_payloads_are_delivered_as_two_messages(self):
        posts = self.capture_posts()
        items = [payload("Sem 1 PASS"), payload("Sem 2 FAIL", 2, False)]
        self.write(self.LIVE, "".join(dumps_line(p) + "\n" for p in items))
        code, out = self.run_step(ACADEMIC, "Publish to ntfy",
                                  env={"DRY_RUN": "false", "NTFY_TOPIC": "t",
                                       "NTFY_SERVER": "https://ntfy.invalid"})
        self.assertEqual(0, code, out)
        self.assertEqual(2, len(posts),
                         "ONE MESSAGE PER PAYLOAD: this is the data[-1] fix")
        self.assertEqual([p["ntfy"]["title"] for p in items],
                         [p_["title"] for p_ in posts])
        self.assertEqual("2", self.outputs()["delivered"],
                         "both payloads confirmed, not just the last one")
        self.assertEqual(2, self.read_json("state/academic_ntfy.json")["delivered"])
        self.assertEqual("ntfy delivered 2 of 2 payload(s)", out.splitlines()[-1])

    def test_one_failed_post_is_counted_honestly(self):
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("ntfy refused")
            return _FakeResponse(b'{"id":"ok"}')

        self._real_urlopen = urllib.request.urlopen
        urllib.request.urlopen = flaky
        self.addCleanup(self._restore_urlopen)
        self.write(self.LIVE, "".join(
            dumps_line(p) + "\n" for p in (payload("Sem 1"), payload("Sem 2", 2))))
        code, out = self.run_step(ACADEMIC, "Publish to ntfy",
                                  env={"DRY_RUN": "false", "NTFY_TOPIC": "t",
                                       "NTFY_SERVER": "https://ntfy.invalid"})
        self.assertEqual(0, code, "the step is continue-on-error by design")
        self.assertEqual("1", self.outputs()["delivered"],
                         "a partial delivery must be visible as 1, not 2")
        self.assertIn("ntfy publish failed for payload 2/2", out)

    def test_a_malformed_payload_file_delivers_nothing_and_says_so(self):
        posts = self.capture_posts()
        self.write(self.LIVE, '{"broken":\n')
        code, out = self.run_step(ACADEMIC, "Publish to ntfy",
                                  env={"DRY_RUN": "false", "NTFY_TOPIC": "t",
                                       "NTFY_SERVER": "https://ntfy.invalid"})
        self.assertEqual(1, code)
        self.assertEqual([], posts, "no silent fallback to the degraded page")
        self.assertIn("::error::", out)

    def test_dry_run_never_posts(self):
        posts = self.capture_posts()
        self.write("state/academic_notify_preview.json", json.dumps([payload("p")]))
        code, out = self.run_step(ACADEMIC, "Publish to ntfy",
                                  env={"DRY_RUN": "true", "NTFY_TOPIC": "t",
                                       "NTFY_SERVER": "https://ntfy.invalid"})
        self.assertEqual(0, code)
        self.assertEqual([], posts)
        self.assertIn("DRY RUN", out)


class DeliveryAssertTests(WorkflowStepHarness):
    STEP = "Assert academic delivery"

    def record(self, emitted, delivered, page_ok, dry_run="false", titles=None):
        self.write("state/academic_ntfy.json",
                   {"delivered": delivered, "titles": titles or [],
                    "truncated": []})
        code, out = self.run_step(ACADEMIC, self.STEP, env={
            "EMITTED": str(emitted), "DELIVERED": str(delivered),
            "PAGE_OK": "true" if page_ok else "false", "DRY_RUN": dry_run})
        return code, out, self.read_json("state/academic_delivery.json")

    def test_emitted_but_undelivered_fails_loudly_with_the_exact_string(self):
        code, out, rec = self.record(2, 1, True, titles=["Sem 1"])
        self.assertEqual(1, code, "an emitted-but-undelivered run MUST be red")
        self.assertIn("::error::academic delivery incomplete: 2 payload(s) "
                      "emitted, 1 confirmed by ntfy", out)
        self.assertFalse(rec["ok"])
        self.assertEqual("ntfy_incomplete", rec["reason"])
        self.assertEqual(2, rec["emitted"])
        self.assertEqual(1, rec["ntfy_delivered"])
        self.assertEqual(["Sem 1"], rec["payload_titles"])
        self.assertEqual(1, rec["schema_version"])
        self.assertIn("at_npt", rec)

    def test_all_delivered_and_page_rendered_is_green(self):
        code, _out, rec = self.record(2, 2, True)
        self.assertEqual(0, code)
        self.assertTrue(rec["ok"])
        self.assertEqual("delivered", rec["reason"])

    def test_page_failure_is_red_even_when_ntfy_took_everything(self):
        code, out, rec = self.record(2, 2, False)
        self.assertEqual(1, code)
        self.assertIn("::error::academic page render failed despite 2 payload(s)", out)
        self.assertEqual("page_render_failed", rec["reason"])

    def test_emitted_zero_asserts_nothing_and_stays_green(self):
        code, out, rec = self.record(0, 0, False)
        self.assertEqual(0, code, "a silent tick promised nothing")
        self.assertTrue(rec["ok"])
        self.assertEqual("nothing_emitted", rec["reason"])
        self.assertIn("nothing to assert", out)

    def test_dry_run_is_never_held_to_a_delivery_it_never_attempted(self):
        code, _out, rec = self.record(2, 0, True, dry_run="true")
        self.assertEqual(0, code)
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["dry_run"])

    def test_step_is_always_and_has_no_continue_on_error(self):
        for job, step, step_dict in W.iter_all_steps(ACADEMIC):
            if step == self.STEP:
                self.assertIn("always()", str(step_dict.get("if")))
                self.assertNotEqual("true",
                                    str(step_dict.get("continue-on-error") or "").lower(),
                                    "continue-on-error here would restore the "
                                    "green-but-undelivered outcome")
                return
        self.fail("the Assert academic delivery step is missing")


# ==========================================================================
# Contract 1d -- empty digests: kind, marker, self-seal, ladder
# ==========================================================================


class EmptyKindTests(unittest.TestCase):
    """The four frozen rows, straight from the real derive_empty_outcome."""

    @staticmethod
    def health(state, empty_hosts=None, failed=0, ok=1, total=1):
        return {"state": state,
                "critical": {"total": total, "ok": ok, "failed": failed,
                             "empty": len(empty_hosts or []),
                             "skipped": 0, "empty_hosts": list(empty_hosts or [])}}

    def test_produced_digest_has_no_kind(self):
        outcome, kind, evidence = vs.derive_empty_outcome(
            [{"t": 1}], [{"t": 1}], self.health("ok"))
        self.assertEqual("produced", outcome)
        self.assertIsNone(kind)
        self.assertIn("raw=1 final=1", evidence)

    def test_row_two_validator_dropped_everything_is_a_quiet_day(self):
        outcome, kind, _e = vs.derive_empty_outcome(
            [{"t": 1}, {"t": 2}, {"t": 3}], [], self.health("ok"))
        self.assertEqual("empty", outcome)
        self.assertEqual("quiet_day", kind)

    def test_row_three_no_raw_and_critical_hosts_were_empty_is_broken(self):
        outcome, kind, evidence = vs.derive_empty_outcome(
            [], [], self.health("ok", empty_hosts=["tu.edu.np"]))
        self.assertEqual("empty", outcome)
        self.assertEqual("broken", kind)
        self.assertIn("tu.edu.np", evidence)

    def test_row_three_degraded_state_is_broken(self):
        _o, kind, _e = vs.derive_empty_outcome([], [], self.health("degraded"))
        self.assertEqual("broken", kind)

    def test_row_three_empty_state_is_broken(self):
        _o, kind, _e = vs.derive_empty_outcome([], [], self.health("empty"))
        self.assertEqual("broken", kind)

    def test_unknown_state_is_broken_never_a_quiet_day(self):
        # Owner ruling: "we could not determine source health at all" is an
        # unmeasurable state, and an unmeasurable state is not evidence of a
        # quiet day.
        _o, kind, _e = vs.derive_empty_outcome([], [], self.health("unknown"))
        self.assertEqual("broken", kind)

    def test_row_four_sources_up_agent_returned_nothing_is_a_quiet_day(self):
        for state in ("ok", "partial"):
            with self.subTest(state=state):
                _o, kind, evidence = vs.derive_empty_outcome(
                    [], [], self.health(state))
                self.assertEqual("quiet_day", kind)
                self.assertIn("the agent returned nothing", evidence)

    def test_junk_input_never_raises(self):
        for raw, final, health in ((None, None, None), ([], [], {}),
                                   ([], [], {"state": 7, "critical": "no"}),
                                   ({"a": 1}, {"b": 2}, {"critical": {"empty_hosts": 5}})):
            with self.subTest(raw=raw):
                outcome, kind, evidence = vs.derive_empty_outcome(raw, final, health)
                self.assertIn(outcome, ("produced", "empty"))
                self.assertIn(kind, (None, "quiet_day", "broken"))
                self.assertIsInstance(evidence, str)


class DeliveryMarkerTests(WorkflowStepHarness):
    STEP = "Record delivery status"
    NOOP = "Fallback no-op check"

    def validated(self, count, kind=None, evidence="e"):
        """The validated payload, with a REAL list of `count` stories."""
        return {"stories": [{"title": "s%d" % i} for i in range(count)],
                "stories_outcome": "empty" if not count else "produced",
                "empty_kind": kind, "empty_evidence": evidence}

    def run_marker(self, count, channels=("ntfy",), kind="quiet_day",
                   date="2026-09-29", slot="morning", dry_run="false"):
        self.write("state/stories.validated.json", self.validated(count, kind))
        for name in channels:
            self.write("state/.%s_ok" % name, "true\n")
        code, out = self.run_step(DAILY, self.STEP, env={
            "DIGEST_TIME": slot, "TODAY": date, "DRY_RUN": dry_run})
        self.assertEqual(0, code, out)
        return self.read_json("state/last_digest_sent.json")

    def run_noop(self, marker, date="2026-09-29", slot="morning"):
        self.write("state/last_digest_sent.json", marker)
        code, out = self.run_step(DAILY, self.NOOP, env={
            "SLOT": slot, "TODAY": date})
        self.assertEqual(0, code, out)
        return self.outputs("gh_env.txt").get("FALLBACK_NOOP")

    # -- the marker --------------------------------------------------------
    def test_zero_stories_never_writes_delivered_true(self):
        marker = self.run_marker(0, channels=("ntfy", "website", "gmail", "telegram"))
        self.assertFalse(marker["delivered"],
                         "a channel delivered an EMPTY digest, not a digest")
        self.assertEqual(0, marker["stories"])
        self.assertTrue(marker["empty_digest"])
        self.assertEqual("quiet_day", marker["empty_kind"])
        self.assertTrue(marker["channels"]["ntfy"])

    def test_non_empty_digest_still_records_delivered(self):
        marker = self.run_marker(7)
        self.assertTrue(marker["delivered"])
        self.assertEqual(7, marker["stories"])
        self.assertFalse(marker["empty_digest"])
        self.assertFalse(os.path.exists(self.path("state/digest_retry.json")))

    def test_dry_run_forces_delivered_false_and_arms_no_ladder(self):
        marker = self.run_marker(7, dry_run="true")
        self.assertFalse(marker["delivered"])
        self.assertFalse(os.path.exists(self.path("state/digest_retry.json")))

    # -- the self-seal -----------------------------------------------------
    def test_zero_story_marker_cannot_arm_fallback_noop(self):
        """The second half of the self-seal, which no audit stated.

        delivered=true is exactly what a one-channel empty run used to write.
        With `stories > 0` as a hard term, that marker can no longer suppress
        the NEXT run's fallback.
        """
        marker = {"date": "2026-09-29", "time": "morning", "stories": 0,
                  "empty_digest": True, "delivered": True, "dry_run": False,
                  "channels": {"ntfy": True}}
        self.assertIsNone(self.run_noop(marker),
                          "FALLBACK_NOOP must not be armed by an empty digest, "
                          "even one that says delivered=true")

    def test_a_real_digest_still_suppresses_the_duplicate_fallback(self):
        marker = {"date": "2026-09-29", "time": "morning", "stories": 12,
                  "delivered": True, "dry_run": False, "channels": {"ntfy": True}}
        self.assertEqual("true", self.run_noop(marker),
                         "the dead-man's switch must keep working for a real digest")

    def test_dry_run_marker_and_legacy_marker_behaviour_is_unchanged(self):
        dry = {"date": "2026-09-29", "time": "morning", "stories": 12,
               "delivered": True, "dry_run": True}
        self.assertIsNone(self.run_noop(dry))
        legacy = {"date": "2026-09-29", "morning": "2026-09-29"}
        self.assertIsNone(self.run_noop(legacy),
                          "a legacy marker has no stories field: it must NOT "
                          "arm the no-op, or every upgraded run would "
                          "double-deliver")

    def test_the_whole_loop_end_to_end_stays_armed(self):
        """A 0-story run that reached ntfy still leaves the fallback armed."""
        self.run_marker(0, channels=("ntfy",))
        marker = self.read_json("state/last_digest_sent.json")
        marker["delivered"] = True  # the historical write
        self.assertIsNone(self.run_noop(marker))

    # -- broken input is not a quiet day -----------------------------------
    LADDER_SEED = {"schema_version": 1, "date": "2026-09-29", "slot": "morning",
                   "attempts": 1, "max_attempts": 3, "last_empty_kind": "quiet_day",
                   "started_at_npt": "2026-09-29 06:20",
                   "next_retry_at_npt": "2026-09-29 06:35"}

    def run_marker_with_payload(self, payload, date="2026-09-29", slot="morning"):
        """The marker step when the validated payload is NOT the happy shape.

        A pending ladder is pre-seeded every time, because 'the ladder was left
        alone' is the property under test. `payload=None` means the file is
        ABSENT; a dict or list is written as JSON, which is how the
        structural-error cases are expressed.
        """
        self.write("state/digest_retry.json", self.LADDER_SEED)
        if payload is not None:
            self.write("state/stories.validated.json", payload)
        self.write("state/.ntfy_ok", "true\n")
        code, out = self.run_step(DAILY, self.STEP, env={
            "DIGEST_TIME": slot, "TODAY": date, "DRY_RUN": "false"})
        self.assertEqual(0, code, out)
        return self.read_json("state/last_digest_sent.json"), out

    def assert_ladder_untouched(self, why):
        """Byte-identical, not merely still present.

        A readable-but-wrong payload used to be classified as a quiet day by
        `data.get("stories") or []`, which REWROTE this ladder (attempts+1,
        kind null) instead of deleting it -- so an `exists()` assertion would
        have passed against the broken code. Content equality is what actually
        pins "left alone".
        """
        self.assertTrue(os.path.exists(self.path("state/digest_retry.json")), why)
        self.assertEqual(self.LADDER_SEED,
                         self.read_json("state/digest_retry.json"), why)

    def test_a_missing_validated_file_does_not_clear_the_retry_ladder(self):
        """N6. The marker had no way to say 'I could not read the count', so an
        unreadable file fell through to the `not empty_digest` branch and
        CLEARED a pending ladder while reporting a 0-story digest. A broken
        input must not destroy the watchdog's only handoff.
        """
        marker, _out = self.run_marker_with_payload(None)
        self.assert_ladder_untouched(
            "an unreadable payload must leave the ladder alone")
        self.assertEqual(0, marker["stories"])
        self.assertFalse(marker["delivered"])
        self.assertFalse(marker["empty_digest"])

    def test_a_readable_payload_without_a_stories_key_is_not_a_quiet_day(self):
        """A file that PARSES but has no `stories` list is broken input.

        `data.get("stories") or []` used to turn it into a legitimate 0-story
        day -- empty_digest true and a ladder armed as quiet_day -- which is the
        same false-quiet-day class as N6. It must behave like an unreadable
        file instead.
        """
        marker, _out = self.run_marker_with_payload({"foo": 1})
        self.assert_ladder_untouched(
            "a structurally wrong payload must leave the ladder alone")
        self.assertEqual(0, marker["stories"])
        self.assertFalse(marker["empty_digest"])
        self.assertIsNone(marker["empty_kind"])
        self.assertFalse(marker["delivered"])

    def test_a_payload_whose_stories_key_is_not_a_list_is_not_a_quiet_day(self):
        marker, _out = self.run_marker_with_payload({"stories": {"a": 1}})
        self.assert_ladder_untouched("a non-list `stories` is broken input")
        self.assertEqual(0, marker["stories"])
        self.assertFalse(marker["empty_digest"])

    def test_a_bare_list_payload_is_not_a_quiet_day(self):
        """The validator never writes a bare list, so it is a shape error."""
        marker, _out = self.run_marker_with_payload([])
        self.assert_ladder_untouched("a bare list is broken input")
        self.assertFalse(marker["empty_digest"])

    def test_a_real_quiet_day_still_arms_the_ladder(self):
        """The counterpart that must NOT regress: `"stories": []` IS a quiet
        day, and the empty-digest RECAP path depends on it staying one. This one
        DOES rewrite the ladder, which is the opposite assertion on purpose.
        """
        marker, _out = self.run_marker_with_payload(
            {"stories": [], "stories_outcome": "empty",
             "empty_kind": "quiet_day", "empty_evidence": "raw=0 final=0"})
        self.assertTrue(marker["empty_digest"])
        self.assertEqual("quiet_day", marker["empty_kind"])
        ladder = self.read_json("state/digest_retry.json")
        self.assertNotEqual(self.LADDER_SEED, ladder,
                            "a real quiet day must advance the ladder")
        self.assertEqual(2, ladder["attempts"])
        self.assertEqual("quiet_day", ladder["last_empty_kind"])

    # -- broken input must make the RUN red, not just print a line ----------

    def test_broken_input_is_recorded_to_the_env_for_the_assert_step(self):
        """A self-explaining ::error:: in one step is not a red run.

        The marker step exports the reason so the end-of-run "Assert digest is
        not empty" step can fail the job on it. This is the first half of that
        chain: the flag is written for EVERY shape of broken input.
        """
        for payload, what in ((None, "an unreadable file"),
                              ({"foo": 1}, "a payload with no `stories` key"),
                              ({"stories": {"a": 1}}, "a non-list `stories`"),
                              ([], "a bare list")):
            with self.subTest(what=what):
                self.run_marker_with_payload(payload)
                recorded = self.outputs("gh_env.txt")
                self.assertEqual(
                    "true", recorded.get("STORIES_VALIDATED_BROKEN"),
                    "%s must record the broken-input flag, got %r"
                    % (what, recorded))

    def test_the_broken_input_flag_fails_the_assert_step(self):
        """The second half: the assert step honours the flag.

        Without this the annotation is decoration -- the run would end green on
        a digest nobody could build.
        """
        code, out = self.run_step(DAILY, "Assert digest is not empty", env={
            "DIGEST_TIME": "morning", "TODAY": "2026-09-29",
            "STORIES_VALIDATED_BROKEN": "true"})
        self.assertEqual(1, code, out)
        self.assertIn("BROKEN input", out)
        self.assertIn("UNKNOWN", out)

    def test_a_real_quiet_day_records_no_flag_and_is_not_called_broken(self):
        """The counterpart, and the one that must not regress.

        `"stories": []` IS a legitimate quiet day -- the empty-digest RECAP path
        depends on it staying one. It must not set the flag, and the assert step
        must still report the quiet-day reason rather than the broken-input one.
        """
        self.run_marker_with_payload(
            {"stories": [], "stories_outcome": "empty",
             "empty_kind": "quiet_day", "empty_evidence": "raw=0 final=0"})
        recorded = self.outputs("gh_env.txt")
        self.assertNotIn("STORIES_VALIDATED_BROKEN", recorded,
                         "a genuine quiet day must not be called broken input")

        self.write("state/last_digest_sent.json", {
            "stories": 0, "empty_kind": "quiet_day", "delivered": False})
        code, out = self.run_step(DAILY, "Assert digest is not empty", env={
            "DIGEST_TIME": "morning", "TODAY": "2026-09-29"})
        self.assertEqual(1, code, out)
        self.assertIn("quiet day", out)
        self.assertNotIn("BROKEN input", out)


class RetryLadderTests(WorkflowStepHarness):
    MARKER = "Record delivery status"
    DECIDE = "Decide whether to retry"
    ALERT = "Alert on the empty digest"

    def empty_run(self, date="2026-09-29", slot="morning", kind="quiet_day"):
        self.write("state/stories.validated.json",
                   {"stories": [], "stories_outcome": "empty",
                    "empty_kind": kind, "empty_evidence": "raw=0 final=0"})
        self.write("state/.ntfy_ok", "true\n")
        code, _out = self.run_step(DAILY, self.MARKER, env={
            "DIGEST_TIME": slot, "TODAY": date, "DRY_RUN": "false"})
        self.assertEqual(0, code)
        return self.read_json("state/digest_retry.json")

    def test_ladder_advances_attempts_and_spreads_the_retries(self):
        first = self.empty_run()
        self.assertEqual(1, first["attempts"])
        self.assertEqual(3, first["max_attempts"])
        self.assertEqual(1, first["schema_version"])
        self.assertEqual("quiet_day", first["last_empty_kind"])
        self.assertEqual("morning", first["slot"])
        self.assertEqual("2026-09-29", first["date"])
        self.assertEqual(15, self._gap(first))

        second = self.empty_run()
        self.assertEqual(2, second["attempts"])
        self.assertEqual(45, self._gap(second), "the second retry waits longer")
        self.assertEqual(first["started_at_npt"], second["started_at_npt"],
                         "the ladder's clock starts at the FIRST empty digest")

        third = self.empty_run()
        self.assertEqual(3, third["attempts"])
        self.assertEqual(45, self._gap(third))

    @staticmethod
    def _gap(entry):
        import datetime
        started = datetime.datetime.strptime(entry["started_at_npt"], "%Y-%m-%d %H:%M")
        nxt = datetime.datetime.strptime(entry["next_retry_at_npt"], "%Y-%m-%d %H:%M")
        return int((nxt - started).total_seconds() // 60)

    def test_a_new_slot_starts_a_fresh_ladder(self):
        self.empty_run(slot="morning")
        other = self.empty_run(slot="evening")
        self.assertEqual(1, other["attempts"], "the ladder is per slot")

    def test_a_digest_with_stories_clears_the_ladder(self):
        self.empty_run()
        self.assertTrue(os.path.exists(self.path("state/digest_retry.json")))
        self.write("state/stories.validated.json",
                   {"stories": [{"t": 1}], "stories_outcome": "produced",
                    "empty_kind": None, "empty_evidence": ""})
        code, out = self.run_step(DAILY, self.MARKER, env={
            "DIGEST_TIME": "morning", "TODAY": "2026-09-29", "DRY_RUN": "false"})
        self.assertEqual(0, code, out)
        self.assertFalse(os.path.exists(self.path("state/digest_retry.json")))
        self.assertTrue(self.read_json("state/last_digest_sent.json")["delivered"])

    # -- the watchdog's decision -------------------------------------------
    def decide(self, retry, marker=None):
        self.write("state/digest_retry.json", retry)
        if marker is not None:
            self.write("state/last_digest_sent.json", marker)
        code, out = self.run_step(WATCHDOG, self.DECIDE, env={})
        self.assertEqual(0, code, out)
        return self.outputs(), out

    def test_watchdog_waits_until_the_ladder_is_due(self):
        got, out = self.decide({"slot": "morning", "date": "2026-09-29",
                                "attempts": 1, "max_attempts": 3,
                                "next_retry_at_npt": "2099-01-01 00:00"})
        self.assertEqual("wait", got["act"])
        self.assertIn("waiting", out)

    def test_watchdog_dispatches_the_next_attempt_when_due(self):
        got, _out = self.decide({"slot": "morning", "date": "2026-09-29",
                                 "attempts": 1, "max_attempts": 3,
                                 "last_empty_kind": "quiet_day",
                                 "next_retry_at_npt": "2000-01-01 00:00"})
        self.assertEqual("retry", got["act"])
        self.assertEqual("2", got["attempt"])
        self.assertEqual("morning", got["slot"])

    def test_watchdog_stops_at_three_attempts(self):
        got, out = self.decide({"slot": "morning", "date": "2026-09-29",
                                "attempts": 3, "max_attempts": 3,
                                "next_retry_at_npt": "2000-01-01 00:00"})
        self.assertEqual("exhausted", got["act"],
                         "no 4th dispatch: the fallback ladder takes over")
        self.assertIn("ladder exhausted", out)

    def test_watchdog_does_not_retry_a_digest_that_recovered(self):
        got, out = self.decide(
            {"slot": "morning", "date": "2026-09-29", "attempts": 1,
             "max_attempts": 3, "next_retry_at_npt": "2000-01-01 00:00"},
            marker={"date": "2026-09-29", "time": "morning", "stories": 9,
                    "delivered": True})
        self.assertEqual("clear", got["act"])
        self.assertIn("now carries 9 stories", out)

    def test_watchdog_is_a_noop_without_a_ladder(self):
        code, out = self.run_step(WATCHDOG, self.DECIDE, env={})
        self.assertEqual(0, code, out)
        self.assertIn("no empty-digest ladder on record", out)
        self.assertEqual("no", self.outputs()["act"])

    def test_the_retired_ladder_is_deleted_from_the_repository(self):
        run = step_run_text(WATCHDOG, "Retire the exhausted ladder")
        self.assertIn("state/digest_retry.json", run)
        self.assertIn("rm -f state/digest_retry.json", run)
        self.assertIn("git rm --cached", run,
                      "a local delete that is never committed comes back on "
                      "the next checkout and re-alerts forever")

    # -- the severity split ------------------------------------------------
    def alert(self, kind, attempt, max_attempts="3"):
        posts = self.capture_posts()
        code, out = self.run_step(WATCHDOG, self.ALERT, env={
            "SLOT": "morning", "DATE": "2026-09-29", "KIND": kind,
            "ATTEMPT": str(attempt), "MAX_ATTEMPTS": max_attempts,
            "NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.invalid"})
        self.assertEqual(0, code, out)
        return out, posts

    def test_quiet_day_attempt_one_is_a_notice(self):
        out, posts = self.alert("quiet_day", 1)
        self.assertIn("::notice::", out)
        self.assertNotIn("::error::", out)
        self.assertEqual(3, posts[0]["priority"])
        self.assertIn("Digest: no stories (1/3) — sources were fine",
                      posts[0]["title"])

    def test_quiet_day_attempt_two_is_a_notice_and_says_retrying(self):
        out, posts = self.alert("quiet_day", 2)
        self.assertIn("::notice::", out)
        self.assertEqual(3, posts[0]["priority"])
        self.assertIn("(2/3) — retrying", posts[0]["title"])

    def test_broken_is_always_a_priority_five_error(self):
        """The severity never decays with the attempt number.

        THE 6H WINDOW SUPPRESSES THE PUSH, NEVER THE VERDICT. A broken digest is
        retried three times, so all three runs used to push; the first one now
        pushes for the whole window and the later two print `NOT pushed` while
        still annotating ::error:: and still failing their own run red. Asserting
        a push on every attempt is exactly the flood the window exists to stop,
        so this pins both halves instead of dropping the case.
        """
        posts = self.capture_posts()
        for attempt in (1, 2, 3):
            with self.subTest(attempt=attempt):
                before = len(posts)
                out, _ = self.alert("broken", attempt)
                self.assertIn("::error::", out,
                              "the annotation must survive suppression")
                if attempt == 1:
                    self.assertEqual(1, len(posts) - before)
                    self.assertEqual(5, posts[before]["priority"])
                    self.assertEqual("Digest BROKEN: sources returned nothing "
                                     "usable — fallback will fire",
                                     posts[before]["title"])
                else:
                    self.assertEqual(
                        0, len(posts) - before,
                        "a second push for the same unchanged cause inside the "
                        "6h window is the flood this change removes")
                    self.assertIn("NOT pushed", out)

    def test_third_attempt_is_the_falling_back_now_error(self):
        out, posts = self.alert("quiet_day", 3)
        self.assertIn("::error::", out)
        self.assertEqual(5, posts[0]["priority"])
        self.assertIn("0 stories after 3 attempts — FALLING BACK NOW",
                      posts[0]["title"])

    def test_no_new_delivery_channel_was_added(self):
        """The alert reuses the existing ntfy topic and nothing else."""
        run = step_run_text(WATCHDOG, self.ALERT)
        self.assertIn("NTFY_TOPIC", run)
        self.assertIn("NTFY_SERVER", run)
        for banned in ("smtp", "SMTP", "telegram", "TELEGRAM", "gmail"):
            self.assertNotIn(banned, run,
                             "the empty-digest alert must not invent a channel")


class EmptyDigestAlertLeadTests(RetryLadderTests):
    """THE LEAD SENTENCE OF EACH ALERT KIND, and its severity.

    WHY THIS CLASS EXISTS. The three titles and three priorities above are
    pinned, and they all passed against the OLD body text as well -- because a
    one-line body still carries the title and the priority. So nothing failed
    when the human-facing half of the alert was rewritten, and equally nothing
    would fail if it were rewritten BACK: the owner would go back to being
    told "produced 0 stories (empty_kind=quiet_day). Attempt 1 of 3." with no
    idea what it means for him, and no test would notice.

    So this pins the thing the owner actually reads: what HAPPENED, what it
    MEANS, and what happens NEXT -- with the engineer identifiers demoted to a
    later `Detail:` line rather than deleted.

    IT INHERITS RetryLadderTests rather than duplicating the `alert()` harness:
    the harness IS the contract being tested (it execs the real step body with
    a poisoned urlopen), and a second copy of it would be a second thing to
    drift. Only the two methods below are new.
    """

    #: What each kind must SAY. The wording is pinned in fragments rather than
    #: whole sentences so a legitimate rewording of a clause does not break the
    #: test, while removing the meaning of it does.
    LEADS = {
        "broken": [
            "could not be built",
            "returned nothing usable",
            "this is an outage, not a quiet news day",
            "will resend shortly",
        ],
        "exhausted": [
            "came back empty after 3 attempts",
            "not reaching you on the usual schedule",
            "is firing now and will resend shortly",
            "needs attention",
        ],
        "quiet_first": [
            "sources answered normally",
            "empty rather than broken",
            "Nothing to do",
        ],
        "quiet_retry": [
            "Still nothing from the sources",
            "still a quiet day rather than an outage",
            "Nothing to do",
        ],
    }

    #: Language that must appear ONLY in a priority-5 alert, phrased as the
    #: ASSERTIVE claim the owner would read as "this is broken". A p3 that
    #: asserts any of it trains the owner to treat a quiet news day as an
    #: emergency, and that is how a real p5 stops being read.
    #:
    #: DELIBERATELY NOT the bare word "outage". The p3 retry lead says
    #: "this is still a quiet day RATHER THAN an outage" -- a negation, and the
    #: honest way to distinguish two causes that look identical from the
    #: outside. Asserting the bare word would have forced that useful sentence
    #: to be deleted, which would be a worse alert. The rule is therefore about
    #: the CLAIM, and the negation case is pinned separately below.
    P5_ONLY = ("BROKEN", "FALLING BACK", "needs attention",
               "could not be built", "this is an outage",
               "is not reaching you")

    def _body(self, kind, attempt):
        # `capture_posts` appends to a list that lives for the whole test, so a
        # second call in the same test would see the first call's POST too.
        # Cleared here rather than by rewriting the inherited harness, because
        # the harness is shared with the frozen title/priority tests above.
        self._postings = []
        _out, posts = self.alert(kind, attempt)
        self.assertEqual(1, len(posts),
                         "the alert must POST exactly once, got %d" % len(posts))
        return posts[0]

    def _assert_lead(self, kind, attempt, priority, fragments):
        post = self._body(kind, attempt)
        self.assertEqual(priority, post["priority"])
        message = post["message"]
        for fragment in fragments:
            self.assertIn(fragment, message,
                          "the %s alert no longer says %r. Body was:\n%s"
                          % (kind, fragment, message))
        # The consequence comes FIRST: on a phone the collapsed notification
        # shows only the opening of the body.
        lead, detail = self._split(message)
        for fragment in fragments:
            self.assertIn(fragment, lead,
                          "the consequence %r was demoted below the Detail line. "
                          "The owner sees the first line on his phone."
                          % fragment)
        self.assertTrue(detail.strip(), "the Detail line went missing entirely")
        return message

    @staticmethod
    def _split(message):
        head, sep, tail = message.partition("\n\nDetail:")
        return head, (tail if sep else "")

    # -- the four kinds ----------------------------------------------------

    def test_the_broken_alert_leads_with_the_outage_not_the_internal_kind(self):
        message = self._assert_lead("broken", 1, 5, self.LEADS["broken"])
        self.assertIn("Detail: slot=morning", message,
                      "the run's own identifiers must survive below the lead")
        self.assertIn("empty_kind=broken", message)
        self.assertIn("attempt=1 of 3", message)
        # The internal enum value is a diagnostic, never the headline.
        self.assertFalse(message.startswith("empty_kind="),
                         "the body opens with an internal identifier")

    def test_the_exhausted_alert_says_the_fallback_is_firing_now(self):
        message = self._assert_lead("quiet_day", 3, 5, self.LEADS["exhausted"])
        self.assertIn("Detail: slot=morning", message)
        self.assertIn("attempt=3 of 3", message)

    def test_the_first_quiet_attempt_says_it_is_a_quiet_day(self):
        message = self._assert_lead("quiet_day", 1, 3, self.LEADS["quiet_first"])
        self.assertIn("Detail: slot=morning", message)
        self.assertIn("attempt=1 of 3", message)

    def test_a_later_quiet_attempt_still_says_quiet_not_outage(self):
        message = self._assert_lead("quiet_day", 2, 3, self.LEADS["quiet_retry"])
        self.assertIn("Detail: slot=morning", message)
        self.assertIn("attempt=2 of 3", message)

    # -- the severity must stay readable -----------------------------------

    def test_a_p3_states_the_quiet_day_cause_and_never_claims_the_outage(self):
        """THE ANTI-CRYING-WOLF ASSERTION, over every p3 the step can emit.

        This is the part that makes the friendlier wording safe. The leads were
        rewritten to be readable, and the failure mode of readable text is that
        every alert now sounds urgent -- at which point a priority 5 is no
        longer distinguishable and the owner learns to dismiss all of them.
        The severity split is therefore asserted from the WORDS, not only from
        the priority field.

        POSITIVE AND NEGATIVE IN ONE TEST, deliberately. A test that only
        forbids p5 words in a p3 body is satisfied by an EMPTY body -- and the
        reverted wording produced exactly that: one line with no cause stated
        at all, which is what the owner complained about. The positive half
        (the p3 must name the quiet-day cause) is what makes this test fail on
        the old wording rather than pass on it.

        `P5_ONLY` says "this is an outage" and never the bare word "outage",
        because the retry lead distinguishes two causes that look identical
        from the phone by saying one is ruled out ("rather than an outage").
        Asserting the bare word forbidden would have made that honest sentence
        impossible to write; the allowance is pinned as a negation below so it
        cannot quietly become a claim.
        """
        for attempt, cause in ((1, "quiet news day"), (2, "quiet day")):
            with self.subTest(attempt=attempt):
                message = self._body("quiet_day", attempt)["message"]
                self.assertIn(cause, message.lower(),
                              "the p3 lead does not name the quiet-day cause at "
                              "all, so the owner cannot tell it from an outage: "
                              "%r" % message)
                for banned in self.P5_ONLY:
                    self.assertNotIn(
                        banned, message,
                        "a priority-3 quiet-day alert says %r. This is exactly "
                        "what makes a real priority 5 stop being read.\n%s"
                        % (banned, message))
                lead, _detail = self._split(message)
                if "outage" in lead.lower():
                    self.assertIn(
                        "rather than an outage", lead.lower(),
                        "the p3 lead mentioned an outage without ruling it "
                        "out: %r" % lead)

    def test_a_priority_five_alert_does_use_its_own_language(self):
        """The other half, or the test above would pass on an empty body."""
        for kind, attempt in (("broken", 1), ("quiet_day", 3)):
            with self.subTest(kind=kind, attempt=attempt):
                post = self._body(kind, attempt)
                self.assertEqual(5, post["priority"])
                self.assertTrue(
                    any(w in post["message"] for w in self.P5_ONLY),
                    "a priority-5 alert used none of the words that make it a "
                    "priority 5: %r" % post["message"])

    def test_each_kind_gets_its_own_sentence_not_a_swapped_enum(self):
        """Four causes, four sentences -- not one template with a swapped noun.

        THE DISCRIMINATING VERSION of the distinctness claim. Comparing the raw
        bodies is not enough: the reverted wording produced four DIFFERENT
        strings too, because each one interpolated `empty_kind=` and a different
        attempt number, and a plain `assertNotEqual` passed against it -- a
        decoration.

        So the identifiers and the numbers are normalised away first, and the
        SENTENCES are compared. Under the reverted wording all four collapse to
        the same normalised text ("... digest for ... (NPT) produced 0 stories
        (empty_kind=). Attempt  of ."), because the only thing that varied was
        the enum value. That is precisely the defect: the owner got one sentence
        with a machine value substituted into it.
        """
        import re as _re

        def normalise(message):
            lead, _detail = self._split(message)
            lead = _re.sub(r"empty_kind=\w+", "empty_kind=", lead)
            lead = _re.sub(r"\d+", "N", lead)
            return " ".join(lead.split())

        leads = {
            "broken": normalise(self._body("broken", 1)["message"]),
            "exhausted": normalise(self._body("quiet_day", 3)["message"]),
            "quiet_first": normalise(self._body("quiet_day", 1)["message"]),
            "quiet_retry": normalise(self._body("quiet_day", 2)["message"]),
        }
        for a, b in (("broken", "exhausted"),
                     ("quiet_first", "quiet_retry"),
                     ("broken", "quiet_first"),
                     ("exhausted", "quiet_first"),
                     ("exhausted", "quiet_retry"),
                     ("broken", "quiet_retry")):
            with self.subTest(pair=(a, b)):
                self.assertNotEqual(
                    leads[a], leads[b],
                    "the %s and %s alerts reduce to the SAME sentence once the "
                    "identifiers are normalised away, so the owner is reading "
                    "one template with a value substituted into it.\n"
                    "  %s: %r\n  %s: %r" % (a, b, a, leads[a], b, leads[b]))

    def test_the_engineer_identifiers_are_demoted_not_deleted(self):
        """Demoted, per the brief. Nothing diagnostic was dropped.

        Asserted on the RENDERED body, not the source, because the claim is
        about what the owner receives. Asserting on the source would pass
        against the reverted wording (which still interpolates every
        identifier) and would prove nothing about the notification.
        """
        for kind, attempt in (("broken", 1), ("quiet_day", 1), ("quiet_day", 3)):
            with self.subTest(kind=kind, attempt=attempt):
                message = self._body(kind, attempt)["message"]
                # `attempt=N of M` is the rendered form of the attempt and
                # max_attempts pair; the Python variable names never reach the
                # body, so the assertion is on what the owner actually reads.
                for identifier in ("slot=", "date=", "empty_kind=",
                                   "attempt=%d" % attempt, "of 3"):
                    self.assertIn(
                        identifier, message,
                        "the diagnostic %r was dropped from the %s alert body"
                        % (identifier, kind))
                _lead, detail = self._split(message)
                self.assertTrue(detail.strip(),
                                "the Detail line went missing from the %s "
                                "alert: %r" % (kind, message))
                for identifier in ("slot=", "date=", "empty_kind="):
                    self.assertNotIn(
                        identifier, _lead,
                        "%r is in the LEAD of the %s alert. The identifiers are "
                        "demoted, not deleted -- the first line is what the "
                        "phone shows." % (identifier, kind))


class DigestAssertTests(WorkflowStepHarness):
    STEP = "Assert digest is not empty"

    def assert_step(self, marker, retry=None, slot="morning", date="2026-09-29"):
        self.write("state/last_digest_sent.json", marker)
        if retry is not None:
            self.write("state/digest_retry.json", retry)
        return self.run_step(DAILY, self.STEP, env={
            "DIGEST_TIME": slot, "TODAY": date, "RETRY_OF": ""})

    def test_zero_story_run_is_red_and_says_the_fallback_stays_armed(self):
        code, out = self.assert_step(
            {"date": "2026-09-29", "time": "morning", "stories": 0,
             "delivered": False, "empty_digest": True, "empty_kind": "quiet_day"},
            retry={"attempts": 2, "max_attempts": 3})
        self.assertEqual(1, code, "the run must end RED")
        self.assertIn("::error::digest is empty", out)
        self.assertIn("FALLBACK_NOOP is NOT armed", out)
        self.assertIn("attempt 2 of 3", out)

    def test_broken_digest_is_named_as_broken(self):
        code, out = self.assert_step(
            {"date": "2026-09-29", "time": "morning", "stories": 0,
             "delivered": False, "empty_digest": True, "empty_kind": "broken"})
        self.assertEqual(1, code)
        self.assertIn("::error::digest BROKEN", out)

    def test_a_real_digest_asserts_nothing_and_stays_green(self):
        code, out = self.assert_step(
            {"date": "2026-09-29", "time": "morning", "stories": 14,
             "delivered": True, "empty_digest": False, "empty_kind": None})
        self.assertEqual(0, code)
        self.assertIn("nothing to assert", out)

    def test_step_is_not_an_always_step(self):
        for _job, step, step_dict in W.iter_all_steps(DAILY):
            if step == self.STEP:
                self.assertNotIn("always()", str(step_dict.get("if")))
                return
        self.fail("the Assert digest is not empty step is missing")

    def test_validate_exit_two_is_an_error_not_a_warning(self):
        run = step_run_text(DAILY, "Validate stories")
        self.assertIn("::error::No stories survived validation", run)
        self.assertNotIn("::warning::No stories survived validation", run)
        self.assertIn("VALIDATE_EXIT=2", run)


class SourceHealthRow2bTests(unittest.TestCase):
    """The owner-ruled row 2b threshold: >= 2 non-dns failures on ONE host."""

    @staticmethod
    def row(host, outcome, error=None, items=3, category="education_tu"):
        return {"name": "notice:%s:%s" % (host, len(str(error or outcome))),
                "host": host, "category": category, "kind": "notice",
                "url": "https://%s/x" % host, "outcome": outcome,
                "error": error, "items_found": items}

    def health(self, results, crit):
        import source_health as sh
        return sh.compute_source_health(results, crit, "2026-09-29 07:50")

    def test_one_non_dns_failure_on_a_critical_host_stays_quiet(self):
        crit = ["news.google.com", "tu.edu.np"]
        results = [self.row("news.google.com", "failed", "HTTP Error 503"),
                   self.row("news.google.com", "ok", items=4)]
        h = self.health(results, crit)
        self.assertEqual([], h["critical"]["hosts_down"],
                         "the host still answers, so row 2 cannot see it")
        self.assertEqual("partial", h["state"],
                         "ONE flaky sweep must not flip the digest to degraded")

    def test_two_non_dns_failures_on_the_same_critical_host_are_degraded(self):
        crit = ["news.google.com", "tu.edu.np"]
        results = [self.row("news.google.com", "failed", "HTTP Error 503"),
                   self.row("news.google.com", "failed", "HTTP Error 500"),
                   self.row("news.google.com", "ok", items=4)]
        h = self.health(results, crit)
        self.assertEqual("degraded", h["state"])
        self.assertEqual(2, h["critical"]["failed"])
        self.assertEqual([], h["critical"]["hosts_down"],
                         "the host is reachable, which is why row 2 misses it")

    def test_two_non_dns_failures_on_different_critical_hosts_stay_quiet(self):
        crit = ["news.google.com", "tu.edu.np", "iost.tu.edu.np"]
        results = [self.row("news.google.com", "failed", "HTTP Error 503"),
                   self.row("news.google.com", "ok", items=4),
                   self.row("tu.edu.np", "failed", "certificate verify failed"),
                   self.row("tu.edu.np", "ok", items=3)]
        h = self.health(results, crit)
        self.assertEqual("partial", h["state"],
                         "the threshold is PER HOST, never a sum across hosts")

    def test_dns_failures_never_trigger_row_2b(self):
        crit = ["news.google.com", "tu.edu.np"]
        results = [self.row("news.google.com", "failed", "getaddrinfo failed"),
                   self.row("news.google.com", "failed", "getaddrinfo failed")]
        # Both dns and no productive sibling -> the host is entirely dark,
        # which is row 2's own trigger, not row 2b.
        self.assertEqual("degraded", self.health(results, crit)["state"])
        results = [self.row("news.google.com", "failed", "getaddrinfo failed"),
                   self.row("news.google.com", "failed", "getaddrinfo failed"),
                   self.row("news.google.com", "ok")]
        self.assertEqual("partial", self.health(results, crit)["state"],
                         "a dark-then-recovered host is a blip, and dns is the "
                         "reason code row 2b deliberately ignores")

    def test_empty_plus_failed_critical_hosts_is_still_degraded(self):
        """Clause (b) of row 2b, unchanged by the >= 2 threshold.

        NOTE WHAT DOES NOT reach clause (b): a critical host that is
        ENTIRELY empty. That is row 3's `empty` verdict, it fires first, and
        it is a better answer than "some sources were unreachable" -- which
        is what tests/collection/test_source_health.py freezes. Clause (b)
        only carries the case row 3 cannot see: critical hosts that are mute
        (empty) while ALSO having a productive task, next to critical hosts
        that failed. Half the critical set answering with nothing and the
        other half erroring is an outage no hosts_down shield can catch.
        """
        crit = ["news.google.com", "tu.edu.np", "iost.tu.edu.np"]
        results = [self.row("iost.tu.edu.np", "empty", items=0),
                   self.row("iost.tu.edu.np", "ok", items=4),
                   self.row("tu.edu.np", "failed", "getaddrinfo failed"),
                   self.row("tu.edu.np", "ok", items=3),
                   self.row("news.google.com", "ok", items=6)]
        h = self.health(results, crit)
        self.assertEqual(1, h["critical"]["empty"])
        self.assertEqual(1, h["critical"]["failed"])
        self.assertEqual([], h["critical"]["empty_hosts"],
                         "iost has a productive sibling, so it is not an "
                         "actionable-empty host")
        self.assertEqual([], h["critical"]["hosts_down"],
                         "tu also has a productive sibling")
        self.assertEqual("degraded", h["state"], "clause (b) fires")

    def test_an_entirely_empty_critical_host_still_reads_empty_not_degraded(self):
        crit = ["news.google.com", "tu.edu.np", "iost.tu.edu.np"]
        results = [self.row("iost.tu.edu.np", "empty", items=0),
                   self.row("news.google.com", "ok", items=6),
                   self.row("tu.edu.np", "failed", "getaddrinfo failed"),
                   self.row("tu.edu.np", "ok", items=3)]
        self.assertEqual("empty", self.health(results, crit)["state"],
                         "row 3 wins over row 2b, exactly as the frozen test "
                         "in tests/collection requires")

    def test_row_2b_sits_after_empty(self):
        import source_health as sh
        self.assertEqual(2, sh.NON_DNS_FAILURE_THRESHOLD)
        self.assertEqual(("dns",), sh.DNS_FAILURE_REASONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
