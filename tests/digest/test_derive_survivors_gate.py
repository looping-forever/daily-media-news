"""tests/digest/test_derive_survivors_gate.py -- the fallback keys on what WOULD SHIP.

WHAT THIS PINS
--------------
On 2026-10-30 and again on 2026-10-31 the model wrote three stories, the
schema dropped all three, and the derive step still skipped the unsummarised
fallback because `state/stories.json` was NON-EMPTY. The gate became "count the
schema survivors and compare to STORIES_FLOOR".

That was still wrong, and run 43235310367 (2026-11-12) proved it: FOUR stories
survived the schema, the gate passed at 4 >= floor 4, skipped the fallback --
and history dedup, which runs INSIDE `validate_stories.py` and therefore after
this gate, removed two of them. A 2-card edition shipped as a healthy digest.

The gate now runs the REAL validator with `--count-only`: the same schema,
keyword, spoiler, crime, history-dedup, event-dedup and rank-cap stages the
"Validate stories" step runs, read-only, printing `COUNT=<n>`. These tests
assert both halves:

  * the shell gate reads `COUNT=`, not a schema check of its own, and the floor
    comparison still sits between that count and the deriver; and
  * the count really is the production pipeline -- the CLI is EXECUTED here, so a
    gate that reimplements the counting in the workflow breaks this suite.

Offline: temp directories, the real scripts, stdlib only.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import tests.workflows as W

WORKFLOW = "daily-news.yml"
DERIVE_STEP = "Derive the unsummarised edition"
DERIVER = "scripts/derive_unsummarised.py"
COUNTER = "scripts/validate_stories.py --count-only"
FIXTURE_CONFIG = str(W.REPO_ROOT / "tests" / "fixtures" / "config")


def step_body():
    for _job, name, step in W.iter_all_steps(WORKFLOW):
        if name == DERIVE_STEP:
            return step["run"]
    raise AssertionError(
        "daily-news.yml has no step %r; the fallback's gate cannot be checked"
        % DERIVE_STEP)


def good_story(**overrides):
    base = {
        "title": "Kathmandu Valley Ring Road Widening Begins",
        "summary": "Confirmed: the expansion works started on the eastern section.",
        "category": "nepal",
        "priority": 5,
        "source_name": "Kathmandu Post",
        "source_url": "https://kathmandupost.example.com/ring-road",
        "published_at": None,
        "event_date": None,
        "location": None,
        "reliability": "tier1",
        "spoiler_level": "none",
        "spoiler_reason": "",
        "relevance_reason": "Infrastructure project in the valley.",
        "language": "en",
    }
    base.update(overrides)
    return base


STORY_SET = [
    good_story(),
    good_story(title="TU Publishes Revised Semester Exam Schedule",
               source_url="https://tuexam.example.edu.np/schedule"),
    good_story(title="New Direct Bus Service to Pokhara Starts",
               source_url="https://kathmandupost.example.com/bus-pokhara"),
    good_story(title="Mobile Wallet Fees Cut by Regulator",
               source_url="https://finance.example.com/wallet-fees"),
]


class TheGateIsTheValidatorsCountTests(unittest.TestCase):
    def test_the_gate_runs_the_real_validator_in_count_only_mode(self):
        body = step_body()

        self.assertIn(COUNTER, body,
                      "the derive gate no longer asks the validator what would "
                      "ship, so it can disagree with the edition again")
        self.assertIn("COUNT=", body,
                      "the gate does not read the validator's own count")
        self.assertIn("--history state/history.json", body,
                       "the count is taken without the history file, so it is "
                       "a schema count wearing a count-only flag")

    def test_the_workflow_no_longer_counts_schema_survivors_itself(self):
        """THE DEFECT: counting `validate_story` here is what missed dedup."""
        body = step_body()

        self.assertNotIn("vs.validate_story(", body,
                         "the workflow is counting schema survivors itself "
                         "again; that number ignores history dedup, which is "
                         "how 4 survivors became 2 cards in 43235310367")
        self.assertNotIn("digest_survivors.txt", body,
                         "the old survivors file is back, so the gate is not "
                         "reading the validator's count")

    def test_the_gate_reads_the_count_against_the_floor_before_the_deriver(self):
        """CHANGED 2026-11-02: any survivors used to be enough.

        Run 41369743596 wrote TWO stories, both valid, and this gate skipped the
        fallback because the count was not zero -- so a 2-story edition shipped
        as a healthy digest. The gate is the same `STORIES_FLOOR` (4) the prompt
        states and the walk's acceptance check enforces.
        """
        body = step_body()

        self.assertIn('if [ "$SURVIVORS" -ge "$STORIES_FLOOR" ]', body,
                      "the 'a digest exists' branch is not keyed on the floor")
        self.assertNotRegex(
            body, r"HAVE=1",
            "the gate is back to 'state/stories.json is non-empty', which is "
            "true of a payload whose every story was rejected")
        self.assertLess(body.index('if [ "$SURVIVORS" -ge "$STORIES_FLOOR" ]'),
                        body.index(DERIVER),
                        "the floor gate sits AFTER the deriver, so it can never "
                        "prevent a thin edition")

    def test_the_floor_is_one_number_and_a_missing_one_is_loud(self):
        body = step_body()

        self.assertIn(': "${STORIES_FLOOR:?', body,
                      "the derive step does not refuse to run without the "
                      "workflow-level floor; an unset floor makes every "
                      "comparison false and the gate meaningless")
        self.assertIn("below the ${STORIES_FLOOR}-story floor", body,
                      "a payload below the floor is not reported with the "
                      "number it missed")

    def test_a_thin_payload_is_removed_before_the_deriver_runs(self):
        """The deriver writes nothing below ITS OWN floor, so a file left here
        would be the too-thin model payload being sealed as an ordinary one."""
        body = step_body()
        gate = body.index('if [ "$SURVIVORS" -ge "$STORIES_FLOOR" ]')
        remove_at = body.index("rm -f state/stories.json", gate)
        self.assertLess(
            remove_at, body.index(DERIVER),
            "the thin payload is not removed before the deriver, so a floor "
            "miss leaves it in place for validation to seal")

    def test_nothing_between_the_gate_and_the_deriver_can_skip_the_fallback(self):
        body = step_body()
        gate = body.index('if [ "$SURVIVORS" -ge "$STORIES_FLOOR" ]')
        # Skip the survivors branch's own `exit 0`: it is the branch that RAN
        # only when survivors were non-zero, so it is not a path out of a
        # zero-survivor run.
        after_gate = body.index("exit 0", gate) + len("exit 0")
        between = body[after_gate:body.index(DERIVER)]

        self.assertEqual(
            1, between.count("exit 0"),
            "a second early exit sits between the survivor gate and the "
            "deriver; on a zero-survivor run it can still swallow the fallback")
        self.assertIn("[ ! -s state/candidates.json ]", between,
                      "the one allowed exit is no longer the documented "
                      "no-candidates guard")


class TheCounterBehaviourTests(unittest.TestCase):
    """The production CLI, executed against real payloads."""

    def run_counter(self, stories, history=None):
        tmp = tempfile.mkdtemp(prefix="survivors-gate-")
        in_path = os.path.join(tmp, "stories.json")
        with open(in_path, "w", encoding="utf-8") as fh:
            json.dump({"digest_time": "morning", "date": "2026-11-12",
                       "stories": stories}, fh)
        history_path = os.path.join(tmp, "history.json")
        if history:
            with open(history_path, "w", encoding="utf-8") as fh:
                json.dump(history, fh)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(W.REPO_ROOT / "scripts")
        proc = subprocess.run(
            [sys.executable, str(W.REPO_ROOT / "scripts" / "validate_stories.py"),
             "--count-only", "--in", in_path,
             "--history", history_path, "--today", "2026-11-12",
             "--config-dir", FIXTURE_CONFIG,
             "--state", os.path.join(tmp, "active_stories.json"),
             "--candidates", os.path.join(tmp, "candidates.json")],
            cwd=tmp, env=env, capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        for line in proc.stdout.splitlines():
            if line.startswith("COUNT="):
                return line.split("=", 1)[1].strip()
        raise AssertionError(
            "the validator printed no COUNT= line, so this gate has no number "
            "to read: %r" % proc.stdout)

    def test_every_story_rejected_is_zero(self):
        """THE 2026-10-31 SHAPE: three stories in, nothing survive."""
        stories = [good_story(title=""), good_story(title="  "),
                   good_story(title=123)]

        self.assertEqual("0", self.run_counter(stories),
                         "an all-rejected payload no longer reads as zero, so "
                         "the fallback would be skipped again")

    def test_stories_that_pass_are_counted(self):
        self.assertEqual("4", self.run_counter(STORY_SET))

    def test_a_partial_payload_counts_only_the_survivors(self):
        stories = [good_story(), good_story(title=""),
                   good_story(title="New Direct Bus Service to Pokhara Starts",
                              source_url="https://other.example.com/bus")]

        self.assertEqual("2", self.run_counter(stories))

    def test_the_43235310367_shape_counts_two_not_four(self):
        """THE DEFECT END TO END: 4 schema survivors, 2 already sent."""
        history = {"stories": [
            {"title": s["title"], "url": s["source_url"],
             "date_sent": "2026-11-11"} for s in STORY_SET[:2]]}

        self.assertEqual("2", self.run_counter(STORY_SET, history=history),
                         "the gate's number is not the edition's number again")

    def test_the_2026_10_30_PAYLOAD_NOW_SURVIVES(self):
        """The incident's own flaw: an absent/null optional field."""
        stories = [good_story(spoiler_reason=None),
                   good_story(spoiler_reason=None,
                              title="TU Publishes Revised Semester Exam Schedule",
                              source_url="https://tuexam.example.edu.np/schedule")]

        self.assertEqual("2", self.run_counter(stories))


if __name__ == "__main__":
    unittest.main()
