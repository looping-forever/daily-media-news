#!/usr/bin/env python3
"""tests/validate/test_derive_unsummarised.py — the no-AI fallback edition.

Run:
    python -m unittest discover -s tests/validate -t . -v

WHAT IS PINNED HERE
-------------------
The edition a failed model run falls back to is derived from the collector's own
candidates, so every rule below is a rule about what the owner is allowed to see
when no model wrote anything:

  * the collector's freshest-first order is preserved and capped at 3 per
    `domain_category` -- the fallback must not become a 15-story one-category run;
  * `priority` never exceeds 7, because daily-news.yml escalates at `>= 8`;
  * titles under 30 characters, empty categories and near-duplicates
    (Jaccard >= 0.5) are dropped;
  * fewer than 5 stories writes NOTHING, so the existing empty/degraded path
    still owns that case;
  * the written payload carries `edition_kind: unsummarised`, a note that says
    no AI was available, the PUBLISHER'S OWN description as `summary` when the
    feed carried one (CHANGED 2026-10-30: a bare title was the floor, a title
    with a lead-in is the floor that can be read), and no `for_you`.

The derived story is also fed through `validate_stories.validate_story`, because
the agreed design keeps the validator unchanged: a story the validator drops is
a story this fallback cannot deliver.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from . import REPO_ROOT, SCRIPTS  # noqa: F401  (SCRIPTS is put on sys.path there)

import derive_unsummarised as dut  # noqa: E402
import validate_stories as vs  # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
FRESH = "2026-10-28T09:00:00+00:00"
OLD = "2026-10-14T09:00:00+00:00"


def cand(title, url, category="world_major", published=OLD, **extra):
    item = {"title": title, "url": url, "source": "feed",
            "domain_category": category, "language": "en",
            "published": published}
    item.update(extra)
    return item


def long_title(seed):
    return "A long enough headline about %s and its consequences today" % seed


# Distinct wording on purpose: near-identical titles ARE duplicates below, so a
# fixture built from one sentence would test the dedupe instead of the cap.
DISTINCT = (
    "Nepal government announces the annual budget for the coming fiscal year",
    "Kathmandu valley reports steady rain through the whole afternoon today",
    "University exam schedule published for the autumn semester students",
    "Local bank raises interest rates on fixed deposits from next month",
    "New anime season confirmed by the studio with a teaser for fans",
)


class DeriveOrderAndCaps(unittest.TestCase):
    def test_first_three_per_category_in_collector_order(self):
        candidates = [cand(DISTINCT[n], "https://a.example/n%d" % n,
                           category="world_major") for n in range(5)]
        candidates.append(cand("Kathmandu council opens a new pedestrian zone",
                               "https://b.example/l1", category="local_city"))
        stories, counts, _ = dut.derive(candidates, now=NOW)
        world = [s for s in stories if s["category"] == "world_major"]
        self.assertEqual(len(world), 3, "the per-category cap is 3, not 5")
        self.assertEqual([s["source_url"] for s in world],
                         ["https://a.example/n0", "https://a.example/n1",
                          "https://a.example/n2"],
                         "the collector's freshest-first order must be preserved")
        self.assertEqual(counts["local_city"], 1)

    def test_priority_never_above_seven(self):
        tier1 = cand(DISTINCT[0], "https://tu.edu.np/n1", published=FRESH)
        tier1_old = cand(DISTINCT[1], "https://tu.edu.np/n2",
                         category="education_tu", published=OLD)
        tier3 = cand(DISTINCT[2], "https://example.org/n1",
                     category="tech_career", published=FRESH)
        tier3_old = cand(DISTINCT[3], "https://example.org/n2",
                         category="bank_local", published=OLD)
        stories, _, _ = dut.derive([tier1, tier1_old, tier3, tier3_old], now=NOW)
        by_title = {s["title"]: s for s in stories}
        self.assertEqual(by_title[tier1["title"]]["priority"], 7,
                         "6 + 1 fresh must clamp to 7")
        self.assertEqual(by_title[tier1["title"]]["reliability"], "tier2")
        self.assertEqual(by_title[tier1_old["title"]]["priority"], 6)
        self.assertEqual(by_title[tier3["title"]]["priority"], 6)
        self.assertEqual(by_title[tier3_old["title"]]["priority"], 5)
        self.assertEqual(by_title[tier3["title"]]["reliability"], "tier3")
        for story in stories:
            self.assertLessEqual(story["priority"], 7,
                                 "priority >= 8 escalates the push")

    def test_drops_short_titles_and_empty_categories(self):
        stories, _, _ = dut.derive([
            cand("Too short", "https://a.example/short"),
            cand(long_title("no category"), "https://a.example/nocat",
                 category=""),
            cand(long_title("good one"), "https://a.example/good"),
        ], now=NOW)
        self.assertEqual([s["source_url"] for s in stories],
                         ["https://a.example/good"])

    def test_drops_a_category_the_digest_schema_does_not_accept(self):
        """`weather_forecast` IS IN state/candidates.json ON EVERY RUN.

        The collector emits it, and the digest schema has no such category, so
        validate_stories drops the story with "schema: invalid category". A
        deriver that emitted it anyway would count a story that is certain to be
        thrown away -- the floor could pass here while the page shipped one
        fewer story than this file claimed.
        """
        stories, counts, skipped = dut.derive([
            cand(long_title("weather"), "https://open-meteo.com/w",
                 category="weather_forecast"),
            cand(long_title("real news"), "https://a.example/ok",
                 category="nepal"),
        ], now=NOW)
        self.assertEqual([s["source_url"] for s in stories],
                         ["https://a.example/ok"])
        self.assertNotIn("weather_forecast", counts)
        self.assertEqual(skipped, {"weather_forecast": 1},
                         "the drop must be counted, not swallowed: it is the "
                         "evidence that a source cost a candidate for nothing")

    def test_the_category_vocabulary_is_the_validators_own_object(self):
        """ONE VOCABULARY, IMPORTED. A copy would drift the day either changes."""
        self.assertIs(dut.VALID_CATEGORIES, vs.VALID_CATEGORIES,
                      "derive_unsummarised no longer imports the validator's "
                      "category set, so the two can disagree")

    def test_dedupes_near_duplicate_titles(self):
        first = cand("Nepal government announces the annual budget for next year",
                     "https://a.example/1", category="nepal")
        near = cand("Nepal government announces annual budget for the next year",
                    "https://b.example/2", category="nepal")
        other = cand("Kathmandu valley reports steady rain through the afternoon",
                     "https://c.example/3", category="local_city")
        stories, _, _ = dut.derive([first, near, other], now=NOW)
        self.assertEqual(len(stories), 2, "Jaccard >= 0.5 titles are duplicates")
        self.assertNotIn("https://b.example/2", [s["source_url"] for s in stories])

    def test_passes_through_language_and_media(self):
        image = {"url": "https://a.example/i.jpg", "type": "image/jpeg"}
        video = {"url": "https://a.example/v.mp4", "type": "video/mp4"}
        stories, _, _ = dut.derive([
            cand(long_title("media item"), "https://a.example/m", language="ne",
                 image=image, video=video),
        ], now=NOW)
        self.assertEqual(stories[0]["language"], "ne")
        self.assertEqual(stories[0]["image"], image)
        self.assertEqual(stories[0]["video"], video)


class DerivedStorySurvivesTheValidator(unittest.TestCase):
    def test_story_shape_passes_validate_story(self):
        stories, _, _ = dut.derive([
            cand(long_title("validator shape"), "https://kathmandupost.com/a",
                 category="nepal", published=FRESH),
        ], now=NOW)
        story = stories[0]
        self.assertEqual(story["summary"], "")
        self.assertNotIn("for_you", story)
        self.assertEqual(story["supporting_sources"], [])
        self.assertIsNone(story["personal_relevance"])
        self.assertEqual(story["source_name"], "kathmandupost.com")
        ok, reason = vs.validate_story(story, 0)
        self.assertTrue(ok, "the unchanged validator must accept it: %s" % reason)


class TheFloorCarriesText(unittest.TestCase):
    """C8: an all-models-fail day must ship more than a headline.

    The text is QUOTED, never written: it is the feed's own description, so the
    edition is still honestly labelled unsummarised. Every rule below returns ""
    (the old bare-headline behaviour) rather than a weaker or invented string.
    """

    DESCRIPTION = ("The ministry said the new schedule starts next month and "
                   "covers every district office in the valley.")

    def test_a_story_with_a_description_ships_it(self):
        stories, _, _ = dut.derive([
            cand(long_title("described"), "https://kathmandupost.com/d",
                 category="nepal", published=FRESH,
                 snippet=self.DESCRIPTION),
        ], now=NOW)
        story = stories[0]
        self.assertEqual(self.DESCRIPTION, story["summary"],
                         "the publisher's own description must be used verbatim")
        ok, reason = vs.validate_story(story, 0)
        self.assertTrue(ok, "the validator must still accept it: %s" % reason)

    def test_a_description_shorter_than_the_floor_is_dropped(self):
        stories, _, _ = dut.derive([
            cand(long_title("stub"), "https://kathmandupost.com/s",
                 category="nepal", snippet="Read more."),
        ], now=NOW)
        self.assertEqual("", stories[0]["summary"])

    def test_a_description_that_repeats_the_headline_is_dropped(self):
        title = long_title("repeat")
        stories, _, _ = dut.derive([
            cand(title, "https://kathmandupost.com/r", category="nepal",
                 snippet=title),
        ], now=NOW)
        self.assertEqual("", stories[0]["summary"],
                         "a repeat of the headline adds nothing to a headline")

    def test_a_long_description_is_cut_on_a_word_boundary(self):
        stories, _, _ = dut.derive([
            cand(long_title("long"), "https://kathmandupost.com/l",
                 category="nepal", snippet="word " * 200),
        ], now=NOW)
        summary = stories[0]["summary"]
        self.assertTrue(summary.endswith("\u2026"), summary)
        self.assertLessEqual(len(summary), dut.SUMMARY_MAX_CHARS + 1)
        self.assertNotIn("wor\u2026", summary, "cut mid-word: %r" % summary[-20:])

    def test_a_description_the_validator_would_veto_keeps_the_bare_headline(self):
        """Quoting adds words to the gates the validator re-runs. Dropping the
        whole story would shrink an edition that has no model behind it, so the
        TEXT goes and the story stays."""
        if not dut.EXCLUDED_KEYWORDS:
            self.skipTest("no excluded keyword configured to test with")
        keyword = dut.EXCLUDED_KEYWORDS[0]
        stories, _, _ = dut.derive([
            cand(long_title("veto"), "https://kathmandupost.com/v",
                 category="nepal",
                 snippet="A report mentioning %s in passing, at length."
                         % keyword.upper()),
        ], now=NOW)
        self.assertEqual("", stories[0]["summary"])
        self.assertEqual(1, len(stories), "the story itself must survive")


class FloorAndPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="derive_floor_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_main(self, candidates, attempts=""):
        path = os.path.join(self.tmp, "candidates.json")
        out = os.path.join(self.tmp, "stories.json")
        log = os.path.join(self.tmp, "agent-attempts.log")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"collected_at": "2026-10-28 09:00", "candidates": candidates}, fh)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(attempts)
        rc = dut.main(["--candidates", path, "--out", out, "--today", "2026-10-28",
                       "--slot", "morning", "--attempts", log])
        return rc, out

    def test_fewer_than_five_stories_writes_nothing(self):
        candidates = [cand(DISTINCT[n], "https://a.example/%d" % n,
                           category=("world_major" if n < 2 else "local_city"))
                      for n in range(4)]
        rc, out = self.run_main(candidates)
        self.assertEqual(rc, 0, "the floor is not an error, it is a refusal")
        self.assertFalse(os.path.exists(out),
                         "below the floor the empty/degraded path must own the run")

    def test_payload_carries_edition_kind_and_note(self):
        candidates = [cand(DISTINCT[n], "https://a.example/%d" % n,
                           category=("world_major" if n < 3 else "local_city"))
                      for n in range(5)]
        rc, out = self.run_main(candidates)
        self.assertEqual(rc, 0)
        with open(out, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["edition_kind"], "unsummarised")
        # With no attempt log at all, the note must say nothing was configured
        # rather than claiming a quota was spent.
        self.assertIn("no AI model was configured or reachable", payload["notes"])
        self.assertNotIn("no AI was available", payload["notes"])
        self.assertEqual(payload["date"], "2026-10-28")
        self.assertEqual(payload["digest_time"], "morning")
        self.assertEqual(len(payload["stories"]), 5)
        for story in payload["stories"]:
            self.assertEqual(story["summary"], "")
            self.assertNotIn("for_you", story)

    def test_payload_names_the_deriver_as_the_writer(self):
        """No model wrote this edition, and the artifact must be able to say so."""
        candidates = [cand(DISTINCT[n], "https://a.example/%d" % n,
                           category=("world_major" if n < 3 else "local_city"))
                      for n in range(5)]
        rc, out = self.run_main(candidates)
        self.assertEqual(rc, 0)
        with open(out, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual({"rung": "derived", "attempt": None, "model": None},
                         payload.get("writer"),
                         "the derived edition does not name its writer, so the "
                         "seal cannot mark it degraded")

    def test_missing_candidates_file_is_not_a_silent_success(self):
        rc = dut.main(["--candidates", os.path.join(self.tmp, "absent.json"),
                       "--out", os.path.join(self.tmp, "stories.json")])
        self.assertEqual(rc, 1)


class NoAIReason(unittest.TestCase):
    """The note must name WHY no model wrote the edition.

    2026-11-05 shipped with "no AI was available" while the keyed Gemini rungs
    were configured and had answered a 250,000 input-tokens/min free-tier
    refusal: true, and useless to the reader.
    """

    QUOTA_LOG = (
        "2026-11-05T00:56:20Z attempt=1 model=gemini/gemini-3.5-flash-lite "
        "kind=quota_or_rate_limited rc=1 allow=0\n"
        "2026-11-05T00:56:54Z attempt=2 model=gemini/gemini-3.1-flash-lite "
        "kind=quota_or_rate_limited rc=1 allow=0\n"
        "2026-11-05T00:57:30Z attempt=3 model=opencode/nemotron-3-ultra-free "
        "kind=payment_required rc=1 allow=0\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="derive_reason_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_an_empty_log_is_not_a_quota_wall(self):
        for text in ("", None, "\n"):
            self.assertEqual(dut.REASON_NOT_CONFIGURED,
                             dut.reason_from_attempts(text),
                             "%r must not claim a spent allowance" % (text,))

    def test_every_quota_row_means_the_allowance_is_spent(self):
        self.assertEqual(dut.REASON_QUOTA, dut.reason_from_attempts(self.QUOTA_LOG))

    def test_a_row_that_failed_for_its_own_reason_is_not_a_quota_wall(self):
        log = self.QUOTA_LOG + (
            "2026-11-05T00:58:10Z attempt=4 model=opencode/big-pickle "
            "kind=auth rc=1 allow=0\n")
        self.assertEqual(dut.REASON_ALL_FAILED, dut.reason_from_attempts(log))

    def test_the_quota_note_names_the_providers_once_each(self):
        note = dut.note_for(dut.REASON_QUOTA, self.QUOTA_LOG)
        self.assertIn("(gemini, opencode)", note)
        self.assertNotIn("gemini, gemini", note)
        self.assertIn("publisher's own description", note)

    def test_the_not_configured_note_does_not_mention_a_quota(self):
        note = dut.note_for(dut.REASON_NOT_CONFIGURED, "")
        self.assertIn("no AI model was configured or reachable", note)
        self.assertNotIn("quota", note)

    def run_main(self, attempts):
        path = os.path.join(self.tmp, "candidates.json")
        out = os.path.join(self.tmp, "stories.json")
        log = os.path.join(self.tmp, "agent-attempts.log")
        candidates = [cand(DISTINCT[n], "https://a.example/%d" % n,
                           category=("world_major" if n < 3 else "local_city"))
                      for n in range(5)]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"candidates": candidates}, fh)
        if attempts is not None:
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(attempts)
        return dut.main(["--candidates", path, "--out", out,
                         "--today", "2026-11-05", "--slot", "morning",
                         "--attempts", log]), out

    def test_the_payload_note_names_the_quota_and_the_providers(self):
        rc, out = self.run_main(self.QUOTA_LOG)
        self.assertEqual(0, rc)
        with open(out, encoding="utf-8") as fh:
            notes = json.load(fh)["notes"]
        self.assertIn("refused on its provider's quota (gemini, opencode)", notes)
        self.assertNotIn("no AI was available", notes)

    def test_a_missing_attempt_log_still_writes_a_note(self):
        rc, out = self.run_main(None)
        self.assertEqual(0, rc)
        with open(out, encoding="utf-8") as fh:
            notes = json.load(fh)["notes"]
        self.assertIn("no AI model was configured or reachable", notes)


class SummarisedFallback(unittest.TestCase):
    """The fallback with a model behind it: counted, labelled, nothing invented.

    2026-11-05 shipped fifteen headlines over the publisher's own descriptions
    because the agentic walk spent its per-minute allowance in thirteen seconds.
    A batch of summaries fits that allowance, so the edition can carry real
    sentences -- and the moment it does, two claims on the page have to change:
    what the note says, and what the edition line counts.
    """

    TEXT = ("The ministry said the new schedule starts next month and covers "
            "every district office in the valley.")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="derive_summarised_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def candidates(self):
        return [cand(DISTINCT[n], "https://a.example/%d" % n,
                     category=("world_major" if n < 3 else "local_city"))
                for n in range(5)]

    def payload_of(self, summarise):
        path = os.path.join(self.tmp, "candidates.json")
        out = os.path.join(self.tmp, "stories.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"candidates": self.candidates()}, fh)
        # `--summarise` would spawn `opencode run`; the SUMMARY RESULT is what
        # this class is about, so the model call is replaced, never made.
        import unittest.mock as mock
        import summarise_stories
        with mock.patch.object(summarise_stories, "summarise",
                               return_value=summarise):
            rc = dut.main(["--candidates", path, "--out", out,
                           "--today", "2026-11-05", "--slot", "morning",
                           "--attempts", os.path.join(self.tmp, "absent.log"),
                           "--summarise"])
        self.assertEqual(0, rc)
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)

    def result(self, count, model="gemini/gemini-3.5-flash-lite"):
        return {"summaries": {"https://a.example/%d" % n: self.TEXT
                              for n in range(count)},
                "model": model, "requests": 1, "waits": [], "refusals": []}

    def test_every_story_can_carry_a_written_summary(self):
        payload = self.payload_of(self.result(5))
        self.assertEqual(5, payload["summary_counts"]["summarised"])
        self.assertEqual(5, payload["summary_counts"]["total"])
        for story in payload["stories"]:
            self.assertEqual(self.TEXT, story["summary"])
        ok, reason = vs.validate_story(payload["stories"][0], 0)
        self.assertTrue(ok, "the validator must accept a written summary: %s"
                        % reason)

    def test_the_edition_kind_stays_unsummarised(self):
        payload = self.payload_of(self.result(5))
        self.assertEqual("unsummarised", payload["edition_kind"],
                         "the SEAL reads this field as 'no model wrote the "
                         "digest'; that is still true when a model wrote one "
                         "sentence per story")
        self.assertEqual({"rung": "derived", "attempt": None, "model": None},
                         payload["writer"])

    def test_the_note_counts_instead_of_claiming_every_text_is_the_publisher_s(self):
        payload = self.payload_of(self.result(2))
        self.assertIn("an AI wrote the summary for 2 of 5 stories",
                      payload["notes"])
        self.assertNotIn("the text under each headline is the publisher's own "
                         "description", payload["notes"],
                         "the old note over an AI sentence is a false claim")

    def test_a_batch_that_answered_nothing_keeps_the_old_edition(self):
        payload = self.payload_of({"summaries": {}, "model": None,
                                   "requests": 2, "waits": [],
                                   "refusals": ["gemini/a: quota"]})
        self.assertEqual({"summarised": 0, "total": 5},
                         payload["summary_counts"])
        self.assertIn("no AI model was configured or reachable",
                      payload["notes"])
        self.assertEqual("unsummarised", payload["edition_kind"])

    def test_no_story_is_dropped_for_lacking_a_summary(self):
        payload = self.payload_of(self.result(2))
        self.assertEqual(5, len(payload["stories"]),
                         "a partial answer must not shrink the edition")
        self.assertEqual("", payload["stories"][4]["summary"],
                         "the unsummarised story keeps what the deriver wrote")

    def test_a_failed_batch_leaves_a_debug_record_beside_the_edition(self):
        debug = [{"model": "gemini/gemini-3.5-flash-lite",
                  "reason": "exit_nonzero", "parse_error": "partial_json",
                  "finish_reason": None, "stop_reason": None, "usage": {},
                  "exit_code": 1, "timeout": 42,
                  "raw": '[{"url": "https://a.example/0", "summ'}]
        result = self.result(0, model=None)
        result["debug"] = debug
        self.payload_of(result)
        path = os.path.join(self.tmp, "summarise-debug.json")
        self.assertTrue(os.path.exists(path),
                        "a run that produced no summary has to explain itself")
        with open(path, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(debug, written["attempts"])
        self.assertTrue(written["generated_at"])

    def test_a_clean_run_writes_no_debug_file(self):
        self.payload_of(self.result(5))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "summarise-debug.json")),
            "an empty debug file would be noise in every artifact")

    def test_below_the_floor_no_model_is_called(self):
        path = os.path.join(self.tmp, "candidates.json")
        out = os.path.join(self.tmp, "stories.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"candidates": self.candidates()[:4]}, fh)
        import unittest.mock as mock
        import summarise_stories
        with mock.patch.object(summarise_stories, "summarise") as called:
            rc = dut.main(["--candidates", path, "--out", out,
                           "--today", "2026-11-05", "--slot", "morning",
                           "--attempts", os.path.join(self.tmp, "absent.log"),
                           "--summarise"])
        self.assertEqual(0, rc)
        called.assert_not_called()
        self.assertFalse(os.path.exists(out),
                         "nothing is written below the floor, so nothing should "
                         "have been spent on prose for it")


if __name__ == "__main__":
    unittest.main()
