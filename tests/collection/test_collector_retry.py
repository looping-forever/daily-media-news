#!/usr/bin/env python3
"""tests/collection/test_collector_retry.py -- offline tests for the
collector's retry policy, the config loader and the per-source truth record
(`source_results`).

NO NETWORK IS TOUCHED ANYWHERE HERE: collect_news.fetch_bytes is the single
network injection point and is monkeypatched in every test. The backoff sleep is
patched too, so the retry tests are instant and deterministic.

Run:
    py -m unittest discover -s tests\\collection -t .
"""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import xml.etree.ElementTree as ET

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import collect_news as cn  # noqa: E402
import source_health as sh  # noqa: E402

REPO_CONFIG = os.path.join(REPO_ROOT, "config", "collection.json")

# This module must never touch the real resolver: the collector's DNS
# pre-flight calls socket.getaddrinfo, so an unpatched test would do real DNS
# (and a dead zone like tu.edu.np would stall it for seconds). Every test in
# this file therefore runs against an offline resolver unless it deliberately
# installs its own (TestDnsPreflight).
_SAVED_GETADDRINFO = None


def _offline_getaddrinfo(host, *args, **kwargs):
    return []


def setUpModule():
    global _SAVED_GETADDRINFO
    _SAVED_GETADDRINFO = socket.getaddrinfo
    socket.getaddrinfo = _offline_getaddrinfo


def tearDownModule():
    if _SAVED_GETADDRINFO is not None:
        socket.getaddrinfo = _SAVED_GETADDRINFO


def rss_doc(items=1):
    body = "".join(
        "<item><title>Headline %d</title><link>https://example.com/%d</link>"
        "<pubDate>Mon, 07 Sep 2026 06:00:00 GMT</pubDate>"
        "<source url=\"https://example.com/\">Example</source></item>" % (i, i)
        for i in range(items))
    return ("<?xml version='1.0' encoding='UTF-8'?><rss><channel>%s</channel>"
            "</rss>" % body).encode("utf-8")


WEATHER_BODY = json.dumps({
    "daily": {"time": ["2026-09-27", "2026-09-28"],
              "temperature_2m_max": [28.0, 27.0],
              "temperature_2m_min": [20.0, 19.0],
              "precipitation_sum": [0.0, 2.0],
              "weathercode": [0, 61]}}).encode("utf-8")


def weather_hourly_body(dates=None):
    """A captured open-meteo HOURLY response, for the collector's own request.

    The collector now asks open-meteo for hourly data (it drives the
    deterministic advice engine) while still reading `daily` out of the SAME
    response, so the end-to-end fixture has to be the hourly shape. The dates
    are derived from Nepal time rather than hard-coded: the advice lane refuses
    to advise unless the response carries a complete forecast for TODAY, so a
    frozen date would make this test start failing the morning after it was
    written.
    """
    dates = dates or [cn.kathmandu_now_str()[:10]]
    count = 24 * len(dates)
    return json.dumps({
        "timezone": "Asia/Kathmandu", "utc_offset_seconds": 20700,
        "hourly": {
            "time": ["%sT%02d:00" % (d, h) for d in dates for h in range(24)],
            "precipitation_probability": [0] * count,
            "precipitation": [0.0] * count,
            "temperature_2m": [19.0] * count,
            "apparent_temperature": [18.6] * count,
            "wind_speed_10m": [1.5] * count,
            "visibility": [9420.0] * count},
        "daily": {
            "time": list(dates),
            "temperature_2m_max": [26.1] * len(dates),
            "temperature_2m_min": [12.6] * len(dates),
            "precipitation_sum": [1.0] * len(dates),
            "weathercode": [51] * len(dates)}}).encode("utf-8")

NOTICE_HTML = (b"<html><body>"
               b"<a href='/notice/1'>Exam notice published</a>"
               b"<a href='/x'>Unrelated link</a>"
               b"</body></html>")


class Recorder(object):
    """Fake fetch_bytes: records calls, replays a scripted outcome per URL."""

    def __init__(self, outcomes=None, default=None):
        # outcomes: {substring-of-url: [exc-or-bytes, ...]} consumed in order.
        self.outcomes = outcomes or {}
        self.default = default
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, timeout, max_bytes):
        with self.lock:
            self.calls.append({"url": url, "timeout": timeout})
            script = None
            for key, seq in self.outcomes.items():
                if key in url:
                    script = seq
                    break
        if script is None:
            script = [self.default] if self.default is not None else [rss_doc()]
        item = script[0] if len(script) == 1 else script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def count(self, needle):
        return sum(1 for c in self.calls if needle in c["url"])


def dns_error():
    return urllib.error.URLError(
        socket.gaierror(-3, "Temporary failure in name resolution"))


def http_error(code):
    return urllib.error.HTTPError("https://x/", code, "boom", {}, None)


def parse_error():
    return ET.ParseError("not well-formed (invalid token): line 1, column 0")


class _TimeShim(object):
    """Stands in for collect_news.time: real clock, recorded sleeps.

    Rebinding the module-global `time` name inside collect_news keeps the real
    time.sleep intact for the test thread, so a genuinely slow task still takes
    real time while backoff sleeps are instant and observable.
    """

    def __init__(self, real, sleep_calls):
        self.__dict__["_real"] = real
        self.sleep_calls = sleep_calls

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)


class Patches(object):
    """Monkeypatch fetch_bytes + the backoff sleep; restore on exit."""

    def __init__(self, recorder):
        self.recorder = recorder
        self.slept = []
        self._fb = cn.fetch_bytes
        self._time = cn.time

    def __enter__(self):
        cn.fetch_bytes = self.recorder
        cn.time = _TimeShim(self._time, self.slept)
        return self

    def __exit__(self, *exc):
        cn.fetch_bytes = self._fb
        cn.time = self._time
        return False


# ---------------------------------------------------------------------------
# rss_task shape detection (the silent-`[]` bug class)
# ---------------------------------------------------------------------------

class TestRssTaskShapeDetection(unittest.TestCase):
    """rss_task must never return [] SILENTLY for a body it did not understand.

    A non-`<rss>` root, an Atom `<entry>` feed, RSS 1.0/RDF and an HTTP-200
    HTML error page all used to come back as an empty list, which the run
    recorded as a healthy fetch. Now the shape is detected and the reason is
    `parse` -- a closed reason code, distinct from a transport failure.
    """

    def _fetch(self, body):
        rec = Recorder(default=body)
        with Patches(rec):
            return cn.rss_task("https://news.google.com/rss?q=x", "nepal",
                               "en", 10, None, None)

    def _row(self, body):
        rec = Recorder(default=body)
        with Patches(rec):
            _ok, failed, _b, raw, _ws, results = cn.collect_parallel(
                [{"name": "gnews:np-en:x", "kind": "gnews", "category": "nepal",
                  "host": "news.google.com",
                  "url": "https://news.google.com/rss?q=x", "att": {"n": 0},
                  "fn": lambda: cn.rss_task("https://news.google.com/rss?q=x",
                                            "nepal", "en", 10, None, None)}],
                {"soft_budget_sec": 30, "max_workers": 1})
        return failed, raw, results[0]

    def test_html_200_error_page_is_parse_not_ok(self):
        html = (b"<!DOCTYPE html><html><head><title>404 Not Found</title>"
                b"</head><body>Not found</body></html>")
        failed, raw, row = self._row(html)
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(sh.classify_reason(row["error"]), "parse")
        self.assertEqual(raw, [])
        self.assertEqual(len(failed), 1)

    def test_atom_feed_is_detected_not_silently_empty(self):
        atom = (b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>"
                b"<entry><title>Real headline</title>"
                b"<link href='https://example.com/1'/>"
                b"<updated>2026-09-27T06:00:00Z</updated></entry></feed>")
        failed, raw, row = self._row(atom)
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(sh.classify_reason(row["error"]), "parse")
        self.assertEqual(raw, [])
        # The word Atom is in the error so an operator can see the cause.
        self.assertIn("Atom", row["error"])

    def test_rss10_rdf_is_detected(self):
        rdf = (b"<?xml version='1.0'?>"
               b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'"
               b" xmlns='http://purl.org/rss/1.0/'>"
               b"<channel><title>x</title></channel>"
               b"<item><title>Headline</title><link>https://e.com/1</link></item>"
               b"</rdf:RDF>")
        failed, raw, row = self._row(rdf)
        # RSS 1.0 is a DIFFERENT shape: it is reported as `parse` rather than
        # being half-parsed into a candidate with a wrong date and wrong source.
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(sh.classify_reason(row["error"]), "parse")
        self.assertEqual(raw, [])

    def test_rss10_items_are_still_reachable_for_an_operator_to_see(self):
        """The failure is RECORDED, not swallowed: it appears in sources_failed
        with a parse reason rather than being reported as a good fetch."""
        rdf = (b"<?xml version='1.0'?><rdf:RDF "
               b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
               b"<item><title>H</title><link>https://e.com/1</link></item>"
               b"</rdf:RDF>")
        with self.assertRaises(Exception) as ctx:
            self._fetch(rdf)
        self.assertEqual(sh.classify_reason(str(ctx.exception)), "parse")

    def test_wellformed_body_with_zero_items_is_empty_not_ok(self):
        empty = b"<?xml version='1.0'?><rss><channel><title>none</title></channel></rss>"
        failed, raw, row = self._row(empty)
        # Reachable, well-formed, zero items -> `empty`. Not a parse failure.
        self.assertEqual(row["outcome"], "empty")
        self.assertEqual(row["error"], "")
        self.assertEqual(row["items_found"], 0)
        self.assertEqual(raw, [])
        self.assertEqual(failed, [])

    def test_wellformed_rss_with_items_still_works(self):
        _failed, raw, row = self._row(rss_doc(3))
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["items_found"], 3)
        self.assertEqual(len(raw), 3)
        self.assertIsNotNone(raw[0]["_dt"])

    def test_atom_updated_is_accepted_as_a_date_fallback(self):
        """A feed with no pubDate must not sort to EPOCH (last) silently."""
        body = (b"<?xml version='1.0'?><rss><channel>"
                b"<item><title>Headline</title>"
                b"<link>https://example.com/9</link>"
                b"<atom:updated xmlns:atom='http://www.w3.org/2005/Atom'>"
                b"2026-09-27T06:00:00Z</atom:updated></item>"
                b"</channel></rss>")
        _failed, raw, row = self._row(body)
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(len(raw), 1)
        self.assertIsNotNone(raw[0]["_dt"])
        self.assertEqual(raw[0]["_dt"].year, 2026)

    def test_published_element_is_accepted_as_a_date_fallback(self):
        body = (b"<?xml version='1.0'?><rss><channel>"
                b"<item><title>Headline</title>"
                b"<link>https://example.com/8</link>"
                b"<published>2026-09-26T06:00:00Z</published></item>"
                b"</channel></rss>")
        _failed, raw, _row = self._row(body)
        self.assertIsNotNone(raw[0]["_dt"])
        self.assertEqual(raw[0]["_dt"].year, 2026)

    def test_unparseable_body_still_raises_its_parse_error(self):
        with self.assertRaises(ET.ParseError):
            self._fetch(b"{not xml at all")


# ---------------------------------------------------------------------------
# NOTICE_KEYWORDS: word boundaries + nav-chrome removal
# ---------------------------------------------------------------------------

NAV_HTML = (b"<html><body>"
            # 9 of 19 live `local_city` candidates were nav links.
            b"<a href='/department/administration'>Administration Department</a>"
            b"<a href='/department/exam'>Directorate of Examination</a>"
            b"<a href='/rate/structure'>Rates &amp; Charges</a>"
            b"<a href='/interest'>Base Rate &amp; Interest Spread Rate</a>"
            b"<a href='/rate-card'>Interest Rate Card</a>"
            b"<a href='/portal/login'>Banking Portal</a>"
            b"<a href='/faculty'>Faculty Directory</a>"
            b"<a href='/branch-locator'>Branch Locator</a>"
            b"<a href='/rates-chart'>Rates Chart Portal</a>"
            # ...and the real notices, which must SURVIVE. Each is DETAIL-shaped
            # (an id, or the document itself) because that is now the contract:
            # a slug-only `/notices/result-published` is a topic page, and the
            # canary's D7 rejects it exactly as this collector now does.
            b"<a href='/notices/1042'>Exam notice published 2026-09-20</a>"
            b"<a href='/notices/1039'>BSc CSIT result published 2026-09-21</a>"
            b"<a href='/download/admission-form.pdf'>Admission form notice 2026-09-22</a>"
            b"<a href='/x'>Unrelated link</a>"
            b"</body></html>")


class TestNoticeKeywordFiltering(unittest.TestCase):
    def _titles(self, body, url="https://nea.org.np/"):
        rec = Recorder(default=body)
        with Patches(rec):
            out = cn.notices_task(url, "local_city", 10, None, None)
        return [c["title"] for c in out]

    def test_rate_is_not_a_substring_of_directorate(self):
        """The live bug: `rate` is inside "DiRATEorate", so every Directorate
        nav link matched the raw substring test."""
        self.assertFalse(cn.NOTICE_KEYWORDS_RE.search("Directorate of Examination"))
        self.assertTrue(cn.NOTICE_KEYWORDS_RE.search("Result published"))
        self.assertFalse(cn.NOTICE_KEYWORDS_RE.search("Administration"))

    def test_nav_chrome_is_dropped_and_real_notices_survive(self):
        titles = self._titles(NAV_HTML)
        self.assertEqual(titles, [
            "Exam notice published 2026-09-20",
            "BSc CSIT result published 2026-09-21",
            "Admission form notice 2026-09-22",
        ])
        for t in titles:
            self.assertTrue(cn.NOTICE_KEYWORDS_RE.search(t), t)
        joined = " | ".join(titles).lower()
        for chrome in ("directorate", "administration", "rates &", "base rate",
                       "portal", "faculty", "branch locator"):
            self.assertNotIn(chrome, joined)

    def test_bank_local_rate_chrome_is_dropped(self):
        """Live: `bank_local` was 9/9 nav chrome ("Rates & Charges",
        "Base Rate & Interest Spread Rate")."""
        html = (b"<html><body>"
                b"<a href='/rate'>Rates &amp; Charges</a>"
                b"<a href='/deposit/rate'>Deposit Rates</a>"
                b"<a href='/notices/1234'>Interest rate notice published</a>"
                b"</body></html>")
        self.assertEqual(self._titles(html, "https://www.bank.example.com/"),
                         ["Interest rate notice published"])

    def test_a_real_notice_path_is_not_treated_as_nav(self):
        """`/notice/...` and `/notices/...` are where notices LIVE. Dropping
        them would delete the real thing this task exists to collect."""
        for path in ("/notice/1", "/notices/2", "/download/exam.pdf"):
            self.assertFalse(cn._is_nav_chrome("Exam notice", path), path)
        self.assertTrue(cn._is_nav_chrome("Anything", "/department/x"))
        self.assertTrue(cn._is_nav_chrome("Anything", "/portal/login"))

    def test_short_section_index_anchor_is_dropped(self):
        """Live D4/D7, 2026-09-29: the Example Bank notice task yields
        EXACTLY one candidate -- "Audio Notice" ->
        /news--notices/audio-notice. It carries the whole-word keyword, so it
        survived the keyword filter, but it is a two-word accessibility nav
        label pointing at a section index, not a publication."""
        self.assertTrue(cn._is_nav_chrome(
            "Audio Notice",
            "https://www.bank.example.com/news--notices/audio-notice"))
        html = (b"<html><body>"
                b"<a href='/news--notices/audio-notice'>Audio Notice</a>"
                b"</body></html>")
        self.assertEqual(
            self._titles(html, "https://www.bank.example.com/"), [])

    def test_short_real_title_survives_a_detail_or_non_index_path(self):
        """The rule is structural, so a real publication with a short title
        must survive. A false drop deletes a real notice, which is the worse
        failure, so every detail shape is asserted to still pass."""
        for url in ("https://www.bank.example.com/notice/123",
                    "https://www.bank.example.com/notice/123/",
                    "https://iost.tu.edu.np/notices/15697",
                    "https://iost.tu.edu.np/notices/15673/",
                    "https://www.bank.example.com/download/notice.pdf",
                    "https://www.bank.example.com/news--notices/audio-notice-2077-01-01"):
            self.assertFalse(cn._is_nav_chrome("Audio Notice", url), url)
        # And end to end: same short title, one index path and one detail path.
        html = (b"<html><body>"
                b"<a href='/news--notices/audio-notice'>Audio Notice</a>"
                b"<a href='/notice/123'>Audio Notice</a>"
                b"</body></html>")
        out = self._titles(html, "https://www.bank.example.com/")
        self.assertEqual(len(out), 1, out)
        with Patches(Recorder(default=html)):
            kept = cn.notices_task("https://www.bank.example.com/",
                                   "bank_local", 10, None, None)
        self.assertEqual(kept[0]["url"],
                         "https://www.bank.example.com/notice/123")

    def test_a_long_title_is_never_treated_as_a_section_index(self):
        """The word-count guard, stated directly: the real IOST titles are 6+
        words and must not be reachable by the slug-echo rule at all, even when
        the slug echoes every word of the anchor. The live title and its live
        detail URL are used, so this is the real pair, not a caricature."""
        self.assertFalse(cn._is_nav_chrome(
            "M.Sc. IV Semester-2080 Make Up Exam Center Notice",
            "https://iost.tu.edu.np/notices/"
            "m-sc-iv-semester-2080-make-up-exam-center-notice-15697"))
        self.assertFalse(cn._is_nav_chrome(
            "M.Sc. IV Semester-2080 Make Up Exam Center Notice",
            "https://iost.tu.edu.np/notices/15697"))

    def test_notice_candidates_get_a_real_published_date(self):
        """A missing pubDate sorted to EPOCH and therefore LAST, which is how
        real notices ended up buried under poisoned rows that had a date."""
        rec = Recorder(default=NAV_HTML)
        with Patches(rec):
            out = cn.notices_task("https://nea.org.np/", "local_city", 10,
                                  None, None)
        dated = [c for c in out if c["_dt"] is not None]
        self.assertEqual(len(dated), len(out))
        self.assertEqual(dated[0]["_dt"].year, 2026)
        # And it reaches the candidate as a real ISO `published`.
        self.assertTrue(dated[0]["published"].startswith("2026-09-20"))

    def test_unparseable_date_leaves_published_none(self):
        """Never invent a date: an absent/garbage date stays None, exactly as
        before, rather than being coerced to EPOCH or to now."""
        self.assertIsNone(cn._notice_published_dt("Exam notice"))
        self.assertIsNone(cn._notice_published_dt("2026-13-45"))
        self.assertIsNone(cn._notice_published_dt(""))
        self.assertIsNotNone(cn._notice_published_dt("Notice 07 Sep 2026"))
        self.assertIsNotNone(cn._notice_published_dt("Sept 7, 2026 notice"))
        html = b"<html><a href='/notices/9'>Exam notice</a></html>"
        rec = Recorder(default=html)
        with Patches(rec):
            out = cn.notices_task("https://x.example/", "nepal", 10, None, None)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["_dt"])
        self.assertIsNone(out[0]["published"])


# ---------------------------------------------------------------------------
# Detail-URL gate (live D7 failure, 2026-09-29)
# ---------------------------------------------------------------------------

#: Every navigation URL a live run emitted as a "notice", with the anchor text
#: it was emitted under. Measured, not invented: these are the 10 offenders the
#: canary's D7 reported across four sources, captured before the fix. Every one
#: of them carries a whole-word notice keyword, which is exactly why no text
#: rule could tell them from a publication.
LIVE_NAV_URLS = (
    ("https://tu.edu.np/notices", "NOTICE"),
    ("https://tuexam.edu.np/", "Exam & Results info (Hotline no: +977-1-5384167)"),
    ("https://iost.tu.edu.np/pages/exam-2013", "Exam"),
    ("https://iost.tu.edu.np/notices?title=&start_date=&nep_start_date="
     "&end_date=&nep_end_date=&notice_type=21", "Result"),
    ("https://iost.tu.edu.np/events/501",
     "Scientific Workshop on Effective Exam Question Preparation"),
    ("https://www.nss.edu.np/entrance", "Entrance Result 2083"),
    ("https://nss.edu.np/admission/admission-policies-procedures/",
     "Admission Policies & Procedures"),
    ("https://nss.edu.np/category/noticenews/", "Notice | News"),
    ("https://nea.org.np/recruitment/recruitment-internal/publications",
     "Accepted Candidates Notice"),
    ("https://nea.org.np/recruitment/open/approved-candidate",
     "Approved Candidate Notice"),
)

#: Real notices observed live on the same run, with the detail URL each one is
#: published at. These are the rows that MUST survive a filter that is only
#: one notch too wide.
LIVE_DETAIL_URLS = (
    ("https://iost.tu.edu.np/notices/15697",
     "M.Sc. IV Semester-2080 Make Up Exam Center Notice"),
    ("https://iost.tu.edu.np/notices/15673", "B.Sc.CSIT I Semester-2082 Exam Result"),
    ("https://iost.tu.edu.np/notices/15565",
     "M.Sc. Microbiology III Semester-2081 Exam Result"),
    ("https://iost.tu.edu.np/notices/15673/", "B.Sc.CSIT I Semester-2082 Exam Result"),
)


class TestNoticeDetailUrlGate(unittest.TestCase):
    """The notice anchor scan must return PUBLICATIONS, not destinations.

    A false drop deletes a real notice, which is the worse failure, so both
    directions are asserted here: every measured navigation URL is refused, and
    every measured real notice still comes through.
    """

    def _titles(self, body, url="https://x.example/"):
        rec = Recorder(default=body)
        with Patches(rec):
            out = cn.notices_task(url, "nepal", 10, None, None)
        return [c["title"] for c in out]

    def test_every_live_navigation_url_is_rejected(self):
        """The 10 URLs that broke D7. Each is rejected by the shape gate AND
        produces no candidate end to end, so the fix is not only in the helper."""
        for url, text in LIVE_NAV_URLS:
            self.assertTrue(cn.NOTICE_KEYWORDS_RE.search(text),
                            "fixture must carry a keyword, else it proves nothing: %s"
                            % text)
            self.assertTrue(cn._is_nav_chrome(text, url), url)
            self.assertFalse(cn.NOTICE_DETAIL_URL_RE.search(url), url)
            html = ("<html><body><a href='%s'>%s</a></body></html>"
                    % (url, text)).encode("utf-8")
            self.assertEqual(self._titles(html), [], url)

    def test_real_detail_urls_with_real_titles_are_accepted(self):
        """The measured live notices must survive. A filter that drops these is
        too wide, and the correct fix is the filter, never this assertion."""
        for url, text in LIVE_DETAIL_URLS:
            self.assertFalse(cn._is_nav_chrome(text, url), url)
            self.assertTrue(cn.NOTICE_DETAIL_URL_RE.search(url), url)
            html = ("<html><body><a href='%s'>%s</a></body></html>"
                    % (url, text)).encode("utf-8")
            self.assertEqual(self._titles(html), [text], url)

    def test_short_title_notice_on_a_detail_url_is_still_accepted(self):
        """The "Audio Notice" fix must not be undone by the new gate: a short
        title is fine as long as the URL identifies ONE publication."""
        for url in ("https://www.bank.example.com/notice/123",
                    "https://www.bank.example.com/notice/123/",
                    "https://www.bank.example.com/download/notice.pdf",
                    "https://www.bank.example.com/news--notices/"
                    "audio-notice-2077-01-01"):
            self.assertFalse(cn._is_nav_chrome("Audio Notice", url), url)
        html = (b"<html><body>"
                b"<a href='/notice/123'>Audio Notice</a>"
                b"</body></html>")
        self.assertEqual(self._titles(html), ["Audio Notice"])

    def test_a_query_string_list_is_never_a_detail_url(self):
        """`/notices?notice_type=21` is a FILTERED LIST. The digit in the query
        string is a notice type, not an id, and must not read as one. The
        anchor after `/notices` is `?`, so no alternative reaches a digit."""
        for ntype in (8, 21, 23, 26):
            url = ("https://iost.tu.edu.np/notices?title=&start_date="
                   "&notice_type=%d" % ntype)
            self.assertFalse(cn.NOTICE_DETAIL_URL_RE.search(url), url)
            self.assertTrue(cn._is_nav_chrome("Result", url), url)

    def test_the_gate_is_a_shape_test_not_a_word_list(self):
        """A blocklist of words rots when a site is rebuilt. The rule must key
        on STRUCTURE only: the same navigation path with an id IS a
        publication, and the same notice vocabulary with no id is NOT. Neither
        'recruitment' nor 'category' nor 'pages' is named in the pattern."""
        pattern = cn.NOTICE_DETAIL_URL_RE.pattern
        for word in ("category", "recruitment", "pages", "entrance", "exam",
                     "admission", "result"):
            self.assertNotIn(word, pattern.lower(),
                             "gate must not name navigation vocabulary: %s" % word)
        # A real NEA publication, measured live, inside the very directory tree
        # whose OTHER two links are navigation: the id-shaped slug is the whole
        # difference. A word list would drop this one too.
        real_nea = ("https://nea.org.np/detail/public-notice-on-letter-of-intent"
                    "-nea-udc-2083084-02-sq-20830608")
        self.assertFalse(cn._is_nav_chrome("Public Notice on Letter of Intent",
                                           real_nea), real_nea)
        shaped = "https://nea.org.np/recruitment/open/approved-candidate-notice-2083084"
        self.assertFalse(cn._is_nav_chrome("Approved Candidate Notice", shaped),
                         shaped)
        # Same notice vocabulary, no id and no file -> still navigation.
        shapeless = "https://iost.tu.edu.np/pages/exam-2013"
        self.assertTrue(cn._is_nav_chrome("Exam", shapeless), shapeless)

    def test_the_gate_cannot_drift_from_the_canary_d7_contract(self):
        """The collector's gate and the canary's D7 must accept the SAME urls.
        If this ever fails, one of them was changed alone, and the product
        regresses back to 'collector emits what D7 rejects'."""
        try:
            import live_canary as lc
        except ImportError:  # pragma: no cover - canary is a sibling module
            self.skipTest("live_canary not importable")
        corpus = ([u for u, _ in LIVE_NAV_URLS]
                  + [u for u, _ in LIVE_DETAIL_URLS]
                  + ["https://nss.edu.np/notices/entry-test-notice",
                     "https://nss.edu.np/notices/",
                     "https://x.example/",
                     "https://x.example/notices",
                     "https://x.example/notice/abc",
                     "https://x.example/download/notice.docx",
                     "https://x.example/files/notice.pdf",
                     "https://x.example/view/123",
                     "https://x.example/uploads/2083-result.pdf"])
        for url in corpus:
            mine = bool(cn.NOTICE_DETAIL_URL_RE.search(url))
            theirs = bool(lc.NOTICE_DETAIL_URL_RE.search(url))
            # The collector may be STRICTER than D7 (that is safe); it may never
            # be LOOSER, because a looser gate re-opens this exact defect.
            self.assertFalse(mine and not theirs,
                             "collector admits %r but canary D7 rejects it" % url)


# ---------------------------------------------------------------------------
# Script-aware notice gate (live canary D5 + D7, 2026-09-29)
# ---------------------------------------------------------------------------

#: Every title below is VERBATIM off the live board, captured 2026-09-29, and
#: every URL is one of the detail-shaped anchors that board carries. These are
#: the rows the historical Latin-only gate threw away: `tu.edu.np/notices` has
#: SIX detail anchors and all six are Nepali-titled, so the old gate reported 0
#: for a `critical_host`, which flipped source_health to `empty`, which is the
#: live D5 failure. Notice 15696 keeps the board's own non-standard spelling
#: "चाैथाे" that devanagari.py documents, and 15362 keeps its non-standard
#: "प्रकशित" -- real text, not a corrected transcription.
LIVE_NEPALI_NOTICE_ANCHORS = (
    ("https://tu.edu.np/notices/15646",
     "त्रिवि केन्द्रीय कार्यालयका लागि जारको पिउने पानी सप्लाई सम्बन्धी पुनः "
     "शिलबन्दी दरभाउ पत्र आह्वानको सूचना 2026-09-28"),
    ("https://tu.edu.np/notices/15386",
     "त्रिवि पाठ्यक्रम विकास केन्द्रको समकक्षतापत्र बुझी लिन आउने सम्बन्धी "
     "जरुरी सूचना 2026-09-17"),
    ("https://tu.edu.np/notices/15192",
     "प्रधानमन्त्री दैवी प्रकोप उद्बार कोषमा रकम जम्मा सम्वन्धमा । 2026-09-09"),
    ("https://tu.edu.np/notices/15078",
     "अनुसन्धान निर्देशनालयको नतिजा प्रकाशन सम्बन्धी सूचना 2026-09-03"),
    ("https://iost.tu.edu.np/notices/15696",
     "स्नातकाेत्तर तह दाेश्राे वर्ष चाैथाे शत्र २०८० समूह माैका परीक्षाकाे "
     "परीक्षा तालिका संशाेधन सम्बन्धी सूचना"),
    ("https://iost.tu.edu.np/notices/15591",
     "B. Tech (Food Technology) कार्यक्रमको पुन: प्रवेश परीक्षाका लागि "
     "आवेदन फाराम भर्ने सम्बन्धी सूचना"),
    ("https://iost.tu.edu.np/notices/15362",
     "B.Sc. प्रवेश परीक्षा 2083 को नतिजा प्रकशित गरिएको बारे"),
    ("https://iost.tu.edu.np/notices/15642",
     "परीक्षा यथावत् सञ्चालन हुने सम्बन्धी अत्यन्त जरुरी सूचना"),
)

#: The Devanagari CHROME measured on the very same page, on the very same run:
#: six Nepali-titled anchors whose URLs are `/news/<id>` -- news items, not
#: notices. This is the case that decides the design. They are ordinary-looking
#: Nepali text with no notice word in them, so a "reject on absence" Nepali
#: gate would have been the right filter FOR THEM; and a "keep on absence"
#: gate would have been the right filter for notice 15192, which is a real
#: notice with no notice word in it. Those two are indistinguishable by
#: vocabulary, so the question has to be answered by the URL, and the URL gate
#: answers it correctly for every one of the fourteen anchors measured: all
#: six `/news/` ones are refused, all eight `/notices/<id>` ones are kept.
LIVE_NEPALI_CHROME_ANCHORS = (
    ("https://tu.edu.np/news/1251",
     "प्रधानमन्त्री दैवी प्रकोप उद्धार कोषमा नौ करोड पच्चीस लाख जम्मा"),
    ("https://tu.edu.np/news/1246",
     "पूल्चोक क्याम्पसलाई अन्तराष्ट्रीय रोवोटिक्स प्रतिस्पर्धामा सफलता"),
    ("https://tu.edu.np/news/1172", "त्रिभुवन विश्वविद्यालयको ६७ औं बार्षिक दिवस"),
)


class TestNoticeScriptAwareGate(unittest.TestCase):
    """The notice keyword gate must ask a question the board can answer.

    The real boards are Nepali, so a Latin-only gate could not see them: it
    reported 0 candidates for `tu.edu.np` (a `critical_host`), which is the
    live canary D5 failure, and it emptied the owner's `education_tu` category
    without ever saying so. The gate here is script-aware, and the two
    directions are asserted SEPARATELY because they are not symmetric: presence
    admits, absence never rejects.
    """

    def _cands(self, pairs, page="https://x.example/"):
        html = ("<html><body>%s</body></html>"
                % "".join("<a href='%s'>%s</a>" % (u, t)
                          for u, t in pairs)).encode("utf-8")
        with Patches(Recorder(default=html)):
            return cn.notices_task(page, "education_tu", 10, None, None)

    def _titles(self, pairs, page="https://x.example/"):
        return [c["title"] for c in self._cands(pairs, page)]

    # -- 1. the real Nepali notices come back ------------------------------
    def test_a_nepali_titled_notice_on_a_detail_url_is_kept(self):
        """The whole bug, end to end: the anchor on /notices/15646 with a
        Devanagari title was dropped before and is collected now."""
        pairs = list(LIVE_NEPALI_NOTICE_ANCHORS)
        out = self._cands(pairs)
        self.assertEqual([c["title"] for c in out], [t for _, t in pairs])
        self.assertEqual([c["url"] for c in out], [u for u, _ in pairs])
        for cand in out:
            self.assertEqual(cand["language"], "ne")
        # And the old gate really did drop them: proven, not asserted by
        # comment, so this test cannot pass on a gate that never regressed.
        for url, title in pairs:
            self.assertFalse(cn.NOTICE_KEYWORDS_RE.search(title),
                             "fixture must be invisible to the Latin gate: %s"
                             % title)
            self.assertTrue(cn._devanagari.detect_script(title)
                            in ("devanagari", "mixed"), title)

    def test_nepali_candidates_are_detail_urls_the_canary_would_accept(self):
        """The live D7 failure was a notice URL the canary rejects, so the
        collector and D7 are checked against each other on the NEW rows."""
        try:
            import live_canary as lc
        except ImportError:  # pragma: no cover - canary is a sibling module
            self.skipTest("live_canary not importable")
        for cand in self._cands(list(LIVE_NEPALI_NOTICE_ANCHORS)):
            url = cand["url"]
            self.assertTrue(lc.NOTICE_DETAIL_URL_RE.search(url), url)
            self.assertFalse(cn._is_nav_chrome(cand["title"], url), url)

    # -- 2. absence never rejects a real notice ----------------------------
    def test_absent_nepali_vocabulary_does_not_reject_a_real_notice(self):
        """15192 is a genuine published notice (it has an id) whose title
        carries no notice word in EITHER script. The English gate would drop
        the English equivalent of this title too, and that is the frozen
        English behaviour -- but for Devanagari a false drop deletes a real
        notice, so absence is not allowed to decide anything here."""
        url, title = ("https://tu.edu.np/notices/15192",
                      "प्रधानमन्त्री दैवी प्रकोप उद्बार कोषमा रकम जम्मा सम्वन्धमा । "
                      "2026-09-09")
        self.assertEqual(self._titles([(url, title)]), [title])
        self.assertEqual(cn.notice_keyword_verdict(title), (True, "ne"))
        for word in cn.NEPALI_NOTICE_KEYWORDS:
            self.assertNotIn(word, title, "fixture must carry no vocabulary")
        # The date in that Nepali title is still read, so it does not sort to
        # EPOCH and bury itself under the rows that do carry a date.
        with Patches(Recorder(default=("<html><body><a href='%s'>%s</a>"
                                       "</body></html>" % (url, title))
                                      .encode("utf-8"))):
            out = cn.notices_task("https://tu.edu.np/", "education_tu", 10,
                                  None, None)
        self.assertEqual(out[0]["published"], "2026-09-09T00:00:00+00:00")

    # -- 3. the chrome still dies -----------------------------------------
    def test_devanagari_chrome_is_still_dropped(self):
        """The six live Nepali `/news/` anchors produce nothing. Vocabulary
        cannot see the difference between a news item and a notice -- the URL
        can, and it is the URL that decides."""
        for url, title in LIVE_NEPALI_CHROME_ANCHORS:
            self.assertFalse(cn.NOTICE_DETAIL_URL_RE.search(url), url)
            self.assertEqual(self._titles([(url, title)]), [], url)
        self.assertEqual(self._titles(list(LIVE_NEPALI_CHROME_ANCHORS)), [])

    def test_the_nepali_vocabulary_cannot_smuggle_chrome_past_the_url_gate(self):
        """The other direction of the widening: a Devanagari anchor that DOES
        carry the notice word is still refused when its URL is navigation, so
        admitting Nepali never became a way around NOTICE_DETAIL_URL_RE."""
        for url in ("https://tu.edu.np/notices",
                    "https://tu.edu.np/notices?notice_type=21",
                    "https://iost.tu.edu.np/notices/",
                    "https://tu.edu.np/notices/old"):
            title = "पुराना सूचनाहरू"
            self.assertTrue(cn._devanagari.contains_any(
                title, cn.NEPALI_NOTICE_KEYWORDS), title)
            self.assertTrue(cn._is_nav_chrome(title, url), url)
            self.assertEqual(self._titles([(url, title)]), [], url)
        # The plural form is a substring hit, because a `\b` boundary is a Latin
        # concept Devanagari does not share (सूचना -> सूचनाहरू).
        self.assertTrue(cn._devanagari.contains_any(
            "पुराना सूचनाहरू", cn.NEPALI_NOTICE_KEYWORDS))

    def test_a_devanagari_nav_label_on_a_detail_url_is_still_caught(self):
        """The two Latin-only text rules stay inert rather than wrong: with a
        Devanagari anchor the slug-echo has nothing to compare (the URL is
        Latin, the anchor is not), so it must not fire and delete the row."""
        url, title = ("https://tu.edu.np/notices/14988",
                      "त्रिवि केन्द्रीय कार्यालयका लागि शिलबन्दी दरभाउपत्र आह्वानको "
                      "सूचना 2026-09-01")
        self.assertFalse(cn._is_nav_chrome(title, url))
        self.assertEqual(self._titles([(url, title)]), [title])

    # -- 4. Latin is untouched --------------------------------------------
    def test_latin_detail_notices_still_work_and_keep_their_language(self):
        """The frozen English path, re-asserted: every measured real English
        notice is still collected, and it is still labelled `en`."""
        for url, title in LIVE_DETAIL_URLS:
            out = self._cands([(url, title)])
            self.assertEqual([c["title"] for c in out], [title], url)
            self.assertEqual(out[0]["language"], "en")
            self.assertEqual(out[0]["published"], None)

    def test_the_english_rules_are_unchanged(self):
        """The historical end-to-end result for the Latin board, verbatim: the
        same three real notices, the same chrome refused. If the script-aware
        gate changed ANY English behaviour, this fails."""
        self.assertEqual(
            self._titles(self._nav_pairs(), "https://nea.org.np/"),
            ["Exam notice published 2026-09-20",
             "BSc CSIT result published 2026-09-21",
             "Admission form notice 2026-09-22"])
        # The gate itself, called directly: presence admits, absence rejects.
        for text, expected in (("Result published", True),
                               ("NOTICE", True),
                               ("B.Sc.CSIT I Semester-2082 Exam Result", True),
                               ("Academic Calendar of B.Sc.CSIT V Semester", False),
                               ("Audio Notice", True),
                               # "Rates" is NOT the weak keyword "rate": it never
                               # reached the weak-keyword rule even before this
                               # change, because the keyword gate refused it
                               # first. "Base Rate" below IS what that rule is for.
                               ("Rates & Charges", False),
                               ("Base Rate", True),
                               ("Branch Locator", True),
                               ("Administration", False),
                               ("Directorate of Examination", False),
                               ("", False)):
            self.assertEqual(cn.notice_keyword_verdict(text)[0], expected, text)
        # `rate`/`branch` remain WEAK: a product label with no content word is
        # chrome, exactly as before, and a real notice that merely mentions a
        # rate is still a notice.
        self.assertTrue(cn._is_nav_chrome(
            "Base Rate & Interest Spread Rate",
            "https://www.bank.example.com/notices/1234"))
        self.assertFalse(cn._is_nav_chrome(
            "Interest rate notice published",
            "https://www.bank.example.com/notices/1234"))

    @staticmethod
    def _nav_pairs():
        return [("/notices/1", "Exam notice published 2026-09-20"),
                ("/notices/2", "BSc CSIT result published 2026-09-21"),
                ("/notices/3", "Admission form notice 2026-09-22"),
                ("/directorate", "Directorate of Examination"),
                ("/notices/4", "Rates & Charges"),
                ("/notices/5", "Branch Locator")]

    # -- 5. a script we have no vocabulary for ----------------------------
    def test_an_unrecognised_script_title_is_kept_not_dropped(self):
        """Absence of OUR words is not evidence about a title in a script we
        cannot read. Dropping it is the same blindness in a new coat, so it is
        kept -- and reported as `unknown`, not mislabelled `en`."""
        for title in ("สัญญาณประกาศ ภายใน", "إشعار داخلي",   # Thai, Arabic
                      "公告と通知", "Ειδοποίηση και ανακοινώσεις"):
            url = "https://x.example/notices/99001"
            self.assertEqual(cn._devanagari.detect_script(title), "empty")
            self.assertFalse(cn.NOTICE_KEYWORDS_RE.search(title))
            keep, language = cn.notice_keyword_verdict(title)
            self.assertTrue(keep, title)
            self.assertEqual(language, "unknown", title)
            out = self._cands([(url, title)])
            self.assertEqual([c["title"] for c in out], [title], title)
            self.assertEqual(out[0]["language"], "unknown", title)
        # ... and the structural rules still apply to an unknown script.
        for url in ("https://x.example/notices",
                    "https://x.example/department/x",
                    "https://x.example/portal/login"):
            self.assertEqual(
                self._titles([(url, "สัญญาณประกาศ ภายใน")], "https://x.example/"),
                [], url)

    def test_an_anchor_with_no_letters_at_all_is_never_a_notice(self):
        """The one narrowing this gate needed: a bare number or punctuation is
        not a title in ANY script, and the widened branches would otherwise
        have admitted it."""
        for text in ("२०८१", "2081", "2026-09-09", "///", "12:30"):
            keep, _ = cn.notice_keyword_verdict(text)
            self.assertFalse(keep, text)
        for url, text in (("/notices/1", "२०८१"), ("/notices/2", "2026-09-09")):
            self.assertEqual(self._titles([(url, text)]), [], text)

    # -- 6. the vocabulary itself -----------------------------------------
    def test_the_nepali_vocabulary_is_the_notice_vocabulary(self):
        """Every word the notice contract names is present, and the academic
        path's administrative list is NOT reused: it contains words that are
        not notice words at all (अध्ययन = study, छुट्टी = holiday, साइकिल =
        cycle) and reusing it would admit a teaching-materials nav bar."""
        for word in ("सूचना", "नतिजा", "परीक्षा", "भर्ना", "परिपत्र",
                     "तालिका", "योग्यता", "कोटा", "रिक्त", "प्रकाशन",
                     "समयसारण"):
            self.assertIn(word, cn.NEPALI_NOTICE_KEYWORDS, word)
        for word in ("अध्ययन", "छुट्टी", "साइकिल", "स्मारक", "रजिस्ट्र"):
            self.assertNotIn(word, cn.NEPALI_NOTICE_KEYWORDS, word)
        for word in ("अध्ययन", "छुट्टी", "साइकिल"):
            self.assertIn(word, cn._devanagari.NEPALI_NON_RESULT_TERMS, word)

    def test_the_tested_script_detector_is_reused_not_reimplemented(self):
        """One script detector in the run. A second one would be how this gate
        and the academic result path drift apart on what script a title is."""
        import devanagari as dv
        self.assertIs(cn._devanagari.detect_script, dv.detect_script)
        self.assertIs(cn._devanagari.latinize_digits, dv.latinize_digits)
        self.assertEqual(cn._devanagari.detect_script(
            "B.Sc. प्रवेश परीक्षा 2083 को नतिजा"), "mixed")
        # No second detector was written in this file.
        import inspect
        src = inspect.getsource(cn)
        self.assertNotIn("def detect_script", src)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestCollectionConfig(unittest.TestCase):
    def test_shipped_config_values(self):
        with open(REPO_CONFIG, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["schema_version"], 2)
        cfg = cn.load_collection_config(REPO_CONFIG)
        self.assertEqual(cfg["critical_hosts"],
                         ["tu.edu.np", "iost.tu.edu.np", "news.google.com"])
        self.assertEqual(cfg["timeout_sec"]["tu.edu.np"], 20)
        self.assertEqual(cfg["timeout_sec"]["iost.tu.edu.np"], 20)
        self.assertEqual(cfg["timeout_sec"]["default"], 10)
        self.assertEqual(cfg["retry"]["attempts"], 2)
        self.assertEqual(cfg["retry"]["backoff_sec"], [0.75])
        self.assertEqual(sorted(cfg["retry"]["retry_on"]),
                         sorted(["timeout", "connection", "dns", "tls",
                                 "throttled", "http_5xx"]))
        self.assertEqual(cfg["retry"]["resolve_timeout_sec"], 5.0)
        self.assertEqual(cfg["soft_budget_sec"], 75.0)
        self.assertEqual(cfg["max_workers"], 8)
        self.assertEqual(cfg["min_items_per_source"], 1)
        self.assertEqual(cn.DEFAULT_RETRY_CFG["resolve_timeout_sec"], 5.0)
        # critical_hosts is deliberately UNCHANGED by v2: tu.edu.np,
        # iost.tu.edu.np and news.google.com were already there, which is what
        # makes the `empty` signal live on day one with no host added.
        self.assertEqual(sorted(on_disk["critical_hosts"]),
                         sorted(cn.DEFAULT_CRITICAL_HOSTS))

    def test_min_items_per_source_is_loaded_and_floored(self):
        tmp = tempfile.mkdtemp(prefix="coll-minitems-")
        path = os.path.join(tmp, "collection.json")

        def _write(payload):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            return cn.load_collection_config(path)

        cfg = _write({"critical_hosts": ["a.example"],
                      "min_items_per_source": 3})
        self.assertEqual(cfg["min_items_per_source"], 3)
        # A 0 or negative floor would make EVERY source empty, so it is raised
        # to 1 (the default) rather than honoured.
        for bad in (0, -7, None, "soon", [], True):
            self.assertEqual(_write({"min_items_per_source": bad})
                             ["min_items_per_source"], 1, bad)
        # Absent -> the default.
        self.assertEqual(_write({"critical_hosts": ["a.example"]})
                         ["min_items_per_source"],
                         cn.DEFAULT_MIN_ITEMS_PER_SOURCE)
        self.assertEqual(cn._built_in_config()["min_items_per_source"], 1)


    def test_missing_config_falls_back_with_one_warning(self):
        buf = io.StringIO()
        stdout = sys.stdout
        sys.stdout = buf
        try:
            cfg = cn.load_collection_config(
                os.path.join("no", "such", "collection.json"))
        finally:
            sys.stdout = stdout
        self.assertEqual(buf.getvalue().count("WARNING"), 1)
        self.assertEqual(cfg["retry"]["attempts"], cn.DEFAULT_RETRY_ATTEMPTS)
        self.assertEqual(cfg["soft_budget_sec"], cn.SOFT_BUDGET_SEC)
        self.assertEqual(cfg["max_workers"], cn.MAX_WORKERS)
        self.assertEqual(cfg["timeout_sec"]["default"], cn.DEFAULT_TIMEOUT_SEC)
        self.assertEqual(cfg["timeout_sec"]["tu.edu.np"], 20)

    def test_corrupt_config_falls_back_with_one_warning(self):
        tmp = tempfile.mkdtemp(prefix="coll-cfg-")
        path = os.path.join(tmp, "collection.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ truncated")
        buf = io.StringIO()
        stdout = sys.stdout
        sys.stdout = buf
        try:
            cfg = cn.load_collection_config(path)
        finally:
            sys.stdout = stdout
        self.assertEqual(buf.getvalue().count("WARNING"), 1)
        self.assertEqual(cfg["max_workers"], cn.MAX_WORKERS)

    def test_junk_keys_fall_back_without_raising(self):
        tmp = tempfile.mkdtemp(prefix="coll-cfg2-")
        path = os.path.join(tmp, "collection.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"critical_hosts": "nope", "timeout_sec": ["a"],
                       "retry": {"attempts": 0, "backoff_sec": "x",
                                 "retry_on": 5},
                       "soft_budget_sec": "soon", "max_workers": None}, f)
        cfg = cn.load_collection_config(path)
        self.assertEqual(cfg["retry"]["attempts"],
                         cn.DEFAULT_RETRY_ATTEMPTS)
        self.assertEqual(cfg["retry"]["backoff_sec"], [0.75])
        self.assertEqual(cfg["soft_budget_sec"], cn.SOFT_BUDGET_SEC)
        self.assertEqual(cfg["max_workers"], cn.MAX_WORKERS)
        self.assertEqual(cfg["critical_hosts"], cn.DEFAULT_CRITICAL_HOSTS)

    def test_per_host_timeout_is_applied_by_the_plan_builder(self):
        cfg = cn.load_collection_config(REPO_CONFIG)
        plans = cn.build_all_plans({}, cfg, None)
        seen = {}
        rec = Recorder({"open-meteo": WEATHER_BODY,
                        "examplebank": NOTICE_HTML}, default=rss_doc())
        with Patches(rec) as p:
            for plan in plans:
                try:
                    plan["fn"]()
                except Exception:
                    pass
            for call in rec.calls:
                seen[call["url"]] = call["timeout"]
        self.assertEqual(seen["https://tu.edu.np/"], 20)
        self.assertEqual(seen["https://iost.tu.edu.np/"], 20)
        self.assertEqual(seen["https://college.example.edu/"], 10)
        self.assertEqual(seen["https://www.bank.example.com/"], 10)
        self.assertTrue(all(t == 10 for u, t in seen.items()
                            if "news.google.com" in u or "open-meteo" in u))
        self.assertGreaterEqual(len(seen), 8)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

class TestRetryPolicy(unittest.TestCase):
    # resolve_timeout_sec is deliberately tiny here: the pre-flight is not what
    # these tests exercise, and no test should ever wait seconds for a probe.
    retry = {"attempts": 2, "backoff_sec": [0.75],
             "retry_on": ["timeout", "connection", "dns", "tls", "throttled",
                          "http_5xx"],
             "resolve_timeout_sec": 0.5}

    def _run(self, error, cfg_retry=None):
        rec = Recorder({"tu.edu.np/": [error]}, default=rss_doc())
        att = {"n": 0}
        with Patches(rec) as p:
            _ok, _failed, _by, _raw, _ws, results = cn.collect_parallel(
                [{"name": "notice:tu.edu.np", "kind": "notice",
                  "category": "education_tu", "host": "tu.edu.np",
                  "url": "https://tu.edu.np/", "att": att,
                  "fn": lambda: cn.notices_task(
                      "https://tu.edu.np/", "education_tu", 20,
                      cfg_retry or self.retry, cn._attempts_recorder(att))}],
                {"soft_budget_sec": 30, "max_workers": 1})
            slept = list(p.slept)
        return rec, results, slept

    def test_retries_on_every_listed_reason_code(self):
        cases = {
            "timeout": urllib.error.URLError("timed out"),
            "connection": urllib.error.URLError(
                socket.gaierror(111, "Connection refused")),
            "dns": dns_error(),
            "tls": urllib.error.URLError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
            "throttled": http_error(429),
            "http_5xx": http_error(503),
        }
        for code, exc in cases.items():
            rec, results, slept = self._run(exc)
            self.assertEqual(rec.count("tu.edu.np/"), 2, code)
            self.assertEqual(results[0]["attempts"], 2, code)
            self.assertEqual(slept, [0.75], code)

    def test_does_not_retry_on_4xx(self):
        rec, results, slept = self._run(http_error(403))
        self.assertEqual(rec.count("tu.edu.np/"), 1)
        self.assertEqual(results[0]["attempts"], 1)
        self.assertEqual(slept, [])
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(sh.classify_reason(results[0]["error"]), "http_4xx")

    def test_does_not_retry_on_404(self):
        rec, results, slept = self._run(http_error(404))
        self.assertEqual(rec.count("tu.edu.np/"), 1)
        self.assertEqual(slept, [])

    def test_does_not_retry_on_parse_error(self):
        rec, results, slept = self._run(parse_error())
        self.assertEqual(rec.count("tu.edu.np/"), 1)
        self.assertEqual(slept, [])
        self.assertEqual(sh.classify_reason(results[0]["error"]), "parse")

    def test_retry_succeeds_on_second_attempt(self):
        rec = Recorder({"tu.edu.np/": [dns_error(), NOTICE_HTML]},
                       default=rss_doc())
        att = {"n": 0}
        with Patches(rec) as p:
            _ok, _f, _b, raw, _ws, results = cn.collect_parallel(
                [{"name": "notice:tu.edu.np", "kind": "notice",
                  "category": "education_tu", "host": "tu.edu.np",
                  "url": "https://tu.edu.np/", "att": att,
                  "fn": lambda: cn.notices_task("https://tu.edu.np/",
                                                "education_tu", 20,
                                                self.retry,
                                                cn._attempts_recorder(att))}],
                {"soft_budget_sec": 30, "max_workers": 1})
            slept = list(p.slept)
        # The retry succeeded AND the page yielded a real notice anchor, so the
        # row is a productive `ok` with items_found 1. (This is also the guard
        # that the word-boundary + nav filters did not over-delete: a genuine
        # "/notice/1" anchor survives both.)
        self.assertEqual(results[0]["outcome"], "ok")
        self.assertEqual(results[0]["items_found"], 1)
        self.assertEqual(results[0]["attempts"], 2)
        self.assertEqual(results[0]["error"], "")
        self.assertEqual(slept, [0.75])
        self.assertEqual(len(raw), 1)

    def test_attempts_is_one_when_retry_disabled(self):
        rec, results, _slept = self._run(dns_error(), cfg_retry={
            "attempts": 1, "backoff_sec": [0.75],
            "retry_on": ["timeout", "connection", "dns", "tls", "throttled",
                         "http_5xx"], "resolve_timeout_sec": 0.5})
        self.assertEqual(rec.count("tu.edu.np/"), 1)
        self.assertEqual(results[0]["attempts"], 1)

    def test_success_never_retries(self):
        rec = Recorder(default=rss_doc())
        with Patches(rec) as p:
            _ok, _f, _b, _raw, _ws, results = cn.collect_parallel(
                [{"name": "gnews:np-en:Example City", "kind": "gnews",
                  "category": "local_city", "host": "news.google.com",
                  "url": "https://news.google.com/rss?q=Example City",
                  "att": {"n": 0},
                  "fn": lambda: cn.rss_task("https://news.google.com/rss?q=Example City",
                                            "local_city", "en", 10,
                                            self.retry, None)}],
                {"soft_budget_sec": 30, "max_workers": 1})
            slept = list(p.slept)
        self.assertEqual(rec.count("news.google.com"), 1)
        self.assertEqual(slept, [])
        self.assertEqual(results[0]["attempts"], 1)

    def test_retry_sleeps_exactly_once_for_two_attempts(self):
        rec, _results, slept = self._run(dns_error())
        self.assertEqual(len(slept), 1)


# ---------------------------------------------------------------------------
# The mandatory budget arithmetic guard
# ---------------------------------------------------------------------------

class TestBudgetArithmetic(unittest.TestCase):
    WORKFLOW_STEP_TIMEOUT_SEC = 180  # .github/workflows/daily-news.yml:230 timeout-minutes: 3

    def test_tuning_constant_cannot_blow_the_workflow_step(self):
        """TRUE bound: soft budget PLUS the longest in-flight task.

        Not `timeout*(attempts-1) + backoff + budget`: the thread pool is
        exited through `with ThreadPoolExecutor(...)`, which does
        shutdown(wait=True), so a straggler started before the deadline is
        waited out in full. The overshoot is therefore one COMPLETE retry
        (timeout + backoff), i.e. 20 + 0.75 on top of the 75s budget = 115.75s.
        """
        cfg = cn.load_collection_config(REPO_CONFIG)
        attempts = int(cfg["retry"]["attempts"])
        backoff = list(cfg["retry"]["backoff_sec"])
        soft_budget = float(cfg["soft_budget_sec"])
        timeout_max = max(float(v) for v in cfg["timeout_sec"].values())
        # Worst single task: every attempt burns the full timeout, plus sleeps.
        longest_in_flight = timeout_max * attempts + sum(backoff)
        total = soft_budget + longest_in_flight
        self.assertAlmostEqual(longest_in_flight, 20 * 2 + 0.75, places=6)
        self.assertAlmostEqual(total, 115.75, places=6)
        self.assertLessEqual(total, self.WORKFLOW_STEP_TIMEOUT_SEC,
                             "retry tuning pushes the collector past the "
                             "workflow step timeout: %r" % total)

    def test_worst_case_overshoot_is_bounded_and_headroom_is_material(self):
        cfg = cn.load_collection_config(REPO_CONFIG)
        attempts = int(cfg["retry"]["attempts"])
        backoff = sum(cfg["retry"]["backoff_sec"])
        timeout_max = max(float(v) for v in cfg["timeout_sec"].values())
        soft_budget = float(cfg["soft_budget_sec"])
        overshoot = soft_budget + timeout_max * attempts + backoff
        self.assertAlmostEqual(overshoot, 115.75, places=6)
        self.assertLessEqual(overshoot, self.WORKFLOW_STEP_TIMEOUT_SEC)
        self.assertGreaterEqual(self.WORKFLOW_STEP_TIMEOUT_SEC - overshoot, 30.0)

    def test_dns_preflight_bound_stays_inside_the_step(self):
        # The resolver pre-flight is a second thing that can be waited out, so
        # it has to fit in the same headroom as the retry overshoot.
        cfg = cn.load_collection_config(REPO_CONFIG)
        resolve_timeout = float(cfg["retry"]["resolve_timeout_sec"])
        self.assertGreaterEqual(resolve_timeout, 0.1)
        self.assertLessEqual(resolve_timeout, 10.0)
        worst = (float(cfg["soft_budget_sec"])
                 + max(float(v) for v in cfg["timeout_sec"].values())
                 * int(cfg["retry"]["attempts"])
                 + sum(cfg["retry"]["backoff_sec"]))
        self.assertLessEqual(worst + resolve_timeout + 1.0,
                             self.WORKFLOW_STEP_TIMEOUT_SEC)


# ---------------------------------------------------------------------------
# source_results (contract A1)
# ---------------------------------------------------------------------------

class TestSourceResults(unittest.TestCase):
    def test_exact_key_set_and_types(self):
        rec = Recorder({"tu.edu.np/": [dns_error(), dns_error()]},
                       default=rss_doc())
        plans = [
            {"name": "notice:tu.edu.np", "kind": "notice",
             "category": "education_tu", "host": "tu.edu.np",
             "url": "https://tu.edu.np/", "att": {"n": 0},
             "fn": lambda: cn.notices_task("https://tu.edu.np/",
                                           "education_tu", 20, None, None)},
            {"name": "gnews:np-en:Example City", "kind": "gnews",
             "category": "local_city", "host": "news.google.com",
             "url": "https://news.google.com/rss?q=Example City", "att": {"n": 0},
             "fn": lambda: cn.rss_task("https://news.google.com/rss?q=Example City",
                                       "local_city", "en", 10, None, None)},
        ]
        with Patches(rec):
            _ok, _f, _b, _raw, _ws, results = cn.collect_parallel(
                plans, {"soft_budget_sec": 30, "max_workers": 2})
        for row in results:
            self.assertEqual(set(row), {"name", "kind", "category", "host",
                                        "url", "outcome", "error", "attempts",
                                        "secs", "items_found"})
            self.assertIsInstance(row["name"], str)
            self.assertIn(row["kind"], ("gnews", "notice", "weather"))
            self.assertIsInstance(row["category"], str)
            self.assertIsInstance(row["host"], str)
            self.assertIsInstance(row["url"], str)
            self.assertIn(row["outcome"],
                          ("ok", "empty", "failed", "skipped_budget"))
            self.assertIsInstance(row["error"], str)
            self.assertIsInstance(row["attempts"], int)
            self.assertGreaterEqual(row["attempts"], 1)
            self.assertIsInstance(row["secs"], float)
            # v2: items_found is always present and always a non-negative int.
            # A missing key would mean UNKNOWN at the reader, and this collector
            # always knows.
            self.assertIsInstance(row["items_found"], int)
            self.assertNotIsInstance(row["items_found"], bool)
            self.assertGreaterEqual(row["items_found"], 0)
            # error is "" if and only if the fetch SUCCEEDED, i.e. outcome is
            # "ok" or the additive "empty" (answered, nothing usable).
            self.assertEqual(row["error"] == "",
                             row["outcome"] in ("ok", "empty"))
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(results[0]["attempts"], 1)
        # failed/skipped rows ALWAYS carry items_found 0 -- never an unknown.
        self.assertEqual(results[0]["items_found"], 0)
        self.assertLessEqual(len(results[0]["error"]), 200)
        self.assertEqual(results[1]["outcome"], "ok")
        self.assertEqual(results[1]["error"], "")
        self.assertEqual(results[1]["items_found"], 1)

    def test_plan_order_not_completion_order(self):
        cfg = cn.load_collection_config(REPO_CONFIG)
        plans = [
            {"name": "slow", "kind": "notice", "category": "education_tu",
             "host": "slow.example", "url": "https://slow.example/",
             "att": {"n": 0}, "fn": _sleeper(0.25)},
            {"name": "fast", "kind": "notice", "category": "education_tu",
             "host": "fast.example", "url": "https://fast.example/",
             "att": {"n": 0}, "fn": lambda: []},
        ]
        with Patches(Recorder()):
            ok, _failed, _by_source, _raw, _ws, results = cn.collect_parallel(
                plans, {"soft_budget_sec": 30, "max_workers": 4})
        # Plan order in the record, completion order in sources_ok.
        self.assertEqual([r["name"] for r in results], ["slow", "fast"])
        self.assertNotEqual(ok, [r["name"] for r in results])
        self.assertEqual(sorted(ok), ["fast", "slow"])

    def test_reachable_with_zero_items_is_empty_not_ok(self):
        """v2: reachability alone is no longer the whole claim.

        This is the exact shape that used to hide: a well-formed feed with an
        empty channel answered 200 and reported `ok`, so "I checked and there
        was nothing" was indistinguishable from "I checked and got garbage
        back". It is still NOT a failure.
        """
        rec = Recorder(default=b"<?xml version='1.0'?><rss><channel></channel></rss>")
        with Patches(rec):
            ok, failed, _b, raw, _ws, results = cn.collect_parallel(
                [{"name": "gnews:np-en:Example City", "kind": "gnews",
                  "category": "local_city", "host": "news.google.com",
                  "url": "https://news.google.com/rss?q=Example City",
                  "att": {"n": 0},
                  "fn": lambda: cn.rss_task("https://news.google.com/rss?q=Example City",
                                            "local_city", "en", 10, None,
                                            None)}],
                {"soft_budget_sec": 30, "max_workers": 1})
        self.assertEqual(raw, [])
        # Not a failure: the fetch succeeded, so it stays in sources_ok.
        self.assertEqual(failed, [])
        self.assertEqual(ok, ["gnews:np-en:Example City"])
        self.assertEqual(results[0]["outcome"], "empty")
        self.assertEqual(results[0]["error"], "")
        self.assertEqual(results[0]["items_found"], 0)
        # And it is still REACHABLE, so nothing on this host can be deleted.
        self.assertEqual(sh.unreachable_hosts(results), set())
        self.assertEqual(sh.reachable_hosts_set(results), {"news.google.com"})

    def test_items_found_is_counted_after_the_tasks_own_filter(self):
        """items_found measures what the source PRODUCED.

        Counted from the task's own candidates -- after the notice keyword test,
        and BEFORE any global/per-domain cap or cross-source dedup -- because
        the banner may only claim a number it can actually attribute.
        """
        rec = Recorder(default=NOTICE_HTML)
        with Patches(rec):
            _ok, _f, _b, raw, _ws, results = cn.collect_parallel(
                [{"name": "notice:tu.edu.np", "kind": "notice",
                  "category": "education_tu", "host": "tu.edu.np",
                  "url": "https://tu.edu.np/", "att": {"n": 0},
                  "fn": lambda: cn.notices_task("https://tu.edu.np/",
                                                "education_tu", 20, None,
                                                None)}],
                {"soft_budget_sec": 30, "max_workers": 1})
        # NOTICE_HTML holds 2 anchors but only one matches a notice keyword.
        self.assertEqual(len(raw), 1)
        self.assertEqual(results[0]["items_found"], 1)
        self.assertEqual(results[0]["outcome"], "ok")

    def test_min_items_per_source_makes_a_thin_source_empty(self):
        rec = Recorder(default=rss_doc(2))
        plan = [{"name": "gnews:np-en:Example City", "kind": "gnews",
                 "category": "local_city", "host": "news.google.com",
                 "url": "https://news.google.com/rss?q=Example City",
                 "att": {"n": 0},
                 "fn": lambda: cn.rss_task("https://news.google.com/rss?q=Example City",
                                          "local_city", "en", 10, None,
                                          None)}]
        cfg = {"soft_budget_sec": 30, "max_workers": 1,
               "min_items_per_source": 3}
        with Patches(rec):
            _ok, failed, _b, raw, _ws, results = cn.collect_parallel(plan, cfg)
        # 2 < 3: reachable but under the floor -> `empty`, and still not a
        # failure.
        self.assertEqual(results[0]["outcome"], "empty")
        self.assertEqual(results[0]["items_found"], 2)
        self.assertEqual(failed, [])
        self.assertEqual(len(raw), 2)
        # A floor of 0/negative cannot be honoured: it would empty everything.
        with Patches(rec):
            _ok, _f, _b, _r2, _w2, res2 = cn.collect_parallel(
                plan, {"soft_budget_sec": 30, "max_workers": 1,
                       "min_items_per_source": 0})
        self.assertEqual(res2[0]["outcome"], "ok")

    def test_weather_row_counts_one_when_a_box_was_built(self):
        rec = Recorder(default=weather_hourly_body())
        # Place + fetched_at must be real, or normalize_hourly_forecast refuses
        # the payload and leaves box["payload"] None.
        box = cn.new_weather_box(
            {"place": {"city": "Example City", "latitude": 27.7,
                       "longitude": 85.3, "timezone": "Asia/Kathmandu"}},
            cn.kathmandu_now_str())
        with Patches(rec):
            _ok, _f, _b, _raw, _ws, results = cn.collect_parallel(
                [{"name": "weather:open-meteo", "kind": "weather",
                  "category": "", "host": "api.open-meteo.com",
                  "url": "https://api.open-meteo.com/v1", "att": {"n": 0},
                  "fn": lambda: cn.weather_task(
                      "https://api.open-meteo.com/v1", 10, None, None, None,
                      box)}],
                {"soft_budget_sec": 30, "max_workers": 1})
        self.assertEqual(results[0]["outcome"], "ok")
        self.assertEqual(results[0]["items_found"], 1)
        self.assertIsNotNone(box.get("payload"))

    def test_budget_expired_is_skipped_not_failed(self):
        plans = [
            {"name": "slow", "kind": "notice", "category": "education_tu",
             "host": "slow.example", "url": "https://slow.example/",
             "att": {"n": 0}, "fn": _sleeper(0.4)},
        ]
        with Patches(Recorder()):
            ok, failed, _b, _raw, _ws, results = cn.collect_parallel(
                plans, {"soft_budget_sec": 0.05, "max_workers": 1})
        self.assertEqual(results[0]["outcome"], "skipped_budget")
        self.assertEqual(results[0]["attempts"], 1)
        self.assertNotEqual(results[0]["error"], "")
        # Historical shape preserved: it still shows up in sources_failed.
        self.assertEqual(ok, [])
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].startswith("slow ("))
        # ...but the truth record never calls it a failure.
        health = sh.compute_source_health(results, ["slow.example"],
                                          "2026-09-27 06:12")
        self.assertEqual(health["all"]["failed"], 0)
        self.assertEqual(health["all"]["skipped"], 1)
        self.assertEqual(health["critical"]["hosts_down"], [])
        self.assertEqual(health["state"], "unknown")

    def test_skipped_budget_source_does_not_degrade_health(self):
        results = [
            {"name": "gnews:np-en:Example City", "kind": "gnews",
             "category": "local_city", "host": "news.google.com",
             "url": "u", "outcome": "ok", "error": "", "attempts": 1,
             "secs": 0.1},
            {"name": "notice:tu.edu.np", "kind": "notice",
             "category": "education_tu", "host": "tu.edu.np", "url": "u",
             "outcome": "skipped_budget", "error": "soft budget exceeded",
             "attempts": 1, "secs": 0.0},
        ]
        health = sh.compute_source_health(results,
                                          ["tu.edu.np", "news.google.com"],
                                          "2026-09-27 06:12")
        self.assertEqual(health["state"], "partial")
        self.assertEqual(health["critical"]["hosts_down"], [])


class TestDnsPreflight(unittest.TestCase):
    """P3: getaddrinfo is NOT bounded by urlopen(timeout=...)."""

    def setUp(self):
        self.calls = []
        self.resolver = cn.DnsResolver(resolve_timeout=0.3,
                                    idle_quit_sec=0.2, wedge_cooldown_sec=5.0)
        self.addCleanup(self.resolver.close)
        socket.getaddrinfo = self._patch(lambda host, *a, **kw: [])

    def tearDown(self):
        socket.getaddrinfo = _offline_getaddrinfo

    def _patch(self, fn):
        def fake(host, *a, **kw):
            self.calls.append(host)
            return fn(host, *a, **kw)
        socket.getaddrinfo = fake
        return fake

    def test_preflight_runs_before_the_fetch_and_caches_per_host(self):
        self._patch(lambda host, *a, **kw: [])
        rec = Recorder(default=rss_doc())
        retry = dict(cn.DEFAULT_RETRY_CFG)
        with Patches(rec):
            for _ in range(3):
                cn.fetch_bytes_retry("https://news.google.com/rss?q=a", 10,
                                     100, retry, None, self.resolver)
        self.assertEqual(self.calls, ["news.google.com"])
        self.assertEqual(rec.count("news.google.com"), 3)

    def test_hung_resolver_returns_within_the_bound_and_is_classified_dns(self):
        release = threading.Event()
        entered = threading.Event()

        def hang(host, *a, **kw):
            entered.set()
            release.wait(30)
            return []

        self._patch(hang)
        rec = Recorder(default=rss_doc())
        retry = {"attempts": 2, "backoff_sec": [], "retry_on": ["dns"],
                 "resolve_timeout_sec": 0.3}
        t0 = time.monotonic()
        try:
            with Patches(rec):
                ok, err = self.resolver.resolve("https://hang.example/x", 0.3)
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            if self.resolver._thread is not None:
                self.resolver._thread.join(timeout=3.0)
        # The regression signal: the caller is released while the resolver is
        # still wedged (it would otherwise wait 30s, or forever pre-fix).
        self.assertLess(elapsed, 5.0,
                        "resolve() must not wait out a hung resolver")
        self.assertFalse(ok)
        self.assertEqual(sh.classify_reason(err), "dns")

    def test_hung_resolver_still_records_a_truthful_outcome(self):
        release = threading.Event()
        entered = threading.Event()

        def hang(host, *a, **kw):
            entered.set()
            release.wait(30)
            return []

        self._patch(hang)

        def failing_fetch(url, timeout, max_bytes):
            raise urllib.error.URLError("getaddrinfo failed")

        rec = Recorder(default=rss_doc())
        retry = {"attempts": 2, "backoff_sec": [], "retry_on": ["dns"],
                 "resolve_timeout_sec": 0.3}
        t0 = time.monotonic()
        try:
            with Patches(failing_fetch):
                _ok, _f, _b, _raw, _ws, results = cn.collect_parallel(
                    [{"name": "notice:hang.example", "kind": "notice",
                      "category": "education_tu", "host": "hang.example",
                      "url": "https://hang.example/", "att": {"n": 0},
                      "fn": lambda: cn.notices_task("https://hang.example/",
                                                    "education_tu", 1, retry,
                                                    None, self.resolver)}],
                    {"soft_budget_sec": 30, "max_workers": 1})
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            if self.resolver._thread is not None:
                self.resolver._thread.join(timeout=3.0)
        self.assertLess(elapsed, 10.0)
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(sh.classify_reason(results[0]["error"]), "dns")
        self.assertIn("DNSResolverTimeout", results[0]["error"])

    def test_dead_resolver_error_is_preferred_over_the_socket_symptom(self):
        self._patch(lambda host, *a, **kw: (_ for _ in ()).throw(
            socket.gaierror(-3, "Temporary failure in name resolution")))

        def failing_fetch(url, timeout, max_bytes):
            raise urllib.error.URLError("Connection refused")

        retry = {"attempts": 1, "backoff_sec": [], "retry_on": ["dns"],
                 "resolve_timeout_sec": 1.0}
        with Patches(failing_fetch):
            _ok, _f, _b, _raw, _ws, results = cn.collect_parallel(
                [{"name": "notice:tu.edu.np", "kind": "notice",
                  "category": "education_tu", "host": "tu.edu.np",
                  "url": "https://tu.edu.np/", "att": {"n": 0},
                    "fn": lambda: cn.notices_task("https://tu.edu.np/",
                                                  "education_tu", 20, retry,
                                                  None, self.resolver)}],

                {"soft_budget_sec": 30, "max_workers": 1})
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(sh.classify_reason(results[0]["error"]), "dns")
        self.assertIn("name resolution", results[0]["error"])

    def test_preflight_is_advisory_and_cannot_cost_content(self):
        # A false-negative pre-flight must never delete a story: the real fetch
        # is still made and its success is what counts.
        self._patch(lambda host, *a, **kw: (_ for _ in ()).throw(
            OSError("resolver says no")))
        rec = Recorder(default=rss_doc())
        retry = {"attempts": 1, "backoff_sec": [], "retry_on": [],
                 "resolve_timeout_sec": 1.0}
        with Patches(rec):
            _ok, failed, _b, raw, _ws, results = cn.collect_parallel(
                [{"name": "gnews:np-en:Example City", "kind": "gnews",
                  "category": "local_city", "host": "news.google.com",
                  "url": "https://news.google.com/rss?q=Example City",
                  "att": {"n": 0},
                  "fn": lambda: cn.rss_task("https://news.google.com/rss?q=Example City",
                                            "local_city", "en", 10, retry,
                                            None)}],
                {"soft_budget_sec": 30, "max_workers": 1})
        self.assertEqual(results[0]["outcome"], "ok")
        self.assertEqual(results[0]["error"], "")
        self.assertEqual(failed, [])
        self.assertEqual(len(raw), 1)

    def test_preflight_does_not_change_a_normal_success(self):
        self._patch(lambda host, *a, **kw: [])
        rec = Recorder(default=rss_doc())
        with Patches(rec):
            data = cn.fetch_bytes_retry("https://ok.example/x", 10, 100,
                                        dict(cn.DEFAULT_RETRY_CFG), None)
        self.assertEqual(data, rss_doc())
        self.assertEqual(rec.count("ok.example"), 1)

    def test_one_helper_thread_only(self):
        alive = lambda: [t for t in threading.enumerate()
                         if t.name == "dnsmain" and t.is_alive()]
        before = len(alive())
        with Patches(Recorder()):
            for i in range(6):
                self.resolver.resolve("https://h%d.example/" % i, 0.3)
                self.assertLessEqual(len(alive()), before + 1,
                                     "one shared resolver thread, not one per "
                                     "fetch")
        self.assertIsNotNone(self.resolver._thread)
        self.assertTrue(self.resolver._thread.daemon,
                        "the helper must never delay interpreter exit")
        self.assertLessEqual(self.resolver.probe_count, 6)
        self.resolver.close()
        self.assertIsNone(self.resolver._thread)
        self.assertTrue(self.resolver.closed)

    def test_close_means_no_blocking_and_no_state_carryover(self):
        # The whole point of per-run state: a second run in the same process
        # cannot inherit a verdict (or a wedge) from the first.
        self._patch(lambda host, *a, **kw: (_ for _ in ()).throw(
            socket.gaierror(-3, "Temporary failure in name resolution")))
        first = self.resolver.resolve("https://dead.example/x", 0.3)
        self.assertFalse(first[0])
        self.assertEqual(self.resolver.probe_count, 1)
        self.resolver.close()
        # A fresh resolver is a clean slate.
        fresh = cn.DnsResolver(resolve_timeout=0.3, idle_quit_sec=0.2)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.probe_count, 0)
        self.assertFalse(fresh.closed)
        ok, err = fresh.resolve("https://dead.example/y", 0.3)
        self.assertFalse(ok, "a new run re-probes rather than inheriting a "
                             "verdict")
        self.assertEqual(fresh.probe_count, 1)

    def test_a_closed_resolver_never_blocks_a_caller(self):
        self.resolver.close()
        t0 = time.monotonic()
        ok, err = self.resolver.resolve("https://any.example/", 30.0)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_wedged_resolver_costs_the_run_one_bound_not_one_per_plan(self):
        release = threading.Event()

        def hang(host, *a, **kw):
            release.wait(30)
            return []

        self._patch(hang)
        rec = Recorder(default=rss_doc())
        retry = {"attempts": 1, "backoff_sec": [], "retry_on": [],
                 "resolve_timeout_sec": 0.3}
        try:
            t0 = time.monotonic()
            with Patches(rec):
                # Six different hosts through the same wedged resolver.
                for i in range(6):
                    cn.fetch_bytes_retry("https://h%d.example/" % i, 5, 100,
                                         retry, None, self.resolver)
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            if self.resolver._thread is not None:
                self.resolver._thread.join(timeout=3.0)
        # One probe pays the bound; the cooldown answers the rest instantly.
        self.assertEqual(self.resolver.probe_count, 1)
        self.assertLess(elapsed, 3.0,
                        "a wedged resolver must not add a bound per plan")
        self.assertEqual(rec.count("h"), 6)

    def test_cache_is_negative_too(self):
        self._patch(lambda host, *a, **kw: (_ for _ in ()).throw(
            socket.gaierror(-3, "Temporary failure in name resolution")))
        first = self.resolver.resolve("https://dead.example/x", 1.0)
        n_after_first = len(self.calls)
        for _ in range(4):
            self.resolver.resolve("https://dead.example/y", 1.0)
        self.assertEqual(len(self.calls), n_after_first,
                         "a dead resolver must cost one probe per host, not "
                         "one per request")
        self.assertFalse(first[0])



def _sleeper(delay):
    """A task that really takes `delay` seconds (real time.sleep)."""
    def fn():
        time.sleep(delay)
        return []
    return fn


# ---------------------------------------------------------------------------
# Plan inventory (contract G) + a full offline end-to-end run
# ---------------------------------------------------------------------------

class TestUniversityRedundancySweeps(unittest.TestCase):
    def test_one_site_sweep_remains_and_is_np_en_only(self):
        """DECISION: the two `site:iost.tu.edu.np` sweeps are DELETED.

        They were poisoned: 6 of 8 items carried the site's CONSTANT title with
        distinct pubDates, so they outranked the real notices (which had no
        pubDate and sorted to EPOCH) and filled `education_tu` to its cap. The
        notice-page scraper already reads iost.tu.edu.np and produces the real
        titles, so the sweep added no unique signal -- only poison.
        """
        cfg = cn.load_collection_config(REPO_CONFIG)
        plans = cn.build_all_plans({}, cfg, None)
        self.assertEqual(cn.NP_EN_EXTRA_QUERIES,
                         [("site:tu.edu.np notice", "education_tu")])
        names = {p["name"] for p in plans}
        # The poison is gone from the plan inventory, not merely filtered out.
        self.assertNotIn("gnews:np-en:site:iost.tu.edu.np exam", names)
        self.assertNotIn("gnews:np-en:site:iost.tu.edu.np notice", names)
        for query, _cat in cn.NP_EN_EXTRA_QUERIES:
            plan_names = [n for n in names if n == "gnews:np-en:%s" % query]
            self.assertEqual(len(plan_names), 1, query)
        # The surviving sweep keeps its shape and stays np-en only.
        plan = [p for p in plans
                if p["name"] == "gnews:np-en:site:tu.edu.np notice"][0]
        self.assertEqual(plan["kind"], "gnews")
        self.assertEqual(plan["category"], "education_tu")
        self.assertEqual(plan["host"], "news.google.com")
        self.assertIn("hl=en", plan["url"])
        self.assertIn("gl=NP", plan["url"])
        self.assertIn("ceid=NP:en", plan["url"])
        self.assertNotIn(plan["name"].replace("np-en", "np-ne"), names)

    def test_iost_is_never_swept_via_site(self):
        """The specific, measured rule behind the deletion.

        IOST is scraped directly by notices_task and produced the real titles
        correctly, so a `site:` sweep of it can only ADD the site-constant
        title -- 6 of 8 rows, measured. `site:tu.edu.np notice` survives because
        it is the only remaining path to a TU notice while tu.edu.np's origin
        is dark, and no site-constant poisoning has been measured there; its
        risk is now covered by the v2 `empty` signal instead of by deleting the
        redundancy lane.
        """
        scraped = {cn.netloc_of(url) for url, _cat in cn.NOTICE_SITES}
        self.assertIn("iost.tu.edu.np", scraped)
        for query, _cat in cn.NP_EN_EXTRA_QUERIES:
            self.assertNotIn("site:iost.tu.edu.np", query)
        self.assertTrue(any("site:tu.edu.np" in q
                            for q, _c in cn.NP_EN_EXTRA_QUERIES))

    def test_plan_growth_matches_the_shipped_query_lists(self):
        cfg = cn.load_collection_config(REPO_CONFIG)
        plans = cn.build_all_plans({}, cfg, None)
        base = len(cn.NP_QUERIES) * 2 + len(cn.NP_EN_EXTRA_QUERIES)
        self.assertEqual(len(cn.NP_EN_EXTRA_QUERIES), 1)
        self.assertGreaterEqual(len(plans), base + len(cn.NOTICE_SITES) + 1)
        self.assertEqual(len({p["name"] for p in plans}), len(plans))

    def test_zero_yield_sweep_is_empty_but_not_failed(self):
        """v2: a reachable source that produced nothing is `empty`, not `ok`.

        `ok` claimed reachability and nothing else, which is why a poisoned or
        zero-yield sweep looked identical to a healthy one. It is still NOT a
        failure: it stays reachable, so the ok-shield and the drop gate are
        unaffected.
        """
        rec = Recorder(default=b"<?xml version='1.0'?><rss><channel/></rss>")
        cfg = cn.load_collection_config(REPO_CONFIG)
        plans = [p for p in cn.build_all_plans({}, cfg, None)
                 if p["name"] == "gnews:np-en:site:tu.edu.np notice"]
        with Patches(rec):
            ok, failed, _b, raw, _ws, results = cn.collect_parallel(plans, cfg)
        self.assertEqual(results[0]["outcome"], "empty")
        self.assertEqual(results[0]["error"], "")
        self.assertEqual(results[0]["items_found"], 0)
        self.assertEqual(raw, [])
        # Still a successful fetch: it is in sources_ok, NOT sources_failed.
        self.assertEqual(ok, ["gnews:np-en:site:tu.edu.np notice"])
        self.assertEqual(failed, [])
        # ...and still reachable, so nothing it hosts can be deleted.
        self.assertEqual(sh.unreachable_hosts(results), set())


class TestOfflineEndToEnd(unittest.TestCase):
    """main() with fetch_bytes patched: no network, no tracked paths."""

    def test_run_writes_source_results_and_survives_a_tu_outage(self):
        tmp = tempfile.mkdtemp(prefix="coll-e2e-")
        out = os.path.join(tmp, "candidates.json")
        timings = os.path.join(tmp, "timings.json")

        # FULLY OFFLINE, and provably so. Two independent things are pinned
        # here and the point of the second is the one that used to be missing:
        #   1. `Patches` swaps in the fake fetch AND the sleep shim, so the
        #      retry backoff costs no wall-clock time, exactly like every other
        #      test in this file.
        #   2. `served` records every URL the collector actually asked for, and
        #      `fake` REFUSES any host it was not scripted for. A collector
        #      path that stopped routing through `cn.fetch_bytes` would then
        #      raise here instead of quietly reaching api.open-meteo.com over
        #      the real network - which is what this test used to do, and why
        #      it failed in the scheduled CI job. A test that needs the
        #      internet is a defect in a suite that runs on a schedule.
        served = []

        def fake(url, timeout, max_bytes):
            served.append(url)
            if "open-meteo" in url:
                return weather_hourly_body()
            if "tu.edu.np" in url and "news.google.com" not in url:
                raise dns_error()
            if "news.google.com" in url:
                return rss_doc(1)
            raise AssertionError("unpatched network path: %s" % url)

        with Patches(fake):
            buf = io.StringIO()
            stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = cn.main([
                    "--out", out, "--timings", timings,
                    "--interests", os.path.join(REPO_ROOT, "config",
                                                "interests.yml"),
                    "--collection-config", REPO_CONFIG,
                    "--timeout", "1"])
            finally:
                sys.stdout = stdout
            log = buf.getvalue()

        # The weather lane really did run, and really did read the fixture.
        self.assertTrue([u for u in served if "open-meteo" in u], served[:5])
        self.assertEqual(rc, 0)
        with open(out, "r", encoding="utf-8") as f:
            payload = json.load(f)
        results = payload["source_results"]
        # One truth record per plan, and every plan is accounted for exactly
        # once across the two historical lists.
        self.assertEqual(len(results),
                         len(payload["sources_ok"]) + len(payload["sources_failed"]))
        with open(timings, "r", encoding="utf-8") as f:
            by_source = json.load(f)["by_source"]
        self.assertEqual(len(by_source), len(results))
        self.assertGreater(len(results), 40)
        hosts = {r["host"]: r for r in results}
        self.assertEqual(hosts["tu.edu.np"]["outcome"], "failed")
        self.assertEqual(hosts["iost.tu.edu.np"]["outcome"], "failed")
        self.assertEqual(hosts["api.open-meteo.com"]["outcome"], "ok")
        self.assertEqual(hosts["news.google.com"]["outcome"], "ok")
        self.assertEqual(hosts["iost.tu.edu.np"]["attempts"], 2)
        self.assertEqual(sh.classify_reason(hosts["tu.edu.np"]["error"]), "dns")
        # Historical keys are untouched in shape.
        self.assertIsInstance(payload["sources_ok"], list)
        self.assertTrue(all(isinstance(s, str) for s in payload["sources_ok"]))
        self.assertTrue(all(isinstance(s, str) for s in payload["sources_failed"]))
        self.assertIn("source_results", log)

        health = sh.compute_source_health(
            results, cn.load_collection_config(REPO_CONFIG)["critical_hosts"],
            "2026-09-27 06:12")
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["critical"]["hosts_down"],
                         ["iost.tu.edu.np", "tu.edu.np"])

    def test_missing_config_does_not_stop_the_run(self):
        tmp = tempfile.mkdtemp(prefix="coll-e2e2-")
        out = os.path.join(tmp, "candidates.json")
        saved = cn.fetch_bytes
        cn.fetch_bytes = lambda url, timeout, max_bytes: rss_doc(1)
        try:
            buf = io.StringIO()
            stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = cn.main([
                    "--out", out, "--timings", os.path.join(tmp, "t.json"),
                    "--interests", os.path.join(REPO_ROOT, "config",
                                                "interests.yml"),
                    "--collection-config",
                    os.path.join(tmp, "nope.json"), "--timeout", "1"])
            finally:
                sys.stdout = stdout
            log = buf.getvalue()
        finally:
            cn.fetch_bytes = saved
        self.assertEqual(rc, 0)
        self.assertEqual(log.count("WARNING"), 1)
        with open(out, "r", encoding="utf-8") as f:
            self.assertIn("source_results", json.load(f))


if __name__ == "__main__":
    unittest.main()
