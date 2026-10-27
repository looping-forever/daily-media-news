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
 *   - DIGEST_PASSWORD unset or empty          -> 401 (fail CLOSED, loudly)
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
 * THE ONE PLACE THE PASSWORD LIVES
 * ---------------------------------------------------------------------------
 * Secrets are per-project. `DIGEST_PASSWORD` is read from THIS Pages project
 * and nowhere else: the `digest-auth` Worker that used to hold a second copy
 * of it was deleted on 2026-10-03, so this Pages secret (Settings ->
 * Environment variables and secrets, Production environment) is the only one.
 *
 * If it is absent or empty this middleware returns 401 for EVERY request -
 * fail-closed, deliberately. Note that an unauthenticated probe therefore
 * returns 401 in BOTH the healthy case (secret set, no credentials offered)
 * and the broken one (secret unset), so "401 + WWW-Authenticate" proves the
 * middleware is running but does NOT by itself prove the password is set. To
 * confirm the secret really is configured, authenticate with the username the
 * 401 body names and see whether a real password is accepted.
 *
 * ---------------------------------------------------------------------------
 * THE USERNAME IS A SECOND SECRET, AND ITS ABSENCE IS LOUD
 * ---------------------------------------------------------------------------
 * `DIGEST_USERNAME` overrides the committed placeholder username. Both halves
 * of the credential are therefore configured on the Pages project and neither
 * has to be edited into this public file. When it is unset the placeholder is
 * used and a real address is refused next to a correct password - logged by
 * name, and visible in the 401 body the browser is shown.
 */

/**
 * The Basic-auth USERNAME, and the only one that is accepted when the
 * DIGEST_USERNAME secret is not set.
 *
 * The committed value is a GENERIC placeholder, not a real mailbox: this
 * repository is public. The deployment sets DIGEST_USERNAME to the owner's own
 * address; this constant is what remains when that secret is missing.
 *
 * Why a secret and not a source literal: the owner's address cannot be
 * committed here, and a placeholder in source means the only way to log in is to
 * edit this public file and redeploy. That is the trap this constant used to be
 * — a correct password was refused because the username typed into the browser
 * was a real address and the one the middleware compared against was a
 * placeholder nobody would guess.
 *
 * The 401 body echoes the EFFECTIVE username back on purpose: if this value ever
 * needs to change, the failure mode is a 401 that tells you exactly what to
 * type, not a silent lockout.
 */
export const DIGEST_USERNAME = "digest@example.com";

/**
 * The Pages secret that overrides the username above. Read BY NAME only; the
 * value is never logged. Unset or empty falls back to `DIGEST_USERNAME`, which
 * fails CLOSED in the sense that matters: nobody types a placeholder address by
 * accident.
 */
const USERNAME_SECRET_NAME = "DIGEST_USERNAME";

/**
 * The realm string in `WWW-Authenticate`. It is what the browser shows in its
 * prompt, so it names what is being protected rather than naming a service
 * nobody outside this repository has heard of.
 */
export const DIGEST_REALM = "Daily Digest (private archive)";

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
 * The 401 body. Names the username and where the password comes from, so a
 * correct deployment that is hard to log into is diagnosable from the browser
 * alone. It carries no secret and cannot be used to distinguish "wrong
 * password" from "unknown user" — every rejection in this file returns these
 * identical bytes.
 */
function unauthorizedBody(username) {
  const shown = typeof username === "string" && username.length > 0
    ? username
    : DIGEST_USERNAME;
  return (
    "401 Unauthorized\n" +
    "\n" +
    "This digest archive is private and requires HTTP Basic Auth.\n" +
    "\n" +
    "  username: " + shown + "\n" +
    "  password: the DIGEST_PASSWORD secret configured on the\n" +
    "            daily-digest Cloudflare Pages project.\n"
  );
}

/**
 * The username this deployment actually accepts: the DIGEST_USERNAME secret, or
 * the committed placeholder when it is unset.
 *
 * Read inside `decide`'s try so a throwing `env` is caught and answered with a
 * 401 rather than escaping into Pages' `env.ASSETS.fetch()` fallback. A missing
 * secret is logged by name because that is the state in which the prompt asks
 * for an address nobody would type.
 */
function effectiveUsername(env) {
  let configured = "";
  try {
    const raw = env ? env[USERNAME_SECRET_NAME] : "";
    configured = typeof raw === "string" ? raw.trim() : "";
  } catch (error) {
    configured = "";
  }
  if (configured.length > 0) return configured;
  if (env) {
    console.error(
      "DIGEST_AUTH_MISCONFIGURED: the " + USERNAME_SECRET_NAME + " Pages secret " +
      "is missing or empty, so the committed PLACEHOLDER username is the only " +
      "one this deployment accepts and a real address will be refused next to " +
      "a correct password. Create it on the daily-digest Cloudflare Pages " +
      "project (Settings -> Environment variables and secrets, Production " +
      "environment). The 401 body names whichever value is in force, so this " +
      "is diagnosable from the browser.",
    );
  }
  return DIGEST_USERNAME;
}

/**
 * A single 401, for every rejection reason.
 *
 * @param {string|null} method  The request method, so a HEAD gets no body.
 *        Cloudflare and RFC 9110 both forbid a body on a HEAD response, and a
 *        HEAD that 401s must still carry `WWW-Authenticate` — so the headers
 *        are identical and only the body differs.
 * @param {string|null} username  The EFFECTIVE username, echoed in the body so
 *        the prompt names the value actually in force. Null when the caller
 *        cannot read one (the catch path), which falls back to the constant.
 */
function unauthorized(method, username) {
  const headers = new Headers({
    "WWW-Authenticate": 'Basic realm="' + DIGEST_REALM + '"',
    "Content-Type": "text/plain; charset=utf-8",
    // A shared cache must never reuse one client's 401 for another's, and must
    // never store a rejection at all.
    "Cache-Control": "no-store",
  });
  return new Response(method === "HEAD" ? null : unauthorizedBody(username), {
    status: 401,
    headers,
  });
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
 * The actual decision. Called only from inside `onRequest`'s try block.
 *
 * @returns {Promise<Response|*>} a 401 Response, or whatever `context.next()`
 *   resolves to when the credentials are correct.
 */
async function decide(context) {
  // `context.env` is read INSIDE the try on purpose. A middleware that reads it
  // outside the try can still throw before the guard is armed.
  const expectedPassword = context.env[PASSWORD_SECRET_NAME];
  if (typeof expectedPassword !== "string" || expectedPassword.length === 0) {
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
    return unauthorized(readMethod(context), effectiveUsername(context.env));
  }

  const credentials = parseBasicCredentials(
    context.request.headers.get("Authorization"),
  );
  if (!credentials) return unauthorized(readMethod(context), effectiveUsername(context.env));

  // Both comparisons always run. Short-circuiting on the username would make
  // the response time a username oracle, and there is nothing to save: two
  // SHA-256 digests do not measurably differ.
  const expectedUsername = effectiveUsername(context.env);
  const usernameMatches = await constantTimeEquals(credentials.username, expectedUsername);
  const passwordMatches = await constantTimeEquals(credentials.password, expectedPassword);
  if (!(usernameMatches && passwordMatches)) {
    return unauthorized(readMethod(context), expectedUsername);
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
    // The catch path passes no username on purpose: `context.env` is the thing
    // that just failed, so re-reading it here could throw a second time.
    return unauthorized(readMethod(context), null);
  }
}