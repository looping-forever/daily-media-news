#!/usr/bin/env python3
"""model_selector.py — DYNAMIC model discovery, ranking and health memory.

Stdlib only. Imported by breaking_monitor.py and ai_classifier.py.

WHY THIS FILE EXISTS
--------------------
The model ladder used to be a hardcoded ordered list, duplicated as an
embedded fallback tuple in every consumer. On 2026-10-13 it cost a real
mass-casualty alert: run 38373618708 tripped the deterministic gate on
"At least 139 migrants dead after boat capsized in Bay of Bengal", then
`opencode/big-pickle` timed out after 60s and `opencode/mimo-v2.5-free`
returned an unparseable verdict. Nothing was decided, nothing was sent.

Hardcoding also rots silently, and it rotted on this machine: on 2026-10-18
`opencode/mimo-v2.5-free`, then rung 2 of the ladder, was absent from the live
Zen catalogue. Neither direction of that fact is permanent -- a rung can be
replaced, renamed or withdrawn and come back, and a model that answers today
can be gone tomorrow -- so no comment here may claim that an ID exists, or that
one is dead for good. Live discovery decides that, run by run.

That catalogue is deliberately NOT copied into this file. A frozen list in a
docstring is the same brittleness in a comment, and it would be wrong within
a week; the reproducible measurement is the command:

    opencode models opencode --verbose

A dead ID in a hardcoded list is worse than no ID: the run spends real
wall-clock proving a model is gone, on exactly the days the provider is
sickest and time matters most.

WHAT IS ACTUALLY DISCOVERABLE (measured, not assumed)
-----------------------------------------------------
`opencode models [provider]` is a DOCUMENTED CLI command (see the official
CLI reference) that prints `provider/model` per line for every model the
configured providers offer. `--verbose` prints, per model, a JSON record
containing:

    cost.input / cost.output        0 for every Zen model  -> "free" is MEASURED
    status                          "active"              -> "not retired" is MEASURED
    capabilities.input.text/output.text
                                   text in, text out      -> "can do this job" is MEASURED
    release_date                    tiebreak for untried models

So none of the three things a caller needs -- is it free, is it still
offered, can it take text -- has to be guessed from a substring of its name.
Every gate below reads a field the CLI actually printed. A model whose
metadata cannot be parsed is NOT selectable: this file is deliberately more
conservative about which models it trusts, never less (see usable()).

WHY NOT A LIVE PROBE
--------------------
A probe costs a model call. This pipeline's entire complaint is wasted
wall-clock on a sick provider, so a probe that runs BEFORE every verdict
re-imports the problem it is meant to solve -- and a parallel probe of N
models costs N calls to avoid at most N-1. Ranking therefore uses signals
that are already free: (a) live catalogue metadata from discovery, and
(b) health measured on THIS pipeline's OWN previous runs. Both are read,
not bought.

Health memory and discovery are cached rather than re-fetched, because
`opencode models` costs ~4.6s of CLI cold start (measured 4.5-6.3s). Over
a 6-hourly cron that is ~0.03% of wall-clock; the cache is an optimisation,
never a correctness dependency -- every cache read has a live fallback
above it and a live path above that.

THE THREE FALLBACK TIERS, AND WHY NONE IS A HARDCODED LIST
----------------------------------------------------------
    1. live `opencode models <provider>` (correct, costs a CLI start)
    2. last good discovery, any age (stale but real; provenance says so)
    3. config/opencode.json `ladder` (the owner's own seed, single source)

A hardcoded fallback tuple in code is exactly the brittleness being removed,
so there is not one here. On total failure the caller decides nothing --
which is already the loud, correct behaviour for a sick provider.

SAFETY INVARIANT, and it is the one that matters
-----------------------------------------------
HEALTH MEMORY CAN ONLY REORDER. It can never veto. A health cooldown demotes
a model to the back of the attempt order; it never removes a model from it,
and rank_models always returns at least one candidate whenever any candidate
exists. If EVERY discovered model is health-cooling, the least-recently-failed
one is still returned and still tried. A system that decides what counts as
breaking news must never be silenced by its own memory of a bad afternoon.

Cooldown decay, stated once: health cooldown = COOLDOWN_UNIT_SEC x consecutive
failures, capped at COOLDOWN_MAX_SEC. COOLDOWN_UNIT_SEC is the breaking
monitor's own 6-hour cron period, so "one failure" means exactly "skip it
for the next scheduled run and try it again", which is a real retry rather
than a blacklist. A single success resets the streak to zero and clears the
cooldown immediately. A model that has never failed is never penalised.

THE ONE VETO, AND WHY IT IS NOT A CONTRADICTION
-----------------------------------------------
A second, HARD cooldown lives in its own file and DOES remove a model from
the attempt list. It exists because a `model_not_found` is not a bad
afternoon: the id is simply wrong, and on 2026-10-27 four such ids cost 4 of
12 digest attempts on every run before a workable model was reached. Health
memory cannot express that, because health memory may not veto.

Two things keep the veto honest, and both are load-bearing:
  * it is applied ONLY when a caller asks for it (`select(cooldown_path=...)`),
    so the breaking-news path keeps the reorder-only invariant above;
  * if every candidate is hard-cooled the selection returns EMPTY and says
    so (`info["all_cooled"]`), and the CLI exits non-zero naming the
    cooldown. It never silently resolves to "nothing to do".
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

#: `opencode models <provider>` costs ~4.6s of CLI cold start, measured. Cached
#: for one 6h window, the same span as the Worker's dedup TTL (21600s).
DISCOVERY_TTL_SEC = 6 * 3600

#: Hard wall clock on discovery. A provider listing that cannot be read in
#: 20s is not going to be read during a breaking-news decision.
DISCOVERY_TIMEOUT_SEC = 20

#: THE PIN, AND IT IS A TEST-ENVIRONMENT AFFORDANCE AND NOTHING ELSE.
#:
#: WHY IT EXISTS. Live discovery shells out to `opencode models <provider>
#: --verbose`. The CI runner is `ubuntu-24.04`, which has no `opencode` binary,
#: so discovery returned [] with `opencode_not_on_path`, the resolved ladder
#: collapsed to the 3-model config seed, and two tests that deliberately assert
#: the ladder came from the provider catalogue failed -- every day, producing a
#: red canary and an email. The tests were RIGHT and the runner was wrong: the
#: property under test is "selection is dynamic and is not the seed", and a
#: runner with no CLI cannot demonstrate it.
#:
#: WHY A PIN RATHER THAN THE TTL CACHE. The cache is already the right SHAPE --
#: `_write_cache` stores exactly the post-gate id list, which is what
#: state/model_cache.json contains. It is unusable here only because it carries
#: a 6-hour TTL, and CI has no warm cache. Pinning is the same content with the
#: clock taken out, which makes discovery deterministic under test instead of
#: dependent on when the runner last talked to the provider.
#:
#: THE CONSTRAINT THAT SHAPES THIS, AND IT IS NOT NEGOTIABLE. This selector's
#: own rule is that a BROKEN selector must degrade LOUDLY, never silently to the
#: seed -- there is a mutation proof pinning that. A pin that fired in
#: production would be exactly that silent degradation, wearing a fixture's
#: clothes: a genuinely broken selector in production would quietly resolve to
#: a frozen list and nobody would ever be told. So:
#:   * the pin is read ONLY when the CLI is genuinely absent, so a live runner
#:     always prefers the real catalogue and the pin is unreachable;
#:   * it is read ONLY from this environment variable, which NO committed
#:     workflow sets (pinned by tests/workflows/test_workflow_lint.py), so
#:     production cannot reach it even by accident;
#:   * a pin that is set but unreadable is NOT a silent seed -- it records why
#:     and keeps degrading loudly, exactly as an absent CLI does.
PIN_ENV = "MODEL_SELECTOR_PIN"


def read_pinned_models(info):
    """Pinned post-gate model ids, or None. Never raises.

    The file format is the cache format: `{"provider": ..., "models": [...]}`.
    """
    path = (os.environ.get(PIN_ENV) or "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        # LOUD, NOT SILENT: a pin that is set and cannot be read is a broken
        # TEST, and answering it with the seed would hide the very degradation
        # the selector is supposed to report.
        info["error"] = "pin_unreadable:%s" % type(exc).__name__
        return None
    ids = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(ids, list) or not ids:
        info["error"] = "pin_empty"
        return None
    return [str(i) for i in ids]

#: `provider/model`, one per line. Used both for the header line of a
#: --verbose record and to validate a plain listing.
MODEL_ID_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$")


def parse_verbose_models(text, provider):
    """(models, unparsed_ids) from `opencode models <p> --verbose` stdout.

    The CLI prints `provider/model` on its own line, then that model's JSON
    object indented underneath. Records are read as "a header line, then
    the balanced {...} beneath it".

    Returns the models whose metadata PARSED, and the ids whose metadata did
    not. The second list is not an error to hide -- it is reported, and those
    ids are not selectable, because an unverifiable model is exactly the one
    this file must refuse to trust.
    """
    models, unparsed = [], []
    pending_id = None
    buf, depth = [], 0

    def flush():
        nonlocal pending_id, buf, depth
        if pending_id is None:
            buf, depth = [], 0
            return
        try:
            rec = json.loads("".join(buf)) if buf else None
        except ValueError:
            rec = None
        if isinstance(rec, dict):
            models.append({"id": pending_id, "meta": rec})
        else:
            unparsed.append(pending_id)
        pending_id, buf, depth = None, [], 0

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = MODEL_ID_RE.match(line)
        if m and not line.startswith(("{", "}", '"', ",")):
            flush()
            if m.group(1) == provider:
                pending_id = line
            continue
        if pending_id is not None:
            buf.append(line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                flush()
    flush()
    return models, unparsed


def _zero(value):
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def usable(model):
    """(bool, why) -- may this discovered model be selected for real work?

    Every clause reads a field the CLI actually printed. There is no
    substring test on a model NAME anywhere in this file: names are not
    evidence of anything.
    """
    meta = (model or {}).get("meta")
    if not isinstance(meta, dict):
        return False, "no_metadata"
    cost = meta.get("cost")
    if not isinstance(cost, dict) or not _zero(cost.get("input")) \
            or not _zero(cost.get("output")):
        return False, "not_free"
    status = meta.get("status")
    # EXPLICITLY ACTIVE, not "anything that is not called deprecated". A model
    # whose record carries no status is unverifiable, and an unverifiable model
    # is refused: this is the predicate that keeps a withdrawn ID out of the
    # walk, and it reads `status` -- a field the CLI printed -- never the name.
    if status != "active":
        return False, "status:%s" % (
            status if isinstance(status, str) and status else "missing")
    caps = meta.get("capabilities")
    if not isinstance(caps, dict):
        return False, "no_capabilities"
    cin, cout = caps.get("input"), caps.get("output")
    if not isinstance(cin, dict) or not cin.get("text"):
        return False, "no_text_input"
    if not isinstance(cout, dict) or not cout.get("text"):
        return False, "no_text_output"
    return True, "ok"


#: The only statuses that count as PROOF a configured model is gone. Read from a
#: record the catalogue actually printed for that exact id -- never from the id's
#: ABSENCE from a listing, which is not evidence of anything.
#:
#: Run 40280837914 is why this distinction is load-bearing. The runner has no
#: credentials, and `opencode models opencode --verbose` in that environment
#: returns a DIFFERENT, smaller catalogue than the same binary on an
#: authenticated machine: seven ids, of which four are not served at all -- the
#: three muse/ling ids pinned in tests/breaking/test_dynamic_model_ladder.py and
#: mimo-v2.5-free -- while the fifth is an id this repo had already banned as
#: dead a day earlier. A listing that advertises a model the project knows is
#: gone cannot be treated as evidence that an absent one is gone. Measured here
#: on 2026-10-26 against `opencode-ai@1.18.31` (the pin): the cold home reports
#: 7 models and omits five of the six configured rungs, the warm one reports 9
#: and includes every rung. The name of the stale rung is built at run time in
#: the test rather than written here, because BANNED_RE bans the literal.
#: The old `[m for m in seed if m not in pool]` read that gap as "gone" and
#: discarded five measured rungs, so production walked seven unmeasured ids and
#: succeeded by luck. A listing can be INCOMPLETE; a record saying `deprecated`
#: cannot mean anything else.
GONE_STATUSES = ("deprecated", "retired", "deleted", "removed")


def _release_ordinal(meta):
    """Sortable proxy for "newer", for tiebreaks among never-tried models.

    Deliberately LAST in the ranking. A release date is a rough proxy for
    capability and nothing more, so it only ever separates two models that
    health memory has never distinguished -- it is never allowed to outrank a
    measured success.
    """
    raw = (meta or {}).get("release_date")
    if not isinstance(raw, str):
        return 0
    digits = re.sub(r"\D", "", raw)
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


#: One process, one CLI start. `opencode models` costs ~4.6s of cold start,
#: and a single process can resolve more than once (the breaking monitor
#: resolves per run; a test process resolves per case). Memoising the LIVE
#: answer for the TTL window is an optimisation only -- a failure is never
#: memoised, so a broken CLI is retried rather than cached as "no models".
_MEMO = {}


def reset_discovery_memo():
    """Drop the process-level discovery memo.

    Exists for tests that inject a fake `opencode models` reader: without it a
    real discovery earlier in the same process answers for them, and a test
    about parsing can pass without the parser ever running.
    """
    _MEMO.clear()


def discover(provider, cache_path=None, ttl_sec=DISCOVERY_TTL_SEC,
             timeout_sec=DISCOVERY_TIMEOUT_SEC, run_cmd=None, now=None):
    """(model_ids, info) — live free models for `provider`, best tier first.

    `info` carries the provenance a log row needs to be honest about WHICH
    tier answered: {"source": live|memo|cache|none, "unparsed": [...],
    "rejected": {id: reason}, "elapsed_sec": float}.
    """
    now = time.time() if now is None else now
    info = {"source": "none", "unparsed": [], "rejected": {}, "release": {},
            "catalogue_status": {}, "elapsed_sec": 0.0}
    started = time.time()

    memo = _MEMO.get(provider)
    if memo and (now - memo["at"]) < ttl_sec:
        info.update(memo["info"])
        info["source"] = "memo"
        info["elapsed_sec"] = time.time() - started
        return list(memo["ids"]), info

    ids = _discover_live(provider, timeout_sec, run_cmd, info)
    if ids:
        if cache_path:
            _write_cache(cache_path, provider, ids, started)
        _MEMO[provider] = {"at": started, "ids": list(ids),
                           "info": {"unparsed": list(info["unparsed"]),
                                    "rejected": dict(info["rejected"]),
                                    "release": dict(info.get("release") or {}),
                                    "catalogue_status": dict(
                                        info.get("catalogue_status") or {})}}
        # A pinned answer stays labelled `pin`. Reporting it as `live` would
        # tell a log reader the provider catalogue answered when nothing asked
        # it -- the same class of dishonesty this selector exists to avoid, one
        # level down.
        if info.get("source") != "pin":
            info["source"] = "live"
        info["elapsed_sec"] = time.time() - started
        return ids, info

    cached = _read_cache(cache_path, provider, now, ttl_sec)
    if cached:
        info["source"] = "cache"
        info["elapsed_sec"] = time.time() - started
        return list(cached), info

    info["source"] = "none"
    info["elapsed_sec"] = time.time() - started
    return [], info


def _discover_live(provider, timeout_sec, run_cmd, info):
    """Live listing, or [] with `info` explaining why there is none."""
    if run_cmd is None:
        exe = shutil.which("opencode")
        if not exe:
            # A live CLI always wins: the pin is unreachable when the real
            # catalogue can be asked, so this can never override production
            # discovery. See PIN_ENV for why it exists at all.
            pinned = read_pinned_models(info)
            if pinned:
                info["source"] = "pin"
                info["pinned_from"] = os.environ.get(PIN_ENV)
                return pinned
            info["error"] = "opencode_not_on_path"
            return []

        def run_cmd(argv, timeout):  # noqa: E306 - tiny injected seam
            return subprocess.run([exe] + list(argv), capture_output=True,
                                  text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")

    try:
        proc = run_cmd(["models", provider, "--verbose"], timeout_sec)
    except subprocess.TimeoutExpired:
        info["error"] = "discovery_timeout"
        return []
    except (OSError, ValueError) as exc:
        info["error"] = "discovery_error:%s" % type(exc).__name__
        return []
    # STDOUT ONLY. The npm `opencode.ps1` shim writes its banner to stderr;
        # reading the merged streams would feed ASCII art into the parser.
    out = getattr(proc, "stdout", "") or ""
    if getattr(proc, "returncode", 0) not in (0, None):
        info["error"] = "discovery_exit:%s" % proc.returncode
        return []

    models, unparsed = parse_verbose_models(out, provider)
    info["unparsed"] = list(unparsed)
    ids = []
    status_by_id = info.setdefault("catalogue_status", {})
    for model in models:
        meta = model.get("meta") or {}
        status_by_id[model["id"]] = meta.get("status")
        ok, why = usable(model)
        if ok:
            ids.append(model["id"])
            # Kept so the ranking can break a tie between two models health
            # memory has never distinguished. Metadata that discovery throws
            # away here is metadata the selector cannot use later.
            info.setdefault("release", {})[model["id"]] = _release_ordinal(
                model["meta"])
        else:
            info["rejected"][model["id"]] = why
    return ids


#: Cache shape marker. A cache written BEFORE the explicit-active gate cannot
#: prove its ids were active when they were stored -- and the 2026-10-25 run
#: showed four dead IDs reaching the walk. An ungated cache is therefore not
#: trusted (tier falls to the config seed, which is visible in `source`), while
#: every cache this version writes carries the marker and is re-validated here.
CACHE_GATE = "active-agent-capable-v1"


def _read_cache(cache_path, provider, now, ttl_sec):
    if not cache_path:
        return []
    try:
        with open(cache_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict) or doc.get("provider") != provider:
        return []
    if doc.get("gate") != CACHE_GATE:
        # A PRE-GATE CACHE IS NOT EVIDENCE. Silently accepting it is how a dead
        # ID survives in a file nobody re-inspects.
        return []
    ids = doc.get("models")
    if not isinstance(ids, list) or not ids \
            or not all(isinstance(m, str) and m for m in ids):
        return []
    # An EXPIRED cache is still better than nothing -- a real listing from
    # yesterday beats a hardcoded guess -- and `source: cache` says so.
    return ids


def _cache_stamp(cache_path, provider, now):
    """(fetched_at, age_sec) of the discovery cache, or (None, None).

    Provenance only: it says whether the pool came from THIS run's live call or
    from a file an earlier run wrote. Every error path returns (None, None) --
    a provenance field that cannot be read must never be able to fail a run.
    """
    try:
        with open(cache_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, TypeError, ValueError):
        return None, None
    if not isinstance(doc, dict) or doc.get("provider") != provider:
        return None, None
    try:
        fetched_at = float(doc.get("fetched_at"))
    except (TypeError, ValueError):
        return None, None
    return fetched_at, max(0.0, float(now) - fetched_at)


def _write_cache(cache_path, provider, ids, fetched_at):
    try:
        parent = os.path.dirname(os.path.abspath(cache_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = "%s.%d.tmp" % (cache_path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"provider": provider, "gate": CACHE_GATE,
                       "fetched_at": fetched_at, "models": list(ids)},
                      fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, cache_path)
    except OSError:
        pass   # a cache that cannot be written is an optimisation lost


# ---------------------------------------------------------------------------
# Health memory
# ---------------------------------------------------------------------------

HEALTH_SCHEMA_VERSION = 1

#: == a 6h window, the Worker's dedup TTL (21600s). NOT the workflow cron
#: period -- that is `20 1 * * *` (daily) since 2026-10-19, so one failure now
#: skips a 6h window rather than exactly one scheduled run.
COOLDOWN_UNIT_SEC = 6 * 3600
COOLDOWN_MAX_SEC = 24 * 3600

#: Bounded growth. The health file is state, not a log.
HEALTH_MAX_MODELS = 64

FAILURE_KINDS = ("timeout", "transport", "garbage")

#: The classifier's vocabulary is wider than health memory's, and this is the
#: bridge. A missing model, a pool-wide quota and a 5xx are all "this attempt
#: produced no verdict", which is exactly what `transport` already meant, so
#: health memory keeps behaving as it did before those classes existed.
HEALTH_KIND_OF = {
    "model_not_found": "transport",
    "provider_outage": "transport",
    "quota_or_rate_limited": "transport",
    "slow_or_stalled": "timeout",
    "other": "transport",
    "auth": "transport",
    "payment_required": "transport",
}


def _empty_health():
    return {"schema_version": HEALTH_SCHEMA_VERSION, "models": {}}


def load_health(path):
    """Best-effort read. Missing or corrupt -> empty; never an exception.

    Health memory is an optimisation. A broken memory file must never be
    able to stop a breaking-news run.
    """
    if not path:
        return _empty_health()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return _empty_health()
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict):
        return _empty_health()
    doc.setdefault("schema_version", HEALTH_SCHEMA_VERSION)
    return doc


def save_health(path, doc):
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        doc["schema_version"] = HEALTH_SCHEMA_VERSION
        doc["models"] = _trim(doc.get("models") or {})
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _trim(models):
    if len(models) <= HEALTH_MAX_MODELS:
        return models
    ordered = sorted(models.items(),
                     key=lambda kv: str(kv[1].get("last_fail_at")
                                        if isinstance(kv[1], dict) else ""))
    return dict(ordered[-HEALTH_MAX_MODELS:])


def _entry(doc, model):
    models = doc.setdefault("models", {})
    row = models.get(model)
    if not isinstance(row, dict):
        row = {}
        models[model] = row
    row.setdefault("success", 0)
    row.setdefault("timeout", 0)
    row.setdefault("transport", 0)
    row.setdefault("garbage", 0)
    row.setdefault("consecutive_failures", 0)
    return row


def record_success(doc, model, at=None):
    """A verdict came back. Streak -> 0, cooldown cleared, count up.

    Clearing on the FIRST success is what makes this a decay rather than a
    blacklist: one good run returns the model to the front of the order.
    """
    row = _entry(doc, model)
    row["success"] = int(row.get("success") or 0) + 1
    row["consecutive_failures"] = 0
    row.pop("cooldown_until", None)
    row["last_ok_at"] = time.time() if at is None else float(at)
    return row


def record_failure(doc, model, kind="transport", at=None):
    """A timeout, a dead transport, or an unusable answer. All three count.

    `garbage` counts here deliberately. The 2026-10-13 failure was not one
    dead model but a TIMEOUT followed by an UNPARSEABLE verdict, and a model
    that answers unusably is exactly as useless to this pipeline as one that
    never answers -- so it earns the same cooldown.
    """
    if kind not in FAILURE_KINDS:
        kind = HEALTH_KIND_OF.get(kind, "transport")
    at = time.time() if at is None else float(at)
    row = _entry(doc, model)
    row[kind] = int(row.get(kind) or 0) + 1
    row["consecutive_failures"] = int(row.get("consecutive_failures") or 0) + 1
    row["last_failure"] = kind
    row["last_fail_at"] = at
    streak = int(row["consecutive_failures"])
    # ABSOLUTE, not a duration: on_cooldown() compares this against an
    # absolute `now`. Storing the bare duration here silently disabled the
    # whole feature — every wall-clock now is ~1.7e9 and every cooldown is
    # ~21600, so nothing ever looked like it was cooling down.
    row["cooldown_until"] = at + min(COOLDOWN_MAX_SEC,
                                     COOLDOWN_UNIT_SEC * streak)
    return row


def on_cooldown(row, now):
    """True while the cooldown window is still open."""
    if not isinstance(row, dict):
        return False
    until = row.get("cooldown_until")
    try:
        return float(until) > float(now)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# The HARD cooldown -- the one veto, in its own file
# ---------------------------------------------------------------------------
# Measured 2026-10-27: four ids the provider's catalogue still advertised were
# ranked into the digest walk and cost 4 of its 12 attempts -- three answered
# `model_not_found` (the id does not exist) and one answered `Model is
# unavailable` (the id exists and the endpoint withdrew it). Ranking them again
# next run is not a health question, it is a fact: the id is not servable.
#
# WHY A SEPARATE FILE RATHER THAN MORE FIELDS IN model_health.json. The health
# file is gitignored and rebuilt from nothing on a clean runner, and it is a
# RANKING input whose invariant is "never veto". This is the opposite: a small,
# committed, explicit list of ids that must not be tried, which has to survive
# the ephemeral runner to be worth anything. Mixing it into a reorder-only file
# would break that file's invariant and lose the veto at the same time.

COOLDOWN_FILENAME = "model_cooldown.json"
COOLDOWN_SCHEMA_VERSION = 1

#: How long each cooled-down class stays cooled. Two very different numbers, on
#: purpose:
#:   model_not_found -- the id is wrong until somebody fixes the list, so a day.
#:   provider_outage -- genuinely transient, so 15 minutes AND only after the
#:                      second hit. One 5xx may recover; that is why this is a
#:                      count and not a marker.
#: `quota_or_rate_limited` is deliberately ABSENT. The whole free pool shares
#: that limit, so cooling models down would remove the pool and end the run.
COOLDOWN_SEC = {
    "model_not_found": 24 * 3600,
    "provider_outage": 15 * 60,
}

#: A provider_outage is evidence only when it repeats.
OUTAGE_HITS_BEFORE_COOLDOWN = 2

#: An outage hit older than this stops counting toward a pair, so two unrelated
#: single 5xx a month apart can never add up to a cooldown.
COOLDOWN_HIT_TTL_SEC = 24 * 3600

#: Bounded growth, like HEALTH_MAX_MODELS: this file is state, not a log.
COOLDOWN_MAX_MODELS = 64


def cooldown_path_for(health_path):
    """The cooldown file that belongs beside `health_path`, or None.

    Both are per-model state for the same walk, so they live in one directory
    and neither caller has to learn a second path.
    """
    if not health_path:
        return None
    return os.path.join(os.path.dirname(os.path.abspath(health_path)),
                        COOLDOWN_FILENAME)


def _iso(at):
    """ISO-8601 UTC stamp for an epoch second. Absolute, never a duration."""
    return datetime.fromtimestamp(float(at), timezone.utc).isoformat()


def _epoch(stamp):
    """Epoch seconds from an ISO-8601 stamp, or 0.0 when it cannot be read."""
    if isinstance(stamp, (int, float)):
        return float(stamp)
    try:
        moment = datetime.fromisoformat(str(stamp).strip().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


# ---------------------------------------------------------------------------
# The SHARED POOL cooldown -- one bucket, not one model
# ---------------------------------------------------------------------------
# Measured 2026-10-27: six different model ids refused inside 16 s, each refusal
# 312 ms after `message=stream` with `tokens.input=0` -- i.e. BEFORE any
# generation. The provider throttles the egress bucket, not the model, so a
# per-model cooldown is the wrong shape: it would have to cool every id at once
# (which is what "remove the pool" looks like) and it still would not tell the
# next workflow anything. So the refusal is recorded ONCE, as a pool fact, in the
# same committed file as the per-model cooldowns -- `state/model_cooldown.json` --
# under `pool` rather than `models`. One state file, one save path, one commit.
#
# WHY THE DIGEST IS TREATED DIFFERENTLY FROM EVERYTHING ELSE. The digest slot is
# the product; a breaking sweep, a canary or a watchdog is not. So:
#   * a NON-digest caller yields: it will not spend the pool during the digest's
#     own slot window, and it never extends past what the provider asked for;
#   * the digest may IGNORE a cooldown it has watched for longer than
#     POOL_DIGEST_GRACE_SEC. That trade is stated out loud at the call site: the
#     slot is the one thing worth re-testing a stale wall for, and a wall that
#     never expires would otherwise lose every future edition silently.
POOL_KEY = "pool"

#: Used when the provider gave no Retry-After. Long enough to cover the measured
#: ~43 s wall several times over, short enough that a stale marker cannot park
#: the pool for an hour.
POOL_DEFAULT_SEC = 15 * 60

#: A Retry-After is an OBSERVED reset (measured: it lands on UTC midnight), and
#: this ceiling exists only so a malformed one cannot park the pool indefinitely.
POOL_MAX_SEC = 24 * 3600

#: How long the digest may sit behind a live cooldown before it re-tests the
#: pool anyway. Ten minutes: one `daily-news` ladder backoff, so a wall that is
#: clearing is given the chance to clear first.
POOL_DIGEST_GRACE_SEC = 10 * 60

#: The digest slots, read from `deploy/schedule.json` (`dispatcher_crons.
#: digest_slots`) so a slot move is one edit, and this constant is only the
#: fallback for a tree that cannot read that file.
DIGEST_SLOTS_FALLBACK = ((1, 35), (13, 35))
#: A slot's protected window: a little lead for the prebuild, an hour of trail
#: for the walk, validate, render and delivery. Numbers, not prose: the window
#: only ever makes a NON-digest caller stricter, so a generous window costs a
#: non-digest call and a tight one loses a digest.
DIGEST_WINDOW_LEAD_SEC = 10 * 60
DIGEST_WINDOW_TRAIL_SEC = 60 * 60

_AUTO = object()

#: `Retry-After: 300`, `retry-after=300`, `retry after 300s`. Seconds only: an
#: HTTP-date form would need a clock agreement this repo cannot verify, and the
#: measured value is a seconds count.
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after\W{0,3}(\d{1,6})", re.I)


def retry_after_from_text(text):
    """Seconds from the provider's Retry-After in `text`, or None if absent."""
    found = _RETRY_AFTER_RE.search(text or "")
    if not found:
        return None
    try:
        return int(found.group(1))
    except (TypeError, ValueError):
        return None


def pool_row(doc):
    """The pool entry, or None. Never raises on a malformed document."""
    row = (doc or {}).get(POOL_KEY) if isinstance(doc, dict) else None
    return row if isinstance(row, dict) else None


def record_pool_refusal(doc, at=None, retry_after=None, reason="quota_or_rate_limited",
                        source=""):
    """Record ONE shared-pool refusal. Returns the row that was written.

    The duration is the provider's own Retry-After when it gave one, because that
    is the OBSERVED reset and guessing past it is what turns a cooldown into a
    blacklist. Without one it is POOL_DEFAULT_SEC. Either way it is clamped, and
    an existing later `until` is never shortened by a shorter later refusal --
    two refusals ten seconds apart describe the same wall.
    """
    at = time.time() if at is None else float(at)
    observed = retry_after
    if retry_after is None:
        seconds = POOL_DEFAULT_SEC
    else:
        try:
            seconds = int(float(retry_after))
        except (TypeError, ValueError):
            seconds = POOL_DEFAULT_SEC
    seconds = max(60, min(seconds, POOL_MAX_SEC))
    until = at + seconds
    previous = pool_row(doc) or {}
    previous_until = _epoch(previous.get("until"))
    if previous_until > until:
        # A longer wall is already recorded. Keep it and keep its provenance.
        previous["last_hit"] = _iso(at)
        return previous
    row = {"until": _iso(until),
           "observed_at": _iso(at),
           "reason": reason,
           "seconds": seconds,
           "retry_after_sec": observed,
           "source": source}
    doc[POOL_KEY] = row
    return row


def prune_pool(doc, now=None):
    """Drop an EXPIRED pool row. Returns True when one was dropped.

    Called before every read, so "the cooldown expired" and "there is no
    cooldown" are the same state here -- an expired marker must not be able to
    block anything, which is the whole reason the digest slot can never be
    permanently barred by one.
    """
    now = time.time() if now is None else float(now)
    row = pool_row(doc)
    if row and _epoch(row.get("until")) <= now:
        del doc[POOL_KEY]
        return True
    return False


def digest_slots(path=None):
    """The digest slot times as ((hour, minute), ...), UTC.

    Read from `deploy/schedule.json` when it is reachable, because that file is
    what the Worker actually schedules; the constant is only a fallback.
    """
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "deploy", "schedule.json")
    slots = []
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        recorded = ((doc.get("dispatcher_crons") or {}).get("digest_slots")
                    if isinstance(doc, dict) else None) or {}
        for value in (recorded or {}).values():
            fields = str(value).split()
            if len(fields) == 5:
                slots.append((int(fields[1]), int(fields[0])))
    except (OSError, ValueError, TypeError):
        slots = []
    return tuple(sorted(slots)) or DIGEST_SLOTS_FALLBACK


def digest_window(now=None, slots=None):
    """(start, end) epoch of the digest slot window containing `now`, or None.

    UTC only, like every other clock in this repository. A window is
    slot - DIGEST_WINDOW_LEAD_SEC .. slot + DIGEST_WINDOW_TRAIL_SEC, and the
    three-day sweep is what makes a slot either side of midnight work without
    special-casing the date roll.
    """
    now = time.time() if now is None else float(now)
    today = datetime.fromtimestamp(now, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    for day in (-1, 0, 1):
        base = today + timedelta(days=day)
        for hour, minute in (slots if slots is not None else digest_slots()):
            try:
                slot = base.replace(hour=hour, minute=minute).timestamp()
            except ValueError:
                continue
            start = slot - DIGEST_WINDOW_LEAD_SEC
            end = slot + DIGEST_WINDOW_TRAIL_SEC
            if start <= now <= end:
                return (start, end)
    return None


def pool_block(doc, now=None, role="other", window=_AUTO):
    """Whether `role` may call a model now. Always a dict, never an exception.

    `blocked` is the verdict; `note` is the single line a caller logs, and it
    always names the cooldown AND its expiry, because a suppressed run that says
    only "skipped" is indistinguishable from a broken one.

    role="digest" and a cooldown watched for longer than POOL_DIGEST_GRACE_SEC ->
    NOT blocked, with the trade stated: the slot is exactly where a fresh pool is
    worth one attempt, and a stale wall must never lose the edition.
    role="other" inside a digest window -> the blocker is extended to the END of
    that window, so a breaking sweep cannot spend the pool minutes before the
    digest needs it. That is the reservation, and it is the only thing that
    actually changes a non-digest caller's behaviour.
    """
    now = time.time() if now is None else float(now)
    row = pool_row(doc)
    if not row:
        return {"blocked": False, "note": ""}
    until = _epoch(row.get("until"))
    if until <= now:
        return {"blocked": False, "note": ""}
    observed = _epoch(row.get("observed_at")) or until
    age = max(0, int(now - observed))
    reason = str(row.get("reason") or "quota_or_rate_limited")
    if role == "digest":
        if age >= POOL_DIGEST_GRACE_SEC:
            return {"blocked": False,
                    "until": _iso(until),
                    "pool_until": _iso(until),
                    "age_sec": age,
                    "reason": reason,
                    "note": ("pool cooldown is EXPIRED-ENOUGH TO IGNORE: the pool "
                             "refused %ds ago (%s until %s, %ds past the %ds "
                             "grace). Attempting anyway -- a fresh pool at this "
                             "slot is exactly what the digest needs, and a wall "
                             "that never expires would lose the edition."
                             % (age, reason, _iso(until), age,
                                POOL_DIGEST_GRACE_SEC))}
        return {"blocked": True,
                "until": _iso(until),
                "pool_until": _iso(until),
                "age_sec": age,
                "reason": reason,
                "note": ("pool cooldown: the shared pool refused %s %ds ago and "
                         "is refused until %s (the digest honours a wall younger "
                         "than %ds before re-testing it)"
                         % (reason, age, _iso(until), POOL_DIGEST_GRACE_SEC))}
    win = digest_window(now) if window is _AUTO else window
    effective = until
    note = ("pool cooldown: the shared pool refused %s and is refused until %s "
            "(%ds remaining)" % (reason, _iso(until), int(until - now)))
    if win and win[1] > effective:
        effective = win[1]
        note = ("pool cooldown %s, AND the digest slot window is open until %s: "
                "yielding the pool to the digest, which is the reservation -- a "
                "non-digest caller may not spend it before the edition is built"
                % (_iso(until), _iso(effective)))
    return {"blocked": True,
            "until": _iso(effective),
            "pool_until": _iso(until),
            "age_sec": age,
            "reason": reason,
            "digest_window": bool(win),
            "note": note}


def _empty_cooldown():
    return {"schema_version": COOLDOWN_SCHEMA_VERSION, "models": {}, POOL_KEY: {}}


def load_cooldown(path):
    """Best-effort read. Missing or corrupt -> empty; never an exception.

    A broken cooldown file must be able to lose a veto, never a run: the worst
    case is that the next run pays one wasted attempt per dead id again.
    """
    if not path:
        return _empty_cooldown()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return _empty_cooldown()
    if not isinstance(doc, dict):
        return _empty_cooldown()
    # A file that carries ONLY the pool marker is still worth reading: the pool
    # wall is the cheapest fact in this file, and dropping it because no model
    # happens to be cooling would silently un-arm the shared cooldown.
    if not isinstance(doc.get("models"), dict):
        if isinstance(doc.get(POOL_KEY), dict):
            doc["models"] = {}
        else:
            return _empty_cooldown()
    doc.setdefault("schema_version", COOLDOWN_SCHEMA_VERSION)
    return doc


def _has_content(doc):
    """True when this document still holds something that can refuse a model.

    WHY THIS EXISTS. Every row in here expires: a model veto lasts at most a day
    (`COOLDOWN_SEC`), a pool wall at most `POOL_MAX_SEC`, and the record and
    select paths both PRUNE before they save. So the steady state of a healthy
    repository is a document with nothing left in it -- and writing that out
    produced `{"schema_version": 1, "models": {}}`: a committed stub that refuses
    nothing, was stale the moment it was written, and failed `workflow_lint` on
    five days out of five. The rule that replaces it is one sentence: the
    committed file exists only while it can still veto something.
    """
    models = doc.get("models") if isinstance(doc, dict) else None
    if isinstance(models, dict) and models:
        return True
    return bool(pool_row(doc))


def save_cooldown(path, doc):
    """Atomic, best-effort write -- or REMOVAL when there is nothing left to say.

    Returns True when the file on disk now reflects `doc`: written, removed, or
    already absent.
    """
    if not path:
        return False
    if not _has_content(doc):
        # DELETE, NEVER WRITE AN EMPTY STUB.
        #
        # This is the D4 fix, and it is here rather than in the test on purpose:
        # the document that reaches this point has already been pruned, so "no
        # rows left" is a fact about the walk, not a guess. Removing the file is
        # what makes the next `git add` see a deletion and what keeps a dead
        # veto list out of the tree. A failed removal is not fatal: the commit
        # step warns and the next run tries again.
        try:
            os.remove(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        doc["schema_version"] = COOLDOWN_SCHEMA_VERSION
        doc["models"] = _trim_cooldown(doc.get("models") or {})
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _trim_cooldown(models):
    if len(models) <= COOLDOWN_MAX_MODELS:
        return models
    ordered = sorted(models.items(),
                     key=lambda kv: str(kv[1].get("last_hit")
                                        if isinstance(kv[1], dict) else ""))
    return dict(ordered[-COOLDOWN_MAX_MODELS:])


def prune_cooldown(doc, now=None):
    """Drop entries that can no longer affect selection; return the dropped ids.

    Two kinds of dead entry. One whose `until` has passed is expired. One with
    NO `until` is a single outage hit waiting for its pair, and it stops
    counting once it is older than COOLDOWN_HIT_TTL_SEC.
    """
    now = time.time() if now is None else float(now)
    models = doc.get("models")
    if not isinstance(models, dict):
        doc["models"] = {}
        return []
    dropped = []
    for model, row in list(models.items()):
        if not isinstance(row, dict):
            dropped.append(model)
            del models[model]
            continue
        if row.get("until"):
            alive = _epoch(row["until"]) > now
        else:
            alive = (now - _epoch(row.get("last_hit"))) <= COOLDOWN_HIT_TTL_SEC
        if not alive:
            dropped.append(model)
            del models[model]
    return dropped


def on_hard_cooldown(doc, model, now=None):
    """True while this model must not be attempted at all."""
    row = ((doc or {}).get("models") or {}).get(model)
    if not isinstance(row, dict) or not row.get("until"):
        return False
    return _epoch(row["until"]) > (time.time() if now is None else float(now))


def record_cooldown(doc, model, kind, at=None):
    """Cool `model` down after a `kind` failure. Returns the row, or None.

    Only COOLDOWN_SEC's two kinds are cooled. `provider_outage` records the hit
    but does not start a cooldown until OUTAGE_HITS_BEFORE_COOLDOWN, so a single
    5xx gets its chance. Everything else -- quota above all -- is ignored.
    """
    seconds = COOLDOWN_SEC.get(kind)
    if not seconds:
        return None
    at = time.time() if at is None else float(at)
    models = doc.setdefault("models", {})
    row = models.get(model)
    if not isinstance(row, dict):
        row = {}
        models[model] = row
    row["hits"] = int(row.get("hits") or 0) + 1
    row["last_hit"] = _iso(at)
    row["reason"] = kind
    if kind == "provider_outage" and row["hits"] < OUTAGE_HITS_BEFORE_COOLDOWN:
        return row
    row["until"] = _iso(at + seconds)
    return row


def clear_cooldown(doc, model):
    """Drop a model's entry. One success is proof the id works today."""
    models = doc.get("models")
    if isinstance(models, dict) and model in models:
        del models[model]
        return True
    return False


# ---------------------------------------------------------------------------
# The wall-clock walk budget (the digest agent stage)
# ---------------------------------------------------------------------------
# WHY A BUDGET AND NOT A COUNT. Run 39117668287 discovered ten free models and
# walked exactly three: two hit the 600s cap, the third exited 1, and the slot
# ended with no digest while seven models sat untried. A re-run minutes later
# succeeded on the first model in ~5 min. So the pool is UNCAPPED (`--all`) and
# the WALK is bounded by the clock instead, which is the only bound that adapts
# to how sick the provider is.

#: Per-attempt cap. Measured completion for this workload is 127.53s, and the
#: cap has to clear the WEDGED path too: an attempt that opens the model call
#: and then goes quiet costs `min(first_output + INTER_TOKEN, this)`. At 480s
#: the inter-token window (420s) still fires before the cap for every rung, so
#: the cap is only the backstop it is meant to be.
#:
#: RAISING THIS CUTS THE ATTEMPT COUNT, DELIBERATELY. The walk budget stays
#: 1800s, so 480s affords 3 attempts where 360s afforded 5. The per-run wall
#: clock is unchanged, which is why the Actions projection in
#: config/actions-budget.json (1776 of the 1800 ceiling) does not move: fewer,
#: longer attempts inside the same bounded job. Never raise the budget itself
#: to buy attempts back -- that is the line the ceiling is guarding.
WALK_PER_ATTEMPT_SEC = 480
#: Whole-walk cap. The agent step gets 35 min (2100s); 1800s leaves 5 min for
#: discovery, the agent-log tail and the step's own bookkeeping.
WALK_TOTAL_BUDGET_SEC = 1800
#: An attempt shorter than this is not worth starting, so the walk STOPS and
#: says so, rather than spending the tail on a doomed three-second call.
WALK_MIN_ATTEMPT_SEC = 90

#: The walk when the caller has established there is NOTHING to write.
#:
#: A run whose collection yielded no usable candidates still has to produce a
#: digest -- `prompts/daily-digest.md` FAILURE MODE requires `state/stories.json`
#: with research_status "limited" and an explanation in `notes`, and that same
#: section forbids answering an empty collection with supplemental
#: re-collection. That task is a file write, not research, so it cannot need the
#: full walk. Measured: nine October digest runs spent 1,168-1,936s each and
#: produced nothing, about 226 min of the 862 this workflow spent that month.
#: 300s bounds each such run to one attempt, and every run that DOES have
#: candidates to classify keeps the full 1800s -- the budget follows what the
#: run has to do, never the clock alone.
WALK_NO_WORK_TOTAL_BUDGET_SEC = 300


#: The no-progress watchdog, window ONE: silence BEFORE the first assistant
#: token. It is per model because one number cannot serve two models whose
#: measured time-to-first-token differs by a factor of nine. Once text has
#: begun, INTER_TOKEN_SEC (below) governs instead.
#:
#: Measured on 2026-10-26 against the REAL prompt (26.8KB prompt + 231KB /
#: 616-row candidates, ~65k tokens), serial, one model at a time:
#:
#:   deepseek-v4.1-flash-free   first token  14.59s, done 127.53s, 4 stories
#:   beam-free                  first token 137.63s, done 138.56s, 6 stories
#:   mimo-v2.6 / nemotron-3.5 / glm-5.3   no assistant text by 150s
#:   flint-40b                  hard failure from 12.6s (see REPEAT_OUTAGE_LIMIT)
#:
#: 60s is ~4x deepseek's measured first token, which is the same margin the
#: old global 90s had over the old measurement. But 60s for beam would make the
#: ONLY other model that has ever produced a digest permanently unreachable, so
#: beam gets 240s -- ~1.7x its measured 137.63s. A global window would be a
#: taste decision with a measured casualty; this is the measurement instead.
#:
#: AND THE OTHER HALF, so nobody reads a quiet attempt as a dead one: stdout
#: carries assistant TEXT only. The probe showed nemotron and glm alive and
#: streaming TOOL CALLS with no text at all, so "no output" here means "no
#: assistant text yet" -- these windows are the ONLY silence signal, and the
#: repeat breaker below is a different signal entirely.
NO_OUTPUT_BY_MODEL = {
    "opencode/deepseek-v4.1-flash-free": 60,   # 14.59s measured -> ~4x margin
    "opencode/beam-free": 240,                 # 137.63s measured -> ~1.7x margin
}
#: Every model the probe did not measure gets this. 60s is safe for them by
#: construction: each produced NO text at 150s, so nothing is being cut short.
NO_OUTPUT_DEFAULT_SEC = 60


def no_output_sec_for(model_id):
    """The FIRST-OUTPUT window for `model_id`, or the default for an unmeasured one."""
    return NO_OUTPUT_BY_MODEL.get(model_id, NO_OUTPUT_DEFAULT_SEC)


#: THE SECOND WINDOW, and the defect that forced it. Run 40286310047, 2026-10-26:
#:
#:     07:32:35 === Attempt 1 with opencode/deepseek-v4.1-flash-free (allow 360s)
#:     07:34:28 __WATCHDOG_NO_OUTPUT__ no output for 60s; attempt abandoned
#:     07:35:52 Agent wrote valid state/stories.json on attempt 2 (beam-free)
#:
#: deepseek had emitted early -- it was not silent -- then went quiet
#: mid-generation, and the 60s window killed it at 112.9s of a generation that
#: was measured finishing at 127.53s. 14.7s short, and the same model completes
#: reliably outside CI. One window was serving two different questions.
#:
#: Time-to-FIRST-token is a property of the queue: 14.59s for deepseek, 137.63s
#: for beam. A gap BETWEEN tokens is a long generation thinking. So the
#: first-output windows stay exactly as measured (60s / 240s / 60s), and this
#: one governs only AFTER the model call has opened.
#:
#: 420s, NOT 240s, AND THE WINDOW READS A DIFFERENT CLOCK SINCE 2026-10-27.
#: `opencode run` does not stream to a pipe: it prints a banner at ~5s and the
#: whole message in one burst at completion (measured: 90 lines at +62.89s), so
#: stdout can only prove COMPLETION. The watchdog's liveness clock is the CLI's
#: own DEBUG log instead, and the logs are quiet for the whole generation --
#: nothing is printed between `llm runtime selected` and `loop step=1`. This
#: number therefore has to cover a COMPLETE generation, not a token gap. The
#: measured completion is 127.53s and CI has been seen past 240s, which is
#: exactly why 240 fired on seven models that were never dead.
INTER_TOKEN_SEC = 420


def inter_token_sec_for(model_id=None):
    """The gap allowed BETWEEN tokens, once assistant text has begun.

    One number on purpose: unlike time-to-first-token, no two models here have
    measured inter-token behaviour 9x apart, so a per-model table would be a
    preference rather than a measurement. `model_id` is accepted so the
    watchdog and the arithmetic below read the same shape as the TTFT table.
    """
    return INTER_TOKEN_SEC


#: Marker the watchdog prints when it abandons a silent attempt. It is in the
#: attempt log, so a later classification does not have to guess why the
#: process died.
WATCHDOG_MARKER = "__WATCHDOG_NO_OUTPUT__"

#: Every failed attempt maps to exactly one of these. The set is closed: a new
#: class is a deliberate change, not a fallback string.
FAILURE_CLASSES = (
    "model_not_found",
    "payment_required",
    "quota_or_rate_limited",
    "auth",
    "provider_outage",
    "slow_or_stalled",
    "other",
)

#: The CLI says `UnknownError: Unexpected server error` for a missing model,
#: which is ambiguous: the SAME string is printed for a real 5xx. So the
#: not-found marker is required on its own -- the debug log carries
#: `ProviderModelNotFoundError: Model not found: <id>` -- and UnknownError
#: alone is provider_outage.
_NOT_FOUND_MARKERS = ("providermodelnotfounderror", "model not found")
#: `Free usage limit reached` is the string the provider used on 2026-10-25 when
#: it rejected a call 1.0s in -- it contains no "quota" and no "rate limit", so
#: without "usage limit" the single most expensive failure looked like "other".
_QUOTA_MARKERS = ("rate limit", "rate_limit", "ratelimit", "quota", "usage limit",
                  "too many requests", "retry-after", "retry after")
_AUTH_MARKERS = ("unauthorized", "invalid api key", "invalid_api_key",
                 "authentication failed", "invalid token", "forbidden")
#: The PLAN refused the call, and the model id is innocent. Measured wording on
#: the OpenCode free plan: "Free usage limit reached ... subscribe to OpenCode
#: Go". That sentence also carries a quota marker, so this class is tested
#: BEFORE quota -- otherwise a plan refusal reads as transient throttling and
#: the whole ladder gets walked, rung by rung, to hear the same refusal.
#: Deliberately narrow: the classifier reads the WHOLE attempt log, assistant
#: prose included, so a loose "upgrade" would misclassify a digest about
#: software pricing.
_PAYMENT_MARKERS = ("subscribe to opencode", "upgrade to opencode go",
                    "payment required")
_OUTAGE_MARKERS = ("unknownerror", "unexpected server error",
                   "internal server error", "bad gateway",
                   "service unavailable", "gateway timeout",
                   "endpoint is unavailable", "upstream request failed",
                   # Measured 2026-10-27: a model the catalogue listed as ACTIVE
                   # answered `Model is unavailable`. That is the endpoint
                   # withdrawing a model it still lists, not a missing id, so it
                   # belongs with the outages and earns the 15-minute cooldown
                   # only after it repeats.
                   "model is unavailable",
                   "econnreset", "econnrefused")

#: HTTP status codes, matched with boundaries so a timestamp or a model id
#: cannot be mistaken for one.
#:
#: THE PRECEDING DOT IS PART OF THE BOUNDARY. Measured 2026-10-27 16:18Z: an
#: attempt whose only fatal line was `Upstream request failed: Model is
#: unavailable` was classified `auth` and aborted the walk with "was rejected
#: (credential)" -- because the CLI's own DEBUG line
#: `timestamp=...T16:18:47.401Z` contains `401` straight after a decimal point,
#: and `(?<!\d)` saw the `.` as a boundary. `47.401` is a millisecond field, not
#: a status. The same hole let `.429Z`, `.500Z` and `.503Z` fake quota and
#: outage classes too, so this is one fix for a whole family of misreads.
_HTTP_RE = re.compile(r"(?<![\d.])(401|402|403|429|500|502|503|504)(?!\d)")


def classify_failure(text, returncode=0):
    """Exactly one FAILURE_CLASSES value for a failed attempt.

    ORDER IS THE CONTRACT, and each step exists because the strings overlap:

      1. model_not_found -- ONLY when the not-found marker is present. The CLI
         reports a missing model as `UnknownError: Unexpected server error`
         (~3s), which is the same text a real 5xx produces, so UnknownError
         alone must never be read as "this model is gone";
      2. auth -- 401/403 and its wording. Terminal for the whole walk: retrying
         another model with a bad credential only wastes the budget and hides
         the cause;
      3. payment_required -- 402, or the plan saying "subscribe to OpenCode
          Go". BEFORE quota on purpose: that refusal sentence also carries
          "usage limit", and reading it as throttle walks the whole ladder to
         hear the same refusal from every rung;
      4. quota_or_rate_limited -- 429/quota/Retry-After;
      5. slow_or_stalled -- the watchdog marker, or rc 124 (what `timeout`
          reports for its own kill);
      6. provider_outage -- 5xx and `UnknownError` with NO not-found marker;
      7. other -- everything else, including an exit 1 with no output.
    """
    low = (text or "").lower()

    if any(m in low for m in _NOT_FOUND_MARKERS):
        return "model_not_found"

    codes = {int(c) for c in _HTTP_RE.findall(low)}
    if codes & {401, 403} or any(m in low for m in _AUTH_MARKERS):
        return "auth"
    if codes & {402} or any(m in low for m in _PAYMENT_MARKERS):
        return "payment_required"
    if codes & {429} or any(m in low for m in _QUOTA_MARKERS):
        return "quota_or_rate_limited"
    if WATCHDOG_MARKER.lower() in low or int(returncode or 0) == 124:
        return "slow_or_stalled"
    if codes & {500, 502, 503, 504} or any(m in low for m in _OUTAGE_MARKERS):
        return "provider_outage"
    return "other"


def worst_case_all_fail_sec(models=None, overhead_sec=0):
    """Worst-case seconds for a walk in which EVERY attempt never EMITS at all.

    Computed FROM THE PER-MODEL FIRST-OUTPUT TABLE, never from one guessed
    window. With the configured six-rung ladder and the 2026-10-26
    measurements:

        deepseek 60 + beam 240 + mimo 60 + nemotron 60 + glm 60 + flint 60
            = 540s = 9 min

    (plus ~30s of CLI cold start for the whole walk, which `overhead_sec`
    states separately so the 540 stays legible). This is the SILENCE bound, and
    it is unchanged by the split into two windows -- an attempt that never emits
    never reaches the second one. The OTHER shape is
    worst_case_emit_then_stall_sec(). A rejected or hard-failing attempt is far
    cheaper -- see fast_fail_all_fail_sec(), and REPEAT_OUTAGE_LIMIT for the one
    that is not silent but is still fatal.
    """
    ids = ladder_model_ids() if models is None else models
    return sum(no_output_sec_for(m) for m in ids) + int(overhead_sec)


def worst_case_emit_then_stall_sec(models=None, per_attempt_cap=None,
                                   overhead_sec=0):
    """Worst-case seconds for a walk in which EVERY attempt emits, then WEDGES.

    The second shape, and a different number. An attempt that emits and stalls
    is measured against INTER_TOKEN_SEC from its LAST line, so its ceiling is
    its first-output window plus that gap -- truncated by the per-attempt cap,
    which is what actually protects a single attempt:

        deepseek   min( 60 + 240, 360) = 300s
        beam       min(240 + 240, 360) = 360s   <- the cap is binding
        the other four                 = 300s each

        = 300 + 360 + 4 * 300 = 1860s

    1860s exceeds the 1800s walk budget, and that is worth stating plainly: in
    this mode the budget and the per-attempt cap bound the run, not the silence
    windows. The windows exist to make the COMMON case cheap; they are not the
    ceiling on the pathological one.
    """
    ids = ladder_model_ids() if models is None else models
    cap = WALK_PER_ATTEMPT_SEC if per_attempt_cap is None else int(per_attempt_cap)
    total = sum(min(no_output_sec_for(m) + inter_token_sec_for(m), cap)
                for m in ids)
    return total + int(overhead_sec)


def ladder_model_ids():
    """The configured seed ladder, in order, from config/opencode.json."""
    return list((load_config(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "opencode.json")) or {}).get("ladder") or [])



#: Measured 2026-10-25: the provider answered `Free usage limit reached` 1.0s
#: after the call started. The CLI printed it to its log and then NEVER EXITED,
#: so both attempts burned their full 480s cap producing zero tokens. Streaming
#: that line and killing on it is what turns a 480s loss into a ~2s one.
FAST_FAIL_SEC = 2

#: Classes that end the attempt the moment they appear on the stream. A
#: provider_outage is NOT here: the CLI may recover from a 5xx by itself, so it
#: keeps the silence window instead of being cut at once.
#: payment_required is here because a plan refusal is instant and permanent for
#: that provider: waiting the silence window out buys nothing but wall clock.
DECISIVE_CLASSES = ("model_not_found", "quota_or_rate_limited", "auth",
                    "payment_required")

#: THE REPEAT BREAKER -- the compensating rule for the line above, and NOT a
#: marker bludgeon.
#:
#: Measured 2026-10-26: flint-40b-free emitted the identical fatal line
#:
#:     AI_APICallError: Upstream request failed: Endpoint is unavailable.
#:
#: FIVE times from 12.6s and still burned the whole allowance, producing zero
#: tokens, because "Endpoint is unavailable" was classified `other` and a
#: provider_outage is deliberately non-decisive. Making the marker decisive
#: would kill a model that is merely having one bad second, so the rule is the
#: REPEAT: the same fatal line REPEAT_OUTAGE_LIMIT times, with ZERO assistant
#: text produced, means this endpoint is gone, not flaky. One 5xx still gets
#: its chance -- which is the whole reason this is a counter and not a marker.
REPEAT_OUTAGE_LIMIT = 3


#: Error-shaped lines, required before a decisive line may kill a live attempt.
#:
#: Without this, prose is a landmine: the digest's own article text can contain
#: "rate limit", "quota" or "unauthorized", and killing a WORKING attempt
#: because a news story mentioned one would be a new way to lose a run. The
#: real failures are exception/log lines, and they say so.
_ERROR_SHAPED = ("apicallerror", "providermodelnotfounderror", "unknownerror",
                 "stream error", "error.error", "error:", "exception",
                 "invalid api key", "authentication failed", "status code")

#: A bare status line (`429 Too Many Requests`) is a real captured shape, so it
#: counts as error-shaped -- but ONLY at the start of the line and ONLY with an
#: HTTP reason phrase, because a digest sentence may legitimately contain a
#: number: "The 429 passengers were rescued" must never kill an attempt.
_STATUS_LINE = re.compile(r"^\s*(?:http[/ ]?\d(?:\.\d)?\s*)?(\d{3})\b")
_REASON_PHRASES = ("too many requests", "payment required", "unauthorized",
                   "forbidden", "not found",
                   "bad gateway", "service unavailable", "gateway timeout",
                   "internal server error", "bad request", "rate limited")


def _error_shaped(low):
    if any(m in low for m in _ERROR_SHAPED):
        return True
    return bool(_STATUS_LINE.match(low)) and any(p in low
                                                 for p in _REASON_PHRASES)


def is_error_shaped(line):
    """True when `line` is a real error/log line rather than assistant prose.

    Exposed because the watchdog needs the SAME test to tell "no assistant text
    yet" (a live stream of tool calls) from "the CLI is printing failures". Two
    implementations of that question would drift apart.
    """
    return _error_shaped((line or "").lower())



def classify_stream_line(line):
    """The failure class for a line ON THE STREAM, or None to let it run.

    Two gates, both required, in this order:

      1. the line must be error-shaped (see _ERROR_SHAPED) -- a digest sentence
         is never decisive;
      2. its class must be in DECISIVE_CLASSES -- an outage is not decisive.

    Returns the class string only when the attempt should be killed NOW.
    """
    low = (line or "").lower()
    if not low.strip():
        return None
    if not _error_shaped(low):
        return None
    kind = classify_failure(low, 1)
    return kind if kind in DECISIVE_CLASSES else None


def fast_fail_all_fail_sec(models=6, overhead_sec=30):
    """Worst case when every attempt is REJECTED (quota / not-found), not silent.

    This is the mode the 2026-10-25 probe measured: 6 x 2s + 30s = 42s. The
    silence bound is still the real worst case, but this is the one that used to
    cost 6 x 480s.
    """
    return int(models) * FAST_FAIL_SEC + int(overhead_sec)


def attempt_timeout(deadline, now, per_attempt):
    """Seconds this attempt may take, or 0 when the stage budget is spent.

    THE TIME MATHS, in one place, because it is the whole claim this file has
    to defend:

        timeout = max(1, min(per_attempt, deadline - now))   if budget remains
        timeout = 0                                           if it does not

    Two consequences, both the point:
      * an attempt never overruns the stage budget, so worst-case time-to-
        verdict is bounded by the budget REGARDLESS of how many models the
        catalogue offers or how many of them are dead; and
      * an attempt that still has budget keeps its FULL per-attempt
        allowance -- the budget trims the tail, it never shortens a working
        model's allowance to make a bad day look better.

    Before this, the worst case was models x attempts x timeout, unbounded by
    anything: 3 x 2 x 60 = 360s before any verdict could be reached.
    """
    remaining = float(deadline) - float(now)
    if remaining <= 0:
        return 0
    return max(1, int(min(float(per_attempt), remaining)))


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_models(candidates, seed=(), health=None, now=0, release=None):
    """Ordered attempt list. Reorders; NEVER removes (see module docstring).

    `candidates` is the live pool (discovered models, or the seed alone when
    discovery is unavailable). `seed` is config/opencode.json's `ladder` --
    the owner's own preference, and still the single source of truth for it.
    `release` is the {model_id: release_ordinal} map discovery produced; it
    feeds the LAST tiebreak and nothing else.

    The sort key, in order, and WHY this order:

      1. cooling down            -- a model that just cost us time goes last;
      2. consecutive failures    -- least-recently-bad first;
      3. the owner's seed order  -- big-pickle leads because the owner says
           so. Proven-ness deliberately does NOT come before this: on a 6-hourly
           cron a single extra success is noise, and letting it promote an
           unknown model over the owner's stated first choice is exactly the
           "not as good as big-pickle" outcome this is meant to prevent;
      4. measured successes      -- the tiebreak WITHIN a seed position, so a
           proven rung beats an unproven one at equal standing;
      5. release date, then id   -- deterministic, and only ever separating
           two models health memory has never distinguished.
    """
    health = (health or {}).get("models") or {}
    release = release or {}
    seed_pos = {m: i for i, m in enumerate(seed)}
    unknown = len(seed_pos)

    def key(model):
        row = health.get(model) or {}
        return (
            1 if on_cooldown(row, now) else 0,          # healthy first
            int(row.get("consecutive_failures") or 0),  # least-recently-bad
            seed_pos.get(model, unknown),               # owner's preference
            -int(row.get("success") or 0),              # proven, as tiebreak
            -int(release.get(model) or 0),              # untried: newer first
            model,                                      # deterministic
        )

    return sorted(candidates, key=key)


def select(config, health, cache_path=None, state_note=None, max_models=4,
           now=None, discover_fn=None, cooldown=None, cooldown_path=None,
           role="other"):
    """The whole decision, in one call. Returns (models, info).

    `models` is the ordered attempt list: capped to `max_models`, or the FULL
    ranked pool when `max_models` is None -- which is what `--all` passes, so
    the caller can bound the WALK instead of the list.
    `info` is what a log row must carry to be honest about how the list was
    built: the discovery source, what discovery rejected and why, and which
    models health memory demoted.

    `cooldown` / `cooldown_path` OPT IN to the hard cooldown, and a caller that
    passes neither keeps the reorder-only behaviour above. When one is given,
    a model whose hard cooldown is live is REMOVED from the list and the walk
    falls through to the next ranked model; if that leaves nothing,
    `info["all_cooled"]` is True and the list is empty, so the caller can fail
    loudly instead of reporting an empty ladder it does not have.

    `role` decides what a SHARED-POOL cooldown does to this caller: "other"
    yields (see pool_block), "digest" may ignore a stale one. When the pool is
    blocked the list comes back EMPTY with `info["pool"]["blocked"]` set, so a
    caller can log the cooldown and skip the call instead of discovering it one
    refused attempt at a time.
    """
    import time as _time

    now = _time.time() if now is None else now
    seed = tuple(m for m in (config.get("ladder") or []) if isinstance(m, str))
    provider = config.get("provider") or _provider_of(seed)

    live, disc = ((discover_fn(provider) if discover_fn
                   else discover(provider, cache_path=cache_path)))

    pool = [m for m in live if isinstance(m, str) and m]
    info = {"source": disc.get("source", "none"),
            "provider": provider,
            "seed": list(seed),
            "discovered": list(pool),
            "rejected": disc.get("rejected") or {},
            "unparsed": disc.get("unparsed") or [],
            "error": disc.get("error", ""),
            "dropped_seed": [], "dropped_seed_evidence": {}, "cooled_down": [],
            "hard_cooled_down": [], "all_cooled": False}

    # THE CONFIGURED LADDER IS AUTHORITATIVE (run 40280837914). Discovery may
    # APPEND, and may never reorder or remove a seed. The only removal is a
    # record that says the model is gone -- see GONE_STATUSES: absence from a
    # listing proves nothing, because the unauthenticated catalogue the runner
    # sees is a different, smaller list that omits five working models.
    status_by_id = disc.get("catalogue_status") or {}
    kept = []
    for m in seed:
        status = status_by_id.get(m)
        status = status.strip().lower() if isinstance(status, str) else ""
        if status in GONE_STATUSES:
            info["dropped_seed"].append(m)
            info["dropped_seed_evidence"][m] = "catalogue status=%r" % status
        else:
            kept.append(m)

    if pool:
        pool = kept + [m for m in pool if m not in kept]
    else:
        pool = list(kept)          # tier 3: the owner's own seed
        info["source"] = "seed" if kept else "none"

    rows = (health or {}).get("models") or {}
    info["cooled_down"] = [m for m in pool if on_cooldown(rows.get(m), now)]

    ordered = rank_models(pool, seed=seed, health=health, now=now,
                          release=disc.get("release"))

    # THE HARD COOLDOWN, applied AFTER ranking so the fall-through is simply
    # "the next ranked model that is not cooling". Prune first, then read:
    # an expired entry must not be able to veto anything.
    cool = cooldown
    if cool is None and cooldown_path:
        cool = load_cooldown(cooldown_path)
    pool = {"blocked": False, "note": ""}
    if cool is not None:
        prune_cooldown(cool, now)
        prune_pool(cool, now)
        info["hard_cooled_down"] = [m for m in ordered
                                    if on_hard_cooldown(cool, m, now)]
        pool = pool_block(cool, now, role=role)
    info["pool"] = pool
    cooled = set(info["hard_cooled_down"])
    attemptable = [m for m in ordered if m not in cooled]
    info["all_cooled"] = bool(ordered) and not attemptable

    if pool.get("blocked"):
        # THE POOL WALL IS NOT AN EMPTY LADDER. Returning ids here would invite
        # the caller to spend an attempt proving what the marker already knows,
        # and returning them empty with no reason is what made a cooldown look
        # like a broken tree. `info["pool"]` carries the reason and the expiry.
        info["selected"] = []
        info["pool_blocked"] = True
    elif max_models is None:
        # The WHOLE ranked pool. The caller bounds the WALK, not the list: it
        # walks in rank order under its own wall-clock budget and stops when one
        # more attempt is no longer affordable. The cap was the defect -- on run
        # 39117668287 all three capped models died (two at the 600s cap) and the
        # slot ended with a known-good pool of ten, none of them tried.
        info["selected"] = list(attemptable)
    else:
        info["selected"] = attemptable[:max(1, max_models)]
        # SAFETY: health memory is a reorder and must never yield silence from a
        # non-empty pool, however bad it looks. A HARD cooldown may, and that
        # case is reported as all_cooled rather than papered over with a model
        # the provider has already proved it cannot serve.
        if attemptable and not info["selected"]:
            info["selected"] = attemptable[:1]
    fetched_at, age_sec = _cache_stamp(cache_path, provider, now)
    info["cache_fetched_at"] = fetched_at
    info["cache_age_sec"] = age_sec
    return info["selected"], info


def _provider_of(seed):
    for model in seed:
        m = MODEL_ID_RE.match(model or "")
        if m:
            return m.group(1)
    return ""


def load_config(path):
    """config/opencode.json, or {} -- the caller degrades, never crashes."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


# ---------------------------------------------------------------------------
# CLI — single-sources the ladder for the GitHub workflows
# ---------------------------------------------------------------------------
# Until 2026-10-18 this module was import-only: breaking_monitor.py and
# ai_classifier.py used it, but daily-news.yml's agent step still read the
# static `jq -r '.ladder[]'` seed and so never got discovery OR health memory.
# On 2026-10-16/17 all three seed models failed and the whole digest slot was
# zeroed. These two subcommands let the shell single-source the same decision.

def _write_info(path, doc):
    """Write provenance JSON. Best-effort, like every other writer here: a
    provenance file that cannot be written must never end a run."""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _read_log_tail(path, limit=262144):
    """The last `limit` bytes of an attempt log, decoded. "" when unreadable.

    Tail only: a runaway attempt log is never worth loading whole to find a
    marker that is always near the end.
    """
    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="model_selector",
        description="Resolve the model ladder dynamically and keep health memory.")
    parser.add_argument("--select", action="store_true",
                        help="print the ordered attempt list, one model per line")
    parser.add_argument("--record", nargs=2, metavar=("MODEL", "OUTCOME"),
                        help="record an outcome: ok, timeout, transport, garbage, "
                             "or a classifier class (model_not_found, "
                             "provider_outage, quota_or_rate_limited, ...)")
    parser.add_argument("--health", default="state/model_health.json",
                        help="health-memory file (default: state/model_health.json)")
    parser.add_argument("--cache", default="state/model_cache.json",
                        help="discovery cache file (default: state/model_cache.json)")
    parser.add_argument("--config", default="config/opencode.json",
                        help="file the seed ladder is read from")
    parser.add_argument("--info-out", default=None,
                        help="write the selection provenance JSON here")
    # `--max` keeps the BREAKING path and the tests bounded, so its default
    # stays 3. The digest agent step passes `--all` instead and bounds the WALK
    # with --budget/--per-attempt, because a fixed count is exactly what failed
    # run 39117668287: three capped models died while ten had been discovered.
    parser.add_argument("--max", type=int, default=3,
                        help="maximum number of models to return (default: 3)")
    parser.add_argument("--all", action="store_true",
                        help="return the FULL ranked pool, uncapped; the caller "
                             "applies its own wall-clock budget")
    parser.add_argument("--budget", type=int, default=WALK_TOTAL_BUDGET_SEC,
                        help="whole-walk budget in seconds, reported to the "
                             "caller (default: %d)" % WALK_TOTAL_BUDGET_SEC)
    parser.add_argument("--per-attempt", type=int, default=WALK_PER_ATTEMPT_SEC,
                        help="per-attempt cap in seconds, reported to the "
                             "caller (default: %d)" % WALK_PER_ATTEMPT_SEC)
    parser.add_argument("--classify", action="store_true",
                        help="classify one failed attempt from its log/exit code")
    parser.add_argument("--log", default=None,
                        help="attempt log to classify (with --classify)")
    parser.add_argument("--rc", type=int, default=0,
                        help="attempt exit code (with --classify)")
    parser.add_argument("--no-work", action="store_true",
                        help="the caller has established there is nothing to "
                             "write, so state the no-work walk budget (%ds) "
                             "instead of %ds; the number stays here, the caller "
                             "only names the case"
                             % (WALK_NO_WORK_TOTAL_BUDGET_SEC,
                                WALK_TOTAL_BUDGET_SEC))
    parser.add_argument("--role", choices=("other", "digest"), default="other",
                        help="what a SHARED-POOL cooldown does to this caller: "
                             "'other' yields (and yields for the rest of an open "
                             "digest window), 'digest' may ignore one it has "
                             "watched for longer than the grace")
    parser.add_argument("--pool-refused", action="store_true",
                        help="record a SHARED-POOL refusal in the cooldown file "
                             "(the whole free pool shares one bucket, so this is "
                             "one fact, not one per model)")
    parser.add_argument("--retry-after", dest="retry_after", type=int,
                        default=None,
                        help="seconds from the provider's Retry-After (with "
                             "--pool-refused); omitted or unreadable means the "
                             "file's own default")
    args = parser.parse_args(argv)

    if args.record:
        model, outcome = args.record
        doc = load_health(args.health)
        cool_path = cooldown_path_for(args.health)
        cool = load_cooldown(cool_path)
        prune_cooldown(cool)
        if outcome == "ok":
            record_success(doc, model)
            clear_cooldown(cool, model)
        else:
            record_failure(doc, model, outcome)
            record_cooldown(cool, model, outcome)
        save_health(args.health, doc)
        # Written only when there is something to say, so a walk with nothing
        # cooling leaves no file behind for the state commit to pick up.
        if cool.get("models") or os.path.isfile(cool_path or ""):
            save_cooldown(cool_path, cool)
        return 0

    if args.classify:
        print(classify_failure(_read_log_tail(args.log), args.rc))
        return 0

    if args.pool_refused:
        cool_path = cooldown_path_for(args.health)
        cool = load_cooldown(cool_path)
        retry_after = args.retry_after
        if retry_after is None:
            # The provider's own number is the observed reset, so read it out of
            # the attempt log before falling back to the file's default.
            retry_after = retry_after_from_text(_read_log_tail(args.log))
        row = record_pool_refusal(cool, retry_after=retry_after)
        save_cooldown(cool_path, cool)
        # ONE LINE, AND IT NAMES THE EXPIRY. A suppression nobody can date is
        # indistinguishable from a broken workflow.
        print("model_selector: pool cooldown recorded -- the shared pool refused "
              "until %s (%s, %ds%s)"
              % (row.get("until"), row.get("reason"), int(row.get("seconds") or 0),
                 "" if retry_after is None else ", from the provider's Retry-After"),
              file=sys.stderr)
        return 0

    if args.select:
        config = load_config(args.config)
        health = load_health(args.health)
        cool_path = cooldown_path_for(args.health)
        cool = load_cooldown(cool_path)
        models, info = select(config, health, cache_path=args.cache,
                              max_models=None if args.all else args.max,
                              cooldown=cool, role=args.role)
        if cool.get("models") or os.path.isfile(cool_path or ""):
            save_cooldown(cool_path, cool)
        # The caller states the CASE, never the number: `--no-work` means "there
        # is nothing to write" and this file owns what that costs, exactly as it
        # owns the full walk. min() so an explicit smaller --budget still wins:
        # naming a case may never enlarge a bound the caller already set.
        total_budget = args.budget
        if args.no_work:
            total_budget = min(args.budget, WALK_NO_WORK_TOTAL_BUDGET_SEC)
        # ONE ATTEMPT MAY NEVER BE GRANTED MORE THAN THE WHOLE WALK. Before this,
        # `--no-work` stated a 300s budget beside a 360s per-attempt cap -- a
        # single attempt could outlast its own walk, which is incoherent even
        # though the caller clamped it with min(remaining, PER).
        info["per_attempt_sec"] = min(args.per_attempt, total_budget)
        # Per model, because the window follows the measurement: the workflow
        # hands the model id to the watchdog and the watchdog looks it up here.
        info["no_output_sec_by_model"] = {m: no_output_sec_for(m)
                                          for m in models}
        info["no_output_sec_default"] = NO_OUTPUT_DEFAULT_SEC
        info["total_budget_sec"] = total_budget
        info["min_attempt_sec"] = WALK_MIN_ATTEMPT_SEC
        if args.no_work:
            info["no_work"] = True
            info["budget_reason"] = (
                "no usable news candidates: the agent's task is to write "
                "state/stories.json with research_status 'limited' and explain "
                "in notes (prompts/daily-digest.md FAILURE MODE), which forbids "
                "answering an empty collection with supplemental "
                "re-collection")
        if args.info_out:
            _write_info(args.info_out, info)
        # STDERR, deliberately: stdout is the model list and the workflow reads
        # it with `mapfile -t models`. One header line on stdout would become a
        # "model" whose name is a sentence, and the walk would spend a whole
        # per-attempt timeout asking the CLI to run it.
        print("model_selector: pool source=%s size=%d returned=%d budget=%ds "
              "per_attempt=%ds min_attempt=%ds hard_cooled=%d%s"
              % (info.get("source"), len(info.get("discovered") or []),
                 len(models), total_budget, args.per_attempt,
                 WALK_MIN_ATTEMPT_SEC, len(info.get("hard_cooled_down") or []),
                 " no_work=yes" if args.no_work else ""), file=sys.stderr)
        print("model_selector: ranked ids=%s" % (", ".join(models) or "none"),
              file=sys.stderr)
        # THE POOL VERDICT, ALWAYS. Either the call is being suppressed (named,
        # with its expiry) or a cooldown was deliberately ignored -- and the
        # second case is the trade the digest slot makes, so it is stated rather
        # than left to be inferred from a successful run.
        pool = info.get("pool") or {}
        if pool.get("note"):
            print("model_selector: %s%s"
                  % ("" if pool.get("blocked") else "WILL NOT YIELD -- ",
                     pool["note"]), file=sys.stderr)
        for model in models:
            print(model)
        if not models:
            if info.get("pool_blocked"):
                # A suppression, and NOT an empty ladder or a cooled-down one.
                # Exit 0: nothing is broken here, the pool is simply walled, and
                # the caller's job is to wait or to yield, not to fail the job.
                return 0
            if info.get("all_cooled"):
                # THE ONE CASE THAT MAY NOT BE SILENT. Every model is cooling,
                # so nothing will be attempted -- and that must be named, not
                # reported as an empty ladder that does not exist.
                print("model_selector: FATAL: every candidate model is on a "
                      "hard cooldown (%s), so no attempt can be made until one "
                      "expires. This is the cooldown, not an empty ladder."
                      % (", ".join(info.get("hard_cooled_down") or [])
                         or "list not recorded"), file=sys.stderr)
                return 1
            print("model_selector: empty model ladder -- discovery returned "
                  "nothing and %s has no seed" % args.config, file=sys.stderr)
            return 1
        return 0

    parser.error("nothing to do: pass --select or --record")


if __name__ == "__main__":
    sys.exit(_main())