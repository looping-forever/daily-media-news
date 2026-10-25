#!/usr/bin/env python3
"""selfcheck_exam_sources.py — is the exam-notice coverage still real?

The product's exam-routine / form-deadline coverage rests on a handful of
scrapers plus a title matcher set. If TU renames its notices, the matchers go
quietly stale and the owner stops hearing about routines and deadlines
without anything ever failing. This script is the loud-failure check for that.

Contract (F5)
--------------
* Per feed, fetch. A fetch failure marks the feed ``live: false`` **in the run
  summary only** — ``config/exam_notices.json`` is NEVER written by this
  script — prints ``::warning::`` and exits 0. Self-disabling here means
  disabling its own VERDICT, never the product.
* Fetch OK but the ``exam_notice`` matchers hit 0 of the first N notices means
  the matcher set is probably stale: emit ``::error::``, set
  ``verified_against_live: false`` in the SUMMARY, and exit 1.
* A verdict change vs the last recorded verdict, or no successful check in
  7 days, raises ONE ntfy warning, rate-limited by a dated marker in
  ``state/exam_selfcheck.json`` (one JSON record per line, appended on EVERY
  run including failures).
* This script can never delay or block a result check. It does not read or
  write ``state/academic_state.json``, and the workflow step that runs it uses
  ``continue-on-error: true``.

Exit codes
----------
0  all feeds live, or at least one feed was simply unreachable
1  a live feed's exam_notice matchers matched nothing — matcher set is stale

CLI:
  py scripts/selfcheck_exam_sources.py [--config config/exam_notices.json]
      [--state state/exam_selfcheck.json] [--timeout 15] [--no-ntfy]
      [--dry-run]

Stdlib only. ``--dry-run`` suppresses the ntfy POST but still records the run.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import ntfy_classes  # noqa: E402  (deliberate: after the sys.path insert)

from check_academic_result import (  # noqa: E402
    DEFAULT_EXAM_NOTICES_PATH,
    parse_notice_list,
    default_fetch_fn,
    load_exam_notice_config,
    match_exam_notice,
    classify_semester,
    fetch_notice_detail,
    download_pdf,
    extract_pdf_text,
    sheet_text_is_readable,
    is_site_constant_title,
    FetchError,
)

DEFAULT_STATE = os.path.join(REPO_ROOT, "state", "exam_selfcheck.json")
STALE_DAYS = 7
RATE_LIMIT_HOURS = 24
# Below this many parsed notices, "0 matched" is not evidence of a stale
# matcher set - it is evidence that the page shape changed or the fetch
# returned a shell. See the minimum-sample guard in main().
MIN_SAMPLE = 5
USER_AGENT = ("Mozilla/5.0 (compatible; daily-media-news-academic-selfcheck/1.0; "
              "personal exam-notice coverage self-check; stdlib-urllib)")


# ---------------------------------------------------------------------------
# Run log (one JSON record per line, appended on EVERY run)
# ---------------------------------------------------------------------------


def read_runs(path):
    """Read the JSON-lines run log, tolerating a corrupt/short file.

    state/exam_selfcheck.json holds one JSON object per line. A plain json.load
    on it would fail, so it is parsed line by line and bad lines are skipped
    rather than allowed to take the watchdog down."""
    runs = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    runs.append(rec)
    except (OSError, ValueError):
        return []
    return runs


def append_run(path, record, cap=200):
    """Append one record. NEVER raises: a self-check must not fail because its
    own bookkeeping could not be written."""
    try:
        runs = read_runs(path)
        runs.append(record)
        if len(runs) > cap:
            runs = runs[-cap:]
        out_dir = os.path.dirname(os.path.abspath(path))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            for rec in runs:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return True
    except (OSError, ValueError) as exc:
        print("::warning::could not write %s: %s" % (path, exc))
        return False


def _parse_iso(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def result_failure_fingerprint(result_failures):
    """A stable hash of the WHOLE result-path failure set, or None if empty.

    WHY A FINGERPRINT AND NOT A TIMESTAMP. The result-path alert fired on every
    single hourly run for three days -- four identical notifications the owner
    received -- because this branch had no dedup at all while the stale-matcher
    branch right below it did. The obvious fix, "only alert every 24h", is the
    wrong one: it would hide a DETERIORATION. If a second notice starts failing,
    or the same notice starts failing a new way, the owner must hear about it
    immediately, inside the cooldown window or not.

    So the cooldown is keyed on WHAT BROKE, not on WHEN. Re-sending is suppressed
    only when the failure set is byte-identical to one already announced inside
    the window. Every probe contributes, so a second broken notice changes the
    hash even though `result_failures[0]` is unchanged.

    The pairs are sorted, so the same failures arriving in a different ORDER are
    correctly treated as the same condition rather than as a new one.
    """
    items = sorted(
        "%s|%s" % (probe.get("id") or "", line)
        for probe in (result_failures or [])
        for line in (probe.get("failures") or [])
    )
    if not items:
        return None
    blob = json.dumps(items, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def result_failure_already_notified(runs, fingerprint, now,
                                    hours=RATE_LIMIT_HOURS):
    """True when THIS exact failure set was already announced inside `hours`.

    Scans backwards and stops at the first match, so the most recent send wins:
    a fingerprint announced at t=0 and again at t=+23h is suppressed until t=+24h
    after the LATEST of them, not the first.
    """
    if not fingerprint:
        return False
    cutoff = now - timedelta(hours=hours)
    for rec in reversed(runs or []):
        if rec.get("result_fingerprint") != fingerprint:
            continue
        if not rec.get("result_notified"):
            continue
        at = _parse_iso(rec.get("at"))
        if at and at > cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# Feed checking
# ---------------------------------------------------------------------------


def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()

def check_feed(feed, matchers, fetch_fn, timeout, max_notices):
    """Fetch one feed and report {id, live, parsed, matched, error}."""
    result = {"id": feed.get("id"), "url": feed.get("url"),
              "kind": feed.get("kind"), "live": False, "parsed": 0,
              "matched": 0, "exam_notice_matched": 0, "error": None}
    feed_url = feed.get("url")
    if not feed_url:
        result["error"] = "feed has no url"
        return result
    try:
        status, body = fetch_fn(feed_url, timeout)
    except Exception as exc:
        result["error"] = "fetch failed: %s" % exc
        return result
    if status is not None and int(status) >= 400:
        result["error"] = "HTTP %s" % status
        return result
    html = body.decode("utf-8", errors="replace")
    notices = parse_notice_list(html)
    result["live"] = True
    result["parsed"] = len(notices)
    require_marker = bool(feed.get("require_marker"))
    matched = 0
    for notice in notices[:max(2, int(max_notices or 10))]:
        if notice.get("title_degraded"):
            continue
        if match_exam_notice(notice.get("title") or "", matchers, require_marker):
            matched += 1
    result["matched"] = matched
    result["exam_notice_matched"] = matched
    result["sample_titles"] = [
        (n.get("title") or "")[:120] for n in notices[:3]]
    return result


# ---------------------------------------------------------------------------
# The RESULT path (P1, added 2026-09-29)
# ---------------------------------------------------------------------------
# WHY THIS EXISTS. This script only ever fetched a notice LIST and matched
# titles with the exam_notice matchers. It never opened a detail page and never
# downloaded a PDF, and the `critical: true` RESULT feed was skipped outright by
# the matcher logic. So the entire result path - the thing the owner actually
# depends on - could be completely broken while this script printed a cheerful
# summary and exited 0. It went green through the sem-1 burn: the site-constant
# <title>, the Latin-only semester matcher, and the OCR branch that never
# rasterised anything were all invisible to it.
#
# WHAT IT NOW ASSERTS, on a feed marked critical:true:
#   1. the result list is fetchable and parses into notices;
#   2. a real DETAIL page yields an authoritative title
#      (title_degraded False) - the site-constant bug in the flesh;
#   3. classify_semester on that title is NOT "unknown" - i.e. the Devanagari
#      and Latin vocabularies can still READ a real board title. A title we
#      cannot read is not a quiet pass, it is a floor that never moves;
#   4. a real PDF downloads and extract_pdf_text yields a roll-shaped token
#      (an 8-digit token or the split "1357 2468" form the real sheet uses).
#
# EXIT-CODE SEMANTICS - UNCHANGED IN SPIRIT, and the distinction is the point:
#   * the result feed is UNREACHABLE (DNS/SERVFAIL/HTTP error, e.g. the
#     tu.edu.np zone being down) -> ``::warning::``, verdict "unreachable",
#     exit 0. Self-disabling means disabling its own VERDICT, never the
#     product, and never turning someone else's outage into our alert.
#   * the result feed is REACHABLE but a probe assertion FAILS -> ``::error::``,
#     the run is marked not-ok, and the exit code is 1. Silence about a broken
#     result path is the failure this whole addition exists to prevent, so this
#     is NOT softened to keep the log quiet.
#   * no notices on the feed to probe -> ``::warning::``, exit 0 (an empty
#     result feed is not evidence of a broken pipeline).

def check_result_path(feed, cfg, fetch_fn, timeout, max_probes=3):
    """Probe the RESULT path end to end. Returns a result dict.

    Never raises: a failure is reported in the returned dict, because this
    script must always be able to record a run.
    """
    rp = (cfg.get("result_path") or {})
    result = {
        "id": feed.get("id"),
        "url": feed.get("url"),
        "probed": False,
        "live": False,
        "detail_ok": None,
        "title": None,
        "classify": None,
        "semester": None,
        "pdf_ok": None,
        "roll_token_found": None,
        "failures": [],
        "error": None,
        "samples": [],
    }
    feed_url = feed.get("url")
    if not feed_url:
        result["error"] = "feed has no url"
        return result

    probe_timeout = int(rp.get("timeout") or max(timeout, 30))
    # --- 1. the list must be fetchable --------------------------------------
    try:
        status, body = fetch_fn(feed_url, probe_timeout)
        if status is not None and int(status) >= 400:
            result["error"] = "HTTP %s" % status
            return result
        notices = parse_notice_list(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        # Unreachable. This is the "self-disable safely" path: the board (or
        # the whole tu.edu.np zone) is down, which is not our bug and must not
        # raise an error against anyone.
        result["error"] = "fetch failed: %s" % exc
        return result
    result["live"] = True

    probes = []
    known_id = rp.get("known_result_notice_id")
    if known_id:
        try:
            probes.append(int(known_id))
        except (TypeError, ValueError):
            pass
    for notice in notices:
        try:
            nid = int(notice.get("id"))
        except (TypeError, ValueError):
            continue
        if nid in probes:
            continue
        probes.append(nid)
        if len(probes) >= max(int(rp.get("max_probed_notices") or max_probes),
                               1):
            break
    if not probes:
        result["error"] = "no notices parsed from the result feed"
        return result

    by_id = {n.get("id"): n for n in notices}
    for nid in probes:
        notice = by_id.get(nid)
        if notice is None:
            continue
        # --- 2. the detail page must yield an authoritative title -----------
        try:
            detail = fetch_notice_detail(notice.get("url") or
                                         "%s%s" % (feed_url.rstrip("/"), nid),
                                         fetch_fn=fetch_fn, timeout=probe_timeout)
        except FetchError as exc:
            result["failures"].append("notice %d detail fetch failed: %s"
                                      % (nid, exc))
            continue
        except Exception as exc:
            result["failures"].append("notice %d detail error: %s"
                                      % (nid, exc))
            continue
        title = (detail.get("title") or "").strip()
        if not title or detail.get("title_degraded") \
                or is_site_constant_title(title):
            # THE site-constant burn, caught by name. If <title> is being taken
            # as the notice title again, the result path is blind.
            result["failures"].append(
                "notice %d detail title is not authoritative (source=%r, "
                "degraded=%r, title=%r) - the site-constant <title> bug is "
                "back" % (nid, detail.get("title_source"),
                          bool(detail.get("title_degraded")), title[:80]))
            continue
        result["detail_ok"] = True
        result["title"] = title[:200]
        result["samples"].append(title[:120])

        # --- 3. the title must be READABLE, not merely present -------------
        verdict = classify_semester(title)
        result["classify"] = verdict["status"]
        result["semester"] = verdict["semester"]
        if verdict["status"] == "unknown":
            result["failures"].append(
                "notice %d title is UNREADABLE by every script's vocabulary "
                "(script=%s, stage_hits=%s): %r - the token set does not cover "
                "this board, and a real result in this wording would defer "
                "forever" % (nid, verdict["script"], verdict["stage_hits"],
                             title[:80]))
            continue

        # --- 4. the PDF must download and yield a roll-shaped token ---------
        if rp.get("require_pdf") is False:
            continue
        pdf_url = detail.get("pdf_url")
        if not pdf_url:
            continue
        try:
            pdf_bytes = download_pdf(pdf_url, fetch_fn=fetch_fn,
                                     timeout=probe_timeout)
        except FetchError as exc:
            result["failures"].append("notice %d pdf download failed: %s"
                                      % (nid, exc))
            continue
        except Exception as exc:
            result["failures"].append("notice %d pdf error: %s" % (nid, exc))
            continue
        text = extract_pdf_text(pdf_bytes)
        if not text:
            result["failures"].append(
                "notice %d PDF yielded no text - the extraction path is "
                "broken (a scanned sheet needs pytesseract + the tesseract "
                "binary + a PDF rasteriser)" % nid)
            continue
        if not sheet_text_is_readable(text):
            result["failures"].append(
                "notice %d PDF text carries no roll-shaped token, so a pass or "
                "fail could never be decided from it" % nid)
            continue
        result["pdf_ok"] = True
        result["roll_token_found"] = True
        # A roll-shaped token is a SHAPE, never a roll number: nothing from a
        # real result sheet (other students' names, rolls and marks) is printed,
        # stored or transmitted by this check.
        break

    if result["detail_ok"] and not result["failures"]:
        result["probed"] = True
    return result


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------


def notify_ntfy(title, message, dry_run, class_tag=ntfy_classes.PIPELINE):
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    server = (os.environ.get("NTFY_SERVER") or "").strip()
    if not topic or not server or not server.startswith(("http://", "https://")):
        print("::warning::NTFY_TOPIC/NTFY_SERVER unset — self-check warning "
              "not sent (issue still raised via the step summary)")
        return False
    if dry_run:
        print("DRY RUN — self-check ntfy not sent: %s" % title)
        return True
    # The class tag is what makes this notification filterable on the owner's
    # lock screen, where a digest and an alert otherwise look identical. It is
    # a literal from ntfy_classes, never assembled from `message`, so nothing
    # fetched or generated can relabel an honesty signal. wire_shape() is what
    # guarantees tags stays a list and priority a number -- the two shapes ntfy
    # answers with HTTP 400.
    body = json.dumps(ntfy_classes.wire_shape(
        topic, title, message, class_tag,
        tags=["warning", "mag"], priority=4)).encode("utf-8")
    try:
        req = urllib.request.Request(server.rstrip("/") + "/", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json",
                                              "Markdown": "yes"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        print("self-check ntfy sent: %s" % title)
        return True
    except Exception as exc:
        print("::warning::self-check ntfy failed: %s" % exc)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify the exam-notice matcher set still matches reality")
    parser.add_argument("--config", default=DEFAULT_EXAM_NOTICES_PATH)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--no-ntfy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-result-path", action="store_true",
                        help="skip the RESULT-path probe (a detail page + a "
                             "PDF). Only for callers that serve notice LIST "
                             "pages only; production must not pass it.")
    args = parser.parse_args(argv)

    cfg = load_exam_notice_config(args.config)
    if not cfg:
        print("::error::exam-notice config unreadable: %s" % args.config)
        return 1
    if not cfg.get("enabled"):
        print("exam-notice coverage is disabled in %s — nothing to check"
              % args.config)
        return 0

    self_check = cfg.get("self_check") or {}
    if self_check.get("enabled") is False:
        print("self-check is disabled in %s — nothing to check" % args.config)
        return 0
    max_notices = int(self_check.get("max_notices") or 10)
    require_exam_match = bool(self_check.get("require_exam_match", True))
    matchers = cfg.get("matchers") or {}
    feeds = [f for f in (cfg.get("feeds") or []) if isinstance(f, dict)]
    if not feeds:
        print("::error::no feeds configured in %s" % args.config)
        return 1

    fetch_fn = default_fetch_fn
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    results = []
    stale_feeds = []
    result_failures = []
    for feed in feeds:
        result = check_feed(feed, matchers, fetch_fn, args.timeout, max_notices)
        results.append(result)
        if not result["live"]:
            print("::warning::exam feed %s is not live: %s"
                  % (result["id"], result["error"]))
            continue
        line = ("exam feed %s live: %d notice(s) parsed, %d matched"
                % (result["id"], result["parsed"], result["matched"]))
        print(line)
        if feed.get("kind") != "exam_notice" or not require_exam_match:
            continue
        if result["exam_notice_matched"] == 0 and result["parsed"] > 0:
            # MINIMUM SAMPLE GUARD. "0 of 3 matched" is weak evidence: three
            # anchors parsed off a notice board is far fewer than a real page
            # renders, and a low count usually means the page shape changed (or
            # the fetch returned a shell/error page) rather than that the
            # matchers went stale. Below the floor we do NOT claim the matcher
            # set is stale - we say we cannot judge, and we print the titles we
            # did see so the DOM can be compared by eye. This narrows no real
            # signal: a genuinely stale matcher set against a full page still
            # trips the hard error.
            if result["parsed"] < MIN_SAMPLE:
                print("::warning::exam_notice matchers matched 0/%d notices on "
                      "%s - but %d is below the %d-notice sample floor, so this "
                      "is INSUFFICIENT SAMPLE, not proof of a stale matcher "
                      "set. Sample titles: %s"
                      % (result["parsed"], result["id"], result["parsed"],
                         MIN_SAMPLE,
                         " | ".join(t for t in result.get("sample_titles", [])
                                   if t) or "(none captured)"))
                continue
            stale_feeds.append(feed.get("id"))
            print("::error::exam_notice matchers matched 0/%d notices on %s — "
                  "matcher set is probably stale"
                  % (min(max_notices, result["parsed"]), result["id"]))
            for sample in result.get("sample_titles", []):
                print("   sample title: %s" % sample)
        elif result["parsed"] == 0:
            print("::warning::exam feed %s parsed 0 notices — cannot judge the "
                  "matchers from an empty page" % result["id"])

    result_path_cfg = cfg.get("result_path") or {}
    # The probe requires BOTH: a declared result path in the config (absent
    # block = this deployment has not declared one) AND the runtime switch
    # below, which defaults ON.
    #
    # The switch exists because the probe needs a real DETAIL page, and a
    # caller that only serves notice LIST pages — which is all the
    # exam_notice matcher has ever needed — would otherwise be reported as a
    # broken result path on every run. That is a false alarm, and a false alarm
    # that fires on every run is how a real check gets silenced. Production
    # leaves it on (the workflow passes no such flag); a caller with no detail
    # pages switches it off explicitly and says so.
    result_path_enabled = bool(result_path_cfg) \
        and result_path_cfg.get("enabled", True) is not False \
        and not args.no_result_path
    result_probe = None
    if bool(result_path_cfg) and args.no_result_path:
        print("::warning::result-path self-check DISABLED by --no-result-path; "
              "the result path is NOT being verified this run")
    if result_path_enabled:
        critical = [f for f in feeds
                    if f.get("kind") == "result" and f.get("critical")]
        if not critical:
            print("::warning::no feed is marked kind=result + critical:true, so "
                  "the result path cannot be self-checked")
        for feed in critical:
            result_probe = check_result_path(feed, cfg, fetch_fn, args.timeout)
            if not result_probe.get("live"):
                # SELF-DISABLE, SAFELY. The board (or the whole tu.edu.np zone)
                # is unreachable. That is not evidence about our pipeline, so
                # it is a warning and the verdict is suppressed, never an error.
                print("::warning::result feed %s is not live: %s - the RESULT "
                      "path is NOT self-checked this run (unreachable board, "
                      "not a pipeline fault)"
                      % (result_probe.get("id"), result_probe.get("error")))
                result_probe = None
                continue
            if result_probe.get("probed"):
                print("result path OK on %s: detail title %r classified %s"
                      % (result_probe["id"], result_probe.get("title"),
                         result_probe.get("classify")))
                if result_probe.get("pdf_ok"):
                    print("result path OK on %s: PDF extracted and a "
                          "roll-shaped token was found" % result_probe["id"])
            else:
                for failure in result_probe.get("failures") or []:
                    print("::error::result path on %s: %s"
                          % (result_probe.get("id"), failure))
                if not result_probe.get("failures"):
                    print("::error::result path on %s: reachable, but no "
                          "notice could be probed end to end"
                          % result_probe.get("id"))
                result_failures.append(result_probe)
            break

    runs = read_runs(args.state)
    last_success = None
    last_verdict = None
    for rec in runs:
        if rec.get("ok"):
            last_success = _parse_iso(rec.get("at")) or last_success
            if last_verdict is None:
                last_verdict = rec.get("verdict")

    verdict = "stale" if stale_feeds else "ok"
    if result_failures:
        verdict = "stale" if verdict == "stale" else "result_path_broken"

    # The dedup decision is made HERE, before the one `append_run` below, so a
    # run writes exactly one record and the "did we already announce this exact
    # failure set?" marker is persisted in the same write. Deciding after the
    # append would need a second write, and two writes per run is two chances
    # for the marker to be lost.
    result_fp = result_failure_fingerprint(result_failures)
    result_already_notified = result_failure_already_notified(
        runs, result_fp, now)

    record = {
        "at": stamp,
        "ok": not stale_feeds and not result_failures,
        "verdict": verdict,
        "feeds": [{k: r.get(k) for k in
                   ("id", "live", "parsed", "matched", "error")}
                  for r in results],
        "stale_feeds": stale_feeds,
        "result_path": (
            {k: result_probe.get(k) for k in
             ("id", "live", "probed", "detail_ok", "classify", "semester",
              "pdf_ok", "roll_token_found", "error", "failures")}
            if result_probe else {"live": False,
                                  "skipped": "unreachable or disabled"}),
        "config_verified_against_live": cfg.get("verified_against_live"),
    }
    if result_fp:
        record["result_fingerprint"] = result_fp
        record["result_notified"] = bool(result_failures
                                         and not result_already_notified)
    # The run log is written on EVERY run, including failures — the dead-man's
    # switch depends on its recency, so a skipped write would hide itself.
    append_run(args.state, record)

    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("## Exam-notice source self-check\n\n")
                fh.write("- Verdict: **%s**\n" % verdict)
                fh.write("- verified_against_live (config, unchanged by this "
                         "script): %r\n" % cfg.get("verified_against_live"))
                for r in results:
                    fh.write("- `%s` — live=%s parsed=%s matched=%s%s\n"
                             % (r["id"], r["live"], r["parsed"], r["matched"],
                                (" error=%s" % r["error"]) if r["error"] else ""))
                if stale_feeds:
                    fh.write("- **exam_notice matcher set looks STALE on: %s**\n"
                             % ", ".join(stale_feeds))
                    fh.write("- `verified_against_live` is now treated as "
                             "**false** (summary only — config/exam_notices.json "
                             "is never written by this script)\n")
                if result_probe:
                    fh.write("- Result path on `%s`: live=%s probed=%s "
                             "detail_ok=%s classify=%s pdf_ok=%s "
                             "roll_token=%s\n"
                             % (result_probe.get("id"),
                                result_probe.get("live"),
                                result_probe.get("probed"),
                                result_probe.get("detail_ok"),
                                result_probe.get("classify"),
                                result_probe.get("pdf_ok"),
                                result_probe.get("roll_token_found")))
                for failure in result_failures:
                    for line in (failure.get("failures") or []):
                        fh.write("- **RESULT PATH BROKEN** (`%s`): %s\n"
                                 % (failure.get("id"), line))
        except OSError as exc:
            print("::warning::could not write step summary: %s" % exc)

    if result_failures:
        # A REACHABLE board whose result path is broken is the loudest thing
        # this script can say, and it exits 1. It is NOT softened to reduce
        # noise: silence about a broken result path is exactly how the sem-1
        # pass was lost for nineteen hours.
        #
        # What IS suppressed is the REPEAT, never the warning. The verdict, the
        # exit 1, the ::error:: lines and the step-summary block above are
        # untouched and still fire on every single run. Only the PUSH is
        # rate-limited, and only when the failure set is byte-identical to one
        # already pushed inside RATE_LIMIT_HOURS. A different notice number, a
        # different failure class, a second broken notice -- any of those
        # changes the fingerprint and pages the owner immediately.
        if result_already_notified:
            print("self-check result-path warning already sent in the last %dh "
                  "for this EXACT failure set (%s) — rate-limited. The run still "
                  "exits 1 and the step summary still carries the failure."
                  % (RATE_LIMIT_HOURS, result_fp))
        else:
            notify_ntfy(
                "⚠️ Result path self-check FAILED",
                "The result path on %s is reachable but broken: %s. A real "
                "result notice would be missed or misread."
                % (", ".join(sorted({str(f.get("id") or "?")
                                     for f in result_failures})),
                   "; ".join(sorted({line
                                     for f in result_failures
                                     for line in (f.get("failures") or [])}))
                   [:400]),
                args.dry_run or args.no_ntfy,
                class_tag=ntfy_classes.RESULT)
        return 1

    if stale_feeds:
        notify_ntfy(
            "⚠️ Exam-notice matchers look stale",
            "exam_notice matchers matched 0 notices on: %s. TU may have renamed "
            "its notices. The result path is unaffected; routines and "
            "deadlines are not being detected." % ", ".join(stale_feeds),
            args.dry_run or args.no_ntfy, class_tag=ntfy_classes.PIPELINE)
        return 1

    # --- Dead-man's switch: no successful check in STALE_DAYS ----------------
    notify = False
    reason = ""
    if last_success is None:
        notify, reason = True, "no previous successful self-check on record"
    elif now - last_success > timedelta(days=STALE_DAYS):
        notify, reason = True, ("last successful self-check was %s (%d days "
                                "ago)" % (last_success.date(),
                                          (now - last_success).days))
    if last_verdict is not None and last_verdict != verdict:
        notify = True
        reason = (reason + "; " if reason else "") + \
            "verdict changed %s -> %s" % (last_verdict, verdict)
    if notify:
        marker_cutoff = now - timedelta(hours=RATE_LIMIT_HOURS)
        recent = False
        for rec in runs:
            at = _parse_iso(rec.get("at"))
            if at and at > marker_cutoff and rec.get("notified"):
                recent = True
                break
        if recent:
            print("self-check warning already sent in the last %dh — "
                  "rate-limited" % RATE_LIMIT_HOURS)
        else:
            sent = notify_ntfy(
                "⚠️ Exam-source self-check needs attention",
                "%s. Routines and form deadlines depend on this matcher set." % reason,
                args.dry_run or args.no_ntfy, class_tag=ntfy_classes.PIPELINE)
            record["notified"] = bool(sent)
            append_run(args.state, record)
            print("::warning::exam-source self-check: %s" % reason)

    print("exam self-check verdict: %s" % verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
