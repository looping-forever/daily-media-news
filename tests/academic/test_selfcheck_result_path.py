#!/usr/bin/env python3
"""tests/academic/test_selfcheck_result_path.py — the RESULT-path self-check.

WHAT IS BEING TESTED HERE
-------------------------
``scripts/selfcheck_exam_sources.py`` used to fetch only a notice LIST and
match titles with the exam_notice matchers. It never opened a detail page, never
downloaded a PDF, and skipped the ``critical: true`` RESULT feed entirely. So the
whole result path could be broken while the self-check printed a healthy summary
and exited 0 — which is exactly how it stayed green through the sem-1 burn (the
site-constant <title>, the Latin-only semester matcher, and the OCR branch that
never rasterised a page).

These tests pin the new probe on both sides, and the second side matters as much
as the first:

  * REACHABLE + HEALTHY  -> probed, exit 0.
  * REACHABLE + BROKEN   -> ::error:: and exit 1. NOT softened. Silence about a
                            broken result path is the failure this exists to
                            prevent, so "reduce the noise" is not an option.
  * UNREACHABLE          -> ::warning:: and exit 0. Self-disabling means
                            disabling its own VERDICT, never the product, and a
                            DNS outage at tu.edu.np must never become our
                            alert.

Everything is offline: a fake fetch, real IOST-shaped detail HTML from
``iost_detail_html``, and a real PDF from ``real_pdf_builder``. No network, no
fixtures directory, no writes outside a temp dir.

NO REAL RESULT CONTENT. The PDF is fabricated and only a roll SHAPE is ever
asserted — never a roll number, a name or a mark.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selfcheck_exam_sources as scs  # noqa: E402
from real_pdf_builder import build_one_page_pdf  # noqa: E402

CONFIG_PATH = os.path.join(REPO_ROOT, "config", "exam_notices.json")

RESULT_FEED = {
    "id": "iost_result",
    "label": "IOST result notices",
    "url": "https://iost.tu.edu.np/notices?notice_type=21",
    "notice_type": 21,
    "kind": "result",
    "require_marker": True,
    "critical": True,
}

KNOWN_CSIT_TITLE = "TU B.Sc.CSIT I Semester-2082 Exam Result"
# The title that must be UNREADABLE: nothing in either script's vocabulary.
UNREADABLE_TITLE = "शैक्षिक सत्र २०८१ को कार्ययोजना"
# A Nepali result title: this is the wording the Latin-only matcher used to
# resolve to None and burn.
NEPALI_RESULT_TITLE = "बी.एस.सी. चौथो सेमेस्टरको परीक्षा नतिजा प्रकाशित भएको छ"


def list_html(rows, next_url=None):
    parts = ["<!DOCTYPE html><html><head><title>Notices</title></head><body>"]
    for nid, title, date in rows:
        parts.append('<div class="notice-item"><span class="date">%s</span>'
                     '<a href="/notices/%d">%s</a></div>' % (date, nid, title))
    if next_url:
        parts.append('<a rel="next" href="%s">Next</a>' % next_url)
    parts.append("</body></html>")
    return "".join(parts)


def detail_html(real_title, pdf_url=None, site_constant=True):
    """A detail page in the REAL IOST shape: site-constant <title>, real title
    in the body. Degrading it is how the site-constant bug is injected."""
    head = ("<title>INSTITUTE OF SCIENCE AND TECHNOLOGY</title>"
            if site_constant else "<title>%s</title>" % real_title)
    body = ('<div class="inner-banner-detail"><h4 class="title">%s</h4></div>'
            '<div class="detail-page-inner"><h4 class="title">%s</h4>'
            '<div class="ck-table"><p>%s</p></div></div>' % (real_title,
                                                              real_title,
                                                              real_title))
    link = ('<a href="%s">Download</a>' % pdf_url) if pdf_url else ""
    return "<!DOCTYPE html><html><head>%s</head><body>%s%s</body></html>" % (
        head, body, link)


def cfg_with_result_path(**overrides):
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["result_path"] = dict(cfg.get("result_path") or {}, **overrides)
    return cfg


def run_selfcheck(fetch_fn, cfg, extra_argv=(), state=None, tmpdir=None):
    """Drive scs.main() with a fake fetch. Returns (rc, stderr, runs)."""
    own_dir = tmpdir is None
    tmpdir = tmpdir or tempfile.mkdtemp()
    try:
        state = state or os.path.join(tmpdir, "sc.json")
        err = io.StringIO()
        out = io.StringIO()
        argv = ["--config", CONFIG_PATH, "--state", state, "--no-ntfy",
                "--dry-run"] + list(extra_argv)
        with mock.patch.object(scs, "default_fetch_fn", fetch_fn), \
                mock.patch.object(scs, "load_exam_notice_config",
                                  return_value=cfg), \
                mock.patch("sys.stderr", err), \
                mock.patch("sys.stdout", out):
            rc = scs.main(argv)
        # GitHub reads workflow annotations from EITHER stream, and this script
        # already prints ::error:: to stdout (see the pre-existing stale-matcher
        # error) while the deferred/unreachable warnings go to stderr. Capture
        # both, so a test can assert on the annotation regardless of stream.
        return rc, out.getvalue() + err.getvalue(), scs.read_runs(state)
    finally:
        if own_dir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


def healthy_fetch(notices=((15700, "B.Sc. CSIT", "2026-09-28"),),
                  detail_title=KNOWN_CSIT_TITLE, pdf=True,
                  detail_site_constant=True, pdf_bytes=None):
    """A board that serves a list, a real detail page and (optionally) a real
    PDF. Every URL is answered, so the probe can run end to end."""
    rows = list(notices)
    board = list_html(rows)
    pdf_url = "https://portal.tu.edu.np/p/%d.pdf" % (rows[0][0],)
    detail = detail_html(detail_title, pdf_url if pdf else None,
                         site_constant=detail_site_constant)
    data = pdf_bytes if pdf_bytes is not None else build_one_page_pdf(
        roll="1357 2468")

    def fetch(url, timeout=15):
        if "notice_type=21" in url:
            return 200, board.encode()
        if "portal.tu.edu.np" in url:
            return 200, data
        if "/notices/" in url:
            return 200, detail.encode()
        if url.rstrip("/").endswith("/notices"):
            return 200, board.encode()
        raise RuntimeError("no fake response for %s" % url)

    return fetch


class ResultPathProbeUnitTest(unittest.TestCase):
    """check_result_path() directly, so each assertion is isolated."""

    def test_a_healthy_board_probes_clean(self):
        r = scs.check_result_path(RESULT_FEED, cfg_with_result_path(),
                                  healthy_fetch(), 15)
        self.assertTrue(r["live"])
        self.assertTrue(r["detail_ok"])
        self.assertTrue(r["probed"])
        self.assertEqual(r["failures"], [])
        self.assertEqual(r["classify"], "match")
        self.assertTrue(r["pdf_ok"])
        self.assertIs(r["roll_token_found"], True)

    def test_a_site_constant_title_is_caught_by_name(self):
        """THE sem-1 burn. If <title> is trusted again, the detail title is the
        site constant and every positive decision is made against the wrong
        string."""
        r = scs.check_result_path(
            RESULT_FEED, cfg_with_result_path(),
            healthy_fetch(detail_site_constant=False), 15)
        self.assertTrue(r["live"], "the board is reachable")
        # detail_html(site_constant=False) puts the real title in <title> too,
        # so the probe still succeeds - that is the healthy control. Now the
        # genuinely degraded case:
        def fetch(url, timeout=15):
            if "notice_type=21" in url:
                return 200, list_html(
                    [(15700, "B.Sc. CSIT", "2026-09-28")]).encode()
            return 200, ("<html><head><title>INSTITUTE OF SCIENCE AND "
                         "TECHNOLOGY</title></head><body></body></html>"
                         ).encode()

        r = scs.check_result_path(RESULT_FEED, cfg_with_result_path(), fetch,
                                  15)
        self.assertTrue(r["live"])
        self.assertFalse(r["probed"])
        self.assertTrue(any("not authoritative" in f for f in r["failures"]),
                        r["failures"])

    def test_an_unreadable_title_is_a_failure_not_a_pass(self):
        """A title we cannot read means a floor that never moves. The probe
        must say so."""
        r = scs.check_result_path(
            RESULT_FEED, cfg_with_result_path(),
            healthy_fetch(detail_title=UNREADABLE_TITLE), 15)
        self.assertTrue(r["live"])
        self.assertFalse(r["probed"])
        self.assertEqual(r["classify"], "unknown")
        self.assertTrue(any("UNREADABLE" in f for f in r["failures"]),
                        r["failures"])

    def test_a_nepali_result_title_is_readable(self):
        """The regression this whole unit exists for: the Nepali result title
        must NOT be unreadable, because unreadable meant burned."""
        r = scs.check_result_path(
            RESULT_FEED, cfg_with_result_path(),
            healthy_fetch(detail_title=NEPALI_RESULT_TITLE), 15)
        self.assertEqual(r["classify"], "match", r["failures"])
        self.assertEqual(r["semester"], 4)
        self.assertFalse(any("UNREADABLE" in f for f in r["failures"]))

    def test_a_pdf_with_no_text_is_a_failure(self):
        empty = b"%PDF-1.4 this file has no extractable text object at all"
        r = scs.check_result_path(
            RESULT_FEED, cfg_with_result_path(),
            healthy_fetch(pdf_bytes=empty), 15)
        self.assertTrue(r["live"])
        self.assertFalse(r["pdf_ok"])
        self.assertTrue(any("no text" in f for f in r["failures"]), r["failures"])

    def test_a_pdf_with_no_roll_shaped_token_is_a_failure(self):
        """Text, but unreadable as a result sheet: the false-failed guard's
        exact shape. A pass/fail could never be decided from it.

        Built with the real builder so the file is a genuine PDF whose text
        layer is prose — the module's own crude BT/Tj fallback then reads the
        words and nothing else, which is precisely the production shape.
        """
        no_roll = build_one_page_pdf(roll="not a roll at all",
                                     lines=["lorem ipsum dolor sit amet",
                                            "consectetur adipiscing elit"])
        r = scs.check_result_path(
            RESULT_FEED, cfg_with_result_path(),
            healthy_fetch(pdf_bytes=no_roll), 15)
        self.assertFalse(r["roll_token_found"])
        self.assertTrue(any("roll-shaped token" in f for f in r["failures"]),
                        r["failures"])

    def test_a_missing_pdf_link_is_not_by_itself_a_failure(self):
        """A notice with no PDF attached is normal on this board. The title
        checks are the ones that must hold; do not invent a failure."""
        r = scs.check_result_path(RESULT_FEED, cfg_with_result_path(),
                                  healthy_fetch(pdf=False), 15)
        self.assertTrue(r["detail_ok"])
        self.assertEqual(r["failures"], [])
        self.assertTrue(r["probed"])

    def test_an_unreachable_feed_is_live_false_and_raises_nothing(self):
        def dead(url, timeout=15):
            raise RuntimeError("SERVFAIL")

        r = scs.check_result_path(RESULT_FEED, cfg_with_result_path(), dead, 15)
        self.assertFalse(r["live"])
        self.assertFalse(r["probed"])
        self.assertIn("fetch failed", r["error"])
        self.assertEqual(r["failures"], [],
                         "an outage is not a pipeline fault")

    def test_it_never_raises_on_junk(self):
        """The self-check must always be able to record a run."""
        for junk in (b"", b"<html>nothing</html>", b"\x00\x01"):
            def fetch(url, timeout=15, _j=junk):
                return 200, _j

            r = scs.check_result_path(RESULT_FEED, cfg_with_result_path(),
                                      fetch, 15)
            self.assertIsInstance(r, dict)
            self.assertIn("failures", r)

    def test_only_a_roll_shape_is_ever_reported(self):
        """No real student data may escape the check."""
        r = scs.check_result_path(RESULT_FEED, cfg_with_result_path(),
                                  healthy_fetch(), 15)
        blob = json.dumps(r, ensure_ascii=False)
        self.assertNotIn("13572468", blob,
                         "the owner's real roll must never appear in a "
                         "self-check report")


class ResultPathExitCodeTest(unittest.TestCase):
    """The exit-code semantics, through main()."""

    def test_a_healthy_result_path_exits_zero(self):
        rc, err, runs = run_selfcheck(healthy_fetch(),
                                      cfg_with_result_path())
        self.assertEqual(rc, 0, err)
        self.assertTrue(runs)
        self.assertEqual(runs[-1]["verdict"], "ok")
        self.assertTrue(runs[-1]["result_path"]["probed"])
        self.assertNotIn("::error::result path", err)

    def test_an_unreadable_result_title_exits_one(self):
        rc, err, runs = run_selfcheck(
            healthy_fetch(detail_title=UNREADABLE_TITLE),
            cfg_with_result_path())
        self.assertEqual(rc, 1,
                         "a broken result path must fail loud, not warn")
        self.assertIn("::error::result path", err)
        self.assertIn("UNREADABLE", err)
        self.assertEqual(runs[-1]["verdict"], "result_path_broken")
        self.assertFalse(runs[-1]["ok"])

    def test_a_dead_board_exits_zero_and_warns(self):
        """Self-disable safely. A tu.edu.np DNS outage is not our alert."""
        def dead(url, timeout=15):
            raise RuntimeError("SERVFAIL")

        rc, err, runs = run_selfcheck(dead, cfg_with_result_path())
        self.assertEqual(rc, 0)
        self.assertIn("::warning::result feed", err)
        self.assertNotIn("::error::result path", err)
        self.assertEqual(runs[-1]["verdict"], "ok")
        self.assertIn("skipped", runs[-1]["result_path"])

    def test_the_run_log_is_written_even_on_a_failure(self):
        """The dead-man's switch depends on the log's recency; a failure must
        not skip the write, or it would hide itself."""
        rc, _err, runs = run_selfcheck(
            healthy_fetch(detail_title=UNREADABLE_TITLE),
            cfg_with_result_path())
        self.assertEqual(rc, 1)
        self.assertTrue(runs)
        self.assertIn("result_path", runs[-1])

    def test_a_config_with_no_result_path_skips_the_probe(self):
        """Opt-in by declaration. A deployment that has not declared a result
        path must not be told it has a broken one."""
        cfg = cfg_with_result_path()
        cfg.pop("result_path")
        rc, err, _runs = run_selfcheck(healthy_fetch(), cfg)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("::error::result path", err)

    def test_result_path_can_be_disabled_explicitly(self):
        rc, err, _runs = run_selfcheck(
            healthy_fetch(detail_title=UNREADABLE_TITLE),
            cfg_with_result_path(), extra_argv=["--no-result-path"])
        self.assertEqual(rc, 0, err)
        self.assertIn("DISABLED by --no-result-path", err)

    def test_the_probe_is_on_by_default_in_production(self):
        """The workflow passes no --no-result-path, so the probe MUST run: a
        self-check that can be silently off by default is how this failed the
        first time."""
        import argparse
        parser = argparse.ArgumentParser()
        # The real parser is built inside main(); assert on the flag's
        # presence and default through a live main() call instead.
        rc, err, runs = run_selfcheck(
            healthy_fetch(detail_title=UNREADABLE_TITLE),
            cfg_with_result_path())
        self.assertEqual(rc, 1,
                         "with no flag the probe must run and must fail loud")
        self.assertTrue(runs[-1]["result_path"]["live"])

    def test_a_config_without_a_critical_result_feed_warns(self):
        cfg = cfg_with_result_path()
        for feed in cfg["feeds"]:
            feed["critical"] = False
        rc, err, _runs = run_selfcheck(healthy_fetch(), cfg)
        self.assertEqual(rc, 0, err)
        self.assertIn("no feed is marked kind=result + critical", err)

    def test_the_shipped_config_declares_a_result_path(self):
        """config/exam_notices.json must carry the block, or the probe never
        runs in CI."""
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertTrue(cfg.get("result_path"),
                        "the shipped config has no result_path block, so the "
                        "result path is never self-checked in CI")
        self.assertTrue(cfg["result_path"].get("enabled", True))
        self.assertTrue(cfg["result_path"].get("require_detail_page", True))
        self.assertTrue(cfg["result_path"].get("require_pdf", True))
        result_feeds = [f for f in cfg["feeds"]
                        if f.get("kind") == "result" and f.get("critical")]
        self.assertTrue(result_feeds,
                        "no critical result feed for the probe to check")
        self.assertIn("1357 2468", cfg["result_path"]
                      ["expected_sheet_roll_token"],
                      "the expected token is a SHAPE, so the split form is "
                      "what the real sheet prints")


class StudentDataHygieneTest(unittest.TestCase):
    """The landmine, named: a result sheet is a table of OTHER students."""

    def test_no_real_result_page_is_committed(self):
        """Structural, not content-based: the repo must not carry a real TU
        result PDF or a page image of one. pdf_shape_*.json metadata is fine."""
        forbidden = (".pdf",)
        offenders = []
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "node_modules", "__pycache__",
                                    ".wrangler", "docs")]
            for name in files:
                lowered = name.lower()
                if lowered.endswith(forbidden) and "workflow" not in root:
                    offenders.append(os.path.join(root, name))
        self.assertEqual(offenders, [],
                         "a PDF in the repo could be a real result sheet "
                         "containing other students' names, rolls and marks")

    def test_the_real_roll_never_appears_in_a_shipped_artifact(self):
        """The roll is a private identifier. It belongs in no committed file
        outside state/ (which is not walked here) and no source file."""
        offenders = []
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "node_modules", "__pycache__",
                                    ".wrangler", "state", "docs")]
            for name in files:
                if not name.endswith((".py", ".json", ".yml", ".md", ".js")):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        if "13572468" in fh.read():
                            offenders.append(path)
                except OSError:
                    continue
        # tests/ uses the roll as a match target; README.md documents it in
        # prose for the owner. Both are legitimate. The rule under test is that
        # no sheet CONTENT and no committed result PDF exists anywhere.
        allowed_prefixes = ("tests/",)
        # README.md documents the roll for the owner, and
        # check_academic_result.py's roll-regex comments cite the split/unsplit
        # SHAPE it has to match. Both are prose about a shape, not data.
        allowed_exact = ("README.md", "scripts/check_academic_result.py")
        for path in offenders:
            rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
            self.assertTrue(rel.startswith(allowed_prefixes)
                            or rel in allowed_exact,
                            "%s carries the tracked roll, which is neither a "
                            "test fixture nor prose about its shape" % rel)


if __name__ == "__main__":
    unittest.main()
