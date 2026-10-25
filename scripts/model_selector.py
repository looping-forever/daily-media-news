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
HEALTH MEMORY CAN ONLY REORDER. It can never veto. A cooldown demotes a
model to the back of the attempt order; it never removes a model from it,
and rank_models always returns at least one candidate whenever any
candidate exists. If EVERY discovered model is cooling down, the
least-recently-failed one is still returned and still tried. A system that
decides what counts as breaking news must never be silenced by its own
memory of a bad afternoon.

Cooldown decay, stated once: cooldown = COOLDOWN_UNIT_SEC x consecutive
failures, capped at COOLDOWN_MAX_SEC. COOLDOWN_UNIT_SEC is the breaking
monitor's own 6-hour cron period, so "one failure" means exactly "skip it
for the next scheduled run and try it again", which is a real retry rather
than a blacklist. A single success resets the streak to zero and clears the
cooldown immediately. A model that has never failed is never penalised.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

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
    if isinstance(status, str) and status and status != "active":
        return False, "status:%s" % status
    caps = meta.get("capabilities")
    if not isinstance(caps, dict):
        return False, "no_capabilities"
    cin, cout = caps.get("input"), caps.get("output")
    if not isinstance(cin, dict) or not cin.get("text"):
        return False, "no_text_input"
    if not isinstance(cout, dict) or not cout.get("text"):
        return False, "no_text_output"
    return True, "ok"


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
            "elapsed_sec": 0.0}
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
                                    "release": dict(info.get("release") or {})}}
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
    for model in models:
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
            json.dump({"provider": provider, "fetched_at": fetched_at,
                       "models": list(ids)}, fh, ensure_ascii=False, indent=2)
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
        kind = "transport"
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
# The wall-clock walk budget (the digest agent stage)
# ---------------------------------------------------------------------------
# WHY A BUDGET AND NOT A COUNT. Run 39117668287 discovered ten free models and
# walked exactly three: two hit the 600s cap, the third exited 1, and the slot
# ended with no digest while seven models sat untried. A re-run minutes later
# succeeded on the first model in ~5 min. So the pool is UNCAPPED (`--all`) and
# the WALK is bounded by the clock instead, which is the only bound that adapts
# to how sick the provider is.

#: Per-attempt cap. The only measurement of a success for this stage is ~300s,
#: so this is that measurement plus a fifth: 600s let each DEAD model eat four
#: minutes more than a working one has ever been measured to need.
WALK_PER_ATTEMPT_SEC = 360
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
           now=None, discover_fn=None):
    """The whole decision, in one call. Returns (models, info).

    `models` is the ordered attempt list: capped to `max_models`, or the FULL
    ranked pool when `max_models` is None -- which is what `--all` passes, so
    the caller can bound the WALK instead of the list.
    `info` is what a log row must carry to be honest about how the list was
    built: the discovery source, what discovery rejected and why, and which
    models health memory demoted.
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
            "dropped_seed": [], "cooled_down": []}

    if pool:
        # A seed model that Zen no longer serves is DROPPED, not attempted.
        # Attempting it is how a run spends 60s proving a model is gone.
        info["dropped_seed"] = [m for m in seed if m not in pool]
    else:
        pool = list(seed)          # tier 3: the owner's own seed
        info["source"] = "seed" if seed else "none"

    rows = (health or {}).get("models") or {}
    info["cooled_down"] = [m for m in pool if on_cooldown(rows.get(m), now)]

    ordered = rank_models(pool, seed=seed, health=health, now=now,
                          release=disc.get("release"))
    if max_models is None:
        # The WHOLE ranked pool. The caller bounds the WALK, not the list: it
        # walks in rank order under its own wall-clock budget and stops when one
        # more attempt is no longer affordable. The cap was the defect -- on run
        # 39117668287 all three capped models died (two at the 600s cap) and the
        # slot ended with a known-good pool of ten, none of them tried.
        info["selected"] = list(ordered)
    else:
        info["selected"] = ordered[:max(1, max_models)]
        # SAFETY: a non-empty pool must yield a non-empty attempt list, however
        # bad the health memory looks. Reordering is allowed; silence is not.
        if pool and not info["selected"]:
            info["selected"] = ordered[:1]
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


def _main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="model_selector",
        description="Resolve the model ladder dynamically and keep health memory.")
    parser.add_argument("--select", action="store_true",
                        help="print the ordered attempt list, one model per line")
    parser.add_argument("--record", nargs=2, metavar=("MODEL", "OUTCOME"),
                        help="record an outcome: ok, timeout, transport or garbage")
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
    parser.add_argument("--no-work", action="store_true",
                        help="the caller has established there is nothing to "
                             "write, so state the no-work walk budget (%ds) "
                             "instead of %ds; the number stays here, the caller "
                             "only names the case"
                             % (WALK_NO_WORK_TOTAL_BUDGET_SEC,
                                WALK_TOTAL_BUDGET_SEC))
    args = parser.parse_args(argv)

    if args.record:
        model, outcome = args.record
        doc = load_health(args.health)
        if outcome == "ok":
            record_success(doc, model)
        else:
            record_failure(doc, model, outcome)
        save_health(args.health, doc)
        return 0

    if args.select:
        config = load_config(args.config)
        health = load_health(args.health)
        models, info = select(config, health, cache_path=args.cache,
                              max_models=None if args.all else args.max)
        # The caller states the CASE, never the number: `--no-work` means "there
        # is nothing to write" and this file owns what that costs, exactly as it
        # owns the full walk. min() so an explicit smaller --budget still wins:
        # naming a case may never enlarge a bound the caller already set.
        total_budget = args.budget
        if args.no_work:
            total_budget = min(args.budget, WALK_NO_WORK_TOTAL_BUDGET_SEC)
        info["per_attempt_sec"] = args.per_attempt
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
              "per_attempt=%ds min_attempt=%ds%s"
              % (info.get("source"), len(info.get("discovered") or []),
                 len(models), total_budget, args.per_attempt,
                 WALK_MIN_ATTEMPT_SEC,
                 " no_work=yes" if args.no_work else ""), file=sys.stderr)
        print("model_selector: ranked ids=%s" % (", ".join(models) or "none"),
              file=sys.stderr)
        for model in models:
            print(model)
        if not models:
            print("model_selector: empty model ladder -- discovery returned "
                  "nothing and %s has no seed" % args.config, file=sys.stderr)
            return 1
        return 0

    parser.error("nothing to do: pass --select or --record")


if __name__ == "__main__":
    sys.exit(_main())