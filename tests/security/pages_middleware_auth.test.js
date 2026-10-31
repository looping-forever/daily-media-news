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
 * in. It passes every request through to the static asset, and it answers one
 * thing itself: a request under /digests/ that Pages resolves to the archive
 * index because no such edition exists now gets a real 404 instead of a 200.
 * Before that, a status-only check could not tell a published edition from a
 * missing one. This suite holds both shapes down, so neither a credential
 * prompt nor the index-as-200 fallback can come back silently.
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

// ---------------------------------------------------------------------------
// THE INDEX-AS-200 FALLBACK (measured in production 2026-10-31)
//
// Pages answered a missing path with HTTP 200 and the 13,859-byte index body.
// A monitoring arm reading `status == 200` therefore reported a failed edition
// as published. The harness below models that world with the REAL archive: the
// committed index for a path that does not exist, and the committed digest
// pages for the paths that do.
// ---------------------------------------------------------------------------

const DOCS_DIR = path.join(__dirname, "..", "..", "docs");
const DIGESTS_DIR = path.join(DOCS_DIR, "digests");

function readIndexHtml() {
  return fs.readFileSync(path.join(DOCS_DIR, "index.html"), "utf8");
}

function digestFileNames() {
  return fs.readdirSync(DIGESTS_DIR)
    .filter((name) => name.endsWith(".html"))
    .sort();
}

function realDigest() {
  const names = digestFileNames();
  if (!names.length) throw new Error("no real digest page under docs/digests/");
  const name = names[0];
  return {
    name: name,
    path: "/digests/" + name.replace(/\.html$/, ""),
    body: fs.readFileSync(path.join(DIGESTS_DIR, name), "utf8"),
  };
}

function respondWith(spec) {
  return new Response(spec.body === undefined ? null : spec.body, {
    status: spec.status || 200,
    headers: Object.assign({ "Content-Type": "text/html; charset=utf-8" },
      spec.headers || {}),
  });
}

/**
 * Run the middleware against a modelled Pages site.
 *
 * `next()` behaves as Pages does: the requested file when it exists, a 308
 * from an existing page's `.html` form to its extensionless form, and — for
 * anything else — the archive index with HTTP 200. That last branch is the
 * defect, reproduced faithfully rather than described.
 */
async function callWithSite(mod, options) {
  const opts = options || {};
  const method = opts.method || "GET";
  const requestPath = opts.path || "/";
  const digest = realDigest();
  const indexHtml = readIndexHtml();
  const files = Object.assign({
    "/": { status: 200, body: indexHtml },
    [digest.path]: { status: 200, body: digest.body },
  }, opts.files || {});

  const request = new Request(ORIGIN + requestPath, { method: method });
  let nextCalls = 0;
  // Both `next()` and `env.ASSETS.fetch()` see the same site, as they do in
  // production: the HEAD path asks ASSETS for the body it cannot read itself.
  const resolve = async (req) => {
    const p = new URL(req.url).pathname;
    if (files[p]) return respondWith(files[p]);
    // The 308 Pages issues for an EXISTING page's `.html` form.
    if (p.endsWith(".html") && files[p.slice(0, -5)]) {
      return new Response(null, {
        status: 308,
        headers: { Location: p.slice(0, -5) },
      });
    }
    // No such file: the measured production answer.
    return respondWith({ status: 200, body: indexHtml });
  };
  const context = {
    request: request,
    env: { ASSETS: { fetch: resolve } },
    next: async () => {
      nextCalls += 1;
      return resolve(request);
    },
    waitUntil: () => {},
    data: {},
  };

  const response = await mod.onRequest(context);
  return {
    response: response instanceof Response ? response : await context.next(),
    nextCalls: nextCalls,
    digest: digest,
    indexHtml: indexHtml,
  };
}

test("an unknown .html digest path answers 404 with the slot page", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, { path: "/digests/2026-12-01-morning.html" });
  assert.strictEqual(out.response.status, 404,
    "a digest path that does not exist answered HTTP " + out.response.status +
    ". Pages serves the archive index with 200 for a missing path, which is " +
    "indistinguishable from a published edition at the status line.");
  const body = await out.response.text();
  assert.ok(body.indexOf("No edition for this slot") >= 0,
    "the 404 does not say there is no edition for this slot");
  assert.ok(body.indexOf('href="/"') >= 0,
    "the 404 page does not link back to the archive index");
  assert.ok(body.indexOf("<title>Personal News Informer — Digest Archive</title>") < 0,
    "the 404 body is the archive index, so the fallback was passed through");
  assert.match(out.response.headers.get("Content-Type") || "", /^text\/html/,
    "the 404 is not served as HTML");
});

test("an unknown extensionless digest path answers 404 too", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, { path: "/digests/2026-12-01-evening" });
  assert.strictEqual(out.response.status, 404,
    "the extensionless form of a missing edition answered HTTP " +
    out.response.status + ". Both URL forms reach the same fallback.");
});

test("any unknown path under /digests/ answers 404, including the bare prefix", async () => {
  const mod = await loadMiddleware();
  for (const requestPath of ["/digests", "/digests/", "/digests/nonsense/deeper.html"]) {
    const out = await callWithSite(mod, { path: requestPath });
    assert.strictEqual(out.response.status, 404,
      requestPath + " answered HTTP " + out.response.status +
      ", so an unknown path under /digests/ still reads as published.");
  }
});

test("an existing digest page is still served with 200", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, { path: realDigest().path });
  assert.strictEqual(out.response.status, 200,
    "the real digest " + realDigest().path + " answered HTTP " +
    out.response.status + ". Only unknown paths may become a 404.");
  assert.strictEqual(await out.response.text(), out.digest.body,
    "the middleware replaced the digest page's body");
  assert.strictEqual(out.nextCalls, 1, "the request did not reach next() exactly once");
});

test("the archive index is still served with 200", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, { path: "/" });
  assert.strictEqual(out.response.status, 200,
    "the archive index answered HTTP " + out.response.status + ", not 200");
  assert.strictEqual(await out.response.text(), out.indexHtml,
    "the middleware replaced the index page's body");
});

test("the 308 from a .html page to its extensionless form is preserved", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, { path: realDigest().path + ".html" });
  assert.strictEqual(out.response.status, 308,
    "the existing page's .html form answered HTTP " + out.response.status +
    ". Pages issues a 308 to the extensionless form and that must survive.");
  assert.strictEqual(out.response.headers.get("Location"), realDigest().path,
    "the 308 no longer points at the extensionless page");
});

test("a non-HTML asset under /digests/ is not turned into a 404", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, {
    path: "/digests/card.png",
    files: { "/digests/card.png": {
      status: 200, body: "PNGDATA", headers: { "Content-Type": "image/png" } } },
  });
  assert.strictEqual(out.response.status, 200,
    "an image under /digests/ answered HTTP " + out.response.status);
  assert.strictEqual(await out.response.text(), "PNGDATA",
    "an image under /digests/ had its body replaced");
});

test("a 404 is never cached", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, { path: "/digests/2026-12-01-morning" });
  assert.strictEqual(out.response.status, 404, "the missing edition was not a 404");
  assert.strictEqual(out.response.headers.get("Cache-Control"), "no-store",
    "the 404 carries `Cache-Control: " + out.response.headers.get("Cache-Control") +
    "`. A cached 404 would keep hiding a slot that publishes successfully later.");
});

test("Pages' own 404 under /digests/ is replaced by the slot page", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, {
    path: "/digests/2026-01-01-morning",
    files: { "/digests/2026-01-01-morning": { status: 404, body: "<html>Pages</html>" } },
  });
  assert.strictEqual(out.response.status, 404, "a real 404 stopped being a 404");
  assert.ok((await out.response.text()).indexOf("No edition for this slot") >= 0,
    "Pages' bare 404 body was passed through instead of the slot page");
});

test("a HEAD on a missing edition is a 404 too", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, {
    method: "HEAD", path: "/digests/2026-12-01-morning" });
  assert.strictEqual(out.response.status, 404,
    "a HEAD on a missing edition answered HTTP " + out.response.status +
    ". A HEAD carries no body, so the middleware must ask for the body it " +
    "cannot read rather than leave a status-only monitor false-green.");
});

test("a HEAD on an existing digest page is still served", async () => {
  const mod = await loadMiddleware();
  const out = await callWithSite(mod, { method: "HEAD", path: realDigest().path });
  assert.strictEqual(out.response.status, 200,
    "a HEAD on the real digest " + realDigest().path + " answered HTTP " +
    out.response.status);
});

test("the index fingerprint is the real archive index's own <title>", async () => {
  const src = fs.readFileSync(MIDDLEWARE_PATH, "utf8");
  const match = readIndexHtml().match(/<title>[^<]*<\/title>/);
  assert.ok(match, "docs/index.html carries no <title> for the middleware to match");
  assert.ok(src.indexOf(match[0]) >= 0,
    "the middleware does not recognise docs/index.html's " + match[0] +
    ", so the index fallback would pass through as a published edition. " +
    "Regenerating the index with a new <title> must fail HERE, not in production.");
});

test("no committed digest page carries the index fingerprint", async () => {
  const match = readIndexHtml().match(/<title>[^<]*<\/title>/);
  const offenders = digestFileNames().filter((name) =>
    fs.readFileSync(path.join(DIGESTS_DIR, name), "utf8").indexOf(match[0]) >= 0);
  assert.deepStrictEqual(offenders, [],
    "these digest pages carry the index's " + match[0] + ": " +
    JSON.stringify(offenders) + ". The middleware would 404 a real edition.");
});
