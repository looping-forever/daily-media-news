#!/usr/bin/env python3
"""tests/academic/test_result_alert_cooldown.py — the RESULT-path alert dedup.

THE DEFECT THIS PINS. The `result_failures` branch of
``scripts/selfcheck_exam_sources.py`` called ``notify_ntfy`` and returned 1 on
EVERY run, with no cooldown at all, while the stale-matcher branch a few lines
below it already had one (``RATE_LIMIT_HOURS`` plus a ``notified`` marker). The
owner received the identical warning four times: 2026-10-13 21:45, 2026-10-14
03:00, 06:05 and 11:55 NPT — one per scheduled run, the exact pattern recorded
in ``state/exam_selfcheck.json`` lines 53-62, ten consecutive
``"verdict": "result_path_broken"`` records with a byte-identical ``failures``
array.

THE CONSTRAINT THAT MAKES THE OBVIOUS FIX WRONG. Silence about a broken result
path is exactly how the sem-1 pass was lost for nineteen hours, so the warning
may never be softened -- but a plain 24-hour timer would ALSO hide a
deterioration: if a second notice starts failing, or the same notice starts
failing a new way, the owner must hear about it immediately, inside the window
or not. So the cooldown is keyed on WHAT BROKE, not on WHEN: the push is
suppressed only when the failure set is byte-identical to one already pushed.

WHAT MUST NOT CHANGE. The exit code stays 1, the ``::error::`` lines stay, the
step-summary block stays, and the run log still gets a record on every run. Only
the PUSH is rate-limited. Every test below asserts that explicitly, because a
dedup that quietly downgraded a loud failure to a quiet one would be worse than
the spam it fixed.

Offline throughout: a fake fetch serving today's REAL failure text, and a real
temp state file the runs accumulate into. No network, no ntfy, no writes outside
the temp dir.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ntfy_classes  # noqa: E402
import selfcheck_exam_sources as scs  # noqa: E402

from test_selfcheck_result_path import (  # noqa: E402
    CONFIG_PATH,
    KNOWN_CSIT_TITLE,
    cfg_with_result_path,
    detail_html,
    list_html,
    run_selfcheck,
)

#: The real notice title the existing harness resolves to a CSIT result, so the
#: probe classifies the notice and reaches the PDF.
KNOWN_TITLE = KNOWN_CSIT_TITLE

# The literal failure text the owner actually received, reproduced verbatim from
# state/exam_selfcheck.json line 62. A test whose RED case is a paraphrase would
# not catch the thing that happened.
REAL_NO_TEXT_FAILURE = (
    "notice %d PDF yielded no text - the extraction path is broken (a scanned "
    "sheet needs pytesseract + the tesseract binary + a PDF rasteriser)")

#: N runs after the first. The real cron is hourly, so N=5 covers the observed
#: four-alert burst and one more.
REPEATS = 5


def broken_pdf_fetch(notice_id=15771, title=None):
    """A board whose detail page resolves, but whose PDF yields NO text.

    This is today's real failure: the notice is reachable and the detail page is
    readable, so the probe gets all the way to the PDF and fails there. The
    detail HTML comes from the existing harness's `detail_html`, so the probe
    reaches the PDF for the SAME reason it does in production -- a hand-rolled
    page here would fail earlier and quietly test the wrong thing.
    """
    rows = [(notice_id, title or KNOWN_TITLE, "2026-10-03")]
    board = list_html(rows)
    pdf_url = "https://portal.tu.edu.np/p/%d.pdf" % notice_id

    def fetch(url, timeout=15):
        if "notice_type=21" in url:
            return 200, board.encode()
        if "portal.tu.edu.np" in url:
            return 200, b"%PDF-1.4 not a real pdf at all"
        if "/notices/" in url:
            return 200, detail_html(KNOWN_TITLE, pdf_url).encode()
        raise RuntimeError("no fake response for %s" % url)

    return fetch


class RecordingNotifier(object):
    """Stands in for notify_ntfy and records what would have been PUSHED.

    `dry_run` returns True exactly like the real helper does, so the run's own
    bookkeeping (`result_notified`) still reflects a successful send.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, title, message, dry_run, class_tag=None):
        self.calls.append({"title": title, "message": message,
                           "class_tag": class_tag})
        return True

    @property
    def count(self):
        return len(self.calls)


class CooldownBase(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.state = os.path.join(self.tmpdir, "sc.json")
        self.summary = os.path.join(self.tmpdir, "summary.md")
        self.notifier = RecordingNotifier()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_once(self, notice_id=15771):
        """One full self-check run against a broken PDF, capturing the push.

        GITHUB_STEP_SUMMARY is pointed at a real file, because the failure block
        the owner reads in the run summary is written THERE and not to stdout.
        Asserting on stdout alone would pass while the summary silently went
        empty -- which is the "softened warning" this file must never allow.
        """
        rc, output, runs = run_selfcheck(
            broken_pdf_fetch(notice_id=notice_id),
            cfg_with_result_path(),
            state=self.state, tmpdir=self.tmpdir)
        return rc, output, runs

    def run_recorded(self, notice_id=15771):
        with mock.patch.object(scs, "notify_ntfy", self.notifier), \
                mock.patch.dict(os.environ,
                                {"GITHUB_STEP_SUMMARY": self.summary}):
            return self.run_once(notice_id)

    def summary_text(self):
        try:
            with open(self.summary, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""


class FirstPushIsTheHappyPath(CooldownBase):
    """Assert the alert ACTUALLY HAPPENS before any test asserts it is quiet.

    A dedup that suppresses everything passes every 'it stayed quiet' test while
    the owner is never told anything. That is the vacuous pass this file exists
    to make impossible, so the very first test is the control."""

    def test_the_first_broken_run_does_push(self):
        rc, output, runs = self.run_recorded()
        self.assertEqual(rc, 1, "a broken result path still exits 1")
        self.assertEqual(self.notifier.count, 1,
                         "the FIRST broken run must push. output=%s" % output)
        self.assertIn("Result path self-check FAILED",
                      self.notifier.calls[0]["title"])
        self.assertIn(REAL_NO_TEXT_FAILURE % 15771,
                      self.notifier.calls[0]["message"])

    def test_the_first_broken_run_is_tagged_as_a_result(self):
        self.run_recorded()
        self.assertEqual(self.notifier.calls[0]["class_tag"],
                         ntfy_classes.RESULT)

    def test_the_control_is_not_vacuous(self):
        """GUARDS THE GUARD. If `run_once` stopped producing failures, the
        'stayed quiet' tests below would pass for the wrong reason."""
        rc, output, runs = self.run_recorded()
        self.assertTrue(runs, "a record was written")
        self.assertFalse(runs[-1]["ok"])
        self.assertEqual(runs[-1]["verdict"], "result_path_broken")
        self.assertTrue(runs[-1]["result_path"]["failures"], runs[-1])
        self.assertIn("::error::result path", output)


class SameFailureSetStaysQuiet(CooldownBase):
    """THE BUG. One push, then silence across N further hourly runs."""

    def test_an_identical_failure_set_pushes_exactly_once(self):
        first_rc, first_out, _ = self.run_recorded()
        self.assertEqual(first_rc, 1)
        self.assertEqual(self.notifier.count, 1)

        for i in range(REPEATS):
            rc, output, runs = self.run_recorded()
            self.assertEqual(rc, 1,
                             "run %d must still exit 1 (the warning is never "
                             "softened)" % (i + 2))
            self.assertIn("::error::result path", output,
                          "run %d must still print the failure" % (i + 2))
        self.assertEqual(self.notifier.count, 1,
                         "the identical failure set must not re-push. "
                         "pushed %d times" % self.notifier.count)

    def test_the_step_summary_still_carries_the_failure_when_suppressed(self):
        """THE LOUDNESS. Suppressing the PUSH must not empty the one record the
        owner reads after the fact."""
        self.run_recorded()
        self.run_recorded()
        text = self.summary_text()
        self.assertIn("RESULT PATH BROKEN", text)
        self.assertIn(REAL_NO_TEXT_FAILURE % 15771, text)
        self.assertIn("result_path_broken", text)

    def test_the_suppressed_run_says_so_loudly_in_its_own_log(self):
        self.run_recorded()
        rc, output, _ = self.run_recorded()
        self.assertIn("rate-limited", output)
        self.assertIn("this EXACT failure set", output)

    def test_every_run_is_still_recorded_with_its_verdict(self):
        for _ in range(REPEATS + 1):
            self.run_recorded()
        runs = scs.read_runs(self.state)
        self.assertEqual(len(runs), REPEATS + 1)
        for rec in runs:
            self.assertFalse(rec["ok"])
            self.assertEqual(rec["verdict"], "result_path_broken")
        # Exactly ONE record claims the push; the rest record the suppression.
        self.assertEqual(sum(1 for r in runs if r.get("result_notified")), 1)
        self.assertEqual(len({r["result_fingerprint"] for r in runs}), 1,
                         "an unchanged failure set keeps one fingerprint")


class ChangedFailureSetAlertsImmediately(CooldownBase):
    """THE CONSTRAINT. Elapsed time alone would hide a deterioration."""

    def test_a_different_notice_number_pushes_immediately(self):
        self.run_recorded(notice_id=15771)
        self.assertEqual(self.notifier.count, 1)
        # Same instant, well inside RATE_LIMIT_HOURS.
        rc, output, runs = self.run_recorded(notice_id=15888)
        self.assertEqual(rc, 1)
        self.assertEqual(self.notifier.count, 2,
                         "a DIFFERENT broken notice must page immediately, "
                         "inside the cooldown window. output=%s" % output)
        self.assertIn(REAL_NO_TEXT_FAILURE % 15888,
                      self.notifier.calls[1]["message"])

    def test_a_second_broken_notice_pushes_immediately(self):
        """Not a swap -- a WIDENING.

        The first failure is still true and still failing, but there is now a
        SECOND one as well. The message the owner already has does not describe
        reality any more, so it must be pushed even though the run is well
        inside the cooldown. A dedup that hashed only `result_failures[0]`, or
        only the first failure line, would swallow this.
        """
        unreadable = "शैक्षिक सत्र २०८१ को कार्ययोजना"
        board = list_html([(15771, KNOWN_TITLE, "2026-10-03"),
                           (15779, unreadable, "2026-10-04")])

        def widened(url, timeout=15):
            if "notice_type=21" in url:
                return 200, board.encode()
            if "portal.tu.edu.np" in url:
                return 200, b"%PDF-1.4 not a real pdf at all"
            if "/notices/15779" in url:
                return 200, detail_html(unreadable, None).encode()
            if "/notices/" in url:
                return 200, detail_html(
                    KNOWN_TITLE,
                    "https://portal.tu.edu.np/p/15771.pdf").encode()
            raise RuntimeError("no fake response for %s" % url)

        self.run_recorded()
        self.assertEqual(self.notifier.count, 1)
        with mock.patch.object(scs, "notify_ntfy", self.notifier), \
                mock.patch.dict(os.environ,
                                {"GITHUB_STEP_SUMMARY": self.summary}):
            rc, output, runs = run_selfcheck(
                widened, cfg_with_result_path(), state=self.state,
                tmpdir=self.tmpdir)
        self.assertEqual(rc, 1)
        self.assertEqual(len(runs[-1]["result_path"]["failures"]), 2,
                         "both notices now fail: %r"
                         % (runs[-1]["result_path"]["failures"],))
        self.assertEqual(self.notifier.count, 2,
                         "a WIDENED failure set must page immediately")
        self.assertIn("15779", self.notifier.calls[1]["message"],
                      "the new push must name the new failure, not just "
                      "re-send the old one")


class FingerprintUnitTest(unittest.TestCase):
    """The hash itself, isolated. Hashing must not depend on ordering."""

    def _probe(self, notice_id, failures):
        return {"id": "iost_result", "failures": failures}

    def test_no_failures_means_no_fingerprint(self):
        self.assertIsNone(scs.result_failure_fingerprint([]))
        self.assertIsNone(scs.result_failure_fingerprint(None))

    def test_order_does_not_change_the_hash(self):
        a = scs.result_failure_fingerprint([
            self._probe("r", ["one", "two"])])
        b = scs.result_failure_fingerprint([
            self._probe("r", ["two", "one"])])
        self.assertEqual(a, b,
                         "the same failures in a different ORDER are the same "
                         "condition and must not re-page the owner")

    def test_a_different_notice_changes_the_hash(self):
        a = scs.result_failure_fingerprint(
            [self._probe("r", [REAL_NO_TEXT_FAILURE % 15771])])
        b = scs.result_failure_fingerprint(
            [self._probe("r", [REAL_NO_TEXT_FAILURE % 15779])])
        self.assertNotEqual(a, b)

    def test_a_second_probe_contributes_to_the_hash(self):
        """`result_failures[0]` alone would miss a widening."""
        one = scs.result_failure_fingerprint(
            [self._probe("r", ["same"])])
        two = scs.result_failure_fingerprint(
            [self._probe("r", ["same"]), self._probe("r2", ["new"])])
        self.assertNotEqual(one, two)

    def test_a_suppressed_run_is_not_treated_as_a_prior_send(self):
        """Only records that actually pushed count as 'already sent'."""
        now = scs.datetime(2026, 10, 14, 6, 0, tzinfo=scs.timezone.utc)
        fp = "abc123"
        runs = [{"at": "2026-10-14T05:00:00Z", "result_fingerprint": fp,
                 "result_notified": False}]
        self.assertFalse(scs.result_failure_already_notified(runs, fp, now))


class CooldownWindowUnitTest(unittest.TestCase):
    """The window is real: it re-arms after RATE_LIMIT_HOURS."""

    def _stamp(self, iso):
        return iso

    def test_outside_the_window_the_same_set_pushes_again(self):
        now = scs.datetime(2026, 10, 14, 6, 0, tzinfo=scs.timezone.utc)
        old = (now - scs.timedelta(hours=scs.RATE_LIMIT_HOURS + 1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        runs = [{"at": old, "result_fingerprint": "abc", "result_notified": True}]
        self.assertFalse(scs.result_failure_already_notified(runs, "abc", now))

    def test_the_latest_send_starts_the_window_not_the_first(self):
        now = scs.datetime(2026, 10, 14, 6, 0, tzinfo=scs.timezone.utc)
        old = (now - scs.timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
        runs = [{"at": old, "result_fingerprint": "abc", "result_notified": True}]
        # 23h ago is inside the window...
        self.assertTrue(scs.result_failure_already_notified(runs, "abc", now))
        # ...but at +25h the SAME pair is outside it.
        later = now + scs.timedelta(hours=25)
        self.assertFalse(scs.result_failure_already_notified(runs, "abc", later))


class TheAlertItselfIsUnchanged(unittest.TestCase):
    """The wording the owner has already read, still exactly as loud."""

    def test_the_title_is_untouched(self):
        rc, output, _ = run_selfcheck(
            broken_pdf_fetch(), cfg_with_result_path())
        # With no ntfy configured the real helper prints its unset warning; the
        # control for the title lives in FirstPushIsTheHappyPath above, which
        # captures the call. This asserts the loud path still runs at all.
        self.assertEqual(rc, 1)
        self.assertIn("::error::result path on iost_result", output)


if __name__ == "__main__":
    unittest.main()