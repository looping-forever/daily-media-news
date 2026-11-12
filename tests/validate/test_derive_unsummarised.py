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
import edition_kind  # noqa: E402
import english_text  # noqa: E402
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
        self.assertEqual(dut.NO_SUMMARY_TEXT, story["summary"])
        self.assertNotIn("for_you", story)
        self.assertEqual(story["supporting_sources"], [])
        self.assertIsNone(story["personal_relevance"])
        self.assertEqual(story["source_name"], "kathmandupost.com")
        ok, reason = vs.validate_story(story, 0)
        self.assertTrue(ok, "the unchanged validator must accept it: %s" % reason)


class TheFloorCarriesText(unittest.TestCase):
    """C8: an all-models-fail day must ship more than a headline.

    The text is QUOTED, never written: it is the feed's own description, so the
    edition is still honestly labelled unsummarised. Every rule below refuses a
    weaker or invented string and leaves the honest `NO_SUMMARY_TEXT` body --
    RUN 42144852087: an empty body rendered as a headline over a blank line.
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
        self.assertEqual(dut.NO_SUMMARY_TEXT, stories[0]["summary"])

    def test_a_description_that_repeats_the_headline_is_dropped(self):
        title = long_title("repeat")
        stories, _, _ = dut.derive([
            cand(title, "https://kathmandupost.com/r", category="nepal",
                 snippet=title),
        ], now=NOW)
        self.assertEqual(dut.NO_SUMMARY_TEXT, stories[0]["summary"],
                         "a repeat of the headline adds nothing to a headline")

    def test_a_description_that_is_a_headline_paraphrase_is_dropped(self):
        """RUN 42078530071: six of eight "publisher" bodies were the headline.

        The snippet was never judged as prose, so the same words that failed the
        model answer shipped as the publisher's own description.
        """
        title = "The Dangers in My Heart Season 3 Set for April 2027"
        stories, _, _ = dut.derive([
            cand(title, "https://kathmandupost.com/p", category="nepal",
                 snippet="A new teaser trailer for the third season of The "
                         "Dangers in My Heart has revealed an April 2027 "
                         "premiere window."),
        ], now=NOW)
        self.assertEqual(dut.NO_SUMMARY_TEXT, stories[0]["summary"],
                         "a paraphrase of the headline adds nothing a headline "
                         "does not already say")

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
        self.assertEqual(dut.NO_SUMMARY_TEXT, stories[0]["summary"])
        self.assertEqual(1, len(stories), "the story itself must survive")


class NoCardIsEverBlank(unittest.TestCase):
    """RUN 42144852087: eleven cards shipped as headline, blank line, `Link:`.

    The repair path already refused to write an empty body; the deriver's own
    `none` output did not, and the email faithfully rendered the hole.
    """

    def test_a_story_with_no_publisher_text_carries_an_honest_body(self):
        stories, _, _ = dut.derive([
            cand(long_title("bare"), "https://kathmandupost.com/b",
                 category="nepal", published=FRESH),
        ], now=NOW)
        story = stories[0]
        self.assertEqual(dut.NO_SUMMARY_TEXT, story["summary"])
        self.assertEqual("none", story["summary_source"],
                         "the fallback sentence is not the publisher's text")
        ok, reason = vs.validate_story(story, 0)
        self.assertTrue(ok, "a fallback body must still validate: %s" % reason)

    def test_the_payload_never_writes_an_empty_body(self):
        tmp = tempfile.mkdtemp(prefix="derive_blank_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "candidates.json")
        out = os.path.join(tmp, "stories.json")
        candidates = [cand(DISTINCT[n], "https://a.example/%d" % n,
                           category=("world_major" if n < 3 else "local_city"))
                      for n in range(5)]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"candidates": candidates}, fh)
        rc = dut.main(["--candidates", path, "--out", out,
                       "--today", "2026-10-28", "--slot", "morning",
                       "--attempts", os.path.join(tmp, "attempts.log")])
        self.assertEqual(0, rc)
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        for story in payload["stories"]:
            self.assertTrue(str(story["summary"]).strip(),
                            "blank card: %r" % story.get("title"))
        self.assertEqual(0, payload["summary_counts"]["summarised"],
                         "nothing here was written by a model or a publisher")


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
            self.assertEqual(dut.NO_SUMMARY_TEXT, story["summary"])
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


class TheHeldBackCountIsData(unittest.TestCase):
    """The count the page prints must come from the payload, not from its prose.

    Two stages withhold stories for their script -- the deriver and the validator --
    and each used to append its own sentence to `notes`. Run 41995372308's page
    ended up stating two different counts for one edition. The number now travels
    as a field, and the sentence is rewritten rather than stacked.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="derive_held_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_main(self, candidates):
        path = os.path.join(self.tmp, "candidates.json")
        out = os.path.join(self.tmp, "stories.json")
        log = os.path.join(self.tmp, "agent-attempts.log")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"collected_at": "2026-10-28 09:00",
                       "candidates": candidates}, fh)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("")
        rc = dut.main(["--candidates", path, "--out", out, "--today", "2026-10-28",
                       "--slot", "morning", "--attempts", log])
        self.assertEqual(rc, 0)
        with open(out, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def candidates(self):
        rows = [cand(DISTINCT[n], "https://a.example/%d" % n,
                     category=("world_major" if n < 3 else "local_city"))
                for n in range(5)]
        # Devanagari headline: readable, but not English, so the deriver withholds it.
        rows.append(cand("काठमाडौंमा आज बाढी आयो र धेरै परिवार प्रभावित भए",
                         "https://a.example/nepali", category="local_city"))
        return rows

    def test_the_payload_carries_the_count_the_page_prints(self):
        payload = self.run_main(self.candidates())
        self.assertEqual({"count": 1, "reason": "not_english"},
                         payload.get("held_back"),
                         "the page has no count to print unless the deriver states "
                         "it as data")
        self.assertEqual(1, len(english_text._HELD_BACK_RE.findall(payload["notes"])))

    def test_nothing_held_still_states_zero(self):
        payload = self.run_main(
            [cand(DISTINCT[n], "https://a.example/%d" % n,
                  category=("world_major" if n < 3 else "local_city"))
             for n in range(5)])
        self.assertEqual(0, payload["held_back"]["count"])
        self.assertNotIn("held back", payload["notes"])


class TheValidatorDoesNotAppendASecondCount(unittest.TestCase):
    """Validation withholds a few more stories; the total must be one number."""

    def test_it_adds_its_own_drops_to_the_carried_count(self):
        self.assertEqual(6, vs.held_back_total({"held_back": {"count": 5}}, 1),
                         "the page's count must be the whole edition's, not one "
                         "stage's share of it")

    def test_a_payload_without_the_field_starts_from_zero(self):
        self.assertEqual(2, vs.held_back_total({}, 2))
        self.assertEqual(2, vs.held_back_total({"held_back": "6"}, 2))

    def test_a_malformed_count_is_not_trusted(self):
        for record in ({"count": "many"}, {"count": None}, {"held_back": True}):
            self.assertEqual(1, vs.held_back_total(record, 1), "record %r" % (record,))


class TheKindFollowsTheCounts(unittest.TestCase):
    """A page that says "22 of 29" must never call itself "unsummarised"."""

    def test_none_some_and_all(self):
        self.assertEqual("unsummarised", edition_kind.kind_for(0, 29))
        self.assertEqual("partial", edition_kind.kind_for(22, 29))
        self.assertEqual("full", edition_kind.kind_for(29, 29))

    def test_an_empty_edition_is_unsummarised_not_full(self):
        self.assertEqual("unsummarised", edition_kind.kind_for(0, 0))

    def test_a_count_past_the_total_is_still_full(self):
        self.assertEqual("full", edition_kind.kind_for(30, 29))


class TheValidatorRecountsTheKind(unittest.TestCase):
    """Validation drops stories, so the kind has to be recomputed, not carried.

    The deriver's name describes its own list. A story dropped here changes the
    total, and if that story carried an AI summary it changes the count too --
    the exact case where a passed-through name starts lying.
    """

    def payload(self, sources):
        stories = [{"source_url": "https://a.example/%d" % n,
                    "summary_source": source,
                    # The count is content-aware now (validate_stories.
                    # is_real_summary): a `summary_source` label over an empty
                    # body is a stub, so the fixtures carry prose like the real
                    # deriver writes.
                    "summary": "Reported: The council approved the budget on "
                               "Tuesday, with road repair and school funding as "
                               "the two largest line items."}
                   for n, source in enumerate(sources)]
        return {"edition_kind": "partial",
                "summary_counts": {"summarised": sources.count("ai"),
                                   "total": len(sources)},
                "stories": stories}

    def test_a_dropped_written_story_lowers_both_numbers(self):
        data = self.payload(["ai", "ai", "ai", "publisher"])
        facts = vs.edition_facts(data, data["stories"][:3])
        self.assertEqual({"summarised": 3, "publisher": 0, "extracted": 0,
                          "none": 0, "total": 3}, facts["summary_counts"])
        self.assertEqual("full", facts["edition_kind"],
                         "the name must describe what survived, not what was sent")

    def test_a_dropped_publisher_story_can_make_the_edition_full(self):
        data = self.payload(["ai", "ai", "publisher"])
        facts = vs.edition_facts(data, data["stories"][:2])
        self.assertEqual("full", facts["edition_kind"])

    def test_a_partial_edition_stays_partial_when_the_mix_holds(self):
        data = self.payload(["ai", "ai", "publisher", "publisher"])
        facts = vs.edition_facts(data, data["stories"][:3])
        self.assertEqual("partial", facts["edition_kind"])
        self.assertEqual({"summarised": 2, "publisher": 1, "extracted": 0,
                          "none": 0, "total": 3}, facts["summary_counts"])

    def test_a_model_written_payload_gains_truthful_counts(self):
        """The round-4 contract: every path gets counts, not only the deriver's.

        Run 42056457121 was model-written, carried no `edition_kind`, and so the
        subject, hero and push had nothing honest to say about four stories whose
        summary was their own headline. This used to assert None -- that absence
        was the bug.
        """
        data = {"stories": [
            {"title": "Closure", "summary": "Reported: " + "Closure"},
            {"title": "Budget", "summary":
                "Confirmed: The ministry published the final figures on "
                "Wednesday, and the district allocation rises by nine percent."}]}
        facts = vs.edition_facts(data, data["stories"])
        self.assertEqual("partial", facts["edition_kind"])
        self.assertEqual({"summarised": 1, "publisher": 0, "extracted": 0,
                          "none": 1, "total": 2}, facts["summary_counts"])


class TheNoteFollowsTheSameCount(unittest.TestCase):
    """The hero and the note print one pair of numbers, so both must move together.

    A drop here used to leave the sentence describing the deriver's list: the page
    said "AI summary for 22 of 27" over a note saying "22 of 29".
    """

    def payload(self, sources):
        stories = [{"source_url": "https://b.example/%d" % n,
                    "summary_source": source,
                    # Prose, because the count is content-aware now: a
                    # `summary_source` label over an empty body is not counted.
                    "summary": "Reported: The council approved the budget on "
                               "Tuesday, with road repair and school funding as "
                               "the two largest line items."}
                   for n, source in enumerate(sources)]
        written = sources.count("ai")
        return {"edition_kind": edition_kind.kind_for(written, len(sources)),
                "summary_counts": {"summarised": written, "total": len(sources)},
                "notes": edition_kind.summary_note(
                    edition_kind.kind_for(written, len(sources)), written,
                    len(sources)),
                "stories": stories}

    def test_a_smaller_total_rewrites_the_sentence(self):
        data = self.payload(["ai"] * 22 + ["publisher"] * 7)
        facts = vs.edition_facts(data, data["stories"][:27])
        note = vs.rewritten_note(facts, data["notes"])
        self.assertIn("22 of 27 stories", note)
        self.assertNotIn("29", note,
                         "the note still quotes the list the deriver saw")

    def test_a_dropped_remainder_makes_the_sentence_complete(self):
        data = self.payload(["ai", "ai", "publisher"])
        facts = vs.edition_facts(data, data["stories"][:2])
        note = vs.rewritten_note(facts, data["notes"])
        self.assertIn("for all 2 stories", note)
        self.assertNotIn("the rest quote", note,
                         "a complete edition describes a remainder that is gone")

    def test_dropping_every_written_story_stops_quoting_a_count(self):
        data = self.payload(["ai", "ai", "publisher"])
        facts = vs.edition_facts(data, data["stories"][2:])
        note = vs.rewritten_note(facts, data["notes"])
        self.assertIn("no AI summary survived validation", note)
        self.assertNotIn("an AI wrote the summary", note,
                         "the dropped summaries are still claimed on the page")

    def test_the_no_ai_reason_paragraph_is_left_alone(self):
        data = self.payload(["publisher", "publisher"])
        facts = vs.edition_facts(data, data["stories"])
        reason = ("Unsummarised edition: every AI model this build reached "
                  "refused on its provider's quota.")
        self.assertIsNone(vs.rewritten_note(facts, reason),
                          "the deriver's reason paragraph is the only true "
                          "sentence for a zero-summary edition")

    def test_the_held_back_sentence_is_rewritten_not_duplicated(self):
        data = self.payload(["ai"] * 2 + ["publisher"] * 2)
        facts = vs.edition_facts(data, data["stories"][:3])
        notes = english_text.replace_held_back(data["notes"], 5)
        notes = english_text.replace_held_back(
            vs.rewritten_note(facts, notes), 5)
        self.assertEqual(1, len(english_text._HELD_BACK_RE.findall(notes)))
        self.assertIn("2 of 3 stories", notes)
        self.assertIn("5 stories were held back", notes)



class OneSentenceHoweverManyStagesWriteIt(unittest.TestCase):
    def test_replacing_never_stacks_two_numbers(self):
        notes = english_text.replace_held_back("Research note.", 5)
        notes = english_text.replace_held_back(notes, 6)
        self.assertEqual(1, len(english_text._HELD_BACK_RE.findall(notes)),
                         "two stages wrote the same sentence twice, so two readers "
                         "can quote two different numbers")
        self.assertIn("6 stories were held back", notes)
        self.assertNotIn("5 stories", notes)

    def test_zero_removes_the_sentence(self):
        notes = english_text.replace_held_back(
            "Research note. 5 stories were held back because they are not in English.",
            0)
        self.assertEqual("Research note.", notes)


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

    def test_the_edition_kind_follows_the_count(self):
        """0 summaries is unsummarised, some is partial, all is full.

        This used to assert `unsummarised` for a fully-written edition, because
        the SEAL read the field as "no model wrote the digest". The owner
        overruled that on 2026-11-05: a page that says "22 of 29" must not call
        itself "no AI today", so the field now names what the counts say.
        """
        self.assertEqual("full", self.payload_of(self.result(5))["edition_kind"])
        self.assertEqual("partial", self.payload_of(self.result(2))["edition_kind"])
        self.assertEqual("unsummarised",
                         self.payload_of(self.result(0))["edition_kind"])

    def test_every_story_is_marked_with_where_its_summary_came_from(self):
        payload = self.payload_of(self.result(2))
        sources = [story.get("summary_source") for story in payload["stories"]]
        self.assertEqual(2, sources.count("ai"))
        self.assertEqual(3, sources.count("none"),
                         "the validator subtracts a dropped story by this mark, "
                         "and a fallback body is not the publisher's text")

    def test_a_fully_summarised_edition_does_not_describe_a_remainder(self):
        payload = self.payload_of(self.result(5))
        self.assertIn("an AI wrote the summary for all 5 stories",
                      payload["notes"])
        self.assertNotIn("the rest quote", payload["notes"],
                         "there is no 'rest' when every story was written")

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
        self.assertEqual(dut.NO_SUMMARY_TEXT,
                         payload["stories"][4]["summary"],
                         "the unsummarised story keeps a body the page can draw")

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

    def test_truncating_a_rejected_answer_never_splits_a_character(self):
        family = "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466"
        clipped = dut.clip_text(family * 40, limit=10)
        self.assertTrue(clipped.endswith("\u2026"),
                        "the cut is marked, so nobody mistakes it for the whole")
        self.assertNotIn("\u200D\u2026", clipped,
                         "a zero-width joiner left dangling is half a character")
        self.assertTrue((family * 40).startswith(clipped[:-1]),
                        "the prefix is untouched text")

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


class TheBannerNamesTheRealCause(unittest.TestCase):
    """Run 41995372308 line 515 of the page, measured, not paraphrased:

        "Unsummarised edition: every AI model this build reached refused on its
         provider's quota, so the text under each headline is the publisher's
         own description, not a summary written for you. 5 stories were held
         back because they are not in…"

    Two things were wrong with that sentence. The reason was the agentic walk's
    log, although the keyed Gemini rungs had ANSWERED in this run -- three
    replies the parser threw away -- so the page blamed a quota that was still
    there. And the sentence was cut mid-word at the renderer's 240-char cap,
    because it grew a held-back clause it had no room for.
    """

    QUOTA_LOG = NoAIReason.QUOTA_LOG

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="derive_cause_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_main(self, causes, attempts=None, summarised=0):
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
        import unittest.mock as mock
        import summarise_stories
        result = {"summaries": {}, "model": "gemini/gemini-3.5-flash-lite",
                  "requests": 2, "waits": [], "refusals": [], "debug": [],
                  "causes": causes}
        with mock.patch.object(summarise_stories, "summarise",
                               return_value=result):
            rc = dut.main(["--candidates", path, "--out", out,
                           "--today", "2026-11-05", "--slot", "morning",
                           "--attempts", log, "--summarise"])
        self.assertEqual(0, rc)
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)["notes"]

    def test_the_summarisers_own_causes_outrank_the_walk_log(self):
        notes = self.run_main({"unreadable_reply": 5}, self.QUOTA_LOG)
        self.assertNotIn("quota", notes,
                         "the keyed rungs answered in this run; the walk's "
                         "log must not outrank what they said")
        self.assertIn("unreadable reply", notes)
        self.assertIn("5 stories", notes)

    def test_every_cause_costs_its_own_stories_in_the_note(self):
        notes = self.run_main({"unreadable_reply": 3, "model_timeout": 2})
        self.assertIn("3 stories", notes)
        self.assertIn("2", notes)
        self.assertIn("timeout", notes)

    def test_a_parse_failure_never_blames_a_quota(self):
        note = dut.note_for(dut.REASON_PARSE, self.QUOTA_LOG,
                            causes={"unreadable_reply": 5})
        self.assertEqual(dut.REASON_PARSE, dut.reason_from_causes(
            {"unreadable_reply": 5}))
        self.assertNotIn("quota", note)

    def test_the_note_still_says_the_text_is_the_publishers_own(self):
        note = dut.note_for(dut.REASON_PARSE, "", causes={"unreadable_reply": 5})
        self.assertIn("publisher's own description", note)

    def test_the_walk_path_still_names_the_quota(self):
        note = dut.note_for(dut.REASON_QUOTA, self.QUOTA_LOG)
        self.assertIn("refused on its provider's quota (gemini, opencode)", note)

    def test_the_note_is_complete_at_both_rendering_widths(self):
        import build_email
        import english_text
        import render_digest
        shapes = (
            {"unreadable_reply": 1},
            {"unreadable_reply": 9},
            {"unreadable_reply": 19, "model_timeout": 19},
            {"unreadable_reply": 29, "model_timeout": 19,
             "provider_refusal": 9},
            {"provider_refusal": 29},
            {"model_timeout": 2, "runner_fault": 3},
        )
        for causes in shapes:
            for held in (0, 1, 29):
                note = dut.note_for(dut.reason_from_causes(causes), "",
                                    causes=causes)
                if held:
                    note += " " + english_text.held_back_note(held)
                for module in (render_digest, build_email):
                    shown = render_digest.reader_note_text(note)
                    self.assertLessEqual(
                        len(shown), module.NOTE_LIMIT,
                        "%r is %d chars at %s's cap"
                        % (causes, len(shown), module.__name__))
                    self.assertFalse(
                        module.capped_note(note).endswith("\u2026"),
                        "%r still gets cut at %s's cap"
                        % (causes, module.__name__))

    def test_a_cause_with_no_stories_is_not_reported(self):
        self.assertEqual(dut.REASON_ALL_FAILED,
                         dut.reason_from_causes({}))
        self.assertEqual(dut.REASON_ALL_FAILED,
                         dut.reason_from_causes({"runner_fault": 0}))


#: RUN 42070185644 (2026-11-05): six stories shipped `summary_source: "ai"` while
#: their body only restated their own headline. This one is the run's verbatim.
#: The label is what the page and the push consulted, so a body that fails the
#: prose test was still announced as written.
HANOVER_TITLE = ("Abduction, barricade situation causes road closure in Hanover "
                 "- WRIC ABC 8News")
HANOVER_STUB = ("An abduction and barricade situation has resulted in a road "
                "closure in Hanover.")
HANOVER_PUBLISHED = ("Police said a barricade situation following an abduction "
                     "closed a road in Hanover on Tuesday afternoon.")
HANOVER_PROSE = ("Police said the road between Hanover Square and Mill Road was "
                 "closed for four hours while officers negotiated, and 12 people "
                 "were escorted out of the building.")


class TheDeriverNeverLabelsAStubAsWritten(unittest.TestCase):
    """ONE predicate decides prose, and it decides the LABEL too.

    The deriver used to mark any non-empty model answer as AI-written. The
    validator counted with `is_real_summary`, so the label and the number told
    two different stories -- and the label is the one the push printed.
    """

    def story(self, url="https://x/1"):
        return {"title": HANOVER_TITLE, "summary": HANOVER_PUBLISHED,
                "source_url": url, "category": "world_major"}

    def test_a_headline_rewrite_is_labelled_publisher_and_not_counted(self):
        stories = [self.story()]
        written = dut.apply_summaries(stories, {"https://x/1": HANOVER_STUB})
        self.assertEqual(0, written,
                         "a body that is the headline again is not written")
        self.assertEqual("publisher", stories[0]["summary_source"])
        self.assertEqual(HANOVER_PUBLISHED, stories[0]["summary"],
                         "the publisher's own description stays; the stub is not "
                         "kept under an AI label")

    def test_a_summary_that_is_real_prose_stays_ai_written(self):
        stories = [self.story()]
        written = dut.apply_summaries(stories, {"https://x/1": HANOVER_PROSE},
                                      snippets={"https://x/1": HANOVER_PUBLISHED})
        self.assertEqual(1, written)
        self.assertEqual("ai", stories[0]["summary_source"])
        self.assertEqual(HANOVER_PROSE, stories[0]["summary"])


if __name__ == "__main__":
    unittest.main()
