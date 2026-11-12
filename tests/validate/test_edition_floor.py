#!/usr/bin/env python3
"""tests/validate/test_edition_floor.py — the floor is counted after dedup, and stated.

WHAT THIS PINS
--------------
Run 43235310367 (2026-11-12 evening) shipped a 2-card edition. The workflow's
pre-derive gate counted 4 stories that survived SCHEMA validation, found 4 >=
STORIES_FLOOR (4), skipped the fallback, and said "a model-written digest
exists". Then `validate_stories.py` ran for real: history dedup removed the two
stories whose URLs had already been sent, and the edition went out with 2 cards.
Nothing on the page said so -- `edition_kind` describes COVERAGE, so 2 cards with
2 written summaries is "full", and "full" is what the banner showed.

Two separate claims are pinned here:

  * THE COUNT IS THE VALIDATOR'S OWN. `--count-only` runs the whole pipeline --
    schema, keywords, spoiler, crime, history dedup, event dedup, rank cap --
    and prints `COUNT=<n>`. It writes no payload and no state. The 4-to-2 shape
    above is reproduced end to end, so a gate that counts schema survivors again
    fails this suite.
  * THE FLOOR IS DATA AND A SENTENCE. `--floor` writes
    `{"required", "kept", "met"}` onto the payload and, when short, one sentence
    into the notes. The repair pass rewrites both from the cards it actually
    kept, so the number on the page cannot describe a list that no longer exists.

Offline: temp directories, literals, no provider, stdlib unittest.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from . import REPO_ROOT  # noqa: F401  (REPO_ROOT puts scripts/ on sys.path)

import summarise_stubs
import validate_stories as vs

FIXTURE_CONFIG = os.path.join(REPO_ROOT, "tests", "fixtures", "config")
TODAY = "2026-11-12"
SHORT_SENTENCE = "Short edition: 2 stories, below the 4-story floor."


def story(title, url, **overrides):
    """A story that passes every check, so each test can vary ONE thing."""
    base = {
        "title": title,
        "summary": "Confirmed: the works started on the eastern section today.",
        "category": "nepal",
        "priority": 5,
        "source_name": "Kathmandu Post",
        "source_url": url,
        "published_at": "2026-11-12",
        "event_date": None,
        "location": None,
        "reliability": "tier1",
        "spoiler_level": "none",
        "spoiler_reason": "",
        "relevance_reason": "Infrastructure project in the valley.",
        "language": "en",
    }
    base.update(overrides)
    return base


#: Four stories that each survive the schema, with titles far enough apart that
#: the in-run title-overlap check keeps all four.
FOUR = [
    story("Kathmandu Valley Ring Road Widening Begins",
          "https://kathmandupost.example.com/ring-road"),
    story("TU Publishes Revised Semester Exam Schedule",
          "https://tuexam.example.edu.np/schedule"),
    story("New Direct Bus Service to Pokhara Starts",
          "https://kathmandupost.example.com/bus-pokhara"),
    story("Mobile Wallet Fees Cut by Regulator",
          "https://finance.example.com/wallet-fees"),
]


def already_sent(stories):
    """The history file that makes `stories` look like yesterday's edition."""
    return {"stories": [{"title": s["title"], "url": s["source_url"],
                         "date_sent": "2026-11-11"} for s in stories]}


def run_validate(stories, history=None, floor=None, notes=None):
    tmp = tempfile.mkdtemp(prefix="edition-floor-")
    in_path = os.path.join(tmp, "stories.json")
    out_path = os.path.join(tmp, "validated.json")
    payload = {"digest_time": "evening", "date": TODAY,
               "research_status": "full", "stories": stories}
    if notes is not None:
        payload["notes"] = notes
    with open(in_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    history_path = os.path.join(tmp, "history.json")
    if history:
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump(history, fh)
    argv = ["--in", in_path, "--out", out_path,
            "--history", history_path,
            "--today", TODAY,
            "--config-dir", FIXTURE_CONFIG,
            "--state", os.path.join(tmp, "active_stories.json"),
            "--candidates", os.path.join(tmp, "candidates.json")]
    if floor is not None:
        argv += ["--floor", str(floor)]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = vs.main(argv)

    written = {}
    if os.path.isfile(out_path):
        with open(out_path, encoding="utf-8") as fh:
            written = json.load(fh)
    return {"rc": rc, "output": buf.getvalue(), "payload": written}


def run_count_only(stories, history=None):
    tmp = tempfile.mkdtemp(prefix="edition-count-")
    in_path = os.path.join(tmp, "stories.json")
    with open(in_path, "w", encoding="utf-8") as fh:
        json.dump({"digest_time": "evening", "date": TODAY,
                   "research_status": "full", "stories": stories}, fh)
    history_path = os.path.join(tmp, "history.json")
    if history:
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump(history, fh)
    state_path = os.path.join(tmp, "active_stories.json")
    out_path = os.path.join(tmp, "validated.json")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = vs.main(["--in", in_path, "--out", out_path,
                      "--history", history_path, "--today", TODAY,
                      "--config-dir", FIXTURE_CONFIG,
                      "--state", state_path,
                      "--candidates", os.path.join(tmp, "candidates.json"),
                      "--count-only"])
    return {"rc": rc, "output": buf.getvalue(), "out": out_path,
            "state": state_path}


class TheValidatorOwnsTheCount(unittest.TestCase):
    """`--count-only` must reproduce the 4-survivors-to-2-cards shape."""

    def test_four_schema_survivors_two_already_sent_count_as_two(self):
        """THE 43235310367 SHAPE: the gate said 4, the edition carried 2."""
        result = run_count_only(FOUR, history=already_sent(FOUR[:2]))

        self.assertEqual(0, result["rc"])
        self.assertIn("COUNT=2", result["output"],
                      "the count is still taken before history dedup, so a "
                      "4-survivor payload that dedup cuts to 2 would pass the "
                      "floor gate again")
        self.assertIn("schema-survivors=4", result["output"],
                      "the schema count is no longer reported, so the drop "
                      "between the two numbers is invisible in the log")
        self.assertIn("after-history-dedup=2", result["output"])

    def test_count_only_writes_no_payload_and_records_no_sighting(self):
        result = run_count_only(FOUR)

        self.assertFalse(os.path.exists(result["out"]),
                         "--count-only wrote a validated payload; the real "
                         "Validate step owns that file and would be clobbered")
        self.assertFalse(os.path.exists(result["state"]),
                         "--count-only recorded a sighting; a read-only count "
                         "must not advance the active-story state")

    def test_a_payload_that_survives_nothing_counts_zero(self):
        broken = [story("", "https://a.example.com/1"),
                  story("   ", "https://a.example.com/2")]
        result = run_count_only(broken)

        self.assertIn("COUNT=0", result["output"])

    def test_count_only_does_not_bypass_the_realness_gate(self):
        """A stub summary still counts as a card; a broken story does not."""
        stubs = [story(s["title"], s["source_url"]) for s in FOUR]
        for s in stubs:
            s["summary"] = "Reported: " + s["title"]
        result = run_count_only(stubs)

        self.assertIn("COUNT=4", result["output"],
                      "the count must be the number of CARDS the edition would "
                      "carry, not the number of written summaries")


class TheFloorIsWrittenAsData(unittest.TestCase):
    """`--floor` records the requirement and states a miss in plain words."""

    def test_a_met_floor_is_recorded_and_says_nothing(self):
        payload = run_validate(FOUR, floor=4)["payload"]

        self.assertEqual({"required": 4, "kept": 4, "met": True},
                         payload.get("floor"))
        self.assertNotIn("Short edition", payload.get("notes") or "")

    def test_a_short_edition_states_how_short_in_the_notes(self):
        payload = run_validate(FOUR[:2], floor=4)["payload"]

        self.assertEqual({"required": 4, "kept": 2, "met": False},
                         payload.get("floor"))
        self.assertEqual(1, (payload.get("notes") or "").count(SHORT_SENTENCE),
                         "a 2-card edition does not say it is short: notes=%r"
                         % payload.get("notes"))

    def test_the_sentence_is_rewritten_not_appended(self):
        """Validation runs after repair can restate the count; two must not survive."""
        stale = "Short edition: 5 stories, below the 6-story floor."
        notes = run_validate(FOUR[:2], floor=4, notes=stale)["payload"].get("notes") or ""

        self.assertEqual(1, notes.count("Short edition:"),
                         "the short-edition sentence was appended to a stale "
                         "one, so the field states two different counts: %r"
                         % notes)
        self.assertIn(SHORT_SENTENCE, notes)
        self.assertNotIn("5 stories", notes)

    def test_no_floor_argument_leaves_the_old_payload_shape(self):
        """A caller that never asked about a floor must get the old bytes."""
        payload = run_validate(FOUR)["payload"]

        self.assertNotIn("floor", payload,
                         "a payload gained a `floor` key without being asked "
                         "for one; every payload-shape assertion downstream "
                         "sees a field it was never told about")


class TheRepairKeepsTheFloorHonest(unittest.TestCase):
    """A story can leave the edition during repair; the number must follow it."""

    def payload(self, count, floor_required):
        stories = [story("Ring Road Works Begin", "https://a.example.com/1"),
                   story("Exam Schedule Revised", "https://a.example.com/2"),
                   story("Bus Service to Pokhara", "https://a.example.com/3"),
                   story("Wallet Fees Cut", "https://a.example.com/4"),
                   story("Weather Warning Issued", "https://a.example.com/5")]
        stories = stories[:count]
        for index, row in enumerate(stories):
            row["summary"] = ("Confirmed: an ordinary written sentence number "
                              "%d about what happened." % index)
            row["summary_source"] = "ai"
        # The last card is an unrepairable stub: no model answer, no source text.
        stories[-1]["summary"] = "Reported: " + stories[-1]["title"]
        return {"stories": stories, "notes": "",
                "floor": {"required": floor_required, "kept": count,
                          "met": count >= floor_required}}

    def repair(self, payload, floor):
        return summarise_stubs.repair(
            payload, snippets={},
            summarise=lambda rows, budget_seconds=None: {"summaries": {},
                                                         "budget_spent": False},
            log=lambda *_a, **_k: None, floor=floor)

    def test_a_drop_rewrites_the_count_the_page_will_print(self):
        payload = self.payload(5, 4)
        self.repair(payload, floor=4)

        self.assertEqual(4, len(payload["stories"]),
                         "the unrepairable stub was not dropped, so this test "
                         "is no longer exercising a drop")
        self.assertEqual({"required": 4, "kept": 4, "met": True},
                         payload["floor"],
                         "the floor still claims 5 cards while the edition "
                         "carries 4")
        self.assertNotIn("Short edition", payload["notes"] or "")

    def test_the_floor_still_blocks_a_drop_that_would_break_it(self):
        payload = self.payload(5, 5)
        self.repair(payload, floor=5)

        self.assertEqual(5, len(payload["stories"]),
                         "the floor no longer prevents a drop that takes the "
                         "edition below it")
        self.assertEqual({"required": 5, "kept": 5, "met": True},
                         payload["floor"])

    def test_a_payload_without_a_floor_gains_none(self):
        payload = self.payload(4, 0)
        payload.pop("floor")
        self.repair(payload, floor=4)

        self.assertNotIn("floor", payload,
                         "repair invented a floor field for a payload that "
                         "never had one")


if __name__ == "__main__":
    unittest.main()
