/**
 * deploy/cloudflare-worker.js
 * Breaking-news tripwire for daily-media-news on Cloudflare Workers (FREE tier).
 *
 * Runs on FIVE cron schedules and branches on controller.cron. The schedule
 * itself is NOT defined here — it is defined in deploy/wrangler.toml
 * `[triggers] crons`, recorded in deploy/schedule.json, and applied by
 * `wrangler deploy`. Check 16 in tests/workflows/test_workflow_lint.py fails if
 * those three ever disagree, which is why no interval is restated in prose
 * anywhere in this repository:
 *   - *\/3 * * * *   : breaking sweep — cheap deterministic detection (regex
 *                      over Google News RSS + direct publisher RSS feeds) and,
 *                      when a candidate trips, ONE repository_dispatch event
 *                      ("breaking-alert") at looping-forever/daily-media-news. That
 *                      dispatch triggers the EXISTING
 *                      .github/workflows/breaking-monitor.yml, which performs
 *                      the full AI classification / verification / dedup /
 *                      alert pipeline — safety logic is NOT duplicated here.
 *   - *\/2 * * * *   : academic sweep — cheap TRIPWIRE over the TU IOST
 *                      Result notice feed (notice_type=21). When a NEW notice
 *                      matches a CSIT semester result, ONE repository_dispatch
 *                      event ("academic-result") fires at
 *                      looping-forever/daily-media-news. That dispatch triggers
 *                      .github/workflows/academic-result.yml, which runs
 *                      scripts/check_academic_result.py (PDF download + roll
 *                      search + ntfy/website delivery). NO PDF/OCR happens in
 *                      the worker — it only detects the notice and hands off.
 *   - 35 1 * * *     : morning digest trigger — POSTs a workflow_dispatch to
 *                      daily-news.yml (digest_time=morning).
 *   - 35 13 * * *    : evening digest trigger — POSTs a workflow_dispatch to
 *                      daily-news.yml (digest_time=evening).
 *   - 17 * * * *     : heartbeat health check — reads both liveness keys and
 *                      pages the owner when a sweep cron has gone silent. It is
 *                      the Worker-side arm of the dead-man's switch, and the
 *                      reason the switch exists at all: this Worker writes NO
 *                      KV state on a quiet sweep, so nothing else could tell a
 *                      healthy-and-quiet system from a dead cron. The reverse arm
 *                      (GitHub Actions polling ?health=1) is in
 *                      digest-watchdog.yml, because a Worker-side switch cannot
 *                      detect its own total death.
 * The GitHub-side schedule in daily-news.yml ('5 2,14 * * *') is only a
 * DEAD-MAN'S SWITCH fallback that fires ~30 min later and no-ops when the
 * worker path already delivered the digest.
 *
 * TWO ARMS, NOT ONE, ON PURPOSE. A Worker-side switch cannot notice that the
 * deployment was deleted or the account suspended: no cron fires, so nothing can
 * scream. The Actions arm in digest-watchdog.yml polls ?health=1 from outside and
 * covers exactly that case. Neither arm subsumes the other.
 *
 * ============================================================================
 * ONE-TIME SETUP — click by click in the Cloudflare dashboard (no card needed)
 * ============================================================================
 *
 * A. CREATE THE ACCOUNT (free, no payment method)
 *    1. Open https://dash.cloudflare.com/sign-up
 *    2. Sign up with an email + password. Choose the Free plan if asked.
 *       No credit/debit card is ever requested.
 *
 * B. CREATE THE WORKER
 *    3. From the dashboard home, left sidebar -> "Compute (Workers)" ->
 *       "Workers & Pages".
 *    4. Click "Create application" -> "Create Worker".
 *    5. Name: breaking-monitor  ->  click "Deploy" (deploys starter code)
 *       -> click "Edit code".
 *
 * C. PASTE THIS CODE
 *    6. In the online editor: select-all, delete the starter code, paste the
 *       ENTIRE contents of this file, then click "Deploy".
 *
 * D. ADD THE GH_TOKEN SECRET
 *    7. Create the token on GitHub first:
 *       https://github.com/settings/personal-access-tokens/new
 *         - Token name: cloudflare-breaking-dispatch
 *         - Expiration: your choice (calendar the renewal!)
 *         - Repository access: "Only select repositories" ->
 *           looping-forever/daily-media-news
 *         - Permissions -> Repository permissions:
 *             Contents : Read-and-write      <- required by the dispatches API
 *             Actions  : Read-and-write      <- required by the workflow
 *                                               dispatches API (digest trigger)
 *             (Metadata : Read-only is selected automatically)
 *         - Generate token, copy the value (shown once).
 *    8. Back in Cloudflare: your worker -> "Settings" -> "Variables and
 *       Secrets" -> "Add variable":
 *         - Type: Secret
 *         - Variable name: GH_TOKEN        (exactly this name)
 *         - Value: paste the PAT
 *       -> Save, then "Deploy".
 *
 * E. BIND THE KV NAMESPACE (dedup store)
 *    9. Left sidebar -> "Storage & Databases" -> "KV" -> "Create namespace":
 *       name it e.g. breaking-dedup  ->  Add.
 *   10. Your worker -> "Settings" -> "Bindings" -> "Add" -> "KV namespace":
 *         - Variable name: KV              (exactly this name — code reads env.KV)
 *         - KV namespace:  breaking-dedup
 *       -> Deploy.
 *
 * F. SCHEDULE IT
 *   11. Crons are NOT added by hand in the dashboard: they are applied by the
 *       Wrangler CLI from the `[triggers] crons` list in deploy/wrangler.toml,
 *       which is committed and reviewable, and mirrored in deploy/schedule.json.
 *       Deploy with `wrangler deploy -c deploy/wrangler.toml` and the dashboard
 *       picks them up. The list is ONE cron:
 *           * * * * *          the dispatcher, every minute
 *       Cloudflare Workers Free allows 5 cron triggers per account and this
 *       account was at the limit, so the jobs are merged into a single
 *       dispatcher that branches on the minute. It reproduces the previous
 *       schedule exactly (see SCHED_* below): the breaking sweep still runs on
 *       every third minute, the academic sweep on every second, the health
 *       check at :17 past the hour, and the digest at 01:35 / 13:35 UTC.
 *       A coarser grid (every 5 minutes) could NOT do this: *\/3 sweeps would
 *       drop by more than half, which is weakening breaking detection.
 *       The digest jobs POST a workflow_dispatch to daily-news.yml ~30 min
 *       before the GitHub-side dead-man's-switch schedule ('5 2,14 * * *')
 *       fires as fallback.
 *
 * G. TEST WITH THE FETCH HANDLER URL
 *   12. Find your workers.dev URL: worker -> "Settings" -> "Domains & Routes".
 *   13. Status ping: open  https://breaking-monitor.<your-subdomain>.workers.dev/
 *       -> plain-text status showing whether KV binding and GH_TOKEN are wired.
 *   14. Force one full detection pass right now (don't wait for cron):
 *       open  https://breaking-monitor.<your-subdomain>.workers.dev/?run=1
 *       -> returns "detection pass executed"; watch details under worker ->
 *       "Logs" (-> "Begin log stream") while the request runs.
 *   15. End-to-end check: when detection trips, the Actions tab of
 *       looping-forever/daily-media-news shows a "Breaking News Monitor" run whose
 *       trigger reads repository_dispatch (breaking-alert).
 *
 * FREE-TIER HEADROOM (why this fits the free plan):
 *   - Workers Free: 100k requests/day; each invocation here is network I/O
 *     only (regex parsing, no heavy CPU). The exact daily invocation count is
 *     NOT restated here on purpose: deploy/schedule.json holds
 *     `expected_invocations_per_day`, derived from its own cron list, and Check
 *     16 in tests/workflows/test_workflow_lint.py recomputes it and fails on a
 *     disagreement. A number in a comment is the drift this file keeps
 *     eliminating.
 *   - KV Free: ~100k READS/day against 1,000 WRITES/day. WRITES are the scarce
 *     resource, so every state write in this file is CONDITIONAL (read-compare
 *     -write) and the academic sweep — the 720x/day hot path — writes nothing
 *     at all on a sweep where neither the floor nor the examined map changed.
 *     See the write-budget block in handleAcademic() for the arithmetic.
 *
 *   BREAKING-SIDE WRITE BUDGET (the other hot path, 480 sweeps/day). Writes
 *   here are bounded by REAL ALERTS, never by the cron, because every write in
 *   handle() sits inside the dispatch branch:
 *     steady state (nothing breaking)   0 writes/sweep   =     0/day
 *     one dispatched item               1 dedup key
 *                                       + breaking:last_dispatch
 *                                       + breaking:last_success   = 3
 *     a FAILED dispatch                 breaking:last_dispatch only = 1
 *     so per DAY: exactly 3 x (successful dispatches) + 1 x (failed attempts)
 *
 *     Note what this is NOT: 3 x tripped. The old code dispatched tripped[0]
 *     and then wrote a dedup key for EVERY tripped item, so a sweep with 7
 *     tripped candidates spent 7 writes to alert 1 of them — the worst of both
 *     worlds, since the extra 6 keys both burned quota AND suppressed those 6
 *     stories for 6h. The per-sweep cost is now a CONSTANT 3 regardless of how
 *     many candidates tripped, and strictly lower than before in the common
 *     case. Exhausting 1,000 writes/day would need 334 real dispatches in a
 *     single day; the academic path's measured ~3/day is untouched by this.
 *
 *   HEARTBEAT WRITE BUDGET (the dead-man's switch, added 2026-10-02). This is
 *   the one path whose cost is bounded by the CLOCK rather than by alerts, so
 *   it is the one that could have quietly reintroduced a quota burn. It cannot,
 *   because HEARTBEAT_THROTTLE_MS makes the cost per unit time, not per sweep:
 *     breaking:heartbeat   1 write per 30 min =  48/day  (480 sweeps -> 48)
 *     academic:heartbeat   1 write per 30 min =  48/day  (720 sweeps -> 48)
 *     ops:heartbeat_alert  1 write per alert   =  <= 4/day (6h cooldown vs an
 *                                                 hourly check)
 *     ------------------------------------------------------------------
 *     new total                                     <= 100/day
 *     plus the ~3/day the system already spends (academic state + dedup +
 *     breaking dispatch telemetry)                 =  ~103/day
 *   Against 1,000 writes/day that is ~10% of the free tier, and the worst case
 *   (a permanently dead cron) is the 4/day alarm, not a write storm. The reason
 *   this matters: writes are the scarce resource on this plan, and the original
 *   academic burn (~1,440/day) had already exhausted the quota once — after
 *   which Cloudflare REJECTS writes and the floor could never advance again.
 * ============================================================================
 */

// THE NP SWEEP, ONE QUERY PER FEED. This list is BREAKING_NP_QUERIES from
// scripts/breaking_monitor.py:549 and MUST stay identical to it — the Python arm
// already polls one feed per term, and this list is the Worker catching up.
//
// WHY IT WAS ONE OR-MERGED QUERY AND WHY THAT WAS A DEFECT. Until 2026-10-13
// every local term shared a single search, `Nepal OR Example City OR Example District OR ...`,
// and Google returns AT MOST 100 items for a search regardless of how many
// clauses it has. Measured live at the same instant the Python arm found 8
// in-window candidates from its 14 feeds, this one merged query returned 100
// items of which the NEWEST WAS 113 MINUTES OLD: the 100-item cap was spent on
// whatever matched the loosest term, and the fresh flood stories were pushed off
// the end of the list before the 30-minute window ever saw them. The feed was
// being fetched every 3 minutes and contributing zero candidates, which is
// indistinguishable from "nothing is happening" — the exact wrong conclusion on
// a life-safety path.
//
// One query per term gives each term its own 100-item budget, so a fresh story
// about Example City cannot be buried by 99 items about "Nepal". The cost is more
// feeds per sweep; the measured parse cost is in the BREAKING-SIDE write-budget
// comment at the top of this file, and the sweep summary now reports per-feed
// fresh counts so a feed that goes dry is visible instead of silent.
const BREAKING_NP_QUERIES = [
  "Nepal",
  "local district",
  "Tribhuvan University",
  "bank notice",
  "Nepal electricity NEA",
];

const WORLD_SWEEP_QUERY = "major world news";

// Google News RSS search URL. encodeURIComponent is used rather than the old
// hand-written `+`/`%22` splicing so a query containing a space or a quote can
// never produce a malformed URL.
const SOURCES = BREAKING_NP_QUERIES.map((query) => ({
  name: "np-en:" + query,
  url:
    "https://news.google.com/rss/search?q=" + encodeURIComponent(query) +
    "&hl=en-US&gl=NP&ceid=NP:en",
})).concat([
  {
    name: "world",
    url:
      "https://news.google.com/rss/search?q=" +
      encodeURIComponent(WORLD_SWEEP_QUERY) +
      "&hl=en-US&gl=US&ceid=US:en",
  },
]);

// Direct publisher RSS feeds (detection_source "rss"), merged into the same
// candidate pool as the Google News feeds BEFORE the emergency gate. Every
// entry below was verified live (HTTP 200 + valid <item> XML) before being
// added; feeds that failed live verification are recorded here so nobody
// re-adds them blindly. Live re-verification table 2026-08-26
// (status / content-type / <item> count):
//   www.onlinekhabar.com/feed        -> 200 application/rss+xml, 55 items (KEPT)
//   kathmandupost.com/rss            -> 200, body is valid RSS XML with 40
//                                       items (server mislabels the response
//                                       Content-Type as text/html — harmless,
//                                       parsing is body-based)
//   www.nepalnews.com/rss             -> 200, valid RSS XML with items (KEPT)
//   english.onlinekhabar.com/feed    -> 200 application/rss+xml, 20 items
//                                       (valid alternate; www variant kept)
//   thehimalayantimes.com/rss/feed   -> 403 Forbidden (bot-blocked; earlier
//                                       checks also saw an HTML page, and
//                                       /feed + /rss.xml were 404)
//   himalayatimes.com/rss/feed       -> timeout (dead)
//   nepalitimes.com/rss              -> 404
//   en.setopati.com/rss              -> 404
//   en.setopati.com/feed             -> 500 (reconfirmed; page head declares
//                                       atom+xml but the endpoint never serves)
// Individual feed failures are tolerated (Promise.allSettled), so a dead
// entry only costs one timed-out fetch per sweep.
const RSS_SOURCES = [
  {
    name: "onlinekhabar",
    url: "https://www.onlinekhabar.com/feed",
  },
  {
    name: "kathmandupost",
    url: "https://kathmandupost.com/rss",
  },
  {
    name: "nepalnews",
    url: "https://www.nepalnews.com/rss",
  },
];

// All feeds polled each sweep, tagged with a detection_source that is
// propagated through repository_dispatch client_payload into
// breaking_monitor.py (which stores it on the active_stories event and
// renders a "Detected via:" line on the alert).
const ALL_FEEDS = [
  ...SOURCES.map((s) => ({ ...s, detection_source: "google-news" })),
  ...RSS_SOURCES.map((s) => ({ ...s, detection_source: "rss" })),
];

const FETCH_TIMEOUT_MS = 8000; // per-feed hard timeout
const WINDOW_MS = 30 * 60 * 1000; // keep items published within last 30 min
const DEDUP_TTL_SECONDS = 21600; // 6h re-alert suppression window

// --- THE BREAKING GATE (mirror of scripts/breaking_monitor.py) -------------
//
// THE SINGLE SOURCE OF TRUTH IS THE PYTHON SIDE. These patterns are a port of
// breaking_monitor.py's EMERGENCY_RE / BREAKING_IMPACT_RE /
// BREAKING_IMPACT_WIDE_RE / NOT_A_LIVE_EVENT_RE / HARD_LIVE_EVIDENCE_RE /
// HARD_LIVE_DISRUPTION_RE / UNIVERSAL_IMPORTANCE_RE and their numeric
// thresholds. Generation at Worker runtime is impossible (a Worker has no
// filesystem and is deployed separately from this checkout), so the port is
// hand-written and DRIFT IS MECHANICALLY DETECTED INSTEAD:
//
//   tests/breaking/test_breaking_chain.py
//     * an EXECUTED check that runs BOTH gates over the 616-row frozen corpus
//       and compares every decision AND every audit reason row by row, and
//     * a guard that the gate is not merely equal to Python but measurably
//       WIDER than the old 13-phrase version, so a parity test cannot pass by
//       both sides being narrowed together.
//
// (tests/breaking/test_worker_gate_parity.py covers the related but SEPARATE
// question of the gate BLOB: title-only vs title+snippet.)
//
// That is the same mitigation this file already carries for the academic regex
// (ACADEMIC_SEM_TOKEN / tests/academic/test_worker_parity.py). It is not
// optional here, and it is not a formality: the gate below used to run
// EMERGENCY_RE ALONE while the comment below it claimed it mirrored
// importance_gate. That claim was false, the Worker's hit count was 33 of the
// corpus against Python's 378, and because the Worker runs FIRST it was the
// binding constraint -- so every one of those 345 headlines was discarded here
// and never reached the classifier. `importanceGate()` is the whole fix.
const EMERGENCY_RE =
  /\b(?:war|invasion|nuclear (?:test|accident|plant)|earthquake|magnitude 6\.[5-9]|magnitude [7-9]|flood warning|landslide warning|national emergency|Example Highway (?:closed|blocked)|Example Bank (?:fraud|security|cyber)|bank of nepal.*cyber|major outage)\b/i;

// Tier 2 (coverage). Kept verbatim and UNIONED with EMERGENCY_RE, never
// substituted for it. See breaking_monitor.py:214 for the contract.
const BREAKING_IMPACT_RE =
  /\b(?:landslide|avalanche|subsidence|erosion|killed|kills|died|dead|death|injured|missing|rescued|survivors|trapped|buried|outage|shutdown|blackout|cut\s*off|blocked|disrupt(?:ed|ion)|cyber\s*attack|hack(?:ed|ing)?|breach|ransomware|phishing|leak(?:ed|s)?|crash(?:ed)?|collision|derail(?:ed)?|sank|sunk|burst|collapse[ds]?|explosion|blast|fire|wildfire|blaze|arson|strike|bandh|protest|curfew|emergency|evacuat(?:e|ed|ion)|rescue|police\s+(?:killed|arrest)|epidemic|outbreak|pandemic|cholera|measles|magnitude\s+\d)\b/i;

// Tier 3 (the measured widening): plural/inflected forms, road and service
// closure, structural damage, live response. See breaking_monitor.py:232.
const BREAKING_IMPACT_WIDE_RE =
  /\b(?:flood(?:s|ing|ed)?|landslides?|avalanches?|mudslides?|landslip|explosions?|fires?|crashes?|collisions?|outages?|shutdowns?|protests?|strikes?|evacuations?|emergencies|blockades?|casualties|injuries|fatalities|death\s+toll|death\s+count|bodies|casualt(?:y|ies)|road(?:s|way)?\s+(?:is\s+|are\s+)?(?:blocked|closed|washed|cut|submerged)|highways?\s+(?:is\s+|are\s+)?(?:blocked|closed|cut)|(?:closed|blocked|cut\s+off|washed\s+away|sealed)\s+(?:off|for|at|from|on)|traffic\s+(?:halted|suspended|stopped|disrupted)|power\s+(?:cut|outage|failure)|load\s+shedding|(?:telecom|internet|network|pipeline|gas|water|electricity)\s+(?:disrupt\w*|down|outage|cut)|swept\s+away|washed\s+away|toppled|collapsed?|destro(?:y|ys|ied)|land\s+slide|debris|landslides?\s+hit|rescued?|rescuers?|search\s+(?:under\s+way|for|operation)|land\s+search|relief\s+operation|evacuat\w*|day\s+of\s+mourning|condolence|explosive|arrest(?:ed|s)?|apprehend\w*)\b/i;

// NOT-A-LIVE-EVENT framing veto (suppress-only, pass 3). Framing markers, NOT a
// topic list: it says "this is journalism ABOUT the event", never "this event is
// unimportant". See breaking_monitor.py:272 for the clause-by-clause rationale.
const NOT_A_LIVE_EVENT_RE =
  /\b(?:explainer|explained|commentary|editorial|op-?ed|analysis|infographic|retrospective|anniversary|here'?s\s+what|what\s+we\s+know|what\s+caused|what\s+triggered|the\s+odds\s+of|study\s+finds|scientists?\s+say|experts?\s+(?:say|warn)|why\s+(?:is|are|do|does|did|has|have)|how\s+(?:to|why|and)|who\s+(?:is|are|was|were|really)|first\s+study|breakdown|factbox|fact\s+box|climate\s+(?:justice|change|change'?s|change’s|crisis|connect\w*|link)|glacial\s+retreat|hydropower\s+(?:risk|fragility)|wider\s+climate|carbon\s+brief|greenhouse|loss-and-damage|emissions|scientists?\b|glacier\s+break|cargo\s+drones|deliver\w*\s+(?:relief|aid|supplies)|turns\s+to\s+drones|drone\b|vows?\s+to|ambivalence|hesitan\w+|how\s+two\s+men|survived\s+nine\s+days|recalls\s+ordeal|heartbreak|come\s+to\s+terms|struggles|harbinger|what'?s\s+to\s+come|routed|rerouted|alternative\s+routes|air\s+travel|the\s+health\s+system|mental\s+health|aiding|sympathy|condolence|study|survey\s+finds|world\s+fears)\b|\b(?:one|a|1)\s+month\s+(?:since|after|later|on|of)|weeks?\s+after|months?\s+after|days?\s+after\s+(?:the\s+)?(?:flood|landslide|avalanche|disaster|quake|earthquake|storm|tragedy|flash)|\d+\s+(?:days?|weeks?|months?|years?)\s+(?:after|since|on)\b|one\s+year\s+on|still\s+a\s+lockdown|\b(?:video|photo\s*gallery|photos?|pics|week\s+in\s+pictures|in\s+pics|watch|licensable\s+picture|news\s+wrap|scenes\s+from|footage\s+shows?|watch:)\b|\b(?:charity\s+appeal|donat(?:e|es|ed|ion)|relief\s+fund|responds?\s+to|response\s+continues|humanitarian\s+response|operational\s+(?:update|response)|aid\s+(?:update|mission)|assessment\s+ordered|damage\s+assessment|compensation|restores?\s+(?:power|electricity)|reopens?|reopened|pension|celebrat\w*|offers?\s+(?:a|an|the)\s+(?:lifeline|relief)|answer\w*\s+to|debate)\b/i;

// HARD live evidence, class 1: a person is dead, missing, trapped or carried
// out. Nothing in the framing veto may outrank this. See breaking_monitor.py:339.
const HARD_LIVE_EVIDENCE_RE =
  /\b(?:kill(?:ed|s|ing)?|die[sd]?|dead|death\s+toll|deaths?\b|fatalit\w+|casualt\w+|bod(?:y|ies)|injur(?:ed|ies)|missing|unaccountable|uncontactable|trapped|stranded|buried|rescued|rescuers?|survivors?|swept\s+away|washed\s+away|toppled)\b/i;

// HARD live evidence, class 2: something is cut off RIGHT NOW. "Repairs may take
// a month" is not a retraction of a road that is blocked today. See
// breaking_monitor.py:374.
const HARD_LIVE_DISRUPTION_RE =
  /\b(?:block(?:s|ed|ing|age)?|clos(?:ed|es|ing|ure)|shut(?:\s*down)?|cut\s+off|severed|evacuat\w+|impassable|washed\s+away|swept\s+away)\b/i;

// Tier 4, consulted only when tiers 1-3 are all silent. The mass-casualty count
// threshold is enforced in universalHits(), not here. See breaking_monitor.py:441.
const UNIVERSAL_IMPORTANCE_RE =
  /(?:\bmagnitude\s+(?:[7-9]|[1-9]\d)\b|(?:earthquake|tsunami|cyclone|hurricane|typhoon)[\s,]+(?:kills?|dead|devastat\w*|destruct\w*|magnitude)|volcan(?:ic|o)\s+(?:eruption|active|explode)|flood\s+(?:kills?|dead|devastat\w*|warning\s+issued)|\b(?:outbreak|pandemic|epidemic)\b|\b(?:war|invasion|coup|peace\s+deal|ceasefire|sanctions?)\b|(?:power\s+(?:grid|station)|internet\s+(?:outage|shutdown|offage)|water\s+(?:crisis|shortage|contamination))|(?:new\s+law|ban\s+imposed|nationwide\s+ban|nationwide\s+restriction|major\s+policy\s+change|new\s+sanctions?\b)|(?:mass\s+casualt\w*|mass\s+kill\w*|terrorist\s+attack|bombing\s+(?:kills?|dead)))/i;
const UNIVERSAL_MIN_DEATH_COUNT = 10;

// A bare section label with no event in it ("Nepal Landslide - Dayton Daily
// News"). STRUCTURAL, not vocabulary: title minus a trailing "- Publisher"
// suffix must carry MIN_INFO_WORDS content words. See breaking_monitor.py:325.
const BARE_LABEL_SUFFIX_RE = /\s+-\s+[^-]{2,40}$/;
const MIN_INFO_WORDS = 4;

// Pure-sport/gossip suppression: >=2 vocabulary hits means the
// emergency-looking word is incidental ("war drama box office score").
const SPORT_GOSSIP_RE = /\b(match|score|goal|celebrity|box office)\b/gi;
const SPORT_GOSSIP_SUPPRESS_HITS = 2;

// THE REPOSITORY EVERY DISPATCH, PROBE AND CONTENTS READ IN THIS FILE TARGETS.
// The URL literals below stay literal because operators and tests read them, so
// this constant is not the source of the URLs -- it is the one place `?health=1`
// can STATE the identity. tests/digest/worker_token_expiry.test.js fails if any
// repo URL stops containing this slug, which is what stops the two drifting, and
// that is why the cutover is now provable from health alone.
const GITHUB_REPO_SLUG = "looping-forever/daily-media-news";

const DISPATCH_URL =
  "https://api.github.com/repos/looping-forever/daily-media-news/dispatches";

// Workflow dispatches API: wakes daily-news.yml for the morning/evening
// digest ~30 min before the GitHub-side dead-man's-switch schedule fires.
const DIGEST_DISPATCH_URL =
  "https://api.github.com/repos/looping-forever/daily-media-news/actions/workflows/daily-news.yml/dispatches";

// THE DEPLOYED SCHEDULE IS ONE CRON. Cloudflare Workers Free allows five cron
// triggers per account and this account is at the limit, so the jobs are merged
// into a single dispatcher rather than upgrading the plan.
//
// CRON_DISPATCHER is the ONLY cron expression Cloudflare holds. It is the only
// `const CRON_* = "<literal>"` in this file, which is what makes it the only
// trigger the three registries can possibly agree on: Check 16 in
// tests/workflows/test_workflow_lint.py reads this block with a regex that
// matches string LITERALS, so every logical schedule below is COMPUTED and is
// deliberately not a literal. A second literal here would silently re-enter
// schedule.json's set and break the free-tier budget all over again.
//
// The job schedule the dispatcher reproduces is stated as NUMBERS below, and
// planTick() is a pure function of them, so the cadence is arithmetic and can be
// compared against the pre-merge crons over a simulated day by running both.
const CRON_DISPATCHER = "* * * * *";
//
// THE JOB SCHEDULE, in the terms the dispatcher branches on. Every value here
// is the pre-merge cron it replaces, kept as the exact same instant.
//
// NOTE THE TWO SWEEPS ARE NOT WHAT THEIR EXPRESSION SUGGESTS: `*/3` is the
// BREAKING sweep and `*/2` is the ACADEMIC sweep. Read the code, not the names.
const SCHED_BREAKING_SWEEP_EVERY_MIN = 3;   // was `*/3 * * * *`, 480 fires/day
const SCHED_ACADEMIC_SWEEP_EVERY_MIN = 2;   // was `*/2 * * * *`, 720 fires/day
// The dead-man's switch's own tick. On its own minute rather than a rider on
// the two digest slots on purpose: those fire 2x/day, which would cap alarm
// resolution at up to 12 hours. Hourly, anchored on :17 (away from the :00
// stampede and away from breaking-monitor's own :47 tick), the same answer
// arrives within ~1h of the dispatcher going silent.
const SCHED_HEALTHCHECK_MIN = 17;           // was `17 * * * *`, 24 fires/day
// THE EMPTY-DIGEST RETRY LADDER'S TICK (2026-10-20). This was digest-watchdog.yml's
// second GitHub cron, `17,32,47 * * * *` -- 72 billed job-minutes a day whose only
// real work was to read state/digest_retry.json and, almost always, find nothing
// due. The Worker already ticks every minute, so the same 15-minute phase is now a
// branch of planTickFor() and a check costs a KV read instead of a runner.
//
// THE PHASE IS 17, NOT 0. The ladder's gates are +15 then +45 minutes after an
// empty digest, so three ticks an hour is the resolution the schedule needs, and
// `:17` keeps the tick off the `:00` stampede -- the same reason the healthcheck
// sits on :17. It is COMPUTED from PHASE+EVERY rather than written as a literal,
// for the reason stated above CRON_DISPATCHER: a `const CRON_* = "<literal>"` for
// this would re-enter schedule.json's trigger set through Check 16's literal regex.
const SCHED_LADDER_EVERY_MIN = 15;          // was `17,32,47 * * * *`, 72 fires/day
const SCHED_LADDER_PHASE = 17;              // the pre-merge cron's phase, in minutes
// The two digest slots, UTC. Nepal is +05:45, no DST, so 01:35 UTC is 07:20 NPT
// and 13:35 UTC is 19:20 NPT on the same calendar day.
const SCHED_DIGEST_SLOTS = {
  morning: { hour: 1, minute: 35 },
  evening: { hour: 13, minute: 35 },
};
// THE RE-FIRE, AS A BOUNDED WINDOW RATHER THAN ONE EXTRA CRON. Cloudflare cron
// START JITTER is unbounded from the inside: the 01:35 tick can genuinely begin
// at 01:47, and nothing in this file can prevent it. The window re-attempts every
// REFIRE_GAP_MINUTES and gives up after REFIRE_ATTEMPTS tries, so a late start
// becomes a short delay instead of a missed digest.
//
// The window closes at slot+20 (01:55 / 13:55 UTC), which is ten minutes BEFORE
// the 02:05 UTC GitHub fallback in daily-news.yml (07:50 NPT). Two arms racing
// for the same slot on every bad day would be a second push by construction, so
// the Worker stops while the fallback still owns the slot.
const REFIRE_GAP_MINUTES = 5;
const REFIRE_ATTEMPTS = 4;

// `M H * * *` for a daily slot, optionally offset by whole minutes.
function slotCronExpr(slotName, offsetMinutes) {
  const slot = SCHED_DIGEST_SLOTS[slotName];
  if (!slot) throw new Error("unknown digest slot: " + slotName);
  const total = slot.minute + (offsetMinutes || 0);
  return String(total % 60).padStart(2, "0") + " " +
         String(slot.hour + Math.floor(total / 60)) + " * * *";
}

// `*/N * * * *` and `M * * * *`, computed so neither is a `const CRON_*` literal.
function stepCronExpr(everyMinutes) {
  return "*/" + everyMinutes + " * * * *";
}

function minuteCronExpr(minute) {
  return String(minute) + " * * * *";
}

// THE PRE-MERGE EXPRESSIONS, recomputed. These are NOT deployed triggers any
// more; they are the logical job schedule the dispatcher reproduces, kept as
// ordinary cron strings because punctuality, the re-fire gap, Check 16 and the
// status page all reason in those terms. `digestScheduledInstant()` parses one,
// exactly as before.
const CRON_BREAKING_SWEEP = stepCronExpr(SCHED_BREAKING_SWEEP_EVERY_MIN);
const CRON_ACADEMIC_SWEEP = stepCronExpr(SCHED_ACADEMIC_SWEEP_EVERY_MIN);
const CRON_HEALTHCHECK = minuteCronExpr(SCHED_HEALTHCHECK_MIN);
const CRON_DIGEST_MORNING = slotCronExpr("morning");
const CRON_DIGEST_EVENING = slotCronExpr("evening");

// The ladder's minutes-of-hour and the cron expression they make, COMPUTED for
// the reason above: `"17,32,47 * * * *"` as a string literal would be a second
// `const CRON_* = "<literal>"` and would silently re-enter schedule.json's set.
// 17 -> 32 -> 47, three an hour, stopping at 60.
function ladderMinutes() {
  const out = [];
  for (let m = SCHED_LADDER_PHASE; m < 60; m += SCHED_LADDER_EVERY_MIN) out.push(m);
  return out;
}
const LADDER_MINUTES = ladderMinutes();
const CRON_DIGEST_LADDER = LADDER_MINUTES.join(",") + " * * * *";

// --- THE PREBUILD TRIGGER ------------------------------------------------------
//
// WHAT THIS IS, AND WHY IT EXISTS. `digest-prebuild.yml` already had a
// `workflow_dispatch` trigger with a `digest_time` choice input, and already did
// dispatch -> wait -> seal. The pipeline was never the problem; only its TRIGGER
// was. GitHub-hosted SCHEDULED runs were measured 4.5-7 hours late across 96
// `daily-news.yml` firings from 2026-08-29 to 2026-10-12 (mean 306.8 min, max
// 708.3 min), while `workflow_dispatch` created a run in ~2 seconds across 90
// dispatched runs. So the prebuild is now asked for by THIS Worker's own clock,
// which is punctual, instead of by GitHub's scheduler, which is not.
//
// THE 40 MINUTES IS DERIVED FROM ITS SLOT, NEVER RESTATED BESIDE IT. The prebuild
// instant is `digest slot - PREBUILD_LEAD_MINUTES`, computed, so the lead is
// arithmetic and cannot drift away from the slot it exists to serve. Measured
// prebuild duration is 5-9 minutes on the four most recent runs and 22 minutes on
// one, so 40 minutes is roughly twice the worst observed build.
//
// NOT A NEW CLOUDARE TRIGGER, AND NOT A NEW SECRET. It rides the ONE deployed
// `* * * * *` dispatcher, exactly like the two sweeps, the health check and the
// digest slots -- this account is AT Cloudflare's Workers Free limit of 5 cron
// triggers, which is why those five jobs were merged into a dispatcher in the
// first place. It reuses `GH_TOKEN`, which is already a wrangler secret.
//
// THE FAIL-SAFE IS STRUCTURAL, NOT A GUARD CLAUSE. Nothing on this path writes
// to KV, reads the delivery ledger, or records any marker the delivery arm
// consults. There is therefore no state a failed, slow, or abandoned prebuild can
// leave behind to suppress the slot: at 07:20 / 19:20 NPT the digest cron simply
// finds no sealed artifact, and the EXISTING fallback ladder owns the slot
// exactly as it did before this trigger existed -- `pushFallbackLadder` pushes
// immediately and `escalateToActions` dispatches a full build. A missed prebuild
// costs ONE PUSH ~22 MINUTES LATE; it never costs the owner the news.
// tests/digest/digest_prebuild_cron.test.js asserts each of those clauses, and
// tests/workflows/test_workflow_lint.py holds the cron and its Nepal Time.
const PREBUILD_LEAD_MINUTES = 40;

// The prebuild instant for one digest slot, as UTC hour/minute on the SAME UTC
// day. Returns null rather than wrapping into the previous day: a prebuild that
// belongs to the previous UTC day is a different date's artifact, and that is
// precisely the mislabelled-archive defect `digest-prebuild.yml` already refuses
// to guess at. null is handled by every caller, so a misconfiguration is loud in
// the log and can never brick the Worker (breaking detection must not fail
// because a digest trigger was mistyped).
function prebuildUtcFor(slotName) {
  const slot = SCHED_DIGEST_SLOTS[slotName];
  if (!slot) return null;
  const total = slot.hour * 60 + slot.minute - PREBUILD_LEAD_MINUTES;
  if (total < 0) return null;
  return {
    hour: Math.floor(total / 60),
    minute: total % 60,
    minutesFromMidnight: total,
  };
}

// `M H * * *` for the prebuild of one slot, or null when the slot cannot carry a
// lead this large. COMPUTED rather than a `const CRON_*` string literal on
// purpose: Check 16 compares the deployed cron list against the Worker's CRON_*
// constants, and a second literal here would silently re-enter the deployed set
// and put the account back over the free-tier trigger limit.
function prebuildCronExpr(slotName) {
  const pre = prebuildUtcFor(slotName);
  if (!pre) return null;
  return String(pre.minute).padStart(2, "0") + " " + String(pre.hour) + " * * *";
}

const CRON_PREBUILD_MORNING = prebuildCronExpr("morning");
const CRON_PREBUILD_EVENING = prebuildCronExpr("evening");

// The workflow_dispatch endpoint for the prebuild. A. IT IS NOW THE SAME
// WORKFLOW AS DIGEST_DISPATCH_URL, AND THAT IS THE POINT OF THE CHANGE.
//
// What this arm asks for has not changed: build the digest AHEAD of its slot,
// which was always a daily-news.yml run with `phase: prebuild`. digest-prebuild.yml
// used to be dialled first only so it could dial that build and then SIT WATCHING
// it for up to 45 minutes -- it had to, because `workflow_dispatch` returns no run
// id, so some other job had to stay alive to learn the result. The build now names
// itself to the seal in its last step, so the watch is gone and this arm dials the
// build directly.
//
// Kept as a named constant rather than inlined: the arm reads as one call with one
// reason, and tests can assert on a name instead of a URL literal.
const PREBUILD_DISPATCH_URL = DIGEST_DISPATCH_URL;
// U5. THE RE-FIRE PAIR, five minutes after each slot, as a BOUNDED WINDOW
// rather than a second cron. They exist because Cloudflare cron START JITTER is
// the real punctuality risk and nothing in this file can prevent it — the slot
// can genuinely begin at 01:47, and a slot that starts twenty minutes late is a
// slot that arrives twenty minutes late for a reason nobody can see. Re-attempting
// every REFIRE_GAP_MINUTES turns "the tick started late" from a MISS into a
// bounded delay, and costs nothing on the days that went normally: the ledger
// guard reads one key, finds `ntfy_sent_at` for the slot, and returns without a
// single network call.
//
// It is a BOUNDED re-fire and not an open-ended loop because two arms racing for
// the same slot on every bad day is a second push by construction, and because
// the 02:05 UTC GitHub fallback (07:50 NPT) already owns the slot after it. The
// last attempt is slot+20 minutes; after that this Worker stops asking.
//
// PUNCTUALITY IS STILL MEASURED AGAINST THE PRIMARY SLOT (see
// digestScheduledInstant). A digest the re-fire delivered really is late, and
// reporting it as on time would make the health line the one thing in this
// region that lies.
const CRON_DIGEST_REFIRE_MORNING = slotCronExpr("morning", REFIRE_GAP_MINUTES);
const CRON_DIGEST_REFIRE_EVENING = slotCronExpr("evening", REFIRE_GAP_MINUTES);
const CRON_DIGEST_REFIRE_MORNING_LAST = slotCronExpr(
  "morning", REFIRE_GAP_MINUTES * REFIRE_ATTEMPTS);
const CRON_DIGEST_REFIRE_EVENING_LAST = slotCronExpr(
  "evening", REFIRE_GAP_MINUTES * REFIRE_ATTEMPTS);

// --- THE DISPATCHER ----------------------------------------------------------
//
// ONE deployed cron, several jobs per tick. planTick() is the whole of the
// dispatch decision and it is PURE: no KV, no fetch, no clock of its own beyond
// the instant handed in. That is what makes "the cadence is unchanged" a thing
// a test can prove rather than a thing a comment asserts — the pre-merge crons
// and this function are both run over a simulated day and compared as sets.
//
// WHY A PURE CLOCK COMPARISON AND NOT A LOOKUP. A tick on which nothing is due
// must cost nothing. Reads are cheap on the free tier but invocations are not
// free to waste, and a KV read on the 456 no-op ticks a day would buy nothing:
// the schedule is arithmetic, so the arithmetic answers "is anything due" with
// no I/O at all.

// The jobs. Deliberately strings, so a test can assert on the PLAN without
// running any of the work.
const JOB_BREAKING = "breaking";
const JOB_ACADEMIC = "academic";
const JOB_HEALTHCHECK = "healthcheck";
const JOB_DIGEST_LADDER = "digest_ladder";

// Which logical cron expression maps to which job. This is also the legacy
// entry point: `scheduled({cron: "*/3 * * * *"})` still runs the breaking sweep
// and nothing else, which is what tests/breaking/worker_heartbeat.test.js drives.
// Those forms are NOT deployed triggers any more — only CRON_DISPATCHER is —
// and keeping them is a deliberate, documented compatibility surface, not a
// second schedule that could drift back into wrangler.toml.
function legacyJobForCron(cron) {
  if (cron === CRON_BREAKING_SWEEP) return JOB_BREAKING;
  if (cron === CRON_ACADEMIC_SWEEP) return JOB_ACADEMIC;
  if (cron === CRON_HEALTHCHECK) return JOB_HEALTHCHECK;
  // The ladder is legacy-drivable for the same reason the digest slots are: a
  // test drives one ladder tick from its own clock instead of waiting for the
  // grid. NOT a deployed trigger.
  if (cron === CRON_DIGEST_LADDER) return JOB_DIGEST_LADDER;
  if (cron === CRON_DIGEST_MORNING || cron === CRON_DIGEST_REFIRE_MORNING ||
      cron === CRON_DIGEST_REFIRE_MORNING_LAST) {
    return "digest:morning";
  }
  if (cron === CRON_DIGEST_EVENING || cron === CRON_DIGEST_REFIRE_EVENING ||
      cron === CRON_DIGEST_REFIRE_EVENING_LAST) {
    return "digest:evening";
  }
  // The prebuild instants are legacy-drivable for the same reason the digest
  // slots are: a test drives one job deterministically from its own clock
  // instead of waiting for the grid. NOT deployed triggers.
  if (CRON_PREBUILD_MORNING && cron === CRON_PREBUILD_MORNING) {
    return "prebuild:morning";
  }
  if (CRON_PREBUILD_EVENING && cron === CRON_PREBUILD_EVENING) {
    return "prebuild:evening";
  }
  return null;
}

// The instant a tick is judged against. `controller.scheduledTime` is the grid
// position Cloudflare scheduled, which is what keeps a job on its ORIGINAL
// instant when the invocation starts late; Date.now() is the fallback for a
// controller that does not carry it.
function tickInstantMs(controller) {
  const scheduled = controller && Number(controller.scheduledTime);
  if (Number.isFinite(scheduled) && scheduled > 0) return scheduled;
  return Date.now();
}

// The retry attempts for one slot, as UTC minute-of-hour offsets from the slot.
// attempt 1 is the slot itself; 2..REFIRE_ATTEMPTS are the re-fires.
function refireAttemptNumbers() {
  const out = [];
  for (let n = 1; n <= REFIRE_ATTEMPTS; n += 1) out.push(n);
  return out;
}

// The jobs due at this UTC hour/minute. Pure, and the ONLY place a tick is
// decided. Returns jobs in a fixed order so a plan is comparable.
function planTickFor(hourUtc, minuteUtc) {
  const jobs = [];
  if (minuteUtc % SCHED_BREAKING_SWEEP_EVERY_MIN === 0) jobs.push(JOB_BREAKING);
  if (minuteUtc % SCHED_ACADEMIC_SWEEP_EVERY_MIN === 0) jobs.push(JOB_ACADEMIC);
  if (minuteUtc === SCHED_HEALTHCHECK_MIN) jobs.push(JOB_HEALTHCHECK);
  if (LADDER_MINUTES.includes(minuteUtc)) jobs.push(JOB_DIGEST_LADDER);
  for (const slotName of Object.keys(SCHED_DIGEST_SLOTS)) {
    const slot = SCHED_DIGEST_SLOTS[slotName];
    if (slot.hour !== hourUtc) continue;
    const offset = minuteUtc - slot.minute;
    if (offset === 0) {
      jobs.push("digest:" + slotName);
    } else if (offset > 0 && offset % REFIRE_GAP_MINUTES === 0 &&
               offset <= REFIRE_GAP_MINUTES * REFIRE_ATTEMPTS) {
      // The bounded re-fire. Same call, same ledger, same guards: `ntfy_sent_at`
      // turns an already-sent slot into one KV read and no network call.
      jobs.push("digest:" + slotName);
    }
  }
  // THE PREBUILD JOB, derived from the slot rather than stated beside it. One
  // extra pair of jobs a day, both on the one-minute grid Cloudflare already
  // invokes, so `expected_invocations_per_day` does not move.
  for (const slotName of Object.keys(SCHED_DIGEST_SLOTS)) {
    const pre = prebuildUtcFor(slotName);
    if (!pre || pre.hour !== hourUtc || pre.minute !== minuteUtc) continue;
    jobs.push("prebuild:" + slotName);
  }
  return jobs;
}

// Every (hour, minute) the re-fire covers for one slot, as cron expressions.
// The window is what makes the retry BOUNDED, and it is reported by ?health=1
// so an operator can see where the Worker stops asking.
function refireWindowFor(slotName) {
  const out = [];
  for (let n = 1; n <= REFIRE_ATTEMPTS; n += 1) {
    out.push(slotCronExpr(slotName, REFIRE_GAP_MINUTES * n));
  }
  return out;
}

// The logical schedule the one deployed cron reproduces, as the cron
// expressions it replaces. Reported by ?health=1 and checked against the
// pre-merge set by the cadence-parity test.
function dispatchSchedule() {
  const jobs = [
    { job: JOB_BREAKING, cron: CRON_BREAKING_SWEEP, every_minutes:
        SCHED_BREAKING_SWEEP_EVERY_MIN },
    { job: JOB_ACADEMIC, cron: CRON_ACADEMIC_SWEEP, every_minutes:
        SCHED_ACADEMIC_SWEEP_EVERY_MIN },
    { job: JOB_HEALTHCHECK, cron: CRON_HEALTHCHECK, minute_of_hour:
        SCHED_HEALTHCHECK_MIN },
    // The ladder, moved off GitHub's scheduler on 2026-10-20. Listed here because
    // this is the Worker's declared job schedule; NOT a Cloudflare trigger, so it
    // does not move expected_invocations_per_day.
    { job: JOB_DIGEST_LADDER, cron: CRON_DIGEST_LADDER, every_minutes:
        SCHED_LADDER_EVERY_MIN },
  ];
  for (const slotName of Object.keys(SCHED_DIGEST_SLOTS)) {
    jobs.push({ job: "digest:" + slotName, cron: slotCronExpr(slotName),
                slot: slotName });
    jobs.push({ job: "digest:" + slotName + ":refire",
                cron: refireWindowFor(slotName)[0], slot: slotName,
                refire_every_minutes: REFIRE_GAP_MINUTES,
                refire_attempts: REFIRE_ATTEMPTS,
                refire_last: refireWindowFor(slotName)[REFIRE_ATTEMPTS - 1] });
    // The prebuild the same dispatcher now asks for, with the lead it was
    // derived from so a reader can see the margin rather than infer it.
    //
    // A. THE TARGET IS THE BUILD ITSELF, NOT THE ORCHESTRATOR. This dispatches
    // daily-news.yml with `phase: prebuild`. digest-prebuild.yml is still what
    // SEALS the result, but the run that produced it now names itself to that
    // workflow, so nothing here has to dial an intermediary or watch one.
    jobs.push({ job: "prebuild:" + slotName, cron: prebuildCronExpr(slotName),
                slot: slotName, lead_minutes: PREBUILD_LEAD_MINUTES,
                workflow: "daily-news.yml" });
  }
  return jobs;
}

// --- Academic sweep (TU IOST result tripwire) --------------------------------
// Cheap detection only: fetch the Result notice feed, regex the notice titles
// for a CSIT semester result, and hand off to the GitHub workflow via
// repository_dispatch. NO PDF download / OCR here — that lives in
// scripts/check_academic_result.py (triggered by the academic-result.yml
// workflow). This keeps the worker on the FREE tier (network I/O only).

// TU IOST Result category feed (notice_type=21).
const ACADEMIC_FEED_URL = "https://iost.tu.edu.np/notices?notice_type=21";

// DUAL-MAINTENANCE WARNING: this regex MUST stay in sync with SEMESTER_RE in
// scripts/check_academic_result.py. If you change the semester-token list or
// the phrase structure there, update it here too (and vice versa). The Python
// version is the authoritative one (it drives the actual PDF/roll logic); this
// JS copy is only a cheap pre-filter to decide whether to wake the workflow.
// Dual maintenance CANNOT be removed — the Worker is deployed separately and
// cannot read config/exam_notices.json. tests/academic/test_worker_parity.py is
// the mitigation: it fails on drift.
// Semester token alternatives (Roman + Arabic + word forms), longest-first so
// "IV" matches over "I", "VIII" over "V", etc. — mirrors _SEM_TOKEN in the
// Python script.
const ACADEMIC_SEM_TOKEN =
  "(?:VIII|8th|Eighth|VII|7th|Seventh|VI|6th|Sixth|IV|4th|Fourth" +
  "|III|3rd|Third|II|2nd|Second|V|5th|Fifth|I|1st|First)";
// F3: stage 1 is three independent presence tests — a CSIT marker, a semester
// token, and a result term — in ANY order. The old ordered regex
// ("CSIT … sem … Semester … Result") was a silent UNDER-matcher: TU is not
// obliged to word a title in that order, and under-matching loses a result
// forever. Over-matching is safe here (it costs one dispatch).
const ACADEMIC_MARKER_RE = /(?:B\.?\s*Sc\.?|BSc|CSIT)/i;
const ACADEMIC_RESULT_TERMS_RE =
  /\b(?:result|results|back|re-?exam|supplementary|provisional|marks|transcript)\b/i;
const ACADEMIC_SEM_TOKEN_RE = new RegExp("\\b" + ACADEMIC_SEM_TOKEN + "\\b", "i");
const ACADEMIC_SEMESTER_RE = new RegExp(
  "(?=.*" + ACADEMIC_MARKER_RE.source + ")" +
    "(?=.*\\b" + ACADEMIC_SEM_TOKEN + "\\b)" +
    "(?=.*" + ACADEMIC_RESULT_TERMS_RE.source + ")",
  "is"
);

// KV keys for the academic sweep.
const ACADEMIC_LAST_EXAMINED_KEY = "academic:last_examined_notice_id"; // gap-free floor (R5)
const ACADEMIC_LEGACY_LAST_CHECKED_KEY = "academic:last_checked_notice_id";
const ACADEMIC_EXAMINED_KEY = "academic:examined"; // read once, written once
const ACADEMIC_DISPATCHED_PREFIX = "academic:dispatched:";
const ACADEMIC_MAX_PENDING = 60; // R2
const ACADEMIC_EXAMINED_CAP = 300; // lowest ids evicted first
const ACADEMIC_REPLAY_WINDOW = 200;

// Summary of the last COMPLETED academic sweep seen by THIS isolate, for the
// status page only. Worker isolates are evicted at will, so this is a
// best-effort liveness signal and is deliberately never read by the sweep
// logic — nothing in the no-burn path may depend on isolate lifetime.
let ACADEMIC_LAST_SWEEP = null;

// Notice detail URL carries a trailing numeric id, e.g. /notices/14559.
const ACADEMIC_NOTICE_ID_RE = /\/(?:notice|notices|view)\/?(\d+)\b/i;
// Anchor: href + inner text (same shape as the Python _ANCHOR_RE).
const ACADEMIC_ANCHOR_RE = /<a\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)<\/a>/gi;

// --- RSS parsing (Workers have no DOMParser; regex only) --------------------

const ITEM_RE = /<item>([\s\S]*?)<\/item>/g;
const TITLE_RE = /<title>([\s\S]*?)<\/title>/i;
const LINK_RE = /<link>([\s\S]*?)<\/link>/i;
const PUBDATE_RE = /<pubDate>([\s\S]*?)<\/pubDate>/i;
// The gate blob is title + snippet, so the parser has to actually carry a
// snippet. Both spellings are real: Google News RSS and every direct publisher
// feed use <description>, Atom-style mirrors use <content:encoded>. Either is
// accepted; an item with neither keeps snippet === "" and degrades to a
// title-only blob, which is the old (tighter) behaviour and therefore safe.
const DESCRIPTION_RE = /<description(?:\s[^>]*)?>([\s\S]*?)<\/description>/i;
const CONTENT_ENCODED_RE =
  /<content:encoded(?:\s[^>]*)?>([\s\S]*?)<\/content:encoded>/i;

// Bound on the snippet so one enormous <content:encoded> body cannot bloat
// every candidate. 500 chars is several times the length of a real feed
// description, so truncation is a safety rail rather than a routine cut.
const SNIPPET_MAX_CHARS = 500;

// RSS descriptions and <content:encoded> are HTML, not text. Strip the markup
// before it reaches the gate: an href, a class name or an alt attribute is not
// newsroom language, and leaving it in would widen the gate on pure noise.
function stripHtml(s) {
  return s
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function snippetOf(block) {
  const raw =
    tagText(block, DESCRIPTION_RE) || tagText(block, CONTENT_ENCODED_RE);
  if (!raw) return "";
  const text = stripHtml(raw);
  return text.length > SNIPPET_MAX_CHARS
    ? text.slice(0, SNIPPET_MAX_CHARS)
    : text;
}

function unescapeXml(s) {
  return s
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&"); // must be last
}

function tagText(block, re) {
  const m = block.match(re);
  if (!m) return "";
  return unescapeXml(
    m[1]
      .replace(/^\s*<!\[CDATA\[/, "")
      .replace(/\]\]>\s*$/, "")
      .trim()
  );
}

// THE FRESHNESS CAVEAT, AND WHAT IT COSTS, IN TWO PARTS.
//
// Until 2026-10-13 the guard was `if (!title || !pubDateRaw) continue;` — an
// item with NO date element was discarded with no count, no log and no flag,
// while the feed list above still read as though kathmandupost.com were being
// ingested. It is not optional and never was: kathmandupost.com/rss serves 40
// valid English headlines and carries no <pubDate> element at all, so all 40
// were thrown away on every sweep, and the code read as if they were not.
//
// THE RULE, and it is deliberately asymmetric:
//   * a date that is PRESENT but unparsable is still discarded. A corrupt
//     timestamp is a data error and guessing an age from it would be worse
//     than dropping it;
//   * a date that is ABSENT cannot be window-checked at all, so the item is
//     admitted on a STRONGER bar instead: its TITLE must carry HARD live
//     evidence — a casualty, or something blocked/closed/evacuated/cut off
//     RIGHT NOW — the same two classes the framing veto already treats as
//     unretractable. See the measurement below for why, and why the evidence
//     is read from the title and not the snippet.
//
// WHY THAT SECOND BAR, MEASURED NOT GUESSED. Admitting every dateless item was
// tried first, live, on 2026-10-13: kathmandupost's 40 undated headlines jumped
// the sweep from 7 in-window candidates to 48 and from 0 tripped to 14 — and
// most of the new 14 were not breaking news at all ("Dismantle
// party-affiliated student unions", "Trump announces Ukraine energy
// ceasefire", "Can Himalayan floods bridge the India-China divide?").
// Requiring HARD evidence at all brought that to 7. Requiring it in the TITLE
// alone brought it to 2, and the 2 that survive are the two this system exists
// to catch: "Army helicopter rescues three trapped by landslide in Gorkha" and
// "At least 139 migrants dead".
//
// TITLE ONLY, and the reason is which layer owns the decision. This feed's
// <description> IS the article's own summary (verified, not assumed), so
// title+snippet is a faithful reading of the story — but what the extra three
// tripped on was the GATE's vocabulary, not the freshness bar: a story about
// protests and arrests genuinely contains casualty and closure words. Widening
// what an undated item may say is therefore a change to shared, parity-tested
// gate vocabulary, and that is a different change from candidate supply. The
// conservative bar is chosen here on purpose: under-admitting is the direction
// that cannot train the owner to ignore alerts again.
//
// WHAT THAT COSTS, stated rather than hidden: a dateless live event whose
// headline asserts no HARD harm is not admitted from this feed. Measured
// example: "Flood cuts off two villages along Budhigandaki in Gorkha" is
// dropped, because HARD_LIVE_DISRUPTION_RE matches `cut off` and not `cuts
// off`. Closing that gap is a one-token widening (`cuts? off`) applied to both
// gate implementations together; it was deliberately NOT done here, because
// widening the shared gate is not a candidate-supply change and belongs in its
// own commit with its own corpus re-measure. Until then the loss is bounded:
// breaking-monitor.yml's 4x/day schedule net runs the full pipeline
// independently, and under-admitting is the safe direction for this bar.
//
// WHY ADMITTING ANY UNDATED ITEM IS SAFE HERE, specifically: this sweep does
// not decide anything. It wakes a pipeline that re-fetches, re-gates,
// classifies and verifies on its own 90-minute window before any alert is
// sent. The 30-minute window below is an OPTIMISATION, not the decision, so a
// headline of unknown age cannot by itself cause a send — it can only cause
// the pipeline to look.
function parseFeedItems(xml) {
  const items = [];
  let m;
  ITEM_RE.lastIndex = 0;
  while ((m = ITEM_RE.exec(xml)) !== null) {
    const block = m[1];
    const title = tagText(block, TITLE_RE);
    const link = tagText(block, LINK_RE);
    const pubDateRaw = tagText(block, PUBDATE_RE);
    if (!title) continue;
    const snippet = snippetOf(block);
    let undated = false;
    if (!pubDateRaw) {
      if (!HARD_LIVE_EVIDENCE_RE.test(title) &&
          !HARD_LIVE_DISRUPTION_RE.test(title)) {
        // Undated AND the headline itself asserts no present-tense live harm: an
        // age we cannot verify and a claim we cannot corroborate. Counted by
        // the caller, never silently dropped.
        continue;
      }
      undated = true;
    } else {
      const ts = new Date(pubDateRaw).getTime();
      if (!Number.isFinite(ts)) continue; // present but corrupt: a data error
      // Within the last 30 minutes (tiny future clock skew tolerated).
      if (ts < Date.now() - WINDOW_MS) continue;
    }
    items.push({ title, link, snippet, undated });
  }
  return items;
}

// --- helpers -----------------------------------------------------------------

async function fetchWithTimeout(url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function sha1Hex(text) {
  const digest = await crypto.subtle.digest(
    "SHA-1",
    new TextEncoder().encode(text)
  );
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// THE GATE BLOB. Frozen shared contract with scripts/breaking_monitor.py
// (_gate_blob there, "%s %s" % (title, snippet)). This exact literal is
// asserted by tests/breaking/test_worker_gate_parity.py — do not "improve" it.
//
// WHY IT EXISTS: the Worker runs BEFORE Python, so it is the binding
// constraint. Gating on the title alone while Python gates on title+snippet
// made the Worker strictly TIGHTER, so a headline whose life-safety signal
// lived only in the snippet was dropped here and never reached Python — and
// that loss is indistinguishable from "nothing is happening", which is exactly
// the wrong conclusion to reach on a breaking-news path.
//
// The `|| ""` is load-bearing: a feed item with no description must degrade to
// the title alone, never to the literal text "undefined" (which would be a new
// and garbage gate input).
function gateBlob(item) {
  return `${item.title} ${item.snippet || ""}`;
}

function sportGossipHits(text) {
  return allHits(SPORT_GOSSIP_RE, text).length;
}

// Whole-match scan, mirroring Python's re.findall() on a 0-capture-group
// pattern (every gate pattern above is non-capturing precisely so that
// findall()/m[0] agree -- a capturing group would make the audit trail a list
// of bare fragments).
//
// Deliberately builds its own global clone instead of touching .lastIndex on
// the shared constant: a stateful regex reused across two sweeps is the kind of
// defect that only shows up on an odd-numbered run.
function allHits(rx, text) {
  const flags = rx.flags.indexOf("g") === -1 ? rx.flags + "g" : rx.flags;
  const re = new RegExp(rx.source, flags);
  const out = [];
  let m;
  while ((m = re.exec(text)) !== null) {
    out.push(m[0]);
    if (m[0].length === 0) re.lastIndex++; // never spin on a zero-width match
  }
  return out;
}

// THE framing veto, in one place so the two override classes can never be
// applied inconsistently between the sweep and the tests. Mirrors
// breaking_monitor.py:_framing_veto_applies.
function framingVetoApplies(text) {
  if (!NOT_A_LIVE_EVENT_RE.test(text)) return false;
  if (HARD_LIVE_EVIDENCE_RE.test(text)) return false;
  if (HARD_LIVE_DISRUPTION_RE.test(text)) return false;
  return true;
}

// "Nepal Landslide - Dayton Daily News" is a section label, not an event.
// Structural, not vocabulary, so it cannot be tuned to one incident.
//
// THE TOKENISER IS SCRIPT-AWARE, and the character class is written out
// literally on purpose. It used to be `[A-Za-z0-9']+`, which is ASCII-only, so
// a Devanagari headline tokenised to ZERO words, scored 0 informative words, and
// was discarded as a bare section label before the emergency vocabulary was ever
// consulted. Measured over live feeds on 2026-10-13 that was 6 of 6 in-window
// headlines suppressed, 55 of 55 over 72h on onlinekhabar and 12 of 12 on
// nepalnews. Nepali-language breaking news — which is where most of it is —
// could not reach the classifier through this arm at all, while the code read as
// though it were ingesting those feeds.
//
// THE CLASS IS ASCII ALNUM + APOSTROPHE + U+0900-U+097F (the Devanagari block),
// spelled identically in breaking_monitor.py:_is_bare_label. A literal class is
// used rather than `\w` or `\p{L}` on purpose: Python's `\w` is Unicode-aware
// and JavaScript's is ASCII-only, and `\p{L}` needs the `u` flag and depends on
// the engine's Unicode tables. Two engines, two Unicode property tables, is a
// parity risk this gate cannot carry — the parity test in
// tests/breaking/test_breaking_chain.py runs BOTH implementations over a frozen
// 616-row corpus and fails on any divergence, so the two sides must mean
// byte-for-byte the same thing.
//
// Devanagari punctuation (the danda, U+0964) sits inside this block but is one
// code point long, and the informative-word count requires length > 1, so a
// danda is a separator in practice rather than a word.
function isBareLabel(title) {
  const stripped = (title || "").trim().replace(BARE_LABEL_SUFFIX_RE, "");
  const words = stripped.match(/[A-Za-z0-9'\u0900-\u097F]+/g) || [];
  let informative = 0;
  for (const w of words) if (w.length > 1) informative++;
  return informative < MIN_INFO_WORDS;
}

// Tier 4. Mirrors breaking_monitor.py:_universal_hits -- the single home of the
// mass-casualty count logic, so the threshold is defined in exactly one place
// per side. Returns null when the tier is silent, else the reason string.
function universalHits(text) {
  const m = /(\d{1,4})\s+(?:dead|kills?|killed|deaths?|casualt(?:y|ies))\b|(?:dead|kills?|killed|deaths?|casualt(?:y|ies))\s+(\d{1,4})\b/i.exec(
    text
  );
  if (m) {
    const raw = m[1] || m[2];
    const count = parseInt(raw, 10);
    if (!isNaN(count) && count > UNIVERSAL_MIN_DEATH_COUNT) {
      return "universal:deaths>" + UNIVERSAL_MIN_DEATH_COUNT + "(" + count + ")";
    }
  }
  if (UNIVERSAL_IMPORTANCE_RE.test(text)) return "universal:pattern";
  return null;
}

// Python's sorted() on str is code-point order; JS's default sort is UTF-16
// code-unit order. They differ only for astral characters, which a regex hit on
// a headline will not contain, but the comparator is spelled out so the parity
// claim does not rest on that assumption.
function byCodePoint(a, b) {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

// THE GATE. A port of breaking_monitor.py:importance_gate, evaluated ONCE,
// before any discard.
//
// ORDERING IS PART OF THE CONTRACT and is the order Python uses:
//   1. the sport/gossip veto runs FIRST, before any hit counting, so a wider
//      vocabulary can never drown it out;
//   2. the not-a-live-event framing veto, skipped whenever HARD live evidence
//      of EITHER class is present (casualty, or blocked/closed/evacuated/cut);
//   3. the bare-label test -- both suppress-only;
//   4. EMERGENCY_RE | BREAKING_IMPACT_RE | BREAKING_IMPACT_WIDE_RE;
//   5. only when all three are silent, the universal-importance tier.
//
// Returns {tripped, why} with `why` in the same vocabulary Python uses, so the
// two implementations' audit trails are comparable rather than merely similar.
function importanceGate(blob, title) {
  if (sportGossipHits(blob) >= SPORT_GOSSIP_SUPPRESS_HITS) {
    return { tripped: false, why: "sport_gossip_dominating" };
  }
  if (framingVetoApplies(blob)) {
    return { tripped: false, why: "not_a_live_event" };
  }
  if (isBareLabel(title === undefined || title === null ? blob : title)) {
    return { tripped: false, why: "bare_label" };
  }
  const hits = new Set();
  for (const rx of [EMERGENCY_RE, BREAKING_IMPACT_RE, BREAKING_IMPACT_WIDE_RE]) {
    for (const h of allHits(rx, blob)) hits.add(h.toLowerCase());
  }
  if (hits.size === 0) {
    const why = universalHits(blob);
    if (why) hits.add(why);
  }
  if (hits.size === 0) return { tripped: false, why: "" };
  const sorted = Array.from(hits).sort(byCodePoint);
  return { tripped: true, why: "unified:" + sorted.join(",").slice(0, 160) };
}

// Delivery telemetry. A 401/403 from the dispatches API is invisible on the
// free tier unless you are already streaming Cloudflare logs, and
// "GH_TOKEN: present" reads like health while proving nothing — the secret can
// be present, wrong, expired, or missing the Contents:write scope, and the
// only symptom is that alerts quietly stop arriving. These two keys put the
// answer on the /status page itself.
const BREAKING_LAST_DISPATCH_KEY = "breaking:last_dispatch";
const BREAKING_LAST_SUCCESS_KEY = "breaking:last_success";

// --- Dead-man's switch: liveness, as distinct from delivery ------------------
//
// WHY THIS EXISTS. BREAKING_LAST_SUCCESS_KEY is written ONLY when a dispatch
// actually succeeds (recordDispatch, reachable only from dispatchAlert). So on
// a quiet day the Worker writes ZERO KV state, and "no key" cannot distinguish a
// healthy-and-quiet system from a dead cron. The system could not answer its own
// most important question. These two keys are written on every sweep regardless
// of whether anything is happening, which is what makes the absence of an alert
// mean something.
//
// THE CONTRACT, in one place:
//   * the heartbeat is written from the CRON path in scheduled(), NEVER from
//     handle()/handleAcademic() — see the placement comment at the call site,
//     which is the load-bearing detail;
//   * it answers "did the cron FIRE?", never "did the work SUCCEED?". A missing
//     GH_TOKEN must not be able to freeze the liveness key and manufacture a
//     false alarm, and delivery telemetry already exists above;
//   * it is throttled, so 479 of 480 */3 sweeps cost zero writes;
//   * a KV READ failure in checkHeartbeats is treated as NOT stale and logged
//     loudly, because a KV blip must not storm the owner every hour.
const BREAKING_HEARTBEAT_KEY = "breaking:heartbeat";
const ACADEMIC_HEARTBEAT_KEY = "academic:heartbeat";
// 480 sweeps/day throttled to one write per 30 min = 48 writes/day; 720
// academic sweeps/day likewise = 48/day. That is 96 writes/day against the
// free tier's ~1,000/day, and it is bounded by the clock rather than by the
// cron, so a schedule change can never multiply it silently.
const HEARTBEAT_THROTTLE_MS = 30 * 60 * 1000;
// Older than this and the cron is presumed dead. Comfortably above the throttle
// so a normal throttle can never be mistaken for staleness, and comfortably
// below the hourly health-check tick so one missed sweep is not an incident.
const HEARTBEAT_STALE_MS = 60 * 60 * 1000;
// Where the "I already screamed" stamp lives. Keyed by nothing else: a second
// stale heartbeat (academic AND breaking) inside the cooldown is folded into
// the next alert's body rather than paging again.
const HEALTH_ALERT_KEY = "ops:heartbeat_alert";
// Storm guard. The health check runs hourly; without this a cron that stays
// dead would page the owner 24x/day, and an alarm that fires that often is an
// alarm nobody reads.
const ALERT_COOLDOWN_MS = 6 * 60 * 60 * 1000;

// BUILD STAMP. "unset" is the honest default and it is load-bearing, not
// cosmetic: it means "this deployment was never stamped", which is exactly what
// the drift canary in drift-canary.yml asserts against master HEAD. The value is
// injected at DEPLOY time from the checkout, never hand-typed into this file:
//
//   wrangler deploy -c deploy/wrangler.toml --var WORKER_BUILD:<short sha>
//
// wrangler's --var lands in env.WORKER_BUILD and overrides nothing else, so a
// deployment from a stale checkout is visible here as a sha that is not master.
const WORKER_BUILD = "unset";

// What the deployed build reports. env.WORKER_BUILD wins when present (that is
// the deploy-time stamp); otherwise the in-source constant, i.e. "unset".
function workerBuild(env) {
  const stamped = env && env.WORKER_BUILD ? String(env.WORKER_BUILD).trim() : "";
  return stamped || WORKER_BUILD;
}

// Telemetry must NEVER change the dispatch verdict. A KV write failure here
// costs observability and nothing else; the caller already has its answer, so
// the error is logged and swallowed rather than propagated.
async function recordDispatch(env, record) {
  if (!env || !env.KV) return;
  try {
    await env.KV.put(BREAKING_LAST_DISPATCH_KEY, JSON.stringify(record));
    if (record.ok) {
      await env.KV.put(
        BREAKING_LAST_SUCCESS_KEY,
        JSON.stringify({ at: record.at, title: record.title })
      );
    }
  } catch (err) {
    console.log("dispatch telemetry write failed:", String(err));
  }
}

// --- Liveness heartbeat (dead-man's switch, worker side) ---------------------

// The two keys the health check watches, with the operator-facing consequence of
// each being silent. Kept in ONE place so the alarm body and the staleness rule
// cannot be written twice and disagree.
const HEARTBEAT_WATCH = [
  {
    key: BREAKING_HEARTBEAT_KEY,
    label: "breaking-monitor cron",
    consequence:
      "No breaking-news detection has run since then, so NO BREAKING ALERT " +
      "CAN FIRE — a flood, an outage or a landslide will pass silently.",
    what: "Workers -> breaking-monitor -> Cron Triggers",
  },
  {
    key: ACADEMIC_HEARTBEAT_KEY,
    label: "academic cron",
    consequence:
      "No TU result-notice detection has run since then, so a semester result " +
      "notice can sit unannounced until someone opens the board by hand.",
    what: "Workers -> breaking-monitor -> Cron Triggers",
  },
];

// Tolerant parse. A corrupt or truncated value is treated as ABSENT rather than
// thrown on, so one bad write self-heals on the next throttled write instead of
// freezing the key forever.
function parseHeartbeat(raw) {
  if (raw === null || raw === undefined || raw === "") return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (err) {
    console.log("heartbeat value unreadable, treating as absent:", String(err));
    return null;
  }
}

// Record "the cron fired", at most once per HEARTBEAT_THROTTLE_MS.
//
// NEVER THROWS. Liveness telemetry that can fail a sweep is worse than no
// telemetry: a heartbeat is not part of any verdict, and the caller (the cron
// path) has already decided what to do. Read-then-conditionally-write, so the
// write is suppressed inside the throttle window and the KV read budget is
// untouched on the hot path (a read is ~100x cheaper than a write here).
//
// Returns true if a write happened. Never inspect the return value for
// correctness — nothing may depend on it.
async function touchHeartbeat(env, key, cronLabel) {
  if (!env || !env.KV) {
    console.log("heartbeat skipped (no KV binding): " + key);
    return false;
  }
  try {
    const prev = parseHeartbeat(await env.KV.get(key));
    const now = Date.now();
    const prevTs = prev ? Number(prev.ts) : NaN;
    if (Number.isFinite(prevTs) && now - prevTs < HEARTBEAT_THROTTLE_MS) {
      return false; // inside the throttle window: 479 of 480 sweeps land here
    }
    await env.KV.put(
      key,
      JSON.stringify({
        ts: now,
        at: new Date(now).toISOString(),
        build: workerBuild(env),
        cron: cronLabel || "",
      })
    );
    return true;
  } catch (err) {
    console.log("heartbeat write failed for " + key + ":", String(err));
    return false;
  }
}

// Age in seconds, or null when the key was never written / unusable.
// null is NOT 0 and NOT a pass: for liveness purposes "never" is the worst
// possible answer, and every consumer treats it that way.
function heartbeatAgeSeconds(record, now) {
  const ts = record ? Number(record.ts) : NaN;
  if (!Number.isFinite(ts)) return null;
  return Math.max(0, Math.round((now - ts) / 1000));
}

// Human-readable rendering of one heartbeat age for the status page.
//
// `unreadable` is deliberately a distinct word from a number. A persistent KV
// outage keeps the WORKER channel silent by design (a KV blip must not storm the
// owner), so the status page is the only place that condition is visible — and
// only if it says so instead of rendering a number or a blank.
function describeHeartbeatAge(ageSeconds, unreadable) {
  if (unreadable) {
    return "unreadable (KV read failed — a KV outage suppresses the alarm by " +
      "design; see the Worker's logs)";
  }
  if (ageSeconds === null) {
    return "NEVER RECORDED (no sweep has written it since this build deployed)";
  }
  return ageSeconds + "s old (" + Math.floor(ageSeconds / 60) +
    " min; stale after " + Math.floor(HEARTBEAT_STALE_MS / 60000) + " min)";
}

async function readHeartbeatFor(env, key) {
  const record = parseHeartbeat(await env.KV.get(key));
  return { record: record, ageSeconds: heartbeatAgeSeconds(record, Date.now()) };
}

// The hourly alarm. Runs from the CRON_HEALTHCHECK tick.
//
// THE THREE RULES THAT MATTER, in order:
//   1. A READ failure is NOT staleness. If KV cannot be read we do not know
//      whether the cron is alive, and paging the owner on a KV blip teaches them
//      to ignore this arm. It is logged at error level instead, and the status
//      page shows the same condition as `unreadable`.
//   2. A cooldown suppresses the send but never the diagnosis: inside
//      ALERT_COOLDOWN_MS it returns without sending, and says so in the log.
//   3. The alert key is written ONLY after a successful POST. A missing or
//      broken NTFY_TOPIC must not burn the cooldown, or fixing the secret would
//      leave the system silent for another 6 hours.
async function checkHeartbeats(env) {
  if (!env || !env.KV) {
    console.error("heartbeat health check skipped: no KV binding");
    return { ok: false, reason: "no KV binding", stale: [], alerted: false };
  }

  const now = Date.now();
  const stale = [];
  const unreadable = [];
  const healthy = [];

  for (const entry of HEARTBEAT_WATCH) {
    let age = null;
    try {
      age = (await readHeartbeatFor(env, entry.key)).ageSeconds;
    } catch (err) {
      // Rule 1. Loud, non-alarming, and explicitly NOT stale.
      console.error(
        "heartbeat READ FAILED for " + entry.key + " (" + String(err) +
          ") — treating as NOT stale: a KV blip must not storm the owner"
      );
      unreadable.push(entry.key);
      continue;
    }
    if (age === null) {
      // The WHOLE entry is carried, not just {key, minutes}: the operator-facing
      // consequence and the "where to look" pointer live on `entry`, and
      // rebuilding them here is how an alert once shipped the literal string
      // "undefined" in place of the most important sentence it carried.
      stale.push(Object.assign({}, entry, { minutes: null }));
    } else if (age * 1000 > HEARTBEAT_STALE_MS) {
      stale.push(Object.assign({}, entry, { minutes: Math.floor(age / 60) }));
    } else {
      healthy.push(entry.key + " (" + Math.floor(age / 60) + " min old)");
    }
  }

  for (const line of healthy) console.log("heartbeat OK: " + line);

  if (!stale.length) {
    return { ok: true, stale: [], unreadable: unreadable, alerted: false };
  }

  const detail = stale
    .map((s) =>
      s.minutes === null
        ? "  * " + s.label + ": NEVER RECORDED (" + s.key +
          " has never been written — the cron has not run since this build " +
          "was deployed, or it is pointed at a different KV namespace)"
        : "  * " + s.label + ": silent for " + s.minutes +
          " min (limit " + Math.floor(HEARTBEAT_STALE_MS / 60000) + " min)"
    )
    .join("\n");
  const consequences = stale.map((s) => "  - " + s.consequence).join("\n");
  // LEAD FIRST. The body used to open with a component name and a count --
  // "breaking-monitor dead-man's switch: 1 of 2 cron heartbeat(s) are stale."
  // -- which told the owner nothing about what he had actually lost, because
  // the consequence (the `consequence` string, already written in plain words)
  // sat three lines further down. The owner reads this on a phone, where the
  // first line is all that shows collapsed. So: consequence, then the
  // measurement, then the per-cron detail, then where to look. Nothing is
  // removed: `detail` still carries the key, the age and the limit, and CHECK
  // still names the surface. The numbered operator procedure that used to sit
  // under CHECK moved to the log below, because a phone notification collapsed
  // on a lock screen is not where anyone runs a wrangler command.
  const body = [
    "CONSEQUENCE:",
    consequences,
    "",
    "dead-man's switch: " + stale.length +
      " of " + HEARTBEAT_WATCH.length + " cron heartbeat(s) are stale.",
    "",
    detail,
    "",
    "CHECK: " + stale[0].what + " — is the cron still listed and enabled?",
  ].join("\n");

  // The operator's half, moved OUT of the notification and INTO the log: the
  // same surfaces, the same wrangler command and the same build stamp the body
  // used to carry, where a developer looks and the owner does not.
  console.log(
    "heartbeat alarm operator detail — surfaces: " +
      HEARTBEAT_WATCH.map((h) => h.what).join(" | ") +
      "; if a cron is missing, run: wrangler deployments list -c " +
      "deploy/wrangler.toml and compare the deployed WORKER_BUILD against " +
      "master HEAD (a build stamp of \"unset\" or an old sha means the " +
      "deployment came from a stale checkout); a missing/expired GH_TOKEN " +
      "does NOT cause this alarm (the heartbeat is written before that guard), " +
      "so do not chase the PAT first. build stamp: " + workerBuild(env)
  );

  // The title used to read "breaking-monitor cron silent: breaking-monitor cron
  // silent for 180 min" — the same fact twice, in the wrong order, with the
  // consequence missing entirely. It now leads with what is broken and keeps
  // the silence measurement, which is the part that identifies WHICH cron.
  const title =
    "No alert can fire: " + stale[0].label +
    (stale[0].minutes === null
      ? " has never run"
      : " silent for " + stale[0].minutes + " min");

  // Rule 3 setup: read the cooldown stamp defensively. An unreadable stamp is
  // treated as "never alerted" — refusing to alert because we could not read a
  // throttle counter is the same class of error as refusing to alert because a
  // heartbeat read failed.
  let lastAlertAt = NaN;
  try {
    const prior = parseHeartbeat(await env.KV.get(HEALTH_ALERT_KEY));
    lastAlertAt = prior ? Number(prior.ts) : NaN;
  } catch (err) {
    console.log("heartbeat alert stamp unreadable:", String(err));
  }
  if (Number.isFinite(lastAlertAt) && now - lastAlertAt < ALERT_COOLDOWN_MS) {
    console.log(
      "heartbeat stale (" + stale.length + ") but an alert was sent " +
        Math.floor((now - lastAlertAt) / 60000) + " min ago — cooldown " +
        Math.floor(ALERT_COOLDOWN_MS / 60000) + " min, NOT re-alerting"
    );
    return { ok: true, stale: stale, unreadable: unreadable, alerted: false,
             suppressed_by: "cooldown" };
  }

  const topic = String((env && env.NTFY_TOPIC) || "").trim();
  const server = String((env && env.NTFY_SERVER) || "").trim();
  if (!topic) {
    // LOUD, and specifically named: without this secret the dead-man's switch
    // is a silent one, which is worse than having none.
    console.error(
      "CRITICAL: NTFY_TOPIC is not set on this Worker, so the dead-man's " +
      "switch CANNOT alarm. " + stale.length + " heartbeat(s) are stale and " +
      "the owner was NOT paged. Fix with: wrangler secret put NTFY_TOPIC " +
        "-c deploy/wrangler.toml"
    );
  } else if (!/^https?:\/\//.test(server)) {
    console.error(
      "CRITICAL: NTFY_SERVER is missing or not an http(s) URL, so the " +
      "dead-man's switch cannot reach ntfy. Set it in [vars] of " +
      "deploy/wrangler.toml."
    );
  } else {
    try {
      const res = await fetch(server.replace(/\/+$/, "") + "/" + topic, {
        method: "POST",
        headers: {
          "Title": title,
          "Priority": "5",
          "Tags": "warning,skull",
          "Content-Type": "text/plain; charset=utf-8",
        },
        body: body,
      });
      if (!res.ok) {
        console.error(
          "heartbeat alarm POST to ntfy FAILED: HTTP " + res.status +
            " — the owner was NOT paged"
        );
        return { ok: true, stale: stale, unreadable: unreadable, alerted: false,
                 reason: "ntfy HTTP " + res.status };
      }
      console.log("heartbeat alarm sent (ntfy HTTP " + res.status + ")");
      try {
        await env.KV.put(
          HEALTH_ALERT_KEY,
          JSON.stringify({
            ts: now,
            at: new Date(now).toISOString(),
            build: workerBuild(env),
            stale: stale.map((s) => s.key),
          })
        );
      } catch (err) {
        // The alert went out; only the throttle stamp is missing, so the worst
        // case is one repeat page next hour.
        console.log("heartbeat alert stamp write failed:", String(err));
      }
      return { ok: true, stale: stale, unreadable: unreadable, alerted: true };
    } catch (err) {
      console.error(
        "heartbeat alarm POST to ntfy threw (" + String(err) +
          ") — the owner was NOT paged"
      );
      return { ok: true, stale: stale, unreadable: unreadable, alerted: false,
               reason: String(err) };
    }
  }

  // Reached only when the alert could not be sent. Deliberately NOT writing
  // HEALTH_ALERT_KEY: burning the cooldown on a failed send would suppress the
  // page for 6 hours after the secret is fixed.
  return { ok: true, stale: stale, unreadable: unreadable, alerted: false,
           reason: "ntfy not configured" };
}

// The machine-readable liveness answer. This is the endpoint the reverse
// (GitHub Actions) arm polls and the drift canary reads, which is why it is
// JSON with named fields and never a prose status line.
// Did detection ever WAKE the workflow? Reported on ?health=1 because the
// heartbeat above cannot answer it, and that gap is what let a six-week-dead
// chain certify itself healthy: the cron fired, so the heartbeat was fresh, so
// the watchdog printed "both sweep crons are alive" — for a pipeline that had
// never dispatched anything, not once, in its entire history.
//
// The distinction being reported:
//   *_dispatch_ever  false  -> detection has NEVER tripped the gate. Either the
//                              gate is too narrow to match real headlines (the
//                              defect fixed on 2026-10-08) or nothing has
//                              genuinely happened yet. A consumer must decide
//                              which, and it cannot: hence the field.
//   *_dispatch_age_s ignored for alarm purposes — a quiet news week is the
//                              normal state, and alarming on the age of the last
//                              alert would page the owner every day forever.
//                              Reported, never gated.
async function dispatchTelemetry(env, key) {
  // http_status is here because the recorded dispatch already carries it
  // (record.http_status) and the watchdog's failure message asks for it by
  // name. Without this field that message printed "HTTP None" — losing the one
  // value that distinguishes a bad token (401) from a wrong scope (403) from
  // GitHub being down (5xx), which is the entire diagnostic value of the
  // "GH_TOKEN: present is not health" note above.
  const out = { ever: false, age_s: null, ok: null, unreadable: false,
                http_status: null };
  if (!env || !env.KV) {
    out.unreadable = true;
    return out;
  }
  try {
    const rec = parseHeartbeat(await env.KV.get(key));
    if (!rec || !rec.at) return out;
    const at = Date.parse(rec.at);
    if (isNaN(at)) return out;
    out.ever = true;
    out.age_s = Math.max(0, Math.floor((Date.now() - at) / 1000));
    out.ok = rec.ok === undefined ? null : !!rec.ok;
    out.http_status =
      rec.http_status === undefined || rec.http_status === null
        ? null : Number(rec.http_status);
  } catch (err) {
    out.unreadable = true;
    console.log("?health=1 could not read " + key + ":", String(err));
  }
  return out;
}

async function healthForEndpoint(env) {
  const out = {
    build: workerBuild(env),
    // WHICH REPOSITORY THIS DEPLOYMENT ACTUALLY TALKS TO. Stated here because the
    // 2026-10-25 cutover could not be proved from health at all: every dispatch
    // field says an attempt happened, none said where it went. It is a constant,
    // not a probe -- health stays network-free -- and the drift guard in
    // tests/digest/worker_token_expiry.test.js binds it to the URL literals.
    repo: GITHUB_REPO_SLUG,
    // THE DEPLOYED TRIGGERS, AND ONLY THOSE. This used to be the in-source
    // CRON_* constants, which meant it reported SEVEN crons while the Cloudflare
    // API had registered five — and that is precisely why the failed `40 1` /
    // `40 13` trigger writes looked like a success: a liveness check built from
    // in-source intent cannot fail when the deploy half-applies.
    //
    // It is now the one cron the Worker is actually deployed with, and it is
    // labelled as to what it can and cannot prove. THE HONESTY IS THE POINT:
    // see crons_source below.
    crons: [CRON_DISPATCHER],
    // A Worker CANNOT query the Cloudflare API for its own registered triggers.
    // There is no account token in scope, and the Workers runtime cannot
    // self-authorise to the account API, so "what Cloudflare actually holds" is
    // not observable from in here. Saying so is better than a list that reads
    // like an observation and is not one.
    crons_source:
      "the single in-source CRON_DISPATCHER constant, i.e. deploy/wrangler.toml " +
      "[triggers] crons. NOT an observation of Cloudflare's registered " +
      "triggers: a Worker cannot query its own schedules at runtime.",
    crons_verified_at_runtime: false,
    // WHAT DOES VERIFY IT, and it is an observation rather than a declaration:
    // breaking:heartbeat and academic:heartbeat are written ONLY from a tick
    // that decided to run a sweep, so if the one trigger were missing, stopped,
    // or pointed elsewhere, both go stale inside HEARTBEAT_STALE_MS and the
    // dead-man's switch pages. One minute of resolution replaces the old
    // "declared and never checked".
    crons_liveness_verified_by: ["breaking:heartbeat", "academic:heartbeat"],
    // The logical schedule that ONE trigger reproduces, so an operator can read
    // what the single expression is standing in for.
    dispatch_schedule: dispatchSchedule(),
    stale_after_s: Math.floor(HEARTBEAT_STALE_MS / 1000),
    heartbeat_throttle_s: Math.floor(HEARTBEAT_THROTTLE_MS / 1000),
  };
  for (const entry of HEARTBEAT_WATCH) {
    const short = entry.key.split(":")[0];
    try {
      const age = (await readHeartbeatFor(env, entry.key)).ageSeconds;
      out[short + "_heartbeat_age_s"] = age;
      out[short + "_heartbeat_unreadable"] = false;
    } catch (err) {
      out[short + "_heartbeat_age_s"] = null;
      out[short + "_heartbeat_unreadable"] = true;
      console.log("?health=1 could not read " + entry.key + ":", String(err));
    }
  }
  const attempted = await dispatchTelemetry(env, BREAKING_LAST_DISPATCH_KEY);
  const delivered = await dispatchTelemetry(env, BREAKING_LAST_SUCCESS_KEY);
  out.breaking_dispatch_ever = attempted.ever;
  out.breaking_dispatch_age_s = attempted.age_s;
  out.breaking_dispatch_unreadable = attempted.unreadable;
  out.breaking_last_dispatch_ok = attempted.ok;
  out.breaking_dispatch_http_status = attempted.http_status;
  out.breaking_success_ever = delivered.ever;
  out.breaking_success_age_s = delivered.age_s;
  out.breaking_success_unreadable = delivered.unreadable;
  // The digest side. Present from the day this path ships, so ?health=1 can
  // answer "was a digest handed over, and when was one last pushed" without
  // anybody reading KV by hand. The delivery arm itself is still dark while
  // `digest.cron_arm` says dispatchDigest.
  out.digest = await digestHealthFields(env, Date.now());
  // The runtime quota, so the owner can see the freeze state and the month's
  // charge even when Actions is unavailable. Reads the CACHED freeze flag only.
  out.budget = await budgetHealthFields(env, Date.now());
  // The token's own expiry. Reads the CACHED observation only, so ?health=1 never
  // makes a GitHub call and never goes dark when the API is down. `state` is
  // `unknown` when nothing has been observed inside the 6h TTL.
  out.token_expiry = await tokenExpiryHealthFields(env, Date.now());
  return out;
}

async function dispatchAlert(env, candidate, totalTripped) {
  // THE QUOTA GATE, before the repository_dispatch that bills a breaking run.
  const budget = await budgetChargeDispatch(env, "breaking", Date.now());
  if (!budget.allow) {
    console.error(
      "breaking dispatch refused (" + budget.reason + "): the month is at the " +
      "hard ceiling, so the candidate keeps its dedup key unset and stays " +
      "alertable on a later sweep"
    );
    return false;
  }
  let ok = false;
  let httpStatus = 0;
  let err = "";
  try {
    const res = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.GH_TOKEN,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "daily-media-news-breaking-worker",
      },
      body: JSON.stringify({
        event_type: "breaking-alert",
        client_payload: {
          title: candidate.title,
          url: candidate.link,
          source: candidate.source,
          detection_source: candidate.detection_source || "google-news",
          // The freshness caveat travels with the candidate instead of being
          // lost at the parse boundary: a downstream reader can see that this
          // headline carried no <pubDate> and weigh it accordingly. It is a
          // hint, never a decision — the pipeline re-fetches and judges.
          undated: candidate.undated === true,
        },
      }),
    });
    // The token's own expiry rides on every authenticated response, so this
    // dispatch is also the observation. No extra call is made for it.
    await noteTokenExpiry(env, res);
    httpStatus = res.status;
    ok = res.ok;
  } catch (e) {
    // A transport failure is recorded exactly like an HTTP failure, so
    // "last dispatch" can never silently go stale while a network fault
    // keeps every dispatch from landing.
    err = String((e && e.message) || e);
  }
  const record = {
    at: new Date().toISOString(),
    ok: ok,
    http_status: httpStatus,
    err: err,
    title: candidate.title,
  };
  console.log(
    'dispatch "' +
      candidate.title +
      '" (' +
      candidate.source +
      " via " +
      (candidate.detection_source || "google-news") +
      ") -> HTTP " +
      httpStatus +
      " [" +
      totalTripped +
      " tripped candidate(s) this run]" +
      (ok ? "" : " (FAILED" + (httpStatus ? " HTTP " + httpStatus : "") +
             (err ? ": " + err : "") + ")")
  );
  await recordDispatch(env, record);
  return ok;
}

// ONE GitHub workflow_dispatch POST, and the ONLY place this file performs one.
// Two callers reach it -- `dispatchDigest` (the fallback ladder) and
// `dispatchPrebuild` (the punctual prebuild trigger). A. THEY NAME THE SAME
// WORKFLOW NOW: daily-news.yml, distinguished only by the inputs they send,
// `phase: full` against `phase: prebuild`. They stay one function because
// two copies of a dispatch is two things that can be wrong at 07:20, and a second
// one would have to re-learn the 401/403 diagnosis that took real evidence to get
// right. Same GH_TOKEN, same headers, same acceptance test.
//
// WHY IT RETURNS A RESULT OBJECT AND NOT A BOOLEAN -- THE 2026-10-13 ALARM.
// `false` meant FIVE unrelated things: no token, the slot already dispatched,
// HTTP 401, HTTP 403, and any other non-2xx or transport error. The notice the
// owner received on that morning was built from the boolean ALONE and therefore
// named a cause the code had never established. The once-per-slot guard stood
// down -- correctly, by design -- and the message said "no usable GH_TOKEN",
// on a token that had dispatched two workflows hours earlier. The owner spent
// real time believing his credential had died. THE CAUSE IS ESTABLISHED HERE,
// ONCE, at the only place that can establish it, and travels with the boolean.
//
// NO SECRET MATERIAL IS EVER PUT IN THAT RESULT. `summary` is redacted by
// redactDispatchSummary before it is returned, so nothing downstream can store
// a token even by accident.
const DISPATCH_CAUSE_DISPATCHED = "dispatched";
const DISPATCH_CAUSE_TOKEN_MISSING = "token_missing";
const DISPATCH_CAUSE_TOKEN_UNAUTHORISED = "token_unauthorised";
const DISPATCH_CAUSE_TOKEN_FORBIDDEN = "token_forbidden";
const DISPATCH_CAUSE_HTTP_ERROR = "http_error";
const DISPATCH_CAUSE_TRANSPORT_ERROR = "transport_error";

// Where a REJECTED dispatch is recorded, so the next failure is a one-command
// diagnosis instead of an inference from its effects. Cloudflare keeps no past
// Worker console output, and `recordDispatch` covers only the breaking-alert
// arm, so before this key the HTTP status of a failed digest dispatch was
// unrecoverable.
//
// ONLY A DISPATCH THAT WAS ACTUALLY SENT AND ACTUALLY REFUSED IS WRITTEN. A
// success already leaves `actions_dispatch_at` in the delivery ledger, and a
// `token_missing` answer has no HTTP status to record. That also keeps the
// steady-state KV budget in deploy/schedule.json exactly true: the two prebuild
// dispatches a day both succeed, so a healthy day spends ZERO extra writes.
const DIGEST_DISPATCH_STATUS_KEY = "digest:dispatch_status";
// Ninety days. Long enough that "what happened last Tuesday" is answerable,
// short enough that a dead Worker cannot keep this key alive forever. A TTL also
// means Cloudflare charges one expiry delete against the free-tier allowance.
const DIGEST_DISPATCH_STATUS_TTL_S = 90 * 24 * 60 * 60;
const DISPATCH_SUMMARY_MAX_CHARS = 180;

// A response body exists to be diagnosed, so it is redacted BEFORE it is stored
// and not merely before it is printed: the token's own value is matched exactly
// (we hold it, so we can), then anything shaped like a GitHub token, then any
// Authorization header value, then it is whitespace-collapsed and clipped.
const GITHUB_TOKEN_SHAPE =
  /\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b/g;

function redactDispatchSummary(text, secret) {
  let s = text === null || text === undefined ? "" : String(text);
  if (secret) s = s.split(String(secret)).join("<REDACTED>");
  s = s.replace(/(authorization\s*[:=]\s*)(\S+)/gi, "$1<REDACTED>");
  s = s.replace(/bearer\s+\S+/gi, "Bearer <REDACTED>");
  s = s.replace(GITHUB_TOKEN_SHAPE, "<REDACTED>");
  s = s.replace(/\s+/g, " ").trim();
  if (s.length > DISPATCH_SUMMARY_MAX_CHARS) {
    s = s.slice(0, DISPATCH_SUMMARY_MAX_CHARS) + "...";
  }
  return s;
}

async function recordGithubDispatchStatus(env, record) {
  if (!env || !env.KV) return;
  try {
    await env.KV.put(
      DIGEST_DISPATCH_STATUS_KEY,
      JSON.stringify({
        at: new Date().toISOString(),
        workflow: record.workflow,
        ok: false,
        http_status: record.http_status,
        cause: record.cause,
        summary: record.summary || "",
        build: workerBuild(env),
      }),
      { expirationTtl: DIGEST_DISPATCH_STATUS_TTL_S }
    );
  } catch (err) {
    // Telemetry must never be able to break a dispatch. Logged and dropped.
    console.error(
      "github dispatch status could not be persisted: " + String(err)
    );
  }
}

// Returns { ok, http_status, cause, summary } on EVERY path, including the ones
// that never touch the network. A 401/403 means the GH_TOKEN PAT is
// missing/invalid/expired or lacks the required permission, and is logged loudly
// rather than swallowed: a dispatch that silently fails is a digest that is
// silently late.
async function githubWorkflowDispatch(env, url, inputs, label) {
  const workflow = String(url).split("/").filter(Boolean).pop() || "";
  if (!env || !env.GH_TOKEN) {
    console.error(label + " skipped: GH_TOKEN secret missing");
    return {
      ok: false, http_status: 0,
      cause: DISPATCH_CAUSE_TOKEN_MISSING, summary: "",
    };
  }
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.GH_TOKEN,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "daily-media-news-breaking-worker",
      },
      body: JSON.stringify({
        ref: "master",
        inputs: inputs,
      }),
    });
  } catch (err) {
    // WAS AN EXCEPTION, NOT A FALSE. A transport failure used to propagate out
    // of the cron invocation as a thrown error, which reads as a failed job
    // rather than as "the request never arrived", and cost the caller the
    // ability to say which of the five causes it was.
    const summary = redactDispatchSummary(
      String((err && err.message) || err), env.GH_TOKEN);
    console.error(
      label + " did not reach GitHub (" + DISPATCH_CAUSE_TRANSPORT_ERROR +
        "): " + summary
    );
    return {
      ok: false, http_status: 0,
      cause: DISPATCH_CAUSE_TRANSPORT_ERROR, summary: summary,
    };
  }
  // Same observation as the breaking dispatch: this authenticated response
  // carries the token's expiry, so the digest ladder and the prebuild refresh it
  // for free. It runs before the status branches, so a 401/403 is observed too.
  await noteTokenExpiry(env, res);
  if (res.status === 401 || res.status === 403) {
    console.error(
      label + " REJECTED (HTTP " +
        res.status +
        "): TOKEN/ACTIONS PERMISSION ERROR — GH_TOKEN is " +
        (res.status === 401
          ? "missing, invalid or expired"
          : "not allowed to dispatch workflows") +
        "; regenerate the PAT with Repository permissions " +
        "Actions: Read-and-write (plus Contents: Read-and-write) for " +
        "looping-forever/daily-media-news and update the worker secret"
    );
    return {
      ok: false, http_status: res.status,
      cause: res.status === 401
        ? DISPATCH_CAUSE_TOKEN_UNAUTHORISED
        : DISPATCH_CAUSE_TOKEN_FORBIDDEN,
      summary: "HTTP " + res.status,
    };
  }
  if (!res.ok) {
    // NOT a token fault, and the notice says so. Reading "GitHub answered 500"
    // as "my credential died" is the same mistake as cause 2, and it costs the
    // owner the same afternoon.
    let summary = "";
    try {
      summary = redactDispatchSummary(await res.text(), env.GH_TOKEN);
    } catch (err) {
      summary = redactDispatchSummary(
        String((err && err.message) || err), env.GH_TOKEN);
    }
    console.error(
      label + " -> HTTP " + res.status + " (FAILED, not a token fault)" +
        (summary ? ": " + summary : "")
    );
    return {
      ok: false, http_status: res.status,
      cause: DISPATCH_CAUSE_HTTP_ERROR, summary: summary,
    };
  }
  console.log(
    label + " -> HTTP " + res.status + " (workflow_dispatch accepted)"
  );
  return {
    ok: true, http_status: res.status,
    cause: DISPATCH_CAUSE_DISPATCHED, summary: "",
  };
}

// THE PREBUILD TRIGGER'S ONE ACTION. It dispatches daily-news.yml with an
// explicit `digest_time`, `phase: prebuild` and `trigger_source: prebuild`, and
// then RETURNS. (A: this used to dispatch digest-prebuild.yml, which dispatched
// this same build and then watched it for up to 45 minutes; the build now hands
// itself to the seal, so the intermediary and its watch are both gone.)
//
// WHY IT RETURNS RATHER THAN WAITING. A workflow_dispatch is fire-and-forget by
// construction: this Worker has no handle on the dispatched run and no way to
// await it inside the cron invocation. The 40-minute lead is the whole budget for
// that -- measured prebuild duration is 5-9 minutes on the four most recent runs
// and 22 on one, so the slot at +40 has room for anything observed so far.
//
// WHY NOTHING ELSE HAPPENS HERE, WHICH IS THE FAIL-SAFE. No KV read, no KV
// write, no ledger entry, no delivery marker, no `waitUntil` of a second job.
// That is deliberate and it is the property that makes a missed or overrun
// prebuild harmless rather than harmful:
//
//   * a prebuild that FAILS seals nothing, so at +40 the digest cron finds no
//     artifact and takes the fallback ladder -- the owner still gets a push, and
//     `escalateToActions` dispatches a full build, so he gets the real digest ~22
//     minutes late instead of nothing;
//   * a prebuild still RUNNING at +40 has the same outcome, because a half-built
//     artifact is never sealed: the seal job requires the dispatched run to have
//     CONCLUDED success first, so a slow build cannot be delivered as a
//     half-finished page;
//   * a prebuild that dispatches while the slot is being delivered by the digest
//     cron cannot double-push, because the prebuild job never pushes. It only
//     seals; the single push belongs to the digest cron and its fallback ladder,
//     and that path's existing guards (`ntfy_sent_at`, the sequence_id read-back,
//     the once-per-slot `actions_dispatch_at` marker) are untouched by this.
//
// So the worst case of this feature is the behaviour the repository already had
// before it: one push, a little late. It is never "no news".
async function dispatchPrebuild(env, slotName, scheduledMs) {
  const label = "digest prebuild dispatch (" + slotName + ")";
  // THE SLOT'S DATE, TAKEN FROM THE INSTANT THE TICK WAS SCHEDULED FOR AND
  // NEVER FROM Date.now(). The prebuild fires 40 minutes BEFORE the slot it
  // serves, so a date read off the clock is the wrong Nepal day for any
  // invocation that starts after midnight -- the same defect class the digest
  // dispatch path had, fixed the same way.
  //
  // WHEN THE INSTANT IS NOT AVAILABLE THE KEY IS OMITTED, NOT GUESSED.
  // `JSON.stringify` drops `undefined`, so daily-news.yml falls back to dating
  // the slot from its OWN firing -- the same answer, reached the same way, rather
  // than a `Date.now()` that can only ever be approximately right.
  const scheduled = Number(scheduledMs);
  const slotDate = Number.isFinite(scheduled) && scheduled > 0
    ? nptDateFromInstant(scheduled)
    : undefined;
  // THE QUOTA GATE. A prebuild is the FIRST thing shed: it is discretionary (the
  // slot falls back to building at its own cron), so refusing it never costs the
  // owner the digest. Charged against the tick's own instant, not Date.now().
  const gateMs = Number.isFinite(scheduled) && scheduled > 0
    ? scheduled
    : Date.now();
  const budget = await budgetChargeDispatch(env, "prebuild", gateMs);
  if (!budget.allow) {
    console.error(
      "prebuild dispatch refused (" + budget.reason + "): the " + slotName +
      " slot falls back to building at its own cron -- one push ~22 min late, " +
      "never no news"
    );
    return false;
  }
  // `.ok` and not the object: this arm's contract is a boolean, and
  // tests/digest/digest_prebuild_cron.test.js asserts `false` on a missing
  // token and "writes NO kv key" on a refused dispatch -- so the telemetry
  // write deliberately lives in escalateToActions, not in the shared helper.
  // A. `phase` AND `trigger_source` TRAVEL WITH THE SLOT, because this arm now
  // dials the BUILD rather than the orchestrator that used to dial it. The old
  // path passed these four inputs on to the build, and the build branches on
  // `phase` (a prebuild must never push) and records `trigger_source` -- so
  // omitting either would quietly turn a prebuild firing into a delivery-shaped
  // `manual` run.
  const result = await githubWorkflowDispatch(
    env,
    PREBUILD_DISPATCH_URL,
    {
      digest_time: slotName,
      phase: "prebuild",
      slot_date: slotDate,
      trigger_source: "prebuild",
    },
    label);
  const dispatched = !!result.ok;
  if (!dispatched) {
    // Logged, not thrown. A rejected dispatch must not fail the tick in a way
    // that hides it, and it must not be able to affect the delivery slot 40
    // minutes later -- which it cannot, because nothing was recorded.
    console.error(
      label + " was NOT accepted. Nothing is sealed for the " + slotName +
        " slot, so it will fall back to building the digest at its own cron -- " +
        "one push ~22 min late rather than no news at all."
    );
  }
  return dispatched;
}

// Wake daily-news.yml via workflow_dispatch. Returns true on acceptance.
//
// RETAINED, AND NOT ON THE DIGEST CRONS' PATH (U5). The digest crons call
// deliverDigest. This stays because it is the only rollback that needs no new
// reasoning to trust: if ntfy is the thing that broke, re-pointing the two
// branches in scheduled() at this function puts the GitHub build back in
// charge, and it is the only arm here that can build the digest from scratch
// when no artifact was ever sealed. `escalateToActions` is its one caller.
async function dispatchDigest(env, digestTime, slotDate) {
  // The RESULT OBJECT, not `.ok`: the caller's whole job now is to say WHICH of
  // the five causes it is looking at, and that information exists only here.
  return githubWorkflowDispatch(
    env,
    DIGEST_DISPATCH_URL,
    {
      digest_time: digestTime,
      dry_run: "false",
      trigger_source: "worker",
      // The SLOT's Nepal date, supplied by the caller, never the runner's
      // clock. A dispatch accepted at 23:59 NPT can start at 00:01 NPT, and the
      // receiving workflow used to date it off that clock: run
      // 2026-10-17T18:16:53Z pushed the 18th's evening digest 19 hours early
      // that way. `escalateToActions` already holds this date as `today` and
      // used to drop it on the floor here. A caller that cannot supply one
      // passes undefined, which JSON.stringify omits rather than serialising,
      // so the workflow falls back to dating the slot from its own firing.
      //
      // WHICH MEANS THE CALLER'S DATE HAS TO BE HONEST TOO, and on the crons it
      // now is: `today` comes from `deliverDigestPlan`, which anchors it to the
      // TICK's scheduled instant (scheduledMs, threaded through scheduledJobs)
      // rather than to `Date.now`. That fallback survives only where there is no
      // tick to anchor to -- the operator's dev-only ?deliver_now probe and the
      // tests -- so no scheduled delivery reads the clock for its date.
      slot_date: slotDate,
    },
    "digest dispatch (" + digestTime + (slotDate ? " " + slotDate : "") + ")");
}

// ===========================================================================
// DIGEST DELIVERY FROM THE WORKER — the SEND arm (unit U3, ships dark)
// ===========================================================================
//
// WHAT THIS IS. `dispatchDigest` above wakes daily-news.yml, which builds the
// digest, renders the page and pushes it from GitHub Actions. This is the other
// half: the Worker pushes the ALREADY-BUILT digest itself, from the artifact the
// build step POSTs to `/?ingest=1`, at the moment the cron fires.
//
// WHY IT IS A SEPARATE ARM AND NOT A REFACTOR. The GitHub-side push is a build
// job: it renders, it validates, and its schedule is the fallback's schedule. A
// send that has to happen AT the scheduled instant cannot depend on a build
// finishing first, and it must not be able to render or judge anything — see
// the determinism paragraph below.
//
// SHIPPED DARK UNTIL U5. `dispatchDigest` above is RETAINED, deliberately and
// on purpose: it is what made the switch a one-line change, and it is what
// would be re-pointed at if ntfy itself were the thing that broke — the Worker
// could not push at all and the GitHub build could still deliver. Deleting it
// would buy a few lines and cost the only rollback that does not need a new
// deployment to reason about.
//
// THE DELIVERY GUARD — READ THIS BEFORE "SIMPLIFYING" IT.
//
// B3, SETTLED BY TEST RATHER THAN ASSUMPTION: a caller-supplied ntfy message
// `id` does NOT deduplicate. Ten POSTs carrying the same `id` produced ten
// distinct messages, all HTTP 200, in every form (header, URL path, JSON body)
// — the publish struct has no `id` field at all and Go's decoder drops it. So
// "ask ntfy whether we already sent this" is NOT available for the message id,
// and a guard built on that assumption would silently do nothing while looking
// like a guard.
//
// Two things ntfy DOES offer, verified against its own server source:
//
//   1. `sequence_id` is honoured — but only as the `X-Sequence-Id` header
//      (aliases `Sequence-Id`, `sid`), never as a JSON body field, and only
//      after passing `^[-_A-Za-z0-9]{1,64}$`. It is stored with the message and
//      survives in the cache for 43200s (12h).
//   2. The cached set is readable back with
//      `GET <server>/<topic>/json?poll=1&since=all`.
//      (`since=<unix-ts>` filtering was NOT verified against the live server, so
//      `since=all` is used. The cost of that choice is 12 hours of cache — a
//      handful of messages a day on this topic — and it is paid twice a day.)
//
// Hence the two guards, in this order:
//
//   1. WRITE-AHEAD LEDGER. mark `pending` -> push -> mark `sent`. A crash
//      between the two leaves `pending`, which recovery treats as UNKNOWN and
//      re-pushes. The ordering is the whole point: a MISS is worse than a
//      DUPLICATE for a daily digest, and a "mark sent, then push" guard can lose
//      a send outright — silently, which is the one outcome this module cannot
//      tolerate.
//   2. `sequence_id` READ-BACK, immediately before the push. This is the guard
//      that survives a LOST ledger write, which is the one failure the ledger
//      cannot see by definition: two sends whose ledger reads are both stale.
//
// THE HONEST GUARANTEE IS AT-LEAST-ONCE, WITH A POSSIBLE DUPLICATE ON THE
// RECOVERY PATH ONLY. It is not exactly-once and must never be described as
// such. There is no transaction spanning "ntfy has the message" and "KV has the
// record", so a crash in that window can only be resolved by CHOOSING. This
// chooses to re-push, because the alternative is a silent miss. Two pushes are
// visible to a human; no push is invisible.
//
// SECURITY, AND IT IS NOT OPTIONAL. The topic appears in the read-back URL, and
// an ntfy topic is a publish/subscribe capability: anyone holding it can read
// the owner's alert stream AND publish into it. So the topic VALUE must never
// reach a log line, an error message, an exception string or a ledger field.
// Only the env-var NAME is ever printed. `scrubTopic` is the belt-and-braces
// that keeps that true even when a transport error stringifies the URL it was
// given.
//
// NO AI, NO GITHUB API, NO RENDER, NO PAGES CALL. The only egress in this whole
// region is ntfy: one read-back GET and one POST per attempt. That is not a
// style choice. Delivery bookkeeping decides whether the owner is told that
// something happened, and a model verdict must never be able to fake a send or
// suppress one, so everything below is deterministic arithmetic over recorded
// instants. An artifact carrying a `verdict`, `kind`, `delivered`,
// `ntfy_failed` or `ntfy_sent_at` field of its own changes nothing here - those
// are inputs to nothing.

// ---- keys, sizes and budgets --------------------------------------------------
const DIGEST_LEDGER_KEY = "digest:ledger";
const DIGEST_ARTIFACT_PREFIX = "digest:artifact:";
const DIGEST_SCHEMA_VERSION = 1;

// Mirrors of scripts/delivery_ledger.py, which was shipped by unit U2. NOT
// forked values: that module renders the health line FROM this ledger, so a
// second set of numbers would let the page and the Worker's own verdict disagree
// about what "on time" means. ON_TIME_S = 60, RETENTION_DAYS = 7 there.
const DELIVERY_ON_TIME_S = 60;
const DELIVERY_RETENTION_DAYS = 7;

// The two slots, in push order. Mirrors SLOTS in scripts/delivery_ledger.py.
const DIGEST_SLOTS = ["morning", "evening"];

// An artifact older than this is not today's digest however well-formed it is.
// 26h rather than 24h: a build that finishes at 19:50 NPT is still legitimately
// "recent" when the following morning's cron reads it, and a threshold of
// exactly one day would reject that.
const ARTIFACT_MAX_BUILD_AGE_S = 26 * 60 * 60;
// A build stamped MORE than this far in the FUTURE has no usable age, so it is
// refused under the same reason as an unparseable one: the build clock and the
// delivery clock disagree, and every age computed from them would be a fiction.
const ARTIFACT_MAX_CLOCK_SKEW_S = 10 * 60;

// THE ARTIFACT'S OWN EXPIRY, and it is the reason the recorded KV budget in
// deploy/schedule.json can say "2 deletes a day" and mean it.
//
// Without a TTL, `digest:artifact:<date>:<slot>` is a key that is never removed
// — two a day is seven hundred and thirty a year, in the same namespace as the
// delivery ledger. With one, Cloudflare expires each key and charges the
// namespace one DELETE for it, which is the arithmetic the budget records and
// the reason that arithmetic is not a guess.
//
// 28h, and NOT 24h: it must outlast ARTIFACT_MAX_BUILD_AGE_S (26h) so that a
// key's PRESENCE never contradicts the age rule that validates it. A digest
// that is 25h old is still acceptable by `validateArtifact`; if it had already
// been expired the delivery path would take the fallback ladder for a build
// that was perfectly good, and the punctuality record would then describe a
// failure that never happened.
const ARTIFACT_TTL_S = 28 * 60 * 60;

// ntfy's own limits, restated as this module's rejection rules so a bad title is
// caught HERE rather than as an HTTP 400 from the server after three retries.
// NTFY_TITLE_MAX_CHARS is the same 72-character ceiling the GitHub-side push
// applies (daily-news.yml, TITLE_MAX_CHARS), re-asserted on the far side of the
// ingest boundary. Worth knowing: the server compares the title in BYTES
// against a smaller limit than 72, so a 64..72-character title with multi-byte
// characters is still a live publish failure — that is why the artifact builder
// keeps titles far shorter than both ceilings, not exactly at them.
const NTFY_TITLE_MAX_CHARS = 72;
const NTFY_BODY_MAX_BYTES = 4096;

// THE ARCHIVE ORIGIN, spelled ONCE in this file. Both the `page_url` host check
// and the archive-index link in the unavailable push read it, so there is no
// second place to correct the archive subdomain inside the Worker.
//
// It is the same origin `.github/workflows/daily-news.yml` builds `pages_url`
// from and the same host the drift canary probes unauthenticated, and
// tests/digest/digest_delivery.test.js derives the expected value from that
// canary's committed host list — so a rename is a red test, not a push that
// quietly stops linking anywhere. (deploy/schedule.json's `health_url` cannot
// supply this: it is the WORKER's own endpoint on workers.dev, a different host
// from the Pages project, so "derive the archive base from it" is not possible
// and guessing it would have been worse than saying so.)
const DIGEST_ARCHIVE_ORIGIN = "https://daily-digest.example.pages.dev";

// Retry budget for the publish. Three attempts, 8s each, 2s then 6s of backoff:
// 32s of worst-case work inside a 45s budget, so the budget gate below is a
// safety valve against an already-blown clock rather than a routine truncation.
const DELIVERY_ATTEMPTS = 3;
const DELIVERY_ATTEMPT_TIMEOUT_MS = 8000;
const DELIVERY_BACKOFF_MS = [2000, 6000];
const DELIVERY_BUDGET_MS = 45000;

// How much of an ntfy error body a caller that asked for one may read back.
// Bounded because it crosses the wire in a response, and because ntfy's own
// rejection pages are short but need not stay that way.
const NTFY_ERROR_BODY_MAX_CHARS = 400;

// Nepal is UTC+05:45 with no DST, so every conversion here is a fixed shift and
// an exact answer — the same reason NPT_OFFSET exists in
// scripts/delivery_ledger.py and NPT_OFFSET_MINUTES in
// tests/workflows/test_workflow_lint.py.
const DIGEST_NPT_OFFSET_MINUTES = 345;

// The ingest credential. The NAME may appear anywhere; the VALUE never leaves
// the comparison below. Same fail-closed rule as functions/_middleware.js:33-38 —
// an absent secret compares against "" and therefore refuses every request.
const INGEST_TOKEN_HEADER = "X-Ingest-Token";
const INGEST_TOKEN_SECRET = "DIGEST_INGEST_TOKEN";

// The kinds an entry may carry. `delivered` and `unavailable` and
// `duplicate_suppressed` are the same three names scripts/delivery_ledger.py
// renders; `partial` and `fallback_last_good` are the two Worker-only facts
// that module does not model yet.
const KIND_DELIVERED = "delivered";
const KIND_PARTIAL = "partial";
const KIND_FALLBACK_LAST_GOOD = "fallback_last_good";
const KIND_UNAVAILABLE = "unavailable";
const KIND_DUPLICATE_SUPPRESSED = "duplicate_suppressed";
// This arm produced NOTHING for this slot because another arm had already done
// the job. Deliberately NOT `unavailable` (which means "we tried and there was
// nothing") and deliberately NOT `delivered` (which would claim this Worker
// pushed it). Nothing was pushed here, so nothing may claim that it was.
const DELIVERY_KIND_STOOD_DOWN = "stood_down";

// Ledger states. `pending` is the write-ahead marker and is deliberately a
// state of its own rather than an absent entry: "we believe we sent it and
// cannot prove it" and "we never started" are different facts and only the first
// one may be re-pushed.
const STATE_PENDING = "pending";
const STATE_SENT = "sent";
const STATE_FAILED = "failed";

// THE VERIFIED-FAILURE FLAG (U7). `ntfy_failed: true` means exactly one thing:
// `publishToNtfy` exhausted DELIVERY_ATTEMPTS and every attempt came back an
// error. It is deterministic arithmetic over HTTP statuses, never a verdict, and
// it is the ONE field the 07:50 GitHub fallback reads to decide that the
// email/Telegram ladder must run — a slot whose only push was a
// `fallback_last_good` or `unavailable` notice has still written
// `ntfy_sent_at`, so "a push happened" is the wrong question and this flag is
// the right one.
//
// IT IS WRITTEN ON BOTH EDGES, AND THAT IS NOT OPTIONAL. The ledger writer
// merges each patch into the slot's entry with Object.assign, so a success
// that omitted the flag would leave a
// stale `ntfy_failed: true` sitting beside a fresh `ntfy_sent_at` — and
// `ntfy_failed` OUTRANKS `ntfy_sent_at` in the fallback step's ledger verdict,
// so every later run for that slot would email and Telegram the owner a second
// copy of a digest their phone already received.
//
// It is NOT written by the write-ahead `pending` patches: a push in flight is
// not a failure, and recording one would fire the ladder on every re-fire that
// is merely mid-flight. It is NOT written by the duplicate-suppressed arm
// either, which records nothing at all.

// THE SWITCH, AND U5 IS THE UNIT THAT FLIPS IT. This string and the two calls
// in scheduled() are the whole switch, and they are reported by ?health=1 so a
// half-finished flip can never leave a reader guessing which arm is live.
const DIGEST_CRON_ARM = "deliverDigest";

// ---- small helpers -----------------------------------------------------------

// Nepal calendar date (YYYY-MM-DD) for an instant. UTC+05:45, fixed, exact.
function nptDateFromInstant(ms) {
  return new Date(Number(ms) + DIGEST_NPT_OFFSET_MINUTES * 60 * 1000)
    .toISOString()
    .slice(0, 10);
}

function isoUtc(ms) {
  return new Date(Number(ms)).toISOString().replace(/\.\d{3}Z$/, "Z");
}

// The deterministic sequence_id of a digest push. 22 characters for a real
// digest ("dmn-2026-10-09-morning") and 27 for the fallback/unavailable push
// that supersedes it — both inside ntfy's `^[-_A-Za-z0-9]{1,64}$`, which is the
// only reason the read-back guard below can work at all.
function digestSequenceId(date, slot) {
  return "dmn-" + date + "-" + slot;
}

function digestFullSequenceId(date, slot) {
  return digestSequenceId(date, slot) + "-full";
}

// The instant a digest slot was DUE, mirroring `_scheduled_instant` in
// scripts/delivery_ledger.py: cron fields are UTC and both real slots fire
// between 18:15 and 18:14 UTC, so the Nepal date and the UTC date are the same
// calendar day. Punctuality is `first_ntfy_sent_at - scheduled_instant` and
// NEVER `now() - scheduled_instant`: the first two are recorded instants, and
// only a difference of recorded instants can measure a punctual send.
//
// THE PRIMARY CRON, AND ONLY THE PRIMARY CRON — the re-fire is never the anchor.
// If a digest went out at 07:25 because the 07:20 cron started late, it went
// out five minutes late, and `DELIVERY_ON_TIME_S` is 60 seconds. Anchoring on
// the re-fire would score that delivery as perfectly punctual, which is the
// single easiest way for this whole module to start reporting good news about a
// delivery that was late.
function digestScheduledInstant(date, slot) {
  const fields = slot === "evening"
    ? CRON_DIGEST_EVENING.split(" ")
    : CRON_DIGEST_MORNING.split(" ");
  const minute = parseInt(fields[0], 10);
  const hour = parseInt(fields[1], 10);
  return Date.parse(date + "T" + String(hour).padStart(2, "0") + ":" +
                    String(minute).padStart(2, "0") + ":00Z");
}

// Strip the topic VALUE out of anything about to be printed. An error string
// from a transport can contain the URL it was handed, and that URL contains the
// topic; this is what makes "never log the topic" a property of the code rather
// than a property of every call site being careful.
function scrubTopic(text, topic) {
  const s = String(text);
  if (!topic) return s;
  return s.split(String(topic)).join("<NTFY_TOPIC>");
}

// ntfy server and topic, read by NAME. Returns "" for either when unset —
// callers must check, and must never print what they got.
function ntfyServerOf(env) {
  const server = env && env.NTFY_SERVER ? String(env.NTFY_SERVER).trim() : "";
  return server.replace(/\/+$/, "");
}

function ntfyTopicOf(env) {
  return env && env.NTFY_TOPIC ? String(env.NTFY_TOPIC).trim() : "";
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Byte length, not character length: ntfy's 4096 limit is bytes, and a body of
// 4096 emoji is 16384 bytes and is rejected by the server.
function utf8Bytes(text) {
  return new TextEncoder().encode(String(text)).length;
}

// Keep a body inside ntfy's hard limit without ever cutting the honesty
// sentence at the top: the head survives, and the truncation is stated rather
// than hidden behind a shorter notification than the owner was promised.
function clipToNtfyLimit(text) {
  const body = String(text);
  if (utf8Bytes(body) <= NTFY_BODY_MAX_BYTES) return body;
  const notice = "\n\n[truncated to fit the ntfy message limit]";
  const room = NTFY_BODY_MAX_BYTES - utf8Bytes(notice);
  let out = "";
  for (const ch of body) {
    if (utf8Bytes(out + ch) > room) break;
    out += ch;
  }
  return out + notice;
}

// ---- artifact validation -----------------------------------------------------

// validateArtifact(raw, slot, todayNpt, nowMs) -> {ok:true, artifact} |
//                                                   {ok:false, reason, plain}
//
// PURE. No KV, no fetch, no clock of its own (nowMs is passed in and defaults
// to Date.now only for the caller's convenience), and every rejection carries a
// `plain` sentence written for the OWNER rather than for a log — because every
// one of these rejections ends up inside a push he reads on a phone.
//
// The order of the checks is the order of how badly each failure misleads him,
// and `stale_date` is the critical one: an artifact from YESTERDAY is
// well-formed, plausible and completely wrong, and sending it unlabelled is
// indistinguishable from sending today's digest. It is therefore checked before
// the freshness question, and its `plain` sentence says outright that what he is
// looking at is not today's digest.
function validateArtifact(raw, slot, todayNpt, nowMs) {
  const today = String(todayNpt || "");
  const now = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();

  if (raw === null || raw === undefined || raw === "") {
    return {
      ok: false,
      reason: "absent",
      plain: "no digest was built for " + slot + " " + today + ".",
    };
  }

  let parsed;
  try {
    parsed = JSON.parse(String(raw));
  } catch (err) {
    return {
      ok: false,
      reason: "corrupt",
      plain: "the " + slot + " digest record could not be parsed at all, " +
             "so nothing about it can be trusted.",
    };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      ok: false,
      reason: "corrupt",
      plain: "the " + slot + " digest record is not a record, so nothing " +
             "about it can be trusted.",
    };
  }

  if (parsed.schema_version !== DIGEST_SCHEMA_VERSION) {
    return {
      ok: false,
      reason: "schema",
      plain: "the " + slot + " digest record is version " +
             String(parsed.schema_version) + " and this Worker understands " +
             "only version " + DIGEST_SCHEMA_VERSION + ".",
    };
  }

  if (String(parsed.slot || "") !== String(slot)) {
    return {
      ok: false,
      reason: "slot",
      plain: "the digest record filed for " + today + " is the " +
             String(parsed.slot || "(none)") + " one, not " + slot + ".",
    };
  }

  // THE CRITICAL ONE. Yesterday's digest is structurally valid and completely
  // wrong, and nothing downstream could tell the difference.
  if (String(parsed.date || "") !== today) {
    return {
      ok: false,
      reason: "stale_date",
      plain: "the only " + slot + " digest on file is dated " +
             String(parsed.date || "(no date)") + ", NOT today (" + today +
             "). Today's digest has not been built yet — this is NOT today's " +
             "digest.",
    };
  }

  const builtAt = Date.parse(String(parsed.built_at || ""));
  if (!Number.isFinite(builtAt)) {
    return {
      ok: false,
      reason: "stale_build",
      plain: "the " + slot + " digest record for " + today + " carries no " +
             "usable build time, so its age cannot be established.",
    };
  }
  const ageS = (now - builtAt) / 1000;
  if (ageS > ARTIFACT_MAX_BUILD_AGE_S) {
    return {
      ok: false,
      reason: "stale_build",
      plain: "the " + slot + " digest for " + today + " was built " +
             Math.floor(ageS / 3600) + "h ago, which is past the " +
             Math.floor(ARTIFACT_MAX_BUILD_AGE_S / 3600) +
             "h freshness limit.",
    };
  }
  if (-ageS > ARTIFACT_MAX_CLOCK_SKEW_S) {
    return {
      ok: false,
      reason: "stale_build",
      plain: "the " + slot + " digest for " + today + " is stamped " +
             Math.floor(-ageS / 60) + "min in the FUTURE, so the build clock " +
             "and this Worker disagree and its age cannot be trusted.",
    };
  }

  const pageUrl = String(parsed.page_url || "");
  if (!/^https?:\/\//.test(pageUrl)) {
    return {
      ok: false,
      reason: "page",
      plain: "the " + slot + " digest for " + today + " has no usable page " +
             "link, so there is nowhere to send you.",
    };
  }
  let pageHost = "";
  try {
    pageHost = new URL(pageUrl).hostname.toLowerCase();
  } catch (err) {
    pageHost = "";
  }
  if (pageHost !== digestArchiveHost()) {
    return {
      ok: false,
      reason: "page",
      plain: "the " + slot + " digest for " + today + " links to " +
             (pageHost || "an unreadable host") + ", which is not the digest " +
             "archive (" + digestArchiveHost() + ").",
    };
  }

  const ntfy = parsed.ntfy && typeof parsed.ntfy === "object" ? parsed.ntfy : {};
  const title = String(ntfy.title || "");
  if (!title) {
    return {
      ok: false,
      reason: "title",
      plain: "the " + slot + " digest for " + today + " carries no push title.",
    };
  }
  if (title.length > NTFY_TITLE_MAX_CHARS) {
    return {
      ok: false,
      reason: "title",
      plain: "the " + slot + " push title is " + title.length +
             " characters, past the " + NTFY_TITLE_MAX_CHARS +
             "-character limit a phone notification shows.",
    };
  }

  const body = String(ntfy.body || "");
  if (!body) {
    return {
      ok: false,
      reason: "body",
      plain: "the " + slot + " digest for " + today + " carries an empty push body.",
    };
  }
  const bodyBytes = utf8Bytes(body);
  if (bodyBytes > NTFY_BODY_MAX_BYTES) {
    return {
      ok: false,
      reason: "body",
      plain: "the " + slot + " push body is " + bodyBytes + " bytes, past the " +
             NTFY_BODY_MAX_BYTES + "-byte limit.",
    };
  }

  return { ok: true, artifact: parsed };
}

function digestArchiveHost() {
  try {
    return new URL(DIGEST_ARCHIVE_ORIGIN).hostname.toLowerCase();
  } catch (err) {
    return "";
  }
}

function digestArchiveIndexUrl() {
  return DIGEST_ARCHIVE_ORIGIN.replace(/\/+$/, "") + "/";
}

// ---- the ledger --------------------------------------------------------------

// An unreadable or absent ledger is treated as EMPTY, never as a reason to skip
// the send. That is the honest reading — we do not know what was sent — and it
// is precisely why guard 2 exists: with the ledger blind, the sequence_id
// read-back is the only thing standing between a lost write and a double push.
// The two guards are not redundant; each covers the other's blind spot.
function normaliseDigestLedger(raw) {
  if (raw === null || raw === undefined || raw === "") {
    return { schema_version: DIGEST_SCHEMA_VERSION, days: {} };
  }
  let parsed;
  try {
    parsed = JSON.parse(String(raw));
  } catch (err) {
    console.error(
      "digest ledger value unreadable, treating as empty: " + String(err) +
        " — the duplicate guard is now blind and only the sequence_id " +
        "read-back stands between this slot and a double push"
    );
    return { schema_version: DIGEST_SCHEMA_VERSION, days: {} };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    console.error(
      "digest ledger is not a record, treating as empty — the duplicate guard " +
        "is blind for this run"
    );
    return { schema_version: DIGEST_SCHEMA_VERSION, days: {} };
  }
  if (!parsed.days || typeof parsed.days !== "object") parsed.days = {};
  return parsed;
}

function ledgerSlotEntry(ledger, date, slot) {
  const day = ledger && ledger.days ? ledger.days[date] : null;
  const entry = day ? day[slot] : null;
  return entry && typeof entry === "object" && !Array.isArray(entry)
    ? entry
    : null;
}

// The date the retention window closes on. Mirrors `prune` in
// scripts/delivery_ledger.py: anchored on today, never on the wall clock, so the
// same ledger and the same argument always produce the same result.
function ledgerCutoffDate(date) {
  const d = new Date(String(date) + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - (DELIVERY_RETENTION_DAYS - 1));
  return d.toISOString().slice(0, 10);
}

// THE HONESTY INVARIANT, AND IT LIVES HERE BECAUSE THIS IS THE ONLY PLACE.
//
// A ledger entry is the record the owner reads when he asks "did I get the
// evening digest?". On 2026-10-14 and 2026-10-15 that record said:
//
//     state=sent  kind=delivered  ntfy_failed=true  http_status=429
//     ntfy_quota_exhausted=true  error=TimeoutError
//
// which is a slot that did not arrive, recorded as a slot that did. The
// escalation at actions_dispatch_at had already been dispatched, so the owner
// was told he had two evening digests he never received.
//
// WHY IT WAS POSSIBLE. The entry is a MERGE (`Object.assign` onto the existing
// entry), and every arm writes its own patch: the real digest, the sequence_id
// read-back dedup, the fallback ladder, the unavailable notice. Each of them
// remembered to write `ntfy_failed` on both edges, and the merge could still
// interleave a `sent` from one arm with a `true` from another, because the
// merge is what produces the contradiction and no single call site ever sees
// both halves.
//
// SO THE RULE IS ENFORCED ON THE MERGED RESULT, AT THE ONE FUNCTION EVERY
// PATCH MUST PASS THROUGH, rather than being remembered at each of the four
// call sites. There is no other write path: every arm reaches KV through the
// ledger writer below, so a call site added later cannot opt out of this by
// forgetting.
//
// TWO DIRECTIONS, BOTH HONESTY:
//   1. `ntfy_failed: true` can never coexist with `state: sent` or
//      `kind: delivered`. The verified failure outranks the success claim, and
//      the slot is downgraded to `failed`. This is recoverable in the right
//      direction: if the digest really is on the topic, the sequence_id
//      read-back on a later run proves it and writes `sent` again.
//   2. A success CLEARS the failure metadata it outranks. Otherwise a slot that
//      genuinely delivered at 13:55 keeps `ntfy_quota_exhausted: true` and
//      `error: "ntfy HTTP 429"` from the 13:35 attempt forever — which is what
//      2026-10-12 and 2026-10-13 still show on master, and it sends the owner
//      looking for an outage that ended.
//
// LOUD, NOT QUIET. A downgrade is a real defect in a delivery record, so it
// says so on stderr in the words the owner would use, and it leaves the slot
// visibly undelivered rather than softening it into a warning that scrolls
// past. This repo's rule is that honesty signals stay loud.
function enforceLedgerHonesty(entry, update) {
  const out = entry;
  const failed = out.ntfy_failed === true;
  const claimsSent = out.state === STATE_SENT || out.kind === KIND_DELIVERED;

  if (failed && claimsSent) {
    console.error(
      "LEDGER HONESTY VIOLATION CORRECTED for " + String(update.date) + " " +
        String(update.slot) + ": this slot recorded a VERIFIED ntfy failure " +
        "(ntfy_failed=true, http_status=" + String(out.http_status) +
        ", attempts=" + String(out.attempts) + ", error=" +
        String(out.error || "none") + ") alongside a delivery claim " +
        "(state=" + String(out.state) + ", kind=" + String(out.kind) + "). " +
        "The owner did NOT receive this digest. Downgrading the record to " +
        STATE_FAILED + " so the slot reads as undelivered. This is the 2026-10-14 " +
        "evening shape: a ledger that claims delivery the phone never showed."
    );
    out.state = STATE_FAILED;
    out.ntfy_failed = true;
    // `kind` is DELIBERATELY LEFT ALONE. It classifies what was BUILT — the
    // digest really did build, and really was a full one — so rewriting it to
    // `unavailable` would mean "we tried and there was nothing", which is a NEW
    // lie in place of the old one. `state` is the delivery verdict and `state`
    // is what this corrects.
    //
    // The push instants ARE removed. `ntfy_sent_at` is documented as "a push
    // happened", and leaving it beside a failed state is the same self-
    // contradiction in a different field. The failure metadata (attempts,
    // http_status, error, quota) is kept in full: that is the diagnosis.
    delete out.ntfy_sent_at;
    delete out.first_ntfy_sent_at;
    out.deduped_via = null;
    out.honesty_downgraded_at = isoUtc(Date.now());
    return out;
  }

  if (!failed && out.state === STATE_SENT) {
    // Direction 2: a real success outranks the failure metadata of every
    // earlier attempt for this same slot.
    if (out.ntfy_quota_exhausted) delete out.ntfy_quota_exhausted;
    if (out.error) delete out.error;
    if (out.honesty_downgraded_at) delete out.honesty_downgraded_at;
  }
  return out;
}

// READ-MODIFY-WRITE. The record is re-read before it is written, because this
// function is called twice per delivery (pending, then sent) and because the
// clock may have moved on: writing back the object we read at the top of the run
// would drop whatever else was recorded in between.
//
// Never throws. A ledger write that fails costs the punctuality record, which is
// exactly the state the sequence_id read-back is there to cover, so it is logged
// loudly and swallowed rather than propagated into the cron.
async function commitDigestLedger(env, update, nowMs) {
  if (!env || !env.KV) return false;
  let raw = null;
  try {
    raw = await env.KV.get(DIGEST_LEDGER_KEY);
  } catch (err) {
    console.error(
      "digest ledger re-read FAILED (" + scrubTopic(String(err), ntfyTopicOf(env)) +
        ") — this delivery will not be recorded, so treat the next run for " +
        "this slot as the first one"
    );
    return false;
  }
  const ledger = normaliseDigestLedger(raw);
  const date = String(update.date);
  const day = ledger.days[date] = ledger.days[date] || {};
  const merged = Object.assign(
    {}, day[String(update.slot)] || {}, update.patch || {});
  day[String(update.slot)] = enforceLedgerHonesty(merged, update);
  if (update.last_good) ledger.last_good = update.last_good;
  ledger.schema_version = DIGEST_SCHEMA_VERSION;
  ledger.as_of = isoUtc(nowMs);
  const cutoff = ledgerCutoffDate(date);
  ledger.pruned_before = cutoff;
  const days = ledger.days;
  for (const key of Object.keys(days)) {
    if (String(key) < cutoff) delete days[key];
  }
  try {
    await env.KV.put(DIGEST_LEDGER_KEY, JSON.stringify(ledger));
    return true;
  } catch (err) {
    console.error(
      "digest ledger WRITE FAILED (" +
        scrubTopic(String(err), ntfyTopicOf(env)) +
        ") — the send may still have happened; the next run for this slot " +
        "falls back to the sequence_id read-back"
    );
    return false;
  }
}

// ---- ntfy: read-back dedup, then publish -------------------------------------

// Parse the body of `GET <server>/<topic>/json?poll=1&since=all`.
//
// WHY THIS EXISTS AT ALL, AND WHY IT IS THE WHOLE FIX: ntfy answers that
// request with `Content-Type: application/x-ndjson; charset=utf-8` —
// NEWLINE-delimited JSON, one complete message object per line, NOT a JSON
// array. And on a topic with nothing in it the body is **zero bytes**.
// Measured 2026-10-13 against ntfy.sh with a generated, non-existent topic:
// `HTTP 200`, `ctype=application/x-ndjson; charset=utf-8`, `bodybytes=0`.
//
// So `JSON.parse(await res.text())` threw on EVERY call, for every topic, at
// every time of day: a non-empty topic is many objects separated by newlines,
// which is not valid JSON, and an empty topic is `""`, which is not valid JSON
// either. Both landed in the catch below, which returns `unknown`, whose one
// consequence is "push anyway". This guard has never once suppressed anything.
//
// Three shapes are accepted, in this order:
//   1. NDJSON — one JSON value per non-blank line. What ntfy actually sends.
//   2. A single JSON array spanning several lines. Accepted so a proxy or a
//      future server-side tidy that pretty-prints one document cannot turn the
//      guard blind again for the same reason it was blind today.
//   3. `{ "messages": [...] }`. Accepted for the same reason, and it is what
//      several ntfy-compatible servers return for the identical query.
//
// AN EMPTY BODY IS NOT AN ERROR. It is the server's answer for "this topic has
// no cached messages", which is the most common answer of all — the ordinary
// first delivery of the day. Throwing on it would report "unknown" for the one
// case where we know the answer exactly, and unknown means push.
function parseNtfyHistory(text) {
  const body = String(text == null ? "" : text).trim();
  if (!body) return [];
  // 1. NDJSON. EVERY non-blank line must parse AND every one must be a message
  //    object, or this is not the shape we were promised and we fall through
  //    rather than silently keeping the lines that happened to work -- a partial
  //    parse finds HALF the messages on the topic, which is a dedup guard that
  //    suppresses the wrong things and looks like it works.
  //
  //    `{"messages": [...]}` on its own line is a CONTAINER, not a message. It
  //    is valid JSON and a valid object, so a naive "is it an object?" test
  //    accepts it as one message that happens to carry no sequence_id -- and
  //    then a topic whose history really does contain the sequence id reads as
  //    one unrelated message. Shape 3 below exists precisely so that container
  //    is unwrapped, and it can only be reached if it is recognised here.
  const lines = body.split("\n").map((l) => l.trim()).filter(Boolean);
  const ndjson = [];
  let everyLineIsAMessage = true;
  for (const line of lines) {
    let value = null;
    try {
      value = JSON.parse(line);
    } catch (err) {
      everyLineIsAMessage = false;
      break;
    }
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        Array.isArray(value.messages)) {
      everyLineIsAMessage = false;
      break;
    }
    ndjson.push(value);
  }
  if (everyLineIsAMessage && ndjson.length === lines.length) return ndjson;
  // 2 + 3. One document for the whole body.
  const parsed = JSON.parse(body);
  if (Array.isArray(parsed)) return parsed.filter((m) => m && typeof m === "object");
  if (parsed && Array.isArray(parsed.messages)) {
    return parsed.messages.filter((m) => m && typeof m === "object");
  }
  return [];
}

// Read the cached message set back and look for one sequence_id.
//
// `since=all` and NOT `since=<unix-ts>`: the timestamp form was not verified
// against the live server, and a dedup guard that depends on an unverified query
// parameter is a guard that can silently return an empty set. The cost of
// `since=all` is 12 hours of cache (ntfy keeps 43200s) on a topic that receives
// a handful of messages a day — two read-backs and two pushes — so it is cheap
// and it is bounded.
//
// The URL contains the topic, so it is never logged, never returned and never
// stored. A transport failure is reported as UNKNOWN rather than as "not
// present", and the caller pushes: a push always happens.
async function findDeliveredBySequenceId(env, sequenceId) {
  const topic = ntfyTopicOf(env);
  const server = ntfyServerOf(env);
  if (!topic || !/^https?:\/\//.test(server)) {
    return { found: false, unknown: true, reason: "ntfy not configured" };
  }
  let messages = [];
  try {
    const res = await fetch(server + "/" + topic + "/json?poll=1&since=all", {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(DELIVERY_ATTEMPT_TIMEOUT_MS),
    });
    if (!res.ok) {
      console.error(
        "digest sequence_id read-back returned HTTP " + res.status +
          " (topic value deliberately not logged) — treating the push as " +
          "unverified and sending it, because a push always happens"
      );
      return { found: false, unknown: true, reason: "read-back HTTP " + res.status };
    }
    const parsed = parseNtfyHistory(await res.text());
    messages = Array.isArray(parsed) ? parsed : [];
  } catch (err) {
    console.error(
      "digest sequence_id read-back FAILED (" +
        scrubTopic(String(err), topic) +
        ") — treating the push as unverified and sending it, because a push " +
        "always happens"
    );
    return { found: false, unknown: true, reason: "read-back failed" };
  }
  for (const message of messages) {
    if (message && String(message.sequence_id || "") === String(sequenceId)) {
      return {
        found: true,
        unknown: false,
        id: String(message.id || ""),
        time: Number(message.time) || 0,
        cached: messages.length,
      };
    }
  }
  return { found: false, unknown: false, cached: messages.length };
}

// POST one payload to ntfy, with retries under a wall-clock budget.
//
// Retries because a transient 5xx or a dropped socket must not cost a day's
// digest, and a bounded retry loop with a hard deadline because an unbounded one
// would hold the cron invocation open. The gate is arithmetic and it is checked
// BEFORE every attempt including the first: an attempt N+1 never begins when the
// elapsed time plus the pending backoff plus a whole attempt would overrun the
// budget.
async function publishToNtfy(env, payload, sequenceId, label, options) {
  const opts = options || {};
  const now = opts.now || Date.now;
  const sleep = opts.sleep || sleepMs;
  const topic = ntfyTopicOf(env);
  const server = ntfyServerOf(env);

  if (!topic) {
    console.error(
      "CRITICAL: the NTFY_TOPIC secret is not set on this Worker, so the " +
        label + " CANNOT be pushed — the owner will be told nothing by this arm"
    );
    return { ok: false, attempts: 0, http_status: 0, error: "ntfy topic missing",
             error_body: "" };
  }
  if (!/^https?:\/\//.test(server)) {
    console.error(
      "CRITICAL: NTFY_SERVER is missing or not an http(s) URL, so " + label +
        " cannot reach ntfy. Set it in [vars] of deploy/wrangler.toml."
    );
    return { ok: false, attempts: 0, http_status: 0, error: "ntfy server missing",
             error_body: "" };
  }

  const startedAt = now();
  const elapsed = () => Math.max(0, now() - startedAt);
  let attempts = 0;
  let httpStatus = 0;
  let messageId = "";
  let lastError = "";
  let lastErrorBody = "";

  for (let attempt = 1; attempt <= DELIVERY_ATTEMPTS; attempt += 1) {
    const backoff = attempt === 1 ? 0 : (DELIVERY_BACKOFF_MS[attempt - 2] || 0);
    if (elapsed() + backoff + DELIVERY_ATTEMPT_TIMEOUT_MS > DELIVERY_BUDGET_MS) {
      console.error(
        label + ": NOT starting attempt " + attempt + " — " + elapsed() +
          "ms of the " + DELIVERY_BUDGET_MS + "ms budget is already spent and " +
          "an attempt may take " + DELIVERY_ATTEMPT_TIMEOUT_MS + "ms plus a " +
          backoff + "ms backoff"
      );
      break;
    }
    if (backoff) await sleep(backoff);
    attempts = attempt;
    try {
      const res = await fetch(server + "/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          // THE HEADER IS THE WHOLE POINT. ntfy reads `sequence_id` from
          // `X-Sequence-Id` (aliases `Sequence-Id`, `sid`) and nowhere else; a
          // `sequence_id` key in this JSON body would be dropped exactly like the
          // `id` key was, and the read-back guard above would then never fire.
          "X-Sequence-Id": sequenceId,
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(DELIVERY_ATTEMPT_TIMEOUT_MS),
      });
      httpStatus = res.status;
      if (res.ok) {
        try {
          const parsed = JSON.parse(await res.text());
          messageId = parsed && parsed.id ? String(parsed.id) : "";
        } catch (err) {
          messageId = "";
        }
        console.log(
          label + ": pushed on attempt " + attempt + " (ntfy HTTP " +
            httpStatus + ", sequence_id " + sequenceId + ")"
        );
        return {
          ok: true,
          attempts: attempts,
          http_status: httpStatus,
          message_id: messageId,
          error: "",
          error_body: "",
        };
      }
      lastError = "ntfy HTTP " + res.status;
      // OPT-IN, and off for every real delivery because `options.captureErrorBody`
      // is undefined on every existing call site. It exists because a bare status
      // is not diagnosable: ntfy says WHICH rejection it was in this body, and
      // `?deliver_test=1` is the one caller that must be able to read it back.
      // Scrubbed before it is stored, because ntfy quotes the topic it was asked
      // about and the topic value must never reach a record or a log line.
      if (opts.captureErrorBody) {
        try {
          lastErrorBody = scrubTopic(String(await res.text()), topic)
            .slice(0, NTFY_ERROR_BODY_MAX_CHARS);
          lastError += ": " + lastErrorBody;
        } catch (bodyErr) {
          lastErrorBody = "(error body unreadable: " +
            scrubTopic(String(bodyErr), topic) + ")";
          lastError += ": " + lastErrorBody;
        }
      }
      console.error(
        label + ": attempt " + attempt + " FAILED (" + lastError + ")"
      );
      // A DAILY-QUOTA REFUSAL ENDS THIS SLOT'S PUSHING. DO NOT RETRY IT.
      //
      // ntfy answers HTTP 429 / code 42908 when the VISITOR's daily message
      // allowance is spent, and that allowance is per visitor, per UTC day. A
      // second and third attempt from the same Worker are therefore not a
      // retry of a transient fault -- they are three more messages charged
      // against an allowance that is already empty, bought at 2s and 6s of
      // backoff each.
      //
      // MEASURED COST, and this is why it mattered: on 2026-10-12, 10-13 and
      // 10-14 the evening slots all ended `ntfy_quota_exhausted: true`. The
      // escalation that could actually have recovered the slot -- the GitHub
      // Actions arm, which pushes from a DIFFERENT IP and therefore holds a
      // SEPARATE allowance -- was only reached after the local quota had been
      // spent three more times for nothing.
      //
      // WHAT THIS DOES NOT TOUCH. The topic, the payload shape, the wire
      // format and DELIVERY_ATTEMPTS itself are all unchanged, because every
      // one of those is known-good and chasing a third party's quota by
      // breaking a working path has already been ruled out. Only the retry of
      // THIS one terminal status is cut. A 5xx or a timeout still retries the
      // full three attempts, because those really are transient.
      //
      // THE FAIL-SAFE IS NOT WEAKENED. `attempts` keeps the real count, the
      // caller still records `state: failed` + `ntfy_failed: true`, and the
      // escalation still fires -- it just fires sooner, with quota left on the
      // table for the recovery arm.
      if (httpStatus === 429) {
        console.error(
          label + ": HTTP 429 is ntfy's DAILY message quota for this visitor " +
            "being spent, so attempts " + (attempt + 1) + "-" + DELIVERY_ATTEMPTS +
            " are NOT being made: they would be charged against an allowance " +
            "that is already empty and would only delay the escalation to the " +
            "Actions arm, which holds its own separate quota. This slot will " +
            "be recorded as NOT delivered and escalated now."
        );
        break;
      }
    } catch (err) {
      lastError = scrubTopic(String(err), topic);
      console.error(
        label + ": attempt " + attempt + " threw (" + lastError + ")"
      );
    }
  }

  return {
    ok: false,
    attempts: attempts,
    http_status: httpStatus,
    message_id: "",
    // THE QUOTA MARKER, AND IT IS A STATUS CHECK NOT A BODY CHECK. ntfy says
    // `429` / code `42908` "daily message quota reached" when the visitor's
    // daily message allowance is spent. The body is only captured when
    // `options.captureErrorBody` is set, and it is not on any real delivery
    // call site, so the status is the only signal available when the decision
    // that matters is made. A distinct field rather than a bare http_status,
    // because quota exhaustion and a 5xx ask the owner for opposite things:
    // wait for midnight UTC, versus go read a log.
    quota_exhausted: httpStatus === 429,
    error: lastError || "no attempt made inside the delivery budget",
    error_body: lastErrorBody,
  };
}

// ---- push payloads -----------------------------------------------------------

// ntfy takes `Priority` as an int and `Tags` as an array of strings, and
// answers a JSON STRING in either field with HTTP 400 / code 40024.
// MEASURED on 2026-10-12 against a throwaway topic, replicating this exact
// wire shape (POST <server>/ with `topic` in the body):
//   {"priority":"4","tags":"newspaper"}   -> HTTP 400
//   {"priority":4,  "tags":"newspaper"}   -> HTTP 400   (tags alone is enough)
//   {"priority":"4","tags":["newspaper"]} -> HTTP 400   (priority alone is enough)
//   {"priority":4,  "tags":["newspaper"]} -> HTTP 200
// So BOTH fields are coerced here, ONCE, at the one function every digest
// push is built by -- rather than at four call sites that each got it wrong.
// `Number(...)` on a real number is identity and on a numeric string is the
// number; a comma-separated tags string becomes an array in the written order.
function digestPayload(topic, title, body, pageUrl, priority, tags) {
  const payload = {
    topic: topic,
    title: title,
    message: clipToNtfyLimit(body),
    markdown: true,
    priority: Number(priority),
    tags: Array.isArray(tags) ? tags : String(tags).split(",")
      .map((t) => t.trim()).filter(Boolean),
  };
  if (pageUrl) {
    payload.click = pageUrl;
    payload.actions = [
      { action: "view", label: "Open the digest", url: pageUrl },
    ];
  }
  return payload;
}

function slotLabel(slot) {
  return slot === "evening" ? "Evening" : "Morning";
}

// ---- THE DELIVERY ROUTINE ----------------------------------------------------

// deliverDigestPlan(env, slot, writer, options)
//
// Everything the delivery path does EXCEPT the ledger write, with the ledger
// write injected as `writer`. That injection is the whole reason a manual probe
// cannot mask a real double-push: `?deliver_now=1` passes null and therefore
// reaches no code that can write `digest:ledger`, rather than passing a flag
// that a later edit could flip. The cron arm below passes the real writer, and
// it is the ONLY caller that does.
//
// A push ALWAYS happens on every path out of here, including every failure. The
// ladder, in order:
//
//   valid artifact .................. push it (kind: delivered | partial)
//   no artifact / stale / invalid,
//     and a last_good digest exists .. push THAT, clearly labelled as older
//                                      (kind: fallback_last_good)
//   no artifact and no last_good ..... push "digest unavailable" anyway
//                                      (kind: unavailable)
//   the ledger itself is unreadable .. push "digest unavailable" naming the KV
//                                      failure and write NOTHING to the ledger
//
// Every one of those records an `ntfy_sent_at`, so "a push happened" is a fact
// rather than an inference, and none of them is silent.
//
// An `is_partial: true` artifact IS sent. The honesty banner already leads its
// body, so an incomplete digest that says so is strictly more useful to the owner
// than silence about a day that was only partly collected.
async function deliverDigestPlan(env, slot, writer, options) {
  const opts = options || {};
  const now = opts.now || Date.now;
  const startedAt = now();
  const slotName = String(slot || "");
  // THE DATE COMES FROM THE TICK, NOT FROM THE CLOCK, WHENEVER A TICK IS KNOWN.
  // The scheduled instant is what the cron fired FOR; `Date.now` is only when the
  // isolate happened to run, and a tick delayed past NPT midnight (18:15 UTC)
  // would otherwise date the digest to the NEXT day and push it a day early --
  // the same class of defect as run 2026-10-17T18:16:53Z, one hop further up this
  // chain. `startedAt` still governs the RECORDED instants, which must say when
  // the send really happened; only the slot's date is anchored.
  const today = nptDateFromInstant(
    opts.scheduledMs != null ? opts.scheduledMs : startedAt);
  const label = "digest delivery (" + slotName + " " + today + ")";
  const topic = ntfyTopicOf(env);

  if (!env || !env.KV) {
    console.error(label + ": no KV binding, nothing can be read and nothing sent");
    return {
      ok: false, kind: KIND_UNAVAILABLE, date: today, slot: slotName,
      pushed: false, reason: "no KV binding",
    };
  }
  if (DIGEST_SLOTS.indexOf(slotName) < 0) {
    console.error(label + ": unknown slot, refusing to send");
    return {
      ok: false, kind: KIND_UNAVAILABLE, date: today, slot: slotName,
      pushed: false, reason: "unknown slot",
    };
  }

  // 1. THE LEDGER. A read failure is caught, logged by secret NAME only, and
  //    answered with a push: writing a ledger we could not read would destroy
  //    the punctuality record this whole module exists to keep. Escalation is
  //    preserved by the arms outside the Worker — the re-fire and the 07:50
  //    GitHub fallback both still run.
  let ledgerRaw = null;
  try {
    ledgerRaw = await env.KV.get(DIGEST_LEDGER_KEY);
  } catch (err) {
    console.error(
      label + ": the delivery ledger could not be READ (" +
        scrubTopic(String(err), topic) + ") — sending the unavailable push and " +
        "writing NOTHING to the ledger; a ledger written from a failed read " +
        "would erase the punctuality record"
    );
    const un = unavailableOutcome(topic, today, slotName, opts);
    await pushUnrecorded(env, un, label, opts);
    return un;
  }

  const ledger = normaliseDigestLedger(ledgerRaw);
  const sequenceId = digestSequenceId(today, slotName);

  // 2. THE LEDGER GUARD. `ntfy_sent_at` set means this slot already produced a
  //    push in this ledger's own history.
  const previous = ledgerSlotEntry(ledger, today, slotName);
  if (previous && previous.ntfy_sent_at) {
    console.log(
      label + ": " + KIND_DUPLICATE_SUPPRESSED + " — the ledger already records " +
        String(previous.kind || "a push") + " sent at " +
        String(previous.ntfy_sent_at) + " (sequence_id " +
        String(previous.sequence_id || sequenceId) + ")"
    );
    return {
      ok: true, kind: KIND_DUPLICATE_SUPPRESSED, date: today, slot: slotName,
      pushed: false, sequence_id: String(previous.sequence_id || sequenceId),
      ledger_patch: null, reason: "ledger already records a push",
    };
  }

  // 2b. THE QUOTA GUARD, AND IT IS THE SECOND HALF OF THE 2026-10-12/13/14
  //     EVENING FIX. The guard above stops a SECOND push once one is recorded.
  //     This one stops a re-fire that could not possibly succeed.
  //
  //     ntfy's message quota is per VISITOR and per UTC DAY. Once this ledger
  //     records `ntfy_quota_exhausted` for this slot, the allowance behind this
  //     Worker's IP is spent until midnight UTC. Every later re-fire of the same
  //     slot on the same day therefore POSTs into a wall, and each of those
  //     POSTs is itself a charge against that same spent allowance. The evening
  //     slots are where this shows: the morning slot lands 24s after its
  //     schedule with quota to spare, and the evening slot arrives after the
  //     day's messages have been spent.
  //
  //     So a re-fire that finds the quota already exhausted does NOT re-push.
  //     It re-asserts the same honest FAILED record and escalates, which is the
  //     only remaining path that can actually deliver -- the Actions arm, from a
  //     different IP, holding a different allowance.
  //
  //     WHAT THIS DOES NOT WEAKEN. The slot is not marked delivered, not
  //     suppressed, and not quietly dropped: it stays `failed` +
  //     `ntfy_failed: true`, so the watchdog still demands it and the owner
  //     still gets the email. What stops is spending a spent quota on a request
  //     that cannot succeed. `ntfy_quota_exhausted` is deliberately NOT cleared
  //     by a later success for this reason -- see enforceLedgerHonesty, which
  //     drops it only when a push genuinely landed.
  const priorEntry = ledgerSlotEntry(ledger, today, slotName);
  if (priorEntry && priorEntry.ntfy_quota_exhausted === true
      && !priorEntry.ntfy_sent_at) {
    console.error(
      label + ": SKIPPING the re-push. The ledger already records ntfy's DAILY " +
        "message quota for this slot as exhausted (recorded " +
        String(priorEntry.http_status || "429") + " at " +
        String(priorEntry.honesty_downgraded_at || "an earlier run") + "), and that " +
        "allowance is per visitor and per UTC day -- a re-push now cannot succeed " +
        "and would only spend quota that is already gone. The slot stays recorded " +
        "as NOT delivered. Recovery is the escalation to the Actions arm, which " +
        "pushes from a different IP and holds its own quota."
    );
    return {
      ok: false, kind: KIND_UNAVAILABLE, date: today, slot: slotName,
      pushed: false, sequence_id: sequenceId,
      quota_exhausted: true,
      http_status: Number(priorEntry.http_status || 429),
      reason: "ntfy daily quota already exhausted for this slot today",
    };
  }

  // 3. THE ARTIFACT.
  const artifactKey = DIGEST_ARTIFACT_PREFIX + today + ":" + slotName;
  let artifactRaw = null;
  try {
    artifactRaw = await env.KV.get(artifactKey);
  } catch (err) {
    console.error(
      label + ": the artifact could not be READ from " + artifactKey + " (" +
        scrubTopic(String(err), topic) + ")"
    );
    const un = unavailableOutcome(topic, today, slotName, opts,
                                  "the digest record for this slot could not " +
                                  "be read.");
    await pushUnrecorded(env, un, label, opts);
    return un;
  }

  const verdict = validateArtifact(artifactRaw, slotName, today, startedAt);
  if (!verdict.ok) {
    console.error(label + ": artifact REJECTED (" + verdict.reason + ") — " + verdict.plain);
    return await pushFallbackLadder(env, ledger, today, slotName, verdict.reason,
                                   verdict.plain, writer, opts, label, startedAt);
  }

  // 4. THE REAL DIGEST. Write-ahead FIRST: `pending` on disk before a byte goes
  //    to ntfy. A crash from here leaves `pending`, which the next run treats as
  //    unknown and re-pushes — a possible duplicate, never a silent miss.
  const artifact = verdict.artifact;
  const kind = artifact.is_partial === true ? KIND_PARTIAL : KIND_DELIVERED;
  const scheduledInstant = digestScheduledInstant(today, slotName);
  if (writer) {
    await writer({
      date: today, slot: slotName,
      patch: {
        state: STATE_PENDING,
        kind: kind,
        sequence_id: sequenceId,
        pending_at: isoUtc(startedAt),
        scheduled_instant: isoUtc(scheduledInstant),
        build: workerBuild(env),
      },
    }, startedAt);
  }

  // 5. THE SEQUENCE_ID READ-BACK. This is the guard that survives a lost ledger
  //    write, so it runs even when the ledger was perfectly readable.
  const seen = await findDeliveredBySequenceId(env, sequenceId);
  if (seen.found) {
    const sentAt = seen.time
      ? isoUtc(seen.time * 1000)
      : isoUtc(startedAt);
    const record = {
      state: STATE_SENT,
      kind: kind,
      sequence_id: sequenceId,
      // Cleared, not omitted: see the ntfy_failed note above. The read-back
      // proved the digest IS on the topic, so any earlier failure for this slot
      // is over.
      ntfy_failed: false,
      ntfy_sent_at: sentAt,
      first_ntfy_sent_at: sentAt,
      scheduled_instant: isoUtc(scheduledInstant),
      deduped_via: "sequence_id read-back",
      build: workerBuild(env),
    };
    if (writer) await writer({ date: today, slot: slotName, patch: record }, startedAt);
    console.log(
      label + ": push SKIPPED — the sequence_id read-back found this digest " +
        "already on the topic (" + seen.cached + " cached message(s) checked). " +
        "The ledger that should have said so is the one that lost the write."
    );
    return {
      ok: true, kind: kind, date: today, slot: slotName, pushed: false,
      deduped: true, sequence_id: sequenceId, ntfy_sent_at: sentAt,
      delta_s: deliveryDeltaSeconds(sentAt, scheduledInstant),
      ledger_patch: null, reason: "sequence_id already present",
    };
  }

  // 6. PUSH.
  const payload = digestPayload(
    topic,
    String(artifact.ntfy.title),
    String(artifact.ntfy.body),
    String(artifact.page_url),
    "4",
    // `digest` FIRST. This push was priority 4 and `newspaper`, which a
    // non-CRITICAL breaking alert and the RESULT-path self-check warning also
    // used, so on a collapsed notification the owner could not tell a routine
    // digest from an alert. A literal array, never a comma string: ntfy
    // answers a string `Tags` with HTTP 400 (see digestPayload).
    ["digest", "newspaper"]
  );
  const published = await publishToNtfy(env, payload, sequenceId, label, opts);

  if (!published.ok) {
    // A VERIFIED FAILURE ESCALATES. This is the rung that was missing: the
    // fallback ladder's dispatch fires when there is no ARTIFACT, so a slot
    // whose digest built perfectly and whose PUBLISH was refused used to end
    // here — recorded, logged, and silent. On 2026-10-11 that is exactly what
    // happened on a 429, and the owner was told nothing for the slot.
    //
    // PLACED AFTER THE READ-BACK ABOVE, so a slot already on the topic never
    // spends runner minutes rebuilding a digest the owner already has.
    //
    // NOT GATED ON THE FAILURE KIND. A 429 (the case that motivated this) and a
    // 5xx or a timeout are the same event from the owner's side -- this slot did
    // not reach him -- and the Actions arm is a different IP, so it is a real
    // recovery for either. What the kind changes is the RECORD, below, not the
    // response: `ntfy_quota_exhausted` is what lets the owner tell "wait for
    // midnight UTC" from "go read a log".
    const escalation = await escalateToActions(
      env, slotName, today,
      "ntfy HTTP " + published.http_status + (published.quota_exhausted
        ? " (daily quota exhausted)" : ""),
      now0(opts, startedAt));
    console.error(
      label + ": the digest was NOT pushed after " + published.attempts +
        " attempt(s) (" + published.error + "); the ledger keeps this slot in " +
        STATE_PENDING + " so the next run treats it as unknown and re-pushes" +
        (escalation.dispatched
          ? ", and the GitHub Actions arm has been dispatched as the recovery"
          : " (no recovery dispatch: " + escalation.reason + ")")
    );
    // Every field of the patch below is described here rather than inside it:
    // `ntfy_failed` is the verified failure and the only thing that arms the
    // 07:50 ladder; `ntfy_quota_exhausted` is named so the owner can tell
    // "wait for midnight UTC" from "go read a log"; `actions_dispatch_at` is
    // the once-per-slot dispatch marker, in the SAME record as the failure so
    // the guard and the evidence cannot disagree.
    if (writer) {
      await writer({
        date: today,
        slot: slotName,
        patch: {
          state: STATE_FAILED,
          kind: kind,
          sequence_id: sequenceId,
          ntfy_failed: true,
          ntfy_quota_exhausted: published.quota_exhausted === true,
          attempts: published.attempts,
          http_status: published.http_status,
          error: published.error,
          actions_dispatch_at: escalation.at,
          actions_dispatch_reason: escalation.reason || null,
        },
      }, now());
    }
    return {
      ok: false, kind: kind, date: today, slot: slotName, pushed: false,
      attempts: published.attempts, sequence_id: sequenceId,
      quota_exhausted: published.quota_exhausted === true,
      http_status: published.http_status,
      actions_dispatched: escalation.dispatched,
      reason: published.error,
    };
  }

  // 7. RECORD. `first_ntfy_sent_at` is the punctuality anchor and is written
  //    ONCE, at the instant of the successful send: it is what
  //    scripts/delivery_ledger.py subtracts `scheduled_instant` from, so it must
  //    be a recorded instant of the push and never `now()` at some later moment.
  const sentAtMs = now();
  const sentAt = isoUtc(sentAtMs);
  const record = {
    state: STATE_SENT,
    kind: kind,
    sequence_id: sequenceId,
    ntfy_failed: false,
    ntfy_sent_at: sentAt,
    first_ntfy_sent_at: sentAt,
    scheduled_instant: isoUtc(scheduledInstant),
    attempts: published.attempts,
    http_status: published.http_status,
    build: workerBuild(env),
    is_partial: artifact.is_partial === true ? true : false,
  };
  if (writer) {
    await writer({
      date: today,
      slot: slotName,
      patch: record,
      // `last_good` advances ONLY on a real digest. A fallback push must never
      // become the next day's fallback, or one bad day would feed itself.
      last_good: {
        date: today,
        slot: slotName,
        page_url: String(artifact.page_url),
        title: String(artifact.ntfy.title),
        body: String(artifact.ntfy.body),
        built_at: String(artifact.built_at || sentAt),
        at: sentAt,
      },
    }, sentAtMs);
  }
  const outcome = {
    ok: true,
    kind: kind,
    date: today,
    slot: slotName,
    pushed: true,
    sequence_id: sequenceId,
    attempts: published.attempts,
    http_status: published.http_status,
    ntfy_sent_at: sentAt,
    delta_s: deliveryDeltaSeconds(sentAt, scheduledInstant),
  };
  console.log(
    label + ": " + JSON.stringify({
      kind: outcome.kind,
      sequence_id: outcome.sequence_id,
      attempts: outcome.attempts,
      http_status: outcome.http_status,
      ntfy_sent_at: outcome.ntfy_sent_at,
      delta_s: outcome.delta_s,
      on_time: outcome.delta_s !== null && outcome.delta_s <= DELIVERY_ON_TIME_S,
    })
  );
  return outcome;
}

// `first_ntfy_sent_at - scheduled_instant`, in seconds, or null when either is
// missing. Mirrors `_delta_s` in scripts/delivery_ledger.py, including the rule
// that keeps it honest: only two RECORDED instants, never a `now()` at write
// time, because a write-time delta measures how fast the code is.
function deliveryDeltaSeconds(sentIso, scheduledMs) {
  const sent = Date.parse(String(sentIso || ""));
  if (!Number.isFinite(sent) || !Number.isFinite(Number(scheduledMs))) return null;
  return (sent - Number(scheduledMs)) / 1000;
}

// The `unavailable` outcome, built without touching the ledger. Takes the topic
// VALUE rather than `env` because that is the only thing it needs from the
// binding, and a parameter nobody reads is a parameter that will drift.
function unavailableOutcome(topic, today, slotName, opts, reason) {
  const startedAt = (opts && opts.now ? opts.now : Date.now)();
  return {
    ok: false,
    kind: KIND_UNAVAILABLE,
    date: today,
    slot: slotName,
    pushed: false,
    reason: reason || "the digest ledger could not be read",
    payload: digestPayload(
      topic,
      "⚠️ " + slotLabel(slotName) + " digest unavailable",
      [
        reason || "the digest record could not be read, so nothing was sent " +
          "for this slot.",
        "",
        "Nothing is lost — the fallback still runs. Archive: " +
          digestArchiveIndexUrl(),
      ].join("\n"),
      digestArchiveIndexUrl(),
      "5",
      "warning,rotting_light"
    ),
    sequence_id: digestFullSequenceId(today, slotName),
    started_at: startedAt,
  };
}

// Push an `unavailable` payload and record NOTHING in the ledger. Used for the
// paths where the ledger could not be read, where a write would destroy the
// record it could not see.
async function pushUnrecorded(env, outcome, label, opts) {
  const published = await publishToNtfy(
    env, outcome.payload, outcome.sequence_id,
    label + " (unavailable)", opts);
  outcome.pushed = published.ok;
  outcome.attempts = published.attempts;
  outcome.http_status = published.http_status;
  if (!published.ok) {
    console.error(
      label + ": even the unavailable push FAILED (" + published.error +
        ") — this arm could not reach the owner at all"
    );
  }
  outcome.reason = (outcome.reason || "") +
    (published.ok ? "" : " | the unavailable push also failed: " + published.error);
  console.log(label + ": " + JSON.stringify({
    kind: outcome.kind, pushed: outcome.pushed, attempts: outcome.attempts,
    http_status: outcome.http_status, sequence_id: outcome.sequence_id,
    ledger_written: false,
  }));
  return outcome;
}

// The fallback ladder: no usable artifact, so show the last digest that DID
// build, clearly labelled as older — and if there is none, say so.

// THE QUOTA ESCALATION. ONE helper, called from BOTH rungs, because a second
// caller that re-implements it is a second thing that can be wrong at 19:20.
//
// WHAT THIS IS. `dispatchDigest` above had exactly ONE call site:
// `pushFallbackLadder`, the NO-ARTIFACT path. A slot that had a perfectly good
// artifact and a 429 from ntfy therefore never reached it -- the push failed,
// `ntfy_failed` was recorded, and the owner was told nothing. Observed
// 2026-10-11: HTTP 429, code 42908, "daily message quota reached", after all
// three attempts. The GitHub Actions arm is the one that demonstrably reached
// the owner that day, and it only ran because of its own separate 02:05/14:05Z
// schedule, not because anything asked it to.
//
// WHY THE GITHUB ARM AND NOT A RETRY. ntfy identifies an unauthenticated
// visitor by source IP (docs: request limits are "per visitor", a token bucket
// of 60 refilled one per 5s; the daily message counter resets midnight UTC).
// The Worker's egress and a GitHub runner's are different IPs and so, on this
// evidence, different visitor buckets: run #288 published to the SAME topic
// from the runner at 08:46Z ("ntfy notification sent ... id=KESM0snbRtYY")
// while the Worker was being refused. Retrying from the Worker walks into the
// same wall seconds later. PROVEN: the two arms differ here. INFERRED, and the
// docs do NOT settle it: that IP classification is the reason.
//
// ONCE PER SLOT, NEVER TWICE. The digest crons re-fire four more times inside a
// bounded window after the slot instant and every one of them re-enters this
// path, so a bad day would otherwise dispatch five builds and push five times.
// The marker lives in the delivery ledger beside `ntfy_failed`, because that is
// the one record this arm, the watchdog and the Actions fallback gate all
// already consult.
//
// AN UNREADABLE LEDGER DISPATCHES ANYWAY. Reading "I cannot see a marker" as
// "not dispatched" would spend a build and risk a duplicate push on every
// re-fire of a bad day. A wasted build is recoverable; a duplicate push is
// visible to the owner and alarming.
//
// `cause` AND `http_status` TRAVEL WITH `dispatched`, because `dispatched`
// alone is what told the owner his token had died on 2026-10-13 when the guard
// had simply stood down. See githubWorkflowDispatch's header.

// ===========================================================================
// DID A WORKER-DISPATCHED RUN ALREADY DELIVER THIS SLOT?
// ===========================================================================
//
// THE DEFECT. `digest:artifact:*` is written by exactly one writer — the seal
// job in digest-prebuild.yml. daily-news.yml NEVER writes it. So on the morning
// of 2026-10-13, when the 06:40 prebuild failed and this Worker dispatched
// daily-news.yml at 01:35:36Z, that run SUCCEEDED and delivered a correct fresh
// digest at 01:40:03Z (run 38332862002, ntfy id yqiRtmjhU7Go). Ten minutes
// later the 01:50:26Z re-fire found no artifact, could not see that delivery
// through any local read, and pushed last-good. The owner received today's
// morning digest at 07:25 NPT and then YESTERDAY'S EVENING digest at 07:35 NPT:
// two pushes, the second contradicting the first.
//
// WHY THE EVIDENCE IS GITHUB'S OWN RECORD OF A RUN, AND NOT A NEW MARKER.
//   * It is PRIMARY. A marker is a second hand-written claim that has to be kept
//     in step with the thing it claims; a successful workflow run IS the thing.
//   * It adds NO WRITER. A marker written by the Actions arm would be a second
//     writer into the delivery namespace, and this module's whole argument for
//     `digest:artifact:*` being trustworthy is that exactly one writer builds
//     it, with `schema_version`, `is_partial` and `source_health_state` coming
//     from the seal path. One writer, one meaning.
//   * It is READ-ONLY and needs no new credential: the same GH_TOKEN that
//     dispatched the run reads the run back.
//   * It keeps the deploy scoped to this one file. A workflow change would mean
//     the fix is not live until a workflow run happens to execute it.
//
// WHY IT FAILS CLOSED. Three answers, never two:
//   delivered    — a successful workflow_dispatch run of daily-news.yml exists
//                  inside this slot's OWN dispatch window. Stand down.
//   not_delivered— GitHub was READ, the list was complete and parseable, and no
//                  such run is in it. Proceed with the existing ladder.
//   unknown      — anything at all could not be established: no token, a non-2xx
//                  read, a transport error, a body that is not a run list, a run
//                  whose created_at will not parse. The ladder STANDS DOWN but
//                  says so loudly and records `unknown`.
//
// `unknown` deliberately does NOT push, and that is the whole point of a third
// answer. Pushing last-good is the irreversible outward-facing act: it reaches
// the owner's phone and contradicts a digest he already has, and nothing the
// Worker can do afterwards takes it back. NOT pushing is recoverable — the next
// re-fire, the 07:50 GitHub fallback and the watchdog all still run. So an
// unreadable state is neither silently "delivered" (which would stop delivery
// outright) nor silently "not delivered" (which would repeat the 2026-10-13
// double push); it is a loud, recorded stand-down that keeps the slot visible.
//
// THE WINDOW IS THIS SLOT'S OWN, WHICH IS WHAT MAKES IT SPECIFIC. It runs from
// the slot's recorded `actions_dispatch_at` to now. That is at most
// REFIRE_GAP_MINUTES * REFIRE_ATTEMPTS after the slot instant, because the
// ladder is only reachable inside the bounded re-fire window. It cannot contain
// any other dispatcher: drift-canary.yml dispatches daily-news.yml at 00:30Z
// and 03:17Z, and digest-prebuild.yml's seal job dispatches it around
// 00:55Z / 12:55Z — all outside 01:35-01:55Z and 13:35-13:55Z.
//
// THE SERVER-SIDE `created` FILTER IS NOT TRUSTED. The window is applied to the
// runs HERE, so an engine-side filter that quietly narrows the set cannot turn a
// delivery into a miss. Only `status=success` is asked of the API, and it is
// re-checked per run rather than taken on trust.
const DELIVERY_EVIDENCE_DELIVERED = "delivered";
const DELIVERY_EVIDENCE_NOT_DELIVERED = "not_delivered";
const DELIVERY_EVIDENCE_UNKNOWN = "unknown";

function digestRunsUrl() {
  return DIGEST_DISPATCH_URL.replace("/dispatches", "/runs");
}

// Pure: the decision, given a parsed run list. Exported so a test can pin the
// fail-closed arithmetic without a network call at all.
function classifyDeliveryEvidence(runs, dispatchAtIso, nowMs) {
  if (!Array.isArray(runs)) {
    return { state: DELIVERY_EVIDENCE_UNKNOWN,
             reason: "the Actions API did not return a run list" };
  }
  const from = Date.parse(String(dispatchAtIso));
  if (!Number.isFinite(from)) {
    return { state: DELIVERY_EVIDENCE_UNKNOWN,
             reason: "this slot has no usable dispatch timestamp to measure from" };
  }
  const now = Number.isFinite(Number(nowMs)) ? Number(nowMs) : from;
  for (const run of runs) {
    if (!run || typeof run !== "object" || Array.isArray(run)) {
      return { state: DELIVERY_EVIDENCE_UNKNOWN,
               reason: "the run list contains an entry that is not a run" };
    }
    const startedAt = Date.parse(String(run.created_at));
    if (!Number.isFinite(startedAt)) {
      // A run we cannot place in time cannot be cleared or convicted. Guessing
      // either way is how a guard silently stops working.
      return { state: DELIVERY_EVIDENCE_UNKNOWN,
               reason: "a run in the list has no parseable created_at" };
    }
    if (String(run.event) !== "workflow_dispatch") continue;
    // BOTH terms, not one. GitHub sets `conclusion: null` while a run is in
    // progress, so `conclusion === "success"` alone would be a complete proof
    // of delivery — and a run that has not finished has not delivered. The
    // second term costs nothing and removes a shape a future run-record change
    // could turn into a silent stand-down on a slot nobody delivered.
    if (String(run.status) !== "completed") continue;
    if (String(run.conclusion) !== "success") continue;
    if (startedAt < from || startedAt > now) continue;
    return {
      state: DELIVERY_EVIDENCE_DELIVERED,
      run: {
        id: String(run.id == null ? "" : run.id),
        url: String(run.html_url || ""),
        created_at: String(run.created_at),
        completed_at: String(run.updated_at || ""),
      },
      reason: "a successful workflow_dispatch run of daily-news.yml exists " +
        "inside this slot's own dispatch window",
    };
  }
  return {
    state: DELIVERY_EVIDENCE_NOT_DELIVERED,
    reason: "no successful workflow_dispatch run of daily-news.yml inside this " +
      "slot's dispatch window",
  };
}

async function checkSlotDeliveredByWorkerRun(env, slotName, today, nowMs, opts) {
  const label = "digest delivery evidence (" + today + " " + slotName + ")";
  const unknown = (why) => {
    console.error(
      label + ": " + DELIVERY_EVIDENCE_UNKNOWN.toUpperCase() + " — " + why +
        ". NOT pushing last-good either: a stale second push contradicts a " +
        "digest the owner may already have, and the 07:50 GitHub fallback and " +
        "the watchdog both still own this slot."
    );
    return { state: DELIVERY_EVIDENCE_UNKNOWN, reason: why, run: null };
  };

  // MARKER FIRST, THEN TOKEN, and the order is load-bearing.
  //   * No marker means this Worker never dispatched a build for this slot, so
  //     there is no run to find and nothing to ask GitHub. That is a DEFINITIVE
  //     "not delivered" and it costs no network call — which is the common case,
  //     since the marker only exists once this arm has escalated once.
  //   * A marker with no token to read the API with is the genuinely ambiguous
  //     case (the token was present when the slot was dispatched and is absent
  //     now), and that is `unknown`, not a guess.
  const markerAt = opts && opts.dispatch_at ? String(opts.dispatch_at) : "";
  if (!markerAt) {
    return { state: DELIVERY_EVIDENCE_NOT_DELIVERED, run: null,
             reason: "this Worker never dispatched a build for this slot, so " +
               "there is no dispatch window and no run to find" };
  }
  if (!env || !env.GH_TOKEN) {
    return unknown("this slot HAS a recorded dispatch but there is no GH_TOKEN " +
      "to read the Actions API with");
  }
  let res;
  try {
    res = await fetch(
      digestRunsUrl() + "?event=workflow_dispatch&status=success&per_page=20",
      {
        headers: {
          Authorization: "Bearer " + env.GH_TOKEN,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "daily-media-news-breaking-worker",
        },
        signal: AbortSignal.timeout(DELIVERY_ATTEMPT_TIMEOUT_MS),
      }
    );
  } catch (err) {
    return unknown("the Actions API read did not complete (" +
      redactDispatchSummary(String((err && err.message) || err),
                            env.GH_TOKEN) + ")");
  }
  if (!res.ok) {
    return unknown("the Actions API answered HTTP " + res.status +
      " (the token can dispatch and may not be able to read runs; neither " +
      "reading is a reason to guess)");
  }
  let parsed;
  try {
    parsed = JSON.parse(await res.text());
  } catch (err) {
    return unknown("the Actions API response is not JSON");
  }
  const runs = parsed && Array.isArray(parsed.workflow_runs)
    ? parsed.workflow_runs
    : (Array.isArray(parsed) ? parsed : null);
  const verdict = classifyDeliveryEvidence(runs, markerAt, nowMs);
  if (verdict.state === DELIVERY_EVIDENCE_UNKNOWN) {
    console.error(label + ": " + DELIVERY_EVIDENCE_UNKNOWN.toUpperCase() +
      " — " + verdict.reason);
    return { state: DELIVERY_EVIDENCE_UNKNOWN,
             reason: verdict.reason, run: null };
  }
  console.log(
    label + ": " + verdict.state.toUpperCase() + " — " + verdict.reason +
      (verdict.run && verdict.run.id ? " (run " + verdict.run.id + ")" : "")
  );
  return verdict;
}

async function escalateToActions(env, slotName, today, reason, nowMs) {
  if (!env || !env.GH_TOKEN) {
    return { dispatched: false, at: null, reason: "GH_TOKEN is not set",
             cause: DISPATCH_CAUSE_TOKEN_MISSING, http_status: 0 };
  }
  let raw = null;
  try {
    raw = await env.KV.get(DIGEST_LEDGER_KEY);
  } catch (err) {
    console.error(
      "quota escalation: the ledger could not be READ (" +
        scrubTopic(String(err), ntfyTopicOf(env)) + ") — dispatching anyway, "
        + "because an invisible marker must never be read as 'not dispatched'"
    );
  }
  const entry = ledgerSlotEntry(normaliseDigestLedger(raw), today, slotName);
  if (entry && entry.actions_dispatch_at) {
    console.log(
      "quota escalation: " + today + " " + slotName + " was already dispatched "
        + "at " + entry.actions_dispatch_at + " — NOT dispatching again"
    );
    // CAUSE 2 OF 5, AND IT IS NOT A FAILURE. This is the once-per-slot guard
    // working exactly as designed: a build for this slot is already on its way,
    // so this pass has nothing left to do. No HTTP request was made and no
    // credential was consulted. It carries its own cause so the notice can say
    // so in words instead of guessing at a token fault.
    return { dispatched: false, at: entry.actions_dispatch_at,
             reason: "already dispatched this slot",
             cause: "already_dispatched", http_status: 0 };
  }
  // THE QUOTA GATE, before the workflow_dispatch that bills a daily-news build.
  // The digest is PROTECTED: it is refused LAST, only once the month has ALREADY
  // reached the hard ceiling. While the month is under it the digest is allowed
  // even when it would tip the month over -- a prebuild is refused instead.
  const budget = await budgetChargeDispatch(env, "digest", nowMs);
  if (!budget.allow) {
    console.error(
      "quota escalation: " + today + " " + slotName + " digest build refused (" +
        budget.reason + "): the Worker still pushes its own fallback from its " +
        "own IP, so this costs the GitHub build, not the news"
    );
    return { dispatched: false, at: null,
             reason: "budget refused (" + budget.reason + ")",
             cause: "budget_refused", http_status: 0 };
  }
  const result = await dispatchDigest(env, slotName, today);
  const dispatched = !!result.ok;
  if (!dispatched) {
    // Persisted, so "what did GitHub actually answer?" is one command next
    // time instead of an argument from side effects. No token and no raw body:
    // `result.summary` was already redacted where it was built.
    await recordGithubDispatchStatus(env, {
      workflow: "daily-news.yml",
      http_status: result.http_status,
      cause: result.cause,
      summary: result.summary,
    });
  }
  console.log(
    "quota escalation: " + today + " " + slotName + " — " +
      (dispatched
        ? "dispatched the GitHub Actions arm, which publishes from its own IP"
        : "the dispatch did NOT succeed (" + result.cause +
          ", HTTP " + result.http_status + "), so nothing is building") +
      " (" + reason + ")"
  );
  return {
    dispatched: dispatched,
    at: dispatched ? isoUtc(nowMs) : null,
    reason: dispatched ? ""
      : ("the workflow_dispatch was not accepted (" + result.cause +
         ", HTTP " + result.http_status + ")"),
    cause: result.cause,
    http_status: result.http_status,
  };
}

async function pushFallbackLadder(env, ledger, today, slotName, reason, plain,
                                 writer, opts, label, startedAt) {
  const topic = ntfyTopicOf(env);
  const lastGood = ledger && ledger.last_good ? ledger.last_good : null;
  const usable = lastGood && String(lastGood.date || "") !== today &&
    String(lastGood.body || "");

  const kind = usable ? KIND_FALLBACK_LAST_GOOD : KIND_UNAVAILABLE;
  const sequenceId = digestFullSequenceId(today, slotName);
  const scheduledInstant = digestScheduledInstant(today, slotName);

  // Write-ahead, same as the real digest: `pending` before the push.
  if (writer) {
    await writer({
      date: today, slot: slotName,
      patch: {
        state: STATE_PENDING,
        kind: kind,
        sequence_id: sequenceId,
        pending_at: isoUtc(startedAt),
        scheduled_instant: isoUtc(scheduledInstant),
        reject_reason: reason,
        build: workerBuild(env),
      },
    }, startedAt);
  }

  const seen = await findDeliveredBySequenceId(env, sequenceId);
  if (seen.found) {
    const sentAt = seen.time ? isoUtc(seen.time * 1000) : isoUtc(startedAt);
    if (writer) {
      await writer({
        date: today, slot: slotName,
        patch: {
          state: STATE_SENT, kind: kind, sequence_id: sequenceId,
          ntfy_failed: false,
          ntfy_sent_at: sentAt, first_ntfy_sent_at: sentAt,
          scheduled_instant: isoUtc(scheduledInstant),
          deduped_via: "sequence_id read-back", build: workerBuild(env),
        },
      }, startedAt);
    }
    console.log(
      label + ": " + kind + " push SKIPPED — the sequence_id read-back found " +
        "this fallback already on the topic"
    );
    return {
      ok: true, kind: kind, date: today, slot: slotName, pushed: false,
      deduped: true, sequence_id: sequenceId, reject_reason: reason,
      ledger_patch: null, reason: "sequence_id already present",
    };
  }

  // HAS A WORKER-DISPATCHED RUN ALREADY TAKEN THIS SLOT?
  //
  // THIRD GUARD, AFTER THE LEDGER AND AFTER THE SEQUENCE_ID READ-BACK, because
  // those two only know about pushes THIS ARM made. Neither can see a delivery
  // made by the Actions arm, and that is the whole gap: on 2026-10-13 the
  // 01:40:03Z Actions delivery was real and correct, the 01:50:26Z re-fire could
  // not see it, and last-good went out over the top of it.
  //
  // PLACED BEFORE THE ESCALATION on purpose. If the slot is already delivered
  // there is nothing to build, so spending a 20-minute Actions run on it is
  // waste as well as the wrong outcome.
  //
  // ONLY `not_delivered` FALLS THROUGH TO THE LADDER, and that is the whole
  // three-answer design. `delivered` and `unknown` both stand down here, for
  // the same reason and for different reasons: pushing last-good is the
  // irreversible outward-facing act — it reaches the owner's phone and
  // contradicts a digest he may already hold, and nothing takes it back — while
  // NOT pushing is recoverable, because the next re-fire, the 07:50 GitHub
  // fallback and the watchdog all still own this slot. `unknown` is recorded
  // AS `unknown` and logged loudly, so it is never a silent "delivered".
  // See the three-answer contract on classifyDeliveryEvidence.
  // The marker comes from the ledger this invocation already read, not from a
  // second KV read: a stand-down must not cost a write, and escalateToActions
  // re-reads the same key itself when it does go on to dispatch.
  const dispatchMarker =
    (ledgerSlotEntry(ledger || {}, today, slotName) || {}).actions_dispatch_at || "";
  const evidence = await checkSlotDeliveredByWorkerRun(
    env, slotName, today, startedAt, { dispatch_at: dispatchMarker });
  if (evidence.state !== DELIVERY_EVIDENCE_NOT_DELIVERED) {
    // NOTHING IS WRITTEN. The slot entry already carries the dispatch marker and
    // this arm produced nothing, so the honest record is the one that is already
    // there — and not writing is also the strongest possible guarantee that a
    // stand-down can never blank a marker.
    console.warn(
      label + ": " + DELIVERY_KIND_STOOD_DOWN + " — " + evidence.reason +
        (evidence.state === DELIVERY_EVIDENCE_UNKNOWN
          ? ". Delivery evidence was UNKNOWN, so this is not a claim that the " +
            "slot was delivered; it is a refusal to push a second, staler " +
            "digest on top of an unverified state."
          : ". No last-good push, and no late build: the slot is already done.") +
        " (last_good on file is " + (usable ? String(lastGood.date) : "none") +
        ")"
    );
    return {
      ok: true, kind: DELIVERY_KIND_STOOD_DOWN, date: today, slot: slotName,
      pushed: false, deduped: true, sequence_id: sequenceId,
      reject_reason: reason,
      delivery_evidence: evidence.state,
      delivery_evidence_reason: evidence.reason,
      delivery_evidence_run: evidence.run || null,
      late_build_dispatched: false,
      // NULL, ALWAYS, EVEN WHEN last_good EXISTS. `last_good_shown` means "this
      // is the digest the owner was shown", and on this path nothing was shown.
      // Reporting the last-good date here would let a reader believe the owner
      // holds yesterday's digest when he holds today's fresh one — which is the
      // mirror image of the 2026-10-13 bug and just as wrong.
      last_good_shown: null,
      reason: evidence.reason,
    };
  }

  // A FAILED PREBUILD MUST DEGRADE TO LATE, NEVER TO NOTHING.
  //
  // `dispatchDigest` above was RETAINED for exactly this moment and had ZERO
  // call sites: `pushFallbackLadder` never dispatched anything. So the chain
  // used to be prebuild dies -> nothing sealed -> no artifact -> last_good is
  // null -> push "digest unavailable" -> and NOTHING IS EVER BUILT. The owner
  // got an apology instead of a digest, and on 2026-10-11 even the apology
  // failed to publish, so he received literally nothing.
  //
  // So: once per slot, here, on the path that means "this slot has no digest".
  // The push below still happens either way -- a dispatch is an ADDITION to the
  // fallback, never a replacement for it, so the owner hears something within
  // seconds and the real digest arrives late if the dispatch is accepted.
  //
  // PLACED AFTER THE READ-BACK DEDUP ON PURPOSE. If this sequence_id is already
  // on the topic then this slot has already been handled, and waking a 20-minute
  // build for it would spend runner minutes to build a digest nobody would be
  // pushed again. Also placed before the payload is built, so the push can tell
  // the owner, truthfully, whether a fresh build is on its way.
  //
  // DELIBERATELY NOT GATED ON `writer`. The probe passes a null writer, but the
  // probe already POSTs this same payload to the owner's phone on this same
  // path, so a workflow_dispatch is a smaller outward effect than the publish
  // it accompanies -- and gating it would invent a distinction the design does
  // not make.
  // THE SAME HELPER THE REAL-PATH FAILURE USES, so the two rungs cannot each
  // dispatch independently and turn one bad slot into two builds and two
  // pushes. It is also what makes the once-per-slot marker cover BOTH rungs: a
  // fallback dispatch marks the slot, so the quota escalation on a later
  // re-fire of the same slot stands down.
  // BOTH FIELDS ARE KEPT, AND `at` IS THE POINT OF THIS CALL.
  // `escalateToActions` returns `at` as "the dispatch marker for this slot",
  // including when it stands down because the marker is ALREADY set -- in that
  // case it hands back the existing timestamp rather than null. Discarding it
  // (which this line once did, by taking `.dispatched` alone) meant the write
  // below computed `null` on every stand-down and OVERWROTE the marker with
  // null, so the very next re-fire read an empty marker and dispatched again.
  // That erase-then-redispatch cycle is what sent the owner three digests for
  // the 2026-10-12 morning slot (ntfy ids nF1QhKGuDrCM, hIG1H3oyuM2C,
  // fsr4kliFEco6, all trigger_source "worker") with the marker ending at
  // 01:55:26Z -- the LAST fire.
  //
  // THE STAND-DOWN SEMANTICS ARE UNCHANGED: `dispatched` is still what decides
  // whether a NEW dispatch happens, and a set marker still suppresses one. Only
  // the erasure is fixed, and `at` is preferred over recomputing a timestamp so
  // a stand-down reproduces the recorded instant EXACTLY instead of inventing a
  // new one on every pass.
  const escalation = await escalateToActions(env, slotName, today, reason, startedAt);
  const dispatched = escalation.dispatched;
  // NO LLM CONTROL OVER THIS STRING, AND NO CAUSE THIS CODE DID NOT ESTABLISH.
  // Derived only from `escalation.cause`, which githubWorkflowDispatch sets from
  // the HTTP status it actually received or from the marker it actually read.
  //
  // WHY IT WAS REWRITTEN. `dispatched ? A : B` had two branches for five causes,
  // and branch B said "(no usable GH_TOKEN)". On 2026-10-13 cause 2 of 5 fired —
  // the once-per-slot guard standing down, which is a SUCCESSFUL guard doing its
  // job — and the owner was told his credential had died. It had not: that token
  // had dispatched two workflows hours earlier. A message that names a fault
  // that did not happen costs the owner real time, so the five causes now get
  // five sentences and the two shapes are unmistakable to a non-technical reader:
  // one lead-in means "nothing is wrong", the other means "here is exactly what
  // stopped it".
  //
  // PLACED DIRECTLY UNDER "Why you are seeing this", NOT APPENDED AT THE END.
  // `clipToNtfyLimit` clips from the END, so a note at the tail of a long
  // last_good body would be the first thing dropped -- and a push that quietly
  // loses its "a build is on its way" line is the exact dishonesty this fix
  // exists to prevent. Near the top it survives clipping, and it is also the
  // part the owner reads first on a phone.
  const dispatchNote = dispatched
    ? "\n\n**A fresh build is on its way.** Nothing was sealed in time for " +
      "this slot, so one was dispatched from here and will arrive late."
    : escalation.cause === "already_dispatched"
      // CAUSE 2. NOT A FAILURE, AND IT MUST NOT READ LIKE ONE — which is why
      // its first words are completely different from every branch below.
      ? "\n\n**Nothing is wrong — this slot is already being handled.** A build " +
        "for it was started at " + String(escalation.at || "an earlier pass") +
        ", so this pass had nothing left to do. There is no action to take and " +
        "nothing to fix. This notice is just the fallback for that same slot."
      // CAUSES 1, 3, 4 AND 5. The lead-in is the stable, already-shipped phrase
      // — tests/digest/digest_delivery.test.js pins it as the honesty contract:
      // a refused dispatch must never leave the owner believing a build is on
      // its way. What follows it is now the CAUSE, established by
      // githubWorkflowDispatch, instead of a guess.
      : "\n\n**No fresh build was started.** " +
        (escalation.cause === DISPATCH_CAUSE_TOKEN_MISSING
          ? "No GitHub token is configured on this Worker, so the request to " +
            "build one was never made. This is a configuration fault, not a " +
            "problem with the digest itself."
          : escalation.cause === DISPATCH_CAUSE_TOKEN_UNAUTHORISED
            ? "GitHub rejected the token (HTTP 401), so the token this Worker " +
              "holds is invalid or expired. Only builds are affected; the " +
              "digest itself is fine."
            : escalation.cause === DISPATCH_CAUSE_TOKEN_FORBIDDEN
              ? "GitHub refused the token (HTTP 403), so the token is valid " +
                "but is not allowed to start workflows. Only builds are " +
                "affected; the digest itself is fine."
              : escalation.cause === DISPATCH_CAUSE_TRANSPORT_ERROR
                ? "The request to GitHub never completed, so this is a network " +
                  "fault rather than a credential one — nothing points at the " +
                  "token."
                : "GitHub answered HTTP " + String(escalation.http_status || 0) +
                  ", so the request was sent and answered and this is not a " +
                  "credential fault either.") +
        " Nothing was dispatched, so this notice is the whole of what this " +
        "slot produced.";
  // UNCHANGED, DELIBERATELY. `reject_reason` is an anchored two-value contract
  // (tests/digest/digest_delivery.test.js pins it with $) read by the watchdog
  // and the ledger reader, so the cause does NOT go here. It goes to
  // `actions_dispatch_reason` / `actions_dispatch_cause` below, which are the
  // fields that exist to be specific.
  reason = (reason || "") +
    (dispatched ? " | a late build was dispatched" : " | NO build was dispatched");
  console.log(
    label + ": no artifact for " + today + " " + slotName + " — " +
      (dispatched
        ? "dispatched a late build via GH_TOKEN (workflow_dispatch accepted)"
        : "the late-build dispatch did NOT succeed (" +
          String(escalation.cause || "unknown") +
          (escalation.http_status ? ", HTTP " + escalation.http_status : "") +
          "), so nothing is building")
  );
  const header = ["**Why you are seeing this**", plain].join("\n") +
    dispatchNote;

  const payload = usable
    ? digestPayload(
        topic,
        "⚠️ " + slotLabel(slotName) + " digest delayed — showing " +
          String(lastGood.date),
        header + "\n\n" +
          "**The last digest that did build — " + String(lastGood.date) + "**\n" +
          String(lastGood.body),
        String(lastGood.page_url || digestArchiveIndexUrl()),
        "5",
        "warning,rotting_light"
      )
    : digestPayload(
        topic,
        "⚠️ " + slotLabel(slotName) + " digest unavailable",
        header + "\n\n" +
          "No earlier digest is on file, so this is all there is. The " +
            "archive index — " + digestArchiveIndexUrl(),
        digestArchiveIndexUrl(),
        "5",
        "warning,rotting_light"
      );

  const published = await publishToNtfy(env, payload, sequenceId, label + " (" + kind + ")", opts);
  if (writer) {
    await writer({
      date: today, slot: slotName,
      patch: {
        state: published.ok ? STATE_SENT : STATE_FAILED,
        kind: kind,
        sequence_id: sequenceId,
        // The fallback ladder is a delivery like any other: a successful
        // `fallback_last_good` push DID reach the owner and must clear any
        // earlier failure, and a failed one is a verified failure and must arm
        // the 07:50 ladder exactly as the real digest's failure does.
        ntfy_failed: published.ok ? false : true,
        reject_reason: reason,
        reject_plain: plain,
        attempts: published.attempts,
        http_status: published.http_status,
        ntfy_sent_at: published.ok ? isoUtc(now0(opts, startedAt)) : null,
        first_ntfy_sent_at: published.ok ? isoUtc(now0(opts, startedAt)) : null,
        scheduled_instant: isoUtc(scheduledInstant),
        build: workerBuild(env),
        // NEVER the last_good slot: a fallback must not become tomorrow's
        // fallback, or one bad day feeds itself for ever.
        last_good_shown: usable ? String(lastGood.date) : null,
        // Was a late build actually dispatched for this slot? The watchdog and
        // the ledger reader both need this to tell "the build is coming" from
        // "nothing is building and only this apology exists", and guessing it
        // from the absence of a failure would be exactly the dishonesty the
        // dispatch note above refuses.
        late_build_dispatched: dispatched,
        // The once-per-slot dispatch marker, on this rung too. Without it here,
        // the quota escalation added to the real-digest path would re-dispatch
        // on every re-fire of a slot this ladder already dispatched.
        //
        // NEVER BLANKED. `escalation.at` is the marker whether this fire
        // dispatched or stood down, so a stand-down REWRITES THE SAME
        // TIMESTAMP rather than clearing it. The previous
        // `dispatched ? isoUtc(startedAt) : null` made every stand-down write
        // null over a real marker, which is the erasure that let each re-fire
        // dispatch again. The `||` is belt-and-braces for the one case where
        // nothing dispatched AND no prior marker existed (no GH_TOKEN at all):
        // there is genuinely nothing to record, so null is still the truth.
        actions_dispatch_at: escalation.at || null,
        actions_dispatch_reason: dispatched ? null : (escalation.reason || reason),
        // THE CAUSE, MACHINE-READABLE. `actions_dispatch_reason` above is prose
        // and `reject_reason` is a deliberately stable two-value contract, so
        // neither can answer "which of the five was it?" on its own. These two
        // are what make the next one a query instead of an argument, and they
        // are what would have told the truth on 2026-10-13.
        actions_dispatch_cause: dispatched ? null : (escalation.cause || null),
        actions_dispatch_http_status:
          dispatched ? null : (escalation.http_status || 0),
      },
    }, now0(opts, startedAt));
  }
  console.log(
    label + ": " + JSON.stringify({
      kind: kind, reject_reason: reason, pushed: published.ok,
      attempts: published.attempts, http_status: published.http_status,
      sequence_id: sequenceId,
      late_build_dispatched: dispatched,
      last_good_shown: usable ? String(lastGood.date) : null,
    })
  );
  return {
    ok: published.ok,
    kind: kind,
    date: today,
    slot: slotName,
    pushed: published.ok,
    attempts: published.attempts,
    http_status: published.http_status,
    sequence_id: sequenceId,
    reject_reason: reason,
late_build_dispatched: dispatched,
      last_good_shown: usable ? String(lastGood.date) : null,
      // Carried on the pushed path too, so `delivery_evidence` is a field every
      // outcome from this function has and a reader never has to infer it from
      // which other fields happen to be set.
      delivery_evidence: DELIVERY_EVIDENCE_NOT_DELIVERED,
      delivery_evidence_reason: evidence.reason,
      reason: published.ok ? "" : published.error,
    };
}

function now0(opts, fallback) {
  const fn = opts && opts.now ? opts.now : Date.now;
  const value = fn();
  return Number.isFinite(value) ? value : fallback;
}

// THE CRON ARM. This is the only caller that passes a ledger writer, which is
// what makes the ledger write structurally unreachable from any fetch path — the
// same argument as the heartbeat placement comment in scheduled(). U5 wired it
// into scheduled(), so all four digest crons reach it and no fetch path can.
async function deliverDigest(env, slot, options) {
  return await deliverDigestPlan(
    env, slot, (update, nowMs) => commitDigestLedger(env, update, nowMs), options);
}

// ---- ingest: POST /?ingest=1 --------------------------------------------------

// Constant-time equality, mirroring functions/_middleware.js. Two SHA-256
// digests compared without an early exit, so neither the token's value nor its
// length leaks through timing.
function constantTimeEqualBytes(left, right) {
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    difference |= (left[i] || 0) ^ (right[i] || 0);
  }
  return difference === 0;
}

async function constantTimeEquals(left, right) {
  const encoder = new TextEncoder();
  const digests = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(String(left))),
    crypto.subtle.digest("SHA-256", encoder.encode(String(right))),
  ]);
  return constantTimeEqualBytes(
    new Uint8Array(digests[0]), new Uint8Array(digests[1]));
}

// Write the built digest's artifact into KV. NO LEDGER READ, NO LEDGER WRITE:
// this endpoint is the build side handing over a record, and giving it any
// access to the delivery ledger would put the punctuality record one crafted
// request away from being rewritten.
//
// Fail-closed on the credential: an ABSENT DIGEST_INGEST_TOKEN is compared
// against "", so it refuses every request rather than accepting them. That is
// functions/_middleware.js's rule, unchanged — an unconfigured secret means
// "nobody gets in", never "everybody does".
async function handleDigestIngest(request, env) {
  if (request.method !== "POST") {
    return ingestError(
      "this endpoint accepts POST only; a GET would carry the token in a URL " +
        "and a log",
      405);
  }
  const expected = env && typeof env[INGEST_TOKEN_SECRET] === "string"
    ? env[INGEST_TOKEN_SECRET]
    : "";
  const provided = request.headers.get(INGEST_TOKEN_HEADER) || "";
  // Fail CLOSED, and check it BEFORE the comparison rather than only through it.
  //
  // Comparing an absent secret against "" would refuse every request that
  // offers a token, and ACCEPT every request that offers none — which is the
  // same string on both sides. An unauthenticated `curl -X POST` would then be
  // authenticated by the absence of a credential, so the check has to be
  // explicit. functions/_middleware.js makes the same check for the same reason
  // and this is the same rule.
  if (!expected) {
    console.error(
      "DIGEST_INGEST_MISCONFIGURED: the " + INGEST_TOKEN_SECRET +
        " secret is missing or empty on this Worker, so every ingest is " +
        "being refused. This is FAIL-CLOSED and is what an unconfigured " +
        "deployment must do — but the owner has not created the secret yet, " +
        "so no digest can be handed over. Create it with: wrangler secret " +
        "put " + INGEST_TOKEN_SECRET + " -c deploy/wrangler.toml (the value " +
        "itself is never read into this message)."
    );
    return ingestError(
      "401 unauthorized: the " + INGEST_TOKEN_SECRET + " secret is not " +
        "configured on this Worker",
      401);
  }
  const authorised = await constantTimeEquals(provided, expected);
  if (!authorised) {
    console.error(
      "digest ingest REFUSED: the " + INGEST_TOKEN_HEADER + " header did not " +
        "match the " + INGEST_TOKEN_SECRET + " secret"
    );
    return ingestError(
      "401 unauthorized: the " + INGEST_TOKEN_HEADER + " header did not match " +
        "the " + INGEST_TOKEN_SECRET + " secret",
      401);
  }

  let payload;
  try {
    payload = JSON.parse(await request.text());
  } catch (err) {
    return ingestError("400 bad request: the body is not valid JSON", 400);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return ingestError("400 bad request: the body is not a record", 400);
  }
  if (payload.schema_version !== DIGEST_SCHEMA_VERSION) {
    return ingestError(
      "400 bad request: schema_version is " + String(payload.schema_version) +
        " and this Worker accepts only " + DIGEST_SCHEMA_VERSION, 400);
  }
  const date = String(payload.date || "");
  const slot = String(payload.slot || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return ingestError(
      "400 bad request: date " + JSON.stringify(date) + " is not YYYY-MM-DD", 400);
  }
  if (DIGEST_SLOTS.indexOf(slot) < 0) {
    return ingestError(
      "400 bad request: slot " + JSON.stringify(slot) + " is not one of " +
        DIGEST_SLOTS.join(", "), 400);
  }
  const key = DIGEST_ARTIFACT_PREFIX + date + ":" + slot;
  if (payload.key !== undefined && String(payload.key) !== key) {
    return ingestError(
      "400 bad request: the declared key " + JSON.stringify(String(payload.key)) +
        " disagrees with date " + date + " / slot " + slot + ", which address " +
        "it as " + key, 400);
  }

  // The click target is the only thing in this record that a reader follows, so
  // it is checked at the boundary rather than trusted: an artifact pointing off
  // the archive host is the shape a phishing push takes.
  const pageUrl = String(payload.page_url || "");
  let pageHost = "";
  try {
    pageHost = new URL(pageUrl).hostname.toLowerCase();
  } catch (err) {
    pageHost = "";
  }
  if (pageHost !== digestArchiveHost()) {
    return ingestError(
      "400 bad request: page_url host " +
        JSON.stringify(pageHost || "(unreadable)") +
        " is not the digest archive host " + digestArchiveHost(), 400);
  }

  if (!env || !env.KV) {
    return ingestError("500: no KV binding on this Worker", 500);
  }
  await env.KV.put(key, JSON.stringify(payload),
                   { expirationTtl: ARTIFACT_TTL_S });
  console.log(
    "digest ingest: stored " + key + " (partial=" +
      (payload.is_partial === true) + ", bytes=" + String(payload).length +
      ", ttl=" + ARTIFACT_TTL_S + "s)"
  );
  return new Response(JSON.stringify({ ok: true, key: key }) + "\n", {
    status: 200,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function ingestError(message, status) {
  return new Response(JSON.stringify({ ok: false, error: message }) + "\n", {
    status: status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

// ---- ?deliver_now=1 -----------------------------------------------------------

// An authenticated probe over the whole delivery path, with the ledger writer
// passed as null. Everything is exercised — the ledger read, validation, the
// sequence_id read-back, the publish, the retry budget — and the ONE thing it
// cannot do is write `digest:ledger`, because the write is injected by the cron
// arm and this passes null. That is the same argument as the heartbeat placement
// comment: a probe that could refresh the guard would permanently mask a real
// double-push, so the refresh must be unreachable from any fetch path, not
// merely unused.
async function handleDeliverNow(request, env) {
  if (request.method !== "POST" && request.method !== "GET") {
    return ingestError("this endpoint accepts GET or POST only", 405);
  }
  const expected = env && typeof env[INGEST_TOKEN_SECRET] === "string"
    ? env[INGEST_TOKEN_SECRET]
    : "";
  const provided = request.headers.get(INGEST_TOKEN_HEADER) || "";
  // Fail closed on an unconfigured secret, for the same reason as the ingest
  // endpoint above: comparing against "" would accept a request that offers no
  // credential at all.
  if (!expected) {
    console.error(
      "DIGEST_INGEST_MISCONFIGURED: the " + INGEST_TOKEN_SECRET +
        " secret is missing or empty on this Worker, so the delivery probe is " +
        "being refused (FAIL-CLOSED)"
    );
    return ingestError(
      "401 unauthorized: the " + INGEST_TOKEN_SECRET + " secret is not " +
        "configured on this Worker",
      401);
  }
  const authorised = await constantTimeEquals(provided, expected);
  if (!authorised) {
    return ingestError(
      "401 unauthorized: the " + INGEST_TOKEN_HEADER + " header did not match " +
        "the " + INGEST_TOKEN_SECRET + " secret",
      401);
  }
  const url = new URL(request.url);
  const slot = url.searchParams.get("slot") || "morning";
  const outcome = await deliverDigestPlan(env, slot, null);
  return new Response(
    JSON.stringify({
      ok: outcome.ok !== false,
      probe: true,
      ledger_written: false,
      slot: slot,
      outcome: outcome,
    }) + "\n",
    {
      status: 200,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
}

// ---- ?deliver_test=1 ----------------------------------------------------------

// The title of the one labelled probe message. Quoted here so a test can assert
// the exact string without restating it, and so nobody "improves" the wording in
// the payload without changing what the owner recognises on their phone.
const DELIVER_TEST_TITLE = "Worker delivery test - ignore";

// ONE push, on the REAL publish function, labelled so it cannot be mistaken for
// a delivery. `?deliver_now=1` exercises the whole plan but pushes whatever the
// delivery logic produced — right now a priority-5 "digest unavailable" — which
// alarms the owner's phone and still proves nothing about the transport. This
// calls publishToNtfy, the same function deliverDigest uses, with the same topic,
// the same server and the same headers, and reports the status the Worker
// itself recorded.
//
// IT TOUCHES NO LEDGER, NOT EVEN A READ. The probe's whole justification is
// that it cannot disturb a real delivery, and the ledger is what the duplicate
// guard rests on, so this route never goes near it and — like ?deliver_now=1 —
// never re-plumbs the ledger writer into a fetch path.
//
// Priority 3, ntfy's `default`: it appears on the phone without ringing or
// alerting. Priority 5 is what makes a real failure unmissable, which is exactly
// why a test must never use it.
async function handleDeliverTest(request, env) {
  if (request.method !== "POST" && request.method !== "GET") {
    return ingestError("this endpoint accepts GET or POST only", 405);
  }
  const expected = env && typeof env[INGEST_TOKEN_SECRET] === "string"
    ? env[INGEST_TOKEN_SECRET]
    : "";
  const provided = request.headers.get(INGEST_TOKEN_HEADER) || "";
  // Fail closed on an unconfigured secret, for the same reason as the two
  // endpoints above: comparing against "" would accept a request that offers no
  // credential at all, and this one can post to the owner's phone.
  if (!expected) {
    console.error(
      "DIGEST_INGEST_MISCONFIGURED: the " + INGEST_TOKEN_SECRET +
        " secret is missing or empty on this Worker, so the delivery test is " +
        "being refused (FAIL-CLOSED)"
    );
    return ingestError(
      "401 unauthorized: the " + INGEST_TOKEN_SECRET + " secret is not " +
        "configured on this Worker",
      401);
  }
  if (!(await constantTimeEquals(provided, expected))) {
    return ingestError(
      "401 unauthorized: the " + INGEST_TOKEN_HEADER + " header did not match " +
        "the " + INGEST_TOKEN_SECRET + " secret",
      401);
  }
  const topic = ntfyTopicOf(env);
  // Cannot collide with `dmn-<date>-<slot>` or `dmn-<date>-<slot>-full`, so the
  // read-back guard can never read this probe as a real slot's send.
  const sequenceId = "dmn-delivery-test-" + nptDateFromInstant(Date.now());
  const body = "Delivery-path test from the Cloudflare Worker.\n\n" +
    "This is NOT a digest. No digest was built, none was affected, and the " +
    "delivery ledger was not read or written. It was sent to check whether " +
    "this Worker can reach ntfy at all. Ignore it.";
  const payload = digestPayload(topic, DELIVER_TEST_TITLE, body, "", 3, []);
  const published = await publishToNtfy(env, payload, sequenceId,
    "delivery test", { captureErrorBody: true });
  return new Response(
    JSON.stringify({
      ok: published.ok === true,
      probe: true,
      ledger_written: false,
      sequence_id: sequenceId,
      // The Worker's OWN record of ntfy's answer. Reported rather than assumed,
      // because "the route returned 200" and "ntfy accepted the push" are two
      // different claims and only this one is evidence.
      ntfy_http_status: published.http_status,
      ntfy_message_id: published.message_id || "",
      attempts: published.attempts,
      error: published.error || "",
      ntfy_error_body: published.error_body || "",
    }) + "\n",
    {
      status: 200,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
}

// ---- ?health=1 digest fields --------------------------------------------------

// The digest side of ?health=1, so the watchdog and a human can see whether a
// digest was handed over, how old it is, and what was last pushed — without
// reading the ledger by hand.
//
// A null age means "not present or not readable" and never 0: 0 is a pass, and
// a KV outage must not be able to render as one. That is why `unreadable` is
// reported as a list of key names rather than left to be guessed from the ages.
async function digestHealthFields(env, nowMs) {
  const today = nptDateFromInstant(nowMs);
  const out = {
    artifact_morning_present: false,
    artifact_evening_present: false,
    artifact_morning_age_s: null,
    artifact_evening_age_s: null,
    ledger_age_s: null,
    last_delivery: null,
    unreadable: [],
    cron_arm: DIGEST_CRON_ARM,
    delivery_arm_enabled: DIGEST_CRON_ARM === "deliverDigest",
    on_time_s: DELIVERY_ON_TIME_S,
    retention_days: DELIVERY_RETENTION_DAYS,
  };
  if (!env || !env.KV) {
    out.unreadable.push("all");
    return out;
  }
  for (const slot of DIGEST_SLOTS) {
    const key = DIGEST_ARTIFACT_PREFIX + today + ":" + slot;
    try {
      const raw = await env.KV.get(key);
      const parsed = raw ? JSON.parse(String(raw)) : null;
      const built = parsed ? Date.parse(String(parsed.built_at || "")) : NaN;
      const present = !!parsed;
      out["artifact_" + slot + "_present"] = present;
      out["artifact_" + slot + "_age_s"] = Number.isFinite(built)
        ? Math.max(0, Math.round((nowMs - built) / 1000))
        : null;
    } catch (err) {
      out.unreadable.push(key);
      console.log("?health=1 could not read " + key + ":", String(err));
    }
  }
  try {
    const ledger = normaliseDigestLedger(await env.KV.get(DIGEST_LEDGER_KEY));
    const asOf = Date.parse(String(ledger.as_of || ""));
    out.ledger_age_s = Number.isFinite(asOf)
      ? Math.max(0, Math.round((nowMs - asOf) / 1000))
      : null;
    out.last_delivery = latestLedgerDelivery(ledger);
  } catch (err) {
    out.unreadable.push(DIGEST_LEDGER_KEY);
    console.log("?health=1 could not read " + DIGEST_LEDGER_KEY + ":", String(err));
  }
  return out;
}

// The most recent entry the ledger holds, by recorded instant rather than by
// iteration order: a ledger is a list of facts about time and the newest one is
// the latest instant, not the last key inserted.
function latestLedgerDelivery(ledger) {
  let best = null;
  // -Infinity, NOT NaN. `at >= NaN` is false for every at, including every
  // real instant, so a NaN seed here does not fail loudly — it silently
  // reports "no delivery has ever been recorded" for a ledger that is full of
  // them. A blank that reads like good news is the exact failure this module
  // exists to prevent, and it is why the seed is a number that can lose.
  let bestAt = -Infinity;
  let bestDate = null;
  let bestSlot = null;
  const days = (ledger && ledger.days) || {};
  for (const date of Object.keys(days)) {
    const day = days[date] || {};
    for (const slot of Object.keys(day)) {
      const entry = day[slot];
      if (!entry || typeof entry !== "object") continue;
      const at = Date.parse(String(entry.ntfy_sent_at || entry.pending_at || ""));
      if (Number.isFinite(at) && at >= bestAt) {
        bestAt = at;
        best = entry;
        bestDate = date;
        bestSlot = slot;
      }
    }
  }
  if (!best) return null;
  const delta = deliveryDeltaSeconds(
    best.first_ntfy_sent_at || best.ntfy_sent_at,
    Date.parse(String(best.scheduled_instant || "")));
  return {
    date: bestDate,
    slot: bestSlot,
    kind: best.kind || null,
    state: best.state || null,
    sequence_id: best.sequence_id || null,
    ntfy_sent_at: best.ntfy_sent_at || null,
    delta_s: delta,
    on_time: delta !== null && delta <= DELIVERY_ON_TIME_S,
  };
}

// --- main pass ----------------------------------------------------------------

async function handle(env) {
  // Boundary validation: without both bindings the worker cannot dedup or
  // dispatch — refuse to run instead of alert-spamming every 5 minutes.
  if (!env || !env.KV || !env.GH_TOKEN) {
    console.error(
      "misconfigured: KV binding and/or GH_TOKEN secret missing — skipping run"
    );
    return { ok: false, reason: "missing KV binding or GH_TOKEN secret" };
  }

  // 1) Fetch all feeds (Google News + direct publisher RSS) in parallel;
  //    individual failures are tolerated.
  const settled = await Promise.allSettled(
    ALL_FEEDS.map(async (feed) => {
      const res = await fetchWithTimeout(feed.url, FETCH_TIMEOUT_MS);
      if (!res.ok) throw new Error("HTTP " + res.status);
      return { feed, xml: await res.text() };
    })
  );

  // 2) Collect fresh (<30 min old) items from successful feeds. The
  //    seenTitles set dedups identical headlines ACROSS sources within one
  //    run (first fetch wins), so the same story surfacing on both a gnews
  //    and a direct RSS feed produces a single candidate/dispatch.
  const fresh = [];
  const seenTitles = new Set();
  for (const result of settled) {
    if (result.status !== "fulfilled") {
      console.log("feed fetch failed:", String(result.reason));
      continue;
    }
    try {
      for (const item of parseFeedItems(result.value.xml)) {
        if (!seenTitles.has(item.title)) {
          seenTitles.add(item.title);
          fresh.push({
            ...item,
            source: result.value.feed.name,
            detection_source: result.value.feed.detection_source,
          });
        }
      }
    } catch (err) {
      console.log("feed parse failed:", String(err));
    }
  }
  const rssFresh = fresh.filter((i) => i.detection_source === "rss").length;
  // The freshness caveat, counted rather than asserted in a comment. A feed that
  // goes dateless is visible here; it used to be invisible, which is how 40
  // kathmandupost headlines a day vanished with the code reading as if they
  // were being ingested.
  const undated = fresh.filter((i) => i.undated).length;
  // Per-feed fresh counts. With one feed per query term the sweep has to be
  // able to say WHICH term is dry, otherwise "6 fresh items" reads identically
  // for six live feeds and for one live feed and five dead ones.
  const perFeed = {};
  for (const item of fresh) {
    perFeed[item.source] = (perFeed[item.source] || 0) + 1;
  }
  console.log(
    "fresh items (<=30min): " + fresh.length +
      " (" + rssFresh + " via direct rss, " + undated + " undated)" +
      " from " +
      settled.filter((r) => r.status === "fulfilled").length +
      "/" + ALL_FEEDS.length + " feeds"
  );
  console.log("fresh items per feed: " + JSON.stringify(perFeed));

  // 3) THE GATE. importanceGate() runs on the SHARED title+snippet blob
  //    (gateBlob), never on the bare title, and it is now a real port of
  //    scripts/breaking_monitor.py's importance_gate(blob, title) -- the same
  //    three vocabulary tiers, the same framing veto and bare-label test, in the
  //    same order.
  //
  //    Until 2026-10-08 this block matched EMERGENCY_RE alone -- 13 literal
  //    phrases -- while the comment above it claimed it mirrored importance_gate.
  //    It did not, and because the Worker runs FIRST it was the binding
  //    constraint: measured over tests/breaking/fixtures/real_headlines.json
  //    (616 rows) the old block hit 33 against Python's 378, so 345 real
  //    headlines were dropped here and never reached the classifier. The claim
  //    was the defect: a comment asserting a mirror nobody maintained.
  const tripped = [];
  for (const item of fresh) {
    const verdict = importanceGate(gateBlob(item), item.title);
    if (!verdict.tripped) {
      // Suppression is logged by reason, so a widened gate's misses are visible
      // instead of being indistinguishable from a quiet news cycle.
      if (verdict.why === "sport_gossip_dominating") {
        console.log('suppressed (sport/gossip): "' + item.title + '"');
      }
      continue;
    }
    tripped.push(item);
  }

  // 4) KV dedup, then AT MOST ONE dispatch per run (the top tripped candidate
  //    that is not already alerted) to avoid spam. Keys are marked AFTER a
  //    successful dispatch so a failed POST is retried on the next cron tick
  //    instead of being lost.
  let dispatched = false;
  const dispatchedTitles = [];
  for (const item of tripped) {
    const key = await sha1Hex(item.title);
    if (await env.KV.get(key)) {
      console.log('dedup skip (already alerted): "' + item.title + '"');
      continue;
    }
    if (await dispatchAlert(env, item, tripped.length)) {
      dispatched = true;
      dispatchedTitles.push(item.title);
      // Mark EXACTLY the item that was dispatched. Nothing else.
      //
      // This line used to be `await Promise.all(tripped.map(...))`: one
      // dispatch of tripped[0], then a dedup key written for EVERY tripped
      // candidate. Candidates 2..N were therefore stamped "already alerted"
      // for 6h having never been alerted at all — a silent, self-inflicted
      // loss of every real story after the first in the same sweep, and the
      // exact shape of the defect that burned the owner's exam result. One
      // dispatch, one key, and the count below states exactly how many are
      // still alertable.
      await env.KV.put(await sha1Hex(item.title), "1", {
        expirationTtl: DEDUP_TTL_SECONDS,
      });
      break; // single dispatch per run
    }
    console.log('dispatch failed for "' + item.title + '", trying next candidate');
  }

  const summary = {
    ok: true,
    fresh_items: fresh.length,
    fresh_per_feed: perFeed,
    // The freshness caveat as a NUMBER, so a human reading ?run=1 can see how
    // much of the candidate pool had no verifiable timestamp at all.
    fresh_undated: undated,
    tripped: tripped.length,
    dispatched,
    // "tripped:7, dispatched:1" alone reads like six stories were dropped.
    // Naming the one title that was sent makes the reading explicit: the other
    // six are still unmarked and still alertable on the next sweep.
    dispatched_titles: dispatchedTitles,
  };
  console.log("run summary: " + JSON.stringify(summary));
  return summary;
}

// --- Academic sweep ------------------------------------------------------------
//
// SITE-CONSTANT <title> - does the Worker share the Python burn? NO, and this
// comment is the durable record of that decision.
//
// scripts/check_academic_result.py reads the notice title from the DETAIL page,
// and on the live IOST site the detail page's <title> element is the site-wide
// constant "INSTITUTE OF SCIENCE AND TECHNOLOGY" - the real title is in the
// body (<h4 class="title"> inside .inner-banner-detail / .detail-page-inner,
// plus a <p> in .ck-table). Reading <title> there burned the sem-1 2082 result
// permanently on 2026-09-28: the constant yields no semester, the run recorded
// `irrelevant` / `no_semester_match`, and under R4 a decided notice is never
// re-fetched. The Python side now parses the body and treats a constant or
// absent title as unextractable -> deferred, never decided.
//
// The Worker NEVER reads a detail page. parseAcademicNotices below parses the
// LIST page and takes each title from its anchor's inner text, which on the
// live board carries the real notice title, and it refuses to decide on a
// degraded (empty) anchor via titleDegraded. So there is nothing to mirror
// here, and adding a detail-page fetch to this path would only give the bug
// somewhere new to live.
//
// The Worker is NOT exempt from the no-burn contract, though: it still
// dispatches on the LIST title, so a board index that truncated or mislabelled a
// title would still mis-trigger. The mitigation is unchanged and still
// load-bearing: this path only fires repository_dispatch ("academic-result"),
// and the WORKFLOW re-reads the authoritative detail page and downloads the PDF
// before announcing anything. A wrong Worker dispatch costs a wasted run, never
// a wrong verdict. Do not "optimise" by moving the verdict into the Worker -
// that is precisely what would make the burn reachable here.

// Parse the IOST notice list page into [{id, title, titleDegraded, url}]. Robust
// regex scan (mirrors parse_notice_list in scripts/check_academic_result.py):
// finds anchors whose href points at a notice detail page (trailing numeric id)
// and pulls the title from the anchor's inner text.
//
// titleDegraded is the R1 load-bearing flag. When the anchor carried no inner
// text the historical code fabricated "notice-<id>" and this file's caller then
// decided "not a result" against that placeholder — that fabricated string IS
// the original burn. The placeholder is still produced (display only) but the
// flag makes the title non-authoritative, so the id is deferred, never skipped.
function parseAcademicNotices(html) {
  const notices = [];
  const seen = new Set();
  let m;
  ACADEMIC_ANCHOR_RE.lastIndex = 0;
  while ((m = ACADEMIC_ANCHOR_RE.exec(html)) !== null) {
    const href = (m[1] || "").trim();
    const inner = (m[2] || "")
      .replace(/<[^>]+>/g, "")
      .replace(/\s+/g, " ")
      .trim();
    const idm = ACADEMIC_NOTICE_ID_RE.exec(href);
    if (!idm) continue;
    const nid = parseInt(idm[1], 10);
    if (!Number.isFinite(nid) || seen.has(nid)) continue;
    seen.add(nid);
    notices.push({
      id: nid,
      title: inner || ("notice-" + nid),
      titleDegraded: !inner,
      url: href,
    });
  }
  notices.sort((a, b) => a.id - b.id);
  return notices;
}

// Extract the semester number (1-8) from a matched title, or null. Mirrors
// _semester_from_token in scripts/check_academic_result.py.
function academicSemesterFromToken(token) {
  const low = String(token).toLowerCase();
  const arabicWords = {
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "6th": 6, "7th": 7, "8th": 8,
    first: 1, second: 2, third: 3, fourth: 4, fifth: 5,
    sixth: 6, seventh: 7, eighth: 8,
  };
  if (low in arabicWords) return arabicWords[low];
  const roman = {
    I: 1, II: 2, III: 3, IV: 4, V: 5, VI: 6, VII: 7, VIII: 8,
  };
  const up = String(token).toUpperCase();
  if (up in roman) return roman[up];
  if (/^[1-8]$/.test(token)) return parseInt(token, 10);
  return null;
}

// Match a notice title against the CSIT semester-result pattern. Returns the
// semester number (1-8) or null. Mirrors match_semester in the Python script.
// F3: stage 1 proves marker + semester token + result term in ANY order;
// stage 2 extracts the semester from the WHOLE title (the stage-1 match is
// zero-width, so there is no matched span to mine).
function academicMatchSemester(title) {
  if (!title) return null;
  if (!ACADEMIC_SEMESTER_RE.test(title)) return null;
  const tm = ACADEMIC_SEM_TOKEN_RE.exec(title);
  if (!tm) return null;
  return academicSemesterFromToken(tm[0]);
}

// --- R5: the gap-free floor, as a PURE function -----------------------------
// Mirrors advance() in scripts/check_academic_result.py. The floor advances to
// N iff every id m with floor < m < N is either decided (present in `examined`
// with a non-deferred outcome) or absent from `listIds`. It is NEVER set to
// notices[last].id — that unconditional max() write is the burn.
function academicAdvance(floor, examined, listIds) {
  const base = Number.isFinite(floor) ? Math.trunc(floor) : 0;
  const decided = new Set();
  const recorded = new Set();
  for (const [key, val] of Object.entries(examined || {})) {
    const nid = parseInt(key, 10);
    if (!Number.isFinite(nid)) continue;
    recorded.add(nid);
    const outcome = val && typeof val === "object" ? val.outcome : null;
    if (outcome && outcome !== "deferred") decided.add(nid);
  }
  const present = new Set();
  for (const id of listIds || []) {
    const nid = parseInt(id, 10);
    if (Number.isFinite(nid)) present.add(nid);
  }
  // A candidate is a decided id, or a listed id with no record at all. A
  // listed id recorded `deferred` is undecided: it blocks the floor and is
  // retried (R1 + R3).
  //
  // THE GAP SCAN IS ANCHORED ON `base` (the ORIGINAL floor), NEVER on the
  // moving `cand`. This is not a style choice. Scanning from the moving
  // candidate makes the scan vacuous for an unrecorded-but-present id sitting
  // immediately after cand, because the loop body never executes when
  // nid === cand + 1 - so consecutive undecided ids walk straight past each
  // other and the function degenerates to returning max(listIds), which is
  // exactly the historical burn signature. A divergence here once made the
  // Worker return 5 where the Python returned 2 for identical inputs. Keep
  // this in step with advance() in scripts/check_academic_result.py;
  // tests/academic/test_worker_parity.py EXECUTES this function via node and
  // fails if the two ever disagree.
  const candidates = new Set(decided);
  for (const nid of present) if (!recorded.has(nid)) candidates.add(nid);

  const sorted = Array.from(candidates).sort((a, b) => a - b);
  let cand = base;
  for (const nid of sorted) {
    if (nid <= cand) continue;
    let blocked = false;
    for (let m = base + 1; m < nid; m++) {
      if (decided.has(m)) continue;
      if (present.has(m)) {
        blocked = true;
        break;
      }
    }
    if (!blocked) cand = nid;
  }
  return cand;
}

// Append a decision for one notice id and cap the map (lowest ids evicted
// first). `deferred` is NOT a decision — it means "could not decide" and must
// be retried on the next tick.
function academicRecord(examined, id, outcome, reason) {
  examined[String(id)] = { outcome: outcome, reason: reason };
  const keys = Object.keys(examined);
  if (keys.length > ACADEMIC_EXAMINED_CAP) {
    keys
      .map((k) => [parseInt(k, 10), k])
      .filter((p) => Number.isFinite(p[0]))
      .sort((a, b) => a[0] - b[0])
      .slice(0, keys.length - ACADEMIC_EXAMINED_CAP)
      .forEach((p) => delete examined[p[1]]);
  }
}

// Fire ONE repository_dispatch ("academic-result") for a matched notice.
async function dispatchAcademic(env, notice, semester) {
  // THE QUOTA GATE, before the repository_dispatch that bills an academic run.
  const budget = await budgetChargeDispatch(env, "academic", Date.now());
  if (!budget.allow) {
    console.error(
      "academic dispatch refused (" + budget.reason + "): the month is at the " +
      "hard ceiling, so the floor is NOT advanced and this notice is retried " +
      "on a later sweep"
    );
    return false;
  }
  const res = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      Authorization: "Bearer " + env.GH_TOKEN,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "daily-media-news-academic-worker",
    },
    body: JSON.stringify({
      event_type: "academic-result",
      client_payload: {
        notice_id: notice.id,
        title: notice.title,
        url: notice.url,
        semester: semester,
      },
    }),
  });
  console.log(
    'academic dispatch notice ' + notice.id +
      ' ("' + notice.title + '", sem ' + semester + ') -> HTTP ' + res.status
  );
  return res.ok;
}

// Cheap tripwire over the TU IOST Result feed. Detects NEW CSIT semester
// result notices and hands off to the GitHub workflow. Errors are logged and
// swallowed so a TU outage never crashes the worker — it just retries next
// tick (the floor is only advanced on success, and only through R5).
async function handleAcademic(env) {
  if (!env || !env.KV || !env.GH_TOKEN) {
    console.error(
      "academic sweep skipped: KV binding and/or GH_TOKEN secret missing"
    );
    return { ok: false, reason: "missing KV binding or GH_TOKEN secret" };
  }

  // 1) Fetch the Result feed.
  let res;
  try {
    res = await fetchWithTimeout(ACADEMIC_FEED_URL, FETCH_TIMEOUT_MS);
  } catch (err) {
    console.log("academic feed fetch failed (TU down?):", String(err));
    return { ok: false, reason: "feed fetch failed" };
  }
  if (!res.ok) {
    console.log("academic feed HTTP " + res.status + " — retry next tick");
    return { ok: false, reason: "feed HTTP " + res.status };
  }
  let html;
  try {
    html = await res.text();
  } catch (err) {
    console.log("academic feed read failed:", String(err));
    return { ok: false, reason: "feed read failed" };
  }

  // 2) Parse notice cards.
  let notices;
  try {
    notices = parseAcademicNotices(html);
  } catch (err) {
    console.log("academic feed parse failed:", String(err));
    return { ok: false, reason: "feed parse failed" };
  }
  if (!notices.length) {
    console.log("academic sweep: no notices parsed");
    return { ok: true, notices: 0, dispatched: 0 };
  }

  // 3) Read the floor + the examined map ONCE. The map is written ONCE at
  //    the end, so a mid-run crash can never leave partial state behind.
  //    The two RAW values are kept (not just the parsed forms) because the
  //    write block below is a read-compare-write against exactly what KV just
  //    returned — that comparison is what makes an unchanged sweep free.
  let floor = 0;
  let examined = {};
  let legacy = null;
  // null = the key has never been written in this namespace. floorStored is
  // the on-disk value normalised to what we WOULD have written, so a floor
  // stored as "0042" does not count as a change against a floor of 42.
  // examinedCanonical is the stored map re-serialised, so the comparison below
  // is SEMANTIC rather than byte-wise (see the comment at its assignment).
  let examinedRaw = null;
  let floorStored = null;
  let examinedCanonical = null;
  try {
    const floorRaw = await env.KV.get(ACADEMIC_LAST_EXAMINED_KEY);
    if (floorRaw !== null && floorRaw !== undefined && floorRaw !== "") {
      const parsed = parseInt(floorRaw, 10);
      if (Number.isFinite(parsed)) {
        floor = parsed;
        floorStored = String(floor);
      } else {
        // Unparseable floor on disk: never trust it, and rewrite it.
        floorStored = String(floorRaw);
      }
    }
    examinedRaw = (await env.KV.get(ACADEMIC_EXAMINED_KEY)) || null;
    if (examinedRaw) {
      const parsed = JSON.parse(examinedRaw);
      if (parsed && typeof parsed === "object") examined = parsed;
      // Re-serialise what is stored. Comparing raw BYTES would report a
      // change for a map that is semantically identical but formatted
      // differently ({"a": 1} vs {"a":1}), and would then rewrite the key on
      // EVERY sweep — reintroducing the burn it is meant to remove. JSON.parse
      // preserves key order, so a same-order store matches exactly; a
      // reordered one costs a single self-healing rewrite and then stabilises.
      examinedCanonical = JSON.stringify(parsed);
    } else {
      // Migration: the legacy high-water mark is KNOWN-UNRELIABLE — it could
      // have advanced past notices nobody ever read, so trusting it as a floor
      // would keep the burn alive. Open an honest replay window instead.
      legacy = await env.KV.get(ACADEMIC_LEGACY_LAST_CHECKED_KEY);
    }
  } catch (err) {
    console.log("academic KV read failed:", String(err));
    return { ok: false, reason: "KV read failed" };
  }
  if (legacy !== null && legacy !== undefined && legacy !== "") {
    const old = parseInt(legacy, 10);
    const base = Number.isFinite(old) ? old : 0;
    floor = Math.max(0, base - ACADEMIC_REPLAY_WINDOW);
    examined = {};
    console.log("academic: replay window opened (" + ACADEMIC_REPLAY_WINDOW + ")");
  }

  // 4) Un-examined notices, ASCENDING, capped (R2). The list is NOT truncated
  //    anywhere: R5 needs the full parsed list or it reads "absent because
  //    truncated" as "absent because nonexistent" and burns notices.
  const listIds = notices.map((n) => n.id);
  const decided = new Set();
  for (const [key, val] of Object.entries(examined)) {
    const nid = parseInt(key, 10);
    if (!Number.isFinite(nid)) continue;
    if (val && typeof val === "object" && val.outcome && val.outcome !== "deferred") {
      decided.add(nid);
    }
  }
  const byId = new Map(notices.map((n) => [n.id, n]));
  let pending = listIds.filter((id) => id > floor && !decided.has(id));
  let pendingOverflow = false;
  if (pending.length > ACADEMIC_MAX_PENDING) {
    pendingOverflow = true;
    console.log(
      "academic pending overflow — " + pending.length +
        " unexamined notices, processing the " + ACADEMIC_MAX_PENDING + " lowest"
    );
    pending = pending.slice(0, ACADEMIC_MAX_PENDING);
  }

  let dispatched = 0;
  let stoppedEarly = false;
  let lastAttempted = floor;

  for (const id of pending) {
    lastAttempted = Math.max(lastAttempted, id);
    const notice = byId.get(id);
    const title = (notice && notice.title) || "";

    // R1: a degraded/empty title is NOT an authoritative title. Record it as
    // deferred and retry next tick — never decide against a placeholder.
    if (!title || (notice && notice.titleDegraded)) {
      academicRecord(examined, id, "deferred", "title_unparseable");
      console.log("academic notice " + id + " has no authoritative title — deferred");
      stoppedEarly = true; // R3: stop here, leave the rest for the next tick
      break;
    }
    // A pre-filter miss on a REAL title is a positive decision (R1/R4): the
    // title was read, and it is not a CSIT semester result.
    const semester = academicMatchSemester(title);
    if (semester === null) {
      academicRecord(examined, id, "irrelevant", "no_semester_match");
      continue;
    }

    // Dedup: skip if this notice was already dispatched successfully.
    const dedupKey = ACADEMIC_DISPATCHED_PREFIX + id;
    let alreadyDone = false;
    try {
      alreadyDone = Boolean(await env.KV.get(dedupKey));
    } catch (err) {
      // A KV read failure is a transport failure, not a decision. Defer and
      // stop (R3) instead of `continue`-ing past it and burning the notice.
      console.log("academic dedup read failed:", String(err));
      academicRecord(examined, id, "deferred", "dispatch_failed");
      stoppedEarly = true;
      break;
    }
    if (alreadyDone) {
      console.log("academic dedup skip (already dispatched): notice " + id);
      academicRecord(examined, id, "already_announced", "already_announced");
      continue;
    }

    if (await dispatchAcademic(env, notice, semester)) {
      dispatched++;
      academicRecord(examined, id, "processed", "result_announced");
      try {
        await env.KV.put(dedupKey, "1");
      } catch (err) {
        console.log("academic dedup write failed:", String(err));
      }
    } else {
      console.log("academic dispatch failed for notice " + id + " — retry next tick");
      academicRecord(examined, id, "deferred", "dispatch_failed");
      stoppedEarly = true; // do not advance past a failed dispatch
      break;
    }
  }

  // 5) Advance the floor through R5 — only after a loop that never broke. The
  //    old code wrote notices[notices.length-1].id UNCONDITIONALLY, which
  //    skipped every id the loop had not reached. R5's literal wording is
  //    permissive about the candidate itself, so R2's cap is applied too:
  //    the floor never passes the last id this run actually attempted. This is
  //    the same two-clause clamp run() applies in check_academic_result.py.
  //    The clamp is a belt, not the fix: the gap scan inside academicAdvance is
  //    anchored on the ORIGINAL floor precisely so the clamp is not
  //    load-bearing. Do not "simplify" the scan back to the moving candidate.
  const computed = academicAdvance(floor, examined, listIds);
  const newFloor = Math.min(computed, Math.max(floor, lastAttempted));

  // 6) CONDITIONAL state writes — the write-budget fix.
  //
  // WHY: this sweep runs 720x/day and used to write BOTH state keys
  // unconditionally, every single time: ~1,440 writes/day against Cloudflare's
  // FREE-tier limit of 1,000 writes/day. The account therefore ran out of
  // writes every day, and once the quota is exhausted Cloudflare REJECTS
  // writes, so the floor could never advance again. The values are almost
  // always identical to what is already stored, so those writes were waste.
  //
  // THE TRADE-OFF: reads for writes, deliberately. Both values were ALREADY
  // read earlier in this same run, so this costs ZERO extra reads — the
  // comparison rides along on reads the sweep had to make anyway. KV's free
  // tier allows ~100,000 reads/day against 1,000 writes/day, so a read is
  // ~100x cheaper than a write. Spending reads to save writes is the correct
  // direction; the reverse would be fatal.
  //
  // This is a read-compare-write against what THIS run read back from KV, NOT
  // an in-memory "last written" cache. Worker isolates are evicted at will, so
  // a cache would become load-bearing for correctness and could silently skip
  // a state commit after an eviction. Correctness must never depend on isolate
  // lifetime, so there is no cache at all — the read we already perform is
  // strictly more correct than one would have been, and it is free.
  //
  // A CHANGED sweep costs exactly what it always cost (the writes it needs).
  // An UNCHANGED sweep costs zero. Nothing is ever skipped: a skipped write is
  // by definition a write whose value already equals the stored value.
  const ledger = { written: 0, skipped: 0 };
  const examinedNext = JSON.stringify(examined);

  // COMMIT ORDER: the examined map first, the floor second. A write failure on
  // either key must leave the floor exactly where it was so the next tick
  // re-examines instead of skipping (R3, and the fail-safe at the top of this
  // function). Writing the floor first could advance it and THEN fail on the
  // map — and an advanced floor IS the burn this entire path exists to
  // prevent, so that ordering was a latent hole and is now closed.
  if (examinedCanonical !== null && examinedCanonical === examinedNext) {
    ledger.skipped += 1;
    console.log("academic KV write skipped (examined map unchanged)");
  } else {
    try {
      await env.KV.put(ACADEMIC_EXAMINED_KEY, examinedNext);
      ledger.written += 1;
    } catch (err) {
      console.log("academic state write failed (examined map):", String(err));
      return { ok: false, reason: "KV state write failed" };
    }
  }

  if (floorStored !== null && floorStored === String(newFloor)) {
    ledger.skipped += 1;
    console.log("academic KV write skipped (floor unchanged at " + newFloor + ")");
  } else {
    try {
      await env.KV.put(ACADEMIC_LAST_EXAMINED_KEY, String(newFloor));
      ledger.written += 1;
    } catch (err) {
      console.log("academic state write failed (floor):", String(err));
      return { ok: false, reason: "KV state write failed" };
    }
  }

  const summary = {
    ok: true,
    notices: notices.length,
    dispatched: dispatched,
    floor: newFloor,
    pendingOverflow: pendingOverflow,
    stoppedEarly: stoppedEarly,
    kvWrites: ledger.written,
    kvWritesSkipped: ledger.skipped,
  };
  ACADEMIC_LAST_SWEEP = summary;
  console.log("academic run summary: " + JSON.stringify(summary));
  return summary;
}

// Read the academic floor for the status page. This is a READ only, and it
// echoes the raw floor value — never a secret, never the examined map.
// "absent" is a real and important answer rather than a cosmetic one: it means
// no sweep has EVER committed a floor to the namespace this Worker is actually
// bound to, which is the exact signature of a wrong-namespace binding or of
// every write being rejected by the daily quota.
async function academicFloorForStatus(env) {
  if (!env || !env.KV) return "unknown (no KV binding)";
  try {
    const raw = await env.KV.get(ACADEMIC_LAST_EXAMINED_KEY);
    if (raw === null || raw === undefined || raw === "") {
      return "absent (no sweep has ever committed a floor here)";
    }
    return String(raw);
  } catch (err) {
    return "unreadable (" + String(err) + ")";
  }
}

// Read the last dispatch outcome for the status page. READS only, never a
// secret. A FAILED dispatch is the whole point: an expired or under-scoped
// GH_TOKEN produces no error anywhere else the owner would look, and the only
// symptom is that alerts stopped arriving.
async function breakingDispatchForStatus(env) {
  const none = {
    last: "none recorded yet (no dispatch attempt has ever reached KV)",
    lastOk: "none recorded yet (no dispatch has ever succeeded)",
  };
  if (!env || !env.KV) {
    return {
      last: "unknown (no KV binding)",
      lastOk: "unknown (no KV binding)",
    };
  }
  try {
    const raw = await env.KV.get(BREAKING_LAST_DISPATCH_KEY);
    const lastOkRaw = await env.KV.get(BREAKING_LAST_SUCCESS_KEY);
    const last = raw
      ? (() => {
          const rec = JSON.parse(raw);
          return (
            rec.at +
            (rec.ok
              ? " (delivered)"
              : " (FAILED HTTP " + rec.http_status + ")" +
                (rec.err ? " " + rec.err : ""))
          );
        })()
      : none.last;
    const lastOk = lastOkRaw ? JSON.parse(lastOkRaw).at : none.lastOk;
    return { last: last, lastOk: lastOk };
  } catch (err) {
    return {
      last: "unreadable (" + String(err) + ")",
      lastOk: "unreadable (" + String(err) + ")",
    };
  }
}

// --- THE EMPTY-DIGEST RETRY LADDER (2026-10-20) -------------------------------
//
// WHAT MOVED, AND WHAT DID NOT. digest-watchdog.yml used to carry a second cron,
// `17,32,47 * * * *`, running its `empty_digest_retry` job 72 times a day. Each
// firing was a billed runner whose only real work was to read
// state/digest_retry.json and decide whether a retry was due. The ladder's own
// gates are +15 then +45 minutes after an empty digest, so at most TWO retries
// exist per ladder and the other ~70 checks a day bought nothing. The schedule is
// unchanged (:17/:32/:47, SCHED_LADDER_PHASE); the reader is now this Worker.
//
// THE WORKFLOW STILL DOES THE WORK. This arm does NOT dispatch daily-news.yml and
// does NOT touch the retry file: it dials digest-watchdog.yml with
// `ladder_only: true`, and that job -- which owns the ladder's gates, its ledger
// and its ntfy alert -- does exactly what it always did. The ONLY thing that
// changed is who notices a retry is due.
//
// TWO READERS, ONE RULE, AND THEY MUST AGREE. The due test below re-implements
// what the watchdog job's own step does (`next_retry_at_npt` reached and the
// attempt budget not spent). Two copies of one rule drift, so
// tests/digest/digest_retry_ladder.test.js is a REQUIRED test that fails if the
// Worker's constants and the workflow's stop disagreeing. Change one, change both.
const DIGEST_RETRY_PATH = "state/digest_retry.json";
// The debounce exists because GitHub cron start jitter can deliver the same tick
// twice, and a second dispatch of the same rung would run the retry twice. The key
// carries the ATTEMPT number, so rung 1, rung 2 and rung 3 each dispatch once and a
// re-delivery of a rung already dialled does nothing.
const DIGEST_LADDER_DEBOUNCE_PREFIX = "digest:ladder_dispatched:";
const DIGEST_LADDER_DEBOUNCE_TTL_S = 60 * 60 * 6;
const WATCHDOG_DISPATCH_URL =
  "https://api.github.com/repos/looping-forever/daily-media-news/actions/workflows/digest-watchdog.yml/dispatches";

// "YYYY-MM-DD HH:MM" in Asia/Kathmandu -- the SAME clock the retry file's
// `next_retry_at_npt` is written in (daily-news.yml's writer runs under
// TZ=Asia/Kathmandu). Lexicographic comparison of the two strings is therefore a
// valid time comparison: fixed width, zero padded, and +05:45 always, no DST.
function nptStampFromInstant(ms) {
  return new Date(Number(ms) + DIGEST_NPT_OFFSET_MINUTES * 60 * 1000)
    .toISOString()
    .slice(0, 16)
    .replace("T", " ");
}

// The committed retry file, or null. A 404 (no ladder pending) and every transport
// or parse failure return null: "I could not read it" must NEVER be read as "a
// retry is due", or a GitHub outage becomes a retry storm.
async function fetchRetryFileJson(env) {
  if (!env || !env.GH_TOKEN) return null;
  const url = "https://api.github.com/repos/looping-forever/daily-media-news/contents/" +
    DIGEST_RETRY_PATH + "?ref=master";
  let res;
  try {
    res = await fetch(url, {
      headers: {
        Authorization: "Bearer " + env.GH_TOKEN,
        Accept: "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "daily-media-news-breaking-worker",
      },
    });
  } catch (err) {
    return null;
  }
  await noteTokenExpiry(env, res);
  if (!res.ok) return null;
  try {
    return JSON.parse(await res.text());
  } catch (err) {
    return null;
  }
}

// One ladder tick. Returns what it did so ?health=1 and the tests can see the
// decision without reading logs.
async function runDigestLadder(env, scheduledMs) {
  const rec = await fetchRetryFileJson(env);
  if (!rec || typeof rec !== "object") return { ran: false, why: "no ladder" };
  const next = String(rec.next_retry_at_npt || "");
  const attempts = Number(rec.attempts || 0);
  const max = Number(rec.max_attempts || 3);
  if (!next) return { ran: false, why: "no next_retry_at_npt" };
  if (attempts >= max) return { ran: false, why: "ladder exhausted" };
  const nowNpt = nptStampFromInstant(scheduledMs);
  if (nowNpt < next) return { ran: false, why: "not due until " + next };
  if (!env || !env.KV) return { ran: false, why: "no KV binding" };
  const key = DIGEST_LADDER_DEBOUNCE_PREFIX +
    String(rec.date || "?") + ":" + String(rec.slot || "?") + ":" + attempts;
  if (await env.KV.get(key)) return { ran: false, why: "already dialled" };
  // THE QUOTA GATE. The watchdog is PROTECTED with the digest: refused LAST,
  // only once the month has already reached the hard ceiling.
  const budget = await budgetChargeDispatch(env, "watchdog", scheduledMs);
  if (!budget.allow) {
    return { ran: false, why: "budget refused (" + budget.reason + ")" };
  }
  const result = await githubWorkflowDispatch(
    env, WATCHDOG_DISPATCH_URL, { ladder_only: "true" },
    "digest-watchdog ladder");
  if (result && result.ok) {
    await env.KV.put(key, isoUtc(scheduledMs),
                     { expirationTtl: DIGEST_LADDER_DEBOUNCE_TTL_S });
  }
  return { ran: true, ok: !!(result && result.ok), key: key,
           dispatch: result || null };
}

// --- THE RUNTIME DISPATCH QUOTA (2026-10-20) ----------------------------------
//
// WHY THIS IS FOR. config/actions-budget.json is a STATIC gate: CI fails when
// the tree's projection exceeds a ceiling. It cannot stop a month the tree never
// predicted -- a retry storm, a doubled dispatch, a job that overruns. Every
// signal that drove the 2026-10 overrun was AFTER the fact. This is the runtime
// counterpart: the Worker charges every dispatch it makes against a per-month KV
// record and, WHILE ENFORCEMENT IS ON, refuses once the month reaches the hard
// ceiling. Enforcement is OFF today (see BUDGET_ENFORCEMENT_ENABLED): the
// repository is public, so minutes are free and a refusal buys nothing. The
// charging and the reporting below are kept either way.
//
// TWO NUMBERS, AND THEY ARE NOT THE SAME NUMBER. Do not collapse them.
//   * design target 1400 min -- an ASPIRATION. REPORTED (the sentinel warns on
//     it); NEVER enforced here. Refusing at it would stop work with headroom left.
//   * hard ceiling 1800 min -- the ENFORCED number: the allowance (2000) minus
//     the reserve (200) that belongs to the OTHER repositories on this account.
//     Measured 2026-10-01..18 came in 9.2% ABOVE its own model (3360 vs 3078); a
//     282-min reserve would put the ceiling BELOW the model and make every day
//     red. So the reserve is 200, and the residual 82 min belongs to the
//     sentinel's 90% freeze arm, which sheds discretionary dispatches.
const BUDGET_ALLOWANCE_MINUTES = 2000;
const BUDGET_RESERVE_MINUTES = 200;
const BUDGET_HARD_CEILING_MINUTES = 1800;   // == allowance - reserve. Refused on only while enforcement is on.
const BUDGET_DESIGN_TARGET_MINUTES = 1400;  // reported only. NEVER enforced here.
// THE ENFORCEMENT SWITCH (2026-10-25). The arithmetic above is unchanged; this
// says whether the quota may REFUSE a dispatch on it. False because the
// repository is PUBLIC now, so standard runners are free: a refusal saves no
// minutes and can only cost a digest. Mirrored from config/actions-budget.json
// `enforcement.enabled` -- the Worker cannot read that file at runtime -- and
// Check 21 in tests/workflows/test_workflow_lint.py fails if the two disagree.
// Flip BOTH to true to re-arm it.
const BUDGET_ENFORCEMENT_ENABLED = false;
const BUDGET_ENFORCEMENT_REASON = "public repository - standard runners are free";
// The visibility probe, so a public repository disables enforcement even if the
// flag above is left true by mistake. ONE cached API call per TTL, the same shape
// as the freeze-label read: the answer changes at most when the owner changes it.
const BUDGET_REPO_API_URL = "https://api.github.com/repos/looping-forever/daily-media-news";
const BUDGET_VISIBILITY_CACHE_KEY = "budget:visibility";  // "1" public, "0" private
const BUDGET_VISIBILITY_CACHE_TTL_S = 60 * 60 * 6;        // 6h, stated not implied
const BUDGET_MONTH_KEY_PREFIX = "budget:";          // budget:YYYY-MM
const BUDGET_MONTH_TTL_S = 60 * 60 * 24 * 40;       // a month plus slack
const BUDGET_FREEZE_CACHE_KEY = "budget:freeze";    // cached `budget-freeze`
const BUDGET_FREEZE_CACHE_TTL_S = 60 * 15;          // worst-case freeze delay 15m
const BUDGET_REFUSED_KEY_PREFIX = "budget:refused:";
const BUDGET_FREEZE_LABEL_URL =
  "https://api.github.com/repos/looping-forever/daily-media-news/issues" +
  "?state=open&labels=budget-freeze&per_page=1";
//
// The per-target estimate, in billed MINUTES, is jobs_per_run x p95_minutes from
// config/actions-budget.json. The Worker cannot read that file at runtime, so
// this is a deliberately SMALL parallel table and
// tests/workflows/test_workflow_lint.py FAILS (both directions) if either side
// moves alone.
const BUDGET_JOB_MINUTES = {
  breaking: 1.6,   // breaking-monitor.yml  1 job x 1.6
  academic: 2.1,   // academic-result.yml   1 job x 2.1
  prebuild: 1.5,   // digest-prebuild.yml   1 job x 1.5
  digest: 6.1,     // daily-news.yml        1 job x 6.1
  watchdog: 1.0,   // digest-watchdog.yml   1 job x 1.0
};
// THE REFUSAL ORDER, WHICH APPLIES ONLY WHILE ENFORCEMENT IS ON. Discretionary
// dispatches are refused FIRST; the digest build
// and the watchdog LAST, because losing the digest is the one outcome the owner
// cannot accept. AT THE MARGIN that means: if a 6.1-min digest would tip the month
// to 1805 and a 1.5-min prebuild is also due, the DIGEST is allowed and the
// PREBUILD refused -- never the reverse.
const BUDGET_PROTECTED_JOBS = ["digest", "watchdog"];

function budgetMonthKey(ms) {
  return BUDGET_MONTH_KEY_PREFIX + isoUtc(ms).slice(0, 7);
}

function budgetJobMinutes(job) {
  return Number(BUDGET_JOB_MINUTES[job] || 0);
}

function budgetJobIsProtected(job) {
  return BUDGET_PROTECTED_JOBS.indexOf(job) !== -1;
}

// The decision, PURE, so tests drive it without KV. `monthMinutes` is what the KV
// record holds; `freeze` is the label state. See BUDGET_PROTECTED_JOBS for why a
// protected job is still allowed while the month is UNDER the ceiling.
//
// `enforce` is the RESOLVED gate from budgetEnforcementState. Only an explicit
// `false` disables the quota, so an omitted argument keeps the ceiling semantics
// every existing caller and test relies on, and a test can drive either arm with
// no KV and no network. With enforcement off this returns allow:true BEFORE the
// freeze arm too: on a public repository there are no minutes to shed, and
// `budget_refused` must never fire.
function budgetDecision(monthMinutes, job, freeze, enforce) {
  const est = budgetJobMinutes(job);
  const protectedJob = budgetJobIsProtected(job);
  if (enforce === false) return { allow: true, reason: "", est: est };
  if (freeze && !protectedJob) return { allow: false, reason: "freeze", est: est };
  if (monthMinutes + est <= BUDGET_HARD_CEILING_MINUTES) {
    return { allow: true, reason: "", est: est };
  }
  if (protectedJob && monthMinutes < BUDGET_HARD_CEILING_MINUTES) {
    return { allow: true, reason: "", est: est };
  }
  return { allow: false, reason: "hard ceiling", est: est };
}

// The month's charge. A MISSING or CORRUPT record reads as 0, which is the safe
// direction: it errs toward allowing a dispatch, never toward refusing one that
// would have fitted.
async function readBudgetMonth(env, ms) {
  if (!env || !env.KV) return { minutes: 0, runs: 0 };
  try {
    const raw = await env.KV.get(budgetMonthKey(ms));
    if (!raw) return { minutes: 0, runs: 0 };
    const rec = JSON.parse(raw);
    return { minutes: Number(rec.minutes || 0), runs: Number(rec.runs || 0) };
  } catch (err) {
    return { minutes: 0, runs: 0 };
  }
}

async function writeBudgetMonth(env, ms, minutes, runs) {
  if (!env || !env.KV) return;
  await env.KV.put(budgetMonthKey(ms), JSON.stringify({
    minutes: minutes, runs: runs, at: isoUtc(ms),
  }), { expirationTtl: BUDGET_MONTH_TTL_S });
}

// The CACHED freeze flag only -- no network. The status page must not make a
// GitHub call, and must not go dark when Actions is unavailable.
async function cachedBudgetFreeze(env) {
  if (!env || !env.KV) return null;
  try {
    const v = await env.KV.get(BUDGET_FREEZE_CACHE_KEY);
    if (v === null || v === undefined) return null;
    return v === "1";
  } catch (err) {
    return null;
  }
}

// Is the `budget-freeze` label on an OPEN issue? ONE SWITCH, TWO READERS: the
// sentinel creates the label (the owner reads it on a phone) and this reads it
// through the SAME GitHub token that already dispatches. An unreadable label is
// NOT a freeze, which errs toward allowing dispatches. The result is CACHED for
// BUDGET_FREEZE_CACHE_TTL_S, so a freeze the sentinel raises takes effect within
// at most 15 minutes; until then the 90% arm still sheds the same dispatches.
async function readBudgetFreeze(env, ms) {
  const cached = await cachedBudgetFreeze(env);
  if (cached !== null) return cached;
  const live = await fetchBudgetFreezeLabel(env);
  try {
    await env.KV.put(BUDGET_FREEZE_CACHE_KEY, live ? "1" : "0",
                     { expirationTtl: BUDGET_FREEZE_CACHE_TTL_S });
  } catch (err) {
    // A failed cache write is not a failed read.
  }
  return live;
}

async function fetchBudgetFreezeLabel(env) {
  if (!env || !env.GH_TOKEN) return false;
  try {
    const res = await fetch(BUDGET_FREEZE_LABEL_URL, {
      headers: {
        Authorization: "Bearer " + env.GH_TOKEN,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "daily-media-news-breaking-worker",
      },
    });
    // Observed on the freeze probe too: it is authenticated and it runs before
    // every charged dispatch, so the expiry record stays fresh on a busy tick.
    await noteTokenExpiry(env, res);
    if (!res.ok) return false;
    const arr = await res.json();
    return Array.isArray(arr) && arr.length > 0;
  } catch (err) {
    return false;
  }
}

// IS THE REPOSITORY PUBLIC? One API call, cached for BUDGET_VISIBILITY_CACHE_TTL_S.
// Enforcement is pointless on a public repository -- standard runners are free --
// so a confirmed public answer force-disables the refusal whatever the committed
// flag says. It returns null when the question cannot be answered (no token, a
// non-200, a body with no boolean `private`, anything thrown) and null is NEVER
// read as "public": the flag decides, which is the only reason the flag is still
// worth having. Only `private === false` disables enforcement against the flag.
async function fetchRepoIsPublic(env) {
  if (!env || !env.GH_TOKEN) return null;
  try {
    const res = await fetch(BUDGET_REPO_API_URL, {
      headers: {
        Authorization: "Bearer " + env.GH_TOKEN,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "daily-media-news-breaking-worker",
      },
    });
    await noteTokenExpiry(env, res);
    if (!res.ok) return null;
    const body = await res.json();
    if (!body || typeof body.private !== "boolean") return null;
    return body.private === false;
  } catch (err) {
    return null;
  }
}

// The CACHED visibility answer only -- no network. The status page uses this, so
// `?health=1` never makes a GitHub call and never goes dark when the API is down.
async function cachedRepoIsPublic(env) {
  if (!env || !env.KV) return null;
  try {
    const v = await env.KV.get(BUDGET_VISIBILITY_CACHE_KEY);
    if (v === "1") return true;
    if (v === "0") return false;
    return null;
  } catch (err) {
    return null;
  }
}

async function readRepoIsPublic(env) {
  const cached = await cachedRepoIsPublic(env);
  if (cached !== null) return cached;
  const live = await fetchRepoIsPublic(env);
  if (live === null) return null;
  try {
    await env.KV.put(BUDGET_VISIBILITY_CACHE_KEY, live ? "1" : "0",
                     { expirationTtl: BUDGET_VISIBILITY_CACHE_TTL_S });
  } catch (err) {
    // A failed cache write is not a failed read: this tick's answer still stands.
  }
  return live;
}

// The resolved gate: may this dispatch be refused at all?
//   flag false             -> never refused. The shipped state.
//   flag true, repo public -> never refused, and the probe wins over the flag.
//   flag true, otherwise   -> refused exactly as before, in the established order.
//
// `enabledByFlag` is an explicit TEST SEAM, not a second configuration surface:
// omitted (every real caller) it reads the committed constant, and passing it is
// how the suite proves both positions of ONE switch without editing the source it
// is judging. It is not read from KV or from env.
async function budgetEnforcementState(env, enabledByFlag) {
  const flagOn = enabledByFlag === undefined
    ? BUDGET_ENFORCEMENT_ENABLED === true
    : enabledByFlag === true;
  if (!flagOn) {
    return {
      enforce: false,
      enforcement_enabled: false,
      enforcement_reason: BUDGET_ENFORCEMENT_REASON,
      repo_public: await cachedRepoIsPublic(env),
    };
  }
  const repoPublic = await readRepoIsPublic(env);
  if (repoPublic === true) {
    return {
      enforce: false,
      enforcement_enabled: true,
      enforcement_reason: "repository is public, so its Actions minutes are free",
      repo_public: true,
    };
  }
  return {
    enforce: true,
    enforcement_enabled: true,
    enforcement_reason: "",
    repo_public: repoPublic,
  };
}

// Alert ONCE per month per reason. The marker is burned only on a SUCCESSFUL
// send, so a failed alert is retried next tick rather than lost -- the same rule
// the heartbeat cooldown follows. Reuses the ntfy arm the owner already reads.
async function alertBudgetRefusedOnce(env, ms, job, decision, monthMinutes) {
  const month = isoUtc(ms).slice(0, 7);
  const key = BUDGET_REFUSED_KEY_PREFIX + month + ":" + decision.reason;
  try {
    if (await env.KV.get(key)) return false;
  } catch (err) {
    // An unreadable marker sends at most one extra alert, never a wrong one.
  }
  const message =
    "The Worker refused a `" + job + "` dispatch for " + month + ". The month is " +
    "charged " + Number(monthMinutes).toFixed(1) + " min against a hard ceiling " +
    "of " + BUDGET_HARD_CEILING_MINUTES + " min (allowance " +
    BUDGET_ALLOWANCE_MINUTES + " - reserve " + BUDGET_RESERVE_MINUTES + "). " +
    "Discretionary dispatches shed first; the digest build and the watchdog are " +
    "refused last. Reason: " + decision.reason + ".";
  const res = await publishToNtfy(env, {
    topic: ntfyTopicOf(env),
    title: "Dispatch refused: Actions at the hard ceiling",
    message: message,
    priority: 4,
    tags: ["warning"],
  }, "dmn-budget-refused-" + month + "-" + decision.reason, "budget refusal");
  if (res && res.ok) {
    try {
      await env.KV.put(key, isoUtc(ms), { expirationTtl: BUDGET_MONTH_TTL_S });
    } catch (err) {
      // At worst one more alert next tick.
    }
  }
  return !!(res && res.ok);
}

// WHEN DOES THE GITHUB TOKEN DIE? GitHub returns
// `GitHub-Authentication-Token-Expiration` on an authenticated response made with
// an EXPIRING token (fine-grained PATs) and omits it for a token that never
// expires. NOTHING NEW IS CALLED FOR THIS: every authenticated fetch this Worker
// already makes passes its response through noteTokenExpiry, so the answer arrives
// as a side effect of work that was happening anyway.
//
// THE CACHE. One KV record, `token:expiry`, holding the RAW header and when it was
// seen, with a 6-HOUR TTL. Worst-case staleness of `?health=1` is therefore 6
// hours: after that the record is gone and health reports `unknown` rather than
// repeating a figure from an older probe. The value itself changes only when the
// token is replaced, so a longer TTL would be safe -- and would also let the field
// sit stale for a week. 6h is the compromise, stated rather than implied.
const TOKEN_EXPIRY_HEADER = "GitHub-Authentication-Token-Expiration";
const TOKEN_EXPIRY_CACHE_KEY = "token:expiry";
const TOKEN_EXPIRY_CACHE_TTL_S = 6 * 60 * 60;
// One alert per calendar month, burned only on a SUCCESSFUL send -- the same rule
// alertBudgetRefusedOnce follows, for the same reason: a failed alert is retried,
// not lost.
const TOKEN_EXPIRY_ALERT_KEY_PREFIX = "token:expiry-alert:";  // + YYYY-MM
const TOKEN_EXPIRY_ALERT_DAYS = 14;
const TOKEN_EXPIRY_UNKNOWN = "unknown";
const MS_PER_DAY = 24 * 60 * 60 * 1000;

// Parse the header into the UTC midnight of its expiry DATE, or null. Accepts the
// shape GitHub actually sends (`2026-11-24 09:33:12 UTC`) and an ISO 8601 variant,
// because the exact shape is GitHub's to change and a parser that knows only one
// of them would turn a token warning into a health-endpoint failure.
function parseTokenExpiry(value) {
  const raw = String(value == null ? "" : value).trim();
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  const ms = Date.UTC(y, mo - 1, d);
  const back = new Date(ms);
  // Date.UTC rolls a nonexistent 2026-02-31 forward into March, so a round-trip is
  // the check that the date existed at all.
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 ||
      back.getUTCDate() !== d) {
    return null;
  }
  return { raw: raw, dayMs: ms };
}

function utcDayStartMs(ms) {
  const d = new Date(ms);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

// WHOLE DAYS BETWEEN THE UTC DATES, which is the only comparison that answers the
// question a human asks. A millisecond delta divided by a day TRUNCATES: a token
// expiring at 09:33 tomorrow, read at 23:00 tonight, is 10.5 hours away and floors
// to 0 ("expires today") when the truthful answer is 1.
function daysUntilExpiry(value, nowMs) {
  const parsed = parseTokenExpiry(value);
  if (!parsed) return null;
  const now = nowMs === undefined ? Date.now() : Number(nowMs);
  if (!isFinite(now)) return null;
  return Math.round((parsed.dayMs - utcDayStartMs(now)) / MS_PER_DAY);
}

// Record the header from a response this Worker already holds. NEVER throws, and
// never overwrites a good record with a worse one:
//   * header ABSENT     -> write nothing. A non-expiring token sends no header, and
//                          an error response may omit it; neither disproves the
//                          expiry already observed.
//   * UNPARSEABLE value -> write nothing, so health reports `unknown` from the
//                          absence of a record instead of NaN from a bad one.
async function noteTokenExpiry(env, res) {
  try {
    if (!res || !res.headers || typeof res.headers.get !== "function") return;
    const raw = res.headers.get(TOKEN_EXPIRY_HEADER);
    if (!raw) return;
    const parsed = parseTokenExpiry(raw);
    if (!parsed) return;
    if (!env || !env.KV) return;
    await env.KV.put(
      TOKEN_EXPIRY_CACHE_KEY,
      JSON.stringify({
        expires_at: parsed.raw,
        observed_at: new Date().toISOString(),
      }),
      { expirationTtl: TOKEN_EXPIRY_CACHE_TTL_S }
    );
  } catch (err) {
    // An observation that cannot be stored is not a failed tick.
    console.log("token expiry observation skipped:", String(err));
  }
}

async function readTokenExpiry(env) {
  if (!env || !env.KV) return null;
  try {
    const raw = await env.KV.get(TOKEN_EXPIRY_CACHE_KEY);
    if (!raw) return null;
    const rec = JSON.parse(String(raw));
    if (!rec || typeof rec.expires_at !== "string" || !rec.expires_at) return null;
    return rec;
  } catch (err) {
    return null;
  }
}

// The `?health=1` fields. `state` is a literal string and `days_remaining` is
// null -- never NaN, never a guess -- whenever the answer is not known.
async function tokenExpiryHealthFields(env, nowMs) {
  const out = {
    state: TOKEN_EXPIRY_UNKNOWN,
    expires_at: null,
    days_remaining: null,
    observed_age_s: null,
  };
  const rec = await readTokenExpiry(env);
  if (!rec) return out;
  const days = daysUntilExpiry(rec.expires_at, nowMs);
  if (days === null) return out;
  const seen = Date.parse(rec.observed_at || "");
  out.state = "known";
  out.expires_at = rec.expires_at;
  out.days_remaining = days;
  out.observed_age_s = isNaN(seen)
    ? null : Math.max(0, Math.floor((Number(nowMs) - seen) / 1000));
  return out;
}

// Tell the owner ONCE per calendar month that the token is nearly dead. Reuses the
// ntfy arm the owner already reads; there is no second notifier.
//
// UNKNOWN CONSUMES NOTHING. If no probe has been observed yet the answer is
// `unknown`, and burning this month's key on it would silence the one alert that
// matters when the record finally arrives -- so the key is only read and written
// on a KNOWN answer at or under the threshold.
async function alertTokenExpiryOnce(env, nowMs) {
  const rec = await readTokenExpiry(env);
  if (!rec) return false;
  const days = daysUntilExpiry(rec.expires_at, nowMs);
  if (days === null) return false;
  if (days > TOKEN_EXPIRY_ALERT_DAYS) return false;
  if (!env || !env.KV) return false;
  const month = isoUtc(nowMs).slice(0, 7);
  const key = TOKEN_EXPIRY_ALERT_KEY_PREFIX + month;
  try {
    if (await env.KV.get(key)) return false;
  } catch (err) {
    // An unreadable marker sends at most one extra alert, never a wrong one.
  }
  const when = days === 0
    ? "TODAY"
    : (days > 0 ? "in " + days + " day(s)" : Math.abs(days) + " day(s) AGO");
  const message =
    "The GitHub token this Worker dispatches with expires " + when + ". " +
    "Expiry: " + rec.expires_at + ". " +
    (days < 0
      ? "IT HAS ALREADY EXPIRED, so every dispatch is failing right now. "
      : "") +
    "Replace the fine-grained PAT, then re-run scripts/set-actions-secret.ps1 " +
    "and scripts/set-worker-secret.ps1. " +
    "This is the only warning you get this month.";
  const res = await publishToNtfy(env, {
    topic: ntfyTopicOf(env),
    title: days < 0
      ? "GH_TOKEN has EXPIRED"
      : "GH_TOKEN expires in " + days + " day(s)",
    message: message,
    priority: days <= 3 ? 5 : 4,
    tags: days <= 3 ? ["rotating_light"] : ["warning"],
  }, "dmn-token-expiry-" + month, "token expiry warning");
  if (res && res.ok) {
    try {
      await env.KV.put(key, isoUtc(nowMs), { expirationTtl: BUDGET_MONTH_TTL_S });
    } catch (err) {
      // At worst one more alert this month.
    }
  }
  return !!(res && res.ok);
}

// CHARGE-THEN-DISPATCH. The charge is PERSISTED BEFORE the caller dispatches, so
// it is recorded even when the dispatch later fails: GitHub bills the job, not
// the success. Returns the decision so a caller can skip its dispatch.
//
// THE RACE. Cloudflare KV is eventually consistent, so two ticks can read the
// same value and one charge can be lost -- the record UNDER-counts by at most one
// dispatch. That is the SAFE direction: it errs toward ALLOWING (never toward
// refusing a digest that would have fitted). A refusal is never refunded, so the
// charge must never be lost in THAT direction -- and it is not, because the charge
// is written before the dispatch, so the only charge that can be lost is one we
// actually made.
async function budgetChargeDispatch(env, job, ms, enabledByFlag) {
  // No KV means the quota cannot count; no GH_TOKEN means nothing will actually
  // dispatch (githubWorkflowDispatch returns token-missing), so there are no
  // minutes to charge. Either way: ALLOW. Never refuse, and never charge a
  // dispatch that cannot happen.
  if (!env || !env.KV || !env.GH_TOKEN) {
    return { allow: true, reason: "", est: budgetJobMinutes(job),
             enforcement: null };
  }
  const enforcement = await budgetEnforcementState(env, enabledByFlag);
  const freeze = await readBudgetFreeze(env, ms);
  const month = await readBudgetMonth(env, ms);
  const decision = budgetDecision(month.minutes, job, freeze, enforcement.enforce);
  decision.enforcement = enforcement;
  // THE CHARGE HAPPENS EITHER WAY. It is the accounting, not the gate: the month
  // record and the `?health=1` figures stay true even while nothing is refused.
  if (decision.allow) {
    await writeBudgetMonth(env, ms, month.minutes + decision.est, month.runs + 1);
  } else {
    await alertBudgetRefusedOnce(env, ms, job, decision, month.minutes);
  }
  return decision;
}

// What ?health=1 exposes, so the owner can see the freeze state even when Actions
// is unavailable: the month key, the two numbers, minutes charged, runs, the
// remaining headroom, whether the ceiling is reached, and whether the freeze flag
// is set. Reads the CACHED flag only.
async function budgetHealthFields(env, nowMs) {
  const out = {
    month: isoUtc(nowMs).slice(0, 7),
    allowance_minutes: BUDGET_ALLOWANCE_MINUTES,
    reserve_minutes: BUDGET_RESERVE_MINUTES,
    hard_ceiling_minutes: BUDGET_HARD_CEILING_MINUTES,
    design_target_minutes: BUDGET_DESIGN_TARGET_MINUTES,
    minutes_charged: null,
    runs: null,
    remaining_minutes: null,
    over_hard_ceiling: null,
    freeze: null,
    // The switch, reported so "is anything being refused?" is answerable from the
    // status page. `enforced` is the RESOLVED answer: the flag AND the cached
    // visibility probe. A null `repo_public` means the probe has not run yet, so
    // the flag alone decides -- the same rule the dispatch path follows.
    enforcement_enabled: BUDGET_ENFORCEMENT_ENABLED === true,
    enforcement_reason: BUDGET_ENFORCEMENT_REASON,
    enforced: BUDGET_ENFORCEMENT_ENABLED === true,
    repo_public: null,
  };
  if (!env || !env.KV) return out;
  const month = await readBudgetMonth(env, nowMs);
  out.minutes_charged = month.minutes;
  out.runs = month.runs;
  out.remaining_minutes = BUDGET_HARD_CEILING_MINUTES - month.minutes;
  out.over_hard_ceiling = month.minutes >= BUDGET_HARD_CEILING_MINUTES;
  out.freeze = await cachedBudgetFreeze(env);
  out.repo_public = await cachedRepoIsPublic(env);
  out.enforced = BUDGET_ENFORCEMENT_ENABLED === true && out.repo_public !== true;
  if (out.enforced) {
    out.enforcement_reason = "";
  } else if (BUDGET_ENFORCEMENT_ENABLED === true) {
    out.enforcement_reason = "repository is public, so its Actions minutes are free";
  }
  return out;
}

// --- Workers entry points ------------------------------------------------------

// ---- PLACEMENT IS THE LOAD-BEARING DETAIL ----------------------------------
// The heartbeat is written HERE, on the dispatcher branch, and deliberately NOT
// inside handle()/handleAcademic(). Two non-obvious reasons:
//
// 1. The `?run=1` / `?run_academic=1` fetch paths call handle()/
//    handleAcademic() directly. A heartbeat written in there would be
//    refreshed by ONE manual probe and would permanently mask a dead cron:
//    the switch would report healthy while the thing it monitors is dead.
//    That is the single worst failure this feature has, so the heartbeat
//    is structurally unreachable from any fetch path. Being reached only from
//    the tick that decided to run the job is what makes that true rather than
//    merely intended.
//
// 2. It sits BEFORE the config guard at the top of handle(), which
//    early-returns on a missing GH_TOKEN/KV. The heartbeat answers "did the
//    cron FIRE?", not "did the work succeed?" — telemetry for the second
//    question already exists (breaking:last_dispatch / last_success).
//    Mixing them means a missing PAT silently freezes the liveness key and
//    manufactures a false "cron is dead" page at the worst possible moment.
//
// The dispatcher DID NOT WEAKEN THIS. Before the merge the two sweeps were two
// triggers and the heartbeat was written once per sweep; now they are two jobs
// of one trigger and it is still written once per sweep, on the same minutes,
// because planTickFor() reproduces the same instants.
async function scheduledJobs(jobs, env, ctx, cronLabel, scheduledMs) {
  for (const job of jobs) {
    if (job === JOB_BREAKING) {
      ctx.waitUntil(touchHeartbeat(env, BREAKING_HEARTBEAT_KEY, cronLabel));
      ctx.waitUntil(handle(env));
    } else if (job === JOB_ACADEMIC) {
      ctx.waitUntil(touchHeartbeat(env, ACADEMIC_HEARTBEAT_KEY, cronLabel));
      ctx.waitUntil(handleAcademic(env));
    } else if (job === JOB_HEALTHCHECK) {
      ctx.waitUntil(checkHeartbeats(env));
    } else if (job === "digest:morning") {
      ctx.waitUntil(deliverDigest(env, "morning", { scheduledMs: scheduledMs }));
    } else if (job === "digest:evening") {
      ctx.waitUntil(deliverDigest(env, "evening", { scheduledMs: scheduledMs }));
    } else if (job === "prebuild:morning") {
      // It dispatches and returns. It does NOT wait for the prebuild, does not
      // poll the artifact, and does not write anything: the slot 40 minutes
      // later either finds a sealed artifact or takes the fallback ladder, and
      // nothing recorded here can change which.
      ctx.waitUntil(dispatchPrebuild(env, "morning", scheduledMs));
    } else if (job === "prebuild:evening") {
      ctx.waitUntil(dispatchPrebuild(env, "evening", scheduledMs));
    } else if (job === JOB_DIGEST_LADDER) {
      // Cheap in the common case: one KV-checked read of the retry file, and
      // most ticks find no ladder at all. See runDigestLadder below.
      ctx.waitUntil(runDigestLadder(env, scheduledMs));
    } else {
      // Unreachable from planTickFor(); a job name added there without a branch
      // here would be a job that silently does nothing.
      console.error("dispatcher: unknown job " + job + " — nothing was run");
    }
  }
}

export default {
  async scheduled(controller, env, ctx) {
    const cron = controller.cron || "";

    // THE LEGACY SHAPE. `scheduled({cron: "<one job's expression>"})` runs that
    // one job and nothing else. It is not a deployed trigger any more — only
    // CRON_DISPATCHER is — but it is retained deliberately: it is how a single
    // job is driven deterministically from a test, and it is what a hand-typed
    // dashboard trigger during the merge window would still do.
    const legacy = legacyJobForCron(cron);
    if (legacy) {
      await scheduledJobs([legacy], env, ctx, "legacy", tickInstantMs(controller));
      return;
    }

    // AN UNRECOGNISED CRON IS NOT THE DISPATCHER. It falls through to the
    // breaking sweep with NO heartbeat, exactly as it did before the merge: a
    // mistyped cron is not evidence that the declared sweep fired, and
    // pretending otherwise would hide the drift this file exists to expose. The
    // breaking sweep is the safe default so a mistyped dashboard trigger can
    // never silently disable detection.
    if (cron !== CRON_DISPATCHER) {
      await scheduledJobs([JOB_BREAKING], env, ctx, "", tickInstantMs(controller));
      return;
    }

    // THE DISPATCHER. Everything else is one tick on the one-minute grid.
    const ms = tickInstantMs(controller);
    const at = new Date(ms);
    const jobs = planTickFor(at.getUTCHours(), at.getUTCMinutes());
    // NOTHING DUE. No KV read, no KV write, no fetch, no allocation of any
    // consequence. This is the 456-ticks-a-day path and it is the reason the
    // KV budget did not move when five crons became one.
    if (!jobs.length) return;
    // THE TOKEN EXPIRY WARNING, on the ticks that are doing work anyway. It reads
    // one cached KV record (no network) and is wrapped so a failure here can never
    // cost the tick its real jobs. Placed AFTER the empty-tick return on purpose:
    // the 456 quiet ticks a day stay free, and 480+ busy ticks are ample to deliver
    // a monthly reminder.
    try {
      await alertTokenExpiryOnce(env, ms);
    } catch (err) {
      console.error("token expiry check failed:", String(err));
    }
    await scheduledJobs(jobs, env, ctx, CRON_DISPATCHER, ms);
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    // Read-only and FIRST, so no query string can smuggle a dispatch past it.
    if (url.searchParams.get("health") === "1") {
      const health = await healthForEndpoint(env);
      return new Response(JSON.stringify(health) + "\n", {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        },
      });
    }
    if (url.searchParams.get("run") === "1") {
      const summary = await handle(env);
      return new Response(JSON.stringify(summary) + "\n", {
        status: summary.ok ? 200 : 500,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.searchParams.get("run_academic") === "1") {
      const summary = await handleAcademic(env);
      return new Response(JSON.stringify(summary) + "\n", {
        status: summary.ok ? 200 : 500,
        headers: { "Content-Type": "application/json" },
      });
    }
    // All three digest endpoints are below the read-only health check and above
    // the run probes, and all three carry their own credential. None can write
    // the delivery ledger: `?ingest=1` never reads or writes it,
    // `?deliver_now=1` runs the plan with a null ledger writer (see
    // handleDeliverNow), and `?deliver_test=1` never reaches KV at all (see
    // handleDeliverTest).
    if (url.searchParams.get("ingest") === "1") {
      return await handleDigestIngest(request, env);
    }
    if (url.searchParams.get("deliver_now") === "1") {
      return await handleDeliverNow(request, env);
    }
    if (url.searchParams.get("deliver_test") === "1") {
      return await handleDeliverTest(request, env);
    }
    const dispatchState = await breakingDispatchForStatus(env);
    const health = await healthForEndpoint(env);
    const stamped = health.build !== WORKER_BUILD;
    const lines = [
      "daily-media-news breaking tripwire: alive",
      "KV binding: " + (env && env.KV ? "present" : "MISSING"),
      // Which source this deployment actually came from. "unset" means no
      // deploy ever passed --var WORKER_BUILD:<sha>; anything else is compared
      // against master HEAD by the drift canary in drift-canary.yml. A
      // deployment from a stale checkout is otherwise undetectable.
      "build: " + health.build + (stamped
        ? " (wrangler deploy --var WORKER_BUILD:<short sha>)"
        : " — NEVER STAMPED: this deployment was published without " +
          "--var WORKER_BUILD, so its source commit is unknown"),
      // Presence of the secret is a CONFIGURATION fact, not a DELIVERY fact.
      // It used to read as health, which is how an expired PAT can hide for
      // weeks: the status page stays green while every alert is 401ing.
      "GH_TOKEN secret: " +
        (env && env.GH_TOKEN
          ? "present (NOT proof of delivery — see last dispatch below)"
          : "MISSING"),
      "last dispatch: " + dispatchState.last,
      "last success: " + dispatchState.lastOk,
      // Liveness of the cron itself, independent of whether anything is
      // happening. This is the dead-man's switch's own view of the world; the
      // same numbers are machine-readable at ?health=1.
      "breaking heartbeat: " +
        describeHeartbeatAge(health.breaking_heartbeat_age_s,
                             health.breaking_heartbeat_unreadable),
      "academic heartbeat: " +
        describeHeartbeatAge(health.academic_heartbeat_age_s,
                             health.academic_heartbeat_unreadable),
      // ONE deployed trigger, and what it stands in for. The list used to be
      // the in-source CRON_* constants, which reported seven crons while
      // Cloudflare held five — so a half-applied deploy looked healthy. The
      // honest split is: this is the one cron Cloudflare holds; the schedule
      // below is what the dispatcher reproduces from it.
      "crons (deployed): " + CRON_DISPATCHER +
        " — ONE trigger. Cloudflare Workers Free allows 5 per account and this " +
        "account was at the limit, so the jobs are merged into a dispatcher. " +
        "This list is in-source, NOT an observation of Cloudflare's registered " +
        "triggers: a Worker cannot query its own schedules. What verifies the " +
        "trigger is still firing is the heartbeat above, which is only ever " +
        "written from a tick that ran a sweep.",
      "dispatches on: " + CRON_BREAKING_SWEEP + " breaking sweep | " +
        CRON_ACADEMIC_SWEEP + " academic sweep | " +
        CRON_HEALTHCHECK + " heartbeat check | " +
        CRON_DIGEST_MORNING + "/" + CRON_DIGEST_EVENING + " digest slots | " +
        CRON_DIGEST_REFIRE_MORNING + ".." + CRON_DIGEST_REFIRE_MORNING_LAST +
        " and " + CRON_DIGEST_REFIRE_EVENING + ".." +
        CRON_DIGEST_REFIRE_EVENING_LAST + " bounded re-fire window (a no-op " +
        "once the ledger records the slot, then it stops)",
      // The prebuild, with the margin it has. Reported here so a reader can see
      // the lead rather than infer it, and so a null (a slot that cannot carry a
      // 40-minute prebuild) is visible instead of silently absent.
      "digest prebuild: " + CRON_PREBUILD_MORNING + "/" + CRON_PREBUILD_EVENING +
        " UTC = 06:40/18:40 NPT, " + PREBUILD_LEAD_MINUTES + " min ahead of the " +
        CRON_DIGEST_MORNING + "/" + CRON_DIGEST_EVENING + " digest slots (06:40 " +
        "NPT would leave a lead that does not exist, so it reports that)" +
        (CRON_PREBUILD_MORNING && CRON_PREBUILD_EVENING
          ? ""
          : " [ONE OR BOTH SLOTS CANNOT CARRY THIS LEAD]") +
        ". It dispatches daily-news.yml (phase=prebuild) and does not wait; a prebuild that " +
        "misses or overruns costs one push ~22 min late via the fallback ladder, " +
        "never a missing digest.",
      // Liveness of the academic path, visible without opening Cloudflare.
      // Presence/value only — no secret is ever echoed here.
      "academic floor: " + (await academicFloorForStatus(env)),
      "academic KV writes: " + (ACADEMIC_LAST_SWEEP
        ? ("last sweep wrote " + ACADEMIC_LAST_SWEEP.kvWrites +
           ", skipped " + ACADEMIC_LAST_SWEEP.kvWritesSkipped +
           " unchanged key(s)")
        : "no sweep completed in this isolate (isolates are evicted, so an " +
          "empty reading here is normal, not a fault)"),
      // Which arm the digest crons call, and what the delivery side last did.
      // The arm is reported rather than assumed, so a half-finished switch can
      // never leave a reader guessing which one is live.
      "digest delivery arm: " + DIGEST_CRON_ARM +
        (DIGEST_CRON_ARM === "deliverDigest"
          ? " (this Worker pushes the prebuilt digest; when the prebuild left " +
            "nothing sealed it ALSO dispatches a late build of its own)"
          : " (dispatchDigest still wakes daily-news.yml; the Worker-side " +
            "push is shipped dark)"),
      "digest last delivery: " + (health.digest && health.digest.last_delivery
        ? JSON.stringify(health.digest.last_delivery)
        : "never recorded"),
      "append ?run=1 to force one breaking pass now",
      "append ?run_academic=1 to force one academic pass now",
      "append ?health=1 for the machine-readable liveness/build/crons JSON",
    ];
    return new Response(lines.join("\n") + "\n", {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },
};
