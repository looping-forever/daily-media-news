"""tests/digest/test_simulate_pool_refusal.py -- the simulated no-AI path.

The unsummarised edition is the branch that only ever runs when the free pool is
refusing, so the one path that serves the owner during an outage was the one
path nobody could exercise. `simulate_pool_refusal` makes it deterministic and
costs ZERO model calls.

Three claims, and each of them can be false while the other two are true:

  * the flag EXISTS ONLY ON `workflow_dispatch`, so a cron can never set it;
  * the flag CANNOT BE HONOURED on any other event, even if it somehow arrives;
  * honouring it FAILS IN THE REAL SHAPE without invoking the agent -- an
    attempt row classified `quota_or_rate_limited`, a non-zero exit, and no
    `state/stories.json` -- so the derive step fires through its own condition
    rather than through a branch written for this test.

The last one is run against the SHIPPED `run:` block, not a copy of it.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests import workflows as W  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = "daily-news.yml"
STEP_NAME = "Run OpenCode agent"
INPUT_NAME = "simulate_pool_refusal"
EXPRESSION = "${{ github.event.inputs.%s }}" % INPUT_NAME

SIMULATED_LINE = "SIMULATED pool refusal \u2014 no model was contacted"
REFUSED_LINE = "simulate_pool_refusal=true arrived on event"


def _doc():
    return W.load_workflow(WORKFLOW)


def _agent_run_block():
    for _job, step, run, _step in W.iter_run_blocks(WORKFLOW):
        if step == STEP_NAME:
            return run
    raise AssertionError(
        "daily-news.yml has no `run:` block named %r. The model walk lives "
        "there; if it was renamed, point this suite at the new name "
        "deliberately." % STEP_NAME)


def _step(name):
    for _job, step, _run, sd in W.iter_run_blocks(WORKFLOW):
        if step == name:
            return sd
    raise AssertionError("daily-news.yml has no step %r" % name)


#: `opencode`, replaced by a real executable first on PATH. Its ONLY job is to
#: record that it was reached: the point of the simulation is that it is not.
_STUB_OPENCODE = r"""#!/usr/bin/env bash
printf 'invoked\n' >> "${STUB_OPENCODE_LOG:?}"
exit 0
"""

#: `python3`, replaced so the NON-dispatch path stays offline. The real selector
#: discovers the pool over the network, and a test that let it do so would be a
#: test that fails on a plane. Exit 1 with no output: the walk's own empty-ladder
#: branch then fires, which is exactly what this step does when discovery fails.
_STUB_PYTHON3 = r"""#!/usr/bin/env bash
exit 1
"""


class TheFlagExistsOnlyOnAManualDispatchTests(unittest.TestCase):
    def test_it_is_declared_under_workflow_dispatch_and_nowhere_else(self):
        doc = _doc()
        on = doc.get("on")
        self.assertIsInstance(on, dict, "daily-news.yml has no `on:` mapping")
        dispatch = on.get("workflow_dispatch")
        self.assertIsInstance(dispatch, dict,
                              "daily-news.yml has no `workflow_dispatch:` trigger")
        inputs = dispatch.get("inputs") or {}
        self.assertIn(INPUT_NAME, inputs,
                      "the %r input is gone. Without it the unsummarised "
                      "edition cannot be exercised at all while the free pool "
                      "is answering." % INPUT_NAME)

        declared = inputs[INPUT_NAME]
        self.assertEqual("boolean", str(declared.get("type")).strip(),
                         "the flag must be a boolean, so a dispatch carries a "
                         "value GitHub itself types, not a free string")
        self.assertEqual("false", str(declared.get("default")).strip().lower(),
                         "the default must be false: an input that defaults to "
                         "true would make every manual dispatch a simulated run")

        # A `schedule` entry is a mapping of `cron`, so an input there would be
        # dead YAML; the real risk is the flag appearing under some OTHER
        # trigger that could be handed a value.
        for trigger, body in on.items():
            if trigger == "workflow_dispatch":
                continue
            if isinstance(body, dict):
                self.assertNotIn(
                    INPUT_NAME, body,
                    "the %r input is declared under trigger %r. It must exist "
                    "ONLY on workflow_dispatch, or a trigger that is not a "
                    "human pressing Run can set it." % (INPUT_NAME, trigger))

    def test_every_read_of_it_comes_from_the_dispatch_event(self):
        """`github.event.inputs` is the only context a cron leaves EMPTY."""
        raw = W.raw_workflow_text(WORKFLOW)
        reads = [line.strip() for line in raw.splitlines()
                 if INPUT_NAME in line
                 and ("${{" in line)
                 and not line.strip().startswith("#")]
        self.assertTrue(reads, "nothing in %s reads the flag at all, so the "
                               "input is inert" % WORKFLOW)
        for line in reads:
            self.assertIn(
                EXPRESSION, line,
                "this line reads the flag without going through %r: %r. "
                "`github.event.inputs` is populated only for workflow_dispatch, "
                "which is half of what makes a cron unable to set it."
                % (EXPRESSION, line))
            self.assertNotIn("${{ vars.", line)
            self.assertNotIn("${{ env.", line)


class TheFlagCannotBeHonouredWithoutAManualDispatchTests(unittest.TestCase):
    """The declaration is structural; this is the runtime half."""

    def setUp(self):
        self.bash = W.bash_executable()
        if not self.bash:
            self.fail("bash is unavailable, so the shipped walk cannot be run. "
                      "This is a skip the suite refuses silently to make: the "
                      "claim below is about behaviour, not text.")
        self.tmp = Path(tempfile.mkdtemp(prefix="simulate-refusal-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "state").mkdir()
        bin_dir = self.tmp / "bin"
        W.install_stub_on_path(bin_dir, _STUB_OPENCODE, name="opencode")
        W.install_stub_on_path(bin_dir, _STUB_PYTHON3, name="python3")
        self.log = self.tmp / "opencode_calls.txt"
        self.log.write_text("", encoding="utf-8")
        self.env = {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "STUB_OPENCODE_LOG": str(self.log),
            "TODAY": "2026-10-28",
            "DIGEST_TIME": "morning",
            "CANDIDATES_PATH": str(self.tmp / "state" / "candidates.json"),
        }

    def run_walk(self, **overrides):
        env = dict(self.env)
        env.update(overrides)
        return W.run_bash_script(_agent_run_block(), env_overrides=env,
                                 cwd=self.tmp, bash=self.bash, timeout=120)

    def test_a_schedule_event_refuses_the_flag_and_walks_the_models(self):
        """THE PIN. The input cannot be set by a cron -- and if it ever is, the
        step says so and carries on as an ordinary run."""
        proc = self.run_walk(SIMULATE_POOL_REFUSAL="true", EVENT_NAME="schedule")
        self.assertIn(
            REFUSED_LINE, proc.stdout,
            "the step honoured -- or silently ignored -- the flag on a schedule "
            "event. It must REFUSE it loudly: a cron that fails the walk with no "
            "model call would serve an unsummarised edition on a day the pool "
            "was healthy. stdout:\n%s\nstderr:\n%s"
            % (proc.stdout, proc.stderr))
        self.assertNotIn(
            SIMULATED_LINE, proc.stdout,
            "the simulated refusal ran on a schedule event")
        # It went on to the ladder, which the stub python3 refuses, so the walk
        # ended in its own empty-ladder branch -- NOT in the simulation.
        self.assertNotEqual(0, proc.returncode,
                            "the walk must still end non-zero here")
        self.assertIn("empty model ladder", proc.stdout,
                      "the step did not continue into the real walk after "
                      "refusing the flag; stdout:\n%s" % proc.stdout)

    def test_a_manual_dispatch_honours_it_and_never_touches_the_agent(self):
        """ZERO MODEL CALLS, and the failure is the REAL one."""
        proc = self.run_walk(SIMULATE_POOL_REFUSAL="true",
                             EVENT_NAME="workflow_dispatch")
        self.assertIn(
            SIMULATED_LINE, proc.stdout,
            "the simulated refusal did not announce itself. A run that fails "
            "without saying it was simulated is indistinguishable from a real "
            "outage in every later log read. stdout:\n%s" % proc.stdout)
        self.assertNotEqual(
            0, proc.returncode,
            "the simulated refusal must exit non-zero: the derive step's "
            "reachability depends on the walk not reporting success")
        self.assertEqual(
            [], self.log.read_text(encoding="utf-8").split(),
            "the agent was INVOKED during a simulated refusal. The whole "
            "promise is that no model is contacted, so no discovery request "
            "and no `opencode run` may happen.")
        self.assertFalse(
            (self.tmp / "state" / "stories.json").exists(),
            "the simulated refusal wrote state/stories.json, so the derive "
            "step's HAVE check would succeed and the unsummarised edition "
            "would never be built")
        attempts = (self.tmp / "state" / "agent-attempts.log").read_text(
            encoding="utf-8")
        self.assertIn(
            "kind=quota_or_rate_limited", attempts,
            "the attempt row is the shape every later reader classifies from. "
            "It must carry the same class a genuine pool wall records, or the "
            "simulation proves a different path than the one it claims. "
            "Row(s):\n%s" % attempts)

    def test_the_short_circuit_precedes_the_ladder_and_the_agent(self):
        """Order in the shipped text: no network before the stop."""
        run = _agent_run_block()
        stop_at = run.find(SIMULATED_LINE)
        self.assertNotEqual(-1, stop_at, "the simulation's announcement is gone")

        def first_live_line(token):
            """The first NON-COMMENT occurrence, in the text that executes.

            `resolve_ladder`, `model_watchdog.py` and `opencode run` are all
            named in the commentary above the loop -- deliberately, because that
            is what the commentary is for -- so a bare `find` would measure a
            comment and report the guard as running after the network it is
            meant to precede.
            """
            for at in range(len(run)):
                if not run.startswith(token, at):
                    continue
                line_start = run.rfind("\n", 0, at) + 1
                if run[line_start:at].lstrip().startswith("#"):
                    continue
                return at
            return -1

        for later, why in (("resolve_ladder", "the selector's discovery request"),
                           ("opencode run", "the only model call in the step"),
                           ("model_watchdog.py", "the per-attempt wrapper")):
            later_at = first_live_line(later)
            self.assertNotEqual(-1, later_at,
                                "%r vanished from the walk's executable text"
                                % later)
            self.assertLess(
                stop_at, later_at,
                "the simulated stop sits AFTER %s, so the step would already "
                "have spent the very model call the simulation exists to avoid"
                % why)


class TheSimulatedPushAndSubjectAreLabelledTests(unittest.TestCase):
    """A simulation that pushed an unlabelled digest would be worse than none."""

    def test_the_push_title_and_body_carry_TEST(self):
        sd = _step("Publish to ntfy (primary)")
        run = sd["run"]
        self.assertIn("${{ github.event.inputs.%s }}" % INPUT_NAME,
                      str((sd.get("env") or {}).get("SIMULATE_POOL_REFUSAL")),
                      "the ntfy step no longer receives the flag, so the "
                      "TESTS-ONLY push it makes cannot be marked")
        self.assertIn("test_prefix = TEST_TITLE_PREFIX if simulate else ''", run,
                      "the push title no longer reserves the TEST marker, so a "
                      "simulated run's notification reaches the owner looking real")
        self.assertIn("**TEST", run,
                      "the push BODY is no longer marked TEST. The title is "
                      "truncated in the shade and the body is what is read "
                      "after a tap; both must say it.")

    def test_the_email_subject_is_composed_once_and_printed(self):
        sd = _step("Build email")
        run = sd["run"]
        self.assertIn("state/email_subject.txt", run,
                      "the subject is no longer written to a file, so the send "
                      "step is back to composing its own copy")
        self.assertIn("EMAIL SUBJECT:", run,
                      "the subject is no longer printed, so it cannot be "
                      "inspected on a run where the backup channel never fires")
        self.assertIn("unsummarised edition (no AI today)", run,
                      "the unsummarised tag is gone from the subject")
        gmail = _step("Send digest via Gmail (backup)")["run"]
        self.assertIn("state/email_subject.txt", gmail,
                      "the Gmail step does not read the composed subject")
        self.assertNotIn(
            "edition_tag = ", gmail,
            "the Gmail step composes the tag a second time. Two copies of the "
            "one string that decides how the owner reads the edition is how "
            "they came to disagree.")


if __name__ == "__main__":
    unittest.main()
