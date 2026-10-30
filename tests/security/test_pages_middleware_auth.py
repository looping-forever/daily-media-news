#!/usr/bin/env python3
"""tests/security/test_pages_middleware_auth.py — CI entry point for the
digest archive's PUBLIC access contract.

WHAT THIS CONTROLS
------------------
The digest archive is public by design (2026-10-30). `functions/_middleware.js`
used to gate it behind a username/password prompt; that gate was removed because
the owner could not open the site from a phone's in-app browser, which
suppresses the native login dialog, and because this repository is public — every
digest page was already readable anonymously from GitHub, so the password
protected nothing.

The file still exists as a pass-through: wrangler discovers `./functions`
relative to the directory the deploy command runs in, and the deploy contract is
checked in. The behaviour under test is "an anonymous request reaches the static
assets and no credential prompt is sent back".

WHY A PYTHON WRAPPER EXISTS AT ALL
---------------------------------
The behaviour is JavaScript and the JS tests live in
`tests/security/pages_middleware_auth.test.js`. The runner that enforces this
repository is `drift-canary.yml`'s `test-suite` job, whose `run_suite` entries
are `python3 -m unittest` invocations, and it has no node setup step. This
module makes the JS suite reachable from that inventory — and a MISSING node
fails loudly rather than skipping, because a skipped security test is a green CI
run with no security test.

The second half executes the committed `drift-canary.yml` step body against a
stubbed `curl`, so the public-access canary is judged by its real behaviour, not
by a copy of it.

RUN IT YOURSELF
    node --test tests/security/pages_middleware_auth.test.js
    python -m unittest tests.security.test_pages_middleware_auth -v

REPRODUCING THE RED
-------------------
`PAGES_MIDDLEWARE` points the JS suite at another file. `DRIFT_CANARY_FILE`
points the canary tests at another workflow file. Both overrides print a NOTE so
they can never be mistaken for CI.
"""

import os
import re
import shutil
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JS_SUITE = REPO_ROOT / "tests" / "security" / "pages_middleware_auth.test.js"
MIDDLEWARE = REPO_ROOT / "functions" / "_middleware.js"
CANARY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "drift-canary.yml"

#: The canary step these tests judge, named exactly as it is named in the
#: workflow, so a rename is one edit and fails loudly instead of skipping.
CANARY_STEP_NAME = "Digest archive is publicly reachable (origin public-access canary)"

# ---------------------------------------------------------------------------
# The probed world. Synthetic hostnames, real archive bodies.
# ---------------------------------------------------------------------------

#: The production origin, served by the pass-through middleware.
PROD_HOST = "digest-archive-canary.pages.dev"

#: The second public hostname for the same Pages project, derived by the
#: resolver from PROD_HOST. Both arms are probed because a guard that watches
#: only one leaves the other as an unmonitored way in.
PREVIEW_HOST = "master.digest-archive-canary.pages.dev"

#: Where Cloudflare Access sends a gated hostname. Access intercepts AHEAD of
#: the Pages Function, so with the gate removed this is now a FAILURE, not a
#: pass: the owner would get a login page instead of the digest.
ACCESS_HOST = "daily-digest-example.cloudflareaccess.com"
ACCESS_LOGIN_URL = ("https://%s/cdn-cgi/access/login/"
                    "0123456789abcdef0123456789abcdef") % ACCESS_HOST

#: Reserved-by-RFC-2606 hosts for redirect targets. If the stub ever stopped
#: intercepting curl these would fail loudly instead of reaching a real server.
STUB_TARGET_HOST = "redirect-target.invalid"
STUB_ELSEWHERE_HOST = "elsewhere.invalid"
STUB_IDP_HOST = "idp.example.invalid"

#: The retired 401 body, kept only as a realistic refusal shape. It carries no
#: archive marker, so a world that serves it can never satisfy the archive check.
UNAUTHORIZED_TEXT = (
    "401 Unauthorized\n"
    "\n"
    "This digest archive is private and requires HTTP Basic Auth.\n"
    "\n"
    "Use the username and password configured for this archive.\n"
)

#: The bare nginx body Cloudflare's edge sends with an Access 302.
NGINX_302_TEXT = (
    "<html>\r\n"
    "<head><title>302 Found</title></head>\r\n"
    "<body>\r\n"
    "<center><h1>302 Found</h1></center>\r\n"
    "<hr><center>nginx</center>\r\n"
    "</body>\r\n"
    "</html>\r\n"
)

#: A stand-in for the Cloudflare Access login page; no archive marker.
ACCESS_LOGIN_TEXT = (
    "<!DOCTYPE html><html><head><title>Access denied</title></head>"
    "<body><div id=\"cf-wrapper\"><h1>Log in to daily-digest</h1></div>"
    "<script src=\"/cdn-cgi/access/login/0123456789abcdef0123456789abcdef\">"
    "</script></body></html>\n"
)

#: A 200 that is not the archive: production serving something else must fail.
NEUTRAL_TEXT = "<html><body><h1>Nothing to see</h1></body></html>\n"


def _archive_index_path():
    """The REAL archive index this repository ships, read as bytes.

    Not a hand-written stand-in: a fabricated fixture would keep these tests
    green after the canary's markers stopped matching the actual archive.
    """
    path = REPO_ROOT / "docs" / "index.html"
    if not path.is_file():
        raise AssertionError("%s is missing: the archive fixture is the real "
                             "index, not a stand-in" % path)
    return path


def _archive_digest_path():
    """The real oldest digest page, so a single served page is still caught."""
    digests = sorted((REPO_ROOT / "docs" / "digests").glob("*.html"))
    if not digests:
        raise AssertionError("no real digest page under docs/digests/")
    return digests[0]


def _resp(status="200", text=None, archive=None, location=None, rc=0):
    """One modelled HTTP response for one hostname."""
    out = {"status": status, "rc": rc}
    if location is not None:
        out["location"] = location
    if archive is not None:
        out["archive"] = archive
    elif text is not None:
        out["text"] = text
    return out


def _refused(status="401"):
    return _resp(status, text=UNAUTHORIZED_TEXT)


def _access_redirect():
    return _resp("302", text=NGINX_302_TEXT, location=ACCESS_LOGIN_URL)


def _stub_key(host):
    """The environment-variable stem the stub answers a hostname from."""
    return host.replace(".", "_").replace("-", "_")


def _find_bash():
    """bash from PATH, or Git-for-Windows' copy when PATH does not carry it.

    The workflow runs on ubuntu-latest, where bash is on PATH. This repository is
    also exercised on Windows, where Git installs bash without adding it to
    PATH; searching the install locations keeps the canary executable there
    instead of turning a missing PATH entry into a fake failure (or a skip).
    """
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


#: The stubbed `curl`, prepended to the canary step's own shell.
#:
#: A BASH FUNCTION, not an executable on PATH: Git-for-Windows bash prepends its
#: own /mingw64/bin, so a PATH stub loses to the real curl and the test would
#: silently fetch the LIVE origin. A shell function is resolved by the language
#: before PATH and cannot be shadowed.
#:
#: The model is a per-hostname response table (`STUB_<host>_STATUS`, `_RC`,
#: `_LOCATION`, `_BODYFILE`). With `-L` the stub walks `_LOCATION`, so a redirect
#: chain is genuinely followed and the `-w` report is computed, not asserted.
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

#: The mandated JS behaviours, each as (description, literal-in-the-JS-suite).
REQUIRED_BEHAVIOURS = (
    ("onRequest is exported and callable",
     "the middleware exports a callable onRequest"),
    ("an anonymous GET reaches next() with a 200",
     "an anonymous GET calls next() and serves the asset"),
    ("the asset response is returned unchanged",
     "the response object from next() is returned unchanged"),
    ("every HTTP method is passed through",
     "every HTTP method is passed through, GET included"),
    ("the retired secrets are never read",
     "the pass-through never reads the retired secret names"),
    ("the source carries no auth artefacts",
     "the middleware source carries no auth artefacts"),
    ("the source is a real pass-through",
     "the middleware is a pass-through, not merely a 200"),
    ("consecutive requests are all served",
     "consecutive requests all reach the asset"),
)

#: node's own TAP plan line; never counted from console lines.
TAP_PLAN_RE = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)


class NodeMissingIsAFailure(unittest.TestCase):
    def test_node_is_available(self):
        import subprocess
        node = shutil.which("node")
        if not node:
            self.fail(
                "node was not found on PATH, so the digest archive's public "
                "access tests CANNOT be executed. This fails instead of "
                "skipping on purpose: a skipped security test is a green CI "
                "run with no security test.")
        proc = subprocess.run([node, "--version"], capture_output=True, text=True,
                              timeout=60)
        print("pages middleware auth tests: node %s (%s)"
              % (proc.stdout.strip(), node))


class MiddlewareJsSuiteTests(unittest.TestCase):
    """Run the JS suite under node and judge it by its EXIT CODE."""

    def test_the_js_suite_passes_against_the_committed_middleware(self):
        import subprocess
        node = shutil.which("node")
        if not node:
            self.fail("node is required to execute the public-access tests")
        proc = subprocess.run(
            [node, "--test", "--test-reporter=tap", str(JS_SUITE)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(
            0, proc.returncode,
            "tests/security/pages_middleware_auth.test.js FAILED against %s.\n"
            "This is the digest archive's public-access contract: the site is "
            "open by design, and a credential prompt, a failed pass-through or "
            "a missing onRequest export would each break the link the owner "
            "opens.\n---- node output ----\n%s\n------------------"
            % (os.environ.get("PAGES_MIDDLEWARE")
               or "the committed functions/_middleware.js", output))
        plan = TAP_PLAN_RE.search(proc.stdout or "")
        self.assertIsNotNone(
            plan,
            "node's own TAP plan line was absent, so this module cannot confirm "
            "the suite RAN anything.\n---- node output ----\n%s" % output)
        self.assertGreaterEqual(
            int(plan.group(1)), len(REQUIRED_BEHAVIOURS),
            "node's TAP plan says the suite ran only %s test(s), but %d mandated "
            "behaviours must be covered. Tests were probably deleted."
            % (plan.group(1), len(REQUIRED_BEHAVIOURS)))
        print("pages middleware auth tests: node TAP plan ran %s test(s), all green"
              % plan.group(1))

    def test_the_suite_still_covers_every_mandated_behaviour(self):
        self.assertTrue(JS_SUITE.is_file(), "%s is missing" % JS_SUITE)
        text = JS_SUITE.read_text(encoding="utf-8")
        missing = [desc for desc, literal in REQUIRED_BEHAVIOURS
                   if literal not in text]
        self.assertEqual(
            [], missing,
            "tests/security/pages_middleware_auth.test.js no longer contains %s. "
            "Each is a required behaviour of the public-access contract; deleting "
            "one must be deliberate and recorded, never silent." % missing)
        self.assertIn(
            "PAGES_MIDDLEWARE", text,
            "the JS suite must honour the PAGES_MIDDLEWARE override, or nobody "
            "can re-run its red/green pair in one command")

    def test_the_middleware_itself_is_the_shipped_pass_through(self):
        self.assertTrue(
            MIDDLEWARE.is_file(),
            "%s is missing. The site still works without it, but the checked-in "
            "deploy contract and these tests would then describe a file that no "
            "longer exists." % MIDDLEWARE)
        text = MIDDLEWARE.read_text(encoding="utf-8")
        self.assertIn(
            "export async function onRequest", text,
            "%s no longer exports `onRequest`. Pages routes a "
            "`functions/_middleware.js` by that name and silently ignores any "
            "other export." % MIDDLEWARE)
        self.assertIn(
            "context.next()", text,
            "%s does not call context.next(), so it is not the pass-through this "
            "repository claims to deploy." % MIDDLEWARE)
        for needle in ("WWW-Authenticate", "DIGEST_USERNAME", "DIGEST_PASSWORD"):
            self.assertNotIn(
                needle, text,
                "%s still carries %r. The credential gate was removed on "
                "2026-10-30 and the site is public by design." % (MIDDLEWARE, needle))
        email_shaped = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                                  text)
        self.assertEqual(
            [], email_shaped,
            "%s contains an email-shaped literal (%r). This repository is public."
            % (MIDDLEWARE, email_shaped))


class WorkflowGuardTests(unittest.TestCase):
    """The workflow half: the deploy assertion and the origin canary."""

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "daily-news.yml"

    def test_the_deploy_step_asserts_the_pass_through_and_no_prompt(self):
        self.assertTrue(self.WORKFLOW.is_file(), "%s is missing" % self.WORKFLOW)
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "FATAL: functions/_middleware.js is missing", text,
            "daily-news.yml no longer refuses a deploy without "
            "functions/_middleware.js, so the checked-in pass-through could "
            "silently stop shipping while the job stays green.")
        self.assertIn(
            "if grep -q 'WWW-Authenticate' functions/_middleware.js", text,
            "daily-news.yml does not refuse a middleware that reintroduces a "
            "credential prompt. The site is public by design; the assertion must "
            "be inverted (present header = fatal).")
        self.assertIn(
            "anonymous GET", text,
            "daily-news.yml does not probe the deployed origin anonymously after "
            "deploy, so a gated deploy would be discovered only by the owner.")

    def test_a_drift_canary_probes_the_origin_and_cannot_pass_when_it_fails(self):
        canary = self._canary_workflow_path()
        self.assertTrue(canary.is_file(), "%s is missing" % canary)
        text = canary.read_text(encoding="utf-8")
        self.assertIn(
            CANARY_STEP_NAME, text,
            "drift-canary.yml has no public-access canary. Without it nothing in "
            "CI can observe that the deployed site stopped opening anonymously.")
        self.assertIn(
            'HOSTS="$(python3 scripts/archive_origin.py --hosts)"', text,
            "drift-canary.yml does not resolve its probe hosts through "
            "scripts/archive_origin.py. A host written into the workflow is a "
            "second copy of a value that lives in CF_ARCHIVE_ORIGIN.")
        self.assertNotIn(
            "example.pages.dev", text,
            "drift-canary.yml probes a placeholder hostname again; it answers 404 "
            "and measures nothing.")

        body = self._canary_step_body(canary)
        self.assertIsNotNone(body,
                             "the canary step body could not be located in %s" % canary)

        archive_index = _archive_index_path()
        archive_digest = _archive_digest_path()
        self.assertGreater(
            archive_index.stat().st_size, 1000,
            "the archive index fixture is %d bytes, too small to be the real page"
            % archive_index.stat().st_size)

        # The shapes that must now FAIL: each is a site the owner cannot open.
        fail_worlds = [
            ("THE REGRESSION: both hostnames refuse with 401 again",
             {PROD_HOST: _refused(), PREVIEW_HOST: _refused()}),
            ("ONLY the master preview hostname is gated",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _refused("403")}),
            ("master.* is behind Cloudflare Access, so a browser gets a login page",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _access_redirect(),
              ACCESS_HOST: _resp("200", text=ACCESS_LOGIN_TEXT)}),
            ("an Access chain that continues to an identity provider is still a gate",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _access_redirect(),
              ACCESS_HOST: _resp("302", text=NGINX_302_TEXT,
                                 location="https://%s/login" % STUB_IDP_HOST),
              STUB_IDP_HOST: _resp("200", text=ACCESS_LOGIN_TEXT)}),
            ("a 2xx that serves something other than the archive is not the digest",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _resp("200", text=NEUTRAL_TEXT)}),
            ("a 404 is not a public archive",
             {PROD_HOST: _resp("404", text=NEUTRAL_TEXT),
              PREVIEW_HOST: _resp("200", archive=archive_index)}),
            ("UNREACHABLE: a timeout on the apex arm is not a verdict",
             {PROD_HOST: _resp("000", rc=28),
              PREVIEW_HOST: _resp("200", archive=archive_index)}),
            ("a DNS failure on the preview arm alone still fails the canary",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _resp("000", rc=6)}),
            ("an endless redirect chain is not a verdict either",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _resp("302", text=NGINX_302_TEXT,
                                  location="https://%s/" % PREVIEW_HOST)}),
            ("a redirect to something that serves no archive is not the digest",
             {PROD_HOST: _resp("200", archive=archive_index),
              PREVIEW_HOST: _resp("302", text=NGINX_302_TEXT,
                                  location="https://%s/" % STUB_ELSEWHERE_HOST),
              STUB_ELSEWHERE_HOST: _resp("200", text=NEUTRAL_TEXT)}),
        ]
        for name, world in fail_worlds:
            proc = self._run_canary(body, world)
            self.assertNotEqual(
                0, proc.returncode,
                "drift-canary.yml's public-access canary passed a world it must "
                "reject: %s. It exited 0, so a site the owner cannot open would "
                "report green.\nworld=%r\n---- step stdout ----\n%s\n"
                "---- step stderr ----\n%s"
                % (name, sorted(world), proc.stdout, proc.stderr))
            self.assertIn(
                "::error::", proc.stdout,
                "a failing canary must emit ::error:: so the failure is visible "
                "in the Actions UI. Case: %s.\n---- step stdout ----\n%s"
                % (name, proc.stdout))

    def test_the_canary_passes_when_the_archive_is_open_anonymously(self):
        canary = self._canary_workflow_path()
        body = self._canary_step_body(canary)
        self.assertIsNotNone(body, "the canary step body is missing from %s" % canary)

        pass_worlds = [
            ("THE LIVE SHAPE: both hostnames answer anonymously with the archive",
             {PROD_HOST: _resp("200", archive=_archive_index_path()),
              PREVIEW_HOST: _resp("200", archive=_archive_index_path())}),
            ("the branch hostname serves a digest page rather than the index",
             {PROD_HOST: _resp("200", archive=_archive_index_path()),
              PREVIEW_HOST: _resp("200", archive=_archive_digest_path())}),
            ("a redirect that lands on the archive is still public access",
             {PROD_HOST: _resp("302", text=NGINX_302_TEXT,
                               location="https://%s/digests/2026-09-24-morning.html"
                                        % STUB_TARGET_HOST),
              STUB_TARGET_HOST: _resp("200", archive=_archive_digest_path()),
              PREVIEW_HOST: _resp("200", archive=_archive_index_path())}),
        ]
        for name, world in pass_worlds:
            proc = self._run_canary(body, world)
            self.assertEqual(
                0, proc.returncode,
                "drift-canary.yml's public-access canary FAILED a world where the "
                "archive is open anonymously: %s.\nworld=%r\nA canary that is red "
                "on the correct state teaches the owner to ignore it.\n"
                "---- step stdout ----\n%s\n---- step stderr ----\n%s"
                % (name, sorted(world), proc.stdout, proc.stderr))
            self.assertNotIn(
                "::error::", proc.stdout,
                "a passing world must not emit ::error::. Case: %s.\n"
                "---- step stdout ----\n%s" % (name, proc.stdout))

    def test_the_canary_judges_the_served_bytes_and_the_access_gate(self):
        canary = self._canary_workflow_path()
        body = self._canary_step_body(canary)
        self.assertIsNotNone(body, "the canary step body is missing from %s" % canary)
        for needed, why in (
            ("ARCHIVE_MARKERS",
             "the canary must name the archive markers it looks for"),
            (" -L ",
             "the probe must follow the redirect chain"),
            (" -D ",
             "the probe must keep every hop's response headers, so an Access gate "
             "is recognised anywhere in the chain"),
            ("url_effective",
             "the probe must report the URL it actually ended up at"),
            ("cloudflareaccess",
             "the canary must recognise the Access domain it treats as a gate"),
            ("access-gate",
             "an Access gate must be a labelled failure"),
            ("auth-required",
             "a 401/403 must be a labelled failure, not a pass"),
            ("archive served anonymously",
             "a pass must say the archive was served to an anonymous client"),
        ):
            self.assertIn(needed, body, why)

    def test_no_protected_fixture_can_satisfy_the_archive_check(self):
        """Guards the fixtures, so a green suite means something."""
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
        override = os.environ.get("DRIFT_CANARY_FILE")
        if override:
            path = Path(override)
            print("NOTE: judging %s (DRIFT_CANARY_FILE) INSTEAD of %s"
                  % (path, CANARY_WORKFLOW))
            return path
        return CANARY_WORKFLOW

    @classmethod
    def _canary_step_body(cls, path=None):
        """The canary's `run:` block, read verbatim from the parsed YAML."""
        try:
            from tests import workflows as W
        except ImportError:  # pragma: no cover - direct execution
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

        Everything except `curl` is the committed shell, so the step's real
        `::error::` lines and exit code are what is being judged.
        """
        import subprocess
        import tempfile

        bash = _find_bash()
        self.assertIsNotNone(
            bash,
            "bash was not found on PATH, so the public-access canary CANNOT be "
            "executed. This fails rather than skipping.")

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            env = dict(os.environ)
            env["RUNNER_TEMP"] = tmpdir.as_posix()
            (tmpdir / "scripts").mkdir()
            shutil.copyfile(REPO_ROOT / "scripts" / "archive_origin.py",
                            tmpdir / "scripts" / "archive_origin.py")
            env.pop("DIGEST_ARCHIVE_ORIGIN", None)
            env["CF_ARCHIVE_ORIGIN"] = "https://" + PROD_HOST
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
        self.assertIn(
            """printf '{"version":1,"include":["/*"],"exclude":[]}'""", text,
            "daily-news.yml does not write the exact _routes.json that makes the "
            "pass-through cover every route (and keeps the deploy contract).")
        routes_at = text.index(
            """printf '{"version":1,"include":["/*"],"exclude":[]}'""")
        deploy_at = text.index("npx wrangler pages deploy")
        self.assertLess(
            routes_at, deploy_at,
            "_routes.json is written AFTER `wrangler pages deploy` in "
            "daily-news.yml, so it would not be uploaded with this deployment.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
