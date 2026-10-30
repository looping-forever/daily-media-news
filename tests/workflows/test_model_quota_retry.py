#!/usr/bin/env python3
"""Quota CONTENTION is transient -- but the walk no longer rides it out in-step.

WHY THIS FILE CHANGED (read this before "restoring" the backoff)
---------------------------------------------------------------
The `opencode/*-free` models are not paid seats. They run on an anonymous shared
pool throttled per IP bucket, and GitHub Actions egress sits in a contended one,
so the refusal is `Free usage limit reached` even when the account has used
nothing. Measured: a wall of 1-second refusals across the whole ladder, then 5/5
models answering in 33-44 seconds each minutes later.

That measurement produced the ORIGINAL contract of this file: an all-quota cycle
slept 180s, 360s, then 720s and walked the ladder again, up to four cycles. It
worked, and it cost more than it was worth:

  * the refusal is PRE-GENERATION -- 312ms after `message=stream`, with
    tokens.input=0 -- and six different ids were refused inside 16 seconds, so
    the extra rungs learned nothing the first one had not;
  * `--all` returns the whole ranked pool, so one cycle is every rung and four
    cycles is a 48-attempt ceiling per slot;
  * MEASURED BURN, 2026-10-27: 112 model attempts in one day on this workflow,
    181 in the week (62% of the repository's total), 33 of the week's 47 quota
    refusals.

So the ride-out moved OUT of the step. A refusal now records ONE shared-pool row
(`state/model_cooldown.json`, with the provider's own Retry-After when it gave
one) and ends the walk; the refire slot (+5/+10/+15/+20 min) retries the edition
with a fresh 1800s budget instead of spending this one on sleep.

WHAT IS PINNED HERE, AND WHAT MUST NEVER CHANGE
  * a quota refusal stops the ladder after ONE attempt: no nap, no second cycle,
    and the pool row is written so the next run starts from the fact;
  * a cycle in which a model ANSWERS is not contention: no wait, no pool row;
  * a NON-quota class -- a wrong id, a 401, a timeout, a 5xx -- never writes a
    pool row, because the pool is not what refused;
  * the step fails with ONE line that names the shared-pool wall rather than a
    false "the models are broken" or a false "every model was quota";
  * the walk bound itself is unchanged: nothing here borrows the time the seal,
    render, deploy and state commit need after this step.

The step is executed from the shipped YAML by `AgentWalkHarness`; `sleep` is
shadowed (recorded, never slept) and `date +%s` is a stepwise clock, so the
arithmetic stays observable without waiting.
"""

import unittest

from tests.workflows.test_agent_walk_step import AgentWalkHarness


class StopOnPoolWallTests(AgentWalkHarness):
    def test_a_quota_refusal_ends_the_walk_after_one_attempt(self):
        """THE WHOLE REDUCTION. One refusal, one recorded fact, no ladder walk.

        `opencode/live` answers, so a walk that continued would have produced a
        digest. It must not get the chance: the pool is the thing that refused,
        and the next model asks the same bucket.
        """
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:quota opencode/live:ok",
            extra_env={"STUB_POOL_2": "opencode/live"})

        self.assertEqual(
            1, proc.returncode,
            "no digest was produced, so the step must fail rather than finish "
            "green. stdout:\n%s" % proc.stdout)
        self.assertEqual(
            ["opencode/a"], art["models"],
            "the walk must stop at the FIRST shared-pool refusal: %r"
            % (art["models"],))
        self.assertEqual([], art["naps"],
                         "a pool wall must not be slept off inside the step: %r"
                         % (art["naps"],))
        self.assertEqual(1, len(art["selects"]),
                         "one cycle. A second resolution is the 48-attempt "
                         "ceiling coming back")
        calls = (self.tmp / "record_calls.txt").read_text(encoding="utf-8")
        self.assertIn("--pool-refused", calls,
                      "the refusal must become a shared cooldown row, or the "
                      "next run pays for the same wall again")

    def test_the_failure_line_names_the_wall_and_not_broken_models(self):
        proc, _art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:quota opencode/b:quota")
        lines = [ln for ln in proc.stdout.splitlines()
                 if ln.startswith("Digest not built:")]
        self.assertEqual(
            1, len(lines),
            "the walk must produce exactly ONE failure line. stdout:\n%s"
            % proc.stdout)
        # REWORDED 2026-10-30 with the Gemini rungs: "the shared anonymous pool
        # this egress sits behind" stopped being true the moment a SECOND
        # provider joined the ladder, so the line now names a quota wall and a
        # spent free allowance. What must survive is the cause and the retry.
        self.assertIn("stopped on a quota wall", lines[0])
        self.assertIn("refire slot retries this edition", lines[0])
        self.assertIn("not a broken model id", lines[0])

    def test_a_successful_run_writes_no_pool_row(self):
        """The cooldown may only ever be earned by a refusal.

        A pool row written on a good run would wall the NEXT run off a pool that
        had just served a digest, which is the same class of bug as the old
        bypass: a signal used for something it does not mean.
        """
        proc, art = self.run_step(
            pool=["opencode/limited", "opencode/live"],
            plan="opencode/limited:ok")
        self.assertGreen(proc, "mixed result")
        self.assertEqual([], art["naps"])
        self.assertEqual(1, len(art["selects"]))
        self.assertEqual(["opencode/limited"], art["models"])
        calls = (self.tmp / "record_calls.txt").read_text(encoding="utf-8")
        self.assertNotIn(
            "--pool-refused", calls,
            "a model answered, so the pool did not refuse this run: writing a "
            "cooldown here would wall the next run off a working pool")

    def test_a_quota_failure_is_recorded_but_never_cools_the_model(self):
        """A pool row is not a per-model cooldown: the pool shares the limit, so
        cooling the id would empty the ladder for a fact about the bucket."""
        _proc, art = self.run_step(
            pool=["opencode/a"],
            plan="opencode/a:quota",
        )
        self.assertIn(["opencode/a", "quota_or_rate_limited"], art["records"])


class NonTransientTests(AgentWalkHarness):
    def test_no_non_quota_class_ever_writes_a_pool_row(self):
        """Each class keeps its own fate: a cooldown, an abort, or an ordinary
        skip. None of them is the pool refusing, so none of them may wall it.

        THE AUTH ARM CHANGED ON 2026-10-27, from measurement. One `auth` no
        longer ends the walk (a single model's refusal is not a property of the
        shared credential -- see tests/workflows/test_agent_walk_step.py
        AuthIsPerModelTests), so the arm that must fail here is TWO of them.
        """
        cases = (
            ("all-not-found",
             dict(pool=["opencode/dead-a", "opencode/dead-b"],
                  plan="opencode/dead-a:notfound opencode/dead-b:notfound")),
            ("auth",
             dict(pool=["opencode/a", "opencode/b"],
                  plan="opencode/a:auth opencode/b:auth")),
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
                self.assertNotIn("SHARED POOL", proc.stdout,
                                 "%s: the pool did not refuse, so the failure "
                                 "line must not blame it" % label)
                calls = (self.tmp / "record_calls.txt").read_text(
                    encoding="utf-8")
                self.assertNotIn("--pool-refused", calls,
                                 "%s: no pool row for a non-pool failure"
                                 % label)

    def test_a_not_found_is_not_reported_as_contention(self):
        """A wrong id next to a quota wall: the line names the wall (that is
        what stopped the walk) and still reports the generic tried-count, so the
        not-found is not buried under a false cause."""
        proc, art = self.run_step(
            pool=["opencode/dead", "opencode/limited"],
            plan="opencode/dead:notfound opencode/limited:quota")
        self.assertEqual(1, proc.returncode)
        self.assertEqual([], art["naps"])
        self.assertIn("2 attempt(s) over 1 cycle(s)", proc.stdout,
                      "the line must count what was actually tried. stdout:\n%s"
                      % proc.stdout)
        self.assertIn("stopped on a quota wall", proc.stdout)
        self.assertNotIn("Every rung answered", proc.stdout,
                         "one of the two was a not-found, so 'every rung' "
                         "would be false. stdout:\n%s" % proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
