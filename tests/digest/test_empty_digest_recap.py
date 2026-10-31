#!/usr/bin/env python3
"""tests/digest/test_empty_digest_recap.py -- the recap path for a zero-story digest.

THE OWNER RULE BEING ENFORCED
-----------------------------
    An evening digest with zero new stories must ship a clearly labelled recap
    of the most important items from the last 24 hours, marked as recap, NOT
    new. Never a bare "empty" push at normal priority.

WHY THIS IS A TEST AND NOT A COMMENT
-------------------------------------
Run 38145451218 (2026-10-11 evening) shipped exactly the forbidden thing. With
`stories == []` the publisher produced

    title    = "<emoji> Evening Digest - 2026-10-11 - 0 items"
    priority = 3      <- NORMAL priority
    message  = ... + "No items cleared filtering today."

`priority = 3` came from the literal `4 if high else 3` below it, and `high` is
False on an empty digest, so the empty case silently inherited the ordinary
priority. A phone notification that says "0 items" at the ordinary volume is
indistinguishable from a normal digest until the owner opens it.

THE TWO SHAPES THAT MUST EXIST
------------------------------
  recap material exists -> labelled recap, ordinary priority, title says RECAP
  no recap material     -> DEGRADED, priority 5, never a confident empty digest

WHAT THE RECAP MUST NOT DO
--------------------------
It must not become `stories`. Feeding recap rows back through the pipeline
would re-consume history via update_history AND mark a zero-story slot
`delivered: true`, which arms FALLBACK_NOOP and suppresses the empty-digest
retry ladder. These tests assert the recap is presentation-only.
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "render"))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts"))
from fixtures import WORKFLOW, write_json  # noqa: E402
import empty_state  # noqa: E402

MAX_BYTES = 4096

START = "- name: Publish to ntfy (primary)"
END = "- name: Send digest via Gmail (backup)"


def ntfy_source(path=None):
    """The ntfy step's python heredoc, dedented and ready to exec.

    `path` exists so the RED demonstration can exec the PRE-fix heredoc from a
    throwaway copy of the workflow and get the same failures these tests pin.
    """
    with io.open(path or WORKFLOW, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index(START)
    end = src.index(END)
    chunk = src[start:end]
    m = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", chunk, re.S)
    assert m, "ntfy heredoc not found in the workflow"
    return "\n".join(ln[10:] if ln.startswith(" " * 10) else ln
                     for ln in m.group(1).split("\n"))


def run_ntfy(tmpdir, payload, history=None, kind="evening",
             today="2026-10-11", workflow=None):
    """Exec the real ntfy heredoc in DRY RUN; return (payload, stdout).

    DRY_RUN=true returns before urlopen and urlopen is poisoned, so this can
    never make a network call.
    """
    os.makedirs(os.path.join(tmpdir, "state"), exist_ok=True)
    write_json(os.path.join(tmpdir, "state", "stories.validated.json"), payload)
    if history is not None:
        write_json(os.path.join(tmpdir, "state", "history.json"), history)

    def _no_network(*_a, **_k):
        raise AssertionError("ntfy test attempted a network call")

    env_backup = dict(os.environ)
    os.environ.update({
        "GITHUB_OUTPUT": os.path.join(tmpdir, "gh_output.txt"),
        "GITHUB_STEP_SUMMARY": os.path.join(tmpdir, "gh_summary.md"),
        "NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.invalid",
        "DRY_RUN": "true", "TODAY": today, "DIGEST_TIME": kind,
    })
    real = urllib.request.urlopen
    urllib.request.urlopen = _no_network
    cwd = os.getcwd()
    buf = io.StringIO()
    try:
        os.chdir(tmpdir)
        with contextlib.redirect_stdout(buf):
            exec(compile(ntfy_source(workflow), "daily-news.yml:ntfy", "exec"),
                 {"__name__": "ntfy"})
    finally:
        urllib.request.urlopen = real
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(env_backup)
    text = buf.getvalue()
    assert "DRY RUN" in text, "ntfy heredoc did not take the DRY RUN path:\n%s" % text[:400]
    marker = "Payload preview:"
    got, _end = json.JSONDecoder().raw_decode(
        text[text.index(marker) + len(marker):].lstrip())
    return got, text


def empty_payload(**extra):
    p = {"stories": [], "data_cutoff_npt": "2026-10-11 19:46",
         "generated_at_npt": "2026-10-11 19:49"}
    p.update(extra)
    return p


def hist(rows):
    return {"stories": rows}


def row(title, category, day):
    return {"title": title, "category": category, "date_sent": day,
            "url_normalized": "example.invalid/" + re.sub(r"\W+", "-", title).lower()}


TODAY_ROWS = [
    row("Budhigandaki flash flood sweeps away houses in Gorkha", "nepal", "2026-10-11"),
    row("Tekken 8 adds a Baki the Grappler crossover", "gaming", "2026-10-11"),
    row("Python 3.15 ships with explicit lazy imports", "tech_career", "2026-10-11"),
]
OLD_ROW = row("An item from last week", "nepal", "2026-10-04")


class TestZeroDigestRecap(unittest.TestCase):
    """The owner's rule, asserted against the REAL publisher heredoc."""

    # -- the recap shape ----------------------------------------------------
    def test_zero_stories_ships_a_labelled_recap_not_an_empty_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(), history=hist(TODAY_ROWS))
        self.assertIn("RECAP", got["title"],
                      "a zero-story digest must title its push as a recap: %r"
                      % got["title"])
        self.assertNotIn("0 items", got["title"],
                         "the recap title must count the recap items, not zero")
        self.assertIn("not new", got["message"].lower())
        self.assertNotIn("No items cleared filtering today.", got["message"])
        self.assertEqual(3, got["priority"],
                         "a recap carries real content, so it keeps ordinary "
                         "priority; it is the BARE empty push that was banned")

    def test_the_recap_carries_the_items_and_says_which_window_it_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(), history=hist(TODAY_ROWS))
        for r in TODAY_ROWS:
            self.assertIn(r["title"], got["message"])
        self.assertIn("already pushed today", got["message"])
        self.assertIn("arrows_counterclockwise", got["tags"])

    def test_recap_falls_back_to_yesterday_and_labels_it_as_yesterday(self):
        rows = [row("Yesterday item one", "nepal", "2026-10-10"),
                row("Yesterday item two", "gaming", "2026-10-10")]
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(), history=hist(rows))
        self.assertIn("already pushed yesterday", got["message"])
        self.assertNotIn("already pushed today", got["message"],
                         "a yesterday recap must never claim to be 24h fresh")

    def test_a_stale_item_is_never_recapped_as_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(),
                              history=hist([OLD_ROW, TODAY_ROWS[0]]))
        self.assertNotIn(OLD_ROW["title"], got["message"])

    # -- the degraded shape -------------------------------------------------
    def test_no_recap_material_pushes_degraded_at_priority_five(self):
        """THE BAN, stated as a test.

        Zero stories AND no recap material must never render as a confident
        ordinary-priority empty digest.
        """
        for label, payload_hist in (
                ("no history file at all", None),
                ("a history file with nothing recent", hist([OLD_ROW]))):
            with self.subTest(label):
                with tempfile.TemporaryDirectory() as tmp:
                    got, _ = run_ntfy(tmp, empty_payload(), history=payload_hist)
                self.assertEqual(5, got["priority"],
                                 "%s: an empty push with nothing to recap "
                                 "must raise priority, not sit at 3" % label)
                self.assertIn("degraded", got["title"])
                self.assertIn("DEGRADED", got["message"])
                self.assertNotIn("No items cleared filtering today.",
                                 got["message"])

    # -- the recap is presentation only ------------------------------------
    def test_the_recap_does_not_become_stories(self):
        """A recap must not consume history or mark the slot delivered."""
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(), history=hist(TODAY_ROWS))
            with open(os.path.join(tmp, "state", "stories.validated.json"),
                      encoding="utf-8") as fh:
                after = json.load(fh)
        self.assertEqual([], after["stories"],
                         "the recap must not be written back as stories")
        payload_file = os.path.join(tmp, "state", "ntfy_payload.json")
        self.assertTrue(got["priority"] in (3, 5))

    def test_the_recap_never_rewrites_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = hist(TODAY_ROWS)
            run_ntfy(tmp, empty_payload(), history=before)
            with open(os.path.join(tmp, "state", "history.json"),
                      encoding="utf-8") as fh:
                after = json.load(fh)
        self.assertEqual(before, after,
                         "the publisher must never mutate history.json")

    # -- the non-empty path must be untouched ------------------------------
    def test_a_real_digest_is_unchanged_by_the_recap_path(self):
        # priority 8 is the `high` threshold, so this digest escalates to
        # priority 4 and gains a title clause - the richest normal-path shape
        # the recap code must leave completely alone.
        story = {"title": "A brand new and important story", "category": "nepal",
                 "priority": 8, "summary": "body", "url": "https://x.invalid/a",
                 "published_at": "2026-10-11", "source": "x.invalid",
                 "domain_category": "nepal", "language": "en"}
        payload = {"stories": [story], "data_cutoff_npt": "2026-10-11 19:46",
                   "generated_at_npt": "2026-10-11 19:49"}
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, payload, history=hist(TODAY_ROWS))
        self.assertNotIn("RECAP", got["title"])
        self.assertIn("1 item", got["title"])
        self.assertNotIn("RECAP", got["message"])
        self.assertEqual(4, got["priority"])

    # -- the byte budget still holds ---------------------------------------
    def test_the_recap_push_stays_inside_the_ntfy_byte_budget(self):
        many = [row("A very long headline number %d about something in Nepal "
                    "that runs on and on and on" % i, "nepal", "2026-10-11")
                for i in range(40)]
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(), history=hist(many))
        self.assertLessEqual(len(got["message"].encode("utf-8")), MAX_BYTES)
        self.assertIn("RECAP", got["title"])


class TestZeroDigestNamesItsCause(unittest.TestCase):
    """WHY the edition is empty is part of the message (2026-10-30).

    THE INCIDENT. `validate_stories.py` rejected three REAL stories -- all on a
    `spoiler_reason` schema check -- and the push said "nothing cleared
    filtering", which is what it says on a slow news day. The owner read a calm
    day where the pipeline had eaten the news.

    The payload now carries `empty_kind`, frozen by the validator, and only a
    `quiet_day` may still use the friendly sentence. `broken` and
    `validator_rejected_all` name the break instead.

    THE DUPLICATION THESE TESTS PIN. The publisher is a heredoc sliced out of
    the YAML and exec'd with no repo on sys.path, so it CANNOT import
    scripts/empty_state.py and carries its own copy of the sentences. Asserting
    the heredoc's real output against `empty_state.DEFECT_TEXTS` is what stops
    the two copies drifting apart silently.
    """

    def test_validator_rejected_says_the_pipeline_rejected_every_story(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(
                tmp, empty_payload(empty_kind="validator_rejected_all"),
                history=hist(TODAY_ROWS))
        self.assertIn(empty_state.DEFECT_TEXTS["validator_rejected_all"],
                      got["message"],
                      "the push must name the pipeline break, not a filter")
        self.assertNotIn("cleared filtering", got["message"],
                         "nothing CLEARED filtering here -- everything was "
                         "rejected after collection: %r" % got["message"])
        self.assertIn("RECAP", got["title"])

    def test_broken_names_the_outage_and_never_the_quiet_day_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(empty_kind="broken"),
                              history=hist(TODAY_ROWS))
        self.assertIn(empty_state.DEFECT_TEXTS["broken"], got["message"])
        self.assertNotIn("cleared filtering", got["message"])
        self.assertNotIn("quiet news day, not a failure", got["message"])

    def test_quiet_day_still_says_nothing_cleared_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(empty_kind="quiet_day"),
                              history=hist(TODAY_ROWS))
        self.assertIn("Nothing cleared filtering for this slot", got["message"])
        self.assertNotIn("No story survived", got["message"],
                         "a genuinely quiet day must not read as a defect")

    def test_a_payload_without_empty_kind_keeps_the_old_wording(self):
        """Backwards compatibility: payloads written before `empty_kind`."""
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = run_ntfy(tmp, empty_payload(), history=hist(TODAY_ROWS))
        self.assertIn("Nothing cleared filtering for this slot", got["message"])
        self.assertNotIn("No story survived", got["message"])


if __name__ == "__main__":
    unittest.main()
