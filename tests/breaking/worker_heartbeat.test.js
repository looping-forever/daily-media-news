"use strict";
/**
 * tests/breaking/worker_heartbeat.test.js
 *
 * THE DEAD-MAN'S SWITCH, EXECUTED. Node's own test runner, the Worker's own
 * functions, a fake KV and a fake fetch. No reimplementation of the logic under
 * test anywhere in this file: a proxy is exactly what let a real divergence
 * survive a "parity" test before.
 *
 * WHY IT IS JS AND NOT PYTHON. Four of the behaviours here are only observable
 * by running the Worker's code: which branch writes the key, how many times a
 * throttle suppresses a write, whether an alert is sent, and whether a KV
 * failure pages anyone. Python would have to reach into the JavaScript to check
 * any of it, and a Python re-implementation would agree with itself forever.
 *
 * WHERE IT LIVES AND WHY. tests/breaking/ is where the breaking path's Worker
 * tests already live (tests/breaking/test_worker_gate_parity.py drives this
 * same deploy/cloudflare-worker.js from Python). A new top-level tests/worker/
 * package would have split one Worker's coverage across two trees.
 *
 * RUN:
 *     node --test tests/breaking/worker_heartbeat.test.js
 *     python -m unittest tests.breaking.test_worker_heartbeat -v     (wrapper,
 *       used by CI, which fails LOUDLY rather than skipping when node is absent)
 *
 * REPRODUCING THE RED. Set WORKER_HEARTBEAT_WORKER to the path of a different
 * cloudflare-worker.js and every test in this file judges THAT file instead of
 * the committed one. A mutant tree can be generated anywhere outside the repo;
 * this env var is how they are re-run in one command. The override is
 * printed as a NOTE, so it can never be mistaken for CI.
 *
 * WHAT EACH TEST IS HOLDING DOWN:
 *   1. the heartbeat is written from the CRON path, and NO manual probe can
 *      refresh it — otherwise `?run=1` would permanently mask a dead cron and
 *      the switch would report healthy while the thing it monitors is dead;
 *   2. the throttle is real: 480 sweeps/day cost 48 writes, not 480;
 *   3. checkHeartbeats alarms ONCE, then honours the 6-hour cooldown;
 *   4. a KV READ failure does not page the owner (a blip must not storm), but
 *      the other key is still judged;
 *   5. the hourly cron actually runs the check and nothing else;
 *   6. ?health=1 is machine-readable and carries the build stamp + crons.
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const COMMITTED_WORKER = path.join(
  __dirname, "..", "..", "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.WORKER_HEARTBEAT_WORKER || COMMITTED_WORKER;

if (process.env.WORKER_HEARTBEAT_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (WORKER_HEARTBEAT_WORKER) INSTEAD of " +
    "the committed " + COMMITTED_WORKER + ".\n");
}

/** Names the harness re-exports from the transformed Worker source. */
const EXPORT_NAMES = [
  "WORKER", "touchHeartbeat", "checkHeartbeats", "healthForEndpoint",
  "BREAKING_HEARTBEAT_KEY", "ACADEMIC_HEARTBEAT_KEY", "HEARTBEAT_THROTTLE_MS",
  "HEARTBEAT_STALE_MS", "HEALTH_ALERT_KEY", "ALERT_COOLDOWN_MS", "WORKER_BUILD",
  "CRON_BREAKING_SWEEP", "CRON_ACADEMIC_SWEEP", "CRON_HEALTHCHECK",
  // The ONE cron this Worker is deployed with, and the pure function that
  // decides each tick. Both are needed because the sweep frequencies are now
  // asserted as EFFECTIVE frequencies rather than as a list of strings.
  "CRON_DISPATCHER", "planTickFor",
];

const loadCounter = { n: 0 };

/**
 * Load the Worker's real source as a CommonJS module.
 *
 * The only edit is `export default {` -> `const WORKER = {`; everything else,
 * including every function under test, is the committed bytes. Each call gets a
 * unique temp path so node's require cache cannot leak module state between
 * tests — the equivalent of a cold isolate, which is what a Worker really is.
 */
function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " — the harness cannot " +
      "reach the Workers entry points, so this suite would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-heartbeat-"));
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

/** A fake KV that counts every read and write and can be told to fail either. */
function makeKV(initial, opts) {
  const o = opts || {};
  const store = Object.assign({}, initial || {});
  const kv = {
    store: store,
    puts: [],
    gets: [],
    async get(key) {
      kv.gets.push(key);
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
      store[key] = value;
    },
  };
  return kv;
}

function putKeys(kv, only) {
  return kv.puts
    .map((p) => p.key)
    .filter((k) => (only ? k === only : true));
}

function putCount(kv, key) {
  return kv.puts.filter((p) => p.key === key).length;
}

/** Set the clock, so "1 minute later" and "480 sweeps across a day" are real. */
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
    restore() {
      console.log = real.log;
      console.error = real.error;
      console.warn = real.warn;
    },
  };
}

/**
 * Stub the network. Feeds return an empty document (so no sweep ever trips and
 * nothing is ever dispatched — the heartbeat must be the ONLY thing that
 * writes), and ntfy POSTs are recorded for the alarm assertions.
 */
function stubNetwork() {
  const posts = [];
  globalThis.fetch = async function (url, init) {
    const method = (init && init.method) || "GET";
    if (String(method).toUpperCase() === "POST") {
      posts.push({ url: String(url), headers: (init && init.headers) || {},
                   body: init && init.body });
      return { ok: true, status: 200, text: async () => "" };
    }
    return { ok: true, status: 200, text: async () => "" };
  };
  return posts;
}

async function runScheduled(WORKER, cron, env) {
  const pending = [];
  await WORKER.scheduled({ cron: cron }, env, { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
}

function hbRecord(tsMs, build, cron) {
  return JSON.stringify({
    ts: tsMs,
    at: new Date(tsMs).toISOString(),
    build: build || "unset",
    cron: cron || "",
  });
}

const T0 = 1767225600000; // 2026-01-01T00:00:00Z, an arbitrary fixed instant
const MIN = 60 * 1000;
const HOUR = 60 * MIN;

// ==========================================================================
// 1. The placement: cron writes it, `?run=1` must NEVER refresh it
// ==========================================================================

test("the breaking heartbeat is written by the cron sweep, not by handle()", async () => {
  const w = loadWorker();
  const kv = makeKV();
  const posts = stubNetwork();
  const logs = captureLogs();
  try {
    await runScheduled(w.WORKER, w.CRON_BREAKING_SWEEP,
                       { KV: kv, GH_TOKEN: "test-token" });
  } finally {
    logs.restore();
  }
  assert.strictEqual(posts.length, 0, "sanity: an empty feed must not dispatch");
  assert.deepStrictEqual(putKeys(kv), [w.BREAKING_HEARTBEAT_KEY],
    "a */3 cron sweep must write EXACTLY the breaking heartbeat and nothing " +
    "else (got " + JSON.stringify(putKeys(kv)) + ")");
  const payload = JSON.parse(putKeys(kv, w.BREAKING_HEARTBEAT_KEY).length
    ? kv.puts[0].value : "{}");
  assert.strictEqual(typeof payload.ts, "number",
    "the heartbeat must carry a machine-comparable ts");
  assert.ok(Object.prototype.hasOwnProperty.call(payload, "build"),
    "every heartbeat payload must carry the build stamp, or a deployment from " +
    "a stale checkout is undetectable");
});

test("the academic heartbeat is written by the academic cron sweep", async () => {
  const w = loadWorker();
  const kv = makeKV();
  stubNetwork();
  const logs = captureLogs();
  try {
    await runScheduled(w.WORKER, w.CRON_ACADEMIC_SWEEP,
                       { KV: kv, GH_TOKEN: "test-token" });
  } finally {
    logs.restore();
  }
  assert.deepStrictEqual(putKeys(kv), [w.ACADEMIC_HEARTBEAT_KEY]);
});

test("?run=1 does NOT refresh a stale heartbeat — a manual probe must not fake liveness", async () => {
  const w = loadWorker();
  const staleTs = Date.now() - 3 * HOUR;
  const stale = hbRecord(staleTs);
  const kv = makeKV({ [w.BREAKING_HEARTBEAT_KEY]: stale });
  stubNetwork();
  const res = await w.WORKER.fetch(new Request("https://probe.invalid/?run=1"),
                                    { KV: kv, GH_TOKEN: "test-token" });
  assert.strictEqual(res.status, 200, "sanity: the probe must actually run");
  assert.strictEqual(kv.puts.length, 0,
    "the ?run=1 path wrote KV state: " + JSON.stringify(putKeys(kv)));
  assert.strictEqual(kv.store[w.BREAKING_HEARTBEAT_KEY], stale,
    "?run=1 REFRESHED the breaking heartbeat. One manual probe would then " +
    "permanently mask a dead cron: the dead-man's switch would report healthy " +
    "while the thing it monitors is dead. The heartbeat must be written from " +
    "the cron path in scheduled() and from nowhere else.");
});

test("?run_academic=1 does NOT refresh the academic heartbeat either", async () => {
  const w = loadWorker();
  const stale = hbRecord(Date.now() - 3 * HOUR);
  const kv = makeKV({ [w.ACADEMIC_HEARTBEAT_KEY]: stale });
  stubNetwork();
  const res = await w.WORKER.fetch(
    new Request("https://probe.invalid/?run_academic=1"),
    { KV: kv, GH_TOKEN: "test-token" });
  assert.strictEqual(res.status, 200);
  assert.strictEqual(kv.store[w.ACADEMIC_HEARTBEAT_KEY], stale,
    "?run_academic=1 refreshed the academic heartbeat; the same masking bug as " +
    "the breaking path.");
});

// ==========================================================================
// 2. The throttle, and the write budget it is what protects
// ==========================================================================

test("the throttle suppresses the second write 1 minute after the first", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const kv = makeKV();
  const env = { KV: kv, GH_TOKEN: "test-token" };
  try {
    const first = await w.touchHeartbeat(env, w.BREAKING_HEARTBEAT_KEY,
                                         w.CRON_BREAKING_SWEEP);
    assert.strictEqual(first, true, "the first sweep must write");
    clock.advance(MIN);
    const second = await w.touchHeartbeat(env, w.BREAKING_HEARTBEAT_KEY,
                                          w.CRON_BREAKING_SWEEP);
    assert.strictEqual(second, false,
      "a sweep 1 minute later wrote again. HEARTBEAT_THROTTLE_MS is 30 min, so " +
      "2 calls 1 minute apart must cost EXACTLY one write.");
    assert.strictEqual(putCount(kv, w.BREAKING_HEARTBEAT_KEY), 1);
  } finally {
    clock.restore();
  }
});

test("the throttle re-opens once the window has passed", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const kv = makeKV();
  const env = { KV: kv, GH_TOKEN: "test-token" };
  try {
    await w.touchHeartbeat(env, w.BREAKING_HEARTBEAT_KEY, "");
    clock.advance(31 * MIN);
    await w.touchHeartbeat(env, w.BREAKING_HEARTBEAT_KEY, "");
    assert.strictEqual(putCount(kv, w.BREAKING_HEARTBEAT_KEY), 2,
      "31 minutes is outside the 30-minute throttle, so the heartbeat must " +
      "refresh; a throttle that never re-opens would freeze the key and " +
      "manufacture a false alarm forever.");
  } finally {
    clock.restore();
  }
});

test("one day of sweeps costs 48 writes per cron, not 480/720 — the measured quota", async () => {
  const w = loadWorker();
  const cases = [
    { key: w.BREAKING_HEARTBEAT_KEY, cron: w.CRON_BREAKING_SWEEP, sweeps: 480,
      stepMinutes: 3 },
    { key: w.ACADEMIC_HEARTBEAT_KEY, cron: w.CRON_ACADEMIC_SWEEP, sweeps: 720,
      stepMinutes: 2 },
  ];
  for (const c of cases) {
    const clock = fakeClock(T0);
    const kv = makeKV();
    const env = { KV: kv, GH_TOKEN: "test-token" };
    try {
      for (let i = 0; i < c.sweeps; i++) {
        await w.touchHeartbeat(env, c.key, c.cron);
        clock.advance(c.stepMinutes * MIN);
      }
    } finally {
      clock.restore();
    }
    assert.strictEqual(putCount(kv, c.key), 48,
      c.sweeps + " " + c.cron + " sweeps over one day cost " +
      putCount(kv, c.key) + " writes. At the throttled rate this must be 48 " +
      "(one per 30 min). A sweep that writes every time is ~1,200 writes/day " +
      "against Cloudflare's ~1,000/day free tier — the exact quota exhaustion " +
      "that once killed this account's academic floor.");
  }
});

test("touchHeartbeat never throws, even when KV explodes", async () => {
  const w = loadWorker();
  const kv = makeKV({}, { failGet: ["breaking:heartbeat"],
                          failPut: ["breaking:heartbeat"] });
  const logs = captureLogs();
  try {
    const wrote = await w.touchHeartbeat({ KV: kv }, "breaking:heartbeat", "");
    assert.strictEqual(wrote, false);
  } finally {
    logs.restore();
  }
  assert.ok(logs.lines.some((l) => /heartbeat/.test(l)),
    "a swallowed KV failure must still be logged; silent is how liveness " +
    "telemetry rots.");
});

// ==========================================================================
// 3. The alarm fires once, then the cooldown holds
// ==========================================================================

function ntfyEnv(kv) {
  return { KV: kv, GH_TOKEN: "test-token", NTFY_TOPIC: "topic-under-test",
           NTFY_SERVER: "https://ntfy.example.invalid" };
}

function staleStore(nowMs, ageMs, withAlert) {
  const store = {};
  store["breaking:heartbeat"] = hbRecord(nowMs - ageMs, "abc1234", "*/3 * * * *");
  store["academic:heartbeat"] = hbRecord(nowMs - 60 * 1000, "abc1234", "*/2 * * * *");
  if (withAlert) store["ops:heartbeat_alert"] = hbRecord(nowMs - ageMs);
  return store;
}

test("checkHeartbeats alarms once, then honours the 6-hour cooldown", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const posts = stubNetwork();
  const logs = captureLogs();
  const kv = makeKV(staleStore(T0, 3 * HOUR, false));
  try {
    const first = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(first.alerted, true,
      "a 3-hour-old breaking heartbeat is past the 60-minute limit and must " +
      "alarm. " + JSON.stringify(first));
    assert.strictEqual(posts.length, 1, "exactly one ntfy POST, not a retry storm");
    assert.strictEqual(posts[0].url, "https://ntfy.example.invalid/topic-under-test");
    assert.strictEqual(posts[0].headers["Priority"], "5");
    assert.strictEqual(posts[0].headers["Tags"], "warning,skull");
    const body = String(posts[0].body);
    assert.ok(/180 min/.test(body),
      "the body must state how many minutes ago the last heartbeat was, got: " +
      body);
    assert.ok(/60 min/.test(body), "the body must state the staleness limit");
    assert.ok(/NO BREAKING ALERT CAN FIRE/.test(body),
      "the body must say what is actually broken, not just that something is");
    assert.ok(/Cron Triggers/.test(body),
      "the body must say where to look");
    assert.strictEqual(putCount(kv, w.HEALTH_ALERT_KEY), 1,
      "the alarm stamp must be written after the send, exactly once");

    // ---- the CONSEQUENCE-FIRST ordering, and the title that names it -------
    //
    // WHY THIS BLOCK EXISTS. Everything above pins the URL, the priority, the
    // tags and four body substrings, and ALL OF IT PASSED AGAINST THE OLD
    // BODY ORDER -- because the old body carried the same four substrings,
    // three lines further down. The dead-man's switch used to open with
    // "breaking-monitor dead-man's switch: 1 of 2 cron heartbeat(s) are
    // stale", which names a component and a count, and put the sentence that
    // says what the owner has actually LOST several lines later.
    //
    // The owner reads this on a phone, where the first line is all that shows
    // collapsed. So the consequence is now first, and the ordering itself is
    // asserted rather than the presence of the words: an assertion that only
    // searched for "NO BREAKING ALERT CAN FIRE" would pass on either order.
    const consequenceAt = body.indexOf("CONSEQUENCE:");
    const measurementAt = body.indexOf("dead-man's switch:");
    assert.ok(consequenceAt === 0,
      "the body must OPEN with the consequence block. The owner sees the " +
      "first line on his phone, so a component-and-count opener is a " +
      "notification that says nothing:\n" + body);
    assert.ok(measurementAt > consequenceAt,
      "the measurement must come AFTER the consequence, not before it:\n" +
      body);
    assert.ok(body.includes("NO BREAKING ALERT CAN FIRE"),
      "the consequence must be the one the owner loses by, not a paraphrase");
    // The per-cron detail and the where-to-look survive, below the lead.
    assert.ok(/silent for 180 min \(limit 60 min\)/.test(body),
      "the per-cron detail line must survive below the consequence: " + body);
    const detailAt = body.indexOf("silent for 180 min (limit 60 min)");
    const checkAt = body.indexOf("CHECK:");
    assert.ok(checkAt > detailAt,
      "CHECK must come last -- it is the operator's part:\n" + body);
    // The title names the consequence too, not just the silence.
    assert.strictEqual(posts[0].headers["Title"],
      "No alert can fire: breaking-monitor cron silent for 180 min",
      "the title must lead with what is broken. It used to read " +
      "'breaking-monitor cron silent: breaking-monitor cron silent for 180 " +
      "min' -- the same fact twice, in the wrong order, with the consequence " +
      "missing entirely.");

    clock.advance(HOUR);
    const during = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(during.alerted, false);
    assert.strictEqual(during.suppressed_by, "cooldown");
    assert.strictEqual(posts.length, 1,
      "the check re-alarmed 1 hour after the first page. The health check " +
      "runs HOURLY, so without the 6-hour cooldown a cron that stays dead " +
      "pages the owner 24x/day — an alarm that often is an alarm nobody reads.");

    clock.advance(5 * HOUR);
    const after = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(after.alerted, true,
      "6 hours after the last page the cooldown is over and a still-dead cron " +
      "must be reported again");
    assert.strictEqual(posts.length, 2);
  } finally {
    logs.restore();
    clock.restore();
  }
});

test("checkHeartbeats stays silent while the cron is alive", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const posts = stubNetwork();
  const kv = makeKV(staleStore(T0, 5 * MIN, false));
  try {
    const r = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(r.alerted, false);
    assert.deepStrictEqual(r.stale, []);
    assert.strictEqual(posts.length, 0,
      "a 5-minute-old heartbeat is HEALTHY. A switch that cries wolf gets " +
      "ignored, which is the same as having no switch.");
    assert.strictEqual(putCount(kv, w.HEALTH_ALERT_KEY), 0,
      "no alarm means no alarm stamp");
  } finally {
    clock.restore();
  }
});

test("a heartbeat key that was never written is treated as the worst case, not as fresh", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const posts = stubNetwork();
  const kv = makeKV({});
  try {
    const r = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(r.alerted, true,
      "absent keys mean the cron has never run since this build deployed. That " +
      "is the strongest possible signal and it must not read as healthy.");
    assert.ok(/NEVER RECORDED/.test(String(posts[0].body)));
    // The never-run case is the WORST case, so its title must say so rather
    // than borrowing the "silent for N min" phrasing, which would imply a
    // heartbeat once existed.
    assert.strictEqual(posts[0].headers["Title"],
      "No alert can fire: breaking-monitor cron has never run",
      "the never-run title must distinguish 'has never run' from 'silent for " +
      "N min' -- the second claims a heartbeat existed once, and the owner " +
      "would go looking for a cron that ran yesterday.");
    assert.ok(posts[0].headers["Title"].length <= 72,
      "the title is " + posts[0].headers["Title"].length + " chars; a phone " +
      "notification truncates well before that, and a clipped title is worse " +
      "than a short one: " + posts[0].headers["Title"]);
  } finally {
    clock.restore();
  }
});

// ==========================================================================
// 4. A KV READ failure must NOT page the owner
// ==========================================================================

test("a KV read failure does NOT alarm — a blip must not storm the owner", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const posts = stubNetwork();
  const logs = captureLogs();
  const kv = makeKV(staleStore(T0, 3 * HOUR, false),
                    { failGet: ["breaking:heartbeat"] });
  try {
    const r = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(posts.length, 0,
      "checkHeartbeats paged the owner because a KV READ failed. The read " +
      "failure means liveness is UNKNOWN, not bad: paging on a KV blip trains " +
      "the owner to ignore this arm, which is worse than not having one.");
    assert.ok(r.unreadable.indexOf("breaking:heartbeat") >= 0,
      "the unreadable key must be reported as unreadable, not as healthy");
    assert.ok(logs.lines.some((l) => /READ FAILED/.test(l) && /error/.test(l)),
      "a suppressed alarm must be logged LOUDLY (error level), got: " +
      JSON.stringify(logs.lines));
  } finally {
    logs.restore();
    clock.restore();
  }
});

test("a KV read failure on ONE key does not blind the other", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const posts = stubNetwork();
  const kv = makeKV(staleStore(T0, 3 * HOUR, false),
                    { failGet: ["breaking:heartbeat"] });
  try {
    const r = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(posts.length, 0);
    // Force the readable key to be stale too, so the failure of the other key
    // is shown not to short-circuit the whole check.
    kv.store["academic:heartbeat"] = hbRecord(T0 - 3 * HOUR);
    const r2 = await w.checkHeartbeats(ntfyEnv(kv));
    assert.strictEqual(r2.alerted, true,
      "with breaking unreadable but academic genuinely stale, the alarm must " +
      "still fire on the evidence that IS available");
    assert.ok(/academic/.test(String(posts[0].body)));
    assert.ok(!/breaking:heartbeat: silent/.test(String(posts[0].body)),
      "the unreadable key must not be reported as stale in the body: " +
      String(posts[0].body));
  } finally {
    clock.restore();
  }
});

// ==========================================================================
// 5. The health-check cron actually runs the check, and only the check
// ==========================================================================

test("the health-check cron runs checkHeartbeats and touches nothing else", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  const posts = stubNetwork();
  const kv = makeKV(staleStore(T0, 3 * HOUR, false));
  try {
    await runScheduled(w.WORKER, w.CRON_HEALTHCHECK, ntfyEnv(kv));
    assert.strictEqual(posts.length, 1, "the hourly tick must be what alarms");
    assert.strictEqual(putCount(kv, w.BREAKING_HEARTBEAT_KEY), 0,
      "the health tick must not refresh the very key it is judging");
    assert.strictEqual(putCount(kv, w.ACADEMIC_HEARTBEAT_KEY), 0);
  } finally {
    clock.restore();
  }
});

// ==========================================================================
// 6. ?health=1 — the endpoint the reverse arm and the drift canary both read
// ==========================================================================

test("?health=1 is machine-readable: build, both ages, and the crons", async () => {
  const w = loadWorker();
  const clock = fakeClock(T0);
  stubNetwork();
  const kv = makeKV(staleStore(T0, 7 * MIN, false));
  try {
    const res = await w.WORKER.fetch(new Request("https://probe.invalid/?health=1"),
                                      { KV: kv, GH_TOKEN: "t",
                                        WORKER_BUILD: "deadbee" });
    assert.strictEqual(res.status, 200);
    assert.strictEqual(res.headers.get("Content-Type"), "application/json");
    const body = JSON.parse(await res.text());
    assert.strictEqual(body.build, "deadbee",
      "the deploy-time stamp must win over the in-source default");
    assert.strictEqual(body.breaking_heartbeat_age_s, 7 * 60);
    assert.strictEqual(body.academic_heartbeat_age_s, 60);
    assert.strictEqual(body.breaking_heartbeat_unreadable, false);
    assert.strictEqual(body.stale_after_s, Math.floor(w.HEARTBEAT_STALE_MS / 1000));
    // DEFECT FIXED HERE. `crons` used to be built from the in-source CRON_*
    // constants, so it reported SEVEN crons while the Cloudflare API had
    // registered five — and that is exactly why the failed `40 1` / `40 13`
    // trigger writes looked like a success: a liveness check built from
    // in-source intent cannot fail when a deploy half-applies.
    //
    // The account then hit the Workers Free limit of 5 cron triggers per
    // account (code 10072), and the five crons were merged into ONE dispatcher.
    // `crons` is therefore the one deployed trigger and nothing else, because
    // drift-canary.yml compares this array against deploy/schedule.json's
    // crons as SETS: an extra entry here turns the deployed Worker's own report
    // into a DRIFT alarm on every canary run.
    assert.deepStrictEqual(body.crons, [w.CRON_DISPATCHER],
      "?health=1 must report the ONE deployed trigger and nothing else. "
      + "Reported: " + JSON.stringify(body.crons));
    assert.strictEqual(body.crons_verified_at_runtime, false,
      "?health=1 must state that it cannot query Cloudflare's registered "
      + "triggers at runtime. A Worker has no account token and cannot "
      + "self-authorise, so a list built from in-source constants presented "
      + "without that admission is the fiction this field used to be.");
    assert.ok(/cannot query its own schedules/i.test(String(body.crons_source)),
      "crons_source must say the limitation in the words an operator needs: "
      + JSON.stringify(body.crons_source));

    // THE SCHEDULE THE DISPATCHER REPRODUCES, asserted as EFFECTIVE
    // FREQUENCIES and not as a list of strings. A dispatcher on a five-minute
    // grid would still "report" all three crons while running the breaking
    // sweep a third as often, which is the regression the merge was forbidden
    // to cause. Counting the dispatcher's own decisions over a simulated day is
    // what makes that impossible to hide.
    const firesPerDay = (job) => {
      let n = 0;
      for (let h = 0; h < 24; h += 1) {
        for (let m = 0; m < 60; m += 1) {
          if (w.planTickFor(h, m).indexOf(job) >= 0) n += 1;
        }
      }
      return n;
    };
    const effective = [
      ["breaking", w.CRON_BREAKING_SWEEP, 480,
       "the breaking sweep must still fire 480 times a day (*/3)"],
      ["academic", w.CRON_ACADEMIC_SWEEP, 720,
       "the academic sweep must still fire 720 times a day (*/2)"],
      ["healthcheck", w.CRON_HEALTHCHECK, 24,
       "the dead-man's switch must still run 24 times a day (hourly, :17)"],
    ];
    const reportedSchedule = JSON.stringify(body.dispatch_schedule);
    for (const [job, cron, perDay, why] of effective) {
      assert.ok(reportedSchedule.indexOf(cron) >= 0,
        "the dispatch schedule ?health=1 reports does not contain " + cron +
        ": " + reportedSchedule);
      assert.strictEqual(firesPerDay(job), perDay,
        why + ". The merged dispatcher fires it " + firesPerDay(job) +
        " time(s) a day, so breaking detection has been WEAKENED — the one " +
        "thing this merge was not allowed to do.");
    }
  } finally {
    clock.restore();
  }
});

test("?health=1 reports build 'unset' when the deploy never stamped it", async () => {
  const w = loadWorker();
  stubNetwork();
  const kv = makeKV(staleStore(Date.now(), MIN, false));
  const res = await w.WORKER.fetch(new Request("https://probe.invalid/?health=1"),
                                    { KV: kv, GH_TOKEN: "t" });
  const body = JSON.parse(await res.text());
  assert.strictEqual(body.build, w.WORKER_BUILD);
  assert.strictEqual(body.build, "unset",
    "an unstamped deployment must SAY so; that is the drift the canary hunts");
});

test("?health=1 reports an unreadable key instead of a fabricated number", async () => {
  const w = loadWorker();
  stubNetwork();
  const kv = makeKV(staleStore(Date.now(), MIN, false),
                    { failGet: ["breaking:heartbeat"] });
  const res = await w.WORKER.fetch(new Request("https://probe.invalid/?health=1"),
                                    { KV: kv, GH_TOKEN: "t" });
  const body = JSON.parse(await res.text());
  assert.strictEqual(body.breaking_heartbeat_unreadable, true);
  assert.strictEqual(body.breaking_heartbeat_age_s, null,
    "a KV outage must surface as `unreadable`, never as a number that would " +
    "silently pass a >3600s staleness gate");
  assert.strictEqual(body.academic_heartbeat_unreadable, false);
});

test("the status page shows the build stamp and both heartbeat ages", async () => {
  const w = loadWorker();
  stubNetwork();
  const kv = makeKV(staleStore(Date.now(), 2 * MIN, false));
  const res = await w.WORKER.fetch(new Request("https://probe.invalid/"),
                                    { KV: kv, GH_TOKEN: "t" });
  const text = await res.text();
  assert.ok(/^build: /m.test(text), "the status page must carry the build stamp");
  assert.ok(/^breaking heartbeat: /m.test(text));
  assert.ok(/^academic heartbeat: /m.test(text));
});