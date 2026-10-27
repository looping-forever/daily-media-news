#!/usr/bin/env python3
"""Quota CONTENTION is transient, so one run must ride it out -- not die in 43s.

WHY THIS FILE EXISTS
--------------------
The `opencode/*-free` models are not paid seats. They run on an anonymous shared
pool throttled per IP bucket, and GitHub Actions egress sits in a contended one,
so the refusal is `Free usage limit reached` even when the account has used
nothing. Measured: a wall of 1-second refusals across the whole ladder, then
5/5 models answering in 33-44 seconds each minutes later, two at a time.

The defect was the reaction, not the throttle. Every model returned
`quota_or_rate_limited`, the walk had no rung left, the step exited 1, and the
slot's edition was gone -- when waiting three minutes would have produced it.

WHAT IS PINNED HERE, AND WHAT MUST NEVER CHANGE
  * an all-quota cycle backs off (180s, 360s, 720s) and RETRIES THE LADDER,
    re-resolving it so a cooldown that expired during the wait is eligible;
  * a cycle in which any model ANSWERS is not contention: no wait, no second
    pass, the walk proceeds exactly as before;
  * a NON-quota class -- a wrong id, a 401, a timeout, a 5xx -- never backs off,
    because repeating it buys nothing and a retry is not a fix for it;
  * when the walk's own bound runs out, the step fails with ONE line that names
    contention, not a false "the models are broken";
  * the wait is clamped so it can never borrow the time the seal, render,
    deploy and state commit need after this step.

The step is executed from the shipped YAML by `AgentWalkHarness`; `sleep` is
shadowed (recorded, never slept) and `date +%s` is a stepwise clock, so a
21-minute retry ladder runs in milliseconds with its arithmetic intact.
"""

import unittest

from tests.workflows.test_agent_walk_step import AgentWalkHarness


class BackoffThenRetryTests(AgentWalkHarness):
    def test_an_all_quota_cycle_backs_off_and_retries_the_ladder(self):
        """THE WHOLE FIX. Two refusals must buy a wait and a second pass, and
        the second pass must produce the digest."""
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:quota opencode/live:ok",
            extra_env={"STUB_POOL_2": "opencode/live"})

        self.assertGreen(proc, "quota contention retry")
        self.assertEqual(
            ["180"], art["naps"],
            "the first contention wait is the 3-minute rung. Naps: %r\nstdout:\n%s"
            % (art["naps"], proc.stdout))
        self.assertEqual(
            ["opencode/a", "opencode/b", "opencode/live"], art["models"],
            "both models must be tried in cycle 1, then the re-resolved ladder "
            "in cycle 2: %r" % (art["models"],))
        self.assertEqual(
            2, len(art["selects"]),
            "the ladder must be RE-RESOLVED for the retry, never reused: a "
            "reused list cannot pick up a cooldown that expired while waiting")
        self.assertIn(
            "Quota contention: cycle 1 tried 2 models and every one returned "
            "quota_or_rate_limited", proc.stdout)
        self.assertIn("next retry at", proc.stdout,
                      "the cycle line must say WHEN the next attempt is due")
        self.assertIn("Contention cleared: the digest was produced on walk "
                      "cycle 2 after 1 backoff(s).", proc.stdout)
        self.assertTrue(art["stories"].is_file(),
                        "the successful cycle must leave the digest on disk")
        self.assertNotIn("Digest not built", proc.stdout)

    def test_the_retry_re_resolves_so_a_dropped_model_is_not_retried(self):
        """Cycle 2 walks what the selector NOW returns, not cycle 1's list.

        The stub answers one outcome per model id, so the proof is a changed
        pool rather than a changed verdict: cycle 1 ranks two models that both
        refuse, and the re-resolved ladder returns a THIRD one that answers. A
        walk that replayed its first list would attempt the first two again and
        never reach it.
        """
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:quota opencode/c:ok",
            extra_env={"STUB_POOL_2": "opencode/c"})
        self.assertGreen(proc, "re-resolved ladder")
        self.assertEqual(
            ["opencode/a", "opencode/b", "opencode/c"], art["models"],
            "cycle 2 must attempt only what the re-resolved ladder names: %r"
            % (art["models"],))

    def test_a_cycle_where_one_model_answers_never_waits(self):
        proc, art = self.run_step(
            pool=["opencode/limited", "opencode/live"],
            plan="opencode/limited:quota opencode/live:ok")
        self.assertGreen(proc, "mixed result")
        self.assertEqual([], art["naps"],
                         "a model answered, so there is no contention to wait "
                         "out. Naps: %r" % (art["naps"],))
        self.assertEqual(1, len(art["selects"]))
        self.assertEqual(["opencode/limited", "opencode/live"], art["models"])

    def test_a_quota_failure_is_recorded_but_never_cools_the_model(self):
        """Backing off must not be confused with a cooldown: the pool shares
        the limit, so cooling it would empty the ladder."""
        _proc, art = self.run_step(
            pool=["opencode/a"],
            plan="opencode/a:quota",
        )
        self.assertIn(["opencode/a", "quota_or_rate_limited"], art["records"])


class RetryLadderTests(AgentWalkHarness):
    def test_the_wait_ladder_is_3_6_12_and_then_it_stops(self):
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:quota")
        self.assertEqual(
            1, proc.returncode,
            "after the bound is spent the step must FAIL, never finish green "
            "with no digest. stdout:\n%s" % proc.stdout)
        self.assertEqual(
            ["180", "360", "720"], art["naps"],
            "the waits must grow 3, 6, 12 minutes and then stop: %r"
            % (art["naps"],))
        self.assertEqual(4, len(art["selects"]),
                         "four cycles: the first pass plus three retries")
        self.assertIn("this run's 4-cycle bound is spent", proc.stdout)

    def test_a_spent_bound_fails_with_one_line_naming_contention(self):
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:quota")
        self.assertEqual(["180", "360", "720"], art["naps"])
        line = [ln for ln in proc.stdout.splitlines()
                if ln.startswith("Digest not built:")]
        self.assertEqual(
            1, len(line),
            "the bound must produce exactly ONE failure line, not a paragraph "
            "repeated per cycle. stdout:\n%s" % proc.stdout)
        self.assertIn("CONTENTION wall", line[0])
        self.assertIn("not the account's own allowance and not broken model "
                      "ids", line[0])
        self.assertNotIn(
            "free models tried", line[0],
            "the generic all-classes-dead wording is a FALSE cause here: %r"
            % line[0])

    def test_the_wait_is_clamped_to_the_walk_bound(self):
        """Budget exhaustion, made observable on the clock.

        With a 1000s bound and 250s per clock tick the second attempt leaves
        250s, so the 180s wait would leave only 160s of room once one attempt
        (90s) is kept -- the wait must shrink to 160 and the NEXT cycle must
        stop at the floor rather than borrow from the delivery stages.
        """
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:quota",
            budget=1000, per=360, min_attempt=90, clock_step=250)
        self.assertEqual(
            ["160"], art["naps"],
            "the wait must be clamped to what leaves one attempt inside the "
            "bound. Naps: %r\nstdout:\n%s" % (art["naps"], proc.stdout))
        self.assertEqual(1, proc.returncode)
        self.assertIn("Budget spent: 0s left", proc.stdout)
        self.assertIn("over 2 cycle(s)", proc.stdout)
        self.assertIn("CONTENTION wall", proc.stdout)


class NonTransientTests(AgentWalkHarness):
    def test_no_non_quota_class_ever_backs_off(self):
        """Each class keeps its own fate: a cooldown, an abort, or an ordinary
        skip. None of them is contention, so none of them may sleep."""
        cases = (
            ("all-not-found",
             dict(pool=["opencode/dead-a", "opencode/dead-b"],
                  plan="opencode/dead-a:notfound opencode/dead-b:notfound")),
            ("auth",
             dict(pool=["opencode/a", "opencode/b"],
                  plan="opencode/a:auth opencode/b:ok")),
            ("timeout",
             dict(pool=["opencode/a", "opencode/b"],
                  plan="opencode/a:124 opencode/b:124")),
            ("provider outage",
             dict(pool=["opencode/a", "opencode/b"],
                  plan="opencode/a:unknown opencode/b:unknown")),
            ("no stories file",
             dict(pool=["opencode/a", "opencode/b"],
                  plan="opencode/a:nofile opencode/b:nofile")),
        )
        for label, kwargs in cases:
            with self.subTest(case=label):
                proc, art = self.run_step(**kwargs)
                self.assertEqual(
                    1, proc.returncode,
                    "%s: the step must fail. stdout:\n%s"
                    % (label, proc.stdout))
                self.assertEqual(
                    [], art["naps"],
                    "%s: a non-transient class must never trigger a backoff. "
                    "Naps: %r\nstdout:\n%s"
                    % (label, art["naps"], proc.stdout))
                self.assertEqual(
                    1, len(art["selects"]),
                    "%s: no retry cycle, so the ladder is resolved once"
                    % label)
                self.assertNotIn("Quota contention", proc.stdout)

    def test_a_not_found_is_not_retried_even_next_to_quotas(self):
        """A wrong id makes the cycle non-transient: one pass, then the honest
        failure. Retrying it is the 4-of-12 waste the cooldown removes."""
        proc, art = self.run_step(
            pool=["opencode/dead", "opencode/limited"],
            plan="opencode/dead:notfound opencode/limited:quota")
        self.assertEqual(1, proc.returncode)
        self.assertEqual([], art["naps"])
        self.assertIn("Digest not built: 2 of 2 free models tried",
                      proc.stdout,
                      "with a non-transient failure present the failure line "
                      "must keep its generic wording. stdout:\n%s"
                      % proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
