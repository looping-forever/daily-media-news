#!/usr/bin/env python3
"""tests/breaking/test_breaking_chain.py â€” the breaking chain END TO END.

WHAT THIS FILE EXISTS FOR
-------------------------
The breaking-news chain had NEVER delivered an alert, in the entire history of
the repository. Every layer reported itself healthy while it did nothing:

  * the Worker's pre-gate matched 13 literal phrases while a comment claimed it
    mirrored `importance_gate`, which matched a 378-row vocabulary -- so the
    Worker, which runs FIRST, was the binding constraint and dropped ~93% of
    real headlines;
  * `breaking-monitor.yml` had no `schedule:` at all (retired 2bd8eda), so with
    `repository_dispatch` never firing, the workflow had no reachable trigger;
  * `digest-watchdog.yml` asserted only that the CRON HEARTBEAT was fresh, which
    is true while the gate matches nothing;
  * `ai_fail_safe` wrote to a gitignored file, so a broken honesty gate was
    indistinguishable from a quiet news day.

Four layers, four ways of being silently dead. So this suite covers all four,
and each part is written to FAIL if its layer regresses.

HOW IT AVOIDS THE THING THAT CAUSED THE BUG
-------------------------------------------
The original defect was TWO COPIES of one gate drifting apart silently. This
file therefore does not re-implement the Worker gate in Python: it EXTRACTS the
Worker's own functions and constants, runs them under node over the frozen
616-row corpus, and compares the result to Python's own `importance_gate` row by
row. A Python stand-in would drift exactly the way the real code did, and a
parity test that compares a copy to itself is worse than no parity test.

REPRODUCING THE RED (never revert the real repo to do this):

    # a deliberately narrow copy of the Worker, then judge IT instead
    py -m unittest tests.breaking.test_breaking_chain \
        2>&1 | tail -5

`WORKER_GATE_WORKER` points the parity test at any worker file. Set it to a
copy whose gate has been reverted to the 13-phrase `EMERGENCY_RE` and
TestWorkerGateMatchesPython goes RED with the real divergence numbers.

Run:
    py -m unittest tests.breaking.test_breaking_chain -v
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
FIXTURE = os.path.join(REPO_ROOT, "tests", "breaking", "fixtures",
                       "real_headlines.json")
WORKER_DEFAULT = os.path.join(REPO_ROOT, "deploy", "cloudflare-worker.js")
WATCHDOG = os.path.join(REPO_ROOT, ".github", "workflows",
                        "digest-watchdog.yml")
BREAKING_WF = os.path.join(REPO_ROOT, ".github", "workflows",
                           "breaking-monitor.yml")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import breaking_monitor as bm  # noqa: E402


def _arg_namespace(**kwargs):
    """notify_ntfy reads exactly one attribute of `args` (dry_run). Building the
    namespace here rather than running the full CLI keeps the assertion about
    the receipt, not about argument parsing."""
    import types
    return types.SimpleNamespace(**kwargs)


def worker_path():
    """The Worker under test. Overrideable ONLY so the red case can be
    reproduced without editing the repository -- never as a way to make a
    failure disappear, because a mismatch here is a failure, not a skip."""
    return os.environ.get("WORKER_GATE_WORKER") or WORKER_DEFAULT


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def corpus():
    return json.load(io.open(FIXTURE, encoding="utf-8"))["rows"]


# ---------------------------------------------------------------------------
# Extracting the Worker's OWN gate, so there is exactly one implementation
# ---------------------------------------------------------------------------

#: Every constant the gate closes over, and every function it needs. Listed
#: explicitly so that a renamed or deleted symbol fails the test loudly instead
#: of silently producing an empty gate that "matches" everything.
GATE_CONSTS = ("EMERGENCY_RE", "BREAKING_IMPACT_RE", "BREAKING_IMPACT_WIDE_RE",
               "NOT_A_LIVE_EVENT_RE", "HARD_LIVE_EVIDENCE_RE",
               "HARD_LIVE_DISRUPTION_RE", "UNIVERSAL_IMPORTANCE_RE",
               "BARE_LABEL_SUFFIX_RE", "SPORT_GOSSIP_RE")
GATE_NUMS = ("UNIVERSAL_MIN_DEATH_COUNT", "MIN_INFO_WORDS",
             "SPORT_GOSSIP_SUPPRESS_HITS")
GATE_FUNCS = ("gateBlob", "sportGossipHits", "allHits", "framingVetoApplies",
              "isBareLabel", "universalHits", "byCodePoint", "importanceGate")

#: The pre-fix Worker gate, verbatim, so the "before" number in the failure
#: message is a MEASUREMENT and not a remembered figure.
OLD_NARROW_GATE = r"""
const OLD_NARROW_EMERGENCY_RE =
  /\b(war|invasion|nuclear (test|accident|plant)|earthquake|magnitude 6\.[5-9]|magnitude [7-9]|flood warning|landslide warning|national emergency|Araniko (highway )?(closed|blocked)|Example Bank (fraud|security|cyber)|major outage)\b/i;
function oldNarrowGate(blob, title) {
  if (!OLD_NARROW_EMERGENCY_RE.test(blob)) return { tripped: false, why: "" };
  if (sportGossipHits(blob) >= SPORT_GOSSIP_SUPPRESS_HITS) {
    return { tripped: false, why: "sport_gossip_dominating" };
  }
  return { tripped: true, why: "emergency" };
}
"""


def _const_initialiser(src, name):
    r"""Verbatim initialiser of `const <name> = ...;`.

    Scans to the first `;` at paren/brace depth 0. Character classes `[...]`
    are NOT brackets for this purpose (that bug terminates the scan early on
    `[5-9]`), and regex escapes are skipped so a `\;` cannot end the match.
    """
    m = re.search(r"const\s+%s\s*=" % re.escape(name), src)
    if not m:
        raise AssertionError("the Worker no longer declares `const %s`" % name)
    i, depth, in_class = m.end(), 0, False
    while i < len(src):
        ch = src[i]
        if in_class:
            if ch == "\\":
                i += 2
                continue
            if ch == "]":
                in_class = False
        elif ch == "\\":
            i += 2
            continue
        elif ch == "[":
            in_class = True
        elif ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return src[m.end():i].strip()
        i += 1
    raise AssertionError("could not find the end of `const %s`" % name)


def _function(src, name):
    marker = "function %s(" % name
    if marker not in src:
        raise AssertionError("the Worker no longer declares `function %s`"
                             % name)
    start = src.index(marker)
    # Include a preceding `async ` or the extracted function silently becomes a
    # syntax error ("await is only valid in async functions"), which reads as a
    # broken test rather than a broken extractor.
    prefix = src[:start].rstrip()
    if prefix.endswith("async"):
        start = src.rindex("async", 0, start)
    i = src.index("{", start)
    depth = 0
    for pos in range(i, len(src)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[start:pos + 1]
    raise AssertionError("could not brace-match `function %s`" % name)


def node_harness():
    """A self-contained JS program that evaluates the Worker's own gate."""
    src = _read(worker_path())
    parts = ["const %s = %s;" % (c, _const_initialiser(src, c))
             for c in GATE_CONSTS]
    parts += ["const %s = %s;" % (n, _const_initialiser(src, n))
              for n in GATE_NUMS]
    parts += [_function(src, f) for f in GATE_FUNCS]
    return "\n".join(parts) + "\n" + OLD_NARROW_GATE + "\n"


def run_gates_real(rows):
    """Run BOTH gates (the old narrow one and the current one) over the corpus.

    node is required and its absence FAILS rather than skips: a parity test
    that skips is a green run with no parity check, which is precisely the
    silence that shipped the divergence in the first place.
    """
    node = shutil.which("node")
    if not node:
        raise AssertionError(
            "node is required to execute the Worker's own gate, and it is not "
            "on PATH. This FAILS instead of skipping: a green run with no "
            "parity check is the exact silence this task exists to end.")
    js = node_harness()
    payload = [[r.get("title", ""), r.get("snippet", "")] for r in rows]
    js += (
        "\nvar CASES = %s;\n"
        "var OUT = CASES.map(function (c) {\n"
        "  var item = { title: c[0], snippet: c[1] };\n"
        "  var blob = gateBlob(item);\n"
        "  return { blob: blob,\n"
        "           old: oldNarrowGate(blob, item.title),\n"
        "           now: importanceGate(blob, item.title) };\n"
        "});\n"
        "process.stdout.write(JSON.stringify(OUT));\n" % json.dumps(payload)
    )
    fd, tmp = tempfile.mkstemp(suffix=".js", text=True)
    try:
        with io.open(fd, "w", encoding="utf-8") as fh:
            fh.write(js)
        proc = subprocess.run([node, tmp], capture_output=True, timeout=600)
    finally:
        os.unlink(tmp)
    if proc.returncode != 0:
        raise AssertionError("node failed (exit %s):\n%s"
                             % (proc.returncode,
                                proc.stderr.decode("utf-8", "replace")[:3000]))
    out = json.loads(proc.stdout.decode("utf-8"))
    return out


# ==========================================================================
# (A) GATE PARITY â€” the Worker's decision must equal Python's, row by row
# ==========================================================================

class WorkerGateMatchesPython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = corpus()
        cls.js = run_gates_real(cls.rows)
        cls.py = []
        for r in cls.rows:
            t, s = r.get("title", ""), r.get("snippet", "")
            blob = bm._gate_blob(t, s)
            tripped, why, _ = bm.importance_gate(blob, t)
            cls.py.append({"blob": blob, "tripped": tripped, "why": why})

    def test_node_is_available_to_execute_the_worker_gate(self):
        self.assertIsNotNone(shutil.which("node"),
                             "node is required; this fails rather than skips")

    def test_the_worker_builds_the_same_gate_blob_as_python(self):
        bad = [(i, j["blob"], p["blob"])
               for i, (j, p) in enumerate(zip(self.js, self.py))
               if j["blob"] != p["blob"]]
        self.assertEqual(
            [], bad,
            "gateBlob diverges on %d row(s); everything downstream is "
            "meaningless until the blob matches:\n%s"
            % (len(bad), "\n".join("  row %d js=%r py=%r" % b for b in bad[:5])))

    def test_worker_and_python_reach_the_same_verdict_on_every_row(self):
        """THE assertion. Not a sample: every row of the frozen corpus."""
        bad = [(i, j, p) for i, (j, p) in enumerate(zip(self.js, self.py))
               if j["now"]["tripped"] != p["tripped"]]
        self.assertEqual(
            [], bad,
            "THE WORKER GATE AND PYTHON'S importance_gate DISAGREE on %d of %d "
            "corpus rows.\n"
            "Measured on this corpus: old narrow Worker gate = %d tripped, "
            "current Worker gate = %d, Python = %d.\n"
            "The Worker runs FIRST, so any row Python keeps and the Worker drops "
            "never reaches the classifier and is invisible downstream. First %d "
            "diverging rows:\n%s"
            % (len(bad), len(self.rows),
               sum(1 for r in self.js if r["old"]["tripped"]),
               sum(1 for r in self.js if r["now"]["tripped"]),
               sum(1 for p in self.py if p["tripped"]),
               len(bad[:10]),
               "\n".join("  row %d worker=%s python=%s worker_why=%r "
                         "python_why=%r | %s"
                         % (i, j["now"]["tripped"], p["tripped"],
                            j["now"]["why"][:40], p["why"][:40],
                            self.rows[i].get("title", "")[:70])
                         for i, j, p in bad[:10])))

    def test_both_sides_report_the_same_audit_reason(self):
        """The `why` string is what an operator reads in the run log. Two
        implementations that decide alike but explain themselves differently
        are a debugging trap, and the difference is the thing that makes a
        future divergence visible instead of invisible."""
        bad = [(i, j["now"]["why"], p["why"])
               for i, (j, p) in enumerate(zip(self.js, self.py))
               if j["now"]["tripped"] and j["now"]["why"] != p["why"]]
        self.assertEqual(
            [], bad,
            "tripped rows disagree on WHY on %d row(s):\n%s"
            % (len(bad), "\n".join("  row %d worker=%r python=%r | %s"
                                   % (i, j, p, self.rows[i]["title"][:70])
                                   for i, j, p in bad[:10])))

    def test_the_worker_gate_is_not_the_narrow_13_phrase_gate(self):
        """Guards against a parity test that passes for the wrong reason.

        If both sides were reverted to something equally narrow, row-by-row
        parity would hold and this whole file would be satisfied while the
        chain was dead again. So the gate must ALSO be measurably wide.
        """
        old_hits = sum(1 for r in self.js if r["old"]["tripped"])
        new_hits = sum(1 for r in self.js if r["now"]["tripped"])
        self.assertNotEqual(
            old_hits, new_hits,
            "the Worker's gate trips exactly as many rows (%d) as the old "
            "13-phrase gate, so the widening is not present and a real "
            "headline would still be dropped before the classifier"
            % old_hits)
        self.assertGreater(
            new_hits, old_hits,
            "the Worker gate (%d) must trip MORE than the old 13-phrase gate "
            "(%d); a narrower gate reintroduces the original defect"
            % (new_hits, old_hits))

    def test_the_widening_rescues_genuinely_urgent_headlines(self):
        """Not 'wider is better' â€” the rescued rows must be REAL alerts."""
        rescued = [i for i, (j, _) in enumerate(zip(self.js, self.py))
                   if not j["old"]["tripped"] and j["now"]["tripped"]]
        urgent = [i for i in rescued if self.rows[i].get("urgent")]
        sys.stderr.write(
            "  [chain] Worker gate: old=%d new=%d python=%d over %d rows; "
            "widening rescued %d rows, %d of them corpus-labelled urgent\n"
            % (sum(1 for r in self.js if r["old"]["tripped"]),
               sum(1 for r in self.js if r["now"]["tripped"]),
               sum(1 for p in self.py if p["tripped"]), len(self.rows),
               len(rescued), len(urgent)))
        self.assertGreater(
            len(urgent), 0,
            "the widening rescued only non-urgent rows, so it is not buying "
            "any real alert and only costs classifier calls")

    def test_real_headlines_the_narrow_gate_dropped_now_trip(self):
        """Named regressions. Each of these was dropped in production."""
        must_trip = [
            "27 killed across Nepal due to rain-induced disasters",
            "Nepal suspends search after 16 swept away by avalanche",
            "Himlung Himal Avalanche in Nepal Buries 16 Climbers",
        ]
        titles = [r.get("title", "") for r in self.rows]
        for needle in must_trip:
            idx = [i for i, t in enumerate(titles) if needle.lower() in t.lower()]
            self.assertTrue(idx, "corpus no longer contains a headline "
                                 "matching %r, so this regression test is "
                                 "testing nothing" % needle)
            for i in idx:
                self.assertTrue(
                    self.js[i]["now"]["tripped"],
                    "regression: %r is dropped by the Worker gate "
                    "(why=%r). This is the exact class of real alert the "
                    "narrow gate lost."
                    % (self.rows[i]["title"][:90], self.js[i]["now"]["why"]))


# ==========================================================================
# (A2) CANDIDATE SUPPLY — the Worker's sweep must be able to SEE events
# ==========================================================================

#: Everything parseFeedItems closes over, extracted from the Worker by name so a
#: rename fails loudly instead of silently yielding an empty parse.
FEED_CONSTS = ("ITEM_RE", "TITLE_RE", "LINK_RE", "PUBDATE_RE", "DESCRIPTION_RE",
               "CONTENT_ENCODED_RE", "SNIPPET_MAX_CHARS", "WINDOW_MS",
               "HARD_LIVE_EVIDENCE_RE", "HARD_LIVE_DISRUPTION_RE")
FEED_FUNCS = ("stripHtml", "snippetOf", "unescapeXml", "tagText", "parseFeedItems")

_SENTINEL2 = "__FEED__"


def run_feed_parse_real(xml, now_ms=None):
    """Execute the Worker's OWN parseFeedItems against feed XML."""
    node = shutil.which("node")
    if not node:
        raise AssertionError("node is required; this fails rather than skips")
    src = _read(worker_path())
    parts = ["const %s = %s;" % (c, _const_initialiser(src, c))
             for c in FEED_CONSTS]
    parts += [_function(src, f) for f in FEED_FUNCS]
    js = "\n".join(parts) + "\n"
    js += ('var XML = %s;\n' % json.dumps(xml))
    if now_ms is not None:
        # Freezing the clock is what makes "fresh" and "stale" testable rather
        # than a property of the day the suite happens to run.
        js += "var REAL_NOW = Date.now; Date.now = function(){return %d};\n" % now_ms
    js += ('process.stdout.write("%s" + JSON.stringify(parseFeedItems(XML)));\n'
           % _SENTINEL2)
    fd, tmp = tempfile.mkstemp(suffix=".js", text=True)
    try:
        with io.open(fd, "w", encoding="utf-8") as fh:
            fh.write(js)
        proc = subprocess.run([node, tmp], capture_output=True, timeout=120)
    finally:
        os.unlink(tmp)
    if proc.returncode != 0:
        raise AssertionError("node failed:\n%s"
                             % proc.stderr.decode("utf-8", "replace")[:2000])
    out = proc.stdout.decode("utf-8", "replace")
    self_marker = out.index(_SENTINEL2)
    return json.loads(out[self_marker + len(_SENTINEL2):])


class WorkerCandidateSupplyIsReal(unittest.TestCase):
    """DEFECT 2: the tripwire was dead at candidate supply, not at dispatch.

    `dispatchAlert` was never entered because `recordDispatch` had never been
    reached either -- so this is "never attempted", not "attempted and failed".
    Three independent supply defects, all measured live on 2026-10-13:

      1. one OR-merged Google News query for every local term, which Google's
         100-item cap turned into a feed whose newest item was 113 minutes old;
      2. feeds with no <pubDate> silently discarded -- kathmandupost.com/rss
         carries 40 English headlines and no date element at all;
      3. an ASCII-only tokeniser that scored every Devanagari headline as a
         bare section label: 6 of 6 in-window, 55 of 55 over 72h, 12 of 12.
    """

    @staticmethod
    def _worker_np_queries():
        src = _read(worker_path())
        m = re.search(r"const\s+BREAKING_NP_QUERIES\s*=\s*\[(.*?)\]\s*;",
                      src, re.S)
        if not m:
            raise AssertionError("the Worker no longer declares "
                                 "`const BREAKING_NP_QUERIES = [...]`")
        return re.findall(r'"([^"]+)"', m.group(1))

    def test_the_np_sweep_is_one_feed_per_term_not_one_OR_merged_query(self):
        queries = self._worker_np_queries()
        self.assertGreaterEqual(
            len(queries), 5,
            "the Worker's NP sweep has %d term(s). One OR-merged query is what "
            "this defect was: Google's 100-item cap is spent on whichever "
            "clause matches loosest, and fresh local news falls off the end."
            % len(queries))
        for q in queries:
            self.assertNotIn(
                " OR ", q,
                "term %r is still an OR-merged clause, so it is still one "
                "budgeted item list rather than its own." % q)

    def test_the_worker_np_sweep_mirrors_the_python_arm(self):
        """Python already polled one feed per term (BREAKING_NP_QUERIES). If the
        two lists diverge the arms sweep different things, which is how a
        "the Worker never fires" question gets answered by blaming the wrong
        half."""
        self.assertEqual(
            list(bm.BREAKING_NP_QUERIES), self._worker_np_queries(),
            "deploy/cloudflare-worker.js:BREAKING_NP_QUERIES and "
            "scripts/breaking_monitor.py:BREAKING_NP_QUERIES have diverged, so "
            "the two arms are sweeping different local interests")

    def _rss(self, inner):
        return ('<?xml version="1.0"?><rss version="2.0"><channel>%s</channel>'
                '</rss>' % inner)

    def _item(self, title, pubdate=None):
        return ("<item><title>%s</title><link>https://example.invalid/x</link>"
                "%s<description>desc</description></item>"
                % (title, ("<pubDate>%s</pubDate>" % pubdate) if pubdate else ""))

    def test_a_dateless_item_is_KEPT_and_flagged_not_silently_discarded(self):
        """kathmandupost.com/rss serves 40 valid headlines and NO <pubDate>.
        The old guard dropped all 40 on every sweep while the feed list read as
        though they were being ingested."""
        now = 1760000000000
        xml = self._rss(self._item(
            "Army helicopter rescues three trapped by landslide in Gorkha"))
        items = run_feed_parse_real(xml, now_ms=now)
        self.assertEqual(
            1, len(items),
            "a dateless headline carrying HARD live evidence was DISCARDED. That "
            "is the exact silent loss this fixes: 40 kathmandupost headlines a "
            "day, with the code reading as if the feed were being ingested.")
        self.assertIs(items[0]["undated"], True,
                      "the item must be FLAGGED undated, so the caveat travels "
                      "with it instead of living only in a comment")

    def test_a_dateless_item_without_live_evidence_is_dropped(self):
        """The freshness caveat has to be a bar, not a shrug.

        Measured live on 2026-10-13: admitting every dateless item took the
        sweep from 0 tripped to 14, and most of those 14 were not breaking news
        ("Dismantle party-affiliated student unions", "Trump announces Ukraine
        energy ceasefire"). Without a date there is no way to tell a
        two-minute-old flood from a week-old retrospective, and the bare word
        "flood" is not evidence. HARD live evidence — a casualty, or something
        blocked/closed/evacuated right now — is.
        """
        now = 1760000000000
        noise = self._rss(self._item(
            "Flood relief funds allocated for the district, explainer"))
        self.assertEqual(
            [], run_feed_parse_real(noise, now_ms=now),
            "a dateless headline with no HARD live evidence was admitted. "
            "Without a date its age cannot be checked, so it must clear the "
            "stronger bar or the dateless feed becomes alert fatigue.")

    def test_a_present_but_corrupt_date_is_still_discarded(self):
        """Deliberately asymmetric: absent means unknown age and is kept; a
        present-but-garbage date is a data error, and guessing from it would be
        worse than dropping it."""
        xml = self._rss(self._item("Landslide blocks road", "not-a-date"))
        self.assertEqual([], run_feed_parse_real(xml))

    def test_fresh_dated_items_are_kept_and_stale_ones_dropped(self):
        """The 30-minute window itself must be unchanged -- widening what is
        INGESTED is not the same as widening the window."""
        import email.utils
        now = 1760000000000
        now_s = now // 1000
        fresh_dt = email.utils.formatdate(now_s - 600, usegmt=True)
        stale_dt = email.utils.formatdate(now_s - 3600, usegmt=True)
        fresh = self._rss(self._item("Fresh landslide", fresh_dt))
        stale = self._rss(self._item("Old landslide", stale_dt))
        kept = run_feed_parse_real(fresh, now_ms=now)
        self.assertEqual(1, len(kept),
                         "a 10-minute-old headline was dropped, so the 30-min "
                         "window no longer admits fresh items")
        self.assertIs(kept[0]["undated"], False)
        self.assertEqual([], run_feed_parse_real(stale, now_ms=now),
                         "a 60-minute-old headline was KEPT, so the 30-minute "
                         "window has been widened. That is a different defect "
                         "and this assertion is what stops it happening here.")

    def test_a_devanagari_headline_is_no_longer_a_bare_section_label(self):
        """The tokeniser was `[A-Za-z0-9']+`, ASCII-only, so a Devanagari
        headline scored 0 informative words and was thrown away before the
        emergency vocabulary was ever consulted."""
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required; this fails, not skips")
        src = _read(worker_path())
        parts = ["const %s = %s;" % (c, _const_initialiser(src, c))
                 for c in GATE_CONSTS]
        parts += ["const %s = %s;" % (n, _const_initialiser(src, n))
                  for n in GATE_NUMS]
        parts += [_function(src, f) for f in ("isBareLabel",)]
        js = "\n".join(parts) + "\n"
        cases = [
            ("Nepal Landslide", True),          # English label: still a label
            ("Nepal Landslide - Dayton Daily News", True),
            ("27 killed across Nepal due to rain", False),
            (u"\u0917\u094b\u0930\u0916\u093e \u092c\u093e\u092a\u094d\u0937\u093e "
             u"\u092a\u0939\u093f\u091b\u093f \u0917\u093e\u092f\u093e, "
             u"\u0917\u0943\u0939 \u0905\u0947\u091b\u0924\u093e\u0930 "
             u"\u092a\u0928\u093f", False),     # 8 Devanagari tokens: an event
            (u"\u0928\u0947\u092a\u093e\u0932", True),  # one word: a label
        ]
        js += "var CASES = %s;\n" % json.dumps([[t, e] for t, e in cases])
        js += ("process.stdout.write(JSON.stringify(CASES.map(function(c){"
               "return isBareLabel(c[0]);})));\n")
        fd, tmp = tempfile.mkstemp(suffix=".js", text=True)
        try:
            with io.open(fd, "w", encoding="utf-8") as fh:
                fh.write(js)
            proc = subprocess.run([node, tmp], capture_output=True, timeout=120)
        finally:
            os.unlink(tmp)
        self.assertEqual(0, proc.returncode,
                         "node failed:\n%s"
                         % proc.stderr.decode("utf-8", "replace")[:2000])
        got = json.loads(proc.stdout.decode("utf-8"))
        for (title, want), actual in zip(cases, got):
            self.assertIs(
                actual, want,
                "isBareLabel(%r) = %r, want %r" % (title[:40], actual, want))

    def test_python_and_the_worker_agree_on_devanagari_titles(self):
        """Lockstep, asserted directly. The 616-row corpus is English apart from
        ONE row, so row-by-row parity alone would not have caught a divergence
        here -- these are the rows that decide it."""
        titles = [
            u"\u0917\u094b\u0930\u0916\u093e \u092c\u093e\u092a\u094d\u0937\u093e "
            u"\u092a\u0939\u093f\u091b\u093f \u0917\u093e\u092f\u093e, "
            u"\u0917\u0943\u0939 \u0905\u0947\u091b\u0924\u093e\u0930 "
            u"\u092a\u0928\u093f",
            u"\u0928\u0947\u092a\u093e\u0932 \u091c\u0932 \u0935\u093f\u0915\u093e\u0938 "
            u"\u091c\u093e\u0930\u094d\u092f \u091b\u0948",
        ]
        for t in titles:
            self.assertIs(
                bm._is_bare_label(t), False,
                "python's _is_bare_label still scores %r as a bare label" % t[:30])
        # A ONE-WORD Devanagari title is still a label. The fix exempts titles
        # by TOKEN COUNT, not by script: if it had exempted every non-ASCII
        # title outright, a one-word section header would start tripping.
        self.assertIs(bm._is_bare_label(u"\u0928\u0947\u092a\u093e\u0932"), True)
        # And the English side must be UNCHANGED, which is what makes this safe:
        self.assertIs(bm._is_bare_label("Nepal Landslide - Dayton Daily News"),
                      True)
        self.assertIs(bm._is_bare_label("27 killed across Nepal due to rain"),
                      False)


# ==========================================================================
# (B) REACHABILITY — a trigger exists, and cannot double-alert
# ==========================================================================

class BreakingMonitorIsReachable(unittest.TestCase):
    def setUp(self):
        self.src = _read(BREAKING_WF)

    def test_the_workflow_has_a_schedule_trigger(self):
        """For six weeks the only triggers were repository_dispatch (which had
        never fired) and workflow_dispatch (manual). A gate nobody can reach is
        a gate that does not exist."""
        on_block = self._on_block()
        self.assertRegex(
            on_block, r"(?m)^\s{2}schedule:\s*$",
            "breaking-monitor.yml has NO `schedule:` under `on:`, so the "
            "workflow has no reachable trigger unless the Worker dispatches â€” "
            "and repository_dispatch has never fired in this project's history")

    def _fires_per_day(self, cron):
        # Just enough cron to answer the one question here: runs per day.
        fields = cron.split()
        self.assertEqual(5, len(fields), "not a 5-field cron: %r" % cron)
        minute, hour, day, month, weekday = fields
        if (day, month, weekday) != ("*", "*", "*"):
            return 1   # a date-restricted schedule cannot fire more than daily
        hours = 24 if hour == "*" else sum(
            24 // int(part[2:]) if part.startswith("*/") else 1
            for part in hour.split(","))
        return hours * (len(minute.split(",")) if minute != "*" else 1)

    def test_the_schedule_is_low_frequency_and_not_the_retired_hourly_tick(self):
        on_block = self._on_block()
        crons = re.findall(r"- cron:\s*'([^']+)'", on_block)
        self.assertTrue(crons, "no cron expression found under `schedule:`")
        for cron in crons:
            self.assertNotEqual(
                cron, "20 * * * *",
                "the hourly tick retired in 2bd8eda is back; it spends Actions "
                "minutes on runs that re-detect inside the 6h dedup window")
        for cron in crons:
            # The property is FREQUENCY, not the `*/N` spelling: `20 */6 * * *`
            # (4/day) passed this test while `20 1 * * *` (1/day) failed it.
            self.assertLessEqual(
                self._fires_per_day(cron), 1,
                "schedule %r fires more than once a day. The Worker is the "
                "sensor; this is a net, and the cut to daily was deliberate -- "
                "a change here must also update tests/workflows/"
                "test_workflow_lint.py CRON_NPT_TABLE and "
                "config/actions-budget.json" % cron)

    def test_the_schedule_documents_why_it_was_restored(self):
        """A schedule with no stated reason gets deleted again within a
        month. That is exactly what happened the first time."""
        on_block = self._on_block().lower()
        self.assertIn(
            "primary", on_block,
            "the `on:` block must state that the Worker dispatch is the PRIMARY "
            "trigger, or the next reader will delete this schedule as "
            "redundant â€” which is how it was lost the first time")
        self.assertRegex(
            on_block, r"(second|second path|second path)",
            "the `on:` block must state what this trigger defends against")

    def test_double_alerting_is_bounded_by_the_kv_dedup_and_the_story_state(self):
        """Two paths now reach the same pipeline. Both must be bounded."""
        self.assertIn(
            "21600", _read(worker_path()),
            "the Worker's KV dedup TTL (21600s) must be preserved: two paths "
            "reaching one pipeline is only safe while a 6h dedup window "
            "bounds it")
        src = _read(os.path.join(SCRIPTS, "breaking_monitor.py"))
        self.assertIn(
            "duplicate_no_escalation", src,
            "the scheduled run must run the same active_stories dedup as a "
            "dispatched run, or the two triggers can double-send one story")

    def test_the_worker_dispatches_at_most_once_per_sweep(self):
        """This is what makes the widening cost-bounded. At most ONE dispatch
        per sweep means a wider gate cannot multiply workflow runs."""
        body = _function(_read(worker_path()), "handle")
        self.assertIn(
            "break; // single dispatch per run", body,
            "the single-dispatch guard is gone from the sweep, so N tripped "
            "candidates would now mean N workflow runs per sweep")
        self.assertEqual(
            1, body.count("dispatchAlert(env, item"),
            "the sweep calls dispatchAlert more than once per item, which "
            "breaks the one-dispatch-per-sweep cost bound")

    def _on_block(self):
        """The `on:` mapping text, from `on:` to the first job-level key."""
        m = re.search(r"(?m)^on:\s*$", self.src)
        self.assertIsNotNone(m, "no top-level `on:` key in breaking-monitor.yml")
        rest = self.src[m.end():]
        stop = re.search(r"(?m)^(jobs|permissions|env|concurrency|defaults):\s*$",
                         rest)
        return rest[:stop.start()] if stop else rest


# ==========================================================================
# (C) THE WATCHDOG MUST BE ABLE TO SEE A DEAD CHAIN
# ==========================================================================

#: A token that exists ONLY inside the chain arm's python body. Deliberately
#: NOT `CHAIN_URL=`, which sits on the shell line *above* the heredoc and so
#: would match nothing in the extracted block.
WATCHDOG_PY_MARKER = "breaking_dispatch_ever"


def yaml_heredoc(path, must_contain):
    """The `python3 - <<'PY' ... PY` block that CONTAINS `must_contain`.

    Selecting by CONTENT rather than by first match matters: both workflows
    contain several heredocs, and a positional grab silently executes an
    unrelated step and reports it green.
    """
    src = _read(path)
    for m in re.finditer(r"<<\s*'PY'\s*\n", src):
        body = src[m.end():]
        end = re.search(r"(?m)^[ \t]*PY\s*$", body)
        if not end:
            raise AssertionError("a heredoc in %s is unterminated" % path)
        code = textwrap.dedent(body[:end.start()])
        if must_contain in code:
            return code
    raise AssertionError(
        "no python heredoc containing %r was found in %s" % (must_contain, path))


def watchdog_chain_python():
    """The watchdog's chain-arm Python, extracted verbatim from the YAML."""
    return yaml_heredoc(WATCHDOG, WATCHDOG_PY_MARKER)


#: A delivery receipt as the SENDER writes it: `last_delivery` present, set only
#: by a confirmed 2xx. This is the POSITIVE record the whole breaking arm now
#: turns on, and it is what the four verified 2026-10-11/12 deliveries would
#: have produced. It is the DEFAULT for every case below that does not override
#: it, so a test about the Worker's trigger leg cannot accidentally depend on the
#: repository's own `state/breaking_delivery.json`.
DELIVERED_RECEIPT = {
    "schema_version": 1,
    "last_delivery": {
        "at": "2026-10-12T06:12:02Z", "ok": True, "channel": "ntfy",
        "note": "ntfy:delivered",
        "title": "Major flash flood in Nepal; 150+ tourists stranded",
    },
    "last_attempt": {
        "at": "2026-10-12T06:12:02Z", "ok": True, "channel": "ntfy",
        "note": "ntfy:delivered",
        "title": "Major flash flood in Nepal; 150+ tourists stranded",
    },
    "history": [],
}


def run_watchdog_chain(health_payload, curl_rc=0, http="200",
                       url="https://x/h", delivery=DELIVERED_RECEIPT,
                       delivery_raw=None):
    """Execute the watchdog's own chain-arm python against a payload.

    Real execution of the committed code, not a paraphrase of it. Returns
    (exit_code, problems, notes, combined_output).

    `delivery` / `delivery_raw` drive the DELIVERY RECEIPT the arm now reads.
    `delivery_raw` writes bytes verbatim, so the "file absent" and "file
    unparsable" cases are real reads of a real (bad) file rather than simulated
    ones. Both default to a healthy delivered receipt, because that is the
    state production is actually in.
    """
    code = watchdog_chain_python()
    tmp = tempfile.mkdtemp(prefix="chain-arm-")
    try:
        health_path = os.path.join(tmp, "health.json")
        with io.open(health_path, "w", encoding="utf-8") as fh:
            fh.write(health_payload)
        if delivery_raw is not None:
            receipt_text = delivery_raw
        elif delivery is None:
            receipt_text = None
        else:
            receipt_text = json.dumps(delivery)
        receipt_path = os.path.join(tmp, "breaking_delivery.json")
        if receipt_text is not None:
            with io.open(receipt_path, "w", encoding="utf-8") as fh:
                fh.write(receipt_text)
        out_path = os.path.join(tmp, "gh_output.txt")
        io.open(out_path, "w", encoding="utf-8").close()
        summary_path = os.path.join(tmp, "summary.md")
        env = dict(os.environ)
        env.update({"CHAIN_URL": url, "CHAIN_CODE": http,
                    "CHAIN_CURL_RC": str(curl_rc),
                    "CHAIN_HEALTH_FILE": health_path,
                    "CHAIN_DELIVERY_FILE": receipt_path,
                    "GITHUB_OUTPUT": out_path,
                    "GITHUB_STEP_SUMMARY": summary_path})
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, timeout=120)
        raw = io.open(out_path, encoding="utf-8").read()
        problems = [l[len("problem="):]
                    for l in raw.splitlines() if l.startswith("problem=")]
        notes = [l for l in raw.splitlines()
                 if l.startswith("note=") or l.startswith("unknown=")]
        return proc.returncode, problems, notes, (proc.stdout or "") + \
            (proc.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class WatchdogDetectsADeadChain(unittest.TestCase):
    HEALTHY = json.dumps({
        "breaking_dispatch_ever": True, "breaking_dispatch_age_s": 7200,
        "breaking_dispatch_unreadable": False, "breaking_last_dispatch_ok": True,
    })

    # ------------------------------------------------------------------
    # THE FALSE PAGE OF 2026-10-13. This case replaced
    # test_a_chain_that_has_never_dispatched_is_a_fault, which asserted the
    # EXACT sentence that was factually false. It is not deleted and not
    # weakened -- it is inverted, because what it used to demand is the defect.
    # ------------------------------------------------------------------
    def test_the_worker_trigger_leg_never_firing_is_NOT_a_fault(self):
        """THE REGRESSION THIS WHOLE TASK EXISTS TO CLOSE.

        `breaking_dispatch_ever: false` measures the Cloudflare Worker's TRIGGER
        leg. On 2026-10-13 this arm escalated it as "THE BREAKING CHAIN HAS
        NEVER RUN ... Every breaking alert is being missed silently." Four
        level-3 alerts were delivered to ntfy in the previous ~50 hours, each
        logging `breaking ntfy delivered (HTTP 2xx)`. A trigger leg is not a
        delivery record.
        """
        rc, problems, notes, out = run_watchdog_chain(
            json.dumps({"breaking_dispatch_ever": False,
                        "breaking_dispatch_unreadable": False,
                        "breaking_last_dispatch_ok": None}))
        self.assertEqual(
            0, rc,
            "the watchdog paged AGAIN with breaking_dispatch_ever=false while a "
            "delivery receipt proves alerts are arriving. That is the false "
            "page of 2026-10-13, verbatim.\nproblems=%r\n%s" % (problems, out))
        self.assertEqual([], problems,
                         "the trigger leg alone produced a fault: %r" % problems)
        # The notes are printed to stdout, not written as `note=` outputs, so the
        # assertion reads the executed arm's real output.
        self.assertIn(
            "breaking_dispatch_ever", out,
            "the trigger-leg fact must still be REPORTED as a note -- demoting "
            "it to a non-fault must not delete the observation")

    def test_the_exact_production_false_page_is_green(self):
        """The real 2026-10-13 payload: the Worker's own trigger telemetry says
        it has never dispatched, the health endpoint is reachable and fresh, and
        four deliveries stand in the run log. Every element of the false page,
        executed, must be GREEN."""
        rc, problems, notes, out = run_watchdog_chain(
            json.dumps({
                "breaking_dispatch_ever": False,
                "breaking_dispatch_age_s": None,
                "breaking_dispatch_unreadable": False,
                "breaking_last_dispatch_ok": None,
                "breaking_dispatch_http_status": None,
                "breaking_heartbeat_age_s": 120,
                "breaking_heartbeat_unreadable": False,
            }),
            delivery={
                "schema_version": 1,
                "last_delivery": {
                    "at": "2026-10-12T06:12:02Z", "ok": True, "channel": "ntfy",
                    "note": "ntfy:delivered",
                    "title": "Major flash flood in Nepal; 150+ tourists stranded",
                },
                "last_attempt": {
                    "at": "2026-10-12T06:12:02Z", "ok": True, "channel": "ntfy",
                    "note": "ntfy:delivered",
                },
                "history": [],
            })
        self.assertEqual(
            0, rc,
            "THE FALSE PAGE REPRODUCED. Fresh heartbeat, reachable endpoint, "
            "four delivered alerts in the log, and the watchdog says every "
            "breaking alert is being missed silently.\n%s" % out)
        self.assertIn("DELIVERY CONFIRMED", out,
                      "the healthy arm must state that delivery is CONFIRMED, "
                      "not merely stay quiet: %r" % problems)

    # ------------------------------------------------------------------
    # WHAT A GENUINE FAULT LOOKS LIKE AFTER THE CHANGE. These are the cases
    # that MUST stay loud. Each is a recorded fact, never an inference from a
    # missing heartbeat.
    # ------------------------------------------------------------------
    def test_no_delivery_evidence_at_all_is_a_fault(self):
        """'We have no evidence any alert was ever sent' IS a legitimate
        alarming state. The receipt exists and carries no last_delivery."""
        rc, problems, _notes, out = run_watchdog_chain(
            self.HEALTHY,
            delivery={"schema_version": 1, "last_delivery": None,
                      "last_attempt": None, "history": []})
        self.assertNotEqual(
            0, rc,
            "a receipt with no last_delivery passed. That means no alert has "
            "ever been confirmed delivered and the arm stayed quiet.\n%s" % out)
        self.assertTrue(
            any("never delivered" in p.lower() or "no evidence" in p.lower()
                for p in problems),
            "the fault must SAY that no alert was ever delivered, not merely "
            "fail: %r" % problems)

    def test_a_missing_delivery_receipt_is_a_fault(self):
        rc, problems, _notes, out = run_watchdog_chain(
            self.HEALTHY, delivery=None)
        self.assertNotEqual(
            0, rc,
            "the arm passed with NO delivery receipt on disk. An absent file is "
            "unreadable, and unreadable must never print as healthy.\n%s" % out)
        self.assertTrue(problems, "no stated problem for a missing receipt")

    def test_an_unreadable_delivery_receipt_is_a_fault(self):
        rc, problems, _notes, out = run_watchdog_chain(
            self.HEALTHY, delivery_raw="{not json at all")
        self.assertNotEqual(
            0, rc,
            "the arm passed with an UNPARSABLE delivery receipt. 'I cannot "
            "tell' and 'it is fine' must never print the same thing.\n%s" % out)
        self.assertTrue(problems, "no stated problem for a corrupt receipt")

    def test_a_recorded_failed_send_is_a_fault(self):
        """A chain that runs and fails never alerts. It is loud because the
        FAILURE WAS RECORDED, not because something was missing."""
        rc, problems, _notes, out = run_watchdog_chain(
            self.HEALTHY,
            delivery={
                "schema_version": 1,
                "last_delivery": dict(DELIVERED_RECEIPT["last_delivery"]),
                "last_attempt": {
                    "at": "2026-10-12T20:00:00Z", "ok": False, "channel": "ntfy",
                    "note": "ntfy_failed:http_404", "title": "T",
                },
                "history": [],
            })
        self.assertNotEqual(
            0, rc,
            "a RECORDED failed delivery attempt passed. Detection running is "
            "not delivery working.\n%s" % out)
        self.assertTrue(
            any("attempt failed" in p.lower() for p in problems),
            "the fault must name the failed attempt: %r" % problems)

    def test_a_chain_that_ran_and_failed_is_a_fault(self):
        """The Worker tripwire's own hand-off failing stays loud, because it is
        a real degradation -- but the wording must no longer claim that no alert
        can reach the owner, because the schedule net still delivers."""
        rc, problems, _notes, out = run_watchdog_chain(
            json.dumps({"breaking_dispatch_ever": True,
                        "breaking_dispatch_unreadable": False,
                        "breaking_last_dispatch_ok": False,
                        "breaking_dispatch_age_s": 600}))
        self.assertNotEqual(
            0, rc,
            "the watchdog passed while the Worker's hand-off to the workflow is "
            "failing — that is a real degradation of the high-frequency "
            "detector.\n%s" % out)
        self.assertFalse(
            any("no breaking alert can reach you" in p for p in problems),
            "the wording still claims no alert can be delivered, which is "
            "false while the receipt above proves delivery: %r" % problems)

    def test_an_unreadable_endpoint_is_unknown_not_healthy(self):
        for label, payload in (
                ("non-200", self.HEALTHY),
                ("curl failure", self.HEALTHY),
                ("unparsable body", "{not json"),
                ("missing field", json.dumps({"build": "abc"}))):
            code = "000" if label == "non-200" else "200"
            rc_, problems, _n, out = run_watchdog_chain(
                payload, curl_rc=1 if label == "curl failure" else 0,
                http=code)
            self.assertNotEqual(
                0, rc_,
                "the watchdog PASSED when the health endpoint was %s. 'I "
                "cannot tell' and 'it is fine' must never print the same "
                "thing.\n%s" % (label, out))
            self.assertTrue(problems, "%s produced no stated problem" % label)

    def test_a_healthy_chain_passes(self):
        """The arm must not be an alarm that always fires. An alarm nobody
        reads is not a control."""
        rc, problems, notes, out = run_watchdog_chain(self.HEALTHY)
        self.assertEqual(0, rc, "a healthy chain was reported as a problem: "
                                "%r\n%s" % (problems, out))
        self.assertEqual([], problems)

    def test_the_arm_does_not_alarm_on_a_quiet_news_week(self):
        """A delivery 30 days ago is NOT a fault. If it were, this would page
        the owner every day forever."""
        rc, problems, _notes, out = run_watchdog_chain(
            self.HEALTHY,
            delivery={
                "schema_version": 1,
                "last_delivery": {
                    "at": "2026-09-13T00:00:00Z", "ok": True, "channel": "ntfy",
                    "note": "ntfy:delivered", "title": "Old but real",
                },
                "last_attempt": {
                    "at": "2026-09-13T00:00:00Z", "ok": True, "channel": "ntfy",
                    "note": "ntfy:delivered",
                },
                "history": [],
            })
        self.assertEqual(0, rc,
                         "the watchdog alarmed purely on the AGE of the last "
                         "DELIVERY. A quiet news week is the normal state and "
                         "alarming on it would make this arm noise.\n%s" % out)

    def test_an_unreadable_kv_read_is_a_note_not_a_storm(self):
        """Deliberate asymmetry, asserted so it stays deliberate: a KV blip
        inside the Worker must not storm the owner, but the cron heartbeat
        arm already covers whether the Worker is alive. NOTE this still needs a
        healthy receipt -- the arm no longer reaches that verdict from the
        Worker's KV state alone."""
        rc, problems, _notes, out = run_watchdog_chain(
            json.dumps({"breaking_dispatch_ever": False,
                        "breaking_dispatch_unreadable": True}))
        self.assertEqual(0, rc,
                         "an UNREADABLE KV read alarmed. That is stated as "
                         "intentional UNKNOWN, not a fault, so a KV blip "
                         "cannot storm the owner.\n%s" % out)


class DeliveryReceiptIsWrittenOnlyByASend(unittest.TestCase):
    """The PRODUCING side of the arm, executed.

    The watchdog arm is only as honest as the record it reads. A receipt written
    by inference, or written by a dry run, or written when ntfy was never
    attempted, would restore the exact defect in a new place -- so the writer
    itself is driven here rather than read.
    """

    def _receipt(self, rows):
        tmp = tempfile.mkdtemp(prefix="receipt-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "breaking_delivery.json")
        for ok, note in rows:
            bm.record_breaking_attempt(path, ok, note, "Headline %s" % note)
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_confirmed_send_writes_the_positive_delivery_record(self):
        doc = self._receipt([(True, "ntfy:delivered")])
        self.assertTrue(doc["last_delivery"],
                        "a 2xx did not write last_delivery, so the watchdog has "
                        "no positive record to read")
        self.assertIs(doc["last_delivery"]["ok"], True)
        self.assertIs(doc["last_attempt"]["ok"], True)

    def test_a_failed_send_writes_an_attempt_and_never_a_delivery(self):
        doc = self._receipt([(True, "ntfy:delivered"), (False, "ntfy_failed:404")])
        self.assertIs(doc["last_attempt"]["ok"], False,
                      "a failed send must be recorded as a failed attempt")
        self.assertEqual("ntfy:delivered", doc["last_delivery"]["note"],
                         "a failed send must NOT overwrite the proof that an "
                         "earlier send really delivered")

    def test_the_receipt_records_only_attempts_nothing_else(self):
        """`not_configured` and `--dry-run` are not sends.

        Driven through notify_ntfy itself rather than through the writer, because
        the real risk is at the CALL SITE: a --dry-run (which runs locally, all
        the time, including in CI) manufacturing a delivery record would restore
        the original defect in a new place.
        """
        tmp = tempfile.mkdtemp(prefix="receipt-nosend-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        paths = {"ntfy_preview": os.path.join(tmp, "ntfy-preview.txt"),
                 "breaking_delivery": os.path.join(tmp, "breaking_delivery.json")}
        verdict = {"title": "T", "summary": "S", "category_suggestion": "x"}

        for label, mode in (("dry_run", "dry_run"), ("not_configured", "off")):
            if mode == "dry_run":
                args = _arg_namespace(dry_run=True)
            else:
                args = _arg_namespace(dry_run=False)
                saved = {k: os.environ.get(k) for k in
                         ("NTFY_TOPIC", "NTFY_SERVER")}
                os.environ.pop("NTFY_TOPIC", None)
                os.environ.pop("NTFY_SERVER", None)
                try:
                    state, _note = bm.notify_ntfy(
                        verdict, {}, "HIGH", "CRITICAL", paths, args,
                        "https://example.invalid/a", "rss", "cutoff NPT")
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                self.assertEqual("not_configured", state)
            if mode == "dry_run":
                state, _note = bm.notify_ntfy(
                    verdict, {}, "HIGH", "CRITICAL", paths, args,
                    "https://example.invalid/a", "rss", "cutoff NPT")
                self.assertEqual("dry_run", state)
            self.assertFalse(
                os.path.exists(paths["breaking_delivery"]),
                "a %s run wrote a delivery receipt. A send that was never "
                "attempted cannot be a delivery, and a receipt that claims "
                "otherwise would make the watchdog report delivery for a dry "
                "run." % label)

    def test_a_corrupt_existing_receipt_is_replaced_not_fatal(self):
        tmp = tempfile.mkdtemp(prefix="receipt-corrupt-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "breaking_delivery.json")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write("{ truncated")
        bm.record_breaking_attempt(path, True, "ntfy:delivered", "T")
        with io.open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertTrue(doc["last_delivery"],
                        "a corrupt receipt left the writer unable to record "
                        "anything, so one bad write would freeze the signal "
                        "forever instead of self-healing on the next send")


#: Marks the result line; see _run for why stdout cannot be parsed directly.
_SENTINEL = "__JSON__"

class WorkerEmitsTheFieldsTheWatchdogReads(unittest.TestCase):
    """Closes the loop on the watchdog arm.

    The watchdog's chain arm reads four named fields off `?health=1`. If the
    Worker stopped emitting them the arm would fail CLOSED — which is safe, but
    would mean the watchdog pages the owner forever about a field that never
    exists. So the producing side is executed here, not assumed: the Worker's
    own `dispatchTelemetry` and `recordDispatch`, against a fake KV.
    """

    def _run(self, body):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required; this fails, not skips")
        src = _read(worker_path())
        parts = []
        for name in ("BREAKING_LAST_DISPATCH_KEY", "BREAKING_LAST_SUCCESS_KEY"):
            parts.append("const %s = %s;" % (name, _const_initialiser(src, name)))
        parts.append(_function(src, "parseHeartbeat"))
        parts.append(_function(src, "recordDispatch"))
        parts.append(_function(src, "dispatchTelemetry"))
        js = "\n".join(parts) + "\n" + body
        fd, tmp = tempfile.mkstemp(suffix=".js", text=True)
        try:
            with io.open(fd, "w", encoding="utf-8") as fh:
                fh.write(js)
            proc = subprocess.run([node, tmp], capture_output=True,
                                  timeout=300)
        finally:
            os.unlink(tmp)
        self.assertEqual(0, proc.returncode,
                         "node failed:\n%s"
                         % proc.stderr.decode("utf-8", "replace")[:2000])
        # `dispatchTelemetry` legitimately console.logs on the KV-error path,
        # and console.log goes to STDOUT -- the same stream as the result. So
        # the payload is read from behind an explicit sentinel rather than by
        # parsing stdout, which would silently choke on the log line and
        # report a JSON error instead of the real behaviour under test.
        out = proc.stdout.decode("utf-8", "replace")
        self.assertIn(
            _SENTINEL, out,
            "the node harness produced no %s payload. stdout was:\n%s"
            % (_SENTINEL, out))
        return json.loads(out.split(_SENTINEL, 1)[1].strip())

    _PRELUDE = """
var SENT = "__JSON__";
function fakeKV(store, failOn) {
  return {
    store: store || {},
    async get(k) {
      if (k === failOn) throw new Error("injected KV read failure");
      return Object.prototype.hasOwnProperty.call(this.store, k)
        ? this.store[k] : null;
    },
    async put(k, v) { this.store[k] = v; },
  };
}
"""

    def test_a_never_dispatched_chain_reports_ever_false(self):
        out = self._run(self._PRELUDE + """
var env = { KV: fakeKV({}) };
(async function () {
  process.stdout.write(SENT + JSON.stringify(await dispatchTelemetry(env,
    BREAKING_LAST_DISPATCH_KEY)));
})();
""")
        self.assertIs(out["ever"], False,
                      "a chain that has never dispatched must report "
                      "ever=false, not a fabricated number")
        self.assertIsNone(out["age_s"])
        self.assertIs(out["unreadable"], False)

    def test_a_successful_dispatch_reports_ever_true_with_an_age(self):
        out = self._run(self._PRELUDE + """
var env = { KV: fakeKV({}) };
(async function () {
  await recordDispatch(env, { at: new Date(Date.now() - 60000).toISOString(),
                              title: "T", ok: true });
  process.stdout.write(SENT + JSON.stringify(
    await dispatchTelemetry(env, BREAKING_LAST_DISPATCH_KEY)));
})();
""")
        self.assertIs(out["ever"], True)
        self.assertIs(out["ok"], True,
                      "a successful dispatch must report ok=true so the "
                      "watchdog can tell 'ran and worked' from 'ran and "
                      "failed'")
        self.assertGreater(out["age_s"], 0)
        self.assertLess(out["age_s"], 300)

    def test_a_failed_dispatch_is_reported_as_ok_false(self):
        """The watchdog alarms on this. If it read true here, a chain that runs
        and fails would be reported as the one thing that is working."""
        out = self._run(self._PRELUDE + """
var env = { KV: fakeKV({}) };
(async function () {
  await recordDispatch(env, { at: new Date().toISOString(),
                              title: "T", ok: false });
  process.stdout.write(SENT + JSON.stringify(
    await dispatchTelemetry(env, BREAKING_LAST_DISPATCH_KEY)));
})();
""")
        self.assertIs(out["ever"], True)
        self.assertIs(out["ok"], False)

    def test_a_failed_dispatch_surfaces_its_http_status(self):
        """The watchdog's message names the status code, which is what
        separates a bad token from a missing scope from GitHub being down.
        Surfacing the boolean alone would leave that message printing None."""
        out = self._run(self._PRELUDE + """
var env = { KV: fakeKV({}) };
(async function () {
  await recordDispatch(env, { at: new Date().toISOString(),
                              title: "T", ok: false, http_status: 401 });
  process.stdout.write(SENT + JSON.stringify(
    await dispatchTelemetry(env, BREAKING_LAST_DISPATCH_KEY)));
})();
""")
        self.assertEqual(out["http_status"], 401,
                         "the recorded HTTP status must reach the health "
                         "payload, or the watchdog reports a failed dispatch "
                         "without the one number that explains it")

    def test_a_kv_blip_is_reported_unreadable_not_as_never(self):
        """The distinction the watchdog's asymmetry rests on: unreadable must
        not collapse into `ever:false`, or a KV blip would page the owner as a
        dead chain."""
        out = self._run(self._PRELUDE + """
var env = { KV: fakeKV({}, BREAKING_LAST_DISPATCH_KEY) };
(async function () {
  process.stdout.write(SENT + JSON.stringify(await dispatchTelemetry(env,
    BREAKING_LAST_DISPATCH_KEY)));
})();
""")
        self.assertIs(out["unreadable"], True)
        self.assertIs(out["ever"], False)

    def test_the_health_payload_assigns_exactly_the_fields_the_watchdog_reads(self):
        """Static, because this is a NAMING contract between two files that
        cannot see each other at runtime. The list is taken from the watchdog's
        own reader so it cannot rot."""
        watchdog_code = watchdog_chain_python()
        read = set(re.findall(r"health\.get\('([a-z_]+)'\)", watchdog_code))
        self.assertTrue(read, "the watchdog arm reads no health fields, so it "
                              "cannot detect anything")
        worker = _function(_read(worker_path()), "healthForEndpoint")
        for field in sorted(read):
            self.assertRegex(
                worker, r"\.%s\s*=" % re.escape(field),
                "the watchdog reads health field %r but healthForEndpoint "
                "never assigns it, so the arm would fail closed forever on a "
                "field that cannot exist. Fields read: %s"
                % (field, sorted(read)))


# ==========================================================================
# (D) THE HONESTY GATE'S FAIL-SAFE MUST BE VISIBLE
# ==========================================================================

class AiFailSafeIsVisible(unittest.TestCase):
    """END TO END, and neither half is a stand-in.

    The fail-safe SURFACING lives in the workflow's `Summarize run outcome`
    step, not in breaking_monitor.py: the script writes one row to
    state/breaking_log.json (gitignored, never committed) and exits 0, and the
    workflow step is what turns that row into a signal. So this test does both
    halves for real:

      1. drive bm.main() with an AI runner that always fails, producing a REAL
         `ai_fail_safe` log row;
      2. execute the workflow's own summary-step python against that row.

    A grep for `::error::` in the YAML would pass on a broken implementation.
    """

    def _drive_fail_safe(self):
        """Real run, real log row. Returns (exit_code, stdout, log_json)."""
        now = bm.datetime.now(bm.timezone.utc)
        title = ("Magnitude 6.8 earthquake strikes near Example District, tremors felt in "
                 "Example City")
        pub = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
        xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><rss><channel>"
               "<item><title>%s</title>"
               "<link>https://example-news-a.com/q</link>"
               "<pubDate>%s</pubDate>"
               "<description>Tremors felt.</description>"
               "</item></channel></rss>" % (title, pub))
        url = "https://feeds.example.test/np.xml"

        def fetch(u):
            if url in u:
                return (200, xml)
            raise AssertionError("unexpected fetch %r" % u)

        def failing_runner(model, prompt_text, env, timeout_sec):
            raise RuntimeError("opencode stubbed unavailable for this test")

        sd = tempfile.mkdtemp(prefix="fail-safe-")
        self.addCleanup(shutil.rmtree, sd, True)
        sources_json = os.path.join(sd, "sources.json")
        bm.write_json_atomic(sources_json, [{
            "name": "test-feed", "url": url, "kind": "rss",
            "category": "local_city"}])
        argv = ["--state-dir", sd, "--sources-json", sources_json,
                "--window-min", "90", "--dry-run"]
        # Stripped for the duration: this test must be structurally unable to
        # publish to the owner's real topic, and it also proves the fail-safe
        # degrades to a visible annotation rather than vanishing.
        backup = {}
        for name in ("NTFY_TOPIC", "NTFY_SERVER"):
            backup[name] = os.environ.pop(name, None)
        self.addCleanup(lambda: [os.environ.__setitem__(k, v)
                                 for k, v in backup.items()
                                 if v is not None])
        buf = io.StringIO()
        real = sys.stdout
        sys.stdout = buf
        try:
            rc = bm.main(argv, fetch_fn=fetch, ai_runner=failing_runner)
        finally:
            sys.stdout = real
        with io.open(os.path.join(sd, "breaking_log.json"),
                     encoding="utf-8") as fh:
            return rc, buf.getvalue(), json.load(fh)

    def _run_summary_step(self, log_json):
        """Execute the workflow's OWN summary python against a real log row.

        Returns (combined_output, step_summary_text).
        """
        code = yaml_heredoc(BREAKING_WF, "ai_fail_safe")
        tmp = tempfile.mkdtemp(prefix="summary-step-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "state"))
        with io.open(os.path.join(tmp, "state", "breaking_log.json"), "w",
                     encoding="utf-8") as fh:
            json.dump(log_json, fh)
        summary = os.path.join(tmp, "summary.md")
        env = dict(os.environ)
        env.update({"PAYLOAD_TITLE": "", "TODAY_NPT": "",
                    "GITHUB_STEP_SUMMARY": summary,
                    "NTFY_TOPIC": "", "NTFY_SERVER": ""})
        proc = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        text = ""
        if os.path.isfile(summary):
            with io.open(summary, encoding="utf-8") as fh:
                text = fh.read()
        return proc.returncode, out, text

    def test_the_script_records_an_ai_fail_safe_row_and_exits_cleanly(self):
        rc, _out, rows = self._drive_fail_safe()
        self.assertEqual(0, rc, "a failed AI ladder must still exit 0")
        self.assertTrue(rows, "no log row was written, so there is nothing for "
                              "the workflow to surface")
        self.assertTrue(
            any((r.get("reason") or "").startswith("ai_fail_safe")
                for r in rows),
            "no row carries an ai_fail_safe reason: %r" % rows[-1])

    def test_the_fail_safe_surfaces_as_a_hard_error_annotation(self):
        """This is the requirement: a broken honesty gate must not be able to
        hide. Before this, the ONLY record was a gitignored file."""
        _rc, _out, log = self._drive_fail_safe()
        code, out, summary = self._run_summary_step(log)
        self.assertIn(
            "::error::", out,
            "the summary step reported the AI ladder's failure WITHOUT an "
            "::error:: annotation. A fail-safe nobody sees is indistinguishable "
            "from a quiet news day.\nstdout:\n%s" % out)
        self.assertIn("HONESTY GATE FAILED", summary,
                      "the run summary does not name the honesty-gate failure "
                      "as its own case, so it reads like an ordinary "
                      "no-send:\n%s" % summary)

    def test_the_failure_is_ntfy_pushed_and_says_so_when_unconfigured(self):
        _rc, _out, log = self._drive_fail_safe()
        _code, out, summary = self._run_summary_step(log)
        self.assertIn("ntfy_not_configured", out,
                      "with the ntfy secrets unset the step must say the "
                      "notice could not be sent. Reporting nothing is the "
                      "same silence this change exists to remove.\n%s" % out)
        self.assertIn("::warning::", out)
        self.assertIn("Honesty-gate notice", summary,
                      "the step summary does not state the honesty-gate "
                      "notice outcome at all:\n%s" % summary)

    def test_the_ladder_working_is_not_reported_as_a_gate_failure(self):
        """The mirror image, and the reason the case is named separately:
        `below_alert_level` means the ladder WORKED and declined to send.
        Printing both with the same words is how a broken gate hides inside a
        healthy log."""
        ok = [{"run_at": "2026-10-08T00:00:00+00:00", "checked": 40,
               "tripped": True, "sent": False, "level": 2,
               "confidence": "MEDIUM", "event_id": "",
               "reason": "below_alert_level"}]
        _code, out, summary = self._run_summary_step(ok)
        self.assertNotIn("::error::", out,
                         "a WORKING ladder that declined to send was reported "
                         "as a hard error:\n%s" % out)
        self.assertNotIn("HONESTY GATE FAILED", summary,
                         "a normal below_alert_level outcome was mislabelled as "
                         "an honesty-gate failure:\n%s" % summary)
        self.assertIn("below_alert_level", summary)

    def test_the_fail_safe_never_echoes_the_ntfy_topic_value(self):
        """The topic is a publish capability: referenced by NAME only."""
        code = yaml_heredoc(BREAKING_WF, "ai_fail_safe")
        self.assertNotIn("os.environ[\"NTFY_TOPIC\"]", code)
        self.assertNotIn("f\"{topic}\"", code)
        self.assertRegex(
            code, r"os\.environ\.get\(\"NTFY_TOPIC\"\)",
            "the topic must be read by name through .get(), never indexed "
            "and never interpolated into printed output")


if __name__ == "__main__":
    unittest.main(verbosity=2)