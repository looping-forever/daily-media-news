#!/usr/bin/env python3
"""tests/digest/test_github_models_family.py -- the fourth keyed HTTP family.

WHY THIS EXISTS. Groq, OpenRouter and Mistral are three providers behind three
public endpoints, but all three are still "an API key the owner minted". GitHub
Models is a different kind of credential: it is the workflow's own
`GITHUB_TOKEN`, scoped by the `models: read` permission rather than by a secret.
That makes it worth having -- and it makes the scope rule the thing to pin:

    THE SUMMARISER AND THE REPAIR WALK IT -- both call `ladder_models()`.
    THE OTHER TWO WALKS NEVER DO -- the breaking classifier and the CLI digest
        walk seed from config/opencode.json, and this family is not a seed.
    THE REQUEST IS THE DOCUMENTED ONE -- models.github.ai, one Bearer header,
        namespaced ids (`openai/gpt-4o-mini`).
    NO TOKEN, NO RUNG -- and the rest of the ladder is byte-identical.
    ONE REASON VOCABULARY -- 429 `quota_notice`, a JSON auth 403 `auth`, an
        HTML/edge 403 `blocked`, which cools nothing.
"""

import json
import os
import sys
import unittest
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import llm_providers as lp  # noqa: E402
import model_selector  # noqa: E402
import summarise_stories as ss  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The three providers this repository already had. Their keys are what make the
#: "no token shortens nothing" comparison meaningful.
KEYS = {
    "GROQ_API_KEY": "test-groq-key",
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "MISTRAL_API_KEY": "test-mistral-key",
    "GEMINI_API_KEY": "test-gemini-key",
}

GITHUB_TOKEN = {"GITHUB_TOKEN": "test-github-token"}

GITHUB_CATALOGUE = ("openai/gpt-4o-mini", "meta/llama-3.3-70b-instruct")

OK_BODY = json.dumps({"choices": [{"message": {"content": "OK"}}]})

CONFIG_LADDER = (
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.1-flash-lite",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/beam-free",
)


def router(catalogues=None, chat=(200, OK_BODY)):
    """(fake transport, recorded specs): `/models` answered per provider."""
    calls = []
    catalogues = dict(catalogues or {})

    def send(spec):
        calls.append(spec)
        if spec["url"].endswith("/models"):
            for name, ids in catalogues.items():
                if lp.PROVIDERS[name].models_url == spec["url"]:
                    return 200, json.dumps({"data": [{"id": i} for i in ids]})
            return 200, json.dumps({"data": []})
        return chat

    return send, calls


def transport_returning(status, body):
    """(fake transport, recorded specs). Answers every request the same way."""
    calls = []

    def send(spec):
        calls.append(spec)
        return status, body

    return send, calls


class TheRequestTests(unittest.TestCase):
    """The family is a `Provider` record like the other three, and nothing else."""

    def test_the_endpoints_and_the_credential_name_are_the_documented_ones(self):
        record = lp.provider("github")
        self.assertEqual("https://models.github.ai/inference/chat/completions",
                         record.url)
        self.assertEqual("https://models.github.ai/inference/models",
                         record.models_url)
        self.assertEqual("GITHUB_TOKEN", record.env)

    def test_the_two_seed_ids_are_namespaced(self):
        seeds = lp.PROVIDERS["github"].models
        self.assertEqual(("openai/gpt-4o-mini",
                          "meta/llama-3.3-70b-instruct"), seeds)

    def test_the_chat_request_carries_the_namespaced_id_and_one_bearer_key(self):
        spec = lp.request_spec("github", "github/openai/gpt-4o-mini", "p", "k")
        self.assertEqual("https://models.github.ai/inference/chat/completions",
                         spec["url"])
        self.assertEqual("Bearer k", spec["headers"]["Authorization"])
        self.assertEqual("openai/gpt-4o-mini",
                         json.loads(spec["body"])["model"],
                         "the rung prefix names the provider; the API takes the "
                         "bare namespaced id")
        self.assertEqual(1, len([h for h in spec["headers"]
                                 if h.lower() == "authorization"]),
                         "the credential must travel in exactly one header")

    def test_provider_of_recognises_the_family(self):
        self.assertEqual("github", lp.provider_of("github/openai/gpt-4o-mini"))


class ReachabilityTests(unittest.TestCase):
    """Which walk may carry the family, and which must never."""

    def test_no_token_contributes_no_rung_and_says_so_once(self):
        log = []
        got = lp.ladder_rungs(environ=KEYS, log=log.append,
                              providers=lp.HTTP_PROVIDERS)
        self.assertEqual([], [m for m in got if m.startswith("github/")])
        self.assertEqual(["llm_providers: github contributes no rung -- no key "
                          "configured"], log)
        self.assertEqual(
            lp.ladder_rungs(environ=KEYS), got,
            "the wider set must add nothing at all without a token")

    def test_an_empty_token_is_the_same_as_an_absent_one(self):
        got = lp.ladder_rungs(environ=dict(KEYS, GITHUB_TOKEN="   "),
                              transport=router()[0],
                              providers=lp.HTTP_PROVIDERS)
        self.assertEqual([], [m for m in got if m.startswith("github/")])

    def test_a_token_adds_exactly_one_rung_and_shortens_nothing(self):
        catalogues = {
            "groq": ["llama-3.3-70b-versatile"],
            "openrouter": ["recursive/flint-v1.0:free"],
            "mistral": ["mistral-small-latest"],
            "github": list(GITHUB_CATALOGUE),
        }
        send, _ = router(catalogues)
        lp.clear_catalogue()
        without = lp.ladder_rungs(environ=KEYS, transport=send, resolve=True,
                                  providers=lp.HTTP_PROVIDERS)
        lp.clear_catalogue()
        with_token = lp.ladder_rungs(environ=dict(KEYS, **GITHUB_TOKEN),
                                     transport=send, resolve=True,
                                     providers=lp.HTTP_PROVIDERS)
        self.assertEqual(
            [m for m in with_token if not m.startswith("github/")], list(without),
            "adding the family must not change any other rung")
        github = [m for m in with_token if m.startswith("github/")]
        self.assertEqual(["github/openai/gpt-4o-mini"], github,
                         "one provider is ONE rung, proven against its own "
                         "catalogue -- got %r" % (with_token,))

    def test_the_summariser_ladder_reaches_it(self):
        send, _ = router({"github": list(GITHUB_CATALOGUE)})
        lp.clear_catalogue()
        got = ss.ladder_models(transport=send,
                               environ=dict(KEYS, **GITHUB_TOKEN))
        github = [m for m in got if m.startswith("github/")]
        self.assertEqual(["github/openai/gpt-4o-mini"], github)
        self.assertEqual(1, len(github), "the family appears once, not twice")

    def test_the_summariser_ladder_places_it_after_mistral_not_in_the_pool(self):
        catalogues = {
            "groq": ["llama-3.3-70b-versatile"],
            "openrouter": ["recursive/flint-v1.0:free"],
            "mistral": ["mistral-small-latest"],
            "github": list(GITHUB_CATALOGUE),
        }
        send, _ = router(catalogues)
        lp.clear_catalogue()
        got = ss.ladder_models(transport=send,
                               environ=dict(KEYS, **GITHUB_TOKEN))
        index = [i for i, m in enumerate(got) if m.startswith("github/")]
        self.assertEqual(1, len(index), "one rung, not one per seed")
        self.assertTrue(got[index[0] - 1].startswith("mistral/"),
                        "the family sits behind the three keyed providers: %r"
                        % (got,))
        self.assertTrue(all(m.startswith("opencode/")
                            for m in got[index[0] + 1:]),
                        "and in front of the shared free pool: %r" % (got,))

    def test_the_summariser_ladder_is_unchanged_without_a_token(self):
        send, _ = router({})
        lp.clear_catalogue()
        got = ss.ladder_models(transport=send, environ=KEYS)
        self.assertEqual([], [m for m in got if m.startswith("github/")])

    def test_the_breaking_walk_never_carries_it(self):
        import breaking_monitor as bm
        with_token = dict(KEYS, **GITHUB_TOKEN)
        self.assertEqual(
            [], [m for m in lp.ordered_ladder(
                list(CONFIG_LADDER), environ=with_token)
                if m.startswith("github/")],
            "the classifier's ladder is built from LADDER_PROVIDERS, not from "
            "every provider this module can call")
        self.assertEqual(
            [], [m for m in bm._ladder_for(list(CONFIG_LADDER), ())
                if m.startswith("github/")],
            "breaking_monitor._ladder_for must not reach the family either")

    def test_the_digest_walk_seed_never_names_it(self):
        config = model_selector.load_config(
            os.path.join(str(REPO_ROOT), "config", "opencode.json"))
        self.assertEqual([], [m for m in (config.get("ladder") or [])
                              if m.startswith("github/")],
                         "the CLI digest walk seeds from this file, so a rung "
                         "here would make a tool-using walk call GitHub Models")


class ClassificationTests(unittest.TestCase):
    """The reason words the rest of the repository already speaks."""

    def test_a_429_is_quota(self):
        send, _ = transport_returning(
            429, json.dumps({"error": {"message": "rate limited"}}))
        got = lp.call("github", "p", transport=send, environ=GITHUB_TOKEN)
        self.assertEqual("quota_notice", got.reason)
        self.assertEqual("quota", lp.PROBE_VERDICT[got.reason])

    def test_a_json_auth_403_is_auth(self):
        send, _ = transport_returning(
            403, json.dumps({"error": {"code": "unauthorized",
                                       "message": "Bad credentials"}}))
        got = lp.call("github", "p", transport=send, environ=GITHUB_TOKEN)
        self.assertEqual("auth", got.reason)
        self.assertEqual("", got.block)

    def test_a_github_scope_403_is_auth_not_an_edge_block(self):
        send, _ = transport_returning(
            403, json.dumps({"message": "Resource not accessible by "
                                        "integration"}))
        got = lp.call("github", "p", transport=send, environ=GITHUB_TOKEN)
        self.assertEqual("auth", got.reason,
                         "a token without models:read is a scope problem, not "
                         "an IP/egress decision")

    def test_an_html_403_is_blocked_and_cools_nothing(self):
        send, _ = transport_returning(
            403, "<!DOCTYPE html><html><body>error code: 1010</body></html>")
        got = lp.call("github", "p", transport=send, environ=GITHUB_TOKEN)
        self.assertEqual("blocked", got.reason)
        self.assertEqual("cloudflare", got.block)
        cooldowns = {}
        self.assertIsNone(ss.record_refusal(
            cooldowns, "github/openai/gpt-4o-mini", got.reason))
        self.assertEqual({}, cooldowns, "a blocked edge is not the model's fault")

    def test_a_missing_token_never_makes_a_request(self):
        send, calls = transport_returning(200, OK_BODY)
        got = lp.call("github", "p", transport=send, environ={})
        self.assertEqual("not_configured", got.reason)
        self.assertEqual([], calls)


class ProbeRowTests(unittest.TestCase):
    """Same columns as every other row, and a `not_configured` when absent."""

    def test_the_absent_row_is_the_pinned_shape(self):
        rows = lp.probe_rows(transport=router()[0], environ=KEYS)
        got = [r for r in rows if r.startswith("github | ")]
        self.assertEqual(
            ["github | key=absent | models=0 | chosen=- | tried=0 | status=- "
             "| ms=0 | verdict=not_configured"], got)

    def test_the_configured_row_proves_the_catalogue_and_one_answer(self):
        send, _ = router({"github": list(GITHUB_CATALOGUE)})
        rows = lp.probe_rows(transport=send,
                             environ=dict(KEYS, **GITHUB_TOKEN))
        row = [r for r in rows if r.startswith("github | ")][0]
        self.assertEqual(
            "github | key=present | models=2 | chosen=openai/gpt-4o-mini "
            "| tried=1 | status=200 | ms=0 | verdict=ok", row,
            "the columns are the ones the workflow header names -- got %r"
            % (row,))

    def test_the_probe_lists_the_family_after_the_three_keyed_providers(self):
        rows = lp.probe_rows(transport=router()[0], environ=KEYS)
        names = [r.split(" | ", 1)[0] for r in rows if " | key=" in r]
        self.assertEqual(["gemini", "gemini-key2", "groq", "openrouter",
                          "mistral", "github"], names)


if __name__ == "__main__":
    unittest.main()
