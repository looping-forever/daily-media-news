#!/usr/bin/env python3
"""check_academic_result.py — TU B.Sc.CSIT academic result monitor.

Watches the IOST (Institute of Science and Technology, Tribhuvan University)
notice board for CSIT semester-result announcements, downloads the result PDF,
and checks whether the tracked roll number appears in it. On a pass/fail it
emits a notification payload (actual ntfy/website delivery is a separate
workflow step) and advances the per-semester state machine.

A second, deliberately OVER-MATCHING pass covers exam routines, form deadlines
and admission-card notices (config/exam_notices.json), which the result feed
never carries.

Stdlib only. Designed to run from cron/Actions. Every network touch goes
through an injectable ``fetch_fn`` so tests run fully offline.

THE NO-BURN INVARIANT (contract D1 — mirrored verbatim in deploy/cloudflare-worker.js)
-----------------------------------------------------------------------------------
R1 (no-burn)          A notice id enters the *examined* set only when the run
                      obtained its **authoritative title** and made a positive
                      decision on it. A notice that could not be read to that
                      point is **deferred**, never skipped, and MUST be retried
                      on every subsequent run until it succeeds.
R2 (bounded)          Each run processes unexamined notices in **ascending id
                      order**, capped at ``MAX_PENDING`` (60). If more than 60
                      are pending: ``pending_overflow: true``, process the 60
                      lowest, and advance nothing past the last **processed** id.
R3 (no-advance-on-    On a transport/parse/KV failure for notice N, the run
    error)            stops at N (does not touch N+1…) and leaves N unexamined.
R4 (no infinite       Notices read and judged irrelevant ARE remembered by id
    re-fetch)         with a reason, so they are not re-fetched daily forever.
                      "Retried forever" applies ONLY to unread/deferred notices.
R5 (gap-free floor)   ``last_examined_notice_id`` advances to N **iff** every id
                      ``m`` with ``floor < m < N`` is either examined or absent
                      from the current parsed list. It is NEVER set to
                      ``max(ids seen)``.

R5 requires the FULL parsed list. The historical ``notices[-max_notices:]``
truncation is therefore REMOVED: truncating before the gap computation would let
R5 read "absent because truncated" as "absent because nonexistent" and silently
reintroduce the burn. The bound now comes from R2's processing cap.

R2b (time-bounded)    R2 bounds how many notices a run may OPEN; it never
                       bounded how LONG a run could take. 60 sequential detail
                       fetches at the default ``--timeout 15`` is 900s, which is
                       the whole ``timeout-minutes: 15`` of the academic-result
                       job, so the run was killed mid-pass and never reached
                       ``save_state``: the floor never advanced and every notice
                       was re-fetched tomorrow. Not a burn, but a WASTED RUN at
                       the one moment a run cannot be wasted. The result pass
                       therefore also carries a wall-clock budget
                       (``RESULT_PASS_BUDGET_S``). When it runs out the pass stops
                       opening new notices, records the untouched remainder as
                       ``deferred`` so R1 retries it next run, and returns
                       normally, so the run saves truthful state instead of being
                       killed with nothing written.

State machine (per semester)
---------------------------
pending --(matching notice seen, roll found)--> passed [advance current_semester]
pending --(matching notice seen, roll NOT found)--> failed [advance current_semester; KEEP checking]
failed  --(later re-exam/back result, roll found)--> passed [stop checking this semester]
passed  --(terminal)--> no further checks

current_semester advances on ANY announcement (pass OR fail). A failed
semester stays in the map and keeps being polled until roll_found becomes true.

Exit codes
----------
0  normal (including no-op)
1  config fatal (bad state)
2  transient network failure
3  state present but invalid (refuse to overwrite)
4  state not found (never re-baselines; restore it from the KV key
   ``academic:state``)

CLI:
  py scripts/check_academic_result.py --state-file state/academic_state.json \
      [--dry-run] [--poll-url URL] [--max-notices 30] [--timeout 15] \
      [--no-exam-notices]
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from html import unescape
from urllib.parse import urljoin, urlsplit

# The Devanagari (Nepali) recognition vocabulary. Python-only and additive: the
# Cloudflare Worker has no Devanagari stage and never will - it is a delivery
# path, not the decision path. See scripts/devanagari.py for why digits are the
# only transliteration and why the semester words are looked up, not derived.
try:
    import devanagari
except ImportError:  # imported from a cwd that is not scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import devanagari

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import ntfy_classes  # noqa: E402  (deliberate: after the sys.path insert)

DEFAULT_STATE_FILE = os.path.join(REPO_ROOT, "state", "academic_state.json")
# The KV key the workflow restores state from and writes it back to. It holds
# the roll number, so this file (never the KV value) is what the monitor reads.
STATE_KV_KEY = "academic:state"
DEFAULT_POLL_URL = "https://iost.tu.edu.np/notices?notice_type=21"
NOTIFY_PREVIEW_PATH = os.path.join(REPO_ROOT, "state",
                                   "academic_notify_preview.json")
DEFAULT_EXAM_NOTICES_PATH = os.path.join(REPO_ROOT, "config", "exam_notices.json")

# R2: per-run bound on notices we will actually open.
MAX_PENDING = 60
# R2b: per-run WALL-CLOCK budget for the result pass. MAX_PENDING bounds the
# NUMBER of notices; it says nothing about the TIME, and the two together are
# what killed the run: 60 sequential detail fetches at the default --timeout 15
# is 900s, which is exactly the academic-result job's timeout-minutes: 15, so
# the run was killed mid-pass and never reached save_state. The floor therefore
# never advanced and every notice was re-fetched on the next tick - a wasted
# run, which is unaffordable during exam season.
#
# WHY 360s (6 of the job's 15 minutes):
#   - the job is capped at 900s TOTAL, and the steps around the monitor
#     (python setup, the OCR/tesseract install, and the self-check step which
#     is itself capped at 3 minutes) already spend minutes of it;
#   - the pass is checked BEFORE a notice is opened, never mid-notice, so the
#     notice already in flight always completes: its detail fetch (<=15s), its
#     PDF download (<=15s) and its FULL OCR pass. That is the "room for the OCR
#     of at least one notice" requirement, and it holds by construction rather
#     than by arithmetic;
#   - what remains after the budget is the notice-list fetch, the exam-notice
#     pass (index titles only, no detail fetches), the atomic save, and the
#     render/deploy/commit steps - minutes, not hundreds of seconds.
# Under the old unbounded pass a single backlog could need 900s of fetches
# alone, before any PDF or OCR.
RESULT_PASS_BUDGET_S = 360
# State-map bound. Lowest ids evicted first (R4 keeps the map from growing
# forever; the floor lives in its own field so eviction below it is harmless).
EXAMINED_CAP = 300
# Legacy-migration replay window. See _migrate_state.
REPLAY_WINDOW = 200

# ---------------------------------------------------------------------------
# Devanagari / unreadable-title loudness (P0, 2026-09-29)
# ---------------------------------------------------------------------------
# 5 of the 14 notices on the live result feed are written in Nepali. When a
# title cannot be READ we cannot conclude anything about the notice, so
# `classify_semester` returns status "unknown" and the pass DEFERS it forever.
# That is the safe direction (R1 retries; the floor never moves past it) but it
# has a failure mode of its own: a token set that does not cover this board's
# vocabulary freezes the floor on the first Nepali notice and the alert never
# comes. So the freeze is made LOUD instead of silent: after this many
# title_script_unknown defers in ONE run, print ::error:: and keep deferring.
# Overridable with --max-script-defers. Never decide, only shout.
ACADEMIC_MAX_SCRIPT_DEFERS = 3

# Same idea, per id rather than per run (head-of-line, see _process_result_feed).
# A notice that has deferred this many times in a row is unfixable from here
# (PDF 404, login-gated, permanently scanned) and `break`ing on it blocks every
# id above it, every run, forever. After this many consecutive defers the pass
# emits a loud unresolved-id alert and continues past it. The floor is NOT
# weakened: the id stays recorded as `deferred`, and a deferred id is not a
# decision, so R5's gap scan still refuses to move the floor past it.
ACADEMIC_MAX_CONSECUTIVE_DEFERS = 3

# ---------------------------------------------------------------------------
# PDF download (P1, 2026-09-29)
# ---------------------------------------------------------------------------
# The real B.Sc.CSIT result sheet is ~10.5 MB. It was downloaded with the
# CLI's --timeout 15 (the NOTICE-LIST timeout) rather than download_pdf's own
# 30s default, so it raised FetchError and deferred, run after run. The PDF
# gets its own generous timeout and its own size cap: a result sheet is large
# by nature, and the cap is a runaway guard, not a size limit.
PDF_FETCH_TIMEOUT_S = 60
PDF_MAX_BYTES = 40 * 1024 * 1024

# OCR bounds. A scanned sheet is rasterised page by page; three pages at 150 dpi
# is enough to find a roll-shaped token and bounded so one scanned notice cannot
# eat the R2b wall-clock budget (see RESULT_PASS_BUDGET_S).
OCR_MAX_PAGES = 3
OCR_DPI = 150

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1

OUTCOMES = ("irrelevant", "processed", "already_announced", "deferred")
REASONS = (
    "no_semester_match", "batch_mismatch", "result_announced",
    "already_announced", "wrong_poll_set", "title_unparseable",
    "detail_fetch_failed", "pdf_missing", "pdf_unextractable",
    "dispatch_failed", "deadline_notified",
    # ADDED for the FALSE-FAILED guard. The PDF text is non-empty but does not
    # look like a readable result sheet (see sheet_text_is_readable): a legacy
    # custom-encoded font, a font pypdf cannot map, an OCR pass that produced
    # prose instead of digits. "The roll is not in this text" is NOT evidence
    # of absence when the text was never readable, so a FAILED verdict cannot be
    # recorded. Recorded as ``deferred`` so R1 retries it forever rather than
    # advancing the floor past a result the owner may have PASSED.
    "pdf_text_unreadable",
    # ADDED after review. Both mean "the state machine could not produce a
    # verdict", which is emphatically NOT a positive decision:
    #   semester_not_tracked  the semesters map has no entry for this semester
    #                         key, so a verdict could not be recorded anywhere.
    #   inconsistent_state    the state contradicts itself (e.g. status
    #                         "passed" with result_announced falsy), so
    #                         anything the run inferred would be a guess.
    # Both are recorded as ``deferred`` so R1 retries them forever.
    "semester_not_tracked", "inconsistent_state",
    # ADDED for the R2b wall-clock budget. ``deferred`` so R1 retries the
    # remainder next run: the pass ran out of TIME, which is emphatically not a
    # decision about the notice.
    "run_budget_exhausted",
    # ADDED for the Devanagari fail-safe. The authoritative detail title could
    # be READ, but nothing in either script's vocabulary was recognised in it,
    # so no verdict about the notice could be formed. That is evidence about US
    # (our token set), not about the notice, and `irrelevant` is permanent under
    # R4 - so it is recorded ``deferred`` and the floor does not move. 5 of the
    # 14 notices on the live result feed are in Nepali, and a Nepali semester-4
    # result recorded this way is a BURN. See classify_semester.
    "title_script_unknown",
    # ADDED 2026-09-30, for the CSIT-marker gap. ``no_csit_marker`` means the
    # title DID carry a semester token and a result term - both positively
    # recognised - but no CSIT marker, so we cannot tell whether it is the
    # owner's programme or another faculty's result (this feed carries B.Tech
    # and M.Sc. notices too). The marker requirement is load-bearing, so the
    # notice cannot be CLAIMED; and keyword absence may never REJECT, so it
    # cannot be dismissed. Recorded ``deferred``: the floor does not move, R1
    # retries, and the gap shouts within one run.
    "no_csit_marker",
    # ADDED 2026-09-30 with the label fix. ``no_semester_number`` is the
    # honest name for "a semester TOKEN was recognised but it carries no
    # number" (the bare Nepali sem token, "Semester" with no ordinal). It was
    # previously reported as no_semester_token, which points a debugger at the
    # wrong gap.
    "no_semester_number",
)

USER_AGENT = (
    "Mozilla/5.0 (compatible; daily-media-news-academic/1.0; "
    "personal result monitor; stdlib-urllib)"
)

KATHMANDU_OFFSET = 5 * 3600 + 45 * 60  # +05:45

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StateError(Exception):
    """State file present but unusable. Caller must refuse to overwrite."""


class StateNotFound(StateError):
    """State file absent. Nothing exists to overwrite; it must be restored."""


class FetchError(Exception):
    """Transient network / HTTP failure. Caller exits 2."""


# ---------------------------------------------------------------------------
# Fetch layer (injectable): fetch_fn(url) -> (status:int, body:bytes)
# ---------------------------------------------------------------------------


def default_fetch_fn(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html, application/pdf, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _host_of(url):
    """Best-effort hostname for the hosts_down map ('' when unparseable)."""
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Notice list parsing
# ---------------------------------------------------------------------------

# Anchor: href + inner text. Handles single/double quotes and whitespace.
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL)

# A notice detail URL carries a trailing numeric id, e.g. /notices/14559.
_NOTICE_ID_RE = re.compile(r"/(?:notice|notices|view)/?(\d+)\b", re.IGNORECASE)

# Date-looking token: YYYY-MM-DD or YYYY/MM/DD or DD-MM-YYYY.
_DATE_RE = re.compile(
    r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b"
    r"|\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")


def _strip_tags(html_fragment):
    return re.sub(r"<[^>]+>", "", html_fragment)


def _clean_ws(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_date(text):
    """Best-effort date extraction from a text blob -> 'YYYY-MM-DD' or ''.

    THE single date parser in this script. F2 requires the exam-notice
    matcher to reuse it rather than adding a second one."""
    m = _DATE_RE.search(text or "")
    if not m:
        return ""
    if m.group(1):
        y, mo, d = m.group(1), m.group(2), m.group(3)
    else:
        d, mo, y = m.group(4), m.group(5), m.group(6)
    try:
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d))
    except ValueError:
        return ""


def parse_notice_list(html):
    """Extract notice cards from an IOST notice page.

    The exact DOM is unverified, so this is deliberately robust: it scans for
    anchors whose href points at a notice detail page (trailing numeric id),
    and pulls the title/date from the surrounding text. Returns a list of
    dicts {id:int, title:str, title_degraded:bool, date:str, url:str,
    pdf_url:str|None}.

    ``title_degraded`` is the R1 load-bearing flag. When the anchor carried no
    inner text the historical code fabricated ``"notice-<id>"`` and the caller
    happily decided "irrelevant" against that placeholder — that fabricated
    string IS the original burn. The placeholder is still produced (display
    only) but the flag tells the caller the title is not authoritative, so the
    id is deferred instead of decided.
    """
    if not html:
        return []
    notices = []
    seen_ids = set()
    for m in _ANCHOR_RE.finditer(html):
        href = m.group(1).strip()
        inner = _clean_ws(_strip_tags(m.group(2)))
        idm = _NOTICE_ID_RE.search(href)
        if not idm:
            continue
        nid = int(idm.group(1))
        if nid in seen_ids:
            continue
        degraded = not inner
        title = inner or ("notice-%d" % nid)
        # The date usually sits in the same card; grab a window around the
        # anchor's inner text from the raw html.
        start = max(0, m.start() - 400)
        window = html[start:m.end() + 200]
        date = _extract_date(window)
        seen_ids.add(nid)
        notices.append({
            "id": nid,
            "title": title,
            "title_degraded": degraded,
            "date": date,
            "url": href,
            "pdf_url": None,
        })
    # Dedup by id, keep first occurrence, sort by id ascending.
    notices.sort(key=lambda n: n["id"])
    return notices


# ---------------------------------------------------------------------------
# Result-feed coverage (P1/P2, verified live 2026-09-29)
# ---------------------------------------------------------------------------
# (P2) THE BOARD PAGINATES WITH rel="next". Confirmed live: page 2 of
# https://iost.tu.edu.np/notices?notice_type=21 adds notices 15313, 15314,
# 15395, 15396 and 15397, and the old code only ever fetched page 1, so those
# notices were invisible to the result pass for as long as they sat on page 2.
# A result on page 2 is a result the owner never hears about.
_RESULT_LIST_MAX_PAGES = 3

# (P1) A RESULT FILED UNDER A DIFFERENT notice_type IS INVISIBLE. Verified
# live: notice 15642 is on /notices but NOT on notice_type=21. The whole-board
# list is therefore UNIONED into the result feed. It is additive by
# construction: unioning more notices can only mean more are examined, and the
# floor logic is unchanged (a notice that turns out irrelevant is decided on
# its own detail page, exactly as before).
DEFAULT_RESULT_FEED_UNION_URLS = ("https://iost.tu.edu.np/notices",)

# Both extra fetches are BOUNDED and their failures are WARNINGS, not exit-2
# failures: a board that stops serving page 2 must not take the primary feed
# down with it, and a silent failure here is exactly how a result path goes
# quietly blind. Each failure prints ::warning:: so it is visible in the
# workflow log.


def parse_next_page_url(html, base_url):
    """The ``rel="next"`` page URL in a notice-list page, or None.

    Both shapes the board may use are accepted: a ``<link rel="next" href=...>``
    in the head and an ``<a rel="next" href=...>`` control in the body.
    Relative hrefs are resolved against ``base_url``.
    """
    if not html:
        return None
    for m in re.finditer(
            r"<a\b[^>]*\brel\s*=\s*[\"']?next[\"']?[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']"
            r"|<link\b[^>]*\brel\s*=\s*[\"']?next[\"']?[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']",
            html, re.IGNORECASE):
        href = (m.group(1) or m.group(2) or "").strip()
        if href and not href.lower().startswith("javascript:"):
            # Unescape: a board that writes href="/notices?type=21&amp;page=2"
            # would otherwise be fetched as the literal string "...&amp;page=2",
            # which the server parses as a parameter named "amp;page".
            return urljoin(base_url, unescape(href))
    return None


def fetch_notice_list(poll_url, fetch_fn=None, timeout=15,
                      max_pages=_RESULT_LIST_MAX_PAGES):
    """GET the notice list page and parse it into notice dicts, following
    ``rel="next"`` pagination up to ``max_pages`` pages.

    Raises FetchError on a failure of the FIRST page - that is the result feed
    and the caller must exit 2. A failure on a FOLLOWING page is a warning:
    page 1's notices are still real and still worth deciding, and refusing them
    because page 2 timed out would be a self-inflicted blindness worse than the
    gap it papers over.
    """
    fetch_fn = fetch_fn or default_fetch_fn
    try:
        status, body = fetch_fn(poll_url, timeout)
    except Exception as exc:
        raise FetchError("fetch notice list %s: %s" % (poll_url, exc))
    if status is not None and int(status) >= 400:
        raise FetchError("fetch notice list %s: HTTP %s" % (poll_url, status))
    html = body.decode("utf-8", errors="replace")
    notices = parse_notice_list(html)
    seen = {n["id"] for n in notices}

    next_url = parse_next_page_url(html, poll_url)
    pages = 1
    while next_url and pages < max_pages:
        try:
            status, body = fetch_fn(next_url, timeout)
            if status is not None and int(status) >= 400:
                raise RuntimeError("HTTP %s" % status)
            page_html = body.decode("utf-8", errors="replace")
        except Exception as exc:
            print("::warning::academic: could not fetch page %d of the result "
                  "feed (%s): %s - notices on that page are NOT being examined"
                  % (pages + 1, next_url, exc), file=sys.stderr)
            break
        for notice in parse_notice_list(page_html):
            if notice["id"] in seen:
                continue
            seen.add(notice["id"])
            notices.append(notice)
        pages += 1
        next_url = parse_next_page_url(page_html, next_url)
    if pages > 1:
        print("academic: result feed paginated - %d notices across %d pages"
              % (len(notices), pages), file=sys.stderr)
    notices.sort(key=lambda n: n["id"])
    return notices


# ---------------------------------------------------------------------------
# Notice detail parsing
# ---------------------------------------------------------------------------

# portal.tu.edu.np notice PDF link.
_PORTAL_PDF_RE = re.compile(
    r"https?://portal\.tu\.edu\.np/[^\"'\s<>]+?\.pdf", re.IGNORECASE)
# Any .pdf link as a fallback.
_ANY_PDF_RE = re.compile(r"https?://[^\"'\s<>]+?\.pdf", re.IGNORECASE)

# ---------------------------------------------------------------------------
# THE SITE-CONSTANT <title> BUG (the 2026-09-28 sem-1 result burn)
# ---------------------------------------------------------------------------
# VERIFIED LIVE against https://iost.tu.edu.np/notices/15673 (the B.Sc.CSIT I
# Semester-2082 Exam Result, 2026-09-28): the detail page's HTML <title> element
# reads the SITE-WIDE CONSTANT
#
#     <title>INSTITUTE OF SCIENCE AND TECHNOLOGY</title>
#
# and the real, notice-specific title lives in the BODY:
#
#     <div class="inner-banner-detail"><h4 class="title">REAL TITLE</h4>
#     <div class="detail-page-inner"><h4 class="title">REAL TITLE</h4>
#     <div class="ck-table"><p>REAL TITLE</p>
#
# The old code read the notice title from <title>. It therefore read the
# CONSTANT, match_semester(constant) returned None, and the run recorded
# `irrelevant` / `no_semester_match` and advanced the floor PAST the notice.
# Under R4 an `irrelevant` verdict is a decision and is never re-fetched, so the
# notice was PERMANENTLY BURNED and the owner's semester-1 pass was never seen.
# The run exited 0 the whole time: a silent, total loss.
#
# Three rules follow, and none of them is optional:
#   1. the BODY is the authoritative source; <title> is a LAST-RESORT fallback
#      and is only trusted when it is not the site constant;
#   2. a title that cannot be read to that standard is UNEXTRACTABLE -> DEFERRED
#      (R1 retries it), never `irrelevant`;
#   3. the h4 search is CONTAINER-SCOPED. The real page also carries
#      <h4 class="title">More Notices</h4> (a section heading). A page-wide
#      h4.title scan returns "More Notices" for some notices, which is a
#      different silent burn - the mirror image of this one.

# Elements whose class marks them as the notice-title container on the live
# page. Checked in order; the FIRST container that yields a non-empty title wins.
_TITLE_CONTAINER_CLASSES = ("inner-banner-detail", "detail-page-inner")
# Secondary source: the CKEditor body block re-states the title in a <p>.
_TITLE_CK_TABLE_CLASS = "ck-table"
# The h4's own class on the live page.
_TITLE_HEADING_CLASS = "title"

# Titles that are the SITE, not the notice. Compared case-insensitively after
# collapsing whitespace, and as a SUBSTRING too, because the constant is
# routinely decorated ("Institute of Science and Technology - Notices").
SITE_CONSTANT_TITLES = (
    "INSTITUTE OF SCIENCE AND TECHNOLOGY",
    "TRIBHUVAN UNIVERSITY",
    "IOST",
)

# Decoration the board wraps AROUND the constant. Stripped before comparison so
# "Institute of Science and Technology - Notices" is still recognised as the
# constant. These are chrome words, never part of a notice title.
_SITE_CHROME_WORDS = (
    "NOTICE", "NOTICES", "HOME", "PAGE", "PORTAL", "WELCOME", "LOGIN",
    "CONTACT", "ABOUT", "SEARCH", "MENU", "TU", "NEPAL",
)


def _title_remainder(norm):
    """The title with every known constant and chrome word removed.

    What survives is the notice-specific part. A bare site constant leaves
    nothing; a real title leaves its subject matter, which is how the two are
    told apart without trusting either on its own.
    """
    rest = norm
    for const in SITE_CONSTANT_TITLES:
        rest = rest.replace(const, " ")
    words = [w for w in re.split(r"[^A-Z0-9]+", rest) if w]
    return [w for w in words if w not in _SITE_CHROME_WORDS]


def is_site_constant_title(text):
    """True when a candidate title is the site chrome, not a notice title.

    Ways to be site chrome, all verified against the live page:
      * empty / whitespace only;
      * equal to a known constant, ignoring case and whitespace;
      * a known constant plus DECORATION only - the live board appends things
        like " - Notices". The test is: strip the constants and the chrome
        words, and if NOTHING notice-specific is left, it was chrome.

    The subtraction is what makes this safe in both directions. A real title
    keeps its subject matter after the subtraction ("B.Sc.CSIT I SEMESTER 2082
    EXAM RESULT" is untouched by the constants), so it is never mistaken for
    chrome. A decorated constant leaves nothing, so it is caught. This is
    deliberately generous in the safe direction: a false positive defers one
    notice for a run and is retried, while a false negative burns a result
    forever - which is the bug this whole block exists to prevent.
    """
    norm = _clean_ws(text or "").upper()
    if not norm:
        return True
    # A non-ASCII title cannot be one of the known (ASCII) constants. This is a
    # real case on the live board, not a theoretical one: most IOST notices are
    # titled in Nepali Devanagari. Without this, `_title_remainder` below would
    # strip every character as "not A-Z0-9", find nothing left, and call a
    # perfectly good Nepali title site chrome - deferring those notices forever.
    if any(ord(ch) > 127 for ch in norm):
        return False
    if not _title_remainder(norm):
        return True
    return False


def _classes_of(attrs):
    for name, value in attrs:
        if name and name.lower() == "class":
            return (value or "").lower().split()
    return []


class _NoticeTitleParser(HTMLParser):
    """Pull the notice title out of an IOST DETAIL page.

    Sources, in priority order:
      1. ``<h4 class="title">`` inside a title container
         (``.inner-banner-detail`` / ``.detail-page-inner``) - the real title;
      2. ``<p>`` inside ``.ck-table`` - the CKEditor body, which re-states it;
      3. ``<title>`` - the FALLBACK, and only when it is not the site constant.

    A page-wide ``<h4 class="title">`` scan is deliberately NOT used: the live
    page also contains ``<h4 class="title">More Notices</h4>``, a section
    heading that belongs to no notice. Container scoping is what keeps that
    out.

    ``title`` is "" when no authoritative title exists. That is NOT an error and
    NOT a decision: the caller must DEFER (R1), because "we could not read it"
    is not "it is not a result".
    """

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.title = ""
        self.title_source = ""      # container_h4 | ck_table_p | html_title
        self.title_degraded = False  # True => a constant/empty title, not a title
        self._classes = []           # stack of class-lists
        # Depths are recorded as the stack length INCLUDING the target element,
        # which is exactly what ``handle_endtag`` sees before it pops. So a
        # capture ends only on the matching close tag of that element, never on
        # a nested one.
        self._cap_depth = None       # set while capturing the target element
        self._cap_target = ""        # "h4" | "p"
        self._buf = []
        self._head_depth = None
        self._head_title = ""      # the <title> fallback, applied at close()
        self._done = False

    # -- helpers ---------------------------------------------------------
    def _in_container(self):
        for classes in self._classes:
            if any(c in _TITLE_CONTAINER_CLASSES for c in classes):
                return True
        return False

    def _in_ck_table(self):
        for classes in self._classes:
            if _TITLE_CK_TABLE_CLASS in classes:
                return True
        return False

    def _emit(self, source):
        text = _clean_ws("".join(self._buf))
        self._buf = []
        self._cap_depth = None
        self._cap_target = ""
        if self.title or self._done:
            return
        self._done = True
        if text:
            self.title = text
            self.title_source = source
        else:
            self.title_degraded = True

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        classes = _classes_of(attrs)
        if self._done:
            self._classes.append(classes)
            return
        if tag == "title" and self._head_depth is None and not self.title:
            self._classes.append(classes)
            self._head_depth = len(self._classes)
            return
        self._classes.append(classes)
        if (tag == "h4" and not self.title and not self._cap_target
                and _TITLE_HEADING_CLASS in classes and self._in_container()):
            self._cap_depth = len(self._classes)
            self._cap_target = tag
            self._buf = []
        elif (tag == "p" and not self.title and not self._cap_target
                and self._in_ck_table()):
            self._cap_depth = len(self._classes)
            self._cap_target = tag
            self._buf = []

    def handle_startendtag(self, tag, attrs):
        # Void/self-closed tag: nothing to capture, but the class stack must
        # not grow either, or every later depth comparison is off by one.
        if not self._done and self._cap_target and tag.lower() == self._cap_target:
            self._emit("container_h4" if self._cap_target == "h4"
                       else "ck_table_p")
        self._classes.append(_classes_of(attrs))
        self._classes.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        # depth BEFORE popping == the depth recorded at the matching start tag.
        depth = len(self._classes)
        if self._classes:
            self._classes.pop()
        if self._done:
            return
        if self._cap_target and depth == self._cap_depth \
                and tag == self._cap_target:
            self._emit("container_h4" if self._cap_target == "h4"
                       else "ck_table_p")
        elif tag == "title" and self._head_depth == depth:
            # The <title> is the LAST-RESORT source, so it is only STORED here.
            # It must not finish the parse: the real title lives in the body and
            # is parsed after </head>, and finalising on the constant would
            # discard it. The fallback is applied in close().
            self._head_depth = None
            self._head_title = _clean_ws("".join(self._buf))
            self._buf = []

    def handle_data(self, data):
        if self._done:
            return
        if self._cap_target or self._head_depth is not None:
            self._buf.append(data)

    def close(self):
        HTMLParser.close(self)
        if self._done:
            return
        self._done = True
        # LAST-RESORT fallback: the <title>, and only when it is not the site
        # constant. This is the path the pre-fix code used for EVERY notice,
        # which is why it burned the sem-1 2082 result.
        if self._head_title and not is_site_constant_title(self._head_title):
            self.title = self._head_title
            self.title_source = "html_title"
            return
        # A truncated page, a page with no <title> and no body title, or a page
        # whose only title is the site constant: non-authoritative by
        # definition, not by accident. The caller must DEFER (R1).
        self.title_degraded = True


def parse_notice_detail_html(html):
    """Detail-page HTML -> {title, title_source, title_degraded}.

    Pure and offline, so the site-constant rule is testable without a network
    touch. ``title_degraded`` is the R1 load-bearing flag and means exactly what
    ``title_degraded`` means on the list parser: the string in ``title`` is NOT
    an authoritative notice title, so the caller must defer rather than decide.
    """
    out = {"title": "", "title_source": "", "title_degraded": True}
    if not html:
        return out
    parser = _NoticeTitleParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # A parse failure is a READ failure, never a decision.
        return out
    out["title"] = parser.title
    out["title_source"] = parser.title_source
    out["title_degraded"] = bool(parser.title_degraded or is_site_constant_title(
        parser.title))
    return out


def fetch_notice_detail(notice_url, fetch_fn=None, timeout=15):
    """GET a notice detail page, return
    {id, title, title_source, title_degraded, date, url, pdf_url}. pdf_url is
    the portal.tu.edu.np link when present, else any .pdf link, else None.
    Raises FetchError on HTTP/parse failure.

    The title comes from :func:`parse_notice_detail_html`, i.e. from the BODY
    of the page. The HTML ``<title>`` element is a fallback only, because the
    live IOST detail page puts the site-wide constant
    ``INSTITUTE OF SCIENCE AND TECHNOLOGY`` in it for every notice - see the
    long comment above :data:`SITE_CONSTANT_TITLES` for the burn that caused.
    """
    fetch_fn = fetch_fn or default_fetch_fn
    try:
        status, body = fetch_fn(notice_url, timeout)
    except Exception as exc:
        raise FetchError("fetch detail %s: %s" % (notice_url, exc))
    if status is not None and int(status) >= 400:
        raise FetchError("fetch detail %s: HTTP %s" % (notice_url, status))
    text = body.decode("utf-8", errors="replace")
    idm = _NOTICE_ID_RE.search(notice_url)
    nid = int(idm.group(1)) if idm else 0
    parsed = parse_notice_detail_html(text)
    title = parsed["title"]
    title_degraded = parsed["title_degraded"]
    if is_site_constant_title(title):
        # Belt and braces: the constant is never an authoritative title, even
        # if a container ever wrapped it.
        title = ""
        title_degraded = True
    pdf_url = None
    pm = _PORTAL_PDF_RE.search(text)
    if pm:
        pdf_url = pm.group(0)
    else:
        am = _ANY_PDF_RE.search(text)
        if am:
            pdf_url = am.group(0)
    return {
        "id": nid,
        "title": title,
        "title_source": parsed["title_source"],
        "title_degraded": title_degraded,
        "date": _extract_date(text),
        "url": notice_url,
        "pdf_url": pdf_url,
    }


# ---------------------------------------------------------------------------
# Semester matching (F3 — order-independent, two-stage)
# ---------------------------------------------------------------------------

# Roman numeral -> arabic.
_ROMAN = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8,
}
_ARABIC_WORDS = {
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "6th": 6, "7th": 7, "8th": 8,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8,
}

# Semester token alternatives (Roman + Arabic + word forms). Ordered
# longest-first so re.search picks "IV" over "I", "VIII" over "V", etc.
# Mirrored verbatim as ACADEMIC_SEM_TOKEN in deploy/cloudflare-worker.js —
# tests/academic/test_worker_parity.py fails on drift.
_SEM_TOKEN = (
    r"(?:VIII|8th|Eighth|VII|7th|Seventh|VI|6th|Sixth|IV|4th|Fourth"
    r"|III|3rd|Third|II|2nd|Second|V|5th|Fifth|I|1st|First)"
)

# Stage 1(a): a CSIT marker.
_MARKER_RE = re.compile(r"(?:B\.?\s*Sc\.?|BSc|CSIT)", re.IGNORECASE)

# NON-CSIT programme markers. ADDED 2026-09-30.
#
# The live IOST result feed is NOT a CSIT-only feed: of the 14 notices on the
# 2026-09-29 capture, 15398 / 15672 are B.Tech.(Food Technology), 15442 /
# 15443 / 15444 / 15565 / 15697 are M.Sc., and 15671 is a joint
# "B.Sc.CSIT and Bachelor in Information Technology (BIT)" calendar. Every one
# of those spellings is read off the live board, not guessed.
#
# WHY A SEPARATE VOCABULARY. The CSIT marker is load-bearing: dropping it would
# make every M.Sc./B.Tech result a CSIT match. This list is what lets the
# marker stay load-bearing AND lets "no marker" stop being a silent shrug. It
# is consulted ONLY in the negative direction - marker absent AND one of these
# present => a POSITIVE no_match. A False here may never reject anything; the
# defer path in classify_semester covers that case instead.
#
# EXTENDED 2026-10-03 with the markers the archived render/state data actually
# carries, so the exam-notice relevance filter (:func:`is_other_programme_notice`)
# rejects the programmes the owner reported rather than an invented list:
#   * MPA, MBS  - named in the archived CSIT notice prose in
#     state/active_stories.json and 3 rendered digests each.
#   * Ph.D     - the owner's own report, and the doctorate programme is real
#     on this board: notice 15515's Nepali title is विद्यावारिधि (see the
#     Nepali list in devanagari.py, which gained the same word).
#   * BBS / BBA / BIM / B.Ed - the other faculties' UG programmes on the same
#     TU board. Not present in the current capture; listed so a notice from one
#     of them is rejected on its first appearance instead of after a report.
#
# EVERY alternative added here is `\b`-anchored. That is not decoration: a
# boundary-less `B\.?\s*Ed\.?` matches the "bed" inside "described", and a
# boundary-less `M\.?A\.?` matches "MA" inside any capitalised pair. The
# pre-2026-09-30 alternatives are deliberately left byte-identical - they are
# already unambiguous and pinned by tests/academic/test_academic_result.py.
_NON_CSIT_MARKER_RE = re.compile(
    r"(?:M\.?\s*Sc\.?|MPhil|B\.?\s*Tech\.?|Bachelor\s+in\s+Information"
    r"\s+Technology|\bBIT\b|B\.?\s*Pharm|MBA|MSc"
    r"|\bMPA\b|\bMBS\b|\bBBS\b|\bBBA\b|\bBIM\b|\bB\.?Ed\b|\bPh\.?D\.?\b)",
    re.IGNORECASE)
# Stage 1(c): a result term. Deliberately wide — see F3. A false positive
# costs one detail fetch and one PDF download; a false negative loses a
# result forever. Over-matching is SAFE here, under-matching is the killer.
_RESULT_TERMS_RE = re.compile(
    r"\b(?:result|results|back|re-?exam|supplementary|provisional"
    r"|marks|transcript)\b", re.IGNORECASE)

# Stage 1: marker AND semester token AND result term, IN ANY ORDER.
# The ordering constraint of the old SEMESTER_RE ("CSIT … sem token …
# Semester … Result") is DELETED: TU is not obliged to word a title in that
# order, and the old ordering was a silent under-matcher.
SEMESTER_RE = re.compile(
    r"(?=.*" + _MARKER_RE.pattern + r")"
    r"(?=.*\b" + _SEM_TOKEN + r"\b)"
    r"(?=.*" + _RESULT_TERMS_RE.pattern + r")",
    re.IGNORECASE | re.DOTALL)

_SEM_TOKEN_SEARCH_RE = re.compile(r"\b" + _SEM_TOKEN + r"\b", re.IGNORECASE)


def _semester_from_token(token):
    """Map a matched semester token to an int 1-8, or None."""
    low = token.lower()
    if low in _ARABIC_WORDS:
        return _ARABIC_WORDS[low]
    up = token.upper()
    if up in _ROMAN:
        return _ROMAN[up]
    # bare arabic digit like "4"
    if re.fullmatch(r"[1-8]", token):
        return int(token)
    return None


def _match_semester_latin(title):
    """The Latin stage-1 semester matcher, byte-identical in behaviour to what
    the Cloudflare Worker mirrors in JavaScript.

    Kept private and uncallable from the Worker: this is the ONE Latin matcher,
    and :func:`classify_semester` is the only thing that should reach for it.
    """
    if not title:
        return None
    if not SEMESTER_RE.search(title):
        return None
    tm = _SEM_TOKEN_SEARCH_RE.search(title)
    if not tm:
        return None
    return _semester_from_token(tm.group(0))


def match_semester(title):
    """Return the semester number (1-8) a CSIT result title refers to, or
    None if it is not a clear CSIT semester result. ``int|None``, unchanged.

    This is a thin delegation to :func:`classify_semester`, so the frozen
    invariant ``match_semester(t) == classify_semester(t)["semester"]`` holds
    for EVERY title, in both scripts, by construction rather than by
    discipline. For a Latin title the behaviour is byte-identical to the
    historical two-stage matcher (SEMESTER_RE proves marker + semester token +
    result term in ANY order, then the token is extracted), which is what
    tests/academic/test_worker_parity.py compares against the JS regex.

    KNOWN, DELIBERATE DIVERGENCE FROM THE WORKER: for a Devanagari (Nepali)
    title this returns a semester where the Worker's JavaScript returns None,
    because the Devanagari stage exists only in Python. That is intended: the
    Worker is a two-minute detection DELIVERY path that wakes the workflow, and
    the workflow's twice-daily `schedule` backstop plus this decision path are
    what actually decide. A Worker that could not see Nepali would simply fail to
    wake the run early, and the schedule catches it. The reverse - a Worker that
    decided wrongly - is the failure that burned the owner's semester-1 pass,
    and it has been removed from the decision path entirely.
    """
    return classify_semester(title)["semester"]


def _nepali_markers(title):
    """True when the title carries a CSIT marker, in either script."""
    return bool(_MARKER_RE.search(title)) or devanagari.has_nepali_marker(title)


def _non_csit_markers(title):
    """True when the title names ANOTHER programme, in either script.

    Used only to REJECT, never to match, and only when the CSIT marker is
    absent. See :data:`_NON_CSIT_MARKER_RE` for why this is a separate
    vocabulary rather than more marker terms.
    """
    return bool(_NON_CSIT_MARKER_RE.search(title)) \
        or devanagari.has_nepali_non_csit_marker(title)


def is_other_programme_notice(title):
    """THE FAIL-SAFE. True only when this notice is EXPLICITLY another
    programme's, so the exam-notice lane can drop it.

    Read the three branches in order; the THIRD one is the safety property.

    1. An unreadable/empty title is NOT another programme's. We know nothing
       about it, so we must not act on that ignorance.
    2. A CSIT/B.Sc marker wins outright, even when the title ALSO names another
       programme. Live notice 15671 is exactly that shape -
       "Academic Calendar of B.Sc.CSIT and Bachelor in Information Technology
       (BIT) V Semester-2081 Batch" - and it IS the owner's notice. A combined
       notice is kept, because being named in it is what makes it relevant.
    3. NO programme marker at all is NOT evidence of irrelevance and is NOT
       evidence of relevance either: it is AMBIGUITY, and ambiguity must never
       become silent exclusion.

    WHY BRANCH 3 IS WRITTEN AS "KEEP" AND NOT AS A THIRD REJECT REASON. The cost
    is asymmetric and the asymmetry decides the code: a wrongly-DROPPED CSIT
    notice is a real routine, form, admission card or deadline the owner needed
    and never sees again, while a wrongly-KEPT M.Sc notice is one line he
    scrolls past. This project has already paid the expensive half of that
    trade twice - `looks_provably_irrelevant` burned a real semester by
    rejecting on a keyword's ABSENCE, and the CSIT-marker-gap branch of
    :func:`classify_semester` exists only because absence-based rejection is
    not safe here. So the filter may reject on POSITIVE evidence of another
    programme and on nothing else. If a genuinely irrelevant notice slips
    through, the cost is one line in a digest. If this ever needs to become
    stricter, add a positively-recognised marker - never narrow branch 3.

    Matching is delegated to :func:`_nepali_markers` and :func:`_non_csit_markers`,
    i.e. to the ONE existing CSIT marker regex and the ONE existing non-CSIT
    vocabulary (:data:`_NON_CSIT_MARKER_RE` plus
    ``devanagari.NEPALI_NON_CSIT_MARKER_TERMS``). This function is the single
    place the rule lives; the two lists are not duplicated anywhere.
    """
    if not title or not str(title).strip():
        return False
    if _nepali_markers(title):
        return False          # branch 2: ours, possibly jointly published
    return _non_csit_markers(title)


def _result_terms(title):
    """True when the title carries a result term, in either script."""
    return bool(_RESULT_TERMS_RE.search(title)) \
        or devanagari.has_nepali_result_term(title)


def _semester_tokens(title):
    """True when the title carries a semester token, in either script.

    Latin: any of the Roman / ordinal / word forms in :data:`_SEM_TOKEN`.
    Nepali: the ordinal words in :data:`devanagari.NEPALI_SEMESTER_WORDS`, or
    the word for "semester" itself. The bare Nepali sem token counts as a
    token but carries no number, so a title can be a result and still be
    unresolvable to a semester - which is a `no_semester_token` no_match, not
    an `unknown`.
    """
    if _SEM_TOKEN_SEARCH_RE.search(title):
        return True
    if devanagari.nepali_semester_from_words(title) is not None:
        return True
    return devanagari.has_nepali_sem_token(title)


def classify_semester(title):
    """Classify a notice title: does it name a CSIT semester result, and if not,
    WHY not - in a way that is safe to act on.

    THE INVARIANT: ``match_semester(title) == classify_semester(title)
    ["semester"]``, for every title, in both scripts. :func:`match_semester`
    remains the Latin-only ``int|None`` the Cloudflare Worker mirrors, and this
    function is the rich entry point that adds the Devanagari stage on top.

    Returns
    -------
    dict with EXACTLY these keys:
        status      "match" | "no_match" | "unknown" - exactly one
        semester    int 1-8, non-None IFF status == "match", else None
        batch_year  int or None
        script      "latin" | "devanagari" | "mixed" | "empty"
        reason      str, see below
        stage_hits  {"marker": bool, "semester": bool, "result": bool}

    reason, exhaustively:
        match     "devanagari" | "latin"
        no_match  "no_semester_token" | "no_semester_number" | "no_marker"
                  | "no_result_term" | "devanagari_non_result"
                  | "other_program"
        unknown   "unreadable_script" | "no_nepali_vocabulary"
                  | "no_csit_marker"

    THE FAIL-SAFE, and why it is evidence-based rather than script-based
    ---------------------------------------------------------------------
    The naive fix for the Nepali burn - "any Devanagari title we cannot parse
    is `unknown`, defer it" - is wrong in BOTH directions and neither is
    survivable:

      * if every Devanagari title were ``unknown``, the floor freezes on the
        first Nepali notice forever and the owner's result is never read;
      * if every Devanagari title were ``no_match``, a Nepali semester-4
        result is recorded ``irrelevant``, the floor advances past it, and
        under R4 it is burned permanently. That already happened: notice 15696.

    So ``unknown`` is returned ONLY when NO recognised vocabulary of any kind
    is found in the title: no Latin marker / sem-token / result term AND no
    Nepali marker / sem-word / sem-token / result term. In that state we have
    learned NOTHING about the notice - we simply do not have the words this
    board uses - and ``unknown`` is the honest verdict. If we DID recognise
    anything, the absence of the remaining tokens is real evidence about the
    notice and ``no_match`` is correct.

    The one residual gap this leaves is a Nepali NON-result notice carrying
    none of the result or marker terms: nothing is recognised, so it reads
    ``unknown`` and defers forever. That is closed with the PRESENCE-only list
    :data:`devanagari.NEPALI_NON_RESULT_TERMS`: a Devanagari title with at
    least one of those words and zero result terms is a positively recognised
    administrative notice -> ``no_match`` / ``devanagari_non_result``.

    ABSENCE OF KEYWORDS NEVER REJECTS. The module already has a tombstone for
    that mistake (:func:`looks_provably_irrelevant`): absence of a result term
    in a title is not proof of irrelevance. Every list below is consulted for
    PRESENCE only.
    """
    if not title or not str(title).strip():
        return {
            "status": "unknown",
            "semester": None,
            "batch_year": None,
            "script": "empty",
            "reason": "unreadable_script",
            "stage_hits": {"marker": False, "semester": False, "result": False},
        }

    script = devanagari.detect_script(title)
    stage_hits = {
        "marker": _nepali_markers(title),
        "semester": _semester_tokens(title),
        "result": _result_terms(title),
    }
    batch_year = _extract_batch_year(title)
    # A Nepali ordinal word is the ONLY source of a semester number in a
    # Devanagari title; the Latin stages below are then run on the
    # digit-latinised text so "२०८१" is visible to the already-verified ASCII
    # regexes. Reuse, do not reimplement: a second semester matcher is a second
    # thing to get wrong.
    latinised = devanagari.latinize_digits(title)
    semester = devanagari.nepali_semester_from_words(title)
    if semester is None:
        # No Nepali ordinal word. The Latin stages then run unchanged on the
        # digit-latinised text: for a Latin title latinised IS title, so this
        # is exactly the historical matcher; for a Nepali title it makes
        # "२०८१" and any embedded Roman/ordinal token visible to the
        # already-verified ASCII regexes. Reuse, do not reimplement.
        semester = _match_semester_latin(latinised)

    if stage_hits["marker"] and stage_hits["semester"] and stage_hits["result"] \
            and semester is not None:
        return {
            "status": "match",
            "semester": semester,
            "batch_year": batch_year,
            "script": script,
            "reason": "devanagari" if script in ("devanagari", "mixed")
                      else "latin",
            "stage_hits": stage_hits,
        }

    # --- ANOTHER PROGRAMME: a POSITIVE rejection --------------------------
    # The result feed is not CSIT-only (see _NON_CSIT_MARKER_RE). A title that
    # names B.Tech / M.Sc. / BIT and carries NO CSIT marker is positively
    # recognised as another faculty's notice, so no_match is evidence-based
    # here, not an absence-based shrug. Checked only when the CSIT marker is
    # absent, so the joint 15671 notice is untouched.
    non_csit = _non_csit_markers(title)
    if non_csit and not stage_hits["marker"]:
        return {
            "status": "no_match",
            "semester": None,
            "batch_year": batch_year,
            "script": script,
            "reason": "other_program",
            "stage_hits": stage_hits,
        }

    # --- THE CSIT-MARKER GAP: DEFER, NEVER DECIDE -------------------------
    # A semester token AND a result term were both POSITIVELY recognised and
    # the CSIT marker is absent. This is precisely the shape that burned a
    # real semester: _match_semester_latin needs the marker, so `semester`
    # stayed None, the old `any(stage_hits)` branch read "I recognised
    # something" as a decision, _process_result_feed recorded `irrelevant` /
    # no_semester_match, and R4 made it permanent. A detail title of "Fourth
    # Semester Result 2082" with the roll in the PDF burned a semester-4
    # result in exactly that way.
    #
    # The marker cannot simply be dropped: this feed carries M.Sc. and B.Tech
    # results, so claiming them would be a false positive on someone else's
    # result. Nor may the absence reject, per the tombstone in
    # looks_provably_irrelevant. So this DEFERs: status "unknown", which the
    # result pass records as `deferred` under the reason no_csit_marker. The
    # floor does not move, R1 retries, and the guard below shouts if the gap
    # is systematic. Nothing here converts a decision into a defer or the
    # reverse: this branch is strictly new territory (it used to be no_match).
    if stage_hits["semester"] and stage_hits["result"] \
            and not stage_hits["marker"]:
        return {
            "status": "unknown",
            "semester": None,
            "batch_year": batch_year,
            "script": script,
            "reason": "no_csit_marker",
            "stage_hits": stage_hits,
        }

    if any(stage_hits.values()):
        # We recognised SOMETHING. The absence of the other tokens is now real
        # evidence about the notice, so this is a decision, not a shrug.
        #
        # The labels are now HONEST about which token was missing, because the
        # marker gap is handled above: `semester` is None *precisely because
        # the marker was missing* in the old code, so a marker-less M.Sc. title
        # reported no_semester_token and sent a debugger to the wrong gap.
        if not stage_hits["semester"]:
            reason = "no_semester_token"
        elif not stage_hits["result"]:
            reason = "no_result_term"
        elif semester is None:
            # A semester TOKEN was recognised but it carries no number (the
            # bare Nepali sem token, "Semester" with no ordinal). That is a
            # different gap from "no token at all", and it was previously
            # mislabelled as no_semester_token, which pointed a debugger at
            # the token set when the real problem was the ordinal.
            reason = "no_semester_number"
        elif not stage_hits["marker"]:
            # Defensive fallback. The marker gap is handled above by the
            # no_csit_marker defer, so this is unreachable while that branch
            # stands; it is kept so the vocabulary stays complete if the two
            # are ever separated again.
            reason = "no_marker"
        else:
            reason = "no_result_term"
        return {
            "status": "no_match",
            "semester": None,
            "batch_year": batch_year,
            "script": script,
            "reason": reason,
            "stage_hits": stage_hits,
        }

    # Nothing recognised at all. In a LATIN title that is not ignorance, it is
    # a verdict: our Latin vocabulary has been running against this board for
    # months and this title simply is not a CSIT result notice. Deferring
    # every unrecognised Latin title would freeze the floor on the first
    # "Library extended opening hours".
    if script == "latin":
        return {
            "status": "no_match",
            "semester": None,
            "batch_year": batch_year,
            "script": script,
            "reason": "no_semester_token",
            "stage_hits": stage_hits,
        }

    # Devanagari (or mixed) with nothing recognised. Close the non-result gap
    # with a PRESENCE test; anything else is a genuine token-set gap.
    if devanagari.has_nepali_non_result_term(title) \
            and not stage_hits["result"]:
        return {
            "status": "no_match",
            "semester": None,
            "batch_year": batch_year,
            "script": script,
            "reason": "devanagari_non_result",
            "stage_hits": stage_hits,
        }
    if script == "mixed":
        # Latin letters are present and we recognised none of our Latin
        # vocabulary either, so this is not a Devanagari reading problem: the
        # title is simply not a result notice. Deferring it would freeze the
        # floor for no gain.
        return {
            "status": "no_match",
            "semester": None,
            "batch_year": batch_year,
            "script": script,
            "reason": "no_semester_token",
            "stage_hits": stage_hits,
        }
    return {
        "status": "unknown",
        "semester": None,
        "batch_year": batch_year,
        "script": script,
        "reason": ("unreadable_script" if script == "empty"
                   else "no_nepali_vocabulary"),
        "stage_hits": stage_hits,
    }


def looks_like_result(title):
    """True when the title carries a result term regardless of a CSIT marker.

    This is the ambiguity signal used by the result pass: a list title with a
    result term but no CSIT marker could still be a CSIT result once the
    authoritative detail page is read, so the id is NOT decided from the list
    title — it gets a detail fetch (R1).
    """
    if not title:
        return False
    return bool(_RESULT_TERMS_RE.search(title))


def looks_provably_irrelevant(title):
    """REMOVED. Kept as a tombstone on purpose.

    This used to let the result pass skip a notice on the strength of its LIST
    title, and it was a burn. Deciding relevance from a board index title is
    never sound: the index truncates and ellipsises, and TU is under no
    obligation to include the word "result". A real counter-example was
    confirmed against a fixture: list title "B.Sc. CSIT Fourth Semester",
    detail title "...Result Published". The list title carries a CSIT marker
    and no result term, so this function said "irrelevant", the floor advanced,
    and the result was never read.

    The replacement is not a smarter heuristic. It is the absence of a
    heuristic: the result pass now fetches the authoritative title for EVERY
    pending notice. Do not re-add a keyword-ABSENCE test here or anywhere on
    the result path. Keyword presence may BOOST a match; keyword absence may
    never REJECT one.
    """
    raise NotImplementedError(
        "looks_provably_irrelevant was removed: absence of a result term in a "
        "LIST title is not proof of irrelevance (R1)")


_BATCH_YEAR_RE = re.compile(r"\b(20[6-9]\d)\b")


def _extract_batch_year(title):
    """Extract a 4-digit batch year (2060-2099) from a notice title, or
    None if no explicit batch year is present.

    Devanagari-aware, and it has to be: a Nepali title writes the year as
    "२०८१", which the ASCII regex cannot see at all. The text is passed
    through :func:`devanagari.digit_boundary_projection` first, which maps the
    ten Nepali digits to ASCII (exhaustive, exactly reversible) and blanks the
    surrounding Devanagari letters to spaces so the ``\\b`` boundaries still
    hold. A pure-Latin title passes through byte-identically, so every existing
    caller and test is unaffected.
    """
    if not title:
        return None
    m = _BATCH_YEAR_RE.search(devanagari.digit_boundary_projection(title))
    return int(m.group(1)) if m else None


def notice_matches_student(notice_title, semester, state):
    """Determine whether a CSIT semester-result notice is relevant to the
    tracked student, considering batch year.

    Called AFTER ``match_semester`` confirms the semester number.  Returns
    True if the notice should be processed, False to skip it.

    Rules
    -----
    1. Notice title contains the student's batch (>= 2081) → match.
    2. Notice title has NO explicit batch year (ambiguous; TU sometimes
       omits it) → match.
    3. Notice title contains an older batch year, but the semester is in
       ``failed`` status → match (re-exam/back result published under the
       next batch year).
    4. Otherwise → no match (wrong batch, not a re-exam case).
    """
    title_batch = _extract_batch_year(notice_title)
    sem_status = state.get("semesters", {}).get(str(semester), {}).get(
        "status", "pending")

    # (a) no batch year in title → ambiguous → treat as potentially relevant
    if title_batch is None:
        return True

    # (b) batch >= 2081 → matches the student's current/upcoming batch
    if title_batch >= 2081:
        return True

    # (c) older batch — only accept for failed semester re-exam
    if sem_status == "failed":
        return True

    # (d) wrong batch, not a re-exam → reject
    return False


# ---------------------------------------------------------------------------
# Exam-notice coverage (F1 / F2)
# ---------------------------------------------------------------------------


def load_exam_notice_config(path=None):
    """Load config/exam_notices.json. Returns {} when absent/unreadable so a
    missing config can never take the RESULT path down with it."""
    path = path or DEFAULT_EXAM_NOTICES_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _contains_any(text, terms):
    low = (text or "").lower()
    for term in terms or ():
        t = str(term).lower()
        if t and t in low:
            return True
    return False


# Titles that are inherently about a date, so the absence of a date token in
# the title says nothing about whether the notice is deadline-bearing. Every
# term here already appears in config/exam_notices.json notice_terms.
_SCHEDULE_BEARING_TERMS = (
    "routine", "schedule", "calendar", "admission card", "form",
    "registration",
)


def match_exam_notice(title, matchers=None, require_marker=False):
    """F2 exam-notice qualifier.

    A notice qualifies when ``notice_terms`` matches AND (``deadline_terms``
    matches OR the title carries a date-shaped token). The date test REUSES
    :func:`_extract_date` plus the existing batch-year token — this script
    gets exactly one date parser.

    DOCUMENTED WIDENING beyond the literal F2 wording: a title that is
    explicitly about a *schedule* (routine / schedule / calendar / admission
    card / form / registration) self-qualifies even with no date token in the
    title. F2's literal test would silently drop "4th semester examination
    schedule published" — which is the same failure class as the burn this
    whole unit exists to remove, one layer up (a dropped routine instead of a
    dropped result). Every term in :data:`_SCHEDULE_BEARING_TERMS` is already
    present in the config's ``notice_terms``; no new vocabulary is invented.

    ``require_marker`` defaults to False and that is deliberate (F1): the IOST
    feed is already CSIT-only, so gating on a ``CSIT``/``B.Sc`` token would
    drop exactly the notice the owner cannot afford to miss ("Exam Routine
    2082"). Marker terms are a BOOST, not a gate, on a host-scoped feed; a
    TU-wide feed would set require_marker True.
    """
    if not title:
        return False
    m = matchers or {}
    if not _contains_any(title, m.get("notice_terms")):
        return False
    has_deadline_term = _contains_any(title, m.get("deadline_terms"))
    # Date-shaped: a full YYYY-MM-DD / DD-MM-YYYY token, or a TU academic-year
    # token (2082). Both reuse the extractors already here.
    has_date = bool(_extract_date(title)) or bool(_BATCH_YEAR_RE.search(title))
    has_schedule = _contains_any(title, _SCHEDULE_BEARING_TERMS)
    if not (has_deadline_term or has_date or has_schedule):
        return False
    if require_marker and not _contains_any(title, m.get("marker_terms")):
        return False
    return True


def exam_notice_semester(title):
    """Semester a generic exam notice refers to, or None. Uses the same
    semester-token logic as the result path (no second semester parser)."""
    if not title:
        return None
    tm = _SEM_TOKEN_SEARCH_RE.search(title)
    return _semester_from_token(tm.group(0)) if tm else None


# ---------------------------------------------------------------------------
# PDF handling
# ---------------------------------------------------------------------------


def download_pdf(pdf_url, fetch_fn=None, timeout=30, max_bytes=PDF_MAX_BYTES):
    """Download PDF bytes. Raises FetchError on failure.

    ``max_bytes`` is a runaway guard, not a size limit: the real B.Sc.CSIT
    result sheet is ~10.5 MB, so the cap is deliberately far above it. A PDF
    larger than the cap is treated as a fetch failure, which defers (the safe
    direction) rather than pulling an unbounded body into a 15-minute job.
    """
    fetch_fn = fetch_fn or default_fetch_fn
    try:
        status, body = fetch_fn(pdf_url, timeout)
    except Exception as exc:
        raise FetchError("download pdf %s: %s" % (pdf_url, exc))
    if status is not None and int(status) >= 400:
        raise FetchError("download pdf %s: HTTP %s" % (pdf_url, status))
    if max_bytes and isinstance(body, (bytes, bytearray)) \
            and len(body) > max_bytes:
        raise FetchError("download pdf %s: %d bytes exceeds the %d-byte cap"
                         % (pdf_url, len(body), max_bytes))
    return body


def _has_roll_like_tokens(text):
    """True if the text contains at least one 8-digit token (a scanned result
    PDF that OCR'd would have roll numbers)."""
    return re.search(r"\b\d{8}\b", text or "") is not None


# A roll that the PDF prints as "1357 2468" rather than "13572468". The real
# sem-1 2082 sheet does exactly this, so a whitespace-tolerant roll shape is
# evidence the sheet was READ, not evidence it was not.
_SPLIT_ROLL_RE = re.compile(r"\b\d{4}\s+\d{4}\b")

def sheet_text_is_readable(text):
    """True when extracted PDF text is plausibly a real, READ result sheet.

    "The roll is not in this text" is only evidence of absence when the text
    was actually read. The test is deliberately the same gate
    :func:`extract_pdf_text` already uses to decide whether a PDF needs OCR,
    widened to the shape the real sheet uses:

      * an 8-digit token (``13572468``), or
      * a roll split across a column boundary (``1357 2468``) - which is how
        the real sem-1 2082 sheet prints it.

    A PDF with a legacy custom-encoded font that pypdf cannot map extracts to
    non-empty, prose-shaped garbage carrying no roll-shaped token at all. That
    is the case this exists to catch, and it is the exact shape that used to
    produce a FALSE FAILED.

    This is the guard against the most damaging outcome this script has:
    marking a semester failed for a student who passed. It advances
    ``current_semester``, sets ``result_announced``, and tells the owner the
    wrong thing about a real result. So non-empty-but-unreadable must DEFER
    (R1 retries it), never decide.
    """
    if not text:
        return False
    return _has_roll_like_tokens(text) or _SPLIT_ROLL_RE.search(text) is not None


def ocr_capability():
    """Report, without installing or side-effecting anything, whether a scanned
    result PDF can actually be OCR'd in this environment.

    Returns exactly ``{"pytesseract": bool, "binary": bool, "pdf_to_image":
    bool}``. All three are needed and none is sufficient alone:

      * ``pytesseract``  - the Python binding is importable.
      * ``binary``       - the ``tesseract`` executable is on PATH. The binding
        is useless without it: it shells out.
      * ``pdf_to_image`` - something can RASTERISE a PDF page. Without it there
        is nothing to OCR, because a PDF is a page-description language, not a
        bitmap.

    WHY THIS EXISTS. The old OCR branch called ``Image.open(io.BytesIO(
    pdf_bytes))`` on PDF BYTES, which raises ``UnidentifiedImageError``, and
    the bare ``except: pass`` swallowed it. So OCR never ran, on any scanned
    PDF, ever: the sheet was never rasterised, page 2 was never read, and every
    scanned result deferred as ``pdf_unextractable`` forever while the run
    exited 0. On CI this also matters because the dependency install step is
    ``continue-on-error: true``, so a half-installed OCR stack is a normal
    state, not an exotic one.

    PURE: it imports nothing, installs nothing, writes nothing. A later unit's
    canary imports this exact name and shape, so both are frozen.
    """
    return {
        "pytesseract": _module_available("pytesseract"),
        "binary": bool(shutil.which("tesseract")),
        "pdf_to_image": (_module_available("pypdfium2")
                         or _module_available("pdf2image")),
    }


def _module_available(name):
    """True when ``name`` is importable. Uses find_spec so a mere presence check
    never executes the module (a broken PDF library must not raise here)."""
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError, TypeError):
        return False


def _ocr_capability_note():
    """Human-readable list of the OCR pieces that are MISSING, or "" when the
    stack is complete. Used to record honestly WHY a scanned sheet deferred
    instead of failing silently."""
    cap = ocr_capability()
    missing = [k for k, v in cap.items() if not v]
    if not missing:
        return ""
    return "missing: " + ", ".join(missing)


def _rasterise_pdf_pages(pdf_bytes, max_pages=OCR_MAX_PAGES, dpi=OCR_DPI):
    """Rasterise up to ``max_pages`` PDF pages to PNG bytes, page by page.

    Returns a list of PNG byte strings, one per page rendered. An empty list
    means "no rasteriser available" or "the PDF could not be rendered" - both
    are handled by the caller, which never treats them as a decision.

    Prefers pypdfium2 (a self-contained wheel, no system binary) and falls back
    to pdf2image (which needs poppler-utils). Either is enough; a scanned
    result sheet is what this exists for.
    """
    pages = []
    # --- pypdfium2: render(scale=...) takes a scale factor, not a dpi -----
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(pdf_bytes)
        try:
            scale = float(dpi) / 72.0
            for index in range(min(max_pages, len(doc))):
                bitmap = doc[index].render(scale=scale)
                image = bitmap.to_pil()
                try:
                    import io as _io
                    buf = _io.BytesIO()
                    image.save(buf, format="PNG")
                    pages.append(buf.getvalue())
                finally:
                    try:
                        image.close()
                    except Exception:
                        pass
                    bitmap.close()
        finally:
            try:
                doc.close()
            except Exception:
                pass
        if pages:
            return pages
    except Exception:
        pass
    # --- pdf2image: needs poppler, renders whole documents -----------------
    try:
        from pdf2image import convert_from_bytes
        import io as _io
        images = convert_from_bytes(pdf_bytes, dpi=dpi,
                                    first_page=1, last_page=max_pages)
        for image in images or ():
            try:
                buf = _io.BytesIO()
                image.save(buf, format="PNG")
                pages.append(buf.getvalue())
            finally:
                try:
                    image.close()
                except Exception:
                    pass
        return pages
    except Exception:
        return pages


def extract_pdf_text(pdf_bytes, ocr=True):
    """Extract text from PDF bytes.

    Direct text extraction first (pypdf, else pdfplumber, else a minimal
    fallback). If the result is empty or has no 8-digit roll-like tokens
    (a scanned image), fall back to OCR: rasterise page by page to PNG and hand
    each page to pytesseract, bounded to OCR_MAX_PAGES pages at OCR_DPI.
    Gracefully degrades: if any part of the OCR stack is unavailable, returns
    whatever direct extraction gave (possibly ""), and the caller treats empty
    as "couldn't extract", which must NOT mark the student as failed.

    THE OCR BRANCH WAS BROKEN AND ALWAYS HAS BEEN. It did
    ``Image.open(io.BytesIO(pdf_bytes))`` - PIL on PDF BYTES, which raises
    UnidentifiedImageError - and swallowed it in a bare ``except: pass``. So a
    scanned result sheet was never rasterised, page 2 was never read, and every
    scanned result deferred as ``pdf_unextractable`` forever while the run
    exited 0. That is a silent, permanent loss of exactly the notices a scan
    produces. Rasterise first, OCR second, and say so when the stack is
    incomplete.
    """
    text = ""
    # --- Direct text extraction -------------------------------------------
    try:
        from pypdf import PdfReader
        reader = PdfReader(__import__("io").BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        text = ""
    if not text:
        try:
            import pdfplumber
            with pdfplumber.open(__import__("io").BytesIO(pdf_bytes)) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception:
            text = ""
    if not text:
        # Minimal fallback: some PDFs embed text in a way pypdf misses; try a
        # crude stream-text scan. Best-effort only.
        try:
            raw = pdf_bytes.decode("latin-1", errors="ignore")
            # Extract text between BT/ET (text objects) — crude but harmless.
            chunks = re.findall(r"\((.*?)\)\s*Tj", raw)
            text = " ".join(chunks)
        except Exception:
            text = ""
    text = _clean_ws(text)
    # --- OCR fallback for scanned images ----------------------------------
    if ocr and (not text or not _has_roll_like_tokens(text)):
        cap = ocr_capability()
        if not all(cap.values()):
            # Record honestly rather than silently degrading. The caller will
            # defer this notice, and the reason it CANNOT be read is a missing
            # capability, not a property of the notice.
            print("::warning::academic: scanned PDF not OCR'd - OCR capability "
                  "is incomplete (%s). A scanned result sheet will defer on "
                  "every run until pytesseract, the tesseract binary and a "
                  "PDF rasteriser are all present." % _ocr_capability_note(),
                  file=sys.stderr)
            return text
        try:
            import pytesseract
            from PIL import Image
            import io as _io
            ocr_chunks = []
            for png in _rasterise_pdf_pages(pdf_bytes):
                with Image.open(_io.BytesIO(png)) as page_image:
                    ocr_chunks.append(pytesseract.image_to_string(page_image)
                                      or "")
            joined = "\n".join(ocr_chunks)
            # Accept on the SAME shape test the caller uses
            # (:func:`sheet_text_is_readable`), not on a bare 8-digit token.
            # The two disagreed, and the disagreement was a burn: the real
            # sem-1 sheet prints the roll SPLIT across a column boundary
            # ("1357 2468"), so an OCR pass that read the scanned sheet
            # correctly still produced no 8-digit token and its text was
            # thrown away, leaving the notice deferred forever. Widening here
            # cannot manufacture a verdict - the caller re-checks
            # sheet_text_is_readable before it will decide anything.
            if joined and sheet_text_is_readable(joined):
                text = _clean_ws(joined)
        except Exception as exc:
            # Never swallow silently: a failure here is why a real result may
            # go unread for days.
            print("::warning::academic: OCR pass failed (%s: %s); keeping "
                  "whatever direct extraction produced"
                  % (type(exc).__name__, exc), file=sys.stderr)
    return text


def roll_in_text(text, roll_number):
    """True if roll_number appears as a standalone token in extracted text.
    Normalizes whitespace and allows whitespace between digits (e.g. a PDF
    that splits '1357 2468'). Uses word boundaries so '13572468' does NOT
    match inside '135724680'."""
    if not text or not roll_number:
        return False
    norm = _clean_ws(text)
    pattern = r"\b" + r"\s*".join(re.escape(ch) for ch in str(roll_number)) + r"\b"
    return re.search(pattern, norm) is not None


# ---------------------------------------------------------------------------
# R5 — the gap-free floor, as a PURE function (contract D4)
# ---------------------------------------------------------------------------


def _decided_ids(examined):
    """Ids in the examined map that carry a POSITIVE decision.

    ``deferred`` is explicitly NOT a decision ("we could not decide", R1), so
    a deferred id both blocks the floor and is retried."""
    out = set()
    if not isinstance(examined, dict):
        return out
    for key, val in examined.items():
        try:
            nid = int(key)
        except (TypeError, ValueError):
            continue
        outcome = val.get("outcome") if isinstance(val, dict) else None
        if outcome and outcome != "deferred":
            out.add(nid)
    return out


def advance(floor, examined, list_ids):
    """R5. Return the new gap-free floor.

    The floor advances to N iff every id ``m`` with ``floor < m < N`` is
    either already decided (``examined`` with a non-deferred outcome) or
    absent from ``list_ids``. Candidates are the union of ``list_ids`` and the
    decided ids, walked in ascending order. NEVER returns max(ids seen)
    unconditionally — that is the bug this replaces.

    Worked examples (contract D4):
      advance(0, {1,2,4},        [1,2,4]) -> 4
      advance(0, {1,2},          [1,2,4]) -> 4   (3 absent from the list)
      advance(0, {1,2,3:deferred}, [1,2,3,4]) -> 2  and 3 is retried

    Note the two-clause safety story: R5's literal wording is permissive about
    the CANDIDATE itself (the second example requires that), so run() clamps
    the result with R2's cap — ``min(advance(...), last_attempted)`` — so the
    floor can never pass the last id the run actually opened.
    """
    try:
        base = int(floor or 0)
    except (TypeError, ValueError):
        base = 0
    decided = _decided_ids(examined)
    recorded = set()
    if isinstance(examined, dict):
        for key in examined:
            if str(key) == "__floor__":
                continue
            try:
                recorded.add(int(key))
            except (TypeError, ValueError):
                continue
    present = set()
    for i in list_ids or ():
        try:
            present.add(int(i))
        except (TypeError, ValueError):
            continue
    # A candidate is a decided id, or a listed id we have no record of at all.
    # A listed id recorded as ``deferred`` is NOT a candidate: it is undecided,
    # so it must block the floor and be retried (R1 + R3).
    #
    # The gap scan is anchored on the ORIGINAL floor, never on the moving
    # candidate: scanning from the moving candidate makes every CONSECUTIVE
    # undecided id vacuously gap-free (range(cand+1, nid) is empty when
    # nid == cand+1) and walks straight past them.
    cand = base
    for nid in sorted(decided | (present - recorded)):
        if nid <= cand:
            continue
        blocked = False
        for m in range(base + 1, nid):
            if m in decided:
                continue
            if m in present:
                blocked = True
                break
        if not blocked:
            cand = nid
    return cand


# ---------------------------------------------------------------------------
# State load / save (contract D2)
# ---------------------------------------------------------------------------


def _migrate_state(state):
    """Schema 1 -> 2, in memory. Returns True when a migration happened.

    The legacy ``last_checked_notice_id`` is KNOWN-UNRELIABLE: it could have
    advanced past notices nobody ever read, so trusting it as a floor would
    keep the burn alive forever. The honest migration therefore opens a replay
    window: ``examined = {}`` and the floor moves back by REPLAY_WINDOW ids so
    the monitor re-reads the recent past under the new R1 discipline. Replay
    cannot re-notify: a notice whose semester is already ``passed`` with
    ``result_announced`` is recorded ``already_announced`` and never emits a
    payload (see _process_result_feed).
    """
    if state.get("schema_version") == LEGACY_SCHEMA_VERSION:
        old = state.get("last_checked_notice_id")
        try:
            old = int(old)
        except (TypeError, ValueError):
            old = 0
        state["examined"] = {}
        state["pending_since"] = {}
        state["hosts_down"] = {}
        state["last_examined_notice_id"] = max(0, old - REPLAY_WINDOW)
        state["pending_overflow"] = False
        state.pop("last_checked_notice_id", None)
        state["schema_version"] = SCHEMA_VERSION
        return True
    return False


def _validate_state(state):
    if not isinstance(state, dict):
        raise StateError("state root must be an object")
    version = state.get("schema_version")
    if version not in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION):
        raise StateError("unsupported schema_version %r" % (version,))
    roll = state.get("roll_number")
    if not isinstance(roll, str) or not roll:
        raise StateError("roll_number missing/invalid")
    cur = state.get("current_semester")
    if not isinstance(cur, int) or not (1 <= cur <= 8):
        raise StateError("current_semester missing/invalid")
    sems = state.get("semesters")
    if not isinstance(sems, dict) or not sems:
        raise StateError("semesters missing/empty")
    for key, sem in sems.items():
        if not isinstance(sem, dict):
            raise StateError("semester %r not an object" % key)
        if sem.get("status") not in ("pending", "passed", "failed"):
            raise StateError("semester %r bad status %r"
                             % (key, sem.get("status")))
    if version == SCHEMA_VERSION:
        floor = state.get("last_examined_notice_id")
        if floor is not None and not isinstance(floor, int):
            raise StateError("last_examined_notice_id must be an int or null")
        examined = state.get("examined", {})
        if not isinstance(examined, dict):
            raise StateError("examined must be an object")
        for key, entry in examined.items():
            if not str(key).lstrip("-").isdigit():
                raise StateError("examined key %r is not a notice id" % (key,))
            if not isinstance(entry, dict):
                raise StateError("examined[%s] not an object" % key)
            if entry.get("outcome") not in OUTCOMES:
                raise StateError("examined[%s] bad outcome %r"
                                 % (key, entry.get("outcome")))
            # DEFENSIVE REASON SET. A recorded `reason` is NOT validated
            # against REASONS here, deliberately: this validator is the one
            # thing that decides whether the monitor runs at all, and it runs
            # against state files written by OTHER versions of this script. A
            # newer version's reason token would be rejected as a corrupt
            # state -> exit 3 -> the monitor refuses to run at all, and the
            # owner loses every future result alert over a bookkeeping string.
            # That is strictly worse than an unrecognised reason in one entry,
            # and the reason is diagnostic only: it never reaches
            # `_record`, which is where REASONS is enforced on the WRITE path.
            # `title_script_unknown` is therefore accepted here, as it is by
            # _record, and a state written by this version loads unchanged on
            # a version whose REASONS lacks it. The same holds for the tokens
            # added 2026-09-30 - `no_csit_marker` and `no_semester_number` -
            # which is exactly why the reason is a plain string check and not a
            # membership test in REASONS: a new token must never be able to
            # brick the monitor on a state file written by the other version.
            if "reason" in entry and not isinstance(entry.get("reason"), str):
                raise StateError("examined[%s] reason must be a string" % key)
        for name in ("pending_since", "hosts_down", "exam_notice_seen"):
            if name in state and not isinstance(state.get(name), dict):
                raise StateError("%s must be an object" % name)
    return state


def _normalise_semesters(state):
    """Repair a semester entry whose verdict fields are incomplete. In memory.

    ``_validate_state`` used to check only ``status``, so a ``failed``+announced
    semester with NO ``roll_found`` key loaded cleanly. Nothing then compared
    the recorded verdict against the freshly computed one properly:
    ``_process_result_feed``'s verdict-change suppression reads
    ``sem_before.get("roll_found")``, which was ``None`` where the run then
    wrote ``False``, so an unchanged FAILED verdict looked CHANGED and the run
    emitted exactly one duplicate "FAILED" alert.

    NORMALISE rather than REJECT, deliberately. Rejecting would raise
    StateError -> exit 3, i.e. refuse to run at all, on precisely the legacy
    and hand-edited states the schema-1 replay-window migration exists to
    rescue: a state that worked before the check would become a hard outage and
    the owner would lose every future result alert over a missing bookkeeping
    key. A total loss of alerting is a far worse failure than a repaired field,
    and the repaired value is not a guess - see below.

    The value written is derived from ``status``, which ``_validate_state`` has
    already proved is exactly one of pending/passed/failed, and NEVER from
    truthiness of the stored value: ``bool("false") is True``, so coercing a
    mistyped field would invert a verdict. The inference itself is sound because
    the only writer of this file (``process_notice``) always sets ``status`` and
    ``roll_found`` together - ``passed`` implies ``True``, ``failed`` implies
    ``False`` - and ``roll_found`` is therefore fully redundant with ``status``.

    A present-but-contradictory pair (``status: passed`` with
    ``roll_found: false``) cannot come from any writer, so it is logged as a
    warning rather than silently resolved: the human who hand-edited the file
    deserves to know which half was believed.

    Only entries with a verdict are touched. A ``pending``/unannounced entry has
    no verdict to complete, and its ``roll_found`` is never read by the
    suppression, so it is left exactly as found.

    Returns True when anything was repaired.
    """
    repaired = False
    sems = state.get("semesters")
    if not isinstance(sems, dict):
        return False
    for key, sem in sems.items():
        if not isinstance(sem, dict) or not sem.get("result_announced"):
            continue
        stored = sem.get("roll_found")
        expected = sem.get("status") == "passed"
        if isinstance(stored, bool) and stored == expected:
            continue
        if stored is not None and stored is not False:
            print("::warning::semester %r contradicts itself (status=%r with "
                  "roll_found=%r); trusting status"
                  % (key, sem.get("status"), stored), file=sys.stderr)
        sem["roll_found"] = expected
        repaired = True
    return repaired


def load_state(path, meta=None):
    """Read + validate state JSON, migrating schema 1 -> 2 in memory.

    Raises StateNotFound when the file is absent, and StateError when it is
    present but unusable (caller must refuse to overwrite). The two are
    separate because the remedies are opposite: an absent file has to be
    restored, an invalid one must not be replaced.
    ``meta`` is an optional dict; when supplied it receives
    ``{"migrated": bool, "repaired": bool}`` so the caller can print the
    replay-window notice and report any self-healed verdict field.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        raise StateNotFound(
            "state not found: %s -- nothing to overwrite and it will NOT be "
            "re-baselined; restore it from the KV key %r or a local copy"
            % (path, STATE_KV_KEY))
    except (OSError, ValueError) as exc:
        raise StateError("state present but unreadable, %s: %s" % (path, exc))
    try:
        _validate_state(state)
    except StateError as exc:
        raise StateError("state present but invalid, %s: %s" % (path, exc))
    # Between validate and migrate, and for BOTH schema versions: a legacy
    # state and a state written by an older build are exactly where a verdict
    # field can be missing, and the repair must happen before the migration
    # reads the semesters map.
    repaired = _normalise_semesters(state)
    migrated = _migrate_state(state)
    if isinstance(meta, dict):
        meta["migrated"] = migrated
        meta["repaired"] = repaired
    return state


def _unique_tmp(path):
    return "%s.%d.tmp" % (path, os.getpid())


def save_state(path, state):
    """Atomic write (tmp + os.replace)."""
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = _unique_tmp(path)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Examined-map bookkeeping
# ---------------------------------------------------------------------------


def _evict_examined(state, bucket):
    """R4/state hygiene: cap the map at EXAMINED_CAP, lowest ids evicted
    first. The floor lives in its own field, so evicting ids at or below it
    can never lose the floor's own history."""
    cap = state.get("examined_cap", EXAMINED_CAP)
    if not isinstance(cap, int) or cap <= 0:
        cap = EXAMINED_CAP
    mapping = state.get(bucket)
    if not isinstance(mapping, dict):
        return
    if len(mapping) <= cap:
        return
    numeric = []
    for key in mapping:
        try:
            numeric.append((int(key), key))
        except (TypeError, ValueError):
            continue
    numeric.sort()
    for _, key in numeric[:len(mapping) - cap]:
        mapping.pop(key, None)
    # Keep the deferred tracker consistent with the map it describes.
    pending = state.get("pending_since")
    if isinstance(pending, dict) and bucket == "examined":
        for nid in list(pending):
            if str(nid) not in mapping:
                pending.pop(nid, None)


def _record(state, bucket, nid, outcome, reason, when, semester=None):
    """Record one decision for notice id ``nid`` in ``bucket``."""
    if outcome not in OUTCOMES:
        raise ValueError("bad outcome %r" % (outcome,))
    if reason not in REASONS:
        raise ValueError("bad reason %r" % (reason,))
    mapping = state.setdefault(bucket, {})
    mapping[str(nid)] = {
        "outcome": outcome,
        "semester": semester,
        "reason": reason,
        "at": when,
    }
    pending = state.get("pending_since")
    if isinstance(pending, dict):
        if outcome == "deferred":
            pending.setdefault(str(nid), when)
        else:
            pending.pop(str(nid), None)
    _evict_examined(state, bucket)
    return mapping[str(nid)]


# ---------------------------------------------------------------------------
# Notification payload
# ---------------------------------------------------------------------------


def _today_iso():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now = now.fromtimestamp(now.timestamp() + KATHMANDU_OFFSET)
    return now.strftime("%Y-%m-%d")


def _ntfy_block(title, message, tags, priority, click, actions, ntfy_topic):
    return {
        "topic": ntfy_topic,
        "title": title,
        "message": message.replace("**", "").replace("\n\n", "\n"),
        "tags": tags,
        "priority": priority,
        "click": click,
        "actions": actions,
    }


def _worker_base():
    """Base URL for the roll-number result page in the ntfy click target.

    Defaults to the Pages ORIGIN. It used to default to the `digest-auth`
    Worker, which only proxied to that same origin while demanding a SECOND
    Basic-auth identity (`digest`) before the origin asked for its own - two
    prompts, two usernames, and no way for the owner to get in from the
    notification. The Worker was deleted 2026-10-03; the origin is now the
    only host, and `functions/_middleware.js` is the access control.

    Kept under the old name and still overridable via ACADEMIC_WORKER_BASE so
    the override path callers may already set keeps working.
    """
    return (os.environ.get("ACADEMIC_WORKER_BASE")
            or "https://daily-digest.example.pages.dev")


def _build_payload(state, semester, notice, passed, result_date):
    roll = state["roll_number"]
    pdf_url = notice.get("pdf_url") or ""
    notice_url = notice.get("url") or ""
    title = notice.get("title") or ""
    ntfy_topic = os.environ.get("NTFY_TOPIC") or "<NTFY_TOPIC>"
    worker_base = _worker_base()
    web_path = "docs/academic/%s-sem%d.html" % (result_date, semester)
    web_url = "%s/academic/%s-sem%d.html" % (worker_base, result_date, semester)

    if passed:
        status = "passed"
        message = (
            "**PASSED** — Roll %s found in the %s.\n\n"
            "[📄 View result PDF](%s)" % (roll, title, pdf_url)
        )
        ntfy_title = "🎓 CSIT Sem %d Result: PASSED" % semester
        tags = ["🎓", "white_check_mark"]
        priority = 4
    else:
        status = "failed"
        message = (
            "**FAILED** — Roll %s NOT found in the %s. You will re-exam this "
            "semester with the next batch. The monitor will keep watching for "
            "your back/re-exam result." % (roll, title)
        )
        ntfy_title = "🎓 CSIT Sem %d Result: FAILED" % semester
        tags = ["🎓", "x"]
        priority = 3

    # `result` first, always. A PASSED/FAILED result is the one notification the
    # owner must be able to isolate from every other notification on the topic,
    # and before this tag a PASSED result and the morning digest both arrived as
    # priority 4 with no distinguishing tag.
    tags = ntfy_classes.with_class_tags(ntfy_classes.RESULT, tags)

    return {
        "kind": "academic_result",
        "semester": semester,
        "roll_number": roll,
        "passed": passed,
        "status": status,
        "result_date": result_date,
        "notice_id": notice.get("id"),
        "pdf_url": pdf_url,
        "notice_url": notice_url,
        "title": title,
        "message": message,
        "ntfy": _ntfy_block(ntfy_title, message, tags, priority, pdf_url,
                            [{"action": "view", "label": "View result",
                              "url": pdf_url}], ntfy_topic),
        "website": {"path": web_path, "url": web_url},
    }


def _build_exam_notice_payload(state, semester, notice, notice_date):
    """F4 exam_deadline payload. SAME delivery path as a result (the existing
    emit_notification -> ntfy route); NO new channel. There is deliberately no
    pass/fail verdict here — this is a pointer, not a judgement."""
    title = notice.get("title") or ""
    notice_url = notice.get("url") or ""
    date = notice_date or _today_iso()
    ntfy_topic = os.environ.get("NTFY_TOPIC") or "<NTFY_TOPIC>"
    worker_base = _worker_base()
    sem_label = str(semester) if semester else "all"
    web_path = "docs/academic/%s-exam-notice-sem%s.html" % (date, sem_label)
    web_url = "%s/academic/%s-exam-notice-sem%s.html" % (
        worker_base, date, sem_label)
    message = (
        "**CSIT exam notice** — %s\n\n"
        "[Open notice](%s)\n\n"
        "No pass/fail verdict: this is a notice alert (routine, form, "
        "admission card, deadline). Check the notice itself for dates."
        % (title, notice_url)
    )
    ntfy_title = "🎓 CSIT exam notice: %s" % title
    return {
        "kind": "exam_deadline",
        "semester": semester,
        "roll_number": state.get("roll_number", ""),
        "result_date": date,
        "notice_id": notice.get("id"),
        "notice_url": notice_url,
        "title": title,
        "message": message,
        "ntfy": _ntfy_block(ntfy_title, message, ["🎓", "calendar"], 4,
                            notice_url,
                            [{"action": "view", "label": "Open notice",
                              "url": notice_url}], ntfy_topic),
        "website": {"path": web_path, "url": web_url},
    }


def _build_degraded_payload(state_file, exit_code, reason):
    """Degraded-run artifact payload (acceptance criterion 6). A failure must
    reach the owner, not vanish."""
    state = {}
    try:
        with open(state_file, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            state = loaded
    except (OSError, ValueError):
        state = {}
    date = _today_iso()
    web_path = "docs/academic/%s-degraded.html" % date
    ntfy_topic = os.environ.get("NTFY_TOPIC") or "<NTFY_TOPIC>"
    message = (
        "**Academic result monitor did not complete** (exit %s).\n\n%s\n\n"
        "The monitor has NOT concluded anything about your result. It will "
        "retry on the next scheduled run; the watchdog will escalate if the "
        "gap persists."
        % (exit_code, reason)
    )
    return {
        "kind": "degraded",
        "semester": state.get("current_semester"),
        "roll_number": state.get("roll_number", ""),
        "result_date": date,
        "title": "Academic result monitor degraded (exit %s)" % exit_code,
        "message": message,
        "ntfy": _ntfy_block("⚠️ Academic monitor DEGRADED (exit %s)" % exit_code,
                            message, ["warning", "rotating_light"], 5, "", [],
                            ntfy_topic),
        "website": {
            "path": web_path,
            "url": "%s/academic/%s" % (_worker_base(), os.path.basename(web_path)),
        },
    }


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def _no_payload_reason(state, notice, semester=None):
    """Why did the state machine produce NO payload for this notice?

    A None payload used to be recorded as ``processed`` / ``result_announced``.
    That is a lie: the run claimed the result had been announced when it had
    not, the floor advanced, and the notice was never revisited. That is a
    result burn with no error anywhere, so a None now maps to an explicit,
    honest reason, and any cause that is not a definitive positive decision
    maps to ``deferred`` so R1 retries it on every subsequent run.

    Returns (outcome, reason). Reasons ``semester_not_tracked`` and
    ``inconsistent_state`` are additions to the frozen reason vocabulary; see
    REASONS below.
    """
    if semester is None:
        semester = match_semester(notice.get("title") or "")
    sem = (state.get("semesters") or {}).get(str(semester))
    if not isinstance(sem, dict) or not sem:
        return "deferred", "semester_not_tracked"
    if sem.get("status") == "passed":
        if sem.get("result_announced"):
            return "already_announced", "already_announced"
        # passed but never announced is a self-contradictory state. Retrying
        # costs one fetch; guessing could hide a real notice.
        return "deferred", "inconsistent_state"
    return "deferred", "inconsistent_state"


def process_notice(state, notice, pdf_bytes, text=None):
    """Apply one notice to the state machine. Returns a notification payload
    dict on a status transition, or None.

    ``text`` lets the caller hand over PDF text it has ALREADY extracted. A
    scanned result PDF costs a full OCR pass, and omitting ``text`` used to
    mean a SECOND full OCR pass inside this function, so a single notice cost
    two OCR passes inside a 3-minute step. Pass it whenever the caller has it.

    If the PDF text cannot be extracted (empty), the pass/fail determination
    is skipped entirely (treated as unknown) and None is returned — the caller
    must NOT mark the student as failed and must NOT advance the floor past
    this notice.

    If the text is NON-EMPTY but does not look like a readable results sheet
    (:func:`sheet_text_is_readable`), the determination is likewise unknown and
    None is returned. The old code only guarded the EMPTY case and fell through
    to ``status = "failed"`` for any non-empty text, so a PDF with a legacy
    custom-encoded font — real, non-empty, and unreadable — declared a student
    FAILED on a semester they had passed. Absence of a roll is evidence only
    when the sheet was actually read.
    """
    semester = match_semester(notice.get("title") or "")
    if semester is None:
        return None
    if not notice_matches_student(notice.get("title") or "", semester, state):
        return None
    sem = state["semesters"].get(str(semester))
    if sem is None:
        return None
    if sem["status"] == "passed":
        return None  # terminal

    if text is None:
        text = extract_pdf_text(pdf_bytes)
    if not text:
        # Couldn't extract -> unknown. Do NOT mark failed.
        return None

    roll = state["roll_number"]
    roll_found = roll_in_text(text, roll)
    if not roll_found and not sheet_text_is_readable(text):
        # Could not READ the sheet -> unknown. Do NOT mark failed either.
        return None
    result_date = _today_iso()
    notice_id = notice.get("id")
    pdf_url = notice.get("pdf_url")

    if roll_found:
        sem["status"] = "passed"
        sem["result_announced"] = True
        sem["roll_found"] = True
        sem["result_date"] = result_date
        sem["notice_id"] = notice_id
        sem["pdf_url"] = pdf_url
        passed = True
    else:
        sem["status"] = "failed"
        sem["result_announced"] = True
        sem["roll_found"] = False
        sem["result_date"] = result_date
        sem["notice_id"] = notice_id
        sem["pdf_url"] = pdf_url
        passed = False

    # Advance current_semester on ANY announcement of the current semester.
    if semester == state["current_semester"]:
        state["current_semester"] = min(8, state["current_semester"] + 1)

    return _build_payload(state, semester, notice, passed, result_date)


# ---------------------------------------------------------------------------
# Notification emission
# ---------------------------------------------------------------------------


def emit_notification(payload, dry_run):
    """dry_run: append payload to state/academic_notify_preview.json.
    live: print the payload (actual ntfy/website delivery is a separate
    workflow step).

    THE LIVE BRANCH PRINTS JSON-LINES: one COMPACT object per line, no
    indent. This is load-bearing, not cosmetic. The workflow tees stdout to
    state/academic_live_payload.jsonl, and scripts/payload_io.read_payloads
    parses that file one line at a time. With `indent=2` a payload spanned
    many lines, so N payloads fused into a file that was not one JSON
    document: the ntfy step's json.load raised "Extra data: line 33" on the
    four-payload run, its try/except turned that into "No notification payload
    available - ntfy skipped", ntfy/render/Pages all skipped, and the job
    exited 0. The semester-1 PASS was announced that way and NOTHING was
    delivered. `separators=(",", ":")` makes each payload exactly one line, so
    a payload can never be half-parsed and a count is trivially visible.
    The dry-run preview file stays a JSON LIST and is unchanged.
    """
    if dry_run:
        existing = []
        try:
            with open(NOTIFY_PREVIEW_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
        except (OSError, ValueError):
            existing = []
        existing.append(payload)
        save_state(NOTIFY_PREVIEW_PATH, existing)
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Feed passes
# ---------------------------------------------------------------------------


def _poll_set(state):
    """Semesters that still need checking: current + any failed."""
    sems = {state["current_semester"]}
    for key, sem in state["semesters"].items():
        if sem.get("status") == "failed":
            try:
                sems.add(int(key))
            except (TypeError, ValueError):
                pass
    return sems


def _semester_already_announced(state, semester):
    """True ONLY for a TERMINAL (passed) semester that was already announced.

    Do NOT widen this to ``failed``. A failed semester is not terminal:
    ``notice_matches_student`` rule (c) deliberately accepts back/re-exam
    notices for a failed semester precisely because that is the notice the
    owner most needs - the one that carries them over the line. A blanket
    "failed implies already announced" guard would swallow it.

    Duplicate suppression for a failed semester is handled where it belongs, by
    comparing the freshly computed verdict against the recorded one in
    ``_process_result_feed``: re-announcing the SAME verdict is suppressed, a
    genuinely changed verdict notifies. That is the per-NOTICE/per-semester
    distinction, and it uses the recorded ``roll_found`` rather than a blanket
    state test.
    """
    sem = (state.get("semesters") or {}).get(str(semester)) or {}
    return bool(sem.get("status") == "passed" and sem.get("result_announced"))


def _select_pending(list_ids, floor, decided, cap):
    """R2: unexamined ids in ascending order, capped. Returns
    (pending, overflow)."""
    pending = [i for i in list_ids if i > floor and i not in decided]
    if len(pending) > cap:
        return pending[:cap], True
    return pending, False


def _defer_streak(state, nid):
    """How many times in a row notice ``nid`` has been recorded ``deferred``.

    Consecutive, not cumulative: a notice that finally gets a decision resets
    to 0, so the count only grows while the id is genuinely stuck. This is what
    distinguishes a transient fetch blip (streak 1) from a permanently
    unfixable notice (PDF 404, login-gated, a scan no OCR can read) - and the
    second is what freezes the whole pass.
    """
    entry = (state.get("examined") or {}).get(str(nid))
    if not isinstance(entry, dict) or entry.get("outcome") != "deferred":
        return 0
    try:
        return int(entry.get("defer_streak") or 0)
    except (TypeError, ValueError):
        return 0


def _defer_and_maybe_block(state, nid, reason, when, threshold,
                           semester=None, extra=""):
    """Record ``nid`` as ``deferred`` and report whether the pass must STOP.

    This is the head-of-line guard. Every defer site in the result pass used to
    ``break``: a permanently unfixable notice (PDF 404, login-gated, a scan no
    OCR can read) then blocked the pass, so every id above it was never
    examined, every run, forever - a silent, permanent blindness above one bad
    notice. That is the same class of failure as the freeze this module exists
    to prevent, one layer down.

    The fix is NOT to weaken the floor. The id is still recorded ``deferred``,
    and a deferred id is not a decision, so :func:`advance`'s gap scan still
    refuses to move the floor past it - exactly as before. All that changes is
    that the pass stops *blocking* on it: after ``threshold`` consecutive
    defers we emit a loud unresolved-id alert and let the loop continue to the
    ids above, so a single unfixable notice costs visibility of nothing.

    Returns True when the caller should ``break`` (the floor stays put and R1
    retries next run), False when it should carry on past this id.
    """
    # Read the streak BEFORE recording: _record overwrites the entry wholesale,
    # so a post-record read would always see 0 and the guard would never fire.
    streak = _defer_streak(state, nid) + 1
    entry = _record(state, "examined", nid, "deferred", reason, when,
                    semester=semester)
    entry["defer_streak"] = streak
    if streak >= threshold:
        print("::error::academic: notice %d has deferred %d times in a row "
              "(last reason: %s%s) and is blocking the result pass. The floor "
              "is NOT advanced past it and it will be retried, but the pass is "
              "no longer stopping on it so the notices above are still "
              "examined. This notice is likely unreachable from here (404, "
              "login-gated, or an unreadable scan) and needs a human."
              % (nid, streak, reason, extra), file=sys.stderr)
        return False
    return True


def _process_result_feed(state, notices, fetch_fn, timeout, cap,
                         budget_s=RESULT_PASS_BUDGET_S, clock=None,
                         max_script_defers=ACADEMIC_MAX_SCRIPT_DEFERS,
                         max_consecutive_defers=ACADEMIC_MAX_CONSECUTIVE_DEFERS):
    """The R1-R5 result pass. Returns (payloads, down_hosts, overflow).

    ``budget_s`` is the R2b wall-clock budget in seconds; ``clock`` is the
    injectable monotonic clock (tests pass a fake, never sleep). A negative or
    None budget disables the deadline entirely; 0 means "no time at all", i.e.
    every notice is deferred.

    ``max_script_defers`` / ``max_consecutive_defers`` are the two loudness
    thresholds. Neither can turn a ``deferred`` into a decision: they only
    control when this run shouts and whether a stuck id stops blocking the ids
    above it. See :func:`_defer_and_maybe_block`.
    """
    when = _today_iso()
    poll = _poll_set(state)
    list_ids = [n["id"] for n in notices]
    by_id = {n["id"]: n for n in notices}
    floor = state.get("last_examined_notice_id")
    floor = 0 if floor is None else int(floor)
    decided = _decided_ids(state.get("examined"))
    pending, overflow = _select_pending(list_ids, floor, decided, cap)
    if overflow:
        print("academic: pending overflow — %d unexamined notices, processing "
              "the %d lowest (R2 cap)" % (len(
                  [i for i in list_ids if i > floor and i not in decided]),
                  cap), file=sys.stderr)

    if clock is None:
        clock = time.monotonic
    if budget_s is None or not isinstance(budget_s, (int, float)) \
            or budget_s < 0:
        deadline = None
    else:
        deadline = clock() + float(budget_s)

    payloads = []
    down_hosts = set()
    last_attempted = floor
    _script_unknown_hits = 0
    _marker_gap_hits = 0

    for pos, nid in enumerate(pending):
        # R2b: the budget is checked BEFORE a notice is opened, never part-way
        # through one, so the notice already in flight always runs to
        # completion - detail fetch, PDF download and the full OCR pass. Stop
        # here rather than be killed by the job's own timeout-minutes.
        if deadline is not None and clock() >= deadline:
            remainder = pending[pos:]
            for rid in remainder:
                _record(state, "examined", rid, "deferred",
                        "run_budget_exhausted", when)
            print("academic: result-pass budget of %gs exhausted; %d notice(s) "
                  "deferred for the next run (R2b)" % (budget_s, len(remainder)),
                  file=sys.stderr)
            # Deliberately NOT breaking out of the contract: `last_attempted`
            # is left where it is, so run()'s `min(advance(...), last_attempted)`
            # cannot carry the floor past the last notice actually opened. The
            # deferred entries are not decisions either, so `advance` blocks on
            # them as well: the floor stays put and R1 retries them next run.
            break
        notice = by_id[nid]
        last_attempted = max(last_attempted, nid)
        list_title = notice.get("title") or ""

        # --- NO cheap skip. Every pending notice gets its detail page. ------
        # There used to be a "provably irrelevant" pre-filter here that
        # skipped a notice when the LIST title carried a CSIT marker and no
        # English result term. It looked like an optimisation and it was a
        # burn: absence of a keyword is NOT proof of absence. A board index
        # truncates and ellipsises titles, and TU is under no obligation to use
        # the word "result" in one. Verified counter-example: list title
        # "B.Sc. CSIT Fourth Semester", detail title "...Result Published" -
        # the list title looks like a bare course heading, the pre-filter
        # recorded `irrelevant`, the floor advanced, and the semester's result
        # was never read. There is NO property of a list-page title that
        # positively establishes irrelevance, so nothing here may skip.
        # DO NOT reintroduce this branch. The cost is one detail fetch per
        # pending notice, which R2's cap (MAX_PENDING) already bounds.

        try:
            detail = fetch_notice_detail(notice["url"], fetch_fn=fetch_fn,
                                         timeout=timeout)
        except FetchError as exc:
            print("academic: notice %d detail failed: %s" % (nid, exc),
                  file=sys.stderr)
            host = _host_of(notice["url"]) or _host_of(DEFAULT_POLL_URL)
            if host:
                down_hosts.add(host)
            if _defer_and_maybe_block(state, nid, "detail_fetch_failed", when,
                                      max_consecutive_defers):
                break  # R3: stop at N, leave it unexamined for the next run
            continue
        host = _host_of(notice["url"])
        if host:
            down_hosts.discard(host)

        merged = dict(notice)
        merged["pdf_url"] = detail.get("pdf_url")
        detail_title = detail.get("title") or ""
        # R1, restated after the site-constant fix: the authoritative title is
        # the thing a positive decision needs. BOTH failure modes defer.
        #   * no title at all            -> we could not read the page;
        #   * a degraded (site-constant) -> we read a title-shaped string that
        #     is not a notice title.
        # Neither is evidence about the NOTICE, and both used to be recorded as
        # `irrelevant`, which under R4 is a permanent decision: the floor
        # advanced past the notice and no run would ever read it again. That is
        # exactly how the sem-1 2082 result was burned. Deferring is the safe
        # direction: R1 retries it, and the floor does not move.
        if not detail_title or detail.get("title_degraded"):
            print("academic: notice %d has no authoritative detail title "
                  "(source=%r, degraded=%r); deferring"
                  % (nid, detail.get("title_source"),
                     bool(detail.get("title_degraded"))), file=sys.stderr)
            if _defer_and_maybe_block(state, nid, "title_unparseable", when,
                                      max_consecutive_defers,
                                      extra=" (unreadable detail title)"):
                break
            continue
        merged["title"] = detail_title

        # --- Positive decisions on the AUTHORITATIVE title ------------------
        # classify_semester is the rich entry point; match_semester (int|None)
        # is its Latin-only projection and the function the Cloudflare Worker
        # mirrors. The invariant between them holds by construction.
        verdict = classify_semester(detail_title)
        if verdict["status"] == "unknown":
            # We could not READ the title: nothing in either script's
            # vocabulary was found. That is evidence about US - our token set
            # does not cover this board's wording - and NOT about the notice.
            # 5 of the 14 notices on the live result feed are in Nepali, and
            # recording one of those as `irrelevant` burned notice 15696
            # permanently. Defer (R1 retries, the floor does not move) and make
            # a run of them LOUD so a token-set gap can never be a quiet freeze.
            print("academic: notice %d title is unreadable in any recognised "
                  "script (script=%s, stage_hits=%s); deferring - this is a "
                  "token-set gap, not a decision about the notice"
                  % (nid, verdict["script"], verdict["stage_hits"]),
                  file=sys.stderr)
            _script_unknown_hits += 1
            unknown_streak = _defer_streak(state, nid) + 1
            # Two distinct gaps reach here and BOTH defer; they must not be
            # reported as one. no_csit_marker means a semester token AND a
            # result term were both positively recognised and the CSIT marker
            # was not, so this could be the owner's result in a spelling the
            # marker table lacks. It is recorded under its own reason so the
            # table a maintainer must extend is named in the state file, and
            # so the marker gap gets its own loud counter below.
            marker_gap = verdict["reason"] == "no_csit_marker"
            defer_reason = "no_csit_marker" if marker_gap \
                else "title_script_unknown"
            if marker_gap:
                _marker_gap_hits += 1
                if _marker_gap_hits >= max_script_defers \
                        or unknown_streak >= max_script_defers:
                    print("::error::academic: notice %d carries a semester "
                          "token and a result term but NO CSIT marker "
                          "(stage_hits=%s, reason=no_csit_marker) and has "
                          "deferred %d times - the CSIT marker vocabulary "
                          "does not cover this notice's spelling. It is "
                          "DEFERRED, never dismissed: the floor has not "
                          "moved past it and it is retried every run."
                          % (nid, verdict["stage_hits"], unknown_streak),
                          file=sys.stderr)
            blocked = _defer_and_maybe_block(state, nid,
                                             defer_reason, when,
                                             max_consecutive_defers,
                                             extra=(" (no CSIT marker)"
                                                    if marker_gap else
                                                    " (unreadable title script)"))
            # The token-gap alert fires on EITHER signal, and the second one
            # matters more than it looks. The caller contract `break`s on an
            # unreadable title, so within a single run the first unknown stops
            # the pass and `_script_unknown_hits` can only ever reach 1 until
            # the per-id head-of-line guard stops blocking. Left alone, the
            # per-run counter would therefore arm only on the fourth or later
            # run, and every run before that was SILENT about a token set that
            # does not cover this board. A single id whose title has been
            # unreadable on N consecutive runs is the same evidence, and it is
            # available immediately.
            if _script_unknown_hits >= max_script_defers \
                    or unknown_streak >= max_script_defers:
                print("::error::academic: %d notices deferred as "
                      "title_script_unknown in this run (notice %d has now "
                      "gone %d runs unreadable) - the Devanagari token set "
                      "does not cover this board's vocabulary. This is a "
                      "TOKEN-SET GAP, not a quiet board."
                      % (_script_unknown_hits, nid, unknown_streak),
                      file=sys.stderr)
            if blocked:
                break
            continue
        if verdict["status"] == "no_match":
            _record(state, "examined", nid, "irrelevant", "no_semester_match",
                    when)
            continue
        semester = verdict["semester"]
        # status == "match" -> the existing chain below is UNCHANGED.
        # Replay guard for a TERMINAL semester, checked first: a passed+announced
        # semester never re-notifies, whatever the batch year. This closes the
        # hole the migration's replay window opens.
        if _semester_already_announced(state, semester):
            _record(state, "examined", nid, "already_announced",
                    "already_announced", when, semester=semester)
            continue
        if semester not in poll:
            _record(state, "examined", nid, "irrelevant", "wrong_poll_set",
                    when, semester=semester)
            continue
        if not notice_matches_student(detail_title, semester, state):
            _record(state, "examined", nid, "irrelevant", "batch_mismatch",
                    when, semester=semester)
            continue

        # Make the missing-key case IMPOSSIBLE rather than deferring on it. The
        # reviewer's repro: a semesters map with no "4" entry, a real sem-4
        # result whose PDF was downloaded and whose roll WAS found, produced
        # zero payloads while the run recorded `processed` and advanced the
        # floor. A result was sitting in a downloaded PDF and the owner was
        # told nothing, forever. Creating the entry is the honest repair: the
        # verdict becomes recordable, the payload is emitted, and the state
        # stops contradicting itself. It is logged loudly because a missing key
        # means the state was hand-edited or lost a semester.
        sem_key = str(semester)
        created_key = not isinstance(state["semesters"].get(sem_key), dict)
        if created_key:
            print("::warning::academic state has no semesters[%s] entry; "
                  "creating a pending one so this result can be recorded and "
                  "reported (notice %d)" % (sem_key, nid), file=sys.stderr)
            state["semesters"][sem_key] = {
                "status": "pending", "result_announced": False,
                "roll_found": False, "result_date": None,
                "notice_id": None, "pdf_url": None,
            }

        pdf_url = merged.get("pdf_url")
        if not pdf_url:
            print("academic: notice %d has no PDF link; deferring" % nid,
                  file=sys.stderr)
            if _defer_and_maybe_block(state, nid, "pdf_missing", when,
                                      max_consecutive_defers,
                                      semester=semester,
                                      extra=" (no PDF link)"):
                break  # cannot determine pass/fail without the PDF
            continue
        try:
            # Its OWN timeout, not the notice-list one. The real sem-4 sheet is
            # ~10.5 MB; downloading it with the CLI's --timeout 15 raised
            # FetchError and deferred, run after run, and a deferred result is
            # a result the owner never hears about.
            pdf_bytes = download_pdf(pdf_url, fetch_fn=fetch_fn,
                                     timeout=PDF_FETCH_TIMEOUT_S)
        except FetchError as exc:
            print("academic: notice %d pdf download failed: %s" % (nid, exc),
                  file=sys.stderr)
            host = _host_of(pdf_url)
            if host:
                down_hosts.add(host)
            # A download failure is a transport failure on the same resource
            # class as a missing link: pdf_missing is the closest frozen
            # reason token, and it defers (R1) rather than burning.
            if _defer_and_maybe_block(state, nid, "pdf_missing", when,
                                      max_consecutive_defers,
                                      semester=semester,
                                      extra=" (pdf download failed)"):
                break  # do not advance past it; retry next tick
            continue

        # CRITICAL: an unextractable PDF means UNKNOWN. It must NEVER mean
        # FAILED, and it must not advance the floor past this notice.
        text = extract_pdf_text(pdf_bytes)
        if not text:
            print("academic: notice %d pdf text extraction failed; "
                  "deferring (will retry)" % nid, file=sys.stderr)
            cap_note = _ocr_capability_note()
            if cap_note:
                print("academic: OCR capability is incomplete - %s; a scanned "
                      "sheet cannot be read without all three, and this notice "
                      "will defer on every run until they are present"
                      % cap_note, file=sys.stderr)
            if _defer_and_maybe_block(state, nid, "pdf_unextractable", when,
                                      max_consecutive_defers,
                                      semester=semester,
                                      extra=" (unextractable PDF)"):
                break
            continue

        # THE FALSE-FAILED GUARD. Empty text is handled above; this is the
        # harder case: text that is non-empty but is not a readable result
        # sheet - a legacy custom-encoded font, a font pypdf cannot map, an
        # OCR pass that returned prose. "The roll is not in this text" is NOT
        # evidence of absence when the sheet was never readable, so a FAILED
        # verdict must not be recorded. Defer (R1 retries, floor does not move)
        # rather than announce a failure the owner may not have had.
        if not roll_in_text(text, state["roll_number"]) \
                and not sheet_text_is_readable(text):
            print("academic: notice %d pdf text is non-empty but does not look "
                  "like a readable result sheet; deferring instead of "
                  "announcing a possible FAILED" % nid, file=sys.stderr)
            _record(state, "examined", nid, "deferred", "pdf_text_unreadable",
                    when, semester=semester)
            if _defer_and_maybe_block(state, nid, "pdf_text_unreadable", when,
                                      max_consecutive_defers,
                                      semester=semester,
                                      extra=" (sheet not readable)"):
                break
            continue

        # P0 hardening, negative half. Creating the missing key makes the
        # verdict RECORDABLE, which is the whole point of the repair - but when
        # the roll is ABSENT from that PDF it would make a FAILED verdict out
        # of a state entry the run invented one line earlier. That is a claim
        # we cannot support: the state does not know this semester was ever
        # examined, and `pending`/roll_found=False is indistinguishable from a
        # semester nobody has looked at. So the just-created case DEFERS with
        # the existing ``inconsistent_state`` reason and stops (R3) - the key
        # it wrote persists, the notice is retried next run, and by then the
        # entry is real state and the verdict is legitimate. The roll-PRESENT
        # case is untouched and must stay untouched: that is the path that
        # fixed the original P0, and a result sitting in a downloaded PDF is
        # never withheld.
        if created_key and not roll_in_text(text, state["roll_number"]):
            print("::warning::semesters[%s] was just created and the roll is "
                  "absent from notice %d; deferring rather than inventing a "
                  "FAILED verdict from an entry this run made up"
                  % (sem_key, nid), file=sys.stderr)
            _record(state, "examined", nid, "deferred", "inconsistent_state",
                    when, semester=semester)
            if _defer_and_maybe_block(state, nid, "inconsistent_state", when,
                                      max_consecutive_defers,
                                      semester=semester,
                                      extra=" (semester entry just created)"):
                break
            continue

        # Snapshot the recorded verdict BEFORE processing so a re-announcement
        # of the SAME verdict can be suppressed exactly (see below).
        sem_before = dict(state["semesters"].get(str(semester)) or {})

        # P2a: hand over the text we already extracted. A scanned result PDF
        # costs a full OCR pass; extracting twice doubled the OCR work per
        # notice inside a 3-minute step.
        payload = process_notice(state, merged, pdf_bytes, text=text)

        if payload is None:
            # P0. A None payload is NEVER `processed`. Recording it as
            # processed/result_announced used to advance the floor and retire
            # the notice while NOTHING had been announced - a silent,
            # permanent result burn. Classify it honestly instead.
            outcome, reason = _no_payload_reason(state, merged, semester)
            print("academic: notice %d produced no payload (%s); recording "
                  "%s/%s" % (nid, reason, outcome, reason), file=sys.stderr)
            _record(state, "examined", nid, outcome, reason, when,
                    semester=semester)
            if outcome == "deferred":
                # R3: stop here so nothing above it is decided on a state we
                # could not reason about.
                break
            continue

        # P1d: suppress a payload that does not CHANGE the recorded verdict.
        # A failed+announced semester whose roll is still absent is not news -
        # re-reading the same failure produced a duplicate "FAILED" ntfy. A
        # genuine back/re-exam result DOES change roll_found, so it still
        # notifies, which is why the guard cannot simply be widened to
        # "failed": notice_matches_student rule (c) deliberately accepts
        # back/re-exam notices for failed semesters, and a blanket skip would
        # swallow the one alert the owner most needs.
        sem_after = state["semesters"].get(str(semester)) or {}
        verdict_changed = (sem_before.get("roll_found")
                           != sem_after.get("roll_found")
                           or sem_before.get("status") != sem_after.get("status"))
        if not verdict_changed:
            payload = None

        if payload is not None:
            payloads.append(payload)
            _record(state, "examined", nid, "processed", "result_announced",
                    when, semester=semester)
        else:
            _record(state, "examined", nid, "already_announced",
                    "already_announced", when, semester=semester)

    return payloads, down_hosts, overflow, last_attempted


def _is_result_notice(title):
    """True when the RESULT matcher claims this title.

    Used only to keep the two delivery paths mutually exclusive. Deliberately
    WIDER than match_semester: it fires on the stage-1 test alone (marker +
    semester token + result term) so a result notice is never ALSO alerted as a
    generic exam notice, even when the semester token cannot be resolved.
    """
    return bool(title) and bool(SEMESTER_RE.search(title))


def _exam_defer_streak(mapping, nid):
    """Consecutive ``deferred`` runs recorded for ``nid`` in an exam-feed map.

    The exam-notice lane keeps its own ``exam_notice_seen:<feed>`` buckets, so
    it cannot use :func:`_defer_streak` (which reads the result pass's
    ``examined`` map) or :func:`_defer_and_maybe_block` (which writes there).
    Same semantics: a decided id resets the count, so the streak only grows
    while the id is genuinely stuck.
    """
    entry = (mapping or {}).get(str(nid))
    if not isinstance(entry, dict) or entry.get("outcome") != "deferred":
        return 0
    try:
        return int(entry.get("defer_streak") or 0)
    except (TypeError, ValueError):
        return 0


def _process_exam_notice_feed(state, feed, fetch_fn, timeout, cap,
                              max_consecutive_defers=(
                                  ACADEMIC_MAX_CONSECUTIVE_DEFERS)):
    """F4 exam-notice pass (routines, forms, admission cards, deadlines).

    Deliberate asymmetry vs the result pass: the result pass reads the
    AUTHORITATIVE title for every notice because it makes a pass/fail verdict.
    This pass only forwards a link, so the board index is authoritative enough,
    and matching there gives complete coverage of routines and forms.

    MUTUAL EXCLUSION (P2b): the iost_all feed overlaps the result feed, and
    "result" is in the config's ``notice_terms``, so a result notice that also
    carries a date-shaped token would emit BOTH an exam_deadline alert and an
    academic_result alert - two ntfy pushes for one notice. Any notice the
    RESULT matcher claims is therefore skipped here, and the result pass owns
    it. A result verdict must come from exactly one code path.

    HEAD-OF-LINE GUARD (added 2026-09-30). This lane used to ``break`` on the
    first unreadable list title and NOTHING else: no streak counter, no escape.
    One malformed anchor therefore silenced every exam routine, form and
    deadline notice above it, forever, silently, at exactly the moment of exam
    season - proven over 6 consecutive runs with the floor pinned at 0 and
    zero output and exit 0. It now carries the same per-id streak escape the
    result pass has (:func:`_defer_and_maybe_block`, and here
    :func:`_exam_defer_streak` because the two lanes keep separate buckets):
    after ``max_consecutive_defers`` consecutive deferrals the pass shouts and
    carries on to the ids above, which are then examined and recorded normally.

    The floor is NOT weakened by that: the id stays ``deferred``, a deferred id
    is not a decision, and :func:`advance`'s gap scan still refuses to move the
    floor past it. One unfixable notice costs visibility of nothing.

    PROGRAMME RELEVANCE (added 2026-10-03). Before a notice is examined for
    exam terms, :func:`is_other_programme_notice` rejects the ones that are
    EXPLICITLY another programme's, recording them ``irrelevant`` /
    ``other_program``. This is a deliberate ASYMMETRY with the result pass,
    which makes a pass/fail verdict and therefore decides relevance from the
    authoritative detail title (R1) rather than from the board index. An
    exam-notice payload is only a pointer to a notice, and a pointer to another
    programme's notice is never the owner's business - while a pointer that is
    wrongly suppressed is a real routine or form he never hears about. The
    filter rejects on POSITIVE evidence of another programme and on nothing
    else; see :func:`is_other_programme_notice` for why the no-marker case must
    keep.
    """
    when = _today_iso()
    matchers = state.get("_exam_matchers") or {}
    require_marker = bool(feed.get("require_marker"))
    try:
        notices = fetch_notice_list(feed.get("url"), fetch_fn=fetch_fn,
                                    timeout=timeout)
    except FetchError as exc:
        print("academic: exam feed %s failed: %s" % (feed.get("id"), exc),
              file=sys.stderr)
        return [], _host_of(feed.get("url")), False

    bucket = "exam_notice_seen:%s" % feed.get("id")
    mapping = state.setdefault(bucket, {})
    seen_ids = _decided_ids(mapping)
    list_ids = [n["id"] for n in notices]
    floor = mapping.get("__floor__")
    try:
        floor = int(floor)
    except (TypeError, ValueError):
        floor = 0
    pending, overflow = _select_pending(list_ids, floor, seen_ids, cap)
    payloads = []
    for nid in pending:
        notice = next(n for n in notices if n["id"] == nid)
        title = notice.get("title") or ""
        if notice.get("title_degraded") or not title:
            streak = _exam_defer_streak(mapping, nid) + 1
            mapping[str(nid)] = {"outcome": "deferred",
                                 "semester": None,
                                 "reason": "title_unparseable", "at": when,
                                 "defer_streak": streak}
            if streak >= max_consecutive_defers:
                print("::error::academic: exam-notice %d has deferred %d "
                      "times in a row (unreadable list title) and was "
                      "blocking the exam-notice pass. The floor is NOT "
                      "advanced past it and it is retried, but the pass is no "
                      "longer stopping on it, so the routines, forms and "
                      "deadlines above it are still examined. It is likely "
                      "unreachable from here and needs a human."
                      % (nid, streak), file=sys.stderr)
                continue
            break
        # PROGRAMME RELEVANCE (added 2026-10-03). This is the point where a
        # notice BECOMES USER-VISIBLE: everything below this line becomes an
        # ntfy push and a page under docs/academic/, and both are labelled
        # "CSIT exam notice" by _build_exam_notice_payload / render_academic
        # unconditionally. The iost_all feed is the whole IOST board, so an
        # M.Sc./B.Tech/Ph.D notice reached the owner wearing that label.
        #
        # WHY HERE AND NOT AT COLLECTION. The fetch, the parse and this id's
        # record in `exam_notice_seen:<feed>` all still happen: the notice is
        # examined, decided, and remembered WITH A REASON, so state/ remains a
        # truthful audit trail of what was on the board and why it was not
        # delivered. Filtering at fetch time would instead delete a notice from
        # the record without ever deciding it, which is indistinguishable from
        # never having seen it - and a review has already wrongly flagged this
        # repo's genuine audit trail as duplicate cruft. Deciding here is also
        # what makes the notice PERMANENTLY irrelevant (R4): another programme's
        # form-fill notice will never become the owner's.
        #
        # Ordering matters and is deliberate: this runs BEFORE
        # match_exam_notice, so an M.Sc notice is recorded with the honest
        # reason `other_program` rather than the uninformative
        # `no_semester_match`, and before _is_result_notice, which could not
        # have claimed it anyway (SEMESTER_RE requires the CSIT marker).
        if is_other_programme_notice(title):
            mapping[str(nid)] = {"outcome": "irrelevant", "semester": None,
                                 "reason": "other_program", "at": when}
            continue
        if match_exam_notice(title, matchers, require_marker):
            # P2b mutual exclusion: a notice the RESULT matcher claims belongs
            # to the result pass. It gets a verdict there, not a pointer here.
            if _is_result_notice(title):
                mapping[str(nid)] = {"outcome": "irrelevant", "semester": None,
                                     "reason": "wrong_poll_set", "at": when}
                continue
            payloads.append(_build_exam_notice_payload(
                state, exam_notice_semester(title), notice,
                notice.get("date")))
            mapping[str(nid)] = {"outcome": "processed", "semester": None,
                                 "reason": "deadline_notified", "at": when}
        else:
            mapping[str(nid)] = {"outcome": "irrelevant", "semester": None,
                                 "reason": "no_semester_match", "at": when}
    mapping["__floor__"] = advance(floor, mapping, list_ids)
    _evict_examined(state, bucket)
    return payloads, "", overflow


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------


def run(state_file, poll_url, dry_run, fetch_fn=None, max_notices=MAX_PENDING,
        timeout=15, notices_config=None, run_exam_notices=True,
        budget_s=RESULT_PASS_BUDGET_S, clock=None,
        max_script_defers=ACADEMIC_MAX_SCRIPT_DEFERS,
        max_consecutive_defers=ACADEMIC_MAX_CONSECUTIVE_DEFERS,
        result_union_urls=DEFAULT_RESULT_FEED_UNION_URLS):
    """Main entry. Returns an exit code (0/1/2/3).

    ``budget_s``/``clock`` are the injectable R2b wall-clock budget and its
    monotonic clock; the CLI never passes them, so production always uses
    RESULT_PASS_BUDGET_S against time.monotonic.

    ``max_script_defers``/``max_consecutive_defers`` are the two loudness
    thresholds, both overridable from the CLI. Neither can turn a ``deferred``
    into a decision.

    ``result_union_urls`` are whole-board list URLs unioned into the result
    feed. Pass ``()`` to switch the union off (tests do, so they never touch a
    second URL).
    """
    fetch_fn = fetch_fn or default_fetch_fn

    # R2 bound. max_notices<=0 falls back to MAX_PENDING; an explicit smaller
    # value is honoured. It is NOT a list truncation any more.
    cap = MAX_PENDING
    if isinstance(max_notices, int) and 0 < max_notices < MAX_PENDING:
        cap = max_notices

    # --- Load state (exit 3 invalid, exit 4 absent) -----------------------
    meta = {}
    try:
        state = load_state(state_file, meta=meta)
    except StateNotFound as exc:
        print("academic: %s" % exc, file=sys.stderr)
        return 4
    except StateError as exc:
        print("academic: %s" % exc, file=sys.stderr)
        return 3
    if meta.get("migrated"):
        print("academic: replay window opened (%d ids)" % REPLAY_WINDOW)
    if meta.get("repaired"):
        # Never a silent state mutation: say that a verdict field was completed.
        print("::warning::academic state was missing a roll_found on an "
              "announced semester; completed it from status in memory (see "
              "_normalise_semesters)", file=sys.stderr)

    # --- Fetch notice list (exit 2 on transient failure) ------------------
    # NOTE: this returns BEFORE save_state on purpose. A total outage must not
    # rewrite the state file. The dead-man's switch for that case is the
    # stale last_poll_time plus the degraded-run artifact written by the
    # workflow, not a state mutation.
    try:
        notices = fetch_notice_list(poll_url, fetch_fn=fetch_fn, timeout=timeout)
    except FetchError as exc:
        print("academic: transient fetch failure: %s" % exc, file=sys.stderr)
        return 2

    # (P1) UNION THE WHOLE-BOARD FEED INTO THE RESULT FEED. Verified live:
    # notice 15642 is listed on /notices but NOT on notice_type=21, so a result
    # filed under a different notice_type was invisible to the result pass
    # entirely. Additive only - unioning more notices means more are examined,
    # and each one is still decided on its own authoritative detail page. A
    # failure here is a WARNING, never exit 2: the primary result feed parsed
    # fine, and refusing its notices because a secondary list was down would
    # turn a partial blind spot into a total one.
    for extra_url in tuple(result_union_urls or ()):
        if not extra_url or extra_url == poll_url:
            continue
        try:
            extra = fetch_notice_list(extra_url, fetch_fn=fetch_fn,
                                      timeout=timeout)
        except FetchError as exc:
            print("::warning::academic: whole-board feed %s not unioned into "
                  "the result feed (%s); a result filed under a "
                  "notice_type != 21 may be missed this run"
                  % (extra_url, exc), file=sys.stderr)
            continue
        known = {n["id"] for n in notices}
        added = [n for n in extra if n["id"] not in known]
        if added:
            print("academic: unioned %d notice(s) from the whole-board feed "
                  "(%s) into the result feed" % (len(added), extra_url),
                  file=sys.stderr)
        notices.extend(added)

    notices = [n for n in notices if isinstance(n, dict) and n.get("id")]
    notices.sort(key=lambda n: n["id"])

    payloads, down_hosts, overflow, last_attempted = _process_result_feed(
        state, notices, fetch_fn, timeout, cap, budget_s=budget_s, clock=clock,
        max_script_defers=max_script_defers,
        max_consecutive_defers=max_consecutive_defers)

    # R5: the floor is computed from the FULL parsed list...
    floor = state.get("last_examined_notice_id")
    new_floor = advance(floor, state.get("examined"), [n["id"] for n in notices])
    # ...and R2 caps it: advance nothing past the last id we actually attempted.
    # R5 alone would let the floor reach the FIRST unprocessed id when the gap
    # below it is clean; R2's "advance nothing past the last processed id" is
    # the tighter promise, so the lower of the two wins.
    state["last_examined_notice_id"] = min(new_floor, max(
        0 if floor is None else int(floor), last_attempted))

    # --- hosts_down bookkeeping ------------------------------------------
    hosts_down = state.setdefault("hosts_down", {})
    stamp = _now_utc_iso()
    for host in down_hosts:
        if host:
            hosts_down.setdefault(host, stamp)
    for host in list(hosts_down):
        if host not in down_hosts:
            hosts_down.pop(host, None)

    # --- Exam-notice coverage --------------------------------------------
    if run_exam_notices:
        cfg = notices_config if notices_config is not None \
            else load_exam_notice_config()
        if cfg.get("enabled") and isinstance(cfg.get("feeds"), list):
            state["_exam_matchers"] = cfg.get("matchers") or {}
            for feed in cfg["feeds"]:
                if not isinstance(feed, dict):
                    continue
                if feed.get("kind") != "exam_notice":
                    continue  # the result feed is covered by the pass above
                if not feed.get("url"):
                    continue
                exam_payloads, host, exam_overflow = _process_exam_notice_feed(
                    state, feed, fetch_fn, timeout, cap,
                    max_consecutive_defers)
                payloads.extend(exam_payloads)
                overflow = overflow or exam_overflow
                if host and host in hosts_down:
                    hosts_down[host] = hosts_down.get(host, stamp)
            state.pop("_exam_matchers", None)

    # --- Persist state ----------------------------------------------------
    state["pending_overflow"] = bool(overflow)
    state["last_poll_time"] = _today_iso()
    try:
        save_state(state_file, state)
    except OSError as exc:
        print("academic: cannot save state: %s" % exc, file=sys.stderr)
        return 1

    # --- Emit notifications ----------------------------------------------
    for payload in payloads:
        emit_notification(payload, dry_run)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="TU B.Sc.CSIT academic result monitor")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-url", default=DEFAULT_POLL_URL)
    parser.add_argument("--max-notices", type=int, default=MAX_PENDING)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--no-exam-notices", action="store_true",
                        help="skip the exam-notice (routine/form/deadline) pass")
    parser.add_argument("--max-script-defers", type=int,
                        default=ACADEMIC_MAX_SCRIPT_DEFERS,
                        help="per-run count of unreadable-title defers that "
                             "triggers a ::error:: token-set-gap alert")
    parser.add_argument("--max-consecutive-defers", type=int,
                        default=ACADEMIC_MAX_CONSECUTIVE_DEFERS,
                        help="how many times one notice may defer in a row "
                             "before it stops blocking the result pass")
    args = parser.parse_args(argv)
    return run(args.state_file, args.poll_url, args.dry_run,
               max_notices=args.max_notices, timeout=args.timeout,
               run_exam_notices=not args.no_exam_notices,
               max_script_defers=args.max_script_defers,
               max_consecutive_defers=args.max_consecutive_defers)


if __name__ == "__main__":
    sys.exit(main())
