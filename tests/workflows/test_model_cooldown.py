#!/usr/bin/env python3
"""The HARD per-model cooldown: skip what cannot be served, and say so.

WHY THIS FILE EXISTS
--------------------
On 2026-10-27 four ids were ranked from the provider's catalogue on every run
and cost 4 of the 12 digest attempts before a workable model was reached: three
answered `model_not_found` and one, which the catalogue still listed as active,
answered `Model is unavailable`. (The ids are deliberately not written here:
`tests/breaking/test_ladder_sync.py` bans stale model IDs from every source
file, and that guard is right -- a named dead id is a copy-paste away from
being a ladder rung again.)

Health memory could not help: its invariant is that it may REORDER but never
VETO, so a dead id still came back every run. This suite pins the separate,
narrow veto that fixes it, and -- just as important -- the three things that
keep it from becoming a way to silence the ladder:

  1. SKIP -- a cooled model is removed from the attempt list, and the walk
     falls through to the next ranked model rather than ending;
  2. COST -- a not-found cools for 24h (the id is wrong), an outage for 15
     minutes and only after TWO hits (it is transient), and a quota never
     cools at all (the pool shares it);
  3. LOUD -- if every model is cooled the selection is EMPTY and the CLI exits
     non-zero naming the cooldown. It never reports an empty ladder it does
     not have.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import model_selector as ms  # noqa: E402

PROVIDER = "opencode"
#: Stands for any id the catalogue advertised and the endpoint did not serve.
#: Synthetic on purpose -- a real dead id in a source file is what
#: `test_ladder_sync` exists to prevent.
DEAD = "%s/gone-model-2026-10-27" % PROVIDER
LIVE = "%s/deepseek-v4.1-flash-free" % PROVIDER


def _select(pool, cooldown, now=1000.0, max_models=None):
    """One selection over `pool`, with the cooldown doc the caller supplies."""
    cfg = {"provider": PROVIDER, "ladder": list(pool)}
    return ms.select(cfg, ms.load_health(None), cache_path=None,
                     max_models=max_models, now=now,
                     discover_fn=lambda _p: (list(pool), {"source": "live"}),
                     cooldown=cooldown)


class SkipTests(unittest.TestCase):
    def test_a_not_found_model_is_skipped_by_the_next_selection(self):
        """THE WHOLE FIX: one run proves the id is gone, the next run does not
        pay for it again -- and the walk still starts on a model that is left."""
        cool = ms.load_cooldown(None)
        ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)

        selected, info = _select([DEAD, LIVE], cool, now=1001.0)

        self.assertEqual([LIVE], selected,
                         "the cooled model must not be attempted: %r" % (info,))
        self.assertEqual([DEAD], info["hard_cooled_down"])
        self.assertFalse(info["all_cooled"])

    def test_it_falls_through_to_the_next_ranked_model_not_to_nothing(self):
        """Three rungs, the first two dead. The third is still attempted."""
        pool = ["%s/a" % PROVIDER, "%s/b" % PROVIDER, LIVE]
        cool = ms.load_cooldown(None)
        ms.record_cooldown(cool, pool[0], "model_not_found", at=1000.0)
        ms.record_cooldown(cool, pool[1], "model_not_found", at=1000.0)

        selected, info = _select(pool, cool, now=1001.0)

        self.assertEqual([LIVE], selected, info)
        self.assertFalse(info["all_cooled"])

    def test_health_demotion_is_unchanged_and_still_never_removes(self):
        """The two mechanisms are separate: health still only reorders."""
        pool = ["%s/a" % PROVIDER, "%s/b" % PROVIDER]
        health = ms.load_health(None)
        ms.record_failure(health, pool[1], "timeout", at=1.0)

        cfg = {"provider": PROVIDER, "ladder": list(pool)}
        selected, _info = ms.select(cfg, health, cache_path=None,
                                    max_models=4, now=2.0,
                                    discover_fn=lambda _p: (
                                        list(pool), {"source": "live"}))
        self.assertEqual([pool[0], pool[1]], selected,
                         "health memory must still return every model")

    def test_a_caller_that_passes_no_cooldown_keeps_reorder_only(self):
        """The breaking-news path never opts in, so a committed cooldown file
        cannot silence an alert."""
        pool = [DEAD, LIVE]
        cool = ms.load_cooldown(None)
        ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)

        cfg = {"provider": PROVIDER, "ladder": list(pool)}
        selected, info = ms.select(cfg, ms.load_health(None), cache_path=None,
                                   max_models=None, now=1001.0,
                                   discover_fn=lambda _p: (
                                       list(pool), {"source": "live"}))
        self.assertEqual([DEAD, LIVE], selected, info)


class DurationTests(unittest.TestCase):
    def test_a_not_found_cools_for_a_day_then_returns(self):
        cool = ms.load_cooldown(None)
        row = ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)

        self.assertEqual("model_not_found", row["reason"])
        self.assertEqual(1, row["hits"])
        self.assertTrue(ms.on_hard_cooldown(cool, DEAD, 1000.0 + 23 * 3600))
        self.assertFalse(ms.on_hard_cooldown(cool, DEAD,
                                             1000.0 + 24 * 3600 + 1),
                         "a cooldown that never expires is a blacklist")

    def test_one_outage_is_only_recorded_and_two_earn_fifteen_minutes(self):
        """One 5xx may recover. A repeat is what makes it evidence."""
        cool = ms.load_cooldown(None)
        first = ms.record_cooldown(cool, LIVE, "provider_outage", at=1000.0)
        self.assertEqual(1, first["hits"])
        self.assertNotIn("until", first)
        self.assertFalse(ms.on_hard_cooldown(cool, LIVE, 1001.0),
                         "a single outage must not remove a model")

        second = ms.record_cooldown(cool, LIVE, "provider_outage", at=1001.0)
        self.assertEqual(2, second["hits"])
        self.assertTrue(ms.on_hard_cooldown(cool, LIVE, 1002.0))
        self.assertFalse(ms.on_hard_cooldown(cool, LIVE, 1002.0 + 15 * 60 + 1),
                         "an outage cooldown must be the SHORT one")
        self.assertLess(1002.0 + 15 * 60,
                        1000.0 + ms.COOLDOWN_SEC["model_not_found"])

    def test_a_quota_failure_cools_nothing_down(self):
        """THE POOL SHARES THAT LIMIT. Cooling one model over it, then the next,
        would empty the ladder and end the run early -- the opposite of the
        fix."""
        cool = ms.load_cooldown(None)
        self.assertIsNone(
            ms.record_cooldown(cool, LIVE, "quota_or_rate_limited", at=1000.0))
        self.assertEqual({}, cool["models"])

        selected, info = _select([DEAD, LIVE], cool, now=1001.0)
        self.assertEqual([DEAD, LIVE], selected, info)
        self.assertEqual([], info["hard_cooled_down"])

    def test_other_classes_are_ignored_by_the_cooldown(self):
        cool = ms.load_cooldown(None)
        for kind in ("other", "slow_or_stalled", "transport", "auth", "ok"):
            with self.subTest(kind=kind):
                self.assertIsNone(ms.record_cooldown(cool, LIVE, kind, at=1.0))
        self.assertEqual({}, cool["models"])

    def test_a_success_clears_the_entry(self):
        """A model that answers today proves the id exists, whatever it did
        before."""
        cool = ms.load_cooldown(None)
        ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)
        self.assertTrue(ms.clear_cooldown(cool, DEAD))
        self.assertFalse(ms.on_hard_cooldown(cool, DEAD, 1001.0))
        self.assertFalse(ms.clear_cooldown(cool, DEAD),
                         "clearing an absent entry is a no-op, not an error")


class PruneTests(unittest.TestCase):
    def test_an_expired_cooldown_is_dropped(self):
        cool = ms.load_cooldown(None)
        ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)
        ms.record_cooldown(cool, LIVE, "provider_outage", at=1000.0)
        ms.record_cooldown(cool, LIVE, "provider_outage", at=1001.0)

        dropped = ms.prune_cooldown(cool, now=1000.0 + 24 * 3600 + 1)

        self.assertEqual([LIVE, DEAD], sorted(dropped))
        self.assertEqual({}, cool["models"])

    def test_a_pending_outage_hit_stops_counting_after_a_day(self):
        """Otherwise two single 5xx a month apart would add up to a cooldown."""
        cool = ms.load_cooldown(None)
        ms.record_cooldown(cool, LIVE, "provider_outage", at=1000.0)

        self.assertEqual([], ms.prune_cooldown(
            cool, now=1000.0 + ms.COOLDOWN_HIT_TTL_SEC - 1))
        self.assertEqual([LIVE], ms.prune_cooldown(
            cool, now=1000.0 + ms.COOLDOWN_HIT_TTL_SEC + 1))

    def test_pruning_runs_on_selection_so_expiry_cannot_veto(self):
        cool = ms.load_cooldown(None)
        ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)

        selected, info = _select([DEAD, LIVE], cool,
                                 now=1000.0 + 24 * 3600 + 1)

        self.assertEqual([DEAD, LIVE], selected, info)
        self.assertEqual([], info["hard_cooled_down"])
        self.assertEqual({}, cool["models"], "the prune must be visible")


class AllCooledTests(unittest.TestCase):
    def test_the_library_reports_an_empty_selection_and_says_why(self):
        pool = [DEAD, LIVE]
        cool = ms.load_cooldown(None)
        for model in pool:
            ms.record_cooldown(cool, model, "model_not_found", at=1000.0)

        selected, info = _select(pool, cool, now=1001.0, max_models=4)

        self.assertEqual([], selected)
        self.assertTrue(info["all_cooled"])
        self.assertEqual(sorted(pool), sorted(info["hard_cooled_down"]))

    def test_the_cli_fails_loudly_naming_the_cooldown(self):
        """NOT a silent no-op, and NOT an "empty ladder" -- the ladder is real
        and every rung is barred. The message must say which."""
        pool = [DEAD, LIVE]
        with tempfile.TemporaryDirectory(prefix="cool-all-") as sd:
            cfg = os.path.join(sd, "opencode.json")
            health = os.path.join(sd, "health.json")
            cache = os.path.join(sd, "cache.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"provider": PROVIDER, "ladder": pool}, fh)

            cool = ms.load_cooldown(ms.cooldown_path_for(health))
            for model in pool:
                # Stamped against the REAL clock, because the CLI prunes with
                # the real clock. A fixed 1000.0 here is 1970 and is correctly
                # pruned as expired before it can prove anything.
                ms.record_cooldown(cool, model, "model_not_found",
                                   at=time.time())
            ms.save_cooldown(ms.cooldown_path_for(health), cool)

            saved_discover = ms.discover
            ms.discover = lambda _p, cache_path=None: (
                list(pool), {"source": "live"})
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(err):
                    code = ms._main(["--select", "--config", cfg,
                                     "--health", health, "--cache", cache,
                                     "--all"])
            finally:
                ms.discover = saved_discover

        self.assertEqual(1, code, err.getvalue())
        message = err.getvalue()
        self.assertIn("hard cooldown", message)
        self.assertIn(DEAD, message)
        self.assertNotIn("empty model ladder", message)


class FileTests(unittest.TestCase):
    def test_the_entry_round_trips_as_an_absolute_iso_stamp(self):
        with tempfile.TemporaryDirectory(prefix="cool-file-") as sd:
            path = os.path.join(sd, ms.COOLDOWN_FILENAME)
            cool = ms.load_cooldown(path)
            ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)
            self.assertTrue(ms.save_cooldown(path, cool))

            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            entry = doc["models"][DEAD]
            self.assertEqual("model_not_found", entry["reason"])
            self.assertEqual(1, entry["hits"])
            self.assertTrue(entry["until"].endswith("+00:00"), entry["until"])
            self.assertEqual(1000.0 + 24 * 3600,
                             ms._epoch(entry["until"]))

            reloaded = ms.load_cooldown(path)
            self.assertTrue(ms.on_hard_cooldown(reloaded, DEAD, 1001.0))

    def test_a_corrupt_file_loses_the_veto_and_never_the_run(self):
        with tempfile.TemporaryDirectory(prefix="cool-bad-") as sd:
            path = os.path.join(sd, ms.COOLDOWN_FILENAME)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual({"models": {}},
                             {"models": ms.load_cooldown(path)["models"]})
            self.assertIsNone(ms.cooldown_path_for(None))

    def test_the_cli_records_then_skips_across_two_runs(self):
        """THE COMMITTED FILE IS THE POINT: run N writes it, run N+1 reads it."""
        with tempfile.TemporaryDirectory(prefix="cool-cli-") as sd:
            cfg = os.path.join(sd, "opencode.json")
            health = os.path.join(sd, "health.json")
            cache = os.path.join(sd, "cache.json")
            info_path = os.path.join(sd, "info.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"provider": PROVIDER, "ladder": [DEAD, LIVE]}, fh)

            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0, ms._main([
                    "--record", DEAD, "model_not_found", "--health", health]))
            self.assertTrue(os.path.isfile(ms.cooldown_path_for(health)))

            saved_discover = ms.discover
            ms.discover = lambda _p, cache_path=None: (
                [DEAD, LIVE], {"source": "live"})
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(io.StringIO()):
                    code = ms._main(["--select", "--all", "--config", cfg,
                                     "--health", health, "--cache", cache,
                                     "--info-out", info_path])
            finally:
                ms.discover = saved_discover

            self.assertEqual(0, code)
            self.assertEqual([LIVE], out.getvalue().split())
            with open(info_path, encoding="utf-8") as fh:
                info = json.load(fh)
            self.assertEqual([DEAD], info["hard_cooled_down"])
            self.assertFalse(info["all_cooled"])

    def test_a_success_clears_a_recorded_cooldown_on_disk(self):
        with tempfile.TemporaryDirectory(prefix="cool-ok-") as sd:
            health = os.path.join(sd, "health.json")
            path = ms.cooldown_path_for(health)
            cool = ms.load_cooldown(path)
            ms.record_cooldown(cool, DEAD, "model_not_found", at=1000.0)
            ms.save_cooldown(path, cool)

            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0, ms._main([
                    "--record", DEAD, "ok", "--health", health]))

            with open(path, encoding="utf-8") as fh:
                self.assertEqual({}, json.load(fh)["models"])


class VocabularyTests(unittest.TestCase):
    def test_a_classifier_class_never_becomes_a_health_counter(self):
        """Health memory speaks timeout/transport/garbage. A missing model is a
        failed attempt there, exactly as before -- it just does not invent a
        new counter nobody reads."""
        doc = ms.load_health(None)
        ms.record_failure(doc, DEAD, "model_not_found", at=1.0)
        row = doc["models"][DEAD]
        self.assertEqual(1, row["transport"])
        self.assertEqual(1, row["consecutive_failures"])
        self.assertNotIn("model_not_found", row)

    def test_the_measured_unavailable_label_is_an_outage(self):
        """`Model is unavailable` is the endpoint withdrawing a listed model,
        not a missing id, so it earns the short repeat cooldown -- not the 24h
        one, and not `other` (which would never cool at all)."""
        self.assertEqual(
            "provider_outage",
            ms.classify_failure("Model is unavailable", 1))
        self.assertEqual(
            "provider_outage",
            ms.classify_failure("AI_APICallError: Model is unavailable.", 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
