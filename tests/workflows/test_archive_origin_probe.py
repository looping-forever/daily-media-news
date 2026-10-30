#!/usr/bin/env python3
"""The privacy canary must probe the archive, not a placeholder hostname.

WHY THIS FILE EXISTS
--------------------
`drift-canary.yml`'s origin canary carried its two probe hostnames as a
literal ending in `.example.pages.dev`. That is the placeholder the private
sanitiser substitutes for the real Pages hostname, so the step probed a hostname
Cloudflare answers with 404 and compared it against "expected 401/403" -- red on
every scheduled run, while the real origin was never probed at all.

The hostname cannot be written in the repository (that is what the sanitiser
prevents), so it is resolved at run time from `CF_ARCHIVE_ORIGIN`. This suite
pins both halves:

  1. the RESOLVER -- it refuses an unset, malformed or placeholder origin with a
     `::error::` line and a non-zero exit, and it derives the second public
     hostname only for a Pages origin;
  2. the WIRING -- the workflow step actually calls it and carries the variable,
     so the resolver cannot be left unused.

The refusal cases are not hypothetical. The literal this replaced is asserted
here as a REFUSED input, which is what makes the defect un-reintroducible: put
that hostname back in the workflow and the probe layer now says why it cannot be
probed instead of quietly measuring a 404.
"""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

try:
    from tests import workflows as W
except ImportError:  # pragma: no cover - direct execution of this file
    sys.path.insert(0, str(REPO_ROOT))
    from tests import workflows as W

import archive_origin as ao  # noqa: E402

STEP_NAME = "Digest archive is publicly reachable (origin public-access canary)"

#: Synthetic, and deliberately not the real project: this file is published, so a
#: real hostname here would be rewritten by the sanitiser and the assertion below
#: would be checking a placeholder again.
FIXTURE_HOST = "canary-fixture-42.pages.dev"
FIXTURE_ORIGIN = "https://" + FIXTURE_HOST

#: The exact literal the step shipped with. `example` is RFC 2606 reserved.
PLACEHOLDER_ORIGIN = "https://daily-digest.example.pages.dev"


def _run_main(argv, environ):
    """Run the CLI with `environ` and return (exit_code, stdout)."""
    out = io.StringIO()
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environ)
        with contextlib.redirect_stdout(out):
            code = ao.main(argv)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return code, out.getvalue()


class ConfigurationTests(unittest.TestCase):
    def test_the_worker_name_is_preferred_and_the_repository_variable_is_the_fallback(self):
        """Both stores are real: the Worker holds a secret, Actions holds a variable.

        Reading only one of them would make the canary green or red depending on
        which side of the fence the value was configured on.
        """
        name, value = ao.configured_origin({"DIGEST_ARCHIVE_ORIGIN": FIXTURE_ORIGIN,
                                            "CF_ARCHIVE_ORIGIN": "https://other.pages.dev"})
        self.assertEqual(("DIGEST_ARCHIVE_ORIGIN", FIXTURE_ORIGIN), (name, value))

        name, value = ao.configured_origin({"CF_ARCHIVE_ORIGIN": FIXTURE_ORIGIN})
        self.assertEqual(("CF_ARCHIVE_ORIGIN", FIXTURE_ORIGIN), (name, value))

    def test_an_unset_repository_variable_arrives_as_empty_not_missing(self):
        """GitHub substitutes an undefined `vars.*` as "", which is not a value.

        Treating "" as configured is what would send the probe to `https:///`.
        """
        name, value = ao.configured_origin({"CF_ARCHIVE_ORIGIN": ""})
        self.assertEqual(("DIGEST_ARCHIVE_ORIGIN", ""), (name, value))


class ResolutionTests(unittest.TestCase):
    def test_a_bare_https_origin_is_accepted_and_normalised(self):
        for raw in (FIXTURE_ORIGIN, FIXTURE_ORIGIN + "/", "  " + FIXTURE_ORIGIN + "  ",
                    "https://CANARY-Fixture-42.pages.dev"):
            self.assertEqual(FIXTURE_ORIGIN, ao.resolve_origin(raw), raw)

    def test_the_refusal_literal_is_the_one_this_canary_shipped_with(self):
        """REGRESSION PROOF, in the direction that matters.

        The defect was not "a hostname was wrong"; it was that a WRONG hostname
        was accepted as a probe target and its 404 reported as a failed guard.
        """
        with self.assertRaises(ao.ArchiveOriginError) as caught:
            ao.resolve_origin(PLACEHOLDER_ORIGIN)
        self.assertIn("PLACEHOLDER", str(caught.exception))
        self.assertIn("example", str(caught.exception))

    def test_every_reserved_placeholder_label_is_refused(self):
        for label in ("example", "invalid", "test", "localhost"):
            with self.assertRaises(ao.ArchiveOriginError, msg=label):
                ao.resolve_origin("https://digest.%s.pages.dev" % label)

    def test_the_variable_is_named_when_it_is_missing(self):
        with self.assertRaises(ao.ArchiveOriginError) as caught:
            ao.resolve_origin("", "CF_ARCHIVE_ORIGIN")
        message = str(caught.exception)
        self.assertIn("CF_ARCHIVE_ORIGIN", message)
        self.assertIn("is not set", message)

    def test_an_origin_that_is_not_a_bare_https_origin_is_refused(self):
        for raw in ("http://" + FIXTURE_HOST,
                    FIXTURE_HOST,
                    "https://" + FIXTURE_HOST + "/digests/",
                    "https://" + FIXTURE_HOST + "?health=1",
                    "https://" + FIXTURE_HOST + "/#top",
                    "https://" + FIXTURE_HOST + ":8443",
                    "https://user:pw@" + FIXTURE_HOST,
                    "https://"):
            with self.assertRaises(ao.ArchiveOriginError, msg=raw):
                ao.resolve_origin(raw)


class ProbeHostTests(unittest.TestCase):
    def test_a_pages_origin_gets_both_public_hostnames(self):
        """The second arm is the one that catches Access being removed.

        Pages serves the same project from `<project>.pages.dev` and
        `<branch>.<project>.pages.dev`. A guard that keeps only the first leaves
        the second as an unmonitored way into the archive.
        """
        self.assertEqual((FIXTURE_HOST, "master." + FIXTURE_HOST),
                         ao.probe_hosts(FIXTURE_ORIGIN))

    def test_a_non_pages_origin_gets_one_hostname_rather_than_a_made_up_second(self):
        """The `master.` arm is a Pages fact. Inventing it elsewhere would probe a
        hostname that does not exist and report a hard failure about it."""
        self.assertEqual(("archive.mysite.dev",),
                         ao.probe_hosts("https://archive.mysite.dev"))


class CliTests(unittest.TestCase):
    def test_hosts_prints_the_two_arms_and_exits_zero(self):
        code, out = _run_main(["--hosts"], {"CF_ARCHIVE_ORIGIN": FIXTURE_ORIGIN})
        self.assertEqual(0, code, out)
        self.assertEqual([FIXTURE_HOST, "master." + FIXTURE_HOST],
                         out.split())

    def test_an_unset_origin_exits_two_with_an_annotation_and_no_hostname(self):
        """`set -euo pipefail` turns this into a red step; the annotation says why.

        The step body uses `HOSTS="$(...)"`, so a non-zero exit aborts before any
        probe runs. Nothing is printed that could be mistaken for a verdict.
        """
        code, out = _run_main(["--hosts"], {})
        self.assertEqual(2, code)
        self.assertIn("::error::", out)
        self.assertIn("CF_ARCHIVE_ORIGIN", out)
        self.assertEqual([], [line for line in out.splitlines()
                              if "::error::" not in line and line.strip()])

    def test_a_placeholder_origin_exits_two_rather_than_probing_it(self):
        code, out = _run_main(["--hosts"], {"CF_ARCHIVE_ORIGIN": PLACEHOLDER_ORIGIN})
        self.assertEqual(2, code)
        self.assertIn("::error::", out)


class WorkflowWiringTests(unittest.TestCase):
    """The resolver is only worth anything if the step actually calls it."""

    def _step(self):
        found = [(job, step) for job, name, step in W.iter_all_steps("drift-canary.yml")
                 if name == STEP_NAME]
        self.assertEqual(1, len(found),
                         "expected exactly one %r step in drift-canary.yml" % STEP_NAME)
        return found[0]

    def test_the_step_resolves_its_hostnames_instead_of_carrying_them(self):
        job, step = self._step()
        run = step.get("run") or ""
        self.assertIn("scripts/archive_origin.py --hosts", run,
                      "%s / %r does not resolve its probe hostnames" % (job, STEP_NAME))
        self.assertNotIn("example.pages.dev", run,
                         "the step is probing a placeholder hostname again")

    def test_the_step_carries_the_repository_variable(self):
        _job, step = self._step()
        env = step.get("env") or {}
        self.assertEqual("${{ vars.CF_ARCHIVE_ORIGIN }}", env.get("CF_ARCHIVE_ORIGIN"),
                         "the step must pass the repository variable through: the "
                         "hostname cannot be committed")

    def test_the_step_still_fails_the_run_when_the_origin_is_missing(self):
        """A resolver that cannot fail loudly is a guard that cannot fire."""
        _job, step = self._step()
        run = step.get("run") or ""
        self.assertIn("set -euo pipefail", run,
                      "without `pipefail` the HOSTS assignment would swallow the "
                      "resolver's exit code and the loop would run over nothing")


if __name__ == "__main__":
    unittest.main()
