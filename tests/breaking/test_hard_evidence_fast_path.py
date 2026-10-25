#!/usr/bin/env python3
"""tests/breaking/test_hard_evidence_fast_path.py — the 2026-10-13 mass-casualty
loss, and the fast path that exists so it cannot happen again.

WHAT HAPPENED. Run 38373618708 (09:03Z) received the headline
"At least 139 migrants dead after boat capsized in Bay of Bengal". It TRIPPED
the deterministic gate. Both ladder models then failed — big-pickle timed out,
mimo-v2.5-free returned an unparseable verdict — so ai_classify returned
(None, err), main() logged `ai_fail_safe`, and NOTHING was sent. The honesty
gate fired, so the fault was loud, and the owner still missed a 139-death
story. A 09:06Z dispatch of "Army helicopter rescues three trapped by landslide
in Gorkha" hit the same wall.

These tests pin all three things that has to be true afterwards:
  1. a stated mass-casualty death toll is alerted with NO classifier at all;
  2. everything that is not a stated mass-casualty death toll still goes
     through the classifier, so the fast path cannot become a rubber stamp;
  3. a failed classifier is STILL logged loudly, and now carries the headline
     the owner needs in order to act by hand.

Plain asserts, stdlib only, no network. Run:

    py tests\\breaking\\test_hard_evidence_fast_path.py
"""

import json
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import breaking_monitor as bm  # noqa: E402
from test_breaking_monitor import (  # noqa: E402
    NOW, FakeSMTP, make_fetch, rfc822, rss_xml, run_main, verdict_runner,
)

# THE PIN, OPTED INTO EXPLICITLY, AND ONLY HERE.
#
# This suite asserts that the attempt ladder came from the provider catalogue
# and is wider than the 3-model config seed. On a runner with no `opencode`
# binary -- the ubuntu-24.04 CI runner -- live discovery cannot answer, the
# ladder collapses to the seed, and the assertion fires for a reason that has
# nothing to do with the behaviour under test. The pin supplies the same
# post-gate id list with the TTL removed, so the property is demonstrated
# deterministically instead of depending on when the runner last reached the
# provider.
#
# It is set HERE, in a test process, and nowhere else. No committed workflow
# sets MODEL_SELECTOR_PIN (pinned by tests/workflows/test_workflow_lint.py), so
# production cannot reach the fixture and a genuinely broken selector in
# production still degrades loudly to the seed rather than quietly to a frozen
# list. See PIN_ENV in scripts/model_selector.py.
os.environ.setdefault(
    "MODEL_SELECTOR_PIN",
    os.path.join(REPO_ROOT, "tests", "fixtures", "model_selector_pin.json"))

CORPUS = os.path.join(REPO_ROOT, "tests", "breaking", "fixtures",
                      "real_headlines.json")

# The two dispatches that were lost on 2026-10-13, verbatim in shape.
LOST_MASS_CASUALTY = ("At least 139 migrants dead after boat capsized in "
                      "Bay of Bengal: Official")
LOST_SMALL_RESCUE = ("Army helicopter rescues three trapped by landslide in "
                     "Gorkha")

#: A runner that fails the way both real models failed on 2026-10-13: one
#: transport timeout, one unusable answer. Returns the per-model call count so
#: a test can prove the fast path never reached it at all.
class DeadLadder:
    def __init__(self):
        self.calls = []

    def __call__(self, model, prompt_text, env, timeout_sec):
        self.calls.append(model)
        if len(self.calls) == 1:
            raise TimeoutError("opencode timed out after %ds" % timeout_sec)
        return 0, "I am not JSON and I am not a verdict.", ""


def _smtp_ok():
    FakeSMTP.sent_subjects = []
    return lambda host, port: FakeSMTP(host, port)


def _run(headline, link, runner, state_dir, dry_run=True, snippet=None):
    body = rss_xml([(headline, link, rfc822(NOW),
                     snippet or headline)])
    fetch = make_fetch({"feeds.example.test": body})
    rc = run_main(state_dir, fetch, runner=runner, dry_run=dry_run,
                  smtp=_smtp_ok())
    with open(os.path.join(state_dir, "breaking_log.json"),
              encoding="utf-8") as fh:
        rows = json.load(fh)
    return rc, rows


def _fresh():
    return tempfile.mkdtemp(prefix="fastpath-")


# ---------------------------------------------------------------------------
# 1. THE LOSS ITSELF. This is the mutation-proof target: revert the fast path
#    and this test goes red with the 2026-10-13 message in the output.
# ---------------------------------------------------------------------------

def test_a_mass_casualty_headline_is_alerted_with_no_classifier_at_all():
    """THE REGRESSION THIS FILE EXISTS FOR.

    The classifier is dead. The headline states 139 dead. An alert must be
    sent anyway -- and the log row must say the ladder was never consulted,
    so a bypass that nobody can read afterwards is impossible.
    """
    sd = _fresh()
    try:
        ladder = DeadLadder()
        rc, rows = _run(LOST_MASS_CASUALTY,
                        "https://example-news-a.com/139-dead", ladder, sd)

        assert rc == 0
        row = rows[-1]
        assert row["tripped"] is True, "the gate must have tripped: %r" % row
        assert row["sent"] is True, (
            "THE 2026-10-13 DEFECT. A stated 139-death toll produced NO alert "
            "(reason=%r)." % row.get("reason"))
        assert row["level"] == 3, row
        assert "hard_evidence_fast_path" in row["reason"], (
            "the log row must record that the classifier was bypassed. A "
            "bypass that leaves no trace is indistinguishable from a "
            "classifier that happened to agree. reason=%r" % row.get("reason"))
        assert ladder.calls == [], (
            "the AI ladder was consulted for a stated mass-casualty death "
            "toll. The fast path is authoritative when it fires: %r"
            % ladder.calls)
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_the_alert_carries_the_headline_verbatim_and_invents_nothing():
    """A fast-path alert must not assert anything the headline did not."""
    sd = _fresh()
    try:
        rc, rows = _run(LOST_MASS_CASUALTY,
                        "https://example-news-a.com/139-dead",
                        DeadLadder(), sd)
        assert rows[-1]["sent"] is True
        verdict, why = bm.hard_evidence_fast_path([{
            "title": LOST_MASS_CASUALTY,
            "url": "https://example-news-a.com/139-dead",
            "source": "example-news-a.com",
            "snippet": LOST_MASS_CASUALTY,
        }])
        assert verdict is not None, why
        assert verdict["title"] == LOST_MASS_CASUALTY, (
            "the title must be the headline VERBATIM, never rewritten")
        assert verdict["summary"] == "", (
            "a fast-path verdict must carry NO summary. An invented sentence is "
            "exactly how a fabricated alert happens.")
        assert verdict["sources"] == [{
            "name": "example-news-a.com",
            "url": "https://example-news-a.com/139-dead"}], (
            "the only source may be the candidate's own link, not a "
            "second-hand citation: %r" % (verdict["sources"],))
        assert "139" in verdict["why_urgent"], verdict["why_urgent"]
        # The spoiler veto matches "death"/"killed"/"died". A verdict that put
        # those words in the summary would be silently downgraded to a
        # level-1 log row by Stage 5, which is a quiet loss of an alert.
        assert not bm.ALERT_SPOILER_RE.search(
            "%s %s" % (verdict["title"], verdict["summary"])), (
            "a fast-path verdict must not trip ALERT_SPOILER_RE, or Stage 5 "
            "silently downgrades it and the alert is lost")
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. WHAT DOES NOT QUALIFY. The fast path must stay a narrow hole, not a new
#    way for anything to be sent.
# ---------------------------------------------------------------------------

def test_the_small_rescue_that_was_also_lost_still_goes_to_the_classifier():
    """"Army helicopter rescues three trapped by landslide in Gorkha" tripped
    the gate on `trapped` and was lost to the same ladder failure. It is still
    NOT a fast-path alert: three people is not a mass casualty, and the
    classifier is the thing that judges a borderline event."""
    sd = _fresh()
    try:
        ladder = DeadLadder()
        rc, rows = _run(LOST_SMALL_RESCUE,
                        "https://example-news-a.com/gorkha-rescue", ladder, sd)
        row = rows[-1]
        assert row["tripped"] is True
        assert row["sent"] is False, (
            "a three-person rescue must not alert without a classifier; "
            "reason=%r" % row.get("reason"))
        assert ladder.calls, "the classifier must have been consulted"
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_nothing_qualifies_without_a_stated_count_above_the_threshold():
    """The rule is a NUMBER above the repo's own mass-casualty threshold, not
    a feeling about the headline."""
    cases = [
        # (title, why it must not fire)
        ("Heavy rain leaves 7 dead in Parsa village", "7 is not > 10"),
        ("Ten dead in Bara landslide", "10 is not > 10"),
        ("Body of a missing hiker found in Annapurna", "no count at all"),
        ("Army helicopter rescues three trapped by landslide in Gorkha",
         "no count at all"),
        ("Villagers mourn after the rescue effort in Syangja", "framing only"),
        ("Explainer: what the death toll figure in Nepal's floods means",
         "no count of its own"),
    ]
    for title, why in cases:
        verdict, _ = bm.hard_evidence_fast_path([{
            "title": title, "url": "https://example-news-a.com/x",
            "source": "example-news-a.com", "snippet": title}])
        assert verdict is None, (
            "%r must NOT take the fast path (%s); it qualified with %r"
            % (title, why, verdict and verdict.get("why_urgent")))


def test_the_existing_tight_extractor_would_have_missed_the_lost_headline():
    """WHY THE EXTRACTOR IS NOT A COPY OF THE OLD ONE.

    The pre-existing mass-casualty extractor in _universal_hits() requires the
    count IMMEDIATELY followed by the casualty noun. "139 migrants dead" puts a
    word between them, so reusing that regex verbatim would have left the
    2026-10-13 headline on the floor -- the defect would have shipped again,
    green, and for the same reason.
    """
    matched, _why = bm._universal_hits(LOST_MASS_CASUALTY)
    assert not matched, (
        "the existing extractor now matches %r, so the new one is redundant "
        "and the two should be reconciled rather than coexist" % LOST_MASS_CASUALTY)
    count, span = bm.stated_casualty_count(LOST_MASS_CASUALTY)
    assert count == 139, (count, span)


def test_a_number_cannot_walk_across_punctuation_into_another_clause():
    """The conservatism that makes the extractor safe. An earlier draft ran on
    the gate blob and matched 'Dead - Ratopati 55' -- a count off the
    PUBLISHER SUFFIX -- calling one casualty 55."""
    assert bm.stated_casualty_count(
        "One dead in a traffic collision - Times of India") == (None, "")
    assert bm.stated_casualty_count(
        "Flood damage 12 hours ago, 3 dead in Parsa") == (3, "3 dead")
    # And the title/snippet boundary is never crossed at all.
    verdict, _ = bm.hard_evidence_fast_path([{
        "title": "One Dead After Collision - Times of India",
        "url": "https://example-news-a.com/x",
        "source": "example-news-a.com",
        "snippet": "55 Injured Treated in Three Hospitals, One Dead",
    }])
    assert verdict is None, (
        "a count taken from the snippet must not qualify a title: %r" % verdict)


def test_a_rumoured_mass_casualty_never_takes_the_fast_path():
    """The rumor cap is one of the stages the fast path must NOT bypass, and
    it is re-asserted here so a future gate change cannot silently widen it."""
    verdict, _ = bm.hard_evidence_fast_path([{
        "title": "Rumour: 40 dead in unreported landslide in Rukum",
        "url": "https://example-news-a.com/x",
        "source": "example-news-a.com",
        "snippet": "Rumour: 40 dead in unreported landslide in Rukum"}])
    assert verdict is None, (
        "a rumoured 40-death toll must not alert unverified: %r" % verdict)


def test_the_fast_path_cannot_bypass_stage_6_dedup():
    """It replaces the CLASSIFIER and nothing else. A second run of the same
    story must still be a silent duplicate, or every sweep would re-page."""
    sd = _fresh()
    try:
        item = (LOST_MASS_CASUALTY, "https://example-news-a.com/139-dead")
        rc, rows = _run(item[0], item[1], DeadLadder(), sd)
        assert rows[-1]["sent"] is True
        rc, rows = _run(item[0], item[1], DeadLadder(), sd)
        assert rows[-1]["sent"] is False, (
            "the same mass-casualty story alerted twice: %r" % rows[-1])
        assert "duplicate_no_escalation" in rows[-1]["reason"], rows[-1]
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. MEASURED, NOT ASSERTED. The rule is pinned against the frozen corpus, so
#    it cannot be widened later without a test going red with a row count.
# ---------------------------------------------------------------------------

def test_the_rule_is_pinned_to_the_616_row_corpus_with_no_false_positive():
    """Every row it fires on is a real mass-casualty headline, and the count
    is asserted so a widening shows up as a number change."""
    with open(CORPUS, encoding="utf-8") as fh:
        rows = json.load(fh)["rows"]
    fired = []
    for row in rows:
        title = row["title"]
        blob = bm._gate_blob(title, row.get("snippet", ""))
        if not bm.importance_gate(blob, title)[0]:
            continue
        verdict, _why = bm.hard_evidence_fast_path([{
            "title": title, "url": "https://x.invalid/%d" % len(fired),
            "source": "x.invalid", "snippet": row.get("snippet", "")}])
        if verdict is not None:
            fired.append(row["title"])
    assert len(fired) == 32, (
        "the fast path now fires on %d of 616 corpus rows (was 32). Every "
        "widening needs a deliberate, measured decision: %s"
        % (len(fired), fired))
    # Spot-check the shapes that matter, so a number-only assertion cannot hide
    # a rule that fires for the wrong reason. Matched WITHOUT the trailing
    # "- Publisher" suffix, which is not part of the rule.
    for must_fire in ("day of mourning for more than 1,300 killed in floods",
                      "death toll reaches 800, with more than 3,000 missing",
                      "kill at least 68 in northern India and Nepal"):
        assert any(must_fire in t for t in fired), must_fire


# ---------------------------------------------------------------------------
# 4. THE LADDER. One extra attempt, transport failures only.
# ---------------------------------------------------------------------------

def test_a_transport_failure_is_retried_once():
    sd = _fresh()
    try:
        calls = []

        def flaky(model, prompt_text, env, timeout_sec):
            calls.append(model)
            if len(calls) == 1:
                raise TimeoutError("provider timed out")
            return 0, json.dumps({
                "level": 3, "confidence": "HIGH",
                "event_key": "e", "title": "t", "summary": "s",
                "why_urgent": "w", "sources": [], "category_suggestion": "",
            }), ""

        rc, rows = _run("Magnitude 7.1 earthquake strikes near Sindhuli",
                        "https://example-news-a.com/sindhuli", flaky, sd)
        assert len(calls) == 2, (
            "a timed-out model must get exactly one more attempt, got %d: %r"
            % (len(calls), calls))
        assert calls[0] == calls[1], (
            "the retry must re-ask the SAME model, not move on: %r" % calls)
        assert not rows[-1]["reason"].startswith("ai_fail_safe"), (
            "the ladder recovered on the retry, so this must not read as a "
            "failed classifier: %r" % rows[-1]["reason"])
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_an_unparseable_verdict_is_NOT_retried():
    """The model ANSWERED; it answered unusably. Re-asking the same question is
    how a broken classifier eats the job's whole time budget.

    The count is asserted against the ladder THIS RUN actually selected, not
    against the static config seed: selection is dynamic now, so pinning the
    number would pin the brittleness this change removes. The property under
    test is unchanged and is asserted twice over -- every selected model is
    tried exactly once, and no model is tried twice.

    NON-VACUOUS: the resolved ladder is also checked to have come from the
    live provider catalogue and to be wider than the config seed. Without
    that, this test would still pass if selection silently degraded back to
    the three hardcoded seeds -- which is exactly the state the 2026-10-13
    incident happened in, and exactly the state in which the ladder names a
    model Zen no longer serves.
    """
    calls = []

    def junk(model, prompt_text, env, timeout_sec):
        calls.append(model)
        return 0, "sorry, I cannot do that", ""

    sd = _fresh()
    try:
        rc, rows = _run("Magnitude 7.1 earthquake strikes near Sindhuli",
                        "https://example-news-a.com/sindhuli", junk, sd)
        selected = list(bm.ai_classify.last_models)
        info = bm.ai_classify.last_info
        assert selected, (
            "the run selected no models at all, so nothing was retried for the "
            "wrong reason: %r" % (info,))
        assert len(calls) == len(selected), (
            "each model must get exactly ONE attempt at an unusable answer "
            "(this run selected %d), got %d calls: %r"
            % (len(selected), len(calls), calls))
        assert len(set(calls)) == len(calls), (
            "a model answered unusably and was asked again: %r" % (calls,))
        assert set(calls) == set(selected), (
            "the models called and the models selected disagree: %r vs %r"
            % (calls, selected))
        assert info["source"] in ("live", "memo", "cache", "pin"), (
            "the attempt ladder did not come from the provider catalogue, so "
            "nothing dynamic happened at all: %r" % (info,))
        assert len(selected) >= len(bm.AI_MODELS), (
            "the resolved ladder (%d) is smaller than the config seed (%d): %r"
            % (len(selected), len(bm.AI_MODELS), info))
        assert len(selected) <= bm.AI_MAX_MODELS, (
            "the resolved ladder is not depth-capped: %r" % (info,))
        assert rows[-1]["sent"] is False, rows[-1]
        assert rows[-1]["reason"].startswith("ai_fail_safe"), rows[-1]
    finally:
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. THE HONESTY GATE STAYS LOUD. The fast path reduces how often this fires;
#    it must not make it quieter when it does.
# ---------------------------------------------------------------------------

def test_a_dead_ladder_is_still_logged_loudly_and_now_names_the_headline():
    """The owner has to be able to act by hand, which needs the headline. The
    2026-10-13 notice named a broken classifier and no event."""
    sd = _fresh()
    try:
        rc, rows = _run(LOST_SMALL_RESCUE,
                        "https://example-news-a.com/gorkha-rescue",
                        DeadLadder(), sd)
        row = rows[-1]
        assert row["reason"].startswith("ai_fail_safe"), (
            "a failed classifier must never become a silent pass: %r" % row)
        assert row["sent"] is False
        assert row.get("unjudged_titles") == [LOST_SMALL_RESCUE], (
            "the honesty-gate notice must carry the un-judged headline, or the "
            "owner is told the classifier broke and not what it broke on: %r"
            % row.get("unjudged_titles"))
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_the_honesty_gate_workflow_reports_the_un_judged_headline():
    """Read the workflow itself: the ntfy body must carry the headline block,
    and `ai_failed` must be tested BEFORE `sent`, so a broken classifier can
    never hide behind a successful send."""
    wf = os.path.join(REPO_ROOT, ".github", "workflows", "breaking-monitor.yml")
    with open(wf, encoding="utf-8") as fh:
        text = fh.read()
    assert "unjudged_block" in text, (
        "the honesty-gate ntfy body must carry the un-judged headline(s)")
    assert "UN-JUDGED HEADLINE(S)" in text
    assert 'get("unjudged_titles")' in text
    ai_at = text.index("if ai_failed:", text.index('last.get("tripped") is False')
                       - 4000)
    sent_at = text.index('elif last.get("sent") is True:')
    assert ai_at < sent_at, (
        "ai_failed must be checked BEFORE the sent branch, or a broken "
        "classifier hides inside a healthy-looking 'ALERT SENT' line")


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in fns:
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as exc:
            failed.append(name)
            print("FAIL %s\n       %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print("ERROR %s: %s" % (name, exc))
    print("\nRan %d tests, %d failed" % (len(fns), len(failed)))
    sys.exit(1 if failed else 0)