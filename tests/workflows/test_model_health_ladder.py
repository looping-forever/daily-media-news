"""The ladder opens on the rung that has been ANSWERING, and prunes only what
the provider says is gone.

Run 42061741326 spent 99s on `gemini-3.5-flash-lite` twice while
`gemini-3.1-flash-lite` answered, and the next run began with the same broken
rung first. The health file recorded the failures; nothing read them for the
order. These tests pin the two halves of the fix:

  1. health order -- successes outrank an unknown rung, failures fall behind;
  2. catalogue pruning -- an id its own provider no longer lists is dropped,
     and a catalogue that is empty, failed, or shares nothing with the config
     leaves the static ladder exactly as written.
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


class CataloguePruning(unittest.TestCase):
    def setUp(self):
        # `_cli_reachable` reads this; an absent key is the ordinary case.
        patcher = mock.patch.dict(os.environ, {"OPENCODE_CONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _select(self, seed, catalogues, health=None):
        def discover(provider):
            return list(catalogues.get(provider, [])), {"source": "live"}

        config = {"provider": "gemini", "ladder": list(seed)}
        return ms.select(config, health or _health(), cache_path=None,
                         max_models=None, now=10.0, discover_fn=discover)

    def test_a_rung_missing_from_its_providers_catalogue_is_dropped(self):
        """The exact id is absent from a catalogue that DOES list its sibling."""
        seed = ["gemini/a", "gemini/b", "opencode/c"]

        selected, info = self._select(seed, {
            "gemini": ["gemini/a"],
            "opencode": ["opencode/c"],
        })

        self.assertEqual(["gemini/a", "opencode/c"], selected,
                         "gemini/b is not in gemini's catalogue and must not "
                         "be walked")
        self.assertEqual([{"model": "gemini/b", "provider": "gemini"}],
                         info["catalogue_missing"])

    def test_a_drop_is_logged_once_and_never_marked_as_a_failure(self):
        """A catalogue absence is evidence of ABSENCE, not of failure.

        Marking it failing would cool a rung for six hours for a fact about the
        provider's listing, and the rung would come back already punished.
        """
        seed = ["gemini/a", "gemini/b"]
        health = _health()
        before = json.dumps(health, sort_keys=True)

        _selected, info = self._select(seed, {"gemini": ["gemini/a"]}, health)

        line = ms.format_order_log(info["order"])
        self.assertEqual(1, line.count("gemini/b"),
                         "a drop must be stated exactly once in the order line: %r"
                         % line)
        self.assertIn("catalogue-missing", line)
        self.assertEqual(before, json.dumps(health, sort_keys=True),
                         "pruning must never write health")

    def test_an_empty_or_failed_catalogue_leaves_the_static_ladder_intact(self):
        """Absence of a listing is not evidence, so nothing may be pruned."""
        seed = ["gemini/a", "gemini/b"]

        selected, info = self._select(seed, {})

        self.assertEqual(seed, selected)
        self.assertEqual([], info["catalogue_missing"])
        self.assertIn("static ladder kept", info["catalogue"]["gemini"]["note"])

    def test_a_catalogue_that_lists_none_of_the_configured_ids_changes_nothing(self):
        """Run 40280837914 saw a smaller catalogue that omitted FIVE working
        models. A listing that shares nothing with the config proves nothing."""
        seed = ["gemini/a", "gemini/b"]

        selected, info = self._select(seed, {"gemini": ["gemini/unrelated"]})

        self.assertEqual(seed, [m for m in selected if m in seed],
                         "a catalogue with no configured id must not empty the "
                         "provider")
        self.assertEqual([], info["catalogue_missing"])

    def test_pruning_can_never_empty_the_ladder(self):
        """Every provider keeps at least one rung, so the walk always has work."""
        seed = ["gemini/a", "opencode/c"]

        selected, info = self._select(seed, {
            "gemini": ["gemini/a"],
            "opencode": [],          # a failed read, not a verdict
        })

        self.assertTrue(selected, "pruning produced an empty ladder: %r" % info)
        self.assertEqual(["gemini/a", "opencode/c"], selected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
