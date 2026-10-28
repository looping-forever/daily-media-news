# Operations

Day-to-day running of this system. Hosting and first-time Cloudflare setup are in
**[DEPLOYMENT.md](DEPLOYMENT.md)**; the history of what broke and why is in
**[PKB.md](PKB.md)**. This file describes what is true now.

Everything here was read from the committed workflows, `deploy/` and `tests/`.
Where something is unverified it says so, because a confident wrong operational
note is worse than a missing one.

---

## 1. What the system is

One person. Two digests a day (07:20 and 19:20 NPT). Between them, two monitors
that stay silent unless something real happens: a breaking-news tripwire and a TU
CSIT result watcher.

It runs on GitHub Actions plus a free Cloudflare Worker. Your PC can be off.

## 2. The delivery model

**ntfy is the primary channel. The website is the complete record. Everything
else is a fallback.**

1. **ntfy** — a push notification to your phone carrying the digest as markdown
   with a click-through to the page.
2. **The Cloudflare Pages site** — the rendered HTML digest. This is the
   auth-gated personal archive; it is where a digest is actually *read*.
3. **Gmail** — fires **only if ntfy failed**.
4. **Telegram** — fires **only if both ntfy and the website failed**.

The last two are **correctly gated and never triggered in observed operation.**
Every delivery on record has been `website: true, ntfy: true, gmail: false,
telegram: false`. They are exercised by the test suite, not by production. Treat
a real backup send as unproven until a primary has actually failed once.

**How a digest is actually produced (Phase U6/U7).** The digest is built *ahead*
of its slot, not during it. Every time below was read from the committed config,
not from prose: the Worker's `dispatcher_crons` in `deploy/schedule.json`, the one
deployed trigger in `deploy/wrangler.toml`, and each workflow's own `on.schedule`.

```
06:40 / 18:40 NPT   the Worker dispatches digest-prebuild.yml. That workflow runs
                    daily-news.yml with phase=prebuild, which builds + renders +
                    deploys the page and pushes NOTHING; digest-prebuild.yml then
                    seals the artifact into Worker KV.

07:20 / 19:20 NPT   the Worker's delivery arm finds the sealed artifact and
                    pushes it -> your phone. This is a KV lookup, not a build.
                    It re-fires at :25 / :30 / :35 / :40 only if no send is
                    recorded for that slot.

07:50 / 19:50 NPT   the GitHub-side fallback cron (daily-news.yml), which stands
                    itself down if the ledger says the slot already went out
```

> This block used to say **06:50 / 18:50** and to call the prebuild a *cron on
> daily-news.yml*. Both were wrong. There is no GitHub cron for the prebuild at
> all, and the Worker asks for it at 00:55 / 12:55 UTC — 40 minutes ahead of the
> slot, not 30. Corrected 2026-10-28 against `deploy/schedule.json`.

A failed prebuild is not a lost digest: the slot's own run then builds it from
scratch, which is slower but still delivers. The watchdog has a dedicated arm
that goes red if a slot's artifact was never sealed.

### The clock, in Nepal time

Every row was read from the file named in its "where" column on 2026-10-28; none
of it is restated from memory. Nepal is UTC+05:45 all year, with no daylight
saving. There is exactly **one** Cloudflare cron trigger (`* * * * *`) — every
Worker row below is a branch of that single dispatcher, not a separate trigger.

| Time (NPT) | What runs | Where it is written | What it does |
|---|---|---|---|
| every 2 min, all day | Worker academic sweep | `deploy/schedule.json` → `dispatcher_crons.academic_sweep` (`*/2 * * * *`) | Checks the TU IOST result feed and dispatches `academic-result.yml` only on a new notice |
| every 3 min, all day | Worker breaking sweep | `deploy/schedule.json` → `dispatcher_crons.breaking_sweep` (`*/3 * * * *`) | Cheap deterministic tripwire; dispatches `breaking-monitor.yml` only on a candidate |
| :17 past every hour | Worker health check | `deploy/schedule.json` → `dispatcher_crons.healthcheck` (`17 * * * *`) | Writes the heartbeat key the `?health=1` page reads |
| :17 / :32 / :47 past every hour | Worker retry-ladder check | `deploy/schedule.json` → `dispatcher_crons.digest_ladder` | Reads the retry state and dials `digest-watchdog.yml` (`ladder_only=true`) only if a retry is actually due |
| 06:15 | `drift-canary.yml` | its own `on.schedule` (`30 0 * * *`) | One job running every drift check, before the morning slot |
| 06:40 | Worker → `digest-prebuild.yml` | `dispatcher_crons.digest_prebuild.morning` (`55 0 * * *`) | Builds and seals the morning artifact; pushes nothing |
| 07:05 | `breaking-monitor.yml` (GitHub net) | its own `on.schedule` (`20 1 * * *`) | Fallback detector if the Worker path is dead |
| 07:20, re-firing 07:25–07:40 | Worker delivery arm, morning | `dispatcher_crons.digest_slots.morning` (`35 1 * * *`) plus `digest_refire_window.morning` (4 attempts, 5 min apart) | Pushes the sealed morning artifact |
| 07:50 | `daily-news.yml`, GitHub fallback, morning | its own `on.schedule` (`5 2 * * *`) | Stands itself down unless the slot was never delivered |
| 09:45 | `digest-watchdog.yml`, morning leg | its own `on.schedule` (`0 4,16 * * *`) | Judges the morning slot and pages if it never arrived |
| 10:02 | `academic-result.yml` (GitHub net) | its own `on.schedule` (`17 4,10 * * *`) | Makes a dead academic monitor visible |
| 12:08 | `budget-sentinel.yml` | its own `on.schedule` (`23 6 * * *`) | Files or updates the Actions-minutes level |
| 16:02 | `academic-result.yml` (GitHub net) | its own `on.schedule` (`17 4,10 * * *`) | Second daily net |
| 18:40 | Worker → `digest-prebuild.yml` | `dispatcher_crons.digest_prebuild.evening` (`55 12 * * *`) | Builds and seals the evening artifact; pushes nothing |
| 19:20, re-firing 19:25–19:40 | Worker delivery arm, evening | `dispatcher_crons.digest_slots.evening` (`35 13 * * *`) plus `digest_refire_window.evening` | Pushes the sealed evening artifact |
| 19:50 | `daily-news.yml`, GitHub fallback, evening | its own `on.schedule` (`5 14 * * *`) | Stands itself down unless the slot was never delivered |
| 21:45 | `digest-watchdog.yml`, evening leg | its own `on.schedule` (`0 4,16 * * *`) | Judges the evening slot |

`tests/workflows/test_workflow_lint.py` re-derives every GitHub row above from the
committed cron, so a time in this table that drifts away from a workflow goes red.

### What reaches your phone

**A normal AI digest.** One push at 07:20 / 19:20 NPT, a few minutes later if the
delivery arm had to re-fire. The title is a time-of-day icon, then the slot, the
date and the story count — `Morning · 2026-10-28 · 12 items` — and the body is the
digest itself with a tap-through to the page. It stays quiet unless something needs
you: then the title gains `· 2 deadlines due` (or `· action needed`), and the top
priority is reserved for a critical source host being down.

**The unsummarised edition, when no model responds.** Same push, same slot, but the
title ends `· unsummarised edition (no AI today)` and the page and email carry the
same label. The stories are collected headlines, links and hosts with nothing
written by a model. It is deliberately capped below the escalation threshold, so a
fallback never pages the way a real digest can.

**A fault, when nothing could be built.** Exactly one page, deduplicated to one per
slot per 6 hours (`scripts/alert_dedupe.py`), plus one GitHub issue:

| What happened | What you see |
|---|---|
| The sources themselves were down or empty | `Digest BROKEN: sources returned nothing usable — fallback will fire` (high priority) |
| The sources were fine and it was simply a quiet day | `Digest: no stories (1/3) — sources were fine` (a notice, **not** a fault) |
| A slot never produced a delivery at all | `Today's <slot> digest never arrived` (high priority) |

A recovery sends one all-clear push and nothing after it.


## 3. Deploying the Worker

Manual, by design, through one script. **Always from the repository root.**

**The committed `deploy/wrangler.toml` carries a placeholder KV namespace id, not
the real one** — in this working copy and in the published copy alike. Deploying
that file directly reaches the Cloudflare API and fails with
`code: 10041 KV namespace not found`, because the placeholder is valid 32-hex and
nothing skips it locally.

```powershell
# 1. the real id, for this session only -- never written to a tracked file
$env:DMNEWS_KV_NAMESPACE_ID = '<the 32-hex id from: wrangler.cmd kv namespace list>'

# 2. deploy
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy-worker.ps1

# optional: see the command without deploying anything
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy-worker.ps1 -DryRun
```

- **Where the id comes from: `$env:DMNEWS_KV_NAMESPACE_ID`.** One value, one
  session. `scripts/deploy-worker.ps1` substitutes it into a rendered copy at
  `deploy/wrangler.local.toml`, which is **gitignored** and rewritten on every
  deploy so it cannot go stale. The id is never printed — deploy output is
  filtered before it reaches the screen.
- **When it is missing, nothing deploys.** The script exits `2` and says so. It
  also refuses a value that is not 32 hex, or that is still the placeholder.
  There is deliberately no fallback: a silent fallback binds the wrong namespace.
- **It always stamps `--var WORKER_BUILD:<short sha>`.** Without it the Worker
  reports `build: "unset"` and the drift canary cannot tell a current deployment
  from a stale one.
- **`-BuildStamp` exists for one case.** The default is `git rev-parse --short
  HEAD` of the checkout. `drift-canary.yml` resolves the stamp against
  `looping-forever/daily-media-news`, so a commit that exists only in the private
  working copy cannot be resolved there and the canary fails closed. If the
  content was synced from the public repository, pass the public commit it was
  synced from.
- **The script verifies itself.** It reads `?health=1` from `deploy/schedule.json`
  after deploying and exits `2` — "deployed but not verified" — if the Worker
  does not report the build stamp it just set.
- **`& wrangler.cmd`, never bare `wrangler`,** for anything run by hand. Bare
  `wrangler` resolves to the PowerShell *alias* for `Invoke-WebRequest` on some
  machines, which produces a baffling error that mentions nothing about
  Cloudflare.
- **`git fetch` needs a schannel override** on this machine:
  `git -c http.sslBackend=schannel fetch`. Without it git fails TLS
  negotiation against GitHub.

After deploying, confirm the Worker answers and that the reported build is the
one you just shipped:

```powershell
(Invoke-WebRequest 'https://<worker>.workers.dev/?health=1').Content | ConvertFrom-Json | Select-Object build, crons
```

`tests/workflows/test_deploy_config_ids.py` fails if a real-looking id is ever
committed again, in either tree, and if the rendered config stops being ignored.

## 4. Deploying the Pages site — read this twice

Two traps, both of which have shipped a **green CI run next to a broken site**.

**Trap 1 — it replaces the whole deployment.** A Pages deploy is not an
incremental publish. Whatever you deploy *is* the site. If you deploy a
directory that is missing `docs/index.html` or the `digests/` folder, those
paths stop existing — there is no merge with what was there before.

**Trap 2 — run it from the repository root, or `functions/_middleware.js` is
silently omitted.** This is the dangerous one. The Basic-auth middleware lives
at the repository root. A Pages deploy from a subdirectory does not include it,
**the archive goes live completely unprotected**, and **every job stays green**
— there is no error, because the deploy genuinely succeeded.

```powershell
# CORRECT — from the repository root
& wrangler.cmd pages deploy docs --project-name daily-digest

# WRONG — from docs/ : middleware omitted, archive public
cd docs; & wrangler.cmd pages deploy . --project-name daily-digest
```

The workflow stages only `docs/digests/`, `docs/index.html` and `_routes.json`.
`_routes.json` must stay committed with `include: ["/*"], exclude: []` — an
auto-generated `exclude` of static routes beats `include` and exempts the whole
archive while looking healthy.

### Always verify a 401 afterwards, on several paths

A successful deploy tells you nothing about protection. Probe it:

```powershell
$base = 'https://daily-digest.example.pages.dev'
foreach ($p in @('/','/HEAD','/digests/2026-10-09-morning.html','/no-such-page.html')) { ... }
```

Check **all** of these, and treat anything other than 401 as a breach:

| Probe | Expected |
|---|---|
| `GET /` | 401 + `WWW-Authenticate` |
| `HEAD /` | 401 + headers, zero-byte body |
| a real digest page | 401 **directly** (no redirect hop to follow) |
| a range request | 401, never 206 |
| **an invented path** | 401 — the catch-all must be closed |

The invented-path probe is the one people skip, and it is the one that catches a
catch-all that has been left open. `drift-canary.yml` runs this probe twice a
day against both live hostnames and fails if archive content is ever served to an
anonymous client — so a silent regression should not survive to the next
morning. Do not rely on that to catch it immediately.

One carve-out, stated rather than smoothed over:
`/.well-known/cloudflare-access-protected-resource` returns Pages' own 404 with
0 bytes rather than 401. No content leaks.

**Unverified:** that a *correct-credential* login works. Every probe above is
made without sending credentials, deliberately — the question being measured is
what an anonymous client receives. So this proves unauthenticated requests are
refused, **not** that you can log in. You confirm that in your browser.

## 5. Adding a source

1. Add the feed URL to the right list in **`config/interests.yml`** (tracked
   titles under `games`/`anime`/`manga`/`manhwa`/`manhua`) or to
   **`config/exam_notices.json`** (official notice pages).
2. For an official page, also add the domain to `config/collection.json` if it
   needs a non-default timeout.
3. Commit and push. The next run picks it up.

There is no separate registration step — the collector reads those files
directly. If a source is a critical host, add it to the pre-flight list in
`scripts/collect_news.py` so its failure is measured rather than inferred.

Official notice pages use crude link-text keyword extraction. That is
deliberate and best-effort: it never fails the run, and it is the most likely
thing to need a matcher tweak when a university reorganises a page.

## 6. Rotating tokens

Every "where it lives" claim below was **verified 2026-10-10** against all three
systems that hold credentials, by **name-only listing** — `gh secret list`,
`wrangler.cmd secret list -c deploy/wrangler.toml`, and
`wrangler.cmd pages secret list --project-name daily-digest`. None of those
prints a value, so no value was read or written. Timestamps below are the
`updated_at` each system reported. Re-run the three listings before you rotate.

| Secret | Where it lives — verified 2026-10-10 | How to rotate |
|---|---|---|
| `DIGEST_INGEST_TOKEN` | **BOTH** — Worker `breaking-monitor` **and** repo Actions. Actions copy `2026-10-09T14:39:47Z` | **Both copies, one operation.** See below — this row was previously wrong |
| `NTFY_TOPIC` | **BOTH** — Worker secret and Actions secret (`2026-08-30T16:58:04Z`) | New topic name is a credential in itself — see §7. **Both copies together**, for the same reason |
| `NTFY_SERVER` | Actions secret `2026-08-27T07:17:54Z`; on the Worker a **plaintext `[vars]` entry** (`https://ntfy.sh`), **not** a secret | An address, not a credential. The Worker side changes in `deploy/wrangler.toml` and needs a redeploy |
| `GH_TOKEN` | Worker secret only — **no Actions copy exists** | Dashboard → worker → Variables and Secrets → edit → **Deploy** |
| `CF_PAGES_API_TOKEN`, `CF_ACCOUNT_ID` | Repo Actions secrets only. `2026-08-27T16:07:43Z` / `2026-08-27T16:08:08Z` | Both needed together; also needs KV **read** scope for U8 |
| `DIGEST_PASSWORD` | Pages project `daily-digest` → **production** environment only. **No Actions copy** | The one lock on the archive; see §4 and the approved-not-added note below |
| `DIGEST_USERNAME` | Pages project `daily-digest` → **production** environment. **No Actions copy** (`digest-prebuild.yml` reads it when present) | The Basic-auth username that goes with `DIGEST_PASSWORD`. Changing it is a Pages setting, not a code change — there is no committed fallback: unset, the middleware fails closed (401 on every route) and logs `DIGEST_AUTH_MISCONFIGURED` |
| `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `GMAIL_RECEIVER`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Repo Actions secrets only | Untouched in practice — these paths have never fired |
| `OPENCODE_API_KEY` | Repo Actions secret `2026-08-24T10:50:14Z` | Present in the store, but the string `OPENCODE_API_KEY` appears in **no** workflow file. `scripts/ai_classifier.py` reads it from the process environment. Treat as unwired, not as a live credential |

**Never commit any of these.** Refer to every one by name.

### `DIGEST_INGEST_TOKEN` — two copies, rotate both, in one operation

This row used to say **"Worker secret only"**. That was **factually wrong**, and
it was the kind of wrong that generates a production incident rather than a bad
document: anyone rotating from that table rotates **one** copy and silently
leaves the other stale.

The token was created in **both** systems in one session. It is a `wrangler`
secret on the Worker *and* a repository Actions secret, and
`digest-prebuild.yml`'s seal step sends the Actions copy as the `X-Ingest-Token`
header on `POST /?ingest=1`. **Both copies are load-bearing.**

The moment they diverge, the prebuild's hand-over is refused with **401** — which
reads like a network fault rather than a credential mismatch, and gets debugged
as one. Nothing else in the system reports the divergence.

```powershell
# Worker copy — & wrangler.cmd, never bare wrangler (see §9)
& wrangler.cmd secret put DIGEST_INGEST_TOKEN -c deploy/wrangler.toml

# Actions copy — separately, but in the SAME operation
gh secret set DIGEST_INGEST_TOKEN --repo looping-forever/daily-media-news
```

**Pipe the value to both. Never pass it as a command-line argument and never
write it to disk.** `wrangler secret put` and `gh secret set` both read the
value from stdin, which keeps it out of the process table and out of shell
history. Do both halves before you walk away.

**Provenance.** Created **2026-10-10**; the value was piped via stdin to both
systems, never passed as a command-line argument and never written to disk.
**The value is not recorded in this file and must not be.**

One discrepancy, flagged rather than smoothed over: the owner's stated creation
date (2026-10-10) does **not** match what GitHub reports — the Actions copy's
`updated_at` is `2026-10-09T14:39:47Z`, which is 2026-10-09 20:24 NPT. GitHub's
value is the measured one; the difference is unresolved and worth a glance
before the next rotation.

### `GH_TOKEN` is not your local wrangler login

`~/.wrangler/config/default.toml` holds a **wrangler OAuth token** written by
`wrangler login`. It is a **different credential** from `GH_TOKEN` and does not
substitute for it. `GH_TOKEN` is a GitHub token with repository dispatch scope
that the Worker sends to `api.github.com` itself; re-running `wrangler login`
does not refresh it, and the two expire on different clocks. Rotating `GH_TOKEN`
means minting a new token in GitHub.

**`GH_TOKEN` expiry is silent.** The Worker health page still reports everything
present, because it only checks that the secret *exists*. The symptom is
dispatch failures in the Worker log and a breaking alert that never arrives.
**Put the expiry in a real calendar.** After rotating, force a pass with
`?run=1` and confirm no dispatch failure.

### `DIGEST_PASSWORD` — Pages only; the Actions copy is approved but NOT yet added

Verified present on the Pages project `daily-digest`, **production** environment
only. There is **no** Actions copy, and `gh secret list` confirms it.

Adding it as an Actions secret has been **approved and not yet performed**.
That is precisely why `digest-prebuild.yml`'s "Verify the digest page is live"
step takes its fallback path today:

- **With** the secret — an authenticated `GET`, asserting HTTP 200 **and** the
  `<h2 class="section-header">` markup. This is the strong check.
- **Without** it — a `::warning::` naming exactly what was skipped, then three
  indirect checks (path agreement, not-inherited, production deployment
  identity), passing only if at least two of the three actually ran.

So prebuild page-liveness is currently **indirect**. It does not prove that the
exact page the artifact points at is what the owner's browser will load.

### PROPOSED, NOT IMPLEMENTED — a `DIGEST_INGEST_TOKEN` drift check

**This is a proposal. No such check exists in this repository and none was
added.** Nothing described here runs today.

The goal: fail **loudly** if the two copies diverge, and **never print a value**
— not in a log line, not in an exception message, not in a diff.

**What it cannot do, stated first because it constrains the whole design: it
cannot compare the two values.** Neither system will hand a value back.
`wrangler secret list` returns names only and `wrangler secret put` values are
write-only; GitHub Actions secrets are write-only and `gh secret list` returns
names and timestamps only. **A true value-comparison drift check is not
directly possible.** Proposing an equality test would be proposing something
that cannot work.

**What is possible:**

1. **Existence-and-freshness — necessary, not sufficient.** Assert the name is
   present in both listings and that both `updated_at` values fall inside the
   same rotation window. This catches the common case, a one-sided rotation,
   because the two timestamps separate immediately. It cannot catch a rotation
   that set both timestamps to the same minute with different values. A
   **backstop, not the real check.**

2. **A functional end-to-end probe — the primary check.** `POST /?ingest=1` with
   a **deliberately invalid body** and the Actions copy in the `X-Ingest-Token`
   header:
   - **400** — auth passed, schema rejected. **The copies agree.**
   - **401** — auth failed. **They have diverged.**
   - anything else — investigate.

   This is the only check that actually proves the copies match, and it does so
   without revealing either value: the token travels in a request header and is
   never echoed, logged, or written anywhere. A deliberately invalid body is
   **rejected before any KV write**, so the probe is side-effect-free — it
   cannot overwrite a sealed artifact and cannot page anyone.

**Proposed shape:** a step in the existing `drift-canary.yml` (currently one run
a day, 06:15 NPT), with 401 as a hard failure. Never `set -x` around it, never
interpolate the token into an echo, and on failure print **only** the two
`updated_at` timestamps and the status code.

**Honest limit of this proposal.** It proves the copies agree **at the moment it
runs** — it is a detector, not a preventer. Without it, a one-sided rotation is
caught by the next seal at 06:40/18:40 as a bare 401 that reads like a network
fault; with it, the same event is caught as an unambiguous "diverged". The
difference is diagnosis, not detection.

## 7. Reading health

```
https://<worker>.workers.dev/?health=1
```

| Field | Means |
|---|---|
| `breaking_heartbeat_age_s` | How long since the breaking sweep last ran |
| `academic_heartbeat_age_s` | Same for the academic sweep |
| `*_unreadable` | The KV read **failed**. Deliberately *not* alarmed on — a KV blip must not storm you |
| `build` | The `WORKER_BUILD` var stamped at deploy. `unset` means the deploy skipped `--var` |
| `artifact_morning_present` / `artifact_evening_present` | Whether this slot's digest was sealed |
| `artifact_*_age_s` | How old that sealed artifact is |
| `crons` | The schedule the **deployed** Worker believes it has |
| `breaking_dispatch_ever` | Whether detection has ever woken the workflow |
| `delivery_arm_enabled` | Whether the slot cron pushes or dispatches |

**A fresh heartbeat does not mean detection is working.** It proves the cron
fired, nothing more. This repository shipped six weeks of green heartbeats while
detection matched almost nothing. `breaking_dispatch_ever` is the field that
answers the question the heartbeat cannot.

**`crons_verified_at_runtime: false` is deliberate.** A Worker cannot query its
own schedules at runtime — there is no API for it from inside the Worker. The
field is there to say "this was not checked here", not "this is wrong". The
schedules are verified offline instead: `tests/workflows/test_workflow_lint.py`
Check 16 compares `deploy/wrangler.toml`, the Worker's `CRON_*` constants and
`deploy/schedule.json` as sets, so the three cannot drift silently.

## 8. ntfy on the phone

- **Delivery is instant via FCM** (Google's push service) on both Android and
  iOS. There is no polling involved.
- **The topic name IS the credential.** Anyone who has it can read every
  notification you receive, and anyone can post to it. Treat it like a password:
  a long random string, never a word.
- **Battery optimisation is the usual cause of "it stopped arriving".** On
  Android, exempt the ntfy app from battery optimisation, or aggressive OEM
  scheduling will silently delay or drop pushes. This is the single most common
  reason a healthy system looks broken.
- Turning off app notifications in ntfy's own settings stops the *display* but
  not the *delivery*, so the ledger still records a push. Do not use that as
  evidence that delivery stopped.

## 9. Operational traps

Each of these has cost time. They are here so the next person does not pay again.

| Trap | What happens | Do instead |
|---|---|---|
| Bare `wrangler` | Resolves to PowerShell's `Invoke-WebRequest` alias on some machines; error mentions nothing about Cloudflare | `& wrangler.cmd` |
| `git fetch` without an override | TLS negotiation fails against GitHub | `git -c http.sslBackend=schannel fetch` |
| `gh run view --log` | **Truncates.** A green-looking tail is not the whole log | Read the run's log from the Actions UI, or per-step |
| Renaming the two ntfy steps | The push silently stops happening; the fallback ladder then also stands down | Never rename `- name: Publish to ntfy (primary)` or `- name: Send digest via Gmail (backup)` — pinned by `tests/digest/test_literal_step_names.py` |
| `python` on Windows | Microsoft Store shim, exits 9009 | `py -3` |
| `pytest` | Not installed | `py -3 -m unittest` |
| `py -3 -m unittest discover -s tests -t .` | Aborts — there is no `tests/__init__.py` | Discover **per subdirectory** |
| Pages deploy from a subdirectory | `functions/_middleware.js` silently omitted, archive public, every job green | Deploy from the repo root; verify 401 on several paths |
| Reading a green run as "the site is fine" | The Pages deploy step carries `continue-on-error` | Verify the 401 yourself |

## 10. The delivery ledger and its git mirror

`state/last_digest_sent.json` records **one slot** and is overwritten by the next
run. That is why the watchdog could never judge a slot displaced by a long
delayed firing.

`state/digest_slot_ledger.json` is the git mirror of the Worker's
`digest:ledger`: the last 7 Nepal days × 2 slots, written by
`digest-prebuild.yml`'s mirror step from the KV payload it just read.

**Direction of truth, and it is not symmetric:**

- **KV is authoritative for DELIVERY.** Git never decides whether a push
  happened. Only the Worker witnessed that.
- **Git is authoritative for BUILD** — `built_at`, `story_count`,
  `source_health_state`. Only the run that built the digest witnessed that.
- If they disagree, **KV wins** and the watchdog logs the disagreement.

**The mirror lags, and that is not a bug to fix.** It is committed at the seal,
which runs *before* the slot's own push, so it is up to ~12 h stale and blind to
the newest delivery. Any line rendered from it is describing the ledger **as of
the previous seal** — which is exactly why no rendered line may claim to describe
today. `scripts/delivery_ledger.py` scopes every phrase to what it actually
measured.

If KV cannot be read, the mirror step writes **nothing** rather than guessing.
An unreadable ledger is not an empty one, and a slot entry with no `ntfy_sent_at`
reads as *missed* to every consumer.

## 11. Running the tests

```powershell
py -3 tests/ci_local.py                    # reproduce the CI suite locally
py -3 tests/ci_local.py --clean-checkout   # ...with state/ moved aside
py -3 tests/ci_local.py --suite digest     # one suite
py -3 tests/ci_local.py --extra            # + manifest guard and live canary
```

`--clean-checkout` is the important one: it moves the real `state/` aside (never
deletes it) and restores it afterwards, so "does this pass from a clean
checkout?" gets answered locally instead of being rediscovered on CI.

**Read the per-suite counts `ci_local.py` prints.** Never count console lines
and never parse console text as a test report.

**Any change to a workflow `run:` block must be verified three ways:** the file
parses as workflow YAML, every `run:` block passes `bash -n`, and every Python
heredoc body compiles. 425 Python tests once passed while all four production
workflows were dead, because none of that was checked. `tests/workflows/`
does all three — run it.
