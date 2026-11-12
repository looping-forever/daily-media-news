#!/usr/bin/env python3
"""tests/collection/test_candidate_cap.py — the per-section cap is a decided number.

WHAT THIS PINS
--------------
Run 43235310367 (2026-11-12) kept exactly 4 candidates per section and lost 472
rows to the per-domain cap. The 4 was a hard-coded constant, so the edition's
pool was decided by the constant and not by how much news existed -- and with
history dedup then removing half of what survived, the edition came out at 2
cards.

The cap is 8 now, chosen from this run's own numbers: 24 extra slots were wanted
across 6 sections, 472 rows were available above the old cap, and MAX_TOTAL (140)
has 111 rows of headroom over the 29 kept. These tests pin:

  * the value itself, behaviourally -- one domain now contributes 8, not 4;
  * the telemetry that names the cap, the supply it was applied to, and what it
    dropped per section, so a binding cap can never again look like a quiet day;
  * the total ceiling still bounding the pool; and
  * the prompt advertising the SAME number the collector uses, because a prompt
    that says "4" while the collector keeps 8 is a lie the model reads.

Offline: literals and the module under test, stdlib unittest.
"""

import datetime
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import collect_news as cn  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)
PROMPT = os.path.join(REPO_ROOT, "prompts", "daily-digest.md")

#: Twelve headlines that share almost no tokens, so the in-run title check keeps
#: every one of them and the DOMAIN cap is the only thing that can bind.
TOPICS = ["Budget Session Opens", "Monsoon Flood Warning Issued",
          "Rail Project Tender Awarded", "Census Results Published",
          "Solar Plant Commissioned", "Airport Runway Reopens",
          "Cricket Squad Announced", "University Grants Approved",
          "Coffee Export Target Raised", "Bridge Repair Completed",
          "Film Festival Lineup Set", "Hospital Wing Inaugurated"]


def _cand(title, url, domain, hours_ago=1):
    """A raw candidate shaped exactly as the collector builds one."""
    dt = NOW - datetime.timedelta(hours=hours_ago)
    return {
        "title": title,
        "url": url,
        "source": "example.com",
        "published": dt.isoformat(),
        "snippet": "",
        "domain_category": domain,
        "language": "en",
        "_dt": dt,
        "_kind": "gnews",
    }


def one_domain(count, domain="world_major", prefix="zone"):
    # The prefix is a WORD inside the headline as well as the URL host: the
    # in-run title check is global and ignores tokens under three characters, so
    # "s1"/"s2" would collapse two sections into one and hide what the cap does.
    return [_cand("%s %s" % (TOPICS[i % len(TOPICS)], prefix),
                  "https://%s.example.com/%d" % (prefix, i), domain)
            for i in range(count)]


class ThePerSectionCapTests(unittest.TestCase):
    def test_one_domain_contributes_eight_not_four(self):
        final, per_domain, _stats = cn.postprocess(one_domain(12), [],
                                                   cn.MAX_PER_DOMAIN)

        self.assertEqual(8, per_domain.get("world_major"),
                         "a domain with 12 usable rows still yields %r; the cap "
                         "is back to deciding the edition instead of the supply"
                         % per_domain.get("world_major"))
        self.assertEqual(8, len(final))

    def test_the_cap_its_supply_and_its_losses_are_on_the_stats(self):
        _final, _per_domain, stats = cn.postprocess(one_domain(12), [],
                                                    cn.MAX_PER_DOMAIN)

        self.assertEqual(8, stats["cap_per_domain"])
        self.assertEqual(cn.MAX_TOTAL, stats["cap_total"])
        self.assertEqual(12, stats["available_by_domain"].get("world_major"),
                         "the report cannot say what the cap cost, because the "
                         "supply before the cap was never counted")
        self.assertEqual(4, stats["dropped_domain_cap_by_domain"].get("world_major"))
        self.assertEqual(4, stats["dropped_domain_cap"])

    def test_the_total_ceiling_still_bounds_the_pool(self):
        """Raising a per-domain cap must not raise the pool beyond MAX_TOTAL."""
        raw = []
        for index in range(20):
            raw.extend(one_domain(12, prefix="zone%d" % index,
                                  domain="section_%d" % index))

        final, _per_domain, stats = cn.postprocess(raw, [], cn.MAX_PER_DOMAIN)

        self.assertEqual(cn.MAX_TOTAL, len(final),
                         "the collector's input is no longer bounded by "
                         "MAX_TOTAL (%d)" % cn.MAX_TOTAL)
        self.assertGreater(stats["dropped_total_cap"], 0,
                           "the total ceiling bound but reported no drops")

    def test_the_prompt_states_the_cap_the_collector_uses(self):
        with open(PROMPT, encoding="utf-8") as fh:
            prompt = fh.read()

        self.assertIn("freshest %d candidates per" % cn.MAX_PER_DOMAIN, prompt,
                      "the prompt tells the model a per-section cap the "
                      "collector does not use")

    def test_the_report_names_the_cap_and_the_supply_it_was_applied_to(self):
        """The run log is the only surface a reader sees; it must carry both."""
        path = os.path.join(SCRIPTS, "collect_news.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()

        self.assertIn('"  caps: per_domain=%d; total=%d"', src,
                      "the report does not print the cap values")
        self.assertIn('stats["available_by_domain"]', src,
                      "the report does not print the supply the caps acted on, "
                      "so 4-per-section reads as a quiet day again")
        self.assertIn('"  interest cap: per list=%d; titles used %d of %d%s"', src,
                      "the query cap is not reported with its value, so a "
                      "search narrowed by a cap looks like a subject with no news")
        self.assertIn("interest_losses", src,
                      "the report cannot say which interest lists were only "
                      "partly searched")


if __name__ == "__main__":
    unittest.main()
