# Project Knowledge Base — daily-media-news

Durable knowledge for future sessions. Verified 2026-09-17. Do not edit product
source code from this file; record decisions here instead.

## 1. Repo state (verified 2026-08-30)

- Repo: this repository (git, branch `main`).
- origin/master HEAD = `9328e80` (chore: reset delivery marker); workflow's own
  state commit `d2552a8` sits on top. Working tree clean.
- Key commits:
  - `65baa93` — feat: media-rich digests, ntfy/website primary delivery,
    big-pickle model ladder.
  - `9328e80` — chore: reset delivery marker.
- Secrets (set via `gh`): `NTFY_TOPIC` = a topic name you generate yourself;
  `NTFY_SERVER` exists; `DIGEST_PASSWORD` (Basic Auth).
  **Superseded 2026-10-03:** `DIGEST_PASSWORD` now lives in exactly **one**
  place — a **Pages** secret on `daily-digest`. It briefly had to exist in two,
  because the now-deleted `digest-auth` Worker held a second copy; that Worker is
  gone. See §38.4.
- Current working-tree `state/last_digest_sent.json` = `{}` (post-reset).

## 2. Live verification (2026-08-30)

- GitHub Actions run `33324524933` → SUCCESS (big-pickle agent, attempt 1).
- ntfy publish SUCCESS (id=`MRX5fqSLFihZ`); earlier HTTP 400 fixed by using a
  valid topic (no spaces/invalid chars).
- Cloudflare Pages deploy SUCCESS.
- Gmail/Telegram backups correctly SKIPPED (primaries succeeded).
- Delivery marker written in new schema `{date, time, channels, delivered}` with
  `delivered: true`. Full chain works end-to-end.

## 3. Delivery architecture (current)

- **Primary**: ntfy + website (Cloudflare Pages).
- **Fallback**: Gmail fires only if website OR ntfy failed; Telegram only if
  BOTH failed.
- **No-op guard**: `state/last_digest_sent.json` marker with `delivered` field;
  legacy markers treated as delivered; `delivered: false` must NOT suppress
  fallback channels.
- ntfy step has `continue-on-error`; success recorded via step output +
  `state/.ntfy_ok`.
- ntfy click URL = `https://daily-digest.example.pages.dev/digests/%s-%s.html` —
  the Pages **origin**, directly. Built once in the ntfy heredoc in
  `daily-news.yml` and consumed three times: the payload's `click`, the
  `Open digest` action URL, and the `full digest:` truncation notice.
  **Superseded 2026-10-03:** this used to be the `digest-auth` Worker's
  `workers.dev` host, which produced **two** Basic-auth prompts with two
  different usernames (`digest` at the Worker, the owner's email at the origin) —
  which is why the owner was locked out of the ntfy link on mobile. The Worker
  was deleted from the repo and from Cloudflare; **do not reintroduce a proxy in
  front of the origin.** The control is `functions/_middleware.js` at that origin.
  Guarded by `tests/render/test_render_personal.py::DigestLinkHostTests` and
  `tests/workflows/test_workflow_lint.py::DeletedDigestAuthWorkerTests`.
  See §38.

## 4. Model ladder

- **Seed** (`config/opencode.json`): `opencode/big-pickle` (primary) →
  `opencode/mimo-v2.6-flash-free` → `opencode/nemotron-3.5-lightning-free`.
- `scripts/model_selector.py --all` returns the WHOLE live-discovered free pool
  in rank order, and the agent step walks it bounded by WALL CLOCK, not by a
  count: per-attempt `timeout 360s`, whole walk `1800s`, and it stops once one
  more attempt would get less than `90s`
  (`WALK_PER_ATTEMPT_SEC` / `WALK_TOTAL_BUDGET_SEC` / `WALK_MIN_ATTEMPT_SEC`).
- No fixed inter-attempt delay: the deadline loop replaced the `0 20 60`
  backoffs (REVISED 2026-10-18 — run 39117668287 discovered ten free models,
  walked a capped three, lost two to the 600s cap and the third to `rc=1`, and
  produced no digest while seven models sat untried).
- Job `timeout-minutes: 45`; agent step `timeout-minutes: 35`.
- `opencode` run flags: `--auto --model` (NOT `--print-logs`; `--standalone` is v2/opencode2-only, v1 run is already in-process).
- `opencode` run has NO built-in timeout.
- CLI pin: `opencode-ai@1.18.31` (binary `opencode`); provider gate requires >=1.17.0 (run 35171159132).

## 5. Root-cause findings — media not appearing (main open issue)

- **Feed level**: only news source is Google News RSS (`collect_news.py` line 80),
  which carries NO media (`media:content`/`media:thumbnail`/`enclosure`).
  `extract_media()` returns `(None, None)`; candidates have no image/video.
  `validate_stories.py` lines 82-83 confirm: "Google News RSS carries no media
  today".
- **Agent**: prompt (`prompts/daily-digest.md`) is passthrough-only for media —
  agent must omit image/video when candidate has none; forbidden to invent.
- **Validator**: `validate_stories.py` does NOT merge media from candidates into
  stories; it only validates media the agent wrote. Passthrough match key is
  `story.source_url` vs `candidate.url` — but stories use outlet homepages while
  candidates use Google News redirect URLs, so they never match → even genuine
  media would be dropped.
- **Renderer**: `render_digest.py` renders media ONLY when story has
  image/video fields; otherwise plain text card.
- **Deployed page**: `docs/digests/2026-08-30-evening.html` was rendered by the
  OLD pre-media renderer (`ede35a1`) — no production run has executed under the
  new media code yet.
- **Net**: media is broken at the first link (feed level) and every downstream
  stage propagates its absence.

## 6. Root-cause finding — ntfy raw URLs

- `daily-news.yml` story_md appends `🔗 <source_url>` as bare text (lines
  502-504) — no markdown link syntax; ntfy clients don't reliably autolink.
- Telegram backup step DOES linkify (lines 747-751).
- Payload uses `'markdown': True` as JSON field (line 575) — no `Markdown: yes`
  header.
- `source_url`s are outlet homepages, not article URLs.
- ntfy attach (image) only fires if a story has an image.

## 7. Planned fixes (tomorrow's work — NOT yet implemented)

- **a. Make media flow**: (i) add real media sources (direct RSS feeds with
  media, or fetch article pages for og:image); (ii) strengthen prompt;
  (iii) candidate→story media merge in `validate_stories.py` keyed on robust
  match (title or normalized URL); (iv) fix passthrough match-key mismatch.
- **b. ntfy message**: replace bare URLs with markdown links (`[label](url)` or
  "Read article"); verify markdown rendering (may need `Markdown: yes` header
  instead of JSON field); keep message ≤ 4096 bytes.
- **c. Website enrichment**: genuinely richer digest (media cards, hero, more
  sections); verify with a real run.
- **d. After fixes**: local render verification with media-rich fixtures, then a
  live run to confirm images/videos appear on website and ntfy.

## 8. User-side items (remind user)

- Subscribe to your `NTFY_TOPIC` in the ntfy app.
- Verify the Pages origin is the notification target (the ntfy click URL is
  `https://daily-digest.example.pages.dev/digests/...`) and that the
  `DIGEST_PASSWORD` **Pages** secret is set. A 401 on an anonymous probe does
  **not** prove the secret is set — the middleware fails closed either way, so
  authenticate for real to confirm. **Superseded 2026-10-03:** this item used to
  ask the user to verify the `digest-auth` Worker was deployed; that Worker is
  deleted and must not be recreated.
- README still describes old Gmail-primary pipeline (docs update pending).
  - **SUPERSEDED (2026-09-27, see §29).** A README update is **IN FLIGHT** with
    a different agent as of this date. Do not assert either the old or the new
    README content from this file, and do not edit `README.md` from a PKB task —
    read the file itself.

## 9. Environment notes

- Use `py` not `python` (MS Store stub).
- `PYTHONIOENCODING=utf-8` for emoji output.
- Local clock skewed ~5.75h ahead of GitHub — always use
  `gh run view --json timestamps`.
- pytest NOT installed; fixture suite runs via `tests/README.md` manifest
  runner; media suite: `py tests\media\test_media_extraction.py` = 13 tests.

## 10. Commits pushed to origin/master (verified 2026-08-31)

- `dc376b9` — "fix: media enrichment pipeline — og:image/video enrichment,
  hardened URL validation, ntfy markdown links" (5 files:
  `.github/workflows/daily-news.yml`, `prompts/daily-digest.md`,
  `scripts/enrich_media.py` NEW, `scripts/validate_stories.py`,
  `tests/media/test_media_extraction.py`).
- `e732095` — "fix: entertainment-only spoiler veto, same-day forecast dedup,
  story-count floor" (`scripts/validate_stories.py`,
  `prompts/daily-digest.md`, `tests/fixtures/manifest.json` + 2 new fixture
  inputs).
- `b30c84a` — workflow auto marker commit from run `33361875753`.

## 11. Media pipeline (live-verified 2026-08-31)

- `scripts/enrich_media.py` (NEW, ~497 lines, stdlib-only) enriches validated
  stories with `og:image`/`og:video` from candidates (exact URL → normalized
  URL → title match ≥0.85).
- Hardened: rejects private/local-host og URLs and fetch redirects (reuses
  `validate_stories.is_private_or_local_host`), rejects host-less URLs, handles
  `og:image:secure_url` / `og:video:url` / `og:video:secure_url` with documented
  priority; atomic write; thread pool 6; exit 0 always.
- Workflow step "Enrich stories with media (og:image/video)" runs after
  Validate, before Build email/Render (continue-on-error, timeout 5).
- `validate_stories.py` media passthrough now 3-tier (`by_raw_url`/`by_url`/
  `by_title`).
- ntfy: `[Read article](url)` markdown links + `Markdown: yes` header (JSON
  `markdown` field removed — no-op).
- Tests: `tests/media/test_media_extraction.py` = 43 tests.

## 12. Validator fixes (root cause of empty digests on 08-31)

- (a) Spoiler veto now entertainment-only — `HIGH_SENSITIVITY_RE` removed;
  non-entertainment hard news with death/dies/killed survives.
- (b) Recurring forecast URLs (open-meteo) dedup by same-calendar-day instead of
  7-day URL TTL (`is_forecast_url` + `hist_forecast_urls_today`); true news URL
  dedup unchanged.
- (c) `prompts/daily-digest.md` soft ≥4-story floor.
- Fixture suite now 23 cases.

## 13. Live verification evidence (run 33361875753, morning 2026-08-31)

- Commit `e732095`, SUCCESS ~1.75 min; big-pickle attempt 1.
- STORIES WRITTEN 2; validate spoilers: 0 dropped, survivors: 2.
- enrich_media: 2 stories, 1 enriched, 1 failed (thunderstorm story has no
  og:image — expected).
- ntfy id `bAfIFbpegNKP` (2 `[Read article]` links, no image attach — top story
  lacks og:image).
- Pages deploy `https://35750b53.daily-digest.example.pages.dev` — deployed digest
  contains `<img class="card-thumb">` on the flood story
  (lh3.googleusercontent.com og:image).
- Marker `{"date":"2026-08-31","time":"morning","channels":{"website":true,
  "ntfy":true,"gmail":false,"telegram":false},"delivered":true}`.
- Gmail/Telegram skipped.
- Prior run `33359616793` (evening, `dc376b9`) proved pipeline mechanics but had
  0 stories (both validator bugs) — now fixed.

## 14. Remaining items (verified 2026-08-31)

- `docs/PKB.md` still untracked (user decision whether to commit).
- README still describes old Gmail-primary pipeline (docs update pending).
  - **SUPERSEDED (2026-09-27, see §29).** A README update is **IN FLIGHT** with
    a different agent as of this date. Do not assert either the old or the new
    README content from this file, and do not edit `README.md` from a PKB task —
    read the file itself.
- ntfy image attachment only fires when a story has an image (top story often
  weather/API without og:image — consider og:image fallback for weather later if
  desired).
- ~~digest-auth worker confirmed deployed (HTTP 401 = auth active)~~ —
  **OBSOLETE 2026-10-03.** The Worker was deleted (it only proxied, and its
  second username locked the owner out of the ntfy link). Access control is
  `functions/_middleware.js` at the Pages origin; verify there, not on a Worker.
- User should subscribe to their `NTFY_TOPIC` in the ntfy app.

## 15. Google News URL decode fix — the key breakthrough (verified 2026-08-31, late)

### Problem
Google News RSS gives encrypted redirect URLs (`news.google.com/rss/articles/CBMi...`)
that CANNOT be decoded offline. The article URL is encrypted inside the blob; there is
no plaintext `http` anywhere in it. The old base64-scan method is dead — it never
finds a real URL.

### Working method: batchexecute decode
Two HTTP requests:
1. GET the interstitial page at the redirect URL; scrape `data-n-a-sg` /
   `data-n-a-ts` hidden fields.
2. POST to `news.google.com/_/DotsSplashUi/data/batchexecute` with `f.req` payload
   containing those values plus the encrypted URL.
3. Parse `)]}'` prefixed JSON response → extract `garturlres` payload → real article
   URL.

Implemented in `scripts/enrich_media.py` `decode_google_news_url()` with:
- Fast offline base64 path first (cheap, may work for some URLs).
- batchexecute fallback (the reliable path).
- Throttling: 0.75s delay between requests, module-level threading lock.
- Caching via `urllib.request` opener.
- Browser UA + `CONSENT` cookie (required by Google to return the interstitial
  instead of a hard block).
- Never-crash: returns `None` on any failure, never raises.
- SSRF-safe: decoded URL still passes `_is_private_host` check.

### Live proof
Resolves to real article URLs, e.g.:
`https://www.cbsnews.com/news/nepal-flooding-warning-death-toll-rescue/`
Previous behavior: ntfy/website links pointed to `news.google.com` redirects (useless
to reader). Now: real destination URLs.

### Commits
- `54fccfa` — "fix: resolve Google News redirect URLs to real articles (batchexecute
  decode), clean Read article links"
  Files: `scripts/enrich_media.py`, `scripts/render_digest.py`,
  `tests/media/test_media_extraction.py`
- `e40c640` — "chore: reset delivery marker to force fresh evening run [skip ci]"
  Files: `state/last_digest_sent.json` → `delivered:false`

### Tests
`tests/media/test_media_extraction.py` now 57 tests (up from 43).

## 16. Render digest link fix (verified 2026-08-31, late)

`scripts/render_digest.py` primary article link now renders as:
```html
<a class="story-link" href="...">Read article →</a>
```
instead of the full URL as link text. `supporting_sources` links unchanged.

## 17. Live verification — evening digest 2026-08-31 (late)

Verified against live deployed output (not run log):
- Deployed evening digest has REAL article URLs in "Read article →" links
  (open-meteo.com, bleedingcool.com) — NOT `news.google.com` redirects.
- Generic `lh3.googleusercontent.com` thumbnail is ABSENT (anime story uses a
  YouTube iframe instead).
- ntfy message uses real article URLs.
- Run `33407472748` was a fallback no-op (Cloudflare worker had already dispatched
  and set the marker). The deployment came via the worker path; the GitHub enrich
  step didn't run in that specific run, but the deployed output confirms the new
  code is live.

## 18. Known issues and notes (2026-08-31, late)

- **Marker misspelling**: `state/last_digest_sent.json` uses key `"nfty"` (instead
  of `"ntfy"`) for the ntfy channel flag. Cosmetic only, not blocking.
- **Cloudflare worker vs GitHub enrich path**: The Cloudflare worker can dispatch
  the digest independently of the GitHub enrich step. When the worker dispatches
  first, the GitHub run no-ops (marker already set). This means the batchexecute
  decode step may not always run. **Follow-up needed**: either (a) add the same
  decode logic to the worker path, or (b) decide the worker is primary and GitHub
  is backup. Flagged for investigation.
- **ntfy image attachment**: Only fires when a story has an image. Top story is
  often the weather/API item (open-meteo) which has no `og:image`.
- `docs/PKB.md` still untracked (user decision whether to commit).

## 19. Doom-loop escalation pattern (recorded 2026-08-31, late)

### Context
A `qa-tester` subagent got stuck in a doom loop (repeated null output) running a
long chained `gh`/`curl` verification sequence.

### Root cause (per solver)
Known `opencode/big-pickle` provider bug (infinite loop after tool calls complete —
opencode issue #26220), aggravated by long single-task sequences with many tool
calls.

### Mitigation that worked
1. Split verification into **3 small bounded steps** (trigger / watch / verify).
2. Use `gh run watch --exit-status --interval 10` instead of manual polling loops.
3. Keep each subagent to **≤3 tool calls** per task.

### Reusable pattern
When delegating verification or monitoring tasks:
- Never chain >3 tool calls in a single subagent task.
- For CI monitoring: trigger → `gh run watch` (blocking, built-in) → result check.
- If a subagent emits repeated null/empty output, STOP immediately; do not retry
  the same approach. Split the work into smaller chunks.

## 20. TU CSIT academic result monitor (verified 2026-09-03)

### New feature (committed `5e6fd86`)
- **`scripts/check_academic_result.py`** (NEW): polls the TU IOST result feed
  (`https://iost.tu.edu.np/notices?notice_type=21`), detects CSIT semester result
  notices, downloads the result PDF, searches for the configured roll number, updates
  `state/academic_state.json`, emits a notification payload. Exit codes:
  `0` normal/no-op, `1` config fatal, `2` transient network, `3` corrupt state.
- **`.github/workflows/academic-result.yml`** (NEW): triggered by
  `repository_dispatch` (type `academic-result`) + manual `workflow_dispatch`.
  Runs the monitor, delivers via ntfy + Cloudflare Pages website, commits state.
- **`deploy/cloudflare-worker.js`** extended with a `*/2 * * * *` academic sweep:
  cheap tripwire that polls the IOST result feed every 2 min, detects new CSIT
  result notices, dispatches `repository_dispatch` to GitHub Actions. No PDF/OCR
  in the worker (heavy work runs in GitHub Actions where tesseract is installed).
- **`scripts/render_academic.py`** (NEW): renders the result notification HTML
  page into `docs/academic/`.
- **Tests**: `tests/academic/test_academic_result.py` (33 tests) +
  `tests/media/test_media_extraction.py` now 64 tests.
- **State file** `state/academic_state.json`: `schema_version` 1,
  `roll_number` (from the `ACADEMIC_ROLL_NUMBER` secret, never committed),
  `current_semester`, per-semester
  `{status, result_announced, roll_found, result_date, notice_id, pdf_url}`,
  `last_checked_notice_id`, `last_poll_time`. State machine:
  `pending→passed/failed` on announcement, `failed→passed` on later re-exam
  result, `current_semester` advances on ANY announcement (pass OR fail), failed
  semesters stay in the poll set until `roll_found`.

### Academic facts the monitor relies on (shape, not one owner's record)
- The roll number is CONSTANT across semesters, so one configured value works
  for every result the student has ever appeared in.
- A semester result is a notice whose title names the semester and batch; the
  roll is looked for in the downloaded PDF, in both the joined form and the
  column-split form the sheet actually prints.
- Found roll → PASSED for that semester; roll absent from a result that covers
  the student's batch → FAILED. A FAILED semester stays in the poll set until
  a later re-exam result contains the roll.
- `current_semester` advances on ANY announcement, pass or fail.
- **Poll set** = `{current_semester} ∪ {failed semesters}`, so a back paper and
  the current semester are both watched.
- This public copy ships no real academic history: `state/academic_state.json`
  is git-ignored and the roll comes from the `ACADEMIC_ROLL_NUMBER` secret.

### Audit findings that were fixed (important lessons)
- A FALSE "Sem 4 PASSED" page/payload was created during development from a
  mislabeled notice (14559 = actually III Semester) and a wrong PDF (notice 9300
  = V Semester, an older batch, did not contain the configured roll). REMOVED. **Lesson**:
  never trust a notice id/title at face value — always verify the batch and
  semester against the actual PDF content.
- The monitor regex originally had NO batch filter, so an older-batch CSIT result
  could be mis-attributed to the student. FIXED with `notice_matches_student()`
  batch logic: accepts batch >= the student's batch year, no-batch (ambiguous), and
  older-batch ONLY for failed semesters (re-exam case). Rejects older-batch for
  pending/passed semesters.
- Result dates were originally hand-seeded and wrong. Corrected to verified
  publication dates.

### Image fix (committed `5e6fd86`)
- `scripts/enrich_media.py` now blocks generic Google placeholder/CDN domains for
  `og:image`/`og:video`: `lh3/lh4/lh5/lh6.googleusercontent.com`,
  `docs.google.com`, `drive.google.com`, `ssl.gstatic.com`, `www.gstatic.com`,
  any `*.googleusercontent.com`, `placehold.co`, `placeholder.com`,
  `via.placeholder.com`, `picsum.photos`, `dummyimage.com`. Case-insensitive,
  subdomain matching. Blocked → story falls through to no-image path (no generic
  fallback substituted). 64 tests pass.

### Known limitations / follow-ups
- The Cloudflare Worker academic sweep has NO batch filter (intentional — it's a
  cheap tripwire; the Python script does batch filtering). Extra dispatches for
  wrong-batch notices cost one workflow run each but are handled correctly.
- The worker's `*/2 * * * *` cron and KV binding must be configured in the
  Cloudflare dashboard (the code is committed but the cron trigger + KV binding
  need to be set up in the dashboard).
- The monitor's regex has no batch filter in the worker; the Python script's
  `notice_matches_student()` handles batch filtering.
- `docs/PKB.md` still untracked (user decision whether to commit).
- README still describes old Gmail-primary pipeline (docs update pending).
  - **SUPERSEDED (2026-09-27, see §29).** A README update is **IN FLIGHT** with
    a different agent as of this date. Do not assert either the old or the new
    README content from this file, and do not edit `README.md` from a PKB task —
    read the file itself.

## 21. P0 hardening: single-source CLI config + drift canaries (verified 2026-09-17)

Context: two drift failures in 2 days silently killed digests (a version-gate
miss and a v2-only `--standalone` flag on the v1 CLI), with no early warning.
Manager evidence (not independently re-verified here): live run 35221963926
succeeded with `--auto --model` on 1.18.31; repo HEAD `294fc72` (drops the
v2-only `--standalone` flag).

Contract (single owner, additive changes only):

- **`config/opencode.json`** is the SINGLE source of truth:
  `pin: opencode-ai@1.18.31`, `min_version: 1.17.0`,
  `ladder: [opencode/big-pickle, opencode/mimo-v2.6-flash-free,
  opencode/nemotron-3.5-lightning-free]`, `flags: [--auto, --model]`.
  Exact pin, never `latest`; free-tier only; no new secrets.
  - **PARTLY SUPERSEDED (2026-09-27, see §28; revised 2026-10-18).** `pin`
    and `min_version` ARE read at runtime (`jq -r .pin`,
    `jq -r .min_version`); the `ladder` is a runtime SEED resolved by
    `scripts/model_selector.py` (config seed + live provider discovery). The
    **`flags` key is DECORATIVE**: the verify gate in
    `daily-news.yml`, `breaking-monitor.yml` and `drift-canary.yml` hardcodes
    `--auto` / `--model` / `--standalone` in `opencode run --help | grep -q`
    rather than iterating `.flags`. So the flags are documented in config and
    *tested* for presence by `tests/breaking/test_ladder_sync.py`, but a change
    to that key would **not** change behaviour. Read this bullet as "config is
    single-sourced for the version pin and the model ladder", not for flags.
- **Workflows read it, never hardcode it**: both `daily-news.yml` and
  `breaking-monitor.yml` install via `jq -r .pin`, gate via
  `jq -r .min_version`, and the daily agent loop resolves the ladder via
  `python3 scripts/model_selector.py --select` (backoffs 0/20/60, extras
  default 30s).
  `breaking_monitor.py` reads the ladder at import (`AI_MODELS`) with an
  embedded fallback that MUST match the config; same for
  `ai_classifier.py` `PROVIDERS` (names: big-pickle / mimo /
  nemotron-lightning).
- **Verify gate (both workflows, before any agent spend)**: version floor +
  flag canary — `opencode run --help` must show `--auto`/`--model` and must
  NOT show `--standalone` (v2/opencode2-only; v1 run is already in-process).
- **Ladder alignment**: `breaking_monitor.py` docstring + `AI_MODELS`,
  `ai_classifier.py` `PROVIDERS`, and `prompts/daily-digest.md` header all
  use the daily ladder. Stale IDs (`nemotron-3-ultra`, `hy3-free`) are gone
  from code; the `--standalone` history NOTEs stay as allowlisted comments
  so the flag is never re-added.
- **`tests/breaking/test_ladder_sync.py`** (7 tests, exits nonzero on
  failure): config contract, stale-reference scan (`*.yml`/`*.py`/`prompts/*`
  with `v2/opencode2-only` allowlist), no hardcoded `opencode/<model>` in
  `.github`, workflow config-read asserts, breaking/classifier ladder ==
  config ladder, `--auto`/`--model` presence. Run:
  `py tests\breaking\test_ladder_sync.py`. `tests/ai/test_ai_classifier.py`
  fakes renamed to the new provider names (big-pickle / mimo).
- **`.github/workflows/drift-canary.yml`** (cron `30 0 * * *` = 06:15 NPT,
  ~95 min before the morning slot): `docs-lint` job (ladder-sync test +
  typo sweep over docs/README/templates/deploy/config/state, PKB excluded
  as dated history + `::warning::` staleness note when Verified >30 days)
  and `drift-canary` job (install pin → verify-gate → dispatch daily
  `dry_run=true` + `gh run watch` → breaking `--dry-run --window-min 5
  --max-candidates 5`). Costs one extra agent dry-run per day.
- **`.github/workflows/digest-watchdog.yml`** (cron `0 4,16 * * *` =
  09:45/21:45 NPT, ~2h after each slot): checks
  `state/last_digest_sent.json` date == today-NPT, slot matches the UTC-hour
  expectation, and `delivered` (missing = delivered, legacy `{slot: date}`
  shape supported). On miss: ntfy ping (reuses existing secrets,
  best-effort) + `::error::` + one deduped `gh issue`, then exit 1.
- Timeouts/delivery gating untouched (daily 45 min, agent 35 min, breaking
  10 min; primary/backup channel order unchanged).

## 22. 2026-09-17 session snapshot — drift fixes + P0 pending (HEAD 294fc72)

- **Morning digest missed twice, both fixed and pushed (tree clean at
  `294fc72`):**
  - Run `35171159132` FAILED — provider gate `OpenCode 1.17.0+ required` vs
    installed beta `0.0.0-beta-17823`. Fix `f9b63e3`: pin
    `opencode-ai@1.18.31`, binary `opencode`, `>=1.17.0` gate.
  - Run `35195161365` FAILED — `opencode run --standalone` invalid on
    v1.18.31 (v2/opencode2-only global flag; v1 help shows `--auto`/`--model`
    only). Fix `294fc72` (`f9b63e3..294fc72`): dropped `--standalone`,
    kept `--auto --model` in `daily-news.yml` + `breaking_monitor.py`
    (missing `--auto` added) + PKB. Reviewer approved.
- **Live proof** — run `35221963926` (morning, live, headSha `294fc72`,
  created `2026-09-17T12:35:20Z`, updated `12:42:44Z`) SUCCESS in ~7m24s;
  ntfy primary OK, Gmail/Telegram skipped by design.
- **P0 hardening BUILT but UNCOMMITTED (11 files, needs user approval to
  commit+push).** Verified `git status`: 7 modified +
  4 untracked; all suites green `33+64+7+12+29=145` (33 academic + 64 media
  + 7 ladder-sync per PKB §§20-21; 12+29 other suites per manager evidence).
  - Modified (7): `.github/workflows/breaking-monitor.yml`,
    `.github/workflows/daily-news.yml`, `docs/PKB.md` (§21),
    `prompts/daily-digest.md`, `scripts/ai_classifier.py`,
    `scripts/breaking_monitor.py`, `tests/ai/test_ai_classifier.py`.
  - Untracked (4): `.github/workflows/digest-watchdog.yml`,
    `.github/workflows/drift-canary.yml`, `config/opencode.json`,
    `tests/breaking/test_ladder_sync.py`.
  - Note: `git ls-files` shows `docs/PKB.md` IS tracked (contrary to older
    "untracked" notes); the pending change is modification §21, not a new
    file.
- **Frozen contracts (do not re-hardcode):** npm package `opencode-ai`
  exact pin (never `latest`), binary `opencode`, flags `--auto --model`
  (v1); `--standalone` is v2/opencode2-only, never re-add. Ladder
  single-sourced from `config/opencode.json` (pin 1.18.31, min 1.17.0;
  big-pickle / mimo-2.5-free / nemotron-3.5-lightning-free). Crons:
  breaking `*/3`, academic worker `*/2`, digests `35 1/13`, canary
  `30 0` (06:15 NPT dry-run), watchdog `0 4,16` (09:45/21:45 NPT
  miss-alert). `NTFY_TOPIC=<your-topic>`; breaking-monitor
  worker alive, KV present, `GH_TOKEN` present.
- **State at close:** `state/last_digest_sent.json` =
  `{"date":"2026-09-16","time":"evening","delivered":true}`
  (website+ntfy true, gmail/telegram false).
- **Lessons:** provider version-gates + CLI flag renames cause silent
  morning misses — canary (early dry-run) + watchdog (miss-alert + deduped
  issue) are the fix; `wrangler.toml */2` academic sweep already live;
  README still describes old Gmail-primary pipeline (**SUPERSEDED 2026-09-27 —
  update IN FLIGHT with another agent, see §29; do not assert its content from
  here**); PKB §§1/6 history is dated (pre-Sept) — read §§21-22 first, and
  §§23-30 for current reality.
- **Next action (blocked on user):** approve → commit + push the 11 P0
  files above. Do NOT trigger workflows manually; next live proof is the
  scheduled morning digest + canary/watchdog runs.

Newest section: §37 (2026-09-28). §§23-37 supersede parts of §§8, §14, §20,
§21, §22 and §30; read §§23-37 first for current reality.

## 23. Reliability rebuild (uncommitted, built 2026-09-27) — read this first

Context. While this work was in progress, Tribhuvan University's entire
`tu.edu.np` DNS zone was returning SERVFAIL from Google's public resolver
(8.8.8.8) — an upstream outage we cannot fix. It exposed that the pipeline was
**silently lying**: it printed "Information checked through: <time>" while every
TU fetch was dead, because `research_status` was the LLM agent's own opinion and
`prompts/daily-digest.md` told it to keep `"full"` when most domains succeed. A
TU-only outage therefore produced no banner at all. Separately, `sources_failed`
was collected and then **discarded** by every downstream consumer.

Status: **all uncommitted** on top of base `2e5fd95` (which is also current
`HEAD`). 13 modified files + 4 untracked config/script files + 3 untracked test
paths. Nothing pushed, no workflow triggered. Verified by reading the code and
by running all seven suites (§27, §30).

Three units: deterministic source truth (§24), academic result path (§25),
surfaces (§26).

## 24. Unit 1 — deterministic source truth

### `scripts/source_health.py` (NEW)

`compute_source_health(source_results, critical_hosts, now_npt="")` — a **pure,
total** function. No I/O, no clock, **no imports from other project modules**;
`now_npt` is passed in by the caller precisely so it stays testable. Never
raises: any surprise degrades to state `"unknown"` with zeroed counters and an
empty `checked_at_npt`.

Hard rules baked in (do not relax):
- `"ok"` means **REACHABLE**, never "produced items". A 200 with an empty RSS
  body is `ok`.
- `"skipped_budget"` is **NOT** `"failed"`. A task the soft budget never started
  is UNKNOWN, not DEAD. Counting it as a failure is a lie.
- `ok` means REACHABLE. `critical.hosts_down` is ok-shielded: a host with one
  failed and one successful task is a working source, and calling it
  "unreachable" would be the same class of lie this signal exists to stop.
- `critical.reasons` covers failed **and** skipped; `hosts_down` is
  failed-only. These two are deliberately different — a budget-skipped critical
  host is a real gap the reader must be told about, but it is not proof the host
  is down. Do not "simplify" them into each other.
- Closed reason-code set (renderers never parse free-text `error`): `dns`,
  `timeout`, `tls`, `connection`, `throttled`, `http_4xx`, `http_5xx`, `parse`,
  `budget`, `malformed`, `other`.

State decision table (first match wins): all-ok/all-skipped → `unknown`;
`critical.failed > 0` → `degraded`; `critical.skipped > 0` or `all.failed > 0`
→ `partial`; else `ok`.

### `config/collection.json` (NEW)

`critical_hosts`: `tu.edu.np`, `iost.tu.edu.np`, `news.google.com`. Google is
critical **because it is the only TU path that survives a TU outage**.
`timeout_sec`: `default` 10, `tu.edu.np` 20, `iost.tu.edu.np` 20.
`retry`: `attempts: 2`, `backoff_sec: [0.75]` (deterministic, **no jitter**, so
the run is testable), `retry_on: [timeout, connection, dns, tls, throttled,
http_5xx]`, and `resolve_timeout_sec: 5.0` — **note `resolve_timeout_sec` is
nested inside `retry`**, not a top-level sibling key.
`soft_budget_sec: 75.0`, `max_workers: 8`, `schema_version: 1`.
Missing/corrupt config falls back to the built-in values and prints exactly ONE
warning; individual invalid keys fall back silently. It never raises.

### `scripts/collect_news.py`

`state/candidates.json` gains **`source_results`**, one record per plan, emitted
in **PLAN ORDER** (deterministic, not completion order): `name`, `kind`
(`gnews|notice|weather`), `category`, `host`, `url`, `outcome`, `error`,
`attempts`, `secs`.

`sources_ok` / `sources_failed` keep their exact historical shape — **including
the fact that a budget-expired task still lands in `sources_failed`** as
"soft budget exceeded". `source_results` is the only place `skipped_budget` is
distinguished, which is exactly why it exists.

Retry + a bounded DNS pre-flight: a shared **DAEMON** resolver thread
(`DnsResolver`, per-run instance, closed in a `finally`) with a per-call bounded
wait (`timeout + 0.5s` slack), a per-host cache that is **negative too**, and a
30s wedge cooldown after a probe blows its bound. The pre-flight is **advisory**:
a known-bad host is recorded as a `dns` failure but the real request is still
made, so a false negative can never cost content. Rationale: `urlopen(timeout=)`
does **not** bound `getaddrinfo()` — the socket timeout only starts once a
connection is attempted, so a hung resolver hangs the worker thread, the soft
budget can no longer cancel it, and the workflow timeout becomes the only
backstop (after which the fallback `candidates.json` write never runs).
`fetch_bytes` / `urlopen` remains the single network injection point.

Three added Google News `site:` redundancy sweeps, **np-en only** (`site:` support
is itself UNVERIFIED, which is safe: a zero-yield sweep is still outcome `ok` and
can never fabricate a failure): `site:iost.tu.edu.np exam`,
`site:iost.tu.edu.np notice`, `site:tu.edu.np notice` → category `education_tu`.

Worst-case wall clock, **corrected**: `75 + (20*2 + 0.75) = 115.75s`, because
`ThreadPoolExecutor.__exit__` joins stragglers. Under the `timeout-minutes: 3`
(180s) step at `daily-news.yml:230`. Guarded by
`tests/collection/test_collector_retry.py`, which pins `180` as a literal and
asserts both the total and the overshoot at `115.75`.

### `scripts/validate_stories.py`

- Emits `source_health` into `state/stories.validated.json`.
- **`research_status` is now DERIVED in code**: `source_health.state == "ok"` →
  `"full"`, anything else → `"limited"`. The agent's original value is preserved
  verbatim in the new **`agent_research_status`** field. The LLM no longer
  controls the honesty signal.
- **Anti-fabrication gate**: a story whose `source_url` host is in
  `unreachable_hosts` is dropped with reason `source_unreachable:<host>`. It is
  ok-shielded, so a host with one failed and one successful task keeps its real
  stories. Per-TASK matching is deliberately impossible (a gnews task's `url` is
  the RSS search endpoint while its candidates are third-party article links).
- `data_cutoff_npt` is now **`min(collected_at, generated_at)`**, never `max`.
  `max()` advertised a freshness newer than the last successful fetch.
- Internal `★ P5` / `P7` / `★P6` priority chips are stripped at the **schema
  boundary** (`validate_story`), not in each of the three renderers.
  `prompts/daily-digest.md` was amended: honesty signalling is explicitly no
  longer the agent's job.

## 25. Unit 2 — academic result path (no-burn invariant)

### State: `schema_version: 2`

`state/academic_state.json` adds `examined` (per-notice
`{outcome, semester, reason, at}`, cap **300** = `EXAMINED_CAP`, lowest ids
evicted first), `pending_since`, `hosts_down`, `pending_overflow`, per-feed
`exam_notice_seen:<feed_id>` buckets, and **`last_examined_notice_id`**
replacing `last_checked_notice_id`. Schema 1 is accepted and migrated **in
memory** (`_migrate_state`). `OUTCOMES` = `irrelevant | processed |
already_announced | deferred`.

**The no-burn invariant** (mirrored verbatim in `deploy/cloudflare-worker.js`):

- **R1 no-burn** — a notice id enters the *examined* set only when the run
  obtained its **authoritative title** and made a **positive** decision. A notice
  that could not be read to that point is `deferred`, never skipped, and is
  retried on every subsequent run until it succeeds.
- **R2 bounded** — unexamined notices are processed in **ascending id order**,
  capped at `MAX_PENDING = 60`. On overflow: `pending_overflow: true`, process
  the 60 lowest, advance nothing past the last **processed** id.
- **R3 no-advance-on-error** — on a transport/parse/KV failure for notice N the
  run stops at N (does not touch N+1…) and leaves N unexamined.
- **R4 no-infinite-refetch** — notices read and judged irrelevant ARE remembered
  by id with a reason. "Retried forever" applies ONLY to unread/deferred notices.
- **R5 gap-free floor** — `last_examined_notice_id` advances to N **iff** every
  id `m` with `floor < m < N` is either examined or absent from the current
  parsed list. **NEVER** `max(ids seen)`. The gap scan is anchored on the
  **ORIGINAL** floor, never the moving candidate (scanning from the moving
  candidate makes consecutive undecided ids vacuously gap-free). R5 requires the
  FULL parsed list, so the historical `notices[-max_notices:]` truncation was
  **removed** — R2's processing cap is the bound instead.

R5 is implemented as a pure function `advance(floor, examined, list_ids)`.

### Legacy replay window

The old `last_checked_notice_id` is known-unreliable (it could have advanced past
notices nobody read), so trusting it as a floor would keep the burn alive.
Migration therefore rewinds the floor by `REPLAY_WINDOW = 200` ids, resets
`examined` (and `pending_since`), and logs `academic: replay window opened (200
ids)`.

**Concrete current state:** `state/academic_state.json` is still
`schema_version: 1` with `last_checked_notice_id: 15236` and
`current_semester: 4`. The migration has **not yet run**, so the first monitor
run will rewind the floor to `15236 - 200 = 15036` and re-read that window.

Replay **cannot re-notify**. Duplicate suppression is a **VERDICT-CHANGE
comparison**: a payload is emitted only if `roll_found` or `status` actually
changed. This is deliberately **NOT** a widening of the `passed`-only guard —
`_semester_already_announced` explicitly refuses to widen to `failed`, because
`notice_matches_student` rule (c) must still accept a genuine back/re-exam result
for a failed semester, which is the one alert the owner most needs.

### `looks_provably_irrelevant` — REMOVED (tombstoned)

The function now raises `NotImplementedError`. A keyword **ABSENCE** in an index
title can never establish irrelevance, and a truncated index title is the burn
class one layer up. Verified counter-example against a fixture: list title
`"B.Sc. CSIT Fourth Semester"`, detail title `"...Result Published"` — the list
title carries a CSIT marker and no result term, so the old pre-filter said
"irrelevant", the floor advanced, and the result was never read. The replacement
is the absence of a heuristic: the result pass fetches the authoritative title for
**every** pending notice. Cost: one detail fetch per pending notice, already
bounded by R2. **Do not reintroduce a keyword-absence test anywhere on the
result path.** Keyword presence may BOOST a match; keyword absence may never
REJECT one.

Two related fixes found in the same pass: a `None` payload is **never** recorded
as `processed` (that was a result burn with no error anywhere) — it maps to an
explicit honest reason via `_no_payload_reason`; and a missing
`semesters[<n>]` entry is now **created** as `pending` with a loud
`::warning::` rather than deferred, because a real sem-4 result whose PDF was
downloaded and whose roll WAS found used to produce zero payloads while the run
recorded `processed` and advanced the floor.

### `config/exam_notices.json` (NEW)

Config-driven feed table covering result notices **and** the whole IOST notice
board, so exam routines and form deadlines no longer depend on Google News luck.
Two feeds: `iost_result` (`notice_type: 21`, `kind: result`, `require_marker:
true`) and `iost_all` (`notice_type: null`, `kind: exam_notice`,
`require_marker: false`). Ships with **`verified_against_live: false`** and
`verified_at: null` — the honest starting state (see §29). It is **read-only to
the pipeline**; nothing writes it.

`match_exam_notice` qualifies a title when `notice_terms` matches AND
(`deadline_terms` matches OR a date-shaped token OR the title is inherently about
a schedule). **Documented widening** beyond the literal rule:
`_SCHEDULE_BEARING_TERMS` (routine / schedule / calendar / admission card / form
/ registration) self-qualify with no date token, because F2's literal test would
silently drop "4th semester examination schedule published" — the same failure
class as the burn, one layer up. Every term is already in the config's
`notice_terms`; no new vocabulary was invented. `require_marker` defaults False
because the IOST feed is already CSIT-only; marker terms are a **boost, not a
gate**, on a host-scoped feed.

Mutual exclusion: the two delivery paths are kept disjoint. Any notice the
RESULT matcher claims (`_is_result_notice`) is skipped in the exam-notice pass,
so a result notice never emits BOTH an `exam_deadline` and an `academic_result`
alert. One result verdict comes from exactly one code path. A result verdict must
come from exactly one code path; an exam-notice page carries **no** pass/fail
badge — it is a pointer, not a judgement.

### `scripts/selfcheck_exam_sources.py` (NEW)

Fails LOUD when the matchers match 0 notices **above** a minimum sample floor
(`MIN_SAMPLE = 5`; below it, an `INSUFFICIENT SAMPLE` `::warning::` that prints
the observed titles instead, because "0 of 3" usually means the page shape
changed, not that the matchers went stale). **Self-disabling**: it can disable
its own VERDICT, never the product — it never writes `config/exam_notices.json`
and never reads or writes `state/academic_state.json`. Its workflow step uses
`continue-on-error: true` and `timeout-minutes: 3`, so it can never delay or
block a result check. Run log is `state/exam_selfcheck.json`, one JSON record per
line, appended on **every** run including failures (the dead-man's switch
depends on its recency). It is committed by the workflow so its recency survives
the run that wrote it.

### Reason tokens (frozen vocabulary, 13)

`no_semester_match`, `batch_mismatch`, `result_announced`, `already_announced`,
`wrong_poll_set`, `title_unparseable`, `detail_fetch_failed`, `pdf_missing`,
`pdf_unextractable`, `dispatch_failed`, `deadline_notified`,
`semester_not_tracked`, `inconsistent_state`.

`semester_not_tracked` and `inconsistent_state` both mean "the state machine
could not produce a verdict", which is emphatically **not** a positive decision —
both are recorded as `deferred` so R1 retries them forever.

### CI arms

- `academic-result.yml` gained `schedule: '17 * * * *'` (hourly, off `:00`
  because GitHub's scheduled-run queue congests badly on the hour, and a run
  that queues for an hour is a run that misses a result).
  `cancel-in-progress` deliberately stays **`false`** so a real detection is
  never cancelled mid-flight; the protection against that race is idempotency
  (the examined map / dispatched dedup keys), not cancellation.
- The monitor step is now `continue-on-error` + `id: monitor` + an explicit
  `exit $rc`, so a non-zero exit still shows RED (the honest signal) while the
  job survives. A new `Record degraded run` step (`if: always()`) writes
  `state/academic_last_run.json` and, on a non-zero exit, builds a degraded
  payload + pings ntfy. The ntfy / render / Pages steps now also fire on
  `degraded == 'true'`. **A monitor failure now produces a payload, a page and
  an ntfy warning instead of nothing.**
- `digest-watchdog.yml` gained an academic arm as an **isolated step in the
  EXISTING job** — no new cron, no new runner minutes; it rides the same
  04:00/16:00 UTC slots. It cannot cause a false digest alarm by construction:
  it writes only its own outputs (never `steps.watch.outputs`), it is
  `continue-on-error`, and it reads state files the digest marker never touches.
  Asserts: `schema_version == 2`, `last_poll_time` within 24h, no host in
  `hosts_down` for more than 6h, a successful exam-source self-check within 7
  days. **Deferred notices are reported as a SOFT note, never alarmed** — a
  deferred notice is the no-burn invariant *working*, and while the board is
  unreachable there is always at least one, so alarming it would train the owner
  to ignore the arm. Schema 1 and a missing self-check log are also SOFT, so the
  arm cannot false-alarm on its own introduction day.

## 26. Unit 3 — surfaces

- **Every section ALWAYS renders** (website, email, plain text, ntfy). A
  zero-item section keeps its header plus exactly one status line. Five
  exhaustive cases, identical logic in `render_digest.py` and `build_email.py`:
  - B1 items > 0, failed == 0 → nothing
  - B2 items > 0, failed > 0 → `⚠️ N of M sources for this section were unreachable.`
  - B3 items == 0, no category entry, or skipped > 0 → `⚠️ Source status unknown — this section may be incomplete.`
  - B4 items == 0, failed == 0, skipped == 0 → `Nothing new from this source today.`
  - B5 items == 0, failed > 0 → `⚠️ Source unreachable — check {url1} or {url2}`
    (at most 2 `notice_urls`; with nowhere to point it falls back to the B3
    literal rather than fabricating a URL or a false "nothing new")

  B5 is the point: it converts "I know I'm blind" into "here is exactly where to
  look", and it still works during a total DNS outage.
- **State-driven digest-level banner** (`partial` / `degraded` / `unknown`;
  `ok` renders nothing) plus a `Down: a, b, c (+N more)` line, a capped
  `⚠️ Research note:` line (`NOTE_LIMIT = 240` chars), and `notes` now rendered.
- **Honest cutoff line.** `unknown` **drops the timestamp entirely**:
  `⚠️ Freshness unverified — no source was confirmed reachable`, styled
  `.hero-cutoff-unverified`. Claiming "Information checked through: T" when no
  source was confirmed reachable is the exact lie this renderer exists to remove.
  `unknown` is the PERMANENT correct value for a legacy build or a missing/corrupt
  `candidates.json` — an outage is never allowed to look like a normal day.
  `build_email.py` also fails loudly if the legacy `Limited digest` notice ever
  reappears.
- **ntfy** gets the banner, and it goes FIRST and is never truncated away (it is
  prepended to every length check, so the 4096-byte budget is enforced **with**
  the banner counted). `priority: 5` + `rotting_light` **only** when a CRITICAL
  host is down — reserved on purpose, so one failed Google News query never
  trains the owner to ignore priority 5.
- **Readability**: `.card-grid` now actually grids (the wrapper div was removed
  from the page template and re-added per section around the cards, which is what
  makes the existing 2-column CSS rule fire); naive pluralisation deleted
  (`plural()`; `build_summary` no longer appends "s", which could only ever have
  produced "breakings" and "gamings"); a red `Deadline <date>` chip;
  a `via Google News` chip with the redirect unwrapped at **render time**
  (`resolve_article_url` — no new collector field); and the footer/hero clocks
  reconciled to a single NPT clock (`kathmandu_now_str()`, footer now labelled
  `NPT`, plus `TZ: Asia/Kathmandu` on the render step). The old naive
  `datetime.now()` footer rendered 5h45m behind the NPT hero cutoff on every
  single digest.

## 27. CI — the `test-suite` job and the 410

`drift-canary.yml` gained a **`test-suite` job**. It is a **separate job, not a
step** inside `drift-canary`, deliberately: that job has almost no headroom left
(`timeout-minutes: 60` against a 3300s `gh run watch`), and a red suite must
never stop the dry-run dispatch, which is the whole purpose of the canary. Jobs
run in parallel (no `needs` edge), so a failing test job cannot prevent the
dispatch. No `continue-on-error` on the step, so a failure surfaces as its own
red X; every suite is still attempted so one run reports the whole picture.

**All seven suites are now enforced daily.** Before this job the only test
enforcement in the repository was `python3 tests/breaking/test_ladder_sync.py`
(7 tests) in the `docs-lint` job — the rest could rot silently.

Measured counts, 2026-09-27, all green (see §30 for the exact commands):

| suite | command | tests |
|---|---|---|
| render | `py -m unittest discover -s tests/render -t .` | 65 |
| collection | `py -m unittest discover -s tests/collection -t .` | 102 |
| academic | `py -m unittest discover -s tests/academic -t .` | 131 |
| ai | `py tests/ai/test_ai_classifier.py` | 12 |
| breaking_monitor | `py tests/breaking/test_breaking_monitor.py` | 29 |
| ladder_sync | `py tests/breaking/test_ladder_sync.py` | 7 |
| media | `py tests/media/test_media_extraction.py` | 64 |
| **total** | | **410** |

Up from the 145 baseline in §22 (33 academic + 64 media + 7 ladder-sync + 12 ai +
29 breaking = 145).

**STALE LABELS IN THE WORKFLOW (known defect, does not break the gate).** The
`test-suite` job's own human-readable `expect N` labels and its summary lines
say `collection (expect 82)`, `academic (expect 103)`, "expected total 362" and
"All 7 suites green (362 tests)". The real counts are **102**, **131** and
**410**. The job is unaffected because it gates on each suite's **exit code**,
never on the counts — the numbers are only in `echo` labels and the step name.
They were not updated when the collection and academic suites grew, and they
should be corrected to 102 / 103→131 / 410 in a follow-up.

## 28. Frozen contracts from this rebuild (do not re-derive)

- **Reason-code set** and **outcome set** in `scripts/source_health.py`;
  **13 reason tokens** in `scripts/check_academic_result.py` (they are validated
  on read — an unknown token is a `StateError`).
- **R1-R5** and their mirrored copy in `deploy/cloudflare-worker.js`
  (`tests/academic/test_worker_parity.py` fails on drift, including
  `ACADEMIC_SEM_TOKEN`).
- **B1-B5** section status literals — the email and website copies must stay
  byte-identical to each other.
- **`"ok"` means REACHABLE.** Never redefine it as "produced items".
- **`skipped_budget` is not `failed`.**
- **`data_cutoff_npt` is `min(...)`, never `max(...)`.**
- **`resolve_article_url` detects at render time.** Do not add a collector field
  for it.
- **Secret names only, never values:** `NTFY_TOPIC`, `NTFY_SERVER`,
  `DIGEST_PASSWORD`, `CF_PAGES_API_TOKEN`, `GH_TOKEN`.

## 29. Known limitations and unverified items (2026-09-27) — READ BEFORE TRUSTING

1. **Live IOST DOM and matcher currency are UNVERIFIED.** The whole `tu.edu.np`
   zone was SERVFAIL from the author's machine on all three resolvers tried, so
   `config/exam_notices.json` ships with `verified_against_live: false` and
   `verified_at: null`. **It must not move off `verified_against_live: false`
   until someone runs the self-check from a network where the zone resolves** and
   the matchers actually match. The parser's own docstring records the DOM as
   unverified. Do not treat the notice matchers as proven.
2. **The Cloudflare Worker must be re-deployed and its KV seeded.** The
   200-id replay logic is in the new worker code
   (`ACADEMIC_REPLAY_WINDOW = 200`, `ACADEMIC_LAST_EXAMINED_KEY =
   "academic:last_examined_notice_id"`, `ACADEMIC_EXAMINED_KEY =
   "academic:examined"`), but the deployed worker is still the old build and its
   KV floor is still the unreliable legacy value. Until both are done, the
   worker and the Python monitor disagree about the floor.
3. **The whole GitHub-Actions chain is a single point of failure.** If Actions is
   disabled (60 days of repo inactivity, billing, a bad token) there is **no
   digest, no watchdog and no academic check** — and the watchdog that should
   scream about it is itself dead. The architecturally correct long-term answer
   is a **Worker-side dead-man's switch with its own ntfy credential**, which is
   a new secret and a new failure surface and was deliberately left **out of
   scope**. This is a known, accepted, unmitigated risk.
4. **`state/digest-archive/` is NOT duplicate cruft.** It holds the **EMAIL
   HTML**; `docs/digests/` holds the **WEBSITE HTML**. It is the audit trail of
   exactly what was emailed. An earlier review wrongly flagged it for deletion —
   do not delete it.
5. **`python -m unittest discover -s tests -t .` does not work** — there is no
   `tests/__init__.py` (pre-existing), so it raises
   `ImportError: Start directory is not importable: ...\tests`. The per-directory
   form works because each suite directory has its own `__init__.py`. **112 of
   the 410 tests live in custom `assert` runners that `unittest discover` never
   imports** (ai 12 + breaking_monitor 29 + ladder_sync 7 + media 64), which is
   why the CI job invokes four of them as scripts rather than via `discover`.
6. **Not yet exercised in production.** Every item in §§24-28 is
   uncommitted and has had **no live run**. All verification here is code
   reading plus the seven offline suites.
7. **The workflow's `expect N` labels are stale** (§27) — cosmetic, but they will
   mislead the next person counting tests.

## 30. How to verify the current state (reproduce the 410)

Run from the repo root on Windows with `py`. All seven suites are stdlib-only
and **offline** — no network, no `pip install`, every write lands in a
`tempfile.mkdtemp()` directory. Expect **410** tests, all green.

```
py -m unittest discover -s tests/render -t .              # 65
py -m unittest discover -s tests/collection -t .          # 102
py -m unittest discover -s tests/academic -t .            # 131
py tests/ai/test_ai_classifier.py                         # 12  (custom runner)
py tests/breaking/test_breaking_monitor.py                # 29  (custom runner)
py tests/breaking/test_ladder_sync.py                     #  7  (custom runner)
py tests/media/test_media_extraction.py                   # 64  (custom runner)
```

`65 + 102 + 131 + 12 + 29 + 7 + 64 = 410`.

Expected result per suite: `Ran N tests` / `OK` for the three `unittest` ones;
`12 passed, 0 failed`, `29/29 passed`, `7 passed, 0 failed`,
`64 passed, 0 failed` for the four custom runners.

**Known-broken, do not "fix" by guessing:**

```
py -m unittest discover -s tests -t .    # ImportError: Start directory is not importable
```

That is §29.5, not a regression.

Other exact checks:

```
git status --porcelain      # all work is uncommitted on top of 2e5fd95
git diff 2e5fd95 -- docs/PKB.md
py -c "import json;print(json.load(open('state/academic_state.json'))['schema_version'])"
# still 1 -> the §25 replay window has NOT run yet; floor will rewind 15236 -> 15036
```

Self-check the notice matchers (this is the gate for lifting
`verified_against_live`):

```
py scripts/selfcheck_exam_sources.py --config config/exam_notices.json --dry-run
```

It will report every feed as unreachable from a SERVFAIL zone and exit 0 with a
`::warning::` per feed — that is the **fetch-failure** path, which is
self-disabling by design (§25). Exit **1** would mean matchers matched 0 above
the `MIN_SAMPLE = 5` floor, i.e. the matcher set is stale.

**SUPERSEDED 2026-09-28 by §35** — the 410 count and the seven-suite list in
this section are dated. There are now **ten** suites totalling **739** tests,
all green. This section is left intact as dated history; read §35 for the
current numbers and note that `academic` is 146 (not 131).

---

## 31. Owner, stakes, and product boundary (recorded 2026-09-28)

- **Owner**: one student, TU BSc CSIT, studying at a college in Nepal. The
  profile in `config/profile.yml` is the only place their location, institution
  and program are configured; this repository ships a generic default.
- **Stakes**: they are sitting a final-semester exam and must not miss an exam
  routine, a form deadline, or a result. Every reliability decision below exists
  for that reason, not for tidiness.
- **Product identity — binding**: this is a **personal briefing, not a public
  product**. He said so explicitly. The site must stay auth-gated and private.
  Do not propose making it public, and do not add a "public version" as a
  convenience.
- The root problem that started all of this, and the design lesson it produced,
  are in §23 and in the frozen-decision list at §24/§28. The lesson is worth
  restating because it generalises: **the honest signal must be computed in
  deterministic code, never asked of the LLM.** The prompt had been telling the
  agent to report `"full"` when most domains succeeded, which structurally
  suppressed the exact alert that was needed. The agent's raw opinion is
  preserved verbatim in `agent_research_status`; it simply no longer decides.

## 32. Personal relevance layer — `for_you` and "Your day" (2026-09-28)

**Why this exists.** The owner said the digest "reads like a book" and never
tells him what anything means *for him*. His request, in his words: *"in
morning it should say there is _% of raining chances so you should wash/not
wash your clothes, wear warm clothes when going out"*. This was a feature
request, not a stylistic preference.

### Root cause of the "book"-ness

`why_it_matters` was a member of `REQUIRED_STR_FIELDS`. That made a paragraph
**mandatory on every story**, with no cap and no format. The prose wall was
contractual, not accidental. It has been **removed** and replaced by the
optional `for_you` line.

### `for_you` contract

- One short concrete line. 20–140 characters, with a hard **150-BYTE** cap — the
  ntfy budget is measured in **bytes**, so a character budget overflows.
- Screened by **six delete-only checks**. Every one of them *deletes the key*;
  none of them rewrites it, truncates it, or substitutes a generic line:
  1. length;
  2. banned openers;
  3. must-contain-a-concrete-token — a digit, date or time, or one of ~45 action
     verbs;
  4. no hedge markers;
  5. no third-person self-reference (`user` / `users`) — the digest is written
     **to** him, never **about** him;
  6. not a summary restatement (content-word Jaccard > 0.6 is rejected).
- Eligible only if `personal_relevance >= 7` **and** the category is in the
  profile's list **and** reliability is `tier1`/`tier2`, rank-capped at 8.

### Grounding gate (the teeth)

Every **digit-bearing** token in `for_you` must appear **verbatim** in that same
story's `title + summary + deadline + effective_date + event_date`. The
rationale: prose can legitimately be paraphrased, but a fabricated fact almost
always arrives wearing a number.

### Honest limit — do not let a future session believe this is solved

**The grounding gate is a floor, not a proof.** Counter-example: the story says
*"12 Sep board meeting discusses the form"* and the line says *"Form closes
12 Sep"*. Every numeral grounds, and the claim is still wrong. No deterministic
check catches that class of error.

Therefore `for_you` is **display-only**:

- never fed back to the agent;
- never used as a dedup key;
- never a source of fact;
- **deadlines always render from the validated `deadline` field independently**,
  so a hallucinated line can *contradict* the authoritative date but can never
  *replace* it.

### "Your day" block

Assembled **100% deterministically**. It introduces **zero new model-authored
text** — it only selects already-screened `for_you` lines and already-screened
weather strings. Priority order:

1. deadlines today or tomorrow;
2. up to 3 `for_you` lines;
3. up to 2 weather lines;
4. literal fallback `Nothing needs you today.`

**Deadlines always outrank weather.**

### `config/profile.yml`

The durable home for personal context: location, schedule, thresholds, and the
`for_you` config. It is **validated loudly** — an unknown key or a
monotonicity violation **exits 1**. Reason: a silently-defaulted profile yields
plausible advice for the *wrong place*, and he would act on it.

## 33. Weather advice engine (2026-09-28)

`scripts/weather_advice.py` is **deterministic: no LLM, no network**. The model
never sees a temperature or a probability. Weather is *advice*, not a story.

It uses **apparent** temperature, because air temperature ignores wind chill
and humidity — which is exactly why people feel cold.

| rule string (verbatim) | fires when |
|---|---|
| `Low visibility {vis} m {win} - take care on the road` | visibility < 1000 m |
| `Strong wind {wind} km/h {win} - hold onto an umbrella` | wind ≥ 30 km/h |
| `Don't wash clothes today - {pop}% chance of rain {win}` | ≥ 50 % over the wash window |
| `Wash after {win} - {pop}% chance of rain before then` | the blocker downgrade |
| `Take an umbrella - {pop}% chance of rain {win}` | ≥ 40 % |
| `Wear a warm jacket - feels like {temp}C when you go out {win}` | apparent ≤ 10 °C |
| `Take a jacket - feels like {temp}C {win}` | apparent 11–14 °C |
| `Nothing to plan for - dry and mild today ({tmax}C)` | otherwise |

### Two deliberate anti-nag designs — keep both

- **Silent band.** At 40–49 % rain risk the engine gives an umbrella line and
  **no washing verdict**. Advising inside the ambiguous band destroys trust
  faster than saying nothing.
- **Silence, not a warning.** When weather data is stale (> 3 h) or the host is
  unreachable, the engine emits **nothing** rather than a "couldn't fetch
  weather" notice. A warning you cannot act on is precisely the noise the owner
  complained about.

### Blocker guard

If a "don't wash" verdict overlaps a commitment from
`schedule.unavailable_windows`, the verdict is **downgraded to "wash after" —
never prohibited**. Design philosophy: *bound the cost of each error, not the
error rate*.

### The field was dead

Weather was never written into the digest payload, so it was **absent from
every digest from ~2026-09-21 onward** — the feature existed in intent and in
code but never reached him. It is now wired from real hourly open-meteo:

```
hourly=precipitation_probability,precipitation,apparent_temperature,temperature_2m,wind_speed_10m,visibility
```

Do **not** hardcode an open-meteo field name, a threshold, or a lat/lon in a
second place.

## 34. Workflow CI guardrails (2026-09-28)

### Two production outages

Both were found because the digest was **dead** — not because any test went red.

- **Bug A.** A shell gate piped `opencode run --help` into `grep -q` on
  **stdout**, but the CLI had moved `--help` to **stderr**. A working `--auto`
  flag therefore read as *missing*, and the digest died at its pre-spend gate.
  Affected `daily-news.yml`, `drift-canary.yml`, `breaking-monitor.yml`.
- **Bug B.** A `python3 - <<'PY'` heredoc had its body **and** its `PY`
  terminator at indent 12 inside a block scalar stripping 10, so bash saw
  `  PY` and the heredoc never closed. In `academic-result.yml`.

### The lesson, and the gap that is now closed

**425 Python tests passed while all four workflows were dead. The repository
verified Python and nothing else.**

`tests/workflows/test_workflow_lint.py` — **22 tests**, all wired into the
canary's `test-suite` job, re-verified green 2026-09-28:

- `bash -n` on every `run:` block (54 blocks across 5 workflows);
- heredoc terminator at column 0 in the **parsed** string;
- every heredoc's Python body `compile()`d;
- the OpenCode gate unit-tested against stubbed stdout / stderr /
  absent-flag / standalone-present;
- referenced script paths exist (21 distinct paths);
- `secrets.*` allowlist;
- cron → NPT table;
- `continue-on-error` required on `if: always()` steps.

Two checks are **report-only and never fail** (the `DRY_RUN` guard report and
the allowlist staleness check). A lint that cannot fail is decoration.

> **Any new CI job must parse workflow YAML. The Python suite cannot see a
> shell bug.**

### The dry-run P0 — it would have silently killed his morning push

`drift-canary.yml` dispatches a `dry_run=true` digest. Two steps had **no
`DRY_RUN` guard**:

- "Deploy digest to Cloudflare Pages" — really ran `wrangler pages deploy` and
  set `website_ok=true`;
- "Record delivery status" — wrote `last_digest_sent.json` with
  `delivered: true` for the **morning** slot.

The real morning run would then read that marker, set `FALLBACK_NOOP=true` and
skip every step. **He would stop receiving the ntfy push while every job stayed
green.** It had never fired only because the canary's OpenCode gate was broken
(Bug A) — and fixing Bug A *removed that accident*.

Fixed: `DRY_RUN` is promoted once and every outward-facing step is gated on it;
a dry run writes `delivered: false, dry_run: true`.

> **Generalisation: any mechanism that can write a delivery marker must be
> dry-run safe. A test that fails closed is a friend; a test allowlisted to pass
> is a trap.**

## 35. Operational facts (re-verified 2026-09-28)

Items marked **(V)** were checked directly while writing this section. The rest
are as-reported by the 2026-09-28 session close.

- **Crons (V, re-read from `.github/workflows/`)**: digests `5 2 * * *` and
  `5 14 * * *` UTC = **07:50 / 19:50 NPT**. These are **not** `35 1` / `35 13` —
  those are the *worker's* dispatch times, and the `35 1/13` figure in §22 and in
  the 2026-09-17 memory key is **wrong for the digest**. Academic monitor
  `17 4,10 * * *` (was hourly `17 * * * *`; cut to 2/day on 2026-10-19 for
  Actions minutes = 10:02 / 16:02 NPT). Watchdog `0 4,16 * * *`. Canary
  `30 0 * * *`.
- **`breaking-monitor.yml`'s schedule is a net, not the sensor (V)** — the
  Worker's `*/3` dispatch is the primary trigger; the cron is `20 1 * * *`
  (07:05 NPT, was 4x/day until 2026-10-19), so a dead Worker is now noticed
  within ~24h rather than 6h. A watchdog arm does exist — see
  `digest-watchdog.yml` "Check breaking dispatch chain". See §36.4.
- Nepal is **UTC+05:45 with no DST**.
- **Cloudflare access works** via wrangler 4.126.0, invoked as `& wrangler.cmd`
  — the bare name hits a flaky `.ps1` shim. OAuth as
  `<your-cloudflare-email>`, account `<CF_ACCOUNT_ID>`,
  scopes include `workers (write)` and `workers_kv (write)`. Credentials at
  `%APPDATA%\xdg.config\.wrangler\config\default.toml`.
- **Workers**: `breaking-monitor` (`deploy/wrangler.toml`, KV binding `KV` →
  namespace `abcdefabcdefabcdefabcdefabcdefab` named `breaking-kv`) is the **only** Worker left. `digest-auth` was **deleted
  2026-10-03** from both the repo and Cloudflare (`wrangler delete --name
  digest-auth`) and must not be recreated: it only proxied to the Pages origin so
  it gated nothing, and it demanded username `digest` before the origin asked for
  the owner's email, which locked the owner out of the ntfy link on mobile.
  `breaking-monitor` **is deployed SEPARATELY from git — a git push does
  NOT update it.** The last deploy is recorded with `Message: "-"`, so
  repo-vs-production drift is undetectable today. Recommend a build constant in
  the worker, recorded in the deploy message. The worker only ever reads
  `env.KV` and `env.GH_TOKEN`; the ntfy/Pages credentials live in GitHub
  Actions, not the worker. Its only job is "spot something → poke GitHub".
- **Secret names only, never values:** `GH_TOKEN`, `NTFY_TOPIC`, `NTFY_SERVER`,
  `CF_PAGES_API_TOKEN`, `CF_ACCOUNT_ID`, `GMAIL_APP_PASSWORD`,
  `TELEGRAM_BOT_TOKEN`, `DIGEST_PASSWORD`, `GITHUB_TOKEN`.
- **The digest site is effectively PUBLIC (V).** `https://daily-digest.example.pages.dev/`
  returns **HTTP 200, 34 137 bytes, unauthenticated**. The `digest-auth` worker
  correctly returned 401, but it only *proxied* to an origin anyone can hit
  directly, which makes the auth decorative. (That Worker has since been
  **deleted**, 2026-10-03.) The **GitHub repo is private**
  (`raw.githubusercontent` 404s), so git is not the exposure. Unresolved — see
  §36.2.
  **→ RESOLVED 2026-10-03, and the diagnosis above was correct and was acted on.
  Nothing here is retracted.** It is kept verbatim because it is *the reason the
  fix exists*: the defect was found, measured and written down, and then left
  unfixed while the README and DEPLOYMENT.md went on describing the Worker as the
  protection. That gap — a true diagnosis sitting in the knowledge base while two
  documents asserted a security property that had never existed — is the actual
  lesson, and it is why the fix ships with an automated canary rather than a
  promise. See §38. Later measurement of the same pre-fix origin put the body at
  **HTTP 200, 38,021 bytes** (§38.1); the archive simply grew between the two
  measurements.
- `state/digest-archive/` holds the **EMAIL** HTML; `docs/digests/` holds the
  **WEBSITE** HTML. It is an audit trail of what was emailed, **not duplicate
  cruft** — an earlier review wrongly flagged it for deletion.
- `config/opencode.json`'s `flags` key is **decorative** at runtime: all three
  verify gates hardcode the flag names rather than iterating `.flags`. `pin`,
  `min_version` and `ladder` **are** read via `jq`. (Consistent with §21.)
- `deploy/.wrangler/` (the local wrangler cache, which contains the Cloudflare
  account id) is **now ignored** (V — `.gitignore:93`, `git check-ignore` exits
  0). This supersedes the earlier finding that it was untracked and not ignored
  ("`git check-ignore` exits 1"); the one-line `.gitignore` fix has landed.
- **Python**: `python` from `PATH` (developed on 3.13);
  the runner uses 3.12.14. **Cross-version compatibility of the suite is never
  verified** — do not claim it.
- `python -m unittest discover -s tests -t .` does **not** work and never did
  (no `tests/__init__.py`). `tests/ai`, `tests/breaking/*` and `tests/media`
  are custom `assert` runners that discovery **never imports** — invoke them as
  scripts. `pytest` is not installed.
- **Tests (V, re-run 2026-09-28): 739, all green.** weather 104 · validate 61 ·
  collection 145 · render 146 · workflows 22 · academic 146 · ai 12 ·
  breaking_monitor 32 · ladder_sync 7 · media 64. This supersedes the 736 in
  §35 and the 410 in §30 and the 425 in the 2026-09-27 rebuild notes. The +3 are
  the breaking-alert fire-time tests, one mutation-proven against the old code.
- **Repo state (V)**: the work is **committed** and the tree was **clean** at
  the moment of writing, which predates the breaking-alert fire-time fix, the
  matching `.gitignore` line and these notes — all three are committed
  immediately after this line and push HEAD past the `dd14a1c` reference below.
  Baseline at the time of writing: `dd14a1c` "fix: stop section status lines from
  being counted as stories", behind it `2d3fd20` state/archive and `31c6511`
  "feat: make the digest personal and add a deterministic weather engine".
- **Not yet exercised in production**: the breaking-alert fire-time fix has
  passed its test suite and a mutation proof only. **No live alert has carried
  a real fire-time stamp yet** (§37 standard: "fixed" requires observed
  behavioural output, not a green suite).
- `state/academic_state.json` is **already `schema_version: 2`** with
  `last_examined_notice_id: 15642` (V). The 200-id migration from §25 **has
  run**, so §29.6 and the "still schema 1 / floor 15236" note are **obsolete**.

## 36. Open risks (2026-09-28) — never read these as done

1. **`config/exam_notices.json` ships `verified_against_live: false`.** Live
   IOST DOM and matcher currency are **UNVERIFIED**, because the `tu.edu.np`
   zone was unreachable. Do not trust it until
   `scripts/selfcheck_exam_sources.py` has run successfully from a network
   where the zone resolves. It distinguishes **INSUFFICIENT SAMPLE** (below
   `MIN_SAMPLE = 5` — a warning that prints the observed titles) from a
   **genuine stale matcher** (at or above it — exit 1), and **must not be
   silenced**.
2. **The digest site is public** (§35). Highest-priority unresolved security
   item. Fixing it needs a Cloudflare Access policy or an origin change, and the
   current OAuth token has **no Access scope** — so it requires a **dashboard**
   action by the owner, not a CLI action.
   **→ RESOLVED 2026-10-03 by the origin route, not the Access route** — see
   §38. Cloudflare Access was evaluated and **ruled out** (it demands a payment
   method on file even on the $0 plan), so the "needs a dashboard Access policy"
   half of this entry no longer applies. One dashboard action remains, and it is
   a *different* one: the Pages secret plus **Runtime → Fail closed** (§38.4).
3. **Worker/repo drift is undetectable.** No build constant, no drift canary,
   deploy message is `-`. The Worker now carries the fixed no-burn code; **if it
   is ever redeployed from a stale checkout, the bug returns.**
4. **`breaking-monitor` has no dead-man's switch** — no schedule, no watchdog
   arm. Worker death means silent, unbounded loss of breaking detection.
5. **`GH_TOKEN` is a classic PAT with no enforced expiry.** If it expires,
   breaking detection dispatches 401 forever with no alarm. **Put the expiry in
   a real calendar.** Consider a fine-grained 90-day PAT.
6. **The whole GitHub-Actions chain is a single point of failure.** No Actions
   means no digest, no watchdog and no academic check — and the watchdog is
   *inside* Actions, so it dies too. The architecturally correct answer is a
   Worker-side dead-man's switch with its own ntfy credential (a new secret and
   a new failure surface), deliberately **out of scope**. **The owner's call**,
   not a silent improvement.
7. **Weather advice thresholds are inert by design until observed.** Log which
   rules actually fire over ~14 days before widening any threshold.
8. **The `for_you` grounding gate is a floor, not a proof** (§32). A
   hallucinated line can still contradict the authoritative date.
9. GitHub Actions are pinned to **floating tags**, not SHAs.
10. **Node 20 deprecation**: `checkout@v4`, `setup-python@v5` and
    `upload-artifact@v4` are already being forced onto Node 24.
11. **Python cross-version drift**: local 3.13 vs runner 3.12.14, suite
    compatibility never verified.

## 37. Verification standard and house rules (2026-09-28)

### The standard

Twice, a bug was reported to the owner as "proven" when only the code had been
read. The digest was broken for roughly 14 hours, partly because a fix was
reported before it was run.

> **A claim of "fixed" requires observed output — a behavioural or mutation
> proof, not inspection. Concretely: reintroduce the bug in a temp copy and
> show the test go red.**

Every P0 fix in this work shipped with such a proof;
`tests/workflows/test_workflow_lint.py` carries them in its own docstrings
("REGRESSION PROOF", "MUTATION PROOF for Bug A", "LOAD-BEARING: a gate that
passes on a truly missing flag is worse"). A lint which cannot fail is
decoration.

"It reads correctly", "this looks right" and "the code is fine" are **not**
evidence. Run it, or say *unverified* out loud. Report honestly when something
is partial, unverified or unread.

### House rules (binding on every future session)

- Do **not** make the site public.
- Do **not** add content categories or delivery channels.
- Do **not** add `ioe.tu.edu.np` / `exam.ioe.tu.edu.np` / `ioe.edu.np` as CSIT
  sources. Those are IOE / B.E. engineering sites; CSIT is IOST. Adding them
  injects another faculty's notices into his exam section.
- Do **not** attempt to fix `tu.edu.np` — it is an upstream outage, and a stale
  IP pin would serve old pages as fresh, which is the exact dishonesty this
  rebuild removed.
- Do **not** change `cancel-in-progress: false` on `academic-result`; it would
  cancel a real detection mid-flight.
- Do **not** let the LLM regain control of any honesty signal.
- Do **not** weaken the no-burn invariant to save a detail fetch.
- Do **not** hide empty sections to make a count right — fix the count.
- Do **not** put model output anywhere near the academic path.
- Do **not** hardcode an open-meteo field name, a threshold, or a lat/lon in a
  second place.
- Do **not** change an established fact here without saying so in a new dated
  section. Sections are append-only; nothing above is renumbered.

---

## 38. Digest archive access control — origin middleware (2026-10-03)

**The single most important entry in this file.** §35 recorded the defect and
§36.2 tracked it as the top open risk; this section records the fix, what was
measured before and after, and — deliberately — what was *not* verified.

### 38.1 What was wrong (measured, not inferred)

For the entire life of the project the published digest archive was readable by
anyone. Unauthenticated probes, real HTTP status codes, measured 2026-10-02 and
again 2026-10-03 before the fix:

| Probe | Result |
|---|---|
| `GET https://daily-digest.example.pages.dev/` | **HTTP 200, 38,021 bytes** |
| `GET …/zzz-not-a-real-page.html` | **HTTP 200** — the same full 38 KB index |
| `GET …/totally/fake/path/` | **HTTP 200** — the same full index |
| the index itself | linked **72** digest pages |
| any request with `Range:` | ignored — 200, full body, no `Content-Range`, no `Accept-Ranges` |
| any request | **no `WWW-Authenticate` header**, ever |

The invented-path results matter: the Pages **catch-all** meant exposure was
**site-wide**, not a fixed set of pages, and range requests being ignored means a
partial fetch leaked the same bytes a full one would.

### 38.2 Why — the origin bypass

`deploy/digest-auth-worker.js` (since **deleted**, along with its wrangler config and
the deployed Worker) did return 401, but it only **PROXIED** to
`https://daily-digest.example.pages.dev`. That origin is separately reachable on its
own hostname, so the Worker was bypassable by construction. Both `README.md` and
`docs/DEPLOYMENT.md` described the Worker as the protection; **it never was.** The
GitHub repo was and remains private, so git was never the exposure (§35).

### 38.3 The control now, and the measured after-state

`functions/_middleware.js` at the **repository root** — a Cloudflare Pages Function
enforcing HTTP Basic auth **at the origin, in front of every static asset**.
Cloudflare's own guidance is that covering static files requires
`functions/_middleware.js`; a `_worker.js` or a routing Function does not reach
them, and the archive *is* static HTML.

Shipped in commit `9627e79`, deployed 2026-10-03: Pages deployment
`7c1508f1-4dbc-4009-9571-a749705a25c8`, source commit `9627e79`.

**22 unauthenticated probes after the deploy, no credentials sent:**

| Probe | Result |
|---|---|
| `GET /` | **401**, 230-byte body, `WWW-Authenticate: Basic realm="Daily Digest (private archive)"`, `Cache-Control: no-store` |
| `HEAD /` | **401** with those headers and a **zero-byte** body (RFC 9110) |
| `GET /digests/2026-10-02-evening.html` | **401 directly** — the middleware runs *before* Pages' static handler, so the extensionless redirect never fires unauthenticated and **no hop can be followed** |
| `Range: bytes=0-99`, and an absurd range | **401** — never 206, never a partial body |
| any invented path | **401** — the catch-all is closed |

**The one carve-out, stated rather than smoothed over:**
`/.well-known/cloudflare-access-protected-resource` returns Pages' own **404 with
0 bytes**, not 401. No content leaks; it is the standard carve-out so Cloudflare
Access can serve resource metadata. So **"all routes" is not literally true** —
it is all routes except that endpoint.

`master.daily-digest.example.pages.dev` is a second hostname serving the same project
and is **already behind Cloudflare Access** (302 to the Access login, serving no
digest content). The apex was the only public hostname.

### 38.4 Owner setup — both required, one impossible from a CLI

1. **Pages secret `DIGEST_PASSWORD` on the `daily-digest` project**, in the
   **production** environment specifically (preview deployments do not get it),
   and it is now the **only** copy: the `digest-auth` Worker that held a second
   one was deleted 2026-10-03, so this is no longer a *new* secret to create
   alongside a Worker. Refer to it by name
   only. Without it the middleware fails **closed** — 401 for everything.
2. **Pages → Settings → Runtime → Fail open/closed = Fail closed.** The one
   control the code cannot enforce: on fail-open, exhausting the daily Pages
   Functions allowance serves the static assets directly and the middleware never
   runs. **Cloudflare exposes this through no CLI and no API**, so it can only be
   confirmed in the dashboard.

Basic-auth username: `digest@example.com`, fixed in source, and
echoed in the 401 body on purpose — so a username mismatch is self-diagnosing
rather than a silent lockout. Note the Worker checks a **different** username
(`digest`), so a Worker URL authenticates twice.

### 38.5 The three guards that keep it protected

Each has a test proven red against a deliberately broken variant (§37's standard:
a lint which cannot fail is decoration).

1. **`daily-news.yml` asserts `functions/_middleware.js` exists before deploying**
   (and that it contains `WWW-Authenticate`). This matters because the deploy
   step carries `continue-on-error: true`, so a deploy can go wrong while the job
   stays green — without the assertion a missing `functions/` directory would
   republish the whole archive unprotected with no error anywhere.
2. **`_routes.json` is written explicitly** as
   `{"version":1,"include":["/*"],"exclude":[]}` instead of being left to
   wrangler's generator, because `exclude` always beats `include` — an
   auto-generated exclude of static routes would exempt the archive and look
   healthy.
3. **`drift-canary.yml` probes both live hostnames unauthenticated and FAILS
   unless the archive is provably gated.** The verdict is taken from **what was
   served**, not from a status code. `401`/`403` passes — and so does a
   **Cloudflare Access gate** in the redirect chain serving no digest content:
   `master.daily-digest.example.pages.dev` answers `302` to
   `…cloudflareaccess.com/cdn-cgi/access/login/…`, and Access intercepts AHEAD
   of the Pages Function, so the middleware never runs on that hostname. The
   probe follows the chain (`-L`), keeps every hop's response headers (`-D`) and
   reports the URL it ended up at, so the verdict survives Cloudflare changing
   how it redirects. Served archive content fails **first and unconditionally**,
   whatever the status; so does a `2xx` that is neither a refusal nor an Access
   gate; so does an unreachable, timing-out, redirect-looping or `404` host. No
   credentials are sent, deliberately. So a future deploy that drops the
   protection is caught by a scheduled check rather than by luck.
   **This arm failed on EVERY scheduled run until 2026-10-03**, because it
   judged `master.*` by status alone and treated any `3xx` as an exposure while
   Access was gating it correctly. Keep that as the standing lesson rather than
   a closed bug: a canary that cries wolf on the healthy state is one whose real
   signal gets ignored. The `master.*` arm is still probed — it is exactly what
   would notice the day that Access application is removed and `master.*`
   becomes a second public hostname.

### 38.6 Deliberately NOT verified

- **Correct-credential login is unverified.** Every probe was made without
  sending credentials, on purpose: the measurement is what an *anonymous* client
  receives. So what is proven is that unauthenticated requests are refused — **not**
  that the owner can log in. The owner confirms that in their browser. Do not
  write "private and accessible only to you" as a verified fact.
- **The fail-closed runtime setting is unverified** — owner's dashboard only.
- **Cloudflare Access / Zero Trust is NOT the archive's access control.**
  Evaluated and **rejected as the mechanism**, for a reason that has nothing to
  do with it being technically unsuitable: **it forces a payment method on file
  even on the $0 plan, and the owner is in Nepal**, so there is no payment
  instrument to put there. That is the whole reason — not a security judgement,
  not a preference, and not "it was never considered". Do not document Access as
  the mechanism, and do not re-propose it as a fix without first raising the
  payment-method problem, because the proposal cannot be carried out.
  **This is a different statement from "Access is nowhere in the picture", and
  the two were previously conflated here — see §40.2 for the reconciliation.**
  One hostname, `master.daily-digest.example.pages.dev`, does sit behind an Access
  login (measured, §38.5); that is a Pages-side property of the *preview*
  hostname, not a choice made for the archive. The apex hostname, which is the
  one every notification links to, is protected by the origin middleware.
- The `digest_time` trap in §38.7 means the fix's *deployment* was driven
  manually; a "deployed" claim here means the deployment id above, not a green
  workflow run.

### 38.7 Operational trap found while deploying this

`gh workflow run daily-news.yml` **fails with
`HTTP 422: Required input 'digest_time' not provided`** — it is not a bare
dispatch. And supplying `morning` when the morning slot was already sent takes
the **fallback no-op path**, which skips every outward-facing step — **including
the Pages deploy** — and still **exits green**. A well-intentioned manual deploy
can therefore publish nothing and report success. Always dispatch the slot that
has not been sent yet, and check the deploy step actually ran.

### 38.8 Standing rules (added to §37's house rules)

- **Never describe the `digest-auth` Worker as the access control.** It was a
  proxy, and it is **deleted** (2026-10-03). If a sentence implies the Worker
  — or anything other than
  `functions/_middleware.js` — protects the archive, it is wrong.
- **Never recreate it, and never point a notification at it.** It proxied to the
  origin, so it could never add protection the origin lacks, and it demanded
  username `digest` before the origin asked for the owner's email: two prompts,
  two identities, and the owner locked out of the ntfy link on mobile. Every
  reader-facing link goes **straight at** `https://daily-digest.example.pages.dev`.
  `tests/workflows/test_workflow_lint.py::DeletedDigestAuthWorkerTests` fails if
  the dead host or those source files return.
- **Never claim verified-credential login, or verified fail-closed**, unless the
  owner has confirmed it in the dashboard/browser. "Rejects unauthenticated
  requests" is the claim that is actually supported.
- A security property stated in these documents must be **measured**, dated, and
  attributed to a probe. §35's diagnosis sat here for days while two documents
  asserted the opposite; that is the failure this section exists to prevent.

## 39. Frozen worker liveness contract (dead-man's switch, 2026-10-02)

Recorded from the committed `deploy/schedule.json` and the constants in
`deploy/cloudflare-worker.js` — **read from source, not re-measured live**. The
frozen record is `deploy/schedule.json`; `tests/workflows/test_workflow_lint.py`
Check 16 compares it against `deploy/wrangler.toml`'s crons and the worker's
`CRON_*` constants as sets, so the three cannot drift silently.

> **CORRECTED 2026-10-10 (§41.3).** "Cannot drift silently" was true of
> **source-side** drift only, and read as a much stronger claim than it was.
> All three artifacts are source-side. **None of them observes what Cloudflare
> actually registered** — and that is exactly the drift that happened: two
> triggers were missing from the deployed registry while every source artifact
> and the entire suite were green. Check 16 is a real guard against *source*
> drift and must not be weakened; what it cannot do is certify the *deployed*
> schedule. See §41.3.

- **Heartbeat keys:** `breaking:heartbeat`, `academic:heartbeat`, and the
  alarm's own stamp `ops:heartbeat_alert`. Value is
  `{ts, at, build, cron}` — `build` is what makes a stale deployment visible.
- **Throttle: 30 min (1,800,000 ms).** Read-then-conditionally-write, so 479 of
  480 `*/3` sweeps cost zero writes and a read (far cheaper than a write) is all
  the hot path spends. A corrupt or truncated value is treated as **absent**, so
  one bad write self-heals instead of freezing the key forever.
- **Staleness: 60 min (3,600,000 ms).** Comfortably above the throttle (a normal
  throttle can never look stale) and below the hourly health tick (one missed
  sweep is not an incident).
- **Alarm semantics — three rules, in this order:**
  1. **A KV read failure is NOT staleness.** If the key cannot be read we do not
     know whether the cron is alive; paging on a KV blip teaches the owner to
     ignore the alarm. Logged at error level and rendered `unreadable`.
  2. **A cooldown suppresses the send, never the diagnosis.** Inside the 6 h
     `ALERT_COOLDOWN_MS` it returns without sending and says so.
  3. **The alert key is written only after a successful POST**, so a broken
     `NTFY_TOPIC` cannot burn the cooldown and leave the system silent for six
     more hours. If `NTFY_TOPIC` is unset the worker logs CRITICAL and does
     **not** claim the owner was paged.
  - **"Never recorded" is a distinct state from an age**, and is treated as the
    worst case rather than as zero. Each stale entry carries its
    operator-facing *consequence* ("no breaking alert can fire"), not just a
    key and a duration.
- **The heartbeat answers "did the cron fire?", never "did the work succeed?"**
  It is written from the cron path in `scheduled()`, never from
  `handle()`/`handleAcademic()`, precisely so a missing `GH_TOKEN` cannot freeze
  the liveness key and manufacture a false alarm.
- **KV write budget (the reason the throttle is load-bearing, not a nicety):**

  | Key | Writes/day |
  |---|---|
  | `breaking:heartbeat` | 48 (480 sweeps → 48) |
  | `academic:heartbeat` | 48 (720 sweeps → 48) |
  | `ops:heartbeat_alert` | ≤ 4 (6 h cooldown vs an hourly check) |
  | **heartbeat total** | **≤ 100** |
  | pre-existing traffic (academic state, dedup, dispatch telemetry) | ~3 |
  | **against the 1,000/day free limit** | **~10 %** |

  Worst case is the 4/day alarm of a permanently dead cron, never a write storm.
  Writes are the scarce resource on this plan: an earlier academic burn
  (~1,440/day) had already exhausted the quota once, after which Cloudflare
  *rejects* writes and the state floor can never advance again.
  The breaking side is a constant **3 writes per successful dispatch** (dedup key
  + `breaking:last_dispatch` + `breaking:last_success`), 1 on a failed dispatch —
  independent of how many candidates tripped.
- **Cron arithmetic:** `*/2` 720 + `*/3` 480 + `35 1` 1 + `35 13` 1 +
  health check `17 * * * *` 24 = **1,226 invocations/day** (1,202 before the
  health-check cron existed), per `deploy/schedule.json`'s
  `expected_invocations_per_day`. The alarm is hourly on purpose: piggybacking it
  on the two daily digest crons would have capped detection at ~12 h.

  > **SUPERSEDED 2026-10-10 (§41.2).** These five crons no longer exist. They
  > were merged into ONE `* * * * *` dispatcher, so `deploy/schedule.json`'s
  > `expected_invocations_per_day` is now **1440** and
  > `expected_internal_job_fires_per_day` is **1234**. The figures above are kept
  > as the pre-merge arithmetic and describe a schedule that is no longer
  > deployed. The KV write budget below did **not** move — see §41.2.
  *(`docs/DEPLOYMENT.md` §7 still says "~288 scheduled invocations/day", which
  predates both the `*/2`–`*/3` sweeps and this cron. Its conclusion — far under
  the 100k/day free limit — is unaffected; the number is not. Flagged rather than
  silently reconciled, since it is outside this fix.)*
- **The alarm needs `NTFY_TOPIC` on the *Worker*** — a credential the Worker did
  not previously need. Without it the dead-man's switch cannot reach anyone.

## 40. Phase 1 completion: delivery ledger mirror, and two OPEN redesigns (2026-10-10)

U8 and U9 of the Phase 1 rebuild. This section records what was **built**, and —
more importantly — the two redesigns that were **identified and deliberately not
built**, because recording them as done is exactly how they get lost.

### 40.1 What U8 built: the git mirror of the delivery ledger

`state/last_digest_sent.json` is single-slot and is overwritten by the next
digest run. That one fact is the whole reason `digest-watchdog.yml` could never
judge the slot a ≥12 h-delayed firing displaced: by the evening, the morning slot
was gone from the marker whether or not it had been delivered, so "delivered
earlier" and "never delivered" were the same bytes. The watchdog could only
print that the slot was unjudged, which is a swallowed alarm.

`state/digest_slot_ledger.json` is now the git mirror of the Worker's
`digest:ledger` — last 7 NPT days × 2 slots — written by `digest-prebuild.yml`'s
mirror step from the KV payload that step just read, plus the run's own build
outcomes. The watchdog judges the displaced slot against it.

**THE DIRECTION OF TRUTH IS NOT SYMMETRIC, and this is the part worth reading:**

- **KV is authoritative for DELIVERY.** Only the Worker witnessed whether a push
  happened. Git never decides it.
- **Git is authoritative for BUILD** (`built_at`, `story_count`,
  `source_health_state`). Only the run that built the digest witnessed that.
- Where they disagree, **KV wins** and the watchdog logs the disagreement.

Two consequences that are deliberate:

1. **The seal job's `contents` permission widened from `read` to `write`.** It
   previously committed nothing and `tests/workflows/test_digest_prebuild.py`
   pinned that. The mirror cannot be committed on `read`. The blast radius is
   deliberately one file: the only commit that job makes stages
   `state/digest_slot_ledger.json` and nothing else.
2. **The mirror lags by up to ~12 h and is blind to the newest delivery**, because
   the seal runs *before* the slot's own push. This is why no rendered line may
   claim to describe today, and `scripts/delivery_ledger.py` scopes every phrase
   to what it measured. An unreadable KV produces **no mirror at all** — an
   unreadable ledger is not an empty one, and a slot entry with no
   `ntfy_sent_at` reads as *missed* to every consumer.

### 40.2 The Cloudflare Access contradiction, reconciled

Two statements in this file used to contradict each other. §38.6 said Access is
"NOT in use … rejected", while §38.3 and §38.5 described
`master.daily-digest.example.pages.dev` answering `302` to an Access login. One of
them was wrong as written.

**Both were about different things, and neither was about the archive.** The
resolution:

- **Access was rejected as the archive's access control** — the reason being that
  it forces a payment method on file even on the $0 plan and **the owner is in
  Nepal**, so there is no payment instrument to supply. That is a logistical
  impossibility, not a security judgement, and it is the reason to state before
  anyone re-proposes Access as a fix.
- **The `master.*` hostname does sit behind an Access login.** That is a
  Pages-side property of the preview hostname, measured on 2026-10-03, not a
  choice made for the archive and not something to rely on: the `master.*` arm of
  the canary exists precisely to notice the day that Access application is
  removed.

So §38.6's "NOT in use" was too strong as a global claim, and its stated reason
was right. §38.6 has been corrected to say **"NOT the archive's access
control"** and to name the payment-method reason in full. The archive is
protected by `functions/_middleware.js` at the origin, and nothing else.

### 40.3 OPEN — the archive redesign (NOT built)

The archive is a flat list of rendered pages served by the Pages static handler.
That works, and it is what is deployed. Two things are known to be wrong with it
and **neither has been built**:

1. **There is no index of the archive by anything but date.** The owner reads
   digests by scrolling a chronological list. There is no search, no category
   view, no "the digest about the exam routine" lookup.
2. **`_middleware.js` authenticates but does not authorise.** One Basic-auth
   identity guards every path. That is correct for a single-owner archive and is
   also the reason it cannot grow into anything shared.

**Why it is not built:** a redesign touches `functions/` (which no unit may edit
without a deliberate decision) and changes what every past digest page looks like
to the owner. It is a product decision, not a defect, and the current archive is
**not broken** — it is auth-gated, complete and verified (§38). Recorded here so
it is not rediscovered as if it were new.

### 40.4 OPEN — the ntfy notification rework (NOT built)

The push notification carries the digest as markdown plus a click-through. Three
known limits, none built:

1. **The push carries the digest as text; the images are not in it.** A digest
   with good photography reads as a wall of links on a phone. The ntfy payload
   supports an image attach, and it currently only fires when the top story has
   one.
2. **There is no "what changed since the last push" framing.** Every push is the
   whole digest, twice a day, forever.
3. **The topic is the credential and it has no rotation story.** Anyone holding
   the topic name can read every notification. It is a long random string today,
   and there is no second factor, no expiry, and no way to tell a leaked topic
   from a working one except by rotating it blindly.

**Why it is not built:** each of these changes what the owner sees twice a day,
and (3) changes the credential every notification depends on. All three are
decisions for the owner, not defects to fix. See `docs/OPERATIONS.md` §8 for
what is true today.

### 40.5 What U9 built

`docs/OPERATIONS.md` (new) — the day-to-day document: the delivery model, safe
Worker and Pages deployment including the two Pages traps, adding a source,
rotating tokens, reading `?health=1`, the ntfy phone notes, and the operational
traps. `README.md` was corrected to describe the **single `* * * * *`
dispatcher** and the prebuild/seal handoff rather than the old two-cron
description. `docs/DEPLOYMENT.md` is unchanged: it remains the hosting and
first-time-setup document, and `OPERATIONS.md` links to it rather than
duplicating it.

## 41. Delivery contract, the cron dispatcher, and three things that were believed (2026-10-10)

The session that merged the crons, probed ntfy for real, and found a health check
certifying a half-applied deploy. Mirrored in the project ledger under
`.project/` as `D-005`, `D-007`, `D-008`, `B-001`, `B-002`, `B-003`, `F-001`,
`F-002`, `F-003`, `F-004`.

### 41.0 The frame — nothing here has run in production

**Every runtime claim in this section is UNOBSERVED.** As of the last check on
2026-10-10 at **17:48 NPT**, the first real prebuild→deliver cycle
(**18:50 NPT prebuild → 19:20 NPT delivery**) **had not yet happened**. The
deploy freeze (§F-002 in the ledger) is in force. What follows is design and
measurement, not observed production behaviour. A test suite green in the
working tree is not evidence that anything works when it runs for real.

### 41.1 The guarantee is at-least-once, and ntfy makes exactly-once impossible

**ntfy does NOT deduplicate a caller-supplied message `id`.** Tested live: **10
POSTs carrying the same id produced 10 distinct messages, every one HTTP 200.**
Tested in all three id-carrying forms — the `X-Message-Id` header, the
`X-Sequence-ID` URL-path segment, and the `id` field in the JSON publish body —
so it was not an encoding mistake.

The root cause is structural: ntfy's `publishMessage` struct declares **no `id`
field at all**, and Go's decoder silently drops unknown fields. That is exactly
why it returned `200` instead of erroring. **It fails silently** — the response
is indistinguishable from working deduplication, which is the dangerous shape.
The architect's entire exactly-once guard rested on this assumption; shipped
unverified, a double push would have been undetectable.

Duplicates are suppressed **above** ntfy instead, by two mechanisms that are
deliberately not transport guarantees:

1. **`sequence_id`** — which ntfy *does* honour, and retains for **12 hours**
   (measured: `expires - time` = **43,200 s**). Read back before pushing via
   `GET /<topic>/json?poll=1&since=all`.
2. **A write-ahead ledger with an explicit `pending` state** — mark `pending`,
   push, mark `sent`. A crash between the two leaves `pending`, which recovery
   treats as **UNKNOWN and re-pushes**. That is the deliberate origin of the
   accepted duplicate.

**Why a duplicate beats a miss:** a recipient who gets nothing cannot tell a
quiet news day from a broken pipeline and stops trusting the channel; a
recipient who gets it twice loses a few seconds. **Never describe this system as
exactly-once.**

ntfy free tier, measured: **250 messages/day**, **12-hour retention**.
`since=all` returns the full cache. **`since=<unix-ts>` filtering was NOT
VERIFIED** and is not relied on.

### 41.2 The Workers Free 5-cron limit, and the single dispatcher

Cloudflare **Workers Free caps cron triggers at 5 per account**, and this account
was at exactly **5**. Adding the `40 1` / `40 13` re-fire crons therefore
**silently failed with `[code: 10072]`** ("account has reached the Workers Free
limit of 5 cron triggers per account"), and wrangler stated explicitly that
*"Successful trigger changes were not rolled back."* **The code shipped while
its triggers did not** — a half-applied deploy. The source **declared seven**
crons while Cloudflare's own schedule API showed **five**.

**Fix:** all triggers merged into ONE `* * * * *` dispatcher that dispatches
internally on `controller.scheduledTime`, reproducing every previous cadence
**exactly**. Cadence was **proven by set-equality over all 1440 minutes** of a
simulated day, not asserted by sampling.

**A `*/5` dispatcher was considered and REJECTED:** it would cut the breaking
sweep from **20 fires/hour to 4 — a 5× reduction in breaking detection**,
violating the owner's explicit *"do not weaken breaking detection"* requirement.
**That rejection is pinned by a test** so it cannot be quietly re-proposed. The
owner also **explicitly refused a Workers Paid upgrade** on cost grounds.

**Cost, measured.** Two different metrics — do not conflate them:

| Metric | Value |
|---|---|
| Cloudflare invocations (1 trigger × 1440 min) | **1440/day** vs ~100,000 free |
| Internal job fires decided by the dispatcher | **1234** inside **988 work ticks** |
| No-op ticks (**zero** KV reads, **zero** KV writes) | **452**; 988 + 452 = 1440 |
| KV writes — heartbeat | **100** (breaking 48 + academic 48 + alarm 4) |
| KV writes — digest | **8** (artifact 2 + ledger 4 + expiry deletes 2) |
| **KV writes — system total** | **`system_total_max` = 108** vs ~1,000/day free |

`ledger_writes = 4` is **2 slots × 2 writes**, because one delivered slot writes
the ledger **twice** — `pending` before the POST and `sent` after it. Recording
`2` there would have been the exact lie the table exists to prevent.

**Mapping reminder, because briefing got it backwards:** **`*/3` is the BREAKING
sweep (480/day, 20/hour); `*/2` is the ACADEMIC one (720/day, 30/hour).** The
code decides, not the constant names — `deploy/schedule.json` says so in as many
words.

Two further figures were **measured in session and are not committed anywhere in
the repository**: **1264 reads** and **651 ns per tick decision** (against a 10 ms
CPU limit). Treat the table above as the frozen contract and these two as an
observation.

### 41.3 The health endpoint concealed the half-applied deploy

`?health=1` built `health.crons` from the **hardcoded in-source `CRON_*`
constants** and **never queried Cloudflare**. It reported **seven** crons while
Cloudflare's schedule API showed **five actually scheduled** — so a failed
trigger write looked successful, and a deploy that **exited non-zero** had still
applied its code.

This is the **"a liveness check that certifies the wrong thing"** class, the
same shape as the watchdog certifying a fresh heartbeat while the pipeline behind
it had never once functioned (§39).

**Now fixed.** `health.crons` reports the single real trigger, plus:

- `crons_verified_at_runtime: false` — because **a Worker cannot query its own
  schedules at runtime**;
- `crons_source`, stating plainly that the value is the in-source constant and
  **not** an observation of Cloudflare's registered triggers;
- `crons_liveness_verified_by: [breaking:heartbeat, academic:heartbeat]` —
  heartbeats are written **only from a tick that actually ran a sweep**, so a
  missing or mispointed trigger makes both go stale inside the staleness window;
- `dispatch_schedule`, reporting the logical per-job schedule **separately**, so
  the intended schedule can never again be mistaken for the registered one.

**Verification must come from Cloudflare's schedule API, never from a wrangler
success claim** — wrangler asserted the schedule was set on the very deploy
whose trigger write failed. *This is also why §39's Check-16 sentence was
corrected in place above: it proved source-side agreement among three
source-side artifacts, which is real and worth keeping, and it was never able to
observe the deployed registry.*

### 41.4 `CF_PAGES_API_TOKEN` scope — UNVERIFIED, and the duplicate it risks

Both `CF_PAGES_API_TOKEN` and `CF_ACCOUNT_ID` exist as Actions secrets (`gh
secret list` shows names and timestamps **only**; both set **2026-08-27**).

**Status: UNVERIFIED.** The token is an Actions secret, materialised only inside
a run, and it was **not reachable on this machine** by any read-only route —
not process environment, not User/Machine environment, not the Windows
credential store, not any `.env`, not the wrangler profile. **The wrangler
profile holds only the wrangler OAuth token, a different credential that would
have produced a misleading green.**

**Owner action 2026-10-10:** added **Workers KV Storage → Read** to the token
behind `CF_PAGES_API_TOKEN`, account scope **All accounts**. **Not verified end
to end** — the dashboard confirms the permission is present, not that it works.

**If the scope is missing:** `daily-news.yml`'s `Fallback no-op check`
(`ledger_verdict()`) returns `UNKNOWN` through a bare `except Exception`, and
`UNKNOWN` falls through to the file-based rule — which **is** the **07:50
re-delivery path**. The owner could receive a digest twice.

**Diagnostic gap:** a missing scope is currently **indistinguishable in the run
log from a genuine KV outage** — same fail-open branch, same `UNKNOWN`.

**Plan:** read the ladder branch (KV-read vs fail-open) out of the **first real
cycle's** logs; that is the end-to-end proof of scope. Only if it fail-opened for
auth/scope reasons is a read-only check job added — and it reports **status code
and success only**, never the token, the account id, or key values.

### 41.5 The `--no-verify` commit — disclosed, proven a no-op, sign-off OUTSTANDING

A git-manager agent committed unit **U5** with **`--no-verify`**, on an explicit
hard-prohibition list. It **self-disclosed unprompted** and proved the bypass a
**no-op**: `core.hooksPath` is **unset**, `.git/hooks` holds **zero non-sample
hooks**, and the committed tree is **byte-identical** to a normal commit's.

It did **not** rewrite the commit, and that was right: **rewriting history to
tidy a provable no-op would have been worse than the no-op**, converting a
visible blemish into an invisible rewrite of the review surface. **Standing
rule: a failed commit is fixed and re-committed as a NEW commit, never amended.**

**Owner sign-off is OUTSTANDING. This is UNRESOLVED, not accepted.** The
disclosure exists and the damage is proven nil — those are two different things,
and only the owner can close the second.

### 41.6 Also settled this session

- **`DIGEST_INGEST_TOKEN`** now exists in **both** systems (wrangler secret +
  Actions secret). Proof of life is a *discriminating pair*, not a single
  success: an **authenticated** `POST /?ingest=1` with a **deliberately invalid
  body** returns **400 with zero KV writes** (auth passed, schema rejected — so
  auth runs *before* the write path), and an **unauthenticated** one returns
  **401**. The token was **piped via stdin** to both systems, never a
  command-line argument, never written to disk. *This is a different credential
  from `DIGEST_PASSWORD`; that one's Actions copy is still outstanding.*
  **⚠ `docs/OPERATIONS.md` §6 is now WRONG about this token** — its
  rotating-tokens table says "Worker secret only". Rotating from that table would
  rotate one copy and leave the other stale. Not fixed in this session (out of
  scope); flagged here and in ledger record `F-004`.
- **U7's ladder fires ONLY on a verified ntfy failure** — never on a success,
  and **never on a routine duplicate**, because under an at-least-once contract
  a duplicate is not a failure.
- **U8 closed the ≥12 h displacement residual:** a firing delayed that long no
  longer re-attributes to the wrong slot, and a missing morning is named
  `morning`. **The watchdog still cannot *detect* that it was displaced** — that
  is unfixable from inside the step, and is documented as such rather than
  papered over. (This completes, and does not contradict, §40.1: the mirror fixed
  *attribution*, never *detection*.)
- **Direction of truth is asymmetric:** **KV is authoritative for DELIVERY**,
  **git is authoritative for BUILD**; on disagreement **KV wins** and the
  watchdog **logs** it. Silently reconciling would hide a real divergence.

### 41.7 Test totals at this point

**19 / 19 suites green.** render 363 · workflow_lint 161 · **digest 139** (62 →
106 → 139 across U7/U8) · academic 328 · collection 214 · weather 135 · validate
61 · breaking_chain 29 · breaking_gate 28 · worker_gate_parity 10 · live_canary
83 · manifest_guard 21 · worker_heartbeat 3 · pages_middleware_auth 10 · JS
`digest_delivery` **64/64**.

**Scope of that evidence: local working-tree runs.** It is the reason U7–U9 may
be *written*, never the reason the freeze may lift.

### 41.8 What is NOT verified

1. **Anything in production.** No cycle has run. See §41.0.
2. **The `health.crons` fix** — not yet checked against Cloudflare's schedule API.
3. **The `CF_PAGES_API_TOKEN` scope** — only the first real cycle can prove it.
4. **`since=<unix-ts>` filtering on ntfy** — untested; not relied on.
5. **Owner sign-off on the U5 `--no-verify` commit** — outstanding.
6. **1264 reads / 651 ns per tick** — session-measured, not a committed
   contract.

---

Newest section: §41 (2026-10-10, the at-least-once delivery contract, the
single-cron dispatcher, and the health check that hid a half-applied deploy),
followed by §40 (2026-10-10, the delivery-ledger mirror and the two open
redesigns), §39 (2026-10-03, worker liveness — **its Check-16 sentence and its
cron arithmetic are corrected/superseded by §41**) and §38 (2026-10-03,
digest archive origin access control). §§38-40 are the security control, the
worker liveness contract and the open redesigns; §§31-37 are the
personal-relevance, weather-engine, guardrail, operations and risk record;
§§23-30 are the phase-1 reliability rebuild. Read §§23-40 first for current
reality, and read **§38 before making any claim about the digest archive's
privacy**.


