#!/usr/bin/env python3
"""tests/academic/test_exam_notices.py — exam-notice coverage (F1–F5).

`notice_type=21` is the ONLY notice_type code in the repo and it is a RESULT
feed. SEMESTER_RE's result terms mean routines and form deadlines are rejected
outright even when filed under 21, so those notices rest entirely on Google
News plus a homepage anchor-scrape. This is the single largest coverage gap in
the product, and these tests pin the fix:

  F1  require_marker=False on a host-scoped feed is a BOOST, not a gate.
  F2  a deadline-bearing test that reuses the ONE date extractor.
  F3  the result-path title matcher is order-independent, and the ordered
      version is gone.
  F4  exam notices ride the EXISTING emit_notification -> ntfy path, carry no
      pass/fail verdict, and dedup via the examined map.
  F5  the self-check fails loud, disables only its own verdict, and can never
      delay or prevent a result check.

Run:
    py -m unittest discover -s tests/academic -t . -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import check_academic_result as car  # noqa: E402
import render_academic as rad  # noqa: E402
import selfcheck_exam_sources as scs  # noqa: E402

CONFIG_PATH = os.path.join(REPO_ROOT, "config", "exam_notices.json")
RESULT_FEED = "https://iost.tu.edu.np/notices?notice_type=21"
ALL_FEED = "https://iost.tu.edu.np/notices"


def seed_state():
    return {
        "schema_version": 2,
        "roll_number": "13572468",
        "current_semester": 4,
        "last_examined_notice_id": 0,
        "examined": {},
        "pending_since": {},
        "hosts_down": {},
        "pending_overflow": False,
        "last_poll_time": None,
        "semesters": {
            "1": {"status": "passed", "result_announced": True,
                  "roll_found": True, "result_date": "2025-08-15",
                  "notice_id": None, "pdf_url": None},
            "2": {"status": "passed", "result_announced": True,
                  "roll_found": True, "result_date": "2025-12-20",
                  "notice_id": None, "pdf_url": None},
            "3": {"status": "passed", "result_announced": True,
                  "roll_found": True, "result_date": "2026-05-10",
                  "notice_id": None, "pdf_url": None},
            "4": {"status": "pending", "result_announced": False,
                  "roll_found": False, "result_date": None,
                  "notice_id": None, "pdf_url": None},
        },
    }


def list_html(rows):
    parts = ["<!DOCTYPE html><html><head><title>Notices</title></head><body>"]
    for nid, title, date in rows:
        parts.append('<div class="notice-item"><span>%s</span>'
                     '<a href="/notices/%d">%s</a></div>' % (date, nid, title))
    parts.append("</body></html>")
    return "".join(parts)


def make_fetch(responses):
    def fetch_fn(url, timeout=15):
        for key, (status, body) in responses.items():
            if key in url:
                return status, body
        raise RuntimeError("no fake response for %s" % url)
    return fetch_fn


class ConfigShapeTest(unittest.TestCase):
    """The config ships with the HONEST starting state, not a fabricated one."""

    def setUp(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            self.cfg = json.load(fh)

    def test_ships_unverified(self):
        """verified_against_live starts false. A config that claims live
        verification it never had is worse than no config."""
        self.assertIs(self.cfg["verified_against_live"], False)
        self.assertIsNone(self.cfg["verified_at"])
        self.assertTrue(self.cfg["enabled"])

    def test_notice_type_21_is_the_only_code(self):
        """We must never invent a notice_type value. 21 is the only verified
        one; iost_all passes no code at all."""
        codes = [f["notice_type"] for f in self.cfg["feeds"]]
        self.assertEqual(codes, [21, None])

    def test_no_ioe_hosts(self):
        """IOE/IOE sites are B.E. engineering, not CSIT. Injecting them would
        put another faculty's notices in the owner's exam section."""
        blob = json.dumps(self.cfg).lower()
        for banned in ("ioe.tu.edu.np", "exam.ioe", "ioe.edu.np"):
            self.assertNotIn(banned, blob)

    def test_matcher_vocabularies_present(self):
        matchers = self.cfg["matchers"]
        for term in ("exam", "routine", "form", "admission card", "result"):
            self.assertIn(term, matchers["notice_terms"])
        for term in ("deadline", "last date", "on or before"):
            self.assertIn(term, matchers["deadline_terms"])
        for term in ("CSIT", "B.Sc", "BSc"):
            self.assertIn(term, matchers["marker_terms"])


class F1MarkerIsBoostNotGateTest(unittest.TestCase):
    def setUp(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            self.cfg = json.load(fh)
        self.matchers = self.cfg["matchers"]
        self.all_feed = [f for f in self.cfg["feeds"]
                         if f["kind"] == "exam_notice"][0]

    def test_iost_all_does_not_require_a_marker(self):
        self.assertFalse(self.all_feed["require_marker"],
                         "F1: gating a CSIT-only feed on a CSIT token drops "
                         "exactly the notice the owner cannot miss")

    def test_routine_2082_without_a_csit_token_still_qualifies(self):
        """The single most important case in this file."""
        title = "Exam Routine 2082 published"
        self.assertNotIn("CSIT", title)
        self.assertNotIn("B.Sc", title)
        self.assertTrue(car.match_exam_notice(title, self.matchers,
                                              self.all_feed["require_marker"]))

    def test_marker_required_feed_still_gates(self):
        """A TU-wide feed WOULD gate on a marker; prove the knob works."""
        self.assertFalse(car.match_exam_notice("Exam Routine 2082 published",
                                               self.matchers, True))
        self.assertTrue(car.match_exam_notice("B.Sc.CSIT Exam Routine 2082",
                                              self.matchers, True))


class F2DeadlineBearingTest(unittest.TestCase):
    def setUp(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            self.matchers = json.load(fh)["matchers"]

    def _ok(self, title):
        return car.match_exam_notice(title, self.matchers, False)

    def test_deadline_term_qualifies(self):
        self.assertTrue(self._ok("Exam form submission — last date 2026-10-05"))

    def test_on_or_before_qualifies(self):
        self.assertTrue(self._ok("Registration open on or before 2082-10-01"))

    def test_date_shaped_token_qualifies(self):
        self.assertTrue(self._ok("Notice published 2026-10-01 about exams"))

    def test_academic_year_qualifies(self):
        self.assertTrue(self._ok("Exam Routine 2082"))

    def test_schedule_bearing_without_any_date_qualifies(self):
        """Documented widening: F2's literal test would drop this."""
        self.assertTrue(self._ok("4th semester examination schedule published"))

    def test_notice_term_alone_is_not_enough(self):
        self.assertFalse(self._ok("Result processing is under way"))

    def test_nothing_exam_related_never_qualifies(self):
        self.assertFalse(self._ok("Library extended opening hours"))
        self.assertFalse(self._ok(""))
        self.assertFalse(self._ok("Sports meet concluded"))

    def test_reuses_the_single_date_extractor(self):
        """F2: no second date parser. The qualifier must go through the same
        _extract_date the result path uses."""
        self.assertIs(car.match_exam_notice.__module__, "check_academic_result")
        self.assertTrue(car._extract_date("published 2026-10-01"))


class F3OrderIndependentResultMatcherTest(unittest.TestCase):
    def test_marker_then_sem_then_result(self):
        self.assertEqual(
            car.match_semester("TU B.Sc.CSIT IV Semester-2081 Exam Result"), 4)

    def test_result_before_marker_and_semester(self):
        self.assertEqual(
            car.match_semester("Result of the B.Sc.CSIT 4th Semester"), 4)

    def test_semester_before_result_before_marker(self):
        self.assertEqual(
            car.match_semester("IV Semester Result - TU B.Sc.CSIT"), 4)

    def test_ordered_regex_is_gone(self):
        """The old regex required CSIT … sem … Semester … Result in that order.
        That ordering constraint is what made it an under-matcher."""
        self.assertNotIn("Semester|Sem|Sem\\.", car.SEMESTER_RE.pattern)
        self.assertIn("(?=", car.SEMESTER_RE.pattern)

    def test_still_rejects_non_results(self):
        for title in ("Admission open for B.Sc.CSIT",
                      "TU exam routine published",
                      "B.Sc.CSIT IV Semester form fill",
                      ""):
            self.assertIsNone(car.match_semester(title), title)

    def test_worker_prefilter_is_widened_too(self):
        self.assertTrue(car.looks_like_result("Result of 4th Semester"))
        self.assertTrue(car.looks_like_result("Back Result"))
        self.assertTrue(car.looks_like_result("Supplementary exam result"))
        self.assertFalse(car.looks_like_result("Exam Routine 2082"))
        self.assertFalse(car.looks_like_result(""))


class F4DeliveryTest(unittest.TestCase):
    """Exam notices ride the EXISTING emit_notification -> ntfy path."""

    def _cfg(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _run(self, rows, out_dir, preview, state_path=None):
        """Run the monitor over BOTH feeds. state_path is reused when supplied
        so cross-run behaviour (dedup) can be asserted."""
        cfg = self._cfg()
        if state_path is None:
            state_path = os.path.join(out_dir, "state.json")
            car.save_state(state_path, seed_state())
        fetch = make_fetch({
            RESULT_FEED: (200, list_html([]).encode()),
            ALL_FEED: (200, list_html(rows).encode()),
        })
        with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
            rc = car.run(state_path, RESULT_FEED, dry_run=True,
                         fetch_fn=fetch, notices_config=cfg)
        return rc, car.load_state(state_path)

    def test_exam_notice_emits_a_payload_on_the_existing_path(self):
        with tempfile.TemporaryDirectory() as d:
            preview = os.path.join(d, "preview.json")
            rc, state = self._run(
                [(16001, "Exam Routine 2082 published", "2026-09-27")],
                d, preview)
            self.assertEqual(rc, 0)
            with open(preview, encoding="utf-8") as fh:
                payloads = json.load(fh)
            self.assertEqual(len(payloads), 1)
            payload = payloads[0]
            self.assertEqual(payload["kind"], "exam_deadline")
            self.assertEqual(payload["ntfy"]["priority"], 4)
            self.assertTrue(payload["ntfy"]["title"].startswith(
                "\U0001F393 CSIT exam notice: "))
            self.assertIn("exam-notice-sem", payload["website"]["path"])
            # No new delivery channel: the ntfy block is the only transport.
            self.assertIn("topic", payload["ntfy"])

    def test_no_pass_fail_verdict_in_the_payload_or_the_page(self):
        with tempfile.TemporaryDirectory() as d:
            preview = os.path.join(d, "preview.json")
            rc, state = self._run(
                [(16002, "B.Sc.CSIT 4th semester examination schedule",
                  "2026-09-27")], d, preview)
            self.assertEqual(rc, 0)
            with open(preview, encoding="utf-8") as fh:
                payload = json.load(fh)[0]
            self.assertNotIn("passed", payload)
            self.assertNotIn("failed", payload)
            self.assertNotIn("status", payload)
            page_dir = os.path.join(d, "pages")
            out = rad.render_payload(payload, page_dir)
            self.assertTrue(os.path.basename(out).endswith(
                "exam-notice-sem4.html"))
            with open(out, encoding="utf-8") as fh:
                page = fh.read()
            for banned in ("PASSED", "FAILED", "Roll Number"):
                self.assertNotIn(banned, page)

    def test_dedup_via_deadline_notified(self):
        with tempfile.TemporaryDirectory() as d:
            preview = os.path.join(d, "preview.json")
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            self._run([(16003, "Exam Routine 2082 published", "2026-09-27")],
                      d, preview, state_path=state_path)
            with open(preview, encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)), 1)
            rc, state = self._run(
                [(16003, "Exam Routine 2082 published", "2026-09-27")],
                d, preview, state_path=state_path)
            self.assertEqual(rc, 0)
            with open(preview, encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)), 1,
                                 "an exam notice must notify exactly once")
            entry = state["exam_notice_seen:iost_all"]["16003"]
            self.assertEqual(entry["reason"], "deadline_notified")

    def test_unrelated_notice_never_notifies(self):
        with tempfile.TemporaryDirectory() as d:
            preview = os.path.join(d, "preview.json")
            rc, state = self._run(
                [(16004, "Library extended opening hours", "2026-09-27")],
                d, preview)
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(preview))

    def test_degraded_index_title_never_burns_an_exam_notice(self):
        with tempfile.TemporaryDirectory() as d:
            preview = os.path.join(d, "preview.json")
            cfg = self._cfg()
            state_path = os.path.join(d, "state.json")
            car.save_state(state_path, seed_state())
            blank = ('<html><body><div><a href="/notices/16005"></a></div>'
                     '</body></html>')
            fetch = make_fetch({RESULT_FEED: (200, b"<html></html>"),
                                ALL_FEED: (200, blank.encode())})
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc = car.run(state_path, RESULT_FEED, dry_run=True,
                             fetch_fn=fetch, notices_config=cfg)
            self.assertEqual(rc, 0)
            state = car.load_state(state_path)
            entry = state["exam_notice_seen:iost_all"]["16005"]
            self.assertEqual(entry["outcome"], "deferred")
            self.assertEqual(state["exam_notice_seen:iost_all"]["__floor__"], 0)

    def test_result_page_output_is_unchanged_by_the_kind_branch(self):
        """The `result` branch must be BYTE-IDENTICAL to before."""
        payload = {
            "kind": "academic_result", "semester": 4,
            "roll_number": "13572468", "passed": True, "status": "passed",
            "result_date": "2026-09-27", "notice_id": 1,
            "pdf_url": "https://portal.tu.edu.np/notice/9300/x.pdf",
            "notice_url": "https://iost.tu.edu.np/notices/1", "title": "T",
        }
        with tempfile.TemporaryDirectory() as a, \
                tempfile.TemporaryDirectory() as b:
            first = rad.render_payload(payload, a)
            with open(first, encoding="utf-8") as fh:
                text = fh.read()
            # no `kind` at all -> the default result path
            payload.pop("kind")
            second = rad.render_payload(payload, b)
            with open(second, encoding="utf-8") as fh:
                self.assertEqual(text, fh.read())
            self.assertTrue(os.path.basename(first).endswith("-sem4.html"))


class F5SelfCheckTest(unittest.TestCase):
    def setUp(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            self.cfg = json.load(fh)

    def _run_selfcheck(self, args, responses):
        # --no-result-path: these fakes serve notice LIST pages only. The
        # result-path probe (added 2026-09-29) fetches a DETAIL page and a PDF,
        # which a list-only fake cannot satisfy, so without this flag every
        # exam_notice test below would fail on a probe that is irrelevant to
        # what it is testing. The probe's own tests pass a real detail page and
        # a real PDF, and live with the probe ENABLED.
        argv = ["--config", CONFIG_PATH, "--state", args[0], "--no-ntfy",
                "--dry-run", "--no-result-path"] + list(args[1:])
        with mock.patch.object(scs, "default_fetch_fn", responses), \
             mock.patch.object(scs, "load_exam_notice_config",
                               return_value=self.cfg):
            rc = scs.main(argv)
        return rc

    def test_dead_feed_is_a_warning_and_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "sc.json")
            def dead(url, timeout=15):
                raise RuntimeError("SERVFAIL")
            rc = self._run_selfcheck([state], dead)
            self.assertEqual(rc, 0)
            runs = scs.read_runs(state)
            self.assertTrue(runs, "the marker is written even on failure")
            self.assertFalse(any(f["live"] for f in runs[-1]["feeds"]))
            # the CONFIG is never written by the self-check
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                self.assertIs(json.load(fh)["verified_against_live"], False)

    def test_zero_matcher_hits_is_the_loud_failure(self):
        """A FULL page with zero matches is a real stale-matcher signal. The
        minimum-sample guard (P3b) must not swallow it."""
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "sc.json")
            noise = list_html([
                (17001 + i, "Notice number %d" % i, "2026-09-27")
                for i in range(scs.MIN_SAMPLE + 2)])
            def fetch(url, timeout=15):
                return 200, noise.encode()
            rc = self._run_selfcheck([state], fetch)
            self.assertEqual(rc, 1, "a stale matcher set must fail loud")
            runs = scs.read_runs(state)
            self.assertEqual(runs[-1]["verdict"], "stale")
            self.assertIn("iost_all", runs[-1]["stale_feeds"])

    def test_matching_feed_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "sc.json")
            good = list_html([
                (17004, "Exam Routine 2082 published", "2026-09-27"),
                (17005, "Admission card available", "2026-09-27"),
            ])
            def fetch(url, timeout=15):
                return 200, good.encode()
            rc = self._run_selfcheck([state], fetch)
            self.assertEqual(rc, 0)
            self.assertEqual(scs.read_runs(state)[-1]["verdict"], "ok")

    def test_selfcheck_never_touches_academic_state(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "sc.json")
            def dead(url, timeout=15):
                raise RuntimeError("down")
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
                self._run_selfcheck([state], dead)
            # the monitor's state file is not in this temp dir at all
            self.assertFalse(os.path.exists(os.path.join(d, "academic_state.json")))

    def test_run_log_survives_a_corrupt_line(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "sc.json")
            with open(state, "w", encoding="utf-8") as fh:
                fh.write('{"at": "2026-09-26T00:00:00Z", "ok": true}\n')
                fh.write("this is not json\n")
                fh.write('{"at": "2026-09-27T00:00:00Z", "ok": true}\n')
            runs = scs.read_runs(state)
            self.assertEqual(len(runs), 2)

    def test_append_run_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            unwritable = os.path.join(d, "nope", "\0bad", "x.json")
            self.assertFalse(scs.append_run(unwritable, {"at": "x"}))


class P2MutualExclusionTest(unittest.TestCase):
    """P2b. iost_all overlaps the result feed and "result" is in
    ``notice_terms``, so a result notice carrying a date-shaped token used to
    emit BOTH an exam_deadline alert and an academic_result alert. Two ntfy
    pushes for one notice, and the pointer alert invites the owner to read a
    notice that the result pass is about to adjudicate."""

    def _cfg(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _run_both(self, d, result_rows, all_rows, pdf_text="Roll 13572468 passed"):
        cfg = self._cfg()
        state_path = os.path.join(d, "state.json")
        preview = os.path.join(d, "preview.json")
        car.save_state(state_path, seed_state())
        detail = ('<html><head><title>TU B.Sc.CSIT IV Semester-2081 Result'
                  '</title></head><body>'
                  '<a href="https://portal.tu.edu.np/n/9300/x.pdf">R</a>'
                  '</body></html>')
        responses = {RESULT_FEED: (200, list_html(result_rows).encode()),
                     ALL_FEED: (200, list_html(all_rows).encode())}
        for nid, _t, _dt in result_rows:
            responses["/notices/%d" % nid] = (200, detail.encode())
        responses["portal.tu.edu.np"] = (200, b"%PDF-1.4 fake")
        with mock.patch.object(car, "extract_pdf_text",
                               return_value=pdf_text), \
             mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
            rc = car.run(state_path, RESULT_FEED, dry_run=True,
                         fetch_fn=make_fetch(responses), notices_config=cfg)
        payloads = []
        if os.path.exists(preview):
            with open(preview, encoding="utf-8") as fh:
                payloads = json.load(fh)
        return rc, payloads, car.load_state(state_path)

    def test_a_result_notice_never_also_fires_an_exam_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, state = self._run_both(
                d,
                [(16010, "B.Sc.CSIT IV Semester Result 2026-09-27",
                  "2026-09-27")],
                [(16010, "B.Sc.CSIT IV Semester Result 2026-09-27",
                  "2026-09-27")])
        self.assertEqual(rc, 0)
        kinds = [p["kind"] for p in payloads]
        self.assertEqual(kinds.count("academic_result"), 1,
                         "the result pass must own the notice")
        self.assertEqual(kinds.count("exam_deadline"), 0,
                         "P2b: one notice must not alert twice - the exam "
                         "pass emitted a pointer for a RESULT notice: %r" % kinds)
        self.assertEqual(len(payloads), 1)
        entry = state["exam_notice_seen:iost_all"]["16010"]
        self.assertEqual(entry["reason"], "wrong_poll_set")

    def test_a_genuine_exam_notice_still_alerts_on_the_exam_pass(self):
        with tempfile.TemporaryDirectory() as d:
            rc, payloads, _state = self._run_both(
                d, [], [(16011, "Exam Routine 2082 published", "2026-09-27")])
        self.assertEqual(rc, 0)
        self.assertEqual([p["kind"] for p in payloads], ["exam_deadline"])

    def test_is_result_notice_tracks_the_result_stage1_test(self):
        """It must fire on the stage-1 test alone (marker + semester token +
        result term, any order), so a result notice is never pointer-alerted
        even when the semester token cannot be resolved to a number."""
        title = "Result notice for B.Sc.CSIT 4th semester"
        self.assertTrue(car._is_result_notice(title))
        self.assertTrue(car.match_semester(title))
        self.assertFalse(car._is_result_notice("Exam Routine 2082"))
        self.assertFalse(car._is_result_notice("Admission open"))
        self.assertFalse(car._is_result_notice(""))
        # A title the result matcher does not claim can still be pointer-alerted
        # without double-notifying, because the result pass will not process it.
        self.assertFalse(car._is_result_notice("B.Sc.CSIT Result notice"))
        self.assertIsNone(car.match_semester("B.Sc.CSIT Result notice"))


class P3MinimumSampleTest(unittest.TestCase):
    """P3b. A real run reported `exam_notice matchers matched 0/3 notices on
    iost_all`. Three parsed notices is far below what a real IOST page renders,
    so that is weak evidence of a stale matcher set - it is more likely the page
    shape changed. The self-check now says INSUFFICIENT SAMPLE instead of
    claiming staleness, and prints the titles it saw. It does NOT stop judging
    a full page."""

    def setUp(self):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            self.cfg = json.load(fh)

    def _run(self, state_path, rows):
        html = list_html(rows)
        # --no-result-path: this fake serves notice LIST pages only, and these
        # tests are about the exam_notice minimum-sample guard, not the
        # result-path probe. See F5SelfCheckTest._run_selfcheck.
        with mock.patch.object(scs, "default_fetch_fn",
                               lambda url, timeout=15: (200, html.encode())), \
             mock.patch.object(scs, "load_exam_notice_config",
                               return_value=self.cfg):
            rc = scs.main(["--config", CONFIG_PATH, "--state", state_path,
                             "--no-ntfy", "--dry-run", "--no-result-path"])
        # read INSIDE the temp dir lifetime
        return rc, scs.read_runs(state_path)

    def test_tiny_sample_is_insufficient_not_stale(self):
        with tempfile.TemporaryDirectory() as d:
            rc, runs = self._run(os.path.join(d, "sc.json"), [
                (18001, "Library extended opening hours", "2026-09-27"),
                (18002, "Annual sports day", "2026-09-27"),
                (18003, "Seminar on research", "2026-09-27"),
            ])
        self.assertEqual(rc, 0, "a 3-notice sample must not fail the step")
        self.assertTrue(runs)
        self.assertEqual(runs[-1]["verdict"], "ok")
        self.assertEqual(runs[-1]["stale_feeds"], [])

    def test_full_page_with_no_match_is_still_a_loud_failure(self):
        """The guard must not silence a genuinely stale matcher set."""
        with tempfile.TemporaryDirectory() as d:
            rows = [(18010 + i, "Notice number %d" % i, "2026-09-27")
                    for i in range(scs.MIN_SAMPLE + 3)]
            rc, runs = self._run(os.path.join(d, "sc.json"), rows)
        self.assertEqual(rc, 1,
                         "a full page with zero matches is a real stale-matcher "
                         "signal and must still fail loud")
        self.assertEqual(runs[-1]["verdict"], "stale")
        self.assertIn("iost_all", runs[-1]["stale_feeds"])

    def test_matching_page_is_ok_regardless_of_sample_size(self):
        with tempfile.TemporaryDirectory() as d:
            rc, runs = self._run(os.path.join(d, "sc.json"), [
                (18020, "Exam Routine 2082 published", "2026-09-27")])
        self.assertEqual(rc, 0)
        self.assertEqual(runs[-1]["verdict"], "ok")

    def test_empty_page_cannot_claim_staleness(self):
        with tempfile.TemporaryDirectory() as d:
            rc, runs = self._run(os.path.join(d, "sc.json"), [])
        self.assertEqual(rc, 0)
        self.assertEqual(runs[-1]["stale_feeds"], [])

    def test_min_sample_floor_is_explicit(self):
        self.assertEqual(scs.MIN_SAMPLE, 5)


class P3WatchdogFirstRunTest(unittest.TestCase):
    """P3a. The watchdog arm asserted schema_version == 2 and 'a self-check must
    exist' as HARD problems, against a repo whose tracked state is still schema
    1 and which has no state/exam_selfcheck.json yet. Two guaranteed false
    alarms on the first run. Deferred notices were a third permanent-noise
    source.

    These tests EXECUTE the python embedded in the workflow step against a
    fabricated state directory, so they are behavioural rather than a grep of
    the YAML.
    """

    WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows",
                            "digest-watchdog.yml")

    @classmethod
    def setUpClass(cls):
        import re
        with open(cls.WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("      - name: Check academic monitor health")
        chunk = text[start:]
        chunk = chunk[chunk.index("python3 - <<'PY'"):]
        body = chunk.split("<<'PY'", 1)[1]
        # The terminator is the INDENTED line "          PY", so a literal
        # "\nPY\n" split runs past it and swallows the step's shell half.
        kept = []
        for line in body.split("\n"):
            if line.strip() == "PY":
                break
            kept.append(line)
        cls.script = "\n".join(
            line[10:] if line.startswith(" " * 10) else line
            for line in kept)
        assert "raise SystemExit(1)" in cls.script, "extraction failed"
        assert "gh issue create" not in cls.script, \
            "extraction leaked into the step's shell half"

    def _check(self, state, selfcheck_lines=None):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "state"))
            if state is not None:
                with open(os.path.join(d, "state", "academic_state.json"),
                          "w", encoding="utf-8") as fh:
                    json.dump(state, fh)
            if selfcheck_lines is not None:
                with open(os.path.join(d, "state", "exam_selfcheck.json"),
                          "w", encoding="utf-8") as fh:
                    fh.write(selfcheck_lines)
            out = os.path.join(d, "gh_output")
            summ = os.path.join(d, "gh_summary")
            open(out, "w").close()
            env = dict(os.environ, GITHUB_OUTPUT=out, GITHUB_STEP_SUMMARY=summ)
            proc = subprocess.run([sys.executable, "-c", self.script],
                                  cwd=d, env=env, capture_output=True,
                                  text=True, timeout=60)
            problems = 0
            with open(out, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("problems="):
                        problems = int(line.split("=", 1)[1])
            return proc.returncode, problems, proc.stdout, \
                (open(summ, encoding="utf-8").read()
                 if os.path.exists(summ) else "")

    def _today(self):
        import datetime
        return datetime.date.today().isoformat()

    def test_first_run_against_the_real_tracked_state_does_not_false_alarm(self):
        """schema 1 + a fresh last_poll + no self-check record = HEALTHY."""
        rc, problems, out, summary = self._check({
            "schema_version": 1, "roll_number": "13572468",
            "current_semester": 4, "last_checked_notice_id": 15236,
            "last_examined_notice_id": 15236,
            "last_poll_time": self._today(),
            "examined": {}, "pending_since": {}, "hosts_down": {},
            "semesters": {"1": {"status": "failed", "result_announced": True},
                          "4": {"status": "pending"}},
        }, selfcheck_lines=None)
        self.assertEqual(problems, 0,
                         "a pending schema-1 migration and a not-yet-written "
                         "self-check log must NOT be hard problems on day one "
                         "(output was: %s)" % out[-800:])
        self.assertEqual(rc, 0)
        self.assertIn("pending, not a failure", summary)
        self.assertIn("no exam-source self-check on record yet", summary)

    def test_deferred_notices_are_informational_never_hard(self):
        rc, problems, out, summary = self._check({
            "schema_version": 2, "roll_number": "13572468",
            "current_semester": 4, "last_examined_notice_id": 15236,
            "last_poll_time": self._today(),
            "examined": {"15240": {"outcome": "deferred",
                                   "reason": "detail_fetch_failed",
                                   "at": "2026-09-27"}},
            "pending_since": {"15240": "2026-09-27"}, "hosts_down": {},
            "semesters": {"4": {"status": "pending"}},
        })
        self.assertEqual(problems, 0,
                         "a deferred notice is the no-burn invariant WORKING; "
                         "alarming on it would be permanently noisy")
        self.assertEqual(rc, 0)
        self.assertIn("deferred and awaiting retry", summary)

    def test_a_stale_poll_is_still_a_hard_problem(self):
        rc, problems, out, summary = self._check({
            "schema_version": 2, "roll_number": "13572468",
            "current_semester": 4, "last_examined_notice_id": 15236,
            "last_poll_time": "2026-09-01",
            "examined": {}, "pending_since": {}, "hosts_down": {},
            "semesters": {"4": {"status": "pending"}},
        })
        self.assertEqual(problems, 1)
        self.assertEqual(rc, 1)
        self.assertIn("nobody watching", summary)

    def test_a_missing_state_file_is_still_a_hard_problem(self):
        rc, problems, out, summary = self._check(None)
        self.assertGreaterEqual(problems, 1)
        self.assertEqual(rc, 1)

    def test_a_stale_selfcheck_record_is_hard(self):
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rc, problems, out, summary = self._check({
            "schema_version": 2, "roll_number": "13572468",
            "current_semester": 4, "last_examined_notice_id": 15236,
            "last_poll_time": self._today(),
            "examined": {}, "pending_since": {}, "hosts_down": {},
            "semesters": {"4": {"status": "pending"}},
        }, selfcheck_lines='{"at": "%s", "ok": true, "verdict": "ok"}\n' % old)
        self.assertEqual(problems, 1)
        self.assertIn("self-check for 9 days", summary)

    def test_a_recent_successful_selfcheck_is_clean(self):
        import datetime
        recent = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        rc, problems, out, summary = self._check({
            "schema_version": 2, "roll_number": "13572468",
            "current_semester": 4, "last_examined_notice_id": 15236,
            "last_poll_time": self._today(),
            "examined": {}, "pending_since": {}, "hosts_down": {},
            "semesters": {"4": {"status": "pending"}},
        }, selfcheck_lines='{"at": "%s", "ok": true, "verdict": "ok"}\n' % recent)
        self.assertEqual(problems, 0, out[-600:])
        self.assertEqual(rc, 0)


class ExamNoticeHeadOfLineTest(unittest.TestCase):
    """DEFECT 2 (HIGH): the exam-notice lane had an unbounded head-of-line freeze.

    `_process_exam_notice_feed` used to ``break`` on the first unreadable list
    title and NOTHING else - no streak counter, no escape. One malformed anchor
    therefore silenced every exam routine, form and deadline notice ABOVE it,
    forever, silently: the floor stayed pinned at 0, zero payloads were emitted,
    and the run still exited 0. During exam season that is the alert the owner
    most needs, gone with no error anywhere.

    The result pass already solves exactly this with `_defer_and_maybe_block`.
    This lane keeps its own ``exam_notice_seen:<feed>`` buckets, so it gets the
    same threshold via `_exam_defer_streak` on the same constant. The floor is
    NOT weakened: the blocked id stays ``deferred``, a deferred id is not a
    decision, and advance() still refuses to move the floor past it.
    """

    MALFORMED = 16001
    ABOVE = (16002, 16003)

    def _feed_html(self):
        # 16001 is an anchor with no title at all - the shape the live index
        # produces when a card renders empty. 16002/16003 above it are the two
        # alerts that must survive it.
        return ('<html><body>'
                '<div class="notice-item"><span>2026-09-30</span>'
                '<a href="/notices/16001"></a></div>'
                '<div class="notice-item"><span>2026-09-30</span>'
                '<a href="/notices/16002">Exam Routine 2082 published</a></div>'
                '<div class="notice-item"><span>2026-09-30</span>'
                '<a href="/notices/16003">Admission Card Form deadline 2082'
                '</a></div>'
                '</body></html>').encode()

    def _run_n(self, d, runs):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        state_path = os.path.join(d, "state.json")
        preview = os.path.join(d, "preview.json")
        car.save_state(state_path, seed_state())
        fetch = make_fetch({
            RESULT_FEED: (200, list_html([]).encode()),
            ALL_FEED: (200, self._feed_html()),
        })
        per_run = []
        for _ in range(runs):
            if os.path.exists(preview):
                os.remove(preview)
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview):
                rc = car.run(state_path, RESULT_FEED, dry_run=True,
                             fetch_fn=fetch, notices_config=cfg)
            got = []
            if os.path.exists(preview):
                with open(preview, encoding="utf-8") as fh:
                    got = json.load(fh)
            per_run.append((rc, got))
        return per_run, car.load_state(state_path)

    def _bucket(self, state):
        return state["exam_notice_seen:iost_all"]

    def test_exam_notices_above_a_malformed_anchor_are_examined(self):
        """The regression itself. Pre-fix, all three runs emitted nothing and no
        id above 16001 was ever recorded."""
        with tempfile.TemporaryDirectory() as d:
            per_run, after = self._run_n(d, car.ACADEMIC_MAX_CONSECUTIVE_DEFERS)
            bucket = self._bucket(after)
            for rc, _payloads in per_run:
                self.assertEqual(rc, 0)
            for nid in self.ABOVE:
                self.assertIn(str(nid), bucket,
                              "the exam notice above the malformed anchor was "
                              "never examined")
                self.assertEqual(bucket[str(nid)]["outcome"], "processed")
                self.assertEqual(bucket[str(nid)]["reason"],
                                 "deadline_notified")
            self.assertEqual(len(per_run[-1][1]), len(self.ABOVE),
                             "both exam alerts must be delivered")

    def test_the_malformed_anchor_is_still_deferred_not_decided(self):
        """The guard must not be a backdoor around R5."""
        with tempfile.TemporaryDirectory() as d:
            _per_run, after = self._run_n(d, 4)
            bucket = self._bucket(after)
            self.assertEqual(bucket[str(self.MALFORMED)]["outcome"], "deferred")
            self.assertNotIn(self.MALFORMED, car._decided_ids(bucket))
            self.assertLessEqual(bucket["__floor__"], self.MALFORMED,
                                 "the floor moved past an undecided notice")

    def test_the_freeze_is_loud(self):
        """Six runs used to be silent. It must shout at the threshold."""
        import io
        with tempfile.TemporaryDirectory() as d:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            state_path = os.path.join(d, "state.json")
            preview = os.path.join(d, "preview.json")
            car.save_state(state_path, seed_state())
            fetch = make_fetch({
                RESULT_FEED: (200, list_html([]).encode()),
                ALL_FEED: (200, self._feed_html()),
            })
            err = io.StringIO()
            with mock.patch.object(car, "NOTIFY_PREVIEW_PATH", preview), \
                    mock.patch("sys.stderr", err):
                for _ in range(car.ACADEMIC_MAX_CONSECUTIVE_DEFERS):
                    car.run(state_path, RESULT_FEED, dry_run=True,
                            fetch_fn=fetch, notices_config=cfg)
            self.assertIn("::error::academic: exam-notice 16001",
                          err.getvalue())
            self.assertIn("blocking the exam-notice pass", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
