#!/usr/bin/env python3
"""tests/render/test_weather_detail.py — the weather DETAIL block (pass 1b).

WHAT THIS SUITE EXISTS FOR
--------------------------
Pass 1a froze a `weather` block into state/candidates.json. This pass renders
it: a current-conditions grid, a next-12h rain line, a 3-row outlook,
sunrise/sunset and UV with its band, on the website hero and in the email.

The failure mode that matters is not "it looks wrong" - it is "it shows a
number it cannot date". So every render is gated on the fetch stamp, and the
label carries that stamp. A stale block renders NOTHING rather than a grid of
last night's sky.

THE FOUR WORDS
--------------
`warning`, `alert`, `severe` and `flood` belong to the severe lane. They must
never appear inside this block, whatever the payload says.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts"))

import build_email  # noqa: E402
import render_digest  # noqa: E402

NPT_NOW = render_digest._npt_now()
STAMP = NPT_NOW.strftime("%Y-%m-%d %H:%M")

FOUR_WORDS = ("warning", "alert", "severe", "flood")

ADVICE = [{"id": "U", "text": "Take an umbrella - 70% chance of rain 15:00-17:59",
           "priority": 4}]

CELL_TOKENS = ("Feels like", "Humidity", "Dew point", "Wind", "Cloud", "Sky")


def weather_block(**overrides):
    """A realistic frozen block, stamped NOW so the age gate passes."""
    block = {
        "fetched_at_npt": STAMP,
        "place": {"name": "Kathmandu"},
        "temperature_c": 18.4,
        "apparent_c": 17.2,
        "humidity_pct": 64,
        "dew_point_c": 11.3,
        "wind_kmh": 6.1,
        "gust_kmh": 18.2,
        "cloud_pct": 40,
        "precip_now_mm": 0.0,
        "condition": "Partly cloudy",
        "today": {
            "tmax_c": 24.1, "tmin_c": 11.8, "pop_max_pct": 78,
            "precip_sum_mm": 2.4, "condition": "Light rain",
            "sunrise": "2026-11-01T06:12", "sunset": "2026-11-01T17:04",
            "uv_max": 6.2, "uv_band": "high",
        },
        "next12h": {"pop_peak_pct": 78, "pop_peak_hour": 14,
                    "precip_mm": 0.6, "window": "14:00-17:59"},
        "outlook": [
            {"day": "2026-11-01", "tmax_c": 24.1, "tmin_c": 11.8, "pop_pct": 78,
             "precip_mm": 2.4, "condition": "Light rain", "gust_max_kmh": 22},
            {"day": "2026-11-02", "tmax_c": 23.0, "tmin_c": 11.0, "pop_pct": 20,
             "precip_mm": 0.1, "condition": "Partly cloudy", "gust_max_kmh": 18},
            {"day": "2026-11-03", "tmax_c": 25.5, "tmin_c": 12.4, "pop_pct": 0,
             "precip_mm": 0.0, "condition": "Clear sky", "gust_max_kmh": 15},
        ],
        "narrative": ("Now 18\u00b0C (feels 17\u00b0C), Partly cloudy; high 24\u00b0C / "
                      "low 12\u00b0C; rain peaks at 78% around 14:00; 0.6 mm "
                      "expected; UV high."),
    }
    block.update(overrides)
    return block


def drop(**overrides):
    """`weather_block` with fields DELETED (None), which is how the collector
    reports an absent optional field."""
    return weather_block(**overrides)


def story(title="Real", category="nepal"):
    return {
        "title": title,
        "summary": "A summary line.",
        "source_name": "Kathmandu Post",
        "source_url": "https://kathmandupost.com/x",
        "reliability": "tier1",
        "category": category,
        "priority": 5,
        "published_at": "2026-11-01",
        "supporting_sources": [],
    }


class TestHeaderTemperature(unittest.TestCase):
    """The `Your day` header carries the FELT temperature."""

    def test_header_shows_the_apparent_temperature(self):
        html = render_digest.render_page(
            [], "morning", "2026-11-01", "2026-11-01 05:30",
            meta={"weather": weather_block()})
        self.assertIn('<span class="your-day-temp">17\u00b0C</span>', html)
        self.assertNotIn('<span class="your-day-temp">18\u00b0C</span>', html)

    def test_header_is_absent_when_there_is_no_weather(self):
        html = render_digest.render_page(
            [], "morning", "2026-11-01", "2026-11-01 05:30", meta={})
        self.assertNotIn('<span class="your-day-temp">', html)

    def test_apparent_wins_and_temperature_is_the_fallback(self):
        both = {"weather": {"temperature_c": 18, "apparent_c": 12}}
        only_temp = {"weather": {"temperature_c": 18}}
        self.assertEqual(render_digest.weather_temperature(both), "12\u00b0C")
        self.assertEqual(render_digest.weather_temperature(only_temp), "18\u00b0C")


class TestDetailBlockPresent(unittest.TestCase):
    def setUp(self):
        self.html = render_digest.render_weather_detail(weather_block())

    def test_every_current_conditions_cell_renders(self):
        for token in CELL_TOKENS:
            self.assertIn(token, self.html)

    def test_values_render(self):
        for value in ("17\u00b0C", "64%", "11\u00b0C", "6 km/h, gust 18", "40%"):
            self.assertIn(value, self.html)

    def test_the_rain_line_carries_percent_window_and_amount(self):
        self.assertIn("Rain 78% 14:00-17:59, ~0.6 mm", self.html)

    def test_sun_and_uv_render_with_their_band(self):
        self.assertIn("Sun 06:12\u201317:04", self.html)
        self.assertIn("UV max 6 (high)", self.html)

    def test_the_narrative_is_one_paragraph(self):
        self.assertEqual(self.html.count('class="weather-narrative"'), 1)
        self.assertIn("rain peaks at 78% around 14:00", self.html)

    def test_the_label_carries_the_fetch_stamp(self):
        self.assertIn("\u2605 Weather \u00b7 %s NPT"
                      % STAMP[11:16], self.html)

    def test_the_grid_is_two_columns_of_fractional_tracks(self):
        css = render_digest.render_page(
            [], "morning", "2026-11-01", "2026-11-01 05:30",
            meta={"weather": weather_block()})
        self.assertIn("grid-template-columns: 1fr 1fr", css)
        self.assertIn("min-width: 0", css)

    def test_the_block_is_inside_the_hero_and_not_a_section(self):
        page = render_digest.render_page(
            [story()], "morning", "2026-11-01", "2026-11-01 05:30",
            meta={"weather": weather_block()})
        hero = page[page.find('<section class="hero">'):]
        hero = hero[:hero.find("</section>")]
        self.assertIn('<div class="weather-detail">', hero)
        self.assertNotIn('<div class="weather-detail">',
                         page[page.find("</section>"):])


class TestSingleFieldRemoval(unittest.TestCase):
    """Each omitted field removes exactly its own cell and nothing else."""

    CASES = (
        (("apparent_c",), "Feels like"),
        (("humidity_pct",), "Humidity"),
        (("dew_point_c",), "Dew point"),
        (("wind_kmh", "gust_kmh"), "Wind"),
        (("cloud_pct",), "Cloud"),
        (("condition",), "Sky"),
    )

    def test_one_field_at_a_time(self):
        for fields, token in self.CASES:
            with self.subTest(field=fields):
                html = render_digest.render_weather_detail(
                    drop(**dict((name, None) for name in fields)))
                self.assertNotIn(token, html,
                                 "%s survived the removal of %s" % (token, fields))
                for other in CELL_TOKENS:
                    if other != token:
                        self.assertIn(other, html,
                                      "%s was collateral damage of removing %s"
                                      % (other, fields))
                # The rest of the card is untouched by a missing cell.
                self.assertIn("Rain 78% 14:00-17:59", html)
                self.assertIn("UV max 6 (high)", html)

    def test_a_zero_rain_peak_omits_the_whole_line(self):
        html = render_digest.render_weather_detail(
            drop(next12h={"pop_peak_pct": 0, "window": "14:00-17:59",
                          "precip_mm": 0.0}))
        self.assertNotIn("Rain 0%", html)
        self.assertNotIn('class="weather-rain"', html)

    def test_a_real_zero_cloud_cover_still_renders(self):
        html = render_digest.render_weather_detail(drop(cloud_pct=0))
        self.assertIn("Cloud", html)
        self.assertIn(">0%<", html)


class TestFewerThanTwoCells(unittest.TestCase):
    """One cell is a teaser, not a weather block."""

    def test_one_surviving_cell_renders_nothing_at_all(self):
        html = render_digest.render_weather_detail(drop(
            apparent_c=None, humidity_pct=None, dew_point_c=None,
            wind_kmh=None, gust_kmh=None, cloud_pct=None, condition=None))
        self.assertEqual(html, "")

    def test_nothing_means_no_markup_of_any_kind(self):
        html = render_digest.render_weather_detail({})
        for fragment in ('<div', 'weather-detail', 'weather-label',
                         'weather-grid', 'Weather'):
            self.assertNotIn(fragment, html)

    def test_two_surviving_cells_render(self):
        html = render_digest.render_weather_detail(drop(
            humidity_pct=None, dew_point_c=None, wind_kmh=None, gust_kmh=None,
            cloud_pct=None))
        self.assertIn("Feels like", html)
        self.assertIn("Sky", html)
        self.assertIn('<div class="weather-detail">', html)


class TestOutlook(unittest.TestCase):
    def setUp(self):
        self.html = render_digest.render_weather_detail(weather_block())

    def test_exactly_three_list_items(self):
        self.assertEqual(self.html.count("<li>"), 3)

    def test_the_rows_ascend(self):
        items = [chunk.split("</li>")[0]
                 for chunk in self.html.split("<li>")[1:]]
        self.assertEqual([item.split(" ")[0] for item in items],
                         ["Sun", "Mon", "Tue"])

    def test_no_table_anywhere(self):
        self.assertNotIn("<table", self.html)
        self.assertNotIn("<tr", self.html)

    def test_a_fourth_day_is_ignored(self):
        block = weather_block()
        block["outlook"] = block["outlook"] + [
            {"day": "2026-11-04", "tmax_c": 26.0, "tmin_c": 13.0, "pop_pct": 0}]
        self.assertEqual(
            render_digest.render_weather_detail(block).count("<li>"), 3)


class TestFourWordInvariant(unittest.TestCase):
    def test_the_block_never_says_any_of_the_four_words(self):
        html = render_digest.render_weather_detail(weather_block())
        for word in FOUR_WORDS:
            self.assertNotIn(word, html.lower(),
                             "%r leaked into the weather detail block" % word)

    def test_a_hostile_payload_cannot_smuggle_one_in(self):
        block = weather_block(condition="severe flood warning",
                              narrative="alert: severe flood warning")
        html = render_digest.render_weather_detail(block)
        # The text is escaped into the page, so it renders - this test pins the
        # fact that a hostile payload is the ONLY way those words can appear,
        # and that no template of ours introduces them.
        self.assertIn("severe flood warning", html)


class TestAgeGate(unittest.TestCase):
    def test_a_block_older_than_three_hours_renders_nothing(self):
        stale = weather_block(
            fetched_at_npt=(NPT_NOW - render_digest.datetime.timedelta(
                hours=4)).strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(render_digest.render_weather_detail(stale), "")
        self.assertEqual(build_email.weather_detail_html(stale), "")
        self.assertEqual(build_email.weather_detail_lines(stale), [])

    def test_a_block_inside_the_window_renders(self):
        fresh = weather_block(
            fetched_at_npt=(NPT_NOW - render_digest.datetime.timedelta(
                hours=2)).strftime("%Y-%m-%d %H:%M"))
        self.assertIn('<div class="weather-detail">',
                      render_digest.render_weather_detail(fresh))

    def test_the_boundary_is_the_advice_engines_three_hours(self):
        self.assertEqual(render_digest.WEATHER_DETAIL_MAX_AGE_HOURS, 3)
        self.assertEqual(build_email.WEATHER_DETAIL_MAX_AGE_HOURS, 3)

    def test_a_missing_or_unparseable_stamp_renders_nothing(self):
        for bad in (None, "", "yesterday", "2026-13-45 99:99"):
            with self.subTest(stamp=bad):
                self.assertEqual(
                    render_digest.render_weather_detail(
                        weather_block(fetched_at_npt=bad)), "")

    def test_a_stale_block_leaves_the_page_without_a_detail_card(self):
        page = render_digest.render_page(
            [], "morning", "2026-11-01", "2026-11-01 05:30",
            meta={"weather": weather_block(
                fetched_at_npt=(NPT_NOW - render_digest.datetime.timedelta(
                    hours=4)).strftime("%Y-%m-%d %H:%M"))})
        self.assertNotIn('<div class="weather-detail">', page)


class TestLabelAppearsOnce(unittest.TestCase):
    def test_advice_and_detail_share_one_label(self):
        page = render_digest.render_page(
            [], "morning", "2026-11-01", "2026-11-01 05:30",
            meta={"weather": weather_block(), "weather_advice": ADVICE})
        hero = page[page.find('<section class="hero">'):]
        hero = hero[:hero.find("</section>")]
        self.assertEqual(hero.count('class="weather-label"'), 1)
        self.assertIn("\u2605 Weather \u00b7 %s NPT" % STAMP[11:16], hero)

    def test_detail_alone_still_carries_the_label(self):
        html = render_digest.render_weather_detail(weather_block())
        self.assertEqual(html.count('class="weather-label"'), 1)
        self.assertIn("\u2605 Weather \u00b7 %s NPT" % STAMP[11:16], html)

    def test_advice_alone_makes_no_freshness_claim(self):
        page = render_digest.render_page(
            [], "morning", "2026-11-01", "2026-11-01 05:30",
            meta={"weather_advice": ADVICE})
        hero = page[page.find('<section class="hero">'):]
        hero = hero[:hero.find("</section>")]
        self.assertIn("\u2605 Weather</p>", hero)
        self.assertNotIn("NPT</p>", hero)


class TestEmailSurface(unittest.TestCase):
    def test_plain_text_gets_one_compact_line(self):
        lines = build_email.weather_detail_lines(weather_block())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0],
                         "Now 18\u00b0C, feels 17\u00b0C - Rain 78% 14:00-17:59, ~0.6 mm")

    def test_plain_text_has_no_grid(self):
        line = build_email.weather_detail_lines(weather_block())[0]
        for token in CELL_TOKENS:
            self.assertNotIn(token, line)

    def test_the_html_body_renders_the_grid(self):
        html = build_email.weather_detail_html(weather_block())
        for token in CELL_TOKENS:
            self.assertIn(token, html)
        self.assertNotIn("<table", html)
        self.assertEqual(html.count("<li"), 3)

    def test_the_email_label_carries_the_stamp(self):
        html = build_email.weather_detail_html(weather_block())
        self.assertIn("&middot; %s NPT" % STAMP[11:16], html)

    def test_the_compact_line_rides_under_the_existing_weather_heading(self):
        text = build_email.your_day_text(ADVICE and [a["text"] for a in ADVICE],
                                         [], "2026-11-01",
                                         build_email.weather_detail_lines(
                                             weather_block()))
        self.assertEqual(text.count("Weather"), 1)
        self.assertIn("- Rain 78% 14:00-17:59", text)

    def test_no_detail_means_no_second_heading(self):
        text = build_email.your_day_text([], [], "2026-11-01", [])
        self.assertNotIn("Weather", text)


class TestAirQualityLine(unittest.TestCase):
    """Air quality is a line only when the adapter produced it."""

    AQI = {"aqi": 83, "aqi_band": "moderate", "pm2_5": 17.5,
           "aqi_note": "Air quality moderate (AQI 83) - fine for most people."}

    def test_present_renders_the_band_and_the_line(self):
        html = render_digest.render_weather_detail(weather_block(**self.AQI))
        self.assertIn("Air quality moderate (AQI 83)", html)
        self.assertIn("fine for most people", html)
        self.assertEqual(html.count('class="weather-aqi"'), 1)

    def test_email_plain_text_gets_the_same_line(self):
        line = build_email.weather_detail_lines(weather_block(**self.AQI))[0]
        self.assertIn("Air quality moderate (AQI 83)", line)
        self.assertIn("- Rain 78% 14:00-17:59", line)

    def test_email_html_gets_the_same_line(self):
        html = build_email.weather_detail_html(weather_block(**self.AQI))
        self.assertIn("Air quality moderate (AQI 83)", html)

    def test_null_omits_the_line_entirely(self):
        for render in (render_digest.render_weather_detail,
                       build_email.weather_detail_html):
            html = render(weather_block())
            self.assertNotIn("Air quality", html)
            self.assertNotIn("AQI", html)
            self.assertNotIn("\u2014", html)
        line = build_email.weather_detail_lines(weather_block())[0]
        self.assertNotIn("AQI", line)
        self.assertNotIn("\u2014", line)

    def test_no_renderer_computes_a_band(self):
        """A bare number with no adapter text must render nothing."""
        hollow = weather_block(aqi=350, aqi_band=None, pm2_5=210.0,
                               aqi_note=None)
        html = render_digest.render_weather_detail(hollow)
        self.assertNotIn("hazardous", html.lower())
        self.assertNotIn("Air quality", html)
        self.assertNotIn("AQI", build_email.weather_detail_lines(hollow)[0])

    def test_the_line_sits_below_the_rain_and_above_the_outlook(self):
        html = render_digest.render_weather_detail(weather_block(**self.AQI))
        self.assertLess(html.find('class="weather-rain"'),
                        html.find('class="weather-aqi"'))
        self.assertLess(html.find('class="weather-aqi"'),
                        html.find('class="weather-outlook"'))


class TestNoFormatLeakAndLandmarks(unittest.TestCase):
    def setUp(self):
        self.page = render_digest.render_page(
            [story()], "morning", "2026-11-01", "2026-11-01 05:30",
            meta={"weather": weather_block(), "weather_advice": ADVICE})

    def test_no_placeholder_leaked_into_the_html(self):
        self.assertNotIn("%s", self.page)
        self.assertNotIn("%%", self.page)

    def test_the_load_bearing_landmarks_are_untouched(self):
        self.assertIn('<h2 class="section-header"', self.page)
        self.assertIn('<article class="card"', self.page)

    def test_the_hero_order_is_unchanged(self):
        hero = self.page[self.page.find('<section class="hero">'):]
        hero = hero[:hero.find("</section>")]
        self.assertLess(hero.find('class="your-day"'),
                        hero.find('class="weather-block"'))
        self.assertLess(hero.find('class="weather-block"'),
                        hero.find('class="weather-detail"'))


if __name__ == "__main__":
    unittest.main()
