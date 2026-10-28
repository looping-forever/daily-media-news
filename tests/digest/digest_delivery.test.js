"use strict";
/**
 * tests/digest/digest_delivery.test.js
 *
 * THE DIGEST DELIVERY PATH, EXECUTED. Node's own test runner, the Worker's own
 * functions, a fake KV and a fake fetch. The harness is a VERBATIM copy of
 * tests/breaking/worker_heartbeat.test.js — same loader, same fake KV, same
 * clock, same log capture, same env override — so that one Worker's coverage is
 * written one way and a reader who has met one file has met both.
 *
 * WHY IT IS JS AND NOT PYTHON. Every behaviour below is a property of
 * JavaScript code that only running can observe: which KV write lands before
 * which HTTP POST, whether a retry stops at the budget, whether a fetch path can
 * reach the ledger writer at all. Python would have to re-implement the path to
 * check any of it, and a re-implementation agrees with itself for ever.
 *
 * WHERE IT LIVES AND WHY. tests/digest/ is where the digest's own contract
 * already lives (tests/digest/test_literal_step_names.py pins the two literal
 * ntfy step names the render suite slices by). A new top-level tests/worker/
 * package would split the digest's coverage across two trees.
 *
 * RUN:
 *     node --test tests/digest/digest_delivery.test.js
 *     py -3 -m unittest tests.digest.test_digest_delivery -v     (wrapper,
 *       used by CI, which fails LOUDLY rather than skipping when node is absent)
 *
 * REPRODUCING THE RED. Set DIGEST_DELIVERY_WORKER to the path of a different
 * cloudflare-worker.js and every test in this file judges THAT file instead of
 * the committed one — which is how each behaviour below is proved to be
 * load-bearing rather than merely present. The override is printed as a NOTE, so
 * it can never be mistaken for CI.
 *
 * THE GUARANTEE UNDER TEST IS AT-LEAST-ONCE, WITH A POSSIBLE DUPLICATE ON THE
 * RECOVERY PATH ONLY. No test name here says exactly-once, and none may: there
 * is no transaction spanning "ntfy has the message" and "KV has the record".
 *
 * WHAT EACH BLOCK IS HOLDING DOWN:
 *   1. validateArtifact refuses a wrong-dated artifact above all else — an
 *      unlabelled yesterday's digest is indistinguishable from today's.
 *   2. the ledger guard suppresses a second send;
 *   3. the sequence_id read-back suppresses it EVEN WHEN THE LEDGER IS BLIND,
 *      which is the one failure the ledger cannot see by definition;
 *   4. `pending` is on disk BEFORE the first POST, and the sequence_id is set by
 *      the header ntfy actually reads;
 *   5. a push ALWAYS happens: stale -> last-good fallback -> unavailable, and
 *      the fallback ladder stays silent when the real digest goes out;
 *   6. a KV read failure pushes and rethrows nothing;
 *   7. the retry loop stops at the budget;
 *   8. `?deliver_now=1` runs the whole path and CANNOT write the ledger;
 *   9. the topic value never reaches a log, an error or a ledger field;
 *  10. the delivery path reaches ntfy and NOTHING ELSE, with no GH_TOKEN in
 *      scope at all;
 *  11. a model's opinion — inside the artifact or inside the ledger — can
 *      neither fake a send nor suppress one;
 *  12. the ingest endpoint fails closed, validates, and never touches the
 *      ledger;
 *  13. U5. THE SWITCH. The digest crons now call deliverDigest, the 5-minute
 *      re-fire cron is a no-op that costs one KV read, dispatchDigest is
 *      RETAINED so the switch back is one line, and the artifact expiry that
 *      the recorded KV budget counts is real.
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const COMMITTED_WORKER = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.DIGEST_DELIVERY_WORKER || COMMITTED_WORKER;

if (process.env.DIGEST_DELIVERY_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (DIGEST_DELIVERY_WORKER) INSTEAD of " +
    "the committed " + COMMITTED_WORKER + ".\n");
}

/** Names the harness re-exports from the transformed Worker source. */
const EXPORT_NAMES = [
  "WORKER", "deliverDigest", "deliverDigestPlan", "commitDigestLedger",
  "validateArtifact", "digestHealthFields", "handleDigestIngest",
  "handleDeliverNow", "findDeliveredBySequenceId", "normaliseDigestLedger",
  "digestSequenceId", "digestFullSequenceId", "digestArchiveHost",
  "digestArchiveOrigin", "digestArchiveIndexUrl", "deliveryDeltaSeconds",
  "DIGEST_LEDGER_KEY", "DIGEST_ARTIFACT_PREFIX", "DIGEST_SCHEMA_VERSION",
  "DIGEST_SLOTS", "DIGEST_CRON_ARM",
  "DELIVERY_ON_TIME_S", "DELIVERY_RETENTION_DAYS", "ARTIFACT_MAX_BUILD_AGE_S",
  "NTFY_TITLE_MAX_CHARS", "NTFY_BODY_MAX_BYTES", "DELIVERY_ATTEMPTS",
  "DELIVERY_BUDGET_MS", "DELIVERY_ATTEMPT_TIMEOUT_MS",
  "INGEST_TOKEN_HEADER", "INGEST_TOKEN_SECRET",
  "KIND_DELIVERED", "KIND_PARTIAL", "KIND_FALLBACK_LAST_GOOD",
  "KIND_UNAVAILABLE", "KIND_DUPLICATE_SUPPRESSED", "KIND_UNSUMMARISED",
  "STATE_PENDING", "STATE_SENT",
  "CRON_DIGEST_MORNING", "CRON_DIGEST_EVENING",
  "CRON_DIGEST_REFIRE_MORNING", "CRON_DIGEST_REFIRE_EVENING",
  // The merged dispatcher. CRON_DISPATCHER is the ONE deployed trigger;
  // planTickFor is the pure function that decides a tick, exported so the
  // cadence-parity proof above runs the Worker's own arithmetic rather than a
  // copy of it.
  "CRON_DISPATCHER", "CRON_BREAKING_SWEEP", "CRON_ACADEMIC_SWEEP",
  "CRON_HEALTHCHECK", "BREAKING_HEARTBEAT_KEY", "ACADEMIC_HEARTBEAT_KEY",
  "touchHeartbeat", "planTickFor", "dispatchSchedule", "refireWindowFor",
  "REFIRE_GAP_MINUTES", "REFIRE_ATTEMPTS",
  "ARTIFACT_TTL_S", "DISPATCH_URL", "DIGEST_DISPATCH_URL",
  // §14. The honesty split: the derived edition of the last delivery, and the
  // once-per-slot recovery alert that names what was actually shown.
  "deliveryEdition", "announceStaleEditionOnce",
  "STALE_EDITION_ALERT_KEY_PREFIX",
  // §15. The simulated-proof-run guards: what a ledger row must prove before it
  // may withhold a slot, and the title marker that makes a test push ignorable
  // by every later read-back.
  "slotWasServed", "slotDueMs", "isTestPushTitle", "TEST_TITLE_PREFIX",
  "SLOT_EARLY_SLACK_MS",
];

const loadCounter = { n: 0 };

/**
 * Load the Worker's real source as a CommonJS module.
 *
 * VERBATIM from tests/breaking/worker_heartbeat.test.js. The only edit is
 * `export default {` -> `const WORKER = {`; everything else, including every
 * function under test, is the committed bytes. Each call gets a unique temp
 * path so node's require cache cannot leak module state between tests — the
 * equivalent of a cold isolate, which is what a Worker really is.
 */
function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " — the harness cannot " +
      "reach the Workers entry points, so this suite would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-digest-"));
  const file = path.join(dir, "worker-" + (loadCounter.n++) + ".cjs");
  const shim =
    "\nmodule.exports = { " + EXPORT_NAMES.join(", ") + " };\n";
  fs.writeFileSync(file, src.replace("export default {", "const WORKER = {") + shim,
                  "utf8");
  let mod;
  try {
    mod = require(file);
  } catch (err) {
    throw new Error(
      "loading the Worker harness failed: " + String(err && err.message || err) +
      "\nThe suite re-exports [" + EXPORT_NAMES.join(", ") +
      "]; a missing name means the Worker no longer defines it and this suite " +
      "must be updated deliberately rather than silently.");
  }
  fs.rmSync(dir, { recursive: true, force: true });
  return mod;
}

// ---------------------------------------------------------------------------
// Fixtures. One fixed instant, so "today" in Nepal is one fixed string.
// ---------------------------------------------------------------------------

const HOUR = 60 * 60 * 1000;
const MIN = 60 * 1000;
/** 2026-10-09T01:36:00Z = 07:06 NPT on 2026-10-09. The morning slot was DUE at
 *  01:35Z (CRON_DIGEST_MORNING), so this clock is 60s past the scheduled
 *  instant — exactly DELIVERY_ON_TIME_S, i.e. the boundary between on time and
 *  late, which is the only interesting value to assert punctuality against. */
const T0 = Date.parse("2026-10-09T01:36:00Z");
const TODAY = "2026-10-09";
const YESTERDAY = "2026-10-08";
const SLOT = "morning";

const NTFY_SERVER = "https://ntfy.example.invalid";
/** A distinctive value, so "the topic must never be logged" is a string search
 *  with a hit count and not a vague intention. Never printed by this file. */
const TOPIC = "dmn-secret-topic-7f3a91c4";
const INGEST_SECRET = "ingest-token-under-test";

/** The archive host every fixture uses. It is the SANITISED name, not the
 *  deployed one, and it reaches the Worker the only way it can: through env.
 *
 *  THE WORKER NO LONGER INVENTS A HOST. digestArchiveOrigin() throws when
 *  DIGEST_ARCHIVE_ORIGIN is absent, so a test env that omits it is testing a
 *  Worker that refuses to start work — which is why it is set here, once, for
 *  every test that runs a delivery or an ingest. */
const ARCHIVE_HOST = "daily-digest.example.pages.dev";
const ARCHIVE_ENV = { DIGEST_ARCHIVE_ORIGIN: "https://" + ARCHIVE_HOST };
const ARCHIVE_PAGE = "https://" + ARCHIVE_HOST + "/digests/2026-10-09-morning.html";

function ntfyEnv(kv, extra) {
  return Object.assign({
    KV: kv,
    NTFY_SERVER: NTFY_SERVER,
    NTFY_TOPIC: TOPIC,
    DIGEST_INGEST_TOKEN: INGEST_SECRET,
    DIGEST_ARCHIVE_ORIGIN: ARCHIVE_ENV.DIGEST_ARCHIVE_ORIGIN,
    WORKER_BUILD: "testbuild",
    // NOTE: no GH_TOKEN anywhere in this file's env. The delivery path must not
    // need it, and the "no GitHub API" test below asserts that it does not.
  }, extra || {});
}

function artifact(overrides) {
  const base = {
    schema_version: 1,
    date: TODAY,
    slot: SLOT,
    built_at: new Date(T0 - 4 * MIN).toISOString(),
    page_url: ARCHIVE_PAGE,
    is_partial: false,
    ntfy: {
      title: "Morning Digest · 2026-10-09 · 12 items",
      body: "All sources reported.\n\n* [WORLD] Something happened today",
    },
  };
  const out = Object.assign({}, base, overrides || {});
  out.ntfy = Object.assign({}, base.ntfy, (overrides || {}).ntfy || {});
  return JSON.stringify(out);
}

function artifactKey(date, slot) {
  return "digest:artifact:" + (date || TODAY) + ":" + (slot || SLOT);
}

function ledgerWith(entry, extra) {
  return JSON.stringify(Object.assign({
    schema_version: 1,
    as_of: new Date(T0 - MIN).toISOString(),
    days: { [TODAY]: { [SLOT]: entry } },
  }, extra || {}));
}

/** A ledger recording a completed send for today's morning slot. */
function ledgerSent(extra) {
  return ledgerWith({
    state: "sent",
    kind: "delivered",
    sequence_id: "dmn-" + TODAY + "-" + SLOT,
    ntfy_sent_at: new Date(T0 - 30 * 1000).toISOString(),
    first_ntfy_sent_at: new Date(T0 - 30 * 1000).toISOString(),
    scheduled_instant: "2026-10-09T01:35:00Z",
  }, extra);
}

function lastGood(date, body) {
  return {
    date: date,
    slot: SLOT,
    page_url: "https://daily-digest.example.pages.dev/digests/" + date + "-morning.html",
    title: "Morning Digest · " + date + " · 9 items",
    body: body || "Yesterday's digest, which did build.",
    built_at: date + "T01:30:00Z",
    at: date + "T01:35:00Z",
  };
}

// ---------------------------------------------------------------------------
// Harness: a fake KV, a fake network with a shared event log, a fake clock and
// a log capture. VERBATIM in shape from tests/breaking/worker_heartbeat.test.js,
// with the one addition the write-ahead ordering test needs: kv and fetch share
// ONE ordered event log, because "the ledger write happened before the POST" is
// a statement about order, not about either artifact alone.
// ---------------------------------------------------------------------------

function makeHarness(opts) {
  const o = opts || {};
  const events = [];
  const store = Object.assign({}, o.initial || {});
  const kv = {
    store: store,
    events: events,
    gets: [],
    puts: [],
    async get(key) {
      kv.gets.push(key);
      events.push({ t: "kv.get", key: key });
      if ((o.failGet || []).indexOf(key) >= 0) {
        throw new Error("injected KV read failure on " + key);
      }
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    async put(key, value, extra) {
      if ((o.failPut || []).indexOf(key) >= 0) {
        throw new Error("injected KV write failure on " + key);
      }
      kv.puts.push({ key: key, value: value, extra: extra || null });
      events.push({ t: "kv.put", key: key });
      store[key] = value;
    },
  };

  const net = { calls: [] };
  let postCount = 0;
  globalThis.fetch = async function (url, init) {
    const method = String((init && init.method) || "GET").toUpperCase();
    const u = String(url);
    net.calls.push({
      method: method, url: u,
      headers: (init && init.headers) || {},
      body: init && init.body,
    });
    events.push({ t: method === "POST" ? "http.post" : "http.get", url: u });
    if (method === "POST") {
      postCount += 1;
      if ((o.failPost || []).indexOf(postCount) >= 0) {
        if (o.failPostThrows) {
          throw new Error("injected transport failure (url was " + u + ")");
        }
        return { ok: false, status: 500, text: async () => "server error" };
      }
      if (o.onPost) {
        const forced = o.onPost(postCount, net.calls[net.calls.length - 1], kv);
        if (forced) return forced;
      }
      return {
        ok: true, status: 200,
        text: async () => JSON.stringify({ id: "ntfy-msg-" + postCount }),
      };
    }
    // GET. The only GET in the delivery path is the sequence_id read-back.
    if (o.failReadback) {
      throw new Error("injected read-back transport failure (url was " + u + ")");
    }
    return {
      ok: o.readbackStatus ? false : true,
      status: o.readbackStatus || 200,
      text: async () => JSON.stringify(o.cache || []),
    };
  };
  return { kv: kv, net: net, events: events };
}

function putCount(kv, key) {
  return kv.puts.filter((p) => p.key === key).length;
}

function postCalls(net) {
  return net.calls.filter((c) => c.method === "POST");
}

function ledgerOf(kv) {
  const raw = kv.store["digest:ledger"];
  return raw ? JSON.parse(raw) : null;
}

/** Set the clock, so "1 minute later" and "the budget is nearly spent" are real. */
function fakeClock(startMs) {
  const realNow = Date.now;
  let current = startMs;
  Date.now = function () { return current; };
  return {
    advance(ms) { current += ms; },
    restore() { Date.now = realNow; },
  };
}

/** Capture the Worker's own logging without touching the test reporter. */
function captureLogs() {
  const lines = [];
  const real = { log: console.log, error: console.error, warn: console.warn };
  const collect = (level) => (...args) => {
    lines.push(level + ": " + args.map((a) => String(a)).join(" "));
  };
  console.log = collect("log");
  console.error = collect("error");
  console.warn = collect("warn");
  return {
    lines: lines,
    text() { return lines.join("\n"); },
    restore() {
      console.log = real.log;
      console.error = real.error;
      console.warn = real.warn;
    },
  };
}

/**
 * Run the delivery path and return everything the assertions need.
 *
 * `slot` and `initial` are the two knobs most tests want; the clock is fixed so
 * "today" is one string and the punctuality delta is a known number.
 */
async function deliver(w, options) {
  const o = options || {};
  const harness = makeHarness({ initial: o.initial, cache: o.cache,
    failGet: o.failGet, failPost: o.failPost, failReadback: o.failReadback,
    readbackStatus: o.readbackStatus, onPost: o.onPost,
    failPostThrows: o.failPostThrows });
  const logs = captureLogs();
  let outcome = null;
  let thrown = null;
  try {
    outcome = await w.deliverDigest(ntfyEnv(harness.kv, o.env), o.slot || SLOT, {
      now: () => (o.now === undefined ? T0 : o.now),
      sleep: o.sleep || (async () => {}),
    });
  } catch (err) {
    thrown = err;
  } finally {
    logs.restore();
  }
  return { kv: harness.kv, net: harness.net, events: harness.events,
           logs: logs, outcome: outcome, thrown: thrown };
}

async function runScheduled(WORKER, cron, env) {
  const pending = [];
  await WORKER.scheduled({ cron: cron }, env, { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
}

/**
 * Run a CRON with the clock pinned to T0.
 *
 * `scheduled()` threads the tick's scheduled instant down to `deliverDigest`
 * (`scheduledJobs` -> `{scheduledMs}`, and `deliverDigestPlan` dates the slot
 * from it), so a controller carrying `scheduledTime` gets the TICK's Nepal date.
 * This wrapper deliberately passes none of that — the legacy shape, where
 * `tickInstantMs` falls back to `Date.now()` — so the date here still comes from
 * the wall clock. Every cron-driven test below therefore has to pin the clock
 * itself, or it is testing whatever day the suite happens to run on and every
 * fixture key (which is anchored on TODAY) is a key for a day that does not
 * exist. The one test that covers the scheduled-instant path passes a
 * `scheduledTime` explicitly, and pins the clock on the OTHER side of NPT
 * midnight so the two cannot be confused.
 */
async function runScheduledAtT0(WORKER, cron, env) {
  const clock = fakeClock(T0);
  try {
    await runScheduled(WORKER, cron, env);
  } finally {
    clock.restore();
  }
}

// ==========================================================================
// 1. validateArtifact — the wrong-day refusal is the one that matters most
// ==========================================================================

test("validateArtifact accepts a well-formed artifact for today and this slot", async () => {
  const w = loadWorker();
  const out = w.validateArtifact(artifact(), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(out.ok, true, JSON.stringify(out));
  assert.strictEqual(out.artifact.slot, SLOT);
  assert.strictEqual(out.artifact.date, TODAY);
});

test("validateArtifact refuses an absent artifact as `absent`", async () => {
  const w = loadWorker();
  for (const raw of [null, undefined, ""]) {
    const out = w.validateArtifact(raw, SLOT, TODAY, T0, ARCHIVE_ENV);
    assert.strictEqual(out.ok, false);
    assert.strictEqual(out.reason, "absent", "for " + JSON.stringify(raw));
    assert.ok(/no digest was built/.test(out.plain),
      "every rejection must carry a sentence for the owner: " + out.plain);
  }
});

test("validateArtifact refuses unparseable JSON as `corrupt`, not as absent", async () => {
  const w = loadWorker();
  const out = w.validateArtifact("{not json", SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.reason, "corrupt");
  assert.ok(/could not be parsed/.test(out.plain), out.plain);
  const notARecord = w.validateArtifact("[1,2,3]", SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(notARecord.reason, "corrupt");
});

test("validateArtifact refuses a schema_version other than 1", async () => {
  const w = loadWorker();
  const out = w.validateArtifact(artifact({ schema_version: 2 }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.reason, "schema");
  assert.ok(/version 2/.test(out.plain), out.plain);
});

test("validateArtifact refuses an artifact filed under the other slot", async () => {
  const w = loadWorker();
  const out = w.validateArtifact(artifact({ slot: "evening" }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.reason, "slot");
  assert.ok(/evening/.test(out.plain), out.plain);
});

test("validateArtifact refuses YESTERDAY's digest — the critical refusal", async () => {
  const w = loadWorker();
  const out = w.validateArtifact(artifact({ date: YESTERDAY }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(out.ok, false,
    "a well-formed artifact dated yesterday was ACCEPTED as today's digest. " +
    "Every other field is correct, so nothing downstream could tell — the " +
    "owner would read yesterday's news believing it is today's.");
  assert.strictEqual(out.reason, "stale_date");
  assert.ok(/NOT today's digest/.test(out.plain),
    "the sentence the owner reads must say outright that this is not today's " +
    "digest, because it is being labelled as yesterday's on purpose: " + out.plain);
  assert.ok(new RegExp(YESTERDAY).test(out.plain),
    "the sentence must name the date that is actually on the record: " + out.plain);
});

test("validateArtifact refuses an artifact older than the 26h freshness limit", async () => {
  const w = loadWorker();
  const stale = artifact({ built_at: new Date(T0 - (w.ARTIFACT_MAX_BUILD_AGE_S + 60) * 1000).toISOString() });
  const out = w.validateArtifact(stale, SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.reason, "stale_build");
  assert.ok(/past the 26h freshness limit/.test(out.plain), out.plain);
});

test("validateArtifact refuses an artifact with no usable build time, or one from the future", async () => {
  const w = loadWorker();
  const missing = w.validateArtifact(artifact({ built_at: "" }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(missing.ok, false);
  assert.strictEqual(missing.reason, "stale_build");
  const future = w.validateArtifact(
    artifact({ built_at: new Date(T0 + 30 * MIN).toISOString() }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(future.ok, false,
    "an artifact stamped half an hour in the FUTURE was accepted, so every " +
    "age computed from it — including the freshness limit just above — is a " +
    "fiction.");
  assert.strictEqual(future.reason, "stale_build");
});

test("validateArtifact refuses an absent or off-archive page_url", async () => {
  const w = loadWorker();
  const absent = w.validateArtifact(artifact({ page_url: "" }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(absent.ok, false);
  assert.strictEqual(absent.reason, "page");
  // An off-host click target is the shape a phishing push takes.
  for (const bad of ["https://evil.example.invalid/x.html",
                     "https://daily-digest.example.pages.dev.evil.invalid/x.html",
                     "not-a-url"]) {
    const out = w.validateArtifact(artifact({ page_url: bad }), SLOT, TODAY, T0, ARCHIVE_ENV);
    assert.strictEqual(out.ok, false, "accepted " + bad);
    assert.strictEqual(out.reason, "page", "for " + bad);
  }
});

test("validateArtifact refuses an empty or over-long title, at exactly 72 chars", async () => {
  const w = loadWorker();
  assert.strictEqual(w.NTFY_TITLE_MAX_CHARS, 72);
  const empty = w.validateArtifact(artifact({ ntfy: { title: "" } }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(empty.reason, "title");
  const atLimit = w.validateArtifact(
    artifact({ ntfy: { title: "T".repeat(72) } }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(atLimit.ok, true, "72 chars is the ceiling, not the first rejected value");
  const over = w.validateArtifact(
    artifact({ ntfy: { title: "T".repeat(73) } }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(over.ok, false);
  assert.strictEqual(over.reason, "title");
  assert.ok(/73 characters/.test(over.plain), over.plain);
});

test("validateArtifact measures the body in BYTES, not characters", async () => {
  const w = loadWorker();
  assert.strictEqual(w.NTFY_BODY_MAX_BYTES, 4096);
  const empty = w.validateArtifact(artifact({ ntfy: { body: "" } }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(empty.reason, "body");
  // 2049 emoji = 8196 bytes but only 2049 characters: a character count would
  // have accepted this and the server would have rejected it.
  const overBytes = w.validateArtifact(
    artifact({ ntfy: { body: "🙂".repeat(2049) } }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(overBytes.ok, false,
    "a 2049-character body of 4-byte emoji was accepted; ntfy counts bytes");
  assert.strictEqual(overBytes.reason, "body");
  const atLimit = w.validateArtifact(
    artifact({ ntfy: { body: "a".repeat(4096) } }), SLOT, TODAY, T0, ARCHIVE_ENV);
  assert.strictEqual(atLimit.ok, true, "4096 bytes is the ceiling");
});

// ==========================================================================
// 2 + 3. The two duplicate guards, in order
// ==========================================================================

test("the ledger alone suppresses a second send of today's morning digest", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: {
    "digest:ledger": ledgerSent(),
    [artifactKey()]: artifact(),
  } });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 0,
    "the ledger already records this slot as sent and a SECOND push went out " +
    "anyway. A digest read twice is a digest nobody reads twice.");
  assert.strictEqual(r.outcome.kind, w.KIND_DUPLICATE_SUPPRESSED);
  assert.ok(/duplicate_suppressed/.test(r.logs.text()),
    "the suppression must be logged, not merely performed: " + r.logs.text());
  assert.strictEqual(putCount(r.kv, "digest:ledger"), 0,
    "a suppressed duplicate must not rewrite the ledger either");
});

test("the sequence_id read-back suppresses the send when the LEDGER IS BLIND", async () => {
  const w = loadWorker();
  // The ledger is EMPTY — the write that should have recorded the first send was
  // lost, which is the one failure the ledger cannot see. The read-back is the
  // only guard left, so it has to work on its own.
  const seq = "dmn-" + TODAY + "-" + SLOT;
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    cache: [{ id: "aaa", time: (T0 - 60 * 1000) / 1000, sequence_id: seq }],
  });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 0,
    "the ledger was empty but the digest was already on the topic (" + seq +
    "), and the push went out anyway: a double push that the ledger cannot " +
    "prevent on its own.");
  assert.strictEqual(r.outcome.deduped, true);
  assert.ok(/sequence_id/.test(r.logs.text()), r.logs.text());
  // And the record it now writes must be a real measured instant: the read-back
  // reported `time`, so punctuality is anchored to when the push happened rather
  // than to when this run happened to notice.
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, w.STATE_SENT);
  assert.strictEqual(entry.ntfy_sent_at, "2026-10-09T01:35:00Z");
  assert.strictEqual(entry.first_ntfy_sent_at, "2026-10-09T01:35:00Z");
  assert.strictEqual(entry.delta_s, undefined,
    "delta_s is computed on read, not stored: a stored delta would be a " +
    "number that could disagree with the two instants it came from");
});

test("an unreadable read-back does NOT suppress the push — a push always happens", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    failReadback: true,
  });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 1,
    "the sequence_id read-back failed and the digest was NOT pushed. " +
    "Unknown is not a reason to stay silent: the possible cost is a duplicate " +
    "the owner can see, the alternative is a missing day he cannot.");
  assert.ok(/unverified/.test(r.logs.text()), r.logs.text());
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, w.STATE_SENT);
});

// ==========================================================================
// 4. Write-ahead ordering, and the header ntfy actually reads
// ==========================================================================

test("`pending` is on disk BEFORE the first POST, and the sequence_id rides in a header", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: { [artifactKey()]: artifact() } });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 1);

  const firstPut = r.events.findIndex((e) => e.t === "kv.put" && e.key === "digest:ledger");
  const firstPost = r.events.findIndex((e) => e.t === "http.post");
  assert.ok(firstPut >= 0 && firstPost >= 0, "expected both a ledger write and a POST");
  assert.ok(firstPut < firstPost,
    "the first POST went out BEFORE anything was written to the ledger. A " +
    "crash here loses the send silently — the exact failure the write-ahead " +
    "marker exists to prevent, and the reason a miss is treated as worse than " +
    "a duplicate.\nevents: " + JSON.stringify(r.events, null, 1));

  const writes = r.kv.puts.filter((p) => p.key === "digest:ledger")
    .map((p) => JSON.parse(p.value).days[TODAY][SLOT]);
  assert.ok(writes.length >= 2, "expected a pending write and a sent write");
  assert.strictEqual(writes[0].state, w.STATE_PENDING,
    "the first ledger write for a slot must be `pending`: " + JSON.stringify(writes[0]));
  assert.strictEqual(writes[writes.length - 1].state, w.STATE_SENT);

  // THE HEADER IS THE GUARD. ntfy reads `sequence_id` from `X-Sequence-Id` and
  // nowhere else; a `sequence_id` key in the JSON body is dropped exactly like
  // the `id` key was, and every read-back above would then never fire.
  const post = postCalls(r.net)[0];
  assert.strictEqual(post.headers["X-Sequence-Id"], "dmn-" + TODAY + "-" + SLOT,
    "the POST carried no X-Sequence-Id header, so the read-back dedup can " +
    "never match anything and the second guard is decoration.\nheaders: " +
    JSON.stringify(post.headers));
  const payload = JSON.parse(post.body);
  assert.strictEqual(payload.topic, TOPIC);
  assert.ok(!("id" in payload),
    "the payload must not carry an `id`: ntfy's publish struct has no such " +
    "field and drops it silently, which is what B3 measured");
  assert.strictEqual(w.digestSequenceId(TODAY, SLOT).length, 22);
  assert.strictEqual(w.digestFullSequenceId(TODAY, SLOT),
                     "dmn-" + TODAY + "-" + SLOT + "-full");
});

test("a push is retried on a 500 and recorded with the attempt that worked", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    failPost: [1, 2],
  });
  assert.strictEqual(postCalls(r.net).length, 3,
    "two 500s and a third push never happened; DELIVERY_ATTEMPTS is " + w.DELIVERY_ATTEMPTS);
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, w.STATE_SENT);
  assert.strictEqual(entry.attempts, 3);
  assert.strictEqual(entry.http_status, 200);
});

test("a push that never succeeds keeps the slot FAILED and re-pushable, never `sent`", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    failPost: [1, 2, 3],
  });
  assert.strictEqual(r.thrown, null, "a failed push must not reject out of the cron");
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, "failed");
  assert.ok(!entry.ntfy_sent_at,
    "a slot with no successful push recorded an ntfy_sent_at, so the ledger " +
    "guard would suppress the re-push that recovery depends on");
  assert.ok(/NOT pushed after 3 attempt/.test(r.logs.text()), r.logs.text());
});

test("a verified publish failure records ntfy_failed", async () => {
  const w = loadWorker();
  // The producer side of the 07:50 ladder trigger. A publish that exhausted
  // every attempt and came back an error is a MEASURED fact, so it is written
  // down as one: `ntfy_failed: true`. Without it the 07:50 fallback reads this
  // entry as "no instant recorded" (unknown), falls back to a marker file that
  // can still claim the slot was delivered, and stands the email/Telegram
  // ladder down on exactly the day the primary channel failed.
  const failed = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    failPost: [1, 2, 3],
  });
  assert.strictEqual(failed.thrown, null, "a failed push must not reject out of the cron");
  const badEntry = ledgerOf(failed.kv).days[TODAY][SLOT];
  assert.strictEqual(badEntry.state, "failed");
  assert.strictEqual(badEntry.ntfy_failed, true,
    "a publish that failed all " + w.DELIVERY_ATTEMPTS + " attempts did not " +
    "record `ntfy_failed`. That flag is the only thing that arms the 07:50 " +
    "GitHub fallback ladder, and nothing else records it.");
  assert.ok(!badEntry.ntfy_sent_at,
    "a failed push recorded an ntfy_sent_at, so the ledger guard would " +
    "suppress the re-push recovery depends on");

  // AND IT IS CLEARED ON THE NEXT SUCCESS. `commitDigestLedger` merges with
  // Object.assign, so a success that omitted the flag would leave a stale true
  // beside a fresh ntfy_sent_at. `ntfy_failed` OUTRANKS `ntfy_sent_at` in the
  // fallback step's verdict, so every later run for that slot would email and
  // Telegram the owner a second copy of a digest their phone already received.
  const ledgerAfterFailure = failed.kv.store["digest:ledger"];
  const recovered = await deliver(w, {
    initial: { [artifactKey()]: artifact(), "digest:ledger": ledgerAfterFailure },
  });
  const goodEntry = ledgerOf(recovered.kv).days[TODAY][SLOT];
  assert.strictEqual(goodEntry.state, "sent");
  assert.ok(goodEntry.ntfy_sent_at,
    "the recovery push did not record an instant, so this test is proving " +
    "nothing about the flag it is checking");
  assert.strictEqual(goodEntry.ntfy_failed, false,
    "a successful push left `ntfy_failed` at true. The next 07:50 run reads " +
    "ntfy_failed FIRST, so a delivered slot would fire the ladder and email " +
    "the owner a duplicate.");
});

test("a FAILED fallback push records ntfy_failed too", async () => {
  const w = loadWorker();
  // The ladder's own push is a delivery like any other. If the "last good"
  // push fails, the owner has nothing at all — which is precisely the case the
  // email/Telegram backup exists for, and it cannot fire on an entry that never
  // says so.
  const r = await deliver(w, {
    initial: {
      "digest:ledger": ledgerWith(null, {
        last_good: lastGood(YESTERDAY, "yesterday's digest, which did build"),
      }),
    },
    failPost: [1, 2, 3],
  });
  assert.strictEqual(r.thrown, null);
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.kind, "fallback_last_good");
  assert.strictEqual(entry.ntfy_failed, true,
    "the fallback push failed on every attempt and did not record " +
    "`ntfy_failed`, so the 07:50 backup ladder would stand itself down on a " +
    "slot that reached nobody.");
});

test("a read-back dedup CLEARS ntfy_failed, because the digest is on the topic", async () => {
  const w = loadWorker();
  // The ledger was blind (the write was lost) but ntfy has the message. That
  // is a DELIVERED slot, so the flag must not survive a stale true from an
  // earlier failed attempt.
  const seqId = w.digestSequenceId(TODAY, SLOT);
  const r = await deliver(w, {
    initial: {
      [artifactKey()]: artifact(),
      "digest:ledger": JSON.stringify(ledgerWith({
        state: "failed", kind: "delivered", ntfy_failed: true,
        ntfy_sent_at: null, first_ntfy_sent_at: null,
      })),
    },
    cache: [{ id: "m1", time: 1, sequence_id: seqId }],
  });
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, "sent");
  assert.ok(entry.deduped_via, "the read-back did not record how it knew");
  assert.strictEqual(entry.ntfy_failed, false,
    "the read-back proved the digest IS on the topic and left `ntfy_failed` " +
    "at true, so the 07:50 run would fire the ladder at a delivered slot");
});

test("the retry loop stops at DELIVERY_BUDGET_MS instead of starting an attempt that cannot fit", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  let posts = 0;
  try {
    const harness = makeHarness({
      initial: { [artifactKey()]: artifact() },
      onPost: () => {
        posts += 1;
        // Each attempt burns 20s of wall clock: 8s of it is the attempt, the
        // rest is the clock the fake fetch is pretending to have spent.
        clock.advance(20 * 1000);
        return { ok: false, status: 503, text: async () => "busy" };
      },
    });
    const logs = captureLogs();
    await w.deliverDigest(ntfyEnv(harness.kv), SLOT, {
      now: () => Date.now(),
      sleep: async (ms) => clock.advance(ms),
    });
    logs.restore();
    assert.ok(posts > 0 && posts < w.DELIVERY_ATTEMPTS,
      "every attempt started even though the budget was spent (" + posts +
      " of " + w.DELIVERY_ATTEMPTS + "). The gate exists so a slow ntfy cannot " +
      "hold the cron invocation open past its budget.");
    assert.ok(/NOT starting attempt/.test(logs.text()), logs.text());
  } finally {
    clock.restore();
  }
});

// ==========================================================================
// 5. The fallback ladder — and it must stay silent when the digest goes out
// ==========================================================================

test("a good digest pushes ONCE, with the artifact's own body and no fallback wrapper", async () => {
  const w = loadWorker();
  const art = JSON.parse(artifact());
  const r = await deliver(w, { initial: { [artifactKey()]: artifact() } });
  const posts = postCalls(r.net);
  assert.strictEqual(posts.length, 1);
  const payload = JSON.parse(posts[0].body);
  assert.strictEqual(payload.message, art.ntfy.body);
  assert.strictEqual(payload.title, art.ntfy.title);
  assert.strictEqual(payload.click, art.page_url);
  assert.ok(!/Why you are seeing this/.test(payload.message),
    "the fallback ladder fired on a delivery that SUCCEEDED. The owner would " +
    "be told his digest is delayed when it arrived normally — an alarm that " +
    "cries wolf on the happy path is an alarm he stops reading.");
  assert.ok(!/Why you are seeing this/.test(r.logs.text()), r.logs.text());
  assert.strictEqual(r.outcome.kind, w.KIND_DELIVERED);
  // last_good advances ONLY on a real digest: a fallback must never become
  // tomorrow's fallback, or one bad day feeds itself for ever.
  assert.strictEqual(ledgerOf(r.kv).last_good.date, TODAY);
});

test("a PARTIAL artifact IS sent — a labelled partial beats a silent day", async () => {
  const w = loadWorker();
  const art = JSON.parse(artifact({ is_partial: true }));
  const r = await deliver(w, { initial: { [artifactKey()]: artifact({ is_partial: true }) } });
  const posts = postCalls(r.net);
  assert.strictEqual(posts.length, 1,
    "an artifact marked is_partial was NOT pushed. Its body already leads " +
    "with the honesty banner, so an incomplete day that SAYS SO is strictly " +
    "more useful to the owner than silence about it.");
  assert.strictEqual(r.outcome.kind, w.KIND_PARTIAL);
  assert.strictEqual(JSON.parse(posts[0].body).message, art.ntfy.body);
  assert.strictEqual(ledgerOf(r.kv).days[TODAY][SLOT].is_partial, true);
});

test("an UNSUMMARISED artifact is pushed as its own kind and never becomes last_good", async () => {
  const w = loadWorker();
  // A last_good on file from yesterday, so "did not advance" is a statement
  // about a real previous value and not an assertion about null.
  const previous = lastGood(YESTERDAY, "Yesterday's real digest.");
  const r = await deliver(w, {
    initial: {
      [artifactKey()]: artifact({
        edition_kind: "unsummarised",
        ntfy: {
          title: "Morning Digest · 2026-10-09 · unsummarised edition (no AI today)",
        },
      }),
      "digest:ledger": JSON.stringify({
        schema_version: 1,
        as_of: new Date(T0 - HOUR).toISOString(),
        days: {},
        last_good: previous,
      }),
    },
  });
  const posts = postCalls(r.net);
  assert.strictEqual(posts.length, 1,
    "the no-AI edition was not pushed at all. Headlines with no summaries are " +
    "still today's news, and silence is the worse outcome.");
  assert.strictEqual(r.outcome.kind, w.KIND_UNSUMMARISED,
    "the artifact said edition_kind=unsummarised and the delivery filed it as " +
    JSON.stringify(r.outcome.kind));

  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.kind, "unsummarised");
  assert.strictEqual(entry.state, "sent");
  assert.strictEqual(entry.edition, "unsummarised",
    "?health=1 and every ledger reader answer with `edition`, which the Worker " +
    "derives. A no-AI edition filed as 'fresh' is the same lie the fallback " +
    "kind was introduced to stop: it reads as a normal digest to anyone who " +
    "stops at `state: sent`.");

  assert.strictEqual(ledgerOf(r.kv).last_good.date, YESTERDAY,
    "an unsummarised edition advanced last_good, so a model outage on day 1 " +
    "becomes the fallback offered on day 2 — headlines with no summaries, " +
    "served as the best available edition.");
});

test("a stale-dated artifact falls back to the last good digest, LABELLED as older", async () => {
  const w = loadWorker();
  const good = lastGood(YESTERDAY, "Yesterday's news, which did build.");
  const r = await deliver(w, {
    initial: {
      [artifactKey()]: artifact({ date: YESTERDAY }),
      "digest:ledger": JSON.stringify({
        schema_version: 1, as_of: new Date(T0 - HOUR).toISOString(),
        days: {}, last_good: good,
      }),
    },
  });
  const posts = postCalls(r.net);
  // TWO POSTs since 5d added the once-per-slot stale-edition alert: the fallback
  // itself, plus the warning that says the edition is old. The count is pinned
  // exactly — "at least one" would let a third message appear unnoticed.
  assert.strictEqual(posts.length, 2);
  const fallbackPosts = posts.filter((c) =>
    c.headers["X-Sequence-Id"] === w.digestFullSequenceId(TODAY, SLOT));
  assert.strictEqual(fallbackPosts.length, 1);
  const payload = JSON.parse(fallbackPosts[0].body);
  assert.strictEqual(payload.title, "⚠️ Morning digest delayed — showing " + YESTERDAY);
  assert.ok(/^\*\*Why you are seeing this\*\*/.test(payload.message),
    "the fallback must OPEN with why he is seeing it: " + payload.message);
  assert.ok(/NOT today's digest/.test(payload.message),
    "the fallback must say in as many words that this is not today's digest: " +
    payload.message);
  assert.ok(payload.message.indexOf(good.body) > 0,
    "the last good digest's body must actually be shown: " + payload.message);
  assert.strictEqual(payload.click, good.page_url);
  assert.strictEqual(fallbackPosts[0].headers["X-Sequence-Id"],
    w.digestFullSequenceId(TODAY, SLOT));
  assert.strictEqual(r.outcome.kind, w.KIND_FALLBACK_LAST_GOOD);
  // The ledger must remember what was shown WITHOUT promoting it to last_good.
  const ledger = ledgerOf(r.kv);
  assert.strictEqual(ledger.last_good.date, YESTERDAY,
    "a fallback push overwrote last_good, so tomorrow's fallback would be " +
    "today's fallback — one bad day would feed itself for ever");
  assert.strictEqual(ledger.days[TODAY][SLOT].last_good_shown, YESTERDAY);
  // The rejection reason CARRIES THE DISPATCH VERDICT, so it is no longer a bare
  // cause. Asserted as a shape rather than an equality, and the shape is
  // STRICTER than the bare string it replaces: both the underlying cause and
  // the dispatch outcome must be present, and the verdict must come last so
  // nothing can be appended after it.
  assert.match(ledger.days[TODAY][SLOT].reject_reason,
    /^stale_date \| (a late build was dispatched|NO build was dispatched)$/,
    "the ledger's reject_reason must name the cause AND whether a late build "
    + "was dispatched, in that order: " +
    JSON.stringify(ledger.days[TODAY][SLOT].reject_reason));
});

test("no artifact and no last good still PUSHES, naming the reason", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: {} });
  const posts = postCalls(r.net);
  assert.strictEqual(posts.length, 1,
    "nothing was built and there was no earlier digest, and NO push went out. " +
    "Silence is the one outcome this path cannot produce: the owner has no way " +
    "to distinguish 'nothing happened' from 'the pipeline is broken'.");
  const payload = JSON.parse(posts[0].body);
  assert.strictEqual(payload.title, "⚠️ Morning digest unavailable");
  assert.ok(/no digest was built for morning/.test(payload.message), payload.message);
  assert.ok(payload.message.indexOf(w.digestArchiveIndexUrl(ARCHIVE_ENV)) >= 0,
    "the unavailable push must point at the archive index: " + payload.message);
  assert.strictEqual(payload.click, w.digestArchiveIndexUrl(ARCHIVE_ENV));
  assert.strictEqual(r.outcome.kind, w.KIND_UNAVAILABLE);
  assert.strictEqual(ledgerOf(r.kv).days[TODAY][SLOT].kind, w.KIND_UNAVAILABLE);
});

// ===========================================================================
// 5d. A FALLBACK IS NEVER REPORTED AS A DELIVERED EDITION.
//
// The ladder suppression elsewhere in this repo stays exactly as it is: a push
// the owner CAN read must not re-run the email/Telegram ladder, because he has
// already been given something to read (see
// tests/digest/test_ladder_trigger.py::test_a_push_the_owner_CAN_read_does_not_run_the_ladder).
//
// That contract is about the LADDER, and it was silently extended into a lie by
// the reporting layer: `state: "sent"` was read as "today's digest arrived", and
// a fallback_last_good entry carries `state: "sent"` because a push really did
// land. On 2026-10-27 the owner was shown the 18th's edition, `?health=1`
// reported a send, and nothing anywhere said the news was nine days old.
//
// So: `kind` and `state` keep their meaning for the ladder and for
// scripts/delivery_ledger.py, and `edition` answers the different question —
// WHICH edition was shown. A fallback must read as `edition: "fallback"` with
// `fresh_edition: false`, and the slot must get ONE recovery alert naming the
// edition actually shown and the reason the build failed.
// ===========================================================================

function staleAlerts(w, net) {
  const seq = "dmn-" + TODAY + "-" + SLOT + "-stale-notice";
  return postCalls(net).filter((c) => c.headers["X-Sequence-Id"] === seq);
}

test("deliveryEdition answers WHICH edition was shown, not whether a push landed", () => {
  const w = loadWorker();
  const none = (entry) => w.deliveryEdition(entry);
  assert.strictEqual(none(null), "unknown");
  assert.strictEqual(none({}), "unknown");
  assert.strictEqual(none({ state: "pending", kind: "delivered" }), "pending");
  // THE ONE THAT MATTERED. A landed fallback is `state: sent`.
  assert.strictEqual(
    w.deliveryEdition({ state: "sent", kind: "fallback_last_good",
                        ntfy_sent_at: "2026-10-09T01:36:00Z" }), "fallback");
  assert.strictEqual(
    w.deliveryEdition({ state: "sent", kind: "delivered",
                        ntfy_sent_at: "2026-10-09T01:36:00Z" }), "fresh");
  assert.strictEqual(
    w.deliveryEdition({ state: "sent", kind: "partial",
                        ntfy_sent_at: "2026-10-09T01:36:00Z" }), "partial");
  assert.strictEqual(
    w.deliveryEdition({ state: "sent", kind: "unavailable",
                        ntfy_sent_at: "2026-10-09T01:36:00Z" }), "notice");
  // The no-AI edition has its OWN word. Folding it into "partial" would say the
  // sources were thin; folding it into "fresh" would say a model wrote it.
  assert.strictEqual(
    w.deliveryEdition({ state: "sent", kind: "unsummarised",
                        ntfy_sent_at: "2026-10-09T01:36:00Z" }), "unsummarised");
  assert.strictEqual(
    w.deliveryEdition({ state: "failed", kind: "fallback_last_good",
                        ntfy_failed: true }), "failed");
  assert.strictEqual(none({ state: "sent", kind: "duplicate_suppressed" }),
    "duplicate");
  assert.strictEqual(none({ state: "sent", kind: "stood_down" }), "stood_down");
});

test("a fallback slot reads as a fallback in the ledger AND in ?health=1", async () => {
  const w = loadWorker();
  const good = lastGood(YESTERDAY, "Yesterday's news, which did build.");
  const r = await deliver(w, {
    initial: {
      [artifactKey()]: artifact({ date: YESTERDAY }),  // stale -> the ladder
      "digest:ledger": JSON.stringify({
        schema_version: 1, as_of: new Date(T0 - HOUR).toISOString(),
        days: {}, last_good: good,
      }),
    },
  });
  assert.strictEqual(r.outcome.kind, w.KIND_FALLBACK_LAST_GOOD);
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, "sent");
  assert.strictEqual(entry.edition, "fallback",
    "the ledger stored no edition for a fallback, so every reader has to infer " +
    "it from `kind` — the inference that made 2026-10-27 look like a delivery");
  assert.strictEqual(entry.last_good_shown, YESTERDAY);

  const clock = fakeClock(T0);
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?health=1"), ntfyEnv(r.kv));
  } finally {
    clock.restore();
  }
  const d = JSON.parse(await res.text()).digest;
  assert.strictEqual(d.last_delivery.edition, "fallback");
  assert.strictEqual(d.last_delivery.fresh_edition, false,
    "?health=1 called a fallback a fresh edition");
  assert.strictEqual(d.last_delivery.fallback_shown, true);
  assert.strictEqual(d.last_delivery.last_good_shown, YESTERDAY,
    "?health=1 must name the date of the edition actually shown");
  assert.ok(d.last_delivery.reason,
    "?health=1 must carry WHY the build fell back: " +
    JSON.stringify(d.last_delivery));
});

test("a real digest reads as a fresh edition and raises NO stale alert", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: { [artifactKey()]: artifact() } });
  assert.strictEqual(r.outcome.kind, w.KIND_DELIVERED);
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.edition, "fresh");
  const clock = fakeClock(T0);
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?health=1"), ntfyEnv(r.kv));
  } finally {
    clock.restore();
  }
  const d = JSON.parse(await res.text()).digest;
  assert.strictEqual(d.last_delivery.edition, "fresh");
  assert.strictEqual(d.last_delivery.fresh_edition, true);
  assert.strictEqual(d.last_delivery.fallback_shown, false);
  assert.strictEqual(staleAlerts(w, r.net).length, 0,
    "a successful build must not tell the owner his edition is stale");
  assert.ok(!Object.keys(r.kv.store).some(
    (k) => k.indexOf(w.STALE_EDITION_ALERT_KEY_PREFIX) === 0),
    "a fresh delivery must not write the stale-alert marker either");
});

test("a fallback also sends the recovery alert, naming the date and the reason", async () => {
  const w = loadWorker();
  const good = lastGood(YESTERDAY, "Yesterday's news, which did build.");
  const r = await deliver(w, {
    initial: {
      [artifactKey()]: artifact({ date: YESTERDAY }),
      "digest:ledger": JSON.stringify({
        schema_version: 1, as_of: new Date(T0 - HOUR).toISOString(),
        days: {}, last_good: good,
      }),
    },
  });
  const alerts = staleAlerts(w, r.net);
  assert.strictEqual(alerts.length, 1,
    "the fallback push went out and NOTHING said the edition is stale; the " +
    "owner has to notice the date in the title of a push he reads as news. " +
    "POSTs seen: " + JSON.stringify(
      postCalls(r.net).map((c) => c.headers["X-Sequence-Id"])));
  const payload = JSON.parse(alerts[0].body);
  assert.match(payload.title, /STALE/);
  assert.ok(payload.title.indexOf(YESTERDAY) >= 0,
    "the alert must name the edition actually shown: " + payload.title);
  assert.match(payload.message, /Why:/,
    "the alert must carry the reason the build failed: " + payload.message);
  assert.ok(payload.message.indexOf(good.body) < 0,
    "the alert must be short — it is a warning, not a second copy of the digest");
  assert.strictEqual(r.outcome.stale_alert, true);
});

test("the stale-edition alert is once per SLOT, and a probe cannot burn the marker", async () => {
  const w = loadWorker();
  const h = makeHarness({});
  const env = ntfyEnv(h.kv);
  const writer = async () => {};
  const clock = fakeClock(T0);
  let first, second, probe;
  try {
    first = await w.announceStaleEditionOnce(
      env, writer, TODAY, SLOT, YESTERDAY, "stale_date", true, T0);
    second = await w.announceStaleEditionOnce(
      env, writer, TODAY, SLOT, YESTERDAY, "stale_date", true, T0 + MIN);
    probe = await w.announceStaleEditionOnce(
      env, null, TODAY, SLOT, YESTERDAY, "stale_date", true, T0 + 2 * MIN);
  } finally {
    clock.restore();
  }
  const posts = postCalls(h.net);
  assert.strictEqual(first.alerted, true);
  assert.strictEqual(second.alerted, false,
    "a second re-fire of the SAME slot alerted again — the owner's phone is " +
    "the thing this marker exists to protect");
  assert.match(String(second.reason), /already announced/);
  assert.strictEqual(probe.alerted, false);
  assert.match(String(probe.reason), /probe/);
  assert.strictEqual(posts.length, 1,
    "exactly one alert must reach ntfy for one slot, and a probe must add none");
  assert.ok(h.kv.store[w.STALE_EDITION_ALERT_KEY_PREFIX + TODAY + ":" + SLOT],
    "the marker must be keyed by date AND slot, or a re-fire of another slot " +
    "would be silenced by this one");
});

test("a corrupt artifact takes the same ladder as an absent one", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: { [artifactKey()]: "{oops" } });
  assert.strictEqual(postCalls(r.net).length, 1);
  assert.strictEqual(r.outcome.kind, w.KIND_UNAVAILABLE);
  assert.match(ledgerOf(r.kv).days[TODAY][SLOT].reject_reason,
    /^corrupt \| (a late build was dispatched|NO build was dispatched)$/,
    "a corrupt artifact and an absent one take the same ladder, and both must "
    + "record the dispatch verdict: " +
    JSON.stringify(ledgerOf(r.kv).days[TODAY][SLOT].reject_reason));
});

// ==========================================================================
// 5b. THE HEADLINE REQUIREMENT: a failed prebuild degrades to LATE, never to
//     NOTHING. `dispatchDigest` had ZERO call sites for its whole life, so a
//     dead prebuild produced an apology and never a digest — and on 2026-10-11
//     even the apology failed to publish, so the owner received nothing at all.
// ==========================================================================

/** The POSTs the harness saw aimed at the GitHub workflow-dispatch API. */
function dispatchCalls(net) {
  return net.calls.filter(
    (c) => c.method === "POST" &&
      String(c.url).indexOf("/actions/workflows/daily-news.yml/dispatches") >= 0);
}

test("a failed prebuild DISPATCHES a late build, and the push still lands", async () => {
  const w = loadWorker();
  // NO artifact at all and NO last_good: the worst case, the one that used to
  // end in silence. GH_TOKEN is present, so the dispatch is a real capability.
  const r = await deliver(w, { initial: {}, env: { GH_TOKEN: "dispatch-token" } });
  assert.strictEqual(r.thrown, null, r.thrown && String(r.thrown));

  const dispatches = dispatchCalls(r.net);
  assert.strictEqual(dispatches.length, 1,
    "the prebuild left nothing sealed and the Worker has a GH_TOKEN, so it must "
    + "have dispatched exactly one late build. It dispatched " +
    dispatches.length + ". Calls: " +
    JSON.stringify(r.net.calls.map((c) => c.method + " " + c.url)));
  assert.strictEqual(dispatches[0].url, w.DIGEST_DISPATCH_URL);
  const body = JSON.parse(dispatches[0].body);
  assert.strictEqual(body.ref, "master");
  assert.strictEqual(body.inputs.digest_time, SLOT,
    "the late build was dispatched for the wrong slot, so it would build a "
    + "digest this slot cannot use");
  // `dry_run` must stay false: a late build that is dry delivers nothing, which
  // is the same silence this test exists to prevent.
  assert.strictEqual(body.inputs.dry_run, "false");
  assert.strictEqual(body.inputs.trigger_source, "worker");
  // THE SLOT'S DATE TRAVELS WITH THE DISPATCH. `TODAY` is the slot's Nepal
  // date, and it is what the receiving workflow must not have to infer: this
  // can run past midnight NPT, and a date-less dispatch would let the build
  // date itself off a clock that has already rolled over.
  assert.strictEqual(body.inputs.slot_date, TODAY,
    "the late build was dispatched without the " + TODAY + " " + SLOT + " slot "
    + "date (slot_date=" + body.inputs.slot_date + "), so it would date itself "
    + "off its own clock. inputs=" + JSON.stringify(body.inputs));

  // AND THE PUSH STILL LANDED. The dispatch is an ADDITION to the fallback, not
  // a replacement for it: the owner hears within seconds either way.
  const posts = postCalls(r.net);
  const ntfy = posts.filter((c) => String(c.url).indexOf(NTFY_SERVER) === 0);
  assert.strictEqual(ntfy.length, 1,
    "the ntfy push did not happen. Dispatching a build is not a reason to stop "
    + "telling the owner, and the push is what tells him a build is coming.");
  const payload = JSON.parse(ntfy[0].body);
  assert.strictEqual(ntfy[0].headers["X-Sequence-Id"],
    w.digestFullSequenceId(TODAY, SLOT),
    "the slot's sequence identity was lost, so the prebuild that this dispatch "
    + "provokes could not dedupe against this push");

  // The message must be honest in BOTH directions, and it is built only from
  // the boolean dispatchDigest actually returned.
  assert.ok(/A fresh build is on its way/.test(payload.message),
    "a dispatch was accepted and the push does not say so: " + payload.message);
  assert.ok(payload.message.indexOf(w.digestArchiveIndexUrl(ARCHIVE_ENV)) >= 0,
    "the dispatch note displaced the archive index link: " + payload.message);

  // The ledger must record the verdict for the watchdog and the next reader.
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.late_build_dispatched, true,
    "the ledger does not record that a late build was dispatched, so nothing "
    + "downstream can tell 'building' from 'not building'");
  assert.strictEqual(r.outcome.late_build_dispatched, true);
  assert.strictEqual(entry.state, w.STATE_SENT,
    "a dispatched fallback push did not reach the owner, so this test would be "
    + "green about a delivery that never happened");
});

test("dispatchDigest is REACHABLE, not merely retained", async () => {
  // The defect was a definition with zero call sites. A grep for the name would
  // have been satisfied by the dead code, so this DRIVES the delivery path with
  // an empty artifact and asserts the HTTP request leaves.
  const w = loadWorker();
  const r = await deliver(w, { initial: {}, env: { GH_TOKEN: "dispatch-token" } });
  assert.strictEqual(dispatchCalls(r.net).length, 1,
    "an empty artifact did not produce a dispatch. dispatchDigest is defined at "
    + "deploy/cloudflare-worker.js and now has a call site on the fallback "
    + "path; if this fails, it is dead code again.");
});

test("a slot that already pushed on the topic dispatches NOTHING", async () => {
  // The dispatch sits AFTER the sequence_id read-back on purpose: if this slot
  // is already on the topic it is handled, and waking a 20-minute build for it
  // would spend runner minutes to build a digest nobody would be pushed again.
  const w = loadWorker();
  const seq = w.digestFullSequenceId(TODAY, SLOT);
  const r = await deliver(w, {
    initial: {},
    env: { GH_TOKEN: "dispatch-token" },
    cache: [{ id: "aaa", time: (T0 - 60 * 1000) / 1000, sequence_id: seq }],
  });
  assert.strictEqual(dispatchCalls(r.net).length, 0,
    "a slot whose fallback is already on the topic dispatched a build anyway");
  assert.strictEqual(r.outcome.deduped, true);
});

test("the ladder reports HONESTLY when the dispatch itself fails", async () => {
  const w = loadWorker();
  // POST #1 on this path is the dispatch (it happens before the publish), so
  // answering the first POST with 403 fails exactly that call and nothing else.
  const r = await deliver(w, {
    initial: {},
    env: { GH_TOKEN: "dispatch-token" },
    onPost: (n) => (n === 1
      ? { ok: false, status: 403, text: async () => "Resource not accessible" }
      : null),
  });
  assert.strictEqual(dispatchCalls(r.net).length, 1,
    "the dispatch was never attempted, so this test is not exercising the "
    + "failure branch at all");
  const payload = JSON.parse(
    postCalls(r.net).filter((c) => String(c.url).indexOf(NTFY_SERVER) === 0)[0].body);

  // THE HONESTY REQUIREMENT. A refused dispatch must never leave the owner
  // believing a digest is on its way: he would wait for one that is not coming,
  // and the next thing he learns about it is the following morning.
  assert.ok(!/A fresh build is on its way/.test(payload.message),
    "the push claims a build is coming after the dispatch was REFUSED: " +
    payload.message);
  assert.ok(/No fresh build was started/.test(payload.message),
    "a refused dispatch is not disclosed to the owner: " + payload.message);

  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.late_build_dispatched, false,
    "the ledger claims a build was dispatched when the API refused it");
  assert.match(entry.reject_reason, /\| NO build was dispatched$/,
    "the reject_reason must not imply a digest is coming: " +
    JSON.stringify(entry.reject_reason));
  // And the push still happened: the apology is better than silence.
  assert.strictEqual(entry.state, w.STATE_SENT);
});

test("a GOOD digest dispatches nothing, even with a GH_TOKEN in scope", async () => {
  // The complement of the headline test, and the one that stops this fix
  // becoming a double arm: a slot whose artifact sealed must not ALSO wake a
  // build, or every healthy slot would cost a second build.
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    env: { GH_TOKEN: "dispatch-token" },
  });
  assert.strictEqual(dispatchCalls(r.net).length, 0,
    "a sealed artifact STILL dispatched a build; the dispatch belongs on the "
    + "missing-artifact path only");
  assert.strictEqual(postCalls(r.net).length, 1);
  assert.strictEqual(r.outcome.kind, w.KIND_DELIVERED);
});

test("a late build that seals later produces NO second push for the slot", async () => {
  // The whole point of dispatching is that the digest arrives LATE, and the
  // delivery guard is at-least-once with a possible duplicate on the recovery
  // path. This pins the half of that which is actually controllable: the
  // fallback push already happened, so the late build's own delivery must be
  // suppressed rather than pushing the owner a second digest for one slot.
  const w = loadWorker();
  const first = await deliver(w, { initial: {}, env: { GH_TOKEN: "dispatch-token" } });
  assert.strictEqual(first.outcome.pushed, true);
  const seq = w.digestFullSequenceId(TODAY, SLOT);

  // The dispatched build finished and the seal job stored the artifact. The
  // ledger carries the first push, which is what must suppress this one.
  const kv = first.kv;
  const second = await deliver(w, {
    initial: {
      [artifactKey()]: artifact(),
      "digest:ledger": kv.store["digest:ledger"],
    },
    env: { GH_TOKEN: "dispatch-token" },
    cache: [{ id: "bbb", time: (T0 - 30 * 1000) / 1000, sequence_id: seq }],
  });
  assert.strictEqual(second.outcome.kind, w.KIND_DUPLICATE_SUPPRESSED,
    "the prebuild's late seal produced a SECOND push for a slot the owner was "
    + "already told about: " + JSON.stringify(second.outcome));
  assert.strictEqual(postCalls(second.net).length, 0,
    "a duplicate reached the topic for one slot");
  assert.strictEqual(dispatchCalls(second.net).length, 0);
});

// ==========================================================================
// 6. A KV failure must not be silent, and must not reject out of the cron
// ==========================================================================

test("a ledger READ failure pushes, writes nothing, and does not throw", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    failGet: ["digest:ledger"],
  });
  assert.strictEqual(r.thrown, null,
    "deliverDigest REJECTED when the ledger could not be read. A rejected " +
    "waitUntil is a cron invocation with no send and no log line the owner " +
    "will ever see.");
  assert.strictEqual(postCalls(r.net).length, 1,
    "a KV read failure produced NO push at all; the push is the fail-safe");
  assert.ok(/unavailable/.test(JSON.parse(postCalls(r.net)[0].body).title),
    "the push must be the unavailable one");
  assert.strictEqual(r.kv.puts.length, 0,
    "the ledger was written after a FAILED READ of it. A ledger rebuilt from a " +
    "read that never happened would erase every punctuality record behind it. " +
    "Wrote: " + JSON.stringify(r.kv.puts.map((p) => p.key)));
  assert.ok(/could not be READ/.test(r.logs.text()), r.logs.text());
});

test("an artifact READ failure pushes and writes nothing either", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: {},
    failGet: [artifactKey()],
  });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 1);
  assert.strictEqual(r.kv.puts.length, 0);
});

// ==========================================================================
// 7. Punctuality is measured from two recorded instants, never from `now()`
// ==========================================================================

test("the ledger records first_ntfy_sent_at and scheduled_instant, not a write-time delta", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: { [artifactKey()]: artifact() } });
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.scheduled_instant, "2026-10-09T01:35:00Z",
    "scheduled_instant must come from the CRON constant on the Nepal date, " +
    "the same arithmetic as _scheduled_instant in scripts/delivery_ledger.py");
  assert.strictEqual(entry.first_ntfy_sent_at, "2026-10-09T01:36:00Z");
  assert.strictEqual(r.outcome.delta_s, 60);
  assert.strictEqual(w.DELIVERY_ON_TIME_S, 60);
  // A push 20 minutes after this clock is late, and is recorded as late. T0 is
  // already 60s past the slot, so the delta is 60 + 1200 — which is the point:
  // the delta comes from the two recorded instants, so it carries the slot's own
  // lateness forward instead of being measured from somewhere else.
  const late = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    now: T0 + 20 * MIN,
  });
  assert.strictEqual(late.outcome.delta_s, 60 + 20 * 60);
  const lateEntry = ledgerOf(late.kv).days[TODAY][SLOT];
  assert.strictEqual(lateEntry.first_ntfy_sent_at, "2026-10-09T01:56:00Z");
  assert.strictEqual(late.outcome.delta_s > w.DELIVERY_ON_TIME_S, true);
});

test("the ledger is pruned to the retention window scripts/delivery_ledger.py keeps", async () => {
  const w = loadWorker();
  assert.strictEqual(w.DELIVERY_RETENTION_DAYS, 7);
  const old = {};
  for (let back = 1; back <= 12; back += 1) {
    const d = new Date(Date.parse(TODAY + "T00:00:00Z") - back * 24 * HOUR)
      .toISOString().slice(0, 10);
    old[d] = { morning: { state: "sent", ntfy_sent_at: d + "T01:36:00Z" } };
  }
  const ledger = { schema_version: 1, as_of: new Date(T0).toISOString(), days: old };
  // No entry for today: a ledger that already recorded today's send would
  // suppress the push, and this test is about what the WRITE leaves behind.
  assert.strictEqual(ledger.days[TODAY], undefined);
  const r = await deliver(w, {
    initial: { "digest:ledger": JSON.stringify(ledger), [artifactKey()]: artifact() },
  });
  assert.strictEqual(r.outcome.pushed, true, "sanity: the prune test needs a real send");
  const days = Object.keys(ledgerOf(r.kv).days).sort();
  assert.strictEqual(days.length, 7,
    "the ledger kept " + days.length + " days; the page renders the last 7 and " +
    "would disagree with a longer one: " + JSON.stringify(days));
  assert.deepStrictEqual(days, [
    "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06",
    "2026-10-07", "2026-10-08", "2026-10-09",
  ], "the kept window must be the 7 days ENDING today, matching " +
    "prune() in scripts/delivery_ledger.py");
  assert.strictEqual(ledgerOf(r.kv).pruned_before, "2026-10-03");
});

// ==========================================================================
// 8. ?deliver_now=1 — the whole path, and no ledger write
// ==========================================================================

test("?deliver_now=1 exercises the whole path and CANNOT write the ledger", async () => {
  const w = loadWorker();
  const harness = makeHarness({
    initial: { [artifactKey()]: artifact() },
  });
  const logs = captureLogs();
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?deliver_now=1&slot=morning", {
        headers: { "X-Ingest-Token": INGEST_SECRET },
      }),
      ntfyEnv(harness.kv));
  } finally {
    logs.restore();
  }
  assert.strictEqual(res.status, 200);
  const body = JSON.parse(await res.text());
  assert.strictEqual(body.ledger_written, false);
  assert.strictEqual(body.outcome.pushed, true);
  assert.strictEqual(postCalls(harness.net).length, 1,
    "the probe must really push, or it is not exercising the path");
  assert.strictEqual(putCount(harness.kv, "digest:ledger"), 0,
    "?deliver_now=1 WROTE the delivery ledger. One manual probe would then " +
    "refresh the duplicate guard and permanently mask a real double-push — " +
    "the same defect class as the heartbeat that ?run=1 must never refresh, " +
    "and the reason the ledger write is injected by the cron arm instead of " +
    "being reachable from a flag.");
  assert.strictEqual(harness.kv.store["digest:ledger"], undefined);
  // STRONGER THAN "did not write the ledger", and deliberately so: the probe
  // must not write ANY key. A new expiry delete or a new bookkeeping key added
  // to the delivery path later would refresh state the duplicate guard rests
  // on, and a test that only asks about `digest:ledger` would not notice.
  assert.deepStrictEqual(harness.kv.puts, [],
    "?deliver_now=1 wrote to KV: " + JSON.stringify(harness.kv.puts.map(
      (p) => p.key)));
  assert.ok(harness.kv.gets.indexOf("digest:ledger") >= 0,
    "the probe must still READ the ledger — that read is part of the path it " +
    "exists to exercise: " + JSON.stringify(harness.kv.gets));
  assert.ok(harness.kv.gets.some((k) => k.indexOf("digest:artifact:") === 0),
    "the probe did not read an artifact, so it is not exercising validation: " +
    JSON.stringify(harness.kv.gets));
});

test("the KV budget deploy/schedule.json records is what the arm really spends", async () => {
  // A budget that is a comment starts lying the first time the code changes.
  // So this measures ONE real delivery with the harness's own counters and
  // requires the committed table to agree, per day, per key.
  const w = loadWorker();
  const schedule = JSON.parse(
    fs.readFileSync(path.join(REPO_ROOT, "deploy", "schedule.json"), "utf8"));
  const table = schedule.heartbeat.kv_writes_per_day;
  const digest = table.digest;
  assert.ok(digest, "deploy/schedule.json's heartbeat.kv_writes_per_day has " +
    "no digest block, so the delivery arm's KV cost is unbudgeted");

  const r = await deliver(w, { initial: { [artifactKey()]: artifact() } });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 1);
  const perDelivery = putCount(r.kv, "digest:ledger");
  assert.ok(perDelivery >= 2,
    "one delivery wrote the ledger " + perDelivery + " time(s). The " +
    "write-ahead rule is `pending` on disk BEFORE the POST, so at least two " +
    "writes are structural; if this ever drops, the ordering guarantee is " +
    "gone and the recorded budget is describing something else.");

  const slotsPerDay = [w.CRON_DIGEST_MORNING, w.CRON_DIGEST_EVENING].length;
  assert.strictEqual(digest.ledger_writes, perDelivery * slotsPerDay,
    "deploy/schedule.json budgets " + digest.ledger_writes + " ledger " +
    "writes/day, but the arm performs " + perDelivery + " per delivered slot " +
    "across " + slotsPerDay + " slots. Change the table or the code, not this " +
    "assertion.");
  assert.strictEqual(digest.artifact_writes, slotsPerDay,
    "one artifact is sealed per slot (digest-prebuild.yml's seal job), so " +
    "the per-day figure is the slot count");
  assert.strictEqual(digest.artifact_expiry_deletes, slotsPerDay,
    "each sealed artifact carries the expiry TTL, and Cloudflare charges one " +
    "delete per TTL-expired key, so this is the same slot count again");
  const summed = digest.artifact_writes + digest.ledger_writes +
    digest.artifact_expiry_deletes;
  assert.strictEqual(digest.total_steady, summed,
    "total_steady is " + digest.total_steady + " but its own terms sum to " +
    summed);
  assert.strictEqual(table.system_total_max,
    table.breaking + table.academic + table.alert_stamp_max + digest.total_steady,
    "system_total_max (" + table.system_total_max + ") does not follow from " +
    "the arms it is supposed to cover: " + JSON.stringify(table));
  console.log("digest KV budget measured: " + perDelivery + " ledger writes x " +
    slotsPerDay + " slots + " + digest.artifact_writes + " artifacts + " +
    digest.artifact_expiry_deletes + " expiry deletes = " +
    digest.total_steady + "/day (system_total_max " + table.system_total_max +
    ")");
});

test("?deliver_now=1 takes the fallback ladder without recording it", async () => {
  const w = loadWorker();
  const harness = makeHarness({ initial: { [artifactKey()]: artifact({ date: YESTERDAY }) } });
  const res = await w.WORKER.fetch(
    new Request("https://probe.invalid/?deliver_now=1", {
      headers: { "X-Ingest-Token": INGEST_SECRET },
    }),
    ntfyEnv(harness.kv));
  assert.strictEqual(res.status, 200);
  const body = JSON.parse(await res.text());
  assert.strictEqual(body.outcome.kind, w.KIND_UNAVAILABLE);
  assert.strictEqual(postCalls(harness.net).length, 1);
  assert.strictEqual(putCount(harness.kv, "digest:ledger"), 0);
});

test("?deliver_now=1 is refused without the ingest token", async () => {
  const w = loadWorker();
  const harness = makeHarness({ initial: { [artifactKey()]: artifact() } });
  for (const headers of [{}, { "X-Ingest-Token": "wrong" }]) {
    const res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?deliver_now=1", { headers: headers }),
      ntfyEnv(harness.kv));
    assert.strictEqual(res.status, 401,
      "an unauthenticated caller reached the delivery path with " +
      JSON.stringify(headers));
  }
  assert.strictEqual(postCalls(harness.net).length, 0);
});

test("?deliver_now=1 is refused when the ingest secret is not configured", async () => {
  const w = loadWorker();
  const harness = makeHarness({ initial: { [artifactKey()]: artifact() } });
  const logs = captureLogs();
  let res;
  try {
    // Same env WITHOUT the secret. Comparing an absent secret against "" would
    // accept a request that offers no credential at all, because both sides
    // would be the empty string — so the refusal has to be an explicit check.
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?deliver_now=1"),
      { KV: harness.kv, NTFY_SERVER: NTFY_SERVER, NTFY_TOPIC: TOPIC });
  } finally {
    logs.restore();
  }
  assert.strictEqual(res.status, 401,
    "the delivery probe answered an UNAUTHENTICATED caller on a Worker whose " +
    "ingest secret is not configured");
  assert.strictEqual(postCalls(harness.net).length, 0);
  assert.ok(/DIGEST_INGEST_MISCONFIGURED/.test(logs.text()), logs.text());
});

// ==========================================================================
// 8b. ?deliver_test=1 — ONE labelled, low-priority push on the REAL publish
//     function, and no ledger touch at all
// ==========================================================================

const DELIVER_TEST_TITLE = "Worker delivery test - ignore";

test("?deliver_test=1 pushes ONE labelled low-priority message on the real publish function", async () => {
  // WHY THIS ROUTE EXISTS AT ALL. The delivery path had never once produced an
  // observable success, and every way of proving it alive — ?deliver_now=1, a
  // curl, a local script — either sends the real (alarming) payload or proves
  // nothing about the Worker's own code. This pushes exactly one plainly
  // labelled, low-priority message through publishToNtfy, the function
  // deliverDigest itself calls, and reports the ntfy status the Worker recorded.
  const w = loadWorker();
  const harness = makeHarness({});
  const res = await w.WORKER.fetch(
    new Request("https://probe.invalid/?deliver_test=1", {
      headers: { "X-Ingest-Token": INGEST_SECRET },
    }),
    ntfyEnv(harness.kv));
  assert.strictEqual(res.status, 200);
  const body = JSON.parse(await res.text());

  const posts = postCalls(harness.net);
  assert.strictEqual(posts.length, 1,
    "?deliver_test=1 made " + posts.length + " POST(s). This route exists to " +
    "prove the path ONCE; it must never be a way to send several.");

  // The Worker's OWN record of ntfy's answer, not an assumption. If the route
  // stopped surfacing the status, this test would still be green and the
  // operator would have nothing to read.
  assert.strictEqual(body.probe, true);
  assert.strictEqual(body.ntfy_http_status, 200,
    "the route must report the HTTP status the Worker's own publish call " +
    "recorded, got " + JSON.stringify(body.ntfy_http_status));
  assert.strictEqual(body.ok, true, JSON.stringify(body));

  const sent = JSON.parse(posts[0].body);
  assert.strictEqual(sent.title, DELIVER_TEST_TITLE);
  assert.strictEqual(sent.topic, TOPIC);
  assert.ok(typeof sent.priority === "number" && sent.priority >= 1 &&
              sent.priority <= 3,
    "a test push must be quiet. priority is " + sent.priority +
    " and must be 1-3; 4 or 5 would alert on the owner's phone.");
  assert.ok(/test/i.test(sent.message) && /no digest/i.test(sent.message),
    "the message must say on its face that it is a test and that no digest is " +
    "affected, so an alert that lands cannot be mistaken for a real send: " +
    sent.message);
  // Same target and same headers as a real push, or it proves nothing about
  // them. These are asserted on the wire, not on the route's own return value.
  assert.strictEqual(posts[0].url, NTFY_SERVER + "/");
  assert.strictEqual(posts[0].headers["X-Sequence-Id"], body.sequence_id);
  assert.ok(/^dmn-delivery-test-/.test(body.sequence_id),
    "the test sequence_id must be unable to collide with a real slot's, which " +
    "is `dmn-<date>-<slot>`, got " + body.sequence_id);
});

test("?deliver_test=1 CANNOT write or even read the delivery ledger", async () => {
  // STRONGER THAN "did not write digest:ledger", and the same standard the
  // ?deliver_now=1 test sets: any key at all. A probe that could refresh the
  // duplicate guard would permanently mask a real double-push.
  const w = loadWorker();
  const harness = makeHarness({});
  const res = await w.WORKER.fetch(
    new Request("https://probe.invalid/?deliver_test=1", {
      headers: { "X-Ingest-Token": INGEST_SECRET },
    }),
    ntfyEnv(harness.kv));
  assert.strictEqual(res.status, 200);
  const body = JSON.parse(await res.text());
  assert.strictEqual(body.ledger_written, false);
  assert.deepStrictEqual(harness.kv.puts, [],
    "?deliver_test=1 wrote to KV: " + JSON.stringify(
      harness.kv.puts.map((p) => p.key)));
  assert.strictEqual(harness.kv.gets.indexOf("digest:ledger"), -1,
    "?deliver_test=1 READ the delivery ledger. It has no reason to: this route " +
    "does not deliver a slot, so reading the guard's own state is pure risk.");
});

test("?deliver_test=1 is refused without the ingest token", async () => {
  // An unauthenticated push endpoint is an open relay onto the owner's phone.
  const w = loadWorker();
  const harness = makeHarness({});
  for (const headers of [{}, { "X-Ingest-Token": "wrong" }]) {
    const res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?deliver_test=1", { headers: headers }),
      ntfyEnv(harness.kv));
    assert.strictEqual(res.status, 401,
      "an unauthenticated caller pushed to the owner's phone with " +
      JSON.stringify(headers));
  }
  assert.strictEqual(postCalls(harness.net).length, 0);
});

test("?deliver_test=1 is refused when the ingest secret is not configured", async () => {
  const w = loadWorker();
  const harness = makeHarness({});
  const logs = captureLogs();
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?deliver_test=1"),
      { KV: harness.kv, NTFY_SERVER: NTFY_SERVER, NTFY_TOPIC: TOPIC });
  } finally {
    logs.restore();
  }
  assert.strictEqual(res.status, 401);
  assert.strictEqual(postCalls(harness.net).length, 0);
  assert.ok(/DIGEST_INGEST_MISCONFIGURED/.test(logs.text()), logs.text());
});

test("?deliver_test=1 surfaces ntfy's rejection: status AND body, and never the topic", async () => {
  // The reason this route exists at all: a bare "HTTP 403" cannot be
  // diagnosed. ntfy's own error body is what says WHICH rejection it was, and
  // the topic must be scrubbed out of it before it is ever returned or logged,
  // because a ntfy error body quotes the topic it was asked about.
  const w = loadWorker();
  const harness = makeHarness({
    onPost: () => ({
      ok: false, status: 403,
      text: async () => JSON.stringify({
        code: 403, error: "forbidden", link: "https://ntfy.sh/docs",
        topic: TOPIC,
      }),
    }),
  });
  const logs = captureLogs();
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?deliver_test=1", {
        headers: { "X-Ingest-Token": INGEST_SECRET },
      }),
      ntfyEnv(harness.kv));
  } finally {
    logs.restore();
  }
  const body = JSON.parse(await res.text());
  assert.strictEqual(body.ok, false);
  assert.strictEqual(body.ntfy_http_status, 403,
    "the Worker's OWN recorded status must reach the operator, got " +
    JSON.stringify(body.ntfy_http_status));
  assert.ok(body.ntfy_error_body && /forbidden/.test(body.ntfy_error_body),
    "ntfy's own error body must be surfaced, or a 403 cannot be told apart " +
    "from a 404 by the person debugging it: " + JSON.stringify(body));
  assert.strictEqual(body.ntfy_error_body.indexOf(TOPIC), -1,
    "the topic VALUE leaked into the returned error body");
  assert.strictEqual(logs.text().indexOf(TOPIC), -1,
    "the topic VALUE leaked into a log line");
});

test("?deliver_test=1 is structurally unable to reach the ledger writer", async () => {
  // The runtime assertions above cannot see a future edit that re-plumbs the
  // writer in; this is the structural proof, mirroring the test above.
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  const at = src.indexOf("async function handleDeliverTest");
  assert.ok(at > 0, "?deliver_test=1 has no handler in " + WORKER_PATH);
  const handler = src.slice(at, src.indexOf("\nasync function ", at + 10));
  assert.ok(!/commitDigestLedger/.test(handler) && !/deliverDigestPlan/.test(handler),
    "?deliver_test=1 must publish on its own and must not go through the " +
    "delivery plan, or one operator probe would take the real path's guards " +
    "and ledger with it");
  assert.ok(/publishToNtfy\(/.test(handler),
    "?deliver_test=1 must call publishToNtfy, the SAME function deliverDigest " +
    "uses. A second, near-identical publish call would prove nothing about the " +
    "real one.");
});

test("the ledger writer is reachable only from the cron arm, not from fetch", async () => {
  // The runtime proof is the previous test; this one is the STRUCTURAL proof,
  // because a runtime assertion cannot see a future edit that re-plumbs the
  // writer into a fetch path.
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  const armAt = src.indexOf("async function deliverDigest(env, slot");
  const cronArm = src.slice(armAt);
  const beforeCronArm = src.slice(0, armAt);
  const fetchRegion = src.slice(src.indexOf("async fetch(request, env)"));
  assert.ok(armAt > 0, "could not find the cron arm in " + WORKER_PATH);
  assert.ok(/commitDigestLedger/.test(cronArm),
    "the cron arm must pass the real ledger writer");
  const mentionsBefore = beforeCronArm.split("commitDigestLedger").length - 1;
  assert.strictEqual(mentionsBefore, 1,
    "the ledger writer is named " + mentionsBefore + " times before the cron " +
    "arm; once is its definition, so every further mention is a second code " +
    "path that can write the delivery ledger");
  assert.ok(!/commitDigestLedger/.test(fetchRegion),
    "the fetch entry point references the ledger writer. A fetch path that " +
    "can write digest:ledger can refresh the duplicate guard and mask a real " +
    "double-push — see the heartbeat placement comment in scheduled().");
  assert.ok(/handleDeliverNow/.test(fetchRegion));
  const probeAt = src.indexOf("async function handleDeliverNow");
  const probe = src.slice(probeAt, probeAt + 2000).replace(/\s+/g, " ");
  assert.ok(/deliverDigestPlan\(env, slot, null\)/.test(probe),
    "?deliver_now=1 must call deliverDigestPlan with a null ledger writer; " +
    "the call reads: " + /deliverDigestPlan\([^;]*\)/.exec(probe));
});

// ==========================================================================
// 9. The topic must never reach a log, an error or a ledger field
// ==========================================================================

test("the topic value never appears in any log line, on ANY delivery path", async () => {
  const w = loadWorker();
  const paths = [
    { name: "delivered", o: { initial: { [artifactKey()]: artifact() } } },
    { name: "fallback", o: { initial: { [artifactKey()]: artifact({ date: YESTERDAY }) } } },
    { name: "unavailable", o: { initial: {} } },
    // The read-back URL contains the topic, and a transport error stringifies
    // the URL it was handed. That is the leak this assertion exists for.
    { name: "read-back throws", o: { initial: { [artifactKey()]: artifact() }, failReadback: true } },
    { name: "read-back non-200", o: { initial: { [artifactKey()]: artifact() }, readbackStatus: 502 } },
    { name: "ledger read throws", o: { failGet: ["digest:ledger"] } },
    { name: "post throws", o: { initial: { [artifactKey()]: artifact() }, failPost: [1, 2, 3], failPostThrows: true } },
    { name: "hit by sequence_id", o: { initial: { [artifactKey()]: artifact() },
      cache: [{ id: "x", time: 1791530400, sequence_id: "dmn-" + TODAY + "-" + SLOT }] } },
  ];
  for (const p of paths) {
    const r = await deliver(w, p.o);
    assert.strictEqual(r.thrown, null, p.name + " threw");
    assert.ok(r.logs.text().indexOf(TOPIC) < 0,
      "the topic VALUE reached a log line on the '" + p.name + "' path. An " +
      "ntfy topic is a publish/subscribe capability: anyone holding it can " +
      "read the owner's alert stream and publish into it.\n" + r.logs.text());
    const ledgerRaw = r.kv.store["digest:ledger"];
    if (ledgerRaw) {
      assert.ok(ledgerRaw.indexOf(TOPIC) < 0,
        "the topic VALUE reached the ledger on the '" + p.name + "' path: " +
        ledgerRaw);
    }
    // Every log line must still say something. A guard that silences the topic
    // by silencing the whole log would satisfy the assertion above.
    assert.ok(r.logs.lines.length > 0, p.name + " logged nothing at all");
  }
});

test("a missing NTFY_TOPIC is reported by NAME, and the value never appears", async () => {
  const w = loadWorker();
  const harness = makeHarness({ initial: { [artifactKey()]: artifact() } });
  const logs = captureLogs();
  try {
    const outcome = await w.deliverDigest(
      { KV: harness.kv, NTFY_SERVER: NTFY_SERVER, WORKER_BUILD: "t",
        DIGEST_ARCHIVE_ORIGIN: ARCHIVE_ENV.DIGEST_ARCHIVE_ORIGIN }, SLOT,
      { now: () => T0, sleep: async () => {} });
    assert.strictEqual(outcome.ok, false);
  } finally {
    logs.restore();
  }
  assert.ok(/NTFY_TOPIC/.test(logs.text()),
    "an unset NTFY_TOPIC must be named loudly — a silent dead-man's switch is " +
    "worse than none: " + logs.text());
  assert.strictEqual(postCalls(harness.net).length, 0);
});

// ==========================================================================
// 10. ntfy and NOTHING else
// ==========================================================================

test("the delivery path reaches ntfy and nothing else, with no GH_TOKEN in scope", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: { [artifactKey()]: artifact() } });
  assert.ok(r.net.calls.length >= 2,
    "expected a read-back GET and a publish POST, got " + r.net.calls.length);
  for (const call of r.net.calls) {
    assert.ok(call.url.indexOf(NTFY_SERVER) === 0,
      "the delivery path made a request outside ntfy: " + call.url +
      "\nThe delivery arm renders nothing, calls no AI and needs no token; a " +
      "request to anything else here is either a design change or a defect.");
  }
  assert.strictEqual(w.DISPATCH_URL.indexOf("api.github.com") > 0, true,
    "sanity: the digest cron dispatches to the GitHub API, which is exactly " +
    "what the delivery path must NOT do");
});

// ==========================================================================
// 11. No model opinion may fake or suppress a send
// ==========================================================================

test("a verdict inside the artifact cannot fake or suppress a delivery", async () => {
  const w = loadWorker();
  const lying = artifact({
    verdict: { delivery: "delivered", ntfy_sent_at: "2026-10-09T01:35:00Z", ok: true },
    delivered: true,
    ntfy_sent_at: "2026-10-09T01:35:00Z",
    ledger: { [TODAY]: { [SLOT]: { state: "sent", kind: "delivered" } } },
  });
  const r = await deliver(w, { initial: { [artifactKey()]: lying } });
  assert.strictEqual(postCalls(r.net).length, 1,
    "an artifact carrying its own `verdict: delivered` SUPPRESSED the push. " +
    "Delivery bookkeeping has to be deterministic: a model verdict that can " +
    "say 'sent' can suppress a send, and one that can say 'failed' can fake " +
    "the appearance of a delivery.");
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, w.STATE_SENT);
  assert.strictEqual(entry.first_ntfy_sent_at, "2026-10-09T01:36:00Z",
    "the recorded instant came from the artifact's own claim instead of the " +
    "push that actually happened");
});

test("a ledger entry that CLAIMS sent without ntfy_sent_at does not suppress anything", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: {
      [artifactKey()]: artifact(),
      "digest:ledger": JSON.stringify({
        schema_version: 1, as_of: new Date(T0).toISOString(),
        days: { [TODAY]: { [SLOT]: { state: "sent", kind: "delivered", verdict: "delivered" } } },
      }),
    },
  });
  assert.strictEqual(postCalls(r.net).length, 1,
    "an entry with state:'sent' and no ntfy_sent_at suppressed the push. The " +
    "guard keys on a RECORDED INSTANT, never on a word describing one.");
});

// ==========================================================================
// 11b. A row that cannot be this slot's push must never withhold it.
//
// MEASURED, 2026-10-28. A PROOF run was dispatched against slot_date=2026-10-29
// so it could not touch the day it ran on. It wrote:
//
//     state: sent   ntfy_sent_at: 2026-10-28T14:44:00Z   on_time: false
//
// 2026-10-29's morning slot is due 01:35Z. The recorded instant is TEN HOURS
// FIFTY-ONE MINUTES EARLY, which is not a late digest but proof the push never
// served that slot. With the old guard the row was still a `sent` row, so at
// 07:20 on the 29th the ladder would have found "already sent" and delivered
// NOTHING: an outage created by the test that was meant to prove reliability.
//
// Two independent defences, because the row above carries NEITHER the
// `simulated` flag (it predates it) NOR a trustworthy `scheduled_instant`:
//   * the writer now stamps `simulated: true` and the slot's own
//     `scheduled_instant` on any proof push, and a simulated row never counts;
//   * the reader compares the recorded push instant with the SLOT'S due instant
//     (recorded, else the schedule table), so the already-written row is inert
//     without rewriting production KV.
// ==========================================================================

test("a SIMULATED ledger row does not suppress the slot's real push", async () => {
  const w = loadWorker();
  const r = await deliver(w, { initial: {
    [artifactKey()]: artifact(),
    "digest:ledger": ledgerWith({
      state: "sent",
      kind: "delivered",
      simulated: true,
      sequence_id: "dmn-" + TODAY + "-" + SLOT,
      ntfy_sent_at: new Date(T0 - 30 * 1000).toISOString(),
      scheduled_instant: "2026-10-09T01:35:00Z",
    }),
  } });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 1,
    "a row flagged `simulated: true` suppressed the real delivery. A proof run " +
    "is evidence about the CODE, never about the slot: suppressing on it is a " +
    "missed digest caused by the test that was supposed to prove reliability.");
  assert.notStrictEqual(r.outcome.kind, w.KIND_DUPLICATE_SUPPRESSED);
  assert.ok(/simulated/.test(r.logs.text()),
    "the referee must SAY why the row was not trusted: " + r.logs.text());
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.state, w.STATE_SENT);
  assert.strictEqual(entry.simulated, undefined,
    "the real delivery must CLEAR the simulated flag, or the next fire reads " +
    "an honest slot as a proof run and pushes a second copy");
});

test("a row whose push instant precedes the slot by more than the slack does not suppress", async () => {
  const w = loadWorker();
  // Byte for byte the shape the 2026-10-29 proof row has: a sent instant, no
  // `simulated` flag, and no `scheduled_instant` to contradict it. Only the
  // schedule table can tell that this cannot be the slot's own push.
  const r = await deliver(w, { initial: {
    [artifactKey()]: artifact(),
    "digest:ledger": ledgerWith({
      state: "sent",
      kind: "delivered",
      sequence_id: "dmn-" + TODAY + "-" + SLOT,
      ntfy_sent_at: "2026-10-08T14:44:00Z",
    }),
  } });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 1,
    "a push instant recorded 10h51m before the slot's due instant suppressed " +
    "the slot. The row cannot be this slot's delivery, so withholding on it " +
    "turns tomorrow's 07:20 into a silent missed day.");
  assert.ok(/cannot belong to it/.test(r.logs.text()),
    "the unreconciled row must be reported, not silently ignored: " +
    r.logs.text());
});

test("slotWasServed is the one predicate, and it fails in every direction that matters", () => {
  const w = loadWorker();
  const due = "2026-10-09T01:35:00Z";
  const good = { ntfy_sent_at: new Date(T0).toISOString(), scheduled_instant: due };
  assert.strictEqual(w.slotWasServed(good, TODAY, SLOT), true,
    "an on-time push with its own scheduled instant is a delivery");
  assert.strictEqual(
    w.slotWasServed(Object.assign({}, good, { simulated: true }), TODAY, SLOT),
    false, "`simulated: true` is never a delivery");
  assert.strictEqual(
    w.slotWasServed(Object.assign({}, good, { dry_run: true }), TODAY, SLOT),
    false, "`dry_run: true` is never a delivery either");
  assert.strictEqual(
    w.slotWasServed({ ntfy_sent_at: "2026-10-08T14:44:00Z" }, TODAY, SLOT),
    false, "with no recorded scheduled instant the DUE TABLE decides, and an " +
    "instant 10h51m early fails it");
  assert.strictEqual(
    w.slotWasServed({ ntfy_sent_at: "2026-10-09T01:34:30Z" }, TODAY, SLOT),
    true, "30s before the due minute is inside the slack a cron firing early " +
    "needs, so it must still suppress");
  assert.strictEqual(
    w.slotWasServed({ ntfy_sent_at: "2026-10-09T01:25:05Z" }, TODAY, SLOT),
    true, "9m55s early is the measured REAL delivery (2026-10-13T13:25:05Z for " +
    "its 13:35Z slot), and calling it a mislabel pushes a second copy");
  assert.strictEqual(
    w.slotWasServed({ ntfy_sent_at: "2026-10-09T01:04:00Z" }, TODAY, SLOT),
    false, "31 minutes early is outside the slack, so it is a drift");
  assert.strictEqual(
    w.slotWasServed({ scheduled_instant: due }, TODAY, SLOT),
    false, "a row with no push instant is evidence of nothing");
  assert.strictEqual(
    w.slotWasServed(good, TODAY, SLOT, "ntfy_full_sent_at"),
    false, "the field argument must be honoured: a supersede asks about the " +
    "FULL push, and this row has none");
  assert.strictEqual(
    w.slotWasServed(good, TODAY, "nonexistent-slot"),
    true, "a slot the table does not know cannot be checked, and refusing to " +
    "suppress on it would be a NEW double-push path, not a fixed one");
  assert.strictEqual(w.slotWasServed(null, TODAY, SLOT), false);
  assert.strictEqual(
    w.slotWasServed({ ntfy_sent_at: "not a date", scheduled_instant: due },
      TODAY, SLOT),
    false, "an unparseable instant is not a delivery");
});

test("slotDueMs dates the slot from the schedule table, and never from the clock", () => {
  const w = loadWorker();
  assert.strictEqual(w.slotDueMs("2026-10-09", "morning"), Date.parse("2026-10-09T01:35:00Z"));
  assert.strictEqual(w.slotDueMs("2026-10-09", "evening"), Date.parse("2026-10-09T13:35:00Z"));
  assert.ok(Number.isNaN(w.slotDueMs("2026-10-09", "afternoon")),
    "an unknown slot has no due instant; returning one would make every check " +
    "against it meaningless");
  assert.ok(Number.isNaN(w.slotDueMs("", "morning")),
    "an absent date has no due instant either");
});

test("the sequence_id read-back IGNORES a push whose title carries the TEST marker", async () => {
  const w = loadWorker();
  const seq = "dmn-" + TODAY + "-" + SLOT;
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    cache: [{ id: "aaa", time: (T0 - 60 * 1000) / 1000, sequence_id: seq,
              title: "TEST · " + "Morning Digest · 2026-10-09 · 12 items" }],
  });
  assert.strictEqual(r.thrown, null);
  assert.strictEqual(postCalls(r.net).length, 1,
    "a TEST push was counted as this slot's delivery, so the proof run " +
    "suppressed the real digest. The title is the only ntfy field a later " +
    "read-back can still see, which is why the marker belongs there.");
  assert.ok(/SIMULATED/.test(r.logs.text()), r.logs.text());
});

test("the read-back still suppresses on the SAME message once the marker is gone", async () => {
  const w = loadWorker();
  // The control for the test above: the only difference is the prefix, so a
  // read-back that never suppressed anything cannot pass both.
  const seq = "dmn-" + TODAY + "-" + SLOT;
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    cache: [{ id: "aaa", time: (T0 - 60 * 1000) / 1000, sequence_id: seq,
              title: "Morning Digest · 2026-10-09 · 12 items" }],
  });
  assert.strictEqual(postCalls(r.net).length, 0);
  assert.strictEqual(r.outcome.deduped, true);
});

test("isTestPushTitle matches the marker and nothing else", () => {
  const w = loadWorker();
  assert.strictEqual(w.isTestPushTitle("TEST · Morning Digest"), true);
  assert.strictEqual(w.isTestPushTitle("  TEST · Morning Digest"), true,
    "leading whitespace is not a different marker");
  assert.strictEqual(w.isTestPushTitle("Morning Digest · TEST"), false,
    "the marker is a PREFIX identity, not a word somewhere in the title");
  assert.strictEqual(w.isTestPushTitle("TESTING the relay"), false);
  assert.strictEqual(w.isTestPushTitle(""), false);
  assert.strictEqual(w.isTestPushTitle(null), false);
  assert.strictEqual(w.TEST_TITLE_PREFIX, "TEST \u00b7 ");
});

// ==========================================================================
// 12. The ingest endpoint
// ==========================================================================

async function ingest(w, kv, body, headers, method, envExtra) {
  const verb = method || "POST";
  const init = {
    method: verb,
    headers: Object.assign({ "Content-Type": "application/json",
                             "X-Ingest-Token": INGEST_SECRET }, headers || {}),
  };
  // undici refuses a body on a GET, which is exactly the request shape the 405
  // assertion needs to be able to build.
  if (verb !== "GET" && verb !== "HEAD") {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
  }
  const logs = captureLogs();
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://worker.example.invalid/?ingest=1", init),
      ntfyEnv(kv, envExtra));
  } finally {
    logs.restore();
  }
  return { res: res, logs: logs };
}

test("the ingest endpoint stores a good artifact and NEVER touches the ledger", async () => {
  const w = loadWorker();
  const harness = makeHarness({});
  const payload = JSON.parse(artifact());
  const { res, logs } = await ingest(w, harness.kv, payload);
  assert.strictEqual(res.status, 200);
  const body = JSON.parse(await res.text());
  assert.strictEqual(body.ok, true);
  assert.strictEqual(body.key, artifactKey());
  assert.strictEqual(harness.kv.store[artifactKey()], JSON.stringify(payload));
  assert.strictEqual(putCount(harness.kv, "digest:ledger"), 0,
    "the ingest endpoint wrote the DELIVERY LEDGER. The build side handing " +
    "over a record must have no way to rewrite the record of what was sent.");
  assert.ok(harness.kv.gets.indexOf("digest:ledger") < 0,
    "the ingest endpoint READ the ledger, which is the other half of the same " +
    "hole: " + JSON.stringify(harness.kv.gets));
  assert.ok(logs.text().indexOf(TOPIC) < 0, logs.text());
});

test("the ingest endpoint fails CLOSED when the secret is absent", async () => {
  const w = loadWorker();
  const harness = makeHarness({});
  const logs = captureLogs();
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://worker.example.invalid/?ingest=1", {
        method: "POST",
        headers: { "X-Ingest-Token": "" },
        body: artifact(),
      }),
      { KV: harness.kv, NTFY_SERVER: NTFY_SERVER, NTFY_TOPIC: TOPIC });
  } finally {
    logs.restore();
  }
  assert.strictEqual(res.status, 401,
    "an UNCONFIGURED ingest secret accepted a write. The rule is " +
    "functions/_middleware.js's: an unset secret means nobody gets in, never " +
    "everybody does.");
  assert.ok(/DIGEST_INGEST_MISCONFIGURED/.test(logs.text()),
    "the misconfiguration must be logged loudly — a silently refused ingest " +
    "looks exactly like a build that stopped handing over: " + logs.text());
  assert.strictEqual(harness.kv.puts.length, 0);
});

test("the ingest endpoint refuses a wrong token and any non-POST method", async () => {
  const w = loadWorker();
  const harness = makeHarness({});
  const wrong = await ingest(w, harness.kv, JSON.parse(artifact()),
                             { "X-Ingest-Token": "not-the-token" });
  assert.strictEqual(wrong.res.status, 401);
  const get = await ingest(w, harness.kv, JSON.parse(artifact()), {}, "GET");
  assert.strictEqual(get.res.status, 405);
  assert.strictEqual(harness.kv.puts.length, 0);
});

test("the ingest endpoint rejects a wrong schema, a key that disagrees, and an off-archive page_url", async () => {
  const w = loadWorker();
  const cases = [
    { name: "schema_version 2", body: { schema_version: 2, date: TODAY, slot: SLOT, page_url: ARCHIVE_PAGE } },
    { name: "schema_version missing", body: { date: TODAY, slot: SLOT, page_url: ARCHIVE_PAGE } },
    { name: "slot disagrees with key", body: Object.assign(JSON.parse(artifact()), { key: artifactKey(TODAY, "evening") }) },
    { name: "date disagrees with key", body: Object.assign(JSON.parse(artifact()), { key: artifactKey(YESTERDAY, SLOT) }) },
    { name: "off-archive page_url", body: Object.assign(JSON.parse(artifact()), { page_url: "https://evil.example.invalid/x.html" }) },
    { name: "lookalike host", body: Object.assign(JSON.parse(artifact()), { page_url: "https://daily-digest.example.pages.dev.evil.invalid/x.html" }) },
    { name: "not a date", body: Object.assign(JSON.parse(artifact()), { date: "not-a-date" }) },
    { name: "not a slot", body: Object.assign(JSON.parse(artifact()), { slot: "lunch" }) },
  ];
  for (const c of cases) {
    const harness = makeHarness({});
    const { res } = await ingest(w, harness.kv, c.body);
    assert.strictEqual(res.status, 400,
      c.name + " was accepted with HTTP " + res.status);
    assert.strictEqual(harness.kv.puts.length, 0, c.name + " wrote KV");
  }
  const badJson = await ingest(w, makeHarness({}).kv, "{not json");
  assert.strictEqual(badJson.res.status, 400);
});

// ==========================================================================
// The archive origin is a RUNTIME value, and that must not become a bypass
// ==========================================================================
//
// The real Pages host cannot be committed (scripts/check_public_sanitisation.py
// fails the build if it appears), so the Worker compares every incoming
// `page_url` against DIGEST_ARCHIVE_ORIGIN and nothing else. It used to fall back
// to a committed PLACEHOLDER that no deployment serves, and the result was a 400
// on every real digest: authentication passed, the host check failed, and the
// response named the placeholder host -- so the failure looked like a bad
// artifact instead of a missing secret, for nine days.
//
// These tests pin BOTH halves of the fix -- the override is honoured, the check
// is still a check, and there is no fallback host to silently compare against.

test("the archive origin comes from env, and a malformed value is REFUSED rather than replaced", async () => {
  const w = loadWorker();
  const SENTINEL = "https://archive.sentinel.invalid";

  assert.strictEqual(w.digestArchiveHost({ DIGEST_ARCHIVE_ORIGIN: SENTINEL }),
    "archive.sentinel.invalid",
    "DIGEST_ARCHIVE_ORIGIN was not read from env, so every real digest stays " +
    "refused by the page_url host check.");
  assert.strictEqual(w.digestArchiveIndexUrl({ DIGEST_ARCHIVE_ORIGIN: SENTINEL + "/" }),
    SENTINEL + "/",
    "the index URL helper does not honour DIGEST_ARCHIVE_ORIGIN");

  // A value that is not an absolute http(s) URL with a dotted host is REFUSED,
  // not trusted: the host check compares against whatever this returns, so a
  // typo must never be able to widen it -- and it must never be quietly swapped
  // for a placeholder either, because that is the bug this replaced. "https:///nohost"
  // is the interesting one: `new URL` turns it into the hostname "nohost", and a
  // single label can never serve the digest.
  for (const bad of ["not a url", "/relative", "javascript:alert(1)",
                     "https:///nohost", "ftp://archive.example.invalid",
                     "https://archive"]) {
    assert.throws(
      () => w.digestArchiveHost({ DIGEST_ARCHIVE_ORIGIN: bad }),
      /DIGEST_ARCHIVE_ORIGIN/,
      "a malformed DIGEST_ARCHIVE_ORIGIN (" + JSON.stringify(bad) + ") did not " +
      "fail: it was either accepted as the archive host, which widens the host " +
      "check, or replaced by a placeholder, which blames the artifact");
  }
});

test("a missing DIGEST_ARCHIVE_ORIGIN FAILS LOUDLY instead of degrading to a placeholder", async () => {
  const w = loadWorker();
  let message = "";
  try {
    w.digestArchiveHost({});
  } catch (err) {
    message = String(err && err.message || err);
  }
  assert.ok(message.indexOf("DIGEST_ARCHIVE_ORIGIN") >= 0,
    "a Worker with no DIGEST_ARCHIVE_ORIGIN did not fail. The placeholder " +
    "fallback is back, and with it the 400 that blames every real artifact. " +
    "Got: " + JSON.stringify(message));
  assert.ok(message.indexOf("wrangler secret put") >= 0,
    "the failure does not say how to fix it. The owner reads this in a log at " +
    "06:42, not a wiki page: " + JSON.stringify(message));
  assert.ok(message.indexOf("daily-digest.example.pages.dev") < 0,
    "the failure message still names the placeholder host, so a reader is again " +
    "told to look at the wrong end of the problem: " + JSON.stringify(message));
  assert.throws(() => w.digestArchiveIndexUrl({}), /DIGEST_ARCHIVE_ORIGIN/,
    "the archive index helper still invents a URL when the origin is unset");

  // And the same through the two callers that face a real request, because a
  // helper that throws while its callers swallow it is not loud at all.
  const ingestRes = await ingest(w, makeHarness({}).kv, JSON.parse(artifact()),
                                 null, null, { DIGEST_ARCHIVE_ORIGIN: "" });
  assert.strictEqual(ingestRes.res.status, 500,
    "a misconfigured Worker answered the ingest with " + ingestRes.res.status +
    ", so the refusal is still being reported as something other than a " +
    "configuration failure");
  const refused = w.validateArtifact(artifact(), SLOT, TODAY, T0, {});
  assert.strictEqual(refused.ok, false);
  assert.strictEqual(refused.reason, "config",
    "validateArtifact did not name the missing origin as a configuration " +
    "problem, so the delivery ladder hunts an artifact defect instead: " +
    JSON.stringify(refused));
});

test("the ingest accepts the CONFIGURED archive host and still refuses every other host", async () => {
  const w = loadWorker();
  const SENTINEL = "https://archive.sentinel.invalid";
  const env = { DIGEST_ARCHIVE_ORIGIN: SENTINEL };

  // 1. the configured host is accepted
  const good = makeHarness({});
  const accepted = await ingest(w, good.kv, Object.assign(JSON.parse(artifact()), {
    page_url: SENTINEL + "/digests/" + TODAY + "-" + SLOT + ".html",
  }), null, null, env);
  assert.strictEqual(accepted.res.status, 200,
    "an artifact pointing at the CONFIGURED archive host was refused: " +
    (await accepted.res.text()));

  // 2. the committed placeholder is now a FOREIGN host and must still 400 --
  //    this is the proof that the override did not turn the check off
  const placeholder = await ingest(w, good.kv, artifact(), null, null, env);
  assert.strictEqual(placeholder.res.status, 400,
    "the placeholder host was accepted once a real host was configured, which " +
    "means the host check was bypassed rather than parameterised");
  assert.strictEqual(good.kv.puts.length, 1,
    "the refused ingest still wrote to KV");

  // 3. and a lookalike of the CONFIGURED host is refused too
  const lookalike = await ingest(w, good.kv, Object.assign(JSON.parse(artifact()), {
    page_url: "https://archive.sentinel.invalid.evil.invalid/x.html",
  }), null, null, env);
  assert.strictEqual(lookalike.res.status, 400,
    "a lookalike of the configured host was accepted, so the comparison is a " +
    "substring test rather than an exact host match");
});

test("validateArtifact gates the delivery path on the same runtime origin", async () => {
  const w = loadWorker();
  const SENTINEL = "https://archive.sentinel.invalid";
  const env = { DIGEST_ARCHIVE_ORIGIN: SENTINEL };
  const payload = JSON.parse(artifact());

  const right = w.validateArtifact(
    JSON.stringify(Object.assign({}, payload, {
      page_url: SENTINEL + "/digests/" + TODAY + "-" + SLOT + ".html",
    })), SLOT, TODAY, T0, env);
  assert.strictEqual(right.ok, true,
    "validateArtifact rejected the configured archive host: " + right.reason +
    " -- " + right.plain);

  // With the sentinel configured, the fixture's own host is now a FOREIGN host:
  // the delivery path and the ingest path must agree that it is refused.
  assert.strictEqual(w.validateArtifact(artifact(), SLOT, TODAY, T0, env).ok, false,
    "validateArtifact accepted the placeholder host while a real one was " +
    "configured, so the delivery path and the ingest path disagree");
  // And a 4-argument call no longer means "use the placeholder": there is no
  // placeholder to use, so it is a configuration failure and must say so.
  const noEnv = w.validateArtifact(artifact(), SLOT, TODAY, T0);
  assert.strictEqual(noEnv.ok, false,
    "the 4-argument call still validated an artifact. There is no fallback " +
    "archive host any more, so it must refuse and name the configuration.");
  assert.strictEqual(noEnv.reason, "config", JSON.stringify(noEnv));
});

test("the ingest token is compared in constant time, not with ===", async () => {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  const start = src.indexOf("async function handleDigestIngest");
  assert.ok(start > 0, "handleDigestIngest is missing from " + WORKER_PATH);
  const region = src.slice(start, start + 4000).replace(/\s+/g, " ");
  assert.ok(/constantTimeEquals\(provided, expected\)/.test(region),
    "the ingest token must go through constantTimeEquals");
  assert.ok(!/provided\s*===|===\s*expected|expected\s*===/.test(region),
    "the ingest token is compared with === somewhere in handleDigestIngest, " +
    "which leaks its value through timing and is flagged by every scanner " +
    "worth reading. functions/_middleware.js compares by SHA-256 for exactly " +
    "this reason.");
});

// ==========================================================================
// 13. ?health=1 digest fields, and the switch that has NOT been flipped
// ==========================================================================

test("?health=1 reports the digest fields, the last delivery and the live arm", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const harness = makeHarness({
    initial: {
      [artifactKey()]: artifact(),
      [artifactKey(TODAY, "evening")]: artifact({ slot: "evening", date: TODAY,
        page_url: "https://daily-digest.example.pages.dev/digests/2026-10-09-evening.html" }),
      "digest:ledger": ledgerSent(),
    },
  });
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?health=1"), ntfyEnv(harness.kv));
  } finally {
    clock.restore();
  }
  assert.strictEqual(res.status, 200);
  const health = JSON.parse(await res.text());
  assert.ok(health.digest, "?health=1 carries no digest object");
  const d = health.digest;
  assert.strictEqual(d.artifact_morning_present, true);
  assert.strictEqual(d.artifact_evening_present, true);
  assert.strictEqual(d.artifact_morning_age_s, 4 * 60,
    "the morning artifact's age must come from built_at");
  assert.strictEqual(d.ledger_age_s, 60,
    "the ledger age must come from as_of");
  assert.strictEqual(d.last_delivery.kind, "delivered");
  assert.strictEqual(d.last_delivery.date, TODAY);
  assert.strictEqual(d.last_delivery.slot, SLOT);
  assert.strictEqual(d.last_delivery.delta_s, 30,
    "delta_s is first_ntfy_sent_at MINUS scheduled_instant, in that order: " +
    "the seeded send was at 01:35:30Z and the slot was due at 01:35:00Z");
  assert.strictEqual(d.last_delivery.on_time, true);
  assert.deepStrictEqual(d.unreadable, []);
  assert.strictEqual(d.on_time_s, w.DELIVERY_ON_TIME_S);
  assert.strictEqual(d.retention_days, w.DELIVERY_RETENTION_DAYS);
});

test("?health=1 reports a send BEFORE its slot as early, never on time", async () => {
  // The 2026-10-17T18:16:53Z mislabel sent the 18th's evening digest 19 hours
  // before its slot. A delta of -5m must not read as punctual: the renderer's
  // on_time count and this boolean describe the same entry.
  const w = loadWorker();
  const clock = fakeClock(T0);
  const sentEarly = new Date(Date.parse("2026-10-09T01:35:00Z") - 5 * MIN)
    .toISOString();
  const harness = makeHarness({
    initial: {
      [artifactKey()]: artifact(),
      "digest:ledger": ledgerWith({
        state: "sent",
        kind: "delivered",
        sequence_id: "dmn-" + TODAY + "-" + SLOT,
        ntfy_sent_at: sentEarly,
        first_ntfy_sent_at: sentEarly,
        scheduled_instant: "2026-10-09T01:35:00Z",
      }),
    },
  });
  let res;
  try {
    res = await w.WORKER.fetch(
      new Request("https://probe.invalid/?health=1"), ntfyEnv(harness.kv));
  } finally {
    clock.restore();
  }
  const d = JSON.parse(await res.text()).digest;
  assert.strictEqual(d.last_delivery.delta_s, -5 * 60);
  assert.strictEqual(d.last_delivery.on_time, false,
    "a send before its own slot is early; counting it as on time made the page " +
    "and ?health=1 contradict each other");
});

test("?health=1 reports a missing artifact as absent and never as age 0", async () => {
  const w = loadWorker();
  const harness = makeHarness({ initial: {} });
  const res = await w.WORKER.fetch(
    new Request("https://probe.invalid/?health=1"), ntfyEnv(harness.kv));
  const d = JSON.parse(await res.text()).digest;
  assert.strictEqual(d.artifact_morning_present, false);
  assert.strictEqual(d.artifact_morning_age_s, null,
    "an absent artifact reported age 0, which is a PASS and would let a KV " +
    "outage or a dead build look healthy");
  assert.strictEqual(d.last_delivery, null);
});

test("?health=1 names the keys it could not read instead of inventing an age", async () => {
  const w = loadWorker();
  const harness = makeHarness({
    failGet: ["digest:ledger", artifactKey()],
  });
  const res = await w.WORKER.fetch(
    new Request("https://probe.invalid/?health=1"), ntfyEnv(harness.kv));
  const d = JSON.parse(await res.text()).digest;
  assert.ok(d.unreadable.indexOf("digest:ledger") >= 0,
    "an unreadable ledger was reported as healthy: " + JSON.stringify(d));
  assert.strictEqual(d.ledger_age_s, null);
  assert.strictEqual(d.artifact_morning_age_s, null);
});

// ==========================================================================
// 13. U5 — THE SWITCH. The digest crons deliver, and the re-fire is a no-op
// ==========================================================================

test("the digest crons call deliverDigest — the switch is flipped", async () => {
  const w = loadWorker();
  assert.strictEqual(w.DIGEST_CRON_ARM, "deliverDigest",
    "DIGEST_CRON_ARM still reads " + w.DIGEST_CRON_ARM + ", so ?health=1 " +
    "would report a switch that the crons did not take");
  for (const [cron, slot] of [[w.CRON_DIGEST_MORNING, "morning"],
                              [w.CRON_DIGEST_EVENING, "evening"]]) {
    const harness = makeHarness({
      initial: { [artifactKey(TODAY, slot)]: artifact({ slot: slot, date: TODAY,
        page_url: "https://daily-digest.example.pages.dev/digests/" + TODAY + "-" +
          slot + ".html" }) },
    });
    const logs = captureLogs();
    let outcome = null;
    try {
      // GH_TOKEN is present HERE and absent everywhere else in this file, on
      // purpose: if the cron still dispatched to GitHub it would have a token
      // to do it with, and the delivery arm would not need one.
      const pending = [];
      const clock = fakeClock(T0);
      try {
        await w.WORKER.scheduled({ cron: cron },
          ntfyEnv(harness.kv, { GH_TOKEN: "dispatch-token" }),
          { waitUntil: (p) => pending.push(p) });
        await Promise.all(pending);
      } finally {
        clock.restore();
      }
      outcome = harness.kv.store["digest:ledger"];
    } finally {
      logs.restore();
    }
    const urls = harness.net.calls.map((c) => c.url);
    assert.ok(!urls.some((u) => u.indexOf("api.github.com") >= 0),
      "cron " + cron + " still dispatches a GitHub Actions build. The owner " +
      "would get the digest twice on every slot, once from the Worker and " +
      "once from the build the dispatch wakes.\n" + JSON.stringify(urls));
    assert.ok(urls.every((u) => u.indexOf(NTFY_SERVER) === 0),
      "cron " + cron + " reached something other than ntfy: " +
      JSON.stringify(urls));
    assert.strictEqual(postCalls(harness.net).length, 1,
      "cron " + cron + " did not push exactly once: " + JSON.stringify(urls));
    assert.ok(outcome, "cron " + cron + " recorded nothing in the ledger, so " +
      "the re-fire and the 07:50 fallback would both re-deliver the slot");
    const entry = JSON.parse(outcome).days[TODAY][slot];
    assert.strictEqual(entry.state, w.STATE_SENT);
    assert.strictEqual(entry.ntfy_sent_at, "2026-10-09T01:36:00Z");
    assert.strictEqual(entry.first_ntfy_sent_at, "2026-10-09T01:36:00Z");
    assert.strictEqual(entry.scheduled_instant,
      slot === "evening" ? "2026-10-09T13:35:00Z" : "2026-10-09T01:35:00Z",
      "the punctuality anchor must be the slot's PRIMARY cron, never the " +
      "re-fire: a digest delivered by the re-fire is genuinely late and has " +
      "to be reported late");
  }
});

test("dispatchDigest is RETAINED, so the switch back is one line", async () => {
  const w = loadWorker();
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  assert.ok(/async function dispatchDigest\(env, digestTime, slotDate\)/.test(src),
    "dispatchDigest was deleted. Retaining it is what makes the switch " +
    "reversible in one line and lets ?health=1 keep reporting the arm that " +
    "would be needed as a last resort if ntfy itself is down.");
  const harness = makeHarness({ initial: { [artifactKey()]: artifact() } });
  const logs = captureLogs();
  try {
    const pending = [];
    const clock = fakeClock(T0);
    try {
      await w.WORKER.scheduled({ cron: w.CRON_DIGEST_MORNING },
        ntfyEnv(harness.kv, { GH_TOKEN: "dispatch-token" }),
        { waitUntil: (p) => pending.push(p) });
      await Promise.all(pending);
    } finally {
      clock.restore();
    }
  } finally {
    logs.restore();
  }
  assert.ok(!harness.net.calls.some((c) => c.url.indexOf("api.github.com") >= 0),
    "the digest cron called dispatchDigest AND delivered. Both arms live at " +
    "once is a double push by construction.");
  const health = await (await w.WORKER.fetch(
    new Request("https://probe.invalid/?health=1"), ntfyEnv(harness.kv))).text();
  assert.ok(/deliverDigest/.test(health),
    "?health=1 must report which arm is live, so a half-finished switch can " +
    "never leave a reader guessing: " + health.slice(0, 400));
});

test("the 40 1 / 40 13 re-fire crons exist and land 5 minutes after each slot", async () => {
  const w = loadWorker();
  assert.strictEqual(w.CRON_DIGEST_REFIRE_MORNING, "40 1 * * *");
  assert.strictEqual(w.CRON_DIGEST_REFIRE_EVENING, "40 13 * * *");
  // Cloudflare cron START JITTER is the punctuality risk this exists for:
  // `35 1` can genuinely start at 01:47, and nothing in the code prevents it.
  const gaps = [
    [w.CRON_DIGEST_MORNING, w.CRON_DIGEST_REFIRE_MORNING],
    [w.CRON_DIGEST_EVENING, w.CRON_DIGEST_REFIRE_EVENING],
  ];
  for (const [primary, refire] of gaps) {
    const at = (c) => Date.parse("2000-01-01T" + c.split(" ")[1].padStart(2, "0") +
      ":" + c.split(" ")[0].padStart(2, "0") + ":00Z");
    assert.strictEqual(at(refire) - at(primary), 5 * 60 * 1000,
      "the re-fire " + refire + " is not 5 minutes after " + primary);
    assert.notStrictEqual(refire, primary,
      "the re-fire and the slot are the same cron, which fires twice and " +
      "never covers a late start");
  }
});

test("the 40 1 re-fire performs ZERO network calls when the ledger already sent the slot", async () => {
  const w = loadWorker();
  // `ledgerSent()` records ntfy_sent_at for TODAY/morning, which is exactly
  // the state the 07:20 slot left behind. The re-fire must read one key and
  // stop: a second read-back GET and a POST here would be a duplicate push
  // bought with a second network call on every single day.
  const harness = makeHarness({
    initial: {
      [artifactKey()]: artifact(),
      "digest:ledger": ledgerSent(),
    },
  });
  const logs = captureLogs();
  try {
    await runScheduledAtT0(w.WORKER, w.CRON_DIGEST_REFIRE_MORNING, ntfyEnv(harness.kv));
  } finally {
    logs.restore();
  }
  assert.deepStrictEqual(harness.net.calls, [],
    "the re-fire made a network call on a slot the ledger already recorded: " +
    JSON.stringify(harness.net.calls));
  assert.strictEqual(harness.kv.gets.length, 1,
    "the re-fire must cost exactly ONE KV read — the ledger. Any more and " +
    "the cheap no-op it is supposed to be is not cheap: " +
    JSON.stringify(harness.kv.gets));
  assert.deepStrictEqual(harness.kv.gets, ["digest:ledger"]);
  assert.deepStrictEqual(harness.kv.puts, [],
    "the re-fire wrote to KV on an already-sent slot: " +
    JSON.stringify(harness.kv.puts));
  assert.ok(/duplicate_suppressed/.test(logs.text()),
    "a suppressed re-fire must say so in its own log line, or a 07:25 that " +
    "did nothing is indistinguishable from a 07:25 that never ran:\n" +
    logs.text());
  const kept = JSON.parse(harness.kv.store["digest:ledger"]).days[TODAY][SLOT];
  assert.strictEqual(kept.ntfy_sent_at, new Date(T0 - 30 * 1000).toISOString(),
    "the re-fire overwrote the slot's recorded send instant, which would move " +
    "the punctuality anchor and rewrite history");
});

test("the re-fire DOES deliver when the slot's cron never ran", async () => {
  const w = loadWorker();
  // The case the re-fire exists for: the 07:20 invocation did not happen (or
  // died before recording anything), and the artifact is sitting there.
  const harness = makeHarness({
    initial: { [artifactKey()]: artifact() },
  });
  await runScheduledAtT0(w.WORKER, w.CRON_DIGEST_REFIRE_MORNING, ntfyEnv(harness.kv));
  assert.strictEqual(postCalls(harness.net).length, 1,
    "the re-fire did not deliver a slot the ledger says was never sent, so a " +
    "cron that started late is still a missed digest: " +
    JSON.stringify(harness.net.calls));
  const entry = JSON.parse(harness.kv.store["digest:ledger"]).days[TODAY][SLOT];
  assert.strictEqual(entry.state, w.STATE_SENT);
  assert.strictEqual(entry.scheduled_instant, "2026-10-09T01:35:00Z",
    "the re-fire must still be measured against the SLOT, not against itself");
});

test("the recorded KV budget is real: the artifact write carries an expiry", async () => {
  const w = loadWorker();
  const harness = makeHarness({});
  const { res } = await ingest(w, harness.kv, JSON.parse(artifact()));
  assert.strictEqual(res.status, 200);
  assert.strictEqual(harness.kv.puts.length, 1);
  const put = harness.kv.puts[0];
  assert.ok(put.extra, "the artifact was stored with no options, so it can " +
    "never expire and the two deletes/day deploy/schedule.json budgets are a " +
    "fiction. 2 artifact keys a day is 730 keys a year in a namespace that " +
    "also holds the ledger.");
  const ttl = Number(put.extra.expirationTtl);
  assert.ok(Number.isFinite(ttl) && ttl >= 26 * 60 * 60,
    "the artifact TTL is " + put.extra.expirationTtl + "s, which is below " +
    "ARTIFACT_MAX_BUILD_AGE_S (" + w.ARTIFACT_MAX_BUILD_AGE_S + "s). A key " +
    "that expires while it is still inside the age ceiling would turn a " +
    "present digest into a fallback push for no reason.");
  assert.strictEqual(w.ARTIFACT_TTL_S / 3600, 28,
    "ARTIFACT_TTL_S changed. update deploy/schedule.json's " +
    "kv_writes_per_day digest block and re-derive the delete count.");
});

test("?health=1 reports the re-fire crons as part of the DISPATCH schedule, not as deployed triggers", async () => {
  const w = loadWorker();
  const harness = makeHarness({ initial: {} });
  const res = await w.WORKER.fetch(
    new Request("https://probe.invalid/?health=1"), ntfyEnv(harness.kv));
  const health = JSON.parse(await res.text());

  // DEFECT FIXED HERE. `crons` used to be built from the in-source CRON_*
  // constants, so it reported SEVEN crons while Cloudflare had registered
  // five. A failed trigger write therefore looked like a success: a liveness
  // check built from in-source intent cannot fail when a deploy half-applies.
  // It is now the one cron the Worker is actually deployed with.
  assert.deepStrictEqual(health.crons, [w.CRON_DISPATCHER],
    "?health=1 must report the ONE deployed trigger and nothing else, because "
    + "drift-canary.yml compares this array against deploy/schedule.json's "
    + "crons as SETS: any extra entry turns the deployed Worker's own report "
    + "into a DRIFT alarm. Reported: " + JSON.stringify(health.crons));

  // ...and it says plainly that it cannot verify Cloudflare's own registry,
  // rather than presenting in-source intent as an observation.
  assert.strictEqual(health.crons_verified_at_runtime, false,
    "?health=1 must admit that a Worker cannot query its own schedules at "
    + "runtime. Reporting an in-source constant without saying so is exactly "
    + "the defect this field replaces.");
  assert.ok(/cannot query its own schedules/i.test(String(health.crons_source)),
    "crons_source must state the limitation in the words an operator needs: " +
    JSON.stringify(health.crons_source));

  // The logical schedule is reported separately and in full, so nothing is
  // lost by `crons` no longer carrying the job expressions.
  const reported = JSON.stringify(health.dispatch_schedule);
  for (const cron of [w.CRON_DIGEST_MORNING, w.CRON_DIGEST_EVENING,
                      w.CRON_DIGEST_REFIRE_MORNING,
                      w.CRON_DIGEST_REFIRE_EVENING]) {
    assert.ok(reported.indexOf(cron) >= 0,
      "the dispatch schedule does not report " + cron + ": " + reported);
  }
});

// ==========================================================================
// 15. THE MERGED DISPATCHER — five crons into one, cadence preserved exactly
// ==========================================================================
//
// WHY THIS BLOCK IS HERE AND WHY IT IS JS. The merge is justified by one claim:
// merging the crons does not change when any job fires. That claim is a property
// of arithmetic inside deploy/cloudflare-worker.js, so it is proved by RUNNING
// the dispatcher's own plan function and comparing it against the pre-merge
// cron expressions over a simulated day. A Python re-implementation would agree
// with itself for ever, which is the failure mode this file exists to avoid.
//
// THE GUARANTEE IS STILL AT-LEAST-ONCE, WITH A POSSIBLE DUPLICATE ON THE
// RECOVERY PATH ONLY. ntfy does not deduplicate a caller-supplied message id —
// measured live: 10 POSTs, 10 messages, HTTP 200 every time — and there is no
// transaction spanning "ntfy has the message" and "KV has the record".
// No test below claims exactly-once, and none may.

/** One dispatcher tick at a given UTC instant, with the grid position pinned. */
async function dispatcherTick(WORKER, dispatcherCron, ms, env) {
  const pending = [];
  await WORKER.scheduled({ cron: dispatcherCron, scheduledTime: ms }, env,
                         { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
}

/** Does a daily 5-field UTC cron fire at this (hour, minute)? Minimal evaluator. */
function cronFiresAt(expr, hour, minute) {
  const [min, hr] = String(expr).split(" ");
  const step = min.startsWith("*/") ? Number(min.slice(2)) : null;
  if (step !== null) return minute % step === 0;
  if (Number(min) !== minute) return false;
  if (hr !== "*") return Number(hr) === hour;
  return true;
}

/** The pre-merge schedule, as it was deployed before the free-tier merge. */
const PRE_MERGE_CRONS = ["*/3 * * * *", "*/2 * * * *", "35 1 * * *",
                         "35 13 * * *", "17 * * * *"];

test("the merged dispatcher reproduces EVERY pre-merge cron instant, over a simulated day", async () => {
  const w = loadWorker();

  // The reference set is not a comment: it is what the Worker's OWN constants
  // still compute to. After the merge these are computed rather than literals,
  // so this is also the check that the computation still produces the
  // pre-merge strings and not something subtly different.
  assert.deepStrictEqual(PRE_MERGE_CRONS.slice().sort(),
    [w.CRON_BREAKING_SWEEP, w.CRON_ACADEMIC_SWEEP, w.CRON_HEALTHCHECK,
     w.CRON_DIGEST_MORNING, w.CRON_DIGEST_EVENING].sort(),
    "the Worker's schedule constants no longer produce the five pre-merge cron "
    + "expressions: "
    + JSON.stringify([w.CRON_BREAKING_SWEEP, w.CRON_ACADEMIC_SWEEP,
      w.CRON_HEALTHCHECK, w.CRON_DIGEST_MORNING, w.CRON_DIGEST_EVENING]));

  // The precise claim, job by job. For each of the five crons the account used
  // to hold, the set of (hour, minute) positions on which the merged dispatcher
  // runs THAT job must equal the set the cron expression fired on. This is
  // stronger than comparing the union: a union comparison cannot tell a job
  // that moved from one that was simply dropped.
  const jobsAndCrons = [
    ["breaking", "*/3 * * * *"],
    ["academic", "*/2 * * * *"],
    ["healthcheck", "17 * * * *"],
    ["digest:morning", "35 1 * * *"],
    ["digest:evening", "35 13 * * *"],
  ];
  for (const [job, cron] of jobsAndCrons) {
    const fromCron = new Set();
    const fromDispatcher = new Set();
    for (let h = 0; h < 24; h += 1) {
      for (let m = 0; m < 60; m += 1) {
        if (cronFiresAt(cron, h, m)) fromCron.add(h * 60 + m);
        if (w.planTickFor(h, m).indexOf(job) >= 0) fromDispatcher.add(h * 60 + m);
      }
    }
    // The digest jobs legitimately fire MORE often than their slot cron: the
    // retry window is the addition this merge makes. For those, the slot cron
    // must be a SUBSET and the extra positions must be exactly the window.
    const isDigest = job.indexOf("digest:") === 0;
    if (isDigest) {
      assert.deepStrictEqual([...fromDispatcher].sort((a, b) => a - b)
        .filter((k) => !fromCron.has(k)),
        extraRetryMinutesFor(w, job),
        "the " + job + " job fires at positions its slot cron `" + cron +
        "` did not, and the extra positions are not exactly the bounded retry "
        + "window.");
    } else {
      assert.deepStrictEqual([...fromDispatcher].sort((a, b) => a - b),
        [...fromCron].sort((a, b) => a - b),
        "the merged dispatcher runs " + job + " on a different set of minutes "
        + "than the `" + cron + "` it replaced.");
    }
  }

  // And the specific numbers the owner asked to be protected, by name.
  const count = (job) => {
    let n = 0;
    for (let h = 0; h < 24; h += 1) {
      for (let m = 0; m < 60; m += 1) {
        if (w.planTickFor(h, m).indexOf(job) >= 0) n += 1;
      }
    }
    return n;
  };
  assert.strictEqual(count("breaking"), 480,
    "the breaking sweep must still be */3 — 480 a day. A coarser stride is "
    + "exactly the weakening this merge was told not to cause.");
  assert.strictEqual(count("academic"), 720,
    "the academic sweep must still be */2 — 720 a day.");
  assert.strictEqual(count("healthcheck"), 24,
    "the dead-man's switch must still run hourly on one fixed minute.");
});

/** The minute positions a digest job runs that its own slot cron did not. */
function extraRetryMinutesFor(w, job) {
  const slot = job.split(":")[1];
  const slotCron = slot === "morning" ? w.CRON_DIGEST_MORNING : w.CRON_DIGEST_EVENING;
  const slotHour = Number(slotCron.split(" ")[1]);
  const out = [];
  for (let m = 0; m < 60; m += 1) {
    if (!w.planTickFor(slotHour, m).some((j) => j === job)) continue;
    if (!cronFiresAt(slotCron, slotHour, m)) out.push(slotHour * 60 + m);
  }
  return out;
}

test("a */5 dispatcher would have WEAKENED detection — the reason this is every minute", async () => {
  const w = loadWorker();
  // The owner suggested every five minutes. This is the arithmetic for why that
  // is not available, kept as a test so the next person to suggest it gets the
  // number rather than an opinion.
  const everyFive = new Set();
  for (let m = 0; m < 60; m += 5) everyFive.add(m);
  let kept = 0;
  for (const minute of everyFive) {
    if (w.planTickFor(0, minute).indexOf("breaking") >= 0) kept += 1;
  }
  assert.strictEqual(kept, 4,
    "a five-minute grid can only ever run the */3 breaking sweep 4 times an "
    + "hour instead of 20 — a 5x drop in detection frequency. This is why the "
    + "dispatcher is every minute.");
});

test("a late tick still sends exactly once: the slot tick never ran, the retry delivers", async () => {
  const w = loadWorker();
  const harness = makeHarness({
    initial: { [artifactKey()]: artifact() },
  });
  const clock = fakeClock(T0);
  const logs = captureLogs();
  try {
    // The 01:35 slot tick is SKIPPED entirely — the case cron start jitter
    // really produces. The whole bounded window then runs.
    for (const minute of [40, 45, 50, 55]) {
      clock.restore();
      const c = fakeClock(Date.parse("2026-10-09T01:" + minute + ":00Z"));
      await dispatcherTick(w.WORKER, w.CRON_DISPATCHER, Date.parse("2026-10-09T01:" + minute + ":00Z"), ntfyEnv(harness.kv));
      c.restore();
    }
  } finally {
    clock.restore();
    logs.restore();
  }
  assert.strictEqual(postCalls(harness.net).length, 1,
    "the bounded retry window pushed " + postCalls(harness.net).length +
    " times for one slot. The ledger's ntfy_sent_at check is what makes the "
    + "later attempts free, and without it a late start is a digest storm on "
    + "the owner's phone: " + JSON.stringify(harness.net.calls.map((c) => c.method + " " + c.url)));
  const entry = JSON.parse(harness.kv.store["digest:ledger"]).days[TODAY][SLOT];
  assert.strictEqual(entry.state, w.STATE_SENT);
  assert.strictEqual(entry.scheduled_instant, "2026-10-09T01:35:00Z",
    "a digest delivered by the retry window is still measured against the "
    + "SLOT, never against the attempt that happened to push it");
});

test("a duplicate tick never double-sends: two ticks on one slot produce one delivery", async () => {
  const w = loadWorker();
  const harness = makeHarness({
    initial: { [artifactKey()]: artifact() },
  });
  const logs = captureLogs();
  const clock = fakeClock(T0);
  try {
    // The same instant dispatched twice — a slow invocation overlapping its own
    // successor, which is what "ticks overlap" means in practice.
    const slotInstant = Date.parse("2026-10-09T01:35:00Z");
    await dispatcherTick(w.WORKER, w.CRON_DISPATCHER, slotInstant, ntfyEnv(harness.kv));
    await dispatcherTick(w.WORKER, w.CRON_DISPATCHER, slotInstant, ntfyEnv(harness.kv));
    // ...and the slot tick together with the first retry, for good measure.
    await dispatcherTick(w.WORKER, w.CRON_DISPATCHER, Date.parse("2026-10-09T01:40:00Z"),
                         ntfyEnv(harness.kv));
  } finally {
    clock.restore();
    logs.restore();
  }
  assert.strictEqual(postCalls(harness.net).length, 1,
    "three ticks on one slot produced " + postCalls(harness.net).length +
    " pushes. Slot-id dedup plus the sequence_id read-back must mean only one "
    + "lands.");
  assert.ok(/duplicate_suppressed/.test(logs.text()),
    "a suppressed duplicate must say so in its own log line, or a 01:45 that "
    + "did nothing is indistinguishable from a 01:45 that never ran:\n"
    + logs.text());
});

test("the retry window STOPS after its bound, and never fires unboundedly", async () => {
  const w = loadWorker();
  const digestAt = (h, m) => w.planTickFor(h, m).some(
    (j) => j.indexOf("digest:") === 0);
  // Bounded: the attempts are +5/+10/+15/+20 and then nothing until the day
  // ends. Two arms racing the 02:05 UTC GitHub fallback would be a second push
  // by construction, so the Worker has to stop while the fallback still owns
  // the slot.
  assert.deepStrictEqual([40, 45, 50, 55].map((m) => digestAt(1, m)),
    [true, true, true, true],
    "the morning retry window must cover +5/+10/+15/+20 minutes");
  assert.deepStrictEqual([40, 45, 50, 55].map((m) => digestAt(13, m)),
    [true, true, true, true],
    "the evening retry window must cover +5/+10/+15/+20 minutes");
  for (const m of [36, 37, 38, 39, 41, 42, 43, 44, 46, 47, 48, 49, 51, 52, 53,
                   54, 56, 57, 58, 59]) {
    assert.strictEqual(digestAt(1, m), false,
      "the dispatcher still runs a digest job at 01:" + m + ". The window is "
      + "bounded so the 02:05 UTC GitHub fallback can own the slot; an "
      + "unbounded retry is two arms racing for the same push.");
    assert.strictEqual(digestAt(13, m), false,
      "the dispatcher still runs a digest job at 13:" + m);
  }
  // The last morning attempt is 01:55, ten clear minutes before the 02:05 UTC
  // fallback in daily-news.yml.
  assert.ok(Date.parse("2026-10-09T01:55:00Z")
    < Date.parse("2026-10-09T02:05:00Z"),
    "the retry window must close before the GitHub fallback fires");
});

test("a no-op tick costs NO KV write, NO KV read and NO network call", async () => {
  const w = loadWorker();
  // Every minute of a day on which no job is due. These are the ticks that
  // make the merge affordable: a "nothing is due" tick must be a clock
  // comparison, not a round-trip.
  const noopMinutes = [];
  for (let h = 0; h < 24; h += 1) {
    for (let m = 0; m < 60; m += 1) {
      if (!w.planTickFor(h, m).length) noopMinutes.push([h, m]);
    }
  }
  assert.ok(noopMinutes.length > 400,
    "expected a substantial no-op path; got " + noopMinutes.length +
    " minutes. If this is zero the dispatcher is doing work every minute and "
    + "the KV argument for the merge does not hold.");

  const harness = makeHarness({ initial: {} });
  const logs = captureLogs();
  try {
    for (const [h, m] of noopMinutes) {
      await dispatcherTick(w.WORKER, w.CRON_DISPATCHER, Date.parse("2026-10-09T" +
        String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":00Z"),
        ntfyEnv(harness.kv));
    }
  } finally {
    logs.restore();
  }
  assert.deepStrictEqual(harness.kv.puts, [],
    "a day of no-op ticks wrote to KV " + harness.kv.puts.length +
    " time(s). KV WRITES are the scarce resource on the free tier and the "
    + "whole reason the dispatcher skips work it does not need to do: "
    + JSON.stringify(harness.kv.puts.map((p) => p.key)));
  assert.deepStrictEqual(harness.kv.gets, [],
    "a day of no-op ticks read KV " + harness.kv.gets.length +
    " time(s). The tick has to be decidable from the clock alone: "
    + JSON.stringify([...new Set(harness.kv.gets)]));
  assert.deepStrictEqual(harness.net.calls, [],
    "a day of no-op ticks made " + harness.net.calls.length +
    " network call(s)");
});

test("a full simulated day of dispatcher ticks stays far under the free KV write limit", async () => {
  const w = loadWorker();
  const table = JSON.parse(fs.readFileSync(
    path.join(REPO_ROOT, "deploy", "schedule.json"), "utf8"));
  const budget = table.heartbeat.kv_writes_per_day;

  // Drives the real heartbeat path the dispatcher calls, at the real sweep
  // cadence, for a day. The throttle is what the budget is made of: the count
  // is set by the CLOCK, not by how many triggers deliver the sweeps.
  const env = { KV: null, WORKER_BUILD: "testbuild" };
  const store = {};
  const puts = [];
  env.KV = {
    store,
    async get(key) { return Object.prototype.hasOwnProperty.call(store, key)
      ? store[key] : null; },
    async put(key, value) { puts.push(key); store[key] = value; },
  };
  const start = Date.parse("2026-10-09T00:00:00Z");
  for (const [key, every] of [[w.BREAKING_HEARTBEAT_KEY, 3],
                              [w.ACADEMIC_HEARTBEAT_KEY, 2]]) {
    const clock = fakeClock(start);
    try {
      for (let minute = 0; minute < 60 * 24; minute += every) {
        // The dispatcher only writes a heartbeat on a tick it decided to sweep.
        const clockNow = start + minute * MIN;
        Date.now = () => clockNow;
        await w.touchHeartbeat(env, key, w.CRON_DISPATCHER);
      }
    } finally {
      clock.restore();
    }
  }
  const heartbeatWrites = puts.length;
  assert.strictEqual(heartbeatWrites,
    budget.breaking + budget.academic,
    "a simulated day of dispatcher sweeps cost " + heartbeatWrites +
    " heartbeat writes; deploy/schedule.json records " +
    (budget.breaking + budget.academic) + ". The budget is derived from the "
    + "throttle, and merging five crons into one must not have moved it.");

  // The system total, which is what the free-tier allowance is compared
  // against, recomputed from the same table rather than read from a comment.
  const systemTotal = budget.breaking + budget.academic
    + budget.alert_stamp_max + budget.digest.total_steady;
  assert.strictEqual(systemTotal, budget.system_total_max,
    "the recorded system_total_max is no longer the sum of its own terms");
  assert.strictEqual(systemTotal, 108,
    "the daily KV write budget moved from 108 to " + systemTotal +
    ". The merge was supposed to change WHICH cron fires, not HOW MANY keys "
    + "are written.");
  assert.ok(systemTotal < 1000,
    "the daily KV write budget (" + systemTotal + ") is no longer comfortably "
    + "under the Workers Free allowance of ~1,000 writes/day");
});

// ==========================================================================
// 16. The archive origin has ONE value, in two stores
// ==========================================================================
//
// THE LITERAL IS GONE FROM BOTH SIDES, and that is what makes this test
// possible to state honestly. The Worker used to carry its own copy of the
// archive host, and drift-canary.yml carried a second copy in its HOSTS list.
// The canary's copy was the SANITISED PLACEHOLDER, so the probe that exists to
// prove the archive is publicly readable spent nine days proving that
// `daily-digest.example.pages.dev` -- a host that is not the archive -- was not
// readable. A canary that goes green or red about the wrong host is worse than
// no canary: it is a confident answer to a question nobody asked.
//
// WHAT IS ASSERTED NOW. The value lives in exactly two stores: the Worker's
// DIGEST_ARCHIVE_ORIGIN secret, and the repo's CF_ARCHIVE_ORIGIN variable.
// The canary holds no host -- it RESOLVES one at run time through
// scripts/archive_origin.py, which accepts both names. So the canary provably
// probes the same value the Worker publishes to, and the only way they can
// disagree again is if someone sets the two stores differently -- which the
// resolver names in its own error message.

test("the archive origin is resolved from one value, and neither side carries a host literal", async () => {
  const w = loadWorker();
  const canary = fs.readFileSync(path.join(REPO_ROOT, ".github", "workflows",
                                           "drift-canary.yml"), "utf8");

  // The Worker returns exactly the host it is given, so the secret is the source.
  assert.strictEqual(w.digestArchiveHost(ARCHIVE_ENV), ARCHIVE_HOST,
    "the Worker did not return the origin it was given, so the secret is not " +
    "the single source of the archive host");
  assert.strictEqual(w.digestArchiveIndexUrl(ARCHIVE_ENV),
    "https://" + ARCHIVE_HOST + "/");

  // The canary RESOLVES its hosts; it no longer declares them. A literal here
  // is the exact drift class that caused the nine-day outage, so its absence is
  // asserted rather than assumed.
  assert.ok(/HOSTS="\$\(python3 scripts\/archive_origin\.py --hosts\)"/.test(canary),
    "drift-canary.yml no longer resolves its probe hosts at run time, so it is " +
    "back to carrying a host of its own that can drift from the Worker's");
  const declared = /HOSTS="(?!\$)[^"]*"/.exec(canary);
  assert.strictEqual(declared, null,
    "drift-canary.yml declares its probe hosts literally: " +
    (declared && declared[1]));

  // Only comments may mention a Pages hostname; no executable line may.
  const executable = canary.split(/\r?\n/)
    .filter((line) => !/^\s*#/.test(line)).join("\n");
  assert.ok(executable.indexOf(".pages.dev") < 0,
    "drift-canary.yml still names a pages.dev host on an executable line; the " +
    "host must come from CF_ARCHIVE_ORIGIN at run time, because the committed " +
    "tree is sanitised and a sanitised host is not the archive");

  // One value, two stores: the canary passes the repo variable, and the
  // resolver reads that name as well as the Worker's.
  assert.ok(/CF_ARCHIVE_ORIGIN:\s*\$\{\{\s*vars\.CF_ARCHIVE_ORIGIN\s*\}\}/.test(canary),
    "the canary step does not pass vars.CF_ARCHIVE_ORIGIN, so the resolver has " +
    "nothing to resolve and the probe would fail for the wrong reason");
  const resolver = fs.readFileSync(path.join(REPO_ROOT, "scripts",
                                             "archive_origin.py"), "utf8");
  assert.ok(resolver.indexOf("CF_ARCHIVE_ORIGIN") >= 0
    && resolver.indexOf("DIGEST_ARCHIVE_ORIGIN") >= 0,
    "the resolver does not accept both stores, so the canary and the Worker " +
    "cannot be shown to read the same value");

  // And the Worker source itself must contain no pages.dev literal at all: a
  // host in the source is a second place to correct, which is the drift class
  // this suite exists to catch, and the one that caused the nine-day outage.
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  const literals = src.match(/["'`][^"'`\n]*\.pages\.dev[^"'`\n]*["'`]/g) || [];
  assert.deepStrictEqual(literals, [],
    "the Worker still carries an archive host literal: " + JSON.stringify(literals));
});

// ==========================================================================
// 17. ntfy 429 QUOTA ESCALATION. Observed 2026-10-11: the Worker's own
//     publish came back HTTP 429 / code 42908 "daily message quota reached".
//     The Worker's push is the PRIMARY arm and, on a quota exhaustion, the
//     ONLY arm that tried: the GitHub Actions ladder is what actually reached
//     the owner, and it never ran, because nothing dispatched it.
//
//     THE DEFECT, precisely. `dispatchDigest` is called from exactly one place:
//     `pushFallbackLadder` -- the NO-ARTIFACT path. A slot that has a perfectly
//     good artifact and a 429 from ntfy never reaches it. The push at
//     deliverDigestPlan step 6 fails, records `ntfy_failed`, and returns. The
//     owner is told nothing. The 14:05Z Actions cron is a SEPARATE schedule
//     with its own 30-minute gap, so on a verified Worker failure the escalation
//     that exists for exactly this case does not fire.
//
//     WHAT IS PROVEN vs INFERRED, because the difference decides the fix.
//     PROVEN (run #288, job 114429603715): a publish from the GitHub runner IP
//     to the same topic SUCCEEDED ("ntfy notification sent ... id=KESM0snbRtYY")
//     at 08:46Z while the Worker's were 429ing. So the two arms are NOT sharing
//     one exhausted bucket in this outage, and routing to Actions is a real
//     recovery, not a retry against the same wall. INFERRED: why. ntfy
//     classifies an unauthenticated visitor by source IP; Cloudflare egress and
//     a GitHub runner are different IPs. The docs do not settle whether a
//     free ntfy.sh quota is per-IP or per-account for an ANONYMOUS publisher --
//     that half is inference and is labelled as such.
// ==========================================================================

/** A 429 answer for the POST that failed, ntfy's own body shape. */
function quotaResponse() {
  return {
    ok: false, status: 429,
    text: async () => JSON.stringify({
      code: 42908, http_code: 429, error: "daily message quota reached",
    }),
  };
}

/** POSTs aimed at the GitHub workflow-dispatch API. */
function dispatchPosts(net) {
  return net.calls.filter(
    (c) => c.method === "POST" &&
      String(c.url).indexOf("/actions/workflows/daily-news.yml/dispatches") >= 0);
}

test("a 429 on the REAL digest path dispatches the Actions fallback", async () => {
  const w = loadWorker();
  // A GOOD artifact + every POST coming back 429. This is the observed outage
  // shape: the build worked, the publish did not.
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    env: { GH_TOKEN: "dispatch-token" },
    onPost: () => quotaResponse(),
  });
  assert.strictEqual(r.thrown, null, r.thrown && String(r.thrown));

  const posts = dispatchPosts(r.net);
  assert.strictEqual(posts.length, 1,
    "a 429 on a slot that HAD a good artifact dispatched nothing, so the owner "
    + "was told nothing and the only other arm -- GitHub Actions, which "
    + "publishes from a different IP -- was never woken. dispatches=" +
    + posts.length + " calls=" +
    + JSON.stringify(r.net.calls.map((c) => c.method + " " + c.url)));
  const body = JSON.parse(posts[0].body);
  assert.strictEqual(body.inputs.digest_time, SLOT);
  // THE SLOT'S DATE TRAVELS WITH THE DISPATCH, and a recovery that omits it is
  // the defect this line exists for: the receiving run would take its date from
  // its own clock, which can already be the next Nepal day. `TODAY` is the
  // slot's NPT date, which is exactly what it must be told.
  assert.strictEqual(body.inputs.slot_date, TODAY,
    "the 429 recovery was dispatched for the " + TODAY + " " + SLOT + " slot "
    + "without its date (slot_date=" + body.inputs.slot_date + "), so the "
    + "receiving run would date the digest off its own clock. inputs="
    + JSON.stringify(body.inputs));
  assert.strictEqual(body.inputs.dry_run, "false",
    "a dispatched recovery that is dry delivers nothing, which is the silence "
    + "this fix exists to end");
  assert.strictEqual(body.inputs.trigger_source, "worker");
});

test("the Worker's digest dispatch dates the slot from the TICK, never the clock", async () => {
  // THE REAL WINDOW, measured rather than invented: the evening chain runs
  // 13:35-13:55 UTC and Nepal midnight is 18:15 UTC, so a tick that starts late
  // enough crosses midnight while still BEING the 9th's evening slot. This is the
  // same defect class as run 2026-10-17T18:16:53Z (a dispatch that dated itself
  // off the clock and pushed the 18th's digest 19 hours early), one hop further
  // up: here the WORKER's own escalation supplies the date it dispatches with.
  const w = loadWorker();
  const TICK = Date.parse(TODAY + "T13:35:00Z");   // 19:20 NPT on the 9th
  const CLOCK = Date.parse(TODAY + "T18:20:00Z");  // 00:05 NPT on the 10th
  const CLOCK_DAY = "2026-10-10";                  // the NPT day the clock is on

  const harness = makeHarness({
    initial: {
      [artifactKey(TODAY, "evening")]: artifact({ date: TODAY, slot: "evening" }),
    },
    onPost: () => quotaResponse(),
  });
  const env = ntfyEnv(harness.kv, { GH_TOKEN: "dispatch-token" });

  // The clock is pinned on the OTHER side of midnight from the tick, so a date
  // taken from `Date.now` and a date taken from the tick cannot agree, and the
  // test can tell which one the dispatch used.
  const clock = fakeClock(CLOCK);
  try {
    await dispatcherTick(w.WORKER, w.CRON_DISPATCHER, TICK, env);
  } finally {
    clock.restore();
  }

  const posts = dispatchPosts(harness.net);
  assert.strictEqual(posts.length, 1,
    "the evening tick on a slot with a good artifact and a 429ing ntfy "
    + "dispatched nothing. dispatches=" + posts.length + " calls="
    + JSON.stringify(harness.net.calls.map((c) => c.method + " " + c.url)));
  const body = JSON.parse(posts[0].body);
  assert.strictEqual(body.inputs.digest_time, "evening");
  assert.strictEqual(body.inputs.slot_date, TODAY,
    "the escalation dated this dispatch " + JSON.stringify(body.inputs.slot_date)
    + " instead of the TICK's day (" + TODAY + "). The tick is what the cron "
    + "fired FOR; `Date.now` is only when the isolate happened to run, and the "
    + "receiving run would build the NEXT day's digest for this slot. inputs="
    + JSON.stringify(body.inputs));
  assert.notStrictEqual(body.inputs.slot_date, CLOCK_DAY,
    "the dispatch used the wall clock's Nepal day (" + CLOCK_DAY + ") rather "
    + "than the tick's (" + TODAY + "), which is the defect this test exists for");
});

test("a 429 is RECORDED as a quota failure, distinguishable from any other", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    env: { GH_TOKEN: "dispatch-token" },
    onPost: () => quotaResponse(),
  });
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.ntfy_failed, true);
  // The whole point of a distinct marker: "quota exhausted" and "ntfy returned
  // 500" call for DIFFERENT responses from the owner -- wait for midnight UTC
  // vs. go read a log. A bare http_status makes the owner do that triage.
  assert.strictEqual(entry.ntfy_quota_exhausted, true,
    "a 429 / 42908 daily-quota rejection did not record `ntfy_quota_exhausted`, "
    + "so the ledger cannot tell quota exhaustion from any other publish "
    + "failure. entry=" + JSON.stringify(entry));
  assert.strictEqual(entry.http_status, 429);
});

test("a NON-429 failure is NOT recorded as quota exhaustion", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    env: { GH_TOKEN: "dispatch-token" },
    onPost: () => ({ ok: false, status: 503, text: async () => "upstream down" }),
  });
  const entry = ledgerOf(r.kv).days[TODAY][SLOT];
  assert.strictEqual(entry.ntfy_failed, true, "a 503 must still be a verified failure");
  assert.ok(!entry.ntfy_quota_exhausted,
    "a plain 503 was recorded as a quota exhaustion, which would send the owner "
    + "waiting for midnight UTC for a quota that was never exhausted");
  assert.strictEqual(entry.http_status, 503);
});

test("a 429 on the FALLBACK ladder path escalates EXACTLY ONCE", async () => {
  const w = loadWorker();
  // The pre-existing dispatch on this path must not double up with the new
  // quota escalation: two dispatches = two Actions builds = two pushes from a
  // path whose whole purpose is exactly one.
  const r = await deliver(w, {
    initial: {},
    env: { GH_TOKEN: "dispatch-token" },
    onPost: (n, call) => {
      const isDispatch = String(call.url).indexOf("dispatches") >= 0;
      return isDispatch ? null : quotaResponse();
    },
  });
  assert.strictEqual(dispatchPosts(r.net).length, 1,
    "the fallback ladder dispatched " + dispatchPosts(r.net).length +
    " times for one slot. The pre-existing late-build dispatch and the new "
    + "quota escalation must be the SAME dispatch, not two.");
});

test("exactly ONE push lands when BOTH arms succeed (slot-id dedup)", async () => {
  // The guarantee the objective names. The Worker dispatched because IT was
  // 429ing; Actions then published successfully from its own IP. Both arms can
  // succeed in the same slot, so dedup must be enforced by OUR logic on BOTH
  // sides -- ntfy does not dedup server-side (measured: 10 POSTs -> 10
  // messages, HTTP 200 every time).
  //
  // The two arms must therefore agree on ONE slot identity. The Worker's is
  // `digestSequenceId(today, slot)`, carried as X-Sequence-Id. The Actions
  // publisher MUST carry the same value on the same header, or the slot has two
  // identities and nothing can dedup them.
  const w = loadWorker();
  const yml = fs.readFileSync(
    path.join(REPO_ROOT, ".github", "workflows", "daily-news.yml"), "utf8");
  const seq = w.digestSequenceId(TODAY, SLOT);
  assert.ok(/X-Sequence-Id/.test(yml),
    "the Actions publisher sends no X-Sequence-Id header, so a message it "
    + "publishes cannot be recognised as the same slot as the Worker's and "
    + "dedup between the two arms is impossible by construction.");
  // And it must be the SLOT identity, not a fresh random one, so the read-back
  // can actually match.
  const ntfyStep = /- name: Publish to ntfy \(primary\)[\s\S]*?\n {6}- name: /.exec(yml);
  assert.ok(ntfyStep, "cannot locate the Publish to ntfy (primary) step");
  assert.ok(/dmn-|sequence_id|SEQUENCE_ID/.test(ntfyStep[0]),
    "the Actions publisher does not derive a sequence_id from the slot, so it "
    + "cannot agree with the Worker's identity for the same slot.");
  assert.ok(seq.indexOf(TODAY) >= 0 && seq.indexOf(SLOT) >= 0,
    "the Worker's slot identity does not carry both the date and the slot: " + seq);
});

test("a successful Worker push dispatches NOTHING (no wasted runner minutes)", async () => {
  const w = loadWorker();
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    env: { GH_TOKEN: "dispatch-token" },
  });
  assert.strictEqual(r.outcome.pushed, true);
  assert.strictEqual(dispatchPosts(r.net).length, 0,
    "the digest published successfully from the Worker and still dispatched a "
    + "second build. That spends 20 runner minutes to produce a push the owner "
    + "already has, and risks the duplicate this fix is meant to prevent.");
});

test("the 429 escalation is SKIPPED when the slot is already on the topic", async () => {
  const w = loadWorker();
  // The read-back proves the digest IS on the topic. Dispatching a rebuild for
  // a slot that already reached the owner spends runner minutes for nothing.
  const seq = w.digestSequenceId(TODAY, SLOT);
  const r = await deliver(w, {
    initial: { [artifactKey()]: artifact() },
    env: { GH_TOKEN: "dispatch-token" },
    cache: [{ id: "aaa", time: (T0 - 60 * 1000) / 1000, sequence_id: seq }],
  });
  assert.strictEqual(dispatchPosts(r.net).length, 0,
    "a slot the read-back proved was already on the topic dispatched a rebuild");
  assert.strictEqual(r.outcome.deduped, true);
});