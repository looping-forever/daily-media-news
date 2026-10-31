# Daily Media News

A personal **information-monitoring assistant** — not a news aggregator. It watches a fixed set of sources for one specific person (Example City, Example District · TU BSc CSIT · Example College Example City · Example Bank customer), filters everything down to "what I need or genuinely want to know", and delivers a compact briefing twice a day. Between briefings, a separate breaking tripwire stays silent unless a genuine emergency clears every gate. A dedicated academic monitor watches the TU CSIT result feed for the student's roll number.

Everything runs on GitHub Actions plus a free Cloudflare Worker tripwire — your PC can be off. **[docs/OPERATIONS.md](docs/OPERATIONS.md)** covers day-to-day running, the delivery model, safe deployment, token rotation, reading health, and the operational traps. **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** covers hosting, first-time Cloudflare setup, and rollback.

## Delivery channels

The digest is delivered through **primary** and **backup** channels, in this order:

1. **ntfy** (primary) — push notification to the topic named by the `NTFY_TOPIC` secret, with the full digest as markdown, a click-to-website action, and an image attach when the top story has one.
2. **Cloudflare Pages website** (primary) — a rendered HTML digest published to the Pages site. This is a **public personal archive**: anyone with the link can open it, and the ntfy click-through points straight at the origin (`https://daily-digest.example.pages.dev/...`), so exactly one page opens — no prompt, no password.
3. **Gmail** (backup) — fires **only if the website OR ntfy failed**.
4. **Telegram** (backup) — fires **only if BOTH primaries failed**.

> **Channel 2 is public by design (2026-10-30).** It used to be gated by HTTP Basic auth in `functions/_middleware.js`. That gate was removed because the owner could not open the site from a phone's in-app browser (which suppresses the native login dialog), and because this repository is public: every digest HTML was already readable anonymously from GitHub, so the password protected nothing. `functions/_middleware.js` remains at the repo root, because that is where the Pages deploy contract expects it; it sends no credential prompt and passes every request through to the static assets. **It answers exactly one thing itself (2026-10-31):** a path under `/digests/` that Pages would resolve to the archive index — because no such edition exists — gets a real `404` with a small "no edition for this slot" page and `Cache-Control: no-store`. Before that fix a missing edition answered `200` with the index body (13,859 bytes, byte-identical to `/`), so a status-only check could not tell a failed publish from a successful one. The canary in `drift-canary.yml` now requires every hostname to answer an anonymous request with HTTP 2xx and archive markup — a login page, a 401/403 or a missing archive fails the run. Deployment steps are in **[docs/DEPLOYMENT.md §4a](docs/DEPLOYMENT.md)**; the historical record of the old gate is in **[docs/PKB.md §38](docs/PKB.md)**.

**Channels 3 and 4 are untested failover, not delivery paths.** They are correctly gated (a backup step physically cannot run when both primaries succeeded) and wired, but in observed operation they have **never fired** — `state/last_digest_sent.json` records `website: true, ntfy: true, gmail: false, telegram: false`. Both primaries have been healthy for every delivery on record, so the backup code paths are exercised only by test suites, not by production. Treat a real backup send as unproven until a primary has actually failed once.

A delivery marker (`state/last_digest_sent.json`) records which channels succeeded; the fallback no-op guard uses it so a worker-dispatched digest is never re-sent by the GitHub-side fallback schedule. It records **one slot** and is overwritten by the next run, so `state/digest_slot_ledger.json` mirrors the Worker's delivery ledger (last 7 Nepal days × 2 slots) into git — that is what lets the watchdog judge a slot displaced by a long delayed firing. **KV is authoritative for delivery; git never decides whether a push happened.** See [docs/OPERATIONS.md §10](docs/OPERATIONS.md).

## Schedule

**One Worker cron, not two.** The digest schedule is a single dispatcher trigger
— `* * * * *` — that wakes the Worker every minute. The Worker then decides, from
its own committed `SCHED_DIGEST_SLOTS`, whether a slot is due; at 01:35 and 13:35
UTC (07:20 / 19:20 NPT) it delivers that slot's digest. All other minutes are a
no-op. This replaced two separate per-slot crons; the merge did not change what
is invoked (1440 no-op ticks instead of 2 dispatches a day), it only removed the
"add a cron and the deploy half-applies" failure mode.

| Workflow | Trigger | NPT timing |
|---|---|---|
| Digest Prebuild | cron `5 1` / `20 1` / `5 13` / `20 13` (UTC) | **06:50 & 07:05**, **18:50 & 19:05** — builds the digest ahead of its slot and seals it into KV |
| Daily Media News Digest | Worker dispatcher cron `* * * * *` (UTC), delivering at 01:35/13:35 → GitHub fallback cron `5 2` / `5 14` (UTC) | **pushes 07:20 & 19:20** → GitHub fallback 07:50 & 19:50 |
| Breaking News Monitor | Cloudflare Worker poll every 3 min → `repository_dispatch` | event-driven, ~5–10 min after indexing |
| Academic Result Monitor | Cloudflare Worker sweep every 2 min → `repository_dispatch` **+ `17 4,10 * * *` cron backstop** | event-driven ~2 min after a new result notice; 10:02 / 16:02 NPT self-sweep otherwise |
| Digest Watchdog | cron `0 4,16 * * *` + a 15-min empty-digest retry ladder | **09:45 & 21:45** |
| Drift Canary + test suite | cron `30 0 * * *` (UTC) | 06:15 NPT — pre-slot early warning, ~95 min before the morning digest |

**How a digest is actually produced.** `digest-prebuild.yml` builds the digest
**ahead** of its slot, waits for that run, and seals the result into the Worker's
KV. At 07:20/19:20 the Worker's delivery arm finds the sealed artifact and
pushes it, so your notification is a KV lookup rather than a 20-minute AI
pipeline. The GitHub-side fallback at 07:50/19:50 is a dead-man's switch: it
fires only if the Worker failed, and stands itself down when the ledger says the
slot already went out. GitHub's scheduler runs late (**+17–31 min observed**), so
the fallback lands ≈08:00/20:00 anyway.

Each digest states its REAL data cutoff — `Information checked through:
YYYY-MM-DD HH:MM NPT` — plus scheduled-vs-actual start times when delivery
lagged. Trust that line, not the nominal schedule; and if it is **missing
entirely**, that is itself the signal (see below).

## Daily digest pipeline

1. **Deterministic collection (Stage-A)** — `scripts/collect_news.py` fetches ~43 sources in parallel (count varies with your interest lists): Google News RSS sweeps in EN/JA/KO/ZH locales built from tracked titles, 14 fixed Nepal queries × en/ne locales, open-meteo Example City weather, and 5 official notice pages (`BANK_URL`, `tu.edu.np`, `iost.tu.edu.np`, `COLLEGE_URL`, `nea.org.np` — the two deployment URLs are dropped when unset). Output: `state/candidates.json`.
2. **AI classification (Stage-B)** — the agent (`prompts/daily-digest.md`) classifies candidates into categories with priority, personal relevance, spoiler levels, deadlines, and translations to English. It writes exactly one file: `state/stories.json`. The model ladder is single-sourced in **`config/opencode.json`** (`ladder`, alongside the CLI `pin` and `min_version` floor) — the **seed** is `opencode/big-pickle` (primary) → `opencode/mimo-v2.6-flash-free` → `opencode/nemotron-3.5-lightning-free`, but the walk is not: `scripts/model_selector.py --all` returns the whole live-discovered free pool in rank order and the agent step walks it under a wall-clock budget (360 s per attempt, 1800 s for the walk, and it stops once one more attempt no longer fits), so a model that dies costs one timeout instead of the slot. `tests/breaking/test_ladder_sync.py` fails CI if any workflow, script or prompt drifts from that file, so the ladder is stated in exactly one place.
3. **Deterministic validation** — `scripts/validate_stories.py`: schema check (a story is dropped only when it is unusable — no title, no http(s) `source_url`, bad category/priority/reliability/spoiler level; an absent or wrong-typed descriptive field such as `spoiler_reason` is normalised to `""` and the story is kept, which is what the 2026-10-30 empty digest proved the hard way), excluded-keyword filter, spoiler veto, crime/depression filter (kept only with a practical-impact marker such as closures/warnings/outages), dedup vs 7-day history + intra-run title similarity, active-event tracking, sort by priority/relevance/tier, cap 15. An empty digest is classified as `quiet_day` (notice), `broken` (source outage) or `validator_rejected_all` (stories were produced and all rejected — paged as an error).
4. **Media enrichment** — `scripts/enrich_media.py` fetches `og:image`/`og:video` for validated stories (exact URL → normalized URL → title match), resolves Google News redirect URLs to real article URLs (batchexecute decode), and blocks generic Google placeholder/CDN domains so wrong generic images never appear.
5. **Build + render** — `scripts/build_email.py` renders a fixed-template HTML + plain-text email; `scripts/render_digest.py` renders the website digest page into `docs/digests/`.
6. **Deliver** — publish to ntfy + deploy to Cloudflare Pages (primary); Gmail and Telegram fire only as backups per the channel rules above.
7. **History update** — `scripts/update_history.py` merges survivors into `state/history.json` **only after a successful delivery**, so a channel failure never marks anything as reported.

## Source-health honesty

The pipeline was hardened after a total `tu.edu.np` DNS outage (SERVFAIL from Google's resolver — an upstream failure nothing here could fix) exposed that the system was quietly lying about its own freshness. The rules now:

- **Every source records its own truth.** `state/candidates.json` carries a `source_results` record per planned fetch: host, outcome, attempts, error. A task the soft budget never started is **`skipped_budget`**, kept deliberately distinct from `failed` — "never tried" is never reported as "unreachable".
- **Reachability is measured, not asked.** `validate_stories.py` derives a `source_health` object from those records, and `research_status` is **computed in code** from measured reachability. The agent's own `research_status` verdict is preserved separately as `agent_research_status` for comparison, but it no longer decides anything.
- **A dead critical host is a first-class result.** The collector runs a bounded DNS pre-flight (5 s resolve timeout) with a 2-attempt retry policy, and three Google News `site:` sweeps act as the one redundancy that survives a TU outage.
- **Every section always renders.** Each digest category prints exactly one status line, so an empty section is explained rather than silently missing. When a section's sources all failed you get `⚠️ Source unreachable — check <url>` naming where to look, not a blank area.
- **Freshness drops the timestamp when it cannot be earned.** With `ok` the digest keeps `Information checked through: <time> NPT`; with `partial`/`degraded` it appends the cause. When *nothing* could be confirmed reachable (state `unknown`) the timestamp is **omitted entirely** — claiming currency it cannot prove is the exact lie this was built to remove.
- The health banner appears identically on the **website, the email and the ntfy push** (one banner definition, driven by the same `source_health` object), so no surface can look healthy while another is degraded.

## Categories covered

| Section | Scope |
|---|---|
| ACTION / IMPORTANT | Reserved for act-today items: outage, closure, deadline, severe warning |
| WORLD — MAJOR EVENTS | Civilization-scale checked facts (war outbreak/escalation, nuclear accident/test, global emergency); ≥2 independent tier1/tier2 sources required |
| LOCAL & CITY | Daily life only: power, water, roads, transport, internet, civic notices |
| TU / BSC CSIT | Exam routines, results, registration/form deadlines, calendar changes |
| Example College / COLLEGE | Student-practical info (exams, fee deadlines, closures) — not marketing |
| MY BANK — EXAMPLE BANK | Customer impact: rates, fees, KYC deadlines, service/ATM changes, fraud warnings |
| IMPORTANT NEPAL | Education policy, major government decisions, economy, major infrastructure, cyber incidents affecting Nepali users |
| GAMING | Tracked games/studios + major general gaming news |
| ANIME | Tracked series + major general anime news |
| MANGA · MANHWA · MANHUA | Tracked series + major general news (JP/KR/ZH sources translated) |
| TECH / CAREER / OPPORTUNITIES | Major market shifts, significant internships/scholarships — not a job board |

- **Foreign-event filtering** — promo events abroad (concerts, conventions, merch drops, stock moves) are auto-demoted or dropped unless they carry practical Nepal impact.

## Breaking-news monitor

A Cloudflare Worker (`deploy/cloudflare-worker.js`, free tier) polls Google News RSS + direct publisher RSS feeds **every 3 minutes** and runs a cheap deterministic emergency-keyword pre-gate. When a candidate trips, it fires one `repository_dispatch` event that wakes the GitHub workflow (`breaking-monitor.yml` → `scripts/breaking_monitor.py`) for the full pipeline: AI classification → verification → spoiler veto → dedup. Silent unless a verified **LEVEL 3** event clears every stage (subject `🚨 Breaking: {title} [{confidence}]`). Setup, token rotation, latency model, and rollback: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

**Qualifies as LEVEL 3:** war outbreak or major escalation between states · nuclear incident · magnitude ≥6.5 earthquake or major disaster warning affecting Nepal/Example District region · national emergency · major Example Highway/Example City infrastructure closure · Example Bank security incident affecting customers · confirmed major cyber incident affecting Nepali users.

**Verification:** level 3 requires ≥2 distinct source domains OR a tier1 domain. Confidence is recomputed deterministically: HIGH (tier1 present or ≥2 independent established-news domains), MEDIUM (≥2 other distinct domains), LOW (otherwise). LOW blocks the routine send; if the event previously alerted at lower level, the alert renders *"Developing — not yet independently confirmed."* Rumor markers (rumor/unconfirmed/allegedly/unverified) cap the level at 2.

**Dedup:** an already-alerted event does not re-alert without material escalation (higher confidence rank or explicit escalation). **Spoilers are never a bypass** — the same veto applies to alerts.

## Academic result monitor

A dedicated monitor watches the TU IOST result feed for the student's CSIT semester results. It is split into a cheap Cloudflare Worker tripwire and a heavy GitHub Actions workflow:

1. **Tripwire (every 2 min)** — the Cloudflare Worker (`deploy/cloudflare-worker.js`) polls the TU IOST result feed (`https://iost.tu.edu.np/notices?notice_type=21`), detects new CSIT semester-result notices, and fires one `repository_dispatch` (`academic-result`). No PDF/OCR runs in the worker — it only detects and hands off.
2. **Heavy work (GitHub Actions)** — `academic-result.yml` runs `scripts/check_academic_result.py`, which downloads the result PDF, searches for the roll number supplied by the `ACADEMIC_ROLL_NUMBER` secret, updates `state/academic_state.json`, and emits a notification payload. PDF text extraction + OCR (tesseract) run here.
3. **Delivery** — the result is delivered via ntfy + a rendered Cloudflare Pages page (`scripts/render_academic.py` → `docs/academic/`), telling the student whether they passed or failed.
4. **Twice-daily backstop** — `academic-result.yml` also carries `cron: '17 4,10 * * *'` (minute 17, because GitHub's scheduled-run queue congests badly on `:00`; cut from hourly to twice daily — 10:02 / 16:02 NPT — on 2026-10-19 for Actions minutes, so a dead worker is now noticed within ~12h rather than ~1h). A worker dispatch is a *notification*, not a guarantee: if the worker dies, its KV binding is unbound, `GH_TOKEN` is invalid, or the TU zone is SERVFAIL, nothing dispatches at all. The schedule is what makes the monitor's own failure state observable instead of silent. Runs share one `concurrency: academic-result` group with `cancel-in-progress: false`, so a scheduled run and a worker dispatch serialise rather than racing; duplicate *notification* is prevented by the `already_announced` outcome in the state file, not by the concurrency group.
5. **A monitor failure now reaches the owner.** A degraded run is recorded as an artifact and published to ntfy, instead of vanishing.

**The no-burn invariant (R1).** A notice id enters the *examined* set **only** when the run obtained its **authoritative title** (the detail page) and made a positive decision. An unreadable notice is **`deferred`, never skipped**, and is retried forever. Before this, a notice could be permanently marked as seen without ever being read. Supporting rules: notices are processed in **ascending id order** and bounded per run (cap 300 examined); a run that hits N stops without touching N+1, leaving N unexamined; `last_examined_notice_id` advances **only** if every id in between is examined or absent, so a gap can never burn a notice.

**State schema.** `state/academic_state.json` moves to `schema_version: 2` at runtime. Migrating from schema 1 opens a bounded **200-id replay window** and resets the examined map, so the recent past is re-read under the new discipline — the window cannot re-notify, because announcements are tracked separately from examination.

**Exam routines and form deadlines** no longer depend on Google News luck: `config/exam_notices.json` supplies a config-driven feed table (matcher terms, deadline terms, `critical` flags) and `scripts/selfcheck_exam_sources.py` runs as an isolated pre-step in the workflow. It is deliberately fenced — `continue-on-error`, 3-minute timeout, writes only `state/exam_selfcheck.json` — so it can never block or delay a real result check. A non-zero exit means the matchers matched nothing, and says so loudly.

> ⚠️ **`config/exam_notices.json` ships UNVERIFIED.** It carries `verified_against_live: false` and `verified_at: null`. Do not trust its matcher terms or URLs until the self-check has actually run successfully from a network where `tu.edu.np` resolves. The committed `state/academic_state.json` is still `schema_version: 1` and will migrate on the first run that reaches it.

**Semester tracking** (`state/academic_state.json`, git-ignored): `current_semester` plus a per-semester record of whether the result is announced and whether the roll was found. The monitor polls `{current_semester} ∪ {failed semesters}`, so a back paper and the current semester are both watched. The Python script applies batch filtering (`notice_matches_student()`) so an older-batch result is never mis-attributed to the student's own batch.

## Spoiler protection (4 layers)

1. **Policy config** — `config/spoiler-policy.yml`, `default_policy: strict` (unknown titles treated strictly).
2. **AI classification** — every entertainment story gets `spoiler_level` (none–high) + reason; spoiler-heavy headlines are rewritten spoiler-safe; trailers are announced without describing content.
3. **Deterministic regex veto** — `validate_stories.py` drops any entertainment story rated medium/high or matching spoiler patterns (dies/killed/leaked/datamined/raw scan/scanlation/"ending explained"/reveal phrases…). Non-entertainment categories drop high-sensitivity hits only.
4. **Validator drop** — vetoed stories never reach the email; the monitor applies the same veto, so entertainment spoilers can never alert.

Configure per-series status in `config/spoiler-policy.yml`:

```yaml
default_policy: strict
series:
  - title: "One Piece"
    mediums: [anime, manga]
    status: {anime: watching, manga: not_started}
    policy: strict        # strict | conservative | relaxed
```

Statuses: `watching`/`completed` (anime side), `reading`/`caught_up` (manga side), `not_started`, `dropped`. Policies: `strict` drops all plot-progress news ahead of you; `conservative` allows release/casting news but drops plot details/leaks; `relaxed` drops only ending/major-twist reveals.

## Story labels

Attached automatically from event tracking (never by the AI):

- `[NEW]` — first time this event is reported
- `[UPDATED]` — tracked event whose facts materially changed (title/summary overlap < 0.85 vs stored known_facts)
- `[DEVELOPING]` — seen again within ~24 h, no material change
- `[ONGOING]` — seen again later than ~24 h, no material change

## Source reliability tiers

- **tier1** — official: government, NEA, DHM, TU/Example College, banks, developers/publishers
- **tier2** — established journalism
- **tier3** — specialized entertainment press
- **tier4** — community (Reddit/X/forums): discovery only, never cited as confirmation

Every summary starts with its claim status: Confirmed / Reported / Rumor / Speculation.

## State files (all bot-committed, scoped)

| File | Purpose | Committed by |
|---|---|---|
| `state/history.json` | 7-day sent ledger (normalized URL + title hash) — prevents re-reporting | daily-news bot |
| `state/active_stories.json` | Event tracking: first/last seen & reported, status lifecycle, deadlines, alert counts | breaking bot |
| `state/digest-archive/*.html` | Sent digest copies, 14-day retention | daily-news bot |
| `state/breaking_state.json` | Durable monitor state incl. pending-alert retry marker | breaking bot |
| `state/last_digest_sent.json` | Delivery marker `{date, time, channels, delivered}` — drives the fallback no-op guard. **One slot**, overwritten each run | daily-news bot |
| `state/digest_slot_ledger.json` | Git mirror of the Worker's delivery ledger (7 NPT days × 2 slots) — delivery facts copied from KV, build facts from the run that built them | prebuild seal job |
| `state/academic_state.json` | Academic monitor state: roll number, current semester, per-semester status, examined-notice floor | academic bot |
| `state/exam_selfcheck.json` | Result of the last exam-notice self-check; `digest-watchdog` reads its recency as a dead-man's switch | academic bot |

Event status lifecycle: `NEW → DEVELOPING → UPDATED → STABLE → RESOLVED/STALE`; a new sighting revives RESOLVED/STALE events to DEVELOPING. Never committed: `state/breaking_pending.json` (gitignored scratch), `state/breaking_log.json` (ephemeral telemetry, cap 500 rows), `state/email-preview.html` (dry-run preview, uploaded as artifact).

## Failure behavior

- **One source fails** → recorded in `source_results` with its error; the digest continues.
- **A source is never tried** (soft budget expired) → `skipped_budget`, counted as unknown rather than dead, so it inflates neither the failure count nor the health verdict.
- **A critical host is unreachable** → `source_health` state `degraded`; the digest carries a loud banner, a `Down:` line naming the hosts, and per-section `⚠️ Source unreachable — check <url>` status lines.
- **Nothing could be confirmed reachable** → state `unknown`; the freshness timestamp is **dropped**, not guessed. `research_status` is computed from these measurements, never taken from the agent.
- **Malformed AI output** → individual schema-violating stories are dropped; unparseable top-level JSON fails the workflow loudly (after 3 retry attempts).
- **Primary channel fails** → the backup chain fires: Gmail if website OR ntfy failed; Telegram if both primaries failed. A `delivered: false` marker never suppresses the fallback. (Untriggered in observed operation — see Delivery channels.)
- **All channels fail** → the delivery marker records `delivered: false`, so the GitHub-side fallback schedule retries the slot.
- **Monitor failure** → fail-safe silence (LEVEL 1 heartbeat logged, exit 0).
- **Academic monitor** → a transient TU/network failure is retried on the next 2-min tick; an unextractable PDF is treated as "unknown" and never marks the student failed. A notice whose authoritative title could not be read is deferred and retried, never burned.

## Dry runs

```bash
gh workflow run daily-news.yml -f digest_time=morning -f dry_run=true
gh workflow run breaking-monitor.yml   # dry_run defaults to true
gh workflow run academic-result.yml    # dry_run defaults to true
```

Daily-news dry run validates, builds, and uploads artifacts but skips all sends (the history update still runs afterward). Monitor dry run writes `state/email-preview.html` instead of sending. Academic dry run writes `state/academic_notify_preview.json` instead of delivering.

## Required secrets

The digest workflow reads `NTFY_TOPIC` (the topic name comes from that secret), `NTFY_SERVER`, `CF_PAGES_API_TOKEN`, `CF_ACCOUNT_ID`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `GMAIL_RECEIVER`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`. `DIGEST_PASSWORD` and `DIGEST_USERNAME` are gone (2026-10-30): they configured the Basic-auth gate on the Pages site, and the site is public by design. If either still exists as a Pages secret it is unused — delete it so nobody mistakes it for a control. The Cloudflare Worker uses `GH_TOKEN` (a PAT with Contents + Actions read/write), `NTFY_TOPIC` (the dead-man's-switch alarm), and a KV binding. Never commit any of these; refer to each by name only.

## Configuration guide

- **`config/interests.yml`** — add tracked titles under `games`/`anime`/`manga`/`manhwa`/`manhua`/`other` (each has `enabled` + `interests`), and global `excluded_keywords` (never included in any category). Commit and push — the next run picks changes up automatically.
- **`config/profile.yml`** — `location`, `education` (program/university/semester/institution), `bank.name`, `privacy.minimize_location_exposure`, and `priorities` weights used to localize and rank stories.
- **`config/spoiler-policy.yml`** — see YAML example above.
- **`config/opencode.json`** — the single source of truth for the CLI `pin`, `min_version` floor, run `flags`, and the model `ladder`. Change the ladder here only; `test_ladder_sync.py` fails CI on any drift elsewhere.
- **`config/collection.json`** — per-host timeouts, the `retry` policy (attempts, backoff, retry-on classes, DNS `resolve_timeout_sec`), `soft_budget_sec` and `max_workers` for the collector.
- **`config/exam_notices.json`** — exam-routine/form-deadline feed table and matchers. **Ships unverified** — see the academic section.

## Repository structure

```
daily-media-news/
├── .github/workflows/
│   ├── daily-news.yml           # digest build + delivery workflow
│   ├── digest-prebuild.yml      # builds the digest ahead of its slot, seals
│   │                            # it into KV, and mirrors the delivery ledger
│   ├── breaking-monitor.yml     # breaking watchdog (event-driven)
│   ├── academic-result.yml      # TU CSIT result monitor (event-driven + twice-daily backstop)
│   ├── digest-watchdog.yml      # late-delivery watchdog + empty-digest retry ladder
│   └── drift-canary.yml         # 06:15 NPT drift check + the daily test suite
├── config/                      # interests.yml, profile.yml, spoiler-policy.yml,
│                                #   opencode.json (CLI pin + model ladder),
│                                #   collection.json (timeouts/retries/budget),
│                                #   exam_notices.json (UNVERIFIED — see academic section)
├── deploy/                      # cloudflare-worker.js (tripwire + digest/academic
│                                #   dispatch + worker liveness heartbeat)
├── functions/                   # _middleware.js — public pass-through, no prompt
│                                #   at the Pages origin, in front of every
│                                #   static asset. Must stay at the repo root.
├── docs/                        # OPERATIONS.md (day-to-day running), DEPLOYMENT.md
│                                # (hosting & setup), PKB.md (history), digests/
│                                # (rendered website), academic/ (result pages)
├── prompts/                     # daily-digest.md, breaking-classify.md (AI stages)
├── scripts/                     # collect_news, source_health, validate_stories,
│                                #   enrich_media, build_email, render_digest, update_history,
│                                #   story_state, breaking_monitor, check_academic_result,
│                                #   selfcheck_exam_sources, render_academic, ai_classifier,
│                                #   mal_status
├── templates/                   # email.html (digest), alert.html (breaking)
├── state/                       # runtime state (see table above)
├── tests/                       # fixture + render + collection + academic + ai +
│                                #   breaking + media + security suites (see testing
│                                #   section)
└── README.md
```

All scripts are stdlib-only Python 3 (Ubuntu `python3` / Windows `py` 3.13).

## Local development & tests

**410 tests across seven suites, enforced daily** by the `test-suite` job in `drift-canary.yml` (06:15 NPT, before the morning slot). Before that job existed, CI enforced exactly one file (`test_ladder_sync.py`); the rest of the suite could rot silently. All seven are stdlib-only and offline.

**Environment notes:** use `py` not `python` (MS Store stub on Windows); set `PYTHONIOENCODING=utf-8` for emoji output; `pytest` is NOT installed.

```powershell
# unittest-discover suites (3) — 298 tests
py -m unittest discover -s tests\render     -t .   # 65
py -m unittest discover -s tests\collection -t .   # 102
py -m unittest discover -s tests\academic   -t .   # 131

# custom `assert` runners (4) — 112 tests. These are NOT unittest modules
# and have no runner entry point: invoke them as scripts.
py tests\ai\test_ai_classifier.py          # 12
py tests\breaking\test_breaking_monitor.py  # 29
py tests\breaking\test_ladder_sync.py      # 7
py tests\media\test_media_extraction.py    # 64
```

⚠️ **`py -m unittest discover -s tests -t .` does not work** — there is no `tests/__init__.py` (only the subpackages have one), so discovery over the whole directory aborts. Discover **per subdirectory** as shown above.

Fixture suite (no test framework; compare survivors/drops against `tests/fixtures/manifest.json`):

```powershell
py scripts\validate_stories.py --in tests\fixtures\input_full_matrix.json `
  --out "$env:TEMP\v.json" --history "$env:TEMP\none.json" `
  --config-dir tests\fixtures\config
py scripts\build_email.py --in "$env:TEMP\v.json" `
  --out-html "$env:TEMP\e.html" --out-text "$env:TEMP\e.txt" `
  --template templates\email.html
```

Other fixtures cover dedup, malformed input, empty input, invalid JSON, event labeling, deadlines, and relevance sorting — see `tests/README.md` (note its date caveat for history fixtures). The breaking monitor suite runs against a fake network/AI/SMTP stack; the media suite covers `og:image`/`og:video` extraction and Google News URL decode.

## Known limitations

- **Scheduler delay**: GitHub fires scheduled workflows late (+17–31 min observed); the Cloudflare Worker dispatch normally avoids this.
- **`config/exam_notices.json` is unverified**: `verified_against_live: false`. Its matchers and URLs are unproven until `selfcheck_exam_sources.py` runs successfully from a network where `tu.edu.np` resolves.
- **`state/academic_state.json` is still schema 1 on disk**: the v2 migration and its 200-id replay window run inside the monitor, so they have not been exercised by a real result sweep yet.
- **Gmail/Telegram backup paths are unproven in production**: correctly gated, never triggered. Only the test suites have executed them.
- **Upstream DNS is a hard dependency**: a full `tu.edu.np` SERVFAIL outage degrades every TU/IOS T-backed section. The digest now says so loudly and names the URLs, but it cannot recover the data — only the `site:` sweeps and other sources still land.
- **AI stage latency variance**: 2.5–18 min observed under free-tier load; retries span three free models.
- **Single AI classification pass** (+ deterministic backstop): no iterative re-research; the agent's supplemental search budget is capped at 5 calls.
- **zh-CN feed sparse**: the ZH sweep often yields few candidates (manhua list is currently empty; it falls back to manga titles).
- **Official notice pages are best-effort**: crude link-text keyword extraction, never fatal.
- **ntfy image attach**: only fires when the top story has an image (the weather/API item often has none).
- **Academic worker tripwire has no batch filter** (intentional): the Python script does batch filtering; wrong-batch notices cost one workflow run each but are handled correctly.
- **`drift-canary.yml` test counts are stale**: its comments say 362 tests and expect 82/103 for collection/academic; the suites actually run **410** (102/131). The job still passes because each step's real count is what `unittest` reports — but the comments and the `expect` labels understate reality.

## License

MIT.
