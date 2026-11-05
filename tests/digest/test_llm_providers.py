#!/usr/bin/env python3
"""tests/digest/test_llm_providers.py -- the three keyed HTTP fallback rungs.

WHY THIS EXISTS. Measured 2026-11-05 (run 41944898717): the digest walk spent
Gemini's free tier in 13.4 seconds, both keyed rungs refused, the shared OpenCode
pool answered `payment_required`, and the edition shipped with publisher text.
ONE account's allowance was the whole pipeline's single point of failure.

`scripts/llm_providers.py` is the adapter for the three providers the owner added
as real fallbacks (Groq, OpenRouter, Mistral). This file pins the contract every
caller depends on, and nothing about the prompts:

    THE REQUEST IS RIGHT      -- url, one Authorization header, one JSON body.
    NO KEY, NO CALL           -- missing credential is `not_configured`, never a
                                 request and never a failure.
    ONE REASON VOCABULARY     -- 401 `auth`, 429 `quota_notice`, a socket timeout
                                 `timeout`, everything else `transport`/`garbage`
                                 -- the words model_selector already speaks.
    THE PAID GUARD            -- an OpenRouter id without `:free` is refused
                                 before a byte is sent.
    THE LADDER ORDER          -- Gemini, then Groq, OpenRouter, Mistral, then the
                                 free pool, in BOTH consumers.
    NO KEY IN ANY OUTPUT      -- not in a result, a debug line, a workflow or
                                 this file.
"""

import json
import os
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import llm_providers as lp  # noqa: E402
import model_selector  # noqa: E402
import summarise_stories as ss  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

KEYS = {
    "GROQ_API_KEY": "test-groq-key",
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "MISTRAL_API_KEY": "test-mistral-key",
    "GEMINI_API_KEY": "test-gemini-key",
}

OK_BODY = json.dumps({"choices": [{"message": {"content": "OK"}}]})

CONFIG_LADDER = (
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.1-flash-lite",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/beam-free",
)


def transport_returning(status, body):
    """(fake transport, recorded specs). Answers every request the same way."""
    calls = []

    def send(spec):
        calls.append(spec)
        return status, body

    return send, calls


def keys(environ=KEYS):
    """A full provider key set, without touching the real environment."""
    return mock.patch.dict(os.environ, dict(environ))


class RequestShapeTests(unittest.TestCase):
    """The request is a value, so it can be asserted without a socket."""

    def test_each_provider_posts_to_its_own_url_with_a_bearer_key(self):
        for name, url in (
            ("groq", "https://api.groq.com/openai/v1/chat/completions"),
            ("openrouter", "https://openrouter.ai/api/v1/chat/completions"),
            ("mistral", "https://api.mistral.ai/v1/chat/completions"),
            ("gemini",
             "https://generativelanguage.googleapis.com/v1beta/openai/"
             "chat/completions"),
        ):
            with self.subTest(provider=name):
                spec = lp.request_spec(name, None, "hello", "k-123", timeout=30)
                self.assertEqual(url, spec["url"])
                self.assertEqual("POST", spec["method"])
                self.assertEqual("Bearer k-123", spec["headers"]["Authorization"])
                self.assertEqual("application/json",
                                 spec["headers"]["Content-Type"])
                body = json.loads(spec["body"])
                self.assertEqual("hello", body["messages"][0]["content"])
                self.assertFalse(body["stream"])
                self.assertEqual(lp.model_for(name), body["model"])

    def test_no_credential_appears_in_the_url_or_any_other_header(self):
        spec = lp.request_spec("groq", None, "hello", "secret-value", timeout=30)
        self.assertNotIn("secret-value", spec["url"])
        self.assertEqual(["Accept", "Authorization", "Content-Type"],
                         sorted(spec["headers"]))
        self.assertTrue(spec["headers"]["Authorization"].endswith("secret-value"))

    def test_the_provider_ceiling_is_enforced_on_the_call_not_only_the_deadline(self):
        # The per-call slice comes from call_timeout(); the adapter may only
        # SHORTEN it. These two numbers are the whole enforcement.
        self.assertEqual(60, lp.effective_timeout("groq", 300))
        self.assertEqual(12, lp.effective_timeout("groq", 12))
        self.assertEqual(75, lp.effective_timeout("openrouter", None))
        spec = lp.request_spec("groq", None, "p", "k", timeout=300)
        self.assertEqual(60, spec["timeout"])
        spec = lp.request_spec("groq", None, "p", "k", timeout=7)
        self.assertEqual(7, spec["timeout"])


class MissingKeyTests(unittest.TestCase):
    def test_no_key_means_not_configured_and_no_request(self):
        seen, calls = transport_returning(200, OK_BODY)
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
                os.environ.pop(name, None)
            got = lp.call("groq", "prompt", transport=seen)
        self.assertFalse(got.ok)
        self.assertEqual("not_configured", got.reason)
        self.assertEqual([], calls, "a provider without a key must not be called")

    def test_an_unconfigured_provider_is_absent_from_both_ladders(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
                os.environ.pop(name, None)
            self.assertEqual((), lp.ladder_rungs())
            self.assertEqual(
                tuple(CONFIG_LADDER),
                lp.ordered_ladder(list(CONFIG_LADDER)),
                "with no provider keys the ladder must be exactly the seeds, so "
                "a repository without the secrets behaves as it did before")


class StatusMappingTests(unittest.TestCase):
    """One status, one reason word -- the vocabulary the rest of the repo uses."""

    def test_a_refused_key_is_auth(self):
        send, _ = transport_returning(401, '{"error":{"message":"bad key"}}')
        with keys():
            got = lp.call("groq", "p", transport=send)
        self.assertEqual("auth", got.reason)
        self.assertEqual(401, got.status)

    def test_a_spent_allowance_is_quota_notice(self):
        send, _ = transport_returning(429, '{"error":{"message":"rate limited"}}')
        with keys():
            got = lp.call("openrouter", "p", transport=send)
        self.assertEqual("quota_notice", got.reason)

    def test_a_socket_timeout_is_timeout(self):
        def send(_spec):
            raise socket.timeout("read timed out")

        with keys():
            got = lp.call("mistral", "p", transport=send)
        self.assertEqual("timeout", got.reason)

    def test_a_dead_endpoint_is_transport(self):
        def send(_spec):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        with keys():
            got = lp.call("mistral", "p", transport=send)
        self.assertEqual("transport", got.reason)

    def test_a_200_with_no_choices_is_garbage(self):
        send, _ = transport_returning(200, '{"unexpected": true}')
        with keys():
            got = lp.call("groq", "p", transport=send)
        self.assertEqual("garbage", got.reason)
        self.assertFalse(got.ok)

    def test_a_missing_model_is_model_not_found(self):
        send, _ = transport_returning(404, '{"error":{"message":"no such model"}}')
        with keys():
            got = lp.call("groq", "p", transport=send)
        self.assertEqual("model_not_found", got.reason)


class PaidOpenRouterGuardTests(unittest.TestCase):
    def test_a_paid_id_is_refused_before_any_request(self):
        seen, calls = transport_returning(200, OK_BODY)
        with keys():
            with self.assertRaises(ValueError) as caught:
                lp.call("openrouter", "p", model_id="openai/gpt-4o",
                        transport=seen)
        self.assertIn(":free", str(caught.exception))
        self.assertEqual([], calls, "the guard must fire before any network I/O")

    def test_a_free_id_passes(self):
        seen, calls = transport_returning(200, OK_BODY)
        with keys():
            got = lp.call("openrouter", "p",
                          model_id="deepseek/deepseek-r1:free", transport=seen)
        self.assertTrue(got.ok)
        self.assertEqual(1, len(calls))
        self.assertEqual("deepseek/deepseek-r1:free",
                         json.loads(calls[0]["body"])["model"])

    def test_discovery_can_only_offer_free_ids(self):
        listing = json.dumps({"data": [
            {"id": "openai/gpt-4o"},
            {"id": "deepseek/deepseek-r1:free"},
        ]})
        seen, _ = transport_returning(200, listing)
        with keys():
            got = lp.list_models("openrouter", transport=seen)
        self.assertEqual(["deepseek/deepseek-r1:free"], got.ids)


class LadderOrderTests(unittest.TestCase):
    """Gemini, then Groq, then OpenRouter, then Mistral, then the free pool."""

    def test_ordered_ladder_places_every_block_in_the_stated_order(self):
        got = lp.ordered_ladder(list(CONFIG_LADDER), environ=KEYS)
        self.assertEqual(
            ("gemini/gemini-3.5-flash-lite",
             "gemini/gemini-3.1-flash-lite",
             "groq/" + lp.PROVIDERS["groq"].models[0],
             "openrouter/" + lp.PROVIDERS["openrouter"].models[0],
             "mistral/" + lp.PROVIDERS["mistral"].models[0],
             "opencode/deepseek-v4.1-flash-free",
             "opencode/beam-free"),
            tuple(got))

    def test_the_summariser_walks_the_same_order(self):
        with keys():
            got = ss.ladder_models()
        providers = [lp.provider_of(m) for m in got]
        self.assertEqual(
            [None, None, "groq", "openrouter", "mistral"],
            providers[:5],
            "the digest ladder must be gemini, the three keyed providers in "
            "order, then the free pool -- got %r" % (got,))
        self.assertTrue(providers[5:], "the free pool must still be walked")
        self.assertEqual({None}, set(providers[5:]),
                         "nothing may follow the free pool -- got %r" % (got,))
        self.assertTrue(all(m.startswith("opencode/") for m in got[5:]),
                        "everything after Mistral must be the free pool -- "
                        "got %r" % (got,))

    def test_the_breaking_walk_walks_the_same_order(self):
        import breaking_monitor as bm
        with keys():
            got = bm._ladder_for(list(CONFIG_LADDER), ("opencode/", "gemini/"))
        self.assertEqual(
            ["gemini", "gemini", "groq", "openrouter", "mistral",
             None, None],
            [lp.provider_of(m) or ("gemini" if m.startswith("gemini/") else None)
             for m in got])

    def test_gemini_keeps_its_existing_cli_path(self):
        self.assertIsNone(lp.provider_of("gemini/gemini-3.5-flash-lite"))
        self.assertEqual("groq", lp.provider_of("groq/llama-3.3-70b-versatile"))


class FailureVocabularyTests(unittest.TestCase):
    """The adapter's words must survive into the two existing consumers."""

    def test_the_breaking_walk_classifies_a_401_and_a_429(self):
        send, _ = transport_returning(401, "unauthorized")
        with keys():
            rc, out, err = lp.cli_result("groq/x", "p", transport=send)
        self.assertEqual(1, rc)
        self.assertIn("HTTP 401", err)
        self.assertEqual("auth", model_selector.classify_failure(err, rc))

        send, _ = transport_returning(429, "slow down")
        with keys():
            rc, out, err = lp.cli_result("groq/x", "p", transport=send)
        self.assertEqual("quota_or_rate_limited",
                         model_selector.classify_failure(err, rc))

    def test_a_timeout_raises_the_exception_the_walk_already_handles(self):
        import subprocess

        def send(_spec):
            raise socket.timeout("nope")

        with keys():
            with self.assertRaises(subprocess.TimeoutExpired):
                lp.cli_result("groq/x", "p", timeout=5, transport=send)

    def test_the_summariser_turns_a_refusal_into_its_own_kind(self):
        with keys():
            with mock.patch.object(lp, "text_for",
                                   side_effect=lp.ProviderError(
                                       "groq/x: auth (HTTP 401)", kind="auth")):
                with self.assertRaises(ss.Refusal) as caught:
                    ss.run_model("prompt", "groq/x", timeout=9)
        self.assertEqual("auth", caught.exception.kind)
        self.assertEqual("provider_refusal",
                         ss.refusal_cause("auth", str(caught.exception)))

    def test_a_provider_quota_reads_as_a_provider_refusal_on_the_page(self):
        self.assertEqual("provider_refusal",
                         ss.refusal_cause("quota_notice", "429 too many"))
        self.assertEqual("unreadable_reply", ss.refusal_cause("garbage", "{}"))
        self.assertEqual("model_timeout", ss.refusal_cause("timeout", ""))


class RedactionTests(unittest.TestCase):
    def test_a_key_echoed_in_an_error_body_never_survives(self):
        body = '{"error":{"message":"invalid header Authorization: Bearer sk-live-abcdefghijklmnop"}}'
        send, _ = transport_returning(401, body)
        with keys():
            got = lp.call("groq", "p", transport=send,
                          environ={"GROQ_API_KEY": "sk-live-abcdefghijklmnop"})
        self.assertNotIn("abcdefghijklmnop", got.detail)
        self.assertIn("redacted", got.detail.lower())


class ProbeTests(unittest.TestCase):
    def _router(self):
        calls = []

        def send(spec):
            calls.append(spec)
            if spec["url"].endswith("/models"):
                return 200, json.dumps({"data": [{"id": "llama-3.3-70b-versatile"}]})
            return 200, OK_BODY

        return send, calls

    def test_the_row_format_is_the_published_contract(self):
        send, _ = self._router()
        rows = lp.probe_rows(transport=send, environ=KEYS)
        groq_row = [r for r in rows if r.startswith("groq | ")][0]
        self.assertEqual(
            "groq | key=present | models=1 | chosen=llama-3.3-70b-versatile | "
            "status=200 | ms=",
            groq_row[:len(
                "groq | key=present | models=1 | chosen=llama-3.3-70b-versatile "
                "| status=200 | ms=")])
        self.assertRegex(groq_row, r"ms=\d+ \| verdict=ok$")
        self.assertEqual(4, len([r for r in rows if "| key=" in r]))

    def test_an_absent_key_reports_not_configured_and_makes_no_request(self):
        send, calls = self._router()
        got = lp.probe("mistral", transport=send, environ={})
        self.assertEqual("not_configured", got["verdict"])
        self.assertEqual("absent", got["key"])
        self.assertEqual(0, got["models"])
        self.assertEqual([], calls)

    def test_the_catalogue_is_reported_as_the_evidence(self):
        send, _ = self._router()
        got = lp.probe("groq", transport=send, environ=KEYS)
        self.assertEqual(["llama-3.3-70b-versatile"], got["available"])


class NoKeyShapedStringsTests(unittest.TestCase):
    """A credential must not be able to arrive as a literal in a later edit."""

    SHAPE = r"(?:gsk_|sk-or-v1-|sk-live-|AIza)[A-Za-z0-9_\-]{12,}"

    def _sources(self):
        return [
            REPO_ROOT / "scripts" / "llm_providers.py",
            REPO_ROOT / "scripts" / "summarise_stories.py",
            REPO_ROOT / "scripts" / "breaking_monitor.py",
            REPO_ROOT / ".github" / "workflows" / "provider-probe.yml",
            REPO_ROOT / ".github" / "workflows" / "daily-news.yml",
            REPO_ROOT / ".github" / "workflows" / "breaking-monitor.yml",
            Path(__file__),
        ]

    def test_no_source_carries_a_key_shaped_literal(self):
        import re
        pattern = re.compile(self.SHAPE)
        offenders = []
        for path in self._sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in pattern.finditer(text):
                # The redaction test builds a synthetic example; it is the one
                # deliberate occurrence and is asserted by RedactionTests.
                if "sk-live-abcdefghijklmnop" in match.group(0) \
                        and path.name == "test_llm_providers.py":
                    continue
                offenders.append("%s: %s" % (path.name, match.group(0)[:8] + "..."))
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
