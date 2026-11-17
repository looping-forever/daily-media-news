#!/usr/bin/env python3
"""tests/digest/test_walk_payload_budget.py — the walk's input fits the free tier.

WHY THIS FILE EXISTS
--------------------
Dry run 44031039679 measured the wall:

    Quota exceeded for metric:
    generativelanguage.googleapis.com/generate_content_free_tier_input_token_count,
    limit: 250000 ... input tokens per minute limit exceeded on Free Tier

The walk's first attempt on `gemini/gemini-3.1-flash-lite` wrote 2 stories; the
second attempt died on that ceiling. Nothing about output quality and nothing
about model choice: an agentic attempt re-sends its whole context on every turn,
so the size of `state/candidates.json` IS the input bill.

`scripts/trim_candidates.py` cuts the model-facing copy. These tests assert the
three properties that cut depends on, and they assert them NUMERICALLY with the
module's own estimators -- a prompt measured against the template instead of the
rendered text would understate the real payload, because `render_prompt.py`
resolves `{{TOKEN}}` placeholders before `opencode run` sees a single character.

Offline: stdlib only, no workflow, no provider.
"""

import copy
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import trim_candidates as tc  # noqa: E402

PROMPT = os.path.join(REPO_ROOT, "prompts", "daily-digest.md")

#: The nine domains the collector emits.
DOMAINS = ("entertainment", "local_city", "nepal", "education_tu",
           "college_local", "bank_local", "tech_career", "weather_forecast",
           "world")


def google_news_url(seed):
    """A URL with the REAL Google News redirect shape and length (~300 chars).

    Using a short `https://example.test/1` here would make the budget assertion
    pass on a payload that could never occur: measured, the URL is the single
    largest field in a real candidate (about 318 characters each).
    """
    body = ("CBMi%02dQGh0dHBzOi8vd3d3LmV4YW1wbGUuY29tL25ld3Mv"
            % (seed % 100)) * 6
    return ("https://news.google.com/rss/articles/%s?oc=5&hl=en-US&gl=US"
            "&ceid=US:en" % body)


def candidate(domain, index, snippet_chars=300):
    """One candidate shaped exactly as collect_news.make_candidate builds it."""
    return {
        "title": "%s: headline %d that runs to a realistic newspaper length"
                 % (domain.replace("_", " ").title(), index),
        "url": google_news_url(index),
        "source": "news.google.com",
        "published": "2026-11-17T0%d:%02d:00+00:00" % (index % 10, index % 60),
        "snippet": ("Sentence %d about the %s beat. " % (index, domain)
                    * (snippet_chars // 30 + 1))[:snippet_chars],
        "domain_category": domain,
        "language": "en",
        "image": {"url": "https://lh3.googleusercontent.com/proxy/%d" % index,
                  "width": 640, "height": 360, "type": "image/jpeg",
                  "source": "media:content"},
    }


def payload(candidates):
    return {
        "collected_at": "2026-11-17T00:00:00+00:00",
        "sources_ok": ["gnews:en", "gnews:np", "iost.tu.edu.np"],
        "sources_failed": [],
        # Never used by the prompt, and the biggest top-level block after the
        # candidates themselves.
        "source_results": [{"name": "plan-%d" % n, "kind": "gnews", "ok": True,
                            "count": 12, "error": "",
                            "url": "https://news.google.com/rss/search?q=x%d" % n}
                           for n in range(40)],
        "weather_summary": "Partly cloudy, 24C max, 18C min.",
        "weather_advice": [{"id": "rain", "text": "Carry an umbrella.",
                            "priority": 2}],
        "weather": {"current": {"temp_c": 24}},
        "candidates": candidates,
    }


def seventy_two_candidates():
    """72 candidates: 8 per domain across the nine the collector emits."""
    rows = []
    for domain in DOMAINS:
        for index in range(8):
            rows.append(candidate(domain, index))
    return payload(rows)


class ThePayloadFitsTheStatedBudget(unittest.TestCase):
    """(a) A 72-candidate input renders a prompt inside the stated budget."""

    def test_the_rendered_prompt_for_72_candidates_is_under_the_budget(self):
        full = seventy_two_candidates()
        self.assertEqual(72, len(full["candidates"]))
        trimmed = tc.trim_payload(full)

        stats = tc.measure(trimmed)

        self.assertLessEqual(
            stats["total_chars"], tc.MODEL_PAYLOAD_CHAR_BUDGET,
            "the model-facing payload for a 72-candidate input is %d characters "
            "against a stated budget of %d (%d estimated tokens). The free tier "
            "allows 250,000 INPUT tokens per minute and every turn re-sends this "
            "payload, so exceeding it is the measured quota wall."
            % (stats["total_chars"], tc.MODEL_PAYLOAD_CHAR_BUDGET,
               stats["total_est_tokens"]))
        self.assertTrue(
            stats["within_budget"], "measure() and the budget disagree")
        # The token statement, in the units the quota error speaks.
        self.assertLessEqual(
            stats["total_est_tokens"], 20000,
            "one message is ~%d estimated tokens; two attempts times six turns "
            "then exceeds the 250,000 per-minute ceiling"
            % stats["total_est_tokens"])

    def test_the_budget_is_measured_on_the_RENDERED_prompt_not_the_template(self):
        with open(PROMPT, "r", encoding="utf-8") as handle:
            template = handle.read()

        rendered = tc.rendered_prompt_chars(PROMPT)

        self.assertGreater(
            rendered, len(template),
            "the prompt was measured on the committed template, which still "
            "carries {{TOKEN}} placeholders: the model receives the RENDERED "
            "text and that is what costs input tokens")
        self.assertEqual(
            rendered,
            tc.measure(tc.trim_payload(seventy_two_candidates()),
                       PROMPT)["prompt_chars"])

    def test_the_estimate_is_the_modules_own_estimator(self):
        """One estimator, so a future change cannot silently double-count."""
        self.assertEqual(250, tc.estimate_tokens(1000))
        self.assertEqual(1, tc.estimate_tokens(1))
        self.assertEqual(0, tc.estimate_tokens(0))

    def test_an_uncapped_payload_is_over_the_budget(self):
        """The budget must BIND: this is what the cap is for.

        Without the assertion below, a payload that happened to be small would
        make every number above pass while nothing was actually being cut.
        """
        full = seventy_two_candidates()
        self.assertGreater(
            tc.payload_chars(full) + tc.rendered_prompt_chars(PROMPT),
            tc.MODEL_PAYLOAD_CHAR_BUDGET,
            "the untrimmed 72-candidate payload already fits, so these tests "
            "would pass without the trim doing anything")


class TruncationIsExplicit(unittest.TestCase):
    """(b) A cut field is marked, and never cut silently mid-word."""

    def test_a_long_snippet_is_cut_on_a_word_boundary_with_an_ellipsis(self):
        text = ("Alpha bravo charlie delta echo foxtrot golf hotel india juliet "
                "kilo lima mike november oscar papa quebec romeo sierra tango")
        clipped = tc.clip_text(text, 40)

        self.assertLessEqual(len(clipped), 41,
                             "the retained text must fit the cap; only the "
                             "ellipsis may exceed it")
        self.assertTrue(clipped.endswith(tc.ELLIPSIS),
                        "a truncated field carries no ellipsis, so the model "
                        "reads a cut sentence as the publisher's whole wording")
        kept = clipped[:-1]
        self.assertTrue(text.startswith(kept),
                        "the kept text is not a prefix of the original")
        self.assertEqual(kept, kept.rstrip(),
                         "the kept text ends on whitespace, which reads as a "
                         "missing word")
        # THE WORD-BOUNDARY ASSERTION. The character after the kept text must be
        # a space in the original: a mid-word cut leaves a letter there.
        self.assertEqual(
            " ", text[len(kept):len(kept) + 1],
            "the snippet was cut mid-word (%r -> %r)"
            % (text[:len(kept) + 8], kept[-16:]))

    def test_the_trimmed_candidate_carries_the_marked_snippet(self):
        cand = candidate("nepal", 1, snippet_chars=300)
        row = tc.trim_candidate(cand)

        self.assertLessEqual(len(row["snippet"]), tc.MODEL_SNIPPET_MAX_CHARS + 1)
        self.assertTrue(row["snippet"].endswith(tc.ELLIPSIS),
                        "a 300-character snippet survived the %d-character cap "
                        "unmarked" % tc.MODEL_SNIPPET_MAX_CHARS)
        self.assertTrue(cand["snippet"].startswith(row["snippet"][:-1]))

    def test_a_short_snippet_is_not_touched(self):
        cand = candidate("nepal", 2)
        cand["snippet"] = "A short summary that fits."
        row = tc.trim_candidate(cand)
        self.assertEqual("A short summary that fits.", row["snippet"],
                         "a snippet under the cap was still marked as truncated")


class TheCapKeepsDomainDiversity(unittest.TestCase):
    """(c) The cap is heterogeneous, not "the first N rows".

    A fixture of 60 tech rows and 12 mixed rows must not come back as 30 tech
    rows and nothing else: the digest exists to report across domains, and a
    first-N cap would hand the model one loud domain and hide the quiet ones.
    """

    def mixed_fixture(self):
        rows = [candidate("tech_career", i) for i in range(60)]
        rows += [candidate("nepal", i) for i in range(4)]
        rows += [candidate("local_city", i) for i in range(4)]
        rows += [candidate("education_tu", i) for i in range(4)]
        return payload(rows)

    def test_twelve_mixed_rows_are_not_swallowed_by_sixty_tech_rows(self):
        full = self.mixed_fixture()
        kept = tc.select_candidates(full["candidates"], 30)

        counts = {}
        for cand in kept:
            counts[cand["domain_category"]] = counts.get(
                cand["domain_category"], 0) + 1

        self.assertEqual(30, len(kept))
        self.assertGreater(
            counts.get("tech_career", 0), 0,
            "the loud domain lost every seat")
        self.assertGreater(
            sum(n for cat, n in counts.items() if cat != "tech_career"), 0,
            "the cap returned 30 tech rows and 0 others, which is the failure "
            "this fixture exists to catch: %r" % (counts,))
        for cat in ("nepal", "local_city", "education_tu"):
            self.assertGreaterEqual(
                counts.get(cat, 0), 1,
                "every domain that had a candidate must keep a seat before "
                "tech_career gets a second one: %r" % (counts,))

    def test_the_freshest_rows_win_inside_a_domain(self):
        rows = [candidate("nepal", i) for i in range(10)]
        for index, cand in enumerate(rows):
            cand["published"] = "2026-11-1%dT00:00:00+00:00" % index
        kept = tc.select_candidates(rows, 3)
        self.assertEqual(
            {"2026-11-17T00:00:00+00:00", "2026-11-18T00:00:00+00:00",
             "2026-11-19T00:00:00+00:00"},
            {cand["published"] for cand in kept},
            "the cap kept older rows than the ones available")
        # The output keeps the collector's own order, so the model still sees
        # the ranking it arrived with rather than a freshness-sorted list.
        self.assertEqual(
            [c["url"] for c in rows[7:]], [c["url"] for c in kept],
            "the kept rows were reordered; only the SET may change")

    def test_the_selection_is_deterministic(self):
        full = self.mixed_fixture()
        first = tc.trim_payload(copy.deepcopy(full))
        second = tc.trim_payload(copy.deepcopy(full))
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True),
                         "two runs over the same input produced different "
                         "payloads, so a walk cannot be reproduced")

    def test_a_payload_under_the_cap_is_passed_through_in_collector_order(self):
        rows = [candidate("nepal", i) for i in range(5)]
        kept = tc.select_candidates(rows, 30)
        self.assertEqual([c["url"] for c in rows], [c["url"] for c in kept])


class TheFullCollectionSurvives(unittest.TestCase):
    """(d) Only the model's copy shrinks; `state/candidates.json` keeps all of it.

    Validation, media passthrough, the derive fallback and the audit upload all
    read the collected file. A trim that rewrote it would make the ship gate
    judge a subset -- which is how stories get silently dropped.
    """

    def test_trimming_does_not_mutate_the_input_payload(self):
        full = seventy_two_candidates()
        before = copy.deepcopy(full)

        tc.trim_payload(full)

        self.assertEqual(before, full,
                         "trim_payload mutated the payload it was handed")

    def test_the_cli_leaves_the_input_file_holding_every_candidate(self):
        full = seventy_two_candidates()
        with tempfile.TemporaryDirectory(prefix="trim-budget-") as tmp:
            in_path = os.path.join(tmp, "candidates.json")
            out_path = os.path.join(tmp, "candidates_model.json")
            with open(in_path, "w", encoding="utf-8") as handle:
                json.dump(full, handle, ensure_ascii=False, indent=2)
            with open(in_path, "rb") as handle:
                before = handle.read()

            self.assertEqual(0, tc.main(["--in", in_path, "--out", out_path]))

            with open(in_path, "rb") as handle:
                self.assertEqual(
                    before, handle.read(),
                    "the collector's own file was rewritten by the trim")
            with open(in_path, "r", encoding="utf-8") as handle:
                self.assertEqual(72, len(json.load(handle)["candidates"]))
            with open(out_path, "r", encoding="utf-8") as handle:
                model = json.load(handle)
            self.assertEqual(72, model["candidates_total"])
            self.assertLessEqual(len(model["candidates"]), tc.MODEL_MAX_CANDIDATES)


class FieldsThePromptNeverUsesAreDropped(unittest.TestCase):
    def test_the_trim_keeps_what_the_prompt_reads_and_drops_the_rest(self):
        trimmed = tc.trim_payload(payload([candidate("nepal", 1)]))

        for key in ("collected_at", "sources_ok", "sources_failed",
                    "weather_summary", "candidates"):
            self.assertIn(key, trimmed,
                          "the prompt reads %r and the trim removed it" % key)
        for key in ("source_results", "weather", "weather_advice"):
            self.assertNotIn(
                key, trimmed,
                "%r is not in the prompt's declared shape, so carrying it is "
                "paid for on every turn of the walk" % key)

    def test_media_is_carried_verbatim(self):
        cand = candidate("entertainment", 1)
        row = tc.trim_candidate(cand)
        self.assertEqual(cand["image"], row["image"],
                         "media is a passthrough field: altering it would make "
                         "the validator drop the story's image")

    def test_internal_collector_keys_are_dropped(self):
        cand = candidate("nepal", 3)
        cand["_dt"] = "internal"
        cand["_kind"] = "gnews"
        row = tc.trim_candidate(cand)
        self.assertNotIn("_dt", row)
        self.assertNotIn("_kind", row)


class AMeasurementFailureNeverDiscardsThePayload(unittest.TestCase):
    """A printed number is not worth losing the payload over.

    The workflow restores the FULL candidate set when trim_candidates.py exits
    non-zero, so a crash AFTER the write replaces a good trimmed file with the
    very payload that hits the wall. Found by running the shipped workflow block
    against a tree where the prompt sat somewhere else.
    """

    def test_a_missing_prompt_counts_as_zero_instead_of_raising(self):
        with tempfile.TemporaryDirectory(prefix="trim-noprompt-") as tmp:
            self.assertEqual(
                0, tc.rendered_prompt_chars(os.path.join(tmp, "absent.md")),
                "a measurement aid that is not on disk must not abort the trim")

    def test_a_broken_measurement_still_writes_the_trimmed_payload(self):
        full = seventy_two_candidates()
        with tempfile.TemporaryDirectory(prefix="trim-measure-") as tmp:
            in_path = os.path.join(tmp, "candidates.json")
            out_path = os.path.join(tmp, "candidates_model.json")
            with open(in_path, "w", encoding="utf-8") as handle:
                json.dump(full, handle, ensure_ascii=False, indent=2)

            original = tc.measure

            def exploding_measure(*args, **kwargs):
                raise RuntimeError("estimator unavailable")

            tc.measure = exploding_measure
            try:
                rc = tc.main(["--in", in_path, "--out", out_path])
            finally:
                tc.measure = original

            self.assertEqual(
                0, rc,
                "a non-zero exit makes the workflow copy the full set over the "
                "trim, which is the wall this module exists to remove")
            with open(out_path, "r", encoding="utf-8") as handle:
                model = json.load(handle)
            self.assertEqual(72, model["candidates_total"])
            self.assertLessEqual(len(model["candidates"]), tc.MODEL_MAX_CANDIDATES)


if __name__ == "__main__":
    unittest.main()
