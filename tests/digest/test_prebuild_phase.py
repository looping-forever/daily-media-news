#!/usr/bin/env python3
"""tests/digest/test_prebuild_phase.py -- the `phase` input on daily-news.yml.

WHAT IS BEING GUARDED
---------------------
`phase: prebuild` makes daily-news.yml build a slot's digest ahead of time
without notifying anybody. It is the smallest possible change to a pipeline
that already exists, and that is exactly why it is dangerous: the same run that
builds the page also writes `state/last_digest_sent.json`, and that file is the
input the 07:50 `Fallback no-op check` reads to decide whether to stand the
whole fallback ladder down.

    06:50 prebuild -> marker says delivered=true for a digest nobody was sent
                    -> 07:50 fallback reads it -> FALLBACK_NOOP=true
                    -> every job green, and the ladder never fires again

So the first test in this file is the highest-severity one, and it is written
to be shown RED by deleting a single line from the workflow.

WHY THESE ARE EXECUTED RATHER THAN READ
---------------------------------------
`delivered = (not dry_run) and (stories > 0) and (website_ok or ...) ` reads
perfectly well with `(not prebuild)` bolted on afterwards, and reading it is
what a reviewer does anyway. The force-false is only proved by running the
step's real Python with all four channel flags set to `true` -- the exact state
a prebuild is in, because the Pages deploy really did succeed -- and reading
the marker it wrote.

OFFLINE BY CONSTRUCTION
-----------------------
No network, no GitHub, no SMTP, no Telegram. The two step bodies executed here
return before their network calls or are never given one, `run_ntfy` poisons
`urlopen` and every write lands in a temp directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
for _p in (_TESTS, os.path.join(_TESTS, "render"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from render.test_render_honesty import (  # noqa: E402
    WORKFLOW, ntfy_source, run_ntfy)
from fixtures import validated_payload  # noqa: E402

from tests import workflows as W  # noqa: E402
from tests.workflows.test_workflow_lint import (  # noqa: E402
    heredoc_bodies)

#: The two literals `ntfy_source()` slices the workflow by. Duplicated from
#: tests/digest/test_literal_step_names.py rather than imported, because that
#: suite already pins them from the OUTSIDE; a second pin here is a guard on
#: the guard, and the two failing together is the signal.
NTFY_STEP = "- name: Publish to ntfy (primary)"
GMAIL_STEP = "- name: Send digest via Gmail (backup)"
TELEGRAM_STEP = "- name: Send Telegram digest (backup)"

PHASE_CHOICES = ("full", "prebuild")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def step_dict(step_name):
    for _job, name, sd in W.iter_all_steps("daily-news.yml"):
        if name == step_name:
            return sd
    raise AssertionError("daily-news.yml has no step %r" % step_name)


def workflow_doc():
    return W.load_workflow("daily-news.yml")


def dispatch_inputs():
    on = workflow_doc().get("on") or {}
    return (on.get("workflow_dispatch") or {}).get("inputs") or {}


def exec_body(body, env, cwd, capture_stderr=False):
    saved = dict(os.environ)
    os.environ.update({k: str(v) for k, v in env.items()})
    buf = io.StringIO()
    err = io.StringIO()
    code = 0
    cwd_before = os.getcwd()
    try:
        os.chdir(str(cwd))
        with contextlib.redirect_stdout(buf):
            # A refused step reports its FATAL on stderr, so a caller that wants
            # to read the refusal has to ask for it; everyone else keeps the
            # stream, because swallowing it would hide the very message a
            # failing run needs to show.
            with contextlib.redirect_stderr(buf if capture_stderr else err):
                try:
                    exec(compile(body, "<step>", "exec"), {"__name__": "step"})
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(cwd_before)
        os.environ.clear()
        os.environ.update(saved)
    if not capture_stderr:
        sys.stderr.write(err.getvalue())
    return code, buf.getvalue()


def gh_env(path):
    return dict(l.split("=", 1) for l in
                Path(path).read_text(encoding="utf-8").split("\n") if "=" in l)


def first_heredoc(step_name):
    """The step's first Python heredoc body.

    `heredoc_bodies` yields `(tag, body)` tuples, and a generator cannot be
    indexed; both mistakes are unwrapped in one place so neither can recur.
    """
    bodies = [body for _tag, body in heredoc_bodies(step_dict(step_name)["run"])]
    if not bodies:
        raise AssertionError(
            "step %r has no Python heredoc. The step was renamed, or its body "
            "was replaced with something this suite can no longer execute -- "
            "update the suite deliberately rather than deleting the check."
            % step_name)
    return bodies[0]


def python3_on_path(tmp):
    """A PATH with a `python3` shim onto this interpreter, for `tmp`.

    The SCHEDULE branch of the phase step runs a real Python heredoc (it has to:
    the slot date is derived from the scheduled firing there), and a Windows box
    has no `python3` to run it with -- `python` is a Store shim that exits 9009.
    The same shim the other suites in this repository install, and the reason a
    step that used to be pure shell needed none.

    A shim rather than a skip: a skipped scheduled-run test would leave the
    schedule path -- the one that mislabelled the archive -- unexercised, with
    the suite still reporting green.
    """
    bin_dir = Path(tmp) / "bin"
    W.install_stub_on_path(
        bin_dir, '#!/usr/bin/env bash\nexec "%s" "$@"\n' % sys.executable,
        name="python3")
    return "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))


def run_step_as_bash(step_name, env_overrides, cwd, expressions=None):
    """Execute a step's `run:` block the way Actions would.

    The `phase` step is SHELL, not a Python heredoc, so it has to be run as
    shell or it is not being tested at all. `${{ ... }}` expressions in the block
    are substituted first from `expressions`, which is precisely the step GitHub
    performs before handing the string to bash -- and skipping it would mean
    the literal text `${{ github.event.schedule }}` is what the shell compares
    against, which tests nothing.
    """
    run = step_dict(step_name)["run"]
    for expression, value in (expressions or {}).items():
        run = run.replace("${{ %s }}" % expression, value)
    proc = W.run_bash_script(run, env_overrides=env_overrides, cwd=str(cwd))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="phase-test-")
        self.tmp = Path(self._tmp)
        self.addCleanup(shutil.rmtree, self._tmp, True)


# --------------------------------------------------------------------------
# A real evaluator for the `if:` expressions under test.
#
# Asserting that a backup step's condition CONTAINS the string
# `env.PHASE != 'prebuild'` proves the term was typed. It does not prove the
# step does not fire. This evaluates the expression the way Actions does, for
# the subset of the grammar these steps use, so the test can assert BEHAVIOUR:
# "with PHASE=prebuild and ntfy_ok=false, this step does not run".
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""\s*(?:
    (?P<op>==|!=|\|\||&&|!)
  | (?P<str>'[^']*'|"[^"]*")
  | (?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)
  | (?P<lp>\()
  | (?P<rp>\))
)""", re.VERBOSE)


def _tokenize(text):
    out, pos = [], 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise AssertionError("cannot tokenize %r at %d" % (text, pos))
        pos = m.end()
        if m.group("op"):
            out.append(("op", m.group("op")))
        elif m.group("str"):
            out.append(("str", m.group("str")[1:-1]))
        elif m.group("name"):
            out.append(("name", m.group("name")))
        elif m.group("lp"):
            out.append(("op", "("))
        else:
            out.append(("op", ")"))
    return out


def _parse_or(tokens, i):
    value, i = _parse_and(tokens, i)
    while i < len(tokens) and tokens[i] == ("op", "||"):
        rhs, i = _parse_and(tokens, i + 1)
        value = value or rhs
    return value, i


def _parse_and(tokens, i):
    value, i = _parse_unary(tokens, i)
    while i < len(tokens) and tokens[i] == ("op", "&&"):
        rhs, i = _parse_unary(tokens, i + 1)
        value = value and rhs
    return value, i


def _parse_unary(tokens, i):
    if tokens[i] == ("op", "!"):
        value, i = _parse_unary(tokens, i + 1)
        return (not value), i
    if tokens[i] == ("op", "("):
        value, i = _parse_or(tokens, i + 1)
        assert tokens[i] == ("op", ")"), "unbalanced parentheses"
        return value, i + 1
    left = tokens[i]
    assert left[0] == "name", "expected a name, got %r" % (left,)
    i += 1
    if i < len(tokens) and tokens[i][0] == "op" and tokens[i][1] in ("==", "!="):
        op = tokens[i][1]
        right = tokens[i + 1]
        assert right[0] == "str", "expected a quoted literal, got %r" % (right,)
        # The COMPARISON uses the value. This is the distinction that matters:
        # `env.PHASE != 'prebuild'` with PHASE=prebuild must be False, and
        # comparing bool('prebuild') to the string 'prebuild' would say True.
        got = _value(left[1])
        want = right[1]
        i += 2
        return (got == want) if op == "==" else (got != want), i
    return _truthy(left[1]), i


#: The context the expressions are evaluated against.
_CONTEXT = {}


def _value(name):
    """A context entry's VALUE. Used on both sides of a comparison."""
    return _CONTEXT.get(name, "")


def _truthy(name):
    """Actions evaluates a BARE context name as a boolean: present and truthy.

    An absent name is false, which is why `env.FALLBACK_NOOP` alone reads as
    "not a no-op run" and the existing conditions lean on that.
    """
    return bool(_CONTEXT.get(name, ""))


def evaluate_if(expression, context):
    """Evaluate a step `if:` against a context dict. Returns a bool."""
    global _CONTEXT
    saved = _CONTEXT
    _CONTEXT = context
    try:
        tokens = _tokenize(expression)
        value, i = _parse_or(tokens, 0)
        assert i == len(tokens), "trailing tokens in %r" % expression
        return bool(value)
    finally:
        _CONTEXT = saved


# ==========================================================================
# THE HIGHEST-SEVERITY TEST.
# ==========================================================================

#: The two independent halves of the prebuild rule, as REMOVALS. Module level
#: because the control test needs both of them at once, and a local closure in
#: one method is not reachable from another.
DROP_DELIVERED_TERM = (
    "delivered = (not dry_run) and (not prebuild) and (stories > 0) and (",
    "delivered = (not dry_run) and (stories > 0) and (")
DROP_CHANNEL_FORCE = ("if prebuild:", "if False:")


def _drop(text, pair):
    old, new = pair
    return text.replace(old, new), text.count(old)


class PrebuildNeverRecordsDeliveryTests(TempCase):
    """A prebuild must never seal a delivered marker.

    Deleting `if prebuild:` / `and (not prebuild)` from "Record delivery
    status" turns this red, which is what makes it a guard rather than a
    description.
    """

    STEP = "Record delivery status"

    def _run_marker(self, phase, flags, stories=12, dry_run="false",
                    mutate=None, expect_hits=1):
        body = first_heredoc(self.STEP)
        if mutate is not None:
            body, hits = mutate(body)
            self.assertEqual(expect_hits, hits,
                             "the mutation target matched %d time(s) in the "
                             "marker step's body, expected %d. The workflow has "
                             "drifted from the text this proof was written "
                             "against." % (hits, expect_hits))
        state = self.tmp / "state"
        state.mkdir(exist_ok=True)
        for name, value in flags.items():
            (state / (".%s_ok" % name)).write_text(value, encoding="utf-8")
        (state / "stories.validated.json").write_text(json.dumps({
            "stories": [{"title": "s%d" % i} for i in range(stories)],
            "stories_outcome": "produced" if stories else "empty",
            "source_health": {"state": "ok"},
        }), encoding="utf-8")
        out = self.tmp / ("gh_env_%s_%d.txt" % (phase or "blank", id(mutate)))
        out.write_text("", encoding="utf-8")
        summary = self.tmp / "summary.md"
        summary.write_text("", encoding="utf-8")
        code, text = exec_body(body, {
            "GITHUB_ENV": str(out),
            "GITHUB_STEP_SUMMARY": str(summary),
            "DIGEST_TIME": "morning",
            "TODAY": "2026-10-09",
            "DRY_RUN": dry_run,
            "PHASE": phase,
            "TZ": "Asia/Kathmandu",
        }, self.tmp)
        marker_path = state / "last_digest_sent.json"
        marker = (json.loads(marker_path.read_text(encoding="utf-8"))
                  if marker_path.is_file() else None)
        return code, text, marker

    # Every channel green, and stories present: the exact state of a prebuild,
    # because the Pages deploy really did publish the page.
    ALL_GREEN = {"website": "true", "ntfy": "true",
                 "gmail": "true", "telegram": "true"}

    def test_a_prebuild_never_writes_delivered_true(self):
        code, out, marker = self._run_marker("prebuild", self.ALL_GREEN)
        self.assertEqual(0, code, out)
        self.assertIsNotNone(marker, "no marker was written:\n%s" % out)
        self.assertIs(
            marker["delivered"], False,
            "A PREBUILD SEALED A DELIVERED MARKER. state/last_digest_sent.json "
            "now claims delivered=true for a digest the owner was NEVER sent. "
            "The 07:50 `Fallback no-op check` reads that field, arms "
            "FALLBACK_NOOP and stands the entire fallback ladder down -- with "
            "every job in this repository green. Marker: %r" % (marker,))

    def test_either_half_of_the_prebuild_rule_alone_is_sufficient(self):
        """THE PROOF THE BRIEF ASKS FOR, made precise.

        "Deleting the force-false must turn this red" is only a meaningful
        demand if deleting it would actually produce a delivered marker. Today
        it would NOT, because the rule has TWO independent halves: every channel
        flag is forced false, AND `delivered` carries `(not prebuild)`. Either
        alone holds the line.

        That redundancy is deliberate -- this is the highest-severity rule in
        the design -- but it also means neither half can be shown load-bearing
        by deleting it from the committed file. So each half is removed HERE,
        from a copy of the body, and the OTHER half is required to hold the
        line on its own. If either half ever stops holding it, this goes red
        and the redundancy is reported for what it has become: one line of
        defence wearing a comment that claims two.
        """
        def drop_delivered_term(body):
            return _drop(body, DROP_DELIVERED_TERM)

        def drop_channel_force(body):
            return _drop(body, DROP_CHANNEL_FORCE)

        for label, mutate in (("the (not prebuild) term", drop_delivered_term),
                              ("the channel-flag force", drop_channel_force)):
            with self.subTest(removed=label):
                code, out, marker = self._run_marker(
                    "prebuild", self.ALL_GREEN, mutate=mutate)
                self.assertEqual(0, code, out)
                self.assertIs(
                    marker["delivered"], False,
                    "removing %s let a prebuild write delivered=true. The other "
                    "half of the rule did NOT hold the line, so this step has "
                    "ONE defence where it reads as having two. Marker: %r"
                    % (label, marker))

    def test_removing_both_halves_really_would_seal_delivery(self):
        """The control for the test above. Without it that test could pass for
        the wrong reason -- e.g. if `delivered` were false for some unrelated
        reason in every prebuild, and the two halves were doing nothing."""
        def drop_both(body):
            a, na = _drop(body, DROP_DELIVERED_TERM)
            b, nb = _drop(a, DROP_CHANNEL_FORCE)
            return b, na + nb

        _code, _out, marker = self._run_marker("prebuild", self.ALL_GREEN,
                                               mutate=drop_both, expect_hits=2)
        self.assertIs(
            marker["delivered"], True,
            "with BOTH halves removed the marker still says delivered=false, so "
            "the halves above are not what is protecting it and this section "
            "proves nothing. Marker: %r" % (marker,))

    def test_the_marker_says_which_phase_produced_it(self):
        _code, _out, marker = self._run_marker("prebuild", self.ALL_GREEN)
        self.assertEqual("prebuild", marker["phase"],
                         "the marker must name the phase that produced it, or "
                         "'delivered=false' is indistinguishable from every "
                         "other reason a marker is false. Marker: %r" % (marker,))
        _code, _out, full = self._run_marker("full", self.ALL_GREEN)
        self.assertEqual("full", full["phase"])

    def test_a_prebuild_forces_every_channel_flag_false(self):
        """Not just `delivered`. `website_ok` is a fact about the SITE, and a
        prebuild really does publish the page -- but the owner was not told, so
        no channel delivered anything to them. A marker that said
        website=true/delivered=false would still read as "the page is up" to
        every consumer that does not know this history."""
        _code, _out, marker = self._run_marker("prebuild", self.ALL_GREEN)
        self.assertEqual({"website": False, "ntfy": False,
                          "gmail": False, "telegram": False},
                         marker["channels"],
                         "a prebuild must record no channel as delivered:\n%r"
                         % (marker["channels"],))

    def test_a_prebuild_is_not_recorded_as_a_dry_run(self):
        """The two flags mean different things and both are in the marker.

        `dry_run: true` means nothing was published anywhere. A prebuild DID
        publish -- the page, the archive and the state commit all ran, because
        that work is the prebuild's product. Recording it as a dry run would be
        a different kind of lie, and it would also switch off the
        `dry_run` term in the fallback no-op check for no reason."""
        _code, _out, marker = self._run_marker("prebuild", self.ALL_GREEN)
        self.assertIs(marker["dry_run"], False,
                      "a prebuild deployed the page and committed the archive; "
                      "recording it as a dry run is a different lie. Marker: %r"
                      % (marker,))

    def test_the_force_is_load_bearing_and_not_a_blanket(self):
        """THE MUTATION, in one test: the same flags, the same stories, one
        variable changed. If the `full` case were ALSO delivered=false, the
        prebuild rule would be a blanket that has quietly broken delivery."""
        _c, _o, pre = self._run_marker("prebuild", self.ALL_GREEN)
        _c, _o, full = self._run_marker("full", self.ALL_GREEN)
        self.assertIs(pre["delivered"], False)
        self.assertIs(full["delivered"], True,
                      "a full run with four green channels and 12 stories must "
                      "still record delivered=true. If it does not, the prebuild "
                      "rule has been implemented as something that stops "
                      "delivery. Marker: %r" % (full,))

    def test_a_full_run_still_records_its_channels(self):
        _c, _o, marker = self._run_marker("full", self.ALL_GREEN)
        self.assertEqual({"website": True, "ntfy": True,
                          "gmail": True, "telegram": True}, marker["channels"])

    def test_a_dry_run_still_forces_everything_false(self):
        """The pre-existing rule must survive the new one."""
        _c, _o, marker = self._run_marker("full", self.ALL_GREEN,
                                          dry_run="true")
        self.assertIs(marker["delivered"], False)
        self.assertIs(marker["dry_run"], True)
        self.assertEqual(False, marker["channels"]["ntfy"])

    def test_an_empty_prebuild_is_still_empty(self):
        _c, _o, marker = self._run_marker("prebuild", self.ALL_GREEN, stories=0)
        self.assertIs(marker["delivered"], False)
        self.assertIs(marker["empty_digest"], True)

    def test_an_unrecognised_phase_is_treated_as_full_not_as_prebuild(self):
        """The step is read on its own by this suite and by a human debugging a
        marker. An absent or misspelled PHASE must land on `full`, which is the
        direction where the earlier step's validation has already had its say."""
        body = first_heredoc(self.STEP)
        state = self.tmp / "state"
        state.mkdir(exist_ok=True)
        for name, value in self.ALL_GREEN.items():
            (state / (".%s_ok" % name)).write_text(value, encoding="utf-8")
        (state / "stories.validated.json").write_text(json.dumps({
            "stories": [{"title": "s"}], "stories_outcome": "produced"}),
            encoding="utf-8")
        out = self.tmp / "env_junk.txt"
        out.write_text("", encoding="utf-8")
        summary = self.tmp / "summary_junk.md"
        summary.write_text("", encoding="utf-8")
        saved = dict(os.environ)
        os.environ.update({
            "GITHUB_ENV": str(out), "GITHUB_STEP_SUMMARY": str(summary),
            "DIGEST_TIME": "morning", "TODAY": "2026-10-09", "DRY_RUN": "false",
            "PHASE": "PREBUILD-BUT-TYPO", "TZ": "Asia/Kathmandu"})
        cwd = os.getcwd()
        buf = io.StringIO()
        try:
            os.chdir(str(self.tmp))
            with contextlib.redirect_stdout(buf):
                exec(compile(body, "<step>", "exec"), {"__name__": "step"})
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        marker = json.loads((state / "last_digest_sent.json").read_text(
            encoding="utf-8"))
        self.assertIs(marker["delivered"], True,
                      "a mistyped phase must not be read as a prebuild and "
                      "silently swallow a real delivery. Marker: %r" % (marker,))
        self.assertEqual("prebuild-but-typo", marker["phase"])


# ==========================================================================
# The input, and its promotion to the environment.
# ==========================================================================

class PhaseInputTests(unittest.TestCase):
    def test_phase_is_a_choice_of_exactly_two_values(self):
        spec = dispatch_inputs().get("phase")
        self.assertIsNotNone(spec,
                             "daily-news.yml declares no `phase` input, so "
                             "digest-prebuild.yml cannot ask for a prebuild "
                             "through the dispatch API.")
        self.assertEqual("choice", spec["type"])
        self.assertEqual(list(PHASE_CHOICES), list(spec["options"]))
        self.assertEqual("full", spec["default"],
                         "a dispatch that omits `phase` must get a delivery "
                         "build. defaulting to prebuild would silently stop "
                         "delivering the digest.")

    def test_the_phase_step_binds_the_input_and_exports_both_values(self):
        step = step_dict("Determine date, digest time and trigger source")
        env = " ".join("%s: %s" % kv for kv in (step.get("env") or {}).items())
        self.assertIn("INPUT_PHASE", env,
                      "the step must bind github.event.inputs.phase into its "
                      "environment, or it validates nothing")
        run = step["run"]
        self.assertIn("PHASE=", run)
        self.assertIn("NTFY_DRY_RUN=", run)


class PhasePromotionTests(TempCase):
    """EXECUTED: the truth table that decides whether anything is sent."""

    STEP = "Determine date, digest time and trigger source"

    def _promote(self, phase, dry_run="", digest_time="morning",
                 event="workflow_dispatch", schedule="", slot_date="2026-10-09"):
        out = self.tmp / ("gh_%s_%s_%s.txt" % (phase, dry_run, digest_time))
        out.write_text("", encoding="utf-8")
        code, text = run_step_as_bash(
            self.STEP,
            {
                "GITHUB_OUTPUT": str(out),
                "GITHUB_ENV": str(out),
                "EVENT_NAME": event,
                # Bound as an env var, not interpolated into the script text:
                # the step reads `github.event.schedule` from its `env:` block
                # so the slot NAME and the slot DATE come from one string.
                "EVENT_SCHEDULE": schedule,
                # A DISPATCH MUST NAME ITS DATE (2026-10-26), so the dispatch
                # arm needs one here; a scheduled run has no inputs at all.
                "INPUT_SLOT_DATE": slot_date if event == "workflow_dispatch" else "",
                "INPUT_DIGEST_TIME": digest_time,
                "INPUT_TRIGGER_SOURCE": "prebuild",
                "INPUT_DRY_RUN": dry_run,
                "INPUT_RETRY_SLOT": "",
                "INPUT_RETRY_OF": "",
                "INPUT_PHASE": phase,
                "PATH": python3_on_path(self.tmp),
            },
            self.tmp,
            expressions={"github.event.schedule": schedule,
                         "github.event_name": event},
        )
        return code, text, gh_env(out)

    def test_the_truth_table(self):
        for phase, dry_run, expected in (
                ("prebuild", "", "true"),     # a prebuild NEVER notifies
                ("prebuild", "true", "true"),
                ("full", "true", "true"),      # an explicit dry run still does
                ("full", "false", "false"),
                ("full", "", "false"),        # a normal delivery build
                ("", "", "false"),            # no phase at all -> full
        ):
            with self.subTest(phase=phase, dry_run=dry_run):
                code, out, env = self._promote(phase, dry_run)
                self.assertEqual(0, code, out)
                self.assertEqual(expected, env.get("NTFY_DRY_RUN"),
                                 "phase=%r dry_run=%r resolved NTFY_DRY_RUN=%r; "
                                 "expected %r. stdout:\n%s"
                                 % (phase, dry_run, env.get("NTFY_DRY_RUN"),
                                    expected, out))
                self.assertEqual(phase or "full", env.get("PHASE"))

    def test_an_invalid_phase_is_fatal(self):
        """Like digest_time: a typo must stop the run, not quietly mean `full`.
        A silent fallback to `full` would notify the owner from the arm that
        exists to be silent, twice a day, with every job green."""
        code, out, _env = self._promote("prebild")
        self.assertNotEqual(0, code,
                            "an invalid phase was accepted. stdout:\n%s" % out)
        self.assertIn("FATAL", out)
        self.assertIn("invalid phase", out)

    def test_a_prebuild_does_not_set_dry_run(self):
        """DRY_RUN gates the Pages deploy and the state commit. A prebuild
        needs both -- they are its product -- so it must not be set."""
        _c, _o, pre = self._promote("prebuild")
        self.assertEqual("false", pre.get("DRY_RUN"),
                         "a prebuild set DRY_RUN=true, which would skip the "
                         "deploy and the state commit and leave nothing to hand "
                         "over. Env: %r" % pre)
        self.assertEqual("true", pre.get("NTFY_DRY_RUN"))

    def test_a_scheduled_run_is_a_full_build(self):
        """A cron firing has no inputs, so `phase` is absent and the run is a
        delivery build. That is what keeps the existing 07:50/19:50 fallback
        behaviour untouched by this change."""
        code, out, env = self._promote("", event="schedule",
                                       schedule="5 2 * * *")
        self.assertEqual(0, code, out)
        self.assertEqual("full", env.get("PHASE"))
        self.assertEqual("false", env.get("NTFY_DRY_RUN"))


# ==========================================================================
# DEFECT #4 (HIGH, 2026-10-10): the slot's DATE came from the wall clock while
# the slot's NAME came from the cron.
#
# Run 38076000974 started at 2026-10-10T18:29:48Z -- 00:29 NPT on the 11th --
# and reported "TODAY: 2026-10-11 / DIGEST_TIME: evening", rendering
# docs/digests/2026-10-11-evening.html for the 10th's evening slot. The watchdog
# then demanded a 2026-10-10-evening record that can never exist, because the
# only evening digest in existence had been filed under the wrong day.
#
# The rule: a slot is the SCHEDULED slot -- an (NPT date, morning|evening) pair
# with both halves from the same firing -- which is the anchoring
# digest-watchdog.yml's digest_due() already uses. Delay moves the job; it does
# not renumber it.
# ==========================================================================

class SlotDateTests(TempCase):
    """The step's real date-derivation, executed at chosen instants."""

    STEP = "Determine date, digest time and trigger source"

    def resolve(self, schedule, now_iso, slot_date=None):
        """The date the step's heredoc prints at `now_iso`, for `schedule`.

        `slot_date` is the DISPATCHER's input when one is supplied. Leaving it
        out is the date-less dispatch, which must fall back to the slot's own
        firing rather than to the clock.
        """
        body = first_heredoc(self.STEP)
        env = {"EVENT_SCHEDULE": schedule}
        if slot_date is not None:
            env["INPUT_SLOT_DATE"] = slot_date
        with W.frozen_datetime(now_iso):
            code, text = exec_body(body, env, self.tmp)
        return code, text.strip()

    def test_a_build_after_midnight_npt_keeps_the_PRIOR_date(self):
        """THE NAMED REQUIREMENT, at the exact instant it actually happened.

        2026-10-10T18:29:48Z is 00:29 NPT on the 11th -- the instant run
        38076000974 started, and the one that produced 2026-10-11-evening for
        the 10th's evening slot.
        """
        for cron in ("5 2 * * *", "5 14 * * *"):
            with self.subTest(cron=cron):
                code, got = self.resolve(cron, "2026-10-10T18:29:48Z")
                self.assertEqual(0, code, got)
                self.assertEqual(
                    "2026-10-10", got,
                    "a build that started after midnight NPT filed the "
                    "previous day's evening slot under the NEW date. This is "
                    "run 38076000974: it rendered "
                    "docs/digests/2026-10-11-evening.html for the 10th's slot, "
                    "and the watchdog then demanded a 2026-10-10-evening "
                    "record that can never exist.")

    def test_a_build_just_after_midnight_npt_keeps_the_PRIOR_date(self):
        """The edge itself: one minute into the new day, not half an hour."""
        code, got = self.resolve("5 14 * * *", "2026-10-10T18:16:00Z")
        self.assertEqual(0, code, got)
        self.assertEqual("2026-10-10", got)

    def test_a_punctual_build_is_unaffected(self):
        """The fix must not move a healthy run: 19:55 NPT on the 10th for the
        19:50 slot is still the 10th."""
        code, got = self.resolve("5 14 * * *", "2026-10-10T14:10:00Z")
        self.assertEqual(0, code, got)
        self.assertEqual("2026-10-10", got)

    def test_both_crons_are_anchored_to_their_OWN_firing(self):
        """Both crons, at two instants where they must DISAGREE.

        `5 2 * * *` is 07:50 NPT and `5 14 * * *` is 19:50 NPT, so there is a
        window after the morning firing and before the evening one in which the
        two most-recent firings belong to DIFFERENT days. A single "today"
        cannot produce both answers, which is exactly the point: each slot is
        resolved from its own firing, not from a clock the two share.
        """
        # 07:15 NPT on the 11th. The 11th's morning firing (02:05 UTC) has not
        # happened yet, so BOTH slots are still the 10th's.
        for cron in ("5 2 * * *", "5 14 * * *"):
            with self.subTest(at="07:15 NPT", cron=cron):
                code, got = self.resolve(cron, "2026-10-11T01:30:00Z")
                self.assertEqual(0, code, got)
                self.assertEqual("2026-10-10", got)
        # 08:15 NPT on the 11th. The morning slot has rolled over and the
        # evening slot has not.
        code, got = self.resolve("5 2 * * *", "2026-10-11T02:30:00Z")
        self.assertEqual(0, code, got)
        self.assertEqual("2026-10-11", got,
                         "the 07:50 NPT morning slot on the 11th was due at "
                         "02:05 UTC, so it belongs to the 11th: %r" % got)
        code, got = self.resolve("5 14 * * *", "2026-10-11T02:30:00Z")
        self.assertEqual(0, code, got)
        self.assertEqual("2026-10-10", got,
                         "the most recent EVENING slot at 08:15 NPT on the 11th "
                         "is still the 10th's: %r" % got)

    def test_an_unusable_schedule_fails_closed_rather_than_using_the_clock(self):
        """Deriving the date from the firing is only worth anything if there is
        no longer a path on which the clock can still supply it. A cron this
        cannot reason about must stop the run, not fall back."""
        for bad in ("5 13 1 * *", "5 13 * 1 *", "5 13 * * 1", "nonsense", ""):
            with self.subTest(schedule=bad):
                code, got = self.resolve(bad, "2026-10-10T14:10:00Z")
                self.assertNotEqual(0, code,
                                    "schedule %r was accepted and a date was "
                                    "invented for it: %r" % (bad, got))

    def test_the_step_still_runs_and_still_exports_today(self):
        """End to end through the SHELL, on the real clock.

        The clock cannot be pinned through bash, so this asserts the step still
        executes and still promotes TODAY -- that the `$( ... <<'PY' ... PY )`
        wrapper around the new heredoc is valid shell and the downstream
        consumers of `$today` still get their value. The exact dates are pinned
        by the tests above, which drive the heredoc itself.
        """
        out = self.tmp / "gh_env.txt"
        out.write_text("", encoding="utf-8")
        code, text = run_step_as_bash(
            self.STEP,
            {"GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(out),
             "EVENT_NAME": "schedule", "EVENT_SCHEDULE": "5 14 * * *",
             "INPUT_DIGEST_TIME": "", "INPUT_TRIGGER_SOURCE": "",
             "INPUT_DRY_RUN": "", "INPUT_RETRY_SLOT": "", "INPUT_RETRY_OF": "",
             "INPUT_PHASE": "", "PATH": python3_on_path(self.tmp)},
            self.tmp,
            expressions={"github.event.schedule": "5 14 * * *"},
        )
        self.assertEqual(0, code, text)
        env = gh_env(out)
        self.assertRegex(env.get("TODAY", ""), r"^\d{4}-\d{2}-\d{2}$",
                         "TODAY is not a date the rest of the workflow can "
                         "use: %r\n%s" % (env.get("TODAY"), text))

    def test_a_supplied_slot_date_wins_over_the_clock(self):
        """THE DISPATCHER'S DATE IS OBEYED, whatever the runner's clock says.

        2026-10-17T18:16:53Z is 00:01 NPT on the 18th. A dispatch that names the
        17th means the 17th, on both sides of Nepal midnight -- and it means the
        17th even when the clock happens to agree, so that no part of this can
        be satisfied by the clock.
        """
        for now in ("2026-10-17T18:16:53Z", "2026-10-18T14:05:00Z"):
            with self.subTest(now=now):
                code, got = self.resolve("5 14 * * *", now,
                                         slot_date="2026-10-17")
                self.assertEqual(0, code, got)
                self.assertEqual(
                    "2026-10-17", got,
                    "a dispatcher named 2026-10-17 and the step filed the slot "
                    "under %r instead. A supplied date is the whole point of the "
                    "input: the clock is not a source the dispatch path may use."
                    % got)

    def test_a_date_less_dispatch_cannot_claim_the_new_day(self):
        """THE OBSERVED DAMAGE, at the exact instant it happened.

        Run 2026-10-17T18:16:53Z -- 00:01 NPT on the 18th -- was dispatched for
        `evening` with no date. The wall clock said the 18th, so the 18th's
        evening digest was pushed 19 hours before its slot. The slot's OWN
        firing (5 14 * * *, 14:05 UTC on the 17th) is the 17th, and that is what
        must win.
        """
        code, got = self.resolve("5 14 * * *", "2026-10-17T18:16:53Z")
        self.assertEqual(0, code, got)
        self.assertEqual(
            "2026-10-17", got,
            "a date-less dispatch just after Nepal midnight claimed %r for the "
            "evening slot. That is run 2026-10-17T18:16:53Z, which pushed the "
            "18th's evening digest 19 hours before its slot." % got)
        # The morning slot at the same instant is also still the 17th: its own
        # firing that day was 02:05 UTC, seven hours before this run started.
        code, got = self.resolve("5 2 * * *", "2026-10-17T18:16:53Z")
        self.assertEqual(0, code, got)
        self.assertEqual(
            "2026-10-17", got,
            "the morning slot was dated %r at an instant whose most recent "
            "07:50 NPT firing is still the 17th" % got)

    def test_the_wall_clock_call_is_gone_from_the_step(self):
        """The mechanism is DELETED, not merely bypassed.

        `TZ=Asia/Kathmandu date +%F` was the defect in one line: it named the
        build's own day. Leaving that call in the script behind a branch would
        leave the defect reachable by the next edit, so its absence is asserted
        rather than its behaviour.
        """
        run = step_dict(self.STEP)["run"]
        self.assertNotIn(
            "date +%F", run,
            "the step still reads the wall clock for the slot date, which is how "
            "a dispatch accepted before Nepal midnight gets filed under the day "
            "it started on")
        self.assertIn("slot_date_from_input", run,
                      "the step has no `slot_date` handling at all, so no "
                      "dispatcher can name the slot it means")

    def test_a_malformed_slot_date_is_fatal(self):
        """A date that is not a date STOPS THE RUN; it never falls back.

        A silent fallback here is worse than no date at all, because the only
        thing to fall back to is the clock -- which is the defect.
        """
        for bad in ("2026-2-3", "2026-02-30", "2026-13-01", "17-10-2026",
                    "2026/10/17", "2026-10-17T00:00:00Z", "yesterday"):
            with self.subTest(slot_date=bad):
                code, got = self.resolve("5 14 * * *", "2026-10-18T14:05:00Z",
                                         slot_date=bad)
                self.assertNotEqual(
                    0, code,
                    "slot_date %r was accepted. A malformed date must be FATAL: "
                    "the alternative is filing the digest under the build date, "
                    "which is the defect itself." % bad)
                self.assertEqual(
                    "", got,
                    "slot_date %r failed but still printed a date (%r), so a "
                    "caller would read a slot date that was never approved"
                    % (bad, got))

    def test_the_dispatch_path_keeps_no_private_copy_of_the_slot_crons(self):
        """THE SLOT TIMES LIVE IN ONE PLACE: deploy/schedule.json.

        The dispatch path used to carry `SLOT_CRON="5 2 * * *"` /
        `"5 14 * * *"` -- the WORKFLOW's dead-man's-switch crons, which fire 30
        minutes AFTER the real slots -- and dated a date-less dispatch by walking
        back to one of them. That is how a dispatch at 14:00 NPT on 2026-10-26
        was filed under the 25th. The variable is gone, and the assertion that
        demanded it was the defect stated as a test.
        """
        run = step_dict(self.STEP)["run"]
        crons = [str(spec.get("cron")) for spec in
                 ((workflow_doc().get("on") or {}).get("schedule") or [])]
        self.assertIn("5 2 * * *", crons)
        self.assertIn("5 14 * * *", crons)
        # Comment lines are excluded on purpose: the history above names the
        # literals it retired, and a grep over the raw text cannot tell that
        # explanation apart from the copy of the crons it is explaining.
        code_only = "\n".join(line for line in run.splitlines()
                              if not line.lstrip().startswith("#"))
        self.assertNotIn(
            "SLOT_CRON", code_only,
            "the step has a private copy of the slot crons again, so a dispatch "
            "with no date can be dated from a firing that has already happened "
            "instead of being refused.")

    def _dispatch_through_shell(self, slot_date, digest_time="evening",
                                extra_env=None):
        """Run the WHOLE step the way a dispatch would, and read back its env."""
        out = self.tmp / "dispatch_env.txt"
        out.write_text("", encoding="utf-8")
        env = {"GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(out),
               "EVENT_NAME": "workflow_dispatch", "EVENT_SCHEDULE": "",
               "INPUT_DIGEST_TIME": digest_time,
               "INPUT_TRIGGER_SOURCE": "worker", "INPUT_DRY_RUN": "",
               "INPUT_RETRY_SLOT": "", "INPUT_RETRY_OF": "", "INPUT_PHASE": "",
               "PATH": python3_on_path(self.tmp)}
        if extra_env:
            env.update(extra_env)
        if slot_date is not None:
            env["INPUT_SLOT_DATE"] = slot_date
        code, text = run_step_as_bash(
            self.STEP, env, self.tmp,
            expressions={"github.event_name": "workflow_dispatch",
                         "github.event.schedule": ""})
        return code, text, gh_env(out)

    def test_a_dispatch_through_the_shell_uses_the_supplied_date(self):
        """END TO END, through bash: the input reaches TODAY unchanged.

        The heredoc tests pin the RULE; this pins that the rule is WIRED -- that
        the input is bound into the step, reaches the derivation, and lands in
        the env every later step reads.
        """
        code, text, env = self._dispatch_through_shell("2026-10-17")
        self.assertEqual(0, code, text)
        self.assertEqual(
            "2026-10-17", env.get("TODAY"),
            "the dispatched slot date never reached TODAY. env=%r\n%s"
            % (env, text))
        self.assertEqual("evening", env.get("DIGEST_TIME"))
        self.assertEqual("worker", env.get("TRIGGER_SOURCE"))

    def test_a_bad_slot_date_stops_the_run_before_anything_is_written(self):
        """END TO END: FATAL, non-zero exit, and no slot date promoted."""
        code, text, env = self._dispatch_through_shell("2026-02-30")
        self.assertNotEqual(
            0, code,
            "a dispatch naming 2026-02-30 -- a day that does not exist -- did "
            "not stop the run")
        self.assertIn("FATAL", text)
        self.assertIn("slot_date", text)
        self.assertNotIn(
            "TODAY", env,
            "a failed slot date still promoted TODAY=%r, so the rest of the "
            "workflow would build a digest for a day nobody named"
            % env.get("TODAY"))

    def test_a_date_less_dispatch_is_dated_to_today_in_nepal(self):
        """END TO END: no date supplied means TODAY IN NEPAL, and it says so.

        CHANGED 2026-10-30 (run 41064520752). This used to assert a refusal, on
        the argument that any day a date-less dispatch picks is a guess. That
        argument was about picking a day in the PAST -- the 14:00 NPT dispatch on
        the 26th that produced the 25th -- and the refusal it produced made the
        documented manual entry point (`gh workflow run daily-news.yml` with its
        own defaults) die in its first step. "Build this slot now" is not a
        guess; it is today in Nepal, and the run now says which date it used.

        The clock is injected so this is the same test at any hour: 18:20Z on
        the 29th is 00:05 NPT on the 30th, so TODAY must be the 30th and not the
        UTC date.
        """
        code, text, env = self._dispatch_through_shell(
            None, extra_env={"SLOT_DATE_NOW_UTC": "2026-10-29T18:20:00Z"})
        self.assertEqual(0, code,
                         "a date-less manual dispatch failed:\n" + text)
        self.assertEqual(
            "2026-10-30", env.get("TODAY"),
            "a date-less dispatch was not dated to today in Asia/Kathmandu "
            "(18:20Z on the 29th is 00:05 NPT on the 30th). env=%r\n%s"
            % (env, text))
        self.assertIn("no slot_date", text,
                      "the run dated itself without saying what it used.\n" + text)

    def test_the_schedule_comes_from_the_env_block_not_the_script_text(self):
        """The step must BIND `github.event.schedule` and read it from there.

        Both halves of the slot -- the date derived above and the morning/
        evening `case` below it -- come from this one string, so a value spliced
        into the script text is a second source that can disagree with it.
        """
        step = step_dict(self.STEP)
        env = step.get("env") or {}
        self.assertEqual("${{ github.event.schedule }}", env.get("EVENT_SCHEDULE"),
                         "the step does not bind github.event.schedule, so the "
                         "slot date cannot be derived from the firing: %r"
                         % sorted(env))
        self.assertNotIn("${{ github.event.schedule }}", step["run"],
                         "github.event.schedule is still interpolated into the "
                         "script text instead of read from the environment, so "
                         "the slot name and the slot date can be taken from "
                         "different places")


# ==========================================================================
# The two literal step names, and the heredoc between them.
# ==========================================================================

class LiteralNamesAndHeredocTests(TempCase):
    def test_both_literal_step_names_survive(self):
        src = Path(WORKFLOW).read_text(encoding="utf-8")
        for name in (NTFY_STEP, GMAIL_STEP, TELEGRAM_STEP):
            self.assertIn(name, src,
                          "%r is gone. tests/render/test_render_honesty.py "
                          "slices daily-news.yml between these literals, so a "
                          "rename breaks every ntfy test with a bare ValueError "
                          "instead of a readable failure." % name)

    def test_run_ntfy_still_execs_and_parses(self):
        """The end-to-end half, re-run here because this change edits the
        heredoc. `run_ntfy` is IMPORTED, never reimplemented: a copy would
        agree with itself and tell us nothing about the real extraction."""
        got = run_ntfy(str(self.tmp), validated_payload("ok"), kind="evening")
        self.assertIsInstance(got, dict)
        for key in ("topic", "title", "message", "tags", "priority", "click"):
            self.assertIn(key, got)
        self.assertTrue(got["message"].strip())

    def test_the_heredoc_still_compiles_and_still_holds_the_dry_run_branch(self):
        source = ntfy_source()
        compile(source, "daily-news.yml:ntfy", "exec")
        for needle in ("urllib.request", "Payload preview:",
                       "print(json.dumps(payload, ensure_ascii=False, indent=2))",
                       "write_flag(False)", "return"):
            self.assertIn(needle, source,
                          "the dry-run branch the ntfy tests depend on is "
                          "missing %r" % needle)

    def test_the_heredoc_cannot_see_the_phase_at_all(self):
        """The STRUCTURAL reason the payload cannot differ between phases: the
        notification step is not given PHASE in its `env:` block, so the heredoc
        has nothing to branch on. This is what makes the byte-identity test
        below a fact about the design rather than a coincidence."""
        env = step_dict("Publish to ntfy (primary)")["env"] or {}
        self.assertNotIn("PHASE", env,
                         "the ntfy step must not receive PHASE. Handing it the "
                         "phase would let the payload branch on it, and the "
                         "prebuild's push would stop being the same bytes as the "
                         "slot's. env: %r" % env)
        source = ntfy_source()
        self.assertNotIn("PHASE", source,
                         "the ntfy heredoc references PHASE. It must not: the "
                         "payload a prebuild seals and the payload a real run "
                         "pushes have to be the same bytes.")

    def test_the_ntfy_step_reads_the_promoted_value_and_nothing_else(self):
        env = step_dict("Publish to ntfy (primary)")["env"] or {}
        self.assertEqual("${{ env.NTFY_DRY_RUN == 'true' }}", env["DRY_RUN"],
                         "the ntfy step must read the value promoted by the "
                         "phase step. Reading github.event.inputs.dry_run "
                         "directly cannot express 'a prebuild is always dry'.")

    def test_the_heredoc_reads_dry_run_from_the_environment_and_only_there(self):
        """Pins the edit to the env line. The heredoc's `DRY_RUN` read is the
        pre-existing contract; what is new is that the VALUE is now the
        promoted one. Any re-derivation inside the body -- from the event
        payload, or from a phase -- would reintroduce the bug this change fixes.
        """
        source = ntfy_source()
        reads = [ln.strip() for ln in source.split("\n") if "DRY_RUN" in ln]
        self.assertEqual(1, len(reads),
                         "the heredoc must read DRY_RUN exactly once, from the "
                         "environment. Found: %r" % (reads,))
        self.assertIn("os.environ", reads[0])
        for banned in ("github.event", "inputs.dry_run", "phase", "PHASE"):
            self.assertNotIn(banned, source,
                             "the ntfy heredoc references %r. It receives only "
                             "the promoted DRY_RUN value and cannot know the "
                             "phase -- which is what makes the payload "
                             "identical across phases." % banned)


class PayloadIsIdenticalAcrossPhasesTests(TempCase):
    """Payload bytes identical at phase=full and phase=prebuild.

    Run through BOTH steps, so the value compared is the one the real chain
    produces: the phase step resolves NTFY_DRY_RUN, and that resolved value is
    what the ntfy step receives.
    """

    def test_the_same_input_yields_the_same_payload_bytes(self):
        payloads = {}
        for phase in ("full", "prebuild"):
            out = self.tmp / ("env_%s.txt" % phase)
            out.write_text("", encoding="utf-8")
            code, text = run_step_as_bash(
                "Determine date, digest time and trigger source",
                {"GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(out),
                 "EVENT_NAME": "workflow_dispatch",
                 # A dispatch must name its slot date since 2026-10-26; without
                 # one the step is refused before anything can be compared.
                 "INPUT_SLOT_DATE": "2026-10-09",
                 "INPUT_DIGEST_TIME": "morning",
                 "INPUT_TRIGGER_SOURCE": "prebuild",
                 "INPUT_DRY_RUN": "true",
                 "INPUT_RETRY_SLOT": "", "INPUT_RETRY_OF": "",
                 "INPUT_PHASE": phase},
                self.tmp,
                expressions={"github.event.schedule": ""})
            self.assertEqual(0, code, text)
            env = gh_env(out)
            # `dry_run=true` is passed so BOTH phases take the heredoc's
            # dry-run branch and a payload exists to compare. A full build with
            # dry_run=false would POST to ntfy, which this suite must never do.
            self.assertEqual("true", env["NTFY_DRY_RUN"])
            work = self.tmp / ("work_%s" % phase)
            work.mkdir(exist_ok=True)
            got = run_ntfy(str(work), validated_payload("degraded"),
                           kind="morning")
            payloads[phase] = json.dumps(got, ensure_ascii=False,
                                         sort_keys=True)
        self.assertEqual(
            payloads["full"], payloads["prebuild"],
            "the ntfy payload differs between phase=full and phase=prebuild for "
            "the same input. The artifact a prebuild seals would not be the push "
            "the slot makes.")

    def test_the_prebuild_payload_lands_on_disk_for_the_seal_step(self):
        """The file is the ONLY channel between the two jobs, so it has to be
        written and it has to be valid JSON."""
        work = self.tmp / "payloadrun"
        work.mkdir()
        run_ntfy(str(work), validated_payload("ok"), kind="morning")
        path = work / "state" / "ntfy_payload.json"
        self.assertTrue(path.is_file(),
                        "the dry-run branch did not write state/ntfy_payload.json, "
                        "so digest-prebuild.yml's seal job has nothing to build "
                        "the artifact from")
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in ("title", "message", "click"):
            self.assertIn(key, payload)
        self.assertTrue(payload["click"].startswith("https://"))

    def test_the_file_write_comes_after_the_preview_print_and_prints_nothing(self):
        """The exact reason it is written to a file and not to stdout: the
        render suite decodes the JSON that follows "Payload preview:" out of
        this step's stdout, so a second JSON document on stdout would break 26
        existing tests."""
        source = ntfy_source()
        start = source.index("if dry_run:")
        end = source.index("# --- POST to the NTFY_SERVER root")
        branch = source[start:end]
        self.assertIn("state/ntfy_payload.json", branch,
                      "the payload file write must live inside the dry-run "
                      "branch; that branch is the only one a prebuild reaches")
        self.assertLess(
            branch.index("print(json.dumps(payload"),
            branch.index("state/ntfy_payload.json"),
            "the file write must come AFTER the preview print, so a reader of "
            "the log sees the payload before the job continues")
        # Nothing the write itself does may print. Checked on the STATEMENT
        # lines only: the comment block above it explains why the write is
        # silent, and quoting that comment back at itself proves nothing.
        lines = branch.split("\n")
        write_at = next(i for i, ln in enumerate(lines)
                        if "state/ntfy_payload.json" in ln)
        statement = [ln.strip() for ln in lines[write_at:write_at + 2]]
        for ln in statement:
            self.assertNotIn("print(", ln,
                             "the payload file write must produce no stdout. "
                             "Statement lines are %r" % (statement,))
        # And nothing else in the branch may print a JSON document either.
        self.assertEqual(1, branch.count("print(json.dumps("),
                         "the dry-run branch must print the payload preview "
                         "exactly once; a second JSON document on stdout breaks "
                         "every ntfy test in tests/render")

    def test_the_file_is_uploaded_and_then_cleaned(self):
        """Both halves, or the artifact survives into the state commit and the
        page payload leaks into the repository."""
        upload = step_dict("Upload digest artifacts")
        self.assertIn("state/ntfy_payload.json", upload["with"]["path"],
                      "the payload file must be in the artifact list, or the "
                      "seal job cannot download it. path=%r"
                      % upload["with"]["path"])
        clean = step_dict("Clean transient state files")["run"]
        self.assertIn("state/ntfy_payload.json", clean,
                      "the payload file must be in the `rm -f` list, or it "
                      "survives into the working tree of a run that was killed "
                      "after the upload.")


# ==========================================================================
# The two backup channels must not fire on a prebuild.
# ==========================================================================

class BackupsDoNotFireOnPrebuildTests(unittest.TestCase):
    def condition_of(self, step_name):
        return str(step_dict(step_name).get("if") or "")

    def test_both_backup_conditions_name_the_prebuild_exclusion(self):
        for name in (GMAIL_STEP.strip("- name: "), TELEGRAM_STEP.strip("- name: ")):
            condition = self.condition_of(name)
            self.assertIn("env.PHASE != 'prebuild'", condition,
                          "%r does not exclude a prebuild. Its condition is %r. "
                          "A prebuild writes state/.ntfy_ok=false BY DESIGN, "
                          "which is exactly this step's trigger, so without the "
                          "term the 06:50 prebuild emails the owner twice a "
                          "day." % (name, condition))

    def test_the_gmail_backup_does_not_fire_on_a_prebuild(self):
        """BEHAVIOUR, not a substring: the exact state a prebuild is in."""
        condition = self.condition_of(GMAIL_STEP.strip("- name: "))
        context = {
            "env.FALLBACK_NOOP": "",
            "steps.deploy_website.outputs.website_ok": "true",
            "steps.send_ntfy.outputs.ntfy_ok": "false",
            "env.PHASE": "prebuild",
        }
        self.assertTrue(
            evaluate_if(condition, dict(context, **{"env.PHASE": "full"})),
            "sanity: on a full run this condition MUST be true, or the "
            "condition cannot express anything")
        self.assertFalse(
            evaluate_if(condition, context),
            "a prebuild with ntfy_ok=false would fire the Gmail backup. That "
            "is the owner being emailed a half-built digest at 06:50 and again "
            "at 18:50, from the arm that exists to be silent.")

    def test_the_telegram_backup_does_not_fire_on_a_prebuild(self):
        condition = self.condition_of(TELEGRAM_STEP.strip("- name: "))
        context = {
            "env.FALLBACK_NOOP": "",
            "steps.deploy_website.outputs.website_ok": "false",
            "steps.send_ntfy.outputs.ntfy_ok": "false",
            "env.PHASE": "prebuild",
        }
        self.assertTrue(
            evaluate_if(condition, dict(context, **{"env.PHASE": "full"})),
            "sanity: on a full run with both primaries down this condition MUST "
            "be true -- that is the case the backup exists for")
        self.assertFalse(
            evaluate_if(condition, context),
            "a prebuild with both primaries reported down would fire the "
            "Telegram backup.")

    def test_the_full_fallback_ladder_still_fires_on_a_full_build(self):
        """The term must EXCLUDE prebuilds, not suppress the backups."""
        for name, ctx in (
                (GMAIL_STEP.strip("- name: "),
                 {"env.FALLBACK_NOOP": "",
                  "steps.deploy_website.outputs.website_ok": "false",
                  "steps.send_ntfy.outputs.ntfy_ok": "false",
                  "env.PHASE": "full"}),
                (TELEGRAM_STEP.strip("- name: "),
                 {"env.FALLBACK_NOOP": "",
                  "steps.deploy_website.outputs.website_ok": "false",
                  "steps.send_ntfy.outputs.ntfy_ok": "false",
                  "env.PHASE": "full"})):
            self.assertTrue(evaluate_if(self.condition_of(name), ctx),
                            "%r no longer fires on a full build, so the "
                            "fallback ladder this repository depends on is "
                            "broken" % name)

    def test_a_whole_run_of_fallback_noop_still_suppresses_everything(self):
        context = {"env.FALLBACK_NOOP": "true",
                   "steps.deploy_website.outputs.website_ok": "false",
                   "steps.send_ntfy.outputs.ntfy_ok": "false",
                   "env.PHASE": "full"}
        for name in (GMAIL_STEP.strip("- name: "),
                     TELEGRAM_STEP.strip("- name: ")):
            self.assertFalse(evaluate_if(self.condition_of(name), context))


class EvaluatorSelfTests(unittest.TestCase):
    """The evaluator is test code, so it is held to the same standard."""

    def test_it_evaluates_the_grammar_it_is_used_on(self):
        ctx = {"a": "", "b": "x", "c": "true"}
        # Comparisons use the VALUE, not its truthiness. This is the exact bug
        # a sloppy evaluator has: bool('prebuild') == 'prebuild' is False, so
        # `env.PHASE != 'prebuild'` would read True and the prebuild backups
        # would fire.
        self.assertTrue(evaluate_if("a != 'true'", ctx))
        self.assertFalse(evaluate_if("b == 'true'", ctx))
        self.assertTrue(evaluate_if("b != 'true'", ctx))
        self.assertTrue(evaluate_if("a == ''", ctx))
        self.assertTrue(evaluate_if("(b != 'true' || c == 'true')", ctx))
        self.assertFalse(evaluate_if("(b != 'true' && c != 'true')", ctx))
        self.assertFalse(evaluate_if("a == 'true' && (b == 'true')", ctx))
        self.assertTrue(evaluate_if("!(b == 'true')", ctx))
        self.assertTrue(evaluate_if("a != 'true' && (b == 'true' || c == 'true')", ctx))
        self.assertTrue(evaluate_if("steps.x.outputs.y != 'true'",
                                    {"steps.x.outputs.y": ""}))
        # A bare name is a BOOLEAN: present and truthy. `a` is the empty string
        # (absent in Actions terms) and `b` is a non-empty value.
        self.assertFalse(evaluate_if("a", ctx))
        self.assertTrue(evaluate_if("b", ctx))
        # And the live case this whole evaluator exists for.
        self.assertFalse(evaluate_if("env.PHASE != 'prebuild'",
                                     {"env.PHASE": "prebuild"}))
        self.assertTrue(evaluate_if("env.PHASE != 'prebuild'",
                                    {"env.PHASE": "full"}))


if __name__ == "__main__":
    unittest.main()
