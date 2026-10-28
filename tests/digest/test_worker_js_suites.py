#!/usr/bin/env python3
"""tests/digest/test_worker_js_suites.py — six shipped JS suites, wired in.

WHAT WAS ACTUALLY BROKEN (measured at 4b31c6a, not assumed)
-----------------------------------------------------------
Three `node --test` suites sit in tests/digest, all passing, all mutation-proved,
and NOTHING EXECUTED THEM. Counted at the committed HEAD, excluding each file's
own header:

  digest_prebuild_cron.test.js              1..15  3 external refs, ALL comments
  digest_dispatch_truthful_notice.test.js   1..17  0 external refs
  digest_ntfy_types_and_dispatch_marker...  1..8   2 external refs, ALL comments

So every green figure ever reported for them was a human running them by hand.
They are not in `tests/ci_local.py`, not in any `run_suite` block, not called from
any Python module. A suite that no job executes is not a gate; it is a comment
with a high line count.

A FOURTH SUITE JOINED THEM, 2026-10-20: `digest_retry_ladder.test.js`, the ladder
arm of the empty-digest retry (moved onto the Worker's own minute grid). It is the
file `deploy/cloudflare-worker.js` names above DIGEST_RETRY_PATH as the test that
fails when the Worker's constants and the workflow's stop disagree.

A FIFTH SUITE JOINED THEM THE SAME DAY: `budget_quota.test.js`, the runtime
dispatch quota — the Worker charges every dispatch it makes against a per-month KV
record and refuses once the month reaches the hard ceiling (2000 allowance - 200
reserve = 1800; the 1400 design target is reported, never enforced). It is the
runtime counterpart to the static `config/actions-budget.json` gate.

A SIXTH SUITE JOINED THEM ON 2026-10-25: `worker_token_expiry.test.js`. The Worker
now records `GitHub-Authentication-Token-Expiration` from the authenticated calls it
was already making, reports it at `?health=1` as `token_expiry`, and warns once a
calendar month when the token is 14 days or fewer from death. It also reports the
repository slug it dispatches to, which is the field whose absence made the
2026-10-25 cutover unprovable from health alone. The day arithmetic is the part
worth executing: a millisecond delta truncates, so a token expiring tomorrow morning
reads as "0 days" — the naive number is asserted IN the suite so it cannot pass by
accident.

THE CONVENTION IS THE ONE THIS REPOSITORY ALREADY USES. See
tests/digest/test_duplicate_detector.py: a `node --test` JS suite reached from a
`python3 -m unittest` entry in the CI inventory, because every entry of
`drift-canary.yml`'s `test-suite` job is a `python3 -m unittest` invocation and
that job has no node setup step. Same reason, same shape, same demand that a
MISSING node be LOUD rather than skipped.

WHY drift-canary.yml WAS NOT TOUCHED
-------------------------------------
node IS available to the `test-suite` job, and this repository proves it rather
than assuming it. The `digest` entry there is `unittest discover -s tests/digest`,
which already reaches `test_duplicate_detector.py`'s `WorkerSideReadbackTests` —
and that class calls `self.fail()` when `shutil.which("node")` is empty. A job that
reaches a loud-fail-on-missing-node test and is green has node. The `ubuntu-latest`
GitHub-hosted image ships Node.js on PATH, which is why the workflow has never
needed `actions/setup-node` and why adding one would have been scope for nothing.

Consequently this file registers the suites BY DISCOVERY: the `digest` entry is a
`-s tests/digest` discovery in BOTH runners, so placing this module in that
directory is the whole registration. No inventory edit, no workflow edit, and no
second copy of a suite list that can drift from the workflow again — the exact
failure this repository's ci_local.py docstring names twice.

THREE ASSERTIONS, AND WHY ONE COUNT IS NOT ENOUGH
--------------------------------------------------
Each suite is pinned three ways, because each catches a different way a gate goes
quiet. The failures are not the same failure:

  1. `returncode == 0`      an assertion in the JS actually broke. Proven: with
                            one assertion broken the digest suite runs 215 tests
                            and reports FAILED (failures=3), one per suite.
  2. the TAP PLAN is `==`  the suite shrank or died part-way. Measured, not
                            assumed: `>=` would let a suite lose a test and stay
                            green, and the plan is the only machine-readable
                            statement of how many tests ran.
  3. `# skipped` is 0      a test marked `.skip()`. MEASURED, and this one is the
                            reason assertion 2 alone is not enough: node keeps a
                            SKIPPED test inside the plan (`1..15`, `# skipped 1`)
                            and still exits 0. So marking a mandated test
                            `.skip` would leave a green run in which that
                            behaviour stopped being checked, with the name still
                            in the file for assertion 4 to find.

Assertion 4, per class, is structural: it reads the committed test names and
asserts each mandated behaviour is still named, so a suite cannot lose coverage
quietly. Between them the plan cannot be satisfied by swapping one test for
another, and the names cannot be satisfied by never running the suite at all.

The pinned counts are deliberate numbers, exactly like the suite list in
ci_local.py: if a JS suite legitimately gains a test, this moves on purpose and
the diff says so.

WHAT THESE SUITES TEST IS NOT TOUCHED. All three files are shipped and
mutation-proved; this module only reaches them.
"""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)

#: node's own TAP plan line: the only machine-readable statement of how many tests
#: actually ran. A wrapper that passes because the runner printed nothing is
#: decoration, so its absence is a failure here, never an empty pass.
TAP_PLAN_RE = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)

#: node's TAP summary line for skipped tests. Separate from the plan because a
#: skipped test is IN the plan -- see the module docstring.
TAP_SKIPPED_RE = re.compile(r"^#\s+skipped\s+(\d+)\s*$", re.MULTILINE)


class _WorkerJsSuiteMixin:
    """Reach one shipped JS suite from this inventory and hold it to its plan.

    The mechanism is identical for all three, so it lives here once: node must
    exist, the suite must exit 0, node's own TAP plan must prove it ran the pinned
    number of tests, and nothing may have been quietly skipped. Subclasses differ
    only in WHICH suite, and in what a red there would mean.

    Deliberately NOT a `unittest.TestCase`: a TestCase subclass with no suite of
    its own is still collected and run, and this base carries no JS file. It is a
    mixin; the three concrete classes below pair it with TestCase.
    """

    #: Subclasses set these.
    JS = None                 # Path to the JS suite
    TAP_PLAN = None           # node's pinned plan for that suite
    WORKER_OVERRIDE = None    # the JS suite's own worker-override env var
    WHAT_IT_GUARDS = ""       # what goes wrong if this suite is ever red
    WHAT_NODE_MISSING = ""    # why a missing node fails rather than skips
    REQUIRED_BEHAVIOURS = ()  # test names that must still exist in the file

    def _node(self):
        node = shutil.which("node")
        if not node:
            self.fail(
                "node is required to execute %s and was not found on PATH. This "
                "FAILS rather than skips on purpose: %s Install node, or put it "
                "on PATH -- do not delete the test."
                % (self.JS.name, self.WHAT_NODE_MISSING))
        return node

    def test_the_suite_passes_against_the_committed_worker(self):
        node = self._node()
        self.assertTrue(self.JS.is_file(),
                        "%s is missing, so the guard it carries would silently "
                        "vanish from the digest suite" % self.JS)
        proc = subprocess.run(
            # TAP, explicitly, because the plan line ("1..N") is the only
            # machine-readable statement of how many tests ran.
            [node, "--test", "--test-reporter=tap", str(self.JS)],
            cwd=str(_ROOT), capture_output=True, text=True, errors="replace",
            timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(
            0, proc.returncode,
            "%s FAILED against %s.\n%s\n"
            "---- node output ----\n%s\n------------------"
            % (self.JS.name,
               os.environ.get(self.WORKER_OVERRIDE)
               or "the committed deploy/cloudflare-worker.js",
               self.WHAT_IT_GUARDS, output))
        plan = TAP_PLAN_RE.search(proc.stdout or "")
        self.assertIsNotNone(
            plan,
            "node's own TAP plan line was absent, so this module cannot confirm "
            "the suite RAN anything. A wrapper that passes because the runner "
            "printed nothing is decoration.\n---- node output ----\n%s" % output)
        self.assertEqual(
            self.TAP_PLAN, int(plan.group(1)),
            "node's TAP plan says %s ran %s test(s); this repository pins %d. A "
            "lower number means tests were deleted or the suite died part-way; a "
            "higher one means it was changed deliberately and this pin is now "
            "wrong. Either way, do not adjust this number to match the output "
            "without recording why.\n%s"
            % (self.JS.name, plan.group(1), self.TAP_PLAN, output))
        skipped = TAP_SKIPPED_RE.search(proc.stdout or "")
        self.assertEqual(
            0, int(skipped.group(1)) if skipped else 0,
            "%s skipped %s test(s). node keeps a skipped test INSIDE the TAP plan "
            "and still exits 0, so the plan assertion above cannot see it: a "
            "mandated behaviour can stop being checked while the run stays green "
            "and the name stays in the file. A skip in a mutation-proved suite is "
            "a deliberate act and must be recorded in the report.\n%s"
            % (self.JS.name, skipped.group(1) if skipped else "?", output))

    def test_the_suite_still_names_every_mandated_behaviour(self):
        """A suite that can lose a test without noticing is not a suite.

        Structural on purpose: it reads the committed test names and asserts each
        mandated behaviour is still named. It does not decide whether they pass
        -- that is the test above.
        """
        self.assertTrue(self.JS.is_file(), "%s is missing" % self.JS)
        text = self.JS.read_text(encoding="utf-8")
        missing = [b for b in self.REQUIRED_BEHAVIOURS if b not in text]
        self.assertEqual(
            [], missing,
            "%s no longer names %s. Each is a mutation-proved behaviour of the "
            "code this suite loads; deleting one must be a deliberate act "
            "recorded in the report, never a silent removal."
            % (self.JS.name, missing))


class PrebuildCronWorkerSuite(_WorkerJsSuiteMixin, unittest.TestCase):
    """The punctual prebuild trigger, executed against the committed Worker.

    THE LOADER IS THE SAME ONE the sibling suites use: committed Worker source,
    `export default {` -> `const WORKER = {`, re-exported through a module shim,
    loaded from a unique temp path. If a name below stops existing in the Worker
    the loader throws rather than quietly testing nothing.
    """

    JS = Path(_TESTS) / "digest" / "digest_prebuild_cron.test.js"
    # RECORDED CHANGE, 2026-10-18: 15 -> 17. The two tests named in
    # REQUIRED_BEHAVIOURS as "a prebuild dispatch dates itself from the SCHEDULED
    # instant..." and "a prebuild dispatch with NO scheduled instant OMITS
    # slot_date..." were ADDED, because the prebuild dispatch carried no
    # `slot_date` at all and derived the slot's day from the clock at its own
    # end. The pin is raised because the suite grew deliberately, not because the
    # output was copied back into this file.
    TAP_PLAN = 17
    WORKER_OVERRIDE = "DIGEST_PREBUILD_WORKER"
    WHAT_IT_GUARDS = (
        "the 06:40 / 18:40 NPT prebuild the Worker's every-minute dispatcher "
        "asks for, forty minutes before the 07:20 / 19:20 NPT slots. Every test "
        "here first asserts the prebuild really dispatched, because a dispatch "
        "test whose fixture omitted GH_TOKEN would count zero dispatches and "
        "then prove vacuously that zero dispatches harm the slot.")
    WHAT_NODE_MISSING = (
        "a skipped prebuild guard is a green CI run with no prebuild guard at "
        "all, and this suite has been in the repository, green and "
        "mutation-proved, while no job ever ran it.")

    # RECORDED CHANGE, 2026-10-20 (item A): the third entry below was renamed from
    # "the 00:55 tick dispatches digest-prebuild.yml once, with digest_time=morning".
    # The BEHAVIOUR changed ON PURPOSE -- the prebuild arm now dials daily-news.yml
    # with phase=prebuild, and the build hands its OWN run id to the seal as its
    # last step, so the intermediary that dispatched the build and then watched it
    # for up to 45 minutes is gone. The name is updated to the test that carries it
    # now; nothing was dropped from this list and TAP_PLAN is unchanged.
    REQUIRED_BEHAVIOURS = (
        "the prebuild instant is exactly PREBUILD_LEAD_MINUTES before its slot",
        "the dispatcher plans the prebuild at 00:55/12:55 and the slot at 01:35/13:35",
        "the 00:55 tick dispatches the prebuild once: daily-news.yml, phase=prebuild",
        "the 12:55 tick dispatches the evening slot",
        "the prebuild arm reuses the ONE dispatch implementation and the ONE token",
        "dispatchPrebuild with NO GH_TOKEN refuses loudly and writes nothing",
        "the prebuild arm records NO delivery state; only the runtime quota's own keys",
        "a REJECTED prebuild dispatch throws nothing and still leaves the slot intact",
        "a missed prebuild leaves the slot to the EXISTING fallback ladder",
        "a sealed artifact still turns the slot into a lookup, not a build",
        "the prebuild arm never pushes, so it cannot double-fire the slot",
        "an UNPLANNED tick still runs the sweeps and never the prebuild",
        "the Worker's scheduled() entry point routes BOTH prebuild crons",
        "a prebuild dispatch dates itself from the SCHEDULED instant, never the clock",
        "a prebuild dispatch with NO scheduled instant OMITS slot_date, guessing nothing",
        "THE REPO CANNOT PRODUCE MORE THAN ONE PREBUILD FIRING PER SLOT",
        "digestScheduledInstant still anchors punctuality on the PRIMARY slot",
    )


class DispatchTruthfulNoticeWorkerSuite(_WorkerJsSuiteMixin, unittest.TestCase):
    """The fallback-ladder notice defects, executed.

    DEFECT 1 the false token alarm, DEFECT 2 last-good pushed over a correct
    delivery, DEFECT 3 no dispatch telemetry. A notice that names a fault which
    did not happen is worse than no notice, because it sends someone to fix
    something that is not broken.

    DEFECT 4 and DEFECT 5 were added on 2026-10-28, and the pin below moved from
    17 to 21 with them: four new tests, no existing test removed or renamed.
    DEFECT 4 is the same class of lie one step later in time -- run 40615951953
    was dispatched at 01:35:27Z, FAILED, and the next fire wrote "NO build was
    dispatched" over a slot whose build had been started and had died, so the
    owner was told to stop waiting for something that had already been tried.
    DEFECT 5 is the same lie in ?health=1: it reported
    `last_good_shown: '2026-10-18'` beside `fallback_shown: false` and
    `edition: failed`, which cannot all be true at once -- a date is only a
    last-good date if that digest is what the owner is actually reading.

    THE DEFECT 5 CONTRACT WAS REVERSED ON THE OWNER'S INSTRUCTION, and the pin
    moved from 21 to 25. The old repair DELETED the recorded date, which
    destroyed the only field that could explain the row. Now a recorded value is
    always reported, `edition` comes from what the row stored rather than from
    its `kind`, and a disagreement between the two is named in
    `edition_disagreement`. What stays false is the claim about the phone,
    `fallback_shown`, and `fresh_edition` cannot be true for a row that
    contradicts itself. The fifth test is the sweep: a push instant recorded
    only as `first_ntfy_sent_at` is reported, not nulled, because `reason`
    already reads it and one response may not contradict itself.
    """

    JS = Path(_TESTS) / "digest" / "digest_dispatch_truthful_notice.test.js"
    TAP_PLAN = 25
    WORKER_OVERRIDE = "DIGEST_NOTICE_WORKER"
    WHAT_IT_GUARDS = (
        "the honesty of the fallback ladder's notice. On 2026-10-13 this Worker "
        "told the owner his GH_TOKEN had died when the real cause was the "
        "once-per-slot guard standing down correctly, and in the same cycle "
        "pushed last-good over a fresh digest the owner had already read. On "
        "2026-10-28 it denied dispatching a build that had in fact been "
        "dispatched and had failed.")
    WHAT_NODE_MISSING = (
        "dispatch truthfulness is the one thing the owner must be able to trust "
        "without checking the logs, and a skipped suite is a green run proving "
        "nothing about it.")

    REQUIRED_BEHAVIOURS = (
        "DEFECT 1: a benign stand-down produces a NON-failure notice, not a token alarm",
        "DEFECT 1: a 401 produces a token-SPECIFIC message, distinct from the stand-down",
        "DEFECT 1: a 403 names PERMISSION, so the owner knows which half to fix",
        "DEFECT 1: a 500 says NOT-A-TOKEN, and a transport error says NOT-A-TOKEN",
        "DEFECT 1: a transport failure is a NETWORK fault, and says so",
        "DEFECT 1: escalateToActions reports a CAUSE for all five paths, never a bare false",
        "DEFECT 2: a delivered slot gets NO last-good push (2026-10-13, exactly)",
        "DEFECT 2 (VACUITY): the SAME harness with NO delivered run DOES push last-good",
        "DEFECT 2: an UNREADABLE state stands down LOUDLY and is not called delivered",
        "DEFECT 2: a marker with no token to read it with is UNKNOWN, not delivered",
        "DEFECT 2: no dispatch marker means no check, no call, and the ladder runs",
        "DEFECT 2: classifyDeliveryEvidence is a total, fail-closed table",
        "DEFECT 2: the stand-down never blanks the dispatch marker",
        "DEFECT 3: a refused dispatch is persisted with its status and cause",
        "DEFECT 3: the persisted record contains NO token and NO unredacted body",
        "DEFECT 3: an ACCEPTED dispatch writes nothing, so the steady KV budget holds",
        "the notice grew, and both ceilings still hold",
        "DEFECT 4: the dispatch verdict has FOUR states, not two",
        "DEFECT 4: a dispatched build that FAILED is named, not denied",
        "DEFECT 4: the stored sentence in KV is repaired at read time, with no migration",
        "DEFECT 5: a recorded last_good is REPORTED, and the contradiction is named",
        "a legacy row whose stored edition says failed is NOT rendered fresh",
        "a recorded last_good_shown is surfaced on every arm",
        "a recorded push instant is never rendered as null",
        "the reason agrees with the state it sits beside",
    )


class NtfyTypesAndDispatchMarkerWorkerSuite(_WorkerJsSuiteMixin, unittest.TestCase):
    """The ntfy type guards and the once-per-slot dispatch marker, executed.

    DEFECT 1 the digest push sent ntfy types it does not accept. DEFECT 2 three
    fires of one slot produced more than one dispatch, because a stand-down fire
    could ERASE the marker. Neither is reachable from a suite that fires the
    ladder once per test against a ledger that starts empty.
    """

    JS = Path(_TESTS) / "digest" / "digest_ntfy_types_and_dispatch_marker.test.js"
    TAP_PLAN = 8
    WORKER_OVERRIDE = "DIGEST_NTFY_TYPES_WORKER"
    WHAT_IT_GUARDS = (
        "the two properties the digest push is only correct by accident of: that "
        "it sends ntfy types ntfy accepts, and that three fires of one slot "
        "produce EXACTLY ONE dispatch. The second is unfalsifiable from any suite "
        "that fires the ladder once, so it needs the persistent ledger.")
    WHAT_NODE_MISSING = (
        "the once-per-slot dispatch marker is the guard against the owner being "
        "pushed the same digest three times, and it was green and unexecuted "
        "while that defect was live.")

    REQUIRED_BEHAVIOURS = (
        "DEFECT 1: the REAL digest push sends ntfy the types it accepts",
        "DEFECT 1: the FALLBACK ladder push (last_good rung) sends acceptable types",
        "DEFECT 1: the UNAVAILABLE rung push sends acceptable types",
        "DEFECT 1: digestPayload cannot emit a string priority or string tags, ever",
        "DEFECT 2: three fires of one slot produce EXACTLY ONE dispatch",
        "DEFECT 2: actions_dispatch_at SURVIVES a stand-down fire",
        "DEFECT 2: a stand-down still suppresses a NEW dispatch (the guard stands)",
        "DEFECT 2: escalateToActions reports the EXISTING marker when it stands down",
    )


class RetryLadderWorkerSuite(_WorkerJsSuiteMixin, unittest.TestCase):
    """The empty-digest retry ladder, executed against the committed Worker.

    The ladder moved off digest-watchdog.yml's 72-a-day GitHub cron onto the
    Worker's own minute grid on 2026-10-20. This suite drives one ladder tick from
    its own clock and proves the DUE test, the KV debounce keyed on the attempt,
    and that "I could not read the retry file" is never read as "a retry is due".
    """

    JS = Path(_TESTS) / "digest" / "digest_retry_ladder.test.js"
    TAP_PLAN = 17
    WORKER_OVERRIDE = "DIGEST_RETRY_LADDER_WORKER"
    WHAT_IT_GUARDS = (
        "the empty-digest retry ladder the Worker now drives: the due test, the "
        "KV debounce keyed on date:slot:attempt, and the rule that an unreadable "
        "retry file dispatches nothing. A GitHub outage read as a pending retry "
        "is a retry storm, and the opposite reads as no retry at all.")
    WHAT_NODE_MISSING = (
        "the ladder's due rule and its debounce are the two things standing "
        "between a 15-minute tick and a duplicate retry every quarter hour; a "
        "skipped suite is a green run proving nothing about either.")

    REQUIRED_BEHAVIOURS = (
        "the ladder minutes are DERIVED from phase and every, not a literal",
        "the ladder plans a job at :17/:32/:47 and nowhere else",
        "the ladder only ADDS work at :47; :17 and :32 were already busy",
        "legacyJobForCron maps the ladder cron to the ladder job",
        "not due yet: no dispatch and NO KV write",
        "due on the first attempt: dispatches ladder_only=true AND writes the debounce key",
        "already dialled: the debounce suppresses a SECOND dispatch",
        "the key separates rungs: attempt 1 dials, attempt 2 dials again",
        "an exhausted ladder dispatches nothing",
        "a rejected dispatch writes NO debounce key, so the rung is retried not lost",
        "a 404 retry file dispatches nothing",
        "a transport failure reading the file dispatches nothing",
        "a missing GH_TOKEN reads as no ladder, not as due",
        "nptStampFromInstant is the same clock the retry file is written in",
        "scheduled() routes the ladder cron to runDigestLadder",
    )


class BudgetQuotaWorkerSuite(_WorkerJsSuiteMixin, unittest.TestCase):
    """The runtime dispatch quota, executed against the committed Worker.

    config/actions-budget.json is a STATIC gate; it cannot stop a month the tree
    never predicted. This suite proves the runtime counterpart: the two numbers
    are distinct, the refusal order is DIGEST-LAST, the alert fires once per month
    per reason, and the KV race errs toward allowing a dispatch.

    Since 2026-10-25 it ALSO proves the ENFORCEMENT SWITCH: the repository is
    public, so the quota ships disarmed and `budget_refused` must never fire --
    while the charge, the month record and the `?health=1` figures are all still
    kept, and the armed behaviour is still exercised so re-arming is one value.
    """

    JS = Path(_TESTS) / "digest" / "budget_quota.test.js"
    # 12 -> 19 on 2026-10-25. SEVEN tests were ADDED, none removed or weakened:
    # the switch is declared off with a stated reason and a 6h probe; enforcement
    # off refuses nothing even far past the ceiling while still charging the
    # month; enforcement on still refuses in the established order with the digest
    # and watchdog last; the switch is the only thing that changes the decision; a
    # public repository force-disarms an armed flag via one cached probe; an
    # unanswerable probe is NOT read as public; and a confirmed private answer
    # keeps an armed quota armed. The pin is raised deliberately, not to match the
    # output.
    TAP_PLAN = 19
    WORKER_OVERRIDE = "BUDGET_QUOTA_WORKER"
    WHAT_IT_GUARDS = (
        "the runtime quota: the hard ceiling (2000 allowance - 200 reserve = "
        "1800) and the 1400 design target as two distinct numbers; the refusal "
        "order is digest-last, so at the margin a digest is allowed and a prebuild "
        "refused; the budget_refused alert fires once per month per reason; the "
        "eventually-consistent KV race under-counts, which errs toward ALLOWING a "
        "dispatch rather than refusing a digest that would have fit; and the "
        "ENFORCEMENT SWITCH, which ships OFF (the repository is public, so minutes "
        "are free) while keeping the charge, the month record and the health "
        "figures -- so re-arming the refusal is one value and not a rewrite.")
    WHAT_NODE_MISSING = (
        "the quota is the one thing that makes 'the account is never pushed over' "
        "true rather than probable, and a skipped suite is a green run proving "
        "nothing about the number that enforces it.")

    REQUIRED_BEHAVIOURS = (
        "the two numbers are distinct and the per-target table is the documented one",
        "the month key is budget:YYYY-MM, taken from the instant",
        "refusal fires AT the ceiling and NOT before",
        "the refusal ORDER is digest-last, proved by a case where only a digest fits",
        "at or over the ceiling EVERYTHING is refused, digest included",
        "the freeze flag short-circuits discretionary dispatches first",
        "charges accumulate in the KV month record, persisted BEFORE the dispatch",
        "the alert fires ONCE per month per reason, not on every tick",
        "a failed alert is retried, not burned (the marker is set only on success)",
        "the race errs toward ALLOWING: a lost charge never turns into a refusal",
        "?health budget fields expose the freeze state and the month's charge",
        "a missing KV binding never refuses a dispatch",
        "the enforcement switch is declared OFF, with a reason and a 6h probe",
        "ENFORCEMENT OFF: nothing is refused, even far past the ceiling",
        "ENFORCEMENT ON: the refusal fires in the established order, digest last",
        "the switch is the ONLY thing that changes the decision",
        "the visibility probe force-disables a FLAG-ON quota on a public repo",
        "a probe that cannot answer is NOT read as public: the flag still decides",
        "a CONFIRMED private answer keeps an armed quota armed",
    )


class TokenExpiryWorkerSuite(_WorkerJsSuiteMixin, unittest.TestCase):
    """The GitHub token's own expiry, executed against the committed Worker.

    GitHub returns `GitHub-Authentication-Token-Expiration` on any authenticated
    response made with an expiring fine-grained PAT, and omits it for a token that
    never expires. The Worker already makes those calls, so the value arrives free;
    everything AFTER that is judgement, and each judgement is a way to be wrong:

      * the day arithmetic must compare UTC DATES. A millisecond delta truncates, so
        a token expiring at 09:33 tomorrow read at 23:00 tonight floors to 0 and the
        warning says "expires today" when the truth is tomorrow;
      * `unknown` must never be spelled 0, because 0 means "expiring now";
      * an unknown value must consume NO dedupe key, or the one real warning is
        silenced by a month in which nothing was observed;
      * the alert must fire once a calendar month, not once a tick.

    It also pins the `repo` field, so the dispatch target is provable from health
    alone -- the exact gap that made the 2026-10-25 cutover unverifiable.
    """

    JS = Path(_TESTS) / "digest" / "worker_token_expiry.test.js"
    # FIRST PIN, 2026-10-25: 11. The suite ships with eleven tests, one per behaviour
    # listed in REQUIRED_BEHAVIOURS. There is no earlier value to raise: the suite is
    # new, and the number is recorded here rather than copied back from a green run.
    TAP_PLAN = 11
    WORKER_OVERRIDE = "TOKEN_EXPIRY_WORKER"
    WHAT_IT_GUARDS = (
        "the token-expiry warning: UTC-date day arithmetic across the midnight "
        "boundary, `unknown` as a literal state with null figures and never 0 or "
        "NaN, an observation that survives an odd response, one alert per calendar "
        "month at or under 14 days, no alert above it, no dedupe key burned by an "
        "unknown value, and the repo slug that makes the dispatch target provable "
        "from ?health=1.")
    WHAT_NODE_MISSING = (
        "the expiry warning is the only thing that tells the owner the PAT is about "
        "to die before every dispatch starts returning 403, and the day arithmetic "
        "is the part no static check can see; a skipped suite is a green run proving "
        "nothing about either.")

    REQUIRED_BEHAVIOURS = (
        "days remaining compares UTC DATES: expiring tomorrow morning is 1, not 0",
        "days remaining is exact and signed, and never NaN",
        "an unparseable or nonexistent expiry is null, never NaN",
        "with nothing observed, health says unknown with null figures",
        "a real header is cached RAW, and health then reports days remaining",
        "an absent or unparseable header leaves a known record untouched",
        "the alert fires exactly once at the threshold; a second tick does not re-fire",
        "no alert above the threshold, and no marker is burned",
        "an unobserved token consumes NO dedupe key, so a later real value still alerts",
        "?health=1 carries the repo slug and the token_expiry block",
        "every GitHub repo URL in the Worker carries the slug health reports",
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)