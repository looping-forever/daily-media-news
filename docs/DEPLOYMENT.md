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
and the checked-in deploy contract expects it. It is now a pass-through:

```js
export async function onRequest(context) {
  return await context.next();
}
```

It sends no `WWW-Authenticate` header, reads no secret, and answers no request
itself. `master.daily-digest.example.pages.dev` is a second hostname for the same
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

```bash
# Trigger a digest manually (dry run first is recommended):
gh workflow run daily-news.yml -f digest_time=morning -f dry_run=true
gh workflow run daily-news.yml -f digest_time=evening -f dry_run=false

# Trigger a breaking scan manually (dry_run defaults to true):
gh workflow run breaking-monitor.yml -f dry_run=true

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

> ⚠️ **`gh workflow run daily-news.yml` is not a bare dispatch, and it can silently
> publish nothing.** Discovered while deploying the origin auth control (§4a).
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

## 6. Enable / disable the system

```bash
# Pause the digests (manual dispatch still works):
gh workflow disable daily-news.yml
gh workflow enable  daily-news.yml
```

The breaking monitor has **no schedule anymore** (it is event-driven from the
Worker), so disabling it is not needed. To pause breaking detection entirely,
pause/disable the Cloudflare Worker's cron trigger instead.

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
