#!/usr/bin/env python3
"""tests/security/test_pages_middleware_auth.py — CI entry point for the
digest archive's origin-level access control.

WHAT THIS CONTROLS
------------------
`functions/_middleware.js` is the ONLY thing standing between the internet and
the 72-page digest archive on `daily-digest.example.pages.dev`. The `digest-auth`
Worker does not: it only PROXIES to that origin, so the origin is directly
reachable and the proxy gates nothing. Probed and confirmed on 2026-10-02 —
HTTP 200, 38 021 bytes, unauthenticated, index linking all 72 pages.

WHY A PYTHON WRAPPER EXISTS AT ALL
---------------------------------
The behaviour under test is JavaScript and the tests that check it are
JavaScript: `tests/security/pages_middleware_auth.test.js` runs under
`node --test` against the middleware's own `onRequest`, a fake Pages context
and a faithful copy of the Pages runtime's documented error behaviour.

The runner that enforces this repository is `drift-canary.yml`'s `test-suite`
job, whose `run_suite` entries are all `python3 -m unittest` invocations, and
that job has no node setup step. This module makes the JS suite reachable from
that inventory without introducing a second mechanism — and, the part that
actually matters, it makes a MISSING node LOUD.

A security control that silently skips is worse than no security control: CI
goes green for a suite that never executed, and the one feature whose entire
job is to scream is the one nobody is looking at. So `node_executable()` calls
`self.fail()` with an explicit message rather than `skipTest`, matching
`tests/breaking/test_worker_heartbeat.py`, which already fails loudly for
exactly this reason and whose header documents the same reasoning.

WHAT IS NOT HERE, ON PURPOSE
----------------------------
No Python re-implementation of any auth logic. A proxy is exactly what let the
digest-auth Worker's uselessness survive as a "protection" for months: a
reimplementation that agrees with itself forever is not evidence.

RUN IT YOURSELF (both forms, both real):
    node --test tests/security/pages_middleware_auth.test.js
    python -m unittest tests.security.test_pages_middleware_auth -v

REPRODUCING THE RED
-------------------
Set `PAGES_MIDDLEWARE` to the path of a deliberately broken copy of
`functions/_middleware.js` and the JS suite judges that file instead of the
committed one. Five committed mutants, each proved to go red:
`m1_fail_open.js` (no auth at all), `m2_no_trycatch.js` (the exception
escapes into `env.ASSETS.fetch()`), `m3_fail_open_unset_secret.js` (an unset
secret opens the door), `m4_get_only.js` (only GET is guarded),
`m5_plain_equals.js` (the comparison regresses to a timing-leaking `===`).

`DRIFT_CANARY_FILE` is the same trick for the OTHER half of this module, the
origin-auth canary step in `drift-canary.yml`. It points the canary tests at a
different workflow file instead of the committed one, which is how the
`master.*` 302-to-Access false failure was proved to be a false failure:

    set DRIFT_CANARY_FILE=<a pre-fix drift-canary.yml>
    python -m unittest tests.security.test_pages_middleware_auth -v

The pre-fix file is the one committed at `9627e79`, where the canary treated
any 3xx as a failure and therefore went red on every scheduled run while the
archive was in fact correctly gated.
"""

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JS_SUITE = REPO_ROOT / "tests" / "security" / "pages_middleware_auth.test.js"
MIDDLEWARE = REPO_ROOT / "functions" / "_middleware.js"
CANARY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "drift-canary.yml"

#: The canary step these tests judge, named exactly as it is named in the
#: workflow. Duplicated here as a constant rather than retyped per assertion so
#: a rename is one edit and fails loudly rather than silently skipping a step.
CANARY_STEP_NAME = "Digest archive is not publicly readable (origin auth canary)"

# ---------------------------------------------------------------------------
# The probed world. Hostnames and the two real response shapes, measured.
# ---------------------------------------------------------------------------

#: The production origin. `functions/_middleware.js` answers it.
PROD_HOST = "daily-digest.example.pages.dev"

#: A SECOND public hostname serving the same Pages project from the same
#: deployment. If the Access application ever goes away this becomes a second
#: way in, which is the whole reason the arm is probed at all.
PREVIEW_HOST = "master.daily-digest.example.pages.dev"

#: Where Cloudflare Access sends the `PREVIEW_HOST` arm (measured 2026-10-03:
#: a 302 to this host, which serves the Access login and no digest content).
#: Access intercepts AHEAD of the Pages Function, so on that hostname the
#: middleware never runs and a 401 is not reachable.
ACCESS_HOST = "daily-digest-example.cloudflareaccess.com"
ACCESS_LOGIN_URL = ("https://%s/cdn-cgi/access/login/"
                    "0123456789abcdef0123456789abcdef") % ACCESS_HOST

#: Two invented hosts for redirect targets. `.invalid` is reserved by RFC 2606
#: and can never resolve, so if the stub ever stopped intercepting curl the test
#: would fail loudly instead of quietly reaching a real server.
STUB_TARGET_HOST = "redirect-target.invalid"
STUB_ELSEWHERE_HOST = "elsewhere.invalid"
STUB_IDP_HOST = "idp.example.invalid"

#: The middleware's own 401 body, transcribed. A faithful transcription rather
#: than the real thing because `unauthorizedBody()` is not exported and this
#: suite must not become a Python copy of the middleware — that is the exact
#: anti-pattern this repository has already paid for. It exists only so a
#: refusal carries realistic bytes, and `test_the_fixtures_cannot_accidentally_
#: satisfy_the_archive_check` proves it carries no archive marker.
UNAUTHORIZED_TEXT = (
    "401 Unauthorized\n"
    "\n"
    "This digest archive is private and requires HTTP Basic Auth.\n"
    "\n"
    "  username: digest-owner@example.invalid\n"
    "  password: the DIGEST_PASSWORD secret configured on the\n"
    "            daily-digest Cloudflare Pages project.\n"
)

#: The bare 143-byte nginx body Cloudflare's edge sends with that 302.
NGINX_302_TEXT = (
    "<html>\r\n"
    "<head><title>302 Found</title></head>\r\n"
    "<body>\r\n"
    "<center><h1>302 Found</h1></center>\r\n"
    "<hr><center>nginx</center>\r\n"
    "</body>\r\n"
    "</html>\r\n"
)

#: A stand-in for the Cloudflare Access login page. Contains no archive marker
#: by construction, which is what makes "Access served this, so nothing leaked"
#: a statement about the bytes rather than about a status code.
ACCESS_LOGIN_TEXT = (
    "<!DOCTYPE html><html><head><title>Access denied</title></head>"
    "<body><div id=\"cf-wrapper\"><h1>Log in to daily-digest</h1></div>"
    "<script src=\"/cdn-cgi/access/login/0123456789abcdef0123456789abcdef\">"
    "</script></body></html>\n"
)

#: A 200 that is neither a refusal nor the archive: the shape that must still
#: fail, because a 2xx from this origin is not a proof of protection.
NEUTRAL_TEXT = "<html><body><h1>Nothing to see</h1></body></html>\n"


def _archive_index_path():
    """The REAL archive index this repository ships, read as bytes.

    Not a hand-written stand-in. If the canary's archive markers ever stopped
    matching the actual archive, a fabricated fixture would keep the tests green
    while the live breach went unnoticed — which is the failure mode this
    repository's own history is a warning about.
    """
    path = REPO_ROOT / "docs" / "index.html"
    if not path.is_file():
        raise AssertionError("%s is missing: the archive fixture is the real "
                             "index, not a stand-in" % path)
    return path


def _archive_digest_path():
    """The real oldest digest page, so a single leaked page is caught too."""
    digests = sorted((REPO_ROOT / "docs" / "digests").glob("*.html"))
    if not digests:
        raise AssertionError("no real digest page under docs/digests/")
    return digests[0]


def _resp(status="200", text=None, archive=None, location=None, rc=0):
    """One modelled HTTP response for one hostname.

    `archive` is a path to a REAL archive file whose bytes become the body;
    `text` is a literal body. Exactly one of the two is normally given.
    """
    out = {"status": status, "rc": rc}
    if location is not None:
        out["location"] = location
    if archive is not None:
        out["archive"] = archive
    elif text is not None:
        out["text"] = text
    return out


#: A refusal from the middleware.
def _refused(status="401"):
    return _resp(status, text=UNAUTHORIZED_TEXT)


#: `master.*` answering with Access's 302 (the shape that failed on every run).
def _access_redirect():
    return _resp("302", text=NGINX_302_TEXT, location=ACCESS_LOGIN_URL)


def _stub_key(host):
    """The environment-variable stem the stub answers a hostname from."""
    return host.replace(".", "_").replace("-", "_")


#: The stubbed `curl`, prepended to the canary step's own shell.
#:
#: A BASH FUNCTION, not an executable on PATH. A PATH-based stub does not work
#: here: Git-for-Windows bash prepends its own /mingw64/bin at startup, so the
#: real curl wins the lookup and the test silently fetches the LIVE origin.
#: That was observed while writing the original version of this stub, and it is
#: the worst possible defect in a security test — it would report "the archive
#: is public" because production happens to be public today, and go quiet the
#: moment that changed. A shell function is resolved by the language itself
#: before PATH, so it cannot be shadowed.
#:
#: THE MODEL IS A RESPONSE TABLE, not a canned string. Per hostname the
#: environment carries `STUB_<host>_STATUS`, `_RC`, `_LOCATION` and
#: `_BODYFILE`. When `-L` is present the stub follows `_LOCATION` to that
#: hostname's own entry, so a redirect chain is genuinely walked and the `-w`
#: report (status, hop count, effective URL) is computed from the walk instead
#: of being asserted by the test. A hostname with no entry answers 404 with an
#: empty body.
#:
#: `%{...}` is expanded with sed rather than `${out//%{...}/…}`: bash's parser
#: closes the parameter expansion at the first `}`, so the brace form silently
#: produces garbage. That was measured, not assumed.
CURL_STUB = r'''
stub_sed_escape() {
  printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}
curl() {
  local body="" hdrs="" fmt="" url="" follow=no
  while [ $# -gt 0 ]; do
    case "$1" in
      -o) body="$2"; shift 2 ;;
      -D) hdrs="$2"; shift 2 ;;
      -w) fmt="$2"; shift 2 ;;
      -L) follow=yes; shift ;;
      -m|--max-redirs) shift 2 ;;
      --disable|-sS|-s|-S) shift ;;
      http*) url="$1"; shift ;;
      *) shift ;;
    esac
  done
  local current="$url" code=000 hops=0 eff="$url" pending="" rc=0 payload=""
  if [ -n "$hdrs" ]; then : > "$hdrs"; fi
  while : ; do
    local host="${current#*://}"
    host="${host%%/*}"
    local key="STUB_${host//[.-]/_}"
    eval "code=\${${key}_STATUS:-404}"
    eval "rc=\${${key}_RC:-0}"
    eval "pending=\${${key}_LOCATION:-}"
    eval "payload=\${${key}_BODYFILE:-}"
    eff="$current"
    if [ -n "$hdrs" ]; then
      printf 'HTTP/1.1 %s\r\n' "$code" >> "$hdrs"
      if [ -n "$pending" ]; then
        printf 'location: %s\r\n' "$pending" >> "$hdrs"
      fi
      printf '\r\n' >> "$hdrs"
    fi
    if [ -n "$body" ]; then
      if [ "$rc" -ne 0 ]; then
        : > "$body"
      elif [ -n "$payload" ] && [ -f "$payload" ]; then
        cat "$payload" > "$body"
      else
        printf 'STUB-NO-BODY-FOR-%s' "$host" > "$body"
      fi
    fi
    if [ "$rc" -ne 0 ]; then
      code=000
      break
    fi
    if [ -n "$pending" ] && [ "$follow" = yes ]; then
      hops=$((hops + 1))
      if [ "$hops" -ge 5 ]; then
        rc=47
        code=000
        break
      fi
      current="$pending"
      pending=""
      continue
    fi
    break
  done
  if [ -n "$fmt" ]; then
    printf '%s' "$fmt" \
      | sed -e "s|%{http_code}|$(stub_sed_escape "$code")|g" \
            -e "s|%{num_redirects}|$(stub_sed_escape "$hops")|g" \
            -e "s|%{url_effective}|$(stub_sed_escape "$eff")|g" \
            -e "s|%{redirect_url}|$(stub_sed_escape "$pending")|g"
  fi
  return "$rc"
}
'''

#: The mandated behaviours, each as (description, literal-that-must-be-present).
#:
#: Several of the JS suite's tests are generated from data tables rather than
#: written out one by one (one test per malformed header shape, per method, per
#: unconfigured-secret shape). For those the literal is the DATA ENTRY plus the
#: template that renders it, because together those two are what make the test
#: exist: remove the entry and that case is never exercised again. Asserting on
#: the rendered test NAME for those would have required either duplicating the
#: table here or a regex against generated output — both worse.
#:
#: Used only to prove the suite cannot quietly lose a behaviour — never to judge
#: behaviour, which is the JS runner's job.
REQUIRED_BEHAVIOURS = (
    ("no Authorization header -> 401 with a Basic challenge",
     "no Authorization header -> 401 with a Basic challenge"),
    ("a wrong password -> 401",
     "the wrong password -> 401, and next() is never reached"),
    ("correct credentials -> next() runs",
     "correct credentials -> next() runs and the asset is served"),
    ("THE CRITICAL ONE: an internal throw answers 401, never a served page",
     "an internal throw (env getter) answers 401, never a served page"),
    ("an unset DIGEST_PASSWORD fails CLOSED",
     '["DIGEST_PASSWORD empty"'),
    ("a fail-closed rejection is logged loudly",
     "DIGEST_AUTH_MISCONFIGURED"),
    ("a malformed Authorization header is refused, not crashed",
     '"a token that is not base64"'),
    ("every HTTP method is guarded, GET included",
     'const PROTECTED_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", '
     '"OPTIONS", "PATCH"];'),
    ("a HEAD 401 carries the challenge and no body",
     "a HEAD 401 carries the challenge and NO body, as RFC 9110 requires"),
    ("the secret is compared through crypto.subtle, never ===",
     "the secret is compared through crypto.subtle, never with ==="),
    ("the middleware has the try/catch the platform fallback requires",
     "the middleware source contains a try/catch around the whole body"),
)

#: node's own TAP plan line. Read from the runner's summary, never counted
#: from console lines by this file — console output is truncated and a count
#: read off it would silently understate what actually ran.
TAP_PLAN_RE = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)


class NodeMissingIsAFailure(unittest.TestCase):
    def test_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.fail(
                "node was not found on PATH, so the digest archive's access "
                "control tests CANNOT be executed.\n"
                "This fails instead of skipping on purpose: a skipped security "
                "test is a green CI run with no security test, which is the exact "
                "silence that let the archive be published unauthenticated while "
                "the repository reported itself as protected. Fix the "
                "environment (install node, or put it on PATH) rather than the "
                "test."
            )
        proc = subprocess.run([node, "--version"], capture_output=True, text=True,
                              timeout=60)
        print("pages middleware auth tests: node %s (%s)"
              % (proc.stdout.strip(), node))


class MiddlewareJsSuiteTests(unittest.TestCase):
    """Run the JS suite under node and judge it by its EXIT CODE."""

    def test_the_js_suite_passes_against_the_committed_middleware(self):
        node = shutil.which("node")
        if not node:
            self.fail("node is required to execute the access control tests")
        proc = subprocess.run(
            # --test-reporter=tap is requested explicitly rather than relying on
            # node's default `spec` output: TAP is the only reporter that emits
            # a machine-readable plan line ("1..N"), and that line is what lets
            # this wrapper prove the suite RAN N tests instead of printing
            # nothing and exiting 0.
            [node, "--test", "--test-reporter=tap", str(JS_SUITE)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(
            0, proc.returncode,
            "tests/security/pages_middleware_auth.test.js FAILED against %s.\n"
            "This is the digest archive's only access control: the "
            "digest-auth Worker only proxies to this Pages origin, so the "
            "origin must refuse unauthenticated requests itself. An internal "
            "throw escaping into Cloudflare's documented env.ASSETS.fetch() "
            "fallback, an unset DIGEST_PASSWORD failing OPEN, or a non-GET "
            "method going unguarded would each serve the entire archive "
            "publicly with this job still green.\n"
            "---- node output ----\n%s\n------------------"
            % (os.environ.get("PAGES_MIDDLEWARE")
               or "the committed functions/_middleware.js",
               output))
        plan = TAP_PLAN_RE.search(proc.stdout or "")
        self.assertIsNotNone(
            plan,
            "node's own TAP plan line was absent, so this module cannot even "
            "confirm the suite RAN anything. A wrapper that passes because the "
            "runner printed nothing is decoration.\n---- node output ----\n%s"
            % output)
        self.assertGreaterEqual(
            int(plan.group(1)), len(REQUIRED_BEHAVIOURS),
            "node's TAP plan says the suite ran only %s test(s), but %d "
            "mandated behaviours must be covered. Tests were probably deleted."
            % (plan.group(1), len(REQUIRED_BEHAVIOURS)))
        print("pages middleware auth tests: node TAP plan ran %s test(s), all green"
              % plan.group(1))

    def test_the_suite_still_covers_every_mandated_behaviour(self):
        """A suite that can lose a test without noticing is not a suite.

        Purely structural and deliberately dumb: it reads the committed test
        names and asserts the mandated behaviours are still named. It does NOT
        decide whether they pass — that is the test above.
        """
        self.assertTrue(JS_SUITE.is_file(), "%s is missing" % JS_SUITE)
        text = JS_SUITE.read_text(encoding="utf-8")
        missing = [desc for desc, literal in REQUIRED_BEHAVIOURS
                   if literal not in text]
        self.assertEqual(
            [], missing,
            "tests/security/pages_middleware_auth.test.js no longer contains %s. "
            "Each is a mutation-proved behaviour of the archive's access "
            "control; deleting one must be a deliberate act recorded in the "
            "report, never a silent removal." % missing)
        self.assertIn(
            "PAGES_MIDDLEWARE", text,
            "the JS suite must honour the PAGES_MIDDLEWARE override, or nobody "
            "can re-run its red/green pair in one command")

    def test_the_middleware_itself_exists(self):
        """The file the suite judges must be the file Pages would run.

        Cheap and non-vacuous on purpose: if `functions/_middleware.js` is
        renamed or moved, wrangler silently stops deploying any middleware, the
        origin reverts to serving the archive to anyone, and every other check
        in this file would go on passing against a file nobody deploys.
        """
        self.assertTrue(
            MIDDLEWARE.is_file(),
            "%s is missing. Wrangler discovers `./functions` relative to the "
            "directory `wrangler pages deploy` runs in — the repo root here — so "
            "a missing or relocated middleware means the origin serves the "
            "entire 72-page archive to any unauthenticated client with no "
            "error anywhere." % MIDDLEWARE)
        text = MIDDLEWARE.read_text(encoding="utf-8")
        self.assertIn(
            "export async function onRequest", text,
            "%s no longer exports `onRequest`. Pages routes a "
            "`functions/_middleware.js` by that name and silently ignores any "
            "other export — the file would compile and protect nothing."
            % MIDDLEWARE)


class WorkflowGuardTests(unittest.TestCase):
    """The workflow half of the control: the parts that stop the archive being
    republished unprotected, checked against the committed workflow text.

    These are the assertions that catch "the deploy step quietly stopped
    shipping the guard". The deploy step carries `continue-on-error: true` by
    design, so these are the only things standing between a missing
    `functions/` directory and a green job that republished the archive.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "daily-news.yml"

    def test_the_deploy_step_asserts_the_middleware_exists(self):
        self.assertTrue(self.WORKFLOW.is_file(), "%s is missing" % self.WORKFLOW)
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "functions/_middleware.js", text,
            "daily-news.yml never references functions/_middleware.js. The "
            "deploy step has `continue-on-error: true`, so without this "
            "assertion a missing or undetected functions/ directory "
            "republishes the whole archive UNPROTECTED and the job still "
            "passes — the silent hole this assertion exists to close.")
        self.assertIn(
            "WWW-Authenticate", text,
            "daily-news.yml checks that functions/_middleware.js exists but not "
            "that it enforces Basic auth. A file that exists and does nothing "
            "is the shape that looks protected and is not.")

    def test_a_drift_canary_probes_the_origin_and_cannot_pass_when_it_fails(self):
        """The drift canary is the arm that catches "the protection vanished".

        `daily-news.yml`'s deploy step carries `continue-on-error: true` by
        design, so a deploy that drops the auth middleware can leave the job
        green. Nothing offline can see that: it is a fact about the DEPLOYED
        origin, not about the repository. So this asserts the live arm is still
        wired, and — separately — that the arm cannot pass on a failure, which
        is the part a canary most easily gets wrong.

        The second half EXECUTES the committed step body with `curl` replaced by
        a stub that models a per-hostname response table and really walks the
        redirect chain. A real `::error::` and exit 1 is asserted for every
        exposed shape. A canary that cannot go red is decoration with a network
        call in front of it, and this is the same standard
        tests/workflows/test_workflow_lint.py applies to every shell gate in
        this repository.

        Each world is keyed by hostname, because the two hostnames are probed
        independently and a guard that watches only the production one is
        defeated by simply using the other. Every archive body is the REAL
        committed html, never a stand-in: a fabricated fixture would keep these
        tests green after the marker stopped matching the actual archive.
        """
        canary = self._canary_workflow_path()
        self.assertTrue(canary.is_file(), "%s is missing" % canary)
        text = canary.read_text(encoding="utf-8")
        self.assertIn(
            CANARY_STEP_NAME, text,
            "drift-canary.yml has no origin-auth canary. Without it, nothing in "
            "CI can observe that the deployed Pages origin stopped refusing "
            "unauthenticated requests — which is exactly how the archive stayed "
            "public while this repository described itself as protected.")
        # BOTH public hostnames. `master.daily-digest.example.pages.dev` is a
        # second hostname serving the same project; a canary that probed only
        # the production one would leave an unmonitored way in.
        for host in (PROD_HOST, PREVIEW_HOST):
            self.assertIn(host, text,
                          "drift-canary.yml does not probe %s. Both hostnames "
                          "serve the same Pages project from the same "
                          "deployment, so probing only one leaves the other as "
                          "an unmonitored way into the archive." % host)
        self.assertIn("401|403", text,
                      "the canary must still accept 401/403 as 'protected'. "
                      "Accepting any other status as a bare refusal is how a "
                      "canary passes on the exact condition it exists to catch.")

        body = self._canary_step_body(canary)
        self.assertIsNotNone(
            body,
            "the canary step body could not be located in %s" % canary)

        archive_index = _archive_index_path()
        archive_digest = _archive_digest_path()
        self.assertGreater(
            archive_index.stat().st_size, 1000,
            "the archive index fixture is %d bytes, which is too small to be "
            "the real page this canary is supposed to recognise"
            % archive_index.stat().st_size)

        # -- the worlds the canary must judge differently -------------------
        fail_worlds = [
            ("THE EXPOSED CASE: the apex origin serves the archive index",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _refused()}),
            ("ONLY THE master preview hostname is exposed",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _resp("200", archive=archive_index)}),
            ("a 302 whose redirect TARGET serves the archive is a breach, not "
             "a gate — following the redirect must not launder the content",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _resp("302", text=NGINX_302_TEXT,
                                  location="https://%s/digests/2026-09-24-morning.html"
                                           % STUB_TARGET_HOST),
              STUB_TARGET_HOST: _resp("200", archive=archive_digest)}),
            ("an Access chain that still delivers archive bytes fails anyway, "
             "so the Access allowance can never excuse served content",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _access_redirect(),
              ACCESS_HOST: _resp("200", archive=archive_index)}),
            ("a 2xx that is neither a refusal nor the archive is still not "
             "protected — the markers are a second line of defence, not the only one",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _resp("200", text=NEUTRAL_TEXT)}),
            ("a redirect to something that is NOT Access is not a gate",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _resp("302", text=NGINX_302_TEXT,
                                  location="https://%s/" % STUB_ELSEWHERE_HOST),
              STUB_ELSEWHERE_HOST: _resp("200", text=NEUTRAL_TEXT)}),
            ("a 404 is not a proof of protection",
             {PROD_HOST: _resp("404", text=NEUTRAL_TEXT),
              PREVIEW_HOST: _refused()}),
            ("UNREACHABLE: a timeout on the apex arm is not a verdict",
             {PROD_HOST: _resp("000", rc=28),
              PREVIEW_HOST: _refused()}),
            ("a DNS failure on the preview arm alone still fails the canary, "
             "and the healthy apex arm does not launder it",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _resp("000", rc=6)}),
            ("an endless redirect chain is not a verdict either",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _resp("302", text=NGINX_302_TEXT,
                                  location="https://%s/" % PREVIEW_HOST)}),
        ]
        for name, world in fail_worlds:
            proc = self._run_canary(body, world)
            failed = proc.returncode != 0
            self.assertEqual(
                True, failed,
                "drift-canary.yml's origin-auth canary passed a world it must "
                "reject: %s. It exited 0.\nA canary that passes on the exposed "
                "case is worse than no canary: it converts a public archive "
                "into a green badge.\nworld=%r\n---- step stdout ----\n%s\n"
                "---- step stderr ----\n%s"
                % (name, sorted(world), proc.stdout, proc.stderr))
            self.assertIn(
                "::error::", proc.stdout,
                "a failing canary must emit ::error:: so the failure is visible "
                "in the Actions UI and not only in the log tail. Case: %s.\n"
                "---- step stdout ----\n%s" % (name, proc.stdout))

    def test_the_canary_passes_only_when_the_archive_is_provably_gated(self):
        """The other direction, and the reason this method exists.

        The canary judged `master.daily-digest.example.pages.dev` by its status
        code, and that hostname is answered by Cloudflare Access with a 302 to
        its login — a bare nginx body with no digest content in it. So the
        canary went red on EVERY scheduled run while the archive was in fact
        correctly protected. A guard that cries wolf on the correct state is
        worse than no guard: it teaches the owner to ignore the one signal that
        would have told them the protection had gone.

        So these worlds must all exit 0. The first is the live shape, and it is
        the case that is RED against the pre-fix canary: prove it by pointing
        `DRIFT_CANARY_FILE` at a pre-fix workflow and re-running this method.
        """
        canary = self._canary_workflow_path()
        body = self._canary_step_body(canary)
        self.assertIsNotNone(body, "the canary step body is missing from %s" % canary)

        pass_worlds = [
            ("THE LIVE SHAPE: the apex refuses with 401 and master.* is behind "
             "Cloudflare Access — an unauthenticated client is given nothing",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _access_redirect(),
              ACCESS_HOST: _resp("200", text=ACCESS_LOGIN_TEXT)}),
            ("an Access chain that continues on to an identity provider is "
             "still a gate — the verdict must not depend on which host the "
             "chain happens to end at",
             {PROD_HOST: _refused(),
              PREVIEW_HOST: _access_redirect(),
              ACCESS_HOST: _resp("302", text=NGINX_302_TEXT,
                                 location="https://%s/login" % STUB_IDP_HOST),
              STUB_IDP_HOST: _resp("200", text=ACCESS_LOGIN_TEXT)}),
            ("a correctly guarded origin refuses with 401 on BOTH hostnames",
             {PROD_HOST: _refused(), PREVIEW_HOST: _refused()}),
            ("a 403 refusal counts as protected too",
             {PROD_HOST: _refused("403"), PREVIEW_HOST: _refused("403")}),
        ]
        for name, world in pass_worlds:
            proc = self._run_canary(body, world)
            self.assertEqual(
                0, proc.returncode,
                "drift-canary.yml's origin-auth canary FAILED a world that is "
                "genuinely protected: %s.\nworld=%r\nThis is the false failure "
                "that made the canary useless: it reported 'the archive is "
                "exposed' while an anonymous client was being handed nothing "
                "but an Access login page.\n---- step stdout ----\n%s\n"
                "---- step stderr ----\n%s"
                % (name, sorted(world), proc.stdout, proc.stderr))
            self.assertNotIn(
                "::error::", proc.stdout,
                "a protected world must not emit ::error::. Case: %s.\n"
                "---- step stdout ----\n%s" % (name, proc.stdout))

    def test_the_canary_judges_the_served_bytes_not_only_the_status_code(self):
        """The structural half of the fix, so it cannot be undone quietly.

        The status-code table is what produced the false failure, so the shape
        of the probe is asserted directly: it follows the chain, keeps the
        chain's headers, reports where it ended up, and carries the archive
        markers it looks for. Each of these was absent from the pre-fix canary,
        which is why this method is red against it too.
        """
        canary = self._canary_workflow_path()
        body = self._canary_step_body(canary)
        self.assertIsNotNone(body, "the canary step body is missing from %s" % canary)
        for needed, why in (
            ("ARCHIVE_MARKERS",
             "the canary must name the archive markers it looks for; a check "
             "that cannot recognise the archive is not a check"),
            (" -L ",
             "the probe must follow the redirect chain — a 302 that lands on "
             "archive content is the breach it must not launder"),
            (" -D ",
             "the probe must keep every hop's response headers; that is how an "
             "Access gate is recognised anywhere in the chain"),
            ("url_effective",
             "the probe must report the URL it actually ended up at, not only "
             "the one it asked for"),
            ("cloudflareaccess",
             "the canary must name the Access domain it accepts as a "
             "legitimate gate"),
            ("archive-content-served",
             "a served-archive failure must be labelled in the summary, so the "
             "two kinds of failure are distinguishable in the log"),
        ):
            self.assertIn(needed, body, why)

        # NO CREDENTIALS. This is the measurement — what an anonymous client
        # gets — and a credential in a CI log on a failed request is its own
        # leak, so the probe must carry no way to send one.
        leaks = re.findall(
            r"(?:^|\s)(?:-u|--user|--netrc|-A|-E)\s|Authorization:|DIGEST_PASSWORD",
            body)
        self.assertEqual(
            [], leaks,
            "the origin-auth probe can be made to send credentials (%r). It must "
            "not: verifying that auth WORKS is the opposite of what this canary "
            "checks, and a secret echoed into a failed CI log is a second "
            "exposure." % leaks)

    def test_no_protected_fixture_can_satisfy_the_archive_check(self):
        """Guards the fixtures, so a green suite means something.

        If any body used for a 'protected' world happened to contain an archive
        marker, those worlds would pass for the wrong reason — or, worse, the
        FAIL worlds would pass because the marker matched when the intent was
        some other arm. Either way the suite would stop measuring what it
        claims to measure, while still reporting green.
        """
        markers = re.compile(r"Personal News Informer|Digest Archive")
        for label, text in (("UNAUTHORIZED_TEXT", UNAUTHORIZED_TEXT),
                            ("NGINX_302_TEXT", NGINX_302_TEXT),
                            ("ACCESS_LOGIN_TEXT", ACCESS_LOGIN_TEXT),
                            ("NEUTRAL_TEXT", NEUTRAL_TEXT)):
            self.assertIsNone(
                markers.search(text),
                "%s carries an archive marker, so the worlds that use it no "
                "longer prove what they claim to prove" % label)
        for label, path in (("archive index", _archive_index_path()),
                            ("archive digest", _archive_digest_path())):
            self.assertIsNotNone(
                markers.search(path.read_text(encoding="utf-8", errors="replace")),
                "the real %s carries no archive marker, so the canary cannot "
                "detect it being served. If this fails the marker list in "
                "drift-canary.yml and the archive templates have drifted apart."
                % label)

    @staticmethod
    def _canary_workflow_path():
        """The workflow file these tests judge.

        `DRIFT_CANARY_FILE` overrides it, exactly as `PAGES_MIDDLEWARE`
        overrides the middleware for the JS suite, so the pre-fix shape can be
        re-run against these tests in one command instead of being described in
        prose. Overridden or not, the override is printed as a NOTE so it can
        never be mistaken for CI.
        """
        override = os.environ.get("DRIFT_CANARY_FILE")
        if override:
            path = Path(override)
            print("NOTE: judging %s (DRIFT_CANARY_FILE) INSTEAD of %s"
                  % (path, CANARY_WORKFLOW))
            return path
        return CANARY_WORKFLOW

    @classmethod
    def _canary_step_body(cls, path=None):
        """The canary's `run:` block, read verbatim from the parsed YAML.

        Read through the repository's own loader rather than by slicing the
        file, so the shell handed to bash is exactly what GitHub Actions would
        execute — the same discipline tests/workflows/test_workflow_lint.py
        applies, and the same reason Bug B stayed invisible to a stripper.
        """
        try:
            from tests import workflows as W
        except ImportError:  # pragma: no cover - direct execution of this file
            sys.path.insert(0, str(REPO_ROOT))
            from tests import workflows as W
        target = Path(path) if path else cls._canary_workflow_path()
        if not target.is_file():
            return None
        doc = W.load_workflow_text(target.read_text(encoding="utf-8"))
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in (job.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                if str(step.get("name") or "") != CANARY_STEP_NAME:
                    continue
                run = step.get("run")
                return run if isinstance(run, str) else None
        return None

    def _run_canary(self, body, world):
        """Execute the canary body against a stubbed `curl`.

        `curl` is a stub rather than the real binary so the cases are
        deterministic and offline — no network, and no chance of the answer
        depending on what the live origin happens to return today. Everything
        else in the step is the committed shell.

        `world` is a {hostname: response} table, because the canary loops over
        TWO hostnames and follows whatever redirect chain it is given. A single
        canned response could not express "the apex refuses but the master
        preview hostname ends up on an Access login", which is precisely the
        world this canary got wrong.

        Hostnames absent from the table answer 404 with no body, so a test that
        forgot to model a hop gets a loud "not a gate" failure rather than a
        silent pass.
        """
        import subprocess
        import tempfile

        bash = shutil.which("bash")
        self.assertIsNotNone(
            bash,
            "bash was not found on PATH, so the origin-auth canary CANNOT be "
            "executed. This fails rather than skipping: a skipped security "
            "canary is a green run with no canary, which is the silence this "
            "whole control exists to end.")

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            env = dict(os.environ)
            # Forward slashes throughout: the step builds
            # "$RUNNER_TEMP/digest_probe_<host>.html" itself, and a Windows
            # backslash path containing a space is one more thing that can fail
            # for a reason that has nothing to do with the control under test.
            env["RUNNER_TEMP"] = tmpdir.as_posix()
            for host, resp in world.items():
                key = "STUB_" + _stub_key(host)
                env[key + "_STATUS"] = str(resp.get("status", "200"))
                env[key + "_RC"] = str(resp.get("rc", 0))
                if resp.get("location"):
                    env[key + "_LOCATION"] = str(resp["location"])
                if resp.get("archive") is not None:
                    env[key + "_BODYFILE"] = Path(resp["archive"]).as_posix()
                elif resp.get("text") is not None:
                    fixture = tmpdir / ("body_%s.txt" % _stub_key(host))
                    fixture.write_text(resp["text"], encoding="utf-8", newline="")
                    env[key + "_BODYFILE"] = fixture.as_posix()
            return subprocess.run([bash, "-c", CURL_STUB + body], cwd=tmp,
                                  env=env, capture_output=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=120)

    def test_the_deploy_step_writes_routes_json_explicitly(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        # A literal substring, not a regex. The payload is a fixed byte string
        # with `*` and `[` in it, and a hand-escaped regex for it would be a
        # second thing to get wrong -- and a regex that silently stops matching
        # is exactly the "cannot fail" decoration this repository forbids.
        self.assertIn(
            """printf '{"version":1,"include":["/*"],"exclude":[]}'""", text,
            "daily-news.yml does not write the exact _routes.json that makes the "
            "middleware cover every route. Left to auto-generation, a generated "
            "`exclude` \"always takes priority over include\" and could exempt "
            "the static archive from the guard while looking perfectly healthy.")
        # And it must land BEFORE the deploy, not after: written afterwards it
        # would never be uploaded with the deployment.
        routes_at = text.index(
            """printf '{"version":1,"include":["/*"],"exclude":[]}'""")
        deploy_at = text.index("npx wrangler pages deploy")
        self.assertLess(
            routes_at, deploy_at,
            "_routes.json is written AFTER `wrangler pages deploy` in "
            "daily-news.yml. It would not be uploaded with this deployment, so "
            "the guard would ship relying on whatever wrangler auto-generates.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
