#!/usr/bin/env python3
"""render_digest.py — render the daily digest as a styled HTML page for GitHub Pages.

Reads the plain-text digest (state/email.txt) and the structured stories
(state/stories.validated.json), merges them (structured data wins when
available, otherwise falls back to parsing the plain text), and renders a
dark-themed HTML page matching docs/index.html (bg #1a1a2e, text #eee,
accent #00d4ff).

Sections are rendered in a FIXED order (matching build_email.py SECTIONS),
omitting empty ones. Each story is a card with title, meta (source, date,
reliability), summary (bullet points preserved), a CONDITIONAL `for_you`
line, and a clickable link.

PERSONAL LAYER (100% deterministic, zero new model-authored text)
-----------------------------------------------------------------
Two blocks near the top of every surface:

  Your day   deadlines due today/tomorrow, then up to 3 surviving `for_you`
             lines, then up to 2 weather advice lines - with the literal
             fallback `Nothing needs you today.` when nothing qualifies.
  Weather    the advice lines from scripts/weather_advice.py, VERBATIM and
             in the engine's own order, or nothing at all when the engine
             returned [] (its `[]` means "say nothing", not "say a warning").

This layer only SELECTS strings something else already produced and
screened. It never re-words a weather verdict, never re-ranks one (the
engine's list order IS its severity order), and never invents prose. A story
with no `for_you` renders no line at all - no label, no em-dash.

Optional per-story media (from stories.validated.json) is rendered when
present and degrades gracefully when absent:
  "image": { "url", "width", "height", "type", "source" }
  "video": { "url", "type", "source" }
YouTube video URLs are embedded via youtube-nocookie.com (lazy); direct
media files use an inline <video> tag; otherwise a thumbnail + "Watch" link
is shown. Images use native lazy loading with an onerror fallback that hides
broken images cleanly.

Outputs:
  docs/digests/YYYY-MM-DD-{morning|evening}.html   (the rendered page)
  docs/index.html                                  (regenerated archive index)

Stdlib only (html, datetime, os, re, json, glob). Windows-friendly.
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
    ("world_major",         "WORLD — MAJOR EVENTS",           "#0891b2"),
    ("local_city",        "LOCAL & CITY",                 "#0d9488"),
    ("education_tu",        "TU / BSC CSIT",                  "#2563eb"),
    ("college_local",       _pv.college_name(),               "#7c3aed"),
    ("bank_local",          _pv.bank_name(),                  "#16a34a"),
    ("nepal",               "IMPORTANT NEPAL",                "#ea580c"),
    ("gaming",              "GAMING",                         "#4f46e5"),
    ("anime",               "ANIME",                          "#db2777"),
    ("manga_manhwa_manhua", "MANGA · MANHWA · MANHUA",        "#9333ea"),
    ("tech_career",         "TECH / CAREER / OPPORTUNITIES",  "#475569"),
]

# THE DIGEST PAGE'S SECTION EMOJI ARE GONE, AND THE MAP IS GONE WITH THEM.
# This used to be SECTION_EMOJI, eleven entries, prepended to each section's
# label row by render_sections(). It is deleted rather than left unused on
# purpose: a constant that still holds all eleven glyphs, read by nothing, is
# one "harmless tidy" away from being read again, and the dot the approved
# design already draws is the only marker that section row is allowed.
#
# THE EMOJI STILL EXIST ON OTHER SURFACES AND ARE NOT AFFECTED BY THAT. They are
# separate implementations, not consumers of this map:
#   - the ntfy push, .github/workflows/daily-news.yml, carries its own copy of
#     all eleven inline in its heredoc;
#   - build_email.py puts no emoji on a section header at all.
# See render_sections()'s docstring and the emoji-absence test that guards this.

# Map plain-text section headers (as they appear in email.txt) to category keys.
# email.txt headers are the plain versions of the SECTIONS display headers.
HEADER_TO_KEY = {}
for _key, _header, _color in SECTIONS:
    plain = (_header.replace("&amp;", "&").replace("&#8212;", "--")
                    .replace("&middot;", "-"))
    HEADER_TO_KEY[plain] = _key
    HEADER_TO_KEY[_header] = _key

# ARCHIVE-INDEX palette: GONE, ON PURPOSE.
#
# This block used to hold a SECOND, private palette for `docs/index.html`
# (`BG #1a1a2e`, `ACCENT #00d4ff`, `CARD_BG #22223a`, `CARD_BORDER #2a2a4a`, ...)
# at a 1000px desktop width, and it was kept separate from the digest page's D_*
# set on purpose: the archive was the one surface the owner had not redesigned,
# and two palettes could not move together without silently restyling a page
# somebody had already approved.
#
# THE OWNER HAS SINCE APPROVED UNIFYING THEM. The archive is now the approved
# 390px mobile design on the approved palette, and the seven constants are
# deleted rather than re-pointed, because a same-valued alias would still be two
# names for one colour - i.e. still a place for the two surfaces to drift apart.
# `render_index` therefore reads the D_* constants BELOW directly. That is the
# stronger form of "they cannot diverge": there is no second value left to
# diverge from. `TestArchivePaletteIsTheApprovedPalette` in
# tests/render/test_render_mobile_design.py is the guard, rewritten from the
# retired "these must differ" premise to assert they now agree.
#
# `MUTED` and `DANGER` were DEAD even before this change - defined, read by
# nothing (verified by grep across the whole repo). They are not carried
# forward; there is no badge on the approved archive design for DANGER to
# colour.

# ---------------------------------------------------------------------------
# DIGEST-PAGE palette: the approved 390px mobile design.
# ---------------------------------------------------------------------------
# Frozen by the owner's sign-off on 2026-10-03. Reproduced, not interpreted:
# every value here is the approved hex and the tests assert these literals, so a
# later "tidying" of a colour fails the suite instead of quietly re-styling a
# page the owner has already approved.
#
# The whole page was 1100px wide with 260px images and coloured header bars,
# designed for a desktop and read on a phone. That is what these replace.
D_BG = "#0E1014"          # page background
D_SURFACE = "#171A21"     # the "Your day" card surface
D_SURFACE_HI = "#1F232C"  # thumbnails, count badge, NEW chip
D_LINE = "#272B34"        # hairline dividers
D_TEXT = "#ECEEF2"        # headlines
D_MUTED = "#9BA1AC"       # supporting sentence
D_DIM = "#6C727C"         # meta line, section labels
D_ACCENT = "#FF7A59"      # ONLY: "YOUR DAY" label, "NEEDS YOU" dot, NEW chip
D_SKY = "#6BA8F5"         # weather icon + temperature

#: The digest's font. `Inter` first, then the system stack that was already in
#: use, so a device without Inter renders the same page in the same spirit
#: rather than falling back to a serif.
D_FONT = ("'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
          "Oxygen, Ubuntu, sans-serif")

#: The place the digest is about, shown in the top-bar sub-line. Hardcoded for
#: the same reason SECTIONS above is: the weather source is a fixed open-meteo
#: point for Example City (`collect_news.py` hardcodes it in its query list) and
#: `breaking_monitor.py` hardcodes it in its severe-weather thresholds, so it is
#: a property of the project rather than a per-run setting. It is NOT read from
#: config/profile.yml, which this module deliberately does not read at all.
CITY = "Example City"

#: Where a temperature would be read from, if any producer emitted one.
#:
#: THE APPROVED DESIGN PUTS A TEMPERATURE NEXT TO THE WEATHER GLYPH AND THE
#: PAYLOAD HAS NO TEMPERATURE. This is not an oversight in the brief; it is a
#: gap in the data, and the honest shape of the requirement is a resolver that
#: returns a number only when the payload actually carries one. See
#: `weather_temperature()` for why parsing it out of the advice prose was
#: rejected instead.
WEATHER_TEMP_KEY = "temperature_c"


def esc(value):
    """HTML-escape any story-derived string."""
    return html.escape(str(value if value is not None else ""), quote=True)


# ---------------------------------------------------------------------------
# Honesty: source health -> what the reader is told (FROZEN CONTRACT B / C)
# ---------------------------------------------------------------------------
# The `source_health` object in state/stories.validated.json is consumed as
# DATA only (scripts/source_health.py is never imported). The state is driven
# by CODE, not by the research agent, so an outage can never be silent.
#
# `unknown` is the PERMANENT correct value when the field is absent, malformed
# or from a legacy build - it is what must render when candidates.json is
# missing or corrupt. It is not a stub.

HEALTH_STATES = ("ok", "partial", "degraded", "empty", "unknown")

# Digest-level banner, one line per state. `ok` renders nothing.
# `empty` is the "answered but produced nothing usable" state: a distinct claim
# from `unknown` (nothing was confirmed reachable) and from `degraded` (a host
# was unreachable). It gets its own literal and never reuses the unknown one.
HEALTH_BANNER = {
    "partial": "⚠️ Some sources failed — this digest may be missing items.",
    "degraded": "⚠️ Some sources were unreachable — this digest may be missing items.",
    "empty": "⚠️ Some sources answered, but returned nothing usable — treat this digest as incomplete.",
    "unknown": "⚠️ Source check unavailable — this digest may be missing items.",
}

# The no-AI fallback's page label. ONE literal, so the page, the email subject
# and the push cannot drift into three different descriptions of one edition.
UNSUMMARISED_LABEL = "Unsummarised edition (no AI today)"

# Freshness line (FROZEN CONTRACT C). `unknown` DROPS the timestamp entirely:
# if no source was confirmed reachable, claiming "checked through T" is the
# exact lie this renderer exists to remove. `empty` KEEPS the timestamp -- the
# sources did answer -- but suffixes it, because "checked through T" over
# garbage is still a partial claim.
CUTOFF_SUFFIX = {
    "ok": "",
    "partial": " · some sources failed",
    "degraded": " · some sources were unreachable",
    "empty": " (incomplete: some sources returned nothing usable)",
}

# Longest research-note text rendered (protects the 4096-byte ntfy budget).
NOTE_LIMIT = 240

# A Google News RSS redirect - an opaque wrapper, not the publisher's URL.
GOOGLE_NEWS_RE = re.compile(r"https?://(?:[a-z0-9\-]+\.)*news\.google\.com/rss/articles/",
                            re.IGNORECASE)

# Nepal Time is a fixed UTC+05:45 (no DST).
KATHMANDU_UTC_OFFSET = datetime.timedelta(hours=5, minutes=45)


def kathmandu_now_str():
    """Current time in Nepal (UTC+05:45) as 'YYYY-MM-DD HH:MM'.

    The naive datetime.now() this replaced rendered a footer clock 5h45m
    behind the hero's NPT cutoff on every single digest.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now_utc + KATHMANDU_UTC_OFFSET).strftime("%Y-%m-%d %H:%M")


# Nepal Time as a real tzinfo. Nepal is a fixed +05:45 with no DST, so this is
# exact forever and needs no zone database.
NPT_TZ = datetime.timezone(KATHMANDU_UTC_OFFSET)

#: THE ONE PLACE THE DIGEST'S HUMAN-FACING WORD IS DECIDED.
#:
#: The owner read "Evening" on a push that had fired at 02:03, because the word
#: was the SLOT (`--digest-time`), and the slot is structural identity, not a
#: claim about the clock. So the word now comes from the real instant and the
#: slot keeps its own job entirely -- see `label_word_for_hour`.
#:
#: Half-open, ascending, and covering all 24 hours: each pair is the FIRST hour
#: carrying that word. Every boundary is a WHOLE HOUR, so the table is a list of
#: hours to read rather than a pile of ranges to keep in agreement -- nine words,
#: nine entries, nothing wrapping and nothing out of order.
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
#: WHY EIGHT. Four were not enough, and the owner said so: "can you add morning
#: and evening for normal news but i can get news at mignight too so add night,
#: midnight, afternoon, noon and so on and it should be according to the time
#: other wise i might get news at 12am and it will be evening which is not
#: right." Every word he named is here. Each of the three old coarse bands is
#: split exactly once, at a whole hour, so the table is eight words and eight
#: entries with nothing wrapping and nothing out of order:
#:
#:   old Night   00:00-04:59 + 21:00-23:59  ->  Midnight, Late Night, Night
#:   old Morning 05:00-11:59                 ->  Dawn, Morning
#:   old Midday  12:00-16:59                 ->  Noon, Afternoon
#:   old Evening 17:00-20:59                 ->  unchanged
#:
#: WHY MIDNIGHT IS ONE HOUR LONG. Because that is the hour the word names. A
#: four-hour block called Midnight would be a lie at 03:00 -- and 03:00 is a
#: real hour for a digest that ran late, which is the whole reason the previous
#: table existed. The hour after midnight now says Midnight; 01:00-04:59 says
#: Late Night, which is what the owner meant when he asked for "midnight": the
#: old table called 02:03 "Night", which is true and useless.
#:
#: WHY THERE IS NO "EARLY MORNING". It was in the first draft of this table,
#: for 07:00-08:59, and it was cut for two measured reasons. Its band is
#: exactly the morning slot, so the owner's own "normal news" push would have
#: read "Early Morning" rather than the "morning" he asked for; and at 13
#: characters it is the longest word in the table, which is what pushed the
#: worst-case push title (and therefore the "and why" escalation clause, which is
#: only appended while the whole title still fits) toward the 72-character
#: ceiling. Tests/render/test_digest_label.py measures both tables' titles so
#: this cannot be re-litigated on opinion.
#:
#: WHAT THE WORDS ARE NOT. Not the slot, and not a forecast: the morning slot
#: firing at 02:03 says Late Night, and the evening slot firing at 07:20 says
#: Morning. The slot is structural identity -- see `digest_time` -- and it has
#: never been allowed to speak for the clock.
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

#: The committed schedule. NOT a copy of it: this is the file, read at render
#: time, and `slot_due_npt` is the only reader. It lives here rather than in a
#: literal table in each renderer because the record itself is already pinned
#: against the Worker's own CRON_DISPATCHER and wrangler.toml by Check 16 in
#: tests/workflows/test_workflow_lint.py -- so restating "07:20" in Python would
#: add a copy that nothing checks, while reading the record adds no drift surface
#: at all. That asymmetry is the whole reason this is the design and not option
#: (b) ("print the build time and call it a build time").
SCHEDULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deploy", "schedule.json")


def label_word_for_hour(hour):
    """The word for one NPT hour (0-23), read out of LABEL_HOUR_BOUNDS.

    The scan takes the LAST bound at or below `hour`, which is what makes the
    wrap at midnight work without a special case.
    """
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

    `instant_utc` may be aware or naive-UTC, because every clock in this repo
    hands over one or the other; a naive value is read as UTC. `None` means
    "now", taken from UTC and converted -- NEVER from the runner's local time.
    That is the whole reason this function exists rather than a
    `datetime.now()` call at the point of use: a naive local clock rendered a
    footer 5h45m behind the NPT cutoff on every digest, and on a developer box
    in any non-NPT zone it would repeat that exact bug in the one field the
    owner uses to tell morning from night.
    """
    if instant_utc is None:
        instant_utc = datetime.datetime.now(datetime.timezone.utc)
    elif instant_utc.tzinfo is None:
        instant_utc = instant_utc.replace(tzinfo=datetime.timezone.utc)
    npt = instant_utc.astimezone(NPT_TZ)
    return label_word_for_hour(npt.hour), npt.strftime("%H:%M")


def npt_stamp_to_utc(stamp):
    """`'YYYY-MM-DD HH:MM'` NPT (what `kathmandu_now_str` writes) -> aware UTC."""
    parsed = datetime.datetime.strptime(str(stamp), "%Y-%m-%d %H:%M")
    return (parsed - KATHMANDU_UTC_OFFSET).replace(tzinfo=datetime.timezone.utc)


def label_from_npt_stamp(stamp):
    """`(word, "HH:MM")` from an NPT stamp string. LOUD on an unusable one.

    The renderer already stamps every page with the instant it was built, so
    deriving the label from THAT string -- rather than reading a second clock --
    is what makes the word and the clock provably the same instant.

    THERE IS NO FALLBACK, AND THAT IS THE POINT. The `except` this replaced
    returned `npt_label()` -- the WALL CLOCK -- for a stamp it could not read,
    so a page handed a malformed build stamp rendered a title claiming whatever
    minute it happened to be looked at, which may be nothing like the minute it
    was built. That is the exact dishonesty this renderer exists to remove, and
    it was hiding behind an `except` that made it look like tolerance.

    A stamp this function cannot read is a caller defect, not weather: the only
    producer is `kathmandu_now_str`, which formats from a literal pattern and
    cannot produce junk. So it is raised, naming the offending value and the
    shape it must have, instead of being papered over with a clock the page
    never received.
    """
    try:
        return npt_label(npt_stamp_to_utc(stamp))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "label_from_npt_stamp got %r, which is not an NPT 'YYYY-MM-DD HH:MM' "
            "build stamp (%s). Refusing to fall back to the wall clock: the label "
            "word and the footer clock both come from this string, so guessing "
            "one would give the page a build time it never received. The stamp "
            "is written by kathmandu_now_str() -- fix the caller."
            % (stamp, exc)) from exc


def digest_label(word, clock):
    """The rendered label for a MEASURED instant: `'Evening Digest · 19:25'`.

    Used by the surfaces that really do know the instant they are naming -- the
    ntfy push, which renders microseconds before the POST. A page built during
    the prebuild does NOT use this one; it uses `delivery_label`.
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
    time of day. Empty is the honest answer for all three and it is what makes
    the page fall back to its build stamp rather than invent a delivery time.

    THE CRON IS READ, NEVER RESTATED. `deploy/schedule.json` carries
    `dispatcher_crons.digest_slots`, a Cloudflare cron in UTC; the digest slots
    are `'35 1 * * *'` and `'35 13 * * *'`, which are 07:20 and 19:20 Nepal
    time. Those minutes are not written out here because a literal '07:20' in
    three renderers is a number nothing checks, whereas the record this reads is
    already compared against the Worker's own dispatcher by Check 16 in
    tests/workflows/test_workflow_lint.py. If the slot moves, that check goes
    red before this function can go quietly wrong.

    WHY NOT A CONSTANT. The owner's complaint was a page reading '18:43' when
    the digest reached him at 19:25 -- the prebuild runs 40 minutes ahead of the
    slot, and a static page cannot learn its own send time because nothing
    rewrites it afterwards. The fix that is honest is to print the time the
    SCHEDULE promises, which is a fact that exists before the build starts, and
    to say out loud that it is a due time rather than a send time.
    """
    path = SCHEDULE_PATH if schedule_path is None else schedule_path
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        cron = str(blob["dispatcher_crons"]["digest_slots"][str(slot)])
    except (OSError, ValueError, KeyError, TypeError):
        # Absent file, unparseable JSON, missing section, unknown slot. All of
        # them mean the same thing to a renderer -- there is no schedule to
        # quote -- so none of them may invent one.
        return ""
    fields = cron.split()
    if len(fields) != 5:
        return ""
    minute_s, hour_s = fields[0], fields[1]
    # `*`, `1-5` and `*/2` are not a time of day, and guessing what a range means
    # would be exactly the fabrication this function exists to avoid.
    if not (minute_s.isdigit() and hour_s.isdigit()):
        return ""
    try:
        utc = datetime.datetime(
            2000, 1, 1, int(hour_s), int(minute_s), tzinfo=datetime.timezone.utc)
    except ValueError:
        return ""
    return utc.astimezone(NPT_TZ).strftime("%H:%M")


def delivery_label(slot, build_npt_stamp, schedule_path=None):
    """`(label, word, clock)` for the page/email header, and nothing guessed.

    `label` is ONE of two honest shapes, and which one it is depends only on
    whether the committed schedule names this build's slot:

      * `'Evening Digest · due 19:20'`  -- the slot has a schedule, so the time
        is read out of it and named as a DUE time. This is the owner's fix: the
        page stops claiming 18:43 for a digest he receives at 19:25, and it
        says `due` so nobody reads it as a claim about when it was SENT.
      * `'Late Night Digest · built 02:03'` -- no schedule for this build (an
        off-schedule run, a breaking run, or a slot the record does not name),
        so the only instant this renderer actually has is the one it was built
        at, and it says `built` for the same reason.

    THE WORD COMES FROM WHATEVER CLOCK IS PRINTED, never from the build stamp
    and never from the slot. That is what makes the word, the clock and the
    emoji one fact rather than three: there is a single instant on this surface
    and everything the reader sees is derived from it.

    WHY NOT (b), PRINT THE BUILD TIME AND LABEL IT. It is honest and it is the
    wrong answer for the owner: he asked whether 18:43 was a problem, and it is
    a real time, but it is 40 minutes earlier than the phone he reads it on.
    Labelling it does not move it. The due time is a fact that existed before
    this build started, which is the only kind of future time a static page can
    print without lying.

    LOUD ON A BAD CLOCK. A `build_npt_stamp` this cannot read is a caller
    defect and is raised, never covered for with a wall clock -- same contract
    as `label_from_npt_stamp`, for the same reason.
    """
    due = slot_due_npt(slot, schedule_path)
    if due:
        hour = _hhmm_to_hour(due)
        word = label_word_for_hour(hour)
        return "%s Digest · due %s" % (word, due), word, due
    word, clock = label_from_npt_stamp(build_npt_stamp)
    return "%s Digest · built %s" % (word, clock), word, clock


def plural(n, singular, many=None):
    """Return the correctly pluralised noun for n. Deletes the naive-'s' bug."""
    if many is None:
        many = singular + "s"
    return singular if n == 1 else many


def _int(value):
    """Best-effort int, never raising on None/junk from the health payload."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def health_state(source_health):
    """Normalise the source_health payload to one of HEALTH_STATES.

    Absent, non-dict, empty or unrecognised input all resolve to `unknown`,
    which is the honest answer rather than a silent `ok`.
    """
    if not isinstance(source_health, dict):
        return "unknown"
    state = str(source_health.get("state") or "").strip().lower()
    return state if state in HEALTH_STATES else "unknown"


def health_banner(source_health):
    """Banner line for the current state, or '' for `ok`."""
    return HEALTH_BANNER.get(health_state(source_health), "")


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
    shown = hosts[:cap]
    text = "Down: %s" % ", ".join(shown)
    if len(hosts) > cap:
        text += " (+%d more)" % (len(hosts) - cap)
    return text


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


#: How a section status line should LOOK. A reader cannot act on an alarm
#: about a section that was never tracked, so the two are now distinguishable:
#:   alarm - a source the owner actually tracks could not be checked. Act on it.
#:   quiet - the check ran and there was nothing, or there was nothing to check.
#: `""` is returned for "say nothing at all" and carries no tone.
STATUS_ALARM = "alarm"
STATUS_QUIET = "quiet"

#: B3a: the benign "there was nothing configured to look at" line.
#:
#: A category with NO entry in `by_category` was never tracked - there was
#: never anything to check, so "this section may be incomplete" was a false
#: alarm. It fired on the real 2026-09-29 digest for 5 of 11 sections (anime,
#: gaming, manga and two more), which put 5 red boxes on a 3-story page and
#: made a working digest read as broken. The section still SAYS something,
#: because silence here is indistinguishable from a render bug, but it says it
#: in the quiet tone.
UNTRACKED_STATUS = "No sources are configured for this section."


def _status_is_untracked(category, source_health):
    """True when `category` is absent from a health map we can actually trust.

    UNTRACKED, not UNKNOWN. The distinction is whether the payload lets us tell
    "no sources are configured for this category" apart from "we could not
    measure this category":

      by_category is a dict, the category key is absent, and the digest-level
      state is not `unknown`  -> UNTRACKED. The collector ran and reported
                                  nothing to check. Quiet.

      source_health absent / malformed, or state == "unknown"  -> we cannot
                                  distinguish the two, so the honest B3 alarm
                                  stands. UNKNOWN.

    So a missing, corrupt or `unknown` health object keeps every alarm it had
    before; only a trustworthy payload that positively omits the category goes
    quiet.
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
    A section therefore NEVER vanishes silently: B5 turns "I know I am
    blind" into "here is exactly where to look", which still works during a
    total DNS outage.

    THE B3a SPLIT (owner-approved). B3 used to cover BOTH "no category entry"
    and "sources were skipped", which put a false "may be incomplete" on every
    category the owner never configured a source for. B3a is that first case
    only, in the quiet tone; the alarm is now reserved for a source the owner
    TRACKS and that genuinely could not be checked. The alarm literals
    themselves are unchanged, so anything reading them verbatim keeps working.

    THE B4/B6 INVARIANT. B4 -- the confident unqualified "nothing new" -- is
    the NARROWER claim: it is reachable only when every reachable host in that
    category produced items. A category whose hosts answered with nothing
    usable falls to B6, which says the same "nothing new" but ADDS that the
    source was checked and returned nothing usable. B4's literal is unchanged;
    only its tone changed (both B4 and B6 are benign facts, never an alarm).
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

    # No items: never claim success without evidence.
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
            # B5 with nowhere to point: the other honest literal, not a
            # fabricated URL and not a false "nothing new".
            return ("⚠️ Source status unknown — this section may be incomplete.",
                    STATUS_ALARM)
        return ("⚠️ Source unreachable — check %s" % " or ".join(urls), STATUS_ALARM)
    if _empty_hosts_of(entry):
        # ONE LINE, DELIBERATELY. This literal lives in three places and must
        # match character for character; when it was wrapped across two source
        # lines here it was not greppable as a whole, so a tool (or a person)
        # comparing the three surfaces could only confirm it by evaluating the
        # code. Kept contiguous so `grep` finds the same string in
        # render_digest.py, build_email.py and the ntfy heredoc.
        return ("Nothing new from this source today (checked — the source returned nothing usable).", STATUS_QUIET)
    return ("Nothing new from this source today.", STATUS_QUIET)


def section_status_line(category, item_count, source_health):
    """The status TEXT alone, for the plain-text surfaces and every consumer
    that only needs the claim. `section_status` is the full answer."""
    return section_status(category, item_count, source_health)[0]


def section_is_hidden(item_count, tone):
    """True when a section must be OMITTED from the rendered page entirely.

    THE OWNER'S RULE, verbatim: "if there is no news then do not display
    Gaming - Nothing new from this source. just don't display it." A quiet
    status on a phone is noise: the reader cannot act on "nothing new" and
    cannot act on "no sources are configured", and on the real 2026-09-29
    digest this produced 5 dead sections on a 3-story page.

    SO A QUIET EMPTY SECTION IS DROPPED, AND ONLY THAT. `tone` is the whole
    decision, and the split is the load-bearing part:

      hidden  -- no items AND a QUIET status. That is exactly the three benign
                 cases in section_status(): B3a (nothing configured), B4
                 (checked, nothing new) and B6 (checked, source returned
                 nothing usable).
      RENDERED -- an ALARM, with or without items. B2, B3 and B5 mean a source
                 the owner TRACKS could not be checked, or has gone dark. That
                 is the only signal that a configured feed broke, it is
                 actionable, and hiding it would convert a repairable outage
                 into silence. The owner's request was about a quiet message,
                 not about alarms.

    CONSEQUENCE, STATED SO IT IS NOT A SURPRISE LATER: a page where every
    section is quiet and empty renders no <section> at all. That is correct --
    there is nothing to say and no alarm to raise -- and the page-level
    `health_banner` still reports the run state, so an "empty" run still says
    so in one line above the sections rather than eleven times below them.
    """
    return item_count <= 0 and tone == STATUS_QUIET


def cutoff_line(cutoff, source_health, scheduled="", generated_at=""):
    """FROZEN CONTRACT C: the honest freshness line."""
    state = health_state(source_health)
    if state == "unknown":
        return "⚠️ Freshness unverified — no source was confirmed reachable"
    if not cutoff:
        return ""
    line = "Information checked through: %s NPT" % cutoff
    suffix = CUTOFF_SUFFIX.get(state, "")
    if suffix:
        line += suffix
    if scheduled and generated_at:
        gen_hhmm = generated_at.strip()[-5:] if len(generated_at.strip()) >= 5 else generated_at
        line += " (scheduled %s · runner started %s)" % (scheduled, gen_hhmm)
    return line


#: Build-log tokens that must never reach a reader surface. The research note
#: is written by the collector and is genuinely useful, but it names the
#: collector's own vocabulary: `notice:<host>` log prefixes and Python
#: exception class names. Both are rewritten for DISPLAY only - the payload and
#: the logs keep the raw text, and nothing here claims or hides a failure: the
#: host that failed is still named, and a timeout is still called a failure.
NOTE_NOTICE_HOST_RE = re.compile(r"\bnotice:([A-Za-z0-9._-]+)")
NOTE_EXCEPTION_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception|Timeout)\b")
#: The collector's own name for itself, dropped so the note reads as a sentence.
NOTE_COLLECTOR_LEAD_RE = re.compile(r"^Collector:\s*", re.IGNORECASE)
#: The collector's source IDs for the RSS feeds, e.g. `feed:gaming-pcgamer`.
#: They reach the note as `notice:feed:<slug>`, and stripping `notice:` left the
#: `feed:<slug>` token behind. A slug is build vocabulary, not a source a reader
#: can visit -- unlike a hostname, which is kept -- so the whole token goes.
NOTE_FEED_RE = re.compile(r"\bfeed:[A-Za-z0-9._-]+\s*")


def reader_note_text(notes):
    """The note with build-log vocabulary rewritten for a reader.

    `notice:tu.edu.np` -> `tu.edu.np` (the host is the useful part; the
    `notice:` prefix is the collector's log key, and it was reaching the page
    verbatim). `TimeoutError` -> `a timeout` (a Python class name is not a
    sentence). `Collector: ` is dropped for the same reason, and so is a
    `feed:<slug>` source ID, which `notice:` used to leave behind. Deterministic,
    total, and a no-op for any note that does not contain these tokens - which
    is why the existing note tests are untouched.
    """
    text = str(notes or "").strip()
    if not text:
        return ""
    text = NOTE_COLLECTOR_LEAD_RE.sub("", text)
    text = NOTE_NOTICE_HOST_RE.sub(r"\1", text)
    text = NOTE_FEED_RE.sub("", text).strip()
    return NOTE_EXCEPTION_RE.sub("a timeout", text)


def truncate_words(text, limit):
    """`text` cut to at most `limit` chars, on a WORD boundary where there is
    one, with a single-character ellipsis.

    The old `text[:limit - 1] + "…"` cut mid-word and shipped "…a fetch of the
    IOE Examination Control Divis…" to the page. A note with no whitespace at
    all (a long token) still gets a hard cut, so the cap stays a hard cap.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    boundary = cut.rfind(" ")
    # Only honour a boundary that does not throw away most of the budget;
    # otherwise the cap would stop being a cap.
    if boundary >= max(1, int(limit * 0.6)):
        cut = cut[:boundary]
    return cut.rstrip() + "…"


#: The note's own label. `capped_note` prepends it, and the fold in
#: `render_health_block` re-uses the same string so the body can drop it again.
NOTE_LABEL = "⚠️ Research note: "


def capped_note(notes, limit=NOTE_LIMIT):
    """`⚠️ Research note: ...` capped at `limit` chars of note text, or ''.

    DISPLAY-ONLY sanitising happens BEFORE the cap, so the cap is still a hard
    ceiling on what the reader sees and a long note cannot push the page over
    it.
    """
    text = reader_note_text(notes)
    if not text:
        return ""
    if len(text) > limit:
        text = truncate_words(text, limit)
    return NOTE_LABEL + text


def resolve_article_url(story):
    """(url, via_google_news) for a story's primary link.

    A news.google.com/rss/articles/ URL is a redirect wrapper that tells the
    reader nothing; when the story carries a supporting source we point at the
    real article instead and label the card `via Google News`. Detected at
    RENDER time on purpose - no new collector field.
    """
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
# The personal layer: `Your day` and the weather block.
#
# WHY THIS EXISTS
# ---------------
# The owner said the digest "reads like a book" and never told him what
# anything meant FOR HIM. These two blocks are the fix, and they are built
# from strings that already passed someone's screen - the validator's
# `for_you`, the deterministic weather engine's `text` - by ordinary
# comparison. No model is called, no number is recomputed, no sentence is
# rewritten. That is the whole safety property: an advice line he acts on has
# to be reproducible, so this layer may only SELECT, never AUTHOR.
#
# The identical logic is duplicated verbatim in build_email.py and in the ntfy
# heredoc in .github/workflows/daily-news.yml, and tests/render holds all four
# surfaces to the same literals. Duplication is deliberate: the four surfaces
# are four separate implementations by design (the ntfy push does not go
# through render_sections_text at all), and the alternative - one shared
# module - would still have to be inlined into the workflow heredoc anyway.
# ---------------------------------------------------------------------------

# THE NO-DUPLICATION RULE (read this before adding a fifth place for weather)
# ---------------------------------------------------------------------------
# INVARIANT: no weather string appears TWICE in one rendered output, and the
# weather is never silently absent.
#
# The weather block and `Your day` are both rendered on this surface, so they
# must never carry the same string. The split is:
#
#   * `Your day` holds deadlines and `for_you` lines ONLY.
#   * The weather appears exactly ONCE, in its own clearly-labelled block.
#
# The same split is applied on the ntfy push, which is a separate heredoc
# implementation in .github/workflows/daily-news.yml. It was tempting to put
# the weather inside `Your day` there and drop the extra heading to save bytes
# on a 4096-byte push, but that surface's `YOUR_DAY_MAX_LINES` is 3 and the cap
# trims from the end, so weather placed in that block is clipped off the moment
# the owner has three deadlines or for_you lines - on exactly the busiest days.
# The weather vanished from 14 consecutive digests once already, silently. One
# heading is not worth that failure mode, and one rule across four surfaces is
# one rule to keep true.
#
# `include_weather` is the single switch, and the call sites below pass it
# EXPLICITLY rather than relying on a default, so that adding a surface forces
# the author to state which contract it is honouring.
FOR_YOU_LABEL = "For you"
YOUR_DAY_LABEL = "Your day"
WEATHER_LABEL = "Weather"
#: The fallback is four words and fires ONLY on a genuine nothing. It is not
#: padding: the deadline rule and the fallback rule together make the block
#: never empty, which is the only thing that would make it worse than absent.
NOTHING_TODAY = "Nothing needs you today."

#: Per-type caps inside `Your day`, applied on every surface.
FOR_YOU_MAX = 3
WEATHER_MAX = 2

#: Which EXISTING categories are treated as obligation-bearing when choosing
#: which `for_you` lines survive into the 3-line box. This is a selection
#: ORDER, not a new content category: it adds no category, it only says that
#: a line about a deadline-bearing topic outranks a line about a game release
#: when both carry a `for_you`. The pipeline's own `priority` breaks the tie.
ACTION_CATEGORIES = (
    "action", "education_tu", "college_local", "bank_local",
    "tech_career", "local_city",
)

#: The ntfy push budget (4096 bytes) reserves this much for the personal layer
#: as a whole. Unused by the rich surfaces, which have no byte ceiling; the
#: constants live here so both renderers quote the same numbers.
#:
#: `Your day` has a LINE cap and deliberately NO byte cap of its own. The
#: push's byte ceiling is PERSONAL_MAX_BYTES, and it is measured on the
#: RENDERED block, so it is the correct outer bound. An inner byte cap
#: strictly tighter than the outer one has exactly one behaviour: it drops a
#: deadline line while hundreds of bytes of the real ceiling are still free.
#: That is not a tighter budget, it is a silent missed deadline.
YOUR_DAY_MAX_LINES = 3
WEATHER_MAX_BYTES = 400
PERSONAL_MAX_BYTES = 1000

_DATE_PREFIX_LEN = 10


def parse_day(value):
    """'YYYY-MM-DD' (or anything starting with it) as a date, else None.

    Deadlines are authored freely - '2026-10-01', '2026-10-01 17:00',
    'Oct 1' - so only the ISO prefix is trusted. An unparseable deadline is
    NOT treated as today or tomorrow: guessing would put a line in the box
    that may be months away, and this block exists to remove noise, not add
    it.
    """
    text = str(value or "").strip()
    if len(text) < _DATE_PREFIX_LEN:
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def deadline_due_on(deadline, today):
    """'YYYY-MM-DD' the deadline falls on (its ISO date), or ''.

    '' means "not today or tomorrow, or not a date at all" - both mean the
    story does not belong in `Your day`.
    """
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

    Tolerant by design, because two files are in play and either may be
    absent: the validated payload and state/candidates.json (a top-level
    `weather_advice` key). The first payload that carries a NON-EMPTY list
    wins; everything else yields []. The engine returns [] to mean "say
    nothing at all", and that is honoured exactly - there is deliberately no
    "couldn't fetch weather" line anywhere in this function.

    Each item is a {"id", "text", "priority"} dict from the engine. A bare
    string is accepted too, so a hand-written or future payload shape cannot
    take the whole block down. No re-ranking: the engine already sorted by
    severity, and re-sorting here would be a second, divergent severity rule.
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
    """[(index, title, due_iso)] for stories due today or tomorrow, in input
    order. Sorted by due date so the nearest obligation reads first."""
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
    """Up to `limit` `for_you` lines, best first.

    Ordering is deterministic and three-deep: obligation-bearing category
    first, then the pipeline's own `priority` descending, then input order.
    A story with no `for_you` is skipped silently - there is no placeholder,
    because a placeholder is exactly the "empty bold label" defect this
    replaces. Stories already shown as a deadline line are excluded so a
    3-line box never says the same story twice.
    """
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
    output. `Your day` needs that: shedding every line because one was
    oversized would then render the fallback `Nothing needs you today.`
    beside a live deadline, which is a lie. The weather block does NOT set
    it, because for weather silence really is the correct answer.
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

    Priority order is the requirement, not a preference: a time-critical
    obligation must never be crowded out by weather, so deadlines come
    first, then the surviving `for_you` lines, then weather.

    `max_lines` is YOUR_DAY_MAX_LINES and EVERY production call site passes it,
    on every surface, so the same fixture yields the same set of lines
    everywhere. The default stays `None` only so the unit tests can exercise
    the uncapped ordering; nothing that renders a digest relies on the
    default. `max_bytes` is left unset by every call site on purpose - see
    YOUR_DAY_MAX_LINES: a byte cap on this block can only silently drop a
    deadline, because the push's real ceiling (PERSONAL_MAX_BYTES) is measured
    on the rendered block and is strictly larger.

    `include_weather` is the NO-DUPLICATION rule (see the comment above
    FOR_YOU_LABEL): True on a surface where this block is the only place the
    weather appears (the ntfy push), False where a labelled weather block
    renders it instead (website, email). It is a keyword argument with a
    default only so the four existing unit tests keep reading naturally.

    THE ONE WAY THIS RETURNS NOTHING
    --------------------------------
    With `include_weather=False` and a non-empty `weather`, this block has
    nothing of its own to say and the weather is being rendered elsewhere. It
    then returns `[]` so the caller omits the block, rather than printing the
    literal `Nothing needs you today.` directly above a "Take an umbrella"
    line that plainly does need him. A fallback that contradicts the next
    block is worse than no fallback, and that contradiction is exactly the
    kind of quiet untrustworthiness the owner complained about.
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
    """Weather block body lines - the engine's strings, in its order, or
    NOTHING when the engine returned nothing."""
    if not weather:
        return []
    return _cap_lines(list(weather)[:WEATHER_MAX], max_lines, max_bytes)


# Map section headers (as they appear in digest HTML, normalized to the
# plain-text form) to the short labels used by the ntfy teaser in the
# workflow. Reused verbatim so the archive summary matches the teaser.
SECTION_TO_LABEL = {
    "ACTION / IMPORTANT": "breaking",
    "WORLD -- MAJOR EVENTS": "world",
    "LOCAL & CITY": "local",
    "TU / BSC CSIT": "education",
    "IMPORTANT NEPAL": "nepal",
    "GAMING": "gaming",
    "ANIME": "anime",
    "MANGA - MANHWA - MANHUA": "manga",
    "TECH / CAREER / OPPORTUNITIES": "tech",
}


def _normalise_header(name):
    """The plain-text key form used by SECTION_TO_LABEL and section_label."""
    plain = (name or "").strip()
    # Headers are HTML-escaped in the rendered page (e.g. "LOCAL &amp; CITY"),
    # so unescape before matching against the plain-text labels.
    plain = html.unescape(plain)
    # Strip leading emoji / non-ASCII decoration added by the renderer.
    plain = re.sub(r"^[^\x00-\x7f]+", "", plain).strip()
    return plain.replace("\u2014", "--").replace("\u00b7", "-")


# The two institution headers are configured, so their mapping is registered
# from the SAME value the renderer prints. A hardcoded key here would stop
# matching the moment the deployment set its own name.
SECTION_TO_LABEL[_normalise_header(_pv.college_name())] = "college"
SECTION_TO_LABEL[_normalise_header(_pv.bank_name())] = "bank"


def section_label(name):
    """Map a section header (as it appears in digest HTML) to a short label.

    Digest HTML uses the display headers (em-dash / middot punctuation) and
    may carry a leading emoji decoration added by the renderer; strip any
    leading non-ASCII characters, then normalize to the plain-text form used
    in SECTION_TO_LABEL. Returns None if the header is not a known section.
    """
    return SECTION_TO_LABEL.get(_normalise_header(name))


def parse_digest_categories(path):
    """Parse a rendered digest HTML file and return category counts.

    Returns a dict mapping short label -> story count, or None if the file
    cannot be read. Uses the same section->label mapping as the ntfy teaser.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    counts = {}
    # Split on the section-header h2; parts[0] is the preamble, then the
    # name and body alternate. Each section body holds <article class="card">
    # blocks that we count.
    parts = re.split(r'<h2 class="section-header"[^>]*>(.*?)</h2>', content)
    for i in range(1, len(parts), 2):
        name = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        label = section_label(name)
        if label is None:
            continue
        count = len(re.findall(r'<article class="card">', body))
        if count:
            counts[label] = counts.get(label, 0) + count
    return counts


def first_thumbnail(path):
    """Extract the first story-card thumbnail src from a rendered digest page.

    Returns the URL string, or None if the file cannot be read or has no
    story thumbnail.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r'<img class="card-thumb"[^>]*src="([^"]+)"', content)
    return m.group(1) if m else None


def build_summary(counts):
    """Build a one-line category-count summary (same format as the ntfy teaser).

    The label vocabulary (breaking, world, gaming, ...) is already a fixed
    display noun, so nothing is appended: the old naive "%ss" could only
    ever produce "breakings" and "gamings".
    """
    if not counts:
        return "no stories"
    parts = []
    for label, count in sorted(counts.items(), key=lambda x: -x[1]):
        if count > 0:
            parts.append("%d %s" % (count, label))
    return ", ".join(parts)


#: The keyed prefix build_email.render_sections_text() writes a section status
#: with. A status is a statement ABOUT a section, never a story, so it never
#: uses the `* ` story marker and can never be parsed as a title. B6 (the
#: `empty` state) is in the alternation too, so a v2 status line in an older
#: text digest is still recognised as a status and never counted as a story.
STATUS_LINE_RE = re.compile(r"^\s+status:\s")

#: A section status as written by a build_email that predates
#: STATUS_LINE_PREFIX, where it was a `* ` bullet - indistinguishable in shape
#: from a story title. These alternatives are the COMPLETE output set of
#: section_status_line() (cases B2-B6), so this is an exact match, not a
#: heuristic. Only a text digest written by an older build can reach it.
#: UNTRACKED_STATUS is in the set because a text digest written by a build
#: that shipped the quiet B3a line would otherwise have that line promoted to a
#: story title by the fallback parser - the same count defect, reintroduced.
LEGACY_STATUS_TITLE_RE = re.compile(
    r"^(?:%s)$" % "|".join((
        r"⚠️ \d+ of \d+ sources for this section were unreachable\.",
        r"⚠️ Source status unknown — this section may be incomplete\.",
        r"⚠️ Source unreachable — check \S+(?: or \S+)*",
        r"Nothing new from this source today\.",
        r"Nothing new from this source today \(checked — the source returned "
        r"nothing usable\)\.",
        re.escape(UNTRACKED_STATUS),
    )))


def is_section_status_line(line):
    """True when `line` is a section status rather than a story.

    THE COUNT CONTRACT
    ------------------
    Every number the owner sees comes from the story list this function
    protects: the website hero (`len(stories)`), the archive index and its
    per-category summary (`parse_digest_categories`, which counts
    `<article class="card">` - one card per story), the email, and the ntfy
    push. A section status entering that list is not a cosmetic slip: it
    invents a story on every surface at once, and it did - a 4-story digest
    reported 6 because two empty sections each contributed a status line
    wearing a `* ` bullet.

    So the check belongs HERE, at the point where text becomes data, and not
    in each consumer. A status line must also never be absorbed as a story's
    summary continuation, so the caller closes the current story first.
    """
    raw = line or ""
    if STATUS_LINE_RE.match(raw):
        return True
    m = re.match(r"^\s*\*\s*(?:\[([^\]]*)\]\s*)?(.*)$", raw)
    if not m:
        return False
    return bool(LEGACY_STATUS_TITLE_RE.match(m.group(2).strip()))


def parse_email_text(text):
    """Parse the plain-text digest into a list of story dicts.

    Format (as produced by build_email.py):
      == SECTION NAME ==
      * [LABEL] Title
        source | reliability | date | event: date
        summary line(s)
        For you: ...
        Link: url
        Link (Name): url

    Returns a list of dicts with keys: title, label, source_name,
    reliability, published_at, event_date, summary, for_you,
    source_url, supporting_sources, category.

    `for_you` is DISPLAY-ONLY and optional. A story without one must render
    nothing at all, so the key exists and is empty rather than absent being
    special-cased by every caller. The legacy `Why it matters:` label is still
    accepted so an email.txt written by an older build round-trips instead of
    silently losing the line.
    """
    stories = []
    current_key = None
    current = None

    def flush():
        nonlocal current
        if current and current.get("title"):
            stories.append(current)
        current = None

    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue

        # Section header: == SECTION NAME ==
        m = re.match(r"^==\s*(.+?)\s*==$", line)
        if m:
            flush()
            header = m.group(1).strip()
            current_key = HEADER_TO_KEY.get(header)
            continue

        # A section status line is not a story (FROZEN CONTRACT B). It is
        # checked before the story marker so an empty section can never
        # contribute a phantom story, and `flush()` keeps it out of the
        # previous story's summary.
        if is_section_status_line(line):
            flush()
            continue

        # Story start: * [LABEL] Title  (or * Title)
        m = re.match(r"^\*\s*(?:\[([^\]]*)\]\s*)?(.*)$", line)
        if m and current_key is not None:
            flush()
            current = {
                "title": m.group(2).strip(),
                "label": (m.group(1) or "").strip(),
                "category": current_key,
                "source_name": "",
                "reliability": "",
                "published_at": "",
                "event_date": "",
                "summary": "",
                "for_you": "",
                "source_url": "",
                "supporting_sources": [],
            }
            continue

        if current is None:
            continue

        # Meta line: source | reliability | date | event: date
        m = re.match(r"^\s{2,}(.+)$", line)
        if m and not current["source_name"] and "|" in line:
            bits = [b.strip() for b in line.split("|")]
            current["source_name"] = bits[0] if bits else ""
            if len(bits) > 1:
                current["reliability"] = bits[1]
            for b in bits[2:]:
                if b.startswith("event:"):
                    current["event_date"] = b[len("event:"):].strip()
                elif b and not current["published_at"]:
                    current["published_at"] = b
            continue

        # For you: ... (and the legacy "Why it matters:" label, so an
        # email.txt written by an older build keeps its line)
        m = re.match(r"^\s*(?:For you:|Why it matters:)\s*(.*)$", line)
        if m:
            current["for_you"] = m.group(1).strip()
            continue

        # Link lines
        m = re.match(r"^\s*Link(?:\s*\(([^)]*)\))?:\s*(.*)$", line)
        if m:
            url = m.group(2).strip()
            name = (m.group(1) or "").strip()
            if name:
                current["supporting_sources"].append({"name": name, "url": url})
            elif not current["source_url"]:
                current["source_url"] = url
            continue

        # Summary continuation (indented, not a recognized field)
        if line.startswith("  "):
            if current["summary"]:
                current["summary"] += " " + line.strip()
            else:
                current["summary"] = line.strip()

    flush()
    return stories


def load_structured(path):
    """Load stories.validated.json; returns (stories_list, meta_dict)."""
    meta = {}
    stories = []
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            meta = data
            raw = data.get("stories") or []
            if isinstance(raw, list):
                stories = raw
    return stories, meta


def merge_stories(structured, parsed):
    """Merge structured and parsed stories.

    Structured data wins when available. Matching is done by title (case-
    insensitive, whitespace-normalized). Parsed stories that have no
    structured counterpart are appended (with their parsed fields).
    """
    def norm(s):
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    by_title = {}
    for s in structured:
        if isinstance(s, dict) and s.get("title"):
            by_title.setdefault(norm(s.get("title")), s)

    merged = []
    seen = set()
    for s in structured:
        if isinstance(s, dict) and s.get("title"):
            merged.append(s)
            seen.add(norm(s.get("title")))

    for p in parsed:
        key = norm(p.get("title"))
        if key in seen:
            continue
        # No structured counterpart — use parsed fields.
        merged.append(p)
        seen.add(key)

    return merged


def youtube_video_id(url):
    """Extract a YouTube video ID from common URL forms, or None.

    Handles youtube.com/watch?v=, youtube.com/embed/, youtube.com/shorts/,
    and youtu.be/ short links.
    """
    if not url:
        return None
    m = re.search(
        r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})",
        url)
    return m.group(1) if m else None


def topbar_subline(date_val):
    """"Fri 3 Oct · Example City" — the approved top-bar sub-line.

    `d.day` rather than `%d` because the approved string is "3 Oct", not
    "03 Oct". An unparseable date falls back to the raw value beside the city
    rather than raising: the sub-line is decoration on a page that must render
    regardless of what `--date` was handed.
    """
    try:
        d = datetime.datetime.strptime(str(date_val), "%Y-%m-%d")
        return "%s %d %s · %s" % (d.strftime("%a"), d.day, d.strftime("%b"), CITY)
    except (TypeError, ValueError):
        return "%s · %s" % (date_val, CITY)


def weather_icon_svg():
    """A 15px lucide `cloud-sun`, inline.

    Inline SVG rather than the `⛅` character the old hero-stat used, and the
    brief allows either ("lucide `cloud-sun` or your existing weather glyph").
    The reason is determinism: `⛅` is a colour emoji, so `color` does not apply
    to it, it renders at whatever size the platform picks, and it is missing
    glyph-for-glyph on some Android builds. The design asks for a 15px icon in
    `--sky`, and only an SVG that is actually guaranteed to be 15px and actually
    `--sky`. It is inline, so there is no network request and no icon font.
    """
    return ('<svg class="weather-icon" width="15" height="15" viewBox="0 0 24 24" '
            'fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M12 2v2"/><path d="m4.93 4.93 1.41 1.41"/>'
            '<path d="M20 12h2"/><path d="m19.07 4.93-1.41 1.41"/>'
            '<path d="M15.947 12.65a4 4 0 0 0-5.925-4.128"/>'
            '<path d="M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6Z"/>'
            '</svg>')


def weather_temperature(*payloads):
    """A display temperature string, or None when the payload has none.

    WHY THIS EXISTS AT ALL. The approved design puts a temperature beside the
    weather glyph in the `Your day` header. `weather_advice` — the only weather
    the digest renderer is given — is documented in weather_advice.py to return
    exactly `{"id", "text", "priority"}`, and those `text` values are completed
    sentences ("Take an umbrella - 70% chance of rain 15:00-17:59"). The numeric
    `apparent_c` / `tmax_c` values exist only INSIDE the weather engine, and
    never reach this module. So there is no temperature to render today, and
    the two available implementations were:

      1. Regex a number out of the advice prose. Rejected: the rain advice
         contains no temperature at all, so the figure would silently change
         meaning depending on which rule fired ("feels like 9C" is an APPARENT
         temperature for a jacket window, not a daily high), and a reader would
         be shown a number the pipeline never asserted. That is exactly the
         quiet untrustworthiness this renderer is written to avoid.
      2. Return None and render the glyph alone. Chosen.

    So the header row renders its icon, and the temperature appears the moment a
    producer emits `meta["weather"]["temperature_c"]`. Both branches are tested;
    this is a contract, not a placeholder.
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for holder in (payload, payload.get("weather")):
            if not isinstance(holder, dict):
                continue
            raw = holder.get(WEATHER_TEMP_KEY)
            if isinstance(raw, bool) or raw is None:
                continue
            if isinstance(raw, (int, float)):
                return "%d°C" % round(float(raw))
            text = str(raw).strip()
            # Tolerate "12", "12C" and "12°C"; refuse anything else rather than
            # print a half-parsed fragment.
            m = re.match(r"^(-?\d+(?:\.\d+)?)\s*°?\s*C?$", text, re.IGNORECASE)
            if m:
                return "%d°C" % round(float(m.group(1)))
    return None


def render_image(url, alt, cls="card-thumb"):
    """Render a lazy-loaded image with a graceful onerror fallback.

    If the image fails to load (e.g. hotlink 404), the onerror handler hides
    it so no broken-image icon or empty box is shown.
    """
    return ('<img class="%s" src="%s" alt="%s" loading="lazy" '
            'onerror="this.style.display=\'none\'">'
            % (cls, esc(url), esc(alt)))


def render_media(story):
    """The story's media, classified for the mobile row layout.

    Returns `(kind, html)` where kind is:

      - ``"hero"``  a playable VIDEO. Full column width inside the 56.25%
        padding-top wrapper. This is a separate case from a thumbnail and the
        brief says so: a 16:9 iframe cannot be a 64x64 square, and shrinking a
        video to 64px would destroy the feature rather than restyle it.
      - ``"thumb"`` an IMAGE with no playable video. The 64x64 square.
      - ``"watch"`` a video URL that cannot be embedded inline, no image: just
        the Watch link, which lives in the text column.
      - ``""``      nothing at all. No empty boxes, exactly as before.

    The five degradation cases are unchanged from the previous implementation
    and in the same order; only the shape of the return value is new, because
    the layout now needs to know WHICH of the two geometries to use.
    """
    image = story.get("image") or {}
    video = story.get("video") or {}
    img_url = (image.get("url") or "").strip() if isinstance(image, dict) else ""
    vid_url = (video.get("url") or "").strip() if isinstance(video, dict) else ""
    vid_type = (video.get("type") or "").strip() if isinstance(video, dict) else ""
    title = story.get("title", "")

    vid_id = youtube_video_id(vid_url)

    if vid_id:
        # Privacy-enhanced YouTube embed, lazy-loaded.
        return "hero", (
            '<div class="media-video">'
            '<iframe class="video-frame" src="https://www.youtube-nocookie.com/embed/%s" '
            'title="%s" loading="lazy" '
            'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
            'gyroscope; picture-in-picture" allowfullscreen></iframe>'
            '</div>' % (esc(vid_id), esc(title)))
    if vid_url and vid_type:
        # Direct media file -> inline video tag (lazy, poster from image).
        poster = ' poster="%s"' % esc(img_url) if img_url else ""
        return "hero", (
            '<div class="media-video">'
            '<video controls preload="none"%s>'
            '<source src="%s" type="%s">'
            'Your browser does not support the video tag.'
            '</video>'
            '</div>' % (poster, esc(vid_url), esc(vid_type)))
    if vid_url:
        # Video URL that cannot be embedded inline -> thumbnail + Watch link.
        parts = [render_image(img_url, title)] if img_url else []
        parts.append(
            '<div class="media-watch">'
            '<a href="%s" target="_blank" rel="noopener">▶ Watch video</a>'
            '</div>' % esc(vid_url))
        return ("thumb" if img_url else "watch"), "\n".join(parts)
    if img_url:
        # Image only.
        return "thumb", render_image(img_url, title)
    return "", ""


def category_chip(category):
    """The category chip MARKUP for a story, or '' if unknown.

    DELIBERATELY NOT RENDERED on the card any more. Every card is rendered
    inside the `<section>` whose `<h2>` is that same category's header, so the
    chip repeated the section header verbatim on every article - the live page
    printed "LOCAL & CITY" twice per story, once as the section and once as
    a chip. The mapping is kept because it is the only place a category key
    becomes a display header, and the badge row below still calls it.
    """
    for key, header, _color in SECTIONS:
        if key == category:
            return '<span class="chip chip-cat">%s</span>' % esc(header)
    return ""


def priority_badge(story):
    """The `★ P<n>` priority badge, or ''.

    NOW ALWAYS ''. `P5` is the pipeline's own triage number: it orders the
    story selection and the `for_you` ranking, but it means nothing to a
    reader, and it was the loudest element on every card. Both values stay in
    the payload untouched. The function is kept (rather than the call deleted)
    so the badge row's shape, and any caller expecting a string, is unchanged.
    """
    return ""


def build_story_card(story):
    """Render a single story as an HTML card."""
    title = esc(story.get("title", ""))
    label = esc(story.get("label", ""))
    published = esc(story.get("published_at", ""))
    deadline = esc(story.get("deadline", ""))
    raw_summary = story.get("summary", "")
    # DISPLAY-ONLY, and OPTIONAL. Empty means render nothing: the previous
    # code emitted a bare bold "Why it matters" heading whenever the field was
    # missing, which is an empty label on the card for every story the
    # validator did not annotate. The condition IS the feature.
    for_you_text = str(story.get("for_you") or "").strip()
    for_you = esc(for_you_text)

    # Meta line: the publication date, and only that.
    #
    # READER SURFACE DISCIPLINE. `source_name`, `reliability` and `event_date`
    # were collector build-log fields and every one of them reached the page:
    # the live digest printed "open-meteo / 2026-09-29 / event: 2026-09-29 /
    # tier2" under a story about rain. A reader gets nothing from a source id,
    # a `tier<n>` reliability code or an `event:` key, and all three stay in
    # the payload for the pipeline and the logs. The date is kept: it is the
    # one field on this line a reader can act on.
    meta_bits = []
    if published:
        meta_bits.append(published)
    meta_line = " \u00b7 ".join(meta_bits)

    # Summary: preserve bullet points (lines starting with "-", "*", "•").
    summary_html = ""
    if raw_summary:
        lines = raw_summary.split("\n")
        if len(lines) > 1 or re.match(r"^\s*[-*•]", lines[0]):
            items = []
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                if re.match(r"^[-*•]\s*", ln):
                    items.append("<li>%s</li>" % esc(re.sub(r"^[-*•]\s*", "", ln)))
                else:
                    items.append("<li>%s</li>" % esc(ln))
            summary_html = "<ul>%s</ul>" % "".join(items)
        else:
            summary_html = "<p>%s</p>" % esc(lines[0].strip())

    # Links
    article_url, via_google = resolve_article_url(story)
    links = []
    if article_url:
        links.append('<a href="%s" class="story-link" target="_blank" rel="noopener">Read article →</a>'
                     % esc(article_url))
    for s in story.get("supporting_sources") or []:
        if isinstance(s, dict) and s.get("url"):
            name = s.get("name") or s["url"]
            links.append('<a href="%s" target="_blank" rel="noopener">%s</a>'
                         % (esc(s["url"]), esc(name)))
    links_html = "".join('<li>%s</li>' % l for l in links) if links else ""

# Media block (image / video) — empty when no media present.
    #
    # `kind` decides the GEOMETRY, not whether the media survives. A playable
    # video keeps the full-width 56.25% wrapper above the text; an image-only
    # story gets the 64x64 square beside it. See render_media().
    media_kind, media_html = render_media(story)

    # Chips / badges row. A deadline is the thing the owner must not miss, so
    # it gets a loud chip, not only the small meta line.
    badges = []
    if label:
        badges.append('<span class="chip chip-label">[%s]</span>' % label)
    # NO category chip here: the <h2> of the section this card sits in already
    # carries the category, so the chip was the same words twice per article.
    badges.append(priority_badge(story))
    if story.get("deadline"):
        badges.append('<span class="chip chip-deadline">📅 Deadline %s</span>'
                      % esc(story["deadline"]))
    if via_google:
        badges.append('<span class="chip chip-via">via Google News</span>')
    badges_html = "".join(b for b in badges if b)

    # Deadline line (if present).
    deadline_html = ""
    if deadline:
        deadline_html = ('<div class="card-deadline">\U0001f4c5 Deadline: %s</div>'
                         % esc(deadline))

    parts = []
    parts.append('<article class="card">')
    if media_kind == "hero":
        # A video spans the column ABOVE the text.
        parts.append('  <div class="card-media">%s</div>' % media_html)
    # THE TEXT COLUMN IS WRAPPED IN `.card-text` IN BOTH CASES, AND THAT IS NOT
    # COSMETIC. `.card-body` is `display: flex` in a row so the 64px thumbnail
    # can sit beside the text. A video story has no thumbnail, so an earlier
    # version emitted the title, chips, meta, summary and links as DIRECT
    # children of that row — five flex items side by side, which squeezed the
    # headline into a 100px column and wrapped "Read article" one character per
    # line. It rendered, and it was unreadable. Every story therefore puts its
    # text in exactly one child, so the flex row has one item and the row can
    # never compete with the content.
    parts.append('  <div class="card-body">')
    if media_kind == "thumb":
        parts.append('    <div class="card-thumb-col">%s</div>' % media_html)
    parts.append('    <div class="card-text">')
    parts.append('      <h3 class="card-title">%s</h3>' % title)
    if badges_html:
        parts.append('      <div class="card-badges">%s</div>' % badges_html)
    if meta_line:
        parts.append('      <div class="card-meta">%s</div>' % meta_line)
    if deadline_html:
        parts.append('      %s' % deadline_html)
    if summary_html:
        parts.append('      <div class="card-summary">%s</div>' % summary_html)
    if for_you:
        parts.append('      <p class="card-foryou"><strong>For you:</strong> %s</p>'
                     % for_you)
    if links_html:
        parts.append('      <ul class="card-links">%s</ul>' % links_html)
    if media_kind == "watch":
        # A video URL that cannot be embedded inline, with no poster image to
        # show. The "Watch video" link is CONTENT, so it goes at the foot of the
        # text column rather than into a `.card-media` block - that block is
        # `background: #000`, which would be a black bar around one line of
        # text. An earlier version of this function emitted media for the
        # "hero" and "thumb" shapes only and silently dropped this one, which
        # lost the link from the page entirely; the branch exists so that
        # cannot happen again.
        parts.append('      %s' % media_html)
    parts.append('    </div>')
    parts.append('  </div>')
    parts.append('</article>')
    return "\n".join(parts)


def render_sections(grouped, source_health=None):
    """Render grouped stories into section blocks in fixed order.

    CONTRACT B, AS THE OWNER HAS NOW NAILED IT DOWN. A section renders when it
    has cards, and it ALSO renders when it has an ALARM to make. It is omitted
    only when it has no cards and its status is quiet -- see
    `section_is_hidden`, which is the whole rule and states why the alarm half
    is not negotiable. The eleven-section-always-renders guarantee that used to
    live here is deliberately GONE: it is the behaviour the owner asked to stop,
    and the honesty it was protecting is preserved by the alarms that remain.
    Cards sit inside a .card-grid wrapper, which is what actually makes the
    existing 2-column CSS rule fire.

    The status line's TONE comes from section_status(): an alarm is a second
    class on the same element, so the element itself is always
    `<p class="section-status">` plus at most one modifier. The base class is
    deliberately the QUIET look, so a tone that is ever forgotten renders
    quiet rather than loud.

    THE PER-SECTION COLOUR MOVED, THE ELEMENT DID NOT. The approved design
    deletes the coloured header bar, so the accent that used to tint a
    `border-left` now tints the 6px dot in front of the label. Two things are
    load-bearing and are why this is a one-property change:

      - the element is STILL `<h2 class="section-header" style="...">`, because
        `parse_digest_categories()` splits the rendered page on exactly that
        pattern to build the archive index's per-category counts. The regex
        matches `[^>]*` for attributes, so the property it sets is irrelevant to
        the split; changing the property is therefore invisible to the index.

    THE HEADER TEXT IS NOW THE BARE HEADER, EMOJI REMOVED. The approved design
    shows one marker per section - the 6px coloured dot - and this row was
    showing two: that dot AND an emoji. Two markers doing one job is the visual
    equivalent of the two claim-voices D2/D3 had to fix, and on a page with
    eleven sections it is eleven extra glyphs of noise. So the digest page's
    label row no longer prepends anything; the header string is rendered as-is.

    SCOPE, AND WHAT WAS DELIBERATELY LEFT ALONE. `SECTION_EMOJI` was the digest
    page's only emoji, and it is deleted rather than left unused, because dead
    code that still holds the glyphs is exactly what a later "harmless tidy"
    would put back. The emoji on OTHER surfaces are separate implementations and
    are untouched by this change:

      - the ntfy push (`.github/workflows/daily-news.yml`) carries its own
        copy of all eleven section emoji inline in its heredoc;
      - `build_email.py` puts no emoji on a section header at all - its
        section rows are the plain display headers - so the email never had
        this defect and never needed this fix.

    `section_label()` still strips leading non-ASCII before mapping a header to
    its short label, so the archive index and the ntfy teaser keep resolving
    sections that arrive from an older digest file that still has an emoji. That
    strip stays: it is a compatibility reader, not the thing that put the emoji
    on the page.
    """
    blocks = []
    for key, header, color in SECTIONS:
        items = grouped.get(key) or []
        # The bare header. See the docstring: the emoji that used to be
        # prepended here were a SECOND marker for the one the 6px dot already
        # is, and the approved design shows only the dot.
        status, tone = section_status(key, len(items), source_health)
        if section_is_hidden(len(items), tone):
            continue
        block = ['<section class="digest-section">',
                 '  <h2 class="section-header" style="--dot:%s;">%s</h2>'
                 % (color, esc(header))]
        if items:
            block.append('  <div class="card-grid">')
            for s in items:
                block.append(build_story_card(s))
            block.append('  </div>')
        if status:
            cls = ("section-status section-status-alarm" if tone == STATUS_ALARM
                   else "section-status")
            block.append('  <p class="%s">%s</p>' % (cls, esc(status)))
        block.append('</section>')
        blocks.append("\n".join(block))
    return "\n".join(blocks)


def render_health_block(source_health, notes="", delivery_line="", edition_line=""):
    """Banner + 'Down:' line + research note + delivery line (website variant).

    `delivery_line` is OPTIONAL and defaults to empty, which renders nothing at
    all -- no line, no placeholder, no gap. That is the whole contract while
    the ledger ships dark: a caller that does not pass one gets byte-identical
    output to a caller that predates the feature.

    `edition_line` follows the SAME contract, for the same reason: the
    unsummarised fallback must not change one byte of a normal digest. It says
    what KIND of edition this is, so it is not folded into the research note and
    it does not borrow the alarm colour -- an edition with no AI is not a fault.

    When it IS passed, the line lands HERE, in the hero health block, and not in
    the footer. The footer is the bare one-liner the archive index shares its
    markup conventions with, so putting punctuality there would couple the
    digest page's footer to a git-mirrored ledger that can lag hours.
    """
    lines = [health_banner(source_health), down_line(source_health),
             capped_note(notes), delivery_line]
    lines = [ln for ln in lines if ln]
    edition_html = ('<p class="health-edition">%s</p>' % esc(edition_line)
                    if edition_line else "")
    if not lines:
        return edition_html
    parts = []
    for ln in lines:
        if ln.startswith(NOTE_LABEL):
            # The note is the long, technical half of this block: the banner
            # says THAT the page may be incomplete, the note says WHY. Fold the
            # detail behind a summary so the block leads with the verdict.
            # Both the `health-note` class and the printed "Research note"
            # label stay in the DOM -- the label is the summary, so the reader
            # can still see there is a note to open.
            # The body then DROPS that label, because the summary directly above
            # it says the same two words; on a 390px phone they read as one
            # stutter. Nothing else about the note is touched -- the hosts it
            # names and the failure it reports are unchanged.
            parts.append('<details class="health-details">'
                         '<summary class="health-details-label">'
                         'Research note</summary>'
                         '<p class="health-note">%s</p></details>'
                         % esc(ln[len(NOTE_LABEL):]))
        else:
            parts.append('<p class="%s">%s</p>'
                         % ("health-down" if ln.startswith("Down:") else
                            "health-line", esc(ln)))
    return '<div class="health-banner">%s%s</div>' % (edition_html, "".join(parts))


def render_topbar(digest_time, date_val, story_count, label=None):
    """The approved top bar: identity on the left, story count on the right.

    This REPLACES the old centred hero header ("Personal News Informer" /
    "Evening Digest") and the four-stat hero row. Nothing was dropped: the
    digest time is now the title's own word ("Morning Digest"), the date is in
    the sub-line, the story count is the pill, and the only stat genuinely
    removed was "N weather notes", which the labelled weather block directly
    below already says better.

    It is NOT sticky. The old bar was `position: sticky`, which on a 390x844
    screen permanently spent ~44px of the viewport on a title the reader has
    already read; the approved design specifies a plain bar with a 1px divider
    under it, and a phone page does not need the header to follow him down.

    The archive back-link moves BELOW the divider rather than competing with the
    title for a 390px row. It is a required feature (the archive is
    unreachable without it) and the approved layout does not place it, so it is
    kept at the quietest available position instead of being deleted or
    squeezed in beside the count pill.

    THE TITLE NOW NAMES AN INSTANT, NOT A SLOT, AND SAYS WHICH KIND. It reads
    `Evening Digest · due 19:20` when the committed schedule names this slot and
    `Late Night Digest · built 02:03` when it does not -- see `delivery_label`,
    which is the only thing that decides between them. `digest_time` is still a
    parameter and is still the SLOT, because the slot is what the filename, the
    KV key and the delivery ledger are keyed on: it is structural identity and
    it must never be replaced by a word about the clock.

    `label` defaults to None so a standalone caller is never handed a header
    with no title at all; it renders nothing for the title in that case rather
    than reading a second clock, because a label this function did not measure
    is the one thing it must never print.
    """
    title_html = ('<h1 class="topbar-title">%s</h1>' % esc(label)) if label else ''
    return ('<header class="topbar">'
            '<div class="topbar-inner">'
            '%s'
            '<p class="topbar-sub">%s</p>'
            '<span class="topbar-count">%d %s</span>'
            '</div>'
            '<div class="topbar-nav">'
            '<a class="back-link" href="/index.html">← All digests</a>'
            '</div>'
            '</header>'
            % (title_html,
               esc(topbar_subline(date_val)),
               story_count, plural(story_count, "story", "stories")))


def render_hero(meta, digest_time, date_val, story_count, source_health=None,
                notes="", stories=None, delivery_line="", edition_line=""):
    """The pre-sections block: honesty, Your day, weather, freshness.

    THE HONESTY BANNER COMES FIRST WHEN IT HAS ANYTHING TO SAY. Its first line
    is the verdict, so a reader who stops after one line still learns the page
    may be incomplete before reading a single item. `render_health_block`
    returns "" when there is nothing to report, so on a healthy day this
    ordering changes nothing at all and `Your day` is still the first thing on
    the page.

    `Your day` is the first CONTENT block, directly under the top bar. That
    position is a requirement, not a preference: the bar is identity, the
    deadline box is action, and action must be the first thing the eye lands
    on. The weather block follows it, then the freshness line. Nothing on this
    surface truncates the honesty banner.

    The header and the stats row that used to live here are now the top bar
    (see render_topbar). What remains is content, and every part of it is a
    tested feature: the personal-relevance block, the weather advice, the
    source-health banner and the freshness line.
    """
    if source_health is None:
        source_health = (meta or {}).get("source_health")
    weather = resolve_weather_advice(meta)
    cutoff = ""
    if isinstance(meta, dict):
        cutoff = str(meta.get("data_cutoff_npt") or "").strip()

    # NO-DUPLICATION RULE: the website is a RICH surface, so the weather is
    # rendered ONCE, in its own labelled block below. `Your day` therefore
    # gets no weather LINE at all — only the header glyph and temperature, which
    # are orientation rather than advice.
    #
    # `max_lines=YOUR_DAY_MAX_LINES` is stated HERE, explicitly, and is the
    # same number the ntfy push uses - see the note on YOUR_DAY_MAX_LINES. A
    # deadline beyond the cap is still shown on its own story card (the
    # `card-deadline` line and the deadline chip), so this caps prominence,
    # not information. Not capping it here is what let the push be quieter
    # than the website.
    your_day_html = render_your_day(
        your_day_lines(stories or [], weather, date_val,
                       max_lines=YOUR_DAY_MAX_LINES, include_weather=False),
        temperature=weather_temperature(meta))
    weather_html = render_weather_block(weather)

    cutoff_html = ""
    line = cutoff_line(cutoff, source_health)
    if line:
        cls = "hero-cutoff hero-cutoff-unverified" \
            if health_state(source_health) == "unknown" else "hero-cutoff"
        cutoff_html = '<p class="%s">%s</p>' % (cls, esc(line))

    banner_html = render_health_block(source_health, notes, delivery_line,
                                      edition_line)

    return ('<section class="hero">'
            '%s%s%s%s'
            '</section>'
            % (banner_html, your_day_html, weather_html, cutoff_html))


def render_your_day(lines, temperature=None):
    """The `Your day` block. NEVER empty, and never a heading with nothing
    under it.

    BUG 1, AND WHY THE FIX IS HERE AND NOT IN `your_day_lines`
    ----------------------------------------------------------
    `lines` is empty in exactly one case (see your_day_lines): a rich surface
    with nothing of its own to say, where the labelled weather block already
    carries the content. The old code turned that into `return ""`, so the
    WHOLE BLOCK DISAPPEARED. The live 2026-09-29 digest proved it: the CSS
    carried `.your-day` and `.your-day-label` and the body carried neither, so
    a feature the owner had shipped rendered as if it had never existed, with
    no error anywhere to explain the absence.

    A silent disappearance is the one failure this block cannot have. Its
    styling existing while its markup does not is indistinguishable, to the
    reader, from a broken renderer - and he had no way to tell whether the
    feature broke or the day was simply quiet. So the empty case now renders
    the DOCUMENTED FALLBACK, `Nothing needs you today.`, which is the same
    literal the true-nothing case uses and is already a fixed string in this
    module: the personal layer SELECTS, and a fallback is not authored prose.

    The fix is deliberately NOT made in `your_day_lines`, which keeps
    returning `[]` for this case. That function is duplicated in
    .github/workflows/daily-news.yml, a file this change does not own, and the
    push's own copy has to keep agreeing with this one. The rendering layer is
    the part this change can fix on every surface it owns, and the divergence
    that leaves is reported rather than papered over.

    The original objection to printing the fallback here - that it contradicts
    the "Take an umbrella" line directly beneath it - is a real one and it is
    why `your_day_lines` returns `[]` rather than the literal. It is outweighed
    by the block vanishing entirely.

    THE HEADER ROW. The approved design puts the weather glyph and the
    temperature on the same line as the `YOUR DAY` label, right-aligned by a
    flexible spacer. `<p class="your-day-label">` and `<div class="your-day">`
    are both load-bearing and both asserted, so the row is a wrapper div around
    them rather than a new element in place of either. The temperature is
    omitted — not rendered empty, not rendered as a placeholder — whenever
    `weather_temperature()` returns None; see its docstring for why.
    """
    items = "".join("<li>%s</li>" % esc(ln) for ln in (lines or [NOTHING_TODAY]))
    temp_html = ('<span class="your-day-temp">%s</span>' % esc(temperature)
                 if temperature else "")
    return ('<div class="your-day">'
            '<div class="your-day-head">'
            '<p class="your-day-label">%s</p>'
            '<span class="your-day-spacer"></span>'
            '<span class="your-day-weather">%s%s</span>'
            '</div>'
            '<ul>%s</ul>'
            '</div>' % (esc(YOUR_DAY_LABEL), weather_icon_svg(), temp_html, items))


def render_weather_block(lines):
    """The weather block, or '' when the engine had nothing to say.

    An empty list must render NOTHING - not a heading, not "no weather
    available". The engine returns [] precisely so the owner is not handed a
    line he cannot act on.

    WEATHER_MAX is applied HERE, in the only function that renders this block,
    so the cap cannot be bypassed by a call site. It used to be applied only in
    `weather_block_lines`, which this function's call site did not go through:
    the website therefore showed every line the engine emitted (up to
    `weather_advice.max_lines` in config/profile.yml) while the email and the
    ntfy push showed WEATHER_MAX. Slicing an already-sliced list is a no-op, so
    this stays correct for a caller that went through `weather_block_lines`.

    WEATHER_MAX is a RENDER cap and is the only place it is stated. It is not
    the same number as `weather_advice.max_lines` in config/profile.yml, which
    is the ENGINE's cap on how many verdicts it produces at all - a different
    contract, and both are required.
    """
    if not lines:
        return ""
    items = "".join("<li>%s</li>" % esc(ln) for ln in lines[:WEATHER_MAX])
    return ('<div class="weather-block">'
            '<p class="weather-label">\u2605 %s</p>'
            '<ul>%s</ul>'
            '</div>' % (esc(WEATHER_LABEL), items))


def render_page(stories, digest_time, date_val, timestamp, meta=None,
                delivery_line="", schedule_path=None):
    """Render the full HTML page.

    `delivery_line` is optional and defaults to empty. Empty means NOTHING is
    rendered for it -- not a placeholder, not an empty <p>, not a comment. A
    caller that never heard of the delivery ledger gets exactly the bytes it
    got before the ledger existed, which is what "ships dark" has to mean.

    `schedule_path` is optional and defaults to the committed
    `deploy/schedule.json`. It exists so the "there is no schedule for this
    build" path can be exercised for real -- a path that does not resolve to
    this slot -- rather than being argued about in a comment.
    """
    # The fallback says what it is, on the page itself. Read from the validated
    # payload rather than passed by the caller, so no caller can render an
    # unsummarised edition that forgets to admit it.
    edition_line = ""
    if (meta or {}).get("edition_kind") == "unsummarised":
        edition_line = UNSUMMARISED_LABEL

    grouped = {}
    for s in stories:
        grouped.setdefault(s.get("category", ""), []).append(s)

    sections_html = render_sections(grouped, (meta or {}).get("source_health"))
    # The empty-state replaces the sections ONLY when there was nothing to say
    # at all. It used to fire on `not stories` alone, which meant a total outage
    # -- zero stories AND several sections screaming B5 "check <url>" -- rendered
    # one polite line and threw the alarms away. That is the exact failure
    # section_is_hidden exists to prevent, so the two conditions are now
    # separate: no stories, and nothing survived to render.
    if not stories and not sections_html:
        sections_html = ('<div class="empty-state">No items cleared filtering today.</div>')

    hero_html = render_hero(meta, digest_time, date_val, len(stories),
                            (meta or {}).get("source_health"),
                            (meta or {}).get("notes"), stories,
                            delivery_line, edition_line)

    # WHAT THIS PAGE CLAIMS ABOUT ITS OWN TIME, STATED PLAINLY.
    #
    # A page is a static committed artifact and the prebuild runs ~40 minutes
    # before the slot, so at render time nobody knows -- and this file is given
    # no way to know -- the minute the push will actually leave. Printing that
    # minute would be fabrication. Printing the BUILD minute was truthful and
    # still wrong for the reader: the owner saw `Evening Digest · 18:43` for a
    # digest his phone delivered at 19:25, and asked whether that was a problem.
    # It was.
    #
    # So the title names the DUE time when the committed schedule names this
    # slot (`· due 19:20`), and falls back to the build instant, named as such
    # (`· built 18:43`), when there is no schedule to quote. Either way the word
    # beside it is derived from the SAME clock the title prints, so the word, the
    # clock and the push's emoji can never disagree about what time this is.
    # `delivery_label` is the only place that decision is made.
    label, _label_word, _label_clock = delivery_label(
        digest_time, timestamp, schedule_path)
    title = "Personal News Informer — %s — %s" % (label, date_val)
    topbar_html = render_topbar(digest_time, date_val, len(stories), label)

    # The CSS below is one long triple-quoted string interpolated with `%s`.
    #
    # Its argument list is written out one entry per placeholder, in order, each
    # annotated with the selector it feeds. That is deliberate and it is not
    # decoration: the list was misaligned twice while this design was written,
    # and a positional run of 45 near-identical hex constants cannot be reviewed
    # by eye. A comment placed after the closing `</html>` and before the
    # closing `"""` — which is where it looked like it belonged — becomes part of
    # the STRING and adds a 46th placeholder, so the annotation lives out here.
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>%s</title>
  <style>
    /* ===================================================================
       THE APPROVED MOBILE DESIGN. Reproduced, not interpreted.

       There is no desktop layout here on purpose. The owner has no desktop use
       case: the previous page was max-width 1100px with 260px images, built
       for a screen he does not use and read on a 390px phone. So the content
       column is capped at 480px and centred, and there is no min-width media
       query that reintroduces a wide multi-column grid. On a wider screen the
       page is a centred 480px column, which is what the design specifies.
       =================================================================== */
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: %s;
      background: %s;
      color: %s;
      line-height: 1.6;
      -webkit-text-size-adjust: 100%%;
      min-height: 100vh;
    }
    .container {
      max-width: 480px;
      margin: 0 auto;
      padding: 20px 18px 32px 18px;
    }
    a { color: %s; }

    /* -- top bar --------------------------------------------------------- */
    .topbar { border-bottom: 1px solid %s; }
    .topbar-inner {
      position: relative;
      padding: 20px 18px 14px 18px;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 3px;
    }
    .topbar-title {
      font-size: 21px;
      font-weight: 700;
      line-height: 1.2;
      color: %s;
      padding-right: 86px;
    }
    .topbar-sub {
      font-size: 12px;
      font-weight: 500;
      color: %s;
    }
    .topbar-count {
      position: absolute;
      top: 20px;
      right: 18px;
      background: %s;
      color: %s;
      font-size: 12px;
      font-weight: 600;
      padding: 6px 10px;
      border-radius: 9px;
      white-space: nowrap;
    }
    .topbar-nav { padding: 9px 18px 11px 18px; }
    .back-link {
      color: %s;
      text-decoration: none;
      font-size: 12px;
      font-weight: 600;
    }
    .back-link:hover { text-decoration: underline; }

    /* -- your day -------------------------------------------------------- */
    .your-day {
      background: %s;
      border-radius: 14px;
      padding: 15px;
      margin: 16px 0 0 0;
    }
    .your-day-head {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .your-day-label {
      color: %s;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 1.1px;
    }
    .your-day-spacer { flex: 1 1 auto; }
    .your-day-weather {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: %s;
    }
    .weather-icon { display: block; flex: 0 0 auto; }
    .your-day-temp {
      font-size: 12px;
      font-weight: 600;
      color: %s;
    }
    /* Main line, then supporting lines. Both sizes are fixed; only weight and
       colour differ, so a one-line block and a three-line block read the same
       way. */
    .your-day ul { margin: 10px 0 0 0; padding: 0; list-style: none; }
    .your-day li {
      color: %s;
      font-size: 16px;
      font-weight: 500;
      line-height: 1.45;
    }
    .your-day li + li {
      margin-top: 4px;
      color: %s;
      font-size: 12px;
      font-weight: 400;
      line-height: 1.45;
    }

    /* -- weather block --------------------------------------------------- */
    /* Not in the approved design: the design has no separate weather block,
       because it moves the glyph into the `Your day` header. The advice TEXT
       is still a tested feature on this surface (an empty engine list renders
       nothing, a populated one renders its lines, capped at WEATHER_MAX), so
       the block stays and is restyled onto the same surface card rather than
       deleted. Reported as a deviation. */
    .weather-block {
      background: %s;
      border-radius: 14px;
      padding: 13px 15px;
      margin: 10px 0 0 0;
    }
    .weather-label {
      color: %s;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 1.1px;
    }
    .weather-block ul {
      margin: 8px 0 0 0;
      padding: 0;
      list-style: none;
    }
    .weather-block li {
      color: %s;
      font-size: 13px;
      line-height: 1.45;
    }
    .weather-block li + li { margin-top: 4px; }

    /* -- honesty + freshness --------------------------------------------- */
    /* ALSO not in the approved design, and deliberately NOT restyled with it.
       The health banner is a tested feature in its own right (degraded /
       down / unverified-cutoff states, and a research note cut on a word
       boundary), and its amber-on-translucent-red is carrying meaning: "this
       digest may be missing items" must not read like ordinary content. It
       keeps its own colours, scaled down to the new type scale. */
    .health-banner {
      background: rgba(220, 38, 38, 0.12);
      border: 1px solid rgba(220, 38, 38, 0.55);
      border-radius: 10px;
      padding: 10px 12px;
      margin: 10px 0 0 0;
      text-align: left;
    }
    .health-line { color: #fbbf24; font-size: 12px; font-weight: 700; }
    /* The unsummarised fallback. An edition KIND, not a fault: it is not folded
       away like the research note and it never borrows the alarm colour. */
    .health-edition {
      color: #f5f5f5;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    .health-down {
      color: #fca5a5;
      font-size: 11px;
      margin-top: 4px;
      word-break: break-all;
    }
    .health-note {
      color: #ddd;
      font-size: 11px;
      line-height: 1.5;
      margin-top: 4px;
    }
    /* A folded research note. Only the summary shows until the reader taps it,
       so the summary carries the weight the whole note used to; the detail
       inside keeps the `health-note` styling. */
    .health-details { margin-top: 4px; }
    .health-details-label {
      color: #ddd;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
    }
    .hero-cutoff { color: %s; font-size: 11px; margin-top: 12px; }
    .hero-cutoff-unverified {
      color: #fbbf24;
      font-weight: 700;
      font-size: 11px;
    }

    /* -- sections -------------------------------------------------------- */
    /* The coloured header bar is gone. What is left is a quiet label row: a
       6px dot in the section's own accent colour, then the name. The accent
       arrives as the `--dot` custom property set inline by render_sections,
       so the eleven existing per-section colours are reused rather than
       re-invented. */
    .digest-section { margin-bottom: 28px; }
    .section-header {
      display: flex;
      align-items: center;
      gap: 7px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.3;
      letter-spacing: 1px;
      color: %s;
      text-transform: uppercase;
      margin: 0;
    }
    .section-header::before {
      content: "";
      display: block;
      flex: 0 0 auto;
      width: 6px;
      height: 6px;
      border-radius: 50%%;
      background: var(--dot, %s);
    }
    .card-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0;
      margin-top: 11px;
    }

    /* -- stories --------------------------------------------------------- */
    /* NO card, NO border, NO per-card background. The cyan 1px card border
       was the loudest thing on a 390px screen and is deleted outright. What
       separates two stories in the same section is a single hairline. */
    .card {
      background: transparent;
      border: 0;
      border-radius: 0;
      padding: 16px 0;
    }
    .card + .card { border-top: 1px solid %s; }
    .card-body {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      padding: 0;
    }
    .card-thumb-col {
      flex: 0 0 64px;
      width: 64px;
      height: 64px;
      border-radius: 10px;
      overflow: hidden;
      background: %s;
    }
    .card-thumb-col img.card-thumb {
      display: block;
      width: 64px;
      height: 64px;
      object-fit: cover;
      border-radius: 10px;
    }
    .card-text { flex: 1 1 auto; min-width: 0; }
    .card-title {
      font-size: 16px;
      font-weight: 600;
      line-height: 1.35;
      color: %s;
    }

    /* A playable video is the one thing that is NOT a 64px square: a 16:9
       embed cannot be, and the brief names the 56.25%% wrapper as the case to
       preserve. It spans the column above the text. */
    .card-media {
      margin: 0 0 10px 0;
      border-radius: 10px;
      overflow: hidden;
      background: #000;
    }
    .media-video { position: relative; padding-top: 56.25%%; background: #000; }
    .media-video iframe.video-frame,
    .media-video video {
      position: absolute;
      top: 0; left: 0;
      width: 100%%; height: 100%%;
      border: 0;
    }
    .media-watch { padding: 0; }
    .media-watch a { color: %s; font-weight: 600; text-decoration: none; }
    .media-watch a:hover { text-decoration: underline; }

    /* -- chips / meta / body --------------------------------------------- */
    .card-badges {
      margin: 6px 0 0 0;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .chip {
      display: inline-block;
      border-radius: 4px;
      padding: 2px 6px;
      font-size: 9px;
      font-weight: 700;
      letter-spacing: 0.6px;
      vertical-align: middle;
    }
    .chip-label { background: %s; color: %s; }
    .chip-cat { background: %s; color: #eef; }
    .chip-deadline { background: #dc2626; color: #fff; }
    .chip-via { background: %s; color: #eef; }
    .badge {
      display: inline-block;
      border-radius: 4px;
      padding: 2px 6px;
      font-size: 9px;
      font-weight: 700;
      background: %s;
      color: #eef;
    }
    .badge-high { background: %s; color: #fff; }
    .card-meta {
      font-size: 11px;
      font-weight: 500;
      color: %s;
      margin-top: 5px;
    }
    .card-deadline {
      font-size: 11px;
      font-weight: 600;
      color: %s;
      margin-top: 5px;
    }
    /* 16px, not 13px: the story body is what the owner actually READS, and it
       used to be the smallest text on the page after the chips. Reading text
       and headline are now the same size and the step between them is carried
       by weight and colour instead, so nothing that must be read is smaller
       than the headline above it. */
    .card-summary {
      font-size: 16px;
      line-height: 1.6;
      color: %s;
      margin-top: 8px;
    }
    .card-summary ul { margin: 4px 0 0 16px; }
    .card-summary li { margin-bottom: 5px; }
    .card-foryou {
      font-size: 16px;
      line-height: 1.6;
      color: %s;
      margin-top: 8px;
    }
    .card-foryou strong { color: %s; }
    .card-links {
      list-style: none;
      margin: 8px 0 0 0;
      padding: 0;
    }
    .card-links li { margin-bottom: 5px; }
    .card-links a {
      color: %s;
      text-decoration: none;
      font-size: 15px;
      word-break: break-word;
    }
    .card-links a:hover { text-decoration: underline; }
    .story-link { font-weight: 600; }

    /* STATUS TONE (D2 + D3). TWO CLAIMS WERE DRESSED IDENTICALLY.
       "I checked and there was no news" and "the source failed, so this may
       be a lie by omission" both rendered as amber #FBBF24 on a translucent
       red background with a red left bar - the reader could not tell a quiet
       day from a broken one. Worse, 5 of 9 status lines on the live 3-story
       page were false alarms about sections with no configured source at all,
       which made a working digest read as broken.

       So the base class is QUIET: --muted, no background, no bar, not bold.
       The alarm colours are opt-in via one modifier class, so a tone that is
       ever forgotten renders quiet rather than loud. Both tones are the same
       size so COLOUR is the only thing that distinguishes them.

       SIZE: METADATA DOES NOT OUTRANK THE STORIES IT ANNOTATES. This rule was
       0.95rem = 15.2px, which is LARGER than the 15px story titles directly
       beneath it, so on a 3-story page the 9 metadata lines were the second
       loudest thing on the screen - the owner's "no clear priority" and "too
       cluttered to scan" were this, still alive. A status line is a fact about
       a SECTION, so it joins the section furniture at 11px rather than
       competing with the content. Same letter-spacing as .section-header so
       the label and its status read as one pair. See
       test_no_metadata_text_outranks_the_story_titles, which asserts the
       general invariant so this cannot be re-inflated.

       COLOUR: --muted (#9BA1AC), measured 7.33:1 on the page background, which
       is WCAG AA and AAA for normal text. --dim (#6C727C) was the first choice
       because the approved palette reserves it for section labels, but it
       measures 3.93:1 on #0E1014 and FAILS AA's 4.5:1 at this size, so it
       would have bought the quietness by making the line hard to read. The
       old #aaa measured 8.20:1, so this is also a genuine small step DOWN in
       loudness rather than a rename.

       The quiet/alarm distinction above is a correctness property and is
       unaffected: the base rule still carries neither the amber nor the red,
       and both tones still render at the same size. */
    .section-status {
      color: %s;
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 1px;
      background: transparent;
      border-left: 3px solid transparent;
      border-radius: 4px;
      padding: 0.4rem 0.7rem;
      word-break: break-word;
    }
    .section-status-alarm {
      color: #fbbf24;
      font-weight: 600;
      background: rgba(220, 38, 38, 0.10);
      border-left-color: #dc2626;
    }
    .empty-state { text-align: center; padding: 3rem 0; color: #ddd; }
    footer {
      text-align: center;
      padding: 20px 0 0 0;
      color: %s;
      font-size: 11px;
      border-top: 1px solid %s;
      margin-top: 24px;
    }
  </style>
</head>
<body>
  %s
  <div class="container">
    %s
    %s
    <footer>
      Generated by Personal News Informer · %s NPT
    </footer>
  </div>
</body>
</html>
""" % (
    esc(title),                          # <title>
    D_FONT,                              # body font-family
    D_BG,                                # body background
    D_TEXT,                              # body color
    D_ACCENT,                            # a { color }
    D_LINE,                              # .topbar border-bottom
    D_TEXT,                              # .topbar-title color
    D_DIM,                               # .topbar-sub color
    D_SURFACE_HI,                        # .topbar-count background
    D_MUTED,                             # .topbar-count color
    D_DIM,                               # .back-link color
    D_SURFACE,                           # .your-day background
    D_ACCENT,                            # .your-day-label color
    D_SKY,                               # .your-day-weather color
    D_SKY,                               # .your-day-temp color
    D_TEXT,                              # .your-day li color (main line)
    D_MUTED,                             # .your-day li + li (supporting line)
    D_SURFACE,                           # .weather-block background
    D_ACCENT,                            # .weather-label color
    D_MUTED,                             # .weather-block li color
    D_DIM,                               # .hero-cutoff color
    D_DIM,                               # .section-header color
    D_ACCENT,                            # .section-header::before dot fallback
    D_LINE,                              # .card + .card border-top
    D_SURFACE_HI,                        # .card-thumb-col background
    D_TEXT,                              # .card-title color
    D_ACCENT,                            # .media-watch a color
    D_SURFACE_HI,                        # .chip-label background
    D_ACCENT,                            # .chip-label color
    D_SURFACE_HI,                        # .chip-cat background
    D_SURFACE_HI,                        # .chip-via background
    D_ACCENT,                            # .badge background
    D_ACCENT,                            # .badge-high background
    D_DIM,                               # .card-meta color
    D_DIM,                               # .card-deadline color
    D_MUTED,                             # .card-summary color
    D_MUTED,                             # .card-foryou color
    D_ACCENT,                            # .card-foryou strong color
    D_ACCENT,                            # .card-links a color
    D_MUTED,                             # .section-status color
    D_DIM,                               # footer color
    D_LINE,                              # footer border-top
    topbar_html, hero_html, sections_html, esc(timestamp))

#: The archive footer's fixed note. Approved, not derived: the reader is told
#: what the ordering rule is and how long the email copy lives, which is the only
#: thing on this page that changes on a schedule the reader cannot see.
ARCHIVE_FOOTER_NOTE = "Newest first · 14-day email retention"

#: The archive's own eyebrow. The `<title>` and `<h1>` used to say
#: "Personal News Informer — Digest Archive"; the approved design splits that
#: into an accent eyebrow plus a plain title, and the `<title>` element keeps
#: the full product name because `.github/workflows/drift-canary.yml` matches
#: it as an archive marker (`ARCHIVE_MARKERS='Personal News Informer|Digest
#: Archive'`) to detect a leak. Both markers therefore still appear: one in
#: `<title>`, one in the `<h1>`. Removing the product name from the page would
#: break the leak canary, so it stays.
ARCHIVE_EYEBROW = "Archive"

#: The chevron on every archive row: a decorative affordance, not a label, so it
#: is `aria-hidden` and the row's own text carries the meaning. Inline SVG
#: because the row must not issue a second request for a 14px glyph, and a
#: `currentColor` stroke means the colour lives in ONE place - the
#: `.digest-chevron` rule - rather than being repeated in every row.
CHEVRON_SVG = (
    '<svg class="digest-chevron" width="14" height="14" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true" focusable="false">'
    '<polyline points="9 18 15 12 9 6"></polyline></svg>')


def _archive_date_span(groups):
    """'Aug 28 - Oct 4' for the archive subtitle: the oldest and newest dates.

    `groups` is newest-first, so the span reads oldest -> newest, which is the
    direction a reader parses "Aug 28 - Oct 4" in.

    `%-d` is deliberately NOT used: it is a glibc/BSD extension and MSVC's
    `strftime` - the one on every Windows box this project runs on - does not
    support it, so the day number is formatted by hand instead. `%d` would have
    produced "Aug 28 - Oct 04" on some platforms and "Aug 28 - Oct 4" on others.
    """
    def short(iso_date):
        try:
            parsed = datetime.datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            return iso_date
        return "%s %d" % (parsed.strftime("%b"), parsed.day)

    if not groups:
        return ""
    return "%s – %s" % (short(groups[-1]["date"]), short(groups[0]["date"]))


def render_index(out_dir, index_path, current_date, current_time):
    """Regenerate docs/index.html from all digest files in out_dir.

    Scans out_dir for files matching YYYY-MM-DD-{morning|evening}.html,
    groups them by date (newest first; evening before morning within a
    date), and writes the archive index. Each digest row shows a 64px
    thumbnail preview (first story image), the story count, and the
    category-count summary. Returns True on success.

    THE APPROVED DESIGN IS A PHONE-ONLY PAGE, and that is a decision rather
    than an omission: the owner reads this archive on a phone and has no
    desktop use case. So there is no `min-width` media query, no multi-column
    grid and no `clamp()` viewport-fluid type anywhere below - the type ladder
    is fixed px. The old desktop version had all three, and this docstring says
    why they must not come back.

    `current_time` is accepted and NOT rendered. The approved footer is the
    fixed `ARCHIVE_FOOTER_NOTE`; a "generated at" stamp on a page that is
    rewritten on every pipeline run carries no information. The parameter is
    kept so the call signature used by `main()` and the tests does not change.
    """
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})-(morning|evening)\.html$")
    entries = []
    if os.path.isdir(out_dir):
        for name in os.listdir(out_dir):
            m = pattern.match(name)
            if not m:
                continue
            date_val, digest_time = m.group(1), m.group(2)
            path = os.path.join(out_dir, name)
            counts = parse_digest_categories(path)
            counts = counts or {}
            entries.append({
                "date": date_val,
                "time": digest_time,
                "rel": "digests/%s" % name,
                "counts": counts,
                "count": sum(counts.values()),
                "thumb": first_thumbnail(path),
            })

    # Sort: newest date first; within a date, evening before morning (evening
    # is the latest digest of the day).
    time_rank = {"morning": 0, "evening": 1}
    entries.sort(key=lambda e: (e["date"], time_rank.get(e["time"], 2)), reverse=True)

    # Group by date, preserving sort order.
    groups = []
    for e in entries:
        if groups and groups[-1]["date"] == e["date"]:
            groups[-1]["entries"].append(e)
        else:
            groups.append({"date": e["date"], "entries": [e]})

    # Build the archive body.
    if not groups:
        body = ('<div class="empty-state">No digests yet. Run the pipeline to '
                'generate your first briefing.</div>')
    else:
        blocks = []
        for g in groups:
            try:
                readable = datetime.datetime.strptime(
                    g["date"], "%Y-%m-%d").strftime("%a, %b %d %Y")
            except ValueError:
                readable = g["date"]
            cards = []
            for e in g["entries"]:
                label = e["time"].capitalize()
                summary = build_summary(e["counts"])
                # THE 64px SQUARE IS RESERVED EVEN WITH NO IMAGE. 18 of the 75
                # shipped digests carry no `<img class="card-thumb">` at all, so
                # this is a real and frequent case, not a hypothetical. The
                # `.digest-thumb` cell is therefore ALWAYS emitted and carries the
                # `--surface-hi` fill that the approved design shows as the
                # placeholder; the image is the only thing conditional. The old
                # desktop card omitted the whole element, which is why a missing
                # thumbnail used to collapse that card's height.
                #
                # `onerror` hides the IMG ONLY, never the cell: a dead image URL
                # therefore degrades to the same reserved placeholder instead of
                # shifting the row.
                thumb_img = ""
                if e["thumb"]:
                    thumb_img = ('<img class="digest-thumb-img" src="%s" alt="" '
                                 'loading="lazy" '
                                 'onerror="this.style.display=\'none\'">'
                                 % esc(e["thumb"]))
                count_html = ('<span class="digest-count">%d %s</span>'
                              % (e["count"], plural(e["count"], "story", "stories")))
                cards.append(
                    '<a class="digest-row" href="%s">'
                    '<span class="digest-thumb">%s</span>'
                    '<span class="digest-body">'
                    '<span class="digest-row-head">'
                    '<span class="digest-card-label">%s</span>'
                    '%s'
                    '</span>'
                    '<span class="digest-card-summary">%s</span>'
                    '</span>'
                    '%s'
                    '</a>' % (esc(e["rel"]), thumb_img, esc(label),
                              count_html, esc(summary), CHEVRON_SVG))
            blocks.append(
                '<div class="date-group">'
                '<h2 class="date-heading">%s</h2>'
                '%s'
                '</div>' % (esc(readable), "\n".join(cards)))
        body = "\n".join(blocks)

    title = "Personal News Informer — Digest Archive"
    # Joined from non-empty parts, so the empty archive does not render a
    # dangling "0 briefings · " with nothing after the separator.
    subtitle = " · ".join(part for part in (
        "%d %s" % (len(entries), plural(len(entries), "briefing", "briefings")),
        _archive_date_span(groups)) if part)
    footer_note = ARCHIVE_FOOTER_NOTE

    page = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>%s</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: %s;
      background: %s;
      color: %s;
      line-height: 1.5;
      min-height: 100vh;
      -webkit-text-size-adjust: 100%%;
    }
    /* 390px column, centred, 20px gutters -> a 350px row, which is what the
       approved render measures. Fixed px type and one column throughout: no
       viewport-fluid type function, no min-width media query, no multi-column
       grid. Phone-only, by decision. */
    .container { max-width: 390px; margin: 0 auto; padding: 32px 20px 40px 20px; }

    /* HEADER: no card, no background, no border of its own. */
    .archive-head {
      padding-bottom: 16px;
      border-bottom: 1px solid %s;
    }
    .archive-eyebrow {
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.7px;
      text-transform: uppercase;
      color: %s;
    }
    .archive-title {
      font-size: 21px;
      font-weight: 700;
      color: %s;
      line-height: 1.25;
      margin-top: 5px;
    }
    .archive-subtitle {
      font-size: 12px;
      color: %s;
      margin-top: 3px;
    }

    /* ONE HAIRLINE PER BOUNDARY. The first group drops its own top rule so it
       does not stack a second line on the header's. */
    .date-group { border-top: 1px solid %s; }
    .date-group:first-of-type { border-top: 0; }
    .date-heading {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      color: %s;
      padding: 20px 0 10px;
    }
    .date-heading::before {
      content: '';
      display: block;
      flex: 0 0 6px;
      width: 6px;
      height: 6px;
      border-radius: 50%%;
      background: %s;
    }

    /* A ROW, NOT A CARD: no background, no border, no radius.
       NO HAIRLINE ON THE ROW EITHER, and that is measured, not stylistic. The
       approved render's within-group row pitch is exactly 88.00px = a 64px
       thumbnail + 12px + 12px, so two digests on the same date are separated by
       24px of background and NOTHING else; a `border-top` here would add ~1px
       per row and put a line where the approved design has none. The separators
       are the hairline above each date group and the one under the header. */
    .digest-row {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 0;
      text-decoration: none;
      color: inherit;
    }
    /* THE RESERVED 64px SQUARE. Rendered with or without an image, so a digest
       with no thumbnail keeps its row height instead of collapsing it. */
    .digest-thumb {
      flex: 0 0 64px;
      width: 64px;
      height: 64px;
      border-radius: 8px;
      overflow: hidden;
      background: %s;
    }
    .digest-thumb .digest-thumb-img {
      display: block;
      width: 100%%;
      height: 100%%;
      object-fit: cover;
    }
    /* min-width:0 so a long word or a long category list wraps INSIDE this
       column instead of widening the row and pushing the chevron off screen. */
    .digest-body { flex: 1 1 auto; min-width: 0; }
    .digest-row-head {
      display: flex;
      align-items: baseline;
      gap: 8px;
    }
    .digest-card-label {
      font-size: 15px;
      font-weight: 600;
      color: %s;
    }
    .digest-count {
      font-size: 12px;
      font-weight: 600;
      color: %s;
    }
    .digest-card-summary {
      display: block;
      font-size: 11px;
      line-height: 1.45;
      color: %s;
      margin-top: 2px;
    }
    .digest-chevron {
      flex: 0 0 14px;
      width: 14px;
      height: 14px;
      color: %s;
    }
    .empty-state {
      padding: 40px 0;
      font-size: 13px;
      color: %s;
    }
    .archive-footer {
      margin-top: 20px;
      padding-top: 18px;
      border-top: 1px solid %s;
      font-size: 11px;
      color: %s;
    }
  </style>
</head>
<body>
  <div class="container">
    <header class="archive-head">
      <div class="archive-eyebrow">%s</div>
      <h1 class="archive-title">Digest Archive</h1>
      <p class="archive-subtitle">%s</p>
    </header>
    %s
    <footer class="archive-footer">
      %s
    </footer>
  </div>
</body>
</html>
""" % (esc(title),
       D_FONT, D_BG, D_TEXT,
       D_LINE, D_ACCENT, D_TEXT, D_MUTED,
       D_LINE, D_DIM, D_ACCENT,
       D_SURFACE_HI, D_TEXT, D_ACCENT, D_MUTED, D_DIM,
       D_MUTED, D_LINE, D_DIM,
       esc(ARCHIVE_EYEBROW), esc(subtitle), body, esc(footer_note))

    # Ensure the parent directory exists, then write the full index.
    parent = os.path.dirname(index_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(page)
    return True


# The card markup one story emits. Counted in an existing page to answer "has
# this slot already published stories?", which is the only question the guard
# below needs answered.
STORY_CARD_TAG = '<article class="card">'


def count_story_cards(path):
    """Story cards in an existing page, or None when the file is unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().count(STORY_CARD_TAG)
    except OSError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render daily digest as a styled HTML page for GitHub Pages.")
    ap.add_argument("--digest-time", dest="digest_time", required=True,
                    choices=["morning", "evening"], help="morning or evening")
    ap.add_argument("--date", dest="date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--email-txt", dest="email_txt", default="state/email.txt")
    ap.add_argument("--stories-json", dest="stories_json", default="state/stories.validated.json")
    ap.add_argument("--candidates", dest="candidates",
                    default="state/candidates.json",
                    help="optional; read for a top-level `weather_advice` key "
                         "when the validated payload does not carry one")
    ap.add_argument("--out-dir", dest="out_dir", default="docs/digests")
    ap.add_argument("--index", dest="index", default="docs/index.html")
    ap.add_argument("--delivery-ledger", dest="delivery_ledger", default=None,
                    help="optional path to a delivery ledger JSON; when given, "
                         "the punctuality line is rendered into the hero health "
                         "block. When NOT given the page is byte-identical to "
                         "one rendered before the ledger existed")
    args = ap.parse_args(argv)

    # Validate date format.
    try:
        datetime.datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("FATAL: --date must be YYYY-MM-DD, got: %s" % args.date)
        return 1

    # Load structured data (best-effort; missing file is not fatal).
    structured, meta = load_structured(args.stories_json)

    # The weather engine's verdicts live in state/candidates.json, not in the
    # validated payload. Both are consulted, in that order, and absence of
    # either is normal rather than an error: no advice must never become a
    # placeholder or a warning line.
    candidates = load_structured(args.candidates)[1]
    if not meta.get("weather_advice") and candidates.get("weather_advice"):
        meta = dict(meta)
        meta["weather_advice"] = candidates["weather_advice"]

    # Load plain-text digest (best-effort).
    parsed = []
    if os.path.isfile(args.email_txt):
        try:
            with open(args.email_txt, "r", encoding="utf-8") as f:
                parsed = parse_email_text(f.read())
        except OSError as exc:
            print("WARNING: cannot read %s: %s" % (args.email_txt, exc))

    stories = merge_stories(structured, parsed)

    # Timestamp for footer. Nepal time (UTC+05:45) explicitly: the naive
    # local clock disagreed with the NPT hero cutoff on every digest.
    timestamp = kathmandu_now_str()

    # Punctuality line, OPTIONAL and shipped dark. No --delivery-ledger means
    # no line at all -- not an empty one and not the "unavailable" alarm. The
    # alarm is only honest once someone has asked for the ledger and the file
    # then turned out to be missing or corrupt; rendering it unconditionally
    # would put a permanent false alarm on every digest.
    delivery_line = ""
    if args.delivery_ledger is not None:
        try:
            import delivery_ledger as delivery_ledger_mod
        except ImportError:
            print("WARNING: --delivery-ledger given but scripts/delivery_ledger.py "
                  "is not importable; rendering without the delivery line")
        else:
            delivery_line = delivery_ledger_mod.delivery_line(
                delivery_ledger_mod.load_ledger(args.delivery_ledger))

    # Render page.
    page = render_page(stories, args.digest_time, args.date, timestamp, meta,
                       delivery_line)

    # Write output file.
    out_name = "%s-%s.html" % (args.date, args.digest_time)
    out_path = os.path.join(args.out_dir, out_name)

    # An empty build must never replace a page that already has stories. On
    # 2026-10-26 the 20:43 run had zero stories and overwrote that evening's
    # 13:35 page (3 stories) at the stable slot URL: an empty render is still a
    # successful render, and the deploy gate on EMPTY_DIGEST does not protect
    # the committed file. Exit non-zero so the caller's swap step is skipped.
    published = count_story_cards(out_path) if os.path.isfile(out_path) else 0
    if not stories and published:
        print("render_digest: REFUSED to overwrite %s with a 0-story build; "
              "the published page has %d story(ies)" % (out_path, published))
        return 1

    if args.out_dir and not os.path.isdir(args.out_dir):
        os.makedirs(args.out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)

    # Regenerate the full archive index from all digest files present.
    updated = render_index(args.out_dir, args.index, args.date, timestamp)

    print("render_digest: %d story(ies) -> %s" % (len(stories), out_path))
    if updated:
        print("  index regenerated: %s" % args.index)
    else:
        print("  WARNING: could not regenerate index: %s" % args.index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
