#!/usr/bin/env python3
"""summarise_stubs.py -- write the summaries a model-written edition skipped.

RUN 42056457121 (2026-11-05): the agent's payload passed validation, so the
deriver -- the only stage that ever called the summariser -- never ran. Four
stories shipped `Reported: <their own headline>`, the validator counted them as
written, and the page, the subject and the push all said nothing about it.

This step runs AFTER validation on every shipping path and repairs exactly that:

  * a story whose summary is a stub (scripts/validate_stories.py:is_real_summary
    -- empty, too short, the headline, or the publisher's snippet copied) is
    given to the provider ladder, with the same budget rules as the deriver;
  * a real summary is NEVER rewritten;
  * a story no model answers for keeps the publisher's own description
    (derive_unsummarised.lead_text -- quoted, never invented) and
    `summary_counts` says how many really were AI-written;
  * a story with NEITHER a written summary nor publisher text is dropped while
    the edition still clears STORIES_FLOOR, and otherwise keeps its own
    non-empty body -- an empty summary is never written (run 42074891307).

A DERIVED edition is repaired too (run 42070185644): its writer rung is
`derived`, but the deriver's labelling used to be the only decision made about
its text, so a stub it wrote had no second stage to catch it. The whole step
shares ONE allowance (summarise_stories.TOTAL_BUDGET_SECONDS), so a fifteen-story
edition cannot hang the job; if the allowance cannot cover every stub, the
edition's own priority order decides which ones are attempted and the count says
honestly how many were written.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import derive_unsummarised  # noqa: E402
import edition_kind  # noqa: E402
import validate_stories  # noqa: E402


def _url(story):
    return story.get("source_url") or story.get("url") or ""


def stubs(stories, snippets):
    """The stories whose summary is not prose, in payload order."""
    return [s for s in (stories or [])
            if isinstance(s, dict)
            and not validate_stories.is_real_summary(
                s.get("summary"), s.get("title"),
                (snippets or {}).get(_url(s), ""))]


def _by_importance(stories):
    """Most important first, keeping payload order for equal priorities."""
    return sorted(stories, key=lambda s: -int(s.get("priority") or 0))


def _ladder(stories, budget_seconds=None):
    import summarise_stories
    return summarise_stories.summarise(stories, budget_seconds=budget_seconds)


def _without_count_sentence(notes):
    """Drop the sentences the counters just replaced, keeping the rest."""
    text = str(notes or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences
            if edition_kind.NOTE_COUNT_MARKER not in s
            and not s.startswith("Fallback edition: no AI summary survived")]
    return " ".join(kept).strip()


def _floor(value=None):
    """The workflow's own STORIES_FLOOR, or the shipped default of 4."""
    if value is not None:
        return int(value)
    try:
        return int(os.environ.get("STORIES_FLOOR") or 4)
    except (TypeError, ValueError):
        return 4


def repair(payload, snippets=None, summarise=None, log=print, derived=False,
           budget_seconds=None, floor=None, debug_path=None):
    """Summarise the stubs in place; returns the counts this step reached.

    `summarise` is injected so a test never calls a provider: it takes the list
    of stub stories (most important first) and `budget_seconds`, and returns the
    summariser's own {"summaries": {url: text}}.

    RUN 42074891307: with no publisher text to fall back to, this step wrote ""
    over the story's own body and the push printed a heading with nothing under
    it. A stub with no publisher text is now DROPPED while the edition still
    clears its floor, and otherwise KEPT with its own non-empty body and marked
    unsummarised -- never an empty string, never invented prose.
    """
    import summarise_stories
    snippets = snippets or {}
    minimum = _floor(floor)
    spend = (summarise_stories.TOTAL_BUDGET_SECONDS
             if budget_seconds is None else budget_seconds)
    stories = payload.get("stories") or []
    found = _by_importance(stubs(stories, snippets))
    if not found:
        log("summarise_stubs: every story already has a written summary")
        return {"stubs": 0, "written": 0, "publisher": 0, "none": 0,
                "dropped": 0}

    log("summarise_stubs: %d of %d stories have no written summary; asking the "
        "ladder (allowance %ds)" % (len(found), len(stories), spend))
    result = (summarise or _ladder)(found, budget_seconds=spend)
    summaries = result.get("summaries") or {}
    model = str(result.get("model") or "")
    written = publisher = none = dropped = 0
    rejected = []
    stub_ids = {id(story) for story in found}
    kept = []
    for index, story in enumerate(stories):
        if id(story) not in stub_ids:
            kept.append(story)
            continue
        text = str(summaries.get(_url(story)) or "").strip()
        snippet = snippets.get(_url(story), "")
        if text and validate_stories.is_real_summary(
                text, story.get("title"), snippet):
            story["summary"] = text
            story["summary_source"] = "ai"
            written += 1
            kept.append(story)
            continue
        if text:
            # A received answer the predicate refused: keep the evidence, so a
            # "0 written" run can be diagnosed instead of guessed at.
            rejected.append({
                "title": str(story.get("title") or ""),
                "model": model,
                "text": derive_unsummarised.clip_text(text),
                "reason": validate_stories.stub_reason(
                    text, story.get("title"), snippet),
            })
        # NO MODEL, NO INVENTED SENTENCE. The publisher's own description is
        # quoted verbatim when the feed carried one.
        fallback = derive_unsummarised.lead_text(
            snippet, story.get("title"), story.get("category"))
        if fallback:
            story["summary"] = fallback
            story["summary_source"] = "publisher"
            publisher += 1
            kept.append(story)
            continue
        # NOTHING TO QUOTE. An empty body is what run 42074891307 shipped, so the
        # story leaves the edition while the floor still holds.
        remaining = len(stories) - index - 1
        if len(kept) + remaining >= minimum:
            dropped += 1
            continue
        # The floor forbids dropping it, so it keeps its own previous text (a
        # non-empty body) -- or its headline, which is true but not a summary.
        body = (str(story.get("summary") or "").strip()
                or str(story.get("title") or "").strip()
                or "No summary available.")
        story["summary"] = body
        story["summary_source"] = "none"
        none += 1
        kept.append(story)

    if dropped:
        payload["stories"] = kept
    if rejected or result.get("debug"):
        derive_unsummarised.record_rejections(
            debug_path, rejected, attempts=result.get("debug") or [])

    facts = validate_stories.edition_facts(payload, kept, snippets=snippets)
    payload["edition_kind"] = facts["edition_kind"]
    payload["summary_counts"] = facts["summary_counts"]
    existing = _without_count_sentence(payload.get("notes"))
    if derived:
        # The deriver's sentence quoted ITS count; the repair just changed it,
        # so the sentence is rebuilt from the same numbers the payload now
        # carries rather than left to describe an edition that no longer exists.
        sentence = validate_stories.rewritten_note(facts, payload.get("notes"))
    else:
        sentence = edition_kind.model_note(
            facts["edition_kind"],
            facts["summary_counts"]["summarised"],
            facts["summary_counts"]["total"],
            facts["summary_counts"]["publisher"],
            facts["summary_counts"]["none"])
        if edition_kind.has_model_note(existing):
            sentence = ""
    if sentence:
        payload["notes"] = (sentence + " " + existing).strip()
    elif existing != str(payload.get("notes") or "").strip():
        payload["notes"] = existing
    log("summarise_stubs: replaced %d with written summaries, %d with publisher "
        "text, %d left without a summary, %d dropped; edition_kind=%s (%d of %d)"
        % (written, publisher, none, dropped, facts["edition_kind"],
           facts["summary_counts"]["summarised"],
           facts["summary_counts"]["total"]))
    return {"stubs": len(found), "written": written, "publisher": publisher,
            "none": none, "dropped": dropped}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Summarise the stubs a digest shipped.")
    ap.add_argument("--in", dest="in_path",
                    default="state/stories.validated.json")
    ap.add_argument("--candidates", default="state/candidates.json")
    args = ap.parse_args(argv)

    try:
        with open(args.in_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print("FATAL: cannot read %s: %s" % (args.in_path, exc))
        return 1
    if not isinstance(payload, dict):
        print("FATAL: %s is not a JSON object" % args.in_path)
        return 1
    writer = payload.get("writer") if isinstance(payload.get("writer"), dict) else {}
    derived = str(writer.get("rung") or "") == "derived"

    debug_path = os.path.join(os.path.dirname(os.path.abspath(args.in_path))
                              or ".", "summarise-debug.json")
    counts = repair(payload, validate_stories.snippet_index(args.candidates),
                    log=lambda line: print(line, flush=True), derived=derived,
                    debug_path=debug_path)
    if not counts["stubs"]:
        return 0

    tmp = args.in_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, args.in_path)
    print("summarise_stubs: wrote %s" % args.in_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
