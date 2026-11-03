#!/usr/bin/env python3
"""tests/breaking/test_english_push_gate.py — a breaking push may only carry text
the owner's page can draw.

Run:
    python -m unittest discover -s tests/breaking -t . -v

WHY THIS EXISTS
---------------
A breaking headline reaches FOUR surfaces: the ntfy push, the Telegram push, the
email and the preview. The 2026-11-03 digest bug proved that a Devanagari title
in this repo renders as nothing on the page, and a push has the same problem on
any client whose font stack cannot draw the script. There is no translation here
and no classifier to ask, so the honest answer is: refuse to send it, and say so
in the log row instead of claiming a delivery failure.

Two rules are pinned:
  1. the hard-evidence fast path does not qualify a headline that is not English,
     even when its Latin part states a mass-casualty toll;
  2. process_alert refuses the whole alert before ANY channel is touched, so
     ntfy, Telegram and SMTP cannot disagree about what was sent.
"""

import os
import sys
import unittest
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import breaking_monitor as bm  # noqa: E402

ENGLISH_TOLL = ("At least 139 migrants dead after boat capsized in "
                "Bay of Bengal: Official")
MIXED_TOLL = ("At least 139 migrants dead after boat capsized "
              "\u092a\u0930\u094d\u092f\u091f\u0928\u092e\u0928\u094d\u0924\u094d")
DEVANAGARI_TITLE = ("\u092a\u0930\u094d\u092f\u091f\u0928\u092e\u0928\u094d\u0924\u094d"
                    "\u094d\u0930\u0940 \u092a\u094c\u0921\u0947\u0932")


def candidate(title):
    return {"title": title, "url": "https://example-news-a.com/x",
            "source": "example-news-a.com", "snippet": title}


class FastPathRefusesNonEnglish(unittest.TestCase):
    def test_a_mixed_headline_that_states_a_toll_is_refused(self):
        verdict, why = bm.hard_evidence_fast_path([candidate(MIXED_TOLL)])
        self.assertIsNone(
            verdict,
            "a Devanagari headline took the fast path because its Latin half "
            "stated a toll (%r)" % (why,))

    def test_an_english_toll_still_fires(self):
        verdict, why = bm.hard_evidence_fast_path([candidate(ENGLISH_TOLL)])
        self.assertIsNotNone(verdict, "the gate must not disarm the fast path")
        self.assertTrue(why.startswith("fast_path:"), why)


class AlertIsHeldBeforeAnyChannel(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.originals = (bm.notify_ntfy, bm.send_email, bm.notify_telegram)

        def spy(name):
            def record(*_a, **_k):
                self.calls.append(name)
                return None
            return record

        bm.notify_ntfy = spy("ntfy")
        bm.send_email = spy("email")
        bm.notify_telegram = spy("telegram")

    def tearDown(self):
        bm.notify_ntfy, bm.send_email, bm.notify_telegram = self.originals

    def test_a_devanagari_headline_is_never_pushed(self):
        verdict = {"level": 3, "confidence": "HIGH",
                   "title": DEVANAGARI_TITLE, "summary": "",
                   "why_urgent": "a stated toll", "sources": [],
                   "category_suggestion": "",
                   "hard_evidence_fast_path": True}
        sent, _level, _confidence, _eid, reason = bm.process_alert(
            verdict, [], {"stories": []}, {}, SimpleNamespace(dry_run=False))
        self.assertFalse(sent)
        self.assertTrue(reason.startswith("held_non_english"), reason)
        self.assertEqual([], self.calls,
                         "a non-English headline reached a delivery channel")


if __name__ == "__main__":
    unittest.main()
