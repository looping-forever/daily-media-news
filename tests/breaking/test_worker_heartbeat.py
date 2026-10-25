#!/usr/bin/env python3
"""tests/breaking/test_worker_heartbeat.py — CI wrapper for the dead-man's switch.

The behaviour under test is JavaScript (deploy/cloudflare-worker.js), and the
tests that check it are JavaScript too:
tests/breaking/worker_heartbeat.test.js runs under `node --test` against the
Worker's own functions with a fake KV and a fake fetch.

WHY A WRAPPER EXISTS AT ALL. The runner that enforces this repository is
`drift-canary.yml`'s `test-suite` job, every entry of which is a
`python3 -m unittest` invocation, and that job has no node setup step. This
module makes the JS suite reachable from that inventory without a second
mechanism, and — the part that actually matters — it makes a MISSING node
LOUD.

A liveness switch that silently skips is worse than no liveness switch: CI goes
green for a suite that never executed, and the one feature whose entire job is
to scream is the one nobody is looking at. So `node_executable()` calls
`self.fail()` with an explicit message rather than `skipTest`, matching
tests/academic/test_worker_kv_writes.py, which already fails loudly for exactly
this reason.

WHAT IS NOT HERE, ON PURPOSE: no Python re-implementation of any heartbeat
logic. A proxy is what let a real divergence survive a "parity" test before.

RUN IT YOURSELF (both forms, both real):
    node --test tests/breaking/worker_heartbeat.test.js
    python -m unittest tests.breaking.test_worker_heartbeat -v

REPRODUCING THE RED: set WORKER_HEARTBEAT_WORKER to a deliberately broken copy of
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
JS_SUITE = REPO_ROOT / "tests" / "breaking" / "worker_heartbeat.test.js"

#: The four behaviours the design mandates, named as they appear in the JS
#: suite's own test names. Used only to prove the suite cannot quietly become
#: empty — never to judge behaviour, which is the JS runner's job.
REQUIRED_BEHAVIOURS = (
    "?run=1 does NOT refresh a stale heartbeat",
    "the throttle suppresses the second write 1 minute after the first",
    "checkHeartbeats alarms once, then honours the 6-hour cooldown",
    "a KV read failure does NOT alarm",
)

#: node's own TAP plan line. Read from the runner's summary, never counted from
#: console lines by this file.
TAP_PLAN_RE = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)


class NodeMissingIsAFailure(unittest.TestCase):
    def test_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.fail(
                "node was not found on PATH, so the dead-man's switch tests "
                "CANNOT be executed.\n"
                "This fails instead of skipping on purpose: a skipped liveness "
                "test is a green CI run with no liveness test, which is the "
                "exact silence this repository has already paid for twice. Fix "
                "the environment (install node, or put it on PATH) rather than "
                "the test."
            )
        proc = subprocess.run([node, "--version"], capture_output=True, text=True,
                              timeout=60)
        print("worker heartbeat tests: node %s (%s)"
              % (proc.stdout.strip(), node))


class HeartbeatJsSuiteTests(unittest.TestCase):
    """Run the JS suite under node and judge it by its EXIT CODE."""

    def test_the_js_suite_passes_against_the_committed_worker(self):
        node = shutil.which("node")
        if not node:
            self.fail("node is required to execute the dead-man's switch tests")
        proc = subprocess.run(
            # --test-reporter=tap is requested explicitly rather than relying on
            # node's default `spec` output: TAP is the only reporter that emits a
            # machine-readable plan line ("1..N"), and that line is what lets this
            # wrapper prove the suite RAN N tests instead of printing nothing and
            # exiting 0.
            [node, "--test", "--test-reporter=tap", str(JS_SUITE)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(
            0, proc.returncode,
            "tests/breaking/worker_heartbeat.test.js FAILED against %s.\n"
            "This is the dead-man's switch: a heartbeat written by a manual "
            "?run=1 probe, an unbounded write cost, an alarm with no cooldown, "
            "or a KV blip that storms the owner, would all pass CI here if this "
            "suite did not go red.\n---- node output ----\n%s\n------------------"
            % (os.environ.get("WORKER_HEARTBEAT_WORKER")
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
        print("worker heartbeat tests: node TAP plan ran %s test(s), all green"
              % plan.group(1))

    def test_the_suite_still_covers_every_mandated_behaviour(self):
        """A suite that can lose a test without noticing is not a suite.

        Purely structural and deliberately dumb: it reads the committed test
        names and asserts the four mandated behaviours are still named. It does
        NOT decide whether they pass — that is the test above.
        """
        self.assertTrue(JS_SUITE.is_file(), "%s is missing" % JS_SUITE)
        text = JS_SUITE.read_text(encoding="utf-8")
        missing = [b for b in REQUIRED_BEHAVIOURS if b not in text]
        self.assertEqual(
            [], missing,
            "tests/breaking/worker_heartbeat.test.js no longer names %s. Each is "
            "a mutation-proved behaviour of the dead-man's switch; deleting one "
            "must be a deliberate act recorded in the report, never a silent "
            "removal." % missing)
        self.assertIn(
            "WORKER_HEARTBEAT_WORKER", text,
            "the JS suite must honour the WORKER_HEARTBEAT_WORKER override, or "
            "nobody can re-run its red/green pair in one command")


if __name__ == "__main__":
    unittest.main(verbosity=2)