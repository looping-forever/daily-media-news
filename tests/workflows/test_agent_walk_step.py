"""The digest agent step's budgeted model walk, EXECUTED.

WHY THIS FILE EXISTS
--------------------
Run 39117668287 discovered ten free models and walked exactly three: two hit the
600s cap, the third exited 1, and the slot produced NO digest while seven models
sat untried. The fix made the walk stop on the CLOCK instead of on a count.

On the first real run after that fix, attempt 1 succeeded -- so the walk's
CONTINUATION path (a model fails, the walk moves on instead of aborting) had
never executed anywhere except in a Python simulation. The shell loop is the
shipped artefact; a simulation of it proves nothing about it. This module RUNS
the shipped loop, out of the parsed YAML, and asserts what it does.

WHAT IS REAL HERE
  * REAL: the `run:` block itself, read out of .github/workflows/daily-news.yml
    by `W.iter_run_blocks` -- the same extractor the workflow-lint suite uses,
    so the text executed is the shipped text and not a copy of it.
  * REAL: bash, `mapfile`, the stdout-is-only-models contract, the arithmetic,
    and every `python3 -c` the step runs (a real interpreter, on real files it
    writes: the JSON validity check and the timings write).
  * STUBBED: `opencode` (no model is contacted, no network, no delivery) and the
    selector's `--select` call, which discovers the pool by asking the provider
    over the network. The selector is not the subject here and has its own
    suite; the WALK is the subject.

HOW THE DEADLINE IS MADE OBSERVABLE WITHOUT SLEEPING
----------------------------------------------------
`BASH_ENV` installs shell FUNCTIONS that shadow `date` and `timeout`, the same
mechanism tests/workflows/test_digest_prebuild.py uses to shadow `sleep` -- a
function shadows an external command whatever a Windows-style `PATH` says.

`date +%s` becomes a clock that advances `$STUB_CLOCK_STEP` seconds per call, so
a walk that would take 25 minutes of wall clock is over in milliseconds while the
step's own arithmetic stays untouched. With a 0 step the clock is frozen, which
is how the "the budget must not bind" cases are set up.

`timeout` records the allowance it was handed and then runs its command. The
stub cannot hang, so there is nothing for a real `timeout` to kill -- recording
the value is what lets a test assert that no zero, negative or non-integer
allowance is ever handed over. Allowing `timeout 0` through would be the worst
possible version of this bug: GNU `timeout 0` means NO LIMIT, so a spent budget
would silently buy an unbounded attempt instead of a bounded one.
"""

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
from unittest import mock

try:
    from tests import workflows as W
except ImportError:  # pragma: no cover - direct execution of this file
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests import workflows as W

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = "daily-news.yml"
STEP_NAME = "Run OpenCode agent"

# The selector is read directly for the one property the walk cannot show on its
# own: that the budget is stated BY the selector, so a caller can name the CASE
# (`--no-work`) but never the number.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import model_selector as ms  # noqa: E402


# --------------------------------------------------------------------------
# The extractor: the SHIPPED text, never a copy of it
# --------------------------------------------------------------------------


def agent_step_run_block():
    """The `Run OpenCode agent` step's `run:` text, from the parsed workflow."""
    for _job, step, run, _step in W.iter_run_blocks(WORKFLOW):
        if step == STEP_NAME:
            return run
    raise AssertionError(
        "no `run:` block named %r in %s. The walk lives in that step; if it was "
        "renamed, this test must be pointed at the new name deliberately."
        % (STEP_NAME, WORKFLOW)
    )


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------

#: `opencode`, replaced by a real executable first on PATH.
#:
#: `run --auto --model <m>` is answered according to `$STUB_OPENCODE_PLAN`, a
#: space-separated list of `<model>:<outcome>` pairs:
#:
#:   ok       write a VALID state/stories.json and exit 0
#:   garbage  write INVALID JSON and exit 0 -- an exit code is not evidence
#:   nofile   write nothing and exit 0
#:   notfound the REAL measured shape: UnknownError on the surface and
#:            ProviderModelNotFoundError in the debug text, exit 1
#:   unknown  UnknownError with NO not-found marker -- ambiguous, an outage
#:   quota    429 with Retry-After, exit 1
#:   auth     401 invalid api key, exit 1
#:   authstamp the MEASURED 2026-10-27 shape: an outage whose only "401" is the
#:            CLI's own millisecond timestamp. Must classify provider_outage.
#:   <n>      exit n (124 is what `timeout` reports for its own kill)
#:
#: A model with no plan entry exits 1, so an attempt nobody expected is loud
#: instead of being silently mistaken for a success. Every invocation appends
#: its model to `$STUB_OPENCODE_LOG`, which is how ORDER and "it stopped there"
#: are asserted.
_STUB_OPENCODE = r"""#!/usr/bin/env bash
set -u
cat >/dev/null                      # drain the prompt on stdin
model=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--model" ]; then model="$arg"; fi
  prev="$arg"
done
printf '%s\n' "$model" >> "${STUB_OPENCODE_LOG:?}"
outcome=""
for entry in ${STUB_OPENCODE_PLAN:-}; do
  if [ "${entry%%:*}" = "$model" ]; then outcome="${entry#*:}"; fi
done
if [ -z "$outcome" ]; then outcome="1"; fi
case "$outcome" in
  ok)      printf '{"stories": [{"id": "stub"}]}\n' > state/stories.json ; exit 0 ;;
  ok_prose)
    # A SUCCESSFUL digest whose article text contains the classifier's own
    # vocabulary. The walk must call this a success: classifying a finished
    # attempt would throw it away as a quota skip.
    printf '{"stories": [{"id": "stub"}]}\n' > state/stories.json
    printf 'Draft note: the exchange kept the rate limit and the quota unchanged.\n'
    exit 0 ;;
  garbage) printf '{"stories": [ not json\n' > state/stories.json ; exit 0 ;;
  nofile)  exit 0 ;;
  notfound)
    printf 'UnknownError: Unexpected server error\n' >&2
    printf 'ProviderModelNotFoundError: Model not found: %s\n' "$model" >&2
    exit 1 ;;
  unknown) printf 'UnknownError: Unexpected server error\n' >&2 ; exit 1 ;;
  quota)   printf '429 Too Many Requests (Retry-After: 30)\n' >&2 ; exit 1 ;;
  auth)    printf '401 Unauthorized: invalid api key\n' >&2 ; exit 1 ;;
  authstamp)
    # VERBATIM 2026-10-27 16:18:47Z. The fatal line is an outage; the ONLY
    # "401" in the log is the clock's millisecond field. Classified `auth`, it
    # aborted the walk with a credential message that was never true.
    printf 'timestamp=2026-10-27T16:18:47.401Z level=INFO run=576d6804 message="all formatters are disabled"\n' >&2
    printf 'AI_APICallError: Upstream request failed: Model is unavailable.\n' >&2
    exit 1 ;;
  *)       exit "$outcome" ;;
esac
"""

#: `python3`, which this repository's harnesses cannot otherwise assume: Windows
#: ships no `python3` that Git-for-Windows bash can execute. TWO jobs, and only
#: two:
#:
#:   1. forward every ordinary call to the real interpreter, so the step's own
#:      `python3 -c` validity check and timings write run for REAL;
#:   2. answer `scripts/model_selector.py` from `$STUB_POOL`, because the real
#:      selector discovers the pool over the network. It writes the same info
#:      JSON keys the real CLI writes, so the step's own `jq` reads a real file
#:      and the budget/per-attempt/minimum all arrive down the shipped path.
_STUB_PYTHON3 = r"""#!/usr/bin/env bash
set -u
real="${STUB_REAL_PYTHON:?}"
# THE WATCHDOG IS A TEST STAND-IN HERE, and only here: this shim replaces
# `python3`, so it can hand the child straight to bash. The watchdog's own
# silence behaviour is tested DIRECTLY against the real script (see
# tests/workflows/test_model_circuit_breaker.py WatchdogTests); what these
# walk tests exercise is the step's decision after an attempt fails.
for arg in "$@"; do
  if [ "${arg##*/}" = "model_watchdog.py" ]; then
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
    if [ $# -gt 0 ]; then shift; fi
    exec "$@"
  fi
done
is_selector=0
for arg in "$@"; do
  if [ "${arg##*/}" = "model_selector.py" ]; then is_selector=1; fi
done
if [ "$is_selector" -ne 1 ]; then
  exec "$real" "$@"
fi
# CLASSIFICATION IS NOT DISCOVERY. `--classify` is pure text analysis, so it is
# forwarded to the real selector: stubbing it would replace the shipped
# circuit breaker with a shim that always says "other".
for arg in "$@"; do
  if [ "$arg" = "--classify" ]; then exec "$real" "$@"; fi
done
info_out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--info-out" ]; then info_out="$arg"; fi
  prev="$arg"
done
if [ -n "$info_out" ]; then
  # HOW MANY TIMES THE LADDER WAS RESOLVED, and optionally a DIFFERENT pool on
  # the second resolution. A retry that reuses cycle 1's list would be
  # indistinguishable from one that re-reads it without these two affordances.
  sel_n=0
  if [ -n "${STUB_SELECT_LOG:-}" ]; then
    if [ -f "$STUB_SELECT_LOG" ]; then sel_n=$(wc -l < "$STUB_SELECT_LOG"); fi
    printf 'select\n' >> "$STUB_SELECT_LOG"
  fi
  pool="${STUB_POOL:-}"
  if [ "$sel_n" -ge 1 ] && [ -n "${STUB_POOL_2:-}" ]; then pool="$STUB_POOL_2"; fi
  for m in $pool; do printf '%s\n' "$m"; done
  STUB_POOL_EFFECTIVE="$pool" "$real" -c 'import json,os,sys;pool=(os.environ.get("STUB_POOL_EFFECTIVE") or "").split();json.dump({"source":"stub","provider":"opencode","seed":[],"discovered":pool,"rejected":{},"unparsed":[],"error":"","dropped_seed":[],"cooled_down":[],"selected":pool,"cache_fetched_at":None,"cache_age_sec":None,"per_attempt_sec":int(os.environ["STUB_PER"]),"total_budget_sec":int(os.environ["STUB_BUDGET"]),"min_attempt_sec":int(os.environ["STUB_MIN"])},open(sys.argv[1],"w",encoding="utf-8"),indent=2)' "$info_out"
  printf 'stub selector: pool=%s budget=%ss per=%ss min=%ss\n' \
    "$pool" "${STUB_BUDGET:-}" "${STUB_PER:-}" "${STUB_MIN:-}" >&2
  exit 0
fi
printf '%s\n' "$*" >> "${STUB_RECORD_LOG:?}"
exit 0
"""

#: Sourced by `bash -c` through `BASH_ENV` before the step's script runs.
#:
#: The tick counter lives in a FILE, not a shell variable, and that is the whole
#: subtlety: the step reads the clock as `$(date +%s)`, a COMMAND SUBSTITUTION,
#: which runs in a subshell -- so a `_STUB_TICKS=...` assignment inside the
#: function is discarded with that subshell and every call returns the same
#: instant. (Verified the hard way: with a variable, all five attempts of a
#: 1000s budget were granted the full 360s and the walk never reached the
#: floor.) A file is shared state and survives the subshell.
_BASH_ENV_HOOK = r"""date() {
  if [ "${1:-}" = "+%s" ]; then
    n=0
    if [ -f "${STUB_CLOCK_FILE:?}" ]; then
      n=$(cat "${STUB_CLOCK_FILE}")
    fi
    case "${n:-}" in ''|*[!0-9]*) n=0 ;; esac
    n=$(( n + 1 ))
    printf '%s\n' "$n" > "${STUB_CLOCK_FILE}"
    echo $(( ${STUB_CLOCK_BASE:-1700000000} + n * ${STUB_CLOCK_STEP:-0} ))
  else
    command date "$@"
  fi
}
timeout() {
  case "${1:-}" in
    ''|*[!0-9]*) printf 'NON-INTEGER %s\n' "${1:-}" >> "${STUB_TIMEOUT_LOG:?}" ;;
    0)           printf 'ZERO\n' >> "${STUB_TIMEOUT_LOG:?}" ;;
    *)           printf '%s\n' "$1" >> "${STUB_TIMEOUT_LOG:?}" ;;
  esac
  shift
  "$@"
}
sleep() {
  # A retry backoff is minutes long. It is RECORDED and skipped, never slept:
  # the value is the part that matters (and a test that really slept would look
  # exactly like a hung suite). No clock tick is consumed, so the deadline
  # arithmetic stays a pure function of the step's own `date +%s` calls.
  if [ -n "${STUB_SLEEP_LOG:-}" ]; then printf '%s\n' "${1:-}" >> "$STUB_SLEEP_LOG"; fi
}
"""


#: The same `python3` shim, with the REAL SELECTOR'S BUDGET CONTRACT mirrored:
#: the caller names the case, the selector decides what it costs, so the budget
#: written into the info JSON is a function of `--no-work` alone.
#:
#: WHY A SECOND SHIM. The shipped step reads the budget OUT of the info JSON and
#: never states it, so the only way to show that a smaller walk really arrives is
#: to make the number depend on the flag arriving. Computing it in the test
#: instead would assert the harness rather than the step.
_STUB_PYTHON3_NO_WORK = r"""#!/usr/bin/env bash
set -u
real="${STUB_REAL_PYTHON:?}"
for arg in "$@"; do
  if [ "${arg##*/}" = "model_watchdog.py" ]; then
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
    if [ $# -gt 0 ]; then shift; fi
    exec "$@"
  fi
done
is_selector=0
for arg in "$@"; do
  if [ "${arg##*/}" = "model_selector.py" ]; then is_selector=1; fi
done
if [ "$is_selector" -ne 1 ]; then
  exec "$real" "$@"
fi
for arg in "$@"; do
  if [ "$arg" = "--classify" ]; then exec "$real" "$@"; fi
done
budget="${STUB_BUDGET:?}"
for arg in "$@"; do
  if [ "$arg" = "--no-work" ]; then budget="${STUB_NO_WORK_BUDGET:?}"; fi
done
info_out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--info-out" ]; then info_out="$arg"; fi
  prev="$arg"
done
if [ -z "$info_out" ]; then exit 0; fi
for m in ${STUB_POOL:-}; do printf '%s\n' "$m"; done
STUB_BUDGET_EFFECTIVE="$budget" "$real" -c 'import json,os,sys;pool=(os.environ.get("STUB_POOL") or "").split();json.dump({"source":"stub","provider":"opencode","seed":[],"discovered":pool,"rejected":{},"unparsed":[],"error":"","dropped_seed":[],"cooled_down":[],"selected":pool,"cache_fetched_at":None,"cache_age_sec":None,"per_attempt_sec":int(os.environ["STUB_PER"]),"total_budget_sec":int(os.environ["STUB_BUDGET_EFFECTIVE"]),"min_attempt_sec":int(os.environ["STUB_MIN"])},open(sys.argv[1],"w",encoding="utf-8"),indent=2)' "$info_out"
if [ "$budget" = "${STUB_BUDGET:?}" ]; then note=""; else note=" no_work=yes"; fi
printf 'stub selector: pool=%s budget=%ss%s\n' "${STUB_POOL:-}" "$budget" "$note" >&2
exit 0
"""


class AgentWalkHarness(unittest.TestCase):
    """Runs the shipped agent step in a throwaway workspace.

    Nothing here asserts anything by itself; `run_step` returns the completed
    process plus the artefacts the step's collaborators recorded.
    """

    def require_bash(self):
        """The bash path, or FAIL.

        `fail` and not `skip` on purpose, following the workflow-lint suite: a
        skipped test reports green while the walk stays unexecuted, which is the
        exact silence this module was written to end.
        """
        bash = W.bash_executable()
        if bash is None:
            self.fail(
                "bash was not found on PATH, so the digest agent step's model "
                "walk CANNOT be executed.\n"
                "This test fails instead of skipping on purpose: a skipped walk "
                "test is a green CI run in which the walk's continuation path "
                "has still never run. Install bash rather than editing this."
            )
        return bash

    # -- workspace ---------------------------------------------------------

    def setUp(self):
        self.bash = self.require_bash()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-walk-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        (self.tmp / "state").mkdir(parents=True, exist_ok=True)
        (self.tmp / "prompts").mkdir(parents=True, exist_ok=True)
        # The REAL prompt, because the step really does `cat` it. Its content is
        # irrelevant to the loop (the stub drains stdin), so a missing file here
        # should fail loudly rather than be papered over with a stub.
        shutil.copy(str(REPO_ROOT / "prompts" / "daily-digest.md"),
                    str(self.tmp / "prompts" / "daily-digest.md"))
        # The shipped step invokes scripts/model_watchdog.py by a path relative
        # to the repository root, so the throwaway workspace must contain it for
        # the same reason it contains the real prompt: a workspace missing a
        # file the step uses tests a tree that is never shipped.
        (self.tmp / "scripts").mkdir(exist_ok=True)
        # render_prompt.py resolves the prompt's {{TOKEN}} placeholders, and
        # private_values.py is what it reads them from. Both are copied for the
        # same reason as model_watchdog.py: the step really runs them.
        for name in ("model_watchdog.py", "model_selector.py",
                     "render_prompt.py", "private_values.py"):
            shutil.copy(str(REPO_ROOT / "scripts" / name),
                        str(self.tmp / "scripts" / name))
        self.bin_dir = self.tmp / "bin"
        W.install_stub_on_path(
            self.bin_dir, _STUB_OPENCODE, name="opencode")
        W.install_stub_on_path(
            self.bin_dir, _STUB_PYTHON3, name="python3")
        self.hook = self.tmp / "bash_env.sh"
        self.hook.write_text(_BASH_ENV_HOOK, encoding="utf-8", newline="\n")

    def step_env(self):
        """The step's own `env:` block, with its `${{ }}` resolved.

        Resolved rather than dropped so the step runs with the variables it
        declares, and asserted to contain no leftover expression: a harness that
        quietly passes `${{ env.TODAY }}` through tests a step nobody ships.
        """
        resolved = {
            "TODAY": "2026-10-18",
            "DIGEST_TIME": "morning",
            "TZ": "Asia/Kathmandu",
            "CANDIDATES_PATH": "%s/state/candidates.json" % self.tmp,
        }
        for _job, step, _run, st in W.iter_run_blocks(WORKFLOW):
            if step != STEP_NAME:
                continue
            for key, raw in (st.get("env") or {}).items():
                text = str(raw)
                text = text.replace("${{ env.TODAY }}", resolved["TODAY"])
                text = text.replace("${{ steps.digest_time.outputs.time }}",
                                    resolved["DIGEST_TIME"])
                text = text.replace("${{ github.workspace }}", str(self.tmp))
                self.assertNotIn(
                    "${{", text,
                    "env[%s] carries an expression this harness does not "
                    "resolve: %r" % (key, text))
                resolved[key] = text
            return resolved
        self.fail("the step %r has no env: block to read" % STEP_NAME)

    # -- execution ---------------------------------------------------------

    def run_step(self, pool=(), plan="", budget=1800, per=360,
                 min_attempt=90, clock_step=0, extra_env=None):
        """Execute the shipped `run:` block; return (proc, artefacts).

        `extra_env` carries what a LATER step exported through `$GITHUB_ENV` --
        the candidate gate's verdict is handed to this step exactly that way, and
        a real job passes it the same way (the step's own `env:` block does not
        declare it).
        """
        log = self.tmp / "opencode_calls.txt"
        timeout_log = self.tmp / "timeout_args.txt"
        record_log = self.tmp / "record_calls.txt"
        clock = self.tmp / "clock.txt"
        sleep_log = self.tmp / "sleep_args.txt"
        select_log = self.tmp / "select_calls.txt"
        for path in (log, timeout_log, record_log, clock, sleep_log, select_log):
            path.write_text("", encoding="utf-8")
        env = self.step_env()
        env.update({
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "BASH_ENV": str(self.hook),
            "GITHUB_OUTPUT": str(self.tmp / "gh_output.txt"),
            "GITHUB_ENV": str(self.tmp / "gh_env.txt"),
            "STUB_REAL_PYTHON": sys.executable,
            "STUB_POOL": " ".join(pool),
            "STUB_OPENCODE_PLAN": plan,
            "STUB_OPENCODE_LOG": str(log),
            "STUB_TIMEOUT_LOG": str(timeout_log),
            "STUB_SLEEP_LOG": str(sleep_log),
            "STUB_SELECT_LOG": str(select_log),
            "STUB_RECORD_LOG": str(record_log),
            "STUB_CLOCK_FILE": str(clock),
            "STUB_BUDGET": str(budget),
            "STUB_PER": str(per),
            "STUB_MIN": str(min_attempt),
            "STUB_CLOCK_STEP": str(clock_step),
        })
        env.update(extra_env or {})
        proc = W.run_bash_script(agent_step_run_block(), env_overrides=env,
                                 cwd=self.tmp, bash=self.bash, timeout=120)
        return proc, {
            "models": log.read_text(encoding="utf-8").split(),
            "allowances": timeout_log.read_text(encoding="utf-8").split(),
            "naps": sleep_log.read_text(encoding="utf-8").split(),
            "selects": select_log.read_text(encoding="utf-8").split(),
            "records": [line.split()[-2:]
                        for line in record_log.read_text(encoding="utf-8").splitlines()
                        if line.split()],
            "stories": (self.tmp / "state" / "stories.json"),
        }

    # -- readers -----------------------------------------------------------

    @staticmethod
    def attempts(stdout):
        """[(n, model, allowance)] in the order the step announced them."""
        pattern = re.compile(
            r"^=== Attempt (\d+) with (\S+) \(allow (\d+)s\) ===$", re.M)
        return [(int(n), model, int(allow))
                for n, model, allow in pattern.findall(stdout)]

    def assertGreen(self, proc, label):
        self.assertEqual(
            0, proc.returncode,
            "%s: the step must succeed. stdout:\n%s\nstderr:\n%s"
            % (label, proc.stdout, proc.stderr))


# ==========================================================================
# 0. The no-work walk -- the gate names the case, the selector owns the number
# ==========================================================================


class NoWorkBudgetTests(AgentWalkHarness):
    """A run with nothing to write must be BOUNDED, never SKIPPED.

    `prompts/daily-digest.md` FAILURE MODE requires a digest even when the
    collection produced nothing (`research_status` "limited", the reason in
    `notes`), so the candidate gate may only change how long the walk may take.
    The waste it exists to remove is measured: nine October digest runs spent
    1,168-1,936s each and produced nothing.
    """

    NO_WORK = 300

    def install_no_work_selector(self, full=1800):
        W.install_stub_on_path(
            self.bin_dir, _STUB_PYTHON3_NO_WORK, name="python3")
        return {"STUB_BUDGET": str(full), "STUB_NO_WORK_BUDGET": str(self.NO_WORK)}

    def test_no_work_bounds_the_walk_and_never_skips_the_agent(self):
        env = self.install_no_work_selector()
        env["MODEL_NO_WORK"] = "1"
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"], plan="opencode/a:ok",
            extra_env=env)
        self.assertGreen(proc, "no-work walk")
        self.assertEqual(
            ["300"], art["allowances"],
            "the walk must be handed the allowance the selector states for the "
            "case the gate named -- the no-work budget, not the full per-attempt "
            "cap. Allowances recorded: %r\nstdout:\n%s"
            % (art["allowances"], proc.stdout))
        self.assertEqual(["opencode/a"], art["models"])
        self.assertTrue(
            art["stories"].is_file(),
            "the agent must STILL run and still write state/stories.json: the "
            "gate changes the budget, it never skips the agent")
        self.assertIn("Agent wrote valid state/stories.json", proc.stdout)

    def test_the_full_walk_is_used_when_the_gate_did_not_speak(self):
        env = self.install_no_work_selector()
        proc, art = self.run_step(pool=["opencode/a"], plan="opencode/a:ok",
                                  extra_env=env)
        self.assertGreen(proc, "full walk")
        self.assertEqual(
            ["360"], art["allowances"],
            "with no verdict from the gate the walk keeps its full per-attempt "
            "cap")

    def test_a_walk_that_spends_the_no_work_budget_fails_honestly(self):
        """Bounded AND honest: never a green step with no digest behind it."""
        env = self.install_no_work_selector()
        env["MODEL_NO_WORK"] = "1"
        pool = ["opencode/a", "opencode/b", "opencode/c", "opencode/d"]
        proc, art = self.run_step(pool=pool, plan="opencode/a:124",
                                  clock_step=100, extra_env=env)
        self.assertEqual(
            1, proc.returncode,
            "a walk that spends its budget without a digest must FAIL the step, "
            "not finish green. stdout:\n%s" % proc.stdout)
        self.assertLess(
            len(art["models"]), len(pool),
            "the no-work budget must stop the walk before it walks the whole "
            "pool. Invoked: %r" % (art["models"],))
        self.assertIn("Digest not built", proc.stdout)
        self.assertFalse(art["stories"].is_file(),
                         "a failed walk leaves no stories.json behind")


class SelectorStatesTheNoWorkBudgetTests(unittest.TestCase):
    """The number is stated by the selector; the caller only names the case."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="selector-nowork-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def select_info(self, *extra):
        """Run the REAL `--select` path and return the info JSON it wrote."""
        info_path = self.tmp / "info.json"
        argv = ["--select", "--all",
                "--config", str(REPO_ROOT / "config" / "opencode.json"),
                "--health", str(self.tmp / "health.json"),
                "--cache", str(self.tmp / "cache.json"),
                "--info-out", str(info_path)] + list(extra)
        # The pin is a TEST-ONLY affordance and is read only when the CLI is
        # genuinely absent (see PIN_ENV in scripts/model_selector.py), so the
        # pool is deterministic here and no provider is contacted.
        pin = str(REPO_ROOT / "tests" / "fixtures" / "model_selector_pin.json")
        with mock.patch.object(ms.shutil, "which", lambda _name: None), \
                mock.patch.dict(os.environ, {"MODEL_SELECTOR_PIN": pin}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            ms.reset_discovery_memo()
            ms._main(argv)
        with open(info_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_full_budget_is_the_default(self):
        info = self.select_info()
        self.assertEqual(ms.WALK_TOTAL_BUDGET_SEC, info["total_budget_sec"])
        self.assertNotIn("no_work", info)

    def test_no_work_states_the_smaller_budget_and_the_reason(self):
        info = self.select_info("--no-work")
        self.assertEqual(ms.WALK_NO_WORK_TOTAL_BUDGET_SEC,
                         info["total_budget_sec"])
        self.assertTrue(info["no_work"])
        self.assertIn("limited", info["budget_reason"],
                      "the info must say WHY the walk is shorter, so the log of "
                      "a short run explains itself")

    def test_naming_the_case_never_enlarges_a_bound_the_caller_set(self):
        info = self.select_info("--no-work", "--budget", "120")
        self.assertEqual(120, info["total_budget_sec"])


# ==========================================================================
# 1. Continuation -- a failed model must not end the walk
# ==========================================================================


class ContinuationTests(AgentWalkHarness):
    def test_a_failed_model_does_not_end_the_walk(self):
        """The path that has never run in production.

        Attempt 1 exits 1, attempt 2 exits 124, attempt 3 writes a valid
        stories.json. A walk that aborted on the first failure -- which is what
        the step would do if the continuation were ever lost -- leaves the last
        two models untried, and this test says so with the invocation log.
        """
        pool = ["opencode/a", "opencode/b", "opencode/c", "opencode/d"]
        proc, art = self.run_step(
            pool=pool, plan="opencode/a:1 opencode/b:124 opencode/c:ok")
        self.assertGreen(proc, "continuation")
        self.assertEqual(
            ["opencode/a", "opencode/b", "opencode/c"], art["models"],
            "the walk must try each model in turn and STOP at the first "
            "success -- `opencode/d` runs only if a failure ended the walk "
            "early or the success did not stop it. Invoked: %r\nstdout:\n%s"
            % (art["models"], proc.stdout))
        self.assertEqual(
            [(1, "opencode/a", 360), (2, "opencode/b", 360),
             (3, "opencode/c", 360)], self.attempts(proc.stdout),
            "attempts must be numbered in pool order, each naming its model and "
            "the allowance it was given. stdout:\n%s" % proc.stdout)
        self.assertIn("Agent wrote valid state/stories.json on attempt 3 "
                      "(opencode/c)", proc.stdout)
        self.assertNotIn("Digest not built", proc.stdout)
        self.assertTrue(art["stories"].is_file(),
                        "the successful attempt's state/stories.json must "
                        "survive to the end of the step")
        # The bookkeeping names the REAL class of each failure: 1 is not a
        # timeout, and calling it one would poison health memory with a lie.
        self.assertEqual(
            [["opencode/a", "transport"], ["opencode/b", "timeout"],
             ["opencode/c", "ok"]], art["records"],
            "each attempt must record its own outcome class: rc=1 transport, "
            "rc=124 timeout, success ok")

    def test_a_later_failure_still_stops_at_the_first_success(self):
        """Order is not luck: success on attempt 2 must stop it at attempt 2."""
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b", "opencode/c"],
            plan="opencode/a:124 opencode/b:ok opencode/c:ok")
        self.assertGreen(proc, "stop-at-first-success")
        self.assertEqual(["opencode/a", "opencode/b"], art["models"],
                         "the walk must stop at the first success, not carry on "
                         "down the pool. Invoked: %r" % (art["models"],))
        self.assertEqual([["opencode/a", "timeout"], ["opencode/b", "ok"]],
                         art["records"])


# ==========================================================================
# 1b. An auth refusal is PER MODEL until it repeats
# ==========================================================================


class AuthIsPerModelTests(AgentWalkHarness):
    """A single `auth` may not end the walk; a shared credential still must.

    Measured 2026-10-27 16:18Z, run 40539483837: attempt 10 of 12 returned
    `auth`, the walk stopped there, and six quota refusals that had just proved
    the pool was throttled were left with the 3/6/12-minute backoff cycles
    unspent -- the edition was lost in 75s. The credential was never refused:
    the log's own timestamp (`.401Z`) had been read as a 401.
    """

    def test_one_model_refusing_its_credentials_does_not_end_the_walk(self):
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b", "opencode/c"],
            plan="opencode/a:auth opencode/b:ok")
        self.assertGreen(proc, "auth-then-success")
        self.assertEqual(
            ["opencode/a", "opencode/b"], art["models"],
            "the walk must continue past ONE auth refusal and try the next "
            "model; stopping there is the 2026-10-27 loss. Invoked: %r\n"
            "stdout:\n%s" % (art["models"], proc.stdout))
        self.assertEqual(
            [["opencode/a", "auth"], ["opencode/b", "ok"]], art["records"],
            "the refusal must be recorded as `auth`, not as `transport`: the "
            "class is the evidence, and health memory should carry it")
        self.assertIn(
            "Circuit breaker: opencode/a -> auth. One model only, so the "
            "credential is not the problem; trying the next model.", proc.stdout)
        self.assertNotIn("walk aborted", proc.stdout)

    def test_a_second_refusal_is_the_shared_credential_and_stops_the_walk(self):
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b", "opencode/c"],
            plan="opencode/a:auth opencode/b:auth opencode/c:ok")
        self.assertEqual(
            1, proc.returncode,
            "two models refusing the same credential is the key, not a model: "
            "the run must fail rather than report a digest. stdout:\n%s"
            % proc.stdout)
        self.assertEqual(
            ["opencode/a", "opencode/b"], art["models"],
            "the walk must stop at the SECOND refusal -- `opencode/c` exists "
            "only to prove it was never tried. Invoked: %r" % (art["models"],))
        self.assertIn(
            "Circuit breaker: opencode/b -> auth (2 models refused the "
            "credential). That is the shared key, not one model; stopping the "
            "walk.", proc.stdout)
        self.assertIn("(last error: auth: 2 models rejected the credential, "
                      "walk aborted)", proc.stdout)

    def test_the_measured_timestamp_line_is_an_outage_not_an_auth(self):
        """The exact log that caused the abort, end to end through the step.

        `--classify` is forwarded to the REAL selector by the python3 shim, so
        this asserts the shipped classifier against the shipped walk: if the
        HTTP-status boundary ever loses its dot guard, this test fails here
        instead of in production.
        """
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:authstamp opencode/b:ok")
        self.assertGreen(proc, "measured-timestamp")
        self.assertEqual(
            [["opencode/a", "provider_outage"], ["opencode/b", "ok"]],
            art["records"],
            "`.401Z` is a clock. The attempt must be recorded as the outage it "
            "was, never as an auth refusal")
        self.assertNotIn("-> auth", proc.stdout)


# ==========================================================================
# 2. The failure line -- the owner's one sentence, honest about the cause
# ==========================================================================

class FailureLineTests(AgentWalkHarness):
    def test_the_line_names_the_tried_count_and_the_last_failure_class(self):
        pool = ["opencode/a", "opencode/b", "opencode/c"]
        proc, art = self.run_step(
            pool=pool, plan="opencode/a:124 opencode/b:1 opencode/c:17")
        self.assertEqual(1, proc.returncode,
                         "every model failed: the step must fail. stdout:\n%s"
                         % proc.stdout)
        self.assertIn(
            "Digest not built: 3 of 3 free models tried in 0 min, none produced "
            "a digest (last error: command failed rc=17). The next scheduled run "
            "will retry.", proc.stdout)
        self.assertEqual(
            [["opencode/a", "timeout"], ["opencode/b", "transport"],
             ["opencode/c", "transport"]], art["records"])

    def test_the_line_says_timeout_when_the_last_failure_was_a_timeout(self):
        proc, _art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:1 opencode/b:124")
        self.assertEqual(1, proc.returncode)
        self.assertIn("(last error: timeout)", proc.stdout)
        self.assertNotIn("command failed rc=0", proc.stdout)

    def test_an_exit_zero_without_a_valid_file_is_not_a_success(self):
        """The only success signal is a PARSEABLE state/stories.json.

        A model that exits 0 having written nothing, or having written rubbish,
        is the case where trusting the exit code would ship an empty digest --
        the loudest possible way to be wrong.
        """
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"],
            plan="opencode/a:garbage opencode/b:nofile")
        self.assertEqual(
            1, proc.returncode,
            "the step must FAIL when no attempt produced a valid stories.json, "
            "even though every exit code was 0. stdout:\n%s" % proc.stdout)
        self.assertIn("(last error: no stories.json)", proc.stdout)
        self.assertIn(
            "Attempt 1 finished without a valid state/stories.json", proc.stdout)
        self.assertEqual(
            [["opencode/a", "transport"], ["opencode/b", "transport"]],
            art["records"],
            "an unparseable or missing file is recorded as a transport-class "
            "failure, never as ok")


# ==========================================================================
# 3. The budget floor -- the clock, not a count, ends the walk
# ==========================================================================


class BudgetFloorTests(AgentWalkHarness):
    def test_the_walk_stops_at_the_budget_floor(self):
        """A real deadline, made observable without sleeping.

        The clock advances 250s per call, so with a 1000s budget the remaining
        time really does fall attempt by attempt: 750, 500, 250, then 0. The
        fourth attempt is where the walk must STOP -- the old shape would have
        spent it.
        """
        pool = ["opencode/a", "opencode/b", "opencode/c", "opencode/d",
                "opencode/e"]
        proc, art = self.run_step(
            pool=pool, plan="", budget=1000, per=360, min_attempt=90,
            clock_step=250)
        self.assertEqual(
            [(1, "opencode/a", 360), (2, "opencode/b", 360),
             (3, "opencode/c", 250)], self.attempts(proc.stdout),
            "each attempt takes the smaller of the per-attempt cap and what is "
            "left, so the allowance must FALL as the deadline comes closer. "
            "stdout:\n%s" % proc.stdout)
        self.assertIn(
            "Budget spent: 0s left, below the 90s one attempt needs. Stopping "
            "after 3 of 5 models.", proc.stdout)
        self.assertEqual(["opencode/a", "opencode/b", "opencode/c"],
                         art["models"],
                         "the walk must not start the model the budget cannot "
                         "afford")
        # The reported duration is the step's own arithmetic on the same clock:
        # ticks at START, deadline, 3 x remaining, END.
        self.assertIn(
            "Digest not built: 3 of 5 free models tried in 25 min, none produced "
            "a digest (last error: command failed rc=1). The next scheduled run "
            "will retry.", proc.stdout)
        self.assertEqual(1, proc.returncode)

    def test_a_budget_too_small_for_one_attempt_starts_nothing(self):
        """A tiny budget is enough: no attempt may be started, and the step must
        say so rather than loop, stall or die under `set -e`.
        """
        proc, art = self.run_step(
            pool=["opencode/a", "opencode/b"], plan="", budget=5, per=360,
            min_attempt=90)
        self.assertEqual(1, proc.returncode,
                         "an unaffordable walk must fail the step, not pass "
                         "silently. stdout:\n%s" % proc.stdout)
        self.assertEqual(
            [], self.attempts(proc.stdout),
            "no attempt may be announced when the budget cannot pay for one")
        self.assertEqual([], art["models"], "no model may be invoked")
        self.assertEqual(
            [], art["allowances"],
            "`timeout` must not be called at all -- this is the case where a "
            "missing floor check would hand it a spent or negative allowance")
        self.assertIn(
            "Budget spent: 5s left, below the 90s one attempt needs. Stopping "
            "after 0 of 2 models.", proc.stdout)
        self.assertIn(
            "Digest not built: 0 of 2 free models tried in 0 min, none produced "
            "a digest (last error: unknown). The next scheduled run will retry.",
            proc.stdout)
        for noise in ("integer expression expected", "syntax error",
                      "unbound variable", "command not found"):
            self.assertNotIn(noise, proc.stderr,
                             "the step must not reach the floor by erroring: "
                             "stderr:\n%s" % proc.stderr)

    def test_no_allowance_is_ever_zero_negative_or_non_integer(self):
        """Every value handed to `timeout` is a positive integer.

        `timeout 0` means NO LIMIT in GNU coreutils, so a spent budget that
        reached it would buy an unbounded attempt at exactly the moment the step
        is out of time. This asserts the shape directly on both the ticking and
        the frozen clock.
        """
        cases = (
            ("ticking", dict(pool=["opencode/a", "opencode/b", "opencode/c",
                                   "opencode/d", "opencode/e"],
                             plan="", budget=1000, per=360, min_attempt=90,
                             clock_step=250)),
            ("frozen", dict(pool=["opencode/a", "opencode/b"], plan="",
                            budget=120, per=360, min_attempt=90,
                            clock_step=0)),
            ("capped", dict(pool=["opencode/a"], plan="opencode/a:ok",
                            budget=1800, per=360, min_attempt=90,
                            clock_step=0)),
        )
        for label, kwargs in cases:
            with self.subTest(case=label):
                _proc, art = self.run_step(**kwargs)
                self.assertTrue(art["allowances"],
                                "%s: expected the walk to hand `timeout` at "
                                "least one allowance" % label)
                for allowance in art["allowances"]:
                    self.assertRegex(
                        allowance, r"^[1-9][0-9]*$",
                        "%s: `timeout` was handed %r. It must be a positive "
                        "integer: 0 means NO LIMIT and a negative or non-integer "
                        "value is a defect in the arithmetic." % (label, allowance))

    def test_the_empty_pool_guard_fails_before_any_model_is_tried(self):
        proc, art = self.run_step(pool=[], plan="")
        self.assertEqual(1, proc.returncode)
        self.assertIn("FATAL: empty model ladder", proc.stdout)
        self.assertEqual([], art["models"])
        self.assertEqual([], art["allowances"],
                         "no model, so no attempt and no `timeout` call")
