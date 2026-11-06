# Project Ledger

This directory is the project's ledger: the durable, reviewable record of what
was decided, what is being built, what is blocked, and what was verified.

**Everything else in this directory is generated.** `PROJECT-STATUS.md` and
`current-state.json` are derived and regenerated on every mutation; never
hand-edit them. `history/*.jsonl` is append-only. The files under `features/`,
`bugs/`, `decisions/` and `milestones/` are the actual records, and they are the
only things you edit.

## Layout

```
.project/
  README.md            this file
  PROJECT-CONFIG.md    project identity: name, repo root, created, versions, git policy
  PROJECT-STATUS.md    GENERATED dashboard
  current-state.json   GENERATED machine snapshot
  features/F-001.md    one record per entity
  bugs/B-001.md
  decisions/D-001.md
  milestones/M-001.md
  history/YYYY-MM.jsonl append-only event log
  history/ids.jsonl    append-only index of every id ever issued (never reused)
  schema/*.schema.json JSON Schema for each record type
```

## The one rule that matters most

**An agent proposing something gets an `IDEA`. An agent cannot commit to work on
its own initiative.**

Every record an agent creates is `created_by: agent`, `approval: pending`,
`status: IDEA`. Reaching `PLANNED` or beyond requires `approval: approved` and
either `created_by: user` or a recorded authorisation. That is enforced in code,
not prose: an agent attempting to create an approved non-IDEA record is refused.

The reason is that a ledger is only worth reading if "we decided to do this" and
"someone suggested this" are visibly different things. If an agent can quietly
promote its own idea to `IN_PROGRESS`, the ledger becomes a list of things that
happened, which is not what it is for.

### When *you* ask for it, the existing record is promoted

If you say "put that on the roadmap", the idea that already exists becomes
`PLANNED`. The agent records that you said so:

```bash
node <ledger>/src/cli.ts set-status F-001 PLANNED --approval approved \
  --user-instructed --user-instruction "Put that on the roadmap."
```

Both parts are required: the flag asserts you instructed it, and the quoted text
is the evidence, stored on the record (`promotion_authorised` +
`user_instruction`) and in the history log. `created_by` stays `agent` — the
agent recorded the idea, and that is not rewritten — while `approval` becomes
`approved`. The default is unchanged: without that pair the promotion is refused,
and a record can never be *created* directly at a commitment status this way.

## Status lifecycle

```
IDEA ──(approval)──> PLANNED ──> READY ──> IN_PROGRESS ──> TESTING ──> DONE
  │                     │           │            │             │
  └─────────────────────┴───────────┴────────────┴─────────────┴──> BLOCKED
                                                                   DEFERRED
                                                                   CANCELLED
```

- `BLOCKED` requires a `blocker_reason` **and** a `blocker_type` (one of `code`,
  `environment`, `provider`, `user_decision`, `dependency`, `external`).
- `DEFERRED` and `CANCELLED` require a written reason.
- `DONE` requires `completion_basis`, and it is one of:
  - `verified` -- every gate required by the record's `verification_profile`
    actually passed, with evidence recorded;
  - `user_asserted` -- a human says it is done. **Not** verified.
  - `manually_accepted` -- accepted by hand, e.g. visual acceptance. **Not**
    verified.
- `DONE` is terminal. A change of mind is a new record that `supersedes` the old
  one, so the evidence behind the original DONE is never erased.

## Readiness is computed, not asserted

`READY` does not mean "has a description". It means every blocking dependency is
`DONE`. If a record claims `READY` while a dependency is still open, the ledger
keeps the human's `READY`, prints a validation warning, and reports
`ready: false` with the blocking ids. It does not silently override you.

## Verification profiles

Each record declares a `verification_profile` naming which gates must pass
before `completion_basis: verified` is legal: `doc-only`, `unit`, `module`,
`app`, `ui-browser`, `security-sensitive`, `release`. They map onto the existing
L0-L5 escalation ladder; the ledger does not define a second meaning of "done".
Each gate's result is stored independently as `status` plus optional `evidence`,
`count` and `reviewer`.

## Dependencies

`dependencies` and `blocked_by` both block readiness. `blocks` is computed
automatically as the reverse -- it is never stored, because a stored reverse
edge would be a second source of truth. Cycles are a validation **error** and are
reported with the path (`F-050 -> F-060 -> F-050`).

## Secrets

Never put a password, API key, token or private key in a record. The ledger
**refuses** the write rather than redacting it: a silently redacted record would
misrepresent what was said and hide the leak. Store a reference instead
("credential stored in the OS credential manager"). Generated files additionally
mask anything credential-shaped so a hand-edited record cannot leak into the
dashboard.

## Using the CLI

```bash
node <ledger>/src/cli.ts init --root .
node <ledger>/src/cli.ts add --root . --title "Playback sleep timer" --summary "Stop playback after N idle minutes."
node <ledger>/src/cli.ts list --root . --status IDEA
node <ledger>/src/cli.ts show --root . F-001
node <ledger>/src/cli.ts validate --root .
node <ledger>/src/cli.ts status --root .
node <ledger>/src/cli.ts snapshot --root . --json
node <ledger>/src/cli.ts history --root . --id F-001
```

Every command takes `--root`, `--json` and `--now`. `--root` is what makes the
whole thing testable against a temporary directory; `--now` pins timestamps so a
regenerated artifact is byte-identical.

Start with `PROJECT-CONFIG.md` -- especially `repo_root`, which is what stops you
using the wrong repository's ledger by accident.
