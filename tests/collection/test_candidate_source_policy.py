#!/usr/bin/env python3
"""Offline tests: what may enter collection at all, judged by HOST.

MEASURED, 3 days / 7 delivered editions / 35 cards:

    Google News links   222 of 541 candidates (41%), 0 resolved, 0 of 35 cards,
                        and 51 of the 57 textless skips.
    dev.to              10 of 35 cards (28.6%), including vendors writing about
                        their own products.

Both are now refused by host, with their own counted reason, so the loss shows
up in telemetry instead of looking like a thin news day. `prefer_direct_feeds`
and `resolve_publisher_url` are deliberately kept as the fallback for a link
that carries a real publisher URL.

NO NETWORK: candidates are in-memory dicts, and the feed check builds its RSS
body the way tests/collection/test_weather_adapter.py does -- a relative pubDate
against the real clock, so the fixture can never age past FRESHNESS_HOURS.
"""

import datetime
import os
import sys
import unittest
from email.utils import format_datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import collect_news as cn  # noqa: E402
import render_digest as rd  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)
SECTION_KEYS = tuple(key for key, _header, _color in rd.SECTIONS)


def _cand(title, url, hours_ago=2, snippet="", language="en"):
    dt = (None if hours_ago is None
          else NOW - datetime.timedelta(hours=hours_ago))
    return {
        "title": title,
        "url": url,
        "source": cn.netloc_of(url),
        "published": dt.isoformat() if dt else None,
        "snippet": snippet,
        "domain_category": "nepal",
        "language": language,
        "_dt": dt,
        "_kind": "gnews",
    }


GOOGLE_URL = "https://news.google.com/rss/articles/CBMiAUF"
HEADLINE = "Nepal cabinet approves annual budget framework"


def rss_doc(items=6, hours_ago=1):
    """An RSS 2.0 body with a pubDate relative to now (never ages to stale)."""
    stamp = format_datetime(NOW - datetime.timedelta(hours=hours_ago))
    body = "".join(
        "<item><title>Headline %d about the world today</title>"
        "<link>https://example.com/%d</link><pubDate>%s</pubDate>"
        "<source url=\"https://example.com/\">Example</source></item>"
        % (i, i, stamp) for i in range(items))
    return ("<?xml version='1.0' encoding='UTF-8'?><rss><channel>%s</channel>"
            "</rss>" % body).encode("utf-8")


class TestGoogleNewsNeverEnters(unittest.TestCase):
    def test_a_google_hosted_candidate_never_survives_collection(self):
        final, _per_domain, stats = cn.postprocess(
            [_cand(HEADLINE, GOOGLE_URL, snippet="Cabinet clears budget.")], [], 4)
        self.assertEqual([], final,
                         "a news.google.com item reached the payload; it cannot "
                         "resolve to publisher text, so every one is a textless "
                         "skip waiting to happen")
        self.assertEqual(1, stats["dropped_google_unresolvable"])

    def test_any_google_subdomain_is_refused_too(self):
        for url in ("https://www.news.google.com/rss/articles/CBMiAUF",
                    "https://feeds.news.google.com/rss/articles/CBMiAUF"):
            self.assertEqual(
                cn.GOOGLE_DROP_REASON, cn.drop_reason_for({"url": url}),
                "%r escaped the Google drop: any subdomain must be refused"
                % (url,))
        # A DIFFERENT registrable domain is not a Google News host: the suffix
        # match must not reach across a dot that is not a label boundary.
        self.assertEqual(
            "", cn.drop_reason_for({"url": "https://news.google.co.uk/rss"}))

    def test_the_drop_is_counted_and_reported(self):
        cands = [_cand("Story %d" % i, GOOGLE_URL + str(i)) for i in range(3)]
        _final, _per_domain, stats = cn.postprocess(cands, [], 4)
        self.assertEqual(3, stats["dropped_google_unresolvable"])
        self.assertIn(
            "3 dropped as unresolvable",
            cn.google_preference_line(stats),
            "the telemetry line does not report the count, so the loss is "
            "invisible again: %r" % (cn.google_preference_line(stats),))
        self.assertIn("no publisher URL", cn.google_preference_line(stats))

    def test_the_fallback_helpers_are_still_present(self):
        """Kept on purpose: a link that does carry a publisher URL still uses them."""
        self.assertTrue(callable(cn.prefer_direct_feeds))
        import article_text
        self.assertTrue(callable(article_text.resolve_publisher_url))


class TestSelfPublishedHostsAreRefused(unittest.TestCase):
    def test_a_devto_url_is_blocked(self):
        final, _per_domain, stats = cn.postprocess(
            [_cand("How our team ships faster", "https://dev.to/acme/shipping")],
            [], 4)
        self.assertEqual([], final, "a dev.to item reached the payload")
        self.assertEqual(1, stats["dropped_self_published"])

    def test_the_same_headline_from_bbc_is_kept(self):
        """The block is the host, not the story: no keyword, no guessing."""
        final, _per_domain, stats = cn.postprocess(
            [_cand(HEADLINE, "https://www.bbc.co.uk/news/world-1234",
                   snippet="The cabinet approved the framework on Friday.")],
            [], 4)
        self.assertEqual(1, len(final), "a real publisher was blocked too")
        self.assertEqual(0, stats["dropped_self_published"])

    def test_a_subdomain_is_blocked_and_a_lookalike_is_not(self):
        self.assertEqual(
            cn.SELF_PUBLISHED_DROP_REASON,
            cn.drop_reason_for({"url": "https://blog.dev.to/acme/post"}))
        self.assertEqual(
            "",
            cn.drop_reason_for({"url": "https://notdev.to/acme/post"}),
            "a host that merely ENDS in the string was blocked: the suffix "
            "match must require a dot label")

    def test_every_platform_in_the_list_is_refused(self):
        for host in cn.SELF_PUBLISHED_HOSTS:
            self.assertEqual(
                cn.SELF_PUBLISHED_DROP_REASON,
                cn.drop_reason_for({"url": "https://%s/somebody/post" % host}),
                "%r is listed but not refused" % (host,))
        self.assertIn("dev.to", cn.SELF_PUBLISHED_HOSTS)

    def test_the_drop_is_counted_and_reported(self):
        cands = [_cand("Post %d" % i, "https://dev.to/acme/%d" % i)
                 for i in range(2)]
        _final, _per_domain, stats = cn.postprocess(cands, [], 4)
        self.assertEqual(2, stats["dropped_self_published"])
        line = cn.self_published_line(stats)
        self.assertIn("2 dropped", line)
        self.assertIn("dev.to", line, "the line does not name the hosts: %r"
                      % (line,))


class TestDirectFeedsParseOffline(unittest.TestCase):
    """Every configured feed URL is exercised through the real parser.

    The body is built here, not fetched: a test that called these hosts would
    fail on a runner with no network and would test the internet, not the
    config. What is pinned is the shape the collector actually requires -- RSS
    2.0, <item> children, dates -- and the language/section each entry carries.
    """

    def _parse(self, url, category, language, items=6):
        body = rss_doc(items)
        original = cn.fetch_bytes_retry
        cn.fetch_bytes_retry = lambda *a, **k: body
        try:
            return cn.rss_task(url, category, language, 5)
        finally:
            cn.fetch_bytes_retry = original

    def test_every_feed_url_parses_with_items_and_dates(self):
        for name, url, category, language in cn.DIRECT_FEEDS:
            with self.subTest(feed=name):
                self.assertTrue(url.startswith("https://"), url)
                parsed = self._parse(url, category, language)
                self.assertGreaterEqual(
                    len(parsed), 5,
                    "%r parses to %d item(s); a feed under 5 is not worth a "
                    "plan" % (name, len(parsed)))
                dated = [c for c in parsed if c["published"]]
                self.assertGreaterEqual(
                    len(dated), 3,
                    "%r produced %d dated item(s): an undated feed sorts to "
                    "EPOCH and is dropped as stale" % (name, len(dated)))
                self.assertEqual(category, parsed[0]["domain_category"])
                self.assertEqual(language, parsed[0]["language"])

    def test_every_feed_declares_a_section_and_a_language(self):
        for name, _url, category, language in cn.DIRECT_FEEDS:
            with self.subTest(feed=name):
                self.assertIn(
                    category, SECTION_KEYS,
                    "%r is filed under %r, which the digest cannot render"
                    % (name, category))
                self.assertIn(language, ("en", "ne"),
                              "%r declares language %r" % (name, language))
                self.assertTrue(name.startswith("feed:%s-" % category.split("_")[0]),
                                "%r does not follow feed:<section>-<name>" % (name,))

    def test_the_urls_and_names_are_unique(self):
        urls = [u for _n, u, _c, _l in cn.DIRECT_FEEDS]
        names = [n for n, _u, _c, _l in cn.DIRECT_FEEDS]
        self.assertEqual(len(set(urls)), len(urls), "duplicate feed URL")
        self.assertEqual(len(set(names)), len(names), "duplicate feed name")

    def test_devto_is_no_longer_configured(self):
        urls = [u for _n, u, _c, _l in cn.DIRECT_FEEDS]
        self.assertNotIn(
            "https://dev.to/feed", urls,
            "dev.to is still a configured feed, so every item it returns is "
            "fetched only to be dropped by the blocklist")


if __name__ == "__main__":
    unittest.main()
