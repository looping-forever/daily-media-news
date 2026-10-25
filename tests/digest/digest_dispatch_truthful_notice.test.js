"use strict";
/**
 * tests/digest/digest_dispatch_truthful_notice.test.js
 *
 * THREE CONFIRMED DEFECTS IN THE WORKER'S FALLBACK LADDER, all executed against
 * the committed source. Every date, clock and id below is real production data
 * from 2026-10-13.
 *
 *   DEFECT 1 -- THE FALSE TOKEN ALARM. `escalateToActions` returns
 *   `dispatched: false` for FIVE unrelated reasons:
 *     1. !env.GH_TOKEN
 *     2. the slot already dispatched   <- the once-per-slot guard, by design
 *     3. HTTP 401
 *     4. HTTP 403
 *     5. any other non-2xx / transport error
 *   and `dispatchNote` was built from the BOOLEAN ALONE:
 *
 *     dispatched ? "A fresh build is on its way."
 *                : "No fresh build was started. This Worker could not dispatch
 *                   one (no usable GH_TOKEN) ..."
 *
 *   On 2026-10-13 cause 2 fired. The 01:50:26Z re-fire found no artifact, so it
 *   escalated; the marker recorded by the 01:35:36Z fire was already set, so the
 *   once-per-slot guard stood down -- correctly, exactly as designed -- and the
 *   notice told the owner "no usable GH_TOKEN". That token had dispatched two
 *   workflows hours earlier. The owner spent real time believing his credential
 *   had died. A notice that names a fault which did not happen is worse than no
 *   notice, because it sends someone to fix something that is not broken.
 *
 *   DEFECT 2 -- LAST-GOOD PUSHED OVER A CORRECT DELIVERY. `digest:artifact:*`
 *   has exactly one writer (the seal job in digest-prebuild.yml) and
 *   daily-news.yml writes none of it, so an Actions-dispatched delivery is
 *   invisible to this Worker. On 2026-10-13 the 06:40 prebuild failed, the
 *   Worker dispatched daily-news.yml at 01:35:36Z, that run succeeded and
 *   delivered a correct fresh digest at 01:40:03Z (run 38332862002, ntfy id
 *   yqiRtmjhU7Go, `delivered: true`), and the 01:50:26Z re-fire could not see
 *   any of it and pushed last-good. The owner received today's morning digest at
 *   07:25 NPT and then YESTERDAY'S EVENING digest at 07:35 NPT. Two pushes, the
 *   second contradicting the first.
 *
 *   DEFECT 3 -- NO DISPATCH TELEMETRY. The HTTP status of that morning's digest
 *   dispatch is unrecoverable today: Cloudflare retains no past Worker console
 *   output and `recordDispatch` covers only the breaking-alert arm. The debugger
 *   had to infer acceptance from its effect.
 *
 * WHY A NEW FILE. digest_delivery.test.js and
 * digest_ntfy_types_and_dispatch_marker.test.js both fire the ladder once per
 * test against a ledger that starts empty, so neither can see what a notice says
 * when the slot's dispatch marker is ALREADY set -- which is the only situation
 * defect 1 is about, and the only situation defect 2 is about as well.
 *
 * THE LOADER IS THE SAME ONE: committed Worker source, `export default {` ->
 * `const WORKER = {`, re-exported through a module shim, loaded from a unique
 * temp path. If a name below stops existing the loader throws rather than
 * quietly testing nothing.
 *
 * RUN:
 *     node --test tests/digest/digest_dispatch_truthful_notice.test.js
 *
 * REPRODUCING A RED WITHOUT TOUCHING THE COMMITTED WORKER:
 *     DIGEST_NOTICE_WORKER=<path> node --test <this file>
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const COMMITTED_WORKER = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.DIGEST_NOTICE_WORKER || COMMITTED_WORKER;

if (process.env.DIGEST_NOTICE_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (DIGEST_NOTICE_WORKER) INSTEAD of " +
    "the committed " + COMMITTED_WORKER + ".\n");
}

const EXPORT_NAMES = [
  "WORKER", "deliverDigest", "digestPayload", "escalateToActions",
  "classifyDeliveryEvidence", "checkSlotDeliveredByWorkerRun",
  "redactDispatchSummary", "githubWorkflowDispatch",
  "DELIVERY_EVIDENCE_DELIVERED", "DELIVERY_EVIDENCE_NOT_DELIVERED",
  "DELIVERY_EVIDENCE_UNKNOWN", "DELIVERY_KIND_STOOD_DOWN",
  "DIGEST_DISPATCH_STATUS_KEY", "KIND_UNAVAILABLE", "KIND_FALLBACK_LAST_GOOD",
  "digestSequenceId", "digestFullSequenceId", "DIGEST_LEDGER_KEY",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " -- this harness cannot " +
      "reach the Worker's entry points, so it would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-notice-"));
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
/** The morning slot instant on the real day: 2026-10-13T01:35:00Z = 07:20 NPT. */
const SLOT_INSTANT = Date.parse("2026-10-13T01:35:00Z");
const TODAY = "2026-10-13";
const SLOT = "morning";

/** Real dispatch instant from the incident report: 2026-10-13T01:35:36Z. */
const DISPATCH_AT = "2026-10-13T01:35:36Z";
/** Real re-fire instant from the incident report: 2026-10-13T01:50:26Z. */
const REFIRE_AT = Date.parse("2026-10-13T01:50:26Z");

/** Real delivery instant: the Actions run finished and pushed at 01:40:03Z. */
const RUN_COMPLETED_AT = "2026-10-13T01:40:03Z";
const RUN_CREATED_AT = "2026-10-13T01:35:41Z";
const RUN_ID = 38332862002;

const NTFY_SERVER = "https://ntfy.example.invalid";
const TOPIC = "dmn-secret-topic-7f3a91c4";
const GH = "dispatch-token-9f2c";
const ARCHIVE_PAGE =
  "https://daily-digest.example.pages.dev/digests/2026-10-13-morning.html";
/** The Worker's own ledger key, spelled the way every other suite spells it. */
const LEDGER_KEY = "digest:ledger";

function ntfyEnv(kv, extra) {
  return Object.assign({
    KV: kv,
    NTFY_SERVER: NTFY_SERVER,
    NTFY_TOPIC: TOPIC,
    WORKER_BUILD: "testbuild",
  }, extra || {});
}

/** A successful workflow_dispatch run of daily-news.yml, inside the window. */
function successfulRun(over) {
  return Object.assign({
    id: RUN_ID,
    name: "Daily News Digest",
    event: "workflow_dispatch",
    status: "completed",
    conclusion: "success",
    workflow_id: 1,
    created_at: RUN_CREATED_AT,
    updated_at: RUN_COMPLETED_AT,
    html_url: "https://github.com/looping-forever/daily-media-news/actions/runs/" + RUN_ID,
  }, over || {});
}

function runsBody(runs) {
  return { total_count: runs.length, workflow_runs: runs };
}

/** A ledger shaped exactly like the morning of 2026-10-13: the slot was
 *  escalated at 01:35:36Z, ntfy refused that push, so `ntfy_sent_at` is null
 *  and the 01:50:26Z re-fire is NOT stopped by the ledger guard. */
function incidentLedger(over) {
  return JSON.stringify(Object.assign({
    schema_version: 1,
    as_of: "2026-10-13T01:36:00Z",
    last_good: {
      date: "2026-10-12",
      slot: "evening",
      page_url: "https://daily-digest.example.pages.dev/digests/2026-10-12-evening.html",
      title: "Evening Digest - 2026-10-12 - 11 items",
      body: "Yesterday's evening digest, which did build.",
      built_at: "2026-10-12T13:30:00Z",
      at: "2026-10-12T13:35:00Z",
    },
    days: {
      [TODAY]: {
        [SLOT]: {
          state: "failed",
          kind: "fallback_last_good",
          ntfy_failed: true,
          ntfy_sent_at: null,
          reject_reason: "absent | NO build was dispatched",
          actions_dispatch_at: DISPATCH_AT,
          actions_dispatch_reason: "already dispatched this slot",
        },
      },
    },
  }, over || {}));
}

/** The full incident state, given what the Actions API answers. */
function incidentStore(o) {
  const opts = o || {};
  return Object.assign({ [LEDGER_KEY]: incidentLedger() }, opts.initial || {});
}

/**
 * A harness that answers the THREE different GETs this path can make:
 *   * the Actions run list  -> `o.runs` (or `o.runsHttp` / `o.runsThrow`)
 *   * the ntfy sequence_id read-back -> `o.cache`
 *   * anything else -> 404, loudly, so an unmodelled call is visible
 * and the two POSTs: the workflow dispatch (`o.dispatchHttp`) and ntfy.
 */
function makeHarness(opts) {
  const o = opts || {};
  const store = Object.assign({}, o.initial || {});
  const kv = {
    store: store,
    puts: [],
    async get(key) {
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    async put(key, value) {
      kv.puts.push({ key: key, value: value });
      store[key] = value;
    },
  };
  const net = { calls: [] };
  globalThis.fetch = async function (url, init) {
    const method = String((init && init.method) || "GET").toUpperCase();
    const u = String(url);
    net.calls.push({ method: method, url: u,
                     headers: (init && init.headers) || {}, body: init && init.body });
    if (method === "POST") {
      if (String(u).indexOf("/dispatches") >= 0) {
        if (o.dispatchHttp) return o.dispatchHttp;
        return { ok: true, status: 204, text: async () => "" };
      }
      if (o.failNtfyPost) return o.failNtfyPost;
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ id: "ntfy-msg" }) };
    }
    // GET. The Actions run list is a NEW call on this path and is routed
    // explicitly, because answering it with the ntfy read-back shape would
    // silently exercise the wrong branch.
    if (String(u).indexOf("/actions/workflows/daily-news.yml/runs") >= 0) {
      if (o.runsThrow) throw new Error(o.runsThrow);
      if (o.runsHttp) return o.runsHttp;
      return { ok: true, status: 200,
               text: async () => JSON.stringify(runsBody(o.runs || [])) };
    }
    return { ok: true, status: 200,
             text: async () => JSON.stringify(o.cache || []) };
  };
  return { kv: kv, net: net };
}

function ntfyPosts(net) {
  return net.calls.filter((c) => c.method === "POST" &&
    String(c.url).indexOf("dispatches") < 0);
}
function dispatchPosts(net) {
  return net.calls.filter((c) => c.method === "POST" &&
    String(c.url).indexOf("/dispatches") >= 0);
}
function runsGets(net) {
  return net.calls.filter((c) => c.method === "GET" &&
    String(c.url).indexOf("/actions/workflows/daily-news.yml/runs") >= 0);
}
function bodies(net) {
  return ntfyPosts(net).map((c) => JSON.parse(c.body));
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
    restore() { console.log = real.log; console.error = real.error;
               console.warn = real.warn; },
  };
}

async function fire(w, h, options) {
  const o = options || {};
  const logs = captureLogs();
  let outcome = null;
  let thrown = null;
  try {
    outcome = await w.deliverDigest(ntfyEnv(h.kv, o.env), SLOT, {
      now: () => (o.now === undefined ? REFIRE_AT : o.now),
      sleep: async () => {},
    });
  } catch (err) {
    thrown = err;
  } finally {
    logs.restore();
  }
  return { outcome: outcome, thrown: thrown, logs: logs };
}

function slotEntry(h) {
  const raw = h.kv.store[LEDGER_KEY];
  if (!raw) return null;
  const parsed = JSON.parse(raw);
  return parsed && parsed.days && parsed.days[TODAY]
    ? parsed.days[TODAY][SLOT] : null;
}

const ntfyRefused = () => ({ ok: false, status: 400,
  text: async () => JSON.stringify({ code: 40024, http: 400, error: "bad body" }) });

/** Every way this file calls the ladder a FAILURE, in the owner's own words. */
const FAILURE_LEAD = "No fresh build was started";
/** The stand-down's words, which must never be confused with a failure. */
const STANDDOWN_LEAD = "Nothing is wrong";

// ==========================================================================
// 1. DEFECT 1. The notice names the cause the code ESTABLISHED, and only that
// ==========================================================================

test("DEFECT 1: a benign stand-down produces a NON-failure notice, not a token alarm",
  async () => {
    // TODAY'S ACTUAL FAILURE, reproduced exactly. Fire 1 escalates and ntfy
    // refuses, so the marker is set and ntfy_sent_at is null. Fire 2 is the
    // 01:50:26Z re-fire: cause 2 of 5, the once-per-slot guard.
    const w = loadWorker();
    const h = makeHarness({ initial: {}, failNtfyPost: ntfyRefused() });

    const first = await fire(w, h, { env: { GH_TOKEN: GH }, now: SLOT_INSTANT });
    assert.strictEqual(first.thrown, null, "fire 1 threw: " + first.thrown);
    assert.strictEqual(dispatchPosts(h.net).length, 1,
      "VACUITY GUARD: fire 1 must actually dispatch, or the stand-down below is " +
      "asserting nothing about the marker");
    assert.ok(typeof slotEntry(h).actions_dispatch_at === "string" &&
      slotEntry(h).actions_dispatch_at.length > 0,
      "VACUITY GUARD: fire 1 must record the dispatch marker this test stands " +
      "down on. Got " + JSON.stringify(slotEntry(h).actions_dispatch_at));

    const before = dispatchPosts(h.net).length;
    const markerAt = slotEntry(h).actions_dispatch_at;
    // Retries are counted, not pushes: a refused ntfy POST is attempted
    // DELIVERY_ATTEMPTS times. Only fire 2's traffic is inspected from here.
    assert.strictEqual(before, 1, "VACUITY GUARD: fire 1's dispatch call");
    h.net.calls.length = 0;
    const second = await fire(w, h, { env: { GH_TOKEN: GH }, now: REFIRE_AT });
    assert.strictEqual(second.thrown, null, "fire 2 threw: " + second.thrown);

    const b = bodies(h.net);
    assert.ok(b.length >= 1,
      "the re-fire pushed nothing at all, so there is no notice to inspect. " +
      "The stand-down ladder must still tell the owner something.");
    const message = b[b.length - 1].message;

    // THE DEFECT. This is the string that cost the owner real time.
    assert.ok(message.indexOf("GH_TOKEN") < 0,
      "the notice names GH_TOKEN on a BENIGN STAND-DOWN — today's false alarm, " +
      "verbatim. The token was never consulted: the guard read a marker and " +
      "stopped. message=" + message);
    assert.ok(!/token/i.test(message),
      "the notice mentions a token at all on a stand-down, which is still " +
      "suggesting a credential problem where none exists. message=" + message);
    assert.ok(message.indexOf(FAILURE_LEAD) < 0,
      "a stand-down is not a failure and must not wear the failure's lead-in. " +
      "message=" + message);
    assert.ok(message.indexOf(STANDDOWN_LEAD) >= 0,
      "the stand-down notice does not tell the owner there is nothing wrong. " +
      "message=" + message);
    assert.ok(/nothing to fix/i.test(message),
      "the stand-down notice must not leave the owner looking for something to " +
      "fix. message=" + message);
    // And it names WHICH build it is standing down for, so the owner can match
    // it against the one he is waiting for.
    assert.ok(message.indexOf(markerAt) >= 0,
      "the stand-down notice does not name the dispatch it is standing down " +
      "for (" + markerAt + "). message=" + message);
    // And it must still be a complete, usable notice — a stand-down is not an
    // excuse to send the owner a broken message.
    assert.ok(/Why you are seeing this/.test(message),
      "the stand-down notice lost its header. message=" + message);
    assert.ok(/archive index/i.test(message),
      "the stand-down notice lost the archive link, so the owner has nowhere to " +
      "go from it. message=" + message);

    // The stand-down is cause 2, and the machine-readable twin says so.
    assert.strictEqual(dispatchPosts(h.net).length, 0,
      "the stand-down fired another dispatch");
    assert.strictEqual(slotEntry(h).actions_dispatch_cause, "already_dispatched",
      "the ledger does not record that this was a stand-down rather than a " +
      "failure. Got " + JSON.stringify(slotEntry(h).actions_dispatch_cause));
  });

test("DEFECT 1: a 401 produces a token-SPECIFIC message, distinct from the stand-down",
  async () => {
    const w = loadWorker();
    const h = makeHarness({
      initial: {},
      failNtfyPost: ntfyRefused(),
      dispatchHttp: { ok: false, status: 401,
                      text: async () => "Bad credentials" },
    });
    const r = await fire(w, h, { env: { GH_TOKEN: GH }, now: SLOT_INSTANT });
    assert.strictEqual(r.thrown, null, "a 401 must not throw out of the cron: " +
      r.thrown);
    assert.strictEqual(dispatchPosts(h.net).length, 1,
      "VACUITY GUARD: the dispatch must actually have been attempted");
    const message = bodies(h.net)[0].message;

    assert.ok(message.indexOf(FAILURE_LEAD) >= 0,
      "a genuinely refused dispatch is NOT disclosed as a failure. message=" +
      message);
    assert.ok(/401/.test(message),
      "a 401 is a token fault and the notice does not say so. message=" +
      message);
    assert.ok(/invalid or expired/i.test(message),
      "a 401 must be named as an invalid-or-expired token, not generically. " +
      "message=" + message);
    assert.ok(message.indexOf(STANDDOWN_LEAD) < 0,
      "a 401 is a real failure and must not wear the stand-down's reassuring " +
      "lead-in. message=" + message);
    assert.strictEqual(slotEntry(h).actions_dispatch_cause, "token_unauthorised");
    assert.strictEqual(slotEntry(h).actions_dispatch_http_status, 401);
  });

test("DEFECT 1: a 403 names PERMISSION, so the owner knows which half to fix",
  async () => {
    const w = loadWorker();
    const h = makeHarness({
      initial: {}, failNtfyPost: ntfyRefused(),
      dispatchHttp: { ok: false, status: 403,
                      text: async () => "Resource not accessible by integration" },
    });
    const r = await fire(w, h, { env: { GH_TOKEN: GH }, now: SLOT_INSTANT });
    assert.strictEqual(r.thrown, null);
    const message = bodies(h.net)[0].message;
    assert.ok(/403/.test(message), "a 403 is not disclosed. message=" + message);
    assert.ok(/not allowed to start workflows/i.test(message),
      "a 403 is a SCOPE fault and the notice must distinguish it from a 401's " +
      "invalid token — they need different fixes. message=" + message);
    assert.ok(!/invalid or expired/i.test(message),
      "a 403 told the owner the token is invalid or expired. It is valid and " +
      "merely unscoped, and that is a different repair. message=" + message);
    assert.strictEqual(slotEntry(h).actions_dispatch_cause, "token_forbidden");
  });

test("DEFECT 1: a 500 says NOT-A-TOKEN, and a transport error says NOT-A-TOKEN",
  async () => {
    // Cause 5 both halves. Both reach GitHub and both come back wrong, and in
    // neither case is the credential implicated.
    for (const [name, harnessOpts, expect] of [
      ["http_500", { dispatchHttp: { ok: false, status: 500,
        text: async () => "server blew up" } }, /500/],
      ["http_422", { dispatchHttp: { ok: false, status: 422,
        text: async () => "invalid workflow" } }, /422/],
    ]) {
      const w = loadWorker();
      const h = makeHarness(Object.assign({ initial: {}, failNtfyPost: ntfyRefused() },
                                         harnessOpts));
      const r = await fire(w, h, { env: { GH_TOKEN: GH }, now: SLOT_INSTANT });
      assert.strictEqual(r.thrown, null, name + " threw: " + r.thrown);
      const message = bodies(h.net)[0].message;
      assert.ok(expect.test(message),
        name + ": the HTTP status the Worker received is not in the notice the " +
        "owner reads. message=" + message);
      assert.ok(/not a credential fault/i.test(message),
        name + ": the notice must say explicitly that this is NOT a credential " +
        "fault, or the owner repeats 2026-10-13 in a new costume. message=" +
        message);
      assert.ok(!/token/i.test(message),
        name + ": a plain HTTP error mentioned a token at all. The credential " +
        "was never implicated. message=" + message);
      assert.ok(message.indexOf(STANDDOWN_LEAD) < 0,
        name + ": a failed dispatch must not read as a benign stand-down");
      assert.strictEqual(slotEntry(h).actions_dispatch_cause, "http_error",
        name + ": the ledger records the wrong cause");
      assert.strictEqual(slotEntry(h).actions_dispatch_http_status,
        harnessOpts.dispatchHttp.status,
        name + ": the ledger does not record the status the Worker received");
    }
  });

test("DEFECT 1: a transport failure is a NETWORK fault, and says so", async () => {
  const w = loadWorker();
  const store = {};
  const kv = {
    store: store, puts: [],
    async get(k) { return Object.prototype.hasOwnProperty.call(store, k)
      ? store[k] : null; },
    async put(k, v) { kv.puts.push({ key: k, value: v }); store[k] = v; },
  };
  const net = { calls: [] };
  // The dispatch cannot be reached; everything else behaves. Only the dispatch
  // is broken, so only the dispatch's absence can be the reason.
  globalThis.fetch = async function (url, init) {
    const method = String((init && init.method) || "GET").toUpperCase();
    net.calls.push({ method: method, url: String(url),
                     headers: (init && init.headers) || {},
                     body: init && init.body });
    if (method === "POST" && String(url).indexOf("/dispatches") >= 0) {
      throw new Error("getaddrinfo ENOTFOUND api.github.com");
    }
    if (method === "POST") {
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ id: "ntfy-msg" }) };
    }
    return { ok: true, status: 200, text: async () => "[]" };
  };
  const logs = captureLogs();
  let outcome = null;
  let thrown = null;
  try {
    outcome = await w.deliverDigest(ntfyEnv(kv, { GH_TOKEN: GH }), SLOT,
      { now: () => SLOT_INSTANT, sleep: async () => {} });
  } catch (err) {
    thrown = err;
  } finally {
    logs.restore();
  }
  assert.strictEqual(thrown, null,
    "a transport failure propagated out of the cron invocation as a throw: " +
    thrown);
  assert.strictEqual(outcome.kind, w.KIND_UNAVAILABLE,
    "the owner still gets the fallback push after a network fault. kind=" +
    outcome.kind);
  const posts = net.calls.filter((c) => c.method === "POST" &&
    String(c.url).indexOf("/dispatches") < 0);
  assert.ok(posts.length >= 1,
    "VACUITY GUARD: the fallback push must actually have been attempted, or " +
    "the message assertions below are about nothing");
  const message = JSON.parse(posts[posts.length - 1].body).message;
  assert.ok(/network fault/i.test(message),
    "a transport failure must be named as a network fault, not a credential " +
    "one. message=" + message);
  assert.ok(!/invalid or expired/i.test(message),
    "a network fault told the owner the token expired. message=" + message);
  // The three BLAME phrasings, checked by name rather than by the word "token":
  // the transport branch may legitimately say "nothing points at the token",
  // which is reassurance, and a crude /token/ check would call that a failure.
  for (const blame of [/no usable GH_TOKEN/i, /invalid or expired/i,
                       /not allowed to start workflows/i,
                       /no GitHub token is configured/i]) {
    assert.ok(!blame.test(message),
      "a network fault used a CREDENTIAL-BLAME phrase (" + blame + "), which " +
      "sends the owner to fix a token that was never involved. message=" +
      message);
  }
});

test("DEFECT 1: escalateToActions reports a CAUSE for all five paths, never a bare false",
  async () => {
    // Unit level, so the contract is pinned at the function that owns it rather
    // than only through one caller. Each case must return a `cause`; a future
    // edit that drops one is a red test here rather than another morning like
    // 2026-10-13.
    const w = loadWorker();
    const noKv = { get: async () => null, put: async () => {} };

    // 1. no token
    const a = await w.escalateToActions(ntfyEnv(noKv), SLOT, TODAY, "probe",
                                        SLOT_INSTANT);
    assert.strictEqual(a.dispatched, false);
    assert.strictEqual(a.cause, "token_missing", JSON.stringify(a));

    // 2. already dispatched
    const marked = { get: async () => incidentLedger(), put: async () => {} };
    const b = await w.escalateToActions(ntfyEnv(marked, { GH_TOKEN: GH }), SLOT,
                                        TODAY, "probe", SLOT_INSTANT);
    assert.strictEqual(b.dispatched, false);
    assert.strictEqual(b.cause, "already_dispatched", JSON.stringify(b));

    // 3/4/5. the HTTP causes, via a stub that answers the dispatch.
    for (const [status, cause] of [[401, "token_unauthorised"],
                                   [403, "token_forbidden"],
                                   [500, "http_error"]]) {
      globalThis.fetch = async (u, init) => {
        if (String((init && init.method) || "GET").toUpperCase() === "POST") {
          return { ok: false, status: status, text: async () => "nope" };
        }
        return { ok: true, status: 200, text: async () => JSON.stringify([]) };
      };
      const c = await w.escalateToActions(ntfyEnv(noKv, { GH_TOKEN: GH }), SLOT,
                                          TODAY, "probe", SLOT_INSTANT);
      assert.strictEqual(c.cause, cause,
        "HTTP " + status + " produced cause=" + JSON.stringify(c));
      assert.strictEqual(c.http_status, status,
        "the HTTP status must travel with the cause so the notice can name it");
    }
  });

// ==========================================================================
// 2. DEFECT 2. The re-fire ladder stands down when a Worker-dispatched run
//    already delivered the slot — and fails CLOSED
// ==========================================================================

test("DEFECT 2: a delivered slot gets NO last-good push (2026-10-13, exactly)",
  async () => {
    const w = loadWorker();
    // TODAY. The ledger says: escalated at 01:35:36Z, ntfy refused, so no
    // ntfy_sent_at. The Actions API says: a successful workflow_dispatch run
    // created at 01:35:41Z finished at 01:40:03Z and delivered.
    const h = makeHarness({
      initial: incidentStore(),
      runs: [successfulRun()],
      cache: [],
    });
    const r = await fire(w, h, { env: { GH_TOKEN: GH } });
    assert.strictEqual(r.thrown, null, "the re-fire threw: " + r.thrown);

    // THE DEFECT. Without this guard, this is the push that sent the owner
    // yesterday's evening digest at 07:35 NPT.
    assert.strictEqual(ntfyPosts(h.net).length, 0,
      "the re-fire pushed last-good over a slot a Worker-dispatched run had " +
      "ALREADY delivered. The owner gets today's morning digest and then " +
      "yesterday's evening digest, and the second contradicts the first. " +
      "bodies=" + JSON.stringify(bodies(h.net).map((b) => b.message)));

    // And it did not waste a build on a slot that is already done.
    assert.strictEqual(dispatchPosts(h.net).length, 0,
      "the re-fire dispatched a 20-minute Actions build for a slot that was " +
      "already delivered");

    assert.strictEqual(r.outcome.kind, w.DELIVERY_KIND_STOOD_DOWN,
      "the outcome must say this arm produced nothing, not that it delivered: " +
      JSON.stringify(r.outcome));
    assert.strictEqual(r.outcome.delivery_evidence, w.DELIVERY_EVIDENCE_DELIVERED);
    assert.strictEqual(r.outcome.delivery_evidence_run.id, String(RUN_ID),
      "the evidence must name the run, so this is diagnosable later. Got " +
      JSON.stringify(r.outcome.delivery_evidence_run));
    // Honest bookkeeping: last_good_shown is null BECAUSE nothing was shown.
    assert.strictEqual(r.outcome.last_good_shown, null,
      "the outcome claims a last_good digest was shown, but nothing was pushed: " +
      JSON.stringify(r.outcome));
  });

test("DEFECT 2 (VACUITY): the SAME harness with NO delivered run DOES push last-good",
  async () => {
    // A guard that stands down unconditionally would pass the test above while
    // silently deleting all delivery. So the counterfactual is executed here:
    // one variable changed, the opposite outcome.
    const w = loadWorker();
    const h = makeHarness({
      initial: incidentStore(),
      runs: [],              // <-- the ONLY difference
      cache: [],
    });
    const r = await fire(w, h, { env: { GH_TOKEN: GH } });
    assert.strictEqual(r.thrown, null);
    const b = bodies(h.net);
    assert.strictEqual(b.length, 1,
      "with no delivered run the ladder MUST push last-good — that push is the " +
      "no-burn invariant. A guard that suppresses it has stopped delivery " +
      "outright.");
    assert.ok(/2026-10-12/.test(b[0].message),
      "the push must be the last-good one it always was. message=" + b[0].message);
    assert.strictEqual(r.outcome.delivery_evidence,
      w.DELIVERY_EVIDENCE_NOT_DELIVERED);
  });

test("DEFECT 2: an UNREADABLE state stands down LOUDLY and is not called delivered",
  async () => {
    // The fail-closed counterpart. Every one of these is a state where the
    // Worker cannot establish whether the slot was delivered. None may push
    // last-good (that is the contradicting second push) and none may claim
    // `delivered` (that would stop delivery for good, silently).
    const cases = [
      ["actions read HTTP 500",
        { runsHttp: { ok: false, status: 500, text: async () => "boom" } }],
      ["actions read HTTP 403 (token cannot read runs)",
        { runsHttp: { ok: false, status: 403, text: async () => "forbidden" } }],
      ["actions read is not JSON",
        { runsHttp: { ok: true, status: 200, text: async () => "<html>login</html>" } }],
      ["actions read throws",
        { runsThrow: "getaddrinfo ENOTFOUND api.github.com" }],
      ["actions read has no run list at all",
        { runsHttp: { ok: true, status: 200,
                      text: async () => JSON.stringify({ total_count: 0 }) } }],
    ];
    for (const [name, opts] of cases) {
      const w = loadWorker();
      const h = makeHarness(Object.assign(
        { initial: incidentStore(), cache: [] }, opts));
      const r = await fire(w, h, { env: { GH_TOKEN: GH } });
      assert.strictEqual(r.thrown, null, name + " threw: " + r.thrown);

      assert.strictEqual(ntfyPosts(h.net).length, 0,
        name + ": an unreadable state pushed last-good. That is the " +
        "contradicting second push all over again — the owner receives a stale " +
        "digest on top of one he may already have, and nothing takes it back.");

      assert.strictEqual(r.outcome.delivery_evidence, w.DELIVERY_EVIDENCE_UNKNOWN,
        name + ": an unreadable state must be recorded as UNKNOWN");
      assert.notStrictEqual(r.outcome.delivery_evidence,
        w.DELIVERY_EVIDENCE_DELIVERED,
        name + ": an unreadable state was recorded as DELIVERED, which would " +
        "stop this slot's delivery for good and say nothing about it");
      assert.ok(r.logs.text().length > 0,
        name + ": an unreadable state produced no log line at all, so it was " +
        "silent");
      assert.ok(/UNKNOWN/i.test(r.logs.text()),
        name + ": the log does not say the evidence was unknown. logs=" +
        r.logs.text());
    }
  });

test("DEFECT 2: a marker with no token to read it with is UNKNOWN, not delivered",
  async () => {
    // The genuinely ambiguous production case: the token was present when the
    // slot was escalated and is absent now. It cannot be read as "not
    // delivered" (a run may have delivered it) and must not be read as
    // "delivered" (nothing established that).
    const w = loadWorker();
    const h = makeHarness({ initial: incidentStore(), cache: [] });
    const r = await fire(w, h, {});   // NO GH_TOKEN
    assert.strictEqual(r.thrown, null);
    assert.strictEqual(ntfyPosts(h.net).length, 0,
      "pushed last-good without being able to check whether the slot was " +
      "already delivered");
    assert.strictEqual(r.outcome.delivery_evidence, w.DELIVERY_EVIDENCE_UNKNOWN);
  });

test("DEFECT 2: no dispatch marker means no check, no call, and the ladder runs",
  async () => {
    // The common case, and it must stay free. Before the marker exists this
    // Worker has dispatched nothing for this slot, so there is provably no run
    // to find and nothing to ask GitHub.
    const w = loadWorker();
    const h = makeHarness({ initial: { [LEDGER_KEY]: JSON.stringify({
      schema_version: 1, days: {}, last_good: {
        date: "2026-10-12", slot: "evening",
        page_url: "https://daily-digest.example.pages.dev/digests/2026-10-12-evening.html",
        title: "t", body: "yesterday", built_at: "2026-10-12T13:30:00Z",
        at: "2026-10-12T13:35:00Z" },
    }) }, cache: [] });
    const r = await fire(w, h, { env: { GH_TOKEN: GH } });
    assert.strictEqual(r.thrown, null);
    assert.strictEqual(runsGets(h.net).length, 0,
      "the Actions API was called for a slot this Worker never escalated — a " +
      "GitHub API call added to a routine re-fire for no possible information");
    assert.strictEqual(ntfyPosts(h.net).length, 1,
      "with no marker the ladder must push exactly as it always did");
  });

test("DEFECT 2: classifyDeliveryEvidence is a total, fail-closed table",
  async () => {
    // Unit level. Every shape that is NOT a clean successful workflow_dispatch
    // run inside the window must be `not_delivered` or `unknown`, and `unknown`
    // is never allowed to masquerade as either of the other two.
    const w = loadWorker();
    const from = DISPATCH_AT;
    const now = REFIRE_AT;
    const ok = (runs) => w.classifyDeliveryEvidence(runs, from, now).state;

    assert.strictEqual(ok([successfulRun()]), w.DELIVERY_EVIDENCE_DELIVERED,
      "a successful workflow_dispatch run in the window is delivered");

    assert.strictEqual(ok([]), w.DELIVERY_EVIDENCE_NOT_DELIVERED,
      "an empty, fully-read run list is definitively not delivered");

    assert.strictEqual(
      ok([successfulRun({ conclusion: "failure" })]),
      w.DELIVERY_EVIDENCE_NOT_DELIVERED,
      "a run that concluded FAILURE delivered nothing");
    assert.strictEqual(
      ok([successfulRun({ conclusion: "cancelled" })]),
      w.DELIVERY_EVIDENCE_NOT_DELIVERED,
      "a CANCELLED run delivered nothing");
    assert.strictEqual(
      ok([successfulRun({ status: "in_progress" })]),
      w.DELIVERY_EVIDENCE_NOT_DELIVERED,
      "a run still in progress has not delivered YET; the ladder must still " +
      "push, which is the existing designed behaviour");
    assert.strictEqual(
      ok([successfulRun({ event: "schedule" })]),
      w.DELIVERY_EVIDENCE_NOT_DELIVERED,
      "a SCHEDULED run is not a Worker dispatch and is not evidence for this " +
      "slot");
    assert.strictEqual(
      ok([successfulRun({ created_at: "2026-10-13T01:35:00Z" })]),
      w.DELIVERY_EVIDENCE_NOT_DELIVERED,
      "a run created BEFORE this slot's own dispatch is somebody else's");
    assert.strictEqual(
      ok([successfulRun({ created_at: "2026-10-14T01:35:41Z" })]),
      w.DELIVERY_EVIDENCE_NOT_DELIVERED,
      "a run created AFTER now is not evidence about this moment");

    // THE FAIL-CLOSED COLUMN. None of these may be silently resolved.
    assert.strictEqual(ok(null), w.DELIVERY_EVIDENCE_UNKNOWN,
      "a missing run list is unknown");
    assert.strictEqual(ok({}), w.DELIVERY_EVIDENCE_UNKNOWN,
      "an object that is not a list is unknown");
    assert.strictEqual(ok([successfulRun({ created_at: "not-a-date" })]),
      w.DELIVERY_EVIDENCE_UNKNOWN,
      "a run whose created_at will not parse cannot be placed in the window, " +
      "and guessing either way is how a guard silently stops working");
    assert.strictEqual(ok(["nope"]), w.DELIVERY_EVIDENCE_UNKNOWN,
      "a list containing a non-run is unknown");
    assert.strictEqual(ok([null]), w.DELIVERY_EVIDENCE_UNKNOWN,
      "a list containing null is unknown");
    assert.strictEqual(
      w.classifyDeliveryEvidence([successfulRun()], "not-a-date", now).state,
      w.DELIVERY_EVIDENCE_UNKNOWN,
      "an unparseable dispatch marker means the window is undefined");
  });

test("DEFECT 2: the stand-down never blanks the dispatch marker", async () => {
    const w = loadWorker();
    const h = makeHarness({ initial: incidentStore(), runs: [successfulRun()],
                            cache: [] });
    const r = await fire(w, h, { env: { GH_TOKEN: GH } });
    assert.strictEqual(r.thrown, null);
    assert.strictEqual(slotEntry(h).actions_dispatch_at, DISPATCH_AT,
      "the stand-down ERASED the marker. It is the once-per-slot marker the " +
      "watchdog and the ledger reader both depend on, and erasing it is what " +
      "let 2026-10-12 send three digests for one slot.");
  });

// ==========================================================================
// 3. DEFECT 3. A refused dispatch is persisted, with no secret material in it
// ==========================================================================

test("DEFECT 3: a refused dispatch is persisted with its status and cause",
  async () => {
    const w = loadWorker();
    const h = makeHarness({
      initial: {},
      dispatchHttp: { ok: false, status: 403,
                      text: async () => "Resource not accessible by integration" },
    });
    const r = await fire(w, h, { env: { GH_TOKEN: GH }, now: SLOT_INSTANT });
    assert.strictEqual(r.thrown, null);

    const put = h.kv.puts.find((p) => p.key === w.DIGEST_DISPATCH_STATUS_KEY);
    assert.ok(put,
      "nothing was persisted under " + w.DIGEST_DISPATCH_STATUS_KEY +
      ". Cloudflare keeps no past Worker console output, so without this the " +
      "next failed dispatch is as unrecoverable as this morning's was. Keys " +
      "written: " + JSON.stringify(h.kv.puts.map((p) => p.key)));
    const rec = JSON.parse(put.value);
    assert.strictEqual(rec.http_status, 403,
      "the recorded status is wrong: " + JSON.stringify(rec));
    assert.strictEqual(rec.cause, "token_forbidden",
      "the recorded cause is wrong: " + JSON.stringify(rec));
    assert.strictEqual(rec.ok, false);
    assert.ok(Number.isFinite(Date.parse(rec.at)),
      "the record carries no usable timestamp, so " +
      "'when did this fail?' is still unanswerable: " + JSON.stringify(rec));
    assert.strictEqual(rec.workflow, "daily-news.yml");
  });

test("DEFECT 3: the persisted record contains NO token and NO unredacted body",
  async () => {
    const w = loadWorker();
    // A response body that ECHOES the credential, which is the shape of the
    // accident this guards against. Driven through the `http_error` branch
    // because that is the branch that READS a body: 401/403 record the status
    // alone, deliberately, so a refused credential cannot pull an
    // attacker-shaped payload into storage in the first place.
    const leaky =
      '{"message":"Validation Failed","debug_token":"' + GH + '",' +
      '"authorization":"Bearer ' + GH + '","hint":"check your token"}';
    const h = makeHarness({
      initial: {},
      failNtfyPost: ntfyRefused(),
      dispatchHttp: { ok: false, status: 500, text: async () => leaky },
    });
    const r = await fire(w, h, { env: { GH_TOKEN: GH }, now: SLOT_INSTANT });
    assert.strictEqual(r.thrown, null);
    const put = h.kv.puts.find((p) => p.key === w.DIGEST_DISPATCH_STATUS_KEY);
    assert.ok(put, "nothing was persisted, so this test proves nothing");
    assert.ok(/Validation Failed/.test(put.value),
      "the body was not recorded at all, so this test is not measuring the " +
      "redaction it is named for. record=" + put.value);
    assert.ok(put.value.indexOf(GH) < 0,
      "THE SECRET IS IN THE PERSISTED RECORD. The token value appears in " +
      JSON.stringify(put.value) + " — that key is readable by anything with " +
      "KV read access and it carries the credential.");
    assert.ok(/<REDACTED>/.test(put.value),
      "nothing was redacted, so the redaction is not running. record=" +
      put.value);

    // And the same guarantee on the helper itself, so a future caller cannot
    // launder a token into a log line or another key by storing the result.
    const s = w.redactDispatchSummary(leaky, GH);
    assert.ok(s.indexOf(GH) < 0, "redactDispatchSummary leaked the token: " + s);
    for (const shaped of ["ghp_" + "a".repeat(36), "github_pat_" + "b".repeat(40)]) {
      assert.ok(w.redactDispatchSummary("x " + shaped + " y", "other").indexOf(
        shaped) < 0, "redactDispatchSummary leaked a GitHub-shaped token: " +
        shaped);
    }
  });

test("DEFECT 3: an ACCEPTED dispatch writes nothing, so the steady KV budget holds",
  async () => {
    // deploy/schedule.json budgets 108 KV writes/day and two tests derive that
    // figure. A telemetry write on the SUCCESS path would make both of them
    // wrong, and the two prebuild dispatches a day both succeed.
    const w = loadWorker();
    const h = makeHarness({ initial: {}, failNtfyPost: ntfyRefused() });
    const r = await fire(w, h, { env: { GH_TOKEN: GH }, now: SLOT_INSTANT });
    assert.strictEqual(r.thrown, null);
    assert.strictEqual(dispatchPosts(h.net).length, 1,
      "VACUITY GUARD: the dispatch must have been accepted for this to mean " +
      "anything");
    const keys = h.kv.puts.map((p) => p.key);
    assert.ok(keys.indexOf(w.DIGEST_DISPATCH_STATUS_KEY) < 0,
      "an ACCEPTED dispatch wrote telemetry. A success is already recorded by " +
      "actions_dispatch_at in the ledger, so this write is pure cost — and " +
      "deploy/schedule.json's 108/day would be describing something else. " +
      "Wrote " + JSON.stringify(keys));
  });

// ==========================================================================
// 4. The notice grew. Both ceilings re-measured, not asserted from a comment
// ==========================================================================

test("the notice grew, and both ceilings still hold", async () => {
  const w = loadWorker();
  const utf8 = (s) => Buffer.byteLength(String(s), "utf8");

  // The LONGEST note is the token-failure branch; measure every branch rather
  // than assuming which one it is.
  const notes = {
    dispatched:
      "\n\n**A fresh build is on its way.** Nothing was sealed in time for " +
      "this slot, so one was dispatched from here and will arrive late.",
    stand_down:
      "\n\n**Nothing is wrong — this slot is already being handled.** A build " +
      "for it was started at 2026-10-13T01:35:36Z, so this pass had nothing " +
      "left to do. There is no action to take and nothing to fix. This notice " +
      "is just the fallback for that same slot.",
    token_missing:
      "\n\n**No fresh build was started.** No GitHub token is configured on " +
      "this Worker, so the request to build one was never made. This is a " +
      "configuration fault, not a problem with the digest itself. Nothing was " +
      "dispatched, so this notice is the whole of what this slot produced.",
    token_401:
      "\n\n**No fresh build was started.** GitHub rejected the token (HTTP " +
      "401), so the token this Worker holds is invalid or expired. Only builds " +
      "are affected; the digest itself is fine. Nothing was dispatched, so " +
      "this notice is the whole of what this slot produced.",
    token_403:
      "\n\n**No fresh build was started.** GitHub refused the token (HTTP " +
      "403), so the token is valid but is not allowed to start workflows. " +
      "Only builds are affected; the digest itself is fine. Nothing was " +
      "dispatched, so this notice is the whole of what this slot produced.",
    transport:
      "\n\n**No fresh build was started.** The request to GitHub never " +
      "completed, so this is a network fault rather than a credential one — " +
      "nothing points at the token. Nothing was dispatched, so this notice is " +
      "the whole of what this slot produced.",
    http_500:
      "\n\n**No fresh build was started.** GitHub answered HTTP 500, so the " +
      "request was sent and answered and this is not a credential fault " +
      "either. Nothing was dispatched, so this notice is the whole of what " +
      "this slot produced.",
  };
  const longestName = Object.keys(notes).reduce(
    (a, b) => (utf8(notes[a]) >= utf8(notes[b]) ? a : b));
  const longest = utf8(notes[longestName]);
  console.log("longest dispatch note: " + longestName + " = " + longest +
    " bytes (was 133 for the single non-dispatch branch; every branch now " +
    "measured: " +
    Object.keys(notes).map((k) => k + "=" + utf8(notes[k])).join(" ") + ")");
  assert.ok(longest < 700,
    "the longest note is " + longest + " bytes, which is most of the 4096-byte " +
    "body budget spent on one sentence. It is placed directly under \"Why you " +
    "are seeing this\" precisely so it survives clipping, so it has to stay " +
    "small enough that a long last_good body cannot crowd it out.");

  // The TITLE is untouched by this change — the notes live in the body — and the
  // 72-character ceiling is re-asserted here so a future edit that moves text
  // into the title is caught against the real constant, not a comment. Driven
  // on the longest-rung shape (last_good), which is the one with the longest
  // title and the longest note together.
  const h = makeHarness({
    initial: incidentStore(),
    runs: [],       // not delivered -> the full ladder, which is the worst case
    cache: [],
  });
  const r = await fire(w, h, { env: { GH_TOKEN: GH } });
  assert.strictEqual(r.outcome.delivery_evidence,
    w.DELIVERY_EVIDENCE_NOT_DELIVERED);
  const b = bodies(h.net)[0];
  console.log("fallback title: " + JSON.stringify(b.title) + " = " +
    b.title.length + " chars");
  assert.ok(b.title.length <= 72,
    "the fallback title is " + b.title.length + " characters, past the " +
    "72-character ceiling: " + b.title);
});