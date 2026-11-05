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
import sys
import tempfile
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

    def test_a_request_cannot_outlive_the_remaining_budget(self):
        clock = FakeClock()
        run = FakeRun([reply_for([story(1)])])
        ss.summarise([story(1)], models=["gemini/a"], run=run,
                     sleep=clock.sleep, clock=clock, log=lambda *a: None,
                     budget_seconds=10)
        self.assertLessEqual(run.timeouts[0], 10,
                             "a 10s budget cannot grant a %ss call"
                             % (run.timeouts[0],))

    def test_a_hanging_first_rung_leaves_time_for_the_second(self):
        clock = FakeClock()
        batch = [story(1)]
        run = HangingRun(clock, reply_for(batch))
        out = ss.summarise(batch, models=["gemini/a", "gemini/b"], run=run,
                           clock=clock, sleep=clock.sleep, log=lambda *a: None,
                           budget_seconds=100)
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
                           budget_seconds=120)
        self.assertGreater(len(run.seen), 1,
                           "one hanging rung must not consume the whole job: "
                           "%r" % (run.seen,))


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


if __name__ == "__main__":
    unittest.main()
