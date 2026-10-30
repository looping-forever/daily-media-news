#!/usr/bin/env python3
"""tests/breaking/test_ladder_sync.py — CLI/ladder/flag drift guard.

P0 hardening: two silent digest kills in 2 days came from CLI/ladder/flag
drift (stale model IDs, a v2-only flag). This test pins the contract:

* config/opencode.json is the SINGLE source of truth for the CLI pin, the
  version floor, the model ladder and the run flags.
* No stale identifiers (nemotron-3-ultra, hy3-free, --standalone) survive in
  *.yml / *.py / prompts/* except explicitly allowlisted history comments.
* No `opencode/<model>` literal is hardcoded in .github workflows — they
  resolve the ladder at run time via scripts/model_selector.py (the
  config/opencode.json seed plus live provider discovery).
* The breaking monitor (scripts/breaking_monitor.py AI_MODELS) and the
  classifier (scripts/ai_classifier.py PROVIDERS) resolve to exactly the
  config ladder at import time.

Plain asserts, no test framework, stdlib only. Run:

    py tests\\breaking\\test_ladder_sync.py

Unlike the older suites, this file exits NONZERO on failure so the
drift-canary docs-lint job can gate on it.
"""

import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

CONFIG_PATH = os.path.join(REPO_ROOT, "config", "opencode.json")
DAILY_YML = os.path.join(REPO_ROOT, ".github", "workflows", "daily-news.yml")
BREAKING_YML = os.path.join(REPO_ROOT, ".github", "workflows",
                            "breaking-monitor.yml")

EXPECTED_PIN = "opencode-ai@1.18.31"
EXPECTED_MIN_VERSION = "1.17.0"
EXPECTED_PROVIDER = "opencode"
# CHANGED DELIBERATELY ON 2026-10-25, with the reason inline. The old seed
# (big-pickle, mimo-v2.6, nemotron-3.5) was not the live ladder: CI discovered
# seven IDs and four of them (muse-spark-1.3, muse-spark-1.2, ling-3.0-fin,
# mimo-v2.5) were absent from the catalogue, each costing an attempt. This list
# is the order the latency probe measured, and it is still only the SEED: live
# discovery decides what exists at run time.
# FOUR, NOT SIX became SIX, NOT FOUR on the same day, for the opposite reason:
# breaking_monitor's AI_MAX_MODELS was raised 4 -> 6 in the same change, so the
# cap can no longer silently make a configured rung unreachable. Keep the two in
# step -- test_breaking_fast_path asserts selected >= len(AI_MODELS) and
# <= AI_MAX_MODELS, which is the invariant that catches a drift either way.
# REORDERED 2026-10-26 FROM MEASUREMENT, not taste. The full ~65k-token prompt,
# serial, one model at a time: deepseek WORKED (first token 14.59s, complete
# 127.53s, 4 stories); beam WORKED but slow (first token 137.63s, 138.56s, 6
# stories); mimo / nemotron / glm produced no assistant text by 150s; flint was
# a HARD FAILURE. So the two models that have ever produced a digest are rungs
# 1 and 2 -- the order a walk actually hits them in a bad week. flint is LAST,
# deliberately NOT removed: it is a known hard failure, but it costs one silence
# window when it fails, and dropping it would lose a rung if it recovers.
# GEMINI LED 2026-10-30, deliberately, and for a reason the earlier ordering
# cannot supply: Google AI Studio serves ~15 RPM / 500 RPD per project and per
# model with no billing account, while the opencode free pool refuses whole
# walks at once. So the two independent free buckets are tried in the order
# most likely to answer, and the opencode pool stays as the fallback -- the
# same six rungs, unchanged and still in their measured order. Seeds lead;
# discovery only appends (see model_selector.select).
EXPECTED_LADDER = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.1-flash-lite",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/beam-free",
    "opencode/mimo-v2.6-flash-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/glm-5.3-flash-free",
    "opencode/flint-40b-free",
]

#: The four IDs CI tried on 2026-10-25 that the catalogue did not serve. This is
#: a REGRESSION PIN for the config seed ONLY -- production never uses a denylist;
#: discovery filters on the catalogue's own `status` field (see usable()).
DEAD_IDS_2026_10_25 = (
    "opencode/muse-spark-1.3-contributor-free",
    "opencode/muse-spark-1.2-contributor-free",
    "opencode/ling-3.0-flash-fin-free",
    "opencode/mimo-v2.5-free",
)
EXPECTED_FLAGS = ["--auto", "--model"]

# Stale identifiers that silently killed digests before. Case-sensitive:
# live model IDs and the v2-only flag only. Bare prose words such as
# "standalone" (no dashes) are NOT matched.
BANNED_RE = re.compile(r"nemotron-3-ultra|hy3-free|--standalone")

# Any hardcoded `opencode/<model>` literal. Workflows must read the ladder
# from config/opencode.json instead (this file itself is excluded: it pins
# the expected values by design).
MODEL_LITERAL_RE = re.compile(r"opencode/[A-Za-z0-9][A-Za-z0-9_.\-]*")

# THE HARDCODE GUARD (added with dynamic model selection, 2026-10-13).
#
# The ladder used to be an ordered list duplicated as an EMBEDDED FALLBACK
# TUPLE inside every consumer, and that second copy is what rotted: on that
# day rung 2, `opencode/mimo-v2.5-free`, was no longer served by Zen at all,
# so every run spent real wall-clock discovering a model was gone — on the
# days the provider was sickest and the alert mattered most.
#
# So this is now a standing guard: the ladder-CONSUMPTION code carries no
# model ID of its own. config/opencode.json stays the single source, the
# provider catalogue is discovered at run time (scripts/model_selector.py),
# and a model literal can only reappear in a consumer as a COMMENT
# explaining the incident that motivated this — which is allowlisted per
# file, exactly like the workflow history notes below.
LADDER_CONSUMERS = ("scripts/breaking_monitor.py", "scripts/ai_classifier.py")
CONSUMER_HISTORY_NOTE = "2026-10-13"


def _model_literals_outside_history_comments(rel):
    """Model IDs in `rel` that are NOT part of a comment documenting the
    incident that motivated this change.

    Scoped to contiguous `#` blocks rather than single lines, because the
    incident note is a paragraph: the date sits on one line and the model it
    names on the next. Anything outside such a block -- code, a docstring,
    a stray line -- is a violation, so history can be recorded without being
    able to smuggle a live ladder back in.
    """
    path = os.path.join(REPO_ROOT, rel)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise AssertionError("cannot read %s: %s" % (rel, exc))

    violations, block = [], []
    for lineno, line in enumerate(lines + [""], 1):
        if line.strip().startswith("#"):
            block.append((lineno, line))
            continue
        if block:
            documented = any(CONSUMER_HISTORY_NOTE in text for _, text in block)
            for blineno, btext in block:
                m = MODEL_LITERAL_RE.search(btext)
                if m and not documented:
                    violations.append("%s:%d: %s"
                                      % (rel, blineno, btext.strip()[:160]))
            block = []
        m = MODEL_LITERAL_RE.search(line)
        if m:
            violations.append("%s:%d: %s" % (rel, lineno, line.strip()[:160]))
    return violations

# file (repo-relative, / separators) -> allowed substrings. A scanned line
# matching BANNED_RE passes only when it contains one of its file's
# substrings. Allowed contexts are deliberately narrow:
#   "v2/opencode2-only" ....... history NOTEs explaining why the v2-only
#                               flag must never be re-added
#   "grep -q -- --standalone" . the canary gate check itself
#   "exposes --standalone" .... gate failure message
#   "--standalone absent" ..... gate success message
#   "exclude=PKB.md" .......... the docs-lint sweep's own search pattern
# The runtime verify-gate (opencode run --help) remains the real
# enforcement; this static scan is defense-in-depth against accidental
# drift, not against a determined adversary.
_GATE_PHRASES = ("v2/opencode2-only",
                 "grep -q -- --standalone",
                 "exposes --standalone",
                 "--standalone absent")
ALLOW_SUBSTRINGS = {
    ".github/workflows/daily-news.yml": _GATE_PHRASES,
    ".github/workflows/breaking-monitor.yml": _GATE_PHRASES,
    ".github/workflows/drift-canary.yml": _GATE_PHRASES + ("exclude=PKB.md",),
    "scripts/breaking_monitor.py": ("v2/opencode2-only",),
}

SELF = os.path.abspath(__file__)
SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _scan_files():
    """Yield (relpath, lineno, line) for every scanned line."""
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            path = os.path.join(root, name)
            if os.path.abspath(path) == SELF:
                continue  # this file pins patterns/values by design
            rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
            _, ext = os.path.splitext(name)
            if ext in (".yml", ".yaml", ".py") or rel.startswith("prompts/"):
                try:
                    with open(path, encoding="utf-8") as fh:
                        for lineno, line in enumerate(fh, 1):
                            yield rel, lineno, line.rstrip("\n")
                except (OSError, UnicodeDecodeError):
                    continue


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_config_contract():
    """config/opencode.json pins the exact CLI contract."""
    cfg = _load_config()
    assert cfg.get("pin") == EXPECTED_PIN, \
        "pin drift: %r" % (cfg.get("pin"),)
    assert cfg.get("min_version") == EXPECTED_MIN_VERSION, \
        "min_version drift: %r" % (cfg.get("min_version"),)
    assert cfg.get("provider") == EXPECTED_PROVIDER, \
        "provider drift: %r" % (cfg.get("provider"),)
    assert cfg.get("ladder") == EXPECTED_LADDER, \
        "ladder drift: %r" % (cfg.get("ladder"),)
    dead = [m for m in (cfg.get("ladder") or []) if m in DEAD_IDS_2026_10_25]
    assert not dead, (
        "the config ladder names model IDs the catalogue did not serve on "
        "2026-10-25: %r. A seed rung that cannot answer is a three-second tax "
        "on every walk that reaches it." % (dead,))
    assert len(set(cfg.get("ladder") or [])) == len(cfg.get("ladder") or []), \
        "the ladder must not repeat a rung"
    assert cfg.get("flags") == EXPECTED_FLAGS, \
        "flags drift: %r" % (cfg.get("flags"),)
    print("  PASS: test_config_contract")


def test_no_stale_references():
    """No stale model IDs / --standalone in *.yml, *.py, prompts/*."""
    violations = []
    for rel, lineno, line in _scan_files():
        if BANNED_RE.search(line):
            allowed = ALLOW_SUBSTRINGS.get(rel, ())
            if not any(s in line for s in allowed):
                violations.append("%s:%d: %s"
                                  % (rel, lineno, line.strip()[:160]))
    assert not violations, \
        "stale CLI/ladder/flag references:\n" + "\n".join(violations)
    print("  PASS: test_no_stale_references")


def test_no_hardcoded_models_in_workflows():
    """Workflows resolve the ladder at run time — never hardcode models."""
    violations = []
    for rel, lineno, line in _scan_files():
        if not rel.startswith(".github/"):
            continue
        m = MODEL_LITERAL_RE.search(line)
        if m and not any(s in line
                         for s in ALLOW_SUBSTRINGS.get(rel, ())):
            violations.append("%s:%d: %s"
                              % (rel, lineno, line.strip()[:160]))
    assert not violations, \
        "hardcoded opencode/<model> in workflows:\n" + "\n".join(violations)
    with open(DAILY_YML, encoding="utf-8") as fh:
        daily = fh.read()
    assert "scripts/model_selector.py --select" in daily, \
        ("daily-news.yml must resolve the model ladder dynamically via "
         "scripts/model_selector.py")
    assert "jq -r .pin config/opencode.json" in daily, \
        "daily-news.yml must read the pin from config/opencode.json"
    assert "jq -r .min_version config/opencode.json" in daily, \
        "daily-news.yml must read min_version from config/opencode.json"

    # RE-APPROVED 2026-10-18 (was: this step must pass `--max 3`). The cap WAS
    # the defect: run 39117668287 discovered ten free models, walked exactly
    # three, lost two to the 600s cap and the third to rc=1, and produced no
    # digest while seven models sat untried. The walk is bounded by wall clock
    # now, so a count in this step is that defect coming back.
    step = daily.split("- name: Run OpenCode agent", 1)[1]
    step = step.split("- name: Validate stories", 1)[0]
    assert "--all" in step, \
        "the agent step must ask the selector for the FULL ranked pool"
    assert "--max" not in step, \
        "the agent step must not cap the pool to N models again"
    for key in (".total_budget_sec", ".per_attempt_sec", ".min_attempt_sec"):
        assert key in step, \
            "the agent step must read %s back from the selector" % key
    assert "deadline" in step and 'timeout "$allow"' in step, \
        "the walk must be bounded by a deadline this step computes itself"
    # ADDED 2026-10-25 with the circuit breaker, deliberately: the walk still
    # owns its own deadline and `timeout`, but an attempt is now wrapped in the
    # silence watchdog and its failure is CLASSIFIED before the next model is
    # tried. Without these the breaker could be deleted and this guard would
    # still pass.
    # CHANGED 2026-10-26: the silence window is PER MODEL, so the watchdog is
    # handed the model id and resolves its own measured window (deepseek 60s,
    # beam 240s). A single global number is the defect: 60s would make beam --
    # the only other model that has produced a digest -- unreachable forever.
    assert "model_watchdog.py" in step, \
        "each attempt must run under the no-progress watchdog"
    assert '--model "$model"' in step, \
        ("the watchdog must be told WHICH model it supervises, so the "
         "per-model window table can be resolved")
    assert '--no-output-sec "$NOOUT"' not in step, \
        ("the walk must not pass one global silence window to every attempt; "
         "the window is measured per model")
    assert "--classify" in step and "model_not_found" in step \
        and "auth" in step, \
        "the walk must classify each failure and act on model_not_found/auth"
    print("  PASS: test_no_hardcoded_models_in_workflows")


def test_breaking_workflow_reads_config():
    """breaking-monitor.yml reads pin/floor from config; ladder via script."""
    with open(BREAKING_YML, encoding="utf-8") as fh:
        text = fh.read()
    assert "jq -r .pin config/opencode.json" in text, \
        "breaking-monitor.yml must read the pin from config/opencode.json"
    assert "jq -r .min_version config/opencode.json" in text, \
        "breaking-monitor.yml must read min_version from config/opencode.json"
    assert "config/opencode.json" in text, \
        "breaking-monitor.yml must point at config/opencode.json"
    print("  PASS: test_breaking_workflow_reads_config")


def test_breaking_ladder_matches_config():
    """breaking_monitor.AI_MODELS == the ladder its CLI can RESOLVE.

    CHANGED 2026-10-30 with the second provider. The breaking walk runs the
    same `opencode` CLI, and a provider only exists for it when its own config
    was loaded. A rung that cannot resolve fails as `model_not_found` and earns
    a 24h hard cooldown in the state file the DIGEST walk also reads, so an
    unreachable rung is worse than no rung: the module drops it instead.
    EXPERT here runs with OPENCODE_CONFIG unset, i.e. the digest-pool-only case.
    """
    import breaking_monitor as bm  # noqa: E402

    cfg = _load_config()
    reachable = [m for m in cfg["ladder"] if m.startswith(cfg["provider"] + "/")]
    assert tuple(bm.AI_MODELS) == tuple(reachable), \
        "AI_MODELS %r != the reachable ladder %r" % (bm.AI_MODELS, reachable)
    assert tuple(bm.AI_MODELS) == tuple(
        m for m in EXPECTED_LADDER if m.startswith("opencode/"))
    print("  PASS: test_breaking_ladder_matches_config")


def test_classifier_ladder_matches_config():
    """ai_classifier PROVIDERS resolve to the config ladder it can SERVE.

    CHANGED 2026-10-30 with the second provider. The classifier posts to one
    endpoint (api.opencode.ai, i.e. the config's own `provider` field), while
    the ladder is shared with the CLI walk, which now leads with a Gemini
    provider that endpoint does not route. A gemini id posted there is a
    guaranteed 404 that would burn one of the four race slots, so the module
    filters by the config's `provider` prefix -- and this pin follows it.
    """
    import ai_classifier as ac  # noqa: E402

    cfg = _load_config()
    servable = [m for m in cfg["ladder"] if m.startswith(cfg["provider"] + "/")]
    models = [p["model"] for p in ac.PROVIDERS]
    assert models == servable, \
        "PROVIDERS %r != the servable ladder %r" % (models, servable)
    assert models == [m for m in EXPECTED_LADDER if m.startswith("opencode/")]
    assert not [m for m in models if not m.startswith("opencode/")], (
        "the classifier must never carry a rung its endpoint cannot route: %r"
        % (models,))
    names = [p["name"] for p in ac.PROVIDERS]
    assert len(set(names)) == len(names) and all(names), \
        "provider names must be unique and non-empty: %r" % (names,)
    print("  PASS: test_classifier_ladder_matches_config")


def test_run_flags_present():
    """Both opencode invocations use the contracted --auto/--model flags."""
    with open(DAILY_YML, encoding="utf-8") as fh:
        daily = fh.read()
    assert "--auto" in daily and "--model" in daily, \
        "daily-news.yml agent step must pass --auto/--model"
    bm_path = os.path.join(REPO_ROOT, "scripts", "breaking_monitor.py")
    with open(bm_path, encoding="utf-8") as fh:
        script = fh.read()
    assert '"--auto"' in script and '"--model"' in script, \
        "breaking_monitor default_ai_runner must pass --auto/--model"
    print("  PASS: test_run_flags_present")


def test_no_model_id_is_hardcoded_in_the_ladder_consumers():
    """THE GUARD AGAINST THE 2026-10-13 BRITTLENESS COMING BACK.

    The ladder is resolved at run time: config/opencode.json supplies the
    owner's seed and the provider catalogue supplies everything else. A model
    ID inside a consumer is therefore always a mistake — and when it was not
    a mistake, it was a copy of the ladder that went stale silently while the
    suite stayed green.

    A line carrying a model ID passes only if it is a COMMENT that names the
    incident, so history can be recorded without being able to smuggle a live
    ladder back in.
    """
    scanned = 0
    violations = []
    for rel in LADDER_CONSUMERS:
        assert os.path.isfile(os.path.join(REPO_ROOT, rel)), \
            "%s is a guarded ladder consumer but does not exist" % rel
        scanned += 1
        violations.extend(_model_literals_outside_history_comments(rel))
    assert scanned == len(LADDER_CONSUMERS), (
        "the guard did not scan every ladder consumer it claims to")
    assert not violations, (
        "a model ID is hardcoded in ladder-consumption code. The ladder is "
        "config/opencode.json + live discovery (scripts/model_selector.py); "
        "an embedded copy is what rotted on 2026-10-13:\n"
        + "\n".join(violations))
    print("  PASS: test_no_model_id_is_hardcoded_in_the_ladder_consumers")


def test_the_measured_working_models_lead_and_the_known_failure_is_last():
    """THE ORDER IS THE MEASUREMENT (2026-10-26, full ~65k-token prompt).

    deepseek worked (14.59s to first token, 127.53s complete) and beam worked
    but slow (137.63s to first token), so those two must be rungs 1 and 2: a
    bad week must spend its time on the models that answer. flint is a KNOWN
    HARD FAILURE and is kept as the LAST rung rather than deleted -- it costs
    one silence window when it fails, and removing it would lose a rung if it
    recovers. This test fails if someone tidies flint away or promotes it.
    """
    ladder = _load_config().get("ladder") or []
    opencode = [m for m in ladder if m.startswith("opencode/")]
    assert opencode[:2] == ["opencode/deepseek-v4.1-flash-free",
                            "opencode/beam-free"], (
        "the two opencode models that have produced a digest must lead the "
        "POOL, in measured order: %r" % (opencode[:2],))
    assert ladder[:2] == ["gemini/gemini-3.5-flash-lite",
                          "gemini/gemini-3.1-flash-lite"], (
        "the free Gemini rungs lead the whole ladder (2026-10-30): a separate "
        "free bucket that is not the shared pool is worth trying first: %r"
        % (ladder[:2],))
    assert ladder[-1] == "opencode/flint-40b-free", (
        "the known hard failure must be the LAST rung, not removed and not "
        "promoted: %r" % (ladder[-1] if ladder else None,))
    assert ladder.count("opencode/flint-40b-free") == 1, \
        "flint may appear exactly once: %r" % (ladder,)
    print("  PASS: test_the_measured_working_models_lead_and_the_known_failure"
          "_is_last")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_config_contract,
    test_no_stale_references,
    test_no_hardcoded_models_in_workflows,
    test_breaking_workflow_reads_config,
    test_breaking_ladder_matches_config,
    test_classifier_ladder_matches_config,
    test_run_flags_present,
    test_no_model_id_is_hardcoded_in_the_ladder_consumers,
    test_the_measured_working_models_lead_and_the_known_failure_is_last,
]

if __name__ == "__main__":
    print("Running ladder-sync tests...\n")
    passed = 0
    failed = 0
    for fn in ALL_TESTS:
        try:
            fn()
            passed += 1
        except Exception as exc:
            print("  FAIL: %s: %s" % (fn.__name__, exc))
            failed += 1

    print("\n%d passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
