#!/usr/bin/env python3
"""tests/workflows/test_digest_watchdog_push_permissions.py — a job that
pushes must hold `contents: write`.

WHY THIS EXISTS
---------------
Measured 2026-10-31, run 41191885247: `digest-watchdog.yml` declared
`contents: read` at BOTH permission sites, and the job-level block of
`empty_digest_retry` overrides the workflow block. Every ledger push therefore
died with

    remote: Permission to looping-forever/daily-media-news.git denied to
    github-actions[bot].

followed by a 403 on all three attempts. The failure was swallowed as a
warning, so the 6h de-duplication window silently stopped persisting and
nothing went red.

The defect is a PERMISSION, not a push race, and it is invisible to any test
that only checks the shell. This one resolves the permission each pushing job
actually ends up with -- job-level block first, workflow-level block second --
which is the same precedence GitHub Actions applies.

RUN IT YOURSELF
    python -m unittest tests.workflows.test_digest_watchdog_push_permissions -v

REPRODUCING THE RED
    DIGEST_WATCHDOG_FILE points this test at another workflow file, so the
    pre-fix version can be judged without editing the repository. The override
    prints a NOTE so it can never be mistaken for CI.
"""

import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - direct execution
    sys.path.insert(0, str(REPO_ROOT))

from tests import workflows as W  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "digest-watchdog.yml"

#: A real push command, never a comment that mentions one. Anchored at a line
#: start and required to carry `origin HEAD:` so prose such as "a push that
#: loses master" cannot satisfy it.
PUSH_RE = re.compile(r"^[^#\n]*\bgit\b[^\n]*\bpush\b[^\n]*\borigin\b[^\n]*HEAD:",
                     re.MULTILINE)


def _workflow_path():
    override = os.environ.get("DIGEST_WATCHDOG_FILE")
    if override:
        path = Path(override)
        print("NOTE: judging %s (DIGEST_WATCHDOG_FILE) INSTEAD of %s"
              % (path, WORKFLOW))
        return path
    return WORKFLOW


def _effective_contents(doc, job):
    """(value, where_it_came_from) for one job's `contents` permission.

    A job-level block REPLACES the workflow-level block rather than merging
    with it, which is why a job that declares `contents: read` is read-only no
    matter what the top of the file says.
    """
    job_perm = job.get("permissions")
    if isinstance(job_perm, dict) and job_perm:
        return job_perm.get("contents"), "job"
    top = doc.get("permissions")
    if isinstance(top, dict):
        return top.get("contents"), "workflow"
    return None, "none"


def pushing_jobs(doc):
    """{job_id: (job, [step names that push])} for every job that pushes."""
    found = {}
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = []
        for step in (job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str) and PUSH_RE.search(run):
                steps.append(str(step.get("name") or "<unnamed step>"))
        if steps:
            found[job_id] = (job, steps)
    return found


class DigestWatchdogPushPermissions(unittest.TestCase):
    def setUp(self):
        self.path = _workflow_path()
        self.assertTrue(self.path.is_file(), "%s is missing" % self.path)
        self.doc = W.load_workflow_text(self.path.read_text(encoding="utf-8"))

    def test_the_push_sites_are_still_found(self):
        """A green suite must mean "every push is checked", not "none seen"."""
        jobs = pushing_jobs(self.doc)
        self.assertTrue(
            jobs,
            "no job in %s runs a `git push`, so the permission check below "
            "would pass without checking anything. If the pushes moved, this "
            "test must move with them." % self.path)
        print("push audit: %s"
              % ", ".join("%s -> %s" % (job_id, steps)
                          for job_id, (job, steps) in sorted(jobs.items())))

    def test_every_pushing_job_holds_contents_write(self):
        problems = []
        for job_id, (job, steps) in sorted(pushing_jobs(self.doc).items()):
            value, source = _effective_contents(self.doc, job)
            if str(value or "").strip() != "write":
                problems.append(
                    "job %r pushes in %r but its effective `contents` "
                    "permission is %r (from the %s-level block)"
                    % (job_id, steps, value, source))
        self.assertEqual(
            [], problems,
            "%s cannot push its state ledgers.\n  %s\nA `contents: read` token "
            "is refused by GitHub with `Permission to ... denied to "
            "github-actions[bot]` and a 403 on every attempt, so the ledger "
            "silently stops persisting. Both the workflow-level block and any "
            "job-level block that overrides it must grant `contents: write`."
            % (self.path, "\n  ".join(problems)))

    def test_a_lost_push_is_an_error_annotation_and_still_not_a_red_job(self):
        """Loud, but the watchdog's own verdict must survive a lost ledger."""
        for job_id, (job, steps) in sorted(pushing_jobs(self.doc).items()):
            for step in (job.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if not (isinstance(run, str) and PUSH_RE.search(run)):
                    continue
                label = "%s/%s" % (job_id, step.get("name") or "<unnamed step>")
                self.assertIn(
                    "::error::", run,
                    "%s swallows a lost push as a warning. The run stays green "
                    "on purpose, but the log must carry an ::error:: line "
                    "naming the attempt count and the real cause." % label)
                self.assertIn(
                    "exit 0", run,
                    "%s no longer ends `exit 0`, so a lost ledger push would "
                    "turn the whole watchdog red for a reason that is not the "
                    "digest verdict." % label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
