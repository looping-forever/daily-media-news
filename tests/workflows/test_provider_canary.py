#!/usr/bin/env python3
"""tests/workflows/test_provider_canary.py -- the alarm, not the diagnostic.

WHY THIS EXISTS. A provider family can retire a model -- or the whole service --
and nothing in the repository says so: the ladder keeps trying it, the edition
keeps degrading, and the owner finds out by reading a run log. Probe 42363318770
is the case: GitHub Models answered HTTP 200 with plain-text `OK` to every path.
This canary is the thing that fails the run instead.

The contract pinned here:
    A DARK FAMILY FAILS AND NAMES THE FIX -- not "probe failed".
    quota AND not_configured ARE EXCUSED -- loudly, with the reason.
    EVERY FAMILY APPEARS ONCE -- checked, excused or dark.
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import llm_providers as lp  # noqa: E402
import provider_canary as pc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "provider-canary.yml"

OK_BODY = json.dumps({"choices": [{"message": {"content": "OK"}}]})

ENV = {
    "GEMINI_API_KEY": "k1",
    "GEMINI_API_KEY_2": "k2",
    "GROQ_API_KEY": "k3",
    "OPENROUTER_API_KEY": "k4",
    "MISTRAL_API_KEY": "k5",
}


def router(answers):
    """Per-provider (status, body); anything unnamed answers 200 and a reply."""
    def send(spec):
        record = None
        for candidate in lp.PROVIDERS.values():
            if spec["url"] in (candidate.url, candidate.models_url):
                record = candidate
                break
        if record is not None and record.name in answers:
            return answers[record.name]
        if spec["url"].endswith("/models"):
            # Its OWN configured seed answered back: an OpenRouter id still has
            # to end `:free`, or the record's own filter empties the listing.
            return 200, json.dumps({"data": [{"id": m} for m in record.models]})
        return 200, OK_BODY
    return send


def run(answers, environ=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pc.main(environ=dict(ENV if environ is None else environ),
                     transport=router(answers), sleep=lambda seconds: 0)
    return rc, out.getvalue()


class DarkFamilyTests(unittest.TestCase):
    def test_a_dark_family_fails_the_run_and_names_the_action(self):
        rc, out = run({"groq": (401, "unauthorized")})
        self.assertEqual(1, rc, "a refused key must fail the canary")
        line = [row for row in out.splitlines() if "DARK groq" in row]
        self.assertTrue(line, "the row must say DARK: %r" % out)
        self.assertIn("GROQ_API_KEY", line[0],
                      "it has to name the secret to check -- got %r" % line[0])
        self.assertIn(lp.PROVIDERS["groq"].models[0], line[0],
                      "it has to name the id to refresh -- got %r" % line[0])

    def test_a_missing_model_is_dark_and_says_to_refresh_the_id(self):
        rc, out = run({"mistral": (404, "no such model")})
        self.assertEqual(1, rc)
        line = [row for row in out.splitlines() if "DARK mistral" in row][0]
        self.assertIn("model_not_found", line)
        self.assertIn(lp.PROVIDERS["mistral"].models[0], line)


class ExcusedStateTests(unittest.TestCase):
    def test_a_rate_limited_family_is_excused_and_does_not_fail_the_run(self):
        rc, out = run({"groq": (429, "slow down")})
        self.assertEqual(0, rc, "capacity is not a dark family")
        line = [row for row in out.splitlines() if "groq" in row][0]
        self.assertIn("excused", line)
        self.assertIn("quota", line)
        self.assertIn("clears by itself", line)

    def test_a_family_with_no_secret_is_excused_and_does_not_fail_the_run(self):
        env = dict(ENV)
        env.pop("OPENROUTER_API_KEY")
        rc, out = run({}, environ=env)
        self.assertEqual(0, rc, "an unset secret is a setting, not a fault")
        line = [row for row in out.splitlines() if "openrouter" in row][0]
        self.assertIn("excused", line)
        self.assertIn("not_configured", line)


class TheTableTests(unittest.TestCase):
    def test_every_family_appears_exactly_once_with_its_status(self):
        rc, out = run({})
        self.assertEqual(0, rc)
        for name in pc.FAMILIES:
            hits = [row for row in out.splitlines()
                    if row.startswith("  ") and (" %s " % name) in row]
            self.assertEqual(1, len(hits),
                             "%s must appear once -- output: %r" % (name, out))
        self.assertIn("canary: 5 families checked", out)


class TheWorkflowTests(unittest.TestCase):
    def test_the_canary_workflow_parses_and_runs_the_script_weekly(self):
        import yaml
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        trigger = doc.get("on") if "on" in doc else doc.get(True)
        self.assertEqual(["20 5 * * 1"],
                         [entry["cron"] for entry in trigger["schedule"]],
                         "weekly, on Monday")
        self.assertEqual({"contents": "read"}, doc["permissions"],
                         "the canary reads the repository and calls APIs")
        runs = " ".join(step.get("run", "")
                        for job in doc["jobs"].values()
                        for step in job["steps"])
        self.assertIn("scripts/provider_canary.py", runs)


if __name__ == "__main__":
    unittest.main()
