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

## 4a. Digest Pages site access control

### ⚠️ The origin is the archive, and it is now the only lock

**For the entire life of this project the published digest archive was readable by
anyone, and this guide described the `digest-auth` Worker as the protection. It
never was.** Measured 2026-10-02 and again 2026-10-03, with real HTTP status
codes and no credentials sent:

| Probe (unauthenticated) | Result |
|---|---|
| `GET https://daily-digest.example.pages.dev/` | **HTTP 200, 38,021 bytes** |
| `GET …/zzz-not-a-real-page.html` | **HTTP 200** — the same full 38 KB index |
| `GET …/totally/fake/path/` | **HTTP 200** — the same full index |
| the index itself | linked **72** digest pages |
| any request carrying `Range:` | ignored — 200 with the full body, no `Content-Range`, no `Accept-Ranges` |
| any request | **no `WWW-Authenticate` header**, ever |

**Why it was wrong.** A `digest-auth` Worker (`deploy/digest-auth-worker.js`, since
deleted) did return HTTP 401 — but it only **PROXIED** to
`https://daily-digest.example.pages.dev`. That origin is separately reachable on its own
hostname, so the Worker was bypassable by construction: a lock on a door with a
second, unguarded entrance. The Pages catch-all made the blast radius site-wide
rather than a fixed set of pages, and range requests being ignored means a partial
fetch leaked the same bytes a full one would.

**The Worker also broke the owner's own access, which is why it was deleted rather
than left as a "convenience path".** It demanded username `digest` before the origin
asked for the owner's email address — **two prompts, two identities**. The owner
therefore could not log in from the ntfy notification link at all, and typing the
right password did not help, because the two prompts wanted different usernames. A
proxy in front of the origin can never add protection the origin does not already
have; it can only add a second identity prompt. It was removed from both the
repository and Cloudflare on 2026-10-03. **Do not recreate it.**

**What the control is now.** `functions/_middleware.js`, at the repository root —
a Cloudflare Pages Function enforcing HTTP Basic auth **at the origin, in front of
every static asset**. That is the only shape that covers static HTML; Cloudflare's
own guidance is that to run middleware in front of static files you must create
`functions/_middleware.js`, because a `_worker.js` or a routing Function does not
cover them. It shipped in commit `9627e79` and was deployed 2026-10-03 — Pages
deployment `7c1508f1-4dbc-4009-9571-a749705a25c8`, source commit `9627e79`.

Verified after-state, **22 unauthenticated probes, no credentials sent**:

| Probe (unauthenticated) | Result |
|---|---|
| `GET /` | **401**, 230-byte body, `WWW-Authenticate: Basic realm="Daily Digest (private archive)"`, `Cache-Control: no-store` |
| `HEAD /` | **401** with those headers and a **zero-byte** body (RFC 9110) |
| `GET /digests/2026-10-02-evening.html` | **401 directly** — the middleware runs *before* Pages' static handler, so the extensionless redirect never fires for an anonymous client and **no hop can be followed** |
| `Range: bytes=0-99`, and an absurd range | **401** — never 206, never a partial body |
| any invented path | **401** — the catch-all is closed |
| `/.well-known/cloudflare-access-protected-resource` | Pages' own **404, 0 bytes** |

Read that last row honestly: **no content leaks through it**, and it is the
standard carve-out so Cloudflare Access can serve resource metadata — but
"every route answers 401" is therefore **not literally true**. Every route except
that one endpoint does.

`master.daily-digest.example.pages.dev` is a **second hostname** serving the same
project from the same deployment. It is already behind **Cloudflare Access**
(a **302** to the Access login, serving no digest content), so the apex was the
only public hostname.

**Username.** The only accepted Basic-auth username is
`digest@example.com`, fixed in source. Set it to your own address before
deploying. The 401 body prints it
deliberately, so a username mismatch is self-diagnosing from the browser instead
of a silent lockout.

### What is NOT verified

- **Correct-credential login has not been verified.** Every probe above was made
  **without sending credentials**, on purpose: the question being measured is
  what an anonymous client receives. What is proven is that unauthenticated
  requests are refused — **not** that the owner can log in with the configured
  secret. That is confirmed by the owner in their browser.
- **The fail-closed runtime setting below is unverified** and rests entirely on
  the owner's dashboard confirmation.
- **Cloudflare Access / Zero Trust is not in use.** It was investigated and
  rejected — it requires a payment method on file even on the $0 plan, which the
  owner cannot provide. Do not expect an Access policy to be part of this setup.

### Owner setup — both steps are required

1. **Pages secret `DIGEST_PASSWORD` on the `daily-digest` project.** Cloudflare
   dashboard → *Workers & Pages* → `daily-digest` → *Settings* →
   *Environment variables and secrets* → Add → type **Secret**, name exactly
   `DIGEST_PASSWORD`, environment **Production** (preview deployments do not get
   it), and it is now the **only** place the password lives — the
   `digest-auth` Worker that held a second copy was deleted 2026-10-03, and
   secrets are per-project, so do not go looking for a Worker secret. A
   middleware with no secret refuses every request — fail-closed 401, never an
   open door. **A 401 does not prove this step is done**: an anonymous probe gets
   401 whether the secret is set or not, so confirm by actually authenticating.
2. **Settings → Runtime → Fail open/closed = Fail closed.** This is the one
   control the code cannot enforce: on fail-open, exhausting the daily Pages
   Functions allowance serves the static assets directly and the middleware never
   runs. Cloudflare exposes this setting through **no CLI and no API**, so it can
   only be confirmed in the dashboard.

### How it is kept protected

Three independent guards, each with a test proven red against a deliberately
broken variant (the project's verification standard — `docs/PKB.md` §37: a guard
that cannot fail is decoration):

1. **`daily-news.yml` asserts the middleware exists before deploying.** The
   deploy step carries `continue-on-error: true` by design, so a deploy can go
   wrong while the job stays green; without that assertion a missing
   `functions/` directory would republish the whole archive unprotected with no
   error anywhere. It also greps the file for `WWW-Authenticate`, so a stub that
   exists but enforces nothing is refused too.
2. **`_routes.json` is written explicitly** as
   `{"version":1,"include":["/*"],"exclude":[]}` rather than left to wrangler's
   generator. In that file `exclude` always takes priority over `include`, so an
   auto-generated exclude covering static routes would exempt the entire archive
   while the job looked healthy. The file is then asserted non-empty.
3. **`drift-canary.yml` probes both live hostnames unauthenticated and fails
   unless the archive is provably gated.** The assertion is inverted on purpose,
   and so is the question behind it: the canary asks **was any digest content
   served to an anonymous client**, not *what was the status code*. So `401`/`403`
   passes — and so does a **Cloudflare Access gate** in the redirect chain that
   served no digest content. The probe follows redirects (`-L`), keeps every
   hop's response headers (`-D`) and reports the URL it actually ended up at, so
   `master.daily-digest.example.pages.dev`'s **302** to
   `…cloudflareaccess.com/cdn-cgi/access/login/…` is correctly read as
   *protected* instead of as an exposure. Against that:
   * served archive content fails **first and unconditionally**, whatever the
     status and whatever else the chain passed through;
   * any `2xx` that is neither a refusal nor an Access gate fails, so renaming a
     marker in a template cannot quietly disarm the guard;
   * an unreachable, timing-out, redirect-looping or `404` host fails — an origin
     that refuses to be measured has not been measured, and "no verdict" is not
     "protected".

   No credentials are sent, and `--disable` also stops a stray `.curlrc` on the
   runner from attaching any: verifying that auth *works* is the opposite of what
   this measures. So a future deploy that drops the protection is caught by a
   scheduled check rather than by luck.

   *This arm used to fail on every scheduled run*, because it judged `master.*`
   by status alone and treated any `3xx` as an exposure while Access was in fact
   gating it correctly. A guard that cries wolf on the healthy state is one whose
   real signal gets ignored, which defeats the entire point of having it. Fixed
   2026-10-03. **The `master.*` arm is still probed** — it is exactly what would
   notice the day that Access application is removed and `master.*` becomes a
   second public hostname serving the whole archive.

### The `digest-auth` Worker — DELETED 2026-10-03, do not recreate

There used to be a second Cloudflare Worker here: `deploy/digest-auth-worker.js`,
an HTTP Basic Auth reverse proxy in front of `https://daily-digest.example.pages.dev`,
deployed manually like `breaking-monitor` via `deploy/digest-auth-wrangler.toml`
(`name = "digest-auth"`, `main = "digest-auth-worker.js"`, `preview_urls = false`).

**It never was the access control, and it actively broke the owner's access.**

- It only **proxied**, so the origin stayed directly reachable and the proxy gated
  nothing. See §4a for the measured proof.
- It checked username **`digest`**, while the origin middleware checks
  **`digest@example.com`**. Following a Worker URL therefore produced
  **two Basic-auth prompts demanding two different usernames**, and the owner could
  not get into the archive from the ntfy notification link on mobile. This is the
  concrete reason it was removed rather than merely deprecated.
- It held a **second copy** of the `DIGEST_PASSWORD` secret. That copy died with
  the Worker. The **Pages** secret is the one that matters now, and it is not
  touched by any of this — see "Owner setup" in §4a.

**Removed on 2026-10-03 from both the repository and Cloudflare:**

```bash
wrangler delete --name digest-auth
```

Both `deploy/digest-auth-worker.js` and `deploy/digest-auth-wrangler.toml` were
deleted from the repo, and the deployed Worker was deleted from the account.
**Verifying the deletion — and reading the result correctly:** that hostname now
returns **HTTP 404, not 401**. DNS still resolves, because Cloudflare wildcards
`*.workers.dev`; the 404 is the wildcard answering for a subdomain that no longer
has a Worker behind it. So on `workers.dev`, **404 means deleted** and **401 means
still deployed** — a 401 after a delete means the delete failed. `wrangler versions
list --name digest-auth` is the authoritative check and now returns
`This Worker does not exist on your account. [code: 10007]`.

The ntfy click-through (`click`, the `Open digest` action, and
the `full digest:` truncation notice) and the academic result link in
`scripts/check_academic_result.py` now point **straight at the Pages origin**, so
exactly **one** prompt appears, asking for the one username the middleware accepts.
`tests/workflows/test_workflow_lint.py::DeletedDigestAuthWorkerTests` fails if the
dead host or those files ever come back.

**A proxy in front of the origin is never the answer here.** It cannot add
protection the origin does not already have, and it can only add a second identity
prompt. If you want a memorable hostname, put it on the Pages project as a custom
domain — do not reintroduce a Worker.

The origin middleware needs **no dashboard deploy** — it ships with every Pages
deploy of the repository, and its two guard assertions live in `daily-news.yml`
(see "How it is kept protected" above).

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
