#!/usr/bin/env python3
"""build_email.py — render state/stories.validated.json into HTML + plain-text email.

Sections are rendered in a FIXED order. A section renders when it has stories,
and it also renders when it has an ALARM to make; a section with zero stories
and only a QUIET status is omitted entirely, at the owner's request ("if there
is no news then do not display Gaming - Nothing new from this source. just
don't display it."). Alarms are never hidden: a source the owner tracks going
dark is the one thing worth saying, so a dead source can never be mistaken for
a quiet day. The digest-level banner and the freshness line are derived from the
`source_health` object in the input (FROZEN CONTRACT C) — never from the
research agent's own research_status.

All story content is HTML-escaped (html.escape). Inline CSS only (email-client
safe): tables/divs, literal bullet characters, max-width 600px, web-safe fonts.
Zero survivors still renders a minimal email. Exit 0 always unless the input
file is missing/unreadable (exit 1).

Stdlib only. Windows-friendly (ASCII-only stdout).
"""

import argparse
import datetime
import html
import json
import os
import re
import sys

try:
    import private_values as _pv
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import private_values as _pv

# Fixed section order: (category key, display header, accent color)
# The two institution headers are runtime values (see private_values).
SECTIONS = [
    ("action",              "ACTION / IMPORTANT",             "#dc2626"),
    ("world_major",         "WORLD &#8212; MAJOR EVENTS",     "#0891b2"),
    ("local_city",        "LOCAL &amp; CITY",             "#0d9488"),
    ("education_tu",        "TU / BSC CSIT",                  "#2563eb"),
    ("college_local",       _pv.college_name(),               "#7c3aed"),
    ("bank_local",          _pv.bank_name().replace("\u2014", "&#8212;"), "#16a34a"),
    ("nepal",               "IMPORTANT NEPAL",                "#ea580c"),
    ("gaming",              "GAMING",                         "#4f46e5"),
    ("anime",               "ANIME",                          "#db2777"),
    ("manga_manhwa_manhua", "MANGA &middot; MANHWA &middot; MANHUA", "#9333ea"),
    ("tech_career",         "TECH / CAREER / OPPORTUNITIES",  "#475569"),
]

FONT_STACK = "Arial, Helvetica, sans-serif"
CHIP_STYLE = ("display:inline-block;background-color:#e5e7eb;color:#374151;"
              "border-radius:9999px;padding:1px 8px;font-size:11px;"
              "font-weight:bold;margin-right:6px;")
# ---------------------------------------------------------------------------
# Honesty: source health -> what the reader is told (FROZEN CONTRACT B / C)
# ---------------------------------------------------------------------------
# There is exactly ONE banner definition in this codebase and it is driven by
# CODE from the `source_health` payload, not by the research agent. The old
# research_status == "limited" notice is gone: research_status is the agent's
# own opinion and a TU-only outage never set it, which is why the TU section
# could vanish silently.
#
# `unknown` is the PERMANENT correct value when the field is absent,
# malformed or from a legacy build.

# `empty` is the "answered but produced nothing usable" state: a distinct
# claim from `unknown` (nothing was confirmed reachable) and from `degraded`
# (a host was unreachable). It gets its own literal and never reuses the
# unknown one, and these three blocks are verbatim copies of
# scripts/render_digest.py -- the email and the website must never disagree
# about whether a digest is complete. That divergence was real: this file
# knew only ok/partial/degraded/unknown, so an `empty` digest rendered the
# `unknown` banner and DROPPED the timestamp while the website rendered the
# correct `empty` banner and kept it.
HEALTH_STATES = ("ok", "partial", "degraded", "empty", "unknown")

HEALTH_BANNER = {
    "partial": "⚠️ Some sources failed — this digest may be missing items.",
    "degraded": "⚠️ Some sources were unreachable — this digest may be missing items.",
    "empty": "⚠️ Some sources answered, but returned nothing usable — treat this digest as incomplete.",
    "unknown": "⚠️ Source check unavailable — this digest may be missing items.",
}

# FROZEN CONTRACT C. `unknown` DROPS the timestamp: if no source was
# confirmed reachable, "checked through T" is a lie. `empty` KEEPS it -- the
# sources did answer -- but suffixes it, because "checked through T" over
# garbage is still a partial claim.
CUTOFF_SUFFIX = {
    "ok": "",
    "partial": " · some sources failed",
    "degraded": " · some sources were unreachable",
    "empty": " (incomplete: some sources returned nothing usable)",
}
CUTOFF_UNKNOWN = "⚠️ Freshness unverified — no source was confirmed reachable"

NOTE_LIMIT = 240

GOOGLE_NEWS_RE = re.compile(r"https?://(?:[a-z0-9\-]+\.)*news\.google\.com/rss/articles/",
                            re.IGNORECASE)

BANNER_STYLE = ("background-color:#fef3c7;border:1px solid #f59e0b;"
                "border-left:4px solid #f59e0b;border-radius:6px;padding:10px 14px;"
                "margin:12px 0 4px 0;font-size:13px;color:#92400e;font-family:%s;"
                % FONT_STACK)
BANNER_NOTE_STYLE = ("background-color:#f9fafb;border:1px solid #e5e7eb;"
                     "border-left:4px solid #9ca3af;border-radius:6px;padding:8px 14px;"
                     "margin:8px 0 4px 0;font-size:12px;color:#4b5563;font-family:%s;"
                     % FONT_STACK)


def _int(value):
    """Best-effort int, never raising on None/junk from the health payload."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def health_state(source_health):
    """Normalise the source_health payload to one of HEALTH_STATES."""
    if not isinstance(source_health, dict):
        return "unknown"
    state = str(source_health.get("state") or "").strip().lower()
    return state if state in HEALTH_STATES else "unknown"


def hosts_down(source_health):
    """Critical hosts confirmed unreachable (list of strings, order kept)."""
    if not isinstance(source_health, dict):
        return []
    critical = source_health.get("critical")
    if not isinstance(critical, dict):
        return []
    raw = critical.get("hosts_down")
    if not isinstance(raw, list):
        return []
    return [h.strip() for h in raw if isinstance(h, str) and h.strip()]


def down_line(source_health, cap=3):
    """`Down: a, b, c (+N more)` — or '' when nothing critical is down."""
    hosts = hosts_down(source_health)
    if not hosts:
        return ""
    text = "Down: %s" % ", ".join(hosts[:cap])
    if len(hosts) > cap:
        text += " (+%d more)" % (len(hosts) - cap)
    return text


#: How a section status line should LOOK. `alarm` = a source the owner tracks
#: could not be checked; `quiet` = the check ran and there was nothing, or
#: there was never anything configured to check. See the identical block in
#: scripts/render_digest.py - the two must not diverge.
STATUS_ALARM = "alarm"
STATUS_QUIET = "quiet"

#: B3a: the benign "there was nothing configured to look at" line. A category
#: with no entry in `by_category` was never tracked, so "may be incomplete" was
#: a false alarm - 5 of 9 status lines on the live 2026-09-29 digest. The
#: section still says something, quietly, because silence is
#: indistinguishable from a render bug.
UNTRACKED_STATUS = "No sources are configured for this section."

#: Quiet and alarm inline styles for the email status box. The email cannot use
#: a stylesheet, so the two tones are two inline style strings; `.quiet` is the
#: default so a forgotten tone renders quiet rather than loud.
STATUS_QUIET_STYLE = ("border-left:3px solid #d1d5db;background-color:transparent;"
                      "border-radius:4px;padding:6px 10px;margin:0 0 10px 0;"
                      "font-size:12px;color:#4b5563;font-family:%s;" % FONT_STACK)
STATUS_ALARM_STYLE = ("border-left:4px solid #dc2626;background-color:#fef2f2;"
                      "border-radius:4px;padding:7px 10px;margin:0 0 10px 0;"
                      "font-size:12px;color:#991b1b;font-family:%s;" % FONT_STACK)


def health_by_category(source_health):
    """The by_category map from the payload, or {} when unusable."""
    if not isinstance(source_health, dict):
        return {}
    by_cat = source_health.get("by_category")
    return by_cat if isinstance(by_cat, dict) else {}


def _empty_hosts_of(entry):
    """`empty_hosts` from one by_category entry, or [] (v1 payloads have none)."""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("empty_hosts")
    if not isinstance(raw, list):
        return []
    return [h.strip() for h in raw if isinstance(h, str) and h.strip()]


def _status_is_untracked(category, source_health):
    """True when `category` is absent from a health map we can actually trust.

    UNTRACKED, not UNKNOWN: a present `by_category` plus a non-`unknown` digest
    state means the collector ran and reported nothing to check. A missing,
    malformed or `unknown` health object cannot tell untracked from unmeasured,
    so it keeps the honest B3 alarm. Verbatim from scripts/render_digest.py.
    """
    if not isinstance(source_health, dict):
        return False
    if health_state(source_health) == "unknown":
        return False
    if not isinstance(source_health.get("by_category"), dict):
        return False
    return category not in source_health["by_category"]


def section_status(category, item_count, source_health):
    """(line, tone) - FROZEN CONTRACT B: the one line a section says about itself.

    Seven exhaustive cases, in this order:
      B1  items > 0, failed == 0                      -> ('', '')          silent
      B2  items > 0, failed  > 0                      -> unreachable count, ALARM
      B3a items == 0, category untracked              -> UNTRACKED_STATUS,  QUIET
      B3  items == 0, health untrustworthy/skipped>0  -> status unknown,    ALARM
      B5  items == 0, failed  > 0                     -> where to look,     ALARM
      B6  items == 0, empty_hosts non-empty           -> checked, nothing,  QUIET
      B4  items == 0, none of the above               -> nothing new,      QUIET

    B6 is why `empty_hosts` is read here and not just in render_digest.py: a
    category whose hosts were CHECKED and returned nothing usable must not
    borrow B4's confident "nothing new". The literal below is character-for-
    character the one in scripts/render_digest.py and the one in the daily-
    news.yml ntfy heredoc; all three must be changed together or the same
    fact is reported three different ways.

    B3a is the one case that DIVERGES from the ntfy heredoc, which cannot be
    changed here: the push still prints the B3 alarm for an untracked
    category. The claim is the same on both surfaces, only the emphasis is not.
    """
    entry = health_by_category(source_health).get(category)
    if not isinstance(entry, dict):
        entry = None
    total = _int((entry or {}).get("total"))
    failed = _int((entry or {}).get("failed"))
    skipped = _int((entry or {}).get("skipped"))

    if item_count > 0:
        if failed > 0:
            return ("⚠️ %d of %d sources for this section were unreachable."
                    % (failed, total), STATUS_ALARM)
        return ("", "")

    if entry is None:
        if _status_is_untracked(category, source_health):
            return (UNTRACKED_STATUS, STATUS_QUIET)
        return ("⚠️ Source status unknown — this section may be incomplete.",
                STATUS_ALARM)
    if skipped > 0:
        return ("⚠️ Source status unknown — this section may be incomplete.",
                STATUS_ALARM)
    if failed > 0:
        raw_urls = entry.get("urls")
        urls = [u.strip() for u in raw_urls if isinstance(u, str) and u.strip()][:2] \
            if isinstance(raw_urls, list) else []
        if not urls:
            return ("⚠️ Source status unknown — this section may be incomplete.",
                    STATUS_ALARM)
        return ("⚠️ Source unreachable — check %s" % " or ".join(urls), STATUS_ALARM)
    if _empty_hosts_of(entry):
        # ONE LINE, DELIBERATELY -- see the identical note in
        # scripts/render_digest.py. Wrapped across two source lines, this
        # literal was not greppable as a whole in either file while the ntfy
        # heredoc carried it on one line, so the three surfaces could not be
        # compared without evaluating the code. Kept contiguous in all three.
        return ("Nothing new from this source today (checked — the source returned nothing usable).", STATUS_QUIET)
    return ("Nothing new from this source today.", STATUS_QUIET)


def section_status_line(category, item_count, source_health):
    """The status TEXT alone, for the plain-text body and every consumer that
    only needs the claim. `section_status` is the full answer."""
    return section_status(category, item_count, source_health)[0]


def section_is_hidden(item_count, tone):
    """True when a section must be OMITTED from the email entirely.

    THE OWNER'S RULE: "if there is no news then do not display Gaming -
    Nothing new from this source. just don't display it."

    VERBATIM FROM scripts/render_digest.py, and it must stay verbatim: the email
    and the website are separate implementations of one contract, and a section
    that is hidden on one and shown on the other is the same fact reported two
    different ways -- the exact divergence this file's own B6 comment warns
    about. Only a QUIET status on an empty section is hidden; an ALARM (B2/B3/
    B5) always renders, because a source the owner tracks going dark is
    actionable and silence would hide it.
    """
    return item_count <= 0 and tone == STATUS_QUIET


def cutoff_line(cutoff, source_health, scheduled="", generated_at=""):
    """FROZEN CONTRACT C: the honest freshness line."""
    state = health_state(source_health)
    if state == "unknown":
        return CUTOFF_UNKNOWN
    if not cutoff:
        return ""
    line = "Information checked through: %s NPT" % cutoff + CUTOFF_SUFFIX.get(state, "")
    if scheduled and generated_at:
        gen_hhmm = generated_at.strip()[-5:] if len(generated_at.strip()) >= 5 else generated_at
        line += " (scheduled %s · runner started %s)" % (scheduled, gen_hhmm)
    return line


#: Build-log tokens that must never reach a reader surface: the collector's
#: `notice:<host>` log prefix and Python exception class names. The host is
#: kept (it is the useful part) and a timeout is still called a failure, so
#: nothing is hidden - only re-worded for someone who does not read build logs.
#: Verbatim from scripts/render_digest.py; the payload keeps the raw text.
NOTE_NOTICE_HOST_RE = re.compile(r"\bnotice:([A-Za-z0-9._-]+)")
NOTE_EXCEPTION_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception|Timeout)\b")
#: The collector's own name for itself, dropped so the note reads as a sentence.
NOTE_COLLECTOR_LEAD_RE = re.compile(r"^Collector:\s*", re.IGNORECASE)
#: The collector's source IDs for the RSS feeds, e.g. `feed:gaming-pcgamer`.
#: Verbatim from scripts/render_digest.py; the payload keeps the raw text.
NOTE_FEED_RE = re.compile(r"\bfeed:[A-Za-z0-9._-]+\s*")


def reader_note_text(notes):
    """The note with build-log vocabulary rewritten for a reader."""
    text = str(notes or "").strip()
    if not text:
        return ""
    text = NOTE_COLLECTOR_LEAD_RE.sub("", text)
    text = NOTE_NOTICE_HOST_RE.sub(r"\1", text)
    text = NOTE_FEED_RE.sub("", text).strip()
    return NOTE_EXCEPTION_RE.sub("a timeout", text)


def truncate_words(text, limit):
    """`text` cut to at most `limit` chars on a WORD boundary where there is
    one, with a single-character ellipsis. A note with no whitespace at all
    still gets a hard cut, so the cap stays a hard cap."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    boundary = cut.rfind(" ")
    if boundary >= max(1, int(limit * 0.6)):
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def capped_note(notes, limit=NOTE_LIMIT):
    """`⚠️ Research note: ...` capped at `limit` chars of note text, or ''.

    Sanitising happens BEFORE the cap, so the cap is still a hard ceiling on
    what the reader sees.
    """
    text = reader_note_text(notes)
    if not text:
        return ""
    if len(text) > limit:
        text = truncate_words(text, limit)
    return "⚠️ Research note: %s" % text


def notice_html(source_health, notes=""):
    """Banner + 'Down:' line for the {{RESEARCH_NOTICE}} slot ('' when ok)."""
    banner = HEALTH_BANNER.get(health_state(source_health), "")
    down = down_line(source_health)
    parts = []
    if banner:
        inner = banner
        if down:
            inner += '<br><span style="font-weight:normal;">%s</span>' % esc(down)
        parts.append('<div style="%s">%s</div>' % (BANNER_STYLE, inner))
    elif down:
        parts.append('<div style="%s">%s</div>' % (BANNER_STYLE, esc(down)))
    note = capped_note(notes)
    if note:
        parts.append('<div style="%s">%s</div>' % (BANNER_NOTE_STYLE, esc(note)))
    return "".join(parts)


def notice_text(source_health, notes=""):
    """Plain-text banner: one literal per line, '' when ok and no note."""
    lines = [HEALTH_BANNER.get(health_state(source_health), ""), down_line(source_health),
             capped_note(notes)]
    return "\n".join(ln for ln in lines if ln)


def resolve_article_url(story):
    """(url, via_google_news) - unwrap a Google News RSS redirect at render time."""
    url = str(story.get("source_url") or "").strip()
    if not GOOGLE_NEWS_RE.search(url):
        return url, False
    for s in story.get("supporting_sources") or []:
        if isinstance(s, dict):
            cand = str(s.get("url") or "").strip()
            if cand:
                return cand, True
    return url, True


# ---------------------------------------------------------------------------
# The personal layer: `Your day` + the weather block.
#
# DUPLICATED FROM render_digest.py ON PURPOSE, LITERALLY. The email, the
# website and the plain-text body are three separate implementations, and the
# ntfy push is a fourth that does not go through this module at all. All four
# are held to the same literals by tests/render/test_render_personal.py, so a
# divergence fails a test rather than shipping.
#
# The layer only SELECTS already-screened strings: the validator's `for_you`
# and the deterministic engine's `text`. No model call, no re-wording, no
# re-ranking. `Your day` is never empty (deadline rule + literal fallback);
# the weather block is empty when the engine had nothing, and an empty weather
# block means NOTHING RENDERS, not a "couldn't fetch weather" line.
# ---------------------------------------------------------------------------

# THE NO-DUPLICATION RULE (read this before adding a fifth place for weather)
# ---------------------------------------------------------------------------
# INVARIANT: no weather string appears TWICE in one rendered output, and the
# weather is never silently absent.
#
# The weather block and `Your day` are both rendered here, so they must never
# carry the same string. The split is:
#
#   * `Your day` holds deadlines and `for_you` lines ONLY.
#   * The weather appears exactly ONCE, in its own clearly-labelled block.
#
# The same split is applied on the website (scripts/render_digest.py) and on
# the ntfy push, which is a separate heredoc implementation in
# .github/workflows/daily-news.yml that does not come through this file at all.
# One rule across four surfaces is one rule to keep true.
#
# `include_weather` is the single switch, and every call site below passes it
# EXPLICITLY, so adding a surface forces the author to state which contract it
# is honouring.
FOR_YOU_LABEL = "For you"
YOUR_DAY_LABEL = "Your day"
WEATHER_LABEL = "Weather"
NOTHING_TODAY = "Nothing needs you today."
FOR_YOU_MAX = 3
WEATHER_MAX = 2
ACTION_CATEGORIES = (
    "action", "education_tu", "college_local", "bank_local",
    "tech_career", "local_city",
)
# ntfy budget constants, quoted here so both renderers cite the same numbers.
# `Your day` is capped at 3 LINES on every surface and has NO byte cap of its
# own: the push's byte ceiling is PERSONAL_MAX_BYTES, measured on the rendered
# block. See the same note in scripts/render_digest.py.
YOUR_DAY_MAX_LINES = 3
WEATHER_MAX_BYTES = 400
PERSONAL_MAX_BYTES = 1000

_DATE_PREFIX_LEN = 10


def parse_day(value):
    """'YYYY-MM-DD' (or anything starting with it) as a date, else None.

    An unparseable deadline is NOT treated as today or tomorrow: guessing
    would put a possibly-months-away line into a box whose whole purpose is
    to remove noise.
    """
    text = str(value or "").strip()
    if len(text) < _DATE_PREFIX_LEN:
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def deadline_due_on(deadline, today):
    """'YYYY-MM-DD' the deadline falls on, or '' when it is neither today nor
    tomorrow (including when it is not a date at all)."""
    day = parse_day(deadline)
    if day is None or today is None:
        return ""
    if day == today or day == today + datetime.timedelta(days=1):
        return day.isoformat()
    return ""


def _int_or(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_weather_advice(*payloads):
    """Weather advice `text` values, VERBATIM and in the engine's order.

    Tolerant on purpose: the engine writes a top-level `weather_advice` key
    into state/candidates.json, the validated payload may also carry one, and
    either file may be absent. The first NON-EMPTY list wins; otherwise []. No
    re-ranking: the engine already ordered by severity.
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        raw = payload.get("weather_advice")
        if not isinstance(raw, (list, tuple)) or not raw:
            continue
        texts = []
        for item in raw:
            text = item if isinstance(item, str) else (
                str((item or {}).get("text") or "").strip()
                if isinstance(item, dict) else "")
            if text:
                texts.append(text)
        if texts:
            return texts
    return []


def due_stories(stories, today):
    """[(index, title, due_iso)] for stories due today or tomorrow, nearest
    first, in input order within a date."""
    out = []
    for index, story in enumerate(stories or []):
        if not isinstance(story, dict):
            continue
        due = deadline_due_on(story.get("deadline"), today)
        if due:
            out.append((index, str(story.get("title") or "").strip() or "(untitled)", due))
    out.sort(key=lambda row: (row[2], row[0]))
    return out


def for_you_lines(stories, skip_indexes=(), limit=None):
    """Up to `limit` `for_you` lines, best first: obligation-bearing category,
    then the pipeline's `priority` descending, then input order. A story
    without a `for_you` is skipped SILENTLY - no placeholder, no empty label.
    Stories already shown as a deadline line are excluded so a 3-line box
    never repeats one story."""
    skip = set(skip_indexes or ())
    ranked = []
    for index, story in enumerate(stories or []):
        if index in skip or not isinstance(story, dict):
            continue
        text = str(story.get("for_you") or "").strip()
        if not text:
            continue
        ranked.append((0 if story.get("category") in ACTION_CATEGORIES else 1,
                       -_int_or(story.get("priority")), index, text))
    ranked.sort()
    cap = FOR_YOU_MAX if limit is None else max(0, limit)
    return [row[3] for row in ranked[:cap]]


def _cap_lines(lines, max_lines=None, max_bytes=None, keep_first=False):
    """Trim to a line count and a UTF-8 byte count.

    `keep_first=True` means a non-empty input always yields a non-empty
    output: `Your day` must never shed every line and then claim that nothing
    needs him while a deadline is live. The weather block leaves it off,
    because there silence really is the right answer.
    """
    out = []
    used = 0
    for line in lines:
        if max_lines is not None and len(out) >= max_lines:
            break
        cost = len(line.encode("utf-8")) + 1
        if out and max_bytes is not None and used + cost > max_bytes:
            break
        if not out and max_bytes is not None and cost > max_bytes and not keep_first:
            break
        out.append(line)
        used += cost
    return out


def your_day_lines(stories, weather, date_val, max_lines=None, max_bytes=None,
                   include_weather=True):
    """The `Your day` body lines, in priority order. Empty ONLY in one case.

    Deadlines first, because a time-critical obligation must never be crowded
    out by weather. `max_lines` is YOUR_DAY_MAX_LINES and every production call
    site passes it, on every surface, so one fixture yields one set of lines
    everywhere. `max_bytes` is left unset by every call site on purpose: a byte
    cap on this block can only silently drop a deadline, because the push's
    real ceiling (PERSONAL_MAX_BYTES) is measured on the rendered block and is
    strictly larger.

    `include_weather` is the NO-DUPLICATION rule (see the comment above
    FOR_YOU_LABEL): True on a surface where this block is the only place the
    weather appears, False where a labelled weather block renders it instead
    (email HTML and the plain-text body, both rich).

    THE ONE WAY THIS RETURNS NOTHING
    --------------------------------
    With `include_weather=False` and a non-empty `weather`, this block has
    nothing of its own to say and the weather is rendered elsewhere. It then
    returns `[]` so the caller omits the block, rather than printing the
    literal `Nothing needs you today.` directly above a "Take an umbrella"
    line that plainly does need him. A fallback that contradicts the next
    block is worse than no fallback.
    """
    today = parse_day(date_val)
    due = due_stories(stories, today)
    lines = ["%s - due %s" % (title, due_iso) for _index, title, due_iso in due]
    lines += for_you_lines(stories, skip_indexes=[row[0] for row in due])
    if include_weather:
        lines += list(weather or [])[:WEATHER_MAX]
    if not lines:
        if weather and not include_weather:
            return []
        return [NOTHING_TODAY]
    return _cap_lines(lines, max_lines, max_bytes, keep_first=True)


def weather_block_lines(weather, max_lines=None, max_bytes=None):
    """Weather block body lines - the engine's strings in its order, or
    NOTHING when the engine returned nothing."""
    if not weather:
        return []
    return _cap_lines(list(weather)[:WEATHER_MAX], max_lines, max_bytes)


def your_day_html(weather, stories, date_val):
    """`Your day` block for the email HTML body (inline styles only).

    RICH surface: no weather in here (NO-DUPLICATION RULE), so
    `include_weather=False` is passed explicitly. `max_lines` is stated here so
    this surface carries the SAME number of `Your day` lines as the push and
    the website.

    NEVER EMPTY (BUG 1). When `your_day_lines` yields nothing - a rich surface
    whose only content is weather, rendered in its own block below - the block
    used to return '' and the whole feature vanished from the page with no
    error to explain it. The live 2026-09-29 digest shipped exactly that: the
    CSS carried the block and the body did not. It now renders the documented
    fallback, so the owner is never left wondering whether the feature broke.

    `your_day_lines` itself is unchanged and still returns [] for this case,
    because it is duplicated in the workflow heredoc, which this change does
    not own; the divergence on the ntfy push is reported, not papered over.
    """
    lines = your_day_lines(stories, weather, date_val,
                           max_lines=YOUR_DAY_MAX_LINES, include_weather=False)
    items = "".join('<li style="margin:0 0 3px 0;">%s</li>' % esc(ln)
                    for ln in (lines or [NOTHING_TODAY]))
    return ('<div style="border:1px solid #00d4ff;border-left:4px solid #00d4ff;'
            'border-radius:6px;padding:10px 14px;margin:0 0 12px 0;'
            'background-color:#f0fdff;font-family:%s;">'
            '<div style="font-size:12px;font-weight:bold;letter-spacing:0.06em;'
            'color:#0e7490;margin:0 0 4px 0;">%s</div>'
            '<ul style="margin:0 0 0 16px;padding:0;font-size:13px;color:#374151;">%s</ul>'
            '</div>' % (FONT_STACK, esc(YOUR_DAY_LABEL), items))


def weather_html(weather):
    """Weather block for the email HTML body, or '' when there is nothing.

    An empty engine list must render NO markup at all - not a heading with
    nothing under it.
    """
    lines = weather_block_lines(weather)
    if not lines:
        return ""
    items = "".join('<li style="margin:0 0 3px 0;">%s</li>' % esc(ln) for ln in lines)
    return ('<div style="border:1px solid #f59e0b;border-left:4px solid #f59e0b;'
            'border-radius:6px;padding:10px 14px;margin:0 0 12px 0;'
            'background-color:#fffbeb;font-family:%s;">'
            '<div style="font-size:12px;font-weight:bold;letter-spacing:0.06em;'
            'color:#b45309;margin:0 0 4px 0;">&#9733; %s</div>'
            '<ul style="margin:0 0 0 16px;padding:0;font-size:13px;color:#374151;">%s</ul>'
            '</div>' % (FONT_STACK, esc(WEATHER_LABEL), items))


def your_day_text(weather, stories, date_val):
    """`Your day` + weather for the PLAIN-TEXT body (Gmail and Telegram).

    Bullets are `- ` and headers are bare, never `== SECTION ==` and never
    `* `, so render_digest.parse_email_text() walking this file cannot mistake
    a personal line for a story or for a section.

    RICH surface: `include_weather=False`, so each weather string appears here
    exactly ONCE, under the WEATHER heading (NO-DUPLICATION RULE). The
    `Your day` heading is rendered whenever the block has anything to say AND
    otherwise still rendered with the documented fallback, because a heading
    that silently disappears (BUG 1) is indistinguishable from a broken
    renderer. `max_lines` is stated here so this body carries the SAME number
    of `Your day` lines as the push and the website.
    """
    out = []
    yd = your_day_lines(stories, weather, date_val,
                        max_lines=YOUR_DAY_MAX_LINES, include_weather=False)
    if not yd:
        yd = [NOTHING_TODAY]
    out.append(YOUR_DAY_LABEL)
    out += ["- %s" % ln for ln in yd]
    lines = weather_block_lines(weather)
    if lines:
        if out:
            out.append("")
        out.append(WEATHER_LABEL)
        out += ["- %s" % ln for ln in lines]
    return "\n".join(out)


def esc(value):
    """HTML-escape any story-derived string."""
    return html.escape(str(value if value is not None else ""), quote=True)


#: Nepal Time, a fixed +05:45 with no DST. Same constant, same reason, as
#: KATHMANDU_UTC_OFFSET in render_digest.py.
KATHMANDU_UTC_OFFSET = datetime.timedelta(hours=5, minutes=45)

#: THE ONE PLACE THE EMAIL'S HUMAN-FACING WORD IS DECIDED, and it is the same
#: table and the same boundaries as render_digest.LABEL_HOUR_BOUNDS and the
#: ntfy heredoc's copy in daily-news.yml. THREE COPIES ON PURPOSE, not three
#: accidents: this file and render_digest.py are independent renderers that the
#: repo has explicitly refused to couple through a shared module (a shared
#: constant across an unowned renderer is how two renderers drift apart
#: silently), and the ntfy heredoc is a string sliced out of YAML and exec'd
#: with no repo on sys.path. All three are pinned against the same boundary
#: hours by tests/render/test_digest_label.py.
#:
#:   Midnight       00:00-00:59
#:   Late Night     01:00-04:59
#:   Dawn           05:00-06:59
#:   Morning        07:00-11:59
#:   Noon           12:00-12:59
#:   Afternoon      13:00-16:59
#:   Evening        17:00-20:59
#:   Night          21:00-23:59
#:
#: WHY EIGHT, and why Midnight is one hour long: the owner asked for "night,
#: midnight, afternoon, noon and so on and it should be according to the time".
#: Each of the three old coarse bands is split once, at a whole hour. Midnight
#: is 00:00-00:59 because that is the hour the word names; a four-hour block
#: called Midnight would be false at 03:00, and 03:00 is a real hour for a
#: digest that ran late. 01:00-04:59 says Late Night, which is what the owner
#: meant by asking for "midnight" -- the old table called 02:03 "Night", true
#: and useless. There is no "Early Morning": its band was exactly the morning
#: slot, so it would have renamed his normal news push, and at 13 characters it
#: was the longest word in the table, which is what eats the 72-character push
#: title's escalation-clause budget. Measured, not guessed; see
#: tests/render/test_digest_label.py.
LABEL_HOUR_BOUNDS = (
    (0, "Midnight"),
    (1, "Late Night"),
    (5, "Dawn"),
    (7, "Morning"),
    (12, "Noon"),
    (13, "Afternoon"),
    (17, "Evening"),
    (21, "Night"),
)

#: The committed schedule, read rather than restated -- see the long note on
#: the same constant in scripts/render_digest.py. The due minutes are NOT
#: written out here: `deploy/schedule.json` is already pinned against the
#: Worker's own CRON_DISPATCHER by Check 16 in
#: tests/workflows/test_workflow_lint.py, so reading it adds no drift surface,
#: while a literal "07:20" here would add one that nothing checks.
SCHEDULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deploy", "schedule.json")


def label_word_for_hour(hour):
    """The word for one NPT hour (0-23), read out of LABEL_HOUR_BOUNDS."""
    hour = int(hour)
    word = LABEL_HOUR_BOUNDS[0][1]
    for start_hour, candidate in LABEL_HOUR_BOUNDS:
        if hour >= start_hour:
            word = candidate
        else:
            break
    return word


def npt_label(instant_utc=None):
    """`(word, "HH:MM")` for an instant, both in Nepal time.

    UTC first, then the fixed +05:45 -- never the runner's local clock, which
    on a non-NPT box would be hours out in the one field the owner reads to
    tell morning from night.
    """
    if instant_utc is None:
        instant_utc = datetime.datetime.now(datetime.timezone.utc)
    elif instant_utc.tzinfo is None:
        instant_utc = instant_utc.replace(tzinfo=datetime.timezone.utc)
    npt = instant_utc.astimezone(datetime.timezone(KATHMANDU_UTC_OFFSET))
    return label_word_for_hour(npt.hour), npt.strftime("%H:%M")


def label_from_npt_stamp(stamp):
    """`(word, "HH:MM")` from an NPT `'YYYY-MM-DD HH:MM'` stamp. LOUD on junk.

    The silent `npt_label()` fallback this replaced read a SECOND clock to
    cover for a stamp it could not parse, so the email's label word and its
    freshness stamp could describe two different instants and both would look
    right. This renderer is pinned against render_digest.py's copy of the same
    function by tests/render/test_digest_label.py, and it is held to the same
    contract: an unusable stamp is named, not substituted.
    """
    try:
        parsed = datetime.datetime.strptime(str(stamp), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "label_from_npt_stamp got %r, which is not an NPT 'YYYY-MM-DD HH:MM' "
            "build stamp (%s). Refusing to fall back to the wall clock: the label "
            "word and the freshness stamp both come from this string, so "
            "guessing one would date the email to an instant it was never "
            "given. The stamp is written by validate_stories.py's "
            "kathmandu_now_str() -- fix the caller or the payload."
            % (stamp, exc)) from exc
    naive_utc = parsed - KATHMANDU_UTC_OFFSET
    return npt_label(naive_utc.replace(tzinfo=datetime.timezone.utc))


def digest_label(word, clock):
    """The rendered label for a MEASURED instant: `'Evening Digest · 19:25'`.

    The surfaces that really know the instant they name use this one -- the ntfy
    push, which renders microseconds before the POST. The email is built during
    the prebuild and does not; it uses `delivery_label`.
    """
    return "%s Digest · %s" % (word, clock)


def _hhmm_to_hour(hhmm):
    """The hour 0-23 in an `'HH:MM'` string, or raise ValueError naming it."""
    text = str(hhmm).strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(len(p) == 2 and p.isdigit() for p in parts):
        raise ValueError("got %r, which is not a 'HH:MM' clock time" % (hhmm,))
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError("got %r, which is not a 'HH:MM' clock time" % (hhmm,))
    return hour


def slot_due_npt(slot, schedule_path=None):
    """`'HH:MM'` Nepal time the committed schedule says this slot is DUE at.

    EMPTY STRING when there is no schedule to read: a slot the record does not
    name, a record that is absent or unparseable, or a cron that is not a fixed
    time of day. All three mean the same thing to a renderer, and none of them
    may be answered with a guessed minute.

    The digest slots in `deploy/schedule.json` are Cloudflare crons in UTC --
    `'35 1 * * *'` and `'35 13 * * *'` -- which are 07:20 and 19:20 Nepal time.
    Kept as a function rather than a constant so this renderer and the web
    renderer both answer from the one committed record instead of each keeping
    their own copy of two minutes.
    """
    path = SCHEDULE_PATH if schedule_path is None else schedule_path
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        cron = str(blob["dispatcher_crons"]["digest_slots"][str(slot)])
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    fields = cron.split()
    if len(fields) != 5:
        return ""
    minute_s, hour_s = fields[0], fields[1]
    # `*`, `1-5` and `*/2` are not a time of day.
    if not (minute_s.isdigit() and hour_s.isdigit()):
        return ""
    try:
        utc = datetime.datetime(
            2000, 1, 1, int(hour_s), int(minute_s), tzinfo=datetime.timezone.utc)
    except ValueError:
        return ""
    return utc.astimezone(
        datetime.timezone(KATHMANDU_UTC_OFFSET)).strftime("%H:%M")


def delivery_label(slot, build_npt_stamp, schedule_path=None):
    """`(label, word, clock)` for the email header, and nothing guessed.

    `'Evening Digest · due 19:20'` when the committed schedule names this slot,
    `'Late Night Digest · built 02:03'` when it does not. The word is derived
    from whichever clock is printed, so the label is one fact rather than two.
    Full reasoning and the rejected alternatives are on the identically named
    function in scripts/render_digest.py; this is the second of the two
    renderers, pinned against it by tests/render/test_digest_label.py.
    """
    due = slot_due_npt(slot, schedule_path)
    if due:
        word = label_word_for_hour(_hhmm_to_hour(due))
        return "%s Digest · due %s" % (word, due), word, due
    word, clock = label_from_npt_stamp(build_npt_stamp)
    return "%s Digest · built %s" % (word, clock), word, clock


def chip_html(label):
    """Inline-styled label chip (NEW/UPDATED/...); empty string when no label."""
    if not label:
        return ""
    return '<span class="chip" style="%s">[%s]</span>' % (CHIP_STYLE, esc(label))


def scheduled_delay_minutes(scheduled_hhmm, generated_at_npt):
    """Minutes by which actual generation lagged the scheduled 'HH:MM' time.

    Returns None when either value is missing/unparseable. Midnight wraparound
    is tolerated by picking the smallest non-negative delta among +/-1 day.
    """
    def hhmm_to_minutes(txt):
        m = re.match(r"^(\d{1,2}):(\d{2})$", (txt or "").strip())
        if not m:
            return None
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            return None
        return hh * 60 + mm

    sched = hhmm_to_minutes(scheduled_hhmm)
    gen_match = re.search(r"(\d{1,2}):(\d{2})\s*$", generated_at_npt or "")
    if sched is None or not gen_match:
        return None
    gen = int(gen_match.group(1)) * 60 + int(gen_match.group(2))
    delta = gen - sched
    for cand in (delta, delta + 1440, delta - 1440):
        if cand >= 0:
            return cand
    return abs(delta)


def story_dates(story):
    """Reader-worthy date fields for the meta line: the publication date and
    the deadline.

    `event_date` is deliberately NOT here. It is a collector key and it was
    printed as a literal "event: 2026-09-29" on every card. The deadline stays
    because it is the one date a reader acts on, and it is pinned by tests.
    """
    parts = []
    published = story.get("published_at")
    if published:
        parts.append(esc(published))
    if story.get("deadline"):
        parts.append("&#128197; Deadline: " + esc(story["deadline"]))
    return parts


def build_story_html(story):
    title = esc(story.get("title", ""))
    # READER SURFACE DISCIPLINE: `source_name` and `reliability` were collector
    # build-log fields reaching the inbox - the email printed "open-meteo" and
    # "tier2" under a story about rain. Both stay in the payload. The
    # publication date is kept because it is actionable.
    meta_bits = list(story_dates(story))
    meta_line = " &middot; ".join(meta_bits) if meta_bits else ""

    summary = esc(story.get("summary", ""))
    # DISPLAY-ONLY and OPTIONAL. Empty means render nothing. The previous code
    # emitted `<strong>Why it matters:</strong> ` unconditionally at line 325
    # and an empty `Why it matters: ` line in the plain text, so every story
    # the validator did not annotate shipped an empty bold heading on the
    # email / Gmail / Telegram path. The condition IS the fix.
    for_you = esc(str(story.get("for_you") or "").strip())

    links = []
    article_url, via_google = resolve_article_url(story)
    if article_url:
        links.append('<a href="%s" style="color:#2563eb; word-break:break-all;">%s</a>'
                     % (esc(article_url), esc(article_url)))
    for s in story.get("supporting_sources") or []:
        if isinstance(s, dict) and s.get("url"):
            label = esc(s.get("name") or s["url"])
            links.append('<a href="%s" style="color:#2563eb; word-break:break-all;">%s</a>'
                         % (esc(s["url"]), label))
    links_html = "<br>&#8226; ".join(links) if links else ""

    chip = chip_html(story.get("label"))
    # Deadline chips first in the badge row: the deadline is what must not be
    # missed. A Google News wrapper link is labelled so the reader knows the
    # URL is a redirect, not the publisher.
    if story.get("deadline"):
        chip += ('<span style="%sbackground-color:#dc2626;color:#ffffff;">'
                 '&#128197; Deadline %s</span>' % (CHIP_STYLE, esc(story["deadline"])))
    if via_google:
        chip += ('<span style="%sbackground-color:#475569;color:#eef;">'
                 'via Google News</span>' % CHIP_STYLE)
    h = []
    h.append('<div style="border:1px solid #e5e7eb;border-left:4px solid {accent};'
             'border-radius:6px;padding:12px 14px;margin:0 0 12px 0;background:#ffffff;'
             'font-family:{font};">'.format(accent="#d1d5db", font=FONT_STACK))
    h.append('  <div style="font-size:15px;font-weight:bold;color:#111827;'
             'line-height:1.35;margin:0 0 4px 0;">%s%s</div>' % (chip, title))
    if meta_line:
        h.append('  <div style="font-size:12px;color:#6b7280;margin:0 0 8px 0;">%s</div>' % meta_line)
    h.append('  <p style="margin:0 0 6px 0;font-size:13px;line-height:1.55;color:#374151;">%s</p>' % summary)
    if for_you:
        h.append('  <p style="margin:0 0 8px 0;font-size:13px;line-height:1.55;color:#4c1d95;">'
                 '<strong style="color:#5b21b6;">For you:</strong> %s</p>' % for_you)
    if links_html:
        h.append('  <div style="font-size:12px;line-height:1.6;color:#2563eb;">&#8226; %s</div>' % links_html)
    h.append('</div>')
    return "\n".join(h)


def build_story_text(story):
    lines = []
    label = story.get("label")
    prefix = "[%s] " % label if label else ""
    lines.append("* %s%s" % (prefix, story.get("title", "")))
    meta_bits = [story.get("source_name", ""), story.get("reliability", "")]
    meta_bits.extend([story.get("published_at"), ("event: " + story["event_date"]) if story.get("event_date") else None])
    if story.get("deadline"):
        meta_bits.append("\U0001F4C5 Deadline: %s" % story["deadline"])
    meta = " | ".join(b for b in meta_bits if b)
    if meta:
        lines.append("  %s" % meta)
    lines.append("  %s" % story.get("summary", ""))
    # Same contract as the HTML card: absent means the line is not emitted at
    # all. Never `  For you: ` with nothing after it.
    for_you = str(story.get("for_you") or "").strip()
    if for_you:
        lines.append("  For you: %s" % for_you)
    article_url, via_google = resolve_article_url(story)
    if article_url:
        lines.append("  Link: %s%s" % (article_url,
                                       "  (via Google News)" if via_google else ""))
    for s in story.get("supporting_sources") or []:
        if isinstance(s, dict) and s.get("url"):
            lines.append("  Link (%s): %s" % (s.get("name", ""), s["url"]))
    return "\n".join(lines)


def group_by_category(stories):
    grouped = {}
    for s in stories:
        grouped.setdefault(s.get("category", ""), []).append(s)
    return grouped


def render_sections_html(grouped, source_health=None):
    """A section renders if it has stories OR an alarm; a quiet empty section
    is omitted (`section_is_hidden`). Mirrors render_digest.render_sections."""
    blocks = []
    for key, header, color in SECTIONS:
        items = grouped.get(key) or []
        status, tone = section_status(key, len(items), source_health)
        if section_is_hidden(len(items), tone):
            continue
        block = ['<div style="margin:18px 0 10px 0;">',
                 '  <div style="border-left:4px solid %s;background-color:#f9fafb;'
                 'padding:8px 12px;font-size:13px;font-weight:bold;letter-spacing:0.06em;'
                 'color:#111827;font-family:%s;">%s</div>' % (color, FONT_STACK, header),
                 '</div>']
        for s in items:
            block.append(build_story_html(s))
        if status:
            # TONE, NOT JUST TEXT. "I checked and there was no news" and "the
            # source failed, so this may be a lie by omission" were the same red
            # box. The benign case is now a muted grey line with no red bar; the
            # alarm colours are opt-in. The email cannot use a stylesheet, so
            # the two tones are two inline style strings.
            block.append('  <div style="%s">%s</div>'
                         % (STATUS_ALARM_STYLE if tone == STATUS_ALARM
                            else STATUS_QUIET_STYLE, esc(status)))
        blocks.append("\n".join(block))
    return "\n".join(blocks)


def render_sections_text(grouped, source_health=None):
    """Same contract as render_sections_html, in plain text.

    The hiding rule matters MORE here, not less: render_digest.parse_email_text()
    reads this text back to build the page when stories.validated.json is absent,
    so a section kept here for cosmetics would come back as a section there.
    """
    blocks = []
    for key, header, _color in SECTIONS:
        items = grouped.get(key) or []
        status, tone = section_status(key, len(items), source_health)
        if section_is_hidden(len(items), tone):
            continue
        plain_header = (header.replace("&amp;", "&").replace("&#8212;", "--")
                              .replace("&middot;", "-"))
        block = ["== %s ==" % plain_header]
        for s in items:
            block.append(build_story_text(s))
        if status:
            # A section status is NOT a story, so it must not wear a story's
            # `* ` marker. It used to: render_digest.parse_email_text() treats
            # any `* ` line as a story title, so every empty section added a
            # phantom story and a real 4-story digest rendered 6 story cards -
            # which inflated the website hero AND the archive-index count that
            # parse_digest_categories() reads back off the page. A keyed,
            # non-bullet line cannot be mistaken for a title. The line is still
            # emitted, exactly once, under the section header.
            block.append("  status: %s" % status)
        blocks.append("\n\n".join(block))
    return "\n\n\n".join(blocks)


def main(argv=None, schedule_path=None):
    """Build the HTML and text email.

    `schedule_path` is optional and defaults to the committed
    `deploy/schedule.json`. It is a parameter so the no-schedule case can be
    exercised for real rather than argued about in a comment: point it at a
    record that does not name this slot and the header says `built` instead of
    `due`, which is the behaviour an off-schedule run gets.
    """
    ap = argparse.ArgumentParser(description="Build digest email from validated stories.")
    ap.add_argument("--in", dest="in_path", default="state/stories.validated.json")
    ap.add_argument("--out-html", dest="out_html", default="state/email.html")
    ap.add_argument("--out-text", dest="out_text", default="state/email.txt")
    ap.add_argument("--template", dest="template", default="templates/email.html")
    ap.add_argument("--candidates", dest="candidates",
                    default="state/candidates.json",
                    help="optional; read for a top-level `weather_advice` key "
                         "when the validated payload does not carry one")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.isfile(args.in_path):
        print("FATAL: input file not found: %s" % args.in_path)
        return 1
    try:
        with open(args.in_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print("FATAL: cannot read/parse input (%s): %s" % (args.in_path, exc))
        return 1

    stories = data.get("stories") or []
    if not isinstance(stories, list):
        stories = []
    digest_time = str(data.get("digest_time") or "")
    date_val = str(data.get("date") or "")
    # NOTE: research_status is deliberately NOT read. The agent's own verdict
    # is what let a TU-only outage pass as a normal day; the banner is now
    # derived from source_health, which code produces.

    try:
        with open(args.template, "r", encoding="utf-8") as f:
            template = f.read()
    except OSError as exc:
        print("FATAL: cannot read template (%s): %s" % (args.template, exc))
        return 1

    grouped = group_by_category(stories)
    source_health = data.get("source_health")
    notes = data.get("notes")

    # The weather engine's verdicts are written to state/candidates.json, not
    # to the validated payload, so BOTH are consulted (validated first) and
    # either may be absent. Absence is normal and silent: no advice means no
    # block, never a warning the owner cannot act on.
    candidates = {}
    if args.candidates and os.path.isfile(args.candidates):
        try:
            with open(args.candidates, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                candidates = loaded
        except (OSError, ValueError):
            candidates = {}
    weather = resolve_weather_advice(data, candidates)

    sections_html = render_sections_html(grouped, source_health)
    sections_text = render_sections_text(grouped, source_health)

    # The personal layer goes FIRST, ahead of every section: it is the only
    # part of the digest that is about him rather than about the world.
    personal_html = your_day_html(weather, stories, date_val) + weather_html(weather)
    personal_text = your_day_text(weather, stories, date_val)
    sections_html = personal_html + sections_html
    sections_text = personal_text + "\n\n" + sections_text

    if not stories:
        # The personal layer SURVIVES an empty digest. `Your day` is never
        # empty, and a day with no surviving stories is exactly the day he
        # most needs to be told whether anything is due.
        empty_html = ('<div style="padding:16px 14px;font-size:14px;color:#6b7280;'
                      'font-family:%s;text-align:center;">No items cleared filtering today.</div>'
                      % FONT_STACK)
        sections_html = personal_html + empty_html
        sections_text = personal_text + "\n\nNo items cleared filtering today."

    notice_html_block = notice_html(source_health, notes)
    notice_text_block = notice_text(source_health, notes)

    # Freshness line: the state decides which form is honest (CONTRACT C). The
    # scheduled/runner-started suffix is preserved for ok/partial/degraded and
    # is gated exactly as before (more than 10 minutes of runner lag).
    data_cutoff_npt = str(data.get("data_cutoff_npt") or "")
    scheduled = os.environ.get("SCHEDULED_TIME", "").strip()
    generated_at = str(data.get("generated_at_npt") or "")
    delay = scheduled_delay_minutes(scheduled, generated_at) if scheduled else None
    late = scheduled if (delay is not None and delay > 10) else ""
    data_cutoff_line = cutoff_line(data_cutoff_npt, source_health,
                                   scheduled=late, generated_at=generated_at)

    # THE WORD AND THE CLOCK THE OWNER READS, and the email says when this
    # message is DUE rather than when it was generated. The prebuild runs 40
    # minutes ahead of the slot, so `generated_at_npt` is true but is not the
    # minute the owner will read it -- the complaint that produced this.
    # `delivery_label` quotes the committed schedule when it names this slot and
    # falls back to `generated_at_npt` (named as the build time) when it does
    # not, and derives the WORD from whichever clock it prints. `digest_time` is
    # untouched and still the SLOT -- it is structural identity and it is what
    # update_history.py marks the stories against.
    label, label_word, _label_clock = delivery_label(
        str(data.get("digest_time") or ""), generated_at, schedule_path)

    html_out = (template
                .replace("{{DIGEST_TIME}}", esc(label))
                .replace("{{DATE}}", esc(date_val))
                .replace("{{STORY_COUNT}}", esc(len(stories)))
                .replace("{{DATA_CUTOFF}}", esc(data_cutoff_line))
                .replace("{{RESEARCH_NOTICE}}", notice_html_block)
                .replace("{{SECTIONS}}", sections_html))

    text_header_lines = [
        "DAILY MEDIA NEWS",
        "%s briefing -- %s" % (label_word.upper(), date_val or "?"),
        "Stories: %d" % len(stories),
    ]
    if data_cutoff_line:
        text_header_lines.append(data_cutoff_line)
    if notice_text_block:
        text_header_lines.extend(notice_text_block.split("\n"))
    text_out = "\n".join(text_header_lines) + "\n" + ("-" * 60) + "\n\n" + sections_text + "\n"

    for path, content in ((args.out_html, html_out), (args.out_text, text_out)):
        out_dir = os.path.dirname(os.path.abspath(path))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    print("build_email: %d story(ies) -> %s , %s" % (len(stories), args.out_html, args.out_text))
    state = health_state(source_health)
    if state != "ok":
        print("  source_health.state=%s: honesty banner included" % state)
    if notice_html_block and "Limited digest" in notice_html_block:
        print("  FATAL: legacy 'Limited digest' notice is back - must not ship")
    if not stories:
        print("  zero stories: minimal 'no items' email rendered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
