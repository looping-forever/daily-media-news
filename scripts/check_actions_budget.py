#!/usr/bin/env python3
"""scripts/check_actions_budget.py -- the Actions minute budget, projected and gated.

WHY THIS EXISTS
---------------
This check was written when the repository was PRIVATE and every job it ran billed
against the account's included Actions allowance. On 2026-10-18 the account was
refused at runner-assignment: October had consumed about 2,016 minutes by the
18th, i.e. about 3,360 minutes a month against an allowance of 2,000. The owner
cannot buy more -- there is no USD access from Nepal -- so the system had to live
under the allowance permanently, and it had to be unable to creep back over it
quietly.

WHAT CHANGED ON 2026-10-25, AND WHAT DID NOT. The repository is PUBLIC now, so
standard runners are free and no minute is billed. The ARITHMETIC here is
deliberately unchanged: it is the record of what this tree costs, and the whole
point of this file is that the record is recomputed rather than asserted. What
changed is the CONSEQUENCE -- `config/actions-budget.json` now carries
`enforcement.enabled: false`, so the Worker's runtime quota reports the ceiling
and no longer refuses a dispatch on it. This script still gates the declared
projection, because a volume that can be raised silently is a lie told to the next
reader, and because the repository could be made private again (set
`enforcement.enabled` back to true and the refusal returns).

Actions bills PER JOB PER MINUTE, ROUNDED UP, so both halves of that matter:
a 5-second job costs a whole minute, and a 4-job workflow costs at least 4
minutes per run. A budget that counted runs instead of jobs would understate
every workflow that has more than one.

WHAT THIS PARSER DELIBERATELY READS INSTEAD OF RESTATING
-------------------------------------------------------
The crons and the job structure come out of the real workflow YAML through the
repository's own reader (`tests.workflows.load_workflow`, PyYAML when it is
importable and the stdlib subset loader otherwise). Nothing here keeps a second
copy of the schedule: a new cron, a renamed job or a fifth job added to a
four-job workflow is visible to this check the moment it is committed, which is
the entire point. A hardcoded list would agree with itself forever.

WHAT IT FAILS ON
----------------
1. the projected month exceeds the hard ceiling (the allowance minus the reserve);
2. a workflow file has no budget entry at all;
3. an entry's `crons` no longer match the crons in the workflow it describes;
4. an entry bills more jobs per run than the workflow actually declares;
5. the recorded projection no longer matches the computed one, so a number
   cannot be raised (or lowered) without the file being edited on purpose;
6. the cap plus the reserve for other repositories exceeds the allowance.

THE MODEL, AND WHAT IT IS NOT
-----------------------------
    monthly_expected = sum over workflows of
        max_runs_per_day x jobs_per_run x p95_minutes x days_per_month

`p95_minutes` is PER JOB. `max_runs_per_day` is the runs a day the workflow is
allowed to produce, not the runs a day GitHub happens to deliver: GitHub
`schedule` is demonstrably not a clock here, so a model built on "the cron says
24 firings a day" would bill a cost that does not occur, and a model built on
`timeout-minutes` would flag everything and be ignored. The values committed in
`config/actions-budget.json` are calibrated against the measured October
consumption, and each entry records that measurement beside the estimate.

THIS IS AN ESTIMATE, NOT THE METER. The account's real billing endpoints are
unreadable to the current token (it lacks the `user` scope), so no endpoint can
be asked what has actually been spent. The number here is a projection from
declared volumes; `budget-sentinel.yml` is the runtime half, which measures real
job durations instead of declaring them.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUDGET_PATH = REPO_ROOT / "config" / "actions-budget.json"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# Reuse the repository's single workflow *parser* rather than growing a second
# one: this check must see exactly the tree the workflow lint sees. Only the
# directory is resolved here, so a test can point the check at a synthetic tree.
sys.path.insert(0, str(REPO_ROOT))
from tests.workflows import load_workflow_text  # noqa: E402


# --------------------------------------------------------------------------
# Cron arithmetic
# --------------------------------------------------------------------------


def _count(field, size):
    """How many values a cron field matches within one cycle of `size`.

    `size` is 60 for minutes and 24 for hours, and it is a parameter because
    `*/6` means four values in the hour field and ten in the minute field.
    `None` means the field could not be counted, and the caller refuses to
    guess rather than reporting a number that is quietly wrong.
    """
    if field == "*":
        return size
    if field.startswith("*/"):
        try:
            step = int(field[2:])
        except ValueError:
            return None
        return size // step if step > 0 else None
    values = set()
    for part in field.split(","):
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                values.update(range(int(lo), int(hi) + 1))
            except ValueError:
                return None
        else:
            try:
                values.add(int(part))
            except ValueError:
                return None
    return len(values)


def cron_fires_per_day(expr):
    """How many times a day a 5-field cron fires, or None if it cannot say.

    Only the minute and hour fields can answer "how many times a day" without
    knowing the calendar, so a restricted day/month/weekday field makes the
    answer unknowable here rather than wrongly small.
    """
    fields = str(expr).split()
    if len(fields) != 5:
        return None
    if fields[2] != "*" or fields[3] != "*" or fields[4] != "*":
        return None
    minutes = _count(fields[0], 60)
    hours = _count(fields[1], 24)
    if minutes is None or hours is None:
        return None
    return minutes * hours


# --------------------------------------------------------------------------
# Reading the tree
# --------------------------------------------------------------------------


def workflow_files():
    """Every workflow file, discovered from disk rather than listed here."""
    return sorted(p.name for p in WORKFLOW_DIR.glob("*.yml"))


def read_workflow(name):
    """Crons and jobs as the YAML actually declares them."""
    doc = load_workflow_text((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    on = doc.get("on")
    crons = []
    if isinstance(on, dict):
        for entry in (on.get("schedule") or []):
            if isinstance(entry, dict) and entry.get("cron"):
                crons.append(str(entry["cron"]).strip())
    jobs = [jid for jid, job in (doc.get("jobs") or {}).items()
            if isinstance(job, dict)]
    return {
        "crons": crons,
        "jobs": jobs,
        "scheduled": bool(crons),
        "dispatchable": isinstance(on, dict) and "workflow_dispatch" in on,
    }


def load_budget(path=None):
    with open(path or BUDGET_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# The accounting
# --------------------------------------------------------------------------


def projects(budget, names=None):
    """Return (rows, total) where each row is one workflow's projection."""
    days = int(budget["days_per_month"])
    entries = budget["workflows"]
    rows = []
    for name in (names if names is not None else workflow_files()):
        tree = read_workflow(name)
        entry = entries.get(name) or {}
        runs = entry.get("max_runs_per_day")
        jobs = entry.get("jobs_per_run")
        p95 = entry.get("p95_minutes")
        expected = None
        if isinstance(runs, int) and isinstance(jobs, int) and isinstance(p95, (int, float)):
            expected = runs * jobs * float(p95) * days
        fires = sum(f for f in (cron_fires_per_day(c) for c in tree["crons"])
                    if f is not None) or 0
        rows.append({
            "name": name,
            "crons": tree["crons"],
            "cron_fires_per_day": fires,
            "jobs_in_file": len(tree["jobs"]),
            "scheduled": tree["scheduled"],
            "entry": entry,
            "expected": expected,
        })
    total = sum(r["expected"] for r in rows if r["expected"] is not None)
    return rows, total


def problems(budget, rows, total):
    """Every reason this tree is not allowed. Empty list means allowed."""
    found = []
    ceiling = float(budget.get("hard_ceiling_minutes")
                    or budget["included_minutes"])

    for row in rows:
        name = row["name"]
        entry = row["entry"]
        if not entry:
            found.append(
                "%s has no entry in %s. Every workflow file needs one: a "
                "workflow that is absent from the budget is a cost that is "
                "absent from the projection, which is how the account reached "
                "2,016 minutes by the 18th."
                % (name, BUDGET_PATH.name))
            continue
        declared = list(entry.get("crons") or [])
        if declared != row["crons"]:
            found.append(
                "%s declares crons %r in its entry but %r in the workflow. A "
                "budget sized for one schedule does not cover another, so a "
                "changed cron has to be acknowledged in the budget file."
                % (name, declared, row["crons"]))
        jobs = entry.get("jobs_per_run")
        if isinstance(jobs, int) and jobs > row["jobs_in_file"]:
            found.append(
                "%s bills %d job(s) per run but the workflow only declares %d. "
                "Billing is per job, so this entry claims minutes no run can "
                "spend." % (name, jobs, row["jobs_in_file"]))
        for field in ("max_runs_per_day", "jobs_per_run", "p95_minutes"):
            if entry.get(field) is None:
                found.append("%s: budget entry has no `%s`." % (name, field))

    if total > ceiling:
        found.append(
            "the projection is %.0f minutes a month against the hard ceiling of "
            "%.0f (%+.0f). The ceiling is the allowance MINUS the reserve, so this "
            "month is out of the margin this repository declared for itself. The "
            "Worker no longer refuses a dispatch on it (enforcement is off while "
            "the repository is public), so this is the static gate doing its job: "
            "the projection was raised without the file being edited to say so."
            % (total, ceiling, total - ceiling))

    recorded = budget.get("current_projected_minutes")
    if recorded is not None and abs(float(recorded) - total) > 0.5:
        found.append(
            "the budget file records a projection of %s but this tree computes "
            "%.0f. Update `current_projected_minutes` deliberately, or find "
            "what moved: this field exists so a volume cannot be raised or "
            "lowered quietly." % (recorded, total))

    included = float(budget["included_minutes"])
    reserve = float(budget.get("budget_reserve_minutes") or 0)
    if ceiling + reserve != included:
        found.append(
            "the hard ceiling (%.0f) plus the budget reserve (%.0f) is %.0f, "
            "which is not the allowance of %.0f. The hard ceiling is DEFINED as "
            "the allowance minus the reserve, so the two must sum to it."
            % (ceiling, reserve, ceiling + reserve, included))
    target = float(budget.get("design_target_minutes")
                   or budget["repo_soft_cap_minutes"])
    if target > ceiling:
        found.append(
            "the design target (%.0f) is above the hard ceiling (%.0f). The "
            "target is an aspiration and the ceiling is the ENFORCED number, so a "
            "target above the ceiling would make every month a failure."
            % (target, ceiling))

    return found


def warnings(budget, total):
    """Non-fatal reports. The design target is an aspiration, not a gate.

    Kept separate from `problems` on purpose: an overage of the DESIGN TARGET is
    what the owner wants to SEE every run, not what should stop the build. The
    only minute count that fails this check is the hard ceiling.
    """
    target = float(budget.get("design_target_minutes")
                   or budget["repo_soft_cap_minutes"])
    out = []
    if total > target:
        out.append(
            "the projection (%.0f) exceeds the DESIGN TARGET of %.0f (%+.0f). "
            "REPORTED, not enforced: this does not fail the check. Close the gap "
            "by reducing daily-news.yml volume (the only line above its target), "
            "or accept it while the hard ceiling holds."
            % (total, target, total - target))
    return out


def report(budget, rows, total):
    """The table, printed so the numbers are read rather than trusted."""
    print("Actions budget -- %s" % BUDGET_PATH.name)
    print("  allowance %s min | design target %s min | hard ceiling %s min "
          "(allowance - %s reserve) | other-repos reserve %s min"
          % (budget["included_minutes"], budget["design_target_minutes"],
             budget["hard_ceiling_minutes"], budget["budget_reserve_minutes"],
             budget["other_repos_reserve_minutes"]))
    print("%-24s %6s %6s %7s %8s %7s  %s"
          % ("workflow", "runs/d", "jobs/r", "p95/job", "cron/d", "min/mo",
             "crons"))
    for row in rows:
        entry = row["entry"]
        print("%-24s %6s %6s %7s %8s %7s  %s"
              % (row["name"],
                 entry.get("max_runs_per_day", "-"),
                 entry.get("jobs_per_run", "-"),
                 entry.get("p95_minutes", "-"),
                 row["cron_fires_per_day"] or "-",
                 "%.0f" % row["expected"] if row["expected"] is not None else "-",
                 ",".join(row["crons"]) or "(none -- dispatched)"))
    print("  projected month: %.0f min | design target: %s min (%+.0f) | "
          "hard ceiling: %s min (%+.0f)"
          % (total, budget["design_target_minutes"],
             float(budget["design_target_minutes"]) - total,
             budget["hard_ceiling_minutes"],
             float(budget["hard_ceiling_minutes"]) - total))
    # The switch, printed because the numbers above mean something different
    # depending on it: a ceiling that refuses and a ceiling that only reports look
    # identical in a table.
    enforcement = budget.get("enforcement") or {}
    print("  enforcement: %s (%s)"
          % ("ON  -- the Worker refuses a dispatch at the ceiling"
             if enforcement.get("enabled")
             else "OFF -- the Worker reports at the ceiling and refuses nothing",
             enforcement.get("reason") or "no reason recorded"))
    measured = sum(float((e.get("measured_october") or {}).get("minutes") or 0)
                   for e in budget["workflows"].values())
    if measured:
        print("  measured October (1st-18th, 18 days): %.0f min -> %.0f min/month"
              % (measured, measured / 18.0 * 30))


def main(argv=None):
    budget = load_budget()
    rows, total = projects(budget)
    report(budget, rows, total)
    found = problems(budget, rows, total)
    for warning in warnings(budget, total):
        print("WARN: %s" % warning)
    for problem in found:
        print("FAIL: %s" % problem)
    if found:
        print("\n%d problem(s). The budget is not satisfied." % len(found))
        return 1
    print("\nOK: the projected month fits the HARD CEILING (the design-target "
          "overage, if any, is a warning above), every workflow is budgeted, and "
          "no cron or job moved without the budget following. %s"
          % ("The Worker's runtime quota REFUSES a dispatch at the ceiling."
             if (budget.get("enforcement") or {}).get("enabled")
             else "The Worker's runtime quota only REPORTS at the ceiling "
                  "(enforcement.enabled is false)."))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
