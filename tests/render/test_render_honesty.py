#!/usr/bin/env python3
"""tests/render/test_render_honesty.py — FROZEN CONTRACT B and C.

The point of these tests is that an outage can never be silent:

  B  every section in SECTIONS always renders, with exactly one status line
     when it has no items, so "TU vanished" is impossible;
  C  the freshness line can never claim currency it does not have, and
     `unknown` drops the timestamp entirely.

Both renderers (website + email html/text) and the ntfy heredoc are held to
the SAME literal strings.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import (  # noqa: E402
    B2, B3, B4, B5, BANNER, CUTOFF, CUTOFF_DEGRADED, CUTOFF_OK, CUTOFF_PARTIAL,
    CUTOFF_UNKNOWN, NOTE_PREFIX, TU_HOSTS, TU_URLS, WORKFLOW, cat_entry, health,
    twenty_story_payload, validated_payload, write_json)

import build_email  # noqa: E402
import render_digest  # noqa: E402

MAX_BYTES = 4096

# ---------------------------------------------------------------------------
# v2 literals: the `empty` state (answered, nothing usable).
#
# Defined HERE rather than in fixtures.py on purpose: fixtures.py is not an
# owned file, and a shared constant for an unowned renderer's behaviour is how
# the two renderers drift apart silently in the first place.
# ---------------------------------------------------------------------------
B6 = ("Nothing new from this source today (checked — the source returned "
      "nothing usable).")
BANNER_EMPTY = ("⚠️ Some sources answered, but returned nothing usable — "
                "treat this digest as incomplete.")
CUTOFF_EMPTY = ("Information checked through: %s NPT (incomplete: some sources "
                "returned nothing usable)" % CUTOFF)


def cat_entry_empty_hosts(total, ok, empty, failed, skipped, empty_hosts,
                          items_found=0, urls=None):
    """A v2 by_category entry.

    Built here rather than via fixtures.cat_entry(), which predates
    `empty_hosts`/`items_found` and is not an owned file.
    """
    entry = {"total": total, "ok": ok, "empty": empty, "failed": failed,
             "skipped": skipped, "items_found": items_found,
             "empty_hosts": list(empty_hosts),
             "urls": [] if urls is None else list(urls)}
    return entry


def ntfy_source():
    """The ntfy step's python heredoc, dedented and ready to exec."""
    with io.open(WORKFLOW, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("- name: Publish to ntfy (primary)")
    end = src.index("- name: Send digest via Gmail (backup)")
    chunk = src[start:end]
    m = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", chunk, re.S)
    assert m, "ntfy heredoc not found in daily-news.yml"
    return "\n".join(ln[10:] if ln.startswith(" " * 10) else ln
                     for ln in m.group(1).split("\n"))


def run_ntfy(tmpdir, payload, kind="morning"):
    """Exec the real ntfy heredoc in DRY RUN; return the payload it printed.

    DRY_RUN=true makes the step print the payload and return before the
    urlopen, and urlopen is additionally poisoned, so this can never make a
    network call. The assertion on the DRY RUN marker proves which branch ran.
    """
    import urllib.request

    os.makedirs(os.path.join(tmpdir, "state"), exist_ok=True)
    write_json(os.path.join(tmpdir, "state", "stories.validated.json"), payload)
    env_backup = dict(os.environ)
    os.environ.update({
        "NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.invalid", "DRY_RUN": "true",
        "TODAY": "2026-09-27", "DIGEST_TIME": kind,
    })

    def _no_network(*_args, **_kwargs):
        raise AssertionError("ntfy test attempted a network call")

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _no_network
    cwd = os.getcwd()
    buf = io.StringIO()
    try:
        os.chdir(tmpdir)
        with contextlib.redirect_stdout(buf):
            exec(compile(ntfy_source(), "daily-news.yml:ntfy", "exec"), {"__name__": "ntfy"})
    finally:
        urllib.request.urlopen = real_urlopen
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(env_backup)
    text = buf.getvalue()
    assert "DRY RUN" in text, \
        "ntfy heredoc did not take the DRY RUN path (a real send?):\n%s" % text[:400]
    marker = "Payload preview:"
    got, _end = json.JSONDecoder().raw_decode(
        text[text.index(marker) + len(marker):].lstrip())
    return got


# ---------------------------------------------------------------------------
# Contract B - section status lines (both renderers agree, literally)
# ---------------------------------------------------------------------------

class TestSectionStatusLines(unittest.TestCase):
    """B1-B5, asserted against BOTH renderers with the same literals."""

    def _assert_both(self, category, items, health_obj, expected):
        self.assertEqual(render_digest.section_status_line(category, items, health_obj),
                         expected)
        self.assertEqual(build_email.section_status_line(category, items, health_obj),
                         expected)

    def test_b1_items_and_no_failures_says_nothing(self):
        self._assert_both("nepal", 3, health("ok", by_category={
            "nepal": cat_entry(4, 4, 0, 0, urls=[])}), "")

    def test_b2_items_with_failures_reports_the_count(self):
        self._assert_both("education_tu", 2, health("degraded", by_category={
            "education_tu": cat_entry(11, 9, 2, 0)}), B2 % (2, 11))

    def test_b3_zero_items_and_absent_category_entry(self):
        self._assert_both("manga_manhwa_manhua", 0, health("unknown", by_category={}), B3)

    def test_b3_zero_items_and_skipped_sources(self):
        self._assert_both("gaming", 0, health("partial", by_category={
            "gaming": cat_entry(5, 4, 0, 1, urls=[])}), B3)

    def test_b3_when_source_health_is_absent_entirely(self):
        self._assert_both("gaming", 0, None, B3)
        self._assert_both("gaming", 0, {}, B3)

    def test_b4_zero_items_healthy_sources(self):
        self._assert_both("gaming", 0, health("ok", by_category={
            "gaming": cat_entry(5, 5, 0, 0, urls=[])}), B4)

    def test_b5_zero_items_and_failed_points_at_the_url(self):
        self._assert_both("education_tu", 0, health("degraded", by_category={
            "education_tu": cat_entry(11, 9, 2, 0)}, hosts_down=TU_HOSTS),
            B5 % " or ".join(TU_URLS))

    def test_b5_with_a_single_url_omits_the_or_tail(self):
        self._assert_both("college_local", 0, health("degraded", by_category={
            "college_local": cat_entry(3, 2, 1, 0, urls=["https://college.example.edu/"])}),
            B5 % "https://college.example.edu/")

    def test_b5_without_urls_never_invents_one(self):
        line = render_digest.section_status_line(
            "gaming", 0, health("degraded", by_category={
                "gaming": cat_entry(5, 4, 1, 0, urls=[])}))
        self.assertEqual(line, B3)
        self.assertNotIn("check", line.replace("Source check", ""))

    def test_check_order_b3_wins_over_b5_when_skipped(self):
        """A skipped source makes the section unmeasured, not unnewsworthy."""
        self._assert_both("gaming", 0, health("partial", by_category={
            "gaming": cat_entry(5, 3, 1, 1, urls=["https://x.example/"])}), B3)

    def test_zero_item_total_falls_back_to_b4(self):
        self._assert_both("bank_local", 0, health("ok", by_category={
            "bank_local": cat_entry(0, 0, 0, 0, urls=[])}), B4)


class TestEmptyStateAndB6(unittest.TestCase):
    """v2: B4's confidence is now a NARROWER claim than it used to be.

    Before v2, `section_status_line` could only say "Nothing new from this
    source today." -- the confident unqualified B4 -- whether the source
    answered with real notices or answered with nothing usable at all. A
    category whose hosts returned garbage therefore borrowed B4's confidence.

    B6 is the same "nothing new" with the honest qualifier attached, and the
    invariant is that B4 is reachable ONLY when every reachable host in that
    category produced items.
    """

    def test_b6_literal_is_exact(self):
        self.assertEqual(
            render_digest.section_status_line(
                "education_tu", 0,
                health("empty", by_category={
                    "education_tu": cat_entry_empty_hosts(
                        3, 3, 1, 0, 0, ["iost.tu.edu.np"], items_found=0)})),
            B6)

    def test_b6_replaces_b4_when_a_category_host_was_empty(self):
        """THE LOAD-BEARING INVARIANT, both directions."""
        empty = health("empty", by_category={
            "education_tu": cat_entry_empty_hosts(
                3, 3, 1, 0, 0, ["iost.tu.edu.np"])})
        self.assertEqual(render_digest.section_status_line("education_tu", 0, empty),
                         B6)
        self.assertNotEqual(render_digest.section_status_line(
            "education_tu", 0, empty), B4)

        # Same counts, but the host produced items -> empty_hosts is empty and
        # B4 is reachable again.
        productive = health("ok", by_category={
            "education_tu": cat_entry_empty_hosts(
                3, 3, 0, 0, 0, [], items_found=20)})
        self.assertEqual(render_digest.section_status_line(
            "education_tu", 0, productive), B4)

    def test_b4_text_is_unchanged_by_v2(self):
        """B4 is the narrower claim, not a different string. Anything reading it
        verbatim must keep working."""
        self.assertEqual(B4, "Nothing new from this source today.")
        self.assertEqual(render_digest.section_status_line(
            "gaming", 0, health("ok", by_category={
                "gaming": cat_entry(5, 5, 0, 0, urls=[])})), B4)

    def test_a_v1_category_entry_without_empty_hosts_still_gets_b4(self):
        """A v1 payload has no `empty_hosts` key. Absence must degrade to the
        v1 behaviour, never raise and never invent an empty host."""
        v1_entry = {"total": 5, "ok": 5, "failed": 0, "skipped": 0, "urls": []}
        self.assertEqual(render_digest.section_status_line(
            "gaming", 0, health("ok", by_category={"gaming": v1_entry})), B4)
        for junk in (None, "iost.tu.edu.np", 5, [None, "  ", 7], {}):
            entry = dict(v1_entry, empty_hosts=junk)
            self.assertEqual(render_digest.section_status_line(
                "gaming", 0, health("ok", by_category={"gaming": entry})), B4, junk)

    def test_b6_does_not_displace_b3_b5_or_b1_b2(self):
        """Order is frozen: B1/B2 (items>0) -> B3 -> B5 -> B6 -> B4.
        B6 sits BELOW the item, skip and failure checks, so it can never mask
        them."""
        # items > 0: B1 wins even with empty_hosts present.
        self.assertEqual(render_digest.section_status_line(
            "education_tu", 2, health("empty", by_category={
                "education_tu": cat_entry_empty_hosts(
                    3, 3, 1, 0, 0, ["iost.tu.edu.np"])})), "")
        # skipped > 0: B3 wins over B6.
        self.assertEqual(render_digest.section_status_line(
            "education_tu", 0, health("empty", by_category={
                "education_tu": cat_entry_empty_hosts(
                    3, 2, 1, 0, 1, ["iost.tu.edu.np"])})), B3)
        # failed > 0: B5 wins over B6, and still points at the real URLs.
        self.assertEqual(render_digest.section_status_line(
            "education_tu", 0, health("degraded", by_category={
                "education_tu": cat_entry_empty_hosts(
                    3, 1, 1, 1, 0, ["iost.tu.edu.np"], urls=TU_URLS)},
                hosts_down=TU_HOSTS)), B5 % " or ".join(TU_URLS))

    def test_b6_is_a_QUIET_status_and_therefore_hides_the_section(self):
        """RE-POINTED (owner-approved). B6 is still a STATUS LINE -- that part
        is unchanged and is proved by the sibling test right above, which asserts
        the literal `section_status` returns. What changed is what the section
        BLOCK does with it: B6 is a QUIET tone, so an empty section carrying only
        B6 is now OMITTED rather than rendered with a grey line.

        That is the correct direction for this specific status: "checked, the
        source returned nothing usable" is a statement about one quiet day, and
        when EVERY source is in that state the page-level banner already says so
        once, in the right tone. Repeating it under a section header, eleven
        times, is the noise the owner asked to lose.

        Section HEADINGS are display names ("TU / BSC CSIT"), not category keys,
        so this asserts on the rendered headings.
        """
        grouped = {key: [] for key, _label, _color in render_digest.SECTIONS}
        grouped["nepal"] = [
            {"title": "t", "summary": "s", "for_you": "", "source_name": "KP",
             "source_url": "https://e.com/1", "reliability": "tier1",
             "category": "nepal", "priority": 5, "published_at": "2026-09-27",
             "supporting_sources": []}]
        health_obj = health("empty", by_category={
            "education_tu": cat_entry_empty_hosts(
                3, 3, 1, 0, 0, ["iost.tu.edu.np"])})
        # The tone is QUIET -- this is the fact the hiding rule keys on.
        self.assertEqual(
            render_digest.section_status("education_tu", 0, health_obj)[1],
            "quiet")
        html = render_digest.render_sections(grouped, health_obj)
        # Only nepal renders: it holds a story. education_tu is empty and B6 is
        # quiet, so it is omitted -- and so is every untracked section.
        self.assertEqual(html.count('<section class="digest-section">'), 1)
        self.assertIn(render_digest.esc("IMPORTANT NEPAL"), html)
        self.assertNotIn(render_digest.esc("TU / BSC CSIT"), html)
        # The B6 literal appears nowhere: not on the page, and -- because
        # render_sections_text feeds the website when the JSON is absent -- not in
        # the text digest either.
        self.assertEqual(html.count(B6), 0)
        self.assertNotIn(B6, build_email.render_sections_text(grouped, health_obj))
        # ...while the B6 literal is still fully reachable as a STATUS, which is
        # what the sibling test above asserts directly. Hidden is not deleted.
        self.assertEqual(
            render_digest.section_status("education_tu", 0, health_obj)[0], B6)

    def test_b6_is_recognised_as_a_status_line_not_a_story(self):
        """A B6 line wearing the legacy `* ` bullet must never be parsed as a
        story title, or the hero count silently inflates.

        A BARE B6 string is deliberately NOT a status line: is_section_status_line
        only claims lines that carry a bullet or the `status:` prefix, and this
        function must not widen that to swallow ordinary prose.
        """
        self.assertTrue(render_digest.is_section_status_line("* " + B6))
        self.assertTrue(render_digest.is_section_status_line("  status: " + B6))
        self.assertTrue(render_digest.is_section_status_line("  status: " + B4))
        # Not a status: a sentence that merely starts with the same words.
        self.assertFalse(render_digest.is_section_status_line(
            "Nothing new from this source today but the mayor disagreed"))

    def test_b6_never_counts_as_a_story_in_a_text_digest(self):
        """The 4-story/6-story defect, re-checked for B6.

        build_email writes each empty section's status line into the text digest
        as a `* ` bullet, and render_digest.parse_email_text() reads it back.
        Every section empty -> a B6 line per section -> all of them must be
        filtered, or the count inflates.
        """
        tmp = tempfile.mkdtemp(prefix="b6-text-")
        try:
            stories = [{"title": "One real story", "summary": "s",
                        "for_you": "", "source_name": "KP",
                        "source_url": "https://e.com/1", "reliability": "tier1",
                        "category": "nepal", "priority": 5,
                        "published_at": "2026-09-27", "supporting_sources": []}]
            payload = validated_payload(
                "empty", stories=stories, by_category={
                    "education_tu": cat_entry_empty_hosts(
                        3, 3, 1, 0, 0, ["iost.tu.edu.np"])})
            payload["stories"] = stories
            json_path = write_json(
                os.path.join(tmp, "stories.validated.json"), payload)
            email_txt = os.path.join(tmp, "email.txt")
            with contextlib.redirect_stdout(io.StringIO()):
                build_email.main([
                    "--in", json_path, "--out-html", os.path.join(tmp, "e.html"),
                    "--out-text", email_txt, "--template", os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(
                            os.path.abspath(__file__)))), "templates", "email.html")])
            with io.open(email_txt, encoding="utf-8") as fh:
                text = fh.read()
            # Only the ONE real story parses out, and the parse is what every
            # count on every surface is computed from.
            parsed = render_digest.parse_email_text(text)
            self.assertEqual([s["title"] for s in parsed], ["One real story"], text)
            # A B6-shaped line in the text is classified as a status, so a
            # reader of the archive never sees it as a headline.
            for line in text.splitlines():
                if B6 in line:
                    self.assertTrue(render_digest.is_section_status_line(line),
                                    line)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_b6_stays_under_the_ntfy_byte_budget(self):
        """Adding a banner state must not blow the 4096-byte push budget.

        NOTE: the ntfy heredoc lives in the workflow file, which is NOT an owned
        file, so it has NOT gained the `empty` state. This test therefore pins
        the budget and that the body still leads with a banner -- it does not
        assert the ntfy side renders B6. That divergence is reported as a
        dependency, not papered over here.
        """
        tmpdir = tempfile.mkdtemp(prefix="b6-ntfy-")
        try:
            got = run_ntfy(tmpdir, twenty_story_payload("empty"))
            self.assertLessEqual(len(got["message"].encode("utf-8")), MAX_BYTES)
            self.assertTrue(got["message"].lstrip().startswith("⚠️"),
                            got["message"][:120])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestEmptyStateLiterals(unittest.TestCase):
    """The two new digest-level literals, verbatim."""

    def test_health_states_gains_empty(self):
        self.assertIn("empty", render_digest.HEALTH_STATES)
        self.assertEqual(render_digest.health_state(health("empty")), "empty")
        # The four pre-existing states are untouched.
        for state in ("ok", "partial", "degraded", "unknown"):
            self.assertIn(state, render_digest.HEALTH_STATES)
            self.assertEqual(render_digest.health_state(health(state)), state)

    def test_banner_literal_is_exact(self):
        self.assertEqual(render_digest.HEALTH_BANNER["empty"], BANNER_EMPTY)
        self.assertEqual(BANNER_EMPTY, "⚠️ Some sources answered, but returned "
                                       "nothing usable — treat this digest as "
                                       "incomplete.")
        self.assertEqual(render_digest.health_banner(health("empty")),
                         BANNER_EMPTY)

    def test_cutoff_suffix_literal_is_exact(self):
        self.assertEqual(render_digest.CUTOFF_SUFFIX["empty"],
                         " (incomplete: some sources returned nothing usable)")
        self.assertEqual(render_digest.cutoff_line(CUTOFF, health("empty")),
                         CUTOFF_EMPTY)

    def test_empty_keeps_the_timestamp_and_unknown_still_drops_it(self):
        """The two states are NOT interchangeable and must not be conflated.

        `empty` means the sources ANSWERED, so the timestamp is real and is
        kept with a qualifier. `unknown` means nothing was confirmed reachable,
        so the timestamp is dropped entirely -- that contract is untouched.
        """
        empty_line = render_digest.cutoff_line(CUTOFF, health("empty"))
        self.assertIn(CUTOFF, empty_line)
        self.assertIn("NPT", empty_line)
        unknown_line = render_digest.cutoff_line(CUTOFF, health("unknown"))
        self.assertNotIn(CUTOFF, unknown_line)
        self.assertNotIn("NPT", unknown_line)
        self.assertNotIn("Information checked through", unknown_line)
        # And the two literals are genuinely different strings.
        self.assertNotIn("Freshness unverified", empty_line)
        self.assertNotIn("incomplete", unknown_line)

    def test_empty_never_reuses_the_unknown_banner_or_suffix(self):
        self.assertNotEqual(render_digest.HEALTH_BANNER["empty"],
                            render_digest.HEALTH_BANNER["unknown"])
        self.assertNotEqual(render_digest.CUTOFF_SUFFIX["empty"],
                            render_digest.HEALTH_BANNER["unknown"])
        self.assertNotIn("Source check unavailable", BANNER_EMPTY)
        self.assertNotIn("Freshness unverified",
                         render_digest.CUTOFF_SUFFIX["empty"])

    def test_empty_still_keeps_the_late_schedule_suffix(self):
        line = render_digest.cutoff_line(CUTOFF, health("empty"),
                                         scheduled="06:00",
                                         generated_at="2026-09-27 06:31")
        self.assertTrue(line.startswith("Information checked through: "))
        self.assertIn("(scheduled 06:00 · runner started 06:31)", line)

    def test_website_hero_carries_the_empty_banner_and_cutoff(self):
        page = render_digest.render_page([], "morning", "2026-09-27",
                                         "2026-09-27 06:31",
                                         validated_payload("empty"))
        self.assertIn(BANNER_EMPTY, page)
        self.assertIn(CUTOFF_EMPTY, page)
        # RE-POINTED (owner-approved). This used to assert that the education_tu
        # section rendered carrying B6. B6 is a QUIET status, so an empty section
        # carrying only B6 is now omitted -- and the claim B6 makes ("the sources
        # answered, and returned nothing usable") is exactly what BANNER_EMPTY
        # already says ONCE, at the top, in the loud tone that belongs to a
        # whole-digest fact. That is the better place for it: it is a statement
        # about the run, not about one quiet section.
        page2 = render_digest.render_page(
            [COUNT_STORIES[0]], "morning", "2026-09-27", "2026-09-27 06:31",
            validated_payload("empty", by_category={
                "education_tu": cat_entry_empty_hosts(
                    3, 3, 1, 0, 0, ["iost.tu.edu.np"])}))
        self.assertNotIn("TU / BSC CSIT", page2)
        self.assertNotIn(B6, page2)
        # The run-level claim is still made, once, and the story still renders.
        self.assertIn(BANNER_EMPTY, page2)
        self.assertIn(CUTOFF_EMPTY, page2)
        self.assertIn("LOCAL &amp; CITY", page2)

    def test_hero_cutoff_class_is_not_the_unverified_one(self):
        """`unknown` is styled as the amber unverified variant. `empty` is a
        real (if incomplete) check, so it must not borrow that class.

        Matched on the class ATTRIBUTE, not a substring: the unverified class
        name contains the verified one ("hero-cutoff-unverified"), so a
        substring assertion would be satisfied by both.
        """
        import re as _re
        page = render_digest.render_page([], "morning", "2026-09-27",
                                         "2026-09-27 06:31",
                                         validated_payload("empty"))
        classes = _re.findall(r'<p class="(hero-cutoff[^"]*)"', page)
        self.assertEqual(classes, ["hero-cutoff"], page[-800:])
        unknown_page = render_digest.render_page([], "morning", "2026-09-27",
                                                 "2026-09-27 06:31",
                                                 validated_payload("unknown"))
        unknown_classes = _re.findall(r'<p class="(hero-cutoff[^"]*)"',
                                      unknown_page)
        self.assertEqual(unknown_classes, ["hero-cutoff hero-cutoff-unverified"])


class TestEverySectionRenders(unittest.TestCase):
    """The structural guarantee, RE-POINTED (owner-approved).

    It used to be "no section may silently disappear", asserted as "all eleven
    sections always render". The owner replaced that rule: a quiet empty section
    is not displayed. The guarantee that SURVIVES, and is asserted here, is the
    one that matters: an ALARM never disappears. Silence about a source the
    owner tracks going dark is the failure mode worth spending tests on; a
    labelled empty section is not.
    """

    def test_an_untrustworthy_health_object_still_renders_every_section(self):
        """The strongest form of the surviving guarantee.

        With health `unknown`, absent, or malformed, section_status cannot tell
        "untracked" from "unmeasured", so EVERY empty section raises the B3
        alarm. Each of those is therefore an alarm section, and every one of them
        must render -- on all three surfaces, for all eleven sections. If this
        fails, an outage has gone silent.
        """
        grouped = {"nepal": [{"title": "t", "summary": "s", "for_you": "w",
                              "category": "nepal", "source_url": "https://e.example/"}]}
        for health_obj, label in ((health("unknown"), "state=unknown"),
                                  (None, "health absent")):
            html_out = render_digest.render_sections(grouped, health_obj)
            self.assertEqual(html_out.count('<section class="digest-section">'),
                             len(render_digest.SECTIONS),
                             "website dropped a section (%s)" % label)
            self.assertIn("TU / BSC CSIT", html_out)
            email_out = build_email.render_sections_html(grouped, health_obj)
            text_out = build_email.render_sections_text(grouped, health_obj)
            for key, header, _color in build_email.SECTIONS:
                plain = (header.replace("&amp;", "&").replace("&#8212;", "--")
                         .replace("&middot;", "-"))
                self.assertIn(plain, text_out,
                              "text digest dropped %s (%s)" % (plain, label))
                self.assertIn(header, email_out,
                              "email html dropped %s (%s)" % (header, label))

    def test_a_tracked_section_with_an_alarm_still_renders_when_empty(self):
        """The same guarantee with a REAL health object rather than a broken one.

        A `degraded` run whose tracked category is down is the commonest real
        outage, and it must produce a rendered section carrying the B5 alarm on
        all three surfaces even though that section has no cards at all.
        """
        degraded = health("degraded", hosts_down=TU_HOSTS, by_category={
            "gaming": cat_entry(5, 4, 1, 0)})
        grouped = {}
        for key, _h, _c in render_digest.SECTIONS:
            grouped[key] = []
        html_out = render_digest.render_sections(grouped, degraded)
        self.assertEqual(html_out.count('<section class="digest-section">'), 1)
        self.assertIn("Source unreachable", html_out)
        self.assertIn("GAMING", html_out)
        for out, label in ((build_email.render_sections_html(grouped, degraded),
                            "email html"),
                           (build_email.render_sections_text(grouped, degraded),
                            "email text")):
            self.assertIn("Source unreachable", out, label)
            self.assertIn("GAMING", out, label)

    def test_a_quiet_empty_section_is_omitted_from_all_three_surfaces(self):
        """The owner's rule, asserted directly. This is what replaced
        "all eleven sections always render", and it is asserted per surface
        rather than by count, so a section that is wrongly dropped fails here.
        """
        quiet = health("ok", by_category={})   # nothing tracked, nothing known
        self.assertEqual(quiet["state"], "ok")
        grouped = {"nepal": [{"title": "t", "summary": "s", "for_you": "w",
                              "category": "nepal", "source_url": "https://e.example/"}]}
        html_out = render_digest.render_sections(grouped, quiet)
        text_out = build_email.render_sections_text(grouped, quiet)
        email_out = build_email.render_sections_html(grouped, quiet)
        self.assertEqual(html_out.count('<section class="digest-section">'), 1)
        self.assertIn("IMPORTANT NEPAL", text_out)
        # And not one quiet status literal reached any surface.
        for literal in (render_digest.UNTRACKED_STATUS,
                        "Nothing new from this source today"):
            for out, label in ((html_out, "website"), (email_out, "email html"),
                               (text_out, "email text")):
                self.assertNotIn(literal, out,
                                 "%s rendered the quiet literal %r; a quiet "
                                 "empty section is omitted, not labelled"
                                 % (label, literal))

    def test_b5_status_survives_into_both_renderers(self):
        """The highest-value string: total DNS outage, zero items, still tells."""
        payload = validated_payload("degraded", hosts_down=TU_HOSTS, by_category={
            "education_tu": cat_entry(11, 9, 2, 0)}, stories=[])
        grouped = build_email.group_by_category(payload["stories"])
        expected = B5 % " or ".join(TU_URLS)
        self.assertIn(expected, render_digest.render_sections(grouped, payload["source_health"]))
        self.assertIn(expected, build_email.render_sections_html(grouped, payload["source_health"]))
        self.assertIn(expected, build_email.render_sections_text(grouped, payload["source_health"]))

    def test_status_lines_contain_no_card_markup(self):
        """parse_digest_categories counts <article class="card">; a status line
        must never inflate or deflate the ntfy teaser.

        `[^"]*` so the ALARM variant (`section-status-alarm`) is covered too -
        matching only the bare class would quietly narrow this check to the
        quiet lines. RE-POINTED: this fixture's only rendered status is the B5
        alarm, because the quiet sections are now omitted. The alarm is the one
        that must never carry markup, so this is the half worth keeping.
        """
        grouped = {"nepal": [{"title": "t", "summary": "s", "for_you": "w",
                              "category": "nepal", "source_url": "https://e.example/"}]}
        degraded = health("degraded", hosts_down=TU_HOSTS, by_category={
            "education_tu": cat_entry(11, 9, 2, 0)})
        out = render_digest.render_sections(grouped, degraded)
        found = re.findall(r'<p class="section-status[^"]*">(.*?)</p>', out)
        self.assertTrue(found, "no status lines matched; the selector is wrong")
        for line in found:
            self.assertNotIn("<article", line)
            self.assertNotIn("</", line)


# ---------------------------------------------------------------------------
# The COUNT contract: a section status is not a story.
#
# THE DEFECT THIS PINS
# --------------------
# A real run produced 4 stories. The email said `Stories: 4`; the deployed
# website said `6 stories`, and the archive index listed six categories of
# one. The page held four genuine cards plus two whose card TITLE was a
# section status string.
#
# The mechanism was not in the website renderer at all. build_email wrote each
# section's status line into the plain-text digest as a `* ` bullet - the same
# shape as a story title - and render_digest.parse_email_text(), which reads
# that text back, turned every one into a story. So an empty section invented
# a story, and the hero (len(stories)) and the teaser (a count of
# `<article class="card">`) inflated together.
#
# The previous review asked for exactly this interaction to be verified and it
# was not: GoldenRenderMixin rendered with `--email-txt <absent>`, so the text
# fallback was never round-tripped through the writer that feeds it.
# ---------------------------------------------------------------------------

#: Four stories in four categories. The other seven sections stay empty, which
#: is what produces the status lines the defect used to promote to stories.
COUNT_STORIES = [
    {"title": "Example City rain eases today", "summary": "Light drizzle.",
     "for_you": "Commute changed.", "source_name": "The Kathmandu Post",
     "source_url": "https://e.example/a", "reliability": "tier1",
     "category": "local_city", "priority": 5, "published_at": "2026-09-27",
     "supporting_sources": []},
    {"title": "Tribhuvan University reform reported", "summary": "Structural reform.",
     "for_you": "Check your faculty notices.", "source_name": "The Kathmandu Post",
     "source_url": "https://e.example/b", "reliability": "tier1",
     "category": "education_tu", "priority": 6, "published_at": "2026-09-27",
     "supporting_sources": []},
    {"title": "Heavy rains disrupt highways", "summary": "Road disruption.",
     "for_you": "Delay travel.", "source_name": "Khabarhub",
     "source_url": "https://e.example/c", "reliability": "tier2",
     "category": "nepal", "priority": 5, "published_at": "2026-09-27",
     "supporting_sources": []},
    {"title": "New graduate tech openings", "summary": "Six roles.",
     "for_you": "Worth a look.", "source_name": "Khabarhub",
     "source_url": "https://e.example/d", "reliability": "tier2",
     "category": "tech_career", "priority": 4, "published_at": "2026-09-27",
     "supporting_sources": []},
]

#: The status literals a reader must never mistake for a title. B2/B3/B4/B5 are
#: the complete output set of section_status_line().
COUNT_STATUS_LITERALS = (B2 % (2, 11), B3, B4, B5 % " or ".join(TU_URLS))


class TestSectionStatusIsNeverAStory(unittest.TestCase):
    """4 stories in, 4 on every surface - with empty sections still rendering.

    Nothing here writes to docs/; the whole round trip is a TEMP dir.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="render-count-")
        cls.payload = validated_payload(
            "degraded", stories=COUNT_STORIES, notes="TU board unreachable.",
            hosts_down=TU_HOSTS, by_category={"education_tu": cat_entry(11, 9, 2, 0)})
        cls.payload["stories"] = COUNT_STORIES
        cls.json_path = write_json(os.path.join(cls.tmp, "stories.validated.json"),
                                   cls.payload)
        cls.email_txt = os.path.join(cls.tmp, "email.txt")
        cls.email_html = os.path.join(cls.tmp, "email.html")
        build_email.main(["--in", cls.json_path, "--out-html", cls.email_html,
                          "--out-text", cls.email_txt,
                          "--template", os.path.join(
                              os.path.dirname(os.path.dirname(os.path.dirname(
                                  os.path.abspath(__file__)))),
                              "templates", "email.html")])
        with io.open(cls.email_txt, encoding="utf-8") as fh:
            cls.text = fh.read()
        cls.out_dir = os.path.join(cls.tmp, "digests")
        cls.index = os.path.join(cls.tmp, "index.html")
        cls.rc = render_digest.main([
            "--digest-time", "morning", "--date", "2026-09-27",
            "--email-txt", cls.email_txt, "--stories-json", cls.json_path,
            "--out-dir", cls.out_dir, "--index", cls.index])
        cls.page_path = os.path.join(cls.out_dir, "2026-09-27-morning.html")
        with io.open(cls.page_path, encoding="utf-8") as fh:
            cls.page = fh.read()
        with io.open(cls.index, encoding="utf-8") as fh:
            cls.index_html = fh.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- the count, on every surface ----------------------------------------

    def test_website_reports_four(self):
        """THE COUNT CONTRACT, re-pointed at the approved design's markup.

        The contract has never been "there is a `hero-stat` span" - it is "the
        page tells the reader how many stories it has, and that number is 4 and
        never the 6 the section-status defect used to produce". The approved
        mobile design deleted the four-stat hero row (date, time, story count,
        weather-note count) and moved the count into a pill in the top bar,
        because three of those four numbers now live in the title and sub-line
        and the fourth is the weather block's own business.

        So the assertion follows the number to its new home. It is the same
        contract, checked on the same page, with both halves kept: the true
        count must be present and the inflated one must be absent.
        """
        self.assertEqual(self.rc, 0)
        self.assertIn('<span class="topbar-count">4 stories</span>', self.page)
        self.assertNotIn("6 stories", self.page)

    def test_exactly_one_card_per_story(self):
        self.assertEqual(self.page.count('<article class="card">'), 4)

    def test_teaser_read_off_the_page_reports_four(self):
        counts = render_digest.parse_digest_categories(self.page_path)
        self.assertEqual(sum(counts.values()), 4, counts)
        self.assertEqual(sorted(counts), ["education", "local", "nepal", "tech"])

    def test_archive_index_agrees_with_the_hero(self):
        self.assertIn('<span class="digest-count">4 stories</span>', self.index_html)
        self.assertNotIn('<span class="digest-count">6 stories</span>',
                         self.index_html)

    def test_email_reports_four(self):
        with io.open(self.email_txt, encoding="utf-8") as fh:
            text = fh.read()
        with io.open(self.email_html, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("Stories: 4", text)
        self.assertNotIn("Stories: 6", text)
        self.assertNotIn("{{STORY_COUNT}}", html)
        self.assertIn("&middot; 4 item(s)", html)
        self.assertNotIn("&middot; 6 item(s)", html)

    def test_ntfy_push_reports_four_items(self):
        """The real heredoc, DRY RUN, offline. Its count comes from the same
        JSON, so this pins that the ntfy title never drifted from the page."""
        got = run_ntfy(self.tmp, self.payload)
        self.assertIn("4 items", got["title"])
        self.assertNotIn("6 item", got["title"])
        for story in COUNT_STORIES:
            self.assertIn(story["title"], got["message"])
        # The push renders no per-section status for an EMPTY section, so an
        # empty-section literal in the body would be a story that is not there.
        for literal in (B3, B4, B5 % " or ".join(TU_URLS)):
            self.assertNotIn(literal, got["message"])

    # -- a status string is never inside a card ----------------------------

    def test_no_status_literal_appears_inside_a_card(self):
        cards = re.findall(r'<article class="card">(.*?)</article>', self.page, re.S)
        self.assertEqual(len(cards), 4)
        for card in cards:
            for literal in COUNT_STATUS_LITERALS:
                self.assertNotIn(literal, card)

    def test_status_lines_are_not_card_titles(self):
        for title in re.findall(r'<h3 class="card-title">(.*?)</h3>', self.page):
            self.assertNotIn("Source status unknown", title)
            self.assertNotIn("Nothing new from this source", title)
            self.assertNotIn("sources for this section were unreachable", title)
            self.assertNotIn("Source unreachable", title)

    def test_a_status_line_is_never_absorbed_as_a_story_summary(self):
        for parsed in render_digest.parse_email_text(self.text):
            self.assertNotIn("status:", parsed["summary"])
            self.assertNotIn("Source status unknown", parsed["title"])

    # -- CONTRACT B, RE-POINTED: alarms are never hidden ------------------------

    def test_every_rendered_section_carries_exactly_one_status_line(self):
        """The count contract, restated for the new rule.

        RE-POINTED (owner-approved). This used to assert eleven sections and that
        the page carried exactly as many status lines as the text digest. The
        eleven is gone by request. What is asserted now is the invariant that
        still has teeth:

          * the page and the TEXT digest agree on how many status lines there
            are -- so the website and the email never describe the same run
            differently (the divergence this file exists to prevent);
          * a section that renders is never left without saying something;
          * the C literal is NOT on the page. This is the re-pointed form of the
            old `assertIn(UNTRACKED_STATUS, self.page)`: the constant still
            exists and `section_status` still returns it, but a quiet empty
            section is omitted rather than labelled.

        The B3 assertion below is UNCHANGED and still the important one: no
        false "may be incomplete" anywhere.
        """
        # `[^>]*` because a status element may carry ONE tone modifier class
        # (`section-status-alarm`), and matching only the bare class would
        # silently stop covering every alarm.
        page_statuses = len(re.findall(r'<p class="section-status[^"]*">', self.page))
        self.assertEqual(page_statuses, self.text.count("  status: "))
        self.assertGreater(page_statuses, 0,
                           "a degraded run must still say something about its "
                           "sources; zero status lines is silence")
        # Exactly the four sections this fixture justifies: the three holding a
        # card, plus the one holding none and raising an alarm. Nothing else may
        # appear, and nothing here may go missing.
        populated = {s.get("category") for s in (self.payload or {}).get("stories", [])}
        expected_keys = set(populated) | {"education_tu"}
        rendered_keys = set()
        for key, header, _color in render_digest.SECTIONS:
            if render_digest.esc(header) in self.page:
                rendered_keys.add(key)
        self.assertEqual(rendered_keys, expected_keys)
        self.assertEqual(self.page.count('<section class="digest-section">'),
                         len(expected_keys))
        # The cardless section kept its place BECAUSE it is the alarm: exactly
        # one status line, and it is the alarm variant.
        self.assertEqual(page_statuses, 1)
        self.assertEqual(
            self.page.count('<p class="section-status section-status-alarm">'), 1)
        self.assertNotIn(render_digest.UNTRACKED_STATUS, self.page)
        self.assertNotIn(B3, self.page)

    def test_b3_alarm_is_reserved_for_a_source_that_is_actually_tracked(self):
        """The other half of the B3a change: the alarm was not deleted, it was
        re-pointed at the case it was always meant for.

        A category the owner DOES track, whose sources were skipped, still
        gets B3 - nothing about a real tracking gap is softened.
        """
        skipped = health("partial", by_category={
            "gaming": cat_entry(5, 4, 0, 1, urls=[])})
        for mod in (render_digest, build_email):
            self.assertEqual(mod.section_status("gaming", 0, skipped),
                             (B3, mod.STATUS_ALARM), mod.__name__)
        # ...and an untrustworthy health object keeps it too, because then
        # untracked and unmeasured cannot be told apart.
        for bad in (None, {}, {"state": "unknown"},
                    {"state": "ok", "by_category": "not-a-dict"}):
            for mod in (render_digest, build_email):
                self.assertEqual(mod.section_status("anime", 0, bad),
                                 (B3, mod.STATUS_ALARM), (mod.__name__, bad))

    def test_the_text_digest_carries_every_section_that_survives(self):
        """The text digest keeps a keyed status line under each section it keeps.

        RE-POINTED (owner-approved). This used to assert all eleven `== HEADER ==`
        lines are present, which encoded the old "every section always renders"
        rule. It now asserts that every section the WEBSITE kept is also in the
        text -- the two surfaces must not disagree about which sections exist,
        because render_digest.parse_email_text() reads this text back to build
        the page when stories.validated.json is absent, so a section kept here
        for cosmetics would come back there.

        The `* ` marker is still absent from status lines, and every `* ` line
        left in the text is still a real story title -- that part is unchanged,
        and it is what stops an empty section inventing a story.
        """
        # The two modules spell their headers differently ON PURPOSE:
        # build_email.SECTIONS carries them already HTML-escaped for the email
        # ("LOCAL &amp; CITY"), render_digest.SECTIONS carries the raw form
        # and escapes at render time. So the text is checked against the email
        # header and the page against the page header -- escaping an
        # already-escaped header would search for "LOCAL &amp;amp; CITY".
        page_headers = dict((k, h) for k, h, _c in render_digest.SECTIONS)
        for key, header, _color in build_email.SECTIONS:
            plain = (header.replace("&amp;", "&").replace("&#8212;", "--")
                     .replace("&middot;", "-"))
            in_text = ("== %s ==" % plain) in self.text
            in_page = render_digest.esc(page_headers[key]) in self.page
            self.assertEqual(
                in_text, in_page,
                "%s is in the text digest but not the page (or the reverse): "
                "the two surfaces must agree on which sections exist" % plain)
        # One keyed status line per section that has something to say, and
        # every `* ` line left in the text is a real story title.
        self.assertGreater(self.text.count("  status: "), 0)
        titles = [ln[2:].strip() for ln in self.text.split("\n") if ln.startswith("* ")]
        self.assertEqual(len(titles), 4)

    # -- the text reader itself --------------------------------------------

    def test_four_stories_and_two_empty_sections_parse_to_four(self):
        """The literal shape from the run: 4 stories, 2 sections that carry
        nothing but a status line - one in the current keyed form, one in the
        pre-fix `* ` form. Both are statuses, so both are not stories."""
        text = "\n\n".join([
            "== LOCAL & CITY ==",
            "* Example City rain eases today",
            "  The Kathmandu Post | tier1 | 2026-09-27",
            "  Light drizzle.",
            "  Link: https://e.example/a",
            "",
            "== TU / BSC CSIT ==",
            "* Tribhuvan University reform reported",
            "  The Kathmandu Post | tier1 | 2026-09-27",
            "  Structural reform.",
            "  Link: https://e.example/b",
            "",
            "== IMPORTANT NEPAL ==",
            "* Heavy rains disrupt highways",
            "  Khabarhub | tier2 | 2026-09-27",
            "  Road disruption.",
            "  Link: https://e.example/c",
            "",
            "== GAMING ==",
            "",
            "* New graduate tech openings",
            "  Khabarhub | tier2 | 2026-09-27",
            "  Six roles.",
            "  Link: https://e.example/d",
            "",
            "== ANIME ==",
            "  status: %s" % B4,
            "",
            "== MANGA - MANHWA - MANHUA ==",
            "* %s" % B3,
        ])
        parsed = render_digest.parse_email_text(text)
        self.assertEqual(len(parsed), 4, [p["title"] for p in parsed])
        self.assertNotIn("anime", [p["category"] for p in parsed])
        self.assertNotIn("manga_manhwa_manhua", [p["category"] for p in parsed])

    def test_a_story_titled_like_a_status_line_is_still_a_story(self):
        """The skip must key on the status FORM, not on the words: a publisher
        headline that happens to contain one of these phrases stays a story.
        Over-matching prose would silently drop real news."""
        text = "\n".join([
            "== ANIME ==",
            "* Nothing new from this source today: season 2 delayed",
            "  Anime News | tier2 | 2026-09-27",
            "  A delay, reported.",
            "  Link: https://e.example/real",
        ])
        parsed = render_digest.parse_email_text(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["category"], "anime")


# ---------------------------------------------------------------------------
# Digest-level banner
# ---------------------------------------------------------------------------

class TestDigestBanner(unittest.TestCase):
    def test_one_banner_per_state_in_both_renderers(self):
        for state, expected in BANNER.items():
            self.assertEqual(render_digest.health_banner(health(state)), expected)
            self.assertEqual(build_email.notice_html(health(state)), build_email.notice_html(health(state)))
            self.assertIn(expected, build_email.notice_html(health(state)))
            self.assertIn(expected, build_email.notice_text(health(state)))

    def test_ok_renders_no_banner(self):
        self.assertEqual(render_digest.health_banner(health("ok")), "")
        self.assertEqual(build_email.notice_html(health("ok")), "")
        self.assertEqual(build_email.notice_text(health("ok")), "")

    def test_absent_source_health_is_unknown_not_ok(self):
        for empty in (None, {}, {"state": ""}, {"state": "banana"}, "junk", 7):
            self.assertEqual(render_digest.health_banner(empty),
                             BANNER["unknown"], "input=%r" % (empty,))
            self.assertEqual(render_digest.health_state(empty), "unknown")
            self.assertIn(BANNER["unknown"], build_email.notice_html(empty))

    def test_website_hero_carries_the_banner(self):
        """The banner was structurally impossible on the website before."""
        for state, expected in BANNER.items():
            page = render_digest.render_page([], "morning", "2026-09-27", "2026-09-27 06:31",
                                             validated_payload(state))
            self.assertIn(expected, page, "state=%s missing from hero" % state)

    def test_down_line_lists_critical_hosts(self):
        obj = health("degraded", hosts_down=["iost.tu.edu.np", "tu.edu.np"])
        self.assertEqual(render_digest.down_line(obj),
                         "Down: iost.tu.edu.np, tu.edu.np")
        self.assertIn("Down: iost.tu.edu.np, tu.edu.np", build_email.notice_html(obj))
        self.assertIn("Down: iost.tu.edu.np, tu.edu.np", build_email.notice_text(obj))

    def test_down_line_caps_at_three_then_counts(self):
        hosts = ["a.example", "b.example", "c.example", "d.example", "e.example"]
        obj = health("degraded", hosts_down=hosts)
        self.assertEqual(render_digest.down_line(obj),
                         "Down: a.example, b.example, c.example (+2 more)")

    def test_no_down_line_when_nothing_critical_is_down(self):
        self.assertEqual(render_digest.down_line(health("partial")), "")
        self.assertNotIn("Down:", build_email.notice_html(health("partial")))

    def test_legacy_limited_notice_is_deleted(self):
        """The research_status=="limited" notice is gone: one banner only.

        Asserted BEHAVIOURALLY (the legacy wording can never reach a reader for
        any state) plus by the removed identifiers. The source is NOT grepped
        for the words "Limited digest" because build_email.main keeps an
        intentional guard print that names them.
        """
        legacy = "some research sources were unavailable"
        for state in (None, "ok", "partial", "degraded", "unknown"):
            obj = None if state is None else health(state)
            self.assertNotIn(legacy, build_email.notice_html(obj))
            self.assertNotIn(legacy, build_email.notice_text(obj))
            page = render_digest.render_page([], "morning", "2026-09-27", "2026-09-27 06:31",
                                             validated_payload(state))
            self.assertNotIn(legacy, page)

        self.assertFalse(hasattr(build_email, "LIMITED_NOTICE_HTML"))
        self.assertFalse(hasattr(build_email, "LIMITED_NOTICE_TEXT"))
        with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "..", "scripts", "build_email.py"),
                     encoding="utf-8") as fh:
            body = fh.read()
        self.assertNotIn("LIMITED_NOTICE_HTML", body)
        self.assertNotIn("LIMITED_NOTICE_TEXT", body)
        self.assertNotIn("&#9888; Limited", body)  # the legacy notice markup
        # The guard that protects against its return must survive.
        self.assertIn("Limited digest", body)


# ---------------------------------------------------------------------------
# Contract C - the honest cutoff line
# ---------------------------------------------------------------------------

class TestCutoffLine(unittest.TestCase):
    def test_all_four_states_literal_in_both_renderers(self):
        cases = {"ok": CUTOFF_OK, "partial": CUTOFF_PARTIAL,
                 "degraded": CUTOFF_DEGRADED, "unknown": CUTOFF_UNKNOWN}
        for state, expected in cases.items():
            self.assertEqual(render_digest.cutoff_line(CUTOFF, health(state)), expected)
            self.assertEqual(build_email.cutoff_line(CUTOFF, health(state)), expected)

    def test_unknown_drops_the_timestamp_entirely(self):
        """Asserting 'checked through T' with nothing confirmed reachable is the
        exact lie this rebuild removes."""
        for fn in (render_digest.cutoff_line, build_email.cutoff_line):
            line = fn(CUTOFF, health("unknown"))
            self.assertEqual(line, CUTOFF_UNKNOWN)
            self.assertNotIn(CUTOFF, line)
            self.assertNotIn("NPT", line)
            self.assertNotIn("Information checked through", line)

    def test_unknown_line_appears_even_with_no_cutoff(self):
        self.assertEqual(render_digest.cutoff_line("", health("unknown")), CUTOFF_UNKNOWN)
        self.assertEqual(build_email.cutoff_line("", health("unknown")), CUTOFF_UNKNOWN)

    def test_ok_state_keeps_the_bare_timestamp(self):
        self.assertEqual(render_digest.cutoff_line(CUTOFF, health("ok")), CUTOFF_OK)
        self.assertNotIn("·", CUTOFF_OK)

    def test_no_cutoff_and_not_unknown_renders_nothing(self):
        for fn in (render_digest.cutoff_line, build_email.cutoff_line):
            self.assertEqual(fn("", health("ok")), "")
            self.assertEqual(fn("", None), CUTOFF_UNKNOWN)

    def test_late_suffix_preserved_for_ok_partial_degraded_only(self):
        for state in ("ok", "partial", "degraded"):
            for fn in (render_digest.cutoff_line, build_email.cutoff_line):
                line = fn(CUTOFF, health(state), scheduled="06:00", generated_at="2026-09-27 06:31")
                self.assertIn("(scheduled 06:00 · runner started 06:31)", line)
        for fn in (render_digest.cutoff_line, build_email.cutoff_line):
            self.assertNotIn("scheduled",
                             fn(CUTOFF, health("unknown"), scheduled="06:00",
                                generated_at="2026-09-27 06:31"))

    def test_website_hero_renders_the_state_cutoff(self):
        for state, expected in (("ok", CUTOFF_OK), ("partial", CUTOFF_PARTIAL),
                                ("degraded", CUTOFF_DEGRADED), ("unknown", CUTOFF_UNKNOWN)):
            page = render_digest.render_page([], "morning", "2026-09-27", "2026-09-27 06:31",
                                             validated_payload(state))
            self.assertIn(expected, page, "state=%s cutoff missing" % state)


# ---------------------------------------------------------------------------
# Research notes (populated by the agent, previously rendered nowhere)
# ---------------------------------------------------------------------------

class TestResearchNote(unittest.TestCase):
    def test_note_renders_with_the_prefix(self):
        for fn in (render_digest.capped_note, build_email.capped_note):
            self.assertEqual(fn("TU board was down"), NOTE_PREFIX + "TU board was down")

    def test_empty_and_whitespace_notes_render_nothing(self):
        for fn in (render_digest.capped_note, build_email.capped_note):
            for empty in (None, "", "   ", "\n\t "):
                self.assertEqual(fn(empty), "")

    def test_note_is_capped_at_240_chars(self):
        for fn in (render_digest.capped_note, build_email.capped_note):
            line = fn("x" * 5000)
            body = line[len(NOTE_PREFIX):]
            self.assertEqual(len(body), 240)
            self.assertTrue(line.endswith("…"))

    def test_note_appears_in_website_email_and_ntfy(self):
        note = "iost.tu.edu.np and tu.edu.np both SERVFAIL this morning."
        payload = validated_payload("degraded", notes=note, hosts_down=TU_HOSTS)
        page = render_digest.render_page(payload["stories"], "morning", "2026-09-27",
                                         "2026-09-27 06:31", payload)
        # RE-APPROVED 2026-10-18: on the www page the note is folded behind
        # `<summary>Research note</summary>`, so its body must NOT restate the
        # label it is sitting under -- a thumb-width apart on a 390px phone that
        # was the owner's duplicate. The note's SUBSTANCE is asserted unchanged;
        # only the label moved, into the summary. The email and the push keep the
        # prefix (neither has an adjacent label), which is why the assertions
        # below are untouched.
        self.assertIn(">Research note</summary>", page)
        self.assertIn('<p class="health-note">%s</p>' % note, page)
        self.assertNotIn(NOTE_PREFIX, page)

        tmp = tempfile.mkdtemp()
        try:
            src = write_json(os.path.join(tmp, "in.json"), payload)
            out_html = os.path.join(tmp, "e.html")
            out_txt = os.path.join(tmp, "e.txt")
            rc = build_email.main(["--in", src, "--out-html", out_html,
                                   "--out-text", out_txt,
                                   "--template", os.path.join(
                                       os.path.dirname(os.path.abspath(__file__)),
                                       "..", "..", "templates", "email.html")])
            self.assertEqual(rc, 0)
            with io.open(out_txt, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn(NOTE_PREFIX + note, text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_feed_source_id_never_reaches_a_reader_surface(self):
        """A `feed:<slug>` source ID is the same class as `Collector:`.

        It arrives as `notice:feed:<slug>`, and stripping `notice:` left the
        `feed:<slug>` token behind -- on the page, in the email AND in the push.
        A slug is build vocabulary, not a source the reader can visit; unlike a
        hostname, it is dropped rather than re-worded.
        """
        raw = "notice:feed:gaming-pcgamer failed with TimeoutError"
        for mod in (render_digest, build_email):
            self.assertEqual("failed with a timeout", mod.reader_note_text(raw))
            self.assertNotIn("feed:", mod.capped_note(raw))
            self.assertNotIn("notice:", mod.capped_note(raw))

        payload = validated_payload("degraded", notes=raw, hosts_down=TU_HOSTS)
        page = render_digest.render_page(payload["stories"], "morning", "2026-09-27",
                                         "2026-09-27 06:31", payload)
        self.assertNotIn("feed:", page)
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertNotIn("feed:", got["message"],
                         "the push is a reader surface too")

    def test_ntfy_body_leads_with_the_banner_and_stays_under_4096(self):
        """Hard gate: the honesty signal must never be the thing truncated."""
        payload = twenty_story_payload("degraded", hosts_down=TU_HOSTS,
                                       notes="TU notice board unreachable (DNS SERVFAIL).")
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        message = got["message"]
        self.assertLessEqual(len(message.encode("utf-8")), MAX_BYTES,
                             "ntfy body is %d bytes (limit %d)"
                             % (len(message.encode("utf-8")), MAX_BYTES))
        self.assertTrue(message.startswith(BANNER["degraded"]),
                        "banner must lead the ntfy body, got: %r" % message[:120])
        self.assertIn("Down: iost.tu.edu.np, tu.edu.np", message)
        self.assertIn(NOTE_PREFIX, message)

    def test_ntfy_reserves_priority_five_for_a_critical_host_down(self):
        for state, hosts, expect_five in (("degraded", TU_HOSTS, True),
                                          ("degraded", [], False),
                                          ("partial", [], False),
                                          ("unknown", [], False),
                                          ("ok", [], False)):
            tmp = tempfile.mkdtemp()
            try:
                got = run_ntfy(tmp, twenty_story_payload(state, hosts_down=hosts))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            self.assertEqual(got["priority"] == 5, expect_five,
                             "state=%s hosts=%r priority=%r" % (state, hosts, got["priority"]))
            self.assertEqual("rotting_light" in got["tags"], expect_five)

    def test_ntfy_keeps_banner_when_body_must_be_truncated(self):
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, twenty_story_payload("degraded", hosts_down=TU_HOSTS))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertLessEqual(len(got["message"].encode("utf-8")), MAX_BYTES)
        self.assertIn(BANNER["degraded"], got["message"])

    def test_ntfy_banner_when_there_are_no_stories_at_all(self):
        """CHANGED (e9d4115): the push states this in its OWN words now.

        The literal this used to assert -- "No items cleared filtering today."
        -- was the website/email sentence, and the push heredoc stopped sharing
        it when it gained a recap path and a degraded path of its own. This
        fixture has no recap material, so it takes the NO-RECAP branch, and that
        branch is the loud one: it names itself a delivery failure rather than a
        quiet news day. That is a stronger honesty signal than the sentence it
        replaced, so the assertion follows the signal instead of the wording.

        The recap branch is not lost coverage -- it is pinned, with its own
        fixtures, by tests/digest/test_empty_digest_recap.py.
        """
        payload = validated_payload("degraded", hosts_down=TU_HOSTS, stories=[])
        tmp = tempfile.mkdtemp()
        try:
            got = run_ntfy(tmp, payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn(BANNER["degraded"], got["message"])
        self.assertIn(
            "nothing new cleared filtering for this slot", got["message"],
            "a zero-story push must still say so, or the owner reads an empty "
            "notification as nothing happening rather than as a delivery "
            "failure")
        self.assertIn(
            "delivery failure notice", got["message"],
            "with no recap material to fall back on, the push must name itself "
            "a delivery failure rather than a quiet news day")


class TestRenderFixtureIsolation(unittest.TestCase):
    """A test may not read the runtime `state/` directory. THIS IS THE REGRESSION.

    The bug this class exists to make unrepeatable: `tests/render/fixtures.py`
    read its fixture from `state/mock_stories.json`. `state/` is a RUNTIME
    directory -- every real digest run rewrites it, `.gitignore` ignores it, and
    a clean CI checkout does not contain it. So the render suite was green on the
    owner's laptop (a stale copy happened to be lying around in `state/`) and
    RED on every GitHub Actions run with
    `FileNotFoundError: .../state/mock_stories.json`.

    A skipped test, a `continue-on-error`, or a "tolerate the missing file"
    loader would all have hidden the dependency instead of removing it, and the
    suite would have gone green while still depending on a file no workflow
    guarantees. So the dependency is deleted, and this class is the guard that
    says so out loud on the next attempt.

    WHAT IS FLAGGED. Any *read* of a path under `<repo>/state/`, spelled as a
    repo-root join (`os.path.join(REPO_ROOT, "state", ...)`, or a literal
    `"state/..."` in a test). Writes to a `state/` inside a `tempfile.mkdtemp()`
    sandbox are legitimate and untouched -- a test that chdirs into a temp dir
    and runs a real script writes exactly what production writes, and that is
    the point of those tests. What is banned is depending on the repository's
    own mutable `state/`, which is a *host* fact, not a test input.
    """

    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    TESTS_DIR = os.path.join(REPO_ROOT, "tests")

    #: Matches a repo-root-anchored read of the runtime state directory.
    #: Deliberately narrow: it needs an anchor to REPO_ROOT (or a relative
    #: "state/..." literal), so prose in a comment that merely mentions
    #: state/candidates.json as documentation of production behaviour is not
    #: mistaken for a dependency.
    PATTERNS = (
        re.compile(r"""join\(\s*REPO_ROOT[A-Z_]*\s*,\s*['"]state['"]"""),
        re.compile(r"""join\(\s*REPOPOOT[A-Z_]*\s*,\s*['"]state['"]"""),
        re.compile(r"""['"][.]{0,2}/state/"""),
    )

    #: Files allowed to NAME the runtime `state/` directory without reading it.
    #: Each entry carries its reason, because a blanket exemption is how this
    #: class of bug comes back wearing a different name.
    #:
    #:   test_render_honesty.py  -- this guard itself: it quotes the banned
    #:                             pattern in its docstring and in PATTERNS.
    #:   ci_local.py             -- the local CI runner. It RENAMES `state/`
    #:                             aside to simulate a clean checkout and
    #:                             restores it; it never opens a file inside it.
    ALLOWED = {
        "test_render_honesty.py": "this guard quotes the pattern it bans",
        "ci_local.py": "moves state/ aside and restores it; reads nothing in it",
    }

    @staticmethod
    def _python_files():
        for dirpath, dirs, files in os.walk(TestRenderFixtureIsolation.TESTS_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)

    def _offenders(self):
        found = []
        for path in self._python_files():
            if os.path.basename(path) in self.ALLOWED:
                continue
            with io.open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    for pattern in self.PATTERNS:
                        if pattern.search(line):
                            rel = os.path.relpath(path, self.REPO_ROOT)
                            found.append("%s:%d: %s"
                                         % (rel, lineno, line.strip()))
                            break
        return found

    def test_no_test_reads_a_file_under_the_runtime_state_directory(self):
        offenders = self._offenders()
        self.assertEqual(
            [], offenders,
            "a test under tests/ reads a path inside the runtime `state/` "
            "directory, so its result depends on whether a digest run has run "
            "yet. That is the bug that made the render suite pass locally and "
            "fail on every CI run:\n  %s\n"
            "Commit the fixture under tests/ (tests/render/data/ for the render "
            "suite) and point the test there."
            % "\n  ".join(offenders))

    def _repo_root_workspace_offenders(self):
        """Tests that hand a workflow step the REPOSITORY as its workspace.

        THE THIRD WAY IN, and the two PATTERNS above cannot see it. `PATTERNS`
        needs the `state/` path spelled out -- a `join(REPO_ROOT, "state")` or a
        relative `"state/..."` literal. `GITHUB_WORKSPACE` needs neither: the
        step reads committed paths from the workspace, so binding the workspace
        to the repository makes it read the repository's own
        `state/digest_slot_ledger.json` with no `state/` string anywhere in
        sight. That is exactly how four frozen 2026-10-04/05 watchdog fixtures
        went red when commit 850c763 created that file, and why this guard --
        written after the original bug and confident it had closed the class --
        did not catch it.

        TWO STEPS, because the assignment and the use are on different lines and
        the offending value is a bare local name: first collect names BOUND to a
        repo root, then flag any `GITHUB_WORKSPACE` line that uses one.

        WHAT IT DELIBERATELY ALLOWS, since a guard that cannot be worked with
        gets deleted:
          - `"GITHUB_WORKSPACE": str(self.tmp)` -- a temp dir. The shape
            tests/digest/test_slot_ledger_mirror.py already uses, and the shape
            every step-under-test harness in this repo should use.
          - an assertion that the workspace is NOT the repo, which mentions
            REPO_ROOT and GITHUB_WORKSPACE on adjacent lines while doing the
            exact opposite of binding them.

        BOTH SPELLINGS ARE CAUGHT, because the defect arrived twice: the name
        bound first and used several lines later (`workspace = str(W.REPO_ROOT)`
        ... `"GITHUB_WORKSPACE": workspace`), and inline
        (`"GITHUB_WORKSPACE": str(W.REPO_ROOT)`). The first version of this
        guard handled only the two-step shape and was shown RED against the
        inline one, which is the only honest way to find out.
        """
        found = []
        for path in self._python_files():
            if os.path.basename(path) in self.ALLOWED:
                continue
            with io.open(path, encoding="utf-8") as fh:
                lines = self._code_lines(fh.read().splitlines())
            # step 1: names BOUND to a repo root
            bound = {}
            for lineno, line in lines:
                if "REPO_ROOT" not in line:
                    continue
                for name in re.findall(r"(\w+)\s*=(?!=)", line):
                    bound.setdefault(name, lineno)
            # step 2: GITHUB_WORKSPACE lines, inline or via a bound name
            for lineno, line in lines:
                if "GITHUB_WORKSPACE" not in line:
                    continue
                if "REPO_ROOT" in line:
                    found.append(
                        "%s:%d: GITHUB_WORKSPACE is given the repository root on "
                        "this very line. A step handed the repository as its "
                        "workspace reads the repository's own committed state/ "
                        "files, so the verdict depends on whatever the last run "
                        "committed. Point it at a tempfile.mkdtemp() workspace "
                        "and write the fixtures there."
                        % (os.path.relpath(path, self.REPO_ROOT), lineno))
                    continue
                tail = line.split("GITHUB_WORKSPACE", 1)[1]
                for name in re.findall(r"\b(\w+)\b", tail):
                    if name in bound:
                        found.append(
                            "%s:%d: GITHUB_WORKSPACE is given %r, which is bound "
                            "to REPO_ROOT on line %d. A step handed the "
                            "repository as its workspace reads the repository's "
                            "own committed state/ files, so the verdict depends "
                            "on whatever the last run committed. Point it at a "
                            "tempfile.mkdtemp() workspace and write the fixtures "
                            "there."
                            % (os.path.relpath(path, self.REPO_ROOT), lineno,
                               name, bound[name]))
                        break
        return found

    @staticmethod
    def _code_lines(lines):
        """Drop comments and docstring bodies, keeping `(lineno, line)`.

        Without this the guard reads its own prose: these tests explain at
        length exactly what a `GITHUB_WORKSPACE` binding looks like, and a
        guard that trips over its own documentation is a guard nobody keeps.
        A run of three double quotes toggles docstring state, which is
        sufficient for this codebase -- the files being scanned are plain test
        modules.
        """
        out = []
        in_doc = False
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            quote = stripped.count('"""')
            if in_doc:
                if quote:
                    in_doc = False
                continue
            if quote == 1:
                in_doc = True
                continue
            if quote >= 2:
                continue
            out.append((lineno, line))
        return out

    def test_no_test_hands_a_step_the_repository_as_its_workspace(self):
        offenders = self._repo_root_workspace_offenders()
        self.assertEqual(
            [], offenders,
            "a test under tests/ binds GITHUB_WORKSPACE to the repository root, "
            "so the step under test reads the repository's own runtime `state/` "
            "mirror rather than a fixture. That is the third spelling of the bug "
            "the guard above was written for, and it is how the digest watchdog "
            "slot tests began failing on data they never authored:\n  %s\n"
            "Copy the committed files the step genuinely needs into a "
            "tempfile.mkdtemp() workspace and pin the rest."
            % "\n  ".join(offenders))

    def test_the_render_fixture_is_committed_test_owned_data_not_state(self):
        """The specific regression, pinned on the path itself.

        A test that reads a committed copy is the correct shape; this asserts
        the copy exists where the tests own it, and that it is NOT the file in
        `state/` that the guard above would have caught.
        """
        from fixtures import MOCK_STORIES
        self.assertTrue(os.path.isfile(MOCK_STORIES),
                        "render fixture is missing: %s" % MOCK_STORIES)
        rel = os.path.relpath(MOCK_STORIES, self.REPO_ROOT).replace("\\", "/")
        self.assertTrue(
            rel.startswith("tests/"),
            "the render fixture must live under tests/, not in the runtime "
            "state directory: %s" % rel)

    def test_the_render_suite_runs_with_the_runtime_state_directory_absent(self):
        """The proof CI could not previously give: no `state/` at all.

        `state/` is moved aside (never deleted) for the duration, the render
        suite is re-run in-process, and the directory is restored in a
        `finally`. On the old code this raised FileNotFoundError from
        `load_mock_stories`; now it must pass, which is what makes "the suite is
        green from a clean checkout" a checked fact rather than a hope.
        """
        import unittest as _unittest
        state_dir = os.path.join(self.REPO_ROOT, "state")
        if not os.path.isdir(state_dir):
            self.skipTest("no state/ to move aside; the suite is trivially clean")

        aside = tempfile.mkdtemp(prefix="state-moved-aside-")
        moved = os.path.join(aside, "state")
        os.rename(state_dir, moved)
        try:
            # Fresh import graph inside this interpreter: fixtures.MOCK_STORIES
            # is resolved at import time, so a cached module would hide a stale
            # path.
            here = os.path.dirname(os.path.abspath(__file__))
            for mod in [m for m in list(sys.modules)
                        if m in ("fixtures", "tests.render.test_render_personal",
                                 "tests.render.test_render_readability")]:
                del sys.modules[mod]
            if here not in sys.path:
                sys.path.insert(0, here)
            import fixtures as _fixtures_mod
            self.assertTrue(
                os.path.abspath(_fixtures_mod.MOCK_STORIES).startswith(
                    os.path.join(self.REPO_ROOT, "tests")),
                "MOCK_STORIES still points at %s" % _fixtures_mod.MOCK_STORIES)
            self.assertTrue(
                os.path.isfile(_fixtures_mod.MOCK_STORIES),
                "render fixture unreadable with state/ absent: %s"
                % _fixtures_mod.MOCK_STORIES)
            # The two SIBLING modules, not this one. Running this module from
            # inside itself would recurse through this very test forever, and
            # the dependency under test is `fixtures.load_mock_stories`, which
            # the siblings exercise on every single test case.
            loader = _unittest.TestLoader()
            suite = _unittest.TestSuite(
                loader.loadTestsFromNames(
                    ["tests.render.test_render_personal",
                     "tests.render.test_render_readability"]))
            result = _unittest.TextTestRunner(
                stream=io.StringIO(), verbosity=0).run(suite)
            self.assertEqual([], list(result.errors),
                             "render suite errors with state/ absent:\n%s"
                             % "\n".join(
                                 "%s\n%s" % (t, e) for t, e in result.errors[:5]))
            self.assertEqual([], list(result.failures),
                             "render suite failures with state/ absent:\n%s"
                             % "\n".join(
                                 "%s\n%s" % (t, e) for t, e in result.failures[:5]))
            self.assertGreater(result.testsRun, 100,
                               "only %d sibling render tests ran; the check is "
                               "not proving anything" % result.testsRun)
        finally:
            os.rename(moved, state_dir)
            shutil.rmtree(aside, ignore_errors=True)
            self.assertTrue(os.path.isdir(state_dir),
                            "state/ was not restored")


if __name__ == "__main__":
    unittest.main()
