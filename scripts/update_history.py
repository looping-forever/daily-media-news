#!/usr/bin/env python3
"""update_history.py — merge surviving digest stories into state/history.json.

For each story in --stories (the validated payload):
  - build entry {url_normalized, title_hash, title, category, date_sent}
    where date_sent = today in Asia/Kathmandu (UTC+5:45)
  - dedupe-merge against existing entries (same url_normalized or title_hash)
  - prune entries older than 7 days
Write atomically (tmp file + os.replace). Print counts.

Reads legacy history entries ({title,url,date}) transparently and rewrites
them in the current format. Stdlib only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    import story_state
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import story_state

HISTORY_TTL_DAYS = 7
KATHMANDU_OFFSET = timedelta(hours=5, minutes=45)


def kathmandu_today():
    """Current date in Asia/Kathmandu (UTC+5:45).

    Equivalent to datetime.utcnow() + timedelta(hours=5, minutes=45) but without
    the Python 3.12+ DeprecationWarning on utcnow().
    """
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now_utc_naive + KATHMANDU_OFFSET).date()


def normalize_url(url):
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", u)
    if u.startswith("www."):
        u = u[4:]
    u = u.split("#", 1)[0]
    u = u.split("?", 1)[0]
    return u.rstrip("/")


def title_hash(title):
    return hashlib.sha1((title or "").strip().lower().encode("utf-8")).hexdigest()


def parse_date(value):
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def load_history(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"stories": []}
    if not isinstance(data, dict) or not isinstance(data.get("stories"), list):
        return {"stories": []}
    return data


def normalize_entry(entry):
    """Coerce a legacy or current history entry into the current format."""
    if not isinstance(entry, dict):
        return None
    title = entry.get("title") or ""
    url_norm = entry.get("url_normalized") or normalize_url(entry.get("url", ""))
    thash = entry.get("title_hash") or (title_hash(title) if title else "")
    date_sent = parse_date(entry.get("date_sent")) or parse_date(entry.get("date"))
    category = entry.get("category") if isinstance(entry.get("category"), str) else ""
    return {
        "url_normalized": url_norm,
        "title_hash": thash,
        "title": title,
        "category": category,
        "date_sent": date_sent.isoformat() if date_sent else kathmandu_today().isoformat(),
    }


def atomic_write_json(path, payload):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Update digest history with sent stories.")
    ap.add_argument("--stories", dest="stories_path", default="state/stories.validated.json")
    ap.add_argument("--history", dest="history_path", default="state/history.json")
    ap.add_argument("--state", dest="state_path", default=None,
                    help="optional active_stories.json; when set, also mark reported events")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.isfile(args.stories_path):
        print("FATAL: stories file not found: %s" % args.stories_path)
        return 1
    try:
        with open(args.stories_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print("FATAL: cannot parse stories JSON (%s): %s" % (args.stories_path, exc))
        return 1

    survivors = data.get("stories") if isinstance(data, dict) else None
    if not isinstance(survivors, list):
        print("FATAL: stories file has no 'stories' list")
        return 1

    digest_time = data.get("digest_time") if isinstance(data.get("digest_time"), str) else ""
    if args.state_path and digest_time not in ("morning", "evening"):
        print("FATAL: --state requires payload digest_time to be 'morning' or 'evening' (got %r)"
              % digest_time)
        return 1

    today = kathmandu_today()
    raw_history = load_history(args.history_path)

    # Normalize existing entries; keep only those within TTL.
    kept_entries = []
    pruned = 0
    for raw in raw_history["stories"]:
        entry = normalize_entry(raw)
        if entry is None:
            continue
        age = today - (parse_date(entry["date_sent"]) or today)
        if age.days > HISTORY_TTL_DAYS:
            pruned += 1
            continue
        kept_entries.append(entry)

    seen_urls = {e["url_normalized"] for e in kept_entries if e["url_normalized"]}
    seen_hashes = {e["title_hash"] for e in kept_entries if e["title_hash"]}

    added = 0
    duplicates_skipped = 0
    date_sent = today.isoformat()
    for story in survivors:
        if not isinstance(story, dict):
            continue
        url_norm = normalize_url(story.get("source_url", ""))
        thash = title_hash(story.get("title", ""))
        if (url_norm and url_norm in seen_urls) or (thash and thash in seen_hashes):
            duplicates_skipped += 1
            continue
        entry = {
            "url_normalized": url_norm,
            "title_hash": thash,
            "title": story.get("title", ""),
            "category": story.get("category", ""),
            "date_sent": date_sent,
        }
        kept_entries.append(entry)
        if url_norm:
            seen_urls.add(url_norm)
        if thash:
            seen_hashes.add(thash)
        added += 1

    out_dir = os.path.dirname(os.path.abspath(args.history_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    atomic_write_json(args.history_path, {"stories": kept_entries})

    if args.state_path:
        try:
            state = story_state.load_state(args.state_path)
        except story_state.StateCorruptionError as exc:
            print("FATAL: %s" % exc)
            return 1
        event_ids = [s.get("event_id") for s in survivors
                     if isinstance(s, dict) and s.get("event_id")]
        marked = story_state.mark_reported(state, event_ids, digest_time, today)
        try:
            story_state.save_state_atomic(args.state_path, state)
        except OSError as exc:
            print("FATAL: cannot save active-story state (%s): %s" % (args.state_path, exc))
            return 1
        print("  state marked:      %d -> %s" % (len(marked), args.state_path))

    print("update_history:")
    print("  input stories:     %d" % len(survivors))
    print("  added:             %d" % added)
    print("  duplicates skipped:%d" % duplicates_skipped)
    print("  pruned (>7d):      %d" % pruned)
    print("  total entries:     %d -> %s" % (len(kept_entries), args.history_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
