#!/usr/bin/env python3
"""tests/academic/test_english_push.py — the academic pushes are English-only.

Run:
    python -m unittest discover -s tests/academic -t . -v

WHY THIS EXISTS
---------------
The exam-notice push built its TITLE from the publisher's notice title, and those
titles are Devanagari. A Devanagari title in a push is the same defect as the
2026-11-03 digest page: it renders as nothing on a client with no Devanagari
face. The notice's own wording is still kept in the payload as data, because the
page and the PDF are the place to read it.

Decision recorded here: the push omits Devanagari entirely — title AND body.
A body that carries the Nepali title is still a push carrying text we cannot
guarantee renders, and the notice link is one tap away.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import check_academic_result as car  # noqa: E402
import english_text as et  # noqa: E402

DEVANAGARI_TITLE = ("\u092a\u0930\u094d\u092f\u091f\u0928\u092e\u0928\u094d\u0924\u094d"
                    "\u094d\u0930\u0940 \u092a\u094c\u0921\u0947\u0932")
NOTICE = {
    "id": "n-1",
    "title": DEVANAGARI_TITLE,
    "url": "https://tu.edu.np/notice/1",
    "pdf_url": "https://tu.edu.np/notice/1.pdf",
}
STATE = {"roll_number": "123"}


class ExamNoticePush(unittest.TestCase):
    def setUp(self):
        self.payload = car._build_exam_notice_payload(STATE, 3, NOTICE,
                                                      "2026-11-03")

    def test_push_title_is_english(self):
        title = self.payload["ntfy"]["title"]
        self.assertTrue(et.looks_english(title), title)

    def test_push_message_is_english(self):
        message = self.payload["ntfy"]["message"]
        self.assertTrue(et.looks_english(message), message)

    def test_the_notice_title_is_still_kept_as_data(self):
        self.assertEqual(DEVANAGARI_TITLE, self.payload["title"],
                         "the verbatim notice title is evidence, not display")


class ResultPush(unittest.TestCase):
    def test_result_push_is_english(self):
        payload = car._build_payload(STATE, 3, NOTICE, True, "2026-11-03")
        self.assertTrue(et.looks_english(payload["ntfy"]["title"]),
                        payload["ntfy"]["title"])
        self.assertTrue(et.looks_english(payload["ntfy"]["message"]),
                        payload["ntfy"]["message"])


if __name__ == "__main__":
    unittest.main()
