#!/usr/bin/env python3
"""tests/validate/test_for_you.py — contract tests for the `for_you` field.

Run:
    python -m unittest discover -s tests/validate -t . -v

Offline and clock-free. The screening config is injected as a literal so the
suite cannot drift when config/profile.yml changes (config/ is owned by
another agent); the values mirror the profile's `for_you:` block.
"""

import os
import re
import unittest

from . import REPO_ROOT, SCRIPTS  # noqa: F401  (SCRIPTS is put on sys.path there)

import validate_stories as vs  # noqa: E402


# Mirrors config/profile.yml `for_you:` — plus the two word lists the profile
# is contracted to carry. A value that lives in both places is a pair that
# drifts, so these are injected here and never read from disk by the tests.
GOOD_CFG = {
    "enabled": True,
    "min_length": 20,
    "max_length": 140,
    "min_personal_relevance": 7,
    "max_stories": 8,
    "categories": ["action", "local_city", "education_tu", "college_local",
                   "bank_local", "tech_career"],
    "reliability_tiers": ["tier1", "tier2"],
    "banned_openers": ["this is important", "it may be worth",
                       "the user should", "users can", "this means"],
    "hedge_markers": ["may", "might", "could", "possibly", "potentially",
                      "reportedly", "allegedly", "rumour", "rumor",
                      "suggests that", "signals", "indicates that",
                      "is expected to", "appears to", "seems to"],
}

PROMPT = os.path.join(REPO_ROOT, "prompts", "daily-digest.md")


def story(**over):
    """A qualifying education_tu/tier1 story, overridable per test."""
    base = {
        "title": "TU BSc CSIT exam form opens for semester 4",
        "summary": ("Confirmed: Tribhuvan University has opened the exam form "
                    "for BSc CSIT semester 4 at Example College Example City."),
        "category": "education_tu",
        "reliability": "tier1",
        "priority": 9,
        "personal_relevance": 9,
        "deadline": "2025-09-12",
        "effective_date": None,
        "event_date": None,
        "source_name": "Example",
        "source_url": "https://example.com/a",
        "spoiler_level": "none",
        "spoiler_reason": "Not a spoiler.",
        "relevance_reason": "Direct exam notice for the reader's program.",
        "language": "en",
    }
    base.update(over)
    return base


GOOD_LINE = "Form closes 12 Sep - that is your last day to apply."


def assert_rejected(case, line, prefix, **over):
    """The line must be DELETED (never shortened, never replaced)."""
    s = story(for_you=line, **over)
    reason = vs.screen_for_you(s, GOOD_CFG)
    case.assertIsNotNone(reason, "line survived screening: %r" % line)
    if prefix is not None:
        case.assertTrue(reason.startswith(prefix),
                        "reason %r does not start with %r" % (reason, prefix))
    stats, _log = vs.apply_for_you_screening([s], GOOD_CFG)
    case.assertNotIn("for_you", s)
    case.assertEqual(stats["stripped"], 1)


def assert_accepted(case, line, **over):
    """An honest line must survive untouched."""
    s = story(for_you=line, **over)
    case.assertIsNone(vs.screen_for_you(s, GOOD_CFG),
                      "honest line was rejected: %r" % line)
    vs.apply_for_you_screening([s], GOOD_CFG)
    case.assertEqual(s.get("for_you"), line)


class ForYouTestCase(unittest.TestCase):
    """Shared delete-only assertions."""

    def assert_rejected(self, line, prefix, **over):
        assert_rejected(self, line, prefix, **over)

    def assert_accepted(self, line, **over):
        assert_accepted(self, line, **over)


class ScreenChecksTest(ForYouTestCase):
    """Each of the six screens deletes a purpose-built offender."""

    # -- 1. length ----------------------------------------------------------
    def test_too_short_is_rejected(self):
        self.assert_rejected("Applies by 12 Sep", "too_short")

    def test_too_long_is_rejected(self):
        long_line = "Register for the exam form by 12 September 2025 online " \
                    "at the Example College Example City office or through the TU " \
                    "portal with your registration number printed"
        self.assertGreater(len(long_line), GOOD_CFG["max_length"])
        self.assert_rejected(long_line, "too_long")

    def test_byte_cap_is_hard_and_character_agnostic(self):
        """A 120-char line of 2-byte chars is 240 bytes and is still deleted."""
        # 120 characters (inside the 140 char band) but way over 150 bytes.
        heavy = "Regíster by 12 Sep — " + "é" * 105
        self.assertLessEqual(len(heavy), GOOD_CFG["max_length"])
        self.assertGreater(len(heavy.encode("utf-8")), vs.FOR_YOU_BYTE_CAP)
        self.assert_rejected(heavy, "too_long_bytes")

    def test_length_20_chars_is_the_floor(self):
        exact = "Register now: 12 Sep"  # 20 chars exactly
        self.assertEqual(len(exact), GOOD_CFG["min_length"])
        self.assert_accepted(exact)

    # -- 2. banned openers --------------------------------------------------
    def test_banned_opener_is_rejected(self):
        self.assert_rejected(
            "This is important: the exam form closes 12 Sep, apply now",
            "banned_opener")

    def test_banned_opener_is_case_insensitive_and_bare(self):
        self.assert_rejected(
            "IT MAY BE WORTH checking: form closes 12 Sep, apply today",
            "banned_opener")

    # -- 3. concreteness ----------------------------------------------------
    def test_no_concrete_token_is_rejected(self):
        self.assert_rejected(
            "Your semester form is worth sorting out at some point soon",
            "not_concrete")

    def test_imperative_alone_satisfies_concreteness(self):
        self.assert_accepted("Register for the exam form on the TU portal")

    def test_am_pm_satisfies_concreteness(self):
        self.assert_accepted(
            "Collect the form at Example College from 10 am to 4 pm",
            summary=("Confirmed: Example College Example City collects the BSc CSIT form "
                     "from 10 am to 4 pm and the TU exam form is open."))

    # -- 4. hedges ----------------------------------------------------------
    def test_hedge_marker_is_rejected(self):
        self.assert_rejected(
            "TU may publish a new exam date on 12 Sep - check the notice",
            "hedge_marker")
    def test_hedged_real_offender_from_the_owner_email_is_rejected(self):
        """The exact line that started this work must never render.

        It fails several screens at once (no concrete token AND a hedge); the
        screen that fires first is not the point, deletion is.
        """
        self.assert_rejected("may signal further TU schedule changes", None)

    def test_a_hedge_alongside_a_real_deadline_is_rejected(self):
        """A grounded numeral does not buy a pass for the hedge beside it."""
        self.assert_rejected(
            "New exam schedule signals a change from 12 Sep - check now",
            "hedge_marker")

    def test_month_name_may_is_not_a_hedge(self):
        """'May' is a hedge marker AND a month; a real deadline must survive."""
        s = story(for_you="Form closes May 12 - that is the last day to apply",
                  deadline="2026-05-12",
                  summary=("Confirmed: the TU exam form for BSc CSIT "
                           "semester 4 closes on May 12."))
        self.assertIsNone(vs.screen_for_you(s, GOOD_CFG),
                          "month name wrongly read as a hedge")

    # -- 5. third-person self-reference ------------------------------------
    def test_third_person_self_reference_is_rejected(self):
        self.assert_rejected(
            "User is 4th semester BSc CSIT - check the 12 Sep form deadline",
            "third_person_self_reference")

    def test_the_user_form_is_also_rejected(self):
        # "the user should" is ALSO a banned opener, so this line trips two
        # screens; deletion is the contract, not which screen fired.
        self.assert_rejected(
            "The user should submit the 12 Sep form to the Example College office", None)

    # -- 6. summary restatement --------------------------------------------
    def test_summary_restatement_is_rejected(self):
        s = story(
            summary=("Confirmed: the exam form for the fourth semester "
                     "closes on 12 September."),
            for_you=("The exam form for the fourth semester closes on 12 "
                     "September, apply today."))
        reason = vs.screen_for_you(s, GOOD_CFG)
        self.assertTrue(reason.startswith("summary_restatement"),
                        "restatement survived: %r" % reason)
        vs.apply_for_you_screening([s], GOOD_CFG)
        self.assertNotIn("for_you", s)

    def test_restatement_guard_is_length_sensitive_and_says_so(self):
        """A short line against a long summary cannot reach 0.6 Jaccard.

        Pinned on purpose: it is a real limitation of a Jaccard threshold over
        unequal-length texts, and a future edit that "fixes" the detector by
        changing the measure must confront this first.
        """
        s = story(summary="Confirmed: " + ("a very long and detailed body "
                                            "of reporting " * 12),
                  for_you="Exam form closes 12 Sep, apply today before 5 pm")
        self.assertLess(vs.jaccard(vs.content_words(s["for_you"]),
                                   vs.content_words(s["summary"])), 0.6)


class GroundingGateTest(ForYouTestCase):
    """Every numeral in the line must exist in the story's own fact fields."""

    def test_ungrounded_number_is_rejected(self):
        self.assert_rejected(
            "Form closes 30 Sep - that is your last day to apply",
            "ungrounded_number")

    def test_grounded_number_is_accepted(self):
        self.assert_accepted(GOOD_LINE)

    def test_grounding_covers_every_fact_field(self):
        for field, value in (("deadline", "2025-09-12"),
                             ("effective_date", "2025-09-12"),
                             ("event_date", "2025-09-12"),
                             ("title", "... on 2025-09-12"),
                             ("summary", "... on 2025-09-12")):
            s = story(for_you="The form closes 2025-09-12, apply that day")
            s[field] = value
            for other in vs.FOR_YOU_GROUNDING_FIELDS:
                if other != field:
                    s[other] = None
            self.assertIsNone(vs.screen_for_you(s, GOOD_CFG),
                              "%s was not accepted as grounding" % field)

    def test_number_grounding_is_boundary_aware(self):
        """'500' must not be grounded by '1500' somewhere in the summary."""
        s = story(summary=("Reported: 1500 students sat the exam last year "
                           "and the pass rate was unchanged."),
                  for_you="Pay the Rs. 500 lab fee by 12 Sep at the counter")
        reason = vs.screen_for_you(s, GOOD_CFG)
        self.assertEqual(reason, "ungrounded_number: '500'")

    def test_number_token_extraction_shape(self):
        self.assertEqual(
            vs.for_you_number_tokens("12 Sep 2025-09-12 at 9:30, 50% of 2.5%"),
            ["12", "2025-09-12", "9:30", "50%", "2.5%"])

    def test_line_without_numbers_passes_the_gate(self):
        self.assert_accepted("Register for the exam form on the TU portal")

    def test_gate_is_not_complete_and_says_so_in_the_source(self):
        """The known residual hole is documented, not papered over.

        A line can be perfectly token-grounded and still semantically wrong.
        No deterministic check catches that; the source must keep saying so.
        """
        s = story(title="TU board meets on 12 Sep to discuss the exam form",
                  summary="Reported: the university board meets on 12 Sep.",
                  for_you="Form closes 12 Sep - that is your last day to apply",
                  deadline=None)
        self.assertIsNone(vs.screen_for_you(s, GOOD_CFG),
                          "grounding gate changed behaviour; re-read the doc")
        with open(vs.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("It is NOT complete", source)


class EligibilityTest(unittest.TestCase):
    """A line is only written where a personal imperative is defensible."""

    def test_low_personal_relevance_is_stripped(self):
        s = story(for_you=GOOD_LINE, personal_relevance=6)
        self.assertTrue(vs.screen_for_you(s, GOOD_CFG)
                        .startswith("ineligible_personal_relevance"))
        vs.apply_for_you_screening([s], GOOD_CFG)
        self.assertNotIn("for_you", s)

    def test_tier3_story_loses_the_line_but_keeps_its_summary(self):
        s = story(for_you=GOOD_LINE, reliability="tier3")
        vs.apply_for_you_screening([s], GOOD_CFG)
        self.assertNotIn("for_you", s)
        self.assertEqual(s["summary"], story()["summary"])
        self.assertEqual(s["title"], story()["title"])

    def test_tier4_story_loses_the_line(self):
        s = story(for_you=GOOD_LINE, reliability="tier4")
        self.assertTrue(vs.screen_for_you(s, GOOD_CFG)
                        .startswith("ineligible_reliability"))

    def test_category_outside_the_list_loses_the_line(self):
        s = story(for_you="Watch the episode release on 12 Sep, it is tracked",
                  category="gaming")
        self.assertTrue(vs.screen_for_you(s, GOOD_CFG)
                        .startswith("ineligible_category"))

    def test_no_new_category_was_added(self):
        """for_you is a FIELD. VALID_CATEGORIES stays closed and unchanged."""
        self.assertEqual(vs.VALID_CATEGORIES, {
            "action", "world_major", "local_city", "nepal", "education_tu",
            "college_local", "bank_local", "gaming", "anime",
            "manga_manhwa_manhua", "tech_career",
        })
        self.assertNotIn("for_you", vs.VALID_CATEGORIES)


class RankCapTest(unittest.TestCase):
    """Past for_you.max_stories the FIELD goes, never the story."""

    def _many(self, n):
        out = []
        for i in range(n):
            out.append(story(
                title="Exam notice %s" % chr(ord("a") + i),
                for_you="Form closes 12 Sep - that is the last day to apply",
                personal_relevance=9, priority=10 - i,
                source_url="https://example.com/%d" % i))
        return out

    def test_cap_strips_the_field_past_max_stories(self):
        stories = self._many(10)
        cap = GOOD_CFG["max_stories"]
        stats, _log = vs.apply_for_you_screening(stories, GOOD_CFG)
        self.assertEqual(stats["kept"], cap)
        self.assertEqual(stats["capped"], 10 - cap)
        self.assertEqual(len(stories), 10, "stories must never be dropped")
        kept = [s for s in stories if "for_you" in s]
        self.assertEqual(len(kept), cap)
        # The survivors are the highest (personal_relevance, priority).
        self.assertEqual([s["title"] for s in kept],
                         ["Exam notice %s" % chr(ord("a") + i)
                          for i in range(cap)])

    def test_cap_respects_a_zero_cap(self):
        stories = self._many(3)
        cfg = dict(GOOD_CFG, max_stories=0)
        vs.apply_for_you_screening(stories, cfg)
        self.assertEqual([s for s in stories if "for_you" in s], [])


class ConfigFailClosedTest(unittest.TestCase):
    """An unusable screening config must silence the field, not open it."""

    def test_missing_config_strips_everything(self):
        s = story(for_you=GOOD_LINE)
        reason = vs.screen_for_you(s, {})
        self.assertTrue(reason.startswith("config_incomplete"))
        vs.apply_for_you_screening([s], {})
        self.assertNotIn("for_you", s)

    def test_missing_word_lists_are_reported(self):
        cfg = dict(GOOD_CFG)
        del cfg["hedge_markers"]
        problems = vs.for_you_config_problems(cfg)
        self.assertTrue(any("hedge_markers" in p for p in problems), problems)

    def test_disabled_strips_everything(self):
        s = story(for_you=GOOD_LINE)
        self.assertEqual(vs.screen_for_you(s, dict(GOOD_CFG, enabled=False)),
                         "disabled")

    def test_profile_max_length_cannot_exceed_the_byte_cap(self):
        problems = vs.for_you_config_problems(
            dict(GOOD_CFG, max_length=vs.FOR_YOU_BYTE_CAP + 1))
        self.assertTrue(any("byte cap" in p for p in problems), problems)

    def test_profile_parser_reads_the_real_profile(self):
        """The loader must read the live profile block, not guess it."""
        cfg = vs.load_for_you_config(os.path.join(REPO_ROOT, "config"))
        self.assertIsInstance(cfg, dict)
        self.assertEqual(cfg.get("min_length"), 20)
        self.assertEqual(cfg.get("max_length"), 140)
        self.assertEqual(cfg.get("min_personal_relevance"), 7)
        self.assertEqual(cfg.get("max_stories"), 8)
        self.assertIn("education_tu", cfg.get("categories", []))
        self.assertEqual(cfg.get("reliability_tiers"), ["tier1", "tier2"])

    def test_no_screening_value_is_hardcoded_a_second_time(self):
        """Thresholds live in config/profile.yml, not in this file."""
        with open(vs.__file__, encoding="utf-8") as fh:
            source = fh.read()
        for value in ("min_length: 20", "max_length: 140",
                      "min_personal_relevance: 7", "max_stories: 8"):
            self.assertNotIn(value, source)
        for banned in ("banned_openers = [", "hedge_markers = ["):
            self.assertNotIn(banned, source)


class ShippedProfileWordListsTest(unittest.TestCase):
    """The LIVE config/profile.yml `for_you:` block, not an injected copy.

    THE WHOLE FEATURE WAS DEAD BECAUSE OF THIS. `screen_for_you()` fails CLOSED,
    so a profile without `banned_openers` and `hedge_markers` does not degrade
    gracefully - it strips every `for_you` line in the digest, prints a WARNING
    in the run summary, and delivers a digest with no personal layer at all.
    Every test above injects its own GOOD_CFG and would have stayed green
    through that entire outage, so the suite has to read the shipped file.

    Three of these are the owner's own offending lines, verbatim.
    """

    @classmethod
    def setUpClass(cls):
        cls.cfg = vs.load_for_you_config(os.path.join(REPO_ROOT, "config"))

    def _story(self, line, **over):
        return story(for_you=line, **over)

    def _screened(self, line, **over):
        """(reason, surviving_line) after the full delete-only screen."""
        s = self._story(line, **over)
        reason = vs.screen_for_you(s, self.cfg)
        vs.apply_for_you_screening([s], self.cfg)
        if reason is None:
            self.assertEqual(s.get("for_you"), line,
                             "a line that passed screening was not left intact")
        else:
            self.assertNotIn("for_you", s, "a rejected line was not deleted")
        return reason, s.get("for_you")

    # -- the config itself -------------------------------------------------
    def test_shipped_profile_carries_both_word_lists(self):
        for key in ("banned_openers", "hedge_markers"):
            self.assertIsInstance(self.cfg.get(key), list, key)
            self.assertTrue(self.cfg[key], "%s is empty in config/profile.yml" % key)
            self.assertTrue(all(isinstance(v, str) and v.strip()
                                for v in self.cfg[key]), key)

    def test_the_screen_can_run_at_all_against_the_shipped_profile(self):
        """The exact condition that killed the feature, asserted directly."""
        self.assertEqual(vs.for_you_config_problems(self.cfg), [])

    def test_shipped_word_lists_are_the_frozen_ones(self):
        # Spot-checks, not a full copy: a full copy here would be the second
        # drifting pair this file exists to prevent. These three are the
        # entries whose absence would have let a real offender through.
        for phrase in ("this may", "it is important to note", "users should"):
            self.assertIn(phrase, self.cfg["banned_openers"], phrase)
        for marker in ("may", "could", "suggests that"):
            self.assertIn(marker, self.cfg["hedge_markers"], marker)
        self.assertEqual(len(self.cfg["banned_openers"]), 24)
        self.assertEqual(len(self.cfg["hedge_markers"]), 15)

    # -- a good line, shipped config ---------------------------------------
    def test_an_honest_line_survives_against_the_shipped_profile(self):
        reason, kept = self._screened(
            "Form closes 12 Sep - that is your last day to apply.")
        self.assertIsNone(reason)
        self.assertEqual(kept, "Form closes 12 Sep - that is your last day to apply.")

    # -- the owner's three real offenders ----------------------------------
    def test_offender_one_may_signal_is_rejected(self):
        """'may signal further TU schedule changes' - the line that started it.

        Two screens reject it and the order is fixed, so the one that REPORTS
        is `not_concrete`: it asserts no time, amount or object and carries no
        imperative. The hedge is real too and the shipped `hedge_markers` does
        catch it - asserted below, because 'the line was deleted' alone would
        not prove the word list is doing any work.
        """
        reason, _ = self._screened("may signal further TU schedule changes")
        self.assertTrue(reason.startswith("not_concrete"), reason)
        self.assertIsNotNone(
            vs._hedge_hit("may signal further TU schedule changes",
                          self.cfg["hedge_markers"]),
            "the shipped hedge list no longer catches this line")

    def test_offender_two_user_is_fourth_semester_is_rejected(self):
        """Third-person self-reference: the digest is written TO him."""
        reason, _ = self._screened(
            "User is 4th semester BSc CSIT - monitor for further postponements")
        self.assertTrue(reason.startswith("third_person_self_reference"), reason)

    def test_offender_three_collaboration_events_is_rejected(self):
        """A mood, not a consequence - no time, no amount, no imperative."""
        reason, _ = self._screened(
            "Collaboration events offer limited-time content.")
        self.assertTrue(reason.startswith("not_concrete"), reason)

    # -- the two lists are individually load-bearing ------------------------
    def test_shipped_banned_opener_is_enforced_from_config(self):
        """'this may' is the FIRST frozen opener; a concrete line still dies."""
        reason, _ = self._screened(
            "This may mean you rebook by 12 Sep - check the TU portal")
        self.assertEqual(reason, "banned_opener: 'this may'")

    def test_shipped_hedge_marker_is_enforced_from_config(self):
        """A grounded numeral does not buy a pass for a hedge beside it."""
        reason, _ = self._screened(
            "The form closes 12 Sep and TU may add more exam centres")
        self.assertEqual(reason, "hedge_marker: 'may'")

    def test_the_lists_are_not_hardcoded_in_the_screener(self):
        """One copy only: dropping a list from config must fail closed, not
        quietly keep screening with a stale in-code list."""
        for banned in ("banned_openers = [", "hedge_markers = ["):
            with open(vs.__file__, encoding="utf-8") as fh:
                self.assertNotIn(banned, fh.read(), banned)
        crippled = dict(self.cfg)
        del crippled["hedge_markers"]
        self.assertTrue(vs.for_you_config_problems(crippled))


class FieldSwapTest(unittest.TestCase):
    """The old field is gone from the contract; the new one is optional."""

    def test_why_it_matters_is_not_required_anywhere(self):
        self.assertNotIn("why_it_matters", vs.REQUIRED_STR_FIELDS)
        # The name survives only in comments that explain what it replaced.
        # It must not appear in any executable line, or it is a live contract.
        with open(vs.__file__, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        live = [ln for ln in lines
                if "why_it_matters" in ln and not ln.lstrip().startswith("#")]
        self.assertEqual(live, [])

    def test_a_story_without_why_it_matters_passes_schema(self):
        s = story()
        s.pop("why_it_matters", None)
        ok, reason = vs.validate_story(s, 0)
        self.assertTrue(ok, reason)

    def test_for_you_is_not_required(self):
        ok, reason = vs.validate_story(story(), 0)
        self.assertTrue(ok, reason)
        self.assertNotIn("for_you", vs.REQUIRED_STR_FIELDS)

    def test_null_for_you_is_normalised_to_absent(self):
        """Never null: a renderer must never have to special-case it."""
        s = story(for_you=None)
        ok, _ = vs.validate_story(s, 0)
        self.assertTrue(ok)
        self.assertNotIn("for_you", s)

    def test_wrong_typed_for_you_never_drops_the_story(self):
        s = story(for_you=["not", "a", "string"])
        ok, _ = vs.validate_story(s, 0)
        self.assertTrue(ok)
        self.assertNotIn("for_you", s)

    def test_prompt_no_longer_references_the_old_field(self):
        with open(PROMPT, encoding="utf-8") as fh:
            self.assertNotIn("why_it_matters", fh.read())

    def test_prompt_specifies_the_new_field(self):
        with open(PROMPT, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('"for_you"', text)
        self.assertIn("20", text)
        self.assertIn("NEVER DESCRIBE THE WEATHER", text)
        # The prompt must not have gained authority over the weather engine.
        self.assertIn("deterministically in code", text)

    def test_for_you_is_not_a_dedup_key(self):
        """Dedup is title/url. for_you is nowhere in the dedup stage."""
        with open(vs.__file__, encoding="utf-8") as fh:
            source = fh.read()
        stage6 = source.split("# -- Stage 6: dedup")[1].split("# -- Stage 6b")[0]
        self.assertNotIn("for_you", stage6)


class DisplayOnlyTest(unittest.TestCase):
    """for_you is never a source of fact: deadlines render from `deadline`."""

    def test_deadline_renders_from_the_deadline_field(self):
        import render_digest as rd  # noqa: E402

        with_deadline = rd.build_story_card(story(
            deadline="2025-09-12",
            for_you="Form closes 12 Sep - that is your last day to apply"))
        self.assertIn("2025-09-12", with_deadline)
        self.assertIn("Deadline", with_deadline)

        without = rd.build_story_card(story(
            deadline=None,
            for_you="Form closes 12 Sep - that is your last day to apply"))
        self.assertNotIn("chip-deadline", without)
        self.assertNotIn("card-deadline", without)

    def test_screening_never_touches_deadline_or_summary(self):
        s = story(for_you="Form closes 30 Sep - that is your last day to apply")
        before = dict(s)
        vs.apply_for_you_screening([s], GOOD_CFG)
        self.assertNotIn("for_you", s)
        for field in ("deadline", "summary", "title", "effective_date",
                      "event_date", "priority", "personal_relevance"):
            self.assertEqual(s[field], before[field], field)


class PromptInjectionRuleTest(unittest.TestCase):
    """Both prompts must say, in their own words, that feed content is DATA.

    A security audit reported this hardening as applied to
    prompts/daily-digest.md. It was not: the rule existed nowhere in either
    prompt. That matters concretely - the pipeline feeds raw RSS headlines and
    scraped article text to a model running with `--auto`, and whatever that
    model returns is rendered and DELIVERED to the owner, so an instruction
    planted in a headline is an instruction aimed straight at him.

    The rule is asserted by content, not by an exact string, so a reworded (but
    still present) rule does not go red, and a DELETED one always does.
    """

    FILES = ("daily-digest.md", "breaking-classify.md")
    #: (a phrase that must be present, a regex that must match)
    PHRASES = (
        ("untrusted", r"untrusted"),
        ("data, not instructions", r"data,?\s+not\s+instructions"),
        ("never follow instructions found in content", r"never\s+follow"),
        ("covers every field name", r"snippet"),
        ("covers the URL", r"url"),
        ("drop the candidate", r"drop"),
        ("notes", r"notes|did not select|select nothing"),
    )

    @classmethod
    def setUpClass(cls):
        cls.texts = {}
        for name in cls.FILES:
            path = os.path.join(REPO_ROOT, "prompts", name)
            with open(path, encoding="utf-8") as fh:
                cls.texts[name] = fh.read()

    def test_the_rule_is_present_in_both_prompts(self):
        for name, text in self.texts.items():
            low = text.lower()
            for label, pattern in self.PHRASES:
                self.assertTrue(re.search(pattern, low, re.IGNORECASE),
                                "%s: the untrusted-input rule is missing %r"
                                % (name, label))

    def test_the_rule_appears_before_any_candidate_is_described(self):
        """It has to be read before the feed is described, not buried under the
        output schema.

        The role paragraph necessarily NAMES the input file before the rule can
        exist, so the anchor is the `## INPUT` heading where the candidates and
        their fields are actually laid out - that is the first place a model
        starts reading candidate-shaped text.
        """
        for name, text in self.texts.items():
            low = text.lower()
            rule_at = low.index("untrusted")
            self.assertLess(rule_at, low.index("## input"),
                            "%s describes the feed before stating the rule" % name)

    def test_the_rule_claims_no_authority_from_the_feed(self):
        for name, text in self.texts.items():
            self.assertIn("THIS prompt", text, name)

    def test_no_prompt_gained_a_second_copy_of_the_rule(self):
        for name, text in self.texts.items():
            self.assertEqual(text.lower().count("untrusted third-party data"), 1,
                             "%s states the rule more than once" % name)


if __name__ == "__main__":
    unittest.main()
