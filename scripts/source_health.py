#!/usr/bin/env python3
"""source_health.py -- pure derivation of the per-source honesty signal.

Stage-B (validate_stories.py) turns the collector's raw `source_results` list
into one `source_health` object that renderers can trust. The whole point is
that the freshness banner must be computed in deterministic code, never left to
the LLM agent: prompts/daily-digest.md tells the agent to keep research_status
"full" when most domains succeed, so a TOTAL Tribhuvan University DNS outage
(the `tu.edu.np` zone is SERVFAIL from public resolvers) produced no banner at
all.

Hard rules baked into this module:
  * `"ok"` means REACHABLE, never "produced items". A 200 with an empty RSS
    body is reachable -- and, since schema 2, it is ALSO `"empty"`, because
    "I checked and there was nothing" must not be indistinguishable from
    "I checked and got garbage back". See the `empty` analysis below.
  * `"skipped_budget"` is NOT `"failed"`. A task the soft budget never started
    is UNKNOWN, not DEAD. Counting it as a failure is a lie.
  * NEVER raises. Missing/corrupt/legacy input degrades to state "unknown" with
    zeroed counters and an empty checked_at_npt. A v1 `source_health` blob read
    by this module's consumers must also degrade, never explode: every new
    field defaults to `0`/`[]`.
  * No I/O, no clock, no imports from other project modules. `now_npt` is
    passed in by the caller so this stays a pure function and is testable.

WHY `empty` IS ADDITIVE AND NEVER REPLACES `ok`
-----------------------------------------------
`unreachable_hosts()` below is the anti-fabrication gate's input, and it keys
its ok-shield on `outcome == "ok"`. If `empty` had REPLACED `ok`, a host with
one empty-but-reachable sibling task and one failed task would be classified
dark and the gate would DELETE that host's real stories -- trading a silent-burn
bug for a silent story-deletion bug, which is strictly worse. So:

  * `outcome` stays a REACHABILITY field. The ok-shield accepts {"ok","empty"}.
  * `empty` is an ADDITIVE yield signal layered on top of that, and it is
    shielded the same way: a host with one empty task and one productive task is
    still working, so its stories are never at risk and its sections do not
    borrow B4's confident "nothing new".

Closed reason-code set (renderers never parse the free-text `error` field):
  dns | timeout | tls | connection | throttled | http_4xx | http_5xx |
  parse | budget | malformed | other
"""

import logging
import re

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
REASON_CODES = (
    "dns", "timeout", "tls", "connection", "throttled", "http_4xx",
    "http_5xx", "parse", "budget", "malformed", "other",
)
VALID_OUTCOMES = ("ok", "empty", "failed", "skipped_budget")
#: Outcomes that prove the request was REACHABLE. `empty` is reachable: the
#: fetch succeeded and the body simply held nothing usable. This set is the
#: ok-shield, and widening it must never mean "the host went dark".
REACHABLE_OUTCOMES = ("ok", "empty")
#: Failure reasons that are, on their own, evidence of a BLIP rather than of
#: an outage: the host never answered at all. Used by state-table row 2b, so
#: that a DNS blip on one task of a critical host that hosts 20+ tasks cannot
#: flip the whole run to `degraded`, while a critical host that answers with
#: 403/500/tls errors can.
DNS_FAILURE_REASONS = ("dns",)
#: How many of ONE critical host's own tasks must fail with a non-dns reason
#: before the run is called `degraded` (state-table row 2b).
#:
#: WHY 2 AND NOT 1. The first cut of row 2b fired on a SINGLE non-dns
#: critical failure. That is a cry-wolf machine: `news.google.com` is a
#: critical host that carries 20+ sweep tasks, so one 503 on one of them
#: relabelled the whole digest "some sources were unreachable", every single
#: run, and a banner that cries wolf is a banner the owner learns to skip --
#: which defeats the entire honesty contract. Two failures ON THE SAME HOST is
#: the smallest claim that is still about a host rather than about a task: one
#: sweep is a blip, two sweeps of the same host are an outage.
#:
#: This is a PER-HOST count, never a sum across hosts: two failures on two
#: DIFFERENT critical hosts is the normal background noise of a slow network
#: and must not escalate anything. A genuinely dark host fails on dns, which
#: row 2 (`hosts_down`) already catches without this threshold.
NON_DNS_FAILURE_THRESHOLD = 2
#: Default for `min_items_per_source`: only a literal zero counts as empty.
DEFAULT_MIN_ITEMS_PER_SOURCE = 1
VALID_KINDS = ("gnews", "notice", "weather")

GNEWS_HOST = "news.google.com"
WEATHER_HOST = "api.open-meteo.com"
# Fallback URL a renderer can offer for a category with no live notice page.
CATEGORY_URL_FALLBACK = "https://news.google.com/"
MAX_CATEGORY_URLS = 3

# Reason-code probes. Order in classify_reason() is significant: more specific
# codes are tested before the looser ones (e.g. 429 -> throttled, not http_4xx).
_DNS_TOKENS = (
    "name resolution",
    "getaddrinfo",
    "errno -2",
    "errno -3",
    "nodename nor servname",
    "name or service not known",
    "name does not resolve",
    "unknown host",
    "no address associated with hostname",
)
_TIMEOUT_TOKENS = (
    "timed out", "timeout", "timeouterror", "timedout",
    "read operation timed out", "connection timed out",
)
_TLS_TOKENS = (
    "ssl", "certificate", "handshake", "tlsv", "wrong version number",
    "certificate verify failed", "hostname mismatch",
)
_THROTTLE_TOKENS = (
    "429", "too many requests", "throttl", "rate limit", "rate-limit",
    "retry-after", "retry after", "slow down",
)
_CONNECTION_TOKENS = (
    "connection refused", "connection reset", "connection aborted",
    "connection closed", "network is unreachable", "host is unreachable",
    "no route to host", "remote end closed", "cannot assign requested",
    "winerror 10061", "winerror 10054", "winerror 10060", "broken pipe",
)
_PARSE_TOKENS = (
    "parse", "xml", "json", "not well-formed", "no element found",
    "mismatch", "unexpected token", "syntaxerror", "invalid control",
    "declaration", "expat", "encoding",
)
_HTTP_CODE_RE = re.compile(r"\b([45]\d\d)\b")


def _has(text, tokens):
    return any(tok in text for tok in tokens)


def classify_reason(error, outcome="failed"):
    """Map a free-text error string to one closed reason code.

    Pure and total: any input (including None, "" or a non-string) returns a
    code from REASON_CODES. `outcome` "skipped_budget" short-circuits to
    "budget" because a task the budget never started has no error at all.
    """
    if outcome == "skipped_budget":
        return "budget"
    text = (error if isinstance(error, str) else str(error or "")).lower()
    if not text.strip():
        return "other"
    if _has(text, _DNS_TOKENS):
        return "dns"
    if _has(text, _TIMEOUT_TOKENS):
        return "timeout"
    if _has(text, _TLS_TOKENS):
        return "tls"
    if _has(text, _THROTTLE_TOKENS):
        return "throttled"
    code = _HTTP_CODE_RE.search(text)
    if code is not None:
        return "http_4xx" if code.group(1).startswith("4") else "http_5xx"
    if _has(text, _CONNECTION_TOKENS):
        return "connection"
    if _has(text, _PARSE_TOKENS):
        return "parse"
    return "other"


def canonical_host(host):
    """Host in a form safe to compare against config critical_hosts.

    Lowercased, trailing dot and port stripped, leading "www." removed. Only
    ever used for MATCHING -- the emitted `host`/`hosts_down`/
    `reachable_hosts` values keep the host exactly as the collector reported it.
    """
    h = (host if isinstance(host, str) else "").strip().lower().rstrip(".")
    if "@" in h:
        h = h.rsplit("@", 1)[1]
    if h.startswith("["):                      # IPv6 literal
        return h.split("]", 1)[0] + "]" if "]" in h else h
    if ":" in h:
        h = h.split(":", 1)[0]
    if h.startswith("www."):
        h = h[4:]
    return h


def reachable_hosts_set(source_results):
    """Canonical hosts with at least one REACHABLE task. Pure; never raises.

    Reachability is {"ok","empty"}: an `empty` row is a successful fetch that
    produced nothing, and treating that as unreachable would make the
    anti-fabrication gate delete the host's real stories.
    """
    hosts = set()
    try:
        if not isinstance(source_results, list):
            return hosts
        for raw in source_results:
            if not isinstance(raw, dict):
                continue
            if raw.get("outcome") not in REACHABLE_OUTCOMES:
                continue
            host = raw.get("host")
            if isinstance(host, str) and host.strip():
                ch = canonical_host(host)
                if ch:
                    hosts.add(ch)
    except Exception as exc:
        # Permissive structural fallback (deliberate): make it visible.
        log.warning("source_health: reachable_hosts_set structural fallback "
                    "(%s); returning %d hosts seen so far", exc, len(hosts))
        return hosts
    return hosts


def unreachable_hosts(source_results):
    """Canonical hosts that are UNREACHABLE: >=1 failed task and ZERO ok tasks.

    This is the A5 anti-fabrication gate input. The ok-shield is the whole
    point: a host with one failed task and one successful task is still a
    working source, so a sibling failure must NOT delete its real stories.
    Only a host that failed every time it was asked is treated as dark.

    Per-TASK matching is NOT determinable from these fields, deliberately:
    `source_results` carries no candidate URLs, and a gnews task's `url` is the
    Google News RSS *search endpoint* while its candidates are third-party
    article links, so "which task produced this story" cannot be recovered from
    a story's source_url. Only for notice tasks does host ~= task. So this is a
    per-HOST decision with a reachability shield, not a per-task one.

    Safety direction: hosts that were never attempted are never in the set, and
    "skipped_budget" is NOT "failed" -- an unstarted task proves nothing. So an
    incomplete or legacy source_results can never empty the digest.
    """
    failed, reachable = set(), set()
    try:
        if not isinstance(source_results, list):
            return failed
        for raw in source_results:
            if not isinstance(raw, dict):
                continue
            host = raw.get("host")
            if not isinstance(host, str) or not host.strip():
                continue
            ch = canonical_host(host)
            if not ch:
                continue
            outcome = raw.get("outcome")
            if outcome == "failed":
                failed.add(ch)
            elif outcome in REACHABLE_OUTCOMES:
                # "empty" SHIELDS the host exactly like "ok" does. One
                # zero-yield-but-successful sibling task is enough to prove the
                # host answers, so a sibling failure must not delete its real
                # stories. This is the line that keeps `empty` additive.
                reachable.add(ch)
    except Exception as exc:
        # Permissive structural fallback (deliberate): make it visible.
        log.warning("source_health: unreachable_hosts structural fallback "
                    "(%s); returning empty set (no host declared dark)", exc)
        return set()
    return failed - reachable


def failed_hosts(source_results):
    """Canonical hosts with at least one "failed" task (no ok-shield).

    Kept for diagnosis only: it answers "which hosts had any failure this run",
    which is what an operator wants when reading the log. The anti-fabrication
    gate must use unreachable_hosts() instead, so a healthy sibling task
    protects its host's real stories.
    """
    hosts = set()
    try:
        if not isinstance(source_results, list):
            return hosts
        for raw in source_results:
            if not isinstance(raw, dict):
                continue
            if raw.get("outcome") != "failed":
                continue
            host = raw.get("host")
            if isinstance(host, str) and host.strip():
                ch = canonical_host(host)
                if ch:
                    hosts.add(ch)
    except Exception as exc:
        # Permissive structural fallback (deliberate): make it visible.
        log.warning("source_health: failed_hosts structural fallback (%s); "
                    "returning %d hosts seen so far", exc, len(hosts))
        return hosts
    return hosts


def empty_source_health(now_npt=""):
    """The all-zero health skeleton (state "unknown"). Never raises.

    Carries every v2 field at its zero value, so a consumer can read
    `empty_hosts`/`items_found` unconditionally without a hasattr dance.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "unknown",
        "checked_at_npt": now_npt if isinstance(now_npt, str) else "",
        "critical": {"total": 0, "ok": 0, "empty": 0, "failed": 0, "skipped": 0,
                     "hosts_down": [], "empty_hosts": [], "reasons": []},
        "all": {"total": 0, "ok": 0, "empty": 0, "failed": 0, "skipped": 0,
                "items_found": 0},
        "by_category": {},
        "failed_names": [],
        "empty_names": [],
        "reachable_hosts": [],
    }


def _row_from_entry(raw):
    """Normalize one source_results entry into a counting row.

    Tolerates every field being missing or of the wrong type. A missing/!str
    `outcome` is treated as `skipped_budget` and contributes the "malformed"
    reason code instead of "budget" -- a corrupt record is not evidence of an
    outage, it is evidence of a bug.
    """
    if not isinstance(raw, dict):
        # MUST carry every key the counter reads, or the caller raises on a
        # hostile row. items_found is None (UNKNOWN), never 0.
        return {"name": "", "kind": "", "category": "", "host": "", "url": "",
                "outcome": "skipped_budget", "reason": "malformed", "ok": False,
                "reachable": False, "empty": False, "items_found": None}

    outcome = raw.get("outcome")
    if not isinstance(outcome, str) or outcome not in VALID_OUTCOMES:
        outcome = "skipped_budget"
        reason = "malformed"
    elif outcome in ("ok", "empty"):
        reason = ""
    elif outcome == "skipped_budget":
        reason = "budget"
    else:
        reason = classify_reason(raw.get("error"), "failed")

    def _str(field):
        v = raw.get(field)
        return v.strip() if isinstance(v, str) else ""

    return {
        "name": _str("name"),
        "kind": _str("kind"),
        "category": _str("category"),
        "host": _str("host"),
        "url": _str("url"),
        "outcome": outcome,
        "reason": reason,
        "ok": outcome == "ok",
        "reachable": outcome in REACHABLE_OUTCOMES,
        "empty": outcome == "empty",
        # MISSING `items_found` MEANS UNKNOWN, never zero. A v1 collector wrote
        # no such field; treating absence as 0 would manufacture an `empty` out
        # of a legacy record and could turn a healthy run into "empty". It must
        # stay None so the sum below skips it and no host is declared empty.
        "items_found": _items_found(raw),
    }


def _items_found(raw):
    """`items_found` as a non-negative int, or None when UNKNOWN.

    None (not 0) is the whole point: absent, null, negative, bool and junk all
    mean "this record cannot answer the question", and an unknown must never be
    laundered into a zero-yield claim.
    """
    v = raw.get("items_found")
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str):
        text = v.strip()
        if text.isdigit():
            return int(text)
    return None


def _compute(source_results, critical_hosts, now_npt, min_items_per_source=1):
    checked_at = now_npt if isinstance(now_npt, str) else ""

    # (1) source_results absent / unusable -> unknown, zeroed, no timestamp.
    if not isinstance(source_results, list) or not source_results:
        return empty_source_health("")

    crit_hosts = set()
    if isinstance(critical_hosts, (list, tuple, set, frozenset)):
        for h in critical_hosts:
            ch = canonical_host(h)
            if ch:
                crit_hosts.add(ch)

    # A reachable row below this many items is `empty`. Guarded to >= 1 so a
    # broken config cannot turn every productive source into a zero-yield claim.
    try:
        min_items = int(min_items_per_source)
    except (TypeError, ValueError):
        min_items = DEFAULT_MIN_ITEMS_PER_SOURCE
    if min_items < 1:
        min_items = 1

    all_c = {"total": 0, "ok": 0, "empty": 0, "failed": 0, "skipped": 0,
             "items_found": 0, "any_items_known": False}
    crit_c = {"total": 0, "ok": 0, "empty": 0, "failed": 0, "skipped": 0}
    crit_failed_hosts = set()
    crit_reasons = set()
    # Row 2b needs the PER-HOST count of failed critical rows whose reason is
    # not dns. `crit_reasons` above deliberately mixes failed AND skipped rows
    # (a budget-skipped critical host is a real gap, but it is not a failure
    # and must not masquerade as one), and it is a SET, so it cannot answer
    # "how many of this host's own tasks broke" -- which is the question that
    # separates a genuinely dark host from one flaky sweep.
    crit_nondns_failures = {}
    reachable = set()
    # Hosts with >=1 productive-`ok` row: the shield that keeps a single
    # zero-yield `site:` sweep from flipping the run state, because
    # news.google.com is critical and hosts 20+ tasks.
    productive_hosts = set()
    empty_hosts_raw = set()
    empty_crit_hosts_raw = set()
    failed_names = []
    empty_names = []
    cats = {}
    # outcome -> counter key (a malformed entry counts as UNKNOWN/skipped).
    # "empty" maps to "ok" DELIBERATELY: it is reachable, so every existing
    # counter that means "answered" keeps its meaning and every existing
    # assertion on `ok: N` still holds. The raw count lives in `.empty`.
    bucket_of = {"ok": "ok", "empty": "ok", "failed": "failed",
                 "skipped_budget": "skipped"}

    for raw in source_results:
        row = _row_from_entry(raw)
        all_c["total"] += 1
        bucket = bucket_of[row["outcome"]]
        all_c[bucket] += 1

        # `min_items_per_source` demotes a REACHABLE row to `empty` at the
        # counting layer, so a source that answered with junk still buckets
        # empty. It never touches failed/skipped rows, and it needs a KNOWN
        # items_found: unknown yield cannot be judged against a floor.
        counted_empty = row["empty"]
        if not counted_empty and row["reachable"]:
            found = row["items_found"]
            if found is not None and found < min_items:
                counted_empty = True
        if counted_empty:
            all_c["empty"] += 1
        if row["items_found"] is not None:
            all_c["items_found"] += row["items_found"]
            all_c["any_items_known"] = True

        is_critical = bool(row["host"]) and canonical_host(row["host"]) in crit_hosts
        if is_critical:
            crit_c["total"] += 1
            crit_c[bucket] += 1
            if counted_empty:
                crit_c["empty"] += 1
            if row["outcome"] == "failed" and row["host"]:
                crit_failed_hosts.add(row["host"])
                if row["reason"] and row["reason"] not in DNS_FAILURE_REASONS:
                    ch = canonical_host(row["host"])
                    crit_nondns_failures[ch] = crit_nondns_failures.get(ch, 0) + 1
            if not row["ok"] and row["reason"]:
                # WHY reasons covers failed AND skipped while hosts_down is
                # failed-only: a budget-skipped critical host is a real gap a
                # reader must be told about, but it is NOT proof the host is
                # down. These two fields are deliberately different, do not
                # "simplify" them into each other.
                crit_reasons.add(row["reason"])

        if row["reachable"] and row["host"]:
            reachable.add(row["host"])
        if row["host"]:
            ch = canonical_host(row["host"])
            if counted_empty:
                empty_hosts_raw.add(ch)
                if is_critical:
                    empty_crit_hosts_raw.add(ch)
            elif row["outcome"] == "ok" and row["host"]:
                # Productive means `ok` with a KNOWN yield. An `ok` row whose
                # items_found is unknown is NOT treated as productive proof for
                # the empty shield, because doing so would let a v1 record
                # (no items_found at all) mask a real zero-yield host.
                found = row["items_found"]
                if found is not None and found >= min_items:
                    productive_hosts.add(ch)
        if not row["ok"] and not row["empty"]:
            # `empty` is NOT a failure: the fetch succeeded. Listing it here
            # would say a source failed when it answered honestly with nothing.
            failed_names.append(row["name"])
        if counted_empty:
            empty_names.append(row["name"])

        cat = row["category"]
        if cat:
            # WHY the "" category is omitted: the weather task has no
            # domain_category, and a "" key would render as a blank section in
            # the digest. A category with no entries is already excluded by
            # construction; weather simply is not a category.
            entry = cats.setdefault(cat, {"total": 0, "ok": 0, "empty": 0,
                                          "failed": 0, "skipped": 0,
                                          "items_found": 0,
                                          "any_items_known": False,
                                          "notice_urls": set(),
                                          "hosts": {}, "empty_hosts": set()})
            entry["total"] += 1
            entry[bucket] += 1
            if counted_empty:
                entry["empty"] += 1
            if row["items_found"] is not None:
                entry["items_found"] += row["items_found"]
                entry["any_items_known"] = True
            if row["kind"] == "notice" and row["url"]:
                entry["notice_urls"].add(row["url"])
            if row["host"]:
                ch = canonical_host(row["host"])
                # Per-host yield ledger for the category: a host counts as
                # "every reachable row was empty" only when it has no
                # productive row. This is what keeps one poisoned `site:` sweep
                # from emptying a whole category.
                led = entry["hosts"].setdefault(
                    ch, {"empty": 0, "productive": 0, "unreachable": 0})
                if row["reachable"] and counted_empty:
                    led["empty"] += 1
                elif row["reachable"]:
                    found = row["items_found"]
                    if found is not None and found >= min_items:
                        led["productive"] += 1
                else:
                    led["unreachable"] += 1

    # Host shield for `empty`, same shape as hosts_down: >=1 empty row AND zero
    # productive-`ok` row. `news.google.com` is critical and hosts 20+ tasks, so
    # ONE zero-yield `site:` sweep can never flip the run state.
    empty_hosts = empty_hosts_raw - productive_hosts
    crit_empty_hosts = empty_crit_hosts_raw - productive_hosts

    by_category = {}
    for cat, entry in cats.items():
        urls = sorted(entry["notice_urls"])
        cat_empty = sorted(
            ch for ch, led in entry["hosts"].items()
            if led["empty"] > 0 and led["productive"] == 0)
        by_category[cat] = {
            "total": entry["total"],
            "ok": entry["ok"],
            "empty": entry["empty"],
            "failed": entry["failed"],
            "skipped": entry["skipped"],
            "items_found": entry["items_found"] if entry["any_items_known"] else 0,
            "empty_hosts": cat_empty,
            "urls": urls[:MAX_CATEGORY_URLS] if urls else [CATEGORY_URL_FALLBACK],
        }

    # WHY the ok-shield: "hosts_down" means the host was entirely DARK, the
    # same notion the anti-fabrication drop gate uses. A host with one failed
    # task and one successful task is still a working source, and calling it
    # "unreachable" in the banner would be a small lie -- the exact class of
    # lie this signal exists to stop. The individual failed task is still
    # visible in all.failed, failed_names and critical.reasons.
    hosts_down = crit_failed_hosts - reachable

    # Ordered decision table, first match wins.
    #
    # Row 3's SECOND clause is the safety net for a missing/misconfigured
    # `critical_hosts` (nothing currently warns about that): if EVERY reachable
    # source produced zero items, that is garbage, never a quiet news day.
    if all_c["ok"] == 0 and all_c["failed"] == 0:
        state = "unknown"
    elif hosts_down:
        # FROZEN: the trigger is `critical.hosts_down`, i.e. a critical host
        # that is entirely DARK. A critical host with one failed task and one
        # productive sibling is still working, and the run-level picture is
        # `partial` -- "some sources failed" is accurate and nothing is being
        # over-claimed. The per-task failure stays visible in critical.failed,
        # critical.reasons and failed_names.
        state = "degraded"
    elif crit_empty_hosts or (all_c["empty"] > 0 and all_c["empty"] == all_c["ok"]):
        state = "empty"
    elif (any(n >= NON_DNS_FAILURE_THRESHOLD
              for n in crit_nondns_failures.values())
          or (crit_c["empty"] > 0 and crit_c["failed"] > 0)):
        # ROW 2b (owner ruling), evaluated AFTER `empty` and BEFORE `partial`.
        #
        # THE EXACT CONDITION, in two clauses:
        #   (a) some SINGLE critical host has >= 2 of ITS OWN failed tasks whose
        #       reason is not dns (http_4xx, http_5xx, tls, connection,
        #       throttled, parse) -- the host answered, and it answered with
        #       errors, repeatedly, for reasons that are not a name-resolution
        #       blip. Counted PER HOST, so a flaky sweep on one of
        #       news.google.com's 20+ tasks stays quiet and a genuinely dark
        #       Google still shouts.
        #   (b) critical hosts returned nothing usable AT THE SAME TIME as
        #       critical hosts failed -- half the critical set is dead and the
        #       other half is mute, which no `hosts_down` shield catches
        #       because the mute hosts are reachable.
        #
        # WHAT IT IS FOR. Row 2 above only fires when a critical host is
        # entirely DARK, which under-alarms the commonest real outage: TU is
        # up, answers the notice page, and its endpoints 403 or 500. The host
        # is reachable, so `hosts_down` is empty and the run reads `partial` --
        # "some sources failed", which is accurate and under-claims a source
        # that is effectively gone.
        #
        # WHAT IT DELIBERATELY DOES NOT CATCH. One non-dns failure on one
        # task of one critical host, and any number of pure-dns failures
        # whose host still has a productive sibling: those are blips, and they
        # still read `partial` -- which is the honest word for them.
        #
        # WHY IT SITS AFTER `empty`. 2b is an ESCALATION OF `partial`, and
        # `empty` is a more specific and more actionable verdict for "the
        # sources answered with nothing usable": it is the state that tells
        # the owner the sources are UP and the news is not there. Putting 2b
        # ahead of it would relabel every actionable-empty critical host as
        # "some sources were unreachable" and hide the fact the owner has to
        # act on.
        state = "degraded"
    elif crit_c["skipped"] > 0 or all_c["failed"] > 0:
        state = "partial"
    else:
        state = "ok"

    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "checked_at_npt": checked_at,
        "critical": {
            "total": crit_c["total"],
            "ok": crit_c["ok"],
            "empty": crit_c["empty"],
            "failed": crit_c["failed"],
            "skipped": crit_c["skipped"],
            "hosts_down": sorted(hosts_down),
            "empty_hosts": sorted(crit_empty_hosts),
            "reasons": sorted(crit_reasons),
        },
        "all": {
            "total": all_c["total"],
            "ok": all_c["ok"],
            "empty": all_c["empty"],
            "failed": all_c["failed"],
            "skipped": all_c["skipped"],
            "items_found": all_c["items_found"] if all_c["any_items_known"] else 0,
        },
        "by_category": dict(sorted(by_category.items())),
        "failed_names": sorted(failed_names),
        "empty_names": sorted(empty_names),
        "reachable_hosts": sorted(reachable),
    }


def compute_source_health(source_results, critical_hosts, now_npt="",
                         min_items_per_source=1):
    """Derive the source_health object. Pure; never raises.

    Args:
        source_results: the collector's list (may be None / not a list).
        critical_hosts: iterable of host strings from config/collection.json
            (may be None / empty -> nothing counts as critical, and the caller
            is expected to warn).
        now_npt: 'YYYY-MM-DD HH:MM' Kathmandu wall time, passed in so this
            function never reads a clock.
        min_items_per_source: a reachable row with fewer items than this counts
            as `empty`. Coerced to >= 1; a junk value falls back to 1.
    """
    try:
        return _compute(source_results, critical_hosts, now_npt,
                        min_items_per_source)
    except Exception:
        # Total function: a surprise here must degrade to "unknown", never take
        # the digest down. Logged so the permissive path is not invisible.
        log.warning("source_health: compute_source_health degraded to "
                    "'unknown' (structural surprise)", exc_info=True)
        return empty_source_health("")
