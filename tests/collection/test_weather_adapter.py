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
import math
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


def weather_body(dates=None, drop=(), current=None, **overrides):
    """hourly_body() plus the pass-1a `current` block and the new arrays.

    `drop` names dotted paths ("daily.sunrise", "current.temperature_2m") to
    delete, which is how one test asks "what does the block look like when this
    optional field is simply not there?".
    """
    dates = dates or DAYS
    body = hourly_body(dates, **overrides)
    count = 24 * len(dates)
    body["hourly"]["dew_point_2m"] = [15.2] * count
    body["hourly"]["uv_index"] = [0.0] * count
    body["daily"].update({
        "sunrise": ["%sT05:55" % day for day in dates],
        "sunset": ["%sT17:58" % day for day in dates],
        "uv_index_max": [8.4] * len(dates),
        "precipitation_probability_max": [85] * len(dates),
        "wind_gusts_10m_max": [22.0] * len(dates),
    })
    body["current"] = current if current is not None else {
        "time": "%sT06:15" % dates[0], "interval": 900,
        "temperature_2m": 21.4, "relative_humidity_2m": 78,
        "apparent_temperature": 22.1, "is_day": 1, "precipitation": 0.2,
        "weathercode": 51, "cloud_cover": 64, "wind_speed_10m": 6.3,
        "wind_gusts_10m": 14.0, "wind_direction_10m": 170,
    }
    for path in drop:
        parts = path.split(".")
        target = body
        for part in parts[:-1]:
            target = target[part]
        target.pop(parts[-1], None)
    return body


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

    def test_url_requests_the_current_block_and_the_new_arrays(self):
        """The frozen `weather` block needs fields the advice lane never reads."""
        url = cn.build_weather_url(self.profile)
        self.assertIn("&current=", url)
        params = dict(part.partition("=")[::2]
                      for part in url.split("?", 1)[1].split("&"))
        self.assertEqual(
            params["current"].split(","),
            ["temperature_2m", "relative_humidity_2m", "apparent_temperature",
             "is_day", "precipitation", "weathercode", "cloud_cover",
             "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m"])
        self.assertEqual(
            params["hourly"].split(","),
            ["precipitation_probability", "precipitation",
             "apparent_temperature", "temperature_2m", "wind_speed_10m",
             "visibility", "dew_point_2m", "uv_index"])
        self.assertEqual(
            params["daily"].split(","),
            ["temperature_2m_max", "temperature_2m_min", "precipitation_sum",
             "weathercode", "sunrise", "sunset", "uv_index_max",
             "precipitation_probability_max", "wind_gusts_10m_max"])
        # The daily-only literal breaking_monitor.py fetches keeps its shape.
        self.assertNotIn("current=", cn.WEATHER_URL)
        self.assertNotIn("uv_index", cn.WEATHER_URL)

    def test_no_new_field_enters_the_required_hourly_tuple(self):
        """A new field must never be able to make a day 'incomplete'."""
        self.assertEqual(cn.WEATHER_REQUIRED_HOURLY,
                         ("time", "precipitation_probability",
                          "apparent_temperature"))
        for name in ("dew_point_2m", "uv_index", "sunrise", "sunset",
                     "uv_index_max", "precipitation_probability_max",
                     "wind_gusts_10m_max"):
            self.assertNotIn(name, cn.WEATHER_REQUIRED_HOURLY)

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


# ---------------------------------------------------------------------------
# The frozen `weather` block -- pass 1a. The renderers read exactly these keys
# and are allowed to know nothing else: no weathercode, no UV threshold, no
# placeholder text. Everything here is derived from the response alone.
# ---------------------------------------------------------------------------

BLOCK_KEYS = {"fetched_at_npt", "place", "temperature_c", "apparent_c",
              "humidity_pct", "dew_point_c", "wind_kmh", "gust_kmh",
              "cloud_pct", "precip_now_mm", "condition", "today", "next12h",
              "outlook", "narrative"}
PLACE_KEYS = {"city", "latitude", "longitude", "timezone"}
TODAY_KEYS = {"tmax_c", "tmin_c", "pop_max_pct", "precip_sum_mm", "condition",
              "sunrise", "sunset", "uv_max", "uv_band"}
NEXT12H_KEYS = {"pop_peak_pct", "pop_peak_hour", "precip_mm", "window"}
OUTLOOK_KEYS = {"day", "tmax_c", "tmin_c", "pop_pct", "precip_mm", "condition",
                "gust_max_kmh"}
NUMERIC_PATHS = ("temperature_c", "apparent_c", "humidity_pct", "dew_point_c",
                 "wind_kmh", "gust_kmh", "cloud_pct", "precip_now_mm",
                 "today.tmax_c", "today.tmin_c", "today.pop_max_pct",
                 "today.precip_sum_mm", "today.uv_max",
                 "next12h.pop_peak_pct", "next12h.pop_peak_hour",
                 "next12h.precip_mm")
OUTLOOK_NUMERIC = ("tmax_c", "tmin_c", "pop_pct", "precip_mm", "gust_max_kmh")
# Never a stand-in for a number: a null stays null.
PLACEHOLDER_TOKENS = ("none", "n/a", "?", "nan", "unknown")

#: Every field this pass added. Deleting one must cost that value and nothing
#: else -- never a placeholder, never a failed run.
NEW_FIELDS = (
    "current.temperature_2m", "current.relative_humidity_2m",
    "current.apparent_temperature", "current.is_day", "current.precipitation",
    "current.weathercode", "current.cloud_cover", "current.wind_speed_10m",
    "current.wind_gusts_10m", "current.wind_direction_10m",
    "hourly.dew_point_2m", "hourly.uv_index",
    "hourly.precipitation_probability",
    "daily.sunrise", "daily.sunset", "daily.uv_index_max",
    "daily.precipitation_probability_max", "daily.wind_gusts_10m_max",
)


def strings_in(value):
    """Every string VALUE in a nested structure (keys are not values)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            for found in strings_in(item):
                yield found
    elif isinstance(value, list):
        for item in value:
            for found in strings_in(item):
                yield found


def dig(block, path):
    current = block
    for part in path.split("."):
        current = current[part]
    return current


class TestWeatherBlock(unittest.TestCase):
    """The `weather` key in candidates.json: shape, degradation, narrative."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wx-block-")
        self.profile, _ = cn.load_weather_profile(REPO_PROFILE)
        self.place = cn.weather_place(self.profile)

    def box_for(self, body, fetched=NPT0):
        return {"place": self.place, "fetched_at_npt": fetched,
                "payload": None, "error": "", "raw": body}

    def block_for(self, body, fetched=NPT0):
        return cn.build_weather_block(self.box_for(body, fetched))

    def assert_no_placeholder(self, block, label=""):
        for text in strings_in(block):
            low = text.lower()
            for token in PLACEHOLDER_TOKENS:
                self.assertNotIn(token, low,
                                 "%r carries the placeholder %r" % (label, token))
        for path in NUMERIC_PATHS:
            value = dig(block, path)
            if isinstance(value, float):
                self.assertFalse(math.isnan(value), path)

    # -- shape -------------------------------------------------------------

    def test_block_matches_the_frozen_shape(self):
        pops = [0] * 72
        for hour in range(6, 18):
            pops[hour] = 70
        pops[9] = 85
        body = weather_body(precipitation_probability=pops)
        body["daily"]["precipitation_probability_max"] = [40, 55, 5]
        block = self.block_for(body)

        self.assertEqual(set(block), BLOCK_KEYS)
        self.assertEqual(set(block["place"]), PLACE_KEYS)
        self.assertEqual(set(block["today"]), TODAY_KEYS)
        self.assertEqual(set(block["next12h"]), NEXT12H_KEYS)
        self.assertEqual(len(block["outlook"]), 3)
        for entry in block["outlook"]:
            self.assertEqual(set(entry), OUTLOOK_KEYS)
        self.assertEqual([e["day"] for e in block["outlook"]], DAYS)

        self.assertEqual(block["fetched_at_npt"], NPT0)
        self.assertEqual(block["place"]["city"], "Kathmandu")
        self.assertEqual(block["temperature_c"], 21.4)
        # The header temperature is the apparent one -- the decision, not a
        # renderer's choice.
        self.assertEqual(block["apparent_c"], 22.1)
        self.assertEqual(block["humidity_pct"], 78)
        self.assertEqual(block["dew_point_c"], 15.2)
        self.assertEqual(block["wind_kmh"], 6.3)
        self.assertEqual(block["gust_kmh"], 14.0)
        self.assertEqual(block["cloud_pct"], 64)
        self.assertEqual(block["precip_now_mm"], 0.2)
        self.assertEqual(block["condition"], "light drizzle")

        self.assertEqual(block["today"]["tmax_c"], 26.1)
        self.assertEqual(block["today"]["tmin_c"], 12.6)
        self.assertEqual(block["today"]["pop_max_pct"], 40)
        self.assertEqual(block["today"]["condition"], "light drizzle")
        self.assertEqual(block["today"]["sunrise"], "2026-09-28T05:55")
        self.assertEqual(block["today"]["sunset"], "2026-09-28T17:58")
        self.assertEqual(block["today"]["uv_max"], 8.4)
        self.assertEqual(block["today"]["uv_band"], "very high")

        self.assertEqual(block["next12h"]["pop_peak_pct"], 85)
        self.assertEqual(block["next12h"]["pop_peak_hour"], 9)
        self.assertEqual(block["next12h"]["precip_mm"], 0.0)
        self.assertEqual(block["next12h"]["window"], "06:00-17:00")

        self.assertEqual(block["outlook"][0]["gust_max_kmh"], 22.0)
        self.assert_no_placeholder(block)

    def test_every_numeric_is_a_number_or_null(self):
        for body in (weather_body(), weather_body(
                drop=("current.temperature_2m", "daily.uv_index_max")),
                weather_body(current={})):
            block = self.block_for(body)
            for path in NUMERIC_PATHS:
                value = dig(block, path)
                self.assertIsInstance(value, (int, float, type(None)),
                                      "%s was %r" % (path, value))
                self.assertNotIsInstance(value, bool, path)
            for entry in block["outlook"]:
                for key in OUTLOOK_NUMERIC:
                    value = entry[key]
                    self.assertIsInstance(value, (int, float, type(None)),
                                          "%s was %r" % (key, value))

    def test_condition_is_null_rather_than_the_word_unknown(self):
        body = weather_body()
        body["current"]["weathercode"] = 42        # not in WEATHERCODE_TEXT
        body["daily"]["weathercode"] = [42, 61, 0]
        block = self.block_for(body)
        self.assertIsNone(block["condition"])
        self.assertIsNone(block["today"]["condition"])
        self.assertIsNone(block["outlook"][0]["condition"])
        self.assertEqual(block["outlook"][1]["condition"], "slight rain")
        self.assert_no_placeholder(block)

    def test_uv_band_ladder_lives_here_and_is_text(self):
        cases = {0: "low", 2.9: "low", 3: "moderate", 5.9: "moderate",
                 6: "high", 7.9: "high", 8: "very high", 10.9: "very high",
                 11: "extreme", 14: "extreme"}
        for value, band in cases.items():
            self.assertEqual(cn.uv_band(value), band, value)
        for absent in (None, "", "high", True, float("nan")):
            self.assertIsNone(cn.uv_band(absent), absent)

    # -- optional fields must never make a day incomplete ------------------

    def test_missing_optional_daily_fields_do_not_incomplete_a_day(self):
        body = weather_body(drop=("daily.uv_index_max", "daily.sunrise",
                                  "daily.sunset",
                                  "daily.precipitation_probability_max",
                                  "daily.wind_gusts_10m_max"))
        norm, err = cn.normalize_hourly_forecast(body, self.place, NPT0)
        self.assertEqual(err, "")
        self.assertEqual([d["date"] for d in norm["days"]], DAYS)

        block = self.block_for(body)
        self.assertIsNone(block["today"]["uv_max"])
        self.assertIsNone(block["today"]["uv_band"])
        self.assertIsNone(block["today"]["sunrise"])
        self.assertIsNone(block["today"]["pop_max_pct"])
        self.assertIsNone(block["outlook"][0]["gust_max_kmh"])
        self.assertEqual(len(block["outlook"]), 3)
        self.assert_no_placeholder(block)

    def test_missing_precipitation_probability_degrades_the_block_not_the_run(self):
        """Contrast with test_short_required_array_makes_today_incomplete.

        For the ADVICE lane a missing precipitation_probability is a hole and
        the day is dropped -- unchanged, and deliberately so. The block has no
        such completeness rule: it degrades the window to null and the daily
        maximum still stands.
        """
        body = weather_body(drop=("hourly.precipitation_probability",))
        norm, err = cn.normalize_hourly_forecast(body, self.place, NPT0)
        self.assertIsNone(norm)
        self.assertIn("precipitation_probability", err)

        block = self.block_for(body)
        self.assertIsNone(block["next12h"]["pop_peak_pct"])
        self.assertIsNone(block["next12h"]["pop_peak_hour"])
        self.assertEqual(block["today"]["pop_max_pct"], 85)
        self.assertEqual(len(block["outlook"]), 3)
        self.assert_no_placeholder(block)

    def test_truncated_required_array_still_refuses_advice(self):
        """The existing :430 rule, restated against the extended body."""
        norm, err = cn.normalize_hourly_forecast(
            weather_body(precipitation_probability=[0] * 10), self.place, NPT0)
        self.assertIsNone(norm)
        self.assertIn("incomplete", err)

    def test_deleting_any_new_field_leaves_no_placeholder(self):
        for path in NEW_FIELDS:
            with self.subTest(field=path):
                box = cn.new_weather_box(self.profile, NPT0)
                body = json.dumps(weather_body(drop=(path,))).encode("utf-8")
                saved = cn.fetch_bytes
                cn.fetch_bytes = lambda url, timeout, max_bytes: body
                try:
                    candidates, summary = cn.weather_task(
                        cn.WEATHER_URL, 1, None, None, None, box)
                finally:
                    cn.fetch_bytes = saved
                self.assertEqual(len(candidates), 1)
                self.assertTrue(summary)
                block = cn.build_weather_block(box)
                self.assertEqual(set(block), BLOCK_KEYS, path)
                self.assert_no_placeholder(block, path)

    # -- narrative ---------------------------------------------------------

    def headline_body(self):
        pops = [0] * 72
        for hour in range(6, 18):
            pops[hour] = 70
        pops[9] = 85
        return weather_body(precipitation_probability=pops)

    def test_narrative_is_deterministic(self):
        body = self.headline_body()
        first = self.block_for(body)["narrative"]
        second = self.block_for(body)["narrative"]
        third = self.block_for(json.loads(json.dumps(body)))["narrative"]
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertTrue(first)

    def test_narrative_carries_the_two_headline_numbers(self):
        block = self.block_for(self.headline_body())
        narrative = block["narrative"]
        self.assertIn(str(int(round(block["temperature_c"]))), narrative)
        self.assertIn(str(int(round(block["next12h"]["pop_peak_pct"]))),
                      narrative)
        self.assertIn("rain peaks at 85%", narrative)

    def test_narrative_drops_the_rain_clause_when_pop_is_absent(self):
        body = weather_body(drop=("hourly.precipitation_probability",
                                  "daily.precipitation_probability_max"))
        narrative = self.block_for(body)["narrative"]
        self.assertNotIn("rain", narrative)
        self.assertIn("Now 21", narrative)
        self.assertTrue(narrative.endswith("."))

    def test_narrative_omits_every_clause_whose_numbers_are_missing(self):
        narrative = self.block_for(weather_body(drop=(
            "current.temperature_2m", "current.apparent_temperature",
            "current.weathercode", "daily.temperature_2m_max",
            "daily.temperature_2m_min", "hourly.precipitation_probability",
            "daily.precipitation_probability_max", "daily.uv_index_max",
            "hourly.precipitation")))["narrative"]
        self.assertEqual(narrative, "", "nothing present -> nothing claimed")

    def test_narrative_never_exceeds_the_cap(self):
        bodies = [self.headline_body(),
                  weather_body(precipitation_probability=[100] * 72)]
        for body in bodies:
            narrative = self.block_for(body)["narrative"]
            self.assertLessEqual(len(narrative), 220)
            self.assertTrue(narrative.endswith("."))

    def test_narrative_cap_is_a_hard_guard(self):
        """A clause longer than any real condition text must still be capped."""
        oversized = {"temperature_c": 21.0, "apparent_c": 22.0,
                     "condition": "x" * 300, "today": {}, "next12h": {}}
        self.assertLessEqual(len(cn.weather_narrative(oversized)), 220)
        realistic = {"temperature_c": 21.0, "apparent_c": 22.0,
                     "condition": "thunderstorm with heavy hail",
                     "today": {"tmax_c": 26.0, "tmin_c": 12.0,
                               "uv_band": "extreme"},
                     "next12h": {"pop_peak_pct": 100.0, "pop_peak_hour": 23,
                                 "precip_mm": 99.9}}
        narrative = cn.weather_narrative(realistic)
        self.assertLessEqual(len(narrative), 220)
        self.assertIn("Now 21", narrative)
        self.assertIn("rain peaks at 100%", narrative)

    def test_the_block_reaches_candidates_json_end_to_end(self):
        """The wiring, not just the function: the key is written by main()."""
        day = cn.kathmandu_now_str()[:10]
        with WeatherFixture(json.dumps(weather_body([day])).encode("utf-8"),
                            self.tmp) as fx:
            rc, payload, _log = fx.run()
        self.assertEqual(rc, 0)
        self.assertIn("weather", payload)
        block = payload["weather"]
        self.assertEqual(set(block), BLOCK_KEYS)
        self.assertEqual(set(block["place"]), PLACE_KEYS)
        self.assertEqual(set(block["today"]), TODAY_KEYS)
        self.assertEqual(set(block["next12h"]), NEXT12H_KEYS)
        self.assertEqual([e["day"] for e in block["outlook"]], [day])
        self.assertEqual(block["temperature_c"], 21.4)
        self.assertEqual(block["condition"], "light drizzle")
        self.assertEqual(block["today"]["uv_band"], "very high")
        self.assertTrue(block["narrative"])
        self.assert_no_placeholder(block)
        # The payload the renderers already read is untouched by this pass.
        self.assertIn("weather_advice", payload)
        self.assertTrue(payload["weather_summary"])

    # -- junk --------------------------------------------------------------

    def test_junk_payloads_emit_an_empty_block_and_exit_zero(self):
        for junk in ({}, "not json", [], {"hourly": {}}):
            with self.subTest(payload=junk):
                with WeatherFixture(json.dumps(junk).encode("utf-8"),
                                    self.tmp) as fx:
                    rc, payload, _log = fx.run()
                self.assertEqual(rc, 0)
                self.assertIn("weather", payload)
                self.assertEqual(payload["weather"], {})
                self.assertTrue(payload["candidates"], "news still collected")

    def test_a_payload_with_no_hourly_block_still_emits_the_block(self):
        """The daily-only shape has no `current`; the block degrades to nulls."""
        body = weather_body(drop=("hourly", "current"))
        block = self.block_for(body)
        self.assertEqual(set(block), BLOCK_KEYS)
        self.assertIsNone(block["temperature_c"])
        self.assertIsNone(block["dew_point_c"])
        self.assertIsNone(block["next12h"]["pop_peak_pct"])
        self.assertEqual(len(block["outlook"]), 3)
        self.assert_no_placeholder(block)

    def test_an_unusable_fetch_time_emits_nothing_rather_than_guessing(self):
        for fetched in ("", "nonsense", None):
            self.assertEqual(self.block_for(weather_body(), fetched=fetched),
                             {}, fetched)

    # -- nothing downstream moved -----------------------------------------

    def test_the_new_fields_do_not_move_the_summary_or_the_advice(self):
        """The frozen summary and the advice templates are untouched.

        The daily-only expectations pinned in TestDailyOnlyPathStillWorks stay
        as they were; this proves the same response with `current` and the new
        arrays attached produces byte-identical output.
        """
        saved = cn.fetch_bytes

        def fetch(body):
            cn.fetch_bytes = lambda url, timeout, max_bytes: json.dumps(
                body).encode("utf-8")
            try:
                return cn.weather_task(cn.WEATHER_URL, 1)
            finally:
                cn.fetch_bytes = saved

        plain_c, plain_summary = fetch(hourly_body())
        rich_c, rich_summary = fetch(weather_body())
        self.assertEqual(plain_summary, rich_summary)
        self.assertEqual([c["snippet"] for c in plain_c],
                         [c["snippet"] for c in rich_c])
        self.assertEqual(
            rich_summary,
            "Weather forecast for the configured location (next 3 days, "
            "Asia/Kathmandu, open-meteo): "
            "2026-09-28: max +26\u00b0C / min +13\u00b0C, precip 1.0 mm, "
            "light drizzle; "
            "2026-09-29: max +26\u00b0C / min +13\u00b0C, precip 1.0 mm, "
            "light drizzle; "
            "2026-09-30: max +26\u00b0C / min +13\u00b0C, precip 1.0 mm, "
            "light drizzle")

        fresh = cn.normalize_hourly_forecast(hourly_body(), self.place, NPT0)[0]
        rich = cn.normalize_hourly_forecast(weather_body(), self.place, NPT0)[0]
        self.assertEqual(fresh, rich)
        self.assertEqual(wa.weather_advice(fresh, self.profile, []),
                         wa.weather_advice(rich, self.profile, []))


if __name__ == "__main__":
    unittest.main()
