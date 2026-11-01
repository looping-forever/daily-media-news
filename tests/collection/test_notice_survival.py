#!/usr/bin/env python3
"""Offline tests for the two notice defects fixed alongside this file.

DEFECT 1 -- THE DATE WAS READ FROM THE ANCHOR ONLY.
TU/IOST notice anchors are title-only; the date lives in a sibling cell of the
same list row. `notices_task` therefore produced `published=None` for every real
notice, `postprocess` sorted them at EPOCH (dead last), and the per-domain cap
deleted the whole kind. Reproduced on the product's own config: 35 kept of 3306
raw, `education_tu=4`, zero notice candidates.

DEFECT 2 -- THE HEALTH RECORD COUNTED PRE-CAP ITEMS.
`items_found` is computed before `postprocess`, so a source that parsed N items
and contributed none still read `ok`.

NO NETWORK: `collect_news.fetch_bytes` is the single injection point and is
patched in every test that needs it, and DNS is forced offline for the whole
module (the collector resolves before it fetches, and tu.edu.np is a dead zone).
Titles are deliberately DISSIMILAR: `title_tokens` keeps words longer than 3
characters and `postprocess` drops a title whose Jaccard overlap with any kept
title is >= 0.7, so two fixtures that differ only by a number are the same
title and would be deduped instead of testing the cap.
"""

import datetime
import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import collect_news as cn  # noqa: E402
import source_health as sh  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)

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


class _Recorder(object):
    """Replays one HTML body for every fetch."""

    def __init__(self, body):
        self.body = body
        self.calls = []

    def __call__(self, url, timeout, max_bytes):
        self.calls.append(url)
        return self.body


class _PatchFetch(object):
    def __init__(self, recorder):
        self.recorder = recorder
        self._fb = cn.fetch_bytes

    def __enter__(self):
        cn.fetch_bytes = self.recorder
        return self

    def __exit__(self, *exc):
        cn.fetch_bytes = self._fb
        return False


def _cand(title, url, domain, hours_ago=None, kind="gnews", plan="",
          source="example.com"):
    """A raw candidate shaped exactly as the collector builds one."""
    dt = None if hours_ago is None else NOW - datetime.timedelta(hours=hours_ago)
    cand = {
        "title": title,
        "url": url,
        "source": source,
        "published": dt.isoformat() if dt else None,
        "snippet": "",
        "domain_category": domain,
        "language": "en",
        "_dt": dt,
        "_kind": kind,
    }
    if plan:
        cand["_plan"] = plan
    return cand


#: Dated competitors, deliberately sharing almost no tokens (see module note).
DATED = [
    ("Nepal cabinet approves annual budget framework",
     "https://news.example.com/a", "nepal", 1, "gnews", ""),
    ("Karnali highway reopens after landslide clearance",
     "https://news.example.com/b", "nepal", 2, "feed", "feed:karnali"),
    ("Valorant champions tour finals venue announced",
     "https://news.example.com/c", "gaming", 3, "gnews", ""),
    ("Kathmandu valley air quality worsens sharply",
     "https://news.example.com/d", "nepal", 4, "gnews", ""),
    ("Counter strike major tournament schedule released",
     "https://news.example.com/e", "gaming", 5, "feed", "feed:cs"),
    ("Rastra bank issues new monetary policy review",
     "https://news.example.com/f", "nepal", 6, "gnews", ""),
    ("Pokhara airport expansion tender awarded",
     "https://news.example.com/g", "nepal", 7, "gnews", ""),
    ("Esports league adds mobile gaming division",
     "https://news.example.com/h", "gaming", 8, "feed", "feed:esports"),
]


def _dated():
    return [_cand(*row) for row in DATED]


# ---------------------------------------------------------------------------
# DEFECT 1a: the date comes from the row, not the anchor text
# ---------------------------------------------------------------------------

#: The live IOST/TU row shape (captured in tests/fixtures/real): the date is a
#: SIBLING cell before the anchor, and the anchor carries the title only.
ROW_HTML = (
    b"<html><body>"
    b"<div class='recent-post-wrapper'>"
    b"  <div class='date'><span class='nep_date'>2026-09-18</span></div>"
    b"  <div class='detail'><a href='/notices/15444'>"
    b"    <h5>M.Sc. Engineering Geology I Semester-2082 Exam Result</h5></a>"
    b"  </div>"
    b"</div>"
    b"<div class='recent-post-wrapper'>"
    b"  <div class='date'><span class='nep_date'>2026-09-23</span></div>"
    b"  <div class='detail'><a href='/notices/15565'>"
    b"    <h5>M.Sc. Microbiology III Semester-2081 Exam Result</h5></a>"
    b"  </div>"
    b"</div>"
    b"</body></html>"
)


class TestNoticeRowDate(unittest.TestCase):
    def test_date_comes_from_the_row_cell_not_the_anchor(self):
        with _PatchFetch(_Recorder(ROW_HTML)):
            out = cn.notices_task("https://iost.tu.edu.np/", "education_tu",
                                  10, None, None)
        by_url = {c["url"]: c for c in out}
        self.assertEqual(len(by_url), 2, [c["url"] for c in out])
        first = by_url["https://iost.tu.edu.np/notices/15444"]
        second = by_url["https://iost.tu.edu.np/notices/15565"]
        self.assertIsNotNone(first["_dt"], "row date cell was not read")
        self.assertEqual(first["_dt"].date(), datetime.date(2026, 9, 18))
        self.assertEqual(second["_dt"].date(), datetime.date(2026, 9, 23))
        # The date reaches the payload as a real ISO string, never invented.
        self.assertTrue(first["published"].startswith("2026-09-18"))

    def test_the_real_iost_list_yields_dated_notices(self):
        path = os.path.join(REPO_ROOT, "tests", "fixtures", "real",
                            "notice_list_iost.tu.edu.np_2026-09-29.html")
        with open(path, "rb") as fh:
            body = fh.read()
        with _PatchFetch(_Recorder(body)):
            out = cn.notices_task("https://iost.tu.edu.np/", "education_tu",
                                  10, None, None)
        dated = [c for c in out if c["_dt"] is not None]
        self.assertTrue(dated,
                        "the real notice list carries row dates; none were read")

    def test_a_row_with_no_date_anywhere_stays_none(self):
        html = b"<html><a href='/notices/9'>Exam notice</a></html>"
        with _PatchFetch(_Recorder(html)):
            out = cn.notices_task("https://x.example/", "nepal", 10, None, None)
        self.assertIsNone(out[0]["_dt"])
        self.assertIsNone(out[0]["published"])

    def test_the_next_row_date_is_never_borrowed(self):
        """The undated row is followed by a dated row. Reading the NEXT row's
        date cell would invent a publication date for the undated notice, so
        the before-anchor window must stop at the previous anchor."""
        html = (
            b"<html><body>"
            b"<div class='recent-post-wrapper'>"
            b"  <div class='detail'><a href='/notices/1'>"
            b"    <h5>Undated admission notice for engineering campus</h5></a>"
            b"  </div>"
            b"</div>"
            b"<div class='recent-post-wrapper'>"
            b"  <div class='date'><span class='nep_date'>2026-09-23</span></div>"
            b"  <div class='detail'><a href='/notices/2'>"
            b"    <h5>Dated transcript distribution notice for graduates</h5></a>"
            b"  </div>"
            b"</div>"
            b"</body></html>"
        )
        with _PatchFetch(_Recorder(html)):
            out = cn.notices_task("https://iost.tu.edu.np/", "education_tu",
                                  10, None, None)
        by_url = {c["url"]: c for c in out}
        self.assertIsNone(by_url["https://iost.tu.edu.np/notices/1"]["_dt"],
                          "the next row's date was borrowed")
        self.assertEqual(
            by_url["https://iost.tu.edu.np/notices/2"]["_dt"].date(),
            datetime.date(2026, 9, 23))


# ---------------------------------------------------------------------------
# DEFECT 1b: notices survive the caps
# ---------------------------------------------------------------------------

NOTICE_TU = ("M.Sc. Engineering Geology first semester exam result published",
             "https://tu.edu.np/notices/15192", "education_tu", None, "notice",
             "notice:tu.edu.np")
NOTICE_IOST = ("B.Sc. Computer Science second semester routine notice",
               "https://iost.tu.edu.np/notices/15697", "education_tu", None,
               "notice", "notice:iost.tu.edu.np")


class TestNoticeSurvival(unittest.TestCase):
    def _education_dated(self):
        """Five dated competitors in the SAME domain as the notices."""
        return [
            _cand("Tribhuvan university publishes semester exam routine",
                  "https://edu.example.com/r1", "education_tu", 1),
            _cand("Ministry announces scholarship deadline extension",
                  "https://edu.example.com/r2", "education_tu", 2),
            _cand("Curriculum centre releases revised syllabus draft",
                  "https://edu.example.com/r3", "education_tu", 3),
            _cand("Higher secondary board declares grade improvement outcome",
                  "https://edu.example.com/r4", "education_tu", 4),
            _cand("Technical institute opens diploma admission intake",
                  "https://edu.example.com/r5", "education_tu", 5),
        ]

    def test_undated_notice_survives_alongside_dated_competitors(self):
        """The reproduced shape: one domain, dated competitors, undated notices.

        Before the fix every notice sorted at EPOCH, so the per-domain cap
        (MAX_PER_DOMAIN=4) filled with the dated rows and the notice kind was
        deleted. Each notice SOURCE must now be in the payload, and the dated
        kind must still be represented.
        """
        raw = self._education_dated()
        raw.append(_cand(*NOTICE_TU))
        raw.append(_cand(*NOTICE_IOST))

        final, per_domain, stats = cn.postprocess(raw, [], 4)
        kept_notices = [c for c in final if c["url"].split("/")[2].endswith(
            "tu.edu.np")]
        kept_dated = [c for c in final if "edu.example.com" in c["url"]]
        self.assertEqual(len(kept_notices), 2,
                         "both notice sources must survive: %r"
                         % [c["url"] for c in final])
        self.assertGreaterEqual(len(kept_dated), 1,
                                "the dated kind must not be starved to zero")
        self.assertEqual(per_domain.get("education_tu"), 4)
        self.assertEqual(stats["kept_by_source"].get("notice:iost.tu.edu.np"), 1)
        self.assertEqual(stats["kept_by_source"].get("notice:tu.edu.np"), 1)

    def test_a_single_notice_source_takes_one_slot_not_the_domain(self):
        raw = self._education_dated()
        raw.append(_cand(*NOTICE_IOST))
        final, per_domain, _stats = cn.postprocess(raw, [], 4)
        notices = [c for c in final if "iost.tu.edu.np" in c["url"]]
        dated = [c for c in final if "edu.example.com" in c["url"]]
        self.assertEqual(len(notices), 1)
        self.assertEqual(len(dated), 3,
                         "one reserved slot, the rest freshest-first")
        self.assertEqual(per_domain.get("education_tu"), 4)

    def test_notice_reservation_leaves_a_slot_for_another_kind(self):
        """The bound that makes the reservation non-starving: with a cap of 4
        and four notice sources, at most 3 slots are reserved, so the other
        kind always keeps at least one."""
        raw = self._education_dated()
        for i, title in enumerate((
                "Entrance examination center notice for engineering campus",
                "Rechecking result application form deadline announced",
                "Academic calendar revision notice for affiliated colleges",
                "Transcript distribution schedule notice for graduates")):
            raw.append(_cand(title, "https://n%d.tu.edu.np/notices/%d"
                             % (i, 15000 + i), "education_tu", None, "notice",
                             "notice:n%d.tu.edu.np" % i))
        final, per_domain, _stats = cn.postprocess(raw, [], 4)
        dated = [c for c in final if "edu.example.com" in c["url"]]
        notices = [c for c in final if "/notices/" in c["url"]]
        self.assertGreaterEqual(len(dated), 1,
                                "the non-notice kind was starved: %r"
                                % [c["url"] for c in final])
        self.assertGreaterEqual(len(notices), 1,
                                "the notice kind was starved: %r"
                                % [c["url"] for c in final])
        self.assertLessEqual(len(notices), 3)
        self.assertEqual(per_domain.get("education_tu"), 4)

    def test_a_notice_only_domain_still_fills_its_cap(self):
        raw = [_cand(title, "https://iost.tu.edu.np/notices/%d" % (15000 + i),
                     "education_tu", None, "notice", "notice:iost.tu.edu.np")
               for i, title in enumerate((
                   "Entrance examination center notice for engineering campus",
                   "Rechecking result application form deadline announced",
                   "Academic calendar revision notice for affiliated colleges",
                   "Transcript distribution schedule notice for graduates",
                   "Thesis defense schedule notice for masters students",
                   "Scholarship recommendation letter request notice"))]
        final, per_domain, _stats = cn.postprocess(raw, [], 4)
        self.assertEqual(len(final), 4)
        self.assertEqual(per_domain.get("education_tu"), 4)

    def test_undated_notice_ranks_below_a_dated_one_and_never_borrows_its_date(self):
        """UNKNOWN_DATE_RANK is a RANK, not a date: a dated notice leads an
        undated one of the same kind, and `published` stays None.

        The real IOST page repeats its newest headlines in an undated marquee
        above the dated list. Ranking the undated copy first let it win the title
        dedup and throw the dated twin's date away, so the rank is the minimum.
        """
        raw = [
            _cand("Older dated notice about examination schedule",
                  "https://iost.tu.edu.np/notices/1", "education_tu", 5,
                  "notice", "notice:iost.tu.edu.np"),
            _cand("Undated newest notice about result publication",
                  "https://iost.tu.edu.np/notices/2", "education_tu", None,
                  "notice", "notice:iost.tu.edu.np"),
        ]
        final, _per_domain, _stats = cn.postprocess(raw, [], 4)
        self.assertEqual(final[0]["url"], "https://iost.tu.edu.np/notices/1")
        self.assertEqual(final[0]["published"], raw[0]["published"])
        self.assertIsNone(final[1]["published"],
                          "a rank must never be written as a publication date")


# ---------------------------------------------------------------------------
# No regression: the non-notice kinds keep today's order and counts
# ---------------------------------------------------------------------------

class TestNonNoticeRankingUnchanged(unittest.TestCase):
    def test_ranking_and_counts_are_unchanged_for_a_fixed_fixture(self):
        """A fixture of the shape today's digest contains (no notices). The fix
        must not reorder it, change its counts, or change its drop stats."""
        final, per_domain, stats = cn.postprocess(_dated(), [], 4)
        self.assertEqual([c["url"] for c in final], [
            "https://news.example.com/a", "https://news.example.com/b",
            "https://news.example.com/c", "https://news.example.com/d",
            "https://news.example.com/e", "https://news.example.com/f",
            "https://news.example.com/h"])
        self.assertEqual(per_domain, {"nepal": 4, "gaming": 3})
        self.assertEqual(stats["dropped_domain_cap"], 1)
        self.assertEqual(stats["dropped_dup_url"], 0)
        self.assertEqual(stats["dropped_dup_title"], 0)


# ---------------------------------------------------------------------------
# DEFECT 2: source_health reports post-cap survival
# ---------------------------------------------------------------------------

class TestSourceHealthSurvivalTruth(unittest.TestCase):
    def _rows(self):
        return [
            {"name": "notice:tu.edu.np", "kind": "notice",
             "category": "education_tu", "host": "tu.edu.np",
             "url": "https://tu.edu.np/", "outcome": "ok", "error": "",
             "attempts": 1, "secs": 1.0, "items_found": 9,
             "candidates_kept": 0},
            {"name": "gnews:nepal", "kind": "gnews", "category": "nepal",
             "host": "news.google.com", "url": "https://news.google.com/rss",
             "outcome": "ok", "error": "", "attempts": 1, "secs": 1.0,
             "items_found": 5, "candidates_kept": 3},
        ]

    def test_a_source_that_contributed_nothing_is_named(self):
        health = sh.compute_source_health(self._rows(), [])
        self.assertIn("notice:tu.edu.np", health["zero_survival_names"])
        self.assertNotIn("gnews:nepal", health["zero_survival_names"])

    def test_survival_is_aggregated_all_and_per_category(self):
        health = sh.compute_source_health(self._rows(), [])
        self.assertEqual(health["all"]["candidates_kept"], 3)
        self.assertEqual(
            health["by_category"]["education_tu"]["candidates_kept"], 0)
        self.assertEqual(health["by_category"]["nepal"]["candidates_kept"], 3)

    def test_a_legacy_row_without_the_field_is_unknown_not_zero(self):
        rows = [{"name": "gnews:nepal", "kind": "gnews", "category": "nepal",
                 "host": "news.google.com", "url": "https://news.google.com/",
                 "outcome": "ok", "error": "", "attempts": 1, "secs": 1.0,
                 "items_found": 5}]
        health = sh.compute_source_health(rows, [])
        self.assertEqual(health["zero_survival_names"], [],
                         "an absent field must never be laundered into zero")
        self.assertEqual(health["all"]["candidates_kept"], 0)

    def test_an_empty_health_payload_carries_the_new_fields(self):
        health = sh.empty_source_health()
        self.assertEqual(health["all"]["candidates_kept"], 0)
        self.assertEqual(health["zero_survival_names"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
