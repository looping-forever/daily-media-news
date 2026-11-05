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
)

PROMPT = (
    "You are summarising news stories for a personal morning digest.\n"
    "For each story below, write ONE plain sentence (at most 40 words) that "
    "says what happened, using only the title and snippet given.\n"
    "Never invent a fact, a number, a name or a quote that is not in the input. "
    "If a story has too little text to summarise, use an empty string.\n\n"
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


def call_timeout(model, seconds_left, batches_left, rungs_left):
    """Seconds one call to *model* is granted, and why it is that number.

    THE ARITHMETIC THAT HAS TO FIT (run 41990956737: 29 stories, so four
    batches, TWO keyed rungs and THREE free rungs, 300s):

        2 * 120 + 3 * 45 = 375s   >   300s

    so the ladder alone can outlive the job, and a single hanging rung used to
    spend 120s that a later batch needed. Three bounds make it fit:

        reserve  MIN_BATCH_SECONDS per batch still to come
        share    (seconds_left - reserve) / rungs_left
        cap      min(rung cap, share), floored at MIN_RUNG_SECONDS,
                 never more than seconds_left

    With four batches and 300s: batch 1 reserves 90s, so its five rungs share
    210s -> 42s each instead of 120s, and batches 2-4 still get a turn.
    """
    batches_left = max(1, int(batches_left))
    rungs_left = max(1, int(rungs_left))
    reserve = (batches_left - 1) * MIN_BATCH_SECONDS
    usable = max(MIN_RUNG_SECONDS, float(seconds_left) - reserve)
    share = usable / rungs_left
    cap = min(timeout_for_rung(model), max(MIN_RUNG_SECONDS, share))
    return max(1, int(min(cap, float(seconds_left))))


def estimate_tokens(text):
    """Approximate input tokens for *text* (pessimistic, never zero)."""
    return max(1, (len(text or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def is_refusal(text):
    """True when *text* is a spent allowance rather than a broken request."""
    low = (text or "").lower()
    return any(pattern in low for pattern in REFUSAL_PATTERNS)


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


def build_prompt(batch):
    """The whole request for one batch: short fields only, never full text.

    `snippet` is the collector's trimmed description, not the article, so a
    request carries no more than it must. The url is the key the answer is
    matched back on, which is why it is repeated in the prompt rather than
    implied by position.
    """
    items = []
    for story in batch:
        items.append({
            "url": story.get("source_url") or story.get("url") or "",
            "title": story.get("title") or "",
            "snippet": (story.get("summary") or "")[:400],
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


def parse_reply(text, batch):
    """{url: summary} for the entries this module is willing to print.

    Anything the model returned that is not in the batch it was asked about is
    dropped: a url it invented has no story to attach to, and a url from
    another batch would silently overwrite that batch's own answer.
    """
    body = (text or "").strip()
    start, end = body.find("["), body.rfind("]")
    if start >= 0 and end > start:
        body = body[start:end + 1]
    try:
        rows = json.loads(body)
    except (TypeError, ValueError):
        return {}
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
    body = (text or "").strip()
    if not body:
        return "empty_content"
    if _truncation_marker(body):
        return "truncated_at_max_tokens"
    start, end = body.find("["), body.rfind("]")
    if start >= 0 and end < start:
        return "partial_json"
    if start < 0:
        return "not_json"
    try:
        rows = json.loads(body[start:end + 1])
    except (TypeError, ValueError):
        return "not_json"
    if not isinstance(rows, list):
        return "not_json"
    if batch is None:
        return "ok" if rows else "no_usable_row"
    return "ok" if parse_reply(body, batch) else "no_usable_row"


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


def ladder_models():
    """The configured ladder, in order -- the SAME list the walk walks.

    Read from config/opencode.json (the selector's own bookkeeping file) so the
    summariser cannot drift into a second, private model list. Cooldowns are
    deliberately NOT consulted: the walk has just recorded `quota_or_rate_limited`
    for these rungs, and the point of a fresh, small request is that the next
    minute's allowance is not the one that ran out.
    """
    import model_selector
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = model_selector.load_config(os.path.join(root, "config", "opencode.json"))
    ladder = config.get("ladder")
    if not isinstance(ladder, list):
        return []
    return [m for m in ladder if isinstance(m, str) and m]


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
        out, _ = proc.communicate()
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


def summarise(stories, models=None, run=None, sleep=None, clock=None,
              log=print, budget=INPUT_TOKEN_BUDGET_PER_MINUTE,
              budget_seconds=TOTAL_BUDGET_SECONDS):
    """Summarise *stories* in paced batches.

    Returns {"summaries": {url: text}, "model": last model that answered,
             "requests": int, "waits": [s], "refusals": [str], "debug": [dict]}.

    Nothing here raises for a model problem: an edition with no summaries is a
    valid outcome that the caller labels, and a crash here would cost the
    edition itself. `debug` is one record per failed attempt -- model, reason,
    the provider's own metadata and a bounded, redacted copy of the raw answer --
    so a run that produced nothing can say why without being re-run.
    """
    stories = [s for s in (stories or []) if (s.get("source_url") or s.get("url"))]
    run = run or opencode_run
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    models = list(models) if models is not None else ladder_models()
    pacer = Pacer(budget=budget, sleep=sleep, clock=clock, log=log)
    pacer.deadline = clock() + float(budget_seconds)
    out = {"summaries": {}, "model": None, "requests": 0, "waits": [],
           "refusals": [], "debug": []}

    def note(model, raw, reason, timeout, exit_code=None):
        """Keep one failed attempt, bounded, so the artifact can explain it."""
        if len(out["debug"]) < DEBUG_MAX_RECORDS:
            out["debug"].append(debug_record(model, raw, reason,
                                             timeout=timeout,
                                             exit_code=exit_code))

    if not stories or not models:
        if not models:
            log("summarise_stories: no model in the configured ladder; "
                "nothing is summarised")
        return out

    plan = batches(stories)[:MAX_REQUESTS]
    for index, batch in enumerate(plan, 1):
        if pacer.deadline - clock() <= 0:
            log("  total budget of %ds is spent before batch %d/%d"
                % (budget_seconds, index, len(plan)))
            break
        prompt = build_prompt(batch)
        prompt_tokens = estimate_tokens(prompt)
        log("  batch %d/%d: %d stories, prompt ~%d tokens (budget %d/min)"
            % (index, len(plan), len(batch), prompt_tokens, budget))
        pacer.wait_for(prompt_tokens)
        answered = False
        ran_out = False
        for rung, model in enumerate(models):
            remaining = pacer.deadline - clock()
            if remaining <= 0:
                ran_out = True
                break
            # THE CLOCK THE CHILD IS ACTUALLY GIVEN, recomputed per call: what
            # this batch may use, and never more than the job has left.
            timeout = call_timeout(model, remaining,
                                   len(plan) - index + 1, len(models) - rung)
            try:
                text = run(prompt, model, timeout=timeout)
                out["requests"] += 1
            except Refusal as exc:
                out["requests"] += 1
                out["refusals"].append(str(exc).strip()[:300])
                kind = getattr(exc, "kind", "") or (
                    "refused" if is_refusal(str(exc)) else "failed")
                note(model, getattr(exc, "raw", "") or str(exc), kind, timeout,
                     exit_code=getattr(exc, "exit_code", None))
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
                delay = min(delay, remaining)
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
                try:
                    text = run(prompt, model, timeout=retry_timeout)
                    out["requests"] += 1
                except Refusal as again:
                    out["requests"] += 1
                    out["refusals"].append(str(again).strip()[:300])
                    note(model, getattr(again, "raw", "") or str(again),
                         getattr(again, "kind", "") or "refused", retry_timeout,
                         exit_code=getattr(again, "exit_code", None))
                    log("  batch %d/%d: %s refused again after the wait; "
                        "trying the next model" % (index, len(plan), model))
                    continue
            except Exception as exc:      # a runner fault must not cost the edition
                out["requests"] += 1
                out["refusals"].append(str(exc).strip()[:300])
                note(model, str(exc), "runner_error", timeout)
                log("  batch %d/%d: %s errored (%s); trying the next model"
                    % (index, len(plan), model, str(exc).strip()[:120]))
                continue
            got = parse_reply(text, batch)
            if not got:
                reason = classify_reply(text, batch)
                note(model, text, reason, timeout)
                log("  batch %d/%d: %s answered but gave no usable summary "
                    "(%s)" % (index, len(plan), model, reason))
                continue
            out["summaries"].update(got)
            out["model"] = model
            answered = True
            log("  batch %d/%d ok: %d of %d stories summarised by %s"
                % (index, len(plan), len(got), len(batch), model))
            break
        if ran_out:
            log("  the job's %ds summarising budget is spent; the remaining "
                "stories keep the publisher's text" % budget_seconds)
            break
        if not answered:
            log("  batch %d/%d: no summary for these %d stories -- they keep "
                "the publisher's text" % (index, len(plan), len(batch)))

    out["waits"].extend(pacer.waits)
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
