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
import unicodedata
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
from validate_stories import (crime_violation, is_real_summary,  # noqa: E402
                              load_excluded_keywords, snippet_index,
                              spoiler_violation, stub_reason)
# And the SAME English-only gate. A headline the page cannot draw must be
# REFUSED here rather than shipped and dropped later, because this file is the
# only one that knows how many stories the fallback held back.
import english_text  # noqa: E402
# And the ONE rule that names the edition, shared with the validator so a drop
# there cannot leave the name describing a count that no longer exists.
import edition_kind  # noqa: E402

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

NOTE = ("Unsummarised edition: %s; the text is the publisher's own "
        "description, not a summary.")

#: The note is capped at 240 chars of text by BOTH renderers (render_digest and
#: build_email each carry their own NOTE_LIMIT), and run 41995372308 shipped the
#: sentence cut mid-word at "…they are not in". So the sentence is composed to
#: fit with room to spare: it is never allowed to reach either cap.
NOTE_BUDGET = 236

#: WHY there is no AI, in the walk's own vocabulary. The old note said only
#: "no AI was available", which is true of every case and therefore tells the
#: reader nothing: the 2026-11-05 edition HAD the keyed models configured, and
#: it was the Gemini free tier's per-minute input-token quota (250,000) that
#: refused them before the free OpenCode pool refused the rest.
REASON_QUOTA = "quota_exhausted"
REASON_NOT_CONFIGURED = "not_configured"
REASON_ALL_FAILED = "all_failed"
#: Run 41995372308 needed these two: the keyed rungs ANSWERED, and the three
#: replies the parser threw away were the only thing standing between the page
#: and a quota sentence that was not even true that morning.
REASON_PARSE = "unreadable_reply"
REASON_TIMEOUT = "model_timeout"

REASON_TEXT = {
    REASON_QUOTA: "every AI model this build reached refused on its provider's quota",
    REASON_NOT_CONFIGURED: "no AI model was configured or reachable for this build",
    REASON_ALL_FAILED: "every AI model this build tried failed to produce a digest",
    REASON_PARSE: "every AI model that answered sent a reply this build could not read",
    REASON_TIMEOUT: "every AI model this build reached ran out of time before answering",
}

#: summarise_stories' own cause names, most actionable first. They are the
#: answer to "why did these stories get no summary" from the component that
#: asked the models, so they outrank the walk's attempt log whenever they exist.
CAUSE_ORDER = ("unreadable_reply", "provider_refusal", "model_timeout",
               "runner_fault")

#: How each cause is named to a reader, and which reason it maps to.
CAUSE_PHRASE = {
    "unreadable_reply": "an unreadable reply",
    "provider_refusal": "a quota refusal",
    "model_timeout": "a timeout",
    "runner_fault": "a crashed model runner",
}

CAUSE_REASON = {
    "unreadable_reply": REASON_PARSE,
    "provider_refusal": REASON_QUOTA,
    "model_timeout": REASON_TIMEOUT,
    "runner_fault": REASON_ALL_FAILED,
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


def rank_causes(causes):
    """(stories lost, cause) pairs, most costly first, ties in priority order."""
    ranked = [(int(count), cause) for cause, count in (causes or {}).items()
              if int(count) > 0]
    return sorted(ranked, key=lambda pair: (-pair[0],
                                            CAUSE_ORDER.index(pair[1])
                                            if pair[1] in CAUSE_ORDER
                                            else len(CAUSE_ORDER)))


def reason_from_causes(causes):
    """WHY no model wrote the edition, from the summariser's own outcomes.

    Run 41995372308 is the reason this exists. The walk's attempt log said
    "quota" and the page printed it, while the keyed Gemini rungs had in fact
    answered -- the parser refused three good replies. The component that asked
    the models knows that, so its causes win over a log written by a different
    component about a different set of calls.
    """
    ranked = rank_causes(causes)
    if not ranked:
        return REASON_ALL_FAILED
    return CAUSE_REASON.get(ranked[0][1], REASON_ALL_FAILED)


def _cause_prose(ranked, keep=None):
    """`6 stories lost their summary to X and 3 to Y`, for `ranked` causes."""
    shown = ranked if keep is None else ranked[:keep]
    parts = []
    for position, (count, cause) in enumerate(shown):
        phrase = CAUSE_PHRASE.get(cause, CAUSE_PHRASE["runner_fault"])
        if position == 0:
            parts.append("%d stor%s lost %s summary to %s"
                         % (count, "y" if count == 1 else "ies",
                            "its" if count == 1 else "their", phrase))
        else:
            parts.append("%d to %s" % (count, phrase))
    remaining = ranked[len(shown):]
    if remaining:
        parts.append("%d to %d other cause%s"
                     % (sum(count for count, _ in remaining), len(remaining),
                        "" if len(remaining) == 1 else "s"))
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def note_for(reason, attempts_text="", causes=None, publisher=0, none=0,
             total=0):
    """The page's own sentence for WHY no model wrote this edition.

    When the summariser recorded causes, each cause is reported with the number
    of STORIES it cost. The sentence is composed longest-first and the first
    form that fits NOTE_BUDGET wins, so a held-back clause appended afterwards can
    never be the thing that gets cut.

    The tail states what is under the headlines. It used to claim every one
    quoted the publisher, which was false for a story whose feed carried no
    description (run 42074891307).
    """
    details = []
    ranked = rank_causes(causes)
    if ranked:
        details = [_cause_prose(ranked), _cause_prose(ranked, 2),
                   _cause_prose(ranked, 1),
                   CAUSE_PHRASE.get(ranked[0][1], CAUSE_PHRASE["runner_fault"])]
    fallback = REASON_TEXT.get(reason, REASON_TEXT[REASON_ALL_FAILED])
    if reason == REASON_QUOTA:
        names = providers_in(attempts_text)
        if names:
            fallback = "%s (%s)" % (fallback, ", ".join(names))
    details.append(fallback)
    if none <= 0:
        pattern = NOTE
    else:
        tail = edition_kind.coverage_phrase(publisher, none, total)
        pattern = "Unsummarised edition: %%s; %s." % tail
    for detail in details:
        note = pattern % detail
        if len(note) <= NOTE_BUDGET:
            return note
    return pattern % details[-1]


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


#: How much of a rejected answer is kept in the debug artifact. Long enough to
#: recognise a headline or a mangled reply, short enough for a JSON file.
DEBUG_TEXT_MAX_CHARS = 500
_JOINERS = "\u200d\ufe0f\u20e3"


def clip_text(text, limit=DEBUG_TEXT_MAX_CHARS):
    """*text* cut to *limit* code points at a character boundary, then marked.

    Python slices code points, but a grapheme can span several of them (an emoji
    family is joined by zero-width joiners). Cutting right after a joiner or a
    combining mark leaves half a visible character, so the cut walks back to the
    last safe boundary first.
    """
    text = str(text or "")
    if len(text) <= limit:
        return text
    cut = max(0, limit)
    while cut > 0 and (unicodedata.combining(text[cut])
                       or text[cut - 1] in _JOINERS):
        cut -= 1
    return text[:cut] + "\u2026"


def record_rejections(path, rejected, attempts=None):
    """Append rejected answers to the gitignored debug artifact.

    The file exists to answer "why did this run write 0 summaries". A failed
    request keeps the record it already had (`attempts`), and a received answer
    the predicate refused is added with its own reason and verbatim text. Best
    effort: a debug artifact must never fail the step.
    """
    if not path:
        return
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    rows = list(data.get("attempts") or [])
    rows.extend(attempts or [])
    rejects = list(data.get("rejected") or [])
    rejects.extend(rejected or [])
    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["attempts"] = rows
    if rejects:
        data["rejected"] = rejects
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        return


def apply_summaries(stories, summaries, snippets=None, model="", rejected=None):
    """Overwrite each story's summary with the model's, and count what landed.

    A url the model did not answer for keeps whatever the deriver wrote, which
    is the publisher's description or the empty string. Nothing is invented to
    fill a gap, and no story is dropped for lacking a summary.

    THE LABEL IS DECIDED BY `is_real_summary`, the same predicate the validator
    counts with. RUN 42070185644 (2026-11-05) shipped six stories marked "ai"
    whose body only restated the headline, so the push said "5 of 15 summarised"
    and the note said the rest quote the publisher -- both false. A text that is
    not prose is not kept, so the story keeps the publisher's own description
    and its label.
    """
    written = 0
    for story in stories or []:
        url = story.get("source_url") or ""
        text = (summaries or {}).get(url)
        snippet = (snippets or {}).get(url, "")
        if isinstance(text, str) and text.strip() and is_real_summary(
                text.strip(), story.get("title"), snippet):
            story["summary"] = text.strip()
            # Per-story truth, so the validator can subtract anything it drops
            # instead of trusting a total that no longer matches the list.
            story["summary_source"] = "ai"
            written += 1
            continue
        if isinstance(text, str) and text.strip() and rejected is not None:
            # A received answer the predicate refused. RUN 42074891307 left no
            # record of these, so "0 written" could not be diagnosed.
            rejected.append({
                "title": str(story.get("title") or ""),
                "model": model or "",
                "text": clip_text(text.strip()),
                "reason": stub_reason(text.strip(), story.get("title"), snippet),
            })
        story["summary_source"] = "publisher"
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
    causes = {}
    publisher = none = 0
    if args.summarise and stories:
        # Imported here, not at module level: the deriver is used offline by
        # tests and by hand, and it must not need the summariser to import.
        import summarise_stories
        result = summarise_stories.summarise(stories)
        rejected = []
        written = apply_summaries(stories, result.get("summaries"),
                                  snippet_index(args.candidates),
                                  model=result.get("model") or "",
                                  rejected=rejected)
        causes = result.get("causes") or {}
        for refusal in result.get("refusals") or []:
            print("  refusal: %s" % refusal)
        print("  %-24s %d of %d (model %s)"
              % ("summarised", written, len(stories),
                 result.get("model") or "none"))
        # WHY EACH BATCH FAILED, and WHY each received answer was refused, next
        # to the edition it failed to write. The run log is truncated and the
        # shell is not where a 2 KB model reply belongs; the artifact is, and it
        # is where the next investigator will look.
        debug = result.get("debug") or []
        if debug or rejected:
            path = os.path.join(os.path.dirname(os.path.abspath(args.out))
                                or ".", "summarise-debug.json")
            record_rejections(path, rejected, attempts=debug)
            print("  %-24s %s (%d attempt(s), %d rejected answer(s))"
                  % ("debug", path, len(debug), len(rejected)))
    if stories:
        for story in stories:
            if story.get("summary_source") == "ai":
                continue
            if str(story.get("summary") or "").strip():
                # A non-AI story with a body: the deriver wrote it from the
                # feed's description, so it is publisher text.
                publisher += 1
            else:
                none += 1

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
    reason = reason_from_causes(causes) if causes else reason_from_attempts(
        attempts_text)
    print("  %-24s %s (%d attempt row(s)%s)"
          % ("no-AI reason", reason, len(re.findall(r"\bkind=", attempts_text)),
             "" if not causes else ", %d summariser cause(s)" % len(causes)))

    notes = note_for(reason, attempts_text, causes=causes,
                     publisher=publisher, none=none, total=len(stories))
    # One decision, taken from the counters the note prints, used by the note AND
    # the payload field: the name and the number cannot drift apart here.
    kind = edition_kind.kind_for(written, len(stories))
    if written:
        notes = edition_kind.summary_note(kind, written, len(stories),
                                          publisher, none)
    if held_n:
        notes = english_text.replace_held_back(notes, held_n)
    payload = {
        "date": args.today,
        # Derived, not assumed: 0 summaries is `unsummarised`, some is `partial`,
        # all is `full`. The seal, the delivery ledger and the Worker all branch
        # on this field, so a hardcoded value here is what made a 22-of-29
        # edition claim "no AI today".
        "edition_kind": kind,
        # The count the page needs to be honest about its own text.
        "summary_counts": {"summarised": written, "total": len(stories)},
        # How many stories this stage withheld for their script, as DATA. The
        # validator adds its own drops to this number and rewrites the sentence,
        # so both renderers read one count instead of each re-reading the prose.
        "held_back": {"count": held_n, "reason": "not_english"},
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
    print("wrote %s (%d stories, edition_kind=%s, %d summarised)"
          % (args.out, len(stories), kind, written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
