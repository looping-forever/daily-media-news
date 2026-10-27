#!/usr/bin/env python3
"""No placeholder institution may reach a BUILT digest.

The bank and the college are deployment values now (scripts/private_values.py),
not literals. The failure this guards is quiet: with the variables unset the
renderer prints the old placeholder, the page looks finished, and nobody finds
out until the owner reads a section headed "MY BANK -- EXAMPLE BANK".

So the assertion is made on the BUILT HTML, not on the constant: a story is
rendered through render_sections() and the page text is searched for the
placeholder. The same helper is then run with the variables UNSET and is
required to FIND the placeholder -- that second run is the mutation proof, so a
helper that had been quietly broken (or a search that could never match) fails
here instead of passing both ways.
"""

import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")

#: Deliberately recognisable and NOT the placeholders.
SENTINEL = {
    "BANK_NAME": "Sentinel Bank Limited",
    "COLLEGE_NAME": "Sentinel Campus / CAMPUS",
    "BANK_URL": "https://bank.sentinel-private.test/notices",
    "COLLEGE_URL": "https://campus.sentinel-private.test/notices",
}
PLACEHOLDERS = (
    "bank.example.com",
    "college.example.edu",
    "EXAMPLE BANK",
    "Example College",
)

# Runs in a FRESH interpreter: the modules read the environment at import, so a
# value set mid-process would not reach them and the test would prove nothing.
_PROBE = r'''
import json, os, sys
sys.path.insert(0, %(scripts)r)
import render_digest as rd
import build_email as be
import collect_news as cn
import private_values as pv

def story(cat):
    return {"category": cat, "title": "Sentinel story for " + cat,
            "summary": "Sentinel summary.", "source_name": "Sentinel Source",
            "source_url": "https://news.sentinel-private.test/a"}

grouped = {cat: [story(cat)] for cat in ("bank_local", "college_local")}
html = rd.render_sections(grouped, None)

print(json.dumps({
    "html": html,
    "sections": [h for _k, h, _c in rd.SECTIONS],
    "email_sections": [h for _k, h, _c in be.SECTIONS],
    "notice_sites": [u for u, _c in cn.NOTICE_SITES],
    "tier1": list(__import__("breaking_monitor").TIER1_DOMAINS),
    "section_labels": sorted(rd.SECTION_TO_LABEL),
    "labels_for_configured": [rd.section_label(pv.bank_name()),
                              rd.section_label(pv.college_name())],
}))
'''


def probe(env_extra):
    """Run the probe in a fresh interpreter with the placeholders UNSET."""
    env = dict(os.environ)
    for name in ("BANK_NAME", "COLLEGE_NAME", "BANK_URL", "COLLEGE_URL"):
        env.pop(name, None)
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE % {"scripts": SCRIPTS}],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise AssertionError("probe failed: %s\n%s"
                             % (proc.returncode, proc.stderr[-2000:]))
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


def flat(probe_out):
    return "\n".join([
        probe_out["html"],
        "\n".join(probe_out["sections"]),
        "\n".join(probe_out["email_sections"]),
        "\n".join(probe_out["notice_sites"]),
        "\n".join(probe_out["tier1"]),
        "\n".join(probe_out["section_labels"]),
    ])


class TestNoPlaceholderInABuiltDigest(unittest.TestCase):

    def test_configured_values_replace_every_placeholder(self):
        out, _err = probe(SENTINEL)
        text = flat(out)
        for placeholder in PLACEHOLDERS:
            self.assertNotIn(
                placeholder, text,
                "a placeholder institution survived into the built digest: %r\n"
                "the renderer is printing a literal instead of reading "
                "scripts/private_values.py" % placeholder)
        # And the configured value really is what gets printed, so "absent"
        # above cannot be satisfied by printing nothing at all.
        for value in (SENTINEL["BANK_NAME"], SENTINEL["COLLEGE_NAME"],
                      SENTINEL["BANK_URL"], SENTINEL["COLLEGE_URL"]):
            self.assertIn(value, text)

    def test_configured_headers_still_map_to_their_teaser_labels(self):
        # The ntfy teaser keys off the header text. A configured header that
        # stopped mapping would silently lose that section from the push.
        out, _err = probe(SENTINEL)
        self.assertEqual(["bank", "college"], out["labels_for_configured"])

    def test_mutation_the_same_check_finds_the_placeholder_when_unset(self):
        # THE MUTATION PROOF. With nothing configured the placeholder MUST be
        # present; if it is not, the check above is not looking at real output.
        out, err = probe({})
        text = flat(out)
        self.assertIn("bank.example.com", text)
        self.assertIn("college.example.edu", text)
        # ... and the absence of configuration is shouted about, by NAME.
        for name in ("BANK_NAME", "COLLEGE_URL"):
            self.assertIn(name, err,
                          "an unset deployment value must be named on stderr")

    def test_placeholders_are_only_the_last_resort(self):
        out, err = probe({})
        self.assertIn("PRIVATE-CONFIG:", err)
        self.assertIn("placeholder", err)


if __name__ == "__main__":
    unittest.main()
