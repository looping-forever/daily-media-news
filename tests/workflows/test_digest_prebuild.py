#!/usr/bin/env python3
"""tests/workflows/test_digest_prebuild.py -- unit U6 and U6a.

WHAT IS BEING GUARDED
---------------------
U6 ships `.github/workflows/digest-prebuild.yml`: a thin orchestrator that
dispatches `daily-news.yml` with `phase: prebuild` 30 min before each slot,
waits for it, and seals what it built into the Worker's KV. U6a ships an arm on
`digest-watchdog.yml` that goes red when the artifact for a slot that should
already have been built is absent.

Both are new surface, and this repository's history is that new surface arrives
unlinted and stays broken: 425 Python tests passed while all four production
workflows were dead, because nothing looked at workflow YAML or the shell in
it. So the rules here are deliberately about the SHAPE of the two new
workflows, not about the pipeline they call.

WHY THE SUBSTANTIVE PROOFS ARE EXECUTED, NOT ASSERTED ABOUT
-------------------------------------------------------------
Several of these run the real code with a controlled environment, because the
defect each guards is not visible by reading:

  * the prebuild's slot derivation is executed against all four committed
    crons, because "which slot am I?" answered by the wall clock is a bug that
    reads perfectly well;
  * the U6a arm is executed against seven different `?health=1` payloads,
    because the whole point of the arm is that ABSENT and UNREADABLE are
    different faults and only executing them proves the arm tells them apart;
  * the seal job's markup list is checked against the archive scrapers'
    ACTUAL behaviour on a really-rendered page, not against a copy of the
    regexes, because a copy agrees with itself forever.

OFFLINE BY CONSTRUCTION
-----------------------
No network, no GitHub API, no Cloudflare, no `gh`, no `wrangler`. Every Python
heredoc body is `exec`'d in a temp directory with its inputs written to files
and its outputs written to files; `urlopen` is not reachable from these bodies
because every one of them either returns before its request or is never given
one.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import workflows as W  # noqa: E402
from tests.workflows import test_workflow_lint as L  # noqa: E402

sys.path.insert(0, str(W.REPO_ROOT / "scripts"))

PREBUILD_WORKFLOW = "digest-prebuild.yml"
WATCHDOG_WORKFLOW = "digest-watchdog.yml"

#: The instants the WORKER dispatches this workflow on, keyed by the NEPAL slot
#: each one prepares. Committed here as a SECOND READING of
#: `deploy/schedule.json`'s `dispatcher_crons.digest_prebuild`, so a wrong
#: conversion is contradicted rather than merely restated --
#: `PrebuildIsMutuallyExclusiveTests` asserts these are exactly the crons
#: the Worker fires, and the lint pins their Nepal Time in
#: PREBUILD_CRON_NPT_TABLE.
#:
#: These used to be the four GitHub crons of digest-prebuild.yml
#: (5 1 / 20 1 / 5 13 / 20 13) with a primary/retry distinction. There is no
#: primary/retry distinction any more because there are no GitHub crons at
#: all, and there is no `PREBUILD_CRONS` mapping onto the workflow's own
#: `schedule:` because that list is deliberately empty.
PREBUILD_DISPATCH_CRONS = {
    "55 0 * * *": "morning",
    "55 12 * * *": "evening",
}

#: The prebuild workflow itself. It must keep a `workflow_dispatch` trigger
#: with this choice input: the Worker dispatches it, and a manual run needs
#: to be able to name its slot the same way.
PREBUILD_SLOT_CHOICES = ["morning", "evening"]

#: The archive index's markup, as the scrapers require it. These are the three
#: literals the seal job asserts, and `SealMarkupIsTheArchiveScrapersMarkup`
#: below proves the equivalence by behaviour rather than by restating it.
SECTION_HEADER_MARKUP = '<h2 class="section-header"'
CARD_MARKUP = '<article class="card">'
CARD_THUMB_MARKUP = '<img class="card-thumb"'


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def job_of(name, job_id):
    doc = W.load_workflow(name)
    jobs = doc.get("jobs") or {}
    if job_id not in jobs:
        raise AssertionError("%s has no job %r; jobs are %s"
                             % (name, job_id, sorted(jobs)))
    return jobs[job_id]


def steps_of(name, job_id):
    return list(job_of(name, job_id).get("steps") or [])


def step_named(name, job_id, step_name):
    for step in steps_of(name, job_id):
        if str(step.get("name") or "") == step_name:
            return step
    raise AssertionError("%s / %s has no step %r; steps are %s"
                         % (name, job_id, step_name,
                            [s.get("name") for s in steps_of(name, job_id)]))


def run_blocks(name, job_id):
    out = []
    for step in steps_of(name, job_id):
        if isinstance(step.get("run"), str):
            out.append((str(step.get("name") or "<unnamed>"), step["run"]))
    return out


def python_heredocs(run_text):
    """Every Python heredoc body in a run block, via the lint's own extractor.

    Imported rather than reimplemented for the reason the rest of this
    repository keeps making: a second copy of a parser agrees with itself and
    therefore proves nothing about the first.
    """
    return [body for _tag, body in L.heredoc_bodies(run_text)]


def shell_commands_only(run_text):
    """`run_text` with heredoc bodies and quoted spans removed.

    Needed because "does this step INVOKE `wrangler`" and "does this step
    MENTION `wrangler`" are different questions, and an `echo` or a `die()`
    message that names a command for the reader to run is not an invocation. A
    naive substring scan fails on the workflow's own error text -- which is
    exactly the text a reader needs most -- so the scan is done on code.
    """
    text = run_text
    lines = text.split("\n")
    out, skipping = [], False
    for line in lines:
        m = L.HEREDOC_RE.search(line)
        if m and not skipping:
            skipping = m.group(1)
            out.append("")
            continue
        if skipping:
            if line == skipping:
                skipping = False
            out.append("")
            continue
        out.append(re.sub(r"'[^']*'|\"[^\"]*\"", "''", line))
    return "\n".join(out)


def exec_heredoc(body, env, cwd, capture_stderr=False):
    """`exec` one heredoc body with `env` applied. Returns (code, stdout).

    `capture_stderr` is OFF by default and that default is load-bearing: the
    other callers assert on this text as the payload the step PRINTED, so folding
    a warning into it would change what they are reading. The caller that turns
    it on needs it because a step may report a FATAL on stderr and exit non-zero
    -- there the failure IS the subject, and the sentence explaining it has to
    arrive in the same string as the verdict.
    """
    saved = dict(os.environ)
    os.environ.update({k: str(v) for k, v in env.items()})
    buf = io.StringIO()
    code = 0
    cwd_before = os.getcwd()
    try:
        os.chdir(str(cwd))
        with contextlib.ExitStack() as streams:
            streams.enter_context(contextlib.redirect_stdout(buf))
            if capture_stderr:
                streams.enter_context(contextlib.redirect_stderr(buf))
            try:
                exec(compile(body, "<heredoc>", "exec"), {"__name__": "heredoc"})
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(cwd_before)
        os.environ.clear()
        os.environ.update(saved)
    return code, buf.getvalue()


#: The expressions the uploader's `path:` list carries, and what they stand
#: for in a fixture. Rendered rather than copied, so no test has to restate the
#: expression the way the runner does -- a copied expression is a second thing
#: that can drift from the first, which is how "email asserted, page absent"
#: survived 12 runs in a repository that already had a guard for this class.
_UPLOAD_EXPRESSIONS = (
    ("${{ env.TODAY }}", "TODAY"),
    ("${{ steps.digest_time.outputs.time }}", "SLOT"),
)


def render_upload_entry(entry, today, slot):
    out = entry
    for token, value in (("${{ env.TODAY }}", today),
                         ("${{ steps.digest_time.outputs.time }}", slot)):
        out = out.replace(token, value)
    return out


def unpacked_path_for(entry, today, slot):
    """Where upload-artifact@v4 puts `entry` once download-artifact unpacks it.

    Derived through the lint's own archive-root calculation, so this test
    fixture cannot disagree with the guard that reads the same list.
    """
    entries = L.upload_artifact_path_entries()
    root = L.upload_archive_root(entries)
    rel = render_upload_entry(entry, today, slot)
    if root and rel.startswith(root + "/"):
        rel = rel[len(root) + 1:]
    return "prebuild/" + rel


def digest_page_entry(today="2026-10-09", slot="morning"):
    """The upload entry that carries the RENDERED WEB PAGE.

    Located by what it produces -- a rendered filename of `<date>-<slot>.html`
    -- rather than by its directory or by its position in the list, so moving
    the page, renaming the expression or reordering the list is handled here
    instead of becoming a stale literal.
    """
    wanted = "%s-%s.html" % (today, slot)
    for entry in L.upload_artifact_path_entries():
        if render_upload_entry(entry, today, slot).endswith(wanted):
            return entry
    raise AssertionError(
        "no entry in the upload `path:` list renders to a file named %r, so the "
        "rendered web page is not in the artifact and the seal has nothing to "
        "assert or hash. Entries=%r"
        % (wanted, L.upload_artifact_path_entries()))


def stage_artifact(base, *, page_html, validated, today="2026-10-09",
                   slot="morning", click=None):
    """Materialise exactly what download-artifact@v4 would produce.

    Writes the rendered page at the location the uploader's own list implies
    for it, plus the ntfy payload and the validated stories at THEIR implied
    locations. Every path comes from `unpacked_path_for`, so this fixture and
    check 20 are reading one description of the archive rather than two.
    """
    base = Path(base)
    payload = {"title": "T", "message": "M",
               "click": click or ("https://daily-digest.example.pages.dev/digests/"
                                  "%s-%s.html" % (today, slot))}
    files = {
        digest_page_entry(today, slot): page_html,
        "state/ntfy_payload.json": json.dumps(payload),
        "state/stories.validated.json": json.dumps(validated),
    }
    written = {}
    for entry, text in files.items():
        path = base / unpacked_path_for(entry, today, slot)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written[entry] = path
    return written


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="prebuild-test-")
        self.tmp = Path(self._tmp)
        self.addCleanup(shutil.rmtree, self._tmp, True)


# ==========================================================================
# Registration. A workflow nobody lints is invisible drift.
# ==========================================================================

class RegistrationTests(unittest.TestCase):
    def test_the_prebuild_workflow_is_registered_for_linting(self):
        self.assertIn(
            PREBUILD_WORKFLOW, W.WORKFLOW_NAMES,
            "%s is not in tests/workflows/__init__.py's WORKFLOW_NAMES. That "
            "tuple is hardcoded, so an unregistered workflow is skipped by "
            "EVERY check in tests/workflows/test_workflow_lint.py -- no bash "
            "-n, no heredoc terminator check, no secret scan -- while the "
            "suite still reports green. Register it in the same change that "
            "adds the file." % PREBUILD_WORKFLOW)

    def test_every_registered_workflow_exists_on_disk(self):
        missing = [n for n in W.WORKFLOW_NAMES
                   if not (W.WORKFLOW_DIR / n).is_file()]
        self.assertEqual([], missing,
                         "WORKFLOW_NAMES names workflows that do not exist: %s"
                         % (missing,))

    def test_every_workflow_file_on_disk_is_registered(self):
        """The FORWARD direction, which nothing checked until 2026-10-19.

        The test above catches a NAME with no file. The reverse -- a FILE with
        no name -- is the more dangerous direction, and it was silently allowed:
        an unregistered workflow is skipped by every check in
        test_workflow_lint.py (no `bash -n`, no heredoc terminator check, no
        secret scan, no cron table) while the suite still reports green. That is
        the hazard written into the tuple's own docstring, and nothing enforced
        it. budget-sentinel.yml arrived on 2026-10-19, which is what prompted
        closing it.
        """
        on_disk = {path.name for path in W.WORKFLOW_DIR.glob("*.yml")}
        unregistered = sorted(on_disk - set(W.WORKFLOW_NAMES))
        self.assertEqual(
            [], unregistered,
            "these workflow files exist but are NOT in WORKFLOW_NAMES: %s. "
            "Every check in test_workflow_lint.py iterates that tuple, so these "
            "files are linted by nobody while a green suite says otherwise. "
            "Register the file in tests/workflows/__init__.py." % (unregistered,))

    def test_the_lint_actually_descends_into_the_new_workflow(self):
        """Registration is necessary but not sufficient: the tuple entry must
        yield `run:` blocks. A parser regression that stopped descending into
        `jobs` would leave the entry present and every check vacuous."""
        jobs = {job for job, _s, _r, _d in W.iter_run_blocks(PREBUILD_WORKFLOW)}
        self.assertEqual({"seal"}, jobs,
                         "digest-prebuild.yml yielded run: blocks for %s; it has "
                         "ONE job since 2026-10-19 (A) -- the build hands the seal "
                         "its run id, so the `dispatch` job that watched the build "
                         "was deleted -- and a second name here means either the "
                         "deletion came back or the YAML parser is not descending "
                         "into jobs, in which case every shell check below is "
                         "checking nothing."
                         % (sorted(jobs),))


# ==========================================================================
# The schedule. Registration of the crons is a deliberate act (Check 8).
# ==========================================================================

class PrebuildScheduleTests(unittest.TestCase):
    def test_the_prebuild_workflow_declares_NO_schedule_trigger(self):
        """THE FIX, AS A SHAPE. `on.schedule` is absent, and `workflow_dispatch`
        with its `digest_time` choice is intact.

        Both halves matter. Deleting the crons without the dispatch input would
        have left a workflow nothing can start; keeping the crons is what let
        three prebuilds fire per slot.
        """
        doc = W.load_workflow(PREBUILD_WORKFLOW)
        on = doc.get("on")
        self.assertIsInstance(on, dict,
                             "%s has no `on:` mapping: %r"
                             % (PREBUILD_WORKFLOW, on))
        crons = list(W.iter_schedule_crons(PREBUILD_WORKFLOW))
        self.assertEqual(
            [], crons,
            "digest-prebuild.yml declares schedule crons %s. The prebuild is "
            "dispatched by the Cloudflare Worker at 06:40 / 18:40 NPT because "
            "GitHub-hosted SCHEDULED runs measured 4.5-7h late. A cron here is a "
            "SECOND firing for the same slot: three digest builds and three "
            "Pages deploys per slot on a punctual day, and on a late day a "
            "12h-delayed firing that overwrites an already-delivered "
            "digest:artifact:<D>:evening under a date that still matches, which "
            "neither stale_date nor ARTIFACT_MAX_BUILD_AGE_S can catch."
            % (crons,))
        self.assertNotIn(
            "schedule", on,
            "digest-prebuild.yml still carries an `on.schedule` key. An empty "
            "one would be a second way to say 'no crons' that a later edit "
            "could quietly fill; the trigger is the Worker and nothing else."
        )

        dispatch = on.get("workflow_dispatch")
        self.assertIsInstance(
            dispatch, dict,
            "digest-prebuild.yml has no workflow_dispatch trigger, so the "
            "Worker cannot ask for a prebuild at all: on: keys are %s"
            % (sorted(on),))
        inputs = dispatch.get("inputs") or {}
        digest_time = inputs.get("digest_time") or {}
        self.assertEqual("choice", str(digest_time.get("type") or ""),
                         "digest_time must stay a choice input: %r"
                         % (digest_time,))
        self.assertEqual(PREBUILD_SLOT_CHOICES,
                         [str(o) for o in (digest_time.get("options") or [])],
                         "the dispatcher's slot names and this workflow's "
                         "choices must be the same two strings, or a dispatch "
                         "names a slot the workflow refuses.")

    def test_the_dispatcher_prebuild_crons_convert_to_the_committed_npt_table(self):
        """The prebuild instants that survive the change, converted rather than
        restated: 06:40 and 18:40 NPT."""
        problems = []
        for expr, slot in sorted(PREBUILD_DISPATCH_CRONS.items()):
            row = L.PREBUILD_CRON_NPT_TABLE.get(expr)
            if row is None:
                problems.append(
                    "dispatcher prebuild cron %r has no row in "
                    "PREBUILD_CRON_NPT_TABLE. Moving the prebuild is a "
                    "deliberate act: add the row with the Nepal Time you "
                    "expect and let CI confirm the conversion." % expr)
                continue
            expected = {(int(h), int(m)) for h, m in row}
            try:
                actual = set(L.cron_to_npt(expr))
            except ValueError as exc:
                problems.append("cron %r is unparseable: %s" % (expr, exc))
                continue
            if actual != expected:
                problems.append("cron %r fires at %s NPT, table says %s"
                                % (expr, sorted(actual), sorted(expected)))
            if len(actual) != 1:
                problems.append(
                    "cron %r resolves to %d Nepal instants (%s). One slot gets "
                    "ONE prebuild; a multi-instant cron is the shape that made "
                    "three firings per slot." % (expr, len(actual),
                                                 sorted(actual)))
            for hour, _minute in actual:
                label = "morning" if hour < 12 else "evening"
                if label != slot:
                    problems.append(
                        "cron %r resolves to %02d NPT, which is the %s slot, but "
                        "it is committed as the %s prebuild" % (expr, hour, label,
                                                               slot))
        self.assertEqual([], problems, "\n  ".join(problems))
        # Non-vacuity: this suite must actually be checking two crons.
        self.assertEqual(2, len(PREBUILD_DISPATCH_CRONS),
                         "the dispatcher prebuild table shrank; the check above "
                         "would then pass by having nothing to check")

    def test_the_prebuild_lands_ahead_of_its_slot(self):
        """The entire reason the unit exists. A prebuild at or after the slot
        removes the latency it was built to remove.

        THE SLOT IS READ FROM `dispatcher_crons.digest_slots`, NOT FROM
        daily-news.yml's `schedule:`.

        This is a fix to a wrong assertion, not a loosened one. daily-news.yml's
        two crons (`5 2` / `5 14` UTC = 07:50 / 19:50 NPT) are its
        DEAD-MAN'S-SWITCH FALLBACK, which its own comment says fires 30 minutes
        AFTER the Worker normally dispatched the slot (07:20 / 19:20 NPT).
        Measuring the lead against them reported 70 minutes and called the
        committed 40-minute lead a failure -- the test was measuring the
        wrong instant. The delivery slot the prebuild exists to serve is the
        Worker's `35 1` / `35 13`, so that is what the lead is measured
        against, and it is the SAME record the watchdog now reads.
        """
        slots = (L.load_schedule_json() or {}).get("dispatcher_crons", {}) \
            .get("digest_slots") or {}
        self.assertEqual({"morning", "evening"}, set(slots),
                         "deploy/schedule.json must record exactly one morning "
                         "and one evening digest slot: %s" % (slots,))
        digest = {}
        for label, expr in sorted(slots.items()):
            npt = L.cron_to_npt(expr)
            self.assertEqual(1, len(npt),
                             "slot %r (%r) must resolve to ONE Nepal instant, "
                             "or there is no single slot to build ahead of"
                             % (label, expr))
            hour, minute = npt[0]
            self.assertEqual(label, "morning" if hour < 12 else "evening",
                             "digest slot %r (%r) resolves to %02d:%02d NPT, "
                             "which is the %s half of the day"
                             % (label, expr, hour, minute,
                                "morning" if hour < 12 else "evening"))
            digest[label] = hour * 60 + minute
        prebuilds = {}
        for expr, slot in sorted(PREBUILD_DISPATCH_CRONS.items()):
            npt = L.cron_to_npt(expr)
            self.assertEqual(1, len(npt), "%r should be one NPT slot" % expr)
            hour, minute = npt[0]
            self.assertEqual(slot, "morning" if hour < 12 else "evening",
                             "%r resolves to %02d:%02d NPT, which is not the "
                             "%s slot it is committed to" % (expr, hour, minute, slot))
            prebuilds[slot] = hour * 60 + minute
        self.assertEqual({"morning", "evening"}, set(prebuilds),
                         "every slot needs exactly one prebuild: %s"
                         % (prebuilds,))
        for slot, at in prebuilds.items():
            lead = digest[slot] - at
            # The committed figure is 40 (06:40 / 18:40 against 07:20 / 19:20),
            # which is PREBUILD_LEAD_MINUTES in deploy/cloudflare-worker.js. The
            # bound is deliberately a little wider than that, because the
            # property that matters is "ahead of the slot, and not so far ahead
            # that the digest is stale when it is pushed" -- the same bounds
            # this assertion used when the instants were GitHub crons.
            self.assertTrue(
                0 < lead <= 75,
                "the %s prebuild fires at %02d:%02d NPT, %d min before the %s "
                "slot. It must be ahead of the slot, and within 75 min of it so "
                "the digest is still fresh when it is pushed."
                % (slot, at // 60, at % 60, lead, slot))
            self.assertTrue(
                35 <= lead <= 45,
                "the %s prebuild is %d min before its slot; the committed lead "
                "is 40 (06:40 / 18:40 against 07:20 / 19:20), which is what "
                "keeps the digest ~40 min old at push time rather than stale."
                % (slot, lead))

    def test_a_missing_dispatcher_npt_row_is_rejected(self):
        """MUTATION PROOF. A table row that is missing must fail, or adding a
        row to this table would be decoration."""
        expr = "55 0 * * *"
        row = L.PREBUILD_CRON_NPT_TABLE.get(expr)
        self.assertIsNotNone(row, "sanity: %r must have a row before it can be "
                                  "removed" % expr)
        self.assertEqual([(6, 40)], [tuple(r) for r in row])

        broken = dict(L.PREBUILD_CRON_NPT_TABLE)
        del broken[expr]
        problems = []
        for cron in PREBUILD_DISPATCH_CRONS:
            if cron not in broken:
                problems.append("cron %r has no row in PREBUILD_CRON_NPT_TABLE"
                                % cron)
        self.assertTrue(
            problems,
            "removing the row for %r produced NO problem. The table is not "
            "gating anything, so a prebuild moved without a row would pass CI "
            "silently." % expr)

    def test_the_watchdog_slot_invariant_is_not_broken(self):
        """`watchdog_slot_table_problems()` requires EXACTLY ONE morning and ONE
        evening digest cron in daily-news.yml. That is precisely why the
        prebuild crons live in their own file rather than being added to
        daily-news.yml. Assert the invariant directly as well as through the
        helper, so a failure says which half broke."""
        problems = L.watchdog_slot_table_problems()
        self.assertEqual(
            [], problems,
            "the watchdog's slot derivation no longer matches the committed "
            "crons:\n  " + "\n  ".join(problems))

        digest_crons = dict(
            (expr, L.cron_to_npt(expr))
            for expr in W.iter_schedule_crons("daily-news.yml"))
        mornings, evenings = [], []
        for npt_slots in digest_crons.values():
            (hour, minute), = npt_slots
            (mornings if hour < 12 else evenings).append((hour, minute))
        self.assertEqual(1, len(mornings),
                         "daily-news.yml must keep exactly one morning digest "
                         "cron; found %s" % (mornings,))
        self.assertEqual(1, len(evenings),
                         "daily-news.yml must keep exactly one evening digest "
                         "cron; found %s" % (evenings,))

        # And the prebuild's own crons must NOT be in daily-news.yml, which is
        # the other half of the same invariant.
        overlap = set(W.iter_schedule_crons(PREBUILD_WORKFLOW)) & set(
            W.iter_schedule_crons("daily-news.yml"))
        self.assertEqual(set(), overlap,
                         "crons %s appear in BOTH daily-news.yml and the "
                         "prebuild file. daily-news.yml must keep exactly one "
                         "morning and one evening slot or the watchdog's slot "
                         "naming becomes ambiguous." % sorted(overlap))


# ==========================================================================
# The shell in the new workflow really is shell.
#
# This is the Bug B rule applied to U6 explicitly: 425 Python tests passed while
# every workflow was dead, because nothing parsed the YAML or the shell.
# ==========================================================================

class NewShellIsActuallyShellTests(TempDirCase):
    WORKFLOWS = (PREBUILD_WORKFLOW, WATCHDOG_WORKFLOW)

    def bash(self):
        bash = W.bash_executable()
        if bash is None:
            self.fail("bash was not found on PATH, so the shell in the new "
                      "workflows cannot be linted at all. This fails instead of "
                      "skipping: a skipped shell lint is a green run with no "
                      "shell lint, which is how Bug A and Bug B both shipped.")
        return bash

    def test_every_new_run_block_parses_as_bash(self):
        bash = self.bash()
        failures, checked = [], 0
        for wf in self.WORKFLOWS:
            for job, step, run, _d in W.iter_run_blocks(wf):
                checked += 1
                proc = W.bash_syntax_check(run, bash)
                if proc.returncode != 0:
                    failures.append("%s / %s / %r: bash -n exit %d: %s"
                                    % (wf, job, step, proc.returncode,
                                       (proc.stderr or "").strip()))
        self.assertEqual([], failures,
                         "shell syntax errors in the new workflows:\n  "
                         + "\n  ".join(failures))
        self.assertGreaterEqual(checked, 6,
                                "only %d run: blocks were found across the new "
                                "and edited workflows; the parser is probably "
                                "not descending." % checked)

    def test_every_new_heredoc_terminator_is_at_column_zero_in_the_parsed_text(self):
        failures, checked = [], 0
        for wf in self.WORKFLOWS:
            for job, step, run, _d in W.iter_run_blocks(wf):
                checked += 1
                for tag, line_no in L.find_unterminated_heredocs(run):
                    failures.append(
                        "%s / %s: heredoc <<%s on line %d has no column-0 '%s' "
                        "terminator. bash will die with 'here-document "
                        "delimited by end-of-file'." % (wf, job, tag, line_no, tag))
        self.assertEqual([], failures, "\n  ".join(failures))
        self.assertGreaterEqual(checked, 1, "no run: blocks examined")

    def test_every_new_python_heredoc_body_compiles(self):
        bodies, failures = 0, []
        for wf in self.WORKFLOWS:
            for job, step, run, _d in W.iter_run_blocks(wf):
                for body in python_heredocs(run):
                    bodies += 1
                    try:
                        compile(body, "%s:%s" % (wf, step), "exec")
                    except SyntaxError as exc:
                        failures.append("%s / %s / %r: %s" % (wf, job, step, exc))
        self.assertEqual([], failures, "\n  ".join(failures))
        self.assertGreaterEqual(bodies, 3,
                                "only %d Python heredoc bodies were found; the "
                                "extractor is probably not matching." % bodies)


# ==========================================================================
# The prebuild is a THIN ORCHESTRATOR.
# ==========================================================================

class ThinOrchestratorTests(unittest.TestCase):
    def test_the_seal_job_never_re_implements_the_build(self):
        """The seal is a SEAL. The build it seals is daily-news.yml, dialled by
        the build's own last step (A), so nothing here may grow a second copy of
        the feed sweep, the gate, the AI ladder, the render or the ntfy send."""
        joined = "\n".join(v for _n, v in run_blocks(PREBUILD_WORKFLOW, "seal"))
        for forbidden in ("fetch_stories", "opencode", "pages/deploy",
                          "send_ntfy"):
            self.assertNotIn(
                forbidden, joined,
                "the seal job mentions %r. Re-implementing any part of the build "
                "here would create a second copy of a pipeline that must not have "
                "two." % forbidden)
        self.assertIn("secrets.GITHUB_TOKEN",
                      W.raw_workflow_text(PREBUILD_WORKFLOW),
                      "the seal needs secrets.GITHUB_TOKEN for the supersede "
                      "dispatch and for reading the run the build handed over. "
                      "No NEW GitHub secret should be required.")

    def test_the_confirm_poll_is_bounded_and_fails_closed(self):
        """The 45-minute `gh run watch` is gone (A), and this is what replaced
        it: a BOUNDED poll of the run the build handed over. The two hazards are
        the old ones -- waiting without a ceiling, and reporting a state the job
        never established -- so both are asserted against the real shell."""
        joined = step_named(
            PREBUILD_WORKFLOW, "seal",
            "Confirm the prebuild run succeeded before touching anything")["run"]
        self.assertIn("seq 1 5", joined,
                      "the confirm poll is not bounded. `gh run watch` has no "
                      "--timeout flag, which is why the old job needed coreutils "
                      "`timeout`; this poll has to carry its own attempt ceiling. "
                      "5 is the ceiling since 2026-10-20: the hand-off step that "
                      "dispatches the seal is gated on `success()` AND is the "
                      "build job's LAST step, so the run has already concluded.")
        self.assertIn("sleep 6", joined)
        self.assertIn("exit 1", joined,
                      "an unreadable or unfinished run must be fatal: a seal that "
                      "cannot tell a successful build from a failed one must "
                      "refuse to seal, not carry on.")
        self.assertNotIn("gh run watch", joined,
                         "the seal must not wait on a run indefinitely; it reads "
                         "the run's state and gives up.")
        self.assertIn("conclusion=$CONCLUSION", joined,
                      "the poll must publish the conclusion it actually read, "
                      "since the fetch step below is gated on it.")

    def test_the_seal_is_handed_its_run_and_never_searches_for_one(self):
        """A. The run id is an INPUT now. The deleted job had to DISCOVER the run
        it had just dispatched (`select(.id > $BEFORE)`) because daily-news.yml
        runs several times a day and 'the most recent run' is not necessarily
        this one. The build knows its own id, so the seal is told it and must
        never go looking -- a search that quietly picked the wrong run would seal
        somebody else's artifact."""
        seal_env = (step_named(
            PREBUILD_WORKFLOW, "seal",
            "Confirm the prebuild run succeeded before touching anything")
            .get("env") or {})
        self.assertEqual(
            "${{ github.event.inputs.run_id }}", str(seal_env.get("RUN_ID") or ""),
            "the seal's run id does not come from its own input. The build states "
            "which run it wants sealed; a value read from anywhere else is a run "
            "this prebuild did not make.")
        joined = "\n".join(v for _n, v in run_blocks(PREBUILD_WORKFLOW, "seal"))
        for search in ("select(.id >", "/runs?per_page", "--workflow "):
            self.assertNotIn(
                search, joined,
                "the seal searches for its run (%r). daily-news.yml runs several "
                "times a day, so a search can seal a run this prebuild never "
                "made." % search)

    def test_the_workflow_has_a_fixed_concurrency_group(self):
        doc = W.load_workflow(PREBUILD_WORKFLOW)
        conc = doc.get("concurrency") or {}
        self.assertEqual("digest-prebuild", str(conc.get("group") or ""),
                         "the prebuild needs a FIXED concurrency group so the "
                         "retry cron queues behind the primary. A group keyed on "
                         "the run id would let both run at once and race on the "
                         "same Pages deploy and the same artifact key.")
        self.assertEqual("false", str(conc.get("cancel-in-progress") or ""),
                         "cancel-in-progress: true would let the 07:05 retry kill "
                         "a 06:50 prebuild that was about to seal.")

    def test_the_workflow_declares_the_permissions_it_uses(self):
        doc = W.load_workflow(PREBUILD_WORKFLOW)
        top = str((doc.get("permissions") or {}).get("actions") or "")
        self.assertEqual("write", top,
                         "the workflow needs actions: write for the seal's "
                         "supersede dispatch to daily-news.yml. The prebuild's own "
                         "dispatch is made by the BUILD (daily-news.yml), which "
                         "carries its own actions: write.")
        seal_perms = job_of(PREBUILD_WORKFLOW, "seal").get("permissions") or {}
        # download-artifact@v4 with `run-id` is an Actions API READ. Without it
        # the seal job 403s on the download after the conclusion gate has already
        # passed -- a real defect found on self-review, not by a test.
        #
        # U8 WIDENED THIS TO `write`, AND THAT IS A REAL CHANGE THAT HAD TO BE
        # PAID FOR. This assertion used to be `read`, with the reason "the seal
        # job commits nothing" -- which was true and stopped being true when U8
        # gave the job the delivery-ledger mirror to commit. A permission that
        # cannot commit cannot record, and a record nothing commits does not
        # exist.
        #
        # SO THE PERMISSION IS NOT SIMPLY RELAXED. What replaces the old
        # assertion is a NARROWER one: the commit must exist, and it must stage
        # exactly one path. A `contents: write` that could stage the digest
        # pages or a workflow would be a far larger blast radius than the
        # mirror needs, and that is the thing actually worth guarding.
        self.assertEqual("write", str(seal_perms.get("contents") or ""),
                         "the seal job commits the delivery-ledger mirror, which "
                         "needs contents: write. Its permissions are %r"
                         % (seal_perms,))
        mirror_commit = step_named(PREBUILD_WORKFLOW, "seal",
                                   "Commit the delivery ledger mirror")["run"]
        staged = re.findall(r"git add ([^\n]+)", mirror_commit)
        self.assertEqual(
            1, len(staged),
            "the mirror commit must stage exactly one path. A contents: write "
            "that could stage the digest pages, a workflow or a state marker is "
            "a much larger blast radius than recording the ledger needs. "
            "Staged: %r" % (staged,))
        self.assertEqual("state/digest_slot_ledger.json", staged[0].strip(),
                         "the mirror commit stages %r" % (staged,))
        for forbidden in ("git add .", "git add -A", "git commit -a",
                          "git add state/history", "git add docs/",
                          "git add .github"):
            self.assertNotIn(forbidden, mirror_commit,
                             "the mirror commit reaches beyond its own file: %r"
                             % forbidden)
        # U7 WIDENED THIS FROM `read` TO `write`, AND THE GUARD MOVED WITH IT.
        # It used to assert `read` AND separately assert "not write", the second
        # being the deliberate U6 rule that a bug in this job must not be able
        # to dispatch anything. U7 gave the job a dispatching duty -- the
        # supersede of a degraded push -- so that rule can no longer be stated
        # as "it cannot dispatch". What replaces it is the narrower, checkable
        # claim below: exactly one dispatch endpoint, to daily-news.yml, with
        # phase=full, and nothing else in this job that dispatches.
        self.assertEqual(
            "write", str(seal_perms.get("actions") or ""),
            "the seal job dispatches the supersede of a degraded push, which "
            "needs actions: write, and it writes digest:ledger, which needs a "
            "reachable Actions token. Its permissions are %r" % (seal_perms,))
        supersede_raw = str(step_named(PREBUILD_WORKFLOW, "seal",
                                       "Supersede a degraded push with the full digest")["run"])
        supersede_run = shell_commands_only(supersede_raw)
        self.assertEqual(
            1, supersede_raw.count("/dispatches"),
            "the seal job must reach exactly ONE dispatch endpoint. More than "
            "one is a second capability nobody enumerated:\n%s" % supersede_run)
        self.assertIn(
            "workflows/daily-news.yml/dispatches", supersede_raw,
            "the seal job dispatches something other than daily-news.yml")
        # COMMAND position, not the bare substring: the step reads the KV
        # namespace id from the CF_KV_NAMESPACE_ID repository variable, and an
        # `echo` telling the reader to run something is not an invocation.
        for forbidden in (r"(?:^|[|;&(]\s*|npx\s+)wrangler\b",
                          r"pages/deploy", r"\bgit\s+push\b"):
            self.assertIsNone(
                re.search(forbidden, supersede_run),
                "the supersede step invokes %r. It hands over an artifact and "
                "asks another workflow to run; it must not publish anything "
                "itself:\n%s" % (forbidden, supersede_run))

    def test_no_step_in_the_prebuild_job_reads_a_clock_to_choose_a_slot(self):
        """A closed this by DELETION rather than by care: the old job resolved a
        slot from `github.event.schedule` and the Nepal date from the firing
        instant, and the defect that produced the 19-hours-early page was a
        derivation that read the wall clock instead. The build resolves its own
        firing (executed by test_workflow_lint's Check 19) and the seal is TOLD
        `digest_time` and `slot_date` outright, so the hazard left in THIS file is
        a clock read creeping back into the seal."""
        joined = "\n".join(v for _n, v in run_blocks(PREBUILD_WORKFLOW, "seal"))
        for clock in (".hour", "date +%H", "date -u +%H", "slot=morning"):
            self.assertNotIn(
                clock, joined,
                "the seal job reads or derives %r, so a slot or a date could be "
                "chosen by when the job happened to start rather than by the "
                "firing it belongs to.\n%s" % (clock, joined))

    def test_the_slot_and_its_date_arrive_as_declared_inputs(self):
        """The seal cannot guess a slot, so both values it files the artifact
        under must be declared `workflow_dispatch` inputs. An input the workflow
        does not declare is a 422, which looks exactly like a working trigger
        that sealed nothing."""
        env = (step_named(PREBUILD_WORKFLOW, "seal",
                          "Assert the rendered page and build the artifact record")
               .get("env") or {})
        self.assertEqual("${{ github.event.inputs.digest_time }}",
                         str(env.get("SLOT") or ""),
                         "the seal's slot is not the input the Worker sent.")
        self.assertEqual("${{ github.event.inputs.slot_date }}",
                         str(env.get("TODAY") or ""),
                         "the seal's date is not the input the build sent, so a "
                         "seal retried after Nepal midnight could file the "
                         "artifact under the wrong day.")


# ==========================================================================
# DEFECT #1 (CRITICAL, 2026-10-11). The dispatch job could not run `gh run
# watch` AT ALL.
#
# That job has no `actions/checkout` step -- only `seal` has one -- and `gh`
# resolves the repository it is watching by invoking git to discover the base
# repo. So all six prebuild runs died in ~70ms with
#
#     failed to determine base repo: fatal: not a git repository
#     gh run watch exit: 1
#     run conclusion: ''
#     ##[error]daily-news run ... FAILED ... Nothing will be sealed
#
# while every build they had dispatched went on to SUCCEED. Six working builds
# abandoned, zero artifacts sealed, and the owner told the digest was
# unavailable. A green `dispatch` job would not have been survivable either:
# the prebuild exists to hand a SEALED artifact over, so a prebuild that cannot
# watch cannot seal.
# ==========================================================================

#: A. THE TWO STEPS THAT REPLACED THE DELETED `dispatch` JOB.
#:
#: `Dispatch the prebuild and wait for it` is gone. What replaced it is a pair:
#: the BUILD (daily-news.yml) hands the seal its OWN run id as its last step, and
#: the SEAL (digest-prebuild.yml) reads that run's conclusion in a bounded poll
#: before it touches the artifact. Both are executed below, each in its own
#: harness, because each carries one of the two hazards the deleted job carried:
#: a successful build nobody seals, and a build whose result nobody could read.
CONFIRM_STEP = "Confirm the prebuild run succeeded before touching anything"
HANDOFF_WORKFLOW = "daily-news.yml"
HANDOFF_JOB = "generate-digest"
DATE_STEP = "Determine date, digest time and trigger source"
HANDOFF_STEP = "Hand this run to the seal"

#: An offline model of the parts of the GitHub CLI this step uses.
#:
#: The ONE behaviour modelled here that matters is BASE REPO RESOLUTION, and it
#: is not invented. gh 2.98.0 -- the version this runner ships, per the ceiling
#: comment in the step -- was run directly on 2026-10-11 in a directory holding
#: no repository, and reproduced the production failure verbatim:
#:
#:     without GH_REPO -> "failed to determine base repo: failed to run git:
#:                        fatal: not a git repository (or any of the parent
#:                        directories): .git";  exit 1
#:     with    GH_REPO -> skips git entirely and goes straight to the Actions
#:                        API, failing only on its credentials;  exit 1
#:
#: So `run watch` needs a repository and honours GH_REPO as the override, while
#: `gh api` against a full URL does not -- which is exactly why the dispatch
#: POST and the run-id poll kept working on the runner and the watch alone
#: killed the job.
#:
#: It is a MODEL rather than the real binary on purpose: the real one needs a
#: network and credentials this suite must not have, and a test that could only
#: run in CI is decoration. The control test below proves the model
#: discriminates, so it cannot pass vacuously.
_GH_STUB = r"""#!/usr/bin/env bash
# `gh api --jq <expr>` applies the filter ITSELF and prints the RESULT, which is
# why the step's `BEFORE="$(... --jq '.workflow_runs[0].id // 0')"` is a number
# and not a JSON blob. So the filter is extracted and applied with real jq --
# also present on this runner, and reading committed fixtures elsewhere in this
# suite -- rather than the stub printing raw JSON.
filter=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--jq" ]; then filter="$a"; fi
  prev="$a"
done
emit() {
  if [ -n "$filter" ]; then printf '%s' "$1" | jq -r "$filter"; else printf '%s' "$1"; fi
}
args="$*"
# Every invocation in order, so a test can assert WHICH call was made rather
# than only that the step survived.
printf '%s\n' "$args" >> "${STUB_GH_LOG:-/dev/null}"
case "$args" in
  *"run watch"*)
    if [ -z "${GH_REPO:-}" ]; then
      echo "failed to determine base repo: failed to run git: fatal: not a git repository (or any of the parent directories): .git" >&2
      exit 1
    fi
    echo "[stub gh] watching with GH_REPO=$GH_REPO" >&2
    exit "${STUB_WATCH_RC:-0}"
    ;;
esac
case "$args" in
  # The real endpoint returns an OBJECT with a workflow_runs array -- both the
  # "highest existing id" read and the "> BEFORE" poll index `.workflow_runs`,
  # so a bare array would fail jq for a reason that has nothing to do with gh.
  *"?per_page=1"*)              emit "{\"workflow_runs\": [{\"id\": ${STUB_GH_BEFORE:-1}}]}" ;;
  *"?event=workflow_dispatch"*) emit "{\"workflow_runs\": [{\"id\": ${STUB_AFTER_ID:-2}}]}" ;;
  # A REFUSED dispatch, for the hand-off tests: what a revoked token or a
  # renamed workflow looks like. Default: accepted, silently and with no body,
  # which is what the real endpoint returns (204).
  *"/dispatches"*)
    if [ -n "${STUB_DISPATCH_FAIL:-}" ]; then
      echo "gh: HTTP 404: Not Found (${STUB_DISPATCH_FAIL})" >&2
      exit "${STUB_DISPATCH_RC:-1}"
    fi
    ;;
  # THE RUN OBJECT, CARRYING BOTH FIELDS THE STEP READS.
  #
  # The step asks for `[.conclusion // "", .updated_at] | @tsv` -- ONE call for
  # two fields of the same object -- so this arm must answer that exact filter.
  # A one-key object is the wrong model: it yields an empty SECOND FIELD, which
  # would look like a production fault that does not exist, and would have made
  # every retry test below prove the wrong thing.
  #
  # It also models the fault that actually cost run 38248516404 its cycle: the
  # call fails, loudly and to stderr, for the first STUB_META_FAIL_TIMES
  # attempts, exactly as `gh: Server Error (HTTP 502)` did. Every attempt is
  # counted into STUB_META_LOG, so a test can assert HOW MANY times the step
  # asked rather than merely that it survived.
  # `${STUB_GH_CONCLUSION-success}` AND NOT `-:-`. `:-` substitutes the default
  # when the variable is unset OR EMPTY, so the empty-conclusion case below
  # would silently model a SUCCESSFUL build and the test guarding it would
  # prove nothing.
  *"conclusion"*|*"updated_at"*)
    prev_count="$(tail -n 1 "${STUB_META_LOG:-/dev/null}" 2>/dev/null || true)"
    meta_calls=$(( ${prev_count:-0} + 1 ))
    echo "$meta_calls" >> "${STUB_META_LOG:-/dev/null}"
    if [ "$meta_calls" -le "${STUB_META_FAIL_TIMES:-0}" ]; then
      echo "gh: Server Error (HTTP 502)" >&2
      exit 1
    fi
    emit "{\"conclusion\": \"${STUB_GH_CONCLUSION-success}\", \"updated_at\": \"${STUB_UPDATED_AT-2026-10-10T18:40:00Z}\"}"
    ;;
  *)                            emit "{\"conclusion\": \"${STUB_GH_CONCLUSION-success}\"}" ;;
esac
"""

#: A `sleep` FUNCTION, installed through `BASH_ENV`, that records the wait
#: instead of performing it.
#:
#: It has to be a function rather than a stub binary on `PATH`. Git-for-Windows
#: bash resolves `sleep` to `/usr/bin/sleep` no matter what a Windows-style
#: `PATH` says -- verified: with the stub directory prepended, `which gh` finds
#: the stub and `which sleep` still finds `/usr/bin/sleep`. `BASH_ENV` is
#: sourced by `bash -c` before a non-interactive script runs, and a shell
#: FUNCTION shadows any external command, so the step's own text executes
#: verbatim and only its environment is controlled -- which is exactly what the
#: `PATH` and `GITHUB_OUTPUT` of this harness already do.
#:
#: The point of recording the argument is that `sleep 0` and `sleep 5` are the
#: same command. A backoff that silently computes to zero -- which is what
#: happens when the loop variable is `_`, because the `META=""` on the line
#: above overwrites bash's `$_` -- passes every behavioural test in the suite,
#: because hammering a load-shedding API four times in a second and waiting
#: between them both eventually recover.
_SLEEP_HOOK = """sleep() { echo "${1:-0}" >> "${STUB_SLEEP_LOG:?}"; return 0; }
"""


class ConfirmPollHarness:
    """The SEAL's confirm-poll shell, run in a workspace with no repository.

    This is the harness that used to run the deleted `dispatch` job, re-pointed
    at the step that replaced it: the seal's own bounded read of the build run's
    conclusion. The modelled `gh` is unchanged and deliberately so -- the poll
    asks the same question the deleted job ended on (the run object carrying
    `conclusion` and `updated_at`), so the retry cases in
    `ConfirmPollRetryTests` still execute the real shell against the real model
    rather than against a reimplementation of it.
    """

    def step_env(self, extra=None, drop=()):
        step = step_named(PREBUILD_WORKFLOW, "seal", CONFIRM_STEP)
        env = {}
        for key, raw in (step.get("env") or {}).items():
            text = str(raw)
            text = text.replace("${{ github.repository }}", "looping-forever/daily-media-news")
            text = text.replace("${{ github.api_url }}", "https://api.github.invalid")
            text = text.replace("${{ secrets.GITHUB_TOKEN }}", "stub-token")
            text = text.replace("${{ github.event.inputs.run_id }}", "2")
            self.assertNotIn("${{", text,
                             "env[%s] carries an expression this harness does "
                             "not resolve: %r" % (key, text))
            env[key] = text
        for name in drop:
            env.pop(name, None)
        env.update({k: str(v) for k, v in (extra or {}).items()})
        return env

    def run_confirm_poll(self, extra=None, drop=()):
        bin_dir = self.tmp / "bin"
        W.install_stub_on_path(bin_dir, _GH_STUB, name="gh")
        # `sleep` is shadowed by a FUNCTION through BASH_ENV, not by a binary on
        # PATH: Git-for-Windows bash resolves `sleep` to /usr/bin/sleep whatever
        # PATH says, and a retry that really sleeps costs this suite 50s a case.
        # The step's own text is executed verbatim; only its environment moves.
        hook = self.tmp / "bash_env.sh"
        hook.write_text(_SLEEP_HOOK, encoding="utf-8", newline="\n")
        (self.tmp / "gh_out.txt").write_text("", encoding="utf-8")
        (self.tmp / "meta_calls.txt").write_text("", encoding="utf-8")
        (self.tmp / "sleep_calls.txt").write_text("", encoding="utf-8")
        env = self.step_env(extra, drop)
        env["PATH"] = "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))
        env["GITHUB_OUTPUT"] = str(self.tmp / "gh_out.txt")
        env["RUNNER_TEMP"] = str(self.tmp)
        env["STUB_META_LOG"] = str(self.tmp / "meta_calls.txt")
        env["STUB_SLEEP_LOG"] = str(self.tmp / "sleep_calls.txt")
        env["BASH_ENV"] = str(hook)
        step = step_named(PREBUILD_WORKFLOW, "seal", CONFIRM_STEP)
        proc = W.run_bash_script(step["run"], env_overrides=env, cwd=str(self.tmp))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def meta_calls_made(self):
        """How many times the step asked the API for the run's own metadata."""
        text = (self.tmp / "meta_calls.txt").read_text(encoding="utf-8")
        return len([line for line in text.splitlines() if line.strip()])

    def backoffs_asked_for(self):
        """Every duration the step asked `sleep` for, in order."""
        text = (self.tmp / "sleep_calls.txt").read_text(encoding="utf-8")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def outputs_produced(self):
        return (self.tmp / "gh_out.txt").read_text(encoding="utf-8")


class HandoffStepHarness:
    """The BUILD's hand-off step, run in a workspace with no repository.

    This is what replaced the deleted `dispatch` job's dispatch-and-wait. It runs
    in the build's own job -- which DOES have a checkout -- so the hazard it
    carries is the mirror image of defect #1: not "the step died because it
    resolved a repository by running git", but "does the step still work when
    there is no repository at all?". It dials the Actions API with an absolute
    URL, so `gh` never resolves a base repo, and both halves of that are asserted
    below rather than assumed.
    """

    def step_env(self, extra=None, drop=()):
        step = step_named(HANDOFF_WORKFLOW, HANDOFF_JOB, HANDOFF_STEP)
        env = {}
        for key, raw in (step.get("env") or {}).items():
            text = str(raw)
            text = text.replace("${{ github.repository }}", "looping-forever/daily-media-news")
            text = text.replace("${{ github.api_url }}", "https://api.github.invalid")
            text = text.replace("${{ secrets.GITHUB_TOKEN }}", "stub-token")
            text = text.replace("${{ github.run_id }}", "4242")
            text = text.replace("${{ steps.digest_time.outputs.time }}", "evening")
            text = text.replace("${{ env.TODAY }}", "2026-10-09")
            self.assertNotIn("${{", text,
                             "env[%s] carries an expression this harness does "
                             "not resolve: %r" % (key, text))
            env[key] = text
        for name in drop:
            env.pop(name, None)
        env.update({k: str(v) for k, v in (extra or {}).items()})
        return env

    def run_handoff_step(self, extra=None, drop=()):
        bin_dir = self.tmp / "bin"
        W.install_stub_on_path(bin_dir, _GH_STUB, name="gh")
        (self.tmp / "gh_calls.txt").write_text("", encoding="utf-8")
        env = self.step_env(extra, drop)
        env["PATH"] = "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))
        env["STUB_GH_LOG"] = str(self.tmp / "gh_calls.txt")
        step = step_named(HANDOFF_WORKFLOW, HANDOFF_JOB, HANDOFF_STEP)
        proc = W.run_bash_script(step["run"], env_overrides=env, cwd=str(self.tmp))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def gh_calls(self):
        """Every `gh` invocation the step made, in order."""
        text = (self.tmp / "gh_calls.txt").read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.strip()]


class HandoffStepTests(HandoffStepHarness, TempDirCase):
    """The hand-off step's REAL SHELL, in a workspace with no repository.

    THE HAZARD, and why this class exists at all. This is the LAST thing the build
    run does, and it is the only thing that tells the seal a run needs sealing.
    If it fails, the build goes RED and the owner loses the slot -- loud. If it
    silently dispatched nothing, the build would be GREEN and the seal would never
    run, which is defect #1's exact shape. So: exactly one dispatch, naming the
    three inputs the seal declares, or a loud failure.
    """

    def test_the_workspace_really_has_no_repository(self):
        """The precondition for every test here, asserted rather than assumed.

        A temp directory inside somebody's checkout would quietly restore the
        very `.git` whose absence defect #1 was about.
        """
        here = self.tmp.resolve()
        probe = here
        while True:
            self.assertFalse((probe / ".git").exists(),
                             "a parent of the test workspace is a git "
                             "repository (%s), so this suite would be testing a "
                             "directory that HAS one." % probe)
            if probe.parent == probe:
                break
            probe = probe.parent
        self.assertFalse((here / ".git").exists())

    def test_the_build_hands_the_seal_its_own_run_id(self):
        """THE HEADLINE. One dispatch, naming the run the seal must confirm.

        The run id is the whole reason the old dispatcher existed: a
        `workflow_dispatch` response carries no id, so a second job used to sit
        alive for up to fifty minutes to learn one. A run always knows its own,
        and this asserts it is what gets sent.
        """
        code, out = self.run_handoff_step()
        self.assertEqual(
            0, code,
            "the hand-off step FAILED, so a build that succeeded would never be "
            "sealed and the Worker's delivery arm would have to build the digest "
            "itself at its own cron.\n%s" % out)
        calls = self.gh_calls()
        self.assertEqual(
            1, len(calls),
            "the hand-off made %d `gh` calls rather than one: %r"
            % (len(calls), calls))
        call = calls[0]
        self.assertIn("digest-prebuild.yml/dispatches", call)
        self.assertIn("inputs[run_id]=4242", call,
                      "the seal was not told which run to confirm: %r" % call)
        self.assertIn("inputs[digest_time]=evening", call)
        self.assertIn("inputs[slot_date]=2026-10-09", call)
        self.assertIn("ref=master", call)
        self.assertIn("run 4242", out)

    def test_the_step_needs_no_repository_and_no_gh_repo(self):
        """DEFECT #1 CANNOT RECUR HERE, and this is what proves it.

        The deleted `dispatch` job died because `gh run watch` resolved its
        repository by running git in a job that had no checkout. This step dials
        the API with an absolute URL instead. GH_REPO -- the variable whose
        absence killed the old job -- is removed to make the point: it changes
        nothing.
        """
        code, out = self.run_handoff_step(drop=("GH_REPO",))
        self.assertEqual(
            0, code,
            "the hand-off step needed a repository, or a GH_REPO that does not "
            "exist in this workspace, so it repeats defect #1 in a new place.\n%s"
            % out)
        self.assertNotIn("failed to determine base repo", out)

    def test_a_refused_dispatch_fails_the_build_loudly(self):
        """The other half, and the one a lazy fix breaks.

        A token that has been revoked dispatches nothing. If that left the build
        green, the owner would learn about the missed slot from the absence of a
        digest -- defect #1 wearing a green tick.
        """
        code, out = self.run_handoff_step({"STUB_DISPATCH_FAIL": "1"})
        self.assertNotEqual(
            0, code,
            "the dispatch was REFUSED and the hand-off step went GREEN, so the "
            "owner learns about the missed slot from the absence of a push.\n%s"
            % out)
        self.assertIn("HTTP 404", out, out)

    def test_an_empty_slot_or_date_is_refused_before_anything_is_dispatched(self):
        """The guard runs BEFORE the POST, not after.

        A dispatch naming an empty digest_time asks the seal to seal a slot that
        does not exist; the seal would refuse it, and the build would have gone
        green having dispatched it.
        """
        for drop in (("SLOT",), ("SLOT_DATE",)):
            with self.subTest(drop=drop):
                code, out = self.run_handoff_step(drop=drop)
                self.assertNotEqual(0, code, out)
                self.assertIn("::error::", out, out)
                self.assertEqual(
                    [], self.gh_calls(),
                    "the step dispatched a slot or a date it had already found "
                    "empty.")

    def test_the_step_dials_the_endpoint_the_dispatch_census_can_see(self):
        """Static, and deliberately so.

        `test_workflow_lint` counts dispatch sites by matching the endpoint
        STRING. A step rewritten to `gh workflow run` -- which resolves its
        repository by running git, i.e. defect #1 -- would still work and would
        silently drop out of that census, so the exact form is pinned here.
        """
        body = step_named(HANDOFF_WORKFLOW, HANDOFF_JOB, HANDOFF_STEP)["run"]
        self.assertIn("digest-prebuild.yml/dispatches", body)
        self.assertIn("inputs[run_id]", body)


# ==========================================================================
# DEFECT #7 (2026-10-11, prebuild run 38248516404). A TRANSIENT GitHub 502 on a
# METADATA READ killed an otherwise fully successful prebuild.
#
# The dispatch job did everything right and then lost the cycle eleven seconds
# before handing over to the seal job:
#
#     dispatched run id: 38248543795
#     gh run watch exit: 0
#     run conclusion: 'success'
#     gh: Server Error (HTTP 502)
#     ##[error]Process completed with exit code 1.
#
# The build HAD ALREADY SUCCEEDED. The line that died read `.updated_at` --
# metadata, for the freshness stamp -- and did so with no retry, under
# `set -eo pipefail`. Because `seal` is gated on `dispatch`, the artifact was
# never sealed and `digest:artifact:*` was never created, so the Worker's
# delivery arm had to build the digest itself at its own cron.
#
# WHAT THIS CLASS HOLDS. Three properties, and the order matters: the retry
# must EXIST, it must WAIT, and it must still FAIL CLOSED. A fix that only
# added `|| true` would pass the first and be worse than the defect.
# ==========================================================================

class ConfirmPollRetryTests(ConfirmPollHarness, TempDirCase):
    def test_the_harness_sleep_shadow_is_actually_in_force(self):
        """THE CONTROL for the backoff assertion below.

        If `sleep` were not a function here, the backoff test would see an empty
        log and fail -- but it would fail for the wrong reason, and the fix
        would be a man-in-the-middle on PATH that this runner ignores anyway.
        So the shadow is asserted directly, and the failure says which of the
        two things broke.
        """
        (self.tmp / "sleep_calls.txt").write_text("", encoding="utf-8")
        (self.tmp / "bash_env.sh").write_text(_SLEEP_HOOK, encoding="utf-8",
                                              newline="\n")
        env = self.step_env()
        env["STUB_SLEEP_LOG"] = str(self.tmp / "sleep_calls.txt")
        env["BASH_ENV"] = str(self.tmp / "bash_env.sh")
        proc = W.run_bash_script("type -t sleep", env_overrides=env,
                                 cwd=str(self.tmp))
        self.assertEqual(
            "function", (proc.stdout or "").strip(),
            "this harness no longer shadows `sleep`, so the backoff "
            "assertions below are measuring the wrong thing. A stub BINARY on "
            "PATH does not work here: Git-for-Windows bash resolves `sleep` "
            "to /usr/bin/sleep regardless. (stdout=%r stderr=%r)"
            % (proc.stdout, proc.stderr))

    def test_one_transient_502_does_not_cost_the_owner_the_digest(self):
        """THE HEADLINE. Run 38248516404, replayed.

        Two of four attempts fail with the exact error that killed the run, the
        third succeeds, and the step must go on to hand over. Before the fix
        this died on the first attempt, taking a build that had already
        succeeded with it.
        """
        code, out = self.run_confirm_poll({"STUB_GH_CONCLUSION": "success",
                                           "STUB_META_FAIL_TIMES": "2"})
        self.assertEqual(
            0, code,
            "a TRANSIENT 502 on the run read cost the prebuild its cycle, which "
            "is exactly what happened in run 38248516404 after the build had "
            "already succeeded.\n%s" % out)
        self.assertIn("prebuild run 2 conclusion: 'success'", out)
        self.assertIn("::warning::", out,
                      "the two recovered attempts left no warning in the log, so "
                      "a reader cannot tell a recovered read from a first-try "
                      "one.")
        self.assertIn("conclusion=success", self.outputs_produced())
        self.assertIn("built_at=2026-10-10T18:40:00Z", self.outputs_produced(),
                      "built_at must survive the retry too. It arrives in the "
                      "SECOND field of the same call, and it is written only to "
                      "the step outputs -- which is where the seal reads it.")

    def test_the_step_actually_reissued_the_call(self):
        """The claim above is only evidence if the call was really repeated.

        A step that somehow succeeded on one 502-free attempt -- or that
        swallowed the error without asking again -- would also report success,
        and would report it having learned nothing.
        """
        code, _out = self.run_confirm_poll({"STUB_WATCH_RC": "0",
                                            "STUB_GH_CONCLUSION": "success",
                                            "STUB_META_FAIL_TIMES": "2"})
        self.assertEqual(0, code)
        self.assertEqual(
            3, self.meta_calls_made(),
            "two attempts failed, so the step must have asked THREE times. It "
            "asked %d. If that is 1, the retry is not there; if it is more than "
            "3, the bounded backoff is not bounding anything."
            % self.meta_calls_made())

    def test_the_happy_path_still_makes_exactly_one_call(self):
        """The two unretried reads became ONE, and that is half the fix.

        Two sequential calls over one object means two chances to lose the cycle
        on a flaky read, and the second existed only to fetch a second field.
        """
        self.assertEqual(
            0, self.run_confirm_poll({"STUB_WATCH_RC": "0",
                                       "STUB_GH_CONCLUSION": "success"})[0])
        self.assertEqual(
            1, self.meta_calls_made(),
            "the step asked for the run's metadata %d times on a healthy run. "
            "Both fields come from one object and must come from one call."
            % self.meta_calls_made())

    def test_the_poll_actually_waits_between_attempts(self):
        """A wait of 0 is not a wait, and twenty of them are a hammer.

        The poll re-asks a load-shedding API until the run concludes, so every
        wait must be a real, non-zero, FIXED interval. A computed interval is
        the trap: `sleep $((attempt * 15))` collapses to `sleep 0` the moment
        anything above it clobbers bash's `$_`, and a shorter wait would turn a
        bounded poll into its whole attempt budget inside a second. The band is
        5 attempts at 6s since 2026-10-20 (was 20 at 15s) because the run has
        already concluded by the time the seal is dialled.
        """
        self.run_confirm_poll({"STUB_GH_CONCLUSION": "success",
                               "STUB_META_FAIL_TIMES": "3"})
        waits = self.backoffs_asked_for()
        self.assertTrue(
            waits,
            "the step never waited at all, so three failures were retried back "
            "to back. Either the poll lost its sleep, or this harness stopped "
            "shadowing sleep -- check BASH_ENV before believing the first.")
        self.assertEqual(
            [6] * len(waits), [int(w or 0) for w in waits],
            "the poll must wait 6s between attempts; it asked for %s" % waits)

    def test_a_permanently_unreadable_result_still_fails_closed(self):
        """THE HALF THAT MATTERS MOST. A retry must not become a shrug.

        Every attempt fails, so the step cannot know whether the build
        succeeded. It must exit non-zero, say why, and hand the seal job
        nothing -- never an empty or assumed conclusion.
        """
        code, out = self.run_confirm_poll({"STUB_WATCH_RC": "0",
                                            "STUB_GH_CONCLUSION": "success",
                                            "STUB_META_FAIL_TIMES": "99"})
        self.assertNotEqual(
            0, code,
            "the step went GREEN with the run's own conclusion UNREADABLE. A "
            "prebuild that cannot read its result must not report one, or the "
            "seal job runs against a conclusion nobody ever verified.\n%s" % out)
        self.assertIn("::error::", out,
                      "the failure produced no ::error:: annotation, so the run "
                      "log says nothing about why.\n%s" % out)
        self.assertEqual(
            5, self.meta_calls_made(),
            "the poll must be BOUNDED. It asked %d times; an unbounded retry "
            "against an API that is refusing would hold the seal open until its "
            "own ceiling instead of failing." % self.meta_calls_made())
        produced = self.outputs_produced()
        self.assertNotIn("conclusion=", produced,
                         "an unreadable conclusion was still published to the "
                         "step outputs, where the seal job reads it: %r"
                         % produced)
        self.assertNotIn("built_at=", produced,
                         "an unreadable built_at was still published, which is "
                         "the field whose failure cost run 38248516404 its "
                         "cycle: %r" % produced)

    def test_the_failure_says_the_build_may_be_fine_and_nothing_is_sealed(self):
        """An operator reading a red prebuild must not panic-seal by hand.

        The honest reading is "we could not read the result", which is not the
        same as "the digest failed", and the message has to distinguish them.
        """
        _code, out = self.run_confirm_poll({"STUB_WATCH_RC": "0",
                                            "STUB_GH_CONCLUSION": "success",
                                            "STUB_META_FAIL_TIMES": "99"})
        self.assertIn("Nothing is sealed", out, out)
        self.assertIn("cannot tell a successful build from a failed one", out, out)
        self.assertIn("delivery arm", out, out)

    def test_the_retry_cannot_smuggle_a_failed_build_through(self):
        """THE ONE THAT MAKES THIS A RESILIENCE FIX AND NOT AN ASSERTION CHANGE.

        Two attempts fail and the third reports `failure`. Retrying must not
        change what the step decides: a build that failed is still a build that
        failed, however many times it is asked.
        """
        for conclusion in ("failure", "cancelled", "timed_out", "skipped"):
            with self.subTest(conclusion=conclusion):
                code, out = self.run_confirm_poll(
                    {"STUB_WATCH_RC": "0", "STUB_GH_CONCLUSION": conclusion,
                     "STUB_META_FAIL_TIMES": "2"})
                self.assertNotEqual(
                    0, code,
                    "a build whose conclusion is %r went GREEN after a retry. "
                    "The retry must not make the verdict easier to reach.\n%s"
                    % (conclusion, out))
                self.assertIn("::error::", out, out)
                self.assertEqual(3, self.meta_calls_made())

    def test_an_empty_conclusion_is_never_read_as_success(self):
        """The narrowest fail-closed case, and the one a fresh dispatch creates.

        A run dispatched seconds ago is `in_progress` and answers with an EMPTY
        conclusion -- which is why this poll WAITS rather than deciding on the
        first read. Exhausting that wait must still be a failure: an empty
        conclusion is never a pass, and the seal must never write an artifact
        from a run nobody could read.
        """
        code, out = self.run_confirm_poll({"STUB_GH_CONCLUSION": ""})
        self.assertNotEqual(0, code,
                            "an empty conclusion let the prebuild report "
                            "success.\n%s" % out)
        self.assertIn("::error::", out, out)
        self.assertEqual(5, self.meta_calls_made(),
                         "an empty conclusion must be retried up to the bound "
                         "and no further; it asked %d times."
                         % self.meta_calls_made())


# ==========================================================================
# DEFECT #4 (HIGH, 2026-10-10). The slot's DATE came from the wall clock while
# the slot's NAME came from the cron.
#
# Run 38076000974 started at 2026-10-10T18:29:48Z -- 00:29 NPT on the 11th --
# and reported "TODAY: 2026-10-11 / DIGEST_TIME: evening", rendering
# docs/digests/2026-10-11-evening.html for the 10th's evening slot. The watchdog
# then demanded a 2026-10-10-evening record that can never exist, because the
# only evening digest in existence had been filed under the wrong day.
#
# THE RULE: a slot is the SCHEDULED slot. An (NPT date, morning|evening) pair,
# both halves from the same firing. Delay moves the job; it does not renumber
# it -- which is the anchoring digest-watchdog.yml's digest_due() already uses.
#
# EXECUTED, not asserted about, because "which day is this?" answered by the
# clock reads perfectly well. The clock is pinned so the post-midnight build is
# a fact rather than a hope about what time the suite happens to run.
# ==========================================================================

class SlotDateIsTheScheduledSlotTests(TempDirCase):
    """The date step's real heredoc, executed at chosen instants.

    RE-POINTED on 2026-10-20 with item A: digest-prebuild.yml's `dispatch` job
    held a copy of the slot rule and the job was deleted, so the copy that now
    runs is daily-news.yml's `Determine date, digest time and trigger source`
    step. The rule did not move -- both halves of a slot still come from the same
    scheduled firing, and a named `slot_date` still wins -- which is why these
    cases still execute rather than being rewritten as assertions about text.

    Driven with the instants the WORKER dispatches on: the step's own
    `schedule:` crons are read from PREBUILD_DISPATCH_CRONS, the same two
    firings `deploy/schedule.json` declares.
    """

    def date_body(self):
        """The date step's real Python heredoc, from the file that RUNS it.

        Re-pointed on 2026-10-20 (item A) for the reason the class docstring
        gives: the copy in digest-prebuild.yml's deleted `dispatch` job is gone,
        and this is the copy that executes.
        """
        return python_heredocs(
            step_named(HANDOFF_WORKFLOW, HANDOFF_JOB, DATE_STEP)["run"])[0]

    def resolve(self, schedule, now_iso, event="schedule", digest_time="",
                slot_date=""):
        """The date the date step derives, executed at a chosen instant.

        The heredoc PRINTS the date and the step captures it as `today`, so the
        date is read from the output TEXT rather than from `GITHUB_OUTPUT` --
        the step's `echo ... >> "$GITHUB_OUTPUT"` lines sit outside the heredoc
        and are not reached here. `emitted` is deliberately EMPTY when the step
        did not succeed, which is what makes "a refused date emitted nothing"
        assertable. stderr is captured because this step reports its FATAL there.
        """
        # NO SUBSTITUTION HERE ANY MORE (2026-10-26). This helper used to model
        # the step's dispatch branch by filling in the named slot's own cron when
        # no schedule was given -- which is exactly the walk-back that dated two
        # undated dispatches to 2026-10-25 while they meant the 26th. The step no
        # longer keeps those literals, so a dispatch that names no date reaches
        # the heredoc with an empty EVENT_SCHEDULE and must be REFUSED. Modelling
        # the guess here would hide that refusal from every test below.
        with W.frozen_datetime(now_iso):
            code, text = exec_heredoc(self.date_body(), {
                "EVENT_NAME": event,
                "EVENT_SCHEDULE": schedule,
                "INPUT_DIGEST_TIME": digest_time,
                "INPUT_SLOT_DATE": slot_date,
            }, self.tmp, capture_stderr=True)
        printed = (text or "").strip()
        # The date is the first thing the heredoc PRINTS; the NOTE a dateless
        # dispatch emits goes to stderr and is read for its wording, never for
        # the date (2026-10-30: reading the last line made the NOTE look like
        # the date, which is how a wording change could have hidden a real one).
        date = printed.splitlines()[0].strip() if printed else ""
        emitted = {"today": date} if (code == 0 and date) else {}
        return code, text, emitted

    def test_a_build_after_midnight_npt_keeps_the_PRIOR_date(self):
        """THE NAMED REQUIREMENT, at the exact instant it happened.

        2026-10-10T18:29:48Z is 00:29 NPT on the 11th -- the instant run
        38076000974 actually started, and the one that produced
        2026-10-11-evening for the 10th's evening slot.
        """
        code, out, emitted = self.resolve("55 12 * * *", "2026-10-10T18:29:48Z")
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-10", emitted.get("today"),
                         "a build that started after midnight NPT filed "
                         "the previous day's evening slot under the "
                         "new date. This is run 38076000974: it rendered "
                         "docs/digests/2026-10-11-evening.html for the "
                         "10th's slot, and the watchdog then demanded a "
                         "2026-10-10-evening record that can never "
                         "exist.\n%s" % out)

    def test_a_build_just_after_midnight_npt_keeps_the_PRIOR_date(self):
        """The edge, one minute into the new day rather than half an hour."""
        code, out, emitted = self.resolve("55 12 * * *", "2026-10-10T18:16:00Z")
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-10", emitted.get("today"), out)

    def test_a_punctual_build_is_unaffected(self):
        """The fix must not move a healthy run: 18:35 NPT on the 10th for the
        19:20 slot is the 10th."""
        code, out, emitted = self.resolve("55 12 * * *", "2026-10-10T13:20:00Z")
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-10", emitted.get("today"), out)

    def test_a_morning_build_is_anchored_to_its_own_firing(self):
        """The evening slot stays the most recent DUE one at 07:15 NPT, and the
        morning slot has not happened yet -- both must come out right, because a
        rule that only works for one of the two crons is not a rule."""
        code, out, evening = self.resolve("55 12 * * *", "2026-10-11T01:30:00Z")
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-10", evening.get("today"),
                         "at 07:15 NPT on the 11th the most recent EVENING slot "
                         "is still the 10th's: %r" % (evening,))

        code, out, morning = self.resolve("55 0 * * *", "2026-10-11T01:30:00Z")
        self.assertEqual(0, code, out)
        self.assertEqual("2026-10-11", morning.get("today"),
                         "the 06:40 NPT morning prebuild on the 11th was due at "
                         "00:55 UTC, so it belongs to the 11th: %r" % (morning,))

    def test_every_committed_firing_files_its_OWN_slot(self):
        """THE RETRY TEST, RE-STATED FOR THE SCHEDULE THAT ACTUALLY EXISTS.

        There is no retry cron any more: one slot gets one build. What remains
        of the original invariant is the half that still bites -- each firing
        must file the date of ITS OWN slot, from the SAME string its name came
        from. Both halves are asserted: the DATE is executed at the committed
        instants, and the NAME mapping is asserted on the step's own shell,
        because that mapping is shell (`time=morning`/`evening`) and not part of
        the frozen heredoc.
        """
        body = step_named(HANDOFF_WORKFLOW, HANDOFF_JOB, DATE_STEP)["run"]
        self.assertIn('if [[ "$EVENT_SCHEDULE" == "5 2 * * *" ]]', body,
                      "the scheduled path no longer takes the slot NAME from the "
                      "same string it derives the DATE from, so the two halves "
                      "of one slot can disagree.")
        for expr, slot in sorted(PREBUILD_DISPATCH_CRONS.items()):
            with self.subTest(cron=expr):
                code, out, emitted = self.resolve(expr, "2026-10-10T18:30:00Z")
                self.assertEqual(0, code, out)
                self.assertEqual("2026-10-10", emitted.get("today"),
                                 "firing %r (%s) filed itself under the wrong "
                                 "day:\n%s" % (expr, slot, out))

    # ------------------------------------------------------------------
    # THE DISPATCH BRANCH: A NAMED DATE IS OBEYED, AND NOTHING ELSE IS
    # INVENTED (2026-10-18, RETIRED 2026-10-26).
    #
    # This test used to be `test_a_manual_dispatch_still_uses_the_builds_own_
    # nepal_date` and it asserted the opposite: that a dispatch of the evening
    # slot at 07:55 NPT must claim the 11th, because "a manual dispatch has no
    # firing to belong to". That assertion WAS the defect -- it is the same
    # claim daily-news.yml's dispatch path made, and the comment it rested on
    # was wrong in the same way. Corrected here rather than deleted, so that a
    # later reader can see it was re-approved on purpose.
    #
    # The correction that replaced it -- "without a date, use the named slot's
    # most recent firing" -- was ALSO wrong, and only became visible on
    # 2026-10-26: a cron names the most recent instant it fired, so at 14:00
    # NPT on the 26th the evening slot's most recent firing is the 25th's, and
    # two undated dispatches for the 26th produced
    # state/digest-archive/2026-10-25-evening-undated.html (header 2026-10-26)
    # and ...-undated-2.html (header 2026-10-25). A dispatcher that cannot say
    # which day it means is now REFUSED, and `SLOT_CRON` is gone from the step
    # entirely -- see test_the_comment_that_argued_the_clock_was_right_is_gone.
    # ------------------------------------------------------------------

    def test_a_dispatch_naming_a_date_is_obeyed_verbatim(self):
        """A supplied slot_date WINS, and it is the only way to mean the 17th.

        The prebuild fires 40 minutes BEFORE its slot, so the clock and the
        slot's date disagree for any dispatch that lands the other side of Nepal
        midnight. That is precisely what a `slot_date` input is for.
        """
        code, out, emitted = self.resolve(
            "", "2026-10-18T14:30:00Z", event="workflow_dispatch",
            digest_time="evening", slot_date="2026-10-17")
        self.assertEqual(0, code, out)
        body = step_named(HANDOFF_WORKFLOW, HANDOFF_JOB, DATE_STEP)["run"]
        self.assertIn('time="$INPUT_DIGEST_TIME"', body,
                      "a dispatch no longer takes the slot NAME from the input "
                      "it was told, so the name and the date can disagree.")
        self.assertEqual(
            "2026-10-17", emitted.get("today"),
            "a dispatch that named 2026-10-17 for the evening slot was filed "
            "under %r instead. A supplied date is the whole point of the input; "
            "the build's own day is not a source this branch may use.\n%s"
            % (emitted.get("today"), out))

    def test_a_date_less_dispatch_is_nepal_today_not_walked_back(self):
        """THE COST OF A WALK-BACK, AT THE INSTANT IT BITES.

        2026-10-11T02:10:00Z is 07:55 NPT on the 11th. NPT-today says the 11th;
        the named evening slot's most recent firing says the 10th; and a
        dispatch at 14:00 NPT on the 26th makes the same two answers disagree
        the other way.

        CHANGED 2026-10-30 (run 41064520752). This used to assert a REFUSAL on
        the argument that both answers are a guess, and that refusal made
        `gh workflow run daily-news.yml` -- with its own defaults -- die in its
        first step. The answer that is NOT a guess for a manual dispatch is
        "now", i.e. today in Nepal; what must never come back is the walk-back
        to a firing that has already passed (2026-10-10 here), which filed two
        undated dispatches under the 25th.
        """
        body = step_named(HANDOFF_WORKFLOW, HANDOFF_JOB, DATE_STEP)["run"]
        code_only = "\n".join(line for line in body.splitlines()
                              if not line.lstrip().startswith("#"))
        self.assertNotIn("SLOT_CRON", code_only,
                         "the step is substituting a slot cron of its own again; "
                         "the slot times live in deploy/schedule.json and a "
                         "dispatch that names no date is dated to TODAY, never "
                         "to a firing that has already happened.")
        code, out, emitted = self.resolve(
            "", "2026-10-11T02:10:00Z", event="workflow_dispatch",
            digest_time="evening")
        self.assertEqual(0, code,
                         "a date-less evening dispatch still failed:\n" + out)
        self.assertEqual(
            "2026-10-11", emitted.get("today"),
            "a date-less evening dispatch at 07:55 NPT was filed under %r "
            "instead of today in Nepal (2026-10-11).\n%s"
            % (emitted.get("today"), out))
        self.assertIn("no slot_date", out,
                      "the run dated itself without saying which date it used "
                      "and why.\n" + out)

    def test_a_scheduled_run_never_reaches_the_nepal_today_default(self):
        """The new default must be unreachable from a cron firing.

        A schedule event with no answerable firing is still a fatal ambiguity:
        the only date the clock could supply there is the build's own day, which
        is defect #4 (run 38076000974).
        """
        code, out, emitted = self.resolve("", "2026-10-11T02:10:00Z",
                                          event="schedule", digest_time="")
        self.assertNotEqual(0, code,
                            "a scheduled run with no usable firing was dated "
                            "from the clock:\n" + out)
        self.assertNotIn("today", emitted, "a refused date still emitted one: %r"
                         % (emitted,))

    def test_a_malformed_slot_date_is_fatal(self):
        """Not a date STOPS the run; it never falls back to anything.

        A silent fallback here is worse than no date at all, because the only
        thing to fall back to is a day the dispatcher never asked for.
        """
        for bad in ("2026-2-3", "2026-02-30", "17-10-2026", "2026/10/17",
                    "yesterday"):
            with self.subTest(slot_date=bad):
                code, out, emitted = self.resolve(
                    "", "2026-10-11T02:10:00Z", event="workflow_dispatch",
                    digest_time="evening", slot_date=bad)
                self.assertNotEqual(0, code,
                                    "slot_date %r was accepted:\n%s" % (bad, out))
                self.assertIn("FATAL", out, out)
                self.assertNotIn("today", emitted,
                                 "a failed slot_date still emitted a date: %r"
                                 % (emitted,))

    def test_the_clock_is_read_only_for_a_dispatch_and_only_in_nepal(self):
        """The SCOPE of the clock, not its absence.

        CHANGED 2026-10-30 (run 41064520752). This used to assert that the
        NPT-today construction had been DELETED, because it was how the dispatch
        branch guessed a day. The refusal that replaced it then killed every
        dateless manual dispatch in step one, so the construction is back --
        for the dispatch arm only, in Nepal, and said out loud. A cron firing
        must still never reach it.
        """
        body = self.date_body()
        self.assertIn("slot_date_from_input", body,
                      "the slot step has no slot_date handling at all, so no "
                      "dispatcher can name the slot it means")
        self.assertIn("today_npt(clock)", body,
                      "the dispatch arm no longer dates itself to today in "
                      "Nepal, so `gh workflow run daily-news.yml` cannot run")
        self.assertIn("elif event_name == 'workflow_dispatch':", body,
                      "the Nepal-today default is not scoped to a manual "
                      "dispatch, so a cron firing can reach it")

    def test_an_unusable_schedule_fails_closed_rather_than_using_the_clock(self):
        """The whole point of deriving the date from the firing is that there is
        no longer any path on which the clock can supply it. A schedule this
        step cannot reason about must stop the run, not fall back."""
        body = self.date_body()
        # Temporarily present an unmappable schedule under a name that IS in
        # the slot map is impossible, so this drives the date helper directly:
        # a cron restricting its date fields has no answerable "most recent
        # firing", and must raise rather than guess.
        self.assertIn("restricts the date fields", body,
                      "the firing-date helper refuses a schedule whose most "
                      "recent instant is not answerable")
        for bad in ("5 13 1 * *", "5 13 * 1 *", "5 13 * * 1", "nonsense"):
            with self.subTest(schedule=bad):
                code, out, _emitted = self.resolve(bad, "2026-10-10T13:20:00Z")
                self.assertNotEqual(0, code,
                                    "cron %r was accepted and a date was "
                                    "invented for it:\n%s" % (bad, out))
                self.assertIn("FATAL", out, out)

    def test_the_comment_that_argued_the_clock_was_right_is_gone(self):
        """Defect #4 also left a comment ARGUING for the broken behaviour.

        A comment that says a firing delayed past midnight "still builds TODAY's
        digest, which is what the owner reads" is not a neutral bystander: it is
        the reasoning that made the wall clock look deliberate to the next
        reader. It has to be inverted, not merely worked around.

        Re-pointed at daily-news.yml with item A: the file that runs the rule is
        where a reader now meets it. Two things are asserted, and the second is
        the MECHANISM rather than a remembered sentence -- the sentence was
        rewritten when the code moved, and asserting the absence of a phrase that
        no longer exists in any workflow would prove nothing.
        """
        raw = W.raw_workflow_text(HANDOFF_WORKFLOW)
        self.assertIn("THE SLOT IS THE SCHEDULED SLOT.", raw,
                      "the build no longer states the rule it implements, so "
                      "the next reader has to re-derive it from the code.")
        self.assertNotIn("now_utc + datetime.timedelta(", raw,
                         "the wall-clock date construction is back in the "
                         "build, so the date would again come from the clock "
                         "the runner started on rather than from the slot's own "
                         "firing.")
        self.assertNotIn("still builds TODAY's", raw,
                         "the comment justifying the wall-clock date is back, so "
                         "the next reader is told the mislabelled archive was "
                         "intended.")


# ==========================================================================
# The seal job: no AI, one write target, and a loud failure.
# ==========================================================================

class SealJobTests(unittest.TestCase):
    AI_MARKERS = ("opencode", "pip install", "npm install", "npx ",
                  "wrangler", "gh workflow run", "scripts/render_digest.py",
                  "scripts/collect_news.py", "scripts/build_email.py")

    def seal_run_text(self):
        return "\n".join(run for _n, run in run_blocks(PREBUILD_WORKFLOW, "seal"))

    def test_the_seal_job_contains_no_ai_and_no_pipeline(self):
        # Scanned with quotes and heredoc bodies removed, so this asks whether
        # seal INVOKES any of these -- not whether it mentions one. An `echo`
        # telling the owner to run `wrangler secret put ...` is the opposite of
        # an invocation and must not be flagged.
        code = shell_commands_only(self.seal_run_text())
        found = [m for m in self.AI_MARKERS if m in code]
        self.assertEqual(
            [], found,
            "the seal job's shell INVOKES %s. The seal step's product is "
            "DECISIVE ARITHMETIC over a built page -- assert the markup, build a "
            "record, POST it. A model verdict must never be able to fake a "
            "seal, and a second copy of the pipeline here is the drift class "
            "this unit was built to avoid." % (found,))

    def test_the_seal_job_writes_to_the_worker_origin_and_nowhere_else(self):
        """DEVIATION FROM THE BRIEF, STATED RATHER THAN PAPERED OVER.

        The brief asked for "the only outbound host in its run: blocks is the
        Worker origin". Requirement E of the same brief requires, as one of the
        three fallback page-liveness checks, the newest production deployment
        identity from the Cloudflare PAGES API -- which is a different host.

        Both are kept, and the assertion is the sharper of the two claims: seal
        performs exactly ONE WRITE, it targets the Worker origin, and that
        origin is DERIVED from deploy/schedule.json rather than written as a
        literal. The Pages API is a READ.
        """
        text = self.seal_run_text()
        posts = re.findall(r"-X\s+POST", shell_commands_only(text))
        # U7 ADDED A SECOND POST, AND THE INVARIANT IS ABOUT WHAT A POST
        # CARRIES, NOT ABOUT HOW MANY THERE ARE. The seal job now dispatches the
        # supersede of a degraded push, which is a POST to the workflow-dispatch
        # endpoint. What must stay exactly one is the place a DIGEST RECORD can
        # land -- and the dispatch carries no body at all, which is asserted
        # below rather than assumed.
        self.assertEqual(2, len(posts),
                         "seal must issue exactly TWO POSTs: the ingest, and the "
                         "U7 supersede dispatch. Found %d. A third is a write "
                         "target nobody enumerated." % len(posts))
        supersede = step_named(PREBUILD_WORKFLOW, "seal",
                               "Supersede a degraded push with the full digest")
        dispatch_block = shell_commands_only(supersede["run"])
        self.assertEqual(
            1, dispatch_block.count("-X POST"),
            "the supersede step must POST exactly once -- the dispatch. Its "
            "ledger write is a PUT and its read is a GET.\n%s" % dispatch_block)
        for line in dispatch_block.splitlines():
            if "-X POST" in line or "/dispatches" in line:
                continue
            self.assertNotIn(
                "--data", line,
                "the supersede dispatch carries a request body:\n%s" % line)
        self.assertNotIn(
            "html_sha256", dispatch_block,
            "the supersede dispatch hands over nothing: the pushed digest is "
            "built and sent by daily-news.yml, which is the whole point of "
            "dispatching it rather than POSTing a payload here.")

        seal_step = step_named(PREBUILD_WORKFLOW, "seal",
                               "Seal the artifact into the Worker")
        run = seal_step["run"]
        hosts = set(re.findall(r"https://([A-Za-z0-9.\-]+)", run))
        self.assertEqual(set(), hosts,
                         "the seal step carries the literal host(s) %s. The "
                         "Worker's origin must be DERIVED from "
                         "deploy/schedule.json's health_url, which is the same "
                         "committed string the watchdog arm polls -- a second "
                         "literal is a fourth copy of one fact and drifts."
                         % sorted(hosts))
        self.assertIn("deploy/schedule.json", run)
        self.assertIn("health_url", run)
        self.assertIn('"$INGEST_URL"', run,
                      "the POST must target the derived INGEST_URL, not a literal")
        self.assertIn("X-Ingest-Token", run,
                      "the ingest credential must be sent as the header the "
                      "Worker checks. The secret NAME may appear; its value "
                      "never does.")
        self.assertIn("${{ secrets.DIGEST_INGEST_TOKEN }}",
                      W.raw_workflow_text(PREBUILD_WORKFLOW))

        # The complete set of literal hosts anywhere in seal's run blocks.
        # The Pages API read is one; U8's mirror commit adds github.com, which
        # is the git remote and is NOT a place a digest record is written --
        # what is written there is the mirror itself, and only that.
        #
        # THE INVARIANT IS UNCHANGED AND IS THE SHARP ONE: seal must not name
        # any host that could receive a DIGEST RECORD other than the Cloudflare
        # API. github.com is admitted precisely because `git push` carries the
        # mirror and nothing else, and the commit's staging list is asserted to
        # be exactly one path by
        # test_the_workflow_declares_the_permissions_it_uses above. A new host
        # appearing here is the signal that this allowlist needs re-reading --
        # which is why it is a closed set and not a subset test.
        every = set(re.findall(r"https://([A-Za-z0-9.\-]+)", self.seal_run_text()))
        self.assertEqual({"api.cloudflare.com", "github.com"}, every,
                         "seal's run blocks name the literal hosts %s. The "
                         "Cloudflare API is requirement E's Pages deployment "
                         "read plus U8's ledger read; github.com is U8's mirror "
                         "commit, whose staging list is asserted to be exactly "
                         "one path. The Worker origin and the archive origin are "
                         "both derived, never written here. A THIRD host means "
                         "something new can receive data and this list is stale."
                         % sorted(every))

    def test_the_seal_job_fails_loudly_before_it_touches_anything(self):
        names = [str(s.get("name") or "") for s in steps_of(PREBUILD_WORKFLOW, "seal")]
        gate_name = "Confirm the prebuild run succeeded before touching anything"
        download = "Download the prebuild output"
        self.assertIn(gate_name, names, "seal's steps are %s" % names)
        self.assertIn(download, names, "seal's steps are %s" % names)
        self.assertLess(
            names.index(gate_name), names.index(download),
            "the conclusion check must come BEFORE the artifact download, so a "
            "failed build never reaches the markup assertion or the POST. Order "
            "is %s" % names)
        gate = step_named(PREBUILD_WORKFLOW, "seal", gate_name)
        self.assertIn("::error::", gate["run"])
        self.assertIn("exit 1", gate["run"])
        self.assertNotIn("continue-on-error", gate,
                         "the gate that refuses to seal a failed build must not "
                         "be able to report green while refusing.")
        self.assertNotIn(
            "needs", job_of(PREBUILD_WORKFLOW, "seal"),
            "the seal no longer has a job to depend on (A): the `dispatch` job "
            "was deleted and its gate moved INTO the seal's first step, which "
            "refuses a non-success conclusion. A `needs:` here would name a job "
            "that does not exist, and the whole job would never run.")

    def test_the_ingest_refusal_is_not_a_success(self):
        run = step_named(PREBUILD_WORKFLOW, "seal",
                         "Seal the artifact into the Worker")["run"]
        self.assertIn('if [ "$CODE" != "200" ]', run,
                      "a non-200 from the ingest must be fatal. A half-sealed "
                      "artifact that the run reports as successful is the "
                      "silent-outage shape.")
        self.assertIn("DIGEST_INGEST_TOKEN is not set", run,
                      "an unset ingest credential must be named as the cause, "
                      "not reported as a sealed artifact.")

    def test_the_page_liveness_check_is_anonymous(self):
        step = step_named(PREBUILD_WORKFLOW, "seal",
                          "Verify the digest page is live before sealing")
        run = step["run"]
        # The site is public by design (2026-10-30). No credential may be sent,
        # and a non-200 is the defect this check exists to catch.
        self.assertNotIn("DIGEST_PASSWORD", run)
        self.assertNotIn("DIGEST_USERNAME", run)
        self.assertNotIn(
            "secrets.", json.dumps(step.get("env") or {}),
            "the liveness check must not be wired to any secret: an anonymous "
            "GET is the measurement.")
        self.assertNotIn("-u ", run,
                         "an authenticated GET reappeared in the liveness check")
        self.assertIn("curl --disable", run,
                      "the anonymous GET must ignore any runner .curlrc, so no "
                      "ambient config can quietly attach credentials.")
        self.assertIn('!= "200"', run)
        self.assertIn("section-header", run,
                      "a 200 is only meaningful when the body is the digest; the "
                      "page must carry <h2 class=\"section-header\">.")
        self.assertNotIn("api.cloudflare.com", run,
                         "the Pages-API fallback was removed with the credential "
                         "gate; an anonymous 200 is now the whole check.")


# ==========================================================================
# THE MARKUP LIST IS THE ARCHIVE SCRAPERS' LIST. Proved by behaviour.
# ==========================================================================

class SealMarkupIsTheArchiveScrapersMarkup(TempDirCase):
    """Not a copy of the scrapers' regexes -- the scrapers themselves.

    The seal job asserts three markup literals. If they are not exactly the
    literals `scripts/render_digest.py`'s `parse_digest_categories()` and
    `first_thumbnail()` depend on, then one of two things is true and both are
    bad: either the prebuild rejects a correctly-rendered digest (a false page,
    every morning), or it accepts one the archive index cannot read (the exact
    silent breakage this assertion exists to prevent).
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(W.REPO_ROOT / "scripts"))
        import render_digest
        sys.path.insert(0, str(W.REPO_ROOT / "tests" / "render"))
        from fixtures import validated_payload  # noqa: E402

        cls.scrapers = render_digest
        cls.tmpdir = tempfile.mkdtemp(prefix="prebuild-markup-")
        cls.pages = {}
        # TWO FIXTURES, AND THE SECOND ONE IS THE POINT. The shared
        # `validated_payload("ok")` carries no story images, so a page rendered
        # from it has NO `<img class="card-thumb">` at all -- which is what
        # render_digest.py's own comment means by "18 of the 75 shipped digests
        # carry no <img class='card-thumb'> at all". Asserting the thumbnail
        # unconditionally was therefore a real defect, found by running the real
        # renderer rather than by reading it.
        for label, with_images in (("bare", False), ("thumbs", True)):
            payload = validated_payload("ok")
            if with_images:
                stories = payload.get("stories") or []
                if not stories:
                    raise AssertionError(
                        "the fixture payload carries no stories, so the "
                        "with-images page cannot be built and the conditional "
                        "markup rule cannot be tested at all")
                stories[0]["image"] = {
                    "url": "https://cdn.invalid/thumb-%d.jpg" % 1,
                    "alt": "fixture thumbnail",
                }
            cls.pages[label] = cls._render(render_digest, payload, label)

    @classmethod
    def _render(cls, render_digest, payload, label):
        d = os.path.join(cls.tmpdir, label)
        os.makedirs(d, exist_ok=True)
        json_path = os.path.join(d, "stories.validated.json")
        with io.open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        out_dir = os.path.join(d, "digests")
        rc = render_digest.main([
            "--digest-time", "morning", "--date", "2026-10-09",
            "--email-txt", os.path.join(d, "absent-email.txt"),
            "--stories-json", json_path,
            "--out-dir", out_dir,
            "--index", os.path.join(d, "index.html")])
        assert rc == 0, ("render_digest.main exited %r for the %r fixture; the "
                         "page this suite judges the seal against could not be "
                         "built" % (rc, label))
        path = os.path.join(out_dir, "2026-10-09-morning.html")
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert render_digest.parse_digest_categories(path), (
            "sanity: the %r rendered fixture must have categories, or nothing "
            "below can tell a working markup list from a broken one" % label)
        return path, text

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        super().setUp()
        # A private copy of the WITH-IMAGES page, so a mutation cannot corrupt
        # the shared fixture. The image-free page is reached via self.pages.
        _src_path, self.page = self.pages["thumbs"]
        self.page_path = str(self.tmp / "page.html")
        Path(self.page_path).write_text(self.page, encoding="utf-8")
        self.bare_path, self.bare_page = self.pages["bare"]

    def seal_markup_lists(self):
        """(required, conditional) -- the two lists the seal step asserts."""
        run = "\n".join(run for _n, run in run_blocks(PREBUILD_WORKFLOW, "seal"))

        def resolve(name):
            block = re.search(r"%s\s*=\s*\(([^)]*)\)" % name, run)
            self.assertIsNotNone(block,
                                 "the seal step must declare %s as one named "
                                 "tuple, so this list and the seal job cannot "
                                 "drift apart" % name)
            literals = []
            # A tuple entry is EITHER a quoted literal or the NAME of one. Both
            # are read, because a list that quietly drops an inline literal is
            # how a wrong marker ends up asserted without this suite seeing it.
            for const in re.findall(r"'[^']*'|[A-Z][A-Z_]*", block.group(1)):
                if const.startswith("'"):
                    literals.append(const[1:-1])
                    continue
                m = re.search(r"^%s\s*=\s*'([^']*)'\s*$" % re.escape(const),
                              run, re.MULTILINE)
                self.assertIsNotNone(m,
                                     "%s is listed in %s but has no literal "
                                     "assignment in the seal step's shell. Every "
                                     "entry must resolve to a literal string, or "
                                     "this suite cannot compare the two."
                                     % (const, name))
                literals.append(m.group(1))
            return literals

        return resolve("REQUIRED_MARKUP"), resolve("CONDITIONAL_MARKUP")

    def seal_markup_list(self):
        required, conditional = self.seal_markup_lists()
        return required + conditional

    def test_the_seal_asserts_exactly_three_markers(self):
        required, conditional = self.seal_markup_lists()
        self.assertEqual(
            [SECTION_HEADER_MARKUP, CARD_MARKUP], required,
            "the seal job's unconditional markup list is %s. These are what the "
            "archive index reads on EVERY rendered page: parse_digest_categories()"
            " splits on the section header and counts cards." % (required,))
        self.assertEqual(
            [CARD_THUMB_MARKUP], conditional,
            "the conditional list is %s. first_thumbnail() reads the card thumb, "
            "and render_media() emits it ONLY for a story with an image url -- "
            "so it cannot be an unconditional assertion. render_digest.py's own "
            "comment records that 18 of the 75 shipped digests carry no "
            "<img class='card-thumb'> at all." % (conditional,))

    def test_every_asserted_marker_is_present_in_a_really_rendered_page(self):
        for literal in self.seal_markup_list():
            self.assertIn(literal, self.page,
                          "%r is asserted by the seal job but does not appear in "
                          "a page scripts/render_digest.py actually produced. "
                          "The seal would fail on every correctly-built digest."
                          % literal)

    def test_the_thumbnail_marker_is_absent_from_an_image_free_page(self):
        """The measured fact that makes the conditional rule necessary. If a
        renderer change ever made every digest carry an image, this test would
        fail and the conditional rule could be tightened -- deliberately, with
        the evidence in hand."""
        self.assertNotIn(
            CARD_THUMB_MARKUP, self.bare_page,
            "sanity: the image-free fixture was expected to carry no card "
            "thumbnail. If it now does, every digest has one and the seal's "
            "conditional rule can become unconditional.")
        self.assertIsNone(self.scrapers.first_thumbnail(self.bare_path))
        self.assertEqual(self.scrapers.parse_digest_categories(self.page_path),
                         self.scrapers.parse_digest_categories(self.bare_path),
                         "sanity: an image changes the markup but not the "
                         "category counts")

    def test_the_seal_applies_the_conditional_rule_to_the_real_heredoc(self):
        """EXECUTED, because the rule is a conditional and a conditional that
        is only asserted as a string has never been run."""
        body = python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]

        def prepare(stories):
            d = Path(tempfile.mkdtemp(prefix="seal-build-"))
            self.addCleanup(shutil.rmtree, str(d), True)
            stage_artifact(d, page_html=self.page, validated={
                "stories": stories, "stories_outcome": "produced",
                "digest_time": "morning", "date": "2026-10-09",
                "generated_at_npt": "2026-10-09 06:41",
                "data_cutoff_npt": "2026-10-09 06:40",
                "source_health": {"state": "ok"}})
            return d

        with_image = [{"title": "s", "category": "nepal",
                       "image": {"url": "https://cdn.invalid/a.jpg"}}]
        without_image = [{"title": "s", "category": "nepal"}]

        d_with = prepare(with_image)
        code, out = exec_heredoc(body, {
            "SLOT": "morning", "TODAY": "2026-10-09",
            "BUILT_AT": "2026-10-09T01:35:00Z"}, d_with)
        self.assertEqual(0, code, "a page with an image and all the markup must "
                                  "seal:\n%s" % out)
        self.assertTrue((d_with / "prebuild" / "artifact.json").is_file(),
                        "the seal wrote no artifact.json:\n%s" % out)
        sealed = json.loads(
            (d_with / "prebuild" / "artifact.json").read_text(encoding="utf-8"))
        self.assertEqual(1, sealed["schema_version"])
        self.assertEqual("digest:artifact:2026-10-09:morning", sealed["key"])
        self.assertEqual("morning", sealed["slot"])
        self.assertEqual("2026-10-09", sealed["date"])
        self.assertTrue(sealed["built_at"].endswith("Z"), sealed["built_at"])
        self.assertIs(sealed["is_partial"], False,
                      "source_health.state == 'ok' means NOT partial")
        self.assertEqual("T", sealed["ntfy"]["title"])

        d_without = prepare(without_image)
        code, out = exec_heredoc(body, {
            "SLOT": "morning", "TODAY": "2026-10-09",
            "BUILT_AT": "2026-10-09T01:35:00Z"}, d_without)
        self.assertEqual(0, code,
                         "a payload with no image must still seal, because the "
                         "page carries no thumbnail and the archive index renders "
                         "that with its reserved placeholder:\n%s" % out)
        self.assertIn("no story carries an image url", out)
        self.assertTrue((d_without / "prebuild" / "artifact.json").is_file(),
                        "the image-free case wrote no artifact.json:\n%s" % out)

    def test_a_degraded_source_health_marks_the_artifact_partial(self):
        """is_partial is a MEASURED fact, and it is what the Worker's delivery
        arm reads to label a push. Asserted from the same executed body."""
        body = python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]
        d = Path(tempfile.mkdtemp(prefix="seal-partial-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        stage_artifact(d, page_html=self.page, validated={
            "stories": [{"title": "s"}], "stories_outcome": "produced",
            "digest_time": "morning", "date": "2026-10-09",
            "generated_at_npt": "2026-10-09 06:41",
            "source_health": {"state": "degraded"}})
        code, out = exec_heredoc(body, {
            "SLOT": "morning", "TODAY": "2026-10-09",
            "BUILT_AT": "2026-10-09T01:35:00Z"}, d)
        self.assertEqual(0, code, out)
        sealed = json.loads(
            (d / "prebuild" / "artifact.json").read_text(encoding="utf-8"))
        self.assertIs(sealed["is_partial"], True,
                      "a digest built from a degraded source set must be sealed "
                      "as partial, or the Worker's push is unlabelled. Sealed: %r"
                      % sealed)
        self.assertEqual("degraded", sealed["source_health_state"])

    def test_an_unsummarised_edition_seals_as_its_own_kind_and_as_partial(self):
        """THE NO-AI EDITION MUST SAY SO IN THE ARTIFACT, NOT ONLY ON THE PAGE.

        The page and the push title carry the label, but the Worker never reads
        either: it reads the sealed artifact. So two facts have to be in it.

        `edition_kind` is what tells the Worker this is not a model-written
        digest. `is_partial` is set as well, deliberately: a Worker deployed
        before `edition_kind` existed knows only `is_partial`, and a labelled
        partial is a far better failure than an unlabelled "fresh".
        """
        body = python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]

        def seal(validated_extra):
            d = Path(tempfile.mkdtemp(prefix="seal-unsummarised-"))
            self.addCleanup(shutil.rmtree, str(d), True)
            validated = {
                "stories": [{"title": "s"}], "stories_outcome": "produced",
                "digest_time": "morning", "date": "2026-10-09",
                "generated_at_npt": "2026-10-09 06:41",
                "source_health": {"state": "ok"}}
            validated.update(validated_extra)
            stage_artifact(d, page_html=self.page, validated=validated)
            code, out = exec_heredoc(body, {
                "SLOT": "morning", "TODAY": "2026-10-09",
                "BUILT_AT": "2026-10-09T01:35:00Z"}, d)
            self.assertEqual(0, code, out)
            return json.loads(
                (d / "prebuild" / "artifact.json").read_text(encoding="utf-8"))

        sealed = seal({"edition_kind": "unsummarised"})
        self.assertEqual(
            "unsummarised", sealed.get("edition_kind"),
            "the no-AI edition sealed without an edition_kind, so the Worker "
            "would file it as an ordinary delivered digest. Sealed: %r" % sealed)
        self.assertIs(
            sealed["is_partial"], True,
            "the no-AI edition sealed with is_partial false. A Worker that does "
            "not know edition_kind then labels it fresh, which is the one claim "
            "this whole fallback must not make. Sealed: %r" % sealed)
        # The CONTROL: a healthy model-written digest is still neither.
        normal = seal({})
        self.assertEqual("", normal.get("edition_kind"),
                         "a normal digest sealed with an edition_kind")
        self.assertIs(normal["is_partial"], False,
                      "a healthy digest sealed as partial")

    def test_the_seal_refuses_a_page_that_dropped_a_load_bearing_marker(self):
        body = python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]
        d = Path(tempfile.mkdtemp(prefix="seal-broken-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        stage_artifact(d, page_html=self.page.replace(
            SECTION_HEADER_MARKUP, "<h2 class=renamed>"), validated={
                "stories": [{"title": "s"}], "stories_outcome": "produced",
                "digest_time": "morning", "date": "2026-10-09",
                "generated_at_npt": "2026-10-09 06:41",
                "source_health": {"state": "ok"}})
        code, out = exec_heredoc(body, {
            "SLOT": "morning", "TODAY": "2026-10-09",
            "BUILT_AT": "2026-10-09T01:35:00Z"}, d)
        self.assertNotEqual(0, code,
                            "a page whose section headers were renamed sealed "
                            "successfully. That is the exact silent breakage "
                            "the assertion exists to prevent.\n%s" % out)
        self.assertIn("::error::", out)
        self.assertIn("archive index", out)

    def test_the_seal_refuses_an_empty_or_a_yesterdays_digest(self):
        body = python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]

        def run_with(validated, page=None):
            d = Path(tempfile.mkdtemp(prefix="seal-bad-"))
            self.addCleanup(shutil.rmtree, str(d), True)
            stage_artifact(
                d, page_html=page if page is not None else self.page,
                validated=validated,
                click="https://daily-digest.example.pages.dev/digests/%s.html"
                      % validated.get("date"))
            return exec_heredoc(body, {
                "SLOT": "morning", "TODAY": "2026-10-09",
                "BUILT_AT": "2026-10-09T01:35:00Z"}, d)

        base = {"stories": [{"title": "s"}], "stories_outcome": "produced",
                "digest_time": "morning", "date": "2026-10-09",
                "generated_at_npt": "2026-10-09 06:41",
                "source_health": {"state": "ok"}}

        for label, override, needle in (
                ("empty", dict(base, stories=[], stories_outcome="empty"),
                 "empty digest"),
                ("yesterday", dict(base, date="2026-10-08",
                                   generated_at_npt="2026-10-08 06:41",
                                   stories=[{"title": "s"}]),
                 "yesterday"),
                ("wrong slot", dict(base, digest_time="evening"),
                 "digest_time")):
            with self.subTest(case=label):
                code, out = run_with(override)
                self.assertNotEqual(0, code,
                                    "%s sealed successfully:\n%s" % (label, out))
                self.assertIn("::error::", out)
                self.assertIn(needle, out)

        # The click-target case needs its own payload, so it is spelled out.
        d = Path(tempfile.mkdtemp(prefix="seal-click-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        stage_artifact(
            d, page_html=self.page, validated=base,
            click="https://daily-digest.example.pages.dev/digests/"
                  "2026-10-08-morning.html")
        code, out = exec_heredoc(body, {
            "SLOT": "morning", "TODAY": "2026-10-09",
            "BUILT_AT": "2026-10-09T01:35:00Z"}, d)
        self.assertNotEqual(0, code,
                            "a page_url addressing a different digest sealed "
                            "successfully; the owner would be sent to the wrong "
                            "page.\n%s" % out)
        self.assertIn("page_url", out)

    def test_removing_any_asserted_marker_breaks_a_real_scraper(self):
        """The behavioural half: each literal is load-bearing, because removing
        it changes what the archive scrapers return."""
        expectations = {
            SECTION_HEADER_MARKUP: "categories",
            CARD_MARKUP: "categories",
            CARD_THUMB_MARKUP: "thumbnail",
        }
        for literal in self.seal_markup_list():
            with self.subTest(marker=literal):
                mutated = self.page.replace(literal, "<!--removed-->")
                target = self.tmp / ("mutated-%d.html" % abs(hash(literal)))
                target.write_text(mutated, encoding="utf-8")
                if expectations[literal] == "categories":
                    counts = self.scrapers.parse_digest_categories(str(target))
                    self.assertNotEqual(
                        self.scrapers.parse_digest_categories(self.page_path),
                        counts,
                        "removing %r left the archive scrapers' category counts "
                        "unchanged, so it is NOT one of their load-bearing "
                        "markers and the seal is asserting something they do not "
                        "depend on." % literal)
                else:
                    self.assertIsNotNone(self.scrapers.first_thumbnail(self.page_path),
                                         "sanity: the fixture has a thumbnail")
                    self.assertIsNone(
                        self.scrapers.first_thumbnail(str(target)),
                        "removing %r did not break first_thumbnail(), so the seal "
                        "is asserting a marker the archive does not read." % literal)

    def test_the_list_is_not_just_everything_the_page_contains(self):
        """A list that asserted every class would be unfalsifiable and useless.
        One markup the page definitely carries, and deliberately does NOT
        depend on, must be absent from the list."""
        self.assertIn('<h3 class="card-title">', self.page,
                      "sanity: the fixture page carries card titles")
        self.assertNotIn('<h3 class="card-title">', self.seal_markup_list(),
                         "the seal's markup list must be the scrapers' list, "
                         "not every class the renderer emits")

    def test_a_marker_no_scraper_needs_is_rejected(self):
        """MUTATION PROOF: adding a literal the archive does not read must
        break the property, so the list cannot quietly grow into a
        pass-everything assertion."""
        not_load_bearing = self._markers_no_scraper_reads()
        self.assertEqual(
            ['<h3 class="card-title">'], not_load_bearing,
            "removing a card title changed neither scraper's output, so that "
            "literal is NOT load-bearing. A seal list containing it would be "
            "asserting markup the archive does not depend on -- and, worse, it "
            "is the shape that grows until the list stops meaning anything.")

    def _markers_no_scraper_reads(self, extra=('<h3 class="card-title">',)):
        """Of `extra`, which the archive index does NOT depend on.

        Measured by removing each from the rendered page and asking the real
        scrapers whether anything they return changed.
        """
        out = []
        for literal in extra:
            self.assertIn(literal, self.page,
                          "sanity: the fixture page must carry %r for this "
                          "mutation to mean anything" % literal)
            mutated = self.page.replace(literal, "<!--removed-->")
            target = self.tmp / ("unread-%d.html" % abs(hash(literal)))
            target.write_text(mutated, encoding="utf-8")
            if (self.scrapers.parse_digest_categories(str(target))
                    == self.scrapers.parse_digest_categories(self.page_path)
                    and self.scrapers.first_thumbnail(str(target))
                    == self.scrapers.first_thumbnail(self.page_path)):
                out.append(literal)
        return out


# ==========================================================================
# The seal asserts and hashes the RENDERED WEB PAGE, and that page has to be
# in the artifact. Run 38244506858 and the 11 before it died here: the seal
# asserted `email.html`, the archive scrapers' markup belongs to
# `scripts/render_digest.py`, and the page the owner opens was never uploaded
# at all. `digest:artifact:*` has therefore never been written once.
#
# Everything below is DERIVED from the code rather than restated. The file the
# seal reads is found by asking which upload entry renders to a page name; the
# location it unpacks to is computed through the same archive-root calculation
# the uploader's own action performs. A test that hardcodes "the page is at
# state/digest-page.html" would pass just as happily against the shipped bug.
# ==========================================================================

class SealSealsThePageItLinksToTests(TempDirCase):
    """The three properties the seal's own contract rests on."""

    TODAY = "2026-10-09"
    SLOT = "morning"

    def seal_body(self):
        return python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]

    def valid_stories(self, **over):
        base = {"stories": [{"title": "s", "category": "nepal"}],
                "stories_outcome": "produced", "digest_time": self.SLOT,
                "date": self.TODAY, "generated_at_npt": "%s 06:41" % self.TODAY,
                "source_health": {"state": "ok"}}
        base.update(over)
        return base

    def run_seal(self, base, *, page_html=None, click=None):
        """Execute the real heredoc over a real unpacked artifact."""
        if page_html is None:
            page_html = ('<h2 class="section-header" style="--dot:red;">N</h2>'
                         '<article class="card">c</article>')
        stage_artifact(base, page_html=page_html,
                       validated=self.valid_stories(), click=click)
        code, out = exec_heredoc(self.seal_body(), {
            "SLOT": self.SLOT, "TODAY": self.TODAY,
            "BUILT_AT": "2026-10-09T01:35:00Z"}, base)
        artifact = None
        path = Path(base) / "prebuild" / "artifact.json"
        if path.is_file():
            artifact = json.loads(path.read_text(encoding="utf-8"))
        return code, out, artifact

    # -- (a) the asserted file is IN the artifact ---------------------------

    def test_the_file_the_seal_asserts_is_in_the_uploaded_artifact(self):
        """THE CONTRACT THAT WAS BROKEN. Derived, never restated.

        `digest_page_entry` finds the upload entry by what it RENDERS to, and
        it raises when no entry renders to `<date>-<slot>.html`. So deleting
        the page from the `path:` list, renaming it, or pointing it at a
        different slot makes this fail -- it does not fall back to "the email
        is close enough".
        """
        entry = digest_page_entry(self.TODAY, self.SLOT)
        self.assertTrue(entry, "sanity: no upload entry renders to the page")
        unpacked = unpacked_path_for(entry, self.TODAY, self.SLOT)
        self.assertTrue(
            unpacked.endswith("%s-%s.html" % (self.TODAY, self.SLOT)),
            "sanity: the derived unpack location %r must be the page's own "
            "name, or this whole class is judging something else" % unpacked)

        # The page the seal reads is the page the uploader uploads. Compared
        # through the unpack path, because that is the only form in which the
        # two can be equal in production.
        d = Path(tempfile.mkdtemp(prefix="seal-asserted-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        code, out, artifact = self.run_seal(d)
        self.assertEqual(0, code,
                         "the seal refused a correctly-built artifact, so this "
                         "class cannot prove the happy path:\n%s" % out)
        self.assertIsNotNone(artifact, "no artifact was sealed:\n%s" % out)
        self.assertTrue((Path(d) / unpacked).is_file(),
                        "the seal sealed successfully without the uploaded page "
                        "being present at %r, so the assertion is not reading "
                        "the artifact" % unpacked)

    def test_removing_the_page_from_the_upload_list_fails_closed(self):
        """MUTATION PROOF, EXECUTED AGAINST A COPY OF THE REAL LIST.

        The pre-fix shape is exactly this: the uploader's list has no page in
        it, so the seal's asserted file is absent from the artifact. Proved by
        deleting the entry and asking the real predicate.
        """
        entries = L.upload_artifact_path_entries()
        self.assertIn(
            digest_page_entry(self.TODAY, self.SLOT), entries,
            "sanity: the page must be in the list for this mutation to mean "
            "anything. Entries=%r" % (entries,))
        without = [e for e in entries
                   if e != digest_page_entry(self.TODAY, self.SLOT)]
        self.assertNotIn(digest_page_entry(self.TODAY, self.SLOT), without,
                         "sanity: the mutation removed nothing")

        landed = L.unpacked_paths(without)
        page_unpacked = unpacked_path_for(
            digest_page_entry(self.TODAY, self.SLOT), self.TODAY, self.SLOT)
        self.assertNotIn(
            page_unpacked, landed,
            "with the page out of the upload list the artifact still contains "
            "%r, so the mutation does not reproduce the defect" % page_unpacked)
        self.assertTrue(
            L.layout_defects(without, [page_unpacked]),
            "layout_defects accepted a seal reading %r when the uploader never "
            "uploads it. This is the shape that failed the seal 12 runs out of "
            "12." % page_unpacked)

    # -- (b) html_sha256 describes the SAME bytes page_url names ------------

    def test_the_sealed_hash_is_the_hash_of_the_page_the_owner_is_sent_to(self):
        """CRITERION 2, MEASURED. Not asserted about -- hashed and compared.

        The digest is rendered once, by the real renderer, and written to the
        artifact at the location the uploader's list implies. The seal is then
        executed for real and its `html_sha256` compared against
        hashlib.sha256 of those exact bytes, AND against the hash of a
        deliberately different page, so a hash that silently described the
        wrong file cannot pass.
        """
        import hashlib
        page = ('<!doctype html><h2 class="section-header" style="--dot:red;">'
                'Nepal</h2><article class="card">one story</article>'
                '<article class="card">another</article>')
        d = Path(tempfile.mkdtemp(prefix="seal-hash-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        code, out, artifact = self.run_seal(d, page_html=page)
        self.assertEqual(0, code, "the happy path must seal first:\n%s" % out)

        unpacked = unpacked_path_for(
            digest_page_entry(self.TODAY, self.SLOT), self.TODAY, self.SLOT)
        on_disk = (d / unpacked).read_text(encoding="utf-8")
        self.assertEqual(
            page, on_disk,
            "sanity: the page the seal read is not the page this test wrote, "
            "so comparing hashes below would prove nothing")
        self.assertEqual(
            hashlib.sha256(page.encode("utf-8")).hexdigest(),
            artifact["html_sha256"],
            "html_sha256 is not the sha256 of the uploaded page. The sealed "
            "record would vouch for bytes the owner is never shown.")

        # AND it is not the hash of some other file -- which is what the
        # email-shaped defect actually produced.
        other = '<h2 class="section-header" style="--dot:red;">X</h2>'
        self.assertNotEqual(
            hashlib.sha256(other.encode("utf-8")).hexdigest(),
            artifact["html_sha256"],
            "sanity: the two candidate pages hash identically, so this test "
            "cannot distinguish them")

        # The name the owner is sent to is the name of the hashed file.
        self.assertEqual(
            artifact["page_url"].rsplit("/", 1)[-1],
            Path(unpacked).name,
            "page_url names %r but the hashed file is %r. The sealed hash and "
            "the click target describe different pages."
            % (artifact["page_url"].rsplit("/", 1)[-1], Path(unpacked).name))

    def test_a_page_url_naming_another_page_is_refused(self):
        """The negative half, so the check above cannot be a tautology.

        `page_url` is made to end in a digest the artifact does not hold. The
        seal must refuse rather than seal a hash for a page it never read.
        """
        d = Path(tempfile.mkdtemp(prefix="seal-wrongpage-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        code, out, _artifact = self.run_seal(
            d, click="https://daily-digest.example.pages.dev/digests/"
                     "2026-10-08-morning.html")
        self.assertNotEqual(0, code,
                            "a page_url addressing a different digest sealed "
                            "successfully:\n%s" % out)
        self.assertIn("::error::", out)
        self.assertIn("page_url", out)

    # -- (c) the failure message reports what was FOUND ----------------------

    def test_the_markup_failure_reports_what_was_found_not_what_was_required(self):
        """A message that lies about what it saw costs real diagnosis time.

        Run 38244506858 printed the two REQUIRED markers under the label
        "Present", directly beneath the same two markers labelled "missing",
        so the log read as a contradiction and sent the next person looking for
        a parser bug instead of at the file.
        """
        d = Path(tempfile.mkdtemp(prefix="seal-msg-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        # A page with NEITHER marker -- the exact shipped condition, because the
        # file the seal read was the email and the email carries neither.
        code, out, _artifact = self.run_seal(
            d, page_html="<!doctype html><html><body><p>hi</p></body></html>")
        self.assertNotEqual(0, code, "a markup-less page sealed successfully:\n%s" % out)
        self.assertIn("::error::", out)

        body = out.split("::error::", 1)[1]
        self.assertIn("Found in", body,
                      "the failure does not report what it found:\n%s" % out)
        self.assertNotIn("Present:", body,
                         "the failure still uses the 'Present:' label that made "
                         "run 38244506858 self-contradictory:\n%s" % out)
        # What was found is genuinely empty here, and the message must say so
        # rather than listing the markers it wanted.
        self.assertRegex(body, r"Found in [^:]*:\s*\[\]",
                         "the message claims it found markers in a page that "
                         "carries none:\n%s" % out)
        for literal in self.seal_markup_list():
            self.assertNotIn(
                "'%s'" % literal, body.split("Found in", 1)[1],
                "the Found list still prints the REQUIRED markup %r, which is "
                "the reported defect itself:\n%s" % (literal, out))

    def test_a_partially_marked_page_reports_exactly_what_survived(self):
        """The found list must discriminate, not just be empty.

        A message that always says "Found: []" would satisfy the test above, so
        this keeps one marker and removes the other. The survivor must be named
        and the casualty must not.
        """
        d = Path(tempfile.mkdtemp(prefix="seal-msg2-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        code, out, _artifact = self.run_seal(
            d, page_html='<h2 class="section-header" style="--dot:red;">N</h2>')
        self.assertNotEqual(0, code, out)
        body = out.split("::error::", 1)[1]
        self.assertIn(
            "Found in", body,
            "the failure does not report what it found, so the surviving "
            "marker cannot be located in it:\n%s" % out)
        found = body.split("Found in", 1)[1].split("bytes read", 1)[0]
        self.assertIn(SECTION_HEADER_MARKUP, found,
                      "the surviving marker is not reported as found:\n%s" % out)
        self.assertNotIn(CARD_MARKUP, found,
                         "a marker the page does not carry is reported as "
                         "found:\n%s" % out)

    def seal_markup_list(self):
        run = "\n".join(r for _n, r in run_blocks(PREBUILD_WORKFLOW, "seal"))
        block = re.search(r"REQUIRED_MARKUP\s*=\s*\(([^)]*)\)", run)
        self.assertIsNotNone(block, "the seal step no longer declares "
                                   "REQUIRED_MARKUP as a named tuple")
        literals = []
        for const in re.findall(r"'[^']*'|[A-Z][A-Z_]*", block.group(1)):
            if const.startswith("'"):
                literals.append(const[1:-1])
            else:
                m = re.search(r"^%s\s*=\s*'([^']*)'\s*$" % re.escape(const),
                              run, re.MULTILINE)
                self.assertIsNotNone(m, "%s has no literal assignment" % const)
                literals.append(m.group(1))
        return literals


#: The externals the seal steps shell out to, defined as SHELL FUNCTIONS
#: rather than as files on PATH.
#:
#: WHY FUNCTIONS AND NOT A PATH DIRECTORY. Git-for-Windows bash (MSYS2) always
#: puts `/mingw64/bin` and `/usr/bin` at the FRONT of PATH regardless of what
#: the caller prepends, and `/mingw64/bin/curl` exists. Measured, not assumed:
#: with the stub directory prepended, `command -v gh` and `command -v python3`
#: resolved to the stubs while `command -v curl` still resolved to
#: `/mingw64/bin/curl`. A PATH-based stub for curl is therefore silently
#: ineffective, and the tests would have been talking to the real network.
#: Functions take precedence over PATH lookups, so they cannot be outranked.
#: `test_the_stubs_are_actually_in_effect` asserts the functions are the ones
#: in scope, so this cannot quietly stop being true either.
def _stub_preamble():
    return """
python3() { "%s" "$@"; }
gh() {
  case "${STUB_GH_MODE:-}" in
    list) printf '[{"id": %%s, "conclusion": "%%s"}]' \\
          "${STUB_GH_BEFORE:-1}" "${STUB_GH_CONCLUSION:-success}" ;;
    *)   printf '%%s' "${STUB_GH_CONCLUSION:-success}" ;;
  esac
}
curl() {
  out=""
  prev=""
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    prev="$a"
  done
  # Optional argument recorder. Without it, "the step passes the configured
  # username to curl" can only be asserted as a string in the YAML.
  if [ -n "${STUB_CURL_ARGS_LOG:-}" ]; then printf '%%s\\n' "$*" >> "$STUB_CURL_ARGS_LOG"; fi
  if [ -n "$out" ]; then printf '%%s' "${STUB_CURL_BODY:-}" > "$out"; fi
  printf '%%s' "${STUB_CURL_CODE:-200}"
}
""" % sys.executable


class SealStepExecutionTests(TempDirCase):
    """The seal steps' real SHELL, executed with stubbed externals.

    Asserting that a step's text contains `::error::` proves the string was
    typed. It does not prove the step exits non-zero, and it certainly does not
    prove it exits ZERO when the strong check is unavailable -- which is the
    whole point of the graceful-degradation requirement. So these run the step.

    Offline: `gh` and `curl` are shell functions that answer from the
    environment, `python3` is a shim onto this interpreter, and nothing here
    reaches a network. `jq` is the real one, reading a real committed file.
    """

    def setUp(self):
        super().setUp()
        # `deploy/schedule.json` is read by two of the steps, for real.
        (self.tmp / "deploy").mkdir(exist_ok=True)
        shutil.copy(W.REPO_ROOT / "deploy" / "schedule.json",
                    self.tmp / "deploy" / "schedule.json")

    def run_seal_step(self, step_name, extra_env=None):
        step = step_named(PREBUILD_WORKFLOW, "seal", step_name)
        script = _stub_preamble() + "\n" + step["run"]
        proc = W.run_bash_script(script, env_overrides=dict(
            {
                "RUNNER_TEMP": str(self.tmp),
                "GITHUB_ENV": str(self.tmp / "gh_env.txt"),
                "GITHUB_OUTPUT": str(self.tmp / "gh_out.txt"),
                "GITHUB_STEP_SUMMARY": str(self.tmp / "summary.md"),
            }, **(extra_env or {})), cwd=str(self.tmp))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def _write_artifact(self, page_url="https://daily-digest.example.pages.dev/digests/2026-10-09-morning.html"):
        prebuild = self.tmp / "prebuild"
        prebuild.mkdir(exist_ok=True)
        (prebuild / "artifact.json").write_text(json.dumps({
            "schema_version": 1, "date": "2026-10-09", "slot": "morning",
            "page_url": page_url,
            "ntfy": {"title": "T", "body": "B"}}), encoding="utf-8")
        # The unpacked artifact layout, staged where the uploader's own `path:`
        # list says the page and the validated stories land. This step's
        # fallback reads BOTH of them, and reading either from the wrong place
        # is the 11-run defect this suite already has a guard for.
        stage_artifact(self.tmp, page_html=(
            '<h2 class="section-header" style="--dot:red;">X</h2>'
            '<article class="card">c</article>'), validated={
                "stories": [{"title": "s"}], "stories_outcome": "produced",
                "digest_time": "morning", "date": "2026-10-09",
                "generated_at_npt": "2026-10-09 06:41",
                "source_health": {"state": "ok"}})

    def test_the_stubs_are_actually_in_effect(self):
        """The precondition for every other test in this class, asserted rather
        than assumed. Without it, a stub that silently fails to be found leaves
        these tests talking to the real `gh` and the real `curl` -- which is
        the opposite of offline, and is exactly what happened on the first
        attempt at this class."""
        script = _stub_preamble() + '\nfor t in gh curl python3; do\n' \
                 '  printf "%s=%s\\n" "$t" "$(type -t $t)"\ndone\n' \
                 'printf "verdict=%s\\n" "$(gh api x --jq .conclusion)"\n' \
                 'printf "code=%s\\n" "$(curl -sS -o /dev/null -w x https://example.invalid)"\n'
        proc = W.run_bash_script(script, env_overrides={
            "STUB_GH_CONCLUSION": "cancelled",
            "STUB_CURL_CODE": "418"}, cwd=str(self.tmp))
        out = proc.stdout or ""
        for tool in ("gh", "curl", "python3"):
            self.assertIn("%s=function" % tool, out,
                          "the %s stub is not a shell function in scope, so the "
                          "step under test would call the real one. Output:\n%s"
                          % (tool, out))
        self.assertIn("verdict=cancelled", out, out)
        self.assertIn("code=418", out, out)

    # -- the conclusion gate -------------------------------------------------

    def test_the_conclusion_gate_is_red_for_a_failed_prebuild_run(self):
        """EXECUTED. The gate's job is to refuse, so a refusal that only exists
        as a string in a comment is not a refusal."""
        code, out = self.run_seal_step(
            "Confirm the prebuild run succeeded before touching anything",
            {"RUN_ID": "12345", "REPO": "o/r", "API": "https://api.github.invalid",
             "GH_TOKEN": "t", "STUB_GH_CONCLUSION": "failure"})
        self.assertNotEqual(0, code,
                            "a FAILED prebuild run did not stop the seal job:\n%s"
                            % out)
        self.assertIn("::error::", out)

        code, out = self.run_seal_step(
            "Confirm the prebuild run succeeded before touching anything",
            {"RUN_ID": "12345", "REPO": "o/r", "API": "https://api.github.invalid",
             "GH_TOKEN": "t", "STUB_GH_CONCLUSION": "cancelled"})
        self.assertNotEqual(0, code, out)

    def test_the_conclusion_gate_is_green_only_for_success(self):
        code, out = self.run_seal_step(
            "Confirm the prebuild run succeeded before touching anything",
            {"RUN_ID": "12345", "REPO": "o/r", "API": "https://api.github.invalid",
             "GH_TOKEN": "t", "STUB_GH_CONCLUSION": "success"})
        self.assertEqual(0, code, out)

    def test_the_conclusion_gate_is_red_with_no_run_id(self):
        code, out = self.run_seal_step(
            "Confirm the prebuild run succeeded before touching anything",
            {"RUN_ID": "", "REPO": "o/r", "API": "https://api.github.invalid",
             "GH_TOKEN": "t", "STUB_GH_CONCLUSION": "success"})
        self.assertNotEqual(0, code,
                            "the seal job would seal an artifact with no build "
                            "behind it:\n%s" % out)

    # -- graceful degradation, which is the requirement most easily asserted
    #    into existence and not actually implemented.

    def test_the_liveness_check_is_an_anonymous_get(self):
        """The site is public by design: no credential, no secret, no fallback."""
        self._write_artifact()
        args_log = self.tmp / "curl_args.txt"
        code, out = self.run_seal_step(
            "Verify the digest page is live before sealing",
            {"SLOT": "morning", "TODAY": "2026-10-09",
             "STUB_CURL_ARGS_LOG": str(args_log),
             "STUB_CURL_CODE": "200",
             "STUB_CURL_BODY": '<h2 class="section-header" style="--dot:red;">X</h2>'})
        self.assertEqual(0, code, out)
        self.assertIn("page liveness: anonymous 200", out)
        self.assertNotIn("::error::", out)
        args = args_log.read_text(encoding="utf-8") if args_log.exists() else ""
        self.assertTrue(args, "the liveness check did not call curl at all")
        self.assertNotIn(" -u ", args,
                         "the liveness check sent credentials. The site is public "
                         "by design and the measurement is anonymous. curl was "
                         "called with:\n%s" % args)
        self.assertNotIn("Authorization", args, args)

    def test_the_liveness_check_is_red_on_a_401(self):
        """A credential prompt is the defect now, not the protection."""
        self._write_artifact()
        code, out = self.run_seal_step(
            "Verify the digest page is live before sealing",
            {"SLOT": "morning", "TODAY": "2026-10-09",
             "STUB_CURL_CODE": "401",
             "STUB_CURL_BODY": "401 Unauthorized"})
        self.assertNotEqual(0, code,
                            "the page required authentication and the seal still "
                            "passed. The owner's link would open a login prompt.\n%s"
                            % out)
        self.assertIn("::error::", out)

    def test_the_liveness_check_is_red_when_the_page_is_not_the_digest(self):
        self._write_artifact()
        code, out = self.run_seal_step(
            "Verify the digest page is live before sealing",
            {"SLOT": "morning", "TODAY": "2026-10-09",
             "STUB_CURL_CODE": "200", "STUB_CURL_BODY": "<html>maintenance</html>"})
        self.assertNotEqual(0, code,
                            "production served something that is not the digest "
                            "and the seal would have pointed the owner's push at "
                            "it.\n%s" % out)
        self.assertIn("::error::", out)

    def test_the_liveness_check_is_red_on_a_404(self):
        self._write_artifact()
        code, out = self.run_seal_step(
            "Verify the digest page is live before sealing",
            {"SLOT": "morning", "TODAY": "2026-10-09",
             "STUB_CURL_CODE": "404", "STUB_CURL_BODY": ""})
        self.assertNotEqual(0, code, out)
        self.assertIn("::error::", out)


class ArtifactContractTests(unittest.TestCase):
    """THE HAND-OVER CONTRACT, held against the file this change may not edit.

    `deploy/cloudflare-worker.js` owns the other end of `?ingest=1`: it decides
    the key an artifact is filed under, the schema version it accepts and the
    slot names it recognises. This unit is not allowed to edit that file, so the
    only way these three facts can stay true is for the tests to READ it and
    fail when they stop agreeing.

    A mismatch here is not a cosmetic drift. A wrong prefix files every
    artifact under a key `digestHealthFields()` never reads, so `?health=1`
    reports `present: false` forever, the U6a arm goes red twice a day, and the
    delivery arm finds no artifact and rebuilds the digest at its own cron --
    which is the prebuild doing nothing at all, with a loud explanation nobody
    can act on because nobody can see the cause.
    """

    WORKER_REL = "deploy/cloudflare-worker.js"

    def worker_source(self):
        path = W.REPO_ROOT / self.WORKER_REL
        self.assertTrue(path.is_file(), "%s is missing" % self.WORKER_REL)
        return path.read_text(encoding="utf-8", errors="replace")

    def worker_const(self, name):
        m = re.search(r'const %s\s*=\s*(.+?);\s*$' % name,
                      self.worker_source(), re.MULTILINE)
        self.assertIsNotNone(m, "no `const %s` in %s" % (name, self.WORKER_REL))
        return m.group(1).strip()

    def test_the_artifact_key_prefix_is_the_workers_own(self):
        worker = self.worker_const("DIGEST_ARTIFACT_PREFIX")
        self.assertRegex(worker, r'^"[^"]*"$',
                         "DIGEST_ARTIFACT_PREFIX is not a plain string: %r" % worker)
        prefix = worker.strip('"')
        seal = "\n".join(run for _n, run in run_blocks(PREBUILD_WORKFLOW, "seal"))
        arm = "\n".join(run for _n, run in run_blocks(WATCHDOG_WORKFLOW, "watchdog"))
        for where, text in (("digest-prebuild.yml seal", seal),
                            ("digest-watchdog.yml prebuild arm", arm)):
            with self.subTest(source=where):
                self.assertIn(
                    prefix, text,
                    "%s does not use the Worker's artifact prefix %r. A "
                    "different prefix files every artifact where ?health=1 never "
                    "looks for it, so the delivery arm finds nothing and the "
                    "U6a arm goes red with no visible cause."
                    % (where, prefix))
                # The prefix must actually BUILD a key, not merely appear. The
                # Worker addresses artifacts as `<prefix><date>:<slot>`, so the
                # prefix has to be followed immediately by the date slot in the
                # format string. A comment that merely names the prefix passes
                # the first assertion and fails this one.
                self.assertRegex(
                    text, re.escape(prefix) + r"'?%s",
                    "%s contains the artifact prefix but never builds a key out "
                    "of it -- the prefix appears in prose only." % where)

    def test_the_schema_version_is_the_workers_own(self):
        worker = self.worker_const("DIGEST_SCHEMA_VERSION")
        self.assertEqual("1", worker)
        body = python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]
        self.assertIn("'schema_version': 1", body,
                      "the seal job writes schema_version 1 and the Worker "
                      "accepts %r. A mismatch is a 400 from ?ingest=1 and no "
                      "artifact at all." % worker)

    def test_the_slot_names_are_the_workers_own(self):
        worker = self.worker_const("DIGEST_SLOTS")
        slots = re.findall(r'"([a-z]+)"', worker)
        self.assertEqual(["morning", "evening"], slots,
                         "DIGEST_SLOTS in %s is %r" % (self.WORKER_REL, worker))
        body = python_heredocs(
            step_named(PREBUILD_WORKFLOW, "seal",
                       "Assert the rendered page and build the artifact record")["run"])[0]
        self.assertIn("slot = os.environ['SLOT']", body)
        arm = python_heredocs(
            step_named(WATCHDOG_WORKFLOW, "watchdog", PREBUILD_ARM_STEP)["run"])[0]
        self.assertIn("if slot not in ('morning', 'evening'):", arm,
                      "the watchdog arm must judge exactly the slots the Worker "
                      "files artifacts for (%r)" % (slots,))

    def test_the_ingest_route_and_header_are_the_workers_own(self):
        worker = self.worker_source()
        self.assertIn('searchParams.get("ingest") === "1"', worker,
                      "the Worker no longer routes ?ingest=1; the seal job's "
                      "POST would 404 against a different path")
        header = self.worker_const("INGEST_TOKEN_HEADER")
        self.assertEqual('"X-Ingest-Token"', header)
        run = step_named(PREBUILD_WORKFLOW, "seal",
                         "Seal the artifact into the Worker")["run"]
        self.assertIn(header.strip('"'), run,
                      "the seal step must send the header the Worker checks (%s)"
                      % header)

    def test_the_artifact_shape_satisfies_the_workers_own_validation(self):
        """The fields `validateArtifact()` reads, one by one, asserted to be
        present in what the seal job actually writes."""
        seal = "\n".join(run for _n, run in run_blocks(PREBUILD_WORKFLOW, "seal"))
        for field, why in (("schema_version", "ingest rejects any other value"),
                           ("date", "addressed and compared as the NEPAL date"),
                           ("slot", "ingest validates against DIGEST_SLOTS"),
                           ("built_at", "the freshness window is measured from it"),
                           ("page_url", "the click target, host-checked"),
                           ("is_partial", "decides partial vs delivered"),
                           ("ntfy", "carries the push title and body")):
            self.assertIn("'%s'" % field, seal,
                          "the sealed artifact has no %s field, and %s."
                          % (field, why))


# ==========================================================================
# U6a -- THE DEAD-MAN'S SWITCH.
# ==========================================================================

PREBUILD_ARM_STEP = "Check prebuild artifact (dead-man's switch, prebuild arm)"

#: A ?health=1 payload from a Worker that HAS the U3 digest fields, with the
#: morning artifact present and 5 minutes old. The baseline every other case
#: is measured against.
#:
#: THE SHAPE IS THE WORKER'S, NOT OURS. The digest half is NESTED under
#: `digest` because cloudflare-worker.js attaches it as one object
#: (`out.digest = await digestHealthFields(env, Date.now())`). An earlier
#: version of this fixture -- and of the arm it feeds -- put those fields at
#: the TOP level, which agreed with itself and disagreed with production: the
#: arm went red on every run of a healthy deployment. `HealthFieldShapeTests`
#: below now reads the Worker's own source and fails if this fixture drifts
#: back to a shape the Worker does not emit.
def healthy_health():
    return {
        "build": "abc123",
        "stale_after_s": 3600,
        "breaking_heartbeat_age_s": 30,
        "breaking_heartbeat_unreadable": False,
        "academic_heartbeat_age_s": 30,
        "academic_heartbeat_unreadable": False,
        "breaking_dispatch_ever": True,
        "breaking_dispatch_age_s": 120,
        "breaking_dispatch_unreadable": False,
        "breaking_last_dispatch_ok": True,
        "breaking_dispatch_http_status": 204,
        "digest": {
            "artifact_morning_present": True,
            "artifact_evening_present": False,
            "artifact_morning_age_s": 300,
            "artifact_evening_age_s": None,
            "unreadable": [],
            "ledger_age_s": 600,
            "last_delivery": None,
            "cron_arm": "dispatchDigest",
            "delivery_arm_enabled": False,
            "on_time_s": 900,
            "retention_days": 30,
        },
    }


#: The ONLY top-level keys a real ?health=1 body carries, per
#: healthForEndpoint() plus its heartbeat loop and the breaking telemetry.
WORKER_HEALTH_TOP_LEVEL = frozenset({
    "build", "crons", "crons_source", "crons_verified_at_runtime",
    "crons_liveness_verified_by", "dispatch_schedule", "stale_after_s",
    "heartbeat_throttle_s",
    "breaking_heartbeat_age_s", "breaking_heartbeat_unreadable",
    "academic_heartbeat_age_s", "academic_heartbeat_unreadable",
    "breaking_dispatch_ever", "breaking_dispatch_age_s",
    "breaking_dispatch_unreadable", "breaking_last_dispatch_ok",
    "breaking_dispatch_http_status", "breaking_success_ever",
    "breaking_success_age_s", "breaking_success_unreadable",
    "digest",
})


class HealthFieldShapeTests(unittest.TestCase):
    """THE REGRESSION GUARD FOR THE DEFECT OF 2026-10-13.

    The prebuild watchdog arm read `artifact_*_present`, `artifact_*_age_s`
    and `unreadable` at the TOP LEVEL of ?health=1. The Worker reports all
    three NESTED under `digest`, so the arm was red on every run of a healthy
    deployment and its message blamed the Worker rather than itself.

    Nothing about that failure is catchable by reading the arm alone, because
    the arm and its test fixture agreed with each other and both disagreed
    with production. These tests therefore pin the SHAPE against the Worker's
    own source, so the fixture cannot drift back into a shape the Worker never
    emits, and so the arm is proved against a payload that looks like the real
    one.
    """

    WORKER = "deploy/cloudflare-worker.js"

    def _worker_source(self):
        return (W.REPO_ROOT / self.WORKER).read_text(encoding="utf-8")

    def test_the_worker_attaches_the_digest_half_as_one_nested_object(self):
        """The single line the whole defect turns on."""
        src = self._worker_source()
        self.assertIn("out.digest = await digestHealthFields(env, Date.now());", src,
                      "the Worker no longer nests its digest half under `digest`. "
                      "If this moved, the prebuild arm and every fixture below "
                      "must move with it -- read the Worker first.")

    def test_the_artifact_fields_are_defined_inside_the_nested_builder(self):
        """`artifact_*_present` / `artifact_*_age_s` / `unreadable` belong to "
        digestHealthFields(), so they can only ever be reached through `digest`."""
        src = self._worker_source()
        start = src.index("async function digestHealthFields(")
        body = src[start:src.index("\nasync function ", start + 10)]
        for field in ('artifact_morning_present', 'artifact_evening_present',
                      'artifact_morning_age_s', 'artifact_evening_age_s',
                      'unreadable: []'):
            self.assertIn(field, body,
                          "%s is no longer defined by digestHealthFields(); the "
                          "worker's digest shape changed." % field)

    def test_no_top_level_unreadable_key_exists_to_read(self):
        """There is no top-level `unreadable` in ?health=1. The arm's
        ABSENT-vs-UNREADABLE discrimination read one and therefore always saw
        an empty list -- a distinction written down and never running."""
        health = healthy_health()
        self.assertNotIn("unreadable", health,
                         "a top-level `unreadable` would mean the Worker changed "
                         "shape; the arm reads the nested one")

    def test_the_fixture_matches_the_worker_on_both_levels(self):
        """Every field the fixture claims at the top level is one the Worker
        really emits at the top level; every digest field is really nested."""
        health = healthy_health()
        self.assertEqual(
            set(), set(health) - WORKER_HEALTH_TOP_LEVEL,
            "the fixture claims top-level keys the Worker does not emit: %s"
            % sorted(set(health) - WORKER_HEALTH_TOP_LEVEL))
        for field in ("artifact_morning_present", "artifact_evening_present",
                      "artifact_morning_age_s", "artifact_evening_age_s",
                      "unreadable"):
            self.assertNotIn(field, health,
                             "%s is NESTED under `digest`, not top level. A "
                             "top-level copy is what made the prebuild arm and "
                             "its fixture agree with each other and disagree "
                             "with production." % field)

    def test_the_arm_reads_every_digest_field_from_the_nested_block(self):
        """Read the arm's own body: no digest-scoped field may be read off
        `health` directly any more."""
        run = step_named(WATCHDOG_WORKFLOW, "watchdog", PREBUILD_ARM_STEP)["run"]
        body = python_heredocs(run)[0]
        self.assertIn("digest_block = health.get('digest')", body,
                      "the arm no longer resolves the nested block; if this line "
                      "is gone the arm is reading the wrong shape again")
        for wrong in ("health.get(present_field)",
                      "health.get(age_field)",
                      "health.get('unreadable')"):
            self.assertNotIn(wrong, body,
                             "%s reads a NESTED field at the top level. That is "
                             "the 2026-10-13 defect: the arm goes red on every "
                             "run of a healthy deployment." % wrong)
        for right in ("digest_block.get(present_field)",
                      "digest_block.get(age_field)",
                      "digest_block.get('unreadable')"):
            self.assertIn(right, body,
                          "%s is missing: the arm must read the nested block" % right)

    def test_the_no_digest_block_failure_names_the_arms_own_misread(self):
        """A missing `digest` block is THIS ARM reading the wrong shape, and
        the message must say so. The original message blamed the Worker
        deployment and told the reader not to look at digest-prebuild.yml --
        which is how a healthy green arm was reported as a broken prebuild."""
        run = step_named(WATCHDOG_WORKFLOW, "watchdog", PREBUILD_ARM_STEP)["run"]
        body = python_heredocs(run)[0]
        self.assertIn("THIS ARM reading the wrong shape", body,
                      "the no-digest-block failure must name the arm's own misread "
                      "rather than blaming a deployment or steering the reader away "
                      "from digest-prebuild.yml")


class PrebuildArmExistsTests(unittest.TestCase):
    def test_the_watchdog_has_the_prebuild_arm(self):
        step = step_named(WATCHDOG_WORKFLOW, "watchdog", PREBUILD_ARM_STEP)
        self.assertIn("::error::", step["run"])
        self.assertNotIn("continue-on-error", step,
                         "THIS STEP DELIBERATELY HAS NONE. The two informational "
                         "arms above end `exit 0` so a red watchdog caused by the "
                         "thing it watches cannot train the owner to ignore the "
                         "badge; this one exists to make a broken prebuild RED, "
                         "and continue-on-error would turn it into an ::error:: "
                         "that pages nobody and reports green.")

    def test_the_arm_is_exempt_from_check_7_for_a_written_reason(self):
        key = (WATCHDOG_WORKFLOW, "watchdog", PREBUILD_ARM_STEP)
        self.assertIn(
            key, L.ALWAYS_STEP_EXEMPTIONS,
            "the arm carries `if: always()`, so Check 7 requires either "
            "continue-on-error (which would disarm it) or an entry here saying "
            "why failing the job is what the step is for. Remove the `if:` and "
            "the row together, or neither.")
        reason = L.ALWAYS_STEP_EXEMPTIONS[key]
        self.assertGreater(len(reason), 120,
                           "an exemption row must carry a reason a later reader "
                           "can argue with, not 'because I said so'")

    def test_the_arm_cannot_affect_the_digest_verdict(self):
        run = step_named(WATCHDOG_WORKFLOW, "watchdog", PREBUILD_ARM_STEP)["run"]
        self.assertNotIn("steps.watch.outputs.miss", run,
                         "the arm must not read or write the digest verdict. "
                         "`Alert on missed digest` is gated on that one value "
                         "and nothing else may touch it.")
        self.assertNotIn("state/last_digest_sent.json", run,
                         "the marker step owns that file; a second reader that "
                         "also writes conclusions from it is how two verdicts "
                         "start disagreeing.")

    def test_it_rides_the_existing_schedule_and_adds_no_runner_minutes(self):
        """An arm, not a job: it must not add a cron."""
        crons = set(W.iter_schedule_crons(WATCHDOG_WORKFLOW))
        self.assertEqual({"0 4,16 * * *"}, crons,
                         "the watchdog's schedule must be exactly the 04/16 pair. "
                         "The prebuild arm rides those firings, and the 17/32/47 "
                         "empty-digest retry ladder was REMOVED on 2026-10-20 and "
                         "moved onto the Worker's own minute grid (see "
                         "SCHED_LADDER_PHASE in deploy/cloudflare-worker.js), so a "
                         "cron here again would be a second retry driver racing the "
                         "Worker's debounce. Declared: %s" % sorted(crons))
        # And the arm must be a STEP of the existing watchdog job, not a job of
        # its own, for the same reason.
        self.assertIn(PREBUILD_ARM_STEP,
                      [str(s.get("name") or "")
                       for s in steps_of(WATCHDOG_WORKFLOW, "watchdog")])


class LadderOnlyGateSkipsTheWatchdogJobTests(unittest.TestCase):
    """A Worker ladder dispatch must run ONLY the empty-digest retry job.

    The Worker's ladder arm dials digest-watchdog.yml with `ladder_only=true`
    when state/digest_retry.json says a retry is due. A workflow_dispatch already
    satisfies `github.event_name != 'schedule'`, so WITHOUT a clause naming the
    input, a Worker ladder ask would ALSO run the `watchdog` job -- and judging a
    slot that is not due is exactly the false page DEFECT 4 was about (the job
    once emailed "digest NOT delivered" ~19h early because it asked "what time is
    it now?" instead of "which slot was due?").

    The `empty_digest_retry` job must STILL run on a ladder dispatch -- it owns
    the ladder's gates, ledger and alert -- so it must not be skipped by the same
    input. These are the two halves that must stay opposite.

    The workflow is read through WATCHDOG_GATE_OVERRIDE when set, so a mutation
    proof can strip the clause from a COPY without touching the committed file.
    """

    def _doc(self):
        override = os.environ.get("WATCHDOG_GATE_OVERRIDE")
        if override:
            return W.load_workflow_text(Path(override).read_text(encoding="utf-8"))
        return W.load_workflow(WATCHDOG_WORKFLOW)

    @staticmethod
    def _on(doc):
        # PyYAML resolves a bare `on:` to the boolean True; the repository's own
        # parser keeps it a string. Accept either.
        return doc.get("on") or doc.get(True) or {}

    def test_the_watchdog_job_is_gated_off_on_a_ladder_dispatch(self):
        jobs = self._doc()["jobs"]
        cond = str(jobs["watchdog"].get("if") or "")
        self.assertIn(
            "github.event.inputs.ladder_only != 'true'", cond,
            "the watchdog job must be SKIPPED on a Worker ladder dispatch: a "
            "ladder ask means 'run the retry and nothing else', and judging a "
            "slot that is not due is the false page DEFECT 4 was about. Got: %r"
            % cond)
        self.assertIn(
            "github.event.schedule == '0 4,16 * * *'", cond,
            "the same gate must keep the job on the 04/16 pair, the only cron "
            "left once the 17/32/47 ladder moved onto the Worker's minute grid. "
            "Got: %r" % cond)

    def test_the_gate_negates_rather_than_affirms_the_ladder_input(self):
        cond = str(self._doc()["jobs"]["watchdog"].get("if") or "")
        self.assertNotIn(
            "github.event.inputs.ladder_only == 'true'", cond,
            "affirming the input would SKIP the job on EVERY normal 04/16 firing "
            "and run it only on a ladder dispatch -- the exact inversion. Got: %r"
            % cond)

    def test_the_retry_job_is_NOT_skipped_by_the_ladder_input(self):
        cond = str(self._doc()["jobs"]["empty_digest_retry"].get("if") or "")
        self.assertNotIn(
            "ladder_only", cond,
            "the retry job owns the ladder and must run on a ladder dispatch, so "
            "it must not reference ladder_only at all. Got: %r" % cond)

    def test_the_ladder_only_input_still_exists_and_defaults_false(self):
        dispatch = self._on(self._doc()).get("workflow_dispatch", {}) or {}
        inputs = (dispatch.get("inputs") or {}) if isinstance(dispatch, dict) else {}
        self.assertIn("ladder_only", inputs,
                      "the Worker dials the workflow with this input; removing it "
                      "means the ladder dispatch is rejected or misfires")
        self.assertIn(inputs["ladder_only"].get("default"), (False, "false"),
                      "a plain manual dispatch must behave as it always did and "
                      "judge the slot, so the default is false, not true")


class PrebuildArmBehaviourTests(TempDirCase):
    """THE RED PROOF, AND THE ABSENT-vs-UNREADABLE PROOF.

    Every case EXECUTES the arm's real Python body against a real
    `?health=1` payload written to a file. Reading the code cannot settle
    whether a `null` age reads as healthy, because that is exactly the question.
    """

    NOW = "2026-10-09T04:05:00+00:00"   # the 04:00 UTC morning firing
    DATE = "2026-10-09"
    MORNING_KEY = "digest:artifact:2026-10-09:morning"
    EVENING_KEY = "digest:artifact:2026-10-09:evening"

    #: The file the arm reads its prebuild schedule out of. It is the same
    #: deploy/schedule.json this step already reads `health_url` from, which is
    #: why it carries the committed dispatcher_crons.digest_prebuild block.
    SCHEDULE_REL = "deploy/schedule.json"

    def run_arm(self, health, slot="morning", now=None, schedule=None,
                schedule_raw=None, schedule_missing=False, code="200",
                curl_rc=0):
        """Execute the real arm body.

        `schedule` defaults to the repository's own deploy/schedule.json; pass a
        dict (or raw text, or `schedule_missing=True`) to hand the arm a
        schedule that is absent, empty or malformed instead. That is the whole
        point of the arm's fail-closed half: a blank schedule must never read
        like "nothing is due", so those shapes have to be executed rather than
        asserted about.
        """
        tmp = tempfile.mkdtemp(prefix="prebuild-arm-")
        try:
            health_path = Path(tmp) / "health.json"
            health_path.write_text(
                json.dumps(health) if not isinstance(health, str) else health,
                encoding="utf-8")
            schedule_path = Path(tmp) / "schedule.json"
            if schedule_missing:
                schedule_path = Path(tmp) / "does-not-exist.json"
            elif schedule_raw is not None:
                schedule_path.write_text(schedule_raw, encoding="utf-8")
            elif schedule is None:
                shutil.copy(W.REPO_ROOT / self.SCHEDULE_REL, schedule_path)
            else:
                schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            out = Path(tmp) / "gh_output.txt"
            out.write_text("", encoding="utf-8")
            summary = Path(tmp) / "summary.md"
            summary.write_text("", encoding="utf-8")
            step = step_named(WATCHDOG_WORKFLOW, "watchdog", PREBUILD_ARM_STEP)
            body = python_heredocs(step["run"])[0]
            code_, text = exec_heredoc(body, {
                "GITHUB_OUTPUT": str(out),
                "GITHUB_STEP_SUMMARY": str(summary),
                "PREBUILD_HEALTH_FILE": str(health_path),
                "PREBUILD_HEALTH_URL": "https://worker.invalid/?health=1",
                "PREBUILD_HEALTH_CODE": code,
                "PREBUILD_HEALTH_CURL_RC": str(curl_rc),
                "PREBUILD_SLOT": slot,
                "PREBUILD_SCHEDULE_FILE": str(schedule_path),
                "PREBUILD_EVENT_NAME": "schedule",
                "WATCHDOG_NOW_UTC": now or self.NOW,
                "RUNNER_TEMP": tmp,
            }, tmp)
            return code_, text, summary.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- the healthy baseline, so a failure below is the arm and not the world

    def test_a_present_fresh_artifact_passes(self):
        code, out, _ = self.run_arm(healthy_health())
        self.assertEqual(0, code, out)
        self.assertIn("is present", out)

    # -- RED PROOF: a deliberately broken prebuild turns the arm red

    def test_a_deliberately_broken_prebuild_turns_the_arm_red(self):
        """THE PROOF THE ARCHITECT ASKED FOR.

        A broken prebuild is precisely a health payload whose artifact is not
        there: the dispatch failed, or the build failed, or the seal step
        refused. The arm must go red, print ::error:: and exit non-zero -- not
        note it, not exit 0 like the informational arms above it.
        """
        broken = healthy_health()
        broken["digest"]["artifact_morning_present"] = False
        broken["digest"]["artifact_morning_age_s"] = None
        code, out, summary = self.run_arm(broken)
        self.assertNotEqual(0, code,
                            "a prebuild that sealed nothing left the arm GREEN. "
                            "Shipping this without a red arm is what converts a "
                            "latency problem into a silent-outage problem.\n%s"
                            % out)
        self.assertIn("::error::", out)
        self.assertIn("ABSENT", out)
        self.assertIn("❌", summary)

    def test_a_failed_prebuild_run_that_never_sealed_anything_is_red(self):
        """The other shape of 'broken': the run produced no artifact AND the
        health endpoint says it never recorded one."""
        health = healthy_health()
        health["digest"].update({"artifact_morning_present": False,
                                 "artifact_morning_age_s": None,
                                 "ledger_age_s": None, "last_delivery": None})
        code, out, _ = self.run_arm(health)
        self.assertNotEqual(0, code, out)
        self.assertIn("ABSENT", out)

    # -- ABSENT vs UNREADABLE. Different faults, different messages, and
    #    NEITHER may read as healthy.

    def test_absent_and_unreadable_are_distinguished(self):
        absent = healthy_health()
        absent["digest"]["artifact_morning_present"] = False
        absent["digest"]["artifact_morning_age_s"] = None

        unreadable = healthy_health()
        # A KV read failure leaves `present` false -- exactly as a missing key
        # does. That is the whole trap: reading the boolean first would report
        # every KV blip as "the prebuild failed".
        unreadable["digest"]["artifact_morning_present"] = False
        unreadable["digest"]["artifact_morning_age_s"] = None
        unreadable["digest"]["unreadable"] = [self.MORNING_KEY]

        absent_code, absent_out, _ = self.run_arm(absent)
        unreadable_code, unreadable_out, _ = self.run_arm(unreadable)

        self.assertNotEqual(0, absent_code, absent_out)
        self.assertNotEqual(0, unreadable_code, unreadable_out)
        self.assertIn("ABSENT", absent_out)
        self.assertNotIn("ABSENT", unreadable_out,
                         "a KV read failure was reported as an absent artifact. "
                         "Those are different faults with different fixes -- one "
                         "is digest-prebuild.yml, the other is the Worker's KV.")
        self.assertIn("UNREADABLE", unreadable_out.upper())
        self.assertIn("could not be READ from KV", unreadable_out)

    def test_an_unreadable_ledger_key_is_not_mistaken_for_this_slots_artifact(self):
        """The Worker's `unreadable` list names the KEY that failed. An
        unrelated unreadable key must not fail this arm."""
        health = healthy_health()
        health["digest"]["unreadable"] = ["digest:ledger"]
        code, out, _ = self.run_arm(health)
        self.assertEqual(0, code,
                         "an unreadable digest:ledger key failed the prebuild "
                         "arm, which judges the ARTIFACT key only.\n%s" % out)

    def test_a_null_age_is_never_a_pass(self):
        """0 would be a pass, so a null age must be loud. The Worker
        deliberately reports null rather than 0 for exactly this reason."""
        for label, health in (
                ("present with a null age", dict(healthy_health()["digest"],
                                                  artifact_morning_age_s=None)),
                ("present with a string age", dict(healthy_health()["digest"],
                                                   artifact_morning_age_s="300")),
                ("present with a bool age", dict(healthy_health()["digest"],
                                                 artifact_morning_age_s=True)),
        ):
            with self.subTest(case=label):
                nested = healthy_health()
                nested["digest"] = health
                code, out, _ = self.run_arm(nested)
                self.assertNotEqual(0, code,
                                    "%s reported healthy. A blank must never read "
                                    "like good news.\n%s" % (label, out))

    def test_a_deployment_that_does_not_report_the_field_is_red_and_names_the_cause(self):
        """A `digest` block with no artifact field in it is a genuinely old
        deployment -- and, now that the arm reads the nested block, that is the
        ONLY shape that may produce this message."""
        health = healthy_health()
        del health["digest"]["artifact_morning_present"]
        code, out, _ = self.run_arm(health)
        self.assertNotEqual(0, code, out)
        self.assertIn("predates the digest artifact fields", out,
                      "a deployment missing the field must be named as a "
                      "WORKER DEPLOYMENT problem, or the owner will go looking at "
                      "the wrong file")
        self.assertIn("digest-prebuild.yml", out)

    def test_a_payload_with_no_digest_block_is_red_and_names_the_ARMS_own_misread(self):
        """THE DIRECT REGRESSION PROOF, direction 1.

        Feed the arm the exact shape the Worker really emits minus the `digest`
        object -- which is what it saw before the fix, and what it would see
        again if anyone reads the digest fields off the top level. It must go
        red, and it must blame ITSELF rather than the Worker deployment.
        """
        health = healthy_health()
        del health["digest"]
        health.update({"artifact_morning_present": True,
                       "artifact_morning_age_s": 300, "unreadable": []})
        code, out, _ = self.run_arm(health)
        self.assertNotEqual(0, code,
                            "a payload carrying the artifact fields ONLY at the top "
                            "level was accepted. That is the 2026-10-13 defect: "
                            "the Worker nests them under `digest`, so this shape "
                            "means the arm is reading the wrong place.\n%s" % out)
        self.assertIn("THIS ARM reading the wrong shape", out,
                      "the failure must name the arm's own misread. Blaming the "
                      "Worker deployment is what sent the owner to the wrong "
                      "repository for a green pipeline.")
        self.assertNotIn("predates the digest artifact fields", out,
                         "a missing `digest` block is not an old deployment; the "
                         "two faults must not share a message")

    def test_a_real_nested_payload_passes_and_a_top_level_only_payload_does_not(self):
        """BOTH DIRECTIONS, in one test, because the defect is a direction
        error: the arm must pass the real shape and reject the invented one."""
        real_code, real_out, _ = self.run_arm(healthy_health())
        self.assertEqual(0, real_code,
                         "the arm went red against the REAL nested ?health=1 "
                         "payload of a healthy Worker.\n%s" % real_out)
        self.assertIn("is present", real_out)

        top_level_only = healthy_health()
        digest_fields = top_level_only.pop("digest")
        top_level_only.update(digest_fields)
        bad_code, bad_out, _ = self.run_arm(top_level_only)
        self.assertNotEqual(0, bad_code,
                            "the arm passed a payload whose fields are at the top "
                            "level, which is a shape the Worker never emits.\n%s"
                            % bad_out)

    def test_genuinely_missing_artifact_is_still_red_on_the_nested_shape(self):
        """BOTH DIRECTIONS, the other one: fixing the shape must not have
        disarmed the arm. A healthy deployment with a real missing artifact is
        still RED."""
        health = healthy_health()
        health["digest"]["artifact_morning_present"] = False
        health["digest"]["artifact_morning_age_s"] = None
        code, out, summary = self.run_arm(health)
        self.assertNotEqual(0, code,
                            "a genuinely missing artifact read as healthy on the "
                            "correct nested shape.\n%s" % out)
        self.assertIn("ABSENT", out)
        self.assertIn("digest.artifact_morning_present=false", out)
        self.assertIn("❌", summary)

    def test_the_absent_vs_unreadable_distinction_is_actually_running(self):
        """It used to read a top-level `unreadable` that does not exist, so the
        list was permanently empty and the distinction never ran -- while the
        comment above it described it as load-bearing. Prove it runs."""
        health = healthy_health()
        health["digest"]["artifact_morning_present"] = False
        health["digest"]["artifact_morning_age_s"] = None
        health["digest"]["unreadable"] = [self.MORNING_KEY]
        code, out, _ = self.run_arm(health)
        self.assertNotEqual(0, code, out)
        self.assertIn("could not be READ from KV", out)
        self.assertNotIn("ABSENT", out)

    # -- the window gate, and the clock is not the slot

    def test_it_does_not_alarm_before_the_prebuild_window_has_passed(self):
        """An hour after the 06:40 NPT prebuild: the artifact is not late yet."""
        code, out, _ = self.run_arm(healthy_health(), now="2026-10-09T00:55:00+00:00")
        self.assertEqual(0, code, out)
        self.assertIn("not due", out)

    def test_the_window_is_derived_from_the_committed_prebuild_schedule(self):
        """THE ARM MUST NOT CARRY ITS OWN SCHEDULE.

        The prebuild is dispatched by the Worker on its own clock, so the arm
        reads the committed `dispatcher_crons.digest_prebuild` block rather than
        a cron list in the prebuild workflow or a number in a comment. If the
        dispatcher's prebuild moves, this deadline moves with it.
        """
        code, out, _ = self.run_arm(healthy_health())
        self.assertIn("06:40 NPT", out,
                      "the deadline must be derived from the committed "
                      "dispatcher_crons.digest_prebuild cron, not from a number "
                      "in a comment. Output:\n%s" % out)
        self.assertIn("55 0 * * *", out,
                      "the arm must name the committed cron it read, so the "
                      "judge can see which record produced the deadline:\n%s"
                      % out)
        self.assertIn("schedule.json", out,
                      "the arm must say which file it read:\n%s" % out)
        self.assertIn("artifact due by", out)

        # And the same pinned instant must get a DIFFERENT verdict when the
        # committed schedule moves -- which is what "derived" actually means, and
        # a number in a comment could never do this. 02:40 UTC is 08:25 NPT, so
        # the artifact is not due until 04:10 UTC and the 04:05 UTC watchdog
        # firing is BEFORE the deadline; against the real 06:40 NPT prebuild the
        # same instant is 185 min past it. One clock, two schedules, two
        # different verdicts.
        moved = self.schedule_json()
        moved["dispatcher_crons"]["digest_prebuild"]["morning"] = "40 2 * * *"
        code, out, _ = self.run_arm(healthy_health(), schedule=moved)
        self.assertEqual(0, code, out)
        self.assertIn("08:25 NPT", out,
                      "moving the committed prebuild did not move the deadline "
                      "the arm computed:\n%s" % out)
        self.assertIn("not due", out,
                      "at 04:05 UTC a prebuild dispatched at 08:25 NPT has not "
                      "had its 90-minute tolerance yet, so the arm must hold. "
                      "It only reaches that answer by reading the schedule.\n%s"
                      % out)

    def schedule_json(self):
        return json.loads((W.REPO_ROOT / self.SCHEDULE_REL).read_text(
            encoding="utf-8"))

    def test_a_missing_prebuild_schedule_fails_closed(self):
        code, out, _ = self.run_arm(healthy_health(),
                                    schedule={"dispatcher_crons": {}})
        self.assertNotEqual(0, code,
                            "with no prebuild schedule to read, the arm exited 0. "
                            "It cannot judge, so it must say so loudly.\n%s" % out)
        self.assertIn("digest_prebuild", out, out)

    def test_an_EMPTY_prebuild_schedule_fails_closed(self):
        """THE EXACT TRAP. An empty schedule must not become "no deadline".

        This is the state the arm is one careless `if not block: pass` away
        from, and it is the dangerous direction: with no cron found, a lenient
        lookup yields no deadline, the arm compares `now < due` against
        nothing, and a prebuild that never sealed anything reads as
        "not due yet" -- forever. The arm would go permanently green on a
        broken prebuild, which is the silent-outage outcome this arm exists to
        prevent.

        Every shape of "the list is there but empty" is executed, because they
        fail in different lines.
        """
        base = self.schedule_json()
        # (label, schedule, the phrase that must appear in the failure). The
        # phrase is per-case on purpose: an arm that failed closed for the WRONG
        # reason is still an arm that cannot tell the owner what to fix.
        cases = (
            ("block present but empty",
             {"dispatcher_crons": {"digest_prebuild": {}}},
             "nothing to judge"),
            ("morning named as an empty string",
             {"dispatcher_crons": {"digest_prebuild":
                                   {"morning": "", "evening": "55 12 * * *"}}},
             "nothing to judge"),
            ("morning named as null",
             {"dispatcher_crons": {"digest_prebuild":
                                   {"morning": None, "evening": "55 12 * * *"}}},
             "nothing to judge"),
            ("morning named as whitespace",
             {"dispatcher_crons": {"digest_prebuild":
                                   {"morning": "   ", "evening": "55 12 * * *"}}},
             "nothing to judge"),
            ("morning named as a number",
             {"dispatcher_crons": {"digest_prebuild":
                                   {"morning": 55, "evening": "55 12 * * *"}}},
             "nothing to judge"),
            ("dispatcher_crons present but not an object",
             {"dispatcher_crons": ["55 0 * * *", "55 12 * * *"]},
             "carries no dispatcher_crons.digest_prebuild block"),
            ("dispatcher_crons absent entirely",
             {"crons": ["* * * * *"]},
             "carries no dispatcher_crons.digest_prebuild block"),
        )
        for label, schedule, phrase in cases:
            with self.subTest(case=label):
                code, out, _ = self.run_arm(healthy_health(), schedule=schedule)
                self.assertNotEqual(
                    0, code,
                    "an empty/malformed prebuild schedule left the arm GREEN "
                    "with no deadline. That is a permanently-green arm on a "
                    "prebuild that never seals anything.\n%s" % out)
                self.assertIn("::error::", out)
                self.assertIn(phrase, out,
                              "%s: the failure must name the schedule and not "
                              "the artifact, and must say what it could not "
                              "read.\n%s" % (label, out))
        # And the same holds for the evening slot, not just morning.
        code, out, _ = self.run_arm(
            healthy_health(), slot="evening", now="2026-10-09T16:05:00+00:00",
            schedule={"dispatcher_crons": {"digest_prebuild": {}}})
        self.assertNotEqual(0, code, out)
        self.assertIn("nothing to judge", out, out)

    def test_an_unreadable_or_absent_prebuild_schedule_fails_closed(self):
        for label, kwargs, phrase in (
                ("the file is not JSON", dict(schedule_raw="not json at all"),
                 "not parsable JSON"),
                ("the file is a JSON array", dict(schedule_raw="[1, 2, 3]"),
                 "not a JSON object"),
                ("the file does not exist", dict(schedule_missing=True),
                 "could not be read"),
        ):
            with self.subTest(case=label):
                code, out, _ = self.run_arm(healthy_health(), **kwargs)
                self.assertNotEqual(
                    0, code,
                    "%s left the arm green with no deadline: it cannot see the "
                    "schedule, so it cannot judge anything.\n%s" % (label, out))
                self.assertIn("::error::", out)
                self.assertIn(phrase, out,
                              "%s must name what it could not read, or the "
                              "owner goes looking at the wrong thing.\n%s"
                              % (label, out))
                self.assertIn(".json", out,
                              "%s must name the FILE it could not read, so the "
                              "owner knows which record is missing.\n%s"
                              % (label, out))

    def test_a_prebuild_cron_filed_under_the_WRONG_slot_fails_closed(self):
        """The committed record contradicts itself: the `morning` cron fires in
        the evening half of the Nepal day.

        This is the mislabelled-archive defect wearing a different hat. A
        deadline taken from the wrong half of the day is a watchdog that alarms
        hours before the prebuild has even been asked for -- or, worse, never
        alarms at all for a slot that genuinely never sealed.
        """
        schedule = self.schedule_json()
        schedule["dispatcher_crons"]["digest_prebuild"]["morning"] = "55 12 * * *"
        code, out, _ = self.run_arm(healthy_health(), schedule=schedule)
        self.assertNotEqual(0, code,
                            "the morning cron resolves to the evening slot and "
                            "the arm judged it anyway.\n%s" % out)
        self.assertIn("18:40", out, out)
        self.assertIn("not the morning slot", out, out)

    def test_a_prebuild_cron_that_is_not_a_plain_daily_cron_fails_closed(self):
        for label, expr in (("restricts the day of month", "5 1 15 * *"),
                            ("is not five fields", "55 0 * *")):
            with self.subTest(case=label):
                schedule = self.schedule_json()
                schedule["dispatcher_crons"]["digest_prebuild"]["morning"] = expr
                code, out, _ = self.run_arm(healthy_health(), schedule=schedule)
                self.assertNotEqual(0, code,
                                    "prebuild cron %r (%s) was accepted; its "
                                    "Nepal Time is not answerable, so the "
                                    "deadline is not either.\n%s"
                                    % (expr, label, out))
                self.assertIn("::error::", out)

    def test_a_prebuild_cron_that_fires_MORE_THAN_ONCE_per_slot_fails_closed(self):
        """One slot gets ONE prebuild.

        `55 0-23 * * *` resolves to 24 Nepal instants inside the morning half,
        so there is no single instant a deadline can be measured from. An arm
        that took the earliest would report "fine" for a schedule that seals
        the slot 24 times -- the same over-firing shape as three prebuild
        crons and one Worker dispatch, which is what this whole change exists
        to make impossible.
        """
        schedule = self.schedule_json()
        schedule["dispatcher_crons"]["digest_prebuild"]["morning"] = "55 0-23 * * *"
        code, out, _ = self.run_arm(healthy_health(), schedule=schedule)
        self.assertNotEqual(0, code,
                            "a prebuild cron that fires 24 times a day was "
                            "accepted and judged from its earliest instant.\n%s"
                            % out)
        self.assertIn("no single instant", out, out)
        self.assertIn("One slot gets ONE prebuild", out, out)

    def test_an_unreachable_worker_fails_closed(self):
        for label, kwargs in (("curl failure", dict(code="000", curl_rc=7)),
                              ("HTTP 500", dict(code="500"))):
            with self.subTest(case=label):
                code, out, _ = self.run_arm(healthy_health(), **kwargs)
                self.assertNotEqual(0, code,
                                    "%s left the arm green: it cannot see the "
                                    "artifact, so it cannot report fine.\n%s"
                                    % (label, out))

    def test_unparsable_health_json_fails_closed(self):
        code, out, _ = self.run_arm("<html>gateway timeout</html>")
        self.assertNotEqual(0, code, out)
        self.assertIn("not parsable JSON", out)

    def test_an_unknown_slot_fails_closed(self):
        code, out, _ = self.run_arm(healthy_health(), slot="")
        self.assertNotEqual(0, code,
                            "with no slot established, the arm must not judge "
                            "either one silently.\n%s" % out)

    def test_the_evening_firing_judges_the_evening_prebuild(self):
        """The 16:00 UTC firing is 185 min past the 18:40 NPT prebuild, and must
        judge the EVENING artifact. Judging the morning one here would read a
        genuinely-absent evening artifact as healthy."""
        health = healthy_health()
        health["digest"]["artifact_evening_present"] = True
        health["digest"]["artifact_evening_age_s"] = 420
        code, out, _ = self.run_arm(health, slot="evening",
                                    now="2026-10-09T16:05:00+00:00")
        self.assertEqual(0, code, out)
        self.assertIn("18:40 NPT", out)
        self.assertIn("55 12 * * *", out,
                      "the evening firing must read the EVENING prebuild cron "
                      "out of the committed schedule, not the morning one.\n%s"
                      % out)

        absent_evening = healthy_health()
        code, out, _ = self.run_arm(absent_evening, slot="evening",
                                    now="2026-10-09T16:05:00+00:00")
        self.assertNotEqual(0, code, out)
        self.assertIn("digest.artifact_evening_present=false", out,
                      "the evening firing judged the EVENING artifact, which is "
                      "the point: judging the morning one would read a genuinely "
                      "absent evening artifact as healthy.\n%s" % out)
        self.assertIn(self.DATE, out)


# --------------------------------------------------------------------------
# FIX 1 -- A SUCCESSFUL DISPATCH THAT UPLOADED NOTHING IS A DEFINED SKIP
# --------------------------------------------------------------------------
#:
#: The live failure, verbatim:
#:
#:     Artifact not found for name: daily-digest-evening-39129324776
#:
#: `actions/download-artifact@v4` raised that for a dispatched run that had
#: LEGITIMATELY no-opped -- nothing to build, so daily-news.yml's `Upload digest
#: artifacts` step (gated `always() && env.FALLBACK_NOOP != 'true'`) was SKIPPED
#: and no artifact existed. Correct upstream behaviour turned this job red, and a
#: job that is red for correct behaviour carries no signal at all.
#:
#: The step separates the two cases by a FACT FROM THE API rather than by a
#: guess: the dispatched run's own step list, filtered to `Upload digest
#: artifacts`. `skipped` is a defined skip; anything else -- including the step
#: being ABSENT from the list, which filters to the empty string -- is fatal.
#:
#: The stub below models the three calls the step makes and lets the REAL `jq`
#: apply the step's own filters, so the shapes exercised are the endpoint's
#: shapes rather than a repainting of them. No network, no `gh`, no credentials.
_FETCH_STEP = "Download the prebuild output"

_FETCH_GH_STUB = r"""#!/usr/bin/env bash
filter=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--jq" ]; then filter="$a"; fi
  prev="$a"
done
emit() {
  if [ -n "$filter" ]; then printf '%s' "$1" | jq -r "$filter"; else printf '%s' "$1"; fi
}
case "$*" in
  *"run download"*)
    printf '%s\n' "$*" >> "${STUB_FETCH_LOG:?}"
    target=""
    prev=""
    for a in "$@"; do
      if [ "$prev" = "--dir" ]; then target="$a"; fi
      prev="$a"
    done
    mkdir -p "$target"
    printf '{"date": "2026-10-10", "slot": "evening"}\n' > "$target/artifact.json"
    exit 0
    ;;
  *"/artifacts"*)
    if [ -n "${STUB_FETCH_LIST_RC:-}" ]; then
      echo "gh: Server Error (HTTP 502)" >&2
      exit "$STUB_FETCH_LIST_RC"
    fi
    # THE JOB'S OWN SCALARS BUILD THE JSON, exactly as the sibling stub at the
    # top of this file does: a JSON blob cannot survive the trip through this
    # harness's environment, and one that arrives mangled would fail `jq` for a
    # reason that has nothing to do with the step under test.
    case "${STUB_FETCH_MODE:-absent}" in
      present) name="daily-digest-${STUB_FETCH_SLOT:-evening}-${STUB_FETCH_RUN_ID:-0}" ;;
      other)   name="daily-digest-evening-38076000974" ;;
      *)       name="" ;;
    esac
    if [ -z "$name" ]; then
      emit '{"artifacts": []}'
    else
      emit "{\"artifacts\": [{\"name\": \"${name}\"}]}"
    fi
    ;;
  *"/jobs"*)
    case "${STUB_FETCH_UPLOAD:-success}" in
      skipped) conclude='"skipped"' ;;
      failure) conclude='"failure"' ;;
      absent)  emit '{"jobs": [{"steps": []}]}'; exit 0 ;;
      *)       conclude='"success"' ;;
    esac
    emit "{\"jobs\": [{\"steps\": [{\"name\": \"Upload digest artifacts\", \"conclusion\": ${conclude}}]}]}"
    ;;
  *) : ;;
esac
"""


class PrebuildFetchIsADefinedSkipTests(TempDirCase):
    """The seal's artifact fetch, executed against a modelled `gh`.

    The step's REAL shell is run with its REAL `env:` block, resolved the way
    Actions resolves it, so deleting the classification from the workflow fails
    these tests rather than passing them for a reason the workflow does not share.
    """

    RUN_ID = "39129324776"
    SLOT = "evening"
    ARTIFACT = "daily-digest-evening-39129324776"

    #: Every step in the seal job that reads `prebuild/`, and so must not run
    #: when the fetch decided there is nothing to read.
    CONSUMERS = (
        "Assert the rendered page and build the artifact record",
        "Verify the digest page is live before sealing",
        "Seal the artifact into the Worker",
        "Mirror the delivery ledger into git",
    )
    SUPERSEDE = "Supersede a degraded push with the full digest"

    def fetch_env(self, extra):
        step = step_named(PREBUILD_WORKFLOW, "seal", _FETCH_STEP)
        values = {
            "secrets.GITHUB_TOKEN": "stub-token",
            "github.repository": "looping-forever/daily-media-news",
            "github.api_url": "https://api.github.invalid",
            # A. The run id, the slot and the conclusion are the seal's own INPUT
            # and the confirm poll's output now -- there is no dispatch job to read
            # them from, and the poll publishes the conclusion it established
            # rather than inheriting one.
            "github.event.inputs.run_id": self.RUN_ID,
            "github.event.inputs.digest_time": self.SLOT,
            "github.event.inputs.slot_date": "2026-10-09",
            "steps.confirm.outputs.conclusion": "success",
            "steps.confirm.outputs.built_at": "2026-10-09T01:35:00Z",
        }
        env = {}
        for key, raw in (step.get("env") or {}).items():
            text = str(raw)
            for expr, val in values.items():
                text = text.replace("${{ %s }}" % expr, val)
            self.assertNotIn(
                "${{", text,
                "env[%s] carries an expression this harness cannot resolve: %r"
                % (key, text))
            env[key] = text
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run_fetch(self, extra):
        step = step_named(PREBUILD_WORKFLOW, "seal", _FETCH_STEP)
        shutil.rmtree(self.tmp / "prebuild", ignore_errors=True)
        bin_dir = self.tmp / "bin"
        W.install_stub_on_path(bin_dir, _FETCH_GH_STUB, name="gh")
        log = self.tmp / "fetch_calls.txt"
        log.write_text("", encoding="utf-8")
        out = self.tmp / "fetch_out.txt"
        out.write_text("", encoding="utf-8")
        summary = self.tmp / "fetch_summary.md"
        summary.write_text("", encoding="utf-8")
        env = self.fetch_env(extra)
        env["PATH"] = "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))
        env["GITHUB_OUTPUT"] = str(out)
        env["GITHUB_STEP_SUMMARY"] = str(summary)
        env["RUNNER_TEMP"] = str(self.tmp)
        env["STUB_FETCH_LOG"] = str(log)
        proc = W.run_bash_script(step["run"], env_overrides=env, cwd=str(self.tmp))
        return {
            "code": proc.returncode,
            "text": (proc.stdout or "") + (proc.stderr or ""),
            "outputs": out.read_text(encoding="utf-8"),
            "summary": summary.read_text(encoding="utf-8"),
            "calls": log.read_text(encoding="utf-8"),
            "downloaded": (self.tmp / "prebuild" / "artifact.json").exists(),
        }

    # ---- the control: the model has to discriminate ----------------------

    def test_the_stub_discriminates_so_the_cases_below_cannot_pass_vacuously(self):
        present = self.run_fetch({"STUB_FETCH_MODE": "present",
                                  "STUB_FETCH_SLOT": self.SLOT,
                                  "STUB_FETCH_RUN_ID": self.RUN_ID})
        absent = self.run_fetch({"STUB_FETCH_MODE": "absent",
                                 "STUB_FETCH_UPLOAD": "skipped"})
        self.assertIn("skip=0", present["outputs"], present["text"])
        self.assertIn("skip=1", absent["outputs"], absent["text"])
        self.assertNotEqual(present["outputs"], absent["outputs"])

    # ---- the delivered behaviour ----------------------------------------

    def test_a_no_op_run_is_a_defined_skip_that_does_NOT_fail_the_job(self):
        """THE HEADLINE. This is the case the live failure threw away.

        The dispatched run succeeded with no artifact because nothing needed
        building; the job must stay green, say why, and record it.
        """
        got = self.run_fetch({"STUB_FETCH_MODE": "absent",
                              "STUB_FETCH_UPLOAD": "skipped"})
        self.assertEqual(
            0, got["code"],
            "a dispatched run that legitimately built nothing made this job RED. "
            "That is the live failure the fetch step exists to end: "
            "'Artifact not found for name: %s'.\n%s" % (self.ARTIFACT, got["text"]))
        self.assertIn("skip=1", got["outputs"], got["outputs"])
        self.assertIn("::notice::", got["text"])
        self.assertIn("defined skip", got["text"])
        self.assertIn(self.RUN_ID, got["summary"],
                      "the skip is not recorded in the job summary:\n%r"
                      % got["summary"])
        self.assertFalse(got["downloaded"],
                         "a defined skip downloaded something")

    def test_the_notice_says_the_supersede_step_still_runs(self):
        """The operator reading the job has to be told the slot is not abandoned."""
        got = self.run_fetch({"STUB_FETCH_MODE": "absent",
                              "STUB_FETCH_UPLOAD": "skipped"})
        self.assertIn("supersede step still runs", got["text"], got["text"])

    def test_a_missing_artifact_whose_upload_step_RAN_is_still_fatal(self):
        """The error class is NOT silenced: only the proven no-op is a skip."""
        got = self.run_fetch({"STUB_FETCH_MODE": "absent",
                              "STUB_FETCH_UPLOAD": "success"})
        self.assertNotEqual(
            0, got["code"],
            "a run whose 'Upload digest artifacts' step DID run has no artifact, "
            "so the artifact is missing rather than never created -- that is a "
            "real break and must stay fatal.\n%s" % got["text"])
        self.assertIn("::error::", got["text"])
        self.assertNotIn("skip=1", got["outputs"])

    def test_an_ABSENT_upload_step_is_not_treated_as_a_skip(self):
        """`skipped` is the ONLY value that means the no-op path.

        The step's filter is `join(",")`, which yields the EMPTY STRING when the
        upload step is not in the run's list at all. A check written as "not
        success" would read that as a skip; the equality check does not.
        """
        got = self.run_fetch({"STUB_FETCH_MODE": "absent",
                              "STUB_FETCH_UPLOAD": "absent"})
        self.assertNotEqual(0, got["code"],
                            "the upload step was absent from the run's step list "
                            "and this step still called it a skip:\n%s"
                            % got["text"])
        self.assertNotIn("skip=1", got["outputs"])

    def test_a_run_whose_artifacts_cannot_be_LISTED_is_fatal_rather_than_guessed(self):
        """No list, no verdict -- it must not fall through to "must be a no-op"."""
        got = self.run_fetch({"STUB_FETCH_MODE": "absent",
                              "STUB_FETCH_UPLOAD": "success",
                              "STUB_FETCH_LIST_RC": "1"})
        self.assertNotEqual(0, got["code"], got["text"])
        self.assertIn("will not guess", got["text"])
        self.assertNotIn("skip=0", got["outputs"])
        self.assertNotIn("skip=1", got["outputs"])

    def test_a_present_artifact_is_downloaded_and_sealed(self):
        """The healthy path must be UNTOUCHED by the skip handling."""
        got = self.run_fetch({"STUB_FETCH_MODE": "present",
                              "STUB_FETCH_SLOT": self.SLOT,
                              "STUB_FETCH_RUN_ID": self.RUN_ID})
        self.assertEqual(0, got["code"], got["text"])
        self.assertIn("skip=0", got["outputs"], got["outputs"])
        self.assertNotIn("::error::", got["text"])
        self.assertTrue(got["downloaded"],
                        "the artifact was reported present and never downloaded")
        self.assertIn("--name %s" % self.ARTIFACT, got["calls"],
                      "the download asked for the wrong name:\n%r" % got["calls"])

    def test_another_runs_artifact_is_not_this_slots(self):
        """`grep -qxF` is an EXACT match, so a previous slot's artifact -- the
        same slot name, a different run id -- is not this one."""
        got = self.run_fetch({"STUB_FETCH_MODE": "other",
                              "STUB_FETCH_UPLOAD": "success"})
        self.assertNotEqual(0, got["code"],
                            "an artifact for a DIFFERENT run id was accepted for "
                            "this slot:\n%s" % got["text"])

    # ---- the ordering a skip must not break -----------------------------

    def test_the_supersede_step_is_not_gated_on_the_skip(self):
        """THE ORDERING REQUIREMENT, pinned structurally.

        A defined skip must still REACH the supersede step, because that is the
        step that replaces a degraded push. Gating it on the fetch output would
        make a no-op the end of the chain, which is the opposite of what a skip
        means.
        """
        step = step_named(PREBUILD_WORKFLOW, "seal", self.SUPERSEDE)
        condition = str(step.get("if") or "")
        self.assertNotIn(
            "skip", condition,
            "the supersede step is gated on the fetch: %r. A defined skip must "
            "still reach it." % condition)
        job = job_of(PREBUILD_WORKFLOW, "seal")
        self.assertIsNone(
            job.get("if"),
            "the seal job itself is gated (%r), so a defined skip cannot reach "
            "the supersede step inside it." % job.get("if"))
        text = W.raw_workflow_text(PREBUILD_WORKFLOW)
        self.assertLess(
            text.index(_FETCH_STEP), text.index(self.SUPERSEDE),
            "the supersede step runs BEFORE the fetch, so it cannot act on what "
            "the fetch learned")

    def test_the_four_artifact_consumers_are_gated_on_the_skip_AND_on_success(self):
        """Every step that reads `prebuild/` must skip when there is none.

        `success()` is asserted too: writing any `if:` REPLACES the implicit
        `success()`, so a gate of only `skip != '1'` would let these run after a
        genuine failure and read a half-written directory.
        """
        gated = {name: str(step.get("if") or "")
                 for job_id, name, step in W.iter_all_steps(PREBUILD_WORKFLOW)
                 if job_id == "seal"}
        for name in self.CONSUMERS:
            with self.subTest(step=name):
                self.assertIn(name, gated,
                              "the step is gone from the seal job; this test "
                              "lists the steps that read prebuild/. Present: %s"
                              % sorted(gated))
                self.assertEqual(
                    "success() && steps.fetch.outputs.skip != '1'", gated[name],
                    "the step that consumes prebuild/ is not gated on the skip "
                    "output and on success()")

    def test_the_fetch_step_is_the_gate_and_the_download_action_is_gone(self):
        """`id: fetch` must exist, and the action that caused the dead job gone."""
        step = step_named(PREBUILD_WORKFLOW, "seal", _FETCH_STEP)
        self.assertEqual("fetch", step.get("id"),
                         "the gate the four consumers reference does not exist, "
                         "so `steps.fetch.outputs.skip` is empty and none of them "
                         "run at all")
        self.assertNotIn("uses", step,
                         "the fetch step is still an action rather than the "
                         "classification")
        self.assertIn(
            "Upload digest artifacts", str(step.get("run") or ""),
            "the fetch step no longer reads the conclusion of the step that "
            "uploads the artifact, so it cannot tell a no-op from a break")
        # The invariant is about what is USED, not about what is NAMED: this
        # workflow's own comment quotes the old action on purpose, because the
        # defect it caused is the reason the step looks the way it does.
        for job_id, name, other in W.iter_all_steps(PREBUILD_WORKFLOW):
            with self.subTest(job=job_id, step=name):
                self.assertNotIn(
                    "download-artifact", str(other.get("uses") or ""),
                    "the action that turned a correct upstream no-op into a red "
                    "seal job is still used by this step")


if __name__ == "__main__":
    unittest.main()
