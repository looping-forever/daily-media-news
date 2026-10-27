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

import json
import os
import re
import shutil
import smtplib
import sys
import tempfile
import threading
import unittest.mock
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import breaking_monitor as bm  # noqa: E402
import ai_classifier as ac     # noqa: E402

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
             sources_json="", window_min=90):
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
    return bm.main(argv, fetch_fn=fetch, ai_runner=runner,
                   smtp_factory=smtp or smtp_factory("ok"))


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
