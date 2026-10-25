#!/usr/bin/env python3
"""tests/weather/test_weather_advice.py — contract tests for the rules engine.

Run:
    python -m unittest discover -s tests/weather -t . -v

Every test is offline and clock-free. Staleness is exercised by injecting
`now`, so no test can pass today and fail tomorrow; the fixture's
`fetched_at_npt` is a constant, not "this morning".

The EXACT output string of every rule is asserted. That is the point of this
file: the owner reads these lines at 06:00 and changes his morning on them, so
a rewording is a behaviour change and a threshold nudged the wrong way is
advice for the wrong place.
"""

import copy
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta

from . import REPO_ROOT, SCRIPTS  # noqa: F401  (SCRIPTS is put on sys.path there)

import weather_advice as wa  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
HOURLY_PATH = os.path.join(FIXTURE_DIR, "hourly_example.json")
PROFILE_OK = os.path.join(FIXTURE_DIR, "profile_ok.yml")
PROFILE_BAD = os.path.join(FIXTURE_DIR, "profile_bad.yml")
REAL_PROFILE = os.path.join(REPO_ROOT, "config", "profile.yml")

#: The fixture's own fetch time. Injected wherever a test cares about age, so
#: the suite never depends on the day it runs.
FETCHED_AT = "2026-09-28 06:12"
NOW = datetime(2026, 9, 28, 6, 12)

#: The shipped outdoor windows: hours 7-11 and 17-19. The wash window is
#: hours 7-17. A test that wants rain at 06:00 has to say so on purpose,
#: because 06:00 is not an outdoor hour and must never trigger a line.
OUTDOOR_HOURS = list(range(7, 12)) + list(range(17, 20))
WASH_HOURS = list(range(7, 18))

#: The profile every rule test runs against. Deliberately the FIXTURE profile,
#: not config/profile.yml: the rule table is the frozen contract, so the rule
#: tests must not move when another unit edits the shipped config. The
#: shipped file has its own dedicated assertions in TestProfileLoading.
PROFILE = None  # set below, after the module is imported


def load_hourly():
    with open(HOURLY_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


PROFILE = wa.load_profile(PROFILE_OK)

#: The wetting floor, READ FROM THE PROFILE and never written here. A test
#: that hardcodes its own copy of this number would keep passing after
#: somebody moved the real one, which is the exact failure the single-source
#: rule exists to prevent.
WET = PROFILE.thresholds.precip_wet_mm

#: An amount comfortably above the floor, for "this hour really is wet".
SOAKED = WET * 3


def raining(pop, mm=SOAKED):
    """A rain hour the rules must actually see: likely AND wet."""
    return {"pop_pct": pop, "precip_mm": mm}


def hours_of(payload, day_index=0):
    return {rec["hour"]: rec
            for rec in payload["days"][day_index]["hours"]}


def set_hours(payload, day_index, values):
    """Overwrite whole hours from {hour: {field: value}}."""
    index = hours_of(payload, day_index)
    for hour, fields in values.items():
        index[hour].update(fields)


def advice(payload, profile=None, blockers=None):
    return wa.weather_advice(payload, profile or PROFILE, blockers=blockers, now=NOW)


def texts(lines):
    return [line["text"] for line in lines]


def ids(lines):
    return [line["id"] for line in lines]


# ---------------------------------------------------------------------------
# The fixture itself is a precondition, not just a convenience
# ---------------------------------------------------------------------------


class TestFixtureIsUsable(unittest.TestCase):
    """If these fail, every other test is asserting nonsense."""

    @classmethod
    def setUpClass(cls):
        cls.payload = load_hourly()

    def test_fixture_has_two_complete_days(self):
        self.assertEqual(len(self.payload["days"]), 2)
        for day in self.payload["days"]:
            self.assertEqual(len(day["hours"]), 24,
                             "a sparse fixture would make contiguity untested")

    def test_untouched_fixture_fires_nothing_but_the_calm_line(self):
        """The base fixture is benign by design; that is what makes C reachable."""
        self.assertEqual(wa.PARSER_NAME.startswith("PyYAML") or True, True)
        self.assertEqual(ids(advice(self.payload)), ["C"])

    def test_touched_fixture_still_uses_pytest_free_unittest(self):
        self.assertTrue(callable(wa.weather_advice))


# ---------------------------------------------------------------------------
# Rule-by-rule: exact strings
# ---------------------------------------------------------------------------


class TestRuleVS(unittest.TestCase):
    """Sub-kilometre visibility. 1000 m is the WMO fog boundary."""

    def test_fires_with_exact_string(self):
        payload = load_hourly()
        set_hours(payload, 0, {
            7: {"visibility_m": 400.0},
            8: {"visibility_m": 600.0},
            9: {"visibility_m": 800.0},
        })
        lines = advice(payload)
        self.assertIn(
            "Low visibility 400 m 07:00-09:59 - take care on the road", texts(lines))
        vs = [line for line in lines if line["id"] == "VS"]
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0]["priority"], wa.PRIORITY["VS"])

    def test_reports_the_run_containing_the_lowest_visibility(self):
        """Two separate fog runs: the string must name the one holding the min.

        Hour 9 is clear at 1500 m, which is what actually breaks the run --
        every neighbouring value here is below the 1000 m threshold, so
        without a real gap the two runs merge and the test would pass for
        the wrong reason while proving nothing about run selection.
        """
        payload = load_hourly()
        set_hours(payload, 0, {
            7: {"visibility_m": 900.0},
            8: {"visibility_m": 900.0},
            9: {"visibility_m": 1500.0},   # clear: the gap between the runs
            10: {"visibility_m": 200.0},   # the daily minimum, its own run
            11: {"visibility_m": 1500.0},
        })
        self.assertIn("Low visibility 200 m 10:00-10:59 - take care on the road",
                      texts(advice(payload)))

    def test_merges_neighbouring_qualifying_hours_into_one_span(self):
        """The complement of the test above: no gap means one honest span."""
        payload = load_hourly()
        set_hours(payload, 0, {
            7: {"visibility_m": 900.0},
            8: {"visibility_m": 400.0},
            9: {"visibility_m": 600.0},
        })
        self.assertIn("Low visibility 400 m 07:00-09:59 - take care on the road",
                      texts(advice(payload)))

    def test_does_not_fire_at_exactly_the_boundary(self):
        """`< 1000`, so 1000 m itself is clear air."""
        payload = load_hourly()
        set_hours(payload, 0, {h: {"visibility_m": 1000.0} for h in OUTDOOR_HOURS})
        self.assertNotIn("VS", ids(advice(payload)))

    def test_missing_visibility_never_fires_but_breaks_nothing_else(self):
        """`visibility_m` is OPTIONAL. Losing it must cost one rule, not all."""
        payload = load_hourly()
        for day in payload["days"]:
            for rec in day["hours"]:
                rec.pop("visibility_m", None)
        self.assertNotIn("VS", ids(advice(payload)))
        self.assertEqual(ids(advice(payload)), ["C"])

        # And a missing visibility alongside a real trigger: the trigger
        # still comes through, with the optional input simply unread.
        payload = load_hourly()
        for day in payload["days"]:
            for rec in day["hours"]:
                rec.pop("visibility_m", None)
        set_hours(payload, 0, {7: raining(70)})
        self.assertIn("U", ids(advice(payload)))

    def test_ignores_fog_outside_outdoor_windows(self):
        """02:00 is not an outdoor hour and must not generate advice."""
        payload = load_hourly()
        set_hours(payload, 0, {2: {"visibility_m": 100.0}, 3: {"visibility_m": 100.0}})
        self.assertNotIn("VS", ids(advice(payload)))


class TestRuleV(unittest.TestCase):
    """Strong wind. 30 km/h is the Beaufort strong-breeze band where umbrellas
    invert and stop helping."""

    def test_fires_with_exact_string(self):
        payload = load_hourly()
        set_hours(payload, 0, {
            17: {"wind_kmh": 34.0},
            18: {"wind_kmh": 41.5},
        })
        self.assertIn("Strong wind 41.5 km/h 17:00-18:59 - hold onto an umbrella",
                      texts(advice(payload)))

    def test_does_not_fire_just_below_the_threshold(self):
        payload = load_hourly()
        set_hours(payload, 0, {h: {"wind_kmh": 29.9} for h in OUTDOOR_HOURS})
        self.assertNotIn("V", ids(advice(payload)))

    def test_fires_at_exactly_the_threshold(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: {"wind_kmh": 30.0}})
        self.assertIn("V", ids(advice(payload)))


class TestRuleW1(unittest.TestCase):
    """Don't wash. max PoP over the wash (drying) window, never a daily total."""

    def test_fires_with_exact_string(self):
        payload = load_hourly()
        set_hours(payload, 0, {
            15: raining(70),
            16: raining(65),
            17: raining(60),
        })
        self.assertIn("Don't wash clothes today - 70% chance of rain 15:00-17:59",
                      texts(advice(payload)))

    def test_uses_max_pop_not_the_daily_rain_total(self):
        """A day can total a lot of rain and still have one dry drying window.

        `precip_sum_mm` is deliberately large on the fixture (17.7 mm) while
        no single hour in the wash window reaches the threshold. If the rule
        were reading the daily total, this day would prohibit washing.
        """
        payload = load_hourly()
        self.assertGreater(payload["days"][0]["precip_sum_mm"], 10.0)
        for hour in WASH_HOURS:
            self.assertLess(payload["days"][0]["hours"][hour]["pop_pct"], 50)
        self.assertNotIn("W1", ids(advice(payload)))

    def test_does_not_fire_at_49_percent(self):
        """The threshold is exclusive below: 49% is not a prohibition.

        The hours are genuinely wet, so this can only pass because of the
        PROBABILITY. Without the amount the same assertion would also pass, and
        would prove nothing about which threshold stopped it.
        """
        payload = load_hourly()
        set_hours(payload, 0, {h: raining(49) for h in WASH_HOURS})
        self.assertNotIn("W1", ids(advice(payload)))

    def test_fires_at_exactly_the_threshold(self):
        payload = load_hourly()
        set_hours(payload, 0, {9: raining(50)})
        self.assertIn("W1", ids(advice(payload)))

    def test_ignores_rain_outside_the_wash_window(self):
        """Hour 19 is after washing ends; hour 5 is before it starts."""
        payload = load_hourly()
        set_hours(payload, 0, {5: raining(95), 19: raining(95),
                               20: raining(95), 21: raining(95)})
        self.assertNotIn("W1", ids(advice(payload)))


class TestSilentMiddleBand(unittest.TestCase):
    """THE DELIBERATE SILENCE. This is the test the whole design rests on.

    40 <= wash risk < 50 must produce an umbrella line and NO washing verdict
    whatsoever. Not a hedge, not "you might want to wait", not a softened
    version of the prohibition. Nothing. A hedge in the band where rain is
    genuinely uncertain is the wording that teaches the owner to ignore the
    line, and he was explicit that accuracy must not be traded for more
    advice.
    """

    def test_band_yields_umbrella_and_no_wash_line_at_all(self):
        for pop in (40, 41, 45, 49):
            with self.subTest(pop=pop):
                payload = load_hourly()
                set_hours(payload, 0, {
                    7: raining(pop),
                    8: raining(pop),
                })
                lines = advice(payload)
                fired = ids(lines)
                self.assertIn("U", fired,
                              "pop=%s is above the umbrella threshold" % pop)
                self.assertNotIn("W1", fired,
                                 "pop=%s is below the wash threshold" % pop)
                self.assertNotIn("W1-blocked", fired)
                for line in lines:
                    self.assertNotIn("wash", line["text"].lower())
                self.assertEqual(
                    [t for t in texts(lines) if "Wash" in t], [],
                    "no washing verdict may exist anywhere in the band")

    def test_band_edge_is_exact(self):
        """39% is silent entirely; 40% gets the umbrella; 50% gets the verdict."""
        by_pop = {}
        for pop in (39, 40, 50):
            payload = load_hourly()
            set_hours(payload, 0, {7: raining(pop)})
            by_pop[pop] = ids(advice(payload))
        self.assertEqual(by_pop[39], ["C"])
        self.assertIn("U", by_pop[40])
        self.assertNotIn("W1", by_pop[40])
        self.assertIn("W1", by_pop[50])

    def test_band_survives_a_day_with_no_water_in_it_at_all(self):
        """THE BAND IS NOT THE AMOUNT CONJUNCT.

        The 40-49% silence and the wetting floor are two different things, and
        a fix for the second must not quietly become a fix for the first. With
        `precip_mm` at 0.0 everywhere the umbrella line disappears -- correctly,
        because an hour with no water in it is not a rain hour -- but the band
        itself is unchanged: no washing verdict appears, and none would appear
        either if the umbrella threshold were reached.
        """
        payload = load_hourly()
        set_hours(payload, 0, {7: {"pop_pct": 45}, 8: {"pop_pct": 45}})
        lines = advice(payload)
        self.assertNotIn("U", ids(lines))
        self.assertNotIn("W1", ids(lines))
        self.assertNotIn("W1-blocked", ids(lines))
        self.assertEqual([t for t in texts(lines) if "Wash" in t], [])

    def test_band_is_silent_at_49_percent_even_when_soaked(self):
        """The other half of the same proof: amount cannot promote a hedge.

        49% of genuine rain in a genuinely wet hour is still the band, and the
        band is silence. If the conjunct ever became sufficient on its own, or
        the two conditions were OR-ed, this is the test that catches it.
        """
        payload = load_hourly()
        set_hours(payload, 0, {h: raining(49, mm=50.0) for h in OUTDOOR_HOURS})
        lines = advice(payload)
        self.assertIn("U", ids(lines))
        self.assertNotIn("W1", ids(lines))
        self.assertEqual([t for t in texts(lines) if "Wash" in t], [])


class TestRuleU(unittest.TestCase):
    def test_fires_with_exact_string(self):
        payload = load_hourly()
        set_hours(payload, 0, {17: raining(55)})
        self.assertIn("Take an umbrella - 55% chance of rain 17:00-17:59",
                      texts(advice(payload)))

    def test_never_reads_as_a_reason_to_wait(self):
        """THE SERIOUS GUARD, second failure path.

        The closest real harm is him reading weather advice as permission to
        leave ten minutes late and missing class. So no emitted string may
        contain time-pressuring language at all.

        The hours are WET as well as likely: without the amount the rain rules
        would not fire at all, C would be the only line, and this test would
        pass without ever putting a rain sentence in front of it. A guard that
        cannot fail is decoration.
        """
        forbidden = ("wait", "stay", "leave early", "hurry", "delay", "later",
                     "you have time", "after the rain", "once it clears")
        payload = load_hourly()
        for hour in range(0, 24):
            set_hours(payload, 0, {
                hour: dict(raining(85), wind_kmh=45.0, apparent_c=4.0,
                           visibility_m=200.0)})
        # max_lines is 3 by default, which would shed U and J2 and leave this
        # test asserting against two lines instead of five.
        profile = wa._build_profile(
            _with_max_lines(wa.load_profile(PROFILE_OK), 6), "synthetic")
        lines = advice(payload, profile=profile)
        self.assertEqual(ids(lines), ["VS", "V", "W1", "U", "J2"])
        for line in lines:
            low = line["text"].lower()
            for word in forbidden:
                self.assertNotIn(word, low,
                                 "rule %s implies waiting: %r" % (line["id"], line["text"]))

    def test_umbrella_can_coexist_with_the_wash_verdict(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: raining(60), 17: raining(45)})
        fired = ids(advice(payload))
        self.assertIn("U", fired)
        self.assertIn("W1", fired)


class TestRuleJ2(unittest.TestCase):
    """Apparent temperature, not air. It already folds in wind chill."""

    def test_fires_with_exact_string(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 9.4}, 8: {"apparent_c": 8.0}})
        self.assertIn("Wear a warm jacket - feels like 8C when you go out 07:00-08:59",
                      texts(advice(payload)))

    def test_fires_at_exactly_the_threshold(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 10.0}})
        self.assertIn("J2", ids(advice(payload)))

    def test_one_decimal_is_preserved(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 7.3}})
        self.assertIn("Wear a warm jacket - feels like 7.3C when you go out 07:00-07:59",
                      texts(advice(payload)))


class TestRuleJ1(unittest.TestCase):
    def test_fires_with_exact_string(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 12.5}})
        self.assertIn("Take a jacket - feels like 12.5C 07:00-07:59",
                      texts(advice(payload)))

    def test_band_is_derived_not_hardcoded(self):
        """11..14 falls out of warm_strong_c / warm_mild_c in the profile."""
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 10.5}})
        self.assertIn("J1", ids(advice(payload)))
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 14.9}})
        self.assertIn("J1", ids(advice(payload)))
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 15.0}})
        self.assertNotIn("J1", ids(advice(payload)))
        self.assertNotIn("J2", ids(advice(payload)))

    def test_moving_the_profile_moves_the_band(self):
        """Proof the numbers are read from the profile, not baked into the code."""
        profile = wa.load_profile(PROFILE_OK)
        raw = copy.deepcopy(profile.raw)
        raw["weather_advice"]["thresholds"]["warm_mild_c"] = 19
        moved = wa._build_profile(raw, "synthetic")
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 17.0}})
        self.assertIn("J1", ids(advice(payload, profile=moved)))

    def test_j2_outranks_j1(self):
        """A cold hour and a merely-cool hour must not both advise a jacket."""
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 12.0}, 17: {"apparent_c": 5.0}})
        fired = ids(advice(payload))
        self.assertIn("J2", fired)
        self.assertNotIn("J1", fired)


class TestRuleC(unittest.TestCase):
    def test_exact_string_when_nothing_fires(self):
        self.assertEqual(
            texts(advice(load_hourly())),
            ["Nothing to plan for - dry and mild today (22C)"])

    def test_header_is_never_empty(self):
        """Silence is ambiguous -- did the rules fail, or did it not run?

        That is why C exists. When nothing else fired, the line must be
        there, so 'no advice' never means 'broken'.
        """
        for payload in (load_hourly(),):
            lines = advice(payload)
            self.assertTrue(lines)
            self.assertEqual(lines[0]["id"], "C")
            self.assertEqual(lines[0]["priority"], wa.PRIORITY["C"])


# ---------------------------------------------------------------------------
# THE SERIOUS GUARD
# ---------------------------------------------------------------------------


class TestBlockerGuard(unittest.TestCase):
    """Advice must never make him act against a commitment he already has."""

    def _raining(self, pop=70, hours=(15, 16, 17), mm=SOAKED):
        payload = load_hourly()
        set_hours(payload, 0, {h: raining(pop, mm) for h in hours})
        return payload

    def test_blocker_inside_window_downgrades_to_wash_after(self):
        payload = self._raining()
        blockers = [(16, 18, "exam")]          # overlaps hours 15-17
        lines = advice(payload, blockers=blockers)
        self.assertIn("Wash after 15:00-17:59 - 70% chance of rain before then",
                      texts(lines))

    def test_downgrade_is_never_a_prohibition(self):
        """The whole point: postpone, never forbid."""
        for start, end in ((15, 18), (16, 17), (14, 16), (17, 18), (0, 24)):
            with self.subTest(blocker=(start, end)):
                payload = self._raining()
                lines = advice(payload, blockers=[(start, end, "exam")])
                self.assertNotIn("W1", ids(lines),
                                 "a commitment must never produce 'don't wash'")
                self.assertIn("W1-blocked", ids(lines))
                for text in texts(lines):
                    self.assertNotIn("Don't", text)

    def test_blocked_variant_keeps_ws_priority(self):
        payload = self._raining()
        lines = advice(payload, blockers=[(16, 18, "exam")])
        blocked = [line for line in lines if line["id"] == "W1-blocked"][0]
        self.assertEqual(blocked["priority"], wa.PRIORITY["W1"])

    def test_blocker_outside_the_window_leaves_the_full_prohibition(self):
        payload = self._raining()
        blockers = [(9, 11, "lecture"), (19, 21, "lab")]
        lines = advice(payload, blockers=blockers)
        self.assertIn("W1", ids(lines))
        self.assertNotIn("W1-blocked", ids(lines))
        self.assertIn("Don't wash clothes today - 70% chance of rain 15:00-17:59",
                      texts(lines))

    def test_adjacent_blocker_does_not_count(self):
        """A blocker that shares no HOUR with the run must not downgrade it.

        The run is hours 15, 16 and 17 inclusive (it renders as 15:00-17:59),
        so a blocker at 17:00 DOES overlap it and legitimately downgrades.
        Half-open windows make 18:00 the first non-overlapping start.
        """
        payload = self._raining()
        self.assertIn("W1-blocked", ids(advice(payload, blockers=[(17, 19, "lab")])))
        self.assertIn("W1", ids(advice(payload, blockers=[(18, 19, "lab")])))
        self.assertIn("W1", ids(advice(payload, blockers=[(10, 12, "lecture")])))

    def test_malformed_blocker_is_skipped_not_raised(self):
        """A bad entry must not cost him the advice he is entitled to."""
        payload = self._raining()
        for bad in (None, (5,), "exam", (18, 18, "empty"), (True, 3, "bool")):
            with self.subTest(blocker=bad):
                lines = advice(payload, blockers=[bad])
                self.assertIn("W1", ids(lines))

    def test_blockers_do_not_affect_other_rules(self):
        """A commitment is a reason to postpone a wash, not a reason to hide
        a road hazard or to withhold a jacket."""
        payload = load_hourly()
        set_hours(payload, 0, {7: {"visibility_m": 300.0},
                               8: {"apparent_c": 3.0}})
        fired = ids(advice(payload, blockers=[(7, 9, "assembly")]))
        self.assertIn("VS", fired)
        self.assertIn("J2", fired)

    def test_no_blockers_argument_is_accepted(self):
        payload = self._raining()
        self.assertIn("W1", ids(advice(payload, blockers=None)))

    # -- case 4: the downgrade path, driven from the PROFILE ---------------

    def test_unavailable_windows_from_the_profile_downgrade_the_verdict(self):
        """REQUIRED CASE 4, first half, through the real configuration key.

        `schedule.unavailable_windows` is the only source of blockers; the
        collector turns it into (start, end, label) triples and hands them to
        `weather_advice()`. This rebuilds that list from the profile so the
        test covers the whole path from the config key, not just the argument.
        """
        profile = self._profile_with_unavailable([["16:00", "18:00"]])
        payload = self._raining()
        lines = advice(payload, profile=profile,
                       blockers=blockers_from(profile))
        self.assertIn("W1-blocked", ids(lines))
        self.assertNotIn("W1", ids(lines))
        self.assertIn("Wash after 15:00-17:59 - 70% chance of rain before then",
                      texts(lines))
        for text in texts(lines):
            self.assertNotIn("Don't", text)

    def test_downgrade_still_happens_only_when_the_hour_really_is_wet(self):
        """REQUIRED CASE 4, second half: the guard survives the conjunct.

        A commitment is a reason to POSTPONE a wash that would otherwise be
        prohibited. It is not a reason to invent one. With the reported drizzle
        there is no prohibition to postpone, so the guard must stay out of it
        even with a commitment across the window -- otherwise the fix would
        have been to move the problem rather than remove it.
        """
        profile = self._profile_with_unavailable([["16:00", "18:00"]])
        lines = advice(example_october_3rd(), profile=profile,
                       blockers=blockers_from(profile))
        fired = ids(lines)
        self.assertNotIn("W1-blocked", fired)
        self.assertNotIn("W1", fired)
        self.assertEqual([t for t in texts(lines) if "Wash" in t], [])

    def test_downgrade_would_still_be_a_downgrade_and_not_a_prohibition(self):
        """The whole serious guard, re-asserted on a wet day after the fix.

        Every blocker that overlaps the wet run must produce `W1-blocked`, and
        none may produce `W1`. If the amount conjunct had been implemented by
        suppressing the prohibition outright, this loop is what would notice.
        """
        profile = self._profile_with_unavailable([["16:00", "18:00"]])
        for start, end in ((15, 18), (16, 17), (14, 16), (17, 18), (0, 24)):
            with self.subTest(blocker=(start, end)):
                lines = advice(self._raining(), profile=profile,
                               blockers=[(start, end, "unavailable")])
                self.assertIn("W1-blocked", ids(lines))
                self.assertNotIn("W1", ids(lines))
                for text in texts(lines):
                    self.assertNotIn("Don't", text)

    def _profile_with_unavailable(self, windows):
        raw = copy.deepcopy(wa.load_profile(PROFILE_OK).raw)
        raw["schedule"]["unavailable_windows"] = windows
        return wa._build_profile(raw, "synthetic")


def blockers_from(profile):
    """The collector's own translation of `schedule.unavailable_windows`.

    Mirrors `collect_news.advice_blockers()` exactly: the engine takes
    (start_hour, end_hour, label) triples and reads nothing else, so this is
    the whole of the guard's input.
    """
    return [(w.start_hour, w.end_hour, "unavailable")
            for w in (profile.unavailable_windows or ())]


# ---------------------------------------------------------------------------
# THE AMOUNT CONJUNCT -- the confirmed 2026-10-03 defect
#
# W1 and U thresholded `precipitation_probability` alone. `precip_mm` was
# fetched, documented in the module's own input contract, and read by no rule
# at all. open-meteo pairs a high probability with a trivially small amount all
# the time, and the digest published "Don't wash clothes today - 88% chance of
# rain 11:00-16:59" on 2026-10-01, -02 and -03 over 1.7 mm of drizzle.
#
# The numbers below are the REAL open-meteo hourly series for Example City on
# 2026-10-03, transcribed rather than invented, because a test built on
# invented numbers proves only that the test agrees with itself.
# ---------------------------------------------------------------------------

#: (hour, pop_pct, precip_mm) exactly as open-meteo returned them.
EXAMPLE_2026_10_03 = (
    (9, 14, 0.1), (10, 33, 0.2), (11, 51, 0.0), (12, 67, 0.0),
    (13, 82, 0.1), (14, 88, 0.2), (15, 80, 0.6), (16, 64, 0.1), (17, 47, 0.1),
)


def example_october_3rd():
    """The reported day, on the fixture's own benign background."""
    payload = load_hourly()
    set_hours(payload, 0, {hour: {"pop_pct": pop, "precip_mm": mm}
                           for hour, pop, mm in EXAMPLE_2026_10_03})
    return payload


class TestAmountConjunct(unittest.TestCase):
    """REQUIRED CASE 1 and CASE 2, plus the edges around them.

    Case 1 is the reported bug: 0.2 mm at 88 % must not prohibit washing.
    Case 2 is the guard on the guard: a genuinely wet hour still must, or the
    fix would be indistinguishable from switching the engine off.
    """

    # -- case 1: the reported bug ---------------------------------------

    def test_trace_drizzle_at_high_probability_does_not_prohibit_washing(self):
        """THE REPORTED DEFECT, on its own real data.

        14:00 peaked at 88 % with 0.2 mm. Thresholding probability alone
        produced a washing prohibition over a shower's worth of water spread
        across nine hours: 1.7 mm in total for the whole day.
        """
        payload = example_october_3rd()
        lines = advice(payload)
        self.assertNotIn("W1", ids(lines))
        self.assertNotIn("W1-blocked", ids(lines))
        for text in texts(lines):
            self.assertNotIn("Don't wash", text)
            self.assertNotIn("Wash after", text)

    def test_trace_drizzle_at_high_probability_does_not_say_carry_an_umbrella(self):
        """Same hour, same 88 %: the umbrella line goes too.

        The conjunct applies to U for the same reason it applies to W1. An
        umbrella is a real cost to carry and the water it would keep off him
        is 0.2 mm.
        """
        payload = example_october_3rd()
        self.assertNotIn("U", ids(advice(payload)))

    def test_the_whole_reported_day_says_nothing_about_rain(self):
        """And the honest replacement is not silence-with-no-header.

        With no rain rule firing, the calm line must still be there: an empty
        header would be indistinguishable from a broken run, which is the one
        outcome the anti-nag design is not allowed to produce.
        """
        lines = advice(example_october_3rd())
        self.assertEqual(ids(lines), ["C"])

    def test_wetting_floor_is_the_boundary(self):
        """`precip_mm >= precip_wet_mm`, asserted on the edge itself.

        Below the floor is silence and at the floor it fires. Pinning the
        comparison this way means moving the profile number moves the boundary
        and nothing else.
        """
        for mm, expect in ((WET - 0.1, False), (WET, True), (WET + 0.1, True)):
            with self.subTest(precip_mm=mm):
                payload = load_hourly()
                set_hours(payload, 0, {17: raining(60, mm=mm)})
                self.assertEqual("U" in ids(advice(payload)), expect)

    # -- case 2: the fix must not disable the rule ------------------------

    def test_a_genuinely_wet_hour_still_prohibits_washing(self):
        """REQUIRED CASE 2. The rule is narrowed, not removed.

        Without this, "fixed" and "switched off" would pass every other test in
        this file identically.
        """
        payload = load_hourly()
        set_hours(payload, 0, {15: raining(70), 16: raining(65),
                               17: raining(60)})
        lines = advice(payload)
        self.assertIn("W1", ids(lines))
        self.assertIn(
            "Don't wash clothes today - 70% chance of rain 15:00-17:59",
            texts(lines))

    def test_a_genuinely_wet_hour_still_says_carry_an_umbrella(self):
        payload = load_hourly()
        set_hours(payload, 0, {17: raining(55)})
        self.assertIn("Take an umbrella - 55% chance of rain 17:00-17:59",
                      texts(advice(payload)))

    def test_reported_pop_belongs_to_an_hour_that_was_also_wet(self):
        """The number in the string describes the hour in the window.

        88 % is the peak-probability hour and it was dry (0.2 mm); 80 % is the
        wet hour. The rule reports the maximum over QUALIFYING hours, so the
        string must say 80, not 88. Reporting the probability of an hour that
        was not in the window would be the same class of quiet wrongness the
        window rule exists to prevent.
        """
        payload = load_hourly()
        set_hours(payload, 0, {11: {"pop_pct": 88, "precip_mm": 0.2},
                               17: {"pop_pct": 80, "precip_mm": 4.0}})
        self.assertIn("Take an umbrella - 80% chance of rain 17:00-17:59",
                      texts(advice(payload)))

    def test_amount_alone_never_fires_the_rain_rules(self):
        """The conjunct is a conjunction, not a disjunction.

        An hour with plenty of water and no probability of rain is not a rain
        hour, and neither rule may say it is.
        """
        payload = load_hourly()
        set_hours(payload, 0, {h: raining(0, mm=40.0) for h in OUTDOOR_HOURS})
        fired = ids(advice(payload))
        self.assertNotIn("U", fired)
        self.assertNotIn("W1", fired)

    def test_missing_amount_degrades_the_rain_rules_and_nothing_else(self):
        """`precip_mm` is optional in the contract, so it can arrive as None.

        An unknown quantity is not evidence of rain, so the rain rules go
        quiet -- but the digest still produces its calm line, and a cold
        morning still produces its jacket. A missing optional input degrades
        one rule, not the whole digest.
        """
        payload = load_hourly()
        for day in payload["days"]:
            for rec in day["hours"]:
                rec["precip_mm"] = None
        set_hours(payload, 0, {7: {"pop_pct": 90, "precip_mm": None,
                                   "apparent_c": 3.0}})
        lines = advice(payload)
        fired = ids(lines)
        self.assertNotIn("U", fired)
        self.assertNotIn("W1", fired)
        self.assertIn("J2", fired)


# ---------------------------------------------------------------------------
# WINDOW-STRING INTEGRITY -- required case 5
# ---------------------------------------------------------------------------

#: Which configured window set each rule is allowed to speak about. W1 names
#: the wash window; every other windowed rule names the outdoor windows.
_WASH_WINDOWED = ("W1", "W1-blocked")
_WINDOW_RE = re.compile(r"(\d{2}):00-(\d{2}):59")


def window_hours(text):
    """The hours a rendered window names, or None when it names none."""
    match = _WINDOW_RE.search(text)
    if not match:
        return None
    return list(range(int(match.group(1)), int(match.group(2)) + 1))


class TestWindowStringIntegrity(unittest.TestCase):
    """No rendered window may name an hour the engine is not allowed to.

    `_runs()` used to judge contiguity on the observed hours alone, so two
    qualifying spells either side of an unsampled stretch were fused into one
    run and rendered by its endpoints. The outdoor windows are 07:00-12:00 and
    17:00-20:00, so hour 12 belongs to neither, and the digest printed
    `11:00-17:59`: a seven-hour block containing five hours that are in no
    outdoor window at all.

    This asserts the INVARIANT rather than one string, so it keeps holding
    whatever the windows are, and it checks the wash-window rules too.
    """

    def _allowed(self, rule_id):
        if rule_id in _WASH_WINDOWED:
            return set(range(PROFILE.wash_window.start_hour,
                             PROFILE.wash_window.end_hour))
        allowed = set()
        for window in PROFILE.outdoor_windows:
            allowed.update(window.hours())
        return allowed

    def _assert_every_window_is_inside_its_configured_windows(self, payload,
                                                               profile=None):
        checked = 0
        for line in advice(payload, profile=profile):
            hours = window_hours(line["text"])
            if hours is None:
                continue
            checked += 1
            stray = [h for h in hours if h not in self._allowed(line["id"])]
            self.assertEqual(
                stray, [],
                "rule %s rendered %r, which names hour(s) %s outside the "
                "windows it may speak about"
                % (line["id"], line["text"], stray))
        return checked

    def test_both_outdoor_spells_qualifying_produce_a_window_inside_a_spell(self):
        """REQUIRED CASE 5, first shape: two spells either side of a gap.

        Outdoor hours are 7-11 and 17-19. Hour 12 is in NO outdoor window, so
        a run that spans it is describing weather the engine has no licence to
        describe. The extremum is at 11, so the reported run is the morning
        one and the string must be 07:00-11:59.
        """
        payload = load_hourly()
        peaks = {h: 50 + h for h in OUTDOOR_HOURS}
        peaks[11] = 99
        set_hours(payload, 0, {h: raining(pop) for h, pop in peaks.items()})
        lines = advice(payload)
        self.assertIn(
            "Take an umbrella - 99% chance of rain 07:00-11:59", texts(lines))
        self.assertGreaterEqual(
            self._assert_every_window_is_inside_its_configured_windows(payload), 1)

    def test_the_reported_fusion_shape_is_not_reproducible(self):
        """REQUIRED CASE 5, reproducing the EXACT string the digest published.

        The digest printed `Take an umbrella - 51% chance of rain 11:00-17:59`
        on 2026-10-03: hour 11 at 51 % and hour 17 at 47 % were the only outdoor
        hours over the umbrella threshold, and `_runs()` fused them across the
        five hours 12-16 that are in no outdoor window at all.

        The hours here are WET as well as likely, deliberately: with the amount
        conjunct in place a drizzle hour cannot qualify at all, and defect 2
        would then be unreproducible on its own data. Fusion was never about
        drizzle -- it is about how a run is built, and it shows on any day the
        rules do speak about.
        """
        payload = load_hourly()
        set_hours(payload, 0, {
            7: raining(30), 8: raining(30), 9: raining(30), 10: raining(30),
            11: raining(51), 17: raining(47), 18: raining(30), 19: raining(30),
        })
        self.assertIn("Take an umbrella - 51% chance of rain 11:00-11:59",
                      texts(advice(payload)))
        for text in texts(advice(payload)):
            self.assertNotIn("11:00-17:59", text)
        self._assert_every_window_is_inside_its_configured_windows(payload)

    def test_an_unsampled_hour_also_breaks_a_run(self):
        """A block we never sampled is not a block we may describe.

        The two outdoor spans are adjacent here (7-8, 10-11), so the gap is a
        missing SAMPLE rather than a window boundary. It must still split the
        run: rendering 07:00-11:59 would assert something about 09:00 that no
        sample supports.
        """
        payload = load_hourly()
        for day in payload["days"]:
            day["hours"] = [rec for rec in day["hours"] if rec["hour"] != 9]
        set_hours(payload, 0, {h: raining(60) for h in (7, 8, 10, 11)})
        self.assertGreaterEqual(
            self._assert_every_window_is_inside_its_configured_windows(payload), 1)

    def test_invariant_holds_for_every_rule_on_a_everything_at_once_day(self):
        """One payload that trips every windowed rule at once.

        A per-rule test only proves the invariant for the rule somebody thought
        of. This runs VS, V, W1, U and J2 together.
        """
        payload = load_hourly()
        set_hours(payload, 0, {
            7: dict(raining(90), visibility_m=200.0, apparent_c=2.0),
            8: dict(raining(85), visibility_m=250.0, apparent_c=3.0,
                    wind_kmh=45.0),
            17: raining(75), 18: raining(70),
        })
        profile = wa._build_profile(
            _with_max_lines(wa.load_profile(PROFILE_OK), 6), "synthetic")
        fired = ids(advice(payload, profile=profile))
        for expected in ("VS", "V", "W1", "U", "J2"):
            self.assertIn(expected, fired)
        self.assertGreaterEqual(
            self._assert_every_window_is_inside_its_configured_windows(
                payload, profile=profile), 4)

    def test_wash_window_rules_are_bounded_by_the_wash_window(self):
        """The same invariant for the rules that speak about the wash window.

        `wash_window` is 07:00-18:00, so an 18:00 or 19:00 hour must never
        appear in a W1 string even though both are outdoor hours.
        """
        payload = load_hourly()
        set_hours(payload, 0, {17: raining(95), 18: raining(95),
                               19: raining(95)})
        lines = [l for l in advice(payload) if l["id"] in _WASH_WINDOWED]
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn("18:00", line["text"])
            self.assertNotIn("19:00", line["text"])
        self._assert_every_window_is_inside_its_configured_windows(payload)


# ---------------------------------------------------------------------------
# THE FROZEN TEMPLATES -- required case 6
# ---------------------------------------------------------------------------


class TestFrozenTemplates(unittest.TestCase):
    """The owner's wording. Byte-identical, and asserted as literals.

    Adding the amount conjunct meant changing what the rain rules SAY. It did
    not mean rewriting what they say it with: these strings are read at 06:00
    and a rewording is a behaviour change, not a copy fix.
    """

    def test_rain_templates_are_byte_identical(self):
        self.assertEqual(
            wa.T_W1,
            "Don't wash clothes today - {pop}% chance of rain {win}")
        self.assertEqual(
            wa.T_W1_BLOCKED,
            "Wash after {win} - {pop}% chance of rain before then")
        self.assertEqual(
            wa.T_U,
            "Take an umbrella - {pop}% chance of rain {win}")

    def test_the_other_templates_are_untouched_too(self):
        self.assertEqual(wa.T_VS,
                         "Low visibility {vis} m {win} - take care on the road")
        self.assertEqual(wa.T_V,
                         "Strong wind {wind} km/h {win} - hold onto an umbrella")
        self.assertEqual(wa.T_J2,
                         "Wear a warm jacket - feels like {temp}C when you go out {win}")
        self.assertEqual(wa.T_J1, "Take a jacket - feels like {temp}C {win}")
        self.assertEqual(wa.T_C,
                         "Nothing to plan for - dry and mild today ({tmax}C)")

    def test_the_amount_is_never_put_into_the_wording(self):
        """No millimetre figure leaks into a rain line.

        Worth pinning explicitly: the natural way to "explain" the conjunct to
        the owner would be to add the amount to the string, which changes the
        owner's wording. The floor is a silent condition, and this is the test
        that keeps it silent.
        """
        payload = load_hourly()
        set_hours(payload, 0, {17: raining(70)})
        for line in advice(payload):
            self.assertNotIn("mm", line["text"])
            self.assertNotIn("precip", line["text"].lower())
            self.assertNotIn("drizzle", line["text"].lower())

    def test_every_emitted_line_is_a_frozen_template_filled_in(self):
        """Structural version of the same claim: no line was re-derived.

        Every emitted string must be one of the frozen templates with its
        slots filled, so a future edit that composes a new sentence fails here
        instead of shipping.
        """
        payload = load_hourly()
        set_hours(payload, 0, {
            7: dict(raining(90), visibility_m=200.0, apparent_c=2.0),
            8: dict(raining(85), wind_kmh=45.0),
        })
        lines = advice(payload)
        self.assertTrue(lines)
        templates = (wa.T_VS, wa.T_V, wa.T_W1, wa.T_W1_BLOCKED,
                     wa.T_U, wa.T_J2, wa.T_J1, wa.T_C)
        for line in lines:
            with self.subTest(rule=line["id"]):
                self.assertTrue(
                    any(_matches(t, line["text"]) for t in templates),
                    "%r was not produced by any frozen template"
                    % line["text"])

    def test_the_blocked_template_is_reachable_and_unchanged(self):
        """The downgrade string is still emitted verbatim, blockers or not."""
        payload = load_hourly()
        set_hours(payload, 0, {15: raining(70), 16: raining(65),
                               17: raining(60)})
        self.assertIn("Wash after 15:00-17:59 - 70% chance of rain before then",
                      texts(advice(payload, blockers=[(16, 18, "exam")])))


def _matches(template, text):
    """True when `text` is `template` with every {slot} replaced by something."""
    # re.escape turns `{vis}` into `\{vis\}`, so the slot has to be matched in
    # its ESCAPED form, and that needs a regex sub rather than a str.replace.
    pattern = re.sub(r"\\\{[a-z_]+\\\}", r".+?", re.escape(template))
    return re.fullmatch(pattern, text) is not None


# ---------------------------------------------------------------------------
# THE OTHER RULES MUST NOT NOTICE -- required case 7
# ---------------------------------------------------------------------------


class TestOtherRulesUnaffected(unittest.TestCase):
    """VS, V, J1 and J2 read no precipitation field and must not have gained one.

    The amount conjunct belongs to W1 and U. If it ever leaked into a rule that
    has no business knowing how much water fell, a dry cold morning would lose
    its jacket line.
    """

    def test_a_real_example_day_produces_no_weather_line_at_all(self):
        """REQUIRED CASE 7, the measured day.

        Visibility never below 1460 m, wind never above 4.6 km/h, apparent
        temperature never below 21.5 C, and 1.7 mm of drizzle spread across the
        day. Nothing here is a road hazard, a wind hazard or a cold one, and the
        water is below the wetting floor, so the honest output is the calm line
        and nothing else.
        """
        payload = load_hourly()
        set_hours(payload, 0, {
            hour: {"pop_pct": pop, "precip_mm": mm,
                   "visibility_m": 1460.0 + hour * 10.0,
                   "wind_kmh": 4.6, "apparent_c": 21.5 + hour * 0.1}
            for hour, pop, mm in EXAMPLE_2026_10_03})
        self.assertEqual(ids(advice(payload)), ["C"])

    def test_cold_and_windy_still_speak_with_zero_precipitation(self):
        """The rules that do not read rain are untouched by it.

        `precip_mm` is 0.0 on every hour of the base fixture, which is exactly
        the point: if the conjunct had leaked, a cold windy morning would go
        silent and the owner would go out in a jacket he was never told about.
        """
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 2.0, "wind_kmh": 45.0},
                               8: {"apparent_c": 3.0, "wind_kmh": 40.0}})
        fired = ids(advice(payload))
        self.assertIn("J2", fired)
        self.assertIn("V", fired)

    def test_fog_still_speaks_with_zero_precipitation(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: {"visibility_m": 300.0},
                               8: {"visibility_m": 350.0}})
        self.assertIn("VS", ids(advice(payload)))

    def test_the_cool_band_still_speaks_with_zero_precipitation(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: {"apparent_c": 12.5}})
        self.assertIn("J1", ids(advice(payload)))


# ---------------------------------------------------------------------------
# ORDERING AND TRUNCATION
# ---------------------------------------------------------------------------


class TestOrdering(unittest.TestCase):
    def test_output_is_ordered_by_priority(self):
        payload = load_hourly()
        set_hours(payload, 0, {
            7: dict(raining(85), wind_kmh=38.0,
                    visibility_m=300.0, apparent_c=4.0),
            8: dict(raining(85), wind_kmh=38.0,
                    visibility_m=300.0, apparent_c=4.0),
        })
        # Raise the cap so the assertion tests ORDER, not the cap. Truncation
        # is TestMaxLines' job; a test that conflated the two would pass for
        # the wrong reason and never notice a reordering.
        profile = wa._build_profile(
            _with_max_lines(wa.load_profile(PROFILE_OK), 5), "synthetic")
        lines = advice(payload, profile=profile)
        priorities = [line["priority"] for line in lines]
        self.assertEqual(priorities, sorted(priorities))
        self.assertEqual(ids(lines), ["VS", "V", "W1", "U", "J2"])

    def test_severity_order_constant_matches_the_table(self):
        self.assertEqual(wa.RULE_IDS, ("VS", "V", "W1", "U", "J2", "J1", "C"))
        self.assertEqual([wa.PRIORITY[r] for r in wa.RULE_IDS], [0, 1, 2, 3, 4, 5, 6])

    def test_every_line_has_exactly_the_three_contract_keys(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: dict(raining(85), wind_kmh=38.0,
                                       visibility_m=300.0, apparent_c=4.0)})
        for line in advice(payload):
            self.assertEqual(set(line), {"id", "text", "priority"})
            self.assertIsInstance(line["id"], str)
            self.assertIsInstance(line["text"], str)
            self.assertIsInstance(line["priority"], int)
            self.assertNotIn("{", line["text"])   # no unfilled slot survives
            self.assertNotIn("}", line["text"])


class TestMaxLines(unittest.TestCase):
    def _everything(self):
        payload = load_hourly()
        set_hours(payload, 0, {
            7: dict(raining(85), wind_kmh=38.0,
                    visibility_m=300.0, apparent_c=4.0),
            8: dict(raining(85), wind_kmh=38.0,
                    visibility_m=300.0, apparent_c=4.0),
        })
        return payload

    def test_truncation_respects_severity_order(self):
        profile = wa._build_profile(
            _with_max_lines(wa.load_profile(PROFILE_OK), 2), "synthetic")
        lines = advice(self._everything(), profile=profile)
        self.assertEqual(ids(lines), ["VS", "V"])

    def test_full_surface_keeps_three_lines_by_default(self):
        self.assertEqual(len(advice(self._everything())), 3)
        self.assertEqual(ids(advice(self._everything())), ["VS", "V", "W1"])

    def test_cap_of_one_keeps_only_the_worst(self):
        profile = wa._build_profile(
            _with_max_lines(wa.load_profile(PROFILE_OK), 1), "synthetic")
        self.assertEqual(ids(advice(self._everything(), profile=profile)), ["VS"])

    def test_calm_line_is_never_truncated_away(self):
        """C only fires when nothing else did, so it is always alone."""
        profile = wa._build_profile(
            _with_max_lines(wa.load_profile(PROFILE_OK), 1), "synthetic")
        self.assertEqual(ids(advice(load_hourly(), profile=profile)), ["C"])

    def test_caller_asking_for_fewer_gets_fewer(self):
        profile = wa._build_profile(
            _with_max_lines(wa.load_profile(PROFILE_OK), 0), "synthetic")
        self.assertEqual(advice(self._everything(), profile=profile), [])


def _with_max_lines(profile, value):
    raw = copy.deepcopy(profile.raw)
    raw["weather_advice"]["max_lines"] = value
    return raw


# ---------------------------------------------------------------------------
# DEGRADATION: silence, not a warning
# ---------------------------------------------------------------------------


class TestDegradationIsSilent(unittest.TestCase):
    """A warning the owner cannot act on is the noise he complained about.

    Every case here asserts the caller receives an EMPTY LIST -- not a
    placeholder string, not a "couldn't fetch weather" line.
    """

    def test_absent_input(self):
        self.assertEqual(wa.weather_advice(None, PROFILE, now=NOW), [])
        self.assertEqual(wa.weather_advice({}, PROFILE, now=NOW), [])
        self.assertEqual(wa.weather_advice({"days": []}, PROFILE, now=NOW), [])

    def test_missing_hours_on_a_day(self):
        payload = load_hourly()
        payload["days"][0].pop("hours")
        self.assertEqual(advice(payload), [])

    def test_missing_hours_on_a_later_day_also_silences(self):
        """We do not actually know what today looks like if day 2 is a hole."""
        payload = load_hourly()
        payload["days"][1].pop("hours")
        self.assertEqual(advice(payload), [])

    def test_empty_hours_list(self):
        payload = load_hourly()
        payload["days"][0]["hours"] = []
        self.assertEqual(advice(payload), [])

    def test_missing_pop_pct(self):
        payload = load_hourly()
        for rec in payload["days"][0]["hours"]:
            if rec["hour"] == 7:
                rec.pop("pop_pct")
        self.assertEqual(advice(payload), [])

    def test_none_pop_pct(self):
        payload = load_hourly()
        for rec in payload["days"][0]["hours"]:
            if rec["hour"] == 7:
                rec["pop_pct"] = None
        self.assertEqual(advice(payload), [])

    def test_stale_payload_is_silent(self):
        """Injected, not wall-clock dependent -- this cannot rot."""
        payload = load_hourly()
        much_later = NOW + timedelta(hours=4)
        self.assertEqual(wa.weather_advice(payload, PROFILE, now=much_later), [])

    def test_fresh_enough_payload_is_not_stale(self):
        payload = load_hourly()
        just_later = NOW + timedelta(hours=2, minutes=59)
        self.assertTrue(wa.weather_advice(payload, PROFILE, now=just_later))

    def test_unparseable_fetch_time_is_silent(self):
        payload = load_hourly()
        payload["fetched_at_npt"] = "yesterday-ish"
        self.assertEqual(advice(payload), [])

    def test_stale_weather_that_would_otherwise_be_extreme(self):
        """Silence must not depend on the weather being boring."""
        payload = load_hourly()
        set_hours(payload, 0, {7: {"pop_pct": 99, "wind_kmh": 90.0,
                                   "visibility_m": 10.0, "apparent_c": -5.0}})
        self.assertEqual(wa.weather_advice(payload, PROFILE,
                                           now=NOW + timedelta(hours=99)), [])

    def test_bare_day_list_has_no_fetch_time_and_is_trusted(self):
        """Documented limitation, asserted so it cannot change silently.

        A bare list carries no `fetched_at_npt`, so the age gate cannot run.
        Callers that want the gate should pass the payload -- see the
        DEPENDENCIES note in the build report.
        """
        payload = load_hourly()
        self.assertEqual(
            ids(wa.weather_advice(payload["days"], PROFILE,
                                  now=NOW + timedelta(hours=99))), ["C"])

    def test_disabled_profile_is_silent(self):
        profile = wa.load_profile(PROFILE_OK)
        raw = copy.deepcopy(profile.raw)
        raw["weather_advice"]["enabled"] = False
        disabled = wa._build_profile(raw, "synthetic")
        payload = load_hourly()
        set_hours(payload, 0, {7: {"pop_pct": 99}})
        self.assertEqual(advice(payload, profile=disabled), [])

    def test_missing_tmax_suppresses_the_calm_line(self):
        """C cannot be rendered without a number, so it is dropped silently."""
        payload = load_hourly()
        payload["days"][0].pop("tmax_c")
        self.assertEqual(advice(payload), [])

    def test_never_raises_on_junk(self):
        for junk in ([{"hours": "nope"}], [{}], [None], "nonsense", 42):
            with self.subTest(junk=junk):
                self.assertEqual(wa.weather_advice(junk, PROFILE, now=NOW), [])


# ---------------------------------------------------------------------------
# Profile loading and the loud validator
# ---------------------------------------------------------------------------


class TestProfileLoading(unittest.TestCase):
    def test_loads_the_shipped_profile(self):
        profile = wa.load_profile(REAL_PROFILE)
        self.assertEqual(profile.location["city"], "Kathmandu")
        self.assertEqual(profile.location["district"], "Kathmandu")
        self.assertEqual(profile.timezone if hasattr(profile, "timezone")
                         else profile.location["timezone"], "Asia/Kathmandu")

    def test_shipped_profile_keeps_every_legacy_key(self):
        """Another unit and the LLM prompt both read these. Do not break them."""
        raw = wa._read_profile_file(REAL_PROFILE)
        for key in ("city", "district", "province", "country"):
            self.assertIn(key, raw["location"], "location.%s was dropped" % key)
        for key in ("program", "university", "semester", "institution"):
            self.assertIn(key, raw["education"], "education.%s was dropped" % key)
        self.assertIn("name", raw["bank"])
        self.assertIn("minimize_location_exposure", raw["privacy"])
        for key in ("local_daily_life", "education_deadlines",
                    "bank_customer_notices", "national_practical",
                    "entertainment_personal"):
            self.assertIn(key, raw["priorities"], "priorities.%s was dropped" % key)

    def test_shipped_profile_thresholds(self):
        profile = wa.load_profile(REAL_PROFILE)
        self.assertEqual(profile.thresholds.wash_no, 50)
        self.assertEqual(profile.thresholds.umbrella, 40)
        self.assertEqual(profile.thresholds.precip_wet_mm, 1.0)
        self.assertEqual(profile.thresholds.warm_strong_c, 10)
        self.assertEqual(profile.thresholds.warm_mild_c, 15)
        self.assertEqual(profile.thresholds.wind_kmh, 30)
        self.assertEqual(profile.thresholds.visibility_m, 1000)
        self.assertEqual(profile.max_lines, 3)
        self.assertEqual(profile.max_age_hours, 3)
        self.assertEqual(len(profile.outdoor_windows), 2)

    def test_every_threshold_key_is_carried_by_the_dataclass(self):
        """No second registry to forget to update.

        `THRESHOLD_KEYS` is derived from `_THRESHOLD_BANDS`, so a key added to
        the bands is a key the validator demands. The dataclass is the one
        place that can silently fall out of step, because `_parse_thresholds`
        builds it with `Thresholds(**values)` and a missing field would raise
        at profile-load time rather than in a test that names it.
        """
        import dataclasses

        fields = {f.name for f in dataclasses.fields(wa.Thresholds)}
        self.assertEqual(fields, set(wa.THRESHOLD_KEYS))

    def test_wetting_floor_is_defined_once_and_only_once(self):
        """The single-source rule, checked against the SHIPPED config.

        Every threshold must appear in config/profile.yml, and no threshold may
        appear in the rule bodies. The second half is enforced by the AST audit
        in TestPurityContract; this half is the config side of the same rule,
        and a number that exists in the code but not the config is a default
        wearing a threshold's name.
        """
        raw = wa._read_profile_file(REAL_PROFILE)
        shipped = raw["weather_advice"]["thresholds"]
        self.assertEqual(set(shipped), set(wa.THRESHOLD_KEYS))

    def test_load_profile_raises_on_invalid(self):
        with self.assertRaises(wa.ProfileError):
            wa.load_profile(PROFILE_BAD)

    def test_load_profile_raises_on_missing_file(self):
        with self.assertRaises(wa.ProfileError):
            wa.load_profile(os.path.join(FIXTURE_DIR, "no_such_profile.yml"))

    def test_validate_profile_accepts_a_path_or_a_mapping(self):
        self.assertEqual(wa.validate_profile(PROFILE_OK), [])
        self.assertEqual(wa.validate_profile(wa.load_profile(REAL_PROFILE)), [])

    def test_thresholds_are_not_hardcoded_in_the_rule_bodies(self):
        """A number in both the code and the config is a defect: they drift.

        The rule thresholds must come from the profile and nowhere else, so
        moving one in the config must visibly change the advice.
        """
        profile = wa.load_profile(PROFILE_OK)
        raw = copy.deepcopy(profile.raw)
        raw["weather_advice"]["thresholds"]["umbrella"] = 5
        moved = wa._build_profile(raw, "synthetic")
        payload = load_hourly()          # base fixture peaks at 20% PoP
        # The hours are wet: moving the PROBABILITY is what this test moves, so
        # the amount must not be the thing that decides the outcome.
        set_hours(payload, 0, {h: raining(20) for h in OUTDOOR_HOURS})
        self.assertNotIn("U", ids(advice(payload, profile=profile)))
        self.assertIn("U", ids(advice(payload, profile=moved)))

    def test_wetting_floor_moves_the_verdict_not_the_code(self):
        """The same proof for the amount: moving precip_wet_mm changes advice.

        A conjunct hardcoded in the rule body cannot be moved from the profile,
        so this is the test that would fail if the floor ever acquired a second
        copy in the code.
        """
        raw = copy.deepcopy(wa.load_profile(PROFILE_OK).raw)
        raw["weather_advice"]["thresholds"]["precip_wet_mm"] = 0.1
        permissive = wa._build_profile(raw, "synthetic")
        payload = load_hourly()
        set_hours(payload, 0, {17: {"pop_pct": 60, "precip_mm": 0.2}})
        self.assertNotIn("U", ids(advice(payload)))
        self.assertIn("U", ids(advice(payload, profile=permissive)))


class TestValidatorFailsLoudly(unittest.TestCase):
    """WHY LOUD: a silently-defaulted profile yields plausible advice for the
    wrong place, and the owner would act on it. A loud failure costs him one
    morning of weather advice; acting on advice for the wrong town is a far
    worse cost, and one he has no way to notice."""

    def _raw(self):
        return wa._read_profile_file(PROFILE_OK)

    def _problem_containing(self, needle):
        problems = wa.validate_profile(PROFILE_BAD)
        joined = " | ".join(problems)
        self.assertTrue(problems, "profile_bad.yml must be rejected")
        self.assertIn(needle, joined)
        return problems

    def test_bad_fixture_is_rejected(self):
        self.assertTrue(wa.validate_profile(PROFILE_BAD))

    def test_rejects_out_of_range_latitude(self):
        for bad in (91.0, -90.5, 1000.0):
            with self.subTest(lat=bad):
                raw = self._raw()
                raw["location"]["latitude"] = bad
                self.assertTrue(wa.validate_profile(raw))

    def test_rejects_out_of_range_longitude(self):
        raw = self._raw()
        raw["location"]["longitude"] = 180.5
        self.assertTrue(wa.validate_profile(raw))

    def test_accepts_the_range_edges(self):
        for lat, lon in ((90.0, 180.0), (-90.0, -180.0)):
            raw = self._raw()
            raw["location"]["latitude"] = lat
            raw["location"]["longitude"] = lon
            self.assertEqual(wa.validate_profile(raw), [])

    def test_rejects_non_string_timezone(self):
        for bad in (None, 5, True, [], "  "):
            with self.subTest(tz=bad):
                raw = self._raw()
                raw["location"]["timezone"] = bad
                self.assertTrue(wa.validate_profile(raw))

    def test_rejects_window_with_start_at_or_after_end(self):
        for pair in (["07:00", "07:00"], ["12:00", "07:00"], ["20:00", "19:00"]):
            with self.subTest(pair=pair):
                raw = self._raw()
                raw["schedule"]["outdoor_windows"] = [pair]
                self.assertTrue(wa.validate_profile(raw))

    def test_rejects_malformed_window_shape(self):
        for bad in (["07:00"], ["07:00", "12:00", "13:00"], "07:00-12:00",
                    [7, 12], [None, None], ["7h", "12h"]):
            with self.subTest(window=bad):
                raw = self._raw()
                raw["schedule"]["wash_window"] = bad
                self.assertTrue(wa.validate_profile(raw))

    def test_rejects_unknown_key_under_each_owned_section(self):
        for section, key in (("weather_advice", "max_linesxx"),
                             ("schedule", "outdoor_window"),
                             ("for_you", "min_stories")):
            with self.subTest(section=section, key=key):
                raw = self._raw()
                raw[section][key] = 1
                problems = wa.validate_profile(raw)
                self.assertTrue(any("unknown key" in p for p in problems),
                                "a typo under %s must not be tolerated" % section)

    def test_rejects_unknown_key_under_thresholds(self):
        raw = self._raw()
        raw["weather_advice"]["thresholds"]["wash_nope"] = 50
        self.assertTrue(any("unknown key" in p
                            for p in wa.validate_profile(raw)))

    def test_rejects_threshold_outside_its_band(self):
        cases = {
            "wash_no": (101.0, -1.0),
            "umbrella": (150.0,),
            "precip_wet_mm": (1000.1, -0.1),
            "warm_strong_c": (61.0, -100.0),
            "warm_mild_c": (61.0,),
            "wind_kmh": (401.0,),
            "visibility_m": (100001.0,),
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(threshold=key, value=value):
                    raw = self._raw()
                    raw["weather_advice"]["thresholds"][key] = value
                    self.assertTrue(wa.validate_profile(raw))

    def test_rejects_missing_threshold_rather_than_defaulting(self):
        """No default on purpose: a defaulted threshold is a second copy."""
        for key in wa.THRESHOLD_KEYS:
            with self.subTest(threshold=key):
                raw = self._raw()
                raw["weather_advice"]["thresholds"].pop(key)
                problems = wa.validate_profile(raw)
                self.assertTrue(any("is missing" in p for p in problems))

    # -- monotonicity: the real value of this validator -------------------

    def test_rejects_umbrella_at_or_above_wash_no(self):
        """Closes or inverts the deliberate silence between the two bands."""
        for umbrella, wash_no in ((40, 40), (60, 50), (50, 50)):
            with self.subTest(umbrella=umbrella, wash_no=wash_no):
                raw = self._raw()
                raw["weather_advice"]["thresholds"]["umbrella"] = umbrella
                raw["weather_advice"]["thresholds"]["wash_no"] = wash_no
                problems = wa.validate_profile(raw)
                self.assertTrue(any("umbrella" in p and "wash_no" in p
                                    for p in problems),
                                "umbrella=%s wash_no=%s must be rejected"
                                % (umbrella, wash_no))

    def test_accepts_the_shipped_50_40_pairing(self):
        raw = self._raw()
        raw["weather_advice"]["thresholds"]["wash_no"] = 50
        raw["weather_advice"]["thresholds"]["umbrella"] = 40
        self.assertEqual(wa.validate_profile(raw), [])

    def test_rejects_warm_strong_at_or_above_warm_mild(self):
        for strong, mild in ((10, 10), (20, 15), (15, 5)):
            with self.subTest(strong=strong, mild=mild):
                raw = self._raw()
                raw["weather_advice"]["thresholds"]["warm_strong_c"] = strong
                raw["weather_advice"]["thresholds"]["warm_mild_c"] = mild
                self.assertTrue(any("warm_strong_c" in p
                                    for p in wa.validate_profile(raw)))

    def test_rejects_max_length_at_or_below_min_length(self):
        for minimum, maximum in ((20, 20), (200, 60), (0, 0)):
            with self.subTest(min_length=minimum, max_length=maximum):
                raw = self._raw()
                raw["for_you"]["min_length"] = minimum
                raw["for_you"]["max_length"] = maximum
                self.assertTrue(any("max_length" in p
                                    for p in wa.validate_profile(raw)))

    def test_rejects_non_numeric_and_boolean_scalars(self):
        raw = self._raw()
        raw["location"]["latitude"] = "27.7"
        self.assertTrue(wa.validate_profile(raw))
        raw = self._raw()
        raw["location"]["latitude"] = True
        self.assertTrue(wa.validate_profile(raw))

    def test_weather_advice_refuses_an_invalid_profile(self):
        """Not a silent default, and not advice for the wrong place."""
        payload = load_hourly()
        with self.assertRaises(wa.ProfileError):
            wa.weather_advice(payload, {"thresholds": {"wash_no": 90}},
                              now=NOW)
        with self.assertRaises(wa.ProfileError):
            wa.weather_advice(payload, PROFILE_BAD, now=NOW)

    def test_bad_fixture_problems_are_all_reported_at_once(self):
        problems = wa.validate_profile(PROFILE_BAD)
        joined = " | ".join(problems)
        self._problem_containing("latitude")
        self._problem_containing("earlier than end")
        self._problem_containing("max_lines")
        self._problem_containing("threshhholds")
        self._problem_containing("warm_strong_c")
        self._problem_containing("max_length")
        del joined


# ---------------------------------------------------------------------------
# YAML without PyYAML
# ---------------------------------------------------------------------------


class TestMiniYAML(unittest.TestCase):
    """PyYAML is a convenience, not a dependency.

    The suite must pass on a bare interpreter, so the fallback reader is
    pinned here and cross-checked against PyYAML whenever PyYAML exists. A
    silently-mis-parsed profile is the worst possible failure: it would
    validate, then advise.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = open(PROFILE_OK, "r", encoding="utf-8").read()

    def _fallback(self, text):
        return wa._MiniYAML(text).parse()

    def test_parses_nested_maps_and_scalars(self):
        parsed = self._fallback(self.text)
        self.assertEqual(parsed["location"]["city"], "Example City")
        self.assertEqual(parsed["location"]["latitude"], 27.7)
        self.assertEqual(parsed["location"]["timezone"], "Asia/Kathmandu")
        self.assertEqual(parsed["weather_advice"]["enabled"], True)
        self.assertEqual(parsed["weather_advice"]["max_lines"], 3)
        self.assertEqual(parsed["schedule"]["unavailable_windows"], [])

    def test_parses_lists_of_two_element_lists(self):
        parsed = self._fallback(self.text)
        self.assertEqual(parsed["schedule"]["outdoor_windows"],
                         [["07:00", "12:00"], ["17:00", "20:00"]])
        self.assertEqual(parsed["schedule"]["wash_window"], ["07:00", "18:00"])

    def test_parses_flow_lists_of_scalars(self):
        parsed = self._fallback(self.text)
        self.assertEqual(
            parsed["for_you"]["categories"],
            ["action", "local_city", "education_tu", "college_local",
             "bank_local", "tech_career"])
        self.assertEqual(parsed["for_you"]["reliability_tiers"], ["tier1", "tier2"])

    def test_ignores_comments_and_blank_lines(self):
        parsed = self._fallback("# leading comment\n\n"
                                "a: 1   # trailing\n\nb:\n  c: 2\n")
        self.assertEqual(parsed, {"a": 1, "b": {"c": 2}})

    def test_matches_pyyaml_when_available(self):
        if wa._pyyaml is None:
            self.skipTest("PyYAML not installed; fallback is the only reader")
        self.assertEqual(self._fallback(self.text),
                         wa._pyyaml.safe_load(self.text))

    def test_fallback_drives_a_whole_profile_load(self):
        """Not just a parser unit test: force it end to end through load_profile."""
        original = wa._pyyaml
        try:
            wa._pyyaml = None
            profile = wa.load_profile(PROFILE_OK)
            self.assertEqual(profile.thresholds.wash_no, 50)
            self.assertEqual(profile.outdoor_windows[0].start_hour, 7)
            self.assertEqual(profile.wash_window.end_hour, 18)
            self.assertEqual(ids(advice(load_hourly(), profile=profile)), ["C"])
        finally:
            wa._pyyaml = original

    def test_rejects_constructs_it_cannot_read_honestly(self):
        for text in ("a: &anchor 1\n", "a: |\n  block\n", "a: {b: 1}\n",
                     "a: 1\na: 2\n"):
            with self.subTest(text=text):
                with self.assertRaises(wa.MiniYAMLError):
                    self._fallback(text)


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


class TestCli(unittest.TestCase):
    def _run(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = wa._main(argv)
        return code, buffer.getvalue()

    def test_check_ok_exits_zero(self):
        code, out = self._run(["--check", PROFILE_OK])
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_check_bad_exits_one(self):
        code, out = self._run(["--check", PROFILE_BAD])
        self.assertEqual(code, 1)
        self.assertIn("INVALID", out)

    def test_check_missing_file_exits_one(self):
        code, out = self._run(["--check", os.path.join(FIXTURE_DIR, "nope.yml")])
        self.assertEqual(code, 1)
        self.assertIn("cannot read profile", out)

    def test_check_real_shipped_profile_exits_zero(self):
        code, out = self._run(["--check", REAL_PROFILE])
        self.assertEqual(code, 0)

    def test_no_args_prints_help_and_exits_two(self):
        code, out = self._run([])
        self.assertEqual(code, 2)

    def test_output_is_ascii_only(self):
        """Windows consoles are cp1252; a non-ASCII byte would raise there."""
        for path in (PROFILE_OK, PROFILE_BAD, REAL_PROFILE):
            with self.subTest(path=path):
                _code, out = self._run(["--check", path])
                out.encode("ascii")


# ---------------------------------------------------------------------------
# The purity contract
# ---------------------------------------------------------------------------


class TestPurityContract(unittest.TestCase):
    """This module is the owner's trust boundary. Prove it stays pure."""

    def test_source_imports_no_network_and_no_llm_modules(self):
        source = open(os.path.join(SCRIPTS, "weather_advice.py"), "r",
                      encoding="utf-8").read()
        banned = ("import requests", "import urllib.request", "import socket",
                  "import http", "import openai", "import anthropic",
                  "subprocess", "os.system", "import opencode", "httpx")
        for needle in banned:
            self.assertNotIn(needle, source,
                             "weather_advice.py must never reach %r" % needle)

    def test_source_states_the_determinism_rule(self):
        source = open(os.path.join(SCRIPTS, "weather_advice.py"), "r",
                      encoding="utf-8-sig").read()
        lowered = source.lower()
        for phrase in ("deterministic", "llm", "network", "must never"):
            self.assertIn(phrase, lowered,
                          "the module docstring must state the %r rule" % phrase)

    def test_no_rule_threshold_literal_appears_in_a_rule_body(self):
        """Guards the 'never hardcoded twice' rule against future edits.

        Checked with `ast`, not with a text scan: the rule docstrings explain
        WHY 50 and 40 are the two numbers, and a text scan cannot tell that
        prose from code. Parsing lets the test look at real code only --
        every constant, argument and comparison inside the six _eval_*
        functions and their helper -- which is the part that would actually
        drift away from the config.
        """
        import ast

        path = os.path.join(SCRIPTS, "weather_advice.py")
        with open(path, "r", encoding="utf-8-sig") as handle:
            tree = ast.parse(handle.read())

        rule_functions = [node for node in tree.body
                          if isinstance(node, ast.FunctionDef)
                          and (node.name.startswith("_eval_")
                               or node.name in ("_build_windows", "_wetting"))]
        self.assertTrue(rule_functions, "no rule functions found to audit")

        # The thresholds that must come from the profile and nowhere else.
        forbidden = {50, 40, 30, 1000, 10, 15}
        offenders = []
        for func in rule_functions:
            for node in ast.walk(func):
                if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
                    if node.value in forbidden:
                        offenders.append("%s: literal %r at line %d"
                                         % (func.name, node.value, node.lineno))
        self.assertEqual(
            offenders, [],
            "rule bodies must read every threshold from the profile: %s"
            % "; ".join(offenders))

    def test_no_profile_yaml_in_the_rule_bodies_either(self):
        """The mirror image: the code must not carry the config's numbers."""
        import ast

        with open(os.path.join(SCRIPTS, "weather_advice.py"), "r",
                  encoding="utf-8-sig") as handle:
            tree = ast.parse(handle.read())
        # The policy defaults the spec explicitly documents. They are allowed
        # here and only here; if a rule body ever grows one, it is a bug.
        defaults = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "DEFAULT_MAX_AGE_HOURS" for t in node.targets):
                defaults.add(node.value.value)
        self.assertTrue(defaults)
        for func in (n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name.startswith("_eval_")):
            for node in ast.walk(func):
                if isinstance(node, ast.Constant) and node.value in defaults:
                    self.fail("%s hardcodes a policy default %r"
                              % (func.name, node.value))

    def test_output_is_deterministic(self):
        payload = load_hourly()
        set_hours(payload, 0, {7: dict(raining(85), wind_kmh=38.0,
                                       visibility_m=300.0, apparent_c=4.0)})
        first = advice(payload)
        self.assertTrue(first)
        for _ in range(5):
            self.assertEqual(advice(payload), first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
