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
EXPECTED_LADDER = [
    "opencode/big-pickle",
    "opencode/mimo-v2.6-flash-free",
    "opencode/nemotron-3.5-lightning-free",
]
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
    """breaking_monitor.AI_MODELS == config ladder (single source)."""
    import breaking_monitor as bm  # noqa: E402

    cfg = _load_config()
    assert tuple(bm.AI_MODELS) == tuple(cfg["ladder"]), \
        "AI_MODELS %r != config ladder %r" % (bm.AI_MODELS, cfg["ladder"])
    assert tuple(bm.AI_MODELS) == tuple(EXPECTED_LADDER)
    print("  PASS: test_breaking_ladder_matches_config")


def test_classifier_ladder_matches_config():
    """ai_classifier PROVIDERS resolve to exactly the config ladder."""
    import ai_classifier as ac  # noqa: E402

    cfg = _load_config()
    models = [p["model"] for p in ac.PROVIDERS]
    assert models == list(cfg["ladder"]), \
        "PROVIDERS %r != config ladder %r" % (models, cfg["ladder"])
    assert models == EXPECTED_LADDER
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
