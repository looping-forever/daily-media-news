"use strict";
/**
 * tests/digest/digest_ntfy_types_and_dispatch_marker.test.js
 *
 * TWO CONFIRMED DEFECTS IN THE WORKER'S DIGEST-DELIVERY PATH, both executed.
 *
 *   DEFECT 1 -- THE DIGEST PUSH IS REJECTED BY ntfy WITH HTTP 400.
 *   `digestPayload` forwards whatever `priority`/`tags` it is handed straight
 *   into the JSON body. Every real call site passed STRING priority ("4", "5")
 *   and a STRING tags value ("newspaper", "warning,rotting_light"), while the
 *   one caller that passes natives -- `?deliver_test=1` at numeric 3 and [] --
 *   is covered by a passing test. So the only arm that had ever succeeded was
 *   the probe, and the arm the owner depends on had never once been accepted.
 *
 *   THE 400 IS MEASURED, NOT INFERRED. Posted to a throwaway ntfy topic on
 *   2026-10-12, replicating the Worker's exact wire shape (POST <server>/ with
 *   `topic` inside the JSON body):
 *
 *     {"priority":"4","tags":"newspaper"}     -> HTTP 400  code 40024
 *     {"priority":4,  "tags":"newspaper"}     -> HTTP 400  code 40024
 *     {"priority":"4","tags":["newspaper"]}   -> HTTP 400  code 40024
 *     {"priority":4,  "tags":["newspaper"]}   -> HTTP 200  (message accepted)
 *     {"priority":"4"}                        -> HTTP 400  (priority alone)
 *     {"tags":"newspaper"}                    -> HTTP 400  (tags alone)
 *     {}                                      -> HTTP 200  (baseline)
 *
 *   So BOTH fields are wrong, independently: ntfy takes `Priority` as a number
 *   and `Tags` as an array, and each one alone is enough for the 400. The
 *   ledger shows the consequence for every slot on 2026-10-10, -11 and -12:
 *   state "failed", http_status 400, attempts 3, ntfy_sent_at null.
 *
 *   DEFECT 2 -- THE ONCE-PER-SLOT DISPATCH MARKER ERASES ITS OWN MARKER.
 *   `escalateToActions` returns BOTH `dispatched` and `at`, and it deliberately
 *   returns `at: entry.actions_dispatch_at` when it stands down because the
 *   marker is already set. `pushFallbackLadder` took only `.dispatched`, threw
 *   `.at` away, and then wrote `actions_dispatch_at: dispatched ? ... : null`.
 *   On every stand-down -- which is every re-fire after the first -- that
 *   overwrote the existing timestamp with null. The next re-fire therefore saw
 *   an empty marker, dispatched again, and re-set it. Erase, then re-dispatch.
 *   Live evidence on the morning slot of 2026-10-12: ntfy ids nF1QhKGuDrCM
 *   (01:39:59Z), hIG1H3oyuM2C (01:50:13Z), fsr4kliFEco6 (02:01:35Z), all
 *   trigger_source "worker", with the ledger marker ending at 01:55:26Z -- the
 *   LAST fire, exactly as that cycle predicts.
 *
 * WHY A NEW FILE AND NOT A SECTION IN digest_delivery.test.js. Both bugs were
 * missed by a suite that is otherwise strong, and the reason is specific: that
 * suite exercises the ladder ONCE per test, so it can only ever see a
 * dispatch marker being SET. It cannot see one being ERASED, because erasure
 * is only observable across re-fires of the same slot against a persistent
 * ledger. "a 429 on the FALLBACK ladder path escalates EXACTLY ONCE" is green
 * for the wrong reason -- it fires once, so "exactly once" is unfalsifiable.
 *
 * THE LOADER IS THE SAME ONE, verbatim in shape: the committed Worker source,
 * `export default {` -> `const WORKER = {`, re-exported through a module shim,
 * loaded from a unique temp path so no state leaks between tests. Honesty: if
 * any name below stops existing in the Worker, the loader throws instead of
 * quietly testing nothing.
 *
 * RUN:
 *     node --test tests/digest/digest_ntfy_types_and_dispatch_marker.test.js
 *
 * REPRODUCING A RED WITHOUT TOUCHING THE COMMITTED WORKER:
 *     DIGEST_NTFY_TYPES_WORKER=<path> node --test <this file>
 * (the override prints a NOTE, so it can never be mistaken for CI)
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const COMMITTED_WORKER = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.DIGEST_NTFY_TYPES_WORKER || COMMITTED_WORKER;

if (process.env.DIGEST_NTFY_TYPES_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (DIGEST_NTFY_TYPES_WORKER) INSTEAD of " +
    "the committed " + COMMITTED_WORKER + ".\n");
}

const EXPORT_NAMES = [
  "WORKER", "deliverDigest", "deliverDigestPlan", "digestPayload",
  "pushFallbackLadder", "escalateToActions", "digestSequenceId",
  "digestFullSequenceId", "DIGEST_LEDGER_KEY",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " -- this harness cannot " +
      "reach the Worker's entry points, so it would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-ntfy-types-"));
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
      "loading the Worker harness failed: " + String(err && err.message || err) +
      "\nThis suite re-exports [" + EXPORT_NAMES.join(", ") +
      "]; a missing name means the Worker no longer defines it and this suite " +
      "must be updated deliberately rather than silently.");
  }
  fs.rmSync(dir, { recursive: true, force: true });
  return mod;
}

// ---- fixtures ---------------------------------------------------------------

const MIN = 60 * 1000;
/** 2026-10-09T01:36:00Z = 07:06 NPT. The morning slot was DUE at 01:35Z, so
 *  this clock is DELIVERY_ON_TIME_S past the scheduled instant. */
const T0 = Date.parse("2026-10-09T01:36:00Z");
const TODAY = "2026-10-09";
const SLOT = "morning";

const NTFY_SERVER = "https://ntfy.example.invalid";
const TOPIC = "dmn-secret-topic-7f3a91c4";
const ARCHIVE_PAGE =
  "https://daily-digest.example.pages.dev/digests/2026-10-09-morning.html";

function ntfyEnv(kv, extra) {
  return Object.assign({
    KV: kv,
    NTFY_SERVER: NTFY_SERVER,
    NTFY_TOPIC: TOPIC,
    WORKER_BUILD: "testbuild",
    // The archive host is a SECRET on the Worker now. No value here is a
    // fallback the Worker can lean on: it throws when this is absent.
    DIGEST_ARCHIVE_ORIGIN: new URL(ARCHIVE_PAGE).origin,
  }, extra || {});
}

function artifact() {
  return JSON.stringify({
    schema_version: 1,
    date: TODAY,
    slot: SLOT,
    built_at: new Date(T0 - 4 * MIN).toISOString(),
    page_url: ARCHIVE_PAGE,
    is_partial: false,
    ntfy: {
      title: "Morning Digest - 2026-10-09 - 12 items",
      body: "All sources reported.\n\n* [WORLD] Something happened today",
    },
  });
}

function artifactKey() { return "digest:artifact:" + TODAY + ":" + SLOT; }

/** A ledger whose `last_good` is an OLDER digest, so the ladder takes its
 *  `fallback_last_good` rung rather than the `unavailable` one. */
function ledgerWithLastGood() {
  return JSON.stringify({
    schema_version: 1,
    as_of: new Date(T0 - MIN).toISOString(),
    last_good: {
      date: "2026-10-08",
      slot: SLOT,
      page_url: "https://daily-digest.example.pages.dev/digests/2026-10-08-morning.html",
      title: "Morning Digest - 2026-10-08 - 9 items",
      body: "Yesterday's digest, which did build.",
      built_at: "2026-10-08T01:30:00Z",
      at: "2026-10-08T01:35:00Z",
    },
    days: {},
  });
}

/** A fake KV over a caller-supplied store, so SEVERAL deliveries can share one
 *  ledger. That sharing is the whole point of defect 2's test: a marker cannot
 *  be erased by a fire that cannot see it. */
function makeHarness(opts) {
  const o = opts || {};
  const store = Object.assign({}, o.initial || {});
  const events = [];
  const kv = {
    store: store,
    puts: [],
    async get(key) {
      events.push({ t: "kv.get", key: key });
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    async put(key, value) {
      kv.puts.push({ key: key, value: value });
      events.push({ t: "kv.put", key: key });
      store[key] = value;
    },
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
    events.push({ t: method === "POST" ? "http.post" : "http.get", url: u });
    if (method === "POST") {
      if (String(u).indexOf("/actions/workflows/daily-news.yml/dispatches") >= 0) {
        // A dispatch is accepted: HTTP 204, exactly what GitHub returns.
        return { ok: true, status: 204, text: async () => "" };
      }
      if (o.failNtfyPost) {
        // The production shape: ntfy refuses the body, so ntfy_sent_at stays
        // null and the next re-fire is not suppressed by the ledger guard.
        // That is how one bad day becomes three pushes instead of one.
        return o.failNtfyPost;
      }
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ id: "ntfy-msg" }) };
    }
    // GET. The only GET on this path is the sequence_id read-back, and an
    // empty cache is the honest default: nothing reached the topic.
    return { ok: true, status: 200, text: async () => JSON.stringify(o.cache || []) };
  };
  return { kv: kv, net: net, events: events };
}

function postCalls(net) {
  return net.calls.filter((c) => c.method === "POST");
}

function dispatchPosts(net) {
  return net.calls.filter((c) => c.method === "POST" &&
    String(c.url).indexOf("/actions/workflows/daily-news.yml/dispatches") >= 0);
}

function ledgerOf(kv) {
  const raw = kv.store["digest:ledger"];
  return raw ? JSON.parse(raw) : null;
}

function slotEntry(kv) {
  const l = ledgerOf(kv);
  return l && l.days && l.days[TODAY] ? l.days[TODAY][SLOT] : null;
}

/** ntfy's measured refusal: HTTP 400, code 40024. */
function ntfyRefused() {
  return { ok: false, status: 400,
           text: async () => JSON.stringify({
             code: 40024, http: 400,
             error: "invalid request: request body must be valid JSON",
           }) };
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
    restore() {
      console.log = real.log;
      console.error = real.error;
      console.warn = real.warn;
    },
  };
}

/** One delivery, on a harness that outlives the call. */
async function fireOnce(w, kv, options) {
  const o = options || {};
  const logs = captureLogs();
  let outcome = null;
  let thrown = null;
  try {
    outcome = await w.deliverDigest(ntfyEnv(kv, o.env), o.slot || SLOT, {
      now: () => (o.now === undefined ? T0 : o.now),
      sleep: async () => {},
    });
  } catch (err) {
    thrown = err;
  } finally {
    logs.restore();
  }
  return { outcome: outcome, thrown: thrown, logs: logs };
}

/** The ntfy POST bodies on a harness, parsed. Never the Worker's own return
 *  value: the claim under test is what went out ON THE WIRE. */
function ntfyBodies(net) {
  return postCalls(net)
    .filter((c) => String(c.url).indexOf("dispatches") < 0)
    .map((c) => JSON.parse(c.body));
}

// ==========================================================================
// 1. DEFECT 1. The wire types ntfy actually accepts
// ==========================================================================

/** The measured contract, restated as the assertion helper every test below
 *  uses. A string priority and a string tags value are each independently
 *  enough for ntfy to answer HTTP 400 / code 40024, so BOTH are checked and
 *  neither is allowed to pass while the other is broken. */
function assertNtfyAcceptableTypes(body, where, expectedPriority, expectedTags) {
  assert.strictEqual(typeof body.priority, "number",
    where + ": ntfy takes `priority` as a NUMBER and a JSON string here is " +
    "rejected with HTTP 400 (code 40024, measured). The digest push sent " +
    "priority=" + JSON.stringify(body.priority) +
    ", so the Worker's own digest has never once been accepted. body=" +
    JSON.stringify(body));
  assert.ok(Array.isArray(body.tags),
    where + ": ntfy takes `tags` as an ARRAY and a JSON string here is " +
    "rejected with HTTP 400 (code 40024, measured). The digest push sent " +
    "tags=" + JSON.stringify(body.tags) + ". body=" + JSON.stringify(body));
  if (expectedPriority !== undefined) {
    assert.strictEqual(body.priority, expectedPriority,
      where + ": priority must still be " + expectedPriority + " -- fixing the " +
      "type must not quietly downgrade or upgrade the urgency the owner sees. " +
      "Got " + JSON.stringify(body.priority));
  }
  if (expectedTags !== undefined) {
    assert.deepStrictEqual(body.tags, expectedTags,
      where + ": the tags themselves must survive the type fix, in order. Got " +
      JSON.stringify(body.tags));
  }
}

test("DEFECT 1: the REAL digest push sends ntfy the types it accepts", async () => {
  // The arm the owner depends on, with a valid artifact, so this reaches the
  // `deliverDigestPlan` push at priority "4" / tags "digest,newspaper".
  // The class tag is part of the expected set, not a loosened check: the helper
  // below asserts `tags` is an ARRAY and `priority` is a NUMBER independently
  // of the value comparison, so the exact equality here still fails on a type
  // regression AND on a dropped class tag.
  const w = loadWorker();
  const h = makeHarness({ initial: { [artifactKey()]: artifact() } });
  const r = await fireOnce(w, h.kv);
  assert.strictEqual(r.thrown, null, "the delivery threw: " + r.thrown);

  const bodies = ntfyBodies(h.net);
  assert.strictEqual(bodies.length, 1,
    "expected exactly one ntfy POST for a valid artifact, got " + bodies.length);
  assertNtfyAcceptableTypes(bodies[0], "the real digest push", 4,
    ["digest", "newspaper"]);

  // And the title/body survive, so the type fix cannot hide a dropped field.
  assert.strictEqual(bodies[0].topic, TOPIC);
  assert.strictEqual(bodies[0].page_url, undefined);
  assert.ok(/Something happened today/.test(bodies[0].message),
    "the digest body must still be the artifact's body, got " +
    JSON.stringify(bodies[0].message));
});

test("DEFECT 1: the FALLBACK ladder push (last_good rung) sends acceptable types",
  async () => {
    // A stale artifact pushes `fallback_last_good` at priority "5" and tags
    // "warning,rotting_light" -- the two-tag string form, which is a DIFFERENT
    // string shape from the digest's one-tag string and therefore its own bug.
    const w = loadWorker();
    const h = makeHarness({
      initial: { [artifactKey()]: JSON.stringify({
        schema_version: 1, date: "2026-10-08", slot: SLOT,
        built_at: new Date(T0 - 24 * 60 * MIN).toISOString(),
        page_url: ARCHIVE_PAGE, is_partial: false,
        ntfy: { title: "old", body: "old" },
      }), [w.DIGEST_LEDGER_KEY]: ledgerWithLastGood() },
    });
    const r = await fireOnce(w, h.kv);
    assert.strictEqual(r.thrown, null, "the delivery threw: " + r.thrown);

    const bodies = ntfyBodies(h.net);
    // TWO messages since the stale-edition alert (digest_delivery.test.js §5d)
    // was added: the fallback push, then the notice that says the edition is
    // old. The count is pinned and the rung is selected by its own priority, so
    // a third message cannot slip in unnoticed and this test still judges the
    // priority-5 push it was written for.
    assert.strictEqual(bodies.length, 2,
      "expected the fallback push plus its one stale-edition notice, got " +
      bodies.length + ": " + JSON.stringify(bodies.map((b) => b.priority)));
    const rung = bodies.filter((b) => b.priority === 5);
    assert.strictEqual(rung.length, 1,
      "expected exactly one priority-5 push on the fallback_last_good rung, got " +
      JSON.stringify(bodies.map((b) => b.priority)));
    const staleNotices = bodies.filter((b) => b.priority === 4);
    assert.strictEqual(staleNotices.length, 1);
    assert.match(staleNotices[0].title, /STALE/,
      "the second message on this rung must be the stale-edition notice, not " +
      "another digest push: " + staleNotices[0].title);
    assertNtfyAcceptableTypes(rung[0], "the fallback_last_good push", 5,
      ["warning", "rotting_light"]);
  });

test("DEFECT 1: the UNAVAILABLE rung push sends acceptable types", async () => {
  // No artifact at all and no last_good: the plain `unavailable` push, also
  // priority "5" / tags "warning,rotting_light".
  const w = loadWorker();
  const h = makeHarness({ initial: {} });
  const r = await fireOnce(w, h.kv);
  assert.strictEqual(r.thrown, null, "the delivery threw: " + r.thrown);

  const bodies = ntfyBodies(h.net);
  assert.strictEqual(bodies.length, 1,
    "expected one ntfy POST on the unavailable rung, got " + bodies.length);
  assertNtfyAcceptableTypes(bodies[0], "the unavailable push", 5,
    ["warning", "rotting_light"]);
});

test("DEFECT 1: digestPayload cannot emit a string priority or string tags, ever",
  async () => {
    // A guard on the FUNCTION, not on one call site, so a future call site
    // cannot reintroduce either shape by passing a string. digestPayload is the
    // single place every digest push is built, which is why this is where the
    // type has to be pinned.
    const w = loadWorker();
    const probes = [
      ["4", "newspaper"],
      ["5", "warning,rotting_light"],
      [4, ["newspaper"]],
      [3, []],
    ];
    for (const [priority, tags] of probes) {
      const body = w.digestPayload(TOPIC, "t", "m", "", priority, tags);
      assertNtfyAcceptableTypes(body,
        "digestPayload(priority=" + JSON.stringify(priority) + ", tags=" +
        JSON.stringify(tags) + ")");
    }

    // EVERY REAL CALL SITE IS COVERED BY THE PROBES ABOVE. Verified by reading
    // the Worker: the five `digestPayload(` calls pass ("4","newspaper"),
    // ("5","warning,rotting_light"), ("5","warning,rotting_light"),
    // ("5","warning,rotting_light") and (3,[]). Without this check the probes
    // could drift away from the call sites and the guard would protect a shape
    // nothing uses -- a test that is green and irrelevant, which is the failure
    // mode this file's own header warns about.
    const src = fs.readFileSync(WORKER_PATH, "utf8");
    // Count call sites by scanning for `digestPayload(` and excluding the
    // definition. A regex over a bounded window undercounts, because the
    // fallback-ladder call spans more than the window and the ternary's two
    // branches are two real call sites.
    let realCallCount = 0;
    let at = 0;
    while ((at = src.indexOf("digestPayload(", at)) >= 0) {
      if (!src.startsWith("function digestPayload(", at)) realCallCount += 1;
      at += "digestPayload(".length;
    }
    assert.strictEqual(realCallCount, 6,
      "found " + realCallCount + " digestPayload call sites in the Worker, " +
      "expected 6. If the call sites moved or a seventh appeared, update the " +
      "probes above so this guard still covers every one of them -- a guard " +
      "that has drifted from the call sites is green and irrelevant.");
    console.log("DEFECT 1: " + realCallCount + " digestPayload call sites, all " +
      "covered by the literal probes above.");
    // The latent shapes, asserted as DOCUMENTED rather than fixed. `Number()`
    // on undefined is NaN and JSON-serialises to null; `String(undefined)` is
    // the literal "undefined". Both are unreachable from all five current call
    // sites (each passes a literal), and the Worker is NOT edited to defend
    // against them because digestPayload IS the F2 fix and that code is
    // deliberately left exactly as shipped. This assertion exists so the day a
    // sixth call site arrives and passes an undefined argument, this test goes
    // RED and the omission is found here rather than as an HTTP 400 from ntfy.
    const bad = w.digestPayload(TOPIC, "t", "m", "", undefined, undefined);
    assert.ok(bad.priority === null || !Number.isFinite(bad.priority),
      "digestPayload(undefined) produced priority=" + JSON.stringify(bad.priority) +
      ". It is now finite, so the Worker has hardened this and this assertion " +
      "should be revisited.");
    assert.ok(bad.tags.includes("undefined"),
      "digestPayload(undefined) produced tags=" + JSON.stringify(bad.tags) +
      ". It no longer contains the literal 'undefined', so the Worker has " +
      "hardened this and this assertion should be revisited.");
    console.log(
      "DEFECT 1 latent: digestPayload(undefined) still yields priority=" +
      JSON.stringify(bad.priority) + " and tags=" + JSON.stringify(bad.tags) +
      " -- unreachable from all " + realCallCount +
      " current call sites (each passes a literal); the Worker is left as " +
      "shipped. If a future call site passes undefined, ntfy answers HTTP 400.");
  });

// ==========================================================================
// 2. DEFECT 2. The once-per-slot dispatch marker must SURVIVE a stand-down
// ==========================================================================

test("DEFECT 2: three fires of one slot produce EXACTLY ONE dispatch", async () => {
  // The missing test. Every existing coverage of this guard fires the ladder
  // ONCE, so it can only observe a marker being SET -- never one being ERASED.
  // Erasure is only visible across re-fires of the same slot against a ledger
  // that persists, which is exactly the production shape: ntfy refused the
  // push, ntfy_sent_at stayed null, so the ledger guard did not suppress the
  // re-fire and the ladder ran again, 10 minutes later, and again after that.
  const w = loadWorker();
  const h = makeHarness({ initial: {}, failNtfyPost: ntfyRefused() });
  // GH_TOKEN is REQUIRED for this test to mean anything: without it
  // escalateToActions stands down for the trivial reason "no token" and the
  // dispatch count is 0 on every fire, which would make "exactly one" pass
  // vacuously. Asserted explicitly below so that failure mode cannot hide.
  const env = { GH_TOKEN: "dispatch-token" };

  for (let i = 1; i <= 3; i += 1) {
    const r = await fireOnce(w, h.kv, { env: env, now: T0 + (i - 1) * 10 * MIN });
    assert.strictEqual(r.thrown, null, "fire " + i + " threw: " + r.thrown);
  }

  const dispatches = dispatchPosts(h.net);
  assert.strictEqual(dispatches.length, 1,
    "the same slot was dispatched " + dispatches.length + " times across three " +
    "fires of the fallback ladder; the once-per-slot guard must hold across " +
    "re-fires, not merely within one. Each dispatch is a 20-minute Actions " +
    "build and a second push the owner already has.");
});

test("DEFECT 2: actions_dispatch_at SURVIVES a stand-down fire", async () => {
  // The erasure itself, asserted on the ledger rather than inferred from the
  // dispatch count -- because a future edit could stop the second dispatch by
  // some other means while still blanking the marker the watchdog and the
  // ledger reader both depend on.
  const w = loadWorker();
  const h = makeHarness({ initial: {}, failNtfyPost: ntfyRefused() });
  const env = { GH_TOKEN: "dispatch-token" };

  await fireOnce(w, h.kv, { env: env, now: T0 });
  const afterFirst = slotEntry(h.kv);
  assert.ok(afterFirst && afterFirst.actions_dispatch_at,
    "fire 1 dispatched, so the slot must carry an actions_dispatch_at marker. " +
    "Got " + JSON.stringify(afterFirst));

  // Fire 2 and fire 3 are stand-downs: the marker is already set, so
  // escalateToActions returns dispatched:false and at:<the existing marker>.
  // The caller must carry that `at` through instead of overwriting with null.
  await fireOnce(w, h.kv, { env: env, now: T0 + 10 * MIN });
  const afterSecond = slotEntry(h.kv);
  assert.ok(afterSecond.actions_dispatch_at,
    "fire 2 stood down and ERASED the dispatch marker: actions_dispatch_at is " +
    "now " + JSON.stringify(afterSecond.actions_dispatch_at) + ". The caller " +
    "discards escalateToActions().at and writes `dispatched ? ... : null`, so a " +
    "stand-down blanks the very marker the guard reads. entry=" +
    JSON.stringify(afterSecond));
  assert.strictEqual(afterSecond.actions_dispatch_at,
    afterFirst.actions_dispatch_at,
    "the marker changed on a stand-down fire: " +
    JSON.stringify(afterFirst.actions_dispatch_at) + " -> " +
    JSON.stringify(afterSecond.actions_dispatch_at) +
    ". It is a once-per-slot marker, so a stand-down must leave it EXACTLY as " +
    "it was.");

  await fireOnce(w, h.kv, { env: env, now: T0 + 20 * MIN });
  const afterThird = slotEntry(h.kv);
  assert.strictEqual(afterThird.actions_dispatch_at, afterFirst.actions_dispatch_at,
    "the marker changed again on fire 3: " +
    JSON.stringify(afterThird.actions_dispatch_at));
});

test("DEFECT 2: a stand-down still suppresses a NEW dispatch (the guard stands)",
  async () => {
    // The fix must remove the ERASURE, never the GUARD. If carrying `.at`
    // through were implemented by ignoring it -- e.g. always writing a fresh
    // timestamp, or treating a stand-down as a dispatch -- the marker test
    // above would still pass while this one fails. So both are required.
    const w = loadWorker();
  const h = makeHarness({ initial: {}, failNtfyPost: ntfyRefused() });
  const env = { GH_TOKEN: "dispatch-token" };

  await fireOnce(w, h.kv, { env: env, now: T0 });
  const first = slotEntry(h.kv).actions_dispatch_at;
  assert.strictEqual(dispatchPosts(h.net).length, 1,
    "the first fire must actually dispatch, or the stand-down below is " +
    "asserting nothing");

  const before = dispatchPosts(h.net).length;
  await fireOnce(w, h.kv, { env: env, now: T0 + 5 * MIN });
  assert.strictEqual(dispatchPosts(h.net).length, before,
    "a stand-down fired ANOTHER dispatch. The marker must suppress a NEW " +
    "dispatch for this slot; the owner gets one push, not two.");
    assert.strictEqual(slotEntry(h.kv).actions_dispatch_at, first,
      "the marker must be unchanged by the stand-down");
  });

test("DEFECT 2: escalateToActions reports the EXISTING marker when it stands down",
  async () => {
    // Unit-level, so the contract that `at` means "the marker for this slot,
    // whatever the reason" is pinned at the function that owns it rather than
    // only through its caller. A stand-down that returned at:null would make
    // the caller's correct behaviour impossible to implement.
    const w = loadWorker();
    const marker = "2026-10-09T01:36:00Z";
    const h = makeHarness({
      initial: {
        [w.DIGEST_LEDGER_KEY]: JSON.stringify({
          schema_version: 1,
          days: { [TODAY]: { [SLOT]: { state: "failed",
                                       actions_dispatch_at: marker } } },
        }),
      },
    });
    const r = await w.escalateToActions(
      ntfyEnv(h.kv, { GH_TOKEN: "dispatch-token" }), SLOT, TODAY, "probe", T0);
    assert.strictEqual(r.dispatched, false,
      "the marker is set, so nothing may be dispatched");
    assert.strictEqual(r.at, marker,
      "escalateToActions must report the EXISTING marker as `at` when it " +
      "stands down, so the caller can preserve it. Got at=" +
      JSON.stringify(r.at));
    assert.strictEqual(dispatchPosts(h.net).length, 0,
      "a stand-down dispatched something");
  });