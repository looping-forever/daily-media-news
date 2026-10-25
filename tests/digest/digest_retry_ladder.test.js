"use strict";
/**
 * tests/digest/digest_retry_ladder.test.js
 *
 * THE EMPTY-DIGEST RETRY LADDER, EXECUTED ON THE WORKER'S OWN CLOCK.
 *
 * WHAT MOVED. digest-watchdog.yml used to carry a second cron, `17,32,47 * * * *`,
 * running its `empty_digest_retry` job 72 times a day. Every one of those was a
 * billed GitHub job-minute whose only work was to read state/digest_retry.json and,
 * almost always, find nothing due -- the ladder's own gates are +15 then +45
 * minutes after an empty digest, so at most two retries exist per ladder. The
 * Worker already ticks every minute, so the same phase is now a branch of
 * planTickFor() and a check costs a KV read. This suite is the file the Worker's
 * own comment names (deploy/cloudflare-worker.js, above DIGEST_RETRY_PATH); it is
 * the REQUIRED test that fails if the Worker's constants and the workflow's stop
 * disagreeing.
 *
 * WHAT IS ASSERTED, AND WHY EACH ONE COULD PASS BY DOING NOTHING.
 *
 *   1. THE DUE TICK ACTUALLY DISPATCHES. Every claim about the ladder's effect
 *      first asserts that a real POST to digest-watchdog.yml went out. A dispatch
 *      test whose fixture omitted GH_TOKEN would count zero dispatches and then
 *      "prove" that zero dispatches are harmless -- vacuously, and green.
 *
 *   2. "I COULD NOT READ IT" IS NEVER "A RETRY IS DUE". A 404, a transport
 *      failure and a parse failure all return null, and null dispatches nothing.
 *      The opposite (a GitHub outage read as a pending retry) is a retry storm.
 *
 *   3. THE DEBOUNCE IS KEYED ON THE ATTEMPT. GitHub cron jitter can deliver the
 *      same tick twice; the key carries date:slot:attempt so rung 1, rung 2 and
 *      rung 3 each dial once and a re-delivery does nothing. A REJECTED dispatch
 *      writes NO key, so a rung is retried rather than lost.
 *
 *   4. THE CONSTANTS ARE DERIVED, NOT RESTATED. LADDER_MINUTES, CRON_DIGEST_LADDER
 *      and the plan are computed from SCHED_LADDER_PHASE + SCHED_LADDER_EVERY_MIN;
 *      tests/workflows/test_workflow_lint.py holds the agreement with
 *      deploy/schedule.json. A literal "17,32,47 * * * *" would re-enter the
 *      deployed-trigger set through Check 16's literal regex.
 *
 * THE LOADER IS THE SAME ONE the sibling suites use: the committed Worker source,
 * `export default {` -> `const WORKER = {`, re-exported through a module shim,
 * loaded from a unique temp path so no state leaks between tests. If any name
 * below stops existing in the Worker the loader throws rather than quietly
 * testing nothing.
 *
 * RUN:
 *     node --test tests/digest/digest_retry_ladder.test.js
 *
 * REPRODUCING A RED WITHOUT TOUCHING THE COMMITTED WORKER (this is how the
 * mutation proofs are run -- against a COPY, never the real tree):
 *     DIGEST_RETRY_LADDER_WORKER=<path> node --test <this file>
 * (the override prints a NOTE, so it can never be mistaken for CI)
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const COMMITTED_WORKER = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.DIGEST_RETRY_LADDER_WORKER || COMMITTED_WORKER;

if (process.env.DIGEST_RETRY_LADDER_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (DIGEST_RETRY_LADDER_WORKER) INSTEAD of " +
    "the committed " + COMMITTED_WORKER + ".\n");
}

const EXPORT_NAMES = [
  "WORKER", "scheduledJobs", "planTickFor", "legacyJobForCron",
  "dispatchSchedule", "runDigestLadder", "nptStampFromInstant",
  "fetchRetryFileJson", "LADDER_MINUTES", "CRON_DIGEST_LADDER",
  "JOB_DIGEST_LADDER", "DIGEST_RETRY_PATH", "DIGEST_LADDER_DEBOUNCE_PREFIX",
  "DIGEST_LADDER_DEBOUNCE_TTL_S", "WATCHDOG_DISPATCH_URL", "CRON_DISPATCHER",
  "SCHED_LADDER_EVERY_MIN", "SCHED_LADDER_PHASE",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " -- this harness cannot " +
      "reach the Worker's entry points, so it would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-retry-ladder-"));
  const file = path.join(dir, "worker-" + (loadCounter.n++) + ".cjs");
  fs.writeFileSync(
    file,
    src.replace("export default {", "const WORKER = {") +
      "\nmodule.exports = { " + EXPORT_NAMES.join(", ") + " };\n",
    "utf8");
  let mod;
  try {
    mod = require(file);
  } catch (err) {
    throw new Error(
      "loading the Worker harness failed: " + String((err && err.message) || err) +
      "\nThis suite re-exports [" + EXPORT_NAMES.join(", ") +
      "]; a missing name means the Worker no longer defines it and this suite " +
      "must be updated deliberately rather than silently.");
  }
  fs.rmSync(dir, { recursive: true, force: true });
  return mod;
}

const W = loadWorker();

// ---- fixtures ---------------------------------------------------------------

/** The morning digest SLOT instant: 2026-10-09T01:35:00Z = 07:20 NPT. The
 *  ladder tick this suite drives sits on it. */
const SLOT_UTC = Date.parse("2026-10-09T01:35:00Z");
const NOW_NPT = "2026-10-09 07:20";
const GH = "dispatch-token";

/** A committed retry file, as daily-news.yml writes it under TZ=Asia/Kathmandu. */
function retryRecord(over) {
  return Object.assign({
    slot: "morning",
    date: "2026-10-09",
    attempts: 0,
    max_attempts: 3,
    next_retry_at_npt: "2026-10-09 07:05",
  }, over || {});
}

/**
 * A fake KV and a fake fetch over a caller-supplied store. Both are counters, so
 * a test can assert that a stand-down made NO write and NO network call -- the
 * measurement is the point, not the convenience.
 */
function makeHarness(opts) {
  const o = opts || {};
  const store = Object.assign({}, o.initial || {});
  const calls = { get: [], put: [] };
  const kv = {
    store: store,
    async get(key) { calls.get.push(key); return store[key] === undefined ? null : store[key]; },
    async put(key, value) { calls.put.push({ key: key, value: value }); store[key] = value; },
  };
  const net = { calls: [] };
  globalThis.fetch = async function (url, init) {
    const method = String((init && init.method) || "GET").toUpperCase();
    const u = String(url);
    net.calls.push({
      method: method, url: u,
      headers: (init && init.headers) || {},
      body: init && init.body,
    });
    if (method === "POST") {
      if (o.refuseDispatch) {
        return { ok: false, status: 403, text: async () => JSON.stringify({}) };
      }
      return { ok: true, status: 204, text: async () => "" };
    }
    // GET is the contents read-back of state/digest_retry.json.
    if (o.retryFile === null) {
      return { ok: false, status: 404, text: async () => "" };
    }
    const rec = o.retryFile === undefined ? retryRecord() : o.retryFile;
    if (o.transportError) throw new Error("simulated transport failure");
    return { ok: true, status: 200, text: async () => JSON.stringify(rec) };
  };
  const env = { GH_TOKEN: o.ghToken === undefined ? GH : o.ghToken, KV: kv };
  return { env: env, kv: kv, net: net, kvCalls: calls };
}

function dispatchPosts(net) {
  return net.calls.filter((c) => c.method === "POST" &&
    String(c.url).indexOf("digest-watchdog.yml/dispatches") >= 0);
}

function bodyOf(post) {
  try { return JSON.parse(post.body || "{}"); } catch (e) { return {}; }
}

/** A ctx whose waitUntil promises are collected, so scheduledJobs' async work is
 *  awaited by the test rather than by a timer. */
function makeCtx() {
  const pending = [];
  return {
    pending: pending,
    waitUntil(p) { pending.push(Promise.resolve(p)); },
    async drain() { await Promise.all(pending); },
  };
}

// ---- the constants are the schedule -----------------------------------------

test("the ladder minutes are DERIVED from phase and every, not a literal", () => {
  assert.equal(W.SCHED_LADDER_PHASE, 17);
  assert.equal(W.SCHED_LADDER_EVERY_MIN, 15);
  assert.deepEqual(W.LADDER_MINUTES, [17, 32, 47],
    "the ladder ticks on :17/:32/:47 -- SCHED_LADDER_PHASE then +15 while < 60");
  assert.equal(W.CRON_DIGEST_LADDER, "17,32,47 * * * *");
  assert.notEqual(W.CRON_DIGEST_LADDER, W.CRON_DISPATCHER,
    "the ladder is NOT a deployed trigger: it rides the one `* * * * *`. A " +
    "`const CRON_DIGEST_LADDER = \"<literal>\"` would re-enter the trigger set.");
});

test("the ladder plans a job at :17/:32/:47 and nowhere else", () => {
  const at = (m) => W.planTickFor(4, m);
  assert.ok(at(17).includes(W.JOB_DIGEST_LADDER));
  assert.ok(at(32).includes(W.JOB_DIGEST_LADDER));
  assert.ok(at(47).includes(W.JOB_DIGEST_LADDER));
  for (const m of [0, 1, 16, 18, 31, 33, 46, 48, 59]) {
    assert.ok(!at(m).includes(W.JOB_DIGEST_LADDER),
      "minute :" + m + " must not plan a ladder tick");
  }
});

test("the ladder only ADDS work at :47; :17 and :32 were already busy", () => {
  // This is the whole basis of "72 job fires but only 24 new work ticks" in
  // deploy/schedule.json: :17 is already the health check and :32 already the
  // academic sweep, so only :47 changes the tick profile.
  const h = W.planTickFor(4, 17);
  assert.ok(h.includes(W.JOB_DIGEST_LADDER) && h.includes("healthcheck"));
  const a = W.planTickFor(4, 32);
  assert.ok(a.includes(W.JOB_DIGEST_LADDER) && a.includes("academic"));
  const only = W.planTickFor(4, 47);
  assert.deepEqual(only, [W.JOB_DIGEST_LADDER],
    ":47 was a no-op before and carries the ladder alone");
});

test("legacyJobForCron maps the ladder cron to the ladder job", () => {
  assert.equal(W.legacyJobForCron(W.CRON_DIGEST_LADDER), W.JOB_DIGEST_LADDER);
});

test("dispatchSchedule lists the ladder on its own computed cron", () => {
  const entry = W.dispatchSchedule().find((j) => j.job === W.JOB_DIGEST_LADDER);
  assert.ok(entry, "the Worker's declared job schedule must include the ladder");
  assert.equal(entry.cron, W.CRON_DIGEST_LADDER);
  assert.equal(entry.every_minutes, W.SCHED_LADDER_EVERY_MIN);
});

// ---- the due test and the debounce ------------------------------------------

test("not due yet: no dispatch and NO KV write", async () => {
  const h = makeHarness({ retryFile: retryRecord({
    next_retry_at_npt: "2026-10-09 07:35" }) });
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, false);
  assert.match(r.why, /not due/);
  assert.equal(dispatchPosts(h.net).length, 0);
  assert.equal(h.kvCalls.put.length, 0);
});

test("due on the first attempt: dispatches ladder_only=true AND writes the debounce key", async () => {
  const h = makeHarness();
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, true);
  assert.equal(r.ok, true);
  const posts = dispatchPosts(h.net);
  assert.equal(posts.length, 1, "exactly one dispatch to digest-watchdog.yml");
  assert.equal(bodyOf(posts[0]).inputs.ladder_only, "true",
    "the Worker asks for the retry job and NOTHING else");
  // The runtime quota (2026-10-20) writes its OWN keys beside the debounce -- the
  // freeze-cache probe and the month charge -- so the assertion is scoped to the
  // debounce key rather than to every put.
  const debouncePuts = h.kvCalls.put.filter(
    (p) => p.key.indexOf(W.DIGEST_LADDER_DEBOUNCE_PREFIX) === 0);
  assert.equal(debouncePuts.length, 1, "exactly one debounce key");
  assert.equal(debouncePuts[0].key,
    W.DIGEST_LADDER_DEBOUNCE_PREFIX + "2026-10-09:morning:0",
    "the key carries date:slot:attempt so a re-delivery of a rung is a no-op");
  assert.equal(W.DIGEST_LADDER_DEBOUNCE_TTL_S, 60 * 60 * 6);
  assert.equal(h.kv.store[debouncePuts[0].key], "2026-10-09T01:35:00Z");
});

test("already dialled: the debounce suppresses a SECOND dispatch", async () => {
  const key = W.DIGEST_LADDER_DEBOUNCE_PREFIX + "2026-10-09:morning:0";
  const h = makeHarness({ initial: { [key]: "2026-10-09T01:20:00.000Z" } });
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, false);
  assert.match(r.why, /already dialled/);
  assert.equal(dispatchPosts(h.net).length, 0);
  assert.equal(h.kvCalls.put.length, 0);
});

test("the key separates rungs: attempt 1 dials, attempt 2 dials again", async () => {
  const first = makeHarness({ retryFile: retryRecord({ attempts: 0 }) });
  await W.runDigestLadder(first.env, SLOT_UTC);
  const second = makeHarness({ retryFile: retryRecord({ attempts: 1 }) });
  await W.runDigestLadder(second.env, SLOT_UTC);
  assert.equal(dispatchPosts(first.net).length, 1);
  assert.equal(dispatchPosts(second.net).length, 1,
    "a second rung is a distinct debounce key, so it is not suppressed by the first");
});

test("an exhausted ladder dispatches nothing", async () => {
  const h = makeHarness({ retryFile: retryRecord({ attempts: 3, max_attempts: 3 }) });
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, false);
  assert.match(r.why, /exhausted/);
  assert.equal(dispatchPosts(h.net).length, 0);
  assert.equal(h.kvCalls.put.length, 0);
});

test("a rejected dispatch writes NO debounce key, so the rung is retried not lost", async () => {
  const h = makeHarness({ refuseDispatch: true });
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, true);
  assert.equal(r.ok, false);
  assert.equal(h.kvCalls.put.filter(
    (p) => p.key.indexOf(W.DIGEST_LADDER_DEBOUNCE_PREFIX) === 0).length, 0,
    "a 403 must leave the rung undialled so the next tick tries again");
});

// ---- "I could not read it" is never "a retry is due" -------------------------

test("a 404 retry file dispatches nothing", async () => {
  const h = makeHarness({ retryFile: null });
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, false);
  assert.equal(dispatchPosts(h.net).length, 0);
  assert.equal(h.kvCalls.put.length, 0);
});

test("a transport failure reading the file dispatches nothing", async () => {
  const h = makeHarness({ transportError: true });
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, false);
  assert.equal(dispatchPosts(h.net).length, 0);
});

test("a missing GH_TOKEN reads as no ladder, not as due", async () => {
  const h = makeHarness({ ghToken: "" });
  const r = await W.runDigestLadder(h.env, SLOT_UTC);
  assert.equal(r.ran, false);
  assert.equal(dispatchPosts(h.net).length, 0);
});

// ---- the NPT clock and the wiring -------------------------------------------

test("nptStampFromInstant is the same clock the retry file is written in", () => {
  assert.equal(W.nptStampFromInstant(SLOT_UTC), NOW_NPT,
    "01:35 UTC + 05:45 = 07:20 NPT, fixed width, so lexicographic compare is valid");
  // A ticket at 07:20 is strictly after a 07:05 stamp, so it is due.
  assert.equal(W.nptStampFromInstant(SLOT_UTC) > "2026-10-09 07:05", true);
});

test("scheduled() routes the ladder cron to runDigestLadder", async () => {
  // The legacy shape: a test drives ONE ladder tick from its own clock instead
  // of waiting for the grid. Same path the deployed dispatcher takes.
  const h = makeHarness();
  const ctx = makeCtx();
  await W.WORKER.scheduled(
    { cron: W.CRON_DIGEST_LADDER, scheduledTime: SLOT_UTC }, h.env, ctx);
  await ctx.drain();
  assert.equal(dispatchPosts(h.net).length, 1,
    "the legacy ladder cron must reach runDigestLadder and actually dispatch");
});

test("the retry file path is the one the workflow writer commits", () => {
  assert.equal(W.DIGEST_RETRY_PATH, "state/digest_retry.json");
  assert.match(W.WATCHDOG_DISPATCH_URL, /digest-watchdog\.yml\/dispatches$/);
});
