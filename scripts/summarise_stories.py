"""summarise_stories.py -- batched AI summaries for the fallback edition.

WHY THIS EXISTS. Measured on 2026-11-05 (run 41944898717, 00:56Z): the agentic
walk sent EIGHT model requests in 13.4 seconds -- one title call and seven build
turns -- and the eighth was refused by Gemini's free tier:

    Quota exceeded for metric:
    generativelanguage.googleapis.com/generate_content_free_tier_input_token_count,
    limit: 250000 ... Please retry in 40.095354008s.

Each turn re-sends the whole conversation, so the spend is the SUM of eight
growing contexts (prompt 54,970 chars + state/history.json 88,442 bytes + the
candidate payload). Both keyed rungs were refused that way, the free OpenCode
pool answered `payment_required`, and the edition shipped with publisher text.

The walk cannot be paced from outside: it is one `opencode run` and the CLI
decides how many turns it takes. ONE REQUEST PER BATCH OF STORIES has a fixed
input (a few thousand tokens) and nothing growing behind it, so the same
250,000-tokens-per-minute allowance covers the whole edition several times over.

What this module will NOT do: invent a story, drop a story, or replace a
publisher description with a guess. A batch that never answers leaves its
stories exactly as the deriver built them, and the count of what DID get a
summary is printed and carried into the page.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import english_text  # noqa: E402
import llm_providers  # noqa: E402
from validate_stories import crime_violation, spoiler_violation  # noqa: E402

#: Stories per request. Six keeps one request near 2,000 tokens: small enough
#: that three batches fit the per-minute allowance with room to spare, large
#: enough that a fifteen-story edition costs three requests rather than fifteen.
BATCH_SIZE = 6

#: Hard ceiling on requests for ONE edition. BATCH_SIZE * MAX_REQUESTS is the
#: most stories this module will try to summarise; the rest keep their
#: publisher text and are counted as not summarised.
MAX_REQUESTS = 4

#: The measured Gemini free-tier limit, in INPUT tokens per minute. It is a
#: named constant rather than a number in the loop because the whole point of
#: this module is that the arithmetic is visible: every request is charged
#: against this budget before it is sent.
INPUT_TOKEN_BUDGET_PER_MINUTE = 250_000

#: Rough token count for English prose. Deliberately pessimistic (4 chars per
#: token is the usual estimate for text this shape) -- over-estimating only
#: makes the pacer wait a little longer, and under-estimating is what spends a
#: budget that is not there.
CHARS_PER_TOKEN = 4

#: No single wait is longer than this, and the hints are honoured only when
#: they fit. A provider that asks for longer than this is abandoned for the
#: edition rather than parked: the refire slot retries the whole job.
MAX_WAIT_SECONDS = 90

#: Used when the refusal carries no hint at all.
DEFAULT_WAIT_SECONDS = 20

#: The WHOLE job's allowance for summarising, waits included. It exists because
#: a per-wait bound is not a total bound: three batches times four models times
#: a 90s hint is eighteen minutes, and the derive step must never be killed with
#: no stories written. When this is spent, the remaining stories keep their
#: publisher text and the count says so.
TOTAL_BUDGET_SECONDS = 300

#: One sentence, not a paragraph. This mirrors the deriver's own cap, because
#: both texts land in the same slot under the same headline.
MAX_SUMMARY_CHARS = 300
MIN_SUMMARY_CHARS = 40

#: Wall clock for ONE CLI call to a KEYED rung (a provider with a real API key,
#: e.g. gemini). The digest walk allows 480s per attempt because the agent does
#: research; a batch of six summaries has nothing to research.
KEYED_REQUEST_TIMEOUT = 120

#: Wall clock for ONE CLI call to a FREE rung (the opencode pool). MEASURED
#: 2026-11-05 (run 41990956737): three free rungs each sat at the 120s ceiling
#: and returned ZERO bytes, spending 360s of a 300s budget inside batch 1 of 4.
#: A rung that has produced nothing after 45s does not produce anything at 120s.
FREE_REQUEST_TIMEOUT = 45

#: No call is granted less than this while the budget can still pay for it.
MIN_RUNG_SECONDS = 20

#: What the FIRST rung of a batch is granted. RUN 42078530071: eight stories and
#: eight rungs cut the fair share to the 20s floor, so `gemini-3.5-flash-lite`
#: failed five 20s attempts that a six-story batch has always needed longer for.
#: 90s still leaves 210s of the 300s allowance. Fallback rungs keep 20s.
LEAD_RUNG_SECONDS = 90

#: Each batch still to come keeps this many seconds back, so one bad batch
#: cannot leave the later batches with a call too short to answer.
MIN_BATCH_SECONDS = 30

#: How much of a failed answer is kept. 2 KB covers a partial JSON array and the
#: metadata line that follows it, and cannot flood an artifact.
DEBUG_RAW_LIMIT = 2048

#: At most one record per failed attempt, and no more than this many in one run.
DEBUG_MAX_RECORDS = 12

#: The refusal vocabulary. A 429 from Gemini arrives as prose inside the CLI's
#: log line, and the OpenCode pool says "Free usage limit reached", so both
#: dialects are matched by text rather than by an HTTP status this module never
#: sees.
REFUSAL_PATTERNS = (
    "429",
    "resource_exhausted",
    "exceeded your current quota",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "free usage limit",
    "too many requests",
    # The provider's edge refused the CLIENT. Measured 2026-11-06: Groq returned
    # this sentence to a request carrying no credential at all.
    "blocked at the edge",
    "access denied",
)

#: Wording that names the CREDENTIAL. It outranks the block words above: a body
#: that says "invalid api key" is an auth fact, whatever else it also says.
KEY_ERROR_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "authentication failed",
)

PROMPT = (
    "You are summarising news stories for a personal morning digest.\n"
    "Each story below has a title, which the page already prints above the "
    "summary, and an `evidence` field: raw text copied from the publisher's "
    "feed. Evidence is NOT prose and is NOT a draft answer.\n"
    "For each story, write ONE plain sentence (at most 40 words) that says what "
    "happened, using only the title and evidence given.\n"
    "NEVER copy, quote or lightly reorder the evidence, and never repeat the "
    "title. An answer that does either is refused by the checker and counts as "
    "nothing -- it is the most common way this task fails.\n"
    "The sentence MUST carry at least one concrete detail the title does not: a "
    "number, a named person, a place, a date, or a specific claim that appears "
    "ONLY in the evidence. A sentence that only restates the headline in different "
    "words is not a summary -- leave it empty instead.\n"
    "Never invent a fact, a number, a name or a quote that is not in the input. "
    "If the evidence adds nothing to the title, or is empty, use an empty "
    "string.\n\n"
    "Return ONLY a JSON array, one object per story, with exactly these keys:\n"
    '  {"url": "<the story\'s url, copied exactly>", "summary": "<one sentence>"}\n\n'
    "Stories:\n%s\n"
)


class Refusal(Exception):
    """A model call that came back with an error, refusal or empty answer.

    The message keeps the provider's own words: the retry hint lives in that
    text ("Please retry in 40.09s"), and quoting it is what makes the wait
    explainable in the log instead of a magic number.

    `kind` names WHY in one word (`timeout`, `exit_nonzero`, `empty_content`,
    `runner_missing`), and `raw` keeps whatever the CLI printed before it failed.
    Both are what the debug record is built from.
    """

    def __init__(self, message, kind="", raw="", exit_code=None):
        super().__init__(message)
        self.kind = kind
        self.raw = raw
        self.exit_code = exit_code


def is_free_rung(model):
    """True for a rung served by the shared free pool rather than a keyed API."""
    model = model or ""
    return model.startswith("opencode/") or model.endswith("-free")


def timeout_for_rung(model):
    """The cap one call to *model* may never exceed, before the budget bites."""
    return FREE_REQUEST_TIMEOUT if is_free_rung(model) else KEYED_REQUEST_TIMEOUT


def min_answer_seconds(model):
    """The least clock a rung of *model*'s class needs, or it cannot answer.

    RUN 42356298224: a ten-rung ladder divided 299s into ~30s slices and six of
    fifteen attempts came back `timeout` -- including a model that answered in
    the same run out of the LEAD rung's 90s slice. The fallback rung is not a
    faster call; it is the same six-story keyed batch, so it needs the same
    clock that was measured to work. A free pool rung is a different call: it
    answers in seconds or not at all (FREE_REQUEST_TIMEOUT is the point at which
    waiting longer was measured to gain nothing), so 20s is a real attempt.
    """
    return MIN_RUNG_SECONDS if is_free_rung(model) else LEAD_RUNG_SECONDS


def rung_group_size(models, index):
    """How many consecutive rungs from *index* are ONE provider's candidates.

    `llm_providers.ladder_rungs` emits an HTTP provider's served ids as
    consecutive rungs -- up to three for OpenRouter, whose 2026-11-05 sweep found
    seven usable free ids. Those ids are one RUNG of the clock: they exist so a
    429, an empty answer or a 404 costs an id instead of the whole provider.
    CLI rungs never group: each is a different provider by construction.
    """
    if not models or index >= len(models):
        return 1
    name = llm_providers.provider_of(models[index])
    if not name:
        return 1
    start = index
    while start > 0 and llm_providers.provider_of(models[start - 1]) == name:
        start -= 1
    end = index + 1
    while end < len(models) and llm_providers.provider_of(models[end]) == name:
        end += 1
    return end - start


def rung_timeout(models, index, seconds_left, batches_left):
    """The slice ONE rung may use, with a provider's ids sharing a single slice.

    Without the division, three OpenRouter rungs would each get a whole rung's
    share and the provider could spend three times the clock it is entitled to.
    With it, the group's slices add up to the share a single rung would have had
    -- and a 429 (which returns in milliseconds) leaves almost all of it for the
    next id.
    """
    group = rung_group_size(models, index)
    share = call_timeout(models[index], seconds_left, batches_left,
                         len(models) - index - (group - 1),
                         lead=(index == 0))
    if group <= 1:
        return int(share)
    return max(1, int(share / group))


def call_timeout(model, seconds_left, batches_left, rungs_left, lead=False):
    """Seconds one call to *model* is granted, and why it is that number.

    THE ARITHMETIC THAT HAS TO FIT (run 41990956737: 29 stories, so four
    batches, TWO keyed rungs and THREE free rungs, 300s):

        2 * 120 + 3 * 45 = 375s   >   300s

    so the ladder alone can outlive the job, and a single hanging rung used to
    spend 120s that a later batch needed. Three bounds make it fit:

        reserve  MIN_BATCH_SECONDS per batch still to come
        share    (seconds_left - reserve) / rungs_left
        cap      min(rung cap, share), floored at the rung's floor,
                 never more than seconds_left

    The floor is LEAD_RUNG_SECONDS for the batch's first rung and the rung's own
    CLASS minimum (`min_answer_seconds`) for the fallbacks -- a keyed fallback
    is still a six-story keyed call, not a faster one. With four batches and
    300s: batch 1 reserves 90s, so its five rungs share 210s, and a keyed rung
    keeps its 90s rather than being cut to 42s.

    A share below the class minimum is not granted at all: `summarise` stops the
    batch with `budget_spent` instead of starting a call that can only time out
    (run 42356298224 spent six of its fifteen attempts that way).
    """
    batches_left = max(1, int(batches_left))
    rungs_left = max(1, int(rungs_left))
    floor = LEAD_RUNG_SECONDS if lead else min_answer_seconds(model)
    reserve = (batches_left - 1) * MIN_BATCH_SECONDS
    usable = max(floor, float(seconds_left) - reserve)
    share = usable / rungs_left
    cap = min(timeout_for_rung(model), max(floor, share))
    return max(1, int(min(cap, float(seconds_left))))


def estimate_tokens(text):
    """Approximate input tokens for *text* (pessimistic, never zero)."""
    return max(1, (len(text or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def is_refusal(text):
    """True when *text* is a spent allowance rather than a broken request."""
    low = (text or "").lower()
    if "access denied" in low and any(m in low for m in KEY_ERROR_MARKERS):
        # "Access denied: invalid api key" names the KEY; it is not an allowance
        # that a wait could restore, and before the block words were added it
        # correctly earned no wait.
        return False
    return any(pattern in low for pattern in REFUSAL_PATTERNS)


def is_blocked(text):
    """True when *text* says the provider's EDGE refused this network."""
    low = (text or "").lower()
    if any(pattern in low for pattern in KEY_ERROR_MARKERS):
        return False
    return "blocked at the edge" in low or "access denied" in low


def retry_hint_seconds(text):
    """The provider's own retry hint from *text*, or None when it gave none.

    Three shapes are seen in the wild, all seconds:
        Please retry in 40.095354008s
        retryDelay: "40s"
        Retry-After: 90
    Minutes ("retry in 2m") are not parsed: no provider measured here sends
    them, and guessing would be worse than the default wait.
    """
    text = text or ""
    patterns = (
        r"retry in\s+(\d+(?:\.\d+)?)\s*s",
        r"retryDelay\"?\s*[:=]\s*\"?(\d+(?:\.\d+)?)s",
        r"retry[- ]after\"?\s*[:=]\s*\"?(\d+(?:\.\d+)?)\s*s?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            try:
                return max(0, int(float(match.group(1))))
            except ValueError:
                return None
    return None


def batches(stories, size=BATCH_SIZE):
    """Split *stories* into request-sized lists, in edition order."""
    size = max(1, int(size))
    return [list(stories[i:i + size]) for i in range(0, len(stories), size)]


def build_prompt(batch, snippets=None):
    """The whole request for one batch: short fields only, never full text.

    `evidence` is the collector's trimmed description, not the article, so a
    request carries no more than it must. The url is the key the answer is
    matched back on, which is why it is repeated in the prompt rather than
    implied by position.

    `snippets` is the candidate index (`url -> the publisher's own description`).
    When it is given -- which is every repair call -- it is the ONLY evidence:
    a missing url sends an empty string, never `story["summary"]`. On the repair
    path that field IS the stub being replaced, and run 42356298224 shows what
    sending it costs: five answers quoted the snippet straight back and the
    validator refused every one. Without an index (the deriver, which runs
    before any summary exists) the story's own text is all there is.
    """
    items = []
    for story in batch:
        url = story.get("source_url") or story.get("url") or ""
        if snippets is not None:
            source = (snippets or {}).get(url) or ""
        else:
            source = story.get("summary") or ""
        items.append({
            "url": url,
            "title": story.get("title") or "",
            "evidence": str(source)[:400],
            "source": story.get("source_name") or "",
        })
    return PROMPT % json.dumps(items, ensure_ascii=False, indent=1)


def _accepted(story_by_url, url, text):
    """The summary if it is safe to print, else "" -- the deriver's gates.

    A model's sentence goes through the SAME three gates the publisher text
    went through (English, no spoiler, no crime detail) plus the length cap.
    Rejecting it here is what keeps a bad generation from shrinking the edition:
    the story keeps the description it already had.
    """
    story = story_by_url.get(url)
    if story is None:
        return ""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) < MIN_SUMMARY_CHARS or len(text) > MAX_SUMMARY_CHARS:
        return ""
    if not english_text.looks_english(text):
        return ""
    category = story.get("category") or ""
    if spoiler_violation(category, "none", text) or crime_violation(text):
        return ""
    return text


#: The CLI's own decoration, which is NOT the answer. CSI covers `\x1b[0m` and
#: the colour forms; OSC covers window titles and hyperlinks, ending in BEL or
#: ST. Both are removed BEFORE any bracket is looked for, because `\x1b[0m`
#: contains the character this module used to slice from -- run 41995372308 lost
#: three good keyed replies to exactly that.
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
#: The CLI's status line: `> build \u00b7 gemini-3.5-flash-lite`.
BUILD_BANNER_RE = re.compile(r"^[ \t]*>[ \t]*build[^\n]*$", re.M)
#: A fenced block, with or without a language tag.
FENCED_BLOCK_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```", re.S | re.I)


def clean_reply(text):
    """The model's actual answer: no escape sequences, no CLI chrome."""
    out = ANSI_OSC_RE.sub("", str(text or ""))
    out = ANSI_CSI_RE.sub("", out)
    out = BUILD_BANNER_RE.sub("", out)
    return out.strip()


def _first_json_list(text):
    """The first COMPLETE JSON array anywhere in `text`, or None.

    `raw_decode` is asked from every "[" rather than a slice being taken to the
    last "]": it knows where an array ends, so a stray bracket -- in a preamble,
    in a note, in a url -- can neither start the slice too early nor end it too
    late.
    """
    decoder = json.JSONDecoder()
    index = text.find("[")
    while index >= 0:
        try:
            value, _end = decoder.raw_decode(text, index)
        except ValueError:
            value = None
        if isinstance(value, list):
            return value
        index = text.find("[", index + 1)
    return None


def reply_rows(text):
    """The array a reply carries, preferring a fenced block when it has one."""
    cleaned = clean_reply(text)
    if not cleaned:
        return None
    for block in FENCED_BLOCK_RE.findall(cleaned):
        rows = _first_json_list(block)
        if rows is not None:
            return rows
    return _first_json_list(cleaned)


def _looks_like_json(cleaned):
    """True when the reply was TRYING to be an array and did not finish."""
    return cleaned.startswith("[") or cleaned.startswith("```")


def parse_reply(text, batch):
    """{url: summary} for the entries this module is willing to print.

    Anything the model returned that is not in the batch it was asked about is
    dropped: a url it invented has no story to attach to, and a url from
    another batch would silently overwrite that batch's own answer.
    """
    rows = reply_rows(text)
    if not isinstance(rows, list):
        return {}
    by_url = {(s.get("source_url") or s.get("url") or ""): s for s in batch}
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        accepted = _accepted(by_url, url, row.get("summary"))
        if accepted:
            out[url] = accepted
    return out


def _truncation_marker(text):
    """True when the CLI itself says the answer was cut at an output cap."""
    return bool(re.search(
        r'"?\s*(?:finish|stop)_?reason"?\s*:\s*"?\s*'
        r'(length|max_tokens|max_output_tokens|token_limit)\b'
        r'|MAX_TOKENS'
        r'|output[_\s-]?token[_\s-]?limit',
        text or "", re.I))


def classify_reply(text, batch=None):
    """WHY a reply produced no summary, in words the log can act on.

    Run 41990956737 logged `answered but no usable summary came back` for two
    keyed rungs -- one exited 1 with PARTIAL JSON on stdout, the other returned a
    parseable answer nothing could be taken from -- and the run could not say
    which. The order below matters: an explicit truncation marker is the CAUSE,
    so it outranks the shape the truncation leaves behind.

        truncated_at_max_tokens  the provider said it hit its output cap
        empty_content            nothing came back at all
        partial_json             a JSON array that stops mid-way
        not_json                 text, but not a JSON array
        no_usable_row            a valid array, none of it safe to print
        ok                       at least one summary is printable
    """
    cleaned = clean_reply(text)
    if not cleaned:
        return "empty_content"
    if _truncation_marker(cleaned):
        return "truncated_at_max_tokens"
    rows = reply_rows(cleaned)
    if rows is None:
        # A reply that STARTS as an array and does not finish is cut; prose that
        # happens to contain a bracket is only prose.
        return "partial_json" if _looks_like_json(cleaned) else "not_json"
    if batch is None:
        return "ok" if rows else "no_usable_row"
    return "ok" if parse_reply(cleaned, batch) else "no_usable_row"


#: Key-shaped strings that must never reach an artifact. Each replacement keeps
#: the credential's own prefix, so the record still shows WHICH kind of key
#: appeared without carrying it. Compiled with re.I because an inline `(?i)`
#: mid-pattern is an error on current Python.
_RAW_REDACTIONS = (
    (r"AIza[0-9A-Za-z_\-]{20,}", "AIzaREDACTED"),
    (r"sk-[A-Za-z0-9_\-]{16,}", "sk-REDACTED"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "gh_REDACTED"),
    (r"xox[abprs]-[A-Za-z0-9\-]{10,}", "xox-REDACTED"),
    (r"(bearer\s+)[A-Za-z0-9._\-]{10,}", r"\1REDACTED"),
    (r'("?(?:api[_-]?key|authorization|token|secret|password)"?\s*[:=]\s*"?)[^"\s,}]{8,}',
     r"\1REDACTED"),
)
REDACTIONS = tuple((re.compile(pattern, re.I), repl)
                   for pattern, repl in _RAW_REDACTIONS)


def redact(text):
    """*text* with every key-shaped string replaced by a named placeholder."""
    out = text or ""
    for pattern, replacement in REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


def extract_meta(text):
    """finish reason, stop reason and token counts visible in *text*.

    The CLI prints these only when something goes wrong, and never in one fixed
    shape, so every field is OPTIONAL here: a field that is not in the text is
    None (or missing from `usage`), never guessed.
    """
    text = text or ""

    def first(pattern):
        match = re.search(pattern, text, re.I)
        return match.group(1) if match else None

    usage = {}
    for key, value in re.findall(
            r'"([a-z_]*(?:token|tokens)[a-z_]*)"\s*:\s*(\d+)', text, re.I):
        usage[key] = int(value)
    return {
        "finish_reason": first(r'"finish_?reason"\s*:\s*"([^"]*)"'),
        "stop_reason": first(r'"stop_?reason"\s*:\s*"([^"]*)"'),
        "usage": usage,
    }


def debug_record(model, raw, reason, timeout=None, exit_code=None):
    """One failed attempt, described well enough to diagnose without the run.

    Redaction happens BEFORE the cut, so a key cannot survive as a fragment at
    the 2 KB boundary.
    """
    meta = extract_meta(raw)
    return {
        "model": model,
        "reason": reason,
        "parse_error": classify_reply(raw),
        "finish_reason": meta["finish_reason"],
        "stop_reason": meta["stop_reason"],
        "usage": meta["usage"],
        "timeout": timeout,
        "exit_code": exit_code,
        "raw": redact(raw or "")[:DEBUG_RAW_LIMIT],
    }


def ladder_models(transport=None, environ=None, log=None):
    """The configured ladder, in order -- the SAME list the walk walks.

    Read from config/opencode.json (the selector's own bookkeeping file) so the
    summariser cannot drift into a second, private model list. QUOTA cooldowns are
    deliberately NOT consulted: the walk has just recorded `quota_or_rate_limited`
    for these rungs, and the point of a fresh, small request is that the next
    minute's allowance is not the one that ran out. A rung demoted by
    `record_refusal` (a rung that answered NOTHING) is reordered by
    `demoted_order`, which is a different fact about a different failure.

    THE ORDER IS BOTH GEMINI PROJECTS, THEN THE KEYED FALLBACKS, THEN THE FREE
    POOL (measured 2026-11-05: Gemini's free tier spent its minute in 13.4s and
    every OpenCode free id shared the same refusal, so one account's allowance
    ended the edition). `GEMINI_API_KEY_2` is a second project with its own
    allowance, so its two rungs sit behind the first project's two and cool on
    their own ids; with the second key unset they contribute nothing, exactly
    as before. The three HTTP providers are inserted by `llm_providers`, which
    skips any provider with no key configured -- so a repository with none of
    those secrets set gets byte-identical behaviour to before.

    EACH HTTP RUNG'S ID IS PROVEN, NOT ASSUMED (probe, 2026-11-05): the account
    serves a different set of ids than the seed guessed, so the catalogue decides
    which is sent and a provider that serves none of them contributes no rung.
    """
    import model_selector
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = model_selector.load_config(os.path.join(root, "config", "opencode.json"))
    ladder = config.get("ladder")
    if not isinstance(ladder, list):
        ladder = []
    return list(llm_providers.ordered_ladder(ladder, environ=environ,
                                             transport=transport, resolve=True,
                                             log=log))


#: Where a demoted rung is remembered. The workflow sets this to
#: state/model_cooldown.json, the one committed model-state file the digest run
#: persists; unset means "no memory", which is what a test and a local run want.
COOLDOWN_ENV = "MODEL_COOLDOWN_PATH"

#: Refusal kinds that mean THIS RUNG produced nothing: no verdict, no bytes, an
#: auth wall, a dead transport, a hit against its own clock, a CLI exit that
#: carried no answer, an empty answer. Quota is excluded -- the whole pool shares
#: that allowance, so cooling rungs for it would empty the ladder -- and so is
#: `not_configured`, which is a repository setting. RUN 42078530071: the five
#: exit-1/empty rows were missing here, so they left no health record.
DEMOTING_KINDS = frozenset({"other", "auth", "transport", "timeout",
                            "model_not_found", "refused", "failed",
                            "exit_nonzero", "empty_content"})

#: A 429 from a KEYED provider is this account's allowance ending, which is a
#: durable fact about the rung, not a shared-pool refusal. The free OpenCode
#: pool is excluded deliberately: every one of its ids draws on the same bucket,
#: so cooling them would empty the ladder (run 42144852087).
QUOTA_KINDS = frozenset({"quota_notice", "quota_or_rate_limited"})


def record_refusal(cooldown, model, kind, now=None):
    """Demote `model` after a refusal that was not about capacity.

    Two of them inside a day set a six-hour cooldown (model_selector's
    `rung_broken`); one only records the hit. Returns the row, or None.

    A KEYED provider's quota refusal cools immediately as `quota_exhausted`
    (run 42144852087): `gemini-3.5-flash-lite` answered 429, kept the front
    position, and burned the next run's first attempt on the same wall.
    """
    if cooldown is None:
        return None
    import model_selector
    if kind in QUOTA_KINDS:
        # KEYED means everything except the free OpenCode pool. `provider_of`
        # cannot answer this: gemini is deliberately a CLI rung there, and so is
        # opencode -- the difference is the ACCOUNT, not the transport.
        if str(model).split("/", 1)[0] == "opencode":
            return None
        return model_selector.record_cooldown(cooldown, model,
                                              "quota_exhausted", at=now)
    if kind not in DEMOTING_KINDS:
        return None
    return model_selector.record_cooldown(cooldown, model, "rung_broken",
                                          at=now)


def promote_rung(cooldown, model, log=None):
    """Clear a demotion because the rung just answered. True when one was cleared."""
    if not isinstance(cooldown, dict):
        return False
    row = (cooldown.get("models") or {}).get(model)
    if not isinstance(row, dict) or not row.get("until"):
        return False
    del cooldown["models"][model]
    (log or (lambda *_a: None))(
        "  ladder: %s answered, so its demotion is over now" % model)
    return True


def _hard_cooled(cooldown, model):
    """True when `model` is on a live rung_broken cooldown right now."""
    if cooldown is None:
        return False
    import model_selector
    return model_selector.on_hard_cooldown(cooldown, model)


def demoted_order(models, cooldown, now=None, log=None):
    """The ladder with a demoted rung at the BACK -- never removed.

    model_selector's invariant is that this file may reorder a ladder but never
    veto one: an edition with no summaries is worse than one written by the rung
    that failed last time, so the demoted rung is still tried once everything
    else has refused. Both ends are logged, because a run log that does not say
    why the order changed is a run log that cannot explain its own 99 seconds.
    """
    log = log or (lambda *_a: None)
    import model_selector
    now = time.time() if now is None else float(now)
    keeping, cooling = [], []
    for model in models:
        row = ((cooldown or {}).get("models") or {}).get(model)
        if model_selector.on_hard_cooldown(cooldown, model, now=now):
            cooling.append(model)
            log("  ladder: %s demoted to the back until %s (%s, %d hit(s))"
                % (model, row.get("until"), row.get("reason") or "cooled",
                   int(row.get("hits") or 0)))
        else:
            if isinstance(row, dict) and row.get("until"):
                log("  ladder: %s promoted back (its cooldown ended %s)"
                    % (model, row["until"]))
            keeping.append(model)
    return keeping + cooling


def _decode(raw):
    return (raw or b"").decode("utf-8", errors="replace")


def opencode_run(prompt, model, timeout=KEYED_REQUEST_TIMEOUT):
    """One `opencode run` for one batch. Returns its stdout text.

    The same command shape the digest walk uses (`--auto --model`), minus
    `--print-logs`: the answer is the only thing read back, and this module does
    not need the CLI's event stream to know a request finished.

    THE TIMEOUT IS ENFORCED ON THE CHILD. `communicate(timeout=...)` waits on the
    process itself, and when it expires the process is killed and reaped -- so a
    hung rung stops running at the instant the budget says it must, instead of
    being noticed only between requests. Whatever it printed before the kill is
    kept: a kill mid-answer is evidence, and the debug record carries it.
    """
    try:
        proc = subprocess.Popen(
            ["opencode", "run", "--auto", "--model", model],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        raise Refusal("%s: %s" % (model, exc), kind="runner_missing")
    try:
        out, _ = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        # BOUNDED, because a child tree that inherited the pipe can keep the
        # second call waiting far past the timeout (run 42144852087's step then
        # died at its own 8-minute cap). Whatever was printed is still kept.
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = b""
        raise Refusal("%s: no answer in %ss (killed)" % (model, timeout),
                      kind="timeout", raw=_decode(out))
    text = _decode(out)
    if proc.returncode != 0:
        raise Refusal("%s: exit %d: %s"
                      % (model, proc.returncode, text[-2000:]),
                      kind="exit_nonzero", raw=text,
                      exit_code=proc.returncode)
    if not text.strip():
        raise Refusal("%s: empty answer" % model, kind="empty_content")
    return text


def run_model(prompt, model, timeout=KEYED_REQUEST_TIMEOUT):
    """One batch, through whichever transport owns *model*.

    Two kinds of rung share this ladder and they need different clients: a
    `gemini/` or `opencode/` rung is an `opencode run`, and a `groq/`,
    `openrouter/` or `mistral/` rung is a direct HTTPS POST. The batch loop, the
    pacer and the budget arithmetic know nothing about the difference -- they
    already pass a model id and a timeout, which is exactly what both clients
    take.

    The reason words are the repository's own (`timeout`, `transport`,
    `quota_notice`, `auth`, `model_not_found`, `garbage`), so a fallback provider
    failing looks the same in the debug record as a CLI rung failing.
    """
    if not llm_providers.provider_of(model):
        return opencode_run(prompt, model, timeout=timeout)
    try:
        return llm_providers.text_for(model, prompt, timeout=timeout)
    except llm_providers.ProviderError as exc:
        raise Refusal(str(exc), kind=exc.kind, raw=exc.detail)


class Pacer:
    """Keeps a rolling per-minute INPUT-token budget, and says what it did.
    Every request is charged BEFORE it is sent. When the charge would take the
    last minute past the budget, the pacer sleeps until the oldest charge ages
    out -- which is the only honest way to wait, because the provider counts a
    rolling window, not a fixed clock minute.
    """

    def __init__(self, budget=INPUT_TOKEN_BUDGET_PER_MINUTE, window=60.0,
                 sleep=None, clock=None, log=print):
        self.budget = int(budget)
        self.window = float(window)
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._log = log
        self.charges = []          # [(monotonic time, tokens)]
        self.waits = []            # [seconds slept]
        self.deadline = None       # monotonic instant after which it stops waiting

    def _spent(self):
        now = self._clock()
        self.charges = [c for c in self.charges if now - c[0] < self.window]
        return sum(tokens for _, tokens in self.charges)

    def wait_for(self, tokens):
        """Sleep until *tokens* fit in the window. Returns the seconds slept."""
        slept = 0.0
        while self._spent() + tokens > self.budget and self.charges:
            oldest_at, _ = self.charges[0]
            needed = self.window - (self._clock() - oldest_at) + 0.05
            cap = MAX_WAIT_SECONDS - slept
            if self.deadline is not None:
                cap = min(cap, self.deadline - self._clock())
            if cap <= 0.05:
                self._log("  pacing: no time left in the job's budget; "
                          "sending without waiting")
                break
            delay = min(max(needed, 0.05), cap)
            if delay <= 0.05 and needed > 0.05:
                break          # the cap is spent; send rather than wait forever
            self._log("  pacing: %d tokens this minute would exceed the %d "
                      "budget; waiting %.0fs" % (self._spent() + tokens,
                                                 self.budget, delay))
            self._sleep(delay)
            slept += delay
            if slept >= MAX_WAIT_SECONDS:
                break
            # No charge is removed here: `_spent` drops what has aged out, so a
            # charge that was NOT covered by the sleep is still counted.
        self.charges.append((self._clock(), tokens))
        if slept:
            self.waits.append(slept)
        return slept


#: WHY a batch produced no summary, most actionable first. A model that ANSWERED
#: and could not be read is the parser's own defect and the one that cost run
#: 41995372308 its edition, so it outranks the rest: a provider refusal, a model
#: that never answered, and a runner that broke all explain themselves, while an
#: unreadable answer silently looks like "no summary available".
CAUSE_ORDER = ("unreadable_reply", "provider_refusal", "provider_blocked",
               "model_timeout", "runner_fault")


def dominant_cause(causes):
    """The most actionable cause in `causes`, or "" when there are none."""
    for cause in CAUSE_ORDER:
        if cause in causes:
            return cause
    return ""


def refusal_cause(kind, message):
    """Which cause a refused call is, for the edition's own note."""
    if kind == "timeout":
        return "model_timeout"
    if kind in ("garbage", "empty_answer"):
        # A 200 whose message carries no text is the model answering with
        # nothing, not a network fault: the reader sees an unreadable reply.
        return "unreadable_reply"
    if kind in ("quota_notice", "auth", "model_not_found", "bad_request",
                "model_rejected"):
        # All of these are the PROVIDER declining this request -- a spent
        # allowance, a refused key, an id it does not serve or cannot chat with,
        # a body it would not accept. None of them is the parser's fault, and
        # the page says "provider refusal" for each. Named here rather than
        # folded into the text heuristic because a 401 carries no "quota" word
        # at all.
        return "provider_refusal"
    if kind == "blocked" or is_blocked(message):
        # The edge refused this NETWORK, not this rung: the request never
        # reached a credential check (Groq's 403 body is identical with no key
        # at all). Reading it as runner_fault blamed our runner for a provider
        # decision, so it gets its own cause.
        return "provider_blocked"
    if any(marker in (message or "").lower() for marker in KEY_ERROR_MARKERS):
        return "provider_refusal"
    if is_refusal(message):
        return "provider_refusal"
    if kind in ("empty_content", "not_json", "partial_json", "exit_nonzero"):
        # RUN 42078530071: a CLI that exits 1 after printing half an array is an
        # UNREADABLE REPLY, not a broken runner -- the runner ran and answered.
        return "unreadable_reply"
    return "runner_fault"


def summarise(stories, models=None, run=None, sleep=None, clock=None,
              log=print, budget=INPUT_TOKEN_BUDGET_PER_MINUTE,
              budget_seconds=TOTAL_BUDGET_SECONDS, cooldown=None,
              cooldown_path=None, snippets=None):
    """Summarise *stories* in paced batches.

    Returns {"summaries": {url: text}, "model": last model that answered,
             "requests": int, "waits": [s], "refusals": [str], "debug": [dict],
             "causes": {cause: stories lost}, "attempts": int,
             "reasons": {reason: failed attempts}, "closest": dict|None}.

    Nothing here raises for a model problem: an edition with no summaries is a
    valid outcome that the caller labels, and a crash here would cost the
    edition itself. `debug` is one record per failed attempt -- model, reason,
    the provider's own metadata and a bounded, redacted copy of the raw answer --
    so a run that produced nothing can say why without being re-run. `causes`
    counts the STORIES each cause cost, because that is what the page has to
    report; the walk's attempt log is a different question and a different file.

    `attempts`, `reasons` and `closest` exist for the caller's ONE verdict line
    (run 42321136172 reported "budget spent after 12 rungs" -- 12 was the
    artifact's cap, not the number of attempts, and nothing named a reason or a
    rung). `attempts` counts every failed attempt even past DEBUG_MAX_RECORDS;
    `reasons` counts them by the module's own reason word; `closest` is the rung
    that came nearest to writing something -- most accepted rows first, then a
    rung that ANSWERED over one that went silent.

    `snippets` is the candidate index handed to `build_prompt`, so the model is
    given the publisher's description rather than a summary the validator has
    already refused.

    `cooldown` / `cooldown_path` are the demotion memory: a rung that refuses
    without a quota reason twice is moved to the back of the ladder for six
    hours, and the next run reads that back instead of paying the same ~99s.
    """
    stories = [s for s in (stories or []) if (s.get("source_url") or s.get("url"))]
    run = run or run_model
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    if cooldown_path is None:
        cooldown_path = os.environ.get(COOLDOWN_ENV) or None
    if cooldown is None and cooldown_path:
        import model_selector
        cooldown = model_selector.load_cooldown(cooldown_path)
    # THE CLOCK STARTS BEFORE THE LADDER IS BUILT (run 42144852087). Resolving
    # the ladder probes every provider's catalogue, and that cost used to sit
    # OUTSIDE the allowance: 4.5 minutes of probes plus the walk overran the
    # step's own 8-minute timeout, so nothing was written. Discovery is part of
    # the step, so it is part of the budget; when it spends the allowance the
    # loop below writes the fallback edition instead of starting a doomed call.
    started = clock()
    pacer = Pacer(budget=budget, sleep=sleep, clock=clock, log=log)
    pacer.deadline = started + float(budget_seconds)
    out = {"summaries": {}, "model": None, "requests": 0, "waits": [],
           "refusals": [], "debug": [], "causes": {},
           "attempts": 0, "reasons": {}, "closest": None,
           "budget_spent": False, "seconds": 0.0}
    #: Rungs demoted by a refusal IN THIS RUN. They are skipped for the rest of
    #: it (run 42070185644 paid the same broken rung in all three batches), but
    #: never removed outright: if every other rung refuses, the fallback is the
    #: full order, because an edition with no summaries is worse than one written
    #: late by a rung that had failed.
    dead = set()
    dirty = False

    def save_memory():
        """Persist the demotion ledger, pruned, where the caller asked for it."""
        if cooldown is None or not cooldown_path:
            return
        import model_selector
        model_selector.prune_cooldown(cooldown)
        model_selector.save_cooldown(cooldown_path, cooldown)

    def note(model, raw, reason, timeout, exit_code=None):
        """Keep one failed attempt, bounded, so the artifact can explain it."""
        # COUNTED BEFORE THE CAP. A twelve-record artifact is a size bound, not a
        # measurement, and printing its length as "after N rungs" under-reported
        # the measured run (42321136172) by an unknown number of rungs.
        out["attempts"] += 1
        out["reasons"][reason] = out["reasons"].get(reason, 0) + 1
        if len(out["debug"]) < DEBUG_MAX_RECORDS:
            out["debug"].append(debug_record(model, raw, reason,
                                             timeout=timeout,
                                             exit_code=exit_code))

    def closer(model, accepted, reason):
        """Keep the rung that came closest, so a 0-repaired run can name it."""
        answered = 1 if reason in ("ok", "no_usable_row") else 0
        row = out["closest"] or {}
        if (int(accepted), answered) > (int(row.get("accepted") or 0),
                                        int(row.get("answered") or 0)):
            out["closest"] = {"model": model, "accepted": int(accepted),
                              "answered": answered, "reason": reason or ""}

    if clock() >= pacer.deadline:
        # NO ALLOWANCE LEFT: resolving the ladder would spend the step's clock
        # on probes for calls that cannot be made. The caller writes the
        # fallback edition and says the budget was spent.
        out["budget_spent"] = True
        log("  total budget of %ds is spent before the ladder is resolved"
            % budget_seconds)
        save_memory()
        return out
    base = list(models) if models is not None else ladder_models(log=log)
    order = demoted_order(base, cooldown, log=log) if cooldown is not None \
        else list(base)

    if not stories or not base:
        if not base:
            log("summarise_stories: no model in the configured ladder; "
                "nothing is summarised")
        save_memory()
        return out

    def record(model, kind):
        """Remember a refusal; announce a demotion the moment it happens."""
        nonlocal dirty
        row = record_refusal(cooldown, model, kind)
        if row is None:
            return False
        dirty = True
        # WRITTEN IMMEDIATELY, not at the end (run 42144852087): seven rungs
        # were demoted at 01:42:31, the step was killed by its 8-minute cap,
        # and the persist step found nothing to commit. A demotion the process
        # observed has to reach the file the next run reads.
        save_memory()
        if cooldown is not None and _hard_cooled(cooldown, model):
            dead.add(model)
            # The same formatter that reorders the next run's ladder, called
            # only for its line: a demotion caused by THIS run has to be in THIS
            # run's log, not only in the next one's.
            demoted_order(base, cooldown, log=log)
            dirty = False
        return True

    plan = batches(stories)[:MAX_REQUESTS]
    for index, batch in enumerate(plan, 1):
        if pacer.deadline - clock() <= 0:
            out["budget_spent"] = True
            log("  total budget of %ds is spent before batch %d/%d"
                % (budget_seconds, index, len(plan)))
            break
        if dirty and cooldown is not None:
            # RECOMPUTED BETWEEN BATCHES: a rung that refused twice during the
            # last one is reordered (and skipped, below) for the rest of this run.
            order = demoted_order(base, cooldown, log=log)
            dirty = False
        active = [m for m in order if m not in dead] or list(order)
        prompt = build_prompt(batch, snippets)
        prompt_tokens = estimate_tokens(prompt)
        log("  batch %d/%d: %d stories, prompt ~%d tokens (budget %d/min)"
            % (index, len(plan), len(batch), prompt_tokens, budget))
        pacer.wait_for(prompt_tokens)
        answered = False
        ran_out = False
        causes = set()
        for rung, model in enumerate(active):
            remaining = pacer.deadline - clock()
            if remaining <= 0:
                ran_out = True
                break
            # A CALL GIVEN LESS THAN THE RUNG NEEDS IS A GUARANTEED `timeout`,
            # not an attempt. Run 42356298224: a ten-rung ladder cut each slice
            # to ~30s and six of fifteen attempts died on the clock, including
            # one on a model that answered from the lead's 90s slice. Stopping
            # here leaves the clock for a rung that can still answer.
            need = min_answer_seconds(model)
            if remaining < need:
                ran_out = True
                log("  batch %d/%d: %.0fs left, less than the %ds %s needs; "
                    "not starting a call that cannot answer"
                    % (index, len(plan), remaining, need, model))
                break
            # THE CLOCK THE CHILD IS ACTUALLY GIVEN, recomputed per call: what
            # this batch may use, and never more than the job has left. A
            # provider's several served ids share ONE rung's slice, so the
            # multi-id rung cannot outlive the rung it belongs to.
            timeout = rung_timeout(active, rung, remaining,
                                   len(plan) - index + 1)
            try:
                text = run(prompt, model, timeout=timeout)
                out["requests"] += 1
            except Refusal as exc:
                out["requests"] += 1
                out["refusals"].append(str(exc).strip()[:300])
                kind = getattr(exc, "kind", "") or (
                    "refused" if is_refusal(str(exc)) else "failed")
                if kind == "not_configured":
                    # A ladder built before the key was removed, or a caller
                    # that named a provider directly. Silence is the contract:
                    # this is a repository setting, not a model failure, and
                    # counting it as one would page the owner about a secret.
                    continue
                note(model, getattr(exc, "raw", "") or str(exc), kind, timeout,
                     exit_code=getattr(exc, "exit_code", None))
                closer(model, 0, kind)
                cause = refusal_cause(kind, str(exc))
                causes.add(cause)
                if cause == "provider_blocked":
                    # A block on this network is not a fact about this rung:
                    # cooling it would punish a healthy key for the provider's
                    # decision, and a wait cannot lift an IP policy.
                    log("  batch %d/%d: %s was blocked at the edge; trying the "
                        "next model" % (index, len(plan), model))
                    continue
                if record(model, kind):
                    log("  batch %d/%d: %s produced nothing (%s); recorded for "
                        "the ladder memory" % (index, len(plan), model, kind))
                if kind == "quota_notice" and llm_providers.provider_of(model):
                    # A 429 from a keyed provider is THIS ACCOUNT'S capacity, not
                    # a wrong id: waiting would keep the batch on a rung already
                    # refused. Record it and move on.
                    log("  batch %d/%d: %s is rate-limited (429); trying the "
                        "next model" % (index, len(plan), model))
                    continue
                if not is_refusal(str(exc)):
                    log("  batch %d/%d: %s %s within %ds (%s); trying the next "
                        "model" % (index, len(plan), model, kind, timeout,
                                   str(exc).strip()[:120]))
                    continue
                hint = retry_hint_seconds(str(exc))
                delay = DEFAULT_WAIT_SECONDS if hint is None else hint
                if delay > MAX_WAIT_SECONDS:
                    log("  batch %d/%d: %s refused and asked for %ds, more "
                        "than the %ds bound; trying the next model"
                        % (index, len(plan), model, delay, MAX_WAIT_SECONDS))
                    continue
                # A SLOW CALL HAS EATEN SOME OF THE CLOCK since `remaining` was
                # read, so the wait is clamped against a fresh reading: a 70s
                # refusal followed by a stale 100s clamp would overshoot the
                # allowance, and the allowance is what keeps the writer alive.
                left = pacer.deadline - clock()
                delay = min(delay, left)
                if delay <= 0:
                    ran_out = True
                    break
                log("  batch %d/%d: %s refused (%s); honouring the provider's "
                    "hint: waiting %ds"
                    % (index, len(plan), model,
                       "quota" if hint is not None else "no hint", delay))
                sleep(delay)
                out["waits"].append(delay)
                retry_timeout = call_timeout(model, pacer.deadline - clock(),
                                             len(plan) - index + 1, 1)
                group = rung_group_size(active, rung)
                if group > 1:
                    # The retry is still this rung's time, not a fresh budget.
                    retry_timeout = max(1, int(retry_timeout / group))
                try:
                    text = run(prompt, model, timeout=retry_timeout)
                    out["requests"] += 1
                except Refusal as again:
                    out["requests"] += 1
                    out["refusals"].append(str(again).strip()[:300])
                    note(model, getattr(again, "raw", "") or str(again),
                         getattr(again, "kind", "") or "refused", retry_timeout,
                         exit_code=getattr(again, "exit_code", None))
                    closer(model, 0, getattr(again, "kind", "") or "refused")
                    again_cause = refusal_cause(
                        getattr(again, "kind", "") or "refused", str(again))
                    causes.add(again_cause)
                    if again_cause == "provider_blocked":
                        continue
                    if record(model, getattr(again, "kind", "") or "refused"):
                        log("  batch %d/%d: %s produced nothing after the wait; "
                            "recorded for the ladder memory"
                            % (index, len(plan), model))
                    log("  batch %d/%d: %s refused again after the wait; "
                        "trying the next model" % (index, len(plan), model))
                    continue
            except Exception as exc:      # a runner fault must not cost the edition
                out["requests"] += 1
                out["refusals"].append(str(exc).strip()[:300])
                note(model, str(exc), "runner_error", timeout)
                closer(model, 0, "runner_error")
                causes.add("runner_fault")
                log("  batch %d/%d: %s errored (%s); trying the next model"
                    % (index, len(plan), model, str(exc).strip()[:120]))
                continue
            got = parse_reply(text, batch)
            if not got:
                reason = classify_reply(text, batch)
                note(model, text, reason, timeout)
                closer(model, 0, reason)
                causes.add("unreadable_reply")
                log("  batch %d/%d: %s answered but gave no usable summary "
                    "(%s)" % (index, len(plan), model, reason))
                continue
            out["summaries"].update(got)
            closer(model, len(got), "ok")
            out["model"] = model
            answered = True
            if promote_rung(cooldown, model, log=log):
                # A rung that answered is no longer demoted; the file the next
                # run reads must say so now, not only when this step finishes.
                save_memory()
            log("  batch %d/%d ok: %d of %d stories summarised by %s"
                % (index, len(plan), len(got), len(batch), model))
            break
        if not answered and causes:
            cause = dominant_cause(causes)
            out["causes"][cause] = out["causes"].get(cause, 0) + len(batch)
            log("  batch %d/%d: %d stories lost to %s"
                % (index, len(plan), len(batch), cause))
        if ran_out:
            out["budget_spent"] = True
            log("  the job's %ds summarising budget is spent; the remaining "
                "stories keep the publisher's text" % budget_seconds)
            break
        if not answered:
            log("  batch %d/%d: no summary for these %d stories -- they keep "
                "the publisher's text" % (index, len(plan), len(batch)))

    out["waits"].extend(pacer.waits)
    out["seconds"] = round(max(0.0, clock() - started), 1)
    save_memory()
    log("summarise_stories: %d of %d stories summarised in %d request(s), "
        "%d wait(s), model %s"
        % (len(out["summaries"]), len(stories), out["requests"],
           len(out["waits"]), out["model"] or "none"))
    return out


def main(argv=None):
    """Summarise a state/stories.json in place (used by hand and by tests)."""
    import argparse
    ap = argparse.ArgumentParser(description="Batched AI summaries for an edition.")
    ap.add_argument("--stories", default="state/stories.json")
    ap.add_argument("--out", default="state/summaries.json")
    args = ap.parse_args(argv)

    try:
        with open(args.stories, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print("FATAL: cannot read %s: %s" % (args.stories, exc))
        return 1
    stories = payload.get("stories") if isinstance(payload, dict) else payload
    if not isinstance(stories, list) or not stories:
        print("FATAL: %s carries no stories" % args.stories)
        return 1

    result = summarise(stories)
    payload_out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": result["model"],
        "requests": result["requests"],
        "counts": {"summarised": len(result["summaries"]),
                   "total": len(stories)},
        "summaries": result["summaries"],
    }
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload_out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, args.out)
    print("wrote %s (%d of %d stories summarised)"
          % (args.out, len(result["summaries"]), len(stories)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
