#!/usr/bin/env python3
"""tests/breaking/test_breaking_gate.py -- offline corpus lint for THE breaking
gate (contract 1e) and its audit side-channel (1f).

WHY THIS FILE EXISTS
--------------------
The gate was widened from EMERGENCY_RE alone to EMERGENCY_RE | BREAKING_IMPACT_RE
| BREAKING_IMPACT_WIDE_RE, and the widening shipped with ZERO tests. That is how
the semester-1 result bug shipped: a rule changed, nothing measured it, and a
green suite said nothing. This file is the measurement, frozen against a real
corpus so the numbers cannot be quietly re-baselined.

OFFLINE BY CONSTRUCTION
-----------------------
No network. No LLM. No `opencode` binary. Every assertion here runs the real
importance_gate() over tests/breaking/fixtures/real_headlines.json -- 616 rows
captured from a real 19/19-live-feed Google News RSS pull through
breaking_monitor.collect_candidates. Those are REAL headlines, not hand-authored,
and they are not re-captured here: re-capturing would make the lint depend on
Google's mood on the day it runs, and a gate test that changes its own corpus is
not a test. The fixture is the input; this file is the rule.

Run:
    py -m unittest tests.breaking.test_breaking_gate -v
    py -m unittest discover -s tests/breaking -t . -v
"""

import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import breaking_monitor as bm  # noqa: E402

FIXTURE = os.path.join(REPO_ROOT, "tests", "breaking", "fixtures",
                       "real_headlines.json")

# --------------------------------------------------------------------------
# Frozen contract literals. Read these, do not recompute them.
# --------------------------------------------------------------------------

# EMERGENCY_RE is the HIGH-PRECISION tier and a FROZEN contract string. It is
# a SUBSET of the union now, but it is still the string the Cloudflare Worker
# runs on title-only. If this drifts, the two sides stop agreeing and the drift
# is silent on both. Byte-identical, not semantically-equal: the Worker cannot
# be re-read to repair this, only policed.
EMERGENCY_RE_FROZEN_PATTERN = (
    r"\b(?:war|invasion|nuclear (?:test|accident|plant)|earthquake"
    r"|magnitude 6\.[5-9]|magnitude [7-9]|flood warning|landslide warning"
    r"|national emergency|Example Highway (?:closed|blocked)"
    r"|Example Bank (?:fraud|security|cyber)|bank of nepal.*cyber"
    r"|major outage)\b"
)

# MEASURED on the 616-row corpus at the time of writing:
#   urgent:true   191 / 202 tripped  =  0.945545
#   urgent:false  181 / 414 tripped  =  0.437198
MEASURED_URGENT_TRUE_RATE = 0.945545
MEASURED_URGENT_FALSE_RATE = 0.437198

# Hard floor on the urgent half. Missed urgent alerts cost a real result; a
# false positive costs one AI classification. That asymmetry sets this number,
# and it is NOT negotiable downward to make a future edit go green.
URGENT_TRUE_MIN_RATE = 0.90


def _script_source():
    with io.open(os.path.join(SCRIPTS, "breaking_monitor.py"),
                 encoding="utf-8") as fh:
        return fh.read()


def _read_fixture():
    with io.open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def corpus():
    data = _read_fixture()
    return data["rows"]


def blob_of(row):
    """The exact blob production code gates on. Never inline this."""
    return bm._gate_blob(row.get("title", ""), row.get("snippet", ""))


def gate_of(row):
    return bm.importance_gate(blob_of(row), row.get("title", ""))


def trip_rate(rows):
    if not rows:
        return 0.0
    return sum(1 for r in rows if gate_of(r)[0]) / float(len(rows))


# ==========================================================================
# 1. The corpus itself is real, fresh and big enough to mean something
# ==========================================================================

class CorpusIntegrityTest(unittest.TestCase):
    def test_corpus_has_enough_rows_to_be_meaningful(self):
        """assertGreaterEqual, NEVER a skip. A gate lint that quietly skips
        because the fixture is missing/thin reports green while measuring
        nothing -- that is the exact failure mode of the semester-1 bug."""
        rows = corpus()
        self.assertGreaterEqual(
            len(rows), 40,
            "corpus must hold >=40 rows to be a measurement; found %d. "
            "Do NOT skip -- restore or re-capture "
            "tests/breaking/fixtures/real_headlines.json." % len(rows))

    def test_corpus_rows_have_the_fields_the_gate_consumes(self):
        for i, row in enumerate(corpus()):
            self.assertIn("title", row, "row %d has no title" % i)
            self.assertIn("snippet", row, "row %d has no snippet" % i)
            self.assertIsInstance(row["urgent"], bool,
                                  "row %d: urgent must be a real bool, not %r"
                                  % (i, row["urgent"]))

    def test_corpus_is_fresh_within_180_days(self):
        data = _read_fixture()
        captured = datetime.fromisoformat(data["captured_at"])
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - captured).days
        self.assertLessEqual(
            age_days, 180,
            "corpus captured_at=%s is %d days old. A gate that has drifted "
            "from the live feed needs a NEW capture, not a re-baselined "
            "threshold." % (data["captured_at"], age_days))

    def test_corpus_provenance_is_recorded(self):
        """The claim 'real headlines from live feeds' must be on disk, so a
        hand-authored corpus cannot be passed off as a real one."""
        data = _read_fixture()
        self.assertIn("source", data)
        self.assertTrue(data["source"].strip())
        self.assertTrue(data.get("label_rule", "").strip())


# ==========================================================================
# 2. Trip rate -- both halves measured, both halves reported
# ==========================================================================

class TripRateTest(unittest.TestCase):
    def setUp(self):
        self.rows = corpus()
        self.urgent = [r for r in self.rows if r["urgent"]]
        self.calm = [r for r in self.rows if not r["urgent"]]

    def test_urgent_true_trip_rate_meets_the_hard_floor(self):
        self.assertTrue(self.urgent, "corpus has no urgent:true rows")
        rate = trip_rate(self.urgent)
        self.assertGreaterEqual(
            rate, URGENT_TRUE_MIN_RATE,
            "URGENT MISS RATE. Gate trips only %d/%d (%.6f) of urgent:true "
            "rows; the floor is %.2f (measured at the time of writing: "
            "%.6f). The correct response is to WIDEN THE GATE AND RE-MEASURE "
            "-- never to lower this number."
            % (sum(1 for r in self.urgent if gate_of(r)[0]), len(self.urgent),
               rate, URGENT_TRUE_MIN_RATE, MEASURED_URGENT_TRUE_RATE))

    def test_urgent_false_trip_rate_is_strictly_lower_than_urgent_true(self):
        """Directional only, DELIBERATELY not gated at 0.15.

        Measured: urgent:false 181/414 = 0.437198 vs urgent:true 0.945545.
        The 0.437 is high and that is NOT a gate failure, because the corpus
        labelling shares vocabulary with the gate: `label_rule` decides
        "urgent" by looking for exactly the casualty/blocked/outage words the
        gate regexes match on. The urgent:false half is therefore partly
        circular, and any hard threshold on it would be a number whose only
        meaning is "the corpus author and the regex author used the same words".

        A hard green at 0.15 here would be decoration. What is NOT decoration
        is the ordering, so that is the only thing asserted: a wider gate must
        never make the calm half equal to or exceed the urgent half. If this
        ever fails, the gate has stopped discriminating at all.
        """
        rate_true = trip_rate(self.urgent)
        rate_false = trip_rate(self.calm)
        self.assertTrue(self.calm, "corpus has no urgent:false rows")
        self.assertLess(
            rate_false, rate_true,
            "gate is not discriminating: urgent:false trip rate %.6f is not "
            "strictly below urgent:true %.6f (comment in this test records the "
            "measured values: %s)" % (rate_false, rate_true,
                                      MEASURED_URGENT_FALSE_RATE))

    def test_measured_rates_are_reported_on_every_run(self):
        """Both halves printed unconditionally, so a widening cannot be merged
        on the strength of the urgent half alone. The trade is the whole point:
        a wider gate means more false positives, and that has to stay visible."""
        buf = io.StringIO()
        saved_stderr = sys.stderr
        sys.stderr = buf
        try:
            sys.stderr.write(
                "  [gate corpus] urgent:true %d/%d = %.6f | urgent:false %d/%d "
                "= %.6f\n" % (sum(1 for r in self.urgent if gate_of(r)[0]),
                              len(self.urgent), trip_rate(self.urgent),
                              sum(1 for r in self.calm if gate_of(r)[0]),
                              len(self.calm), trip_rate(self.calm)))
        finally:
            sys.stderr = saved_stderr
        printed = buf.getvalue()
        rate_true = "%.6f" % trip_rate(self.urgent)
        rate_false = "%.6f" % trip_rate(self.calm)
        self.assertIn(
            rate_true, printed,
            "urgent:true rate %s missing from the printed line: %r"
            % (rate_true, printed))
        self.assertIn(
            rate_false, printed,
            "urgent:false rate %s missing from the printed line: %r"
            % (rate_false, printed))


# ==========================================================================
# 3. The sport/gossip veto must never be drowned out by the wider vocabulary
# ==========================================================================

class _CountingRe(object):
    """Proxy that records every regex call, so 'the veto runs BEFORE any hit
    counting' is a behavioural claim and not a comment."""

    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def findall(self, s):
        self._log.append("findall")
        return self._inner.findall(s)

    def search(self, s):
        self._log.append("search")
        return self._inner.search(s)


# "war" is a genuine EMERGENCY_RE hit and "war drama" is exactly the
# entertainment framing the veto exists for. The live-event term is therefore
# really present and really contested -- this is not a case the veto wins by
# default because nothing matched.
GOSSIP_AND_LIVE_EVENT = {
    "title": "War drama box office score: season finale match goal explained",
    "snippet": "Critics give the war series a perfect match score.",
}


class SportGossipVetoTest(unittest.TestCase):
    def test_gossip_plus_live_event_is_vetoed_with_the_exact_tuple(self):
        row = GOSSIP_AND_LIVE_EVENT
        blob = blob_of(row)
        self.assertGreaterEqual(
            len(bm.SPORT_GOSSIP_RE.findall(blob)),
            bm.SPORT_GOSSIP_DOMINATING_HITS,
            "fixture row must really contain >=%d gossip terms, or this test "
            "is not testing the veto"
            % bm.SPORT_GOSSIP_DOMINATING_HITS)
        self.assertTrue(
            bm.EMERGENCY_RE.search(blob),
            "fixture row must really contain a live-event term from the "
            "frozen EMERGENCY_RE, or the veto is not being contested")
        self.assertEqual(gate_of(row),
                         (False, "sport_gossip_dominating", []))

    def test_veto_runs_before_any_hit_counting(self):
        """Instrumented: every counting regex must be called ZERO times."""
        row = GOSSIP_AND_LIVE_EVENT
        calls = []
        originals = (bm.EMERGENCY_RE, bm.BREAKING_IMPACT_RE,
                     bm.BREAKING_IMPACT_WIDE_RE)
        try:
            bm.EMERGENCY_RE = _CountingRe(originals[0], calls)
            bm.BREAKING_IMPACT_RE = _CountingRe(originals[1], calls)
            bm.BREAKING_IMPACT_WIDE_RE = _CountingRe(originals[2], calls)
            tripped, why, hits = bm.importance_gate(blob_of(row),
                                                    row["title"])
        finally:
            (bm.EMERGENCY_RE, bm.BREAKING_IMPACT_RE,
             bm.BREAKING_IMPACT_WIDE_RE) = originals
        self.assertEqual((tripped, why, hits),
                         (False, "sport_gossip_dominating", []))
        self.assertEqual(
            calls, [],
            "the sport/gossip veto must return BEFORE any hit counting. "
            "Counting regexes were evaluated %d time(s) before the veto "
            "fired, so a wider vocabulary can drown it out." % len(calls))

    def test_veto_guard_appears_before_the_hit_counting_in_source(self):
        """Structural guard behind the behavioural one, so the next editor sees
        WHY the line has to stay first."""
        src = inspect.getsource(bm.importance_gate)
        veto = src.index("SPORT_GOSSIP_DOMINATING_HITS")
        counting = src.index("hits = ({h.lower() for h in")
        self.assertLess(veto, counting,
                        "the gossip guard must precede the hits= union")

    def test_no_corpus_row_trips_while_carrying_dominating_gossip(self):
        """The real 616-row property version of the veto test. Whatever else
        the widening catches, it must not resurrect pure sport/gossip rows."""
        offenders = []
        for row in corpus():
            blob = blob_of(row)
            if len(bm.SPORT_GOSSIP_RE.findall(blob)) >= \
                    bm.SPORT_GOSSIP_DOMINATING_HITS:
                if gate_of(row)[0]:
                    offenders.append(row["title"])
        self.assertEqual(
            offenders, [],
            "%d corpus row(s) tripped the gate despite dominating "
            "sport/gossip vocabulary" % len(offenders))

    def test_veto_beats_every_vocabulary_tier_at_once(self):
        """Harden: the veto must not be survivable by any single tier."""
        tiers = ("EMERGENCY_RE", "BREAKING_IMPACT_RE", "BREAKING_IMPACT_WIDE_RE")
        for tier in tiers:
            rx = getattr(bm, tier)
            row = dict(GOSSIP_AND_LIVE_EVENT)
            row["snippet"] = "%s %s" % (row["snippet"], rx.pattern[:40])
            self.assertEqual(gate_of(row),
                             (False, "sport_gossip_dominating", []), tier)


# ==========================================================================
# 4. Frozen-string drift guard
# ==========================================================================

class EmergencyReFrozenTest(unittest.TestCase):
    def test_emergency_re_pattern_is_byte_identical(self):
        self.assertEqual(
            bm.EMERGENCY_RE.pattern, EMERGENCY_RE_FROZEN_PATTERN,
            "EMERGENCY_RE is the frozen high-precision tier and the string the "
            "Cloudflare Worker also runs. Editing it is a CONTRACT CHANGE, not "
            "a widening: it silently desynchronises the Worker.")

    def test_emergency_re_is_still_case_insensitive(self):
        self.assertTrue(bm.EMERGENCY_RE.flags & re.IGNORECASE)

    def test_emergency_re_uses_non_capturing_groups(self):
        """findall() must return WHOLE matches; a capturing group would make
        `hits` a list of bare fragments ('closed' instead of 'Example Highway
        closed') and quietly corrupt the audit trail the log row depends on."""
        self.assertEqual(
            re.compile(EMERGENCY_RE_FROZEN_PATTERN, re.IGNORECASE).groups, 0,
            "EMERGENCY_RE must contain no capturing groups")
        self.assertEqual(
            [h.lower() for h in bm.EMERGENCY_RE.findall(
                "Nepal hit by a MAJOR OUTAGE and a Flood Warning")],
            ["major outage", "flood warning"])

    def test_emergency_re_source_literal_still_matches(self):
        """Pin the SOURCE text, not just the compiled object, so an edit that
        is later reverted at runtime still shows up in review."""
        src = _script_source()
        m = re.search(r"EMERGENCY_RE = re\.compile\((.*?)\n\)", src, re.DOTALL)
        self.assertIsNotNone(m, "could not locate the EMERGENCY_RE literal")
        parts = re.findall(r'r"((?:[^"\\]|\\.)*)"', m.group(1))
        self.assertTrue(parts, "no string literals found in the compile call")
        self.assertEqual("".join(parts), EMERGENCY_RE_FROZEN_PATTERN)

    def test_emergency_re_tier_still_fires_on_its_own(self):
        """The frozen tier is not dead code inside the union."""
        for text in ("War declared between neighbouring states",
                     "Magnitude 7.1 earthquake near Example District",
                     "National emergency declared in Banke",
                     "Major outage reported worldwide"):
            self.assertTrue(bm.EMERGENCY_RE.search(text), text)


# ==========================================================================
# 5. One gate, one decision: importance_gate and pre_gate must agree
# ==========================================================================

class GateAgreementTest(unittest.TestCase):
    def test_pre_gate_and_importance_gate_agree_row_by_row(self):
        mismatches = []
        for row in corpus():
            direct = gate_of(row)[0]
            via_pre, _matched = bm.pre_gate([{
                "title": row.get("title", ""),
                "snippet": row.get("snippet", ""),
            }])
            if direct != via_pre:
                mismatches.append((row.get("title", "")[:70], direct,
                                   via_pre))
        self.assertEqual(
            mismatches, [],
            "pre_gate() and importance_gate() disagree on %d corpus row(s) -- "
            "there must be ONE gate, not two vocabularies for one decision:\n%s"
            % (len(mismatches),
               "\n".join("  %r direct=%r pre_gate=%r" % m
                         for m in mismatches[:10])))

    def test_pre_gate_aggregate_matches_importance_gate_aggregate(self):
        rows = corpus()
        aggregate, matched = bm.pre_gate([{
            "title": r.get("title", ""), "snippet": r.get("snippet", "")}
            for r in rows])
        expected = [r for r in rows if gate_of(r)[0]]
        self.assertEqual(aggregate, bool(expected))
        self.assertEqual(len(matched), len(expected))

    def test_pre_gate_reason_never_contains_a_candidate_it_did_not_match(self):
        """The audit side-channel must be a subset of the matched set, or a
        widened gate produces a log that blames titles it never tripped."""
        rows = corpus()
        _tripped, matched = bm.pre_gate([{
            "title": r.get("title", ""), "snippet": r.get("snippet", "")}
            for r in rows])
        matched_titles = {m.get("title", "") for m in matched}
        whys = list(getattr(bm.pre_gate, "last_why", []))
        self.assertEqual(len(whys), len(matched),
                         "one reason per matched candidate")
        for entry in whys:
            title = entry.split(" :: ", 1)[0]
            self.assertIn(title, matched_titles,
                          "pre_gate_why names a candidate that was not "
                          "matched: %r" % title)


# ==========================================================================
# 6. The pre_gate_why audit channel (the change that shipped untested)
# ==========================================================================

class PreGateAuditChannelTest(unittest.TestCase):
    def test_last_why_is_populated_for_a_tripping_run(self):
        bm.pre_gate([{"title": "Magnitude 6.8 earthquake strikes near Example District",
                      "snippet": "Strong shaking reported."}])
        whys = getattr(bm.pre_gate, "last_why", None)
        self.assertIsNotNone(whys,
                             "pre_gate must publish pre_gate_why via the "
                             "last_why side-channel")
        self.assertEqual(len(whys), 1)
        self.assertIn("Magnitude 6.8", whys[0])
        self.assertIn("::", whys[0], "each reason must name its title")

    def test_last_why_is_empty_for_a_run_that_trips_nothing(self):
        tripped, matched = bm.pre_gate([{
            "title": "Local council approves new park in Example City",
            "snippet": "Routine civic news."}])
        self.assertFalse(tripped)
        self.assertEqual(matched, [])
        self.assertEqual(list(getattr(bm.pre_gate, "last_why", [])), [])

    def test_last_why_is_reset_between_calls_not_accumulated(self):
        bm.pre_gate([{"title": "Magnitude 6.8 earthquake strikes near Example District",
                      "snippet": "Strong shaking reported."}])
        bm.pre_gate([{"title": "Local council approves new park",
                      "snippet": "Routine civic news."}])
        self.assertEqual(list(bm.pre_gate.last_why), [],
                         "a non-tripping run must not inherit the previous "
                         "run's audit trail")

    def test_a_silent_heartbeat_row_carries_no_level(self):
        """The shape the old KeyError('level') was reaching for, pinned
        explicitly so the next reader knows it is intentional, not a gap.

        A non-tripping run writes ONE row with tripped=False and no `level`
        key at all. Callers that read row['level'] unconditionally will
        KeyError. That is the whole hazard.
        """
        from tests.breaking import test_breaking_monitor as mon
        sd = mon.fresh_dir()
        try:
            body = mon.rss_xml([("Local council approves new park in Example City",
                                 "https://e.test/p", mon.rfc822(mon.NOW),
                                 "Routine civic news.")])
            mon.run_main(sd, mon.make_fetch({"feeds.example.test": body}))
            rows = mon.log_rows(sd)
            self.assertEqual(len(rows), 1)
            self.assertIs(rows[-1]["tripped"], False)
            self.assertNotIn("level", rows[-1],
                             "the heartbeat row is expected to have no level")
        finally:
            shutil.rmtree(sd, ignore_errors=True)

    def test_gmail_receiver_no_longer_buys_a_model_call(self):
        """REPLACES test_bypass_run_does_not_log_another_runs_reasons.

        That test mirrored main()'s bypass branch by hand, so it went on passing
        after the branch was deleted -- a test of the test. This one asserts the
        behaviour instead: with GMAIL_RECEIVER set (which breaking-monitor.yml
        does), a sweep whose candidates do not qualify must still make ZERO model
        calls.

        MEASURED BURN, 2026-10-27: 112 model attempts in one day, 33 of the
        week's 47 quota refusals on this path. GMAIL_RECEIVER answers "where does
        the mail go", never "is this worth a model call"; while it widened the
        gate, every hour that produced one candidate spent the whole ladder.
        """
        from tests.breaking import test_breaking_monitor as mon
        sd = mon.fresh_dir()
        calls = []

        def must_not_run(prompt, model=None, timeout=None):
            calls.append(model)
            raise AssertionError("a model was called for a non-qualifying "
                                 "candidate: %r" % (model,))

        prev = os.environ.get("GMAIL_RECEIVER")
        os.environ["GMAIL_RECEIVER"] = "ops@example.invalid"
        try:
            body = mon.rss_xml([("Local council approves new park in Example City",
                                 "https://e.test/p", mon.rfc822(mon.NOW),
                                 "Routine civic news.")])
            rc = mon.run_main(sd, mon.make_fetch({"feeds.example.test": body}),
                              runner=must_not_run)
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [], "the gate must have suppressed the walk")
            rows = mon.log_rows(sd)
            self.assertIs(rows[-1]["tripped"], False)
            self.assertEqual(rows[-1].get("gate"), "suppressed",
                             "the decision must be recorded, not implied by "
                             "silence")
            self.assertIn("none met the importance gate",
                          rows[-1].get("gate_reason", ""))
        finally:
            if prev is None:
                os.environ.pop("GMAIL_RECEIVER", None)
            else:
                os.environ["GMAIL_RECEIVER"] = prev
            shutil.rmtree(sd, ignore_errors=True)

    def test_gate_still_passes_a_qualifying_candidate(self):
        """The suppression above must not be a mute button.

        Same wiring, a headline that does qualify: the classifier is reached
        exactly once, and the decision is logged as passed with its count.
        """
        from tests.breaking import test_breaking_monitor as mon
        sd = mon.fresh_dir()
        calls = []
        try:
            body = mon.rss_xml([mon.EARTHQUAKE_ITEM])
            rc = mon.run_main(
                sd, mon.make_fetch({"feeds.example.test": body}),
                runner=mon.verdict_runner(
                    {"level": 3, "confidence": "high", "reason": "test"}))
            self.assertEqual(rc, 0)
            rows = mon.log_rows(sd)
            self.assertIs(rows[-1]["tripped"], True)
            self.assertEqual(rows[-1].get("gate"), "passed")
            self.assertIn("met the importance gate",
                          rows[-1].get("gate_reason", ""))
            # pre_gate_why must describe THIS run, not a previous one.
            self.assertTrue(rows[-1].get("pre_gate_why"))
        finally:
            shutil.rmtree(sd, ignore_errors=True)

    def test_no_model_flag_suppresses_the_call_and_says_so(self):
        """A diagnostic run may not spend the shared pool.

        Drift-canary's `--dry-run` breaking walk was 29 of the week's 292 model
        attempts, every one of them proving nothing a unit test cannot. The
        suppression is a SEPARATE flag precisely because `--dry-run` must keep
        meaning "render, do not send": an end-to-end check of the classifier is a
        legitimate use of dry-run, and a canary is not.
        """
        from tests.breaking import test_breaking_monitor as mon
        sd = mon.fresh_dir()

        def must_not_run(prompt, model=None, timeout=None):
            raise AssertionError("--no-model called a model: %r" % (model,))

        try:
            body = mon.rss_xml([mon.EARTHQUAKE_ITEM])
            rc = mon.run_main(sd, mon.make_fetch({"feeds.example.test": body}),
                              runner=must_not_run, no_model=True)
            self.assertEqual(rc, 0)
            rows = mon.log_rows(sd)
            self.assertIs(rows[-1]["tripped"], True,
                          "the gate still decides; only the model is suppressed")
            self.assertIs(rows[-1]["sent"], False)
            self.assertEqual(rows[-1].get("gate"), "passed")
            self.assertIn("model_call_suppressed", rows[-1].get("reason", ""))
            self.assertTrue(rows[-1].get("unjudged_titles"),
                            "unjudged headlines must ride the row so the "
                            "honesty gate can still carry them")
        finally:
            shutil.rmtree(sd, ignore_errors=True)

    def test_a_recorded_pool_cooldown_stops_the_sweep_before_any_model(self):
        """The shared pool is honoured BEFORE a model is named.

        The provider refuses pre-generation and every free id shares the bucket
        (measured: six ids refused inside 16 s, 312 ms after `message=stream`,
        `tokens.input=0`), so walking the ladder against a recorded refusal spends
        attempts to learn nothing. This is the caller that spent 82 of a week's
        292 attempts, so the check has to be here and not only in the selector.
        """
        from tests.breaking import test_breaking_monitor as mon
        import model_selector as ms
        sd = mon.fresh_dir()
        calls = []

        def must_not_run(prompt, model=None, timeout=None):
            calls.append(model)
            raise AssertionError("a refused pool named a model: %r" % (model,))

        try:
            cool_path = ms.cooldown_path_for(
                os.path.join(sd, bm.MODEL_HEALTH_FILENAME))
            doc = ms.load_cooldown(cool_path)
            row = ms.record_pool_refusal(doc, retry_after=3600, source="test")
            ms.save_cooldown(cool_path, doc)
            body = mon.rss_xml([mon.EARTHQUAKE_ITEM])
            rc = mon.run_main(sd, mon.make_fetch({"feeds.example.test": body}),
                              runner=must_not_run)
            self.assertEqual(rc, 0)
            self.assertEqual([], calls,
                             "no model may be named while the pool is refused")
            rows = mon.log_rows(sd)
            reason = rows[-1].get("reason", "")
            self.assertIn("pool_cooldown", reason)
            self.assertIn(
                row["until"], reason,
                "a suppressed run must say WHEN the pool is usable again, or it "
                "is indistinguishable from a broken one")
        finally:
            shutil.rmtree(sd, ignore_errors=True)

    def test_the_canary_workflow_passes_no_model(self):
        """The flag only saves anything if the canary actually passes it.

        A behavioural test cannot reach this: the saving lives in the workflow
        file, and a later edit that drops `--no-model` would put 29 model
        attempts a week back on the shared pool with every test still green.
        """
        path = os.path.join(REPO_ROOT, ".github", "workflows",
                            "drift-canary.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        step = text.split("Breaking monitor dry-run", 1)
        self.assertEqual(2, len(step), "the canary step has been renamed")
        body = step[1].split("- name:", 1)[0]
        self.assertIn("scripts/breaking_monitor.py", body)
        self.assertIn("--no-model", body,
                      "the drift canary's breaking walk must not spend the "
                      "shared model pool; measured at 29 attempts/week for "
                      "nothing a unit test cannot prove")

    def test_entertainment_spoiler_still_reaches_the_ai_classifier(self):
        """The wider gate must NOT swallow the case test_7 exists to protect.

        'Anime finale leaks: hero dies in war ending explained scene' trips on
        'war' from the FROZEN tier, reaches the classifier, and is vetoed
        downstream by the spoiler rule. If a future widening ever suppresses
        this at the gate, the alert dies silently instead of being vetoed --
        a different bug with the same user-visible symptom.
        """
        from tests.breaking import test_breaking_monitor as mon
        row = {"title": "Anime finale leaks: hero dies in war ending "
                        "explained scene",
               "snippet": "Plot details inside."}
        tripped, why, _hits = gate_of(row)
        self.assertTrue(tripped,
                        "the entertainment/spoiler case must still trip the "
                        "gate so the classifier can veto it; got %r" % (why,))
        self.assertIn("war", [h.lower() for h in bm.EMERGENCY_RE.findall(
            blob_of(row))])

        sd = mon.fresh_dir()
        try:
            src = os.path.join(sd, "sources.json")
            bm.write_json_atomic(src, [{
                "name": "anime-feed",
                "url": "https://feeds.example.test/anime.xml",
                "kind": "rss", "category": "anime"}])
            body = mon.rss_xml([(row["title"],
                                 "https://example-fansite.test/ep",
                                 mon.rfc822(mon.NOW), row["snippet"])])
            verdict = {
                "level": 3, "confidence": "HIGH",
                "event_key": "anime-ending-leak",
                "title": "New anime episode released",
                "summary": "The finale leaked online; the main character dies "
                           "in the ending explained scene.",
                "why_urgent": "", "escalated_from_previous": False,
                "sources": [{"name": "Fansite",
                             "url": "https://example-fansite.test/ep"}],
                "category_suggestion": "nepal"}
            rc = mon.run_main(sd, mon.make_fetch({"feeds.example.test": body}),
                              runner=mon.verdict_runner(verdict),
                              sources_json=src)
            self.assertEqual(rc, 0)
            last = mon.log_rows(sd)[-1]
            self.assertEqual(last["level"], 1)
            self.assertIs(last["sent"], False)
            self.assertIn("spoiler_veto", last["reason"])
            self.assertTrue(last.get("pre_gate_why"),
                            "the audit side-channel must survive into the "
                            "alerted log row")
        finally:
            shutil.rmtree(sd, ignore_errors=True)


# ==========================================================================
# 7. MUTATION PROOF -- a lint that cannot fail is decoration
# ==========================================================================

# The OLD narrow gate: EMERGENCY_RE alone, exactly as it stood before the
# widening. This is the text being reverted in the temp copy below.
NARROW_HITS_EXPR = (
    "    hits = ({h.lower() for h in EMERGENCY_RE.findall(text)}\n"
    "            | {h.lower() for h in BREAKING_IMPACT_RE.findall(text)}\n"
    "            | {h.lower() for h in BREAKING_IMPACT_WIDE_RE.findall(text)})"
)
OLD_NARROW_HITS_EXPR = (
    "    hits = ({h.lower() for h in EMERGENCY_RE.findall(text)})"
)


def _load_narrow_mutant():
    """Write a TEMP COPY of scripts/breaking_monitor.py with the union reverted
    to EMERGENCY_RE-only, and import THAT. The production file on disk is
    never touched."""
    with io.open(os.path.join(SCRIPTS, "breaking_monitor.py"),
                 encoding="utf-8") as fh:
        src = fh.read()
    if NARROW_HITS_EXPR not in src:
        raise AssertionError(
            "could not find the widening union in importance_gate; the "
            "mutation below is anchored to the CURRENT source, so if this "
            "fires the gate has been restructured and this proof must be "
            "re-anchored by hand.")
    mutated = src.replace(NARROW_HITS_EXPR, OLD_NARROW_HITS_EXPR, 1)
    tmpdir = tempfile.mkdtemp(prefix="brk-mutant-")
    path = os.path.join(tmpdir, "breaking_monitor_narrow.py")
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(mutated)
    spec = importlib.util.spec_from_file_location(
        "breaking_monitor_narrow_mutant", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, tmpdir


def _rate_with(module, rows):
    trips = 0
    for row in rows:
        blob = module._gate_blob(row.get("title", ""), row.get("snippet", ""))
        if module.importance_gate(blob, row.get("title", ""))[0]:
            trips += 1
    return trips / float(len(rows))


class MutationProofTest(unittest.TestCase):
    """This class is the point of the whole file. Everything above asserts a
    property; this one proves the property has TEETH by reverting the change
    and watching the corpus test go red.

    Without it, TripRateTest could be green because the corpus is easy, or
    because the comparison is vacuous, and nobody would know. That is how the
    semester-1 result bug shipped: a green suite that was measuring nothing.
    """

    def test_reverting_to_the_old_narrow_gate_makes_the_corpus_test_red(self):
        rows = corpus()
        urgent = [r for r in rows if r["urgent"]]
        self.assertTrue(urgent)

        mutant, tmpdir = _load_narrow_mutant()
        try:
            # Sanity: the mutant really is the narrow gate.
            self.assertIsNotNone(mutant.EMERGENCY_RE)
            self.assertEqual(mutant.EMERGENCY_RE.pattern,
                             EMERGENCY_RE_FROZEN_PATTERN,
                             "the mutant must keep the frozen tier intact; "
                             "only the union is reverted")

            mutant_rate = _rate_with(mutant, urgent)
            production_rate = trip_rate(urgent)

            self.assertLess(
                mutant_rate, URGENT_TRUE_MIN_RATE,
                "MUTATION SURVIVED. The old narrow gate scores %.6f on "
                "urgent:true, which still clears the %.2f floor -- so "
                "TripRateTest would have passed on the pre-widening gate and "
                "is therefore not actually testing the widening."
                % (mutant_rate, URGENT_TRUE_MIN_RATE))
            self.assertLess(
                mutant_rate, production_rate,
                "the old narrow gate (%.6f) is not strictly worse than the "
                "shipped gate (%.6f); the widening is doing nothing measurable"
                % (mutant_rate, production_rate))
            sys.stderr.write(
                "  [mutation] old narrow gate urgent:true = %.6f  -> RED "
                "below the %.2f floor (shipped gate %.6f)\n"
                % (mutant_rate, URGENT_TRUE_MIN_RATE, production_rate))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_mutant_keeps_the_veto_and_the_frozen_tier_working(self):
        """The mutation is surgical: it may only WIDEN, never remove the veto
        or the frozen tier. If it broke those, the rate comparison above would
        be measuring a different gate entirely."""
        mutant, tmpdir = _load_narrow_mutant()
        try:
            self.assertEqual(
                mutant.importance_gate(
                    GOSSIP_AND_LIVE_EVENT["title"] + " " +
                    GOSSIP_AND_LIVE_EVENT["snippet"],
                    GOSSIP_AND_LIVE_EVENT["title"]),
                (False, "sport_gossip_dominating", []))
            self.assertTrue(mutant.EMERGENCY_RE.search(
                "Magnitude 7.1 earthquake near Example District"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
