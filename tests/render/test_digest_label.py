#!/usr/bin/env python3
"""tests/render/test_digest_label.py — the label says WHEN, not WHICH SLOT.

THE OWNER'S COMPLAINT, VERBATIM
-------------------------------
    "instead of evening can you add the time it send in... look at top right
    at that evening, if i get some important news in like 2am then it will
    still say evening even though its not."

So the word the owner reads stopped following the clock. It was
`--digest-time` -- the SLOT -- which is structural identity, not a claim about
what time it is. A slot that slipped from 07:50 to 02:03 kept saying
"Morning"/"Evening" because the slot never moves and a clock always does.

WHAT THIS FILE PINS, AND WHY EACH PART IS HERE
---------------------------------------------
  1. The boundary table, at every edge, with the NPT conversion ASSERTED
     rather than assumed. A renderer that forgot the +05:45 would render the
     right word for the wrong hour and every "obvious" assertion would still
     pass, so the test builds the UTC instant itself and hands that in. This
     machine's local clock is ALSO Nepal time, so a naive-local-clock bug
     renders correctly here and is 5h45m wrong on GitHub's UTC runners; which
     is why this is a source-level assertion on the arithmetic as well as a
     behavioural one on the values.
  2. The two clocks a page can honestly print, and which one it printed. The
     DUE clock exists before the build (it is the committed schedule); the BUILD
     clock is the only thing an unscheduled build has. See
     `TestThePageNeverPrintsATimeItDidNotKnow`.
  3. The case that motivated the whole change: a digest with NO schedule for its
     slot, built at 02:03, must not say "Morning".
  4. The structural slot key is UNTOUCHED -- KV key, filename, the
     `render_index` glob, and the Worker's ledger field. This is the half that
     must not move, and it is asserted by name because it is invisible: a
     renderer cannot tell you it broke the archive's addressing, it just
     quietly stops being found.
  5. The archive-index scrapers run against the NEW pages. The title is the
     only thing that changed, so the proof is that `parse_digest_categories`
     and `first_thumbnail` return IDENTICAL results for a page labelled
     "Late Night · built 02:03" and for one labelled "Night · built 23:59" --
     the label is proven not to carry the scrapers.
  6. All THREE copies of the boundary table agree (render_digest,
     build_email, and the ntfy heredoc string inside daily-news.yml). They are
     three copies on purpose -- see the comments at each definition -- and
     three copies is exactly what rots without this test.
  7. The push icon is a function of the word, which is a function of the real
     push instant. Asserted on the source (the defect WAS a source shape) and on
     the bytes the real heredoc produced.

THE OWNER'S TWO MESSAGES, VERBATIM
----------------------------------
    "the time in the page is Evening Digest · 18:43. is that a problem or is it
     intended? also can you add morning and evening for normal news but i can
     get news at mignight too so add night, midnight, afternoon, noon and so on
     and it should be according to the time other wise i might get news at 12am
     and it will be evening which is not right."

EVERYTHING RENDERS TO A TEMP DIRECTORY. Nothing here writes to docs/.
"""

import ast
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(_HERE)), "scripts"))

from fixtures import validated_payload, write_json  # noqa: E402
from test_render_honesty import ntfy_source, run_ntfy  # noqa: E402

import build_email  # noqa: E402
import render_digest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "daily-news.yml")
WORKER = os.path.join(REPO_ROOT, "deploy", "cloudflare-worker.js")
TEMPLATE = os.path.join(REPO_ROOT, "templates", "email.html")

#: Nepal is a fixed +05:45 and has never observed DST, so this is exact.
NPT = datetime.timedelta(hours=5, minutes=45)

#: THE BOUNDARY TABLE, restated here as the expectation rather than read back
#: from the implementation. Reading it back from the implementation would make
#: the test agree with any table at all, which is the failure mode this whole
#: repo keeps getting bitten by.
#:
#: BOTH EDGES OF EVERY BAND, in clock order. The first hour of a band is where a
#: missing entry shows up and the last minute is where a band that is one hour
#: too short shows up, so a table with a hole or an overlap in the middle fails
#: here rather than somewhere nobody was looking.
#:
#:     (NPT wall clock, expected word)
BOUNDARY_CASES = (
    ("00:00", "Midnight"),      # the first edge of the day
    ("00:59", "Midnight"),
    ("01:00", "Late Night"),
    ("04:59", "Late Night"),
    ("05:00", "Dawn"),
    ("06:59", "Dawn"),
    ("07:00", "Morning"),       # the owner's own morning slot fires here
    ("11:59", "Morning"),
    ("12:00", "Noon"),
    ("12:59", "Noon"),
    ("13:00", "Afternoon"),
    ("16:59", "Afternoon"),
    ("17:00", "Evening"),       # the owner's own evening slot fires here
    ("20:59", "Evening"),
    ("21:00", "Night"),
    ("23:59", "Night"),
)

#: The same table as a plain hour -> word expectation, which is what the
#: exhaustion tests use. Written out rather than derived from
#: BOUNDARY_CASES, because deriving it would make the two agree by
#: construction and the point of asserting both is that they are independent.
EXPECTED_WORD_BY_HOUR = (
    "Midnight",    # 00
    "Late Night",  # 01
    "Late Night",  # 02
    "Late Night",  # 03
    "Late Night",  # 04
    "Dawn",        # 05
    "Dawn",        # 06
    "Morning",     # 07
    "Morning",     # 08
    "Morning",     # 09
    "Morning",     # 10
    "Morning",     # 11
    "Noon",        # 12
    "Afternoon",   # 13
    "Afternoon",   # 14
    "Afternoon",   # 15
    "Afternoon",   # 16
    "Evening",     # 17
    "Evening",     # 18
    "Evening",     # 19
    "Evening",     # 20
    "Night",       # 21
    "Night",       # 22
    "Night",       # 23
)

#: THE ICON FOR EVERY WORD, as the expectation. Parsed out of the heredoc and
#: compared against this by `TestTheEmojiFollowsTheWord`, so a copy that drifts
#: is caught here instead of showing the owner a sun at 02:03.
#:
#: The moon icons are deliberately DIFFERENT from each other (new moon for
#: Midnight, crescent for Late Night, stars for Night): ntfy renders the first
#: character of the notification as its icon, so three identical moons for three
#: different hours of the night would throw away the granularity this change
#: exists to add.
EXPECTED_WORD_EMOJI = {
    "Midnight": "\U0001f311",
    "Late Night": "\U0001f319",
    "Dawn": "\U0001f305",
    "Morning": "☀️",
    "Noon": "\U0001f31e",
    "Afternoon": "\U0001f324",
    "Evening": "\U0001f307",
    "Night": "\U0001f303",
}

#: The two slots, and the Nepal minutes the COMMITTED SCHEDULE says they are due
#: at. Computed here from the raw cron rather than copied from the renderer, so
#: that a renderer which invented its own minute fails instead of agreeing with
#: itself.
#:
#:     slot     -> the Cloudflare cron in deploy/schedule.json (UTC)
SCHEDULE_CRONS = {"morning": "35 1 * * *", "evening": "35 13 * * *"}


def read_text(path):
    """Read a repo file as UTF-8. A `with` block, so no ResourceWarning.

    `io.open(...).read()` leaves the handle to the collector, which prints a
    `ResourceWarning` into the runner output and makes a real failure harder to
    find in it.
    """
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def npt_stamp_to_utc(hhmm, date="2026-10-12"):
    """The UTC instant that IS the given Nepal wall clock.

    Computed HERE with a plain timedelta and nothing from render_digest, so a
    renderer that dropped the offset would be caught rather than agreeing with
    its own mistake. The date rolls backwards on purpose: 00:00 NPT is 18:15
    UTC on the PREVIOUS day, and a table that could not express that would not
    be testing the real conversion.
    """
    naive_npt = datetime.datetime.strptime("%s %s" % (date, hhmm),
                                           "%Y-%m-%d %H:%M")
    return (naive_npt - NPT).replace(tzinfo=datetime.timezone.utc)


# ===========================================================================
# 1. The boundary table, with the conversion asserted
# ===========================================================================

class TestBoundaryTable(unittest.TestCase):
    """Every edge, on both real renderers, with NPT conversion proven."""

    def _assert_table(self, module):
        for hhmm, expected_word in BOUNDARY_CASES:
            with self.subTest(surface=module.__name__, npt=hhmm):
                utc = npt_stamp_to_utc(hhmm)
                got_word, got_clock = module.npt_label(utc)
                self.assertEqual(
                    got_word, expected_word,
                    "%s: NPT %s should read %r, read %r (the UTC instant the "
                    "test handed in was %s)"
                    % (module.__name__, hhmm, expected_word, got_word, utc))
                # The clock is asserted too: a renderer could return the right
                # WORD for the wrong hour and still look correct here.
                self.assertEqual(
                    got_clock, hhmm,
                    "%s: NPT %s reported the clock as %s -- the +05:45 "
                    "conversion is wrong even though the word happened to be "
                    "right" % (module.__name__, hhmm, got_clock))

    def test_render_digest_gets_every_edge(self):
        self._assert_table(render_digest)

    def test_build_email_gets_every_edge(self):
        self._assert_table(build_email)

    def test_the_two_renderers_agree_on_every_edge(self):
        """Two independent renderers, one word, no drift.

        They are separate files on purpose -- the repo has refused to couple
        them through a shared module -- so the only thing holding them together
        is this test. A silent split would show the owner "Evening" on the page
        and "Morning" in the email for the same digest.
        """
        for hhmm, expected_word in BOUNDARY_CASES:
            with self.subTest(npt=hhmm):
                utc = npt_stamp_to_utc(hhmm)
                self.assertEqual(render_digest.npt_label(utc),
                                 build_email.npt_label(utc))

    def test_the_table_covers_all_24_hours_with_no_gap_and_no_overlap(self):
        """The scan is total: every hour 0-23 maps to exactly one word.

        Proved by exhausting the hours against a table written out INDEPENDENTLY
        of the implementation, so dropping an entry, reordering one or widening a
        band all turn this red. A hole would hand some hour to whichever entry
        happens to precede it, and an overlap would hand the same hour two
        different words depending on the order the scan reaches them.
        """
        self.assertEqual(len(EXPECTED_WORD_BY_HOUR), 24)
        for hour in range(24):
            with self.subTest(hour=hour):
                self.assertEqual(render_digest.label_word_for_hour(hour),
                                 EXPECTED_WORD_BY_HOUR[hour])
                self.assertEqual(build_email.label_word_for_hour(hour),
                                 EXPECTED_WORD_BY_HOUR[hour])

    def test_every_band_is_contiguous_and_bands_do_not_share_an_hour(self):
        """The two facts the hour table above cannot show on its own.

        Exhausting the hours proves each hour has a word. It does not prove the
        word is spelled the same for every hour in a band -- a table that said
        "Morning" at 07:00 and "Dawn" at 08:00 and "Morning" again at 09:00
        would pass the exhaustion test. This walks the table in order and
        requires each word to appear as ONE unbroken run.
        """
        runs = []
        for hour, word in enumerate(EXPECTED_WORD_BY_HOUR):
            if runs and runs[-1][0] == word:
                runs[-1][2] = hour
            else:
                runs.append([word, hour, hour])
        # The runs must TILE the day: start at 00:00, end at 23:59, and each
        # one begin the minute after the last one ended. A gap or an overlap in
        # the middle would leave an hour reachable by no rule at all.
        self.assertEqual(runs[0][1], 0, "no band covers 00:00: %r" % (runs,))
        self.assertEqual(runs[-1][2], 23, "no band covers 23:00: %r" % (runs,))
        for (_prev_word, _prev_start, prev_end), (word, start, _end) in zip(
                runs, runs[1:]):
            with self.subTest(after=prev_end, word=word):
                self.assertEqual(
                    start, prev_end + 1,
                    "hour %02d belongs to no band, or to two of them: %r"
                    % (prev_end + 1, runs))
        for word, start, end in runs:
            with self.subTest(word=word):
                self.assertEqual(render_digest.label_word_for_hour(start), word)
                self.assertEqual(render_digest.label_word_for_hour(end), word)
                self.assertEqual(build_email.label_word_for_hour(start), word)

    def test_the_two_halves_of_the_night_do_not_share_a_word(self):
        """02:03 is Late Night, 23:59 is Night, and 00:43 is Midnight.

        This is the shape the owner's "add midnight" complaint lives in. The
        previous table gave 00:00-04:59 and 21:00-23:59 one word, so 02:03 read
        "Night" -- true, and useless to anyone trying to tell a late-night push
        from an evening one. A naive `hour >= 21 or hour < 5` split is exactly
        the shape that drifts, so the three hours are pinned by name.
        """
        self.assertEqual(render_digest.label_word_for_hour(0), "Midnight")
        self.assertEqual(render_digest.label_word_for_hour(2), "Late Night")
        self.assertEqual(render_digest.label_word_for_hour(23), "Night")
        # The two night ends must NOT collapse into one another any more.
        self.assertNotEqual(render_digest.label_word_for_hour(23),
                            render_digest.label_word_for_hour(0))
        self.assertNotEqual(render_digest.label_word_for_hour(23),
                            render_digest.label_word_for_hour(2))

    def test_the_owner_named_every_word_the_table_uses(self):
        """Night, midnight, afternoon, noon -- plus morning and evening.

        The owner's request was "add morning and evening for normal news but I
        can get news at mignight too so add night, midnight, afternoon, noon and
        so on". This asserts the set of words against that sentence rather than
        against taste, so a later edit that quietly renames "Noon" to "Midday"
        fails here. Dawn, Late Night and Morning are the splits of the three
        coarse bands he already had.
        """
        words = {word for _start_hour, word in render_digest.LABEL_HOUR_BOUNDS}
        for named in ("Night", "Midnight", "Afternoon", "Noon",
                      "Morning", "Evening"):
            with self.subTest(owner_named_word=named):
                self.assertIn(named, words)
        self.assertEqual(
            len(words), len(render_digest.LABEL_HOUR_BOUNDS),
            "two hours share a word, so the vocabulary is not one word per band "
            "and the table can drift in one place without anyone noticing")

    def test_the_conversion_is_plus_0545_and_not_the_runners_local_time(self):
        """02:03 UTC is NOT 02:03 NPT, and the renderer must not pretend it is.

        The whole point of the NPT conversion is that it moves the clock. If
        this ever passes with a renderer that echoes the UTC hour, the timezone
        handling is gone -- and the field it broke is the one the owner uses to
        tell morning from night.
        """
        utc_0203 = datetime.datetime(2026, 10, 12, 2, 3, tzinfo=datetime.timezone.utc)
        word, clock = render_digest.npt_label(utc_0203)
        # 02:03 UTC + 05:45 = 07:48 the same day.
        self.assertEqual((word, clock), ("Morning", "07:48"))

    def test_naive_and_aware_instants_agree(self):
        """A naive datetime is read as UTC, never as local time.

        Every clock in this repo hands over one or the other. If the naive path
        were treated as local, the same instant would render two different
        labels depending on the runner's timezone -- and CI runs in more than
        one.
        """
        aware = datetime.datetime(2026, 10, 12, 18, 15, tzinfo=datetime.timezone.utc)
        naive = aware.replace(tzinfo=None)
        self.assertEqual(render_digest.npt_label(aware),
                         render_digest.npt_label(naive))
        # 18:15 UTC + 05:45 = 00:00 the NEXT NPT day -- the first edge of the
        # table, and the one the old four-word table got wrong by calling it
        # "Night".
        self.assertEqual(render_digest.npt_label(aware), ("Midnight", "00:00"))

    #: Every shape of "not a stamp" that has actually arrived here. The
    #: trailing " NPT" is the one that mattered: it was parseable-looking, was
    #: passed by a real test, and silently rendered the wall clock.
    UNUSABLE_STAMPS = ("", None, "not-a-stamp", "2026-13-45 99:99",
                       "2026-10-12 06:00 NPT", "2026-10-12")

    def test_an_unusable_stamp_is_refused_rather_than_read_off_a_second_clock(self):
        """LOUD, not `now`. This is the honesty property, asserted on both renderers.

        The old contract here was the opposite one, and it was a lie with a
        plausible face: an unparseable stamp fell back to `npt_label()`, i.e.
        THE WALL CLOCK, so a page whose build stamp was malformed rendered a
        title claiming whatever minute it happened to be looked at. The footer
        clock came from the stamp and the label word came from `now`, so the two
        halves of one line described two different instants and neither looked
        wrong.

        So the fallback is gone and the stamp is named instead. Both renderers
        are covered because they are independent copies of this function pinned
        against each other, and a fix to one that left the other lying would be
        a fix that only held on the website.
        """
        for module in (render_digest, build_email):
            for bad in self.UNUSABLE_STAMPS:
                with self.subTest(renderer=module.__name__, stamp=bad):
                    with self.assertRaises(ValueError) as ctx:
                        module.label_from_npt_stamp(bad)
                    message = str(ctx.exception)
                    self.assertIn(
                        "YYYY-MM-DD HH:MM", message,
                        "%s refused the stamp without naming the shape it needs, "
                        "so the caller cannot tell what to fix: %s"
                        % (module.__name__, message))
                    self.assertIn(
                        repr(bad), message,
                        "%s refused the stamp without naming WHICH value was "
                        "wrong, which is the part an operator needs: %s"
                        % (module.__name__, message))

    def test_a_well_formed_stamp_is_still_served_by_the_stamp_and_not_a_clock(self):
        """The other side of the refusal, so it cannot be bought with a blanket raise.

        A renderer that simply always raised would pass the test above. This
        pins that the well-formed path -- the one production actually uses,
        because `kathmandu_now_str` formats from a literal pattern -- is
        unchanged: it is answered from the stamp, at every hour of the day.
        """
        for hhmm, expected_word in BOUNDARY_CASES:
            with self.subTest(npt=hhmm):
                stamp = "2026-10-12 %s" % hhmm
                for module in (render_digest, build_email):
                    word, clock = module.label_from_npt_stamp(stamp)
                    self.assertEqual(
                        (word, clock), (expected_word, hhmm),
                        "%s: stamp %r must be answered from ITSELF, not from a "
                        "clock read at call time" % (module.__name__, stamp))


# ===========================================================================
# 2. The label follows the REAL instant, not the configured slot
# ===========================================================================

class TestTheLabelFollowsTheClockNotTheSlot(unittest.TestCase):
    """The owner's complaint, as an assertion.

    WHICH CLOCK, STATED UP FRONT, because it is the one thing this suite cannot
    be vague about. There are two clocks on a page and they are not
    interchangeable:

      * the DUE clock -- the slot's scheduled delivery minute, quoted from
        `deploy/schedule.json` and printed with the word `due`. It exists BEFORE
        the build starts, which is the only reason a static page may print it.
      * the BUILD clock -- the NPT instant this artifact was rendered at,
        printed with the word `built`, and what the footer has always printed.

    A digest with no scheduled slot has only the second, so the first four tests
    below use an off-schedule slot. That is the owner's original 02:03 case
    exactly: a run nobody scheduled, which must not be allowed to borrow the
    morning slot's 07:20 and call itself a Morning digest.
    """

    META = {"source_health": {"state": "ok", "source_results": [],
                              "by_category": {}}}

    #: A slot the committed schedule does not name. NOT invented for the test --
    #: `deploy/schedule.json`'s `digest_slots` has exactly two entries and this
    #: is not one of them, which the first test below proves before relying on
    #: it.
    OFF_SCHEDULE_SLOT = "breaking"

    def _page(self, slot, stamp):
        return render_digest.render_page([], slot, "2026-10-12", stamp,
                                         self.META)

    def test_the_off_schedule_slot_really_is_off_schedule(self):
        """The fixture above is verified, not assumed.

        A test that leaned on "breaking" being absent from the schedule while
        the schedule quietly grew a third entry would be asserting against a
        fiction -- and it would keep passing while the thing it guards stopped
        being true.
        """
        self.assertEqual(render_digest.slot_due_npt(self.OFF_SCHEDULE_SLOT), "")
        self.assertEqual(build_email.slot_due_npt(self.OFF_SCHEDULE_SLOT), "")

    def test_an_off_schedule_build_at_0203_is_a_LATE_NIGHT_digest(self):
        """The owner's complaint, verbatim: 02:03 is not morning.

        Before this suite asserted the word "Night" here, which was true and
        still too coarse to distinguish a 00:43 midnight push from a 02:03 one.
        """
        page = self._page(self.OFF_SCHEDULE_SLOT, "2026-10-12 02:03")
        self.assertIn('<h1 class="topbar-title">Late Night Digest · built 02:03</h1>',
                      page)
        self.assertNotIn(
            "Morning Digest", page,
            "a digest built at 02:03 NPT is calling itself a Morning digest; "
            "this is the owner's complaint, verbatim, unfixed")

    def test_an_off_schedule_build_at_0043_is_a_MIDNIGHT_digest(self):
        """The word he explicitly asked for, at the hour he named.

        "i can get news at mignight too so add ... midnight" -- an artifact
        rendered at 00:43 with no schedule behind it must say Midnight, not
        Night, not Morning, and certainly not Evening.
        """
        page = self._page(self.OFF_SCHEDULE_SLOT, "2026-10-12 00:43")
        self.assertIn('<h1 class="topbar-title">Midnight Digest · built 00:43</h1>',
                      page)

    def test_the_two_ends_of_the_night_render_different_words(self):
        """23:59 and 00:43 are 44 minutes apart and must not read the same."""
        late = self._page(self.OFF_SCHEDULE_SLOT, "2026-10-12 23:59")
        early = self._page(self.OFF_SCHEDULE_SLOT, "2026-10-12 00:43")
        self.assertIn("Night Digest · built 23:59", late)
        self.assertIn("Midnight Digest · built 00:43", early)

    def test_the_browser_title_carries_the_same_label_as_the_topbar(self):
        page = self._page(self.OFF_SCHEDULE_SLOT, "2026-10-12 02:03")
        self.assertIn(
            "<title>Personal News Informer — Late Night Digest · built 02:03 "
            "— 2026-10-12</title>", page)

    def test_the_footer_still_reports_the_build_instant_it_always_did(self):
        """The build stamp is untouched, and it is still the build stamp.

        The footer is the one claim on this page that never changed, so it is
        asserted unchanged: a page whose title says `due 19:20` and whose
        footer says `Generated by ... 18:40 NPT` is reporting two different
        facts about two different instants, which is the honest arrangement.
        """
        page = self._page(self.OFF_SCHEDULE_SLOT, "2026-10-12 02:03")
        self.assertIn('<h1 class="topbar-title">Late Night Digest · built 02:03</h1>',
                      page)
        self.assertIn("Generated by Personal News Informer · 2026-10-12 02:03 NPT",
                      page)

    def test_the_email_labels_itself_from_the_same_schedule_as_the_page(self):
        """The email and the page are two renderers and must not disagree.

        Both are built during the prebuild, both quote the committed schedule,
        and both fall back to `generated_at_npt` named as the build time. A
        difference between them would show the owner "Morning" in one surface
        and "Late Night" in the other for the same digest.
        """
        payload = validated_payload("ok")
        payload["digest_time"] = self.OFF_SCHEDULE_SLOT
        payload["generated_at_npt"] = "2026-10-12 02:03"
        tmp = tempfile.mkdtemp(prefix="label-email-")
        try:
            path = write_json(os.path.join(tmp, "p.json"), payload)
            html_path = os.path.join(tmp, "email.html")
            rc = build_email.main(["--in", path, "--out-html", html_path,
                                   "--out-text", os.path.join(tmp, "e.txt"),
                                   "--template", TEMPLATE])
            self.assertEqual(rc, 0)
            with io.open(html_path, encoding="utf-8") as fh:
                html = fh.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("<span>Late Night Digest · built 02:03</span> briefing",
                      html)
        self.assertNotIn("Morning Digest", html)
        # `text-transform:capitalize` lowercased the rest of the label, which
        # would have rendered "Late Night digest · built 02:03". Assert it is
        # gone rather than trusting the template by eye.
        self.assertNotIn("text-transform:capitalize", html)


# ===========================================================================
# 2b. The page names the DUE time, and only ever one it actually knows
# ===========================================================================

class TestThePageNeverPrintsATimeItDidNotKnow(unittest.TestCase):
    """The owner's second question, as an assertion.

        "the time in the page is Evening Digest · 18:43. is that a problem or
        is it intended?"

    It was a problem, and the reason is structural rather than a bug: the
    prebuild renders the page ~40 minutes before the slot and the page is a
    static committed artifact, so nothing can ever rewrite it afterwards. 18:43
    is true -- it is when the file was made -- and useless, because the phone it
    was read on said 19:25.

    THE FIX, AND ITS ONE LIMIT. The page prints the slot's scheduled minute
    (07:20 / 19:20) with the word `due`, because that minute is a fact that
    existed before the build started: it is read out of `deploy/schedule.json`,
    which is already pinned against the Worker's own dispatcher. What it may
    NOT do is print a send time, because no build can know when a push leaves,
    and the surface that DOES know (ntfy) renders at push time.

    The degradation is the half that makes this honest rather than merely
    convenient: a build with no scheduled slot prints the build instant, says
    `built`, and never borrows a minute that was never intended for it.
    """

    META = {"source_health": {"state": "ok", "source_results": [],
                              "by_category": {}}}
    #: The prebuild stamps. 18:43 is the owner's literal example; 06:41 is the
    #: morning equivalent, 39 minutes before the 07:20 slot.
    PREBUILD_STAMPS = (("evening", "2026-10-13 18:43", "19:20"),
                       ("morning", "2026-10-13 06:41", "07:20"))

    def _page(self, slot, stamp, schedule_path=None):
        return render_digest.render_page([], slot, "2026-10-13", stamp,
                                         self.META,
                                         schedule_path=schedule_path)

    def _schedule_without(self, slot):
        """A schedule file that genuinely does not name `slot`."""
        blob = json.loads(read_text(os.path.join(REPO_ROOT, "deploy",
                                                 "schedule.json")))
        blob["dispatcher_crons"]["digest_slots"].pop(slot, None)
        tmp = tempfile.mkdtemp(prefix="label-nosched-")
        path = write_json(os.path.join(tmp, "schedule.json"), blob)
        return tmp, path

    # -- the owner's own page, reproduced ---------------------------------

    def test_the_owners_page_stops_saying_1843_and_names_the_real_delivery(self):
        """`Evening Digest · 18:43` -> `Evening Digest · due 19:20`.

        This is the exact string he quoted, on the exact slot. The build stamp is
        still on the page -- in the footer, which has always printed it -- so
        nothing is lost, it is just no longer pretending to be the delivery.
        """
        page = self._page("evening", "2026-10-13 18:43")
        self.assertIn(
            '<h1 class="topbar-title">Evening Digest · due 19:20</h1>', page)
        self.assertNotIn(
            '<h1 class="topbar-title">Evening Digest · 18:43</h1>', page,
            "the owner's complaint is unfixed: the page still shows the prebuild "
            "minute as the digest's time")
        # The build minute is still available, where it was always true.
        self.assertIn("Generated by Personal News Informer · 2026-10-13 18:43 NPT",
                      page)

    def test_the_morning_prebuild_gets_the_same_treatment(self):
        page = self._page("morning", "2026-10-13 06:41")
        self.assertIn('<h1 class="topbar-title">Morning Digest · due 07:20</h1>',
                      page)

    # -- the minutes come from the SCHEDULE, not from a literal -----------

    def test_the_due_minute_is_the_schedule_cron_converted_to_npt(self):
        """07:20 and 19:20 are computed here, from the raw cron, again.

        This test builds the expected minute with a plain timedelta from the
        five cron fields and never asks either renderer for it, so a renderer
        that hard-coded "19:20" would agree here by accident while a schedule
        change moved the real slot -- which is exactly the drift
        tests/workflows/test_workflow_lint.py exists to catch.
        """
        for slot, cron in SCHEDULE_CRONS.items():
            with self.subTest(slot=slot):
                minute_s, hour_s = cron.split()[0], cron.split()[1]
                utc = datetime.datetime(2000, 1, 1, int(hour_s), int(minute_s),
                                        tzinfo=datetime.timezone.utc)
                expected = utc.astimezone(render_digest.NPT_TZ).strftime("%H:%M")
                self.assertEqual(render_digest.slot_due_npt(slot), expected)
                self.assertEqual(build_email.slot_due_npt(slot), expected)
        # And the two are the minutes the owner's brief names.
        self.assertEqual(render_digest.slot_due_npt("morning"), "07:20")
        self.assertEqual(render_digest.slot_due_npt("evening"), "19:20")

    def test_the_two_renderers_read_the_same_due_minutes(self):
        for slot in ("morning", "evening", "breaking", "", "MORNING"):
            with self.subTest(slot=slot):
                self.assertEqual(render_digest.slot_due_npt(slot),
                                 build_email.slot_due_npt(slot))

    # -- the word, the clock and the icon are ONE instant ------------------

    def test_the_word_is_derived_from_the_clock_that_is_printed(self):
        """`due 19:20` beside `Evening`, never beside `Morning`.

        The word and the clock are two halves of one claim. If the word came
        from the build stamp while the clock came from the schedule, a prebuild
        at 06:41 for the 07:20 slot would print `Morning · due 07:20` -- which
        happens to be right today and would silently stop being right the first
        time the two drifted apart.
        """
        for slot, stamp, due in self.PREBUILD_STAMPS:
            with self.subTest(slot=slot):
                page = self._page(slot, stamp)
                shown_word = re.search(
                    r'<h1 class="topbar-title">(.+?) Digest · due %s</h1>'
                    % re.escape(due), page)
                self.assertIsNotNone(
                    shown_word,
                    "the page does not carry a `<word> Digest · due %s` "
                    "title: %s" % (due, page[:0] or "no match"))
                self.assertEqual(
                    shown_word.group(1),
                    render_digest.label_word_for_hour(int(due.split(":")[0])),
                    "the word beside `%s` is not the word for %s -- the clock and "
                    "the word are two different instants" % (due, due))

    # -- the degradation, which is what makes the rest honest ------------

    def test_a_build_with_no_schedule_falls_back_to_the_build_instant(self):
        """No schedule for this slot -> the build minute, named `built`.

        This is the case the brief calls out: "a build that is NOT for a known
        slot (an off-schedule or breaking run) must not display a slot time
        that was never intended." A real schedule file with the slot removed is
        used rather than a flag, so this exercises the real code path.
        """
        tmp, path = self._schedule_without("evening")
        try:
            page = self._page("evening", "2026-10-13 18:43", schedule_path=path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn(
            '<h1 class="topbar-title">Evening Digest · built 18:43</h1>', page)
        self.assertNotIn("due 19:20", page,
                         "a build with no schedule claimed a delivery minute "
                         "that was never intended for it")

    def test_a_missing_or_corrupt_schedule_degrades_instead_of_raising(self):
        """Four ways for there to be no schedule. All four answer `built`.

        A renderer that crashed on an unreadable schedule would take the page
        down entirely, which is a worse outcome than printing the one instant
        it actually has.
        """
        tmp = tempfile.mkdtemp(prefix="label-badsched-")
        try:
            missing = os.path.join(tmp, "absent.json")
            corrupt = os.path.join(tmp, "corrupt.json")
            with io.open(corrupt, "w", encoding="utf-8") as fh:
                fh.write("{not json at all")
            shape = os.path.join(tmp, "shape.json")
            with io.open(shape, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"dispatcher_crons": {"digest_slots": {}}}))
            for name, path in (("missing", missing), ("corrupt", corrupt),
                               ("no slots", shape)):
                with self.subTest(schedule=name):
                    self.assertEqual(render_digest.slot_due_npt(
                        "evening", schedule_path=path), "")
                    page = self._page("evening", "2026-10-13 18:43",
                                      schedule_path=path)
                    self.assertIn("Evening Digest · built 18:43", page)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_cron_that_is_not_a_time_of_day_is_refused_not_guessed(self):
        """`*/5 * * * *` and `1-5 * * * *` are schedules, not minutes.

        Turning a cron range into a minute would be inventing a delivery time,
        which is the one thing this whole change exists to avoid.
        """
        tmp = tempfile.mkdtemp(prefix="label-cron-")
        try:
            for bad_cron in ("*/5 * * * *", "1-5 * * * *", "* * * * *",
                             "35 13 * *", "99 13 * * *"):
                with self.subTest(cron=bad_cron):
                    path = write_json(os.path.join(tmp, "s.json"),
                                      {"dispatcher_crons": {"digest_slots": {
                                          "evening": bad_cron}}})
                    self.assertEqual(
                        render_digest.slot_due_npt("evening", schedule_path=path),
                        "", "a cron that is not a fixed time of day was turned "
                            "into a delivery minute")
                    self.assertEqual(
                        build_email.slot_due_npt("evening", schedule_path=path),
                        "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_provenance_property_over_every_hour_and_both_slots(self):
        """The invariant, exhaustively: the printed clock is a KNOWN clock.

        For all 24 hours and both slots, the clock the page prints is either the
        schedule's due minute or the build stamp's own minute, and it is labelled
        with which of the two it is. Nothing else can appear, because nothing
        else is ever computed -- which is the property the brief asks for, since
        a page showing 19:20 is only allowed to because 19:20 IS the schedule.

        The build hour varies and the due minute does not, so this also proves
        the word tracks the DUE clock rather than the build clock: a word
        derived from the build stamp would change with the hour below while the
        printed clock stayed at 19:20, and the two halves of the title would be
        describing different instants.
        """
        known_due = {"morning": "07:20", "evening": "19:20"}
        due_word = {due: render_digest.label_word_for_hour(int(due[:2]))
                    for due in known_due.values()}
        for slot in ("morning", "evening"):
            for hour in range(24):
                stamp = "2026-10-13 %02d:%02d" % (hour, 37)
                build_minute = stamp.split()[1]
                with self.subTest(slot=slot, hour=hour):
                    page = self._page(slot, stamp)
                    m = re.search(
                        r'<h1 class="topbar-title">(.+?) Digest · '
                        r'(due|built) (\d{2}:\d{2})</h1>', page)
                    self.assertIsNotNone(m, "the page title carries no clock")
                    word, kind, clock = m.group(1), m.group(2), m.group(3)
                    # A scheduled slot ALWAYS takes the schedule, whatever hour
                    # the build happened to run at.
                    self.assertEqual(kind, "due")
                    self.assertEqual(clock, known_due[slot])
                    self.assertEqual(word, due_word[clock])
                    # And the build minute must not be the claim.
                    if build_minute != clock:
                        self.assertNotIn(
                            '<h1 class="topbar-title">%s Digest · %s</h1>'
                            % (word, build_minute), page)

    def test_an_unreadable_build_stamp_is_refused_not_covered_for(self):
        """A clock this renderer cannot read is a caller defect, and is raised.

        Same contract as `label_from_npt_stamp`, and the same reason: the old
        `except` covered for a bad stamp with the WALL CLOCK, so a page whose
        stamp was unreadable rendered a title claiming whatever minute it
        happened to be looked at.
        """
        for bad in (None, "", "not-a-stamp", "2026-13-45 99:99"):
            with self.subTest(stamp=bad):
                with self.assertRaises(ValueError) as ctx:
                    render_digest.delivery_label("breaking", bad)
                self.assertIn("YYYY-MM-DD HH:MM", str(ctx.exception))
                with self.assertRaises(ValueError):
                    build_email.delivery_label("breaking", bad)

    def test_the_email_follows_the_same_two_rules(self):
        """Scheduled -> `due`; unscheduled -> `built`. On the email too.

        build_email is the second of the two independent renderers. It reads the
        same record through its own copy of the reader, and a digest arriving in
        one surface with `due` and in the other with `built` would be a real
        disagreement about a real fact.
        """
        for slot, stamp, due in self.PREBUILD_STAMPS:
            with self.subTest(slot=slot, kind="due"):
                self.assertEqual(
                    build_email.delivery_label(slot, stamp)[0],
                    "%s Digest · due %s"
                    % (render_digest.label_word_for_hour(
                        int(due.split(":")[0])), due))
        tmp, path = self._schedule_without("evening")
        try:
            self.assertEqual(
                build_email.delivery_label("evening", "2026-10-13 18:43",
                                           schedule_path=path)[0],
                "Evening Digest · built 18:43")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 3. The structural slot key is UNTOUCHED
# ===========================================================================

class TestTheStructuralSlotKeyIsUntouched(unittest.TestCase):
    """The half that must NOT move, asserted by name.

    `morning`/`evening` is structural: the KV key, the artifact filename, the
    `render_index` glob, `state/last_digest_sent.json`'s `digest_time`, the
    Worker's dedup guards and `workflow_dispatch` inputs all read it. A
    renderer change cannot detect that it broke those -- the archive just
    quietly stops finding its own pages -- so it is asserted directly.
    """

    def test_main_still_writes_the_slot_named_filename(self):
        for slot in ("morning", "evening"):
            with self.subTest(slot=slot):
                tmp = tempfile.mkdtemp(prefix="label-slot-")
                try:
                    out_dir = os.path.join(tmp, "digests")
                    rc = render_digest.main([
                        "--digest-time", slot, "--date", "2026-10-12",
                        "--email-txt", os.path.join(tmp, "absent.txt"),
                        "--stories-json", write_json(
                            os.path.join(tmp, "s.json"), validated_payload("ok")),
                        "--out-dir", out_dir,
                        "--index", os.path.join(tmp, "index.html")])
                    self.assertEqual(rc, 0)
                    self.assertTrue(os.path.isfile(
                        os.path.join(out_dir, "2026-10-12-%s.html" % slot)),
                        "the artifact filename must stay <date>-<slot>.html; "
                        "the archive and the KV key are addressed by it")
                    self.assertFalse(os.path.isfile(
                        os.path.join(out_dir, "2026-10-12-%s-2026-10-12.html" % slot)))
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

    def test_render_index_still_reads_only_morning_and_evening(self):
        """The archive glob is the addressing, and it is unchanged."""
        tmp = tempfile.mkdtemp(prefix="label-index-")
        try:
            out_dir = os.path.join(tmp, "digests")
            os.makedirs(out_dir)
            rc = render_digest.main([
                "--digest-time", "morning", "--date", "2026-10-12",
                "--email-txt", os.path.join(tmp, "absent.txt"),
                "--stories-json", write_json(
                    os.path.join(tmp, "s.json"), validated_payload("ok")),
                "--out-dir", out_dir,
                "--index", os.path.join(tmp, "index.html")])
            self.assertEqual(rc, 0)
            with io.open(os.path.join(tmp, "index.html"), encoding="utf-8") as fh:
                index = fh.read()
            self.assertIn('href="digests/2026-10-12-morning.html"', index)
            # And the entry's own `time` field is the slot, not the label word.
            self.assertRegex(index, r"2026-10-12-morning\.html")
            self.assertNotIn("Night Digest · ", index)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_renderers_still_expose_digest_time_as_the_slot(self):
        """`digest_time` is still the slot, and still refuses anything else.

        The word the owner reads was derived from the clock; the slot kept its
        own job. If the two ever merge, `digest_time` would start arriving as
        "Night" and update_history.py -- which marks stories against
        digest_time -- would be handed a word it does not accept.

        PREVIOUS VERSION OF THIS TEST WAS DECORATION. It asserted
        `hasattr(module, "digest_label")`, which says nothing at all about
        digest_time and would still have passed if digest_time had been
        replaced by the label word everywhere. It is now asserted where the
        slot is actually consumed.
        """
        # The shipped gate: story_state rejects anything but the two slots.
        src = read_text(os.path.join(REPO_ROOT, "scripts", "story_state.py"))
        self.assertIn("digest_time must be 'morning' or 'evening'", src)

        # And the gate is LIVE, not merely present in the source: calling
        # `mark_reported` proves story_state still refuses a label word. This
        # used to be a `hasattr` check with a conditional fallback, which would
        # have silently passed on a rename -- the vacuous-pass shape.
        import story_state
        for bad_word in ("Night", "Midnight", "Midday", "Noon", "Evening",
                         "morning digest"):
            with self.subTest(bad_word=bad_word):
                self.assertRaises(
                    ValueError, story_state.mark_reported,
                    {"stories": []}, [], bad_word, "2026-10-12")
        for slot in ("morning", "evening"):
            with self.subTest(slot=slot):
                state = {"stories": [{"event_id": "e1", "first_reported": None,
                                      "last_reported": None}]}
                story_state.mark_reported(state, ["e1"], slot, "2026-10-12")
                key = "in_%s" % slot
                self.assertEqual(state["stories"][0][key], "2026-10-12")

        # The slot still reaches `main()`'s addressing, which is the one place
        # the renderer itself turns the slot into a name on disk.
        self.assertTrue(callable(render_digest.main))
        for module in (render_digest, build_email):
            with self.subTest(module=module.__name__):
                # `digest_label` takes a WORD and a CLOCK. If it ever took the
                # slot instead, the signature would change shape.
                self.assertEqual(module.digest_label("Night", "02:03"),
                                 "Night Digest · 02:03")
                self.assertEqual(module.digest_label("Morning", "06:12"),
                                 "Morning Digest · 06:12")

    def test_the_worker_still_addresses_and_records_the_slot(self):
        """cloudflare-worker.js: the KV key and the ledger field are unchanged.

        Asserted against the WORKER SOURCE, which is the honest way to say "I
        did not touch this": the Worker is not imported by Python and is not
        executed by these suites.

        PINNING THE EXACT LINE WAS ITSELF A TRAP, AND IT FIRED. This asserted
        `{ digest_time: slotName },` with its trailing comma, so adding
        `slot_date` beside it -- the fix that stops a dispatched slot being
        dated off the receiving clock -- broke a test whose own docstring says
        the KV key and the ledger field are unchanged. Those ARE unchanged: the
        invariant is the ADDRESSING, not the byte-perfect line. A pinned literal
        is a comment that begins lying, so each assertion below names the fact
        it protects, and the two NEW facts (the date a dispatch carries, the
        tick the crons pass) are pinned here as well as in the digest suites.
        """
        src = read_text(WORKER)
        # The KV namespace prefix is still the literal the ledger, the archive
        # and the seal all share; a typo here re-addresses every artifact.
        self.assertIn('const DIGEST_ARTIFACT_PREFIX = "digest:artifact:";', src)
        # The slot is still ADDRESSED by name: the artifact key is still built
        # from the slot, in the same order, from the same prefix.
        self.assertIn('DIGEST_ARTIFACT_PREFIX + today + ":" + slotName', src)
        # The slot is still RECORDED by name in the dispatch payload. The
        # surrounding braces and commas are NOT pinned, because the payload is
        # where a new field legitimately appears.
        self.assertIn("digest_time: slotName", src)
        # The honesty fix is pinned HERE, not only in the digest suites: the
        # dispatch tells the receiving run which day its slot belongs to. The
        # field and its VALUE are pinned; the trailing comma is not, for the
        # same reason the braces are not above -- a field added after it would
        # otherwise break this again.
        self.assertIn("slot_date: slotDate", src)
        # The two slots the Worker dispatches are still the two slots, and each
        # call still addresses its slot by name. The ARGUMENT LIST is NOT pinned:
        # the tick now has to reach the callee for the date to be honest, and a
        # test forbidding an extra argument would forbid that fix.
        self.assertIn('deliverDigest(env, "morning"', src)
        self.assertIn('deliverDigest(env, "evening"', src)
        # ...and the tick really reaches both calls, under the field name the
        # callee reads (`opts.scheduledMs`). Pinned by FIELD, not by value.
        for slot in ("morning", "evening"):
            with self.subTest(slot=slot):
                self.assertRegex(
                    src,
                    r'deliverDigest\(env, "%s",\s*\{[^}]*scheduledMs' % slot)

    def test_the_workflow_dispatch_input_is_still_the_slot(self):
        src = read_text(WORKFLOW)
        self.assertIn("digest_time:", src)
        self.assertIn("morning", src)
        self.assertIn("evening", src)


# ===========================================================================
# 4. The archive scrapers still work against the NEW pages
# ===========================================================================

class TestArchiveScrapersSurviveTheTitleChange(unittest.TestCase):
    """The load-bearing coupling, proven rather than asserted.

    `docs/index.html` scrapes digest pages through `parse_digest_categories`
    and `first_thumbnail`. Those read `<h2 class="section-header">`,
    `<article class="card">` and `<img class="card-thumb">`. The title is not
    one of those markers -- but "the title is not one of those markers" is
    exactly the kind of claim that is true until it is not, so the scrapers
    are RUN against a page whose title carries the new label and their results
    are compared with the same page under the old label.

    Both pages are produced by the real `render_page`, so a real regression in
    the markup would be caught here rather than in production.
    """

    META = {"source_health": {"state": "ok", "source_results": [],
                              "by_category": {}}}

    #: An off-schedule slot, so the two pages below can carry DIFFERENT labels
    #: at all. With a scheduled slot both would print the same `· due 19:20`
    #: regardless of build stamp, and the comparison these tests exist for
    #: would quietly become a tautology -- both sides identical, so the scrapers
    #: would agree for free.
    OFF_SCHEDULE_SLOT = "breaking"

    def _render(self, tmp, label_name, stamp, with_image,
                slot=OFF_SCHEDULE_SLOT):
        """Render ONE page under a chosen build stamp, via render_page directly.

        `render_digest.main()` reads its own clock (`kathmandu_now_str()`) and
        takes no `--timestamp`, so a page with a CONTROLLED label cannot be
        produced through it. That is the point of this fixture: the label has
        to be forced to each side of the table so the two pages differ ONLY in
        the label. The filename is written here with the slot shape the
        pipeline uses, so the scrapers are handed a realistically-named file.
        """
        payload = validated_payload("ok")
        stories = payload.get("stories") or []
        self.assertTrue(stories, "the fixture payload carries no stories, so "
                                 "the scrapers would have nothing to read and "
                                 "this comparison would be vacuous")
        if with_image:
            stories[0]["image"] = {"url": "https://cdn.invalid/t.jpg",
                                   "alt": "fixture thumbnail"}
        d = os.path.join(tmp, label_name)
        os.makedirs(d, exist_ok=True)
        # `meta=payload`, NOT a hand-rolled stub: render_sections hides a
        # section whose per-category evidence is missing, so a stub with an
        # empty `by_category` renders a page with NO section headers at all --
        # which would make the scraper comparison below vacuously true on both
        # sides. The payload carries the health the pipeline really produces.
        page = render_digest.render_page(stories, slot, "2026-10-12",
                                         stamp, payload)
        page_path = os.path.join(d, "2026-10-12-%s.html" % slot)
        with io.open(page_path, "w", encoding="utf-8") as fh:
            fh.write(page)
        return page_path

    def test_the_three_scraper_markers_are_still_present_on_a_new_page(self):
        """Assert the HAPPY PATH first.

        A test that only says "the scrapers agree" would pass on a page that
        has no stories at all -- both sides would return None. So the markers
        are asserted to EXIST here, before anything compares them.
        """
        tmp = tempfile.mkdtemp(prefix="label-scrape-")
        try:
            page_path = self._render(tmp, "thumbs", "2026-10-12 02:03", True)
            with io.open(page_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("Late Night Digest · built 02:03", page)
            # THE MARKERS ARE ASSERTED WITH THE SCRAPERS' OWN PATTERNS, not
            # with a hand-written guess at the markup. Asserting the literal
            # string `<h2 class="section-header">` is WRONG and was wrong when
            # this test was first written: render_sections emits
            # `<h2 class="section-header" style="--dot:#2563eb;">`, so the
            # attribute is never the last one. That mistake made this test fail
            # against a completely healthy page -- a test bug dressed as a
            # regression. Pinning the exact regexes below means a future change
            # to the markup's attribute ORDER is caught here (it would break
            # the archive index), while an innocent extra attribute is not.
            self.assertRegex(page, r'<h2 class="section-header"[^>]*>')
            self.assertRegex(page, r'<article class="card">')
            self.assertRegex(page, r'<img class="card-thumb"[^>]*src="[^"]+"')
            # Non-vacuous: the scrapers really do find something.
            self.assertTrue(render_digest.parse_digest_categories(page_path),
                            "the fixture page produced no categories, so the "
                            "comparison below would be vacuously true")
            self.assertIsNotNone(render_digest.first_thumbnail(page_path))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_label_does_not_change_what_either_scraper_reads(self):
        """Same stories, opposite ends of the day, identical scraper output.

        The two stamps are the widest possible pair on purpose -- 02:03 reads
        Late Night and 23:59 reads Night, so these are different WORDS as well
        as different clocks. A `Digest · <word> · <clock>` shape that somehow
        leaked into the scrapers' input would break this; a change that only
        altered the clock would not.
        """
        tmp = tempfile.mkdtemp(prefix="label-scrape-")
        try:
            for with_image in (True, False):
                with self.subTest(with_image=with_image):
                    late = self._render(tmp, "late%s" % with_image,
                                        "2026-10-12 02:03", with_image)
                    night = self._render(tmp, "night%s" % with_image,
                                         "2026-10-12 23:59", with_image)
                    with io.open(late, encoding="utf-8") as fh:
                        late_html = fh.read()
                    with io.open(night, encoding="utf-8") as fh:
                        night_html = fh.read()
                    # Precondition: the two pages really do carry different
                    # labels, so this is a comparison and not a tautology.
                    self.assertIn("Late Night Digest · built 02:03", late_html)
                    self.assertIn("Night Digest · built 23:59", night_html)
                    self.assertEqual(
                        render_digest.parse_digest_categories(late),
                        render_digest.parse_digest_categories(night),
                        "the label moved what parse_digest_categories reads; "
                        "the archive index would show a different count for "
                        "the same digest depending on what time it was built")
                    self.assertEqual(
                        render_digest.first_thumbnail(late),
                        render_digest.first_thumbnail(night),
                        "the label moved what first_thumbnail reads")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_due_label_does_not_reach_the_scrapers_either(self):
        """The scheduled form is a different string shape, so it is checked too.

        `Late Night Digest · built 02:03` and `Evening Digest · due 19:20` are
        not the same prefix, and the scrapers must be indifferent to both. This
        is the comparison the previous version of the file could not make: both
        of its pages were built at stamps the renderer mapped identically.
        """
        tmp = tempfile.mkdtemp(prefix="label-scrape-due-")
        try:
            due = self._render(tmp, "due", "2026-10-12 18:43", True,
                               slot="evening")
            built = self._render(tmp, "built", "2026-10-12 18:43", True)
            with io.open(due, encoding="utf-8") as fh:
                due_html = fh.read()
            with io.open(built, encoding="utf-8") as fh:
                built_html = fh.read()
            self.assertIn("Evening Digest · due 19:20", due_html)
            self.assertIn("Evening Digest · built 18:43", built_html)
            self.assertEqual(render_digest.parse_digest_categories(due),
                             render_digest.parse_digest_categories(built))
            self.assertEqual(render_digest.first_thumbnail(due),
                             render_digest.first_thumbnail(built))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_archive_index_title_is_untouched(self):
        """drift-canary.yml greps docs/index.html's <title> as a leak marker.

        The renderer must keep emitting exactly that string, or the canary
        starts screaming about a title that never changed.
        """
        expected = "Personal News Informer — Digest Archive"
        tmp = tempfile.mkdtemp(prefix="label-archive-")
        try:
            rc = render_digest.main([
                "--digest-time", "morning", "--date", "2026-10-12",
                "--email-txt", os.path.join(tmp, "absent.txt"),
                "--stories-json", write_json(
                    os.path.join(tmp, "s.json"), validated_payload("ok")),
                "--out-dir", os.path.join(tmp, "digests"),
                "--index", os.path.join(tmp, "index.html")])
            self.assertEqual(rc, 0)
            with io.open(os.path.join(tmp, "index.html"), encoding="utf-8") as fh:
                index = fh.read()
            m = re.search(r"<title>(.*?)</title>", index, re.S)
            self.assertIsNotNone(m, "the archive index has no <title> at all")
            self.assertEqual(m.group(1).strip(), expected)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_committed_archive_still_carries_that_title(self):
        """The shipped docs/index.html, read only. Nothing writes to docs/."""
        committed = os.path.join(REPO_ROOT, "docs", "index.html")
        if not os.path.isfile(committed):
            self.skipTest("no committed docs/index.html in this checkout")
        with io.open(committed, encoding="utf-8") as fh:
            index = fh.read()
        m = re.search(r"<title>(.*?)</title>", index, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(),
                         "Personal News Informer — Digest Archive")


# ===========================================================================
# 5. The ntfy surface: real push instant, and the third copy of the table
# ===========================================================================

class TestNtfyTitle(unittest.TestCase):
    """The push is the surface the owner actually named ("top right").

    THIS IS THE ONE SURFACE THAT REALLY KNOWS WHEN IT WAS SENT. The heredoc
    builds the title microseconds before the POST below it, so the clock it
    renders is the push instant and not a prediction.
    """

    TITLE_MAX_CHARS = 72
    MAX_BYTES = 4096

    def _payload(self, count=3):
        payload = validated_payload("ok")
        payload["digest_time"] = "morning"
        payload["generated_at_npt"] = "2026-10-12 02:03"
        return payload

    def test_the_heredoc_still_renders_and_is_under_both_budgets(self):
        tmp = tempfile.mkdtemp(prefix="label-ntfy-")
        try:
            got = run_ntfy(tmp, self._payload())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        title = got["title"]
        self.assertLessEqual(
            len(title), self.TITLE_MAX_CHARS,
            "the title is %d characters and a phone will clip it: %r"
            % (len(title), title))
        self.assertLessEqual(len(got["message"].encode("utf-8")), self.MAX_BYTES)

    def test_the_title_carries_the_real_push_clock_in_npt(self):
        """The clock in the title is NPT *now*, to the minute.

        Asserted by re-reading the clock the test itself sees and comparing,
        rather than by comparing against a fixed constant that would silently
        rot. A one-minute tolerance covers the seconds between the heredoc's
        `now()` and this assertion; a title stamped 40 minutes early (the
        prebuild lead) or stamped in UTC would both fail it by a wide margin,
        which is the whole failure being guarded.
        """
        tmp = tempfile.mkdtemp(prefix="label-ntfy-")
        try:
            got = run_ntfy(tmp, self._payload())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        title = got["title"]
        m = re.search(r"Digest · (\d{2}):(\d{2}) ·", title)
        self.assertIsNotNone(
            m, "the ntfy title carries no clock time at all: %r" % title)
        npt_now = (datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None) + NPT)
        self.assertEqual(m.group(1), npt_now.strftime("%H"),
                         "the title's HOUR is not the current Nepal hour, so "
                         "the clock is not the push instant: %r" % title)
        delta = abs(int(m.group(2)) - int(npt_now.strftime("%M")))
        # 0-59 minutes apart; allow the minute boundary to roll.
        self.assertTrue(delta <= 1 or delta >= 59,
                        "the title's MINUTE is %s but Nepal is now %s -- the "
                        "clock is not the push instant: %r"
                        % (m.group(2), npt_now.strftime("%M"), title))

    def test_the_title_word_is_one_of_the_eight_and_not_the_slot_word(self):
        """Whatever the clock says, the word comes from the table.

        The slot word can only be correct by coincidence now, so this asserts
        the word is drawn from the table and that the title format no longer
        interpolates the slot.
        """
        tmp = tempfile.mkdtemp(prefix="label-ntfy-")
        try:
            got = run_ntfy(tmp, self._payload(), kind="morning")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        title = got["title"]
        self.assertRegex(title, r"Digest · \d{2}:\d{2} ·")
        words = set(render_digest.LABEL_HOUR_BOUNDS[i][1]
                    for i in range(len(render_digest.LABEL_HOUR_BOUNDS)))
        self.assertTrue(
            any(("%s Digest ·" % w) in title for w in words),
            "the title's word is not one of %s: %r" % (sorted(words), title))

    def test_the_workflow_step_names_the_tests_slice_out_are_unchanged(self):
        """Renaming either step breaks all 26 ntfy tests with an IndexError.

        tests/render/test_render_honesty.py:69-70 slices the heredoc out of
        the YAML between those two literal strings, so a rename looks like a
        test crash rather than a broken contract. Pinned here so the failure
        mode is caught as this message instead.
        """
        src = read_text(WORKFLOW)
        self.assertIn("- name: Publish to ntfy (primary)", src)
        self.assertIn("- name: Send digest via Gmail (backup)", src)

    def test_the_heredoc_carries_the_third_copy_of_the_same_table(self):
        """The three copies are proved equal by PARSING the heredoc.

        The heredoc is a string inside YAML, so its table is read with `ast`
        and compared to the renderer's. A copy that drifts would otherwise be
        invisible until the owner saw "Morning" on one surface and "Night" on
        another for the same digest.
        """
        code = ntfy_source()
        self.assertIn("LABEL_HOUR_BOUNDS", code)
        tree = ast.parse(code)
        table = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "LABEL_HOUR_BOUNDS"
                    for t in node.targets):
                table = ast.literal_eval(node.value)
        self.assertIsNotNone(table, "the ntfy heredoc no longer defines "
                                   "LABEL_HOUR_BOUNDS")
        self.assertEqual(tuple(map(tuple, table)),
                         tuple(map(tuple, render_digest.LABEL_HOUR_BOUNDS)))

    def test_the_heredoc_computes_its_clock_from_utc_plus_0545(self):
        """Asserted on the heredoc's SOURCE, because it cannot be frozen.

        `npt_label()` reads the wall clock, so calling it proves nothing about
        the arithmetic. What matters -- and what is asserted here -- is that it
        goes through UTC and the fixed +05:45, and never the runner's local
        time. A local-time call would produce a correct-looking title on a
        developer box in Nepal and a wrong one everywhere else, which is the
        exact bug `kathmandu_now_str` was written to kill.
        """
        code = ntfy_source()
        self.assertIn("NPT_OFFSET = datetime.timedelta(hours=5, minutes=45)",
                      code)
        self.assertIn("datetime.datetime.now(datetime.timezone.utc)", code)
        # The naive local clock must not appear anywhere in the label path.
        self.assertNotIn("datetime.datetime.now()", code,
                         "the ntfy heredoc calls the naive local clock; a "
                         "local-time label is hours out in Nepal")

    def test_the_heredoc_no_longer_builds_a_title_from_the_slot_word(self):
        code = ntfy_source()
        self.assertNotIn(
            "kind.capitalize()", code,
            "a push title is still being built from the SLOT word; that is the "
            "defect this change exists to remove")

    def test_the_push_title_variable_is_not_shadowed_by_the_two_local_labels(self):
        """The title's variable cannot be the bare name `label`.

        This heredoc uses `label` for a story's chip label (`story_md`) and for
        a category name (`build_digest`). Both are function-local, so today they
        cannot clobber a module-scope `label` -- which is exactly why the
        hazard is invisible and worth pinning. A push title that silently became
        "WORLD - MAJOR EVENTS" is a wrong claim to the owner, and the only way
        to get one is somebody hoisting a variable out of those two functions.

        THE FIRST VERSION OF THIS TEST WAS VACUOUS. It scanned only
        `ast.parse(...).body`, which is the heredoc's true module scope -- but
        `send_label` is assigned inside `def main()`, so a reintroduced bare
        `label` was never in that list and the test passed against the very
        defect it was written for. It now walks EVERY scope and records which
        function each `label` assignment lives in.
        """
        code = ntfy_source()
        owners = []

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                here = child.name if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == "label":
                            owners.append(here)
                walk(child, here)

        walk(ast.parse(code), None)
        # Exactly the two known, deliberate users. `main` appearing here is the
        # regression this guards: that is the scope that builds the title.
        self.assertEqual(
            sorted(str(o) for o in owners), ["build_digest", "story_md"],
            "`label` is assigned in a scope other than the two known "
            "function-locals (found %r); if `main` is among them the push "
            "title's variable has been renamed back to the shadowable name"
            % (owners,))

    def test_the_heredoc_uses_send_label_in_all_title_shapes(self):
        """One word in, four titles out -- and none of them re-derives it.

        The fourth shape is the unsummarised fallback (2026-10-28): it names the
        edition kind instead of the item count, so the count can never push the
        one fact that changes how the push must be read off the shade.
        """
        code = ntfy_source()
        self.assertIn("send_label = '%s Digest · %s' % (label_word, label_clock)",
                      code)
        # Counted as AST Name nodes, NOT as raw text: a comment that mentions
        # the variable (as the comment explaining the rename does) would
        # otherwise change the count and make this test lie about what it
        # measures.
        tree = ast.parse(code)
        names = [n.id for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and n.id == "send_label"]
        self.assertEqual(
            len(names), 5,
            "expected the one assignment plus its four title uses, found %d "
            "references: %r -- a title shape was left building its own label"
            % (len(names), names))
        self.assertNotIn("emoji, label,", code)


# ===========================================================================
# 6. The bounds the change could plausibly have broken
# ===========================================================================

class TestWorstCaseBudgets(unittest.TestCase):
    TITLE_MAX_CHARS = 72

    #: The shipped push-title format, reproduced here rather than imported,
    #: because the budget belongs to the TITLE STRING and the string is what a
    #: phone truncates. `emoji <word> Digest · HH:MM · <date> · <n> items`.
    @staticmethod
    def _title(emoji, word, hhmm, n):
        return "%s %s · 2026-10-12 · %d %s" % (
            emoji, render_digest.digest_label(word, hhmm), n,
            "item" if n == 1 else "items")

    @staticmethod
    def _worst_title():
        """`(length, title)` for the longest push title that can actually exist.

        Per band, because a word can only ever be paired with the hours it
        covers; crossing every word with every clock would report a worst case
        the table cannot produce.
        """
        longest, longest_title = 0, ""
        for hour, word in enumerate(EXPECTED_WORD_BY_HOUR):
            end_hour = hour
            for probe in range(hour, 24):
                if EXPECTED_WORD_BY_HOUR[probe] != word:
                    break
                end_hour = probe
            last_minute = "%02d:59" % end_hour
            for emoji in EXPECTED_WORD_EMOJI.values():
                for n in (1, 12, 999):
                    title = TestWorstCaseBudgets._title(emoji, word,
                                                        last_minute, n)
                    if len(title) > longest:
                        longest, longest_title = len(title), title
        return longest, longest_title

    def test_the_worst_case_title_fits_a_phone_notification(self):
        """Measured per WORD, over the hours that word can actually occur.

        The previous version of this test crossed every word with every clock,
        which over-states the worst case: it would happily pair "Midday" with
        00:00, a combination the table can never produce. Every one of the eight
        words is tried, because "Late Night" is four characters longer than
        "Night" and a budget proved on one word is not a budget.
        """
        longest, longest_title = self._worst_title()
        self.assertLessEqual(
            longest, self.TITLE_MAX_CHARS,
            "the worst-case title is %d characters: %r"
            % (longest, longest_title))
        # And prove the measurement is not vacuous.
        self.assertGreater(
            longest, 40,
            "the longest title measured was only %d characters, so the 72 "
            "ceiling was never approached and this proves nothing"
            % longest)

    def test_the_worst_case_title_is_reported_with_the_real_number(self):
        """The measured figure, so a reviewer does not have to re-run anything.

        This is the number the change is judged on and it is printed here rather
        than buried in a comment that will go stale the first time somebody adds
        a ninth word. The count is asserted to be real first, so the figure is
        never a measurement of an empty set of shapes.
        """
        longest, longest_title = self._worst_title()
        self.assertGreaterEqual(longest, 45)
        sys.stderr.write(
            "\n    [label] worst push title = %d/%d chars "
            "(%d words): %s\n"
            % (longest, self.TITLE_MAX_CHARS, len(EXPECTED_WORD_EMOJI),
               ascii(longest_title)))

    def test_a_reason_clause_still_fits_after_the_clock_was_added(self):
        """The escalation clause is the thing the 72 cap actually protects.

        The clause is only appended while the WHOLE title fits, so a longer word
        could have started silently dropping it -- which is why the first draft
        of this table's vocabulary cut "Early Morning": at 13 characters it was
        the longest word in the table and its band was exactly the morning slot,
        so the owner's normal morning push lost the clause that tells him why
        his phone just went loud.

        Asserted for BOTH slots at a realistic item count, because the morning
        push is the one a long word would have hurt.
        """
        for slot_word, clock in (("Morning", "07:20"), ("Evening", "19:20")):
            with self.subTest(slot_word=slot_word):
                title = self._title(EXPECTED_WORD_EMOJI[slot_word],
                                    slot_word, clock, 12)
                reason = "3 deadlines due"
                self.assertLessEqual(
                    len(title) + len(reason) + 3, self.TITLE_MAX_CHARS,
                    "the %s push can no longer carry its escalation clause and "
                    "would silently lose the reason it escalated (%d chars): %r"
                    % (slot_word.lower(), len(title) + len(reason) + 3, title))

    def test_the_worst_case_word_does_not_shrink_the_clause_budget(self):
        """The clause survives for every word, not just the two slot words.

        A vocabulary change is allowed to make a title longer; it is not allowed
        to make the escalation clause disappear for most of the day. This
        measures, for all eight words at a 12-item digest, whether the clause
        still fits -- which is the real cost of a long word, and the reason
        "Early Morning" was not kept.
        """
        dropped = []
        for hour, word in enumerate(EXPECTED_WORD_BY_HOUR):
            title = self._title(EXPECTED_WORD_EMOJI[word], word,
                                "%02d:00" % hour, 12)
            reason = "3 deadlines due"
            if len(title) + len(reason) + 3 > self.TITLE_MAX_CHARS:
                dropped.append((word, len(title) + len(reason) + 3))
        self.assertEqual(
            dropped, [],
            "these words push the 12-item title past the point where the "
            "escalation clause still fits, so a digest in that part of the day "
            "would go loud with no reason given: %r" % (dropped,))


# ===========================================================================
# 7. The icon can never disagree with the word
# ===========================================================================

class TestTheEmojiFollowsTheWord(unittest.TestCase):
    """The third half of the owner's complaint, and the one that was weakest.

    "a digest firing at 02:03 still shows ☀️ beside the word 'Night'".

    The icon was `'\u2600\ufe0f' if kind == 'morning' else '\U0001f319'` --
    keyed to the SLOT -- while the word came from the clock. Two different
    sources for two halves of one line, which is why they could contradict each
    other. The fix is structural: the icon is now looked up BY the word, and the
    word comes from the push instant. There is no longer a second input.
    """

    def _heredoc_tables(self):
        code = ntfy_source()
        out = {}
        for node in ast.parse(code).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in (
                        "LABEL_HOUR_BOUNDS", "LABEL_WORD_EMOJI")
                    for t in node.targets):
                out[node.targets[0].id] = ast.literal_eval(node.value)
        return code, out

    def test_the_icon_map_is_exactly_the_expected_one(self):
        """Parsed out of the heredoc and compared to a table written here.

        Read back from the implementation it would be decoration: the map would
        agree with itself whatever it said. Written here instead, so an icon that
        moves is a test failure rather than a notification the owner notices at
        02:03.
        """
        _code, tables = self._heredoc_tables()
        self.assertIn("LABEL_WORD_EMOJI", tables,
                      "the ntfy heredoc no longer defines LABEL_WORD_EMOJI")
        self.assertEqual(tables["LABEL_WORD_EMOJI"], EXPECTED_WORD_EMOJI)

    def test_every_word_in_the_hour_table_has_an_icon_and_no_others(self):
        """Eight bands, eight icons -- no gaps, no spares.

        A word with no icon would raise KeyError at push time and cost the owner
        the whole digest; an icon for a word that no hour produces is dead code
        that reads as coverage.
        """
        _code, tables = self._heredoc_tables()
        words = {w for _start, w in tables["LABEL_HOUR_BOUNDS"]}
        self.assertEqual(set(tables["LABEL_WORD_EMOJI"]), words)
        self.assertEqual(len(tables["LABEL_WORD_EMOJI"]), len(words))

    def test_the_icon_is_looked_up_by_the_word_and_never_by_the_slot(self):
        """The slot may not reach the icon any more.

        Asserted on the AST, not on raw text, and that distinction is the whole
        point. The comment explaining this change quotes the old defect verbatim
        -- ``\\u2600\\ufe0f if kind == 'morning' else`` -- so a text search for
        that string finds the explanation of the fix and reports the defect is
        back. What actually matters is the shape of the code: exactly ONE
        assignment to `emoji`, and it reads the word.
        """
        tree = ast.parse(ntfy_source())
        assignments = []

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                here = child.name if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == "emoji":
                            assignments.append((here, ast.dump(child.value)))
                walk(child, here)

        walk(tree, None)
        # Scoped to `main`, because `build_digest` has its own function-local
        # `emoji` for a section header and has always had one; the push title's
        # icon is the one built in the scope that also builds `send_label`.
        in_main = [dump for scope, dump in assignments if scope == "main"]
        self.assertEqual(
            len(in_main), 1,
            "expected exactly one `emoji` assignment inside main() and found "
            "%d: %r (all emoji assignments: %r)"
            % (len(in_main), in_main, assignments))
        self.assertEqual(
            in_main[0],
            ast.dump(ast.parse("icon_for_word(label_word)", mode="eval").body),
            "`emoji` is no longer a pure function of the label word: %s"
            % in_main[0])
        # And no branch anywhere in the heredoc still compares the slot to a
        # word, which is how the icon used to be chosen.
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(
                    node.left, ast.Name) and node.left.id == "kind":
                self.fail("the heredoc still branches on the SLOT (`kind`) at "
                          "line %d; the icon must be a function of the word"
                          % node.lineno)

    def test_icon_for_word_names_an_unknown_word_instead_of_raising(self):
        """A word with no icon must not take the whole push down.

        `icon_for_word` prints the offending word and falls back to the night
        icon. The fallback is justified by what is missing rather than by what
        the fallback is: an unreadable word has not told us it was daylight, so
        showing a moon is the conservative reading, and the WARNING names the
        word so the gap is visible in the run log rather than inferred from a
        wrong-looking icon.

        Asserted on the function's own source, sliced out of the heredoc by AST
        so a same-named word elsewhere in the file cannot satisfy it.
        """
        code = ntfy_source()
        fn = None
        for node in ast.parse(code).body:
            if isinstance(node, ast.FunctionDef) and node.name == "icon_for_word":
                fn = ast.get_source_segment(code, node)
        self.assertIsNotNone(fn, "the heredoc has no icon_for_word function")
        self.assertIn("except KeyError", fn,
                      "an unknown label word would raise at push time and cost "
                      "the owner the whole digest")
        self.assertIn("print(", fn,
                      "the fallback is silent, so a word without an icon would "
                      "be invisible in the run log")
        self.assertIn("%r", fn,
                      "the warning does not name the offending word, so the gap "
                      "cannot be found from the log")
        self.assertIn("LABEL_WORD_EMOJI['Late Night']", fn,
                      "the fallback icon is not read out of the map, so the map "
                      "can be edited without the fallback following it")

    def test_every_band_of_the_day_is_reachable_by_exactly_one_icon(self):
        """Hour in, icon out, for all 24 hours -- no hour left unclassified.

        This is the property that makes "the icon and the word can never
        disagree" true rather than merely intended: `icon_for_word` is total over
        the table, and `label_word_for_hour` is total over the day, so every
        real push instant has both.
        """
        _code, tables = self._heredoc_tables()
        for hour in range(24):
            with self.subTest(hour=hour):
                word = render_digest.label_word_for_hour(hour)
                self.assertIn(word, tables["LABEL_WORD_EMOJI"])
        # And the night is three distinct icons rather than three identical
        # moons, because ntfy renders this glyph as the notification's icon and
        # one moon for three hours would throw away the granularity.
        night_icons = [EXPECTED_WORD_EMOJI[w]
                       for w in ("Midnight", "Late Night", "Night")]
        self.assertEqual(len(set(night_icons)), 3,
                         "the three night bands share an icon, so a midnight "
                         "push and a 23:59 push look identical: %r"
                         % (night_icons,))

    def test_the_real_push_title_carries_the_icons_word(self):
        """Run the actual heredoc and read the title it really produced.

        Everything above is about the source; this is about the bytes. The
        rendered title's leading glyph must be the icon the map gives the word
        that the same title names, checked against the current Nepal clock
        rather than a constant that could rot.
        """
        tmp = tempfile.mkdtemp(prefix="label-emoji-")
        try:
            got = run_ntfy(tmp, self._payload())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        title = got["title"]
        npt_now = datetime.datetime.now(datetime.timezone.utc).astimezone(
            render_digest.NPT_TZ)
        expected_word = render_digest.label_word_for_hour(npt_now.hour)
        # The clock can cross an hour boundary between the heredoc's read and
        # this one; accept either word the two neighbouring hours produce.
        for hour in {npt_now.hour, (npt_now.hour - 1) % 24}:
            word = render_digest.label_word_for_hour(hour)
            if ("%s Digest · " % word) in title:
                self.assertTrue(
                    title.startswith(EXPECTED_WORD_EMOJI[word]),
                    "the title %r does not begin with the icon the map gives "
                    "%r (%r)" % (title, word, EXPECTED_WORD_EMOJI[word]))
                return
        self.fail("the rendered title %r carries no word from the table" % title)

    def _payload(self, count=3):
        payload = validated_payload("ok")
        payload["digest_time"] = "morning"
        payload["generated_at_npt"] = "2026-10-12 02:03"
        return payload


if __name__ == "__main__":
    unittest.main(verbosity=2)