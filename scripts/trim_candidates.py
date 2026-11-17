#!/usr/bin/env python3
"""trim_candidates.py -- the candidate payload the MODEL reads, cut to a budget.

WHY THIS EXISTS (measured, not guessed)
---------------------------------------
Dry run 44031039679 hit the free tier's own ceiling:

    Quota exceeded for metric:
    generativelanguage.googleapis.com/generate_content_free_tier_input_token_count,
    limit: 250000 ... input tokens per minute limit exceeded on Free Tier

The walk's first attempt wrote 2 stories and the second attempt (same minute)
died on that wall. Nothing about model choice or output quality: an AGENTIC
attempt re-sends its whole context on every turn, so the input bill is
`turns x payload`, and a full `state/candidates.json` makes every turn expensive.
Two attempts in one minute cannot fit under 250k input tokens when one attempt
already spends most of it.

So the model-facing payload is capped here. Nothing else shrinks:
`state/candidates.json` keeps EVERY collected candidate, and every downstream
step (validation, media passthrough, the derive fallback, the audit upload)
still reads the full file. Only the file the agent is told to read is trimmed.

WHAT SHRINKS, IN ORDER OF ATTACK
--------------------------------
1. Fields the prompt never uses are dropped -- `source_results`, `weather` and
   `weather_advice` are not in the prompt's declared shape and the model is
   never asked to use them; `sources_ok` / `sources_failed` / `weather_summary`
   are what it reads.
2. `snippet` is truncated to MODEL_SNIPPET_MAX_CHARS with an explicit ellipsis,
   cut on a word boundary. The collector's own ceiling is 300 characters.
3. The candidate COUNT is capped at MODEL_MAX_CANDIDATES, and the selection is
   deterministic and defensible: freshest first, round-robin across
   `domain_category`, so every domain keeps a seat before any domain gets a
   second one. Taking the first N would hand the model one loud domain and hide
   the quiet ones it is meant to report on.

The whole thing is asserted against a stated character budget in
tests/digest/test_walk_payload_budget.py, measured from the RENDERED prompt
(`private_values.substitute_tokens`), not from the template.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

#: The cap on candidates handed to the model. Chosen from the measurement: at
#: MODEL_SNIPPET_MAX_CHARS a real candidate costs roughly 1050 characters of JSON
#: (the Google News redirect URL alone is ~318), so 40 is about 42k characters.
#: It also keeps the model-facing file one single `Read`: OpenCode's read tool
#: caps at 2000 lines, and a 140-candidate file with media blocks is ~2300 lines,
#: which forces a second overlapping read and pays for the whole file again.
MODEL_MAX_CANDIDATES = 40

#: The snippet cap for the model. The prompt decides from the snippet, and the
#: first ~160 characters carry the lede; 300 is the collector's ceiling and is
#: more than the classification step can use.
MODEL_SNIPPET_MAX_CHARS = 160

#: Titles are already capped at 300 by the collector. This is a second, lower
#: bound so a pathological headline cannot eat a candidate's whole allowance.
MODEL_TITLE_MAX_CHARS = 220

#: Appended to any text this module cuts, so a truncated field is never mistaken
#: for the publisher's own wording.
ELLIPSIS = "\u2026"

#: Top-level keys the model actually reads (the prompt's declared shape, plus
#: `sources_ok` / `sources_failed`). Everything else is dropped.
TOP_LEVEL_FIELDS = ("collected_at", "sources_ok", "sources_failed",
                    "weather_summary", "candidates")

#: Candidate fields the model reads. `image` / `video` are carried VERBATIM
#: because the prompt's passthrough rule copies them and the validator matches
#: media by URL against the FULL candidates.json, which is never trimmed.
CANDIDATE_FIELDS = ("title", "url", "source", "published", "snippet",
                    "domain_category", "language", "image", "video")

#: Characters the rendered prompt PLUS the model-facing candidates file may not
#: exceed. WHY THIS NUMBER. The free tier allows 250,000 input tokens per minute
#: and an agentic attempt re-sends its whole context every turn, so one minute
#: holds `2 attempts x turns x this message`. At 80,000 characters (about 20,000
#: estimated tokens) and a conservative 6 turns per attempt, two attempts cost
#: roughly 240,000 -- inside the wall, with the retry included. Measured before
#: this cap, on real feed text at the collector's 12-per-domain ceiling: a
#: 144-candidate payload was 179,827 characters (about 45,000 tokens) per
#: message, which is the wall in three turns. After it: 70,053 characters,
#: about 17,500 tokens.
MODEL_PAYLOAD_CHAR_BUDGET = 80000


def estimate_tokens(chars):
    """Characters -> input tokens. The usual 4-characters-per-token estimate."""
    return (int(chars) + 3) // 4


def clip_text(text, cap):
    """Cut `text` to `cap` characters on a word boundary, marking the cut.

    Returns the text unchanged when it already fits, so a short field is never
    touched. The ellipsis is added ON TOP of `cap` (the result can be
    `cap + 1` long), so the retained wording is always the requested length.
    """
    text = "" if text is None else str(text)
    cap = max(0, int(cap))
    if len(text) <= cap:
        return text
    window = text[:cap]
    cut = window.rfind(" ")
    # A single unbroken word longer than the cap has no boundary to respect; cut
    # it at the cap rather than returning nothing. Still explicit, still marked.
    if cut < cap // 2:
        cut = cap
    return window[:cut].rstrip() + ELLIPSIS


def _category(cand):
    cat = str(cand.get("domain_category") or "").strip()
    return cat or "uncategorised"


def _freshness(cand):
    """Sort key: newer is larger. An unknown or unparseable date sorts LAST."""
    raw = cand.get("published")
    if not raw:
        return float("-inf")
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return float("-inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def select_candidates(candidates, cap):
    """At most `cap` candidates: freshest first, round-robin across domains.

    Deterministic by construction:
      * inside a domain, freshest first, ties broken by the collector's order;
      * domains take turns, and the domain holding the freshest candidate goes
        first;
      * the collector's order is restored on the way out, so the model still
        sees the ranking it arrived with.

    Taking the first `cap` rows instead would let one loud domain fill the
    payload and hide the others -- which is exactly the failure the prompt's
    "never claim coverage beyond what you were given" is about.
    """
    cands = [c for c in (candidates or []) if isinstance(c, dict)]
    cap = max(0, int(cap))
    if len(cands) <= cap:
        return list(cands)
    if cap == 0:
        return []

    buckets = {}
    for idx, cand in enumerate(cands):
        buckets.setdefault(_category(cand), []).append((idx, cand))
    for rows in buckets.values():
        rows.sort(key=lambda pair: (_freshness(pair[1]), -pair[0]), reverse=True)
    order = sorted(buckets,
                   key=lambda cat: (_freshness(buckets[cat][0][1]),
                                    -buckets[cat][0][0]),
                   reverse=True)

    kept = []
    rank = 0
    while len(kept) < cap:
        took_any = False
        for cat in order:
            rows = buckets[cat]
            if rank >= len(rows):
                continue
            kept.append(rows[rank])
            took_any = True
            if len(kept) >= cap:
                break
        if not took_any:
            break
        rank += 1
    kept.sort(key=lambda pair: pair[0])
    return [cand for _idx, cand in kept]


def trim_candidate(cand):
    """One candidate, reduced to the fields the model reads."""
    out = {}
    for key in CANDIDATE_FIELDS:
        if key not in cand or cand[key] is None:
            continue
        value = cand[key]
        if key == "snippet":
            out[key] = clip_text(value, MODEL_SNIPPET_MAX_CHARS)
        elif key == "title":
            out[key] = clip_text(value, MODEL_TITLE_MAX_CHARS)
        else:
            out[key] = value
    return out


def trim_payload(payload, cap=None, snippet_cap=None):
    """The model-facing copy of a candidates.json payload.

    The input is never mutated. `candidates_total` and the caps are recorded so
    the model (and the log) can see that this is a subset, not the whole supply.
    """
    if not isinstance(payload, dict):
        raise ValueError("candidates payload must be a JSON object")
    cands = payload.get("candidates")
    if not isinstance(cands, list):
        raise ValueError("candidates payload has no 'candidates' list")

    cap = MODEL_MAX_CANDIDATES if cap is None else cap
    snippet_cap = MODEL_SNIPPET_MAX_CHARS if snippet_cap is None else snippet_cap

    chosen = select_candidates(cands, cap)
    out = {}
    for key in TOP_LEVEL_FIELDS:
        if key == "candidates":
            continue
        if key in payload:
            out[key] = payload[key]
    rows = []
    for cand in chosen:
        row = trim_candidate(cand)
        if snippet_cap is not None and "snippet" in row:
            row["snippet"] = clip_text(row["snippet"], snippet_cap)
        rows.append(row)
    out["candidates"] = rows
    out["candidates_total"] = len(cands)
    out["candidates_model_cap"] = cap
    out["snippet_cap_chars"] = snippet_cap
    return out


def payload_chars(payload):
    """The exact character count of the file the model reads."""
    return len(json.dumps(payload, ensure_ascii=False, indent=2))


def rendered_prompt_chars(prompt_path=None):
    """Characters of the prompt AS THE MODEL RECEIVES IT.

    The committed prompt carries `{{TOKEN}}` placeholders that `render_prompt.py`
    resolves before `opencode run`, so measuring the template would understate
    the real payload. Imported lazily so this module stays runnable on its own.
    """
    if prompt_path is None:
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "daily-digest.md")
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import private_values  # noqa: E402  (deliberate: after the sys.path insert)
    with open(prompt_path, "r", encoding="utf-8") as handle:
        return len(private_values.substitute_tokens(handle.read()))


def total_payload_chars(payload, prompt_path=None):
    """Rendered prompt + trimmed candidates file: what one message costs."""
    return rendered_prompt_chars(prompt_path) + payload_chars(payload)


def measure(payload, prompt_path=None):
    """The numbers a report or a test needs, from one estimator."""
    pchars = payload_chars(payload)
    total = total_payload_chars(payload, prompt_path)
    return {
        "prompt_chars": rendered_prompt_chars(prompt_path),
        "candidates_chars": pchars,
        "total_chars": total,
        "total_est_tokens": estimate_tokens(total),
        "budget_chars": MODEL_PAYLOAD_CHAR_BUDGET,
        "within_budget": total <= MODEL_PAYLOAD_CHAR_BUDGET,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_path", default="state/candidates.json")
    ap.add_argument("--out", dest="out_path",
                    default="state/candidates_model.json")
    ap.add_argument("--cap", type=int, default=MODEL_MAX_CANDIDATES)
    ap.add_argument("--snippet-cap", type=int, default=MODEL_SNIPPET_MAX_CHARS)
    args = ap.parse_args(argv)

    with open(args.in_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    before = len(payload.get("candidates") or [])
    kept = trim_payload(payload, args.cap, args.snippet_cap)

    out_dir = os.path.dirname(os.path.abspath(args.out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = args.out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(kept, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, args.out_path)

    stats = measure(kept)
    print("trim_candidates: %d -> %d candidate(s) (cap %d, snippet %d) -> %s"
          % (before, len(kept["candidates"]), args.cap, args.snippet_cap,
             args.out_path))
    print("trim_candidates: prompt %d + candidates %d = %d chars, ~%d tokens "
          "(budget %d chars: %s)"
          % (stats["prompt_chars"], stats["candidates_chars"],
             stats["total_chars"], stats["total_est_tokens"],
             stats["budget_chars"],
             "ok" if stats["within_budget"] else "OVER"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
