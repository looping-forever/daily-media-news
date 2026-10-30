"""derive_unsummarised.py -- the no-AI fallback edition.

The collector's candidates ARE the edition when no model is available: title,
link, host and time, plus the PUBLISHER'S OWN description when the feed carried
one (trimmed to one paragraph). Nothing here invents a sentence and nothing is
summarised: the text under a fallback headline is quoted feed text, and the
edition is labelled as unsummarised on every surface.

Priority is capped at 7 because daily-news.yml treats `priority >= 8` as "needs
the owner" and escalates the push; a fallback must never page like a real one.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The SAME tier-1 list the breaking path trusts, never a second copy of it.
from breaking_monitor import is_tier1_domain  # noqa: E402
# And the SAME category vocabulary the validator enforces. The collector emits
# candidates the digest schema does not accept -- `weather_forecast` is one, and
# it is in state/candidates.json on every run -- and validate_stories DROPS such
# a story as "schema: invalid category". Emitting one would spend a story slot on
# something that is certain to be thrown away, so the floor could pass here and
# the page still ship fewer stories than this file claimed.
from validate_stories import VALID_CATEGORIES  # noqa: E402
# And the SAME three text gates the validator will re-run on this file's output.
# Quoting a publisher's description adds words that never used to reach them, so
# the fallback applies them FIRST: a story whose description would be vetoed
# keeps its bare headline (the old behaviour) instead of being dropped as a
# whole and shrinking an edition that already has no model behind it.
from validate_stories import (crime_violation, load_excluded_keywords,  # noqa: E402
                              spoiler_violation)

MIN_STORIES = 5
PER_CATEGORY = 3
MAX_PRIORITY = 7
NEAR_DUP = 0.5
MIN_TITLE_CHARS = 30
FRESH_HOURS = 12
#: A description shorter than this says nothing, and longer than this needs
#: cutting: the fallback is a headline with a lead-in, not an article.
SUMMARY_MIN_CHARS = 24
SUMMARY_MAX_CHARS = 240

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "config")

NOTE = ("Unsummarised edition: no AI was available for this build, so the text "
        "under each headline is the publisher's own description, not a summary "
        "written for you.")


def title_tokens(title):
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) > 3}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    union = len(a | b)
    return 0.0 if union == 0 else len(a & b) / union


def host_of(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def parse_published(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def priority_for(host, published, now):
    priority = 6 if is_tier1_domain(host) else 5
    moment = parse_published(published)
    if moment is not None and now - moment <= timedelta(hours=FRESH_HOURS):
        priority += 1
    return min(priority, MAX_PRIORITY)


#: Loaded once, lowercased, exactly as validate_stories will match it.
EXCLUDED_KEYWORDS = tuple(
    k.lower() for k in load_excluded_keywords(CONFIG_DIR) if str(k).strip())


def lead_text(snippet, title, category):
    """The publisher's own description, trimmed; "" when it adds nothing.

    VERBATIM, never rewritten -- this file still writes no prose of its own.
    Three rules, each of which returns "" rather than a weaker string:

      * shorter than SUMMARY_MIN_CHARS says nothing a headline does not;
      * a description that opens by repeating the headline is dead weight, so
        the repeat is removed and the remainder judged again;
      * text the validator would VETO is dropped here first. Quoting a
        description adds words that never used to reach those gates, and the
        floor must not lose a story to text it added itself.
    """
    text = re.sub(r"\s+", " ", str(snippet or "")).strip()
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    if title_text and text.lower().startswith(title_text.lower()):
        text = text[len(title_text):].lstrip(" -\u2013\u2014:.")
    if len(text) < SUMMARY_MIN_CHARS:
        return ""
    if len(text) > SUMMARY_MAX_CHARS:
        cut = text.rfind(" ", 0, SUMMARY_MAX_CHARS)
        text = text[:cut if cut > SUMMARY_MIN_CHARS else SUMMARY_MAX_CHARS]
        text = text.rstrip(" ,;:-") + "\u2026"
    low = text.lower()
    if any(k in low for k in EXCLUDED_KEYWORDS):
        return ""
    if spoiler_violation(category, "none", text) or crime_violation(text):
        return ""
    return text


def derive(candidates, now=None):
    """(stories, per-category counts, skipped-category counts).

    `skipped` is printed by main rather than swallowed: a candidate the schema
    will not accept is a candidate the collector spent a source on for nothing,
    and the count is the evidence for that.
    """
    now = now or datetime.now(timezone.utc)
    stories, counts, skipped, seen = [], {}, {}, []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        title = str(cand.get("title") or "").strip()
        category = str(cand.get("domain_category") or "").strip()
        url = str(cand.get("url") or "").strip()
        if not title or not category or not url.startswith("http"):
            continue
        if category not in VALID_CATEGORIES:
            skipped[category] = skipped.get(category, 0) + 1
            continue
        if len(title) < MIN_TITLE_CHARS:
            continue
        if counts.get(category, 0) >= PER_CATEGORY:
            continue
        tokens = title_tokens(title)
        if any(jaccard(tokens, prev) >= NEAR_DUP for prev in seen):
            continue
        host = host_of(url)
        published = cand.get("published")
        story = {
            "title": title,
            "summary": lead_text(cand.get("snippet"), title, category),
            "source_name": host,
            "source_url": url,
            "published_at": published if isinstance(published, str) and published.strip() else None,
            "category": category,
            "language": str(cand.get("language") or "en"),
            "reliability": "tier2" if is_tier1_domain(host) else "tier3",
            "priority": priority_for(host, published, now),
            "spoiler_level": "none",
            "spoiler_reason": "",
            "relevance_reason": "",
            "supporting_sources": [],
            "personal_relevance": None,
        }
        if isinstance(cand.get("image"), dict):
            story["image"] = cand["image"]
        if isinstance(cand.get("video"), dict):
            story["video"] = cand["video"]
        stories.append(story)
        counts[category] = counts.get(category, 0) + 1
        seen.append(tokens)
    return stories, counts, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Derive the no-AI fallback edition.")
    ap.add_argument("--candidates", default="state/candidates.json")
    ap.add_argument("--out", default="state/stories.json")
    ap.add_argument("--today", default="")
    ap.add_argument("--slot", default="")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.candidates):
        print("FATAL: candidates file not found: %s" % args.candidates)
        return 1
    try:
        with open(args.candidates, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("FATAL: cannot parse candidates JSON: %s" % exc)
        return 1
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        print("FATAL: candidates JSON has no 'candidates' list")
        return 1

    stories, counts, skipped = derive(candidates)
    for category in sorted(counts):
        print("  %-24s %d" % (category, counts[category]))
    for category in sorted(skipped):
        print("  %-24s skipped %d (not a digest category)"
              % (category, skipped[category]))
    print("derive_unsummarised: %d stories from %d candidates"
          % (len(stories), len(candidates)))

    if len(stories) < MIN_STORIES:
        # Fewer than the floor means the empty/degraded path owns this run.
        print("FLOOR: only %d stories (minimum %d); nothing written to %s"
              % (len(stories), MIN_STORIES, args.out))
        return 0

    payload = {
        "date": args.today,
        "edition_kind": "unsummarised",
        "notes": NOTE,
        "stories": stories,
    }
    if args.slot:
        payload["digest_time"] = args.slot

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, args.out)
    print("wrote %s (%d stories, edition_kind=unsummarised)"
          % (args.out, len(stories)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
