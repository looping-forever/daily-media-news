"""tests/digest/test_derive_survivors_gate.py -- the fallback keys on SURVIVORS.

WHAT THIS PINS
--------------
On 2026-10-30 and again on 2026-10-31 the model wrote three stories, the
schema dropped all three, and the derive step still skipped the unsummarised
fallback because `state/stories.json` was NON-EMPTY. The gate was "a
model-written digest exists", which is true of a digest whose every story was
just rejected. The owner got an empty edition and the seal never ran.

The gate is now the number of stories that SURVIVE schema validation, counted
with the same `validate_story` the "Validate stories" step runs. These tests
assert both halves:

  * the shell gate reads that survivor count, and nothing between it and the
    deriver can skip the fallback when it is zero; and
  * the counter really is the production validator -- it is EXTRACTED from the
    workflow body and EXECUTED here, not reimplemented, so a change to either
    side breaks this suite.

Offline: a temp directory, the real validator module, stdlib only.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import tests.workflows as W

WORKFLOW = "daily-news.yml"
DERIVE_STEP = "Derive the unsummarised edition"
DERIVER = "scripts/derive_unsummarised.py"

#: The counter heredoc, taken from the shipped workflow text. Group 1 is the
#: Python body exactly as it runs on the runner.
COUNTER_RE = re.compile(
    r"python3 - <<'PY' > state/digest_survivors\.txt[^\n]*\n(.*?)\n\s*PY\n",
    re.S)


def step_body():
    for _job, name, step in W.iter_all_steps(WORKFLOW):
        if name == DERIVE_STEP:
            return step["run"]
    raise AssertionError(
        "daily-news.yml has no step %r; the fallback's gate cannot be checked"
        % DERIVE_STEP)


def counter_source():
    body = step_body()
    match = COUNTER_RE.search(body)
    if match is None:
        raise AssertionError(
            "the derive step has no survivor-count heredoc, so the gate cannot "
            "be keyed on survivors at all")
    return match.group(1)


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


class TheGateIsTheSurvivorCountTests(unittest.TestCase):
    def test_the_gate_reads_the_survivor_count(self):
        body = step_body()

        self.assertIn("read RAW SURVIVORS < state/digest_survivors.txt", body,
                      "the derive step no longer reads the survivor count")
        self.assertIn('if [ "$SURVIVORS" != "0" ]', body,
                      "the 'a digest exists' branch is not keyed on survivors")
        self.assertNotRegex(
            body, r"HAVE=1",
            "the gate is back to 'state/stories.json is non-empty', which is "
            "true of a payload whose every story was rejected")
        self.assertLess(body.index('if [ "$SURVIVORS" != "0" ]'),
                        body.index(DERIVER),
                        "the survivor gate sits AFTER the deriver, so it can "
                        "never prevent an empty edition")

    def test_nothing_between_the_gate_and_the_deriver_can_skip_the_fallback(self):
        body = step_body()
        gate = body.index('if [ "$SURVIVORS" != "0" ]')
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

    def test_the_counter_runs_the_real_validator(self):
        src = counter_source()

        self.assertIn("import validate_stories as vs", src,
                      "the counter does not use the production validator, so "
                      "it can disagree with the step that drops the stories")
        self.assertIn("vs.validate_story(", src)


class TheCounterBehaviourTests(unittest.TestCase):
    """The extracted counter, executed against real payloads."""

    def run_counter(self, stories, write_file=True):
        tmp = tempfile.mkdtemp(prefix="survivors-gate-")
        os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
        if write_file:
            with open(os.path.join(tmp, "state", "stories.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"digest_time": "morning", "stories": stories}, fh)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(W.REPO_ROOT / "scripts")
        proc = subprocess.run([sys.executable, "-c", counter_source()],
                              cwd=tmp, env=env, capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout.strip()

    def test_every_story_rejected_is_zero_survivors(self):
        """THE 2026-10-31 SHAPE: three stories in, zero survive."""
        stories = [good_story(title=""), good_story(title="  "),
                   good_story(title=123)]

        self.assertEqual("3 0", self.run_counter(stories),
                         "an all-rejected payload no longer reads as zero "
                         "survivors, so the fallback would be skipped again")

    def test_stories_that_pass_are_counted_as_survivors(self):
        self.assertEqual("3 3", self.run_counter([good_story() for _ in range(3)]))

    def test_a_partial_payload_counts_only_the_survivors(self):
        stories = [good_story(), good_story(title=""), good_story()]

        self.assertEqual("3 2", self.run_counter(stories))

    def test_the_2026_10_30_PAYLOAD_NOW_SURVIVES(self):
        """The incident's own flaw: an absent/null optional field."""
        stories = [good_story(spoiler_reason=None),
                   good_story(spoiler_reason=None),
                   good_story(spoiler_reason=None)]

        self.assertEqual("3 3", self.run_counter(stories))

    def test_a_missing_or_unaligned_payload_is_zero_survivors(self):
        self.assertEqual("0 0", self.run_counter([], write_file=False))
        self.assertEqual("0 0", self.run_counter({"not": "a list"}))


if __name__ == "__main__":
    unittest.main()
