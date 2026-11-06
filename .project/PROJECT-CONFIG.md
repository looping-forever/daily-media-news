# PROJECT-CONFIG.md

Project identity for this ledger. One ledger belongs to exactly one project
root, and that root is recorded here. If you are reading this from a checkout of
a different repository, you are in the wrong ledger.

Do not edit the fenced block by hand and expect it to stay parsed: regenerate it
with `node <ledger>/src/cli.ts init` in a fresh ledger. Everything outside the
block is free-form prose for humans.

<!-- ledger:config:start -->
ledger_version: 1.0.0
schema_version: 1
project_name: dmnews-public
repo_root: C:\src\dmnews-public
created_at: 2026-11-06T17:49:15.564Z
created_by: user
git_policy: undecided
lock_stale_seconds: 900
duplicate_threshold: 0.55
default_verification_profile: module
<!-- ledger:config:end -->

## What each field is for

| Field | Meaning |
|---|---|
| `ledger_version` | version of the ledger library that wrote this ledger |
| `schema_version` | version of the record frontmatter contract; bumped on breaking field changes |
| `project_name` | human name of the project |
| `repo_root` | **absolute path of the repository this ledger belongs to.** Checked on every load; a mismatch is reported, never silently ignored |
| `created_at` | ISO timestamp of ledger initialisation |
| `created_by` | `user` or `agent` -- who ran `init` |
| `git_policy` | whether `.project/` is committed. Ships `undecided` — see the recommended policy below |
| `lock_stale_seconds` | a lock older than this with a live pid is still recoverable |
| `duplicate_threshold` | duplicate-detection score at which a candidate is reported |
| `default_verification_profile` | profile applied when a record does not declare one |

## Recommended policy — split: commit the record, ignore the derived

**Commit `.project/` records and history; ignore the generated artifacts, the
lockfile, and temp files.** That is the recommendation, and it is a default to
accept or overrule — not a rule the library enforces. `validate` does not fail on
`git_policy: undecided`, and nothing here stages, commits, or touches your Git
state.

**Why the split.** The two halves of `.project/` have opposite properties. Records
(`features/`, `bugs/`, `decisions/`, `milestones/`), `history/*.jsonl` and
`PROJECT-CONFIG.md` are the *reasoning*: requirements, why a decision was taken,
what verification evidence existed. That is exactly the content worth having in
version-controlled history, and it only pays off if a teammate, a second checkout,
or CI can read it. The other half — `PROJECT-STATUS.md` and `current-state.json` —
is **derived** from the records on every mutation. Committing it buys nothing and
costs a real thing: it rewrites on essentially every status change, so every
mutation shows up as a diff in files no human wrote, and two branches both moving
records guarantee a merge conflict in both. `.lock` and `.tmp` are transient by
definition: a committed lockfile is a lie that outlives the process that made it.

The cost of committing everything is mostly *noise*, not data loss — which is
precisely why the fix is a `.gitignore` rather than a policy change.

**Copy this into the project's root `.gitignore`.** The records and history stay
tracked; only the derived and transient files are excluded:

```gitignore
# Project ledger: commit records + history, ignore derived artifacts and
# transient state. The ledger regenerates these; never hand-edit them.
/.project/PROJECT-STATUS.md
.project/current-state.json
.project/.lock
.project/.tmp/
.project/*.tmp
```

`PROJECT-STATUS.md` and `current-state.json` stay fully available locally — anyone
can regenerate them with `status --write`, and `validate` still catches a stale one
in a checkout that lacks them.

**If you overrule it:** ignoring all of `.project/` gives a private ledger with no
branch conflicts, at the cost that no other agent, checkout or CI can see the state
at all. Setting `git_policy: ignored` (or your own word) on the line above records
that ruling. Nothing in the library reads this value to change behaviour — it is
recorded intent, so a reader knows the choice was deliberate. **All actual Git
state changes belong to `git-manager` and need explicit user approval for the exact
action**; writing a record's `milestone` field is not a Git operation, staging and
committing are.

**Why the shipped value is `undecided` rather than this recommendation.** `init`
hardcodes `git_policy: 'undecided'` (`templates.ts:108`) and the test suite asserts
it on purpose (`test/cases-t1-t9.ts:76` — *"git policy is left undecided rather than
decided for the owner"*). Making the library pick for you would silently answer a
question that is yours, so it ships the open value and states a recommendation here
instead. Record your ruling by editing that one line in **your own** ledger.
