#!/usr/bin/env python3
"""ai_classifier.py — deterministic concurrent race pattern for AI providers.

Concurrent race — first provider returning valid JSON wins.

Fires multiple AI providers concurrently via asyncio.wait; first provider
returning a valid JSON array of classifications wins.  All remaining tasks
are cancelled and logged.

Stdlib only (asyncio + urllib.request).  No third-party dependencies.

Contract
--------
classify_batch(items) -> list[dict]
  Each dict has keys: title, category, confidence, summary, relevance_score.

Providers race concurrently.  Each provider retries up to its max_attempts
with per-attempt backoff.  First valid JSON array response wins.
If ALL providers exhaust their attempts, raises RuntimeError.
"""

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request

try:
    import model_selector
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import model_selector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Per-position wall clock for the race. Positional, not per-MODEL: a name
#: per model is a hardcoded ladder wearing a disguise, and it is what went
#: stale on 2026-10-13.
PROVIDER_TIMEOUTS = (120, 180, 120)

#: How many models the dynamic pool contributes to one race. A free tier
#: does not want twelve concurrent calls, and a race that fans out without
#: bound is the time waste this change exists to remove.
MAX_RACE_PROVIDERS = 4

MAX_RETRIES = 3
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB — cap per-provider response read

API_BASE_URL = os.environ.get("OPENCODE_API_URL", "https://api.opencode.ai/v1")

REQUIRED_KEYS = frozenset({
    "title", "category", "confidence", "summary", "relevance_score",
})


def _repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def _config_path():
    return os.path.join(_repo_root(), "config", "opencode.json")


def _api_prefix():
    """`<provider>/` -- the prefix THIS module's endpoint can route.

    Derived from the config's own `provider` field, never written down as a
    model list. It exists because the ladder is now shared with the CLI walk:
    the walk resolves a second provider (Gemini, via config/opencode-cli.json)
    that api.opencode.ai does not serve, and a `gemini/...` id posted here is a
    guaranteed 404 that would burn one of the four race slots.
    """
    provider = model_selector.load_config(_config_path()).get("provider")
    return "%s/" % provider if isinstance(provider, str) and provider else ""


def _load_ladder():
    """Model ladder from config/opencode.json (single source of truth).

    NO embedded fallback any more, and that removal is the point. The
    embedded copy was a second source of truth that nothing tested against
    reality: on 2026-10-13 it still named a Zen model (`mimo-v2.5-free`)
    that was no longer served at all. An unreadable config now yields an
    empty seed, and resolve_providers() falls back through the live
    catalogue to that empty seed rather than inventing an answer.

    FILTERED to the ids this endpoint serves (see _api_prefix), so a rung that
    only exists for the CLI walk cannot reach the race.
    """
    cfg = model_selector.load_config(_config_path())
    ladder = cfg.get("ladder")
    prefix = _api_prefix()
    if isinstance(ladder, list):
        return tuple(m for m in ladder
                     if isinstance(m, str) and m and m.startswith(prefix))
    return ()


LADDER = _load_ladder()


def provider_name(model):
    """The label a race result is reported under, derived from the model ID.

    Derived, never written down: a hardcoded label per ladder position is a
    hardcoded ladder, and it silently re-couples this module to ladder
    indices that discovery is free to reorder.
    """
    return (model or "").rsplit("/", 1)[-1] or "unknown"


def _provider_entry(index, model):
    return {
        "name": provider_name(model),
        "model": model,
        "max_attempts": 3 if index == 0 else 2,
        "backoff": [0, 20, 40] if index == 0 else [30, 60],
        "timeout": PROVIDER_TIMEOUTS[min(index, len(PROVIDER_TIMEOUTS) - 1)],
    }


#: The owner's SEED, in preference order. Kept module-level and deterministic
#: because it is what the ladder-sync drift test pins against config, and
#: because resolving the live pool costs a CLI start that an import must not.
PROVIDERS = [_provider_entry(i, m) for i, m in enumerate(LADDER)]

_RESOLVED = None


def resolve_providers(force=False):
    """The provider pool for one race: config seed + live catalogue, ranked.

    Memoised per process, because `opencode models` costs ~4.6s of CLI cold
    start and a race may be resolved more than once. Ordering, filtering and
    the never-veto rule all live in model_selector.
    """
    global _RESOLVED
    if _RESOLVED is not None and not force:
        return _RESOLVED
    cfg = model_selector.load_config(_config_path())
    state_dir = os.path.join(_repo_root(), "state")
    try:
        # Seed with the SERVABLE ladder only, and over-fetch by one slot per
        # unservable rung: the selector caps what it returns, so a `gemini/...`
        # seed left in place would spend a race slot on an id this endpoint
        # answers 404 for. The slice below re-applies the real cap.
        seed_only = dict(cfg, ladder=list(LADDER))
        models, _info = model_selector.select(
            seed_only,
            model_selector.load_health(
                os.path.join(state_dir, "model_health.json")),
            cache_path=os.path.join(state_dir, "model_cache.json"),
            max_models=MAX_RACE_PROVIDERS + 2)
    except Exception as exc:  # selection must never end a digest build
        # Never degrade silently: a discovery regression must be visible.
        log.warning("ai_classifier: model selection failed (%s); using "
                    "config seed of %d models", exc, len(LADDER))
        models = list(LADDER)
    # Selection appends everything the CLI catalogue lists, INCLUDING providers
    # this module's endpoint does not serve. Same filter as the seed.
    prefix = _api_prefix()
    models = [m for m in models
              if isinstance(m, str) and m.startswith(prefix)][:MAX_RACE_PROVIDERS]
    _RESOLVED = [_provider_entry(i, m) for i, m in enumerate(models)]
    return _RESOLVED

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = (
    "You are a news classifier. For each news item below, return a JSON array "
    "where each element has exactly these keys:\n"
    "- title: the story title\n"
    "- category: one of [tech, gaming, entertainment, politics, sports, local, "
    "education, weather, other]\n"
    "- confidence: one of [HIGH, MEDIUM, LOW]\n"
    "- summary: 1-2 sentence summary\n"
    "- relevance_score: float 0.0-1.0 indicating relevance\n"
    "- universal_importance: boolean — true if this is a major event that "
    "everyone should know about regardless of personal interests (mass "
    "casualties, major disaster, war, significant policy change, outbreak, "
    "major infrastructure failure); false otherwise\n\n"
    "Items to classify:\n{items_json}\n\n"
    "Return ONLY the JSON array, no other text."
)


def _build_prompt(items):
    """Build classification prompt from a list of candidate dicts."""
    payload = []
    for i, item in enumerate(items):
        payload.append({
            "index": i,
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "source": item.get("source", ""),
            "domain_category": item.get("domain_category", ""),
        })
    return _CLASSIFY_PROMPT.format(
        items_json=json.dumps(payload, ensure_ascii=False, indent=2),
    )


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------

def _is_valid_response(result):
    """True when *result* is a non-empty list of dicts with all required keys."""
    if not isinstance(result, list) or not result:
        return False
    return all(
        isinstance(item, dict) and REQUIRED_KEYS.issubset(item.keys())
        for item in result
    )


def _parse_json_array(text):
    """Try to extract a JSON array from *text* (direct or embedded)."""
    text = (text or "").strip()
    # Direct parse.
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # Embedded: find first '[' ... last ']' for the outermost array.
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, list):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# HTTP transport (async, stdlib only)
# ---------------------------------------------------------------------------

def _http_post_sync(url, payload_bytes, headers, timeout):
    """Blocking POST — runs in a thread executor."""
    req = urllib.request.Request(url, data=payload_bytes, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_RESPONSE_BYTES)


async def _http_post(model, prompt, api_key, timeout):
    """Async POST to the chat completions endpoint; returns parsed JSON."""
    url = "%s/chat/completions" % API_BASE_URL
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(
        None, _http_post_sync, url, body, headers, timeout,
    )
    return json.loads(raw.decode("utf-8", errors="replace"))


def _extract_text(response):
    """Pull assistant message text from a chat completions response."""
    try:
        choices = response.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return msg.get("content") or ""
    except (AttributeError, TypeError):
        pass
    return ""


# ---------------------------------------------------------------------------
# Single-provider runner (with retries + backoff)
# ---------------------------------------------------------------------------

async def _run_provider(provider, prompt, api_key):
    """Execute one provider with retries.  Returns (name, data_or_error_str).

    *data_or_error_str* is a list (valid classification) on success or a
    string describing the failure on exhaustion.
    """
    name = provider["name"]
    model = provider["model"]
    timeout = provider["timeout"]
    max_attempts = provider["max_attempts"]
    backoff = provider["backoff"]

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        idx = attempt - 1
        wait = backoff[idx] if idx < len(backoff) else (backoff[-1] if backoff else 0)

        if attempt > 1:
            log.info("%s attempt %d: retrying after %ds", name, attempt, wait)
            await asyncio.sleep(wait)

        try:
            log.info("%s attempt %d: calling", name, attempt)
            response = await _http_post(model, prompt, api_key, timeout)
            text = _extract_text(response)
            parsed = _parse_json_array(text)

            if parsed is not None and _is_valid_response(parsed):
                log.info("%s attempt %d: ok (%d items)", name, attempt, len(parsed))
                return name, parsed

            last_error = "%s attempt %d: invalid response format" % (name, attempt)
            log.warning("%s", last_error)
        except urllib.error.HTTPError as exc:
            last_error = "%s attempt %d: HTTP %d" % (name, attempt, exc.code)
            log.warning("%s", last_error)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            last_error = "%s attempt %d: %s" % (name, attempt, str(reason)[:80])
            log.warning("%s", last_error)
        except Exception as exc:
            last_error = "%s attempt %d: %s" % (name, attempt, type(exc).__name__)
            log.warning("%s", last_error)

    return name, last_error or "%s: all %d attempts exhausted" % (name, max_attempts)


# ---------------------------------------------------------------------------
# Concurrent provider race
# ---------------------------------------------------------------------------

async def _race_providers(providers, prompt, api_key):
    """Fire ALL providers concurrently via asyncio.wait; first valid JSON
    array wins.  Remaining tasks are cancelled and logged.

    A provider that FAILS does not end the race.  FIRST_COMPLETED returns on
    the first task to finish whether it won or lost, so a fast failure used to
    discard healthy siblings that were still in flight and report the whole
    race as failed.  The wait therefore loops until some provider returns a
    valid array or every provider has exhausted its attempts.

    Returns a list of dicts (classification) on success.
    Raises nothing — errors are collected for the caller.
    """
    tasks = [
        asyncio.ensure_future(_run_provider(p, prompt, api_key))
        for p in providers
    ]

    names = [p["name"] for p in providers]
    pending = set(tasks)
    errors = []

    while pending:
        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)

        # `done` is a set and has no order; walk it in ladder order so the
        # same inputs always pick the same winner.
        for task in sorted(done, key=tasks.index):
            idx = tasks.index(task)
            try:
                result = task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One provider blowing up must not end the race.
                errors.append("%s: %s" % (names[idx], type(exc).__name__))
                continue
            if not isinstance(result, tuple) or len(result) != 2:
                errors.append("%s: unexpected result type" % names[idx])
                continue
            name, data = result
            if isinstance(data, list):
                for t in pending:
                    t.cancel()
                    log.info("race: cancelled pending provider %s",
                             t.get_name() if hasattr(t, "get_name")
                             else "unknown")
                return data
            if isinstance(data, str):
                errors.append(data)
            else:
                errors.append("%s: unexpected data type" % name)

    return errors  # caller inspects this list to raise RuntimeError


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_batch(items, providers=None):
    """Classify a batch of news items via concurrent AI provider race.

    Args:
        items: list of dicts with at least ``title``, ``snippet``, ``source``.
        providers: explicit pool, or None for the dynamically resolved one
            (config seed + live catalogue, ranked and capped).

    Returns:
        list of dicts, each with keys: title, category, confidence, summary,
        relevance_score.

    Raises:
        RuntimeError: if all providers fail or return invalid data.
    """
    api_key = os.environ.get("OPENCODE_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENCODE_API_KEY environment variable not set")

    if not items:
        return []

    prompt = _build_prompt(items)
    pool = list(providers) if providers is not None else resolve_providers()

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            _race_providers(pool, prompt, api_key),
        )
    finally:
        loop.close()

    # result is a list of dicts (classification) on success,
    # or a list of error strings on total failure.
    if isinstance(result, list) and result and isinstance(result[0], dict):
        # Ensure universal_importance is always present (default False)
        # for backward compatibility with older AI responses.
        for item in result:
            if "universal_importance" not in item:
                item["universal_importance"] = False
        return result

    # All providers failed — build combined error info.
    if isinstance(result, list):
        error_summary = "; ".join(str(e) for e in result)
    else:
        error_summary = str(result)
    raise RuntimeError("All AI providers failed: %s" % error_summary)


# ---------------------------------------------------------------------------
# CLI (quick manual test)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _test_items = [
        {
            "title": "Test: major world news",
            "snippet": "Something happened today",
            "source": "test.com",
            "domain_category": "world",
        },
    ]
    try:
        _results = classify_batch(_test_items)
        print(json.dumps(_results, indent=2))
    except RuntimeError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(1)
