"use strict";
/**
 * tests/digest/worker_token_expiry.test.js
 *
 * THE TOKEN'S OWN DEATH CERTIFICATE, EXECUTED. GitHub returns
 * `GitHub-Authentication-Token-Expiration` on any authenticated response made with
 * an expiring fine-grained PAT. The Worker already makes those calls, so the
 * expiry arrives free; what can be wrong is everything AFTER that: the day
 * arithmetic, what an absent header means, and whether the warning fires once a
 * month or once a minute.
 *
 * WHAT EACH TEST HOLDS DOWN:
 *   1. days-remaining compares UTC DATES. The boundary case is the whole point: a
 *      token expiring at 09:33 tomorrow, read at 23:00 tonight, is 10.5 hours away
 *      and a millisecond delta floors to 0 -- "expires today" -- when the truthful
 *      answer is 1. The test asserts the naive number really is 0, so it cannot
 *      pass by accident against an implementation that agrees with itself;
 *   2. the scale is exact and signed: +30, 0 today, -1 yesterday, never NaN;
 *   3. an unparseable or nonexistent date is null, and null never becomes NaN;
 *   4. with nothing observed, health says `unknown` with null figures -- it does
 *      not guess 0 days, which would read as "expiring now";
 *   5. a real header is cached RAW and health then reports the days remaining;
 *   6. an ABSENT header and an UNPARSEABLE header both leave a known record alone,
 *      so one odd response cannot erase a real expiry;
 *   7. the alert fires exactly once at the threshold and a second tick in the same
 *      calendar month does not re-fire;
 *   8. above the threshold nothing is sent AND no marker is burned;
 *   9. an unobserved token consumes NO dedupe key, so a later real observation
 *      still produces the warning it deserves;
 *  10. `?health=1` carries the repo slug (the cutover proof that was missing) and
 *      the nested token_expiry block;
 *  11. every GitHub repo URL in the Worker carries that same slug, so the value
 *      health reports cannot drift away from the URLs actually called.
 *
 * RUN:
 *     node --test tests/digest/worker_token_expiry.test.js
 *
 * REPRODUCING A RED WITHOUT TOUCHING THE COMMITTED WORKER:
 *     TOKEN_EXPIRY_WORKER=<path> node --test <this file>
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const COMMITTED_WORKER = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.TOKEN_EXPIRY_WORKER || COMMITTED_WORKER;

if (process.env.TOKEN_EXPIRY_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (TOKEN_EXPIRY_WORKER) INSTEAD of " +
    "the committed " + COMMITTED_WORKER + ".\n");
}

const EXPORT_NAMES = [
  "parseTokenExpiry", "daysUntilExpiry", "noteTokenExpiry", "readTokenExpiry",
  "tokenExpiryHealthFields", "alertTokenExpiryOnce", "healthForEndpoint",
  "GITHUB_REPO_SLUG", "TOKEN_EXPIRY_HEADER", "TOKEN_EXPIRY_CACHE_KEY",
  "TOKEN_EXPIRY_CACHE_TTL_S", "TOKEN_EXPIRY_ALERT_KEY_PREFIX",
  "TOKEN_EXPIRY_ALERT_DAYS", "TOKEN_EXPIRY_UNKNOWN",
  "DISPATCH_URL", "DIGEST_DISPATCH_URL", "BUDGET_REPO_API_URL",
  "BUDGET_FREEZE_LABEL_URL",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " -- this harness cannot " +
      "reach the Worker's entry points, so it would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-token-expiry-"));
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

// A Map-backed KV. TTL is not modelled: these tests assert what is STORED, and the
// 6h TTL is a constant the suite reads rather than a clock it simulates.
function fakeKV(seed) {
  const store = new Map(Object.entries(seed || {}));
  return {
    store,
    async get(key) {
      return store.has(key) ? store.get(key) : null;
    },
    async put(key, value) {
      store.set(key, String(value));
    },
  };
}

/** A response carrying exactly the headers given, lower-cased like the real thing. */
function fakeResponse(headers) {
  const map = {};
  for (const key of Object.keys(headers || {})) {
    map[key.toLowerCase()] = headers[key];
  }
  return {
    ok: true,
    status: 200,
    headers: {
      get(name) {
        const value = map[String(name).toLowerCase()];
        return value === undefined ? null : value;
      },
    },
  };
}

/** Count ntfy posts without reaching the network. */
function stubNtfy() {
  const posts = [];
  const realFetch = global.fetch;
  global.fetch = async (url, init) => {
    posts.push({ url: String(url), init: init || {} });
    return { ok: true, status: 200, text: async () => JSON.stringify({ id: "m1" }) };
  };
  return {
    posts,
    restore() { global.fetch = realFetch; },
  };
}

const ENV_SHELL = () => ({
  KV: fakeKV(),
  GH_TOKEN: "t",
  NTFY_SERVER: "https://ntfy.example",
  NTFY_TOPIC: "topic-xyz",
});

// 2026-10-25T23:00Z is the boundary instant: 09:33 the next morning is 10.5 hours
// away, so today's date and tomorrow's are one day apart and the clock is not.
const LATE_EVENING = Date.parse("2026-10-25T23:00:00Z");
const MIDDAY = Date.parse("2026-10-25T12:00:00Z");

test("days remaining compares UTC DATES: expiring tomorrow morning is 1, not 0", () => {
  const expires = "2026-10-26 09:33:12 UTC";
  assert.strictEqual(
    W.daysUntilExpiry(expires, LATE_EVENING), 1,
    "a token expiring at 09:33 tomorrow, read at 23:00 tonight, is ONE day away");

  // The test proves it is testing something: the naive calculation on the same two
  // instants really does truncate to 0, which is the defect being pinned.
  const naive = Math.floor(
    (Date.parse("2026-10-26T09:33:12Z") - LATE_EVENING) / 86400000);
  assert.strictEqual(
    naive, 0,
    "the naive millisecond division must still say 0 here, or this test is no " +
    "longer exercising the date-boundary defect it exists for");
});

test("days remaining is exact and signed, and never NaN", () => {
  assert.strictEqual(W.daysUntilExpiry("2026-11-24 09:33:12 UTC", MIDDAY), 30);
  assert.strictEqual(W.daysUntilExpiry("2026-10-25 01:00:00 UTC", MIDDAY), 0,
    "expiring earlier today is 0 days, not negative");
  assert.strictEqual(W.daysUntilExpiry("2026-10-24 23:00:00 UTC", MIDDAY), -1,
    "expired yesterday is -1, so the alert can say 'AGO'");
  const value = W.daysUntilExpiry("2026-10-25T01:00:00Z", MIDDAY);
  assert.strictEqual(value, 0, "the ISO 8601 shape is accepted too");
  assert.ok(!Number.isNaN(value));
});

test("an unparseable or nonexistent expiry is null, never NaN", () => {
  for (const bad of ["", "   ", "not-a-date", "2026-13-01 00:00:00 UTC",
                     "2026-02-31 00:00:00 UTC", null, undefined]) {
    const value = W.daysUntilExpiry(bad, MIDDAY);
    assert.strictEqual(value, null,
      JSON.stringify(bad) + " must be null, got " + JSON.stringify(value));
    assert.ok(!Number.isNaN(value),
      JSON.stringify(bad) + " must never produce NaN");
  }
  // 2026-02-31 is rolled forward to March by Date.UTC; the round-trip check is what
  // rejects it instead of silently reporting a March expiry.
  assert.strictEqual(W.parseTokenExpiry("2026-02-31 00:00:00 UTC"), null);
});

test("with nothing observed, health says unknown with null figures", async () => {
  const env = { KV: fakeKV() };
  const fields = await W.tokenExpiryHealthFields(env, MIDDAY);
  assert.deepStrictEqual(fields, {
    state: "unknown",
    expires_at: null,
    days_remaining: null,
    observed_age_s: null,
  }, "an unobserved token must read as unknown, never as '0 days left'");
  assert.strictEqual(W.TOKEN_EXPIRY_UNKNOWN, "unknown");
  assert.strictEqual(W.TOKEN_EXPIRY_CACHE_TTL_S, 6 * 60 * 60,
    "the stated worst-case staleness of ?health=1 is the cache TTL");
});

test("a real header is cached RAW, and health then reports days remaining", async () => {
  const env = ENV_SHELL();
  const header = "2026-11-24 09:33:12 UTC";
  await W.noteTokenExpiry(
    env, fakeResponse({ [W.TOKEN_EXPIRY_HEADER]: header }));

  const rec = JSON.parse(await env.KV.get(W.TOKEN_EXPIRY_CACHE_KEY));
  assert.strictEqual(rec.expires_at, header,
    "the RAW header is stored, not a reformatted date");
  assert.ok(typeof rec.observed_at === "string" && rec.observed_at.length > 0,
    "when it was seen is stored too, so staleness is reportable");

  const fields = await W.tokenExpiryHealthFields(env, MIDDAY);
  assert.strictEqual(fields.state, "known");
  assert.strictEqual(fields.expires_at, header);
  assert.strictEqual(fields.days_remaining, 30);
  assert.ok(typeof fields.observed_age_s === "number");
});

test("an absent or unparseable header leaves a known record untouched", async () => {
  const env = ENV_SHELL();
  const header = "2026-11-24 09:33:12 UTC";
  await W.noteTokenExpiry(
    env, fakeResponse({ [W.TOKEN_EXPIRY_HEADER]: header }));
  const before = await env.KV.get(W.TOKEN_EXPIRY_CACHE_KEY);

  await W.noteTokenExpiry(env, fakeResponse({}));
  assert.strictEqual(await env.KV.get(W.TOKEN_EXPIRY_CACHE_KEY), before,
    "a response with no header (non-expiring token, or an error body) must not " +
    "erase an expiry already observed");

  await W.noteTokenExpiry(
    env, fakeResponse({ [W.TOKEN_EXPIRY_HEADER]: "garbage" }));
  assert.strictEqual(await env.KV.get(W.TOKEN_EXPIRY_CACHE_KEY), before,
    "an unparseable header must not overwrite a good record");

  const fields = await W.tokenExpiryHealthFields(env, MIDDAY);
  assert.strictEqual(fields.state, "known");
  assert.strictEqual(fields.days_remaining, 30);
});

test("the alert fires exactly once at the threshold; a second tick does not re-fire",
  async () => {
    const env = ENV_SHELL();
    // Exactly 14 days: the threshold is inclusive.
    await env.KV.put(W.TOKEN_EXPIRY_CACHE_KEY, JSON.stringify({
      expires_at: "2026-11-08 09:00:00 UTC",
      observed_at: "2026-10-25T12:00:00Z",
    }));
    assert.strictEqual(W.daysUntilExpiry("2026-11-08 09:00:00 UTC", MIDDAY), 14,
      "the fixture must really be ON the threshold");

    const ntfy = stubNtfy();
    try {
      assert.strictEqual(await W.alertTokenExpiryOnce(env, MIDDAY), true,
        "the first tick at the threshold must send");
      assert.strictEqual(ntfy.posts.length, 1);
      const marker = await env.KV.get(W.TOKEN_EXPIRY_ALERT_KEY_PREFIX + "2026-10");
      assert.ok(marker, "a successful send must burn the month's marker");

      assert.strictEqual(await W.alertTokenExpiryOnce(env, MIDDAY + 60000), false,
        "the second tick in the same month must NOT send again");
      assert.strictEqual(await W.alertTokenExpiryOnce(env, MIDDAY + 120000), false);
      assert.strictEqual(ntfy.posts.length, 1,
        "exactly one post for the whole month, not one per tick");
    } finally {
      ntfy.restore();
    }
  });

test("no alert above the threshold, and no marker is burned", async () => {
  const env = ENV_SHELL();
  await env.KV.put(W.TOKEN_EXPIRY_CACHE_KEY, JSON.stringify({
    expires_at: "2026-11-09 09:00:00 UTC",   // 15 days
    observed_at: "2026-10-25T12:00:00Z",
  }));
  assert.strictEqual(W.daysUntilExpiry("2026-11-09 09:00:00 UTC", MIDDAY), 15);

  const ntfy = stubNtfy();
  try {
    assert.strictEqual(await W.alertTokenExpiryOnce(env, MIDDAY), false,
      "15 days out must not alert");
    assert.strictEqual(ntfy.posts.length, 0);
    assert.strictEqual(
      await env.KV.get(W.TOKEN_EXPIRY_ALERT_KEY_PREFIX + "2026-10"), null,
      "above the threshold nothing is sent AND no dedupe key is consumed");
  } finally {
    ntfy.restore();
  }
});

test("an unobserved token consumes NO dedupe key, so a later real value still alerts",
  async () => {
    const env = ENV_SHELL();
    const ntfy = stubNtfy();
    try {
      assert.strictEqual(await W.alertTokenExpiryOnce(env, MIDDAY), false,
        "unknown must not alert");
      assert.strictEqual(ntfy.posts.length, 0);
      assert.strictEqual(
        await env.KV.get(W.TOKEN_EXPIRY_ALERT_KEY_PREFIX + "2026-10"), null,
        "unknown must NOT burn the month's key -- burning it would silence the " +
        "one alert that matters when the record finally arrives");

      // Now the observation lands, ten days out: the warning must still fire.
      await env.KV.put(W.TOKEN_EXPIRY_CACHE_KEY, JSON.stringify({
        expires_at: "2026-11-04 09:00:00 UTC",
        observed_at: "2026-10-25T12:00:00Z",
      }));
      assert.strictEqual(await W.alertTokenExpiryOnce(env, MIDDAY), true,
        "a real value inside the window still alerts after an unknown");
      assert.strictEqual(ntfy.posts.length, 1);
    } finally {
      ntfy.restore();
    }
  });

test("?health=1 carries the repo slug and the token_expiry block", async () => {
  const env = { KV: fakeKV() };
  const health = await W.healthForEndpoint(env);
  assert.strictEqual(health.repo, "looping-forever/daily-media-news",
    "health must state the dispatch target, which is what the 2026-10-25 " +
    "cutover could not prove from health at all");
  assert.deepStrictEqual(Object.keys(health.token_expiry).sort(),
    ["days_remaining", "expires_at", "observed_age_s", "state"]);
  assert.strictEqual(health.token_expiry.state, "unknown");
  assert.strictEqual(health.token_expiry.days_remaining, null);
  // The rest of the payload must survive the addition.
  assert.ok(typeof health.build === "string" && health.build.length > 0);
  assert.ok(health.budget && typeof health.budget.enforcement_enabled === "boolean");
});

test("every GitHub repo URL in the Worker carries the slug health reports", () => {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  const urls = src.match(/https:\/\/api\.github\.com\/repos\/[^"'\s]+/g) || [];
  assert.ok(urls.length >= 4,
    "expected several repo URLs in the Worker, found " + urls.length);
  // The API URL for the repository itself legitimately has no trailing path, so the
  // slug must be the WHOLE of what follows /repos/, optionally followed by a path.
  const base = "https://api.github.com/repos/" + W.GITHUB_REPO_SLUG;
  for (const url of urls) {
    const tail = url.slice(base.length);
    assert.ok(url.indexOf(base) === 0 && (tail === "" || tail[0] === "/"),
      "this URL does not target the repository health reports (" +
      W.GITHUB_REPO_SLUG + "): " + url);
  }
  assert.strictEqual(src.indexOf("alish-3"), -1,
    "the old repository must appear nowhere in the Worker");
  assert.strictEqual(W.DISPATCH_URL.indexOf(W.GITHUB_REPO_SLUG) > 0, true);
  assert.strictEqual(W.DIGEST_DISPATCH_URL.indexOf(W.GITHUB_REPO_SLUG) > 0, true);
  assert.strictEqual(W.BUDGET_REPO_API_URL.indexOf(W.GITHUB_REPO_SLUG) > 0, true);
});
