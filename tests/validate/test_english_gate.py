#!/usr/bin/env python3
"""tests/validate/test_english_gate.py — the English-only payload gate.

Run:
    python -m unittest discover -s tests/validate -t . -v

WHY THIS EXISTS
---------------
docs/digests/2026-11-03-morning.html shipped a Devanagari headline that the page
could not draw: the browser rendered an empty box where the text should be,
because the page loads no Devanagari face. The text came from the collector
verbatim (the no-AI fallback), so no prompt could have prevented it.

The rule pinned here is the boundary rule:

  * a story whose TITLE is not English is DROPPED, because a headline the page
    cannot draw is not a headline;
  * a story whose SUMMARY is not English keeps its English title and has the
    summary CLEARED, because the text is the defect, not the story;
  * the no-AI deriver counts what it held back, and the payload says the number
    in its notes instead of becoming a silent quiet day.

The real failing headline is read out of the shipped page, not hand-copied, so
the exact input that broke the page is what the test measures.
"""

import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from . import REPO_ROOT  # noqa: E402  (SCRIPTS is put on sys.path there)

import derive_unsummarised as dut  # noqa: E402
import english_text as et  # noqa: E402
import validate_stories as vs  # noqa: E402

PAGE = os.path.join(REPO_ROOT, "docs", "digests", "2026-11-03-morning.html")
DEVANAGARI = "\u092a\u0930\u094d\u092f\u091f\u0928\u092e\u0928\u094d\u0924\u094d"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def story(title, summary=""):
    return {
        "title": title,
        "summary": summary,
        "source_url": "https://example.com/story",
        "category": "nepal",
        "priority": 5,
        "reliability": "tier3",
        "spoiler_level": "none",
    }


def cand(title, url, category="nepal", snippet=""):
    return {"title": title, "url": url, "source": "feed",
            "domain_category": category, "language": "en",
            "snippet": snippet,
            "published": "2026-11-03T09:00:00+00:00"}


class TheGate(unittest.TestCase):
    def test_devanagari_is_not_english(self):
        self.assertFalse(et.looks_english(DEVANAGARI))
        self.assertEqual("devanagari", et.non_english_script(DEVANAGARI))

    def test_the_real_shipped_headline_is_refused(self):
        """The artifact that broke the page is the fixture, not a guess."""
        with open(PAGE, encoding="utf-8") as fh:
            html = fh.read()
        titles = re.findall(r'<h3 class="card-title">(.*?)</h3>', html, re.S)
        offending = [t for t in titles if not et.looks_english(t)]
        self.assertTrue(offending,
                        "the shipped page no longer contains the Devanagari "
                        "headline this gate exists for; if the page was "
                        "regenerated, pick another artifact that proves the bug")
        for title in offending:
            self.assertFalse(et.looks_english(title), title)

    def test_english_and_accented_latin_pass(self):
        for text in ("Nepal budget passes after a long debate",
                     "Caf\u00e9 owners in Kathmandu protest the new tax",
                     "Roll 123 found in the CSIT semester 3 result notice"):
            self.assertTrue(et.looks_english(text), text)

    def test_emoji_are_not_a_script(self):
        self.assertTrue(et.looks_english("\U0001F393 CSIT Sem 3 Result: PASSED"),
                        "a push title with an emoji must still render")

    def test_other_non_latin_scripts_fail(self):
        for text, name in (("119 dead in \u4e2d\u56fd", "cjk"),
                           ("\u0645\u0648\u0636\u0648\u0639", "arabic"),
                           ("\u05e0\u05d5\u05e9\u05d0", "hebrew")):
            self.assertFalse(et.looks_english(text), text)
            self.assertEqual(name, et.non_english_script(text))

    def test_held_back_note_counts(self):
        self.assertEqual(
            "1 story was held back because it is not in English.",
            et.held_back_note(1))
        self.assertEqual(
            "2 stories were held back because they are not in English.",
            et.held_back_note(2))

    def test_the_count_can_be_read_back_out_of_the_note(self):
        """The page states the number the notes carry, so the pair must round-trip."""
        for n in (1, 2, 7, 31):
            notes = "Research note. " + et.held_back_note(n)
            self.assertEqual(n, et.held_back_from_notes(notes), notes)

    def test_a_note_that_says_nothing_reports_zero(self):
        for notes in ("", None, "no AI was available this run",
                      "1 story was dropped for another reason."):
            self.assertEqual(0, et.held_back_from_notes(notes), notes)


class DevanagariIsStillDefinedOnce(unittest.TestCase):
    """Three files recognise Devanagari for three different jobs.

    `english_text` (renderability), `devanagari` (the academic parser) and
    `live_canary` (the A4 canary) each carry their own range on purpose -- merging
    them would couple a parser to the page's font stack. The ranges must still
    agree, and this test is what makes drift a failure instead of a surprise.
    """

    def ours(self):
        found = {lo: hi for name, lo, hi in et.NON_LATIN_RANGES
                 if name == "devanagari"}
        self.assertEqual(1, len(found), "one devanagari row, not two: %r" % found)
        lo, hi = list(found.items())[0]
        return set(range(ord(lo), ord(hi) + 1))

    def test_the_parser_agrees(self):
        import devanagari
        theirs = {cp for cp in range(0x10000)
                  if devanagari.DEVANAGARI_RE.match(chr(cp))}
        self.assertEqual(self.ours(), theirs,
                         "scripts/devanagari.py and english_text disagree about "
                         "which code points are Devanagari")

    def test_the_canary_agrees(self):
        import live_canary
        theirs = {cp for cp in range(0x10000)
                  if live_canary._is_devanagari(chr(cp))}
        self.assertEqual(self.ours(), theirs,
                         "scripts/live_canary.py and english_text disagree about "
                         "which code points are Devanagari")


class ValidatorDropsTheTitleAndClearsTheSummary(unittest.TestCase):
    def test_a_non_english_title_is_dropped(self):
        ok, reason = vs.validate_story(story(DEVANAGARI), 0)
        self.assertFalse(ok, "a headline the page cannot draw must not survive")
        self.assertTrue(reason.startswith("non_english_title"), reason)

    def test_a_non_english_summary_is_cleared_and_the_story_survives(self):
        s = story("Nepal budget passes after a long debate", DEVANAGARI)
        ok, reason = vs.validate_story(s, 0)
        self.assertTrue(ok, reason)
        self.assertEqual("", s["summary"],
                         "the summary is the defect, not the headline")

    def test_an_english_story_is_untouched(self):
        s = story("Nepal budget passes after a long debate",
                  "The finance ministry published the details today.")
        ok, reason = vs.validate_story(s, 0)
        self.assertTrue(ok, reason)
        self.assertEqual("The finance ministry published the details today.",
                         s["summary"])


class DeriverHoldsBackNonEnglish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="english_gate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_derive_counts_what_it_refused(self):
        held = {}
        stories, _counts, _skipped = dut.derive([
            cand("Nepal government announces the annual budget for next year",
                 "https://a.example/1"),
            cand(DEVANAGARI + " announces a new plan for the valley today",
                 "https://a.example/2"),
        ], now=NOW, held=held)
        self.assertEqual(["https://a.example/1"],
                         [s["source_url"] for s in stories])
        self.assertEqual(1, held.get("non_english"),
                         "the refusal must be counted, not swallowed")

    def test_a_non_english_description_keeps_the_bare_english_headline(self):
        held = {}
        stories, _c, _s = dut.derive([
            cand("Nepal government announces the annual budget for next year",
                 "https://a.example/1", snippet=DEVANAGARI * 3),
        ], now=NOW, held=held)
        self.assertEqual(1, len(stories))
        self.assertEqual("", stories[0]["summary"])
        self.assertEqual(0, held.get("non_english", 0),
                         "only a refused TITLE counts as a held-back story")

    def run_main(self, candidates):
        path = os.path.join(self.tmp, "candidates.json")
        out = os.path.join(self.tmp, "stories.json")
        log = os.path.join(self.tmp, "agent-attempts.log")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"candidates": candidates}, fh)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("")
        rc = dut.main(["--candidates", path, "--out", out,
                       "--today", "2026-11-03", "--slot", "morning",
                       "--attempts", log])
        return rc, out

    def test_payload_notes_say_how_many_were_held_back(self):
        english = [
            "Nepal government announces the annual budget for next year",
            "Kathmandu valley reports steady rain through the whole afternoon",
            "University exam schedule published for the autumn semester students",
            "Local bank raises interest rates on fixed deposits from next month",
            "New anime season confirmed by the studio with a teaser for fans",
        ]
        candidates = [cand(t, "https://a.example/%d" % n,
                           category=("world_major" if n < 3 else "local_city"))
                      for n, t in enumerate(english)]
        candidates.append(cand(DEVANAGARI + " announces a new plan today",
                               "https://a.example/x"))
        rc, out = self.run_main(candidates)
        self.assertEqual(0, rc)
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(5, len(payload["stories"]))
        self.assertIn("no AI model was configured or reachable", payload["notes"])
        self.assertIn("1 story was held back because it is not in English.",
                      payload["notes"])

    def test_below_the_floor_still_writes_nothing(self):
        english = [
            "Nepal government announces the annual budget for next year",
            "Kathmandu valley reports steady rain through the whole afternoon",
            "University exam schedule published for the autumn semester students",
            "Local bank raises interest rates on fixed deposits from next month",
        ]
        candidates = [cand(t, "https://a.example/%d" % n,
                           category=("world_major" if n < 2 else "local_city"))
                      for n, t in enumerate(english)]
        candidates.append(cand(DEVANAGARI + " announces a new plan today",
                               "https://a.example/x"))
        rc, out = self.run_main(candidates)
        self.assertEqual(0, rc, "the floor is a refusal, not an error")
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
