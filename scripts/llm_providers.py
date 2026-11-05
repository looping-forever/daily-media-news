"""llm_providers.py -- one OpenAI-compatible chat adapter, three providers.

WHY THIS EXISTS. Every model call in this repository went through ONE account's
allowance: the two `gemini/*` rungs (a single Google AI Studio key) and the
shared OpenCode free pool (one bucket for every free id). Measured 2026-11-05:
the digest walk spent the Gemini free tier in 13.4s, the keyed rungs refused,
the pool answered `payment_required`, and the edition shipped with publisher
text under every headline. One provider's spent allowance became the whole
pipeline's outage.

The owner added three repository secrets -- `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `MISTRAL_API_KEY` -- and this module is the single
adapter that talks to all three. They are OpenAI-compatible chat-completion
endpoints, so the request shape is IDENTICAL across them and differs only in
the URL, the model id and the credential. One shape means one set of failure
mappings, and a provider's quirks cannot grow a second health system beside
model_selector's.

WHAT IT IS NOT. Not a fourth model-selection engine. `scripts/model_selector.py`
still owns health memory, cooldowns and failure classification; this module only
turns (provider, prompt) into either text or ONE reason word from the existing
vocabulary (`quota_notice`, `timeout`, `transport`, `auth`, `garbage`,
`model_not_found`, plus `not_configured`, which the probe reports and the
ladders filter out before a call is ever attempted).

NO KEY IS EVER LOGGED, PRINTED OR WRITTEN. Each credential is read from the
environment AT CALL TIME into a local, put in one request header, and dropped
when the function returns. Nothing here caches it, and `redact()` scrubs a
credential out of any provider text before that text can reach a log, a debug
record or an artifact.

    Groq        https://api.groq.com/openai/v1/chat/completions
    OpenRouter  https://openrouter.ai/api/v1/chat/completions
    Mistral     https://api.mistral.ai/v1/chat/completions

`gemini` is in the table for the PROBE ONLY. Its production path stays the
existing keyed `opencode` CLI rung (`OPENCODE_CONFIG` + `{env:GEMINI_API_KEY}`),
which the digest and breaking walks already resolve; routing `gemini/*` through
this adapter as well would give one provider two code paths and two timeouts.
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: One sentence's worth of answer is well under this. Named rather than inlined
#: because the summariser needs ~6 summaries per request and the classifier one
#: small JSON object; both fit, and a truncated model reply is indistinguishable
#: from an empty one, so the cap is generous on purpose.
MAX_TOKENS = 1024

#: The probe's whole input. Fixed, tiny and answerable in one word, so a failure
#: is about the provider and never about the prompt.
PROBE_PROMPT = "reply with the single word OK"

#: One bounded second attempt on a 429. Measured 2026-11-05: Mistral answered 429
#: in 219 ms, which is a per-minute window rather than a broken id, and reporting
#: "quota" off one sample sends the owner to the billing page for nothing.
PROBE_RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible endpoint and the facts needed to call it."""

    name: str
    url: str
    models_url: str
    env: str
    models: tuple          # candidate ids, best first; corrected by the probe
    timeout: int           # the socket ceiling for ONE call, in seconds
    free_only: bool = False


#: THE LADDER ORDER, AND THE ONLY PLACE IT IS WRITTEN. Gemini is absent on
#: purpose: its rungs come first from `config/opencode.json` and are walked by
#: the CLI, so the two blocks are composed in `ordered_ladder()` instead of
#: being flattened into one hardcoded list that can rot (the 2026-10-13 lesson).
LADDER_PROVIDERS = ("groq", "openrouter", "mistral")

PROVIDERS = {
    "gemini": Provider(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "GEMINI_API_KEY",
        ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
        60,
    ),
    "groq": Provider(
        "groq",
        "https://api.groq.com/openai/v1/chat/completions",
        "https://api.groq.com/openai/v1/models",
        "GROQ_API_KEY",
        ("llama-3.3-70b-versatile",),
        60,
    ),
    "openrouter": Provider(
        "openrouter",
        "https://openrouter.ai/api/v1/chat/completions",
        "https://openrouter.ai/api/v1/models",
        "OPENROUTER_API_KEY",
        # `:free` VARIANTS ONLY. A paid id here would spend the owner's credit
        # on a digest, silently, and the guard below refuses one before a byte
        # is sent. The candidate list is the SEED; `list_models()` is what
        # proves which ids this account can actually reach.
        #
        # SEEDED FROM THE ACCOUNT'S OWN CATALOGUE (probe, 2026-11-05): it serves
        # 8 of the 12 ids listed and NOT the two guessed here before, so a seed
        # id is a claim to check, never a fact. Order is for PROSE SUMMARISATION:
        # general instruct models first, then vision-capable, then small, then
        # the code specialist, and the content-safety classifier last -- it is
        # built to refuse, not to write.
        ("xingchen-agi/xing4.0-29b-a4b:free",
         "nvidia/nemotron-3.5-super-vl:free",
         "recursive/flint-v1.0:free",
         "dots-studio/dots-3-note-preview:free",
         "nvidia/nemotron-3.5-lightning:free",
         "liquid/lfm-2.5-2.6b:free",
         "cohere/north-mini-code:free",
         "nvidia/nemotron-3.5-content-safety:free"),
        75,
        free_only=True,
    ),
    "mistral": Provider(
        "mistral",
        "https://api.mistral.ai/v1/chat/completions",
        "https://api.mistral.ai/v1/models",
        "MISTRAL_API_KEY",
        ("mistral-small-latest",),
        60,
    ),
}

#: Status -> the ONE reason word the rest of the repository already speaks.
#: A 402 is not special-cased: these three providers bill per token against a
#: credit balance, and a spent balance arrives as 429 or 401 -- reading it as
#: `quota_notice` keeps the walk moving to the next provider rather than
#: treating a billing state as a broken request.
STATUS_REASON = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "model_not_found",
    408: "timeout",
    429: "quota_notice",
    500: "transport",
    502: "transport",
    503: "transport",
    504: "timeout",
}

REASON_TEXT = {
    "auth": "key refused",
    "quota_notice": "quota refused",
    "timeout": "no answer in time",
    "transport": "transport failed",
    "garbage": "unreadable reply",
    "model_not_found": "model not found",
    "bad_request": "request refused",
    "not_configured": "key not configured",
}

#: What the PROBE prints. Closed on purpose -- a probe that invents verdicts is
#: a second vocabulary. `garbage` is reported as `network`: a 200 with no usable
#: answer is a transport-shaped failure for an operator reading the table.
PROBE_VERDICT = {
    "auth": "auth",
    "quota_notice": "quota",
    "model_not_found": "model_not_found",
    "timeout": "timeout",
    "transport": "network",
    "garbage": "network",
    "bad_request": "network",
}

#: Name markers that make an id a POOR prose summariser, with how hard to avoid it.
#: Only consulted for ids the configured seeds do not name.
_PREFERENCE_MARKERS = (
    ("content-safety", 95), ("moderation", 90), ("guard", 85), ("safety", 80),
    ("embed", 95), ("rerank", 95),
    ("code", 60), ("coder", 60),
    ("tiny", 40), ("small", 30), ("mini", 30), ("lite", 15), ("flash", 10),
    ("experimental", 10), ("preview", 5),
)

#: `/models` reads, one per provider per process. The catalogue changes slowly
#: and a walk asks about the same three providers repeatedly.
_CATALOGUE_CACHE = {}


class ProviderError(Exception):
    """A call that produced no text, carrying the ONE reason word.

    `kind` is the shared vocabulary; `detail` is the provider's own (redacted)
    words, short enough for a debug record and never containing the credential.
    """

    def __init__(self, message, kind="transport", status=0, detail=""):
        super().__init__(message)
        self.kind = kind
        self.status = int(status or 0)
        self.detail = detail or ""


@dataclass
class ProviderResult:
    """One call, described. `ok` is the only field a caller must branch on."""

    ok: bool
    text: str = ""
    model: str = ""
    elapsed: float = 0.0
    status: int = 0
    reason: str = ""
    detail: str = ""


@dataclass
class ModelList:
    """One `/models` read, described. `ids` is empty on any failure."""

    ids: list = field(default_factory=list)
    status: int = 0
    elapsed: float = 0.0
    reason: str = ""


def provider_of(model_id):
    """The HTTP provider a rung belongs to, or None for a CLI rung.

    Only `LADDER_PROVIDERS` route here. `gemini/*` deliberately returns None so
    it keeps walking the existing keyed CLI path.
    """
    name = str(model_id or "").split("/", 1)[0]
    return name if name in LADDER_PROVIDERS else None


def provider(name):
    """The Provider record, or ValueError naming what was asked for."""
    if name not in PROVIDERS:
        raise ValueError("unknown provider %r (known: %s)"
                         % (name, ", ".join(sorted(PROVIDERS))))
    return PROVIDERS[name]


def _free_guard(record, model_id):
    """Refuse a paid OpenRouter id BEFORE any request is built.

    Raised rather than mapped to a result: a paid id is a programming error in
    the ladder, not a provider failure, and recording it as one would earn a
    cooldown for a model that was never called.
    """
    if record.free_only and not str(model_id or "").endswith(":free"):
        raise ValueError("%s: refusing the paid model id %r -- only ':free' ids "
                         "may be selected" % (record.name, model_id))
    return model_id


def model_for(name, model_id=None):
    """The bare model id to send, defaulting to the provider's first candidate."""
    record = provider(name)
    chosen = model_id or record.models[0]
    if chosen.startswith("%s/" % record.name):
        chosen = chosen.split("/", 1)[1]
    return _free_guard(record, chosen)


def key_for(name, environ=None):
    """The credential, read at CALL TIME. Never stored, never returned upstream."""
    return ((environ if environ is not None else os.environ)
            .get(provider(name).env) or "").strip()


def configured(name, environ=None):
    """True when this provider's key is present. Absence is not a failure."""
    return bool(key_for(name, environ))


def effective_timeout(name, requested=None):
    """The socket ceiling for one call: never above the provider's own cap.

    `requested` is the caller's fair share (summarise_stories.call_timeout /
    model_selector.attempt_timeout). This clamp is the second half of that
    arithmetic: a share can only ever SHORTEN a provider call, never extend it
    past the provider's measured ceiling.
    """
    cap = provider(name).timeout
    if requested is None:
        return cap
    return max(1, int(min(cap, float(requested))))


def redact(text, key=""):
    """Strip a credential (and anything that looks like one) out of *text*.

    Providers sometimes echo the Authorization header in an error body. That
    sentence is useful and the credential in it is not, so the sentence is kept
    and the credential is replaced.
    """
    text = str(text or "")
    if key:
        text = text.replace(key, "[redacted]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1[redacted]", text)
    return text


def request_spec(name, model_id, prompt, key, timeout=None, max_tokens=MAX_TOKENS):
    """The exact request one call makes: url, headers, body, timeout.

    Built as a value so a test can assert the URL, the header and the body
    without a socket, and so nothing in the send path can add a header nobody
    reviewed. The credential is in `Authorization` and NOWHERE ELSE -- no query
    string, because a query string lands in provider logs and proxy logs.
    """
    record = provider(name)
    body = {
        "model": model_for(name, model_id),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    return {
        "url": record.url,
        "method": "POST",
        "timeout": effective_timeout(name, timeout),
        "headers": {
            "Authorization": "Bearer %s" % key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        "body": json.dumps(body),
    }


def _http(spec):
    """(status, body_text) for one request. Transport faults RAISE.

    The socket timeout is the one in `spec`, set by `effective_timeout`, so the
    bound is enforced by the OS on the connection itself rather than by an outer
    deadline that only notices between calls.
    """
    data = spec.get("body")
    req = urllib.request.Request(
        spec["url"],
        data=data.encode("utf-8") if data is not None else None,
        headers=dict(spec.get("headers") or {}),
        method=spec.get("method") or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=spec.get("timeout")) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read() or b"").decode("utf-8", "replace")


def _reason_for_status(status):
    if status in STATUS_REASON:
        return STATUS_REASON[status]
    if 500 <= int(status or 0) <= 599:
        return "transport"
    return "bad_request"


def _message_text(body):
    """`choices[0].message.content` out of a chat-completions body, or ""."""
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        return ""
    try:
        return str(doc["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _error_text(body):
    """The provider's own error sentence, bounded. Never contains the request."""
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        return str(body or "")[:300]
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err:
            return str(err)[:300]
        if doc.get("message"):
            return str(doc["message"])[:300]
    return str(body or "")[:300]


def call(name, prompt, model_id=None, timeout=None, max_tokens=MAX_TOKENS,
         transport=None, environ=None):
    """One chat-completion call. Returns a ProviderResult; never raises for a
    provider problem, and never makes a request without a credential."""
    started = time.monotonic()
    record = provider(name)
    key = key_for(name, environ)
    model_id = model_for(name, model_id)     # the guard fires before any I/O
    if not key:
        # NOT A FAILURE. The ladder filters unconfigured providers out before
        # this point; the probe reports `not_configured`; a stray call answers
        # the same way instead of pretending the provider refused.
        return ProviderResult(False, "", model_id, 0.0, 0, "not_configured")
    spec = request_spec(name, model_id, prompt, key, timeout=timeout,
                        max_tokens=max_tokens)
    send = transport or _http
    try:
        status, body = send(spec)
    except (socket.timeout, TimeoutError) as exc:
        return ProviderResult(False, "", model_id, time.monotonic() - started,
                              0, "timeout", redact(str(exc), key)[:300])
    except urllib.error.URLError as exc:
        reason = "timeout" if isinstance(getattr(exc, "reason", None),
                                         (socket.timeout, TimeoutError)) \
            else "transport"
        return ProviderResult(False, "", model_id, time.monotonic() - started,
                              0, reason, redact(str(exc), key)[:300])
    except OSError as exc:
        return ProviderResult(False, "", model_id, time.monotonic() - started,
                              0, "transport", redact(str(exc), key)[:300])
    elapsed = time.monotonic() - started
    status = int(status or 0)
    if status == 200:
        text = _message_text(body)
        if text.strip():
            return ProviderResult(True, text, model_id, elapsed, status, "ok")
        return ProviderResult(False, "", model_id, elapsed, status, "garbage",
                              redact(_error_text(body), key)[:300])
    reason = _reason_for_status(status)
    return ProviderResult(False, "", model_id, elapsed, status, reason,
                          redact(_error_text(body), key)[:300])


def text_for(model_id, prompt, timeout=None, max_tokens=MAX_TOKENS,
             transport=None, environ=None):
    """The answer text for a full rung id (`groq/<id>`), or ProviderError."""
    name = provider_of(model_id)
    if not name:
        raise ProviderError("%r is not an HTTP provider rung" % (model_id,),
                            kind="model_not_found")
    result = call(name, prompt, model_id=model_id, timeout=timeout,
                  max_tokens=max_tokens, transport=transport, environ=environ)
    if result.ok:
        return result.text
    raise ProviderError(
        "%s: %s (HTTP %d)%s" % (result.model, result.reason, result.status,
                                (" -- %s" % result.detail) if result.detail else ""),
        kind=result.reason, status=result.status, detail=result.detail)


def cli_result(model_id, prompt, timeout=None, transport=None, environ=None):
    """(rc, out, err) for the breaking walk, in the shape its runner returns.

    The walk was written against `opencode run` and classifies failures from the
    TEXT it reads, so this returns the provider's HTTP status and own sentence:
    `model_selector.classify_failure` then maps 401 to `auth`, 429 to
    `quota_or_rate_limited`, 404-with-marker to `model_not_found` and 5xx to
    `provider_outage` -- the SAME vocabulary, no second classifier.

    A timeout is raised as TimeoutExpired because that is what the walk already
    catches for a killed CLI, and it carries the same "this model spent its
    window" meaning.
    """
    import subprocess
    name = provider_of(model_id)
    if not name:
        return 1, "", "%s: not an HTTP provider rung" % model_id
    result = call(name, prompt, model_id=model_id, timeout=timeout,
                  transport=transport, environ=environ)
    if result.ok:
        return 0, result.text, ""
    detail = result.detail or ""
    line = "HTTP %d %s: %s" % (result.status, REASON_TEXT.get(result.reason,
                                                             result.reason), detail)
    if result.reason in ("timeout", "not_configured"):
        # Both mean "this rung produced nothing and its window is spent". The
        # walk retries a fast transport failure but never a timeout, and
        # `not_configured` can only be reached through a stale ladder, so the
        # honest handling is the same for both.
        raise subprocess.TimeoutExpired(model_id, float(timeout or 0),
                                        output="", stderr=line)
    return 1, "", line.strip()


def _bare_id(raw):
    """The id without the transport's own namespace prefix.

    Gemini's OpenAI-compatible `/models` answers `models/gemini-…` while its chat
    endpoint takes the bare id. Comparing the prefixed form against the configured
    candidates matched nothing, so a healthy Gemini read as an unserved provider
    in the 2026-11-05 probe.
    """
    text = str(raw or "")
    return text[len("models/"):] if text.startswith("models/") else text


def list_models(name, transport=None, environ=None):
    """Read the provider's own `/models`. Discovery, never a guess.

    The probe calls this so the model ids in `PROVIDERS` can be corrected from
    what the account actually serves. An OpenRouter listing is filtered to
    `:free` ids so a paid model can never enter the ladder by discovery.
    """
    started = time.monotonic()
    record = provider(name)
    key = key_for(name, environ)
    if not key:
        return ModelList([], 0, 0.0, "not_configured")
    spec = {
        "url": record.models_url,
        "method": "GET",
        "timeout": record.timeout,
        "headers": {"Authorization": "Bearer %s" % key,
                    "Accept": "application/json"},
        "body": None,
    }
    try:
        status, body = (transport or _http)(spec)
    except (socket.timeout, TimeoutError) as exc:
        return ModelList([], 0, time.monotonic() - started, "timeout")
    except (urllib.error.URLError, OSError):
        return ModelList([], 0, time.monotonic() - started, "transport")
    elapsed = time.monotonic() - started
    status = int(status or 0)
    if status != 200:
        return ModelList([], status, elapsed, _reason_for_status(status))
    try:
        doc = json.loads(body)
        rows = doc.get("data") if isinstance(doc, dict) else None
        ids = [_bare_id(r.get("id")) for r in (rows or [])
               if isinstance(r, dict) and r.get("id")]
    except (TypeError, ValueError):
        return ModelList([], status, elapsed, "garbage")
    if record.free_only:
        ids = [i for i in ids if i.endswith(":free")]
    return ModelList(ids, status, elapsed, "ok")


def choose_model(name, discovered, environ=None):
    """The id to probe: first candidate the provider actually serves.

    Falls back to the discovered list's first id, then to the configured
    candidate, so the probe always names SOMETHING and the row makes clear
    whether the configured name was found.
    """
    record = provider(name)
    served = [i for i in (discovered or []) if not record.free_only
              or i.endswith(":free")]
    for candidate in record.models:
        if candidate in served:
            return candidate
    if served:
        return served[0]
    return record.models[0]


def _preference_key(model_id):
    """Sort key for a served id whose name is not one of the configured seeds.

    General instruct models summarise prose; a safety classifier is built to
    refuse, an embedding cannot write a sentence, and a code specialist writes
    the wrong register. Those are pushed to the back rather than winning by
    catalogue order.
    """
    text = str(model_id or "").lower()
    penalty = 0
    for marker, weight in _PREFERENCE_MARKERS:
        if marker in text:
            penalty += weight
    return (penalty, len(text), text)


def preference_order(served, preferred=()):
    """`preferred` first (in its own order), then the rest best-first."""
    served = [i for i in (served or []) if i]
    head = [m for m in (preferred or ()) if m in set(served)]
    head_set = set(head)
    return head + sorted((m for m in served if m not in head_set),
                         key=_preference_key)


def catalogue(name, transport=None, environ=None, refresh=False):
    """One `/models` read per provider per process. ONLY a good read is cached.

    A failed read is not evidence about the catalogue, so it is never cached: the
    next caller asks again instead of inheriting a transient 429 for the rest of
    the process.
    """
    if not refresh and name in _CATALOGUE_CACHE:
        return _CATALOGUE_CACHE[name]
    listing = list_models(name, transport=transport, environ=environ)
    if listing.reason == "ok":
        _CATALOGUE_CACHE[name] = listing
    return listing


def clear_catalogue():
    """Forget cached reads -- for tests and for a deliberate re-probe."""
    _CATALOGUE_CACHE.clear()


def served_model(name, transport=None, environ=None, refresh=False):
    """The id to actually send, proven against this account's catalogue.

    Three outcomes, and each one is explicit because a silently wrong id is the
    defect this replaced:

    * the catalogue was read and lists ids -> the first SERVED candidate in
      preference order; `substituted` says whether that differed from the
      configured one, so the walk log can name what really answered;
    * the catalogue was read and lists nothing usable -> `model` is None and
      `reason` is `no_served_model`. The provider contributes NO rung: calling it
      would only earn a `model_not_found` cooldown for an id nobody serves;
    * the catalogue could not be read (no key, network, timeout, 429) -> the
      configured candidate is kept and `reason` says `catalogue_unreadable`. That
      is not evidence of an unserved id, and dropping a working provider on a
      failed GET would be the same class of guess in the other direction.
    """
    record = provider(name)
    configured_id = record.models[0]
    got = {"provider": name, "model": configured_id, "source": "configured",
           "reason": "", "configured": configured_id, "substituted": False,
           "available": []}
    if not key_for(name, environ):
        got.update(model=None, source="none", reason="not_configured")
        return got
    listing = catalogue(name, transport=transport, environ=environ,
                        refresh=refresh)
    got["available"] = list(listing.ids)
    if listing.reason != "ok":
        got["reason"] = "catalogue_unreadable"
        got["catalogue_reason"] = listing.reason
        return got
    if not listing.ids:
        got.update(model=None, source="none", reason="no_served_model")
        return got
    chosen = preference_order(listing.ids, record.models)[0]
    got.update(model=chosen, source="catalogue",
               substituted=chosen != configured_id)
    return got


def _probe_note(got):
    """The plain-sentence line under a probe row, or "".

    NEVER appended to the row: the row is a fixed shape that tests and the
    summary table read, so an explanation goes on its own indented line.
    """
    verdict = got.get("verdict")
    if verdict == "auth":
        return ("the provider REFUSED the key (HTTP %s) -- the secret is present "
                "and was rejected; regenerate it, then re-run this probe"
                % (got.get("status") or "401/403"))
    if verdict == "model_not_found":
        return ("the chosen id %r is not in this account's catalogue; seed from "
                "the `available` line instead" % (got.get("chosen") or "-"))
    if verdict == "quota":
        return ("rate-limited after %d attempt(s): capacity, not a wrong id"
                % got.get("attempts", 1))
    if verdict == "ok" and got.get("attempts", 1) > 1:
        return "answered on the second attempt after a 429"
    return ""


def probe(name, transport=None, environ=None, sleep=None):
    """One provider, proven: key, catalogue, and one tiny real answer.

    A 429 earns ONE retry after PROBE_RETRY_SECONDS: a per-minute window says
    nothing about whether the id works, and the wait is injected so a test never
    sleeps. The row records how many attempts it took.
    """
    started = time.monotonic()
    key_present = configured(name, environ)
    discovered = ModelList([], 0, 0.0, "not_configured") \
        if not key_present else catalogue(name, transport=transport,
                                          environ=environ, refresh=True)
    chosen = choose_model(name, discovered.ids, environ)
    if not key_present:
        return {
            "provider": name,
            "key": "absent",
            "models": 0,
            "chosen": "",
            "status": 0,
            "ms": int((time.monotonic() - started) * 1000),
            "verdict": "not_configured",
            "available": [],
            "attempts": 0,
        }
    result = call(name, PROBE_PROMPT, model_id=chosen,
                  timeout=effective_timeout(name), transport=transport,
                  environ=environ)
    attempts = 1
    if not result.ok and result.reason == "quota_notice":
        (sleep or time.sleep)(PROBE_RETRY_SECONDS)
        result = call(name, PROBE_PROMPT, model_id=chosen,
                      timeout=effective_timeout(name), transport=transport,
                      environ=environ)
        attempts = 2
    verdict = "ok" if result.ok else PROBE_VERDICT.get(result.reason,
                                                       result.reason)
    return {
        "provider": name,
        "key": "present",
        "models": len(discovered.ids),
        "chosen": chosen,
        "status": result.status,
        "ms": int((result.elapsed or 0.0) * 1000),
        "verdict": verdict,
        "available": list(discovered.ids[:8]),
        "attempts": attempts,
    }


def probe_rows(transport=None, environ=None, sleep=None):
    """The probe's table, one row per provider, Gemini first (it leads).

    The `available=` line under each row is the EVIDENCE the model ids in this
    file are corrected from: it is the account's own catalogue, not a guess. The
    `note:` line carries anything explanatory -- refused key, wrong id, retry --
    so the pinned row shape never changes.
    """
    rows = []
    for name in ("gemini",) + LADDER_PROVIDERS:
        got = probe(name, transport=transport, environ=environ, sleep=sleep)
        rows.append("%s | key=%s | models=%d | chosen=%s | status=%s | "
                    "ms=%d | verdict=%s"
                    % (got["provider"], got["key"], got["models"],
                       got["chosen"] or "-", got["status"] or "-", got["ms"],
                       got["verdict"]))
        if got["available"]:
            rows.append("    %s available: %s"
                        % (got["provider"], ", ".join(got["available"])))
        note = _probe_note(got)
        if note:
            rows.append("    %s note: %s" % (got["provider"], note))
    return rows


def ladder_rungs(environ=None, transport=None, resolve=False, log=None,
                 refresh=False):
    """The configured provider rungs, in ladder order, as full rung ids.

    A provider with no key is SKIPPED SILENTLY -- its absence is a repository
    setting, not a model failure, and a rung that cannot be called would cost
    the walk an attempt to learn what one `os.environ` lookup already says.

    `resolve=True` asks each provider's catalogue for an id it actually serves
    (`served_model`), which is the production path: the probe showed the seed ids
    are guesses and this account serves a different set. It is opt-in because a
    caller that builds a ladder at IMPORT time must not open a socket.
    """
    rungs = []
    for name in LADDER_PROVIDERS:
        if not configured(name, environ):
            continue
        if not resolve:
            rungs.append("%s/%s" % (name, model_for(name)))
            continue
        got = served_model(name, transport=transport, environ=environ,
                           refresh=refresh)
        if not got["model"]:
            if log:
                log("llm_providers: %s contributes no rung -- %s"
                    % (name, got["reason"]))
            continue
        if got["substituted"] and log:
            log("llm_providers: %s does not serve %r; using the served id %r"
                % (name, got["configured"], got["model"]))
        rungs.append("%s/%s" % (name, got["model"]))
    return tuple(rungs)


def ordered_ladder(seeds, prefixes=(), environ=None, transport=None,
                   resolve=False, log=None):
    """`seeds` re-ordered as: Gemini, then Groq/OpenRouter/Mistral, then the rest.

    Gemini rungs stay first because they are the owner's stated preference,
    and the free OpenCode pool stays LAST because its shared bucket is the one
    that answers `payment_required` for every id at once -- a rung worth trying
    only when a keyed or free-keyed provider has already declined.

    `prefixes` is the existing CLI-reachability filter: an empty tuple means
    "no filter" (the summariser, which owns no CLI), and a non-empty tuple keeps
    only seeds this process can resolve (the breaking walk, whose seeds are CLI
    rungs). The HTTP rungs are added regardless: they do not use the CLI, which
    is the point of having them.
    """
    seeds = [m for m in (seeds or ()) if isinstance(m, str) and m]
    if prefixes:
        seeds = [m for m in seeds if m.startswith(tuple(prefixes))]
    gemini = [m for m in seeds if m.startswith("gemini/")]
    rest = [m for m in seeds if not m.startswith("gemini/")]
    return tuple(gemini) + ladder_rungs(environ, transport=transport,
                                        resolve=resolve, log=log) + tuple(rest)
