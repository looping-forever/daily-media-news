"""tests/digest/test_derive_step_wiring.py -- the derive step is REACHED.

tests/validate/test_derive_unsummarised.py proves the deriver is correct, and
tests/render/test_edition_label.py proves the label is rendered. Neither proves
the deriver is ever CALLED: a correct script that no workflow invokes is a file,
not a fallback. This module asserts the wiring, and it asserts it in the order
the run actually happens, because every one of these claims can be true while
the step is still unreachable:

  * the step exists, sits AFTER the model walk, and runs the committed script;
  * the walk carries `continue-on-error`, or a dead walk fails the job before
    the fallback is reached -- which is the exact defect that cost the slot;
  * the step is skipped when the walk DID write stories, so a normal run is
    byte-for-byte unchanged;
  * the step containing no model invocation at all, which is the whole promise:
    an unsummarised edition costs zero model calls.

Mutation-checked: removing any of these makes a named test below fail.
"""

import re
import unittest

import tests.workflows as W

WORKFLOW = "daily-news.yml"
DERIVE_STEP = "Derive the unsummarised edition"
AGENT_STEP = "Run OpenCode agent"
VALIDATE_STEP = "Validate stories"
DERIVER = "scripts/derive_unsummarised.py"


def steps():
    """[(index, name, step_dict)] in the order the workflow declares them."""
    out = []
    for index, (_job, name, sd) in enumerate(W.iter_all_steps(WORKFLOW)):
        out.append((index, name, sd))
    return out


def step_named(name):
    for index, step_name, sd in steps():
        if step_name == name:
            return index, sd
    raise AssertionError("daily-news.yml has no step %r. The step was renamed "
                         "or removed; update this suite deliberately rather "
                         "than deleting the check." % name)


class TheFallbackIsWiredTests(unittest.TestCase):
    def test_the_step_exists_and_calls_the_committed_script(self):
        _index, sd = step_named(DERIVE_STEP)
        run = sd["run"]
        self.assertIn(DERIVER, run,
                      "the derive step no longer runs %s, so the fallback is "
                      "never produced" % DERIVER)
        for flag, why in (("--candidates", "reads the collector's own output"),
                          ("--out", "writes the file validate_stories reads"),
                          ("--today", "dates the edition"),
                          ("--slot", "files it under the slot that is running")):
            self.assertIn(flag, run,
                          "the derive step passes no %s, and it is what %s"
                          % (flag, why))

    def test_it_runs_after_the_walk_and_before_validation(self):
        derive_index, _ = step_named(DERIVE_STEP)
        agent_index, _ = step_named(AGENT_STEP)
        validate_index, _ = step_named(VALIDATE_STEP)
        self.assertLess(agent_index, derive_index,
                        "the derive step runs BEFORE the model walk, so it would "
                        "write an unsummarised edition over a digest the walk "
                        "had not yet had its chance to build")
        self.assertLess(derive_index, validate_index,
                        "the derive step runs AFTER validation, so the edition "
                        "it writes is never validated, never counted and never "
                        "sealed")

    def test_the_walk_cannot_fail_the_job_before_the_fallback_runs(self):
        """THE SPECIFIC DEFECT THIS FALLBACK EXISTS FOR, PINNED.

        The walk used to `exit 1` on "no digest", which failed the job before
        the derive step could run. `continue-on-error` is what makes a dead walk
        surviveable, and the derive step decides from the FILE, not from this
        step's outcome.
        """
        _index, sd = step_named(AGENT_STEP)
        # The repo's own YAML reader keeps `true` as the string it read, so both
        # spellings are accepted and only a falsy value fails.
        self.assertIn(str(sd.get("continue-on-error")).strip().lower(),
                      ("true", "yes", "on"),
                      "the model walk no longer carries `continue-on-error: "
                      "true`. A failed walk then fails the job, and the derive "
                      "step below it never runs -- which is exactly the run this "
                      "fallback was written for.")

    def test_the_step_contains_no_model_invocation(self):
        _index, sd = step_named(DERIVE_STEP)
        run = sd["run"]
        for forbidden in ("opencode", "model_selector", "agent-log"):
            self.assertNotIn(
                forbidden, run,
                "the derive step mentions %r. This step is the fallback for a "
                "walk that could not reach a model; invoking one here (or "
                "depending on its output) would spend the very allowance that "
                "is unavailable, and would make the zero-model-call promise "
                "false." % forbidden)

    def test_the_step_yields_to_a_digest_the_walk_did_write(self):
        """A normal run must be unchanged, so the fallback decides from the FILE."""
        _index, sd = step_named(DERIVE_STEP)
        run = sd["run"]
        guard_at = run.find("state/stories.json")
        derive_at = run.find(DERIVER)
        self.assertNotEqual(-1, guard_at,
                            "the derive step no longer inspects state/stories.json, "
                            "so it would overwrite a digest the walk did produce")
        self.assertLess(guard_at, derive_at,
                        "the derive step writes before it checks whether a "
                        "digest already exists, so it can overwrite a real one")
        self.assertIn("exit 0", run[guard_at:derive_at],
                      "the stories-exist branch does not exit before the "
                      "deriver runs")
        self.assertRegex(run, r"HAVE=1|stories.{0,40}not used",
                         "the step has no 'a digest already exists' branch")

    def test_the_step_defers_to_the_empty_outcome_path_with_no_candidates(self):
        _index, sd = step_named(DERIVE_STEP)
        run = sd["run"]
        guard = re.search(r"\[\s*!\s*-s\s+state/candidates\.json\s*\]", run)
        self.assertIsNotNone(
            guard,
            "the derive step no longer refuses to run without candidates. The "
            "empty-outcome classification owns that case, and a deriver invoked "
            "on a missing file would exit non-zero and fail the slot.")
        self.assertLess(guard.start(), run.find(DERIVER),
                        "the candidate guard sits after the deriver call")


if __name__ == "__main__":
    unittest.main()
