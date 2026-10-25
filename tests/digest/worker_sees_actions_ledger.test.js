"use strict";
/**
 * tests/digest/worker_sees_actions_ledger.test.js
 *
 * GUARD 2, AND IT IS THE HALF PYTHON CANNOT PROVE.
 *
 * `tests/digest/test_duplicate_detector.py` execs the real ntfy step of
 * `.github/workflows/daily-news.yml` with `urllib.request.urlopen` poisoned,
 * captures the bytes that step really PUT to Cloudflare KV, writes them to a
 * file, and runs THIS file with that path in `ACTIONS_LEDGER_FILE`.
 *
 * WHY THE ROUND TRIP AND NOT TWO SEPARATE TESTS
 * ---------------------------------------------
 * The defect was that two arms wrote and read a ledger they did not share. A
 * Python-only test can prove the Actions arm writes `ntfy_sent_at`; a JS-only
 * test would prove the Worker honours `ntfy_sent_at` — against a FIXTURE the
 * test author typed. Both halves would be green while the real bytes disagreed,
 * which is precisely how this stayed broken: `state/last_digest_sent.json` was
 * written faithfully for months by a test that could not see that the Worker
 * never read it.
 *
 * So this reads the ledger the Actions arm really produced. There is no
 * fixture in this file, which is the point.
 *
 * RUN:   node --test tests/digest/worker_sees_actions_ledger.test.js
 *        Requires ACTIONS_LEDGER_FILE. Without it every test below fails
 *        loudly rather than skipping: a skip is how a cross-language contract
 *        quietly stops being checked.
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const WORKER_PATH = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const LEDGER_FILE = process.env.ACTIONS_LEDGER_FILE;

const EXPORT_NAMES = [
  "WORKER", "deliverDigest", "commitDigestLedger", "normaliseDigestLedger",
  "validateArtifact", "DIGEST_LEDGER_KEY", "DIGEST_ARTIFACT_PREFIX",
  "DIGEST_CRON_ARM", "CRON_DIGEST_MORNING", "CRON_DIGEST_EVENING",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-handoff-"));
  const file = path.join(dir, "worker-" + (loadCounter.n++) + ".cjs");
  fs.writeFileSync(file,
    src.replace("export default {", "const WORKER = {") +
    "\nmodule.exports = { " + EXPORT_NAMES.join(", ") + " };\n", "utf8");
  const mod = require(file);
  fs.rmSync(dir, { recursive: true, force: true });
  return mod;
}

function actionsLedger() {
  assert.ok(LEDGER_FILE,
    "ACTIONS_LEDGER_FILE is unset. This suite has no fixture on purpose: it " +
    "must be handed the ledger the Actions arm really wrote, or it is not " +
    "testing the handoff.");
  assert.ok(fs.existsSync(LEDGER_FILE),
    "the Actions step did not produce a ledger at " + LEDGER_FILE);
  return JSON.parse(fs.readFileSync(LEDGER_FILE, "utf8"));
}

const NTFY_SERVER = "https://ntfy.example.invalid";
const TOPIC = "dmn-secret-topic-7f3a91c4";
const MIN = 60 * 1000;
const T0 = Date.parse("2026-10-13T13:36:00Z");
const ARCHIVE_PAGE = "https://daily-digest.example.pages.dev/digests/2026-10-13-evening.html";

function makeFetch(calls) {
  globalThis.fetch = async function (url, init) {
    const method = String((init && init.method) || "GET").toUpperCase();
    calls.push({ method: method, url: String(url) });
    if (method === "POST") {
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ id: "must-not-happen" }) };
    }
    return { ok: true, status: 200, text: async () => "" };
  };
  return calls;
}

function fakeKV(initial) {
  const store = Object.assign({}, initial || {});
  return { store: store, puts: [],
           async get(k) {
             return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null;
           },
           async put(k, v) { this.puts.push({ key: k, value: v }); store[k] = v; } };
}

// ==========================================================================
// 1. THE WORKER READS THE ACTIONS ARM'S LEDGER AND STANDS DOWN
// ==========================================================================

test("the Worker's guard sees an Actions delivery and suppresses its own push", async () => {
  const { deliverDigest, DIGEST_LEDGER_KEY } = loadWorker();
  const ledger = actionsLedger();

  // THE SCENARIO, EXACTLY. The Actions arm pushed the evening digest at 19:25
  // NPT and recorded it. Then the Worker's 13:55Z re-fire woke up with its OWN
  // view: an artifact on disk and a topic read-back that cannot see the Actions
  // push. That re-fire is what published the older 18:44 artifact over the top
  // of the notification the owner had already read.
  const kv = fakeKV({
    [DIGEST_LEDGER_KEY]: JSON.stringify(ledger),
    "digest:artifact:2026-10-13:evening": JSON.stringify({
      schema_version: 1,
      date: "2026-10-13",
      slot: "evening",
      built_at: new Date(T0 - 4 * MIN).toISOString(),
      page_url: ARCHIVE_PAGE,
      is_partial: false,
      ntfy: { title: "Evening Digest - 18:44 - 2026-10-13 - 3 items",
              body: "## Headlines\n\n* [GAMING] BLEACH" },
    }),
  });
  const calls = makeFetch([]);
  const res = await deliverDigest({ KV: kv, NTFY_SERVER: NTFY_SERVER,
                                    NTFY_TOPIC: TOPIC, WORKER_BUILD: "testbuild" },
                                  "evening", { now: () => T0 });
  const posts = calls.filter((c) => c.method === "POST");
  assert.strictEqual(posts.length, 0,
    "the Worker, handed the ledger the Actions arm really wrote, published " +
    "again. This is the 19:40 push that replaced the owner's read " +
    "notification: " + JSON.stringify(res));
  assert.strictEqual(res.pushed, false, JSON.stringify(res));
});

test("the ledger the Actions arm wrote is shaped the way the Worker reads it", async () => {
  const { normaliseDigestLedger, DIGEST_LEDGER_KEY } = loadWorker();
  const raw = JSON.stringify(actionsLedger());
  assert.ok(raw.indexOf(DIGEST_LEDGER_KEY) < 0, "fixture sanity");
  const ledger = normaliseDigestLedger(raw);
  const day = (ledger.days || {})["2026-10-13"] || {};
  const entry = day.evening;
  assert.ok(entry, "the Worker cannot find a 2026-10-13 evening entry in what " +
                   "the Actions arm wrote: " + raw);
  // `ntfy_sent_at` is THE field the Worker's ledger guard reads. A record
  // without it is a record the guard walks straight past -- which is why this
  // is asserted by name rather than inferred from the suppression above.
  assert.ok(entry.ntfy_sent_at,
    "the Actions arm recorded no ntfy_sent_at, which is the exact field the " +
    "Worker's duplicate guard reads: " + raw);
  assert.strictEqual(entry.sequence_id, "dmn-2026-10-13-evening",
    "the recorded sequence_id does not match the slot identity both arms " +
    "derive from the same two inputs: " + raw);
});

// ==========================================================================
// 2. AND IT STILL DELIVERS WHEN THERE IS NO ACTIONS DELIVERY
// ==========================================================================

test("the Worker's guard does NOT suppress a slot the Actions arm never touched", async () => {
  const { deliverDigest } = loadWorker();
  // A DIFFERENT day in the ledger, so the 2026-10-13 evening slot is untouched
  // by it. This is the control: a guard that suppresses unconditionally would
  // pass the test above while the owner stopped receiving digests entirely.
  const kv = fakeKV({
    "digest:ledger": JSON.stringify({
      schema_version: 1,
      days: { "2026-10-12": { evening: { ntfy_sent_at: "2026-10-12T13:30:00Z",
                                         sequence_id: "dmn-2026-10-12-evening" } } },
    }),
    "digest:artifact:2026-10-13:evening": JSON.stringify({
      schema_version: 1, date: "2026-10-13", slot: "evening",
      built_at: new Date(T0 - 4 * MIN).toISOString(), page_url: ARCHIVE_PAGE,
      is_partial: false,
      ntfy: { title: "Evening Digest - 2026-10-13 - 3 items", body: "## Headlines" },
    }),
  });
  const calls = makeFetch([]);
  const res = await deliverDigest({ KV: kv, NTFY_SERVER: NTFY_SERVER,
                                    NTFY_TOPIC: TOPIC, WORKER_BUILD: "testbuild" },
                                  "evening", { now: () => T0 });
  assert.strictEqual(calls.filter((c) => c.method === "POST").length, 1,
    "yesterday's delivery suppressed today's digest. A duplicate is the accepted " +
    "cost of this guard; a silent miss is not: " + JSON.stringify(res));
});