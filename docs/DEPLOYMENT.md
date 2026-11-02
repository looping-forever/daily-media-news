# Deployment & Operations Guide

How this system is hosted, how to operate it day-to-day, and how to roll it back.
Companion to the [README](../README.md); this document describes what is actually deployed.

## 1. Architecture overview

Two independent schedulers feed one alert/digest pipeline:

```
                    ┌──────────────────────────────────────────────┐
 TWICE-DAILY DIGEST │ GitHub Actions cron '35 1,13 * * *' (UTC)    │
                    │ = fires 07:20 / 19:20 NPT                    │
                    │   └─ daily-news.yml → collect → AI classify  │
                    │      → validate → render → Gmail SMTP        │
                    └──────────────────────────────────────────────┘

                    ┌──────────────────────────────────────────────┐
 BREAKING TRIPWIRE  │ Cloudflare Worker "breaking-monitor"         │
 (always-on, free)  │   cron */5 * * * * → poll Google News RSS    │
                    │   emergency-keyword regex trips              │
                    │     └─ repository_dispatch (breaking-alert)  │
                    │        └─ breaking-monitor.yml on GitHub:    │
                    │           AI classification → verification   │
                    │           → spoiler veto → dedup → SMTP send │
                    └──────────────────────────────────────────────┘
```

**Why the digest fires at 07:20/19:20 NPT instead of 08:00/20:00:** GitHub's
scheduler queues workflows late (**+17–31 min observed**). Firing ~40 min
early means delivery lands ≈08:00/20:00 anyway. Every email states its REAL
data cutoff (`Information checked through: YYYY-MM-DD HH:MM NPT`) — trust that
line, not the nominal schedule.

**Why the tripwire lives on Cloudflare:** GitHub's minimum schedule granularity
is hourly, too slow for breaking news. The Worker (`deploy/cloudflare-worker.js`)
runs every 5 minutes on the Cloudflare **free tier (no card required)** and does
only cheap deterministic detection (regex over Google News RSS). When a
candidate trips, it fires ONE `repository_dispatch` event (`breaking-alert`)
that wakes the existing `breaking-monitor.yml` workflow for the full AI
classification / verification / spoiler-veto / dedup / send pipeline. Safety
logic is never duplicated — the Worker only decides *whether to wake the
pipeline*.

## 2. Cloudflare setup (as actually performed)

All click-by-click, in the Cloudflare dashboard (free plan, no payment method):

1. **Account** — sign up at dash.cloudflare.com; choose Free. No card requested.
2. **Create Worker** — left sidebar → *Compute (Workers)* → *Workers & Pages* →
   *Create application* → *Create Worker*. Name: `breaking-monitor` → Deploy →
   *Edit code*.
3. **Paste code** — select-all, delete starter code, paste the entire contents
   of `deploy/cloudflare-worker.js` → Deploy.
4. **GH_TOKEN secret** — first create a fine-grained PAT on GitHub
   (Settings → Developer settings → Personal access tokens → fine-grained):
   repository access **Only select repositories → this repo only**;
   permissions: **Contents: Read-and-write** (Metadata read-only is added
   automatically). Then in the Worker: *Settings* → *Variables and Secrets* →
   add variable, type **Secret**, name exactly `GH_TOKEN`, paste the PAT → Deploy.
5. **KV namespace** — *Storage & Databases* → *KV* → create namespace
   `breaking-kv`. Then Worker *Settings* → *Bindings* → *Add* → *KV namespace*:
   variable name exactly `KV` (the code reads `env.KV`) → namespace
   `breaking-kv` → Deploy. (Any namespace name works; only the binding
   variable name `KV` matters.)
6. **Cron trigger** — Worker *Settings* → *Triggers events* → *Cron Triggers*
   → add `*/5 * * * *` → Save → Deploy.
7. **workers.dev route** — enabled under *Settings* → *Domains & Routes*
   (this is where the status/test URL below comes from).

## 3. ⚠️ TOKEN ROTATION (GH_TOKEN expires)

The fine-grained PAT has an expiration (90 days is typical). When it expires,
detection keeps running silently but every dispatch fails with HTTP 401.

**Symptom:** the worker status page (`https://breaking-monitor.<subdomain>.workers.dev/`)
still reports everything present (it only checks that the secret *exists*), but
forcing a pass with `?run=1` shows dispatch failures in the JSON response /
worker logs.

**Renewal steps:**

1. Generate a new fine-grained PAT with the same permissions
   (only this repo · Contents: Read-and-write).
2. Cloudflare dashboard → your worker → *Settings* → *Variables and Secrets*.
3. Edit `GH_TOKEN` → paste the new value → Save → **Deploy**.
4. Verify: open `<worker-url>/?run=1` and confirm no dispatch failure.

Calendar the renewal — there is no expiry alarm.

## 4. Updating the worker code

Deployment is **manual by design** (no wrangler CI yet):

1. Edit `deploy/cloudflare-worker.js`, commit and push to GitHub.
2. Open the raw file on GitHub, copy its entire contents.
3. Cloudflare dashboard → worker `breaking-monitor` → *Edit code* →
   select-all, paste, **Deploy**.

(A `wrangler.toml` + auto-deploy from Actions is possible later if the manual
copy-paste becomes a burden.)

## 4a. Digest Pages site: public by design

The published digest archive is **public**. Anyone with the link can open it; there
is no username, no password and no prompt. That is a deliberate decision taken on
2026-10-30, not an oversight:

- the owner could not open the site from a phone's in-app browser, which suppresses
  the native HTTP-auth login dialog, so the ntfy click-through dead-ended;
- the repository is public, so every digest HTML was already readable anonymously
  from GitHub — the password protected nothing that was not already public.

`functions/_middleware.js` still exists at the repository root, because wrangler
discovers `./functions` relative to the directory `wrangler pages deploy` runs in
and the checked-in deploy contract expects it. It passes every request through
and answers one thing itself: a path under `/digests/` whose answer would be the
archive index — that is, an edition that does not exist — becomes a real `404`
with a small "no edition for this slot" page.

**Why the 404 exists (2026-10-31).** Pages serves the archive index with HTTP
`200` for any path that does not exist. A missing edition was therefore
indistinguishable from a published one at the status line, which makes a
status-only monitor false-green on the exact failure it exists to catch. The
middleware recognises the fallback by the index's own `<title>` (generated by
`scripts/render_digest.py`), which no rendered digest carries; anything else —
the index, assets, a digest page, the `308` from a `.html` form to its
extensionless form — is returned untouched. The 404 carries
`Cache-Control: no-store`, deliberately: a cached 404 would keep hiding a slot
that publishes successfully minutes later.

It sends no `WWW-Authenticate` header, reads no secret, and returns every real
page unchanged. `master.daily-digest.example.pages.dev` is a second hostname for the same
project; it must also answer anonymously. If a Cloudflare Access application still
covers either hostname, **remove it** — with the credential gate gone, Access can
only present the owner with a login page, which is the defect this change removed.

The old gate (commit `9627e79`, 2026-10-03) and the exposure that preceded it are
recorded as history in **[PKB.md §38](PKB.md)**. Do not restore them by copying
that record back into the code.

### How it is kept open, and how a regression is caught

1. **`daily-news.yml` refuses to deploy a credential prompt.** Before the deploy it
   asserts `functions/_middleware.js` exists, and it greps the file for
   `WWW-Authenticate` — **finding it is fatal**. The deploy step carries
   `continue-on-error: true` by design, so the assertion is what makes a missing or
   re-gated middleware loud instead of silent. After the deploy it also fetches the
   origin anonymously and fails on any status other than 200.
2. **`_routes.json` is written explicitly** as
   `{"version":1,"include":["/*"],"exclude":[]}` rather than left to wrangler's
   generator, so every route reaches the middleware and the deploy contract stays
   reviewable. The file is asserted non-empty.
3. **`drift-canary.yml` probes both public hostnames and fails unless the archive is
   served anonymously.** It sends no credentials (and `--disable` so no runner
   `.curlrc` can attach any). A pass requires **HTTP 2xx AND this archive's own
   markup** on both hostnames, with no Cloudflare Access gate in the redirect chain.
   A login page, an Access gate, a `401`/`403`, a `404`, any other non-2xx, an
   unreachable host or a looped redirect all fail: each means the owner's link does
   not open.

### Owner setup

Nothing is required to open the site. Two clean-ups are worth doing:

1. **Delete the leftover Pages secrets** `DIGEST_PASSWORD` and `DIGEST_USERNAME` on
   the `daily-digest` project (*Settings → Environment variables and secrets*), in
   every environment. Nothing reads them any more; leaving them invites the
   mistaken belief that a control still exists.
2. **Remove any Cloudflare Access application** covering the archive hostnames, for
   the reason above.

*Settings → Runtime → Fail open/closed* no longer affects access: the middleware
performs no access decision. It can stay as it is.

### What is NOT verified

- The live origin's current status was not probed while making this change. The
  scheduled canary and the post-deploy check verify it on every run; the owner also
  confirms the link opens in their browser.

## 5. Manual operations cheat-sheet (`gh` CLI)

All nine workflows in `.github/workflows/` carry a `workflow_dispatch` trigger, so
every one can be started by hand. The inputs below are transcribed from each
file's `workflow_dispatch.inputs` block (verified 2026-11-02); two workflows take
no inputs at all.

**The operational risk is uneven.** Most manual dispatches are safe at their
defaults: `breaking-monitor.yml`, `academic-result.yml` and `ledger-repair.yml`
default to a dry run, and `digest-prebuild.yml` has no safe defaults because it
never invents a slot. `daily-news.yml` is the exception — its defaults
**publish and deliver**. `delivery-selftest.yml` sends by design, and
`digest-watchdog.yml`, `budget-sentinel.yml` and `drift-canary.yml` can notify
(or, in the watchdog's case, trigger a delivery) when their own condition says
something is wrong.

### Trigger examples

```bash
# Digest: -f digest_time=... is mandatory (it has no default).
gh workflow run daily-news.yml -f digest_time=morning -f dry_run=true
gh workflow run daily-news.yml -f digest_time=evening -f dry_run=false

# Breaking scan: dry_run defaults to true, which also suppresses the model call.
gh workflow run breaking-monitor.yml
# Probe the AI ladder once: spends free-tier quota, never sends.
gh workflow run breaking-monitor.yml -f ladder_probe=true

# Force one full detection pass on the Worker right now (browser or curl):
open https://breaking-monitor.<your-subdomain>.workers.dev/?run=1

# Recent workflow runs:
gh run list --limit 10

# Health checks:
#  - Worker: status URL above should print "alive" + KV/GH_TOKEN present.
#  - Digest heartbeat: the email arriving twice a day IS the heartbeat.
#  - Monitor heartbeat: silent runs append rows to state/breaking_log.json;
#    each run's summary appears in the Actions step summary.
```

### Dispatch inputs

#### `daily-news.yml` — builds, publishes and delivers a digest

| Input | Default | Effect |
|---|---|---|
| `digest_time` | **none — required** (`morning` \| `evening`) | Which slot to build. Omitting it fails the dispatch outright. |
| `dry_run` | `false` | `true` runs collection, validation, render and artifact upload only: no Pages deploy, no state commit, no notification. |
| `trigger_source` | `manual` | Origin label for this run. The Worker sends `worker`, `prebuild` or `supersede`; a human dispatch stays `manual`. It is also what decides whether a `phase=prebuild` dispatch may publish (see `publish`). |
| `retry_slot` | `""` | Slot being retried. Must equal `digest_time` or the first step exits fatally. Set by `digest-watchdog.yml`. |
| `retry_of` | `""` | NPT date of the empty digest being retried; recorded and printed by the empty-digest assertion. |
| `slot_date` | `""` | NPT date `YYYY-MM-DD` of the slot. Empty derives it: today in Nepal for a manual dispatch, the actual firing for a scheduled run. |
| `phase` | `full` | `full` builds for delivery; `prebuild` builds ahead of the slot, forces the notification path dry, and seals an artifact for `digest-prebuild.yml`. |
| `publish` | `false` | Only affects a manual `phase=prebuild` run: unless `true`, it is build-and-upload only — no page deploy, no state/archive commit, no KV ledger write, no seal hand-off. Ignored when `phase=full`. |
| `simulate_pool_refusal` | `false` | Fails the model walk as a shared-pool refusal with **zero model calls**, to exercise the degraded path. Manual dispatch only; ignored on `schedule`. |

**Can it publish or notify?** Yes. A default dispatch (`phase=full`,
`dry_run=false`) deploys the page, commits the archive/state and sends the digest
notification. `phase=prebuild` always suppresses the notification; add
`publish=true` only when a page/archive/KV write is actually intended. Note that
`phase=prebuild` **with** `trigger_source=prebuild` is treated as the Worker's own
arm and publishes without `publish=true`.

> ⚠️ **`gh workflow run daily-news.yml` is not a bare dispatch, and it can silently
> publish nothing.**
>
> - **It requires `-f digest_time=…`.** Without it the dispatch fails outright with
>   `HTTP 422: Required input 'digest_time' not provided`.
> - **A slot that was already sent no-ops — and still exits green.** Supplying
>   `morning` when the morning digest already went out takes the **fallback
>   no-op path**: every outward-facing step is skipped, **including the Pages
>   deploy**, and the run reports success. So a well-intentioned manual deploy can
>   do nothing at all while showing a green tick.
>
> To force a real deploy, dispatch the slot that has **not** been sent yet, then
> confirm the deploy step actually ran in the run's step log — do not trust the
> green tick alone.

#### `breaking-monitor.yml` — classifies and can send a breaking alert

| Input | Default | Effect |
|---|---|---|
| `dry_run` | `true` | `true` runs `--dry-run --no-model`: the importance gate and hard-evidence path run, previews are written, nothing is sent and no model quota is spent. `false` takes the live path (ntfy first, email/Telegram as fallbacks). |
| `ladder_probe` | `false` | Walks the configured AI ladder once on a fixed synthetic candidate (up to four 150 s windows). Never sends, cannot promote a rung or mark the ladder healthy, and writes `state/ladder_probe.json`, which the state commit persists. Honoured only on `workflow_dispatch`; it overrides the `dry_run` argument even if `dry_run=false`. |

**Can it publish or notify?** Not at its defaults. `dry_run=false` on this
workflow is the only manual shape that can send a real alert, and
`ladder_probe=true` never sends regardless of `dry_run`.

**What a probe run records, and what it cannot conclude.** `state/ladder_probe.json`
holds one row per attempt — granted window, measured wall, **first model byte**,
outcome — plus a top-level `attribution` field. A rung that has written no model
output after `AI_FIRST_OUTPUT_SEC` (40 s) is abandoned rather than left to burn
its whole 150 s window, and `no_output` is recorded instead of a guessed timeout,
so the free walk's worst case is `4 × 40 + 40 = 200 s` rather than 600 s. The
probe also walks **one control rung** against the keyed provider
(`config/opencode-cli.json`, credential `GEMINI_API_KEY`) in its own state
directory, labelled `"control": true` and excluded from the free `verdict`. That
is the only way to tell a stalled free tier from a runner that never shows model
output at all:

| `attribution` | Means |
|---|---|
| `harness_no_stream` | The control rung also emitted nothing. Nothing about the free pool can be learned from this run; fix the invocation first. |
| `free_tier_stalled` | The control answered, no free rung ever produced a byte. The free pool is the fault. |
| `free_tier_answered` | At least one free rung produced model output. The ladder is reachable; `verdict` then says whether the answer parsed. |
| `inconclusive` | The control is unconfigured (`control: unconfigured`, no `GEMINI_API_KEY`) or neither side produced evidence either way. |

The credential reaches the **control child only** — `ai_classify` is given an
`OPENCODE_CONFIG` that the sweep never sees, so probe mode cannot change which
models a real run walks. The variable's name is recorded; its value never is.
Two honest limits: the 40 s deadline would abandon a free rung that is merely
slow to its first token (one measured 137.6 s once), and the probe still cannot
show whether a *real* sweep would have answered — it shows what the invocation
did on this run.

#### `digest-prebuild.yml` — seals an already-built digest into the Worker

Normally dispatched by `daily-news.yml` (which passes its own run id), not by hand.

| Input | Default | Effect |
|---|---|---|
| `run_id` | **none — required** | Run id of the `daily-news.yml` prebuild whose uploaded artifact this run must seal. The seal refuses a missing, in-progress or failed run. |
| `digest_time` | **none — required** (`morning` \| `evening`) | Which slot to seal. |
| `slot_date` | `""` | Nepal date `YYYY-MM-DD` of the slot; empty derives it from the named slot's own firing. |

**Can it publish or notify?** It does not build or deploy a digest; it verifies
the named prebuild failed-or-not, downloads its artifact, checks the page answers
anonymously, seals the artifact into the Worker's KV, commits the delivery-ledger
mirror, and — only when the ledger says that slot's push was degraded
(`partial`, `fallback_last_good`, `unavailable`) — dispatches a `daily-news.yml`
supersede at `phase=full`, which **does** push. So it can cause a notification,
but only on that repair path.

#### `digest-watchdog.yml` — missing/empty digest checks and the retry ladder

| Input | Default | Effect |
|---|---|---|
| `ladder_only` | `false` | `false` runs both jobs: the slot delivery check and the empty-digest retry ladder. `true` runs only the retry ladder and skips the slot check. The Worker sets `true` when a retry is due. |

**Can it publish or notify?** Yes, indirectly and by design. The slot check files
a GitHub issue and sends an ntfy alert when a digest is missing; the retry job
re-dispatches `daily-news.yml`, whose run can deliver a real digest.

#### `academic-result.yml` — TU result monitor

| Input | Default | Effect |
|---|---|---|
| `dry_run` | `true` | `true` writes `state/academic_notify_preview.json` instead of delivering. `false` takes the live path (ntfy + website) for a detected result. |

**Can it publish or notify?** Only with `dry_run=false`.

#### `delivery-selftest.yml` — one labelled test message per channel

| Input | Default | Effect |
|---|---|---|
| `channel` | `both` | Which channel(s) to exercise: `both`, `telegram` or `gmail`. Narrow this on a retry so the channel that already passed is not messaged twice. |

**Can it publish or notify?** Yes — by design it sends exactly one test message
per selected channel and no digest content. That is its whole purpose.

#### `ledger-repair.yml` — one-off repair of the KV delivery ledger

| Input | Default | Effect |
|---|---|---|
| `dry_run` | `true` (required) | `true` reads KV and prints the planned diff without writing. `false` applies the declared correction, PUTs it back and re-reads it to verify. |

**Can it publish or notify?** No. It never sends, deploys or commits; its only
write is the single gated KV PUT.

#### `budget-sentinel.yml` — takes no inputs

Reads `config/actions-budget.json` and the month's run durations, then files an
issue and sends an ntfy message at 80% of the ceiling, again at 90% with a
`budget-freeze` label, and at 100% fails the job on purpose. **It can notify**
(issue and ntfy) when a threshold is crossed; it never publishes a digest.

#### `drift-canary.yml` — takes no inputs

Runs the CLI/ladder/flag drift checks and the anonymous probes of both Pages
hostnames, and fails on a gated or non-2xx archive. **It can notify**: its live
canary passes `--notify`, so a persistent failure escalates to an ntfy page at
strike 2 and above. It publishes nothing.

### Reading a run when the artifact host is flaky

`gh run view <run-id>` renders a web page and can fail or truncate while the
Actions artifact/log host is unhealthy. Two fallbacks do not depend on it:
`gh run view <run-id> --log` prints the whole run's log (add `--job <job-id>` to
narrow it), and the per-job raw endpoint
`gh api repos/<owner>/<repo>/actions/jobs/<job-id>/logs --allow-escape-sequences`
returns the same text without the rendered view. Beyond the logs, the durable
record is committed state: the bot commits `state/last_digest_sent.json`,
`state/digest_retry.json`, `state/breaking_state.json`,
`state/breaking_delivery.json`, `state/model_health.json` and
`state/ladder_probe.json` with `[skip ci]`, so `git log` / `git show` on `master`
shows exactly what a run recorded even when the artifact cannot be downloaded.

## 6. Enable / disable the system

```bash
# Pause the digests (manual dispatch still works):
gh workflow disable daily-news.yml
gh workflow enable  daily-news.yml
```

The breaking monitor is event-driven from the Worker, plus one scheduled
dead-man's-switch run a day at `20 1 * * *` UTC (07:05 NPT) so a dead Worker is
noticed within ~24 hours. Disabling it therefore also removes that backstop. To
pause breaking detection entirely, pause/disable the Cloudflare Worker's cron
trigger instead.

## 7. Free-tier limits

| Resource | Free limit | This system's usage |
|---|---|---|
| CF Workers requests | 100k/day | ~288 scheduled invocations/day (+ rare manual `?run=1`) |
| CF KV writes | 1k/day | A few/day worst case — keys are written ONLY after a successful dispatch (6 h TTL) |
| CF KV reads | ~100k/day | Only for tripped candidates (rare) |
| GitHub Actions minutes | 2,000 min/month (private repo) | Digests ≈ 400–1,000 min/month observed (AI latency variance dominates); monitor runs are event-driven and short |

## 8. Latency model (honest)

```
source publishes → indexed by Google News (external, minutes)
  → Worker poll picks it up (≤5 min, avg ~2.5)
  → repository_dispatch → GitHub runner pickup (seconds–minutes)
  → AI classification (~1–3 min) → verification/dedup (seconds) → SMTP (seconds)
```

Total detection→alert is typically **~5–10 min after indexing**, dominated by
external indexing and the poll interval. This is NOT guaranteed instant, and
indexing lag itself is outside our control.

## 9. Rollback to GitHub-only monitoring

If Cloudflare is ever unwanted:

1. Re-add the hourly schedule to `.github/workflows/breaking-monitor.yml`:

   ```yaml
   on:
     schedule:
       - cron: '20 * * * *'   # ~:05 past every hour NPT
     repository_dispatch:
       types: [breaking-alert]
     workflow_dispatch: ...
   ```

   CAUTION, as of 2026-10-19: with no Worker this cadence is the *only* trigger,
   but hourly costs ~1,150 Actions minutes/month on its own against a 1,400
   repository cap, and `tests/breaking/test_breaking_chain.py` now fails any
   schedule that fires more than once a day. Reconcile both before using it.

2. Delete or disable the Cloudflare Worker (and optionally the `breaking-kv`
   namespace).

Everything else (digests, state files, secrets) is unchanged — the monitor
script already contains its own deterministic pre-gate and does not depend on
the Worker.

## 10. State & concurrency notes

- **Single production writer per file**: `state/history.json` +
  `state/digest-archive/` are committed only by the daily-news bot;
  `state/breaking_state.json` only by the breaking bot.
- `state/active_stories.json` is **shared** by both pipelines. Safety comes
  from: separate concurrency groups (`daily-news-{morning|evening}` vs
  `breaking-monitor`, `cancel-in-progress: false`), all state writes being
  atomic (tmp file + `os.replace`), and `git pull --ff-only` immediately
  before every bot push so interleaved commits re-sync or fail loudly instead
  of corrupting history.
- Ephemeral scratch (`state/breaking_pending.json`,
  `state/breaking_log.json`, previews) is never committed.
