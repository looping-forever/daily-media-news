"""tests/digest/test_digest_slot_stamp.py -- the SLOT is stamped, not chosen.

THE DEFECT THIS PINS. On 2026-10-31 the evening dispatch built an edition whose
payload said `"digest_time": "morning"` -- the writer answered the prompt's
free "morning|evening" placeholder instead of the slot it was dispatched for.
Nothing between the writer and `story_state.mark_reported` overrode it, so the
three evening stories were recorded as `in_morning` for a date that had already
been delivered, the delivery dedupe read the edition as a duplicate of the
morning one, and the whole evening edition was swallowed with nothing sent.

The fix is one step that runs AFTER the writer and BEFORE every consumer, and
takes the slot from `DIGEST_TIME` rather than from the payload. These tests
assert the wiring (it exists, it sits in the right place, it is bound to the
dispatched slot) and the behaviour (it overwrites what the writer said, it is
idempotent, and it refuses rather than guesses when the slot is unknown).

Mutation-checked: deleting the step fails `test_the_step_exists...`; moving it
after validation fails `test_it_runs_after_...`; removing the `DIGEST_TIME`
binding fails `test_the_step_reads_the_dispatched_slot`; dropping the invalid
slot guard fails `test_an_unknown_slot_is_refused...`.
"""

import json
import os
import tempfile
import unittest

import tests.workflows as W

WORKFLOW = "daily-news.yml"
STAMP_STEP = "Stamp the digest slot from DIGEST_TIME"
AGENT_STEP = "Run OpenCode agent"
DERIVE_STEP = "Derive the unsummarised edition"
VALIDATE_STEP = "Validate stories"


def steps():
    return [(index, name, sd) for index, (_job, name, sd)
            in enumerate(W.iter_all_steps(WORKFLOW))]


def step_named(name):
    for index, step_name, sd in steps():
        if step_name == name:
            return index, sd
    raise AssertionError("daily-news.yml has no step %r. The slot stamping is "
                         "then gone and the writer's guess is back in charge; "
                         "update this suite deliberately rather than deleting "
                         "the check." % name)


def stamp_script():
    _index, sd = step_named(STAMP_STEP)
    return sd["run"]


def write_payload(root, payload):
    state = os.path.join(root, "state")
    os.makedirs(state, exist_ok=True)
    path = os.path.join(state, "stories.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def read_payload(root):
    path = os.path.join(root, "state", "stories.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TheStampIsWiredTests(unittest.TestCase):
    def test_the_step_exists_and_stamps_state_stories_json(self):
        run = stamp_script()
        self.assertIn("state/stories.json", run,
                      "the stamp step no longer touches state/stories.json, so "
                      "the payload the writer produced reaches validation and "
                      "mark_reported unmodified")
        self.assertIn("digest_time", run,
                      "the stamp step no longer writes a digest_time field")

    def test_it_runs_after_the_writer_and_derive_and_before_validation(self):
        stamp_index, _ = step_named(STAMP_STEP)
        for producer in (AGENT_STEP, DERIVE_STEP):
            producer_index, _ = step_named(producer)
            self.assertLess(
                producer_index, stamp_index,
                "%r runs AFTER the stamp step, so the slot is set before the "
                "payload exists and the writer's guess survives" % producer)
        validate_index, _ = step_named(VALIDATE_STEP)
        self.assertLess(
            stamp_index, validate_index,
            "the stamp step runs AFTER validation, so the slot it fixes is the "
            "one validation already read -- and mark_reported, which writes "
            "in_morning/in_evening, is still fed the writer's guess")

    def test_it_is_still_gated_on_the_fallback_noop(self):
        _index, sd = step_named(STAMP_STEP)
        self.assertEqual("env.FALLBACK_NOOP != 'true'", str(sd.get("if")),
                         "the stamp step lost its FALLBACK_NOOP gate, so it now "
                         "runs on a day where the whole digest is deliberately "
                         "skipped")

    def test_the_step_reads_the_dispatched_slot(self):
        _index, sd = step_named(STAMP_STEP)
        env = sd.get("env") or {}
        self.assertEqual("${{ steps.digest_time.outputs.time }}",
                         str(env.get("DIGEST_TIME")),
                         "the stamp step no longer binds DIGEST_TIME to the "
                         "slot this run was dispatched for, which is the only "
                         "source that cannot be wrong")


class TheStampRewritesWhatTheWriterSaidTests(unittest.TestCase):
    def _run(self, payload, digest_time):
        with tempfile.TemporaryDirectory() as root:
            write_payload(root, payload)
            env = {"DIGEST_TIME": digest_time}
            result = W.run_bash_script(stamp_script(), env_overrides=env, cwd=root)
            return result, read_payload(root)

    def test_a_morning_guess_on_an_evening_run_is_corrected(self):
        """The exact 2026-10-31 payload: writer said morning, run was evening."""
        result, payload = self._run(
            {"digest_time": "morning", "date": "2026-10-31",
             "stories": [{"title": "Tourists Stranded in Manang"}]},
            "evening")
        self.assertEqual(0, result.returncode,
                         "the stamp step failed on a payload it must correct: %s"
                         % (result.stderr or result.stdout))
        self.assertEqual("evening", payload["digest_time"],
                         "the writer's 'morning' survived an evening run, so "
                         "mark_reported files the edition under a slot that was "
                         "already delivered and the send is suppressed")
        self.assertEqual("2026-10-31", payload["date"],
                         "the stamp step altered the edition date; it owns the "
                         "slot only")
        self.assertEqual(1, len(payload["stories"]),
                         "the stamp step dropped stories; it must rewrite one "
                         "field of the payload and nothing else")
        self.assertIn("Slot correction", result.stdout,
                      "a correction happened silently, so a wrong slot in the "
                      "log can never be traced to this step")

    def test_an_evening_guess_on_a_morning_run_is_corrected_the_other_way(self):
        result, payload = self._run({"digest_time": "evening"}, "morning")
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        self.assertEqual("morning", payload["digest_time"])

    def test_a_correct_payload_is_left_alone(self):
        result, payload = self._run({"digest_time": "evening"}, "evening")
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        self.assertEqual("evening", payload["digest_time"])
        self.assertNotIn("Slot correction", result.stdout,
                         "the step reported a correction it did not make")

    def test_the_slot_is_normalised(self):
        result, payload = self._run({"digest_time": "morning"}, "  EVENING ")
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        self.assertEqual("evening", payload["digest_time"],
                         "the dispatched slot is not normalised, so a padded or "
                         "upper-case env value falls through to validation as an "
                         "unrecognised digest_time")


class TheStampRefusesRatherThanGuessesTests(unittest.TestCase):
    def _run(self, payload, digest_time, write_file=True):
        with tempfile.TemporaryDirectory() as root:
            if write_file:
                write_payload(root, payload)
            result = W.run_bash_script(
                stamp_script(), env_overrides={"DIGEST_TIME": digest_time}, cwd=root)
            return result, root

    def test_an_unknown_slot_is_refused_and_the_payload_is_untouched(self):
        for value in ("", "afternoon", "morning|evening"):
            with tempfile.TemporaryDirectory() as root:
                write_payload(root, {"digest_time": "morning"})
                result = W.run_bash_script(
                    stamp_script(), env_overrides={"DIGEST_TIME": value}, cwd=root)
                self.assertNotEqual(
                    0, result.returncode,
                    "DIGEST_TIME=%r was accepted as a slot. A run whose slot is "
                    "unknown must fail loudly, not pick one." % value)
                self.assertIn("DIGEST_TIME", result.stderr + result.stdout,
                              "the refusal does not name the variable that is "
                              "wrong")
                with open(os.path.join(root, "state", "stories.json"),
                          encoding="utf-8") as fh:
                    self.assertEqual("morning", json.load(fh)["digest_time"],
                                     "the payload was rewritten even though the "
                                     "slot was refused")

    def test_a_missing_payload_is_refused_by_name(self):
        result, _root = self._run({}, "evening", write_file=False)
        self.assertNotEqual(0, result.returncode,
                            "the step exited 0 with no state/stories.json to "
                            "stamp, so the missing payload reaches validation "
                            "and only a later step reports it")
        self.assertIn("state/stories.json", result.stderr + result.stdout)


# ---------------------------------------------------------------------------
# THE CLASS, NOT THE INSTANCE.
#
# The stamp above is one step in one workflow, and that is exactly the shape
# that fails: on 2026-10-31 the seal refused an evening artifact because the
# payload it carried said `digest_time="morning"`. A guard that exists at one
# producing site is not a guard of the class -- the next workflow that writes
# the payload would silently bypass it. These tests DISCOVER the producers from
# the workflow directory instead of naming them, so a new producer that forgets
# the stamp fails here rather than at the seal, hours later, with a slot that has
# already been spent.
#
# The directory is read, not `tests.workflows.WORKFLOW_NAMES`: that tuple is
# hardcoded, so a workflow nobody registers is one every check over it silently
# skips. Discovery is done on the RAW text (not the parsed doc) so a marker
# inside a heredoc or a YAML block scalar still counts.
# ---------------------------------------------------------------------------

#: Anything that WRITES `state/stories.json`. A consumer (`--in state/stories.json`,
#: a `-f` existence test, an `rm -f`) is deliberately not a marker: a validator
#: must not be forced to carry a producer's stamp.
PRODUCER_MARKERS = (
    "--out state/stories.json",       # scripts/derive_unsummarised.py
    "> state/stories.json",           # a shell redirect writing the payload
    "prompts/daily-digest.md",        # the agent, which writes the payload itself
)


def workflow_texts():
    """Every workflow file as (name, raw_text), read from the workflow directory."""
    out = []
    workflow_dir = str(W.WORKFLOW_DIR)
    for name in sorted(os.listdir(workflow_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(workflow_dir, name), encoding="utf-8") as fh:
            out.append((name, fh.read()))
    return out


def is_payload_producer(name, raw_text):
    """True when this workflow contains a step that produces the payload."""
    return any(marker in raw_text for marker in PRODUCER_MARKERS)


def carries_the_stamp(step):
    """The signature of a LOUD stamp: right file, dispatched slot, loud refusal."""
    run = str(step.get("run") or "")
    env = step.get("env") or {}
    return (
        "state/stories.json" in run
        and "digest_time" in run
        and "morning" in run
        and "evening" in run
        and "sys.exit" in run
        and str(env.get("DIGEST_TIME")) == "${{ steps.digest_time.outputs.time }}"
    )


class EveryPayloadProducerCarriesTheStampTests(unittest.TestCase):
    def producers(self):
        return {name: raw for name, raw in workflow_texts()
                if is_payload_producer(name, raw)}

    def test_the_discovery_finds_the_digest_pipeline(self):
        producers = self.producers()
        self.assertIn(
            "daily-news.yml", producers,
            "the producer discovery no longer finds daily-news.yml, so this "
            "suite would pass on an empty set and guard nothing. The markers "
            "in PRODUCER_MARKERS have drifted from the workflow.")
        self.assertNotIn(
            "digest-prebuild.yml", producers,
            "digest-prebuild.yml is now seen as a PRODUCER of state/stories.json. "
            "It only downloads and seals the payload daily-news.yml built "
            "(prebuild/state/stories.validated.json), so either the build really "
            "moved there -- in which case it must carry the stamp step -- or the "
            "marker set has become too loose.")

    def test_every_producer_has_one_loud_stamp_from_the_dispatched_slot(self):
        producers = self.producers()
        self.assertTrue(producers, "no payload producer was discovered at all")
        for name in sorted(producers):
            with self.subTest(workflow=name):
                stamped = [(idx, step_name) for idx, (_job, step_name, sd)
                           in enumerate(W.iter_all_steps(name))
                           if carries_the_stamp(sd)]
                self.assertEqual(
                    1, len(stamped),
                    "%s produces state/stories.json but carries %d loud stamp "
                    "step(s) bound to steps.digest_time.outputs.time. Without it "
                    "the writer's own `digest_time` reaches validation and "
                    "mark_reported, which is how an evening edition was filed as "
                    "morning. Found: %r" % (name, len(stamped), stamped))

    def test_the_stamp_runs_before_the_validator_in_every_producer(self):
        for name in sorted(self.producers()):
            with self.subTest(workflow=name):
                all_steps = [(idx, step_name, sd) for idx, (_job, step_name, sd)
                             in enumerate(W.iter_all_steps(name))]
                stamp_index = next(idx for idx, _n, sd in all_steps
                                   if carries_the_stamp(sd))
                # The VALIDATE INVOCATION, not a step that merely names the
                # script: daily-news.yml's "Verify required files exist" lists
                # `scripts/validate_stories.py` in a for-loop and consumes
                # nothing, so matching on the bare name orders the stamp against
                # the wrong step.
                validators = [(idx, step_name) for idx, step_name, sd in all_steps
                              if "--out state/stories.validated.json"
                              in str(sd.get("run") or "")]
                self.assertTrue(
                    validators,
                    "%s produces the payload but never invokes validate_stories.py "
                    "with an output, so there is no downstream site to order the "
                    "stamp against -- re-read that workflow before assuming the "
                    "stamp is enough" % name)
                for idx, step_name in validators:
                    self.assertLess(
                        stamp_index, idx,
                        "%s: the stamp runs after %r, so validation reads the "
                        "writer's slot and the corrected one never reaches it"
                        % (name, step_name))

    def test_the_stamp_is_not_exempted_by_the_build_phase(self):
        """A `phase` condition would exempt the prebuild -- the arm that failed."""
        for name in sorted(self.producers()):
            with self.subTest(workflow=name):
                for _idx, _step_name, sd in W.iter_all_steps(name):
                    if not carries_the_stamp(sd):
                        continue
                    condition = str(sd.get("if") or "")
                    self.assertNotIn(
                        "PHASE", condition,
                        "%s: the stamp is gated on the build phase, so the "
                        "prebuild arm -- the one whose artifact the seal "
                        "rejected -- is exempt" % name)


if __name__ == "__main__":
    unittest.main()
