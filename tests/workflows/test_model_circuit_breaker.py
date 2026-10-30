#!/usr/bin/env python3
"""The digest walk's circuit breaker: classify, skip, abort, watchdog.

WHAT THIS FILE EXISTS FOR
-------------------------
On 2026-10-25 CI tried seven model IDs. Four were absent from the live
catalogue; each failed in ~3s and the CLI reported it as
`UnknownError: Unexpected server error`, so nothing in the log said "this model
does not exist". Every dead ID still consumed an attempt, and a hung one could
consume the full 360s per-attempt allowance. This suite pins the four things
that make a dead model cheap:

  1. CLASSIFY -- every failed attempt maps to exactly one class, from the CLI's
     own words. `UnknownError` alone is an OUTAGE, never "model gone";
  2. SKIP -- model_not_found and quota_or_rate_limited move to the next model
     without spending the allowance;
  3. ABORT -- auth stops the whole walk, because another model cannot fix a
     credential;
  4. WATCHDOG -- an attempt that prints nothing for the window is abandoned,
     and the all-fail walk is bounded by arithmetic, not by hope.

The walk tests run the SHIPPED `Run OpenCode agent` step, out of the parsed
YAML, through the harness in test_agent_walk_step.py -- not a copy of it.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import model_selector as ms  # noqa: E402
import model_watchdog as mw  # noqa: E402
from tests.workflows.test_agent_walk_step import AgentWalkHarness  # noqa: E402

PROVIDER = "opencode"


# Captured from the CLI. The first pair is the REAL not-found shape: the
# surface says `UnknownError`, and only the debug log carries the marker.
REAL_NOT_FOUND = (
    "UnknownError: Unexpected server error\n"
    "ProviderModelNotFoundError: Model not found: "
    "opencode/muse-spark-1.3-contributor-free\n"
)
REAL_UNKNOWN_NO_MARKER = "UnknownError: Unexpected server error\n"
REAL_QUOTA = "429 Too Many Requests\nRetry-After: 30\n"
REAL_AUTH = "401 Unauthorized: invalid api key\n"
REAL_OUTAGE = "502 Bad Gateway\n"
REAL_WATCHDOG = "%s no output for 60s; attempt abandoned\n" % ms.WATCHDOG_MARKER
#: Captured 2026-10-26 from flint-40b-free, which printed this FIVE times from
#: 12.6s, produced zero tokens, and still burned its whole allowance.
REAL_ENDPOINT_GONE = ("AI_APICallError: Upstream request failed: "
                      "Endpoint is unavailable.")


def _stamp(line):
    """The CLI prefixes its log lines with a clock."""
    return "12:34:56.789 " + line


class ClassifierTests(unittest.TestCase):
    def test_every_captured_failure_maps_to_exactly_one_class(self):
        table = (
            (REAL_NOT_FOUND, 1, "model_not_found"),
            ("ProviderModelNotFoundError: Model not found: %s/x" % PROVIDER,
             1, "model_not_found"),
            (REAL_QUOTA, 1, "quota_or_rate_limited"),
            ("daily quota exhausted", 1, "quota_or_rate_limited"),
            (REAL_AUTH, 1, "auth"),
            ("403 Forbidden", 1, "auth"),
            ("invalid api key", 1, "auth"),
            (REAL_WATCHDOG, 124, "slow_or_stalled"),
            ("", 124, "slow_or_stalled"),
            (REAL_OUTAGE, 1, "provider_outage"),
            ("service unavailable", 1, "provider_outage"),
            ("", 1, "other"),
            ("some unrelated crash", 17, "other"),
        )
        for text, rc, expected in table:
            with self.subTest(text=text[:40], rc=rc):
                got = ms.classify_failure(text, rc)
                self.assertEqual(expected, got)
                self.assertIn(got, ms.FAILURE_CLASSES)

    def test_unknown_error_alone_is_an_outage_not_a_missing_model(self):
        """THE AMBIGUITY, PINNED. The CLI prints this exact line for BOTH a
        missing model and a 5xx. Treating it as not-found on its own would
        silently drop a model that is merely having a bad afternoon."""
        self.assertEqual("provider_outage",
                         ms.classify_failure(REAL_UNKNOWN_NO_MARKER, 1))
        self.assertEqual("model_not_found",
                         ms.classify_failure(REAL_NOT_FOUND, 1))

    def test_a_status_inside_a_larger_number_is_not_matched(self):
        self.assertEqual("other", ms.classify_failure("took 1500ms", 1))
        self.assertEqual("other", ms.classify_failure("request-id 4290", 1))

    def test_a_millisecond_stamp_is_not_an_http_status(self):
        """THE LINE THAT ABORTED A WALK WITH NO CREDENTIAL PROBLEM IN IT.

        Captured 2026-10-27 16:18:47Z, attempt 10 of 12 -- the last failure
        before the walk stopped:

            timestamp=2026-10-27T16:18:47.401Z level=INFO run=576d6804 message="init"
            AI_APICallError: Upstream request failed: Model is unavailable.

        The fatal line is an outage. `47.401Z` matched the 401 rule because the
        character before it is a dot, not a digit, so the attempt classified
        `auth`, the walk aborted six quota refusals deep, and the run reported a
        credential that had never been refused. The same hole made `.403Z`,
        `.429Z`, `.500Z` and `.503Z` possible -- a whole family of misreads from
        one missing boundary -- so every one of them is pinned here.
        """
        measured = ('timestamp=2026-10-27T16:18:47.401Z level=INFO '
                    'run=576d6804 message="all formatters are disabled"\n'
                    'AI_APICallError: Upstream request failed: '
                    'Model is unavailable.\n')
        self.assertEqual("provider_outage", ms.classify_failure(measured, 1))
        for millis in (".401Z", ".403Z", ".429Z", ".500Z", ".502Z", ".503Z"):
            stamp = ("timestamp=2026-10-27T16:18:47%s level=INFO message=init"
                     % millis)
            self.assertEqual("other", ms.classify_failure(stamp, 1),
                             "%r is a clock, not a status code" % stamp)
        # AND A REAL STATUS IS STILL MATCHED: this boundary may not be widened
        # into a fix that hides a 401.
        self.assertEqual("auth", ms.classify_failure("HTTP 401 Unauthorized", 1))
        self.assertEqual("auth", ms.classify_failure("status code 403", 1))
        self.assertEqual("auth", ms.classify_failure("HTTP/1.1 403 Forbidden", 1))
        self.assertEqual("quota_or_rate_limited",
                         ms.classify_failure("HTTP 429 Too Many Requests", 1))
        self.assertEqual("provider_outage",
                         ms.classify_failure("HTTP 503 Service Unavailable", 1))

    def test_the_measured_attempt_that_aborted_the_walk_is_not_an_auth(self):
        """THE FIXTURE IS THE REAL LOG, not a paraphrase of it.

        tests/fixtures/walk_auth_abort_2026_10_27.txt holds attempt 10 of run
        40539483837 verbatim, including the CLI's own `...T16:18:47.401Z`
        timestamps. That attempt ended the walk with

            auth: <the model> was rejected (credential), walk aborted

        while the model's actual failure was `Model is unavailable` and nothing
        in the log was ever refused. The watchdog's repeat-breaker had already
        named its own kind (`kind=provider_outage`), which is what the corrected
        classification reports -- and, above all, NOT `auth`.
        """
        path = REPO_ROOT / "tests" / "fixtures" / "walk_auth_abort_2026_10_27.txt"
        text = path.read_text(encoding="utf-8")
        self.assertIn("was rejected (credential), walk aborted", text,
                      "the fixture must still carry the verdict this fix exists "
                      "to make impossible, or it proves nothing")
        self.assertIn(".401Z", text,
                      "the fixture must keep the millisecond stamp that caused "
                      "the misread")
        self.assertEqual("provider_outage", ms.classify_failure(text, 1),
                         "the attempt was an outage the watchdog had already "
                         "declared, and the decimal field must not turn it into "
                         "an auth refusal")
        self.assertNotEqual("auth", ms.classify_failure(text, 1))

    def test_auth_outranks_quota_and_outage(self):
        text = "401 Unauthorized after 503 from gateway"
        self.assertEqual("auth", ms.classify_failure(text, 1))

    def test_the_measured_free_usage_rejection_is_quota(self):
        """THE LINE THAT COST 480s. Captured 2026-10-25, 1.0s into the call:

            stream error error.error="AI_APICallError: Free usage limit reached."

        It contains neither "quota" nor "rate limit", so before this it was
        classified "other" -- and an "other" is not skipped, it is waited on.
        """
        line = ('15:42:09.535 stream error '
                'error.error="AI_APICallError: Free usage limit reached."\n')
        self.assertEqual("quota_or_rate_limited", ms.classify_failure(line, 1))
        self.assertEqual("quota_or_rate_limited", ms.classify_failure(
            "AI_APICallError: Free usage limit reached.", 1))

    def test_a_streamed_decisive_error_is_a_kill_signal(self):
        """Only error-shaped lines can be decisive. The four real shapes are,
        and the class they yield is what the walk acts on."""
        table = (
            ('stream error error.error="AI_APICallError: Free usage limit '
             'reached."', "quota_or_rate_limited"),
            ("ProviderModelNotFoundError: Model not found: opencode/x",
             "model_not_found"),
            ("401 Unauthorized: invalid api key", "auth"),
            ("429 Too Many Requests (Retry-After: 30)", "quota_or_rate_limited"),
        )
        for line, expected in table:
            with self.subTest(line=line[:40]):
                self.assertEqual(expected, ms.classify_stream_line(line), line)

    def test_prose_on_the_stream_never_kills_a_working_attempt(self):
        """A digest article can say "rate limit" or "quota". Killing a WORKING
        attempt over its own text would be a new way to lose a run, so a line
        must be error-shaped before its class is even considered."""
        for prose in (
            "Draft note: the exchange kept the rate limit and the quota unchanged.",
            "The central bank said the quota system would be reviewed.",
            "Protesters called the order unauthorized.",
            "UnknownError: Unexpected server error",   # outage: NOT decisive
            "502 Bad Gateway",                          # outage: NOT decisive
            "The 429 passengers were rescued from the ferry.",  # a real number
            "",
        ):
            with self.subTest(prose=prose[:40]):
                self.assertIsNone(ms.classify_stream_line(prose), prose)

    def test_only_four_classes_are_decisive(self):
        # payment_required joined on 2026-10-30: a plan refusal is instant and
        # permanent for that provider, so waiting the silence window out buys
        # nothing but wall clock.
        self.assertEqual(("model_not_found", "quota_or_rate_limited", "auth",
                          "payment_required"),
                         ms.DECISIVE_CLASSES)
        for kind in ("provider_outage", "slow_or_stalled", "other"):
            self.assertNotIn(kind, ms.DECISIVE_CLASSES)

    def test_the_measured_endpoint_failure_is_an_outage(self):
        """THE LINE THAT COST FLINT ITS WHOLE ALLOWANCE. Captured 2026-10-26:

            AI_APICallError: Upstream request failed: Endpoint is unavailable.

        It matched no marker, so it classified `other` -- and an `other` is not
        skipped, it is waited on. It is an OUTAGE, and an outage is deliberately
        NOT decisive (a single 5xx may recover), so the kill comes from the
        repeat breaker instead. Both halves are asserted here.
        """
        self.assertEqual("provider_outage",
                         ms.classify_failure(REAL_ENDPOINT_GONE, 1))
        self.assertEqual("provider_outage",
                         ms.classify_failure(_stamp(REAL_ENDPOINT_GONE), 1))
        self.assertIsNone(ms.classify_stream_line(REAL_ENDPOINT_GONE),
                          "an outage must not be decisive: one 5xx is not death")
        self.assertEqual(3, ms.REPEAT_OUTAGE_LIMIT)

    def test_a_plan_refusal_is_its_own_class_and_earns_no_cooldown(self):
        """ADDED 2026-10-30 with the second provider.

        The wording is the plan's own, and it OVERLAPS quota ("usage limit").
        If quota won, the refusal would read as transient throttling and the
        ladder would be walked rung by rung to hear it again.
        """
        refusal = ("AI_APICallError: Free usage limit reached. To keep going, "
                   "subscribe to OpenCode Go.")
        self.assertEqual("payment_required", ms.classify_failure(refusal, 1))
        self.assertEqual("payment_required", ms.classify_stream_line(refusal),
                         "a hanging CLI must be killed on this line, not waited "
                         "on for the whole silence window")
        self.assertNotIn("payment_required", ms.COOLDOWN_SEC,
                         "a hard cooldown would remove a provider the walk "
                         "needs; the plan is recorded, never cooled")
        self.assertEqual("transport", ms.HEALTH_KIND_OF["payment_required"],
                         "health memory must map it without inventing a kind")
        self.assertEqual("payment_required",
                         ms.classify_failure("402 Payment Required", 1))
        self.assertEqual("payment_required",
                         ms.classify_stream_line("402 Payment Required"))
        # NARROWNESS, PINNED: the ordinary free-tier limit keeps its own class.
        self.assertEqual("quota_or_rate_limited",
                         ms.classify_failure("Free usage limit reached", 1))


class DiscoveryGateTests(unittest.TestCase):
    """The catalogue's own fields decide membership -- no denylist anywhere."""

    @staticmethod
    def _listing(status_line, model_id="dead-one"):
        return (
            "%s/%s\n"
            "{\n"
            '  "id": "%s",\n'
            '  "cost": {"input": 0, "output": 0},\n'
            '  "capabilities": {"input": {"text": true}, '
            '"output": {"text": true}},\n'
            "%s"
            '  "release_date": "2026-10-01"\n'
            "}\n"
        ) % (PROVIDER, model_id, model_id, status_line)

    def test_a_model_without_an_active_status_is_rejected(self):
        for status_line in ('  "status": "deprecated",\n', ""):
            with self.subTest(status=status_line.strip() or "missing"):
                ms.reset_discovery_memo()
                proc = type("P", (), {
                    "returncode": 0,
                    "stdout": self._listing(status_line),
                    "stderr": ""})()

                def run_cmd(_argv, _timeout, proc=proc):
                    return proc

                sd = tempfile.mkdtemp(prefix="gate-")
                try:
                    models, info = ms.discover(
                        PROVIDER, cache_path=os.path.join(sd, "c.json"),
                        run_cmd=run_cmd)
                finally:
                    import shutil
                    shutil.rmtree(sd, ignore_errors=True)
                    ms.reset_discovery_memo()
                self.assertEqual([], models, (models, info))
                self.assertTrue(
                    list(info["rejected"].values())[0].startswith("status:"),
                    info)

    def test_a_model_the_catalogue_does_not_list_can_never_be_discovered(self):
        """The four dead IDs were ABSENT from the catalogue, so the only way
        they can reach a walk is a file that remembers them. A pre-gate cache
        is refused, which drops the tier to the visible config seed instead."""
        sd = tempfile.mkdtemp(prefix="cache-gate-")
        try:
            cache = os.path.join(sd, "model_cache.json")
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump({"provider": PROVIDER,
                           "models": ["%s/mimo-v2.5-free" % PROVIDER]}, fh)
            ms.reset_discovery_memo()

            def boom(_argv, _timeout):
                raise OSError("opencode not on path")

            models, info = ms.discover(PROVIDER, cache_path=cache,
                                       run_cmd=boom)
            self.assertEqual([], models, info)
            self.assertEqual("none", info["source"], info)
        finally:
            import shutil
            shutil.rmtree(sd, ignore_errors=True)
            ms.reset_discovery_memo()


class WatchdogTests(unittest.TestCase):
    def test_a_silent_attempt_is_abandoned_at_the_window(self):
        """A child that prints NOTHING is killed at the window, and the marker
        is printed so the classifier can say why the attempt died."""
        sink = io.StringIO()
        started = time.monotonic()
        rc = mw.supervise(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            no_output_sec=1, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(124, rc, "a watchdog kill must look like a timeout")
        self.assertLess(elapsed, 15,
                        "the watchdog did not abandon the silent attempt: "
                        "%.1fs" % elapsed)
        self.assertIn(ms.WATCHDOG_MARKER, sink.getvalue())

    def test_a_talking_attempt_is_not_killed(self):
        """Silence, not slowness, is the trigger -- and the only thing that
        counts as speech is the CLI's own LOG.

        Since 2026-10-27 stdout is a COMPLETION signal, not a liveness one:
        `opencode run` in non-TTY mode prints the banner and then nothing until
        the whole message lands. A child that writes plain stdout is therefore
        NOT evidence of life, and the log lines below are what keeps an attempt
        alive.
        """
        sink = io.StringIO()
        child = ("import time\n"
                 "for _ in range(6):\n"
                 "    print('timestamp=2026-10-27T10:00:00.000Z level=INFO "
                 "run=x message=loop session.id=s step=1', flush=True)\n"
                 "    time.sleep(0.4)\n")
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=1, sink=sink)
        self.assertEqual(0, rc, sink.getvalue())
        self.assertNotIn(ms.WATCHDOG_MARKER, sink.getvalue())

    def test_plain_stdout_does_not_keep_a_wedged_attempt_alive(self):
        """THE OTHER DIRECTION, and the whole defect. A child whose only output
        is stdout has not demonstrated liveness, so once it STOPS it is
        abandoned at the window -- if stdout reset the clock the watchdog would
        wait out every attempt whose CLI had already died after printing its
        banner, which is exactly what happened on seven models."""
        sink = io.StringIO()
        child = ("import time\n"
                 "print('token', flush=True)\n"
                 "time.sleep(30)\n")
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=1, sink=sink)
        self.assertEqual(124, rc, sink.getvalue())
        self.assertIn(ms.WATCHDOG_MARKER, sink.getvalue())

    def test_the_window_follows_the_measurement_per_model(self):
        """ONE GLOBAL NUMBER CANNOT SERVE TWO MODELS 9x APART.

        Measured on the real ~65k-token prompt (2026-10-26): deepseek's first
        token at 14.59s, beam's at 137.63s. A single 60s window would kill beam
        every time and make the only other working model unreachable; a single
        window generous enough for beam would waste 180s on each dead model.
        """
        self.assertEqual(60, ms.no_output_sec_for(
            "opencode/deepseek-v4.1-flash-free"))
        self.assertEqual(240, ms.no_output_sec_for("opencode/beam-free"))
        self.assertGreaterEqual(ms.no_output_sec_for(
            "opencode/deepseek-v4.1-flash-free"), 4 * 14.59)
        self.assertGreater(ms.no_output_sec_for("opencode/beam-free"), 137.63)
        self.assertEqual(60, ms.no_output_sec_for("opencode/never-measured"))

    def test_the_watchdog_resolves_the_window_from_the_model_id(self):
        """The table is useless unless the SUPERVISOR actually consults it."""
        self.assertEqual(240, mw.resolve_window(model="opencode/beam-free"))
        self.assertEqual(60, mw.resolve_window(
            model="opencode/deepseek-v4.1-flash-free"))
        self.assertEqual(60, mw.resolve_window(model="opencode/unknown-free"))
        self.assertEqual(5, mw.resolve_window(explicit=5))

    def test_the_first_output_window_is_the_one_that_fires_before_any_text(self):
        """NOTHING EMITTED -> the FIRST-OUTPUT window, not the generous one.

        If a silent attempt were judged on the inter-token window it would cost
        240s instead of 60s, and the all-fail walk would be four times longer.
        """
        sink = io.StringIO()
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c", "import time; time.sleep(30)"],
                          no_output_sec=1, inter_token_sec=240, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(124, rc, sink.getvalue())
        self.assertLess(elapsed, 15, "the first-output window did not fire")
        self.assertIn("no output for 1s", sink.getvalue())

    def test_the_inter_token_window_does_not_fire_at_the_first_output_window(self):
        """THE REGRESSION, run 40286310047 -- and the whole reason for the split.

        The model call opens, and the CLI then goes quiet for the generation
        (measured: nothing between `llm runtime selected` and the finished
        message, 90 lines in one burst at +62.89s). That gap must be judged on
        the inter-token window, not the startup one, or every attempt that takes
        longer than its first-output window dies.
        COLLAPSE THE TWO WINDOWS INTO ONE AND THIS TEST GOES RED AT 1s.
        """
        sink = io.StringIO()
        child = ("import time\n"
                 "print('timestamp=2026-10-27T10:00:00.000Z level=INFO run=x "
                 "message=stream providerID=opencode modelID=m small=false "
                 "agent=manager mode=primary', flush=True)\n"
                 "time.sleep(4)\n"
                 "print('...and the rest of it, finished late.', flush=True)\n")
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=1, inter_token_sec=10, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(0, rc, sink.getvalue())
        self.assertNotIn(ms.WATCHDOG_MARKER, sink.getvalue(),
                         "a generation-long quiet log was read as a dead stream")
        self.assertGreaterEqual(elapsed, 4,
                                "the gap must really have exceeded the first "
                                "output window: %.1fs" % elapsed)

    def test_the_inter_token_window_fires_at_its_own_limit(self):
        """A genuinely WEDGED stream is still caught -- one window is not a
        licence to hang. The MAIN call opens, then the log stops past the
        inter-token window: 124, and the marker names the window that fired."""
        sink = io.StringIO()
        child = ("import time\n"
                 "print('timestamp=2026-10-27T10:00:00.000Z level=INFO run=x "
                 "message=stream providerID=opencode modelID=m small=false', "
                 "flush=True)\n"
                 "time.sleep(30)\n")
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=1, inter_token_sec=2, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(124, rc, sink.getvalue())
        self.assertLess(elapsed, 20, "the inter-token window never fired")
        self.assertGreaterEqual(elapsed, 2)
        self.assertIn("no output for 2s", sink.getvalue())

    def test_the_inter_token_window_covers_a_whole_generation(self):
        """420s CANNOT FIRE ON A MEASURED COMPLETION, AND 240S COULD.

        The window no longer measures a gap BETWEEN tokens -- the CLI prints no
        tokens at all before the message is complete -- so it has to cover the
        whole generation. Measured completion for this workload is 127.53s and
        CI has been observed past 240s, which is what made seven models look
        dead at exactly 240s.
        """
        self.assertEqual(420, ms.INTER_TOKEN_SEC)
        self.assertGreater(ms.INTER_TOKEN_SEC, 240,
                           "240s is the number that misfired")
        self.assertGreater(ms.INTER_TOKEN_SEC, 127.53 * 3)
        self.assertGreater(ms.INTER_TOKEN_SEC, ms.no_output_sec_for(
            "opencode/deepseek-v4.1-flash-free"),
            "the inter-token window must be the more generous of the two")
        self.assertLess(ms.INTER_TOKEN_SEC, ms.WALK_PER_ATTEMPT_SEC,
                        "the per-attempt cap must outlast the window, or the "
                        "cap fires first and the window never runs")
        self.assertEqual(420, mw.resolve_inter_token_window())
        self.assertEqual(7, mw.resolve_inter_token_window(explicit=7))

    def test_a_rejected_attempt_is_killed_at_the_error_line(self):
        """THE 480s BURN, KILLED AT +1s. The child prints the measured rejection
        line and then hangs exactly as the CLI did. The watchdog must abandon it
        on the LINE, not wait out the window."""
        sink = io.StringIO()
        child = ("import sys, time\n"
                 "print('stream error error.error=\"AI_APICallError: Free usage "
                 "limit reached.\"', flush=True)\n"
                 "time.sleep(30)\n")
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=90, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(mw.FAST_FAIL_RC, rc,
                         "a rejected attempt must not look like a timeout")
        self.assertLess(elapsed, 10,
                        "the watchdog waited for the silence window instead of "
                        "the error line: %.1fs" % elapsed)
        log = sink.getvalue()
        self.assertIn(mw.FATAL_MARKER, log)
        self.assertIn("kind=quota_or_rate_limited", log)
        self.assertNotIn(ms.WATCHDOG_MARKER, log)

    def test_a_decisive_error_wrapped_in_a_log_line_is_still_decisive(self):
        """MEASURED 2026-10-27, and it is the whole reason --print-logs needed a
        second fix. The CLI retried, then printed its final rejection INSIDE a
        log line and hung for 420s without exiting:

            level=ERROR run=... message="stream error" ...
            error.error="AI_APICallError: Free usage limit reached. ..."

        Log lines are liveness, but they are not ONLY liveness: treating them as
        a `continue` made the decisive line invisible and every rejection cost
        the full window again.
        """
        sink = io.StringIO()
        line = ('timestamp=2026-10-27T11:15:20.754Z level=ERROR run=1d846dd1 '
                'message="stream error" providerID=opencode '
                'modelID=deepseek-v4.1-flash-free small=false '
                'error.error="AI_APICallError: Free usage limit reached. Free '
                'models work in OpenCode Zen."')
        child = ("import time\nprint(%r, flush=True)\ntime.sleep(30)\n" % line)
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=90, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(mw.FAST_FAIL_RC, rc, sink.getvalue())
        self.assertLess(elapsed, 10, "%.1fs" % elapsed)
        self.assertIn("kind=quota_or_rate_limited", sink.getvalue())

    def test_repeated_log_errors_collapse_to_one_key(self):
        """A repeated outage must still be COUNTED when every repeat carries a
        different `timestamp=` and `run=`. Measured log lines always differ in
        both, so an unstripped key never reaches the repeat limit."""
        line = ('timestamp=2026-10-27T11:14:47.%03dZ level=ERROR run=1d846dd1 '
                'message="stream error" error.error="AI_APICallError: Unknown '
                'Error"')
        first = mw._normal_key(line % 100)
        second = mw._normal_key(line % 900)
        self.assertEqual(first, second)
        self.assertNotIn("1d846dd1", first)
        self.assertNotIn("timestamp=", first)

    def test_the_title_call_does_not_open_the_main_window(self):
        """MEASURED 2026-10-27: the CLI opens a SMALL call first -- title
        generation, a DIFFERENT model (`modelID=big-pickle`, `small=true`) --
        +5.8s in, before the chosen model is contacted at all. Reading that as
        "the main call is open" put every attempt on the generous window five
        seconds in and made the per-model first-output table dead code again.
        """
        sink = io.StringIO()
        title = ('timestamp=2026-10-27T11:14:26.213Z level=INFO run=x '
                 'message=stream providerID=opencode modelID=big-pickle '
                 'small=true agent=title mode=primary')
        self.assertFalse(mw.is_phase(title))
        self.assertTrue(mw.is_phase(title.replace('small=true', 'small=false')))
        self.assertTrue(mw.is_cli_log(title))
        # And end to end: a 6s sleep past a 1s first-output window must be
        # judged on the FIRST-OUTPUT window, which fires at 1s.
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c",
                           "import time\nprint(%r, flush=True)\ntime.sleep(30)\n"
                           % title],
                          no_output_sec=1, inter_token_sec=20, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(124, rc, sink.getvalue())
        self.assertIn("no output for 1s", sink.getvalue())
        self.assertLess(elapsed, 15)

    def test_a_banner_does_not_disable_the_repeat_breaker(self):
        """THE ORIGINAL SEVEN-MODEL DEATH, in one line, and why the banner must
        be rejected BEFORE the "is this assistant text" test.

        The banner arrives at ~+5.9s. Counting it as assistant text sets
        state["text"], and the repeat breaker is deliberately disabled once any
        assistant text exists (a partially working model must not be killed).
        So a banner was enough to turn the one mechanism that could end a dead
        endpoint back off -- and the attempt then burned the whole window
        instead of being abandoned in ~1s.
        """
        sink = io.StringIO()
        fatal = "AI_APICallError: Upstream request failed: Endpoint is unavailable."
        child = ("import time\n"
                 "print('\\x1b[0m> manager \\u00b7 deepseek-v4.1-flash-free', "
                 "flush=True)\n"
                 "for _ in range(5):\n"
                 "    print(%r, flush=True)\n"
                 "time.sleep(30)\n" % fatal)
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=30, sink=sink)
        elapsed = time.monotonic() - started
        self.assertTrue(mw.is_banner("> manager \u00b7 m"))
        self.assertEqual(mw.FAST_FAIL_RC, rc, sink.getvalue())
        self.assertIn("repeats=", sink.getvalue())
        self.assertLess(elapsed, 10, "the repeat breaker never fired: %.1fs"
                        % elapsed)

    def test_a_not_found_line_is_killed_too(self):
        sink = io.StringIO()
        child = ("import time\n"
                 "print('ProviderModelNotFoundError: Model not found: "
                 "opencode/gone', flush=True)\n"
                 "time.sleep(30)\n")
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=90, sink=sink)
        self.assertEqual(mw.FAST_FAIL_RC, rc)
        self.assertIn("kind=model_not_found", sink.getvalue())

    def test_prose_does_not_kill_and_the_attempt_finishes(self):
        """The other half of the fast-fail: the same watchdog must NOT kill a
        child whose output merely talks about quotas."""
        sink = io.StringIO()
        child = ("print('Draft note: the exchange kept the rate limit and the "
                 "quota unchanged.', flush=True)\n")
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=90, sink=sink)
        self.assertEqual(0, rc, sink.getvalue())
        self.assertNotIn(mw.FATAL_MARKER, sink.getvalue())

    def test_a_repeating_outage_is_abandoned_without_waiting_the_window(self):
        """THE FLINT DEATH. Its endpoint answered with the same fatal line five
        times and then hung, producing zero tokens, for the whole allowance.

        The line is NOT decisive on its own (one 5xx may recover), so the kill
        has to come from the repeat: identical fatal lines with no assistant
        text. The window here is 30s and the kill must land in ~1s, which is
        what proves the mechanism is the counter and not the silence clock.
        """
        sink = io.StringIO()
        child = ("import time\n"
                 "for _ in range(5):\n"
                 "    print('12:34:56.789 ' + %r, flush=True)\n"
                 "    time.sleep(0.3)\n"
                 "time.sleep(60)\n") % REAL_ENDPOINT_GONE
        started = time.monotonic()
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=30, sink=sink)
        elapsed = time.monotonic() - started
        self.assertEqual(mw.FAST_FAIL_RC, rc, sink.getvalue())
        self.assertLess(elapsed, 15,
                        "the attempt waited for the silence window instead of "
                        "the repeating outage: %.1fs" % elapsed)
        log = sink.getvalue()
        self.assertIn(mw.FATAL_MARKER, log)
        self.assertIn("kind=provider_outage", log)
        self.assertIn("repeats=3", log)
        self.assertNotIn(ms.WATCHDOG_MARKER, log,
                         "this is a repeating outage, not silence")

    def test_the_repeat_threshold_is_three_so_one_bad_second_keeps_its_chance(self):
        """THE ASSERTION THAT KEEPS THE BREAKER FROM BEING A MARKER BLUDGEON.

        One fatal line must never kill an attempt, and neither must two. Only
        the third identical line -- with zero assistant text produced -- means
        the endpoint is gone rather than flaky.
        """
        for count, expected_rc in ((1, 0), (2, 0), (3, mw.FAST_FAIL_RC)):
            with self.subTest(lines=count):
                sink = io.StringIO()
                child = ("import time\n"
                         "for _ in range(%d):\n"
                         "    print('12:34:56.789 ' + %r, flush=True)\n"
                         "for _ in range(3):\n"
                         "    print('assistant text', flush=True)\n"
                         "    time.sleep(0.2)\n") % (count, REAL_ENDPOINT_GONE)
                rc = mw.supervise([sys.executable, "-c", child],
                                  no_output_sec=30, sink=sink)
                self.assertEqual(expected_rc, rc, sink.getvalue())

    def test_a_repeating_line_after_assistant_text_is_not_an_outage_kill(self):
        """A model that has ALREADY produced text is partially working; killing
        it would throw the work away. The rule is 'zero assistant tokens'."""
        sink = io.StringIO()
        child = ("import time\n"
                 "print('Here is the digest:', flush=True)\n"
                 "for _ in range(5):\n"
                 "    print('12:34:56.789 ' + %r, flush=True)\n"
                 "    time.sleep(0.2)\n") % REAL_ENDPOINT_GONE
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=30, sink=sink)
        self.assertEqual(0, rc, sink.getvalue())
        self.assertNotIn(mw.FATAL_MARKER, sink.getvalue())


class AllFailBoundTests(unittest.TestCase):
    def test_the_arithmetic_of_a_silent_all_fail_walk(self):
        """THE SILENCE BOUND, COMPUTED FROM THE TABLE.

            gemini x2 60 + 60            <- unmeasured rungs, default window
            deepseek 60 + beam 240 + mimo 60 + nemotron 60 + glm 60 + flint 60
                = 660s = 11 min

        Before the per-model table every rung cost one global window; before
        the watchdog a dead rung cost its full cap. The point of computing it
        FROM the table is that adding, removing or re-measuring a model moves
        this number without anyone editing a test -- which is exactly what
        happened on 2026-10-30 when two rungs joined carrying no measurement.
        """
        ladder = ms.ladder_model_ids()
        self.assertEqual(8, len(ladder), ladder)
        self.assertEqual(660, ms.worst_case_all_fail_sec(ladder))
        self.assertEqual(660, ms.worst_case_all_fail_sec(),
                         "the default must be the configured ladder")
        self.assertEqual(690, ms.worst_case_all_fail_sec(ladder,
                                                        overhead_sec=30))
        # A ninth, unmeasured rung costs only the default window.
        self.assertEqual(720, ms.worst_case_all_fail_sec(
            ladder + ["opencode/some-new-free"]))
        self.assertLess(660, 1800,
                        "the all-silent bound must stay inside the walk budget")
        self.assertLess(660, 8 * ms.WALK_PER_ATTEMPT_SEC)

    def test_a_ladder_without_beam_is_cheaper_which_proves_the_table_is_read(self):
        """If the table were ignored, dropping beam would change nothing."""
        ladder = [m for m in ms.ladder_model_ids()
                  if m != "opencode/beam-free"]
        # beam's 240s window is the only measured one above the default, so
        # removing it must take exactly 240s off the 660s total.
        self.assertEqual(420, ms.worst_case_all_fail_sec(ladder))
        self.assertLess(ms.worst_case_all_fail_sec(ladder), 660)

    def test_the_arithmetic_of_an_emit_then_stall_walk(self):
        """THE OTHER WORST CASE, and a different number from the silence bound.

        An attempt that never opens the model call costs its FIRST-OUTPUT window
        (660s over the eight rungs). An attempt that opens the call and then goes
        quiet is measured from its last LOG line, so it costs
        first-output + INTER_TOKEN, truncated by the per-attempt cap:

            deepseek   min( 60 + 420, 480) = 480
            beam       min(240 + 420, 480) = 480   <- the cap is binding
            the other six                  = 480 each
            = 3840s

        Longer than the 1800s walk budget, which is the honest reading: in this
        mode the budget and the cap bound the run, not the windows.
        """
        ladder = ms.ladder_model_ids()
        self.assertEqual(8, len(ladder), ladder)
        self.assertEqual(3840, ms.worst_case_emit_then_stall_sec(ladder))
        self.assertEqual(3840, ms.worst_case_emit_then_stall_sec(),
                         "the default must be the configured ladder")
        self.assertEqual(3870, ms.worst_case_emit_then_stall_sec(
            ladder, overhead_sec=30))
        self.assertEqual(
            480,
            min(ms.no_output_sec_for("opencode/beam-free") + ms.INTER_TOKEN_SEC,
                ms.WALK_PER_ATTEMPT_SEC),
            "beam's ceiling must be the cap, not the sum of the windows")
        self.assertGreater(ms.worst_case_emit_then_stall_sec(ladder),
                           ms.worst_case_all_fail_sec(ladder),
                           "emitting then stalling must cost MORE than silence")
        self.assertLessEqual(ms.worst_case_emit_then_stall_sec(ladder),
                             8 * ms.WALK_PER_ATTEMPT_SEC)

    def test_the_arithmetic_of_a_rejected_all_fail_walk(self):
        """THE OBSERVED 2026-10-25 MODE: every model rejected at ~1s.

        6 x 2s + 30s = 42s. Before the fast-fail each of those six burned its
        full 480s cap -- 2,880s of wall clock producing zero tokens, which is
        how a 19-minute slot disappeared.
        """
        self.assertEqual(2, ms.FAST_FAIL_SEC)
        self.assertEqual(42, ms.fast_fail_all_fail_sec(6))
        self.assertLess(ms.fast_fail_all_fail_sec(6), 60)
        self.assertLess(ms.fast_fail_all_fail_sec(6),
                        ms.worst_case_all_fail_sec(ms.ladder_model_ids()),
                        "a rejection must be cheaper than silence")
        self.assertLess(ms.FAST_FAIL_SEC, ms.NO_OUTPUT_DEFAULT_SEC)


# ==========================================================================
# The SHIPPED walk, executed
# ==========================================================================


class ShippedWalkTests(AgentWalkHarness):
    def attempt_log(self):
        path = Path(self.tmp) / "state" / "agent-attempts.log"
        return path.read_text(encoding="utf-8").splitlines()

    def test_not_found_skips_to_the_next_model(self):
        proc, art = self.run_step(
            pool=["opencode/dead", "opencode/live"],
            plan="opencode/dead:notfound opencode/live:ok")
        self.assertGreen(proc, "not-found skip")
        self.assertEqual(["opencode/dead", "opencode/live"], art["models"],
                         "the dead model must be tried once and skipped, then "
                         "the next model must be tried")
        self.assertIn("Skipping to the next model", proc.stdout)
        self.assertIn("kind=model_not_found", "\n".join(self.attempt_log()))
        self.assertFalse(
            [r for r in art["records"] if r == ["opencode/dead", "timeout"]],
            "a not-found must not be recorded as a timeout: it is not slow, it "
            "is absent")

    def test_quota_stops_the_walk_and_records_the_shared_pool(self):
        """CHANGED DELIBERATELY, from measurement (see test_model_quota_retry).

        This test used to pin "a quota skips to the NEXT model". That is exactly
        the multiplier: the refusal is pre-generation (312ms after
        `message=stream`, tokens.input=0) and six ids were refused inside 16s, so
        every further rung cost an attempt to hear one fact about the bucket. The
        walk now records that fact once and stops.
        """
        proc, art = self.run_step(
            pool=["opencode/limited", "opencode/live"],
            plan="opencode/limited:quota opencode/live:ok")
        self.assertEqual(
            1, proc.returncode,
            "no digest was produced, so the step must fail. stdout:\n%s"
            % proc.stdout)
        self.assertEqual(
            ["opencode/limited"], art["models"],
            "the next model asks the same bucket, so it must not be tried: %r"
            % (art["models"],))
        self.assertIn("kind=quota_or_rate_limited", "\n".join(self.attempt_log()))

    def test_auth_is_per_model_until_it_repeats(self):
        """CHANGED DELIBERATELY ON 2026-10-27, from measurement.

        This test used to pin the opposite rule -- one `auth` ends the walk --
        on the reasoning that one bad credential cannot be fixed by another
        model. Run 40539483837 is the counter-evidence: attempt 10 of 12
        classified `auth` (a millisecond stamp read as a 401, see
        test_a_millisecond_stamp_is_not_an_http_status), the walk stopped six
        quota refusals deep, and the 3/6/12-minute backoff cycles that exist for
        exactly that wall never ran. The edition was lost in 75s.

        The credential IS shared, so it still stops the walk -- but only when the
        refusal actually repeats across models. One model refusing is that
        model's entitlement, and the next model must be tried.
        """
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b", "opencode/c"],
            plan="opencode/a:auth opencode/b:ok opencode/c:ok")
        self.assertGreen(proc, "one auth, then a success")
        self.assertEqual(
            ["opencode/a", "opencode/b"], art["models"],
            "one auth must not stop the walk: %r" % (art["models"],))
        self.assertIn("trying the next model", proc.stdout)
        self.assertNotIn("walk aborted", proc.stdout)

    def test_two_auth_refusals_stop_the_walk(self):
        """The half of the old rule that is still true: a shared key is fatal."""
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b", "opencode/c"],
            plan="opencode/a:auth opencode/b:auth opencode/c:ok")
        self.assertEqual(1, proc.returncode,
                         "an aborted walk must fail loudly. stdout:\n%s"
                         % proc.stdout)
        self.assertEqual(["opencode/a", "opencode/b"], art["models"],
                         "two refusals must stop the walk: %r"
                         % (art["models"],))
        # REWORDED 2026-10-30: the refusal now abandons the PROVIDER, so the
        # message says what actually happens. Here every rung is the same
        # provider, which is why the walk still ends.
        self.assertIn("skipped and the walk continues with the next provider",
                      proc.stdout)
        self.assertIn("auth:", proc.stdout)

    def test_two_auth_refusals_on_two_providers_do_not_abort_the_walk(self):
        """THE CLASS FIX for the second provider (2026-10-30).

        A credential belongs to a PROVIDER. Counting auth across providers is
        how a bad Gemini key alone would have aborted the opencode fallback --
        two refusals that say nothing about each other.
        """
        proc, art = self.run_step(
            pool=["gemini/a", "gemini/b", "opencode/c"],
            plan="gemini/a:auth gemini/b:auth opencode/c:ok")
        self.assertGreen(proc, "two providers, one bad key")
        self.assertEqual(["gemini/a", "gemini/b", "opencode/c"], art["models"],
                         "the fallback provider must still be tried: %r"
                         % (art["models"],))

    def test_a_plan_refusal_is_recorded_and_never_poisons_the_pool(self):
        """C5/C6, PROVEN END TO END.

        The measured plan string classifies as payment_required, the walk goes
        on, TWO refusals retire that provider (not the walk), the fallback
        provider answers, and no shared-pool row is written -- a pool row is
        what used to silence every rung for the whole grace window.
        """
        proc, art = self.run_step(
            pool=["gemini/a", "gemini/b", "opencode/c"],
            plan="gemini/a:planrefused gemini/b:planrefused opencode/c:ok")
        self.assertGreen(proc, "plan refusal then fallback")
        self.assertEqual(["gemini/a", "gemini/b", "opencode/c"], art["models"])
        self.assertIn(["gemini/a", "payment_required"], art["records"])
        self.assertIn(["gemini/b", "payment_required"], art["records"])
        calls = (self.tmp / "record_calls.txt").read_text(encoding="utf-8")
        self.assertNotIn("--pool-refused", calls,
                         "a plan refusal says nothing about the shared bucket, "
                         "so it may never write that row: %s" % calls)
        log = "\n".join(self.attempt_log())
        self.assertIn("kind=payment_required", log)
        self.assertNotIn("kind=quota_or_rate_limited", log,
                         "the plan string also says 'usage limit'; misclassifying"
                         " it as quota is the defect this class exists for")
        self.assertIn("skipped and the walk continues with the next provider",
                      proc.stdout)

    def test_one_plan_refusal_keeps_the_provider_alive(self):
        """One refusal may be that id's own allowance, so the next rung runs."""
        proc, art = self.run_step(
            pool=["gemini/a", "gemini/b"],
            plan="gemini/a:planrefused gemini/b:ok")
        self.assertGreen(proc, "one plan refusal")
        self.assertEqual(["gemini/a", "gemini/b"], art["models"])
        self.assertNotIn("skipped and the walk continues", proc.stdout)

    def test_a_second_providers_quota_writes_no_pool_row(self):
        """C5's companion rule, proven: the pool row gates EVERY later walk, so
        one provider's own 429 must not write it."""
        proc, art = self.run_step(
            pool=["gemini/a", "opencode/c"],
            plan="gemini/a:quota opencode/c:ok")
        self.assertGreen(proc, "second provider quota")
        self.assertEqual(["gemini/a", "opencode/c"], art["models"])
        calls = (self.tmp / "record_calls.txt").read_text(encoding="utf-8")
        self.assertNotIn("--pool-refused", calls, calls)

    def test_a_pool_quota_still_writes_the_row_it_always_did(self):
        """The rule is scoped to OTHER providers, not removed."""
        proc, _art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:ok")
        self.assertEqual(1, proc.returncode)
        calls = (self.tmp / "record_calls.txt").read_text(encoding="utf-8")
        self.assertIn("--pool-refused", calls, calls)

    def test_the_preflight_warns_and_the_pool_still_runs(self):
        """C9: a provider that never resolved is a WARNING, not a failure.

        Without OPENCODE_CONFIG (or with a wrong config shape) the Gemini rungs
        fail exactly like dead models. The step must discover that BEFORE the
        walk, say so, and use the pool.
        """
        proc, art = self.run_step(
            pool=["opencode/a"], plan="opencode/a:ok",
            extra_env={"STUB_OPENCODE_MODELS": ""})
        self.assertGreen(proc, "no gemini provider")
        self.assertIn("::warning::Provider preflight FAILED", proc.stdout)
        self.assertEqual(["opencode/a"], art["models"],
                         "the walk must still run on the pool: %r"
                         % (art["models"],))

    def test_the_preflight_is_silent_when_the_provider_resolves(self):
        proc, art = self.run_step(pool=["opencode/a"], plan="opencode/a:ok")
        self.assertGreen(proc, "provider resolved")
        self.assertNotIn("Provider preflight FAILED", proc.stdout)
        self.assertIn("the free Gemini rungs are live", proc.stdout)
        self.assertEqual(["opencode/a"], art["models"])

    def test_unknown_error_does_not_stop_the_walk(self):
        """The ambiguous string continues the walk as an outage -- it may not
        abort (that is auth's job) and it may not be skipped as not-found."""
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:unknown opencode/b:ok")
        self.assertGreen(proc, "unknown-error continuation")
        self.assertEqual(["opencode/a", "opencode/b"], art["models"])
        log = "\n".join(self.attempt_log())
        self.assertIn("kind=provider_outage", log)
        self.assertNotIn("kind=model_not_found", log)

    def test_a_good_digest_that_mentions_quota_is_still_a_success(self):
        """ORDERING, PINNED. The attempt succeeds and its own article text
        contains the classifier's vocabulary. Classifying before the success
        check would discard it as a quota skip and spend the walk on the next
        model -- a good digest lost to a news story about quotas."""
        proc, art = self.run_step(
            pool=["opencode/chatty", "opencode/live"],
            plan="opencode/chatty:ok_prose opencode/live:ok")
        self.assertGreen(proc, "success with prose")
        self.assertEqual(["opencode/chatty"], art["models"],
                         "the first model produced a valid digest, so the walk "
                         "must stop there: %r" % (art["models"],))
        self.assertIn("Agent wrote valid state/stories.json", proc.stdout)
        self.assertIn("kind=ok", "\n".join(self.attempt_log()))
        self.assertIn("rate limit", proc.stdout,
                      "the prose really was in the log, so the test is not "
                      "vacuous")

    def test_an_all_not_found_walk_finishes_in_seconds(self):
        """The end-to-end half of the bound: seven absent IDs, and the step is
        done in seconds rather than burning seven allowances."""
        pool = ["opencode/dead-%d" % i for i in range(7)]
        plan = " ".join("%s:notfound" % m for m in pool)
        started = time.monotonic()
        proc, art = self.run_step(pool=pool, plan=plan)
        elapsed = time.monotonic() - started
        self.assertEqual(1, proc.returncode)
        self.assertEqual(len(pool), len(art["models"]),
                         "every model was tried: %r" % (art["models"],))
        self.assertLess(elapsed, 120,
                        "seven not-found attempts took %.1fs" % elapsed)


class LadderPurityTests(unittest.TestCase):
    """The seed is config, and it must name only models the catalogue serves."""

    # CHANGED 2026-10-30: the two free Gemini rungs lead, because they are a
    # SEPARATE free bucket that is not the shared pool the opencode rungs share.
    EXPECTED = [
        "gemini/gemini-3.5-flash-lite",
        "gemini/gemini-3.1-flash-lite",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/beam-free",
        "opencode/mimo-v2.6-flash-free",
        "opencode/nemotron-3.5-lightning-free",
        "opencode/glm-5.3-flash-free",
        "opencode/flint-40b-free",
    ]

    def test_the_config_ladder_is_the_live_models(self):
        cfg = ms.load_config(str(REPO_ROOT / "config" / "opencode.json"))
        self.assertEqual(self.EXPECTED, cfg.get("ladder"))
        self.assertEqual(len(set(self.EXPECTED)), len(self.EXPECTED),
                         "a repeated rung is a wasted attempt")

    def test_the_order_is_the_measurement(self):
        """The two models that produced a digest are rungs 1 and 2; the known
        hard failure is the LAST rung and is not deleted."""
        cfg = ms.load_config(str(REPO_ROOT / "config" / "opencode.json"))
        ladder = cfg.get("ladder") or []
        agreed = [m for m in ladder if m.startswith("opencode/")]
        self.assertEqual(["opencode/deepseek-v4.1-flash-free",
                          "opencode/beam-free"], agreed[:2])
        self.assertEqual(["gemini/gemini-3.5-flash-lite",
                          "gemini/gemini-3.1-flash-lite"], ladder[:2],
                         "the free Gemini bucket is tried before the shared "
                         "pool it does not share a wall with")
        self.assertEqual("opencode/flint-40b-free", ladder[-1])
        self.assertEqual(1, ladder.count("opencode/flint-40b-free"))
        self.assertEqual(ladder, ms.ladder_model_ids(),
                         "worst_case_all_fail_sec must read this same ladder")

    def test_no_dead_id_is_configured(self):
        dead = {"opencode/muse-spark-1.3-contributor-free",
                "opencode/muse-spark-1.2-contributor-free",
                "opencode/ling-3.0-flash-fin-free",
                "opencode/mimo-v2.5-free"}
        cfg = ms.load_config(str(REPO_ROOT / "config" / "opencode.json"))
        self.assertFalse(dead & set(cfg.get("ladder") or []),
                         cfg.get("ladder"))

    def test_the_breaking_depth_cap_can_reach_every_rung(self):
        """A cap below the ladder silently makes the tail unreachable: the
        model is configured, reported in the seed, and never tried."""
        import breaking_monitor as bm
        self.assertGreaterEqual(bm.AI_MAX_MODELS, len(self.EXPECTED),
                                "AI_MAX_MODELS (%d) < ladder (%d)"
                                % (bm.AI_MAX_MODELS, len(self.EXPECTED)))
        # The breaking walk CANNOT resolve a provider whose config it was never
        # handed (OPENCODE_CONFIG is unset here, as it is in the canary), so its
        # seed is the reachable subset -- and an unreachable rung would earn a
        # 24h cooldown in the file the digest walk reads. See
        # breaking_monitor._reachable_prefixes.
        self.assertEqual([m for m in self.EXPECTED
                          if m.startswith("opencode/")],
                         list(bm.AI_MODELS))

    def test_one_attempt_may_never_exceed_its_whole_walk(self):
        """--no-work states a 300s walk; the per-attempt cap must be clamped to
        it, or a single attempt can outlast the walk that granted it."""
        sd = tempfile.mkdtemp(prefix="nowork-")
        cfg = os.path.join(sd, "opencode.json")
        info_path = os.path.join(sd, "i.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"provider": "opencode", "ladder": self.EXPECTED[:2]}, fh)
        saved_discover, saved_err, saved_out = ms.discover, sys.stderr, sys.stdout
        try:
            ms.discover = lambda provider, cache_path=None: (
                [], {"source": "none"})
            sys.stderr, sys.stdout = io.StringIO(), io.StringIO()
            code = ms._main(["--select", "--config", cfg,
                             "--health", os.path.join(sd, "h.json"),
                             "--cache", os.path.join(sd, "c.json"),
                             "--info-out", info_path, "--no-work"])
            with open(info_path, encoding="utf-8") as fh:
                info = json.load(fh)
        finally:
            ms.discover, sys.stderr, sys.stdout = saved_discover, saved_err, saved_out
            import shutil
            shutil.rmtree(sd, ignore_errors=True)
        self.assertEqual(0, code)
        self.assertEqual(300, info["total_budget_sec"])
        self.assertLessEqual(
            info["per_attempt_sec"], info["total_budget_sec"],
            "per-attempt %ds exceeds the %ds walk it belongs to"
            % (info["per_attempt_sec"], info["total_budget_sec"]))
        self.assertEqual(300, info["per_attempt_sec"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
