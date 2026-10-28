#!/usr/bin/env python3
"""No placeholder institution may reach a BUILT digest.

The bank and the college are deployment values now (scripts/private_values.py),
not literals. The failure this guards is quiet: with the variables unset the
renderer prints a stand-in, the page looks finished, and nobody finds out until
the owner reads a section headed "MY BANK -- EXAMPLE BANK".

There are TWO classes of stand-in and they are not equally bad, so they are
checked separately:

  * a placeholder HOST (`bank.example.com`) is the worse one. It is rendered as a
    link to a site that has never existed, and the notice collector makes a real
    HTTP request to it. An unset URL must therefore produce NOTHING.
  * a placeholder NAME is a label. It is wrong, but it is visible, it links
    nowhere, and the PRIVATE-CONFIG line names the variable that fixes it.

The assertion is made on the BUILT output, not on the constant: stories are
rendered through render_sections() and the page HTML is searched. The detector is
then given a page that DOES carry a placeholder host and is required to find it
(test_a_configured_placeholder_host_is_actually_detected) -- the mutation proof,
so a search that could never match fails here instead of passing both ways.
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

#: Every stand-in the module used to fall back to.
PLACEHOLDER_HOSTS = ("bank.example.com", "college.example.edu")
PLACEHOLDER_NAMES = ("EXAMPLE BANK", "Example College")
PLACEHOLDERS = PLACEHOLDER_HOSTS + PLACEHOLDER_NAMES

CONFIGURED_ENV_NAMES = ("BANK_NAME", "COLLEGE_NAME", "BANK_URL", "COLLEGE_URL")

# Runs in a FRESH interpreter: the modules read the environment at import, so a
# value set mid-process would not reach them and the test would prove nothing.
_PROBE = r'''
import json, os, sys
sys.path.insert(0, %(scripts)r)
import render_digest as rd
import build_email as be
import collect_news as cn
import private_values as pv

# Each story links to the deployment's own notice page when there is one, so a
# configured host genuinely reaches the rendered HTML and the detector can be
# proved able to see it.
def story(cat, url):
    return {"category": cat, "title": "Sentinel story for " + cat,
            "summary": "Sentinel summary.", "source_name": "Sentinel Source",
            "source_url": url or "https://news.sentinel-private.test/a"}

grouped = {
    "bank_local": [story("bank_local", pv.bank_url())],
    "college_local": [story("college_local", pv.college_url())],
}
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

    def test_an_unset_url_leaves_no_placeholder_host_anywhere(self):
        """The requirement, on the built output rather than on the constant."""
        out, _err = probe({})
        text = flat(out)
        for host in PLACEHOLDER_HOSTS:
            self.assertNotIn(
                host, text,
                "an unset BANK_URL/COLLEGE_URL still put the placeholder host %r "
                "into a built digest. That host is a link to a site that has "
                "never existed, and the notice collector FETCHES it." % host)

    def test_an_unset_url_is_dropped_rather_than_fetched(self):
        """No empty entry, and no duplicate plan name.

        The collector's plan name is `notice:<host>`, so two unset URLs would
        both become `notice:` -- and the collector rejects duplicate plan names,
        which would take the whole collection run down in any deployment that
        had not configured them.
        """
        out, _err = probe({})
        sites = out["notice_sites"]
        self.assertNotIn("", sites,
                         "an unset URL reached NOTICE_SITES as an empty string; "
                         "it would be fetched and named `notice:`")
        self.assertEqual(len(sites), len(set(sites)),
                         "NOTICE_SITES carries a duplicate URL")
        for url in sites:
            self.assertTrue(url.startswith("http"),
                            "NOTICE_SITES carries a non-URL entry: %r" % url)
        self.assertEqual(
            ["https://tu.edu.np/", "https://iost.tu.edu.np/", "https://nea.org.np/"],
            sites,
            "the three public notice sources must survive an unset deployment")

    def test_an_unset_host_is_not_a_tier1_domain(self):
        out, _err = probe({})
        self.assertNotIn("", out["tier1"],
                         "TIER1_DOMAINS carries an empty host; `_domain_matches` "
                         "answers True for any domain ending in '.' against it")

    def test_a_configured_placeholder_host_is_actually_detected(self):
        """THE MUTATION PROOF for the host check.

        Configure a URL whose host IS `bank.example.com`, render, and require the
        detector to find it. Without this, the "no placeholder host" test above
        could pass against a search that can never match anything.
        """
        out, _err = probe({"BANK_URL": "https://bank.example.com/notices"})
        self.assertIn(
            "bank.example.com", flat(out),
            "the placeholder-host detector cannot see a host that is literally "
            "configured and linked from a rendered card, so the test above "
            "proves nothing")

    def test_unset_values_are_announced_as_not_configured_by_name(self):
        out, err = probe({})
        for name in CONFIGURED_ENV_NAMES:
            self.assertIn(
                "PRIVATE-CONFIG: " + name + " is not configured", err,
                "an unset deployment value must be announced on stderr, naming "
                "the variable: %s" % name)
        # The announcement is about the NAME, never the value.
        for host in PLACEHOLDER_HOSTS:
            self.assertNotIn(host, err,
                             "the warning line printed a placeholder host")

    def test_the_announcement_is_emitted_once_per_name(self):
        out, err = probe({})
        for name in CONFIGURED_ENV_NAMES:
            self.assertEqual(
                1, err.count("PRIVATE-CONFIG: " + name),
                "%s was announced more than once, so the line will be scrolled "
                "away in a CI log" % name)


if __name__ == "__main__":
    unittest.main()
