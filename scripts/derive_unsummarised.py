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
# And the SAME English-only gate. A headline the page cannot draw must be
# REFUSED here rather than shipped and dropped later, because this file is the
# only one that knows how many stories the fallback held back.
import english_text  # noqa: E402

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

NOTE = ("Unsummarised edition: %s, so the text under each headline is the "
        "publisher's own description, not a summary written for you.")

#: The same note when SOME stories did get a written summary. Saying "the text
#: under each headline is the publisher's description" over an AI sentence is
#: the one claim this page must never make, so the counted form replaces it
#: whenever summarise_stories answered for at least one story.
NOTE_SUMMARISED = ("Fallback edition: an AI wrote the summary for %d of %d "
                   "stories; the rest quote the publisher's own description.")

#: WHY there is no AI, in the walk's own vocabulary. The old note said only
#: "no AI was available", which is true of every case and therefore tells the
#: reader nothing: the 2026-11-05 edition HAD the keyed models configured, and
#: it was the Gemini free tier's per-minute input-token quota (250,000) that
#: refused them before the free OpenCode pool refused the rest.
REASON_QUOTA = "quota_exhausted"
REASON_NOT_CONFIGURED = "not_configured"
REASON_ALL_FAILED = "all_failed"

REASON_TEXT = {
    REASON_QUOTA: "every AI model this build reached refused on its provider's quota",
    REASON_NOT_CONFIGURED: "no AI model was configured or reachable for this build",
    REASON_ALL_FAILED: "every AI model this build tried failed to produce a digest",
}

#: The walk's failure kinds that mean "the allowance is spent", not "the rung
#: broke". `payment_required` is the OpenCode plan refusing its free rungs
#: ("Free usage limit reached"), which is the same fact about an account.
QUOTA_KINDS = frozenset({"quota_or_rate_limited", "payment_required"})


def reason_from_attempts(text):
    """Which of the three no-AI reasons the walk's attempt log shows.

    daily-news.yml writes one row per attempt (`kind=quota_or_rate_limited`,
    `kind=payment_required`, `kind=ok`, ...). Three outcomes, three sentences:

      * NO ROWS: no model was ever contacted, so nothing was configured or
        reachable. That is not a quota wall and not a rung that failed.
      * every row a spent allowance: the quota ran out.
      * anything else: a rung was tried and failed for its own reason.
    """
    kinds = re.findall(r"\bkind=([a-z_]+)", text or "")
    if not kinds:
        return REASON_NOT_CONFIGURED
    if all(kind in QUOTA_KINDS for kind in kinds):
        return REASON_QUOTA
    return REASON_ALL_FAILED


def providers_in(text):
    """`model=<provider>/<id>` -> the provider names, in first-seen order."""
    names = []
    for name in re.findall(r"\bmodel=([A-Za-z0-9_.-]+)/", text or ""):
        if name not in names:
            names.append(name)
    return names


def note_for(reason, attempts_text=""):
    """The page's own sentence for WHY no model wrote this edition."""
    detail = REASON_TEXT.get(reason, REASON_TEXT[REASON_ALL_FAILED])
    if reason == REASON_QUOTA:
        names = providers_in(attempts_text)
        if names:
            detail = "%s (%s)" % (detail, ", ".join(names))
    return NOTE % detail


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
    Four rules, each of which returns "" rather than a weaker string:

      * text the page cannot draw is dropped first: a Devanagari description
        would render as an empty box;
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
    if not english_text.looks_english(text):
        # The page cannot draw it, so quoting it would ship an empty box. The
        # headline is already English; this story keeps its bare headline.
        return ""
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


def derive(candidates, now=None, held=None):
    """(stories, per-category counts, skipped-category counts).

    `skipped` is printed by main rather than swallowed: a candidate the schema
    will not accept is a candidate the collector spent a source on for nothing,
    and the count is the evidence for that.

    `held` is an optional dict the caller may pass; a candidate whose title is
    not English is counted into ``held["non_english"]`` instead of disappearing,
    because "the page could not draw it" is a decision the payload has to report.
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
        if not english_text.looks_english(title):
            if isinstance(held, dict):
                held["non_english"] = held.get("non_english", 0) + 1
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


def apply_summaries(stories, summaries):
    """Overwrite each story's summary with the model's, and count what landed.

    A url the model did not answer for keeps whatever the deriver wrote, which
    is the publisher's description or the empty string. Nothing is invented to
    fill a gap, and no story is dropped for lacking a summary.
    """
    written = 0
    for story in stories or []:
        text = (summaries or {}).get(story.get("source_url") or "")
        if isinstance(text, str) and text.strip():
            story["summary"] = text.strip()
            written += 1
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Derive the no-AI fallback edition.")
    ap.add_argument("--candidates", default="state/candidates.json")
    ap.add_argument("--out", default="state/stories.json")
    ap.add_argument("--today", default="")
    ap.add_argument("--slot", default="")
    # The walk's own attempt log: the only evidence of why no model wrote this
    # edition. Absent or empty means no model was contacted at all.
    ap.add_argument("--attempts", default="state/agent-attempts.log")
    # OFF by default so a hand-run and every existing test stay offline; the
    # workflow turns it on. The flag, not a hidden default, is what makes "this
    # run may call a model" visible in the YAML.
    ap.add_argument("--summarise", action="store_true",
                    help="ask a model for one summary per story, in paced batches")
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

    held = {}
    stories, counts, skipped = derive(candidates, held=held)
    for category in sorted(counts):
        print("  %-24s %d" % (category, counts[category]))
    for category in sorted(skipped):
        print("  %-24s skipped %d (not a digest category)"
              % (category, skipped[category]))
    held_n = held.get("non_english", 0)
    if held_n:
        print("  %-24s %d (title not in English)" % ("held back", held_n))
    print("derive_unsummarised: %d stories from %d candidates"
          % (len(stories), len(candidates)))

    if len(stories) < MIN_STORIES:
        # Fewer than the floor means the empty/degraded path owns this run. This
        # is decided BEFORE any model call: the count cannot change here, so
        # summarising first would spend quota on an edition nothing will read.
        print("FLOOR: only %d stories (minimum %d); nothing written to %s"
              % (len(stories), MIN_STORIES, args.out))
        if held_n:
            print("HELD: %d candidate(s) were not in English; the floor counted "
                  "only the stories the page can draw" % held_n)
        return 0

    # Only what the model wrote, never which stories ship: a refused batch
    # leaves those stories with the publisher's text.
    written = 0
    if args.summarise and stories:
        # Imported here, not at module level: the deriver is used offline by
        # tests and by hand, and it must not need the summariser to import.
        import summarise_stories
        result = summarise_stories.summarise(stories)
        written = apply_summaries(stories, result.get("summaries"))
        for refusal in result.get("refusals") or []:
            print("  refusal: %s" % refusal)
        print("  %-24s %d of %d (model %s)"
              % ("summarised", written, len(stories),
                 result.get("model") or "none"))
        # WHY EACH BATCH FAILED, next to the edition it failed to write. The run
        # log is truncated and the shell is not where a 2 KB model reply belongs;
        # the artifact is, and it is where the next investigator will look.
        debug = result.get("debug") or []
        if debug:
            path = os.path.join(os.path.dirname(os.path.abspath(args.out))
                                or ".", "summarise-debug.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"generated_at": datetime.now(timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "attempts": debug}, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            print("  %-24s %s (%d attempt(s))"
                  % ("debug", path, len(debug)))

    # WHY, not merely THAT. The attempt log is the walk's own record, so the
    # note names a spent quota, a missing configuration or failed rungs instead
    # of the old catch-all "no AI was available".
    attempts_text = ""
    if args.attempts:
        try:
            with open(args.attempts, "r", encoding="utf-8", errors="replace") as fh:
                attempts_text = fh.read()
        except OSError:
            attempts_text = ""
    reason = reason_from_attempts(attempts_text)
    print("  %-24s %s (%d attempt row(s))"
          % ("no-AI reason", reason, len(re.findall(r"\bkind=", attempts_text))))

    notes = note_for(reason, attempts_text)
    if written:
        notes = NOTE_SUMMARISED % (written, len(stories))
    if held_n:
        notes = notes + " " + english_text.held_back_note(held_n)
    payload = {
        "date": args.today,
        # STILL `unsummarised`, even when every story got a written summary.
        # The seal, the delivery ledger and the Worker all read this field as
        # "no model produced this digest", which remains true: the selection,
        # the ranking and the page are derived. What the model wrote is one
        # sentence per story, and `summary_counts` is what says how many.
        "edition_kind": "unsummarised",
        # The count the page needs to be honest about its own text.
        "summary_counts": {"summarised": written, "total": len(stories)},
        # No model wrote this edition; naming the deriver as the writer is what
        # lets the seal report how the digest was produced.
        "writer": {"rung": "derived", "attempt": None, "model": None},
        "notes": notes,
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
    print("wrote %s (%d stories, edition_kind=unsummarised, %d summarised)"
          % (args.out, len(stories), written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
