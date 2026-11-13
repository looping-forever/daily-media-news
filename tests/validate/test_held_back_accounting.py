#!/usr/bin/env python3
"""tests/validate/test_held_back_accounting.py — two pools, two counts, no sum.

WHAT THIS PINS
--------------
Run 43246133293 logged, in order:

    held back                7 (title not in English)
    derive_unsummarised: 20 stories from 53 candidates
    ... provenance: candidates=0 raw=20 ... final=15

and then the edition note said "7 stories were held back because they are not in
English" on a 15-card page. Every number there is a real measurement of a
DIFFERENT population, and none of them balances against the sentence:

    candidates   53 = 7 held (not English) + 46 considered, of which 20 became stories
    stories      20 = 15 shipped + 5 dropped (2 already sent, 3 past the rank cap)

53-7 is 46, not 20. 20-7 is 13, not 15. The 7 was a CANDIDATE count and the
sentence claimed stories. Summing the two pools ("already + dropped") was the
previous design and made the disagreement worse, because neither stage knew the
other's denominator.

The rule pinned here:

  * the deriver counts EVERY refusal by reason, so its pool reconciles in one
    printed line (pool = used + held + refused);
  * the payload carries `count` WITH `pool`, and `stories_dropped` separately;
  * the sentence names its own population ("7 of the 53 collected candidates")
    and the story-population sentence is a different sentence, never an
    addition;
  * `held_back_record` keeps the two apart, whatever arrives in the payload.

Offline: temp directories, literals, no provider, stdlib unittest.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from . import REPO_ROOT  # noqa: F401  (REPO_ROOT puts scripts/ on sys.path)

import derive_unsummarised as dut
import english_text as et
import validate_stories as vs

DEVANAGARI = "\u0906\u091c \u0915\u093e\u0920\u092e\u093e\u0921\u094c\u0902\u092e\u093e"
NOW = dut.datetime(2026, 11, 12, 12, 0, tzinfo=dut.timezone.utc)

GOOD = [
    "Nepal government announces the annual budget for next year",
    "Kathmandu valley reports steady rain through the whole afternoon",
    "University exam schedule published for the autumn semester students",
    "Local bank raises interest rates on fixed deposits from next month",
    "New anime season confirmed by the studio with a teaser for fans",
]


def cand(title, url, category="local_city"):
    return {"title": title, "url": url, "source": "feed",
            "domain_category": category, "language": "en", "snippet": "",
            "published": "2026-11-12T09:00:00+00:00"}


def run_main(candidates):
    tmp = tempfile.mkdtemp(prefix="held-back-")
    in_path = os.path.join(tmp, "candidates.json")
    out_path = os.path.join(tmp, "stories.json")
    log = os.path.join(tmp, "agent-attempts.log")
    with open(in_path, "w", encoding="utf-8") as fh:
        json.dump({"candidates": candidates}, fh)
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = dut.main(["--candidates", in_path, "--out", out_path,
                       "--today", "2026-11-12", "--slot", "morning",
                       "--attempts", log])
    payload = {}
    if os.path.isfile(out_path):
        with open(out_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    return rc, payload, buf.getvalue()


class TheDeriverAccountsForEveryCandidate(unittest.TestCase):
    def test_each_refusal_is_counted_under_its_own_reason(self):
        held = {}
        stories, _counts, _skipped = dut.derive([
            cand(GOOD[0], "https://a.example/1"),
            cand(GOOD[1], "https://a.example/2"),
            cand(GOOD[2], "https://a.example/3"),
            cand(DEVANAGARI + " announces a new plan today",
                 "https://a.example/np"),
            cand("Brief note", "https://a.example/short"),
            cand(GOOD[3], "https://a.example/bad", category="not_a_category_xyz"),
        ], now=NOW, held=held)

        self.assertEqual(3, len(stories))
        self.assertEqual({"not_english": 1, "title_too_short": 1,
                          "not_a_category": 1}, held.get("refused"),
                         "a refusal with no counter is a candidate that "
                         "vanishes from the arithmetic")

    def test_the_pool_adds_up(self):
        held = {}
        stories, _c, _s = dut.derive([
            cand(GOOD[0], "https://a.example/1"),
            cand(GOOD[1], "https://a.example/2"),
            cand(DEVANAGARI + " announces a new plan today",
                 "https://a.example/np"),
            cand("Brief note", "https://a.example/short"),
        ], now=NOW, held=held)
        refused = sum(held["refused"].values())
        self.assertEqual(held["pool"], len(stories) + refused,
                         "the pool does not reconcile: %r used, %r refused"
                         % (len(stories), held["refused"]))

    def test_a_near_duplicate_is_a_named_refusal_not_silence(self):
        held = {}
        _stories, _c, _s = dut.derive([
            cand(GOOD[0], "https://a.example/1"),
            cand(GOOD[0], "https://a.example/2"),
        ], now=NOW, held=held)
        self.assertEqual(1, held["refused"].get("near_duplicate"))

    def test_a_non_dict_row_is_counted_rather_than_skipped(self):
        held = {}
        _stories, _c, _s = dut.derive(["not a dict"], now=NOW, held=held)
        self.assertEqual({"not_a_candidate": 1}, held["refused"])
        self.assertEqual(1, held["pool"])


class ThePayloadCarriesThePool(unittest.TestCase):
    def candidates(self):
        rows = [cand(GOOD[n], "https://a.example/%d" % n,
                     category=("world_major" if n < 3 else "local_city"))
                for n in range(5)]
        rows.append(cand(DEVANAGARI + " announces a new plan today",
                         "https://a.example/np"))
        return rows

    def test_the_count_travels_with_its_own_population(self):
        rc, payload, _out = run_main(self.candidates())
        self.assertEqual(0, rc)
        self.assertEqual({"count": 1, "pool": 6, "stories_dropped": 0,
                          "reason": "not_english"}, payload.get("held_back"))

    def test_the_note_names_the_population_it_counts(self):
        _rc, payload, _out = run_main(self.candidates())
        self.assertIn("1 of the 6 collected candidates was held back because "
                      "its title is not in English.", payload["notes"])
        self.assertNotIn("1 story was held back", payload["notes"],
                         "a candidate count is still being stated as a story "
                         "count, which is the sentence that did not balance")

    def test_the_run_prints_a_reconciliation_line(self):
        _rc, _payload, out = run_main(self.candidates())
        self.assertIn("6 in = 5 used + 1 held + 0 other (none)", out,
                      "the pool does not reconcile in the log, so a reader has "
                      "to subtract numbers that were never printed: %r" % out)


class TwoPoolsAreNeverSummed(unittest.TestCase):
    def test_the_record_keeps_the_pools_apart(self):
        record = vs.held_back_record(
            {"held_back": {"count": 7, "pool": 53}}, 2)
        self.assertEqual({"count": 7, "pool": 53, "stories_dropped": 2}, record)
        self.assertNotEqual(9, record["count"],
                            "the two pools are being added again, and 7+2 "
                            "describes no population that exists")

    def test_a_malformed_record_is_zero_not_a_crash(self):
        for record in ({"held_back": "6"}, {"held_back": True},
                       {"held_back": {"count": "many"}}, {}, None):
            self.assertEqual({"count": 0, "pool": 0, "stories_dropped": 3},
                             vs.held_back_record(record, 3), "record %r" % (record,))

    def test_the_two_sentences_are_visibly_different(self):
        candidates = et.held_back_note(7, pool=53)
        stories = et.held_back_note(7)
        self.assertEqual("7 of the 53 collected candidates were held back "
                         "because their titles are not in English.", candidates)
        self.assertEqual("7 stories were held back because they are not in "
                         "English.", stories)

    def test_both_clauses_are_written_when_both_pools_dropped_something(self):
        notes = et.replace_held_back("Research note.", 7, pool=53, stories=2)
        self.assertIn("7 of the 53 collected candidates", notes)
        self.assertIn("2 stories were held back", notes)
        self.assertEqual(1, len(et._HELD_BACK_CANDIDATE_RE.findall(notes)))
        self.assertEqual(1, len(et._HELD_BACK_RE.findall(notes)))

    def test_rewriting_never_stacks_a_third_number(self):
        notes = et.replace_held_back("Research note.", 7, pool=53, stories=2)
        notes = et.replace_held_back(notes, 7, pool=53, stories=2)
        self.assertEqual(1, len(et._HELD_BACK_CANDIDATE_RE.findall(notes)))
        self.assertEqual(1, len(et._HELD_BACK_RE.findall(notes)))

    def test_the_43246133293_numbers_now_balance(self):
        """The real measurements: 53 candidates, 7 held, 20 stories, 15 shipped."""
        pool, held, stories, shipped = 53, 7, 20, 15
        self.assertEqual(46, pool - held, "candidates considered")
        self.assertEqual(5, stories - shipped, "stories dropped after burning")
        record = vs.held_back_record({"held_back": {"count": held, "pool": pool}}, 0)
        self.assertEqual((held, pool), (record["count"], record["pool"]))
        sentence = et.held_back_note(record["count"], pool=record["pool"])
        self.assertIn("of the %d" % pool, sentence,
                      "the sentence does not state the population its number "
                      "was taken from")


if __name__ == "__main__":
    unittest.main()
