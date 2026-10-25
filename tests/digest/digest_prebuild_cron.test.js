"use strict";
/**
 * tests/digest/digest_prebuild_cron.test.js
 *
 * THE PUNCTUAL PREBUILD TRIGGER, EXECUTED.
 *
 * WHAT THIS IS ABOUT. The prebuild pipeline itself was never broken: ask for a
 * build 40 minutes early, seal the result, and the slot becomes a KV lookup. What
 * was broken is that the SCHEDULED firings that used to trigger it are 4.5-7 hours
 * late: measured across 96 daily-news.yml scheduled runs from 2026-08-29 to
 * 2026-10-12, mean 306.8 min late, max 708.3 min, with the step change dated to
 * 2026-08-29 rather than gradual. `workflow_dispatch` is not delayed -- ~2
 * seconds to run creation, measured across 90 dispatched runs, queue average
 * 0.00 min.
 *
 * So the Worker's own every-minute dispatcher asks for the prebuild, at 06:40
 * and 18:40 NPT, forty minutes before the 07:20 / 19:20 NPT digest slots. The
 * clock is the Worker's because Cloudflare's cron fires promptly and GitHub's
 * scheduler does not.
 *
 * WHAT THE PREBUILD ARM DIALS, NOW. daily-news.yml itself, with `phase:
 * prebuild` (A, 2026-10-19). It used to dial digest-prebuild.yml, whose first job
 * then dialled daily-news.yml and waited for it -- a second job billed for the
 * wait, and one 45-minute watch per slot. The build run knows its OWN run id, so
 * as its last act, and only on success, it hands the seal that id: nothing
 * watches anything, and the seal is dialled by the build. Two consequences this
 * file now pins: both arms share ONE endpoint, so every count below is filtered
 * by `phase` and not by URL; and `trigger_source: prebuild` travels with the
 * dispatch, because the build branches on it.
 *
 * WHAT IS ASSERTED, AND WHY EACH ONE COULD PASS BY DOING NOTHING.
 *
 *   1. THE HAPPY PATH ACTUALLY DISPATCHES. Every guard below first asserts
 *      that a real POST to the real endpoint went out. This is not
 *      belt-and-braces: a dispatch test whose fixture omitted GH_TOKEN would
 *      count zero dispatches and then "prove" that zero dispatches harm the
 *      slot -- vacuously, and green. The first assertion in every test that
 *      makes a claim about the fallback is exactly that the prebuild dispatched.
 *
 *   2. THE FAIL-SAFE IS THE POINT. A prebuild that fails, or one still running
 *      when the slot arrives, must leave NOTHING that could suppress or delay
 *      the slot. The worst case is designed to be "one push ~22 min late", which
 *      is the behaviour this repository already had -- never "no news". So:
 *      no KV write, no delivery marker, no ledger entry, and no push of its own.
 *
 *   3. THE CRON -> NPT ARITHMETIC IS DERIVED, NOT RESTATED. The prebuild instant
 *      is `digest slot - PREBUILD_LEAD_MINUTES`, computed from the same
 *      SCHED_DIGEST_SLOTS the delivery slot uses, so the lead cannot drift away
 *      from the slot it exists to serve. tests/workflows/test_workflow_lint.py
 *      holds the Nepal Time (06:40 / 18:40 NPT) as a separate conversion.
 *
 * THE LOADER IS THE SAME ONE as digest_ntfy_types_and_dispatch_marker.test.js:
 * the committed Worker source, `export default {` -> `const WORKER = {`,
 * re-exported through a module shim, loaded from a unique temp path so no state
 * leaks between tests. Honesty: if any name below stops existing in the Worker,
 * the loader throws rather than quietly testing nothing.
 *
 * RUN:
 *     node --test tests/digest/digest_prebuild_cron.test.js
 *
 * REPRODUCING A RED WITHOUT TOUCHING THE COMMITTED WORKER (this is how the
 * mutation proofs are run -- against a COPY, never the real tree):
 *     DIGEST_PREBUILD_WORKER=<path> node --test <this file>
 * (the override prints a NOTE, so it can never be mistaken for CI)
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const COMMITTED_WORKER = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.DIGEST_PREBUILD_WORKER || COMMITTED_WORKER;

if (process.env.DIGEST_PREBUILD_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (DIGEST_PREBUILD_WORKER) INSTEAD of " +
    "the committed " + COMMITTED_WORKER + ".\n");
}

const EXPORT_NAMES = [
  "WORKER", "deliverDigest", "dispatchPrebuild", "dispatchDigest",
  "githubWorkflowDispatch", "scheduledJobs", "planTickFor", "prebuildUtcFor",
  "prebuildCronExpr", "planTickFor", "SCHED_DIGEST_SLOTS", "REFIRE_GAP_MINUTES",
  "REFIRE_ATTEMPTS", "PREBUILD_LEAD_MINUTES", "CRON_PREBUILD_MORNING",
  "CRON_PREBUILD_EVENING", "PREBUILD_DISPATCH_URL", "DIGEST_DISPATCH_URL",
  "CRON_DISPATCHER", "DIGEST_LEDGER_KEY", "digestScheduledInstant",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " -- this harness cannot " +
      "reach the Worker's entry points, so it would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-prebuild-cron-"));
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

// ---- fixtures ---------------------------------------------------------------

const MIN = 60 * 1000;

/** The morning digest SLOT instant: 2026-10-09T01:35:00Z = 07:20 NPT. */
const SLOT_UTC = Date.parse("2026-10-09T01:35:00Z");
/** The morning PREBUILD instant: 2026-10-09T00:55:00Z = 06:40 NPT. */
const PREBUILD_UTC = Date.parse("2026-10-09T00:55:00Z");
const TODAY = "2026-10-09";
const SLOT = "morning";
const EVENING_SLOT_UTC = Date.parse("2026-10-09T13:35:00Z");   // 19:20 NPT
const EVENING_PREBUILD_UTC = Date.parse("2026-10-09T12:55:00Z"); // 18:40 NPT

const NTFY_SERVER = "https://ntfy.example.invalid";
const TOPIC = "dmn-secret-topic-7f3a91c4";
const ARCHIVE_PAGE =
  "https://daily-digest.example.pages.dev/digests/2026-10-09-morning.html";
const GH = "dispatch-token";

function ntfyEnv(kv, extra) {
  return Object.assign({
    KV: kv,
    NTFY_SERVER: NTFY_SERVER,
    NTFY_TOPIC: TOPIC,
    WORKER_BUILD: "testbuild",
  }, extra || {});
}

/**
 * A fake KV over a caller-supplied store. The prebuild arm is asserted to make
 * ZERO calls on this -- one `get` and one `put` is already a fail-safe defect,
 * so the counter is the measurement, not a convenience.
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
      if (u.indexOf("/dispatches") >= 0) {
        // GitHub returns 204 on an accepted workflow_dispatch. `refuseDispatch`
        // models the dispatch being REJECTED, which is what a revoked PAT and
        // a missing Actions permission both look like from here.
        if (o.refuseDispatch) {
          return { ok: false, status: 403, text: async () => JSON.stringify({}) };
        }
        return { ok: true, status: 204, text: async () => "" };
      }
      // Anything else that POSTs is ntfy.
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ id: "ntfy-msg" }) };
    }
    // GET: the sequence_id read-back. Empty cache = nothing reached the topic.
    return { ok: true, status: 200, text: async () => JSON.stringify(o.cache || []) };
  };
  return { kv: kv, net: net, kvCalls: calls };
}

function postsTo(net, needle) {
  return net.calls.filter((c) => c.method === "POST" &&
    String(c.url).indexOf(needle) >= 0);
}
// A. THE DISCRIMINATOR IS THE PAYLOAD, NOT THE URL. The prebuild arm now dials
// daily-news.yml itself (`phase: prebuild`) rather than digest-prebuild.yml, so a
// URL filter would either find nothing or, worse, count the DELIVERY arm's
// dispatches as prebuild ones. `phase` is what makes a dispatch a prebuild.
function prebuildPosts(net) {
  return dailyNewsPosts(net).filter((c) => {
    let body = {};
    try { body = JSON.parse(c.body || "{}"); } catch (e) { return false; }
    return ((body.inputs || {}).phase) === "prebuild";
  });
}
function dailyNewsPosts(net) {
  return postsTo(net, "daily-news.yml/dispatches");
}
// A. ...AND ITS COMPLEMENT. Now that one endpoint serves both arms, a test that
// means "the BUILD ran" must exclude the prebuild dispatch, or the prebuild gets
// counted as evidence that the fallback ladder fired.
function deliveryPosts(net) {
  return dailyNewsPosts(net).filter((c) => {
    let body = {};
    try { body = JSON.parse(c.body || "{}"); } catch (e) { return false; }
    return ((body.inputs || {}).phase) !== "prebuild";
  });
}
function ntfyPosts(net) {
  return net.calls.filter((c) => c.method === "POST" &&
    String(c.url).indexOf("/dispatches") < 0);
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
    restore() { console.log = real.log; console.error = real.error; console.warn = real.warn; },
  };
}

/** Run one dispatcher tick, and return what it decided and what it did. */
async function tick(w, hourUtc, minuteUtc, env) {
  const ctx = makeCtx();
  const logs = captureLogs();
  let thrown = null;
  try {
    await w.scheduledJobs(w.planTickFor(hourUtc, minuteUtc), env, ctx,
                          w.CRON_DISPATCHER);
    await ctx.drain();
  } catch (err) {
    thrown = err;
  } finally {
    logs.restore();
  }
  return { thrown: thrown, logs: logs };
}

// ==========================================================================
// 1. THE CRON ARITHMETIC. Derived, and provable in both directions.
// ==========================================================================

test("the prebuild instant is exactly PREBUILD_LEAD_MINUTES before its slot",
  () => {
    const w = loadWorker();
    // 40 minutes, and the two real instants, cross-checked against each other so
    // a constant that disagreed with the crons could not pass.
    assert.strictEqual(w.PREBUILD_LEAD_MINUTES, 40,
      "the lead is a committed design choice; measured prebuild duration is " +
      "5-9 min on the four most recent runs and 22 min on one, so the headroom " +
      "is 40 and this constant is what both the crons and CI are held to");

    assert.strictEqual(SLOT_UTC - PREBUILD_UTC, 40 * MIN,
      "the committed morning instants are 40 minutes apart by hand: " +
      "01:35Z - 00:55Z. 00:55 UTC + 05:45 = 06:40 NPT and 01:35 UTC + 05:45 = " +
      "07:20 NPT.");
    assert.strictEqual(EVENING_SLOT_UTC - EVENING_PREBUILD_UTC, 40 * MIN,
      "the committed evening instants are 40 minutes apart by hand: " +
      "13:35Z - 12:55Z. 12:55 UTC + 05:45 = 18:40 NPT and 13:35 UTC + 05:45 = " +
      "19:20 NPT.");

    for (const slot of ["morning", "evening"]) {
      const pre = w.prebuildUtcFor(slot);
      assert.ok(pre, "prebuildUtcFor(" + slot + ") returned null, so the " +
        "Worker's own prebuild cannot be placed at all");
      const live = w.SCHED_DIGEST_SLOTS[slot];
      const lead = live.hour * 60 + live.minute - pre.minutesFromMidnight;
      assert.strictEqual(lead, w.PREBUILD_LEAD_MINUTES,
        "the Worker's prebuild for the " + slot + " slot is " +
        pre.minutesFromMidnight + " minutes from midnight, which is " + lead +
        " minutes before its slot -- not the " + w.PREBUILD_LEAD_MINUTES +
        " the crons and CI claim. The instant is COMPUTED from the slot, so " +
        "this can only fail if the lead constant and the arithmetic disagree.");
      assert.ok(pre.minutesFromMidnight >= 0,
        "the " + slot + " prebuild must fall on the SAME UTC day as its slot; " +
        "an instant that wrapped negative belongs to a different firing, which " +
        "is the mislabelled-archive defect the pipeline refuses to guess at");
    }

    assert.strictEqual(w.CRON_PREBUILD_MORNING, "55 0 * * *",
      "the morning prebuild cron is " + w.CRON_PREBUILD_MORNING +
      ", not `55 0 * * *`. 00:55 UTC is 06:40 NPT.");
    assert.strictEqual(w.CRON_PREBUILD_EVENING, "55 12 * * *",
      "the evening prebuild cron is " + w.CRON_PREBUILD_EVENING +
      ", not `55 12 * * *`. 12:55 UTC is 18:40 NPT.");
  });

test("the dispatcher plans the prebuild at 00:55/12:55 and the slot at 01:35/13:35",
  () => {
    const w = loadWorker();
    // 00:55 is divisible by neither the 2-minute nor the 3-minute sweep and is
    // not :17, so this tick is the prebuild and NOTHING ELSE -- which makes it
    // a clean measurement of the new job rather than a sweep we already had.
    assert.deepStrictEqual(w.planTickFor(0, 55), ["prebuild:morning"],
      "the 00:55 UTC tick should plan exactly the morning prebuild and no " +
      "other job, got " + JSON.stringify(w.planTickFor(0, 55)));
    assert.deepStrictEqual(w.planTickFor(12, 55), ["prebuild:evening"],
      "the 12:55 UTC tick should plan exactly the evening prebuild and no " +
      "other job, got " + JSON.stringify(w.planTickFor(12, 55)));

    // The slot tick is untouched: the prebuild is not a re-fire and did not
    // move the delivery instant.
    assert.deepStrictEqual(w.planTickFor(1, 35), ["digest:morning"],
      "the 01:35 UTC slot tick must still plan the delivery and nothing else. " +
      "Got " + JSON.stringify(w.planTickFor(1, 35)));
    assert.deepStrictEqual(w.planTickFor(13, 35), ["digest:evening"],
      "the 13:35 UTC slot tick must still plan the delivery and nothing else. " +
      "Got " + JSON.stringify(w.planTickFor(13, 35)));

    // The prebuild instant is NOT any instant the delivery path consults: not a
    // slot, and not a re-fire window entry. A prebuild sharing a tick with a
    // delivery would be two arms racing the same push.
    const refire = w.REFIRE_GAP_MINUTES * w.REFIRE_ATTEMPTS;
    for (const slot of ["morning", "evening"]) {
      const live = w.SCHED_DIGEST_SLOTS[slot];
      const pre = w.prebuildUtcFor(slot);
      assert.notStrictEqual(pre.minutesFromMidnight, live.hour * 60 + live.minute,
        "the " + slot + " prebuild and its slot resolve to the same instant");
      const offset = live.hour * 60 + live.minute - pre.minutesFromMidnight;
      assert.ok(offset > refire,
        "the " + slot + " prebuild is " + offset + " minutes before its slot, " +
        "so it must not fall inside the +" + refire + "-minute re-fire window; " +
        "a prebuild on a re-fire tick would dispatch from the same instant the " +
        "delivery does.");
    }
  });

// ==========================================================================
// 2. THE DISPATCH. With the vacuous-pass guard FIRST, in every test.
// ==========================================================================

test("the 00:55 tick dispatches the prebuild once: daily-news.yml, phase=prebuild",
  async () => {
    const w = loadWorker();
    const h = makeHarness({ initial: {} });
    const env = ntfyEnv(h.kv, { GH_TOKEN: GH });

    const r = await tick(w, 0, 55, env);
    assert.strictEqual(r.thrown, null, "the prebuild tick threw: " + r.thrown);

    // THE VACUOUS-PASS GUARD, FIRST. Everything below is a claim about what a
    // MISSED prebuild costs, and a harness that dispatched nothing would make
    // all of those claims vacuously true.
    const dispatched = prebuildPosts(h.net);
    assert.strictEqual(dispatched.length, 1,
      "the 00:55 tick must ACTUALLY dispatch the prebuild exactly once, or " +
      "every assertion below about what happens when it is missed is proving " +
      "nothing. It dispatched " + dispatched.length + " times. All POSTs: " +
      JSON.stringify(h.net.calls.map((c) => c.method + " " + c.url)));

    const body = JSON.parse(dispatched[0].body);
    assert.strictEqual(body.ref, "master");
    assert.strictEqual(body.inputs.digest_time, "morning",
      "the prebuild dispatch must name the slot explicitly: the build takes " +
      "`digest_time` and refuses to guess one from the clock. Got " +
      JSON.stringify(body.inputs));
    // The workflow_dispatch inputs this arm may send are exactly the four
    // daily-news.yml declares for a prebuild. An input it does not declare is
    // rejected by GitHub with HTTP 422, which would silently turn a working
    // trigger into a red prebuild every day.
    // `slot_date` is deliberately ABSENT here: this tick was fed no scheduled
    // instant, and the two tests further down pin both halves of that rule -- one
    // that a tick with no instant guesses nothing, one that a real cron tick,
    // which always has an instant, sends the date.
    assert.deepStrictEqual(Object.keys(body.inputs).sort(),
      ["digest_time", "phase", "trigger_source"],
      "the prebuild dispatch carries `digest_time`, `phase: prebuild` and " +
      "`trigger_source: prebuild`, plus `slot_date` when the tick knows its own " +
      "instant -- the same inputs the deleted intermediary passed on. Sending " +
      JSON.stringify(Object.keys(body.inputs)) + " risks a 422 from GitHub, " +
      "which would look like a working trigger dispatching nothing.");
    // A. `phase` and `trigger_source` are load-bearing, not decoration: the build
    // branches on `phase` (a prebuild must never push) and records
    // `trigger_source`, so a prebuild that arrived as `manual`/`full` would be a
    // delivery-shaped run 40 minutes early.
    assert.strictEqual(body.inputs.phase, "prebuild");
    assert.strictEqual(body.inputs.trigger_source, "prebuild");

    assert.ok(String(dispatched[0].headers.Authorization || "").indexOf(GH) >= 0,
      "the dispatch must carry GH_TOKEN from the Worker secret; got headers " +
      JSON.stringify(dispatched[0].headers));
    assert.ok(/accepted/.test(r.logs.text()),
      "an accepted dispatch must be logged. Got: " + r.logs.text());
  });

test("the 12:55 tick dispatches the evening slot", async () => {
  const w = loadWorker();
  const h = makeHarness({ initial: {} });
  const r = await tick(w, 12, 55, ntfyEnv(h.kv, { GH_TOKEN: GH }));
  assert.strictEqual(r.thrown, null, "the prebuild tick threw: " + r.thrown);
  const dispatched = prebuildPosts(h.net);
  assert.strictEqual(dispatched.length, 1,
    "the 12:55 tick must ACTUALLY dispatch the evening prebuild exactly once. " +
    "It dispatched " + dispatched.length + " times.");
  assert.strictEqual(JSON.parse(dispatched[0].body).inputs.digest_time, "evening");
});

test("the prebuild arm reuses the ONE dispatch implementation and the ONE token",
  () => {
    const w = loadWorker();
    // Same PAT, same endpoint shape, same acceptance rule -- reached through the
    // shared helper, so there is no second place where the 401/403 diagnosis can
    // be wrong at 06:40.
    assert.ok(w.PREBUILD_DISPATCH_URL.indexOf(
      "/actions/workflows/daily-news.yml/dispatches") > 0,
      "the prebuild arm dispatches " + w.PREBUILD_DISPATCH_URL +
      ", which is not daily-news.yml's dispatch endpoint");
    // A. THE TWO ARMS SHARE ONE ENDPOINT NOW, AND THAT IS DELIBERATE. The prebuild
    // asks for the same workflow as the delivery arm -- the BUILD -- and differs
    // only in the inputs it sends, `phase: prebuild` against `phase: full`. What
    // used to distinguish them was an intermediary, and that intermediary existed
    // only to learn a run id the build now states itself.
    assert.strictEqual(w.PREBUILD_DISPATCH_URL, w.DIGEST_DISPATCH_URL,
      "the prebuild arm must target the BUILD (daily-news.yml), the same " +
      "endpoint the delivery arm uses: the seal is dialled by the build, not by " +
      "this Worker.");
    assert.ok(/actions\/workflows\/[A-Za-z0-9._-]+\/dispatches$/
      .test(w.PREBUILD_DISPATCH_URL),
      "the prebuild URL is not a workflow_dispatch endpoint: " +
      w.PREBUILD_DISPATCH_URL);
    // No second token: the arm reads env.GH_TOKEN and nothing else, which is
    // asserted by executing it below with only that secret present.
  });

test("dispatchPrebuild with NO GH_TOKEN refuses loudly and writes nothing",
  async () => {
    const w = loadWorker();
    const h = makeHarness({ initial: {} });
    const logs = captureLogs();
    let result = null;
    let thrown = null;
    try {
      result = await w.dispatchPrebuild(
        ntfyEnv(h.kv, { GH_TOKEN: "" }), "morning");
    } catch (err) {
      thrown = err;
    } finally {
      logs.restore();
    }
    assert.strictEqual(thrown, null,
      "a missing secret must not throw out of a cron job; it must return " +
      "false and be logged. Threw: " + thrown);
    assert.strictEqual(result, false,
      "a dispatch with no token cannot have been accepted");
    assert.ok(/GH_TOKEN/.test(logs.text()),
      "a missing token must be logged loudly -- a silently skipped prebuild is " +
      "exactly the failure that makes a punctuality fix invisible. Got: " +
      logs.text());
    assert.strictEqual(h.kvCalls.put.length, 0,
      "a refused prebuild must write nothing: " +
      JSON.stringify(h.kvCalls.put));
  });

// ==========================================================================
// 3. THE FAIL-SAFE. A missed or overrun prebuild degrades; it never blocks.
// ==========================================================================

test("the prebuild arm records NO delivery state; only the runtime quota's own keys",
  async () => {
    const w = loadWorker();
    const h = makeHarness({ initial: {} });
    const r = await tick(w, 0, 55, ntfyEnv(h.kv, { GH_TOKEN: GH }));
    assert.strictEqual(r.thrown, null, "the prebuild tick threw: " + r.thrown);

    // Guard: it dispatched. Otherwise "wrote nothing" is trivially true of a
    // tick that did nothing, which is not the claim.
    assert.strictEqual(prebuildPosts(h.net).length, 1,
      "the tick must actually dispatch, or 'wrote no KV' is vacuous");

    // THE RUNTIME QUOTA (2026-10-20) writes its OWN accounting keys (`budget:*`)
    // on this path, and reads them. The invariant asserted here is the DELIVERY
    // one: the prebuild must leave no state the delivery cron reads to gate a
    // push. `budget:*` is not read by any delivery path, so it is excluded.
    const quota = (p) => String(p.key || p).indexOf("budget:") === 0;
    assert.strictEqual(h.kvCalls.put.filter((p) => !quota(p)).length, 0,
      "the prebuild arm wrote a NON-quota KV key: " +
      JSON.stringify(h.kvCalls.put.map((p) => p.key)) + ". Any such key is " +
      "state a FAILED prebuild leaves behind, and the delivery cron's own " +
      "guards are the only things allowed to gate a push.");
    assert.strictEqual(h.kvCalls.get.filter((k) => !quota(k)).length, 0,
      "the prebuild arm read a NON-quota KV key: " +
      JSON.stringify(h.kvCalls.get) + ". Reading the delivery ledger here would " +
      "put a delivery decision on the prebuild's path.");
    assert.strictEqual(h.kv.store[w.DIGEST_LEDGER_KEY], undefined,
      "the prebuild arm must never create a delivery ledger entry. Got " +
      String(h.kv.store[w.DIGEST_LEDGER_KEY]));
  });

test("a REJECTED prebuild dispatch throws nothing and still leaves the slot intact",
  async () => {
    const w = loadWorker();
    // The dispatched workflow is REFUSED (HTTP 403 -- a revoked PAT, or Actions
    // permission withdrawn). This is the "prebuild fails" case.
    const h = makeHarness({ initial: {}, refuseDispatch: true });
    const r = await tick(w, 0, 55, ntfyEnv(h.kv, { GH_TOKEN: GH }));

    assert.strictEqual(r.thrown, null,
      "a refused dispatch must not propagate out of the cron job: a throw here " +
      "would surface as a failed invocation, which is a different signal from a " +
      "missed prebuild and tells the operator the wrong thing.");
    assert.strictEqual(prebuildPosts(h.net).length, 1,
      "the dispatch must actually have been ATTEMPTED, or the rest of this " +
      "test is asserting nothing about a refusal.");
    assert.ok(/REJECTED/.test(r.logs.text()),
      "a refused dispatch must be logged loudly. Got: " + r.logs.text());
    assert.ok(/fall back/i.test(r.logs.text()),
      "the refusal must say what happens next -- the slot falls back to building " +
      "the digest at its own cron -- so an operator reading the log is not left " +
      "believing the slot was covered. Got: " + r.logs.text());
    assert.strictEqual(h.kvCalls.put.filter(
      (p) => String(p.key).indexOf("budget:") !== 0).length, 0,
      "a REFUSED prebuild wrote a NON-quota KV key: " +
      JSON.stringify(h.kvCalls.put.map((p) => p.key)) +
      ". A failed prebuild leaving delivery state behind is how a failed " +
      "prebuild suppresses its own slot. (The runtime quota's own `budget:*` " +
      "keys are excluded: no delivery path reads them.)");
  });

test("a missed prebuild leaves the slot to the EXISTING fallback ladder",
  async () => {
    const w = loadWorker();
    // The whole point of the 40-minute lead, executed end to end:
    //   tick 1 (00:55) : the prebuild dispatch is REFUSED -- nothing sealed
    //   tick 2 (01:35) : the digest cron finds no artifact, so the pre-existing
    //                    ladder pushes AND escalatesToActions dispatches a full
    //                    build of daily-news.yml
    // The outcome the owner must still get is a push. One push, and a build on
    // its way, delivered about 22 minutes late.
    const h = makeHarness({ initial: {}, refuseDispatch: true });
    const env = ntfyEnv(h.kv, { GH_TOKEN: GH });

    const pre = await tick(w, 0, 55, env);
    assert.strictEqual(pre.thrown, null, "the prebuild tick threw: " + pre.thrown);
    assert.strictEqual(prebuildPosts(h.net).length, 1,
      "GUARD: the prebuild dispatch must have been attempted, or this test is " +
      "not measuring the consequence of a MISSED prebuild.");
    assert.strictEqual(ntfyPosts(h.net).length, 0,
      "the prebuild arm must not push anything itself; the single push belongs " +
      "to the digest cron. It pushed " + ntfyPosts(h.net).length + " time(s).");

    const ledger = h.kv.store[w.DIGEST_LEDGER_KEY];
    assert.strictEqual(ledger, undefined,
      "after a failed prebuild there must be no ledger at all, so nothing can " +
      "suppress the slot's own delivery. Got " + String(ledger));

    // The slot, 40 minutes later, on the same harness.
    const logs = captureLogs();
    let outcome = null;
    let thrown = null;
    try {
      outcome = await w.deliverDigest(env, SLOT, {
        now: () => SLOT_UTC + MIN,
        sleep: async () => {},
      });
    } catch (err) {
      thrown = err;
    } finally {
      logs.restore();
    }
    assert.strictEqual(thrown, null,
      "the slot delivery threw after a missed prebuild: " + thrown);

    // THE PUSH HAPPENED. This is the acceptance criterion stated as an outcome.
    const pushed = ntfyPosts(h.net);
    assert.strictEqual(pushed.length, 1,
      "a missed prebuild must still cost the owner exactly ONE push, not zero. " +
      "The slot pushed " + pushed.length + " time(s). This is the whole " +
      "fail-safe: ~22 minutes late, never silent.");

    // And the real digest is already being built, so the late push is superseded
    // by the genuine article rather than being the last thing the owner hears.
    const built = deliveryPosts(h.net);
    assert.strictEqual(built.length, 1,
      "the fallback ladder must dispatch a full daily-news.yml build after a " +
      "missed prebuild, so the owner gets the real digest ~22 min late. It " +
      "dispatched " + built.length + " time(s).");
    assert.ok(outcome, "deliverDigest returned nothing");
  });

test("a sealed artifact still turns the slot into a lookup, not a build",
  async () => {
    // The happy path, and the reason the feature exists: with the prebuild
    // working, the slot pushes the sealed artifact and the fallback ladder never
    // runs. Without this the suite would only prove the degradation.
    const w = loadWorker();
    const artifactKey = "digest:artifact:" + TODAY + ":" + SLOT;
    const artifact = JSON.stringify({
      schema_version: 1, date: TODAY, slot: SLOT,
      built_at: new Date(PREBUILD_UTC + 6 * MIN).toISOString(),
      page_url: ARCHIVE_PAGE, is_partial: false,
      ntfy: { title: "Morning Digest - " + TODAY,
              body: "All sources reported.\n\n* [WORLD] Something happened today" },
    });
    const h = makeHarness({ initial: { [artifactKey]: artifact } });
    const env = ntfyEnv(h.kv, { GH_TOKEN: GH });

    const pre = await tick(w, 0, 55, env);
    assert.strictEqual(pre.thrown, null, "the prebuild tick threw: " + pre.thrown);
    assert.strictEqual(prebuildPosts(h.net).length, 1,
      "GUARD: the prebuild must actually dispatch, or the lookup below is not " +
      "being exercised against a real trigger.");

    const outcome = await w.deliverDigest(env, SLOT, {
      now: () => SLOT_UTC + MIN, sleep: async () => {},
    });
    const pushed = ntfyPosts(h.net);
    assert.strictEqual(pushed.length, 1,
      "a sealed prebuild must produce exactly ONE push at the slot");
    assert.strictEqual(deliveryPosts(h.net).length, 0,
      "with an artifact sealed, the fallback ladder must NOT dispatch a second " +
      "build: the whole value of the prebuild is that the slot is a KV lookup " +
      "instead of a 20-minute build.");
    const body = JSON.parse(pushed[0].body);
    assert.ok(body.message.indexOf("Something happened today") >= 0,
      "the push must carry the sealed digest's OWN message, not a fallback " +
      "notice. Got: " + JSON.stringify(body.message).slice(0, 200));
    assert.ok(body.title.indexOf("Morning Digest") === 0,
      "the push must be titled from the sealed artifact, got " +
      JSON.stringify(body.title));
    assert.ok(outcome, "deliverDigest returned nothing");
  });

test("the prebuild arm never pushes, so it cannot double-fire the slot",
  async () => {
    const w = loadWorker();
    const h = makeHarness({ initial: {} });
    const r = await tick(w, 0, 55, ntfyEnv(h.kv, { GH_TOKEN: GH }));
    assert.strictEqual(r.thrown, null, "the prebuild tick threw: " + r.thrown);
    assert.strictEqual(prebuildPosts(h.net).length, 1,
      "GUARD: the prebuild must actually dispatch, or 'never pushes' is " +
      "trivially true of a tick that did nothing.");
    assert.strictEqual(ntfyPosts(h.net).length, 0,
      "the prebuild arm POSTed to ntfy " + ntfyPosts(h.net).length +
      " time(s). It only asks for a prebuild; the prebuild only seals. The ONE " +
      "push belongs to the digest cron and its ladder.");
    // A. THE PREBUILD ARM NOW DISPATCHES THE BUILD ITSELF -- exactly once, as a
    // prebuild. What must never appear is a DELIVERY-shaped dispatch (`phase:
    // full`): that would build the same slot a second time, which is the
    // double-fire this test has always been about.
    assert.strictEqual(deliveryPosts(h.net).length, 0,
      "the prebuild arm dispatched " + deliveryPosts(h.net).length + " " +
      "delivery-shaped build(s). It may only ask for the PREBUILD " +
      "(phase=prebuild); a second, full build of the same slot is how a slot " +
      "gets built twice.");
    assert.strictEqual(
      JSON.parse(prebuildPosts(h.net)[0].body).inputs.phase, "prebuild");
  });

test("an UNPLANNED tick still runs the sweeps and never the prebuild",
  () => {
    const w = loadWorker();
    // The prebuild must not have leaked into the grid. 00:55 and 12:55 are the
    // only two additions; every other minute's plan is unchanged.
    const added = [];
    for (let h = 0; h < 24; h += 1) {
      for (let m = 0; m < 60; m += 1) {
        for (const job of w.planTickFor(h, m)) {
          if (String(job).indexOf("prebuild:") === 0) added.push(job + "@" +
            String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0"));
        }
      }
    }
    assert.deepStrictEqual(added.sort(),
      ["prebuild:evening@12:55", "prebuild:morning@00:55"],
      "the prebuild must fire on exactly two instants a day. It fired on " +
      JSON.stringify(added.sort()));
    // And no tick can plan a prebuild and a delivery at once.
    for (let h = 0; h < 24; h += 1) {
      for (let m = 0; m < 60; m += 1) {
        const jobs = w.planTickFor(h, m);
        const pre = jobs.filter((j) => String(j).indexOf("prebuild:") === 0);
        const dig = jobs.filter((j) => String(j).indexOf("digest:") === 0);
        if (pre.length && dig.length) {
          assert.fail("tick " + h + ":" + m + " plans a prebuild AND a delivery " +
            "(" + jobs.join(", ") + "). Two arms on one tick is a second push by " +
            "construction.");
        }
      }
    }
  });

test("the Worker's scheduled() entry point routes BOTH prebuild crons",
  async () => {
    // The legacy one-cron-per-job entry point, driven with the prebuild crons,
    // because that is how a single job is driven deterministically from a test
    // -- and because a cron that `planTickFor` produces but `scheduled()` does
    // not route is a job that fires and does nothing.
    const w = loadWorker();
    for (const [cron, slot] of [[w.CRON_PREBUILD_MORNING, "morning"],
                                [w.CRON_PREBUILD_EVENING, "evening"]]) {
      const h = makeHarness({ initial: {} });
      const ctx = makeCtx();
      let thrown = null;
      try {
        await w.WORKER.scheduled(
          { cron: cron, scheduledTime: PREBUILD_UTC },
          ntfyEnv(h.kv, { GH_TOKEN: GH }), ctx);
        await ctx.drain();
      } catch (err) {
        thrown = err;
      }
      assert.strictEqual(thrown, null, "scheduled({cron:" + cron + "}) threw: " + thrown);
      const dispatched = prebuildPosts(h.net);
      assert.strictEqual(dispatched.length, 1,
        "scheduled({cron: " + cron + "}) dispatched " + dispatched.length +
        " time(s). A prebuild cron the entry point does not route is a job that " +
        "fires and does nothing.");
      assert.strictEqual(JSON.parse(dispatched[0].body).inputs.digest_time, slot);
    }
  });

test("a prebuild dispatch dates itself from the SCHEDULED instant, never the clock",
  async () => {
    // THE DEFECT THIS GUARDS, and why it is not the same as "use today".
    //
    // The prebuild fires 40 minutes BEFORE the slot it serves, so a late tick
    // runs on the WRONG side of Nepal midnight: at 00:40 NPT on the 6th the slot
    // being prepared is still the 5th's evening one. A date read off the clock
    // names the 6th, and the seal then files the digest under a day that was
    // never due -- the defect daily-news.yml's own dispatch path had.
    //
    // The instant the tick was SCHEDULED for is what identifies the slot, so it
    // supplies the date. Both instants below are in the past, so a reading of
    // `Date.now()` cannot be accidentally right.
    const w = loadWorker();
    const cases = [
      [Date.UTC(2026, 0, 5, 12, 55), "2026-01-05", "the punctual 18:40 NPT tick"],
      [Date.UTC(2025, 11, 31, 18, 55), "2026-01-01", "a tick past NPT midnight"],
    ];
    for (const [ms, expected, label] of cases) {
      const h = makeHarness({ initial: {} });
      const ctx = makeCtx();
      let thrown = null;
      try {
        await w.WORKER.scheduled(
          { cron: w.CRON_PREBUILD_EVENING, scheduledTime: ms },
          ntfyEnv(h.kv, { GH_TOKEN: GH }), ctx);
        await ctx.drain();
      } catch (err) {
        thrown = err;
      }
      assert.strictEqual(thrown, null, label + ": scheduled() threw: " + thrown);
      const dispatched = prebuildPosts(h.net);
      assert.strictEqual(dispatched.length, 1,
        label + ": dispatched " + dispatched.length + " time(s)");
      const inputs = JSON.parse(dispatched[0].body).inputs;
      assert.strictEqual(inputs.digest_time, "evening", label);
      assert.strictEqual(inputs.slot_date, expected,
        label + ": the dispatch carried slot_date " +
        JSON.stringify(inputs.slot_date) + ", expected " + expected + ". The date " +
        "must come from the scheduled instant; a clock reading names a day this " +
        "slot was never due, and the seal then files the digest under it.");
    }
  });

test("a prebuild dispatch with NO scheduled instant OMITS slot_date, guessing nothing",
  async () => {
    // `JSON.stringify` drops `undefined`, so the key is absent rather than
    // invented -- and digest-prebuild.yml then dates the slot from its OWN
    // firing, which is the same answer reached the same way. A `Date.now()`
    // fallback here could only ever be approximately right.
    const w = loadWorker();
    const h = makeHarness({ initial: {} });
    let thrown = null;
    try {
      await w.dispatchPrebuild(ntfyEnv(h.kv, { GH_TOKEN: GH }), "morning");
    } catch (err) {
      thrown = err;
    }
    assert.strictEqual(thrown, null, "dispatchPrebuild threw: " + thrown);
    const dispatched = prebuildPosts(h.net);
    assert.strictEqual(dispatched.length, 1,
      "the dispatch did not happen. POSTs were: " +
      JSON.stringify(h.net.calls.map((c) => c.url)));
    const inputs = JSON.parse(dispatched[0].body).inputs;
    assert.strictEqual(inputs.digest_time, "morning");
    assert.strictEqual(
      Object.prototype.hasOwnProperty.call(inputs, "slot_date"), false,
      "slot_date was sent as " + JSON.stringify(inputs.slot_date) + " with no " +
      "scheduled instant to derive it from. It must be omitted so the workflow " +
      "can date the slot from its own firing.");
  });

// ==========================================================================
// 4. THE FIRING CENSUS. THE TEST WHOSE ABSENCE LET THE BLOCKER THROUGH.
// ==========================================================================

test("THE REPO CANNOT PRODUCE MORE THAN ONE PREBUILD FIRING PER SLOT",
  () => {
    // THE DEFECT THIS EXISTS TO MAKE IMPOSSIBLE.
    //
    // The prebuild fix ADDED the Worker's punctual dispatch at 00:55/12:55 UTC
    // but LEFT digest-prebuild.yml's four GitHub crons (`5 1`, `20 1`, `5 13`,
    // `20 13`) in place. Two sources, three firings per slot, six workflow runs
    // a day, all racing on one Pages deploy and one
    // `digest:artifact:<date>:<slot>` KV key -- and on a late day a ~12h-delayed
    // cron overwriting an ALREADY-DELIVERED slot's sealed artifact, which
    // neither stale_date nor ARTIFACT_MAX_BUILD_AGE_S can catch because the date
    // still matches.
    //
    // WHY THE PREVIOUS TESTS DID NOT CATCH IT. Every test above counts the
    // WORKER's firings, and test_the_prebuild_workflow_declares_NO_schedule_trigger
    // in tests/workflows/test_digest_prebuild.py counts the WORKFLOW's. Neither
    // ever added the two together, so a change that left both intact was green
    // in both suites while the slot was fired three times. A census is only a
    // census if it reads EVERY source, so this one enumerates all three:
    //
    //   1. the WORKER's dispatcher, over a real simulated UTC day;
    //   2. the WORKFLOW's own `on.schedule` block, read as YAML-ish text;
    //   3. any OTHER workflow in .github/workflows that dispatches
    //      digest-prebuild.yml, which would be a third source nobody declared.
    //
    // The total must be exactly ONE per slot. Not "at least one", not "one
    // unless another is found" -- exactly one, summed across every source.
    const w = loadWorker();

    // -- source 1: the Worker dispatcher, over a whole day ----------------
    const fromWorker = { morning: 0, evening: 0 };
    for (let h = 0; h < 24; h += 1) {
      for (let m = 0; m < 60; m += 1) {
        for (const job of w.planTickFor(h, m)) {
          const name = String(job);
          if (name.indexOf("prebuild:") !== 0) continue;
          const slot = name.slice("prebuild:".length);
          if (slot in fromWorker) fromWorker[slot] += 1;
          else assert.fail("the dispatcher planned an unknown prebuild job " +
            name + "; the census would silently ignore it and under-count.");
        }
      }
    }

    // -- source 2: digest-prebuild.yml's own `schedule:` block --------------
    // Read as text rather than with a YAML parser (this suite has no YAML
    // dependency), and read the `on:` BLOCK rather than the whole file, so a
    // cron mentioned in a comment or in a Python string cannot inflate the
    // count. `on:` is at column 0 and its keys are indented, so the block runs
    // until the next column-0 key.
    const wfPath = path.join(REPO_ROOT, ".github", "workflows",
      "digest-prebuild.yml");
    const wfText = fs.readFileSync(wfPath, "utf8");
    const lines = wfText.split(/\r?\n/);
    const onStart = lines.findIndex((l) => /^on:\s*$/.test(l));
    assert.ok(onStart >= 0,
      "digest-prebuild.yml has no top-level `on:` block at column 0. This " +
      "census could not find the trigger surface and would pass by reading " +
      "nothing.");
    const onBlock = [];
    for (let i = onStart + 1; i < lines.length; i += 1) {
      const line = lines[i];
      if (line.trim() === "" || line.trimStart().startsWith("#")) continue;
      if (/^\S/.test(line)) break;            // next top-level key: block ends
      onBlock.push(line);
    }
    const blockText = onBlock.join("\n");
    const fromWorkflow = { morning: 0, evening: 0 };
    const cronRe = /- cron:\s*'?([^'#]+?)'?\s*(#.*)?$/gm;
    let match;
    let workflowCronCount = 0;
    while ((match = cronRe.exec(blockText)) !== null) {
      workflowCronCount += 1;
      const fields = match[1].trim().split(/\s+/);
      if (fields.length !== 5) {
        assert.fail("digest-prebuild.yml declares a cron that is not a 5-field " +
          "expression: " + match[1] + ". The census cannot resolve it to a " +
          "slot and must not silently pass.");
      }
      // Nepal Time decides which half of the day the firing belongs to, the
      // same conversion the watchdog and the lint use (Nepal = UTC+05:45).
      const total = Number(fields[1]) * 60 + Number(fields[0]) + 345;
      const nptHour = Math.floor(total / 60) % 24;
      const slot = nptHour < 12 ? "morning" : "evening";
      fromWorkflow[slot] += 1;
    }
    assert.strictEqual(workflowCronCount, fromWorkflow.morning + fromWorkflow.evening,
      "the census counted " + workflowCronCount + " cron entries but attributed " +
      (fromWorkflow.morning + fromWorkflow.evening) + " of them to a slot. A " +
      "cron that resolves to neither half would be dropped from the total and " +
      "let a second firing through.");

    // -- source 3: any OTHER workflow that dispatches the prebuild ---------
    // A third dispatcher for the same workflow would be a third firing, and it
    // is the shape nobody writes down. Counted across every workflow file.
    const wfDir = path.join(REPO_ROOT, ".github", "workflows");
    const others = [];
    for (const name of fs.readdirSync(wfDir).sort()) {
      if (!/\.ya?ml$/i.test(name)) continue;
      if (name === "digest-prebuild.yml") continue;
      const text = fs.readFileSync(path.join(wfDir, name), "utf8");
      if (text.indexOf("digest-prebuild.yml") < 0) continue;
      // Ignore the file's own comments and heredoc prose: only an actual
      // dispatch call counts, and the repository writes the workflow name
      // into several explanations on purpose.
      const code = text.split(/\r?\n/)
        .filter((l) => !/^\s*#/.test(l))
        .join("\n");
      if (/workflows\/digest-prebuild\.yml\/dispatches/.test(code) ||
          /[- ]f\s+?ref=[^\s]*\s*digest-prebuild/.test(code) ||
          // A. ...AND THE OTHER DISPATCH IDIOM, which this census could not see
          // before: `gh workflow run <file>`. Closing that blind spot matters more
          // now than it did, because a second dispatcher must not be able to hide
          // behind a different command spelling.
          /gh\s+workflow\s+run\s+digest-prebuild\.yml/.test(code)) {
        // A. ONE NAMED EXCEPTION, WITH ITS REASON: daily-news.yml hands ITS OWN
        // finished run to the seal (`inputs[run_id]`). The seal reads one existing
        // run's artifact and cannot build, so it is not a firing source -- it is
        // the hand-off that replaced the watching dispatch job.
        const isSealHandoff = name === "daily-news.yml" &&
          /inputs\[run_id\]/.test(code);
        if (!isSealHandoff) others.push(name);
      }
    }

    // -- THE TOTAL, which is the whole point ------------------------------
    const report = [];
    for (const slot of ["morning", "evening"]) {
      const worker = fromWorker[slot];
      const workflow = fromWorkflow[slot];
      const total = worker + workflow + others.length;
      report.push(slot + ": worker=" + worker + " workflow-crons=" + workflow +
        " other-dispatchers=" + others.length + " TOTAL=" + total);
      assert.strictEqual(total, 1,
        "the " + slot + " prebuild can fire " + total + " times a day (" +
        report.join(" | ") + "). ONE slot gets ONE prebuild.\n" +
        "  * A second firing is a second full digest build and a second Pages\n" +
        "    deploy for the same slot.\n" +
        "  * `concurrency: digest-prebuild` serialises them; it does not remove\n" +
        "    them, so the loser's seal is whichever finished last.\n" +
        "  * On a late day a ~12h-delayed firing overwrites the ALREADY-\n" +
        "    DELIVERED `digest:artifact:<date>:<slot>` under a date that still\n" +
        "    matches, which neither stale_date nor ARTIFACT_MAX_BUILD_AGE_S=26h\n" +
        "    can catch.\n" +
        "  * The trigger is the Worker's punctual clock (GitHub-hosted SCHEDULED\n" +
        "    runs measured 4.5-7h late). Re-add a cron here and this goes red.");
    }
    assert.deepStrictEqual(others, [],
      "these other workflows dispatch digest-prebuild.yml: " +
      JSON.stringify(others) + ". A workflow that dispatches the prebuild is a " +
      "firing source, and it is the source nothing declares.");

    // -- NON-VACUITY, EXPLICIT --------------------------------------------
    // The census must have actually FOUND the one firing it blessed. A census
    // that summed zero would pass every assertion above -- the exact vacuous
    // pass that let a fixture without GH_TOKEN "prove" something -- so the one
    // firing is pinned to the committed instant, per slot.
    assert.strictEqual(fromWorker.morning, 1,
      "the Worker dispatched the morning prebuild " + fromWorker.morning +
      " times a day; the census is not reading the dispatcher at all.");
    assert.strictEqual(fromWorker.evening, 1,
      "the Worker dispatched the evening prebuild " + fromWorker.evening +
      " times a day; the census is not reading the dispatcher at all.");
    const pb = w.prebuildUtcFor("morning");
    assert.strictEqual(pb.hour, 0, "the morning prebuild is at " + pb.hour + ":" +
      pb.minute + " UTC, not the committed 00:55.");
    assert.strictEqual(pb.minute, 55, "the morning prebuild is at " + pb.hour + ":" +
      pb.minute + " UTC, not the committed 00:55.");
    console.log("prebuild firing census: " + report.join(" | ") +
      " (other dispatchers: " + (others.length ? others.join(", ") : "none") + ")");
  });

test("digestScheduledInstant still anchors punctuality on the PRIMARY slot",
  () => {
    // The prebuild must not have been adopted as the punctuality anchor. A
    // digest delivered at the slot instant is punctual; one delivered late is
    // late, and reporting a late push as on time because the prebuild ran early
    // is precisely how this whole module starts telling good news about a bad
    // delivery.
    const w = loadWorker();
    const instant = w.digestScheduledInstant(TODAY, "morning");
    assert.strictEqual(instant, SLOT_UTC,
      "the punctuality anchor is " + instant + ", not the 01:35 UTC slot " +
      SLOT_UTC + ". Anchoring on the prebuild would round every late " +
      "delivery down to 'on time'.");
    assert.strictEqual(w.digestScheduledInstant(TODAY, "evening"),
      EVENING_SLOT_UTC,
      "the evening punctuality anchor moved off the 13:35 UTC slot");
  });