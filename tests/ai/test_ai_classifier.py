#!/usr/bin/env python3
"""tests/ai/test_ai_classifier.py — offline tests for scripts/ai_classifier.py.

Plain asserts, no test framework, stdlib only.  Run:

    py tests\\ai\\test_ai_classifier.py

Every network touch is replaced by injecting mock _run_provider functions.
"""

import asyncio
import json
import os
import sys
import unittest.mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import ai_classifier as ac  # noqa: E402

#: The pool one race actually uses. DERIVED, never written down: the whole
#: point of dynamic selection is that a race no longer depends on a hardcoded
#: provider name at a hardcoded index, so every expectation below is derived
#: from the same resolved pool classify_batch() uses. A test that pinned
#: "big-pickle" here would pin the brittleness the change removes.
POOL = ac.resolve_providers()
P0, P1 = POOL[0]["name"], POOL[1]["name"]
P0_MODEL, P1_MODEL = POOL[0]["model"], POOL[1]["model"]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_ITEMS = [
    {
        "title": "Major world news today",
        "snippet": "Something big happened",
        "source": "reuters.com",
        "domain_category": "world",
    },
]

VALID_CLASSIFICATION = [
    {
        "title": "Major world news today",
        "category": "politics",
        "confidence": "HIGH",
        "summary": "A major event occurred.",
        "relevance_score": 0.9,
    },
]

VALID_CLASSIFICATION_MIMO = [
    {
        "title": "Major world news today",
        "category": "politics",
        "confidence": "MEDIUM",
        "summary": "A notable event occurred.",
        "relevance_score": 0.7,
    },
]

_ENV = {"OPENCODE_API_KEY": "test-key-123"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_provider_wins():
    """When the first provider returns valid JSON, the rest are ignored."""
    call_log = []

    async def fake_run_provider(provider, prompt, api_key):
        call_log.append(provider["name"])
        if provider["name"] == P0:
            return P0, VALID_CLASSIFICATION
        return P1, "second: should not win"

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            result = ac.classify_batch(VALID_ITEMS)

    assert isinstance(result, list), "expected list"
    assert len(result) == 1
    assert result[0]["title"] == "Major world news today"
    assert result[0]["category"] == "politics"
    assert result[0]["confidence"] == "HIGH"
    assert P0 in call_log, (
        "the winning provider was never called: %r" % (call_log,))
    print("  PASS: test_first_provider_wins")


def test_first_fails_second_wins():
    """When the first provider fails, the second's valid result is used."""
    async def fake_run_provider(provider, prompt, api_key):
        if provider["name"] == P0:
            return P0, "%s attempt 1: HTTP 502" % P0
        return P1, VALID_CLASSIFICATION_MIMO

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            result = ac.classify_batch(VALID_ITEMS)

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["confidence"] == "MEDIUM"
    print("  PASS: test_first_fails_second_wins")


def test_all_providers_fail():
    """When every provider fails, classify_batch raises RuntimeError."""
    async def fake_run_provider(provider, prompt, api_key):
        return provider["name"], "%s: HTTP 500" % provider["name"]

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            try:
                ac.classify_batch(VALID_ITEMS)
                assert False, "should have raised RuntimeError"
            except RuntimeError as exc:
                assert "All AI providers failed" in str(exc)
                for name in (P0, P1):
                    assert name in str(exc), (
                        "%s missing from the all-failed message: %s"
                        % (name, exc))
    print("  PASS: test_all_providers_fail")


def test_malformed_first_second_wins():
    """When the first returns an error string, the second's valid JSON wins."""
    async def fake_run_provider(provider, prompt, api_key):
        if provider["name"] == P0:
            return P0, "%s attempt 1: invalid response format" % P0
        return P1, VALID_CLASSIFICATION

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            result = ac.classify_batch(VALID_ITEMS)

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["title"] == "Major world news today"
    print("  PASS: test_malformed_first_second_wins")


def test_empty_items():
    """Empty input returns empty list without calling any provider."""
    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        result = ac.classify_batch([])
    assert result == []
    print("  PASS: test_empty_items")


def test_missing_api_key():
    """Missing OPENCODE_API_KEY raises RuntimeError."""
    env = {k: v for k, v in os.environ.items() if k != "OPENCODE_API_KEY"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        try:
            ac.classify_batch(VALID_ITEMS)
            assert False, "should have raised RuntimeError"
        except RuntimeError as exc:
            assert "OPENCODE_API_KEY" in str(exc)
    print("  PASS: test_missing_api_key")


def test_is_valid_response():
    """_is_valid_response accepts/rejects edge cases correctly."""
    assert ac._is_valid_response(VALID_CLASSIFICATION) is True
    assert ac._is_valid_response([]) is False
    assert ac._is_valid_response(None) is False
    assert ac._is_valid_response("not a list") is False
    # Missing key.
    assert ac._is_valid_response([{"title": "x", "category": "y"}]) is False
    # Extra keys are fine.
    extra = [{
        "title": "x", "category": "y", "confidence": "HIGH",
        "summary": "z", "relevance_score": 0.5, "bonus": True,
    }]
    assert ac._is_valid_response(extra) is True
    print("  PASS: test_is_valid_response")


def test_parse_json_array():
    """_parse_json_array extracts arrays from direct and embedded JSON."""
    arr = VALID_CLASSIFICATION
    # Direct.
    assert ac._parse_json_array(json.dumps(arr)) == arr
    # Embedded in surrounding text.
    wrapped = "Here is the result:\n" + json.dumps(arr) + "\nDone."
    assert ac._parse_json_array(wrapped) == arr
    # Garbage.
    assert ac._parse_json_array("no json here") is None
    # Object, not array.
    assert ac._parse_json_array('{"key": "value"}') is None
    # None / empty.
    assert ac._parse_json_array(None) is None
    assert ac._parse_json_array("") is None
    print("  PASS: test_parse_json_array")


def test_classification_keys_standardized():
    """Result items always have the five required standardized keys."""
    async def fake_run_provider(provider, prompt, api_key):
        return provider["name"], [{
            "title": "Story",
            "category": "tech",
            "confidence": "LOW",
            "summary": "A tech story.",
            "relevance_score": 0.3,
        }]

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            result = ac.classify_batch(VALID_ITEMS)

    required = {"title", "category", "confidence", "summary", "relevance_score"}
    for item in result:
        assert required.issubset(item.keys()), \
            "missing keys: %s" % (required - item.keys())
    print("  PASS: test_classification_keys_standardized")


def test_runtime_error_combined_info():
    """RuntimeError message includes error info from ALL providers."""
    async def fake_run_provider(provider, prompt, api_key):
        return provider["name"], "%s: timeout after %ds" % (
            provider["name"], provider["timeout"])

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            try:
                ac.classify_batch(VALID_ITEMS)
                assert False, "should have raised RuntimeError"
            except RuntimeError as exc:
                msg = str(exc)
                for name in (P0, P1):
                    assert name in msg, (
                        "%s missing from the combined-failure message: %s"
                        % (name, msg))
    print("  PASS: test_runtime_error_combined_info")


def test_build_prompt():
    """_build_prompt embeds items as JSON and returns a string."""
    prompt = ac._build_prompt(VALID_ITEMS)
    assert isinstance(prompt, str)
    assert "Major world news today" in prompt
    assert "reuters.com" in prompt
    print("  PASS: test_build_prompt")


def test_multiple_items():
    """classify_batch handles multiple items in one batch."""
    items = [
        {"title": "Story A", "snippet": "a", "source": "a.com", "domain_category": "tech"},
        {"title": "Story B", "snippet": "b", "source": "b.com", "domain_category": "sports"},
    ]
    multi_result = [
        {"title": "Story A", "category": "tech", "confidence": "HIGH",
         "summary": "Tech story.", "relevance_score": 0.8},
        {"title": "Story B", "category": "sports", "confidence": "MEDIUM",
         "summary": "Sports story.", "relevance_score": 0.6},
    ]

    async def fake_run_provider(provider, prompt, api_key):
        return provider["name"], multi_result

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            result = ac.classify_batch(items)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["category"] == "tech"
    assert result[1]["category"] == "sports"
    print("  PASS: test_multiple_items")


def test_first_failure_does_not_abort_viable_sibling():
    """A provider that FAILS first must not end the race.

    THIS is the shape every other test in this file is not.  Every other fake
    ``_run_provider`` here returns without ever awaiting, so all three tasks
    land in ``done`` on the same event-loop tick and ``asyncio.wait(...,
    FIRST_COMPLETED)`` sees them all at once.  Those tests therefore pass
    against the buggy race as well, because the race never actually races.

    Here the FIRST provider fails (one ``await``, so it genuinely completes
    before its siblings) while the SECOND is still in flight and about to
    succeed.  The winner must be the second's, not a false "all providers
    failed".
    """
    order = []
    finished = set()

    async def fake_run_provider(provider, prompt, api_key):
        name = provider["name"]
        if name == P0:
            await asyncio.sleep(0)          # yields, so it completes FIRST
            order.append(name)
            finished.add(name)
            return name, "%s attempt 3: HTTP 429" % P0
        if name == P1:
            await asyncio.sleep(0.05)       # still in flight when P0 dies
            order.append(name)
            finished.add(name)
            return name, VALID_CLASSIFICATION_MIMO
        await asyncio.sleep(30)             # never wins; must be cancelled
        finished.add(name)
        return name, [{"title": "too late", "category": "x", "confidence": "LOW",
                       "summary": "too late", "relevance_score": 0.0}]

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            # HAPPY PATH FIRST, and with a real message: against the buggy
            # race this raises RuntimeError, and that is the RED.
            result = ac.classify_batch(VALID_ITEMS)

    # The happy path genuinely happened -- a real list, not an error path.
    assert isinstance(result, list), \
        "expected a classification list, got %r" % (type(result).__name__,)
    assert len(result) == 1, "expected exactly one classification"

    # The race really raced: the failure completed before the winner did.
    assert order[:2] == [P0, P1], \
        "test did not exercise the race; completion order was %r" % (order,)

    # Safety property: the healthy sibling was consulted and won.
    assert result[0]["confidence"] == "MEDIUM", (
        "the healthy sibling's valid classification must win, got %r"
        % (result[0],))

    # Safety property: the loser that could never win was not left to finish.
    # Names come from the resolved POOL, not hardcoded, so this cannot go vacuous.
    loser = POOL[2]["name"]
    assert loser not in finished, \
        "a losing provider must be cancelled on the win, not run to completion"
    assert loser == POOL[2]["model"].rsplit("/", 1)[-1], (
        "a provider label must be DERIVED from its model id, not hardcoded "
        "beside it: %r" % (loser,))
    assert POOL[0]["model"] == P0_MODEL and POOL[1]["model"] == P1_MODEL, (
        "the pool this test indexes must be the pool classify_batch races "
        "against")
    print("  PASS: test_first_failure_does_not_abort_viable_sibling")


def test_staggered_all_fail_reports_every_provider():
    """Providers failing one after another still raise, naming all of them.

    Covers the drain path: the race now keeps waiting while any provider is
    still in flight, so the all-failed case needs providers that finish at
    DIFFERENT times to be exercised at all.
    """
    async def fake_run_provider(provider, prompt, api_key):
        await asyncio.sleep(0.01 * (provider["timeout"] % 7))
        return provider["name"], "%s: timeout after %ds" % (
            provider["name"], provider["timeout"])

    with unittest.mock.patch.dict(os.environ, _ENV, clear=False):
        with unittest.mock.patch.object(ac, "_run_provider", fake_run_provider):
            try:
                ac.classify_batch(VALID_ITEMS)
                assert False, "should have raised RuntimeError"
            except RuntimeError as exc:
                msg = str(exc)
                assert "All AI providers failed" in msg
                for name in [p["name"] for p in POOL]:
                    assert name in msg, \
                        "%s missing from the all-failed message: %s" % (name, msg)
    print("  PASS: test_staggered_all_fail_reports_every_provider")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_first_provider_wins,
    test_first_fails_second_wins,
    test_all_providers_fail,
    test_malformed_first_second_wins,
    test_empty_items,
    test_missing_api_key,
    test_is_valid_response,
    test_parse_json_array,
    test_classification_keys_standardized,
    test_runtime_error_combined_info,
    test_build_prompt,
    test_multiple_items,
    test_first_failure_does_not_abort_viable_sibling,
    test_staggered_all_fail_reports_every_provider,
]

if __name__ == "__main__":
    print("Running ai_classifier tests...\n")
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
    if failed:
        sys.exit(1)
