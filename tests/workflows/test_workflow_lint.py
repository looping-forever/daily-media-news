"""CI guardrail for the shell and YAML embedded in .github/workflows/.

WHY THIS FILE EXISTS
--------------------
On 2026-09-27 two bugs killed every production workflow in this repository
while 425 Python tests stayed green, because not one of them looked at the
workflow YAML or at the shell inside it:

  * Bug A (stream drift) -- the OpenCode flag gate captured
    `opencode run --help` into stdout and grepped stdout for `--auto`.  The
    CLI had moved `--help` to stderr, so a WORKING `--auto` flag read as
    missing, the gate printed
        FATAL: opencode run lacks --auto (config/opencode.json flags)
    and the daily digest died at its pre-spend gate.  Affected
    daily-news.yml, drift-canary.yml and breaking-monitor.yml.
  * Bug B (YAML indentation) -- in academic-result.yml a `python3 - <<'PY'`
    heredoc had its body and its `PY` terminator indented two columns
    inside a block scalar that strips ten.  bash therefore looked for a
    column-0 `PY`, never found one, and the step died with
        here-document at line 4 delimited by end-of-file (wanted 'PY')
    and exit 2 -- AFTER the monitor had already succeeded.

The checks below are the guardrail.  Each asserts something about the
real committed workflows.  Design rules, all deliberate:

  * NO CHECK COUNT IS PINNED HERE, deliberately, for the same reason
    drift-canary.yml pins none: a pinned number is a comment that starts
    lying the moment a check is added and nothing fails when it does.  (This
    docstring used to say "the nine checks" and had been wrong for a dozen
    checks before anyone deleted the number.)
  * A lint that cannot fail is decoration, so checks 2, 4, 10, 11, 14, 15,
    16 and 17 each carry a committed regression test that feeds a
    deliberately broken input and asserts the check REJECTS it.  Check 4
    additionally reconstructs the pre-fix gate shape and proves it dies
    against a stub whose help text is on stderr.
  * Silence caused this outage, so nothing here skips.  If `bash` is
    missing the shell tests call `self.fail()` with an explicit message
    rather than `self.skipTest()`.  A skip renders green in CI while the
    workflows stay unlinted, which is the exact failure mode being fixed.
    (The one disclosed exception is the PyYAML parity test, which cannot
    run at all without PyYAML; the stdlib loader it would cross-check is
    already pinned by a golden-case test that always runs.)
  * Parsing uses the real parser (PyYAML when importable, otherwise the
    stdlib subset loader in tests/workflows/__init__.py).  It never strips
    a hardcoded number of leading spaces -- the trick
    tests/academic/test_exam_notices.py uses, and the reason that test is
    blind to Bug B.
  * Everything is offline: no network, no GitHub API, no Cloudflare, no
    real `opencode`.  The CLI is replaced by a real executable stub placed
    first on PATH inside a temp directory, so the gate under test is the
    workflow's own shell, never a reimplementation of it.
  * There are no pinned test counts anywhere in this file, for the same
    reason drift-canary.yml has none: a pinned number is a comment that
    starts lying the moment a test is added, and nothing fails when it
    does.  Where a count is needed to stop a lint going vacuous, it is a
    deliberately loose floor.
"""

import ast
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from tests import workflows as W
except ImportError:  # pragma: no cover - direct execution of this file
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests import workflows as W


# ==========================================================================
# Check 2 helpers (shared with the regression test that proves they fail)
# ==========================================================================

#: A heredoc redirect is the last thing on its line.  Requiring an
#: identifier-shaped, conventionally-uppercase tag and end-of-line position
#: keeps `a << b` (a shift) and `cat <<< word` (a herestring) out.
HEREDOC_RE = re.compile(r"<<-?\s*'?([A-Z][A-Z0-9_]*)'?\s*$")

#: Heredoc tags whose body is Python and must therefore compile.
PYTHON_HEREDOC_TAGS = ("PY",)


def find_unterminated_heredocs(run_text):
    """Return [(tag, line_number_1based)] for heredocs with no column-0 terminator.

    This is the Bug B check.  The terminator must be a line that is exactly
    the tag, with ZERO leading whitespace: bash does not care about YAML
    indentation, only about the bytes it receives, and a block scalar that
    shifts the body two columns right turns a working heredoc into
    `here-document delimited by end-of-file`.
    """
    lines = run_text.split("\n")
    broken = []
    for idx, line in enumerate(lines):
        m = HEREDOC_RE.search(line)
        if not m:
            continue
        tag = m.group(1)
        if any(later == tag for later in lines[idx + 1:]):
            continue
        broken.append((tag, idx + 1))
    return broken


def heredoc_bodies(run_text, tags=PYTHON_HEREDOC_TAGS):
    """Yield (tag, body) for every closed Python heredoc in `run_text`."""
    lines = run_text.split("\n")
    for idx, line in enumerate(lines):
        m = HEREDOC_RE.search(line)
        if not m or m.group(1) not in tags:
            continue
        tag = m.group(1)
        for end in range(idx + 1, len(lines)):
            if lines[end] == tag:
                yield tag, "\n".join(lines[idx + 1:end])
                break


# ==========================================================================
# Check 6 constant: the secrets allowlist
# ==========================================================================

#: EVERY `secrets.*` name any workflow may reference.
#:
#: Adding a name here is a DELIBERATE ACT, not a formality.  Before this
#: file existed there was no coverage at all: a typo'd or deleted secret
#: name was discovered only at run time, when the digest needed it, i.e.
#: at the worst possible moment.  Listing a name asserts that it is
#: configured in the repository settings.  Conversely, a name that is
#: referenced nowhere is also a failure (`test_allowlist_has_no_stale_names`)
#: so a secret cannot be quietly dropped from the allowlist and then from
#: the workflows while the guardrail still reports green.
ALLOWED_SECRETS = frozenset({
    "CF_ACCOUNT_ID",
    "CF_PAGES_API_TOKEN",
    # U6. `DIGEST_PASSWORD` is the Basic-auth password
    # functions/_middleware.js protects the digest archive with; the seal job
    # uses it to prove the page is live with an AUTHENTICATED GET. It is listed
    # here while NOT YET CONFIGURED on purpose: the allowlist documents which
    # names the workflows expect, and the seal job's graceful-degradation path
    # is what happens until the owner adds it. Listing it does not assert it
    # exists, and `digest-prebuild.yml` treats its absence as a warning rather
    # than a failure, so the workflow is never bricked waiting on it.
    "DIGEST_PASSWORD",
    # U6. The Worker's `?ingest=1` credential, by NAME only. The value is read
    # from the environment into a header and is never printed or written to a
    # file. The Worker refuses every ingest when this is unset, which is
    # fail-closed on its side and is why the seal step says so plainly rather
    # than reporting a successful seal.
    "DIGEST_INGEST_TOKEN",
    "GITHUB_TOKEN",
    "GMAIL_APP_PASSWORD",
    "GMAIL_RECEIVER",
    "GMAIL_USER",
    "NTFY_SERVER",
    "NTFY_TOPIC",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
})

SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


# ==========================================================================
# Check 7 constant
# ==========================================================================

#: `if: always()` steps that are allowed to fail the job after the work has
#: already succeeded.
#:
#: EMPTY IS THE NORM, NOT THE RULE. Every entry here is a DELIBIBERATE,
#: documented exception to a rule that otherwise exists to stop a step from
#: killing an already-successful job (Bug B's exact shape: a step that runs
#: after the real work and can still fail the whole thing). An entry is only
#: honest when the step is SUPPOSED to be the thing that fails -- that is,
#: when failing the job IS the step's job.
ALWAYS_STEP_EXEMPTIONS = {
    # academic-result.yml "Assert academic delivery" is the anti-green-run
    # gate for the whole monitor. It is `if: always()` (it must judge the run
    # even when an earlier step failed), it has NO continue-on-error (a
    # `continue-on-error: true` here would let the one job that exists to
    # turn a silent no-delivery into a red run stay green -- the exact
    # failure this rebuild was commissioned to remove), and its heredoc body
    # exits 1 on purpose when payloads were emitted and not all of them were
    # delivered, or the page did not render. It is placed after the state
    # commit, so it cannot cost the run any state, and its evidence is
    # state/academic_delivery.json. See the delivery contract in
    # tests/test_payload_io.py, which exercises this exact step body.
    ("academic-result.yml", "academic-result", "Assert academic delivery"): (
        "load-bearing by design: the step's purpose is to FAIL a run that "
        "emitted payloads and delivered fewer than all of them. Adding "
        "continue-on-error: true would restore the exact green-but-undelivered "
        "outcome this step exists to make impossible."
    ),
    # academic-result.yml "Academic page is NOT published (private result
    # stays private)" replaced the P0 leak: it used to `cp -r docs/academic`
    # into a staging dir and run `wrangler pages deploy` against the PUBLIC
    # `daily-digest` Pages project, exposing the owner's roll number and a link
    # to the official TU result PDF to any unauthenticated client (the
    # digest-auth Worker that used to sit in front was a proxy and is trivially
    # bypassed at daily-digest.example.pages.dev; it has since been deleted). The
    # step is KEPT, at the exact place the
    # defect lived, as a tripwire in the Actions UI on every run -- so
    # `if: always()` is correct: the notice matters most on the runs where
    # something earlier failed. It is exempted from continue-on-error for the
    # OPPOSITE reason to every other entry here: this step can never be
    # load-bearing, because it publishes nothing, sends nothing and asserts
    # nothing. Every command is an `echo` or a read guarded by `|| echo 0`, so
    # it cannot fail a successful monitor (Bug B's shape); it exists purely to
    # say, in the run log, that a private result was deliberately not published.
    ("academic-result.yml", "academic-result",
     "Academic page is NOT published (private result stays private)"): (
        "reporting only: a tripwire that replaced the P0 `wrangler pages "
        "deploy` of docs/academic. It has no network call, no secret and no "
        "assertion -- it cannot turn a green monitor red, because it does "
        "nothing but echo. continue-on-error would be meaningless here and the "
"step still has to run on `always()` to be visible when a result WAS "
            "detected."
    ),
    # drift-canary.yml "Digest archive is not publicly readable (origin auth
    # canary)" is the only guard in this repository that can notice the digest
    # archive becoming publicly readable again, and it is exempted from
    # continue-on-error for the same reason "Assert academic delivery" is: the
    # step's purpose IS to go red. `if: always()` was added to it on 2026-10-04
    # after real run logs showed it had been SKIPPED in both of that day's Drift
    # Canary failures and had NEVER EXECUTED AT ALL -- a step with no `if:`
    # carries the implicit `success()` condition, so an upstream failure skips
    # it silently. `continue-on-error: true` here would run the check and then
    # swallow a breach into a green job, which is WORSE than not running it: an
    # exposure that cannot turn the run red cannot be actioned. Same rule as
    # Check 7's own docstring -- an exemption is only honest when failing the
    # job is what the step is for.
    ("drift-canary.yml", "docs-lint",
     "Digest archive is not publicly readable (origin auth canary)"): (
        "load-bearing by design: an unauthenticated request served digest "
        "content, or an origin that refused to be measured, must turn the run "
        "red so the owner is paged. `if: always()` makes it run even when an "
        "earlier step failed; `continue-on-error: true` would make a real "
        "breach report green, which is the exact inversion this step's own "
        "comments describe. Do NOT 'fix' the missing continue-on-error here "
        "without reading the step's comment block first."
    ),
    # U6a. The prebuild dead-man's switch. Exempted for the same reason as
    # "Assert academic delivery" above and for the OPPOSITE reason to the two
    # informational arms in this same workflow: `steps.worker` and
    # `steps.dispatch_chain` deliberately end `exit 0` so a red watchdog caused
    # by the thing it watches cannot train the owner to ignore the badge. This
    # step's job is not a report, it is a PROOF that a prebuild produced an
    # artifact, and its whole value is that a broken prebuild turns it red. With
    # `continue-on-error: true` it would print `::error::`, page nothing, and
    # report green -- the silent-outage outcome the architect named as the worst
    # result available here. It is placed last and gated on `always()` so a red
    # digest verdict above cannot suppress it.
    ("digest-watchdog.yml", "watchdog",
     "Check prebuild artifact (dead-man's switch, prebuild arm)"): (
        "load-bearing by design: an artifact that is absent, unreadable or "
        "unstamped means the prebuild handed nothing over, and the slot's push "
        "will have to build the digest from scratch. That is exactly the "
        "failure the prebuild exists to prevent, so failing the job IS this "
        "step's job. `if: always()` is required and sufficient for its own "
        "ordering (last step, so nothing above it is skipped); do NOT add "
        "`continue-on-error: true`."
    ),
    # drift-canary.yml's full-suite step became a step of the docs-lint job on
    # 2026-10-19, when the canary's four jobs were merged into one to stop
    # spending four runner slots a night. It keeps `if: always()`, because it
    # must still run on a night an earlier step already failed -- the stdlib
    # suite is this repository's only enforcement, and the night it goes dark is
    # the night nobody finds out. The consequence is that a suite failure now
    # turns the docs-lint job red instead of a job of its own, which is the
    # intended outcome, not a side effect. `continue-on-error: true` would make
    # every test in the repository advisory, so it claims the exemption.
    ("drift-canary.yml", "docs-lint",
     "Run full test suite (stdlib suites, offline)"): (
        "load-bearing by design: this step runs the full stdlib suite, so the "
        "job going red IS the signal the suite failed. "
        "`continue-on-error: true` would turn every test in the repository "
        "advisory -- the opposite of what this job exists to do."
    ),
}

#: A step this many commands long (excluding blanks and comments) can do
#: enough to fail on its own, so `if: always()` alone is not enough to make
#: it non-fatal.
ALWAYS_STEP_COMMAND_THRESHOLD = 1


# ==========================================================================
# Check 8 constant: cron -> Nepal Time
# ==========================================================================

#: Nepal is UTC+05:45 with no DST, so every conversion in this repository
#: is a fixed +345 minutes.  The table below is the committed expectation
#: for every scheduled workflow, written the way a human reads it.
#:
#: Adding a schedule means adding a row here on purpose.  That is the whole
#: point of the table: it turns "what time does this fire?" from a comment
#: somebody has to keep in sync into a fact CI can contradict.  Note what
#: it does NOT do -- see the doc/code contradiction reported by
#: `test_cron_npt_table_covers_every_schedule_and_reports_worker_interval`.
NPT_OFFSET_MINUTES = 345

#: (workflow file, cron expression) -> [(NPT hour, NPT minute), ...] with
#: hour == "*" meaning every hour of the day.
CRON_NPT_TABLE = {
    # 17 4,10 * * * -> 10:02 and 16:02 NPT (04:17 / 10:17 UTC, +5h45), the two
    # firings the dead-man's backstop was cut to on 2026-10-19 for Actions
    # minutes. It was hourly: `17 * * * *` -> :02 past every hour (17 + 345 =
    # 362 min = 6h02m). Both firings land inside TU office hours, which is when
    # a result notice can be published. THE COST IS LATENCY, RECORDED ON
    # PURPOSE: a silently dead monitor is now noticed within ~12 hours, not
    # within one, and the Worker remains the sensor at every 2 minutes.
    ("academic-result.yml", "17 4,10 * * *"): [(10, 2), (16, 2)],
    # 20 1 * * * -> 07:05 NPT (01:20 UTC, +5h45), the ONE backstop firing the
    # safety schedule was cut to on 2026-10-19 for Actions minutes. It was
    # `20 */6 * * *` -> 06:05, 12:05, 18:05 and 00:05 NPT (UTC 0/6/12/18 at
    # :20, +5h45; the 18:20 UTC slot rolls over past midnight to 00:05).
    # 07:05 is before the 07:50 morning slot, so an overnight Worker outage is
    # still seen before the digest goes out. THE COST IS LATENCY, RECORDED ON
    # PURPOSE: a dead Worker is now noticed within ~24 hours, not within 6. The
    # Worker is still the sensor and sweeps every 3 minutes; this row is the
    # net, not the sensor.
    # The history that put a row here survives the cut: this workflow carried
    # NO row at all until 2026-10-08, which is exactly what "no schedule"
    # looks like in this table -- a workflow whose only trigger was
    # `repository_dispatch`, which had never fired, so the whole detection
    # chain was unreachable for six weeks.
    ("breaking-monitor.yml", "20 1 * * *"): [(7, 5)],
    # 23 6 * * *  ->  12:08 NPT (06:23 UTC, +5h45), the Actions budget sentinel.
    # One firing a day, off the hour because GitHub's scheduled queue congests
    # at :00. A workflow that spends minutes to guard minutes is still a
    # workflow that spends minutes, so it carries a row and a budget entry.
    ("budget-sentinel.yml", "23 6 * * *"): [(12, 8)],
    # 5 2 * * *   ->  07:50 NPT, the morning digest slot
    ("daily-news.yml", "5 2 * * *"): [(7, 50)],
    # 5 14 * * *  ->  19:50 NPT, the evening digest slot
    ("daily-news.yml", "5 14 * * *"): [(19, 50)],
    # NO digest-prebuild.yml ROWS, AND THE ABSENCE IS THE POINT. This workflow
    # used to declare four GitHub crons (5 1 / 20 1 / 5 13 / 20 13, i.e. 06:50 /
    # 07:05 / 18:50 / 19:05 NPT) and that is exactly why it has none now.
    # GitHub-hosted SCHEDULED runs measured 4.5-7h late, so the Cloudflare
    # Worker dispatches this workflow on its own punctual clock instead -- and
    # leaving the four crons in place would have ADDED a third firing per slot
    # rather than replacing the broken one (3 builds and 3 Pages deploys per
    # slot, and on a late day a 12h-late `5 13` firing overwriting an
    # already-delivered `digest:artifact:<D>:evening` under a date that still
    # matches, which neither stale_date nor ARTIFACT_MAX_BUILD_AGE_S catches).
    # Its prebuild instants live in PREBUILD_CRON_NPT_TABLE below, keyed on
    # the Worker's committed dispatcher crons rather than on a workflow
    # `schedule:`, because they are not Cloudflare-invocable times. A row here
    # can only come back by re-adding a `schedule:` block to that workflow,
    # which `test_digest_prebuild.py` fails on first.
    # 0 4,16 * * *  ->  09:45 and 21:45 NPT, the watchdog pair
    ("digest-watchdog.yml", "0 4,16 * * *"): [(9, 45), (21, 45)],
    # THE 17,32,47 ROW IS GONE (2026-10-20). That cron -- the empty-digest retry
    # ladder -- was removed from digest-watchdog.yml's `schedule:` and moved onto
    # the Worker's own minute grid (SCHED_LADDER_PHASE in
    # deploy/cloudflare-worker.js), so there is no workflow schedule left for a
    # table row to convert. Removing the row is the point: a row for a schedule
    # that no longer exists is exactly the drift this table exists to catch.
    # 30 0 * * *  ->  06:15 NPT, ~95 min before the morning slot
    ("drift-canary.yml", "30 0 * * *"): [(6, 15)],
}


def expand_numeric_field(field, maximum):
    """Expand one cron field into a sorted list of ints in 0..maximum.

    Handles `*`, `a`, `a-b` and `a/n` (and any comma-separated mix), which is
    everything the schedules in this repository use. An unsupported shape raises
    rather than guessing, because a silently-wrong schedule conversion is worse
    than no conversion.
    """
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            step = int(step_s)
        if part == "*":
            start, end = 0, maximum
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return sorted(values)


def expand_hour_field(field):
    """Expand a cron hour field to a sorted list of hours 0..23."""
    hours = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            step = int(step_s)
        if part == "*":
            start, end = 0, 23
        elif "-" in part:
            start_s, _, end_s = part.partition("-")
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        hours.update(range(start, end + 1, step))
    return sorted(hours)


def cron_to_npt(expr):
    """Convert a 5-field UTC cron to a sorted list of (NPT hour, NPT minute)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("expected a 5-field cron, got %d: %r" % (len(fields), expr))
    minute_field, hour_field = fields[0], fields[1]
    # A STRICT SUPERSET of the previous hand-rolled minute expansion, and that
    # matters: no GitHub Actions schedule in this repository puts `*/n` in the
    # MINUTE field, so the old expansion never met one -- and the Cloudflare
    # Worker's `*/2` and `*/3` sweeps are exactly that shape. The check that
    # requires a Nepal Time row for every worker cron could not even be written
    # until this handled them, which is how a gap this old survives: nothing
    # exercised the case. `*`, `a`, `a-b` and comma lists expand identically to
    # before, so every existing expectation is unchanged.
    minutes = expand_numeric_field(minute_field, 59)
    out = set()
    for minute in minutes:
        total = minute + NPT_OFFSET_MINUTES
        carry, npt_minute = divmod(total, 60)
        for hour in expand_hour_field(hour_field):
            out.add(((hour + carry) % 24, npt_minute))
    return sorted(out)


# ==========================================================================
# Check 16 constant: the WORKER schedule, and why it is not in CRON_NPT_TABLE
# ==========================================================================
#
# CRON_NPT_TABLE above covers GitHub Actions schedules only, and its own test
# enforces that every row corresponds to a real `on.schedule[].cron` in one of
# the five workflows. The Cloudflare Worker's crons live in
# deploy/wrangler.toml and are applied by `wrangler deploy`; putting their rows
# in CRON_NPT_TABLE would make that test fail ("no such schedule exists") or
# require editing it. Editing it is not an option -- a check that has to be
# loosened to accommodate a new source of truth is a check that has stopped
# checking -- so the Worker schedule gets its OWN table here, fed by the SAME
# `cron_to_npt` converter, and Check 16 requires a row for every worker cron.
#
# The shape differs from CRON_NPT_TABLE for one honest reason: `*/2` fires 30
# times an hour, so a (hour, minute) pair list would be 720 entries of noise.
# Instead each row names the HOUR set and the MINUTE set, `None` meaning "every
# hour" / "every minute", and the check compares the cartesian product against
# what cron_to_npt actually computes.
#
# The NPT facts below are not obvious and are exactly the kind of thing a human
# gets wrong. Nepal is +05:45, i.e. 345 minutes, and 345 mod 60 = 45:
#   * `*/2` -> UTC minutes 0,2,...,58. Adding 45 keeps them all EVEN+ODD=ODD,
#     so in NPT the academic sweep fires on ODD minutes, not even ones.
#   * `*/3` -> 45 is a multiple of 3, so the residue class survives: NPT fires
#     on every minute divisible by 3.
#   * `17 * * * *` -> :02 past every hour. That is the shape of the Worker's
#     hourly health check. academic-result.yml held the same row until
#     2026-10-19, when its backstop was cut to two firings a day.

#: Keyed (source file, cron) exactly like CRON_NPT_TABLE, so "which schedule is
#: this expectation about" is answerable from the row alone.
#:
#: ONE ROW. The account hit Cloudflare's Workers Free limit of 5 cron triggers
#: per account (code 10072) and the jobs were merged into a single dispatcher,
#: so the Worker is deployed with exactly one cron: every minute. The logical
#: schedule that one trigger reproduces is NOT here -- it is not a Cloudflare
#: trigger, so putting its rows in this table would assert a NPT conversion for
#: a schedule Cloudflare does not hold. Those instants are checked against the
#: pre-merge crons by the cadence-parity test instead, which is a stronger check
#: than a conversion table: it runs both schedules over a simulated day.
WORKER_CRON_NPT_TABLE = {
    ("deploy/wrangler.toml", "* * * * *"): {
        "hours": None, "minutes": None},
}

#: THE LOGICAL SCHEDULE THE ONE TRIGGER REPRODUCES, keyed by the same shape.
#: `dispatcher_crons` in deploy/schedule.json is the human-reviewable record of
#: this; these constants are what the cadence-parity test compares it against,
#: and they are the PRE-MERGE cron expressions verbatim. Changing one of these is
#: how a cadence regression gets in, and the parity test is what catches it.
#:
#: NOTE THE SWEEPS ARE NOT WHAT THEIR EXPRESSION SUGGESTS: `*/3` is the BREAKING
#: sweep and `*/2` is the ACADEMIC one. Read the code, not the names.
PRE_MERGE_WORKER_CRONS = ("*/3 * * * *", "*/2 * * * *", "35 1 * * *",
                          "35 13 * * *", "17 * * * *")

#: THE PREBUILD TRIGGER, and the two facts about it that are easy to get wrong.
#:
#: 1. THE NEPAL OFFSET. `55 0 * * *` is 00:55 UTC and Nepal is +05:45 with no
#:    DST, so it is 06:40 NPT — 00:55 + 5h45 = 06:40 exactly. `55 12 * * *`
#:    is 12:55 UTC and that is 18:40 NPT. Both are 40 minutes before the
#:    07:20 / 19:20 NPT digest slots. These are checked by `cron_to_npt` below
#:    rather than asserted, because the whole class of bug this session exists to
#:    kill is a silently-wrong offset that moves the delivery time.
#: 2. THESE ARE NOT CLOUDARE TRIGGERS. They ride the one deployed `* * * * *`,
#:    so they belong here and NOT in WORKER_CRON_NPT_TABLE, which is keyed by
#:    the crons in deploy/wrangler.toml. Putting them there would assert that
#:    Cloudflare holds three triggers, and it holds one.
PREBUILD_CRON_NPT_TABLE = {
    "55 0 * * *": [(6, 40)],      # 06:40 NPT, 40 min before the 07:20 slot
    "55 12 * * *": [(18, 40)],    # 18:40 NPT, 40 min before the 19:20 slot
}

#: The minutes of headroom the prebuild gets, as a COMMITTED design choice
#: rather than a derived value: the Worker computes `slot - lead`, so this is
#: both the number in the Worker and the number CI holds it to. Measured
#: prebuild duration is 5-9 minutes on the four most recent runs and 22 on one,
#: so 40 is roughly twice the worst observed build. The worst case is designed
#: to be safe rather than prevented: a prebuild that overruns seals nothing and
#: the digest cron falls back to a full build (see
#: `prebuild_fail_safe_problems`).
PREBUILD_LEAD_MINUTES = 40

#: The re-fire attempts per digest slot, and the gap between them, as the merged
#: dispatcher states them. BOUNDED on purpose: the window closes at slot+20,
#: ten minutes before the 02:05 UTC GitHub fallback owns the slot.
REFIRE_GAP_MINUTES = 5
REFIRE_ATTEMPTS = 4

#: The digest slots the merged dispatcher still fires, keyed by the same shape.
#: Identified BY NAME rather than by expression so a third slot is caught rather
#: than un-countable, exactly as the pre-merge check did.
DISPATCH_DIGEST_SLOTS = ("morning", "evening")

#: WHEN THE GITHUB FALLBACK OWNS EACH SLOT, in UTC. daily-news.yml fires at
#: 02:05 and 14:05 UTC (07:50 / 19:50 NPT) and no earlier than that can be
#: correct: the Worker's re-fire window must CLOSE before this, or two arms
#: race the same slot on every bad day and a race is a second push by
#: construction. This is what makes the retry bounded rather than merely
#: repetitive.
GITHUB_FALLBACK_UTC = {"morning": 2 * 60 + 5, "evening": 14 * 60 + 5}

#: The punctuality anchor stays on the PRIMARY slot instant. A digest the
#: re-fire delivered is genuinely late and has to be reported late; anchoring on
#: a re-fire would round a late start down to "on time".
#:
#: The pre-merge regexes (PRIMARY_DIGEST_CRON_RE / REFIRE_DIGEST_CRON_RE) are
#: GONE, and that is not a loosening. They matched the Worker's CRON_DIGEST_*
#: string constants, which after the merge are COMPUTED from the schedule data
#: rather than written as literals -- a second literal in that block would have
#: re-entered schedule.json's trigger set and put the account back over the
#: free-tier limit. The pairs are now read from the committed dispatcher_crons
#: record and checked for the property the old regexes existed to protect: every
#: slot has a re-fire window, and every re-fire sits exactly REFIRE_GAP_MINUTES
#: after its slot.

#: The recorded KV budget's digest terms, in the order the arithmetic reads.
DIGEST_KV_TERMS = ("artifact_writes", "ledger_writes", "artifact_expiry_deletes")

#: The committed file that holds the Worker schedule as one reviewable record.
SCHEDULE_JSON_REL = "deploy/schedule.json"

#: The config `wrangler deploy` actually reads. The path is relative to
#: REPO_ROOT but the TOML's own `main` is relative to the config file, which is
#: why the Worker lives beside it rather than at the repo root.
WRANGLER_TOML_REL = "deploy/wrangler.toml"

WORKER_JS_REL = "deploy/cloudflare-worker.js"

#: `const CRON_<NAME> = "<expr>";` -- declarations only, so the string
#: concatenations on the status page cannot masquerade as constants.
CRON_CONST_RE = re.compile(
    r"^\s*const\s+(CRON_[A-Z0-9_]+)\s*=\s*[\"']([^\"']+)[\"']\s*;",
    re.MULTILINE)

#: The workflows that poll the Worker's `?health=1` endpoint. All MUST read
#: the URL out of deploy/schedule.json rather than carrying a literal, so there
#: is exactly one place to correct a subdomain and Check 16 can hold them to it.
#:
#: U6 added `digest-prebuild.yml`: its seal job derives the INGEST origin from
#: the same `health_url` string rather than carrying
#: `breaking-monitor.example.workers.dev` as a literal, so a corrected
#: subdomain reaches the seal POST too. A second literal here would be the
#: fourth copy of one fact and would drift exactly like the others did.
HEALTH_URL_WORKFLOWS = ("digest-watchdog.yml", "drift-canary.yml",
                        "digest-prebuild.yml")


def cron_invocations_per_day(expr):
    """Fires per day for a DAILY 5-field UTC cron.

    Deliberately refuses a cron that restricts the date fields instead of
    guessing: `0 4 1 * *` is not 30 fires/day, and a count that is
    "approximately right" is precisely how a documented total starts lying.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            "expected a 5-field cron, got %d: %r" % (len(fields), expr))
    if tuple(fields[2:]) != ("*", "*", "*"):
        raise ValueError(
            "cron_invocations_per_day only counts DAILY crons (day-of-month, "
            "month and day-of-week all '*'); %r restricts them, so a per-day "
            "figure would be wrong rather than approximately right" % (expr,))
    minutes = len(expand_numeric_field(fields[0], 59))
    hours = len(expand_numeric_field(fields[1], 23))
    return minutes * hours


def wrangler_crons(text=None):
    """Return `[triggers] crons` from deploy/wrangler.toml.

    Parsed with tomllib when the interpreter has it (3.11+). The stdlib-only
    fallback exists because this suite must stay runnable on a bare
    interpreter, and it RAISES rather than returning an empty list: a fallback
    that silently found nothing would make Check 16 pass by checking nothing,
    which is the failure mode this whole file exists to prevent.
    """
    if text is None:
        # utf-8-sig, not utf-8: the committed file carries a UTF-8 BOM, and
        # tomllib rejects a BOM outright.
        text = (W.REPO_ROOT / WRANGLER_TOML_REL).read_text(encoding="utf-8-sig")
    try:
        import tomllib
    except ImportError:  # pragma: no cover - exercised only on <3.11
        m = re.search(r"^\s*crons\s*=\s*\[([^\]]*)\]", text, re.MULTILINE)
        if not m:
            raise AssertionError(
                "no `crons = [...]` line found in %s and tomllib is "
                "unavailable, so the Worker schedule cannot be checked at all"
                % WRANGLER_TOML_REL)
        return [c.strip().strip("\"'") for c in m.group(1).split(",")
                if c.strip()]
    return [str(c) for c in tomllib.loads(text)["triggers"]["crons"]]


def worker_cron_constants(text=None):
    """Return {NAME: expr} for every CRON_* constant declared in the Worker."""
    if text is None:
        text = (W.REPO_ROOT / WORKER_JS_REL).read_text(
            encoding="utf-8", errors="replace")
    return dict(CRON_CONST_RE.findall(text))


def load_schedule_json(text=None):
    """Parse deploy/schedule.json and shape-check the keys Check 16 relies on."""
    import json
    if text is None:
        text = (W.REPO_ROOT / SCHEDULE_JSON_REL).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AssertionError("%s is not valid JSON: %s" % (SCHEDULE_JSON_REL, exc))
    if not isinstance(data, dict):
        raise AssertionError(
            "%s must be a JSON object, got %s" % (SCHEDULE_JSON_REL, type(data).__name__))
    for key in ("crons", "expected_invocations_per_day", "health_url"):
        if key not in data:
            raise AssertionError(
                "%s has no %r key. Check 16 needs it; add it deliberately rather "
                "than loosening the check." % (SCHEDULE_JSON_REL, key))
    return data


def schedule_sync_problems(wrangler_list, schedule, worker_consts):
    """Reasons the three records of the Worker schedule do not agree.

    Empty means they agree. Deliberately source-parameterised so the mutation
    proof can run the SAME code against a deliberately divergent triple.
    """
    problems = []
    declared = [str(c) for c in wrangler_list]
    from_json = [str(c) for c in schedule.get("crons") or []]
    from_js = [worker_consts[name] for name in sorted(worker_consts)]

    for label, seq in (("deploy/wrangler.toml", declared),
                       ("deploy/schedule.json", from_json),
                       ("the CRON_* constants", from_js)):
        if not seq:
            problems.append(
                "%s yielded NO crons. An empty schedule cannot be compared to "
                "anything, so every other assertion in this check would be "
                "vacuously true -- the check must not be able to pass by finding "
                "nothing." % label)
    for label, seq in (("deploy/wrangler.toml", declared),
                       ("deploy/schedule.json", from_json),
                       ("the CRON_* constants", from_js)):
        dupes = sorted({c for c in seq if seq.count(c) > 1})
        if dupes:
            problems.append(
                "%s lists %s more than once. A duplicated cron means the "
                "invocation count is wrong and one trigger is deployed twice."
                % (label, dupes))
    if not problems:
        if len(set(declared)) != len(set(from_json)):
            problems.append(
                "the cron sets differ.\n  wrangler.toml [triggers] crons: %s\n"
                "  schedule.json crons:               %s\n"
                "  Wrangler is what actually gets deployed, so the deployed "
                "schedule is NOT the documented one."
                % (sorted(set(declared)), sorted(set(from_json))))
        if set(declared) != set(from_js):
            problems.append(
                "the deployed cron list and the Worker's CRON_* constants "
                "differ.\n  wrangler.toml [triggers] crons: %s\n"
                "  deploy/cloudflare-worker.js CRON_*: %s\n"
                "  A cron deployed but not branched on in scheduled() is a cron "
                "that fires and does nothing; a constant branched on but not "
                "deployed is a branch that can never be taken."
                % (sorted(set(declared)), sorted(from_js)))
    return problems


def invocation_count_problems(wrangler_list, schedule):
    """Reasons `expected_invocations_per_day` is not what the crons produce."""
    problems = []
    total = 0
    for expr in wrangler_list:
        try:
            total += cron_invocations_per_day(expr)
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        return problems
    claimed = schedule.get("expected_invocations_per_day")
    if claimed != total:
        problems.append(
            "%s claims expected_invocations_per_day=%r, but its own cron list "
            "produces %d:\n%s\n"
            "Either the number or the list is wrong. Both are committed, so "
            "whichever is stale is now a documented lie."
            % (SCHEDULE_JSON_REL, claimed, total,
               "\n".join("    %-14s -> %d/day"
                         % (e, cron_invocations_per_day(e))
                         for e in wrangler_list)))
    return problems


def worker_npt_problems(wrangler_list):
    """Reasons a worker cron is missing a Nepal-Time row, or contradicts it."""
    problems = []
    declared = [str(c) for c in wrangler_list]
    for expr in declared:
        row = WORKER_CRON_NPT_TABLE.get((WRANGLER_TOML_REL, expr))
        if row is None:
            problems.append(
                "worker cron %r has no row in WORKER_CRON_NPT_TABLE. Adding a "
                "schedule is a deliberate act: add the row with the Nepal Time "
                "you expect (%s) and let CI confirm the conversion."
                % (expr, WRANGLER_TOML_REL))
            continue
        try:
            actual = set(cron_to_npt(expr))
        except ValueError as exc:
            problems.append("worker cron %r is unparseable: %s" % (expr, exc))
            continue
        hours = range(24) if row["hours"] is None else row["hours"]
        minutes = range(60) if row["minutes"] is None else row["minutes"]
        expected = {(int(h), int(m)) for h in hours for m in minutes}
        if actual != expected:
            problems.append(
                "worker cron %r fires at %d distinct Nepal Time slot(s), "
                "WORKER_CRON_NPT_TABLE says %d. First divergence: %r"
                % (expr, len(actual), len(expected),
                   sorted(actual ^ expected)[:4]))
    for key in WORKER_CRON_NPT_TABLE:
        if key[1] not in declared:
            problems.append(
                "WORKER_CRON_NPT_TABLE has a row for %s / %r but no such cron "
                "is deployed; remove the stale row" % key)
    return problems


def health_url_problems(schedule, workflow_texts):
    """Reasons the two arms of the switch are not reading one health URL."""
    problems = []
    url = str(schedule.get("health_url") or "").strip()
    if not url:
        problems.append("%s has an empty health_url" % SCHEDULE_JSON_REL)
        return problems
    if "?health=1" not in url:
        problems.append(
            "%s health_url is %r, which does not ask for ?health=1. Polling "
            "the status page instead of the health endpoint yields prose, which "
            "no script can assert on." % (SCHEDULE_JSON_REL, url))
    for wf, text in sorted(workflow_texts.items()):
        if SCHEDULE_JSON_REL not in text:
            problems.append(
                "%s does not reference %s. Both arms of the dead-man's switch "
                "must read the health URL from the committed schedule, so there "
                "is exactly one place to correct a subdomain." % (wf, SCHEDULE_JSON_REL))
        if "health_url" not in text:
            problems.append(
                "%s references %s but never reads the health_url key from it"
                % (wf, SCHEDULE_JSON_REL))
    return problems


# ==========================================================================
# Check 16, part two: the delivery arm's cron shape and its recorded KV budget
# ==========================================================================
#
# BOTH OF THESE ARE ARITHMETIC CHECKS ON COMMITTED NUMBERS, and that is the
# whole reason they are here. A free-tier budget kept in a comment starts
# lying the first time a line of code is added, and the cheapest possible
# place to notice is a check that recomputes the figure from the table it is
# written next to.


def cron_minutes(expr):
    """(minute, hour) of a daily 5-field UTC cron. Reuses Check 8's parser."""
    fields = str(expr).split()
    if len(fields) != 5:
        raise ValueError("not a 5-field cron: %r" % (expr,))
    return int(fields[0]), int(fields[1])


def cron_minute_field(expr):
    """Just the minute field of a 5-field cron, whatever its hour field is.

    `cron_minutes` cannot read `17 * * * *`, because the hour field is `*` and
    int('*') raises. The dispatcher records its hourly health check in exactly
    that shape, so this exists rather than the health check being counted with
    a hardcoded 17 that would stop following the record.
    """
    fields = str(expr).split()
    if len(fields) != 5:
        raise ValueError("not a 5-field cron: %r" % (expr,))
    return int(fields[0])


def digest_slots_per_day(schedule):
    """How many digest slots the merged dispatcher fires. Derived, not assumed.

    Read from the committed `dispatcher_crons.digest_slots` record rather than
    from the Worker's CRON_* literals, because those are computed after the
    merge and a regex over literals would find none of them.
    """
    slots = (schedule.get("dispatcher_crons") or {}).get("digest_slots") or {}
    if not isinstance(slots, dict):
        return 0
    return len(slots)


#: The `prebuild_lead_minutes` constant as the WORKER declares it. Read from the
#: source, so the number CI holds the schedule to is the number the code
#: subtracts -- which is the whole reason the prebuild is derived rather than
#: restated. A lead that exists only in schedule.json is a comment.
WORKER_PREBUILD_LEAD_RE = re.compile(
    r"const\s+PREBUILD_LEAD_MINUTES\s*=\s*(\d+)\s*;")


def worker_prebuild_lead_minutes(text=None):
    """The lead the Worker actually subtracts, or None if it declares none."""
    if text is None:
        text = (W.REPO_ROOT / WORKER_JS_REL).read_text(
            encoding="utf-8", errors="replace")
    match = WORKER_PREBUILD_LEAD_RE.search(text)
    return int(match.group(1)) if match else None


def prebuild_schedule(schedule):
    """The committed `digest_prebuild` record, or {} when it is absent."""
    dispatcher = schedule.get("dispatcher_crons")
    if not isinstance(dispatcher, dict):
        return {}
    block = dispatcher.get("digest_prebuild")
    return block if isinstance(block, dict) else {}


def _prebuild_minutes_from(schedule, problems=None):
    """{(hour, minute)} the prebuild fires on, read from the committed block.

    Shared by every count that includes the prebuild, so there is ONE place that
    turns the recorded crons into grid positions. An unparseable cron appends to
    `problems` when one is passed rather than being skipped silently -- a count
    that quietly drops a term is the arithmetic-lying failure this file exists to
    prevent.
    """
    out = set()
    for slot in sorted(DISPATCH_DIGEST_SLOTS):
        expr = prebuild_schedule(schedule).get(slot)
        if not isinstance(expr, str) or not expr.strip():
            continue
        try:
            p_min, p_hour = cron_minutes(expr)
        except (ValueError, TypeError) as exc:
            if problems is not None:
                problems.append(
                    "unparseable digest_prebuild %s cron %r: %s"
                    % (slot, expr, exc))
            continue
        out.add((p_hour, p_min))
    return out


def prebuild_fail_safe_problems(schedule):
    """Reasons the prebuild could block or double-fire its digest slot.

    THE ACCEPTANCE CRITERION, AS ARITHMETIC AND AS ASSERTED FACTS. Four things
    must hold, and each is a failure the owner would experience as missing news
    or as two pushes:

      1. both prebuild instants EXIST and convert to the Nepal Time the table
         expects -- 06:40 and 18:40 NPT. A wrong offset silently moves the
         delivery time, which is the exact class of bug this session exists to
         eliminate, so this is a conversion and never a restatement;
      2. each prebuild is EXACTLY PREBUILD_LEAD_MINUTES before its slot, and the
         lead is large enough to contain the worst measured prebuild;
      3. the prebuild does NOT wait, write KV, or record a delivery marker -- the
         properties that make a failed prebuild degrade to the fallback ladder
         instead of suppressing the slot;
      4. it does NOT push, so a prebuild running alongside the slot cannot
         produce a second notification.
    """
    problems = []
    block = prebuild_schedule(schedule)
    if not block:
        return ["%s declares no dispatcher_crons.digest_prebuild block, so the "
                "prebuild trigger cannot be checked at all. A punctuality fix "
                "whose schedule is unrecorded is the drift Check 16 exists to "
                "kill." % SCHEDULE_JSON_REL]
    slots = (schedule.get("dispatcher_crons") or {}).get("digest_slots") or {}
    lead = block.get("prebuild_lead_minutes")

    if lead != PREBUILD_LEAD_MINUTES:
        problems.append(
            "digest_prebuild.prebuild_lead_minutes is %r, not %d. The Worker "
            "computes `slot - lead`, so this number IS the headroom: a lead that "
            "is not this one makes the committed crons below not describe what "
            "the Worker does." % (lead, PREBUILD_LEAD_MINUTES))

    for slot in sorted(DISPATCH_DIGEST_SLOTS):
        expr = block.get(slot)
        if not isinstance(expr, str) or not expr.strip():
            problems.append("digest_prebuild declares no %r cron" % slot)
            continue
        if slot not in slots:
            problems.append(
                "digest_prebuild fires for the %r slot, which the dispatcher "
                "does not declare in digest_slots (%s), so there is no slot for "
                "it to prepare" % (slot, sorted(slots)))
            continue
        # (1) THE NEPAL-TIME CONVERSION, computed rather than believed.
        expected_npt = PREBUILD_CRON_NPT_TABLE.get(expr)
        if expected_npt is None:
            problems.append(
                "digest_prebuild %s is %r, which has no row in "
                "PREBUILD_CRON_NPT_TABLE. Adding a schedule is a deliberate act: "
                "add the row with the Nepal Time you expect and let CI confirm "
                "the conversion." % (slot, expr))
            continue
        try:
            actual_npt = set(cron_to_npt(expr))
        except ValueError as exc:
            problems.append("digest_prebuild %s is unparseable: %s" % (slot, exc))
            continue
        expected_set = set((int(h), int(m)) for h, m in expected_npt)
        if actual_npt != expected_set:
            problems.append(
                "digest_prebuild %s fires at %s NPT, PREBUILD_CRON_NPT_TABLE "
                "says %s. The offset is Nepal's +05:45 = 345 minutes; getting it "
                "wrong moves the delivery time and nothing else would say so."
                % (slot, sorted(actual_npt), sorted(expected_set)))
            continue
        # (2) THE LEAD, recomputed from the slot it exists to serve.
        try:
            p_min, p_hour = cron_minutes(expr)
            s_min, s_hour = cron_minutes(slots[slot])
        except (ValueError, TypeError) as exc:
            problems.append("unparseable prebuild/slot pair for %r: %s"
                            % (slot, exc))
            continue
        pre_total = p_hour * 60 + p_min
        slot_total = s_hour * 60 + s_min
        actual_lead = slot_total - pre_total
        if actual_lead != PREBUILD_LEAD_MINUTES:
            problems.append(
                "digest_prebuild %s is %r, which is %+d minutes from its slot "
                "%r. It is computed as `slot - %d`, so the two have drifted "
                "apart and one of them is lying."
                % (slot, expr, actual_lead, slots[slot], PREBUILD_LEAD_MINUTES))
        if actual_lead <= 0:
            problems.append(
                "digest_prebuild %s (%r) does not PRECEDE its slot %r. A prebuild "
                "at or after the slot cannot be a prebuild."
                % (slot, expr, slots[slot]))
        # The lead must also be positive on the same UTC day, or the prebuild
        # would belong to a different date than the slot it is preparing --
        # which is the mislabelled-archive defect digest-prebuild.yml refuses.
        if pre_total >= slot_total:
            problems.append(
                "digest_prebuild %s (%r) is not on the same UTC day as its slot "
                "%r, so it would seal an artifact dated for a different firing."
                % (slot, expr, slots[slot]))
        # (3)+(4) THE FAIL-SAFE PROPERTIES, as committed facts.
        if block.get("waits_for_completion") is not False:
            problems.append(
                "digest_prebuild.waits_for_completion is %r, not false. The "
                "dispatcher dispatches and RETURNS; a trigger that blocks the "
                "cron invocation on a 5-22 minute workflow can outlive its own "
                "invocation and is a second punctuality problem."
                % (block.get("waits_for_completion"),))
        if block.get("kv_writes_per_day") != 0:
            problems.append(
                "digest_prebuild.kv_writes_per_day is %r, not 0. The prebuild "
                "arm records nothing: a key written here is state a failed "
                "prebuild can leave behind, and the delivery cron's own guards "
                "are the only things allowed to gate a push."
                % (block.get("kv_writes_per_day"),))
        if block.get("delivery_marker_written") is not False:
            problems.append(
                "digest_prebuild.delivery_marker_written is %r, not false. A "
                "marker written by the prebuild arm would let a FAILED prebuild "
                "suppress the slot's own delivery -- the exact failure that "
                "leaves the owner with no news at all."
                % (block.get("delivery_marker_written"),))
        if not str(block.get("on_failure") or "").strip():
            problems.append(
                "digest_prebuild.on_failure is empty. The degradation path is "
                "the whole safety argument for a 40-minute lead, so it is "
                "recorded as a fact or not at all.")

    for expr in PREBUILD_CRON_NPT_TABLE:
        if expr not in (block.get("morning"), block.get("evening")):
            problems.append(
                "PREBUILD_CRON_NPT_TABLE has a row for %r but the dispatcher "
                "does not fire it; remove the stale row" % expr)
    if str(block.get("workflow") or "") != "digest-prebuild.yml":
        problems.append(
            "digest_prebuild.workflow is %r, not 'digest-prebuild.yml'. This "
            "arm exists to replace a delayed SCHEDULED trigger on that "
            "workflow; pointing it elsewhere is not the same change."
            % (block.get("workflow"),))
    return problems


def ladder_minutes(schedule):
    """The ladder's (hour, minute) pairs, from the committed block.

    Empty when the block is absent or malformed; every caller guards on that.
    """
    dispatcher = schedule.get("dispatcher_crons") or {}
    block = dispatcher.get("digest_ladder") or {}
    fires = block.get("fire_minutes")
    if not isinstance(fires, list):
        return []
    out = []
    for minute in fires:
        if not isinstance(minute, int) or isinstance(minute, bool):
            return []
        for hour in range(24):
            out.append((hour, minute))
    return out


def ladder_fire_count(schedule):
    """How many ladder job fires a day the committed block produces."""
    return len(ladder_minutes(schedule))


def ladder_internal_count_problems(schedule):
    """Reasons the ladder's contribution to the daily counts is not what the
    schedule gives.

    The empty-digest retry ladder was digest-watchdog.yml's second GitHub cron
    (`17,32,47 * * * *`, 72 billed job-minutes a day). On 2026-10-20 it became a
    branch of the Worker's own planTickFor(), so it is 72 INTERNAL JOB FIRES on
    the existing every-minute trigger -- Cloudflare invocations do not move.

    Its three minutes are :17 (already the health check), :32 (already the
    academic sweep) and :47 (a no-op tick until now), so the TICK effect is 24
    minutes switching from no-op to work, NOT 72. That distinction is why this
    is a function of its own rather than a term added to the sweeps.
    """
    problems = []
    dispatcher = schedule.get("dispatcher_crons")
    if not isinstance(dispatcher, dict):
        return ["%s declares no dispatcher_crons" % SCHEDULE_JSON_REL]
    block = dispatcher.get("digest_ladder")
    if not isinstance(block, dict):
        return ["%s declares no digest_ladder block" % SCHEDULE_JSON_REL]
    every = block.get("every_minutes")
    phase = block.get("phase_minute")
    fires = block.get("fire_minutes")
    if not isinstance(every, int) or isinstance(every, bool) or every <= 0:
        problems.append("digest_ladder.every_minutes is %r, not a positive integer"
                        % (every,))
        every = None
    if not isinstance(phase, int) or isinstance(phase, bool):
        problems.append("digest_ladder.phase_minute is %r, not an integer"
                        % (phase,))
        phase = None
    if not isinstance(fires, list) or not fires:
        problems.append("digest_ladder.fire_minutes is %r, not a non-empty list"
                        % (fires,))
        return problems
    if every and phase is not None:
        derived = []
        m = phase
        while m < 60:
            derived.append(m)
            m += every
        if derived != fires:
            problems.append(
                "digest_ladder.fire_minutes is %r, but phase_minute=%d stepped "
                "by every_minutes=%d over one hour produces %r. The Worker "
                "computes its list the same way, so a hand-written copy here is "
                "a second opinion that can drift."
                % (fires, phase, every, derived))
    if block.get("workflow") != "digest-watchdog.yml":
        problems.append(
            "digest_ladder.workflow is %r, not 'digest-watchdog.yml'. The ladder "
            "is that workflow's own job; pointing it elsewhere moves work into a "
            "workflow that has none of its ledger or its alert."
            % (block.get("workflow"),))
    if block.get("input") != "ladder_only":
        problems.append(
            "digest_ladder.input is %r, not 'ladder_only'. Without that input a "
            "Worker ladder ask runs digest-watchdog.yml's `watchdog` job too, "
            "which would judge a slot that is not due." % (block.get("input"),))
    return problems


def worker_ladder_constants_problems(schedule, worker_text):
    """The Worker's ladder constants and schedule.json must be the same numbers.

    The Worker decides WHEN a ladder tick happens (SCHED_LADDER_EVERY_MIN and
    SCHED_LADDER_PHASE in deploy/cloudflare-worker.js); schedule.json records
    what that produces. Two copies of one number drift, and the failure is
    silent: the committed file keeps claiming 72 fires a day while the deployed
    Worker runs on a different phase. This makes the agreement a test.
    """
    problems = []
    block = ((schedule.get("dispatcher_crons") or {}).get("digest_ladder") or {})
    src = re.sub(r"\s+", " ", worker_text or "")
    for key, const in (("every_minutes", "SCHED_LADDER_EVERY_MIN"),
                       ("phase_minute", "SCHED_LADDER_PHASE")):
        match = re.search(r"const " + const + r" = (-?\d+);", src)
        if not match:
            problems.append(
                "deploy/cloudflare-worker.js declares no `const %s = <int>;`, so "
                "the ladder's %s cannot be checked against this file"
                % (const, key))
            continue
        value = int(match.group(1))
        if value != block.get(key):
            problems.append(
                "digest_ladder.%s is %r but the Worker's %s is %d. The Worker "
                "decides when a ladder tick happens; this file records what that "
                "produces. They must be the same number."
                % (key, block.get(key), const, value))
    return problems


# The Worker's runtime-quota per-target estimate must equal jobs_per_run x
# p95_minutes of the workflow that dispatch maps to. Two copies of one number
# drift, and the failure is SILENT: the Worker refuses on a stale ceiling while
# config/actions-budget.json claims another.
WORKER_BUDGET_JOB_WORKFLOW = {
    "breaking": "breaking-monitor.yml",
    "academic": "academic-result.yml",
    "prebuild": "digest-prebuild.yml",
    "digest": "daily-news.yml",
    "watchdog": "digest-watchdog.yml",
}


def worker_budget_constants_problems(budget, worker_text):
    """The Worker's runtime-quota constants and config/actions-budget.json must be
    the same numbers.

    Fails in BOTH directions: it reads the Worker's constants AND the budget file,
    so a one-sided change to the ceiling, the reserve, the design target, or ANY
    per-target estimate is a disagreement. This is the mandatory agreement test
    that stops the Worker's parallel table from becoming a second opinion.
    """
    problems = []
    src = re.sub(r"\s+", " ", worker_text or "")
    for key, const in (("included_minutes", "BUDGET_ALLOWANCE_MINUTES"),
                       ("budget_reserve_minutes", "BUDGET_RESERVE_MINUTES"),
                       ("hard_ceiling_minutes", "BUDGET_HARD_CEILING_MINUTES"),
                       ("design_target_minutes", "BUDGET_DESIGN_TARGET_MINUTES")):
        match = re.search(r"const " + const + r" = (\d+);", src)
        if not match:
            problems.append(
                "deploy/cloudflare-worker.js declares no `const %s = <int>;`, so "
                "the runtime quota's %s cannot be checked against this file"
                % (const, key))
            continue
        if int(match.group(1)) != int(budget.get(key)):
            problems.append(
                "config/actions-budget.json %s is %r but the Worker's %s is %s. "
                "The Worker ENFORCES this number at runtime; the file records it. "
                "Change both or neither."
                % (key, budget.get(key), const, match.group(1)))
    # The per-target table.
    table = re.search(r"const BUDGET_JOB_MINUTES = \{(.*?)\};", src)
    if not table:
        problems.append(
            "deploy/cloudflare-worker.js declares no `const BUDGET_JOB_MINUTES = "
            "{...};`, so the per-target estimates cannot be checked")
        return problems
    workflows = budget.get("workflows") or {}
    for job, wfname in WORKER_BUDGET_JOB_WORKFLOW.items():
        entry = workflows.get(wfname)
        if not isinstance(entry, dict):
            problems.append(
                "config/actions-budget.json has no %r entry, so BUDGET_JOB_MINUTES."
                "%s cannot be checked" % (wfname, job))
            continue
        expected = round(float(entry.get("jobs_per_run")) *
                         float(entry.get("p95_minutes")), 1)
        got = re.search(r"\b" + re.escape(job) + r": ([\d.]+)", table.group(1))
        if not got:
            problems.append(
                "BUDGET_JOB_MINUTES has no `%s` entry in the Worker's table; the "
                "%s dispatch would then be charged 0 minutes" % (job, job))
            continue
        if abs(float(got.group(1)) - expected) > 1e-9:
            problems.append(
                "BUDGET_JOB_MINUTES.%s is %s in the Worker but jobs_per_run %s x "
                "p95_minutes %s = %s in config/actions-budget.json (%s). The "
                "runtime charge and the static projection must be the same number."
                % (job, got.group(1), entry.get("jobs_per_run"),
                   entry.get("p95_minutes"), expected, wfname))
    # THE ENFORCEMENT SWITCH (2026-10-25). A boolean and a reason, because the
    # Worker cannot read config/actions-budget.json at runtime: without this the
    # file would say one thing about whether a dispatch can be refused while the
    # deployed Worker did another.
    enforcement = budget.get("enforcement")
    if not isinstance(enforcement, dict):
        problems.append(
            "config/actions-budget.json declares no `enforcement` object, so "
            "whether the Worker may REFUSE a dispatch at the hard ceiling is not "
            "written down anywhere a reader of this file can see")
    else:
        flag = re.search(r"const BUDGET_ENFORCEMENT_ENABLED = (true|false);", src)
        if not flag:
            problems.append(
                "deploy/cloudflare-worker.js declares no `const "
                "BUDGET_ENFORCEMENT_ENABLED = <true|false>;`, so "
                "`enforcement.enabled` cannot be checked against it")
        elif (flag.group(1) == "true") != bool(enforcement.get("enabled")):
            problems.append(
                "config/actions-budget.json enforcement.enabled is %r but the "
                "Worker's BUDGET_ENFORCEMENT_ENABLED is %s. The file would then "
                "describe the opposite of what the deployed Worker does. Change "
                "both or neither."
                % (enforcement.get("enabled"), flag.group(1)))
        reason = re.search(r'const BUDGET_ENFORCEMENT_REASON = "([^"]*)";', src)
        if not reason:
            problems.append(
                "deploy/cloudflare-worker.js declares no `const "
                "BUDGET_ENFORCEMENT_REASON = \"...\";`, so a disabled switch has "
                "no stated reason at runtime")
        elif reason.group(1) != enforcement.get("reason"):
            problems.append(
                "config/actions-budget.json enforcement.reason is %r but the "
                "Worker's BUDGET_ENFORCEMENT_REASON is %r. The reason is reported "
                "by ?health=1, so the two must be the same words."
                % (enforcement.get("reason"), reason.group(1)))
    return problems


def additions_internal_count_problems(schedule):
    """Reasons the recorded daily counts do not include the two ADDITIONS that
    ride the existing every-minute trigger: the prebuild and the retry ladder.

    THE PREBUILD (U9). Its two minutes (00:55, 12:55) are divisible by NEITHER
    the 2-minute NOR the 3-minute sweep and are not :17, so each was a no-op
    tick and is a work tick now: work_ticks 988 -> 990, no_op 452 -> 450.

    THE LADDER (U10, 2026-10-20). It moved off digest-watchdog.yml's 72-a-day
    GitHub cron onto the same grid. Its minutes are :17 (the health check), :32
    (the academic sweep) and :47 (a no-op until now), so it adds 72 JOB FIRES
    but only 24 WORK TICKS: work 990 -> 1014, no_op 450 -> 426.

    Invocations stay 1440 throughout. Deriving both here is what stops the
    numbers in schedule.json from becoming the prose this file replaced.
    """
    problems = []
    dispatcher = schedule.get("dispatcher_crons")
    if not isinstance(dispatcher, dict):
        return ["%s declares no dispatcher_crons" % SCHEDULE_JSON_REL]
    block = prebuild_schedule(schedule)
    if not block:
        return ["%s declares no digest_prebuild block" % SCHEDULE_JSON_REL]
    if "digest_ladder" not in (dispatcher or {}):
        return ["%s declares no digest_ladder block" % SCHEDULE_JSON_REL]

    pre = _prebuild_minutes_from(schedule, problems)
    if len(pre) != len(DISPATCH_DIGEST_SLOTS):
        problems.append("the prebuild fires on %d distinct instants, not %d"
                        % (len(pre), len(DISPATCH_DIGEST_SLOTS)))
        return problems

    jobs = int(schedule.get("expected_internal_job_fires_per_day") or 0)
    work_claim = int(schedule.get("expected_internal_work_ticks_per_day") or 0)
    noop_claim = int(schedule.get("no_op_ticks_per_day") or 0)
    invocations = int(schedule.get("expected_invocations_per_day") or 0)

    base_jobs, base_work, base_noop = _dispatcher_tick_arithmetic(schedule)
    if base_jobs is None:
        return problems + ["the dispatcher's own tick arithmetic is unparseable"]
    ladder = ladder_minutes(schedule)
    added_fires = len(pre) + len(ladder)
    # WITHOUT EITHER ADDITION: what the schedule committed before the prebuild
    # and the ladder existed. Both are job fires on the existing grid.
    if jobs != base_jobs + added_fires:
        problems.append(
            "expected_internal_job_fires_per_day is %d, but the schedule "
            "produces %d without the additions plus %d prebuild fires + %d "
            "ladder fires = %d. These are job fires on an existing grid, not "
            "rounding errors; they are the count."
            % (jobs, base_jobs, len(pre), len(ladder),
               base_jobs + added_fires))

    work, noop = _tick_grid_with(schedule, set(pre) | set(ladder))
    if work_claim != work or noop_claim != noop:
        problems.append(
            "the recorded tick counts are work=%d / no_op=%d, but the minute "
            "grid with the prebuild produces work=%d / no_op=%d."
            % (work_claim, noop_claim, work, noop))
    if work + noop != invocations:
        problems.append(
            "work (%d) + no_op (%d) = %d, not the %d invocations Cloudflare "
            "makes. The prebuild must ride the EXISTING trigger, so the "
            "invocation count must not move -- a change here means someone "
            "deployed a second cron."
            % (work, noop, work + noop, invocations))
    return problems


def _dispatcher_tick_arithmetic(schedule):
    """(job_fires, work_ticks, no_op_ticks) WITHOUT the prebuild.

    The pre-merge arithmetic, re-derived from the committed dispatcher so the
    comparison in `additions_internal_count_problems` is against a computed
    baseline rather than a remembered number.
    """
    dispatcher = schedule.get("dispatcher_crons") or {}
    slots = dispatcher.get("digest_slots") or {}
    attempts = dispatcher.get("digest_refire_attempts")
    every_breaking = dispatcher.get("breaking_sweep_every_minutes")
    every_academic = dispatcher.get("academic_sweep_every_minutes")
    if not all(isinstance(v, int) for v in
               (attempts, every_breaking, every_academic)):
        return None, None, None
    jobs = (60 // every_breaking) * 24 + (60 // every_academic) * 24 \
        + 24 + len(slots) * (1 + attempts)
    work, noop = _tick_grid_with(schedule, set())
    return jobs, work, noop


def _tick_grid_with(schedule, extra_minutes):
    """(work_ticks, no_op_ticks) over all 1440 daily grid positions.

    `extra_minutes` is a set of (hour, minute) pairs that are ALSO due -- the
    prebuild's, when they are in play. Every tick is exactly once, however many
    jobs run on it: a minute divisible by six runs both sweeps and is still ONE
    Cloudflare invocation.
    """
    dispatcher = schedule.get("dispatcher_crons") or {}
    slots = dispatcher.get("digest_slots") or {}
    attempts = dispatcher.get("digest_refire_attempts")
    every_breaking = dispatcher.get("breaking_sweep_every_minutes")
    every_academic = dispatcher.get("academic_sweep_every_minutes")
    if not all(isinstance(v, int) for v in
               (attempts, every_breaking, every_academic)):
        return 0, 0
    try:
        healthcheck_minute = cron_minute_field(dispatcher.get("healthcheck") or "")
    except ValueError:
        healthcheck_minute = None
    digest_by_hour = {}
    for slot, expr in sorted(slots.items()):
        try:
            s_min, s_hour = cron_minutes(expr)
        except (ValueError, TypeError):
            continue
        digest_by_hour.setdefault(s_hour, set())
        for index in range(attempts + 1):
            digest_by_hour[s_hour].add(s_min + REFIRE_GAP_MINUTES * index)
    work = 0
    noop = 0
    for hour in range(24):
        digest_minutes = digest_by_hour.get(hour) or set()
        for minute in range(60):
            due = (every_breaking and minute % every_breaking == 0) \
                or (every_academic and minute % every_academic == 0) \
                or minute == healthcheck_minute \
                or minute in digest_minutes \
                or (hour, minute) in extra_minutes
            if due:
                work += 1
            else:
                noop += 1
    return work, noop


def dispatcher_cron_problems(schedule):
    """Reasons the merged dispatcher's logical schedule is wrong or unbounded.

    Every fact is derived from the committed record and from the constants
    beside it, so a re-fire window that drifts, grows, or loses its slot is a
    red suite rather than a retry that quietly never fires.

    THE THREE THINGS THAT MATTER, in the order a bug would appear:

      1. every slot has a re-fire window, and the window has exactly
         REFIRE_ATTEMPTS entries -- a slot with no retry is a MISSED digest when
         cron start jitter bites, and an unbounded window is two arms racing the
         02:05 UTC GitHub fallback for the same slot on every bad day;
      2. attempt N sits exactly N * REFIRE_GAP_MINUTES after the slot, so the
         gap the whole thing exists for did not quietly change;
      3. the last attempt ends at slot+20, which leaves the 02:05 UTC fallback
         ten clear minutes to own the slot.
    """
    problems = []
    dispatcher = schedule.get("dispatcher_crons")
    if not isinstance(dispatcher, dict):
        problems.append(
            "%s has no dispatcher_crons block, so the schedule the ONE deployed "
            "cron reproduces cannot be checked at all -- and a merge whose "
            "internal cadence is unrecorded is exactly the drift Check 16 "
            "exists to kill" % SCHEDULE_JSON_REL)
        return problems

    slots = dispatcher.get("digest_slots")
    windows = dispatcher.get("digest_refire_window")
    if not isinstance(slots, dict) or not slots:
        problems.append("%s declares no digest slots" % SCHEDULE_JSON_REL)
        return problems
    if set(slots) != set(DISPATCH_DIGEST_SLOTS):
        problems.append(
            "the dispatcher's digest slots are %s, not %s. Adding or dropping a "
            "slot changes the KV budget and the delivery line, so it has to be "
            "deliberate rather than incidental."
            % (sorted(slots), sorted(DISPATCH_DIGEST_SLOTS)))
    if not isinstance(windows, dict):
        problems.append("%s declares no digest_refire_window" % SCHEDULE_JSON_REL)
        return problems

    for slot in sorted(slots):
        if slot not in windows:
            problems.append(
                "digest slot %r has no re-fire window. Cloudflare cron start "
                "jitter is unbounded from the inside: `%s` can genuinely begin "
                "twenty minutes late, and with no retry that is a MISSED digest "
                "rather than a late one." % (slot, slots[slot]))
            continue
        window = windows[slot]
        if not isinstance(window, list) or len(window) != REFIRE_ATTEMPTS:
            problems.append(
                "the %r re-fire window holds %r entries, not %d. The window is "
                "what makes the retry BOUNDED: it must stop before the 02:05 UTC "
                "GitHub fallback owns the slot, or the two arms race every bad "
                "day and a race is a second push by construction."
                % (slot, window if not isinstance(window, list)
                   else len(window), REFIRE_ATTEMPTS))
            continue
        try:
            p_min, p_hour = cron_minutes(slots[slot])
        except (ValueError, TypeError) as exc:
            problems.append("unparseable digest slot %s: %s" % (slot, exc))
            continue
        base = p_hour * 60 + p_min
        for index, expr in enumerate(window, start=1):
            try:
                r_min, r_hour = cron_minutes(expr)
            except (ValueError, TypeError) as exc:
                problems.append("unparseable re-fire %s: %s" % (expr, exc))
                continue
            offset = (r_hour * 60 + r_min) - base
            if offset != REFIRE_GAP_MINUTES * index:
                problems.append(
                    "re-fire %d for slot %r is %r, which is %+d minutes after "
                    "its slot %r, not %d. A re-fire that fires before its slot, "
                    "or later than the %dth step, is not the safety net this "
                    "window exists to be."
                    % (index, slot, expr, offset, slots[slot],
                       REFIRE_GAP_MINUTES * index, REFIRE_ATTEMPTS))
        last_min, last_hour = cron_minutes(window[-1])
        last_minutes = last_hour * 60 + last_min
        # The fallback that owns the slot after the window closes. Compared
        # WITHIN the same UTC day, which is why it is keyed by slot: an
        # evening window at 13:55 is nowhere near the 02:05 morning fallback.
        fallback = GITHUB_FALLBACK_UTC.get(slot)
        if fallback is not None and last_minutes >= fallback:
            problems.append(
                "the %r re-fire window ends at %r (%02d:%02d UTC), which is at "
                "or after the %02d:%02d UTC GitHub fallback in daily-news.yml. "
                "The Worker must stop before the fallback owns the slot, or two "
                "arms race every bad day."
                % (slot, window[-1], last_hour, last_min,
                   fallback // 60, fallback % 60))
    return problems


def cadence_parity_problems(schedule):
    """Reasons the merged dispatcher does NOT reproduce the pre-merge schedule.

    THE STRONGEST CHECK IN THIS FILE, and the one the merge actually rests on.
    Everything else here compares committed strings. This one recomputes, from
    the dispatcher's own committed data, the exact set of (hour, minute) pairs
    each job fires on, and requires that set to be IDENTICAL to what the
    pre-merge cron expression produced.

    So "do not weaken breaking detection: keep the same effective sweep
    frequency" stops being a promise in a comment and becomes a set equality
    over 1440 minutes. A dispatcher on a five-minute grid, or a sweep at
    minute % 5, fails here immediately.
    """
    problems = []
    dispatcher = schedule.get("dispatcher_crons")
    if not isinstance(dispatcher, dict):
        problems.append("%s declares no dispatcher_crons" % SCHEDULE_JSON_REL)
        return problems

    def minutes_of(expr):
        return set(cron_to_npt(expr))

    jobs = (
        ("breaking sweep", dispatcher.get("breaking_sweep"),
         dispatcher.get("breaking_sweep_every_minutes"), "*/3 * * * *"),
        ("academic sweep", dispatcher.get("academic_sweep"),
         dispatcher.get("academic_sweep_every_minutes"), "*/2 * * * *"),
        ("healthcheck", dispatcher.get("healthcheck"), None, "17 * * * *"),
    )
    for label, expr, every, before in jobs:
        if not expr:
            problems.append(
                "the dispatcher records no %s schedule, so its cadence cannot be "
                "compared with the pre-merge `%s`" % (label, before))
            continue
        try:
            now, then = minutes_of(expr), minutes_of(before)
        except ValueError as exc:
            problems.append("unparseable %s schedule: %s" % (label, exc))
            continue
        if now != then:
            problems.append(
                "the merged %s fires on %d Nepal minute slot(s); the pre-merge "
                "`%s` fired on %d. This is the cadence regression the merge was "
                "forbidden to cause. First divergence: %r"
                % (label, len(now), before, len(then),
                   sorted(now ^ then)[:4]))
        if every is not None:
            try:
                stride = int(expr.split("/")[1].split()[0])
            except (IndexError, ValueError):
                problems.append(
                    "the dispatcher's %s records every_minutes=%r but its cron %r "
                    "does not say so, so the recorded number is not derived from "
                    "the schedule" % (label, every, expr))
                continue
            if stride != every:
                problems.append(
                    "the dispatcher records %s every_minutes=%r, but its cron %r "
                    "fires every %d minutes. A recorded stride that disagrees "
                    "with the schedule is a budget figure nobody recomputes."
                    % (label, every, expr, stride))

    slots = dispatcher.get("digest_slots") or {}
    before_slots = {"morning": "35 1 * * *", "evening": "35 13 * * *"}
    for slot, before in sorted(before_slots.items()):
        if slot not in slots:
            problems.append(
                "the dispatcher has no %r digest slot; the pre-merge schedule "
                "fired `%s` and losing it is a silent loss of a daily delivery"
                % (slot, before))
            continue
        try:
            if minutes_of(slots[slot]) != minutes_of(before):
                problems.append(
                    "the merged %r digest slot is %r, which is not the pre-merge "
                    "`%s`" % (slot, slots[slot], before))
        except ValueError as exc:
            problems.append("unparseable %r digest slot: %s" % (slot, exc))
    return problems


def internal_fire_count_problems(schedule):
    """Reasons the recorded internal per-day counts are not what the plan gives.

    Three numbers, and the relationship between them is the whole point:
    job fires is NOT tick count, because a minute divisible by six runs both
    sweeps. work_ticks + no_op_ticks must be exactly the number of Cloudflare
    invocations, and no_op_ticks is the number of ticks that must cost no KV and
    no network I/O at all.

    SCOPE, DELIBERATELY: the JOB-FIRE TOTAL is every term -- the pre-merge
    cadence, the prebuild trigger added in U9, and the retry ladder moved onto
    the Worker in U10 -- because that total must equal the sum of its parts.
    The TICK walk below is the pre-merge grid plus both additions, since both
    ride the same every-minute trigger. The per-addition arithmetic (and the
    exact work/no-op swap each one caused) lives in
    `additions_internal_count_problems` and `ladder_internal_count_problems`;
    splitting the scopes is what keeps each arithmetic checkable by hand.
    """
    problems = []
    dispatcher = schedule.get("dispatcher_crons")
    if not isinstance(dispatcher, dict):
        return ["%s declares no dispatcher_crons" % SCHEDULE_JSON_REL]

    slots = dispatcher.get("digest_slots") or {}
    attempts = dispatcher.get("digest_refire_attempts")
    every_breaking = dispatcher.get("breaking_sweep_every_minutes")
    every_academic = dispatcher.get("academic_sweep_every_minutes")

    def int_or_none(value, label):
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append("%s is %r, which is not an integer count"
                            % (label, value))
            return None
        return value

    invocations = int_or_none(schedule.get("expected_invocations_per_day"),
                              "expected_invocations_per_day")
    jobs_claimed = int_or_none(schedule.get("expected_internal_job_fires_per_day"),
                               "expected_internal_job_fires_per_day")
    work_claimed = int_or_none(
        schedule.get("expected_internal_work_ticks_per_day"),
        "expected_internal_work_ticks_per_day")
    noop_claimed = int_or_none(schedule.get("no_op_ticks_per_day"),
                               "no_op_ticks_per_day")
    if None in (invocations, jobs_claimed, work_claimed, noop_claimed):
        return problems

    for expr in (dispatcher.get("dispatcher") or "",):
        if expr:
            try:
                actual = cron_invocations_per_day(expr)
            except ValueError as exc:
                problems.append(str(exc))
                return problems
            if actual != invocations:
                problems.append(
                    "expected_invocations_per_day is %d, but the dispatcher %r "
                    "fires %d times a day" % (invocations, expr, actual))

    if isinstance(every_breaking, int) and isinstance(every_academic, int) \
            and isinstance(attempts, int):
        breaking = (60 // every_breaking) * 24 if every_breaking else 0
        academic = (60 // every_academic) * 24 if every_academic else 0
        health = 24  # the health check is hourly on one fixed minute
        digest = len(slots) * (1 + attempts)
        # The prebuild rides this same trigger, so it is a job fire on the
        # existing grid rather than a new trigger. Counted from the committed
        # block so a prebuild added without its count is red here.
        prebuild = _prebuild_minutes_from(schedule, problems)
        ladder_fires = ladder_fire_count(schedule)
        expected_jobs = (breaking + academic + health + digest + len(prebuild)
                         + ladder_fires)
        if expected_jobs != jobs_claimed:
            problems.append(
                "expected_internal_job_fires_per_day is %d, but the schedule "
                "produces %d (%d breaking + %d academic + %d healthcheck + "
                "%d digest + %d prebuild + %d ladder). A per-day figure that "
                "does not follow from the schedule is a documented lie."
                % (jobs_claimed, expected_jobs, breaking, academic, health,
                   digest, len(prebuild), ladder_fires))
        # TICKS, NOT JOBS. A tick is counted ONCE however many jobs run on it, and a
        # tick is one of 1440 grid positions a day -- so this walks the whole
        # day rather than counting minutes per hour and multiplying. Two earlier
        # shapes of this arithmetic were wrong and both were caught here: adding
        # sweep ticks to digest ticks double-counts 01:40, and counting digest
        # MINUTES then multiplying by 24 invents 48 digest ticks a day when
        # there are 10. The digest slots fire in ONE hour each.
        try:
            healthcheck_minute = cron_minute_field(
                dispatcher.get("healthcheck") or "")
        except ValueError as exc:
            healthcheck_minute = None
            problems.append(
                "the dispatcher records no parseable healthcheck cron (%s), so "
                "the hourly tick cannot be counted" % exc)
        # hour -> set of minutes on which a digest job is due.
        digest_by_hour = {}
        for slot, expr in sorted(slots.items()):
            try:
                s_min, s_hour = cron_minutes(expr)
            except (ValueError, TypeError) as exc:
                problems.append("unparseable %r digest slot: %s" % (slot, exc))
                continue
            digest_by_hour.setdefault(s_hour, set())
            for index in range(attempts + 1):
                digest_by_hour[s_hour].add(s_min + REFIRE_GAP_MINUTES * index)
        for p_hour, p_min in prebuild:
            digest_by_hour.setdefault(p_hour, set()).add(p_min)
        for l_hour, l_min in ladder_minutes(schedule):
            digest_by_hour.setdefault(l_hour, set()).add(l_min)
        work = 0
        noop = 0
        for hour in range(24):
            digest_minutes = digest_by_hour.get(hour) or set()
            for minute in range(60):
                due = bool(every_breaking and minute % every_breaking == 0) \
                    or bool(every_academic and minute % every_academic == 0) \
                    or minute == healthcheck_minute \
                    or minute in digest_minutes
                if due:
                    work += 1
                else:
                    noop += 1
        if work_claimed is not None and noop_claimed is not None:
            if work != work_claimed:
                problems.append(
                    "expected_internal_work_ticks_per_day is %d, but the minute "
                    "grid produces %d ticks on which at least one job is due. A "
                    "tick due to two jobs is still ONE Cloudflare invocation."
                    % (work_claimed, work))
            if noop != noop_claimed:
                problems.append(
                    "no_op_ticks_per_day is %d, but %d of the 1440 daily ticks "
                    "have nothing due. These are the ticks that must cost no KV "
                    "and no network I/O, so the figure is the merge's whole KV "
                    "argument." % (noop_claimed, noop))
            if work + noop != invocations:
                problems.append(
                    "work_ticks (%d) + no_op_ticks (%d) = %d, which is not the "
                    "%d invocations Cloudflare makes. Every tick is either work "
                    "or a no-op, and a tick that is neither is an unaccounted "
                    "invocation." % (work, noop, work + noop, invocations))
    return problems


def kv_write_budget_problems(schedule, worker_consts=None):
    """Reasons the recorded daily KV budget does not follow from its own table.

    Three separate sums, each checked against the number printed next to it:

      * the digest arm's terms add up to its own `total_steady`
      * the arms add up to `total_max`  (what the heartbeat check spent before
        U5 existed, kept so that number is also derived rather than asserted)
      * `total_max + total_steady` adds up to `system_total_max`

    and, when the Worker's constants are available, the digest arm's per-day
    figures are held against the number of digest SLOTS actually deployed --
    because "2 writes a day" is only true while there are two slots.
    """
    problems = []
    heartbeat = schedule.get("heartbeat")
    table = (heartbeat or {}).get("kv_writes_per_day") \
        if isinstance(heartbeat, dict) else None
    if not isinstance(table, dict):
        problems.append(
            "%s has no heartbeat.kv_writes_per_day table, so the daily KV "
            "budget is an assertion with nothing behind it" % SCHEDULE_JSON_REL)
        return problems
    digest = table.get("digest")
    if not isinstance(digest, dict):
        problems.append(
            "%s's kv_writes_per_day has no `digest` block. The delivery arm "
            "writes to the same namespace as the heartbeat sweep, so its cost "
            "belongs in this table and not in a comment." % SCHEDULE_JSON_REL)
        return problems

    values = {}
    for key in DIGEST_KV_TERMS:
        raw = digest.get(key)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            problems.append(
                "kv_writes_per_day.digest.%s is %r, which is not a "
                "non-negative integer count" % (key, raw))
        else:
            values[key] = raw
    if len(values) != len(DIGEST_KV_TERMS):
        return problems
    summed = sum(values.values())
    if digest.get("total_steady") != summed:
        problems.append(
            "kv_writes_per_day.digest.total_steady is %r, but its own terms "
            "sum to %d (%s). A total nobody recomputes is a comment that "
            "starts lying."
            % (digest.get("total_steady"), summed,
               " + ".join("%s=%d" % (k, values[k]) for k in DIGEST_KV_TERMS)))

    arms = {}
    for key in ("breaking", "academic", "alert_stamp_max"):
        raw = table.get(key)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            problems.append("kv_writes_per_day.%s is %r, which is not a "
                            "non-negative integer count" % (key, raw))
        else:
            arms[key] = raw
    if len(arms) != 3:
        return problems
    if table.get("total_max") != sum(arms.values()):
        problems.append(
            "kv_writes_per_day.total_max is %r, but the heartbeat arms sum to "
            "%d (%s)"
            % (table.get("total_max"), sum(arms.values()),
               " + ".join("%s=%d" % (k, arms[k]) for k in sorted(arms))))
    expected_system = sum(arms.values()) + summed
    if table.get("system_total_max") != expected_system:
        problems.append(
            "kv_writes_per_day.system_total_max is %r, but the table's own "
            "figures produce %d (%d heartbeat + %d digest). The system total "
            "is what the free-tier allowance is compared against, so a wrong "
            "one here is how a budget silently creeps."
            % (table.get("system_total_max"), expected_system,
               sum(arms.values()), summed))

    if worker_consts is not None:
        slots = digest_slots_per_day(schedule)
        for key in ("artifact_writes", "artifact_expiry_deletes"):
            if values[key] != slots:
                problems.append(
                    "kv_writes_per_day.digest.%s is %d, but %d digest slot "
                    "cron(s) are deployed. The digest arm's steady-state cost "
                    "is one %s per slot; when a slot is added this number has "
                    "to move with it, which is what makes the arithmetic a "
                    "check instead of a note."
                    % (key, values[key], slots,
                       "artifact write" if key == "artifact_writes" else
                       "TTL delete"))
    return problems


# ==========================================================================
# Check 9 classification
# ==========================================================================

#: A step "writes delivery state" if it can cause a slot to be treated as
#: delivered: a real deploy, a per-channel flag file, the delivery marker,
#: or a step output the marker is derived from.
DELIVERY_WRITE_MARKERS = (
    "wrangler pages deploy",
    "state/last_digest_sent.json",
    "state/.%s_ok",
    "_ok'",
)

#: The v2-only flag this suite must be able to assert about, assembled from
#: two parts on purpose.
#:
#: tests/breaking/test_ladder_sync.py fails ANY *.py line containing the
#: literal two-dash standalone flag, because that flag must never be *used*
#: anywhere in this repository. This file is the one place that has to
#: *name* it: the Bug-A regression test asserts that the gate REJECTS a CLI
#: advertising it. Assembling the string at runtime keeps the ladder-sync
#: sweep fully armed (it still catches a genuine accidental use such as
#: `opencode run <two-dash standalone>`) while letting the guardrail talk
#: about the flag at all. This comment cannot spell the flag out for the
#: same reason the constant cannot contain it. Do not "simplify" it back
#: into one literal -- the next test run will tell you why.
STANDALONE_FLAG = "--" + "standalone"

DRY_RUN_TOKENS = ("DRY_RUN", "dry_run", "inputs.dry_run")


class WorkflowLintBase(unittest.TestCase):
    """Shared plumbing. Nothing here asserts anything by itself."""

    def require_bash(self):
        """Return the bash path, or FAIL the test.

        `self.fail()` and not `self.skipTest()` is a deliberate choice.  A
        skip reports green in CI while the workflows go unlinted, which is
        the precise silence that let two outages ship with 425 passing
        tests.  A failure here is visible, explainable, and forces someone
        to decide whether shell linting is wanted on that machine.
        """
        bash = W.bash_executable()
        if bash is None:
            self.fail(
                "bash was not found on PATH, so the shell embedded in "
                ".github/workflows/ CANNOT be checked.\n"
                "This test fails instead of skipping on purpose: a skipped "
                "shell lint is a green CI run with no shell lint, which is "
                "how Bug A (opencode --help read from stdout instead of "
                "stderr) shipped in the first place. Fix the environment "
                "(install bash, or put it on PATH) rather than the test."
            )
        return bash


# ==========================================================================
# Check 1 -- every `run:` block is syntactically valid bash
# ==========================================================================


class RunBlockShellSyntaxTests(WorkflowLintBase):
    def test_every_run_block_parses_as_bash(self):
        bash = self.require_bash()
        checked = 0
        failures = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, _step in W.iter_run_blocks(wf):
                checked += 1
                proc = W.bash_syntax_check(run, bash)
                if proc.returncode != 0:
                    failures.append(
                        "%s / %s / %r\n    bash -n exit %d: %s"
                        % (wf, job, step, proc.returncode, (proc.stderr or "").strip())
                    )
        self.assertEqual(
            [], failures,
            "shell syntax errors in workflow `run:` blocks:\n  " + "\n  ".join(failures),
        )
        # A loose floor, deliberately not an exact count: it exists only so
        # a parser regression that silently yields zero run blocks cannot
        # turn this lint green by finding nothing to check.
        self.assertGreaterEqual(
            checked, 40,
            "expected at least 40 `run:` blocks across the five workflows, found %d. "
            "Either a workflow lost its steps or the YAML parser stopped "
            "descending into `jobs` -- a lint that checks nothing is not a lint."
            % checked,
        )
        print("check 1: bash -n accepted %d run: blocks across %d workflows"
              % (checked, len(W.WORKFLOW_NAMES)))


# ==========================================================================
# Check 2 -- heredoc terminators sit at column 0 in the PARSED string
# ==========================================================================


class HeredocTerminatorTests(WorkflowLintBase):
    def test_every_heredoc_terminator_is_at_column_zero(self):
        checked = 0
        failures = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, _s in W.iter_run_blocks(wf):
                checked += 1
                for tag, line_no in find_unterminated_heredocs(run):
                    failures.append(
                        "%s / %s: heredoc <<%s opened on line %d of the step has no "
                        "line exactly equal to '%s' at column 0 anywhere after it. "
                        "bash will fail with \"here-document delimited by "
                        "end-of-file (wanted '%s')\". This is Bug B: the YAML "
                        "block scalar indents the terminator, and YAML comments "
                        "cannot stop it."
                        % (wf, job, tag, line_no, tag, tag)
                    )
        self.assertEqual(
            [], failures,
            "mis-indented heredoc terminators:\n  " + "\n  ".join(failures),
        )
        self.assertGreaterEqual(
            checked, 1, "no `run:` blocks were found; the lint is not running"
        )
        total = sum(
            sum(1 for line in run.split("\n") if HEREDOC_RE.search(line))
            for wf in W.WORKFLOW_NAMES
            for _j, _s, run, _st in W.iter_run_blocks(wf)
        )
        print("check 2: %d heredocs across %d run: blocks, all terminators at column 0"
              % (total, checked))

    def test_mis_indented_terminator_is_detected(self):
        """REGRESSION PROOF: a lint that cannot fail is decoration.

        The bad case is built as an in-memory string.  No workflow file is
        edited: this is Bug B's exact shape (body and `PY` two columns right
        of column 0, which is what a block scalar that strips ten produces)
        and the check must reject it.
        """
        good = (
            "set -eo pipefail\n"
            "python3 - <<'PY'\n"
            "import json\n"
            "print(json.dumps({}))\n"
            "PY\n"
        )
        bad = (
            "set -eo pipefail\n"
            "if [ -f state/x.json ]; then\n"
            "  python3 - <<'PY'\n"
            "  import json\n"
            "  print(json.dumps({}))\n"
            "  PY\n"
            "fi\n"
        )
        self.assertEqual([], find_unterminated_heredocs(good),
                         "sanity: the well-formed heredoc must not be flagged")
        found = find_unterminated_heredocs(bad)
        self.assertEqual(
            1, len(found),
            "the mis-indented terminator MUST be flagged; got %r" % (found,),
        )
        self.assertEqual("PY", found[0][0])
        self.assertEqual(3, found[0][1], "the report must point at the opening redirect line")


# ==========================================================================
# Check 3 -- every heredoc's Python body compiles
# ==========================================================================


class HeredocPythonBodyTests(WorkflowLintBase):
    def test_every_python_heredoc_body_compiles(self):
        checked = 0
        failures = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, _s in W.iter_run_blocks(wf):
                for tag, body in heredoc_bodies(run):
                    checked += 1
                    try:
                        compile(body, "<%s heredoc in %s / %s>" % (tag, wf, step), "exec")
                    except SyntaxError as exc:
                        failures.append(
                            "%s / %s / %s heredoc: %s at body line %s: %s"
                            % (wf, job, tag, type(exc).__name__, exc.lineno, exc.msg)
                        )
        self.assertEqual(
            [], failures,
            "Python heredoc bodies that do not compile:\n  " + "\n  ".join(failures),
        )
        self.assertGreaterEqual(
            checked, 1, "no Python heredocs were found; the lint is not running"
        )
        print("check 3: %d Python heredoc bodies compiled" % checked)


# ==========================================================================
# Check 4 -- the OpenCode flag gate, against stubbed streams (Bug A)
# ==========================================================================


class OpenCodeFlagGateTests(WorkflowLintBase):
    #: First and last lines of the gate, matched against the PARSED step.
    #: The gate is located, never hand-copied, so this test cannot drift
    #: away from what the workflow actually runs.
    GATE_FIRST = 'help_out="$(opencode run --help 2>&1)"'
    GATE_LAST = "OpenCode verify OK"
    GATE_WORKFLOWS = ("daily-news.yml", "drift-canary.yml", "breaking-monitor.yml")

    # The shell options the workflow sets two lines above the gate.  Taken
    # from the same step so the gate runs under the same `set -o pipefail`
    # the runner uses; without it, `printf | grep -q` could mask a failure.
    GATE_PREAMBLE = "set -eo pipefail"

    def extract_gate(self):
        """Return the flag-drift gate, read verbatim from the parsed YAML."""
        found = []
        for wf in self.GATE_WORKFLOWS:
            for job, step, run, _s in W.iter_run_blocks(wf):
                if self.GATE_FIRST not in run:
                    continue
                lines = run.split("\n")
                start = next(i for i, l in enumerate(lines) if self.GATE_FIRST in l)
                end = next(i for i, l in enumerate(lines) if self.GATE_LAST in l)
                found.append((wf, job, step, "\n".join(lines[start:end + 1])))
        self.assertTrue(
            found,
            "the OpenCode flag gate (a `help_out=` capture followed by a "
            "`--auto`/`--model`/`%s` check) was not found in any of "
            "%s. If it was renamed or deleted, this test must be updated "
            "deliberately -- not deleted." % (STANDALONE_FLAG, self.GATE_WORKFLOWS),
        )
        gates = {g for _wf, _j, _s, g in found}
        self.assertEqual(
            1, len(gates),
            "the flag gate has DRIFTED between workflows -- the copies are no "
            "longer identical, so one of them is a different gate:\n%s"
            % "\n---\n".join("%s / %s / %s:\n%s" % (w, j, s, g) for w, j, s, g in found),
        )
        return found

    def run_gate(self, gate, help_stream, help_flags):
        """Run the extracted gate with a stubbed `opencode` first on PATH."""
        bash = self.require_bash()
        stub_body = W.STUB_OPENCODE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            W.install_stub_on_path(bin_dir, stub_body, "opencode")
            script = self.GATE_PREAMBLE + "\n" + gate + "\n"
            return W.run_bash_script(
                script,
                env_overrides={
                    "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                    "STUB_HELP_STREAM": help_stream,
                    "STUB_HELP_FLAGS": help_flags,
                },
                cwd=tmp,
                bash=bash,
            )

    def test_the_three_gate_copies_are_identical(self):
        found = self.extract_gate()
        self.assertEqual(
            len(self.GATE_WORKFLOWS), len(found),
            "expected the flag gate in all of %s, found it in %s"
            % (self.GATE_WORKFLOWS, sorted({f[0] for f in found})),
        )

    def test_gate_passes_when_help_is_on_stderr_and_flags_are_present(self):
        """Bug A, case 1: the CLI moved --help to stderr and the gate must pass."""
        found = self.extract_gate()
        _wf, job, step, gate = found[0]
        proc = self.run_gate(gate, help_stream="stderr", help_flags="--auto --model")
        self.assertEqual(
            0, proc.returncode,
            "the gate in %s / %r must pass when `opencode run --help` writes to "
            "STDERR and both --auto and --model are advertised. This is the "
            "exact shape of Bug A. stdout:\n%s\nstderr:\n%s"
            % (job, step, proc.stdout, proc.stderr),
        )

    def test_gate_passes_when_help_is_on_stdout_and_flags_are_present(self):
        """The pre-move shape must keep working, or the fix is not a fix."""
        found = self.extract_gate()
        _wf, _job, _step, gate = found[0]
        proc = self.run_gate(gate, help_stream="stdout", help_flags="--auto --model")
        self.assertEqual(
            0, proc.returncode,
            "the gate must also pass when --help writes to STDOUT (the old CLI "
            "behaviour); `2>&1` has to cover both streams. stdout:\n%s\nstderr:\n%s"
            % (proc.stdout, proc.stderr),
        )

    def test_gate_fails_and_names_auto_when_the_flag_is_genuinely_absent(self):
        """LOAD-BEARING: a gate that passes on a truly missing flag is worse
        than the outage, because it converts a loud failure into silence."""
        found = self.extract_gate()
        _wf, _job, _step, gate = found[0]
        proc = self.run_gate(gate, help_stream="stderr", help_flags="--model")
        self.assertNotEqual(
            0, proc.returncode,
            "the gate PASSED although --auto is absent from the help text. "
            "That means the gate can no longer detect the drift it exists to "
            "detect. stdout:\n%s" % proc.stdout,
        )
        self.assertIn(
            "--auto", proc.stdout,
            "on a genuinely missing --auto the gate must name the flag it "
            "could not find, so the failure is diagnosable from the log. "
            "stdout was:\n%s" % proc.stdout,
        )

    def test_gate_fails_and_names_standalone_when_the_flag_appears(self):
        found = self.extract_gate()
        _wf, _job, _step, gate = found[0]
        proc = self.run_gate(gate, help_stream="stderr",
                            help_flags="--auto --model " + STANDALONE_FLAG)
        self.assertNotEqual(
            0, proc.returncode,
            "the gate PASSED although %s is advertised. That is "
            "v2-only flag drift and must be fatal. stdout:\n%s"
            % (STANDALONE_FLAG, proc.stdout),
        )
        self.assertIn(
            STANDALONE_FLAG, proc.stdout,
            "on %s drift the gate must name the offending flag. "
            "stdout was:\n%s" % (STANDALONE_FLAG, proc.stdout),
        )

    def test_pre_fix_gate_shape_dies_against_a_stderr_only_help(self):
        """MUTATION PROOF for Bug A.

        Reconstructs the pre-fix shape -- capture stdout only, no `2>&1` --
        and asserts it FAILS against a CLI whose help text is on stderr,
        producing the exact production message.  Without this, "the gate
        passes" could be true for the wrong reason: e.g. because the stub
        was never found on PATH, in which case the gate would still print
        FATAL and still exit non-zero, and every "must pass" case above
        would be telling us nothing.
        """
        found = self.extract_gate()
        pre_fix = (
            "help_out=\"$(opencode run --help)\"\n"
            "printf '%s' \"$help_out\" | grep -q -- --auto || "
            "{ echo \"FATAL: opencode run lacks --auto\"; exit 1; }\n"
        )
        current = found[0][3]
        # The pre-fix shape differs from the shipped gate by exactly the
        # stream redirect, so the failure below is attributable to `2>&1`.
        self.assertIn("2>&1", current)
        self.assertNotIn("2>&1", pre_fix)

        proc = self.run_gate(pre_fix, help_stream="stderr", help_flags="--auto --model")
        self.assertNotEqual(
            0, proc.returncode,
            "the pre-fix gate shape unexpectedly passed; the stub or the PATH "
            "setup is wrong, which would invalidate every case in this class",
        )
        self.assertIn("--auto", proc.stdout)


# ==========================================================================
# Check 5 -- every repo path a workflow references exists
# ==========================================================================


SCRIPT_REF_RE = re.compile(r"scripts/[A-Za-z0-9_./-]+\.py")
TEST_FILE_REF_RE = re.compile(r"(?<![\w./-])tests/[A-Za-z0-9_./-]+\.py")
UNITTEST_DIR_REF_RE = re.compile(r"python3\s+-m\s+unittest\b[^\n]*?-s\s+(\S+)")


class ReferencedPathTests(WorkflowLintBase):
    def test_every_referenced_script_and_test_path_exists(self):
        """Move the 07:20 NPT 'Verify required files exist' check into CI.

        A renamed script used to cost two failed digest runs before anyone
        noticed; a mis-typed path now costs one red CI run instead.
        """
        missing = []
        seen = set()
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, _s in W.iter_run_blocks(wf):
                for pattern in (SCRIPT_REF_RE, TEST_FILE_REF_RE, UNITTEST_DIR_REF_RE):
                    for m in pattern.finditer(run):
                        ref = m.group(1) if pattern is UNITTEST_DIR_REF_RE else m.group(0)
                        if ref in seen:
                            continue
                        seen.add(ref)
                        if not (W.REPO_ROOT / ref).exists():
                            missing.append(
                                "%s / %s references %r, which does not exist in the repository"
                                % (wf, job, ref)
                            )
        self.assertEqual(
            [], missing,
            "workflow references to paths that do not exist:\n  " + "\n  ".join(missing),
        )
        self.assertGreaterEqual(
            len(seen), 10,
            "only %d distinct repo paths were referenced; expected at least 10, "
            "so the reference patterns are probably not matching" % len(seen),
        )
        print("check 5: %d distinct referenced repo paths all exist" % len(seen))


# ==========================================================================
# Check 6 -- every referenced secret is on the allowlist
# ==========================================================================


class SecretAllowlistTests(WorkflowLintBase):
    def _referenced(self):
        used = set()
        per_workflow = {}
        for wf in W.WORKFLOW_NAMES:
            names = set(SECRET_REF_RE.findall(W.raw_workflow_text(wf)))
            per_workflow[wf] = names
            used |= names
        return used, per_workflow

    def test_every_referenced_secret_is_allowlisted(self):
        used, per_workflow = self._referenced()
        unlisted = sorted(used - ALLOWED_SECRETS)
        self.assertEqual(
            [], unlisted,
            "workflows reference secrets that are NOT on the allowlist: %s.\n"
            "Either these names are typos (and would fail only when the digest "
            "needs them), or they are real and must be added to ALLOWED_SECRETS "
            "AND configured in repository settings. Referenced, by workflow: %s"
            % (unlisted, {k: sorted(v) for k, v in per_workflow.items() if v}),
        )
        self.assertTrue(used, "no secrets.* references found at all; the scan is broken")

    def test_allowlist_has_no_stale_names(self):
        used, _ = self._referenced()
        stale = sorted(ALLOWED_SECRETS - used)
        self.assertEqual(
            [], stale,
            "ALLOWED_SECRETS lists %s, which no workflow references any more. "
            "A stale allowlist entry hides the fact that a secret has stopped "
            "being used -- remove it here once the workflow change is intended."
            % (stale,),
        )

    def test_every_workflow_with_secrets_is_in_the_allowlist_coverage(self):
        _used, per_workflow = self._referenced()
        for wf, names in sorted(per_workflow.items()):
            if not names:
                continue
            missing = sorted(n for n in names if n not in ALLOWED_SECRETS)
            self.assertEqual([], missing, "%s references unlisted secrets: %s" % (wf, missing))


# ==========================================================================
# Check 7 -- `if: always()` steps that can kill an already-successful job
# ==========================================================================


def effective_command_count(run_text):
    """Commands in a shell block: non-blank, non-comment lines."""
    return len([
        line for line in run_text.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ])


class AlwaysStepFatalityTests(WorkflowLintBase):
    def test_always_steps_that_can_fail_are_marked_continue_on_error(self):
        offenders = []
        examined = 0
        for wf in W.WORKFLOW_NAMES:
            for job, step, step_dict in W.iter_all_steps(wf):
                if "run" not in step_dict:
                    continue
                condition = str(step_dict.get("if") or "")
                if "always()" not in condition:
                    continue
                run = step_dict["run"]
                n_cmds = effective_command_count(run)
                risky = ("<<" in run) or (n_cmds > ALWAYS_STEP_COMMAND_THRESHOLD)
                if not risky:
                    continue
                examined += 1
                if str(step_dict.get("continue-on-error") or "").lower() == "true":
                    continue
                key = (wf, job, step)
                if key in ALWAYS_STEP_EXEMPTIONS:
                    continue
                offenders.append(
                    "%s / %s / %r: `if: %s` with %d command(s)%s and no "
                    "`continue-on-error: true`.\n"
                    "    This is Bug B's shape: the step runs after the work has "
                    "already succeeded and can still fail the whole job, turning "
                    "a green monitor into a red one. Fix: add "
                    "`continue-on-error: true` to the step, or -- if it is "
                    "genuinely load-bearing -- add an entry to "
                    "ALWAYS_STEP_EXEMPTIONS with a reason."
                    % (wf, job, step, condition.strip(), n_cmds,
                       " and a heredoc" if "<<" in run else "")
                )
        self.assertEqual(
            [], offenders,
            "`if: always()` steps that can fail an already-successful job:\n  "
            + "\n  ".join(offenders),
        )
        self.assertGreaterEqual(
            examined, 1,
            "no `if: always()` steps were examined; the rule is not running",
        )
        print("check 7: %d risky always() steps, all continue-on-error" % examined)


# ==========================================================================
# Check 8 -- cron expressions and their Nepal Time equivalents
# ==========================================================================


class CronNepalTimeTests(WorkflowLintBase):
    def test_every_cron_converts_to_the_committed_npt_table(self):
        problems = []
        seen = set()
        for wf in W.WORKFLOW_NAMES:
            for expr in W.iter_schedule_crons(wf):
                seen.add((wf, expr))
                if (wf, expr) not in CRON_NPT_TABLE:
                    problems.append(
                        "%s: cron %r has no row in CRON_NPT_TABLE. Adding a "
                        "schedule is a deliberate act: add the row with the "
                        "Nepal Time you expect and let CI confirm the "
                        "conversion." % (wf, expr)
                    )
                    continue
                try:
                    actual = cron_to_npt(expr)
                except ValueError as exc:
                    problems.append("%s: cron %r is unparseable: %s" % (wf, expr, exc))
                    continue
                expected = set()
                for hour, minute in CRON_NPT_TABLE[(wf, expr)]:
                    if hour == "*":
                        expected |= {(h, minute) for h in range(24)}
                    else:
                        expected.add((hour, minute))
                if set(actual) != expected:
                    problems.append(
                        "%s: cron %r fires at %s NPT, table says %s"
                        % (wf, expr, sorted(actual), sorted(expected))
                    )
        self.assertEqual([], problems, "\n  ".join(problems))

        # Every table row must correspond to a real schedule, or the table
        # itself rots into a second source of truth nobody checks.
        for wf, expr in CRON_NPT_TABLE:
            self.assertIn(
                (wf, expr), seen,
                "CRON_NPT_TABLE has a row for %s / %r but no such schedule "
                "exists; remove the stale row" % (wf, expr),
            )
        print("check 8: %d schedules converted to Nepal Time (UTC+05:45), "
              "all matching the committed table" % len(seen))

    def test_the_worker_sweep_interval_is_never_restated_in_the_breaking_monitor_header(self):
        """FORMERLY PRINT-ONLY. NOW A GATE. This is the single highest
        value-per-line change in the schedule-drift work.

        It used to extract the Worker's crons and breaking-monitor.yml's claimed
        interval, print the two side by side, and return with ZERO assertions —
        so it had been printing `*/3` against a stale "every 5 min" to a log
        nobody reads, for as long as the comment had been wrong.

        The interval is not a fact this file can reconcile by itself, because the
        source of truth is deploy/wrangler.toml. So the gate is not "the comment
        agrees with the code" (there is no code number to agree with) but the
        stronger, durable rule: **the sweep interval must never be restated as a
        number in that comment at all.** The comment points at the source; the
        source is checked by Check 16; there is no third copy left to go stale.

        Scoped to breaking-monitor.yml on purpose. academic-result.yml carries
        the same class of hardcoded interval ("every 2 min"), currently
        CORRECT, but that file is not owned by this change and widening the gate
        to it would turn this suite red on a file nobody may edit here. Reported
        instead.
        """
        worker = W.REPO_ROOT / WORKER_JS_REL
        self.assertTrue(
            worker.is_file(),
            "%s is missing, so the Worker crons cannot be read and this check "
            "would pass by finding nothing" % WORKER_JS_REL)
        worker_crons = sorted(set(worker_cron_constants().values()))
        self.assertTrue(
            worker_crons,
            "no CRON_* constants were parsed out of %s (regex %r). Without them "
            "this check cannot tell whether the comment describes a real "
            "schedule, so it would be decoration."
            % (WORKER_JS_REL, CRON_CONST_RE.pattern))

        header = W.raw_workflow_text("breaking-monitor.yml")
        stale = [(m.group(0), header.count("\n", 0, m.start()) + 1)
                 for m in re.finditer(r"every\s+\d+\s+min(?:ute)?s?", header, re.I)]
        self.assertEqual(
            [], stale,
            "breaking-monitor.yml restates the Worker's sweep interval as a "
            "hardcoded number: %s.\n"
            "  the worker actually runs on the crons declared in "
            "deploy/wrangler.toml (%s) and mirrored in deploy/schedule.json, so "
            "a number in this comment is a fourth copy of one fact and it will "
            "drift again -- it already did once ('every 5 min' against a real "
            "*/3). Name the SOURCE instead: \"interval defined in "
            "deploy/wrangler.toml, enforced by Check 16\".\n"
            "  (academic-result.yml carries the same class of hardcoded interval; "
            "it is currently correct, and it is out of scope for this change -- "
            "reported, not edited.)"
            % (["line %d: %r" % (n, t) for t, n in stale],
               ", ".join(worker_crons)))
        print("check 8 interval gate: breaking-monitor.yml restates no sweep "
              "interval; worker crons are %s (declared in deploy/wrangler.toml)"
              % ", ".join(worker_crons))


# ==========================================================================
# Check 9 -- can a dry run mark a slot delivered?  REPORT-ONLY, on purpose
# ==========================================================================


class DryRunDeliveryMarkerTests(WorkflowLintBase):
    """REPORT-ONLY. Deliberately does not fail.

    The classifier is deliberately BROAD: it lists any step whose shell can
    create, write, delete or read a delivery marker or a per-channel flag.
    So "UNGUARDED" below means "this step has no dry-run gate at all", not
    "this step is certainly harmful" -- a step that only reads the marker
    and alerts on it is not the same risk as one that writes it. The two
    steps the audit asked about are named explicitly in the output, and the
    definitive answer for each is in the run report.

    The finding that prompted this class: drift-canary.yml dispatched
    `dry_run=true` for daily-news.yml, so daily-news.yml's "Deploy digest to
    Cloudflare Pages" and "Record delivery status" steps both had to be
    examined -- if either could run under a dry run then a canary could really
    publish and really write state/last_digest_sent.json with delivered=true
    for the morning slot, which would make the real morning run's fallback
    no-op check skip. THAT DISPATCH WAS REMOVED ON 2026-10-19 for Actions
    minutes, so this particular hazard is closed at the source and the only
    dispatcher of daily-news.yml left is the Worker's own. The class is kept,
    and still prints its answer on every run, because the question it asks is
    about EVERY dry-run path rather than that one dispatch -- and a step that
    is removed can be added back. This test prints the answer on every run so
    the question cannot be forgotten, and does not assert, because the fix
    decision belongs to the repository owner.
    """

    MARKER_PATTERNS = (
        re.compile(r"wrangler\s+pages\s+deploy"),
        re.compile(r"last_digest_sent\.json"),
        re.compile(r"state/\.\w+_ok"),
        re.compile(r"website_ok|ntfy_ok|gmail_ok|telegram_ok"),
    )

    def test_report_which_delivery_marker_steps_are_dry_run_guarded(self):
        rows = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, step_dict in W.iter_all_steps(wf):
                if "run" not in step_dict:
                    continue
                run = step_dict["run"]
                if not any(p.search(run) for p in self.MARKER_PATTERNS):
                    continue
                env = step_dict.get("env")
                env_text = " ".join("%s: %s" % (k, v) for k, v in env.items()) \
                    if isinstance(env, dict) else str(env or "")
                env_guarded = any(tok in env_text for tok in DRY_RUN_TOKENS)
                run_guarded = any(tok in run for tok in DRY_RUN_TOKENS)
                cond = str(step_dict.get("if") or "")
                cond_guarded = any(tok in cond for tok in DRY_RUN_TOKENS)
                guarded = env_guarded or run_guarded or cond_guarded
                rows.append((wf, step, guarded, env_guarded, cond_guarded))

        self.assertTrue(rows, "no delivery-marker-writing steps were identified; "
                              "the classifier is broken")
        print("")
        print("=" * 78)
        print("CHECK 9 -- DRY_RUN GUARD REPORT (report-only, never fails)")
        print("=" * 78)
        for wf, step, guarded, env_g, cond_g in rows:
            verdict = "GUARDED" if guarded else "*** UNGUARDED ***"
            print("  [%s] %-24s %r" % (verdict, wf, step))
            print("        DRY_RUN in env: %s | in run body: %s | in if: %s"
                  % (env_g, guarded and not env_g, cond_g))
        unguarded = [r for r in rows if not r[2]]
        print("-" * 78)
        if unguarded:
            print("  %d step(s) can act under a dry run:" % len(unguarded))
            for wf, step, _g, _e, _c in unguarded:
                print("    - %s / %r" % (wf, step))
        else:
            print("  every delivery-marker step is dry-run guarded")
        print("=" * 78)
        print("")


# ==========================================================================
# Parser self-checks -- so the lint above cannot pass by parsing nothing
# ==========================================================================


class ParserSelfCheckTests(unittest.TestCase):
    """The stdlib fallback loader is pinned by golden cases.

    PyYAML is the reference parser and is used whenever it is importable.
    The fallback exists so the suite can run on a bare interpreter, so its
    correctness cannot depend on PyYAML being present. These cases are
    in-memory only and include the exact block-scalar shapes that made Bug
    B invisible to the repo's existing hand-rolled stripper.
    """

    GOLDEN = (
        "name: T\n"
        "on:\n"
        "  schedule:\n"
        "    - cron: '30 0 * * *'\n"
        "  workflow_dispatch:\n"
        "jobs:\n"
        "  j:\n"
        "    steps:\n"
        "      - name: good\n"
        "        run: |\n"
        "          set -eo pipefail\n"
        "          python3 - <<'PY'\n"
        "          import json\n"
        "          PY\n"
        "      - name: deeper\n"
        "        if: always()\n"
        "        continue-on-error: true\n"
        "        env:\n"
        "          DRY_RUN: ${{ github.event.inputs.dry_run == 'true' }}\n"
        "        run: |\n"
        "          echo hi   # trailing comment stays inside the scalar\n"
    )

    def test_golden_block_scalars_resolve_to_column_zero_terminators(self):
        doc = W._MiniYAML(self.GOLDEN).parse()
        steps = doc["jobs"]["j"]["steps"]
        self.assertEqual(2, len(steps))
        good = steps[0]["run"]
        self.assertEqual(
            "set -eo pipefail\npython3 - <<'PY'\nimport json\nPY\n", good,
            "the block scalar that strips 10 must leave the PY terminator at "
            "column 0; a fixed 10-space stripper would be right here, so the "
            "next case is the one that matters",
        )
        self.assertEqual([], find_unterminated_heredocs(good))

    def test_golden_block_scalar_with_extra_indent_is_not_accepted(self):
        """Bug B's YAML shape: a body indented 2 past the block indent.

        Here the `PY` is at 12 while the block scalar strips 10, so bash
        receives "  PY" and the heredoc never closes. A loader that stripped
        a hardcoded 10 spaces -- or a test that did the same -- would see a
        terminator here and miss the outage entirely.
        """
        text = (
            "jobs:\n"
            "  j:\n"
            "    steps:\n"
            "      - name: buggy\n"
            "        run: |\n"
            "          set -eo pipefail\n"
            "          python3 - <<'PY'\n"
            "            import json\n"
            "            PY\n"
        )
        doc = W._MiniYAML(text).parse()
        run = doc["jobs"]["j"]["steps"][0]["run"]
        self.assertIn("  PY\n", run, "the loader must preserve the extra 2 columns")
        self.assertEqual(
            [("PY", 2)], find_unterminated_heredocs(run),
            "a terminator that is not at column 0 must be reported",
        )
        self.assertEqual([], list(heredoc_bodies(run)))

    def test_pyyaml_and_fallback_parser_agree_on_every_workflow(self):
        """Cross-check the two parsers, when PyYAML is available.

        Not a silent skip: when PyYAML is missing this prints a loud note
        to stderr saying the cross-check did not run. The fallback is still
        fully pinned by the two golden cases above, so the suite remains
        trustworthy without PyYAML.
        """
        try:
            import yaml  # noqa: F401
        except ImportError:
            sys.stderr.write(
                "NOTE: PyYAML is not installed, so the PyYAML/fallback parser "
                "cross-check did not run. The fallback loader is still pinned "
                "by test_golden_* above. To enable the cross-check: "
                "pip install pyyaml\n"
            )
            return
        for wf in W.WORKFLOW_NAMES:
            text = W.raw_workflow_text(wf)
            via_pyyaml = W.load_workflow_text(text, prefer_pyyaml=True)
            via_fallback = W.load_workflow_text(text, prefer_pyyaml=False)
            self.assertEqual(
                via_pyyaml, via_fallback,
                "PyYAML and the stdlib fallback loader disagree on %s. The "
                "active parser is whichever runs in CI, so a disagreement "
                "means one of the two is parsing a workflow wrongly." % wf,
            )

    def test_parser_reports_a_coverage_floor(self):
        """The five workflows must actually yield jobs, steps and crons."""
        total_steps = sum(len(list(W.iter_all_steps(wf))) for wf in W.WORKFLOW_NAMES)
        total_runs = sum(len(list(W.iter_run_blocks(wf))) for wf in W.WORKFLOW_NAMES)
        self.assertGreaterEqual(total_steps, 40)
        self.assertGreaterEqual(total_runs, 40)
        crons = sum(len(list(W.iter_schedule_crons(wf))) for wf in W.WORKFLOW_NAMES)
        self.assertGreaterEqual(crons, 4)
        print("parser: %s; %d steps, %d run: blocks, %d schedules"
              % (W.parser_name(), total_steps, total_runs, crons))


# ==========================================================================
# Check 10 -- DEFECT 1 (P0): docs/academic is never deployed to Pages
# ==========================================================================

PAGES_DEPLOY_RE = re.compile(r"wrangler\s+pages\s+deploy")


class AcademicPagesExposureTests(WorkflowLintBase):
    """The owner's roll number must never reach a public origin.

    The leak was `cp -r docs/academic "$stage_dir/academic"` immediately
    before `npx wrangler pages deploy "$stage_dir" --project-name
    daily-digest` in academic-result.yml. `daily-digest.example.pages.dev` serves
    every unauthenticated client; the `digest-auth` Worker that used to sit in
    front was a PROXY and was bypassed by hitting the origin directly. That
    Worker has since been deleted, and `functions/_middleware.js` now enforces
    Basic auth at the origin itself.
    Exposure was
    intermittent only because each `wrangler pages deploy` REPLACES the whole
    deployment and daily-news.yml re-deploys `docs/digests` + `docs/index.html`
    alone -- so the directory was wiped by every digest run and re-exposed by
    every academic run, on a trivially guessable filename.

    The check is on the SHAPE, not on the wording: any run block that deploys
    to Pages and mentions `docs/academic` is an offender, however it is
    spelled, and whichever step is used to do it.
    """

    def _deploy_blocks(self):
        """[(workflow, job, step, run)] for every run block that deploys."""
        out = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, _st in W.iter_run_blocks(wf):
                if PAGES_DEPLOY_RE.search(run):
                    out.append((wf, job, step, run))
        return out

    def test_no_workflow_deploys_docs_academic_to_pages(self):
        offenders = []
        for wf, job, step, run in self._deploy_blocks():
            if "docs/academic" in run:
                offenders.append(
                    "%s / %s / %r deploys to Cloudflare Pages AND references "
                    "docs/academic. The rendered academic page carries the "
                    "owner's roll number, the result date and a link to the "
                    "official TU result PDF; the Pages origin serves it to any "
                    "unauthenticated client." % (wf, job, step))
        self.assertEqual(
            [], offenders,
            "docs/academic reached a public Pages deploy:\n  " + "\n  ".join(offenders))
        # The rule must not go vacuous: the digest site really does deploy.
        self.assertGreaterEqual(
            len(self._deploy_blocks()), 1,
            "no workflow deploys to Cloudflare Pages any more, so this check "
            "is inspecting nothing")
        print("check 10: %d Pages deploy block(s), none references docs/academic"
              % len(self._deploy_blocks()))

    def test_academic_result_never_calls_wrangler_at_all(self):
        calls = []
        for job, step, run, _st in W.iter_run_blocks("academic-result.yml"):
            if "wrangler" in run:
                calls.append("%s / %r mentions wrangler" % (job, step))
        self.assertEqual(
            [], calls,
            "academic-result.yml must not be able to deploy anywhere -- the "
            "roll number stays private:\n  " + "\n  ".join(calls))

    def test_the_digest_stage_never_copies_docs_academic(self):
        """The wiping deploy must stay a wipe, not become a republish."""
        offenders = []
        for job, step, run, _st in W.iter_run_blocks("daily-news.yml"):
            if PAGES_DEPLOY_RE.search(run) and "docs/academic" in run:
                offenders.append(step)
        self.assertEqual([], offenders)

    def test_the_pre_fix_deploy_shape_is_rejected(self):
        """REGRESSION PROOF: the check above can actually fail.

        The bad case is the literal pre-fix academic deploy, built in memory.
        No workflow file is edited.
        """
        pre_fix = (
            '          stage_dir="$(mktemp -d)"\n'
            '          cp -r docs/academic "$stage_dir/academic"\n'
            '          npx wrangler pages deploy "$stage_dir" \\\n'
            '            --project-name daily-digest \\\n'
            '            --branch master\n')
        self.assertTrue(PAGES_DEPLOY_RE.search(pre_fix),
                        "sanity: the pre-fix snippet must look like a deploy")
        self.assertIn("docs/academic", pre_fix)
        self.assertIn("docs/academic", pre_fix,
                      "sanity: the check keys on docs/academic appearing in a "
                      "Pages-deploy block, which is what the pre-fix step does")


# ==========================================================================
# Check 11 -- DEFECT 4: the ntfy push carries the freshness line
# ==========================================================================

#: The health states FROZEN CONTRACT C defines, plus the payloads that
#: produce them. Every one is exercised, because a drift guard that only
#: checks the `ok` path cannot see a changed qualifier.
CUTOFF_HEALTH_CASES = (
    ("ok", {"state": "ok", "by_category": {}}),
    ("partial", {"state": "partial", "by_category": {}}),
    ("degraded", {"state": "degraded", "by_category": {},
                  "critical": {"hosts_down": ["tu.edu.np"]}}),
    ("empty", {"state": "empty", "by_category": {}}),
    ("unknown", {"state": "whatever", "by_category": {}}),
    ("absent", None),
)


def _load_render_digest():
    """Import scripts/render_digest.py by path, once, without side effects."""
    import importlib.util
    path = W.REPO_ROOT / "scripts" / "render_digest.py"
    spec = importlib.util.spec_from_file_location("_lint_render_digest", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NtfyCutoffDriftTests(WorkflowLintBase):
    """The push may not silently drop the freshness line or its qualifiers.

    The ntfy heredoc in daily-news.yml contained ZERO occurrences of
    `cutoff` or "Information checked through", while
    scripts/render_digest.py called `cutoff_line()` and
    scripts/build_email.py carried the line. So the push dropped the claim and
    dropped its qualifiers with it -- the degraded suffix ("TU sources were
    unreachable" as it read then, "some sources were unreachable" after the
    cause-agnostic fix), "incomplete: some sources returned nothing usable" --
    which is the same cross-surface divergence class the owner already suffered
    once.

    These tests execute the REAL step body with DRY_RUN=true (which prints the
    payload and returns before any network call) and compare the bytes of the
    body the owner would actually receive against render_digest's own
    `cutoff_line`. A changed qualifier literal in the workflow fails here
    instead of shipping.
    """

    WORKFLOW = "daily-news.yml"
    STEP = "Publish to ntfy (primary)"
    CUTOFF = "2026-09-30 06:00"

    # -- the offline harness ----------------------------------------------
    def _run_ntfy(self, tmp, health):
        """exec the ntfy step body with DRY_RUN=true; return its stdout."""
        import contextlib
        import io
        import json as _json

        for job, step, run, _st in W.iter_run_blocks(self.WORKFLOW):
            if step == self.STEP:
                body = next(b for _t, b in heredoc_bodies(run))
                break
        else:  # pragma: no cover - the step is committed
            self.fail("no run: block for %r" % self.STEP)

        os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
        validated = {"stories": [], "data_cutoff_npt": self.CUTOFF,
                     "generated_at_npt": "2026-09-30 06:01"}
        if health is not None:
            validated["source_health"] = health
        with open(os.path.join(tmp, "state", "stories.validated.json"),
                  "w", encoding="utf-8") as fh:
            _json.dump(validated, fh)

        saved = dict(os.environ)
        cwd = os.getcwd()
        buf = io.StringIO()
        os.environ.update({
            "GITHUB_OUTPUT": os.path.join(tmp, "gh_output.txt"),
            "GITHUB_STEP_SUMMARY": os.path.join(tmp, "gh_summary.md"),
            "NTFY_TOPIC": "lint-topic",
            "NTFY_SERVER": "https://ntfy.invalid",
            "DRY_RUN": "true",
            "TODAY": "2026-09-30",
            "DIGEST_TIME": "morning",
        })
        try:
            os.chdir(tmp)
            with contextlib.redirect_stdout(buf):
                exec(compile(body, "<ntfy>", "exec"), {"__name__": "wf_step"})
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        return buf.getvalue()

    @staticmethod
    def _payload_message(stdout):
        """The `message` of the payload the dry run printed."""
        import json as _json
        start = stdout.index("\n{\n") + 1
        end = stdout.rindex("}")
        payload = _json.loads(stdout[start:end + 1])
        return payload["message"], payload

    def _message_for(self, health):
        with tempfile.TemporaryDirectory() as tmp:
            return self._payload_message(self._run_ntfy(tmp, health))[0]

    # -- the checks --------------------------------------------------------
    def test_the_push_body_carries_render_digests_cutoff_line(self):
        rd = _load_render_digest()
        checked = 0
        failures = []
        for name, health in CUTOFF_HEALTH_CASES:
            expected = rd.cutoff_line(self.CUTOFF, health)
            actual = self._message_for(health)
            checked += 1
            if expected and expected not in actual:
                failures.append("%s: the push is missing %r" % (name, expected))
            if not expected and "Information checked through" in actual:
                failures.append("%s: the push invented a freshness line %r"
                                % (name, actual))
        self.assertEqual(
            [], failures,
            "the ntfy push and scripts/render_digest.py disagree on the "
            "freshness line (FROZEN CONTRACT C):\n  " + "\n  ".join(failures))
        self.assertGreaterEqual(checked, len(CUTOFF_HEALTH_CASES))
        print("check 11: freshness line matched render_digest on %d health "
              "state(s)" % checked)

    def test_the_qualifier_strings_are_the_ones_render_digest_uses(self):
        """Pin the qualifiers themselves, so a renamed suffix cannot pass."""
        rd = _load_render_digest()
        for state, health in CUTOFF_HEALTH_CASES:
            if health is None:
                continue
            message = self._message_for(health)
            suffix = rd.CUTOFF_SUFFIX.get(state, "")
            expected = rd.cutoff_line(self.CUTOFF, health)
            if suffix:
                self.assertIn(
                    suffix, message,
                    "the push is missing the %r qualifier %r for source_health "
                    "state %r. FROZEN CONTRACT C: the freshness line and its "
                    "qualifier travel together -- dropping the qualifier while "
                    "keeping the timestamp is the exact lie the renderer "
                    "exists to remove." % (state, suffix, state))
            if state == "unknown":
                self.assertIn(expected, message)
                self.assertNotIn(
                    "Information checked through", message,
                    "the `unknown` state DROPS the timestamp entirely: claiming "
                    "'checked through T' when no source was confirmed reachable "
                    "is the exact lie FROZEN CONTRACT C exists to remove")
            else:
                self.assertNotIn(
                    "Freshness unverified", message,
                    "the %r state must not claim the freshness is unverified" % state)

    def test_the_honesty_banner_still_leads_and_the_freshness_line_never_is_dropped(self):
        """The banner, when there is one, stays FIRST; the freshness line under it.

        Both are outside every byte budget in the body, so neither can be
        squeezed out by a busy day. A healthy `ok` run has no banner at all --
        `HEALTH_BANNER` deliberately carries no `ok` entry -- so there the
        freshness line is legitimately the first line.
        """
        rd = _load_render_digest()
        for name, health in CUTOFF_HEALTH_CASES:
            if health is None:
                continue
            message = self._message_for(health)
            line = rd.cutoff_line(self.CUTOFF, health)
            if not line:
                continue
            self.assertIn(line, message, "%s: freshness line missing" % name)
            blocks = message.split("\n\n")
            self.assertEqual(
                line, blocks[1] if blocks[0].startswith("⚠️") else blocks[0],
                "%s: the freshness line must sit immediately under the honesty "
                "banner, so a reader sees the caveat before the content" % name)
            if blocks[0].startswith("⚠️"):
                self.assertIn(
                    rd.HEALTH_BANNER.get(rd.health_state(health)), blocks[0],
                    "%s: the honesty banner must remain the first block of the "
                    "push, and it must be render_digest's own literal -- an "
                    "outage cannot be scrolled past" % name)
            else:
                self.assertEqual(
                    "ok", rd.health_state(health),
                    "%s: only a healthy `ok` run may open with the freshness "
                    "line instead of the banner" % name)

    def test_the_push_stays_inside_the_ntfy_byte_budget(self):
        for name, health in CUTOFF_HEALTH_CASES:
            message, _payload = self._payload_message(
                self._run_ntfy(
                    tempfile.mkdtemp(prefix="ntfy-budget-"), health))
            self.assertLessEqual(
                len(message.encode("utf-8")), 4096,
                "%s: the ntfy body is %d bytes, over the 4096 budget"
                % (name, len(message.encode("utf-8"))))

    def test_a_heredoc_without_a_cutoff_literal_is_detected(self):
        """REGRESSION PROOF: this drift guard can fail.

        The pre-fix heredoc shape is asserted against the same rule the real
        checks use, in memory. No workflow file is edited.
        """
        pre_fix_ntfy_heredoc = (
            "          def banner_md(health, raw_notes):\n"
            "              return ''\n"
            "          banner = banner_md(source_health, notes)\n")
        self.assertNotIn("cutoff", pre_fix_ntfy_heredoc)
        self.assertNotIn("Information checked through", pre_fix_ntfy_heredoc)
        # And the real, committed heredoc must have BOTH -- the exact thing
        # that was absent before.
        for job, step, run, _st in W.iter_run_blocks(self.WORKFLOW):
            if step == self.STEP:
                self.assertIn("Information checked through", run)
                self.assertIn("CUTOFF_SUFFIX", run)
                self.assertIn("data_cutoff_npt", run)
                break
        else:  # pragma: no cover - the step is committed
            self.fail("no run: block for %r" % self.STEP)


# ==========================================================================
# Check 12 -- DEFECT 5: a degraded run fails loudly, and its payload parses
# ==========================================================================

ACADEMIC = "academic-result.yml"
DEGRADED_STEP = "Record degraded run"
ASSERT_STEP = "Assert academic delivery"
RENDER_STEP = "Render academic result page"


class AcademicDegradedChannelTests(WorkflowLintBase):
    """The FAILURE channel, which was the one that had gone silent.

    Two defects, one chain. "Record degraded run" wrote the degraded payload
    with `json.dump(..., indent=2)` -- a pretty-printed single object -- while
    `payload_io.read_payloads` is JSON-Lines-or-list and RAISES on that shape
    (the file starts with `{`, so line 1 is `{`, which is not JSON). So on a
    degraded run: the ntfy step caught the error and recorded 0, the render
    step's count raised, `degraded` became -1, and `delivery_assert` -- which
    treated `emitted == 0` as "a silent tick, nothing to assert" -- exited 0.

    The net effect was that the one run which must be loudest was the one run
    that published nothing AND went green. Every test here EXECUTES the real
    step bodies; inspection proved nothing about this class of defect.
    """

    # -- a minimal offline harness, local so this file needs no sibling ---
    def setUp(self):
        import shutil
        self._tmp = tempfile.mkdtemp(prefix="degraded-step-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        # The steps import `payload_io` from `./scripts`, exactly as they do
        # on the runner. Copying the real (stdlib-only) module in is more
        # honest than stubbing the reader the step is supposed to agree with.
        os.makedirs(os.path.join(self._tmp, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(self._tmp, "state"), exist_ok=True)
        shutil.copyfile(str(W.REPO_ROOT / "scripts" / "payload_io.py"),
                        os.path.join(self._tmp, "scripts", "payload_io.py"))
        for name in ("gh_output.txt", "gh_summary.md"):
            with open(os.path.join(self._tmp, name), "w", encoding="utf-8") as fh:
                fh.write("")

    def _body(self, step_name):
        for _job, step, step_dict in W.iter_all_steps(ACADEMIC):
            if step == step_name and "run" in step_dict:
                for _tag, body in heredoc_bodies(step_dict["run"]):
                    return body
        self.fail("no python heredoc in %s / %r" % (ACADEMIC, step_name))

    def _run(self, step_name, env):
        """exec a step body against the temp dir; return (exit_code, stdout)."""
        import contextlib
        import io as _io
        saved = dict(os.environ)
        cwd = os.getcwd()
        buf = _io.StringIO()
        os.environ.update({
            "GITHUB_OUTPUT": os.path.join(self._tmp, "gh_output.txt"),
            "GITHUB_STEP_SUMMARY": os.path.join(self._tmp, "gh_summary.md"),
        })
        os.environ.update(env)
        code = 0
        try:
            os.chdir(self._tmp)
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(self._body(step_name), "<%s>" % step_name, "exec"),
                         {"__name__": "wf_step"})
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        return code, buf.getvalue()

    def _read(self, rel):
        import json as _json
        with open(os.path.join(self._tmp, rel), encoding="utf-8") as fh:
            return _json.load(fh)

    def _write_ntfy(self, delivered, titles=("fake",)):
        with open(os.path.join(self._tmp, "state", "academic_ntfy.json"),
                  "w", encoding="utf-8") as fh:
            import json as _json
            _json.dump({"delivered": delivered, "titles": list(titles),
                        "truncated": []}, fh)

    # -- the payload contract ---------------------------------------------
    def test_the_degraded_payload_is_json_lines_the_reader_accepts(self):
        code, out = self._run(DEGRADED_STEP, {"MONITOR_EXIT": "2"})
        self.assertEqual(0, code, out)
        path = os.path.join(self._tmp, "state", "academic_degraded_payload.json")
        self.assertTrue(os.path.isfile(path), out)
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        lines = [ln for ln in raw.split("\n") if ln.strip()]
        self.assertEqual(
            1, len(lines),
            "the degraded payload must be ONE compact JSON object on ONE line "
            "(JSON-Lines), got %d non-blank line(s). `json.dump(indent=2)` is "
            "the shape that made read_payloads raise on every degraded run."
            % len(lines))
        import json as _json
        payloads = [_json.loads(ln) for ln in lines]
        self.assertEqual(1, len(payloads), "expected exactly one payload")
        self.assertEqual("degraded", payloads[0]["kind"])
        self.assertNotIn("\n  ", raw, "the payload must be compact, not indented")

    def test_the_payload_parses_with_the_real_reader(self):
        sys.path.insert(0, os.path.join(self._tmp, "scripts"))
        import importlib
        payload_io = importlib.import_module("payload_io")
        self.assertIsNot(payload_io, None)
        self._run(DEGRADED_STEP, {"MONITOR_EXIT": "3"})
        payloads = payload_io.read_payloads(
            os.path.join(self._tmp, "state", "academic_degraded_payload.json"))
        self.assertEqual(1, len(payloads))
        self.assertEqual("degraded", payloads[0]["kind"])
        self.assertEqual(
            1, payload_io.count_payloads(
                os.path.join(self._tmp, "state", "academic_degraded_payload.json")))

    def test_a_healthy_run_writes_no_degraded_payload(self):
        code, out = self._run(DEGRADED_STEP, {"MONITOR_EXIT": "0"})
        self.assertEqual(0, code, out)
        self.assertIn("monitor completed normally", out)
        self.assertFalse(
            os.path.isfile(
                os.path.join(self._tmp, "state", "academic_degraded_payload.json")),
            "a healthy run must not leave a degraded payload behind")

    # -- the assert --------------------------------------------------------
    def test_a_degraded_run_with_nothing_delivered_exits_non_zero(self):
        self._run(DEGRADED_STEP, {"MONITOR_EXIT": "2"})
        self._write_ntfy(0, [])
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "0", "DELIVERED": "0", "PAGE_OK": "false",
            "DEGRADED": "true", "MONITOR_EXIT": "2", "DRY_RUN": "false"})
        self.assertEqual(
            1, code,
            "a degraded run that rendered no failure page and confirmed no "
            "notification MUST exit 1. It exited %d. The old assert saw "
            "emitted=0, called it a silent tick, and exited 0 -- so the one "
            "run that must be loudest went green.\n%s" % (code, out))
        self.assertIn("::error::", out)
        self.assertEqual("degraded_page_render_failed",
                         self._read("state/academic_delivery.json")["reason"])

    def test_a_degraded_run_whose_ntfy_delivered_nothing_exits_non_zero(self):
        self._run(DEGRADED_STEP, {"MONITOR_EXIT": "2"})
        self._write_ntfy(0, [])
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "0", "DELIVERED": "0", "PAGE_OK": "true",
            "DEGRADED": "true", "MONITOR_EXIT": "2", "DRY_RUN": "false"})
        self.assertEqual(
            1, code,
            "the failure page rendered but the owner was told nothing; that is "
            "still silence.\n%s" % out)
        self.assertEqual("ntfy_incomplete",
                         self._read("state/academic_delivery.json")["reason"])

    def test_a_degraded_run_with_no_parsable_payload_exits_non_zero(self):
        """The pre-fix file shape on disk must still turn the run red."""
        with open(os.path.join(self._tmp, "state",
                               "academic_degraded_payload.json"), "w",
                  encoding="utf-8") as fh:
            fh.write('{\n  "kind": "degraded"\n}\n')
        self._write_ntfy(0, [])
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "0", "DELIVERED": "0", "PAGE_OK": "true",
            "DEGRADED": "true", "MONITOR_EXIT": "2", "DRY_RUN": "false"})
        self.assertEqual(1, code,
                         "an unreadable degraded payload must be a failure, "
                         "not a shrug.\n%s" % out)
        self.assertEqual("degraded_payload_missing",
                         self._read("state/academic_delivery.json")["reason"])

    def test_a_degraded_run_is_red_even_if_the_degraded_step_itself_crashed(self):
        """`Record degraded run` has continue-on-error, so its output can vanish.

        MONITOR_EXIT alone must be enough to keep the run from going green.
        """
        self._write_ntfy(0, [])
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "0", "DELIVERED": "0", "PAGE_OK": "",
            "DEGRADED": "", "MONITOR_EXIT": "2", "DRY_RUN": "false"})
        self.assertEqual(1, code,
                         "DEGRADED empty + MONITOR_EXIT 2 is still a degraded "
                         "run, and it must still fail loudly.\n%s" % out)

    def test_a_healthy_delivery_is_still_green(self):
        self._write_ntfy(1)
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "1", "DELIVERED": "1", "PAGE_OK": "true",
            "DEGRADED": "false", "MONITOR_EXIT": "0", "DRY_RUN": "false"})
        self.assertEqual(0, code, out)
        record = self._read("state/academic_delivery.json")
        self.assertEqual("delivered", record["reason"])
        self.assertTrue(record["ok"])
        self.assertFalse(record["degraded"])

    def test_a_healthy_silent_tick_is_still_green(self):
        """The exemption is narrow and must stay: a silent tick promised nothing."""
        self._write_ntfy(0, [])
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "0", "DELIVERED": "0", "PAGE_OK": "",
            "DEGRADED": "false", "MONITOR_EXIT": "0", "DRY_RUN": "false"})
        self.assertEqual(0, code, out)
        record = self._read("state/academic_delivery.json")
        self.assertEqual("nothing_emitted", record["reason"])
        self.assertTrue(record["ok"])

    def test_a_healthy_partial_delivery_keeps_its_exact_operational_string(self):
        """The pre-existing operator-facing wording is unchanged.

        `tests/test_payload_io.py` pins `::error::academic delivery incomplete:
        N payload(s) emitted, M confirmed by ntfy` for the ordinary case, and
        that test is not mine to touch. The degraded branch adds a NEW message
        rather than re-wording the old one, so a run an operator already knows
        how to read still reads the same.
        """
        self._write_ntfy(1, ["Sem 1"])
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "2", "DELIVERED": "1", "PAGE_OK": "true",
            "DEGRADED": "false", "MONITOR_EXIT": "0", "DRY_RUN": "false"})
        self.assertEqual(1, code, out)
        self.assertIn("::error::academic delivery incomplete: 2 payload(s) "
                      "emitted, 1 confirmed by ntfy", out)
        self.assertEqual("ntfy_incomplete",
                         self._read("state/academic_delivery.json")["reason"])

    def test_a_dry_run_degraded_is_not_held_to_a_network_delivery(self):
        self._run(DEGRADED_STEP, {"MONITOR_EXIT": "2"})
        self._write_ntfy(0, [])
        code, out = self._run(ASSERT_STEP, {
            "EMITTED": "0", "DELIVERED": "0", "PAGE_OK": "true",
            "DEGRADED": "true", "MONITOR_EXIT": "2", "DRY_RUN": "true"})
        self.assertEqual(0, code, out)
        self.assertEqual("degraded_delivered",
                         self._read("state/academic_delivery.json")["reason"])


# ==========================================================================
# Check 13 -- DEFECT 3: the NPT offset is +05:45, not +00:00
# ==========================================================================


class KathmanduOffsetTests(WorkflowLintBase):
    """`at_npt` must be Nepal time.

    `KATHMANDU = timezone.utc.utcoffset(None)` evaluates to `timedelta(0)` --
    an offset of ZERO in a field named `at_npt`. Nepal is a fixed UTC+05:45
    with no DST, so every timestamp that step wrote was 5h45m behind the wall
    clock it claimed to name (observed: `2026-09-30T11:28:45+0000` for a true
    `17:13:45+0545`).

    The check EXECUTES the step body and compares its own output against
    `check_academic_result.KATHMANDU_OFFSET`, which is the constant the owner
    already trusts. A second literal is not invented here.
    """

    def _offset_seconds_from_checker(self):
        import importlib.util
        path = W.REPO_ROOT / "scripts" / "check_academic_result.py"
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        m = re.search(r"^KATHMANDU_OFFSET\s*=\s*(.+)$", source, re.M)
        self.assertIsNotNone(m, "check_academic_result.KATHMANDU_OFFSET is gone")
        return eval(m.group(1).split("#")[0].strip(), {"__builtins__": {}}, {})

    def _write_ntfy(self, tmp, delivered, titles=("fake",)):
        import json as _json
        os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
        with open(os.path.join(tmp, "state", "academic_ntfy.json"),
                  "w", encoding="utf-8") as fh:
            _json.dump({"delivered": delivered, "titles": list(titles),
                        "truncated": []}, fh)

    def test_the_assert_writes_at_npt_in_plus_0545(self):
        import datetime
        import json as _json
        import shutil
        expected = self._offset_seconds_from_checker()
        self.assertEqual(
            5 * 3600 + 45 * 60, expected,
            "check_academic_result.KATHMANDU_OFFSET changed -- if Nepal's "
            "offset really moved, fix the workflow and this test together")

        tmp = tempfile.mkdtemp(prefix="npt-offset-")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(str(W.REPO_ROOT / "scripts"),
                        os.path.join(tmp, "scripts"))
        for name in ("gh_output.txt",):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write("")
        self._write_ntfy(tmp, 1)

        body = None
        for _job, step, step_dict in W.iter_all_steps(ACADEMIC):
            if step == ASSERT_STEP and "run" in step_dict:
                body = next(b for _t, b in heredoc_bodies(step_dict["run"]))
        self.assertIsNotNone(body, "no python heredoc for %r" % ASSERT_STEP)

        import contextlib
        import io as _io
        saved = dict(os.environ)
        cwd = os.getcwd()
        buf = _io.StringIO()
        before = datetime.datetime.now(datetime.timezone.utc)
        os.environ.update({
            "GITHUB_OUTPUT": os.path.join(tmp, "gh_output.txt"),
            "GITHUB_STEP_SUMMARY": os.path.join(tmp, "gh_summary.md"),
            "EMITTED": "1", "DELIVERED": "1", "PAGE_OK": "true",
            "DEGRADED": "false", "MONITOR_EXIT": "0", "DRY_RUN": "false",
        })
        try:
            os.chdir(tmp)
            with contextlib.redirect_stdout(buf):
                exec(compile(body, "<assert>", "exec"), {"__name__": "wf_step"})
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        after = datetime.datetime.now(datetime.timezone.utc)

        with open(os.path.join(tmp, "state", "academic_delivery.json"),
                  encoding="utf-8") as fh:
            record = _json.load(fh)
        at_npt = record["at_npt"]
        self.assertTrue(
            at_npt.endswith("+0545"),
            "at_npt is %r. Nepal is UTC+05:45 with no DST, so this field must "
            "carry +0545; `timezone.utc.utcoffset(None)` gave +0000 and named "
            "it NPT anyway." % at_npt)
        stamp = datetime.datetime.strptime(at_npt, "%Y-%m-%dT%H:%M:%S%z")
        self.assertEqual(
            expected, int(stamp.utcoffset().total_seconds()),
            "at_npt's offset disagrees with check_academic_result.KATHMANDU_OFFSET")
        # And it must be the CURRENT instant, with the Nepal wall clock applied.
        # `+05:45` above already proves the offset is right; this proves the
        # value is not stale and not the un-shifted UTC time in disguise.
        lo = before.timestamp() - 60
        hi = after.timestamp() + 60
        self.assertTrue(
            lo <= stamp.timestamp() <= hi,
            "at_npt %s is not the current instant (expected between %s and %s)"
            % (at_npt, before.isoformat(), after.isoformat()))
        self.assertEqual(
            20700, int(stamp.utcoffset().total_seconds()),
            "sanity: the fixed Nepal offset is 20700 seconds (5h45m)")
        print("check 13: at_npt=%s matches KATHMANDU_OFFSET=%d and the wall clock"
              % (at_npt, expected))

    def test_the_workflow_does_not_derive_npt_from_utc_utcoffset(self):
        offenders = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, _st in W.iter_run_blocks(wf):
                # Comments are exempt: the DEFECT 3 comment in this step quotes
                # the broken expression in order to name it. Only EXECUTABLE
                # lines are judged.
                code = [ln for ln in run.split("\n")
                        if ln.strip() and not ln.strip().startswith("#")]
                code = "\n".join(code)
                if "utcoffset(None)" in code and "KATHMANDU" in code:
                    offenders.append("%s / %r" % (wf, step))
        self.assertEqual(
            [], offenders,
            "`timezone.utc.utcoffset(None)` is timedelta(0). Deriving a field "
            "named at_npt from it is how the offset was 5h45m wrong:\n  "
            + "\n  ".join(offenders))


# ==========================================================================
# Check 14 -- DEFECT 2: no checkout leaves GITHUB_TOKEN in the workspace
# ==========================================================================

PERSIST_RE = re.compile(r"persist-credentials:\s*(\S+)")


class CheckoutCredentialTests(WorkflowLintBase):
    """The `--auto` agent must not find a pushable token in the workspace.

    `actions/checkout` defaults to `persist-credentials: true`, which writes
    an `http.https://github.com/.extraheader` entry carrying GITHUB_TOKEN into
    `.git/config` and leaves it there for the whole job. The job runs
    `opencode run --auto` -- auto-approved, no tool allowlist -- against text
    harvested from RSS feeds, with `contents: write`.

    The split this file pins:
      * EVERY checkout sets `persist-credentials: false`, so the token is
        never on disk for any step that follows it;
      * EVERY step that genuinely commits binds `GITHUB_TOKEN` in its own
        `env:` and injects it with `git -c` on its own pull/push, so the four
        `[skip ci]` state commits keep working without the workspace ever
        holding a credential.
    """

    def _checkouts(self):
        out = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, st in W.iter_all_steps(wf):
                if "actions/checkout" in (st.get("uses") or ""):
                    out.append((wf, job, step, st))
        return out

    def test_every_checkout_does_not_persist_credentials(self):
        offenders = []
        checkouts = self._checkouts()
        for wf, job, step, st in checkouts:
            value = (st.get("with") or {}).get("persist-credentials")
            if str(value).lower() != "false":
                offenders.append(
                    "%s / %s / %r: persist-credentials=%r. Unset means TRUE, "
                    "which parks GITHUB_TOKEN in .git/config for every later "
                    "step in the job -- including any `opencode run --auto` "
                    "step fed by untrusted RSS text." % (wf, job, step, value))
        self.assertEqual(
            [], offenders,
            "checkouts that persist credentials:\n  " + "\n  ".join(offenders))
        self.assertGreaterEqual(
            len(checkouts), 8,
            "expected at least 8 checkouts across the %d workflows, found %d. "
            "The floor exists so this check cannot pass by finding nothing: "
            "lower it only when a job merge genuinely removed a checkout "
            "(drift-canary.yml went from four jobs to one on 2026-10-19, and "
            "three duplicate job-level checkouts went with them)."
            % (len(W.WORKFLOW_NAMES), len(checkouts)))
        print("check 14: %d checkouts, none persists credentials" % len(checkouts))

    def test_every_commit_step_injects_its_own_token(self):
        commit_steps = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, st in W.iter_run_blocks(wf):
                if "git commit" in run:
                    commit_steps.append((wf, job, step, run, st))
        self.assertGreaterEqual(
            len(commit_steps), 4,
            "expected the four `[skip ci]` state commits, found %d"
            % len(commit_steps))
        offenders = []
        for wf, job, step, run, st in commit_steps:
            env_token = (st.get("env") or {}).get("GITHUB_TOKEN") or ""
            if "secrets.GITHUB_TOKEN" not in env_token:
                offenders.append(
                    "%s / %s / %r: no GITHUB_TOKEN in its own env:. With "
                    "persist-credentials: false this state commit cannot "
                    "authenticate, so it would silently stop pushing."
                    % (wf, job, step))
            if "auth=(" not in run:
                offenders.append("%s / %s / %r: does not build the `git -c` "
                                 "auth argument" % (wf, job, step))
            # A bare `git push`/`git pull` in a commit step would go out with
            # whatever credentials happened to be around -- which is exactly
            # what persist-credentials: false removes.
            for line in run.split("\n"):
                stripped = line.strip()
                if stripped.startswith("git ") and '"${auth[@]}"' not in stripped \
                        and re.match(r"git (push|pull|fetch|remote|clone)\b", stripped):
                    offenders.append(
                        "%s / %s / %r: unauthenticated network command %r -- it "
                        "must use the `git -c` auth argument"
                        % (wf, job, step, stripped))
        self.assertEqual(
            [], offenders,
            "state commits that cannot authenticate after "
            "persist-credentials: false:\n  " + "\n  ".join(offenders))
        print("check 14: %d state commit steps each inject their own token"
              % len(commit_steps))

    def test_the_auto_agent_job_does_not_persist_credentials(self):
        """Targeted: the job that actually runs `opencode run --auto`."""
        agents = []
        for wf in W.WORKFLOW_NAMES:
            for job, step, run, st in W.iter_run_blocks(wf):
                if "opencode run" in run and "--help" not in run:
                    agents.append((wf, job, step, "--auto" in run))
        self.assertTrue(agents, "no `opencode run` step found; the check is blind")
        for wf, job, step, has_auto in agents:
            self.assertTrue(
                has_auto,
                "%s / %s / %r runs `opencode run` without --auto; if that is "
                "deliberate, this test needs to be told why" % (wf, job, step))
            checkout = None
            for _wf, cjob, _cstep, cst in self._checkouts():
                if _wf == wf and cjob == job:
                    checkout = cst
            self.assertIsNotNone(
                checkout, "%s / %s has a checkout? the agent job has none"
                % (wf, job))
            self.assertEqual(
                "false", str((checkout.get("with") or {}).get(
                    "persist-credentials")).lower(),
                "%s / %s runs an auto-approved agent and must not leave "
                "GITHUB_TOKEN in .git/config" % (wf, job))
        print("check 14: %d auto-approved agent job(s), credentials not persisted"
              % len(agents))

    def test_the_pre_fix_checkout_shape_is_rejected(self):
        """REGRESSION PROOF: the rule above can actually fail.

        The pre-fix shape -- `uses: actions/checkout` with no
        `persist-credentials`, followed by a bare `git push` -- is judged by
        the same helper in memory. No workflow file is edited.
        """
        pre_fix = {"uses": "actions/checkout@v4",
                   "with": {"token": "${{ secrets.GITHUB_TOKEN }}",
                            "fetch-depth": 0}}
        value = (pre_fix.get("with") or {}).get("persist-credentials")
        self.assertNotEqual(
            "false", str(value).lower(),
            "the pre-fix checkout had no persist-credentials, which means "
            "TRUE -- exactly what test_every_checkout_does_not_persist_"
            "credentials must reject")
        pre_fix_push = "          git push"
        self.assertNotIn('"${auth[@]}"', pre_fix_push)
        self.assertIsNotNone(
            re.match(r"git (push|pull)\b", pre_fix_push.strip()),
            "sanity: the pre-fix push is a bare network command, which "
            "test_every_commit_step_injects_its_own_token must reject")


# ==========================================================================
# Check 15 -- DEFECT 6: the degraded fallback is reachable ONLY when the
#             monitor emitted nothing, and page_ok is written exactly once
# ==========================================================================
#
# THE DEFECT (five consecutive red hourly runs, 2026-10-02). "Render academic
# result page" had an `if count:` with NO `else:`. On a healthy run the render
# SUCCEEDED (`render_academic: rendered 5 page(s) carrying 5 payload(s)`),
# `finish(rc == 0)` wrote page_ok=true and RETURNED -- it only calls
# `sys.exit(1)` on failure -- and control FELL THROUGH into the
# degraded-payload fallback. `payload_io.count_payloads()` returns -1 for a
# missing file and the degraded payload file only exists on degraded runs, so
# `degraded = -1`, `degraded <= 0` was true, and `finish(False)` overwrote the
# flag file and appended a SECOND `page_ok=false`. GitHub takes the LAST value
# for a repeated output key, so `Assert academic delivery` correctly read
# page_ok=false and failed the run with
#     "academic page render failed despite 5 payload(s)"
# The assert is the messenger. It is correct and it is not weakened here.
#
# WHAT IS ASSERTED, and WHY IT IS NOT LINE-NUMBERED. A line-numbered assertion
# would drift on the next edit and go quietly vacuous. Instead the check reads
# the heredoc body the PARSER produced and walks its AST:
#   * the `if count:` guard must have an `else:` branch, and
#   * every reference to the degraded payload file must live inside that
#     `else:` branch, so a truthy `count` cannot reach it, and
#   * `page_ok=` must be written from exactly one place, and every write to
#     $GITHUB_OUTPUT must be inside the `finish()` helper.
# The behavioural test then EXECUTES the real step body against the real
# payload_io/render_academic modules with five payloads, because "the AST looks
# right" proved nothing about this class of defect.
#
# REPRODUCING THE RED ITSELF: set WORKFLOW_LINT_ACADEMIC_OVERRIDE to the path
# of another academic-result.yml and these checks judge THAT file instead of the
# committed one. It is how the mutation proof below was seen failing, and it is
# left in place so the next person can re-run the pair in one command. The
# override is printed as a NOTE on stderr, so it can never be mistaken for CI.

#: Name of the degraded-payload file the fallback must never read on a
#: non-empty run. Spelled once; the check and the comment above agree.
DEGRADED_FALLBACK_FILE = "state/academic_degraded_payload.json"

#: The module-level guard whose `else:` makes the fallback exclusive.
RENDER_COUNT_GUARD = "count"

#: Set this to the path of a workflow to judge instead of the committed one.
ACADEMIC_OVERRIDE_ENV = "WORKFLOW_LINT_ACADEMIC_OVERRIDE"


def academic_workflow_text():
    """The academic-result.yml text under test, honouring the override env var."""
    override = os.environ.get(ACADEMIC_OVERRIDE_ENV)
    if override:
        path = Path(override)
        if not path.is_file():
            raise AssertionError(
                "%s=%r does not exist, so the check would judge nothing"
                % (ACADEMIC_OVERRIDE_ENV, override))
        sys.stderr.write(
            "NOTE: judging %s (%s=%s) INSTEAD of the committed workflow.\n"
            % (path, ACADEMIC_OVERRIDE_ENV, override))
        return path.read_text(encoding="utf-8")
    return W.raw_workflow_text(ACADEMIC)


def render_step_body(text):
    """The Python heredoc body of the 'Render academic result page' step."""
    doc = W.load_workflow_text(text)
    for job in (doc.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            if step.get("name") == RENDER_STEP and "run" in step:
                bodies = [b for _t, b in heredoc_bodies(step["run"])]
                if bodies:
                    return bodies[0]
    raise AssertionError(
        "no Python heredoc in %r / %r -- if the step was renamed the check "
        "needs to be updated deliberately, not deleted" % (ACADEMIC, RENDER_STEP))


def degraded_fallback_problems(body):
    """Reasons `body` can reach the degraded fallback with a non-empty payload.

    Returns a list of human-readable strings; empty means the invariant holds.
    Read off the AST rather than the text, so comments cannot satisfy it and a
    re-indentation or a reordering cannot break it.
    """
    problems = []
    tree = ast.parse(body)

    guard = None
    for node in tree.body:
        if (isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == RENDER_COUNT_GUARD):
            guard = node
            break
    if guard is None:
        return ["no module-level `if %s:` guard was found in the render step"
                % RENDER_COUNT_GUARD]

    if not guard.orelse:
        problems.append(
            "`if %s:` (line %d) has NO `else:` branch, so every statement "
            "after it is reachable after a SUCCESSFUL render. `finish(ok)` "
            "returns on success instead of exiting, so control falls into the "
            "degraded-payload fallback and `count_payloads()` returns -1 for "
            "the (correctly absent) degraded file, making `degraded <= 0` "
            "true and appending a SECOND page_ok=false to $GITHUB_OUTPUT."
            % (RENDER_COUNT_GUARD, guard.lineno))

    # Where is the degraded file read? Only inside the `else:` may it be read.
    inside_else = set()
    if guard.orelse:
        for node in ast.walk(ast.Module(body=guard.orelse, type_ignores=[])):
            inside_else.add(id(node))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and DEGRADED_FALLBACK_FILE in node.value):
            if id(node) not in inside_else:
                problems.append(
                    "line %d reads %s, which is NOT inside the `else:` of "
                    "`if %s:` -- the degraded fallback is reachable when the "
                    "monitor did emit a payload"
                    % (node.lineno, DEGRADED_FALLBACK_FILE, RENDER_COUNT_GUARD))

    # Exactly one `page_ok=` write, and only from inside finish().
    writes = len(re.findall(r"page_ok=%s", body))
    if writes != 1:
        problems.append(
            "the step body writes `page_ok=` %d time(s); it must be written "
            "from exactly one place, because GitHub takes the LAST value for a "
            "repeated output key and a second write silently replaces a "
            "correct one" % writes)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    and sub.value == "GITHUB_OUTPUT"):
                problems.append(
                    "line %d writes to $GITHUB_OUTPUT outside the finish() "
                    "helper, so a single step output can be written from more "
                    "than one place" % sub.lineno)
    return problems


def reconstruct_pre_fix_render_shape(text):
    """Return `text` with the DEFECT 6 fix undone -- the `else:` removed.

    A real temporary copy of the workflow, not a hand-typed snippet: the file is
    read, the else-branch is dedented back to module level and the `else:` line
    dropped, and the `page_ok=` single-write guard inside finish() is reverted
    too, because that guard was part of the same fix and leaving it in would
    make the reconstruction unfaithful in the other direction. Asserts that each
    edit actually applied, so a silent no-op cannot produce a fake mutation.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    start = next(i for i, l in enumerate(lines)
                 if l.strip() == "- name: %s" % RENDER_STEP)

    # Everything in this step's heredoc sits at 10 columns of YAML indentation;
    # `else:` at 10 and its body at 14 are what a block scalar strips to 0 and 4.
    else_at = next(i for i in range(start, len(lines))
                   if lines[i].rstrip() == "          else:")

    # Locate the single-write guard BEFORE mutating anything: `def finish(ok):`
    # is ABOVE the `else:`, so a search that starts at `else_at` cannot find it.
    def_lines = next(i for i in range(start, len(lines))
                     if lines[i].strip() == "def finish(ok):")
    open_at = next(i for i in range(def_lines + 1, len(lines))
                   if lines[i].strip().startswith(
                       "with open('state/.academic_page_ok'"))
    assert any("_wrote_page_ok" in ln for ln in lines[def_lines + 1:open_at]), (
        "expected the single-write guard at the top of finish()'s body; the "
        "shape changed, so this mutation would not be the pre-fix shape")

    # Dedent the else body back to module level, then drop the `else:` line.
    end = else_at + 1
    while end < len(lines) and lines[end].strip() != "PY":
        if lines[end].strip():
            assert lines[end].startswith("            "), (
                "unexpected indentation inside the else body at line %d: %r"
                % (end + 1, lines[end]))
            lines[end] = lines[end][4:]
        end += 1
    assert lines[else_at + 1:end], "the else body was empty; nothing to mutate"
    del lines[else_at]

    # Revert the single-write guard: the explanatory comment plus
    # `_wrote_page_ok = []` ahead of finish(), and the guard at the very top of
    # finish()'s body -- everything between `def finish(ok):` and the first
    # `with open(...)` inside it, which is exactly the guard and nothing else.
    # (Indices are recomputed because the `else:` deletion shifted them.)
    def_lines = next(i for i in range(start, len(lines))
                     if lines[i].strip() == "def finish(ok):")
    open_at = next(i for i in range(def_lines + 1, len(lines))
                   if lines[i].strip().startswith(
                       "with open('state/.academic_page_ok'"))

    out = []
    dropping_comment = False
    for index, line in enumerate(lines):
        if def_lines < index < open_at:
            continue
        stripped = line.strip()
        if stripped.startswith(
                "# DEFECT 6. `page_ok` is written to $GITHUB_OUTPUT"):
            dropping_comment = True
            continue
        if dropping_comment:
            if stripped.startswith("#"):
                continue
            dropping_comment = False
        if stripped == "_wrote_page_ok = []":
            continue
        out.append(line)

    rebuilt = "\n".join(out)
    assert "_wrote_page_ok" not in rebuilt, (
        "the single-write guard was not fully reverted, so the mutation would "
        "not be the pre-fix shape")
    # Scoped to this step's own lines rather than the whole file, because other
    # steps may legitimately contain an `else:` at the same YAML indentation.
    step_text = rebuilt[rebuilt.index("- name: %s" % RENDER_STEP):]
    step_end = step_text.find("\n      - name: ")
    if step_end != -1:
        step_text = step_text[:step_end]
    assert "\n          else:\n" not in step_text, "the else: was not removed"
    assert "\n          print('No payload in %s" in step_text, (
        "the fallback was not dedented to module level, so the mutation is not "
        "the pre-fix fall-through shape")
    return rebuilt


class AcademicRenderFallbackTests(WorkflowLintBase):
    """One `page_ok` per step, and the fallback only when nothing was emitted.

    Every test here EXECUTES the real heredoc body against the real
    `scripts/payload_io.py` and `scripts/render_academic.py`. Reading the code
    is what shipped this bug: the step was readable, the assert was readable,
    and only the run log showed a successful render turning into page_ok=false.
    """

    #: A five-payload run, because the observed failure was exactly that.
    PAYLOAD_COUNT = 5

    def setUp(self):
        import shutil
        self._tmp = tempfile.mkdtemp(prefix="render-fallback-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        os.makedirs(os.path.join(self._tmp, "state"), exist_ok=True)
        os.makedirs(os.path.join(self._tmp, "scripts"), exist_ok=True)
        for mod in ("payload_io.py", "render_academic.py"):
            shutil.copyfile(str(W.REPO_ROOT / "scripts" / mod),
                            os.path.join(self._tmp, "scripts", mod))
        self.gh_output = os.path.join(self._tmp, "gh_output.txt")
        with open(self.gh_output, "w", encoding="utf-8") as fh:
            fh.write("")

    # -- helpers ------------------------------------------------------------
    def _committed_body(self):
        return render_step_body(academic_workflow_text())

    def _write_live_payloads(self, n):
        """`state/academic_live_payload.jsonl` with `n` renderable payloads."""
        import json as _json
        path = os.path.join(self._tmp, "state", "academic_live_payload.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(n):
                _json.dump({
                    "kind": "academic_result",
                    "title": "CSIT Sem 1 Result: PASSED",
                    "semester": 1,
                    "roll_number": "0000000%d" % i,
                    "passed": True,
                    "status": "passed",
                    "result_date": "2026-09-29",
                    "pdf_url": "https://example.invalid/result.pdf",
                    "website": {"path": "docs/academic/2026-09-29-sem1.html"},
                }, fh)
                fh.write("\n")

    def _exec(self, body):
        """Run the step body in the temp dir; return (exit_code, stdout)."""
        import contextlib
        import io as _io
        saved = dict(os.environ)
        cwd = os.getcwd()
        buf = _io.StringIO()
        os.environ.update({"GITHUB_OUTPUT": self.gh_output, "DRY_RUN": "false"})
        code = 0
        try:
            os.chdir(self._tmp)
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(body, "<render>", "exec"), {"__name__": "wf_step"})
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        return code, buf.getvalue()

    def _page_ok_values(self):
        with open(self.gh_output, encoding="utf-8") as fh:
            return [ln.split("=", 1)[1] for ln in fh.read().split("\n")
                    if ln.startswith("page_ok=")]

    # -- the invariant, structurally ----------------------------------------
    def test_the_degraded_fallback_is_exclusive_to_an_empty_payload(self):
        """REGRESSION PROOF (DEFECT 6): the `else:` must exist and own the fallback."""
        problems = degraded_fallback_problems(self._committed_body())
        self.assertEqual(
            [], problems,
            "the render step can reach the degraded-payload fallback on a run "
            "that DID emit payloads, which is what turned 5 successful renders "
            "into page_ok=false on five consecutive runs:\n  "
            + "\n  ".join(problems))
        print("check 15: degraded fallback reachable only when count == 0, and "
              "page_ok is written from exactly one place")

    # -- the invariant, behaviourally ---------------------------------------
    def test_a_five_payload_run_writes_exactly_one_page_ok_true(self):
        """The observed production run, executed: 5 payloads must mean page_ok=true."""
        self._write_live_payloads(self.PAYLOAD_COUNT)
        code, out = self._exec(self._committed_body())
        self.assertIn(
            "rendered %d page(s) carrying %d payload(s)" % (self.PAYLOAD_COUNT,
                                                            self.PAYLOAD_COUNT),
            out, "sanity: the real renderer must actually have run.\n%s" % out)
        self.assertEqual(
            0, code,
            "a healthy %d-payload run must exit 0.\n%s"
            % (self.PAYLOAD_COUNT, out))
        self.assertEqual(
            ["true"], self._page_ok_values(),
            "page_ok must be written EXACTLY once and must be true for a run "
            "that rendered %d page(s). GitHub keeps the last value of a "
            "repeated output key, so a second write is a silent overwrite, not "
            "extra information.\nstep output:\n%s\n$GITHUB_OUTPUT:\n%s"
            % (self.PAYLOAD_COUNT, out, self.gh_output))
        self.assertNotIn(
            "falling back to the degraded page", out,
            "a run that emitted %d payload(s) must never consult the degraded "
            "channel.\n%s" % (self.PAYLOAD_COUNT, out))
        with open(os.path.join(self._tmp, "state", ".academic_page_ok"),
                  encoding="utf-8") as fh:
            self.assertEqual("true", fh.read().strip())

    # -- the mutation proof -------------------------------------------------
    def test_the_pre_fix_fall_through_shape_is_rejected(self):
        """REGRESSION PROOF: the two checks above can actually fail.

        Builds the PRE-FIX workflow in a temp directory -- the `else:` removed
        and the single-write guard reverted -- and runs the SAME checks against
        it, from the SAME code path, via WORKFLOW_LINT_ACADEMIC_OVERRIDE. The
        structural check must report the fall-through, and executing that body
        must reproduce the production symptom exactly: exit 1 and two
        `page_ok=` lines whose last value is `false`.
        """
        pre_fix_path = os.path.join(self._tmp, "academic-result.pre-fix.yml")
        with open(pre_fix_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(reconstruct_pre_fix_render_shape(academic_workflow_text()))

        self._write_live_payloads(self.PAYLOAD_COUNT)
        saved = os.environ.get(ACADEMIC_OVERRIDE_ENV)
        os.environ[ACADEMIC_OVERRIDE_ENV] = pre_fix_path
        try:
            pre_fix_body = self._committed_body()
            problems = degraded_fallback_problems(pre_fix_body)
            self.assertTrue(
                problems,
                "the structural check PASSED against the reconstructed pre-fix "
                "shape, so it cannot detect the defect it was written for -- "
                "the check is decoration.")
            code, out = self._exec(pre_fix_body)
        finally:
            if saved is None:
                os.environ.pop(ACADEMIC_OVERRIDE_ENV, None)
            else:
                os.environ[ACADEMIC_OVERRIDE_ENV] = saved

        self.assertEqual(
            1, code,
            "sanity: the pre-fix shape must reproduce the production failure "
            "(exit 1 after a successful render).\n%s" % out)
        self.assertIn(
            "rendered %d page(s) carrying %d payload(s)" % (self.PAYLOAD_COUNT,
                                                            self.PAYLOAD_COUNT),
            out, "sanity: the render itself succeeded in production too.\n%s" % out)
        self.assertEqual(
            ["true", "false"], self._page_ok_values(),
            "the pre-fix shape must reproduce the DOUBLE page_ok write -- that "
            "duplicate is what made the failure silent-but-wrong.\n%s" % out)
        print("check 15 mutation proof: pre-fix shape rejected (%d structural "
              "problem(s)) and reproduced exit=1 with page_ok=%r"
              % (len(problems), self._page_ok_values()))


# ==========================================================================
# Check 16 -- the Cloudflare Worker's schedule is ONE committed fact
# ==========================================================================
#
# WHY THIS EXISTS (the drift class this kills at the root). The Worker had four
# crons recorded in THREE places -- deploy/wrangler.toml `[triggers] crons`, the
# `CRON_*` constants in deploy/cloudflare-worker.js, and a prose "every 5 min"
# in breaking-monitor.yml -- with nothing comparing them. So a stale checkout
# could be deployed and NOTHING would fail: not the build, not the tests, not
# the canary. That is how one wrong comment survived for months, and how a
# deploy from the wrong commit would ship a schedule nobody intended.
#
# There are now four artifacts and this check holds them together:
#   * deploy/wrangler.toml  -- what `wrangler deploy` actually applies
#   * deploy/schedule.json  -- the human-reviewable record, plus the daily
#     invocation count and the `?health=1` URL both Actions arms poll
#   * the CRON_* constants   -- what scheduled() actually branches on
#   * WORKER_CRON_NPT_TABLE -- the Nepal Time a human reads, per worker cron
#
# The mutation proof at the end is not decoration. A check that has never been
# seen red is decoration.


class WorkerScheduleSingleSourceOfTruthTests(WorkflowLintBase):
    """wrangler.toml == schedule.json == the Worker's CRON_* constants."""

    def setUp(self):
        self.wrangler_list = wrangler_crons()
        self.schedule = load_schedule_json()
        self.worker_consts = worker_cron_constants()

    def test_all_three_records_of_the_schedule_agree(self):
        problems = schedule_sync_problems(
            self.wrangler_list, self.schedule, self.worker_consts)
        self.assertEqual(
            [], problems,
            "the Cloudflare Worker's schedule is recorded three times and the "
            "three disagree:\n  " + "\n  ".join(problems))
        print("check 16: %s -- wrangler.toml, schedule.json and the CRON_* "
              "constants all declare %d crons"
              % (", ".join(self.wrangler_list), len(self.wrangler_list)))

    def test_expected_invocations_per_day_is_derived_not_asserted(self):
        """Recomputed HERE from the cron list, never read and believed."""
        problems = invocation_count_problems(self.wrangler_list, self.schedule)
        self.assertEqual(
            [], problems,
            "deploy/schedule.json's documented invocation count does not follow "
            "from its own cron list:\n  " + "\n  ".join(problems))

    def test_every_worker_cron_converts_to_the_committed_npt_row(self):
        problems = worker_npt_problems(self.wrangler_list)
        self.assertEqual(
            [], problems,
            "Nepal Time (UTC+05:45) expectations for the Worker's crons:\n  "
            + "\n  ".join(problems))
        print("check 16: %d worker crons converted to Nepal Time, all matching "
              "WORKER_CRON_NPT_TABLE" % len(self.wrangler_list))

    def test_both_arms_of_the_switch_read_one_committed_health_url(self):
        texts = dict((wf, W.raw_workflow_text(wf)) for wf in HEALTH_URL_WORKFLOWS)
        problems = health_url_problems(self.schedule, texts)
        self.assertEqual(
            [], problems,
            "the dead-man's switch has two arms (the Worker itself, and GitHub "
            "Actions polling ?health=1) and both must read the endpoint from "
            "deploy/schedule.json:\n  " + "\n  ".join(problems))

    def test_the_documented_daily_total_counts_the_re_fires(self):
        """The figure this unit committed, held against the committed crons.

        `invocation_count_problems` already proves the number FOLLOWS from the
        list. This one proves the list is the ONE dispatcher, and that the
        internal figures derived from it are the pre-merge cadence -- 480
        breaking, 720 academic, 24 health, 2 slots, 8 re-fire attempts -- plus
        the 2 prebuild dispatches that ride the same trigger.

        The internal total is 1308: 1234 for the pre-merge cadence, plus 2,
        where the re-fire went from one extra firing per slot to a bounded window
        of four and the prebuild added one dispatch per slot, plus 72 for the
        retry ladder moved off its GitHub cron (U10). Each difference is
        deliberate and is asserted here rather than left to be discovered from a
        comment. `invocation_count_problems` and the explicit 1440 below are what
        hold the OTHER number still: the prebuild did not add a trigger.
        """
        problems = invocation_count_problems(self.wrangler_list, self.schedule)
        self.assertEqual([], problems, "\n  ".join(problems))
        self.assertEqual(
            1, len(self.wrangler_list),
            "the Worker is deployed with %d cron triggers (%s). It is supposed "
            "to be ONE: Cloudflare Workers Free allows 5 per account and this "
            "account was at the limit, which is why the jobs were merged into a "
            "dispatcher."
            % (len(self.wrangler_list), ", ".join(self.wrangler_list)))
        self.assertEqual(
            1440, int(self.schedule["expected_invocations_per_day"]),
            "deploy/schedule.json's expected_invocations_per_day is %r. One "
            "every-minute trigger is 60 x 24 = 1440. If the digest arm's "
            "schedule has genuinely changed, change this number deliberately and "
            "say so in the commit -- do not let it drift."
            % (self.schedule["expected_invocations_per_day"],))
        self.assertEqual(
            1308, int(self.schedule["expected_internal_job_fires_per_day"]),
            "expected_internal_job_fires_per_day is %r. It is the pre-merge "
            "cadence (480 breaking + 720 academic + 24 health + 2 slots + 8 "
            "re-fire attempts = 1234) plus 2 prebuild dispatches and 72 retry "
            "ladder fires, all of which ride this one every-minute trigger."
            % (self.schedule["expected_internal_job_fires_per_day"],))
        self.assertEqual(
            [], internal_fire_count_problems(self.schedule),
            "the internal per-day figures do not follow from the committed "
            "dispatcher schedule:\n  "
            + "\n  ".join(internal_fire_count_problems(self.schedule)))

    def test_the_merged_dispatcher_reproduces_every_pre_merge_cadence(self):
        """THE MERGE'S OWN ACCEPTANCE CRITERION, as a set equality.

        "Do not weaken breaking detection: keep the same effective sweep
        frequency as today." Recomputed here from the committed schedule data,
        comparing the Nepal-Time minute set each job fires on against the set
        the pre-merge cron produced. A dispatcher on a five-minute grid, or a
        sweep at minute % 5, fails this immediately -- which is why the owner's
        suggestion of `*/5` was refused.
        """
        problems = cadence_parity_problems(self.schedule)
        self.assertEqual(
            [], problems,
            "the merged dispatcher does not reproduce the pre-merge schedule:\n  "
            + "\n  ".join(problems))
        dispatcher = self.schedule["dispatcher_crons"]
        self.assertEqual(
            "*/3 * * * *", dispatcher["breaking_sweep"],
            "the breaking sweep must still be every third minute. 480 sweeps a "
            "day is what the owner asked to preserve, and a coarser stride is "
            "exactly the weakening this merge was told not to cause.")
        self.assertEqual(
            3, dispatcher["breaking_sweep_every_minutes"],
            "the recorded stride must be derived from the schedule it describes")
        print("check 16: the merged dispatcher reproduces %s to the minute"
              % ", ".join(sorted(PRE_MERGE_WORKER_CRONS)))

    def test_the_pre_merge_schedule_is_recorded_for_comparison(self):
        """The reference the parity test compares against must be committed.

        Without this the parity check would be comparing the schedule against a
        copy that exists only inside the check, which is the drift class this
        file exists to kill -- just moved from a comment into an assertion.
        """
        self.assertTrue(PRE_MERGE_WORKER_CRONS,
                        "the pre-merge cron set this file compares against is "
                        "empty, so the cadence-parity check would pass by "
                        "comparing nothing")
        recorded = (self.schedule.get("dispatcher_crons") or {})
        for expr in PRE_MERGE_WORKER_CRONS:
            self.assertIn(
                expr, json.dumps(recorded),
                "deploy/schedule.json no longer records the pre-merge cron %r "
                "anywhere in its dispatcher_crons block, so the cadence-parity "
                "check has lost the record of what the merge had to preserve: "
                "%s" % (expr, sorted(recorded)))

    def test_the_re_fire_window_is_bounded_and_five_minutes_after_its_slot(self):
        problems = dispatcher_cron_problems(self.schedule)
        self.assertEqual(
            [], problems,
            "the digest re-fire is not correctly wired as a bounded window:\n  "
            + "\n  ".join(problems))
        windows = self.schedule["dispatcher_crons"]["digest_refire_window"]
        slots = self.schedule["dispatcher_crons"]["digest_slots"]
        for slot, window in sorted(windows.items()):
            self.assertEqual(
                REFIRE_ATTEMPTS, len(window),
                "the %r window holds %d attempts, not %d: the retry must be "
                "bounded, and the bound is what stops the Worker racing the "
                "02:05 UTC GitHub fallback." % (slot, len(window),
                                                REFIRE_ATTEMPTS))
            self.assertIn(
                "40 1 * * *" if slot == "morning" else "40 13 * * *", window,
                "the first re-fire for %r is no longer the documented +5 minute "
                "firing" % slot)
            self.assertNotIn(
                slots[slot], window,
                "the re-fire window for %r repeats its own slot, which fires the "
                "same tick twice" % slot)
        print("check 16: digest re-fire windows bounded at %d attempts, +%d min "
              "each: %s"
              % (REFIRE_ATTEMPTS, REFIRE_GAP_MINUTES,
                 ", ".join("%s->%s" % (s, " ".join(w)) for s, w
                           in sorted(windows.items()))))

    def test_the_recorded_kv_budget_is_derived_from_its_own_table(self):
        problems = kv_write_budget_problems(self.schedule, self.worker_consts)
        self.assertEqual(
            [], problems,
            "the daily KV budget is recorded next to the figures it is made "
            "of, so it must add up:\n  " + "\n  ".join(problems))
        table = self.schedule["heartbeat"]["kv_writes_per_day"]
        digest = table["digest"]
        print("check 16: KV budget digest=%d/day (%s), heartbeat=%d/day, "
              "system_total_max=%d"
              % (digest["total_steady"],
                 " + ".join("%s=%d" % (k, digest[k]) for k in DIGEST_KV_TERMS),
                 table["total_max"], table["system_total_max"]))

    # -- U9: the punctual prebuild trigger -----------------------------------

    def test_the_prebuild_is_derived_from_its_slot_and_converts_to_npt(self):
        """THE CRON -> NEPAL-TIME MAPPING, COMPUTED AND NOT ASSUMED.

        `55 0 * * *` must be 06:40 NPT and `55 12 * * *` must be 18:40 NPT, and
        `prebuild_fail_safe_problems` proves it by running both through the
        same `cron_to_npt` converter every other schedule in this repository
        goes through. Nepal is +05:45 with no DST: 00:55 + 5h45 = 06:40 and
        12:55 + 5h45 = 18:40.

        A wrong offset here would move the delivery time and NOTHING ELSE WOULD
        SAY SO -- the prebuild would still dispatch, still seal, still be
        punctual relative to itself. So this is asserted on the conversion, not
        on a comment.
        """
        problems = prebuild_fail_safe_problems(self.schedule)
        self.assertEqual(
            [], problems,
            "the punctual prebuild trigger is not correctly wired:\n  "
            + "\n  ".join(problems))
        block = prebuild_schedule(self.schedule)
        for slot, expected_npt in (("morning", "06:40"), ("evening", "18:40")):
            expr = block[slot]
            actual = cron_to_npt(expr)
            self.assertEqual(
                [(int(expected_npt[:2]), int(expected_npt[3:]))], actual,
                "prebuild %s is %r, which converts to %s NPT, not %s NPT"
                % (slot, expr, actual, expected_npt))
            self.assertEqual(
                expected_npt, block[slot + "_npt"],
                "digest_prebuild.%s_npt is %r but %r converts to %s NPT. The two "
                "must not disagree, because one of them is what a human reads "
                "and the other is what the Worker runs."
                % (slot, block[slot + "_npt"], expr, expected_npt))
        # THE FOURTH COPY, CROSS-CHECKED. The lead exists in the Worker source
        # (subtracted from the slot), in schedule.json (committed), and in this
        # file (pinned). A prebuild instant is DERIVED from the lead, so a lead
        # that drifts makes every committed cron above describe something the
        # Worker does not do -- and nothing else in the suite would say so,
        # because the cron arithmetic is checked against the schedule's OWN
        # lead. That is exactly the four-way drift this file exists to kill.
        worker_lead = worker_prebuild_lead_minutes()
        self.assertIsNotNone(
            worker_lead,
            "%s declares no PREBUILD_LEAD_MINUTES constant. The prebuild instant "
            "is computed as `slot - lead`, so without it there is nothing to "
            "compute and every cron in schedule.json is unfalsifiable."
            % WORKER_JS_REL)
        self.assertEqual(
            PREBUILD_LEAD_MINUTES, worker_lead,
            "%s subtracts PREBUILD_LEAD_MINUTES=%r, but deploy/schedule.json "
            "records %r and this suite pins %r. The crons in schedule.json are "
            "the arithmetic result of the Worker's constant, so when these "
            "disagree the committed schedule describes a Worker that does not "
            "exist."
            % (WORKER_JS_REL, worker_lead,
               prebuild_schedule(self.schedule).get("prebuild_lead_minutes"),
               PREBUILD_LEAD_MINUTES))
        print("check 16: prebuild %s = %s NPT, %s = %s NPT (lead %d min, the "
              "same constant the Worker subtracts), both derived from the slot "
              "and both converted, not restated"
              % (block["morning"], block["morning_npt"],
                 block["evening"], block["evening_npt"],
                 PREBUILD_LEAD_MINUTES))

    def test_the_prebuild_lead_covers_the_worst_measured_prebuild(self):
        """The 40 minutes is a DESIGN CHOICE, so it is held to its evidence.

        Measured prebuild duration is 5-9 minutes on the four most recent runs
        and 22 minutes on one. The headroom must exceed the worst observation by
        a real margin, and the margin is asserted rather than assumed: a lead
        tightened to 25 minutes would still pass every other check here and
        would start missing slots on the days that already run long.
        """
        block = prebuild_schedule(self.schedule)
        self.assertTrue(block, "no digest_prebuild block to check a lead against")
        lead = int(block["prebuild_lead_minutes"])
        worst_observed = 22  # minutes, measured on the slowest of the recent runs
        self.assertGreaterEqual(
            lead, PREBUILD_LEAD_MINUTES,
            "the committed lead is %d, below the %d this suite pins"
            % (lead, PREBUILD_LEAD_MINUTES))
        self.assertGreater(
            lead, worst_observed,
            "the prebuild lead is %d minutes and a prebuild has been observed "
            "taking %d. A lead that does not exceed the worst observed build is "
            "not headroom, it is a schedule that misses on the days that are "
            "already slow." % (lead, worst_observed))
        # And the lead must fit INSIDE the freshness window it protects: the
        # prebuild at 06:40 NPT still leaves the 07:20 push ~40 minutes of
        # news freshness, which is what the digest's own "checked through" line
        # claims. Assert the shape rather than a number of minutes.
        for slot, npt_clock in (("morning", (7, 20)), ("evening", (19, 20))):
            pre = cron_to_npt(block[slot])
            self.assertEqual(1, len(pre), "%s prebuild is not one NPT slot" % slot)
            pre_h, pre_m = pre[0]
            pre_minutes = pre_h * 60 + pre_m
            slot_minutes = npt_clock[0] * 60 + npt_clock[1]
            self.assertLess(
                pre_minutes, slot_minutes,
                "the %s prebuild (%02d:%02d NPT) is not before its %02d:%02d NPT "
                "slot" % (slot, pre_h, pre_m, *npt_clock))
        print("check 16: prebuild lead %d min > worst observed prebuild %d min, "
              "and both prebuilds precede their slots in Nepal Time"
              % (lead, worst_observed))

    def test_the_prebuild_adds_two_job_fires_and_no_new_invocations(self):
        """2 job fires, 2 work ticks, 2 fewer no-op ticks, 1440 UNCHANGED.

        The prebuild rides the existing every-minute trigger, so the number of
        invocations Cloudflare makes must not move. If it does, someone deployed
        a second cron -- which the free tier has no room for, and which is the
        failure that half-applied once already.
        """
        problems = additions_internal_count_problems(self.schedule)
        self.assertEqual(
            [], problems,
            "the prebuild's contribution to the daily counts is wrong:\n  "
            + "\n  ".join(problems))
        # The arithmetic, spelled out, because a reader should be able to check
        # it by hand: 480 + 720 + 24 + 2 slots + 8 re-fires = 1234 pre-merge
        # cadence, plus 2 prebuilds.
        base_jobs, base_work, base_noop = _dispatcher_tick_arithmetic(self.schedule)
        self.assertEqual(
            1234, base_jobs,
            "the pre-merge internal cadence computes to %d, not 1234 (480 "
            "breaking + 720 academic + 24 health + 2 slots + 8 re-fires)"
            % base_jobs)
        self.assertEqual(
            988, base_work,
            "the pre-merge work-tick count computes to %d, not 988" % base_work)
        self.assertEqual(
            452, base_noop,
            "the pre-merge no-op count computes to %d, not 452" % base_noop)
        self.assertEqual(
            1308, int(self.schedule["expected_internal_job_fires_per_day"]),
            "expected_internal_job_fires_per_day is %r. The pre-merge cadence is "
            "1234, the prebuild adds exactly 2, and the ladder adds 72."
            % (self.schedule["expected_internal_job_fires_per_day"],))
        self.assertEqual(
            1014, int(self.schedule["expected_internal_work_ticks_per_day"]),
            "expected_internal_work_ticks_per_day is %r. 00:55 and 12:55 are "
            "divisible by neither the 2-minute nor the 3-minute sweep and are "
            "not :17, so each was a no-op tick and is now a work tick (988 + 2); "
            "the ladder then moves only :47, one hour at a time (990 + 24)."
            % (self.schedule["expected_internal_work_ticks_per_day"],))
        self.assertEqual(
            426, int(self.schedule["no_op_ticks_per_day"]),
            "no_op_ticks_per_day is %r. The prebuild moved two minutes from "
            "no-op to work and the ladder moved twenty-four, so 452 - 2 - 24."
            % (self.schedule["no_op_ticks_per_day"],))
        self.assertEqual(
            1440, int(self.schedule["expected_invocations_per_day"]),
            "expected_invocations_per_day is %r. The prebuild rides the ONE "
            "existing every-minute trigger, so this number must NOT move."
            % (self.schedule["expected_invocations_per_day"],))
        print("check 16: prebuild adds 2 job fires and the ladder 72 "
              "(1234->1308); work ticks 988->990->1014, no-op 452->450->426; "
              "invocations stay 1440")

    def test_the_ladder_adds_only_its_own_job_fires_and_24_work_ticks(self):
        """U10: the retry ladder's move off GitHub's scheduler, as arithmetic.

        It was 72 billed GitHub job-minutes a day. It is now 72 job fires on the
        Worker's existing one-minute trigger, so invocations must not move -- and
        because two of its three minutes (:17, :32) were ALREADY work ticks, only
        24 minutes change the tick profile. Both facts are asserted, because the
        tempting version of this figure (72 new work ticks) is wrong and would
        misstate the no-op argument this file exists to protect.
        """
        problems = ladder_internal_count_problems(self.schedule)
        self.assertEqual(
            [], problems,
            "the retry ladder's committed block does not describe itself:\n  "
            + "\n  ".join(problems))
        self.assertEqual(
            [17, 32, 47],
            sorted(set(m for _h, m in ladder_minutes(self.schedule))),
            "the ladder's minutes are not :17/:32/:47. The pre-merge GitHub cron "
            "fired on exactly those instants, and any other phase changes when a "
            "retry is dialled.")
        self.assertEqual(
            72, ladder_fire_count(self.schedule),
            "the ladder produces %d job fires a day, not 72 (3 an hour x 24)"
            % ladder_fire_count(self.schedule))
        base_jobs, base_work, base_noop = _dispatcher_tick_arithmetic(self.schedule)
        pre = _prebuild_minutes_from(self.schedule, [])
        work, noop = _tick_grid_with(
            self.schedule, set(pre) | set(ladder_minutes(self.schedule)))
        self.assertEqual(
            1234 + 2 + 72,
            base_jobs + len(pre) + ladder_fire_count(self.schedule),
            "the committed job-fire total is not the pre-merge 1234 plus 2 "
            "prebuild plus 72 ladder")
        self.assertEqual(
            24, work - (base_work + len(pre)),
            "the ladder moved %d minutes from no-op to work. :17 and :32 were "
            "already work ticks (the health check and the academic sweep), so it "
            "must be exactly 24 -- :47, once an hour."
            % (work - (base_work + len(pre))))
        self.assertEqual(
            1014, int(self.schedule["expected_internal_work_ticks_per_day"]),
            "expected_internal_work_ticks_per_day is %r, not 1014 (988 + 2 "
            "prebuild + 24 ladder)"
            % (self.schedule["expected_internal_work_ticks_per_day"],))
        self.assertEqual(
            426, int(self.schedule["no_op_ticks_per_day"]),
            "no_op_ticks_per_day is %r, not 426 (452 - 2 - 24); the grid walk "
            "above computes %d." % (self.schedule["no_op_ticks_per_day"], noop))
        self.assertEqual(
            1440, int(self.schedule["expected_invocations_per_day"]),
            "expected_invocations_per_day is %r. The ladder rides the ONE "
            "existing every-minute trigger, so this must NOT move."
            % (self.schedule["expected_invocations_per_day"],))
        worker = (W.REPO_ROOT / WORKER_JS_REL).read_text(
            encoding="utf-8", errors="replace")
        self.assertEqual(
            [], worker_ladder_constants_problems(self.schedule, worker),
            "the Worker's ladder constants and deploy/schedule.json disagree:\n  "
            + "\n  ".join(worker_ladder_constants_problems(self.schedule, worker)))
        print("check 16: the ladder adds 72 job fires (1236->1308) but only 24 "
              "work ticks (990->1014; no-op 450->426); invocations stay 1440")

    def test_the_worker_quota_constants_and_the_budget_file_agree(self):
        """THE MANDATORY AGREEMENT TEST, both directions.

        The Worker cannot read config/actions-budget.json at runtime, so it keeps
        a small parallel table. Two drifting copies is exactly how the overrun
        returns: the Worker refuses on a stale ceiling while the file claims
        another, and nothing notices. This reads BOTH sides, so a one-sided change
        to the ceiling, the reserve, the design target, or any per-target estimate
        fails here.
        """
        worker = (W.REPO_ROOT / WORKER_JS_REL).read_text(
            encoding="utf-8", errors="replace")
        budget = _load_budget_checker().load_budget()
        problems = worker_budget_constants_problems(budget, worker)
        self.assertEqual(
            [], problems,
            "the Worker's runtime-quota constants and config/actions-budget.json "
            "disagree:\n  " + "\n  ".join(problems))
        print("check 16: the Worker quota constants match "
              "config/actions-budget.json (allowance %s - reserve %s = ceiling %s; "
              "design target %s, reported not enforced)"
              % (budget.get("included_minutes"),
                 budget.get("budget_reserve_minutes"),
                 budget.get("hard_ceiling_minutes"),
                 budget.get("design_target_minutes")))

    def test_the_prebuild_is_not_a_second_cloudflare_trigger(self):
        """It is a JOB on the one trigger, and the three records must say so.

        This account is at Cloudflare's Workers Free limit of 5 cron triggers,
        which is why the five jobs were merged into a dispatcher. A prebuild
        added as a deployed trigger would be the third attempt at that limit and
        would half-apply the same way. So: still one deployed cron, and the
        prebuild appears only in the dispatcher's logical schedule.
        """
        self.assertEqual(
            1, len(self.wrangler_list),
            "the Worker is deployed with %d cron triggers (%s). It is supposed "
            "to be ONE: the account is at the Workers Free limit of 5, which is "
            "why every job is merged into a dispatcher."
            % (len(self.wrangler_list), ", ".join(self.wrangler_list)))
        self.assertEqual(
            ["* * * * *"], [str(c) for c in self.schedule["crons"]],
            "deploy/schedule.json declares %r. The prebuild is a job on the "
            "one-minute grid, NOT a second Cloudflare trigger."
            % (self.schedule["crons"],))
        worker = (W.REPO_ROOT / WORKER_JS_REL).read_text(
            encoding="utf-8", errors="replace")
        for expr in (prebuild_schedule(self.schedule)["morning"],
                     prebuild_schedule(self.schedule)["evening"]):
            self.assertNotIn(
                'crons = ["%s"' % expr, worker,
                "%r appears in the Worker's own CRON_* constants, which means it "
                "would be compared against the deployed cron list as a second "
                "trigger. It must be COMPUTED from the slot instead."
                % expr)
        print("check 16: prebuild rides the one deployed trigger; neither "
              "prebuild cron is registered with Cloudflare")

    def test_a_prebuild_that_could_silence_its_own_slot_fails(self):
        """MUTATION PROOF for the fail-safe, run through the REAL function.

        Five defects, each fed through `prebuild_fail_safe_problems` exactly as
        the real test calls it. Every one of them is a way the 40-minute lead
        turns from headroom into a way to LOSE the slot:

          1. a prebuild that WAITS -- it can outlive its own cron invocation and
             block the very tick it rides;
          2. a prebuild that writes KV -- the state a failed prebuild leaves
             behind, which nothing else would consult;
          3. a prebuild that writes a delivery marker -- the precise failure
             where a FAILED prebuild suppresses the slot's own push and the
             owner gets nothing at all;
          4. a prebuild at the wrong offset -- the silent one, which still
             dispatches, still seals, and still moves the delivery time;
          5. a prebuild that no longer precedes its slot.

        A check that has never been seen red is decoration.
        """
        schedule = self.schedule
        self.assertEqual(
            [], prebuild_fail_safe_problems(schedule),
            "sanity: the committed prebuild wiring must hold before a mutation "
            "can be shown to break it")
        self.assertEqual(
            [], additions_internal_count_problems(schedule),
            "sanity: the committed counts must hold before a mutation can be "
            "shown to break it")
        base = schedule["dispatcher_crons"]

        def mutated_prebuild(**changes):
            out = dict(schedule)
            out["dispatcher_crons"] = dict(
                base, digest_prebuild=dict(base["digest_prebuild"], **changes))
            return out

        # 1. It waits.
        problems = prebuild_fail_safe_problems(
            mutated_prebuild(waits_for_completion=True))
        self.assertTrue(
            problems, "prebuild_fail_safe_problems PASSED with a prebuild that "
            "WAITS for a 5-22 minute workflow from inside a cron invocation.")

        # 2. It writes KV.
        problems = prebuild_fail_safe_problems(
            mutated_prebuild(kv_writes_per_day=2))
        self.assertTrue(
            problems, "prebuild_fail_safe_problems PASSED with a prebuild arm "
            "that writes KV. A key written by the prebuild is state a failed "
            "prebuild leaves behind.")

        # 3. It writes a delivery marker -- the worst one.
        problems = prebuild_fail_safe_problems(
            mutated_prebuild(delivery_marker_written=True))
        self.assertTrue(
            problems, "prebuild_fail_safe_problems PASSED with a prebuild that "
            "writes a delivery marker. That is how a FAILED prebuild suppresses "
            "the slot's own push and the owner gets no news at all.")

        # 4. THE SILENT ONE: the offset is wrong. `55 1 * * *` is 01:55 UTC =
        # 07:40 NPT -- after the 07:20 slot -- so the prebuild would arrive
        # twenty minutes LATE and every other check here would still pass.
        late = mutated_prebuild(morning="55 1 * * *", morning_npt="07:40")
        problems = prebuild_fail_safe_problems(late)
        self.assertTrue(
            problems, "prebuild_fail_safe_problems PASSED with the morning "
            "prebuild at 55 1 * * * (07:40 NPT), which is TWENTY MINUTES AFTER "
            "the 07:20 NPT slot it is supposed to prepare. The NPT conversion "
            "is the only thing standing between this and a silent 20-minute "
            "regression.")

        # 5. It no longer precedes its slot -- swapped for the evening one, so
        #    the prebuild is a full day early rather than a few minutes late.
        wrong_day = mutated_prebuild(evening="55 1 * * *", evening_npt="07:40")
        problems = prebuild_fail_safe_problems(wrong_day)
        self.assertTrue(
            problems, "prebuild_fail_safe_problems PASSED with the evening "
            "prebuild pointed at the MORNING hour, so it would seal an "
            "artifact for a different firing.")

        # 6. The counts are inflated without the schedule producing them --
        #    the budget-drift shape this whole check was written to kill.
        overstated = dict(schedule)
        overstated["dispatcher_crons"] = base
        overstated["expected_internal_job_fires_per_day"] = 1310
        self.assertTrue(
            additions_internal_count_problems(overstated),
            "additions_internal_count_problems PASSED with the job-fire count "
            "inflated to 1240. A budget nobody recomputes is the comment this "
            "check exists to replace.")
        overstated2 = dict(schedule)
        overstated2["expected_invocations_per_day"] = 1442
        self.assertTrue(
            additions_internal_count_problems(overstated2),
            "additions_internal_count_problems PASSED with the invocation count "
            "at 1442. The prebuild must ride the existing trigger; a count that "
            "moved means someone deployed a second cron.")

        print("check 16 mutation: a waiting prebuild, a KV-writing prebuild, a "
              "marker-writing prebuild, a wrong-offset prebuild, a wrong-day "
              "prebuild and two inflated counts were ALL rejected")

    def test_a_refire_cron_missing_from_any_one_registry_fails(self):
        """MUTATION PROOF. One of the three records drops it; all three fail.

        The re-fire crons are the first thing a half-finished switch leaves
        behind: a constant in the source, a cron in wrangler, a row in
        schedule.json. Any one of them alone produces a deployed Worker whose
        behaviour and whose documentation disagree, so each is removed on its
        own here.
        """
        good = list(self.wrangler_list)
        self.assertEqual(
            [], dispatcher_cron_problems(self.schedule),
            "sanity: the committed dispatcher wiring must hold before a mutation "
            "can be shown to break it")
        self.assertEqual(
            [], cadence_parity_problems(self.schedule),
            "sanity: the committed cadence must match the pre-merge schedule "
            "before a mutation can be shown to break it")
        self.assertEqual(
            [], kv_write_budget_problems(self.schedule, self.worker_consts),
            "sanity: the committed KV budget must add up before a mutation can "
            "be shown to break it")
        dispatcher = str(self.schedule["dispatcher_crons"]["dispatcher"])
        self.assertIn(dispatcher, good,
                      "sanity: %r must be the one deployed cron" % dispatcher)

        # 1. wrangler.toml stops deploying it. What `wrangler deploy` applies
        #    is the deployed truth; the other two records would then describe
        #    a schedule nobody runs.
        problems = schedule_sync_problems(
            [c for c in good if c != dispatcher], self.schedule, self.worker_consts)
        self.assertTrue(problems,
                        "schedule_sync_problems PASSED with %r deployed by no "
                        "one: a cron the source branches on and the record "
                        "lists is not applied." % dispatcher)
        problems = schedule_sync_problems(
            [c for c in good if c != dispatcher], self.schedule,
            dict(self.worker_consts))
        self.assertTrue(problems,
                        "schedule_sync_problems PASSED with the deployed trigger "
                        "gone from wrangler.toml AND the Worker's own constant")

        # 2. schedule.json forgets it — the drift that a hand-edited record
        #    looks like.
        stale = dict(self.schedule)
        stale["crons"] = [c for c in good if c != dispatcher]
        problems = schedule_sync_problems(good, stale, self.worker_consts)
        self.assertTrue(problems,
                        "schedule_sync_problems PASSED with schedule.json "
                        "missing %r, which wrangler deploys" % dispatcher)

        # 3. the Worker's own constant is deleted while both records keep it.
        #    A branch that can never be taken, which is the mirror image.
        phantom = dict(self.worker_consts)
        phantom.pop("CRON_DISPATCHER", None)
        problems = schedule_sync_problems(good, self.schedule, phantom)
        self.assertTrue(problems,
                        "schedule_sync_problems PASSED with a deployed cron the "
                        "Worker's source no longer branches on")

        # 4. THE REGRESSION THE WHOLE MERGE WAS REFUSED FOR. A dispatcher on a
        #    five-minute grid cannot reproduce a */3 sweep, and saying so in a
        #    comment is how the stale "every 5 min" header survived for months.
        coarse = dict(self.schedule)
        coarse["dispatcher_crons"] = dict(
            self.schedule["dispatcher_crons"],
            breaking_sweep="*/15 * * * *",
            breaking_sweep_every_minutes=15)
        problems = cadence_parity_problems(coarse)
        self.assertTrue(problems,
                        "cadence_parity_problems PASSED with the breaking sweep "
                        "moved to every 15 minutes. That is a 5x drop in "
                        "detection frequency and the check that exists to catch "
                        "it did not.")
        print("check 16 mutation: the dispatcher dropped from EACH of the three "
              "records was rejected, and a coarsened sweep failed the parity "
              "check")

    def test_a_kv_budget_whose_terms_disagree_with_its_total_fails(self):
        """MUTATION PROOF for the accounting itself.

        Four breakages, each fed through the SAME function the real test uses:
        the digest total, the heartbeat total, the system total, and the slot
        count. The last is the interesting one — it is the failure that turns
        "3 writes a day" into a lie the day someone adds a third slot.
        """
        schedule = self.schedule
        table = schedule["heartbeat"]["kv_writes_per_day"]
        self.assertEqual(
            [], kv_write_budget_problems(schedule, self.worker_consts),
            "sanity: the committed KV budget must add up before a mutation can "
            "be shown to break it")
        self.assertIn("digest", table,
                      "sanity: kv_writes_per_day has no digest block to mutate")

        overstated = dict(schedule)
        overstated["heartbeat"] = dict(schedule["heartbeat"])
        overstated["heartbeat"]["kv_writes_per_day"] = dict(table)
        overstated["heartbeat"]["kv_writes_per_day"]["digest"] = dict(
            table["digest"], total_steady=int(table["digest"]["total_steady"]) + 3)
        self.assertTrue(
            kv_write_budget_problems(overstated, self.worker_consts),
            "kv_write_budget_problems PASSED with the digest total inflated. A "
            "budget nobody recomputes is the comment this check exists to "
            "replace.")

        understated = dict(schedule)
        understated["heartbeat"] = dict(schedule["heartbeat"])
        understated["heartbeat"]["kv_writes_per_day"] = dict(
            table, system_total_max=int(table["system_total_max"]) - 1)
        self.assertTrue(
            kv_write_budget_problems(understated, self.worker_consts),
            "kv_write_budget_problems PASSED with system_total_max off by one "
            "-- the quietest drift there is, and the one a reader never spots.")

        wrong_arms = dict(schedule)
        wrong_arms["heartbeat"] = dict(schedule["heartbeat"])
        wrong_arms["heartbeat"]["kv_writes_per_day"] = dict(
            table, total_max=int(table["total_max"]) + 5)
        self.assertTrue(
            kv_write_budget_problems(wrong_arms, self.worker_consts),
            "kv_write_budget_problems PASSED with the heartbeat total no longer "
            "the sum of the arms it covers.")

        grown = dict(schedule)
        grown["heartbeat"] = dict(schedule["heartbeat"])
        grown["heartbeat"]["kv_writes_per_day"] = dict(table)
        grown["heartbeat"]["kv_writes_per_day"]["digest"] = dict(table["digest"])
        grown["dispatcher_crons"] = dict(
            schedule["dispatcher_crons"],
            digest_slots=dict(schedule["dispatcher_crons"]["digest_slots"],
                              lunch="35 7 * * *"))
        self.assertTrue(
            kv_write_budget_problems(grown, self.worker_consts),
            "kv_write_budget_problems PASSED with a THIRD digest slot in the "
            "schedule and the per-slot figures unchanged. Adding a slot is "
            "exactly the moment the recorded cost goes stale.")
        print("check 16 mutation proof: 4 deliberate KV-budget breakages all "
              "rejected by the function the gate above uses")

    def test_a_divergent_schedule_is_rejected(self):
        """MUTATION PROOF. The checks above must be able to fail.

        Three deliberate breakages, each fed through the SAME functions the
        real test uses -- no reimplementation, so a green mutant here would mean
        the real check is not the thing being exercised.

          1. wrangler.toml keeps the health cron but schedule.json does not:
             the committed record is stale, which is exactly what a
             hand-edited JSON file looks like.
          2. the Worker's CRON_* constants disagree with both (a cron branched
             on but never deployed).
          3. expected_invocations_per_day is off by one -- the quietest drift
             of all, and the one a reader would never spot.
        """
        good = list(self.wrangler_list)
        self.assertEqual(
            [], schedule_sync_problems(good, self.schedule, self.worker_consts),
            "sanity: the committed records must agree before a mutation can be "
            "shown to break them")
        self.assertEqual(
            [], invocation_count_problems(good, self.schedule),
            "sanity: the committed invocation count must be correct first")

        # 1) schedule.json forgets the dispatcher that wrangler actually applies.
        dispatcher = str(self.schedule["dispatcher_crons"]["dispatcher"])
        without_dispatcher = [c for c in good if c != dispatcher]
        self.assertNotEqual(
            len(without_dispatcher), len(good),
            "sanity: %r must actually be the one deployed cron" % dispatcher)
        dropped = dict(self.schedule)
        dropped["crons"] = without_dispatcher
        problems = schedule_sync_problems(good, dropped, self.worker_consts)
        self.assertTrue(
            problems,
            "schedule_sync_problems PASSED with schedule.json missing the "
            "dispatcher that wrangler.toml deploys. The check cannot detect "
            "the drift it was written for, so it is decoration.")
        print("check 16 mutation 1/3 rejected: %s"
              % problems[0].splitlines()[0])

        # 2) a CRON_* constant that is branched on but never deployed.
        phantom = dict(self.worker_consts)
        phantom["CRON_PHANTOM"] = "59 * * * *"
        problems = schedule_sync_problems(good, self.schedule, phantom)
        self.assertTrue(
            problems,
            "schedule_sync_problems PASSED with a CRON_* constant that no "
            "deployed cron matches -- a branch scheduled() could take but "
            "Cloudflare would never fire.")

        # 3) the invocation total is one wrong.
        off_by_one = dict(self.schedule)
        off_by_one["expected_invocations_per_day"] = (
            int(self.schedule["expected_invocations_per_day"]) + 1)
        problems = invocation_count_problems(good, off_by_one)
        self.assertTrue(
            problems,
            "invocation_count_problems PASSED with expected_invocations_per_day "
            "off by one. A count nobody recomputes is a comment that starts "
            "lying.")
        print("check 16 mutation proof: 3 deliberate schedule breakages all "
              "rejected by the same functions the gate above uses")


class DeletedDigestAuthWorkerTests(unittest.TestCase):
    """The `digest-auth` Worker is deleted. Nothing may point a reader at it.

    It was an HTTP Basic proxy in front of the Pages site, so it could never
    have been the access control - and it demanded username `digest` before
    the origin asked for the owner's email, which is why the owner was locked
    out of the ntfy link on mobile. `functions/_middleware.js` is the real
    control and lives at the origin.

    The scan is on the HOST LITERAL, not on the string `digest-auth`. Several
    files still name the Worker in prose that records why the middleware
    exists, and that institutional history is worth keeping; what must not
    survive is a URL a reader can be sent to. The check is deliberately
    scoped to the trees that produce or configure a click target, so it
    cannot be satisfied by editing prose elsewhere.
    """

    DEAD_HOST = "digest-auth.example.workers.dev"
    # The real Worker host. deploy/schedule.json names it, so every health poll
    # reaches the deployment that exists instead of a placeholder subdomain.
    LIVE_HOST = "breaking-monitor.alishmainalee4.workers.dev"
    SCANNED = ("scripts", "templates", "prompts", "config", "deploy",
               ".github/workflows")

    def _files(self):
        root = W.REPO_ROOT
        for rel in self.SCANNED:
            base = root / rel
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if path.is_file() and path.suffix in (
                        ".py", ".yml", ".yaml", ".json", ".html", ".md",
                        ".txt", ".js", ".toml"):
                    yield path

    def _read(self, path):
        try:
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IOError):
            return None

    def test_no_file_ships_the_deleted_workers_dev_host(self):
        offenders = [str(p.relative_to(W.REPO_ROOT)) for p in self._files()
                     if self.DEAD_HOST in (self._read(p) or "")]
        self.assertEqual(
            [], offenders,
            "these files still send readers to the DELETED %s:\n  %s"
            % (self.DEAD_HOST, "\n  ".join(sorted(offenders))))

    def test_the_only_live_workers_dev_host_is_breaking_monitor(self):
        """Names the one Worker that must survive this cleanup."""
        found = set()
        for path in self._files():
            blob = self._read(path) or ""
            found.update(re.findall(r"https?://([a-z0-9.-]*workers\.dev)", blob))
        self.assertEqual(
            {"breaking-monitor.alishmainalee4.workers.dev"}, found,
            "unexpected workers.dev host(s) in the repo: %s" % sorted(found))

    def test_the_worker_source_files_are_gone(self):
        for rel in ("digest-auth-worker.js", "digest-auth-wrangler.toml"):
            path = W.REPO_ROOT / "deploy" / rel
            self.assertFalse(
                path.exists(),
                "%s still exists. The Worker is deleted; leaving its source "
                "behind invites someone to deploy it again and reintroduce the "
                "second login prompt." % path)


# ==========================================================================
# Check 17 -- a diagnostic step must actually carry `if: always()`.
#             DEFECT 2026-10-04: the digest origin auth canary had NEVER RUN.
# ==========================================================================

#: The phrases that DECLARE "this step must run regardless of what failed
#: above".  A step whose own name or comment block says one of these, and which
#: carries no `if: always()`, is a step that silently stops existing on exactly
#: the runs somebody is most likely to open.
#:
#: WHY RAW TEXT AND NOT THE PARSED DOCUMENT.  The parsed workflow contains no
#: comments at all -- the parser strips them before anything can read them --
#: and in this repository the intent is written in the comment block above
#: `run:`.  A rule that reads the parsed document therefore cannot see the very
#: thing it is looking for: an early version of this check read the parsed
#: document and did NOT flag the step whose comment says "this step failed on
#: every scheduled run before it".  So the scan is over raw text and the step
#: NAME is the anchor that ties a comment back to a step.
#:
#: EVERY PHRASE HERE IS A CONDITION PHRASE: it declares WHEN a step runs.
#: THAT IS THE WHOLE FILTER, and it is why the list is short.  Words that
#: describe a step's ROLE rather than its schedule -- "diagnostic", "tripwire",
#: "note only", "soft note" -- are deliberately NOT here, and each was removed
#: because it was MEASURED to fire on a step that is not missing anything:
#: "diagnostic" matched drift-canary.yml's own lint-gate step (which describes
#: itself as a diagnostic and must still fail the job), "tripwire" matched
#: breaking-monitor.yml's main monitor, "note only"/"soft note" matched a PKB
#: step, "on every run" matched academic-result.yml's state commit, and
#: "regardless of" matched daily-news.yml's story-history step.  A rule that
#: cannot tell "must run always" from "is a diagnostic" is not a rule, it is an
#: opinion about four other workflows -- three of which this change does not own.
#:
#: HONEST LIMITS, because a lint that hides its own weakness is decoration:
#:   * a comment is attributed to the step it follows, and a JOB-level comment
#:     (the `docs-lint` blurb, the `live-parser-canary` essay) is attributed to
#:     nobody: a job header resets the current step (JOB_HEADER_RE);
#:   * a `#` line inside a `run:` block scalar is indistinguishable from a YAML
#:     comment and is read as one.  That is a real false-positive risk, measured
#:     rather than assumed -- the check prints its own `declared` count;
#:   * it is a heuristic and cannot be complete.  A diagnostic step that
#:     declares nothing recognisable needs a table row (below), which is why
#:     ALWAYS_REQUIRED_STEPS exists and is enforced separately.
ALWAYS_RUN_INTENT_PHRASES = (
    "must always run",
    "must run on every",
    "on every scheduled",
    "every scheduled invocation",
    "even when an earlier step failed",
    "even when something earlier failed",
    "even when an earlier",
    "even if an earlier",
)

#: `- name: <step>`, however deeply indented.
STEP_NAME_RE = re.compile(r"^\s*-\s+name:\s*(.+?)\s*$")

#: A job header -- a key at two-space indent with no value.  It ends the
#: previous job's step list, so the comment block above `runs-on:` belongs to
#: the job rather than to whatever step happened to precede it.
JOB_HEADER_RE = re.compile(r"^ {2}[A-Za-z0-9_.-]+:\s*(#.*)?$")

#: (workflow, job, step name) -> the reason this step must run on every
#: invocation, independent of what its comment happens to say today.
#:
#: A row is not a wish: `test_required_always_run_steps_are_wired_up` asserts
#: BOTH that the step still exists (so a rename cannot quietly retire the
#: requirement) and that it really carries `if: always()`.
#:
#: WHY THIS TABLE IS EMPTY OF EXCEPTIONS RATHER THAN FULL OF THEM.  The one
#: step that most wants to be here -- the drift canary itself -- is deliberately
#: NOT required to run always: it is a gate whose job is to go red, and it needs
#: a healthy checkout and a reachable Worker.  Forcing `always()` onto a gate
#: would either produce a second red on a run that is already red, or require a
#: `continue-on-error` that would disarm it.  Requirement and fatality are
#: different questions, and this table only asks the first.
ALWAYS_REQUIRED_STEPS = {
    ("drift-canary.yml", "docs-lint",
     "Digest archive is not publicly readable (origin auth canary)"): (
        "The ONLY guard that can notice the digest archive becoming publicly "
        "readable again; the 2026-10-03 fix rests entirely on the deployed "
        "functions/_middleware.js. It was SKIPPED in both of that day's Drift "
        "Canary failures and had never executed once, because a step with no "
        "`if:` carries the implicit `success()` condition. It must report on "
        "every scheduled invocation, including the runs where the worker drift "
        "canary above it has already gone red."
    ),
    ("drift-canary.yml", "docs-lint",
     "PKB verified-date staleness note"): (
        "Pure reporting about the repository, and the most useful on exactly "
        "the runs where something else broke. It went dark on both 2026-10-04 "
        "failures for the same missing-`if:` reason."
    ),
}

#: (workflow, job, step name) -> why this step declares always-run intent but
#: must genuinely NOT run when an earlier step failed.
#:
#: EMPTY IS THE NORM, exactly as in ALWAYS_STEP_EXEMPTIONS.  Populate it only
#: for a step that needs state an earlier step was supposed to produce (a failed
#: step leaves none), and say so in the reason -- "because I said so" is not a
#: reason, and a future reader cannot tell a considered exception from an
#: oversight.
ALWAYS_RUN_OPT_OUTS = {}


def step_runs_on_always(step_dict):
    """True if the step's `if:` condition contains `always()`."""
    return "always()" in str(step_dict.get("if") or "")


def raw_always_run_intent_from_text(text):
    """(job_id, step name) -> phrases it declares, scanning RAW workflow text.

    Keyed by JOB as well as name, and that is not decoration: step names repeat
    across jobs -- `Checkout repository` appears once per job, and until
    2026-10-19 that meant four times in drift-canary.yml and twice in
    digest-watchdog.yml -- so a name-only key would merge those steps and let
    one step's comment decide another's verdict.

    Split out from `raw_always_run_intent` so the regression test can feed it a
    string and get the real answer, instead of asserting against a copy of the
    logic (which would prove nothing).
    """
    intent = {}
    job = None
    current = None
    for line in text.splitlines():
        header = JOB_HEADER_RE.match(line)
        if header:
            job = header.group(0).strip().rstrip(":").strip()
            current = None
            continue
        m = STEP_NAME_RE.match(line)
        if m:
            current = (job, m.group(1).strip().strip("'\""))
            intent.setdefault(current, [])
            continue
        if current is None:
            continue
        low = line.lower()
        for phrase in ALWAYS_RUN_INTENT_PHRASES:
            if phrase in low and phrase not in intent[current]:
                intent[current].append(phrase)
    return intent


def raw_always_run_intent(wf_name):
    """Map (job, step name) -> phrases, for one real workflow file."""
    return raw_always_run_intent_from_text(W.raw_workflow_text(wf_name))


def declares_always_run_intent(wf, job, step_name, step_dict):
    """The phrases this step's own text declares. Empty once it runs always."""
    if step_runs_on_always(step_dict):
        return []
    return list(raw_always_run_intent(wf).get((job, step_name), []))


def missing_always_condition(wf, job, step_name, step_dict, phrases=None):
    """Return an offender string if this step must run always and does not.

    Two independent reasons to want `if: always()`: the step declares the
    intent in its own comment (the general rule), or it is a row in
    ALWAYS_REQUIRED_STEPS (pinned by name so a reworded comment cannot retire
    the requirement).  Returns None when it runs always, when neither applies,
    or when the step carries an ALWAYS_RUN_OPT_OUTS entry.

    `phrases` defaults to whatever the workflow's raw text declares for this
    step.  It is a parameter rather than a hard-wired read so this function
    stays PURE and can be judged against a fixture -- a helper that reaches for
    the real file cannot be tested against anything, which is how a "proof"
    ends up proving nothing.
    """
    key = (wf, job, step_name)
    if step_runs_on_always(step_dict) or key in ALWAYS_RUN_OPT_OUTS:
        return None
    if phrases is None:
        phrases = declares_always_run_intent(wf, job, step_name, step_dict)
    if not phrases and key not in ALWAYS_REQUIRED_STEPS:
        return None
    return (
        "%s / %s / %r must run on every invocation but carries no "
        "`if: always()` (declared: %s).\n"
        "    GitHub Actions gives a step with no `if:` the implicit condition "
        "`success()`, so an earlier failure SKIPS it -- silently, with no "
        "warning and no red. This is not a theoretical shape: the digest origin "
        "auth canary in drift-canary.yml was skipped in both 2026-10-04 Drift "
        "Canary failures and had never executed once, so the repository's only "
        "guard against a publicly readable archive was reporting nothing.\n"
        "    Fix: add `if: always()` to the step. If the step can also fail, "
        "add `continue-on-error: true` -- or, if failing is the step's job, "
        "add an ALWAYS_STEP_EXEMPTIONS entry (Check 7). If this step genuinely "
        "must NOT run after a failure, add an ALWAYS_RUN_OPT_OUTS row saying "
        "why."
        % (wf, job, step_name,
           ", ".join(repr(p) for p in phrases) if phrases else "pinned by table")
    )


class DiagnosticStepAlwaysRunTests(WorkflowLintBase):
    def test_every_declared_step_is_a_real_step(self):
        """The raw-text scan must not invent (job, step) keys.

        It reads bytes and guesses structure, so the one thing that can go
        quietly wrong is attribution: a phrase landing on a key that is not a
        step, or a real step whose key the scan shaped differently.  Both would
        make the gate judge the wrong step, or judge nothing.  Step names are
        NOT unique per workflow -- `Checkout repository` appears four times in
        drift-canary.yml -- which is exactly why the key carries the job.
        """
        phantom = []
        missed = []
        for wf in W.WORKFLOW_NAMES:
            real = set()
            for job, name, _step in W.iter_all_steps(wf):
                real.add((job, name))
            scanned = set(raw_always_run_intent(wf))
            phantom += ["%s / %r is not a real step" % (wf, k)
                        for k in sorted(scanned - real)]
            missed += ["%s / %r was never reached by the scan" % (wf, k)
                       for k in sorted(real - scanned)]
        self.assertEqual(
            [], phantom,
            "the raw-text intent scan produced keys that are not steps, so a "
            "comment is being attributed to something that does not exist:\n  "
            + "\n  ".join(phantom))
        self.assertEqual(
            [], missed,
            "steps the raw-text scan never reached, so their comments are "
            "invisible to this check:\n  " + "\n  ".join(missed))

    def test_declared_always_run_steps_carry_if_always(self):
        """The lint gap this check closes, over EVERY workflow.

        Check 7 only ever looked at steps that ALREADY had `if: always()`, so
        nothing in CI could complain about a diagnostic step that had none --
        which is how the origin auth canary sat unexecuted for a day while
        every lint stayed green.
        """
        offenders = []
        declared = []
        for wf in W.WORKFLOW_NAMES:
            # The TEXT declares intent regardless of whether the step is
            # already wired; `missing_always_condition` is what decides
            # whether that needs fixing.  Conflating the two would make this
            # check vacuous the moment it is satisfied.
            raw = raw_always_run_intent(wf)
            for job, step_name, step_dict in W.iter_all_steps(wf):
                phrases = raw.get((job, step_name)) or []
                if not phrases:
                    continue
                declared.append(((wf, job, step_name),
                                 step_runs_on_always(step_dict)))
                offender = missing_always_condition(wf, job, step_name, step_dict)
                if offender:
                    offenders.append(offender)
        self.assertEqual(
            [], offenders,
            "diagnostic steps that will be SKIPPED on any failing run:\n  "
            + "\n  ".join(offenders),
        )
        # Loose floor, not a count: this rule must not be able to go vacuous by
        # finding nothing to check.
        self.assertGreaterEqual(
            len(declared), 1,
            "no step in any workflow declares always-run intent, so this rule "
            "cannot fire and is not actually running.",
        )
        # And the specific one it exists for: it must be SEEN as declaring the
        # intent (which is what makes the rule general) and be running always.
        canary = ("drift-canary.yml", "docs-lint",
                  "Digest archive is not publicly readable (origin auth canary)")
        self.assertIn(
            (canary, True), declared,
            "the origin auth canary is no longer detected as an always-run "
            "diagnostic. Either the step was renamed, or the phrase list was "
            "trimmed past the words that declare its intent, or the raw-text "
            "scan stopped reaching it -- in any case this rule has lost the "
            "one case it was written for. Raw text is what it reads: %r"
            % (canary,),
        )
        print("check 17: %d step(s) declare always-run intent, all carry "
              "`if: always()`" % len(declared))

    def test_required_always_run_steps_are_wired_up(self):
        """Every ALWAYS_REQUIRED_STEPS row exists AND is really always()."""
        real = {
            (wf, job, name): step_dict
            for wf in W.WORKFLOW_NAMES
            for job, name, step_dict in W.iter_all_steps(wf)
        }
        stale = []
        unwired = []
        for key, reason in sorted(ALWAYS_REQUIRED_STEPS.items()):
            step_dict = real.get(key)
            if step_dict is None:
                stale.append(
                    "%s / %s / %r is required to run on `always()` but no such "
                    "step exists. Rename the step and this row follows it; "
                    "delete the step and DELETE THIS ROW, because a requirement "
                    "pointing at nothing is a comment that lies. (Reason on "
                    "file: %s)" % (key[0], key[1], key[2], reason)
                )
                continue
            if not step_runs_on_always(step_dict):
                unwired.append(
                    "%s / %s / %r has no `if: always()`. It will be skipped on "
                    "every failing run." % key
                )
        self.assertEqual([], stale, "stale ALWAYS_REQUIRED_STEPS rows:\n  "
                         + "\n  ".join(stale))
        self.assertEqual([], unwired, "required always-run steps that are not "
                          "wired up:\n  " + "\n  ".join(unwired))
        self.assertGreaterEqual(
            len(ALWAYS_REQUIRED_STEPS), 1,
            "ALWAYS_REQUIRED_STEPS is empty, so nothing is pinned by name and "
            "only the phrase rule above is running.",
        )

    def test_the_privacy_canary_runs_always_and_can_still_go_red(self):
        """Pin the origin auth canary BY NAME, in both directions.

        Two failure modes, opposite, and both real:

          * no `if: always()`  -> the guard is silently skipped (2026-10-04);
          * `continue-on-error: true` -> the guard runs, finds a breach, and
            reports GREEN. A check nobody is paged about is worse than no
            check, because the exposure is real and the badge is not.

        So this asserts the condition is present AND that the step is NOT
        exempted into silence -- and that the exemption Check 7 relies on is
        documented rather than accidental.
        """
        step_name = "Digest archive is not publicly readable (origin auth canary)"
        found = [
            (job, step_dict)
            for job, name, step_dict in W.iter_all_steps("drift-canary.yml")
            if name == step_name
        ]
        self.assertEqual(
            1, len(found),
            "expected exactly one step named %r in drift-canary.yml, found %d. "
            "A rename must be followed in ALWAYS_REQUIRED_STEPS and "
            "ALWAYS_STEP_EXEMPTIONS, or this pin silently stops pinning."
            % (step_name, len(found)),
        )
        job, step_dict = found[0]
        self.assertTrue(
            step_runs_on_always(step_dict),
            "%s / %r carries no `if: always()`. It is the only guard against a "
            "publicly readable digest archive and it was SKIPPED in both "
            "2026-10-04 Drift Canary failures -- it had never executed once."
            % (job, step_name),
        )
        self.assertNotEqual(
            "true", str(step_dict.get("continue-on-error") or "").lower(),
            "%s / %r has `continue-on-error: true`. That would make a publicly "
            "readable archive exit 1 into a GREEN job: the guard would run and "
            "still page nobody. Failing is this step's job, which is why it is "
            "in ALWAYS_STEP_EXEMPTIONS instead." % (job, step_name),
        )
        key = ("drift-canary.yml", job, step_name)
        self.assertIn(
            key, ALWAYS_STEP_EXEMPTIONS,
            "the step is not exempted from Check 7, so Check 7 will fail on "
            "it. Either give it `continue-on-error: true` (which disarms the "
            "guard) or record the exemption.",
        )
        self.assertGreaterEqual(
            len(ALWAYS_STEP_EXEMPTIONS[key].strip()), 40,
            "the exemption for %r carries no real reason. Check 7's rule is "
            "that an exemption is honest only when failing is the step's job, "
            "so the reason has to say what the step is for." % (step_name,),
        )
        # It must also still probe both hostnames: an `always()` guard that
        # quietly lost an arm is the same class of defect as one that never ran.
        run = step_dict.get("run") or ""
        for host in ("daily-digest.example.pages.dev", "master.daily-digest.example.pages.dev"):
            self.assertIn(
                host, run,
                "the origin auth canary no longer probes %s. Both hostnames are "
                "load-bearing: the second is an unmonitored way in the day "
                "Cloudflare Access is removed." % host,
            )

    def test_a_missing_if_always_is_rejected(self):
        """REGRESSION PROOF: the rule above can actually fail.

        This is the exact shape that shipped: a step whose own comment declares
        it must run on every scheduled run, with no `if:` key at all.  The
        comment is part of the fixture because that is where the intent lives
        and the rule reads RAW text -- an earlier version of this check read the
        parsed document, where comments do not exist, and silently saw nothing.
        No workflow file is edited.
        """
        step_name = "Digest archive is not publicly readable"
        broken_workflow = "\n".join([
            "name: proof",
            "on:",
            "  workflow_dispatch:",
            "jobs:",
            "  docs-lint:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - name: %s" % step_name,
            "        # THIS STEP FAILED ON EVERY SCHEDULED RUN BEFORE IT, which",
            "        # is worse than useless: a guard that cries wolf on the",
            "        # correct state teaches the owner to ignore it.",
            "        run: curl -sS -m 15 https://example.invalid/",
            "",
        ])
        intent = raw_always_run_intent_from_text(broken_workflow)
        fixture_key = ("docs-lint", "Digest archive is not publicly readable")
        self.assertIn(
            fixture_key, intent,
            "sanity: the fixture's own comment must declare the intent, or this "
            "proof proves nothing. Scanned: %r" % (intent,))
        self.assertIn(
            "on every scheduled", intent[fixture_key],
            "the phrase that made this rule fire on the real file must match "
            "the fixture too")

        step_dict = {"run": "curl -sS -m 15 https://example.invalid/"}

        # The scanner is a TEXT function -- it reports what the bytes say and
        # does not know whether the step is already wired. The `if:` filter is
        # `declares_always_run_intent`, checked here against the REAL file in
        # both states, because that split is easy to get wrong.
        real_name = "Digest archive is not publicly readable (origin auth canary)"
        self.assertEqual(
            [], declares_always_run_intent(
                "drift-canary.yml", "docs-lint", real_name, {"if": "always()"}),
            "a step that already runs on `always()` has nothing left to fix, "
            "so it must not be reported as declaring a missing condition")
        self.assertTrue(
            declares_always_run_intent(
                "drift-canary.yml", "docs-lint", real_name, {}),
            "and the same step with its `if:` stripped must be reported")

        offender = missing_always_condition(
            "drift-canary.yml", "docs-lint", step_name, step_dict,
            phrases=intent[fixture_key])
        self.assertIsNotNone(
            offender,
            "a step that declares always-run intent and carries no `if:` MUST "
            "be rejected; that is the whole defect")
        self.assertIn("success()", offender)

        # The fixed shape passes.
        self.assertIsNone(
            missing_always_condition(
                "drift-canary.yml", "docs-lint", step_name,
                dict(step_dict, **{"if": "always()"}),
                phrases=intent[fixture_key]))

        # An opt-out row is the sanctioned way to say "no, and here is why".
        key = ("drift-canary.yml", "docs-lint", step_name)
        try:
            ALWAYS_RUN_OPT_OUTS[key] = (
                "proof only: the step needs a file an earlier step writes, "
                "which does not exist when that step failed")
            self.assertIsNone(
                missing_always_condition(*key[:2], key[2], step_dict,
                                         phrases=intent[fixture_key]))
        finally:
            del ALWAYS_RUN_OPT_OUTS[key]

        # And a step that declares nothing is not this rule's business.
        quiet_workflow = "\n".join([
            "jobs:",
            "  docs-lint:",
            "    steps:",
            "      - name: Typo sweep",
            "        run: grep -rn 'teh' docs",
            "",
        ])
        self.assertEqual(
            [], raw_always_run_intent_from_text(quiet_workflow).get(
                ("docs-lint", "Typo sweep"), []),
            "sanity: a step whose comment declares no intent must declare none")
        self.assertIsNone(
            missing_always_condition("drift-canary.yml", "docs-lint",
                                     "Typo sweep", {"run": "grep x ."}))


# ==========================================================================
# Check 18 -- DEFECT 3: the worker drift canary must test ANCESTRY, not a
#             short-sha prefix match against the current HEAD.
# ==========================================================================
#
# WHAT HAPPENED (2026-10-05). drift-canary.yml's step comment and its error
# message both promised that the deployed WORKER_BUILD stamp must be "an
# ancestor-or-equal of the commit this run checked out". The code said:
#
#     elif not head.startswith(build):
#
# with `head = os.environ['GITHUB_SHA']` (40 chars) and `build` a 7-char short
# sha from the deployed Worker. A prefix test of a 40-char sha against a 7-char
# one is an EXACT CURRENT-HEAD MATCH wearing the word "ancestor": it can only
# pass when the Worker was deployed at precisely the checked-out commit. Every
# commit landing on master turned the job red with zero drift --
# runs 37182008636, 37193122408, 37270387471, 37296164969:
#
#     worker drift canary DRIFTED: the deployed build stamp '6aadfc0' is not an
#     ancestor of this checkout (646212b5c94c)
#
# while the compare API said 6aadfc0...646212b5c94c is `ahead`, `behind_by=0`,
# `ahead_by=16`, `merge_base=6aadfc0f0594b75a788bcda2b227e59d7d858776` -- i.e.
# the deployed stamp IS the merge base, a clean direct ancestor. Two firings a
# day on crons `30 0` and `17 3`, which is essentially the owner's whole email
# volume.
#
# WHY THE THREE SHAPES ARE ALL CHECKED, and none of them alone:
#   * `.startswith(` -- the exact defect. A cheap text ban catches it, and a
#     cheap text ban is not enough on its own, because the same wrong idea can
#     be spelled `head[:7] == build` or `build == head[:7]`.
#   * a short-sha EQUALITY comparison -- the same wrong idea re-spelled.
#   * `fetch-depth` -- `merge-base --is-ancestor` answers from the history it
#     is given. On the default shallow depth-1 clone it can report "not an
#     ancestor" for a commit that is one, so a correct implementation on a
#     shallow clone still produces the false page this check exists to end.
# The behavioural tests then EXECUTE the step's own body against four real git
# repositories, because "the text says merge-base" is not evidence that the
# verdict is right.


DRIFT_WORKFLOW = "drift-canary.yml"
DRIFT_JOB = "docs-lint"
DRIFT_STEP = "Worker build/schedule drift canary"

#: The ancestry mechanism the fix is allowed to use. Anchored so a comment or
#: an unrelated word cannot satisfy it.
IS_ANCESTOR_RE = re.compile(r"merge-base[^\n]*--is-ancestor|--is-ancestor")

#: `rev-parse --short` is fine on its own (the deploy recipe in this repo's own
#: error message uses it); it is only a defect when used to COMPARE two shas.
REV_PARSE_SHORT_RE = re.compile(r"rev-parse\s+--short")

#: String literals, matched multiline so the step's multi-line message strings
#: are blanked too rather than leaking a `rev-parse --short` into the code that
#: the sha rules scan.
STRING_LITERAL_RE = re.compile(
    r"'''.*?'''|\"\"\".*?\"\"\"|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"",
    re.DOTALL)


def drift_step_body(text=None):
    """The Python heredoc body of the worker drift canary step."""
    if text is None:
        text = W.raw_workflow_text(DRIFT_WORKFLOW)
    doc = W.load_workflow_text(text)
    for job_id, job in (doc.get("jobs") or {}).items():
        for step in (job.get("steps") or []):
            if step.get("name") == DRIFT_STEP and "run" in step:
                bodies = [b for _t, b in heredoc_bodies(step["run"])]
                if bodies:
                    return bodies[0]
    raise AssertionError(
        "no Python heredoc in %r / %r -- if the step was renamed the check needs "
        "updating deliberately, not deleting" % (DRIFT_WORKFLOW, DRIFT_STEP))


def _executable_lines(body):
    """Body lines with comments and blank lines removed.

    Comments are excluded because the fix DOCUMENTS the defect in the step's
    own comment block, and a rule that reads comments would flag the very
    paragraph explaining why the code changed. Same technique as Check 13.
    """
    return [ln for ln in body.split("\n")
            if ln.strip() and not ln.strip().startswith("#")]


#: Short-sha equality / prefix matching. The defect is specifically a
#: comparison BETWEEN the short stamp (`build`/`stamp`) and the checked-out
#: commit (`head`/`checkout`/`current`), so the rule below is expressed as that
#: pairing rather than as a blanket ban: `full == head` is legitimate (it is how
#: the step detects the equal case) and flagging it would flag the fix.
SHA_STAMP_NAME_RE = re.compile(r"\b\w*(?:build|stamp)\w*\b", re.I)
SHA_HEAD_NAME_RE = re.compile(r"\b\w*(?:head|checkout|current)\w*\b", re.I)
SLICED_HEAD_RE = re.compile(r"\w*(?:head|checkout|current)\w*\s*\[\s*:\s*\d+\s*\]",
                            re.I)
EQUALITY_RE = re.compile(r"(\w+(?:\[[^\]]*\])?)\s*(==|!=)\s*(\w+(?:\[[^\]]*\])?)")


def sha_equality_offenders(code):
    """Comparisons between the short deployed stamp and the checked-out commit.

    Line-scanned rather than a single regex because the shape varies
    (`head.startswith(build)`, `build == head`, `head[:7] == build`) while the
    INTENT is one thing, and because a whole-file regex would fire on
    `build == 'unset'`, which is a test against a literal the deploy recipe
    produces and not against a sha at all.
    """
    offenders = []
    for line in code.split("\n"):
        if not (SHA_STAMP_NAME_RE.search(line) or SHA_HEAD_NAME_RE.search(line)):
            continue
        for match in EQUALITY_RE.finditer(line):
            left, right = match.group(1), match.group(3)
            for short_side, other_side in ((left, right), (right, left)):
                if not (SLICED_HEAD_RE.fullmatch(short_side)
                        or SHA_STAMP_NAME_RE.fullmatch(short_side)):
                    continue
                if SHA_HEAD_NAME_RE.fullmatch(other_side):
                    offenders.append((line.strip(), match.group(0)))
                    break
    return offenders


def drift_ancestry_problems(body):
    """Reasons `body` does not implement an ancestor-or-equal test.

    Pure and text-only on purpose, so the mutation proof can feed it the
    reconstructed pre-fix body and get a real answer out of the same function
    the real gate uses.

    String literals are blanked before the sha rules run, for the same reason
    the equality rule is line-scoped: `rev-parse --short` appears inside this
    step's OWN error message telling the operator how to redeploy, and a rule
    that cannot tell a message from a comparison flags the fix and misses the
    defect.
    """
    problems = []
    raw = "\n".join(_executable_lines(body))
    code = STRING_LITERAL_RE.sub("''", raw)
    if not IS_ANCESTOR_RE.search(raw):
        problems.append(
            "the drift step never calls `git merge-base --is-ancestor`. The "
            "deployed stamp is a SHORT sha on a history the checkout may have "
            "moved on from, so only git can say whether it is on the path to "
            "HEAD; anything shorter (a prefix test, a sha equality, a "
            "timestamp comparison) is the DEFECT 3 shape.")
    offenders = sha_equality_offenders(code)
    for line, matched in offenders:
        problems.append(
            "line %r compares the short deployed stamp to the checked-out "
            "commit (%r). That is the exact test that produced four false "
            "'DRIFTED' verdicts on runs "
            "37182008636/37193122408/37270387471/37296164969: "
            "`head.startswith(build)` with a 40-char head and a 7-char build "
            "can only pass when the Worker was deployed at precisely HEAD, so "
            "every commit on master reads as drift. An ancestor that is merely "
            "BEHIND master is the healthy state -- the Worker is deployed "
            "separately from git and is EXPECTED to lag." % (line, matched))
    if ".startswith(" in code:
        problems.append(
            "the drift step contains a `.startswith(` test on the deployed "
            "stamp. A 40-char HEAD starts with a 7-char stamp ONLY when they "
            "are the same commit, so this is an exact-match test wearing the "
            "word ancestor: it is DEFECT 3.")
    if REV_PARSE_SHORT_RE.search(code):
        problems.append(
            "the drift step shortens a sha with `rev-parse --short` and then "
            "judges on it. Shortening is fine for a MESSAGE; it is not a "
            "relationship test.")
    return problems


def drift_job_checkout(text=None):
    """(job_id, checkout step_dict) for the job that RUNS the drift step."""
    if text is None:
        text = W.raw_workflow_text(DRIFT_WORKFLOW)
    doc = W.load_workflow_text(text)
    jobs = doc.get("jobs") or {}
    carriers = [job_id for job_id, job in jobs.items()
                if isinstance(job, dict)
                and any(isinstance(s, dict) and s.get("name") == DRIFT_STEP
                        for s in (job.get("steps") or []))]
    if len(carriers) != 1:
        raise AssertionError(
            "%r must live in exactly one job of %s, found it in %r -- a check "
            "that pins fetch-depth on the wrong job pins nothing"
            % (DRIFT_STEP, DRIFT_WORKFLOW, carriers))
    job_id = carriers[0]
    for step in (jobs[job_id].get("steps") or []):
        if "actions/checkout" in (step.get("uses") or ""):
            return job_id, step
    raise AssertionError("%s / %s has no checkout step" % (DRIFT_WORKFLOW, job_id))


def _git(repo, *args):
    import subprocess
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "lint", "GIT_AUTHOR_EMAIL": "lint@example.invalid",
                "GIT_COMMITTER_NAME": "lint",
                "GIT_COMMITTER_EMAIL": "lint@example.invalid",
                # Deterministic shas: the test asserts on short shas and a
                # clock-dependent one would make the fixture unreproducible.
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"})
    proc = subprocess.run(["git", "-c", "init.defaultBranch=master"] + list(args),
                          cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=120)
    return proc


class WorkerDriftAncestryTests(WorkflowLintBase):
    """The deployed stamp must be tested for ANCESTRY against the checkout.

    Every behavioural case here EXECUTES the step's own Python body against a
    REAL temporary git repository. Reading the code is what shipped this bug:
    the code said "ancestor" in its comment and in its error message and did
    something else entirely, and the only thing that noticed was four emails.
    """

    def setUp(self):
        import shutil
        self._tmp = tempfile.mkdtemp(prefix="drift-ancestry-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    # -- fixtures ---------------------------------------------------------
    def _repo(self, shape):
        """Build a small git repo in the shape named by `shape`.

        'linear'   A <- B(stamp) <- C(HEAD)     : stamp is a STRICT ancestor
        'equal'    A <- B == C(HEAD,stamp)      : stamp IS head
        'diverged' A <- B(stamp) on a side branch, master A <- C(HEAD)
        """
        path = Path(self._tmp) / ("repo-" + shape)
        path.mkdir()
        _git(path, "init", "-q", ".")
        _git(path, "config", "user.name", "lint")
        _git(path, "config", "user.email", "lint@example.invalid")
        _git(path, "config", "commit.gpgsign", "false")
        _git(path, "commit", "-q", "--allow-empty", "-m", "A")
        if shape == "diverged":
            _git(path, "checkout", "-q", "-b", "side")
        _git(path, "commit", "-q", "--allow-empty", "-m", "B")
        stamp = _git(path, "rev-parse", "HEAD").stdout.strip()
        if shape == "diverged":
            _git(path, "checkout", "-q", "master")
        if shape == "equal":
            head = stamp
        else:
            _git(path, "commit", "-q", "--allow-empty", "-m", "C")
            head = _git(path, "rev-parse", "HEAD").stdout.strip()
        return path, stamp, head

    def _run(self, body, stamp, head, reported_crons=None, expected_crons=None,
             repo=None):
        """Execute the step body; return (exit_code, stdout).

        `reported_crons` is what the deployed Worker claims and `expected_crons`
        is what deploy/schedule.json declares. They are separate arguments
        because assertion 2 of this step compares them, and a test that fed
        both sides the same list could never see that assertion fail.
        """
        import contextlib
        import io as _io
        import json as _json

        reported = list(reported_crons if reported_crons is not None else
                        ["*/2 * * * *", "*/3 * * * *", "35 1 * * *",
                         "35 13 * * *", "17 * * * *"])
        expected = list(expected_crons if expected_crons is not None else reported)
        health = Path(self._tmp) / ("health-%s.json" % abs(hash((stamp, head))))
        with open(health, "w", encoding="utf-8") as fh:
            _json.dump({"build": stamp, "crons": reported}, fh)
        out = Path(self._tmp) / "gh_output.txt"
        out.write_text("", encoding="utf-8")
        env = {
            "EXPECTED_CRONS": _json.dumps(expected),
            "HEALTH_URL": "https://breaking-monitor.example.invalid/?health=1",
            "WORKER_CODE": "200", "WORKER_CURL_RC": "0",
            "WORKER_HEALTH_FILE": str(health),
            "HEAD_SHA": head,
            "REPO_DIR": str(repo) if repo else os.getcwd(),
            "GITHUB_OUTPUT": str(out),
        }
        saved = dict(os.environ)
        buf = _io.StringIO()
        code = 0
        try:
            os.environ.update(env)
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(body, "<drift>", "exec"), {"__name__": "wf_step"})
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, buf.getvalue()

    def _committed(self):
        return drift_step_body()

    # -- 1. the static gate ----------------------------------------------
    def test_the_drift_step_implements_a_real_ancestry_test(self):
        problems = drift_ancestry_problems(self._committed())
        self.assertEqual(
            [], problems,
            "the worker drift canary does not test ancestry:\n  "
            + "\n  ".join(problems))
        print("check 18: drift step calls merge-base --is-ancestor and carries no "
              "short-sha prefix/equality comparison")

    def test_the_job_that_runs_the_drift_check_checks_out_full_history(self):
        job_id, checkout = drift_job_checkout()
        depth = (checkout.get("with") or {}).get("fetch-depth")
        self.assertEqual(
            "0", str(depth),
            "%s / %s / 'Checkout repository' has fetch-depth=%r. "
            "`git merge-base --is-ancestor` decides ancestry from the history it "
            "is GIVEN: on the default shallow depth-1 clone it can report 'not "
            "an ancestor' for a commit that is one, because the connecting "
            "commit is not in the clone. A correct implementation on a shallow "
            "clone still produces the exact false page DEFECT 3 was."
            % (DRIFT_WORKFLOW, job_id, depth))
        print("check 18: the job running the drift check uses fetch-depth: 0")

    # -- 2. behaviour, against real git ----------------------------------
    def test_a_strictly_older_deployed_build_is_healthy(self):
        """THE FALSE POSITIVE, executed. An ancestor behind master must PASS.

        This is the production case: build 6aadfc0, checkout 646212b, sixteen
        commits of drift in the DEPLOYED-WORKER-BEHIND sense only.
        """
        repo, stamp, head = self._repo("linear")
        code, out = self._run(self._committed(), stamp[:7], head, repo=repo)
        self.assertEqual(
            0, code,
            "a deployed build that is a STRICT ANCESTOR of this checkout is the "
            "HEALTHY state -- the Worker is deployed separately from git and is "
            "expected to lag master -- and this step exited %d.\n%s" % (code, out))
        self.assertIn(
            "is an ANCESTOR of", out,
            "the success message must state the real relationship. The old one "
            "implied an exact match, which is why a healthy lag read as drift.\n"
            "%s" % out)
        self.assertIn("1 commit(s) behind master", out,
                      "the message must quantify the lag, so an operator can "
                      "tell 'one deploy behind' from 'a fork'.\n%s" % out)

    def test_a_build_equal_to_the_checkout_is_healthy(self):
        repo, stamp, head = self._repo("equal")
        code, out = self._run(self._committed(), stamp[:7], head, repo=repo)
        self.assertEqual(0, code,
                         "a build stamp EQUAL to the checkout is the healthiest "
                         "state and must pass.\n%s" % out)
        self.assertIn("IS this checkout", out, out)

    def test_a_diverged_build_still_goes_red(self):
        """THE CHECK MUST STILL BE ABLE TO FAIL. Not weakened into decoration.

        A stamp that is NOT an ancestor is the drift this canary exists for --
        it means the deployment came from a branch, not from master.
        """
        repo, stamp, head = self._repo("diverged")
        code, out = self._run(self._committed(), stamp[:7], head, repo=repo)
        self.assertEqual(
            1, code,
            "a deployed build that is NOT an ancestor of this checkout must exit "
            "non-zero. It exited %d, so the canary can no longer see a deploy "
            "from the wrong branch -- which is the ONE thing it is for.\n%s"
            % (code, out))
        self.assertIn("::error::worker drift canary DRIFTED", out, out)
        self.assertIn("DIVERGED", out,
                      "the failure must name the real relationship so it is "
                      "diagnosable from the log alone.\n%s" % out)

    def test_an_unresolvable_build_stamp_fails_closed(self):
        """FAIL CLOSED, as required: unverifiable is not verified.

        If the stamped commit was garbage-collected or force-pushed away,
        ancestry cannot be established. Passing there would mean an unknown
        relationship is treated as a good one.
        """
        repo, _stamp, head = self._repo("linear")
        code, out = self._run(self._committed(), "deadbee", head, repo=repo)
        self.assertEqual(
            1, code,
            "a build stamp that resolves to nothing in the fetched history is "
            "UNVERIFIABLE and must exit non-zero, not pass. It exited %d.\n%s"
            % (code, out))
        self.assertIn("UNVERIFIABLE", out, out)

    def test_a_cron_mismatch_still_goes_red(self):
        """Assertion 2 of the same step must survive the rewrite."""
        repo, stamp, head = self._repo("linear")
        code, out = self._run(self._committed(), stamp[:7], head,
                              reported_crons=["17 * * * *"],
                              expected_crons=["*/2 * * * *", "*/3 * * * *",
                                              "35 1 * * *", "35 13 * * *",
                                              "17 * * * *"],
                              repo=repo)
        self.assertEqual(1, code,
                         "a deployed Worker whose crons differ from "
                         "deploy/schedule.json must still exit non-zero.\n%s" % out)
        self.assertIn("reports crons", out, out)

    # -- DEFECT 5: a stamp that is not a sha -------------------------------
    def test_a_stamp_that_is_not_a_sha_fails_closed(self):
        """A REF NAME IS NOT A DEPLOYED COMMIT, and git will happily lie here.

        `git rev-parse --verify 'master^{commit}'` resolves. The DEFECT 3 fix
        handed the deployed stamp straight to it, so a stamp of `master` or
        `HEAD` resolved to whatever that ref pointed at and the step printed

            worker drift canary: deployed build f1dc9dc IS this checkout

        with exit 0. Two things are wrong with that: the pre-fix prefix test
        went RED on `master`, so the ancestry fix WIDENED a pass path rather
        than narrowing one, and a branch can move between the deploy and the
        check, so a ref name is not evidence of WHICH commit was deployed --
        which is the entire thing this canary exists to find out. The fabricated
        sha in the success message is the worst of it: it reads as a verified
        result to whoever reads the log.
        """
        repo, _stamp, head = self._repo("linear")
        for stamp in ("master", "HEAD", "origin/master", "deadbeefZ",
                      "master~1", "refs/heads/master"):
            code, out = self._run(self._committed(), stamp, head, repo=repo)
            self.assertEqual(
                1, code,
                "the deployed build stamp %r is not a commit sha, so ancestry "
                "is unverifiable BY DEFINITION and this step must exit non-zero. "
                "It exited %d.\n%s" % (stamp, code, out))
            self.assertIn(
                "not a commit sha at all", out,
                "the failure must say WHY the stamp is unusable -- an operator "
                "who fat-fingered --var WORKER_BUILD needs to be told the value "
                "is not a sha, not that a commit could not be found.\n%s" % out)
            self.assertNotIn(
                "IS this checkout", out,
                "a stamp that is not a sha must never produce a success "
                "message. This one FABRICATED a healthy-looking short sha out "
                "of the nonsense value and reported exit 0.\n%s" % out)
            self.assertIn(
                "rev-parse --short HEAD", out,
                "the message must repeat the correct deploy recipe, so the fix "
                "is actionable without leaving the log.\n%s" % out)

    def test_a_real_sha_of_every_accepted_length_still_resolves(self):
        """The shape check must not become "reject anything unusual".

        7 chars is what the deploy recipe produces; 40 is a full sha; uppercase
        hex is valid git input and must not be refused for its case. If this
        fails, the stamp gate is rejecting correct stamps, which would turn
        every healthy run red for a reason no operator could act on.
        """
        repo, stamp, head = self._repo("linear")
        for value, label in ((stamp[:7], "7-char short sha"),
                             (stamp[:12], "12-char sha"),
                             (stamp.upper(), "40-char UPPERCASE sha")):
            code, out = self._run(self._committed(), value, head, repo=repo)
            self.assertEqual(
                0, code,
                "a real %s must still resolve and be judged on its merits; it "
                "exited %d.\n%s" % (label, code, out))
            self.assertIn("is an ANCESTOR of", out,
                          "a real %s must reach the ancestry test, not the "
                          "shape test.\n%s" % (label, out))

    def test_the_step_validates_the_stamp_shape_before_asking_git(self):
        """Structural, because the behavioural cases above depend on ORDER.

        Handing the stamp to `rev-parse` first and checking its shape later
        would still produce the right verdict on today's cases while leaving
        the widening in place for any input git resolves and the shape rule
        happens not to cover. This asserts the shape rule EXISTS and is a hex
        class with a length bound -- not the specific spelling, so a rename of
        the variable does not break it.
        """
        code = "\n".join(_executable_lines(self._committed()))
        hex_class = re.compile(r"\[\s*0-9a-f", re.IGNORECASE)
        length_bound = re.compile(r"\{\s*7\s*,\s*40\s*\}")
        self.assertIsNotNone(
            hex_class.search(code),
            "the drift step has no hex character class, so a stamp's SHAPE is "
            "never checked and a ref name is still resolved by git.\n%s" % code)
        self.assertIsNotNone(
            length_bound.search(code),
            "the stamp shape check has no 7..40 length bound. Without one, a "
            "stamp of `d` or of a 200-character string would be accepted, and "
            "the length is the part that makes 'short sha' a fact rather than "
            "an assumption.\n%s" % code)
        validate_at = hex_class.search(code).start()
        resolve_at = code.find("rev-parse")
        self.assertLess(
            validate_at, resolve_at,
            "the shape check (offset %d) must come BEFORE the first `rev-parse` "
            "call (offset %d): asking git first is what let a ref name through."
            % (validate_at, resolve_at))

    # -- 3. the mutation proof -------------------------------------------
    def test_the_pre_fix_prefix_test_shape_is_rejected(self):
        """MUTATION PROOF. The gate above must be able to go red.

        Reconstructs the pre-fix body -- the `head.startswith(build)` branch and
        the no-op `git` helper -- in memory, and feeds it to the SAME functions
        the real gate uses. It then runs the SAME production false positive
        through the reconstructed body and requires it to fire, because "the
        check passes" is only meaningful if "the check fails on the defect" has
        been observed.
        """
        pre_fix = self._reconstruct_pre_fix_body(self._committed())
        problems = drift_ancestry_problems(pre_fix)
        self.assertTrue(
            problems,
            "drift_ancestry_problems PASSED against the reconstructed pre-fix "
            "body, so it cannot detect the defect it was written for -- the "
            "check is decoration.")
        self.assertTrue(
            any("startswith" in p for p in problems),
            "the pre-fix shape must be caught by the prefix-match rule "
            "specifically, not only by the missing merge-base rule: %r"
            % (problems,))
        # And the reconstructed body must actually raise the production alarm
        # on a healthy, strictly-ancestor deployment.
        repo, stamp, head = self._repo("linear")
        code, out = self._run(pre_fix, stamp[:7], head, repo=repo)
        self.assertEqual(
            1, code,
            "the reconstructed pre-fix shape did not reproduce the production "
            "failure (exit %d). It must demand the exact HEAD.\n%s" % (code, out))
        self.assertIn("not an ancestor", out,
                      "the production message must be reproduced verbatim, so a "
                      "reader can match a red run against this proof.\n%s" % out)
        print("check 18 mutation proof: the pre-fix prefix shape is rejected by "
              "the same rules and still demands the exact HEAD")

    @staticmethod
    def _reconstruct_pre_fix_body(body):
        """Undo DEFECT 3 in the real body, in memory.

        Replaces the whole `else:` ancestry branch -- and the `else:` keyword
        itself -- with the pre-fix `elif not head.startswith(build)` branch, so
        the reconstructed chain is `if not head: ... elif not build ...:
        elif not head.startswith(build): ...`, which is the shape that shipped
        and it compiles. Editing the real text rather than retyping the step
        matters: a retyped fixture would prove nothing about the code that
        actually runs. Every edit is asserted to have applied, so a silent
        no-op cannot fake a mutation.
        """
        lines = body.split("\n")
        start = None
        for index, line in enumerate(lines):
            if line.strip().startswith("# DEFECT 3. THE REAL ANCESTRY TEST"):
                start = index
                break
        if start is None:
            raise AssertionError(
                "the ancestry branch is gone, so the pre-fix shape cannot be "
                "reconstructed; if the fix was rewritten, this proof must be "
                "updated with it -- not deleted")
        # The `else:` that owns the ancestry branch is directly above it.
        branch = None
        for index in range(start - 1, -1, -1):
            if lines[index].strip() == "else:":
                branch = index
                break
        if branch is None:
            raise AssertionError(
                "could not find the `else:` that opens the ancestry branch, so "
                "the pre-fix shape cannot be reconstructed faithfully")
        # The branch runs to the `if expected and reported != expected:` line.
        end = None
        for index in range(start + 1, len(lines)):
            if lines[index].strip().startswith("if expected and reported"):
                end = index
                break
        if end is None:
            raise AssertionError("could not find the end of the ancestry branch")
        original = "\n".join(lines[branch:end])
        assert "--is-ancestor" in original, (
            "sanity: the branch being replaced really is the ancestry test")
        assert "git(" in original, (
            "sanity: the branch being replaced really shells out to git")
        replacement = [
            "elif not head.startswith(build):",
            "    problems.append(",
            "        'DRIFTED: the deployed build stamp %r is not an ancestor of '",
            "        'this checkout (%s). The deployment came from a different or '",
            "        'older commit - redeploy from a clean master.'",
            "        % (build, head[:12] or '(unknown)'))",
        ]
        mutated = "\n".join(lines[:branch] + replacement + lines[end:])
        try:
            compile(mutated, "<pre-fix>", "exec")
        except SyntaxError as exc:  # pragma: no cover - a broken proof
            raise AssertionError(
                "the reconstructed pre-fix body does not compile (%s), so it is "
                "not the pre-fix shape and the mutation proof would prove "
                "nothing" % exc)
        assert "head.startswith(build)" in mutated, "mutation did not apply"
        assert "--is-ancestor" not in "\n".join(replacement), (
            "the replacement still contains the real ancestry test, so it is "
            "not the pre-fix shape")
        return mutated


# ==========================================================================
# Check 19 -- DEFECT 4: the watchdog must judge the slot it was SCHEDULED
#             for, never the one implied by the moment the job ran.
# ==========================================================================
#
# WHAT HAPPENED (2026-10-05). digest-watchdog.yml's "Check delivery marker"
# step derived BOTH its slot and its Nepal date from the wall-clock instant the
# job happened to start:
#
#     now_utc = datetime.datetime.now(datetime.timezone.utc)
#     npt = now_utc + timedelta(hours=5, minutes=45)
#     today = npt.strftime('%F')
#     expected = 'morning' if now_utc.hour < 12 else 'evening'
#
# GitHub delayed the `0 4,16 * * *` firing to ~19:19 UTC (runs 37146641145,
# 37227803911). 19:19 UTC is 01:04 NPT on 2026-10-05, so the run announced
# itself as the 16:00 UTC evening firing while reading a NPT date belonging to
# the NEXT day, and demanded an evening digest that was not due until 19:50 NPT
# 2026-10-05 -- about 19 hours early:
#
#     Digest watchdog: evening digest for 2026-10-05 (NPT) NOT delivered
#
# The 2026-10-04 NPT evening digest HAD been delivered (daily-news run
# 37106181621, 2026-10-04T13:35:47Z = 19:20 NPT). Two false pages, one per day,
# forever.
#
# THE SHAPE OF THE FIX, and why the arithmetic below is committed as a table
# rather than trusted to the code: the watchdog cron and the digest cron are two
# files that must agree on a relationship (each watchdog firing sits 1h55m
# AFTER the digest it judges), and that relationship is what keeps the due
# window safe — a gate that can suppress a real alarm is worse than no gate, so
# the non-suppression is asserted here from the crons rather than assumed in the
# step's comment.
#
# HONEST LIMIT OF THAT GATE, stated here because a lint that hides its own
# weakness is decoration. With the slot anchored to the SCHEDULED firing, the
# due-window comparison in the step is STRUCTURALLY OPEN for the committed
# crons: the firing is at or before now, and the digest is before the firing,
# so `now >= due` always holds. No test below pretends to exercise that branch,
# because no reachable state of the shipped crons can. What this check DOES
# enforce is that the 115-minute gap stays positive, so the gate keeps meaning
# "a digest moved later than its watchdog" rather than "a watchdog that fires
# before the digest exists" — the inversion that would make the watchdog either
# cry wolf nightly or, worse, swallow a real miss.
#
# `WATCHDOG_NOW_UTC` is the clock seam the step exposes so these tests can
# execute its REAL body at the two frozen instants the defect was observed at.
# It is unset in CI, so production reads the real clock.

WATCHDOG_WORKFLOW = "digest-watchdog.yml"
WATCHDOG_JOB = "watchdog"
WATCHDOG_STEP = "Check delivery marker"

#: Keyed by the watchdog's NEPAL firing time (the same shape CRON_NPT_TABLE
#: uses), because that is the clock both crons are compared on: the gap between
#: a watchdog firing and the digest it judges is a difference of NEPALI wall
#: clock times, and expressing it in UTC would make 21:45 look "after 19:50"
#: only by accident.
WATCHDOG_SLOT_TABLE = {
    # 04:00 UTC = 09:45 NPT, judging the 02:05 UTC = 07:50 NPT morning digest
    # of the SAME Nepal date: 1h55m earlier.
    (9, 45): ("morning", "07:50", 115),
    # 16:00 UTC = 21:45 NPT, judging the 14:05 UTC = 19:50 NPT evening digest
    # of the SAME Nepal date: 1h55m earlier.
    (21, 45): ("evening", "19:50", 115),
}

#: A NEPALI wall clock before 12:00:00 is the morning slot; after it is the
#: evening one. The step and this table must agree, because the step names a
#: digest by its NEPALI hour and a mismatch here would rename every slot.
NPT_MORNING_CUTOFF_HOUR = 12


def watchdog_marker_body(text=None):
    """The Python heredoc body of the watchdog's 'Check delivery marker' step."""
    if text is None:
        text = W.raw_workflow_text(WATCHDOG_WORKFLOW)
    doc = W.load_workflow_text(text)
    for job_id, job in (doc.get("jobs") or {}).items():
        for step in (job.get("steps") or []):
            if step.get("name") == WATCHDOG_STEP and "run" in step:
                bodies = [b for _t, b in heredoc_bodies(step["run"])]
                if bodies:
                    return bodies[0], step
    raise AssertionError(
        "no Python heredoc in %r / %r -- if the step was renamed the check needs "
        "updating deliberately, not deleting"
        % (WATCHDOG_WORKFLOW, WATCHDOG_STEP))


def watchdog_slot_problems(body, step_dict):
    """Reasons the marker step can derive its slot/date from the wall clock.

    `step_dict` is the step, not just the body: the schedule has to be bound
    into the environment from `${{ github.event.schedule }}`, and a body that
    reads a variable nothing sets is a body that silently falls back.
    """
    problems = []
    code = "\n".join(_executable_lines(body))
    env = step_dict.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    env_text = " ".join("%s: %s" % (k, v) for k, v in env.items())

    if "EVENT_SCHEDULE" not in env_text or "github.event.schedule" not in env_text:
        problems.append(
            "the marker step does not bind `github.event.schedule` into its "
            "environment (env was %r). The scheduled instant is what names the "
            "slot; without this the step can only guess from the clock, which is "
            "DEFECT 4." % (env,))
    if "EVENT_SCHEDULE" not in code:
        problems.append(
            "the marker step never reads the schedule it is handed. Deriving "
            "'which slot am I?' from anything but the schedule is the defect.")
    # The pre-fix shape: the slot and the date read off the running clock.
    for pattern, why in (
            (re.compile(r"\b\w+\s*=\s*'morning'\s+if\s+\w*now\w*\.hour"),
             "the slot is chosen by the hour the job HAPPENED TO START, which is "
             "not the hour it was scheduled for"),
            (re.compile(r"\w+\s*=\s*\w*now\w*\.strftime\('%F'\)"),
             "the date is the Nepal date of the job's START, so a firing that "
             "starts after 18:15 UTC reads tomorrow's date and demands a digest "
             "that is not due for hours"),
            (re.compile(r"\w+\s*=\s*\w*now\w*\s*\+\s*datetime\.timedelta"),
             "the Nepal conversion is anchored on the job's start instant"),
    ):
        found = pattern.search(code)
        if found:
            problems.append("%s -- found %r" % (why, found.group(0)))
    # ...and the positive requirement: a scheduled instant must be derived and
    # used for BOTH the slot and the date.
    if not re.search(r"most_recent_firing", code):
        problems.append(
            "the marker step never derives the scheduled firing it belongs to. "
            "A cron with two hours ('0 4,16 * * *') names a SCHEDULE, not a "
            "firing, so the instant has to be computed: the latest instant at "
            "or before the job's start at which that cron fires.")
    return problems


def watchdog_slot_table_problems():
    """Reasons the committed crons no longer agree with WATCHDOG_SLOT_TABLE.

    Re-derived from the workflows themselves, so neither the table nor the step
    can quietly start lying about the 1h55m gap the due-window gate depends on.
    """
    import datetime

    digest_crons = {}
    for expr in W.iter_schedule_crons("daily-news.yml"):
        digest_crons[expr] = cron_to_npt(expr)
    watch_crons = {}
    for expr in W.iter_schedule_crons(WATCHDOG_WORKFLOW):
        watch_crons[expr] = cron_to_npt(expr)

    problems = []
    mornings, evenings = [], []
    for expr, npt_slots in sorted(digest_crons.items()):
        hours = {h for h, _m in npt_slots}
        if hours == {"*"}:
            continue  # the 15-minute ladder; not a digest slot
        if len(hours) != 1 or len(npt_slots) != 1:
            problems.append("digest cron %r expands to %s NPT, so it is not one "
                            "slot any more; WATCHDOG_SLOT_TABLE must be "
                            "re-derived" % (expr, sorted(npt_slots)))
            continue
        (hour, minute), = npt_slots
        label = 'morning' if hour < NPT_MORNING_CUTOFF_HOUR else 'evening'
        (mornings if label == 'morning' else evenings).append((hour, minute))
    if len(mornings) != 1 or len(evenings) != 1:
        problems.append(
            "expected exactly one morning and one evening digest cron in "
            "daily-news.yml, found mornings=%s evenings=%s. The watchdog names a "
            "slot by its Nepal hour, so the pair has to be unambiguous."
            % (sorted(mornings), sorted(evening)))

    watch_slots = set()
    for expr, npt_slots in watch_crons.items():
        if {h for h, _m in npt_slots} == set(range(24)):
            continue  # the 15-minute retry ladder
        watch_slots.update(npt_slots)
    if not watch_slots:
        problems.append("no watchdog firing slot could be derived from %s"
                        % WATCHDOG_WORKFLOW)
        return problems

    # Each watchdog firing must land 1h55m after the digest it judges, and both
    # must be the SAME Nepal date. This is the arithmetic the due-window gate
    # rests on, so it is asserted rather than assumed.
    for npt_hour, npt_minute in sorted(watch_slots):
        row = WATCHDOG_SLOT_TABLE.get((npt_hour, npt_minute))
        if row is None:
            problems.append(
                "the watchdog fires at %02d:%02d NPT, which has no row in "
                "WATCHDOG_SLOT_TABLE. Adding a schedule is a deliberate act: add "
                "the row with the digest slot it judges and the gap in minutes, "
                "and let this check confirm the arithmetic." % (npt_hour, npt_minute))
            continue
        label, expected_due, gap_minutes = row
        pool = mornings if label == 'morning' else evenings
        if not pool:
            continue
        due_hour, due_minute = pool[0]
        watch_abs = npt_hour * 60 + npt_minute
        due_abs = due_hour * 60 + due_minute
        if "%02d:%02d" % (due_hour, due_minute) != expected_due:
            problems.append(
                "WATCHDOG_SLOT_TABLE says the %s digest is due at %s NPT, but "
                "daily-news.yml's cron puts it at %02d:%02d NPT"
                % (label, expected_due, due_hour, due_minute))
        if watch_abs - due_abs != gap_minutes:
            problems.append(
                "WATCHDOG_SLOT_TABLE claims the %02d:%02d NPT watchdog sits %d "
                "minutes after the %02d:%02d NPT digest, but the crons put it "
                "%d minutes after. The due-window gate in the marker step is "
                "only safe while that gap is POSITIVE: a watchdog that fired "
                "BEFORE the digest it judges would see a due time in the future "
                "and silently swallow the alarm."
                % (npt_hour, npt_minute, gap_minutes, due_hour, due_minute,
                   watch_abs - due_abs))
    return problems


#: Sentinel inside the pinned delivery ledger. It exists in NO committed file,
#: which is the entire point: `test_the_judgement_is_read_from_the_pinned_
#: ledger_and_not_the_repository` proves the step read the fixture by finding
#: this string in its output, so pointing GITHUB_WORKSPACE back at the
#: repository is detected by a string that cannot be there by accident.
PINNED_SEND_AT = "2026-10-04T02:06:00Z"


class DigestWatchdogSlotTests(WorkflowLintBase):
    """Judge the slot you were scheduled for, not the one the clock implies.

    These EXECUTE the step's real body with the clock pinned to the instants
    the false page was observed at, because the defect was purely about time
    and no amount of reading the code can settle whether a datetime comparison
    is right at a boundary.
    """

    def setUp(self):
        import shutil
        self._tmp = tempfile.mkdtemp(prefix="watchdog-slot-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    # -- the frozen production cases -------------------------------------
    #: run 37227803911: the 16:00 UTC firing started at 19:19 UTC on 2026-10-04,
    #: which is 01:04 NPT on 2026-10-05.
    DELAYED_EVENING_RUN = "2026-10-04T19:19:00+00:00"
    DELAYED_EVENING_DATE = "2026-10-04"
    #: A firing so delayed it starts after midnight UTC: the 16:00 UTC 2026-10-04
    #: run that began at 00:30 UTC on 2026-10-05. The old code called this
    #: `morning` (hour 0 < 12) AND read the NPT date as 2026-10-05, so it
    #: demanded a morning digest nobody owed it.
    ACROSS_MIDNIGHT_RUN = "2026-10-05T00:30:00+00:00"
    #: A normal, on-time morning firing.
    ON_TIME_MORNING_RUN = "2026-10-05T04:00:20+00:00"

    #: THE PINNED DELIVERY LEDGER -- the fix for a defect that was never in the
    #: watchdog, and one sentence explains it.
    #:
    #: `read_kv_ledger` returns None without CF_LEDGER_TOKEN/CF_ACCOUNT_ID, so
    #: the step's only record is the git mirror at `state/digest_slot_ledger.json`
    #: -- and this class used to read it out of the REAL REPOSITORY, through
    #: GITHUB_WORKSPACE, while every fixture here is frozen at 2026-10-04/05.
    #: Commit 850c763 (2026-10-12) created that file. Before it the file did not
    #: exist, which is what the predecessor of the displaced-slot test recorded
    #: in its own docstring: "This fixture has no delivery ledger on disk, so
    #: the verdict must be UNKNOWN." From 850c763 that premise was false
    #: FOREVER -- a mirror's `as_of` only moves forward, while these fixtures
    #: never do -- and the mirror's real `as_of` made every 2026-10-04/05 slot
    #: look genuinely absent, so `fresh=True` turned "I have no record of it"
    #: into MISSING and paged the owner for a digest this class never claimed
    #: was missing.
    #:
    #: So the ledger is a fixture now, and its two facts are chosen to put each
    #: verdict on the branch under test rather than to make the assertions
    #: easy:
    #:
    #:   as_of 2026-10-05T01:00:00Z  LATER than the 2026-10-04 morning digest's
    #:                                02:05 UTC due time, so the mirror is FRESH
    #:                                for that slot -- a fresh mirror holding no
    #:                                entry is a MISSING, and that is the branch
    #:                                tests 1-3 need to be sure is NOT being
    #:                                reached.
    #:   ...and EARLIER than the 2026-10-05 morning digest's 02:05 UTC due
    #:                                time, with NO entry for it -- a lagging
    #:                                record cannot testify, which is the UNKNOWN
    #:                                branch test 4 pins, and the one that must
    #:                                NOT raise the alarm.
    #:
    #: One mirror, both verdicts, and neither of them the branch the other test
    #: is on. Nothing here weakens an assertion: the tests still demand exactly
    #: the same three verdicts, they just no longer inherit them from whatever
    #: the last seal happened to commit.
    PINNED_DELIVERY_LEDGER = {
        "schema_version": 1,
        "as_of": "2026-10-05T01:00:00Z",
        "days": {
            "2026-10-04": {
                "morning": {
                    "ntfy_sent_at": PINNED_SEND_AT,
                    "kind": "delivered",
                    "build": "fixture01",
                    "sequence_id": "pinned-2026-10-04-morning",
                },
            },
        },
    }

    def _fixture_workspace(self):
        """A workspace that is a faithful mini-repo EXCEPT for the ledger.

        The two committed workflows and the deploy config are COPIED, never
        faked. That is deliberate and it is the reason this class is worth
        running: it executes the step against the crons the repository actually
        ships, so a watchdog cron moved without the digest cron moving is still
        caught here. Only the delivery ledger is pinned, because only the
        ledger is data that keeps changing underneath frozen fixtures.
        """
        import json as _json
        import shutil as _shutil
        work = Path(self._tmp) / "workspace"
        (work / "state").mkdir(parents=True, exist_ok=True)
        for rel in (".github/workflows/daily-news.yml",
                    ".github/workflows/digest-watchdog.yml",
                    "deploy/wrangler.toml"):
            dst = work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(str(W.REPO_ROOT / rel), str(dst))
        (work / "state" / "digest_slot_ledger.json").write_text(
            _json.dumps(self.PINNED_DELIVERY_LEDGER, indent=2, sort_keys=True),
            encoding="utf-8")
        return work

    def _run(self, marker, now, event="schedule",
             schedule="0 4,16 * * *", body=None, step_dict=None):
        """Execute the marker step body; return (exit_code, stdout, outputs)."""
        import contextlib
        import io as _io
        import json as _json

        if body is None:
            body, step_dict = watchdog_marker_body()
        work = self._fixture_workspace()
        state = work / "state"
        # `marker=None` means the marker file is absent entirely.
        if marker is not None:
            (state / "last_digest_sent.json").write_text(
                _json.dumps(marker), encoding="utf-8")
        elif (state / "last_digest_sent.json").exists():
            (state / "last_digest_sent.json").unlink()
        out = Path(self._tmp) / "gh_output.txt"
        out.write_text("", encoding="utf-8")
        summary = Path(self._tmp) / "gh_summary.md"
        summary.write_text("", encoding="utf-8")

        # GITHUB_WORKSPACE points at the FIXTURE, not the repository. The step
        # reads the digest cron, its own `on.schedule` and the delivery-ledger
        # mirror from it, so pointing it at the repo made four frozen fixtures
        # depend on the last seal's committed `as_of`. See PINNED_DELIVERY_LEDGER.
        assert not str(work).startswith(str(W.REPO_ROOT)), (
            "the fixture workspace is inside the repository (%s), so this test "
            "is reading committed runtime state again" % work)
        saved = dict(os.environ)
        cwd = os.getcwd()
        os.environ.update({
            "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_WORKSPACE": str(work),
            "WATCHDOG_NOW_UTC": now,
            "EVENT_NAME": event,
            "EVENT_SCHEDULE": schedule,
            # PINNED EMPTY, NOT INHERITED. With a real CF_LEDGER_TOKEN in the
            # ambient environment the step would call out to Cloudflare and
            # these would stop being tests at all -- and would pass or fail on
            # the state of somebody's production KV namespace. The mirror path
            # is the one under test, so the KV path is switched off explicitly
            # rather than left to whoever ran them.
            "CF_LEDGER_TOKEN": "",
            "CF_ACCOUNT_ID": "",
        })
        buf = _io.StringIO()
        code = 0
        try:
            os.chdir(str(work))
            with contextlib.redirect_stdout(buf):
                try:
                    exec(compile(body, "<watchdog>", "exec"),
                         {"__name__": "wf_step"})
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved)
        outputs = dict(
            line.split("=", 1) for line in out.read_text(encoding="utf-8").split("\n")
            if "=" in line)
        return code, buf.getvalue(), outputs

    def test_the_judgement_is_read_from_the_pinned_ledger_and_not_the_repository(self):
        """THE FIX'S OWN PROOF, and the first thing to break if it is undone.

        Every behavioural test in this class was red for one reason, and none of
        the reasons was the watchdog: the step read the REAL committed
        `state/digest_slot_ledger.json`, and the moment commit 850c763 created
        that file the frozen 2026-10-04/05 fixtures inherited a live `as_of`
        that kept moving forward while they could not. Pinning the ledger fixes
        the symptom; this test is what stops the cause from coming back wearing
        the same name, because the sentinel below exists in no committed file.

        If someone points GITHUB_WORKSPACE at the repository again, the step
        stops finding the sentinel and finds the real mirror instead, and this
        goes red BEFORE the four behavioural tests do -- with one line of
        output instead of four.
        """
        _code, out, _outputs = self._run(
            {"date": self.DELAYED_EVENING_DATE, "time": "evening",
             "stories": 12, "delivered": True},
            self.DELAYED_EVENING_RUN)
        self.assertIn(
            PINNED_SEND_AT, out,
            "the displaced morning verdict was NOT read from the pinned ledger "
            "fixture (expected its sentinel send time %r in the output). The "
            "step is reading some other mirror -- most likely the repository's "
            "own committed state/digest_slot_ledger.json through "
            "GITHUB_WORKSPACE, which is the exact defect this class had.\n%s"
            % (PINNED_SEND_AT, out))
        self.assertNotIn(
            "live KV ledger unreadable", out,
            "CF_LEDGER_TOKEN/CF_ACCOUNT_ID must be pinned empty by _run so "
            "these tests never reach out to Cloudflare. A KV read here means the "
            "verdicts came from a production namespace, not from a fixture.\n%s"
            % out)

    # -- 1. the static gates ---------------------------------------------
    def test_the_marker_step_binds_and_uses_the_scheduled_cron(self):
        body, step_dict = watchdog_marker_body()
        problems = watchdog_slot_problems(body, step_dict)
        self.assertEqual(
            [], problems,
            "the digest watchdog can still derive its slot from the wall clock:\n  "
            + "\n  ".join(problems))
        print("check 19: the marker step binds github.event.schedule and derives "
              "its slot from the scheduled firing")

    def test_the_watchdog_and_digest_crons_still_agree_on_the_gap(self):
        problems = watchdog_slot_table_problems()
        self.assertEqual(
            [], problems,
            "the committed crons no longer match WATCHDOG_SLOT_TABLE, so the "
            "mark due-window gate is either unsafe or useless:\n  "
            + "\n  ".join(problems))
        print("check 19: each watchdog firing still sits after the digest it "
              "judges, by the amount WATCHDOG_SLOT_TABLE commits to")

    # -- 2. behaviour, at the observed instants ---------------------------
    def test_a_delayed_evening_run_judges_the_evening_digest_it_was_fired_for(self):
        """THE FALSE PAGE, executed at the minute it happened.

        Run 37227803911 started at 19:19 UTC on 2026-10-04 -- the 16:00 UTC
        firing, DELAYED by 3h19m -- and the Oct-4 NPT evening digest WAS
        delivered (daily-news run 37106181621, 19:20 NPT). The step must report
        evening / 2026-10-04 DELIVERED and must NOT page.
        """
        code, out, outputs = self._run(
            {"date": self.DELAYED_EVENING_DATE, "time": "evening",
             "stories": 12, "delivered": True},
            self.DELAYED_EVENING_RUN)
        self.assertEqual(0, code, out)
        self.assertEqual("false", outputs.get("miss"),
                         "this run must NOT claim a missed digest; stdout:\n%s" % out)
        self.assertEqual("evening", outputs.get("slot"),
                         "the 16:00 UTC firing judges the EVENING slot even when "
                         "it starts at 19:19 UTC. Deriving the slot from the "
                         "start hour is DEFECT 4.\n%s" % out)
        self.assertEqual(self.DELAYED_EVENING_DATE, outputs.get("today"),
                         "the judged date is the Nepal date of the SCHEDULED "
                         "firing (2026-10-04), not of the job's start "
                         "(2026-10-05 in Nepal).\n%s" % out)
        self.assertIn("DELIVERED", out, out)
        self.assertIn("2026-10-04", out, out)
        self.assertNotIn("2026-10-05", out,
                         "the verdict must not mention the next Nepal date at "
                         "all; that is the date the false page used.\n%s" % out)

    def test_a_delayed_evening_run_still_pages_when_that_digest_really_is_missing(self):
        """A MISSED ALARM IS WORSE THAN A FALSE ONE, so this must not regress.

        Same delayed run, but the evening digest it was fired for is genuinely
        absent: the step must report a miss. A fix that made the watchdog quiet
        by simply not looking would pass the test above and fail this one.

        NOTE ON THE EXIT CODE: this step always exits 0 by design — it only
        WRITES `miss=true` into $GITHUB_OUTPUT and annotates `::warning::`.
        Turning the job red is the next step's job ("Alert on missed digest",
        `if: steps.watch.outputs.miss == 'true'`, which ends in `exit 1`). So
        the thing asserted here is the verdict the marker step publishes, and
        `test_the_miss_verdict_really_can_turn_the_job_red` separately pins the
        consumer that acts on it. Asserting an exit code this step never had
        would have been a test that passes for the wrong reason.
        """
        code, out, outputs = self._run(
            {"date": self.DELAYED_EVENING_DATE, "time": "morning",
             "stories": 9, "delivered": True},
            self.DELAYED_EVENING_RUN)
        self.assertEqual("true", outputs.get("miss"),
                         "a genuinely undelivered evening digest must still be "
                         "reported as a miss. stdout:\n%s" % out)
        self.assertEqual("evening", outputs.get("slot"), out)
        self.assertEqual(self.DELAYED_EVENING_DATE, outputs.get("today"), out)
        self.assertIn("::warning::", out,
                      "the miss must be annotated, because that annotation and "
                      "the step output are what the alerting step consumes.\n%s"
                      % out)
        self.assertIn("MISSING", out, out)

    def test_the_miss_verdict_really_can_turn_the_job_red(self):
        """The marker step only REPORTS; something must act on it.

        Pinned in both directions, for the same reason Check 17 pins the
        origin auth canary: a `miss=true` nobody acts on is silence, and a
        `continue-on-error` on the alerting step would make a real missed
        digest report green.
        """
        alerts = [
            step_dict for _job, name, step_dict
            in W.iter_all_steps(WATCHDOG_WORKFLOW)
            if name == "Alert on missed digest"
        ]
        self.assertEqual(
            1, len(alerts),
            "expected exactly one 'Alert on missed digest' step in %s, found %d. "
            "If it was renamed, update this pin deliberately."
            % (WATCHDOG_WORKFLOW, len(alerts)))
        alert = alerts[0]
        condition = str(alert.get("if") or "")
        self.assertIn(
            "steps.watch.outputs.miss", condition,
            "the alerting step is not gated on the marker step's verdict, so a "
            "missed digest would be reported to nobody. `if:` was %r" % condition)
        self.assertIn("'true'", condition,
                      "the gate must be on miss == 'true', i.e. it must fire on "
                      "a MISS. `if:` was %r" % condition)
        self.assertNotEqual(
            "true", str(alert.get("continue-on-error") or "").lower(),
            "the alerting step carries continue-on-error: true, so a genuinely "
            "missed digest would exit 1 into a GREEN job. That is the exact "
            "inversion this watchdog exists to prevent.")
        run = alert.get("run") or ""
        self.assertIn("exit 1", run,
                      "the alerting step must end in `exit 1` so a missed digest "
                      "turns the run red. A warning that nothing escalates is "
                      "not a dead-man's switch.")
        # And the marker step's own output keys must be the ones the gate reads.
        _body, step_dict = watchdog_marker_body()
        self.assertEqual("watch", step_dict.get("id"),
                         "the marker step must keep `id: watch`; the alerting "
                         "step's gate names steps.watch.outputs.miss")

    def test_a_run_delayed_past_midnight_utc_still_judges_the_previous_evening(self):
        """The same defect, second shape: the 18:15 UTC boundary.

        A firing that starts after 18:15 UTC is already on the NEXT Nepal day,
        so the old `today = npt.strftime('%F')` moved the date forward and
        `now_utc.hour < 12` called it MORNING. Both halves were wrong, in
        opposite directions from the case above, which is why one test is not
        enough.
        """
        code, out, outputs = self._run(
            {"date": self.DELAYED_EVENING_DATE, "time": "evening",
             "stories": 12, "delivered": True},
            self.ACROSS_MIDNIGHT_RUN)
        self.assertEqual(0, code, out)
        self.assertEqual("false", outputs.get("miss"), out)
        self.assertEqual("evening", outputs.get("slot"), out)
        self.assertEqual(self.DELAYED_EVENING_DATE, outputs.get("today"),
                         "a run that starts at 00:30 UTC on 2026-10-05 is still "
                         "the 16:00 UTC firing of 2026-10-04 and must judge the "
                         "2026-10-04 NPT evening digest.\n%s" % out)

    def test_an_on_time_morning_run_is_unchanged(self):
        """The fix must not disturb the normal path, in either direction."""
        code, out, outputs = self._run(
            {"date": "2026-10-05", "time": "morning", "stories": 8,
             "delivered": True},
            self.ON_TIME_MORNING_RUN)
        self.assertEqual(0, code, out)
        self.assertEqual("false", outputs.get("miss"), out)
        self.assertEqual("morning", outputs.get("slot"), out)
        self.assertEqual("2026-10-05", outputs.get("today"), out)
        self.assertIn("on time", out,
                      "an on-time run must say so, so a delay is visible in the "
                      "log rather than inferred.\n%s" % out)

    def test_an_on_time_morning_run_still_pages_a_real_miss(self):
        code, out, outputs = self._run(
            None, self.ON_TIME_MORNING_RUN)
        self.assertEqual("true", outputs.get("miss"),
                         "a missing marker for an on-time, due morning digest "
                         "must be reported as a miss.\n%s" % out)
        self.assertEqual("morning", outputs.get("slot"), out)
        self.assertIn("marker unreadable", out,
                      "an absent marker file must be reported rather than "
                      "silently treated as delivered.\n%s" % out)

    def test_a_manual_dispatch_falls_back_to_the_committed_schedule(self):
        """No `github.event.schedule` on a dispatch: it must not guess.

        The fallback reads this workflow's own `on.schedule`, so a dispatch at
        a delayed time still judges the same slot the cron would have judged.
        """
        code, out, outputs = self._run(
            {"date": self.DELAYED_EVENING_DATE, "time": "evening",
             "stories": 12, "delivered": True},
            self.DELAYED_EVENING_RUN, event="workflow_dispatch", schedule="")
        self.assertEqual(0, code, out)
        self.assertEqual("false", outputs.get("miss"), out)
        self.assertEqual("evening", outputs.get("slot"), out)
        self.assertEqual(self.DELAYED_EVENING_DATE, outputs.get("today"), out)
        self.assertIn("committed on.schedule", out,
                      "the dispatch path must say where the schedule came from, "
                      "because it is a guess the operator should be able to "
                      "check.\n%s" % out)

    # -- DEFECT 5: a firing displaced past the NEXT firing ----------------
    #: The measured boundary, two minutes apart, from a review of the committed
    #: `0 4,16 * * *`. The evening marker is PRESENT in both, so the verdict is
    #: green either way and the ONLY difference is whether the step says which
    #: slot it did not judge.
    JUST_BEFORE_DISPLACEMENT = "2026-10-05T15:59:00+00:00"
    JUST_AFTER_DISPLACEMENT = "2026-10-05T16:01:00+00:00"
    DISPLACED_DATE = "2026-10-05"

    def test_the_evening_run_JUDGES_the_morning_slot_it_was_displaced_from(self):
        """THE RESIDUAL, CLOSED. The predecessor of this test asserted the gap.

        `most_recent_firing` takes the latest firing at or before now, so once
        the 16:00 UTC firing has also happened this run is re-attributed to the
        EVENING slot — and the 04:00 UTC firing's morning digest, genuinely
        absent in this fixture, used to be judged by nothing at all. The step
        could only PRINT that it could not tell, because the single-slot marker
        no longer held the morning.

        U8 gives it a per-slot delivery ledger to judge against, so the morning
        is now JUDGED. This replaces the disclosure assertion with a judgement
        assertion, and it is deliberately STRICTER than what it replaced: the
        old one was satisfied by any note naming the slot, and this one requires
        an actual verdict for that slot, under its own name, reaching an output
        the alerting step reads.

        What did NOT change, and is still asserted: the step still cannot
        DETECT that it was displaced (at 16:01 `late_by` is one minute, because
        it is measured from the firing it was re-attributed to), so the run must
        still name the displaced firing; and the slot this run OWNS is still
        judged exactly as before.
        """
        evening_delivered = {"date": self.DISPLACED_DATE, "time": "evening",
                             "stories": 12, "delivered": True}
        code, out, outputs = self._run(evening_delivered,
                                      self.JUST_AFTER_DISPLACEMENT)
        self.assertIn(
            "Watchdog (displaced slot)", out,
            "an evening run whose preceding firing is the same Nepal day's "
            "morning firing must JUDGE that slot, not merely mention it. This "
            "fixture has no delivery ledger on disk, so the verdict must be "
            "UNKNOWN — and saying so is the honest answer, not a gap.\n%s" % out)
        self.assertIn("04:00", out,
                      "the note must name the firing that was displaced, for "
                      "the same reason.\n%s" % out)
        self.assertIn(
            "cannot testify", out,
            "the note must state the reason in terms an operator can act on: "
            "the answer changes once a ledger exists, and the operator needs to "
            "know that.\n%s" % out)
        # THE VERDICT IS EMITTED, not merely printed. A line in the log that no
        # gate reads is the decoration this repository has been bitten by
        # before, and it is the whole difference between closing this residual
        # and merely narrating it.
        self.assertEqual(
            "UNKNOWN", outputs.get("displaced_verdict"),
            "the displaced slot must reach a verdict the alerting step reads.\n%s"
            % out)
        self.assertEqual("evening", outputs.get("judged_slot"),
                         "the slot this run owns is still the evening one, and "
                         "`judged_slot` is what keeps the two apart.\n%s" % out)
        # AND AN UNKNOWN DISPLACED SLOT MUST NOT RAISE THE ALARM. Collapsing it
        # into MISSED would page the owner nightly until the mirror caught up.
        self.assertEqual("false", outputs.get("miss"),
                         "an UNKNOWN displaced slot must never raise the alert "
                         "gate.\n%s" % out)
        self.assertEqual(0, code, out)

    def test_the_morning_run_does_not_claim_a_gap_it_does_not_have(self):
        """The coverage note must not become daily noise.

        A morning run's preceding firing is YESTERDAY's evening — a different
        Nepal date — so it has displaced nothing and has no same-day gap to
        report. Printing the note there would be a warning on every healthy run
        of the day, which is how a canary ends up crying wolf on the correct
        state until the owner ignores it.
        """
        _code, out, outputs = self._run(
            {"date": "2026-10-05", "time": "morning", "stories": 8,
             "delivered": True},
            self.ON_TIME_MORNING_RUN)
        self.assertNotIn(
            "Watchdog coverage", out,
            "this run judged the 04:00 UTC firing; its preceding firing is "
            "yesterday's 16:00 UTC one, on a different Nepal date, so there is "
            "no same-day gap to report and the note must stay silent.\n%s"
            % out)
        self.assertNotIn(
            "::warning::Digest watchdog", out,
            "a healthy on-time run must emit no watchdog warning of any kind.\n%s"
            % out)
        self.assertEqual("morning", outputs.get("slot"), out)
        self.assertEqual("2026-10-05", outputs.get("today"), out)

    def test_a_delay_just_short_of_the_next_firing_is_still_attributed_correctly(self):
        """11h59m late is still the MORNING firing's slot.

        This is the boundary that a "warn when displaced" rule would have to
        get right, and it pins that the attribution itself is correct on the
        near side: the run still judges the morning digest and still reports
        the real miss.
        """
        evening_only = {"date": self.DISPLACED_DATE, "time": "evening",
                        "stories": 12, "delivered": True}
        _code, out, outputs = self._run(evening_only,
                                        self.JUST_BEFORE_DISPLACEMENT)
        self.assertEqual("morning", outputs.get("slot"),
                         "15:59 UTC is still before the 16:00 UTC firing, so "
                         "this run belongs to the morning one.\n%s" % out)
        self.assertEqual("true", outputs.get("miss"),
                         "and it must still report the genuinely missing morning "
                         "digest. The coverage note must not soften a real "
                         "miss.\n%s" % out)
        self.assertIn("DELAYED by 719m00s", out,
                      "a late run must still say how late it was.\n%s" % out)
        self.assertNotIn(
            "Watchdog coverage", out,
            "no same-day gap is being displaced yet, so there is nothing to "
            "report.\n%s" % out)

    def test_the_clock_seam_is_not_reachable_from_the_workflow_env(self):
        """The harness seam must stay a harness seam.

        `WATCHDOG_NOW_UTC` freezes the clock this step reasons about. If it
        were settable from the workflow's committed `env:` block it would be one
        careless line away from a watchdog that judges a fixed instant forever,
        and the line would look harmless in review. This asserts the env block
        does not bind it -- the hazard is a repository-level variable, which is
        outside the repository and therefore outside this check's reach, which
        is why the step's comment names it explicitly.
        """
        _body, step_dict = watchdog_marker_body()
        env = step_dict.get("env") or {}
        self.assertIsInstance(env, dict, "the marker step's env: is not a "
                                         "mapping, so this check cannot see it")
        for key, value in env.items():
            self.assertNotIn(
                "WATCHDOG_NOW_UTC", str(value),
                "the marker step binds %s to %r. That makes the clock seam "
                "reachable from the committed workflow, where one careless line "
                "would freeze the watchdog's clock permanently and silently "
                "disarm it." % (key, value))

    # -- 3. the mutation proof -------------------------------------------
    def test_the_pre_fix_wall_clock_derivation_is_rejected(self):
        """MUTATION PROOF. The static gate above must be able to go red.

        Feeds the SAME function the exact pre-fix expressions from the real
        body. This is the cheap half of the proof; the behavioural half is that
        the pre-fix expressions produce the production date, which is asserted
        here by evaluating them rather than by trusting the regex.
        """
        body, step_dict = watchdog_marker_body()
        pre_fix_exprs = (
            "expected = 'morning' if now_utc.hour < 12 else 'evening'",
            "today = npt.strftime('%F')",
        )
        pre_fix_step = dict(step_dict)
        pre_fix_step["env"] = {}
        problems = watchdog_slot_problems(
            "\n".join(_executable_lines(body)) + "\n" + "\n".join(pre_fix_exprs),
            pre_fix_step)
        self.assertTrue(
            problems,
            "watchdog_slot_problems PASSED against the pre-fix wall-clock "
            "derivation, so it cannot detect DEFECT 4 -- the check is "
            "decoration.")
        self.assertTrue(
            any("HAPPENED TO START" in p for p in problems),
            "the pre-fix slot expression must be caught by the wall-clock rule "
            "specifically: %r" % (problems,))
        self.assertTrue(
            any("github.event.schedule" in p for p in problems),
            "a step with no schedule in its environment must be caught too: %r"
            % (problems,))

        # The pre-fix expressions evaluated at the observed instant, which is
        # the strongest form of the proof available without a second copy of
        # the step: they name 2026-10-05 evening, which is the exact demand
        # that paged the owner.
        import datetime
        observed = datetime.datetime.fromisoformat(self.DELAYED_EVENING_RUN)
        pre_fix_expected = ('morning' if observed.hour < 12 else 'evening')
        pre_fix_today = (observed + datetime.timedelta(hours=5, minutes=45)
                         ).strftime('%F')
        self.assertEqual(("evening", "2026-10-05"), (pre_fix_expected, pre_fix_today),
                         "sanity: the pre-fix derivation at the observed instant "
                         "must reproduce the production demand 'evening digest "
                         "for 2026-10-05', or this proof proves nothing")
        self.assertNotEqual(self.DELAYED_EVENING_DATE, pre_fix_today,
                            "sanity: the pre-fix date must differ from the date "
                            "the digest was actually delivered on, which is the "
                            "whole defect")
        print("check 19 mutation proof: the pre-fix wall-clock derivation is "
              "rejected by the same rules the gate uses")


# ==========================================================================
# Check 20 -- the seal job's paths agree with the artifact the uploader
#             actually produces (the 11-consecutive-run seal outage)
# ==========================================================================

#: The upload step in daily-news.yml and every reader of the unpacked
#: artifact in digest-prebuild.yml's seal job.
UPLOADER_WORKFLOW = "daily-news.yml"
UPLOAD_STEP = "Upload digest artifacts"
SEAL_WORKFLOW = "digest-prebuild.yml"


def upload_artifact_path_entries():
    """Return the upload step's `path:` list as a list of entries.

    Read from the parsed YAML, never hardcoded, because the whole point of
    this check is CONSISTENCY between uploader and consumer: a hardcoded
    list here would go stale the moment someone adds a sixth file and would
    happily keep passing while the consumer kept reading a path the
    unpack never creates.
    """
    wf = W.load_workflow(UPLOADER_WORKFLOW)
    for job_name, job in wf.get("jobs", {}).items():
        for step in job.get("steps", []) or []:
            if step.get("name") == UPLOAD_STEP:
                raw = (step.get("with") or {}).get("path") or ""
                entries = []
                for line in raw.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        entries.append(line)
                if not entries:
                    break
                return entries
    raise AssertionError(
        "no step named %r carrying a non-empty `path:` was found in %s, so the "
        "artifact layout this check reasons about cannot be derived. Either the "
        "step was renamed or its path list emptied -- and until this check can "
        "see the uploader, it cannot prove anything about the consumer."
        % (UPLOAD_STEP, UPLOADER_WORKFLOW)
    )


def upload_archive_root(entries):
    """Return the directory actions/upload-artifact@v4 will use as the
    archive root, i.e. the least common ancestor of ALL the search paths.

    This is the behaviour that killed the seal job. upload-artifact does NOT
    keep the directory structure you wrote in `path:`; it stores each file
    relative to the least common ancestor of ALL the search paths
    (`getMultiPathLCA` in the action's own `src/shared/search.ts`). The
    artifact therefore mirrors the `path:` list from the LCA down, and
    download-artifact unpacks that tree into `path: prebuild`.

    WHAT THE LAYOUT IS NOW, AND WHY THIS FUNCTION HAD TO CHANGE. With every
    entry under `state/`, the LCA was `state/` itself and the five payload
    files unpacked FLAT: `prebuild/email.html`. The upload list now also
    carries `docs/digests/<date>-<slot>.html` -- the rendered WEB page, which
    is the file the seal asserts and hashes -- so the LCA of the list is the
    WORKSPACE ROOT and each file keeps its own directory:
    `prebuild/state/email.html`, `prebuild/docs/digests/<date>-<slot>.html`.

    THE BUG THIS FUNCTION CARRIED UNTIL NOW. It computed the LCA over
    `parts[:-1]`, which silently EXCLUDES THE LAST ENTRY. While every entry
    shared the leading `state/` segment that made no difference, so the bug
    was invisible and the function agreed with production. The moment an entry
    outside `state/` is appended it stops agreeing: this function kept
    reporting `state` while the real action reported the workspace root, so
    the consistency check below would have GREEN-LIT a seal job reading
    `prebuild/state/ntfy_payload.json` when the unpack created
    `prebuild/ntfy_payload.json`. A guard that disagrees with the action it
    is guarding is worse than no guard, so the derivation is now over every
    entry, exactly as the action does it.
    """
    parts = [e.split("/") for e in entries]
    if any(len(p) == 0 for p in parts):
        raise AssertionError("upload `path:` entry with no directory component: %r" % entries)
    common = []
    for segment_group in zip(*parts):
        if len(set(segment_group)) != 1:
            break
        common.append(segment_group[0])
    return "/".join(common)  # '' means the workspace root


def unpacked_paths(entries):
    """Every `prebuild/...` path the unpack will create, from the entries.

    The archive root is STRIPPED from each entry, because that is what the
    action does: a file stored relative to the LCA loses the LCA's own
    segments. A workspace-root LCA (``''``) strips nothing, which is why a
    nested layout and a flat one are both described by this one function
    rather than by two shapes of special case.
    """
    root = upload_archive_root(entries)
    out = set()
    for entry in entries:
        rel = entry
        if root and entry.startswith(root + "/"):
            rel = entry[len(root) + 1:]
        out.add("prebuild/" + rel)
    return out


def seal_written_paths():
    """Return the `prebuild/...` files the seal job CREATES, not downloads.

    The seal writes its own `artifact.json` into the unpack directory and then
    reads it back in three later steps. Those are OUTPUTS of this job, so they
    are legitimately absent from the uploader's `path:` list -- and a guard
    that treats every `prebuild/...` string as a downloaded input would flag
    the job's own result as a broken path, which would push the next reader
    toward "fixing" it into the upload list.

    DERIVED, NOT HARDCODED. The name is recovered from the seal step's own
    `os.path.join('prebuild', ...)` construction rather than written out here,
    so renaming the artifact does not turn this into a stale literal -- which
    is the same mistake this check exists to catch in the workflows.
    """
    written = set()
    for _job, _step, run, _sd in W.iter_run_blocks(SEAL_WORKFLOW):
        for match in re.finditer(
                r"\.join\(\s*'prebuild'\s*,\s*'([^']+)'\s*\)", run):
            written.add("prebuild/" + match.group(1))
        for match in re.finditer(
                r"open\(\s*'prebuild/([A-Za-z0-9_.-]+)'\s*,\s*'w", run):
            written.add("prebuild/" + match.group(1))
    return written


def seal_consumer_paths():
    """Return every 'prebuild/...' path the seal job READS OUT of the artifact.

    Covers the three Python constants and every shell-side reader, because
    the shell readers were part of the same defect: they are reached only on
    the fallback path (no DIGEST_PASSWORD secret), so a fix that corrected
    the heredoc but not the shell would stay green here and stay broken in
    production the first time that secret is absent.

    Files the seal job writes itself are excluded -- see `seal_written_paths`.
    """
    written = seal_written_paths()
    found = set()
    wf = W.load_workflow(SEAL_WORKFLOW)
    for _job, _step, run, _sd in W.iter_run_blocks(SEAL_WORKFLOW):
        for match in re.finditer(r"prebuild/[A-Za-z0-9_./-]*", run):
            found.add(match.group(0).rstrip("/'\"`"))
    return sorted(found - written)


def layout_defects(entries, consumers):
    """Return the list of disagreements between uploader and consumers.

    THE single predicate check 20 applies. Factored out so the mutation
    proof below can feed it a deliberately wrong consumer set and watch it
    come back non-empty, instead of asserting a hand-written string.

    THE PREDICATE IS NOW LAYOUT-AGNOSTIC. A consumer path is a defect when it
    is not one of the paths the unpack will create. The earlier version
    hardcoded the flat shape -- it compared a consumer against
    `prebuild/<leaf>` and `prebuild/<root>/<leaf>` -- so it could only ever
    describe a single-level archive. With a workspace-root LCA the landed
    paths carry their own directories and `prebuild/<root>/<leaf>` IS the
    correct answer, so the old predicate would have reported every correct
    reader as broken (and, worse, reported every WRONG reader as fine,
    because `landed == wrong` collapses when the root is empty).

    A consumer that names a DIRECTORY rather than a file -- the seal builds
    the page's path from `today` and `slot`, so its literal is the directory
    and the filename is formatted in -- is checked as a directory prefix.
    """
    landed = unpacked_paths(entries)
    failures = []
    for consumer in consumers:
        if consumer in landed:
            continue
        if any(path.startswith(consumer.rstrip("/") + "/") for path in landed):
            continue  # a directory the unpack really will populate
        leaf = consumer.rsplit("/", 1)[-1]
        same_named = sorted(p for p in landed if p.rsplit("/", 1)[-1] == leaf)
        detail = ("%s unpacks to %s" % (leaf, ", ".join(same_named))) if same_named \
            else "no uploaded file unpacks to a path ending in %r" % leaf
        failures.append(
            "%s is read, but the uploader never creates it: %s. Read through "
            "%s instead. A consumer naming a location the unpack does not "
            "produce is the exact defect that failed the seal job 11 "
            "consecutive times -- every build succeeded, every artifact "
            "uploaded correctly, and the seal simply looked in the wrong place."
            % (consumer, detail, ", ".join(sorted(landed)) or "<nothing>")
        )
    return failures



class ArtifactLayoutAgreementTests(WorkflowLintBase):
    """Check 20. Fails closed if the consumer and the producer disagree."""

    def test_the_unpack_location_is_derived_from_the_uploader(self):
        """The derivation itself, asserted against the real artifact.

        Verified by unzipping a real artifact from this pipeline: run
        11733181570's zip holds `email.html` and `stories.validated.json` as
        bare entries. So the derived root below must be `state` and the
        consumer paths must carry no `state/` segment.
        """
        entries = upload_artifact_path_entries()
        root = upload_archive_root(entries)
        self.assertTrue(
            entries and all("/" in e for e in entries),
            "sanity: the upload `path:` entries were expected to be "
            "directory-qualified, got %r" % (entries,),
        )
        # A non-empty root must be a real leading directory of every entry --
        # it is their LCA. An EMPTY root is the workspace root, which is a
        # real answer rather than a failed derivation, and it is the answer
        # the list gives now that it spans `state/` and `docs/digests/`.
        if root:
            for entry in entries:
                self.assertIn(root, entry.split("/"),
                              "sanity: the derived archive root %r must be a "
                              "real leading directory of the upload entry %r "
                              "(entries=%r)" % (root, entry, entries))
        else:
            self.assertTrue(
                len({e.split("/")[0] for e in entries}) > 1,
                "sanity: the derived archive root is the workspace root, which "
                "is only correct when the entries do NOT all share a leading "
                "directory. All of them start with the same segment, so the "
                "LCA is that segment and this is a broken derivation, not a "
                "workspace root. Entries=%r" % (entries,),
            )
        # Whatever the root, every entry must unpack INSIDE `prebuild/`.
        for path in sorted(unpacked_paths(entries)):
            self.assertTrue(
                path.startswith("prebuild/") and len(path) > len("prebuild/"),
                "sanity: %r is uploaded, so its unpacked path must be a real "
                "path under prebuild/, not %r" % (path, path),
            )

    def test_every_seal_reader_uses_the_derived_archive_root(self):
        """THE CONTRACT. This is what would have caught the outage.

        Asserted as CONSISTENCY: the consumer must reference each payload
        file at the location the uploader's own `path:` list implies. No
        literal 'state/' is asserted anywhere, so adding a sixth upload
        entry, or moving the files into a differently-named directory, is
        handled by this test automatically instead of silently re-encoding
        the defect as a hardcoded expectation.
        """
        entries = upload_artifact_path_entries()
        consumers = seal_consumer_paths()
        self.assertTrue(consumers, "no prebuild/ consumer paths were found in "
                                   "%s, so this check is vacuous" % SEAL_WORKFLOW)
        self.assertTrue(
            any("digests" in c for c in consumers),
            "sanity: the seal job is expected to read the RENDERED PAGE from "
            "the unpacked artifact, so one of its paths must name the digests "
            "directory. Found %r" % (consumers,),
        )

        failures = layout_defects(entries, consumers)
        self.assertEqual(
            [], failures,
            "seal-job paths disagree with the artifact layout the uploader "
            "produces:\n  " + "\n  ".join(failures),
        )

    def test_no_seal_reader_assumes_a_flat_unpack(self):
        """Fail closed on the specific shape, independent of the leaf names.

        Belt and braces on top of the consistency check above. The defect in
        this repository has always been the same mistake in opposite
        directions -- a reader that adds a directory the unpack does not
        create, or (now that the LCA is the workspace root) one that drops a
        directory the unpack DOES create because it remembers the older flat
        layout. Both are "this path is not a path the unpack produces", so
        this asserts the flat assumption directly and by shape.

        It is deliberately not phrased as "no reader re-adds the archive root
        prefix": that phrasing only means anything when the LCA is a real
        directory, and the LCA is now the workspace root, so it would have
        skipped itself out of existence exactly when the layout changed.
        """
        landed = unpacked_paths(upload_artifact_path_entries())
        offenders = [
            c for c in seal_consumer_paths()
            if c not in landed
            and not any(p.startswith(c.rstrip("/") + "/") for p in landed)
            and not c.endswith("/")
        ]
        self.assertEqual(
            [], offenders,
            "%s reads %d path(s) the uploader never creates. The unpack mirrors "
            "the upload `path:` list from the archive root down, so every real "
            "path is one of: %s"
            % (SEAL_WORKFLOW, len(offenders), ", ".join(sorted(landed))),
        )

    def test_the_pre_fix_shape_is_rejected(self):
        """MUTATION PROOF, EXECUTED. The gate must reject the broken layout.

        Makes every consumer assume the artifact unpacks FLAT -- strip the
        directory from every path, which is precisely the shape the shipped
        workflow had when it read `prebuild/email.html` for a page that is
        uploaded under `docs/digests/` -- then runs the SAME `layout_defects`
        predicate the real check uses. A consistency check that has never
        been seen red is decoration, and a bug in this repo shipped twice on
        code reading alone while production was dead -- so the mutation is
        executed here, not described in a comment.

        THE DIRECTION OF THE MUTATION IS DERIVED, NOT CHOSEN. The old
        mutation re-added the archive root, which is only a defect when the
        LCA is a real directory: under a workspace-root LCA
        `prebuild/state/ntfy_payload.json` is the CORRECT path, so that
        mutation would have been a no-op and the proof would have been
        vacuous. Flattening is the direction that is wrong under every LCA.
        """
        entries = upload_artifact_path_entries()
        landed = unpacked_paths(entries)
        consumers = seal_consumer_paths()
        files = sorted(c for c in consumers
                       if c in landed or not any(
                           p.startswith(c.rstrip("/") + "/") for p in landed))

        mutated = []
        for c in files:
            rest = c[len("prebuild/"):]
            flattened = "prebuild/" + rest.split("/", 1)[1] if "/" in rest else c
            mutated.append(flattened)
        self.assertNotEqual(
            files, mutated,
            "sanity: the flattening mutation changed nothing, so it proves "
            "nothing. Consumers=%r" % (consumers,),
        )
        self.assertTrue(
            mutated,
            "sanity: the flattening mutation produced no consumer paths at "
            "all, so there is nothing for the predicate to reject. "
            "Consumers=%r" % (consumers,),
        )
        # Every mutated path must be exactly one directory level shallower
        # than the real one. Asserted structurally rather than against a
        # hardcoded name, because which file a seal reads changes whenever
        # the artifact changes -- a literal here would go stale and start
        # passing for the wrong reason.
        for c, m in zip(files, mutated):
            self.assertEqual(
                len(c.split("/")) - 1, len(m.split("/")),
                "sanity: flattening %r produced %r, which is not one level "
                "shallower" % (c, m),
            )
            self.assertNotIn(m, landed,
                             "sanity: flattening produced %r, which is a REAL "
                             "unpacked path, so the mutation is not a defect "
                             "at all" % m)

        failures = layout_defects(entries, mutated)
        self.assertTrue(
            failures,
            "layout_defects accepted the flat-unpack consumer paths %r. This "
            "is the shape that failed the seal job 11 consecutive times, so a "
            "predicate that cannot reject it would not have caught the outage "
            "and cannot be trusted to catch the next one." % (mutated,),
        )
        print("check 20 mutation proof: the pre-fix shape is REJECTED by the "
              "same predicate that accepts the real layout (%d defect(s): %s)"
              % (len(failures), failures[0]))


# ==========================================================================
# EVERY DISPATCHER OF daily-news.yml NAMES THE SLOT'S DATE (2026-10-18).
#
# A dispatch that cannot say which slot it means leaves the receiving workflow
# to read the date off the runner's own wall clock -- and a dispatch accepted
# before Nepal midnight can START after it. Run 2026-10-17T18:16:53Z is exactly
# that failure: 00:01 NPT on the 18th, dispatched for `evening`, dated itself the
# 18th, and pushed that slot's digest 19 hours before it was due.
#
# FOUR sites dispatch daily-news.yml, and the defect was that ANY of them could
# be date-less. So this is one scan over the whole SET rather than four
# assertions beside four call sites: a fifth dispatcher is covered the moment it
# is written, and none of them can drift back unnoticed.
# ==========================================================================

#: How a workflow dispatches daily-news.yml. Both forms are in live use.
DISPATCH_MARKERS = (
    "actions/workflows/daily-news.yml/dispatches",
    "gh workflow run daily-news.yml",
)

#: How far below a dispatch call its `-f` arguments may sit.
DISPATCH_ARG_WINDOW = 12


class EveryDispatcherOfDailyNewsNamesTheSlotDateTests(unittest.TestCase):
    def _dispatch_sites(self):
        """Yield (workflow, line_number, block) for every dispatch site.

        COMMENTS ARE STRIPPED FIRST. `actions: write   # gh workflow run
        daily-news.yml` in a permissions block is not a dispatch, and a scan
        that cannot tell the two apart would fail on a comment.
        """
        for name in W.WORKFLOW_NAMES:
            if name == "daily-news.yml":
                continue
            lines = W.raw_workflow_text(name).split("\n")
            for idx, line in enumerate(lines):
                code_part = line.split("#", 1)[0]
                if any(marker in code_part for marker in DISPATCH_MARKERS):
                    yield name, idx + 1, "\n".join(
                        lines[idx:idx + DISPATCH_ARG_WINDOW])

    def test_the_scan_finds_every_dispatch_site(self):
        """Guard the guard: a scan that matches nothing proves nothing.

        TWO sites remain, and both are real: digest-prebuild.yml's supersede and
        digest-watchdog.yml's retry dispatch. The count is asserted EXACTLY, not
        as a floor with slack, so losing either goes red here.

        It was THREE until 2026-10-20 (item A). The third was digest-prebuild.yml's
        own dispatch of daily-news.yml, and it went with the `dispatch` job that
        held it: the build now hands the seal its own run id, so nothing in
        digest-prebuild.yml dispatches daily-news.yml any more. The hand-off that
        replaced it lives in daily-news.yml and dials digest-prebuild.yml, which
        this census does not count -- it counts dispatchers OF daily-news.yml.
        drift-canary.yml's fourth site was removed on 2026-10-19 for Actions
        minutes.
        """
        sites = list(self._dispatch_sites())
        for name, line, _block in sites:
            print("slot-date dispatch scan: %s:%d" % (name, line))
        self.assertEqual(
            2, len(sites),
            "the scan found %d dispatch site(s) of daily-news.yml across %r. "
            "Two are known and both are real (digest-prebuild.yml x1, "
            "digest-watchdog.yml x1). A different count means either the scan is "
            "broken or a dispatcher was added or deleted without this test "
            "following." % (len(sites), W.WORKFLOW_NAMES))

    def test_every_dispatch_of_daily_news_carries_slot_date(self):
        for name, line, block in self._dispatch_sites():
            with self.subTest(workflow=name, line=line):
                self.assertIn(
                    "slot_date", block,
                    "%s:%d dispatches daily-news.yml without a slot_date, so the "
                    "receiving run dates the digest off its own clock. A "
                    "dispatch accepted before Nepal midnight can start after it "
                    "and file the slot under the next day -- run "
                    "2026-10-17T18:16:53Z pushed the 18th's evening digest 19 "
                    "hours early that way. Block:\n%s" % (name, line, block))

    def test_the_worker_dispatch_helper_carries_slot_date(self):
        """The fourth dispatcher is not a workflow, so the scan cannot see it."""
        worker = (Path(__file__).resolve().parents[2]
                  / "deploy" / "cloudflare-worker.js").read_text(encoding="utf-8")
        self.assertIn(
            "slot_date: slotDate,", worker,
            "the Worker's dispatchDigest does not send slot_date, so a dispatch "
            "accepted before Nepal midnight hands daily-news.yml a clock that "
            "may already be on the next day")


# ==========================================================================
# Check 21 -- the Actions minute budget (scripts/check_actions_budget.py)
# ==========================================================================


def _load_budget_checker():
    """Import scripts/check_actions_budget.py by path, without side effects."""
    import importlib.util
    path = W.REPO_ROOT / "scripts" / "check_actions_budget.py"
    spec = importlib.util.spec_from_file_location("_lint_actions_budget",
                                                 str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ActionsBudgetTests(unittest.TestCase):
    """The budget gate, exercised on SYNTHETIC trees, and why.

    On 2026-10-18 the account was refused at runner-assignment: about 2,016
    Actions minutes had been consumed by the 18th, i.e. about 3,360 in a month
    against an allowance of 2,000, and the owner cannot buy more. The gates
    worth having are the ones that stop it happening again, and there are two:
    this check (what the tree DECLARES) and budget-sentinel.yml (what the
    account really spent).

    THE REAL TREE IS OVER THE CAP TODAY, ON PURPOSE, SO THAT IS NOT WHAT THESE
    TESTS ASSERT. Cutting Drift Canary's dry-run dispatch and the two backstop
    crons took the projection from 3,777 to 3,078 minutes a month; the cap is
    1,400. A test that demanded "the real tree passes" would therefore have to
    assert something false, or be red until the remaining work lands -- and a
    permanently red gate is one the owner learns to ignore, which is the failure
    this repository already documents in digest-watchdog.yml about its own
    worker arm. So the behaviour is tested where it can be decided honestly, on
    synthetic trees, and the real tree gets the ratchet instead: its computed
    projection must equal the number committed in config/actions-budget.json, so
    a new cron, a fifth job or a raised volume goes red until somebody edits the
    budget on purpose.

    Each test below is backed by a mutation proof in the session that added it:
    the guard was broken, the test went red, the guard was restored.
    """

    DAYS = 30

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="actions-budget-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.dir = Path(self._tmp) / "workflows"
        self.dir.mkdir()
        self.check = _load_budget_checker()
        # The check resolves its tree from a module constant, which is what
        # makes a synthetic tree possible at all: the PARSER is still the
        # repository's own, so a cron is read exactly as the lint reads it.
        self.addCleanup(setattr, self.check, "WORKFLOW_DIR", self.check.WORKFLOW_DIR)
        self.check.WORKFLOW_DIR = self.dir

    def _workflow(self, name, cron=None, jobs=1):
        lines = ["name: %s" % name, "on:"]
        if cron:
            lines += ["  schedule:", "    - cron: '%s'" % cron]
        lines += ["  workflow_dispatch:", "jobs:"]
        for index in range(jobs):
            lines += ["  job%d:" % index, "    runs-on: ubuntu-latest",
                      "    steps:", "      - name: step", "        run: echo ok"]
        (self.dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _budget(self, entries, cap=1300.0, projected=None, reserve=100.0,
                included=1400.0, ceiling=None, budget_reserve=0.0):
        return {
            "included_minutes": included,
            "other_repos_reserve_minutes": reserve,
            "design_target_minutes": cap,
            "repo_soft_cap_minutes": cap,
            "budget_reserve_minutes": budget_reserve,
            "hard_ceiling_minutes": (included - budget_reserve
                                     if ceiling is None else ceiling),
            "warn_ratio": 0.8,
            "freeze_ratio": 0.9,
            "days_per_month": self.DAYS,
            "current_projected_minutes": projected,
            "workflows": entries,
        }

    def _entry(self, runs, jobs, p95, crons):
        return {"max_runs_per_day": runs, "jobs_per_run": jobs,
                "p95_minutes": p95, "crons": crons}

    def _problems(self, budget):
        rows, total = self.check.projects(budget)
        return self.check.problems(budget, rows, total), total

    def test_a_tree_within_the_cap_is_accepted(self):
        """The one test that must pass, and the per-job rule inside it.

        Actions bills PER JOB, so a two-job workflow costs twice what the same
        run costs with one. A check that multiplied by runs and minutes alone
        would understate every multi-job workflow in this repository -- which is
        why the expected total is written out here as arithmetic rather than
        compared to whatever the code returns.
        """
        self._workflow("cheap.yml", cron="20 */6 * * *", jobs=2)
        budget = self._budget(
            {"cheap.yml": self._entry(4, 2, 5.0, ["20 */6 * * *"])},
            projected=1200.0)
        problems, total = self._problems(budget)
        self.assertEqual(1200.0, total,
                         "4 runs x 2 jobs x 5.0 min x 30 days is 1200.0 "
                         "minutes; the check computed %.1f" % total)
        self.assertEqual([], problems, "\n  ".join(problems))

    def test_a_tree_over_the_design_target_is_warned_but_NOT_rejected(self):
        """Over the DESIGN TARGET is reported, never enforced (2026-10-20).

        The design target is an aspiration: refusing at it would stop work
        while the allowance still had headroom. Only the HARD CEILING fails.
        """
        self._workflow("cheap.yml", cron="20 */6 * * *", jobs=2)
        budget = self._budget(
            {"cheap.yml": self._entry(4, 2, 5.0, ["20 */6 * * *"])},
            cap=1000.0, projected=1200.0)
        problems, total = self._problems(budget)
        self.assertEqual([], problems,
                         "over the design target must NOT fail: %s"
                         % "\n  ".join(problems))
        warns = self.check.warnings(budget, total)
        self.assertEqual(1, len(warns), "\n  ".join(warns))
        self.assertIn("DESIGN TARGET", warns[0])

    def test_a_tree_over_the_hard_ceiling_IS_rejected(self):
        """The ENFORCED number: the allowance minus the reserve."""
        self._workflow("cheap.yml", cron="20 */6 * * *", jobs=2)
        budget = self._budget(
            {"cheap.yml": self._entry(4, 2, 5.0, ["20 */6 * * *"])},
            cap=1000.0, projected=1200.0, included=1100.0, budget_reserve=0.0)
        problems, _total = self._problems(budget)
        self.assertEqual(1, len(problems), "\n  ".join(problems))
        self.assertIn("hard ceiling", problems[0])

    def test_a_scheduled_workflow_with_no_budget_entry_is_rejected(self):
        """The failure that let the account reach 2,016 minutes by the 18th.

        A workflow absent from the budget is a cost absent from the projection.
        The forward direction matters as much as the cap: an unbudgeted file is
        exactly how `budget-sentinel.yml` itself would have slipped in.
        """
        self._workflow("unbudgeted.yml", cron="0 3 * * *", jobs=1)
        budget = self._budget({}, projected=0.0)
        problems, _total = self._problems(budget)
        self.assertEqual(1, len(problems), "\n  ".join(problems))
        self.assertIn("no entry", problems[0])
        self.assertIn("unbudgeted.yml", problems[0])

    def test_a_cron_change_without_the_budget_following_is_rejected(self):
        """A budget sized for one schedule does not cover another."""
        self._workflow("moved.yml", cron="0 9 * * *", jobs=1)
        budget = self._budget(
            {"moved.yml": self._entry(1, 1, 1.0, ["0 3 * * *"])},
            projected=30.0)
        problems, _total = self._problems(budget)
        self.assertEqual(1, len(problems), "\n  ".join(problems))
        self.assertIn("declares crons", problems[0])

    def test_an_entry_billing_more_jobs_than_the_file_declares_is_rejected(self):
        self._workflow("onejob.yml", cron="0 9 * * *", jobs=1)
        budget = self._budget(
            {"onejob.yml": self._entry(1, 2, 1.0, ["0 9 * * *"])},
            projected=60.0)
        problems, _total = self._problems(budget)
        self.assertEqual(1, len(problems), "\n  ".join(problems))
        self.assertIn("bills 2 job(s)", problems[0])

    def test_the_recorded_projection_must_match_the_tree(self):
        """The ratchet: a volume cannot be raised or lowered quietly."""
        self._workflow("cheap.yml", cron="20 */6 * * *", jobs=1)
        budget = self._budget(
            {"cheap.yml": self._entry(4, 1, 5.0, ["20 */6 * * *"])},
            projected=100.0)
        problems, total = self._problems(budget)
        self.assertEqual(600.0, total)
        self.assertEqual(1, len(problems), "\n  ".join(problems))
        self.assertIn("records a projection", problems[0])

    def test_a_hard_ceiling_that_is_not_allowance_minus_reserve_is_rejected(self):
        """The hard ceiling is DEFINED as the allowance minus the reserve.

        If the two stop summing to the allowance, the ceiling is no longer the
        margin-protected number and the invariant that keeps this repository
        from eating the reserve silently stops holding.
        """
        budget = self._budget({}, cap=1200.0, included=1350.0, projected=0.0,
                              ceiling=1200.0, budget_reserve=0.0)
        problems, _total = self._problems(budget)
        self.assertEqual(1, len(problems), "\n  ".join(problems))
        self.assertIn("hard ceiling", problems[0])
        self.assertIn("reserve", problems[0])

    def test_cron_fires_per_day_counts_hours_and_minutes_separately(self):
        """`*/6` is FOUR values in the hour field and ten in the minute field.

        Getting this wrong is easy and silent: the first version of this
        arithmetic divided 60 by the step regardless of the field, so
        `20 */6 * * *` read as ten firings a day instead of four -- a number
        nobody would have questioned in the output table.
        """
        self.assertEqual(4, self.check.cron_fires_per_day("20 */6 * * *"))
        self.assertEqual(24, self.check.cron_fires_per_day("17 * * * *"))
        self.assertEqual(2, self.check.cron_fires_per_day("0 4,16 * * *"))
        self.assertEqual(72, self.check.cron_fires_per_day("17,32,47 * * * *"))
        self.assertEqual(1, self.check.cron_fires_per_day("5 2 * * *"))
        self.assertIsNone(
            self.check.cron_fires_per_day("5 13 1 * *"),
            "a cron restricting the day of the month cannot be counted as "
            "firings per day, and guessing would be worse than refusing")

    def test_the_real_tree_matches_the_projection_it_records(self):
        """The ratchet on the real tree, and the honest statement of the gap.

        A fresh loader, so this reads the REAL .github/workflows: the value
        committed in config/actions-budget.json must equal what this tree
        computes. That is what makes a new cron, an extra job or a raised
        `max_runs_per_day` a deliberate edit rather than a quiet drift.

        The gap to the cap is PRINTED rather than asserted. It is real, it is
        recorded in the budget file, and the work that closes it is out of
        scope for this change -- so asserting the tree passes would be a lie,
        and asserting the tree stays red would freeze a number that is meant to
        fall.
        """
        check = _load_budget_checker()
        budget = check.load_budget()
        rows, total = check.projects(budget)
        recorded = float(budget["current_projected_minutes"])
        cap = float(budget["repo_soft_cap_minutes"])
        # The same half-minute tolerance the check itself uses: the projection
        # is a sum of floats (6.1 and 2.4 are not exact in binary), so the
        # comparison has to be at a resolution smaller than one billed minute
        # rather than at floating-point equality.
        self.assertAlmostEqual(
            recorded, total, delta=0.5,
            msg="config/actions-budget.json records %s minutes a month but this "
            "tree computes %.0f. Edit the budget on purpose, or find what "
            "moved: a cron, a job count or a declared volume." % (recorded, total))
        unbudgeted = [p for p in check.problems(budget, rows, total)
                      if "no entry" in p]
        self.assertEqual(
            [], unbudgeted,
            "a workflow file exists that the budget does not mention, so its "
            "cost is missing from the projection: %s" % (unbudgeted,))
        ceiling = float(budget["hard_ceiling_minutes"])
        print("check 21: %d workflow(s) projected at %.0f min/month against the "
              "hard ceiling of %.0f (%+.0f) and the design target of %.0f (%+.0f); "
              "measured October was ~3,360"
              % (len(rows), total, ceiling, ceiling - total, cap, cap - total))

    def test_the_enforcement_switch_ships_off_and_the_worker_agrees(self):
        """The runtime quota ships DISARMED, and both copies of the flag agree.

        The repository is now PUBLIC, where standard runners are free, so a
        refusal at the ceiling saves nothing and can only cost a digest. The
        arithmetic, the charge and the reports are unchanged -- only `enabled` is
        false. Asserted here rather than described, because a sentence cannot stop
        config/actions-budget.json and the deployed Worker disagreeing.
        """
        check = _load_budget_checker()
        budget = check.load_budget()
        enforcement = budget.get("enforcement") or {}
        self.assertFalse(
            bool(enforcement.get("enabled")),
            "the Worker's quota must ship DISARMED: this repository is public, so "
            "its Actions minutes are free and a refusal can only cost a digest")
        self.assertTrue(
            str(enforcement.get("reason") or "").strip(),
            "a disabled switch must state WHY, or it reads as an accident")
        worker = (W.REPO_ROOT / "deploy" / "cloudflare-worker.js").read_text(
            encoding="utf-8")
        problems = worker_budget_constants_problems(budget, worker)
        self.assertEqual([], problems, "\n  ".join(problems))
        print("check 21: the Worker's enforcement switch is %r (%s)"
              % (enforcement.get("enabled"), enforcement.get("reason")))

    def test_a_one_sided_enforcement_flip_is_rejected(self):
        """MUTATION PROOF: flip the switch on ONE side and the gate must catch it.

        This is the check that stops the whole feature becoming a comment. The
        flip is made on an in-memory COPY of the real files -- nothing on disk is
        touched -- in both directions, because either copy drifting alone is the
        failure mode.
        """
        check = _load_budget_checker()
        budget = check.load_budget()
        worker = (W.REPO_ROOT / "deploy" / "cloudflare-worker.js").read_text(
            encoding="utf-8")
        self.assertEqual(
            [], worker_budget_constants_problems(budget, worker),
            "sanity: the committed pair must agree before a mutation proves anything")
        flipped = worker.replace("const BUDGET_ENFORCEMENT_ENABLED = false;",
                                 "const BUDGET_ENFORCEMENT_ENABLED = true;")
        self.assertNotEqual(
            worker, flipped,
            "the Worker no longer declares the flag in the shape this mutation "
            "assumes; the mutation must be updated with it")
        problems = worker_budget_constants_problems(budget, flipped)
        self.assertTrue(problems, "a flag flipped only in the Worker was accepted")
        self.assertIn("enforcement.enabled", "\n".join(problems))
        other = json.loads(json.dumps(budget))
        other["enforcement"]["enabled"] = True
        problems = worker_budget_constants_problems(other, worker)
        self.assertTrue(problems, "a flag flipped only in the budget file was accepted")
        self.assertIn("enforcement.enabled", "\n".join(problems))
        print("check 21 mutation proof: a one-sided enforcement flip is rejected in "
              "BOTH directions")


# ==========================================================================
# Public sanitisation gate (2026-10-25)
# ==========================================================================


def _load_sanitisation_gate():
    """Import the gate as a module, so its report path can be exercised directly."""
    import importlib.util

    path = W.REPO_ROOT / "scripts" / "check_public_sanitisation.py"
    spec = importlib.util.spec_from_file_location("check_public_sanitisation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_sanitisation_gate(root):
    import subprocess

    script = W.REPO_ROOT / "scripts" / "check_public_sanitisation.py"
    return subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        cwd=str(W.REPO_ROOT), capture_output=True, text=True)


class PublicSanitisationTests(unittest.TestCase):
    """The published tree carries no private value, and the gate can prove it.

    WHY THIS CLASS EXISTS
    ---------------------
    On 2026-10-25 the Worker was regenerated by copying the private file over the
    published one. The Worker's own harness caught the Pages origin; nothing
    looked at the rest of the tree, so the district names that had come with it
    were found by a manual grep afterwards. The gate is that grep, made
    repeatable, and this class is what keeps it wired in: a gate registered in no
    suite is a file, not a check.

    The vocabulary is not written out here, and not written out in the gate
    either: the gate holds SHA-256 fingerprints. These tests therefore prove the
    MECHANISM with a synthetic fingerprint of their own, prove the real tree is
    clean against the committed set, and pin the structural rule that no plaintext
    and no decodable encoding may come back into the gate.
    """

    def test_the_published_tree_carries_no_private_value(self):
        proc = _run_sanitisation_gate(W.REPO_ROOT)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("public sanitisation clean", proc.stdout)
        self.assertNotIn("sha256=", proc.stdout,
                         "a clean run must print no fingerprint at all")
        match = re.search(r"clean: (\d+) file", proc.stdout)
        self.assertIsNotNone(match, "the gate stopped reporting a scan count: %r"
                             % proc.stdout)
        # A loose FLOOR, not a pinned count -- the same rule the rest of this
        # file follows. A gate that scanned three files would print "clean" and
        # prove nothing.
        scanned = int(match.group(1))
        self.assertGreater(
            scanned, 100,
            "the sanitisation gate scanned only %d file(s), so it is not reading "
            "the tree it claims to check" % scanned)
        print("public sanitisation: %d file(s) clean" % scanned)

    def test_the_gate_names_file_line_and_never_echoes_the_value(self):
        """MUTATION PROOF: the gate must bite, and must bite quietly.

        A synthetic token is used, not a real one: the public tree has no real
        value to inject and must never gain one. What is proved here is the
        machinery -- exit 1, `file:line`, the hash in the report, and the value
        itself absent from the output, because that output goes into CI logs.
        """
        import contextlib
        import hashlib
        import io

        gate = _load_sanitisation_gate()
        secret = "zzqqxxnotarealvocabularyword"
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            target = root / "deploy" / "cloudflare-worker.js"
            target.parent.mkdir(parents=True)
            target.write_text(
                'const A = 1;\nconst B = 2;\nconst C = "%s";\n' % secret,
                encoding="utf-8")
            original = gate.VOCABULARY_SET
            try:
                gate.VOCABULARY_SET = frozenset({digest})
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = gate.main(["--root", str(root)])
            finally:
                gate.VOCABULARY_SET = original
            output = buffer.getvalue()

        self.assertEqual(1, code, output)
        self.assertIn("PUBLIC SANITISATION FAILED", output)
        self.assertIn("deploy/cloudflare-worker.js:3", output,
                      "the gate must name the file AND the line, or the next "
                      "person greps the whole tree by hand")
        self.assertIn(digest, output, "the report must carry the fingerprint")
        self.assertNotIn(secret, output,
                         "the gate printed the value it matched; that output "
                         "lands in CI logs and publishes the thing it guards")
        print("mutation proof: rejected at deploy/cloudflare-worker.js:3, "
              "fingerprint reported, value never printed")

    def test_the_vocabulary_is_fingerprints_and_nothing_decodable(self):
        """The rule that stops 2026-10-25 recurring: no map, of any encoding.

        The first version stored the vocabulary base64-encoded in this tree. One
        line reverses base64, so that version published the map it was built to
        remove. This asserts the gate holds only SHA-256 hex, and that no base64
        machinery or encoded blob has come back.
        """
        gate = _load_sanitisation_gate()
        self.assertGreaterEqual(len(gate.VOCABULARY_SHA256), 3,
                                "a vocabulary of fewer than 3 fingerprints is a "
                                "gate that has lost its coverage quietly")
        for entry in gate.VOCABULARY_SHA256:
            self.assertRegex(entry, r"^[0-9a-f]{64}$",
                             "every vocabulary entry must be a bare SHA-256 hex "
                             "digest, never a token and never an encoding of one")
        source = (W.REPO_ROOT / "scripts" / "check_public_sanitisation.py").read_text(
            encoding="utf-8")
        # The prose is allowed to discuss base64 -- this file's docstring explains
        # why the first version was wrong. The MACHINERY is not: no decoder, no
        # encoded blob, no import that could carry a stored map back in.
        self.assertNotRegex(source, r"(?m)^\s*(import\s+base64|from\s+base64)",
                            "the gate must not import a decoder; a stored map "
                            "behind one is the mistake of 2026-10-25")
        self.assertNotIn("b64decode", source,
                         "the gate must not decode a stored map")
        self.assertNotIn("_ENCODED", source,
                         "no encoded vocabulary may live in this tree")
        long_literals = re.findall(r"[\"']([A-Za-z0-9+/]{40,}={0,2})[\"']", source)
        self.assertTrue(long_literals, "the fingerprints themselves disappeared")
        for literal in long_literals:
            self.assertRegex(literal, r"^[0-9a-f]{64}$",
                             "every long literal in the gate must be a bare "
                             "SHA-256 hex digest; anything else could be a stored "
                             "map in an encoding that decodes in one line")
        print("vocabulary: %d SHA-256 fingerprint(s), no base64 machinery, "
              "no plaintext" % len(gate.VOCABULARY_SHA256))

    def test_matching_is_whole_runs_so_innocuous_words_do_not_fire(self):
        """The false-positive rule, pinned so nobody 'improves' it to substrings.

        Substring matching produced 139 bogus hits (inside `administrator` and its
        relatives) and one inside a base64 image. Whole-run matching removes both,
        at the documented cost that a value glued into a longer run is not caught.
        That trade is asserted here rather than left to be rediscovered.
        """
        import hashlib

        gate = _load_sanitisation_gate()
        token = "zzqqxx"
        vocabulary = frozenset({hashlib.sha256(token.encode()).hexdigest()})
        self.assertEqual(
            [], list(gate.scan_text("administrator ministry finalists", vocabulary)),
            "an unrelated word must not match")
        self.assertEqual(
            [], list(gate.scan_text("9R/aa0" + token + "8JeKp/", vocabulary)),
            "a run containing the token as a substring must not match, or a "
            "committed base64 image turns the gate red")
        hits = list(gate.scan_text("x " + token + " y", vocabulary))
        self.assertEqual(1, len(hits), "the token as a whole run must match")
        print("whole-run matching: substring runs ignored, standalone run caught")


if __name__ == "__main__":
    unittest.main(verbosity=2)
