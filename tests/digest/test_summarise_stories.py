#!/usr/bin/env python3
"""tests/digest/test_summarise_stories.py -- batched summaries for the fallback.

WHY THIS EXISTS. Measured 2026-11-05 00:56Z (run 41944898717): the agentic walk
made EIGHT model requests in 13.4 seconds -- a title call and seven build turns,
each re-sending a larger context -- and the eighth was refused by Gemini's free
tier with `generate_content_free_tier_input_token_count, limit: 250000 ...
Please retry in 40.095354008s`. Both keyed rungs refused the same way, the free
OpenCode pool answered `payment_required`, and the edition shipped with
publisher text under every headline.

The property this file pins is the one that ends that outcome:

    ONE REQUEST PER BATCH. A batch's input is fixed and small, and it is charged
    against the measured per-minute allowance BEFORE it is sent.

and the three rules that keep the fix honest:

    NO STORY IS DROPPED for lacking a summary; it keeps the publisher's text.
    NO SUMMARY IS INVENTED: a url outside the batch, an empty or non-English
    sentence, or a story the model ignored stays as the deriver wrote it.
    THE PROVIDER'S OWN HINT IS HONOURED, bounded, and never exceeds the cap.
    THE WHOLE JOB IS BOUNDED: waits are capped in total, not per batch, because
    the step that writes the edition must never be killed waiting for prose.
"""

import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import summarise_stories as ss  # noqa: E402

ENOUGH = ("Publisher text that is long enough to stand in for a real "
          "description of the story.")


def story(n, snippet=ENOUGH, category="world_major"):
    return {
        "title": "Story number %d about something that happened" % n,
        "summary": snippet,
        "source_name": "example.com",
        "source_url": "https://example.com/%d" % n,
        "category": category,
    }


def reply_for(batch, text="A model-written sentence about what happened today."):
    """A well-formed answer for the batch, as the CLI would print it."""
    rows = [{"url": s["source_url"], "summary": text} for s in batch]
    import json
    return json.dumps(rows)


class FakeRun:
    """Records every request and answers from a scripted plan.

    A plan entry is either a string (the answer) or an exception instance to
    raise. Entries are consumed in order; an exhausted plan repeats the last.
    """

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    def __call__(self, prompt, model):
        self.calls.append((prompt, model))
        entry = self.plan.pop(0) if len(self.plan) > 1 else self.plan[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Batching(unittest.TestCase):
    def test_fifteen_stories_become_three_requests(self):
        self.assertEqual([6, 6, 3], [len(b) for b in ss.batches(
            [story(n) for n in range(15)])])

    def test_the_request_carries_the_short_fields_only(self):
        batch = [story(1, snippet="x" * 900)]
        prompt = ss.build_prompt(batch)
        self.assertIn("https://example.com/1", prompt)
        self.assertIn("Story number 1", prompt)
        self.assertLess(len(prompt), 1400,
                        "the snippet must be trimmed to the 400-char cap, not "
                        "sent whole")
        self.assertNotIn("x" * 500, prompt)

    def test_one_request_stays_small(self):
        batch = [story(n, snippet="y" * 399) for n in range(6)]
        tokens = ss.estimate_tokens(ss.build_prompt(batch))
        self.assertLess(tokens, 3000,
                        "six stories must fit in one small request; %d tokens "
                        "means the payload grew" % tokens)


class Pacing(unittest.TestCase):
    def test_a_full_minute_makes_the_next_request_wait(self):
        clock = FakeClock()
        pacer = ss.Pacer(budget=1000, clock=clock, sleep=clock.sleep,
                         log=lambda *a: None)
        pacer.wait_for(600)
        slept = pacer.wait_for(600)
        self.assertGreaterEqual(slept, 59.0,
                                "600 more tokens on a spent 1000-token budget "
                                "must wait for the window to roll")
        self.assertEqual([600], [t for _, t in pacer.charges],
                         "the charge that aged out during the sleep is spent; "
                         "only the new request is still on the books")

    def test_the_wait_never_exceeds_the_cap(self):
        clock = FakeClock()
        pacer = ss.Pacer(budget=1000, clock=clock, sleep=clock.sleep,
                         log=lambda *a: None)
        for _ in range(30):
            pacer.wait_for(700)
        self.assertLessEqual(sum(clock.slept), 30 * ss.MAX_WAIT_SECONDS)
        self.assertTrue(all(s <= ss.MAX_WAIT_SECONDS + 0.1 for s in clock.slept))


class Refusals(unittest.TestCase):
    def test_a_429_hint_is_honoured_and_the_same_batch_retried(self):
        clock = FakeClock()
        batch = [story(n) for n in range(3)]
        run = FakeRun([
            ss.Refusal("AI_APICallError: Quota exceeded for metric: "
                       "generativelanguage.googleapis.com/"
                       "generate_content_free_tier_input_token_count, "
                       "limit: 250000 ... Please retry in 40.095354008s."),
            reply_for(batch),
        ])
        out = ss.summarise(batch, models=["gemini/gemini-3.5-flash-lite"],
                           run=run, sleep=clock.sleep, clock=clock,
                           log=lambda *a: None)
        self.assertEqual([40], clock.slept,
                         "the provider asked for 40s and that is what it gets")
        self.assertEqual(3, len(out["summaries"]))
        self.assertEqual(["gemini/gemini-3.5-flash-lite"] * 2,
                         [model for _, model in run.calls],
                         "the retry is of the SAME model, and only one wait "
                         "happens per batch")
        self.assertEqual(1, len(out["refusals"]))

    def test_a_hint_longer_than_the_bound_abandons_that_model(self):
        clock = FakeClock()
        batch = [story(1)]
        run = FakeRun([
            ss.Refusal("quota exceeded; retry in 400s"),
            reply_for(batch, "A sentence from the second model about the news."),
        ])
        out = ss.summarise(batch, models=["gemini/a", "opencode/b"],
                           run=run, sleep=clock.sleep, clock=clock,
                           log=lambda *a: None)
        self.assertEqual([], clock.slept,
                         "a 400s hint is refused, never slept: the edition "
                         "cannot wait seven minutes")
        self.assertEqual(["gemini/a", "opencode/b"],
                         [m for _, m in run.calls])
        self.assertEqual(1, len(out["summaries"]))
        self.assertEqual("opencode/b", out["model"])

    def test_a_refusal_without_a_hint_waits_the_default(self):
        clock = FakeClock()
        batch = [story(1)]
        run = FakeRun([ss.Refusal("429 Too Many Requests"), reply_for(batch)])
        ss.summarise(batch, models=["opencode/a"], run=run,
                     sleep=clock.sleep, clock=clock, log=lambda *a: None)
        self.assertEqual([ss.DEFAULT_WAIT_SECONDS], clock.slept)

    def test_a_broken_model_failure_moves_on_without_sleeping(self):
        clock = FakeClock()
        batch = [story(1)]
        run = FakeRun([ss.Refusal("model not found"), reply_for(batch)])
        out = ss.summarise(batch, models=["opencode/a", "opencode/b"],
                           run=run, sleep=clock.sleep, clock=clock,
                           log=lambda *a: None)
        self.assertEqual([], clock.slept)
        self.assertEqual(1, len(out["summaries"]))


class NeverInvent(unittest.TestCase):
    def test_a_url_outside_the_batch_is_dropped(self):
        batch = [story(1)]
        parsed = ss.parse_reply(
            '[{"url": "https://evil.example/x", "summary": "%s"}]' % ENOUGH,
            batch)
        self.assertEqual({}, parsed)

    def test_an_empty_or_short_summary_is_dropped(self):
        batch = [story(1)]
        for text in ("", "Too short."):
            parsed = ss.parse_reply(
                '[{"url": "%s", "summary": "%s"}]'
                % (batch[0]["source_url"], text), batch)
            self.assertEqual({}, parsed, "accepted %r" % text)

    def test_a_non_english_summary_is_dropped(self):
        batch = [story(1)]
        parsed = ss.parse_reply(
            '[{"url": "%s", "summary": "%s"}]'
            % (batch[0]["source_url"],
               "\u092f\u0939 \u090f\u0915 \u0938\u092e\u093e\u091a\u093e\u0930 "
               "\u0939\u0948 \u091c\u094b \u0938\u0941\u0928\u093e\u0908 "
               "\u0926\u0947\u0924\u093e \u0939\u0948\u0964"), batch)
        self.assertEqual({}, parsed)

    def test_a_story_the_model_ignored_keeps_its_text(self):
        batch = [story(1), story(2)]
        parsed = ss.parse_reply(reply_for(batch[:1]), batch)
        self.assertEqual(1, len(parsed))
        import derive_unsummarised as derive
        written = derive.apply_summaries(batch, parsed)
        self.assertEqual(1, written)
        self.assertEqual(ENOUGH, batch[1]["summary"],
                         "a story with no answer keeps the publisher's text")

    def test_at_most_max_requests_are_made(self):
        batch = [story(n) for n in range(40)]
        run = FakeRun([
            reply_for(batch[0:6]), reply_for(batch[6:12]),
            reply_for(batch[12:18]), reply_for(batch[18:24]),
        ])
        out = ss.summarise(batch, models=["opencode/a"], run=run,
                           sleep=lambda s: None, clock=FakeClock(),
                           log=lambda *a: None)
        self.assertEqual(ss.MAX_REQUESTS, out["requests"],
                         "a forty-story pool must not become seven requests")
        self.assertEqual(24, len(out["summaries"]))


class TheJobBudget(unittest.TestCase):
    """A per-wait bound is not a TOTAL bound.

    Three batches times four models times a 90s hint is eighteen minutes, and
    the derive step is what writes stories.json: being killed there loses the
    edition itself, which is worse than losing the prose. These pin the total.
    """

    def test_a_refusal_storm_stops_when_the_budget_is_spent(self):
        clock = FakeClock()
        batch = [story(n) for n in range(40)]
        run = FakeRun([ss.Refusal("quota exceeded; retry in 40s")])
        out = ss.summarise(batch, models=["gemini/a"], run=run,
                           sleep=clock.sleep, clock=clock, log=lambda *a: None,
                           budget_seconds=100)
        self.assertLessEqual(sum(clock.slept), 100.1,
                             "the job asked for a 100s budget and slept %s"
                             % (clock.slept,))
        self.assertEqual({}, out["summaries"])
        self.assertTrue(out["waits"], "the storm must have waited, not looped")

    def test_a_hint_is_clamped_to_what_is_left(self):
        clock = FakeClock()
        batch = [story(1)]
        run = FakeRun([ss.Refusal("quota exceeded; retry in 40s")])
        ss.summarise(batch, models=["gemini/a"], run=run, sleep=clock.sleep,
                     clock=clock, log=lambda *a: None, budget_seconds=10)
        self.assertEqual([10], clock.slept,
                         "10s of budget cannot buy a 40s wait")

    def test_a_pacing_wait_is_clamped_by_the_deadline(self):
        clock = FakeClock()
        pacer = ss.Pacer(budget=1000, clock=clock, sleep=clock.sleep,
                         log=lambda *a: None)
        pacer.deadline = clock.now + 5
        pacer.wait_for(600)
        pacer.wait_for(600)
        self.assertLessEqual(sum(clock.slept), 5.1)

    def test_a_runner_fault_does_not_cost_the_edition(self):
        clock = FakeClock()
        batch = [story(1)]
        run = FakeRun([RuntimeError("opencode exited 127"),
                       reply_for(batch)])
        out = ss.summarise(batch, models=["opencode/a", "opencode/b"],
                           run=run, sleep=clock.sleep, clock=clock,
                           log=lambda *a: None)
        self.assertEqual(1, len(out["summaries"]),
                         "the next model still gets the batch")
        self.assertEqual([], clock.slept)


class TheHintParser(unittest.TestCase):
    def test_the_measured_shape_is_read(self):
        self.assertEqual(40, ss.retry_hint_seconds(
            "... Please retry in 40.095354008s."))

    def test_retry_after_and_retry_delay_are_read(self):
        self.assertEqual(90, ss.retry_hint_seconds("Retry-After: 90"))
        self.assertEqual(12, ss.retry_hint_seconds('retryDelay: "12s"'))

    def test_no_hint_is_none(self):
        self.assertIsNone(ss.retry_hint_seconds("429 Too Many Requests"))


if __name__ == "__main__":
    unittest.main()
