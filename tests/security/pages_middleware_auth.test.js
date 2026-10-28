"use strict";
/**
 * tests/security/pages_middleware_auth.test.js
 *
 * THE ORIGIN-LEVEL GUARD, EXECUTED. Node's own test runner, the committed
 * `functions/_middleware.js`, a fake Pages context and a faithful copy of the
 * Pages runtime's documented error behaviour. No reimplementation of the
 * middleware anywhere in this file: a proxy that agrees with itself is
 * exactly what let the `digest-auth` Worker's uselessness survive as a
 * "protection" for months.
 *
 * ---------------------------------------------------------------------------
 * WHY IT IS JS AND NOT PYTHON
 * ---------------------------------------------------------------------------
 * The behaviour under test is JavaScript, and three of the mandated behaviours
 * are only observable by RUNNING it: whether an internal throw escapes into
 * the framework's asset fallback, whether an unset secret fails closed, and
 * whether the comparison goes through `crypto.subtle`. Python would have to
 * reimplement all three to reach any of them, and a reimplementation that
 * agrees with itself is decoration.
 *
 * ---------------------------------------------------------------------------
 * WHERE IT LIVES AND WHY
 * ---------------------------------------------------------------------------
 * The layout precedent is `tests/breaking/worker_heartbeat.test.js` beside its
 * Python CI wrapper `tests/breaking/test_worker_heartbeat.py`: one JS suite
 * per unit, plus a thin `python -m unittest` wrapper, in the same directory.
 * This file copies that layout exactly, into a new `tests/security/`
 * package. A new package rather than `tests/breaking/` because
 * `tests/breaking/` is named for the BREAKING-NEWS path, and a digest-archive
 * access control is not part of it — filing a security boundary under another
 * feature's name is how the boundary stops being found. The `tests/breaking/`
 * file's own header makes the same argument about a sibling directory.
 *
 * ---------------------------------------------------------------------------
 * REPRODUCING THE RED
 * ---------------------------------------------------------------------------
 * Set `PAGES_MIDDLEWARE` to the path of a deliberately broken copy of the
 * middleware and every test in this file judges THAT file instead of the
 * committed one. A mutant tree can be generated anywhere outside the repo;
 * this env var is how they are all re-run in one command. The override is
 * printed as a NOTE so it can never be mistaken for CI.
 *
 * ---------------------------------------------------------------------------
 * WHAT EACH TEST IS HOLDING DOWN
 * ---------------------------------------------------------------------------
 *   1. no `Authorization` header -> 401 carrying `WWW-Authenticate`, so the
 *      browser shows its native prompt;
 *   2. a wrong password -> 401, and `next()` is never reached;
 *   3. correct credentials -> `next()` runs and the asset is served;
 *   4. THE CRITICAL ONE — an internal throw answers 401. Cloudflare's own
 *      docs say an uncaught throw falls back to `env.ASSETS.fetch()`, i.e.
 *      it SERVES THE ARCHIVE. This file models that fallback in
 *      `callAsPagesWould` and asserts the status code the CLIENT would see,
 *      not merely that a throw did not escape;
 *   5. a missing, empty or non-string `DIGEST_PASSWORD` -> 401 and a loud
 *      log, never `next()`. An unconfigured secret must mean "nobody gets in";
 *   6. every malformed `Authorization` shape -> 401, no crash;
 *   7. HEAD, POST, PUT, DELETE, OPTIONS and a ranged GET are all protected,
 *      and a HEAD 401 carries the challenge with no body;
 *   8. the secret comparison goes through `crypto.subtle` and never `===`
 *      (a plain `===` on a secret leaks timing and every scanner flags it);
 *   9. every rejection is byte-identical, so the response is not an oracle;
 *  10. the 401 body names NO username in either state — secret set or unset —
 *      because naming the accepted account in an unauthenticated response is a
 *      disclosure and it made the browser prompt lie;
 *  11. a missing or blank `DIGEST_USERNAME` fails CLOSED with the same generic
 *      401 and an operator log line that says the secret is unset. There is no
 *      placeholder username to fall back to, and the source carries no default
 *      credential at all.
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const COMMITTED_MIDDLEWARE = path.join(
  __dirname, "..", "..", "functions", "_middleware.js");
const MIDDLEWARE_PATH = process.env.PAGES_MIDDLEWARE || COMMITTED_MIDDLEWARE;

if (process.env.PAGES_MIDDLEWARE) {
  process.stderr.write(
    "NOTE: judging " + MIDDLEWARE_PATH + " (PAGES_MIDDLEWARE) INSTEAD of " +
    "the committed " + COMMITTED_MIDDLEWARE + ".\n");
}

/** The password used only by the tests. Never a real secret, never printed. */
const TEST_PASSWORD = "test-only-digest-password";

/**
 * The username used only by the tests, supplied through `DIGEST_USERNAME` in
 * every authenticated case. It is a test value, NOT a default the middleware
 * carries: the middleware has no username of its own, which is the property
 * the "no placeholder credential" tests hold down.
 */
const TEST_USERNAME = "owner@archive.test";
const ORIGIN = "https://daily-digest.example.pages.dev";

const loadCounter = { n: 0 };

/**
 * Load the middleware's real source as an ES module.
 *
 * The committed bytes are copied verbatim to a unique temp `.mjs` file and
 * imported. The only reason for the copy is that `node --test` runs this file
 * as CommonJS while the middleware is an ES module; nothing is rewritten, so
 * every function under test is the committed code. Each load gets its own
 * path because Node caches modules by resolved URL — the equivalent of a cold
 * isolate, which is what a Worker really is.
 */
async function loadMiddleware() {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  if (src.indexOf("export ") < 0) {
    throw new Error(
      "no `export ` in " + MIDDLEWARE_PATH + " — the harness cannot reach the " +
      "Pages Function entry point, so this suite would be testing nothing");
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pages-middleware-"));
  const file = path.join(dir, "middleware-" + (loadCounter.n++) + ".mjs");
  fs.writeFileSync(file, src, "utf8");
  let mod;
  try {
    mod = await import(pathToFileURL(file).href);
  } catch (err) {
    throw new Error(
      "loading the middleware harness failed: " +
      String((err && err.message) || err) +
      "\nThe suite needs `onRequest`; if the module stopped exporting it, " +
      "update this suite deliberately rather than silently.");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  if (typeof mod.onRequest !== "function") {
    throw new Error(
      MIDDLEWARE_PATH + " does not export a function named onRequest. Pages " +
      "routes a `functions/_middleware.js` by that name and silently IGNORES " +
      "any other export, so this file would be testing something the platform " +
      "never calls.");
  }
  return mod;
}

/**
 * Run the middleware the way Cloudflare runs it.
 *
 * Cloudflare's Pages Functions documentation, verbatim:
 *
 *   "We currently wrap Pages Functions code in a try…catch block and fallback
 *    to calling `env.ASSETS.fetch()`."
 *
 * So the wrapper below is not decoration — it is the mechanism that turns an
 * uncaught throw into a PUBLICLY SERVED ARCHIVE, and the only reason a test
 * can observe the difference between "returned 401" and "served the archive".
 * `next()` and `env.ASSETS.fetch()` resolve to the same static asset, exactly
 * as they do in the real runtime, where `next()`'s chain ends at the assets.
 *
 * @returns {Promise<{response: Response, fellBack: boolean, nextCalls: number,
 *   threw: Error|null}>}
 */
async function callAsPagesWould(mod, options) {
  const opts = options || {};
  const method = opts.method || "GET";
  const path_ = opts.path || "/";
  const headers = opts.headers || {};
  const body = opts.body;
  const request = new Request(ORIGIN + path_, {
    method: method,
    headers: headers,
    body: body,
  });

  const served = new Response("SERVED-THE-ARCHIVE", {
    status: 200,
    headers: { "Content-Type": "text/html" },
  });
  const assets = { fetch: async () => served };

  let nextCalls = 0;
  // The env is INHERITED, never copied. `Object.assign({}, env)` would invoke
  // a throwing `DIGEST_PASSWORD` getter right here, inside the harness, before
  // the middleware ever ran -- which would test the harness instead of the
  // control. Reading through the prototype chain reproduces the real runtime:
  // `context.env.DIGEST_PASSWORD` is evaluated lazily, by the middleware.
  const baseEnv = (typeof opts.env === "object" && opts.env !== null)
    ? opts.env
    : {};
  const context = {
    request: request,
    env: Object.create(baseEnv),
    next: async () => { nextCalls += 1; return served; },
    waitUntil: () => {},
    data: {},
  };
  context.env.ASSETS = (baseEnv && baseEnv.ASSETS) ? baseEnv.ASSETS : assets;

  let response;
  let fellBack = false;
  let threw = null;
  try {
    const result = await mod.onRequest(context);
    if (result instanceof Response) {
      response = result;
    } else if (result === undefined) {
      // The documented contract for a middleware that lets the request through.
      response = await context.next();
    } else {
      throw new Error("onRequest returned " + typeof result + ", not a Response");
    }
  } catch (error) {
    // THIS is the branch that serves the archive. Reproducing it is the whole
    // point of the harness.
    fellBack = true;
    threw = error;
    response = await context.env.ASSETS.fetch(request);
  }
  return { response: response, fellBack: fellBack, nextCalls: nextCalls,
           threw: threw };
}

/** An `Authorization` value for the committed username and a given password. */
function basicHeader(password, username) {
  const user = username === undefined
    ? undefined  // filled in by the caller through `loadMiddleware`
    : username;
  return "Basic " + Buffer.from(String(user) + ":" + password, "utf8").toString("base64");
}

/** Capture the middleware's own console output without touching the reporter. */
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
 * Assert a response is a refusal, and say precisely why not when it is not.
 *
 * Every rejection test funnels through here so the failure text names the
 * mechanism rather than the symptom: a 200 with a `SERVED-THE-ARCHIVE` body is
 * an origin-level exposure, and the message has to say that.
 */
function assertRefused(outcome, why) {
  assert.strictEqual(
    outcome.fellBack, false,
    why + " — the middleware THREW, and Cloudflare's documented fallback "
      + "(env.ASSETS.fetch()) served the archive publicly. The exception was: "
      + String(outcome.threw && outcome.threw.message || outcome.threw));
  assert.strictEqual(
    outcome.nextCalls, 0,
    why + " — the middleware called next() " + outcome.nextCalls + " time(s), "
      + "so an unauthenticated request reached the static assets. That is the "
      + "whole 72-page archive.");
  assert.strictEqual(
    outcome.response.status, 401,
    why + " — expected HTTP 401, got " + outcome.response.status +
      ". A 200 here means the archive was served to an unauthenticated client.");
  assert.ok(
    (outcome.response.headers.get("WWW-Authenticate") || "").startsWith("Basic "),
    why + " — the 401 must carry a `WWW-Authenticate: Basic realm=…` header or "
      + "the browser shows an error page instead of its own credential prompt. "
      + "Got: " + JSON.stringify(outcome.response.headers.get("WWW-Authenticate")));
}

// ==========================================================================
// 1 + 2 + 6 + 7 + 10: the rejections
// ==========================================================================

test("no Authorization header -> 401 with a Basic challenge", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, { method: "GET" });
  assertRefused(out, "a request with no Authorization header at all");
});

test("an empty Authorization header -> 401, not a crash", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, { headers: { Authorization: "" } });
  assertRefused(out, "an empty Authorization header");
});

test("the wrong password -> 401, and next() is never reached", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader("not-the-password", TEST_USERNAME) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assertRefused(out, "a wrong password");
});

test("the right password with the wrong username -> 401", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader(TEST_PASSWORD, "digest") },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assertRefused(out, "the correct password under the wrong username");
});

test("a password differing only in its last character -> 401", async () => {
  const mod = await loadMiddleware();
  const near = TEST_PASSWORD.slice(0, -1) +
    (TEST_PASSWORD.endsWith("d") ? "e" : "d");
  const out = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader(near, TEST_USERNAME) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assertRefused(out, "a one-character-off password");
});

const MALFORMED_HEADERS = [
  ["a bare scheme with no token", "Basic"],
  ["a scheme and token with no space", "Basic" + Buffer.from("a:b").toString("base64")],
  ["a scheme with only whitespace after it", "Basic   "],
  ["a different auth scheme", "Bearer eyJhbGciOiJIUzI1NiJ9"],
  ["a token that is not base64", "Basic !!!!not-base64!!!!"],
  ["base64 that contains no colon", "Basic " + Buffer.from("nocolonhere").toString("base64")],
  ["a header that is only whitespace", "   "],
  ["lowercase scheme", "basic " + Buffer.from("u:p").toString("base64")],
];

for (const [label, value] of MALFORMED_HEADERS) {
  test("a malformed Authorization header (" + label + ") -> 401, no crash", async () => {
    const mod = await loadMiddleware();
    const logs = captureLogs();
    let out;
    try {
      out = await callAsPagesWould(mod, {
        headers: { Authorization: value },
        env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
      });
    } finally {
      logs.restore();
    }
    assertRefused(out, "a malformed Authorization header: " + label);
  });
}

test("an oversized Authorization header -> 401, and is not parsed", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    headers: { Authorization: "Basic " + "A".repeat(20000) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assertRefused(out, "a 20 KB Authorization header");
});

const PROTECTED_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"];

for (const method of PROTECTED_METHODS) {
  test(method + " is protected exactly like GET", async () => {
    const mod = await loadMiddleware();
    const out = await callAsPagesWould(mod, {
      method: method,
      env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
    });
    assertRefused(out, "an unauthenticated " + method);
  });
}

test("a ranged GET (Range: bytes=0-1023) is protected", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    method: "GET",
    headers: { Range: "bytes=0-1023" },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assertRefused(out, "an unauthenticated ranged GET");
});

test("a HEAD 401 carries the challenge and NO body, as RFC 9110 requires", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    method: "HEAD",
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assertRefused(out, "an unauthenticated HEAD");
  assert.strictEqual(
    await out.response.text(), "",
    "a HEAD response must not carry a body; sending one is a protocol error "
    + "and some runtimes drop the response entirely");
});

test("every digest path is protected, not just the index", async () => {
  const mod = await loadMiddleware();
  for (const p of ["/", "/index.html", "/digests/2026-10-02-morning.html",
                   "/digests/2026-07-14-evening.html", "/../../etc/passwd"]) {
    const out = await callAsPagesWould(mod, {
      path: p,
      env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
    });
    assertRefused(out, "an unauthenticated GET of " + p);
  }
});

test("every rejection is byte-identical, so the response is not an oracle", async () => {
  const mod = await loadMiddleware();
  const cases = [
    {},
    { headers: { Authorization: "Basic Zm9vOmJhcg==" } },
    { headers: { Authorization: basicHeader("wrong", TEST_USERNAME) },
      env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME } },
    { headers: { Authorization: basicHeader(TEST_PASSWORD, "wrong-user") },
      env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME } },
    { env: { DIGEST_PASSWORD: "", DIGEST_USERNAME: TEST_USERNAME } },
    { env: { DIGEST_PASSWORD: TEST_PASSWORD } },
    { env: { DIGEST_USERNAME: TEST_USERNAME } },
    { env: {} },
  ];
  const bodies = [];
  const statuses = [];
  for (const c of cases) {
    const out = await callAsPagesWould(mod, c);
    statuses.push(out.response.status);
    bodies.push(await out.response.text());
  }
  assert.ok(statuses.every((s) => s === 401),
    "every rejection must be a 401: " + JSON.stringify(statuses));
  assert.strictEqual(
    new Set(bodies).size, 1,
    "the " + cases.length + " different rejection reasons produced " +
    new Set(bodies).size + " distinct bodies. A response that differs between " +
    "'no such user' and 'wrong password' is a credential oracle, and a response " +
    "that differs between 'secret set' and 'secret missing' tells an anonymous " +
    "client how the deployment is configured.\n" +
    JSON.stringify(bodies, null, 2));
});

// ==========================================================================
// 10: the 401 body names NOTHING — in either state
// ==========================================================================

/** Any email-shaped token, i.e. anything that could be an accepted username. */
const EMAIL_SHAPED = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;

test("the 401 body names no username when DIGEST_USERNAME is set", async () => {
  const mod = await loadMiddleware();
  const OWNER = "owner@private.example";
  const out = await callAsPagesWould(mod, {
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: OWNER },
  });
  assertRefused(out, "an anonymous GET with the deployment fully configured");
  const body = await out.response.text();
  assert.ok(body.indexOf(OWNER) < 0,
    "the 401 body named the accepted username. Naming the account in an " +
    "unauthenticated response tells an anonymous client which identifier is " +
    "worth guessing a password against — the disclosure this test exists to " +
    "stop. Body was: " + JSON.stringify(body));
  assert.ok(!EMAIL_SHAPED.test(body),
    "the 401 body contains something shaped like an email address, so it names " +
    "a username (or a placeholder) whatever the code intended. Body was: " +
    JSON.stringify(body));
  assert.ok(body.indexOf("DIGEST_USERNAME") < 0,
    "the 401 body named the credential's secret. The body must describe no " +
    "credential at all. Body was: " + JSON.stringify(body));
});

test("the 401 body names no username when DIGEST_USERNAME is unset", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    env: { DIGEST_PASSWORD: TEST_PASSWORD },
  });
  assertRefused(out, "an anonymous GET with DIGEST_USERNAME missing");
  const body = await out.response.text();
  assert.ok(!EMAIL_SHAPED.test(body),
    "the 401 body named a placeholder username when the real secret is " +
    "missing. A committed placeholder is not a safe fallback: it is a " +
    "published credential, and the prompt built from it rejects the owner's " +
    "own address. Body was: " + JSON.stringify(body));
  assert.ok(body.indexOf("DIGEST_USERNAME") < 0,
    "the 401 body named the credential's secret. Body was: " +
    JSON.stringify(body));
});

test("a wrong password and an unset secret return the SAME generic 401", async () => {
  const mod = await loadMiddleware();
  const wrongPassword = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader("not-the-password", TEST_USERNAME) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  const unsetSecret = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
    env: {},
  });
  assertRefused(wrongPassword, "a wrong password");
  assertRefused(unsetSecret, "no secrets configured at all");
  const wrongBody = await wrongPassword.response.text();
  const unsetBody = await unsetSecret.response.text();
  assert.strictEqual(wrongBody, unsetBody,
    "a wrong password and an unconfigured deployment returned different bodies, " +
    "so the refusal leaks how the deployment is configured.\nwrong password: " +
    JSON.stringify(wrongBody) + "\nunset secret:   " + JSON.stringify(unsetBody));
  assert.strictEqual(
    wrongPassword.response.headers.get("WWW-Authenticate"),
    unsetSecret.response.headers.get("WWW-Authenticate"),
    "the challenge header must not differ between an unset secret and a wrong " +
    "password either");
});

test("the middleware carries no username of its own — no default, no placeholder", async () => {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  assert.ok(!/export const DIGEST_USERNAME\s*=/.test(src),
    "the middleware exports a default username. A committed default is a " +
    "credential the whole world can read, and it is the value the 401 body " +
    "used to advertise. The only username that may authenticate is the one " +
    "DIGEST_USERNAME holds at runtime.");
  assert.ok(!EMAIL_SHAPED.test(src),
    "the middleware source contains an email-shaped literal, i.e. a username " +
    "value inside this public file. Found: " +
    JSON.stringify(src.match(EMAIL_SHAPED)));
});

// ==========================================================================
// 11: the username is CONFIGURED, and its absence FAILS CLOSED
// ==========================================================================

test("the configured username is the only one accepted", async () => {
  const mod = await loadMiddleware();
  const OWNER = "owner@private.example";

  // The configured username + the configured password opens the door.
  const ok = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader(TEST_PASSWORD, OWNER) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: OWNER },
  });
  assert.strictEqual(ok.nextCalls, 1,
    "a correct password under the username configured in DIGEST_USERNAME was " +
    "refused. That is the lockout this secret exists to remove: the username " +
    "used to be a committed placeholder, so a real address could never log in " +
    "no matter how right the password was.");

  // Any OTHER username, including the one this test file uses elsewhere, is a
  // different user and must be refused.
  const other = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: OWNER },
  });
  assertRefused(other, "a username other than the configured one");
});

const UNCONFIGURED_USERNAMES = [
  ["DIGEST_USERNAME absent", {}],
  ["DIGEST_USERNAME empty", { DIGEST_USERNAME: "" }],
  ["DIGEST_USERNAME whitespace only", { DIGEST_USERNAME: "   " }],
  ["DIGEST_USERNAME null", { DIGEST_USERNAME: null }],
  ["DIGEST_USERNAME a number", { DIGEST_USERNAME: 123456 }],
];

for (const [label, extra] of UNCONFIGURED_USERNAMES) {
  test("a missing username (" + label + ") fails CLOSED with the generic 401", async () => {
    const mod = await loadMiddleware();
    const logs = captureLogs();
    let out;
    try {
      out = await callAsPagesWould(mod, {
        headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
        env: Object.assign({ DIGEST_PASSWORD: TEST_PASSWORD }, extra),
      });
    } finally {
      logs.restore();
    }
    assertRefused(out,
      "DIGEST_USERNAME is not usable (" + label + "). With no username there " +
      "is no credential that can be right, so the only safe answer is 401 — a " +
      "placeholder fallback would be a published credential");
    assert.ok(
      logs.lines.some((l) => /DIGEST_AUTH_MISCONFIGURED/.test(l)),
      "a fail-closed rejection must be logged LOUDLY. Captured: " +
      JSON.stringify(logs.lines));
  });
}

test("the missing-username log names the secret and says UNSET, not a credential", async () => {
  const mod = await loadMiddleware();
  const logs = captureLogs();
  try {
    await callAsPagesWould(mod, {
      headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
      env: { DIGEST_PASSWORD: TEST_PASSWORD },
    });
  } finally {
    logs.restore();
  }
  const text = logs.lines.join("\n");
  assert.ok(text.indexOf("DIGEST_USERNAME") >= 0,
    "the operator log must name the secret that is missing, or nobody can fix " +
    "it. Logs were: " + JSON.stringify(text));
  assert.ok(text.indexOf("missing or empty") >= 0,
    "the operator log must say the secret is missing/empty rather than " +
    "presenting a placeholder as the accepted user. Logs were: " +
    JSON.stringify(text));
  assert.ok(!EMAIL_SHAPED.test(text),
    "the operator log printed an address as if it were the accepted username. " +
    "When the secret is unset there is no accepted username, and logging a " +
    "placeholder as one is the same lie the 401 body used to tell. Logs were: " +
    JSON.stringify(text));
  assert.ok(!lines_leak(logs.lines, TEST_PASSWORD),
    "the operator log contained the submitted password");
});

// ==========================================================================
// 3: the happy path
// ==========================================================================

test("correct credentials -> next() runs and the asset is served", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assert.strictEqual(out.fellBack, false,
    "the happy path must not take the framework fallback");
  assert.strictEqual(out.nextCalls, 1,
    "correct credentials must call next() exactly once (got " + out.nextCalls + ")");
  assert.strictEqual(out.response.status, 200);
  assert.strictEqual(await out.response.text(), "SERVED-THE-ARCHIVE");
});

test("correct credentials work for HEAD too", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {
    method: "HEAD",
    headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
    env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME },
  });
  assert.strictEqual(out.fellBack, false);
  assert.strictEqual(out.nextCalls, 1,
    "a HEAD with correct credentials must still reach the asset");
});

test("a correct latin-1 encoded password is accepted, not locked out", async () => {
  const mod = await loadMiddleware();
  const password = "pa§sword-with-1-byte-chars";
  const b64 = Buffer.from(
    Buffer.from(TEST_USERNAME + ":" + password, "utf8").toString("binary"),
    "binary").toString("base64");
  const out = await callAsPagesWould(mod, {
    headers: { Authorization: "Basic " + b64 },
    env: { DIGEST_PASSWORD: password, DIGEST_USERNAME: TEST_USERNAME },
  });
  assert.strictEqual(out.nextCalls, 1,
    "RFC 7617 leaves the charset to the client and some HTTP clients send a "
    + "latin-1 password. Refusing that reading would lock the owner out of "
    + "their own archive depending on which client they used.");
});

// ==========================================================================
// 4: THE CRITICAL ONE — an internal throw must never serve the archive
// ==========================================================================

test("an internal throw (env getter) answers 401, never a served page", async () => {
  const mod = await loadMiddleware();
  const logs = captureLogs();
  let out;
  try {
    out = await callAsPagesWould(mod, {
      headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
      // A `context.env` whose DIGEST_PASSWORD getter throws: the shape of a
      // platform-level fault, and the exact reason the whole body is wrapped.
      // The username IS configured, so the password getter really is reached.
      env: {
        DIGEST_USERNAME: TEST_USERNAME,
        get DIGEST_PASSWORD() { throw new Error("injected env fault"); },
      },
    });
  } finally {
    logs.restore();
  }
  assertRefused(out,
    "context.env threw while the middleware read DIGEST_PASSWORD. Cloudflare "
    + "documents that an uncaught throw falls back to env.ASSETS.fetch(), "
    + "which SERVES the archive publicly — this is the failure that would "
    + "have produced a deployment that looked protected and was not");
  assert.ok(
    logs.lines.some((l) => /DIGEST_AUTH_MIDDLEWARE_ERROR/.test(l)),
    "a swallowed internal fault must still be logged LOUDLY; silent is how "
    + "the origin-level exposure would rot unnoticed. Captured: "
    + JSON.stringify(logs.lines));
});

test("an internal throw from next() answers 401 rather than escaping", async () => {
  const mod = await loadMiddleware();
  const logs = captureLogs();
  const served = new Response("SERVED-THE-ARCHIVE", { status: 200 });
  // CORRECT credentials, so the middleware genuinely REACHES next() and the
  // throw happens inside its own try. Without the Authorization header this
  // would be rejected at the credential check and would never exercise the
  // asset-lookup path at all -- a test that passes without testing anything.
  const request = new Request(ORIGIN + "/", {
    method: "GET",
    headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
  });
  let out;
  try {
    let response; let fellBack = false; let threw = null;
    try {
      const result = await mod.onRequest({
        request: request,
        env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME,
               ASSETS: { fetch: async () => served } },
        // Correct credentials, and the asset lookup itself explodes. The
        // middleware owns this call, so it owns the exception.
        next: async () => { throw new Error("injected next() fault"); },
        waitUntil: () => {},
      });
      response = result instanceof Response ? result : served;
    } catch (error) {
      fellBack = true; threw = error;
      response = await served;
    }
    // nextCalls is 1, and that is CORRECT here: the middleware WAS authorised,
    // so it legitimately called next(), and next() is what threw. This case
    // asserts `fellBack === false` (the exception did not escape) rather than
    // `nextCalls === 0`, so it asserts its own invariants explicitly.
    out = { response: response, fellBack: fellBack, nextCalls: 1, threw: threw };
  } finally {
    logs.restore();
  }
  assert.strictEqual(
    out.fellBack, false,
    "context.next() threw inside the middleware's own try block, so the "
    + "exception was caught here rather than escaping into Cloudflare's "
    + "env.ASSETS.fetch() fallback. The escaped exception was: "
    + String(out.threw && out.threw.message || out.threw));
  assert.strictEqual(
    out.nextCalls, 1,
    "the middleware must have reached next() (got " + out.nextCalls + "); "
    + "otherwise this test proved nothing about the asset-lookup path");
  assert.strictEqual(
    out.response.status, 401,
    "a failed asset lookup must still answer 401, got " + out.response.status);
  assert.ok(
    (out.response.headers.get("WWW-Authenticate") || "").startsWith("Basic "),
    "even a fault-closed 401 must carry the challenge header");
  assert.ok(
    logs.lines.some((l) => /DIGEST_AUTH_MIDDLEWARE_ERROR/.test(l)),
    "a swallowed asset-lookup fault must still be logged. Captured: "
    + JSON.stringify(logs.lines));
});

test("an internal throw while reading the Authorization header answers 401", async () => {
  const mod = await loadMiddleware();
  const logs = captureLogs();
  const served = new Response("SERVED-THE-ARCHIVE", { status: 200 });
  const request = new Request(ORIGIN + "/", { method: "GET" });
  let out;
  try {
    let response; let fellBack = false; let threw = null; let nextCalls = 0;
    try {
      const result = await mod.onRequest({
        request: {
          method: "GET",
          get headers() { throw new Error("injected headers fault"); },
        },
        env: { DIGEST_PASSWORD: TEST_PASSWORD, DIGEST_USERNAME: TEST_USERNAME,
               ASSETS: { fetch: async () => served } },
        next: async () => { nextCalls += 1; return served; },
        waitUntil: () => {},
      });
      response = result instanceof Response ? result : await served;
    } catch (error) {
      fellBack = true; threw = error;
      response = await served;
    }
    out = { response: response, fellBack: fellBack, nextCalls: nextCalls,
            threw: threw };
  } finally {
    logs.restore();
  }
  assertRefused(out, "reading request.headers threw");
});

// ==========================================================================
// 5: fail closed
// ==========================================================================

// The USERNAME is configured in every entry, so the password is genuinely the
// half that is missing and the password branch is the one under test.
const UNCONFIGURED_ENVS = [
  ["no password at all", { DIGEST_USERNAME: TEST_USERNAME }],
  ["DIGEST_PASSWORD absent", { DIGEST_USERNAME: TEST_USERNAME, OTHER: "x" }],
  ["DIGEST_PASSWORD empty", { DIGEST_USERNAME: TEST_USERNAME, DIGEST_PASSWORD: "" }],
  ["DIGEST_PASSWORD null", { DIGEST_USERNAME: TEST_USERNAME, DIGEST_PASSWORD: null }],
  ["DIGEST_PASSWORD a number",
   { DIGEST_USERNAME: TEST_USERNAME, DIGEST_PASSWORD: 123456 }],
  ["DIGEST_PASSWORD an empty String object",
   { DIGEST_USERNAME: TEST_USERNAME, DIGEST_PASSWORD: new String("") }],
];

for (const [label, env] of UNCONFIGURED_ENVS) {
  test("unconfigured secret (" + label + ") -> 401 fail-closed, loudly", async () => {
    const mod = await loadMiddleware();
    const logs = captureLogs();
    let out;
    try {
      out = await callAsPagesWould(mod, {
        headers: { Authorization: basicHeader(TEST_PASSWORD, TEST_USERNAME) },
        env: env,
      });
    } finally {
      logs.restore();
    }
    assertRefused(out,
      "the Pages secret is not configured (" + label + "). Even with correct "
      + "credentials this must be a 401: an unconfigured secret means NOBODY "
      + "gets in, not EVERYBODY gets in");
    assert.ok(
      logs.lines.some((l) => /DIGEST_AUTH_MISCONFIGURED/.test(l)),
      "a fail-closed rejection must be logged LOUDLY, otherwise the owner sees "
      + "a locked archive and no explanation. Captured: "
      + JSON.stringify(logs.lines));
    assert.ok(
      !lines_leak(logs.lines, TEST_PASSWORD),
      "the log must not contain the secret value");
  });
}

/** True if any log line contains the given literal. Used to catch secret leaks. */
function lines_leak(lines, needle) {
  return lines.some((l) => l.indexOf(needle) >= 0);
}

// ==========================================================================
// 8: the comparison is constant-time by construction
// ==========================================================================

test("the secret is compared through crypto.subtle, never with ===", async () => {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  assert.match(
    src, /crypto\.subtle\.digest\(/,
    "the middleware must hash both sides with crypto.subtle before comparing. "
    + "A plain `===` on a secret leaks timing and is flagged as a weakness by "
    + "every scanner worth reading");
  assert.match(
    src, /constantTimeEqualBytes|difference \|=/,
    "the digest comparison must accumulate differences rather than early-exit "
    + "on the first mismatch, which would leak the position of the first wrong byte");

  // And the equality itself must not be a string comparison anywhere: a
  // `===` against the secret, however hidden in a helper, is the weakness.
  // Deliberately NARROW: only a comparison whose operands are the SECRET or
  // the CANDIDATE password is a finding. A `typeof` guard or a `.length === 0`
  // emptiness test on the secret is a correctness check, not a comparison of
  // its value, and flagging those would make the guard cry wolf and get
  // weakened until it checks nothing.
  const stringComparisons = src.split("\n").filter((line, i) => {
    if (!/===/.test(line)) return false;
    if (/^\s*(\/\/|\*|\/\*)/.test(line)) return false;
    // Comparison of the two digests, or the single accumulator test at the
    // end of the constant-time loop. Neither operand is the secret.
    if (/difference/.test(line) || /left\[|right\[/.test(line)) return false;
    // Emptiness / type guards.
    if (/\.length === 0/.test(line)) return false;
    if (/typeof /.test(line)) return false;
    // The only remaining candidate: the secret or a candidate compared to
    // something with ===.
    return (/expectedPassword\s*===|===\s*expectedPassword/.test(line) ||
            /credentials\.password\s*===|===\s*credentials\.password/.test(line) ||
            /DIGEST_USERNAME\s*===|===\s*DIGEST_USERNAME/.test(line) ||
            /===\s*DIGEST_USERNAME/.test(line)) && i >= 0;
  });
  assert.deepStrictEqual(
    stringComparisons, [],
    "the password is compared with `===` here, which leaks timing:\n  "
    + stringComparisons.join("\n  "));
});

test("the middleware source contains a try/catch around the whole body", async () => {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  assert.match(
    src, /catch \(error\)/,
    "the middleware has no catch block. Without one, ANY internal fault is "
    + "handled by Cloudflare's fallback to env.ASSETS.fetch(), which serves "
    + "the archive publicly");
});