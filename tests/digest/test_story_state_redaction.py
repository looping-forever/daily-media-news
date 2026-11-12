#!/usr/bin/env python3
"""tests/digest/test_story_state_redaction.py -- the write-path privacy guard.

WHY THIS EXISTS. Measured 2026-11-12: scripts/check_public_sanitisation.py
failed on the published tree with two matches in state/active_stories.json
(the same private run in one event's known_facts and latest_development). The
gate did its job -- it refused -- but nothing stopped the pipeline from writing
the run in the first place, so the next run of the same article could write it
again and the failure would repeat.

WHAT THIS PINS. story_state.upsert_sighting is the only writer of those fields,
so the guard lives there, and it decides by the same rule the gate uses in
reverse: maximal alphanumeric run, lowercased, SHA-256, compared against the
fingerprints in scripts/check_public_sanitisation.py. A test uses a synthetic
vocabulary instead of the real one, because the real tokens must never appear
in this repository -- not even as a test fixture.

    A PRIVATE RUN NEVER LANDS, on the create path or on the update path.
    A CLEAN SENTENCE IS STORED UNCHANGED -- the guard is not a blanket scrub.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import check_public_sanitisation as sanitise  # noqa: E402
import story_state  # noqa: E402

#: Stands in for a private value. Not a real token: the real vocabulary is
#: compared by fingerprint only, so this file never needs the plaintext.
FAKE_PRIVATE = "zzzdistrict"

SENTENCE = ("Exactly 2,486 passengers used the transport provided by the Armed "
            "Police Force across Kathmandu and %s during the Tihar holiday."
            % FAKE_PRIVATE)


def synthetic_vocabulary():
    return mock.patch.object(sanitise, "VOCABULARY_SET",
                             frozenset({sanitise.fingerprint(FAKE_PRIVATE)}))


def story(**overrides):
    base = {
        "title": "Passenger numbers on the holiday transport service",
        "url": "https://example.com/holiday-transport",
        "event_key": "holiday-transport",
        "category": "nepal",
        "summary": SENTENCE,
        "known_facts": SENTENCE,
        "latest_development": SENTENCE,
    }
    base.update(overrides)
    return base


class GateRedactTests(unittest.TestCase):
    """The gate's redact() is the shared rule; story_state must not restate it."""

    def test_redact_replaces_only_known_runs(self):
        text = "Kathmandu and %s and Kathmandu" % FAKE_PRIVATE
        with synthetic_vocabulary():
            self.assertEqual("Kathmandu and [redacted] and Kathmandu",
                             sanitise.redact(text))

    def test_redact_is_case_insensitive_and_leaves_clean_text_alone(self):
        clean = "Exactly 2,486 passengers across Kathmandu."
        with synthetic_vocabulary():
            self.assertEqual("[redacted]", sanitise.redact(FAKE_PRIVATE.upper()))
            self.assertEqual(clean, sanitise.redact(clean))

    def test_redact_never_splits_a_run(self):
        # "notzzzdistrict" is ONE run, so no half-match may be replaced inside it.
        with synthetic_vocabulary():
            self.assertEqual("notzzzdistrict", sanitise.redact("notzzzdistrict"))


class RedactionAtTheWriteTests(unittest.TestCase):

    def test_a_private_run_never_reaches_the_state_file(self):
        state = {"version": story_state.STATE_VERSION, "stories": [], "archived": []}
        with synthetic_vocabulary():
            story_state.upsert_sighting(
                state,
                story(title="Transport across Kathmandu and %s" % FAKE_PRIVATE),
                "2026-11-12")

        event = state["stories"][0]
        for field in ("canonical_title", "known_facts", "latest_development"):
            self.assertIn(story_state.PRIVATE_RUN_REPLACEMENT, event[field],
                          "%s kept nothing to mark the removal" % field)
            self.assertNotIn(FAKE_PRIVATE, event[field].lower(),
                             "%s still carries the private run" % field)
        # The remainder of the fact survives, so the sentence stays readable.
        self.assertIn("Kathmandu", event["known_facts"])
        self.assertIn("Tihar holiday", event["known_facts"])
        self.assertIn("2,486 passengers", event["known_facts"])

    def test_a_clean_sentence_is_stored_unchanged(self):
        clean = "Exactly 2,486 passengers used the transport across Kathmandu."
        state = {"version": story_state.STATE_VERSION, "stories": [], "archived": []}
        with synthetic_vocabulary():
            story_state.upsert_sighting(
                state, story(summary=clean, known_facts=clean,
                             latest_development=clean), "2026-11-12")

        event = state["stories"][0]
        self.assertEqual(clean, event["known_facts"])
        self.assertEqual(clean, event["latest_development"])
        self.assertNotIn(story_state.PRIVATE_RUN_REPLACEMENT, event["known_facts"])

    def test_the_update_path_is_guarded_too(self):
        state = {"version": story_state.STATE_VERSION, "stories": [], "archived": []}
        with synthetic_vocabulary():
            story_state.upsert_sighting(
                state, story(summary="A short first report.",
                             known_facts="A short first report.",
                             latest_development="A short first report."),
                "2026-11-12")
            # Materially different facts, and this time they carry the run.
            event_id, label = story_state.upsert_sighting(
                state, story(known_facts=SENTENCE), "2026-11-12")

        self.assertEqual("UPDATED", label)
        event = next(e for e in state["stories"] if e["event_id"] == event_id)
        self.assertIn(story_state.PRIVATE_RUN_REPLACEMENT, event["known_facts"])
        self.assertNotIn(FAKE_PRIVATE, event["known_facts"].lower())
        self.assertNotIn(FAKE_PRIVATE, event["latest_development"].lower())


if __name__ == "__main__":
    unittest.main()
