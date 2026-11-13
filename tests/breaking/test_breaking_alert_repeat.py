#!/usr/bin/env python3
"""tests/breaking/test_breaking_alert_repeat.py — the 2026-11-12/13
Makwanpur microbus crash produced FOUR CRITICAL pushes for ONE real event.

Two distinct faults are pinned here:

  A. the same real event, reported by another outlet under a fresh per-outlet
     event id, re-alerted inside the TTL;
  B. the Worker tripwire payload and the alerted story were different objects,
     so an elephant-attack dispatch sent a minibus-crash headline.

Plain asserts, stdlib only, no network. Run:

    py tests\\breaking\\test_breaking_alert_repeat.py
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import breaking_monitor as bm  # noqa: E402
from test_breaking_monitor import (  # noqa: E402
    NOW, make_fetch, read_json, rfc822, rss_xml, run_main,
)

FIXTURE = os.path.join(REPO_ROOT, "tests", "breaking", "fixtures",
                       "makwanpur_runs.json")

CRASH_PAYLOAD = ("10 killed, five injured as electric microbus plunges off "
                 "road in Makawanpur")
ELEPHANT_PAYLOAD = "Woman killed in elephant attack in Kanchanpur"
FIRECRACKER_PAYLOAD = ("Seven held for setting off firecrackers in Rupandehi, "
                       "one injured")

# The validate_stories controls: these must never be read as one event.
SHOUT = ("Nvidia CEO Jensen Huang resigns from leadership positions amid "
         "controversy over alleged affairs")
DENIAL = "Nvidia CEO Jensen Huang denies reports he is stepping down"
EARNINGS = "Nvidia shares slide as AI chip demand cools"


def _paths(sd):
    return {
        "active": os.path.join(sd, "active_stories.json"),
        "log": os.path.join(sd, "breaking_log.json"),
        "pending": os.path.join(sd, "breaking_pending.json"),
        "breaking_state": os.path.join(sd, "breaking_state.json"),
        "preview": os.path.join(sd, "email-preview.html"),
        "telegram_preview": os.path.join(sd, "telegram-preview.txt"),
        "ntfy_preview": os.path.join(sd, "ntfy-preview.txt"),
        "breaking_delivery": os.path.join(sd, "breaking_delivery.json"),
        "quiet_queue": os.path.join(sd, "quiet_queue.json"),
    }


def _candidate(run):
    return {
        "title": run["candidate_title"],
        "url": run["candidate_url"],
        "source": run["source"],
        "snippet": run.get("snippet") or run["candidate_title"],
    }


def _manual_verdict(run):
    """The pre-fix selected story, kept so B is exercised even if the fixed
    fast path would now refuse it."""
    return {
        "level": 3,
        "confidence": "HIGH",
        "event_key": "",
        "title": run["candidate_title"],
        "summary": "",
        "why_urgent": "fixture replay",
        "sources": [{"name": run["source"], "url": run["candidate_url"]}],
        "category_suggestion": "",
        "universal_importance": True,
        "hard_evidence_fast_path": True,
    }


def _with_payload(payload, source="google-news"):
    os.environ["PAYLOAD_TITLE"] = payload
    os.environ["PAYLOAD_SOURCE"] = source


def _clear_payload():
    os.environ.pop("PAYLOAD_TITLE", None)
    os.environ.pop("PAYLOAD_SOURCE", None)


class AlertRepeatTest(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp(prefix="brk-repeat-")
        self.paths = _paths(self.sd)
        self.state = {"stories": [], "archived": []}
        self.args = argparse.Namespace(dry_run=True)
        self.now = datetime(2026, 11, 13, 3, 0, tzinfo=timezone.utc)
        _clear_payload()

    def tearDown(self):
        _clear_payload()
        shutil.rmtree(self.sd, ignore_errors=True)

    def _run(self, run, use_fast_path=True):
        _with_payload(run["payload"])
        cand = _candidate(run)
        verdict = None
        if use_fast_path:
            verdict, _why = bm.hard_evidence_fast_path(
                [cand], anchor_title=run["payload"])
        if verdict is None:
            verdict = _manual_verdict(run)
        return bm.process_alert(verdict, [cand], self.state, self.paths,
                                self.args, now_utc=self.now)

    def test_the_four_incident_runs_produce_exactly_one_push(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            runs = json.load(fh)["runs"]
        results = [(run, self._run(run)) for run in runs]

        sent = [run["run_id"] for run, result in results if result[0]]
        self.assertEqual(sent, ["43215822535"], (
            "the four incident runs must produce exactly one push; sent=%r"
            % sent))

        by_id = {run["run_id"]: result for run, result in results}
        self.assertIn("hard_evidence_fast_path", by_id["43215822535"][4])
        self.assertIn("payload_unbound", by_id["43231822031"][4])
        self.assertIn("duplicate_same_event", by_id["43336436813"][4])
        self.assertIn("payload_unbound", by_id["43352664558"][4])

        alerted = [ev for ev in self.state["stories"] if ev.get("alerted")]
        self.assertEqual(len(alerted), 1, (
            "one real event must leave one alerted state row: %r" % alerted))
        self.assertEqual(alerted[0]["alert_count"], 1)
        self.assertEqual(alerted[0]["alert_anchor"], CRASH_PAYLOAD)

    def test_a_genuinely_different_second_event_still_alerts(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            first = json.load(fh)["runs"][0]
        self.assertTrue(self._run(first)[0])

        second = {
            "payload": "At least 20 dead after landslide in Sindhuli",
            "candidate_title": ("At least 20 dead after landslide in Sindhuli "
                                "- Example News"),
            "candidate_url": "https://example-news-a.com/sindhuli-landslide",
            "source": "example-news-a.com",
        }
        _with_payload(second["payload"])
        cand = _candidate(second)
        verdict, _why = bm.hard_evidence_fast_path(
            [cand], anchor_title=second["payload"])
        self.assertIsNotNone(verdict)
        result = bm.process_alert(verdict, [cand], self.state, self.paths,
                                  self.args, now_utc=self.now)
        self.assertTrue(result[0], (
            "a different real event must still alert: %r" % (result,)))
        self.assertEqual(
            len([e for e in self.state["stories"] if e.get("alerted")]), 2)

    def test_the_elephant_payload_never_yields_a_minibus_title(self):
        minibus = {
            "title": ("Minibus accident in Nepal results in 14 fatalities"),
            "url": "https://www.example-news.com/minibus-accident-nepal",
            "source": "example-news.com",
            "snippet": "Fourteen people died.",
        }
        verdict, _why = bm.hard_evidence_fast_path(
            [minibus], anchor_title=ELEPHANT_PAYLOAD)
        self.assertIsNone(verdict, (
            "an elephant-attack tripwire must not take the fast path on a "
            "minibus story: %r" % (verdict,)))

        _with_payload(ELEPHANT_PAYLOAD)
        result = bm.process_alert(_manual_verdict({
            "candidate_title": minibus["title"],
            "candidate_url": minibus["url"],
            "source": minibus["source"],
        }), [minibus], self.state, self.paths, self.args, now_utc=self.now)
        self.assertFalse(result[0])
        self.assertIn("payload_unbound", result[4])

    def test_title_and_body_come_from_one_story_object(self):
        first = {
            "title": ("Death toll rises to 14 in Makwanpur microbus crash; "
                      "six from same family killed - Nepalnews.com"),
            "url": "https://www.nepalnews.com/makwanpur-crash",
            "source": "nepalnews.com",
        }
        second = {
            "title": ("14 killed as microbus plunges off hilly road in "
                      "central Nepal - Daily Excelsior"),
            "url": "https://www.dailyexcelsior.com/central-nepal-microbus",
            "source": "dailyexcelsior.com",
        }
        verdict = {
            "title": second["title"],
            "summary": "Only the second story's body.",
        }
        bound = bm._bind_alert_story(verdict, [first, second], CRASH_PAYLOAD)
        self.assertIs(bound, second, (
            "the alert must bind to the story whose title it repeats, not to "
            "an earlier candidate the anchor happens to match: %r" % (bound,)))

        mixed = dict(verdict, title="A headline nobody in the pool reported")
        self.assertIsNone(bm._bind_alert_story(mixed, [first, second],
                                               CRASH_PAYLOAD), (
            "a verdict title that matches no candidate must suppress, not "
            "borrow another story's summary"))

    def test_ttl_expiry_lets_a_new_real_alert_through(self):
        event = {
            "event_id": "evt-b57401979cb5",
            "canonical_title": ("Death toll rises to 14 in Makwanpur microbus "
                                "crash; six from same family killed - "
                                "Nepalnews.com"),
            "alerted": True,
            "alert_anchor": CRASH_PAYLOAD,
            "last_alerted": (self.now - timedelta(hours=23)).isoformat(),
            "status": "DEVELOPING",
        }
        state = {"stories": [event]}
        self.assertIsNotNone(bm._recent_alert_event(
            state, "a different title", self.now, anchor_title=CRASH_PAYLOAD))

        event["last_alerted"] = (self.now - timedelta(hours=25)).isoformat()
        self.assertIsNone(bm._recent_alert_event(
            state, "a different title", self.now, anchor_title=CRASH_PAYLOAD), (
            "25 hours after the last push the TTL must have expired"))

    def test_main_wires_the_payload_into_the_fast_path_and_the_dedupe(self):
        """The same wiring the two real runs used: --no-model, a Worker
        PAYLOAD_TITLE, and two outlets naming the same crash."""
        first = ("Death toll rises to 14 in Makwanpur microbus crash; six "
                 "from same family killed - Nepalnews.com")
        second = ("14 killed as microbus plunges off hilly road in central "
                  "Nepal - Daily Excelsior")
        _with_payload(CRASH_PAYLOAD)
        body = rss_xml([(first, "https://www.nepalnews.com/makwanpur-crash",
                         rfc822(NOW), first)])
        rc = run_main(self.sd, make_fetch({"feeds.example.test": body}),
                      no_model=True)
        self.assertEqual(rc, 0)
        rows = read_json(os.path.join(self.sd, "breaking_log.json"))
        self.assertTrue(rows[-1]["sent"], rows[-1])

        body = rss_xml([(second,
                         "https://www.dailyexcelsior.com/central-nepal-microbus",
                         rfc822(NOW), second)])
        rc = run_main(self.sd, make_fetch({"feeds.example.test": body}),
                      no_model=True)
        self.assertEqual(rc, 0)
        rows = read_json(os.path.join(self.sd, "breaking_log.json"))
        self.assertFalse(rows[-1]["sent"], rows[-1])
        self.assertIn("duplicate_same_event", rows[-1]["reason"])

    def test_the_earlier_controls_stay_apart(self):
        self.assertFalse(bm._same_alert_event(SHOUT, DENIAL), (
            "a denial merged into the report it denies"))
        self.assertFalse(bm._same_alert_event(SHOUT, EARNINGS), (
            "an unrelated earnings story merged into the resignation"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
