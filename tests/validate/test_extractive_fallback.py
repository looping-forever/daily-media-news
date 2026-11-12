#!/usr/bin/env python3
"""tests/validate/test_extractive_fallback.py — sentences, not a model, not a stub.

WHY THIS EXISTS. Across four delivered editions 107 stories reached the repair
stage and 32 left without a summary; two were repaired. Most of those had
publisher text in hand and no model to rewrite it. This file pins the fallback
that closes that gap WITHOUT inventing prose:

  * 1-2 sentences of the publisher's own text, copied verbatim, chosen by a
    fixed rule -- no request, no rewrite;
  * it is marked `summary_source: "extractive"`, counted apart from both
    AI-written and publisher-quoted cards, and named on the page;
  * it ships only when the SAME predicate the validator runs would accept it;
    otherwise the story keeps the old paths and is still dropped when the
    edition can hold its floor.

Offline: the summariser is injected, no provider is contacted.
"""

import unittest

from . import REPO_ROOT  # noqa: F401  (REPO_ROOT puts scripts/ on sys.path)

import derive_unsummarised
import edition_kind
import summarise_stubs
import validate_stories as vs

TITLE = "Highway closure announced for Friday night"
#: Two sentences of publisher text. Its FIRST sentence repeats part of the
#: headline, so only the sentence carrying times and a diversion should win.
SNIPPET = ("The East-West Highway closure was announced on Thursday. "
           "Buses will be diverted through Bardibas from 22:00 on Friday until "
           "Saturday morning, the department said.")
HEADLINE_ONLY = "Short line that is only the headline"
SHORT_SNIPPET = "Too short to say anything."


def story(url, summary, title=TITLE):
    return {"title": title, "summary": summary, "source_url": url,
            "category": "local_city"}


class ExtractionTests(unittest.TestCase):
    def test_the_extract_is_a_verbatim_substring_of_the_source(self):
        for source in (SNIPPET, SNIPPET + " " + SNIPPET, "  " + SNIPPET + "  "):
            text, why = derive_unsummarised.extractive_summary(
                source, TITLE, SNIPPET, "local_city")
            self.assertTrue(text, why)
            self.assertIn(text, source, "every word must be the publisher's")
            self.assertGreaterEqual(len(text),
                                    derive_unsummarised.EXTRACT_MIN_CHARS)
            self.assertLessEqual(len(text),
                                 derive_unsummarised.EXTRACT_MAX_CHARS)

    def test_a_headline_only_snippet_is_refused(self):
        text, why = derive_unsummarised.extractive_summary(
            TITLE, TITLE, TITLE, "local_city")
        self.assertEqual("", text)
        self.assertEqual("the text is the headline", why)

    def test_a_below_minimum_snippet_is_refused(self):
        text, why = derive_unsummarised.extractive_summary(
            SHORT_SNIPPET, HEADLINE_ONLY, SHORT_SNIPPET, "local_city")
        self.assertEqual("", text)
        self.assertIn("under %d" % derive_unsummarised.EXTRACT_MIN_CHARS, why)

    def test_the_snippet_copied_whole_is_refused_so_only_a_subset_ships(self):
        # One sentence IS the whole snippet: quoting it as an extract would be
        # the publisher's snippet copied verbatim -- the validator's own stub.
        single = ("Buses will be diverted through Bardibas from 22:00 on Friday "
                  "until Saturday morning, the department said.")
        text, why = derive_unsummarised.extractive_summary(
            single, TITLE, single, "local_city")
        self.assertEqual("", text)
        self.assertEqual("it is the publisher's snippet copied verbatim", why)


class RepairIntegrationTests(unittest.TestCase):
    def _repair(self, payload, snippets, summarise):
        return summarise_stubs.repair(payload, snippets, summarise=summarise,
                                      log=lambda *_a: None)

    def test_it_ships_when_no_model_answers_and_is_counted(self):
        payload = {"stories": [story("https://x/1", "Reported: " + TITLE)]}
        counts = self._repair(payload, {"https://x/1": SNIPPET},
                              lambda rows, **_kw: {"summaries": {}})
        row = payload["stories"][0]
        self.assertEqual("extractive", row["summary_source"])
        self.assertIn(row["summary"], SNIPPET)
        self.assertEqual(1, counts["extracted"])
        self.assertEqual(0, counts["publisher"],
                         "an extract is counted once, never also as quoted text")
        self.assertEqual(1, payload["summary_counts"]["extracted"])
        self.assertEqual(0, payload["summary_counts"]["summarised"])

    def test_the_extractive_path_costs_no_extra_request(self):
        payload = {"stories": [story("https://x/1", "Reported: " + TITLE)]}
        asked = []

        def summarise(rows, **_kw):
            asked.append(list(rows))
            return {"summaries": {}}

        self._repair(payload, {"https://x/1": SNIPPET}, summarise)
        self.assertEqual(1, len(asked), "one ladder walk, no call per story")
        self.assertIn(payload["stories"][0]["summary"], SNIPPET)

    def test_the_validator_accepts_what_it_ships_and_still_refuses_stubs(self):
        text, _why = derive_unsummarised.extractive_summary(
            SNIPPET, TITLE, SNIPPET, "local_city")
        self.assertTrue(vs.is_real_summary(text, TITLE, SNIPPET),
                        "an extract must pass the same predicate as prose")
        # The gate itself is not relaxed: a headline restated is still a stub,
        # and so is the snippet copied whole.
        self.assertFalse(vs.is_real_summary("Reported: " + TITLE, TITLE, SNIPPET))
        self.assertFalse(vs.is_real_summary(SNIPPET, TITLE, SNIPPET))

    def test_a_refused_extract_leaves_the_old_paths_untouched(self):
        # One sentence that IS the snippet: nothing to extract, so the whole
        # description is quoted as before and labelled publisher, not extracted.
        single = ("Buses will be diverted through Bardibas from 22:00 on Friday "
                  "until Saturday morning, the department said.")
        payload = {"stories": [story("https://x/1", "Reported: " + TITLE)]}
        counts = self._repair(payload, {"https://x/1": single},
                              lambda rows, **_kw: {"summaries": {}})
        self.assertEqual(0, counts["extracted"])
        self.assertEqual("publisher", payload["stories"][0]["summary_source"])
        self.assertEqual(single, payload["stories"][0]["summary"])


class NoteTests(unittest.TestCase):
    def test_the_note_names_how_many_were_extracted(self):
        note = edition_kind.model_note("partial", 3, 5, 2, 0, extracted=2)
        self.assertIn("3 of 5 stories", note)
        self.assertIn("2 of them are single sentences picked out of the "
                      "publisher's text.", note)

    def test_a_zero_count_leaves_the_sentence_exactly_as_it_was(self):
        self.assertEqual(edition_kind.model_note("partial", 3, 5, 2, 0),
                         edition_kind.model_note("partial", 3, 5, 2, 0,
                                                 extracted=0))
        self.assertEqual("", edition_kind.extracted_clause(0))
        self.assertEqual("", edition_kind.extracted_clause(None))


if __name__ == "__main__":
    unittest.main()
