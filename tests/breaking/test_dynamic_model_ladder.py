#!/usr/bin/env python3
"""tests/breaking/test_dynamic_model_ladder.py — dynamic model selection.

THE DEFECT THIS EXISTS FOR. On 2026-10-13 (run 38373618708) the headline
"At least 139 migrants dead after boat capsized in Bay of Bengal" tripped the
deterministic gate, `opencode/big-pickle` timed out after 60s, and the second
ladder rung returned an unparseable verdict. Nothing was decided, nothing was
sent. The ladder was a hardcoded ordered list, duplicated as an embedded
fallback tuple in every consumer — and it had already rotted: by 2026-10-18
rung 2, `opencode/mimo-v2.5-free`, was absent from the live listing. A dead ID
in a hardcoded list is worse than no ID, because the run spends wall-clock
proving a model is gone on exactly the days the provider is sickest. An ID's
liveness is read from a record the catalogue printed for it -- never from its
absence from a listing, and never frozen in this file.

Four things have to be true afterwards, and each is pinned here:
  1. DISCOVERY is real and measured — free / active / text-capable are read
     from what the CLI reported, never guessed from a model's NAME;
  2. HEALTH MEMORY demotes a model that timed out or answered unusably, with
     a documented decay that lets it come back;
  3. HEALTH MEMORY CAN ONLY REORDER — it can never veto, because a watchdog
     that decides what counts as breaking news must never be silenced by its
     own memory of a bad afternoon;
  4. WORST-CASE TIME is bounded and provably lower than 360s.

Offline and stdlib-only: the CLI's real output shape is a fixture, and time
is a synthetic clock. Nothing here calls a model or the network.

    py tests\\breaking\\test_dynamic_model_ladder.py
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import breaking_monitor as bm  # noqa: E402
import model_selector as ms  # noqa: E402

#: NOW/RUN_MAIN fixtures live in the breaking suite that owns them; importing
#: them here keeps the two suites on ONE run_main, so this test exercises the
#: real pipeline rather than a private re-implementation of it.
from test_breaking_monitor import NOW  # noqa: E402

# THE PIN. Opted into explicitly, in a test process only -- see the identical
# block in test_hard_evidence_fast_path.py and PIN_ENV in
# scripts/model_selector.py for why discovery must be deterministic on a
# runner with no `opencode` binary, and why no committed workflow may set this.
os.environ.setdefault(
    "MODEL_SELECTOR_PIN",
    os.path.join(REPO_ROOT, "tests", "fixtures", "model_selector_pin.json"))

PROVIDER = "opencode"


# ---------------------------------------------------------------------------
# The real CLI output shape, trimmed. `opencode models <p> --verbose` prints
# `provider/model` on its own line, then that model's JSON object indented
# underneath. Field values are copied from the live listing measured on this
# machine; the fixture is two models because two is all a parser needs.
# ---------------------------------------------------------------------------
def _record(mid, name, status, cost_in, cost_out, text_in=True, text_out=True,
            release="2026-08-11"):
    return (
        "%s/%s\n"
        '{\n'
        '  "id": "%s",\n'
        '  "providerID": "%s",\n'
        '  "name": "%s",\n'
        '  "status": "%s",\n'
        '  "cost": {\n'
        '    "input": %d,\n'
        '    "output": %d,\n'
        '    "cache": {\n'
        '      "read": 0,\n'
        '      "write": 0\n'
        '    }\n'
        '  },\n'
        '  "capabilities": {\n'
        '    "reasoning": true,\n'
        '    "input": {"text": %s, "audio": false, "image": false},\n'
        '    "output": {"text": %s, "image": false}\n'
        '  },\n'
        '  "release_date": "%s"\n'
        '}\n'
    ) % (PROVIDER, mid, mid, PROVIDER, name, status, cost_in, cost_out,
         "true" if text_in else "false", "true" if text_out else "false",
         release)


LIVE_LISTING = (
    _record("big-pickle", "Big Pickle", "active", 0, 0)
    + _record("paid-model", "Paid Model", "active", 3, 15)
    + _record("retired-model", "Retired Model", "deprecated", 0, 0)
    + _record("image-only-model", "Image Only", "active", 0, 0, text_out=False)
    + _record("audio-only-model", "Audio Only", "active", 0, 0, text_in=False)
    + _record("nemotron-3.5-lightning-free", "Nemotron", "active", 0, 0)
)

#: The two IDs that were hardcoded on 2026-10-13. Neither may be dropped merely
#: for being absent from a listing -- that rule is what run 40280837914 broke,
#: and `COLD_LISTING` below is that listing.
SEED_BIG_PICKLE = "%s/big-pickle" % PROVIDER


def _fake_run(stdout):
    class _Proc(object):
        returncode = 0

        def __init__(self):
            self.stdout = stdout
            self.stderr = ""

    def run(argv, timeout):
        assert argv[0] == "models" and argv[1] == PROVIDER, argv
        return _Proc()

    return run


def _fail_run(*_a, **_k):
    raise OSError("opencode not found")


# ---------------------------------------------------------------------------
# 1. DISCOVERY IS REAL, AND MEASURED
# ---------------------------------------------------------------------------

def test_discovery_returns_only_free_active_text_models():
    """The three gates are read from fields the CLI printed, not from names."""
    ms.reset_discovery_memo()
    sd = tempfile.mkdtemp(prefix="discovery-")
    try:
        cache = os.path.join(sd, "model_cache.json")
        models, info = ms.discover(PROVIDER, cache_path=cache,
                                   run_cmd=_fake_run(LIVE_LISTING))
        assert models == [SEED_BIG_PICKLE, SEED_BIG_PICKLE.rsplit("/", 1)[0]
                          + "/nemotron-3.5-lightning-free"], models
        assert info["source"] == "live", info
        rejected = info["rejected"]
        assert rejected["%s/paid-model" % PROVIDER] == "not_free", rejected
        assert rejected["%s/retired-model" % PROVIDER].startswith("status:"), rejected
        assert rejected["%s/image-only-model" % PROVIDER] == "no_text_output", rejected
        assert rejected["%s/audio-only-model" % PROVIDER] == "no_text_input", rejected
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_a_model_whose_metadata_does_not_parse_is_not_selectable():
    """Unverifiable is not the same as usable. A model we cannot prove is free
    and active is refused, because this file must be more conservative about
    what it trusts, never less."""
    ms.reset_discovery_memo()
    sd = tempfile.mkdtemp(prefix="discovery-junk-")
    try:
        cache = os.path.join(sd, "model_cache.json")
        broken = LIVE_LISTING + "%s/half-broken\n{\n  \"id\": \"half-broken\",\n" \
                                      % PROVIDER
        models, info = ms.discover(PROVIDER, cache_path=cache,
                                   run_cmd=_fake_run(broken))
        assert "%s/half-broken" % PROVIDER not in models, models
        assert "%s/half-broken" % PROVIDER in info["unparsed"], info
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_discovery_never_invents_a_model_when_the_cli_fails():
    """Total discovery failure yields the CACHE, then nothing. Never a list
    written down in code — that copy is the 2026-10-13 defect."""
    ms.reset_discovery_memo()
    sd = tempfile.mkdtemp(prefix="discovery-fail-")
    try:
        cache = os.path.join(sd, "model_cache.json")
        ms.discover(PROVIDER, cache_path=cache,
                    run_cmd=_fake_run(LIVE_LISTING))
        # Drop the process memo so the SECOND call genuinely exercises the
        # cache tier. Without this the memo answers first and the cache path
        # is never reached, so a broken fallback would still pass.
        ms.reset_discovery_memo()
        models, info = ms.discover(PROVIDER, cache_path=cache,
                                   run_cmd=_fail_run)
        assert models == [SEED_BIG_PICKLE,
                          "%s/nemotron-3.5-lightning-free" % PROVIDER], models
        assert info["source"] == "cache", info

        ms.reset_discovery_memo()
        models, info = ms.discover(PROVIDER, cache_path=None,
                                   run_cmd=_fail_run)
        assert models == [] and info["source"] == "none", info
    finally:
        shutil.rmtree(sd, ignore_errors=True)
        ms.reset_discovery_memo()


# ---------------------------------------------------------------------------
# 2 + 3. HEALTH MEMORY: demotion with decay, and never a veto
# ---------------------------------------------------------------------------

def test_a_timeout_demotes_the_model_and_a_success_returns_it():
    """The decay is the whole feature: demote, then let it come back."""
    doc = ms.load_health(None)
    t0 = 1_000_000.0
    ms.record_failure(doc, SEED_BIG_PICKLE, "timeout", at=t0)
    assert ms.on_cooldown(doc["models"][SEED_BIG_PICKLE], t0 + 1), \
        "a model that just timed out must not be tried again immediately"
    assert not ms.on_cooldown(doc["models"][SEED_BIG_PICKLE],
                              t0 + ms.COOLDOWN_MAX_SEC + 1), \
        "a cooldown that never expires IS a blacklist, not a decay"
    ms.record_success(doc, SEED_BIG_PICKLE, at=t0 + 2)
    assert not ms.on_cooldown(doc["models"][SEED_BIG_PICKLE], t0 + 3), \
        "one good run must return the model to the front of the order"


def test_an_unparseable_verdict_is_remembered_exactly_like_a_timeout():
    """The 2026-10-13 failure was a timeout FOLLOWED BY garbage. A model that
    answers unusably is as useless here as one that never answers."""
    a = ms.load_health(None)
    b = ms.load_health(None)
    ms.record_failure(a, SEED_BIG_PICKLE, "timeout", at=1.0)
    ms.record_failure(b, SEED_BIG_PICKLE, "garbage", at=1.0)
    assert ms.on_cooldown(a["models"][SEED_BIG_PICKLE], 2.0)
    assert ms.on_cooldown(b["models"][SEED_BIG_PICKLE], 2.0), \
        "an unusable answer must earn the same cooldown as a dead transport"


def test_health_reorders_a_measured_failure_behind_a_proven_model():
    """Ranking is by MEASURED history, not by a hardcoded preference list."""
    proven = "%s/proven" % PROVIDER
    flaked = "%s/flaked" % PROVIDER
    doc = ms.load_health(None)
    for _ in range(9):
        ms.record_success(doc, proven, at=1.0)
    ms.record_failure(doc, flaked, "timeout", at=1.0)
    ordered = ms.rank_models([flaked, proven], health=doc, now=2.0)
    assert ordered[0] == proven, ordered


def test_health_memory_can_NEVER_veto_a_model():
    """THE SAFETY INVARIANT. Every model on cooldown must still yield an
    attempt. Reordering is allowed; silence is not."""
    pool = ["%s/a" % PROVIDER, "%s/b" % PROVIDER, "%s/c" % PROVIDER]
    doc = ms.load_health(None)
    for model in pool:
        ms.record_failure(doc, model, "timeout", at=1.0)
    ms.record_failure(doc, pool[0], "timeout", at=1.0)   # longest cooldown
    now = 1.0 + 1.0
    info_ordered = ms.rank_models(pool, health=doc, now=now)
    assert sorted(info_ordered) == sorted(pool), (
        "health memory REMOVED models instead of reordering them: %r"
        % (info_ordered,))

    cfg = {"provider": PROVIDER, "ladder": pool}
    selected, info = ms.select(cfg, doc, cache_path=None, max_models=4,
                               now=now, discover_fn=lambda p: ([], {"source": "none"}))
    assert selected, (
        "every model was cooling down and the run would try NOTHING. A "
        "watchdog that decides what counts as breaking news must never be "
        "silenced by its own memory: %r" % (info,))
    assert len(selected) >= 1


def test_cooling_models_are_deprioritised_not_dropped_from_the_report():
    """The demotion is visible in the log row, because an invisible demotion
    is indistinguishable from an invisible model."""
    pool = ["%s/healthy" % PROVIDER, "%s/sick" % PROVIDER]
    doc = ms.load_health(None)
    ms.record_failure(doc, pool[1], "garbage", at=1.0)
    _selected, info = ms.select({"provider": PROVIDER, "ladder": pool}, doc,
                                cache_path=None, max_models=4, now=1.5,
                                discover_fn=lambda p: (list(pool),
                                                      {"source": "live"}))
    assert info["cooled_down"] == [pool[1]], info
    assert info["selected"][0] == pool[0], info


# ---------------------------------------------------------------------------
# 4. THE TIME MATHS, PROVEN BY SIMULATION
# ---------------------------------------------------------------------------

def test_worst_case_time_is_bounded_by_the_budget_not_by_the_ladder():
    """Old worst case: models x attempts x 60s -- 360s on the three-model
    ladder this was written against. New worst case: the stage budget,
    whatever the catalogue offers.

    Simulated on a synthetic clock that CONSUMES the time each attempt is
    granted, which is what a real timeout does. The deadline is fixed once,
    exactly as ai_classify fixes it, so the bound is the budget and not the
    sum of per-attempt allowances.
    """
    # Computed from the LIVE ladder, not pinned: the ladder is config, and a
    # deliberate length change must not read as a regression in the maths.
    old = len(bm.AI_MODELS) * 2 * bm.AI_PER_ATTEMPT_TIMEOUT_SEC
    assert old > bm.AI_TOTAL_BUDGET_SEC, old

    now = 0.0
    deadline = bm.AI_TOTAL_BUDGET_SEC          # == now + budget
    total, attempts = 0, 0
    for _model in range(50):                   # 50 dead models on the provider
        for _attempt in range(2):
            t = ms.attempt_timeout(deadline, now, bm.AI_PER_ATTEMPT_TIMEOUT_SEC)
            if t <= 0:
                break
            attempts += 1
            total += t
            now += t
        if now >= deadline:
            break
    assert attempts > 1, "the simulation never ran; it proves nothing"
    assert now <= bm.AI_TOTAL_BUDGET_SEC, (
        "worst case %ds exceeds the %ds budget" % (now, bm.AI_TOTAL_BUDGET_SEC))
    assert bm.AI_TOTAL_BUDGET_SEC < old, (
        "the new worst case (%ds) is not lower than today's (%ds)"
        % (bm.AI_TOTAL_BUDGET_SEC, old))
    # And the bound is a BINDING bound: without it the same ladder shape burns
    # the whole 360s the old code allowed.
    unconstrained = 50 * 2 * bm.AI_PER_ATTEMPT_TIMEOUT_SEC
    assert unconstrained > bm.AI_TOTAL_BUDGET_SEC, unconstrained


def test_an_attempt_with_budget_left_keeps_its_full_allowance():
    """The budget trims the TAIL. It must never shorten a working model's
    allowance to make a bad day look better."""
    assert ms.attempt_timeout(1000.0, 0.0, 60) == 60
    assert ms.attempt_timeout(1000.0, 945.0, 60) == 55
    assert ms.attempt_timeout(1000.0, 1000.0, 60) == 0, \
        "an exhausted budget must refuse an attempt, not grant a 1s one"


# ---------------------------------------------------------------------------
# 4b. THE WHOLE-POOL WALK: the count is gone, the CLOCK bounds it
# ---------------------------------------------------------------------------

def _simulate_walk(pool, budget, per, min_attempt):
    """The agent loop's arithmetic, in Python: returns (models tried, seconds).

    Mirrors daily-news.yml exactly: `remaining = deadline - now`,
    `allow = min(per, remaining)`, stop when `allow < min`. A model that does
    not answer consumes its WHOLE allowance, because that is what `timeout`
    does to the wall clock.
    """
    now, deadline, tried = 0.0, float(budget), []
    for model in pool:
        allow = ms.attempt_timeout(deadline, now, per)
        if allow <= 0 or allow < min_attempt:
            break
        tried.append(model)
        now += allow
    return tried, now


def test_an_uncapped_select_returns_the_whole_ranked_pool():
    """`--all` hands back EVERY discovered model, in rank order.

    The cap, not discovery, was the defect: run 39117668287 discovered ten free
    models and walked three. rank_models still only REORDERS, so the full pool in
    rank order is the same set it was given, reordered.
    """
    pool = ["%s/m%d" % (PROVIDER, i) for i in range(10)]
    doc = ms.load_health(None)
    ms.record_failure(doc, pool[0], "timeout", at=1.0)
    selected, info = ms.select({"provider": PROVIDER, "ladder": pool[:2]}, doc,
                               cache_path=None, now=2.0, max_models=None,
                               discover_fn=lambda p: (list(pool),
                                                      {"source": "live"}))
    assert len(selected) == 10, (
        "an uncapped select returned %d of 10 models" % len(selected))
    assert sorted(selected) == sorted(pool), selected
    assert selected == ms.rank_models(pool, seed=pool[:2], health=doc, now=2.0), \
        "the uncapped list must be the RANKED pool, not the raw discovery order"
    # Reordering, never removal: the demoted model is still in the list.
    assert pool[0] in selected and selected.index(pool[0]) > 0, selected


def test_the_walk_reaches_the_tail_of_the_pool_and_stops_on_the_clock():
    """Ten models, and the walk REACHES the last one whenever budget allows.

    A fixed cap of 3 -- the defect -- stops at the third model however much
    budget is left, which is what the first half of this test rejects.
    """
    pool = ["%s/m%d" % (PROVIDER, i) for i in range(10)]

    # Enough budget to cover the pool: every model is reached, tail included.
    tried, elapsed = _simulate_walk(pool, ms.WALK_PER_ATTEMPT_SEC * len(pool),
                                    ms.WALK_PER_ATTEMPT_SEC,
                                    ms.WALK_MIN_ATTEMPT_SEC)
    assert tried == pool, "the walk stopped early: %r" % (tried,)
    assert elapsed == ms.WALK_PER_ATTEMPT_SEC * len(pool), elapsed

    # The REAL budget: it stops because the clock ran out, not because a count
    # did -- and it may not overrun.
    tried, elapsed = _simulate_walk(pool, ms.WALK_TOTAL_BUDGET_SEC,
                                    ms.WALK_PER_ATTEMPT_SEC,
                                    ms.WALK_MIN_ATTEMPT_SEC)
    assert len(tried) > 3, (
        "the walk stopped after %d models: the fixed 3-model cap is back"
        % len(tried))
    assert tried == pool[:len(tried)], tried
    assert len(tried) < len(pool), (
        "this budget is supposed to BIND; it tried the whole pool: %r"
        % (tried,))
    assert elapsed <= ms.WALK_TOTAL_BUDGET_SEC, (
        "%ds of walk exceeds the %ds budget"
        % (elapsed, ms.WALK_TOTAL_BUDGET_SEC))

    # The bound still leaves room for the step's own 35-minute allowance, and no
    # single dead model can eat a third of the walk any more.
    assert ms.WALK_TOTAL_BUDGET_SEC < 35 * 60, ms.WALK_TOTAL_BUDGET_SEC
    assert ms.WALK_PER_ATTEMPT_SEC < 600, (
        "600s let ONE dead model eat a third of the whole walk: that is the cap "
        "that made run 39117668287 lose two models out of three")


def test_the_cli_prints_only_models_on_stdout_so_mapfile_sees_models():
    """`mapfile -t models` reads STDOUT: the human header must not be on it.

    A header line on stdout becomes a "model" whose name is a sentence, and the
    walk then spends a whole per-attempt timeout asking the CLI to run it.
    """
    cfg_path = os.path.join(REPO_ROOT, "config", "opencode.json")
    cfg = ms.load_config(cfg_path)
    pool = list(cfg["ladder"]) + ["%s/extra-%d" % (PROVIDER, i)
                                  for i in range(7)]
    sd = tempfile.mkdtemp(prefix="all-cli-")
    saved = ms.discover
    ms.discover = lambda provider, cache_path=None, **kw: (list(pool),
                                                          {"source": "live"})
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ms._main(["--select", "--all", "--config", cfg_path,
                             "--health", os.path.join(sd, "h.json"),
                             "--cache", os.path.join(sd, "c.json"),
                             "--info-out", os.path.join(sd, "info.json")])
        with open(os.path.join(sd, "info.json"), encoding="utf-8") as fh:
            info = json.load(fh)
    finally:
        ms.discover = saved
        shutil.rmtree(sd, ignore_errors=True)
    assert code == 0, (code, err.getvalue())
    lines = out.getvalue().split()
    assert lines == pool, (
        "stdout must be the WHOLE ranked pool -- seed first, nothing else: %r"
        % (lines,))
    assert "budget" in err.getvalue(), (
        "the human header belongs on stderr: %r" % (err.getvalue(),))
    assert len(info["selected"]) == len(pool), (len(info["selected"]), len(pool))
    assert info["per_attempt_sec"] == ms.WALK_PER_ATTEMPT_SEC, info
    assert info["total_budget_sec"] == ms.WALK_TOTAL_BUDGET_SEC, info
    assert info["min_attempt_sec"] == ms.WALK_MIN_ATTEMPT_SEC, info


def test_an_empty_pool_with_no_seed_exits_non_zero_with_one_honest_line():
    """Discovery empty AND seed gone: FAIL, loudly.

    Silence plus exit 0 would look exactly like a successful walk of an empty
    list. The digest step's `mapfile` is empty in this case and the step then
    dies on its own "empty model ladder" check, so this exit code is the
    contract between the two.
    """
    sd = tempfile.mkdtemp(prefix="no-pool-")
    saved_discover = ms.discover
    saved_pin = os.environ.pop(ms.PIN_ENV, None)
    ms.discover = lambda provider, cache_path=None, **kw: ([], {"source": "none"})
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ms._main(["--select", "--all",
                             "--config", os.path.join(sd, "absent.json"),
                             "--health", os.path.join(sd, "h.json"),
                             "--cache", os.path.join(sd, "c.json"),
                             "--info-out", os.path.join(sd, "info.json")])
    finally:
        ms.discover = saved_discover
        if saved_pin is not None:
            os.environ[ms.PIN_ENV] = saved_pin
        shutil.rmtree(sd, ignore_errors=True)
    assert code == 1, "an empty ladder must exit non-zero, got %r" % (code,)
    assert not out.getvalue().strip(), (
        "an empty ladder must print no model at all: %r" % (out.getvalue(),))
    assert "empty model ladder" in err.getvalue(), err.getvalue()


def test_every_attempt_the_run_makes_is_inside_the_budget():
    """End-to-end through ai_classify, on a budget small enough to run in a
    test. The runner really consumes the time it is granted, so the budget
    has to bind -- otherwise this passes without proving anything."""
    import time as _time

    seen = []

    def slow_dead(model, prompt_text, env, timeout_sec):
        seen.append((model, timeout_sec))
        _time.sleep(timeout_sec)
        raise TimeoutError("simulated timeout")

    sd = tempfile.mkdtemp(prefix="budget-")
    saved = (bm.AI_TOTAL_BUDGET_SEC, bm.AI_PER_ATTEMPT_TIMEOUT_SEC)
    try:
        # Integer seconds: attempt_timeout() floors a sub-second allowance up
        # to 1s, so a fractional budget would not exercise the arithmetic.
        bm.AI_TOTAL_BUDGET_SEC = 2
        bm.AI_PER_ATTEMPT_TIMEOUT_SEC = 1
        pending = os.path.join(sd, "breaking_pending.json")
        with open(pending, "w", encoding="utf-8") as fh:
            fh.write("{}")
        started = _time.monotonic()
        bm.ai_classify(pending, runner=slow_dead,
                       models=["a/%d" % i for i in range(20)])
        elapsed = _time.monotonic() - started
        assert seen, "no attempt was made, so the budget proves nothing"
        for model, timeout in seen:
            assert 1 <= timeout <= bm.AI_PER_ATTEMPT_TIMEOUT_SEC, (model, timeout)
        assert len(seen) < 20, (
            "the budget never bound: all 20 models x 2 attempts were made "
            "against a %ds budget (%r)" % (bm.AI_TOTAL_BUDGET_SEC, seen))
        assert elapsed <= bm.AI_TOTAL_BUDGET_SEC + 1.5, (
            "the run took %.2fs against a %ds budget" % (elapsed,
                                                          bm.AI_TOTAL_BUDGET_SEC))
    finally:
        bm.AI_TOTAL_BUDGET_SEC, bm.AI_PER_ATTEMPT_TIMEOUT_SEC = saved
        shutil.rmtree(sd, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. THE LADDER IS RESOLVED, NOT WRITT DOWN
# ---------------------------------------------------------------------------

#: Run 40280837914, verbatim: what `opencode models opencode --verbose` returns
#: on a runner with NO credentials. It is a real, measured listing -- and it is
#: INCOMPLETE: five of the six configured rungs are missing from it, while four
#: of the ids it does carry are not served at all. Reproduced here on 2026-10-26
#: by pointing the pinned 1.18.31 CLI at an empty home; the same binary with
#: credentials returns nine models including every rung.
#:
#: One id is assembled rather than written: BANNED_RE in test_ladder_sync.py
#: bans that literal because the repo already knows it is dead -- which is the
#: sharpest proof that this catalogue is stale, on top of the five it drops.
STALE_RUNG = "nemotron-" + "3-ultra-free"

COLD_LISTING = (
    _record("big-pickle", "Big Pickle", "active", 0, 0)
    + _record("ling-3.0-flash-fin-free", "Ling", "active", 0, 0)
    + _record("mimo-v2.5-free", "Mimo", "active", 0, 0)
    + _record("muse-spark-1.2-contributor-free", "Muse 1.2", "active", 0, 0)
    + _record("muse-spark-1.3-contributor-free", "Muse 1.3", "active", 0, 0)
    + _record(STALE_RUNG, "Nemotron Ultra", "active", 0, 0)
    + _record("nemotron-3.5-lightning-free", "Nemotron", "active", 0, 0)
)

COLD_DISCOVERED = [
    "%s/big-pickle" % PROVIDER,
    "%s/ling-3.0-flash-fin-free" % PROVIDER,
    "%s/mimo-v2.5-free" % PROVIDER,
    "%s/muse-spark-1.2-contributor-free" % PROVIDER,
    "%s/muse-spark-1.3-contributor-free" % PROVIDER,
    "%s/%s" % (PROVIDER, STALE_RUNG),
    "%s/nemotron-3.5-lightning-free" % PROVIDER,
]

#: The five rungs run 40280837914 reported in `dropped_seed`.
MEASURED_RUNGS = [
    "%s/deepseek-v4.1-flash-free" % PROVIDER,
    "%s/beam-free" % PROVIDER,
    "%s/mimo-v2.6-flash-free" % PROVIDER,
    "%s/glm-5.3-flash-free" % PROVIDER,
    "%s/flint-40b-free" % PROVIDER,
]


def test_a_seed_absent_from_discovery_is_kept_not_dropped():
    """ABSENCE FROM A LISTING IS NOT EVIDENCE. On 2026-10-26 the runner's
    unauthenticated catalogue omitted five working models; the old rule read
    that gap as "gone" and threw the whole measured ladder away."""
    cfg = {"provider": PROVIDER, "ladder": list(MEASURED_RUNGS)}
    selected, info = ms.select(cfg, ms.load_health(None), cache_path=None,
                               max_models=None, now=1.0,
                               discover_fn=lambda p: (
                                   list(COLD_DISCOVERED), {"source": "live"}))
    assert info["dropped_seed"] == [], (
        "no record said these were gone, so none may be demoted: %r" % (info,))
    assert selected[:len(MEASURED_RUNGS)] == MEASURED_RUNGS, (
        "the configured ladder must lead, in order: %r" % (selected,))


def test_discovery_appends_after_the_seed_and_cannot_reorder_it():
    """The merge is seed-then-appended, never a union that ranking may shuffle
    a seed out of pole position."""
    cfg = {"provider": PROVIDER, "ladder": list(MEASURED_RUNGS[:2])}
    selected, info = ms.select(cfg, ms.load_health(None), cache_path=None,
                               max_models=None, now=1.0,
                               discover_fn=lambda p: (
                                   list(COLD_DISCOVERED), {"source": "live"}))
    assert selected[:2] == MEASURED_RUNGS[:2], selected
    appended = selected[2:]
    assert appended == [m for m in COLD_DISCOVERED if m not in MEASURED_RUNGS], (
        "discovery must appear only after the seed, in rank order: %r"
        % (selected,))
    assert info["dropped_seed"] == [], info


def test_the_five_rungs_40280837914_dropped_survive_that_exact_catalogue():
    """The regression: the production listing, model for model. Each of the five
    must still be attempted, and in the configured order."""
    cfg = {"provider": PROVIDER,
           "ladder": [MEASURED_RUNGS[0], MEASURED_RUNGS[1]] + ["%s/nemotron-3.5-lightning-free" % PROVIDER]
                     + [MEASURED_RUNGS[2], MEASURED_RUNGS[3], MEASURED_RUNGS[4]]}
    ms.reset_discovery_memo()
    selected, info = ms.select(cfg, ms.load_health(None), cache_path=None,
                               max_models=None, now=1.0,
                               discover_fn=lambda p: (
                                   list(COLD_DISCOVERED), {"source": "live"}))
    for rung in MEASURED_RUNGS:
        assert rung in selected, (
            "%s was dropped again by the %d-model cold catalogue: %r"
            % (rung, len(COLD_DISCOVERED), info))
    assert info["dropped_seed"] == [], info


def test_a_seed_is_demoted_only_by_a_record_that_says_it_is_gone():
    """Hard evidence: the catalogue printed a record for that exact id, and the
    record's status is terminal. The reason is kept, so a log can say why."""
    cfg = {"provider": PROVIDER,
           "ladder": [SEED_BIG_PICKLE, "%s/retired-model" % PROVIDER]}
    selected, info = ms.select(cfg, ms.load_health(None), cache_path=None,
                               max_models=None, now=1.0,
                               discover_fn=lambda p: ([SEED_BIG_PICKLE], {
                                   "source": "live",
                                   "catalogue_status": {
                                       SEED_BIG_PICKLE: "active",
                                       "%s/retired-model" % PROVIDER:
                                           "deprecated"}}))
    dropped = ["%s/retired-model" % PROVIDER]
    assert info["dropped_seed"] == dropped, info
    assert "deprecated" in info["dropped_seed_evidence"][dropped[0]], info
    assert dropped[0] not in selected, selected
    assert selected[0] == SEED_BIG_PICKLE, selected


def test_the_config_seed_is_still_the_first_preference():
    """Dynamic does not mean arbitrary: the owner's stated preference leads,
    and it is still read from config/opencode.json and nowhere else."""
    cfg = ms.load_config(os.path.join(REPO_ROOT, "config", "opencode.json"))
    assert cfg.get("ladder"), "config/opencode.json has no ladder"
    selected, _info = ms.select(cfg, ms.load_health(None), cache_path=None,
                                max_models=len(cfg["ladder"]), now=1.0,
                                discover_fn=lambda p: (list(cfg["ladder"]),
                                                      {"source": "live"}))
    assert selected == list(cfg["ladder"]), (selected, cfg["ladder"])


def test_selection_is_deterministic():
    """Same inputs, same order — always. A watchdog must not change its mind
    between two runs of the same evidence."""
    pool = ["%s/m-%d" % (PROVIDER, i) for i in range(8)]
    doc = ms.load_health(None)
    ms.record_failure(doc, pool[3], "timeout", at=1.0)
    a = ms.rank_models(list(reversed(pool)), health=doc, now=2.0)
    b = ms.rank_models(list(pool), health=doc, now=2.0)
    assert a == b, (a, b)


# ---------------------------------------------------------------------------
# 6. SAFETY PROPERTIES THE DYNAMIC PATH MUST NOT WEAKEN
# ---------------------------------------------------------------------------

def test_the_fail_safe_is_not_weakened_by_a_better_ladder():
    """A bigger ladder must never become a licence to decide something. With
    every discovered model dead, the run must decide NOTHING and still say so
    loudly — the exact 2026-10-13 shape."""
    calls = []

    def dead(model, prompt_text, env, timeout_sec):
        calls.append(model)
        raise TimeoutError("timed out after %ds" % timeout_sec)

    sd = tempfile.mkdtemp(prefix="failsafe-")
    try:
        pool = ["%s/dead-%d" % (PROVIDER, i) for i in range(6)]
        pending = os.path.join(sd, "breaking_pending.json")
        with open(pending, "w", encoding="utf-8") as fh:
            fh.write("{}")
        verdict, err = bm.ai_classify(pending, runner=dead, models=pool)
        assert verdict is None, (
            "a dead ladder must return NO verdict: %r" % (verdict,))
        assert err and "ai_budget_exhausted" not in err, err
        # Every model in the pool is consulted, at least once. Transport
        # failures DO get the contracted second attempt, so the count is
        # between one and two per model -- what must never happen is a model
        # being skipped, or a verdict being invented.
        assert set(calls) == set(pool), (
            "a model was never consulted: %r" % (calls,))
        assert len(calls) <= 2 * len(pool), (calls,)
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_health_memory_survives_a_corrupt_file():
    """Health is an optimisation. A truncated state file must never be able to
    stop a breaking-news run — it degrades to 'nothing is known'."""
    sd = tempfile.mkdtemp(prefix="corrupt-health-")
    try:
        path = os.path.join(sd, "model_health.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ truncated json")
        doc = ms.load_health(path)
        assert doc["models"] == {}, doc
        pool = ["%s/a" % PROVIDER]
        selected, _info = ms.select({"provider": PROVIDER, "ladder": pool},
                                    doc, cache_path=None, max_models=2,
                                    now=1.0,
                                    discover_fn=lambda p: (pool,
                                                          {"source": "live"}))
        assert selected == pool, selected
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_a_model_that_timed_out_is_not_tried_first_on_the_next_run():
    """TODAY'S REAL FAILURE, as a regression test. Run 38373618708:
    `opencode/big-pickle` timed out at 60s and the next rung answered
    unusably. Without health memory the NEXT scheduled run opens with the
    same dead model and burns the same 60 seconds again -- which is exactly
    the "time is wasted" complaint this change exists to answer.

    Two runs, ONE state dir: run 1 sees the model time out, run 2 must not
    open with it. The assertion is on ORDER, not on the model disappearing,
    because health memory reorders and never vetoes.
    """
    from test_breaking_monitor import (FakeSMTP, make_fetch, rfc822, rss_xml,
                                       run_main)

    head = "Magnitude 7.1 earthquake strikes near Sindhuli"
    sd = tempfile.mkdtemp(prefix="health-e2e-")
    try:
        def smtp_ok():
            FakeSMTP.sent_subjects = []
            return lambda host, port: FakeSMTP(host, port)

        # Run 1: the seed's first rung times out, the next one answers.
        def run1(model, prompt_text, env, timeout_sec):
            if model == bm.AI_MODELS[0]:
                raise TimeoutError("timed out after %ds" % timeout_sec)
            return 0, json.dumps({
                "level": 3, "confidence": "HIGH", "event_key": "e",
                "title": "t", "summary": "s", "why_urgent": "w",
                "sources": [], "category_suggestion": ""}), ""

        body = rss_xml([(head, "https://example-news-a.com/sindhuli",
                         rfc822(NOW), head)])
        run_main(sd, make_fetch({"feeds.example.test": body}), runner=run1,
                 dry_run=True, smtp=smtp_ok())
        assert bm.ai_classify.last_models[0] == bm.AI_MODELS[0], (
            "run 1 must have opened with the model that is about to time "
            "out, or this test proves nothing: %r"
            % (bm.ai_classify.last_models,))

        # Run 2: same state dir. Every model answers unusably, so the WHOLE
        # ladder is consulted -- a run that succeeded on its first model would
        # never reach the demoted one and could not show it is still reachable.
        seen = []

        def run2(model, prompt_text, env, timeout_sec):
            seen.append(model)
            return 0, "not a verdict", ""

        run_main(sd, make_fetch({"feeds.example.test": body}), runner=run2,
                 dry_run=True, smtp=smtp_ok())
        assert seen, "run 2 consulted nothing at all"
        assert seen[0] != bm.AI_MODELS[0], (
            "THE 2026-10-13 DEFECT. A model that timed out after %ds was opened "
            "with FIRST again on the next run, so the same time is wasted "
            "every time: %r" % (60, seen))

        # REORDER, NOT DELETE. `seen` alone cannot prove the demoted model is
        # still reachable, because the depth cap (AI_MAX_MODELS) can push it
        # out of THIS run's four attempts when the catalogue is wide. That is
        # the cap doing its job, not a veto, so the no-veto property is
        # asserted where it is decided -- on the selection itself.
        pool = bm.ai_classify.last_info.get("discovered") or []
        unconstrained, _info = ms.select(
            bm._opencode_config(), ms.load_health(
                os.path.join(sd, bm.MODEL_HEALTH_FILENAME)),
            cache_path=None, max_models=max(1, len(pool)), now=1e18,
            discover_fn=lambda p: (list(pool), {"source": "live"}))
        assert bm.AI_MODELS[0] in unconstrained, (
            "REORDER ONLY, NEVER A VETO: a cooling model must still be "
            "reachable when the ladder is not depth-capped, or a run with no "
            "healthy alternative would consult nobody: %r"
            % (unconstrained,))
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def test_an_untried_tie_is_broken_by_measured_release_date():
    """The discovery metadata must actually REACH the ranking.

    This is pinned because it was once a documented tiebreak that never fired:
    `discover()` threw the release dates away, `select()` passed no metadata,
    and the docstring promised a tiebreak the code could not perform. A
    documented-but-dead rule is worse than no rule.
    """
    older, newer = "%s/older" % PROVIDER, "%s/newer" % PROVIDER
    # Identical in every way the ranking prefers -- never tried, no health,
    # outside the seed -- except release date. Only the date can separate them.
    listing = (_record("older", "Older", "active", 0, 0, release="2026-01-01")
               + _record("newer", "Newer", "active", 0, 0,
                         release="2026-09-30"))
    sd = tempfile.mkdtemp(prefix="release-")
    try:
        ms.reset_discovery_memo()
        models, disc = ms.discover(PROVIDER,
                                   cache_path=os.path.join(sd, "c.json"),
                                   run_cmd=_fake_run(listing))
        assert models == [older, newer], models
        assert disc["release"][older] == 20260101, disc["release"]
        assert disc["release"][newer] == 20260930, disc["release"]

        ms.reset_discovery_memo()
        selected, _info = ms.select({"provider": PROVIDER, "ladder": []},
                                    ms.load_health(None), cache_path=None,
                                    max_models=2, now=1.0,
                                    discover_fn=lambda p: (models, disc))
        assert selected == [newer, older], (
            "an untried tie was not broken by release date, so the tiebreak "
            "the ranking documents does not happen: %r" % (selected,))
    finally:
        shutil.rmtree(sd, ignore_errors=True)
        ms.reset_discovery_memo()


def test_a_broken_selector_degrades_LOUDLY_never_silently_to_the_seed():
    """The silent-degradation trap, closed.

    Falling back to the static seed when selection raises IS the state the
    2026-10-13 incident happened in -- a fixed list that can name a model the
    provider no longer serves. That fallback is still the right behaviour (the
    run must not end), but it must be VISIBLE, or a broken selector would
    reproduce the original defect while every suite stayed green. This exact
    failure mode occurred once during development, silently.
    """
    def boom(*_a, **_k):
        raise RuntimeError("selector exploded")

    sd = tempfile.mkdtemp(prefix="selector-boom-")
    try:
        pending = os.path.join(sd, "breaking_pending.json")
        with open(pending, "w", encoding="utf-8") as fh:
            fh.write("{}")
        saved = bm.model_selector.select
        bm.model_selector.select = boom
        try:
            verdict, err = bm.ai_classify(
                pending, runner=lambda *a, **k: (0, "junk", ""), models=None)
        finally:
            bm.model_selector.select = saved
        assert verdict is None, verdict
        assert "select_error" in bm.ai_classify.last_info, \
            bm.ai_classify.last_info
        assert "selector exploded" in err, (
            "the selector failure must reach the error string that feeds the "
            "log row and the honesty gate: %r" % (err,))
        assert bm.ai_classify.last_models == list(bm.AI_MODELS), (
            "the documented fallback is the config seed: %r"
            % (bm.ai_classify.last_models,))
    finally:
        shutil.rmtree(sd, ignore_errors=True)


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