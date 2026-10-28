"""derive_unsummarised.py -- the no-AI fallback edition.

The collector's candidates ARE the edition when no model is available: title,
link, host and time, with `summary` empty and `for_you` absent. Nothing here
invents a sentence: an unsummarised edition is labelled as one on every surface.

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

MIN_STORIES = 5
PER_CATEGORY = 3
MAX_PRIORITY = 7
NEAR_DUP = 0.5
MIN_TITLE_CHARS = 30
FRESH_HOURS = 12

NOTE = ("Unsummarised edition: no AI was available for this build, so these are "
        "collected headlines with no summary.")


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


def derive(candidates, now=None):
    """(stories, per-category counts) in the collector's freshest-first order."""
    now = now or datetime.now(timezone.utc)
    stories, counts, seen = [], {}, []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        title = str(cand.get("title") or "").strip()
        category = str(cand.get("domain_category") or "").strip()
        url = str(cand.get("url") or "").strip()
        if not title or not category or not url.startswith("http"):
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
            "summary": "",
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
    return stories, counts


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

    stories, counts = derive(candidates)
    for category in sorted(counts):
        print("  %-24s %d" % (category, counts[category]))
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
