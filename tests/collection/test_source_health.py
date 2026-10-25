#!/usr/bin/env python3
"""tests/collection/test_source_health.py -- offline tests for the source-health
honesty layer.

Covers:
  * scripts/source_health.py::compute_source_health (pure, total function)
  * the frozen survival matrix (missing / corrupt / legacy / malformed input)
  * every branch of the ordered `state` decision table
  * the closed reason-code set
  * the validate_stories.py wiring: source_health in the payload, the
    deterministic research_status override (A4), the anti-fabrication drop gate
    (A5), the honest data cutoff (A6) and the internal-label strip

Run:
    py -m unittest discover -s tests\\collection -t .
    py -m unittest discover -s tests -t tests
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import source_health as sh  # noqa: E402
import validate_stories as vs  # noqa: E402


_SENTINEL = object()


def entry(name="gnews:np-en:Example City", kind="gnews", category="local_city",
          host="news.google.com", url="https://news.google.com/rss?q=Example City",
          outcome="ok", error="", attempts=1, secs=0.5, items_found=_SENTINEL):
    """One source_results row.

    `items_found` is OMITTED by default, deliberately: a v1 record carries no
    such field, and that is exactly the UNKNOWN case the v2 reader must treat
    as unknown rather than as a zero-yield claim. Pass an int to state a real
    yield.
    """
    row = {"name": name, "kind": kind, "category": category, "host": host,
           "url": url, "outcome": outcome, "error": error,
           "attempts": attempts, "secs": secs}
    if items_found is not _SENTINEL:
        row["items_found"] = items_found
    return row


TU_DNS = ("URLError: <urlopen error [Errno -3] Temporary failure in name "
          "resolution>")


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

class TestReasonCodes(unittest.TestCase):
    def test_dns_variants(self):
        for msg in (TU_DNS,
                    "URLError: <urlopen error [Errno -2] Name or service not known>",
                    "URLError: <urlopen error [Errno -3] Temporary failure in name resolution>",
                    "gaierror: [Errno 11001] getaddrinfo failed",
                    "socket.gaierror: nodename nor servname provided, or not known"):
            self.assertEqual(sh.classify_reason(msg), "dns", msg)

    def test_timeout_tls_throttled_http_parse_other(self):
        cases = [
            ("URLError: <urlopen error timed out>", "timeout"),
            ("TimeoutError: The read operation timed out", "timeout"),
            ("SSLError: [SSL: CERTIFICATE_VERIFY_FAILED]", "tls"),
            ("URLError: <urlopen error [SSL: TLSV1_ALERT_PROTOCOL_VERSION]>", "tls"),
            ("HTTPError: HTTP Error 429: Too Many Requests", "throttled"),
            ("HTTPError: HTTP Error 404: Not Found", "http_4xx"),
            ("HTTPError: HTTP Error 503: Service Unavailable", "http_5xx"),
            ("HTTPError: HTTP Error 500: Internal Server Error", "http_5xx"),
            ("HTTPError: HTTP Error 502: Bad Gateway", "http_5xx"),
            ("ParseError: not well-formed (invalid token): line 1, column 0", "parse"),
            ("JSONDecodeError: Expecting value: line 1 column 1 (char 0)", "parse"),
            ("ValueError: open-meteo returned no daily data", "other"),
            ("", "other"),
            (None, "other"),
            (12345, "other"),
        ]
        for msg, want in cases:
            self.assertEqual(sh.classify_reason(msg), want, msg)

    def test_skipped_budget_short_circuits_to_budget(self):
        self.assertEqual(sh.classify_reason("", "skipped_budget"), "budget")
        self.assertEqual(sh.classify_reason(TU_DNS, "skipped_budget"), "budget")

    def test_every_code_is_in_the_closed_set(self):
        for msg in ("boom", TU_DNS, "timed out", "", "429"):
            self.assertIn(sh.classify_reason(msg), sh.REASON_CODES)

    def test_429_is_throttled_not_http_4xx(self):
        self.assertEqual(sh.classify_reason("HTTP Error 429: Too Many Requests"),
                         "throttled")

    def test_403_is_http_4xx_not_throttled(self):
        self.assertEqual(sh.classify_reason("HTTP Error 403: Forbidden"),
                         "http_4xx")


# ---------------------------------------------------------------------------
# The ordered state decision table
# ---------------------------------------------------------------------------

class TestStateDecisionTable(unittest.TestCase):
    crit = ["tu.edu.np", "iost.tu.edu.np", "news.google.com"]

    def test_ok_everything_reachable(self):
        results = [entry(), entry(name="notice:nea.org.np", kind="notice",
                                  category="local_city", host="nea.org.np")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "ok")
        self.assertEqual(h["all"]["ok"], 2)
        self.assertEqual(h["critical"]["failed"], 0)

    def test_degraded_when_a_critical_host_fails(self):
        results = [entry(), entry(name="notice:iost.tu.edu.np", kind="notice",
                                  category="education_tu", host="iost.tu.edu.np",
                                  url="https://iost.tu.edu.np/", outcome="failed",
                                  error=TU_DNS, attempts=2)]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "degraded")
        self.assertEqual(h["critical"]["failed"], 1)
        self.assertEqual(h["critical"]["hosts_down"], ["iost.tu.edu.np"])
        self.assertEqual(h["critical"]["reasons"], ["dns"])
        self.assertEqual(h["failed_names"], ["notice:iost.tu.edu.np"])
        self.assertEqual(h["reachable_hosts"], ["news.google.com"])

    def test_partial_when_only_a_non_critical_source_fails(self):
        results = [entry(), entry(name="notice:nea.org.np", kind="notice",
                                  category="local_city", host="nea.org.np",
                                  outcome="failed", error="timed out")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "partial")
        self.assertEqual(h["critical"]["failed"], 0)
        self.assertEqual(h["all"]["failed"], 1)

    def test_partial_when_a_critical_source_was_skipped(self):
        results = [entry(), entry(name="notice:tu.edu.np", kind="notice",
                                  category="education_tu", host="tu.edu.np",
                                  outcome="skipped_budget")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "partial")
        self.assertEqual(h["critical"]["skipped"], 1)
        self.assertEqual(h["critical"]["failed"], 0)
        self.assertEqual(h["critical"]["reasons"], ["budget"])
        # A skipped host is UNKNOWN, never reported as down.
        self.assertEqual(h["critical"]["hosts_down"], [])

    def test_degraded_wins_over_partial(self):
        results = [entry(name="notice:tu.edu.np", kind="notice",
                         category="education_tu", host="tu.edu.np",
                         outcome="skipped_budget"),
                   entry(name="notice:iost.tu.edu.np", kind="notice",
                         category="education_tu", host="iost.tu.edu.np",
                         outcome="failed", error=TU_DNS),
                   entry(name="notice:nea.org.np", kind="notice",
                         category="local_city", host="nea.org.np",
                         outcome="failed", error="timed out")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "degraded")

    def test_unknown_when_nothing_ok_and_nothing_failed(self):
        results = [entry(outcome="skipped_budget", error="")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "unknown")
        self.assertEqual(h["all"], {"total": 1, "ok": 0, "empty": 0, "failed": 0,
                                    "skipped": 1, "items_found": 0})
        # Data was present, so the check timestamp is real.
        self.assertEqual(h["checked_at_npt"], "2026-09-27 06:12")

    def test_unknown_when_source_results_is_empty(self):
        h = sh.compute_source_health([], self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "unknown")
        self.assertEqual(h["all"]["total"], 0)
        self.assertEqual(h["checked_at_npt"], "")


# ---------------------------------------------------------------------------
# v2: the `empty` yield signal
#
# The bug class this closes: `ok` is defined as REACHABLE, never "produced
# items", so an HTTP-200 HTML error page, an Atom feed, RSS 1.0/RDF and a
# valid-but-empty channel ALL reported `ok` with no banner and no items-count
# signal anywhere. "I checked and there was nothing" was indistinguishable from
# "I checked and got garbage back".
# ---------------------------------------------------------------------------

class TestEmptyIsAdditiveAndShielded(unittest.TestCase):
    crit = ["tu.edu.np", "iost.tu.edu.np", "news.google.com"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-empty-")

    def _empty(self, **kw):
        return entry(outcome="empty", items_found=0, **kw)

    def test_reachable_with_zero_items_is_empty_not_ok(self):
        results = [self._empty()]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["all"]["empty"], 1)
        # ...but it is STILL reachable, so all.ok keeps its reachability meaning.
        self.assertEqual(h["all"]["ok"], 1)
        self.assertEqual(h["reachable_hosts"], ["news.google.com"])
        self.assertEqual(sh.reachable_hosts_set(results),
                         {"news.google.com"})
        self.assertEqual(sh.unreachable_hosts(results), set())

    def test_empty_counts_as_ok_in_every_existing_counter(self):
        """Every pre-v2 assertion on `ok: N` must survive unchanged."""
        results = [entry(items_found=4),
                   self._empty(name="gnews:np-en:q2"),
                   entry(name="notice:nea.org.np", kind="notice",
                         category="local_city", host="nea.org.np",
                         outcome="failed", error="timed out"),
                   entry(name="notice:college.example.edu", kind="notice",
                         category="college_local", host="college.example.edu",
                         outcome="skipped_budget")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        # v1 numbers, unchanged: 2 ok (one of them empty), 1 failed, 1 skipped.
        self.assertEqual(h["all"], {"total": 4, "ok": 2, "empty": 1,
                                    "failed": 1, "skipped": 1,
                                    "items_found": 4})
        self.assertEqual(h["critical"], {
            "total": 2, "ok": 2, "empty": 1, "failed": 0, "skipped": 0,
            "hosts_down": [], "empty_hosts": [], "reasons": []})
        # `empty` is additive, so it is NOT in failed_names: nothing failed.
        # `empty` is additive, so it is NOT in failed_names: nothing failed.
        # (skipped_budget was always listed, and still is.)
        self.assertEqual(h["failed_names"],
                         ["notice:college.example.edu", "notice:nea.org.np"])
        self.assertEqual(h["empty_names"], ["gnews:np-en:q2"])
        # local_city holds the productive gnews row, the EMPTY gnews row and
        # the failed notice task: `empty` buckets into `ok`, so ok is 2.
        self.assertEqual(h["by_category"]["local_city"]["ok"], 2)
        self.assertEqual(h["by_category"]["local_city"]["empty"], 1)
        self.assertEqual(h["by_category"]["local_city"]["failed"], 1)

    def test_all_items_found_sums_only_known_rows(self):
        results = [entry(items_found=3), entry(name="b", items_found=0),
                   entry(name="c", items_found=7),
                   # items_found UNKNOWN: contributes nothing, and is not
                   # laundered into 0.
                   entry(name="d", outcome="failed", error="timed out"),
                   {"name": "e", "host": "h", "outcome": "ok"}]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["all"]["items_found"], 10)
        self.assertEqual(h["all"]["total"], 5)
        # No row carried the field at all -> render 0, never a crash.
        only_unknown = sh.compute_source_health(
            [{"name": "x", "host": "h", "outcome": "ok"}], self.crit, "t")
        self.assertEqual(only_unknown["all"]["items_found"], 0)

    def test_missing_items_found_is_unknown_never_empty(self):
        """A v1 row with no items_found must not manufacture a zero-yield claim."""
        for bad in ({}, {"items_found": None}, {"items_found": "many"},
                    {"items_found": -3}, {"items_found": True},
                    {"items_found": 2.5}):
            row = entry(items_found=None)
            row.update(bad)
            row.pop("items_found", None)
            row.update(bad)
            h = sh.compute_source_health([row], self.crit, "t")
            self.assertEqual(h["all"]["empty"], 0, bad)
            self.assertEqual(h["state"], "ok", bad)
            self.assertEqual(h["critical"]["empty_hosts"], [], bad)
        # A non-negative int string is accepted (JSON round-trip tolerance).
        h = sh.compute_source_health(
            [entry(items_found="0")], self.crit, "t")
        self.assertEqual(h["all"]["empty"], 1)

    def test_ok_shield_protects_stories_when_empty_plus_failed(self):
        """THE DANGEROUS REGRESSION. A host with one empty-but-reachable task
        and one failed task must NOT be treated as dark, or the
        anti-fabrication gate deletes its real stories."""
        results = [entry(name="notice:tu.edu.np:exam", kind="notice",
                         category="education_tu", host="tu.edu.np",
                         outcome="failed", error=TU_DNS),
                   self._empty(name="notice:tu.edu.np:notice", kind="notice",
                               category="education_tu", host="tu.edu.np",
                               url="https://tu.edu.np/x")]
        self.assertEqual(sh.unreachable_hosts(results), set())
        self.assertEqual(sh.reachable_hosts_set(results), {"tu.edu.np"})
        self.assertEqual(sh.failed_hosts(results), {"tu.edu.np"})
        h = sh.compute_source_health(results, self.crit, "t")
        self.assertEqual(h["critical"]["hosts_down"], [])
        # The host IS actionable-empty: one empty row, zero productive rows.
        self.assertEqual(h["critical"]["empty_hosts"], ["tu.edu.np"])
        # Row 3 of the frozen table fires on critical.empty_hosts, ahead of
        # row 4 (partial). The failed task is still fully visible in
        # critical.failed, critical.reasons and failed_names -- `empty` does
        # not hide it -- and the banner is the honest "answered, nothing
        # usable" rather than a quiet ok.
        self.assertEqual(h["state"], "empty")
        self.assertEqual(h["critical"]["failed"], 1)
        self.assertEqual(h["critical"]["reasons"], ["dns"])
        self.assertEqual(h["failed_names"], ["notice:tu.edu.np:exam"])
        # The end-to-end proof through the REAL validate_stories gate: the
        # story survives. This is the regression the whole additive-`empty`
        # design exists to prevent.
        stories = [valid_story(source_url="https://tu.edu.np/notice/1")]
        rc, out, payload = Harness(self.tmp, stories, candidates=results).run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(payload["stories"]), 1, payload["filter_log"])
        self.assertNotIn("source_unreachable:tu.edu.np",
                         [e["reason"] for e in payload["filter_log"]])

    def test_one_zero_yield_site_sweep_does_not_flip_the_state(self):
        """`news.google.com` is critical and hosts 20+ tasks, so a single
        zero-yield `site:` sweep must not make the run `empty`."""
        results = [entry(name="gnews:np-en:site:tu.edu.np notice",
                         category="education_tu", items_found=5)
                   for _ in range(19)]
        results.append(self._empty(name="gnews:np-en:site:tu.edu.np notice2",
                                   category="education_tu"))
        h = sh.compute_source_health(results, self.crit, "t")
        self.assertEqual(h["all"]["empty"], 1)
        self.assertNotEqual(h["state"], "empty")
        # The empty host is shielded by the 19 productive siblings.
        self.assertEqual(h["critical"]["empty_hosts"], [])
        self.assertEqual(h["by_category"]["education_tu"]["empty_hosts"], [])
        self.assertEqual(h["state"], "ok")

    def test_by_category_empty_hosts_needs_every_reachable_row_empty(self):
        results = [self._empty(name="notice:nea.org.np:a", kind="notice",
                               category="local_city", host="nea.org.np",
                               url="https://nea.org.np/a"),
                   entry(name="notice:nea.org.np:b", kind="notice",
                         category="local_city", host="nea.org.np",
                         url="https://nea.org.np/b", items_found=2)]
        h = sh.compute_source_health(results, self.crit, "t")
        cat = h["by_category"]["local_city"]
        self.assertEqual(cat["empty"], 1)          # raw per-task count
        self.assertEqual(cat["empty_hosts"], [])   # shielded: one host, productive
        # Now make EVERY reachable row for that host empty.
        results[1] = self._empty(name="notice:nea.org.np:b", kind="notice",
                                 category="local_city", host="nea.org.np",
                                 url="https://nea.org.np/b")
        h2 = sh.compute_source_health(results, self.crit, "t")
        self.assertEqual(h2["by_category"]["local_city"]["empty"], 2)
        self.assertEqual(h2["by_category"]["local_city"]["empty_hosts"],
                         ["nea.org.np"])

    def test_min_items_per_source_is_honoured(self):
        results = [entry(name="a", items_found=1),
                   entry(name="b", items_found=2),
                   entry(name="c", items_found=0)]
        # Default 1: only the literal zero is empty.
        h = sh.compute_source_health(results, self.crit, "t")
        self.assertEqual(h["all"]["empty"], 1)
        # 3: a-and-b are both below the floor, so all three rows are empty.
        h3 = sh.compute_source_health(results, self.crit, "t",
                                      min_items_per_source=3)
        self.assertEqual(h3["all"]["empty"], 3)
        self.assertEqual(h3["all"]["ok"], 3)
        # A 0/negative/junk floor falls back to 1 -- it must never make every
        # productive source empty.
        for bad in (0, -5, None, "banana", 1.0):
            hb = sh.compute_source_health(results, self.crit, "t",
                                          min_items_per_source=bad)
            self.assertEqual(hb["all"]["empty"], 1, bad)

    def test_empty_never_raises_on_hostile_rows(self):
        hostile = [{"outcome": "empty"}, {"outcome": "empty", "host": 5,
                                          "items_found": "x"},
                   {"outcome": "empty", "host": "a", "category": None},
                   {"outcome": "ok", "host": "a", "items_found": []}]
        h = sh.compute_source_health(hostile, ["a"], "t")
        self.assertIn(h["state"], ("unknown", "partial", "degraded", "empty", "ok"))
        self.assertEqual(sh.unreachable_hosts(hostile), set())


# ---------------------------------------------------------------------------
# The v2 state decision table, all five rows
# ---------------------------------------------------------------------------

class TestStateTableV2(unittest.TestCase):
    crit = ["tu.edu.np", "iost.tu.edu.np", "news.google.com"]

    def test_row1_unknown(self):
        h = sh.compute_source_health(
            [entry(outcome="skipped_budget")], self.crit, "t")
        self.assertEqual(h["state"], "unknown")

    def test_row2_degraded(self):
        h = sh.compute_source_health(
            [entry(kind="notice", host="tu.edu.np", outcome="failed",
                   error=TU_DNS)], self.crit, "t")
        self.assertEqual(h["state"], "degraded")
        self.assertEqual(h["critical"]["hosts_down"], ["tu.edu.np"])

    def test_row3_empty_via_critical_empty_hosts(self):
        h = sh.compute_source_health(
            [entry(kind="notice", host="iost.tu.edu.np", outcome="empty",
                   items_found=0)], self.crit, "t")
        self.assertEqual(h["state"], "empty")
        self.assertEqual(h["critical"]["empty_hosts"], ["iost.tu.edu.np"])
        self.assertEqual(h["critical"]["hosts_down"], [])

    def test_row3_second_clause_is_the_missing_critical_hosts_safety_net(self):
        """With NO critical_hosts configured, "every reachable source produced
        zero items" must still be `empty`, never a quiet `ok`."""
        results = [entry(name="a", host="x.example", outcome="empty",
                         items_found=0),
                   entry(name="b", host="y.example", outcome="empty",
                         items_found=0)]
        h = sh.compute_source_health(results, [], "t")
        self.assertEqual(h["critical"]["total"], 0)
        self.assertEqual(h["all"]["empty"], 2)
        self.assertEqual(h["all"]["ok"], 2)
        self.assertEqual(h["state"], "empty")

    def test_row4_partial(self):
        h = sh.compute_source_health(
            [entry(items_found=3),
             entry(name="n", kind="notice", host="nea.org.np", outcome="failed",
                   error="timed out")], self.crit, "t")
        self.assertEqual(h["state"], "partial")

    def test_row5_ok(self):
        h = sh.compute_source_health(
            [entry(items_found=3), entry(name="b", items_found=1)], self.crit, "t")
        self.assertEqual(h["state"], "ok")

    def test_empty_beats_partial(self):
        h = sh.compute_source_health(
            [entry(kind="notice", host="iost.tu.edu.np", outcome="empty",
                   items_found=0),
             entry(name="n", kind="notice", host="nea.org.np", outcome="failed",
                   error="timed out")], self.crit, "t")
        self.assertEqual(h["state"], "empty")

    def test_degraded_beats_empty(self):
        h = sh.compute_source_health(
            [entry(kind="notice", host="iost.tu.edu.np", outcome="empty",
                   items_found=0),
             entry(name="t", kind="notice", host="tu.edu.np", outcome="failed",
                   error=TU_DNS)], self.crit, "t")
        self.assertEqual(h["state"], "degraded")


# ---------------------------------------------------------------------------
# Aggregate shape
# ---------------------------------------------------------------------------

class TestAggregateShape(unittest.TestCase):
    crit = ["tu.edu.np", "iost.tu.edu.np", "news.google.com"]

    def test_full_shape_matches_the_contract_example(self):
        results = [
            entry(name="notice:bank.example.com", kind="notice",
                  category="bank_local", host="www.bank.example.com",
                  url="https://www.bank.example.com/"),
            entry(name="notice:college.example.edu", kind="notice",
                  category="college_local", host="college.example.edu",
                  url="https://college.example.edu/"),
            entry(name="notice:nea.org.np", kind="notice",
                  category="local_city", host="nea.org.np",
                  url="https://nea.org.np/"),
            entry(name="weather:open-meteo", kind="weather", category="",
                  host="api.open-meteo.com", url="https://api.open-meteo.com/v1"),
            entry(name="notice:tu.edu.np", kind="notice",
                  category="education_tu", host="tu.edu.np",
                  url="https://tu.edu.np/", outcome="failed", error=TU_DNS,
                  attempts=2, secs=0.31),
            entry(name="notice:iost.tu.edu.np", kind="notice",
                  category="education_tu", host="iost.tu.edu.np",
                  url="https://iost.tu.edu.np/", outcome="failed", error=TU_DNS,
                  attempts=2, secs=0.31),
        ]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(sorted(h), [
            "all", "by_category", "checked_at_npt", "critical", "empty_names",
            "failed_names", "reachable_hosts", "schema_version", "state"])
        self.assertEqual(h["schema_version"], 2)
        self.assertEqual(h["state"], "degraded")
        self.assertEqual(h["checked_at_npt"], "2026-09-27 06:12")
        self.assertEqual(h["critical"], {
            "total": 2, "ok": 0, "empty": 0, "failed": 2, "skipped": 0,
            "hosts_down": ["iost.tu.edu.np", "tu.edu.np"], "empty_hosts": [],
            "reasons": ["dns"]})
        # `ok` still counts only REACHABLE rows and `empty` buckets into it, so
        # the v1 numbers are unchanged; `items_found` is 0 because no row in
        # this fixture carried the field (UNKNOWN, never manufactured).
        self.assertEqual(h["all"], {"total": 6, "ok": 4, "empty": 0, "failed": 2,
                                    "skipped": 0, "items_found": 0})
        self.assertEqual(h["failed_names"],
                         ["notice:iost.tu.edu.np", "notice:tu.edu.np"])
        self.assertEqual(h["empty_names"], [])
        self.assertEqual(h["reachable_hosts"], [
        "api.open-meteo.com", "college.example.edu", "nea.org.np",
        "www.bank.example.com"])
        self.assertEqual(h["by_category"]["education_tu"], {
            "total": 2, "ok": 0, "empty": 0, "failed": 2, "skipped": 0,
            "items_found": 0, "empty_hosts": [],
            "urls": ["https://iost.tu.edu.np/", "https://tu.edu.np/"]})
        self.assertEqual(sorted(h["by_category"]), [
            "bank_local", "college_local", "education_tu", "local_city"])
        # The weather entry has no category, so it never invents a "" bucket.
        self.assertNotIn("", h["by_category"])

    def test_uncapped_failed_names(self):
        results = [entry(name="gnews:np-en:q%d" % i, host="news.google.com",
                         outcome="failed", error="timed out") for i in range(30)]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(len(h["failed_names"]), 30)
        self.assertEqual(h["critical"]["failed"], 30)

    def test_by_category_urls_capped_at_three_and_sorted(self):
        results = [
            entry(name="notice:c%d" % i, kind="notice", category="education_tu",
                  host="tu.edu.np", url="https://tu.edu.np/%d" % i,
                  outcome="failed", error=TU_DNS) for i in range(5)
        ]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["by_category"]["education_tu"]["urls"],
                         ["https://tu.edu.np/0", "https://tu.edu.np/1",
                          "https://tu.edu.np/2"])

    def test_by_category_url_fallback_when_no_notice_page(self):
        results = [entry(category="tech_career", host="news.google.com")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["by_category"]["tech_career"]["urls"],
                         ["https://news.google.com/"])

    def test_ok_means_reachable_not_producing_items(self):
        # A v1 record with NO items_found is UNKNOWN yield, and the honest
        # state is still `ok` -- absence of the field must never manufacture an
        # `empty`. This is the "reachable != useful" gap the v2 field closes,
        # and it must not close by inventing zeros for records that cannot
        # answer the question.
        results = [entry(attempts=1, secs=0.02)]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["state"], "ok")
        self.assertEqual(h["all"]["failed"], 0)
        self.assertEqual(h["all"]["empty"], 0)
        self.assertEqual(h["all"]["items_found"], 0)

    def test_critical_ignores_empty_host(self):
        results = [entry(host="", outcome="failed", error="timed out")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["critical"]["total"], 0)
        self.assertEqual(h["all"]["failed"], 1)
        self.assertEqual(h["state"], "partial")

    def test_mixed_host_is_not_reported_as_down(self):
        # Ok-shield: hosts_down means "entirely dark", the same notion the drop
        # gate uses. The failed task is still counted and named.
        results = [entry(name="notice:tu.edu.np:exam", host="tu.edu.np",
                         kind="notice", category="education_tu",
                         outcome="failed", error=TU_DNS),
                   entry(name="notice:tu.edu.np:notice", host="tu.edu.np",
                         kind="notice", category="education_tu",
                         url="https://tu.edu.np/x")]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["critical"]["hosts_down"], [])
        self.assertEqual(h["critical"]["failed"], 1)
        self.assertEqual(h["critical"]["reasons"], ["dns"])
        self.assertEqual(h["failed_names"], ["notice:tu.edu.np:exam"])
        # v2 row 2 of the state table triggers on `critical.hosts_down` (a
        # critical host that is entirely DARK), not on any critical failure.
        # The host is not dark -- its sibling task answered -- so the run is
        # `partial`, which still tells the reader "some sources failed". The
        # individual failure stays in critical.failed/reasons/failed_names.
        self.assertEqual(h["state"], "partial")

    def test_critical_ignores_missing_host_key(self):
        results = [{"name": "x", "outcome": "failed", "error": "timed out"}]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["critical"]["total"], 0)
        self.assertEqual(h["state"], "partial")

    def test_www_host_matches_critical_host(self):
        results = [entry(host="www.iost.tu.edu.np", kind="notice",
                         category="education_tu", outcome="failed",
                         error=TU_DNS)]
        h = sh.compute_source_health(results, self.crit, "2026-09-27 06:12")
        self.assertEqual(h["critical"]["total"], 1)
        self.assertEqual(h["state"], "degraded")
        # The emitted host is untouched.
        self.assertEqual(h["critical"]["hosts_down"], ["www.iost.tu.edu.np"])

    def test_empty_critical_hosts_means_no_critical_bucket(self):
        results = [entry(), entry(name="notice:tu.edu.np", kind="notice",
                                  category="education_tu", host="tu.edu.np",
                                  outcome="failed", error=TU_DNS)]
        for crit in ([], None, "", "tu.edu.np", {}):
            h = sh.compute_source_health(results, crit, "2026-09-27 06:12")
            self.assertEqual(h["critical"]["total"], 0, crit)
            self.assertEqual(h["state"], "partial", crit)
            self.assertEqual(h["all"]["failed"], 1, crit)


# ---------------------------------------------------------------------------
# Survival matrix: never raise, always degrade to unknown
# ---------------------------------------------------------------------------

class TestNeverRaises(unittest.TestCase):
    def test_hostile_inputs(self):
        hostile = [
            None, [], {}, "nope", 42,
            [None, 1, "x", [], {}],
            [{"outcome": None}, {"outcome": 7}, {"outcome": "weird"}],
            [{"outcome": "failed", "host": None, "name": None, "url": 5,
              "category": [], "error": {"a": 1}}],
            [{"outcome": "ok", "host": "h", "secs": "NaN"}],
        ]
        for value in hostile:
            h = sh.compute_source_health(value, ["tu.edu.np"], "2026-09-27 06:12")
            self.assertIn(h["state"],
                          ("unknown", "partial", "degraded", "empty", "ok"), value)
            self.assertEqual(h["schema_version"], 2)
            for bucket in (h["all"], h["critical"]):
                # `empty` buckets into `ok`, so the v1 invariant survives: the
                # three original counters still sum to total.
                self.assertEqual(bucket["total"],
                                 bucket["ok"] + bucket["failed"] + bucket["skipped"])
                self.assertGreaterEqual(bucket["empty"], 0)
                self.assertLessEqual(bucket["empty"], bucket["ok"])
            self.assertIsInstance(h["all"]["items_found"], int)
            self.assertEqual(h["critical"]["empty_hosts"], [])

    def test_v1_payload_without_the_new_fields_reads_without_raising(self):
        """A v1 source_health blob is DATA for a v2 reader.

        Every new field defaults to 0/[] exactly as `_row_from_entry` already
        does for attempts/secs, so an old state file can never explode a
        render or a diff.
        """
        v1 = {
            "schema_version": 1, "state": "partial", "checked_at_npt": "x",
            "critical": {"total": 2, "ok": 1, "failed": 1, "skipped": 0,
                         "hosts_down": ["tu.edu.np"], "reasons": ["dns"]},
            "all": {"total": 4, "ok": 3, "failed": 1, "skipped": 0},
            "by_category": {"education_tu": {"total": 2, "ok": 1, "failed": 1,
                                             "skipped": 0, "urls": ["u"]}},
            "failed_names": ["n"], "reachable_hosts": ["a"],
        }
        for cat in v1["by_category"].values():
            self.assertNotIn("empty_hosts", cat)
        # The row-level reader: a v1 row has no items_found -> UNKNOWN.
        row = sh._row_from_entry(v1["all"])
        self.assertIsNone(row["items_found"])
        self.assertEqual(row["ok"], False)
        # And a whole v1-shaped source_results list still computes.
        legacy_rows = [{"name": "n", "host": "tu.edu.np", "outcome": "ok",
                        "category": "education_tu", "kind": "gnews"}]
        h = sh.compute_source_health(legacy_rows, ["tu.edu.np"], "x")
        self.assertEqual(h["all"]["empty"], 0)
        self.assertEqual(h["all"]["items_found"], 0)
        self.assertEqual(h["critical"]["empty_hosts"], [])

    def test_malformed_entry_becomes_skipped_and_flagged(self):
        results = [entry(), {"name": "notice:tu.edu.np", "host": "tu.edu.np"}]
        h = sh.compute_source_health(results, ["tu.edu.np"], "2026-09-27 06:12")
        self.assertEqual(h["all"], {"total": 2, "ok": 1, "empty": 0,
                                    "failed": 0, "skipped": 1,
                                    "items_found": 0})
        self.assertEqual(h["critical"]["reasons"], ["malformed"])
        self.assertEqual(h["critical"]["hosts_down"], [])
        self.assertEqual(h["state"], "partial")
        self.assertEqual(h["failed_names"], ["notice:tu.edu.np"])

    def test_non_dict_entry_is_malformed_not_failed(self):
        h = sh.compute_source_health(["oops"], [], "2026-09-27 06:12")
        self.assertEqual(h["all"]["failed"], 0)
        self.assertEqual(h["all"]["skipped"], 1)
        self.assertEqual(h["state"], "unknown")

    def test_reasons_are_closed_codes_only(self):
        results = [
            entry(name="a", outcome="failed", error=TU_DNS),
            entry(name="b", outcome="failed", error="timed out"),
            entry(name="c", outcome="skipped_budget"),
            {"name": "d", "host": "iost.tu.edu.np"},
        ]
        h = sh.compute_source_health(results,
                                     ["news.google.com", "iost.tu.edu.np"],
                                     "2026-09-27 06:12")
        for reason in h["critical"]["reasons"]:
            self.assertIn(reason, sh.REASON_CODES)
        self.assertEqual(h["critical"]["reasons"],
                         ["budget", "dns", "malformed", "timeout"])
        # The malformed record sits on iost.tu.edu.np and is flagged, but it is
        # never reported as a host being down.
        self.assertEqual(h["critical"]["hosts_down"], ["news.google.com"])


# ---------------------------------------------------------------------------
# validate_stories.py wiring (loaders + payload + honesty fixes)
# ---------------------------------------------------------------------------

def valid_story(title="TU exam schedule published", source_url="https://iost.tu.edu.np/",
                category="education_tu", priority=9, label=None):
    story = {
        "title": title,
        "summary": "Tribhuvan University published the exam schedule.",
        "why_it_matters": "Exam notices must not be missed.",
        "source_name": "IOST",
        "spoiler_reason": "none",
        "relevance_reason": "exam notice",
        "language": "en",
        "priority": priority,
        "category": category,
        "source_url": source_url,
        "reliability": "tier1",
        "spoiler_level": "none",
    }
    if label is not None:
        story["label"] = label
    return story


class TestLoaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-")

    def _write(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_missing_candidates_file_warns(self):
        results, warn = vs.load_source_results(
            os.path.join(self.tmp, "nope.json"))
        self.assertIsNone(results)
        self.assertTrue(warn.startswith("candidates.json unreadable ("), warn)
        self.assertTrue(warn.endswith(" - source_health unknown"), warn)

    def test_corrupt_candidates_file_warns(self):
        path = self._write("bad.json", "{not json")
        results, warn = vs.load_source_results(path)
        self.assertIsNone(results)
        self.assertIn("candidates.json unreadable (", warn)
        self.assertIn("- source_health unknown", warn)

    def test_legacy_candidates_file_is_quiet(self):
        path = self._write("legacy.json", json.dumps(
            {"collected_at": "2026-09-27T00:00:00+00:00", "candidates": []}))
        results, warn = vs.load_source_results(path)
        self.assertIsNone(results)
        self.assertIsNone(warn)

    def test_source_results_not_a_list_is_treated_as_missing(self):
        path = self._write("weird.json", json.dumps({"source_results": {}}))
        results, warn = vs.load_source_results(path)
        self.assertIsNone(results)
        self.assertIsNone(warn)

    def test_source_results_list_is_returned(self):
        payload = [{"name": "gnews:np-en:Example City", "kind": "gnews",
                    "category": "local_city", "host": "news.google.com",
                    "url": "https://news.google.com/rss?q=Example City",
                    "outcome": "ok", "error": "", "attempts": 1, "secs": 0.2}]
        path = self._write("good.json", json.dumps({"source_results": payload}))
        results, warn = vs.load_source_results(path)
        self.assertEqual(results, payload)
        self.assertIsNone(warn)

    def test_critical_hosts_missing_config_warns_once(self):
        hosts, warn = vs.load_critical_hosts(
            os.path.join(self.tmp, "no-such-config"))
        self.assertEqual(hosts, [])
        self.assertIn("critical_hosts", warn)

    def test_critical_hosts_empty_list_warns(self):
        cfg = self._write("collection.json", json.dumps(
            {"schema_version": 1, "critical_hosts": []}))
        hosts, warn = vs.load_critical_hosts(os.path.dirname(cfg))
        self.assertEqual(hosts, [])
        self.assertIsNotNone(warn)

    def test_critical_hosts_read(self):
        cfg = self._write("collection.json", json.dumps(
            {"schema_version": 1,
             "critical_hosts": ["tu.edu.np", "iost.tu.edu.np"]}))
        hosts, warn = vs.load_critical_hosts(os.path.dirname(cfg))
        self.assertEqual(hosts, ["tu.edu.np", "iost.tu.edu.np"])
        self.assertIsNone(warn)

    def test_critical_hosts_ignores_junk_entries(self):
        cfg = self._write("collection.json", json.dumps(
            {"critical_hosts": ["tu.edu.np", 5, None, "  ", "iost.tu.edu.np"]}))
        hosts, warn = vs.load_critical_hosts(os.path.dirname(cfg))
        self.assertEqual(hosts, ["tu.edu.np", "iost.tu.edu.np"])


class Harness(object):
    """Runs validate_stories.main over temp files and returns (rc, out, payload)."""

    def __init__(self, tmp, stories, candidates=None, critical_hosts=None,
                 collected_at="2026-09-27T00:00:00+00:00",
                 research_status="full", corrupt_candidates=False,
                 legacy_candidates=False):
        self.tmp = tmp
        self.candidates_path = os.path.join(tmp, "candidates.json")
        if corrupt_candidates:
            with open(self.candidates_path, "w", encoding="utf-8") as f:
                f.write("{ truncated")
        elif candidates is not None:
            with open(self.candidates_path, "w", encoding="utf-8") as f:
                json.dump({"collected_at": collected_at,
                           "candidates": [], "sources_ok": [],
                           "sources_failed": [],
                           "source_results": candidates}, f)
        elif legacy_candidates:
            with open(self.candidates_path, "w", encoding="utf-8") as f:
                json.dump({"collected_at": collected_at, "candidates": [],
                           "sources_ok": [], "sources_failed": []}, f)
        else:
            with open(self.candidates_path, "w", encoding="utf-8") as f:
                json.dump({"collected_at": collected_at, "candidates": [],
                           "sources_ok": [], "sources_failed": []}, f)

        cfg_dir = os.path.join(tmp, "config")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "collection.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"schema_version": 1,
                       "critical_hosts": (critical_hosts
                                          if critical_hosts is not None
                                          else ["tu.edu.np", "iost.tu.edu.np",
                                                "news.google.com"])}, f)
        with open(os.path.join(cfg_dir, "interests.yml"), "w",
                  encoding="utf-8") as f:
            f.write("excluded_keywords: []\n")

        self.in_path = os.path.join(tmp, "stories.json")
        with open(self.in_path, "w", encoding="utf-8") as f:
            json.dump({"digest_time": "morning", "date": "2026-09-27",
                       "research_status": research_status,
                       "stories": stories}, f)
        self.out_path = os.path.join(tmp, "validated.json")

    def run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vs.main([
                "--in", self.in_path, "--out", self.out_path,
                "--history", os.path.join(self.tmp, "history.json"),
                "--state", os.path.join(self.tmp, "active.json"),
                "--config-dir", os.path.join(self.tmp, "config"),
                "--candidates", self.candidates_path])
        payload = None
        if os.path.isfile(self.out_path):
            with open(self.out_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        return rc, buf.getvalue(), payload


class TestValidateStoriesWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-e2e-")

    def test_source_health_reaches_the_payload(self):
        results = [entry(name="notice:tu.edu.np", kind="notice",
                         category="education_tu", host="tu.edu.np",
                         url="https://tu.edu.np/", outcome="failed",
                         error=TU_DNS, attempts=2, secs=0.31)]
        h = Harness(self.tmp, [valid_story()], candidates=results).run()
        rc, _out, payload = h
        self.assertEqual(rc, 0)
        health = payload["source_health"]
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["critical"]["hosts_down"], ["tu.edu.np"])
        self.assertEqual(health["critical"]["reasons"], ["dns"])
        self.assertTrue(health["checked_at_npt"])

    def test_unreadable_candidates_yields_unknown_and_warns(self):
        h = Harness(self.tmp, [valid_story()], corrupt_candidates=True).run()
        rc, out, payload = h
        self.assertEqual(rc, 0)
        self.assertEqual(payload["source_health"]["state"], "unknown")
        self.assertEqual(payload["source_health"]["all"]["total"], 0)
        self.assertEqual(payload["source_health"]["checked_at_npt"], "")
        self.assertIn("WARNING: candidates.json unreadable (", out)
        self.assertIn("- source_health unknown", out)
        # Exactly one such warning.
        self.assertEqual(out.count("source_health unknown"), 1)

    def test_legacy_candidates_yields_unknown_without_warning(self):
        # SCOPED, NOT DELETED. This assertion guards one specific class of bug:
        # a candidates file whose shape predates `source_results` being treated
        # as fine instead of as UNKNOWN. The original form was a blanket
        # `assertNotIn("WARNING", out)`, which is not what it was guarding - it
        # fails for a warning from ANY subsystem, including the `for_you` screen
        # (a different config file entirely: the Harness points --config-dir at
        # an empty temp dir, so config/profile.yml has no for_you: block and the
        # screen correctly fails closed). Coupling the source-health contract to
        # an unrelated subsystem's warning is how this test went red for a
        # reason it does not describe.
        #
        # Decision: keep the assertion, scope it to the source-health warning,
        # and additionally PIN the for_you warning so this test states the real
        # behaviour instead of quietly ignoring it.
        rc, out, payload = Harness(self.tmp, [valid_story()],
                                  legacy_candidates=True).run()
        self.assertEqual(payload["source_health"]["state"], "unknown")
        # The class of bug this test exists for: a legacy candidates file must
        # NOT produce the "candidates.json unreadable" warning. load_source_results
        # returns a legacy file QUIET, and this is what proves it still does.
        self.assertNotIn("WARNING: candidates.json", out)
        self.assertNotIn("source_health unknown", out)
        # And the unrelated warning that IS legitimately expected here, stated
        # outright so a future reader does not rediscover it as a mystery.
        self.assertIn("WARNING: for_you screen fails closed", out)

    def test_missing_candidates_file_yields_unknown(self):
        h = Harness(self.tmp, [valid_story()])
        os.remove(h.candidates_path)
        rc, out, payload = h.run()
        self.assertEqual(payload["source_health"]["state"], "unknown")
        self.assertIn("WARNING: candidates.json unreadable (", out)

    def test_unreadable_config_warns_once_and_degrades_gracefully(self):
        h = Harness(self.tmp, [valid_story()], candidates=[entry()])
        os.remove(os.path.join(self.tmp, "config", "collection.json"))
        rc, out, payload = h.run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("critical_hosts"), 1)
        health = payload["source_health"]
        self.assertEqual(health["critical"]["total"], 0)
        self.assertEqual(health["state"], "ok")


class TestResearchStatusOverride(unittest.TestCase):
    """A4: the honesty signal is code, not the LLM's free text."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-status-")

    def _status(self, candidates, agent_value="full"):
        rc, _out, payload = Harness(self.tmp, [valid_story()],
                                   candidates=candidates,
                                   research_status=agent_value).run()
        return rc, payload

    def test_ok_state_forces_full(self):
        rc, payload = self._status([entry()], agent_value="limited")
        self.assertEqual(payload["source_health"]["state"], "ok")
        self.assertEqual(payload["research_status"], "full")
        self.assertEqual(payload["agent_research_status"], "limited")

    def test_degraded_state_forces_limited_even_if_agent_said_full(self):
        results = [entry(name="notice:tu.edu.np", kind="notice",
                         category="education_tu", host="tu.edu.np",
                         outcome="failed", error=TU_DNS),
                   entry(name="notice:iost.tu.edu.np", kind="notice",
                         category="education_tu", host="iost.tu.edu.np",
                         outcome="failed", error=TU_DNS)]
        rc, payload = self._status(results, agent_value="full")
        self.assertEqual(payload["source_health"]["state"], "degraded")
        self.assertEqual(payload["research_status"], "limited")
        self.assertEqual(payload["agent_research_status"], "full")

    def test_partial_and_unknown_also_force_limited(self):
        for results in ([entry(name="notice:nea.org.np", kind="notice",
                               category="local_city", host="nea.org.np",
                               outcome="failed", error="timed out")],
                        [entry(outcome="skipped_budget")]):
            rc, payload = self._status(results)
            self.assertIn(payload["source_health"]["state"],
                          ("partial", "unknown"))
            self.assertEqual(payload["research_status"], "limited", results)
            self.assertEqual(payload["agent_research_status"], "full")

    def test_agent_value_is_preserved_verbatim_when_garbage(self):
        rc, payload = self._status([entry()], agent_value="banana")
        self.assertEqual(payload["agent_research_status"], "banana")
        self.assertEqual(payload["research_status"], "full")


class TestAntiFabricationGate(unittest.TestCase):
    """A5: a story whose source host is known-dead is dropped."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-drop-")

    def _run(self, stories, results):
        rc, out, payload = Harness(self.tmp, stories, candidates=results).run()
        return rc, out, payload

    def test_story_from_a_failed_host_is_dropped(self):
        results = [entry(name="notice:tu.edu.np", kind="notice",
                         category="education_tu", host="tu.edu.np",
                         url="https://tu.edu.np/", outcome="failed",
                         error=TU_DNS)]
        rc, out, payload = self._run(
            [valid_story(source_url="https://tu.edu.np/notice/1")], results)
        self.assertEqual(payload["stories"], [])
        reasons = [e["reason"] for e in payload["filter_log"]]
        self.assertIn("source_unreachable:tu.edu.np", reasons)

    def test_story_from_a_host_that_was_never_attempted_survives(self):
        results = [entry(name="notice:tu.edu.np", kind="notice",
                         category="education_tu", host="tu.edu.np",
                         outcome="failed", error=TU_DNS)]
        rc, out, payload = self._run(
            [valid_story(source_url="https://kathmandupost.com/news/1")],
            results)
        self.assertEqual(len(payload["stories"]), 1)
        self.assertNotIn("source_unreachable:kathmandupost.com",
                         [e["reason"] for e in payload["filter_log"]])

    def test_story_from_a_reachable_host_survives(self):
        results = [entry(name="notice:tu.edu.np", kind="notice",
                         category="education_tu", host="tu.edu.np",
                         outcome="skipped_budget"),
                   entry()]
        rc, out, payload = self._run(
            [valid_story(source_url="https://news.google.com/x")], results)
        self.assertEqual(len(payload["stories"]), 1)

    def test_www_variant_of_a_failed_host_is_also_dropped(self):
        results = [entry(host="iost.tu.edu.np", kind="notice",
                         category="education_tu", outcome="failed",
                         error=TU_DNS)]
        rc, out, payload = self._run(
            [valid_story(source_url="https://www.iost.tu.edu.np/notice/1")],
            results)
        self.assertEqual(payload["stories"], [])

    def test_legacy_candidates_file_drops_nothing(self):
        rc, out, payload = Harness(self.tmp, [valid_story()],
                                  legacy_candidates=True).run()
        self.assertEqual(len(payload["stories"]), 1)
        self.assertEqual(payload["source_health"]["state"], "unknown")

    def test_budget_skipped_host_does_not_drop_stories(self):
        # UNKNOWN != DEAD: only outcome == "failed" drops a story.
        results = [entry(host="iost.tu.edu.np", kind="notice",
                         category="education_tu", outcome="skipped_budget")]
        rc, out, payload = self._run([valid_story()], results)
        self.assertEqual(len(payload["stories"]), 1)


class TestAntiFabricationOkShield(unittest.TestCase):
    """P2 regression: a sibling failure must not delete a working host's news."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-shield-")

    def _run(self, stories, results):
        return Harness(self.tmp, stories, candidates=results).run()

    def test_mixed_host_keeps_its_stories(self):
        # One notice page failed, the other succeeded on the SAME host.
        results = [
            entry(name="notice:tu.edu.np:exam", kind="notice",
                  category="education_tu", host="tu.edu.np",
                  url="https://tu.edu.np/exam/", outcome="failed", error=TU_DNS),
            entry(name="notice:tu.edu.np:notice", kind="notice",
                  category="education_tu", host="tu.edu.np",
                  url="https://tu.edu.np/notice/"),
        ]
        rc, out, payload = self._run(
            [valid_story(source_url="https://tu.edu.np/notice/1")], results)
        self.assertEqual(len(payload["stories"]), 1)
        self.assertNotIn("source_unreachable:tu.edu.np",
                         [e["reason"] for e in payload["filter_log"]])
        # The failure is still VISIBLE to the owner, it just does not delete.
        self.assertIn("hosts with any failure: tu.edu.np", out)
        self.assertIn("unreachable hosts: none", out)

    def test_mixed_host_still_reports_health(self):
        results = [
            entry(name="notice:tu.edu.np:exam", kind="notice",
                  category="education_tu", host="tu.edu.np",
                  outcome="failed", error=TU_DNS),
            entry(name="notice:tu.edu.np:notice", kind="notice",
                  category="education_tu", host="tu.edu.np"),
        ]
        rc, out, payload = self._run(
            [valid_story(source_url="https://tu.edu.np/notice/1")], results)
        # Reachable, so the host is not "down" -- but the failed task is still
        # counted and named, so nothing is hidden from the owner.
        health = payload["source_health"]
        self.assertEqual(health["all"]["failed"], 1)
        self.assertEqual(health["failed_names"], ["notice:tu.edu.np:exam"])
        self.assertEqual(health["critical"]["hosts_down"], [])
        self.assertEqual(health["critical"]["reasons"], ["dns"])
        self.assertEqual(health["reachable_hosts"], ["tu.edu.np"])

    def test_fully_dark_host_still_drops(self):
        results = [
            entry(name="notice:tu.edu.np:exam", kind="notice",
                  category="education_tu", host="tu.edu.np",
                  outcome="failed", error=TU_DNS),
            entry(name="notice:tu.edu.np:notice", kind="notice",
                  category="education_tu", host="tu.edu.np",
                  outcome="failed", error=TU_DNS),
        ]
        rc, out, payload = self._run(
            [valid_story(source_url="https://tu.edu.np/notice/1")], results)
        self.assertEqual(payload["stories"], [])
        self.assertIn("source_unreachable:tu.edu.np",
                      [e["reason"] for e in payload["filter_log"]])

    def test_ok_shield_is_www_and_port_insensitive(self):
        results = [
            entry(name="gnews:np-en:x", kind="gnews", category="education_tu",
                  host="tu.edu.np", outcome="failed", error=TU_DNS),
            entry(name="notice:www", kind="notice", category="education_tu",
                  host="www.tu.edu.np", url="https://www.tu.edu.np/"),
        ]
        rc, out, payload = self._run(
            [valid_story(source_url="https://www.tu.edu.np/x")], results)
        self.assertEqual(len(payload["stories"]), 1)


class TestUnreachableHostsFunction(unittest.TestCase):
    def test_ok_shield_semantics(self):
        mixed = [entry(host="tu.edu.np", outcome="failed", error=TU_DNS),
                 entry(host="tu.edu.np")]
        self.assertEqual(sh.unreachable_hosts(mixed), set())
        dark = [entry(host="tu.edu.np", outcome="failed", error=TU_DNS)]
        self.assertEqual(sh.unreachable_hosts(dark), {"tu.edu.np"})
        # A skipped sibling is neither an ok sibling nor a failure.
        skipped = [entry(host="tu.edu.np", outcome="failed", error=TU_DNS),
                   entry(host="tu.edu.np", outcome="skipped_budget")]
        self.assertEqual(sh.unreachable_hosts(skipped), {"tu.edu.np"})
        # Never attempted -> never unreachable.
        self.assertEqual(sh.unreachable_hosts([entry()]), set())
        # Malformed records carry no host, so they cannot darken anything.
        self.assertEqual(sh.unreachable_hosts([{"name": "x"}]), set())

    def test_hostile_inputs_never_raise(self):
        for value in (None, [], "x", 7,
                      [None, 1, {"host": 5, "outcome": "failed"}],
                      [{"host": "", "outcome": "failed"},
                       {"host": "h", "outcome": "failed"}, None, 3]):
            self.assertIsInstance(sh.unreachable_hosts(value), set)
        self.assertEqual(sh.unreachable_hosts(None), set())

    def test_failed_hosts_is_the_unshielded_diagnostic_view(self):
        mixed = [entry(host="tu.edu.np", outcome="failed", error=TU_DNS),
                 entry(host="tu.edu.np")]
        self.assertEqual(sh.failed_hosts(mixed), {"tu.edu.np"})
        self.assertEqual(sh.unreachable_hosts(mixed), set())

    def test_mass_drop_guard_still_holds_for_garbage_input(self):
        # The central safety claim: broken source_results can NEVER empty the
        # digest, because a drop needs a literal outcome == "failed" host.
        for junk in (None, [], {}, "broken", [None], [{"outcome": "failed"}],
                     [{"host": "h"}], [{"outcome": "ok", "host": "h"}]):
            self.assertEqual(sh.unreachable_hosts(junk), set(), junk)


class TestHonestCutoff(unittest.TestCase):
    """A6: data_cutoff_npt must not claim currency beyond the last fetch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-cutoff-")

    def test_cutoff_is_collected_at_not_the_later_generation_time(self):
        rc, out, payload = Harness(self.tmp, [valid_story()],
                                  collected_at="2026-09-26T18:30:00+00:00",
                                  candidates=[entry()]).run()
        # 18:30 UTC + 05:45 = 2026-09-27 00:15 Kathmandu.
        self.assertEqual(payload["data_cutoff_npt"], "2026-09-27 00:15")
        # And it must not have been silently "max"'d into the present.
        self.assertLess(payload["data_cutoff_npt"],
                        payload["generated_at_npt"])

    def test_cutoff_falls_back_to_generated_at_without_candidates(self):
        h = Harness(self.tmp, [valid_story()], collected_at="2026-09-26T18:30:00+00:00")
        os.remove(h.candidates_path)
        rc, out, payload = h.run()
        self.assertEqual(payload["data_cutoff_npt"],
                         payload["generated_at_npt"])

    def test_cutoff_is_older_than_generation_when_fetch_is_old(self):
        rc, out, payload = Harness(self.tmp, [valid_story()],
                                  collected_at="2026-09-20T00:00:00+00:00",
                                  candidates=[entry()]).run()
        self.assertEqual(payload["data_cutoff_npt"], "2026-09-20 05:45")


class TestLabelStrip(unittest.TestCase):
    """Internal priority chips must not leak into user-facing markup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="src-health-label-")

    def _label_of(self, label):
        rc, out, payload = Harness(self.tmp, [valid_story(label=label)],
                                  candidates=[entry()]).run()
        reasons = [e["reason"] for e in payload["filter_log"]]
        stripped = [r for r in reasons if r.startswith("label_stripped")]
        return payload, stripped

    def test_chips_are_stripped_at_the_schema_boundary(self):
        for chip in ("★ P5", "P7", "★P6", "P10", "☆ P3"):
            story = valid_story(label=chip)
            ok, _reason = vs.validate_story(story, 0)
            self.assertTrue(ok)
            self.assertEqual(story["label"], "", chip)

    def test_legit_labels_survive_the_strip(self):
        for label in ("NEW", "UPDATED", "DEVELOPING", "ONGOING", ""):
            story = valid_story(label=label)
            vs.validate_story(story, 0)
            self.assertEqual(story["label"], label)

    def test_missing_label_is_not_invented(self):
        story = valid_story()
        vs.validate_story(story, 0)
        self.assertNotIn("label", story)


if __name__ == "__main__":
    unittest.main()
