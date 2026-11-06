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

#: The second Google project's key. Separate from KEYS on purpose: the existing
#: order tests pin what a repository with ONE Gemini key does today, and the
#: second-key tests opt in explicitly.
KEYS_BOTH = dict(KEYS, GEMINI_API_KEY_2="test-gemini-key2")

OK_BODY = json.dumps({"choices": [{"message": {"content": "OK"}}]})

CONFIG_LADDER = (
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.1-flash-lite",
    "gemini-key2/gemini-3.5-flash-lite",
    "gemini-key2/gemini-3.1-flash-lite",
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


def router(catalogues=None, chat=(200, OK_BODY)):
    """(fake transport, recorded specs): /models answered per provider.

    `catalogues` maps a provider name to the ids its catalogue lists; a provider
    missing from it answers with an EMPTY catalogue, which is the interesting
    case as often as a populated one.
    """
    calls = []
    catalogues = catalogues or {}

    def send(spec):
        calls.append(spec)
        if spec["url"].endswith("/models"):
            for name, ids in catalogues.items():
                if lp.PROVIDERS[name].models_url == spec["url"]:
                    return 200, json.dumps(
                        {"data": [{"id": i} for i in ids]})
            return 200, json.dumps({"data": []})
        return chat

    return send, calls


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
            # A second project, so the same endpoint and a different key.
            ("gemini-key2",
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
            for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
                         "GEMINI_API_KEY_2"):
                os.environ.pop(name, None)
            self.assertEqual((), lp.ladder_rungs())
            # THE SEEDS THAT NEED NO KEY, EXACTLY. A `gemini-key2/*` seed names
            # a second Google project and disappears without its own key, which
            # is why the comparison is the seed minus those two entries.
            self.assertEqual(
                tuple(m for m in CONFIG_LADDER
                      if not m.startswith(lp.GEMINI_KEY2_PROVIDER + "/")),
                lp.ordered_ladder(list(CONFIG_LADDER)),
                "with no provider keys the ladder must be the seeds that need "
                "none, so a repository without the secrets behaves as before")


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
        # A fake catalogue: `ladder_models` resolves the HTTP ids against what
        # the account serves, and a test must never open a socket.
        send, _ = router({"groq": ["llama-3.3-70b-versatile"],
                          "openrouter": ["recursive/flint-v1.0:free"],
                          "mistral": ["mistral-small-latest"]})
        lp.clear_catalogue()
        with keys():
            got = ss.ladder_models(transport=send)
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
        self.assertIsNone(lp.provider_of("gemini-key2/gemini-3.5-flash-lite"))
        self.assertEqual("groq", lp.provider_of("groq/llama-3.3-70b-versatile"))


class SecondGeminiKeyTests(unittest.TestCase):
    """A second Google project is an INDEPENDENT rung, never a replacement.

    `gemini-key2` is a CLI provider exactly like `gemini`: both rungs come from
    the seed, both are keyed, and an absent key contributes nothing.
    """

    def test_the_second_key_is_its_own_rung_right_after_the_first(self):
        got = lp.ordered_ladder(list(CONFIG_LADDER), environ=KEYS_BOTH)
        self.assertEqual(
            ("gemini/gemini-3.5-flash-lite",
             "gemini/gemini-3.1-flash-lite",
             "gemini-key2/gemini-3.5-flash-lite",
             "gemini-key2/gemini-3.1-flash-lite",
             "groq/" + lp.PROVIDERS["groq"].models[0],
             "openrouter/" + lp.PROVIDERS["openrouter"].models[0],
             "mistral/" + lp.PROVIDERS["mistral"].models[0],
             "opencode/deepseek-v4.1-flash-free",
             "opencode/beam-free"),
            tuple(got))

    def test_the_second_key_is_a_cli_rung_not_a_fourth_http_provider(self):
        self.assertEqual(2, len([m for m in
                                 lp.ordered_ladder(list(CONFIG_LADDER),
                                                   environ=KEYS_BOTH)
                                 if m.startswith("gemini-key2/")]))
        self.assertEqual(2, len([m for m in
                                 lp.ordered_ladder(list(CONFIG_LADDER),
                                                   environ=KEYS_BOTH)
                                 if m.startswith("gemini/")]))

    def test_an_absent_second_key_contributes_no_rung_and_says_why(self):
        log = []
        got = lp.ordered_ladder(list(CONFIG_LADDER), environ=KEYS,
                                log=log.append)
        self.assertEqual([], [m for m in got if m.startswith("gemini-key2/")])
        self.assertEqual(2, len([m for m in got if m.startswith("gemini/")]),
                         "the first key's rungs must be untouched")
        self.assertTrue(any("GEMINI_API_KEY_2" in line for line in log),
                        "an absent second key is a setting, not a failure, but "
                        "the run log has to name it: %r" % (log,))

    def test_an_empty_second_key_is_the_same_as_an_absent_one(self):
        got = lp.ordered_ladder(list(CONFIG_LADDER),
                                environ=dict(KEYS, GEMINI_API_KEY_2="   "))
        self.assertEqual([], [m for m in got if m.startswith("gemini-key2/")])

    def test_the_probe_prints_a_row_for_each_gemini_key(self):
        def send(spec):
            if spec["url"].endswith("/models"):
                return 200, json.dumps(
                    {"data": [{"id": "gemini-3.1-flash-lite"}]})
            return 200, OK_BODY

        rows = lp.probe_rows(transport=send, environ=KEYS_BOTH)
        keyed = [r for r in rows if "| key=" in r]
        self.assertEqual(5, len(keyed),
                         "one row per provider: two Gemini keys and the three "
                         "HTTP providers -- got %r" % (rows,))
        self.assertTrue([r for r in rows if r.startswith("gemini-key2 | ")],
                        rows)


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
        # FIVE: gemini, the second Google project, and the three HTTP providers.
        self.assertEqual(5, len([r for r in rows if "| key=" in r]))

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


class ServedModelTests(unittest.TestCase):
    """The id sent is the one the ACCOUNT serves, not the one the seed guessed.

    Measured 2026-11-05 (probe run 42032952302): OpenRouter served 8 of 12 listed
    ids and NEITHER configured candidate. A seed id is a claim to check.
    """

    def setUp(self):
        lp.clear_catalogue()

    def tearDown(self):
        lp.clear_catalogue()

    def test_a_served_id_replaces_a_seed_the_account_does_not_serve(self):
        send, _ = router({"openrouter": ["recursive/flint-v1.0:free"]})
        got = lp.served_model("openrouter", transport=send, environ=KEYS)
        self.assertEqual("recursive/flint-v1.0:free", got["model"])
        self.assertEqual("catalogue", got["source"])
        self.assertTrue(got["substituted"],
                        "the log must be able to say the seed was replaced")
        self.assertEqual(lp.PROVIDERS["openrouter"].models[0],
                         got["configured"])

    def test_a_safety_classifier_is_never_preferred_over_an_instruct_model(self):
        # Neither id is a seed, so the NAME decides -- which is the rule that
        # applies the moment the account's catalogue changes.
        send, _ = router({"openrouter": ["acme/guard-classifier:free",
                                         "acme/new-instruct:free"]})
        got = lp.served_model("openrouter", transport=send, environ=KEYS)
        self.assertEqual("acme/new-instruct:free", got["model"])

    def test_a_catalogue_with_nothing_usable_contributes_no_rung(self):
        send, _ = router({"groq": []})
        got = lp.served_model("groq", transport=send, environ=KEYS)
        self.assertIsNone(got["model"])
        self.assertEqual("no_served_model", got["reason"])
        rungs = lp.ladder_rungs(environ={"GROQ_API_KEY": "k"},
                                transport=send, resolve=True)
        self.assertEqual((), rungs,
                         "a provider serving nothing must not be attempted")

    def test_an_unreadable_catalogue_keeps_the_configured_id(self):
        send, _ = transport_returning(503, "down")
        got = lp.served_model("groq", transport=send, environ=KEYS)
        self.assertEqual("llama-3.3-70b-versatile", got["model"])
        self.assertEqual("catalogue_unreadable", got["reason"],
                         "a failed GET is not evidence of an unserved id")
        self.assertEqual("transport", got["catalogue_reason"])

    def test_no_key_means_no_catalogue_request(self):
        send, calls = router({"groq": ["llama-3.3-70b-versatile"]})
        got = lp.served_model("groq", transport=send, environ={})
        self.assertEqual("not_configured", got["reason"])
        self.assertIsNone(got["model"])
        self.assertEqual([], calls)

    def test_one_catalogue_read_per_process(self):
        send, calls = router({"groq": ["llama-3.3-70b-versatile"]})
        lp.served_model("groq", transport=send, environ=KEYS)
        lp.served_model("groq", transport=send, environ=KEYS)
        self.assertEqual(1, len(calls),
                         "the catalogue is read once, not once per rung")

    def test_the_substitution_is_named_in_the_log(self):
        send, _ = router({"openrouter": ["recursive/flint-v1.0:free"]})
        notes = []
        rungs = lp.ladder_rungs(environ={"OPENROUTER_API_KEY": "k"},
                                transport=send, resolve=True,
                                log=notes.append)
        self.assertEqual(("openrouter/recursive/flint-v1.0:free",), rungs)
        self.assertTrue(any("does not serve" in n for n in notes),
                        "the run must name what answered -- got %r" % (notes,))


class GeminiCatalogueTests(unittest.TestCase):
    """Gemini's catalogue is `models/`-prefixed; its chat endpoint is not."""

    def test_a_prefixed_catalogue_matches_the_bare_candidate(self):
        send, _ = transport_returning(200, json.dumps({"data": [
            {"id": "models/gemini-3.5-flash-lite"},
            {"id": "models/gemini-9-pro"},
        ]}))
        got = lp.list_models("gemini", transport=send, environ=KEYS)
        self.assertEqual(["gemini-3.5-flash-lite", "gemini-9-pro"], got.ids)
        self.assertEqual("gemini-3.5-flash-lite",
                         lp.choose_model("gemini", got.ids))


class ProbeFindingTests(unittest.TestCase):
    """Each 2026-11-05 finding, as the probe must now report it."""

    GROQ_CATALOGUE = json.dumps({"data": [{"id": "llama-3.3-70b-versatile"}]})

    def _send(self, chat_status, chat_body, chat_after=None):
        state = {"n": 0}

        def send(spec):
            if spec["url"].endswith("/models"):
                return 200, self.GROQ_CATALOGUE
            state["n"] += 1
            if state["n"] == 1:
                return chat_status, chat_body
            return chat_after or (chat_status, chat_body)

        return send

    def test_a_404_names_the_id_it_tried(self):
        rows = lp.probe_rows(transport=self._send(
            404, '{"error":{"message":"model not found"}}'), environ=KEYS)
        row = [r for r in rows if r.startswith("groq | ")][0]
        self.assertIn("verdict=model_not_found", row)
        note = [r for r in rows if r.strip().startswith("groq note:")][0]
        self.assertIn("llama-3.3-70b-versatile", note,
                      "the owner must see WHICH id was refused -- got %r" % note)

    def test_a_refused_key_points_at_scope_and_account(self):
        # Probe 42049082917: Groq answered 403 in 26 ms to a key regenerated
        # minutes earlier. "Regenerate it" was advice ALREADY TAKEN, so the note
        # must send the owner to the two things that were not tried.
        rows = lp.probe_rows(transport=self._send(
            403, '{"error":{"message":"forbidden"}}'), environ=KEYS)
        row = [r for r in rows if r.startswith("groq | ")][0]
        self.assertTrue(row.endswith("verdict=auth"),
                        "the row shape stays pinned -- got %r" % row)
        note = [r for r in rows if r.strip().startswith("groq note:")][0]
        self.assertIn("REFUSED the key", note)
        self.assertIn("scope", note)
        self.assertIn("account", note)
        self.assertNotIn("regenerat", note,
                         "the owner already regenerated it -- got %r" % note)

    def test_a_429_is_retried_once_then_recorded(self):
        slept = []
        got = lp.probe("mistral",
                       transport=self._send(429, '{"error":{"message":"slow"}}'),
                       environ=KEYS, sleep=slept.append)
        self.assertEqual("quota", got["verdict"])
        self.assertEqual(2, got["attempts"])
        self.assertEqual([lp.PROBE_RETRY_SECONDS], slept,
                         "exactly one bounded wait, never a loop")

    def test_a_429_then_a_200_is_ok_on_the_second_attempt(self):
        got = lp.probe("mistral",
                       transport=self._send(429, "slow", (200, OK_BODY)),
                       environ=KEYS, sleep=lambda _s: None)
        self.assertEqual("ok", got["verdict"])
        self.assertEqual(2, got["attempts"])

    def test_a_200_with_no_text_says_the_model_said_nothing(self):
        rows = lp.probe_rows(transport=self._send(200, EmptyAnswerTests.EMPTY),
                             environ=KEYS)
        row = [r for r in rows if r.startswith("groq | ")][0]
        self.assertTrue(row.endswith("verdict=empty"),
                        "an empty answer is not `network` -- got %r" % row)
        note = [r for r in rows if r.strip().startswith("groq note:")][0]
        self.assertIn("said nothing", note)
        self.assertIn("llama-3.3-70b-versatile", note,
                      "the note must name the id -- got %r" % note)


class QuotaMovesOnTests(unittest.TestCase):
    """A 429 from a keyed provider is capacity: record it, walk on, do not wait."""

    def test_the_batch_moves_on_without_sleeping(self):
        batch = [{"title": "Story", "summary": "Publisher text.",
                  "source_url": "https://example.com/1",
                  "category": "world_major"}]
        calls = []

        def run(prompt, model, timeout=None):
            calls.append(model)
            if model == "groq/a":
                raise ss.Refusal("groq/a: quota_notice (HTTP 429) too many",
                                 kind="quota_notice")
            return json.dumps([{"url": "https://example.com/1",
                                "summary": "A model-written sentence about "
                                           "what happened today."}])

        waits = []
        out = ss.summarise(batch, models=["groq/a", "mistral/b"], run=run,
                           sleep=waits.append, log=lambda *a: None,
                           budget=10 ** 9)
        self.assertEqual(["groq/a", "mistral/b"], calls)
        self.assertEqual([], waits, "a 429 costs no wait on an HTTP rung")
        self.assertEqual("mistral/b", out["model"])
        self.assertEqual(1, len(out["refusals"]))
        self.assertIn("quota_notice", out["refusals"][0],
                      "the 429 is recorded even though the next rung answered")


class EmptyAnswerTests(unittest.TestCase):
    """HTTP 200 with no `content` is not a network fault, and it is not garbage.

    Probe 42043556280: OpenRouter returned 200 in 11.9s from
    `xingchen-agi/xing4.0-29b-a4b:free` with EMPTY content. The adapter called
    that `garbage`, the probe printed `network`, and the id stayed in the seeds.
    """

    REASONING = json.dumps({"choices": [{"message": {
        "content": "", "reasoning": "OK"}}]})
    EMPTY = json.dumps({"choices": [{"message": {"content": "",
                                                "reasoning": ""}}]})

    def test_reasoning_text_is_used_when_content_is_empty(self):
        send, _ = transport_returning(200, self.REASONING)
        with keys():
            got = lp.call("openrouter", "p", model_id="x/y:free",
                          transport=send)
        self.assertTrue(got.ok)
        self.assertEqual("OK", got.text)
        self.assertEqual("choices.0.message.reasoning", got.field)

    def test_a_message_with_nothing_anywhere_is_empty_answer(self):
        send, _ = transport_returning(200, self.EMPTY)
        with keys():
            got = lp.call("openrouter", "p", model_id="x/y:free",
                          transport=send)
        self.assertFalse(got.ok)
        self.assertEqual("empty_answer", got.reason)
        self.assertEqual("empty", lp.PROBE_VERDICT["empty_answer"],
                         "an empty answer is not a network fault")
        with keys():
            with self.assertRaises(lp.ProviderError) as caught:
                lp.text_for("openrouter/x/y:free", "p", transport=send)
        self.assertEqual("empty_answer", caught.exception.kind)
        self.assertIn("x/y:free", str(caught.exception),
                      "the log must name the model that returned nothing")
        self.assertEqual("unreadable_reply",
                         ss.refusal_cause("empty_answer", ""))

    def test_an_empty_reasoning_field_is_not_mistaken_for_text(self):
        body = json.dumps({"choices": [{"message": {
            "content": "  ", "reasoning": "\n"}}]})
        send, _ = transport_returning(200, body)
        with keys():
            got = lp.call("groq", "p", transport=send)
        self.assertFalse(got.ok)
        self.assertEqual("empty_answer", got.reason)


class FreeSweepTests(unittest.TestCase):
    """Every served free id, asked once -- the seeds are evidence, not a guess."""

    PROSE = json.dumps({"choices": [{"message": {
        "content": "This model answers in a sentence."}}]})

    def setUp(self):
        lp.clear_catalogue()

    def tearDown(self):
        lp.clear_catalogue()

    def _send(self, ids, answers):
        def send(spec):
            if spec["url"].endswith("/models"):
                return 200, json.dumps({"data": [{"id": i} for i in ids]})
            model = json.loads(spec["body"])["model"]
            return answers[model]

        return send

    def test_one_row_per_model_then_the_usable_line(self):
        ids = ["a/one:free", "b/two:free", "c/three:free"]
        answers = {"a/one:free": (200, OK_BODY),
                   "b/two:free": (200, self.PROSE),
                   "c/three:free": (200, EmptyAnswerTests.EMPTY)}
        rows = lp.free_probe_rows(
            transport=self._send(ids, answers),
            environ={"OPENROUTER_API_KEY": "k"})
        model_rows = [r for r in rows if " | status=" in r]
        self.assertEqual(3, len(model_rows), rows)
        self.assertIn("said_ok=yes", model_rows[0])
        self.assertIn("text=yes", model_rows[0])
        self.assertIn("said_ok=no", model_rows[1])
        self.assertIn("verdict=usable", model_rows[1],
                      "prose is a working answer -- the row asks for TEXT")
        self.assertIn("text=no", model_rows[2])
        self.assertIn("verdict=empty", model_rows[2])
        usable = [r for r in rows if r.startswith("usable: ")][0]
        self.assertEqual("usable: a/one:free, b/two:free", usable)

    def test_the_cap_is_stated_when_more_ids_are_served(self):
        ids = ["m%d/x:free" % i for i in range(15)]
        answers = {i: (200, OK_BODY) for i in ids}
        rows = lp.free_probe_rows(
            transport=self._send(ids, answers),
            environ={"OPENROUTER_API_KEY": "k"}, limit=12)
        self.assertEqual(12, len([r for r in rows if " | status=" in r]))
        self.assertIn("12 of 15", rows[0])
        self.assertTrue(any("NOT tested (per-job cap 12)" in r for r in rows),
                        "a cap that is not stated reads as a short catalogue")

    def test_an_unreachable_catalogue_is_reported_not_guessed(self):
        send, _ = transport_returning(503, "down")
        rows = lp.free_probe_rows(transport=send,
                                  environ={"OPENROUTER_API_KEY": "k"})
        self.assertEqual(1, len(rows))
        self.assertIn("no catalogue", rows[0])


class OpenRouterRungTests(unittest.TestCase):
    """Probe 42049082917: seven usable free ids, so ONE id is not a rung.

    The sweep measured these answering with text; the two 429s and the 404 are
    today's weather, not a licence to hardcode anything -- the catalogue check
    stays, and only the ORDER and the count come from the measurement.
    """

    MEASURED = (
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3.5-lightning:free",
        "recursive/flint-v1.0:free",
        "nvidia/nemotron-3.5-super-vl:free",
        "dots-studio/dots-3-note-preview:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "cohere/north-mini-code:free",
    )

    def setUp(self):
        lp.clear_catalogue()

    def tearDown(self):
        lp.clear_catalogue()

    def _rungs(self, ids=None):
        send, _ = router({"openrouter": list(ids or self.MEASURED)})
        return lp.ladder_rungs(environ={"OPENROUTER_API_KEY": "k"},
                               transport=send, resolve=True)

    def test_the_first_rung_is_a_prose_model_from_the_measured_list(self):
        rungs = self._rungs()
        self.assertEqual("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
                         rungs[0],
                         "a specialist or a silent id must not lead")
        self.assertEqual(lp.PROVIDERS["openrouter"].models[0],
                         "nvidia/nemotron-3-ultra-550b-a55b:free")

    def test_specialists_are_last_in_the_seed_order(self):
        # Requirement, not a guess: a model that exists but writes the wrong
        # register must not lead. The code specialist is last, and the
        # reasoning-tuned id (whose answer can land in `reasoning`) sits behind
        # every plain instruct model.
        seeds = lp.PROVIDERS["openrouter"].models
        self.assertEqual("cohere/north-mini-code:free", seeds[-1])
        self.assertLess(seeds.index("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
                        len(seeds) - 1)

    def test_the_rung_is_bounded_to_three_ids(self):
        rungs = [r for r in self._rungs() if r.startswith("openrouter/")]
        self.assertEqual(lp.PROVIDER_ROUNDS["openrouter"], len(rungs))
        self.assertEqual(3, len(rungs), "the group must not grow with the catalogue")

    def test_a_429_falls_through_to_the_next_id_and_logs_both(self):
        lines = []
        models = ["openrouter/%s" % i for i in self.MEASURED[:3]]
        calls = []

        def run(prompt, model, timeout=None):
            calls.append(model)
            if len(calls) == 1:
                raise ss.Refusal("openrouter: quota_notice (HTTP 429) too many",
                                 kind="quota_notice")
            return json.dumps([{"url": "https://example.com/1",
                                "summary": "A model-written sentence about what "
                                           "happened today."}])

        out = ss.summarise(_one_story(), models=models, run=run,
                           sleep=lambda _s: None, log=lines.append,
                           budget=10 ** 9)
        self.assertEqual(models[:2], calls, "the 429 costs one id, not the provider")
        self.assertEqual(models[1], out["model"])
        joined = "\n".join(lines)
        self.assertIn(models[0], joined)
        self.assertIn(models[1], joined)

    def test_an_empty_answer_falls_through_too(self):
        lines = []
        models = ["openrouter/%s" % i for i in self.MEASURED[:2]]
        calls = []

        def run(prompt, model, timeout=None):
            calls.append(model)
            if len(calls) == 1:
                raise ss.Refusal("%s: empty_answer (HTTP 200)" % model,
                                 kind="empty_answer")
            return json.dumps([{"url": "https://example.com/1",
                                "summary": "A model-written sentence about what "
                                           "happened today."}])

        out = ss.summarise(_one_story(), models=models, run=run,
                           sleep=lambda _s: None, log=lines.append,
                           budget=10 ** 9)
        self.assertEqual(models[1], out["model"], "an empty answer is not the end")
        self.assertIn("empty_answer", "\n".join(out["refusals"]))

    def test_a_404_falls_through_too(self):
        models = ["openrouter/%s" % i for i in self.MEASURED[:2]]
        calls = []

        def run(prompt, model, timeout=None):
            calls.append(model)
            if len(calls) == 1:
                raise ss.Refusal("%s: model_not_found (HTTP 404)" % model,
                                 kind="model_not_found")
            return json.dumps([{"url": "https://example.com/1",
                                "summary": "A model-written sentence about what "
                                           "happened today."}])

        out = ss.summarise(_one_story(), models=models, run=run,
                           sleep=lambda _s: None, log=lambda *_a: None,
                           budget=10 ** 9)
        self.assertEqual(models[1], out["model"])

    def test_the_group_fits_inside_one_rung_of_the_clock(self):
        # The same provider before the expansion (one id) and after (three):
        # the three slices must add up to the ONE slice the single id had.
        before = ["gemini/x", "openrouter/a", "mistral/y"]
        after = ["gemini/x", "openrouter/a", "openrouter/b", "openrouter/c",
                 "mistral/y"]
        single = ss.rung_timeout(before, 1, seconds_left=300, batches_left=1)
        slices = [ss.rung_timeout(after, i, seconds_left=300, batches_left=1)
                  for i in (1, 2, 3)]
        self.assertLessEqual(sum(slices), single,
                             "the group may not outlive the rung it replaces")
        self.assertGreaterEqual(min(slices), 1)

    def test_the_stale_404_id_is_absent_everywhere(self):
        stale = "nvidia/nemotron-3.5-content-safety:free"
        offenders = []
        for path in (REPO_ROOT / "scripts", REPO_ROOT / "config",
                     REPO_ROOT / "tests", REPO_ROOT / "README.md"):
            files = [path] if path.is_file() else list(path.rglob("*"))
            for item in files:
                if not item.is_file():
                    continue
                if item.name == Path(__file__).name:
                    continue      # this test names the id on purpose
                if item.suffix in (".py", ".json", ".yml", ".md"):
                    if stale in item.read_text(encoding="utf-8",
                                               errors="replace"):
                        offenders.append(item.name)
        self.assertEqual([], offenders,
                         "a 404 id in a seed list is a defect, not a spare")


def _one_story():
    return [{"title": "Story", "summary": "Publisher text.",
             "source_url": "https://example.com/1", "category": "world_major"}]


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
