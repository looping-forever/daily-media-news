"use strict";
/**
 * tests/digest/digest_duplicate_detector.test.js
 *
 * GUARD 1, ON THE WORKER SIDE. The ntfy sequence_id read-back, executed.
 *
 * WHAT WAS DEAD
 * -------------
 * `findDeliveredBySequenceId` read the cached message set back with
 * `GET <server>/<topic>/json?poll=1&since=all` and then ran `JSON.parse` over
 * the WHOLE body. ntfy answers that request with
 * `Content-Type: application/x-ndjson; charset=utf-8` — newline-delimited
 * JSON, one complete message per line — and with a ZERO-BYTE body when the
 * topic holds nothing. Measured 2026-10-13 against ntfy.sh with a generated,
 * non-existent topic: HTTP 200, ctype=application/x-ndjson, bodybytes=0.
 *
 * So the parse threw on every call, for every topic, at every hour: a populated
 * topic is many objects separated by newlines (not JSON) and an empty topic is
 * "" (not JSON either). Both landed in the catch, which returns `unknown` —
 * and the sole consequence of `unknown` is "a push always happens". This guard
 * has never once suppressed anything.
 *
 * WHY THIS IS JS AND NOT PYTHON
 * -----------------------------
 * The behaviour is a property of JavaScript that only running observes: what
 * `JSON.parse` throws on `""`, and whether a guard reached through the real
 * cron arm really stops the POST. Python would re-implement the path and agree
 * with itself for ever.
 *
 * WHERE IT LIVES AND WHY
 * ----------------------
 * `tests/digest/` is where the digest's delivery contract already lives
 * (`digest_delivery.test.js`, `digest_prebuild_cron.test.js`). The loader, the
 * fake KV and the log capture below are copied from `digest_delivery.test.js`
 * so a reader who has met one has met both.
 *
 * RUN:  node --test tests/digest/digest_duplicate_detector.test.js
 * MUTATE: DIGEST_DELIVERY_WORKER=<path to another cloudflare-worker.js>
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
  process.stderr.write("NOTE: judging " + WORKER_PATH + " INSTEAD of " +
                       COMMITTED_WORKER + "\n");
}

const EXPORT_NAMES = [
  "WORKER", "deliverDigest", "deliverDigestPlan", "commitDigestLedger",
  "validateArtifact", "findDeliveredBySequenceId", "normaliseDigestLedger",
  "digestSequenceId", "digestFullSequenceId",
  "DIGEST_LEDGER_KEY", "DIGEST_ARTIFACT_PREFIX", "DIGEST_SCHEMA_VERSION",
  "KIND_DELIVERED", "KIND_DUPLICATE_SUPPRESSED", "STATE_SENT", "STATE_PENDING",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error("no `export default {` in " + WORKER_PATH +
                    " — this suite would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-dupe-"));
  const file = path.join(dir, "worker-" + (loadCounter.n++) + ".cjs");
  const shim = "\nmodule.exports = { " + EXPORT_NAMES.join(", ") + " };\n";
  fs.writeFileSync(file, src.replace("export default {", "const WORKER = {") + shim,
                  "utf8");
  let mod;
  try {
    mod = require(file);
  } catch (err) {
    throw new Error("loading the Worker harness failed: " + String(err && err.message || err));
  }
  fs.rmSync(dir, { recursive: true, force: true });
  return mod;
}

// ---------------------------------------------------------------------------
// Fixtures. 2026-10-13 evening, because that is the day this file is about.
// Nepal is UTC+05:45 with no DST, so the slot instant and the date are exact.
// ---------------------------------------------------------------------------

/** 2026-10-13T13:25:05Z = 19:00:05 NPT... the two real pushes, as ntfy reported them.
 *  The sequence id is identical in both: `dmn-2026-10-13-evening`. */
const TODAY = "2026-10-13";
const SLOT = "evening";
const SEQ = "dmn-2026-10-13-evening";

const PUSH_1925 = {
  id: "ohOaYDwxpqvq",
  time: 1791951905,
  event: "message",
  topic: "redacted-in-tests",
  sequence_id: SEQ,
  title: "Evening Digest - 19:25 - 2026-10-13 - 3 items",
};
const PUSH_1940 = {
  id: "pMq3dJt2R7Xk",
  time: 1791952838,
  event: "message",
  topic: "redacted-in-tests",
  sequence_id: SEQ,
  title: "Evening Digest - 18:44 - 2026-10-13 - 3 items",
};

const NTFY_SERVER = "https://ntfy.example.invalid";
const TOPIC = "dmn-secret-topic-7f3a91c4";
const MIN = 60 * 1000;
/** The slot's scheduled instant: CRON_DIGEST_EVENING 13:35Z. */
const T0 = Date.parse("2026-10-13T13:36:00Z");
const ARCHIVE_PAGE = "https://daily-digest.example.pages.dev/digests/2026-10-13-evening.html";

/** What ntfy actually serves: one JSON object per line, no enclosing array. */
function ndjson(messages) {
  return messages.map((m) => JSON.stringify(m)).join("\n");
}

/** A fake network whose ONLY GET is the topic read-back. */
function makeFetch(o) {
  const opts = o || {};
  const calls = [];
  globalThis.fetch = async function (url, init) {
    const method = String((init && init.method) || "GET").toUpperCase();
    calls.push({ method: method, url: String(url),
                 headers: (init && init.headers) || {}, body: init && init.body });
    if (method === "POST") {
      if (opts.postStatus && opts.postStatus !== 200) {
        return { ok: false, status: opts.postStatus, text: async () => "no" };
      }
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ id: "ntfy-msg-pushed" }) };
    }
    if (opts.readbackStatus) {
      return { ok: false, status: opts.readbackStatus, text: async () => "err" };
    }
    if (opts.failReadback) {
      throw new Error("injected read-back transport failure");
    }
    // `body` is the RAW bytes ntfy would send. Default: a zero-byte body,
    // which is what it answers for a topic with nothing cached.
    return { ok: true, status: 200, text: async () => (opts.body === undefined ? "" : opts.body) };
  };
  return calls;
}

function artifact(overrides) {
  const base = {
    schema_version: 1,
    date: TODAY,
    slot: SLOT,
    built_at: new Date(T0 - 4 * MIN).toISOString(),
    page_url: ARCHIVE_PAGE,
    is_partial: false,
    ntfy: { title: "Evening Digest - 19:24 - 2026-10-13 - 3 items",
            body: "## Headlines\n\n* [WORLD] Something happened" },
  };
  return JSON.stringify(Object.assign({}, base, overrides || {}));
}

function env(kv, extra) {
  return Object.assign({
    KV: kv, NTFY_SERVER: NTFY_SERVER, NTFY_TOPIC: TOPIC,
    WORKER_BUILD: "testbuild",
  }, extra || {});
}

function fakeKV(initial) {
  const store = Object.assign({}, initial || {});
  return {
    store: store, puts: [], gets: [],
    async get(key) {
      this.gets.push(key);
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    async put(key, value) {
      this.puts.push({ key: key, value: value });
      store[key] = value;
    },
  };
}

// ==========================================================================
// 1. THE PARSER
// ==========================================================================

test("a ZERO-BYTE history is an empty history, not a parse failure", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: "" });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  // The decisive field is `unknown`. Unknown means "push anyway", so a zero-byte
  // body reported as unknown makes the most common answer of all -- an empty
  // topic, which is the ordinary first delivery of the day -- indistinguishable
  // from a broken guard.
  assert.strictEqual(got.unknown, false,
    "an empty body was reported as unknown, so the guard answers 'push' for " +
    "the single most common server response");
  assert.strictEqual(got.found, false, got);
  assert.strictEqual(got.cached, 0, "an empty body was not read as 0 messages");
});

test("NDJSON with the sequence id in it IS found", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: ndjson([PUSH_1940, PUSH_1925]) });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.found, true,
    "two messages carrying " + SEQ + " were on the topic and the read-back " +
    "did not find them. This is the guard that was structurally dead.");
  assert.strictEqual(got.unknown, false);
  // Both real pushes carry the SAME sequence id, so either one is a correct
  // answer -- what matters is that one of them matched at all. Asserting a
  // specific id would pin an implementation detail (first match wins) that is
  // not part of the contract, and would fail on a perfectly good reordering.
  assert.ok([PUSH_1925.id, PUSH_1940.id].indexOf(got.id) >= 0,
    "it matched a message that is not one of the two on the topic: " + got.id);
  assert.strictEqual(got.cached, 2, "it did not count both lines");
});

test("NDJSON without the sequence id is read and reports nothing found", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: ndjson([
    Object.assign({}, PUSH_1925, { id: "a", sequence_id: "dmn-2026-10-13-morning" }),
    Object.assign({}, PUSH_1925, { id: "b", sequence_id: "dmn-2026-10-12-evening" }),
  ]) });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.found, false, "another slot's message suppressed this one");
  assert.strictEqual(got.unknown, false, got);
  assert.strictEqual(got.cached, 2, "it did not count both lines: " + JSON.stringify(got));
});

test("a single NDJSON line is found", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: ndjson([PUSH_1925]) });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.found, true, JSON.stringify(got));
});

test("a single pretty-printed JSON array is still read", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: JSON.stringify([PUSH_1940, PUSH_1925], null, 2) });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.found, true,
    "a pretty-printed single document blinded the guard. ntfy serves NDJSON, " +
    "but a proxy or a server-side tidy must not put it back to dead.");
});

test('a {"messages": [...]} body is still read', async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: JSON.stringify({ messages: [PUSH_1925] }) });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.found, true, JSON.stringify(got));
});

test("a genuinely unparseable body stays UNKNOWN and still pushes", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: "<html>gateway error</html>" });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.found, false, got);
  assert.strictEqual(got.unknown, true,
    "a body that is neither NDJSON nor JSON reported a definite 'not found'. " +
    "That is the read-back guessing, and a guess that suppresses is a silent miss.");
});

test("an HTTP failure is UNKNOWN, never a definite not-found", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ readbackStatus: 502 });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.unknown, true, JSON.stringify(got));
});

test("a transport failure is UNKNOWN", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ failReadback: true });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.strictEqual(got.unknown, true, JSON.stringify(got));
});

// ==========================================================================
// 2. THE TRAP: `poll=1` MUST STAY
// ==========================================================================

test("the read-back keeps poll=1, or it becomes an unbounded long-poll", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  const calls = makeFetch({ body: ndjson([PUSH_1925]) });
  await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  const readback = calls.filter((c) => c.url.indexOf("/json") >= 0);
  assert.strictEqual(readback.length, 1,
    "expected exactly one topic read-back, got " + JSON.stringify(readback));
  assert.ok(readback[0].url.indexOf("poll=1") >= 0,
    "the read-back dropped poll=1. GET /json without it is a LONG-POLL: ntfy " +
    "holds the connection open until a message arrives. On a topic that gets a " +
    "handful of messages a day that is not a cache read, it is a cron that " +
    "never returns: " + readback[0].url);
  assert.ok(readback[0].url.indexOf("since=all") >= 0, readback[0].url);
});

// ==========================================================================
// 3. THE GUARD CAN NOW ACTUALLY SUPPRESS THE PUSH
// ==========================================================================

test("the 2026-10-13 evening duplicate is SUPPRESSED end to end", async () => {
  const { deliverDigest } = loadWorker();
  // The ledger is BLIND -- this is exactly the 2026-10-13 state, because the
  // Actions arm recorded its delivery in the committed marker and nowhere the
  // Worker reads. Only the read-back stands between this run and a duplicate.
  const kv = fakeKV({
    "digest:artifact:2026-10-13:evening": artifact(),
  });
  const calls = makeFetch({ body: ndjson([PUSH_1925]) });
  const res = await deliverDigest(env(kv), SLOT, { now: () => T0 });
  const posts = calls.filter((c) => c.method === "POST");
  assert.strictEqual(posts.length, 0,
    "a slot already carrying " + SEQ + " on the topic was published again. " +
    "That push would have REPLACED the notification the owner had already " +
    "read -- ntfy replaces a message it has already seen under a sequence id. " +
    JSON.stringify(res));
  assert.strictEqual(res.pushed, false, JSON.stringify(res));
});

test("the HAPPY PATH still pushes, so the test above is not vacuous", async () => {
  const { deliverDigest } = loadWorker();
  const kv = fakeKV({ "digest:artifact:2026-10-13:evening": artifact() });
  const calls = makeFetch({ body: "" });
  const res = await deliverDigest(env(kv), SLOT, { now: () => T0 });
  const posts = calls.filter((c) => c.method === "POST");
  assert.strictEqual(posts.length, 1,
    "an EMPTY topic published nothing. A guard that never sends anything " +
    "would pass the suppression test above for the wrong reason: " +
    JSON.stringify(res));
  assert.strictEqual(res.pushed, true, JSON.stringify(res));
});

test("a topic carrying a DIFFERENT slot still publishes", async () => {
  const { deliverDigest } = loadWorker();
  const kv = fakeKV({ "digest:artifact:2026-10-13:evening": artifact() });
  const calls = makeFetch({ body: ndjson([
    Object.assign({}, PUSH_1925, { id: "z", sequence_id: "dmn-2026-10-13-morning" }),
  ]) });
  await deliverDigest(env(kv), SLOT, { now: () => T0 });
  assert.strictEqual(calls.filter((c) => c.method === "POST").length, 1,
    "another slot's message on the topic suppressed this slot");
});

test("an unreadable read-back still PUSHES -- a miss is the worse outcome", async () => {
  const { deliverDigest } = loadWorker();
  const kv = fakeKV({ "digest:artifact:2026-10-13:evening": artifact() });
  const calls = makeFetch({ failReadback: true });
  const res = await deliverDigest(env(kv), SLOT, { now: () => T0 });
  assert.strictEqual(calls.filter((c) => c.method === "POST").length, 1,
    "a transport failure turned into a silent miss: " + JSON.stringify(res));
});

test("the topic value never appears in the read-back's result", async () => {
  const { findDeliveredBySequenceId } = loadWorker();
  makeFetch({ body: ndjson([PUSH_1925]) });
  const got = await findDeliveredBySequenceId(env(fakeKV()), SEQ);
  assert.ok(JSON.stringify(got).indexOf(TOPIC) < 0,
    "the read-back result carried the topic value: " + JSON.stringify(got));
});

if (process.env.DIGEST_DELIVERY_WORKER) {
  test("the mutation target really is a different file", () => {
    assert.notStrictEqual(path.resolve(WORKER_PATH),
                          path.resolve(COMMITTED_WORKER));
  });
}