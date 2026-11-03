#!/usr/bin/env python3
"""tests/breaking/test_breaking_monitor.py — offline tests for
scripts/breaking_monitor.py.

Plain asserts, no test framework, stdlib only. Run:

    py tests\\breaking\\test_breaking_monitor.py

Every network touch is removed by injecting a fake fetch_fn(url)->
(status, text); the AI stage is replaced by a fake runner returning canned
strict-JSON verdicts; SMTP is replaced by a fake factory (raising or
recording). All state lives in per-test temp dirs.
"""

import contextlib
import io
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import threading
import time
import unittest.mock
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import alert_dedupe            # noqa: E402
import breaking_monitor as bm  # noqa: E402
import ai_classifier as ac     # noqa: E402
import live_canary             # noqa: E402

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

def rss_xml(items):
    chunks = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<rss><channel>"]
    for title, link, pub, desc in items:
        chunks.append(
            "<item><title>%s</title><link>%s</link>"
            "<pubDate>%s</pubDate><description>%s</description></item>"
            % (title, link, pub, desc))
    chunks.append("</channel></rss>")
    return "".join(chunks)


def rfc822(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def make_fetch(responses):
    """responses: {url_substring: body}. Unmatched URLs raise."""
    def fetch_fn(url):
        for key, body in responses.items():
            if key in url:
                return 200, body
        raise RuntimeError("no fake response for %s" % url)
    return fetch_fn


def failing_fetch(url):
    raise RuntimeError("network down")


def verdict_runner(verdict):
    def runner(model, prompt_text, env, timeout_sec):
        assert "CANDIDATES_PATH" in env, "agent env must carry CANDIDATES_PATH"
        return 0, json.dumps(verdict), ""
    return runner


class FakeSMTP:
    behavior = "ok"
    sent_subjects = []

    def __init__(self, host, port):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if FakeSMTP.behavior == "raise":
            raise smtplib.SMTPException("relay denied (fake)")
        FakeSMTP.sent_subjects.append(msg["Subject"])


def smtp_factory(behavior):
    def factory(host, port):
        FakeSMTP.behavior = behavior
        return FakeSMTP(host, port)
    return factory


def fresh_dir():
    path = tempfile.mkdtemp(prefix="brk-test-")
    return path


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_active(state_dir):
    """active_stories.json only exists once something was written; a missing
    file means an empty fresh state."""
    path = os.path.join(state_dir, "active_stories.json")
    if not os.path.isfile(path):
        return {"stories": [], "archived": []}
    return read_json(path)


def log_rows(state_dir):
    return read_json(os.path.join(state_dir, "breaking_log.json"))


EARTHQUAKE_ITEM = (
    "Magnitude 6.8 earthquake strikes near Example District, tremors felt in Example City",
    "https://example-news-a.com/quake-example-district",
    rfc822(NOW), "Strong shaking reported; details emerging.",
)

QUAKE_VERDICT = {
    "level": 3,
    "confidence": "HIGH",
    "event_key": "example-district-earthquake",
    "title": "Magnitude 6.8 earthquake near Example District",
    "summary": "Confirmed: a magnitude 6.8 earthquake struck near Example District. "
               "Tremors were felt in Example City.",
    "why_urgent": "Aftershocks possible; check family and structure safety.",
    "escalated_from_previous": False,
    "sources": [
        {"name": "NEA", "url": "https://nea.org.np/quake-notice"},
        {"name": "Kathmandu Post",
         "url": "https://kathmandupost.com/national/quake-example-district"},
    ],
    "category_suggestion": "local_city",
}


def quake_sources():
    return [{
        "name": "test-feed",
        "url": "https://feeds.example.test/np.xml",
        "kind": "rss",
        "category": "local_city",
    }]


def run_main(state_dir, fetch, runner=None, dry_run=True, smtp=None,
             sources_json="", window_min=90, no_model=False, extra_argv=(),
             now_utc=None):
    if not sources_json:
        # Default every run to the single synthetic test feed; otherwise the
        # built-in real-URL source list would bypass the fake fetch layer.
        sources_json = os.path.join(state_dir, "sources.json")
        bm.write_json_atomic(sources_json, quake_sources())
    argv = ["--state-dir", state_dir,
            "--sources-json", sources_json,
            "--window-min", str(window_min)]
    if dry_run:
        argv.append("--dry-run")
    if no_model:
        argv.append("--no-model")
    argv.extend(extra_argv)
    return bm.main(argv, fetch_fn=fetch, ai_runner=runner,
                   smtp_factory=smtp or smtp_factory("ok"), now_utc=now_utc)


# ---------------------------------------------------------------------------
# 1. Routine news -> silent heartbeat
# ---------------------------------------------------------------------------

def test_1_routine_news_silent_heartbeat():
    sd = fresh_dir()
    try:
        body = rss_xml([
            ("TU publishes BSc CSIT exam routine",
             "https://tu.edu.np/routine", rfc822(NOW - timedelta(minutes=5)),
             "Regular exams begin next month."),
            ("Example Bank opens new Example City branch counter",
             "https://bank.example.com/branch-news", rfc822(NOW),
             "Extended banking hours announced."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}))
        assert rc == 0, "exit code must be 0, got %s" % rc
        rows = log_rows(sd)
        assert len(rows) == 1 and rows[0]["tripped"] is False
        assert rows[0]["checked"] == 2
        assert not os.path.isfile(os.path.join(sd, "breaking_pending.json"))
        assert not os.path.isfile(os.path.join(sd, "email-preview.html"))
        active = read_active(sd)
        assert active["stories"] == []
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. BREAKING headline but routine substance -> no trip
# ---------------------------------------------------------------------------

def test_2_breaking_headline_routine_substance_no_trip():
    sd = fresh_dir()
    try:
        body = rss_xml([
            ("BREAKING: Celebrity box office war ends as final match score "
             "revealed",
             "https://example-news-a.com/sport", rfc822(NOW),
             "Pure entertainment coverage, nothing civic."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}))
        assert rc == 0
        rows = log_rows(sd)
        assert rows[0]["tripped"] is False, \
            "sport/gossip domination must suppress the emergency word"
        assert not os.path.isfile(os.path.join(sd, "breaking_pending.json"))
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. Earthquake-Example District candidate -> trips -> level 3 -> alert rendered
# ---------------------------------------------------------------------------

def test_3_earthquake_trips_level3_alert_rendered():
    sd = fresh_dir()
    try:
        body = rss_xml([EARTHQUAKE_ITEM])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(QUAKE_VERDICT))
        assert rc == 0
        preview_path = os.path.join(sd, "email-preview.html")
        assert os.path.isfile(preview_path), "dry-run must render a preview"
        with open(preview_path, "r", encoding="utf-8") as f:
            html = f.read()
        assert "Magnitude 6.8 earthquake near Example District" in html
        assert ">3<" in html or "Level 3" in html or "LEVEL 3" in html.upper()
        assert "{{" not in html, "all tokens must be substituted"
        assert bm.DEVELOPING_NOTICE_TEXT not in html, \
            "HIGH confidence must NOT render the developing notice"
        assert "nea.org.np" in html
        active = read_active(sd)
        assert len(active["stories"]) == 1
        ev = active["stories"][0]
        assert ev["alerted"] is True and ev["alert_count"] == 1
        assert ev["last_confidence"] == "HIGH"
        row = log_rows(sd)[-1]
        assert row["tripped"] is True and row["level"] == 3
        assert row["confidence"] == "HIGH" and row["sent"] is True
        assert os.path.isfile(os.path.join(sd, "breaking_pending.json"))
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Same event next run -> no duplicate alert
# ---------------------------------------------------------------------------

def test_4_same_event_no_duplicate():
    sd = fresh_dir()
    try:
        body = rss_xml([EARTHQUAKE_ITEM])
        fetch = make_fetch({"feeds.example.test": body})
        runner = verdict_runner(QUAKE_VERDICT)
        assert run_main(sd, fetch, runner=runner) == 0
        preview_path = os.path.join(sd, "email-preview.html")
        with open(preview_path, "rb") as f:
            before = f.read()
        rc = run_main(sd, fetch, runner=runner)
        assert rc == 0
        with open(preview_path, "rb") as f:
            after = f.read()
        assert before == after, "duplicate run must not re-render/send"
        active = read_active(sd)
        assert active["stories"][0]["alert_count"] == 1, \
            "alert_count must stay at 1 on duplicate"
        row = log_rows(sd)[-1]
        assert row["sent"] is False
        assert row["reason"] == "duplicate_no_escalation"
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. Escalated confidence MEDIUM -> HIGH -> re-alert
# ---------------------------------------------------------------------------

def test_5_confidence_escalation_realerts():
    sd = fresh_dir()
    try:
        body = rss_xml([EARTHQUAKE_ITEM])
        fetch = make_fetch({"feeds.example.test": body})
        medium = dict(QUAKE_VERDICT, confidence="MEDIUM", sources=[
            {"name": "A", "url": "https://example-news-a.com/quake-example-district"},
            {"name": "B", "url": "https://example-b.net/quake"},
        ])
        high = dict(QUAKE_VERDICT, confidence="HIGH", sources=[
            {"name": "NEA", "url": "https://nea.org.np/quake-notice"},
            {"name": "A", "url": "https://example-news-a.com/quake-example-district"},
        ])
        assert run_main(sd, fetch, runner=verdict_runner(medium)) == 0
        active = read_active(sd)
        assert active["stories"][0]["last_confidence"] == "MEDIUM"
        assert active["stories"][0]["alert_count"] == 1

        assert run_main(sd, fetch, runner=verdict_runner(high)) == 0
        active = read_active(sd)
        ev = active["stories"][0]
        assert ev["alert_count"] == 2, "rank increase must re-alert"
        assert ev["last_confidence"] == "HIGH"
        assert log_rows(sd)[-1]["sent"] is True
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Rumor wording -> capped level 2, no send
# ---------------------------------------------------------------------------

def test_6_rumor_wording_capped_no_send():
    sd = fresh_dir()
    try:
        body = rss_xml([EARTHQUAKE_ITEM])
        rumor = dict(QUAKE_VERDICT)
        rumor["summary"] = ("Reported: an earthquake was allegedly felt near "
                            "Example District, but damage claims remain unconfirmed.")
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(rumor))
        assert rc == 0
        assert not os.path.isfile(os.path.join(sd, "email-preview.html")), \
            "level 2 must never send"
        row = log_rows(sd)[-1]
        assert row["level"] == 2 and row["sent"] is False
        assert "rumor_marker_level_cap" in row["reason"]
        active = read_active(sd)
        assert all(not e.get("alerted") for e in active["stories"])
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. Entertainment spoiler urgent -> vetoed silent
# ---------------------------------------------------------------------------

def test_7_entertainment_spoiler_vetoed():
    sd = fresh_dir()
    try:
        src_path = os.path.join(sd, "sources.json")
        write_json_atomic = bm.write_json_atomic
        write_json_atomic(src_path, [{
            "name": "anime-feed",
            "url": "https://feeds.example.test/anime.xml",
            "kind": "rss",
            "category": "anime",
        }])
        body = rss_xml([
            ("Anime finale leaks: hero dies in war ending explained scene",
             "https://example-fansite.test/ep", rfc822(NOW),
             "Plot details inside."),
        ])
        spoiler_verdict = {
            "level": 3, "confidence": "HIGH",
            "event_key": "anime-ending-leak",
            "title": "New anime episode released",
            "summary": "The finale leaked online; the main character dies "
                       "in the ending explained scene.",
            "why_urgent": "", "escalated_from_previous": False,
            "sources": [
                {"name": "Fansite", "url": "https://example-fansite.test/ep"},
                {"name": "Forum", "url": "https://forum.example.test/thread"},
            ],
            "category_suggestion": "nepal",
        }
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(spoiler_verdict),
                      sources_json=src_path)
        assert rc == 0
        assert not os.path.isfile(os.path.join(sd, "email-preview.html")), \
            "spoiler veto must silence the alert"
        row = log_rows(sd)[-1]
        assert row["level"] == 1 and row["sent"] is False
        assert "spoiler_veto" in row["reason"]
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. All sources fail -> silent, sources_failed logged
# ---------------------------------------------------------------------------

def test_8_all_sources_fail_silent_logged():
    sd = fresh_dir()
    try:
        rc = run_main(sd, failing_fetch)
        assert rc == 0
        rows = log_rows(sd)
        assert rows[0]["tripped"] is False and rows[0]["checked"] == 0
        assert rows[0]["sources_failed"] > 0, "failures must be logged"
        assert not os.path.isfile(os.path.join(sd, "breaking_pending.json"))
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. SMTP raises -> pending_alert persisted, event NOT marked alerted;
#    a later successful retry clears pending and alerts.
# ---------------------------------------------------------------------------

def test_9_smtp_failure_persists_pending_then_retry_clears():
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    try:
        body = rss_xml([EARTHQUAKE_ITEM])
        fetch = make_fetch({"feeds.example.test": body})
        runner = verdict_runner(QUAKE_VERDICT)
        rc = run_main(sd, fetch, runner=runner, dry_run=False,
                      smtp=smtp_factory("raise"))
        assert rc == 0
        bstate = read_json(os.path.join(sd, "breaking_state.json"))
        assert len(bstate["pending_alert"]) == 1, \
            "SMTP failure must persist pending_alert"
        (marked_date,) = bstate["pending_alert"].values()
        assert marked_date == bm.story_state.kathmandu_today().isoformat()
        active = read_active(sd)
        assert all(not e.get("alerted") for e in active["stories"]), \
            "failed send must NOT mark alerted"
        # ntfy-first: the reason now leads with the PRIMARY channel's outcome,
        # so the smtp_error marker is looked for anywhere in it, not only as
        # the whole prefix. Both the ntfy state and the SMTP failure must be
        # present — neither failure may be hidden behind the other.
        row = log_rows(sd)[-1]
        assert row["sent"] is False
        assert "smtp_error" in row["reason"], row["reason"]
        assert "ntfy_not_configured" in row["reason"], row["reason"]

        # Retry with working SMTP: sends, clears pending, marks alerted.
        rc = run_main(sd, fetch, runner=runner, dry_run=False,
                      smtp=smtp_factory("ok"))
        assert rc == 0
        bstate = read_json(os.path.join(sd, "breaking_state.json"))
        assert bstate["pending_alert"] == {}, \
            "successful send must clear pending_alert"
        active = read_active(sd)
        assert any(e.get("alerted") for e in active["stories"])
        assert len(FakeSMTP.sent_subjects) == 1
        assert FakeSMTP.sent_subjects[0].startswith("🚨 Breaking:")
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 10. Atomic race: parallel save calls keep files valid JSON
# ---------------------------------------------------------------------------

def test_10_parallel_writes_keep_files_valid():
    sd = fresh_dir()
    try:
        log_path = os.path.join(sd, "breaking_log.json")
        st_path = os.path.join(sd, "breaking_state.json")
        errors = []

        def worker(n):
            try:
                for i in range(6):
                    bm.append_breaking_log(log_path, {
                        "run_at": "t", "checked": n * 100 + i,
                        "tripped": False})
                    bm.write_json_atomic(st_path, {"pending_alert": {
                        "evt-%d-%d" % (n, i): "2026-08-25"}})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        rows = read_json(log_path)          # must parse cleanly
        assert isinstance(rows, list) and len(rows) <= bm.LOG_CAP
        assert all(isinstance(r, dict) and "tripped" in r for r in rows)
        st = read_json(st_path)
        assert isinstance(st, dict) and "pending_alert" in st
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 11. Window filter drops >90min-old items (and unparseable dates)
# ---------------------------------------------------------------------------

def test_11_window_filter_drops_old_items():
    cands = [
        bm._mk_cand("Fresh quake report", "https://a.test/1", "a.test",
                    NOW - timedelta(minutes=10), "", "c", "en", "rss"),
        bm._mk_cand("Stale quake report", "https://b.test/2", "b.test",
                    NOW - timedelta(minutes=120), "", "c", "en", "rss"),
        bm._mk_cand("No date article", "https://c.test/3", "c.test",
                    None, "", "c", "en", "rss"),
        bm._mk_cand("Weather forecast Example City", "https://open-meteo.test/w",
                    "open-meteo.test", None, "", "weather_forecast", "en",
                    "weather"),
    ]
    final, stats = bm.apply_window_and_cap(cands, NOW, window_min=90,
                                           max_candidates=40)
    urls = [c["url"] for c in final]
    assert "https://a.test/1" in urls, "fresh item kept"
    assert "https://b.test/2" not in urls, ">90min dropped"
    assert "https://c.test/3" not in urls, "unparseable date dropped"
    assert "https://open-meteo.test/w" in urls, "weather forecast exempt"
    assert stats["dropped_stale"] == 1 and stats["dropped_nodate"] == 1

    # cap: freshest-first ordering under a tight cap (distinct token sets so
    # the title-Jaccard dedup does not interfere)
    many = [bm._mk_cand("Bulletin alpha%02d update" % i,
                        "https://x.test/%d" % i, "x.test",
                        NOW - timedelta(minutes=i), "", "c", "en", "rss")
            for i in range(10)]
    final2, _ = bm.apply_window_and_cap(many, NOW, 90, 3)
    assert [c["url"] for c in final2] == [
        "https://x.test/0", "https://x.test/1", "https://x.test/2"]
    # unparseable-date RSS candidates are also dropped during collection
    body = rss_xml([
        ("Dated item", "https://d.test/1", rfc822(NOW), ""),
        ("Undated item", "https://d.test/2", "", ""),
    ])
    got, ok, failed, _ = bm.collect_candidates(
        [{"name": "f", "url": "https://feeds.example.test/x", "kind": "rss"}],
        make_fetch({"feeds.example.test": body}), 90, 40, now_utc=NOW)
    assert [c["url"] for c in got] == ["https://d.test/1"]
    assert ok and not failed


# ---------------------------------------------------------------------------
# 12. Pre-gate regex unit table
# ---------------------------------------------------------------------------

PRE_GATE_TABLE = [
    # (text, expect_trip)
    ("Magnitude 6.7 earthquake hits Example District district", True),
    ("Flood warning issued for low-lying Example City wards", True),
    ("Example Highway blocked after night rains", True),
    ("Example Highway closed near Example District", True),
    ("Example Bank cyber incident affects customers", True),
    ("Example Bank fraud case registered", True),
    ("Bank of Nepal systems hit by cyber attack", True),
    ("Nepal declares national emergency amid crisis", True),
    ("Major outage knocks NEA grid offline", True),
    ("Officials confirm nuclear plant inspection", True),
    ("Celebrity box office war ends as final match score revealed", False),
    ("TU publishes BSc CSIT exam routine", False),
    ("Football match score: Nepal wins friendly", False),
    ("Example Bank opens new branch in Example City", False),
    ("Example Bank launches festival offer", False),
]


def test_12_pre_gate_regex_table():
    for text, expect_trip in PRE_GATE_TABLE:
        tripped, why = bm.pre_gate_check(text)
        assert tripped is expect_trip, \
            "pre_gate(%r) => %s (%s), expected trip=%s" % (
                text, tripped, why, expect_trip)


# ---------------------------------------------------------------------------
# 13. Synthetic direct-RSS fixture (Nepal emergency) trips end-to-end with
#     detection_source=rss (PAYLOAD_SOURCE env from the worker dispatch)
# ---------------------------------------------------------------------------

def test_13_rss_fixture_trips_with_detection_source_rss():
    sd = fresh_dir()
    prev = os.environ.pop("PAYLOAD_SOURCE", None)
    try:
        os.environ["PAYLOAD_SOURCE"] = "rss"
        body = rss_xml([
            ("Flood warning issued for Sunkoshi river banks in Example District district",
             "https://www.onlinekhabar.com/flood-example-district-sunkoshi",
             rfc822(NOW),
             "District administration asks riverside wards to stay alert."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(QUAKE_VERDICT))
        assert rc == 0
        preview_path = os.path.join(sd, "email-preview.html")
        assert os.path.isfile(preview_path), "dry-run must render a preview"
        with open(preview_path, "r", encoding="utf-8") as f:
            html = f.read()
        assert "Detected via: rss" in html, \
            "alert preview must carry the detection_source line"
        assert "{{" not in html, "all tokens must be substituted"
        active = read_active(sd)
        assert len(active["stories"]) == 1
        ev = active["stories"][0]
        assert ev.get("detection_source") == "rss", \
            "active_stories event must store detection_source=rss"
        assert ev["alerted"] is True
    finally:
        if prev is None:
            os.environ.pop("PAYLOAD_SOURCE", None)
        else:
            os.environ["PAYLOAD_SOURCE"] = prev
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 14. gnews + direct-RSS carrying the SAME story -> collapsed to ONE
#     candidate -> single dispatch/alert (worker seenTitles analog: title
#     Jaccard dedup in apply_window_and_cap)
# ---------------------------------------------------------------------------

def test_14_gnews_rss_same_story_collision_single_dispatch():
    sd = fresh_dir()
    try:
        src_path = os.path.join(sd, "sources.json")
        bm.write_json_atomic(src_path, [
            {"name": "gnews-np-en",
             "url": "https://gnews.example.test/np", "kind": "rss",
             "category": "local_city"},
            {"name": "publisher-rss",
             "url": "https://publisher.example.test/feed", "kind": "rss",
             "category": "local_city"},
        ])
        bodies = {
            "gnews.example.test": rss_xml([EARTHQUAKE_ITEM]),
            # Same headline, different publisher URL — only the title
            # Jaccard signal can collapse this pair.
            "publisher.example.test": rss_xml([
                (EARTHQUAKE_ITEM[0],
                 "https://www.onlinekhabar.com/quake-example-district-magnitude",
                 rfc822(NOW), EARTHQUAKE_ITEM[3]),
            ]),
        }

        def fetch(url):
            for key, b in bodies.items():
                if key in url:
                    return 200, b
            raise RuntimeError("no fake response for %s" % url)

        rc = run_main(sd, fetch, runner=verdict_runner(QUAKE_VERDICT),
                      sources_json=src_path)
        assert rc == 0
        row = log_rows(sd)[-1]
        assert row["checked"] == 1, \
            "identical story on two feeds must collapse to ONE candidate"
        assert row["sent"] is True
        active = read_active(sd)
        assert len(active["stories"]) == 1
        assert active["stories"][0]["alert_count"] == 1, \
            "collision must produce exactly one dispatch/alert"
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 15–18. Telegram channel for Level-3 alerts (additive to email)
# ---------------------------------------------------------------------------

TG_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


class TgRecorder:
    """Injectable Telegram transport (replaces bm.tg_http). Records every
    (url, payload) call; getUpdates returns ``updates`` (or raises when
    None); sendMessage returns ok or a Telegram error description."""

    def __init__(self, updates=None, send_ok=True):
        self.calls = []
        self.updates = updates
        self.send_ok = send_ok

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if "/getUpdates" in url:
            if self.updates is None:
                raise RuntimeError("getUpdates down (fake)")
            return 200, json.dumps(self.updates)
        if self.send_ok is True:
            return 200, json.dumps({"ok": True,
                                    "result": {"message_id": 7}})
        return 200, json.dumps({"ok": False, "description": str(self.send_ok)})

    def sends(self):
        return [(u, p) for u, p in self.calls
                if p is not None and "/sendMessage" in u]

    def get_updates_calls(self):
        return [c for c in self.calls if "/getUpdates" in c[0]]


def telegram_env(token=None, chat_id=None):
    """Pop both TELEGRAM_* vars and set the requested ones.
    Returns a restore() callable."""
    prev = {k: os.environ.pop(k, None) for k in TG_ENV_KEYS}
    if token is not None:
        os.environ["TELEGRAM_BOT_TOKEN"] = token
    if chat_id is not None:
        os.environ["TELEGRAM_CHAT_ID"] = chat_id

    def restore():
        for k in TG_ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in prev.items():
            if v is not None:
                os.environ[k] = v

    return restore


def quake_body():
    return rss_xml([EARTHQUAKE_ITEM])


def test_15_l3_email_and_telegram_both_fire_chat_id_autodiscovered():
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    restore = telegram_env(token="123:AUTO")
    orig_http = bm.tg_http
    rec = TgRecorder(updates={
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 111}}},
            {"update_id": 2, "message": {"chat": {"id": 4242}}},
        ],
    })
    bm.tg_http = rec
    sent_before = len(FakeSMTP.sent_subjects)
    try:
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
        assert rc == 0
        # email still fired
        assert len(FakeSMTP.sent_subjects) == sent_before + 1
        # chat id auto-discovered: exactly one getUpdates, LAST id wins
        assert len(rec.get_updates_calls()) == 1
        sends = rec.sends()
        assert len(sends) == 1, "exactly one sendMessage expected"
        url, payload = sends[0]
        assert "/bot123:AUTO/sendMessage" in url
        assert payload["chat_id"] == 4242, \
            "must use the LAST result[].message.chat.id"
        assert payload["parse_mode"] == "HTML"
        assert payload["disable_web_page_preview"] is False
        text = payload["text"]
        assert "\U0001f6a8 <b>BREAKING</b> [HIGH] Magnitude 6.8 earthquake near Example District" in text
        assert "NEW \u00b7 local_city" in text
        assert '<a href="https://nea.org.np/quake-notice">' in text
        assert "Read article</a>" in text
        assert "<i>Detected via google-news" in text
        # The real NPT fire time rides on the non-CRITICAL alert too.
        assert re.search(r"fired \d{4}-\d{2}-\d{2} \d{2}:\d{2} NPT", text), \
            "breaking Telegram alert must state the real NPT fire time"
        # Simplified format: no summary, no bold title, no bracketed label
        assert "<b>Magnitude" not in text, "simplified format must not bold the title separately"
        assert "Confirmed: a magnitude" not in text, "simplified format must not include summary"
        row = log_rows(sd)[-1]
        assert row["sent"] is True and row["reason"].startswith("sent")
        # ntfy is the primary channel and was never even attempted here, so
        # the record must say so rather than claim a clean email-only send.
        assert "ntfy_not_configured" in row["reason"], row["reason"]
        active = read_active(sd)
        assert any(e.get("alerted") for e in active["stories"])
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_16_telegram_api_fails_email_still_sent_state_updated():
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    restore = telegram_env(token="123:FAIL", chat_id="777")
    orig_http = bm.tg_http
    rec = TgRecorder(send_ok="Bad Request: chat not found")
    bm.tg_http = rec
    sent_before = len(FakeSMTP.sent_subjects)
    try:
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
        assert rc == 0
        # email unaffected
        assert len(FakeSMTP.sent_subjects) == sent_before + 1
        # explicit chat id => no getUpdates attempt
        assert not rec.get_updates_calls()
        assert len(rec.sends()) == 1
        # state semantics unchanged by the Telegram failure
        active = read_active(sd)
        ev = active["stories"][0]
        assert ev["alerted"] is True and ev["alert_count"] == 1
        row = log_rows(sd)[-1]
        assert row["sent"] is True
        assert "telegram_failed:" in row["reason"], row["reason"]
        assert "chat not found" in row["reason"]
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_17_no_token_no_attempt():
    sd = fresh_dir()
    restore = telegram_env(token=None)          # both vars absent
    orig_http = bm.tg_http
    rec = TgRecorder()
    bm.tg_http = rec
    try:
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
        assert rc == 0
        assert rec.calls == [], "no Telegram network call without a token"
        row = log_rows(sd)[-1]
        assert row["sent"] is True and row["reason"].startswith("sent")
        assert "ntfy_not_configured" in row["reason"], row["reason"]
        assert not os.path.isfile(os.path.join(sd, "telegram-preview.txt"))
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_18_dry_run_writes_preview_file_no_network():
    sd = fresh_dir()
    restore = telegram_env(token="123:DRY", chat_id="999")
    orig_http = bm.tg_http
    rec = TgRecorder()
    bm.tg_http = rec
    try:
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(QUAKE_VERDICT), dry_run=True)
        assert rc == 0
        assert rec.calls == [], "dry-run must never touch the Telegram API"
        tg_path = os.path.join(sd, "telegram-preview.txt")
        assert os.path.isfile(tg_path), \
            "dry-run must write state/telegram-preview.txt"
        with open(tg_path, "r", encoding="utf-8") as f:
            text = f.read()
        assert "\U0001f6a8 <b>BREAKING</b> [HIGH] Magnitude 6.8 earthquake near Example District" in text
        assert "NEW \u00b7 local_city" in text
        assert '<a href="https://nea.org.np/quake-notice">' in text
        assert "Read article</a>" in text
        assert "<i>Detected via google-news" in text
        assert re.search(r"fired \d{4}-\d{2}-\d{2} \d{2}:\d{2} NPT", text), \
            "breaking Telegram preview must state the real NPT fire time"
        # Simplified format: no summary. The fire time is present, but the
        # CRITICAL-only "checked through" line is not.
        assert "Confirmed:" not in text, "simplified format must not include summary"
        assert "checked through" not in text
        assert os.path.isfile(os.path.join(sd, "email-preview.html")), \
            "email preview still written alongside"
        row = log_rows(sd)[-1]
        assert row["sent"] is True and row["reason"].startswith("sent")
        # --dry-run never touches the network, so the ntfy outcome is the
        # preview state and must be recorded as such, not as delivered.
        assert "ntfy:dry_run_preview" in row["reason"], row["reason"]
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 19–24. Universal importance override + CRITICAL detailed alert format
# ---------------------------------------------------------------------------

def test_19_check_universal_importance_death_count():
    """Items with death counts > 10 match universal importance."""
    assert bm.check_universal_importance({
        "title": "Bus crash kills 15 in Nepal",
        "summary": "A bus fell off a cliff killing at least 15 passengers."
    }) is True
    assert bm.check_universal_importance({
        "title": "12 dead after building collapse in India",
        "summary": "Rescuers pull survivors from rubble."
    }) is True
    # Low death count should NOT trigger
    assert bm.check_universal_importance({
        "title": "Bus crash kills 3 in Example City",
        "summary": "Three passengers died in the accident."
    }) is False
    # No death count
    assert bm.check_universal_importance({
        "title": "Bus accident reported near Example District",
        "summary": "Injuries reported, no fatalities."
    }) is False


def test_20_check_universal_importance_disasters():
    """Major disasters with context cues trigger universal importance."""
    # Magnitude 7+ earthquake
    assert bm.check_universal_importance({
        "title": "Magnitude 7.2 earthquake strikes Nepal",
        "summary": "Strong tremors felt across the country."
    }) is True
    # Earthquake with destruction
    assert bm.check_universal_importance({
        "title": "Earthquake devastates coastal city",
        "summary": "Thousands left homeless after the quake."
    }) is True
    # Volcanic eruption
    assert bm.check_universal_importance({
        "title": "Volcanic eruption forces evacuation",
        "summary": "Lava flow threatens nearby villages."
    }) is True
    # Tsunami with deaths
    assert bm.check_universal_importance({
        "title": "Tsunami kills dozens in Indonesia",
        "summary": "Waves crash into coastal settlements."
    }) is True
    # Flood with destruction
    assert bm.check_universal_importance({
        "title": "Flood devastates lowland districts",
        "summary": "Thousands displaced by rising waters."
    }) is True
    # Weak earthquake (not major)
    assert bm.check_universal_importance({
        "title": "Minor earthquake felt in Kathmandu",
        "summary": "Tremors briefly shook buildings."
    }) is False


def test_21_check_universal_importance_policy_geopolitics():
    """Policy changes, wars, pandemics, and infrastructure failures match."""
    # War / invasion
    assert bm.check_universal_importance({
        "title": "War breaks out between two nations",
        "summary": "Border conflict escalates to full-scale war."
    }) is True
    # Pandemic
    assert bm.check_universal_importance({
        "title": "WHO declares new pandemic",
        "summary": "New virus spreading rapidly across continents."
    }) is True
    # Nationwide ban
    assert bm.check_universal_importance({
        "title": "Government imposes nationwide ban on social media",
        "summary": "New regulation affects millions of users."
    }) is True
    # Power grid failure
    assert bm.check_universal_importance({
        "title": "Power grid failure leaves millions without electricity",
        "summary": "Major outage affects three provinces."
    }) is True
    # Terrorist attack
    assert bm.check_universal_importance({
        "title": "Terrorist attack in capital city",
        "summary": "Multiple casualties reported."
    }) is True
    # Routine policy (not major)
    assert bm.check_universal_importance({
        "title": "Local council approves park renovation",
        "summary": "Budget allocated for green space improvement."
    }) is False


def test_22_universal_importance_overrides_relevance():
    """Universal importance forces level=3, confidence=HIGH, label=CRITICAL."""
    # Use a verdict that triggers universal importance (war).
    war_verdict = {
        "level": 2, "confidence": "MEDIUM",
        "event_key": "test-war",
        "title": "War declared between neighboring countries",
        "summary": "Full-scale war declared, armies mobilizing.",
        "why_urgent": "Major geopolitical crisis.",
        "escalated_from_previous": False,
        "sources": [
            {"name": "Reuters", "url": "https://reuters.com/war-declared"},
        ],
        "category_suggestion": "local_city",
    }
    sd = fresh_dir()
    try:
        body = rss_xml([
            ("War declared between neighboring countries",
             "https://reuters.com/war-declared",
             rfc822(NOW),
             "Full-scale war declared between nations."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(war_verdict))
        assert rc == 0
        preview_path = os.path.join(sd, "email-preview.html")
        assert os.path.isfile(preview_path), "dry-run must render a preview"
        with open(preview_path, "r", encoding="utf-8") as f:
            html = f.read()
        # Must be CRITICAL detailed format
        assert "CRITICAL ALERT" in html, \
            "universal-importance item must use CRITICAL format"
        assert "War declared between neighboring countries" in html
        # Must contain bullet-point structure
        assert "What:" in html, "must have What bullet"
        assert "Where:" in html, "must have Where bullet"
        assert "Status:" in html, "must have Status bullet"
        # Must not contain old one-liner tokens
        assert "{{" not in html, "all tokens must be substituted"
        active = read_active(sd)
        assert len(active["stories"]) == 1
        ev = active["stories"][0]
        assert ev["alerted"] is True and ev["alert_count"] == 1
        row = log_rows(sd)[-1]
        assert row["tripped"] is True and row["sent"] is True
        assert row["confidence"] == "HIGH"
        assert "universal_importance_override" in row["reason"]
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_23_ai_universal_importance_flag_triggers_override():
    """When the AI sets universal_importance=true, the override fires
    even if the regex check doesn't match."""
    # Craft a verdict that the AI marks as universal importance but the regex
    # doesn't catch (e.g. a unique phrase like "global crisis").
    # NOTE: The title must pass EMERGENCY_RE pre-gate to reach the AI stage.
    ai_verdict = {
        "level": 2, "confidence": "MEDIUM",
        "event_key": "test-ai-universal",
        "title": "Major outage reported worldwide affecting millions",
        "summary": "Major cloud providers experiencing widespread outages "
                   "affecting millions worldwide.",
        "why_urgent": "Critical infrastructure impact.",
        "escalated_from_previous": False,
        "universal_importance": True,    # AI says universal
        "sources": [
            {"name": "TechCrunch",
             "url": "https://techcrunch.com/infrastructure-crisis"},
        ],
        "category_suggestion": "local_city",
    }
    sd = fresh_dir()
    try:
        body = rss_xml([
            ("Major outage reported worldwide affecting millions",
             "https://techcrunch.com/infrastructure-crisis",
             rfc822(NOW),
             "Major cloud outages worldwide."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(ai_verdict))
        assert rc == 0
        preview_path = os.path.join(sd, "email-preview.html")
        assert os.path.isfile(preview_path)
        with open(preview_path, "r", encoding="utf-8") as f:
            html = f.read()
        assert "CRITICAL ALERT" in html, \
            "AI universal_importance=true must trigger CRITICAL format"
        row = log_rows(sd)[-1]
        assert row["confidence"] == "HIGH"
        assert "universal_importance_override" in row["reason"]
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_24_critical_telegram_uses_detailed_format():
    """CRITICAL items use detailed bullet-point format in Telegram too."""
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    restore = telegram_env(token="123:CRIT", chat_id="555")
    orig_http = bm.tg_http
    rec = TgRecorder()
    bm.tg_http = rec
    war_verdict = {
        "level": 2, "confidence": "MEDIUM",
        "event_key": "test-war-tg",
        "title": "War declared in the region",
        "summary": "Major war declared, forces mobilizing at the border.",
        "why_urgent": "Geopolitical crisis.",
        "escalated_from_previous": False,
        "sources": [
            {"name": "Al Jazeera",
             "url": "https://aljazeera.com/war-declared"},
        ],
        "category_suggestion": "local_city",
    }
    try:
        body = rss_xml([
            ("War declared in the region",
             "https://aljazeera.com/war-declared",
             rfc822(NOW),
             "Major war declared."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(war_verdict), dry_run=False)
        assert rc == 0
        sends = rec.sends()
        assert len(sends) == 1
        _, payload = sends[0]
        text = payload["text"]
        # Must be CRITICAL format, not the standard BREAKING one-liner
        assert "CRITICAL ALERT" in text, \
            "Telegram must use CRITICAL format for universal-importance"
        assert "What:" in text, "must have bullet points"
        assert "Status:" in text
        assert "<b>" in text, "must use HTML formatting"
        assert "[NEW]" not in text, \
            "CRITICAL format should not include [label] one-liner"
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_25_non_critical_items_use_standard_format():
    """Items that do NOT match universal importance use the standard format."""
    sd = fresh_dir()
    try:
        body = rss_xml([EARTHQUAKE_ITEM])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(QUAKE_VERDICT))
        assert rc == 0
        preview_path = os.path.join(sd, "email-preview.html")
        assert os.path.isfile(preview_path)
        with open(preview_path, "r", encoding="utf-8") as f:
            html = f.read()
        # Standard format, NOT CRITICAL
        assert "CRITICAL ALERT" not in html, \
            "non-universal-importance item must NOT use CRITICAL format"
        assert "Breaking" in html or "BREAKING" in html
        row = log_rows(sd)[-1]
        assert row["sent"] is True
        assert "universal_importance_override" not in row.get("reason", "")
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_26_ai_classifier_universal_importance_passthrough():
    """ai_classifier ensures universal_importance is in output (default False)."""
    test_items = [{"url": "https://example.com/test-story",
                   "title": "Test story about tech",
                   "summary": "A tech story for classification."}]
    env_patch = {
        "GEMINI_API_KEY": "fake-key",
        "OPENAI_API_KEY": "fake-key",
        "ANTHROPIC_API_KEY": "fake-key",
        "OPENCODE_API_KEY": "fake-key",
    }

    async def fake_run_provider(provider, prompt, api_key):
        return provider["name"], [{
            "title": "Test story",
            "category": "tech",
            "confidence": "HIGH",
            "summary": "A tech story.",
            "relevance_score": 0.5,
            # No universal_importance key — should default to False
        }]

    with unittest.mock.patch.dict(os.environ, env_patch, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            result = ac.classify_batch(test_items)

    assert result[0].get("universal_importance") is False, \
        "missing universal_importance must default to False"

    # When AI returns universal_importance=true
    async def fake_run_provider_universal(provider, prompt, api_key):
        return provider["name"], [{
            "title": "Major disaster",
            "category": "local",
            "confidence": "HIGH",
            "summary": "A major disaster occurred.",
            "relevance_score": 0.9,
            "universal_importance": True,
        }]

    with unittest.mock.patch.dict(os.environ, env_patch, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider",
                                        fake_run_provider_universal):
            result = ac.classify_batch(test_items)

    assert result[0]["universal_importance"] is True, \
        "AI universal_importance=true must pass through"


def test_27_prompt_includes_universal_importance():
    """The classification prompt includes universal_importance instruction."""
    test_items = [{"url": "https://example.com/test-story",
                   "title": "Test story about tech",
                   "summary": "A tech story for classification."}]
    prompt = ac._build_prompt(test_items)
    assert "universal_importance" in prompt
    assert "mass" in prompt.lower() or "everyone" in prompt.lower()


# ---------------------------------------------------------------------------
# 28–29. ALL breaking alerts → both Gmail AND Telegram
# ---------------------------------------------------------------------------

def test_28_non_critical_breaking_both_gmail_and_telegram():
    """Non-CRITICAL level-3 alert sends BOTH Gmail and Telegram (simple format)."""
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    restore = telegram_env(token="123:NOCRIT", chat_id="800")
    orig_http = bm.tg_http
    rec = TgRecorder(updates={
        "ok": True,
        "result": [{"update_id": 1, "message": {"chat": {"id": 800}}}],
    })
    bm.tg_http = rec
    sent_before = len(FakeSMTP.sent_subjects)
    # Example Bank incident: matches EMERGENCY_RE pre-gate but does NOT
    # match UNIVERSAL_IMPORTANCE_RE, so stays non-CRITICAL.
    # Use non-established domains so verify_level3 keeps MEDIUM confidence.
    medium_verdict = {
        "level": 3, "confidence": "MEDIUM",
        "event_key": "test-example-incident",
        "title": "Example Bank security incident affects Example City branch",
        "summary": "Customers report service disruption at Example City counter.",
        "why_urgent": "Cybersecurity incident at major bank.",
        "escalated_from_previous": False,
        "sources": [
            {"name": "Local News NP",
             "url": "https://localnewsnp.com/example-bank-incident"},
            {"name": "Example City Times",
             "url": "https://exampletimes.com/example-bank-incident"},
        ],
        "category_suggestion": "local_city",
    }
    try:
        body = rss_xml([
            ("Example Bank security incident affects Example City branch",
             "https://localnewsnp.com/example-bank-incident",
             rfc822(NOW),
             "Customers report service disruption."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(medium_verdict), dry_run=False)
        assert rc == 0
        # Gmail sent
        assert len(FakeSMTP.sent_subjects) == sent_before + 1
        assert FakeSMTP.sent_subjects[-1].startswith("🚨 Breaking:")
        # Telegram sent
        sends = rec.sends()
        assert len(sends) == 1, "non-CRITICAL level-3 must send Telegram"
        _, payload = sends[0]
        text = payload["text"]
        # Must be simple format, NOT CRITICAL
        assert "CRITICAL ALERT" not in text, \
            "non-CRITICAL must not use CRITICAL format"
        assert "\U0001f6a8 <b>BREAKING</b> [MEDIUM] Example Bank security incident affects Example City branch" in text
        assert "NEW \u00b7 local_city" in text
        assert '<a href="https://localnewsnp.com/example-bank-incident">' in text
        assert "Read article</a>" in text
        assert "<i>Detected via google-news" in text
        assert re.search(r"fired \d{4}-\d{2}-\d{2} \d{2}:\d{2} NPT", text), \
            "breaking Telegram alert must state the real NPT fire time"
        # No summary in simplified format
        assert "Customers" not in text
        # State correctly updated
        active = read_active(sd)
        assert any(e.get("alerted") for e in active["stories"])
        row = log_rows(sd)[-1]
        assert row["sent"] is True
        assert row["level"] == 3 and row["confidence"] == "MEDIUM"
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_29_critical_breaking_both_gmail_and_telegram_detailed():
    """CRITICAL (universal-importance) alert sends BOTH Gmail and Telegram
    with the detailed bullet-point format for Telegram."""
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    restore = telegram_env(token="123:CRIT2", chat_id="900")
    orig_http = bm.tg_http
    rec = TgRecorder(updates={
        "ok": True,
        "result": [{"update_id": 1, "message": {"chat": {"id": 900}}}],
    })
    bm.tg_http = rec
    sent_before = len(FakeSMTP.sent_subjects)
    # War verdict → triggers universal importance regex → CRITICAL
    war_verdict = {
        "level": 2, "confidence": "MEDIUM",
        "event_key": "test-war-both",
        "title": "War declared between neighboring countries",
        "summary": "Full-scale war declared, armies mobilizing at the border.",
        "why_urgent": "Major geopolitical crisis.",
        "escalated_from_previous": False,
        "sources": [
            {"name": "Reuters", "url": "https://reuters.com/war-declared"},
            {"name": "Al Jazeera",
             "url": "https://aljazeera.com/war-declared"},
        ],
        "category_suggestion": "local_city",
    }
    try:
        body = rss_xml([
            ("War declared between neighboring countries",
             "https://reuters.com/war-declared",
             rfc822(NOW),
             "Full-scale war declared."),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(war_verdict), dry_run=False)
        assert rc == 0
        # Gmail sent
        assert len(FakeSMTP.sent_subjects) == sent_before + 1
        assert FakeSMTP.sent_subjects[-1].startswith("🚨 CRITICAL:")
        # Telegram sent
        sends = rec.sends()
        assert len(sends) == 1, "CRITICAL must send Telegram"
        _, payload = sends[0]
        text = payload["text"]
        # Must be detailed CRITICAL format, NOT the simple one-liner
        assert "CRITICAL ALERT" in text, "Telegram must use CRITICAL format"
        assert "What:" in text, "CRITICAL format must have bullet points"
        assert "Where:" in text
        assert "Status:" in text
        assert "<b>" in text, "must use HTML formatting"
        # Must NOT be the simple BREAKING format
        assert "\U0001f6a8 <b>BREAKING</b>" not in text, \
            "CRITICAL must not use simple format"
        assert "NEW \u00b7" not in text, \
            "CRITICAL must not use label one-liner"
        # State correctly updated
        active = read_active(sd)
        assert any(e.get("alerted") for e in active["stories"])
        row = log_rows(sd)[-1]
        assert row["sent"] is True
        assert row["confidence"] == "HIGH"
        assert "universal_importance_override" in row["reason"]
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 30-31. Event-driven labelling contract
# ---------------------------------------------------------------------------

#: Tokens that belong to the 2x/day SCHEDULED digest and must never appear on
#: an event-driven breaking/urgent alert. A breaking alert has no slot: it
#: fires whenever the event happens, so a morning/evening label on it is a lie
#: (it is what made a 23:00 alert read as "evening").
SLOT_TOKENS = ("morning", "evening", "☀", "\U0001f319",
               "Morning", "Evening", "MORNING", "EVENING")

FIRED_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} NPT")


def _assert_no_slot(where, blob):
    for tok in SLOT_TOKENS:
        assert tok not in blob, (
            "%s must not carry the digest slot token %r: a breaking alert is "
            "event-driven, not slot-driven.\n---\n%s\n---" % (where, tok, blob))


def _assert_has_fired_time(where, blob):
    assert FIRED_RE.search(blob), (
        "%s must state the real NPT fire time (YYYY-MM-DD HH:MM NPT).\n---\n%s\n---"
        % (where, blob))


def test_30_alert_labels_real_fire_time_never_a_slot():
    """A breaking alert shows the REAL NPT time it fired and carries NO
    morning/evening/☀/🌙 token — on every surface the owner actually reads:
    email subject, email HTML, email text, and both Telegram formats.

    Regression guard for the owner's report: an off-schedule night alert was
    readable as "evening" because the alert carried no fire time of its own.
    """
    # -- the two helpers, pinned directly ---------------------------------
    assert bm.alert_fired_str("2026-09-27 23:42") == "2026-09-27 23:42 NPT"
    assert bm.alert_subject("🚨 Breaking: X [HIGH]", "2026-09-27 23:42") == \
        "🚨 Breaking: X [HIGH] — 2026-09-27 23:42 NPT"
    # A real 23:42 fire must never be re-labelled by hour-of-day.
    _assert_no_slot("alert_subject at 23:42",
                    bm.alert_subject("🚨 Breaking: X [HIGH]", "2026-09-27 23:42"))

    # -- standard (non-CRITICAL) alert end to end, offline -----------------
    sd = fresh_dir()
    restore = telegram_env(token="123:T30", chat_id="321")
    rec = TgRecorder()
    orig_http = bm.tg_http
    bm.tg_http = rec
    try:
        # dry-run: writes the two preview files the owner would inspect.
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(QUAKE_VERDICT), dry_run=True)
        assert rc == 0
        with open(os.path.join(sd, "email-preview.html"), encoding="utf-8") as f:
            html = f.read()
        _assert_no_slot("breaking email HTML", html)
        _assert_has_fired_time("breaking email HTML", html)

        text_body = bm.build_plain_text({
            "LEVEL": "3", "CONFIDENCE": "HIGH",
            "TITLE": "Magnitude 6.8 earthquake near Example District",
            "SUMMARY": "Confirmed: a magnitude 6.8 earthquake struck near Example District.",
            "WHY_URGENT": "Shaking felt in Example City.",
            "CUTOFF_NPT": "2026-09-27 23:42",
            "DEVELOPING_NOTICE": "", "DETECTED_VIA": "Detected via: rss",
            "_SOURCE_LINES": ["nea.org.np — https://nea.org.np/quake-notice"],
        })
        _assert_no_slot("breaking email text", text_body)
        _assert_has_fired_time("breaking email text", text_body)

        with open(os.path.join(sd, "telegram-preview.txt"), encoding="utf-8") as f:
            tg = f.read()
        _assert_no_slot("breaking Telegram alert", tg)
        _assert_has_fired_time("breaking Telegram alert", tg)
    finally:
        bm.tg_http = orig_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)

    # -- the live SMTP path: the subject is the lock-screen surface --------
    sd2 = fresh_dir()
    try:
        sent_before = len(FakeSMTP.sent_subjects)
        rc = run_main(sd2, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
        assert rc == 0
        assert len(FakeSMTP.sent_subjects) == sent_before + 1, \
            "the live path must send exactly one alert email"
        subject = FakeSMTP.sent_subjects[-1]
        _assert_no_slot("breaking email subject", subject)
        _assert_has_fired_time("breaking email subject", subject)
    finally:
        shutil.rmtree(sd2, ignore_errors=True)

    # -- CRITICAL (universal-importance) format, same contract ------------
    for rendered in (
        bm._render_critical_html(
            bm.generate_detailed_alert(QUAKE_VERDICT), "HIGH", "rss",
            "2026-09-27 23:42"),
        bm._render_critical_plain(
            bm.generate_detailed_alert(QUAKE_VERDICT), "HIGH", "rss",
            "2026-09-27 23:42"),
        bm._render_critical_telegram(
            bm.generate_detailed_alert(QUAKE_VERDICT), "rss", "2026-09-27 23:42"),
    ):
        _assert_no_slot("CRITICAL alert surface", rendered)
        _assert_has_fired_time("CRITICAL alert surface", rendered)


def test_31_normal_digest_keeps_its_own_slot_labels():
    """The scheduled 2x/day digest is UNCHANGED: morning still renders
    "Morning", evening still renders "Evening", and neither borrows the
    alert's fire-time line. This fix must not touch the normal digest.

    CHANGED, and the reason is worth keeping: this test used to hand BOTH
    iterations the same string, `"2026-09-27 06:00 NPT"`. That string is not an
    `NPT` stamp at all -- it fails `strptime(..., "%Y-%m-%d %H:%M")` -- so the
    label was silently derived from the wall clock, and then BOTH pages were
    rendered from the SAME minute. Since "Morning" (05:00-11:59) and "Evening"
    (17:00-20:59) are disjoint windows, at most one of the two assertions could
    ever pass at any instant: it was not flaky, it was unsatisfiable, and it was
    green only while the wall clock happened to sit inside one of the windows.

    So each slot is now stamped with a well-formed build instant inside its own
    window, which is what a real morning and a real evening render with. The
    assertions are also STRICTER than the ones they replace: the old pair proved
    each page CONTAINED its own word, which a page carrying both words would
    also satisfy. These prove each page carries its own word and NOT the
    other's, which is the claim the docstring was already making.
    """
    sys.path.insert(0, SCRIPTS)
    import render_digest

    stories = [{
        "id": "s1", "title": "Road opened", "summary": "The road reopened.",
        "category": "local_city", "priority": 5, "reliability": "tier1",
        "url": "https://example.com/a", "sources": [],
    }]
    # (slot, NPT build stamp, expected word, the word it must NOT borrow)
    for slot, stamp, expected, forbidden in (
        ("morning", "2026-09-27 06:00", "Morning", "Evening"),
        ("evening", "2026-09-27 19:00", "Evening", "Morning"),
    ):
        page = render_digest.render_page(stories, slot, "2026-09-27", stamp)
        assert "%s Digest · " % expected in page, \
            "%s digest must still render its own %r label (built at %s NPT)" \
            % (slot, expected, stamp)
        assert "%s Digest · " % forbidden not in page, \
            "%s digest (built at %s NPT) must not carry the %r label: the word " \
            "is derived from the real send instant, so a morning digest can " \
            "only claim to be a morning digest" % (slot, stamp, forbidden)
        assert "Alert fired:" not in page, \
            "the scheduled digest must not gain the alert fire-time line"


def test_32_plain_text_source_lines_are_bulleted():
    """The plain-text alert body renders every source as a "- " bullet.

    Regression guard: _SOURCE_LINES is built as a bare "name — url" string
    (no prefix of its own, unlike the HTML path which wraps in <li>), so
    build_plain_text owns the bullet. A refactor dropped the prefix and the
    Sources block shipped as a run-on list of unbulleted URLs. Nothing covered
    it, so the breaking suite stayed green.
    """
    sources = [
        "nea.org.np — https://nea.org.np/quake-notice",
        "kathmandupost.com — https://kathmandupost.com/national/2026/09/27/quake",
    ]
    text = bm.build_plain_text({
        "LEVEL": "3", "CONFIDENCE": "HIGH",
        "TITLE": "Magnitude 6.8 earthquake near Example District",
        "SUMMARY": "Confirmed: a magnitude 6.8 earthquake struck near Example District.",
        "WHY_URGENT": "Shaking felt in Example City.",
        "CUTOFF_NPT": "2026-09-27 23:42",
        "DEVELOPING_NOTICE": "", "DETECTED_VIA": "Detected via: rss",
        "_SOURCE_LINES": sources,
    })

    # The Sources: block, pinned exactly, bullets and order included.
    expected_block = "Sources:\n" + "".join("- %s\n" % s for s in sources)
    assert expected_block in text, (
        "the Sources: block must render one '- ' bullet per source, in order; "
        "got:\n%s" % text[text.index("Sources:"):]
    )

    # Every source present, each bulleted, and never bare on its own line.
    for src in sources:
        assert "- %s" % src in text, "source missing its '- ' bullet: %s" % src
        for line in text.splitlines():
            if src in line:
                assert line == "- %s" % src, (
                    "source line must be exactly the bulleted form, got %r" % line
                )


# ---------------------------------------------------------------------------
# 33-38. universal_importance coercion at the normalize_verdict boundary
# ---------------------------------------------------------------------------

def base_verdict(**extra):
    """A minimal verdict that normalize_verdict accepts, plus any overrides.

    With no overrides the universal_importance key is ABSENT, which is itself
    one of the cases under test.
    """
    verdict = {
        "level": 2,
        "confidence": "MEDIUM",
        "event_key": "coercion-probe",
        "title": "Routine local update",
        "summary": "A short summary.",
        "why_urgent": "Not urgent.",
        "escalated_from_previous": False,
        "sources": [{"name": "Example", "url": "https://example.test/a"}],
        "category_suggestion": "local_city",
    }
    verdict.update(extra)
    return verdict


def test_33_normalize_verdict_string_true_is_true():
    """THE BUG. universal_importance:"true" (a JSON *string*) used to be
    coerced to False by `obj.get("universal_importance") is True`: the string
    evaluates False, and False was then STORED, so by the time any consumer
    read the field the model's actual answer no longer existed. Silent - no
    error, no log. Test_37 pins the user-visible consequence.

    `is True` on the right-hand side is deliberately required, not `== True`:
    a non-bool must not be able to pass this assertion."""
    v = bm.normalize_verdict(base_verdict(universal_importance="true"))
    assert v is not None, "this bug must not reject the whole verdict either"
    assert v["universal_importance"] is True, (
        'the string "true" must coerce to True, got %r'
        % (v["universal_importance"],))


def test_34_normalize_verdict_bool_true_still_true():
    """The working path must survive the fix: real booleans still pass
    through untouched, in both directions."""
    for raw in (True, False):
        v = bm.normalize_verdict(base_verdict(universal_importance=raw))
        assert v is not None
        assert v["universal_importance"] is raw, (
            "boolean %r must pass through unchanged, got %r"
            % (raw, v["universal_importance"]))


def test_35_normalize_verdict_false_spellings():
    """False in every spelling a model actually emits stays False.

    The companion question is the positive one: should "True" and " true "
    count? They do, and that is a deliberate decision rather than leniency.
    A model writing `universal_importance: "True"` or `" true "` has said
    true; rejecting those spellings would reintroduce the exact silent-
    downgrade class this fix exists to remove, just with a narrower trigger.
    Hence .strip().lower() rather than a bare == "true"."""
    for raw in ("false", "False", "FALSE", " false ", "false ", " false",
                "falsey", "no", ""):
        v = bm.normalize_verdict(base_verdict(universal_importance=raw))
        assert v is not None
        assert v["universal_importance"] is False, (
            "%r must coerce to False, got %r"
            % (raw, v["universal_importance"]))

    for raw in ("true", "True", "TRUE", " true ", "true ", "\ttrue\n"):
        v = bm.normalize_verdict(base_verdict(universal_importance=raw))
        assert v is not None
        assert v["universal_importance"] is True, (
            "%r must coerce to True, got %r"
            % (raw, v["universal_importance"]))


def test_36_normalize_verdict_junk_never_raises():
    """A stringly-typed LLM field must never crash the normaliser.

    normalize_verdict's contract is to coerce a loose schema strictly and
    return None only for an UNUSABLE verdict. A junk universal_importance
    makes the field meaningless, not the verdict unusable, so the answer is
    False - never an exception, and never None for the whole verdict.

    Truthy junk is included deliberately: `["true"]` and `{"a": 1}` are truthy,
    so a naive `bool(value)` coercion would read them as True. Same for 1.
    Integers 0/1 are specified as False rather than truthiness-coerced: `1`
    is ambiguous as a JSON value, the old code also read it as False, and
    upgrading it to True would be a new silent upgrade in the other
    direction."""
    junk_values = [
        None,                # explicit null
        0, 1, -1, 2,         # integers (1 is truthy in Python)
        2.0,                 # float
        "yes", "no", "1", "0", "truthy", "maybe", "null", "undefined",
        [], ["true"], [1],   # truthy containers
        {}, {"a": 1}, {"universal_importance": True},
        (), ("true",), set(), frozenset(["true"]),
        object(),
    ]
    # Absent key is handled separately: base_verdict() omits it entirely.
    for raw in junk_values:
        try:
            v = bm.normalize_verdict(base_verdict(universal_importance=raw))
        except Exception as exc:                      # noqa: BLE001
            raise AssertionError(
                "normalize_verdict raised on universal_importance=%r: %r"
                % (raw, exc))
        assert v is not None, (
            "junk universal_importance=%r must not void the whole verdict"
            % (raw,))
        assert v["universal_importance"] is False, (
            "junk universal_importance=%r must coerce to False, got %r"
            % (raw, v["universal_importance"]))

    # Absent key -> False, and still a usable verdict.
    v = bm.normalize_verdict(base_verdict())
    assert v is not None, "an absent universal_importance must not void verdict"
    assert v["universal_importance"] is False


def test_37_string_true_flag_drives_override_end_to_end():
    """THE END-TO-END CONSEQUENCE, not the field shape.

    A verdict whose title and summary do NOT match the universal regexes
    (asserted below, so this test cannot silently decay into a regex test)
    but which carries universal_importance as the STRING "true", must still
    receive the level-3 bump and HIGH confidence at the consumer. Under the
    old code the string was flattened to False at the boundary, the regex arm
    stayed silent, and the story silently lost all three of: the level-3
    bump, HIGH confidence, and its survival through the personal-relevance
    gate. A field-shape assertion would not have caught that downgrade being
    reintroduced somewhere between the boundary and the consumer."""
    title = "Major outage reported across the country affecting payment networks"
    summary = ("A major outage is being reported and engineers are "
               "investigating. Details are still emerging.")
    desc = "Major outage reported across the country."

    # Precondition: the regex arm MUST be silent, or this test proves nothing
    # about the AI flag. Both sentences below are load-bearing.
    assert bm.check_universal_importance({"title": title,
                                          "summary": summary}) is False, (
        "fixture drifted into the universal regex; pick wording that does not "
        "match so this test keeps exercising the AI-flag path only")
    tripped, _why, _hits = bm.importance_gate("%s %s" % (title, desc), title)
    assert tripped, "fixture must pass the pre-gate to reach the AI stage"

    ai_verdict = {
        "level": 2, "confidence": "MEDIUM",
        "event_key": "test-string-true-universal",
        "title": title,
        "summary": summary,
        "why_urgent": "Payment networks down across the country.",
        "escalated_from_previous": False,
        "universal_importance": "true",   # STRING, not bool - the bug
        "sources": [
            {"name": "Example Wire",
             "url": "https://example.test/payment-outage"},
        ],
        "category_suggestion": "local_city",
    }
    sd = fresh_dir()
    try:
        body = rss_xml([
            (title, "https://example.test/payment-outage",
             rfc822(NOW), desc),
        ])
        rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                      runner=verdict_runner(ai_verdict))
        assert rc == 0
        preview_path = os.path.join(sd, "email-preview.html")
        assert os.path.isfile(preview_path)
        with open(preview_path, "r", encoding="utf-8") as f:
            html = f.read()
        assert "CRITICAL ALERT" in html, (
            'universal_importance:"true" (string) must trigger CRITICAL '
            "format exactly like the boolean True does")
        rows = log_rows(sd)
        assert len(rows) == 1
        row = rows[-1]
        assert row["tripped"] is True
        assert row["level"] == 3, (
            "the string flag must produce the level-3 bump, got level %r"
            % (row["level"],))
        assert row["confidence"] == "HIGH", (
            "the string flag must produce HIGH confidence, got %r"
            % (row["confidence"],))
        assert "universal_importance_override" in row["reason"], (
            "override must be recorded as coming from the AI signal, got %r"
            % (row["reason"],))
        # ai=True, regex=False - proves the override rode on the AI flag alone.
        assert "ai=True" in row["reason"] and "regex=False" in row["reason"], (
            "expected the AI-signal-only override signature, got %r"
            % (row["reason"],))
        active = read_active(sd)
        assert len(active["stories"]) == 1
        assert active["stories"][0]["alerted"] is True, (
            "universal-importance items must survive the personal-relevance "
            "gate and be alerted")
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_38_universal_importance_is_always_a_bool():
    """Regression guard: no input shape can put a non-bool in the normalised
    field. This is the invariant the whole fix rests on - the consumer's
    `is True` at the call site is only correct while this holds, so it is
    asserted directly rather than inferred."""
    inputs = [
        True, False, "true", "True", "TRUE", " true ", " false ", "false",
        "False", "", " ", None, 0, 1, -1, 2.0, 0.0, "yes", "1", "0", "none",
        "null", "[]", "{}", [], ["true"], [1, 2], {}, {"a": 1}, (), ("true",),
        set(), frozenset(), object(), b"true", b"false",
    ]
    for raw in inputs:
        v = bm.normalize_verdict(base_verdict(universal_importance=raw))
        assert v is not None, (
            "input %r must not void the verdict" % (raw,))
        got = v["universal_importance"]
        assert type(got) is bool, (
            "universal_importance must always be exactly bool, input %r "
            "produced %r of type %s" % (raw, got, type(got).__name__))

    # Same invariant for the absent-key path.
    assert type(bm.normalize_verdict(base_verdict())["universal_importance"]) \
        is bool


# ---------------------------------------------------------------------------
# 39-44. escalated_from_previous coercion at the normalize_verdict boundary
# ---------------------------------------------------------------------------
#
# The same bug class as 33-38, one line above it: `obj.get(...) is True` both
# EVALUATED the model's string "true" as False and STORED that False, so the
# real answer was already gone before any consumer could read it.
#
# The consumer is process_alert's LOW-confidence branch
# (`developing_notice = DEVELOPING_NOTICE_TEXT`). Losing this flag does NOT
# lose the alert - it loses the SAFETY CAVEAT on a LOW-confidence one, so an
# unconfirmed story reached the owner reading as firm.
#
# The argument for coercing at the boundary is even STRONGER here than for
# universal_importance, because this field's consumer is a TRUTHINESS read:
# `if verdict.get("escalated_from_previous"):`. A boundary that passed the
# model's strings straight through would make "false" TRUTHY - the opposite
# failure, and a more alarming one, because it would staple a "not yet
# confirmed" caveat onto a firmly confirmed alert. Coercing once here makes
# both string values impossible downstream.

_ABSENT = object()


def esc_verdict(value=_ABSENT, **extra):
    """base_verdict with escalated_from_previous set, or deliberately absent.

    base_verdict() seeds the key with False, so the absent-key cases need this
    to POP it rather than overwrite it - otherwise "absent" is never tested.
    """
    v = base_verdict(**extra)
    if value is _ABSENT:
        v.pop("escalated_from_previous", None)
    else:
        v["escalated_from_previous"] = value
    return v


def test_39_normalize_verdict_string_true_escalates():
    """THE BUG. escalated_from_previous:"true" (a JSON *string*) used to be
    coerced to False by `obj.get("escalated_from_previous") is True`: the
    string evaluated False, and False was then STORED, so by the time any
    consumer read the field the model's actual answer no longer existed.
    Silent - no error, no log, and no dropped alert, only a missing safety
    notice. test_43 pins the user-visible consequence.

    `is True` on the right-hand side is deliberately required, not `== True`:
    a non-bool must not be able to satisfy this assertion."""
    v = bm.normalize_verdict(esc_verdict("true"))
    assert v is not None, "this bug must not reject the whole verdict either"
    assert v["escalated_from_previous"] is True, (
        'the string "true" must coerce to True, got %r'
        % (v["escalated_from_previous"],))


def test_40_normalize_verdict_bool_escalated_passes_through():
    """The working path must survive the fix: real booleans still pass through
    untouched, in BOTH directions. A coercion that only handled strings would
    be caught here, and so would one that inverted the boolean."""
    for raw in (True, False):
        v = bm.normalize_verdict(esc_verdict(raw))
        assert v is not None
        assert v["escalated_from_previous"] is raw, (
            "boolean %r must pass through unchanged, got %r"
            % (raw, v["escalated_from_previous"]))


def test_41_normalize_verdict_escalated_false_spellings():
    """False in every spelling a model emits stays False.

    The positive arm is the deliberate half. A model writing
    escalated_from_previous:"True" has said true; rejecting that spelling would
    reintroduce the exact silent-downgrade class this fix exists to remove,
    just with a narrower trigger - so .strip().lower(), not a bare == "true".

    This arm matters more here than it does for universal_importance: the
    consumer is a truthiness read, so an un-coerced "false" would be TRUE."""
    for raw in ("false", "False", "FALSE", " false ", "false ", " false",
                "falsey", "no", ""):
        v = bm.normalize_verdict(esc_verdict(raw))
        assert v is not None
        assert v["escalated_from_previous"] is False, (
            "%r must coerce to False, got %r"
            % (raw, v["escalated_from_previous"]))

    for raw in ("true", "True", "TRUE", " true ", "true ", "\ttrue\n"):
        v = bm.normalize_verdict(esc_verdict(raw))
        assert v is not None
        assert v["escalated_from_previous"] is True, (
            "%r must coerce to True, got %r"
            % (raw, v["escalated_from_previous"]))


def test_42_normalize_verdict_escalated_junk_never_raises():
    """A stringly-typed LLM field must never crash the normaliser, and must
    never void the verdict.

    normalize_verdict's contract is to coerce a loose schema strictly and
    return None only for an UNUSABLE verdict. A junk escalation flag makes
    that FIELD meaningless, not the verdict unusable - so the answer is False,
    never an exception and never None for the whole verdict.

    Truthy junk is included deliberately: ["true"], {"a": 1} and 1 are all
    truthy, so a naive bool(value) would read them as True. Integers 0/1 are
    specified as False rather than truthiness-coerced: 1 is ambiguous as a
    JSON value, the old code also read it as False, and upgrading it to True
    would be a NEW silent upgrade in the other direction, not a fix."""
    junk_values = [
        None,                     # explicit null
        0, 1, -1, 2,              # integers (1 is truthy in Python)
        2.0,                      # float
        "yes", "no", "1", "0", "truthy", "maybe", "null", "undefined",
        [], ["true"], [1],        # truthy containers
        {}, {"a": 1}, {"escalated_from_previous": True},
        (), ("true",), set(), frozenset(["true"]),
        b"true", b"false",
        object(),
    ]
    for raw in junk_values:
        try:
            v = bm.normalize_verdict(esc_verdict(raw))
        except Exception as exc:                       # noqa: BLE001
            raise AssertionError(
                "normalize_verdict raised on escalated_from_previous=%r: %r"
                % (raw, exc))
        assert v is not None, (
            "junk escalated_from_previous=%r must not void the whole verdict"
            % (raw,))
        assert v["escalated_from_previous"] is False, (
            "junk escalated_from_previous=%r must coerce to False, got %r"
            % (raw, v["escalated_from_previous"]))

    # Absent key -> False, and still a fully usable verdict.
    v = bm.normalize_verdict(esc_verdict())
    assert v is not None, \
        "an absent escalated_from_previous must not void the verdict"
    assert v["escalated_from_previous"] is False


def test_43_string_true_escalation_keeps_developing_notice_end_to_end():
    """THE END-TO-END CONSEQUENCE, not the field shape.

    A LOW-confidence level-3 alert carrying escalated_from_previous as the
    STRING "true" must still render the DEVELOPING_NOTICE_TEXT safety caveat.
    Under the old code the string was flattened to False at the boundary, so
    the alert sent WITHOUT the "not yet independently confirmed" line - i.e.
    an unconfirmed story was presented as firm, with no error and no log.

    The second arm is the contrast that stops this test decaying into a
    template test: with the boolean False the notice must NOT appear and the
    alert must be downgraded instead. If both arms ever behaved identically,
    the notice would be riding on something other than this field. Under the
    old code the two arms WERE identical, which is exactly why the first arm
    alone is a real test and the pair is a better one."""
    title = "Major outage reported across the country affecting payment networks"
    summary = ("A major outage is being reported and engineers are "
               "investigating. Details are still emerging.")
    desc = "Major outage reported across the country."
    url = "https://example.test/payment-outage"

    # Preconditions, so this cannot silently decay into a template/regex test.
    assert bm.check_universal_importance({"title": title,
                                          "summary": summary}) is False, \
        "fixture drifted into the universal regex"
    tripped, _why, _hits = bm.importance_gate("%s %s" % (title, desc), title)
    assert tripped, "fixture must pass the pre-gate to reach the AI stage"
    # LOW confidence must be what verify_level3 RECOMPUTES, not what we assert:
    # one non-tier1, non-established domain (example.test) is the whole recipe.
    recomputed = bm.verify_level3({"sources": [{"url": url}]}, [])[2]
    assert recomputed == "LOW", \
        "fixture must recompute to LOW or this test proves nothing, got %r" \
        % (recomputed,)

    def build(escalated):
        return {
            "level": 3, "confidence": "LOW",
            "event_key": "test-string-true-escalated",
            "title": title,
            "summary": summary,
            "why_urgent": "Payment networks down across the country.",
            "escalated_from_previous": escalated,
            "sources": [{"name": "Example Wire", "url": url}],
            "category_suggestion": "local_city",
        }

    def run(escalated):
        sd = fresh_dir()
        try:
            body = rss_xml([(title, url, rfc822(NOW), desc)])
            rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                          runner=verdict_runner(build(escalated)))
            assert rc == 0
            preview = os.path.join(sd, "email-preview.html")
            html = None
            if os.path.isfile(preview):
                with open(preview, "r", encoding="utf-8") as f:
                    html = f.read()
            rows = log_rows(sd)
            assert len(rows) == 1
            return sd, html, rows[0]
        except Exception:
            shutil.rmtree(sd, ignore_errors=True)
            raise

    # --- Arm A: the STRING "true" must keep the safety notice ---------------
    sd_a, html_a, row_a = run("true")
    try:
        assert html_a is not None, \
            'escalated_from_previous:"true" must not downgrade a LOW alert'
        assert bm.DEVELOPING_NOTICE_TEXT in html_a, (
            "the safety notice must survive a string-typed escalation flag; "
            "under the bug the alert went out reading as firm")
        assert "{{" not in html_a, "all tokens must be substituted"
        assert row_a["tripped"] is True
        assert row_a["level"] == 3, \
            "an escalated LOW-confidence alert must stay level 3, got %r" \
            % (row_a["level"],)
        assert row_a["confidence"] == "LOW"
        assert row_a["sent"] is True
        assert "low_confidence_send_block_downgrade" not in row_a["reason"], \
            "the downgrade note must be absent when the story is escalated"
        assert read_active(sd_a)["stories"][0]["alerted"] is True
    finally:
        shutil.rmtree(sd_a, ignore_errors=True)

    # --- Arm B: the contrast. Boolean False must still downgrade ------------
    sd_b, html_b, row_b = run(False)
    try:
        assert html_b is None, \
            "an un-escalated LOW-confidence alert must not render an alert"
        assert row_b["level"] == 2, \
            "boolean False must keep the old downgrade behaviour, got %r" \
            % (row_b["level"],)
        assert row_b["sent"] is False
        assert row_b["reason"] == "low_confidence_send_block_downgrade"
    finally:
        shutil.rmtree(sd_b, ignore_errors=True)


def test_44_escalated_from_previous_is_always_a_bool():
    """Regression guard: no input shape can put a non-bool in the normalised
    field. This is the invariant the fix rests on, and it is asserted directly
    rather than inferred - the consumer is a truthiness read, so a stray "false"
    or "no" string would flip a firmly-confirmed alert into a falsely-hedged
    one. That is the whole reason the coercion belongs at this boundary."""
    inputs = [
        True, False, "true", "True", "TRUE", " true ", " false ", "false",
        "False", "", " ", None, 0, 1, -1, 2.0, 0.0, "yes", "1", "0", "none",
        "null", "[]", "{}", [], ["true"], [1, 2], {}, {"a": 1}, (), ("true",),
        set(), frozenset(), object(), b"true", b"false",
    ]
    for raw in inputs:
        v = bm.normalize_verdict(esc_verdict(raw))
        assert v is not None, "input %r must not void the verdict" % (raw,)
        got = v["escalated_from_previous"]
        assert type(got) is bool, (
            "escalated_from_previous must always be exactly bool, input %r "
            "produced %r of type %s" % (raw, got, type(got).__name__))
        # And it must be the *right* bool, not merely a bool.
        assert got is (raw.strip().lower() == "true"
                       if isinstance(raw, str) else raw is True), \
            "escalated_from_previous misread %r as %r" % (raw, got)

    # Same invariant for the absent-key path.
    assert type(bm.normalize_verdict(esc_verdict())[
        "escalated_from_previous"]) is bool


# ---------------------------------------------------------------------------
# 45-56. ntfy is the PRIMARY breaking channel; email/Telegram are FALLBACKS
# ---------------------------------------------------------------------------

NTFY_ENV_KEYS = ("NTFY_TOPIC", "NTFY_SERVER")

# A test topic/server is never a real secret: the transport is replaced, so
# nothing here can reach a live ntfy server or a real topic.
TEST_NTFY_SERVER = "https://ntfy.example.invalid"


def ntfy_env(topic="dummy-topic", server=TEST_NTFY_SERVER):
    """Pop both NTFY_* vars and set the requested ones (None => leave unset).
    Returns a restore() callable. NEVER used with a real value."""
    prev = {k: os.environ.pop(k, None) for k in NTFY_ENV_KEYS}
    if topic is not None:
        os.environ["NTFY_TOPIC"] = topic
    if server is not None:
        os.environ["NTFY_SERVER"] = server

    def restore():
        for k in NTFY_ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in prev.items():
            if v is not None:
                os.environ[k] = v

    return restore


class NtfyTransport:
    """Injectable ntfy transport (replaces bm.ntfy_http).

    `status` is what the transport returns as the HTTP status: an int (the
    real signal), a non-int (a transport that answered nothing checkable), or
    the string "raise" to simulate a timeout / connection error. Every call is
    recorded so a test can assert an ORDER, not just a presence.
    """

    def __init__(self, status=200, raises=False):
        self.status = status
        self.raises = raises
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if self.raises:
            raise RuntimeError("ntfy unreachable (fake)")
        if self.status == "raise":
            raise RuntimeError("ntfy timed out (fake)")
        return self.status, '{"id":"abc","time":1}'

    def bodies(self):
        return [p for _u, p in self.calls]


class OrderedChannels:
    """Records the ORDER of the three delivery decision points.

    Wraps the real bm.send_email / bm.notify_telegram / bm.ntfy_http, so an
    ordering assertion is about the code's own call sequence and not about a
    stub that bypasses the production path.
    """

    def __init__(self, transport):
        self.order = []
        self.transport = transport
        self._saved = (bm.send_email, bm.notify_telegram, bm.ntfy_http)

    def __enter__(self):
        real_send_email, real_notify_telegram, _real_ntfy_http = self._saved

        def send_email(*a, **kw):
            self.order.append("email")
            return real_send_email(*a, **kw)

        def notify_telegram(*a, **kw):
            self.order.append("telegram")
            return real_notify_telegram(*a, **kw)

        def ntfy_http(url, payload):
            self.order.append("ntfy")
            return self.transport(url, payload)

        bm.send_email = send_email
        bm.notify_telegram = notify_telegram
        bm.ntfy_http = ntfy_http
        return self

    def __exit__(self, *exc):
        bm.send_email, bm.notify_telegram, bm.ntfy_http = self._saved
        return False


def test_45_ntfy_is_attempted_before_any_fallback():
    """THE ORDER CONTRACT: ntfy first, always. On success nothing else runs."""
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    restore = telegram_env(token="123:ORDER", chat_id="777")
    restore_ntfy = ntfy_env()
    transport = NtfyTransport(status=200)
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            assert rc == 0
            assert ch.order == ["ntfy"], \
                "ntfy must be attempted first and ALONE when it delivers, got %r" \
                % (ch.order,)
            assert len(transport.calls) == 1, \
                "exactly one ntfy POST per alert, got %d" % len(transport.calls)
        finally:
            restore()
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_46_fallbacks_are_not_called_when_ntfy_succeeds():
    """ntfy delivered => email and Telegram are SKIPPED, and that is recorded."""
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    sent_before = len(FakeSMTP.sent_subjects)
    restore = telegram_env(token="123:SKIP", chat_id="777")
    restore_ntfy = ntfy_env()
    tg_rec = TgRecorder(send_ok=True)
    orig_http = bm.tg_http
    bm.tg_http = tg_rec
    transport = NtfyTransport(status=200)
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            assert rc == 0
            assert "email" not in ch.order and "telegram" not in ch.order, \
                "no fallback may fire once ntfy delivered, got %r" % (ch.order,)
            assert len(FakeSMTP.sent_subjects) == sent_before, \
                "SMTP must not be touched once ntfy delivered"
            assert tg_rec.sends() == [], \
                "Telegram must not be touched once ntfy delivered"
            row = log_rows(sd)[-1]
            assert row["sent"] is True
            assert "ntfy:delivered" in row["reason"], row["reason"]
            assert "email_skipped" in row["reason"], row["reason"]
            assert "telegram_skipped" in row["reason"], row["reason"]
            # The event is still marked alerted: it WAS delivered, by ntfy.
            active = read_active(sd)
            assert any(e.get("alerted") for e in active["stories"])
        finally:
            bm.tg_http = orig_http
            restore()
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_47_fallbacks_fire_on_a_genuine_ntfy_failure():
    """A non-2xx is a VERIFIED failure, so both fallbacks run."""
    os.environ.setdefault("GMAIL_USER", "test@example.invalid")
    os.environ.setdefault("GMAIL_APP_PASSWORD", "not-a-secret-value")
    sd = fresh_dir()
    sent_before = len(FakeSMTP.sent_subjects)
    restore = telegram_env(token="123:FALLBACK", chat_id="777")
    restore_ntfy = ntfy_env()
    tg_rec = TgRecorder(send_ok=True)
    orig_http = bm.tg_http
    bm.tg_http = tg_rec
    transport = NtfyTransport(status=503)
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            assert rc == 0
            assert ch.order[0] == "ntfy", \
                "ntfy is still attempted FIRST even when it will fail, got %r" \
                % (ch.order,)
            assert "email" in ch.order and "telegram" in ch.order, \
                "both fallbacks must fire on a genuine ntfy failure, got %r" \
                % (ch.order,)
            assert ch.order.index("ntfy") < ch.order.index("email"), \
                "ntfy must precede the email fallback, got %r" % (ch.order,)
            assert len(FakeSMTP.sent_subjects) == sent_before + 1
            assert len(tg_rec.sends()) == 1
        finally:
            bm.tg_http = orig_http
            restore()
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_48_fallback_success_does_not_erase_the_primary_failure():
    """ntfy 500 + email OK => sent True, but ntfy_failed is still on record."""
    sd = fresh_dir()
    restore = telegram_env(token=None)
    restore_ntfy = ntfy_env()
    transport = NtfyTransport(status=500)
    with OrderedChannels(transport):
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            assert rc == 0
            row = log_rows(sd)[-1]
            assert row["sent"] is True, \
                "a fallback DID deliver, so sent is true"
            assert "ntfy_failed" in row["reason"], (
                "the primary failure must survive the fallback's success, "
                "reason was %r" % (row["reason"],))
            assert "http_500" in row["reason"], row["reason"]
            assert "ntfy:delivered" not in row["reason"], row["reason"]
        finally:
            restore()
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_49_total_failure_is_never_written_as_sent_true():
    """Primary AND fallbacks all down => sent MUST be False."""
    sd = fresh_dir()
    restore = telegram_env(token="123:ALLFAIL", chat_id="777")
    restore_ntfy = ntfy_env()
    tg_rec = TgRecorder(send_ok="Bad Request: chat not found")
    orig_http = bm.tg_http
    bm.tg_http = tg_rec
    transport = NtfyTransport(status="raise")
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False,
                          smtp=smtp_factory("raise"))
            assert rc == 0
            row = log_rows(sd)[-1]
            assert row["sent"] is False, (
                "every channel failed; sent must NOT be true, got %r"
                % (row.get("sent"),))
            assert "ntfy_failed" in row["reason"], row["reason"]
            assert "smtp_error" in row["reason"], row["reason"]
            # Nothing was alerted, and the retry marker is persisted so a
            # later working run can clear it instead of double-sending.
            active = read_active(sd)
            assert all(not e.get("alerted") for e in active["stories"])
            bstate = read_json(os.path.join(sd, "breaking_state.json"))
            assert len(bstate["pending_alert"]) == 1
            assert "email" in ch.order
            # Telegram is NOT attempted after a failed SMTP send: process_alert
            # returns early there so pending_alert can be persisted for a
            # retry. That is the pre-existing contract (Telegram has always
            # been gated on a successful email), unchanged by ntfy-first —
            # asserted here so the behaviour is on the record, not assumed.
            assert "telegram" not in ch.order, (
                "a failed SMTP send returns early; Telegram is gated on it: %r"
                % (ch.order,))
        finally:
            bm.tg_http = orig_http
            restore()
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_50_an_unverifiable_transport_never_counts_as_success():
    """A transport that returns no checkable status is UNKNOWN, not delivered.

    This is the shape the repo has been burned by: a call that returns without
    raising while nothing was actually sent. Anything that is not an int
    status must be treated as a failure the fallback path acts on.
    """
    for bogus in (None, "", "200 OK", [], {}, object()):
        sd = fresh_dir()
        sent_before = len(FakeSMTP.sent_subjects)
        restore = telegram_env(token=None)
        restore_ntfy = ntfy_env()
        transport = NtfyTransport(status=bogus)
        with OrderedChannels(transport) as ch:
            try:
                rc = run_main(sd, make_fetch(
                    {"feeds.example.test": quake_body()}),
                    runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
                assert rc == 0, repr(bogus)
                assert len(transport.calls) == 1, repr(bogus)
                assert "email" in ch.order, (
                    "a non-status return %r must trigger the fallback, got %r"
                    % (bogus, ch.order))
                assert len(FakeSMTP.sent_subjects) == sent_before + 1, repr(bogus)
                row = log_rows(sd)[-1]
                assert "ntfy:delivered" not in row["reason"], (
                    "%r must never be recorded as delivered: %r"
                    % (bogus, row["reason"]))
                assert "ntfy_failed" in row["reason"], \
                    "%r: %r" % (bogus, row["reason"])
                assert "unverified" in row["reason"], \
                    "%r: %r" % (bogus, row["reason"])
                assert os.environ.get("NTFY_TOPIC") == "dummy-topic", \
                    "the secret NAME is all this module may ever reference"
            finally:
                restore()
                restore_ntfy()
                shutil.rmtree(sd, ignore_errors=True)


def test_51_a_transport_exception_is_recorded_not_swallowed():
    """A raising transport (timeout / connection error) is a real failure."""
    sd = fresh_dir()
    restore = telegram_env(token=None)
    restore_ntfy = ntfy_env()
    transport = NtfyTransport(raises=True)
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            assert rc == 0
            assert "email" in ch.order, \
                "an exception must trigger the fallback, got %r" % (ch.order,)
            row = log_rows(sd)[-1]
            assert "ntfy_failed" in row["reason"], row["reason"]
            assert "unreachable" in row["reason"], \
                "the real cause must survive into the record: %r" % (row["reason"],)
            assert "ntfy:delivered" not in row["reason"]
        finally:
            restore()
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_52_not_configured_is_not_recorded_as_a_delivery_failure():
    """Unset NTFY_* => never attempted, recorded as NOT CONFIGURED, and the
    fallbacks still carry the alert. Unset is not the same claim as "ntfy is
    down", and neither is allowed to pass silently."""
    for topic, server, why in ((None, None, "both unset"),
                               ("dummy-topic", None, "server unset"),
                               (None, TEST_NTFY_SERVER, "topic unset"),
                               ("dummy-topic", "not-a-url",
                                "server is not an http(s) address")):
        sd = fresh_dir()
        sent_before = len(FakeSMTP.sent_subjects)
        restore = telegram_env(token="123:UNCONF", chat_id="777")
        restore_ntfy = ntfy_env(topic=topic, server=server)
        tg_rec = TgRecorder(send_ok=True)
        orig_http = bm.tg_http
        bm.tg_http = tg_rec
        transport = NtfyTransport(status=200)
        with OrderedChannels(transport) as ch:
            try:
                rc = run_main(
                    sd, make_fetch({"feeds.example.test": quake_body()}),
                    runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
                assert rc == 0, why
                assert transport.calls == [], (
                    "%s: with no usable server/topic there must be NO ntfy "
                    "network call" % why)
                assert "ntfy" not in ch.order, (why, ch.order)
                assert len(FakeSMTP.sent_subjects) == sent_before + 1, (
                    "%s: the fallbacks must carry an alert the primary "
                    "cannot take" % why)
                row = log_rows(sd)[-1]
                assert row["sent"] is True, why
                assert "ntfy_not_configured" in row["reason"], \
                    "%s: %r" % (why, row["reason"])
                assert "ntfy_failed" not in row["reason"], (
                    "%s: an unset secret must NOT be logged as ntfy being "
                    "down: %r" % (why, row["reason"]))
                assert "ntfy:delivered" not in row["reason"], \
                    "%s: %r" % (why, row["reason"])
            finally:
                bm.tg_http = orig_http
                restore()
                restore_ntfy()
                shutil.rmtree(sd, ignore_errors=True)


def test_53_the_ai_judge_cannot_control_any_delivery_signal():
    """NO LLM CONTROL OVER HONESTY.

    The breaking path runs through an AI judge, so this is the test that
    matters most for requirement 5: whatever the model puts in the verdict, the
    recorded outcome is decided by the HTTP status alone. A verdict claiming
    "sent" cannot fake a delivery, and a verdict claiming "not urgent" cannot
    suppress the primary attempt.
    """
    sd = fresh_dir()
    restore_ntfy = ntfy_env()
    liar = dict(QUAKE_VERDICT)
    liar.update({
        "sent": True, "delivered": True, "notify": "skip",
        "channels": {"ntfy": "skipped", "email": "not needed"},
        "notification_status": "delivered", "level": 3,
    })
    transport = NtfyTransport(status=500)
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(liar), dry_run=False,
                          smtp=smtp_factory("raise"))
            assert rc == 0
            assert "ntfy" in ch.order, \
                "the model cannot talk the code out of attempting the primary"
            row = log_rows(sd)[-1]
            assert row["sent"] is False, (
                "a model claiming sent=true must never produce sent=true: %r"
                % (row,))
            assert "ntfy_failed" in row["reason"], row["reason"]
            assert "smtp_error" in row["reason"], row["reason"]
            for claim in ("delivered", "not needed"):
                assert claim not in row["reason"], (
                    "model-supplied claim %r leaked into the run record: %r"
                    % (claim, row["reason"]))
        finally:
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)

    # And the mirror case: a model claiming failure cannot suppress delivery.
    sd = fresh_dir()
    restore_ntfy = ntfy_env()
    transport = NtfyTransport(status=200)
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(liar), dry_run=False)
            assert rc == 0
            assert ch.order == ["ntfy"], \
                "no model field may add or remove a channel: %r" % (ch.order,)
        finally:
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_54_ntfy_message_orients_before_it_details():
    """The assembled TITLE and BODY, asserted for content AND for order."""
    body = quake_body()
    sd = fresh_dir()
    restore_ntfy = ntfy_env()
    transport = NtfyTransport(status=200)
    with OrderedChannels(transport):
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": body}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            assert rc == 0
            assert len(transport.calls) == 1
            _url, payload = transport.calls[0]
            title = payload["title"]
            message = payload["message"]
        finally:
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)

    # --- title: short, says what happened, carries a phone emoji -------------
    assert title == ("\U0001f6a8 Breaking: Magnitude 6.8 earthquake near "
                     "Example District"), title
    assert len(title) <= 120, "a collapsed notification truncates: %r" % title
    # Exact list equality on purpose, and NOT a "contains" check: a JSON string
    # "breaking,warning" does not equal this list, so a regression back to the
    # shape ntfy answers with HTTP 400 (code 40024) still fails here. `warning`
    # must remain in the set -- it is what marks the alert as needing attention --
    # and `breaking` is the class that separates this alert from a routine digest
    # on a collapsed notification.
    assert payload["priority"] == 4 and payload["tags"] == ["breaking", "warning"]
    assert payload["click"] == "https://nea.org.np/quake-notice"
    assert payload["topic"] == "dummy-topic"
    # 4096-byte budget is enforced by payload_io, the repo's single owner.
    assert len(json.dumps(payload).encode("utf-8")) <= bm.payload_io.MAX_NTFY_BYTES

    # --- body line 1 is a complete human sentence, not a field dump ---------
    lines = message.split("\n")
    lead = lines[0]
    assert lead.startswith("Confirmed: a magnitude 6.8 earthquake struck "
                           "near Example District."), lead
    assert "Aftershocks possible; check family and structure safety." in lead, \
        "the ORIENTATION line must carry why it matters to the reader: %r" % lead
    assert "•" not in lead, \
        "line 1 must be prose that orients, not a bullet fragment: %r" % lead

    # --- ORIENT BEFORE DETAIL: every diagnostic is strictly after the facts --
    first_fact = message.index("• Category: local_city")
    diagnostics = message.index("Detected via google-news")
    link = message.index("\U0001f517 Read more:")
    assert 0 < first_fact < link < diagnostics, (
        "order must be lead -> facts -> link -> diagnostics; got "
        "facts=%d link=%d diagnostics=%d" % (first_fact, link, diagnostics))
    assert "• Confidence: HIGH" in message
    # The fire time rides on the ntfy alert, same as email/Telegram.
    assert re.search(r"fired \d{4}-\d{2}-\d{2} \d{2}:\d{2} NPT", message), \
        "ntfy alert must state the real NPT fire time: %r" % message
    assert "Detected via google-news · fired " in message


def test_55_critical_ntfy_message_uses_the_detailed_format():
    """A CRITICAL / universal-importance alert: priority 5, What+Impact lead."""
    sd = fresh_dir()
    restore_ntfy = ntfy_env()
    transport = NtfyTransport(status=200)
    universal = dict(QUAKE_VERDICT)
    universal["universal_importance"] = True
    universal["category_suggestion"] = "disaster"
    universal["summary"] = (
        "A magnitude 6.8 earthquake struck near Example District. Tremors were felt "
        "across Example City and damage has been reported in at least two villages.")
    with OrderedChannels(transport):
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(universal), dry_run=False)
            assert rc == 0
            assert len(transport.calls) == 1
            _url, payload = transport.calls[0]
        finally:
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)

    assert payload["priority"] == 5, payload["priority"]
    assert "rotating_light" in payload["tags"], payload["tags"]
    assert payload["title"].startswith("\U0001f6a8 CRITICAL: "), payload["title"]
    assert payload["title"].endswith("— check this now"), payload["title"]
    message = payload["message"]
    lead = message.split("\n")[0]
    assert lead.startswith("A magnitude 6.8 earthquake struck near Example District."), lead
    assert "Damage has been reported" in lead, (
        "the CRITICAL lead must carry the consequence, not just the headline: "
        "%r" % lead)
    assert "• Where:" in message and "• Status:" in message
    assert message.index("Detected via") > message.index("• Status:"), \
        "diagnostics stay last on the CRITICAL path too"


def test_56_dry_run_writes_the_ntfy_preview_and_touches_no_network():
    """--dry-run renders the primary channel's body and posts nothing."""
    sd = fresh_dir()
    restore_ntfy = ntfy_env()
    transport = NtfyTransport(status=200)
    with OrderedChannels(transport) as ch:
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=True)
            assert rc == 0
            assert transport.calls == [], "--dry-run must not post to ntfy"
            assert "ntfy" not in ch.order, ch.order
            path = os.path.join(sd, "ntfy-preview.txt")
            assert os.path.isfile(path), \
                "dry-run must write state/ntfy-preview.txt"
            with open(path, "r", encoding="utf-8") as f:
                preview = f.read()
            assert preview.startswith("TITLE: \U0001f6a8 Breaking:"), preview[:80]
            assert "Confirmed: a magnitude 6.8 earthquake struck near Example District." \
                in preview
            row = log_rows(sd)[-1]
            assert "ntfy:dry_run_preview" in row["reason"], row["reason"]
            assert "ntfy:delivered" not in row["reason"], \
                "a dry run must never claim a delivery"
        finally:
            restore_ntfy()
            shutil.rmtree(sd, ignore_errors=True)


def test_57_the_topic_never_reaches_a_log_or_a_url():
    """The topic is a publish/subscribe capability: it travels in the POST
    body, never in the URL and never into the run's reason."""
    sd = fresh_dir()
    restore = ntfy_env(topic="super-secret-topic-value",
                       server=TEST_NTFY_SERVER)
    transport = NtfyTransport(status=404)
    with OrderedChannels(transport):
        try:
            rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                          runner=verdict_runner(QUAKE_VERDICT), dry_run=False)
            assert rc == 0
            assert len(transport.calls) == 1
            url, payload = transport.calls[0]
            assert "super-secret-topic-value" not in url, \
                "the topic must not be in the URL: %r" % url
            assert url == TEST_NTFY_SERVER + "/", url
            assert payload["topic"] == "super-secret-topic-value"
            row = log_rows(sd)[-1]
            blob = json.dumps(row)
            assert "super-secret-topic-value" not in blob, \
                "the topic leaked into the run record: %r" % blob
        finally:
            restore()
            shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def test_58_chat_id_discovery_prefers_the_private_chat_over_a_newer_group():
    """The defect this closes: "LAST result[].message.chat.id wins" meant a
    group the bot had been added to could take the destination, silently,
    because sendMessage succeeds against any chat the bot can see."""
    bm._TG_CHAT_ID_CACHE.clear()
    rec = TgRecorder(updates={
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 500, "type": "private"}}},
            {"update_id": 2, "message": {"chat": {"id": -100999, "type": "group"}}},
            {"update_id": 3, "message": {"chat": {"id": -100888,
                                                 "type": "supergroup"}}},
        ],
    })
    try:
        assert bm.tg_get_chat_id("T-PRIVATE", http_fn=rec) == 500, \
            "a group update newer than the owner's must not win discovery"
    finally:
        bm._TG_CHAT_ID_CACHE.clear()


def test_59_chat_id_discovery_falls_back_to_newest_of_any_type():
    """Backwards compatibility: when the queue holds no chat with a type at
    all, the previous "newest wins" answer is preserved. A tightened rule that
    broke every existing deployment would trade a silent misdelivery for a
    silent no-delivery."""
    bm._TG_CHAT_ID_CACHE.clear()
    rec = TgRecorder(updates={
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 111}}},
            {"update_id": 2, "message": {"chat": {"id": 4242}}},
        ],
    })
    try:
        assert bm.tg_get_chat_id("T-UNTYPED", http_fn=rec) == 4242
    finally:
        bm._TG_CHAT_ID_CACHE.clear()


def test_60_chat_id_discovery_skips_a_chat_whose_sender_is_a_bot():
    """A bot's own message is not a destination, and a peer bot must never
    become one -- otherwise a single bot-to-bot message redirects every alert."""
    bm._TG_CHAT_ID_CACHE.clear()
    rec = TgRecorder(updates={
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 111, "type": "private"},
                                         "from": {"is_bot": False}}},
            {"update_id": 2, "message": {"chat": {"id": 222, "type": "private"},
                                         "from": {"is_bot": True}}},
        ],
    })
    try:
        assert bm.tg_get_chat_id("T-BOT", http_fn=rec) == 111, \
            "an update sent by a bot must not supply the chat id"
    finally:
        bm._TG_CHAT_ID_CACHE.clear()


def test_61_smtp_factory_sets_a_connect_timeout():
    """smtplib's default is the OS TCP timeout -- minutes. A Gmail that accepts
    the connection and then goes quiet would hang the job with no output and no
    failure, leaving only the Actions job timeout to stop it."""
    captured = {}
    orig = bm.smtplib.SMTP

    class FakeSmtp(object):
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

    bm.smtplib.SMTP = FakeSmtp
    try:
        bm.default_smtp_factory("smtp.gmail.com", 587)
    finally:
        bm.smtplib.SMTP = orig
    assert captured["host"] == "smtp.gmail.com"
    assert captured["port"] == 587
    assert captured["timeout"] == bm.SMTP_TIMEOUT_SEC, \
        "the SMTP factory must pass an explicit timeout"
    assert captured["timeout"] is not None


def test_62_selftest_payload_matches_tg_send_exactly():
    """The self-test must exercise PRODUCTION's request shape. If tg_send ever
    changes what it posts, this comparison goes red instead of the self-test
    quietly testing something production no longer sends."""
    sys.path.insert(0, os.path.join(bm.REPO_ROOT, "scripts"))
    import delivery_selftest as ds
    rec = TgRecorder(send_ok=True)
    bm.tg_send("T-SHAPE", 42, "hello", http_fn=rec)
    _url, production_payload = rec.sends()[0]
    assert ds.telegram_payload(42, "hello") == production_payload, \
        "delivery_selftest's Telegram payload drifted from tg_send's"


def test_63_timeout_evidence_is_captured_redacted_and_kept():
    """A real killed child, not a fabricated exception.

    TimeoutExpired carries whatever the process wrote before the kill; the old
    handler read neither stdout nor stderr, so the run could not say which model
    failed or why. This drives the REAL handler with the REAL evidence a
    timed-out child leaves behind.
    """
    marker = "Z" * 300
    token = "ghp_" + "A" * 36
    child = ("import sys, time\n"
             "sys.stderr.write('auth failed for %s\\n')\n"
             "sys.stderr.write('%s\\n')\n"
             "sys.stderr.flush()\n"
             "time.sleep(5)\n" % (token, marker))
    timed_out = None
    try:
        subprocess.run([sys.executable, "-c", child],
                       capture_output=True, text=True, timeout=0.5)
    except subprocess.TimeoutExpired as exc:
        timed_out = exc
    assert timed_out is not None, "the child was expected to time out"
    assert marker in (timed_out.stderr or ""), (
        "the real child's stderr did not survive the kill, so this test cannot "
        "prove the handler keeps it")

    sd = fresh_dir()
    pending = os.path.join(sd, "breaking_pending.json")
    with open(pending, "w", encoding="utf-8") as fh:
        fh.write("{}")

    def runner(model, prompt_text, env, timeout_sec):
        raise timed_out

    verdict, err = bm.ai_classify(pending, runner=runner, models=["p/dead"],
                                  attempts_per_model=1)
    assert verdict is None
    path = os.path.join(sd, "breaking_attempts.jsonl")
    assert os.path.isfile(path), (
        "no breaking_attempts.jsonl was written: the timeout evidence is still "
        "being discarded")
    with open(path, encoding="utf-8") as fh:
        row = json.loads(fh.readline())
    assert row["model"] == "p/dead", row
    assert row["timeout_sec"] == bm.AI_PER_ATTEMPT_TIMEOUT_SEC, row
    body = json.dumps(row)
    assert marker in body, (
        "the whole stderr must be recorded, not a 160-char tail: %r" % body)
    assert token not in body, "a token was written to the attempt record"
    assert "[REDACTED]" in body, body
    assert token not in err, "a token reached the reason string (and the log row)"
    assert "timed out" in err, err


def test_64_a_model_not_found_is_cooled_not_called_garbage():
    """A dead id is a fact about the ID, not an unusable ANSWER.

    Before this arm `model_not_found` fell through to the unparseable tail: it
    was remembered as `garbage`, never entered the hard cooldown, and every
    later run paid the same window to rediscover that the id does not exist.
    """
    sd = fresh_dir()
    pending = os.path.join(sd, "breaking_pending.json")
    with open(pending, "w", encoding="utf-8") as fh:
        fh.write("{}")

    def runner(model, prompt_text, env, timeout_sec):
        return 1, "", "ProviderModelNotFoundError: Model not found: p/gone"

    verdict, err = bm.ai_classify(pending, runner=runner, models=["p/gone"],
                                  attempts_per_model=1)
    assert verdict is None
    assert "model_not_found" in err, err

    cool_path = bm.model_selector.cooldown_path_for(
        os.path.join(sd, bm.MODEL_HEALTH_FILENAME))
    cool = bm.model_selector.load_cooldown(cool_path)
    assert bm.model_selector.on_hard_cooldown(cool, "p/gone"), (
        "a not-found id must be cooled down, or every later run pays the same "
        "dead rung: %r" % cool)

    health = read_json(os.path.join(sd, bm.MODEL_HEALTH_FILENAME))
    assert health["models"]["p/gone"].get("last_failure") != "garbage", (
        "a dead id was recorded as an unusable ANSWER: %r" % health)


def test_65_the_attempt_record_keeps_the_measured_wall_not_just_the_window():
    """The artifact recorded the window it GRANTED and never how long the
    attempt actually ran, so it could not answer "how close is this rung to the
    150s wall?" -- the one number that sizes the wall. Both are in the row now,
    for a timeout AND for a winning verdict.
    """
    sd = fresh_dir()
    pending = os.path.join(sd, "breaking_pending.json")
    with open(pending, "w", encoding="utf-8") as fh:
        fh.write("{}")

    def slow(model, prompt_text, env, timeout_sec):
        time.sleep(0.2)
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout_sec)

    verdict, _err = bm.ai_classify(pending, runner=slow, models=["p/slow"],
                                   attempts_per_model=1)
    assert verdict is None
    path = os.path.join(sd, "breaking_attempts.jsonl")
    with open(path, encoding="utf-8") as fh:
        row = json.loads(fh.readline())
    assert row["timeout_sec"] == bm.AI_PER_ATTEMPT_TIMEOUT_SEC, row
    assert isinstance(row["wall_sec"], (int, float)), row
    assert 0.15 <= row["wall_sec"] < row["timeout_sec"], (
        "the measured wall must be the real 0.2s spent, not the granted "
        "window: %r" % (row,))

    def answers(model, prompt_text, env, timeout_sec):
        time.sleep(0.05)
        return 0, json.dumps({
            "level": 3, "confidence": "HIGH", "event_key": "e",
            "title": "t", "summary": "s", "why_urgent": "w",
            "sources": [], "category_suggestion": ""}), ""

    verdict, _err = bm.ai_classify(pending, runner=answers, models=["p/ok"],
                                   attempts_per_model=1)
    assert verdict is not None, "the canned verdict was not accepted"
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    won = rows[-1]
    assert won["reason"] == "verdict", won
    assert won["wall_sec"] is not None and won["wall_sec"] < won["timeout_sec"], (
        "a winning attempt's latency is the measurement, so it must be "
        "recorded: %r" % (won,))


def test_66_the_ladder_probe_walks_once_and_cannot_send_judge_or_promote():
    """The probe is the ONLY supported way to prove the four rungs answer.

    Every other suite drives `ai_classify` with a stubbed runner, which proves
    the code, not the ladder: whether the ids are still served, whether a plan
    still allows them, whether an answer makes it back inside the 150s window.
    So the probe has to (a) actually walk, (b) write a durable, redacted answer,
    and (c) be structurally unable to reach the pre-gate, the fast path, dedupe,
    delivery or the honesty-gate log row -- a probe that could send or that
    promoted a rung in the committed health memory would be a monitor action
    wearing a diagnostic's name.
    """
    good = json.dumps({"level": 1, "confidence": "LOW", "event_key": "",
                       "title": "", "summary": "", "why_urgent": "",
                       "escalated_from_previous": False, "sources": [],
                       "category_suggestion": "nepal"})

    def runner(model, prompt_text, env, timeout_sec):
        if "slow" in model:
            raise subprocess.TimeoutExpired(
                cmd="opencode", timeout=timeout_sec,
                output="partial sk-abcdefghijklmnopqrstuvwx")
        return 0, good, ""

    sd = fresh_dir()

    def unreachable(name):
        return unittest.mock.patch.object(
            bm, name,
            side_effect=AssertionError("the probe reached %s" % name))

    with unreachable("pre_gate"), unreachable("hard_evidence_fast_path"), \
            unreachable("collect_candidates"), unreachable("load_sources"), \
            unreachable("process_alert"), \
            unittest.mock.patch.object(bm.story_state, "load_state",
                                       side_effect=AssertionError(
                                           "the probe read the story ledger")), \
            unittest.mock.patch.object(
                bm.model_selector, "select",
                return_value=(["p/slow", "p/fast"], {"source": "probe"})):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bm.main(["--ladder-probe", "--state-dir", sd,
                            "--config-dir", fresh_dir(),
                            "--head-sha", "deadbeef"], ai_runner=runner)

    assert code == 0, ("the probe ran but did not report success once it had "
                       "written its answer")
    doc = read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME))
    assert doc["verdict"] == "parseable", doc
    # No keyed provider config in that empty dir, so the control is a stated
    # absence -- not a failure, and not an attribution that was guessed.
    assert doc["control"] == "unconfigured", doc
    assert doc["attribution"] == "inconclusive", doc
    assert doc["head_sha"] == "deadbeef", doc
    assert doc["per_attempt_sec"] == bm.AI_PER_ATTEMPT_TIMEOUT_SEC, doc
    assert doc["pool"] == list(bm.AI_LADDER), doc
    assert isinstance(doc["ladder_seconds"], (int, float)), doc
    assert [a["model"] for a in doc["attempts"]] == ["p/slow", "p/slow",
                                                     "p/fast"], (
        "the probe must record the REAL walk, one row per attempt, in order: %r"
        % (doc["attempts"],))
    wanted = {"model", "attempt", "granted_sec", "wall_sec", "first_byte_sec",
              "first_byte_source", "first_liveness_sec", "first_liveness_source",
              "outcome", "control", "candidate", "tail_excerpt"}
    for row in doc["attempts"]:
        assert set(row) == wanted, row
        assert row["control"] is False, (
            "a free-rung row must never be labelled as the control: %r" % (row,))
        assert row["candidate"] == "unimportant", (
            "the free walk asks the candidate that must be declined: %r" % (row,))
        assert row["granted_sec"] == bm.AI_PER_ATTEMPT_TIMEOUT_SEC, row
        assert row["first_byte_sec"] is None, (
            "this stub runner cannot measure a first byte, so a number here "
            "would be invented rather than observed: %r" % (row,))
        assert row["first_byte_source"] == "", (
            "an unmeasured timing must not name a stream: %r" % (row,))
        assert row["first_liveness_sec"] is None, (
            "this stub runner never went live, so a liveness number here would "
            "be invented: %r" % (row,))
        assert row["first_liveness_source"] == "", (
            "an unmeasured liveness must not name a stream: %r" % (row,))
    assert doc["attempts"][0]["outcome"] == "timeout", doc["attempts"][0]
    assert doc["attempts"][-1]["outcome"] == "verdict", doc["attempts"][-1]
    assert "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(doc), (
        "a credential-shaped token reached the committed probe file: %s"
        % json.dumps(doc))

    # ISOLATION. Health, cooldown, attempts and the pending payload live in
    # state/ladder_probe/ so the shared memory the NEXT sweep reorders around
    # cannot be touched by a diagnostic.
    assert os.path.isdir(os.path.join(sd, bm.LADDER_PROBE_DIRNAME))
    for name in ("breaking_log.json", "model_health.json", "model_cooldown.json",
                 "active_stories.json", "breaking_attempts.jsonl",
                 "breaking_state.json"):
        assert not os.path.exists(os.path.join(sd, name)), (
            "the probe wrote %r into the SHARED state dir -- it can then be "
            "read as a judged sweep or promote a rung" % name)

    text = out.getvalue()
    assert text.count("ladder_probe: p/") == 3, text
    assert "verdict=parseable" in text, text

    # The payload must be INDISTINGUISHABLE from the one a real sweep writes:
    # same keys, one candidate, a recent `published`. A probe the model can
    # recognise (or one with window_min=0, which reads as "nothing is fresh")
    # would not test the production prompt at all.
    pending = read_json(os.path.join(sd, bm.LADDER_PROBE_DIRNAME,
                                     "breaking_pending.json"))
    assert set(pending) == {"generated_at", "window_min", "pre_gate_matched",
                            "pre_gate_why", "candidates"}, sorted(pending)
    assert pending["window_min"] == 90, pending
    assert len(pending["candidates"]) == 1, pending
    assert pending["candidates"][0]["title"] \
        == bm.LADDER_PROBE_CANDIDATE["title"], pending
    assert pending["candidates"][0].get("published"), pending

    # AN ANSWER THAT DOES NOT PARSE IS ITS OWN VERDICT, not "none": those two
    # mean opposite things when the ladder is being diagnosed.
    bad = fresh_dir()
    with contextlib.redirect_stdout(io.StringIO()):
        bm.run_ladder_probe(bad, runner=lambda m, p, e, t: (0, "no json here", ""),
                            models=["p/garbage"], config_dir=fresh_dir())
    assert read_json(os.path.join(bad, bm.LADDER_PROBE_FILENAME))["verdict"] \
        == "unparseable"

    down = fresh_dir()

    def unreachable_model(model, prompt_text, env, timeout_sec):
        raise OSError("connection reset")

    with contextlib.redirect_stdout(io.StringIO()):
        bm.run_ladder_probe(down, runner=unreachable_model, models=["p/down"],
                            config_dir=fresh_dir())
    assert read_json(os.path.join(down, bm.LADDER_PROBE_FILENAME))["verdict"] \
        == "none"

    # NO RESULT FILE, NO SUCCESS. The file is the evidence; a probe that
    # reported 0 without it would be the same lie the honesty gate exists for.
    real_write = bm.write_json_atomic

    def disk_full(path, payload):
        if path.endswith(bm.LADDER_PROBE_FILENAME):
            raise OSError("disk full")
        return real_write(path, payload)

    with unittest.mock.patch.object(bm, "write_json_atomic", disk_full), \
            contextlib.redirect_stdout(io.StringIO()):
        assert bm.run_ladder_probe(fresh_dir(), runner=runner,
                                   models=["p/fast"],
                                   config_dir=fresh_dir()) == 1

    # AND IT IS UNREACHABLE BY DEFAULT. A default-ON probe would spend the
    # free pool on every scheduled sweep.
    wf = os.path.join(REPO_ROOT, ".github", "workflows",
                      "breaking-monitor.yml")
    with open(wf, encoding="utf-8") as fh:
        src = fh.read()
    block = src.split("ladder_probe:", 1)[1].split("concurrency:", 1)[0]
    assert "type: boolean" in block and "default: false" in block, block
    assert "free quota" in block, (
        "the input must say what it costs, or it will be dispatched casually: %r"
        % block)
    assert 'ARGS="--ladder-probe' in src, "the input never reaches the script"
    assert "inputs.ladder_probe" in src, (
        "the probe flag must come from the dispatch input, not a schedule")


def test_67_a_silent_rung_is_abandoned_at_the_first_output_deadline():
    """A rung that writes only its own banner is abandoned, not paid for.

    The observed probe: four rungs, 150s each, ZERO model output -- every tail
    just `> build - <model>`. Burning the full grant on that silence is what
    made the probe cost 600s and a real sweep lose ten minutes of dead time.
    Abandonment must be honest about what it saw: first_byte_sec stays null
    (nothing was invented) and the banner it did write is kept as evidence.

    THE DEADLINE WATCHES STDERR LIVENESS. This child writes nothing at all on
    stderr, so it never went live and is abandoned -- which is the same verdict
    the old stdout-only deadline reached here, but for the stated reason.
    """
    code = ("import sys,time;"
            "sys.stdout.write('> build - p/silent\\n');sys.stdout.flush();"
            "time.sleep(60)")
    started = time.monotonic()
    try:
        bm._stream_child([sys.executable, "-c", code], "", dict(os.environ),
                         timeout_sec=60, first_output_sec=2)
    except bm.NoFirstOutput as exc:
        waited = time.monotonic() - started
        # Python unbinds `exc` when the block ends, so read it here.
        reported = exc.waited_sec
        seen_stdout = exc.stdout
        first_seen = bm._stream_child.last_first_byte_sec
        live_seen = bm._stream_child.last_first_liveness_sec
    else:
        raise AssertionError(
            "a rung that never went live was allowed to run to its whole 60s "
            "grant; the liveness deadline did not fire")
    assert waited < 10, ("abandonment must be fast, not 'soon': waited %.1fs"
                         % waited)
    assert reported < 10, reported
    assert first_seen is None, (
        "no model byte ever arrived, so a first-output TIME would be invented: "
        "%r" % (first_seen,))
    assert live_seen is None, (
        "nothing was written on stderr, so a liveness TIME would be invented: "
        "%r" % (live_seen,))
    assert "> build" in seen_stdout, (
        "the banner the rung DID write is the evidence for 'stalled, not "
        "failed'; it was thrown away: %r" % (seen_stdout,))


def test_68_a_rung_that_goes_live_at_two_seconds_is_allowed_to_finish():
    """The deadline must not cut a rung that simply takes a few seconds.

    THE SHAPE OF A REAL CHILD: liveness on STDERR early, the answer on STDOUT
    later. With a 4s deadline and an answer at 5s this rung must survive -- the
    deadline is about going live, not about how long the answer takes.
    """
    code = ("import sys,time;"
            "sys.stdout.write('> build - p/fast\\n');sys.stdout.flush();"
            "time.sleep(2);"
            "sys.stderr.write('time=0 level=INFO msg=\"x\" "
            "message=stream model=p/fast\\n');sys.stderr.flush();"
            "time.sleep(3);"
            "sys.stdout.write('{\"level\": 1}\\n');sys.stdout.flush()")
    rc, out, err = bm._stream_child([sys.executable, "-c", code], "",
                                   dict(os.environ), timeout_sec=30,
                                   first_output_sec=4)
    assert rc == 0, rc
    assert '{"level": 1}' in out, (
        "a completed run must still hand the parser its FULL stdout: %r"
        % (out,))
    assert out.startswith("> build"), (
        "the banner is part of the real output; dropping it would change what "
        "the parser sees: %r" % (out,))
    assert bm._stream_child.last_first_liveness_sec is not None, (
        "the rung went live on stderr at 2s, inside the 4s deadline, so it must "
        "have been seen as live")
    assert bm._stream_child.last_first_byte_sec is not None, (
        "the rung DID emit model output, so the first-output time is known "
        "and must be reported")


def test_69_first_byte_time_is_recorded_for_a_slow_but_streaming_rung():
    """`first_byte_sec` is measured, not inferred from the wall clock."""
    code = ("import sys,time;"
            "sys.stdout.write('> build - p/slow\\n');sys.stdout.flush();"
            "sys.stderr.write('time=0 level=INFO msg=\"x\" "
            "message=stream model=p/slow\\n');sys.stderr.flush();"
            "time.sleep(1.5);"
            "sys.stdout.write('{\"level\": 1}\\n');sys.stdout.flush()")
    bm._stream_child([sys.executable, "-c", code], "", dict(os.environ),
                     timeout_sec=30, first_output_sec=10)
    first = bm._stream_child.last_first_byte_sec
    assert first is not None, (
        "a rung that streamed after its banner has a first-output time")
    assert 0.8 <= first <= 6.0, (
        "the first byte arrived after the 1.5s sleep, so the recorded time "
        "must be that moment -- not 0 (the banner) and not the exit: %r"
        % (first,))


def test_70_the_control_rung_is_labelled_and_excluded_from_the_free_verdict():
    """The control answers the attribution question, never the free verdict.

    If the KEYED rung also emits nothing in the same window the harness cannot
    stream and no free-tier tuning matters; if it answers, the free tier is the
    fault. That only holds if the control (a) is a real attempt, (b) is labelled
    in the rows, and (c) cannot change `verdict`, which is about the free rungs.
    """
    good = json.dumps({"level": 1, "confidence": "LOW", "event_key": "",
                       "title": "", "summary": "", "why_urgent": "",
                       "escalated_from_previous": False, "sources": [],
                       "category_suggestion": "nepal"})
    seen_env = {}

    def runner(model, prompt_text, env, timeout_sec):
        seen_env[model] = dict(env)
        if model.startswith("ctrl/"):
            return 0, good, ""
        return 0, "no json here", ""

    cfgdir = fresh_dir()
    with open(os.path.join(cfgdir, "opencode-cli.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"provider": {"ctrl": {
            "options": {"apiKey": "{env:LADDER_PROBE_TEST_KEY}"},
            "models": {"cheap-1": {"name": "Cheap One"}}}}}, fh)
    sd = fresh_dir()
    with unittest.mock.patch.dict(os.environ,
                                  {"LADDER_PROBE_TEST_KEY": "not-a-real-key"}):
        with contextlib.redirect_stdout(io.StringIO()):
            code = bm.run_ladder_probe(sd, runner=runner, models=["free/one"],
                                       config_dir=cfgdir)
    assert code == 0, code
    doc = read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME))

    control_rows = [a for a in doc["attempts"] if a.get("control")]
    free_rows = [a for a in doc["attempts"] if not a.get("control")]
    # TWO walks of the same keyed rung, one per fixed candidate -- the pair is
    # the measurement now, so the control is no longer a single row.
    assert [r["model"] for r in control_rows] == ["ctrl/cheap-1",
                                                  "ctrl/cheap-1"], doc["attempts"]
    assert sorted(r["candidate"] for r in control_rows) == ["important",
                                                            "unimportant"], (
        "each keyed row must say which fixed candidate it answered: %r"
        % (control_rows,))
    assert [r["model"] for r in free_rows] == ["free/one"], doc["attempts"]
    assert doc["control"] == "answered", doc
    assert doc["control_model"] == "ctrl/cheap-1", doc
    assert "not-a-real-key" not in json.dumps(doc), (
        "the credential VALUE reached the committed file: %s" % json.dumps(doc))
    assert seen_env["ctrl/cheap-1"].get("LADDER_PROBE_TEST_KEY") == \
        "not-a-real-key", (
            "the control attempt was not given the credential at all, so it "
            "could only ever fail")
    assert seen_env["ctrl/cheap-1"].get("OPENCODE_CONFIG", "").endswith(
        "opencode-cli.json"), (
        "the keyed provider only exists for the CLI when its config file is "
        "the one loaded: %r" % (seen_env["ctrl/cheap-1"].get("OPENCODE_CONFIG"),))
    assert doc["verdict"] == "unparseable", (
        "a control that ANSWERED must not turn the FREE rungs' garbage into a "
        "pass: %r" % (doc["verdict"],))

    # No credential configured => a stated state, never a failed probe.
    bare = fresh_dir()
    with unittest.mock.patch.dict(os.environ,
                                  {"LADDER_PROBE_TEST_KEY": ""}):
        with contextlib.redirect_stdout(io.StringIO()):
            assert bm.run_ladder_probe(bare, runner=runner,
                                       models=["free/one"],
                                       config_dir=cfgdir) == 0
    bare_doc = read_json(os.path.join(bare, bm.LADDER_PROBE_FILENAME))
    assert bare_doc["control"] == "unconfigured", bare_doc
    assert not [a for a in bare_doc["attempts"] if a.get("control")], (
        "an unconfigured control has no row: there was no attempt")


def test_71_every_attribution_value_comes_from_its_stated_condition():
    """Exactly one of four values, by a rule that is stated, not implied."""
    silent = [{"first_byte_sec": None, "first_liveness_sec": None}]
    streamed = [{"first_byte_sec": 5.0, "first_liveness_sec": 1.0}]
    # LIVE BUT UNANSWERED: the call went out and no answer came back. That is
    # evidence the harness can stream, and it is NOT the harness's fault, so
    # `harness_no_stream` must not be reachable from here.
    live_only = [{"first_byte_sec": None, "first_liveness_sec": 2.0}]
    # A REFUSAL IS NOT SILENCE. The row could only carry the plan's notice by
    # reading stderr, so the harness demonstrably streams.
    noticed = [{"first_byte_sec": None, "first_liveness_sec": None,
                "outcome": "quota_notice"}]
    cases = [
        ("no_output", silent, "harness_no_stream"),
        ("failed", silent, "inconclusive"),
        ("unconfigured", silent, "inconclusive"),
        ("answered", silent, "free_tier_stalled"),
        ("answered", streamed, "free_tier_answered"),
        ("unparseable", streamed, "free_tier_answered"),
        ("no_output", streamed, "free_tier_answered"),
        ("ANSWERED", silent, "inconclusive"),
        # The new signal. A free rung that went live proves the harness can see
        # a stream, so a silent CONTROL can no longer indict the harness.
        ("no_output", live_only, "inconclusive"),
        ("unconfigured", live_only, "inconclusive"),
        ("answered", live_only, "free_tier_stalled"),
        ("unparseable", live_only, "free_tier_stalled"),
        # NO CONTROL MEANS NO ATTRIBUTION, whatever the free rungs did: that
        # precedence is unchanged by the liveness rework, and `verdict` is what
        # records a free answer in that case.
        ("unconfigured", streamed, "inconclusive"),
        # A free rung that carried the plan's own refusal notice proves the
        # stderr stream WAS read, so the harness cannot be indicted for it.
        ("no_output", noticed, "free_tier_stalled"),
    ]
    for control, rows, want in cases:
        got = bm._probe_attribution(control, rows)
        assert got == want, (
            "control=%r free=%r must be %r by the stated rule, got %r"
            % (control, rows, want, got))
    for value in ("harness_no_stream", "free_tier_stalled",
                  "free_tier_answered", "inconclusive"):
        assert value in bm.LADDER_PROBE_ATTRIBUTIONS, (
            "%r is not in the declared vocabulary" % value)


def test_72_the_silent_probe_is_bounded_by_the_new_arithmetic():
    """The 600s probe becomes a few minutes, and the arithmetic says so."""
    assert bm.AI_FIRST_OUTPUT_SEC == 40, (
        "the first-output deadline is the number this whole task turns on: %r"
        % (bm.AI_FIRST_OUTPUT_SEC,))
    silent_bound = (bm.AI_POOL_SIZE * bm.AI_FIRST_OUTPUT_SEC
                    + bm.AI_FIRST_OUTPUT_SEC)
    assert silent_bound <= 200, (
        "four silent free rungs plus a silent control must fit under 200s, not "
        "%ds" % silent_bound)
    assert silent_bound < bm.AI_POOL_SIZE * bm.AI_PER_ATTEMPT_TIMEOUT_SEC, (
        "the deadline must actually reduce the worst case it was added for")

    # AND IT IS BEHAVIOURAL: a stalled rung is not re-asked. A second attempt
    # buys the same silence and doubles the dead time.
    sd = fresh_dir()

    def stalled(model, prompt_text, env, timeout_sec):
        raise bm.NoFirstOutput(bm.AI_FIRST_OUTPUT_SEC, "> build - %s" % model, "")

    with contextlib.redirect_stdout(io.StringIO()):
        assert bm.run_ladder_probe(sd, runner=stalled,
                                   models=["p/a", "p/b"], config_dir=fresh_dir()) == 0
    doc = read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME))
    free = [a for a in doc["attempts"] if not a.get("control")]
    assert [a["model"] for a in free] == ["p/a", "p/b"], free
    assert all(a["attempt"] == 1 for a in free), (
        "a stalled rung must not be retried inside its window: %r" % (free,))
    assert all(a["outcome"] == "no_output" for a in free), free
    assert all(a["first_byte_sec"] is None for a in free), free
    assert doc["verdict"] == "none", doc


def test_73_a_rung_that_goes_live_on_stderr_is_not_abandoned():
    """THE DEFECT THIS TASK EXISTS TO FIX.

    `opencode run` writes its narration to STDERR and the assistant message to
    STDOUT exactly once, at completion (measured 2026-11-02: one stdout line at
    37.57s, the banner on stderr at 9.00s). A deadline that watches stdout alone
    therefore kills a child that is answering normally, and records the corpse
    as `no_output` -- which is what the last probe did to five rungs.
    """
    line = ("timestamp=2026-11-02T10:00:01.000Z level=INFO run=abc "
            "message=stream providerID=opencode modelID=p/live")
    code = ("import sys,time;"
            "sys.stdout.write('> build - p/live\\n');sys.stdout.flush();"
            "sys.stderr.write(%r);sys.stderr.flush();"
            "time.sleep(3);"
            "sys.stdout.write('{\"level\": 1}\\n');sys.stdout.flush()"
            % (line + "\n"))
    rc, out, err = bm._stream_child([sys.executable, "-c", code], "",
                                    dict(os.environ), timeout_sec=30,
                                    first_output_sec=1)
    assert rc == 0, ("a rung that proved it was alive on stderr was killed at "
                     "the stdout deadline: rc=%r out=%r err=%r"
                     % (rc, out[:200], err[:200]))
    assert '{"level": 1}' in out, out
    live = bm._stream_child.last_first_liveness_sec
    assert live is not None, (
        "the stderr `message=stream` line was never recognised as liveness, so "
        "the deadline still watches the wrong stream: %r" % (live,))
    assert live < 2, ("liveness arrived well before the 3s answer; recorded %r"
                      % (live,))
    byte = bm._stream_child.last_first_byte_sec
    assert byte is not None and byte >= 2.5, (
        "`first_byte_sec` must still mean the first STDOUT byte (the answer), "
        "not the log line: %r" % (byte,))


def test_74_a_rung_silent_on_both_streams_is_still_abandoned():
    """The deadline must still fire: a boot line is not proof the call went out."""
    line = "timestamp=2026-11-02T10:00:00.500Z level=INFO run=abc message=init"
    code = ("import sys,time;"
            "sys.stderr.write(%r);sys.stderr.flush();"
            "time.sleep(60)" % (line + "\n"))
    started = time.monotonic()
    try:
        bm._stream_child([sys.executable, "-c", code], "", dict(os.environ),
                         timeout_sec=60, first_output_sec=2)
    except bm.NoFirstOutput as exc:
        waited = time.monotonic() - started
        reported = exc.waited_sec
    else:
        raise AssertionError(
            "a rung that only printed a startup line ran to its whole 60s "
            "grant; the deadline no longer fires at all")
    assert waited < 10 and reported < 10, (waited, reported)
    assert bm._stream_child.last_first_liveness_sec is None, (
        "a startup line is not the main model call opening, so there is no "
        "liveness time to report: %r"
        % (bm._stream_child.last_first_liveness_sec,))
    assert bm._stream_child.last_first_byte_sec is None


def test_75_a_liveness_only_row_is_accepted_and_explains_itself():
    """Null `first_byte_sec` beside a present `first_liveness_sec` must read as
    one fact, not as a contradiction: the call went out, no answer came back."""
    good = json.dumps({"level": 1, "confidence": "LOW", "event_key": "",
                       "title": "", "summary": "", "why_urgent": "",
                       "escalated_from_previous": False, "sources": [],
                       "category_suggestion": "nepal"})

    def runner(model, prompt_text, env, timeout_sec):
        # Live at 2s, answered at 9s -- but this stub cannot measure stdout, so
        # the byte stays unmeasured rather than being approximated.
        runner.last_first_liveness_sec = 2.0
        return 0, good, ""

    runner.last_first_liveness_sec = None
    runner.last_first_byte_sec = None
    sd = fresh_dir()
    with contextlib.redirect_stdout(io.StringIO()):
        assert bm.run_ladder_probe(sd, runner=runner, models=["p/live"],
                                   config_dir=fresh_dir()) == 0
    doc = read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME))
    free = [a for a in doc["attempts"] if not a.get("control")]
    assert len(free) == 1, doc["attempts"]
    row = free[0]
    assert row["first_byte_sec"] is None, row
    assert row["first_liveness_sec"] == 2.0, row
    assert row["first_liveness_source"] == "stderr", (
        "the file must say WHICH stream produced each time: %r" % (row,))
    assert row["first_byte_source"] == "", row
    assert "message=stream" in doc["liveness_signal"], (
        "the committed file must state what counted as liveness: %r"
        % (doc.get("liveness_signal"),))
    assert doc["verdict"] == "parseable", doc
    assert doc["attribution"] in bm.LADDER_PROBE_ATTRIBUTIONS, doc


def test_76_the_verdict_is_parsed_from_stdout_only():
    """stderr carries the CLI's log and must never become the answer."""
    real = json.dumps({"level": 3, "confidence": "HIGH", "event_key": "k",
                       "title": "t", "summary": "s", "why_urgent": "w",
                       "escalated_from_previous": False, "sources": [],
                       "category_suggestion": "nepal"})
    decoy = json.dumps({"level": 1, "confidence": "LOW", "event_key": "decoy",
                        "title": "decoy", "summary": "", "why_urgent": "",
                        "escalated_from_previous": False, "sources": [],
                        "category_suggestion": "nepal"})

    def runner(model, prompt_text, env, timeout_sec):
        return 0, real, "level=INFO message=stream\n" + decoy

    sd = fresh_dir()
    pending = os.path.join(sd, "breaking_pending.json")
    with open(pending, "w", encoding="utf-8") as fh:
        json.dump({"candidates": []}, fh)
    with contextlib.redirect_stdout(io.StringIO()):
        verdict, err = bm.ai_classify(pending, runner=runner, models=["p/one"])
    assert verdict is not None, err
    assert verdict.get("level") == 3, (
        "a JSON object on STDERR was allowed to answer for the model: %r"
        % (verdict,))

    def stderr_only(model, prompt_text, env, timeout_sec):
        return 0, "> build - p/one\n", "level=INFO message=stream\n" + real

    with contextlib.redirect_stdout(io.StringIO()):
        verdict, err = bm.ai_classify(pending, runner=stderr_only,
                                      models=["p/one"], attempts_per_model=1)
    assert verdict is None, (
        "an answer found only on stderr was accepted; the parser must read "
        "stdout alone: %r" % (verdict,))


def test_77_a_live_rung_that_is_slow_gets_its_whole_grant():
    """The 40s deadline measures SILENCE, never a slow answer."""
    line = ("timestamp=2026-11-02T10:00:01.000Z level=INFO run=abc "
            "message=stream providerID=opencode modelID=p/slow")
    code = ("import sys,time;"
            "sys.stderr.write(%r);sys.stderr.flush();"
            "time.sleep(6)" % (line + "\n"))
    try:
        bm._stream_child([sys.executable, "-c", code], "", dict(os.environ),
                         timeout_sec=2, first_output_sec=1)
    except bm.subprocess.TimeoutExpired as exc:
        assert exc.timeout == 2, (
            "a live rung must be bounded by its GRANT, not the 40s deadline: "
            "%r" % (exc.timeout,))
    else:
        raise AssertionError("the child outlived its own 2s grant")
    assert bm._stream_child.last_first_liveness_sec is not None, (
        "the grant killed a rung that had gone live, and the liveness time was "
        "not recorded, so the two deaths cannot be told apart")
    assert bm._stream_child.last_first_byte_sec is None


def test_78_the_breaking_child_asks_the_cli_for_its_log():
    """Without `--print-logs --log-level INFO` there is no liveness to read."""
    seen = {}

    def fake_stream(argv, prompt_text, env, timeout_sec, first_output_sec=None):
        seen["argv"] = list(argv)
        seen["deadline"] = first_output_sec
        return 0, "{}", ""

    fake_stream.last_first_byte_sec = None
    fake_stream.last_first_liveness_sec = None
    real_which, real_stream = bm.shutil.which, bm._stream_child
    bm.shutil.which = lambda name: "opencode"
    bm._stream_child = fake_stream
    try:
        bm.default_ai_runner("p/one", "prompt", {}, 10)
    finally:
        bm.shutil.which, bm._stream_child = real_which, real_stream
    argv = seen["argv"]
    assert "--print-logs" in argv, (
        "the child was not asked to print its own log, so stderr carries no "
        "liveness and the deadline is back to watching stdout: %r" % (argv,))
    assert "--log-level" in argv, argv
    assert argv[argv.index("--log-level") + 1] == "INFO", (
        "INFO is the level that carries `message=stream`; DEBUG is unnecessary "
        "volume: %r" % (argv,))
    assert seen["deadline"] == bm.AI_FIRST_OUTPUT_SEC, seen


def test_79_the_keyed_rung_leads_the_ladder_when_its_credential_is_present():
    """THE FREE POOL IS EXHAUSTED; THE KEYED RUNG IS A REAL RUNG, TRIED FIRST.

    Measured 2026-11-02: a keyed rung answered the probe in 5.49s while all
    four free rungs produced zero bytes in 150s and carried the CLI's
    "subscribe to OpenCode Go" notice. No free-tier tuning can fix that, so the
    order a walk actually tries rungs in is the fix.

    The order is NOT a second list: it is config/opencode.json's ladder, and
    the pool is its head. What changed is that the keyed provider is now
    REACHABLE for this process (OPENCODE_CONFIG declares it and its credential
    is present), so its leading rungs survive the reachability filter instead
    of being dropped at import.
    """
    with open(os.path.join(REPO_ROOT, "config", "opencode.json"),
              encoding="utf-8") as fh:
        ladder = json.load(fh)["ladder"]

    both = bm._ladder_for(ladder, {"opencode/", "gemini/"})
    assert both[:2] == ("gemini/gemini-3.5-flash-lite",
                        "gemini/gemini-3.1-flash-lite"), both
    pool = both[:bm.AI_POOL_SIZE]
    assert len(pool) == bm.AI_POOL_SIZE, pool
    assert pool[0].startswith("gemini/"), (
        "a working rung must be tried FIRST while the free pool is exhausted; "
        "the pool leads with %r" % (pool[0],))
    assert "opencode/deepseek-v4.1-flash-free" in pool, (
        "the free rungs must stay in the table even while exhausted: %r" % (pool,))

    # WITHOUT the keyed provider this CLI can resolve, the same filter yields
    # the free pool in its own order -- the graceful degradation, unchanged.
    only_free = bm._ladder_for(ladder, {"opencode/"})
    assert only_free[0].startswith("opencode/"), only_free
    assert "gemini/" not in "".join(only_free), only_free

    # And the module's own ladder is exactly config + whatever THIS process
    # can resolve -- never a third opinion.
    assert tuple(bm.AI_MODELS) == bm._ladder_for(ladder, bm._reachable_prefixes())


def test_80_a_quota_notice_is_its_own_class_not_a_timeout_or_garbage():
    """PLAN EXHAUSTION IS NOT SLOWNESS, and the state file must say so.

    The notice is the CLI's own wording. It OVERLAPS quota ("usage limit"), so
    the payment arm has to win, and it must not collapse into `timeout` (the
    full window was never used) or `garbage` (the model never answered).
    """
    ms = bm.model_selector
    notice = ("level=ERROR message=Free usage limit reached. To keep going, "
              "subscribe to OpenCode Go to keep going.")
    assert ms.classify_failure(notice, 0) == "payment_required", (
        "the plan notice must keep its own class, got %r"
        % (ms.classify_failure(notice, 0),))
    assert ms.classify_failure(ms.WATCHDOG_MARKER, 0) == "slow_or_stalled"
    assert ms.classify_failure(notice, 0) != ms.classify_failure(
        ms.WATCHDOG_MARKER, 0), "a quota notice is not a timeout"
    assert ms.classify_failure(notice, 0) != ms.classify_failure(
        "rc=1 and nothing useful", 1), "a quota notice is not garbage"
    assert ms.classify_failure("rc=1 and nothing useful", 1) == "other"

    assert "quota_notice" in ms.FAILURE_KINDS, (
        "health memory must be able to record the class it is given: %r"
        % (ms.FAILURE_KINDS,))
    assert ms.HEALTH_KIND_OF["payment_required"] == "quota_notice", (
        "a plan refusal recorded as `transport` is a lie about why the rung "
        "failed, and a reader cannot tell a dead pool from a slow one: %r"
        % (ms.HEALTH_KIND_OF["payment_required"],))
    assert ms.HEALTH_KIND_OF["slow_or_stalled"] == "timeout", (
        "the timeout kind stays its own kind: %r"
        % (ms.HEALTH_KIND_OF["slow_or_stalled"],))

    row = ms.record_failure(ms.load_health(None), "free/a", "payment_required")
    assert row["quota_notice"] == 1, row
    assert row["timeout"] == 0 and row["transport"] == 0, row
    assert row["last_failure"] == "quota_notice", row


def test_81_a_quota_notice_before_liveness_is_recorded_and_stops_the_provider():
    """THE NOTICE ARRIVES WITHOUT A LIVENESS LINE, so the no-output arm must
    read the stderr it captured instead of calling it a slow rung.

    Before this, a refused free pool was recorded as `slow_or_stalled` and
    every rung of the provider was asked in turn -- 40s each -- to hear the
    same paragraph again.
    """
    notice = ("level=ERROR message=Free usage limit reached. To keep going, "
              "subscribe to OpenCode Go to keep going.")
    sd = fresh_dir()
    pending = os.path.join(sd, "breaking_pending.json")
    with open(pending, "w", encoding="utf-8") as fh:
        fh.write("{}")

    calls = []

    def refused(model, prompt_text, env, timeout_sec):
        calls.append(model)
        raise bm.NoFirstOutput(1.0, "", notice)

    with contextlib.redirect_stdout(io.StringIO()):
        verdict, err = bm.ai_classify(pending, runner=refused,
                                      models=["free/a", "free/b", "free/c"],
                                      attempts_per_model=2)
    assert verdict is None
    assert "quota_notice" in err, err

    health = read_json(os.path.join(sd, bm.MODEL_HEALTH_FILENAME))
    row = health["models"]["free/a"]
    assert row.get("quota_notice") == 1, (
        "the refusal was not recorded as a quota notice: %r" % (row,))
    assert row.get("timeout") == 0 and row.get("transport") == 0, row
    assert row.get("last_failure") == "quota_notice", row

    with open(os.path.join(sd, "breaking_attempts.jsonl"), encoding="utf-8") as fh:
        reasons = [json.loads(line)["reason"] for line in fh if line.strip()]
    assert reasons and reasons[0] == "quota_notice", reasons

    assert calls == ["free/a", "free/b"], (
        "the provider is refused, not these two ids: after the second notice "
        "the remaining rungs must be skipped, got %r" % (calls,))


def test_82_a_non_probe_run_cannot_commit_the_probe_file():
    """state/ladder_probe.json WAS CLOBBERED 36s AFTER THE PROBE WROTE IT.

    A concurrent run whose checkout still contained the committed file added
    it to its own FILES list and rewrote it with stale content. The file is a
    PROBE's result; a run that did not probe has nothing to say about it.
    """
    yml = os.path.join(REPO_ROOT, ".github", "workflows",
                       "breaking-monitor.yml")
    with open(yml, encoding="utf-8") as fh:
        text = fh.read()
    step = text.split("- name: Commit breaking state", 1)[1]
    step = step.split("- name:", 1)[0]
    # THE SHAPE CHANGED ON 2026-11-03 and this pin follows it: the probe used to
    # APPEND its file to a shared list and now REPLACES the list with it, which
    # is the stronger rule (a probe commits only its own result). What must not
    # change is that the probe file is still committed -- and only by a probe.
    assert "FILES=(state/ladder_probe.json)" in step, (
        "the probe result is no longer committed at all, or a probe still "
        "stages the whole state list:\n%s" % step)
    assert 'if [ "$LADDER_PROBE" = "true" ]' in step, (
        "the FILES list is not gated on the probe flag, so any run present in "
        "the step commits whatever copy its checkout happens to hold:\n%s"
        % step)
    assert "LADDER_PROBE:" in step, (
        "the step never learns whether it is a probe run")

    script = os.path.join(REPO_ROOT, "scripts", "commit_state.sh")
    with open(script, encoding="utf-8") as fh:
        body = fh.read()
    assert "state/ladder_probe.json" in body and "LADDER_PROBE" in body, (
        "the staging helper must refuse the probe file on its own, so a future "
        "caller cannot re-introduce the clobber")

    # BEHAVIOURAL, not just textual: a real repository with the file present
    # and modified commits NOTHING when the run is not a probe.
    bash = shutil.which("bash")
    if not bash and os.path.isfile(r"C:\Program Files\Git\bin\bash.exe"):
        bash = r"C:\Program Files\Git\bin\bash.exe"
    assert bash, "bash is required for this test; a skip would render green"
    tmp = fresh_dir()
    git = ["git", "-C", tmp]
    for argv in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(git + argv, check=True, capture_output=True)
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    probe = os.path.join(tmp, "state", "ladder_probe.json")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write('{"run_at": "stale"}\n')
    subprocess.run(git + ["add", "state/ladder_probe.json"], check=True,
                   capture_output=True)
    subprocess.run(git + ["commit", "-qm", "seed"], check=True,
                   capture_output=True)
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write('{"run_at": "rewritten by a non-probe run"}\n')
    env = dict(os.environ, LADDER_PROBE="false", GITHUB_TOKEN="")
    proc = subprocess.run([bash, script, "chore: state",
                           "state/ladder_probe.json"],
                          cwd=tmp, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = subprocess.run(git + ["log", "--oneline"], capture_output=True,
                         text=True).stdout.strip().splitlines()
    assert len(log) == 1, (
        "a non-probe run committed the probe file: %r (%s)"
                         % (log, proc.stdout + proc.stderr))


def _bash_for_commit_state():
    """The bash this machine actually has. A skip would render the test green."""
    bash = shutil.which("bash")
    if not bash and os.path.isfile(r"C:\Program Files\Git\bin\bash.exe"):
        bash = r"C:\Program Files\Git\bin\bash.exe"
    assert bash, "bash is required for this test; a skip would render green"
    return bash


def _put(path, body):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)


def _dirty_state_repo(paths):
    """A real clone with a bare remote, every path committed then DIRTY.

    The remote body is `remote-<name>` and the working copy is
    `this-run-<name>`, so a staging bug shows up as the WRONG BODY on the branch
    rather than as a missing file -- which is how the 2026-11-03 revert read
    (`a7c7a48ce`: 0 additions, 9 deletions).
    """
    bash = _bash_for_commit_state()
    tmp = fresh_dir()
    remote = os.path.join(tmp, "remote.git")
    work = os.path.join(tmp, "work")
    os.makedirs(work)
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True,
                   capture_output=True)
    git = ["git", "-C", work]
    for argv in (["init", "-q"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"],
                 ["symbolic-ref", "HEAD", "refs/heads/master"],
                 ["remote", "add", "origin", remote]):
        subprocess.run(git + argv, check=True, capture_output=True)
    os.makedirs(os.path.join(work, "state"), exist_ok=True)
    for rel in paths:
        _put(os.path.join(work, rel), "remote-%s\n" % os.path.basename(rel))
    subprocess.run(git + ["add", "--"] + list(paths), check=True,
                   capture_output=True)
    subprocess.run(git + ["commit", "-qm", "seed"], check=True,
                   capture_output=True)
    subprocess.run(git + ["push", "-q", "-u", "origin", "HEAD"], check=True,
                   capture_output=True)
    for rel in paths:
        _put(os.path.join(work, rel), "this-run-%s\n" % os.path.basename(rel))
    return bash, remote, work


def _remote_body(remote, rel):
    return subprocess.run(["git", "--git-dir", remote, "show",
                           "master:%s" % rel],
                          capture_output=True, text=True).stdout


def _remote_touched(remote):
    out = subprocess.run(["git", "--git-dir", remote, "show", "--name-only",
                          "--format=", "master"],
                         capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _run_commit(work, bash, *args, **env_over):
    env = dict(os.environ, GITHUB_TOKEN="", GITHUB_REF_NAME="master")
    env.pop("LADDER_PROBE", None)
    env.update(env_over)
    return subprocess.run(
        [bash, os.path.join(REPO_ROOT, "scripts", "commit_state.sh")] + list(args),
        cwd=work, env=env, capture_output=True, text=True)


def test_83_a_probe_run_commits_only_its_own_result():
    """A PROBE RE-APPLIED ITS WHOLE FILE LIST OVER THE FRESH TIP.

    On 2026-11-03 the probe's commit step put its own checkout copies of every
    state file on top of the freshly-fetched branch: commit `a7c7a48ce` (0
    additions, 9 deletions) deleted a concurrent run's gemini success block from
    `state/model_health.json`, and `state/alert_ledger.json` went back to
    `healthy_streak: 0` with a stale `last_seen`. A probe decides nothing, so it
    may commit ONE path: its own result. Asserted against a real bare remote,
    because the damage was done to the BRANCH, not to a working tree.
    """
    probe = "state/ladder_probe.json"
    health = "state/model_health.json"
    ledger = "state/alert_ledger.json"
    stories = "state/active_stories.json"
    bash, remote, work = _dirty_state_repo((probe, health, ledger, stories))
    proc = _run_commit(work, bash, "chore: ladder probe [skip ci]",
                       health, ledger, probe, stories, LADDER_PROBE="true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _remote_touched(remote) == [probe], (
        "a probe run committed more than its own result: %r -- re-applying "
        "those files over the fresh tip is what reverted a concurrent run's "
        "state" % (_remote_touched(remote),))
    assert _remote_body(remote, probe) == "this-run-ladder_probe.json\n", (
        "the probe's own result is the one thing it MUST commit")
    for rel in (health, ledger, stories):
        assert _remote_body(remote, rel) == "remote-%s\n" % os.path.basename(rel), (
            "the probe's commit step published its own copy of %r" % rel)
        with open(os.path.join(work, rel), encoding="utf-8") as fh:
            assert fh.read() == "this-run-%s\n" % os.path.basename(rel), (
                "a refusal must drop the PATH, never discard the file: %r" % rel)


def test_84_the_refused_paths_are_named_in_the_output():
    """REFUSING SILENTLY IS THE SAME AS LOSING IT QUIETLY.

    A path dropped from a probe's commit has to say so: otherwise the next
    reader sees a run whose state was never published and no reason for it.
    """
    probe = "state/ladder_probe.json"
    health = "state/model_health.json"
    ledger = "state/alert_ledger.json"
    bash, remote, work = _dirty_state_repo((probe, health, ledger))
    before = subprocess.run(["git", "--git-dir", remote, "rev-parse", "master"],
                            capture_output=True, text=True).stdout.strip()
    assert before, "the seed commit never reached the bare remote"
    proc = _run_commit(work, bash, "chore: ladder probe [skip ci]",
                       health, ledger, LADDER_PROBE="true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # STDOUT only: git writes its own CRLF warnings to stderr, and a warning
    # containing the path is not a refusal naming the path.
    text = proc.stdout
    for rel in (health, ledger):
        lines = [ln for ln in text.splitlines() if rel in ln]
        assert lines, ("the refused path %r is never named, so a reader cannot "
                       "tell why it was not published:\n%s" % (rel, text))
        assert "probe" in lines[0].lower(), (
            "the refusal must state WHY, not just the path: %r" % lines[0])
    assert "state/ladder_probe.json" in text and "only" in text.lower(), (
        "the output must say a probe may commit only its own result:\n%s" % text)
    after = subprocess.run(["git", "--git-dir", remote, "rev-parse", "master"],
                           capture_output=True, text=True).stdout.strip()
    assert after == before, "a probe run with no result file still wrote a commit"


def test_85_a_non_probe_run_still_commits_its_own_state():
    """THE PROBE'S RULE MUST NOT LEAK INTO THE SWEEP.

    Every scheduled run commits its own health/story state; only a probe is
    restricted. A guard that froze the real monitor's state would trade one
    silent defect for a worse one.
    """
    probe = "state/ladder_probe.json"
    health = "state/model_health.json"
    stories = "state/active_stories.json"
    bash, remote, work = _dirty_state_repo((probe, health, stories))
    proc = _run_commit(work, bash, "chore: update breaking monitor state [skip ci]",
                       health, stories, probe)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    touched = _remote_touched(remote)
    assert set(touched) == {health, stories}, (
        "a non-probe run must still publish its own state and only that: %r"
        % (touched,))
    assert _remote_body(remote, health) == "this-run-model_health.json\n", (
        "the sweep's own health rows are exactly what a run exists to publish")
    assert _remote_body(remote, probe) == "remote-ladder_probe.json\n", (
        "a run that did not probe still committed the probe file")


def test_86_the_probe_candidate_is_realistic_and_non_empty():
    """AN EMPTY PAYLOAD IS NOT A TEST OF THE CLASSIFIER.

    The probe's whole claim is "the rungs can judge"; that needs a candidate a
    real sweep could plausibly deliver -- a headline, a snippet naming two
    newsrooms, a source and a URL -- and it must stay clearly NON-alerting, so a
    level-3 answer is the model over-escalating rather than a news event. No
    death toll and no digits: a count would trip the deterministic fast path,
    which is a different mechanism from the classifier under test.
    """
    cand = bm.LADDER_PROBE_CANDIDATE
    for key in ("title", "url", "source", "snippet", "domain_category"):
        assert isinstance(cand.get(key), str) and cand[key].strip(), key
    assert len(cand["title"]) >= 30, cand["title"]
    assert len(cand["snippet"]) >= 60, cand["snippet"]
    assert re.match(r"^https://[a-z0-9.-]+\.example/", cand["url"]), cand["url"]
    text = "%s %s" % (cand["title"], cand["snippet"])
    assert not re.search(r"\d", text), (
        "a number in the probe's input could trip the death-count fast path: %r"
        % text)
    assert not re.search(r"(?i)\b(dead|deaths|killed|casualt|injured|earthquake)\b",
                         text), text
    outlets = re.findall(r"\b(?:Herald|Post|Times|Gazette|Journal|Chronicle)\b",
                         cand["snippet"])
    assert len(outlets) >= 2, (
        "a single anonymous outlet is not corroboration, and the real feed is "
        "full of multi-outlet stories: %r" % cand["snippet"])
    payload = bm._ladder_probe_pending()
    assert len(payload["candidates"]) == 1, payload
    sent = payload["candidates"][0]
    for key in ("title", "url", "source", "snippet", "domain_category"):
        assert sent[key] == cand[key], (key, sent)
    assert sent.get("published"), sent

    # THE IMPORTANT HALF MUST BE JUST AS REAL AND JUST AS SAFE. It is the story
    # that has to be judged, so it has to read like something a sweep could
    # deliver -- and it must not be able to trip the death-count fast path or
    # name a real outlet either.
    important = bm.LADDER_PROBE_IMPORTANT_CANDIDATE
    for key in ("title", "url", "source", "snippet", "domain_category"):
        assert isinstance(important.get(key), str) and important[key].strip(), key
    assert important["domain_category"] == "nepal", important
    assert len(important["title"]) >= 30, important["title"]
    assert re.match(r"^https://[a-z0-9.-]+\.example/", important["url"]), \
        important["url"]
    itext = "%s %s" % (important["title"], important["snippet"])
    assert not re.search(r"\d", itext), (
        "a number in the important candidate could trip the death-count fast "
        "path: %r" % itext)
    assert not re.search(r"(?i)\b(dead|deaths|killed|casualt|injur|earthquake)\b",
                         itext), itext
    assert len(re.findall(r"\b(?:Herald|Post|Times|Gazette|Journal|Chronicle)\b",
                          important["snippet"])) >= 2, important["snippet"]
    assert important["title"] != cand["title"]
    ipayload = bm._ladder_probe_pending(candidate=important)
    assert ipayload["candidates"][0]["title"] == important["title"], ipayload


def _minimal_verdict():
    return json.dumps({"level": 1, "confidence": "LOW", "event_key": "",
                       "title": "", "summary": "", "why_urgent": "",
                       "escalated_from_previous": False, "sources": [],
                       "category_suggestion": "nepal"})


def test_87_an_empty_judgment_is_recorded_as_judged_content_false():
    """A SCHEMA-VALID EMPTY OBJECT IS NOT PROOF THE MODEL CAN JUDGE.

    Measured 2026-11-03: the probe's verdict was
    `{"level":1,...,"title":"","summary":""}` -- valid, and indistinguishable
    from a model that returned the prompt's "nothing qualifies" template without
    reading anything. The file must say which it got.
    """
    sd = fresh_dir()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = bm.run_ladder_probe(sd, runner=lambda m, p, e, t: (0, _minimal_verdict(), ""),
                                   models=["p/empty"], config_dir=fresh_dir())
    assert code == 0
    doc = read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME))
    assert doc["verdict"] == "parseable", doc
    assert doc["judged_content"] is False, (
        "an empty judgment was recorded as content: %r" % (doc,))
    assert doc["control_judged_content"] is False, doc
    rule = doc["judged_rule"]
    assert "title" in rule and "summary" in rule, (
        "the file must state how judged_content is derived: %r" % (rule,))
    text = out.getvalue()
    assert "judged_content=no" in text, text
    assert "judged nothing" in text.lower(), (
        "an empty judgment must be reported as judging nothing, never as a "
        "successful judgment: %s" % text)


def test_88_a_real_verdict_is_recorded_as_judged_content_true():
    """THE FIELD MUST BE ABLE TO SAY YES, or it only ever measures emptiness."""
    real = json.dumps({"level": 2, "confidence": "MEDIUM",
                       "event_key": "water-supply-cut",
                       "title": "City water utility announces a two-day supply cut",
                       "summary": "Reported: the utility will interrupt supply to "
                                  "four wards while a trunk main is repaired.",
                       "why_urgent": "Households in four wards should store water.",
                       "escalated_from_previous": False,
                       "sources": [{"name": "The Valley Herald",
                                    "url": "https://valleyherald.example/a"}],
                       "category_suggestion": "local_city"})
    sd = fresh_dir()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = bm.run_ladder_probe(sd, runner=lambda m, p, e, t: (0, real, ""),
                                   models=["p/real"], config_dir=fresh_dir())
    assert code == 0
    doc = read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME))
    assert doc["judged_content"] is True, (
        "a verdict that names an event and summarises it was recorded as "
        "judging nothing: %r" % (doc,))
    assert "judged_content=yes" in out.getvalue(), out.getvalue()


def _keyed_probe_config(env_name="LADDER_PROBE_TEST_KEY"):
    """A CLI config declaring one keyed provider, and the env var holding it."""
    cfgdir = fresh_dir()
    with open(os.path.join(cfgdir, "opencode-cli.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"provider": {"ctrl": {
            "options": {"apiKey": "{env:%s}" % env_name},
            "models": {"cheap-1": {"name": "Cheap One"}}}}}, fh)
    return cfgdir, env_name


def _important_answer():
    return json.dumps({"level": 2, "confidence": "HIGH",
                       "event_key": "carrier-grounds-fleet",
                       "title": "National carrier grounds its international fleet",
                       "summary": "Reported: the airline suspended international "
                                  "flights while engineers review maintenance "
                                  "records, and passengers are being rebooked.",
                       "why_urgent": "Anyone with an international booking must "
                                     "rebook before departure.",
                       "escalated_from_previous": False,
                       "sources": [{"name": "The Northgate Journal",
                                    "url": "https://northgatejournal.example/a"}],
                       "category_suggestion": "nepal"})


def _pair_runner(calls, judge_important=True, judge_unimportant=False):
    """A stub that answers each candidate the way the goal says it must be
    answered: the important one with a real verdict, the unimportant one with
    the minimal object."""
    def runner(model, prompt_text, env, timeout_sec):
        with open(env["CANDIDATES_PATH"], encoding="utf-8") as fh:
            pending = json.load(fh)
        title = pending["candidates"][0]["title"]
        calls.append((env["CANDIDATES_PATH"], title))
        if title == bm.LADDER_PROBE_IMPORTANT_CANDIDATE["title"]:
            return 0, (_important_answer() if judge_important
                       else _minimal_verdict()), ""
        return 0, (_important_answer() if judge_unimportant
                   else _minimal_verdict()), ""
    return runner


def _run_pair_probe(judge_important=True, judge_unimportant=False):
    cfgdir, env_name = _keyed_probe_config()
    sd = fresh_dir()
    calls = []
    out = io.StringIO()
    with unittest.mock.patch.dict(os.environ, {env_name: "not-a-real-key"}):
        with contextlib.redirect_stdout(out):
            code = bm.run_ladder_probe(
                sd, runner=_pair_runner(calls, judge_important,
                                        judge_unimportant),
                models=["free/one"], config_dir=cfgdir)
    assert code == 0, code
    return read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME)), out.getvalue(), calls


def test_89_a_decline_is_not_reported_as_a_failure_of_capability():
    """A DECLINE IS A CORRECT ANSWER TO THE UNIMPORTANT STORY.

    The prompt prescribes the minimal object both for "nothing qualifies" and for
    "I cannot read this", so a decline can only ever be read as one half of a
    pair. When the important story IS judged, the unimportant decline must appear
    as required behaviour -- not as a failed half, and not as a capability result
    on its own.
    """
    doc, text, calls = _run_pair_probe(judge_important=True,
                                       judge_unimportant=False)
    assert doc["judgment_measured"] is True, doc
    assert doc["candidates"]["important"]["judged_content"] is True, doc
    assert doc["candidates"]["unimportant"]["judged_content"] is False, (
        "the unimportant candidate must be declined, and that must be recorded "
        "as a decline rather than left blank: %r" % (doc,))
    assert doc["judgment_failed"] == [], (
        "a decline of the story that MUST be declined was counted as a failed "
        "half: %r" % (doc.get("judgment_failed"),))
    assert doc["judgment_capable"] is True, doc
    assert "judged_content=no" in text, (
        "the decline must be visible in the run log, not hidden: %s" % text)

    # A DECLINE ALONE PROVES NOTHING: with no keyed rung the pair was never
    # asked, which is recorded as unmeasured rather than as "cannot judge".
    sd = fresh_dir()
    with contextlib.redirect_stdout(io.StringIO()):
        bm.run_ladder_probe(sd, runner=lambda m, p, e, t: (0, _minimal_verdict(), ""),
                            models=["free/one"], config_dir=fresh_dir())
    bare = read_json(os.path.join(sd, bm.LADDER_PROBE_FILENAME))
    assert bare["judgment_measured"] is False, bare
    assert bare["judgment_capable"] is False, bare
    assert "not measured" in bare["judgment_reason"].lower(), bare


def test_90_a_judged_important_candidate_makes_the_probe_capable():
    """THE PAIR, BOTH HALVES AS REQUIRED, IS THE ONLY `true`.

    Two walks of the leading keyed rung, one fixed candidate each, and each
    candidate reaches the model as the ONLY candidate in its payload -- so the
    answer is about that story and nothing else.
    """
    doc, text, calls = _run_pair_probe(judge_important=True,
                                       judge_unimportant=False)
    assert doc["judgment_capable"] is True, (
        "an important story judged AND an unimportant story declined is the one "
        "shape that shows capability: %r" % (doc,))
    assert doc["judgment_failed"] == [], doc
    titles = [title for _path, title in calls]
    assert bm.LADDER_PROBE_IMPORTANT_CANDIDATE["title"] in titles, (
        "the important candidate was never asked: %r" % (titles,))
    assert bm.LADDER_PROBE_CANDIDATE["title"] in titles, (
        "the unimportant candidate was never asked: %r" % (titles,))
    assert len(set(titles)) == 2, (
        "the pair must be two different stories, not one asked twice: %r"
        % (titles,))
    # TWO walks of the keyed rung, and the free walk: never four.
    keyed = [a for a in doc["attempts"] if a.get("control")]
    assert len(keyed) == 2, (
        "one attempt per candidate is the whole cost of the pair: %r" % (keyed,))
    assert sorted(a["candidate"] for a in keyed) == ["important", "unimportant"]
    assert len({a["attempt"] for a in keyed}) == 1, (
        "a second walk is a second QUESTION, never a retry of the first: %r"
        % (keyed,))
    assert "judgment_capable=yes" in text, text


def test_91_judging_the_unimportant_candidate_is_a_failed_half():
    """A RUNG THAT SUMMARISES EVERYTHING IS NOT DISCRIMINATING.

    Both judged means the rung answers but cannot tell an important story from
    an unimportant one, so capability is false and the failing half is named.
    """
    doc, text, _calls = _run_pair_probe(judge_important=True,
                                        judge_unimportant=True)
    assert doc["judgment_capable"] is False, doc
    assert doc["judgment_failed"] == ["unimportant"], (
        "the failing half must be the unimportant one, by name: %r"
        % (doc.get("judgment_failed"),))
    assert "unimportant half" in doc["judgment_reason"], doc["judgment_reason"]
    assert "judgment_capable=no" in text and "unimportant half" in text, text


def test_92_a_decline_of_the_important_candidate_names_the_failing_half():
    """THE HARD CASE: BOTH DECLINED LOOKS EXACTLY LIKE "CANNOT JUDGE".

    Measured 2026-11-03: two different candidates both came back as the minimal
    object. That is a decline by a rung that can judge and a failure by a rung
    that cannot, and the file must say which half's failure produced the verdict
    rather than reporting a bare false.
    """
    doc, text, _calls = _run_pair_probe(judge_important=False,
                                        judge_unimportant=False)
    assert doc["judgment_capable"] is False, doc
    assert doc["judgment_failed"] == ["important"], (
        "both halves declined, so the failing half is the important one: %r"
        % (doc.get("judgment_failed"),))
    assert "important half" in doc["judgment_reason"], doc["judgment_reason"]
    assert "declined" in doc["judgment_reason"], doc["judgment_reason"]
    assert "important half" in text, (
        "the run log must name the failing half, not print a bare false: %s"
        % text)
    # AND THE DECLINE OF THE UNIMPORTANT HALF IS STILL NOT A FAILURE there.
    assert "unimportant half" not in doc["judgment_reason"], (
        "a required decline was reported as a failed half: %r"
        % doc["judgment_reason"])


def test_93_the_two_candidate_probe_still_commits_only_its_own_file():
    """THE NEW PAIR MUST NOT RE-OPEN THE CLOBBER.

    Two walks write into `state/ladder_probe/`, and the result still lands in
    exactly one tracked path: `state/ladder_probe.json`. Asserted against a real
    bare remote, with a dirty health file present, because that is the shape that
    reverted a concurrent run's state in `a7c7a48ce`.
    """
    probe = "state/ladder_probe.json"
    health = "state/model_health.json"
    bash, remote, work = _dirty_state_repo((probe, health))
    # The real result of a real two-candidate probe run, not a stub file.
    doc, _text, _calls = _run_pair_probe()
    _put(os.path.join(work, probe), json.dumps(doc, indent=2, sort_keys=True))
    proc = _run_commit(work, bash, "chore: ladder probe [skip ci]",
                       health, probe, LADDER_PROBE="true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _remote_touched(remote) == [probe], (
        "the two-candidate probe committed more than its own result: %r"
        % (_remote_touched(remote),))
    assert _remote_body(remote, health) == "remote-model_health.json\n", (
        "the probe published its own copy of a concurrent run's health state")


def _probe_summary_body():
    """The workflow's probe-summary heredoc, dedented to column zero."""
    path = os.path.join(REPO_ROOT, ".github", "workflows",
                        "breaking-monitor.yml")
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    opener = re.search(r"python3 - <<'PROBE_PY'\s*\n", raw)
    assert opener, "the probe summary is no longer a python3 heredoc"
    rest = raw[opener.end():]
    close = re.search(r"\n[ \t]*PROBE_PY[ \t]*\n", rest)
    assert close, "the probe-summary heredoc has no PROBE_PY terminator"
    lines = rest[:close.start()].split("\n")
    first = next((ln for ln in lines if ln.strip()), "")
    prefix = first[:len(first) - len(first.lstrip())]
    return "\n".join(ln[len(prefix):] if ln.startswith(prefix) else ln
                     for ln in lines)


def _run_probe_summary(doc):
    """Execute the real summary step against a real probe result file."""
    body = _probe_summary_body()
    work = fresh_dir()
    os.makedirs(os.path.join(work, "state"), exist_ok=True)
    with open(os.path.join(work, "state", "ladder_probe.json"), "w",
              encoding="utf-8") as fh:
        json.dump(doc, fh)
    out = io.StringIO()
    here = os.getcwd()
    try:
        os.chdir(work)
        with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            with contextlib.redirect_stdout(out):
                exec(compile(body, "<PROBE_PY>", "exec"),
                     {"__name__": "__main__"})
    finally:
        os.chdir(here)
    return out.getvalue()


def test_94_the_judged_candidate_keeps_the_answer_it_gave():
    """A BOOLEAN CANNOT BE AUDITED. The important story's own words are kept.

    `judgment_capable: true` rests on the model having written a title and a
    summary for a story nobody could see after the fact. The result file must
    carry that text, or the claim is only as good as our memory of the run.
    """
    doc, text, _calls = _run_pair_probe()
    rec = doc["candidates"]["important"]
    answer = rec["verdict"]
    assert rec["judged_content"] is True, rec
    assert answer["title"] == ("National carrier grounds its international "
                               "fleet"), (
        "the judged candidate's title was not persisted: %r" % (answer,))
    assert answer["summary"].startswith("Reported: the airline suspended"), (
        "the judged candidate's summary was not persisted: %r" % (answer,))
    assert answer["level"] == 2 and answer["confidence"] == "HIGH", answer
    assert answer["why_urgent"], (
        "the judge's reason is part of the answer and must survive too: %r"
        % (answer,))
    assert answer["category_suggestion"] == "nepal", answer
    assert answer["event_key"] == "carrier-grounds-fleet", answer
    # THE FILE MUST STAY READABLE THROUGH THE CONTENTS API: a few KB, not a log.
    raw = json.dumps(doc)
    assert len(raw) < 8000, (
        "the probe result grew past a few KB (%d chars), which makes it "
        "unreadable through the raw-contents API" % len(raw))
    assert answer["title"] in text and answer["summary"] in text, (
        "the run log must carry the answer too: %s" % text)
    # REDACTED LIKE EVERY OTHER COMMITTED SINK: a model that echoes a token into
    # its headline must not carry it into the file or the log.
    secret = bm._probe_verdict({"level": 2, "confidence": "HIGH",
                                "title": "quoted ghp_" + "a" * 24,
                                "summary": "Bearer " + "b" * 24})
    assert "[REDACTED]" in secret["title"] and "[REDACTED]" in secret["summary"], (
        "a credential-shaped answer text reached the committed shape: %r"
        % (secret,))


def test_95_the_declined_candidate_keeps_the_empty_object():
    """A DECLINE IS PERSISTED AS THE EMPTY OBJECT, NOT AS OMITTED TEXT.

    The prompt's "nothing qualifies" answer is level 1 with an empty title and
    summary. Recording that verbatim is what lets a reader tell a decline from a
    field that was silently dropped by a later edit.
    """
    doc, text, _calls = _run_pair_probe(judge_important=True,
                                        judge_unimportant=False)
    rec = doc["candidates"]["unimportant"]
    answer = rec["verdict"]
    assert rec["judged_content"] is False, rec
    assert answer["level"] == 1 and answer["confidence"] == "LOW", answer
    assert answer["title"] == "" and answer["summary"] == "", (
        "a decline must stay empty, never filled in: %r" % (answer,))
    assert answer["why_urgent"] == "", answer
    assert "candidate=unimportant title: (empty)" in text, (
        "the log must show the decline as empty rather than hide it: %s" % text)


def test_96_the_summary_step_prints_the_important_answer():
    """A RUN LOG WITHOUT THE TEXT IS NOT SELF-SUFFICIENT.

    Executed against the step's real heredoc, not a copy: the summary must print
    the important candidate's level, confidence, title and summary, because the
    JSON file is what a later reader needs but the log is what a run leaves.
    """
    doc, _text, _calls = _run_pair_probe()
    out = _run_probe_summary(doc)
    answer = doc["candidates"]["important"]["verdict"]
    assert "Important answer: level=2, confidence=HIGH" in out, out
    assert answer["title"] in out, (
        "the summary step does not print the important title:\n%s" % out)
    assert answer["summary"] in out, (
        "the summary step does not print the important summary:\n%s" % out)
    # AND A DECLINE IS PRINTED AS EMPTY, never as a blank that looks like a bug.
    declined = dict(doc)
    declined["candidates"] = dict(doc["candidates"])
    declined["candidates"]["important"] = dict(
        doc["candidates"]["unimportant"], label="important", expect="judged")
    out = _run_probe_summary(declined)
    assert "Important answer: level=1" in out, out
    assert "  * title: (empty)" in out, out


# ---------------------------------------------------------------------------
# 97-103. Quiet hours: hold a non-critical alert overnight, deliver ONE labelled
#         batch when quiet hours end, and never hold a genuine critical alert.
# ---------------------------------------------------------------------------

# Nepal is UTC+05:45 all year (no DST), so a wall clock and an instant are two
# different facts and both are written down here: 23:00 NPT = 17:15Z, 06:59 NPT
# = 01:14Z, 07:00 NPT = 01:15Z.
NPT = timezone(timedelta(hours=5, minutes=45))


def npt(day, hour, minute=0):
    """The aware NPT instant for a 2026-11-<day> HH:MM Nepal wall clock."""
    return datetime(2026, 11, day, hour, minute, tzinfo=NPT)


# The same verified earthquake, with an EMPTY why_urgent: nothing about this
# item is a "check this now" fact, so quiet hours may hold it.
NON_CRITICAL_VERDICT = dict(QUAKE_VERDICT, why_urgent="")


def quiet_queue_path(state_dir):
    return os.path.join(state_dir, "quiet_queue.json")


def test_97_a_non_critical_level3_at_2300_npt_is_held_not_sent():
    sd = fresh_dir()
    restore = ntfy_env()
    transport = NtfyTransport()
    real_http = bm.ntfy_http
    bm.ntfy_http = transport
    try:
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(NON_CRITICAL_VERDICT),
                      dry_run=False, now_utc=npt(3, 23, 0))
        assert rc == 0, "a held alert is a successful run, got %s" % rc
        assert transport.calls == [], \
            "23:00 NPT is the owner's sleep window; nothing may be pushed"
        rows = log_rows(sd)
        assert len(rows) == 1, rows
        assert rows[0]["sent"] is False, rows[0]
        assert rows[0]["reason"].startswith(
            "quiet hours, held 1 item(s) until 07:00 NPT"), \
            "the log row must state the reason: %r" % rows[0]["reason"]
        doc = read_json(quiet_queue_path(sd))
        assert len(doc["items"]) == 1, doc
        assert doc["items"][0]["raised_npt"] == "2026-11-03 23:00", doc
        assert doc["items"][0]["title"] == NON_CRITICAL_VERDICT["title"], doc
        # HELD IS NOT DROPPED: the story is marked alerted, so the 07:0x run
        # dedupes against it instead of notifying it a second time later.
        story = read_active(sd)["stories"][0]
        assert story["alerted"] is True, story
    finally:
        bm.ntfy_http = real_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_98_a_non_critical_level3_at_0659_npt_is_still_held():
    sd = fresh_dir()
    restore = ntfy_env()
    transport = NtfyTransport()
    real_http = bm.ntfy_http
    bm.ntfy_http = transport
    try:
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(NON_CRITICAL_VERDICT),
                      dry_run=False, now_utc=npt(4, 6, 59))
        assert rc == 0
        assert transport.calls == [], \
            "06:59 NPT is still inside quiet hours (they end at 07:00)"
        doc = read_json(quiet_queue_path(sd))
        assert len(doc["items"]) == 1, doc
        assert doc["items"][0]["raised_npt"] == "2026-11-04 06:59", doc
    finally:
        bm.ntfy_http = real_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_99_the_held_batch_is_delivered_once_when_quiet_hours_end():
    sd = fresh_dir()
    restore = ntfy_env()
    transport = NtfyTransport()
    real_http = bm.ntfy_http
    bm.ntfy_http = transport
    try:
        run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                 runner=verdict_runner(NON_CRITICAL_VERDICT),
                 dry_run=False, now_utc=npt(3, 23, 0))
        assert transport.calls == [], "still held at 23:00"

        # 07:00 the next morning. The same story is deduped, and the held batch
        # goes out in ONE message.
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(NON_CRITICAL_VERDICT),
                      dry_run=False, now_utc=npt(4, 7, 0))
        assert rc == 0
        assert len(transport.calls) == 1, \
            "the batch is one message, not one per held item: %d" % len(
                transport.calls)
        payload = transport.bodies()[0]
        assert "Held overnight" in payload["title"], payload["title"]
        assert "2026-11-03 23:00" in payload["message"], payload["message"]
        assert NON_CRITICAL_VERDICT["title"] in payload["message"], payload
        assert read_json(quiet_queue_path(sd))["items"] == [], \
            "the queue is cleared only after the batch was delivered"

        # ... and ONCE. A later run the same morning must not deliver it again.
        run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                 runner=verdict_runner(NON_CRITICAL_VERDICT),
                 dry_run=False, now_utc=npt(4, 7, 30))
        assert len(transport.calls) == 1, \
            "a delivered batch must never be delivered a second time"
    finally:
        bm.ntfy_http = real_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_100_the_critical_definition_is_exactly_two_shapes():
    fast = dict(QUAKE_VERDICT, hard_evidence_fast_path=True)
    # The two shapes that may wake the owner at 23:00.
    assert bm.is_critical_alert(fast, 3) is True, \
        "a mass-casualty hard-evidence alert must never be held"
    assert bm.is_critical_alert(QUAKE_VERDICT, 3) is True, \
        "a verified level-3 with a why_urgent must never be held"
    # Everything else is held, and each of these is a way a future edit could
    # widen the definition. Changing behaviour here must fail this test.
    assert bm.is_critical_alert(NON_CRITICAL_VERDICT, 3) is False, \
        "a level-3 WITHOUT a why_urgent is not a critical alert"
    assert bm.is_critical_alert(dict(QUAKE_VERDICT, why_urgent="   "),
                                3) is False, \
        "whitespace is not a why_urgent"
    assert bm.is_critical_alert(QUAKE_VERDICT, 2) is False, \
        "only level 3 can be critical"
    assert bm.is_critical_alert(fast, 2) is False, \
        "the fast-path flag cannot promote a level-2 verdict to critical"
    assert bm.is_critical_alert(dict(QUAKE_VERDICT, level=2), 2) is False


def test_101_a_critical_alert_at_2300_npt_is_sent_immediately():
    sd = fresh_dir()
    restore = ntfy_env()
    transport = NtfyTransport()
    real_http = bm.ntfy_http
    bm.ntfy_http = transport
    try:
        rc = run_main(sd, make_fetch({"feeds.example.test": quake_body()}),
                      runner=verdict_runner(QUAKE_VERDICT),
                      dry_run=False, now_utc=npt(3, 23, 0))
        assert rc == 0
        assert len(transport.calls) == 1, \
            "a genuine critical alert must NOT wait for morning"
        assert not os.path.isfile(quiet_queue_path(sd)) or \
            read_json(quiet_queue_path(sd))["items"] == [], \
            "a sent alert must not also be queued"
        assert log_rows(sd)[0]["sent"] is True
    finally:
        bm.ntfy_http = real_http
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_102_the_queue_is_capped_and_the_batch_says_so():
    sd = fresh_dir()
    path = quiet_queue_path(sd)
    transport = NtfyTransport()
    restore = ntfy_env()
    try:
        for i in range(alert_dedupe.QUIET_QUEUE_CAP + 3):
            alert_dedupe.enqueue_quiet(path, alert_dedupe.quiet_item(
                "Held item %02d" % i, "body %02d" % i,
                condition="breaking.alert:%02d" % i, kind="breaking",
                now=npt(3, 23, i % 60)))
        doc = alert_dedupe.load_quiet_queue(path)
        assert len(doc["items"]) == alert_dedupe.QUIET_QUEUE_CAP, doc
        assert doc["dropped"] == 3, doc
        assert alert_dedupe.QUIET_QUEUE_CAP > 0, "the cap is a named constant"

        note = bm.flush_quiet_queue({"quiet_queue": path},
                                    now_utc=npt(4, 7, 0),
                                    http_fn=transport)
        assert note, "a non-empty queue must report what it did"
        assert len(transport.calls) == 1, transport.calls
        message = transport.bodies()[0]["message"]
        assert "queue cap %d" % alert_dedupe.QUIET_QUEUE_CAP in message, \
            "the batch must state the cap it hit: %s" % message[:400]
        assert "3 oldest" in message, message[:400]
        dropped_line = [line for line in message.splitlines()
                        if "oldest" in line]
        assert dropped_line and "Held item 00" in dropped_line[0], \
            "the dropped items must be named as dropped, not passed off as " \
            "delivered: %r" % (dropped_line,)
        assert read_json(path)["items"] == []
    finally:
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_103_a_held_recovery_is_superseded_by_a_later_failure():
    """A recovery held at 23:00 and a re-failure at 05:00 must not contradict.

    The morning batch carries the LATEST state of that condition, not both an
    "it recovered" and an "it is broken" for the same thing.
    """
    sd = fresh_dir()
    path = quiet_queue_path(sd)
    transport = NtfyTransport()
    restore = ntfy_env()
    try:
        alert_dedupe.enqueue_quiet(path, alert_dedupe.quiet_item(
            "The breaking honesty gate (AI ladder) recovered",
            "The condition that was alerting has stopped occurring.",
            condition="breaking.honesty_gate", kind="honesty_gate_recovery",
            now=npt(3, 23, 10)))
        alert_dedupe.enqueue_quiet(path, alert_dedupe.quiet_item(
            "Breaking honesty gate FAILED — breaking news was not judged",
            "The AI ladder returned no verdict, so this run decided nothing.",
            condition="breaking.honesty_gate", kind="honesty_gate",
            now=npt(4, 5, 0)))

        doc = alert_dedupe.load_quiet_queue(path)
        assert len(doc["items"]) == 1, \
            "one condition holds one item: %r" % (doc["items"],)
        assert doc["superseded"] == 1, doc
        assert doc["items"][0]["kind"] == "honesty_gate"

        bm.flush_quiet_queue({"quiet_queue": path}, now_utc=npt(4, 7, 0),
                             http_fn=transport)
        message = transport.bodies()[0]["message"]
        assert "FAILED" in message, message
        assert "recovered" not in message.lower(), \
            "the batch must not claim a recovery that a later failure revoked"
        assert "superseded" in message.lower(), \
            "the owner must be told an earlier held item was replaced"
    finally:
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_104_a_canary_failure_at_0615_npt_is_held_and_rides_the_0700_batch():
    """A drifting parser is not critical: 06:15 NPT holds it, 07:00 delivers it."""
    sd = fresh_dir()
    path = os.path.join(sd, "state", "quiet_queue.json")
    restore = ntfy_env()
    saved = os.environ.get("ALERT_DEDUPE_NOW_UTC")
    here = os.getcwd()
    pushed = []

    def _forbidden(*args, **kwargs):
        pushed.append(args)
        raise AssertionError("nothing may be pushed inside quiet hours")

    try:
        os.chdir(sd)
        os.environ["ALERT_DEDUPE_NOW_UTC"] = npt(4, 6, 15).astimezone(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with unittest.mock.patch("urllib.request.urlopen", _forbidden):
            sent = live_canary.notify_ntfy(
                "live-parser-canary FAILED (strike 2)", "a parser drifted",
                priority=4, dry_run=False)
        assert sent is False, "a held canary notice is not a send"
        assert pushed == [], pushed
        doc = alert_dedupe.load_quiet_queue(
            os.path.join(sd, "state", "quiet_queue.json"))
        assert len(doc["items"]) == 1, doc
        assert doc["items"][0]["kind"] == "canary", doc["items"][0]
        assert doc["items"][0]["condition"] == "canary.live_parser", doc
        assert doc["items"][0]["raised_npt"] == "2026-11-04 06:15", doc

        delivered = []

        def recorder(url, payload):
            delivered.append(payload)
            return 200, "{}"

        note = bm.flush_quiet_queue({"quiet_queue": path},
                                    now_utc=npt(4, 7, 0), http_fn=recorder)
        assert "delivered 1" in note, note
        assert len(delivered) == 1, delivered
        assert "strike 2" in delivered[0]["message"], delivered[0]
        assert "2026-11-04 06:15" in delivered[0]["message"], delivered[0]
        assert alert_dedupe.load_quiet_queue(path)["items"] == []
    finally:
        os.chdir(here)
        if saved is None:
            os.environ.pop("ALERT_DEDUPE_NOW_UTC", None)
        else:
            os.environ["ALERT_DEDUPE_NOW_UTC"] = saved
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_105_a_watchdog_failure_at_2145_notifies_and_one_at_2205_is_held():
    """The window is the owner's clock, not a round UTC hour."""
    sd = fresh_dir()
    path = quiet_queue_path(sd)
    try:
        early = alert_dedupe.hold_if_quiet(
            "digest_watchdog", "Worker heartbeat failed", "detail",
            "digest_watchdog.worker_heartbeat", priority=5,
            now=npt(3, 21, 45), path=path)
        assert early is None, "21:45 NPT is before the window: push normally"
        assert alert_dedupe.load_quiet_queue(path)["items"] == []

        late = alert_dedupe.hold_if_quiet(
            "digest_watchdog", "Worker heartbeat failed", "detail",
            "digest_watchdog.worker_heartbeat", priority=5,
            now=npt(3, 22, 5), path=path)
        assert late and late.startswith("quiet hours, held 1 item(s)"), late
        items = alert_dedupe.load_quiet_queue(path)["items"]
        assert len(items) == 1 and items[0]["raised_npt"] == "2026-11-03 22:05"
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_106_only_one_flush_site_exists_and_a_second_caller_is_refused():
    """Two deliverers would push the same held items twice and clear each other."""
    sd = fresh_dir()
    path = quiet_queue_path(sd)
    sent = []
    try:
        alert_dedupe.enqueue_quiet(path, alert_dedupe.quiet_item(
            "A held notice", "body", condition="canary.live_parser",
            kind="canary", now=npt(4, 6, 15)))
        refused = bm.flush_quiet_queue(
            {"quiet_queue": path}, now_utc=npt(4, 7, 0),
            http_fn=lambda url, payload: (sent.append(payload), (200, "{}"))[1],
            caller="digest_watchdog")
        assert refused.startswith("quiet_flush_refused"), refused
        assert sent == [], "a refused caller must not send anything"
        assert len(alert_dedupe.load_quiet_queue(path)["items"]) == 1

        owners = []
        for root, _dirs, files in os.walk(SCRIPTS):
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(root, name), "r",
                          encoding="utf-8") as fh:
                    if "def flush_quiet_queue" in fh.read():
                        owners.append(name)
        assert owners == ["breaking_monitor.py"], owners
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_107_a_scheduled_run_and_a_manual_dispatch_cannot_both_deliver():
    """An overlapping run must find the lease taken, not deliver a second copy."""
    sd = fresh_dir()
    path = quiet_queue_path(sd)
    lease = os.path.join(sd, "state", "quiet_queue.lease")
    sent = []
    here = os.getcwd()
    restore = ntfy_env()
    try:
        os.chdir(sd)
        os.makedirs(os.path.join(sd, "state"))
        alert_dedupe.enqueue_quiet(path, alert_dedupe.quiet_item(
            "A held notice", "body", condition="canary.live_parser",
            kind="canary", now=npt(4, 6, 15)))
        assert alert_dedupe.claim_flush(npt(4, 7, 0), path=lease) is True
        try:
            note = bm.flush_quiet_queue(
                {"quiet_queue": path}, now_utc=npt(4, 7, 0),
                http_fn=lambda url, payload: (sent.append(payload),
                                              (200, "{}"))[1])
            assert "lease" in note, note
            assert sent == [], sent
        finally:
            alert_dedupe.release_flush(lease)

        note = bm.flush_quiet_queue(
            {"quiet_queue": path}, now_utc=npt(4, 7, 1),
            http_fn=lambda url, payload: (sent.append(payload), (200, "{}"))[1])
        assert "delivered 1" in note, note
        assert len(sent) == 1, sent
        assert bm.flush_quiet_queue(
            {"quiet_queue": path}, now_utc=npt(4, 7, 2),
            http_fn=lambda url, payload: (sent.append(payload), (200, "{}"))[1]
        ) == "", "an empty queue must send nothing a second time"
        assert len(sent) == 1, sent
    finally:
        alert_dedupe.release_flush(lease)
        os.chdir(here)
        restore()
        shutil.rmtree(sd, ignore_errors=True)


def test_108_both_workflows_reach_the_same_quiet_hours_decision():
    """No workflow may re-implement the window: they all call the shared gate."""
    workflows = os.path.join(REPO_ROOT, ".github", "workflows")
    with open(os.path.join(workflows, "drift-canary.yml"), "r",
              encoding="utf-8") as fh:
        drift = fh.read()
    with open(os.path.join(workflows, "digest-watchdog.yml"), "r",
              encoding="utf-8") as fh:
        watchdog = fh.read()
    with open(os.path.join(SCRIPTS, "live_canary.py"), "r",
              encoding="utf-8") as fh:
        canary_py = fh.read()

    assert "alert_dedupe.hold_if_quiet" in canary_py, \
        "the canary must use the shared decision"
    assert watchdog.count("scripts/may-notify.sh") >= 4, \
        "every watchdog push arm must go through the shared gate"
    assert "alert_dedupe.hold_if_quiet" in watchdog, \
        "the watchdog's python push arms must use the shared decision"
    for name, text in (("drift-canary.yml", drift),
                       ("digest-watchdog.yml", watchdog)):
        assert "QUIET_HOURS_START" not in text, \
            "%s must not restate the window" % name
        assert "quiet_queue.json" in text, \
            "%s must commit the hold, or the hold is a drop" % name


def test_109_the_bash_bridge_returns_the_same_three_answers():
    """The four watchdog curl arms ask this bridge, so its exit codes are the gate.

    0 = push, 1 = held (and queued), 2 = misuse. Executed for real: a shell
    bridge that is only asserted by reading it is how a wrong argument order
    ships green.
    """
    bash = _bash_for_commit_state()
    script = os.path.join(SCRIPTS, "may-notify.sh")
    args = [bash, script, "canary", "Live parser canary",
            "three parsers dead", "canary.live_parser", "5"]

    def run(now, argv):
        work = fresh_dir()
        env = dict(os.environ)
        env["PYTHONPATH"] = SCRIPTS
        env["ALERT_DEDUPE_NOW_UTC"] = now
        proc = subprocess.run(argv, cwd=work, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        return proc, work

    proc, work = run("2026-11-04T00:30:00Z", args)
    try:
        assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
        assert "quiet hours, held 1 item(s) until 07:00 NPT" in proc.stdout, \
            proc.stdout
        path = os.path.join(work, "state", "quiet_queue.json")
        assert os.path.isfile(path), "held must mean QUEUED, not dropped"
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        assert [i["condition"] for i in doc["items"]] == ["canary.live_parser"], doc
        assert doc["items"][0]["priority"] == 5, doc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    proc, work = run("2026-11-04T10:00:00Z", args)
    try:
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stdout.strip() == "", proc.stdout
        assert not os.path.isfile(os.path.join(work, "state",
                                              "quiet_queue.json")), \
            "a daytime notice must not touch the queue"
    finally:
        shutil.rmtree(work, ignore_errors=True)

    proc, work = run("2026-11-04T10:00:00Z", [bash, script, "canary"])
    try:
        assert proc.returncode == 2, (proc.returncode, proc.stdout)
        assert "required" in proc.stdout, proc.stdout
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    tests = [
        test_1_routine_news_silent_heartbeat,
        test_2_breaking_headline_routine_substance_no_trip,
        test_3_earthquake_trips_level3_alert_rendered,
        test_4_same_event_no_duplicate,
        test_5_confidence_escalation_realerts,
        test_6_rumor_wording_capped_no_send,
        test_7_entertainment_spoiler_vetoed,
        test_8_all_sources_fail_silent_logged,
        test_9_smtp_failure_persists_pending_then_retry_clears,
        test_10_parallel_writes_keep_files_valid,
        test_11_window_filter_drops_old_items,
        test_12_pre_gate_regex_table,
        test_13_rss_fixture_trips_with_detection_source_rss,
        test_14_gnews_rss_same_story_collision_single_dispatch,
        test_15_l3_email_and_telegram_both_fire_chat_id_autodiscovered,
        test_16_telegram_api_fails_email_still_sent_state_updated,
        test_17_no_token_no_attempt,
        test_18_dry_run_writes_preview_file_no_network,
        test_19_check_universal_importance_death_count,
        test_20_check_universal_importance_disasters,
        test_21_check_universal_importance_policy_geopolitics,
        test_22_universal_importance_overrides_relevance,
        test_23_ai_universal_importance_flag_triggers_override,
        test_24_critical_telegram_uses_detailed_format,
        test_25_non_critical_items_use_standard_format,
        test_26_ai_classifier_universal_importance_passthrough,
        test_27_prompt_includes_universal_importance,
        test_28_non_critical_breaking_both_gmail_and_telegram,
        test_29_critical_breaking_both_gmail_and_telegram_detailed,
        test_30_alert_labels_real_fire_time_never_a_slot,
        test_31_normal_digest_keeps_its_own_slot_labels,
        test_32_plain_text_source_lines_are_bulleted,
        test_33_normalize_verdict_string_true_is_true,
        test_34_normalize_verdict_bool_true_still_true,
        test_35_normalize_verdict_false_spellings,
        test_36_normalize_verdict_junk_never_raises,
        test_37_string_true_flag_drives_override_end_to_end,
        test_38_universal_importance_is_always_a_bool,
        test_39_normalize_verdict_string_true_escalates,
        test_40_normalize_verdict_bool_escalated_passes_through,
        test_41_normalize_verdict_escalated_false_spellings,
        test_42_normalize_verdict_escalated_junk_never_raises,
        test_43_string_true_escalation_keeps_developing_notice_end_to_end,
        test_44_escalated_from_previous_is_always_a_bool,
        test_45_ntfy_is_attempted_before_any_fallback,
        test_46_fallbacks_are_not_called_when_ntfy_succeeds,
        test_47_fallbacks_fire_on_a_genuine_ntfy_failure,
        test_48_fallback_success_does_not_erase_the_primary_failure,
        test_49_total_failure_is_never_written_as_sent_true,
        test_50_an_unverifiable_transport_never_counts_as_success,
        test_51_a_transport_exception_is_recorded_not_swallowed,
        test_52_not_configured_is_not_recorded_as_a_delivery_failure,
        test_53_the_ai_judge_cannot_control_any_delivery_signal,
        test_54_ntfy_message_orients_before_it_details,
        test_55_critical_ntfy_message_uses_the_detailed_format,
        test_56_dry_run_writes_the_ntfy_preview_and_touches_no_network,
        test_57_the_topic_never_reaches_a_log_or_a_url,
        test_58_chat_id_discovery_prefers_the_private_chat_over_a_newer_group,
        test_59_chat_id_discovery_falls_back_to_newest_of_any_type,
        test_60_chat_id_discovery_skips_a_chat_whose_sender_is_a_bot,
        test_61_smtp_factory_sets_a_connect_timeout,
        test_62_selftest_payload_matches_tg_send_exactly,
        test_63_timeout_evidence_is_captured_redacted_and_kept,
        test_64_a_model_not_found_is_cooled_not_called_garbage,
        test_65_the_attempt_record_keeps_the_measured_wall_not_just_the_window,
        test_66_the_ladder_probe_walks_once_and_cannot_send_judge_or_promote,
        test_67_a_silent_rung_is_abandoned_at_the_first_output_deadline,
        test_68_a_rung_that_goes_live_at_two_seconds_is_allowed_to_finish,
        test_69_first_byte_time_is_recorded_for_a_slow_but_streaming_rung,
        test_70_the_control_rung_is_labelled_and_excluded_from_the_free_verdict,
        test_71_every_attribution_value_comes_from_its_stated_condition,
        test_72_the_silent_probe_is_bounded_by_the_new_arithmetic,
        test_73_a_rung_that_goes_live_on_stderr_is_not_abandoned,
        test_74_a_rung_silent_on_both_streams_is_still_abandoned,
        test_75_a_liveness_only_row_is_accepted_and_explains_itself,
        test_76_the_verdict_is_parsed_from_stdout_only,
        test_77_a_live_rung_that_is_slow_gets_its_whole_grant,
        test_78_the_breaking_child_asks_the_cli_for_its_log,
        test_79_the_keyed_rung_leads_the_ladder_when_its_credential_is_present,
        test_80_a_quota_notice_is_its_own_class_not_a_timeout_or_garbage,
        test_81_a_quota_notice_before_liveness_is_recorded_and_stops_the_provider,
        test_82_a_non_probe_run_cannot_commit_the_probe_file,
        test_83_a_probe_run_commits_only_its_own_result,
        test_84_the_refused_paths_are_named_in_the_output,
        test_85_a_non_probe_run_still_commits_its_own_state,
        test_86_the_probe_candidate_is_realistic_and_non_empty,
        test_87_an_empty_judgment_is_recorded_as_judged_content_false,
        test_88_a_real_verdict_is_recorded_as_judged_content_true,
        test_89_a_decline_is_not_reported_as_a_failure_of_capability,
        test_90_a_judged_important_candidate_makes_the_probe_capable,
        test_91_judging_the_unimportant_candidate_is_a_failed_half,
        test_92_a_decline_of_the_important_candidate_names_the_failing_half,
        test_93_the_two_candidate_probe_still_commits_only_its_own_file,
        test_94_the_judged_candidate_keeps_the_answer_it_gave,
        test_95_the_declined_candidate_keeps_the_empty_object,
        test_96_the_summary_step_prints_the_important_answer,
        test_97_a_non_critical_level3_at_2300_npt_is_held_not_sent,
        test_98_a_non_critical_level3_at_0659_npt_is_still_held,
        test_99_the_held_batch_is_delivered_once_when_quiet_hours_end,
        test_100_the_critical_definition_is_exactly_two_shapes,
        test_101_a_critical_alert_at_2300_npt_is_sent_immediately,
        test_102_the_queue_is_capped_and_the_batch_says_so,
        test_103_a_held_recovery_is_superseded_by_a_later_failure,
        test_104_a_canary_failure_at_0615_npt_is_held_and_rides_the_0700_batch,
        test_105_a_watchdog_failure_at_2145_notifies_and_one_at_2205_is_held,
        test_106_only_one_flush_site_exists_and_a_second_caller_is_refused,
        test_107_a_scheduled_run_and_a_manual_dispatch_cannot_both_deliver,
        test_108_both_workflows_reach_the_same_quiet_hours_decision,
        test_109_the_bash_bridge_returns_the_same_three_answers,
    ]
    failures = []
    for test in tests:
        try:
            test()
            print("PASS %s" % test.__name__)
        except AssertionError as exc:
            failures.append((test.__name__, exc))
            print("FAIL %s: %s" % (test.__name__, exc))
        except Exception as exc:
            failures.append((test.__name__, exc))
            print("ERROR %s: %r" % (test.__name__, exc))
    print("---")
    print("%d/%d passed" % (len(tests) - len(failures), len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
