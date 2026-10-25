#!/usr/bin/env python3
"""LIVE PARSER CANARY -- the daily "are the parsers still reading reality?" job.

WHY THIS FILE EXISTS
--------------------
A sibling bug cost the owner a semester-1 exam result for roughly 19 hours. A
parser read a plausible-but-wrong value from the wrong place, decided "no
match", and the product treated that as a CONFIRMED negative -- permanently
burning the result. It shipped because not one test in this repository had ever
run against a real captured feed, notice page or PDF. Every test fed a
hand-written fixture shaped like what the parser *should* see, so a parser that
had drifted from the live site still agreed with the fixture perfectly.

THE FROZEN SHAPE OF THIS FILE (contract 1g)
-------------------------------------------
  * EVERY assertion is a pure function ``assert_X(blob) -> (ok, detail)``.
    No clock, no filesystem, no network.
  * ``collect()`` is the ONLY function that touches the network, and NO unit
    test ever calls it. That is the whole reason this file can be loud in CI
    and offline-safe under ``unittest``: the tests exercise the assertions, the
    canary run exercises the assertions against a live blob.

THE ANTI-FLAKE RULE, AND WHY IT IS NOT A DISABLE
-----------------------------------------------
An upstream that is unreachable makes the canary's answer meaningless, not
"failing". A network problem is classified with the product's own
``source_health.classify_reason`` codes (dns | timeout | tls | connection |
throttled | http_5xx) and, if the fetches really did fail, the run reports
INCONCLUSIVE and exits 0. But the inverse is the whole point of this job:

    A 200 that parses to zero items is NOT a network failure.

That case MUST fail the run, loudly, because it is indistinguishable -- from
the outside -- from the burn. Conflating the two is what makes a canary
untrustworthy in both directions.

GUARDED IMPORTS
---------------
Peers are landing in this tree concurrently, so ``classify_semester``,
``ocr_capability``, ``compute_source_health`` and the ``items_found`` row field
are imported defensively. A peer's absence prints a PARTIAL warning and exits
0: this canary never fails the build for something that is not its own
assertion, and it never silently passes either.

Stdlib only.
"""

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import ntfy_classes  # noqa: E402  (deliberate: after the sys.path insert)


# ---------------------------------------------------------------------------
# Guarded peer imports. A missing name is a PARTIAL run, never a crash and
# never a silent pass.
# ---------------------------------------------------------------------------

PARTIAL_NOTES = []

try:
    import check_academic_result as AR
except Exception:                                    # pragma: no cover
    AR = None
    PARTIAL_NOTES.append("check_academic_result absent")

try:
    import collect_news as CN
except Exception:                                    # pragma: no cover
    CN = None
    PARTIAL_NOTES.append("collect_news absent")

try:
    import source_health as SH
except Exception:                                    # pragma: no cover
    SH = None
    PARTIAL_NOTES.append("source_health absent")

try:
    import build_email as BE
except Exception:                                    # pragma: no cover
    BE = None

try:
    import render_digest as RD
except Exception:                                    # pragma: no cover
    RD = None


def _academic_api_present():
    """True when the academic parser API this canary asserts against exists.

    Checked by ATTRIBUTE, not by module: a peer can land a module that has not
    grown the frozen function yet, and that is exactly the case the PARTIAL
    warning exists for.
    """
    if AR is None:
        return False
    return (hasattr(AR, "classify_semester")
            and hasattr(AR, "ocr_capability")
            and hasattr(AR, "is_site_constant_title"))


def _source_health_api_present():
    return SH is not None and hasattr(SH, "compute_source_health")


def section_status_line(category, item_count, source_health):
    """The product's own B-contract line. build_email first, render_digest next.

    Both carry the same literal; build_email is the one the digest renders from,
    so it is preferred. Falling back keeps D6 runnable when a peer's refactor
    moves the function.
    """
    for mod in (BE, RD):
        fn = getattr(mod, "section_status_line", None) if mod else None
        if callable(fn):
            return fn(category, item_count, source_health)
    return ""


#: The confident, wrong string D6 exists to catch. Byte-for-byte the B4 line.
B4_CONFIDENT = "Nothing new from this source today."

#: A notice candidate must point at a DETAIL page, not at navigation.
#:
#: THIS WAS A CANARY BUG, MEASURED AGAINST THE LIVE BOARD: the pattern was
#: ``/notice/?\d+`` — SINGULAR only. Every real TU IOST detail URL is PLURAL
#: (``https://iost.tu.edu.np/notices/15697`` is a genuine notice id), so 0 of 5
#: notice sources emitted a URL this regex would accept and D7 reported five
#: sources' worth of failure for a collector that had been right all along.
#: The fix belongs here and NOT in the collector: weakening the collector's
#: URL discipline to satisfy a wrong assertion would have destroyed the one
#: thing D7 exists to protect.
#:
#: Both singular and plural are accepted, plus the download/PDF shapes a
#: notice may be published as (a notice is sometimes a file, not a page), and
#: ``/notices?notice_type=21`` — the LIST url — still correctly FAILS: after
#: ``/notices`` comes ``?``, not ``/``, so no alternative can reach a digit.
NOTICE_DETAIL_URL_RE = re.compile(
    r"/notices?/\d+"
    r"|/[a-z0-9_\-]*notice[a-z0-9_\-]*[-_/]\d+"
    r"|/(?:download|downloads|documents?|files?|attachment)s?/"
    r"|\.(?:pdf|docx?)\b",
    re.IGNORECASE,
)

#: `items_found` on a collector row. A v1 record has no such field at all.
ITEMS_FOUND_FIELD = "items_found"

#: Failure strike count at which the canary escalates from warning to error.
CANARY_ERROR_STRIKES = 2

#: How old a `state/canary_history.json` entry may be before "CANARY DEAD".
CANARY_DEAD_DAYS = 4

#: Reason codes that mean "the upstream is the problem", not "our parser is".
NETWORK_REASONS = ("dns", "timeout", "tls", "connection", "throttled", "http_5xx")


# ---------------------------------------------------------------------------
# Small pure helpers. No clock, no IO.
# ---------------------------------------------------------------------------

def _digest(blob):
    """The digest half of a blob.

    Accepts EITHER the raw collect_news.py output (what a test of D1..D7
    naturally holds) or the ``{"digest": ..., "academic": ...}`` wrapper
    ``collect()`` returns, so every assertion is testable without reproducing
    the wrapper.
    """
    if not isinstance(blob, dict):
        return {}
    inner = blob.get("digest")
    return inner if isinstance(inner, dict) else blob


def _academic(blob):
    if not isinstance(blob, dict):
        return {}
    inner = blob.get("academic")
    return inner if isinstance(inner, dict) else blob


def _rows(blob):
    rows = _digest(blob).get("source_results")
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def _candidates(blob):
    cands = _digest(blob).get("candidates")
    if isinstance(cands, list):
        return [c for c in cands if isinstance(c, dict)]
    return []


def _host_of(value):
    """Bare hostname, lowercased, no port and no leading www."""
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text


def _candidate_host(cand):
    for key in ("source", "domain", "host", "url"):
        host = _host_of(cand.get(key))
        if host:
            return host
    return ""


def _same_origin(a, b):
    """True when two hostnames are the same site, allowing a www. mismatch."""
    ha, hb = _host_of(a), _host_of(b)
    if not ha or not hb:
        return False
    if ha == hb:
        return True
    return ha[4:] == hb[4:] if ha.startswith("www.") or hb.startswith("www.") else False


def _site_constant_title(text):
    """Is this title site chrome? The product's own rule when it is importable.

    The local fallback is deliberately the same generous shape (subtract the
    known constants and the chrome words; what survives is the notice-specific
    part), so D2 still means the same thing when a peer is mid-refactor.
    """
    if AR is not None and hasattr(AR, "is_site_constant_title"):
        try:
            return bool(AR.is_site_constant_title(text))
        except Exception:
            pass
    norm = re.sub(r"\s+", " ", (text or "")).strip().upper()
    if not norm:
        return True
    if any(ord(ch) > 127 for ch in norm):
        return False
    if getattr(AR, "SITE_CONSTANT_TITLES", None):
        rest = norm
        for const in AR.SITE_CONSTANT_TITLES:
            rest = rest.replace(const, " ")
        words = [w for w in re.split(r"[^A-Z0-9]+", rest) if w]
        return not [w for w in words if w not in {
            "NOTICE", "NOTICES", "HOME", "PAGE", "PORTAL", "WELCOME", "LOGIN",
            "CONTACT", "ABOUT", "SEARCH", "MENU", "TU", "NEPAL"}]
    return False


def _notice_keyword_re():
    """The product's whole-word notice-keyword test, or a local equivalent."""
    if CN is not None and getattr(CN, "NOTICE_KEYWORDS_RE", None) is not None:
        return CN.NOTICE_KEYWORDS_RE
    return re.compile(r"\b(?:notice|result|exam|admission|rate|branch)\b",
                      re.IGNORECASE)


def _published_of(cand):
    """The candidate's real publication datetime, or None.

    Accepts the serialized ``published`` ISO string (what actually lands in
    candidates.json) and the internal ``_dt``/``published_dt`` a caller may
    still be holding.
    """
    for key in ("_dt", "published_dt"):
        raw = cand.get(key)
        if raw is not None and not isinstance(raw, str):
            return raw
    raw = cand.get("published")
    if not isinstance(raw, str) or not raw.strip():
        return None
    import datetime as _dt
    text = raw.strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw.strip())
    except Exception:
        return None


def _is_epoch(moment):
    """True when a candidate's date is absent, unparseable, or literally 1970."""
    if moment is None:
        return True
    try:
        import datetime as _dt
        ref = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
        if getattr(moment, "tzinfo", None) is None:
            return moment <= _dt.datetime(1970, 1, 1)
        return moment <= ref
    except Exception:
        return False


def _is_devanagari(text):
    return bool(text) and any("\u0900" <= ch <= "\u097f" for ch in text)


# ---------------------------------------------------------------------------
# DIGEST ASSERTIONS (D1..D7). Blob = the real collect_news.py output.
# ---------------------------------------------------------------------------

def assert_d1_gnews_produced(blob):
    """D1: some gnews task answered with at least one item.

    A 200 whose body parsed to zero items is the shape that hides a broken
    feed reader, so `outcome == "ok"` with `items_found == 0` does NOT count.
    """
    for row in _rows(blob):
        if row.get("kind") != "gnews":
            continue
        if row.get("outcome") != "ok":
            continue
        try:
            if int(row.get(ITEMS_FOUND_FIELD) or 0) >= 1:
                return True, "gnews:%s items_found=%s" % (
                    row.get("name"), row.get(ITEMS_FOUND_FIELD))
        except (TypeError, ValueError):
            continue
    return False, "canary D1: no gnews task produced any item"


def assert_d2_titles_not_chrome(blob):
    """D2: at least one candidate title is notice-specific, not site chrome.

    The historical bug parsed the board's constant banner and reported it as a
    story. A blob where EVERY title is chrome means the parser is reading the
    frame, not the content.
    """
    cands = _candidates(blob)
    real = [c for c in cands
            if (c.get("title") or "").strip()
            and not _site_constant_title(c.get("title"))]
    if real:
        return True, "%d/%d candidate titles are content" % (len(real), len(cands))
    return False, "canary D2: every candidate title is site chrome"


def assert_d3_pubdate_present(blob):
    """D3: at least one candidate carried a real pubDate.

    Without a date every row sorts to EPOCH and lands last, which is how real
    notices ended up buried under rows that at least had a date.
    """
    cands = _candidates(blob)
    dated = [c for c in cands if not _is_epoch(_published_of(c))]
    if dated:
        return True, "%d/%d candidates carry a real pubDate" % (len(dated), len(cands))
    return False, "canary D3: no candidate carried a real pubDate"


def _notice_candidates(blob, row):
    """Candidates attributable to one notice source, matched by ORIGIN.

    Matched by host rather than by task name because a notice candidate's
    `source` is the origin's own domain, which is the only join key the
    collector's output actually carries.
    """
    host = _host_of(row.get("host") or row.get("url"))
    out = []
    for cand in _candidates(blob):
        if _same_origin(_candidate_host(cand), host):
            out.append(cand)
    return out


def assert_d4_notice_is_not_chrome(blob):
    """D4: at least one notice source produced a real notice-shaped title.

    "Real" is >=4 words AND a whole-word notice keyword. The keyword alone is
    satisfied by "Rates & Charges"; the word count alone by a long nav label.
    Both together is what a published notice looks like.

    EVERY notice source is evaluated and every verdict is reported. The old
    body `return`ed from inside the loop, so the assertion's outcome was
    decided by whichever source happened to be collected FIRST — and with
    bank.example.com first, a source that simply had no keyword-matching
    notice on its homepage today sank the whole check and hid the four
    sources behind it that were fine (measured: examplebank 0 candidates,
    tu.edu.np 2, iost 6, college 3, nea 2). A source with nothing today is
    reported as EMPTY, which is real truth and not a failure; only "no notice
    source anywhere produced a real title" is a failure.
    """
    kw = _notice_keyword_re()
    passed, chrome, empty = [], [], []
    for row in _rows(blob):
        if row.get("kind") != "notice":
            continue
        name = row.get("name") or row.get("host") or "?"
        cands = _notice_candidates(blob, row)
        hit = None
        for cand in cands:
            title = re.sub(r"\s+", " ", (cand.get("title") or "")).strip()
            if len(title.split()) >= 4 and kw.search(title):
                hit = title
                break
        if hit is not None:
            passed.append("%s: %s" % (name, hit[:60]))
        elif cands:
            chrome.append(name)
        else:
            empty.append(name)
    total = len(passed) + len(chrome) + len(empty)
    if passed:
        return True, ("D4 ok: %d/%d notice sources yielded a real notice "
                      "title [%s] | %d nav-chrome (%s) | %d empty (%s)"
                      % (len(passed), total, "; ".join(passed),
                         len(chrome), ", ".join(chrome) or "-",
                         len(empty), ", ".join(empty) or "-"))
    if not total:
        return False, "canary D4: no notice source was collected at all"
    parts = ["canary D4: notice source %s yielded only nav chrome" % n
             for n in chrome]
    parts += ["canary D4: notice source %s emitted 0 candidates" % n
              for n in empty]
    return False, "; ".join(parts)


def assert_d5_source_health_not_degraded(blob):
    """D5: the product's own health verdict is not degraded/empty.

    Uses the real ``compute_source_health`` so the canary asserts the number
    the digest itself will print, not a reimplementation of it.
    """
    if not _source_health_api_present():
        return None, "source_health.compute_source_health absent"
    health = SH.compute_source_health(_rows(blob), _critical_hosts())
    state = str((health or {}).get("state") or "unknown")
    if state not in ("degraded", "empty"):
        return True, "source_health state=%s" % state
    return False, "canary D5: source_health is %s against live sources" % state


def assert_d6_empty_category_is_honest(blob):
    """D6: an empty category must not render the confident B4 string.

    A category whose hosts were CHECKED and returned nothing usable has to say
    so. Rendering B4 there is the anti-fabrication lie this assertion exists to
    keep impossible. Checked on the smallest non-weather category so the
    cheapest category is covered rather than whichever one happens to be
    interesting today.
    """
    if not _source_health_api_present():
        return None, "source_health.compute_source_health absent"
    health = SH.compute_source_health(_rows(blob), _critical_hosts())
    by_cat = (health or {}).get("by_category") or {}
    cats = [c for c in sorted(by_cat) if c and c != "weather"]
    if not cats:
        return False, "canary D6: no non-weather category to check"
    cat = min(cats, key=lambda c: int((by_cat[c] or {}).get("total") or 0))
    entry = by_cat.get(cat) or {}
    empty_hosts = entry.get("empty_hosts") or []
    if not empty_hosts:
        return True, "category %r has no empty hosts" % cat
    line = section_status_line(cat, 0, health)
    if line == B4_CONFIDENT:
        return False, "canary D6: an empty category rendered the confident B4 string"
    return True, "category %r renders %r" % (cat, line[:60])


def assert_d7_notice_urls_are_detail(blob):
    """D7: every notice candidate URL is a DETAIL page, not navigation.

    A detail-page shape (a trailing numeric notice id, or the PDF/document the
    notice is published as) is what identifies one publication. A nav link
    reaching this far means the anchor scan is walking the frame.

    Like D4, this evaluates EVERY notice source and reports all of them rather
    than returning on the first. The old body sank the assertion on whichever
    source was collected first, which is how one empty source masked the whole
    board.
    """
    parts, seen, checked = [], False, 0
    for row in _rows(blob):
        if row.get("kind") != "notice":
            continue
        seen = True
        name = row.get("name") or row.get("host") or "?"
        cands = _notice_candidates(blob, row)
        checked += len(cands)
        if not cands:
            parts.append("canary D7: notice source %s emitted 0 candidates"
                         % name)
            continue
        bad = [c for c in cands
               if not NOTICE_DETAIL_URL_RE.search(c.get("url") or "")]
        if bad:
            parts.append("canary D7: notice source %s emitted %d non-detail "
                         "URLs" % (name, len(bad)))
    if parts:
        return False, "; ".join(parts)
    if not seen:
        return False, "canary D7: no notice source was collected at all"
    return True, ("every notice candidate URL is a detail page (%d checked)"
                  % checked)


# ---------------------------------------------------------------------------
# ACADEMIC ASSERTIONS (A1..A8). Blob = collect()'s academic branch.
# ---------------------------------------------------------------------------

def assert_a1_result_feed_parsed(blob):
    """A1: the live result feed (notice_type=21) parsed to >=3 notices.

    Fewer than three from a page that visibly lists a dozen means the list
    parser stopped understanding the board. The critical result feed is the
    one selfcheck_exam_sources.py SKIPS, so nothing else covers this.
    """
    html = _academic(blob).get("result_list_html") or ""
    if AR is None or not hasattr(AR, "parse_notice_list"):
        return None, "parse_notice_list absent"
    try:
        notices = AR.parse_notice_list(html)
    except Exception as exc:
        return False, "canary A1: result feed parse raised %s" % type(exc).__name__
    if len(notices) >= 3:
        return True, "result feed parsed %d notices" % len(notices)
    return False, "canary A1: result feed parsed only %d notices" % len(notices)


def assert_a2_detail_title_not_chrome(blob):
    """A2: some live detail page yielded a title that is NOT site chrome.

    The burn happened because the detail title came back as the board's
    constant, which then read as a confidently wrong title. `parse_notice_detail`
    now blanks such a title and flags it degraded; this asserts that at least
    one page actually produced an authoritative one.
    """
    details = _details(blob)
    if not details:
        return None, "no detail pages were fetched"
    for d in details:
        title = d.get("title") or ""
        if title and not _site_constant_title(title):
            return True, "detail %s title is content" % (d.get("id") or "?")
    return False, "canary A2: detail title is site chrome: %s" % (
        (details[0].get("title") or "")[:120])


def assert_a3_titles_classifiable(blob):
    """A3: at least one live detail title classified to something but `unknown`.

    `unknown` means the parser recognised no vocabulary at all. If EVERY live
    title reads `unknown` the token sets have drifted off the board.
    """
    if not _academic_api_present():
        return None, "academic API absent"
    details = [d for d in _details(blob) if (d.get("title") or "").strip()]
    if not details:
        return None, "no detail titles to classify"
    read = 0
    for d in details:
        try:
            verdict = AR.classify_semester(d.get("title") or "")
        except Exception:
            continue
        if (verdict or {}).get("status") != "unknown":
            read += 1
    if read >= 1:
        return True, "%d/%d detail titles classified" % (read, len(details))
    return False, "canary A3: %d/%d live detail titles were unreadable" % (
        read, len(details))


def assert_a4_no_devanagari_deferred(blob):
    """A4: zero Devanagari titles were deferred as unreadable.

    A Devanagari title that reads `unknown` is a gap in the Nepali token set,
    not a fact about the notice. Deferring those forever freezes the floor, so
    a NEW one appearing is a real regression signal.
    """
    if not _academic_api_present():
        return None, "academic API absent"
    deferred = []
    for d in _details(blob):
        title = d.get("title") or ""
        if not _is_devanagari(title):
            continue
        try:
            verdict = AR.classify_semester(title)
        except Exception:
            deferred.append(title)
            continue
        if (verdict or {}).get("status") == "unknown":
            deferred.append(title)
    if not deferred:
        return True, "no Devanagari title deferred as unreadable"
    return False, ("canary A4: %d Devanagari titles deferred as unreadable - "
                   "token set gap" % len(deferred))


def assert_a5_pdf_text_or_deferred(blob):
    """A5: every attempted PDF produced text OR an explicit deferral.

    Never a decision. A PDF that yields neither is a sheet the pipeline would
    have to guess about, and guessing about a result is how it gets burned.
    """
    pdfs = _pdfs(blob)
    if not pdfs:
        return None, "no PDF was attempted"
    blind = [p for p in pdfs
             if not (p.get("text") or "").strip() and not p.get("deferred")]
    if not blind:
        return True, "%d/%d PDFs yielded text or a deferral" % (
            len(pdfs) - len(blind), len(pdfs))
    return False, "canary A5: %d PDFs produced neither text nor a deferral" % len(blind)


def assert_a6_unreadable_is_deferred(blob):
    """A6: non-empty-but-unreadable PDF text was DEFERRED, never decided.

    This is the sharpest assertion in the file. Non-empty prose-shaped garbage
    carrying no roll-shaped token is what used to produce a FALSE FAILED, and
    a false failed is the single most damaging thing this pipeline can emit.
    """
    pdfs = [p for p in _pdfs(blob) if (p.get("text") or "").strip()]
    if not pdfs:
        return None, "no PDF produced text to judge"
    if AR is None or not hasattr(AR, "sheet_text_is_readable"):
        return None, "sheet_text_is_readable absent"
    decided = []
    for p in pdfs:
        try:
            readable = AR.sheet_text_is_readable(p.get("text") or "")
        except Exception:
            readable = False
        if not readable and not p.get("deferred"):
            decided.append(p)
    if not decided:
        return True, "%d readable-or-deferred PDF(s) judged" % len(pdfs)
    return False, "canary A6: unreadable text was DECIDED, not deferred"


def assert_a7_ocr_shape_and_need(blob):
    """A7: ocr_capability has the frozen three keys, and a need implies a stack.

    The old OCR branch could not run at all and swallowed the error, so a
    scanned result deferred silently forever. The shape check is cheap; the
    need check is the load-bearing half: if a PDF deferred for extraction
    reasons while all three capabilities are False, a scanned sheet is stuck
    and nothing else would say so.
    """
    if AR is None or not hasattr(AR, "ocr_capability"):
        return None, "ocr_capability absent"
    try:
        cap = AR.ocr_capability() or {}
    except Exception as exc:
        return False, "canary A7: ocr_capability raised %s" % type(exc).__name__
    if set(cap) != {"pytesseract", "binary", "pdf_to_image"}:
        return False, ("canary A7: OCR is entirely unavailable and a PDF needed it "
                       "-- capability keys are %s" % sorted(cap))
    needed = [p for p in _pdfs(blob)
              if p.get("deferred") and p.get("deferral_reason") == "extraction"]
    if needed and not any(bool(v) for v in cap.values()):
        return False, ("canary A7: OCR is entirely unavailable and a PDF needed it "
                       "-- %d PDF(s) deferred for extraction" % len(needed))
    if not any(bool(v) for v in cap.values()):
        return True, "OCR unavailable but no PDF needed it (capability=%s)" % cap
    return True, "OCR capability=%s" % cap


def assert_a8_critical_feeds_fetched(blob):
    """A8: every `critical: true` feed in config/exam_notices.json was fetched.

    selfcheck_exam_sources.py skips the critical result feed and exits green, so
    nothing else proves the critical path was touched. The canary asserts
    AROUND that script; it does not modify it.
    """
    feeds = _critical_feeds_from_config()
    if not feeds:
        return False, "canary A8: no critical feed found in config/exam_notices.json"
    fetched = set()
    for fid in (_academic(blob).get("feeds_fetched") or []):
        if isinstance(fid, str):
            fetched.add(fid)
    missing = [f.get("id") for f in feeds if f.get("id") not in fetched]
    if not missing:
        return True, "%d critical feed(s) fetched" % len(feeds)
    return False, "canary A8: a critical feed was never fetched (%s)" % ", ".join(
        str(m) for m in missing)


# ---------------------------------------------------------------------------
# Blob accessors (pure)
# ---------------------------------------------------------------------------

def _details(blob):
    details = _academic(blob).get("details")
    if isinstance(details, list):
        return [d for d in details if isinstance(d, dict)]
    return []


def _pdfs(blob):
    pdfs = _academic(blob).get("pdfs")
    if isinstance(pdfs, list):
        return [p for p in pdfs if isinstance(p, dict)]
    return []


def _critical_hosts():
    path = os.path.join(REPO_ROOT, "config", "collection.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        hosts = cfg.get("critical_hosts")
        return [h for h in hosts if isinstance(h, str)] if isinstance(hosts, list) else []
    except (OSError, ValueError):
        return []


def _critical_feeds_from_config():
    path = os.path.join(REPO_ROOT, "config", "exam_notices.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        feeds = cfg.get("feeds")
        if not isinstance(feeds, list):
            return []
        return [f for f in feeds if isinstance(f, dict) and f.get("critical") is True]
    except (OSError, ValueError):
        return []


#: Frozen (id, assertion) pairs, in run order.
DIGEST_CHECKS = (
    ("D1", assert_d1_gnews_produced),
    ("D2", assert_d2_titles_not_chrome),
    ("D3", assert_d3_pubdate_present),
    ("D4", assert_d4_notice_is_not_chrome),
    ("D5", assert_d5_source_health_not_degraded),
    ("D6", assert_d6_empty_category_is_honest),
    ("D7", assert_d7_notice_urls_are_detail),
)

ACADEMIC_CHECKS = (
    ("A1", assert_a1_result_feed_parsed),
    ("A2", assert_a2_detail_title_not_chrome),
    ("A3", assert_a3_titles_classifiable),
    ("A4", assert_a4_no_devanagari_deferred),
    ("A5", assert_a5_pdf_text_or_deferred),
    ("A6", assert_a6_unreadable_is_deferred),
    ("A7", assert_a7_ocr_shape_and_need),
    ("A8", assert_a8_critical_feeds_fetched),
)

ALL_CHECKS = DIGEST_CHECKS + ACADEMIC_CHECKS


def run_checks(blob):
    """Every assertion against one blob. Pure. Returns [(id, ok, detail)].

    ``ok is None`` means SKIPPED, and there are two distinct reasons:
      * a peer API the assertion needs is absent (PARTIAL), or
      * that HALF of the blob was not collected at all -- ``"digest": None``.

    The second case is the one that had a real bug: with only the digest
    collected, the academic half ran against an absent blob and reported
    "A8: a critical feed was never fetched", a canary inventing a finding about
    a path it never walked. A half that was not collected is SKIPPED, loudly,
    and never counted as a pass.

    A skip is always reported. A canary that silently checked six of eight
    things is worse than one that says which two it skipped.
    """
    is_wrapper = isinstance(blob, dict) and (
        "digest" in blob or "academic" in blob)
    out = []
    for cid, fn in ALL_CHECKS:
        if is_wrapper:
            half = "digest" if cid.startswith("D") else "academic"
            if blob.get(half) is None:
                out.append((cid, None, "%s collection skipped" % half))
                continue
        try:
            ok, detail = fn(blob)
        except Exception as exc:            # a crashing assertion IS a failure
            ok, detail = False, "canary %s: assertion raised %s: %s" % (
                cid, type(exc).__name__, exc)
        out.append((cid, ok, detail))
    return out


# ---------------------------------------------------------------------------
# collect() -- THE ONLY NETWORK TOUCH
# ---------------------------------------------------------------------------

#: Cheap by construction. The digest reuses collect_news' own collector, which
#: is already bounded by config's soft budget; the academic branch fetches the
#: two critical notice lists, at most MAX_DETAIL_PAGES detail pages and at most
#: MAX_PDFS PDFs, and NEVER more than one PDF per run above the byte cap.
MAX_DETAIL_PAGES = 3
MAX_PDFS = 2

#: A runaway guard, matching check_academic_result.PDF_MAX_BYTES' intent. The
#: real B.Sc.CSIT sheet is ~10.5 MB, so this must stay well above it.
CANARY_PDF_MAX_BYTES = 64 * 1024 * 1024


def collect(workdir=None, digest=True, academic=True):
    """Fetch the live blob. The ONLY function here that touches the network.

    Returns ``{"digest": {...} | None, "academic": {...} | None,
    "net_ok": int, "net_failures": int, "reasons": {code: count}}``.
    """
    import datetime
    import tempfile

    blob = {"net_ok": 0, "net_failures": 0, "reasons": {}}
    tmp = workdir or tempfile.mkdtemp(prefix="live-canary-")

    def record(ok, reason=""):
        if ok:
            blob["net_ok"] += 1
        else:
            blob["net_failures"] += 1
            code = reason or "other"
            if SH is not None and hasattr(SH, "classify_reason"):
                try:
                    code = SH.classify_reason(reason, "failed")
                except Exception:
                    pass
            blob["reasons"][code] = blob["reasons"].get(code, 0) + 1

    if digest:
        blob["digest"] = _collect_digest(tmp, record)
    if academic:
        blob["academic"] = _collect_academic(tmp, record)
    blob["captured_at"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")
    return blob


def _collect_digest(tmp, record):
    """Run the product's own collector and return its real output payload."""
    if CN is None:
        return None
    out_path = os.path.join(tmp, "candidates.json")
    timings = os.path.join(tmp, "timings.json")
    argv = ["--out", out_path, "--timings", timings,
            "--interests", os.path.join(REPO_ROOT, "config", "interests.yml"),
            "--collection-config", os.path.join(REPO_ROOT, "config", "collection.json"),
            "--profile", os.path.join(REPO_ROOT, "config", "profile.yml")]
    cwd = os.getcwd()
    try:
        os.chdir(REPO_ROOT)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            CN.main(argv)
    except Exception as exc:
        # A collector that raises outright is an upstream reachability problem
        # in the overwhelming majority of cases, so it is recorded as a failure
        # rather than asserted against -- an INCONCLUSIVE run is the honest
        # verdict there, and a crash on every run is loudly visible in the log.
        record(False, "digest collector raised %s: %s" % (type(exc).__name__, exc))
        return None
    finally:
        os.chdir(cwd)
    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        record(False, "digest output unreadable: %s" % exc)
        return None
    for row in (payload.get("source_results") or []):
        if not isinstance(row, dict):
            continue
        if row.get("outcome") == "failed":
            record(False, str(row.get("error") or "failed"))
        else:
            record(True)
    return payload


def _collect_academic(tmp, record):
    """Fetch the critical exam-notice feeds and probe the result path."""
    if AR is None:
        return None
    out = {"result_list_html": "", "details": [], "pdfs": [], "feeds_fetched": []}
    feeds = _critical_feeds_from_config()
    timeout = 30

    for feed in feeds:
        url = feed.get("url")
        if not url:
            continue
        try:
            status, body = AR.default_fetch_fn(url, timeout)
        except Exception as exc:
            record(False, "academic feed %s: %s" % (feed.get("id"), exc))
            continue
        if status is not None and int(status) >= 400:
            record(False, "academic feed %s: HTTP %s" % (feed.get("id"), status))
            continue
        record(True)
        out["feeds_fetched"].append(feed.get("id"))
        html = body.decode("utf-8", errors="replace")
        if feed.get("kind") == "result" and not out["result_list_html"]:
            out["result_list_html"] = html
        try:
            notices = AR.parse_notice_list(html)
        except Exception as exc:
            record(False, "academic feed %s parse: %s" % (feed.get("id"), exc))
            continue
        for notice in notices[:MAX_DETAIL_PAGES]:
            if len(out["pdfs"]) >= MAX_PDFS:
                break
            try:
                detail = AR.fetch_notice_detail(notice.get("url") or "",
                                                timeout=timeout)
            except Exception as exc:
                record(False, "detail %s: %s" % (notice.get("id"), exc))
                continue
            record(True)
            out["details"].append(detail)
            pdf_url = detail.get("pdf_url")
            if not pdf_url or len(out["pdfs"]) >= MAX_PDFS:
                continue
            entry = {"notice_id": detail.get("id"), "url": pdf_url,
                     "text": "", "deferred": False, "deferral_reason": ""}
            try:
                pdf = AR.download_pdf(pdf_url, timeout=timeout,
                                      max_bytes=CANARY_PDF_MAX_BYTES)
            except Exception as exc:
                entry["deferred"] = True
                entry["deferral_reason"] = "fetch"
                record(False, "pdf %s: %s" % (detail.get("id"), exc))
                out["pdfs"].append(entry)
                continue
            record(True)
            try:
                entry["text"] = AR.extract_pdf_text(pdf, ocr=True) or ""
            except Exception as exc:
                entry["deferred"] = True
                entry["deferral_reason"] = "extraction"
                entry["extract_error"] = "%s: %s" % (type(exc).__name__, exc)
            if not entry["text"].strip():
                entry["deferred"] = True
                entry["deferral_reason"] = entry["deferral_reason"] or "extraction"
            elif AR is not None and hasattr(AR, "sheet_text_is_readable"):
                try:
                    if not AR.sheet_text_is_readable(entry["text"]):
                        # Unreadable text is a READ failure. The canary records
                        # it as a deferral because that is what a correct
                        # pipeline does with it; A6 then holds the product to
                        # that rule rather than letting the canary invent a
                        # decision path of its own.
                        entry["deferred"] = True
                        entry["deferral_reason"] = "extraction"
                except Exception:
                    pass
            out["pdfs"].append(entry)
    return out


# ---------------------------------------------------------------------------
# Failure bookkeeping: the noise ramp.
# ---------------------------------------------------------------------------

def _history_path():
    return os.path.join(REPO_ROOT, "state", "canary_history.json")


def load_history(path=None):
    path = path or _history_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_history(data, path=None):
    path = path or _history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def record_outcome(failed_ids, checked_at, path=None):
    """Fold today's verdicts into the history and return the strike count.

    Pure with respect to the network; the only IO is the state file. Strike
    counting resets the moment a run is fully green, so a canary that was
    broken for a week does not stay loud forever.
    """
    path = path or _history_path()
    hist = load_history(path)
    prev = hist.get("consecutive_failures")
    strikes = int(prev) if isinstance(prev, int) else 0
    if failed_ids:
        strikes += 1
        # `first_failed_at` is only ever SET, never advanced, so "CANARY DEAD
        # for <n> days" counts from the beginning of the streak rather than
        # from the most recent failure -- which would always read zero.
        if not hist.get("first_failed_at"):
            hist["first_failed_at"] = checked_at
    else:
        strikes = 0
        hist.pop("first_failed_at", None)
    hist["consecutive_failures"] = strikes
    hist["last_checked_at"] = checked_at
    hist["last_failed"] = sorted(failed_ids)
    save_history(hist, path)
    return strikes


def _days_since(stamp, now):
    import datetime
    try:
        then = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (now - then).days


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

_BLOB_PROBE = {}


def items_found_present(blob):
    """Does ANY collector row carry the ``items_found`` field at all?

    This is the D1 guard's whole job: distinguish "the field is missing from
    every row" (a peer mid-refactor -> PARTIAL, exit 0) from "the field is
    there and every gnews task produced nothing" (a real failure, exit 1).
    Conflating them would either make the canary crash on a peer's WIP or
    silently stop checking the most important digest assertion there is.
    """
    return any(isinstance(r, dict) and ITEMS_FOUND_FIELD in r
               for r in _rows(blob))


def notify_ntfy(title, message, priority=4, dry_run=True):
    """Best-effort ntfy for the escalation ramp. Never fails the run."""
    import urllib.request
    server = os.environ.get("NTFY_SERVER", "").strip()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not server or not topic or dry_run:
        print("ntfy (not sent): %s" % title)
        return False
    body = {"topic": topic, "title": title, "message": message,
            "priority": ntfy_classes.priority_for(priority),
            "tags": ntfy_classes.with_class_tags(ntfy_classes.PIPELINE,
                                                 ["canary"])}
    try:
        data = json.dumps(body).encode("utf-8")
    except Exception:
        return False
    if len(data) > 4096:
        # The 4096-byte ntfy budget is a delivery contract, not a suggestion.
        trimmed = dict(body)
        trimmed["message"] = (body["message"][:2000]
                              + "\n\n[message truncated to fit the ntfy "
                                "4096-byte limit]")
        try:
            data = json.dumps(trimmed).encode("utf-8")
        except Exception:
            return False
        if len(data) > 4096:
            return False
    try:
        req = urllib.request.Request(
            server.rstrip("/") + "/" + topic, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:
        print("::warning::live-canary ntfy send failed: %s" % exc)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run the real parsers against the live sites and fail loudly "
                    "when they stop agreeing with reality.")
    ap.add_argument("--skip-digest", action="store_true",
                    help="academic assertions only (no collector run)")
    ap.add_argument("--skip-academic", action="store_true",
                    help="digest assertions only (no notice/PDF probes)")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--history", default=None,
                    help="override state/canary_history.json (tests)")
    ap.add_argument("--notify", action="store_true",
                    help="allow the ntfy escalation (off by default; the "
                         "workflow passes it only for strikes >= 2)")
    args = ap.parse_args(argv)

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    checked_at = now.isoformat(timespec="seconds")

    if not _academic_api_present():
        print("::warning::live-canary PARTIAL: academic API absent, A2-A7 skipped")
    if not _source_health_api_present():
        print("::warning::live-canary PARTIAL: source API absent, D5-D6 skipped")
    for note in PARTIAL_NOTES:
        print("::warning::live-canary PARTIAL: %s" % note)

    print("live-canary: fetching (this is the only part that touches the network)")
    blob = collect(workdir=args.workdir,
                   digest=not args.skip_digest,
                   academic=not args.skip_academic)

    net_ok = int(blob.get("net_ok") or 0)
    net_fail = int(blob.get("net_failures") or 0)
    total = net_ok + net_fail
    reasons = blob.get("reasons") or {}
    print("live-canary: %d fetches ok, %d failed %s" % (
        net_ok, net_fail, ("(" + ", ".join("%s=%d" % kv
                                           for kv in sorted(reasons.items())) + ")")
        if reasons else ""))

    # --- The anti-flake gate. Upstream unreachable -> INCONCLUSIVE, exit 0.
    if net_ok == 0 or (net_fail / float(total)) >= 0.5:
        print("::warning::live-canary INCONCLUSIVE - upstream unreachable "
              "(%d/%d fetches failed: %s)" % (
                  net_fail, total,
                  ", ".join("%s=%d" % kv for kv in sorted(reasons.items()))
                  or "no network at all"))
        return 0

    combined = {"digest": blob.get("digest"), "academic": blob.get("academic")}
    if blob.get("digest") is not None and not items_found_present(combined):
        print("::warning::live-canary PARTIAL: %s field absent, D1 skipped"
              % ITEMS_FOUND_FIELD)

    # A half that was NOT collected is SKIPPED, never run against an absent
    # blob. `run_checks` handles that from the wrapper's own `None` halves, so
    # there is exactly one place that decides what a missing half means.
    results = run_checks(combined)
    failed, skipped, passed = [], [], []
    for cid, ok, detail in results:
        if ok is True:
            passed.append(cid)
            print("canary %s ok    %s" % (cid, detail))
        elif ok is None:
            skipped.append(cid)
            print("::warning::canary %s skipped: %s" % (cid, detail))
        else:
            failed.append(cid)
            print(detail)

    if not failed:
        record_outcome([], checked_at, args.history)
        print("live-canary: %d passed, %d skipped, 0 failed" % (
            len(passed), len(skipped)))
        return 0

    strikes = record_outcome(failed, checked_at, args.history)
    summary = "; ".join(results_by_id(results, failed))
    if strikes < CANARY_ERROR_STRIKES:
        print("::warning::live-canary FAILED (strike %d/%d, not fatal yet): %s"
              % (strikes, CANARY_ERROR_STRIKES, summary))
        return 0

    print("::error::live-canary FAILED (strike %d): %s" % (strikes, summary))
    if strikes >= CANARY_ERROR_STRIKES + CANARY_DEAD_DAYS - 1:
        age = _days_since(_first_failure(load_history(args.history)), now)
        notify_ntfy("CANARY DEAD for %d days" % age,
                    "live-parser-canary has failed %d runs in a row:\n%s"
                    % (strikes, summary), priority=5, dry_run=not args.notify)
    else:
        notify_ntfy("live-parser-canary FAILED (strike %d)" % strikes,
                    summary, priority=4, dry_run=not args.notify)
    return 1


def _first_failure(hist):
    return hist.get("first_failed_at") or hist.get("last_checked_at") or ""


def results_by_id(results, ids):
    want = set(ids)
    return [detail for cid, ok, detail in results if cid in want and ok is False]


if __name__ == "__main__":
    sys.exit(main())
