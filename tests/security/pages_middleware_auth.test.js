"use strict";
/**
 * tests/security/pages_middleware_auth.test.js
 *
 * THE DIGEST ARCHIVE IS PUBLIC BY DESIGN (2026-10-30).
 *
 * This suite used to assert the opposite: that `functions/_middleware.js`
 * refused every request with a 401 + `WWW-Authenticate` unless the right
 * username and password were presented. That gate is gone. The owner could not
 * open the site from a phone's in-app browser, which suppresses the native
 * login dialog, and because this repository is public every digest page was
 * already readable anonymously from GitHub — the password protected nothing.
 *
 * The file still exists, because wrangler discovers `./functions` relative to
 * the directory the deploy command runs in and the deploy contract is checked
 * in. It is now a pass-through: `onRequest` calls `context.next()` and returns
 * the static asset. This suite holds that shape down, so a credential prompt
 * cannot be re-introduced silently.
 *
 * The suite is JS because the behaviour under test is JavaScript. It loads the
 * COMMITTED bytes as an ES module and calls the real `onRequest`; there is no
 * reimplementation of the middleware anywhere in this file.
 *
 * Set `PAGES_MIDDLEWARE` to the path of another file and every test judges that
 * file instead — that is how a mutant is re-run in one command. The override is
 * printed as a NOTE so it can never be mistaken for CI.
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

const ORIGIN = "https://digest-archive.test";
const loadCounter = { n: 0 };

/**
 * Load the middleware's real source as an ES module.
 *
 * The committed bytes are copied verbatim to a unique temp `.mjs` file and
 * imported, because `node --test` runs this file as CommonJS while the
 * middleware is an ES module. Each load gets its own path because Node caches
 * modules by resolved URL — the equivalent of a cold isolate.
 */
async function loadMiddleware() {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pages-middleware-"));
  const file = path.join(dir, "middleware-" + (loadCounter.n++) + ".mjs");
  fs.writeFileSync(file, src, "utf8");
  let mod;
  try {
    mod = await import(pathToFileURL(file).href);
  } catch (err) {
    throw new Error(
      "loading the middleware harness failed: " +
      String((err && err.message) || err));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  if (typeof mod.onRequest !== "function") {
    throw new Error(
      MIDDLEWARE_PATH + " does not export a function named onRequest. Pages " +
      "routes a `functions/_middleware.js` by that name and silently IGNORES " +
      "any other export.");
  }
  return mod;
}

/**
 * Run the middleware the way Cloudflare runs it.
 *
 * `next()` resolves to the static asset, and a thrown exception falls back to
 * `env.ASSETS.fetch()` exactly as Cloudflare's documented wrapper does. The
 * fallback is modelled so a regression that throws is visible as "the harness
 * had to rescue it", not silently swallowed.
 */
async function callAsPagesWould(mod, options) {
  const opts = options || {};
  const method = opts.method || "GET";
  const requestPath = opts.path || "/";
  const headers = opts.headers || {};
  const request = new Request(ORIGIN + requestPath, { method: method, headers: headers });

  const served = new Response("SERVED-THE-ARCHIVE", {
    status: 200,
    headers: { "Content-Type": "text/html" },
  });
  const assets = { fetch: async () => served };

  let nextCalls = 0;
  const baseEnv = (typeof opts.env === "object" && opts.env !== null) ? opts.env : {};
  const context = {
    request: request,
    env: Object.create(baseEnv),
    next: async () => { nextCalls += 1; return served; },
    waitUntil: () => {},
    data: {},
  };
  context.env.ASSETS = assets;

  let response;
  let fellBack = false;
  let threw = null;
  try {
    const result = await mod.onRequest(context);
    if (result instanceof Response) {
      response = result;
    } else if (result === undefined) {
      response = await context.next();
    } else {
      throw new Error("onRequest returned " + typeof result + ", not a Response");
    }
  } catch (error) {
    fellBack = true;
    threw = error;
    response = await context.env.ASSETS.fetch(request);
  }
  return { response: response, fellBack: fellBack, nextCalls: nextCalls, threw: threw };
}

test("the middleware exports a callable onRequest", async () => {
  const mod = await loadMiddleware();
  assert.strictEqual(typeof mod.onRequest, "function",
    "Pages routes functions/_middleware.js by the name onRequest and ignores " +
    "every other export, so a different export name is deployed and does nothing.");
});

test("an anonymous GET calls next() and serves the asset", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, { method: "GET" });
  assert.strictEqual(out.fellBack, false,
    "the middleware THREW and the harness had to fall back to env.ASSETS.fetch(): " +
    String(out.threw && out.threw.message || out.threw));
  assert.strictEqual(out.nextCalls, 1,
    "next() was called " + out.nextCalls + " time(s). A pass-through calls it " +
    "exactly once; zero means the request was answered by the middleware itself.");
  assert.strictEqual(out.response.status, 200,
    "an anonymous GET got HTTP " + out.response.status + ", not 200. The digest " +
    "site is public by design and must serve the archive to anyone with the link.");
  assert.strictEqual(out.response.headers.get("WWW-Authenticate"), null,
    "the response carries `WWW-Authenticate: " +
    out.response.headers.get("WWW-Authenticate") + "`. That asks the browser for " +
    "a password, which is the defect this change removed.");
});

test("the response object from next() is returned unchanged", async () => {
  const mod = await loadMiddleware();
  const out = await callAsPagesWould(mod, {});
  assert.strictEqual(await out.response.text(), "SERVED-THE-ARCHIVE",
    "the middleware replaced or rewrote the asset response. A pass-through " +
    "returns exactly what next() produced.");
});

test("every HTTP method is passed through, GET included", async () => {
  const mod = await loadMiddleware();
  for (const method of ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"]) {
    const out = await callAsPagesWould(mod, { method: method });
    assert.strictEqual(out.fellBack, false, method + " made the middleware throw");
    assert.strictEqual(out.nextCalls, 1, method + " did not reach next() exactly once");
    assert.notStrictEqual(out.response.status, 401,
      method + " was refused with a 401. Every method must reach the static asset.");
  }
});

test("the pass-through never reads the retired secret names", async () => {
  const mod = await loadMiddleware();
  // Getters that throw on ANY read: if the middleware still looks for a
  // credential, it trips here instead of shipping a gate the owner cannot open.
  const env = {};
  for (const name of ["DIGEST_USERNAME", "DIGEST_PASSWORD", "DIGEST_REALM"]) {
    Object.defineProperty(env, name, {
      enumerable: true,
      get() { throw new Error("the middleware read the retired secret " + name); },
    });
  }
  const out = await callAsPagesWould(mod, { env: env });
  assert.strictEqual(out.fellBack, false,
    "the middleware read a retired secret: " +
    String(out.threw && out.threw.message || out.threw));
  assert.strictEqual(out.nextCalls, 1, "the request did not reach next()");
  assert.strictEqual(out.response.status, 200, "the request was not served");
});

test("the middleware source carries no auth artefacts", async () => {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  const forbidden = [
    "WWW-Authenticate",
    "DIGEST_USERNAME",
    "DIGEST_PASSWORD",
    "DIGEST_REALM",
    "Basic ",
    "crypto.subtle",
  ];
  const found = forbidden.filter((needle) => src.indexOf(needle) >= 0);
  assert.deepStrictEqual(found, [],
    "functions/_middleware.js still carries auth machinery: " + JSON.stringify(found) +
    ". The site is public by design; these are the pieces of the removed gate.");
  const emails = src.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || [];
  assert.deepStrictEqual(emails, [],
    "functions/_middleware.js carries an email-shaped literal: " + JSON.stringify(emails) +
    ". This repository is public and a username is a credential.");
});

test("the middleware is a pass-through, not merely a 200", async () => {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  assert.ok(src.indexOf("context.next()") >= 0,
    "functions/_middleware.js does not call context.next(), so it is not the " +
    "pass-through this repository claims to deploy.");
  assert.ok(/export\s+async\s+function\s+onRequest/.test(src),
    "functions/_middleware.js does not export `async function onRequest`.");
});

test("consecutive requests all reach the asset", async () => {
  const mod = await loadMiddleware();
  for (let i = 0; i < 3; i += 1) {
    const out = await callAsPagesWould(mod, { path: "/digests/page-" + i + ".html" });
    assert.strictEqual(out.nextCalls, 1, "request " + i + " did not reach next()");
    assert.strictEqual(out.response.status, 200, "request " + i + " was not served");
  }
});
