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

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    `timeouts` is the wall clock each call was granted -- the module under test
    decides that number, and it must never exceed the job budget.
    """

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []
        self.timeouts = []

    def __call__(self, prompt, model, timeout=None):
        self.calls.append((prompt, model))
        self.timeouts.append(timeout)
        entry = self.plan.pop(0) if len(self.plan) > 1 else self.plan[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


class HangingRun:
    """A rung that spends EXACTLY the timeout it was given, as a killed CLI does.

    The fake clock advances by the granted timeout, so `summarise` sees real
    elapsed time without the suite waiting: that is what makes "a slow rung
    cannot spend the window another rung needed" testable in milliseconds.
    """

    def __init__(self, clock, answer, hang=("gemini/a",)):
        self.clock = clock
        self.answer = answer
        self.hang = set(hang)
        self.seen = []

    def __call__(self, prompt, model, timeout=None):
        self.seen.append({"model": model, "timeout": timeout})
        self.clock.now += float(timeout or 0)
        if model in self.hang:
            raise ss.Refusal("%s: no answer in %ss (killed)"
                             % (model, timeout), kind="timeout")
        return self.answer


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
        self.assertNotIn("x" * 500, prompt)
        self.assertIn("x" * 400, prompt)
        self.assertNotIn("x" * 401, prompt,
                         "the source text is trimmed to the 400-char cap, not "
                         "sent whole")
        longer = ss.build_prompt([story(1, snippet="x" * 4000)])
        self.assertEqual(len(prompt), len(longer),
                         "a longer source must not make a longer request")

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

    def test_an_access_denied_key_error_does_not_earn_a_wait(self):
        clock = FakeClock()
        batch = [story(1)]
        run = FakeRun([
            ss.Refusal("groq/a: auth (HTTP 403) -- Access denied: invalid api key",
                       kind="auth"),
            reply_for(batch),
        ])
        out = ss.summarise(batch, models=["groq/a", "groq/b"], run=run,
                           sleep=clock.sleep, clock=clock, log=lambda *a: None)
        self.assertEqual([], clock.slept,
                         "a key error is not an allowance a wait can restore")
        self.assertEqual("provider_refusal", ss.refusal_cause(
            "auth", "Access denied: invalid api key"))
        self.assertEqual(1, len(out["summaries"]))

    def test_a_blocked_edge_does_not_cool_the_rung_or_wait(self):
        clock = FakeClock()
        batch = [story(1)]
        cool = {}
        run = FakeRun([
            ss.Refusal("groq/a: blocked (HTTP 403) -- Access denied. Please "
                       "check your network settings.", kind="blocked"),
            reply_for(batch),
        ])
        out = ss.summarise(batch, models=["groq/a", "groq/b"], run=run,
                           sleep=clock.sleep, clock=clock, cooldown=cool,
                           log=lambda *a: None)
        self.assertEqual([], clock.slept,
                         "no wait can lift a provider's IP policy")
        self.assertEqual({}, cool,
                         "the provider blocked the network, not this rung")
        self.assertEqual(1, len(out["summaries"]))
        self.assertEqual("provider_blocked", ss.refusal_cause(
            "blocked", "Access denied. Please check your network settings."))


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
        """A call that ate the clock cannot then wait on a stale reading.

        The 60s hint is cut to what the deadline still holds, and the total
        stays inside the job's own allowance.
        """
        clock = FakeClock()

        def run(prompt, model, timeout=None):
            clock.now += 70          # most of the 100s is gone
            raise ss.Refusal("quota exceeded; retry in 60s")

        ss.summarise([story(1)], models=["gemini/a"], run=run,
                     sleep=clock.sleep, clock=clock, log=lambda *a: None,
                     budget_seconds=100)
        self.assertEqual([30], clock.slept,
                         "the 60s hint cannot outlive the 30s left")
        self.assertLessEqual(sum(clock.slept), 100)

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

    def test_the_clock_starts_before_the_ladder_is_resolved(self):
        """RUN 42144852087: 4.5 minutes of probes sat OUTSIDE the 300s allowance,
        so a fresh 300s walk began after them and the step died at its 8-minute
        cap with nothing written. Discovery is part of the step."""
        clock = FakeClock()

        def slow_ladder(log=None):
            clock.now += 400          # catalogue probes cost more than the walk
            return ["gemini/a"]

        run = FakeRun([reply_for([story(1)])])
        with mock.patch.object(ss, "ladder_models", slow_ladder):
            out = ss.summarise([story(1)], models=None, run=run,
                               sleep=clock.sleep, clock=clock,
                               budget_seconds=300, log=lambda *a: None)
        self.assertEqual([], run.calls,
                         "no model call may start with the allowance spent")
        self.assertEqual(0, out["requests"])
        self.assertTrue(out["budget_spent"],
                        "the caller has to be able to say why it stopped")


class TheHintParser(unittest.TestCase):
    def test_the_measured_shape_is_read(self):
        self.assertEqual(40, ss.retry_hint_seconds(
            "... Please retry in 40.095354008s."))

    def test_retry_after_and_retry_delay_are_read(self):
        self.assertEqual(90, ss.retry_hint_seconds("Retry-After: 90"))
        self.assertEqual(12, ss.retry_hint_seconds('retryDelay: "12s"'))

    def test_no_hint_is_none(self):
        self.assertIsNone(ss.retry_hint_seconds("429 Too Many Requests"))


class TheBudgetArithmetic(unittest.TestCase):
    """MEASURED (run 41990956737): batch 1 of 4, ~854 tokens of a 250,000/min
    allowance, and the run still produced 0 of 29 summaries. Two keyed rungs and
    three free rungs were each given the full 120s; the free three returned
    nothing at all, and the 300s budget was spent inside batch ONE while three
    later batches never ran.

    So the timeout a call is granted is not a constant:
        timeout = min(rung cap, share of the budget left)
        share   = seconds left / (batches left * rungs left)
    and the cap is shorter for a free rung, which is the rung measured timing out
    with zero bytes.
    """

    def test_a_call_is_never_granted_more_than_the_clock_left(self):
        clock = FakeClock()
        run = FakeRun([reply_for([story(1)])])
        ss.summarise([story(1)], models=["gemini/a"], run=run,
                     sleep=clock.sleep, clock=clock, log=lambda *a: None,
                     budget_seconds=100)
        self.assertLessEqual(run.timeouts[0], 100,
                             "a 100s job cannot grant a %ss call"
                             % (run.timeouts[0],))

    def test_a_clock_that_cannot_fund_a_keyed_call_starts_none(self):
        """10s is not a keyed attempt, it is a guaranteed `timeout`."""
        clock = FakeClock()
        run = FakeRun([reply_for([story(1)])])
        out = ss.summarise([story(1)], models=["gemini/a"], run=run,
                           sleep=clock.sleep, clock=clock, log=lambda *a: None,
                           budget_seconds=10)
        self.assertEqual([], run.timeouts,
                         "no call can be given the 90s a keyed rung needs")
        self.assertTrue(out["budget_spent"])

    def test_a_hanging_first_rung_leaves_time_for_the_second(self):
        clock = FakeClock()
        batch = [story(1)]
        run = HangingRun(clock, reply_for(batch))
        out = ss.summarise(batch, models=["gemini/a", "gemini/b"], run=run,
                           clock=clock, sleep=clock.sleep, log=lambda *a: None,
                           budget_seconds=200)
        self.assertEqual(1, len(out["summaries"]),
                         "the second rung must still get a call: %r"
                         % (run.seen,))
        self.assertGreater(run.seen[1]["timeout"], 0)

    def test_free_rungs_get_the_shorter_cap(self):
        self.assertEqual(ss.FREE_REQUEST_TIMEOUT,
                         ss.timeout_for_rung("opencode/beam-free"))
        self.assertEqual(ss.KEYED_REQUEST_TIMEOUT,
                         ss.timeout_for_rung("gemini/gemini-3.5-flash-lite"))
        self.assertLess(ss.FREE_REQUEST_TIMEOUT, ss.KEYED_REQUEST_TIMEOUT)

    def test_a_free_rung_observed_in_a_run_gets_the_shorter_cap(self):
        clock = FakeClock()
        run = FakeRun([reply_for([story(1)])])
        ss.summarise([story(1)], models=["opencode/beam-free"], run=run,
                     sleep=clock.sleep, clock=clock, log=lambda *a: None,
                     budget_seconds=300)
        self.assertEqual(ss.FREE_REQUEST_TIMEOUT, run.timeouts[0])

    def test_every_batch_still_gets_a_call_when_the_first_one_hangs(self):
        clock = FakeClock()
        batch = [story(n) for n in range(24)]      # four batches of six
        run = HangingRun(clock, reply_for(batch[:6]))
        out = ss.summarise(batch, models=["gemini/a"], run=run,
                           clock=clock, sleep=clock.sleep, log=lambda *a: None,
                           budget_seconds=450)
        self.assertGreater(len(run.seen), 1,
                           "one hanging rung must not consume the whole job: "
                           "%r" % (run.seen,))

    def test_the_lead_rung_gets_the_realistic_budget(self):
        """RUN 42078530071: five ~20s attempts, because 20s is the FLOOR.

        Eight rungs and two batches made the fair share 33s in batch 1 and less
        later, so `MIN_RUNG_SECONDS` -- not the provider's cap -- became the time
        a six-story batch was given. It is the batch's first rung that has to
        answer, so it gets the intended value and the fallbacks keep the floor.
        """
        self.assertEqual(90, ss.LEAD_RUNG_SECONDS)
        ladder = (["gemini/gemini-3.5-flash-lite", "gemini/gemini-3.1-flash-lite"]
                  + ["opencode/m%d-free" % n for n in range(6)])
        lead = ss.rung_timeout(ladder, 0, seconds_left=300, batches_left=2)
        self.assertEqual(ss.LEAD_RUNG_SECONDS, lead)
        free_fallback = ss.rung_timeout(ladder, 2, seconds_left=300,
                                        batches_left=2)
        self.assertLess(free_fallback, lead,
                        "a free rung is a different, faster call and keeps the "
                        "shorter floor, so the pool is still reachable behind "
                        "the lead")

    def test_a_keyed_fallback_is_not_cut_below_a_six_story_need(self):
        """RUN 42356298224: ten rungs split 299s into ~30s slices and six of the
        fifteen attempts were `timeout` -- while the same run's lead slice, at
        90s, answered. A fallback keyed rung is the same six-story call and gets
        the same clock; only a free rung is shorter."""
        ladder = (["gemini/gemini-3.5-flash-lite", "gemini/gemini-3.1-flash-lite"]
                  + ["opencode/m%d-free" % n for n in range(8)])
        self.assertEqual(ss.LEAD_RUNG_SECONDS,
                         ss.rung_timeout(ladder, 1, seconds_left=300,
                                         batches_left=2))
        self.assertEqual(ss.FREE_REQUEST_TIMEOUT,
                         ss.rung_timeout(ladder, 5, seconds_left=300,
                                         batches_left=2))

    def test_a_rung_that_answers_within_its_clock_records_no_timeout(self):
        """The 40s rung of RUN 42356298224's shape: granted 30s it dies, granted
        the keyed floor it answers, and no timeout is recorded."""
        class SlowFallback:
            def __init__(self, clock, batch):
                self.clock = clock
                self.answer = reply_for(batch)

            def __call__(self, prompt, model, timeout=None):
                if model == "gemini/lead":
                    return reply_for([story(1)], "Too short.")
                if float(timeout or 0) < 40:
                    self.clock.now += float(timeout or 0)
                    raise ss.Refusal("%s: no answer in %ss (killed)"
                                     % (model, timeout), kind="timeout")
                self.clock.now += 40
                return self.answer

        clock = FakeClock()
        ladder = (["gemini/lead", "gemini/gemini-3.1-flash-lite"]
                  + ["opencode/m%d-free" % n for n in range(8)])
        out = ss.summarise([story(1)], models=ladder, run=SlowFallback(clock, [story(1)]),
                           clock=clock, sleep=clock.sleep, log=lambda *a: None,
                           budget_seconds=300)
        self.assertNotIn("timeout", out["reasons"],
                         "a rung that answers inside its grant cannot be a "
                         "timeout: %r" % (out["reasons"],))
        self.assertEqual(1, len(out["summaries"]))

    def test_the_lead_rungs_budget_never_exceeds_the_job(self):
        ladder = ["gemini/gemini-3.5-flash-lite", "gemini/other"]
        self.assertLessEqual(
            ss.rung_timeout(ladder, 0, seconds_left=25, batches_left=1), 25)

    def test_an_exit_nonzero_twice_demotes_the_rung_within_the_run(self):
        """The four rc=1 partial answers left no health record at all."""
        cool = {"schema_version": 1, "models": {}, "pool": {}}
        stories = [story(n) for n in range(7)]      # two batches: 6 + 1
        failed = []

        def run(prompt, model, timeout=None):
            if model == BROKEN:
                failed.append(model)
                raise ss.Refusal("%s: exit 1: partial json" % model,
                                 kind="exit_nonzero", exit_code=1)
            return json.dumps([
                {"url": url,
                 "summary": "A model-written sentence about what happened today."}
                for url in __import__("re").findall(
                    r"https://example\.com/\d+", prompt)])

        out = ss.summarise(stories, models=[BROKEN, WORKS], run=run,
                           sleep=lambda *_a: None, cooldown=cool,
                           log=lambda *_a: None)
        self.assertEqual(7, len(out["summaries"]))
        self.assertEqual(2, len(failed),
                         "two attempts in two batches, then it is skipped")
        row = cool["models"][BROKEN]
        self.assertTrue(row.get("until"),
                        "two unanswered requests must cool the rung: %r" % (row,))

    def test_an_empty_answer_twice_demotes_the_rung_within_the_run(self):
        cool = {"schema_version": 1, "models": {}, "pool": {}}
        stories = [story(n) for n in range(7)]
        failed = []

        def run(prompt, model, timeout=None):
            if model == BROKEN:
                failed.append(model)
                raise ss.Refusal("%s: empty answer" % model,
                                 kind="empty_content")
            return json.dumps([
                {"url": url,
                 "summary": "A model-written sentence about what happened today."}
                for url in __import__("re").findall(
                    r"https://example\.com/\d+", prompt)])

        out = ss.summarise(stories, models=[BROKEN, WORKS], run=run,
                           sleep=lambda *_a: None, cooldown=cool,
                           log=lambda *_a: None)
        self.assertEqual(7, len(out["summaries"]))
        self.assertEqual(2, len(failed))
        self.assertTrue(cool["models"][BROKEN].get("until"))


class WhyTheReplyIsUnusable(unittest.TestCase):
    """`answered but yielded no usable summary` is not a diagnosis.

    Run 41990956737 logged exactly that for two keyed rungs -- one exited 1 with
    partial JSON on stdout, the other returned something parseable-but-unusable
    -- and nothing in the run could say which. These pin the vocabulary that
    replaces it.
    """

    URL = "https://example.com/1"

    def test_a_reply_cut_at_the_output_cap_says_so(self):
        raw = ('[{"url": "%s", "summary": "The ministry said the new schedule"\n'
               '{"finishReason": "MAX_TOKENS"}' % self.URL)
        self.assertEqual("truncated_at_max_tokens", ss.classify_reply(raw))

    def test_a_partial_json_reply_is_named_partial(self):
        raw = '[{"url": "%s", "summary": "The ministry said the new' % self.URL
        self.assertEqual("partial_json", ss.classify_reply(raw))

    def test_a_genuinely_empty_reply_is_empty_content(self):
        for raw in ("", "   \n\t "):
            self.assertEqual("empty_content", ss.classify_reply(raw))

    def test_a_balanced_but_broken_reply_is_not_json(self):
        self.assertEqual("not_json", ss.classify_reply("I cannot help with that."))

    def test_a_parseable_reply_with_no_accepted_row_says_so(self):
        batch = [story(1)]
        raw = reply_for(batch, "Too short.")
        self.assertEqual("no_usable_row", ss.classify_reply(raw, batch))

    def test_a_good_reply_is_ok(self):
        batch = [story(1)]
        self.assertEqual("ok", ss.classify_reply(reply_for(batch), batch))

    def test_the_reason_reaches_the_log_and_the_result(self):
        clock = FakeClock()
        batch = [story(1)]
        raw = '[{"url": "%s", "summary": "The ministry said the new' % self.URL
        run = FakeRun([raw, raw])
        out = ss.summarise(batch, models=["gemini/a", "gemini/b"], run=run,
                           clock=clock, sleep=clock.sleep, log=lambda *a: None,
                           budget_seconds=100)
        reasons = [d["reason"] for d in out["debug"]]
        self.assertEqual(["partial_json", "partial_json"], reasons,
                         "a generic 'no usable summary' is what this replaces")


class TheDebugCapture(unittest.TestCase):
    """What reaches the artifact: bounded, described, and free of credentials."""

    def test_the_raw_capture_is_bounded(self):
        record = ss.debug_record(model="gemini/a", raw="x" * 9000,
                                 reason="partial_json")
        self.assertLessEqual(len(record["raw"]), ss.DEBUG_RAW_LIMIT)

    def test_the_record_names_the_model_the_reason_and_the_metadata(self):
        raw = ('{"finish_reason": "length", "stop_reason": "max_tokens",\n'
               ' "usage": {"prompt_token_count": 854, "candidates_token_count": 0}}')
        record = ss.debug_record(model="gemini/a", raw=raw, reason="partial_json")
        self.assertEqual("gemini/a", record["model"])
        self.assertEqual("length", record["finish_reason"])
        self.assertEqual("max_tokens", record["stop_reason"])
        self.assertEqual(854, record["usage"].get("prompt_token_count"))

    def test_a_crashed_rung_records_the_exit_code_it_died_with(self):
        def run(prompt, model, timeout=None):
            raise ss.Refusal("%s: exit 1" % model, kind="exit_nonzero",
                             raw='[{"url": "https://example.com/1", "summ',
                             exit_code=1)
        clock = FakeClock()
        out = ss.summarise([story(1)], models=["gemini/a"], run=run,
                           clock=clock, sleep=clock.sleep,
                           log=lambda *a: None, budget_seconds=100)
        record = out["debug"][0]
        self.assertEqual("exit_nonzero", record["reason"])
        self.assertEqual("partial_json", record["parse_error"])
        self.assertEqual(1, record["exit_code"],
                         "an exit code that is always None cannot be told "
                         "apart from a timeout")

    def test_the_capture_redacts_any_key_shaped_string(self):
        secrets = (
            "AIzaSyD-EXAMPLEEXAMPLEEXAMPLE123",
            "sk-abcdefghijklmnopqrstuvwx",
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "xoxb-1234567890-abcdefghij",
            "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
            "AKIAABCDEFGHIJKLMNOP",
        )
        raw = ('failed: auth AIzaSyD-EXAMPLEEXAMPLEEXAMPLE123 sk-abcdefghijklmnopqrstuvwx '
               'ghp_abcdefghijklmnopqrstuvwxyz0123 xoxb-1234567890-abcdefghij '
               'Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature '
               '{"api_key": "AKIAABCDEFGHIJKLMNOP"}')
        dumped = json.dumps(ss.debug_record(model="gemini/a", raw=raw,
                                           reason="exit_nonzero"))
        for secret in secrets:
            self.assertNotIn(secret, dumped, "%r survived redaction" % secret)
        self.assertIn("REDACTED", dumped)
        self.assertIn("failed: auth", dumped,
                      "redaction must not eat the diagnosis")


class TheChildIsActuallyKilled(unittest.TestCase):
    """`timeout=` on the child, not only a check between requests."""

    def test_the_granted_timeout_is_passed_to_the_process(self):
        import unittest.mock as mock
        proc = mock.MagicMock()
        proc.communicate.return_value = (b'[{"url": "u", "summary": "s"}]', None)
        proc.returncode = 0
        with mock.patch.object(ss.subprocess, "Popen", return_value=proc) as popen:
            ss.opencode_run("prompt", "gemini/a", timeout=7)
        self.assertEqual(7, proc.communicate.call_args[1]["timeout"])
        self.assertIn("gemini/a", popen.call_args[0][0])

    def test_a_timeout_kills_the_child_and_keeps_the_partial_output(self):
        import subprocess
        import unittest.mock as mock
        proc = mock.MagicMock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="opencode", timeout=7),
            (b'[{"url": "https://example.com/1", "summ', None),
        ]
        with mock.patch.object(ss.subprocess, "Popen", return_value=proc):
            with self.assertRaises(ss.Refusal) as caught:
                ss.opencode_run("prompt", "gemini/a", timeout=7)
        proc.kill.assert_called_once()
        self.assertEqual("timeout", caught.exception.kind)
        self.assertIn("summ", caught.exception.raw)


class TheReplyIsClean(unittest.TestCase):
    """Run 41995372308: three good keyed replies were thrown away.

    Measured in that run's state/summarise-debug.json -- not inferred:

        gemini/gemini-3.5-flash-lite  reason=not_json
        raw starts  "\\x1b[0m\\n> build \\u00b7 gemini-3.5-flash-lite\\n..."
        and inside the fence is a complete JSON array, every row intact.

    The extractor sliced from the first "[" in the reply and the reply begins
    with the CLI's own escape sequence, `\\x1b[0m` -- a "[" that belongs to the
    terminal, not to the answer. So the slice started one character early and
    json.loads refused text that was valid JSON all along.
    """

    BANNER = "\x1b[0m\n> build \u00b7 gemini-3.5-flash-lite\n\x1b[0m\n"

    def test_a_reply_wrapped_in_cli_chrome_still_parses(self):
        batch = [story(1), story(2)]
        raw = self.BANNER + "```json\n" + reply_for(batch) + "\n```\n"
        self.assertEqual(2, len(ss.parse_reply(raw, batch)))

    def test_colour_inside_the_answer_does_not_move_the_slice(self):
        batch = [story(1), story(2)]
        raw = "\x1b[91m[\x1b[0m" + reply_for(batch)[1:-1] + "\x1b[0m]"
        self.assertEqual(2, len(ss.parse_reply(raw, batch)))

    def test_a_reply_that_is_only_cli_chrome_is_empty_content(self):
        # The banner is not an answer. Once the chrome is stripped there is
        # nothing left, which is `empty_content` -- more precise than calling
        # the CLI's own decoration "text that failed to parse".
        self.assertEqual("empty_content", ss.classify_reply(self.BANNER))

    def test_prose_around_a_bare_array_is_still_read(self):
        batch = [story(1)]
        raw = "Here is the JSON you asked for:\n" + reply_for(batch) + "\nDone."
        self.assertEqual(1, len(ss.parse_reply(raw, batch)))

    def test_prose_with_no_array_is_not_json(self):
        self.assertEqual("not_json", ss.classify_reply(
            "I could not reach the model for this batch."))

    def test_an_unterminated_array_is_partial_json(self):
        raw = self.BANNER + ('```json\n[{"url": "https://example.com/1", '
                             '"summary": "a sentence that stops')
        self.assertEqual("partial_json", ss.classify_reply(raw))

    def test_a_bracket_in_the_preamble_is_not_mistaken_for_the_answer(self):
        batch = [story(1)]
        raw = "Note [draft]:\n" + reply_for(batch)
        self.assertEqual(1, len(ss.parse_reply(raw, batch)))


class CapturedReplies(unittest.TestCase):
    """The three replies the run threw away, replayed from the artifact.

    tests/digest/fixtures/captured-replies-41995372308.json holds them verbatim,
    ASCII-escaped. They are the evidence that the parser fix addresses the real
    failure and not a guess about it.
    """

    FIXTURE = os.path.join(_HERE, "fixtures",
                           "captured-replies-41995372308.json")

    def captured(self):
        with open(self.FIXTURE, encoding="utf-8") as fh:
            return json.load(fh)["captured"]

    def test_all_three_parse_into_the_summaries_they_contained(self):
        rows = self.captured()
        self.assertEqual(3, len(rows), "three replies were recorded as not_json")
        for captured in rows:
            batch = [{"source_url": url, "category": "world_major"}
                     for url in captured["urls"]]
            parsed = ss.parse_reply(captured["raw"], batch)
            self.assertEqual(captured["expected_summaries"], len(parsed),
                             "%s: %d of %d rows should be printable"
                             % (captured["model"], len(parsed),
                                captured["rows"]))
            self.assertEqual("ok", ss.classify_reply(captured["raw"], batch),
                             captured["model"])

    def test_no_captured_reply_is_classified_as_a_parse_failure(self):
        for captured in self.captured():
            self.assertIn(ss.classify_reply(captured["raw"]),
                          ("ok", "no_usable_row"),
                          "%s still looks unreadable" % captured["model"])

    def test_the_fixture_loads_from_any_working_directory(self):
        """The path is anchored to THIS file, never to the caller's cwd.

        The runner starts these suites from the repository root, but a developer
        running one file from its own folder must get the same evidence, and a test
        that quietly found zero fixtures would pass forever.
        """
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(tempfile.gettempdir())
        rows = self.captured()
        self.assertEqual(3, len(rows),
                         "the fixture was not found from %s" % os.getcwd())


#: RUN 42061741326 (2026-11-05): `gemini-3.5-flash-lite` produced nothing twice
#: (`rc=1`, kind `other`) and burned ~99 s before a request was ever sent, while
#: `gemini-3.1-flash-lite` answered. Nothing recorded that, so the next run began
#: with the same broken rung first. These tests pin the memory that stops it.
BROKEN = "gemini/gemini-3.5-flash-lite"
WORKS = "gemini/gemini-3.1-flash-lite"
THIRD = "openrouter/some-free-model"


class TheBrokenRungIsDemoted(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.cool = {"schema_version": 1, "models": {}, "pool": {}}

    def lines(self):
        self.log = []
        return self.log

    def test_the_rungs_own_order_is_kept_until_something_is_cooled(self):
        self.assertEqual([BROKEN, WORKS, THIRD],
                         ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                          now=self.now, log=lambda *_a: None))

    def test_two_refusals_cool_the_rung_and_the_demotion_is_logged(self):
        log = []
        for _ in range(2):
            ss.record_refusal(self.cool, BROKEN, "other", now=self.now)
        row = self.cool["models"][BROKEN]
        self.assertEqual("rung_broken", row["reason"])
        self.assertTrue(row.get("until"),
                        "two refusals inside the day must set an expiry")
        self.assertEqual([WORKS, THIRD, BROKEN],
                         ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                          now=self.now, log=log.append))
        self.assertTrue(any(BROKEN in line and "demoted" in line for line in log),
                        "the demotion has to be visible in the run log, not only "
                        "in a state file: %r" % (log,))

    def test_one_refusal_is_not_enough(self):
        ss.record_refusal(self.cool, BROKEN, "other", now=self.now)
        self.assertNotIn("until", self.cool["models"][BROKEN],
                         "a single failed attempt may be the prompt, not the rung")
        self.assertEqual([BROKEN, WORKS, THIRD],
                         ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                          now=self.now, log=lambda *_a: None))

    #: The free pool every one of its ids draws on. A quota refusal here is a
    #: fact about the bucket, never about the rung.
    SHARED = "opencode/deepseek-v4.1-flash-free"

    def test_a_shared_pool_quota_refusal_never_cools_a_rung(self):
        for kind in ("quota_notice", "quota_or_rate_limited", "not_configured"):
            for _ in range(3):
                ss.record_refusal(self.cool, self.SHARED, kind, now=self.now)
        self.assertEqual({}, self.cool["models"],
                         "the whole pool shares one allowance: cooling rungs for "
                         "it would remove every model and end the edition")

    def test_a_keyed_provider_quota_refusal_cools_that_rung_for_six_hours(self):
        """RUN 42144852087: gemini-3.5-flash-lite refused `quota_or_rate_limited`,
        which cooled nothing, so it led the next run and burned its first attempt
        on the same wall."""
        row = ss.record_refusal(self.cool, BROKEN, "quota_notice", now=self.now)
        self.assertIsNotNone(row)
        self.assertEqual("quota_exhausted", row["reason"])
        self.assertTrue(row.get("until"))
        self.assertEqual([WORKS, THIRD, BROKEN],
                         ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                          now=self.now),
                         "the keyed rung moves behind the ones that may work")
        later = self.now + 6 * 3600 + 1
        self.assertEqual([BROKEN, WORKS, THIRD],
                         ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                          now=later),
                         "six hours is the window, and it lapses on its own")

    def test_the_cooldown_lapses_and_the_rung_comes_back_with_a_line(self):
        for _ in range(2):
            ss.record_refusal(self.cool, BROKEN, "other", now=self.now)
        later = self.now + 7 * 3600
        log = []
        self.assertEqual([BROKEN, WORKS, THIRD],
                         ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                          now=later, log=log.append))
        self.assertTrue(any(BROKEN in line and "promoted back" in line
                            for line in log),
                        "the return from a demotion is as consequential as the "
                        "demotion: %r" % (log,))

    def test_a_success_clears_the_demotion_immediately(self):
        for _ in range(2):
            ss.record_refusal(self.cool, BROKEN, "other", now=self.now)
        log = []
        self.assertTrue(ss.promote_rung(self.cool, BROKEN, log=log.append))
        self.assertNotIn(BROKEN, self.cool["models"])
        self.assertTrue(any(BROKEN in line for line in log))
        self.assertEqual([BROKEN, WORKS, THIRD],
                         ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                          now=self.now, log=lambda *_a: None))

    def test_a_demoted_rung_is_still_tried_when_everything_else_refuses(self):
        """The demotion REORDERS, it does not veto: an edition with no summaries
        is worse than one written by the rung that failed last time."""
        for _ in range(2):
            ss.record_refusal(self.cool, BROKEN, "other", now=self.now)
        ordered = ss.demoted_order([BROKEN, WORKS, THIRD], self.cool,
                                   now=self.now, log=lambda *_a: None)
        self.assertIn(BROKEN, ordered)
        self.assertEqual(len(ordered), 3)

    def test_a_demoted_lead_rung_costs_no_request(self):
        """THE DEFECT ITSELF: the run must not spend ~99 s on it first."""
        for _ in range(2):
            ss.record_refusal(self.cool, BROKEN, "other", now=self.now)
        run = FakeRun([reply_for([story(1)])])
        ss.summarise([story(1)], models=[BROKEN, WORKS],
                     run=run, sleep=lambda *_a: None,
                     cooldown=self.cool, clock=lambda: self.now)
        self.assertEqual([WORKS], [model for _p, model in run.calls])


class TheOrderIsRecomputedBetweenBatches(unittest.TestCase):
    """RUN 42070185644: the order was fixed for the whole run.

    `demoted_order` ran once, before batch 1. A rung that then refused twice --
    three times, in that edition -- was still tried first for every later batch,
    and not one demotion line appeared in the log.
    """

    def setUp(self):
        self.cool = {"schema_version": 1, "models": {}, "pool": {}}
        self.stories = [story(n) for n in range(13)]     # 6 + 6 + 1

    def run_of(self):
        batches = ss.batches(self.stories)
        return FakeRun([
            ss.Refusal("%s: exit 1: internal error" % BROKEN, kind="other"),
            reply_for(batches[0]),
            ss.Refusal("%s: exit 1: internal error" % BROKEN, kind="other"),
            reply_for(batches[1]),
            reply_for(batches[2]),
        ])

    def test_a_rung_that_failed_twice_mid_run_is_skipped_for_the_rest(self):
        log = []
        run = self.run_of()
        out = ss.summarise(self.stories, models=[BROKEN, WORKS], run=run,
                           sleep=lambda *_a: None, cooldown=self.cool,
                           log=log.append)
        self.assertEqual([BROKEN, WORKS, BROKEN, WORKS, WORKS],
                         [model for _p, model in run.calls],
                         "the third batch must not pay the broken rung again")
        self.assertTrue(any("ladder: %s demoted to the back until" % BROKEN
                            in line for line in log),
                        "the demotion has to be visible in the run that caused "
                        "it, not only in the next run's log: %r" % (log,))
        self.assertEqual(13, len(out["summaries"]),
                         "every story still gets a summary from the rung that "
                         "works")


class ADemotionSurvivesTheProcess(unittest.TestCase):
    """RUN 42144852087: seven rungs were demoted at 01:42:31, the step was killed
    by its 8-minute cap at 01:46:13, `Persist model cooldowns` found nothing to
    commit, and master kept the previous run's file. A demotion this process
    observed has to be on disk before the process can be killed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cool_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "model_cooldown.json")

    def test_a_demotion_reaches_the_file_before_the_run_ends(self):
        plan = [ss.Refusal("%s: exit 1: internal error" % BROKEN, kind="other"),
                ss.Refusal("%s: exit 1: internal error" % BROKEN, kind="other")]

        def killed(prompt, model, timeout=None):
            if plan:
                raise plan.pop(0)
            raise KeyboardInterrupt      # the step's own wall clock ending

        with self.assertRaises(KeyboardInterrupt):
            ss.summarise([story(n) for n in range(13)], models=[BROKEN],
                         run=killed, sleep=lambda *_a: None, clock=FakeClock(),
                         cooldown_path=self.path, log=lambda *_a: None)
        with open(self.path, encoding="utf-8") as fh:
            saved = json.load(fh)
        row = saved["models"][BROKEN]
        self.assertEqual("rung_broken", row["reason"])
        self.assertTrue(row.get("until"),
                        "the next run reads this file instead of paying the rung")
        self.assertEqual(2, row["hits"])

    def test_a_keyed_quota_demotion_is_saved_on_the_first_refusal(self):
        seen = []

        def quota_then_killed(prompt, model, timeout=None):
            if not seen:
                seen.append(1)
                raise ss.Refusal("quota exceeded; retry in 5s",
                                 kind="quota_notice")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            ss.summarise([story(1)],
                         models=["gemini/gemini-3.5-flash-lite"],
                         run=quota_then_killed, sleep=lambda *_a: None,
                         clock=FakeClock(), cooldown_path=self.path,
                         log=lambda *_a: None)
        with open(self.path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual("quota_exhausted",
                         saved["models"]["gemini/gemini-3.5-flash-lite"]["reason"])


#: The second Google project's rung. Same model, different key and bucket.
KEY2 = "gemini-key2/gemini-3.5-flash-lite"


class TwoGeminiKeysAreIndependentRungs(unittest.TestCase):
    """One project's 429 may not cool the other project's rung.

    Health memory is keyed by model id, and the provider prefix makes the two
    ids different, so the rows can never collide. This pins that at the
    summariser boundary, where the cooldown is written.
    """

    def setUp(self):
        self.cool = {"schema_version": 1, "models": {}, "pool": {}}

    def test_each_key_cools_on_its_own_row(self):
        ss.record_refusal(self.cool, BROKEN, "quota_notice", now=1_800_000_000.0)
        self.assertEqual({BROKEN}, set(self.cool["models"]))
        ss.record_refusal(self.cool, KEY2, "quota_notice", now=1_800_000_000.0)
        self.assertEqual({BROKEN, KEY2}, set(self.cool["models"]))
        self.assertEqual("quota_exhausted", self.cool["models"][KEY2]["reason"])

    def test_a_refused_first_key_does_not_stop_the_second(self):
        run = FakeRun([
            ss.Refusal("%s: quota exhausted" % BROKEN, kind="quota_notice"),
            reply_for([story(1)]),
        ])
        out = ss.summarise([story(1)], models=[BROKEN, KEY2], run=run,
                           sleep=lambda *_a: None, cooldown=self.cool,
                           clock=lambda: 1_800_000_000.0)
        self.assertEqual([BROKEN, KEY2], [model for _p, model in run.calls],
                         "the second project is the next rung, not a retry")
        self.assertIn(BROKEN, self.cool["models"])
        self.assertNotIn(KEY2, self.cool["models"],
                         "the second project's rung answered and stays clean")
        self.assertEqual(1, len(out["summaries"]))


#: A publisher description the deriver may REFUSE to print (a veto, an excluded
#: keyword) but which a model can still summarise safely. It is source text.
DESCRIPTION = ("The Texas Department of Transportation says the Loop 1604 ramp "
               "at Highway 90 closes at 21:00 on Thursday for resurfacing, and "
               "westbound drivers should use the Frontage Road instead.")


class ThePromptCarriesTheSourceText(unittest.TestCase):
    """RUN 42321136172: the repair asked the model to rewrite the stub itself.

    `build_prompt` sent `story["summary"]` as the snippet. On the repair path
    that is the stub -- "Reported: <headline>", or the deriver's own "No
    summary available." -- so the model was handed the sentence it was being
    asked to replace, and the prompt's "never invent... use an empty string"
    rule left it nothing to say. The candidate's publisher description is the
    source text, and it was in `snippet_index` all along.
    """

    def test_the_candidate_snippet_beats_the_storys_own_summary(self):
        batch = [story(1, snippet="Reported: " + ENOUGH)]
        prompt = ss.build_prompt(batch, {"https://example.com/1": DESCRIPTION})
        self.assertIn("Loop 1604", prompt)
        self.assertNotIn("Reported: " + ENOUGH, prompt,
                         "the stub must not be sent as the story's text")

    def test_a_story_with_no_candidate_snippet_keeps_its_own_text(self):
        batch = [story(1, snippet=ENOUGH)]
        self.assertIn(ENOUGH[:40], ss.build_prompt(batch))

    def test_an_index_is_the_only_evidence_and_a_missing_url_sends_none(self):
        """RUN 42356298224: with an index in hand, a story the index does not
        know must send NO evidence. The fallback was `story["summary"]`, which on
        the repair path IS the stub -- five answers quoted the snippet straight
        back and the checker refused every one."""
        batch = [story(1, snippet=ENOUGH)]
        prompt = ss.build_prompt(batch, {"https://other/1": DESCRIPTION})
        items = json.loads(prompt.split("Stories:\n", 1)[1])
        self.assertEqual("", items[0]["evidence"],
                         "the stub must never be sent as evidence: %r" % (prompt,))
        self.assertNotIn(ENOUGH[:40], prompt)

    def test_the_prompt_forbids_copying_the_evidence_verbatim(self):
        """The rule has to be stated, not merely implied by "never invent"."""
        self.assertIn("NEVER copy, quote or lightly reorder the evidence",
                      ss.PROMPT)
        self.assertIn("refused by the checker", ss.PROMPT)
        self.assertIn("`evidence`", ss.PROMPT)
        self.assertIn("adds nothing to the title", ss.PROMPT)

    def test_summarise_threads_the_snippet_index_into_the_prompt(self):
        batch = [story(1, snippet="No summary available.")]
        run = FakeRun([reply_for(batch)])
        ss.summarise(batch, models=["gemini/a"], run=run, sleep=lambda *_a: None,
                     clock=FakeClock(), log=lambda *_a: None,
                     snippets={"https://example.com/1": DESCRIPTION})
        self.assertIn("Loop 1604", run.calls[0][0])


class TheOutcomeIsDiagnosable(unittest.TestCase):
    """A failed run has to say WHY, and how close any rung came.

    RUN 42321136172 reported "budget spent after 12 rungs, 0 repaired". Twelve
    was the debug-record cap, not the number of attempts, and nothing in the log
    named a reason or a rung.
    """

    def test_the_reasons_are_counted_per_attempt(self):
        batch = [story(1)]
        run = FakeRun([
            ss.Refusal("gemini/a: no answer in 20s (killed)", kind="timeout"),
            reply_for(batch, "Too short."),
            reply_for(batch),
        ])
        out = ss.summarise(batch, models=["gemini/a", "gemini/b", "gemini/c"],
                           run=run, sleep=lambda *_a: None, clock=FakeClock(),
                           log=lambda *_a: None, budget_seconds=100)
        self.assertEqual({"timeout": 1, "no_usable_row": 1}, out["reasons"])
        self.assertEqual(2, out["attempts"])
        self.assertEqual("gemini/c", out["closest"]["model"],
                         "the rung that answered is the one that came closest")
        self.assertEqual(1, out["closest"]["accepted"])

    def test_attempts_are_counted_past_the_debug_cap(self):
        batch = [story(1)]
        run = FakeRun([reply_for(batch, "Too short.")])
        models = ["gemini/m%d" % n for n in range(ss.DEBUG_MAX_RECORDS + 2)]
        out = ss.summarise(batch, models=models, run=run, sleep=lambda *_a: None,
                           clock=FakeClock(), log=lambda *_a: None,
                           budget_seconds=600)
        self.assertEqual(ss.DEBUG_MAX_RECORDS, len(out["debug"]),
                         "the artifact keeps its bound")
        self.assertEqual(len(models), out["attempts"],
                         "the count must be the rungs the run actually paid for, "
                         "not the size of the capped artifact")


if __name__ == "__main__":
    unittest.main()
