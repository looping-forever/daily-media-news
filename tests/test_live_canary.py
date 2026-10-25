"""Offline tests for the live-parser canary (contract 1g).

WHAT THIS SUITE IS
------------------
Every assertion in ``scripts/live_canary.py`` is a pure function
``assert_X(blob) -> (ok, detail)``. That is not a stylistic preference: it is
what makes the canary LOUD in CI and OFFLINE-SAFE here. Each of the 15
assertions is fed a synthetic blob that should pass and one that should fail,
and the failing blob is checked for the EXACT frozen failure string, because a
canary that reports a different sentence than the one the runbook tells the
owner to grep for is a canary nobody can act on.

``collect()`` is NEVER called from this file. It is the only function that
touches the network, and a test that calls it would make this suite flaky,
slow and dependent on the mood of news.google.com.

Stdlib only, fully offline. Nothing here writes outside a temp directory.
"""

import copy
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import live_canary as LC          # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic blobs. Shaped like the real collect_news.py / collect() output, and
# deliberately NOT copied from the hand-written fixture style the product tests
# use -- see tests/fixtures/real/ for genuine captured bytes.
# ---------------------------------------------------------------------------

def _row(name, kind, outcome, items_found, category="", host="", url="",
         error=""):
    return {
        "name": name, "kind": kind, "category": category, "host": host,
        "url": url, "outcome": outcome, "error": error, "attempts": 1,
        "secs": 1.0, "items_found": items_found,
    }


def _cand(title, url, source, published):
    return {"title": title, "url": url, "source": source,
            "published": published, "snippet": "",
            "domain_category": "nepal", "language": "en"}


def good_digest():
    """A digest blob a healthy collector would produce."""
    return {
        "source_results": [
            _row("gnews:np-en:nepal", "gnews", "ok", 12,
                 category="nepal", host="news.google.com",
                 url="https://news.google.com/rss/search?q=nepal"),
            _row("gnews:en-us:\"Genshin\"", "gnews", "ok", 3,
                 category="entertainment", host="news.google.com"),
            _row("notice:iost.tu.edu.np", "notice", "ok", 1,
                 category="education_tu", host="iost.tu.edu.np",
                 url="https://iost.tu.edu.np/"),
            _row("weather:open-meteo", "weather", "ok", 1),
        ],
        "candidates": [
            _cand("Landslide blocks highway in Example District",
                  "https://kathmandupost.com/landslide", "kathmandupost.com",
                  "2026-09-28T21:35:00+00:00"),
            _cand("B.Sc.CSIT I Semester exam result published",
                  "https://iost.tu.edu.np/notice/15673",
                  "iost.tu.edu.np", "2026-09-28T04:00:00+00:00"),
        ],
    }


def good_academic():
    """An academic blob a healthy run would produce."""
    return {
        "result_list_html": (
            '<a href="/notices/15673">B.Sc.CSIT I Semester-2082 Exam Result</a>'
            '<a href="/notices/15398">B.Tech I Semester-2082 Exam Result</a>'
            '<a href="/notices/15442">M.Sc. Food Technology Exam Result</a>'),
        "feeds_fetched": ["iost_result", "iost_all"],
        "details": [
            {"id": 15673, "title": "B.Sc.CSIT I Semester-2082 Exam Result",
             "title_source": "container_h4", "title_degraded": False,
             "pdf_url": "https://portal.tu.edu.np/notice/15673/x.pdf"},
        ],
        "pdfs": [
            {"notice_id": 15673, "text": "1357 2468 3.5 GPA",
             "deferred": False, "deferral_reason": ""},
        ],
    }


def good_blob():
    return {"digest": good_digest(), "academic": good_academic()}


# ---------------------------------------------------------------------------
# D1
# ---------------------------------------------------------------------------

class D1Tests(unittest.TestCase):
    FAIL = "canary D1: no gnews task produced any item"

    def test_ok_when_a_gnews_task_produced_items(self):
        ok, detail = LC.assert_d1_gnews_produced(good_digest())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_every_gnews_task_was_empty(self):
        """A 200 that parsed to zero items is NOT a network failure.

        This is the load-bearing negative: `outcome == "ok"` with
        `items_found == 0` is the exact shape of a feed reader that has quietly
        stopped reading, and treating it as a pass is what lets the blind run
        look healthy.
        """
        blob = good_digest()
        for row in blob["source_results"]:
            if row["kind"] == "gnews":
                row["items_found"] = 0
        ok, detail = LC.assert_d1_gnews_produced(blob)
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)

    def test_fails_when_every_gnews_task_failed(self):
        blob = {"source_results": [
            _row("gnews:np-en:nepal", "gnews", "failed", 0,
                 error="HTTP 503")]}
        ok, detail = LC.assert_d1_gnews_produced(blob)
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)

    def test_fails_on_an_empty_blob(self):
        ok, detail = LC.assert_d1_gnews_produced({})
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)

    def test_items_found_guard_distinguishes_absent_field_from_zero(self):
        """The D1 field guard: absent field -> PARTIAL, present-and-zero -> FAIL.

        Collapsing those two would either crash the canary on a peer's WIP or
        silently stop checking the most important digest assertion there is.
        """
        with_field = good_digest()
        for row in with_field["source_results"]:
            if row["kind"] == "gnews":
                row["items_found"] = 0
        self.assertTrue(LC.items_found_present(with_field),
                        "a row carrying items_found=0 still HAS the field")
        without = good_digest()
        for row in without["source_results"]:
            row.pop("items_found", None)
        self.assertFalse(LC.items_found_present(without),
                         "a v1 record with no items_found is a PARTIAL run")


# ---------------------------------------------------------------------------
# D2
# ---------------------------------------------------------------------------

class D2Tests(unittest.TestCase):
    FAIL = "canary D2: every candidate title is site chrome"

    def test_ok_when_a_content_title_is_present(self):
        ok, detail = LC.assert_d2_titles_not_chrome(good_digest())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_every_title_is_chrome(self):
        blob = good_digest()
        blob["candidates"] = [
            _cand("INSTITUTE OF SCIENCE AND TECHNOLOGY", "https://x/1", "x", None),
            _cand("Notices", "https://x/2", "x", None),
        ]
        ok, detail = LC.assert_d2_titles_not_chrome(blob)
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)

    def test_fails_with_no_candidates_at_all(self):
        ok, detail = LC.assert_d2_titles_not_chrome(
            {"source_results": [], "candidates": []})
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)


# ---------------------------------------------------------------------------
# D3
# ---------------------------------------------------------------------------

class D3Tests(unittest.TestCase):
    FAIL = "canary D3: no candidate carried a real pubDate"

    def test_ok_when_a_candidate_has_a_real_date(self):
        ok, detail = LC.assert_d3_pubdate_present(good_digest())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_every_date_is_epoch(self):
        blob = good_digest()
        for cand in blob["candidates"]:
            cand["published"] = "1970-01-01T00:00:00+00:00"
        ok, detail = LC.assert_d3_pubdate_present(blob)
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)

    def test_fails_when_dates_are_absent(self):
        blob = good_digest()
        for cand in blob["candidates"]:
            cand["published"] = None
        ok, detail = LC.assert_d3_pubdate_present(blob)
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)

    def test_accepts_an_rfc822_pubdate(self):
        blob = good_digest()
        blob["candidates"] = [
            _cand("Headline about the flood", "https://x/1", "x",
                  "Mon, 28 Sep 2026 21:35:00 GMT")]
        ok, detail = LC.assert_d3_pubdate_present(blob)
        self.assertTrue(ok, detail)


# ---------------------------------------------------------------------------
# D4
# ---------------------------------------------------------------------------

class D4Tests(unittest.TestCase):
    @staticmethod
    def _blob(title, words=5):
        cand = _cand(title, "https://iost.tu.edu.np/notice/15673",
                     "iost.tu.edu.np", "2026-09-28T04:00:00+00:00")
        del cand["title"]
        cand["title"] = title
        return {
            "source_results": [
                _row("notice:iost.tu.edu.np", "notice", "ok", 1,
                     category="education_tu", host="iost.tu.edu.np",
                     url="https://iost.tu.edu.np/"),
            ],
            "candidates": [cand],
        }

    def test_ok_with_four_words_and_a_notice_keyword(self):
        ok, detail = LC.assert_d4_notice_is_not_chrome(
            self._blob("B.Sc.CSIT I Semester-2082 Exam Result notice published"))
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_on_nav_chrome(self):
        """Eight words, no notice keyword: a long nav label is still chrome.

        Note the deliberate choice of example. "Interest Rate And Branch
        Locator" would NOT fail D4 -- it is 5 words and carries a whole-word
        keyword, which is precisely the weak-keyword case the collector itself
        handles. Testing D4 against it would assert the opposite of the
        contract.
        """
        ok, detail = LC.assert_d4_notice_is_not_chrome(
            self._blob("Welcome To The Institute Of Science And Technology"))
        self.assertFalse(ok)
        self.assertEqual(
            "canary D4: notice source notice:iost.tu.edu.np yielded only nav chrome",
            detail)

    def test_keyword_alone_is_not_enough(self):
        """"Deposit Rates" has the keyword and only 2 words -- still chrome."""
        ok, _ = LC.assert_d4_notice_is_not_chrome(self._blob("Deposit Rates"))
        self.assertFalse(ok)

    def test_many_words_without_a_keyword_is_still_chrome(self):
        ok, _ = LC.assert_d4_notice_is_not_chrome(
            self._blob("Welcome To The Institute Of Science And Technology"))
        self.assertFalse(ok)

    def test_fails_when_no_notice_source_was_collected(self):
        ok, detail = LC.assert_d4_notice_is_not_chrome(
            {"source_results": [_row("weather:x", "weather", "ok", 1)]})
        self.assertFalse(ok)
        self.assertEqual("canary D4: no notice source was collected at all", detail)

    def test_a_later_source_satisfies_d4_when_the_first_is_empty(self):
        """THE REGRESSION THIS FIX EXISTS FOR. The old body `return`ed inside
        the loop, so the FIRST notice source decided the outcome: with
        bank.example.com (0 candidates) first, four healthy sources behind it
        were never even looked at. Measured per source on the live board:
        examplebank 0, tu.edu.np 2, iost 6, college 3, nea 2."""
        blob = {
            "source_results": [
                _row("notice:bank.example.com", "notice", "ok", 0,
                     category="banking", host="bank.example.com"),
                _row("notice:iost.tu.edu.np", "notice", "ok", 1,
                     category="education_tu", host="iost.tu.edu.np"),
            ],
            "candidates": [
                _cand("M.Sc. IV Semester-2080 Make Up Exam Center Notice",
                      "https://iost.tu.edu.np/notices/15697",
                      "iost.tu.edu.np", "2026-09-28T04:00:00+00:00"),
            ],
        }
        ok, detail = LC.assert_d4_notice_is_not_chrome(blob)
        self.assertTrue(ok, detail)
        self.assertIn("bank.example.com", detail,
                      "an empty source is real truth and must be REPORTED")
        self.assertIn("iost.tu.edu.np", detail)

    def test_fails_only_when_no_source_anywhere_satisfies_it(self):
        """Empty and nav-chrome are different failures and both are reported,
        because they call for opposite responses (an upstream outage vs an
        anchor scan walking the nav frame)."""
        blob = {
            "source_results": [
                _row("notice:bank.example.com", "notice", "ok", 0,
                     category="banking", host="bank.example.com"),
                _row("notice:nea.org.np", "notice", "ok", 1,
                     category="energy", host="nea.org.np"),
            ],
            "candidates": [
                _cand("Welcome To The Institute Of Science And Technology",
                      "https://nea.org.np/", "nea.org.np",
                      "2026-09-28T04:00:00+00:00"),
            ],
        }
        ok, detail = LC.assert_d4_notice_is_not_chrome(blob)
        self.assertFalse(ok)
        self.assertIn("bank.example.com", detail)
        self.assertIn("0 candidates", detail)
        self.assertIn("only nav chrome", detail)


# ---------------------------------------------------------------------------
# D5
# ---------------------------------------------------------------------------

class D5Tests(unittest.TestCase):
    FAIL_TAIL = "against live sources"

    def test_ok_when_health_is_not_degraded(self):
        ok, detail = LC.assert_d5_source_health_not_degraded(good_digest())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_a_critical_host_is_down(self):
        """A failed critical host is what `degraded` means; assert the string."""
        blob = good_digest()
        blob["source_results"] = [
            _row("gnews:np-en:nepal", "gnews", "ok", 12,
                 category="nepal", host="news.google.com"),
            _row("gnews:en-us:x", "gnews", "ok", 4,
                 category="entertainment", host="news.google.com"),
            _row("notice:iost.tu.edu.np", "notice", "failed", 0,
                 category="education_tu", host="iost.tu.edu.np",
                 error="HTTP 503 Service Unavailable"),
        ]
        ok, detail = LC.assert_d5_source_health_not_degraded(blob)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("canary D5: source_health is "), detail)
        self.assertTrue(detail.endswith(self.FAIL_TAIL), detail)

    def test_uses_the_products_own_computation(self):
        """Not a reimplementation: the canary asserts the number digest prints."""
        import source_health as SH
        blob = good_digest()
        expected = SH.compute_source_health(blob["source_results"],
                                            LC._critical_hosts())
        _, detail = LC.assert_d5_source_health_not_degraded(blob)
        self.assertIn(str(expected.get("state")), detail)


# ---------------------------------------------------------------------------
# D6
# ---------------------------------------------------------------------------

class D6Tests(unittest.TestCase):
    FAIL = "canary D6: an empty category rendered the confident B4 string"

    def test_ok_when_no_category_has_empty_hosts(self):
        ok, detail = LC.assert_d6_empty_category_is_honest(good_digest())
        self.assertTrue(ok, detail)

    def test_fails_when_an_empty_category_renders_the_b4_string(self):
        """The anti-fabrication lie: a CHECKED source with nothing usable must
        not borrow B4's confident "nothing new".

        D6 is the one digest assertion that asserts about the RENDERER rather
        than about the collector, so the failing case has to break the RENDERER:
        `section_status_line` is stubbed to return the B4 literal for a
        category that genuinely has empty hosts. The product is correct today,
        which is exactly why D6 has to be mutation-proven -- otherwise it would
        be a test that can only ever pass.
        """
        blob = {
            "source_results": [
                _row("gnews:np-en:nepal", "gnews", "ok", 0,
                     category="nepal", host="news.google.com"),
            ],
            "candidates": [],
        }
        health = LC.SH.compute_source_health(blob["source_results"],
                                             LC._critical_hosts())
        cat = sorted(health["by_category"])[0]
        self.assertTrue(health["by_category"][cat]["empty_hosts"],
                        "fixture must actually produce an empty host")
        # Sanity: the REAL renderer does not emit B4 for this category today.
        self.assertNotEqual(LC.B4_CONFIDENT,
                            LC.section_status_line(cat, 0, health))
        real = LC.section_status_line
        try:
            LC.section_status_line = lambda c, n, h: LC.B4_CONFIDENT
            ok, detail = LC.assert_d6_empty_category_is_honest(blob)
        finally:
            LC.section_status_line = real
        self.assertFalse(ok)
        self.assertEqual(self.FAIL, detail)

    def test_the_honest_line_passes_for_an_empty_category_today(self):
        """The same category with the renderer intact: B6's honest line, ok."""
        blob = {
            "source_results": [
                _row("gnews:np-en:nepal", "gnews", "ok", 0,
                     category="nepal", host="news.google.com"),
            ],
            "candidates": [],
        }
        health = LC.SH.compute_source_health(blob["source_results"],
                                             LC._critical_hosts())
        cat = sorted(health["by_category"])[0]
        self.assertIn("nothing usable",
                      LC.section_status_line(cat, 0, health).lower())
        ok, detail = LC.assert_d6_empty_category_is_honest(blob)
        self.assertTrue(ok, detail)

    def test_checks_the_smallest_non_weather_category(self):
        blob = {
            "source_results": [
                _row("gnews:a", "gnews", "ok", 5, category="big", host="news.google.com"),
                _row("gnews:b", "gnews", "ok", 5, category="big", host="news.google.com"),
                _row("gnews:c", "gnews", "ok", 5, category="big", host="news.google.com"),
                _row("notice:iost", "notice", "ok", 1, category="small",
                     host="iost.tu.edu.np"),
                _row("weather:x", "weather", "ok", 1, category=""),
            ],
            "candidates": [],
        }
        ok, detail = LC.assert_d6_empty_category_is_honest(blob)
        self.assertTrue(ok, detail)
        self.assertIn("small", detail, "must check the SMALLEST category")


# ---------------------------------------------------------------------------
# D7
# ---------------------------------------------------------------------------

class D7Tests(unittest.TestCase):
    def _blob(self, url):
        return {
            "source_results": [
                _row("notice:iost.tu.edu.np", "notice", "ok", 1,
                     category="education_tu", host="iost.tu.edu.np"),
            ],
            "candidates": [
                _cand("B.Sc.CSIT I Semester-2082 Exam Result", url,
                      "iost.tu.edu.np", "2026-09-28T04:00:00+00:00"),
            ],
        }

    def test_ok_for_a_detail_url(self):
        ok, detail = LC.assert_d7_notice_urls_are_detail(
            self._blob("https://iost.tu.edu.np/notice/15673"))
        self.assertTrue(ok, detail)

    def test_ok_for_a_trailing_slash_detail_url(self):
        ok, _ = LC.assert_d7_notice_urls_are_detail(
            self._blob("https://iost.tu.edu.np/notice/15673/"))
        self.assertTrue(ok)

    def test_fails_with_exact_string_on_a_nav_url(self):
        ok, detail = LC.assert_d7_notice_urls_are_detail(
            self._blob("https://iost.tu.edu.np/"))
        self.assertFalse(ok)
        self.assertEqual(
            "canary D7: notice source notice:iost.tu.edu.np emitted 1 non-detail URLs",
            detail)

    def test_fails_with_exact_string_when_a_notice_source_produced_nothing(self):
        blob = {
            "source_results": [
                _row("notice:iost.tu.edu.np", "notice", "ok", 0,
                     category="education_tu", host="iost.tu.edu.np"),
            ],
            "candidates": [],
        }
        ok, detail = LC.assert_d7_notice_urls_are_detail(blob)
        self.assertFalse(ok)
        self.assertEqual(
            "canary D7: notice source notice:iost.tu.edu.np emitted 0 candidates",
            detail)

    # -- the plural shape: the live TU IOST detail url, captured from a real
    # -- run. The old regex was `/notice/?\d+` (SINGULAR) and matched 0 of 5
    # -- live notice sources, which is how a correct collector was reported as
    # -- five sources' worth of failure.

    def test_ok_for_the_real_plural_detail_url(self):
        ok, detail = LC.assert_d7_notice_urls_are_detail(
            self._blob("https://iost.tu.edu.np/notices/15697"))
        self.assertTrue(ok, detail)

    def test_still_rejects_the_filtered_list_url(self):
        """`/notices?notice_type=21` is the BOARD, not one notice.

        Pinned deliberately: the plural widening must not slide into accepting
        every URL under /notices/, which would make D7 assert nothing at all.
        """
        self.assertIsNone(LC.NOTICE_DETAIL_URL_RE.search(
            "https://iost.tu.edu.np/notices?title=&start_date=&notice_type=21"))
        ok, _ = LC.assert_d7_notice_urls_are_detail(
            self._blob("https://iost.tu.edu.np/notices?notice_type=21"))
        self.assertFalse(ok)

    def test_still_rejects_real_live_navigation_urls(self):
        """Every one of these is a section/category/root link harvested by the
        live anchor scan. D7 exists to catch exactly these, so a regex that
        accepted them would be worse than no D7 at all."""
        for url in ("https://tu.edu.np/notices",
                    "https://tuexam.edu.np/",
                    "https://iost.tu.edu.np/pages/exam-2013",
                    "https://nss.edu.np/category/noticenews/",
                    "https://nea.org.np/recruitment/open/approved-candidate"):
            self.assertIsNone(LC.NOTICE_DETAIL_URL_RE.search(url), url)
            ok, _ = LC.assert_d7_notice_urls_are_detail(self._blob(url))
            self.assertFalse(ok, url)

    def test_every_notice_source_is_reported_not_just_the_first(self):
        """The old body returned on the FIRST notice source, so one bad source
        masked every source behind it. All offenders must appear."""
        blob = {
            "source_results": [
                _row("notice:empty.tu.edu.np", "notice", "ok", 0,
                     category="education_tu", host="empty.tu.edu.np"),
                _row("notice:iost.tu.edu.np", "notice", "ok", 1,
                     category="education_tu", host="iost.tu.edu.np"),
            ],
            "candidates": [
                _cand("B.Sc.CSIT I Semester-2082 Exam Result",
                      "https://iost.tu.edu.np/", "iost.tu.edu.np",
                      "2026-09-28T04:00:00+00:00"),
            ],
        }
        ok, detail = LC.assert_d7_notice_urls_are_detail(blob)
        self.assertFalse(ok)
        self.assertIn("empty.tu.edu.np", detail)
        self.assertIn("iost.tu.edu.np", detail)


# ---------------------------------------------------------------------------
# A1..A8
# ---------------------------------------------------------------------------

class A1Tests(unittest.TestCase):
    def test_ok_with_three_or_more_notices(self):
        ok, detail = LC.assert_a1_result_feed_parsed(good_academic())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_only_one_notice_parses(self):
        blob = good_academic()
        blob["result_list_html"] = '<a href="/notices/15673">Result</a>'
        ok, detail = LC.assert_a1_result_feed_parsed(blob)
        self.assertFalse(ok)
        self.assertEqual("canary A1: result feed parsed only 1 notices", detail)

    def test_fails_with_exact_string_on_an_empty_page(self):
        ok, detail = LC.assert_a1_result_feed_parsed(
            {"result_list_html": "<html>nothing here</html>"})
        self.assertFalse(ok)
        self.assertEqual("canary A1: result feed parsed only 0 notices", detail)


class A2Tests(unittest.TestCase):
    def test_ok_with_a_content_title(self):
        ok, detail = LC.assert_a2_detail_title_not_chrome(good_academic())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_on_site_chrome(self):
        blob = good_academic()
        blob["details"] = [{"id": 15673, "title": "INSTITUTE OF SCIENCE AND TECHNOLOGY"}]
        ok, detail = LC.assert_a2_detail_title_not_chrome(blob)
        self.assertFalse(ok)
        self.assertEqual(
            "canary A2: detail title is site chrome: INSTITUTE OF SCIENCE AND TECHNOLOGY",
            detail)

    def test_skipped_when_no_detail_pages_were_fetched(self):
        ok, _ = LC.assert_a2_detail_title_not_chrome({"details": []})
        self.assertIsNone(ok)


class A3Tests(unittest.TestCase):
    def test_ok_when_a_live_title_classifies(self):
        ok, detail = LC.assert_a3_titles_classifiable(good_academic())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_every_title_is_unknown(self):
        """`unknown` means NO vocabulary of any kind was recognised.

        A Latin title can never reach it -- an unrecognised Latin title is a
        confident `no_match`, because the Latin vocabulary has been running
        against this board for months. So the unreadable case is a DEVANAGARI
        title with no recognised Nepali word, which is the real token-set gap
        and the shape the canary must catch.
        """
        blob = good_academic()
        blob["details"] = [{"id": 1, "title": "अज्ञात"},
                           {"id": 2, "title": "अज्ञात अर्को"}]
        ok, detail = LC.assert_a3_titles_classifiable(blob)
        self.assertFalse(ok)
        self.assertEqual("canary A3: 0/2 live detail titles were unreadable", detail)


class A4Tests(unittest.TestCase):
    def test_ok_when_a_devanagari_title_classifies(self):
        """The real notice 15696 shape: Devanagari, and NOT `unknown`."""
        blob = good_academic()
        blob["details"] = [{
            "id": 15696,
            "title": "स्नातकाेत्तर तह दाेश्राे वर्ष चाैथाे शत्र २०८० समूह माैका "
                     "परीक्षाकाे परीक्षा तालिका",
        }]
        ok, detail = LC.assert_a4_no_devanagari_deferred(blob)
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_on_a_deferred_devanagari_title(self):
        blob = good_academic()
        blob["details"] = [{"id": 90001, "title": "अज्ञात"},
                           {"id": 90002, "title": "अज्ञात अर्को"}]
        ok, detail = LC.assert_a4_no_devanagari_deferred(blob)
        self.assertFalse(ok)
        self.assertEqual(
            "canary A4: 2 Devanagari titles deferred as unreadable - token set gap",
            detail)

    def test_ignores_latin_titles(self):
        """Deferring a LATIN title is not a token-set gap, so A4 stays quiet."""
        blob = good_academic()
        blob["details"] = [{"id": 90003, "title": "zzzz qqqq"}]
        ok, detail = LC.assert_a4_no_devanagari_deferred(blob)
        self.assertTrue(ok, detail)


class A5Tests(unittest.TestCase):
    def test_ok_when_text_was_extracted(self):
        ok, detail = LC.assert_a5_pdf_text_or_deferred(good_academic())
        self.assertTrue(ok, detail)

    def test_ok_when_the_pdf_is_explicitly_deferred(self):
        blob = good_academic()
        blob["pdfs"] = [{"notice_id": 1, "text": "", "deferred": True,
                         "deferral_reason": "fetch"}]
        ok, detail = LC.assert_a5_pdf_text_or_deferred(blob)
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_on_text_neither_nor_deferral(self):
        """Never a decision: a sheet the pipeline cannot read must be deferred."""
        blob = good_academic()
        blob["pdfs"] = [
            {"notice_id": 1, "text": "", "deferred": False},
            {"notice_id": 2, "text": "   ", "deferred": False},
        ]
        ok, detail = LC.assert_a5_pdf_text_or_deferred(blob)
        self.assertFalse(ok)
        self.assertEqual(
            "canary A5: 2 PDFs produced neither text nor a deferral", detail)


class A6Tests(unittest.TestCase):
    def test_ok_when_every_text_is_readable_or_deferred(self):
        ok, detail = LC.assert_a6_unreadable_is_deferred(good_academic())
        self.assertTrue(ok, detail)

    def test_ok_when_unreadable_text_is_deferred(self):
        """Non-empty prose-shaped garbage WITH a deferral is correct behaviour."""
        blob = good_academic()
        blob["pdfs"] = [{"notice_id": 1, "text": "lorem ipsum dolor sit amet",
                         "deferred": True, "deferral_reason": "extraction"}]
        ok, detail = LC.assert_a6_unreadable_is_deferred(blob)
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_unreadable_text_was_decided(self):
        """This is the sharpest assertion in the file.

        Non-empty garbage with no roll-shaped token used to produce a FALSE
        FAILED, and a false failed is the single most damaging output this
        pipeline can emit.
        """
        blob = good_academic()
        blob["pdfs"] = [{"notice_id": 1, "text": "lorem ipsum dolor sit amet",
                         "deferred": False, "deferral_reason": ""}]
        ok, detail = LC.assert_a6_unreadable_is_deferred(blob)
        self.assertFalse(ok)
        self.assertEqual("canary A6: unreadable text was DECIDED, not deferred",
                         detail)

    def test_skipped_when_no_pdf_produced_text(self):
        ok, _ = LC.assert_a6_unreadable_is_deferred(
            {"pdfs": [{"notice_id": 1, "text": "", "deferred": True}]})
        self.assertIsNone(ok)


class A7Tests(unittest.TestCase):
    def test_ok_with_the_frozen_three_keys(self):
        ok, detail = LC.assert_a7_ocr_shape_and_need(good_academic())
        self.assertTrue(ok, detail)
        self.assertIn("pytesseract", detail + str(LC.AR.ocr_capability()))

    def test_fails_with_exact_string_when_a_pdf_needs_ocr_and_none_exists(self):
        """A scanned sheet would defer silently forever with no OCR stack."""
        blob = good_academic()
        blob["pdfs"] = [{"notice_id": 1, "text": "", "deferred": True,
                         "deferral_reason": "extraction"}]
        real = LC.AR.ocr_capability
        try:
            LC.AR.ocr_capability = lambda: {"pytesseract": False,
                                            "binary": False,
                                            "pdf_to_image": False}
            ok, detail = LC.assert_a7_ocr_shape_and_need(blob)
        finally:
            LC.AR.ocr_capability = real
        self.assertFalse(ok)
        self.assertTrue(
            detail.startswith("canary A7: OCR is entirely unavailable and a PDF "
                              "needed it"), detail)

    def test_ok_when_no_pdf_deferred_for_extraction(self):
        blob = good_academic()
        blob["pdfs"] = [{"notice_id": 1, "text": "1357 2468", "deferred": False}]
        real = LC.AR.ocr_capability
        try:
            LC.AR.ocr_capability = lambda: {"pytesseract": False,
                                            "binary": False,
                                            "pdf_to_image": False}
            ok, detail = LC.assert_a7_ocr_shape_and_need(blob)
        finally:
            LC.AR.ocr_capability = real
        self.assertTrue(ok, detail)
        self.assertIn("no PDF needed it", detail)

    def test_fails_on_a_changed_capability_shape(self):
        real = LC.AR.ocr_capability
        try:
            LC.AR.ocr_capability = lambda: {"pytesseract": False, "binary": False}
            ok, detail = LC.assert_a7_ocr_shape_and_need(good_academic())
        finally:
            LC.AR.ocr_capability = real
        self.assertFalse(ok)
        self.assertIn("capability keys are", detail)


class A8Tests(unittest.TestCase):
    def test_ok_when_every_critical_feed_was_fetched(self):
        ok, detail = LC.assert_a8_critical_feeds_fetched(good_academic())
        self.assertTrue(ok, detail)

    def test_fails_with_exact_string_when_a_critical_feed_was_skipped(self):
        """The self-check skips the critical result feed and exits green.

        Nothing else proves the critical path was touched, which is why A8
        exists and why the canary asserts AROUND that script rather than
        modifying it.
        """
        blob = good_academic()
        blob["feeds_fetched"] = ["iost_all"]
        ok, detail = LC.assert_a8_critical_feeds_fetched(blob)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("canary A8: a critical feed was never "
                                          "fetched"), detail)

    def test_config_really_does_mark_feeds_critical(self):
        feeds = LC._critical_feeds_from_config()
        self.assertTrue(feeds, "config/exam_notices.json must mark feeds critical")
        self.assertIn("iost_result", [f.get("id") for f in feeds])


# ---------------------------------------------------------------------------
# The runner: crashes are failures, skips are reported, never hidden.
# ---------------------------------------------------------------------------

class RunChecksTests(unittest.TestCase):
    def test_a_healthy_blob_reports_every_check(self):
        results = LC.run_checks(good_blob())
        self.assertEqual(len(LC.ALL_CHECKS), len(results))
        self.assertEqual([cid for cid, _, _ in results],
                         [cid for cid, _ in LC.ALL_CHECKS])

    def test_a_half_that_was_not_collected_is_skipped_not_failed(self):
        """`--skip-academic` must not accuse the product of a gap it never looked at.

        This is a real bug this suite caught: with only the digest collected,
        the academic half ran against an empty blob and reported `A8: a critical
        feed was never fetched` -- a canary inventing a finding. A half that was
        not collected is SKIPPED.
        """
        combined = {"digest": good_digest(), "academic": None}
        results = LC.run_checks(combined)
        by_id = {cid: (ok, detail) for cid, ok, detail in results}
        for cid, _assertion in LC.ACADEMIC_CHECKS:
            ok, detail = by_id[cid]
            self.assertIsNone(ok, "%s must be SKIPPED, got %r" % (cid, detail))
        for cid, _assertion in LC.DIGEST_CHECKS:
            self.assertIsNotNone(by_id[cid][0],
                                 "%s should still have been checked" % cid)

    def test_a_crashing_assertion_is_a_failure_not_a_crash(self):
        """A canary whose assertion raises must FAIL LOUDLY, not exit 0."""
        results = LC.run_checks(None)
        failed = [(cid, detail) for cid, ok, detail in results if ok is False]
        self.assertTrue(failed, "a None blob must fail, not crash the canary")
        for _, detail in failed:
            self.assertTrue(detail.startswith("canary "), detail)

    def test_no_test_calls_collect(self):
        """The offline guarantee, asserted rather than assumed.

        Scans THIS file's own AST for a call to `collect`, so the rule cannot
        rot when someone later adds a "just one live call" to a test. A textual
        search would trip over the word in a docstring, which is why this walks
        the tree instead.
        """
        import ast
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=__file__)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "collect":
                offenders.append("%s line %s" % (name, node.lineno))
        self.assertEqual([], offenders,
                         "no test may call collect(): %s" % offenders)

    def test_collect_is_the_only_network_touch(self):
        """`collect` is the only function in live_canary.py allowed to fetch.

        Asserted structurally: no OTHER top-level function may name a network
        primitive. That is the property the anti-flake gate depends on -- the
        fetch accounting is only meaningful if nothing fetches behind its back.
        """
        import ast
        path = os.path.join(REPO_ROOT, "scripts", "live_canary.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        net_names = {"fetch_bytes", "urlopen", "fetch_notice_detail",
                     "fetch_notice_list", "default_fetch_fn", "download_pdf",
                     "main", "collect"}
        # notify_ntfy is the escalation notifier, not a collection path: it runs
        # only at strike >= 2, is best-effort, and never fails the run. It is
        # exempt here for the same reason `collect` is: it is a named,
        # auditable network call rather than a hidden one.
        allowed = net_names | {"notify_ntfy"}
        offenders = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in allowed or node.name.startswith("_collect"):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    name = (getattr(sub.func, "attr", None)
                            or getattr(sub.func, "id", None))
                    if name in net_names:
                        offenders.append("%s calls %s" % (node.name, name))
        self.assertEqual([], offenders,
                         "only collect() may touch the network: %s" % offenders)


# ---------------------------------------------------------------------------
# The anti-flake gate and the noise ramp.
# ---------------------------------------------------------------------------

class NetworkGateTests(unittest.TestCase):
    """The distinction the whole job turns on, tested as pure arithmetic."""

    @staticmethod
    def _inconclusive(net_ok, net_fail):
        total = net_ok + net_fail
        return net_ok == 0 or (net_fail / float(total)) >= 0.5

    def test_all_fetches_failing_is_inconclusive(self):
        self.assertTrue(self._inconclusive(0, 12))

    def test_a_half_failed_run_is_inconclusive(self):
        self.assertTrue(self._inconclusive(6, 6))

    def test_a_mostly_successful_run_is_a_real_verdict(self):
        self.assertFalse(self._inconclusive(20, 1))

    def test_one_failed_fetch_of_thirty_is_a_real_verdict(self):
        self.assertFalse(self._inconclusive(29, 1))

    def test_a_zero_item_success_is_never_inconclusive(self):
        """The load-bearing case: fetches succeeded, the PARSE did not.

        `net_ok` counts successful FETCHES. A source that answered 200 with a
        body the parser could not read is `net_ok`, so the run proceeds to the
        assertions and fails there -- which is exactly right.
        """
        self.assertFalse(self._inconclusive(30, 0))


class StrikeRampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="canary-hist-")
        self.path = os.path.join(self.tmp, "canary_history.json")

    def test_clean_run_is_strike_zero(self):
        self.assertEqual(0, LC.record_outcome([], "2026-09-29T00:00:00+00:00",
                                              self.path))

    def test_first_failure_is_strike_one(self):
        self.assertEqual(1, LC.record_outcome(["A4"], "2026-09-29T00:00:00+00:00",
                                              self.path))

    def test_strike_two_is_the_frozen_escalation_point(self):
        LC.record_outcome(["A4"], "2026-09-29T00:00:00+00:00", self.path)
        self.assertEqual(2, LC.record_outcome(["A4"], "2026-09-30T00:00:00+00:00",
                                              self.path))
        self.assertEqual(2, LC.CANARY_ERROR_STRIKES)

    def test_a_green_run_resets_the_strike_count(self):
        """A canary broken for a week must not stay loud forever."""
        LC.record_outcome(["A4"], "2026-09-29T00:00:00+00:00", self.path)
        LC.record_outcome(["A4"], "2026-09-30T00:00:00+00:00", self.path)
        self.assertEqual(0, LC.record_outcome([], "2026-10-01T00:00:00+00:00",
                                              self.path))
        self.assertEqual(1, LC.record_outcome(["A4"], "2026-10-02T00:00:00+00:00",
                                              self.path))

    def test_first_failed_at_is_set_once_and_cleared_on_green(self):
        LC.record_outcome(["A4"], "2026-09-29T00:00:00+00:00", self.path)
        LC.record_outcome(["A4"], "2026-09-30T00:00:00+00:00", self.path)
        hist = LC.load_history(self.path)
        self.assertEqual("2026-09-29T00:00:00+00:00",
                         hist.get("first_failed_at"),
                         "the streak must count from its BEGINNING")
        LC.record_outcome([], "2026-10-01T00:00:00+00:00", self.path)
        self.assertIsNone(LC.load_history(self.path).get("first_failed_at"))

    def test_history_written_is_valid_json_with_the_documented_keys(self):
        LC.record_outcome(["A1", "D3"], "2026-09-29T00:00:00+00:00", self.path)
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(1, data["consecutive_failures"])
        self.assertEqual(["A1", "D3"], data["last_failed"])
        self.assertIn("last_checked_at", data)

    def test_dead_days_is_measured_from_the_streak_start(self):
        import datetime
        hist = {"first_failed_at": "2026-09-01T00:00:00+00:00",
                "consecutive_failures": 6}
        now = datetime.datetime(2026, 9, 29, 3, 17, tzinfo=datetime.timezone.utc)
        self.assertEqual(28, LC._days_since(hist["first_failed_at"], now))


# ---------------------------------------------------------------------------
# Regression contracts: every fixture's `expect` block must still hold.
# ---------------------------------------------------------------------------

class RealFixtureExpectationTests(unittest.TestCase):
    """Each real capture's `expect` block is a regression contract.

    A fixture that stops matching its own expectation means a PARSER changed,
    not that the site drifted -- and the change has to be made deliberately in
    the manifest rather than discovered by a burned result.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(REPO_ROOT, "tests", "fixtures", "real")
        manifest_path = os.path.join(cls.root, "manifest.json")
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                cls.manifest = json.load(fh)
        except (OSError, ValueError) as exc:
            raise unittest.SkipTest("real fixture manifest unavailable: %s" % exc)
        cls.samples = cls.manifest.get("samples") or []

    def test_manifest_has_samples(self):
        self.assertTrue(self.samples, "manifest.json indexes no samples")

    def test_every_sample_declares_a_guard(self):
        """A fixture must DOCUMENT what it protects, or nobody knows when to
        delete it."""
        for sample in self.samples:
            self.assertTrue(sample.get("guard"),
                            "%s has no guard label" % sample.get("file"))

    def test_notice_detail_expects_hold_against_the_real_parser(self):
        import check_academic_result as AR
        checked = 0
        for sample in self.samples:
            if sample.get("kind") != "notice_detail":
                continue
            expect = sample.get("expect") or {}
            with open(os.path.join(self.root, sample["file"]),
                      encoding="utf-8", errors="replace") as fh:
                parsed = AR.parse_notice_detail_html(fh.read())
            title = parsed["title"]
            if "title_present" in expect:
                self.assertEqual(expect["title_present"],
                                 bool(title and not parsed["title_degraded"]),
                                 "%s: authoritative title changed" % sample["file"])
            if "title_degraded" in expect:
                self.assertEqual(expect["title_degraded"],
                                 bool(parsed["title_degraded"]),
                                 "%s: degraded flag changed" % sample["file"])
            if "script" in expect:
                verdict = AR.classify_semester(title)
                self.assertEqual(expect["script"], verdict.get("script"),
                                 "%s: script changed" % sample["file"])
            if "classify_status" in expect:
                verdict = AR.classify_semester(title)
                self.assertEqual(expect["classify_status"],
                                 verdict.get("status"),
                                 "%s: classify status changed" % sample["file"])
            if "semester" in expect:
                verdict = AR.classify_semester(title)
                self.assertEqual(expect["semester"], verdict.get("semester"),
                                 "%s: semester changed" % sample["file"])
            checked += 1
        self.assertGreaterEqual(checked, 2, "expected >=2 real detail pages")

    def test_notice_list_expects_hold_against_the_real_parser(self):
        import check_academic_result as AR
        checked = 0
        for sample in self.samples:
            if sample.get("kind") != "notice_list":
                continue
            expect = sample.get("expect") or {}
            with open(os.path.join(self.root, sample["file"]),
                      encoding="utf-8", errors="replace") as fh:
                notices = AR.parse_notice_list(fh.read())
            if "min_notices" in expect:
                self.assertGreaterEqual(len(notices), expect["min_notices"],
                                        "%s: parsed fewer notices than the "
                                        "regression contract" % sample["file"])
            if "notice_ids" in expect:
                got = sorted(n["id"] for n in notices)
                self.assertEqual(sorted(expect["notice_ids"]), got,
                                 "%s: notice ids changed" % sample["file"])
            if "any_id" in expect:
                self.assertIn(expect["any_id"],
                              [n["id"] for n in notices],
                              "%s: expected notice %s" % (sample["file"],
                                                          expect["any_id"]))
            checked += 1
        self.assertGreaterEqual(checked, 1, "expected >=1 real notice list")

    def test_feed_expects_hold_against_the_real_collector(self):
        """The captured feed must be GENUINE bytes, not a hand-written shape.

        `tests/collection/test_collector_retry.py` (not this file) asserts a
        real fixture is not the `<rss><channel><item><title>Headline N` shape;
        this asserts the other half -- that the real parser still reads it.
        """
        import collect_news as CN
        import xml.etree.ElementTree as ET
        checked = 0
        for sample in self.samples:
            if sample.get("kind") != "feed":
                continue
            expect = sample.get("expect") or {}
            with open(os.path.join(self.root, sample["file"]), "rb") as fh:
                data = fh.read()
            root = ET.fromstring(data)
            items = CN._iter_items(root)
            if "min_items" in expect:
                self.assertGreaterEqual(len(items), expect["min_items"],
                                        "%s: fewer real <item> than the "
                                        "contract" % sample["file"])
            if expect.get("items_look_hand_written") is False:
                # The shape tests/collection/test_collector_retry.py rejects:
                # a synthetic `<title>Headline N` ladder proves nothing about
                # the site, because it was written to match the parser.
                titles = [(it.findtext("title") or "") for it in items]
                self.assertNotEqual(
                    ["Headline %d" % (i + 1) for i in range(len(titles))],
                    titles,
                    "%s: this looks like the hand-written fixture shape"
                    % sample["file"])
                hand = [t for t in titles if t.startswith("Headline ")]
                self.assertEqual(
                    [], hand,
                    "%s: %d title(s) carry the synthetic 'Headline N' marker"
                    % (sample["file"], len(hand)))
            if "min_distinct_titles" in expect:
                titles = set((it.findtext("title") or "") for it in items)
                self.assertGreaterEqual(
                    len(titles), expect["min_distinct_titles"],
                    "%s: only %d distinct titles; a real feed is not a ladder of "
                    "repeats" % (sample["file"], len(titles)))
            if "max_bytes" in expect:
                self.assertLessEqual(len(data), expect["max_bytes"],
                                     "%s: capture exceeds the declared cap"
                                     % sample["file"])
            checked += 1
        self.assertGreaterEqual(checked, 1, "expected >=1 genuine feed capture")

    def test_pdf_shape_samples_carry_no_student_data(self):
        """A result sheet is a table of OTHER students' names, rolls and marks.

        Mirroring one into git is not licensed by its being public, so the
        committed artefact is metadata only. This asserts that shape, because a
        PDF committed "just for the test" is unrecoverable once pushed.
        """
        checked = 0
        for sample in self.samples:
            if sample.get("kind") != "pdf_shape":
                continue
            self.assertTrue(sample["file"].endswith(".json"),
                            "%s: a pdf_shape sample must be JSON metadata"
                            % sample["file"])
            with open(os.path.join(self.root, sample["file"]),
                      encoding="utf-8") as fh:
                payload = json.load(fh)
            for key in ("source_sha256", "page_count",
                        "roll_shaped_tokens_present", "extractor"):
                self.assertIn(key, payload,
                              "%s: pdf_shape metadata missing %r"
                              % (sample["file"], key))
            self.assertNotIn("text", payload,
                             "%s: extracted text must never be committed"
                             % sample["file"])
            self.assertNotIn("roll", payload,
                             "%s: a roll token must never be committed"
                             % sample["file"])
            self.assertNotIn("name", payload,
                             "%s: a student name must never be committed"
                             % sample["file"])
            checked += 1
        self.assertGreaterEqual(checked, 1, "expected >=1 pdf_shape sample")

    def test_no_committed_binary_pdf_in_the_real_tree(self):
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                self.assertFalse(
                    name.lower().endswith(".pdf"),
                    "%s: a real result PDF must never be committed"
                    % os.path.join(dirpath, name))


if __name__ == "__main__":
    unittest.main()
