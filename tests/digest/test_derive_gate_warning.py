#!/usr/bin/env python3
"""tests/digest/test_derive_gate_warning.py — one true warning, not three.

WHAT THIS PINS
--------------
Run 43246133293 printed three warnings in a row about the same five stories:

    Only 3 of 5 model-written stories would ship, below the 4-story floor
    All 5 model-written stories were rejected by validation (0 of 5 passed ...)
    No model-written digest met the 4-story floor -- deriving ...

The first was true. The second was FALSE: the schema rejected none of them, two
went to history dedup and the rest to the rank cap -- the numbers existed in the
provenance line and no branch used them. The third restated the first.

The gate now parses all five counts, computes REJECTED / HISTORY_DROPS /
EVENT_DROPS / CAP_DROPS, and prints exactly ONE branch, each true about its own
population, followed once by the neutral floor statement. These tests EXECUTE
the step's bash with a stubbed validator whose output is the run's own number
shape, so the arithmetic and the branching are measured, not grepped.

Offline: temp directories, stub python3, no provider, no network.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

import tests.workflows as W

WORKFLOW = "daily-news.yml"
DERIVE_STEP = "Derive the unsummarised edition"

#: `python3` on PATH: Windows has none that Git-for-Windows bash can execute.
#: `--count-only` answers with the fixture provenance; `-c` is real Python (the
#: step's timings write); everything else is the deriver, stubbed to exit 0.
PY3_SHIM = r"""#!/usr/bin/env bash
case "$*" in
  *--count-only*) cat ./prov.txt; exit 0 ;;
  -c*) exec "${STUB_REAL_PYTHON:?}" "$@" ;;
esac
exit 0
"""

BRANCHES = ("would ship", "were rejected by validation", "None of the",
            "returned no stories at all")


def step_body():
    for _job, name, step in W.iter_all_steps(WORKFLOW):
        if name == DERIVE_STEP:
            return step["run"]
    raise AssertionError("daily-news.yml has no step %r" % DERIVE_STEP)


class TheGateSaysOneTrueThing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gate-warning-"))
        self.bash = W.bash_executable()
        if self.bash is None:
            self.fail("bash was not found on PATH, so the gate's own branching "
                      "has never run. Install bash rather than editing this.")
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "stories.json").write_text(
            '{"stories": [{"title": "x"}]}', encoding="utf-8")
        (self.tmp / "state" / "candidates.json").write_text(
            '{"candidates": [{"title": "a"}, {"title": "b"}]}', encoding="utf-8")
        (self.tmp / "state" / "history.json").write_text("{}", encoding="utf-8")
        (self.tmp / "config").mkdir()
        self.bin = self.tmp / "bin"
        W.install_stub_on_path(self.bin, PY3_SHIM, name="python3")

    def gate(self, provenance):
        """Run the real step body with `provenance` as the validator's output."""
        (self.tmp / "prov.txt").write_text(provenance + "\n", encoding="utf-8")
        env = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "STUB_REAL_PYTHON": sys.executable,
            "STORIES_FLOOR": "4",
            "TODAY": "2026-11-13",
            "DIGEST_TIME": "morning",
            "GITHUB_STEP_SUMMARY": str(self.tmp / "summary.md"),
        }
        proc = W.run_bash_script(step_body(), env_overrides=env,
                                 cwd=str(self.tmp), bash=self.bash, timeout=120)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        warnings = [line for line in proc.stdout.splitlines()
                    if line.startswith("::warning::")]
        branches = [line for line in warnings
                    if any(marker in line for marker in BRANCHES)]
        return proc.stdout, warnings, branches

    def test_the_43246133293_shape_says_one_thing_and_it_is_true(self):
        """5 written, 5 passed the schema, 2 already sent, 3 would ship."""
        out, warnings, branches = self.gate(
            "digest provenance: candidates=53 raw=5 schema-survivors=5 "
            "after-history-dedup=3 after-event-dedup=3 would-ship=3\nCOUNT=3")

        self.assertEqual(1, len(branches),
                         "more than one population is being described again: %r"
                         % (branches,))
        self.assertIn("Only 3 of 5 model-written stories would ship", branches[0])
        self.assertIn("0 rejected by validation, 2 already sent within TTL, "
                      "0 duplicating an event already reported", branches[0])
        self.assertNotIn("All 5", out,
                         "the false 'all rejected' sentence is back, and the "
                         "schema rejected none of them")
        self.assertNotIn("returned no stories at all", out,
                         "the model did write five stories")
        self.assertIn("Digest provenance: candidates=2 raw=5 schema-survivors=5 "
                      "after-history-dedup=3 after-event-dedup=3 would-ship=3", out)

    def test_everything_rejected_by_the_schema_says_exactly_that(self):
        out, _warnings, branches = self.gate(
            "digest provenance: candidates=53 raw=4 schema-survivors=0 "
            "after-history-dedup=0 after-event-dedup=0 would-ship=0\nCOUNT=0")

        self.assertEqual(1, len(branches), branches)
        self.assertIn("All 4 model-written stories were rejected by validation "
                      "(0 of 4 passed the schema)", branches[0])
        self.assertNotIn("Only 0 of", out, "a zero survivor count is not 'only'")

    def test_nothing_survived_without_the_schema_failing_names_each_stage(self):
        """The shape the false sentence was printed for: 0 would ship, 0 rejected."""
        _out, _warnings, branches = self.gate(
            "digest provenance: candidates=40 raw=5 schema-survivors=5 "
            "after-history-dedup=5 after-event-dedup=5 would-ship=0\nCOUNT=0")

        self.assertEqual(1, len(branches), branches)
        self.assertIn("None of the 5 model-written stories would ship: 5 passed "
                      "validation, 0 were already sent within TTL, 0 duplicated "
                      "an event already reported today, and 5 fell below the "
                      "rank cap", branches[0])

    def test_no_stories_at_all_is_its_own_sentence(self):
        out, _warnings, branches = self.gate(
            "digest provenance: candidates=40 raw=0 schema-survivors=0 "
            "after-history-dedup=0 after-event-dedup=0 would-ship=0\nCOUNT=0")

        self.assertEqual(1, len(branches), branches)
        self.assertIn("The model walk returned no stories at all", branches[0])
        self.assertNotIn("All 0 model-written", out)

    def test_a_healthy_edition_warns_about_nothing_and_derives_nothing(self):
        out, warnings, _branches = self.gate(
            "digest provenance: candidates=53 raw=5 schema-survivors=5 "
            "after-history-dedup=4 after-event-dedup=4 would-ship=4\nCOUNT=4")

        self.assertEqual([], warnings, "a healthy edition raised an alarm: %r"
                         % (warnings,))
        self.assertIn("A model-written digest exists (4 of 5 stories survive "
                      "validation and dedup, floor 4)", out)
        self.assertFalse((self.tmp / "summary.md").exists(),
                         "the fallback heading was written for an edition that "
                         "shipped the model's digest")

    def test_the_neutral_floor_statement_is_written_once(self):
        out, warnings, _branches = self.gate(
            "digest provenance: candidates=53 raw=5 schema-survivors=5 "
            "after-history-dedup=3 after-event-dedup=3 would-ship=3\nCOUNT=3")
        neutral = [line for line in warnings
                   if line.startswith("::warning::No model-written digest met")]
        self.assertEqual(1, len(neutral),
                         "the floor statement is duplicated or missing: %r"
                         % (warnings,))


if __name__ == "__main__":
    unittest.main()
