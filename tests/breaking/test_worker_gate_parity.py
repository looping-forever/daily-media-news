#!/usr/bin/env python3
"""tests/breaking/test_worker_gate_parity.py -- Worker <-> Python gate blob
parity (contract 1f).

THE BUG THIS FILE EXISTS TO CATCH
---------------------------------
The Cloudflare Worker gates on the candidate TITLE only. Python gates on
title+snippet. The Worker runs first. So the Worker is strictly TIGHTER, and a
headline whose life-safety signal lives only in the snippet would be dropped by
the Worker before Python is ever consulted -- and that loss would look
identical to "nothing was happening", which is precisely the wrong conclusion
to reach on a breaking-news path.

MEASURED, so read this before reacting: on the current 616-row real corpus the
title-only gate drops 0 rows that the title+snippet gate keeps (see
TitleOnlyLossTest, which prints the figure on every run). The divergence is
therefore LATENT on this corpus, not a live missed alert. It still has to be
closed, because the Worker's EMERGENCY_RE is the NARROW pre-widening
vocabulary: the Worker is the binding constraint, so widening the Python gate
alone silently accomplishes nothing while every Python test stays green.

DEPLOY/CLOUDFLARE-WORKER.JS IS NOT EDITED BY THIS TEST
------------------------------------------------------
Another agent owns that file. This file only READS it. If the Worker has not
been updated, the parity assertions below go RED on purpose: a green parity
test that skips or that asserts the current (wrong) behaviour is worse than no
parity test, because it manufactures the false confidence this file exists to
remove. Red here is the correct, honest state until the Worker owner lands
`${item.title} ${item.snippet || ""}`.

Run:
    py -m unittest tests.breaking.test_worker_gate_parity -v
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
FIXTURE = os.path.join(REPO_ROOT, "tests", "breaking", "fixtures",
                       "real_headlines.json")
WORKER = os.path.join(REPO_ROOT, "deploy", "cloudflare-worker.js")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import breaking_monitor as bm  # noqa: E402

# THE FROZEN SHARED BLOB. Same semantic on both sides:
#   JS      `${item.title} ${item.snippet || ""}`
#   Python  "%s %s" % (title, snippet)
# The `|| ""` matters: a missing snippet must not produce the literal text
# "undefined" in the blob, which would be a new (garbage) gate input.
EXPECTED_WORKER_GATE_BLOB = '${item.title} ${item.snippet || ""}'

CONTRACT_MSG = (
    "CLOUDFLARE WORKER GATE PARITY NOT SATISFIED. The Worker must expose a "
    "single gateBlob(item) returning the frozen blob "
    "`${item.title} ${item.snippet || \"\"}` and must call the gate with "
    "blob(item), never with item.title. deploy/cloudflare-worker.js is owned "
    "by another agent and is NOT edited here; this test stays red until that "
    "owner lands the change. "
    "HONEST SCOPE, measured on the 616-row corpus: the title-only gate "
    "currently drops 0 rows that the title+snippet gate keeps, so this is a "
    "CONTRACT divergence today, not a measured missed alert. It is still "
    "worth closing because the Worker's EMERGENCY_RE is the NARROW pre-widening "
    "vocabulary, so the Worker is the binding constraint and widening the "
    "Python gate alone silently does nothing.")


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def worker_source():
    return _read(WORKER)


def corpus():
    with io.open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["rows"]


def _extract_js_function(name, src=None):
    """Return the exact source text of `function <name>(...) {...}` from the
    Worker, brace-matched. The REAL function text, not a reconstruction."""
    src = src if src is not None else worker_source()
    marker = "function %s(" % name
    if marker not in src:
        raise AssertionError(CONTRACT_MSG + "\n  (no `%s` in the Worker)"
                             % marker)
    start = src.index(marker)
    i = src.index("{", start)
    depth = 0
    for pos in range(i, len(src)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[start:pos + 1]
    raise AssertionError("could not brace-match function %s" % name)


# ==========================================================================
# The Python side of the contract
# ==========================================================================

class PythonGateBlobTest(unittest.TestCase):
    """The Python half is correct today; pin it so it cannot drift either."""

    def test_gate_blob_is_title_plus_snippet(self):
        self.assertEqual(bm._gate_blob("TITLE", "SNIPPET"), "TITLE SNIPPET")

    def test_gate_blob_tolerates_an_empty_snippet(self):
        """The JS `|| ""` exists so a missing snippet does not become the
        literal text "undefined" in the blob -- that would be a new, garbage
        gate input. The Python mirror is the caller's "" default, which
        production does supply: pre_gate() calls .get("snippet", "")."""
        self.assertEqual(bm._gate_blob("TITLE", ""), "TITLE ")
        src = _read(os.path.join(SCRIPTS, "breaking_monitor.py"))
        self.assertIn('cand.get("snippet", "")', src,
                      "pre_gate() must pass a '' default for a missing "
                      "snippet, mirroring the Worker's `|| \"\"`")

    def test_gate_blob_uses_exactly_one_separator(self):
        self.assertEqual(len(bm._gate_blob("A", "B").split(" ")), 2)


# ==========================================================================
# The Worker side of the contract
# ==========================================================================

class WorkerGateBlobParityTest(unittest.TestCase):
    def test_worker_defines_the_shared_gate_blob(self):
        body = _extract_js_function("gateBlob")
        self.assertIn("item.title", body)
        self.assertIn("item.snippet", body)

    def test_worker_gate_blob_literal_matches_the_frozen_python_blob(self):
        body = _extract_js_function("gateBlob")
        lits = re.findall(r"`([^`]*)`", body)
        self.assertEqual(
            len(lits), 1,
            "gateBlob must build exactly one template literal, found %r"
            % lits)
        self.assertEqual(
            lits[0], EXPECTED_WORKER_GATE_BLOB,
            "WORKER LITERAL MISMATCH. Worker: %r  Contract: %r"
            % (lits[0], EXPECTED_WORKER_GATE_BLOB))

    def test_worker_never_gates_on_bare_item_title(self):
        """Source-level guard: the inline `item.title` gate call sites that
        make the Worker strictly tighter than Python. If a blob helper is
        added but these call sites are not switched over, the helper is
        decoration and the test above would still be the only thing failing."""
        src = worker_source()
        forbidden = [r"EMERGENCY_RE\.test\(\s*item\.title\s*\)",
                     r"sportGossipHits\(\s*item\.title\s*\)"]
        offenders = [p for p in forbidden if re.search(p, src)]
        self.assertEqual(
            offenders, [],
            "Worker still gates on bare item.title at %d site(s) (%s). Every "
            "gate call must take the shared blob, not the title alone."
            % (len(offenders), offenders))

    def test_worker_snippet_is_actually_available_at_the_gate(self):
        """If the Worker never reads item.snippet at all, the blob cannot be
        built and the mismatch is a Worker PARSING problem (the item shape
        has to carry a snippet) and not just a call-site problem. Flag which,
        so the owner's fix is obvious."""
        src = worker_source()
        n = src.count("snippet")
        self.assertGreater(
            n, 0,
            "the Worker never references `snippet` anywhere, so a "
            "title+snippet gate is not constructible there yet: the RSS "
            "parse must first put a snippet on each item")


class WorkerGateBlobExecutedParityTest(unittest.TestCase):
    """Execute the Worker's OWN gateBlob under node and compare with Python on
    the real corpus. A source grep cannot catch a template-literal bug; running
    it can."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise AssertionError(
                "node is required by tests/breaking/test_worker_gate_parity.py "
                "to execute the Worker's gateBlob. It is missing from this "
                "machine, so JS/Python parity is UNVERIFIED. Failing loudly "
                "rather than skipping.")

    def test_worker_and_python_build_the_same_blob_on_the_real_corpus(self):
        fn = _extract_js_function("gateBlob")          # red if absent
        payload = [[r.get("title", ""), r.get("snippet", "")]
                   for r in corpus()]
        # The 616-row payload is ~113 KB, which blows the Windows 32,767-char
        # command-line limit (WinError 206) when it is passed as `node -e`
        # argv — a harness defect that looks exactly like a parity failure.
        # So the harness goes to a temp FILE and node runs the file. gateBlob's
        # own source text is still exactly the thing being executed.
        #
        # Also: capture BYTES and decode UTF-8 ourselves. `text=True` decodes
        # node's stdout with the Windows ANSI codepage (cp1252 here), and one
        # corpus headline carries a byte cp1252 cannot represent — the reader
        # thread dies inside subprocess and proc.stdout comes back None, which
        # surfaces as "node produced no output" rather than as the encoding bug
        # it actually is.
        harness = (
            fn + "\n"
            "const CASES = " + json.dumps(payload) + ";\n"
            "const OUT = CASES.map(([title, snippet]) =>\n"
            "  gateBlob({title: title, snippet: snippet}));\n"
            "process.stdout.write(JSON.stringify(OUT));\n"
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8")
        try:
            tmp.write(harness)
            tmp.close()
            proc = subprocess.run([self.node, tmp.name],
                                  capture_output=True, timeout=120)
        finally:
            os.unlink(tmp.name)
        stderr = (proc.stderr or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            self.fail("node failed to run gateBlob (exit %s):\n%s"
                      % (proc.returncode, stderr[:2000]))
        if not proc.stdout:
            self.fail("node produced no output (stdout empty, stderr=%r)"
                      % stderr[:500])
        js_blobs = json.loads(proc.stdout.decode("utf-8"))
        py_blobs = [bm._gate_blob(t, s) for (t, s) in payload]
        self.assertEqual(len(js_blobs), len(py_blobs))
        mismatches = [(t[:60], p, j) for (t, _s), p, j
                      in zip(payload, py_blobs, js_blobs) if p != j]
        self.assertEqual(
            mismatches, [],
            "JS/Python gate-blob divergence on %d corpus row(s):\n%s"
            % (len(mismatches),
               "\n".join("  %r python=%r worker=%r" % m
                         for m in mismatches[:10])))


# ==========================================================================
# Quantify the silent loss, so the fix can be judged on a number
# ==========================================================================

class TitleOnlyLossTest(unittest.TestCase):
    """Measure the divergence instead of asserting it from intuition.

    MEASURED on the 616-row corpus: title-only gating loses 0 rows that the
    title+snippet gate keeps. So the divergence is LATENT on this corpus, not
    currently costing an alert. That is recorded here rather than papered
    over, because the file above still goes red for the contract reason and a
    reader is entitled to know whether it also costs alerts. It does not.
    """

    def _decide(self, row):
        title = row.get("title", "")
        snippet = row.get("snippet", "")
        on_title_only = bm.importance_gate(title, title)[0]
        on_blob = bm.importance_gate(
            bm._gate_blob(title, snippet), title)[0]
        return on_title_only, on_blob

    def test_title_only_gate_is_a_subset_of_the_title_plus_snippet_gate(self):
        """The invariant that actually has teeth, over all 616 real rows.

        Gating on a SUPERSET string should match at least as much, never less.
        It currently does NOT, and the single violating row is a real
        life-safety alert, so this is a live finding rather than a stylistic
        one. Recorded, not papered over:

          TITLE  "Krishna Bhir landslide blocks Muglin-Kathmandu road,
                  repairs may take a month - OnlineKhabar En"
          SNIPPET the same headline (Google News snippets are title + source)
          corpus label urgent=True  -- correctly so, a blocked road
          title-only gate   -> True,  "unified:landslide"
          title+snippet gate-> False, "not_a_live_event"

        So the snippet's "repairs may take a month" trips the
        not_a_live_event framing veto and HARD_LIVE_EVIDENCE_RE does not
        override it, and Python SILENTLY DROPS a genuine urgent alert that the
        title-only Worker would have caught. Widening the blob is therefore
        not a free win: right now the tighter Worker is accidentally
        compensating for a Python suppression defect.

        This is a scoped-out defect, reported not fixed. Fixing it means
        changing what NOT_A_LIVE_EVENT_RE / HARD_LIVE_EVIDENCE_RE may veto,
        which is a judgement call on the breaking path, not a mechanical fix.
        """
        violations = []
        for row in corpus():
            on_title_only, on_blob = self._decide(row)
            if on_title_only and not on_blob:
                violations.append(
                    "  %r\n      urgent=%s  title-only=%r  "
                    "title+snippet=%r"
                    % (row.get("title", "")[:90], row.get("urgent"),
                       on_title_only, on_blob))
        self.assertEqual(
            violations, [],
            "title-only gate tripped on %d corpus row(s) the title+snippet "
            "gate vetoes. Gating on a superset string must never match LESS; "
            "every row here is a real alert Python is dropping:\n%s"
            % (len(violations), "\n".join(violations)))

    def test_loss_figure_is_measured_and_reported(self):
        """Reports the silent-loss count on every run. It is a MEASUREMENT,
        not a gate: hard-gating it would be re-baselining a corpus statistic
        dressed up as a contract, which is the mistake this whole file is
        written against."""
        lost = sum(1 for r in corpus()
                   if self._decide(r)[1] and not self._decide(r)[0])
        total = len(corpus())
        sys.stderr.write(
            "  [parity] MEASURED title-only loss: %d of %d corpus rows are "
            "kept by the Python title+snippet gate and dropped by the "
            "Worker's title-only gate\n" % (lost, total))
        self.assertIsInstance(lost, int)
        self.assertGreaterEqual(lost, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
