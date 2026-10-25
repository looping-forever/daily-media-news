#!/usr/bin/env python3
"""tests/media/test_media_extraction.py — offline tests for the media
extraction (collect_news.py) and media passthrough validation
(validate_stories.py) added for the digest media upgrade.

Plain asserts, no test framework, stdlib only. Run:

    py tests\\media\\test_media_extraction.py

Network is never touched: collect_news.fetch_bytes is mocked to return the
fixture RSS; validate_stories is exercised directly on in-memory dicts.
"""

import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
import unittest.mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import collect_news as cn      # noqa: E402
import validate_stories as vs  # noqa: E402
import enrich_media as em      # noqa: E402

MEDIA_NS = "http://search.yahoo.com/mrss/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def rss_doc(items_xml):
    """Wrap a list of <item> fragments in a minimal RSS document."""
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<rss><channel>%s</channel></rss>" % "".join(items_xml))


def item_from(xml):
    return ET.fromstring(rss_doc([xml])).find(".//item")


# ---------------------------------------------------------------------------
# Fixture RSS items (one per extraction path)
# ---------------------------------------------------------------------------

ITEM_MEDIA_CONTENT = (
    '<item><title>Media content story</title>'
    '<link>https://example.com/mc</link>'
    '<pubDate>Sat, 29 Aug 2026 10:00:00 +0000</pubDate>'
    '<description>desc</description>'
    '<media:content xmlns:media="%s" '
    'url="https://cdn.example.com/img.jpg" type="image/jpeg" '
    'width="1266" height="628"/></item>' % MEDIA_NS
)

ITEM_ENCLOSURE = (
    '<item><title>Enclosure story</title>'
    '<link>https://example.com/enc</link>'
    '<pubDate>Sat, 29 Aug 2026 10:00:00 +0000</pubDate>'
    '<description>desc</description>'
    '<enclosure url="https://cdn.example.com/v.mp4" type="video/mp4"/>'
    '</item>'
)

ITEM_INLINE_IMG = (
    '<item><title>Inline img story</title>'
    '<link>https://example.com/inline</link>'
    '<pubDate>Sat, 29 Aug 2026 10:00:00 +0000</pubDate>'
    '<description>desc</description>'
    '<content:encoded xmlns:content="%s">'
    '&lt;p&gt;Some text &lt;img src="https://cdn.example.com/inline.png" '
    'width="800" height="450"/&gt; more text&lt;/p&gt;'
    '</content:encoded></item>' % CONTENT_NS
)

ITEM_NO_MEDIA = (
    '<item><title>No media story</title>'
    '<link>https://example.com/none</link>'
    '<pubDate>Sat, 29 Aug 2026 10:00:00 +0000</pubDate>'
    '<description>just text</description></item>'
)

ITEM_ITUNES = (
    '<item><title>Itunes story</title>'
    '<link>https://example.com/it</link>'
    '<pubDate>Sat, 29 Aug 2026 10:00:00 +0000</pubDate>'
    '<description>desc</description>'
    '<itunes:image xmlns:itunes="%s" '
    'href="https://cdn.example.com/pod.jpg"/></item>' % ITUNES_NS
)

ITEM_THUMBNAIL = (
    '<item><title>Thumbnail story</title>'
    '<link>https://example.com/th</link>'
    '<pubDate>Sat, 29 Aug 2026 10:00:00 +0000</pubDate>'
    '<description>desc</description>'
    '<media:thumbnail xmlns:media="%s" '
    'url="https://cdn.example.com/thumb.jpg" width="120" height="90"/>'
    '</item>' % MEDIA_NS
)


# ---------------------------------------------------------------------------
# extract_media unit tests (all extraction paths)
# ---------------------------------------------------------------------------

def test_extract_media_content_image():
    image, video = cn.extract_media(item_from(ITEM_MEDIA_CONTENT))
    assert image == {
        "url": "https://cdn.example.com/img.jpg",
        "type": "image/jpeg",
        "source": "media:content",
        "width": 1266,
        "height": 628,
    }, image
    assert video is None
    print("  PASS: test_extract_media_content_image")


def test_extract_media_enclosure_video():
    image, video = cn.extract_media(item_from(ITEM_ENCLOSURE))
    assert image is None
    assert video == {
        "url": "https://cdn.example.com/v.mp4",
        "type": "video/mp4",
        "source": "enclosure",
    }, video
    print("  PASS: test_extract_media_enclosure_video")


def test_extract_media_inline_img():
    image, video = cn.extract_media(item_from(ITEM_INLINE_IMG))
    assert image == {
        "url": "https://cdn.example.com/inline.png",
        "source": "content:encoded-img",
        "type": "image/png",
        "width": 800,
        "height": 450,
    }, image
    assert video is None
    print("  PASS: test_extract_media_inline_img")


def test_extract_media_no_media():
    image, video = cn.extract_media(item_from(ITEM_NO_MEDIA))
    assert image is None and video is None
    print("  PASS: test_extract_media_no_media")


def test_extract_media_itunes():
    image, video = cn.extract_media(item_from(ITEM_ITUNES))
    assert image == {
        "url": "https://cdn.example.com/pod.jpg",
        "source": "itunes:image",
        "type": "image/jpeg",
    }, image
    assert video is None
    print("  PASS: test_extract_media_itunes")


def test_extract_media_thumbnail():
    image, video = cn.extract_media(item_from(ITEM_THUMBNAIL))
    assert image == {
        "url": "https://cdn.example.com/thumb.jpg",
        "source": "media:thumbnail",
        "type": "image/jpeg",
        "width": 120,
        "height": 90,
    }, image
    assert video is None
    print("  PASS: test_extract_media_thumbnail")


def test_extract_media_rejects_non_http():
    """data:/javascript:/relative URLs are dropped (never emitted)."""
    bad = item_from(
        '<item><title>t</title><link>https://example.com/b</link>'
        '<media:content xmlns:media="%s" '
        'url="data:image/png;base64,AAAA" type="image/png"/>'
        '<enclosure url="javascript:alert(1)" type="video/mp4"/>'
        '</item>' % MEDIA_NS)
    image, video = cn.extract_media(bad)
    assert image is None and video is None, (image, video)
    print("  PASS: test_extract_media_rejects_non_http")


# ---------------------------------------------------------------------------
# rss_task end-to-end: candidates carry optional media fields
# ---------------------------------------------------------------------------

def test_rss_task_emits_media_fields():
    body = rss_doc([ITEM_MEDIA_CONTENT, ITEM_ENCLOSURE, ITEM_INLINE_IMG,
                    ITEM_NO_MEDIA])
    with unittest.mock.patch.object(cn, "fetch_bytes",
                                    return_value=body.encode("utf-8")):
        cands = cn.rss_task("https://feeds.example.test/rss",
                            "nepal", "en", 10)
    by_url = {c["url"]: c for c in cands}
    assert len(cands) == 4, cands

    mc = by_url["https://example.com/mc"]
    assert mc["image"] == {
        "url": "https://cdn.example.com/img.jpg", "type": "image/jpeg",
        "source": "media:content", "width": 1266, "height": 628,
    }, mc["image"]
    assert "video" not in mc

    enc = by_url["https://example.com/enc"]
    assert enc["video"] == {
        "url": "https://cdn.example.com/v.mp4", "type": "video/mp4",
        "source": "enclosure",
    }, enc["video"]
    assert "image" not in enc

    inline = by_url["https://example.com/inline"]
    assert inline["image"]["source"] == "content:encoded-img"
    assert inline["image"]["url"] == "https://cdn.example.com/inline.png"

    none = by_url["https://example.com/none"]
    assert "image" not in none and "video" not in none, \
        "story without media must flow through unchanged"
    print("  PASS: test_rss_task_emits_media_fields")


# ---------------------------------------------------------------------------
# Validator: passthrough valid media, drop invalid, never kill the story
# ---------------------------------------------------------------------------

def _write_candidates(cands):
    path = os.path.join(tempfile.mkdtemp(prefix="media-test-"), "c.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"candidates": cands}, f)
    return path


def test_validator_keeps_valid_media():
    cands = [{
        "url": "https://example.com/story1",
        "image": {"url": "https://cdn.example.com/img.jpg"},
        "video": {"url": "https://cdn.example.com/v.mp4"},
    }]
    cmap = vs.build_candidate_media_map(_write_candidates(cands))
    story = {
        "source_url": "https://example.com/story1",
        "image": {"url": "https://cdn.example.com/img.jpg"},
        "video": {"url": "https://cdn.example.com/v.mp4"},
    }
    story, dropped = vs.validate_media_fields(story, cmap)
    assert dropped == [], dropped
    assert story["image"]["url"] == "https://cdn.example.com/img.jpg"
    assert story["video"]["url"] == "https://cdn.example.com/v.mp4"
    print("  PASS: test_validator_keeps_valid_media")


def test_validator_drops_invented_media_keeps_story():
    cands = [{
        "url": "https://example.com/story1",
        "image": {"url": "https://cdn.example.com/img.jpg"},
    }]
    cmap = vs.build_candidate_media_map(_write_candidates(cands))
    # AI invented a URL not present in the candidate -> dropped, story kept.
    story = {
        "source_url": "https://example.com/story1",
        "image": {"url": "https://cdn.example.com/INVENTED.jpg"},
    }
    story, dropped = vs.validate_media_fields(story, cmap)
    assert "image" not in story, "invented media must be dropped"
    assert story["source_url"] == "https://example.com/story1", \
        "story must survive media validation"
    assert any(f == "image" for f, _ in dropped)
    print("  PASS: test_validator_drops_invented_media_keeps_story")


def test_validator_drops_bad_scheme_and_private_host():
    cands = [{
        "url": "https://example.com/story1",
        "image": {"url": "https://cdn.example.com/img.jpg"},
    }]
    cmap = vs.build_candidate_media_map(_write_candidates(cands))
    for bad_url in ("data:image/png;base64,AAAA",
                    "http://192.168.1.5/x.jpg",
                    "http://localhost/x.jpg"):
        story = {"source_url": "https://example.com/story1",
                 "image": {"url": bad_url}}
        story, dropped = vs.validate_media_fields(story, cmap)
        assert "image" not in story, bad_url
        assert story["source_url"] == "https://example.com/story1"
        assert dropped, bad_url
    print("  PASS: test_validator_drops_bad_scheme_and_private_host")


def test_validator_no_media_unchanged():
    cmap = vs.build_candidate_media_map(_write_candidates([]))
    story = {"source_url": "https://example.com/story1", "title": "x"}
    story, dropped = vs.validate_media_fields(story, cmap)
    assert dropped == []
    assert story == {"source_url": "https://example.com/story1", "title": "x"}
    print("  PASS: test_validator_no_media_unchanged")


def test_validator_null_media_normalized_to_absent():
    cmap = vs.build_candidate_media_map(_write_candidates([]))
    story = {"source_url": "https://example.com/story1",
             "image": None, "video": None}
    story, dropped = vs.validate_media_fields(story, cmap)
    assert dropped == []
    assert "image" not in story and "video" not in story
    print("  PASS: test_validator_null_media_normalized_to_absent")


# ---------------------------------------------------------------------------
# enrich_media.parse_og_meta unit tests (offline, local HTML strings only)
# ---------------------------------------------------------------------------

def test_parse_og_meta_image_present():
    html = ('<html><head>'
            '<meta property="og:image" content="https://cdn.example.com/img.jpg">'
            '</head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"] == {
        "url": "https://cdn.example.com/img.jpg", "source": "og",
    }, og["image"]
    assert og["video"] is None
    print("  PASS: test_parse_og_meta_image_present")


def test_parse_og_meta_image_absent():
    html = '<html><head><title>no media here</title></head></html>'
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"] is None and og["video"] is None, og
    print("  PASS: test_parse_og_meta_image_absent")


def test_parse_og_meta_twitter_image_fallback():
    html = ('<html><head>'
            '<meta name="twitter:image" content="https://cdn.example.com/tw.jpg">'
            '</head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"] == {
        "url": "https://cdn.example.com/tw.jpg", "source": "og",
    }, og["image"]
    print("  PASS: test_parse_og_meta_twitter_image_fallback")


def test_parse_og_meta_og_image_url_fallback():
    html = ('<html><head>'
            '<meta property="og:image:url" content="https://cdn.example.com/ogurl.jpg">'
            '</head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"] == {
        "url": "https://cdn.example.com/ogurl.jpg", "source": "og",
    }, og["image"]
    print("  PASS: test_parse_og_meta_og_image_url_fallback")


def test_parse_og_meta_relative_url_resolution():
    html = ('<html><head>'
            '<meta property="og:image" content="/img/rel.jpg">'
            '</head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"]["url"] == "https://example.com/img/rel.jpg", og["image"]
    print("  PASS: test_parse_og_meta_relative_url_resolution")


def test_parse_og_meta_protocol_relative_url_resolution():
    html = ('<html><head>'
            '<meta property="og:image" content="//cdn.example.com/proto.jpg">'
            '</head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"]["url"] == "https://cdn.example.com/proto.jpg", og["image"]
    print("  PASS: test_parse_og_meta_protocol_relative_url_resolution")


def test_parse_og_meta_video_with_type():
    html = ('<html><head>'
            '<meta property="og:video" content="https://cdn.example.com/v.mp4">'
            '<meta property="og:video:type" content="video/mp4">'
            '</head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["video"] == {
        "url": "https://cdn.example.com/v.mp4",
        "type": "video/mp4", "source": "og",
    }, og["video"]
    assert og["image"] is None
    print("  PASS: test_parse_og_meta_video_with_type")


def test_parse_og_meta_rejects_non_http():
    html = ('<html><head>'
            '<meta property="og:image" content="data:image/png;base64,AAAA">'
            '<meta property="og:video" content="javascript:alert(1)">'
            '</head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"] is None and og["video"] is None, og
    print("  PASS: test_parse_og_meta_rejects_non_http")


def test_parse_og_meta_malformed_html_no_crash():
    # Unclosed tags, stray '<', mismatched tags: must never raise.
    html = ('<html><head><meta property="og:image" '
            'content="https://cdn.example.com/x.jpg"'
            '<meta property="og:video" content="https://cdn.example.com/v.mp4"'
            '<div><span>unclosed</div></head></html>')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert isinstance(og, dict)
    assert "image" in og and "video" in og
    print("  PASS: test_parse_og_meta_malformed_html_no_crash")


# ---------------------------------------------------------------------------
# find_candidate_media (validate_stories) 3-tier matching
# ---------------------------------------------------------------------------

def test_find_candidate_media_exact_url():
    cmap = {
        "by_raw_url": {"https://example.com/a": {"image": {"u1"}, "video": set()}},
        "by_url": {},
        "by_title": [],
    }
    entry = vs.find_candidate_media(cmap, "https://example.com/a", "Some title")
    assert entry == {"image": {"u1"}, "video": set()}, entry
    print("  PASS: test_find_candidate_media_exact_url")


def test_find_candidate_media_normalized_url():
    cmap = {
        "by_raw_url": {},
        "by_url": {"example.com/a": {"image": {"u1"}, "video": set()}},
        "by_title": [],
    }
    entry = vs.find_candidate_media(
        cmap, "https://www.example.com/a/?q=1#frag", "t")
    assert entry == {"image": {"u1"}, "video": set()}, entry
    print("  PASS: test_find_candidate_media_normalized_url")


def test_find_candidate_media_title_sequence():
    cmap = {
        "by_raw_url": {},
        "by_url": {},
        "by_title": [("Apple launches new iPhone 15 Pro Max today",
                      {"image": {"u1"}, "video": set()})],
    }
    entry = vs.find_candidate_media(
        cmap, "https://example.com/x", "Apple launches new iPhone 15 Pro Max")
    assert entry == {"image": {"u1"}, "video": set()}, entry
    print("  PASS: test_find_candidate_media_title_sequence")


def test_find_candidate_media_title_jaccard():
    # SequenceMatcher ratio < 0.85 (word order differs) but jaccard >= 0.7.
    cmap = {
        "by_raw_url": {},
        "by_url": {},
        "by_title": [("Apple iPhone 15 Pro Max launches new",
                      {"image": {"u1"}, "video": set()})],
    }
    entry = vs.find_candidate_media(
        cmap, "https://example.com/x", "Apple launches new iPhone 15 Pro Max")
    assert entry == {"image": {"u1"}, "video": set()}, entry
    print("  PASS: test_find_candidate_media_title_jaccard")


def test_find_candidate_media_empty_fallback():
    empty = {"image": set(), "video": set()}
    assert vs.find_candidate_media({}, "https://example.com/x", "t") == empty
    assert vs.find_candidate_media(None, "https://example.com/x", "t") == empty
    assert vs.find_candidate_media("not-a-dict", "https://example.com/x", "t") == empty
    print("  PASS: test_find_candidate_media_empty_fallback")


# ---------------------------------------------------------------------------
# enrich_media.find_candidate (its own matcher)
# ---------------------------------------------------------------------------

def test_find_candidate_exact_url():
    story = {"source_url": "https://example.com/a", "title": "T"}
    cands = [{"url": "https://example.com/a", "title": "T"}]
    assert em.find_candidate(story, cands) is cands[0]
    print("  PASS: test_find_candidate_exact_url")


def test_find_candidate_normalized_url():
    story = {"source_url": "https://www.example.com/a/?q=1", "title": "T"}
    cands = [{"url": "https://example.com/a", "title": "T"}]
    assert em.find_candidate(story, cands) is cands[0]
    print("  PASS: test_find_candidate_normalized_url")


def test_find_candidate_title_match():
    story = {"source_url": "https://example.com/x",
             "title": "Apple launches new iPhone 15 Pro Max"}
    cands = [{"url": "https://example.com/c",
              "title": "Apple launches new iPhone 15 Pro Max today"}]
    assert em.find_candidate(story, cands) is cands[0]
    print("  PASS: test_find_candidate_title_match")


def test_find_candidate_no_match():
    story = {"source_url": "https://example.com/x", "title": "Completely unrelated"}
    cands = [{"url": "https://example.com/c", "title": "Something else entirely"}]
    assert em.find_candidate(story, cands) is None
    print("  PASS: test_find_candidate_no_match")


# ---------------------------------------------------------------------------
# enrich_media._http_url host-less rejection
# ---------------------------------------------------------------------------

def test_http_url_rejects_hostless():
    assert em._http_url("http://") is None
    assert em._http_url("https://") is None
    assert em._http_url("http:///path") is None
    assert em._http_url("https://example.com/x") == "https://example.com/x"
    assert em._http_url("data:image/png;base64,AAAA") is None
    assert em._http_url("javascript:alert(1)") is None
    print("  PASS: test_http_url_rejects_hostless")


# ---------------------------------------------------------------------------
# enrich_media.parse_og_meta priority + secure_url + private-host rejection
# ---------------------------------------------------------------------------

def test_parse_og_meta_image_priority_over_document_order():
    # twitter:image appears first in the document, but og:image must win.
    html = ('<meta name="twitter:image" content="https://cdn.example.com/tw.jpg">'
            '<meta property="og:image" content="https://cdn.example.com/og.jpg">')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"]["url"] == "https://cdn.example.com/og.jpg", og["image"]
    print("  PASS: test_parse_og_meta_image_priority_over_document_order")


def test_parse_og_meta_secure_url_variants():
    html = ('<meta property="og:image:secure_url" content="https://cdn.example.com/sec.jpg">'
            '<meta property="og:video:url" content="https://cdn.example.com/vurl.mp4">'
            '<meta property="og:video:secure_url" content="https://cdn.example.com/vsec.mp4">')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["image"]["url"] == "https://cdn.example.com/sec.jpg", og["image"]
    # og:video:url has higher priority than og:video:secure_url.
    assert og["video"]["url"] == "https://cdn.example.com/vurl.mp4", og["video"]
    print("  PASS: test_parse_og_meta_secure_url_variants")


def test_parse_og_meta_rejects_private_host():
    for bad in ("http://192.168.1.5/x.jpg",
                "http://169.254.169.254/latest/meta-data/",
                "http://localhost/x.jpg",
                "http://[::1]/x.jpg"):
        html = '<meta property="og:image" content="%s">' % bad
        og = em.parse_og_meta(html, "https://example.com/story")
        assert og["image"] is None, bad
    print("  PASS: test_parse_og_meta_rejects_private_host")


# ---------------------------------------------------------------------------
# enrich_media.parse_og_meta blocked placeholder/CDN host rejection
# ---------------------------------------------------------------------------

def _og_image_html(url):
    return '<meta property="og:image" content="%s">' % url


def test_parse_og_meta_rejects_lh3_googleusercontent():
    og = em.parse_og_meta(
        _og_image_html("https://lh3.googleusercontent.com/abc/def.jpg"),
        "https://example.com/story")
    assert og["image"] is None, og["image"]
    print("  PASS: test_parse_og_meta_rejects_lh3_googleusercontent")


def test_parse_og_meta_rejects_docs_google():
    og = em.parse_og_meta(
        _og_image_html("https://docs.google.com/abc/def.png"),
        "https://example.com/story")
    assert og["image"] is None, og["image"]
    print("  PASS: test_parse_og_meta_rejects_docs_google")


def test_parse_og_meta_accepts_legit_news_host():
    og = em.parse_og_meta(
        _og_image_html("https://assets2.cbsnewsstatic.com/abc/def.jpg"),
        "https://example.com/story")
    assert og["image"] == {
        "url": "https://assets2.cbsnewsstatic.com/abc/def.jpg",
        "source": "og",
    }, og["image"]
    print("  PASS: test_parse_og_meta_accepts_legit_news_host")


def test_parse_og_meta_rejects_placehold_co():
    og = em.parse_og_meta(
        _og_image_html("https://placehold.co/600x400"),
        "https://example.com/story")
    assert og["image"] is None, og["image"]
    print("  PASS: test_parse_og_meta_rejects_placehold_co")


def test_parse_og_meta_blocklist_case_insensitive():
    og = em.parse_og_meta(
        _og_image_html("https://LH3.GOOGLEUSERCONTENT.COM/abc/def.jpg"),
        "https://example.com/story")
    assert og["image"] is None, og["image"]
    print("  PASS: test_parse_og_meta_blocklist_case_insensitive")


def test_parse_og_meta_blocklist_subdomain_match():
    og = em.parse_og_meta(
        _og_image_html("https://foo.googleusercontent.com/abc/def.jpg"),
        "https://example.com/story")
    assert og["image"] is None, og["image"]
    print("  PASS: test_parse_og_meta_blocklist_subdomain_match")


def test_parse_og_meta_blocklist_applies_to_video():
    html = ('<meta property="og:video" '
            'content="https://lh3.googleusercontent.com/abc/def.mp4">')
    og = em.parse_og_meta(html, "https://example.com/story")
    assert og["video"] is None, og["video"]
    print("  PASS: test_parse_og_meta_blocklist_applies_to_video")


# ---------------------------------------------------------------------------
# enrich_media.enrich_story (fetch path mocked — no network)
# ---------------------------------------------------------------------------

def test_enrich_story_attaches_og_media():
    story = {"source_url": "https://example.com/story", "title": "Story"}
    candidates = [{"url": "https://example.com/story", "title": "Story"}]
    html = ('<meta property="og:image" content="https://cdn.example.com/img.jpg">'
            '<meta property="og:video" content="https://cdn.example.com/v.mp4">')
    with unittest.mock.patch.object(
            em, "fetch_page",
            return_value=("https://example.com/story", html)):
        changed = em.enrich_story(story, candidates)
    assert changed is True
    assert story["image"]["url"] == "https://cdn.example.com/img.jpg"
    assert story["video"]["url"] == "https://cdn.example.com/v.mp4"
    print("  PASS: test_enrich_story_attaches_og_media")


def test_enrich_story_skips_existing_media():
    story = {"source_url": "https://example.com/story", "title": "Story",
             "image": {"url": "https://cdn.example.com/existing.jpg"}}
    with unittest.mock.patch.object(em, "fetch_page") as mock_fetch:
        changed = em.enrich_story(story, [])
    assert changed is False
    mock_fetch.assert_not_called()
    print("  PASS: test_enrich_story_skips_existing_media")


def test_enrich_story_rejects_private_og_url():
    story = {"source_url": "https://example.com/story", "title": "Story"}
    candidates = [{"url": "https://example.com/story", "title": "Story"}]
    html = '<meta property="og:image" content="http://192.168.1.5/x.jpg">'
    with unittest.mock.patch.object(
            em, "fetch_page",
            return_value=("https://example.com/story", html)):
        changed = em.enrich_story(story, candidates)
    assert changed is False
    assert "image" not in story
    print("  PASS: test_enrich_story_rejects_private_og_url")


def test_enrich_story_rejects_hostless_url():
    story = {"source_url": "http://", "title": "Story"}
    with unittest.mock.patch.object(em, "fetch_page") as mock_fetch:
        changed = em.enrich_story(story, [])
    assert changed is False
    mock_fetch.assert_not_called()
    print("  PASS: test_enrich_story_rejects_hostless_url")


def test_enrich_story_rejects_private_candidate_url():
    story = {"source_url": "http://192.168.1.5/story", "title": "Story"}
    with unittest.mock.patch.object(em, "fetch_page") as mock_fetch:
        changed = em.enrich_story(story, [])
    assert changed is False
    mock_fetch.assert_not_called()
    print("  PASS: test_enrich_story_rejects_private_candidate_url")


def test_enrich_story_rejects_private_redirect():
    story = {"source_url": "https://example.com/story", "title": "Story"}
    candidates = [{"url": "https://example.com/story", "title": "Story"}]
    html = '<meta property="og:image" content="https://cdn.example.com/img.jpg">'
    # fetch_page returns a private final URL (simulating a redirect to a
    # private host); the resolved final URL must be rejected.
    with unittest.mock.patch.object(
            em, "fetch_page",
            return_value=("http://169.254.169.254/latest/meta-data/", html)):
        changed = em.enrich_story(story, candidates)
    assert changed is False
    assert "image" not in story
    print("  PASS: test_enrich_story_rejects_private_redirect")


# ---------------------------------------------------------------------------
# enrich_media.load_stories / save_stories structure preservation
# ---------------------------------------------------------------------------

def test_load_save_wrapped_dict_roundtrip():
    d = tempfile.mkdtemp(prefix="media-rt-")
    path = os.path.join(d, "stories.json")
    original = {"stories": [{"title": "A", "source_url": "https://example.com/a"}],
                "extra_key": "keep-me", "digest_time": "morning"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(original, f)
    stories, is_wrapped = em.load_stories(path)
    assert is_wrapped is True
    assert stories == original["stories"]
    stories[0]["image"] = {"url": "https://cdn.example.com/i.jpg", "source": "og"}
    em.save_stories(path, stories, is_wrapped, dict(original))
    with open(path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["extra_key"] == "keep-me"
    assert saved["digest_time"] == "morning"
    assert saved["stories"][0]["image"]["url"] == "https://cdn.example.com/i.jpg"
    print("  PASS: test_load_save_wrapped_dict_roundtrip")


def test_load_save_plain_list_roundtrip():
    d = tempfile.mkdtemp(prefix="media-rt-")
    path = os.path.join(d, "stories.json")
    original = [{"title": "A", "source_url": "https://example.com/a"}]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(original, f)
    stories, is_wrapped = em.load_stories(path)
    assert is_wrapped is False
    em.save_stories(path, stories, is_wrapped, None)
    with open(path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert isinstance(saved, list)
    assert saved == original
    print("  PASS: test_load_save_plain_list_roundtrip")


# ---------------------------------------------------------------------------
# decode_google_news_url tests
# ---------------------------------------------------------------------------

# A Google News URL whose protobuf encodes a plaintext article URL in an
# older (non-encrypted) format: field 1 varint(19), field 2 string(URL).
_GN_DECODED_ARTICLE = "https://www.bbc.com/news/world-asia-68012345"
_GN_ENCODED_URL = (
    "https://news.google.com/rss/articles/"
    "CBMSLGh0dHBzOi8vd3d3LmJiYy5jb20vbmV3cy93b3JsZC1hc2lhLTY4MDEyMzQ1GgA"
)


def test_decode_google_news_url_real_protobuf():
    """Decode a Google News URL with a plaintext article URL inside."""
    result = em.decode_google_news_url(_GN_ENCODED_URL)
    assert result == _GN_DECODED_ARTICLE, result
    print("  PASS: test_decode_google_news_url_real_protobuf")


def test_decode_google_news_url_non_google_passthrough():
    """Non-Google-News URLs are returned unchanged."""
    url = "https://www.bbc.com/news/world-12345"
    assert em.decode_google_news_url(url) == url
    print("  PASS: test_decode_google_news_url_non_google_passthrough")


def test_decode_google_news_url_malformed():
    """Malformed / truncated Google News URL returns the original URL."""
    truncated = "https://news.google.com/rss/articles/AAAA"
    with unittest.mock.patch.object(
            em, "_decode_google_news_batchexecute", return_value=None):
        assert em.decode_google_news_url(truncated) == truncated
    print("  PASS: test_decode_google_news_url_malformed")


def test_decode_google_news_url_resolves_to_http():
    """A successfully decoded Google News URL must start with http:// or https://."""
    result = em.decode_google_news_url(_GN_ENCODED_URL)
    assert result.startswith("http://") or result.startswith("https://"), result
    # Verify it contains the actual news site domain.
    assert "bbc.com" in result, result
    print("  PASS: test_decode_google_news_url_resolves_to_http")


def test_decode_google_news_url_encrypted_format_returns_original():
    """Newer encrypted-format Google News URLs return the original URL when
    the batchexecute decode fails (no network in tests)."""
    encrypted_url = (
        "https://news.google.com/rss/articles/"
        "CBMiekFVX3lxTE9ocXFaN1VYLTc2X01QV1dQb3MwRHZRd0hvZ1Q5b0ZFbFdOcF9I"
        "MTNXWUUtbE50R2RpYkNzWXZpQURqUUpfMFlaYTJHenBrcDM2QW5CZG1aRE0yMlM2"
        "N0ZKWjNnd0hOV2VLUldZOW9oNFpJQzMtYkxVNUxR?oc=5"
    )
    with unittest.mock.patch.object(
            em, "_decode_google_news_batchexecute", return_value=None):
        result = em.decode_google_news_url(encrypted_url)
    assert result == encrypted_url, result
    print("  PASS: test_decode_google_news_url_encrypted_format_returns_original")


def test_decode_google_news_url_strips_query_params():
    """Query params like ?oc=5 are stripped before decoding."""
    url_with_params = _GN_ENCODED_URL + "?oc=5"
    result = em.decode_google_news_url(url_with_params)
    assert result == _GN_DECODED_ARTICLE, result
    print("  PASS: test_decode_google_news_url_strips_query_params")


def test_decode_google_news_url_empty_and_none():
    """Empty and None inputs return their input unchanged."""
    assert em.decode_google_news_url("") == ""
    assert em.decode_google_news_url(None) is None
    print("  PASS: test_decode_google_news_url_empty_and_none")


# A current-generation (encrypted) Google News redirect URL. The offline
# base64-scan cannot decode it, so it exercises the batchexecute path.
_GN_ENCRYPTED_URL = (
    "https://news.google.com/rss/articles/"
    "CBMiekFVX3lxTE9ocXFaN1VYLTc2X01QV1dQb3MwRHZRd0hvZ1Q5b0ZFbFdOcF9I"
    "MTNXWUUtbE50R2RpYkNzWXZpQURqUUpfMFlaYTJHenBrcDM2QW5CZG1aRE0yMlM2"
    "N0ZKWjNnd0hOV2VLUldZOW9oNFpJQzMtYkxVNUxR?oc=5"
)


def test_decode_google_news_url_non_google_no_network():
    """Non-Google-News URLs pass through unchanged with no network call."""
    url = "https://www.bbc.com/news/world-12345"
    with unittest.mock.patch.object(
            em, "_decode_google_news_batchexecute") as mock_batch:
        result = em.decode_google_news_url(url)
    assert result == url
    mock_batch.assert_not_called()
    print("  PASS: test_decode_google_news_url_non_google_no_network")


def test_decode_google_news_url_batchexecute_failure_returns_original():
    """A Google News URL whose batchexecute decode fails returns the original."""
    with unittest.mock.patch.object(
            em, "_decode_google_news_batchexecute", return_value=None):
        result = em.decode_google_news_url(_GN_ENCRYPTED_URL)
    assert result == _GN_ENCRYPTED_URL, result
    print("  PASS: test_decode_google_news_url_batchexecute_failure_returns_original")


def test_decode_google_news_url_batchexecute_success():
    """A Google News URL that decodes successfully returns the real article URL."""
    real_url = "https://www.bbc.com/news/world-asia-68012345"
    with unittest.mock.patch.object(
            em, "_decode_google_news_batchexecute", return_value=real_url):
        result = em.decode_google_news_url(_GN_ENCRYPTED_URL)
    assert result == real_url, result
    print("  PASS: test_decode_google_news_url_batchexecute_success")


class _FakeResp:
    """Minimal urllib response stand-in (context manager + read())."""
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _fake_urlopen(page_html, resp_body):
    """Return a urlopen side_effect that serves the page GET then the POST."""
    def fake(req, timeout=None):
        if req.full_url.startswith("https://news.google.com/rss/articles/"):
            return _FakeResp(page_html.encode("utf-8"))
        return _FakeResp(resp_body.encode("utf-8"))
    return fake


def test_decode_google_news_batchexecute_parses_response():
    """The batchexecute response parser recovers the real article URL."""
    real_url = "https://www.bbc.com/news/world-asia-68012345"
    page_html = ('<html><body><c-wiz data-n-a-sg="AESabc123" '
                 'data-n-a-ts="1720000000"></c-wiz></body></html>')
    inner = json.dumps(["garturlres", real_url])
    resp_body = ")]}'\n\n" + json.dumps([
        ["wrb.fr", "Fbv4je", inner, None, None, [3], ""],
        ["di", 16],
        ["af.httprm", 16, "x", 9],
    ])
    with unittest.mock.patch.object(em.time, "sleep"), \
         unittest.mock.patch.object(
             em.urllib.request, "urlopen",
             side_effect=_fake_urlopen(page_html, resp_body)):
        result = em._decode_google_news_batchexecute(_GN_ENCRYPTED_URL)
    assert result == real_url, result
    print("  PASS: test_decode_google_news_batchexecute_parses_response")


def test_decode_google_news_batchexecute_null_payload_returns_none():
    """A 200 response with a null payload (Google can't resolve) returns None."""
    page_html = ('<html><body><c-wiz data-n-a-sg="AESabc123" '
                 'data-n-a-ts="1720000000"></c-wiz></body></html>')
    resp_body = ")]}'\n\n" + json.dumps([
        ["wrb.fr", "Fbv4je", None, None, None, [3], ""],
        ["di", 16],
    ])
    with unittest.mock.patch.object(em.time, "sleep"), \
         unittest.mock.patch.object(
             em.urllib.request, "urlopen",
             side_effect=_fake_urlopen(page_html, resp_body)):
        result = em._decode_google_news_batchexecute(_GN_ENCRYPTED_URL)
    assert result is None, result
    print("  PASS: test_decode_google_news_batchexecute_null_payload_returns_none")


def test_decode_google_news_batchexecute_missing_params_returns_none():
    """A page without data-n-a-sg/data-n-a-ts (consent/CAPTCHA) returns None."""
    page_html = '<html><body><c-wiz></c-wiz></body></html>'
    with unittest.mock.patch.object(em.time, "sleep"), \
         unittest.mock.patch.object(
             em.urllib.request, "urlopen",
             side_effect=_fake_urlopen(page_html, ")]}'\n\n[]")):
        result = em._decode_google_news_batchexecute(_GN_ENCRYPTED_URL)
    assert result is None, result
    print("  PASS: test_decode_google_news_batchexecute_missing_params_returns_none")


def test_enrich_story_decodes_google_news_url():
    """enrich_story decodes Google News URL before fetching and updates source_url."""
    story = {"source_url": _GN_ENCODED_URL, "title": "Test"}
    candidates = [{"url": _GN_ENCODED_URL, "title": "Test"}]
    html = '<meta property="og:image" content="https://cdn.example.com/img.jpg">'
    with unittest.mock.patch.object(
            em, "fetch_page",
            return_value=(_GN_DECODED_ARTICLE, html)):
        changed = em.enrich_story(story, candidates)
    assert changed is True
    assert story["source_url"] == _GN_DECODED_ARTICLE
    assert story["image"]["url"] == "https://cdn.example.com/img.jpg"
    print("  PASS: test_enrich_story_decodes_google_news_url")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_extract_media_content_image,
    test_extract_media_enclosure_video,
    test_extract_media_inline_img,
    test_extract_media_no_media,
    test_extract_media_itunes,
    test_extract_media_thumbnail,
    test_extract_media_rejects_non_http,
    test_rss_task_emits_media_fields,
    test_validator_keeps_valid_media,
    test_validator_drops_invented_media_keeps_story,
    test_validator_drops_bad_scheme_and_private_host,
    test_validator_no_media_unchanged,
    test_validator_null_media_normalized_to_absent,
    test_parse_og_meta_image_present,
    test_parse_og_meta_image_absent,
    test_parse_og_meta_twitter_image_fallback,
    test_parse_og_meta_og_image_url_fallback,
    test_parse_og_meta_relative_url_resolution,
    test_parse_og_meta_protocol_relative_url_resolution,
    test_parse_og_meta_video_with_type,
    test_parse_og_meta_rejects_non_http,
    test_parse_og_meta_malformed_html_no_crash,
    test_find_candidate_media_exact_url,
    test_find_candidate_media_normalized_url,
    test_find_candidate_media_title_sequence,
    test_find_candidate_media_title_jaccard,
    test_find_candidate_media_empty_fallback,
    test_find_candidate_exact_url,
    test_find_candidate_normalized_url,
    test_find_candidate_title_match,
    test_find_candidate_no_match,
    test_http_url_rejects_hostless,
    test_parse_og_meta_image_priority_over_document_order,
    test_parse_og_meta_secure_url_variants,
    test_parse_og_meta_rejects_private_host,
    test_parse_og_meta_rejects_lh3_googleusercontent,
    test_parse_og_meta_rejects_docs_google,
    test_parse_og_meta_accepts_legit_news_host,
    test_parse_og_meta_rejects_placehold_co,
    test_parse_og_meta_blocklist_case_insensitive,
    test_parse_og_meta_blocklist_subdomain_match,
    test_parse_og_meta_blocklist_applies_to_video,
    test_enrich_story_attaches_og_media,
    test_enrich_story_skips_existing_media,
    test_enrich_story_rejects_private_og_url,
    test_enrich_story_rejects_hostless_url,
    test_enrich_story_rejects_private_candidate_url,
    test_enrich_story_rejects_private_redirect,
    test_load_save_wrapped_dict_roundtrip,
    test_load_save_plain_list_roundtrip,
    test_decode_google_news_url_real_protobuf,
    test_decode_google_news_url_non_google_passthrough,
    test_decode_google_news_url_malformed,
    test_decode_google_news_url_resolves_to_http,
    test_decode_google_news_url_encrypted_format_returns_original,
    test_decode_google_news_url_strips_query_params,
    test_decode_google_news_url_empty_and_none,
    test_decode_google_news_url_non_google_no_network,
    test_decode_google_news_url_batchexecute_failure_returns_original,
    test_decode_google_news_url_batchexecute_success,
    test_decode_google_news_batchexecute_parses_response,
    test_decode_google_news_batchexecute_null_payload_returns_none,
    test_decode_google_news_batchexecute_missing_params_returns_none,
    test_enrich_story_decodes_google_news_url,
]

if __name__ == "__main__":
    print("Running media extraction tests...\n")
    passed = 0
    failed = 0
    for fn in ALL_TESTS:
        try:
            fn()
            passed += 1
        except Exception as exc:
            print("  FAIL: %s: %s" % (fn.__name__, exc))
            failed += 1

    print("\n%d passed, %d failed" % (passed, failed))
    if failed:
        sys.exit(1)
