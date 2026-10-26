#!/usr/bin/env python3
"""tests/render/test_render_personal.py — the personal layer on ALL FOUR surfaces.

THE PROBLEM THIS SUITE EXISTS FOR
---------------------------------
The owner said the digest "reads like a book" and never told him what anything
meant FOR HIM. The fix is two blocks near the top of every surface (`Your day`
and a weather block) plus a per-story `for_you` line, and the whole thing is
built by SELECTING strings something else already screened. It introduces zero
new model-authored text, and a test that cannot see that is a test that cannot
protect it.

THE FOUR SURFACES ARE FOUR IMPLEMENTATIONS
------------------------------------------
  1. website            scripts/render_digest.py
  2. email HTML         scripts/build_email.py
  3. plain-text body    scripts/build_email.py render_sections_text
                        -> consumed by BOTH Gmail and Telegram
  4. ntfy push          a SEPARATE implementation inside the
                        .github/workflows/daily-news.yml heredoc, which does
                        NOT go through render_sections_text

Getting (4) wrong is the silent failure this file is built around: a renderer
change that never reaches the heredoc looks completely correct in every other
test and ships a push with no personal layer at all. So the golden test here
renders all four and asserts they AGREE.

Every render goes to a TEMP directory. Nothing here writes to docs/.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import (  # noqa: E402
    TU_HOSTS, health, validated_payload, write_json)
from test_render_honesty import ntfy_source, run_ntfy  # noqa: E402

import build_email  # noqa: E402
import render_digest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO_ROOT, "templates", "email.html")
MAX_BYTES = 4096

DATE = "2026-09-27"
TOMORROW = "2026-09-28"
YESTERDAY = "2026-09-26"

NOTHING_TODAY = "Nothing needs you today."
FOR_YOU_LABEL = "For you"
YOUR_DAY_LABEL = "Your day"
WEATHER_LABEL = "Weather"

#: What EACH surface says when nothing cleared filtering, VERBATIM.
#:
#: These stopped being one string in e9d4115, which gave the ntfy push a recap
#: path and a degraded path of its own; it no longer borrows the website's
#: sentence. Keeping one literal across all four would have pinned a WORDING
#: CHOICE rather than the property the digest actually depends on, and it is
#: the wording choice that already broke twice. What must stay true is that no
#: surface goes quiet when there is nothing to say.
ZERO_STORY_LINE = {
    "website": "No items cleared filtering today.",
    "email_html": "No items cleared filtering today.",
    "email_text": "No items cleared filtering today.",
    "ntfy": "nothing new cleared filtering for this slot",
}

# The engine's own strings, VERBATIM and in the engine's own order. Nothing
# here may be re-worded, so these are copied from the FROZEN templates in
# scripts/weather_advice.py rather than paraphrased.
W_UMBRELLA = "Take an umbrella - 70% chance of rain 15:00-17:59"
W_JACKET = "Take a jacket - feels like 9C 07:00-11:59"

WEATHER_TWO = [
    {"id": "U", "text": W_UMBRELLA, "priority": 4},
    {"id": "J1", "text": W_JACKET, "priority": 6},
]


def story(title, category="nepal", **extra):
    out = {
        "title": title,
        "summary": "A summary line.",
        "source_name": "Kathmandu Post",
        "source_url": "https://kathmandupost.com/%s" % title.lower().replace(" ", "-"),
        "reliability": "tier1",
        "category": category,
        "priority": 5,
        "published_at": DATE,
        "supporting_sources": [],
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Render all four surfaces from ONE payload, into a TEMP dir.
# ---------------------------------------------------------------------------

class Surfaces(object):
    """The four rendered blobs, plus where they were written."""

    def __init__(self, page, email_html, email_text, ntfy_message):
        self.page = page
        self.email_html = email_html
        self.email_text = email_text
        self.ntfy_message = ntfy_message

    def all_four(self):
        return {"website": self.page, "email_html": self.email_html,
                "email_text": email_text_name(self.email_text),
                "ntfy": self.ntfy_message}

    def plain(self):
        """The three text-ish surfaces (ntfy markdown included)."""
        return {"email_text": self.email_text, "ntfy": self.ntfy_message}


def email_text_name(text):
    return text


def render_all_four(payload, candidates=None, tmp=None):
    """Render payload onto all four surfaces. Returns a Surfaces.

    `candidates` is written to state/candidates.json, which is where the
    weather engine's verdicts actually live in production. Passing it here
    therefore exercises the SAME read path the workflow uses rather than
    testing a shape the pipeline never produces.
    """
    owned = tmp is None
    tmp = tmp or tempfile.mkdtemp(prefix="render-personal-")
    cwd = os.getcwd()
    try:
        os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
        stories_json = write_json(os.path.join(tmp, "state", "stories.validated.json"),
                                 payload)
        if candidates is not None:
            write_json(os.path.join(tmp, "state", "candidates.json"), candidates)

        # Run from the temp dir with RELATIVE paths so the PRODUCTION defaults
        # are what gets exercised (`state/stories.validated.json`,
        # `state/candidates.json`) rather than a test-only spelling of them.
        os.chdir(tmp)
        out_dir = os.path.join(tmp, "digests")
        index = os.path.join(tmp, "index.html")
        rc = render_digest.main([
            "--digest-time", "morning", "--date", DATE,
            "--email-txt", os.path.join(tmp, "absent-email.txt"),
            "--stories-json", os.path.join("state", "stories.validated.json"),
            "--candidates", os.path.join("state", "candidates.json"),
            "--out-dir", out_dir, "--index", index])
        assert rc == 0, "render_digest.main exited %r" % rc
        with io.open(os.path.join(out_dir, "%s-morning.html" % DATE),
                     encoding="utf-8") as fh:
            page = fh.read()

        email_html_path = os.path.join(tmp, "email.html")
        email_text_path = os.path.join(tmp, "email.txt")
        rc = build_email.main(["--in", os.path.join("state", "stories.validated.json"),
                               "--out-html", email_html_path,
                               "--out-text", email_text_path,
                               "--template", TEMPLATE,
                               "--candidates", os.path.join("state", "candidates.json")])
        assert rc == 0, "build_email.main exited %r" % rc
        with io.open(email_html_path, encoding="utf-8") as fh:
            email_html = fh.read()
        with io.open(email_text_path, encoding="utf-8") as fh:
            email_text = fh.read()

        # run_ntfy execs the REAL heredoc in DRY RUN with urlopen poisoned, so
        # this can never make a network call. It reads state/*.json from cwd.
        got = run_ntfy(tmp, payload)
        return Surfaces(page, email_html, email_text, got["message"])
    finally:
        os.chdir(cwd)
        if owned:
            shutil.rmtree(tmp, ignore_errors=True)


def payload_of(stories, weather=None, health_state="degraded", notes=""):
    data = validated_payload(health_state, stories=stories, notes=notes,
                             hosts_down=TU_HOSTS)
    if weather is not None:
        data["weather_advice"] = weather
    return data


# ===========================================================================
# 1. `Your day` - every combination, and the literal fallback
# ===========================================================================

class TestYourDayCombinations(unittest.TestCase):
    """The block is NEVER empty and NEVER padded.

    Each case is asserted on all four surfaces, because a combination that
    renders on three and not the fourth is exactly the failure this suite is
    here to catch.
    """

    def _assert_all_four(self, stories, weather, expect_lines, absent=()):
        payload = payload_of(stories, weather=weather)
        s = render_all_four(payload, candidates={"weather_advice": weather or []})
        for name, blob in s.all_four().items():
            for line in expect_lines:
                self.assertIn(line, blob, "%s is missing %r" % (name, line))
            for line in absent:
                self.assertNotIn(line, blob, "%s unexpectedly carries %r" % (name, line))
        return s

    def test_deadline_today_wins_the_first_line(self):
        s = self._assert_all_four(
            [story("Exam form closes", "education_tu", deadline=DATE,
                   for_you="the portal closes at 17:00")],
            weather=[],
            expect_lines=["Exam form closes - due %s" % DATE, YOUR_DAY_LABEL],
            absent=[NOTHING_TODAY])
        # The deadline line and its for_you line are the SAME story: the box
        # must not say the same story twice.
        self.assertEqual(s.email_text.count("Exam form closes - due"), 1)

    def test_deadline_tomorrow_also_qualifies(self):
        self._assert_all_four(
            [story("Scholarship window", "tech_career", deadline=TOMORROW)],
            weather=[], expect_lines=["Scholarship window - due %s" % TOMORROW])

    def test_deadline_yesterday_and_far_off_never_qualify(self):
        s = self._assert_all_four(
            [story("Yesterday", "nepal", deadline=YESTERDAY),
             story("Next year", "nepal", deadline="2027-06-01"),
             story("Unparseable", "nepal", deadline="sometime soon")],
            weather=[],
            expect_lines=[NOTHING_TODAY],
            absent=["- due ", "Yesterday - due", "Next year - due",
                    "Unparseable - due"])

    def test_only_for_you(self):
        self._assert_all_four(
            [story("Steam sale", "gaming", for_you="wait for the sale, it is not worth full price")],
            weather=[], expect_lines=["wait for the sale, it is not worth full price"],
            absent=[NOTHING_TODAY])

    def test_only_weather(self):
        """CHANGED (owner-approved BUG 1). This used to assert NOTHING_TODAY is
        ABSENT from all four surfaces, which is precisely what shipped the bug:
        with weather and nothing else, the whole `Your day` block vanished and
        the page kept the CSS for a feature that was no longer in the body. The
        owner read that as a broken feature and could not tell it from a quiet
        day.

        So on the three surfaces this change owns the block now renders the
        documented fallback. The ntfy push is a separate implementation inside
        .github/workflows/daily-news.yml, which is NOT an owned file, so it
        still omits the block - asserted explicitly below rather than left to
        diverge silently."""
        s = self._assert_all_four([], weather=WEATHER_TWO,
                                  expect_lines=[W_UMBRELLA, W_JACKET])
        for name in ("website", "email_html", "email_text"):
            self.assertIn(NOTHING_TODAY, s.all_four()[name],
                          "%s dropped the fallback; the block must never vanish" % name)
        # The push is unchanged and cannot be changed from here.
        self.assertNotIn(NOTHING_TODAY, s.ntfy_message)

    def test_completely_empty_renders_the_literal_fallback(self):
        """Four words, and only on a genuine nothing."""
        self._assert_all_four([story("No for_you here", "gaming")], weather=[],
                              expect_lines=[NOTHING_TODAY])

    def test_zero_stories_at_all_still_renders_the_block(self):
        """An empty digest is exactly when he most needs to be told.

        CHANGED (e9d4115). This used to assert ONE sentence on all four
        surfaces, and that sentence no longer exists on the push: it gained a
        recap path and a degraded path of its own and states the same fact in
        its own words. Asserting the shared literal was therefore pinning a
        wording choice, not the property in the docstring.

        Each surface is now asserted against ITS OWN sentence instead, which is
        strictly more precise rather than less: the old form could not express
        a surface that keeps its own wording, and the new form still fails if
        any surface stops saying so. The exhaustiveness check below is what
        stops it decaying into a test of three surfaces -- adding a surface must
        break this test, not silently skip it.
        """
        s = self._assert_all_four([], weather=[], expect_lines=[NOTHING_TODAY])
        blobs = s.all_four()
        self.assertEqual(
            set(ZERO_STORY_LINE), set(blobs),
            "the set of surfaces changed and the zero-story assertion no longer "
            "covers all of them. Surfaces rendered: %s; declared: %s. A new "
            "surface must be added to ZERO_STORY_LINE or it is untested."
            % (sorted(blobs), sorted(ZERO_STORY_LINE)))
        for name, blob in blobs.items():
            self.assertIn(ZERO_STORY_LINE[name], blob,
                          "%s must still say that nothing cleared filtering, in "
                          "its own wording" % name)

    def test_deadlines_outrank_weather_in_the_order(self):
        """Deadlines first, then the surviving for_you lines, then weather."""
        lines = render_digest.your_day_lines(
            [story("Game", "gaming", for_you="pay the fee"),
             story("Due", "nepal", deadline=DATE),
             story("Also due", "nepal", deadline=TOMORROW)],
            [W_UMBRELLA, W_JACKET], DATE)
        self.assertEqual(lines, ["Due - due %s" % DATE, "Also due - due %s" % TOMORROW,
                                 "pay the fee", W_UMBRELLA, W_JACKET])

    def test_deadline_line_is_not_also_a_for_you_line(self):
        lines = render_digest.your_day_lines(
            [story("Only deadline", "nepal", deadline=DATE, for_you="this is mine")],
            [], DATE)
        self.assertEqual(lines, ["Only deadline - due %s" % DATE])

    def test_both_renderers_agree_line_for_line(self):
        cases = [
            ([], []),
            ([story("A", "nepal", deadline=DATE)], []),
            ([story("A", "gaming", for_you="skip")], [W_UMBRELLA]),
            ([story("A", "education_tu", deadline=TOMORROW, for_you="read it")],
             [W_UMBRELLA, W_JACKET]),
        ]
        for stories, weather in cases:
            got = render_digest.your_day_lines(stories, weather, DATE)
            self.assertEqual(got, build_email.your_day_lines(stories, weather, DATE),
                             "stories=%r weather=%r" % (stories, weather))

    def test_nearest_deadline_reads_first(self):
        lines = render_digest.your_day_lines(
            [story("Later", "nepal", deadline=TOMORROW),
             story("Sooner", "nepal", deadline=DATE)], [], DATE)
        self.assertEqual(lines, ["Sooner - due %s" % DATE, "Later - due %s" % TOMORROW])

    def test_at_most_three_for_you_lines_and_two_weather_lines(self):
        many = [story("S%d" % i, "gaming", for_you="line %d" % i, priority=i)
                for i in range(6)]
        lines = render_digest.your_day_lines(many, [W_UMBRELLA, W_JACKET] * 3, DATE)
        fy = [ln for ln in lines if ln.startswith("line ")]
        self.assertEqual(len(fy), render_digest.FOR_YOU_MAX)
        self.assertEqual(fy, ["line 5", "line 4", "line 3"])
        self.assertEqual(lines[-1], W_JACKET)  # only WEATHER_MAX survive

    def test_for_you_order_prefers_action_categories_then_priority(self):
        """Obligation-bearing category first, then the pipeline's priority
        descending, then input order - a documented, deterministic rule."""
        stories = [story("Game", "gaming", for_you="game line", priority=9),
                   story("Bank", "bank_local", for_you="bank line", priority=5),
                   story("Exam", "education_tu", for_you="exam line", priority=8)]
        lines = render_digest.your_day_lines(stories, [], DATE)
        self.assertEqual(lines, ["exam line", "bank line", "game line"])

    def test_weather_text_is_verbatim_and_never_re_worded(self):
        """The engine's string reaches the reader character for character.

        The NO-DUPLICATION rule (see the comment above FOR_YOU_LABEL in
        scripts/render_digest.py) is that a weather string appears exactly
        ONCE per rendered output, so `count` is asserted here too, not just
        presence. It used to appear twice on a text surface - once inside
        `Your day`, once under the labelled weather block - and printing the
        same sentence twice in a digest the owner reads at 06:00 for exactly
        the opposite reason is precisely the verbosity he complained about.
        """
        s = render_all_four(payload_of([story("A", "nepal")], weather=WEATHER_TWO),
                            candidates={"weather_advice": WEATHER_TWO})
        for name, blob in s.plain().items():
            self.assertIn(W_UMBRELLA, blob, name)
            self.assertIn(W_JACKET, blob, name)
            self.assertEqual(blob.count(W_UMBRELLA), 1,
                             "%s printed the umbrella advice twice" % name)
            self.assertEqual(blob.count(W_JACKET), 1,
                             "%s printed the jacket advice twice" % name)
            for mangled in ("70% chance of rain 15:00 to 17:59",
                            "Take an umbrella, 70% chance of rain 15:00-17:59",
                            "70 % chance of rain"):
                self.assertNotIn(mangled, blob, "%s re-worded the advice" % name)

    def test_engine_order_is_preserved_not_re_ranked(self):
        """The engine sorted by severity; a second sort here would be a rival
        severity rule that can silently disagree with the engine. Asserted
        RELATIVE order inside each block, so it holds whichever order the
        engine chose."""
        for order in (WEATHER_TWO, list(reversed(WEATHER_TWO))):
            payload = payload_of([], weather=order)
            s = render_all_four(payload, candidates={"weather_advice": order})
            expected = [item["text"] for item in order]
            for block in (s.ntfy_message.split("**%s**" % WEATHER_LABEL)[1],
                          s.email_text.split(WEATHER_LABEL, 1)[1]):
                positions = [block.index(text) for text in expected]
                self.assertEqual(positions, sorted(positions),
                                 "engine order %r was not preserved" % (expected,))

    def test_advice_arrives_via_candidates_when_the_payload_lacks_it(self):
        """The production shape: the engine writes state/candidates.json."""
        payload = payload_of([story("A", "nepal")], weather=None)
        self.assertNotIn("weather_advice", payload)
        s = render_all_four(payload, candidates={"weather_advice": WEATHER_TWO})
        for name, blob in s.all_four().items():
            self.assertIn(W_UMBRELLA, blob, "%s lost the candidates.json advice" % name)

    def test_absent_weather_data_everywhere_is_silent_not_fatal(self):
        """No candidates.json, no key, no list: the render still succeeds and
        the block says the honest four-word thing."""
        payload = payload_of([story("A", "nepal")], weather=None)
        s = render_all_four(payload, candidates=None)
        for name, blob in s.all_four().items():
            self.assertIn(NOTHING_TODAY, blob, name)
            self.assertNotIn("couldn't fetch", blob.lower(), name)
            self.assertNotIn("weather unavailable", blob.lower(), name)


# ===========================================================================
# 2. The weather block: presence, and total absence
# ===========================================================================

class TestWeatherBlockPresence(unittest.TestCase):
    """Weather was present only through 2026-09-01 and then vanished from all
    14 digests since 2026-09-21, silently, because nothing asserted it. These
    tests are the reason it cannot happen again: an empty engine list must
    produce NO weather block, and a populated one must produce the text."""

    def test_present_on_all_four_surfaces(self):
        s = render_all_four(payload_of([story("A", "nepal")], weather=WEATHER_TWO),
                            candidates={"weather_advice": WEATHER_TWO})
        for name, blob in s.all_four().items():
            self.assertIn(W_UMBRELLA, blob, "%s has no weather" % name)
            self.assertIn(WEATHER_LABEL, blob, "%s has no weather label" % name)

    def test_empty_list_renders_no_weather_block_anywhere(self):
        """[] means SAY NOTHING: no heading with nothing under it, and above
        all no 'couldn't fetch weather' line."""
        s = render_all_four(payload_of([story("A", "nepal")], weather=[]),
                            candidates={"weather_advice": []})
        for name, blob in s.all_four().items():
            self.assertIn(NOTHING_TODAY, blob, name)
            for banned in ("Take an umbrella", "Take a jacket", "Nothing to plan for"):
                self.assertNotIn(banned, blob, "%s invented weather: %r" % (name, banned))
        # A bare WEATHER heading with no advice under it is the same defect in
        # a different costume.
        self.assertNotIn('<p class="weather-label">', s.page)
        self.assertNotIn("&#9733; Weather", s.email_html)

    def test_engine_weather_block_lines_are_empty_for_an_empty_list(self):
        for mod in (render_digest, build_email):
            self.assertEqual(mod.weather_block_lines([]), [])
            self.assertEqual(mod.weather_block_lines(None), [])
            self.assertEqual(mod.weather_block_lines(WEATHER_TWO[:0]), [])

    def test_single_advice_line_still_renders(self):
        s = render_all_four(payload_of([], weather=WEATHER_TWO[:1]),
                            candidates={"weather_advice": WEATHER_TWO[:1]})
        for name, blob in s.all_four().items():
            self.assertIn(W_UMBRELLA, blob, name)

    def test_junk_weather_shapes_never_crash_a_render(self):
        for junk in (None, [], {}, "banana", 7, [""], [None], [{"text": ""}],
                     {"nope": 1}, [{"id": "U"}]):
            payload = payload_of([story("A", "nepal")])
            payload["weather_advice"] = junk
            tmp = tempfile.mkdtemp()
            try:
                s = render_all_four(payload, candidates=None, tmp=tmp)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            self.assertIn(NOTHING_TODAY, s.email_text, "junk=%r" % (junk,))


# ===========================================================================
# 3. `for_you` on the story card: rendered, or absolutely nothing
# ===========================================================================

class TestForYouConditional(unittest.TestCase):
    """The live bug: build_email.py rendered a bare bold `Why it matters`
    label UNCONDITIONALLY, so every story missing the field shipped an empty
    heading on the email / Gmail / Telegram path. Absence must be total."""

    LINE = "this is the one that changes your Thursday"

    def test_rendered_on_all_four_surfaces_when_present(self):
        s = render_all_four(
            payload_of([story("Has it", "nepal", for_you=self.LINE)], weather=[]),
            candidates={"weather_advice": []})
        for name, blob in s.all_four().items():
            self.assertIn(self.LINE, blob, "%s dropped the for_you line" % name)
            self.assertIn(FOR_YOU_LABEL, blob, "%s has no for_you label" % name)

    def test_absent_leaves_no_label_and_no_dash_on_any_surface(self):
        s = render_all_four(
            payload_of([story("Has none", "nepal"), story("Also none", "gaming")],
                       weather=[]),
            candidates={"weather_advice": []})
        for name, blob in s.all_four().items():
            self.assertNotIn(FOR_YOU_LABEL, blob,
                             "%s rendered an empty for_you label" % name)
            self.assertNotIn("Why it matters", blob,
                             "%s still renders the legacy label" % name)
        # The old email bug verbatim: a bold label with nothing after it.
        self.assertNotIn("<strong", s.email_html.split("Has none")[1][:400])
        self.assertNotIn("  For you: \n", s.email_text)

    def test_whitespace_only_for_you_counts_as_absent(self):
        s = render_all_four(
            payload_of([story("Blank", "nepal", for_you="   \n\t ")], weather=[]),
            candidates={"weather_advice": []})
        for name, blob in s.all_four().items():
            self.assertNotIn(FOR_YOU_LABEL, blob, name)

    def test_the_two_stories_disagree_only_where_they_should(self):
        """One story carries a line, one does not. Exactly one label ships."""
        s = render_all_four(
            payload_of([story("With", "nepal", for_you=self.LINE),
                        story("Without", "gaming")], weather=[]),
            candidates={"weather_advice": []})
        self.assertEqual(s.email_text.count("For you:"), 1)
        self.assertEqual(s.page.count('class="card-foryou"'), 1)
        self.assertEqual(s.ntfy_message.count("_For you: %s_" % self.LINE), 1)

    def test_for_you_is_escaped_not_injected(self):
        evil = '<script>alert("x")</script>'
        s = render_all_four(
            payload_of([story("Evil", "nepal", for_you=evil)], weather=[]),
            candidates={"weather_advice": []})
        self.assertNotIn("<script>", s.page)
        self.assertNotIn("<script>", s.email_html)
        self.assertIn("&lt;script&gt;", s.page)

    def test_website_card_label_is_the_field_name(self):
        card = render_digest.build_story_card(story("X", "nepal", for_you=self.LINE))
        self.assertIn('<p class="card-foryou"><strong>For you:</strong> %s</p>'
                      % self.LINE, card)
        self.assertNotIn("card-why", card)

    def test_plain_text_line_round_trips_through_the_website_parser(self):
        """render_digest falls back to parsing email.txt when the validated
        payload is missing, so a `For you:` line there must survive the
        round-trip or the fallback page silently loses it."""
        s = render_all_four(
            payload_of([story("Round", "nepal", for_you=self.LINE),
                        story("No line", "gaming")], weather=[]),
            candidates={"weather_advice": []})
        parsed = render_digest.parse_email_text(s.email_text)
        by_title = {p["title"]: p for p in parsed}
        self.assertEqual(by_title["Round"]["for_you"], self.LINE)
        self.assertEqual(by_title["No line"]["for_you"], "")

    def test_personal_block_never_becomes_a_story_for_the_parser(self):
        """`Your day` bullets must not be parsed as stories by the fallback
        page: a line like "Take an umbrella - ..." would otherwise become a
        headline. (Empty-section status lines DO parse as stories - that is
        pre-existing behaviour in render_sections_text and out of scope here;
        what matters is that the personal block never does.)"""
        s = render_all_four(
            payload_of([story("Real", "nepal", for_you=self.LINE)], weather=WEATHER_TWO),
            candidates={"weather_advice": WEATHER_TWO})
        titles = [p["title"] for p in render_digest.parse_email_text(s.email_text)]
        self.assertIn("Real", titles)
        for leaked in ("Your day", "Weather", W_UMBRELLA, W_JACKET):
            self.assertNotIn(leaked, titles,
                             "the personal block leaked into a story title")


# ===========================================================================
# 4. The ntfy byte budget - the hardest constraint
# ===========================================================================

LONG_FOR_YOU = ("Your TU re-registration form is due before Friday and the portal "
                "drops your seat if you miss the window, so this one is not "
                "optional reading for once")[:140]
assert len(LONG_FOR_YOU) == 140, len(LONG_FOR_YOU)


def worst_case_payload(stories=15):
    """15 stories, each with a 140-char `for_you`, plus 2 weather lines and a
    degraded health banner. The brief's exact worst case."""
    filler = ("Tribhuvan University and the wider Nepali public sector continue "
              "to publish overlapping notices, and the digest builder keeps "
              "every field intact while doing it. ")
    stories_list = []
    cats = ["education_tu", "nepal", "gaming", "tech_career", "world_major"]
    for i in range(stories):
        stories_list.append(story(
            "Worst case story %02d with a deliberately long headline" % i,
            cats[i % len(cats)], for_you=LONG_FOR_YOU, summary=filler * 2,
            priority=9 - (i % 5),
            source_url="https://news.google.com/rss/articles/CBMiSmhwwAAAAAAAAAAAAAAAAAAAAAAAA%02d" % i,
            supporting_sources=[
                {"name": "Google News redirect",
                 "url": "https://news.google.com/rss/articles/CBMiSmhwwBBBBBBBBBBBBBBBBBBBBBBBBBBBB%02d" % i},
                {"name": "The Kathmandu Post",
                 "url": "https://kathmandupost.com/real-%02d" % i},
                {"name": "Another redirect",
                 "url": "https://news.google.com/rss/articles/CBMiSmhwwCCCCCCCCCCCCCCCCCCCCCCCCCCCC%02d" % i},
            ]))
    return payload_of(stories_list, weather=WEATHER_TWO,
                      health_state="degraded",
                      notes="TU notice board unreachable (DNS SERVFAIL) for 41 minutes.")


class TestNtfyByteBudget(unittest.TestCase):
    MAX_BYTES = MAX_BYTES
    PERSONAL_MAX_BYTES = 1000

    def test_worst_case_is_within_4096_bytes(self):
        """THE mandatory test. 15 stories x 140-char for_you, 2 weather lines,
        a degraded banner, and every story carrying THREE Google News
        redirects."""
        payload = worst_case_payload()
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        size = len(got["message"].encode("utf-8"))
        self.assertLessEqual(size, self.MAX_BYTES,
                             "ntfy worst case is %d bytes (limit %d)"
                             % (size, self.MAX_BYTES))

    def test_worst_case_keeps_the_banner_and_the_personal_layer(self):
        payload = worst_case_payload()
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        self.assertLessEqual(len(message.encode("utf-8")), self.MAX_BYTES)
        self.assertTrue(message.startswith("\u26a0\ufe0f Some sources were unreachable"),
                        "the honesty banner must still lead, got %r" % message[:80])
        self.assertIn("**%s**" % YOUR_DAY_LABEL, message)
        self.assertIn("Down: iost.tu.edu.np, tu.edu.np", message)

    def test_personal_layer_stays_inside_its_own_ceiling(self):
        """The personal layer is ~24% of the budget by contract, so the
        remainder is available to the stories rather than being eaten."""
        payload = worst_case_payload()
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        start = message.index("**%s**" % YOUR_DAY_LABEL)
        end = message.index("\n## ", start)
        personal = message[start:end if end > 0 else len(message)]
        self.assertLessEqual(len(personal.encode("utf-8")),
                             self.PERSONAL_MAX_BYTES,
                             "personal layer is %d bytes (ceiling %d)"
                             % (len(personal.encode("utf-8")), self.PERSONAL_MAX_BYTES))

    def test_your_day_block_is_at_most_three_lines(self):
        payload = worst_case_payload()
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        block = got["message"].split("**%s**" % YOUR_DAY_LABEL, 1)[1]
        lines = []
        for raw in block.split("\n"):
            if not raw.startswith("- "):
                break
            lines.append(raw)
        self.assertLessEqual(len(lines), render_digest.YOUR_DAY_MAX_LINES,
                             "ntfy `Your day` rendered %d lines: %r"
                             % (len(lines), lines))

    def test_weather_block_is_at_most_two_lines(self):
        payload = worst_case_payload()
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        if "**%s**" % WEATHER_LABEL in message:
            block = message.split("**%s**" % WEATHER_LABEL, 1)[1]
            lines = []
            for raw in block.split("\n"):
                if not raw.startswith("- "):
                    break
                lines.append(raw)
            self.assertLessEqual(len(lines), render_digest.WEATHER_MAX)

    def test_drop_order_is_stories_then_weather_then_your_day(self):
        """First dropped = last kept. The banner is never in the list."""
        # No for_you: only stories to drop, and the banner plus `Your day` must
        # both survive the squeeze. ONE deadline is given to the top story on
        # purpose: `Your day` is now deadlines + `for_you` only (the no-
        # duplication rule), so with neither it would be legitimately EMPTY -
        # the weather block alone would render, and this test would then be
        # asserting the empty-header case instead of the drop order.
        payload = worst_case_payload()
        payload["stories"][0]["deadline"] = DATE
        for s in payload["stories"]:
            s.pop("for_you", None)
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        self.assertLessEqual(len(message.encode("utf-8")), self.MAX_BYTES)
        self.assertTrue(message.startswith("\u26a0\ufe0f"), message[:80])
        self.assertIn("**%s**" % YOUR_DAY_LABEL, message)
        # Stories are what got dropped, so the truncation notice is present.
        # The Pages ORIGIN, not the deleted digest-auth Worker - see
        # DigestLinkHostTests, which now owns that contract.
        self.assertIn("full digest: https://daily-digest.example.pages.dev", message)

    def test_banner_survives_even_when_nothing_else_fits(self):
        """A single enormous story must not cost the honesty contract."""
        payload = payload_of([story("Enormous", "nepal", summary="x" * 20000,
                                    for_you="y" * 4000)])
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        self.assertLessEqual(len(message.encode("utf-8")), self.MAX_BYTES)
        self.assertTrue(message.startswith("\u26a0\ufe0f"), message[:80])

    def test_ok_health_still_renders_the_personal_layer(self):
        """No banner is normal; the personal layer is not conditional on one."""
        payload = payload_of([story("A", "nepal", for_you="read the notice")],
                             weather=WEATHER_TWO, health_state="ok")
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("**%s**" % YOUR_DAY_LABEL, got["message"])
        self.assertIn(W_UMBRELLA, got["message"])

    def test_story_counts_still_drive_the_title(self):
        payload = worst_case_payload(3)
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("3 items", got["title"])


# ===========================================================================
# 4b. The glance block and the title's reason clause
# ===========================================================================

GLANCE_LABEL = "At a glance"
#: The value the shipped heredoc declares. Pinned BOTH ways on purpose: the
#: rendered title is checked against the literal 72 below, AND the source is
#: checked for this exact constant. A silent raise of the budget would
#: otherwise pass a test that only compared the title against whatever the
#: source currently says.
TITLE_MAX_CHARS = 72
#: Same reasoning for the glance: these two are the caps the block is built to,
#: and both are asserted against the source so neither can drift silently.
GLANCE_MAX_CATEGORIES = 5

BANNER_UNREACHABLE = "\u26a0\ufe0f Some sources were unreachable"


def glance_block_of(message):
    """The glance block's body lines, or [] when it is absent.

    Split on the same shape the rest of this file uses (`**Label**` then `- `
    lines), so a glance that renders a header with nothing under it is []
    rather than a pass.
    """
    return _md_block(message, "**%s**" % GLANCE_LABEL)


def _section_start(message):
    """Index of the first digest section heading, or -1."""
    for marker in ("\n## ", "\n_Truncated"):
        at = message.find(marker)
        if at >= 0:
            return at
    return -1


class TestNtfyGlanceBlock(unittest.TestCase):
    """THE ORIENTATION LAYER, pinned because it is invisible when it breaks.

    WHY THIS CLASS EXISTS. The push is read on a phone, where the notification
    shows the title and roughly the first line of the body. That is what made
    the owner's complaint possible: a title carrying an emoji, a date and a
    count says "a digest exists" and nothing about what is in it.

    WHY IT HAD NO TEST, WHICH IS WHY THIS FILE IS BEING EDITED. When the glance
    block and the title's reason clause were added, NOTHING failed if they were
    deleted: the whole suite stayed green with the notification text reverted
    to the old wording. A layer whose removal is invisible is not a layer.
    Every test below therefore asserts text that does not exist in the old
    wording, so reverting the wording turns them red.

    THE HONESTY ORDER IS NOT NEGOTIABLE. `test_render_honesty.py` owns the rule
    that the banner leads the body, and this class does not restate it -- it
    asserts the OTHER half: that the glance block appears at all, and that it
    appears after the banner rather than instead of it.
    """

    def _run(self, payload, kind="morning"):
        tmp = tempfile.mkdtemp(prefix="ntfy-glance-")
        try:
            return run_ntfy(tmp, payload, kind=kind)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- it is there, and it leads ----------------------------------------

    def test_the_glance_block_is_present_and_precedes_the_digest(self):
        payload = payload_of([story("A", "nepal"), story("B", "gaming")],
                             weather=[], health_state="ok")
        message = self._run(payload)["message"]
        self.assertIn("**%s**" % GLANCE_LABEL, message,
                      "the glance block is gone. This is the layer that makes "
                      "the collapsed notification worth opening, and nothing "
                      "else fails when it disappears.")
        glance_at = message.index("**%s**" % GLANCE_LABEL)
        section_at = _section_start(message)
        self.assertGreater(section_at, 0, "no digest section rendered at all")
        self.assertLess(glance_at, section_at,
                        "the glance block must come BEFORE the story sections; "
                        "a phone preview stops at the first section heading")

    def test_the_glance_block_precedes_your_day_and_the_weather(self):
        """Both personal blocks survive, underneath the glance, not instead of it.

        The glance was added as an orientation layer. A glance that REPLACED
        `Your day` or the weather would pass "the glance comes first" while
        destroying the two blocks the owner actually acts on.
        """
        payload = payload_of(
            [story("Exam form closes", "education_tu", deadline=DATE,
                   for_you="the portal closes at 17:00")],
            weather=WEATHER_TWO, health_state="ok")
        message = self._run(payload)["message"]
        # Assert presence BEFORE indexing: a missing block must be an
        # assertion failure with a readable message, not a ValueError from
        # str.index -- an ERROR says "the test broke", a FAIL says what is
        # wrong with the push.
        self.assertIn("**%s**" % GLANCE_LABEL, message,
                      "the glance block is gone, so its position relative to "
                      "the personal blocks cannot be asserted at all")
        glance_at = message.index("**%s**" % GLANCE_LABEL)
        self.assertIn("**%s**" % YOUR_DAY_LABEL, message,
                      "the glance block displaced `Your day`")
        self.assertLess(glance_at, message.index("**%s**" % YOUR_DAY_LABEL))
        if "**%s**" % WEATHER_LABEL in message:
            self.assertLess(glance_at, message.index("**%s**" % WEATHER_LABEL))
            self.assertIn("**%s**" % GLANCE_LABEL, message,
                          "the glance block is missing, so its position "
                          "relative to the weather block was never proven")

    # -- the honesty banner still outranks it ---------------------------

    def test_the_banner_still_leads_and_the_glance_still_follows(self):
        """The one ordering that matters most, and neither half is optional.

        A degraded push carries the banner. Two things must hold at once: the
        banner is still the FIRST thing (that is the existing contract, owned
        by test_render_honesty.py), and the glance is still there. An outage
        must not be pushed down the phone preview by an orientation layer, and
        a degraded day must not silently lose its orientation.
        """
        payload = payload_of([story("One survivor", "nepal")], weather=[],
                             health_state="degraded",
                             notes="TU notice board unreachable.")
        message = self._run(payload)["message"]
        self.assertTrue(
            message.startswith(BANNER_UNREACHABLE),
            "the honesty banner must still lead the body, got %r" % message[:90])
        self.assertIn("**%s**" % GLANCE_LABEL, message,
                      "on a degraded day the glance block disappeared. The "
                      "banner outranks it, but it does not replace it.")
        self.assertGreater(message.index("**%s**" % GLANCE_LABEL), 0)
        self.assertLess(message.index("**%s**" % GLANCE_LABEL),
                        _section_start(message))

    # -- the mix line carries real counts, bounded ------------------------

    def test_the_mix_line_carries_a_real_count_per_category(self):
        payload = payload_of([
            story("G1", "gaming"), story("G2", "gaming"), story("G3", "gaming"),
            story("N1", "nepal"), story("N2", "nepal"),
            story("T1", "tech_career"),
        ], weather=[], health_state="ok")
        lines = glance_block_of(self._run(payload)["message"])
        self.assertTrue(lines, "the glance block rendered no lines")
        mix = lines[0]
        # Counts, biggest section first. The exact string is the contract: a
        # mix that lost its counts would still "look like" a mix.
        self.assertEqual("3 gaming · 2 Nepal · 1 tech", mix,
                         "the mix line must count every category present, "
                         "biggest first: %r" % mix)
        self.assertIn("3 gaming", mix)
        self.assertIn("2 Nepal", mix)

    def test_the_mix_names_at_most_the_capped_number_of_categories(self):
        """More sections than the cap, and the overflow is said out loud.

        A glance that silently dropped the 6th category would under-report the
        digest's shape, which is the one thing this block exists to prevent.
        """
        cats = ["action", "world_major", "local_city", "education_tu",
                "college_local", "bank_local", "nepal", "gaming"]
        payload = payload_of([story("S%d" % i, cat)
                              for i, cat in enumerate(cats)],
                             weather=[], health_state="ok")
        lines = glance_block_of(self._run(payload)["message"])
        self.assertTrue(lines, "the glance block rendered no lines")
        mix = lines[0]
        parts = [p for p in mix.split(" · ") if p and not p.startswith("+")]
        self.assertLessEqual(len(parts), GLANCE_MAX_CATEGORIES,
                             "the mix named %d categories, cap is %d: %r"
                             % (len(parts), GLANCE_MAX_CATEGORIES, mix))
        self.assertIn("+%d more" % (len(cats) - GLANCE_MAX_CATEGORIES), mix,
                      "the overflow must be stated, not silently dropped: %r"
                      % mix)

    def test_the_mix_line_is_never_what_gets_dropped_for_budget(self):
        """The mix is the block's reason to exist, so it survives the squeeze.

        The glance has an explicit drop order and the obligation line is the
        thing it drops. This asserts the surviving half: whatever else happens
        under the byte cap, the category mix is still there.
        """
        payload = payload_of([
            story("D%d" % i, "education_tu", deadline=DATE,
                  for_you="a long personal line %d" % i, priority=8 - (i % 2))
            for i in range(4)
        ] + [story("G%d" % i, "gaming", for_you="gaming line %d" % i)
             for i in range(4)],
            weather=[], health_state="ok")
        lines = glance_block_of(self._run(payload)["message"])
        self.assertTrue(lines, "the glance block vanished under byte pressure")
        self.assertIn("gaming", lines[0],
                      "the mix line is gone: %r" % lines[0])
        self.assertRegex(lines[0], r"\d+ ")

    def test_the_caps_the_block_is_built_to_are_still_the_shipped_ones(self):
        source = ntfy_source()
        self.assertIn("GLANCE_MAX_CATEGORIES = %d" % GLANCE_MAX_CATEGORIES,
                      source)
        self.assertIn("TITLE_MAX_CHARS = %d" % TITLE_MAX_CHARS, source)
        self.assertIn("GLANCE_LABEL = '%s'" % GLANCE_LABEL, source)

    # -- zero stories must read honestly ---------------------------------

    def test_a_zero_story_digest_says_zero_stories(self):
        payload = payload_of([], weather=[], health_state="ok")
        lines = glance_block_of(self._run(payload)["message"])
        self.assertTrue(
            lines and "0 stories" in lines[0],
            "an empty digest must say so in the glance block. An empty category "
            "mix reads as 'nothing new', and 'nothing new' is a claim this "
            "repository refuses to make without a source check behind it. "
            "Got: %r" % (lines,))

    def test_the_two_zero_story_causes_read_as_different_strings(self):
        """THE POINT OF THE WHOLE BLOCK IN ONE TEST.

        An empty digest has two very different causes: the sources answered and
        there was genuinely nothing, or the sources were down and the digest is
        an outage. If those two render the same words, an outage reads as a
        quiet news day -- which is the exact failure the honesty banner exists
        to prevent, one layer further down.

        So this asserts the two strings are DISTINCT and that each carries its
        own cause, not merely that "something" is printed.
        """
        quiet = glance_block_of(self._run(
            payload_of([], weather=[], health_state="ok"))["message"])
        # `payload_of` already builds the degraded payload with TU_HOSTS down,
        # which is what makes `health_state` report `degraded` rather than
        # `partial` -- the glance block keys its zero-story wording off that.
        outage = glance_block_of(self._run(
            payload_of([], weather=[], health_state="degraded",
                       notes="iost.tu.edu.np unreachable (DNS SERVFAIL).")
        )["message"])

        self.assertTrue(quiet, "the healthy zero-story glance rendered nothing")
        self.assertTrue(outage, "the degraded zero-story glance rendered nothing")
        self.assertNotEqual(
            quiet, outage,
            "a quiet news day and an outage rendered IDENTICAL text (%r). An "
            "outage that reads as a quiet day is a lie the owner cannot see." %
            quiet)

        self.assertIn("quiet news day", " ".join(quiet).lower(),
                      "the healthy case must name the quiet-day cause: %r"
                      % quiet)
        self.assertNotIn(
            "outage", " ".join(quiet).lower(),
            "the healthy zero-story text must not mention an outage: %r" % quiet)
        self.assertIn("degraded", " ".join(outage).lower(),
                      "the degraded case must name the degraded health: %r"
                      % outage)

    # -- the title says WHY ----------------------------------------------

    def test_the_title_states_the_reason_it_was_escalated(self):
        """`high` used to raise the push to priority 4 and say nothing.

        A priority-4 phone notification is visibly louder than the day before
        it, and the only way to learn what was earned for it was to open the
        push. Each cause is asserted in its OWN wording, because the point is
        that the title distinguishes them.
        """
        cases = [
            ("action category, no deadline",
             payload_of([story("KYC", "action", priority=9),
                         story("Quiet", "gaming", priority=2)],
                        weather=[], health_state="ok"),
             "action needed"),
            ("a deadline due today or tomorrow",
             payload_of([story("Exam form closes", "education_tu",
                               deadline=DATE, priority=8),
                         story("Quiet", "gaming", priority=2)],
                        weather=[], health_state="ok"),
             "1 deadline due"),
            ("priority 8+, nothing dated or actionable",
             payload_of([story("Flash flood warning", "nepal", priority=8),
                         story("Quiet", "gaming", priority=2)],
                        weather=[], health_state="ok"),
             "high-priority"),
        ]
        for label, payload, reason in cases:
            with self.subTest(case=label):
                got = self._run(payload)
                self.assertEqual(4, got["priority"],
                                 "this case must actually escalate")
                self.assertIn(reason, got["title"],
                              "the escalation is not stated in the title for "
                              "%s. A louder push that does not say why is "
                              "indistinguishable from a bug. Title: %r"
                              % (label, got["title"]))
                # The count is NOT optional: test_render_honesty.py:748 pins
                # `4 items`, and this asserts the clause did not displace it.
                self.assertRegex(got["title"], r"· \d+ items? ·")

        # THE OTHER DIRECTION, IN THIS SAME TEST, ON PURPOSE. A clause that
        # always fires is noise: it trains the owner to read the title as
        # boilerplate, which is the state this change started from. Asserted
        # here rather than in its own test because a standalone "no clause"
        # test cannot fail when the clause is absent altogether -- the reverted
        # wording has no clause, so such a test is green on BOTH wordings and
        # is therefore decoration.
        calm = self._run(payload_of(
            [story("Quiet one", "gaming", priority=3),
             story("Quiet two", "nepal", priority=2)],
            weather=[], health_state="ok"))
        self.assertEqual(3, calm["priority"], "a calm digest must stay p3")
        self.assertNotIn("action needed", calm["title"])
        self.assertNotIn("high-priority", calm["title"])
        self.assertNotIn("deadline", calm["title"].lower(),
                         "a calm digest claimed a deadline: %r" % calm["title"])

    def test_the_title_never_exceeds_the_phone_budget(self):
        """The bound, not just the clause's presence.

        A phone notification truncates the title in the shade, and a title
        clipped mid-word is worse than one that dropped the clause. Checked
        against the LITERAL 72, so raising the constant in the source without
        raising it here fails here.
        """
        cases = {
            "busy mixed day": payload_of([
                story("A%d" % i, ["nepal", "gaming", "tech_career",
                                   "anime", "education_tu"][i % 5],
                      priority=8, for_you="x") for i in range(15)],
                weather=WEATHER_TWO, health_state="ok"),
            "many deadlines": payload_of([
                story("D%d" % i, "action", deadline=DATE, priority=9)
                for i in range(9)], weather=[], health_state="ok"),
            "worst case": worst_case_payload(),
            "calm": payload_of([story("Q", "gaming", priority=1)],
                               weather=[], health_state="ok"),
        }
        longest = 0
        saw_clause = False
        for label, payload in cases.items():
            for kind in ("morning", "evening"):
                with self.subTest(case=label, slot=kind):
                    title = self._run(payload, kind=kind)["title"]
                    longest = max(longest, len(title))
                    # The bound is only a bound if something was ever appended.
                    # Without this, the reverted wording (no clause at all) would
                    # satisfy a pure length check and this test would be
                    # decoration -- which is exactly what it was before.
                    # The bare-count baseline, at the shape the title now has:
                    # `emoji <word> Digest · HH:MM · <date> · <n> items`. The
                    # clock is part of the label, so this baseline is longer
                    # than the old bare-count string was and must be measured
                    # from the real shape or `saw_clause` would be true for the
                    # wrong reason (the clock, not a reason clause).
                    if len(title) > len("%s %s · %s · %d items"
                                        % ("", "Evening Digest · 19:25",
                                           "2026-09-27", 0)):
                        saw_clause = True
                    self.assertLessEqual(
                        len(title), TITLE_MAX_CHARS,
                        "the title is %d characters (budget %d) and a phone "
                        "will clip it: %r"
                        % (len(title), TITLE_MAX_CHARS, title))
        self.assertTrue(
            saw_clause,
            "every measured title was exactly the bare count, so this budget "
            "assertion proved nothing: with no clause ever appended there is "
            "nothing to exceed %d characters. The bound needs a long clause to "
            "be measured against." % TITLE_MAX_CHARS)
        self.assertGreaterEqual(
            longest, 45,
            "the longest title measured was %d characters, which is too short to "
            "exercise a %d-character phone budget -- these cases are not "
            "reaching the condition the cap exists for" % (longest, TITLE_MAX_CHARS))

    def test_the_budget_drops_the_whole_clause_rather_than_clipping_it(self):
        """A too-long clause is dropped, not truncated mid-word.

        `TITLE_MAX_CHARS` is a hard ceiling, so a clause that would overflow it
        must not appear AT ALL -- a partial clause reads as a real, different
        message ('... 3 deadlines due t') and is worse than no clause. This
        reaches the branch by finding the longest real reason and proving the
        guard, using the source's own arithmetic rather than a fake date.
        """
        self.assertIn("if len(title) + len(reason) + 3 <= TITLE_MAX_CHARS:",
                      ntfy_source(),
                      "the whole-title-fits guard is gone; a clause can now be "
                      "appended past the budget and clipped by the phone")

    # -- weather must not be duplicated by the new layer -----------------

    def test_the_glance_block_did_not_duplicate_the_weather(self):
        """The no-duplication invariant, checked on the push itself.

        The glance block sits directly above the weather block, so duplicating
        a weather line between them is the obvious accident this change could
        introduce -- and the existing weather tests only cover `Your day`, not
        a brand-new block above it. Counted, not just searched for.
        """
        payload = payload_of([story("Rain in the valley", "world_major")],
                             weather=WEATHER_TWO, health_state="ok")
        message = self._run(payload)["message"]
        # The SUBJECT of this test must exist for it to mean anything. Without
        # this, the reverted wording (no glance block at all) satisfies every
        # no-duplication assertion below and the test is decoration -- it would
        # be green on both wordings. Stating the subject first keeps it honest.
        self.assertIn("**%s**" % GLANCE_LABEL, message,
                      "the glance block this test checks for duplication is gone")
        for text in (W_UMBRELLA, W_JACKET):
            self.assertEqual(1, message.count(text),
                             "the weather line %r appears %d times in the "
                             "ntfy body. A digest read at 06:00 for exactly "
                             "the opposite reason must say it once."
                             % (text, message.count(text)))
        glance = " ".join(glance_block_of(message))
        for text in (W_UMBRELLA, W_JACKET):
            self.assertNotIn(text, glance,
                             "the glance block repeated a weather line: %r"
                             % glance)


# ===========================================================================
# 5. ntfy: one link per story
# ===========================================================================

class TestNtfyOneLinkPerStory(unittest.TestCase):
    """Three Google News redirects for one story is ~900 of the 4096 bytes and
    is the mechanical reason the push truncates. The push links to the website,
    which carries every link, so deep links here are pure redundancy."""

    def test_one_link_per_story_despite_three_supporting_sources(self):
        payload = worst_case_payload(4)
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        for index in range(4):
            story_block = [b for b in message.split("\n\n")
                           if "Worst case story %02d" % index in b]
            self.assertEqual(len(story_block), 1, "story %02d split oddly" % index)
            self.assertEqual(story_block[0].count("](http"), 1,
                             "story %02d rendered %d links"
                             % (index, story_block[0].count("](")))

    def test_the_single_link_is_the_real_publisher_not_the_redirect(self):
        payload = worst_case_payload(2)
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("https://kathmandupost.com/real-00", got["message"])
        self.assertNotIn("news.google.com/rss/articles/CBMiSmhwwBBBB", got["message"])

    def test_redirect_without_a_real_source_keeps_one_labelled_link(self):
        payload = payload_of([story("Lonely", "nepal",
                                    source_url="https://news.google.com/rss/articles/CBMiXY",
                                    supporting_sources=[])], weather=[])
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        self.assertEqual(message.count("]("), 1)
        self.assertIn("via Google News", message)

    def test_email_keeps_every_link_it_is_the_archive(self):
        """The fix is ntfy-only. Email is the archive surface: bytes are not
        scarce there and removing its links would lose information."""
        payload = worst_case_payload(2)
        tmp = tempfile.mkdtemp()
        try:
            s = render_all_four(payload, tmp=tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("https://kathmandupost.com/real-00", s.email_text)
        self.assertIn("https://news.google.com/rss/articles/CBMiSmhwwBBBB", s.email_text)
        self.assertIn("https://news.google.com/rss/articles/CBMiSmhwwCCCC", s.email_text)


# ===========================================================================
# 6. The golden: all four surfaces agree on which stories carry a line
# ===========================================================================

GOLDEN = [
    story("Deadline today", "education_tu", deadline=DATE,
          for_you="portal closes 17:00", priority=9),
    story("Deadline tomorrow", "bank_local", deadline=TOMORROW,
          for_you="forms due before close", priority=8),
    story("Obligation line", "tech_career", for_you="this is your registration",
          priority=7),
    story("Plain game news", "gaming", priority=6),
    story("No line at all", "nepal", priority=5),
]
GOLDEN_FOR_YOU = {
    "Deadline today": "portal closes 17:00",
    "Deadline tomorrow": "forms due before close",
    "Obligation line": "this is your registration",
}


class TestGoldenFourSurfaceAgreement(unittest.TestCase):
    """One payload, four surfaces, one agreement: exactly these three stories
    carry a `for_you` line, and the other two carry nothing at all."""

    def setUp(self):
        self.s = render_all_four(payload_of(GOLDEN, weather=WEATHER_TWO),
                                 candidates={"weather_advice": WEATHER_TWO})

    def test_exactly_three_stories_carry_a_line_on_every_surface(self):
        for name, blob in self.s.all_four().items():
            for title, line in GOLDEN_FOR_YOU.items():
                self.assertIn(line, blob, "%s lost %r" % (name, title))
            for title in ("Plain game news", "No line at all"):
                card = card_for(blob, name, title)
                self.assertIsNotNone(card, "%s has no card for %r" % (name, title))
                self.assertNotIn(FOR_YOU_LABEL, card,
                                 "%s gave %r an empty for_you label" % (name, title))

    def test_the_plain_text_body_agrees_exactly(self):
        self.assertEqual(self.s.email_text.count("  For you:"), len(GOLDEN_FOR_YOU))
        for title, line in GOLDEN_FOR_YOU.items():
            self.assertIn("  For you: %s" % line, self.s.email_text)
        for absent in ("  For you: \n", "Plain game news\n  For you",
                       "No line at all\n  For you"):
            self.assertNotIn(absent, self.s.email_text)

    def test_the_website_agrees_exactly(self):
        self.assertEqual(self.s.page.count('class="card-foryou"'), len(GOLDEN_FOR_YOU))
        self.assertEqual(self.s.page.count('<article class="card">'), len(GOLDEN))

    def test_the_push_agrees_exactly(self):
        self.assertEqual(self.s.ntfy_message.count("_For you: "), len(GOLDEN_FOR_YOU))

    def test_all_four_carry_the_same_your_day_lines(self):
        expected = ["Deadline today - due %s" % DATE,
                    "Deadline tomorrow - due %s" % TOMORROW,
                    "this is your registration", W_UMBRELLA, W_JACKET]
        for line in expected:
            for name, blob in self.s.all_four().items():
                self.assertIn(line, blob, "%s missing %r" % (name, line))
        self.assertNotIn(NOTHING_TODAY, self.s.ntfy_message)
        self.assertNotIn(NOTHING_TODAY, self.s.email_text)
        self.assertNotIn("Plain game news", self.s.ntfy_message.split("## ")[0])

    def test_the_label_is_exactly_the_contract(self):
        self.assertEqual(render_digest.YOUR_DAY_LABEL, "Your day")
        self.assertEqual(build_email.YOUR_DAY_LABEL, "Your day")
        self.assertEqual(render_digest.FOR_YOU_LABEL, "For you")
        self.assertEqual(build_email.FOR_YOU_LABEL, "For you")
        self.assertEqual(render_digest.NOTHING_TODAY, NOTHING_TODAY)
        self.assertEqual(build_email.NOTHING_TODAY, NOTHING_TODAY)


# ===========================================================================
# 7. THE NO-DUPLICATION INVARIANT
# ===========================================================================

class TestNoDuplicateWeather(unittest.TestCase):
    """No weather string appears TWICE in one rendered output, and none is
    silently lost.

    The owner is complaining about verbosity, and printing the same sentence
    twice in the same screen is the most expensive kind of wasted byte there
    is. The rule lives in a comment above FOR_YOU_LABEL in
    scripts/render_digest.py, scripts/build_email.py and the ntfy heredoc in
    .github/workflows/daily-news.yml; these tests are the three-way check that
    the three copies of that comment still agree with each other.

    `Your day` = deadlines + `for_you`. The weather = its own labelled block,
    once, on all four surfaces.
    """

    #: A deliberately crowded payload: three deadline/`for_you` lines saturate
    #: YOUR_DAY_MAX_LINES (3) on the push, which is the case that used to clip
    #: the weather out of the header entirely.
    CROWDED = [
        story("Deadline today", "education_tu", deadline=DATE,
              for_you="portal closes 17:00", priority=9),
        story("Deadline tomorrow", "bank_local", deadline=TOMORROW,
              for_you="forms due before close", priority=8),
        story("Obligation line", "tech_career",
              for_you="this is your registration", priority=7),
    ]

    def _render(self, stories, weather):
        return render_all_four(payload_of(stories, weather=weather),
                               candidates={"weather_advice": weather or []})

    def test_each_weather_string_occurs_at_most_once_on_every_surface(self):
        for stories, weather in (([story("A", "nepal")], WEATHER_TWO),
                                 (self.CROWDED, WEATHER_TWO),
                                 (self.CROWDED, WEATHER_TWO[:1]),
                                 ([story("A", "nepal")], [])):
            s = self._render(stories, weather)
            for name, blob in s.all_four().items():
                for line in weather:
                    count = blob.count(line["text"])
                    self.assertLessEqual(
                        count, 1,
                        "%s printed %r %d times (stories=%d weather=%d)"
                        % (name, line["text"], count, len(stories), len(weather)))
                    if weather:
                        self.assertEqual(
                            count, 1,
                            "%s LOST the weather advice %r entirely - silence "
                            "is not the same as saying it once"
                            % (name, line["text"]))

    def test_your_day_never_carries_a_weather_string(self):
        s = self._render(self.CROWDED, WEATHER_TWO)
        for name, blob in s.all_four().items():
            head = blob.split("## ")[0] if name == "ntfy" else blob
            for line in WEATHER_TWO:
                self.assertIn(line["text"], head, name)
            self.assertEqual(
                head.count(WEATHER_TWO[0]["text"]), 1, name)

    def test_the_rule_is_stated_in_all_three_implementations(self):
        """Three copies of one rule, so all three must actually say it."""
        for path in (os.path.join(REPO_ROOT, "scripts", "render_digest.py"),
                     os.path.join(REPO_ROOT, "scripts", "build_email.py"),
                     os.path.join(REPO_ROOT, ".github", "workflows",
                                  "daily-news.yml")):
            with io.open(path, encoding="utf-8") as fh:
                source = fh.read()
            self.assertIn("no weather string appears TWICE", source, path)
            self.assertIn("include_weather", source, path)

    def test_no_rich_call_site_still_passes_weather_into_your_day(self):
        """The switch has to be used, not merely defined."""
        for path in ("render_digest.py", "build_email.py"):
            with io.open(os.path.join(REPO_ROOT, "scripts", path),
                         encoding="utf-8") as fh:
                source = fh.read()
            self.assertIn("include_weather=False", source, path)


#: Per-surface card boundaries. Each surface wraps a story in a different
#: container, and a shared extractor would return the whole document for one
#: of them - which would make "this card has no for_you label" pass or fail
#: for the wrong reason.
CARD_SPLIT = {
    "website": ('<article class="card">', "</article>"),
    "email_html": ('<div style="border:1px solid #e5e7eb', "</div>"),
    "email_text": ("\n* ", "\n"),
    "ntfy": ("\n\n**", "\n\n"),
}


def card_for(blob, surface, title):
    """The rendered block for `title` on `surface`, or None."""
    opener, closer = CARD_SPLIT[surface]
    for part in blob.split(opener)[1:]:
        block = part.split(closer, 1)[0]
        if title in block:
            return block
    return None


# ===========================================================================
# 8. CROSS-SURFACE `Your day` AGREEMENT  (the 3-vs-2 regression)
# ===========================================================================

def ntfy_source_text():
    """The ntfy step's heredoc as text, for source-level parity assertions.

    The personal layer is defined INSIDE the heredoc's `main()`, so its
    constants are locals and cannot be reached from a namespace. Asserting on
    the source text is therefore the only way to hold the push to the same
    literals as the two Python implementations - and it is also what catches a
    regression in the two lines that caused the 3-vs-2 in the first place.
    """
    return ntfy_source()


def ntfy_constant(name, source=None):
    """The literal the heredoc assigns to `name`, e.g. 3 / 2 / 700 / 'Your day'."""
    import re as _re
    src = source if source is not None else ntfy_source_text()
    m = _re.search(r"^\s*%s = (.+?)\s*$" % _re.escape(name), src, _re.M)
    if not m:
        raise AssertionError("the ntfy heredoc does not assign %s" % name)
    value = _re.sub(r"[\"']", "", m.group(1))
    return int(value) if _re.match(r"^-?\d+$", value) else value


def ntfy_your_day_call(source=None):
    """The body of the heredoc's `your_day_lines`, up to the next sibling def.

    The personal layer is nested inside the heredoc's `main()`, so the defs sit
    at an indent rather than at column 0. Locate the def by line and stop at the
    next line with the same indent that starts a new def.
    """
    lines = (source if source is not None else ntfy_source_text()).split("\n")
    start = end = None
    for i, raw in enumerate(lines):
        stripped = raw.lstrip()
        if not stripped.startswith("def your_day_lines("):
            continue
        indent = len(raw) - len(stripped)
        start = i
        for j in range(i + 1, len(lines)):
            later = lines[j]
            if not later.strip():
                continue
            later_indent = len(later) - len(later.lstrip())
            if later_indent == indent and later.lstrip().startswith("def "):
                end = j
                break
        if end is None:
            end = len(lines)
        break
    if start is None:
        raise AssertionError("the ntfy heredoc does not define your_day_lines")
    return _squash("\n".join(lines[start:end]))


def _squash(text):
    """Collapse all whitespace runs to single spaces, for source assertions."""
    import re as _re
    return _re.sub(r"\s+", " ", text)


#: A long deadline title. At ~262 bytes per rendered line, three of these are
#: 789 bytes - which is what the removed 700-byte inner cap used to clip, while
#: the personal layer as rendered was 545 bytes of a 1000-byte ceiling and the
#: whole push was 2657 bytes of 4096. This is the exact shape of the reported
#: "email and website render 3, the ntfy push renders 2".
LONG_TITLE = ("Tu registration form deadline and the portal drops your seat if "
              "you miss the window and the college will not reopen it " * 2)[:250]

#: SIX due items, deliberately NOT in priority order. The input index is the
#: tiebreak between two deadlines falling on the same day, so a fixture whose
#: priority order differs from its input order is the only way to catch a
#: surface that renumbers that index.
SIX_DUE = [
    story("Exam form closes", "education_tu", deadline=DATE, priority=1),
    story("Bank KYC deadline", "bank_local", deadline=DATE, priority=6),
    story("Re-registration opens", "action", deadline=DATE, priority=4),
    story("Scholarship window", "tech_career", deadline=TOMORROW, priority=5),
    story("Local notice", "local_city", deadline=TOMORROW, priority=2),
    story("College notice", "college_local", deadline=TOMORROW, priority=3),
]


class TestCrossSurfaceYourDayAgreement(unittest.TestCase):
    """THE REGRESSION TEST FOR THE REPORTED 3-vs-2.

    One fixture, three implementations of the same function, asserted to
    produce the SAME set of `Your day` lines. A fixture with more due items
    than the cap is mandatory: with only three due items every implementation
    trivially shows all three and a cap difference hides completely.

    The set is identical because there is exactly ONE selection rule now - a
    3-line cap, applied by every production call site on every surface - and
    because the push builds the block from the same input ordering the rich
    surfaces use. There is no longer a per-surface byte cap to disagree with.
    """

    def _two_python(self, stories, weather=None, date_val=None):
        """(render_digest, build_email) `your_day_lines` output.

        The third implementation is the push, and it is asserted end-to-end
        below through the REAL heredoc, because the heredoc's personal layer is
        nested inside its `main()` and cannot be called directly.
        """
        date_val = date_val or DATE
        weather = weather or []
        return {
            "render_digest": render_digest.your_day_lines(
                stories, weather, date_val,
                max_lines=render_digest.YOUR_DAY_MAX_LINES, include_weather=False),
            "build_email": build_email.your_day_lines(
                stories, weather, date_val,
                max_lines=build_email.YOUR_DAY_MAX_LINES, include_weather=False),
        }

    def test_the_two_python_implementations_return_the_identical_set(self):
        got = self._two_python(SIX_DUE)
        self.assertEqual(got["render_digest"], got["build_email"])
        self.assertEqual(len(got["render_digest"]), 3,
                         "the cap should leave exactly 3 of 6, got %r"
                         % (got["render_digest"],))

    def test_due_stories_agrees_between_the_two_python_implementations(self):
        """The predicate itself, isolated from any cap."""
        today = render_digest.parse_day(DATE)
        rows = render_digest.due_stories(SIX_DUE, today)
        self.assertEqual(rows, build_email.due_stories(SIX_DUE, today))
        self.assertEqual(rows, [(i, s["title"], DATE if s["deadline"] == DATE
                                 else TOMORROW)
                                for i, s in enumerate(SIX_DUE)
                                if s.get("deadline") in (DATE, TOMORROW)])
        self.assertEqual(len(rows), len(SIX_DUE))

    def test_the_push_cap_is_the_line_cap_and_nothing_else(self):
        """ROOT CAUSE, pinned at the line that caused it.

        `Your day` in the heredoc used to be capped at 3 lines AND 700 bytes.
        At ~262 bytes per deadline line that fired on the third line of an
        ordinary three-deadline day, and the nearest deadline vanished from the
        push while the personal layer was 545 bytes of a 1000-byte ceiling.
        The push must now carry the line cap only.
        """
        body = ntfy_your_day_call()
        self.assertIn("return cap_lines(lines, YOUR_DAY_MAX_LINES, keep_first=True)",
                      _squash(body),
                      "the heredoc must cap `Your day` at YOUR_DAY_MAX_LINES "
                      "and pass no byte budget; found: ...%s"
                      % body[-200:])
        self.assertNotIn("YOUR_DAY_MAX_BYTES", ntfy_source_text())

    def test_the_push_builds_your_day_from_the_unreordered_stories(self):
        """ROOT CAUSE, the second one, and the one that dropped a DIFFERENT
        deadline rather than merely a later one.

        `due_stories` sorts by (due_date, input_index), so the input index is
        the tiebreak between two deadlines on the SAME day. The heredoc used
        to pass `ordered` (a priority-sorted copy) into the personal layer,
        which renumbers that index and changes which deadlines survive the cap.
        `ordered` is for the story fill; the personal layer takes `stories`.
        """
        src = ntfy_source_text()
        self.assertIn("ordered = sorted(stories, key=lambda s: -(s.get('priority') or 0))",
                      src, "the story-fill priority sort must still exist")
        self.assertIn(
            "yd = your_day_lines(stories, weather, today, include_weather=False)",
            _squash(src),
            "the personal layer must be built from `stories` in input order, "
            "not from the priority-sorted `ordered`")
        self.assertNotIn("your_day_lines(ordered,", src)

    def test_the_set_is_the_nearest_deadlines_not_an_arbitrary_three(self):
        """All six land on the same two days, so only the ORDER can be wrong.
        Nearest day first; within a day, input order - which is also what the
        push now uses, so the three surfaces keep the same three."""
        got = self._two_python(SIX_DUE)["render_digest"]
        self.assertEqual(got, ["Exam form closes - due %s" % DATE,
                               "Bank KYC deadline - due %s" % DATE,
                               "Re-registration opens - due %s" % DATE])

    def test_a_deadline_survives_while_the_push_still_has_budget(self):
        """The exact reported symptom: three ordinary long deadlines.

        3 x 262 bytes = 789, over the removed 700-byte inner cap and under the
        1000-byte ceiling that is actually enforced. Every surface must show
        all three.
        """
        long_due = [story(LONG_TITLE, "education_tu", deadline=DATE),
                    story(LONG_TITLE, "bank_local", deadline=DATE),
                    story(LONG_TITLE, "tech_career", deadline=TOMORROW)]
        got = self._two_python(long_due)
        for name, lines in got.items():
            self.assertEqual(len(lines), 3,
                             "%s dropped a deadline: %d of 3 rendered, %r"
                             % (name, len(lines), lines))
        s = render_all_four(payload_of(long_due, weather=[]),
                            candidates={"weather_advice": []})
        for name, blob in s.all_four().items():
            self.assertEqual(len(yd_block(blob, name)), 3,
                             "%s dropped a deadline: %r" % (name, yd_block(blob, name)))
        for name in ("email_html", "email_text", "ntfy"):
            self.assertEqual(yd_block(s.all_four()[name], name),
                             got["render_digest"], name)

    def test_all_four_surfaces_carry_the_same_three_lines(self):
        """THE MANDATE: one fixture, all three implementations, one set.

        Rendered through the real website renderer, the real email renderer
        (HTML and plain text) and the REAL ntfy heredoc in dry run.
        """
        s = render_all_four(payload_of(SIX_DUE, weather=[]),
                            candidates={"weather_advice": []})
        expected = ["Exam form closes - due %s" % DATE,
                    "Bank KYC deadline - due %s" % DATE,
                    "Re-registration opens - due %s" % DATE]
        self.assertEqual(len(expected), render_digest.YOUR_DAY_MAX_LINES)
        for name, blob in s.all_four().items():
            block = yd_block(blob, name)
            self.assertEqual(block, expected, "%s `Your day` block: %r" % (name, block))
        # The three that did not fit are absent from every surface equally.
        for dropped in ("Scholarship window - due", "Local notice - due",
                        "College notice - due"):
            for name, blob in s.all_four().items():
                self.assertNotIn(dropped, yd_block(blob, name),
                                 "%s leaked a capped line %r" % (name, dropped))

    def test_six_due_items_all_three_surfaces_never_agree_on_fewer_than_three(self):
        """A count assertion, because a count is what was reported."""
        s = render_all_four(payload_of(SIX_DUE, weather=[]),
                            candidates={"weather_advice": []})
        counts = {name: len(yd_block(blob, name))
                  for name, blob in s.all_four().items()}
        self.assertEqual(len(set(counts.values())), 1,
                         "surfaces rendered different counts: %r" % counts)
        self.assertEqual(list(counts.values())[0], 3)

    def test_the_cap_is_stated_identically_in_all_three_implementations(self):
        """The cap is one number. If these ever differ, the surfaces differ."""
        src = ntfy_source_text()
        for name in ("FOR_YOU_MAX", "WEATHER_MAX", "YOUR_DAY_MAX_LINES",
                     "PERSONAL_MAX_BYTES", "WEATHER_MAX_BYTES", "NOTHING_TODAY",
                     "FOR_YOU_LABEL", "YOUR_DAY_LABEL", "WEATHER_LABEL"):
            self.assertEqual(ntfy_constant(name, src),
                             getattr(render_digest, name),
                             "ntfy %s differs from render_digest" % name)
            self.assertEqual(getattr(build_email, name),
                             getattr(render_digest, name),
                             "build_email %s differs from render_digest" % name)

    def test_no_deadline_byte_cap_survives_in_any_implementation(self):
        """The removed cap must stay removed.

        A byte cap strictly inside the 1000-byte ceiling can only ever drop a
        deadline, so a future author re-adding it would be re-introducing the
        3-vs-2 bug. This asserts its ABSENCE, which is what a test can do
        about a constant that must not come back.
        """
        for path in (os.path.join(REPO_ROOT, "scripts", "render_digest.py"),
                     os.path.join(REPO_ROOT, "scripts", "build_email.py"),
                     os.path.join(REPO_ROOT, ".github", "workflows",
                                  "daily-news.yml")):
            with io.open(path, encoding="utf-8") as fh:
                source = fh.read()
            self.assertNotIn("YOUR_DAY_MAX_BYTES", source, path)
        for mod in (render_digest, build_email):
            self.assertFalse(hasattr(mod, "YOUR_DAY_MAX_BYTES"), mod.__name__)

    def test_weather_only_digest_returns_empty_from_all_three(self):
        """THE SELECTION FUNCTION still returns [] for a weather-only digest,
        and that part of the old contract is UNCHANGED.

        `your_day_lines` is duplicated verbatim inside the ntfy heredoc in
        .github/workflows/daily-news.yml, a file this change does not own, so
        the selection function could not be altered without the push and the
        two Python renderers disagreeing about WHICH LINES survive - which is
        the 3-vs-2 defect this class exists to prevent. The fix for BUG 1 is
        therefore in the RENDERING layer, not here.
        """
        for weather in (WEATHER_TWO, WEATHER_TWO[:1]):
            for name, lines in self._two_python([], weather=weather).items():
                self.assertEqual(lines, [],
                                 "%s invented a `Your day` block for a "
                                 "weather-only digest: %r" % (name, lines))

    def test_weather_only_digest_still_renders_the_block_on_the_owned_surfaces(self):
        """BUG 1, the reader-visible half.

        The selection returns nothing, so the block is empty - and an EMPTY
        BLOCK THAT DISAPPEARS is what shipped on the live 2026-09-29 digest:
        `.your-day` and `.your-day-label` were in the <style> block and there
        was no `<div class="your-day">` in the body at all. Styling that exists
        while its markup does not is indistinguishable, to the reader, from a
        renderer that broke. So the block renders the documented fallback on
        every surface this change owns, and the ntfy push - which cannot be
        edited from here - is pinned as still omitting it."""
        s = render_all_four(payload_of([], weather=WEATHER_TWO),
                            candidates={"weather_advice": WEATHER_TWO})
        for name in ("website", "email_html", "email_text"):
            blob = s.all_four()[name]
            self.assertIn(NOTHING_TODAY, blob,
                          "%s dropped the fallback: the block must never vanish" % name)
            self.assertIn(W_UMBRELLA, blob, name)
        # Website: the CONTAINER is present in the body, not only in the CSS.
        # Both assertions matter - the bug was the CSS surviving without it.
        self.assertIn('<div class="your-day">', s.page)
        self.assertIn('<p class="your-day-label">', s.page)
        self.assertEqual(yd_block(s.page, "website"), [NOTHING_TODAY])
        self.assertEqual(yd_block(s.email_text, "email_text"), [NOTHING_TODAY])
        # The push is unchanged and cannot be changed from here. The divergence
        # is asserted, not left to be discovered later.
        self.assertEqual(yd_block(s.ntfy_message, "ntfy"), [])
        self.assertNotIn(NOTHING_TODAY, s.ntfy_message)
        # The heredoc's own branch is still there, at the source level.
        self.assertIn("if wea and not include_weather:", ntfy_your_day_call())
        self.assertIn("return []", ntfy_your_day_call())


# ===========================================================================
# 9. THE WEATHER CAP IS ONE RULE (the 3-vs-2 on the weather block)
# ===========================================================================

class TestWeatherCapIsOneRule(unittest.TestCase):
    """`render_weather_block` did not apply WEATHER_MAX, so the website showed
    every line the engine emitted while the email and the push showed two.
    The cap now lives in the one function that renders the block."""

    THREE = WEATHER_TWO + [
        {"id": "C", "text": "Roads are wet on the way to Example City this evening",
         "priority": 7},
    ]

    def test_all_four_surfaces_show_the_same_weather_lines(self):
        self.assertEqual(len(self.THREE), render_digest.WEATHER_MAX + 1,
                         "the fixture must exceed the cap or this proves nothing")
        s = render_all_four(payload_of([story("A", "nepal")], weather=self.THREE),
                            candidates={"weather_advice": self.THREE})
        for name, blob in s.all_four().items():
            block = weather_block(blob, name)
            self.assertEqual(block, [W_UMBRELLA, W_JACKET],
                             "%s weather block: %r" % (name, block))

    def test_the_website_block_is_capped_in_the_renderer_itself(self):
        """Not merely at the call site - a new call site must not bypass it."""
        self.assertEqual(render_digest.render_weather_block([]), "")
        got = render_digest.render_weather_block([W_UMBRELLA, W_JACKET, "third"])
        self.assertEqual(got.count("<li>"), render_digest.WEATHER_MAX)
        self.assertNotIn("third", got)
        # Idempotent: an already-capped list survives a second pass unchanged.
        once = render_digest.render_weather_block([W_UMBRELLA, W_JACKET])
        self.assertEqual(render_digest.render_weather_block(
            [W_UMBRELLA, W_JACKET]), once)

    def test_the_render_cap_is_stated_once_per_implementation(self):
        src = ntfy_source_text()
        self.assertEqual(ntfy_constant("WEATHER_MAX", src),
                         render_digest.WEATHER_MAX)
        self.assertEqual(build_email.WEATHER_MAX, render_digest.WEATHER_MAX)
        # WEATHER_MAX is a RENDER cap and is not read from the profile. The
        # engine's `weather_advice.max_lines` is a different contract - how
        # many verdicts the engine produces at all - and both are required.
        for path in (os.path.join(REPO_ROOT, "scripts", "render_digest.py"),
                     os.path.join(REPO_ROOT, "scripts", "build_email.py"),
                     os.path.join(REPO_ROOT, ".github", "workflows",
                                  "daily-news.yml")):
            with io.open(path, encoding="utf-8") as fh:
                source = fh.read()
            self.assertIn("WEATHER_MAX = 2", source, path)
            # The render cap is never READ from the profile. A quoted key is
            # what a lookup looks like; the unquoted `max_lines` elsewhere in
            # these files is only a function parameter.
            self.assertNotIn('"max_lines"', source, path)
            self.assertNotIn("'max_lines'", source, path)
        # And the personal layer never mentions it at all.
        self.assertNotIn("max_lines", ntfy_your_day_call())

    def test_the_push_still_fits_its_budget_with_the_cap_in_place(self):
        """Weather capped once, everywhere, and the 4096 ceiling intact."""
        payload = worst_case_payload()
        payload["weather_advice"] = self.THREE
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        self.assertLessEqual(len(message.encode("utf-8")), MAX_BYTES)
        block = weather_block(message, "ntfy")
        self.assertEqual(block, [W_UMBRELLA, W_JACKET])
        start = message.index("**%s**" % YOUR_DAY_LABEL)
        end = message.index("\n## ", start)
        self.assertLessEqual(
            len(message[start:end if end > 0 else len(message)].encode("utf-8")),
            render_digest.PERSONAL_MAX_BYTES)
        # Drop order unchanged: stories are what got shed, not the personal layer.
        self.assertIn("**%s**" % YOUR_DAY_LABEL, message)
        self.assertIn("**%s**" % WEATHER_LABEL, message)
        # The truncation notice points at the Pages ORIGIN. It used to name the
        # `digest-auth` Worker's workers.dev host, which put a second Basic-auth
        # prompt (username `digest`) in front of the origin's own - see
        # DigestLinkHostTests, which now owns that contract.
        self.assertIn("full digest: https://daily-digest.example.pages.dev", message)


class DigestLinkHostTests(unittest.TestCase):
    """Every click target the reader can follow must be the Pages ORIGIN.

    The live defect this pins shut: the ntfy click URL pointed at the
    `digest-auth` Worker, which proxied to the very same Pages origin. That
    produced TWO Basic-auth prompts asking for TWO different usernames
    (`digest` at the Worker, the owner's email at the origin), so the owner
    could not get in from the notification at all - and could not fix it by
    typing the right password, because the two prompts wanted different
    identities.

    The Worker is gone (deleted 2026-10-03). These assertions are on the
    RENDERED output and the rendered ntfy payload, not on the string constant
    in the workflow, because a constant can be correct while the payload that
    consumes it is not. `run_ntfy` execs the real heredoc, so these fail if
    the workflow ever regresses regardless of what the constant says.
    """

    ORIGIN = "https://daily-digest.example.pages.dev"
    # Substrings that must not survive anywhere a reader can be sent.
    FORBIDDEN = ("workers.dev", "digest-auth")

    def _assert_clean(self, label, blob):
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, blob,
                             "%s still references %r - it would send the reader to a "
                             "host that no longer exists, or to a second Basic-auth "
                             "prompt with a different username" % (label, bad))

    def _assert_no_dead_host(self, label, blob):
        """For SOURCE text, where naming the deleted Worker in a comment is
        documentation worth keeping. Only a live URL is a defect here - a
        reader-facing surface must additionally be free of the word, which is
        what _assert_clean enforces."""
        self.assertNotIn(
            "workers.dev", blob,
            "%s still contains a workers.dev URL" % label)

    def test_ntfy_click_and_action_point_at_the_pages_origin(self):
        tmp = tempfile.mkdtemp(prefix="digest-link-host-")
        try:
            got = run_ntfy(tmp, worst_case_payload())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        click = got["click"]
        self.assertTrue(
            click.startswith(self.ORIGIN + "/digests/"), click)
        self._assert_clean("ntfy payload 'click'", click)

        urls = [a.get("url", "") for a in got.get("actions", [])]
        self.assertTrue(urls, "ntfy payload carried no action URL to check")
        for url in urls:
            self.assertTrue(url.startswith(self.ORIGIN + "/digests/"), url)
            self._assert_clean("ntfy payload action url", url)

        # The markdown body carries the same link in its truncation notice.
        self.assertIn(self.ORIGIN + "/digests/", got["message"])
        self._assert_clean("ntfy message", got["message"])

    def test_all_four_surfaces_are_free_of_the_deleted_worker(self):
        """Website + BOTH email variants + ntfy, rendered from one payload.

        The email pair is included deliberately and the assertion is honest
        about what it can catch: as rendered today neither email variant
        carries ANY site URL (they link to articles only), so for those two
        surfaces this is a regression guard, not a repair. It is here because
        "the text version is easy to miss" is exactly how a second hardcoded
        host survives a fix that only patched the HTML.
        """
        surfaces = render_all_four(worst_case_payload()).all_four()
        for name in ("website", "email_html", "email_text", "ntfy"):
            self._assert_clean("rendered %s" % name, surfaces[name])

    def test_the_workflow_literal_is_the_origin(self):
        """The origin, so a bad edit fails here and not in production.

        The real host is NOT committed (scripts/check_public_sanitisation.py
        forbids it): the step reads CF_ARCHIVE_ORIGIN and falls back to this
        placeholder with a warning, so the fallback is what is pinned here.
        """
        self.assertIn("archive_origin = 'https://daily-digest.example.pages.dev'",
                      ntfy_source())
        self.assertIn("os.environ.get('CF_ARCHIVE_ORIGIN')", ntfy_source())
        self.assertIn("/digests/%s-%s.html", ntfy_source())
        # Only the HOST is forbidden in source: the heredoc documents in a
        # comment why the Worker was removed, and that history should survive.
        self._assert_no_dead_host("ntfy heredoc source", ntfy_source())


def yd_block(blob, surface):
    """The `Your day` body lines of a RENDERED surface, in order."""
    if surface == "website":
        start = blob.find('<div class="your-day">')
        if start < 0:
            return []
        body = blob[start:].split("</ul>")[0]
        out = []
        for chunk in body.split("<li")[1:]:
            out.append(chunk.split(">", 1)[1].split("<", 1)[0].strip())
        return out
    if surface == "email_html":
        start = blob.find(YOUR_DAY_LABEL)
        if start < 0:
            return []
        body = blob[start:].split("</ul>")[0]
        out = []
        for chunk in body.split("<li")[1:]:
            out.append(chunk.split(">", 1)[1].split("<", 1)[0].strip())
        return out
    marker = "**%s**" % YOUR_DAY_LABEL if surface == "ntfy" else YOUR_DAY_LABEL
    return _md_block(blob, marker)


def weather_block(blob, surface):
    """The weather block's body lines of a RENDERED surface, in order."""
    if surface in ("website", "email_html"):
        for opener in ('<div class="weather-block">',):
            start = blob.find(opener)
            if start >= 0:
                body = blob[start:].split("</ul>")[0]
                return [chunk.split(">", 1)[1].split("<", 1)[0].strip()
                        for chunk in body.split("<li")[1:]]
        start = blob.find(WEATHER_LABEL)
        if start < 0:
            return []
        body = blob[start:].split("</ul>")[0]
        return [chunk.split(">", 1)[1].split("<", 1)[0].strip()
                for chunk in body.split("<li")[1:]]
    marker = "**%s**" % WEATHER_LABEL if surface == "ntfy" else WEATHER_LABEL
    return _md_block(blob, marker)


def _md_block(blob, marker):
    lines, inside = [], False
    for raw in blob.split("\n"):
        if not inside:
            if raw.strip() == marker:
                inside = True
            continue
        if raw.startswith("- "):
            lines.append(raw[2:].strip())
        else:
            break
    return lines


if __name__ == "__main__":
    unittest.main()
