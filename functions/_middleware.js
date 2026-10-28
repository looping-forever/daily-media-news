/**
 * functions/_middleware.js
 *
 * THE ACTUAL SECURITY CONTROL for the digest archive, and now the ONLY
 * one. It used to share the job with a `digest-auth` Worker that merely
 * PROXIED to this Pages project, so the origin stayed directly reachable
 * and the proxy gated nothing. Probed and confirmed on 2026-10-02:
 * `https://daily-digest.example.pages.dev/` returned HTTP 200,
 * unauthenticated, with the index linking the whole archive. That Worker was
 * deleted on 2026-10-03; this file is what enforces the password.
 * Cloudflare's own docs are the instruction this file exists to follow:
 *
 *   "If you want to run a middleware on your entire application, INCLUDING IN
 *    FRONT OF STATIC FILES, create a `functions/_middleware.js` file."
 *
 * A file-based Worker (`_worker.js`) or a routing Function would NOT do:
 * neither covers the static HTML, and the archive IS static HTML.
 *
 * ---------------------------------------------------------------------------
 * THE PROPERTY THIS FILE IS BUILT AROUND: IT MUST NEVER THROW
 * ---------------------------------------------------------------------------
 * Cloudflare's Pages Functions documentation, verbatim:
 *
 *   "We currently wrap Pages Functions code in a try…catch block and fallback
 *    to calling `env.ASSETS.fetch()`. This means that any critical failures
 *    (such as exceeding CPU time or exceeding memory) may still throw an
 *    error."
 *
 * Read that again in context: the framework's own error handler is the thing
 * that serves the archive. An uncaught throw here does not produce a 500 — it
 * produces the unauthenticated index, silently. So EVERY path out of
 * `onRequest` is a deliberate Response or `next()`:
 *
 *   - absent / wrong / malformed credentials  -> 401
 *   - either secret unset or empty            -> 401 (fail CLOSED, loudly)
 *   - anything at all throws                  -> 401 (logged, never re-thrown)
 *
 * An unconfigured secret means "nobody gets in", never "everybody gets in".
 *
 * The free plan allows 10 ms of CPU per request, so the handler is trivial
 * and the password comparison is a fixed-length SHA-256 comparison through
 * `crypto.subtle` rather than `===`: a plain `===` on a secret leaks timing
 * and is also flagged as a weakness by every scanner worth reading.
 *
 * ---------------------------------------------------------------------------
 * DEPLOYMENT CONTRACT (three things outside this file, all verified)
 * ---------------------------------------------------------------------------
 * 1. Wrangler discovers `./functions` relative to the directory the deploy
 *    command RUNS IN, not relative to the staged asset directory. The deploy
 *    step in .github/workflows/daily-news.yml runs from the REPO ROOT and
 *    passes a `mktemp -d` staging dir as the asset directory, so this file at
 *    the repo root is the right place, and the middleware must never be moved
 *    under `docs/`.
 * 2. `_routes.json` is written explicitly by that same deploy step as
 *    `{"version":1,"include":["/*"],"exclude":[]}`. Cloudflare would otherwise
 *    auto-generate one, and `exclude` "always takes priority over `include`" —
 *    so an auto-generated exclude of static routes would exempt the entire
 *    archive while the job stays green.
 * 3. The Pages project MUST be set to Settings -> Runtime -> Fail closed. On
 *    "fail open", exhausting the daily Function allowance serves the static
 *    assets directly and the control this file implements never runs. The
 *    drift canary probes the live origin unauthenticated every day precisely
 *    so that configuration cannot rot unnoticed.
 *
 * ---------------------------------------------------------------------------
 * BOTH HALVES OF THE CREDENTIAL ARE PAGES SECRETS, AND BOTH ARE REQUIRED
 * ---------------------------------------------------------------------------
 * `DIGEST_USERNAME` and `DIGEST_PASSWORD` are read from THIS Pages project
 * (Settings -> Environment variables and secrets, Production environment) and
 * nowhere else: the `digest-auth` Worker that used to hold a second copy of
 * the password was deleted on 2026-10-03.
 *
 * There is deliberately NO committed default for either one. A placeholder
 * username was the trap here: the 401 body named it, so the browser prompt
 * told the owner to type an address that was not his, a correct password was
 * refused beside it, and the string itself was published in this public
 * repository. Nothing in this file now names, guesses or falls back to a
 * username, so the only username that can ever authenticate is the one the
 * deployment sets.
 *
 * If either secret is absent or empty this middleware returns 401 for EVERY
 * request — fail-closed, deliberately — and logs which secret is missing. Note
 * that an unauthenticated probe therefore returns 401 in BOTH the healthy case
 * (secrets set, no credentials offered) and the broken one, so "401 +
 * WWW-Authenticate" proves the middleware is running but does NOT by itself
 * prove the credentials are set. The operator log line is how that difference
 * is observed: it names the missing secret, and never a value.
 *
 * ---------------------------------------------------------------------------
 * THE 401 BODY NAMES NOTHING
 * ---------------------------------------------------------------------------
 * Naming the accepted account in an unauthenticated response is a disclosure
 * whatever the value is — it tells an unauthenticated client which identifier
 * is worth guessing a password against, and it made the browser prompt lie.
 * So the body is a fixed, generic string with no username, no secret name and
 * no deployment-specific detail. Every rejection returns the same bytes: absent
 * credentials, a malformed header, a wrong username, a wrong password and an
 * unconfigured secret are indistinguishable to the client, which is what stops
 * the response being a credential oracle.
 *
 * The diagnosis that body used to provide lives in the server-side log instead:
 * a rejection line records the request method and path and the username this
 * deployment accepts. The password is never logged — not its value, not its
 * length, not a prefix.
 */

/**
 * The realm string in `WWW-Authenticate`. It is what the browser shows in its
 * prompt, so it names what is being protected rather than naming a service
 * nobody outside this repository has heard of.
 */
export const DIGEST_REALM = "Daily Digest (private archive)";

/**
 * The Pages secret holding the Basic-auth username. Referenced BY NAME only;
 * the value is never written into this file, a response or a log line that is
 * not the operator diagnostic below.
 */
const USERNAME_SECRET_NAME = "DIGEST_USERNAME";

/**
 * The Pages secret holding the password. Referenced BY NAME only; the value is
 * never read into a log, a response body or an error message anywhere below.
 */
const PASSWORD_SECRET_NAME = "DIGEST_PASSWORD";

/**
 * A Basic credential can never be longer than this. Anything bigger is either
 * a mistake or an attempt to make the base64 decoder do work for free, and
 * either way the answer is the same 401 every other bad credential gets.
 */
const MAX_AUTHORIZATION_HEADER_LENGTH = 4096;

/**
 * The 401 body, and it is the SAME bytes for every rejection reason.
 *
 * It names no username, no secret and nothing about this deployment: an
 * unauthenticated response must not tell an unauthenticated client which
 * identifier to try. The reason for a refusal is logged instead, server-side.
 */
const UNAUTHORIZED_BODY =
  "401 Unauthorized\n" +
  "\n" +
  "This digest archive is private and requires HTTP Basic Auth.\n" +
  "\n" +
  "Use the username and password configured for this archive.\n";

/**
 * Read one env secret as a string.
 *
 * No try/catch here on purpose: a `context.env` whose getter THROWS is a
 * platform fault, not an unset secret, and it must be reported as one rather
 * than relabelled "misconfigured". It is swallowed by `onRequest`'s catch —
 * which logs it as an internal error and still answers 401. A value that is
 * absent or not a string is the empty string, which every caller reads as "not
 * configured", i.e. fail closed.
 */
function readSecret(env, name) {
  const raw = env ? env[name] : "";
  return typeof raw === "string" ? raw : "";
}

/** The configured username, trimmed: a stray space would be untypeable. */
function configuredUsername(env) {
  return readSecret(env, USERNAME_SECRET_NAME).trim();
}

/** The configured password, NOT trimmed: its bytes are its value. */
function configuredPassword(env) {
  return readSecret(env, PASSWORD_SECRET_NAME);
}

/**
 * A single 401, for every rejection reason, carrying the fixed generic body.
 *
 * @param {string|null} method  The request method, so a HEAD gets no body.
 *        Cloudflare and RFC 9110 both forbid a body on a HEAD response, and a
 *        HEAD that 401s must still carry `WWW-Authenticate` — so the headers
 *        are identical and only the body differs.
 */
function unauthorized(method) {
  const headers = new Headers({
    "WWW-Authenticate": 'Basic realm="' + DIGEST_REALM + '"',
    "Content-Type": "text/plain; charset=utf-8",
    // A shared cache must never reuse one client's 401 for another's, and must
    // never store a rejection at all.
    "Cache-Control": "no-store",
  });
  return new Response(method === "HEAD" ? null : UNAUTHORIZED_BODY, {
    status: 401,
    headers,
  });
}

/**
 * The operator's diagnostic, and the replacement for the username the 401 body
 * used to print.
 *
 * Server-side only. It names the EFFECTIVE username so an operator can tell at
 * a glance whether `DIGEST_USERNAME` is the address they think it is, and says
 * why the request was refused. The password is never part of this line: not
 * the submitted one, not the configured one.
 *
 * Never throws — it runs on the rejection path, which must not be able to turn
 * a 401 into an escaped exception.
 */
function logRejection(method, path, username, reason) {
  try {
    console.error(
      "DIGEST_AUTH_REJECTED: " + reason + " on " + method + " " + path +
      ". This deployment accepts only this username: " + username +
      ". The password is the " + PASSWORD_SECRET_NAME + " secret and is never " +
      "logged.",
    );
  } catch (error) {
    // A log that cannot be written must not change the answer.
  }
}

/**
 * Describe a thrown value without ever throwing itself.
 *
 * Nested on purpose: this runs inside the catch that exists because something
 * already went wrong, so it must be incapable of causing a second failure. A
 * throw from here would escape `onRequest` and land in the framework fallback
 * that serves the archive.
 */
function describe(error) {
  try {
    if (error && typeof error.message === "string" && error.message) {
      const name = typeof error.name === "string" && error.name ? error.name : "Error";
      return name + ": " + error.message;
    }
    return String(error);
  } catch (nested) {
    return "<an error that could not be printed>";
  }
}

/**
 * Decode a base64 Basic-auth payload to a string.
 *
 * RFC 7617 leaves the charset to the client, so UTF-8 is tried first (what
 * every current browser sends) and a latin-1 reading is accepted as a
 * fallback. Accepting both is not a weakening: either reading still has to
 * equal the secret, and refusing the latin-1 reading would lock the owner out
 * of their own archive depending on which HTTP client they happened to use.
 *
 * @throws only for input `atob` rejects, which the caller turns into a 401.
 */
function decodeBase64Payload(base64) {
  let binary;
  try {
    binary = atob(base64);
  } catch (error) {
    throw new Error("Authorization header is not valid base64");
  }
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i) & 0xff;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (error) {
    let out = "";
    for (let i = 0; i < bytes.length; i += 1) {
      out += String.fromCharCode(bytes[i]);
    }
    return out;
  }
}

/**
 * Parse an `Authorization` header.
 *
 * Returns null for EVERY unparseable shape — absent, empty, a different
 * scheme, a token that is not base64, a payload with no colon, a header long
 * enough to be an attack. Null always means the same thing to the caller: 401.
 *
 * The regex is anchored and has a closed character class, so it cannot
 * backtrack: there is no input for which this line becomes expensive.
 */
function parseBasicCredentials(header) {
  if (typeof header !== "string") return null;
  if (header.length === 0 || header.length > MAX_AUTHORIZATION_HEADER_LENGTH) return null;
  const match = /^Basic[ \t]+([A-Za-z0-9+/]+={0,2})[ \t]*$/.exec(header);
  if (!match) return null;
  let decoded;
  try {
    decoded = decodeBase64Payload(match[1]);
  } catch (error) {
    return null;
  }
  const colon = decoded.indexOf(":");
  if (colon < 0) return null;
  return { username: decoded.slice(0, colon), password: decoded.slice(colon + 1) };
}

/**
 * Compare two byte arrays without an early exit.
 *
 * Both arrays are the same fixed 32 bytes here, the loop walks all of them
 * unconditionally, and nothing branches on the value being compared until the
 * single accumulator test at the end.
 */
function constantTimeEqualBytes(left, right) {
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    difference |= (left[i] || 0) ^ (right[i] || 0);
  }
  return difference === 0;
}

/**
 * Constant-time string equality via SHA-256.
 *
 * Hashing first makes both sides the same length regardless of the input, so
 * the comparison cannot leak the secret's length either. Two SHA-256 digests
 * of a short string are microseconds, which is why this fits the free plan's
 * 10 ms CPU budget where a deliberately slow comparison would not.
 *
 * A missing `crypto.subtle` throws here and is caught by `onRequest`, which
 * means the answer is 401 rather than an open door.
 */
async function constantTimeEquals(left, right) {
  const encoder = new TextEncoder();
  const digests = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(left)),
    crypto.subtle.digest("SHA-256", encoder.encode(right)),
  ]);
  return constantTimeEqualBytes(
    new Uint8Array(digests[0]),
    new Uint8Array(digests[1]),
  );
}

/**
 * The request path, for the operator log, or "" when it cannot be read.
 *
 * Never throws: it is called from the rejection path, which must not be able
 * to turn a 401 into an escaped exception.
 */
function readPath(context) {
  try {
    const url = context.request.url;
    if (typeof url !== "string" || !url) return "";
    const path = url.replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^/]*/, "");
    return path || "/";
  } catch (error) {
    return "";
  }
}

/**
 * The actual decision. Called only from inside `onRequest`'s try block.
 *
 * @returns {Promise<Response|*>} a 401 Response, or whatever `context.next()`
 *   resolves to when the credentials are correct.
 */
async function decide(context) {
  // `context.env` is read INSIDE the try on purpose. A middleware that reads it
  // outside the try can still throw before the guard is armed.
  const method = readMethod(context);
  const expectedUsername = configuredUsername(context.env);
  const expectedPassword = configuredPassword(context.env);

  // FAIL CLOSED ON EITHER MISSING HALF. There is no fallback credential, so a
  // half-configured deployment refuses everyone and says which half is missing.
  if (expectedUsername.length === 0) {
    console.error(
      "DIGEST_AUTH_MISCONFIGURED: the " + USERNAME_SECRET_NAME + " Pages secret " +
      "is missing or empty, so every request is being refused. This is " +
      "FAIL-CLOSED and there is no placeholder username to fall back to — a " +
      "committed default would be a credential the whole world can read, and a " +
      "401 body that named it would tell the browser to type an address nobody " +
      "can log in with. Create the secret on the daily-digest Cloudflare Pages " +
      "project (Settings -> Environment variables and secrets, Production " +
      "environment). (The value itself is never logged.)",
    );
    return unauthorized(method);
  }

  if (expectedPassword.length === 0) {
    console.error(
      "DIGEST_AUTH_MISCONFIGURED: the " + PASSWORD_SECRET_NAME + " Pages secret " +
      "is missing or empty, so every request is being refused. This is " +
      "FAIL-CLOSED and is what an unconfigured deployment must do — but it " +
      "also means the owner has not created the Pages secret yet, and the " +
      "archive is unreachable rather than public. Create it on the " +
      "daily-digest Pages project (Settings -> Environment variables and " +
      "secrets, Production environment). It is the ONLY place the password " +
      "now lives: the `digest-auth` Worker that used to hold a second copy " +
      "was deleted on 2026-10-03. (The value itself is never read into " +
      "this message.)",
    );
    return unauthorized(method);
  }

  const credentials = parseBasicCredentials(
    context.request.headers.get("Authorization"),
  );
  if (!credentials) {
    logRejection(method, readPath(context), expectedUsername,
                 "no usable Basic credentials were presented");
    return unauthorized(method);
  }

  // Both comparisons always run. Short-circuiting on the username would make
  // the response time a username oracle, and there is nothing to save: two
  // SHA-256 digests do not measurably differ.
  const usernameMatches = await constantTimeEquals(credentials.username, expectedUsername);
  const passwordMatches = await constantTimeEquals(credentials.password, expectedPassword);
  if (!(usernameMatches && passwordMatches)) {
    // One reason string for both halves: the submitted username is NOT logged
    // (it is a guess, often a fat-fingered real address), only whether it
    // matched, which is what an operator needs to tell the two cases apart.
    logRejection(method, readPath(context), expectedUsername,
                 usernameMatches
                   ? "the username matched but the password did not"
                   : "the username did not match");
    return unauthorized(method);
  }

  return await context.next();
}

/**
 * The request method, or null if it cannot be read.
 *
 * Null is fine everywhere it is used: `unauthorized(null)` sends a body, and
 * sending a body to a HEAD is a smaller problem than failing to authenticate
 * one. It exists only so HEAD does not get a body it is not allowed to have.
 */
function readMethod(context) {
  try {
    const method = context.request.method;
    return typeof method === "string" ? method : null;
  } catch (error) {
    return null;
  }
}

/**
 * The middleware entry point. Every route, every method, including GET, HEAD,
 * POST and range requests, because a file-based middleware at the root of
 * `functions/` is invoked before any route is resolved.
 *
 * Never throws. See the header comment: on this platform an uncaught throw
 * means `env.ASSETS.fetch()` and therefore a publicly served archive.
 */
export async function onRequest(context) {
  try {
    return await decide(context);
  } catch (error) {
    console.error(
      "DIGEST_AUTH_MIDDLEWARE_ERROR: the auth middleware threw and Pages would " +
      "have fallen back to env.ASSETS.fetch(), serving the archive " +
      "unauthenticated. Answering 401 instead. Cause: " + describe(error),
    );
    // The catch path passes no method on purpose: `context.request` may be the
    // thing that just failed, so re-reading it here could throw a second time.
    return unauthorized(null);
  }
}
