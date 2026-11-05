"""tests/digest/test_repair_gate.py -- the stub-repair stage is REACHABLE.

RUN 42061741326 (2026-11-05) proved the opposite of what this step was written
for: the step existed, the script was correct, and the step still never ran,
because its `if:` carried `DRY_RUN != 'true'`. A verification run is exactly the
run that needs to exercise the repair, so this module pins:

  * the gate keeps the two terms that mean "there is nothing to repair"
    (FALLBACK_NOOP, EMPTY_DIGEST) and no longer refuses a DRY_RUN;
  * the step still runs the committed script against the file validation wrote;
  * it cannot fail the edition, and it keeps its bound;
  * it hands the summariser the committed cooldown file, so a rung that fails
    there is demoted for the NEXT run rather than burning the same ~99 s again.
"""

import unittest

import tests.workflows as W

WORKFLOW = "daily-news.yml"
REPAIR_STEP = "Summarise the summaries the model only stubbed"
VALIDATE_STEP = "Validate stories"
ENRICH_STEP = "Enrich stories with media (og:image/video)"
SCRIPT = "scripts/summarise_stubs.py"


def step_named(name):
    for _job, step_name, sd in W.iter_all_steps(WORKFLOW):
        if step_name == name:
            return sd
    raise AssertionError("daily-news.yml has no step %r. The step was renamed "
                         "or removed; update this suite deliberately rather "
                         "than deleting the check." % name)


def step_index(name):
    for index, (_job, step_name, _sd) in enumerate(W.iter_all_steps(WORKFLOW)):
        if step_name == name:
            return index
    raise AssertionError("daily-news.yml has no step %r" % name)


class TheGateLetsAVerificationRunThrough(unittest.TestCase):
    def test_a_dry_run_does_not_skip_the_repair(self):
        condition = str(step_named(REPAIR_STEP).get("if") or "")
        self.assertNotIn(
            "DRY_RUN", condition,
            "the repair step is still gated on DRY_RUN (%r). A dry run sends no "
            "notification and commits nothing, so refusing it here means the "
            "one run that exists to VERIFY the repair can never exercise it."
            % condition)

    def test_the_two_terms_that_mean_nothing_to_repair_stay(self):
        condition = str(step_named(REPAIR_STEP).get("if") or "")
        for term in ("FALLBACK_NOOP", "EMPTY_DIGEST"):
            self.assertIn(term, condition,
                          "the repair step no longer checks %s, so it runs on a "
                          "run that produced nothing to repair" % term)

    def test_it_says_out_loud_that_a_dry_run_spends_quota(self):
        # The reason belongs beside the gate as a comment, not in the shell body,
        # so read the raw source and hold the comment to the same claim.
        source = W.raw_workflow_text(WORKFLOW)
        self.assertIn("spends model quota", source,
                      "the gate was loosened without saying what it now costs: a "
                      "dry run reaches a model. That is the point of a "
                      "verification run, and it must be stated where the gate is.")


class TheStepIsStillWiredAndBounded(unittest.TestCase):
    def test_it_runs_the_committed_script_on_the_validated_file(self):
        run = step_named(REPAIR_STEP).get("run") or ""
        self.assertIn(SCRIPT, run)
        self.assertIn("--in state/stories.validated.json", run,
                      "the repair must rewrite the file validation just wrote")
        self.assertIn("--candidates state/candidates.json", run,
                      "without the candidates the predicate cannot spot "
                      "publisher text copied as a summary")

    def test_it_runs_after_validation_and_before_the_media_enrichment(self):
        self.assertLess(step_index(VALIDATE_STEP), step_index(REPAIR_STEP),
                        "a repair before validation is repaired by nothing")
        self.assertLess(step_index(REPAIR_STEP), step_index(ENRICH_STEP),
                        "the media stage reads the story list the repair rewrites")

    def test_it_cannot_fail_the_edition(self):
        sd = step_named(REPAIR_STEP)
        self.assertIn(str(sd.get("continue-on-error")).strip().lower(),
                      ("true", "yes", "on"),
                      "a repair that reaches no model is already labelled by the "
                      "counts; letting it fail the job would cost the edition")

    def test_it_keeps_its_wall_clock_bound(self):
        sd = step_named(REPAIR_STEP)
        self.assertEqual("8", str(sd.get("timeout-minutes")),
                         "summarise_stories.TOTAL_BUDGET_SECONDS is 300; the "
                         "step bound is what lets the process finish writing")


class TheDemotionSurvivesToTheNextRun(unittest.TestCase):
    def test_the_step_hands_the_summariser_the_committed_cooldown_file(self):
        env = step_named(REPAIR_STEP).get("env") or {}
        self.assertEqual("state/model_cooldown.json",
                         str(env.get("MODEL_COOLDOWN_PATH") or ""),
                         "the summariser records a demoted rung in "
                         "MODEL_COOLDOWN_PATH; without it here the demotion is "
                         "written nowhere and the next run pays the same ~99 s")
