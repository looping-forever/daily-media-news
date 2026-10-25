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
REAL_WATCHDOG = "%s no output for 90s; attempt abandoned\n" % ms.WATCHDOG_MARKER


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

    def test_only_three_classes_are_decisive(self):
        self.assertEqual(("model_not_found", "quota_or_rate_limited", "auth"),
                         ms.DECISIVE_CLASSES)
        for kind in ("provider_outage", "slow_or_stalled", "other"):
            self.assertNotIn(kind, ms.DECISIVE_CLASSES)


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
        """Silence, not slowness, is the trigger. A child that keeps printing
        outlives the same window."""
        sink = io.StringIO()
        child = ("import time\n"
                 "for _ in range(6):\n"
                 "    print('token', flush=True); time.sleep(0.4)\n")
        rc = mw.supervise([sys.executable, "-c", child],
                          no_output_sec=1, sink=sink)
        self.assertEqual(0, rc, sink.getvalue())
        self.assertNotIn(ms.WATCHDOG_MARKER, sink.getvalue())

    def test_the_window_is_over_four_times_the_measured_first_token(self):
        """90s vs the measured 12-20s to first token, and the 20.9s control run.
        A number without its measurement is a guess, and this one would kill
        working models."""
        self.assertEqual(90, ms.NO_OUTPUT_WATCHDOG_SEC)
        self.assertGreater(ms.NO_OUTPUT_WATCHDOG_SEC, 4.3 * 20.9)

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


class AllFailBoundTests(unittest.TestCase):
    def test_the_arithmetic_of_a_silent_all_fail_walk(self):
        """6 models x 90s watchdog + 30s overhead = 570s = 9.5 min.

        The 90s is the silence window; the per-attempt cap (360s) is never the
        cost of a dead model any more, which is why the walk can no longer spend
        6 x 360 = 2,160s.
        """
        self.assertEqual(570, ms.worst_case_all_fail_sec(6))
        self.assertEqual(660, ms.worst_case_all_fail_sec(7))
        self.assertLess(ms.worst_case_all_fail_sec(7), 19 * 60)
        self.assertLess(ms.worst_case_all_fail_sec(7), 7 * ms.WALK_PER_ATTEMPT_SEC)

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
                        ms.worst_case_all_fail_sec(6),
                        "a rejection must be cheaper than silence")
        self.assertLess(ms.FAST_FAIL_SEC, ms.NO_OUTPUT_WATCHDOG_SEC)


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

    def test_quota_skips_to_the_next_model(self):
        proc, art = self.run_step(
            pool=["opencode/limited", "opencode/live"],
            plan="opencode/limited:quota opencode/live:ok")
        self.assertGreen(proc, "quota skip")
        self.assertEqual(["opencode/limited", "opencode/live"], art["models"])
        self.assertIn("kind=quota_or_rate_limited", "\n".join(self.attempt_log()))

    def test_auth_stops_the_whole_walk(self):
        """One bad credential, one attempt. Another model cannot fix it."""
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b", "opencode/c"],
            plan="opencode/a:auth opencode/b:ok opencode/c:ok")
        self.assertEqual(1, proc.returncode,
                         "an aborted walk must fail loudly. stdout:\n%s"
                         % proc.stdout)
        self.assertEqual(["opencode/a"], art["models"],
                         "auth must stop the walk: %r" % (art["models"],))
        self.assertIn("stopping the walk", proc.stdout)
        self.assertIn("auth:", proc.stdout)

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

    EXPECTED = [
        "opencode/deepseek-v4.1-flash-free",
        "opencode/mimo-v2.6-flash-free",
        "opencode/flint-40b-free",
        "opencode/nemotron-3.5-lightning-free",
        "opencode/beam-free",
        "opencode/glm-5.3-flash-free",
    ]

    def test_the_config_ladder_is_the_six_live_models(self):
        cfg = ms.load_config(str(REPO_ROOT / "config" / "opencode.json"))
        self.assertEqual(self.EXPECTED, cfg.get("ladder"))
        self.assertEqual(len(set(self.EXPECTED)), len(self.EXPECTED),
                         "a repeated rung is a wasted attempt")

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
        self.assertEqual(self.EXPECTED, list(bm.AI_MODELS))

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
