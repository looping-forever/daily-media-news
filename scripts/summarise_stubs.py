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
import time

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


def _ladder(stories, budget_seconds=None, snippets=None):
    """The summariser, with the publisher's own description as its source text.

    `snippets` is the SAME candidate index the caller judges with and quotes
    from. Without it `summarise_stories.build_prompt` falls back to
    `story["summary"]` -- which on this path IS the stub being repaired, so the
    model was asked to rewrite the sentence it was told to replace (run
    42321136172: a working rung, 299 s, 0 of 11 repaired).
    """
    import summarise_stories
    return summarise_stories.summarise(stories, budget_seconds=budget_seconds,
                                       snippets=snippets)


def _verdict(result, found, no_source, spend, rejected=()):
    """One line: WHY nothing was repaired, and how close a rung came.

    RUN 42321136172 printed `budget spent after 12 rungs, 0 repaired, 11 left`
    and nothing else. Twelve was the debug artifact's record cap, not the number
    of attempts; no reason was named; and the rung that had written six
    summaries in the same run seven minutes earlier was not mentioned. An owner
    should not have to download an artifact to learn any of that.
    """
    attempts = int(result.get("attempts") or len(result.get("debug") or [])
                   or int(result.get("requests") or 0))
    why = []
    if no_source:
        why.append("no source text=%d of %d stubs (not asked)"
                   % (no_source, len(found)))
    reasons = result.get("reasons") or {}
    ranked = sorted(reasons.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
    why.extend("%s=%d" % (name, count) for name, count in ranked[:3])
    if rejected:
        # THE SECOND GATE. The ladder can return text the repair's predicate
        # refuses -- most often the publisher's own snippet quoted back -- and
        # that answer never reaches `reasons` because the model call succeeded.
        # Without this a 0-repaired run would name no reason at all.
        counts = {}
        for row in rejected:
            reason = str(row.get("reason") or "no reason recorded")
            counts[reason] = counts.get(reason, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        why.append("validator rejected %d answer(s): %s" % (len(rejected), top))
    if not why:
        why.append("no rung returned a summary and none gave a reason")
    closest = result.get("closest") or {}
    if closest.get("model"):
        if closest.get("accepted"):
            near = "%s accepted %d row(s)" % (closest["model"],
                                              int(closest["accepted"]))
        else:
            near = "%s answered, nothing accepted (%s)" % (
                closest["model"], closest.get("reason") or "no usable row")
    else:
        near = "no rung answered"
    return ("%d attempt(s), %ds of %ds spent; why: %s; closest: %s"
            % (attempts, int(result.get("seconds") or 0),
               int(result.get("budget_seconds") or spend or 0),
               "; ".join(why), near))


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

    `snippets` is the candidate index, and it is now handed to the model as its
    source text as well as used to judge and to quote. It was never passed
    through before, so the ladder was asked to rewrite the very stub it was
    meant to replace.
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
                "dropped": 0, "budget_spent": False}

    log("summarise_stubs: %d of %d stories have no written summary; asking the "
        "ladder (allowance %ds)" % (len(found), len(stories), spend))
    # ONLY THE STUBS WITH SOMETHING TO SUMMARISE FROM ARE SENT. A story with no
    # publisher text has no evidence, and the prompt rightly refuses to invent --
    # so asking the ladder about it can only buy a `no_usable_row` (run
    # 42321136172 paid for ten of those). It also matters for the echo defect:
    # with no index entry the prompt used to send `story["summary"]`, which on
    # this path IS the stub, and the model handed it straight back.
    askable = [story for story in found
               if str(snippets.get(_url(story)) or "").strip()]
    no_source = len(found) - len(askable)
    if no_source:
        log("summarise_stubs: %d of %d stubs carry no publisher text; they are "
            "not sent (a model cannot summarise a headline)" % (no_source, len(found)))
    try:
        if not askable:
            result = {"summaries": {}, "requests": 0, "attempts": 0,
                      "reasons": {}, "budget_spent": False}
        elif summarise is not None:
            result = summarise(askable, budget_seconds=spend)
        else:
            result = _ladder(askable, budget_seconds=spend, snippets=snippets)
    except Exception as exc:      # the edition must survive a broken ladder
        log("summarise_stubs: the ladder raised (%s); writing the edition "
            "without it" % exc)
        result = {"summaries": {}, "requests": 0, "budget_spent": True,
                  "attempts": 1, "reasons": {"ladder_error": 1}}
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
                or derive_unsummarised.NO_SUMMARY_TEXT)
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
    if result.get("budget_spent") or written == 0:
        # ONE VERDICT LINE. Run 42144852087 overran its allowance by 4.5
        # minutes, was killed by the step's own cap, and said nothing at all;
        # the step then reported success with no file written. This line is the
        # evidence that the budget, not a crash, ended the repair. RUNGS, not
        # requests: the failed run tried 14 of them, and that is the number a
        # reader needs to see before deciding to widen the ladder.
        why = _verdict(result, found, no_source, spend, rejected)
        if result.get("budget_spent"):
            attempts = int(result.get("attempts")
                           or len(result.get("debug") or [])
                           or int(result.get("requests") or 0))
            log("summarise_stubs: budget spent after %d rungs, %d repaired, "
                "%d left \u2014 writing | %s"
                % (attempts, written, max(0, len(found) - written), why))
        else:
            # REPAIRED NOTHING AND NOT FOR THE BUDGET: the rung is named too, or
            # a working model that answered nothing looks identical to a broken
            # ladder (run 42321136172: 0 of 11, one log line, no reason).
            log("summarise_stubs: 0 repaired, %d left \u2014 %s"
                % (max(0, len(found) - written), why))
    log("summarise_stubs: replaced %d with written summaries, %d with publisher "
        "text, %d left without a summary, %d dropped; edition_kind=%s (%d of %d)"
        % (written, publisher, none, dropped, facts["edition_kind"],
           facts["summary_counts"]["summarised"],
           facts["summary_counts"]["total"]))
    return {"stubs": len(found), "written": written, "publisher": publisher,
            "none": none, "dropped": dropped,
            "budget_spent": bool(result.get("budget_spent"))}


def main(argv=None):
    # THE ALLOWANCE IS MEASURED FROM HERE, not from inside the ladder call.
    # Run 42144852087 spent ~4.5 minutes resolving 14 rungs, then started a
    # fresh 300 s walk: 8m 12s against a 300 s allowance, killed by the step's
    # own 8-minute cap before anything was written.
    started = time.monotonic()
    ap = argparse.ArgumentParser(
        description="Summarise the stubs a digest shipped.")
    ap.add_argument("--in", dest="in_path",
                    default="state/stories.validated.json")
    ap.add_argument("--candidates", default="state/candidates.json")
    ap.add_argument("--budget-seconds", type=float, default=None,
                    help="wall clock for the whole step (default: the "
                         "summariser's own TOTAL_BUDGET_SECONDS)")
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

    import summarise_stories
    total = (summarise_stories.TOTAL_BUDGET_SECONDS
             if args.budget_seconds is None else float(args.budget_seconds))
    remaining = max(0.0, total - (time.monotonic() - started))
    debug_path = os.path.join(os.path.dirname(os.path.abspath(args.in_path))
                              or ".", "summarise-debug.json")
    counts = {"stubs": 0, "written": 0, "publisher": 0, "none": 0,
              "dropped": 0}
    failed = False
    try:
        counts = repair(payload, validate_stories.snippet_index(args.candidates),
                        log=lambda line: print(line, flush=True), derived=derived,
                        budget_seconds=remaining, debug_path=debug_path)
    except Exception as exc:      # a broken repair must not lose the edition
        failed = True
        print("summarise_stubs: repair failed (%s); writing %s unchanged"
              % (exc, args.in_path), flush=True)
    if not counts["stubs"] and not failed:
        return 0

    # ALWAYS WRITE when there was work. A green step whose only artifact is a
    # log line is how eleven blank cards shipped (run 42144852087).
    tmp = args.in_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, args.in_path)
    print("summarise_stubs: wrote %s" % args.in_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
