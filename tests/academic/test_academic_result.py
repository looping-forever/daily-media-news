#!/usr/bin/env python3
"""tests/academic/test_academic_result.py — offline tests for
scripts/check_academic_result.py.

Uses unittest (pytest is not installed). Every network touch is removed by
injecting a fake fetch_fn; PDF extraction is mocked so tests run offline.

Run:
    py tests\\academic\\test_academic_result.py
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import check_academic_result as car  # noqa: E402
import devanagari as dv  # noqa: E402

# real_pdf_builder lives BESIDE this file, but unittest discovery loads this
# module as tests.academic.test_academic_result, and a plain `from
# real_pdf_builder import ...` only works when the tests/academic DIRECTORY is
# on sys.path. Import it by package path instead, with a sys.path fallback for
# the direct-invocation case (py tests\academic\test_academic_result.py).
try:
    from tests.academic.real_pdf_builder import build_one_page_pdf
except ImportError:  # pragma: no cover - direct invocation
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from real_pdf_builder import build_one_page_pdf

ROLL = "13572468"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def seed_state():
    """A fresh state dict mirroring the seed file (schema_version 2)."""
    return {
        "schema_version": 2,
        "roll_number": ROLL,
        "current_semester": 4,
        "last_examined_notice_id": None,
        "examined": {},
        "pending_since": {},
        "hosts_down": {},
        "pending_overflow": False,
        "last_poll_time": None,
        "semesters": {
            "1": {"status": "passed", "result_announced": True,
                  "roll_found": True, "result_date": "2025-08-15",
                  "notice_id": None, "pdf_url": None},
            "2": {"status": "passed", "result_announced": True,
                  "roll_found": True, "result_date": "2025-12-20",
                  "notice_id": None, "pdf_url": None},
            "3": {"status": "passed", "result_announced": True,
                  "roll_found": True, "result_date": "2026-05-10",
                  "notice_id": None, "pdf_url": None},
            "4": {"status": "pending", "result_announced": False,
                  "roll_found": False, "result_date": None,
                  "notice_id": None, "pdf_url": None},
        },
    }


def run_monitor(state_path, poll_url="https://iost.tu.edu.np/notices?notice_type=21",
                dry_run=True, fetch_fn=None, **kw):
    """run() with the exam-notice pass off unless a test asks for it, so the
    result-path tests never touch the second feed."""
    kw.setdefault("run_exam_notices", False)
    return car.run(state_path, poll_url, dry_run=dry_run, fetch_fn=fetch_fn, **kw)


def notice_list_html(rows):
    """Build a realistic IOST notice-list HTML snippet.

    rows: list of (id, title, date). Each becomes a card with an anchor to
    /notices/<id>."""
    parts = ["<!DOCTYPE html><html><head><title>Notices</title></head><body>"]
    for nid, title, date in rows:
        parts.append(
            '<div class="notice-item">'
            '<span class="date">%s</span>'
            '<a href="/notices/%d">%s</a>'
            "</div>" % (date, nid, title))
    parts.append("</body></html>")
    return "".join(parts)


def make_fetch(responses):
    """responses: {url_substring: (status, bytes)}. Unmatched URLs raise."""
    def fetch_fn(url, timeout=15):
        for key, (status, body) in responses.items():
            if key in url:
                return status, body
        raise RuntimeError("no fake response for %s" % url)
    return fetch_fn


def fake_pdf_bytes():
    return b"%PDF-1.4 fake pdf bytes"


# --- The REAL IOST detail-page shape ---------------------------------------
# Transcribed from the live https://iost.tu.edu.np/notices/15673 (the
# B.Sc.CSIT I Semester-2082 Exam Result, 2026-09-28) on 2026-09-29. Every
# other academic fixture in this file fabricates a <title> containing the notice
# title; that is the gap that let 162 green tests hide a burned result.
#
# The three things that matter and that the fabricated fixtures all got wrong:
#   1. <title> is the SITE-WIDE CONSTANT, not the notice title.
#   2. The real title is in the BODY, in .inner-banner-detail and again in
#      .detail-page-inner, plus a <p> in the .ck-table editor block.
#   3. There is a THIRD <h4 class="title"> on the page reading "More Notices" -
#      a section heading that belongs to no notice. A page-wide h4.title scan
#      would return that instead, which is a different silent burn.

SITE_CONSTANT = "INSTITUTE OF SCIENCE AND TECHNOLOGY"


def iost_detail_html(real_title, pdf_url=None, include_body_title=True,
                     include_title_tag=True, extra_heading="More Notices"):
    """A detail page in the REAL IOST shape, not the convenient one."""
    body_title = ""
    if include_body_title:
        body_title = (
            '<div class="inner-banner"><div class="container">'
            '<div class="inner-banner-wrapper">'
            '<div class="inner-banner-detail">'
            '<h4 class="title">%s</h4>'
            '<nav aria-label="breadcrumb"><ol class="breadcrumb">'
            '<li class="breadcrumb-item"><a href="/">Home</a></li>'
            '<li class="breadcrumb-item active">Notices</li>'
            '</ol></nav>'
            '</div></div></div></div>'
            '<section class="detail-page"><div class="container"><div class="row">'
            '<div class="detail-page-inner">'
            '<h4 class="title">%s</h4>'
            '<div class="badge date"><span class="nep_date">2026-09-28</span></div>'
            '<hr />'
            '<div class="ck-table"><p>%s</p></div>'
            '</div>' % (real_title, real_title, real_title)
        )
    if extra_heading:
        body_title += ('<h4 class="title">%s</h4>' % extra_heading)
    body_title += '<section class="inner-downloads"></section></section></div></div>'
    pdf_link = ('<table class="table"><tr><td>'
                '<a href="%s">Download</a></td></tr></table>' % pdf_url) \
        if pdf_url else ""
    head = "<title>%s</title>" % SITE_CONSTANT if include_title_tag else ""
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">%s</head>'
            '<body><header class="header"></header>%s%s</body></html>'
            % (head, body_title, pdf_link))


# The real sem-1 2082 PDF's shape: the roll is printed SPLIT ("1357 2468")
# between two result columns, next to other rolls. Every roll here is
# fabricated; only the layout is copied from the actual sheet.
SEM1_PDF_TEXT = (
    "TU B.Sc.CSIT I Semester 2082 Result  Example College 90011492 90011493 "
    "90011495 90011496 9001 1500 9001 1508 90011241 90011374 9001 1392 "
    "1357 2468 9001 1507 9001 1516 29377 t078 90011502 9001 1505 "
) * 40  # repeated so digit density clears MIN_SHEET_DIGITS, as a real sheet is


def unreadable_pdf_text():
    """Non-empty text from a PDF whose custom-encoded font pypdf cannot map.

    Real failure mode: the extractor returns prose-shaped garbage with almost
    no digits. Non-empty (so the old empty-text guard did not fire) and
    unreadable (so a FAILED verdict is unsupportable).
    """
    return ("lqsfl ;q;_ ,xls ;y ;`_k ,ulf ;yfg ;kyf nsfl ,s ;kzsgsf ;ksTnsf "
            "ksTfklg ;ao ;Tkso ;ksTnsf ;fuf ;aTksTfklg nfav lsnkf ;ao fyf "
            "nsl ;Tkso ;ay ;dksTnsf ;ay ;Tkfnsf ldg ;do ;Ykso ldg ;ao ;ae: ") * 30


class AcademicClickHostTests(unittest.TestCase):
    """The result-page link in the ntfy payload must be the Pages ORIGIN.

    The academic result notification carries the owner's ROLL NUMBER, so its
    click target is the most sensitive link in the repo. It defaulted to the
    `digest-auth` Worker, which proxied to the same Pages origin and demanded
    a second Basic-auth identity (`digest`) before the origin asked for the
    owner's email - two prompts, two usernames, no way in. That Worker was
    deleted on 2026-10-03; `functions/_middleware.js` at the origin is now
    the only access control.
    """

    ORIGIN = "https://daily-digest.example.pages.dev"

    def test_default_worker_base_is_the_pages_origin(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACADEMIC_WORKER_BASE", None)
            self.assertEqual(self.ORIGIN, car._worker_base())

    def test_default_worker_base_names_no_deleted_worker(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACADEMIC_WORKER_BASE", None)
            base = car._worker_base()
        for bad in ("workers.dev", "digest-auth"):
            self.assertNotIn(bad, base, base)

    def test_override_still_wins(self):
        """The documented override must keep working."""
        with mock.patch.dict(os.environ,
                             {"ACADEMIC_WORKER_BASE": "https://example.test"}):
            self.assertEqual("https://example.test", car._worker_base())


class AcademicResultTest(unittest.TestCase):

    # ------------------------------------------------------------------
    # parse_notice_list
    # ------------------------------------------------------------------
    def test_parse_notice_list_extracts_cards(self):
        html = notice_list_html([
            (14559, "TU B.Sc.CSIT IV Semester-2081 Result Published",
             "2026-09-03"),
            (14560, "TU B.Sc.CSIT V Semester Back Result", "2026-09-04"),
        ])
        notices = car.parse_notice_list(html)
        self.assertEqual(len(notices), 2)
        self.assertEqual(notices[0]["id"], 14559)
        self.assertIn("IV Semester", notices[0]["title"])
        self.assertEqual(notices[0]["date"], "2026-09-03")
        self.assertIn("/notices/14559", notices[0]["url"])
        self.assertIsNone(notices[0]["pdf_url"])

    def test_parse_notice_list_dedups_and_sorts(self):
        html = notice_list_html([
            (14560, "Second", "2026-09-04"),
            (14559, "First", "2026-09-03"),
            (14560, "Duplicate", "2026-09-04"),
        ])
        notices = car.parse_notice_list(html)
        ids = [n["id"] for n in notices]
        self.assertEqual(ids, [14559, 14560])

    def test_parse_notice_list_empty(self):
        self.assertEqual(car.parse_notice_list(""), [])
        self.assertEqual(car.parse_notice_list("<html></html>"), [])

    # ------------------------------------------------------------------
    # match_semester
    # ------------------------------------------------------------------
    def test_match_semester_roman(self):
        self.assertEqual(
            car.match_semester("TU B.Sc.CSIT IV Semester-2081 Exam Result"), 4)
        self.assertEqual(
            car.match_semester("B.Sc.CSIT V Semester Back Result"), 5)
        self.assertEqual(
            car.match_semester("B.Sc. CSIT VIII Semester Result"), 8)

    def test_match_semester_arabic(self):
        self.assertEqual(
            car.match_semester("B.Sc. CSIT 4th Semester Result"), 4)
        self.assertEqual(
            car.match_semester("B.Sc.CSIT 6th Semester Supplementary Result"), 6)

    def test_match_semester_unrelated(self):
        self.assertIsNone(car.match_semester("Admission open for B.Sc.CSIT"))
        self.assertIsNone(car.match_semester("TU exam routine published"))
        self.assertIsNone(car.match_semester(""))
        self.assertIsNone(car.match_semester("B.Sc.CSIT IV Semester form fill"))

    # ------------------------------------------------------------------
    # roll_in_text
    # ------------------------------------------------------------------
    def test_roll_in_text_standalone(self):
        self.assertTrue(car.roll_in_text("Roll 13572468 passed", ROLL))
        self.assertTrue(car.roll_in_text("13572468", ROLL))

    def test_roll_in_text_no_false_positive_longer(self):
        self.assertFalse(car.roll_in_text("135724680", ROLL))
        self.assertFalse(car.roll_in_text("roll 135724680 failed", ROLL))

    def test_roll_in_text_whitespace(self):
        self.assertTrue(car.roll_in_text("1357 2468", ROLL))
        self.assertTrue(car.roll_in_text("Roll 1357 2468 passed", ROLL))

    def test_roll_in_text_empty(self):
        self.assertFalse(car.roll_in_text("", ROLL))
        self.assertFalse(car.roll_in_text("no roll here", ROLL))

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def test_pending_to_passed_advances(self):
        state = seed_state()
        notice = {"id": 14559, "title": "TU B.Sc.CSIT IV Semester Result",
                  "url": "https://iost.tu.edu.np/notices/14559",
                  "pdf_url": "https://portal.tu.edu.np/notice/9300/x.pdf"}
        with mock.patch.object(car, "extract_pdf_text",
                               return_value="Roll %s passed" % ROLL):
            payload = car.process_notice(state, notice, fake_pdf_bytes())
        self.assertIsNotNone(payload)
        self.assertTrue(payload["passed"])
        self.assertEqual(state["semesters"]["4"]["status"], "passed")
        self.assertTrue(state["semesters"]["4"]["roll_found"])
        self.assertEqual(state["current_semester"], 5)
        self.assertEqual(state["semesters"]["4"]["notice_id"], 14559)

    def test_pending_to_failed_advances_keeps_checking(self):
        state = seed_state()
        notice = {"id": 14559, "title": "TU B.Sc.CSIT IV Semester Result",
                  "url": "https://iost.tu.edu.np/notices/14559",
                  "pdf_url": "https://portal.tu.edu.np/notice/9300/x.pdf"}
        with mock.patch.object(car, "extract_pdf_text",
                               return_value="Roll 99999999 not here"):
            payload = car.process_notice(state, notice, fake_pdf_bytes())
        self.assertIsNotNone(payload)
        self.assertFalse(payload["passed"])
        self.assertEqual(state["semesters"]["4"]["status"], "failed")
        self.assertFalse(state["semesters"]["4"]["roll_found"])
        self.assertEqual(state["current_semester"], 5)

    def test_failed_to_passed_later_reexam(self):
        state = seed_state()
        state["semesters"]["4"]["status"] = "failed"
        state["semesters"]["4"]["result_announced"] = True
        state["semesters"]["4"]["roll_found"] = False
        state["current_semester"] = 5
        notice = {"id": 14600, "title": "TU B.Sc.CSIT IV Semester Back Result",
                  "url": "https://iost.tu.edu.np/notices/14600",
                  "pdf_url": "https://portal.tu.edu.np/notice/9300/y.pdf"}
        with mock.patch.object(car, "extract_pdf_text",
                               return_value="Roll %s passed" % ROLL):
            payload = car.process_notice(state, notice, fake_pdf_bytes())
        self.assertIsNotNone(payload)
        self.assertTrue(payload["passed"])
        self.assertEqual(state["semesters"]["4"]["status"], "passed")
        self.assertTrue(state["semesters"]["4"]["roll_found"])
        # current_semester is already 5; a re-exam of sem 4 must NOT advance it.
        self.assertEqual(state["current_semester"], 5)

    def test_passed_is_terminal(self):
        state = seed_state()
        state["semesters"]["4"]["status"] = "passed"
        notice = {"id": 14559, "title": "TU B.Sc.CSIT IV Semester Result",
                  "url": "https://iost.tu.edu.np/notices/14559",
                  "pdf_url": "https://portal.tu.edu.np/notice/9300/x.pdf"}
        with mock.patch.object(car, "extract_pdf_text",
                               return_value="Roll %s passed" % ROLL):
            payload = car.process_notice(state, notice, fake_pdf_bytes())
        self.assertIsNone(payload)

    def test_unrelated_notice_returns_none(self):
        state = seed_state()
        notice = {"id": 14559, "title": "Admission open",
                  "url": "https://iost.tu.edu.np/notices/14559",
                  "pdf_url": None}
        payload = car.process_notice(state, notice, fake_pdf_bytes())
        self.assertIsNone(payload)
        self.assertEqual(state["semesters"]["4"]["status"], "pending")

    # ------------------------------------------------------------------
    # Extraction failure does NOT mark failed
    # ------------------------------------------------------------------
    def test_extraction_failure_does_not_mark_failed(self):
        state = seed_state()
        notice = {"id": 14559, "title": "TU B.Sc.CSIT IV Semester Result",
                  "url": "https://iost.tu.edu.np/notices/14559",
                  "pdf_url": "https://portal.tu.edu.np/notice/9300/x.pdf"}
        with mock.patch.object(car, "extract_pdf_text", return_value=""):
            payload = car.process_notice(state, notice, fake_pdf_bytes())
        self.assertIsNone(payload)
        self.assertEqual(state["semesters"]["4"]["status"], "pending")
        self.assertFalse(state["semesters"]["4"]["result_announced"])
        self.assertEqual(state["current_semester"], 4)

    # ------------------------------------------------------------------
    # load_state / save_state round-trip
    # ------------------------------------------------------------------
    def test_load_save_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            car.save_state(path, seed_state())
            loaded = car.load_state(path)
            self.assertEqual(loaded["roll_number"], ROLL)
            self.assertEqual(loaded["current_semester"], 4)
            self.assertEqual(loaded["semesters"]["4"]["status"], "pending")

    def test_load_state_corrupt_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(car.StateError):
                car.load_state(path)

    def test_load_state_bad_schema_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 99}, f)
            with self.assertRaises(car.StateError):
                car.load_state(path)

    # ------------------------------------------------------------------
    # run() end-to-end
    # ------------------------------------------------------------------
    def test_run_end_to_end_pass(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview_path = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14559, "TU B.Sc.CSIT IV Semester-2081 Result Published",
                 "2026-09-03"),
            ])
            detail_html = (
                '<html><head><title>TU B.Sc.CSIT IV Semester Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/1758455673.pdf">'
                'Result</a></body></html>'
            )
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode("utf-8")),
                "/notices/14559": (200, detail_html.encode("utf-8")),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL), \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview_path):
                rc = run_monitor(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                             dry_run=True, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["semesters"]["4"]["status"], "passed")
            self.assertEqual(state["current_semester"], 5)
            self.assertEqual(state["last_examined_notice_id"], 14559)
            # dry-run wrote the preview file
            self.assertTrue(os.path.exists(preview_path))

    def test_run_no_result_noop(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14559, "Admission open for B.Sc.CSIT", "2026-09-03"),
            ])
            # The index title is NOT authoritative, so the detail page is
            # fetched and ITS title decides (P1a).
            detail_html = ("<html><head><title>Admission open for B.Sc.CSIT"
                           "</title></head><body>no pdf</body></html>")
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/14559": (200, detail_html.encode()),
            })
            rc = run_monitor(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["semesters"]["4"]["status"], "pending")
            self.assertEqual(state["current_semester"], 4)
            self.assertEqual(state["last_examined_notice_id"], 14559)
            self.assertEqual(state["examined"]["14559"]["outcome"], "irrelevant")
            self.assertEqual(state["examined"]["14559"]["reason"],
                             "no_semester_match")

    def test_run_network_failure_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())

            def failing_fetch(url, timeout=15):
                raise RuntimeError("network down")

            rc = run_monitor(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=failing_fetch)
            self.assertEqual(rc, 2)
            # state untouched
            state = car.load_state(state_path)
            self.assertEqual(state["semesters"]["4"]["status"], "pending")

    def test_run_corrupt_state_exit_3(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                f.write("{bad")
            rc = run_monitor(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=make_fetch({}))
            self.assertEqual(rc, 3)

    def test_run_pdf_download_failure_does_not_advance(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14559, "TU B.Sc.CSIT IV Semester-2081 Result Published",
                 "2026-09-03"),
            ])
            detail_html = (
                '<html><head><title>TU B.Sc.CSIT IV Semester Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/1758455673.pdf">'
                'Result</a></body></html>'
            )
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode("utf-8")),
                "/notices/14559": (200, detail_html.encode("utf-8")),
                # no portal.tu.edu.np response -> download raises
            })
            rc = run_monitor(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            # R1/R3: a PDF download failure DEFERS the id. The floor must not
            # move past it and the id must be retried next run.
            self.assertEqual(state["last_examined_notice_id"], 0)
            self.assertEqual(state["examined"]["14559"]["outcome"], "deferred")
            self.assertEqual(state["examined"]["14559"]["reason"], "pdf_missing")
            self.assertIn("14559", state["pending_since"])
            self.assertEqual(state["semesters"]["4"]["status"], "pending")

    def test_run_extraction_failure_does_not_advance(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14559, "TU B.Sc.CSIT IV Semester-2081 Result Published",
                 "2026-09-03"),
            ])
            detail_html = (
                '<html><head><title>TU B.Sc.CSIT IV Semester Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/1758455673.pdf">'
                'Result</a></body></html>'
            )
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode("utf-8")),
                "/notices/14559": (200, detail_html.encode("utf-8")),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text", return_value=""):
                rc = run_monitor(state_path,
                             "https://iost.tu.edu.np/notices?notice_type=21",
                             dry_run=True, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            # extraction failed -> UNKNOWN, never FAILED, and DEFERRED so the
            # id is retried instead of being burned.
            self.assertEqual(state["last_examined_notice_id"], 0)
            self.assertEqual(state["examined"]["14559"]["outcome"], "deferred")
            self.assertEqual(state["examined"]["14559"]["reason"],
                             "pdf_unextractable")
            self.assertEqual(state["semesters"]["4"]["status"], "pending")
            self.assertFalse(state["semesters"]["4"]["result_announced"])
            self.assertEqual(state["current_semester"], 4)

    # ------------------------------------------------------------------
    # Batch filter
    # ------------------------------------------------------------------
    def test_batch_filter_rejects_older_batch_pending_semester(self):
        """2079-batch notice must NOT match for a pending (not failed) sem."""
        state = seed_state()
        result = car.notice_matches_student(
            "B.Sc.CSIT IV Semester-2079 Exam Result", 4, state)
        self.assertFalse(result)

    def test_batch_filter_accepts_matching_batch(self):
        """2081-batch notice SHOULD match."""
        state = seed_state()
        result = car.notice_matches_student(
            "B.Sc.CSIT IV Semester-2081 Exam Result", 4, state)
        self.assertTrue(result)

    def test_batch_filter_accepts_ambiguous_no_batch(self):
        """Notice with no batch year SHOULD match (ambiguous)."""
        state = seed_state()
        result = car.notice_matches_student(
            "B.Sc.CSIT IV Semester Exam Result", 4, state)
        self.assertTrue(result)

    def test_batch_filter_accepts_older_batch_failed_semester(self):
        """Older-batch notice for a FAILED semester = re-exam → accept."""
        state = seed_state()
        state["semesters"]["1"]["status"] = "failed"
        state["semesters"]["1"]["result_announced"] = True
        state["semesters"]["1"]["roll_found"] = False
        result = car.notice_matches_student(
            "B.Sc.CSIT I Semester-2082 Back Result", 1, state)
        self.assertTrue(result)

    def test_batch_filter_accepts_older_batch_passed_semester(self):
        """Older-batch notice for an already-PASSED semester → rejected
        (already terminal, but batch filter should also reject)."""
        state = seed_state()
        state["semesters"]["3"]["status"] = "passed"
        result = car.notice_matches_student(
            "B.Sc.CSIT III Semester-2079 Exam Result", 3, state)
        self.assertFalse(result)

    def test_batch_filter_accepts_newer_batch(self):
        """Newer-batch notice (e.g. 2082) SHOULD match (future batch)."""
        state = seed_state()
        result = car.notice_matches_student(
            "B.Sc.CSIT IV Semester-2082 Exam Result", 4, state)
        self.assertTrue(result)

    def test_batch_filter_run_rejects_wrong_batch(self):
        """End-to-end: run() must skip a 2079-batch notice for pending sem."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14559, "B.Sc.CSIT IV Semester-2079 Exam Result",
                 "2026-09-03"),
            ])
            detail_html = (
                '<html><head><title>B.Sc.CSIT IV Semester-2079 Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/x.pdf">'
                'Result</a></body></html>'
            )
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode("utf-8")),
                "/notices/14559": (200, detail_html.encode("utf-8")),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL):
                rc = run_monitor(state_path,
                             "https://iost.tu.edu.np/notices?notice_type=21",
                             dry_run=True, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            # Must NOT have processed the wrong-batch notice
            self.assertEqual(state["semesters"]["4"]["status"], "pending")
            self.assertEqual(state["current_semester"], 4)
            self.assertEqual(state["last_examined_notice_id"], 14559)

    def test_batch_filter_run_accepts_correct_batch(self):
        """End-to-end: run() must process a 2081-batch notice."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview_path = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14559, "TU B.Sc.CSIT IV Semester-2081 Result Published",
                 "2026-09-03"),
            ])
            detail_html = (
                '<html><head><title>TU B.Sc.CSIT IV Semester-2081 Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/1758455673.pdf">'
                'Result</a></body></html>'
            )
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode("utf-8")),
                "/notices/14559": (200, detail_html.encode("utf-8")),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL), \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview_path):
                rc = run_monitor(state_path,
                             "https://iost.tu.edu.np/notices?notice_type=21",
                             dry_run=True, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["semesters"]["4"]["status"], "passed")
            self.assertEqual(state["current_semester"], 5)


class NoBurnContractTest(unittest.TestCase):
    """Contract D1/D2/D4: no notice can ever be permanently burned without
    having been authoritatively read."""

    # ---------------- D4: advance() is a pure function --------------------
    def test_advance_all_decided(self):
        examined = {"1": {"outcome": "irrelevant"}, "2": {"outcome": "irrelevant"},
                    "4": {"outcome": "processed"}}
        self.assertEqual(car.advance(0, examined, [1, 2, 4]), 4)

    def test_advance_over_a_gap_in_the_list(self):
        """3 is absent from the list, so R5 permits advancing to 4."""
        examined = {"1": {"outcome": "irrelevant"}, "2": {"outcome": "irrelevant"}}
        self.assertEqual(car.advance(0, examined, [1, 2, 4]), 4)

    def test_advance_stops_at_a_deferred_notice(self):
        examined = {"1": {"outcome": "irrelevant"}, "2": {"outcome": "irrelevant"},
                    "3": {"outcome": "deferred"}}
        self.assertEqual(car.advance(0, examined, [1, 2, 3, 4]), 2)
        # and 3 is still undecided, so the next run retries it
        self.assertNotIn(3, car._decided_ids(examined))

    def test_advance_stops_at_a_dispatch_failure(self):
        examined = {"1": {"outcome": "irrelevant"},
                    "2": {"outcome": "deferred", "reason": "dispatch_failed"}}
        self.assertEqual(car.advance(1, examined, [1, 2]), 1)
        self.assertEqual(car.advance(1, examined, [1, 2, 3]), 1)

    def test_advance_is_never_max_of_list(self):
        """The historical bug was floor = max(ids seen), UNCONDITIONALLY.

        R5's literal wording is deliberately permissive about the candidate
        itself (D4 case 2 requires exactly that: examined {1,2} + list [1,2,4]
        -> 4), so advance() alone will step onto the next listed id. Safety
        comes from R2's cap in run(): the floor never passes the last id the
        run actually attempted. Both halves are asserted here.
        """
        examined = {"1": {"outcome": "irrelevant"}}
        # steps to 2, then STOPS — 3..5 are blocked by 2 being undecided
        self.assertEqual(car.advance(1, examined, [1, 2, 3, 4, 5]), 2)
        self.assertNotEqual(car.advance(1, examined, [1, 2, 3, 4, 5]), 5)
        # a deferred id in the middle stops it dead
        examined = {"1": {"outcome": "irrelevant"}, "2": {"outcome": "deferred"}}
        self.assertEqual(car.advance(1, examined, [1, 2, 3, 4, 5]), 1)

    def test_r2_cap_holds_the_floor_at_the_last_processed_id(self):
        """R2: 'advance nothing past the last PROCESSED id'. 1..3 decided, 4..9
        listed but never processed -> the floor must stop at 3, not 9."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            state = seed_state()
            state["last_examined_notice_id"] = 0
            car.save_state(state_path, state)
            rows = [(i, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-01")
                    for i in range(1, 10)]
            list_html = notice_list_html(rows)
            # the PDF download fails on the FIRST result notice -> the run stops
            # there (R3), so 4..9 are never touched
            detail_html = ("<html><head><title>TU B.Sc.CSIT IV Semester Result"
                           "</title></head><body>no pdf</body></html>")
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/1": (200, detail_html.encode()),
            })
            rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            after = car.load_state(state_path)
            self.assertEqual(after["last_examined_notice_id"], 0)
            self.assertEqual(after["examined"]["1"]["outcome"], "deferred")
            for untouched in ("2", "3", "4", "9"):
                self.assertNotIn(untouched, after["examined"])

    def test_deferred_is_not_a_decision(self):
        self.assertEqual(car._decided_ids({"7": {"outcome": "deferred"}}), set())
        self.assertEqual(car._decided_ids({"7": {"outcome": "processed"}}), {7})

    # ---------------- R1: a degraded title is deferred, never burned -----
    def test_degraded_list_title_is_recovered_from_the_detail_page(self):
        """The original burn: parse_notice_list fabricated 'notice-<id>' and the
        caller decided 'irrelevant' against that placeholder. Now the degraded
        index title routes to the detail page, whose title is authoritative."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            # an anchor with NO inner text -> title_degraded True
            list_html = ('<!DOCTYPE html><html><body>'
                         '<div><a href="/notices/14560"></a></div>'
                         '</body></html>')
            detail_html = (
                '<html><head><title>TU B.Sc.CSIT IV Semester-2081 Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/x.pdf">R</a>'
                '</body></html>')
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/14560": (200, detail_html.encode()),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL), \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["examined"]["14560"]["outcome"], "processed")
            self.assertEqual(state["semesters"]["4"]["status"], "passed")

    def test_degraded_list_title_with_unreachable_detail_is_deferred(self):
        """…and when the detail page cannot be read either, the id is DEFERRED
        (retried forever), never burned."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            list_html = ('<!DOCTYPE html><html><body>'
                         '<div><a href="/notices/14560"></a></div>'
                         '</body></html>')
            fetch = make_fetch({"notice_type=21": (200, list_html.encode())})
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["examined"]["14560"]["outcome"], "deferred")
            self.assertEqual(state["examined"]["14560"]["reason"],
                             "detail_fetch_failed")
            self.assertEqual(state["last_examined_notice_id"], 0)
            self.assertIn("14560", state["pending_since"])

    def test_missing_authoritative_detail_title_is_deferred(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14561, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-03")])
            detail_html = "<html><head><title></title></head><body></body></html>"
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/14561": (200, detail_html.encode()),
            })
            rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["examined"]["14561"]["outcome"], "deferred")
            self.assertEqual(state["last_examined_notice_id"], 0)

    def test_markerless_result_titled_notice_gets_a_detail_fetch(self):
        """'4th Semester Result' has no CSIT marker on the board, but the detail
        page reveals it is a CSIT result. It must NOT be burned from the list
        title."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14562, "4th Semester Result", "2026-09-03")])
            detail_html = (
                '<html><head><title>TU B.Sc.CSIT IV Semester Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/x.pdf">'
                'Result</a></body></html>')
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/14562": (200, detail_html.encode()),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL), \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["semesters"]["4"]["status"], "passed")
            self.assertEqual(state["examined"]["14562"]["outcome"], "processed")

    # ---------------- R3: the four failure breaks still refuse to advance --
    def test_detail_fetch_failure_breaks_and_defers(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14563, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-03")])
            # no detail response -> fetch raises
            fetch = make_fetch({"notice_type=21": (200, list_html.encode())})
            rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["examined"]["14563"]["outcome"], "deferred")
            self.assertEqual(state["examined"]["14563"]["reason"],
                             "detail_fetch_failed")
            self.assertEqual(state["last_examined_notice_id"], 0)
            # a host-down entry is recorded so the watchdog can escalate
            self.assertIn("iost.tu.edu.np", state["hosts_down"])

    def test_no_pdf_link_breaks_and_defers(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14564, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-03")])
            detail_html = ("<html><head><title>TU B.Sc.CSIT IV Semester Result"
                           "</title></head><body>no pdf here</body></html>")
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/14564": (200, detail_html.encode()),
            })
            rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["examined"]["14564"]["reason"], "pdf_missing")
            self.assertEqual(state["last_examined_notice_id"], 0)

    def test_failure_at_2_never_advances_past_it(self):
        """R3: 1 is decided, 2 fails -> floor 1, and 3 is never touched."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (1, "Admission open for B.Sc.CSIT", "2026-09-01"),
                (2, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-02"),
                (3, "TU B.Sc.CSIT IV Semester-2081 Result (revised)",
                 "2026-09-03"),
            ])
            detail1 = ("<html><head><title>Admission open for B.Sc.CSIT"
                       "</title></head><body>x</body></html>")
            detail2 = ("<html><head><title>TU B.Sc.CSIT IV Semester Result"
                       "</title></head><body>no pdf</body></html>")
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/1": (200, detail1.encode()),
                "/notices/2": (200, detail2.encode()),
            })
            rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            self.assertEqual(state["last_examined_notice_id"], 1)
            self.assertEqual(state["examined"]["1"]["outcome"], "irrelevant")
            self.assertEqual(state["examined"]["2"]["outcome"], "deferred")
            self.assertNotIn("3", state["examined"])

    # ---------------- idempotency: two runs -> exactly one payload --------
    def test_two_runs_emit_exactly_one_payload(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            list_html = notice_list_html([
                (14570, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-03")])
            detail_html = (
                '<html><head><title>TU B.Sc.CSIT IV Semester Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/x.pdf">R</a>'
                '</body></html>')
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/14570": (200, detail_html.encode()),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL), \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc1 = run_monitor(state_path, fetch_fn=fetch)
                rc2 = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual((rc1, rc2), (0, 0))
            with open(preview, encoding="utf-8") as fh:
                payloads = json.load(fh)
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["notice_id"], 14570)
            state = car.load_state(state_path)
            self.assertEqual(state["last_examined_notice_id"], 14570)

    # ---------------- D2 migration + replay no-re-notify ------------------
    def test_schema1_migrates_to_2_with_a_replay_window(self):
        legacy = {
            "schema_version": 1, "roll_number": ROLL, "current_semester": 4,
            "last_checked_notice_id": 15236, "last_poll_time": "2026-09-10",
            "semesters": seed_state()["semesters"],
        }
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(legacy, fh)
            meta = {}
            state = car.load_state(path, meta=meta)
            self.assertTrue(meta["migrated"])
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["examined"], {})
            self.assertEqual(state["last_examined_notice_id"],
                             15236 - car.REPLAY_WINDOW)
            self.assertNotIn("last_checked_notice_id", state)

    def test_schema1_migration_is_written_back_as_2(self):
        legacy = {
            "schema_version": 1, "roll_number": ROLL, "current_semester": 4,
            "last_checked_notice_id": 15236, "last_poll_time": "2026-09-10",
            "semesters": seed_state()["semesters"],
        }
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(legacy, fh)
            list_html = notice_list_html([
                (15240, "Admission open for B.Sc.CSIT", "2026-09-27")])
            fetch = make_fetch({"notice_type=21": (200, list_html.encode())})
            rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            with open(state_path, encoding="utf-8") as fh:
                written = json.load(fh)
            self.assertEqual(written["schema_version"], 2)
            self.assertNotIn("last_checked_notice_id", written)

    def test_replayed_already_announced_sem3_emits_zero_payloads(self):
        """The one hole the replay window opens: a re-read of the sem-3 result
        must never re-notify."""
        state = seed_state()
        state["schema_version"] = 1
        state["last_checked_notice_id"] = 15236
        state["last_examined_notice_id"] = None
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, state)
            list_html = notice_list_html([
                (15234, "B.Sc.CSIT III Semester-2080 Result", "2026-08-18")])
            detail_html = (
                '<html><head><title>B.Sc.CSIT III Semester Result</title>'
                '</head><body>'
                '<a href="https://portal.tu.edu.np/notice/9300/x.pdf">R</a>'
                '</body></html>')
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/15234": (200, detail_html.encode()),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL), \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(preview),
                             "replay must emit ZERO payloads")
            after = car.load_state(state_path)
            self.assertEqual(after["examined"]["15234"]["outcome"],
                             "already_announced")
            self.assertEqual(after["examined"]["15234"]["reason"],
                             "already_announced")
            self.assertEqual(after["semesters"]["3"]["status"], "passed")
            self.assertEqual(after["current_semester"], 4)

    def test_bad_schema_version_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 99}, f)
            with self.assertRaises(car.StateError):
                car.load_state(path)

    def test_schema3_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 3}, f)
            with self.assertRaises(car.StateError):
                car.load_state(path)

    # ---------------- exit 2 happens BEFORE save_state -------------------
    def test_fetch_failure_exits_2_without_rewriting_state(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            with open(state_path, encoding="utf-8") as fh:
                before = fh.read()

            def failing_fetch(url, timeout=15):
                raise RuntimeError("SERVFAIL")

            rc = run_monitor(state_path, fetch_fn=failing_fetch)
            self.assertEqual(rc, 2)
            with open(state_path, encoding="utf-8") as fh:
                after = fh.read()
            self.assertEqual(before, after,
                             "a total outage must not rewrite the state file")

    # ---------------- R2: the processing cap ------------------------------
    def test_pending_overflow_caps_and_blocks_the_floor(self):
        state = seed_state()
        state["last_examined_notice_id"] = 1000
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, state)
            rows = [(1000 + i, "B.Sc. CSIT notice %d" % (1000 + i), "2026-09-01")
                    for i in range(1, 80)]
            list_html = notice_list_html(rows)
            # every detail page is read (P1a) and none of them is a result, so
            # all 79 are cheaply and legitimately decided irrelevant
            detail = ("<html><head><title>Admission open for B.Sc.CSIT"
                      "</title></head><body>x</body></html>")
            responses = {"notice_type=21": (200, list_html.encode())}
            for nid, _t, _dt in rows:
                responses["/notices/%d" % nid] = (200, detail.encode())
            fetch = make_fetch(responses)
            rc = run_monitor(state_path, fetch_fn=fetch)
            self.assertEqual(rc, 0)
            after = car.load_state(state_path)
            self.assertTrue(after["pending_overflow"])
            self.assertEqual(len(after["examined"]), car.MAX_PENDING)
            self.assertEqual(after["last_examined_notice_id"],
                             1000 + car.MAX_PENDING)
            # ids 61..79 were NOT examined and remain pending for the next run
            self.assertNotIn(str(1000 + car.MAX_PENDING + 1), after["examined"])

    # ---------------- examined cap (R4) ------------------------------------
    def test_examined_map_is_capped_and_evicts_lowest_first(self):
        state = seed_state()
        state["examined"] = {str(i): {"outcome": "irrelevant", "semester": None,
                                     "reason": "no_semester_match",
                                     "at": "2026-09-01"}
                             for i in range(1, car.EXAMINED_CAP + 11)}
        car._evict_examined(state, "examined")
        self.assertEqual(len(state["examined"]), car.EXAMINED_CAP)
        self.assertNotIn("1", state["examined"])
        self.assertIn(str(car.EXAMINED_CAP + 10), state["examined"])


class ReviewerReproTest(unittest.TestCase):
    """The three scenarios an independent reviewer reproduced against the
    previous build. Each one is a permanent result burn, so each has a test
    that FAILS on the pre-fix code."""

    def _run_result(self, d, list_rows, detail_titles, pdf_text,
                    state=None, calls=None):
        """Drive one result notice through run() and return (payloads, state)."""
        state_path = os.path.join(d, "state.json")
        preview = os.path.join(d, "preview.json")
        car.save_state(state_path, state if state is not None else seed_state())
        list_html = notice_list_html(list_rows)
        nid = list_rows[0][0]
        detail_html = ('<html><head><title>%s</title></head><body>'
                       '<a href="https://portal.tu.edu.np/notice/9300/x.pdf">R'
                       '</a></body></html>' % detail_titles[nid])
        responses = {"notice_type=21": (200, list_html.encode()),
                     "/notices/%d" % nid: (200, detail_html.encode()),
                     "portal.tu.edu.np": (200, fake_pdf_bytes())}
        fetch = make_fetch(responses)
        if calls is not None:
            base_fetch = fetch

            def counting(url, timeout=15):
                calls.append(url)
                return base_fetch(url, timeout)
            fetch = counting
        with mock.patch.object(car, "extract_pdf_text",
                               return_value=pdf_text), \
             mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
            rc = car.run(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=fetch, run_exam_notices=False)
        payloads = []
        if os.path.exists(preview):
            with open(preview, encoding="utf-8") as fh:
                payloads = json.load(fh)
        return rc, payloads, car.load_state(state_path)

    # ---------------- REPRO 1: missing semester key ------------------------
    def test_repro_missing_semester_key_never_records_processed(self):
        """P0. A semesters map with no "4" entry, a real sem-4 result whose PDF
        was downloaded and whose roll WAS FOUND.

        Pre-fix: payloads=0, floor advanced to 101, and examined recorded
        ``processed``/``result_announced`` - a permanent burn with no error
        anywhere, on a result that was sitting in a downloaded PDF.

        The fix makes the missing key impossible: the entry is created, so the
        verdict becomes recordable and IS reported. `processed` is then
        truthful rather than a lie, because a payload really was emitted.
        """
        state = seed_state()
        del state["semesters"]["4"]
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after = self._run_result(
                d, [(101, "4th Semester Result", "2026-09-27")],
                {101: "TU B.Sc.CSIT IV Semester-2081 Result"},
                "Roll %s passed" % ROLL, state=state)
        self.assertEqual(rc, 0)
        # THE property that matters: the owner is told.
        self.assertEqual(len(payloads), 1,
                         "the roll WAS found in the PDF - the owner must be "
                         "notified, not silently dropped")
        self.assertTrue(payloads[0]["passed"])
        self.assertEqual(payloads[0]["notice_id"], 101)
        # ...and the verdict is recorded somewhere it can be found.
        self.assertEqual(after["semesters"]["4"]["status"], "passed")
        self.assertTrue(after["semesters"]["4"]["roll_found"])
        self.assertEqual(after["examined"]["101"]["outcome"], "processed")
        self.assertEqual(after["last_examined_notice_id"], 101)

    def test_unrecordable_verdict_defers_and_never_advances_the_floor(self):
        """The other half of P0: when a payload genuinely cannot be produced,
        the notice must be DEFERRED and the floor must not move.

        `status: passed` with `result_announced: false` is a self-contradictory
        state, so the run refuses to infer a verdict, says so, and retries.
        """
        state = seed_state()
        state["semesters"]["4"] = {"status": "passed",
                                   "result_announced": False,
                                   "roll_found": False, "result_date": None,
                                   "notice_id": None, "pdf_url": None}
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after = self._run_result(
                d, [(102, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-27")],
                {102: "TU B.Sc.CSIT IV Semester Result"},
                "Roll %s passed" % ROLL, state=state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(payloads), 0)
        entry = after["examined"]["102"]
        self.assertEqual(entry["outcome"], "deferred",
                         "a verdict that could not be reported must never be "
                         "recorded as processed/result_announced")
        self.assertEqual(entry["reason"], "inconsistent_state")
        self.assertEqual(after["last_examined_notice_id"], 0,
                         "the floor must not advance past a deferred notice")
        self.assertIn("102", after["pending_since"])

    def test_no_payload_reason_never_says_processed(self):
        for semester, sem, expected in (
                (4, None, ("deferred", "semester_not_tracked")),
                (4, {"status": "passed", "result_announced": False},
                 ("deferred", "inconsistent_state")),
                (4, {"status": "passed", "result_announced": True},
                 ("already_announced", "already_announced"))):
            state = seed_state()
            if sem is None:
                del state["semesters"][str(semester)]
            else:
                state["semesters"][str(semester)] = sem
            notice = {"id": 9, "title": "B.Sc.CSIT IV Semester Result"}
            self.assertEqual(car._no_payload_reason(state, notice, semester),
                             expected, semester)
            outcome, reason = car._no_payload_reason(state, notice, semester)
            self.assertNotEqual((outcome, reason),
                                ("processed", "result_announced"))

    def test_new_reason_tokens_are_in_the_frozen_vocabulary(self):
        for reason in ("semester_not_tracked", "inconsistent_state"):
            self.assertIn(reason, car.REASONS)
        for outcome in car.OUTCOMES:
            self.assertIn(outcome, car.OUTCOMES)
        # every reason a deferred outcome can carry is a declared token
        for reason in car.REASONS:
            self.assertIsInstance(reason, str)

    # ---------------- REPRO 2: truncated list title -----------------------
    def test_repro_truncated_list_title_is_not_decided_from_the_index(self):
        """P1. List title "B.Sc. CSIT Fourth Semester" (CSIT marker, NO result
        term), authoritative detail title "...Result Published".

        Pre-fix: the "provably irrelevant" pre-filter recorded `irrelevant`
        from the LIST title, floor advanced to 201, payloads=0, and the detail
        page was never fetched.
        """
        with tempfile.TemporaryDirectory() as d:
            calls = []
            rc, payloads, after = self._run_result(
                d, [(201, "B.Sc. CSIT Fourth Semester", "2026-09-27")],
                {201: "B.Sc. CSIT IV Semester Result Published"},
                "Roll %s passed" % ROLL, calls=calls)
        self.assertEqual(rc, 0)
        self.assertTrue(any("/notices/201" in u for u in calls),
                        "the detail page MUST be fetched: a board index title "
                        "is never authoritative")
        self.assertNotEqual(after["examined"]["201"]["outcome"], "irrelevant")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["passed"])

    def test_the_removed_prefilter_cannot_be_called(self):
        """The burn helper is a tombstone that raises, so nobody can quietly
        reintroduce a keyword-absence skip through it."""
        with self.assertRaises(NotImplementedError):
            car.looks_provably_irrelevant("B.Sc. CSIT Fourth Semester")

    def test_every_pending_notice_gets_a_detail_fetch(self):
        """No notice is ever decided from the index title, whatever it says."""
        with tempfile.TemporaryDirectory() as d:
            calls = []
            state = seed_state()
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, state)
            rows = [(300, "B.Sc. CSIT", "2026-09-01"),
                    (301, "Admission open", "2026-09-01"),
                    (302, "Library hours", "2026-09-01"),
                    (303, "Exam Routine 2082", "2026-09-01"),
                    (304, "", "2026-09-01")]
            list_html = notice_list_html(rows)
            detail = ("<html><head><title>B.Sc.CSIT II Semester Result</title>"
                      "</head><body>x</body></html>")
            responses = {"notice_type=21": (200, list_html.encode())}
            for nid, _t, _d in rows:
                responses["/notices/%d" % nid] = (200, detail.encode())
            base = make_fetch(responses)

            def counting(url, timeout=15):
                calls.append(url)
                return base(url, timeout)

            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH",
                                   os.path.join(d, "p.json")):
                car.run(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                        dry_run=True, fetch_fn=counting, run_exam_notices=False)
            for nid, _t, _d in rows:
                self.assertTrue(any("/notices/%d" % nid in u for u in calls),
                                "notice %d was never given a detail fetch" % nid)

    # ---------------- REPRO 3: replay of a failed+announced semester ------
    def test_repro_replaying_a_failed_announced_result_emits_no_payload(self):
        """P1. semesters["1"] = failed + result_announced, roll absent.

        Pre-fix: the replay emitted a SECOND "FAILED" ntfy for a verdict the
        owner was already told about.
        """
        state = seed_state()
        state["schema_version"] = 1
        state["last_checked_notice_id"] = 15236
        state["last_examined_notice_id"] = None
        state["semesters"]["1"] = {"status": "failed",
                                   "result_announced": True,
                                   "roll_found": False,
                                   "result_date": "2025-07-25",
                                   "notice_id": 15100, "pdf_url": None}
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after = self._run_result(
                d, [(15240, "B.Sc.CSIT I Semester-2081 Result", "2026-08-01")],
                {15240: "B.Sc.CSIT I Semester Result"},
                "Roll 99999999 not present", state=state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(payloads), 0,
                         "re-announcing an unchanged FAILED verdict is not news")
        self.assertEqual(after["examined"]["15240"]["outcome"],
                         "already_announced")
        self.assertEqual(after["semesters"]["1"]["status"], "failed")
        self.assertFalse(after["semesters"]["1"]["roll_found"])

    def test_a_real_back_result_for_a_failed_semester_still_notifies(self):
        """The reason the guard is NOT widened to "failed": rule (c) accepts
        back/re-exam notices for a failed semester on purpose, and that alert
        is the one the owner cannot miss."""
        state = seed_state()
        state["semesters"]["1"]["status"] = "failed"
        state["semesters"]["1"]["result_announced"] = True
        state["semesters"]["1"]["roll_found"] = False
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after = self._run_result(
                d, [(401, "B.Sc.CSIT I Semester-2082 Back Result", "2026-09-27")],
                {401: "B.Sc.CSIT I Semester Back Result"},
                "Roll %s passed" % ROLL, state=state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(payloads), 1,
                         "a back result that carries the roll MUST notify")
        self.assertTrue(payloads[0]["passed"])
        self.assertEqual(after["semesters"]["1"]["status"], "passed")
        self.assertTrue(after["semesters"]["1"]["roll_found"])


class P2EfficiencyTest(unittest.TestCase):
    def test_pdf_text_is_extracted_once_per_notice(self):
        """P2a. A scanned result PDF costs a full OCR pass; running it twice per
        notice doubled the OCR work inside a 3-minute step."""
        state = seed_state()
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, state)
            list_html = notice_list_html([
                (501, "TU B.Sc.CSIT IV Semester-2081 Result", "2026-09-03")])
            detail_html = ('<html><head><title>TU B.Sc.CSIT IV Semester Result'
                           '</title></head><body>'
                           '<a href="https://portal.tu.edu.np/n/9300/x.pdf">R'
                           '</a></body></html>')
            fetch = make_fetch({
                "notice_type=21": (200, list_html.encode()),
                "/notices/501": (200, detail_html.encode()),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            })
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value="Roll %s passed" % ROLL
                                   ) as spy, \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH",
                                   os.path.join(d, "p.json")):
                car.run(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                        dry_run=True, fetch_fn=fetch, run_exam_notices=False)
            self.assertEqual(spy.call_count, 1,
                             "extract_pdf_text ran %d times for one notice"
                             % spy.call_count)


class FakeClock:
    """Deterministic monotonic clock: every read advances by ``step`` seconds.

    The budget tests use this, never a real sleep, so they cannot be flaky and
    cannot take 360 seconds.
    """

    def __init__(self, step=1.0, start=0.0):
        self.now = float(start)
        self.step = float(step)

    def __call__(self):
        self.now += self.step
        return self.now

    @property
    def reads(self):
        return int(self.now / self.step)


def drive_result_pass(d, state, rows, detail_titles, pdf_texts, **run_kw):
    """Drive `rows` = [(id, list title, date)] through ONE run() result pass.

    detail_titles: {id: authoritative detail-page title}
    pdf_texts:     {id: what extract_pdf_text should return for that notice}
    run_kw:        forwarded to car.run (budget_s=, clock=, ...)

    The whole-board feed UNION is switched OFF here (``result_union_urls=()``)
    so this helper drives exactly one URL and the call-list assertions stay
    meaningful. The union has its own tests.

    Returns (rc, payloads, state_after, fetch_calls). Every notice gets its own
    detail page and its own fake PDF whose bytes carry the notice id, so one
    extract_pdf_text side_effect can serve the whole batch.
    """
    state_path = os.path.join(d, "state.json")
    preview = os.path.join(d, "preview.json")
    car.save_state(state_path, state)
    calls = []
    responses = {"notice_type=21": (200, notice_list_html(rows).encode())}
    for nid, _title, _date in rows:
        detail_html = ('<html><head><title>%s</title></head><body>'
                       '<a href="https://portal.tu.edu.np/pdf/%d/x.pdf">R</a>'
                       '</body></html>' % (detail_titles[nid], nid))
        responses["/notices/%d" % nid] = (200, detail_html.encode())
        responses["pdf/%d" % nid] = (200, b"%PDF-1.4 nid=" + str(nid).encode()
                                     + b" ")
    base = make_fetch(responses)

    def fetch(url, timeout=15):
        calls.append(url)
        return base(url, timeout)

    def fake_text(pdf_bytes):
        try:
            nid = int(pdf_bytes.split(b"nid=")[1].split(b" ")[0])
        except (IndexError, ValueError):
            return ""
        return pdf_texts.get(nid, "")

    with mock.patch.object(car, "extract_pdf_text", side_effect=fake_text), \
            mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
        rc = car.run(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                     dry_run=True, fetch_fn=fetch, run_exam_notices=False,
                     result_union_urls=(), **run_kw)
    payloads = []
    if os.path.exists(preview):
        with open(preview, encoding="utf-8") as fh:
            payloads = json.load(fh)
    return rc, payloads, car.load_state(state_path), calls


class ResultPassBudgetTest(unittest.TestCase):
    """R2b. MAX_PENDING bounds how many notices a run opens, not how long it may
    take. The pass must stop on its own budget and SAVE truthful state instead
    of being killed by the job's timeout-minutes before save_state."""

    ROWS = [(600 + i, "B.Sc. CSIT", "2026-09-27") for i in range(6)]
    TITLES = {600 + i: "TU B.Sc.CSIT IV Semester-2081 Result" for i in range(6)}

    def test_budget_exhaustion_defers_the_remainder_and_exits_zero(self):
        """The regression test. Pre-fix, 60 notices x a 15s timeout reached the
        job's own 900s cap and the run died before save_state."""
        texts = {nid: ("Roll %s passed" % ROLL if nid <= 602 else "no roll")
                 for nid, _t, _d in self.ROWS}
        clock = FakeClock(step=100.0)          # 4th read crosses the deadline
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES, texts,
                budget_s=360, clock=clock)
        # 1. it exits SUCCESSFULLY, having saved state: no exception, rc 0.
        self.assertEqual(rc, 0,
                         "a budget-exhausted run must still exit 0 so the job "
                         "records a successful run, not a killed one")
        # 2. the remainder is recorded `deferred`, so R1 retries it next run.
        for nid, _t, _d in self.ROWS[3:]:
            entry = after["examined"].get(str(nid))
            self.assertIsNotNone(entry, "notice %d has no record at all" % nid)
            self.assertEqual(entry["outcome"], "deferred", nid)
            self.assertEqual(entry["reason"], "run_budget_exhausted", nid)
            self.assertIn(str(nid), after["pending_since"],
                          "a deferred notice must be queued for retry")
        # 3. the floor does NOT advance past the last notice actually processed.
        self.assertEqual(after["last_examined_notice_id"], 602)
        self.assertNotIn("603", car._decided_ids(after["examined"]))
        # 4. and the deferred notices were never even opened.
        for nid, _t, _d in self.ROWS[3:]:
            self.assertFalse(any("/notices/%d" % nid in u for u in calls),
                             "notice %d was opened despite the exhausted budget"
                             % nid)
        # The notices that DID fit are still decided normally.
        self.assertEqual(after["examined"]["600"]["outcome"], "processed")
        self.assertEqual(len(payloads), 1)

    def test_the_deferred_remainder_is_retried_and_then_advances(self):
        """R1 closure: a run with the budget lifted picks the deferred notices
        straight back up and the floor finally moves past them."""
        texts = {nid: ("Roll %s passed" % ROLL if nid <= 602 else "no roll")
                 for nid, _t, _d in self.ROWS}
        with tempfile.TemporaryDirectory() as d:
            drive_result_pass(d, seed_state(), self.ROWS, self.TITLES, texts,
                              budget_s=360, clock=FakeClock(step=100.0))
            rc, payloads, after, _calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES, texts,
                budget_s=360, clock=FakeClock(step=1.0))
        self.assertEqual(rc, 0)
        self.assertEqual(after["last_examined_notice_id"], 605)
        for nid, _t, _d in self.ROWS[3:]:
            self.assertNotEqual(after["examined"][str(nid)]["outcome"],
                                "deferred", nid)
            self.assertNotIn(str(nid), after["pending_since"])

    def test_a_zero_budget_defers_everything_and_moves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES,
                {nid: "no roll" for nid, _t, _d in self.ROWS},
                budget_s=0, clock=FakeClock(step=1.0))
        self.assertEqual(rc, 0)
        self.assertEqual(payloads, [])
        self.assertEqual(calls, ["https://iost.tu.edu.np/notices?notice_type=21"],
                         "no notice may be opened at all")
        for nid, _t, _d in self.ROWS:
            self.assertEqual(after["examined"][str(nid)]["reason"],
                             "run_budget_exhausted", nid)
        self.assertEqual(after["last_examined_notice_id"], 0)

    def test_a_generous_budget_changes_nothing(self):
        """The guard must not fire spuriously: with the real default budget and
        a fast pass, behaviour is the pre-fix behaviour."""
        with tempfile.TemporaryDirectory() as d:
            rc, _payloads, after, _calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES,
                {nid: "Roll %s passed" % ROLL for nid, _t, _d in self.ROWS},
                clock=FakeClock(step=1.0))
        self.assertEqual(rc, 0)
        self.assertEqual(after["last_examined_notice_id"], 605)
        for nid, _t, _d in self.ROWS:
            self.assertNotEqual(after["examined"][str(nid)]["reason"],
                                "run_budget_exhausted", nid)

    def test_a_negative_budget_disables_the_deadline(self):
        """The documented escape hatch (unbounded pass) still works."""
        with tempfile.TemporaryDirectory() as d:
            rc, _payloads, after, _calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES,
                {nid: "Roll %s passed" % ROLL for nid, _t, _d in self.ROWS},
                budget_s=None, clock=FakeClock(step=10_000.0))
        self.assertEqual(rc, 0)
        self.assertEqual(after["last_examined_notice_id"], 605)

    def test_the_budget_is_a_named_constant_inside_the_job_timeout(self):
        """Documented relationship to the job's `timeout-minutes: 15` (900s).
        The budget must be comfortably inside it so the run finishes, and it
        must leave room for the in-flight notice's OCR."""
        job_budget_s = 15 * 60
        self.assertIsInstance(car.RESULT_PASS_BUDGET_S, int)
        self.assertGreater(car.RESULT_PASS_BUDGET_S, 0)
        self.assertLess(car.RESULT_PASS_BUDGET_S, job_budget_s / 2,
                        "the result pass must not be able to eat the whole job")
        # room for one full notice (detail fetch + PDF + OCR) plus the tail
        # steps after the pass returns.
        self.assertGreater(job_budget_s - car.RESULT_PASS_BUDGET_S, 300)
        # Look the default up BY NAME. run() has gained parameters since this
        # was written, and a positional index into __defaults__ silently
        # asserts against the wrong argument - which is how a budget default
        # could have drifted from the constant without this test noticing.
        import inspect
        defaults = dict(inspect.signature(car.run).parameters)
        self.assertEqual(defaults["budget_s"].default,
                         car.RESULT_PASS_BUDGET_S,
                         "run() must default budget_s to the constant, not a "
                         "literal")
        self.assertEqual(defaults["max_script_defers"].default,
                         car.ACADEMIC_MAX_SCRIPT_DEFERS)
        self.assertEqual(defaults["max_consecutive_defers"].default,
                         car.ACADEMIC_MAX_CONSECUTIVE_DEFERS)


class JustCreatedSemesterKeyTest(unittest.TestCase):
    """The by-construction semester-key creation (the P0 fix) and its negative
    half: a FAILED verdict must never be invented out of the entry the run
    itself just made up."""

    ROWS = [(700, "4th Semester Result", "2026-09-27")]
    TITLES = {700: "TU B.Sc.CSIT IV Semester-2081 Result"}

    def _state_without_sem4(self):
        state = seed_state()
        del state["semesters"]["4"]
        return state

    def test_roll_absent_with_a_just_created_key_defers_and_retries(self):
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, _calls = drive_result_pass(
                d, self._state_without_sem4(), self.ROWS, self.TITLES,
                {700: "Roll 99999999 not present"})
        self.assertEqual(rc, 0)
        self.assertEqual(payloads, [],
                         "a FAILED verdict invented from a missing state entry "
                         "is a claim we cannot support - it must not be pushed")
        entry = after["examined"]["700"]
        self.assertEqual(entry["outcome"], "deferred")
        self.assertEqual(entry["reason"], "inconsistent_state")
        # The key is still created (P0 keeps the missing-key case impossible),
        # but it is left PENDING: no verdict was asserted.
        self.assertEqual(after["semesters"]["4"]["status"], "pending")
        self.assertFalse(after["semesters"]["4"]["result_announced"])
        self.assertFalse(after["semesters"]["4"]["roll_found"])
        self.assertEqual(after["last_examined_notice_id"], 0)
        self.assertIn("700", after["pending_since"])

    def test_the_retry_after_the_defer_reaches_the_verdict(self):
        """Defer-and-retry must terminate, not defer forever: the pending entry
        written on run 1 is real state on disk, so run 2 is entitled to a
        verdict. (Re-driving with run 1's own saved state is byte-identical to
        the next cron tick reading the same file.)"""
        texts = {700: "Roll 99999999 not present"}
        with tempfile.TemporaryDirectory() as d:
            rc1, first, after1, _ = drive_result_pass(
                d, self._state_without_sem4(), self.ROWS, self.TITLES, texts)
            self.assertEqual(first, [])
            rc2, second, after2, _ = drive_result_pass(
                d, after1, self.ROWS, self.TITLES, texts)
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(len(second), 1,
                         "the retry must reach the verdict the defer avoided "
                         "inventing")
        self.assertFalse(second[0]["passed"])
        self.assertEqual(after2["semesters"]["4"]["status"], "failed")
        self.assertEqual(after2["examined"]["700"]["outcome"], "processed")
        self.assertNotIn("700", after2["pending_since"])
        self.assertEqual(after2["last_examined_notice_id"], 700)

    def test_roll_present_with_a_just_created_key_still_alerts(self):
        """The path that fixed the original P0. It must NOT regress: a result
        sitting in a downloaded PDF is never withheld."""
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, _calls = drive_result_pass(
                d, self._state_without_sem4(), self.ROWS, self.TITLES,
                {700: "Roll %s passed" % ROLL})
        self.assertEqual(rc, 0)
        self.assertEqual(len(payloads), 1,
                         "a real result in a downloaded PDF must be reported")
        self.assertTrue(payloads[0]["passed"])
        self.assertEqual(after["semesters"]["4"]["status"], "passed")
        self.assertEqual(after["examined"]["700"]["outcome"], "processed")
        self.assertEqual(after["last_examined_notice_id"], 700)

    def test_an_existing_pending_key_still_asserts_a_failed_verdict(self):
        """The guard keys on "the key was just created", nothing else: an entry
        the state has carried for a while still gets its honest FAILED alert."""
        state = seed_state()
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, _calls = drive_result_pass(
                d, state, self.ROWS, self.TITLES,
                {700: "Roll 99999999 not present"})
        self.assertEqual(rc, 0)
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["passed"])
        self.assertEqual(after["semesters"]["4"]["status"], "failed")
        self.assertEqual(after["examined"]["700"]["outcome"], "processed")


class SiteConstantTitleTest(unittest.TestCase):
    """THE 2026-09-28 BURN, in the real page shape.

    The live IOST detail page returns the site-wide constant
    ``INSTITUTE OF SCIENCE AND TECHNOLOGY`` in the HTML <title> element for
    EVERY notice; the real notice title is in the body. The monitor read
    <title>, got the constant, match_semester() returned None, and the run
    recorded `irrelevant` / `no_semester_match` and advanced the floor past
    notice 15673 - the owner's semester-1 2082 result. Under R4 that is
    permanent: a decided notice is never re-fetched.

    Every other academic fixture in this file fabricated a <title> containing
    the notice title, so nothing here fails on the pre-fix code. These do.
    """

    SEM1_TITLE = "B.Sc.CSIT I Semester-2082 Exam Result"
    PDF = "https://portal.tu.edu.np/notice/15673/1790601157.pdf"

    def _state_sem1_failed(self):
        """The production shape: sem 1 already failed+announced, sem 4 pending.

        This is the tracked owner's actual state on 2026-09-29. It is why a
        sem-1 PASS is news: the run must report the change of verdict, and the
        P1d duplicate suppression must not swallow it.
        """
        state = seed_state()
        state["semesters"]["1"] = {
            "status": "failed", "result_announced": True, "roll_found": False,
            "result_date": "2025-07-25", "notice_id": None, "pdf_url": None}
        return state

    def _run(self, d, state, detail_html, pdf_text, rows=None, nid=15673):
        state_path = os.path.join(d, "state.json")
        preview = os.path.join(d, "preview.json")
        car.save_state(state_path, state)
        rows = rows or [(nid, self.SEM1_TITLE, "2026-09-28")]
        responses = {
            "notice_type=21": (200, notice_list_html(rows).encode()),
            "/notices/%d" % nid: (200, detail_html.encode()),
            "portal.tu.edu.np": (200, fake_pdf_bytes()),
        }
        fetch = make_fetch(responses)
        with mock.patch.object(car, "extract_pdf_text",
                               return_value=pdf_text), \
             mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
            rc = car.run(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=fetch, run_exam_notices=False)
        payloads = []
        if os.path.exists(preview):
            with open(preview, encoding="utf-8") as fh:
                payloads = json.load(fh)
        return rc, payloads, car.load_state(state_path)

    # ---------------- the parser, offline --------------------------------
    def test_the_real_page_shape_yields_the_real_title(self):
        parsed = car.parse_notice_detail_html(
            iost_detail_html(self.SEM1_TITLE, self.PDF))
        self.assertEqual(parsed["title"], self.SEM1_TITLE)
        self.assertEqual(parsed["title_source"], "container_h4")
        self.assertFalse(parsed["title_degraded"])

    def test_the_site_constant_is_recognised(self):
        for text in (SITE_CONSTANT,
                     "Institute of Science and Technology",
                     "  institute OF   science AND technology  ",
                     SITE_CONSTANT + " - Notices",
                     "TRIBHUVAN UNIVERSITY", "IOST", "", "   "):
            self.assertTrue(car.is_site_constant_title(text), repr(text))

    def test_a_real_title_is_not_mistaken_for_the_constant(self):
        for text in (self.SEM1_TITLE,
                     "B.Sc.CSIT IV Semester-2081 Exam Result",
                     "Exam Routine 2082",
                     "B.Sc. प्रवेश परीक्षा 2083 को नतिजा प्रकशित गरिएको बारे",
                     "विद्यावारिधि प्रवेश परीक्षा केन्द्र र समयसम्बन्धी जरुरी सूचना",
                     "Institute of Science and Technology - B.Sc.CSIT I "
                     "Semester-2082 Exam Result"):
            self.assertFalse(car.is_site_constant_title(text), repr(text))

    def test_a_nepali_title_is_readable_not_site_chrome(self):
        """Most IOST notices are titled in Devanagari. Verified live against
        notices 15323, 15362, 15515, 15591 and 15696.

        The risk this pins: the constant check compares against ASCII constants,
        so a non-ASCII title has to be treated as a real title outright. If it
        were normalised instead, every Devanagari character would be stripped as
        "not A-Z0-9", nothing would be left, and a perfectly good title would
        be misread as site chrome - deferring those notices forever.
        """
        for text in ("परीक्षार्थीहरूका लागि अत्यन्त जरूरी सूचना",
                     "विद्यावारिधि प्रवेश परीक्षा केन्द्र र समयसम्बन्धी जरुरी सूचना"):
            self.assertFalse(car.is_site_constant_title(text), repr(text))
        parsed = car.parse_notice_detail_html(
            iost_detail_html("विद्यावारिधि प्रवेश परीक्षा केन्द्र र समयसम्बन्धी जरुरी सूचना"))
        self.assertFalse(parsed["title_degraded"])
        self.assertEqual(parsed["title_source"], "container_h4")

    def test_the_more_notices_heading_is_never_the_title(self):
        """The page carries a THIRD h4.title that belongs to no notice. A
        page-wide scan returns it, which would be a different silent burn."""
        parsed = car.parse_notice_detail_html(
            iost_detail_html(self.SEM1_TITLE, self.PDF, extra_heading="More Notices"))
        self.assertEqual(parsed["title"], self.SEM1_TITLE)
        self.assertNotEqual(parsed["title"], "More Notices")

    def test_the_ck_table_is_a_secondary_source(self):
        """No h4 container, but the editor block still re-states the title."""
        html = iost_detail_html(self.SEM1_TITLE, self.PDF, include_body_title=False)
        html = html.replace('<div class="ck-table">', "")  # not built by default
        html = ('<html><head><title>%s</title></head><body>'
                '<div class="ck-table"><p>%s</p></div></body></html>'
                % (SITE_CONSTANT, self.SEM1_TITLE))
        parsed = car.parse_notice_detail_html(html)
        self.assertEqual(parsed["title"], self.SEM1_TITLE)
        self.assertEqual(parsed["title_source"], "ck_table_p")

    def test_a_non_constant_title_tag_is_still_a_usable_fallback(self):
        """The fallback is preserved for a page that only has the <title>."""
        parsed = car.parse_notice_detail_html(
            '<html><head><title>%s</title></head><body>x</body></html>'
            % self.SEM1_TITLE)
        self.assertEqual(parsed["title"], self.SEM1_TITLE)
        self.assertEqual(parsed["title_source"], "html_title")
        self.assertFalse(parsed["title_degraded"])

    def test_a_constant_title_tag_alone_is_degraded_not_authoritative(self):
        parsed = car.parse_notice_detail_html(
            '<html><head><title>%s</title></head><body>x</body></html>'
            % SITE_CONSTANT)
        self.assertEqual(parsed["title"], "")
        self.assertTrue(parsed["title_degraded"])

    def test_fetch_notice_detail_reads_the_body_not_the_title_tag(self):
        detail = car.fetch_notice_detail(
            "https://iost.tu.edu.np/notices/15673",
            fetch_fn=make_fetch({"/notices/15673": (
                200, iost_detail_html(self.SEM1_TITLE, self.PDF).encode())}))
        self.assertEqual(detail["title"], self.SEM1_TITLE)
        self.assertEqual(detail["title_source"], "container_h4")
        self.assertFalse(detail["title_degraded"])
        self.assertEqual(detail["pdf_url"], self.PDF)

    # ---------------- end to end: the recovered result -------------------
    def test_the_site_constant_does_not_burn_a_sem1_2082_result(self):
        """THE regression test.

        Pre-fix on this fixture: match_semester(SITE_CONSTANT) -> None,
        examined["15673"] = irrelevant/no_semester_match, floor advanced to
        15673, sem 1 stayed `failed`, and no payload was ever emitted. The owner
        had passed semester 1 and was told nothing, permanently.
        """
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after = self._run(
                d, self._state_sem1_failed(),
                iost_detail_html(self.SEM1_TITLE, self.PDF),
                SEM1_PDF_TEXT)
        self.assertEqual(rc, 0)
        self.assertEqual(len(payloads), 1,
                         "the owner's sem-1 result must be announced")
        self.assertTrue(payloads[0]["passed"])
        self.assertEqual(payloads[0]["notice_id"], 15673)
        sem1 = after["semesters"]["1"]
        self.assertEqual(sem1["status"], "passed")
        self.assertTrue(sem1["roll_found"])
        self.assertEqual(after["examined"]["15673"]["outcome"], "processed")
        self.assertNotEqual(after["examined"]["15673"]["reason"],
                            "no_semester_match")

    def test_the_roll_split_across_a_space_is_found(self):
        """The real sheet prints the roll split across a column boundary."""
        self.assertTrue(car.roll_in_text(SEM1_PDF_TEXT, ROLL))
        self.assertFalse(car.roll_in_text(SEM1_PDF_TEXT.replace("1357 2468", ""),
                                          ROLL))
        # The roll genuinely absent from a READABLE sheet is the ONE case that
        # may record a verdict, and the guard must not swallow it. Here the
        # recorded verdict is already `failed`, so P1d correctly suppresses the
        # unchanged repeat as `already_announced` - what matters is that it is
        # NOT deferred as unreadable, i.e. the sheet was read successfully.
        # NB: the sheet prints the roll SPLIT, so the substitution targets the
        # split form; replacing the joined "13572468" would match nothing.
        with tempfile.TemporaryDirectory() as d:
            _rc, payloads, after = self._run(
                d, self._state_sem1_failed(),
                iost_detail_html(self.SEM1_TITLE, self.PDF),
                SEM1_PDF_TEXT.replace("1357 2468", "1357 2467"))
        self.assertEqual(payloads, [])
        self.assertEqual(after["examined"]["15673"]["outcome"],
                         "already_announced")
        self.assertNotEqual(after["examined"]["15673"]["reason"],
                            "pdf_text_unreadable")
        self.assertEqual(after["semesters"]["1"]["status"], "failed")
        self.assertEqual(after["last_examined_notice_id"], 15673,
                         "a readable sheet advances the floor normally")

    def test_a_truly_empty_title_still_defers(self):
        """Pre-existing behaviour, preserved: no title anywhere -> defer, and
        the floor must not advance past it."""
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after = self._run(
                d, self._state_sem1_failed(),
                iost_detail_html("", None, include_body_title=False,
                                 include_title_tag=False),
                SEM1_PDF_TEXT)
        self.assertEqual(rc, 0)
        self.assertEqual(payloads, [])
        self.assertEqual(after["examined"]["15673"]["outcome"], "deferred")
        self.assertEqual(after["examined"]["15673"]["reason"], "title_unparseable")
        self.assertEqual(after["last_examined_notice_id"], 0)
        self.assertEqual(after["semesters"]["1"]["status"], "failed",
                         "an unreadable page must not produce a verdict")

    def test_a_constant_only_page_defers_and_does_not_advance_the_floor(self):
        """Constant <title>, no body title: unextractable, so DEFER. Pre-fix
        this recorded `irrelevant` and advanced the floor - the burn."""
        html = ('<html><head><title>%s</title></head><body>x</body></html>'
                % SITE_CONSTANT)
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after = self._run(
                d, self._state_sem1_failed(), html, SEM1_PDF_TEXT)
        self.assertEqual(rc, 0)
        self.assertEqual(payloads, [])
        self.assertEqual(after["examined"]["15673"]["outcome"], "deferred")
        self.assertEqual(after["examined"]["15673"]["reason"], "title_unparseable")
        self.assertEqual(after["last_examined_notice_id"], 0,
                         "a deferred notice must block the floor (R1/R5)")
        self.assertIn("15673", after["pending_since"])

    def test_a_deferred_notice_is_retried_and_then_lands(self):
        """Defer-and-retry must terminate. Run 1 sees only the constant; run 2
        sees the real page shape and announces the result."""
        state = self._state_sem1_failed()
        constant_only = ('<html><head><title>%s</title></head><body>x</body></html>'
                         % SITE_CONSTANT)
        with tempfile.TemporaryDirectory() as d:
            rc1, p1, after1 = self._run(d, state, constant_only, SEM1_PDF_TEXT)
            self.assertEqual((rc1, p1), (0, []))
            self.assertEqual(after1["examined"]["15673"]["outcome"], "deferred")
            # same state file, next tick, real page
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            responses = {
                "notice_type=21": (200, notice_list_html(
                    [(15673, self.SEM1_TITLE, "2026-09-28")]).encode()),
                "/notices/15673": (200, iost_detail_html(
                    self.SEM1_TITLE, self.PDF).encode()),
                "portal.tu.edu.np": (200, fake_pdf_bytes()),
            }
            with mock.patch.object(car, "extract_pdf_text",
                                   return_value=SEM1_PDF_TEXT), \
                 mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc2 = car.run(state_path,
                              "https://iost.tu.edu.np/notices?notice_type=21",
                              dry_run=True, fetch_fn=make_fetch(responses),
                              run_exam_notices=False)
            payloads = []
            if os.path.exists(preview):
                with open(preview, encoding="utf-8") as fh:
                    payloads = json.load(fh)
            after2 = car.load_state(state_path)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(payloads), 1, "the retry must reach the verdict")
        self.assertTrue(payloads[0]["passed"])
        self.assertEqual(after2["semesters"]["1"]["status"], "passed")
        self.assertEqual(after2["examined"]["15673"]["outcome"], "processed")
        self.assertEqual(after2["last_examined_notice_id"], 15673)


class FalseFailedGuardTest(unittest.TestCase):
    """The SECOND, worse bug: a FALSE FAILED on a semester the owner passed.

    ``process_notice`` guarded only the EMPTY-text case ("Do NOT mark failed")
    and then marked `failed` for ANY non-empty text where the roll was absent.
    A result PDF with a legacy custom-encoded font extracts to non-empty,
    digit-poor garbage, so the monitor would announce a FAILURE the owner never
    had - and advance current_semester off the strength of it.
    """

    def _sem4_notice(self):
        return {"id": 16000,
                "title": "TU B.Sc.CSIT IV Semester-2082 Exam Result",
                "url": "https://iost.tu.edu.np/notices/16000",
                "pdf_url": "https://portal.tu.edu.np/notice/16000/1.pdf"}

    def test_the_guard_reads_a_real_sheet_as_readable(self):
        self.assertTrue(car.sheet_text_is_readable(SEM1_PDF_TEXT))
        self.assertTrue(car.sheet_text_is_readable(
            "roll 13572468 and 13570000 listed in the results table"))
        # a long roll column of SPLIT rolls is still a read sheet
        self.assertTrue(car.sheet_text_is_readable(
            " ".join("1357 %04d" % i for i in range(100))))

    def test_the_guard_rejects_unreadable_text(self):
        self.assertFalse(car.sheet_text_is_readable(unreadable_pdf_text()))
        self.assertFalse(car.sheet_text_is_readable("No students were listed."))
        self.assertFalse(car.sheet_text_is_readable(""))
        # digits present but far too few to be a result sheet
        self.assertFalse(car.sheet_text_is_readable("Total 42 students passed."))
        # digits present but no roll-like token at all
        self.assertFalse(car.sheet_text_is_readable(
            " ".join("12345" for _ in range(100))))

    def test_an_unreadable_sheet_never_marks_failed(self):
        state = seed_state()
        with mock.patch.object(car, "extract_pdf_text",
                               return_value=unreadable_pdf_text()):
            payload = car.process_notice(state, self._sem4_notice(),
                                        fake_pdf_bytes())
        self.assertIsNone(payload)
        sem4 = state["semesters"]["4"]
        self.assertEqual(sem4["status"], "pending",
                         "an unreadable sheet must NOT produce a verdict")
        self.assertFalse(sem4["result_announced"])
        self.assertFalse(sem4["roll_found"])
        self.assertEqual(state["current_semester"], 4,
                         "current_semester must not advance on a false fail")

    def test_a_readable_sheet_with_the_roll_still_passes(self):
        """The guard must not touch the positive path."""
        state = seed_state()
        text = SEM1_PDF_TEXT
        with mock.patch.object(car, "extract_pdf_text", return_value=text):
            payload = car.process_notice(state, self._sem4_notice(),
                                        fake_pdf_bytes())
        self.assertIsNotNone(payload)
        self.assertTrue(payload["passed"])
        self.assertEqual(state["semesters"]["4"]["status"], "passed")
        self.assertTrue(state["semesters"]["4"]["roll_found"])

    def test_a_readable_sheet_without_the_roll_still_fails(self):
        """A confident, readable sheet on which the roll is genuinely absent
        is the ONLY case allowed to mark failed. That behaviour is unchanged."""
        state = seed_state()
        text = SEM1_PDF_TEXT.replace("1357 2468", "1357 2469")
        self.assertFalse(car.roll_in_text(text, ROLL))
        self.assertTrue(car.sheet_text_is_readable(text))
        with mock.patch.object(car, "extract_pdf_text", return_value=text):
            payload = car.process_notice(state, self._sem4_notice(),
                                        fake_pdf_bytes())
        self.assertIsNotNone(payload)
        self.assertFalse(payload["passed"])
        self.assertEqual(state["semesters"]["4"]["status"], "failed")

    def test_end_to_end_an_unreadable_sheet_defers_and_does_not_burn(self):
        state = seed_state()
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, _calls = drive_result_pass(
                d, state, [(16000, "B.Sc.CSIT IV Semester-2082 Exam Result",
                            "2026-09-28")],
                {16000: "TU B.Sc.CSIT IV Semester-2082 Exam Result"},
                {16000: unreadable_pdf_text()})
        self.assertEqual(rc, 0)
        self.assertEqual(payloads, [],
                         "a false FAILED is the worst outcome this script has")
        entry = after["examined"]["16000"]
        self.assertEqual(entry["outcome"], "deferred")
        self.assertEqual(entry["reason"], "pdf_text_unreadable")
        self.assertEqual(after["semesters"]["4"]["status"], "pending")
        self.assertFalse(after["semesters"]["4"]["result_announced"])
        self.assertEqual(after["last_examined_notice_id"], 0,
                         "the floor must not advance past a deferred notice")
        self.assertIn("16000", after["pending_since"])

    def test_the_new_reason_token_is_declared(self):
        self.assertIn("pdf_text_unreadable", car.REASONS)
        for outcome in car.OUTCOMES:
            self.assertIsInstance(outcome, str)


class RollFoundNormalisationTest(unittest.TestCase):
    """A `failed`+announced semester with NO `roll_found` key used to load
    cleanly, and the missing key then read as a CHANGED verdict (None -> False)
    in the P1 duplicate suppression, emitting one duplicate FAILED alert."""

    def _write(self, d, state):
        path = os.path.join(d, "state.json")
        car.save_state(path, state)
        return path

    def test_a_complete_state_is_accepted_untouched(self):
        state = seed_state()
        with tempfile.TemporaryDirectory() as d:
            meta = {}
            loaded = car.load_state(self._write(d, state), meta=meta)
        for key in ("1", "2", "3"):
            self.assertIs(loaded["semesters"][key]["roll_found"], True)
        self.assertIs(loaded["semesters"]["4"]["roll_found"], False)
        self.assertFalse(meta["repaired"])
        self.assertEqual(loaded, state, "a valid state must not be mutated")

    def test_an_announced_semester_without_roll_found_is_normalised(self):
        for status, expected in (("failed", False), ("passed", True)):
            state = seed_state()
            sem = state["semesters"]["1"]
            del sem["roll_found"]
            sem["status"] = status
            with tempfile.TemporaryDirectory() as d:
                meta = {}
                loaded = car.load_state(self._write(d, state), meta=meta)
            self.assertIs(loaded["semesters"]["1"]["roll_found"], expected,
                          "status=%s must imply roll_found=%r" % (status,
                                                                 expected))
            self.assertTrue(meta["repaired"])

    def test_a_legacy_schema1_state_without_roll_found_still_loads(self):
        """The accept-vs-reject decision, proven. Rejecting would exit 3 - the
        monitor refusing to run at all - on exactly the legacy states the
        replay-window migration exists to rescue."""
        legacy = {
            "schema_version": 1, "roll_number": ROLL, "current_semester": 4,
            "last_checked_notice_id": 15236,
            "semesters": {"1": {"status": "failed", "result_announced": True},
                          "4": {"status": "pending", "result_announced": False}},
        }
        with tempfile.TemporaryDirectory() as d:
            meta = {}
            loaded = car.load_state(self._write(d, legacy), meta=meta)
        self.assertEqual(loaded["schema_version"], 2)
        self.assertIs(loaded["semesters"]["1"]["roll_found"], False)
        # The unannounced entry has no verdict to complete: left exactly as is.
        self.assertNotIn("roll_found", loaded["semesters"]["4"])
        self.assertTrue(meta["migrated"])

    def test_a_mistyped_roll_found_is_rederived_not_truthiness_coerced(self):
        """`bool("false") is True`, so a truthiness coercion would INVERT the
        verdict. The value is rederived from status instead."""
        for bad in ("false", "no", 0, 1, []):
            state = seed_state()
            state["semesters"]["1"]["status"] = "failed"
            state["semesters"]["1"]["roll_found"] = bad
            with tempfile.TemporaryDirectory() as d:
                loaded = car.load_state(self._write(d, state))
            self.assertIs(loaded["semesters"]["1"]["roll_found"], False,
                          "roll_found=%r on a failed semester" % (bad,))

    def test_replaying_a_failed_verdict_with_no_roll_found_emits_nothing(self):
        """End to end: the actual spurious push this closes."""
        state = seed_state()
        state["semesters"]["1"] = {"status": "failed", "result_announced": True,
                                   "result_date": "2025-07-25",
                                   "notice_id": 15100, "pdf_url": None}
        rows = [(800, "B.Sc.CSIT I Semester-2081 Result", "2026-08-01")]
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, _calls = drive_result_pass(
                d, state, rows, {800: "B.Sc.CSIT I Semester Result"},
                {800: "Roll 99999999 not present"})
        self.assertEqual(rc, 0)
        self.assertEqual(payloads, [],
                         "an unchanged FAILED verdict is not news, and the "
                         "missing roll_found key used to disguise it as news")
        self.assertEqual(after["examined"]["800"]["outcome"],
                         "already_announced")
        self.assertFalse(after["semesters"]["1"]["roll_found"])


# ---------------------------------------------------------------------------
# Devanagari (Nepali) titles — the P0 that would have burned semester 4
# ---------------------------------------------------------------------------
# Verified live on 2026-09-29: 5 of the 14 notices on the result feed are in
# Nepali. The Latin-only matcher read them as "no semester match", recorded
# `irrelevant` - a PERMANENT decision under R4 - and advanced the floor past
# them. Notice 15696 was already burned this way. The owner's own sem-1 pass
# had already been lost to the same class of bug one layer down (the site-wide
# constant in <title>), so this is the same fire, one storey up.

# Real titles in the shape the board publishes them (Devanagari with an ASCII
# batch year and an ASCII abbreviation is the common mixed case).
NEPALI_SEM4_RESULT = "बी.एस.सी. चौथो सेमेस्टरको परीक्षा नतिजा प्रकाशित भएको छ"
NEPALI_SEM1_RESULT = "बी.एस.सी. पहिलो सेमेस्टर २०८१ नतिजा प्रकाशित"
NEPALI_SEM1_BACK = "सी.एस.आई.टी पहिलो सेमेस्टर ब्याक परीक्षा नतिजा"
NEPALI_NON_RESULT = "विद्यार्थी साइकिल चलाउन सम्बन्धी सूचना"
NEPALI_MEETING = "बैठक बस्ने सम्बन्धी सूचना"
# Recognised marker + sem token + result term would all be present if the
# vocabulary covered this wording - it does not, which is the point: nothing is
# recognised, so we must NOT decide.
NEPALI_OPAQUE = "शैक्षिक सत्र २०८१ को कार्ययोजना"
NEPALI_DIGITS_ONLY = "१२३४५६७८९०"


class DevanagariModuleTest(unittest.TestCase):
    """scripts/devanagari.py on its own terms."""

    def test_detect_script_classifies_all_four_states(self):
        self.assertEqual(dv.detect_script("B.Sc.CSIT IV Semester Result"),
                         "latin")
        self.assertEqual(dv.detect_script("परीक्षा नतिजा"), "devanagari")
        self.assertEqual(dv.detect_script("B.Sc.CSIT सेमेस्टर नतिजा २०८१"),
                         "mixed",
                         "an ASCII abbreviation inside Devanagari prose is the "
                         "real mixed case on this board")
        self.assertEqual(dv.detect_script(""), "empty")
        self.assertEqual(dv.detect_script(None), "empty")
        self.assertEqual(dv.detect_script("   "), "empty")

    def test_ascii_punctuation_and_devanagari_digits_are_not_mixed(self):
        """CORRECTION to an earlier expectation of mine. 'B.S' in ASCII dots
        plus Devanagari DIGITS is not a mixed-script title: the digits belong
        to the Devanagari block (U+0966..U+096F) and an ASCII full stop is not
        a letter, so there is no Latin letter present and the honest answer is
        'devanagari'. 'mixed' must mean Latin LETTERS alongside Devanagari -
        the case that actually needs both stages to run."""
        self.assertEqual(dv.detect_script("बी.एस.सी. सेमेस्टर नतिजा २०८१"),
                         "devanagari")
        self.assertEqual(dv.detect_script("B.Sc.CSIT सेमेस्टर नतिजा"),
                         "mixed")

    def test_latinize_digits_is_exhaustive_and_reversible(self):
        """Ten entries, exact, reversible. This is the ONLY transliteration in
        the academic path, and its safety rests entirely on being lossless."""
        self.assertEqual(len(dv.NEPALI_DIGITS), 10)
        for deva, ascii_d in dv.NEPALI_DIGITS.items():
            self.assertEqual(len(deva), 1, repr(deva))
            self.assertEqual(len(ascii_d), 1, repr(ascii_d))
            # Every key really is a Devanagari digit, i.e. inside the block the
            # script detector looks at, and inside the digit subrange.
            self.assertTrue(dv.DEVANAGARI_RE.fullmatch(deva), repr(deva))
            self.assertTrue(("\u0966" <= deva <= "\u096f"), repr(deva))
            self.assertTrue(ascii_d.isdigit())
            self.assertIn(ascii_d, "0123456789")
        # Exhaustive in the right ORDER: "०" is 0, "९" is 9, with no gap.
        for i in range(10):
            self.assertEqual(dv.NEPALI_DIGITS[chr(0x0966 + i)], str(i))
        # Reversible: no other character is touched.
        self.assertEqual(dv.latinize_digits("२०८१"), "2081")
        self.assertEqual(dv.latinize_digits("नतिजा २०८१"), "नतिजा 2081")
        self.assertEqual(dv.latinize_digits("B.Sc.CSIT 4th"), "B.Sc.CSIT 4th")
        self.assertEqual(dv.latinize_digits(""), "")
        self.assertEqual(dv.latinize_digits(None), "")
        # Applying it twice changes nothing: the output is pure ASCII digits,
        # so the second pass is a fixed point.
        once = dv.latinize_digits("२०८१ पहिलो")
        self.assertEqual(dv.latinize_digits(once), once)

    def test_latinize_digits_leaves_latin_titles_byte_identical(self):
        for title in ("TU B.Sc.CSIT IV Semester-2081 Exam Result", "",
                      "Admission open for B.Sc.CSIT"):
            self.assertEqual(dv.latinize_digits(title), title)

    def test_digit_boundary_projection_restores_word_boundaries(self):
        """\\b in Python's Unicode-aware re does not fire next to a Devanagari
        letter, so plain digit-latinisation would hide the year. The projection
        blanks the surrounding Devanagari instead."""
        projected = dv.digit_boundary_projection("वर्ष२०८१")
        self.assertIn("2081", projected)
        self.assertEqual(projected.strip(), "2081")
        self.assertEqual(len(projected), len("वर्ष२०८१"))

    def test_the_projection_preserves_ascii_letters(self):
        """It must not blank the ASCII a title needs: 'B.Sc.CSIT २०८१' has to
        keep its marker for the Latin stages."""
        projected = dv.digit_boundary_projection("B.Sc.CSIT २०८१")
        self.assertIn("B.Sc.CSIT", projected)
        self.assertIn("2081", projected)

    def test_the_boards_own_nonstandard_spelling_is_recognised(self):
        """LIVE FINDING, 2026-09-29. The board does not write standard Nepali:
        notice 15696's real title reads "चाैथाे" (fourth) and "शत्र"
        (semester), where standard Nepali is "चौथो" and "सेमेस्टर". The matra
        is typed ा + े instead of ौ, the way a legacy font or a romanised
        keyboard IME produces it.

        Before these spellings were observed and added, a CSIT result published
        in the board's ACTUAL spelling resolved to no_semester_token -> no_match
        -> recorded `irrelevant` -> BURNED, which is precisely the failure this
        whole unit exists to remove. This test is the regression guard.
        """
        board = "बी.एस.सी. चाैथाे शत्र नतिजा प्रकाशित"
        v = car.classify_semester(board)
        self.assertEqual(v["status"], "match",
                         "the board's own spelling must resolve: %r" % v)
        self.assertEqual(v["semester"], 4)
        self.assertIn("शत्र", dv.NEPALI_SEM_TOKENS)

    def test_the_live_15696_title_is_decided_not_deferred(self):
        """The real title, transcribed from the live board. It is an M.A.
        notice, so no_match is the CORRECT answer — what matters is that it is
        a decision rather than an unreadable deferral."""
        live = ("स्नातकाेत्तर तह दाेश्राे वर्ष चाैथाे शत्र २०८० समूह माैका "
                "परीक्षाका नतिजा प्रकाशित")
        v = car.classify_semester(live)
        self.assertEqual(v["status"], "no_match", v)
        self.assertNotEqual(v["status"], "unknown")
        self.assertEqual(v["batch_year"], 2080)

    def test_a_misspelled_sem_four_result_in_the_board_style_resolves(self):
        """The case that would have burned: CSIT + the board's spelling."""
        for title in ("बी.एस.सी. चाैथाे शत्र नतिजा",
                      "बी.एस.सी. चाैथाे सेमेस्टरको परीक्षा नतिजा",
                      "सी.एस.आई.टी चाैथाे शत्र नतिजा प्रकाशित"):
            v = car.classify_semester(title)
            self.assertEqual(v["semester"], 4, title)

    def test_every_ordinal_word_maps_to_its_own_int(self):
        """A wrong lookup here is a wrong semester, so each entry is pinned."""
        expected = {
            "पहिलो": 1, "पहिल": 1, "दोस्रो": 2, "दोस्र": 2, "तेस्रो": 3,
            "तेस्र": 3, "चौथो": 4, "चौथ": 4, "पाँचौं": 5, "पाँचौ": 5,
            "छैठौं": 6, "छैठौ": 6, "सातौं": 7, "सातौ": 7, "आठौं": 8,
            "आठौ": 8,
            # Observed non-standard spellings from the live board (notice
            # 15696): the matra ौ typed as ा + े, and "शत्र" for semester.
            "चाैथाे": 4, "चाैथ": 4, "पहिलोँ": 1, "दोस्रोँ": 2,
            "तेस्रोँ": 3, "पाँचाैं": 5, "पाँचाै": 5, "छैठाैं": 6,
            "छैठाै": 6, "साताैं": 7, "साताै": 7, "आठाैं": 8, "आठाै": 8,
        }
        self.assertEqual(dv.NEPALI_SEMESTER_WORDS, expected)
        for word, want in expected.items():
            self.assertEqual(dv.nepali_semester_from_words(word), want, word)
            # And in a real title, not just as a bare word.
            self.assertEqual(
                dv.nepali_semester_from_words("बी.एस.सी. %s सेमेस्टर" % word),
                want, word)

    def test_longest_word_wins_so_a_stem_never_shadows_its_ordinal(self):
        """"पहिलो" contains "पहिल". A prefix match on the stem alone would be a
        silent wrong-semester bug, so the order is pinned."""
        self.assertEqual(dv.NEPALI_SEMESTER_WORDS_LONGEST_FIRST[0].__len__(),
                         max(len(w) for w in dv.NEPALI_SEMESTER_WORDS))
        self.assertEqual(dv.nepali_semester_from_words("पहिलो"), 1)

    def test_no_ordinal_word_means_none(self):
        self.assertIsNone(dv.nepali_semester_from_words("बैठक सूचना"))
        self.assertIsNone(dv.nepali_semester_from_words(""))
        self.assertIsNone(dv.nepali_semester_from_words(None))

    def test_there_is_no_word_transliterator(self):
        """The design constraint, asserted. A Devanagari->Latin WORD
        transliterator would be a second thing to get wrong, and getting it
        wrong is a burned result, so the module must not grow one."""
        import inspect
        src = inspect.getsource(dv)
        self.assertNotIn("translit", src.lower().replace("transliterat", "")
                         .replace("transliteration", ""))


class ClassifySemesterTest(unittest.TestCase):
    """classify_semester: the frozen return shape and the frozen invariant."""

    def test_the_return_shape_is_exactly_the_frozen_contract(self):
        verdict = car.classify_semester(NEPALI_SEM4_RESULT)
        self.assertEqual(set(verdict), {"status", "semester", "batch_year",
                                        "script", "reason", "stage_hits"})
        self.assertIn(verdict["status"], ("match", "no_match", "unknown"))
        self.assertIn(verdict["script"], ("latin", "devanagari", "mixed",
                                          "empty"))
        self.assertIsInstance(verdict["reason"], str)
        self.assertEqual(set(verdict["stage_hits"]),
                         {"marker", "semester", "result"})
        for value in verdict["stage_hits"].values():
            self.assertIsInstance(value, bool)

    def test_semester_is_non_none_iff_status_is_match(self):
        """The exact contract, over every case in the table."""
        titles = [NEPALI_SEM4_RESULT, NEPALI_SEM1_RESULT, NEPALI_SEM1_BACK,
                  NEPALI_NON_RESULT, NEPALI_MEETING, NEPALI_OPAQUE,
                  NEPALI_DIGITS_ONLY, "", "TU B.Sc.CSIT IV Semester-2081 "
                  "Exam Result", "Admission open for B.Sc.CSIT",
                  "B.Sc.CSIT IV Semester form fill", "TU exam routine "
                  "published"]
        for title in titles:
            v = car.classify_semester(title)
            if v["status"] == "match":
                self.assertIsInstance(v["semester"], int, title)
                self.assertIn(v["semester"], range(1, 9), title)
            else:
                self.assertIsNone(v["semester"], title)

    def test_status_is_exactly_one_of_three(self):
        for title in (NEPALI_SEM4_RESULT, NEPALI_OPAQUE, "",
                      "TU exam routine published"):
            self.assertIn(car.classify_semester(title)["status"],
                          ("match", "no_match", "unknown"), title)

    # --- the headline case: a Devanagari result must resolve --------------
    def test_a_nepali_semester_four_result_resolves_to_four(self):
        v = car.classify_semester(NEPALI_SEM4_RESULT)
        self.assertEqual(v["status"], "match")
        self.assertEqual(v["semester"], 4)
        self.assertEqual(v["reason"], "devanagari")
        self.assertEqual(v["script"], "devanagari")
        self.assertEqual(v["stage_hits"], {"marker": True, "semester": True,
                                           "result": True})

    def test_a_nepali_first_semester_back_result_resolves_to_one(self):
        v = car.classify_semester(NEPALI_SEM1_BACK)
        self.assertEqual(v["status"], "match")
        self.assertEqual(v["semester"], 1)

    def test_devanagari_digits_feed_the_existing_batch_year_regex(self):
        """"२०८१" -> 2081 through the ALREADY-VERIFIED _BATCH_YEAR_RE, not a
        second year parser."""
        self.assertEqual(car.classify_semester(NEPALI_SEM1_RESULT)
                         ["batch_year"], 2081)
        self.assertEqual(car._extract_batch_year("वर्ष २०८१"), 2081)
        self.assertEqual(car._extract_batch_year("वर्ष २०७५"), 2075)
        # An out-of-range year is still not a batch year.
        self.assertIsNone(car._extract_batch_year("वर्ष २०५०"))
        # And the Latin path is untouched.
        self.assertEqual(car._extract_batch_year("Semester-2081 Result"), 2081)
        self.assertIsNone(car._extract_batch_year("Semester Result"))

    def test_notice_matches_student_sees_the_nepali_batch_year(self):
        """A Nepali title must go through the SAME batch rule as a Latin one,
        or a 2075 result could be read as the student's own."""
        state = seed_state()
        self.assertFalse(car.notice_matches_student(
            "बी.एस.सी. चौथो सेमेस्टर २०७५ नतिजा", 4, state),
            "an older batch with a pending semester is not this student's")
        # Rule (c) — 2082-for-failed-semester acceptance — must survive the
        # Devanagari path. It is load-bearing.
        state["semesters"]["4"]["status"] = "failed"
        self.assertTrue(car.notice_matches_student(
            "बी.एस.सी. चौथो सेमेस्टर २०७५ नतिजा", 4, state))

    # --- the fail-safe: unknown --------------------------------------------
    def test_a_nepali_notice_with_no_recognised_vocabulary_is_unknown(self):
        v = car.classify_semester(NEPALI_OPAQUE)
        self.assertEqual(v["status"], "unknown")
        self.assertEqual(v["reason"], "no_nepali_vocabulary")
        self.assertEqual(v["script"], "devanagari")
        self.assertEqual(v["semester"], None)
        self.assertEqual(v["stage_hits"], {"marker": False, "semester": False,
                                           "result": False})

    def test_digits_only_devanagari_is_unknown_not_a_silent_no_match(self):
        v = car.classify_semester(NEPALI_DIGITS_ONLY)
        self.assertEqual(v["status"], "unknown")
        self.assertIsNone(v["semester"])

    def test_an_empty_title_is_unknown_with_unreadable_script(self):
        v = car.classify_semester("")
        self.assertEqual(v["status"], "unknown")
        self.assertEqual(v["reason"], "unreadable_script")
        self.assertEqual(v["script"], "empty")

    def test_a_nepali_non_result_notice_is_no_match(self):
        """The residual gap the presence-only non-result list closes."""
        for title in (NEPALI_NON_RESULT, NEPALI_MEETING):
            v = car.classify_semester(title)
            self.assertEqual(v["status"], "no_match", title)
            self.assertEqual(v["reason"], "devanagari_non_result", title)
            self.assertEqual(v["stage_hits"]["result"], False, title)

    def test_the_non_result_list_is_presence_only(self):
        """Every term must be a real, non-empty Devanagari string, and the
        decision must be reachable by PRESENCE. Absence is never consulted."""
        self.assertTrue(dv.NEPALI_NON_RESULT_TERMS)
        for term in dv.NEPALI_NON_RESULT_TERMS:
            self.assertTrue(term.strip(), repr(term))
            self.assertRegex(term, r"[\u0900-\u097f]")
        # A title made of ONE non-result word is decided; a title made of
        # nothing we know stays unknown.
        self.assertEqual(car.classify_semester("स्मारक")["reason"],
                         "devanagari_non_result")
        self.assertEqual(car.classify_semester("छुट्टी")["reason"],
                         "devanagari_non_result")
        self.assertEqual(car.classify_semester("अज्ञात शब्द")["status"],
                         "unknown")

    def test_recognising_anything_turns_unknown_into_a_decision(self):
        """The load-bearing rule, both directions. If we recognised SOMETHING,
        the absence of the rest is evidence and no_match is correct — otherwise
        the floor would freeze on the first Nepali administrative notice."""
        # marker only
        self.assertEqual(car.classify_semester("बी.एस.सी. कार्यक्रम")["status"],
                         "no_match")
        # sem token only
        self.assertEqual(car.classify_semester("सेमेस्टर")["status"],
                         "no_match")
        # result term only
        self.assertEqual(car.classify_semester("परीक्षा")["status"],
                         "no_match")

    def test_every_no_match_reason_is_in_the_frozen_set(self):
        allowed = {"no_semester_token", "no_marker", "no_result_term",
                   "devanagari_non_result"}
        for title in (NEPALI_NON_RESULT, NEPALI_MEETING, "बी.एस.सी. कार्यक्रम",
                      "सेमेस्टर", "परीक्षा", "TU exam routine published",
                      "B.Sc.CSIT IV Semester form fill"):
            v = car.classify_semester(title)
            if v["status"] == "no_match":
                self.assertIn(v["reason"], allowed, title)

    def test_every_unknown_reason_is_in_the_frozen_set(self):
        for title in (NEPALI_OPAQUE, NEPALI_DIGITS_ONLY, "", "   "):
            v = car.classify_semester(title)
            if v["status"] == "unknown":
                self.assertIn(v["reason"], {"unreadable_script",
                                            "no_nepali_vocabulary"}, title)

    def test_every_match_reason_is_latin_or_devanagari(self):
        for title in (NEPALI_SEM4_RESULT, NEPALI_SEM1_RESULT,
                      "TU B.Sc.CSIT IV Semester-2081 Exam Result"):
            self.assertIn(car.classify_semester(title)["reason"],
                          ("latin", "devanagari"), title)

    # --- the frozen invariant ----------------------------------------------
    def test_match_semester_equals_classify_semester_semester(self):
        """THE invariant. It holds by construction: match_semester delegates
        to classify_semester."""
        for title in (NEPALI_SEM4_RESULT, NEPALI_SEM1_RESULT, NEPALI_SEM1_BACK,
                      NEPALI_NON_RESULT, NEPALI_OPAQUE, NEPALI_DIGITS_ONLY, "",
                      "TU B.Sc.CSIT IV Semester-2081 Exam Result",
                      "B.Sc.CSIT V Semester Back Result",
                      "B.Sc. CSIT 6th Semester Supplementary Result",
                      "B.Sc.CSIT VIII Semester Result",
                      "Result of the B.Sc.CSIT 4th Semester",
                      "IV Semester Result - TU B.Sc.CSIT",
                      "Admission open for B.Sc.CSIT",
                      "B.Sc.CSIT IV Semester form fill",
                      "TU exam routine published"):
            self.assertEqual(car.match_semester(title),
                             car.classify_semester(title)["semester"], title)

    def test_match_semester_still_returns_int_or_none(self):
        """The frozen return type. The Cloudflare Worker parity depends on it."""
        for title in (NEPALI_SEM4_RESULT, "TU B.Sc.CSIT IV Semester Result",
                      "", None, "nonsense"):
            value = car.match_semester(title)
            self.assertTrue(value is None or isinstance(value, int), title)
            if value is not None:
                self.assertIn(value, range(1, 9), title)

    def test_the_semester_token_constant_is_untouched(self):
        """Byte-identical, because test_worker_parity.py compares it against the
        Worker's ACADEMIC_SEM_TOKEN."""
        self.assertEqual(
            car._SEM_TOKEN,
            r"(?:VIII|8th|Eighth|VII|7th|Seventh|VI|6th|Sixth|IV|4th|Fourth"
            r"|III|3rd|Third|II|2nd|Second|V|5th|Fifth|I|1st|First)")

    def test_every_latin_ordinal_still_maps(self):
        for literal, want in (("I", 1), ("II", 2), ("III", 3), ("IV", 4),
                              ("V", 5), ("VI", 6), ("VII", 7), ("VIII", 8),
                              ("1st", 1), ("4th", 4), ("Eighth", 8)):
            self.assertEqual(car._semester_from_token(literal), want, literal)


class UnknownNeverBurnsTest(unittest.TestCase):
    """The no-burn invariant applied to the Devanagari path, end to end through
    a real run() against a temp state file."""

    ROWS = [(900, "B.Sc. CSIT", "2026-09-29"),
            (901, "B.Sc. CSIT", "2026-09-29")]
    TITLES = {900: NEPALI_OPAQUE, 901: NEPALI_SEM4_RESULT}

    def test_an_unreadable_title_defers_and_never_becomes_irrelevant(self):
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, after, _calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES,
                {900: "", 901: "Roll %s" % ROLL})
        self.assertEqual(rc, 0)
        entry = after["examined"]["900"]
        self.assertEqual(entry["outcome"], "deferred")
        self.assertEqual(entry["reason"], "title_script_unknown")
        self.assertNotEqual(entry["outcome"], "irrelevant")

    def test_the_floor_does_not_advance_past_an_unreadable_notice(self):
        with tempfile.TemporaryDirectory() as d:
            _rc, _payloads, after, _calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES,
                {900: "", 901: "Roll %s" % ROLL})
        self.assertLess(after["last_examined_notice_id"], 900,
                        "the floor moved past a notice we could not read — "
                        "that is the burn, in a new hat")
        self.assertNotIn("900", car._decided_ids(after["examined"]))

    def test_a_nepali_result_above_an_unreadable_notice_is_still_found(self):
        """The whole point: 901 is a sem-4 result in Nepali. Before this
        change it was recorded irrelevant and burned. Now the semester is
        resolved and the roll in its PDF is read."""
        with tempfile.TemporaryDirectory() as d:
            _rc, payloads, after, _calls = drive_result_pass(
                d, seed_state(), self.ROWS, self.TITLES,
                {900: "", 901: "Roll %s Passed" % ROLL},
                max_consecutive_defers=1)
        self.assertEqual(len(payloads), 1, "the sem-4 result was not announced")
        self.assertEqual(payloads[0]["semester"], 4)
        self.assertTrue(payloads[0].get("passed", True))
        self.assertEqual(after["semesters"]["4"]["status"], "passed")
        self.assertEqual(after["semesters"]["4"]["notice_id"], 901)

    def test_title_script_unknown_is_in_reasons_and_validates(self):
        self.assertIn("title_script_unknown", car.REASONS)
        state = seed_state()
        car._record(state, "examined", 900, "deferred", "title_script_unknown",
                    car._today_iso())
        # _validate_state must accept what _record wrote...
        car._validate_state(state)
        self.assertEqual(state["examined"]["900"]["reason"],
                         "title_script_unknown")
        # ...and must accept a reason string it does not itself know, so a
        # state written by a NEWER version of this script still loads here
        # (see the defensive-set note in _validate_state).
        car._record(state, "examined", 901, "deferred", "pdf_missing",
                    car._today_iso())
        state["examined"]["901"]["reason"] = "some_future_reason_token"
        car._validate_state(state)

    def test_a_non_string_reason_is_still_rejected(self):
        """The defensive set is permissive about VALUE, not about type. A
        corrupt entry must still be caught before the monitor runs."""
        state = seed_state()
        state["examined"]["900"] = {"outcome": "deferred", "reason": 42}
        with self.assertRaises(car.StateError):
            car._validate_state(state)

    def _unknown_run(self, d, rows, titles, max_consecutive_defers=None,
                     max_script_defers=None):
        """One run in which every listed notice's detail title is unreadable in
        any recognised script. Returns (rc, state, stderr)."""
        state_path = os.path.join(d, "state.json")
        preview = os.path.join(d, "preview.json")
        if not os.path.exists(state_path):
            car.save_state(state_path, seed_state())
        responses = {"notice_type=21":
                     (200, notice_list_html(rows).encode())}
        for nid in titles:
            # Real-shape detail page: the body title is what the pass reads,
            # and a page without one defers as title_unparseable, which would
            # make this test assert the wrong reason.
            responses["/notices/%d" % nid] = (
                200, iost_detail_html(NEPALI_OPAQUE).encode())
        err = io.StringIO()
        kwargs = {}
        if max_consecutive_defers is not None:
            kwargs["max_consecutive_defers"] = max_consecutive_defers
        if max_script_defers is not None:
            kwargs["max_script_defers"] = max_script_defers
        with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview), \
                mock.patch("sys.stderr", err):
            rc = car.run(state_path,
                         "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=make_fetch(responses),
                         run_exam_notices=False, result_union_urls=(), **kwargs)
        return rc, car.load_state(state_path), err.getvalue()

    def test_max_script_defers_emits_the_error_and_still_defers(self):
        """Loud, not frozen: the alert fires and the notices are STILL
        deferred — it never decides.

        Three runs, not one. The caller contract `break`s on an unreadable
        title, so a single run sees exactly ONE unknown and cannot reach the
        threshold on its own. The alert is reached on the per-id streak, which
        is the same evidence and is available on the third run. (This test
        originally drove one run and asserted a 3-in-one-run alert; that
        expectation was wrong about how the pass actually behaves.)
        """
        rows = [(910, "B.Sc. CSIT", "2026-09-29")]
        titles = {910: NEPALI_OPAQUE}
        with tempfile.TemporaryDirectory() as d:
            stderr = ""
            for _ in range(car.ACADEMIC_MAX_SCRIPT_DEFERS):
                rc, after, stderr = self._unknown_run(d, rows, titles)
        self.assertEqual(rc, 0)
        self.assertIn("::error::academic:", stderr)
        self.assertIn("title_script_unknown", stderr)
        self.assertIn("TOKEN-SET GAP", stderr)
        self.assertEqual(car.ACADEMIC_MAX_SCRIPT_DEFERS, 3)
        entry = after["examined"]["910"]
        self.assertEqual(entry["outcome"], "deferred",
                         "the alert must not turn a defer into a decision")
        self.assertEqual(entry["reason"], "title_script_unknown")

    def test_the_per_run_counter_fires_when_the_pass_does_not_break(self):
        """The other arm of the alert: several unknown titles inside ONE run.
        Reached when the head-of-line guard lets the pass continue past an id,
        which is the case where the per-id streak is still short."""
        rows = [(920 + i, "B.Sc. CSIT", "2026-09-29") for i in range(4)]
        titles = {920 + i: NEPALI_OPAQUE for i in range(4)}
        with tempfile.TemporaryDirectory() as d:
            rc, after, stderr = self._unknown_run(
                d, rows, titles, max_consecutive_defers=1)
        self.assertEqual(rc, 0)
        self.assertIn("TOKEN-SET GAP", stderr)
        self.assertIn("4 notices deferred as title_script_unknown", stderr)
        for nid in titles:
            self.assertEqual(after["examined"][str(nid)]["outcome"],
                             "deferred", nid)

    def test_below_the_threshold_no_error_is_emitted(self):
        rows = [(920 + i, "B.Sc. CSIT", "2026-09-29") for i in range(2)]
        titles = {920 + i: NEPALI_OPAQUE for i in range(2)}
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            responses = {"notice_type=21":
                         (200, notice_list_html(rows).encode())}
            for nid in titles:
                responses["/notices/%d" % nid] = (
                    200, iost_detail_html(NEPALI_OPAQUE).encode())
            err = io.StringIO()
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview), \
                    mock.patch("sys.stderr", err):
                car.run(state_path,
                        "https://iost.tu.edu.np/notices?notice_type=21",
                        dry_run=True, fetch_fn=make_fetch(responses),
                        run_exam_notices=False, result_union_urls=())
            stderr = err.getvalue()
        self.assertNotIn("TOKEN-SET GAP", stderr,
                         "2 defers is below the threshold of 3 and must be quiet")


class HeadOfLineDeferGuardTest(unittest.TestCase):
    """P1: a permanently unfixable notice must not blind the pass to every id
    above it. It must not weaken the floor either."""

    def test_the_threshold_constant_is_three(self):
        self.assertEqual(car.ACADEMIC_MAX_CONSECUTIVE_DEFERS, 3)

    def _stuck_run(self, d, runs=1):
        """One run in which notice 800 always defers (its detail fetch raises),
        and 801/802 are ordinary irrelevants above it."""
        rows = [(800, "B.Sc. CSIT", "2026-09-29"),
                (801, "B.Sc. CSIT", "2026-09-29"),
                (802, "B.Sc. CSIT", "2026-09-29")]
        state_path = os.path.join(d, "state.json")
        preview = os.path.join(d, "preview.json")
        if not os.path.exists(state_path):
            car.save_state(state_path, seed_state())
        responses = {"notice_type=21": (200, notice_list_html(rows).encode())}
        for nid in (801, 802):
            responses["/notices/%d" % nid] = (
                200, ('<html><head><title>Library opening hours</title></head>'
                      '<body></body></html>').encode())
        base = make_fetch(responses)

        def fetch(url, timeout=15):
            if "/notices/800" in url:
                raise RuntimeError("simulated host failure for 800")
            return base(url, timeout)

        err = io.StringIO()
        with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview), \
                mock.patch("sys.stderr", err):
            rc = car.run(state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                         dry_run=True, fetch_fn=fetch, run_exam_notices=False,
                         result_union_urls=())
        return rc, car.load_state(state_path), err.getvalue()

    def test_a_stuck_notice_alerts_loudly_and_stops_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(3):
                rc, after, stderr = self._stuck_run(d)
            self.assertEqual(rc, 0)
        self.assertIn("::error::academic: notice 800", stderr)
        self.assertIn("blocking the result pass", stderr)
        self.assertEqual(after["examined"]["800"]["outcome"], "deferred")

    def test_the_ids_above_a_stuck_notice_are_still_examined(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(3):
                rc, after, _stderr = self._stuck_run(d)
        for nid in ("801", "802"):
            self.assertIn(nid, after["examined"],
                          "the id above the stuck notice was never examined")
            self.assertEqual(after["examined"][nid]["outcome"], "irrelevant",
                             "and it was never stuck behind it")

    def test_the_floor_is_not_weakened_by_the_guard(self):
        """THE check. The guard must not be a quiet backdoor around R5: 800 is
        still deferred, still undecided, and the floor still cannot pass it."""
        with tempfile.TemporaryDirectory() as d:
            for _ in range(4):
                rc, after, _stderr = self._stuck_run(d)
        self.assertEqual(after["examined"]["800"]["outcome"], "deferred")
        self.assertNotIn(800, car._decided_ids(after["examined"]),
                         "a deferred id is not a decision, whatever the guard")
        # CORRECTION to an earlier expectation of mine. advance() returns the
        # highest gap-free DECIDED id, and with 800 deferred-but-listed the
        # floor parks ON 800 — not below it. That is still the floor refusing
        # to cross an undecided notice, which is the property that matters: it
        # must never reach 801 or 802, which sit above the stuck id.
        self.assertEqual(car.advance(0, {801: 1, 802: 1}, [800, 801, 802]), 800)
        self.assertLess(after["last_examined_notice_id"], 801,
                        "the floor moved past the undecided notice 800 and on "
                        "to 801 — that is the burn the guard must not permit")
        # And with 800 genuinely decided, the floor is free to advance over it.
        # NOTE the values must be entry DICTS: _decided_ids reads
        # entry["outcome"], so a bare int is "no decision recorded" and 801/802
        # would stay undecided, which is a different test entirely.
        self.assertEqual(
            car.advance(0, {"800": {"outcome": "irrelevant"},
                            "801": {"outcome": "irrelevant"},
                            "802": {"outcome": "irrelevant"}},
                        [800, 801, 802]), 802)

    def test_the_streak_resets_when_a_notice_is_finally_decided(self):
        state = seed_state()
        car._record(state, "examined", 800, "deferred", "detail_fetch_failed",
                    "2026-09-29")
        self.assertEqual(car._defer_streak(state, 800), 0)
        car._defer_and_maybe_block(state, 800, "detail_fetch_failed",
                                   "2026-09-29", 3)
        self.assertEqual(car._defer_streak(state, 800), 1)
        car._defer_and_maybe_block(state, 800, "detail_fetch_failed",
                                   "2026-09-30", 3)
        self.assertEqual(car._defer_streak(state, 800), 2)
        car._record(state, "examined", 800, "irrelevant", "no_semester_match",
                    "2026-10-01")
        self.assertEqual(car._defer_streak(state, 800), 0,
                         "a resolved notice must not keep a streak")


class ResultFeedCoverageTest(unittest.TestCase):
    """P1 (whole-board union) and P2 (rel="next" pagination), both verified
    against the live board."""

    def test_parse_next_page_url_reads_both_shapes(self):
        self.assertEqual(car.parse_next_page_url(
            '<a rel="next" href="/notices?notice_type=21&amp;page=2">Next</a>',
            "https://iost.tu.edu.np/notices?notice_type=21"),
            "https://iost.tu.edu.np/notices?notice_type=21&page=2")
        self.assertEqual(car.parse_next_page_url(
            '<link rel="next" href="https://iost.tu.edu.np/notices?page=2" />',
            "https://iost.tu.edu.np/notices"),
            "https://iost.tu.edu.np/notices?page=2")
        self.assertIsNone(car.parse_next_page_url("<html>no pagination</html>",
                                                  "https://x/"))
        self.assertIsNone(car.parse_next_page_url(
            '<a rel="next" href="javascript:void(0)">Next</a>', "https://x/"))
        self.assertIsNone(car.parse_next_page_url("", "https://x/"))

    def test_pagination_follows_rel_next_and_merges_ids(self):
        """Verified live: page 2 of the result feed adds 15313/15314/15395/
        15396/15397. Only page 1 was ever fetched."""
        page1 = notice_list_html([(100, "A", "2026-09-01")]).replace(
            "</body>",
            '<a rel="next" href="/notices?notice_type=21&amp;page=2">N</a>'
            "</body>")
        page2 = notice_list_html([(100, "A", "2026-09-01"),
                                  (101, "B", "2026-09-02")])
        calls = []

        def fetch(url, timeout=15):
            calls.append(url)
            if "page=2" in url:
                return 200, page2.encode()
            return 200, page1.encode()

        notices = car.fetch_notice_list(
            "https://iost.tu.edu.np/notices?notice_type=21", fetch_fn=fetch)
        self.assertEqual([n["id"] for n in notices], [100, 101])
        self.assertEqual(len(calls), 2, "page 2 was never fetched before")

    def test_pagination_is_bounded(self):
        pages = {}
        for i in range(1, 9):
            body = notice_list_html([(100 + i, "x", "2026-09-01")]).replace(
                "</body>",
                '<a rel="next" href="/notices?p=%d">N</a></body>' % (i + 1))
            pages[i] = body
        calls = []

        def fetch(url, timeout=15):
            calls.append(url)
            for i, body in pages.items():
                if ("p=%d" % i) in url or (i == 1 and "p=" not in url):
                    return 200, body.encode()
            return 200, pages[1].encode()

        notices = car.fetch_notice_list(
            "https://iost.tu.edu.np/notices?notice_type=21", fetch_fn=fetch)
        self.assertEqual(len(calls), car._RESULT_LIST_MAX_PAGES)
        self.assertEqual(car._RESULT_LIST_MAX_PAGES, 3)
        self.assertGreaterEqual(len(notices), 3)

    def test_a_failure_on_a_following_page_warns_and_keeps_page_one(self):
        """Not exit 2. The primary feed parsed fine; refusing its notices
        because page 2 timed out would be self-inflicted blindness."""
        page1 = notice_list_html([(100, "A", "2026-09-01")]).replace(
            "</body>",
            '<a rel="next" href="/notices?page=2">N</a></body>')

        def fetch(url, timeout=15):
            if "page=2" in url:
                raise RuntimeError("boom")
            return 200, page1.encode()

        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            notices = car.fetch_notice_list(
                "https://iost.tu.edu.np/notices?notice_type=21",
                fetch_fn=fetch)
        self.assertEqual([n["id"] for n in notices], [100])
        self.assertIn("::warning::", err.getvalue())

    def test_a_failure_on_the_first_page_still_raises(self):
        def fetch(url, timeout=15):
            raise RuntimeError("boom")

        with self.assertRaises(car.FetchError):
            car.fetch_notice_list("https://iost.tu.edu.np/notices",
                                  fetch_fn=fetch)

    def test_the_whole_board_feed_is_unioned_into_the_result_feed(self):
        """Verified live: notice 15642 is on /notices but NOT on
        notice_type=21, so it was invisible to the result pass."""
        result_rows = [(15700, "B.Sc. CSIT", "2026-09-28")]
        board_rows = [(15642, "बी.एस.सी. चौथो सेमेस्टर नतिजा", "2026-09-28")]
        detail_15642 = iost_detail_html(
            NEPALI_SEM4_RESULT, pdf_url="https://portal.tu.edu.np/p/15642.pdf")
        detail_15700 = iost_detail_html("Library extended opening hours")

        def fetch(url, timeout=15):
            if "/notices/15642" in url:
                return 200, detail_15642.encode()
            if "/notices/15700" in url:
                return 200, detail_15700.encode()
            if "notice_type=21" in url:
                return 200, notice_list_html(result_rows).encode()
            if url.rstrip("/").endswith("/notices"):
                return 200, notice_list_html(board_rows).encode()
            if "portal.tu.edu.np" in url:
                return 200, build_one_page_pdf(roll="1357 2468")
            raise RuntimeError("no fake response for %s" % url)

        self.assertIn("https://iost.tu.edu.np/notices",
                      car.DEFAULT_RESULT_FEED_UNION_URLS)
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            err = io.StringIO()
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview), \
                    mock.patch("sys.stderr", err):
                rc = car.run(state_path,
                             "https://iost.tu.edu.np/notices?notice_type=21",
                             dry_run=True, fetch_fn=fetch,
                             run_exam_notices=False)
            payloads = []
            if os.path.exists(preview):
                with open(preview, encoding="utf-8") as fh:
                    payloads = json.load(fh)
        self.assertEqual(rc, 0)
        self.assertIn("unioned 1 notice", err.getvalue())
        self.assertEqual(len(payloads), 1,
                         "the result filed under a notice_type != 21 was "
                         "missed — that is the blind spot this closes")
        self.assertEqual(payloads[0]["semester"], 4)
        self.assertEqual(payloads[0]["kind"], "academic_result")

    def test_a_union_failure_warns_and_does_not_take_the_run_down(self):
        result_rows = [(15700, "B.Sc. CSIT", "2026-09-28")]

        def fetch(url, timeout=15):
            if "notice_type=21" in url:
                return 200, notice_list_html(result_rows).encode()
            raise RuntimeError("board down")

        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            responses = {"/notices/15700": (
                200, ('<html><head><title>Library hours</title></head>'
                      '<body></body></html>').encode())}
            base = make_fetch(responses)

            def fetch2(url, timeout=15):
                if "notice_type=21" in url or url.rstrip("/").endswith(
                        "/notices"):
                    return fetch(url, timeout)
                return base(url, timeout)

            err = io.StringIO()
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview), \
                    mock.patch("sys.stderr", err):
                rc = car.run(state_path,
                             "https://iost.tu.edu.np/notices?notice_type=21",
                             dry_run=True, fetch_fn=fetch2,
                             run_exam_notices=False)
        self.assertEqual(rc, 0, "a board outage must not be exit 2 once the "
                                "primary result feed parsed")
        self.assertIn("::warning::", err.getvalue())


class RealPdfInTheResultPassTest(unittest.TestCase):
    """The result pass, driven with a genuinely parseable PDF instead of the
    23-byte literal that stood in for one until now."""

    def test_a_real_pdf_carries_the_owner_through_the_whole_pass(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            pdf = build_one_page_pdf(roll="1357 2468")
            # The REAL detail-page shape (body title, site-constant <title>),
            # not a convenient fixture: a detail page with no body title
            # defers as title_unparseable and would prove nothing.
            detail = iost_detail_html(
                "TU B.Sc.CSIT IV Semester-2082 Exam Result",
                pdf_url="https://portal.tu.edu.np/p/15800.pdf")
            responses = {
                "notice_type=21": (200, notice_list_html(
                    [(15800, "B.Sc. CSIT", "2026-09-29")]).encode()),
                "/notices/15800": (200, detail.encode()),
                "portal.tu.edu.np": (200, pdf),
            }
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc = car.run(state_path,
                             "https://iost.tu.edu.np/notices?notice_type=21",
                             dry_run=True, fetch_fn=make_fetch(responses),
                             run_exam_notices=False, result_union_urls=())
            payloads = []
            if os.path.exists(preview):
                with open(preview, encoding="utf-8") as fh:
                    payloads = json.load(fh)
            after = car.load_state(state_path)
        self.assertEqual(rc, 0)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["semester"], 4)
        self.assertTrue(payloads[0]["passed"])
        self.assertEqual(after["semesters"]["4"]["status"], "passed")
        self.assertEqual(after["examined"]["15800"]["outcome"], "processed")


class CsitMarkerGapDeferTest(unittest.TestCase):
    """DEFECT 1 (HIGH): the CSIT marker was still a hard REJECT gate.

    `_match_semester_latin` requires marker AND semester token AND result term,
    so a marker-less title resolved `semester = None` even though a semester
    token AND a result term had both been positively recognised. The old
    `any(stage_hits)` branch then read "I recognised something" as a DECISION,
    `_process_result_feed` recorded `irrelevant` / `no_semester_match`, R4 made
    it permanent, and the floor advanced. That is the shape that burned the
    owner's semester-1 result for ~19 hours, and it burns a semester-4 result
    the same way: the detail title "Fourth Semester Result 2082" with a
    downloadable PDF containing the roll is not our spelling, but nothing
    about it proves the result is not ours.

    The fix may NOT simply drop the marker: this feed is not CSIT-only (live
    notices 15398 / 15672 are B.Tech, 15442-15444 / 15565 / 15697 are M.Sc.,
    15671 is a joint B.Sc.CSIT + BIT calendar). So an observed non-CSIT marker
    makes those a POSITIVE no_match, and anything else with a semester token
    and a result term DEFERS.
    """

    MARKERLESS = "Fourth Semester Result 2082"
    MSC = "M.Sc. Geology I Semester-2082 Exam Result"
    BTECH = "B.Tech.(Food Technology) I Semester-2082 Exam Result"
    ROLL_TEXT = "Roll Number 1357 2468 Result PASSED"

    def _run(self, d, rows, detail_titles, pdf_text, runs=1, seed=None):
        """Drive `rows` through run() `runs` times over ONE state file.

        Returns (payloads_per_run, state_after, stderr). Reusing the state file
        is the whole point: a burn is only visible on the SECOND run, where the
        notice is no longer pending and nothing re-opens it.
        """
        state_path = os.path.join(d, "state.json")
        preview = os.path.join(d, "preview.json")
        if not os.path.exists(state_path):
            car.save_state(state_path, seed if seed is not None else seed_state())
        responses = {"notice_type=21": (200, notice_list_html(rows).encode())}
        for nid, title in detail_titles.items():
            responses["/notices/%d" % nid] = (200, iost_detail_html(
                title, "https://portal.tu.edu.np/notice/9300/x.pdf").encode())
        responses["portal.tu.edu.np"] = (200, fake_pdf_bytes())
        fetch = make_fetch(responses)
        per_run = []
        err = io.StringIO()
        with mock.patch.object(car, "extract_pdf_text",
                               return_value=pdf_text), \
             mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview), \
             mock.patch("sys.stderr", err):
            for _ in range(runs):
                if os.path.exists(preview):
                    os.remove(preview)
                rc = car.run(
                    state_path, "https://iost.tu.edu.np/notices?notice_type=21",
                    dry_run=True, fetch_fn=fetch, run_exam_notices=False,
                    result_union_urls=())
                got = []
                if os.path.exists(preview):
                    with open(preview, encoding="utf-8") as fh:
                        got = json.load(fh)
                per_run.append((rc, got))
        return per_run, car.load_state(state_path), err.getvalue()

    # ---- the burn itself -------------------------------------------------
    def test_markerless_sem4_defers_and_the_floor_never_moves(self):
        with tempfile.TemporaryDirectory() as d:
            per_run, after, _err = self._run(
                d, [(15696, "Some notice", "2026-09-29")],
                {15696: self.MARKERLESS}, self.ROLL_TEXT, runs=3)
            for rc, payloads in per_run:
                self.assertEqual(rc, 0)
                self.assertEqual(payloads, [],
                                 "nothing may be announced off a deferral")
            entry = after["examined"]["15696"]
            self.assertEqual(entry["outcome"], "deferred")
            self.assertEqual(entry["reason"], "no_csit_marker")
            self.assertLess(after["last_examined_notice_id"], 15696,
                            "the floor advanced past an undecided notice - "
                            "that IS the burn")
            self.assertNotIn(15696, car._decided_ids(after["examined"]),
                             "a deferred id is not a decision")
            self.assertEqual(after["semesters"]["4"]["status"], "pending",
                             "semester 4 must still be pending, never decided")

    def test_the_markerless_notice_is_re_opened_on_every_run(self):
        """R1. It must still be pending, or 'deferred' is only cosmetic."""
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            self._run(d, [(15696, "Some notice", "2026-09-29")],
                      {15696: self.MARKERLESS}, self.ROLL_TEXT, runs=1)
            after = car.load_state(state_path)
            floor = after["last_examined_notice_id"] or 0
            decided = car._decided_ids(after["examined"])
            pending, _overflow = car._select_pending([15696], floor, decided,
                                                     car.MAX_PENDING)
            self.assertEqual(pending, [15696],
                             "a deferred notice must stay pending")

    def test_the_gap_is_loud_within_one_run(self):
        """A systematic marker gap must scream, not defer in silence.

        The same two-signal shape as the Devanagari token gap: the per-run
        counter cannot arm inside a single loop iteration while the head-of-line
        guard still blocks on the first deferral, so the per-id streak carries
        the first three runs. By the threshold run the error is printed inside
        that run's loop - not one semester later.
        """
        with tempfile.TemporaryDirectory() as d:
            _per_run, after, err = self._run(
                d, [(9000, "Some notice", "2026-09-29")],
                {9000: self.MARKERLESS}, self.ROLL_TEXT,
                runs=car.ACADEMIC_MAX_SCRIPT_DEFERS)
            self.assertIn("::error::academic", err)
            self.assertIn("NO CSIT marker", err)
            self.assertEqual(after["examined"]["9000"]["outcome"], "deferred")

    # ---- the marker must stay load-bearing -------------------------------
    def test_other_programmes_still_reject_positively(self):
        """Dropping the marker requirement is NOT the fix, and this is the
        test that says so: an M.Sc. result and a B.Tech result must still be
        no_match - now on a POSITIVE marker, not on an absence."""
        for title in (self.MSC, self.BTECH,
                      "M.Sc. Microbiology III Semester-2081 Exam Result",
                      "M.Sc. IV Semester-2080 Make Up Exam Center Notice"):
            v = car.classify_semester(title)
            self.assertEqual(v["status"], "no_match", (title, v))
            self.assertEqual(v["reason"], "other_program", (title, v))
            self.assertIsNone(v["semester"], title)

    def test_the_joint_csit_bit_calendar_is_not_claimed_as_other_program(self):
        """Live notice 15671 names CSIT AND BIT. The CSIT marker wins; it is
        not a result notice and must be a decision, not a defer."""
        title = ("Academic Calendar of B.Sc.CSIT and Bachelor in Information "
                 "Technology (BIT) V Semester-2081 Batch")
        v = car.classify_semester(title)
        self.assertEqual(v["status"], "no_match", v)
        self.assertNotEqual(v["reason"], "other_program", v)
        self.assertNotEqual(v["status"], "unknown", v)

    def test_a_csit_result_is_still_claimed_not_deferred(self):
        for title in ("B.Sc.CSIT I Semester-2082 Exam Result",
                      "B.Sc.CSIT Fourth Semester Result 2082"):
            v = car.classify_semester(title)
            self.assertEqual(v["status"], "match", (title, v))
            self.assertEqual(v["semester"] in (1, 4), True, (title, v))

    # ---- the frozen contracts -------------------------------------------
    def test_the_pure_delegation_invariant_still_holds(self):
        for title in (self.MARKERLESS, self.MSC, self.BTECH, "",
                      "   ", "Library extended opening hours",
                      "B.Sc.CSIT IV Semester form fill",
                      "\u0938\u094d\u0928\u093e\u0924\u0915\u094b\u0924\u094d"
                      "\u0924\u0930 \u0924\u0939 \u0926\u093e\u0947\u0936\u094d"
                      "\u0930\u093e\u0947 \u0935\u0930\u094d\u0937 "
                      "\u092a\u0930\u0940\u0915\u094d\u0937\u093e"):
            self.assertEqual(car.match_semester(title),
                             car.classify_semester(title)["semester"], title)

    def test_the_new_reasons_are_recorded_and_both_validators_accept_them(self):
        self.assertIn("no_csit_marker", car.REASONS)
        self.assertIn("no_semester_number", car.REASONS)
        for reason in ("no_csit_marker", "no_semester_number"):
            state = seed_state()
            car._record(state, "examined", 900, "deferred", reason,
                        "2026-09-30")
            # A state carrying a reason this version may not know must still
            # load, or the monitor refuses to run at all (exit 3).
            self.assertIsNotNone(
                car._validate_state(json.loads(json.dumps(state))), reason)


class NotificationLogRedactionTest(unittest.TestCase):
    """The delivered payload carries the roll; the public LOG must not.

    Until 2026-10-27 the live branch printed the payload and the workflow teed
    stdout into state/academic_live_payload.jsonl, so every live run wrote the
    owner's roll number into a public Actions log. The delivery file still needs
    it (the ntfy message and the rendered page both show it), so the roll is
    masked in the printed COPY only.
    """

    ROLL = "13572468"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="academic-log-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.live = os.path.join(self.tmp, "live.jsonl")
        patcher = mock.patch.object(car, "NOTIFY_LIVE_PATH", self.live)
        patcher.start()
        self.addCleanup(patcher.stop)
        car._LIVE_WRITTEN["started"] = False

    def _payload(self, semester=1):
        roll = self.ROLL
        return {
            "kind": "academic_result",
            "semester": semester,
            "roll_number": roll,
            "passed": True,
            "status": "passed",
            "result_date": "2026-09-29",
            "title": "Semester %d PASS" % semester,
            "message": ("Roll %s cleared semester %d. The split form %s too."
                        % (roll, semester, roll[:4] + " " + roll[4:])),
            "ntfy": {"topic_hint": "academic", "message": "Roll %s passed" % roll},
            "sections": [{"heading": "Roll %s" % roll, "body": "ok"}],
        }

    def test_the_printed_copy_carries_no_roll_and_no_split_form(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            car.emit_notification(self._payload(), dry_run=False)
        out = buf.getvalue()
        self.assertNotIn(self.ROLL, out,
                         "the printed copy still carries the roll number")
        self.assertNotIn(self.ROLL[:4] + " " + self.ROLL[4:], out,
                         "the four-four split form is still printable")
        self.assertIn(car.ROLL_MASK, out, "nothing was masked at all")

    def test_the_delivery_file_still_carries_the_real_roll(self):
        with contextlib.redirect_stdout(io.StringIO()):
            car.emit_notification(self._payload(), dry_run=False)
        with io.open(self.live, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(1, len(lines), "one payload must be one line")
        delivered = json.loads(lines[0])
        self.assertEqual(self.ROLL, delivered["roll_number"],
                         "the delivery file lost the roll, so ntfy and the "
                         "rendered page would have nothing to show")
        self.assertIn(self.ROLL, delivered["ntfy"]["message"])

    def test_the_printed_line_is_still_one_parseable_object_per_payload(self):
        for semester in (1, 2, 3):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                car.emit_notification(self._payload(semester), dry_run=False)
            json.loads(buf.getvalue())
        with io.open(self.live, encoding="utf-8") as fh:
            self.assertEqual(3, len(fh.read().splitlines()),
                             "three payloads must be three lines in the file")


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(AcademicResultTest),
        loader.loadTestsFromTestCase(NoBurnContractTest),
        loader.loadTestsFromTestCase(ReviewerReproTest),
        loader.loadTestsFromTestCase(P2EfficiencyTest),
        loader.loadTestsFromTestCase(ResultPassBudgetTest),
        loader.loadTestsFromTestCase(JustCreatedSemesterKeyTest),
        loader.loadTestsFromTestCase(SiteConstantTitleTest),
        loader.loadTestsFromTestCase(FalseFailedGuardTest),
        loader.loadTestsFromTestCase(RollFoundNormalisationTest),
        loader.loadTestsFromTestCase(CsitMarkerGapDeferTest),
        loader.loadTestsFromTestCase(NotificationLogRedactionTest),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
