#!/usr/bin/env python3
"""tests/validate/test_empty_outcome.py — the empty-digest CLASSIFICATION.

Run:
    python -m unittest discover -s tests/validate -t . -v

WHAT IS PINNED HERE
-------------------
`derive_empty_outcome` decides whether an empty digest is a `quiet_day` (a
notice, plus a retry) or `broken` (a priority-5 page). On 2026-10-26 evening it
answered `broken` for a run that had collected 139 usable candidates, because
`source_health.state` was `degraded` (tu.edu.np and iost.tu.edu.np have been
timing out for days) and the rule read that as "collection failed". The owner
was paged for a quiet day.

The classification now reads the COLLECTION PATH as well as source health: a
non-empty candidate pool proves the collector fetched and parsed candidates, so
an empty digest on that evidence is dedup/newness, not an outage. Two
directions are pinned, and both matter equally:

  * a real pool under `degraded` health -> `quiet_day`  (the false page)
  * no pool at all under `degraded` health -> `broken`  (the real outage, which
    must NOT be silenced by the fix)

The other rows of the table are pinned too so the fix cannot quietly widen:
validator-dropped stories stay `quiet_day`, a produced digest has no kind, and
a critical feed that answered with nothing (`empty_hosts`) stays `broken` even
when the pool is full.

Offline and clock-free: every fixture is a literal, and `derive_empty_outcome`
takes `source_health` as data rather than reading a clock. Stdlib unittest only.
"""

import unittest

from . import REPO_ROOT, SCRIPTS  # noqa: F401  (SCRIPTS is put on sys.path there)

import validate_stories as vs  # noqa: E402


def candidate(title, url):
    return {"title": title, "url": url, "category": "national", "source": "test"}


def history_entry(title, url):
    """A fresh history row, already normalized the way load_history_entries does."""
    return {"url_normalized": vs.normalize_url(url),
            "title_hash": vs.title_hash(title),
            "title": title,
            "date_sent": None}


def health(state="degraded", hosts_down=(), empty_hosts=(), ok=57, total=59,
           failed=2, items_found=4058, all_total=81):
    """A source_health block shaped like source_health.compute_source_health."""
    return {
        "state": state,
        "critical": {"total": total, "ok": ok, "empty": 0, "failed": failed,
                     "skipped": 0, "hosts_down": list(hosts_down),
                     "empty_hosts": list(empty_hosts), "reasons": []},
        "all": {"total": all_total, "ok": 77, "empty": 0, "failed": 4,
                "skipped": 0, "items_found": items_found},
    }


# The reported run's shape: degraded health, both TU hosts dark, 0 empty hosts.
TU_DOWN = health(state="degraded", hosts_down=["iost.tu.edu.np", "tu.edu.np"])

# A pool where every single candidate is already in the fresh history.
HISTORY = [
    history_entry("Nepal Oil Corporation requires VAT invoices for cooking gas sales",
                  "https://english.nepalnews.com/s/business/noc-vat-invoices"),
    history_entry("Arkane Lyon given until mid-December to find a buyer",
                  "https://www.videogameschronicle.com/news/arkane-lyon-deadline"),
    history_entry("Steam Personal Calendar leaves early access",
                  "https://www.eurogamer.net/steam-personal-calendar-full-release"),
]

DEDUPED_POOL = [
    # same URL as history, with a tracking query the normalizer strips
    candidate("NOC makes VAT bills mandatory for cooking gas",
              "https://english.nepalnews.com/s/business/noc-vat-invoices?utm=x"),
    # same title as history, different URL (the gnews-redirect shape)
    candidate("Arkane Lyon given until mid-December to find a buyer",
              "https://news.google.com/rss/articles/CBMiabc"),
    # near-title: history tokens plus one extra word
    candidate("Steam Personal Calendar leaves early access today",
              "https://www.eurogamer.net/steam-personal-calendar-release-news"),
    # genuinely new, and still not published: the writer returned nothing
    candidate("Kathmandu Valley records first frost of the season",
              "https://kathmandupost.com/climate/2026/10/26/first-frost"),
]


class EmptyOutcomeClassification(unittest.TestCase):
    def test_dedup_exhausted_digest_under_degraded_health_is_quiet_day(self):
        outcome, kind, evidence = vs.derive_empty_outcome(
            [], [], TU_DOWN, candidates=DEDUPED_POOL, fresh_history=HISTORY)

        self.assertEqual(outcome, "empty")
        self.assertEqual(kind, "quiet_day")
        self.assertIn("collection path", evidence)
        self.assertIn("4 usable candidates", evidence)
        self.assertIn("3 of 4 already sent within the TTL", evidence)
        self.assertIn("not an outage", evidence)
        # The degraded counters are still REPORTED, not hidden.
        self.assertIn("source_health.state=degraded", evidence)
        self.assertIn("critical=57/59 ok", evidence)

    def test_the_pool_decides_not_the_state_string(self):
        """Unknown/zero-item health cannot out-vote a pool that exists."""
        unknown = health(state="unknown", ok=0, total=0, failed=0,
                         items_found=0, all_total=0)
        outcome, kind, evidence = vs.derive_empty_outcome(
            [], [], unknown, candidates=DEDUPED_POOL, fresh_history=HISTORY)

        self.assertEqual((outcome, kind), ("empty", "quiet_day"))
        self.assertIn("collection path", evidence)

    def test_genuine_collection_failure_stays_broken(self):
        """The fix must not silence the outage it was written next to."""
        for pool in (None, []):
            with self.subTest(pool=pool):
                outcome, kind, evidence = vs.derive_empty_outcome(
                    [], [], TU_DOWN, candidates=pool,
                    fresh_history=HISTORY)

                self.assertEqual(outcome, "empty")
                self.assertEqual(kind, "broken")
                self.assertIn("collection path", evidence)
                expected = ("unmeasured" if pool is None
                            else "0 usable candidates")
                self.assertIn(expected, evidence)

    def test_silent_critical_feed_stays_broken_even_with_a_full_pool(self):
        silent = health(state="empty", empty_hosts=["tu.edu.np"])
        outcome, kind, evidence = vs.derive_empty_outcome(
            [], [], silent, candidates=DEDUPED_POOL, fresh_history=HISTORY)

        self.assertEqual((outcome, kind), ("empty", "broken"))
        self.assertIn("tu.edu.np", evidence)

    def test_validator_dropped_stories_stay_quiet_day(self):
        outcome, kind, evidence = vs.derive_empty_outcome(
            [{"title": "collected but filtered"}], [], TU_DOWN)

        self.assertEqual((outcome, kind), ("empty", "quiet_day"))
        self.assertIn("the validator dropped", evidence)

    def test_produced_digest_has_no_kind(self):
        outcome, kind, _evidence = vs.derive_empty_outcome(
            [], [{"title": "one story survived"}], TU_DOWN)

        self.assertEqual((outcome, kind), ("produced", None))

    def test_healthy_sources_without_a_pool_are_still_quiet_day(self):
        outcome, kind, evidence = vs.derive_empty_outcome(
            [], [], health(state="ok"), fresh_history=HISTORY)

        self.assertEqual((outcome, kind), ("empty", "quiet_day"))
        self.assertIn("sources were up", evidence)


class CollectionEvidence(unittest.TestCase):
    def test_match_kinds_are_counted_separately(self):
        coverage = vs.history_coverage(DEDUPED_POOL, HISTORY)

        self.assertEqual(coverage, {"total": 4, "matched": 3, "by_url": 1,
                                    "by_title": 1, "by_overlap": 1})

    def test_forecast_url_is_not_counted_as_a_url_duplicate(self):
        """Forecasts recur by design; Stage 6 re-sends them on a new day."""
        pool = [candidate("Weather today in Kathmandu valley",
                          "https://api.open-meteo.com/v1/forecast?latitude=27.7")]
        row = history_entry("Kathmandu forecast",
                            "https://api.open-meteo.com/v1/forecast")

        self.assertEqual(vs.history_coverage(pool, [row])["matched"], 0)

    def test_ok_only_when_measured_and_non_empty(self):
        self.assertTrue(vs.collection_path_evidence(DEDUPED_POOL, HISTORY, TU_DOWN)["ok"])
        self.assertFalse(vs.collection_path_evidence(None, HISTORY, TU_DOWN)["ok"])
        self.assertFalse(vs.collection_path_evidence([], HISTORY, TU_DOWN)["ok"])
        self.assertFalse(vs.collection_path_evidence([{"url": "  "}], [], TU_DOWN)["ok"])

    def test_summary_names_the_counts_it_measured(self):
        summary = vs.collection_path_evidence(DEDUPED_POOL, HISTORY, TU_DOWN)["summary"]

        self.assertIn("4 usable candidates", summary)
        self.assertIn("4058 items found", summary)
        self.assertIn("57 critical ok", summary)


if __name__ == "__main__":
    unittest.main()
