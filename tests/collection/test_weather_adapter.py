#!/usr/bin/env python3
"""tests/collection/test_weather_adapter.py -- the open-meteo ADAPTER.

Covers the network-facing half of the weather feature: the URL builder, the
normalizer that turns an open-meteo response into the schema
scripts/weather_advice.py consumes, the deterministic advice call, the
`weather_advice` key in candidates.json, and every documented failure path.

NO NETWORK IS TOUCHED ANYWHERE HERE. collect_news.fetch_bytes -- the single
network injection point -- is monkeypatched in every test that exercises the
collector, and the response bodies are captured fixtures. The fixtures use the
field names the LIVE endpoint actually returned, verified by a real GET
(HTTP 200, application/json):

    hourly: precipitation_probability, precipitation, apparent_temperature,
            temperature_2m, wind_speed_10m, visibility, time
    hourly_units: %, mm, degC, degC, km/h, m, iso8601
    all 72 long for forecast_days=3; utc_offset_seconds 20700 (+05:45)

Run:
    py -m unittest discover -s tests\\collection -t .
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import collect_news as cn  # noqa: E402
import weather_advice as wa  # noqa: E402

REPO_PROFILE = os.path.join(REPO_ROOT, "config", "profile.yml")
REPO_INTERESTS = os.path.join(REPO_ROOT, "config", "interests.yml")
REPO_COLLECTION_CFG = os.path.join(REPO_ROOT, "config", "collection.json")

# The date every fixed fixture is built around. Tests pass this to the adapter
# explicitly as `fetched_at_npt`, so nothing here depends on the real clock.
DAY0 = "2026-09-28"
DAYS = ["2026-09-28", "2026-09-29", "2026-09-30"]
NPT0 = "2026-09-28 06:12"


def hourly_body(dates=None, **overrides):
    """A captured open-meteo response, 24 hours per date.

    `overrides` replaces a whole hourly array, which is how a length mismatch
    or a missing field is expressed.
    """
    dates = dates or DAYS
    count = 24 * len(dates)
    hourly = {
        "time": ["%sT%02d:00" % (day, h)
                 for day in dates for h in range(24)],
        "precipitation_probability": [0] * count,
        "precipitation": [0.0] * count,
        "temperature_2m": [19.0] * count,
        "apparent_temperature": [18.6] * count,
        "wind_speed_10m": [1.5] * count,
        "visibility": [9420.0] * count,
    }
    hourly.update(overrides)
    return {
        "latitude": 27.592266, "longitude": 85.56053, "elevation": 1462.0,
        "timezone": "Asia/Kathmandu", "timezone_abbreviation": "GMT+5:45",
        "utc_offset_seconds": 20700,
        "hourly_units": {
            "time": "iso8601", "precipitation_probability": "%",
            "precipitation": "mm", "temperature_2m": "\u00b0C",
            "apparent_temperature": "\u00b0C", "wind_speed_10m": "km/h",
            "visibility": "m"},
        "hourly": hourly,
        "daily_units": {
            "time": "iso8601", "weathercode": "wmo code",
            "temperature_2m_max": "\u00b0C", "temperature_2m_min": "\u00b0C",
            "precipitation_sum": "mm"},
        "daily": {
            "time": list(dates),
            "temperature_2m_max": [26.1] * len(dates),
            "temperature_2m_min": [12.6] * len(dates),
            "precipitation_sum": [1.0] * len(dates),
            "weathercode": [51] * len(dates)},
    }


def wet_body(dates=None, start=7, end=20, pop=70, apparent=8.0, mm=2.4):
    """A body where the outdoor + wash hours are wet AND cold.

    The engine turns this into W1 (don't wash) + U (umbrella) + J2 (warm
    jacket), which is the owner's actual ask in one fixture.

    WET means both fields now, not just `pop_pct`. The rain rules require a
    probability AND an amount in the same hour (`precip_wet_mm` in the
    profile), so a body with `precipitation` left at 0.0 is a dry body with a
    high chance of rain -- which is precisely the shape the engine is supposed
    to refuse. `mm` defaults to 2.4 mm, comfortably over the shipped 1.0 mm
    floor, and is a plain argument so a test can drop it to the floor to check
    the boundary. `profile_path()` below derives from config/profile.yml, so a
    change to the real floor is felt here rather than papered over.
    """
    dates = dates or DAYS
    count = 24 * len(dates)
    pops = [0] * count
    appar = [18.6] * count
    mms = [0.0] * count
    for h in range(start, end):
        if h < 24:                      # first day only
            pops[h] = pop
            appar[h] = apparent
            mms[h] = mm
    return hourly_body(dates, precipitation_probability=pops,
                       apparent_temperature=appar, precipitation=mms)


def profile_path(tmpdir, unavailable=None, mutate=None, name="profile.yml"):
    """Write a profile derived from the shipped one, optionally patched.

    Deriving rather than hand-writing keeps these tests honest about Unit 1's
    schema: a fixture that invented its own thresholds would keep passing after
    the real config changed shape.
    """
    with open(REPO_PROFILE, "r", encoding="utf-8") as handle:
        text = handle.read()
    if unavailable is not None:
        # json.dumps of the list-of-pairs IS the whole flow sequence already.
        rendered = json.dumps([["%02d:00" % s, "%02d:00" % e]
                               for s, e in unavailable]).replace('"', "'")
        text, hits = re.subn(r"unavailable_windows:.*$",
                             "unavailable_windows: %s" % rendered,
                             text, count=1, flags=re.MULTILINE)
        if not hits:
            raise AssertionError("profile.yml has no unavailable_windows line "
                                 "to patch; this fixture needs updating")
    if mutate is not None:
        text = mutate(text)
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def rss_doc(items=1, hours_ago=1):
    """A one-item-per-feed RSS body whose pubDate is RELATIVE TO THE CLOCK.

    `hours_ago` (default 1) is measured back from `datetime.now()`, so the
    fixture is ~1h old whenever the test runs and can never age past
    collect_news.FRESHNESS_HOURS (36h) and get discarded as stale. A hardcoded
    calendar date here was a real time bomb: postprocess() compares every
    candidate's `_dt` against `datetime.now(timezone.utc) - FRESHNESS_HOURS`,
    so once the literal fell 36h in the past the collector correctly dropped
    the item and the "news is still collected" assertion saw zero candidates.
    The freshness rule is right; the fixture was wrong.

    format_datetime() rather than strftime("%a, %d %b %Y") because %a/%b are
    locale-dependent and would emit non-English day/month names on a
    non-English Windows locale -- which parsedate_to_datetime cannot read.
    """
    stamp = format_datetime(
        datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    body = "".join(
        "<item><title>Headline %d</title><link>https://example.com/%d</link>"
        "<pubDate>%s</pubDate>"
        "<source url=\"https://example.com/\">Example</source></item>"
        % (i, i, stamp) for i in range(items))
    return ("<?xml version='1.0' encoding='UTF-8'?><rss><channel>%s</channel>"
            "</rss>" % body).encode("utf-8")


class WeatherFixture(object):
    """Install a fake fetch_bytes and run the collector end to end."""

    def __init__(self, weather, tmpdir):
        self.weather = weather
        self.tmpdir = tmpdir

    def __enter__(self):
        def fake(url, timeout, max_bytes):
            if "open-meteo" in url:
                if isinstance(self.weather, BaseException):
                    raise self.weather
                return self.weather
            return rss_doc(1)

        self.saved = cn.fetch_bytes
        cn.fetch_bytes = fake
        return self

    def __exit__(self, *exc):
        cn.fetch_bytes = self.saved
        return False

    def run(self, profile=None, extra_args=()):
        out = os.path.join(self.tmpdir, "candidates.json")
        args = ["--out", out,
                "--timings", os.path.join(self.tmpdir, "timings.json"),
                "--interests", REPO_INTERESTS,
                "--collection-config", REPO_COLLECTION_CFG,
                "--profile", profile or REPO_PROFILE,
                "--timeout", "1"] + list(extra_args)
        buf = io.StringIO()
        stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = cn.main(args)
        finally:
            sys.stdout = stdout
        with open(out, "r", encoding="utf-8") as handle:
            return rc, json.load(handle), buf.getvalue()


# ---------------------------------------------------------------------------


class TestWeatherUrlBuilder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wx-url-")
        self.profile, err = cn.load_weather_profile(REPO_PROFILE)
        self.assertEqual(err, "")
        self.assertIsNotNone(self.profile)

    def test_weather_url_literal_is_unchanged(self):
        """breaking_monitor.py imports this symbol. Freeze it."""
        self.assertEqual(
            cn.WEATHER_URL,
            "https://api.open-meteo.com/v1/forecast?latitude=27.7"
            "&longitude=85.3&daily=temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,weathercode&timezone=Asia%2FKathmandu")
        # And it must still be the DEFAULT: no profile -> no hourly request.
        self.assertEqual(cn.build_weather_url(), cn.WEATHER_URL)
        self.assertEqual(cn.build_weather_url(None), cn.WEATHER_URL)
        self.assertNotIn("hourly=", cn.WEATHER_URL)

    def test_url_carries_every_hourly_field_the_probe_returned(self):
        url = cn.build_weather_url(self.profile)
        self.assertIn("hourly=", url)
        for name in ("precipitation_probability", "apparent_temperature",
                     "wind_speed_10m", "visibility"):
            self.assertIn(name, url, name)
        self.assertIn("latitude=27.7", url)
        self.assertIn("longitude=85.3", url)
        self.assertIn("timezone=Asia%2FKathmandu", url)   # percent-encoded
        self.assertIn("forecast_days=3", url)
        # daily rides along in the SAME request, so the plan count is unchanged
        self.assertIn("daily=temperature_2m_max", url)

    def test_a_profile_without_coordinates_falls_back_rather_than_invents(self):
        for location in ({}, {"city": "Example City"}, {"latitude": "12.34"},
                         {"latitude": 12.34, "longitude": 56.78},
                         {"latitude": 12.34, "longitude": 56.78,
                          "timezone": ""}):
            fake = type("P", (), {"location": location})()
            self.assertEqual(cn.build_weather_url(fake), cn.WEATHER_URL,
                             location)

    def test_plan_builder_keeps_the_historical_url_without_a_profile(self):
        plans = cn.build_all_plans({}, cn.load_collection_config(
            REPO_COLLECTION_CFG), None)
        weather = [p for p in plans if p["name"] == "weather:open-meteo"]
        self.assertEqual(len(weather), 1)
        self.assertEqual(weather[0]["url"], cn.WEATHER_URL)
        # Still exactly one weather plan: the hourly request is not a new one.
        self.assertEqual(len([p for p in plans if p["kind"] == "weather"]), 1)

    def test_plan_builder_uses_the_hourly_url_with_a_profile(self):
        box = cn.new_weather_box(self.profile, NPT0)
        plans = cn.build_all_plans({}, cn.load_collection_config(
            REPO_COLLECTION_CFG), None, profile=self.profile, weather_box=box)
        weather = [p for p in plans if p["name"] == "weather:open-meteo"][0]
        self.assertEqual(weather["url"], cn.build_weather_url(self.profile))
        self.assertIs(weather["weather"], box)
        self.assertEqual(weather["host"], "api.open-meteo.com")
        # Budget-critical constants are NOT touched by this feature.
        self.assertEqual(cn.MAX_WORKERS, 8)
        self.assertEqual(cn.SOFT_BUDGET_SEC, 75.0)


class TestDailyOnlyPathStillWorks(unittest.TestCase):
    """The breaking-monitor contract: daily-only parse, byte-for-byte."""

    def test_daily_only_body_yields_the_historical_candidate_and_summary(self):
        daily_only = json.dumps({
            "daily": {"time": ["2026-09-27", "2026-09-28"],
                      "temperature_2m_max": [28.0, 27.0],
                      "temperature_2m_min": [20.0, 19.0],
                      "precipitation_sum": [0.0, 2.0],
                      "weathercode": [0, 61]}}).encode("utf-8")
        saved = cn.fetch_bytes
        cn.fetch_bytes = lambda url, timeout, max_bytes: daily_only
        try:
            candidates, summary = cn.weather_task(cn.WEATHER_URL, 1)
        finally:
            cn.fetch_bytes = saved
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["domain_category"], "weather_forecast")
        self.assertEqual(candidates[0]["source"], "open-meteo.com")
        self.assertIn("2026-09-27: max +28", summary)
        self.assertIn("2026-09-28: max +27", summary)
        self.assertIn("slight rain", summary)
        # No box was passed, so nothing about advice changed here.
        self.assertEqual(
            set(candidates[0]),
            {"title", "url", "source", "published", "snippet",
             "domain_category", "language", "_dt"})

    def test_daily_only_body_records_the_advice_lane_as_not_derivable(self):
        daily_only = json.dumps({"daily": {"time": ["2026-09-28"],
                                           "temperature_2m_max": [27.0],
                                           "temperature_2m_min": [19.0],
                                           "precipitation_sum": [2.0],
                                           "weathercode": [61]}}
                                ).encode("utf-8")
        box = cn.new_weather_box(None, NPT0)
        saved = cn.fetch_bytes
        cn.fetch_bytes = lambda url, timeout, max_bytes: daily_only
        try:
            candidates, _summary = cn.weather_task(cn.WEATHER_URL, 1, None,
                                                   None, None, box)
        finally:
            cn.fetch_bytes = saved
        self.assertEqual(len(candidates), 1)      # daily content preserved
        self.assertIsNone(box["payload"])
        self.assertIn("no hourly block", box["error"])


class TestNormalizer(unittest.TestCase):
    def setUp(self):
        self.profile, _ = cn.load_weather_profile(REPO_PROFILE)
        self.place = cn.weather_place(self.profile)

    def norm(self, payload, fetched=NPT0, max_days=3):
        return cn.normalize_hourly_forecast(payload, self.place, fetched,
                                            max_days=max_days)

    def test_maps_the_real_response_shape(self):
        payload, err = self.norm(hourly_body())
        self.assertEqual(err, "")
        self.assertEqual(payload["fetched_at_npt"], NPT0)
        self.assertEqual([d["date"] for d in payload["days"]], DAYS)
        for day in payload["days"]:
            self.assertEqual(len(day["hours"]), 24)
            self.assertEqual(day["tmax_c"], 26.1)
            self.assertEqual(day["tmin_c"], 12.6)
            self.assertEqual(day["precip_sum_mm"], 1.0)
            self.assertEqual(day["weathercode"], 51)
        first = payload["days"][0]["hours"][7]
        self.assertEqual(set(first),
                         {"hour", "pop_pct", "precip_mm", "apparent_c",
                          "wind_kmh", "visibility_m"})
        self.assertEqual(first["hour"], 7)
        self.assertEqual(first["apparent_c"], 18.6)
        self.assertEqual(first["wind_kmh"], 1.5)
        self.assertEqual(first["visibility_m"], 9420.0)
        # hours are ordered and cover 0..23 exactly
        self.assertEqual([h["hour"] for h in payload["days"][0]["hours"]],
                         list(range(24)))

    def test_place_comes_from_the_profile_not_the_grid_cell(self):
        """open-meteo echoes a slightly different cell for the requested point."""
        payload, _ = self.norm(hourly_body())
        self.assertEqual(payload["place"],
                         {"city": "Kathmandu", "latitude": 27.7,
                          "longitude": 85.3, "timezone": "Asia/Kathmandu"})

    def test_max_days_is_respected(self):
        payload, _ = self.norm(hourly_body(), max_days=1)
        self.assertEqual(len(payload["days"]), 1)
        self.assertEqual(payload["days"][0]["date"], DAY0)

    def test_incomplete_day_is_dropped_and_today_still_advises(self):
        """The deliberate choice: drop the hole, do not pass it on."""
        times, pops = [], []
        for day in DAYS:
            for h in range(24):
                times.append("%sT%02d:00" % (day, h))
                pops.append(0)
        # Day 2 loses hour 13 entirely by truncating that day's samples:
        # emulate it with a None pop, which the adapter must treat as a hole.
        pops[24 + 13] = None
        payload, err = self.norm(hourly_body(
            precipitation_probability=pops))
        self.assertEqual(err, "")
        self.assertEqual([d["date"] for d in payload["days"]],
                         [DAYS[0], DAYS[2]])

    def test_today_incomplete_means_no_advice_and_an_explicit_error(self):
        pops = [0] * 72
        pops[13] = None                     # a hole in TODAY
        payload, err = self.norm(hourly_body(precipitation_probability=pops))
        self.assertIsNone(payload)
        self.assertIn("incomplete", err)
        self.assertIn(DAY0, err)

    def test_a_missing_today_is_refused_rather_than_advising_tomorrow(self):
        payload, err = self.norm(hourly_body(), fetched="2026-10-05 06:12")
        self.assertIsNone(payload)
        self.assertIn("incomplete or absent", err)
        self.assertIn("2026-10-05", err)

    def test_days_before_today_are_dropped(self):
        payload, err = self.norm(hourly_body(
            dates=["2026-09-26", "2026-09-27", "2026-09-28"]))
        self.assertEqual(err, "")
        self.assertEqual([d["date"] for d in payload["days"]], [DAY0])

    def test_short_optional_array_is_truncated_and_never_indexed_past_end(self):
        # visibility stops after hour 19 of today. It is OPTIONAL, so it must
        # cost rule VS and nothing else: pop, apparent temperature and the
        # remaining hours survive intact.
        payload, err = self.norm(hourly_body(visibility=[9420.0] * 20))
        self.assertEqual(err, "")
        hours = payload["days"][0]["hours"]
        self.assertEqual(hours[7]["visibility_m"], 9420.0)
        self.assertEqual(hours[19]["visibility_m"], 9420.0)
        self.assertIsNone(hours[20]["visibility_m"])
        self.assertIsNone(hours[23]["visibility_m"])
        self.assertEqual(hours[23]["pop_pct"], 0)
        self.assertEqual(hours[23]["apparent_c"], 18.6)
        self.assertEqual(len(hours), 24)

    def test_short_required_array_makes_today_incomplete(self):
        # A truncated precipitation_probability is a real hole in the rain
        # probability, and a hole in today means no advice at all -- not advice
        # silently advanced to tomorrow.
        payload, err = self.norm(hourly_body(
            precipitation_probability=[0] * 10))
        self.assertIsNone(payload)
        self.assertIn("incomplete", err)
        self.assertIn(DAY0, err)

    def test_a_hole_in_today_does_not_advance_the_advice_to_tomorrow(self):
        """days[0] must be today even when tomorrow is complete."""
        pops = [0] * 72
        pops[13] = None                     # a hole in TODAY, hours 13
        payload, err = self.norm(hourly_body(precipitation_probability=pops))
        self.assertIsNone(payload)
        self.assertIn(DAY0, err)
        self.assertNotIn("2026-09-29", str(payload))

    def test_missing_visibility_key_still_normalizes(self):
        body = hourly_body()
        del body["hourly"]["visibility"]
        payload, err = self.norm(body)
        self.assertEqual(err, "")
        self.assertIsNone(payload["days"][0]["hours"][7]["visibility_m"])

    def test_missing_apparent_temperature_is_refused_with_a_reason(self):
        body = hourly_body()
        del body["hourly"]["apparent_temperature"]
        payload, err = self.norm(body)
        self.assertIsNone(payload)
        self.assertIn("apparent_temperature", err)

    def test_no_hourly_block_is_refused(self):
        payload, err = self.norm({"daily": {"time": [DAY0]}})
        self.assertIsNone(payload)
        self.assertIn("no hourly block", err)

    def test_junk_payload_never_raises(self):
        for junk in (None, [], "x", 7, {"hourly": []}, {"hourly": {}},
                     {"hourly": {"time": ["nonsense"]}}):
            payload, err = self.norm(junk)
            self.assertIsNone(payload, junk)
            self.assertTrue(err, junk)

    def test_unknown_fetch_time_is_refused(self):
        payload, err = self.norm(hourly_body(), fetched="")
        self.assertIsNone(payload)
        self.assertIn("fetch time unknown", err)


class TestAdviceCall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wx-advice-")
        self.profile, _ = cn.load_weather_profile(REPO_PROFILE)

    def box(self, body, fetched=NPT0):
        payload, err = cn.normalize_hourly_forecast(
            body, cn.weather_place(self.profile), fetched)
        return {"payload": payload, "error": err, "place":
                cn.weather_place(self.profile), "fetched_at_npt": fetched}

    def test_lines_are_the_engine_output_verbatim(self):
        box = self.box(wet_body())
        lines, err = cn.compute_weather_advice(box, self.profile, now=NPT0)
        self.assertEqual(err, "")
        self.assertEqual([l["id"] for l in lines], ["W1", "U", "J2"])
        for line in lines:
            self.assertEqual(set(line), {"id", "text", "priority"})
            self.assertIsInstance(line["text"], str)
            self.assertIsInstance(line["priority"], int)
        self.assertEqual(
            lines[0]["text"],
            "Don't wash clothes today - 70% chance of rain 07:00-17:59")
        # Priorities are non-decreasing: the cap sheds the least severe line.
        self.assertEqual([l["priority"] for l in lines],
                         sorted(l["priority"] for l in lines))

    def test_a_response_without_visibility_still_yields_advice(self):
        body = wet_body()
        del body["hourly"]["visibility"]
        lines, err = cn.compute_weather_advice(self.box(body), self.profile,
                                               now=NPT0)
        self.assertEqual(err, "")
        self.assertEqual([l["id"] for l in lines], ["W1", "U", "J2"])
        self.assertNotIn("VS", [l["id"] for l in lines])

    def test_missing_apparent_temperature_degrades_to_empty_not_an_exception(self):
        body = wet_body()
        del body["hourly"]["apparent_temperature"]
        box = self.box(body)
        self.assertIsNone(box["payload"])
        lines, err = cn.compute_weather_advice(box, self.profile, now=NPT0)
        self.assertEqual(lines, [])
        self.assertIn("apparent_temperature", err)

    def test_stale_payload_is_silence_not_a_warning(self):
        lines, err = cn.compute_weather_advice(self.box(wet_body()),
                                               self.profile,
                                               now="2026-09-28 20:00")
        self.assertEqual(lines, [])       # older than max_age_hours=3
        self.assertEqual(err, "")

    def test_disabled_by_profile_is_not_a_failure(self):
        path = profile_path(self.tmp, mutate=lambda t: t.replace(
            "  enabled: true\n  max_age_hours: 3",
            "  enabled: false\n  max_age_hours: 3"), name="off.yml")
        profile, perr = cn.load_weather_profile(path)
        self.assertEqual(perr, "")
        lines, err = cn.compute_weather_advice(self.box(wet_body()), profile,
                                               now=NPT0)
        self.assertEqual(lines, [])
        self.assertEqual(err, "")          # deliberate, so NOT recorded

    def test_blockers_come_only_from_unavailable_windows(self):
        self.assertEqual(cn.advice_blockers(self.profile), [])
        path = profile_path(self.tmp, unavailable=[(10, 12)],
                            name="blocked.yml")
        profile, perr = cn.load_weather_profile(path)
        self.assertEqual(perr, "")
        self.assertEqual(cn.advice_blockers(profile), [(10, 12, "unavailable")])
        self.assertEqual(cn.advice_blockers(None), [])

    def test_a_blocker_downgrades_w1_to_w1_blocked(self):
        blocked, _ = cn.load_weather_profile(
            profile_path(self.tmp, unavailable=[(10, 12)], name="b.yml"))
        # Unblocked: a prohibition.
        lines, _ = cn.compute_weather_advice(self.box(wet_body()), self.profile,
                                             now=NPT0)
        self.assertIn("W1", [l["id"] for l in lines])
        # Blocked: postponed, never forbidden. THE SERIOUS GUARD.
        lines, err = cn.compute_weather_advice(self.box(wet_body()), blocked,
                                               now=NPT0)
        self.assertEqual(err, "")
        self.assertIn("W1-blocked", [l["id"] for l in lines])
        self.assertNotIn("W1", [l["id"] for l in lines])
        self.assertEqual(
            [l for l in lines if l["id"] == "W1-blocked"][0]["text"],
            "Wash after 07:00-17:59 - 70% chance of rain before then")
        # The rain line is unaffected: only the washing verdict is downgraded.
        self.assertIn("U", [l["id"] for l in lines])

    def test_a_blocker_outside_the_qualifying_run_changes_nothing(self):
        # The W1 run is 07:00-17:59, so a 21:00-23:00 commitment does not
        # overlap it and the verdict must stay a plain prohibition.
        late, _ = cn.load_weather_profile(
            profile_path(self.tmp, unavailable=[(21, 23)], name="late.yml"))
        self.assertEqual(
            cn.compute_weather_advice(self.box(wet_body()), late, now=NPT0)[0],
            cn.compute_weather_advice(self.box(wet_body()), self.profile,
                                      now=NPT0)[0])


class TestCollectorEmission(unittest.TestCase):
    """main() with fetch_bytes patched: no network, no tracked paths."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wx-e2e-")

    def test_candidates_always_contains_weather_advice(self):
        """The key that went missing once, silently blinding the digest."""
        with WeatherFixture(json.dumps(wet_body(
            [cn.kathmandu_now_str()[:10]])).encode("utf-8"),
                            self.tmp) as fx:
            rc, payload, _log = fx.run()
        self.assertEqual(rc, 0)
        self.assertIn("weather_advice", payload)
        self.assertIsInstance(payload["weather_advice"], list)

    def test_key_is_present_even_when_the_advice_is_empty(self):
        # A calm, dry day: the engine legitimately says one calm line. Now
        # force a genuinely empty result AND a failure, and the key must
        # still be there -- that is the whole regression this pins.
        with WeatherFixture(b"not json at all", self.tmp) as fx:
            rc, payload, _log = fx.run()
        self.assertEqual(rc, 0)
        self.assertIn("weather_advice", payload)
        self.assertEqual(payload["weather_advice"], [])
        # ...and the absence is RECORDED, not passed off as "nothing to say".
        rows = {r["name"]: r for r in payload["source_results"]}
        self.assertIn("weather_advice:open-meteo", rows)
        self.assertEqual(rows["weather_advice:open-meteo"]["outcome"], "failed")
        self.assertNotEqual(rows["weather_advice:open-meteo"]["error"], "")

    def test_a_real_forecast_produces_lines_and_a_silent_source_record(self):
        day = cn.kathmandu_now_str()[:10]
        with WeatherFixture(json.dumps(wet_body([day])).encode("utf-8"),
                            self.tmp) as fx:
            rc, payload, log = fx.run()
        self.assertEqual(rc, 0)
        lines = payload["weather_advice"]
        self.assertTrue(lines, "a wet day must produce advice")
        for line in lines:
            self.assertEqual(set(line), {"id", "text", "priority"})
        rows = {r["name"]: r for r in payload["source_results"]}
        self.assertEqual(rows["weather:open-meteo"]["outcome"], "ok")
        # Nothing failed, so no advice row is invented.
        self.assertNotIn("weather_advice:open-meteo", rows)
        self.assertNotIn("weather_advice:open-meteo", payload["sources_failed"])
        self.assertEqual(rows["weather:open-meteo"]["kind"], "weather")

    def test_open_meteo_unreachable_writes_empty_advice_and_exits_zero(self):
        with WeatherFixture(wa.ProfileError("simulated outage"), self.tmp) as fx:
            rc, payload, log = fx.run()
        self.assertEqual(rc, 0, "weather must never take down the digest")
        self.assertEqual(payload["weather_advice"], [])
        self.assertTrue(payload["candidates"], "news still collected")
        rows = {r["name"]: r for r in payload["source_results"]}
        self.assertEqual(rows["weather:open-meteo"]["outcome"], "failed")
        self.assertEqual(rows["weather_advice:open-meteo"]["outcome"], "failed")
        # The truth record stays internally consistent.
        self.assertEqual(
            len(payload["source_results"]),
            len(payload["sources_ok"]) + len(payload["sources_failed"]))
        with open(os.path.join(self.tmp, "timings.json"), "r",
                  encoding="utf-8") as handle:
            by_source = json.load(handle)["by_source"]
        self.assertIn("weather_advice:open-meteo", by_source)
        self.assertEqual(len(by_source), len(payload["source_results"]))

    def test_a_malformed_profile_does_not_stop_the_run(self):
        path = profile_path(self.tmp, mutate=lambda t: t.replace(
            "    wash_no: 50", "    wash_no: 10"), name="bad.yml")
        # Unit 1 refuses it loudly, with no silent default.
        profile, err = cn.load_weather_profile(path)
        self.assertIsNone(profile)
        self.assertIn("umbrella", err)
        self.assertIn("BELOW wash_no", err)

        with WeatherFixture(json.dumps(hourly_body(
            [cn.kathmandu_now_str()[:10]])).encode("utf-8"),
                            self.tmp) as fx:
            rc, payload, log = fx.run(profile=path)
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", log)
        self.assertEqual(payload["weather_advice"], [])
        self.assertTrue(payload["candidates"])
        rows = {r["name"]: r for r in payload["source_results"]}
        self.assertEqual(rows["weather:open-meteo"]["outcome"], "ok")
        self.assertEqual(rows["weather_advice:open-meteo"]["outcome"], "failed")
        self.assertIn("profile", rows["weather_advice:open-meteo"]["error"])

    def test_a_broken_profile_keeps_the_daily_weather_candidate(self):
        """Degrading the advice must not cost the older daily summary."""
        path = profile_path(self.tmp, mutate=lambda t: t.replace(
            "    wash_no: 50", "    wash_no: 10"), name="bad2.yml")
        with WeatherFixture(json.dumps(hourly_body(
            [cn.kathmandu_now_str()[:10]])).encode("utf-8"),
                            self.tmp) as fx:
            rc, payload, _log = fx.run(profile=path)
        self.assertEqual(rc, 0)
        self.assertTrue(payload["weather_summary"])
        self.assertIn("weather_forecast", [c["domain_category"]
                                           for c in payload["candidates"]])
        self.assertEqual(payload["weather_advice"], [])

    def test_a_missing_profile_file_is_survivable(self):
        with WeatherFixture(json.dumps(hourly_body(
            [cn.kathmandu_now_str()[:10]])).encode("utf-8"),
                            self.tmp) as fx:
            rc, payload, log = fx.run(
                profile=os.path.join(self.tmp, "does-not-exist.yml"))
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", log)
        self.assertEqual(payload["weather_advice"], [])

    def test_blockers_reach_the_engine_through_the_whole_collector(self):
        path = profile_path(self.tmp, unavailable=[(10, 12)], name="blk.yml")
        day = cn.kathmandu_now_str()[:10]
        with WeatherFixture(json.dumps(wet_body([day])).encode("utf-8"),
                            self.tmp) as fx:
            rc, payload, _log = fx.run(profile=path)
        self.assertEqual(rc, 0)
        ids = [l["id"] for l in payload["weather_advice"]]
        self.assertIn("W1-blocked", ids)
        self.assertNotIn("W1", ids)

    def test_no_second_network_call_is_made_for_the_advice(self):
        """One fetch, one open-meteo request: the budget does not move."""
        day = cn.kathmandu_now_str()[:10]
        calls = []
        saved = cn.fetch_bytes

        def counting(url, timeout, max_bytes):
            if "open-meteo" in url:
                calls.append(url)
                return json.dumps(wet_body([day])).encode("utf-8")
            return rss_doc(1)

        cn.fetch_bytes = counting
        try:
            buf = io.StringIO()
            stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = cn.main([
                    "--out", os.path.join(self.tmp, "c.json"),
                    "--timings", os.path.join(self.tmp, "t.json"),
                    "--interests", REPO_INTERESTS,
                    "--collection-config", REPO_COLLECTION_CFG,
                    "--timeout", "1"])
            finally:
                sys.stdout = stdout
        finally:
            cn.fetch_bytes = saved
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("hourly=", calls[0])

    def test_kathmandu_now_str_is_delegated_not_reimplemented(self):
        """The +05:45 shift is not hand-rolled a fifth time."""
        stamp = cn.kathmandu_now_str()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        import validate_stories
        self.assertEqual(stamp, validate_stories.kathmandu_now_str())


class TestRecordWeatherAdvice(unittest.TestCase):
    def test_nothing_to_report_changes_nothing(self):
        rows = [{"name": "weather:open-meteo", "kind": "weather", "category": "",
                 "host": "api.open-meteo.com", "url": "u", "outcome": "ok",
                 "error": "", "attempts": 1, "secs": 0.1}]
        out_rows, out_failed, out_timing = cn.record_weather_advice(
            rows, [], ["weather:open-meteo"], {"weather:open-meteo": 0.1}, "")
        self.assertIs(out_rows, rows)
        self.assertEqual(out_failed, ["weather:open-meteo"])
        self.assertEqual(out_timing, {"weather:open-meteo": 0.1})

    def test_a_budget_skipped_fetch_is_unknown_not_failed(self):
        rows = [{"name": "weather:open-meteo", "kind": "weather", "category": "",
                 "host": "api.open-meteo.com", "url": "u",
                 "outcome": "skipped_budget", "error": "soft budget exceeded",
                 "attempts": 1, "secs": 0.0}]
        out_rows, out_failed, _t = cn.record_weather_advice(
            rows, [], [], {}, "no normalized forecast")
        self.assertEqual(out_rows[-1]["outcome"], "skipped_budget")
        self.assertEqual(out_failed, [], "UNKNOWN is never a failure")
        self.assertEqual(out_rows[-1]["attempts"], 1)

    def test_the_row_keeps_the_frozen_key_set(self):
        rows = [{"name": "weather:open-meteo", "kind": "weather", "category": "",
                 "host": "api.open-meteo.com", "url": "u", "outcome": "ok",
                 "error": "", "attempts": 2, "secs": 0.3}]
        out_rows, out_failed, _t = cn.record_weather_advice(
            rows, [], [], {}, "boom")
        self.assertEqual(set(out_rows[-1]),
                         {"name", "kind", "category", "host", "url", "outcome",
                          "error", "attempts", "secs"})
        self.assertEqual(out_rows[-1]["attempts"], 2)
        self.assertEqual(out_rows[-1]["kind"], "weather")
        self.assertEqual(out_failed, ["weather_advice:open-meteo (boom)"])


if __name__ == "__main__":
    unittest.main()
