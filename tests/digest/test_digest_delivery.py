#!/usr/bin/env python3
"""tests/digest/test_digest_delivery.py — CI wrapper for the digest delivery path.

The behaviour under test is JavaScript (deploy/cloudflare-worker.js), and the
tests that check it are JavaScript too: tests/digest/digest_delivery.test.js
runs under `node --test` against the Worker's own functions with a fake KV, a
fake fetch and a fake clock.

WHY A WRAPPER EXISTS AT ALL. The runner that enforces this repository is
`drift-canary.yml`'s `test-suite` job, every entry of which is a
`python3 -m unittest` invocation, and that job has no node setup step. This
module makes the JS suite reachable from that inventory without a second
mechanism, and — the part that actually matters — it makes a MISSING node
LOUD.

A delivery guard that silently skips is worse than no delivery guard: CI goes
green for a suite that never executed, and the feature whose entire job is to
stop a double push is the one nobody is looking at. So `node_executable()`
fails with an explicit message rather than skipping, matching
tests/breaking/test_worker_heartbeat.py, which already fails loudly for exactly
this reason.

WHY A WRAPPER MUST ALSO ASSERT THE TAP PLAN LINE. `node --test` exiting 0 with
no output is indistinguishable, from here, from a suite that ran zero tests —
which is what a deleted or misnamed file looks like. The plan line (`1..N`) is
node's own statement of how many tests ran, and it is the only number in the
file that cannot go stale. A wrapper that passes because the runner printed
nothing is decoration.

WHAT IS NOT HERE, ON PURPOSE: no Python re-implementation of any delivery
logic. A proxy is what let a real divergence survive a "parity" test before.

THE GUARANTEE THIS SUITE PROTECTS is at-least-once with a possible duplicate on
the recovery path only. It is not exactly-once and no REQUIRED_BEHAVIOURS entry
below says so; there is no transaction spanning "ntfy has the message" and "KV
has the record".

RUN IT YOURSELF (both forms, both real):
    node --test tests/digest/digest_delivery.test.js
    py -3 -m unittest tests.digest.test_digest_delivery -v

REPRODUCING THE RED: set DIGEST_DELIVERY_WORKER to a deliberately broken copy of
deploy/cloudflare-worker.js and the JS suite judges that file instead of the
committed one.
"""

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JS_SUITE = REPO_ROOT / "tests" / "digest" / "digest_delivery.test.js"

#: The behaviours the design mandates, named as they appear in the JS suite's own
#: test names. Used only to prove the suite cannot quietly become empty — never
#: to judge behaviour, which is the JS runner's job. Every one of these is
#: mutation-proved against a broken Worker copy.
REQUIRED_BEHAVIOURS = (
    # 1. the wrong-day refusal
    "validateArtifact refuses YESTERDAY's digest",
    # 2 + 3. both duplicate guards, the second with the ledger blind
    "the ledger alone suppresses a second send of today's morning digest",
    "the sequence_id read-back suppresses the send when the LEDGER IS BLIND",
    # 4. write-ahead ordering and the header ntfy actually reads
    "`pending` is on disk BEFORE the first POST",
    # U7. The producer side of the 07:50 ladder trigger: a verified publish
    # failure is recorded as `ntfy_failed`, and a success CLEARS it. Without
    # the first the fallback ladder is unreachable; without the second every
    # recovered slot emails the owner a duplicate forever.
    "a verified publish failure records ntfy_failed",
    "a FAILED fallback push records ntfy_failed too",
    "a read-back dedup CLEARS ntfy_failed, because the digest is on the topic",
    # 5. the fallback ladder, and its silence on the happy path
    "a good digest pushes ONCE, with the artifact's own body",
    "a stale-dated artifact falls back to the last good digest",
    "no artifact and no last good still PUSHES, naming the reason",
    # 5b. THE HEADLINE FIX. `dispatchDigest` was retained with ZERO call sites,
    # so a failed prebuild produced an apology and never a digest. Each of
    # these is a distinct way that can go wrong again, and each is
    # mutation-proved against a copy of the Worker with the call removed.
    "a failed prebuild DISPATCHES a late build, and the push still lands",
    "dispatchDigest is REACHABLE, not merely retained",
    "a slot that already pushed on the topic dispatches NOTHING",
    "the ladder reports HONESTLY when the dispatch itself fails",
    "a GOOD digest dispatches nothing, even with a GH_TOKEN in scope",
    "a late build that seals later produces NO second push for the slot",
    # 6 + 7. the fail-safes
    "a ledger READ failure pushes, writes nothing, and does not throw",
    "the retry loop stops at DELIVERY_BUDGET_MS",
    # 8. the probe cannot refresh the guard
    "?deliver_now=1 exercises the whole path and CANNOT write the ledger",
    "?deliver_now=1 is refused when the ingest secret is not configured",
    # 8b. ?deliver_test=1 — the labelled single-push transport probe. Every
    # delivery before it had never once produced an observable success, and the
    # only ways to prove it alive pushed the real alarming payload or proved
    # nothing about the Worker's own code. These six are what stop a later edit
    # from quietly turning it into a second, near-identical publish call, an
    # unauthenticated relay onto the owner's phone, a priority-5 test alert, or
    # a probe that can reach the delivery ledger.
    "?deliver_test=1 pushes ONE labelled low-priority message on the real publish function",
    "?deliver_test=1 CANNOT write or even read the delivery ledger",
    "?deliver_test=1 is refused without the ingest token",
    "?deliver_test=1 is refused when the ingest secret is not configured",
    "?deliver_test=1 surfaces ntfy's rejection: status AND body, and never the topic",
    "?deliver_test=1 is structurally unable to reach the ledger writer",
    # 9 + 10 + 11 + 12. the boundaries
    "the topic value never appears in any log line, on ANY delivery path",
    "the delivery path reaches ntfy and nothing else, with no GH_TOKEN in scope",
    "a verdict inside the artifact cannot fake or suppress a delivery",
    "the ingest endpoint fails CLOSED when the secret is absent",
    # 13. the switch, and the ledger it reads
    "the digest crons call deliverDigest",
    "dispatchDigest is RETAINED",
    "the 40 1 / 40 13 re-fire crons exist",
    "the 40 1 re-fire performs ZERO network calls",
    "the re-fire DOES deliver when the slot's cron never ran",
    "the recorded KV budget is real",
    "the KV budget deploy/schedule.json records is what the arm really spends",
    "?health=1 reports the re-fire crons",
    # 14. THE DATE THE ESCALATION DISPATCHES WITH. The Worker's own recovery
    # dispatch carries `slot_date`, and where that date comes from decides which
    # day the receiving run builds. It is anchored on the TICK's scheduled
    # instant rather than `Date.now` — the evening chain runs 13:35-13:55 UTC and
    # Nepal midnight is 18:15 UTC, so a late tick would otherwise send the NEXT
    # day's date for THIS slot, which is the 2026-10-17T18:16:53Z defect one hop
    # further up the chain.
    "the Worker's digest dispatch dates the slot from the TICK, never the clock",
)

#: node's own TAP plan line. Read from the runner's summary, never counted from
#: console lines by this file.
TAP_PLAN_RE = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)


class NodeMissingIsAFailure(unittest.TestCase):
    def test_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.fail(
                "node was not found on PATH, so the digest delivery tests CANNOT "
                "be executed.\n"
                "This fails instead of skipping on purpose: a skipped "
                "double-push guard is a green CI run with no double-push "
                "guard, and the whole point of this suite is that the one "
                "guard the owner cannot see is the one that must not rot. Fix "
                "the environment (install node, or put it on PATH) rather than "
                "the test."
            )
        proc = subprocess.run([node, "--version"], capture_output=True, text=True,
                              timeout=60)
        print("digest delivery tests: node %s (%s)"
              % (proc.stdout.strip(), node))


class DigestDeliveryJsSuiteTests(unittest.TestCase):
    """Run the JS suite under node and judge it by its EXIT CODE."""

    def test_the_js_suite_passes_against_the_committed_worker(self):
        node = shutil.which("node")
        if not node:
            self.fail("node is required to execute the digest delivery tests")
        proc = subprocess.run(
            # --test-reporter=tap is requested explicitly rather than relying on
            # node's default `spec` output: TAP is the only reporter that emits
            # a machine-readable plan line ("1..N"), and that line is what lets
            # this wrapper prove the suite RAN N tests instead of printing
            # nothing and exiting 0.
            [node, "--test", "--test-reporter=tap", str(JS_SUITE)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(
            0, proc.returncode,
            "tests/digest/digest_delivery.test.js FAILED against %s.\n"
            "This is the delivery guard: a stale artifact pushed unlabelled, a "
            "second push for one slot, `pending` written after the POST, a KV "
            "read failure that throws out of the cron, a silent path where no "
            "push happens at all, or the ntfy topic value reaching a log line, "
            "would all pass CI here if this suite did not go red.\n"
            "---- node output ----\n%s\n------------------"
            % (os.environ.get("DIGEST_DELIVERY_WORKER")
               or "the committed deploy/cloudflare-worker.js",
               output))
        plan = TAP_PLAN_RE.search(proc.stdout or "")
        self.assertIsNotNone(
            plan,
            "node's own TAP plan line was absent, so this module cannot even "
            "confirm the suite RAN anything. A wrapper that passes because the "
            "runner printed nothing is decoration.\n---- node output ----\n%s"
            % output)
        self.assertGreaterEqual(
            int(plan.group(1)), len(REQUIRED_BEHAVIOURS),
            "node's TAP plan says the suite ran only %s test(s), but %d "
            "mandated behaviours must be covered. Tests were probably deleted."
            % (plan.group(1), len(REQUIRED_BEHAVIOURS)))
        print("digest delivery tests: node TAP plan ran %s test(s), all green"
              % plan.group(1))

    def test_the_suite_still_covers_every_mandated_behaviour(self):
        """A suite that can lose a test without noticing is not a suite.

        Purely structural and deliberately dumb: it reads the committed test
        names and asserts every mandated behaviour is still named. It does NOT
        decide whether they pass — that is the test above.
        """
        self.assertTrue(JS_SUITE.is_file(), "%s is missing" % JS_SUITE)
        text = JS_SUITE.read_text(encoding="utf-8")
        missing = [b for b in REQUIRED_BEHAVIOURS if b not in text]
        self.assertEqual(
            [], missing,
            "tests/digest/digest_delivery.test.js no longer names %s. Each is a "
            "mutation-proved behaviour of the delivery guard; deleting one "
            "must be a deliberate act recorded in the report, never a silent "
            "removal." % missing)
        self.assertIn(
            "DIGEST_DELIVERY_WORKER", text,
            "the JS suite must honour the DIGEST_DELIVERY_WORKER override, or "
            "nobody can re-run its red/green pair in one command")
        # The claim, not the word. Banning the string outright would be
        # self-defeating: the suite has to be able to NAME the guarantee it does
        # not make. So every occurrence must be a negation, and an un-negated
        # one ("delivers exactly-once") is the failure this is looking for.
        mentions = len(re.findall(r"exactly-once", text))
        negated = len(re.findall(r"(?:not|no|never)[a-z -]*exactly-once", text,
                                 re.IGNORECASE))
        self.assertEqual(
            mentions, negated,
            "tests/digest/digest_delivery.test.js mentions exactly-once %d "
            "time(s) but negates it %d time(s). The delivery path is "
            "at-least-once with a possible duplicate on the recovery path only: "
            "there is no transaction spanning 'ntfy has the message' and 'KV has "
            "the record', so an un-negated claim of exactly-once in this file is "
            "a description of a guarantee the code cannot make."
            % (mentions, negated))
        self.assertIn(
            "AT-LEAST-ONCE", text,
            "the suite must state the guarantee it does hold, in those words, so "
            "a reader is never left to infer it")


if __name__ == "__main__":
    unittest.main(verbosity=2)