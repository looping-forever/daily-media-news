#!/usr/bin/env python3
"""tests/digest/test_article_text.py -- bounded article text for empty cards.

WHY THIS EXISTS. Run 42376820992 skipped 12 of 27 stories with "no publisher text
to summarise", and those shipped as headline-only cards. The fix is to read the
article itself and hand that text to the model -- never to invent a sentence.

The rules pinned here are the ones that keep that fetch safe and honest:

    ROBOTS IS ASKED FIRST, and an unreadable answer is a refusal.
    ONE PAGE IS BOUNDED: 5s, 512KB, 200 characters minimum, prose only.
    THE PHASE IS BOUNDED: 20 fetches, 120s, and it stops and says so.
    PUBLISHER TEXT IS NEVER REPLACED OR RE-FETCHED.
    FETCHED TEXT IS EVIDENCE, NOT THE PUBLISHER'S DESCRIPTION.
"""

import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import article_text as at  # noqa: E402
import summarise_stories as ss  # noqa: E402
from validate_stories import is_real_summary  # noqa: E402

PROSE = ("The district office said the highway would stay closed until the water "
         "receded, and crews were clearing debris from two landslide sites on the "
         "eastern approach through the morning. Officials expected the road to "
         "reopen only after engineers had checked the bridge decks on both sides.")

TITLE = "Flood closes highway for a second day"


class FakePage:
    """A url -> (status, content type, body) table that records every request."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def __call__(self, url, timeout, max_bytes):
        self.requested.append(url)
        if url not in self.pages:
            raise OSError("connection refused: %s" % url)
        return self.pages[url]


def allowed_robots():
    return (200, "text/plain", b"User-agent: *\nAllow: /\n")


def page_with(prose, extra=""):
    return (200, "text/html; charset=utf-8", (
        "<html><head><style>p{color:red}</style><script>alert(1)</script>"
        "<script /></head>"
        "<body><nav>Menu home world</nav><header>Site name</header>"
        "<p>%s</p>%s<footer>Copyright notice</footer></body></html>"
        % (prose, extra)).encode("utf-8"))


class RobotsIsAskedFirst(unittest.TestCase):
    def test_a_disallowed_path_is_never_fetched(self):
        page = FakePage({"https://x/robots.txt":
                         (200, "text/plain", b"User-agent: *\nDisallow: /private")})
        text, reason = at.fetch_with_reason("https://x/private/story",
                                            open_page=page)
        self.assertIsNone(text)
        self.assertEqual("robots", reason)
        self.assertEqual(["https://x/robots.txt"], page.requested,
                         "the article was requested despite robots.txt")

    def test_an_unreadable_robots_file_is_a_refusal(self):
        page = FakePage({})
        self.assertIsNone(at.fetch_article_text("https://x/story", open_page=page))
        self.assertEqual(["https://x/robots.txt"], page.requested)

    def test_a_404_robots_file_allows_the_fetch(self):
        page = FakePage({"https://x/robots.txt": (404, "text/plain", b""),
                         "https://x/story": page_with(PROSE)})
        text = at.fetch_article_text("https://x/story", open_page=page)
        self.assertIsNotNone(text, "RFC 9309: 4xx means no rules were published")
        self.assertEqual(["https://x/robots.txt", "https://x/story"],
                         page.requested)

    def test_a_real_404_exception_is_also_allowed(self):
        def page(url, timeout, max_bytes):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        self.assertTrue(at.robots_allows("https://x/story", open_page=page))

    def test_a_5xx_robots_file_is_a_refusal(self):
        page = FakePage({"https://x/robots.txt": (503, "text/plain", b"")})
        self.assertIsNone(at.fetch_article_text("https://x/story", open_page=page))
        self.assertEqual(["https://x/robots.txt"], page.requested)

    def test_disallowing_everything_is_a_refusal(self):
        page = FakePage({"https://x/robots.txt":
                         (200, "text/plain", b"User-agent: *\nDisallow: /\n")})
        self.assertIsNone(at.fetch_article_text("https://x/story", open_page=page))
        self.assertEqual(["https://x/robots.txt"], page.requested)

    def test_a_disallowed_prefix_blocks_only_that_path(self):
        page = FakePage({"https://x/robots.txt":
                         (200, "text/plain", b"User-agent: *\nDisallow: /private\n"),
                         "https://x/news/x": page_with(PROSE)})
        text = at.fetch_article_text("https://x/news/x", open_page=page)
        self.assertIsNotNone(text)
        self.assertIn("https://x/news/x", page.requested)


class OnePageIsBounded(unittest.TestCase):
    def test_a_403_or_404_yields_none_and_does_not_raise(self):
        for status in (403, 404):
            page = FakePage({"https://x/robots.txt": allowed_robots(),
                             "https://x/story": (status, "text/html", b"<p>no</p>")})
            self.assertIsNone(at.fetch_article_text("https://x/story",
                                                    open_page=page))

    def test_a_non_text_content_type_yields_none(self):
        page = FakePage({"https://x/robots.txt": allowed_robots(),
                         "https://x/story": (200, "application/octet-stream",
                                             b"\x00" * 500)})
        self.assertIsNone(at.fetch_article_text("https://x/story", open_page=page))

    def test_a_successful_page_yields_cleaned_text(self):
        page = FakePage({"https://x/robots.txt": allowed_robots(),
                         "https://x/story": page_with(PROSE)})
        text = at.fetch_article_text("https://x/story", open_page=page)
        self.assertIsNotNone(text)
        self.assertIn("the highway would stay closed", text)
        for gone in ("alert(1)", "color:red", "Menu home", "Copyright", "<p>"):
            self.assertNotIn(gone, text, "page chrome reached the evidence: %r" % text)

    def test_a_page_under_two_hundred_characters_yields_none(self):
        page = FakePage({"https://x/robots.txt": allowed_robots(),
                         "https://x/story": page_with("Too short to summarise.")})
        self.assertIsNone(at.fetch_article_text("https://x/story", open_page=page))


class ThePhaseIsBounded(unittest.TestCase):
    def items(self, count):
        return [{"title": "Story %d" % n, "summary": "",
                 "source_url": "https://x/%d" % n} for n in range(count)]

    def test_the_twenty_fetch_cap_stops_the_phase(self):
        calls = []

        def fetch(url, timeout=None):
            calls.append(url)
            return "x" * 300, "ok"

        lines = []
        texts, stats = ss.fetch_missing_text(
            self.items(25), fetch=fetch, log=lines.append, clock=lambda: 0)
        self.assertEqual(20, len(calls))
        self.assertEqual(20, stats["fetched"])
        self.assertEqual(20, len(texts))
        self.assertTrue(any("20-fetch cap" in line for line in lines),
                        "the phase must say it stopped: %r" % (lines,))

    def test_the_time_cap_stops_the_phase(self):
        now = [0.0]
        calls = []

        def clock():
            return now[0]

        def fetch(url, timeout=None):
            calls.append(url)
            now[0] += 40.0
            return "x" * 300, "ok"

        lines = []
        _texts, stats = ss.fetch_missing_text(
            self.items(5), fetch=fetch, log=lines.append, clock=clock,
            seconds=120)
        self.assertEqual(3, len(calls), "120s must stop the phase")
        self.assertTrue(any("120s budget" in line for line in lines),
                        "the phase must say it stopped: %r" % (lines,))

    def test_the_job_deadline_wins_over_the_phase_allowance(self):
        _texts, stats = ss.fetch_missing_text(
            self.items(5), fetch=lambda url, timeout=None: ("x" * 300, "ok"),
            log=lambda *_a: None, clock=lambda: 0, seconds=120, deadline=0)
        self.assertEqual(0, stats["fetched"])


class OnlyStoriesWithNoPublisherText(unittest.TestCase):
    def summarise(self, stories, snippets, fetch, run):
        return ss.summarise(
            stories, models=["gemini/a"], run=run, snippets=snippets,
            fetch_text=fetch, sleep=lambda *_a: None, clock=lambda: 0.0,
            log=lambda *_a: None, cooldown_path="", health_path="",
            budget_seconds=600)

    def test_a_story_with_publisher_text_is_never_fetched(self):
        stories = [
            {"title": "Has text", "summary": PROSE,
             "source_url": "https://x/1"},
            {"title": "No text", "summary": "",
             "source_url": "https://x/2"},
        ]
        fetched = []

        def fetch(url, timeout=None):
            fetched.append(url)
            return PROSE, "ok"

        def run(prompt, model=None, timeout=None):
            return json.dumps([{"url": "https://x/2",
                                "summary": "A model-written sentence about the "
                                           "closed highway and the crews."}])

        self.summarise(stories, {"https://x/1": PROSE}, fetch, run)
        self.assertEqual(["https://x/2"], fetched,
                         "publisher text must not be replaced by a scrape")

    def test_a_failed_fetch_does_not_fail_the_run(self):
        stories = [{"title": "No text", "summary": "",
                    "source_url": "https://x/2"}]
        out = self.summarise(stories, {}, lambda url, timeout=None:
                             (None, "http"), lambda *a, **k: "[]")
        self.assertEqual({}, out["summaries"])
        self.assertEqual(1, out["article_fetch"]["failed"])
        self.assertEqual(1, out["skipped_no_text"])

    def test_a_fetched_story_is_summarised_and_still_passes_is_real_summary(self):
        stories = [{"title": TITLE, "summary": "",
                    "source_url": "https://x/2"}]
        prompts = []
        answer = ("The highway stayed closed for a second day while crews "
                  "cleared two landslide sites, and the district office said 48 "
                  "hours of work remained.")

        def run(prompt, model=None, timeout=None):
            prompts.append(prompt)
            return json.dumps([{"url": "https://x/2", "summary": answer}])

        out = self.summarise(stories, {}, lambda url, timeout=None:
                             (PROSE, "ok"), run)
        self.assertEqual(answer, out["summaries"].get("https://x/2"))
        self.assertIn("the water receded", prompts[0],
                      "the fetched article must be the prompt's evidence")
        self.assertEqual("fetched", stories[0]["evidence_source"])
        self.assertTrue(is_real_summary(answer, TITLE, PROSE),
                        "a fetched summary must pass the same predicate")
        self.assertTrue(is_real_summary(answer, TITLE, ""),
                        "the pipeline judges it with the empty publisher snippet")

    def test_the_callers_snippets_are_not_mutated(self):
        stories = [{"title": TITLE, "summary": "",
                    "source_url": "https://x/2"}]
        snippets = {}
        self.summarise(stories, snippets, lambda url, timeout=None:
                       (PROSE, "ok"), lambda *a, **k: "[]")
        self.assertEqual({}, snippets,
                         "fetched text is not the publisher's description")


class TheEvidenceWindow(unittest.TestCase):
    """400 characters for a publisher snippet, 1200 for a fetched article."""

    LONG = "the water kept rising through the eastern approach " * 80

    def prompts_for(self, stories, snippets, fetch):
        prompts = []

        def run(prompt, model=None, timeout=None):
            prompts.append(prompt)
            return "[]"

        ss.summarise(stories, models=["gemini/a"], run=run, snippets=snippets,
                     fetch_text=fetch, sleep=lambda *_a: None,
                     clock=lambda: 0.0, log=lambda *_a: None,
                     cooldown_path="", health_path="", budget_seconds=600)
        return prompts

    def test_fetched_evidence_reaches_the_prompt_at_twelve_hundred(self):
        stories = [{"title": TITLE, "summary": "", "source_url": "https://x/2"}]
        prompts = self.prompts_for(stories, {},
                                   lambda url, timeout=None: (self.LONG, "ok"))
        self.assertIn(self.LONG[:1200], prompts[0],
                      "the fetched article must reach the prompt")
        self.assertNotIn(self.LONG[:1201], prompts[0],
                         "the fetched window is 1200 characters, not more")

    def test_publisher_evidence_stays_at_four_hundred(self):
        stories = [{"title": TITLE, "summary": "", "source_url": "https://x/2"}]

        def never_called(url, timeout=None):
            raise AssertionError("publisher text must not be fetched: %s" % url)

        prompts = self.prompts_for(stories, {"https://x/2": self.LONG},
                                   never_called)
        self.assertIn(self.LONG[:400], prompts[0])
        self.assertNotIn(self.LONG[:401], prompts[0],
                         "a publisher snippet keeps the proven 400 characters")


if __name__ == "__main__":
    unittest.main()
