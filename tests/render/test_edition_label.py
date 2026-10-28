#!/usr/bin/env python3
"""tests/render/test_edition_label.py — "unsummarised edition (no AI today)".

WHY THIS EXISTS. The no-AI fallback renders headlines with an EMPTY summary. An
unlabelled empty summary is indistinguishable from a model that had nothing to
say, so the label is not decoration: it is the only thing that keeps a
degraded edition honest on the page. Three surfaces must say the same words
(page, email subject, push title); this file pins the PAGE and the renderer
contract.

The property that matters as much as the label itself:

    NO edition_kind, NO CHANGE. An absent `edition_kind` renders the page
    byte-for-byte as it did before this feature, exactly like `delivery_line`.

The words are pinned as a literal, not imported from the module under test,
because a test that imports the constant cannot notice the constant being
edited.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fixtures import validated_payload  # noqa: E402

import render_digest  # noqa: E402

LABEL = "Unsummarised edition (no AI today)"
SLOT = "evening"
DAY = "2026-10-28"
STAMP = "2026-10-28 18:00"
NO_SCHEDULE = os.path.join(_HERE, "does-not-exist-schedule.json")


class HealthBlock(unittest.TestCase):
    def test_absent_renders_nothing_at_all(self):
        self.assertEqual(render_digest.render_health_block({"state": "ok"}), "",
                         "a caller that never heard of the edition kind gets "
                         "the old bytes: no line, no placeholder")

    def test_label_is_its_own_line_not_an_alarm(self):
        html = render_digest.render_health_block({"state": "ok"},
                                                 edition_line=LABEL)
        self.assertEqual(html, '<p class="health-edition">%s</p>' % LABEL,
                         "a healthy unsummarised page must NOT be wrapped in "
                         "the red .health-banner: no AI is a kind, not a fault")
        self.assertNotIn("health-banner", html)

    def test_label_survives_alongside_a_real_health_alarm(self):
        health = {"state": "degraded",
                  "critical": {"hosts_down": ["tu.edu.np"]}}
        html = render_digest.render_health_block(health, edition_line=LABEL)
        self.assertIn("health-banner", html)
        self.assertIn('<p class="health-edition">%s</p>' % LABEL, html)
        self.assertLess(html.index("health-edition"), html.index("health-down"),
                        "the edition kind leads; the host list follows")

    def test_label_text_is_escaped(self):
        html = render_digest.render_health_block({"state": "ok"},
                                                 edition_line="<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)


class RenderPage(unittest.TestCase):
    def page(self, meta):
        return render_digest.render_page([], SLOT, DAY, STAMP, meta,
                                         schedule_path=NO_SCHEDULE)

    def test_unsummarised_meta_renders_the_label(self):
        html = self.page({"edition_kind": "unsummarised"})
        self.assertIn(LABEL, html)
        self.assertIn('<p class="health-edition">', html)

    def test_ordinary_meta_renders_no_label(self):
        for meta in ({}, {"edition_kind": None}, {"edition_kind": "full"},
                     validated_payload("ok")):
            html = self.page(meta)
            self.assertNotIn(LABEL, html)
            self.assertNotIn('<p class="health-edition">', html,
                             "no edition kind means no new ELEMENT (the CSS rule "
                             "is always in the stylesheet)")

    def test_only_the_exact_kind_is_labelled(self):
        for kind in ("", "partial", "UNSUMMARISED", "unsummarised-ish"):
            self.assertNotIn(LABEL, self.page({"edition_kind": kind}))


if __name__ == "__main__":
    unittest.main()
