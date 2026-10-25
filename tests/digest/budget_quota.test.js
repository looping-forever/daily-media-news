"use strict";
/**
 * tests/digest/budget_quota.test.js
 *
 * THE RUNTIME DISPATCH QUOTA, executed against the committed Worker.
 *
 * WHAT THIS IS. config/actions-budget.json is a STATIC gate: CI fails when the
 * tree's projection exceeds the hard ceiling. It cannot stop a month the tree
 * never predicted. deploy/cloudflare-worker.js now charges every dispatch it
 * makes against a per-month KV record and REFUSES once the month reaches the
 * hard ceiling. This suite proves the four properties that make that safe.
 *
 *   1. THE TWO NUMBERS ARE DISTINCT. The hard ceiling is 2000 allowance - 200
 *      reserve = 1800 and is ENFORCED; the 1400 design target is REPORTED only.
 *      A test that collapsed them would refuse work with headroom left.
 *
 *   2. THE REFUSAL ORDER IS DIGEST-LAST. At the margin -- only a digest fits --
 *      the DIGEST is allowed and the PREBUILD refused, never the reverse. This
 *      is the property the whole feature exists for: losing the digest is the
 *      one outcome the owner cannot accept.
 *
 *   3. THE ALERT FIRES ONCE PER MONTH PER REASON, not on every tick. A repeated
 *      alert is a muted channel, which is the same as no alert.
 *
 *   4. THE RACE ERRORS TOWARD ALLOWING. KV is eventually consistent, so a charge
 *      can be lost; the record then UNDER-counts. That errs toward allowing a
 *      dispatch, never toward refusing a digest that would have fitted.
 *
 *   5. ENFORCEMENT IS A SWITCH, AND IT IS OFF (2026-10-25). The repository is
 *      public, where standard runners are free, so the quota may not refuse.
 *      Everything else is kept and asserted: the charge, the month record, the
 *      `?health=1` figures and the refusal ORDER (still exercised, armed). The
 *      switch is exercised in BOTH positions below, and `budgetDecision`'s 4th
 *      argument is the ONLY difference between them -- that is what makes the
 *      flag, rather than a hidden second condition, the thing that decides.
 *
 * THE LOADER IS THE SAME ONE the sibling suites use: the committed Worker source,
 * `export default {` -> `const WORKER = {`, re-exported through a module shim. If
 * any name below stops existing the loader throws rather than testing nothing.
 *
 * RUN:
 *     node --test tests/digest/budget_quota.test.js
 *
 * REPRODUCING A RED WITHOUT TOUCHING THE COMMITTED WORKER:
 *     BUDGET_QUOTA_WORKER=<path> node --test <this file>
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const COMMITTED_WORKER = path.join(REPO_ROOT, "deploy", "cloudflare-worker.js");
const WORKER_PATH = process.env.BUDGET_QUOTA_WORKER || COMMITTED_WORKER;

if (process.env.BUDGET_QUOTA_WORKER) {
  process.stderr.write(
    "NOTE: judging " + WORKER_PATH + " (BUDGET_QUOTA_WORKER) INSTEAD of the " +
    "committed " + COMMITTED_WORKER + ".\n");
}

const EXPORT_NAMES = [
  "budgetDecision", "budgetChargeDispatch", "budgetHealthFields",
  "readBudgetMonth", "writeBudgetMonth", "budgetMonthKey", "isoUtc",
  "BUDGET_ALLOWANCE_MINUTES", "BUDGET_RESERVE_MINUTES",
  "BUDGET_HARD_CEILING_MINUTES", "BUDGET_DESIGN_TARGET_MINUTES",
  "BUDGET_JOB_MINUTES", "BUDGET_PROTECTED_JOBS",
  "BUDGET_MONTH_KEY_PREFIX", "BUDGET_FREEZE_CACHE_KEY",
  "BUDGET_REFUSED_KEY_PREFIX",
  "BUDGET_ENFORCEMENT_ENABLED", "BUDGET_ENFORCEMENT_REASON",
  "BUDGET_REPO_API_URL", "BUDGET_VISIBILITY_CACHE_KEY",
  "BUDGET_VISIBILITY_CACHE_TTL_S",
  "budgetEnforcementState", "readRepoIsPublic", "cachedRepoIsPublic",
  "fetchRepoIsPublic",
];

const loadCounter = { n: 0 };

function loadWorker() {
  const src = fs.readFileSync(WORKER_PATH, "utf8");
  if (src.indexOf("export default {") < 0) {
    throw new Error(
      "no `export default {` in " + WORKER_PATH + " -- this harness cannot " +
      "reach the Worker's entry points, so it would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "worker-budget-quota-"));
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
const CEIL = W.BUDGET_HARD_CEILING_MINUTES;

// A Map-backed KV. TTL is ignored: these tests assert the ACCOUNTING, not expiry.
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

// A fixed instant inside October 2026, so the month key is deterministic.
const MS = Date.parse("2026-10-09T01:35:00Z");

test("the two numbers are distinct and the per-target table is the documented one", () => {
  assert.strictEqual(W.BUDGET_ALLOWANCE_MINUTES, 2000);
  assert.strictEqual(W.BUDGET_RESERVE_MINUTES, 200);
  assert.strictEqual(
    W.BUDGET_HARD_CEILING_MINUTES,
    W.BUDGET_ALLOWANCE_MINUTES - W.BUDGET_RESERVE_MINUTES,
    "the hard ceiling must be allowance - reserve, or the reserve is not the margin");
  assert.strictEqual(W.BUDGET_HARD_CEILING_MINUTES, 1800);
  assert.strictEqual(W.BUDGET_DESIGN_TARGET_MINUTES, 1400);
  assert.ok(W.BUDGET_DESIGN_TARGET_MINUTES < W.BUDGET_HARD_CEILING_MINUTES,
    "the design target is an aspiration BELOW the enforced ceiling");
  assert.deepStrictEqual(W.BUDGET_JOB_MINUTES,
    { breaking: 1.6, academic: 2.1, prebuild: 1.5, digest: 6.1, watchdog: 1.0 });
  assert.deepStrictEqual(W.BUDGET_PROTECTED_JOBS, ["digest", "watchdog"]);
});

test("the month key is budget:YYYY-MM, taken from the instant", () => {
  assert.strictEqual(W.budgetMonthKey(MS), "budget:2026-10");
  assert.strictEqual(W.isoUtc(MS), "2026-10-09T01:35:00Z");
});

test("refusal fires AT the ceiling and NOT before", () => {
  // Just under: a breaking dispatch (1.6) fits, so it is allowed.
  assert.strictEqual(W.budgetDecision(1798.0, "breaking", false).allow, true,
    "1798 + 1.6 = 1799.6 <= 1800 must be allowed");
  // Exactly at the ceiling: 1800 + 1.6 > 1800, and it is not protected.
  assert.strictEqual(W.budgetDecision(1800.0, "breaking", false).allow, false,
    "a discretionary dispatch AT the ceiling must be refused");
  assert.strictEqual(W.budgetDecision(1800.0, "breaking", false).reason,
    "hard ceiling");
  // The EXACT boundary. "fits" is INCLUSIVE: a dispatch landing precisely on the
  // ceiling is ALLOWED, and one tick past it is REFUSED. 1798.5 + 1.5 = 1800.0
  // exactly (both are representable), so this pins the <= against an off-by-one.
  assert.strictEqual(W.budgetDecision(1798.5, "prebuild", false).allow, true,
    "1798.5 + 1.5 = 1800.0 must be ALLOWED -- the ceiling is inclusive");
  assert.strictEqual(W.budgetDecision(1798.6, "prebuild", false).allow, false,
    "1798.6 + 1.5 = 1800.1 must be REFUSED -- one tick past the ceiling");
});

test("the refusal ORDER is digest-last, proved by a case where only a digest fits", () => {
  const month = 1799.0; // room = 1.0
  const digest = W.budgetDecision(month, "digest", false);
  const prebuild = W.budgetDecision(month, "prebuild", false);
  assert.strictEqual(prebuild.allow, false,
    "a 1.5-min prebuild at 1799 (needs 1800.5) must be REFUSED");
  assert.strictEqual(digest.allow, true,
    "a 6.1-min digest at 1799 must be ALLOWED while the month is under the " +
    "ceiling -- the digest is refused LAST");
  // And the reverse never happens.
  assert.ok(!(digest.allow === false && prebuild.allow === true),
    "the prebuild must never be allowed while the digest is refused");
});

test("at or over the ceiling EVERYTHING is refused, digest included", () => {
  for (const job of ["breaking", "academic", "prebuild", "digest", "watchdog"]) {
    assert.strictEqual(W.budgetDecision(CEIL, job, false).allow, false,
      job + " must be refused once the month has reached the ceiling");
  }
});

test("the freeze flag short-circuits discretionary dispatches first", () => {
  const frozen = W.budgetDecision(0, "breaking", true);
  assert.strictEqual(frozen.allow, false);
  assert.strictEqual(frozen.reason, "freeze");
  // The digest is PROTECTED, so a freeze alone does not shed it.
  assert.strictEqual(W.budgetDecision(0, "digest", true).allow, true);
});

test("charges accumulate in the KV month record, persisted BEFORE the dispatch", async () => {
  const env = { KV: fakeKV({ "budget:freeze": "0" }), GH_TOKEN: "t" };
  const first = await W.budgetChargeDispatch(env, "breaking", MS);
  assert.strictEqual(first.allow, true);
  assert.strictEqual(await env.KV.get("budget:2026-10"),
    JSON.stringify({ minutes: 1.6, runs: 1, at: "2026-10-09T01:35:00Z" }),
    "the charge must be written before the caller dispatches, so a dispatch " +
    "that later FAILS is still billed");
  await W.budgetChargeDispatch(env, "academic", MS);
  const rec = JSON.parse(await env.KV.get("budget:2026-10"));
  assert.strictEqual(rec.minutes, 3.7); // 1.6 + 2.1
  assert.strictEqual(rec.runs, 2);
});

test("the alert fires ONCE per month per reason, not on every tick", async () => {
  const env = {
    KV: fakeKV({ "budget:freeze": "0", "budget:visibility": "0",
                 "budget:2026-10":
      JSON.stringify({ minutes: 1800, runs: 99 }) }),
    GH_TOKEN: "t",
    NTFY_SERVER: "https://ntfy.example",
    NTFY_TOPIC: "topic-xyz",
  };
  let ntfyPosts = 0;
  const realFetch = global.fetch;
  global.fetch = async (url) => {
    if (String(url).indexOf("ntfy.example") !== -1) {
      ntfyPosts += 1;
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ id: "m1" }) };
    }
    return { ok: true, status: 200, json: async () => [] };
  };
  try {
    // ARMED. Two refusals, SAME reason ("hard ceiling"). `true` is the flag's
    // position; the shipped position is false and is proved separately below.
    await W.budgetChargeDispatch(env, "breaking", MS, true);
    await W.budgetChargeDispatch(env, "academic", MS, true);
    assert.strictEqual(ntfyPosts, 1,
      "two refusals for the same month and reason must send ONE alert");
    // A DIFFERENT reason (freeze) is a separate marker, so it alerts again.
    env.KV.store.set("budget:freeze", "1");
    await W.budgetChargeDispatch(env, "breaking", MS, true);
    assert.strictEqual(ntfyPosts, 2,
      "a different refusal reason is a separate once-per-month marker");
  } finally {
    global.fetch = realFetch;
  }
});

test("a failed alert is retried, not burned (the marker is set only on success)", async () => {
  const env = {
    KV: fakeKV({ "budget:freeze": "0", "budget:visibility": "0",
                 "budget:2026-10":
      JSON.stringify({ minutes: 1800, runs: 1 }) }),
    GH_TOKEN: "t",
    NTFY_SERVER: "https://ntfy.example",
    NTFY_TOPIC: "topic-xyz",
  };
  let posts = 0;
  const realFetch = global.fetch;
  global.fetch = async () => {
    posts += 1;
    return { ok: false, status: 500, text: async () => "boom" };
  };
  try {
    // ARMED, or there is no refusal and therefore no alert to retry.
    await W.budgetChargeDispatch(env, "breaking", MS, true);
    assert.strictEqual(await env.KV.get("budget:refused:2026-10:hard ceiling"), null,
      "a FAILED send must leave no marker, so the alert is retried next tick");
    assert.ok(posts >= 1, "an attempt was actually made");
  } finally {
    global.fetch = realFetch;
  }
});

test("the race errs toward ALLOWING: a lost charge never turns into a refusal", async () => {
  const env = { KV: fakeKV({ "budget:freeze": "0" }), GH_TOKEN: "t" };
  await W.budgetChargeDispatch(env, "digest", MS);
  const afterFirst = JSON.parse(await env.KV.get("budget:2026-10"));
  assert.strictEqual(afterFirst.minutes, 6.1);
  // Simulate the lost update: the second tick read the SAME stale value and its
  // write overwrote nothing new (same starting point), so the record is unchanged.
  const stale = { minutes: 0, runs: 0 };
  const decision = W.budgetDecision(stale.minutes, "digest", false);
  assert.strictEqual(decision.allow, true,
    "an UNDER-counted record must err toward allowing, never toward refusing a " +
    "digest that would have fitted");
});

test("?health budget fields expose the freeze state and the month's charge", async () => {
  const env = {
    KV: fakeKV({ "budget:freeze": "1",
                 "budget:2026-10": JSON.stringify({ minutes: 1796.5, runs: 3 }) }),
    GH_TOKEN: "t",
  };
  const health = await W.budgetHealthFields(env, MS);
  assert.strictEqual(health.month, "2026-10");
  assert.strictEqual(health.hard_ceiling_minutes, 1800);
  assert.strictEqual(health.design_target_minutes, 1400);
  assert.strictEqual(health.minutes_charged, 1796.5);
  assert.strictEqual(health.runs, 3);
  assert.strictEqual(health.remaining_minutes, 3.5);
  assert.strictEqual(health.over_hard_ceiling, false);
  assert.strictEqual(health.freeze, true,
    "the status page must show the freeze flag even when Actions is unavailable");
  // THE SWITCH IS REPORTED TOO, so "is anything being refused?" is answerable
  // from the status page instead of from the source.
  assert.strictEqual(health.enforcement_enabled, false);
  assert.strictEqual(health.enforced, false);
  assert.strictEqual(health.enforcement_reason, W.BUDGET_ENFORCEMENT_REASON);
  assert.strictEqual(health.repo_public, null,
    "no cached visibility answer means unknown, never 'private'");
});

test("a missing KV binding never refuses a dispatch", async () => {
  const decision = await W.budgetChargeDispatch({ GH_TOKEN: "t" }, "digest", MS);
  assert.strictEqual(decision.allow, true,
    "with no KV the quota cannot count, so it must not refuse");
});

// --- THE ENFORCEMENT SWITCH (2026-10-25) -------------------------------------
// The repository is now PUBLIC, where standard runners are free, so the quota may
// not refuse a dispatch any more. The arithmetic, the charge, the month record and
// the reports are all kept and asserted above. What follows pins the switch in
// BOTH positions, and pins that the switch -- not some hidden second condition --
// is the only thing that separates them.

test("the enforcement switch is declared OFF, with a reason and a 6h probe", () => {
  assert.strictEqual(W.BUDGET_ENFORCEMENT_ENABLED, false,
    "the shipped position: the hard-ceiling refusal is off");
  assert.strictEqual(typeof W.BUDGET_ENFORCEMENT_REASON, "string");
  assert.ok(W.BUDGET_ENFORCEMENT_REASON.length > 0,
    "a switch that is off must say why, or it reads as an accident");
  assert.strictEqual(W.BUDGET_VISIBILITY_CACHE_TTL_S, 6 * 60 * 60,
    "the visibility probe's TTL is 6 hours, stated rather than implied");
  assert.strictEqual(W.BUDGET_VISIBILITY_CACHE_KEY, "budget:visibility");
  assert.ok(/^https:\/\/api\.github\.com\/repos\/[^/]+\/[^/]+$/.test(
    W.BUDGET_REPO_API_URL),
    "the probe must read the repository endpoint and nothing else: " +
    W.BUDGET_REPO_API_URL);
});

test("ENFORCEMENT OFF: nothing is refused, even far past the ceiling", async () => {
  for (const job of ["breaking", "academic", "prebuild", "digest", "watchdog"]) {
    const off = W.budgetDecision(99999, job, true, false);
    assert.strictEqual(off.allow, true,
      job + " must not be refused with enforcement off, even frozen and 50x over");
    assert.strictEqual(off.reason, "");
  }
  const env = {
    KV: fakeKV({ "budget:freeze": "1", "budget:visibility": "1",
                 "budget:2026-10": JSON.stringify({ minutes: 9000, runs: 500 }) }),
    GH_TOKEN: "t",
  };
  let ntfyPosts = 0;
  const realFetch = global.fetch;
  global.fetch = async (url) => {
    if (String(url).indexOf("ntfy") !== -1) ntfyPosts += 1;
    return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
  };
  try {
    const decision = await W.budgetChargeDispatch(env, "digest", MS, false);
    assert.strictEqual(decision.allow, true,
      "the dispatch that matters most must get through with enforcement off");
    assert.strictEqual(decision.enforcement.enforce, false);
    assert.strictEqual(ntfyPosts, 0,
      "budget_refused must never fire with enforcement off: no alert at all");
    assert.strictEqual(await env.KV.get("budget:refused:2026-10:freeze"), null);
    assert.strictEqual(await env.KV.get("budget:refused:2026-10:hard ceiling"), null);
    const rec = JSON.parse(await env.KV.get("budget:2026-10"));
    assert.strictEqual(rec.minutes, 9006.1,
      "the per-dispatch charge still lands, so the month record stays true");
    assert.strictEqual(rec.runs, 501);
  } finally {
    global.fetch = realFetch;
  }
});

test("ENFORCEMENT ON: the refusal fires in the established order, digest last", () => {
  assert.strictEqual(W.budgetDecision(99999, "breaking", true, true).allow, false);
  assert.strictEqual(W.budgetDecision(99999, "digest", true, true).allow, false);
  assert.strictEqual(W.budgetDecision(0, "breaking", true, true).reason, "freeze");
  assert.strictEqual(W.budgetDecision(0, "digest", true, true).allow, true,
    "the freeze arm still spares the digest and the watchdog");
  assert.strictEqual(W.budgetDecision(1799.0, "prebuild", false, true).allow, false);
  assert.strictEqual(W.budgetDecision(1799.0, "digest", false, true).allow, true,
    "at the margin the digest is allowed and the prebuild refused, never the reverse");
  assert.strictEqual(W.budgetDecision(1798.5, "prebuild", false, true).allow, true);
  assert.strictEqual(W.budgetDecision(1798.6, "prebuild", false, true).allow, false,
    "the ceiling is still inclusive, one tick past it still refuses");
});

test("the switch is the ONLY thing that changes the decision", async () => {
  const seed = { "budget:freeze": "0", "budget:visibility": "0",
                 "budget:2026-10": JSON.stringify({ minutes: 9000, runs: 5 }) };
  const armedEnv = { KV: fakeKV(seed), GH_TOKEN: "t" };
  const offEnv = { KV: fakeKV(seed), GH_TOKEN: "t" };
  const realFetch = global.fetch;
  let calls = 0;
  global.fetch = async () => {
    calls += 1;
    return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
  };
  try {
    const armed = await W.budgetChargeDispatch(armedEnv, "breaking", MS, true);
    const off = await W.budgetChargeDispatch(offEnv, "breaking", MS, false);
    assert.strictEqual(armed.allow, false, "armed: refused");
    assert.strictEqual(off.allow, true, "off: allowed");
    assert.strictEqual(armed.est, off.est,
      "same job, same estimate: the money the charge records cannot differ");
    assert.strictEqual(calls, 0,
      "both answers came from KV alone; a cached switch must not need the network");
    // The month record is charged on BOTH paths -- the accounting is not the gate.
    assert.strictEqual(JSON.parse(await armedEnv.KV.get("budget:2026-10")).minutes, 9000);
    assert.strictEqual(
      JSON.parse(await offEnv.KV.get("budget:2026-10")).minutes, 9001.6);
  } finally {
    global.fetch = realFetch;
  }
});

test("the visibility probe force-disables a FLAG-ON quota on a public repo", async () => {
  const kv = fakeKV({ "budget:freeze": "0" });
  const env = { KV: kv, GH_TOKEN: "t" };
  const realFetch = global.fetch;
  let calls = 0;
  global.fetch = async (url) => {
    calls += 1;
    assert.ok(String(url).indexOf("/repos/") !== -1,
      "the probe reads the repository endpoint: " + url);
    return { ok: true, status: 200, json: async () => ({ private: false }) };
  };
  try {
    const state = await W.budgetEnforcementState(env, true);
    assert.strictEqual(state.enforce, false,
      "a public repository is force-disarmed even with the flag on");
    assert.strictEqual(state.repo_public, true);
    assert.ok(state.enforcement_reason.indexOf("public") !== -1);
    assert.strictEqual(calls, 1, "one probe, not one per dispatch");
    assert.strictEqual(await kv.get("budget:visibility"), "1",
      "the answer is cached, so the next 6 hours cost no API call");
    await W.budgetEnforcementState(env, true);
    assert.strictEqual(calls, 1, "the second call read the cache");
  } finally {
    global.fetch = realFetch;
  }
});

test("a probe that cannot answer is NOT read as public: the flag still decides", async () => {
  const realFetch = global.fetch;
  const cases = [
    ["no GH_TOKEN", null, async () => { throw new Error("must not fetch"); }],
    ["non-200", "t", async () => ({ ok: false, status: 403, json: async () => ({}) })],
    ["no boolean private", "t", async () => ({ ok: true, status: 200,
                                               json: async () => ({}) })],
    ["transport throw", "t", async () => { throw new Error("boom"); }],
  ];
  try {
    for (const [label, token, stub] of cases) {
      global.fetch = stub;
      const env = { KV: fakeKV({}), GH_TOKEN: token };
      const state = await W.budgetEnforcementState(env, true);
      assert.strictEqual(state.enforce, true,
        label + ": an unanswerable probe must leave the FLAG in charge, or a " +
        "GitHub outage would silently disarm the guard");
      assert.strictEqual(state.repo_public, null);
      assert.strictEqual(await env.KV.get("budget:visibility"), null,
        label + ": an unknown answer must not be cached as either value");
    }
  } finally {
    global.fetch = realFetch;
  }
});

test("a CONFIRMED private answer keeps an armed quota armed", async () => {
  const env = { KV: fakeKV({ "budget:freeze": "0" }), GH_TOKEN: "t" };
  const realFetch = global.fetch;
  global.fetch = async () => ({ ok: true, status: 200,
                                json: async () => ({ private: true }) });
  try {
    const state = await W.budgetEnforcementState(env, true);
    assert.strictEqual(state.enforce, true);
    assert.strictEqual(state.repo_public, false);
    assert.strictEqual(state.enforcement_reason, "");
  } finally {
    global.fetch = realFetch;
  }
});


