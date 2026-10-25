#!/usr/bin/env python3
"""story_state.py — persistent active-story state for the daily media digest.

Maintains state/active_stories.json:
  {"version": 1,
   "stories": [{event_id, canonical_title, category, first_seen, last_seen,
                first_reported, last_reported, importance, personal_relevance,
                sources, source_count, reliability, status, known_facts,
                latest_development, alerted, alert_count, last_alerted,
                in_morning, in_evening, deadline, effective_date, updated_at}],
   "archived": [...]}

statuses: NEW | DEVELOPING | UPDATED | STABLE | RESOLVED | STALE

Library API:
  load_state(path)                 missing file -> fresh state; corrupt -> StateCorruptionError
  save_state_atomic(path, data)    tmp file + os.replace
  normalize_url(url)               canonical URL for dedupe
  title_tokens(s), jaccard(a, b)   token-set Jaccard similarity
  match_event(story, stories)      event_id if >=2 merge signals, else None
  upsert_sighting(state, story, today)   -> (event_id, label)
  mark_reported(state, event_ids, digest_time, today)
  expire(state, today)             deadline/stale/archive transitions

CLI:
  python scripts/story_state.py sightings --stories FILE --state FILE
  python scripts/story_state.py mark-reported --stories FILE --state FILE --digest-time {morning,evening}
  python scripts/story_state.py expire --state FILE

Corrupt state files are never auto-reset: the CLI reports on stderr and exits 3.
Stdlib only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

STATE_VERSION = 1
STATUSES = ("NEW", "DEVELOPING", "UPDATED", "STABLE", "RESOLVED", "STALE")
MERGE_TITLE_JACCARD = 0.6
MATERIAL_CHANGE_JACCARD = 0.85
STALE_AFTER_DAYS = 7
ARCHIVE_AFTER_DAYS = 30
ARCHIVE_CAP = 200
KATHMANDU_OFFSET = timedelta(hours=5, minutes=45)


class StateCorruptionError(Exception):
    """Raised when an existing state file cannot be trusted."""

    def __init__(self, path, reason=""):
        self.path = path
        self.reason = reason
        msg = "corrupt state file: %s" % path
        if reason:
            msg += " (%s)" % reason
        super().__init__(msg)


def kathmandu_today():
    """Current date in Asia/Kathmandu (UTC+5:45)."""
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now_utc_naive + KATHMANDU_OFFSET).date()


def kathmandu_now_iso():
    """Current timestamp in Asia/Kathmandu as an ISO-8601 string."""
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now_utc_naive + KATHMANDU_OFFSET).strftime("%Y-%m-%dT%H:%M:%S+05:45")


def normalize_url(url):
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", u)
    if u.startswith("www."):
        u = u[4:]
    u = u.split("#", 1)[0]
    u = u.split("?", 1)[0]
    return u.rstrip("/")


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def title_tokens(s):
    """Lowercased word-token set (>3 chars) of a title/summary string.

    The length filter matches validate_stories.title_tokens and
    collect_news.title_tokens so Jaccard thresholds are equivalent across
    modules."""
    return {t for t in _TOKEN_RE.findall((s or "").lower()) if len(t) > 3}


def jaccard(a, b):
    """Jaccard similarity of two token sets (strings are tokenized first)."""
    if isinstance(a, str):
        a = title_tokens(a)
    if isinstance(b, str):
        b = title_tokens(b)
    a, b = set(a), set(b)
    union = a | b
    if not union:
        return 0.0
    return float(len(a & b)) / float(len(union))


def parse_date(value):
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _coerce_today(today):
    if isinstance(today, date):
        return today
    return parse_date(today) or kathmandu_today()


def _story_title(story):
    t = story.get("canonical_title") or story.get("title") or ""
    return (t or "").strip()


def _story_urls(story):
    urls = set()
    for key in ("source_url", "url"):
        v = story.get(key)
        if isinstance(v, str) and v.strip():
            urls.add(normalize_url(v))
    for v in story.get("sources") or []:
        if isinstance(v, str) and v.strip():
            urls.add(normalize_url(v))
    urls.discard("")
    return urls


def load_state(path):
    """Load state JSON; missing file -> fresh state, corrupt file -> raise."""
    if not os.path.exists(path):
        return {"version": STATE_VERSION, "stories": [], "archived": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as exc:
        raise StateCorruptionError(path, str(exc))
    if not isinstance(data, dict) or not isinstance(data.get("stories"), list):
        raise StateCorruptionError(path, "missing 'stories' list")
    data.setdefault("version", STATE_VERSION)
    if not isinstance(data.get("archived"), list):
        data["archived"] = []
    return data


def save_state_atomic(path, data):
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def match_event(story, stories):
    """Return the event_id of the first story matching on >=2 signals, else None.

    Signals: identical non-null event_key; normalized-URL overlap;
    title Jaccard >= 0.6 vs canonical_title.
    """
    if not isinstance(story, dict):
        return None
    tokens_a = title_tokens(_story_title(story))
    urls_a = _story_urls(story)
    key_a = story.get("event_key")
    for s in stories or []:
        if not isinstance(s, dict):
            continue
        signals = 0
        key_b = s.get("event_key")
        if key_a and key_b and key_a == key_b:
            signals += 1
        if urls_a and (urls_a & _story_urls(s)):
            signals += 1
        if tokens_a and jaccard(tokens_a, title_tokens(s.get("canonical_title") or "")) >= MERGE_TITLE_JACCARD:
            signals += 1
        if signals >= 2:
            return s.get("event_id")
    return None


def _new_event_id(canonical_title, category):
    basis = "%s|%s" % ((category or "").strip().lower(), canonical_title.strip().lower())
    return "evt-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _blank_event(canonical_title, category, today_iso, now_iso):
    return {
        "event_id": _new_event_id(canonical_title, category),
        "event_key": None,
        "canonical_title": canonical_title,
        "category": category or "",
        "first_seen": today_iso,
        "last_seen": today_iso,
        "first_reported": None,
        "last_reported": None,
        "importance": 0,
        "personal_relevance": 0,
        "sources": [],
        "source_count": 0,
        "reliability": "",
        "status": "NEW",
        "known_facts": "",
        "latest_development": "",
        "alerted": False,
        "alert_count": 0,
        "last_alerted": None,
        "in_morning": None,
        "in_evening": None,
        "deadline": None,
        "effective_date": None,
        "updated_at": now_iso,
    }


def upsert_sighting(state, story, today):
    """Create or update an event from one story sighting.

    Returns (event_id, label). Labels:
      NEW        event created by this sighting (first_reported necessarily null)
      UPDATED    existing tracked event with materially changed facts
                 (summary Jaccard < 0.85 vs stored known_facts); status -> DEVELOPING
      DEVELOPING seen again within ~24h of previous last_seen, no material change
      ONGOING    seen again later than ~24h, no material change
    A sighting also revives RESOLVED/STALE events back to DEVELOPING.
    """
    if not isinstance(story, dict):
        raise TypeError("story must be a dict")
    today_d = _coerce_today(today)
    today_iso = today_d.isoformat()
    now_iso = kathmandu_now_iso()
    canonical_title = _story_title(story)
    urls = _story_urls(story)
    known_facts = (story.get("known_facts") or story.get("summary") or "").strip()

    eid = match_event(story, state.get("stories", []))
    ev = None
    if eid is not None:
        ev = next((s for s in state["stories"]
                   if isinstance(s, dict) and s.get("event_id") == eid), None)

    if ev is None:
        ev = _blank_event(canonical_title, story.get("category") or "", today_iso, now_iso)
        if story.get("event_key"):
            ev["event_key"] = story["event_key"]
        ev["sources"] = sorted(urls)
        ev["source_count"] = len(ev["sources"])
        for key in ("importance", "personal_relevance", "reliability"):
            if story.get(key) is not None:
                ev[key] = story[key]
        ev["known_facts"] = known_facts
        ev["latest_development"] = (story.get("latest_development") or known_facts).strip()
        if story.get("deadline"):
            ev["deadline"] = story["deadline"]
        if story.get("effective_date"):
            ev["effective_date"] = story["effective_date"]
        state.setdefault("stories", []).append(ev)
        return ev["event_id"], "NEW"

    prev_last_seen = parse_date(ev.get("last_seen"))
    merged_urls = set(ev.get("sources") or []) | urls
    ev["sources"] = sorted(merged_urls)
    ev["source_count"] = len(ev["sources"])
    ev["last_seen"] = today_iso
    if story.get("event_key"):
        ev["event_key"] = story["event_key"]
    if story.get("category"):
        ev["category"] = story["category"]
    for key in ("importance", "personal_relevance", "reliability"):
        if story.get(key) is not None:
            ev[key] = story[key]
    if story.get("deadline"):
        ev["deadline"] = story["deadline"]
    if story.get("effective_date"):
        ev["effective_date"] = story["effective_date"]

    material = bool(known_facts) and jaccard(known_facts, ev.get("known_facts") or "") < MATERIAL_CHANGE_JACCARD
    revived = ev.get("status") in ("RESOLVED", "STALE")
    if material:
        ev["known_facts"] = known_facts
        ev["latest_development"] = (story.get("latest_development") or known_facts).strip()
    if material or revived:
        ev["status"] = "DEVELOPING"

    if material:
        label = "UPDATED"
    elif prev_last_seen is not None and (today_d - prev_last_seen).days <= 1:
        label = "DEVELOPING"
    else:
        label = "ONGOING"
    ev["updated_at"] = now_iso
    return ev["event_id"], label


def mark_reported(state, event_ids, digest_time, today):
    """Record that these events were reported in the given digest edition."""
    if digest_time not in ("morning", "evening"):
        raise ValueError("digest_time must be 'morning' or 'evening'")
    today_iso = _coerce_today(today).isoformat()
    wanted = set(event_ids or [])
    marked = []
    for ev in state.get("stories", []):
        if not isinstance(ev, dict) or ev.get("event_id") not in wanted:
            continue
        if not ev.get("first_reported"):
            ev["first_reported"] = today_iso
        ev["last_reported"] = today_iso
        if digest_time == "morning":
            ev["in_morning"] = today_iso
        else:
            ev["in_evening"] = today_iso
        marked.append(ev["event_id"])
    return marked


def expire(state, today):
    """Apply lifecycle transitions; returns transition counts."""
    today_d = _coerce_today(today)
    stories = state.setdefault("stories", [])
    archived = state.setdefault("archived", [])
    resolved = stale = moved = 0
    kept = []
    for ev in stories:
        if not isinstance(ev, dict):
            continue
        deadline = parse_date(ev.get("deadline"))
        last_seen = parse_date(ev.get("last_seen")) or parse_date(ev.get("first_seen"))
        # deadline < today aligns with validate_stories' deadline_expired drop
        # semantics: a deadline equal to today is still live today.
        if deadline and deadline < today_d and ev.get("status") != "RESOLVED":
            ev["status"] = "RESOLVED"
            ev["updated_at"] = kathmandu_now_iso()
            resolved += 1
        elif (last_seen and (today_d - last_seen).days > STALE_AFTER_DAYS
              and ev.get("status") != "STALE"):
            ev["status"] = "STALE"
            ev["updated_at"] = kathmandu_now_iso()
            stale += 1
        if (ev.get("status") in ("STALE", "RESOLVED") and last_seen
                and (today_d - last_seen).days > ARCHIVE_AFTER_DAYS):
            archived.append(ev)
            moved += 1
        else:
            kept.append(ev)
    state["stories"] = kept

    def _age_key(e):
        return parse_date(e.get("last_seen")) or parse_date(e.get("first_seen")) or date.min

    archived.sort(key=_age_key, reverse=True)
    dropped = max(len(archived) - ARCHIVE_CAP, 0)
    if dropped:
        del archived[ARCHIVE_CAP:]
    return {"resolved": resolved, "stale": stale, "archived": moved, "dropped": dropped}


def _load_story_payload(path):
    if not os.path.isfile(path):
        print("FATAL: stories file not found: %s" % path, file=sys.stderr)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print("FATAL: cannot parse stories JSON (%s): %s" % (path, exc), file=sys.stderr)
        return None
    stories = data.get("stories") if isinstance(data, dict) else data
    if not isinstance(stories, list):
        print("FATAL: stories file has no 'stories' list", file=sys.stderr)
        return None
    return [s for s in stories if isinstance(s, dict)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Active-story state library CLI.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_sight = sub.add_parser("sightings", help="Upsert story sightings into state.")
    p_sight.add_argument("--stories", dest="stories_path", required=True)
    p_sight.add_argument("--state", dest="state_path", required=True)

    p_mark = sub.add_parser("mark-reported", help="Mark events reported in a digest edition.")
    p_mark.add_argument("--stories", dest="stories_path", required=True)
    p_mark.add_argument("--state", dest="state_path", required=True)
    p_mark.add_argument("--digest-time", dest="digest_time",
                        choices=("morning", "evening"), required=True)

    p_exp = sub.add_parser("expire", help="Apply deadline/stale/archive transitions.")
    p_exp.add_argument("--state", dest="state_path", required=True)

    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        state = load_state(args.state_path)
    except StateCorruptionError as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 3

    today_iso = kathmandu_today().isoformat()

    if args.command == "sightings":
        stories = _load_story_payload(args.stories_path)
        if stories is None:
            return 1
        labels = {}
        for story in stories:
            _, label = upsert_sighting(state, story, today_iso)
            labels[label] = labels.get(label, 0) + 1
        save_state_atomic(args.state_path, state)
        print("story_state sightings:")
        print("  input stories: %d" % len(stories))
        for label in ("NEW", "UPDATED", "DEVELOPING", "ONGOING"):
            if labels.get(label):
                print("  %-10s     %d" % (label, labels[label]))
        print("  total active:  %d -> %s" % (len(state["stories"]), args.state_path))
        return 0

    if args.command == "mark-reported":
        stories = _load_story_payload(args.stories_path)
        if stories is None:
            return 1
        event_ids = [s.get("event_id") for s in stories if s.get("event_id")]
        marked = mark_reported(state, event_ids, args.digest_time, today_iso)
        save_state_atomic(args.state_path, state)
        print("story_state mark-reported (%s):" % args.digest_time)
        print("  requested: %d" % len(event_ids))
        print("  marked:    %d" % len(marked))
        return 0

    if args.command == "expire":
        counts = expire(state, today_iso)
        save_state_atomic(args.state_path, state)
        print("story_state expire:")
        print("  resolved (deadline passed): %d" % counts["resolved"])
        print("  stale (unseen >%dd):        %d" % (STALE_AFTER_DAYS, counts["stale"]))
        print("  archived (>30d):            %d" % counts["archived"])
        print("  archive overflow dropped:   %d" % counts["dropped"])
        print("  total active:               %d" % len(state["stories"]))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
