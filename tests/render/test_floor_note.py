#!/usr/bin/env python3
"""tests/render/test_floor_note.py — a short edition says so on the page.

WHAT THIS PINS
--------------
Run 43235310367 shipped 2 cards and the page banner was the ordinary one. The
reason was structural: the edition banner only exists for FALLBACK_KINDS, and a
model-written edition whose every summary is written renders nothing at all --
deliberately, because calling a complete digest a "fallback edition" would be the
opposite lie. `edition_kind` is about COVERAGE (`kind_for` counts written vs
total), so 2 cards with 2 written summaries is `full`.

Coverage and the floor are different claims, so the floor sentence is rendered
OUTSIDE that branch: whenever the payload says `floor.met` is false, the banner
names both numbers. These tests pin that, including the exact `full` +
model-written shape that hid the defect, and that a payload without a floor
keeps the old bytes.

Offline: literals, no provider, stdlib unittest.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import render_digest  # noqa: E402

SLOT = "evening"
DAY = "2026-11-12"
STAMP = "2026-11-12 18:00"
NO_SCHEDULE = os.path.join(_HERE, "does-not-exist-schedule.json")
SHORT = "Short edition: 2 stories, below the 4-story floor."


def meta(**overrides):
    """A complete, model-written 2-card edition — the 43235310367 payload."""
    base = {
        "edition_kind": "full",
        "writer": {"rung": "deepseek", "model": "example"},
        "summary_counts": {"summarised": 2, "total": 2,
                           "publisher": 0, "none": 0, "extracted": 0},
        "floor": {"required": 4, "kept": 2, "met": False},
    }
    base.update(overrides)
    return base


def page(m):
    return render_digest.render_page([], SLOT, DAY, STAMP, m,
                                     schedule_path=NO_SCHEDULE)


class TheShortEditionSaysSo(unittest.TestCase):
    def test_a_full_written_two_card_edition_states_the_floor(self):
        """THE DEFECT: `full` coverage meant no banner at all."""
        html = page(meta())

        self.assertIn(SHORT, html,
                      "a 2-card edition that met no floor is still rendered "
                      "as an ordinary full digest")

    def test_a_derived_short_edition_gets_one_sentence_not_two(self):
        html = page(meta(writer=None, edition_kind="partial",
                         summary_counts={"summarised": 0, "total": 2,
                                         "publisher": 2, "none": 0,
                                         "extracted": 0}))

        self.assertEqual(1, html.count(SHORT),
                         "the floor sentence was appended twice, or not at all")

    def test_a_met_floor_renders_nothing(self):
        html = page(meta(floor={"required": 4, "kept": 4, "met": True}))

        self.assertNotIn("Short edition", html,
                         "a full edition is advertising a floor it met")

    def test_a_payload_without_a_floor_keeps_the_old_bytes(self):
        bare = meta()
        bare.pop("floor")

        self.assertNotIn("Short edition", page(bare),
                         "a payload that predates `floor` gained a sentence")

    def test_a_broken_floor_record_does_not_render_a_half_sentence(self):
        for record in ({"met": False}, {"met": False, "kept": "x", "required": 4},
                       {"met": False, "kept": 2, "required": 0}):
            html = page(meta(floor=record))
            self.assertNotIn("Short edition", html, repr(record))


if __name__ == "__main__":
    unittest.main()
