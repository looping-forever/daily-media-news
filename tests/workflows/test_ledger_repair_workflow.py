#!/usr/bin/env python3
"""tests/workflows/test_ledger_repair_workflow.py -- ledger-repair.yml's contract.

WHY THIS EXISTS
---------------
`ledger-repair.yml` is the one workflow in this repository that edits the
delivery ledger stored in KV. Its correctness claims are therefore narrow and
must not drift:

  * MANUAL ONLY       -- workflow_dispatch and nothing else: no schedule, no
                         push, no repository_dispatch. A repair of historical
                         delivery records must never fire on its own.
  * DRY RUN BY DEFAULT -- the input defaults to true and the write flag is
                         DERIVED from it, so an unasked-for dispatch cannot
                         write.
  * ONE WRITE PATH    -- exactly one step runs scripts/repair_ledger.py, with
                         the same three credentials the daily step uses, and
                         the log can never carry them.
  * REGISTERED        -- in tests/workflows/__init__.py and in
                         config/actions-budget.json. An unregistered workflow is
                         one every lint check silently skips.
"""

import json
import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
for _path in (_TESTS, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tests import workflows as W  # noqa: E402

WORKFLOW = "ledger-repair.yml"
SCRIPT = "scripts/repair_ledger.py"
BUDGET_PATH = Path(_ROOT) / "config" / "actions-budget.json"


def repair_step():
    found = [(job, name, step) for job, name, step in W.iter_all_steps(WORKFLOW)
             if "scripts/repair_ledger.py" in str(step.get("run") or "")]
    return found


class RegistrationTests(unittest.TestCase):
    def test_the_workflow_file_exists_and_is_registered(self):
        self.assertTrue((W.WORKFLOW_DIR / WORKFLOW).is_file(),
                        "%s is missing from .github/workflows" % WORKFLOW)
        self.assertIn(WORKFLOW, W.WORKFLOW_NAMES,
                      "%s is not in WORKFLOW_NAMES, so every lint check in "
                      "tests/workflows silently skips it" % WORKFLOW)

    def test_the_script_it_runs_exists(self):
        self.assertTrue((Path(_ROOT) / SCRIPT).is_file(),
                        "%s names %s, which does not exist" % (WORKFLOW, SCRIPT))

    def test_it_has_a_budget_entry(self):
        budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        self.assertIn(WORKFLOW, budget["workflows"],
                      "%s has no entry in %s, which the budget check fails on"
                      % (WORKFLOW, BUDGET_PATH.name))
        entry = budget["workflows"][WORKFLOW]
        self.assertEqual([], entry["crons"],
                         "the workflow has no schedule, so its entry must "
                         "declare no crons")
        self.assertEqual(1, entry["jobs_per_run"])
        self.assertGreater(entry["p95_minutes"], 0)


class DispatchOnlyTests(unittest.TestCase):
    def setUp(self):
        self.doc = W.load_workflow(WORKFLOW)
        self.on = self.doc.get("on")

    def test_the_only_trigger_is_workflow_dispatch(self):
        self.assertIsInstance(self.on, dict)
        self.assertEqual(["workflow_dispatch"], list(self.on),
                         "this workflow repairs delivery history; nothing but a "
                         "human may trigger it")

    def test_there_is_no_schedule_and_no_dispatch_surface(self):
        raw = W.raw_workflow_text(WORKFLOW)
        self.assertNotIn("schedule", self.on)
        self.assertNotIn("schedule:", raw,
                         "a cron here would re-read the ledger every day to "
                         "find nothing to do")
        # The parsed trigger map above is the authority on what can fire this;
        # the text scan only guards the YAML key itself, so a comment may
        # discuss the trigger it deliberately does not use.
        for trigger in ("push:", "pull_request:", "repository_dispatch:",
                        "workflow_call:"):
            self.assertNotIn(trigger, raw,
                             "%r would let something other than a human run a "
                             "repair of delivery history" % trigger)

    def test_dry_run_is_a_boolean_that_defaults_to_true(self):
        inputs = self.on["workflow_dispatch"]["inputs"]
        self.assertIn("dry_run", inputs)
        self.assertEqual("boolean", inputs["dry_run"]["type"])
        self.assertEqual("true", inputs["dry_run"]["default"],
                         "the write must have to be asked for")


class WriteGateTests(unittest.TestCase):
    def setUp(self):
        self.found = repair_step()
        self.assertEqual(1, len(self.found),
                         "expected exactly one step running %s, found %d"
                         % (SCRIPT, len(self.found)))
        _job, _name, self.step = self.found[0]
        self.env = self.step.get("env") or {}
        self.run = str(self.step.get("run") or "")

    def test_the_write_flag_is_derived_from_the_input(self):
        mode = str(self.env.get("REPAIR_MODE") or "")
        self.assertIn("inputs.dry_run == false", mode,
                      "the write flag must be derived from the dry_run input, "
                      "not typed independently of it: %r" % mode)
        self.assertIn("--write", mode)
        self.assertIn("--dry-run", mode,
                      "the fallback must be a dry run, so an unset or unexpected "
                      "input value cannot write")
        self.assertIn("$REPAIR_MODE", self.run)

    def test_it_uses_the_same_credentials_as_the_daily_step(self):
        self.assertEqual("${{ secrets.CF_PAGES_API_TOKEN }}",
                         self.env.get("CF_LEDGER_TOKEN"))
        self.assertEqual("${{ secrets.CF_ACCOUNT_ID }}",
                         self.env.get("CF_ACCOUNT_ID"))
        self.assertEqual("${{ vars.CF_KV_NAMESPACE_ID }}",
                         self.env.get("CF_KV_NAMESPACE_ID"))

    def test_the_run_body_cannot_print_a_credential(self):
        for forbidden in ("$CF_LEDGER_TOKEN", "$CF_ACCOUNT_ID",
                          "$CF_KV_NAMESPACE_ID", "printenv", "set -x", "env |"):
            self.assertNotIn(forbidden, self.run,
                             "the run body references %r; this repository is "
                             "public and its logs are too" % forbidden)
        self.assertNotIn("echo", self.run.lower())

    def test_the_job_is_read_only_and_cannot_push(self):
        self.assertEqual({"contents": "read"}, self.doc_permissions())
        checkouts = [step for _job, _name, step
                     in W.iter_all_steps(WORKFLOW)
                     if str(step.get("uses") or "").startswith("actions/checkout")]
        self.assertEqual(1, len(checkouts))
        self.assertEqual("false", str(checkouts[0].get("with", {})
                                      .get("persist-credentials")),
                         "the checkout must not leave a token in .git/config")

    def test_the_job_is_bounded(self):
        jobs = self.doc_jobs()
        self.assertEqual(1, len(jobs))
        job = list(jobs.values())[0]
        self.assertIn("timeout-minutes", job,
                      "a wedged runner must not hold this job open")

    def doc_permissions(self):
        return W.load_workflow(WORKFLOW).get("permissions")

    def doc_jobs(self):
        return W.load_workflow(WORKFLOW).get("jobs") or {}


if __name__ == "__main__":
    unittest.main(verbosity=2)
