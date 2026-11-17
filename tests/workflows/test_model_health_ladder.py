"""The ladder opens on the rung that has been ANSWERING, and removes only what
the provider says is gone.

Run 42061741326 spent 99s on `gemini-3.5-flash-lite` twice while
`gemini-3.1-flash-lite` answered, and the next run began with the same broken
rung first. The health file recorded the failures; nothing read them for the
order. These tests pin the two halves of the fix:

  1. health order -- successes outrank an unknown rung, failures fall behind;
  2. catalogue absence -- an id its own provider does not list is ranked last,
     never removed; only an explicit terminal status record removes a rung.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import model_selector as ms  # noqa: E402


def _health():
    return ms.load_health(None)


def _sick(doc, model, successes=0, timeouts=0):
    for _ in range(successes):
        ms.record_success(doc, model, at=1.0)
    for _ in range(timeouts):
        ms.record_failure(doc, model, "timeout", at=1.0)
    return doc


class HealthOrder(unittest.TestCase):
    def test_a_reliable_rung_outranks_an_unknown_one_and_a_failing_rung_falls(self):
        """Measured history decides, not the seed position alone.

        PROVEN has answered five times and timed out three; UNKNOWN has never
        been tried; BROKEN has answered once and timed out four times. The
        configured order puts BROKEN first. Health must reorder all three.
        """
        doc = _health()
        _sick(doc, "gemini/proven", successes=5, timeouts=3)
        _sick(doc, "gemini/broken", successes=1, timeouts=4)
        seed = ["gemini/broken", "gemini/proven", "gemini/unknown"]

        # Far enough forward that the cooldowns those failures earned have
        # expired: this test is about the SCORE, not the cooldown.
        ordered = ms.rank_models(seed, seed=seed, health=doc, now=1_000_000.0)

        self.assertEqual(["gemini/proven", "gemini/unknown", "gemini/broken"],
                         ordered,
                         "a reliable rung must lead an unknown one, and a "
                         "failing rung must fall behind both")

    def test_the_configured_order_still_decides_between_equally_healthy_rungs(self):
        """Health is an override, not a replacement: a tie is the owner's call."""
        doc = _health()
        _sick(doc, "gemini/a", successes=2)
        _sick(doc, "gemini/b", successes=2)
        seed = ["gemini/b", "gemini/a"]

        self.assertEqual(seed, ms.rank_models(seed, seed=seed, health=doc,
                                              now=10.0))

    def test_a_cooling_rung_still_ranks_last_however_reliable_it_is(self):
        """A cooling rung is a KNOWN cost, not a preference."""
        doc = _health()
        _sick(doc, "gemini/reliable", successes=10)
        _sick(doc, "gemini/flaky", successes=1, timeouts=5)
        row = doc["models"]["gemini/reliable"]
        row["cooldown_until"] = 9_999_999.0

        ordered = ms.rank_models(["gemini/reliable", "gemini/flaky"],
                                 seed=["gemini/reliable", "gemini/flaky"],
                                 health=doc, now=1_000_000.0)

        self.assertEqual(["gemini/flaky", "gemini/reliable"], ordered,
                         "a live health cooldown must send the rung to the back")

    def test_a_blocked_cause_records_nothing_and_cools_nothing(self):
        """`blocked` is the edge refusing the NETWORK, not the rung.

        A bad key would fail every provider identically, and a block is about
        the address. Recording it would cool a model that never answered.
        """
        doc = _health()

        row = ms.record_failure(doc, "gemini/a", "blocked", at=1.0)

        self.assertIsNone(row, "blocked must not produce a health row")
        self.assertEqual({}, doc["models"],
                         "blocked must not cool or demote a model that never "
                         "got a chance to answer")


class CatalogueAbsence(unittest.TestCase):
    def setUp(self):
        # `_cli_reachable` reads this; an absent key is the ordinary case.
        patcher = mock.patch.dict(os.environ, {"OPENCODE_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _select(self, seed, catalogues, health=None, statuses=None):
        def discover(provider):
            return list(catalogues.get(provider, [])), {
                "source": "live",
                "catalogue_status": dict(statuses or {}),
            }

        config = {"provider": "gemini", "ladder": list(seed)}
        return ms.select(config, health or _health(), cache_path=None,
                         max_models=None, now=10.0, discover_fn=discover)

    def test_an_absent_rung_is_kept_and_ranked_last(self):
        """Absence from a catalogue that lists a sibling is ORDER evidence."""
        seed = ["gemini/a", "gemini/b", "opencode/c"]

        selected, info = self._select(seed, {
            "gemini": ["gemini/a"],
            "opencode": ["opencode/c"],
        })

        self.assertEqual(["gemini/a", "opencode/c", "gemini/b"], selected,
                         "gemini/b is absent but must still be walked, last")
        self.assertEqual([{"model": "gemini/b", "provider": "gemini"}],
                         info["catalogue_absent"])
        self.assertEqual([], info["dropped_seed"])

    def test_an_absent_rung_is_logged_once_and_never_marked_as_a_failure(self):
        """A catalogue absence is evidence of ABSENCE, not of failure.

        Marking it failing would cool a rung for six hours for a fact about the
        provider's listing, and the rung would come back already punished.
        """
        seed = ["gemini/a", "gemini/b"]
        health = _health()
        before = json.dumps(health, sort_keys=True)

        selected, info = self._select(seed, {"gemini": ["gemini/a"]}, health)

        self.assertEqual(seed, selected)
        self.assertEqual([], info["dropped_seed"])
        line = ms.format_order_log(info["order"])
        self.assertEqual(1, line.count("gemini/b"),
                         "an absence must be stated exactly once in the order "
                         "line: %r" % line)
        self.assertIn("catalogue-absent", line)
        self.assertNotIn("catalogue-missing", line)
        self.assertEqual(before, json.dumps(health, sort_keys=True),
                         "absence must never write health")

    def test_a_terminal_status_record_still_removes_the_rung(self):
        """The one removal that is evidence: the catalogue says it is gone."""
        seed = ["gemini/a", "gemini/b"]

        selected, info = self._select(
            seed, {"gemini": ["gemini/a"]},
            statuses={"gemini/b": "deprecated"})

        self.assertEqual(["gemini/a"], selected)
        self.assertEqual(["gemini/b"], info["dropped_seed"])
        self.assertIn("deprecated",
                      info["dropped_seed_evidence"]["gemini/b"])

    def test_an_empty_or_failed_catalogue_leaves_the_static_ladder_intact(self):
        """Absence of a listing is not evidence, so nothing may be reordered."""
        seed = ["gemini/a", "gemini/b"]

        selected, info = self._select(seed, {})

        self.assertEqual(seed, selected)
        self.assertEqual([], info["catalogue_absent"])
        self.assertIn("static ladder kept", info["catalogue"]["gemini"]["note"])

    def test_a_catalogue_that_lists_none_of_the_configured_ids_changes_nothing(self):
        """Run 40280837914 saw a smaller catalogue that omitted FIVE working
        models. A listing that shares nothing with the config proves nothing."""
        seed = ["gemini/a", "gemini/b"]

        selected, info = self._select(seed, {"gemini": ["gemini/unrelated"]})

        self.assertEqual(seed, [m for m in selected if m in seed],
                         "a catalogue with no configured id must not reorder "
                         "the provider")
        self.assertEqual([], info["catalogue_absent"])

    def test_absence_can_never_empty_the_ladder(self):
        """Every rung stays, so the walk always has work."""
        seed = ["gemini/a", "opencode/c"]

        selected, info = self._select(seed, {
            "gemini": ["gemini/a"],
            "opencode": [],          # a failed read, not a verdict
        })

        self.assertTrue(selected, "absence produced an empty ladder: %r" % info)
        self.assertEqual(["gemini/a", "opencode/c"], selected)


class GroqCliRungs(unittest.TestCase):
    """Groq is NOT a CLI walk rung any more; it is still a summariser rung.

    The ordering and catalogue rules below are exercised with `groq/*` SEED ids
    because they are generic provider rules -- an external provider's rung is
    ranked behind Gemini and ahead of the shared pool, an absent catalogue entry
    is reordered rather than removed. What changed on 2026-11-17 is only that
    the shipped ladder no longer SEEDS one: `groq/allam-2-7b` answers `tool
    calling is not supported with this model`, so it could never complete a
    walk turn (dry run 44049081005) while answering the summariser's plain HTTP
    call perfectly well.
    """

    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        self.config = ms.load_config(str(root / "config" / "opencode.json"))
        self.cli_config = root / "config" / "opencode-cli.json"

    def _select(self, seed, env, catalogues=None):
        catalogues = catalogues or {}

        def discover(provider):
            return list(catalogues.get(provider, [])), {
                "source": "live",
                "catalogue_status": {},
            }

        with mock.patch.dict(os.environ, env):
            return ms.select(self.config if seed is None else
                             {"provider": "gemini", "ladder": list(seed)},
                             _health(), cache_path=None, max_models=None,
                             now=10.0, discover_fn=discover)

    def test_the_shipped_ladder_seeds_no_groq_rung_and_keeps_gemini_first(self):
        """TEN RUNGS, NONE OF THEM GROQ, AND STILL ORDERED BY POOL.

        This replaced an assertion that BOTH Groq rungs sat between Gemini and
        the opencode pool. The rungs are gone, so the interesting claim is now
        the boundary itself: nothing in the shipped seed is a Groq id, and the
        two free pools still follow the two Gemini keys.
        """
        ladder = self.config["ladder"]
        gemini = [i for i, m in enumerate(ladder)
                  if m.startswith(("gemini/", "gemini-key2/"))]
        groq = [i for i, m in enumerate(ladder)
                if m.startswith(("groq/", "groq-key2/"))]
        pool = [i for i, m in enumerate(ladder) if m.startswith("opencode/")]

        self.assertEqual([], groq,
                         "a Groq rung is back in config/opencode.json; its model "
                         "cannot call tools, so it fails every walk on turn one")
        self.assertTrue(gemini and pool)
        self.assertLess(max(gemini), min(pool))
        self.assertEqual(10, len(ladder))

        selected, _info = self._select(None, {"OPENCODE_CONFIG": ""})
        self.assertEqual(ladder, selected)

    def test_a_healthy_groq_rung_never_outranks_an_unknown_gemini_rung(self):
        doc = _health()
        _sick(doc, "groq/healthy", successes=5)
        seed = ["gemini/unknown", "groq/healthy"]

        ordered = ms.rank_models(seed, seed=seed, health=doc, now=1_000_000.0)

        self.assertEqual(seed, ordered)

    def test_an_empty_groq_key_contributes_no_rung(self):
        selected, info = self._select(
            None,
            {"OPENCODE_CONFIG": str(self.cli_config),
             "GEMINI_API_KEY": "present",
             "GEMINI_API_KEY_2": "present",
             "GROQ_API_KEY": "",
             "GROQ_API_KEY_2": "   "})

        self.assertEqual([], [m for m in selected
                              if m.startswith(("groq/", "groq-key2/"))])
        self.assertEqual([], [m for m in info["seed"]
                              if m.startswith(("groq/", "groq-key2/"))])
        self.assertTrue([m for m in selected if m.startswith("opencode/")])

    def test_an_absent_groq_catalogue_rung_is_reordered_not_removed(self):
        seed = ["gemini/a", "groq/absent-model",
                "groq/allam-2-7b", "opencode/free"]

        selected, info = self._select(
            seed, {"OPENCODE_CONFIG": ""},
            {"gemini": ["gemini/a"],
             "groq": ["groq/allam-2-7b"],
             "opencode": ["opencode/free"]})

        self.assertEqual(["gemini/a", "groq/allam-2-7b",
                          "opencode/free", "groq/absent-model"], selected)
        self.assertEqual([{"model": "groq/absent-model", "provider": "groq"}],
                         info["catalogue_absent"])
        self.assertEqual([], info["dropped_seed"])

    def test_a_groq_gone_status_still_removes_the_rung(self):
        seed = ["gemini/a", "groq/gone"]
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG": ""}):
            selected, info = ms.select(
                {"provider": "gemini", "ladder": seed}, _health(),
                cache_path=None, max_models=None, now=10.0,
                discover_fn=lambda provider: (
                    ["gemini/a"] if provider == "gemini" else [],
                    {"source": "live",
                     "catalogue_status": ({} if provider == "gemini" else
                                          {"groq/gone": "retired"})}))

        self.assertEqual(["gemini/a"], selected)
        self.assertEqual(["groq/gone"], info["dropped_seed"])

    def test_the_cli_config_declares_no_groq_walk_provider(self):
        """GROQ IS NOT A WALK PROVIDER ANY MORE (2026-11-17).

        Dry run 44049081005: both `groq/allam-2-7b` rungs failed every attempt
        with `tool calling is not supported with this model`, so a provider
        block here buys a ladder position that cannot complete one turn. Groq
        still summarises the derived edition over HTTP -- scripts/llm_providers.py
        owns that ladder and keeps its own 4096-token ceiling -- so its absence
        from the CLI config is the walk's boundary, not the provider's.
        """
        doc = json.loads(self.cli_config.read_text(encoding="utf-8"))
        for name in ("groq", "groq-key2"):
            self.assertNotIn(
                name, doc["provider"],
                "config/opencode-cli.json declares a `%s` provider again, so a "
                "groq rung can be discovered by a walk that cannot use it" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
