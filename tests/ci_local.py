#!/usr/bin/env python3
"""Reproduce the GitHub Actions test-suite job locally. Stdlib only.

WHY THIS FILE EXISTS
--------------------
Both bugs fixed alongside it (2026-09-30) were green on the owner's machine and
red on every CI run, for the same reason: nobody could run the CI path locally.
The render suite read a fixture from the runtime `state/` directory, so a clean
CI checkout had no file to open -- `FileNotFoundError`, 70 errors in one run,
while a laptop with a stale `state/` copy sailed through. The real-fixture
manifest guard recorded hashes of PRE-COMMIT bytes, and `core.autocrlf=true`
stripped the CRs at commit time, so the runner held different bytes than the
manifest described. Both pass locally. Both were invisible locally.

So the local/CI gap is itself a defect, and this closes it. `test-suite` in
`.github/workflows/drift-canary.yml` runs a fixed list of suites and gates on
their exit codes. This runs the SAME list, in the SAME order, and reports the
same thing: per-suite pass/fail plus the exit code.

    python.exe tests/ci_local.py                 # reproduce the CI test-suite job
    python.exe tests/ci_local.py --clean-checkout  # ...with state/ moved aside
    python.exe tests/ci_local.py --suite render    # just one suite

`--clean-checkout` is the important one. It renames the repository's real
`state/` directory aside (never deletes it), runs everything, and restores it in
a `finally` -- so the question "does this suite pass from a clean checkout?" gets
answered locally instead of being rediscovered on CI. The directory is
restored even if a suite fails or the run is interrupted; the restore is
asserted before this exits.
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

#: The EXACT list from the `test-suite` job of
#: `.github/workflows/drift-canary.yml`, in the same order, so this runner and
#: the workflow run the same thing. `--list` prints it.
#:
#: KNOWN DRIFT RISK, stated rather than hidden: this file is a copy of what the
#: workflow runs, and a copy can go stale. `tests/workflows/test_workflow_lint.py`
#: owns the workflow-side lint and is the right place for a test that greps the
#: workflow for each command above; that test was not added here because this
#: task did not own `tests/workflows/`. Re-check this list when the workflow's
#: `run_suite` block changes.
#:
#: The 2026-10-08 breaking-chain registration is the worked example of what this
#: risk costs: three suites existed, all green locally, and NONE of them was in
#: either list, so CI proved nothing about the fix that added them.
SUITES = (
    ("render", [sys.executable, "-m", "unittest", "discover",
                "-s", "tests/render", "-t", ".", "-v"]),
    ("collection", [sys.executable, "-m", "unittest", "discover",
                    "-s", "tests/collection", "-t", ".", "-v"]),
    ("validate", [sys.executable, "-m", "unittest", "discover",
                  "-s", "tests/validate", "-t", ".", "-v"]),
    ("weather", [sys.executable, "-m", "unittest", "discover",
                 "-s", "tests/weather", "-t", ".", "-v"]),
    ("academic", [sys.executable, "-m", "unittest", "discover",
                  "-s", "tests/academic", "-t", ".", "-v"]),
    ("ai", [sys.executable, "tests/ai/test_ai_classifier.py"]),
    ("breaking_monitor", [sys.executable, "tests/breaking/test_breaking_monitor.py"]),
    # Registered 2026-10-08, in the same order as the `run_suite` block of the
    # `test-suite` job above. These three ran locally only until then, which is
    # how the Worker/Python gate divergence survived a green suite: nothing that
    # CI executed ever ran the Worker's OWN gate.
    ("breaking_gate", [sys.executable, "-m", "unittest",
                       "tests.breaking.test_breaking_gate", "-v"]),
    ("breaking_chain", [sys.executable, "-m", "unittest",
                        "tests.breaking.test_breaking_chain", "-v"]),
    ("worker_gate_parity", [sys.executable, "-m", "unittest",
                            "tests.breaking.test_worker_gate_parity", "-v"]),
    ("ladder_sync", [sys.executable, "tests/breaking/test_ladder_sync.py"]),
    # Registered 2026-10-13 with the fast path it guards, and in the same
    # commit as the suite for the reason the comment above gives: on that run a
    # stated 139-death headline tripped the Worker gate, both ladder models
    # failed, the honesty gate fired and no alert was sent. The suite pins the
    # qualification rule to the frozen 616-row corpus AND executes the whole
    # run end to end with a dead ladder, so the mass-casualty class can never
    # silently lose its alert again. Mirrored in drift-canary.yml.
    ("breaking_fast_path", [sys.executable,
                            "tests/breaking/test_hard_evidence_fast_path.py"]),
    # Registered 2026-10-13 with dynamic model selection. The ladder is no
    # longer written down: it is the config seed, extended by what the provider
    # actually serves and reordered by measured health, under a stage-wide time
    # budget. The properties that keep that safe -- the free/active/text gates
    # read from what the CLI reported, health memory demoting a model WITHOUT
    # ever silencing the run, and worst case bounded by the budget rather than
    # by the ladder -- are only observable by running them. Mirrored in
    # drift-canary.yml.
    ("dynamic_ladder", [sys.executable,
                        "tests/breaking/test_dynamic_model_ladder.py"]),
    ("media", [sys.executable, "tests/media/test_media_extraction.py"]),
    ("workflow_lint", [sys.executable, "-m", "unittest", "discover",
                       "-s", "tests/workflows", "-t", ".", "-v"]),
    # Registered alongside U1 itself. This suite pins the two literal ntfy step
    # names that tests/render/test_render_honesty.py slices daily-news.yml by,
    # so a later rename fails HERE with a readable message instead of there
    # with a bare ValueError out of str.index. Registered in the SAME change as
    # the suite because a guard that nobody runs is decoration.
    ("digest", [sys.executable, "-m", "unittest", "discover",
                "-s", "tests/digest", "-t", ".", "-v"]),
    # THE DRIFT THIS FILE'S OWN DOCSTRING WARNS ABOUT, CLOSED. U3 found that
    # `worker_heartbeat` and `pages_middleware_auth` were in the workflow's
    # `run_suite` list and NOT here, so the local runner never executed either
    # of them: the dead-man's switch and the archive's only access control could
    # go red on CI with a green local run. The documented mitigation was "re-check
    # this list when the workflow's `run_suite` block changes" — so this is that
    # re-check, and the tail of the list now matches the workflow's exactly.
    ("worker_heartbeat", [sys.executable, "-m", "unittest",
                          "tests.breaking.test_worker_heartbeat", "-v"]),
    ("pages_middleware_auth", [sys.executable, "-m", "unittest",
                               "tests.security.test_pages_middleware_auth", "-v"]),
    # Registered by unit U3, in the same change as the suite and in the same
    # order as the `run_suite` block of the `test-suite` job above. The delivery
    # guard's two properties are only observable by RUNNING the Worker's own
    # code: that `pending` lands on disk before the POST, and that a fetch path
    # cannot reach the ledger writer at all. A Python suite could re-implement
    # the path and agree with itself forever.
    ("digest_delivery", [sys.executable, "-m", "unittest",
                         "tests.digest.test_digest_delivery", "-v"]),
    # Registered 2026-10-14 with the ntfy wire-shape guard it runs. Its own
    # suite rather than an addition to an existing one on purpose: the pinned
    # suite counts (digest 215, academic 362, breaking_monitor 57) are numbers
    # several suites assert, so growing one to carry this would have moved a
    # number that exists to catch a suite that silently LOST a test. It drives
    # every notification site to the byte ntfy would receive and requires
    # `tags` to be an array and `priority` a number -- the shape that took
    # production down on 2026-10-12. Mirrored in drift-canary.yml.
    ("notification_wire_shape", [sys.executable, "-m", "unittest",
                                 "tests.notifications.test_ntfy_wire_shape_guard",
                                 "-v"]),
    # Registered 2026-10-18, in the same change as the delivery-marker fixes it
    # guards, and in the same order as the `run_suite` block above. It was
    # absent from BOTH lists, so the only suite that proves a broken
    # state/stories.validated.json can neither clear NOR arm the retry ladder --
    # and now also that it reddens the run -- never executed on CI at all. A
    # suite nobody invokes is not a passing suite.
    ("payload_io", [sys.executable, "-m", "unittest",
                    "tests.test_payload_io", "-v"]),
)

#: The other step of the same job, and the two suites the `live-parser-canary`
#: job runs offline. Included because the two CI-only bugs of 2026-09-30 were in
#: these, not in the main list.
EXTRA = (
    ("manifest_guard", [sys.executable, "-m", "unittest",
                        "tests.fixtures.real.test_manifest_freshness"]),
    ("live_canary", [sys.executable, "-m", "unittest", "tests.test_live_canary"]),
)


def _run(name, cmd, verbose):
    started = time.time()
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                          errors="replace")
    elapsed = time.time() - started
    out = proc.stdout or ""
    err = proc.stderr or ""
    ran = _test_count(out + err)
    status = "PASS" if proc.returncode == 0 else "FAIL"
    print("%-4s %-18s exit=%-3d %6.1fs%s"
          % (status, name, proc.returncode, elapsed,
             ("  [%d tests]" % ran) if ran else ""))
    if proc.returncode != 0 or verbose:
        # unittest writes its report to stderr; the scripts write to stdout.
        for stream, label in ((err, "stderr"), (out, "stdout")):
            if not stream.strip():
                continue
            print("----- %s %s -----" % (name, label))
            print(stream.strip()[-6000:])
    return proc.returncode


def _test_count(text):
    """The `Ran N tests` line, which is the only count that cannot go stale."""
    for line in text.splitlines():
        if line.startswith("Ran ") and " test" in line:
            parts = line.split()
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return 0
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the CI test-suite job locally.")
    parser.add_argument("--clean-checkout", action="store_true",
                        help="move the real state/ aside for the run and "
                             "restore it afterwards, so a clean-checkout "
                             "failure cannot hide behind a local artifact")
    parser.add_argument("--suite", action="append", default=None,
                        help="run only this suite (repeatable)")
    parser.add_argument("--extra", action="store_true",
                        help="also run the manifest guard and the live canary "
                             "suite that live-parser-canary runs offline")
    parser.add_argument("--list", action="store_true",
                        help="print the suite list and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every suite's output, not just failures")
    args = parser.parse_args(argv)

    if args.list:
        for name, cmd in SUITES + EXTRA:
            print("%-18s %s" % (name, " ".join(cmd[1:])))
        return 0

    suites = SUITES + (EXTRA if args.extra else ())
    if args.suite:
        wanted = set(args.suite)
        unknown = wanted - {n for n, _ in suites}
        if unknown:
            parser.error("unknown suite(s): %s" % ", ".join(sorted(unknown)))
        suites = tuple(s for s in suites if s[0] in wanted)

    state_dir = os.path.join(REPO_ROOT, "state")
    moved_to = None
    if args.clean_checkout:
        if os.path.isdir(state_dir):
            holder = tempfile.mkdtemp(prefix="ci-local-state-aside-")
            moved_to = os.path.join(holder, "state")
            os.rename(state_dir, moved_to)
            print("state/ moved aside -> %s (restored on exit, never deleted)"
                  % moved_to)
        else:
            print("state/ is absent already: this IS the clean-checkout case")

    failed = []
    try:
        for name, cmd in suites:
            if _run(name, cmd, args.verbose) != 0:
                failed.append(name)
    finally:
        if moved_to is not None:
            # Restored unconditionally, and verified: a runner that quietly
            # leaves the owner's runtime state in a temp directory is worse
            # than the bug it was built to find.
            os.rename(moved_to, state_dir)
            shutil.rmtree(os.path.dirname(moved_to), ignore_errors=True)
            if not os.path.isdir(state_dir):
                print("FATAL: state/ was not restored", file=sys.stderr)
                return 2
            print("state/ restored")

    if failed:
        print("::error::failing test suites: %s" % " ".join(failed))
        return 1
    print("All suites green (exit codes 0; per-suite counts above).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
