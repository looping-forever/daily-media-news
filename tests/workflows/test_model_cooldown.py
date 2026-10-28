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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import model_selector as ms  # noqa: E402

try:
    from tests import workflows as W
except ImportError:  # pragma: no cover - direct execution of this file
    sys.path.insert(0, str(REPO_ROOT))
    from tests import workflows as W

#: `python3`, replaced by a shim: Windows ships no `python3` that Git-for-Windows
#: bash can execute, and the step's read-back check is real Python. It must run
#: for real -- asserting that the text mentions json.load proves nothing.
_PY3_SHIM = r"""#!/usr/bin/env bash
exec "${STUB_REAL_PYTHON:?}" "$@"
"""

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


class CommittedCooldownTests(unittest.TestCase):
    """The veto is worthless unless the file is IN git.

    WHY THIS EXISTS. The cooldown was written on the runner and published by the
    step `Commit state changes` -- which carried no `always()`, so every run that
    RECORDED a dead id also failed the walk, and the file died with the
    container. On 2026-10-27 the repository held no `state/model_cooldown.json`
    at all (`git ls-files` and `git check-ignore` both confirmed it), so the same
    three ids were discovered, ranked and tried again on every run -- three
    attempts of a twelve-attempt walk, exactly the waste the veto was built for.
    The file is committed now, and it is published by `Persist model cooldowns`,
    which runs on `always()` (see PersistedCooldownStepTests). The test below
    keeps it honest DATA rather than a file that silently stops vetoing anything.
    """

    PATH = REPO_ROOT / "state" / ms.COOLDOWN_FILENAME

    def test_the_committed_file_is_a_veto_the_selector_understands(self):
        self.assertTrue(
            self.PATH.is_file(),
            "state/%s must be committed. An uncommitted cooldown is rebuilt from "
            "nothing on every clean runner, which is the defect it exists to "
            "remove." % ms.COOLDOWN_FILENAME)
        doc = ms.load_cooldown(str(self.PATH))
        self.assertEqual(ms.COOLDOWN_SCHEMA_VERSION, doc.get("schema_version"))
        models = doc.get("models") or {}
        self.assertTrue(
            models,
            "a committed cooldown with no entries is dead weight: either name "
            "the ids that must not be tried, or delete the file and let the "
            "recorder recreate it")
        for model, row in models.items():
            with self.subTest(model=model):
                self.assertTrue(
                    model.startswith(PROVIDER + "/"),
                    "%r is not a fully-qualified model id, so it can never match "
                    "the catalogue" % (model,))
                self.assertIn(
                    row.get("reason"), ms.COOLDOWN_SEC,
                    "the reason must be one of the cooled classes (%r), because "
                    "that is the only thing that says how long it lasts"
                    % (sorted(ms.COOLDOWN_SEC),))
                # `until` is ABSOLUTE. now=0 makes every real future stamp true,
                # so an unparseable stamp fails here instead of quietly expiring
                # the entry and re-arming the waste.
                self.assertTrue(
                    ms.on_hard_cooldown(doc, model, 0.0),
                    "%r carries no readable `until`: %r" % (model, row))

    def test_an_expired_committed_entry_stops_vetoing(self):
        """A cooldown must decay, not become a permanent denylist."""
        doc = ms.load_cooldown(str(self.PATH))
        for model in list((doc.get("models") or {})):
            self.assertFalse(
                ms.on_hard_cooldown(doc, model, 4_102_444_800.0),  # 2100-01-01
                "%r would still be vetoed in 2100 -- that is a blacklist, and a "
                "blacklist never re-tests an id that came back" % (model,))


class PersistedCooldownStepTests(unittest.TestCase):
    """The cooldown has to outlive the FAILURE that created it.

    WHY THIS EXISTS. Run 40658585532 recorded the shared-pool refusal correctly
    and then lost it: the only step that committed state was `Commit state
    changes`, which carries no `always()`, so the run that recorded a cooldown --
    usually the run that died on that cooldown -- skipped it. origin/master never
    saw the `pool` row, and every other workflow went on burning the bucket. Run
    40539483837 lost three `model_not_found` vetoes the same way.

    So the publishing step must (a) run on `always()`, (b) be placed AFTER the
    walk that writes the file, (c) stage that one path by explicit name, and
    (d) never commit a file it could not read back. The last two are EXECUTED
    against a real git repository, because both are easy to satisfy in a comment
    and get wrong in a shell.
    """

    WORKFLOWS = ("daily-news.yml", "breaking-monitor.yml")
    STEP = "Persist model cooldowns"
    STATE = "state/model_cooldown.json"

    # -- helpers -----------------------------------------------------------

    def _step(self, wf):
        for job, name, run, st in W.iter_run_blocks(wf):
            if name == self.STEP:
                return job, run, st
        self.fail(
            "%s has no `run:` step named %r. The walk that records a cooldown is "
            "usually the walk that fails, and a persisting step without "
            "`if: always()` never runs on the runs that need it."
            % (wf, self.STEP))

    def _ordered_steps(self, wf):
        doc = W.load_workflow(wf)
        out = []
        for job, steps in ((doc.get("jobs") or {}).items()):
            if not isinstance(steps, dict):
                continue
            for step in (steps.get("steps") or []):
                if isinstance(step, dict):
                    out.append(str(step.get("name") or ""))
        return out

    def _must(self, cwd, *args):
        env = dict(os.environ)
        env.update({"GIT_AUTHOR_NAME": "cooldown",
                    "GIT_AUTHOR_EMAIL": "cooldown@example.invalid",
                    "GIT_COMMITTER_NAME": "cooldown",
                    "GIT_COMMITTER_EMAIL": "cooldown@example.invalid"})
        proc = subprocess.run(
            ["git", "-c", "init.defaultBranch=master"] + list(args),
            cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=120)
        if proc.returncode != 0:
            self.fail("git %s failed in %s (exit %d):\n%s\n%s"
                      % (" ".join(args), cwd, proc.returncode,
                         proc.stdout, proc.stderr))
        return proc

    def _repo(self, name):
        """A throwaway clone of a bare origin, seeded with a cooldown file."""
        origin = self.tmp / (name + "-origin.git")
        work = self.tmp / (name + "-work")
        self._must(self.tmp, "init", "-q", "--bare", str(origin))
        self._must(self.tmp, "clone", "-q", str(origin), str(work))
        self._must(work, "config", "user.name", "cooldown")
        self._must(work, "config", "user.email", "cooldown@example.invalid")
        self._must(work, "config", "commit.gpgsign", "false")
        (work / "state").mkdir()
        self._write(work, {"schema_version": 1, "models": {}})
        (work / "notes.txt").write_text("seed\n", encoding="utf-8")
        self._must(work, "add", self.STATE, "notes.txt")
        self._must(work, "commit", "-q", "-m", "seed")
        self._must(work, "push", "-q", "origin", "master")
        return origin, work

    def _write(self, work, doc):
        path = work / self.STATE
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(doc, str):
                fh.write(doc)
            else:
                json.dump(doc, fh, indent=2)
                fh.write("\n")

    def _remote(self, origin):
        """The cooldown as origin/master holds it, or None if it is not there."""
        proc = self._must(origin, "show", "master:%s" % self.STATE)
        return json.loads(proc.stdout)

    def _run(self, wf, work):
        _job, body, _st = self._step(wf)
        env = {
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "STUB_REAL_PYTHON": sys.executable,
            # EMPTY, not absent: the step must take its unauthenticated branch
            # (a local-path origin needs no credential) and say so, rather than
            # failing on an unset variable.
            "GITHUB_TOKEN": "",
        }
        return W.run_bash_script(body, env_overrides=env, cwd=str(work),
                                 bash=self.bash, timeout=120)

    def setUp(self):
        self.bash = W.bash_executable()
        if self.bash is None:
            self.fail(
                "bash was not found on PATH, so the cooldown-persisting step "
                "CANNOT be executed. This fails instead of skipping on purpose: "
                "a skipped test here reports green while the one path that saves "
                "a cooldown has still never run.")
        self.tmp = Path(tempfile.mkdtemp(prefix="cooldown-step-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.bin_dir = self.tmp / "bin"
        W.install_stub_on_path(self.bin_dir, _PY3_SHIM, name="python3")

    # -- the step, statically ----------------------------------------------

    def test_the_persisting_step_runs_after_a_failed_walk(self):
        for wf in self.WORKFLOWS:
            with self.subTest(workflow=wf):
                _job, run, st = self._step(wf)
                self.assertIn(
                    "always()", str(st.get("if") or ""),
                    "%s / %r must carry `if: always()`: without it the step is "
                    "skipped exactly when a failed walk recorded a cooldown"
                    % (wf, self.STEP))
                self.assertEqual(
                    "true", str(st.get("continue-on-error")).lower(),
                    "an `always()` step that can fail a finished job is Bug B's "
                    "shape (Check 7). Losing the file is a warning, not a red run")
                self.assertIn(
                    "secrets.GITHUB_TOKEN",
                    str((st.get("env") or {}).get("GITHUB_TOKEN") or ""),
                    "the checkout persists no credentials, so this step needs "
                    "the token in its own env: block")
                self.assertIn("auth=(", run,
                              "the step must build the `git -c` auth argument")

    def test_the_persisting_step_comes_after_the_walk_that_writes_it(self):
        for wf in self.WORKFLOWS:
            with self.subTest(workflow=wf):
                names = self._ordered_steps(wf)
                self.assertIn(
                    self.STEP, names,
                    "%s has no step named %r" % (wf, self.STEP))
                persist = names.index(self.STEP)
                writers = [i for i, name in enumerate(names)
                           if name in ("Run OpenCode agent",
                                       "Run breaking news monitor")]
                self.assertTrue(
                    writers,
                    "%s: no walk step found, so this ordering check is blind" % wf)
                self.assertLess(
                    max(writers), persist,
                    "%s: %r is before the walk that writes the cooldown, so it "
                    "would commit the previous run's file"
                    % (wf, self.STEP))

    def test_staging_is_by_explicit_name_only(self):
        for wf in self.WORKFLOWS:
            with self.subTest(workflow=wf):
                _job, run, _st = self._step(wf)
                # Commands only: a comment that NAMES the forbidden form (this
                # one warns against it) is not a command.
                commands = "\n".join(
                    line for line in run.splitlines()
                    if line.strip() and not line.strip().startswith("#"))
                adds = [line.strip() for line in commands.splitlines()
                        if re.match(r"git add\b", line.strip())]
                self.assertEqual(
                    1, len(adds),
                    "expected exactly one `git add`, found %r. Staging is by "
                    "explicit name or it is a sweep." % (adds,))
                self.assertEqual(
                    'git add -- "$MODEL_STATE"', adds[0],
                    "the single staging command must name the cooldown path")
                self.assertIn(
                    "MODEL_STATE=%s" % self.STATE, commands,
                    "the step must name the one tracked model-state file")
                for forbidden in ("git add -A", "git add --all", "git add -u",
                                  "git add .", "git add state/",
                                  "git add state ", "git commit -a",
                                  "git commit -am"):
                    self.assertNotIn(
                        forbidden, commands,
                        "%s: %r would sweep in whatever the workspace holds "
                        "(including state/email.html), not just the cooldown"
                        % (wf, forbidden))

    # -- the step, executed ------------------------------------------------

    def test_a_cooldown_written_by_a_failed_run_is_still_committed(self):
        for wf in self.WORKFLOWS:
            with self.subTest(workflow=wf):
                origin, work = self._repo(wf.replace(".yml", ""))
                # The shape run 40658585532 recorded: a live shared-pool wall.
                self._write(work, {
                    "schema_version": 1,
                    "models": {},
                    "pool": {"until": "2026-10-28T08:56:56+00:00",
                             "reason": "quota_or_rate_limited",
                             "last_hit": "2026-10-28T08:41:56+00:00"},
                })
                proc = self._run(wf, work)
                self.assertEqual(
                    0, proc.returncode,
                    "%s: the persisting step exited %d\n%s\n%s"
                    % (wf, proc.returncode, proc.stdout, proc.stderr))
                pushed = self._remote(origin)
                self.assertIn(
                    "pool", pushed,
                    "%s: the pool row never reached origin/master -- this is "
                    "the defect the step exists to remove" % wf)
                self.assertEqual(
                    "quota_or_rate_limited", pushed["pool"].get("reason"))
                tip = self._must(origin, "log", "-1", "--format=%s", "master")
                self.assertIn("persist model cooldown state", tip.stdout,
                              "%s: the cooldown was not pushed as its own "
                              "commit (tip: %r)" % (wf, tip.stdout.strip()))

    def test_a_corrupt_cooldown_is_never_committed(self):
        for wf in self.WORKFLOWS:
            with self.subTest(workflow=wf):
                origin, work = self._repo(wf.replace(".yml", "") + "-corrupt")
                before = self._must(origin, "rev-parse", "master").stdout.strip()
                self._write(work, '{"models": ')
                proc = self._run(wf, work)
                self.assertEqual(
                    0, proc.returncode,
                    "%s: a corrupt file must not red the run\n%s\n%s"
                    % (wf, proc.stdout, proc.stderr))
                self.assertIn(
                    "did not read back as a cooldown document", proc.stdout,
                    "%s: the refusal must be LOUD, not a silent skip" % wf)
                after = self._must(origin, "rev-parse", "master").stdout.strip()
                self.assertEqual(
                    before, after,
                    "%s: a corrupt file was committed over a good one, which "
                    "un-arms every veto it held" % wf)

    def test_a_lost_rebase_race_logs_and_does_not_red_the_step(self):
        for wf in self.WORKFLOWS:
            with self.subTest(workflow=wf):
                origin, work = self._repo(wf.replace(".yml", "") + "-race")
                other = self.tmp / (wf.replace(".yml", "") + "-other")
                self._must(self.tmp, "clone", "-q", str(origin), str(other))
                self._must(other, "config", "user.name", "cooldown")
                self._must(other, "config", "user.email",
                           "cooldown@example.invalid")
                (other / "concurrent.txt").write_text("other writer\n",
                                                     encoding="utf-8")
                self._must(other, "add", "concurrent.txt")
                self._must(other, "commit", "-q", "-m", "concurrent push")
                self._must(other, "push", "-q", "origin", "master")
                seed = self._remote(origin)

                self._write(work, {
                    "schema_version": 1, "models": {},
                    "pool": {"until": "2026-10-28T08:56:56+00:00",
                             "reason": "quota_or_rate_limited"},
                })
                # A dirty tracked file is what makes the rebase itself refuse;
                # the remote commit above is the race it is refusing over.
                with open(work / "notes.txt", "a", encoding="utf-8") as fh:
                    fh.write("touched by an earlier step\n")

                proc = self._run(wf, work)
                self.assertEqual(
                    0, proc.returncode,
                    "%s: losing a race must be neutral, not a red run (exit %d)"
                    "\n%s\n%s" % (wf, proc.returncode, proc.stdout, proc.stderr))
                self.assertIn(
                    "LOST THE COOLDOWN REBASE RACE", proc.stdout,
                    "%s: the loss must be VISIBLE in the log; a silent loss is "
                    "the defect this step exists to remove" % wf)
                self.assertEqual(
                    seed, self._remote(origin),
                    "%s: the refused cooldown must not appear on origin/master "
                    "by accident" % wf)


class SharedPoolTests(unittest.TestCase):
    """The refusal is about THE BUCKET, not the model.

    WHY THIS EXISTS. Measured on the live provider: six different ids were refused
    inside 16 seconds, 312 ms after the request was accepted and with
    `tokens.input=0` -- the refusal happens BEFORE generation. So the per-model
    cooldown above cannot help: it would let the walk spend the next five rungs
    (60 s each) relearning one fact about the pool. On 2026-10-27 that fact cost
    33 of the week's 47 quota refusals.

    The three things this suite pins:

      1. ONE ROW. A refusal is recorded once, as a `pool` row, with an absolute
         `until`; it is never recorded per model.
      2. YIELD. Every model is withdrawn while the pool row is live, and the
         existing empty-selection path is used -- the caller is told, not failed.
      3. THE DIGEST RESERVATION. `role="digest"` may ignore a wall it has watched
         for longer than the grace, because an edition that is never written is a
         worse outcome than one re-test; `role="other"` may not, and yields for
         the rest of an open slot window.
    """

    UTC = timezone.utc
    SLOT = datetime(2026, 10, 28, 1, 35, tzinfo=UTC).timestamp()

    def test_a_refusal_is_one_absolute_pool_row(self):
        doc = {}
        row = ms.record_pool_refusal(doc, at=1000.0, retry_after=300)
        self.assertEqual(["pool"], list(doc))
        self.assertEqual("quota_or_rate_limited", row["reason"])
        self.assertEqual(300, row["seconds"])
        self.assertEqual("1970-01-01T00:21:40+00:00", row["until"],
                         "until must be absolute, so expiry does not depend on "
                         "when the reader runs")

    def test_the_providers_own_retry_after_wins(self):
        doc = {}
        ms.record_pool_refusal(doc, at=1000.0, retry_after=1200)
        self.assertEqual(1200, doc["pool"]["seconds"],
                         "Retry-After is the observed reset; a default is only a "
                         "fallback")

    def test_an_absent_retry_after_falls_back_and_a_huge_one_is_clamped(self):
        doc = {}
        ms.record_pool_refusal(doc, at=1000.0, retry_after=None)
        self.assertEqual(ms.POOL_DEFAULT_SEC, doc["pool"]["seconds"])
        big = {}
        ms.record_pool_refusal(big, at=1000.0, retry_after=10 ** 9)
        self.assertEqual(ms.POOL_MAX_SEC, big["pool"]["seconds"],
                         "an absurd header must not become a permanent wall")

    def test_a_second_refusal_never_shortens_the_wall(self):
        doc = {}
        ms.record_pool_refusal(doc, at=1000.0, retry_after=600)
        ms.record_pool_refusal(doc, at=1010.0, retry_after=60)
        self.assertEqual(600, doc["pool"]["seconds"],
                         "a later, shorter report must not re-arm the pool early")

    def test_a_live_pool_row_withdraws_every_model(self):
        doc = {}
        ms.record_pool_refusal(doc, at=1000.0, retry_after=300)
        models, info = _select([DEAD, LIVE], doc, now=1100.0)
        self.assertEqual([], models)
        self.assertTrue(info["pool_blocked"])
        self.assertIn("until", info["pool"]["note"],
                      "the suppression must name its expiry, or a wall with no "
                      "date is indistinguishable from a broken workflow")

    def test_an_expired_pool_row_withdraws_nothing(self):
        doc = {}
        ms.record_pool_refusal(doc, at=1000.0, retry_after=300)
        models, info = _select([LIVE], doc, now=1400.0)
        self.assertEqual([LIVE], models)
        self.assertFalse(info.get("pool_blocked"))
        self.assertFalse(doc.get("pool"), "expiry must prune the row")

    def test_the_digest_may_ignore_a_wall_it_has_watched_past_the_grace(self):
        doc = {}
        ms.record_pool_refusal(doc, at=self.SLOT - 700, retry_after=7200)
        fresh = ms.pool_block(doc, now=self.SLOT - 640, role="digest")
        self.assertTrue(fresh["blocked"],
                        "inside the grace the digest must still honour the wall")
        late = ms.pool_block(doc, now=self.SLOT, role="digest")
        self.assertFalse(late["blocked"],
                         "after the grace the digest attempts anyway -- losing "
                         "the edition is the worse failure")
        self.assertIn("Attempting anyway", late["note"])

    def test_a_non_digest_caller_yields_to_the_end_of_an_open_digest_window(self):
        """The reservation's only job: do not let another walk eat a pool that
        has just come back, while the digest that needs it is about to run.

        A wall that expires INSIDE an open slot window is held to the window's
        end. It is deliberately NOT a blanket hour-long embargo around every
        slot: with a healthy pool nobody is being refused, and a quiet breaking
        monitor for two hours a day would be a real cost paid for nothing.
        """
        doc = {}
        # Refused 100s before the slot with 300s to wait: the wall dies at
        # SLOT+200, which is inside the window ending at SLOT+3600.
        ms.record_pool_refusal(doc, at=self.SLOT - 100, retry_after=300)
        out = ms.pool_block(doc, now=self.SLOT, role="other")
        self.assertTrue(out["blocked"])
        self.assertTrue(out["digest_window"])
        self.assertEqual(
            datetime.fromtimestamp(self.SLOT + ms.DIGEST_WINDOW_TRAIL_SEC,
                                   timezone.utc).isoformat(),
            out["until"],
            "the yield must last exactly as long as the window it protects")

    def test_a_wall_that_outlives_the_window_is_not_shortened(self):
        doc = {}
        ms.record_pool_refusal(doc, at=self.SLOT, retry_after=7200)
        out = ms.pool_block(doc, now=self.SLOT, role="other")
        self.assertEqual(
            datetime.fromtimestamp(self.SLOT + 7200, timezone.utc).isoformat(),
            out["until"], "the provider's reset outranks the window")

    def test_no_window_means_no_extension(self):
        doc = {}
        ms.record_pool_refusal(doc, at=1000.0, retry_after=300)
        out = ms.pool_block(doc, now=1100.0, role="other")
        self.assertFalse(out["digest_window"])
        self.assertEqual(
            datetime.fromtimestamp(1300.0, timezone.utc).isoformat(),
            out["until"])

    def test_the_selector_reports_the_reservation_it_is_honouring(self):
        doc = {}
        ms.record_pool_refusal(doc, at=self.SLOT - 100, retry_after=300)
        models, info = _select([LIVE], doc, now=self.SLOT)
        self.assertEqual([], models)
        self.assertTrue(info["pool"]["digest_window"])

    def test_the_window_is_closed_outside_the_two_slots(self):
        self.assertIsNotNone(ms.digest_window(now=self.SLOT))
        self.assertIsNone(ms.digest_window(now=self.SLOT + 3 * 3600),
                          "the reservation must not wall the pool all day")

    def test_the_slot_table_comes_from_the_deployment_schedule(self):
        """The window is derived, not typed twice.

        `deploy/schedule.json` already states the digest slots; a second copy in
        Python would drift the moment the schedule moved, and the drift would be
        invisible until a digest slot silently stopped being protected.
        """
        self.assertEqual(((1, 35), (13, 35)), ms.digest_slots())


class PoolVocabularyTests(unittest.TestCase):
    def test_the_pre_generation_refusal_is_what_earns_a_pool_row(self):
        refusal = ("AI_APICallError: Rate limit exceeded. Retry-After: 1200\n"
                   "message=stream tokens.input=0")
        self.assertEqual("quota_or_rate_limited",
                         ms.classify_failure(refusal, 1))
        self.assertEqual(1200, ms.retry_after_from_text(refusal))

    def test_a_not_found_id_is_not_a_pool_refusal(self):
        self.assertEqual(
            "model_not_found",
            ms.classify_failure(
                "ProviderModelNotFoundError: Model not found: %s" % DEAD, 1))
        self.assertIsNone(
            ms.retry_after_from_text("ProviderModelNotFoundError: Model not "
                                     "found: %s" % DEAD))


class PoolCliTests(unittest.TestCase):
    """The shell walks speak to this through the CLI, so the CLI is where a
    suppression has to be legible. A silent empty ladder is the failure mode:
    `mapfile` discards the exit code, so a caller that exits non-zero on a
    suppression turns a normal "wait" into a red workflow."""

    def _run(self, argv, discover):
        saved = ms.discover
        ms.discover = discover
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ms._main(argv)
        finally:
            ms.discover = saved
        return code, out.getvalue(), err.getvalue()

    def test_pool_refused_writes_a_row_that_the_next_select_honours(self):
        with tempfile.TemporaryDirectory(prefix="cool-pool-") as sd:
            cfg = os.path.join(sd, "opencode.json")
            health = os.path.join(sd, "health.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"provider": PROVIDER, "ladder": [LIVE]}, fh)

            code, _, err = self._run(
                ["--pool-refused", "--retry-after", "1200", "--health", health],
                lambda _p, cache_path=None: ([LIVE], {"source": "live"}))
            self.assertEqual(0, code)
            self.assertIn("until", err, "the recorded wall must name its expiry")

            doc = ms.load_cooldown(ms.cooldown_path_for(health))
            self.assertEqual(1200, doc["pool"]["seconds"])
            self.assertEqual({}, doc.get("models") or {},
                             "a pool refusal must not cool a model")

            code, out, err = self._run(
                ["--select", "--all", "--config", cfg, "--health", health],
                lambda _p, cache_path=None: ([LIVE], {"source": "live"}))
            self.assertEqual(0, code,
                             "a suppression is not a failure: the caller's job "
                             "is to wait, not to go red")
            self.assertEqual("", out)
            self.assertIn("pool cooldown", err)

    def test_pool_refused_reads_the_retry_after_out_of_the_attempt_log(self):
        with tempfile.TemporaryDirectory(prefix="cool-pool-log-") as sd:
            health = os.path.join(sd, "health.json")
            log = os.path.join(sd, "attempt.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("AI_APICallError: Rate limit exceeded\n"
                         "Retry-After: 1800\nmessage=stream tokens.input=0\n")
            code, _, _ = self._run(
                ["--pool-refused", "--log", log, "--health", health],
                lambda _p, cache_path=None: ([LIVE], {"source": "live"}))
            self.assertEqual(0, code)
            doc = ms.load_cooldown(ms.cooldown_path_for(health))
            self.assertEqual(1800, doc["pool"]["seconds"],
                             "the provider's own reset must win over the default")

    def test_role_digest_reaches_the_selector(self):
        with tempfile.TemporaryDirectory(prefix="cool-pool-role-") as sd:
            cfg = os.path.join(sd, "opencode.json")
            health = os.path.join(sd, "health.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"provider": PROVIDER, "ladder": [LIVE]}, fh)
            doc = ms.load_cooldown(ms.cooldown_path_for(health))
            ms.record_pool_refusal(doc, at=time.time() - 700, retry_after=7200)
            ms.save_cooldown(ms.cooldown_path_for(health), doc)

            code, out, err = self._run(
                ["--select", "--all", "--role", "other", "--config", cfg,
                 "--health", health],
                lambda _p, cache_path=None: ([LIVE], {"source": "live"}))
            self.assertEqual(0, code)
            self.assertEqual("", out)

            code, out, err = self._run(
                ["--select", "--all", "--role", "digest", "--config", cfg,
                 "--health", health],
                lambda _p, cache_path=None: ([LIVE], {"source": "live"}))
            self.assertEqual(0, code)
            self.assertEqual([LIVE], out.split(),
                             "the digest attempts anyway past the grace")
            self.assertIn("WILL NOT YIELD", err)


class WorkflowWiringTests(unittest.TestCase):
    """The digest walk is the caller the reservation exists for.

    `pool_block` and `--role digest` are covered above, but the SAVING lives in
    the workflow: a later edit that drops the role, or that stops diagnosing the
    blocked pool, would leave every unit test green while the digest went back to
    honouring a stale wall and losing the edition.
    """

    def _resolve_ladder_body(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        path = os.path.join(repo, ".github", "workflows", "daily-news.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        parts = text.split("resolve_ladder() {", 1)
        self.assertEqual(2, len(parts), "resolve_ladder has been renamed")
        return parts[1].split("tried=0", 1)[0]

    def test_the_digest_walk_resolves_its_ladder_as_the_digest_role(self):
        body = self._resolve_ladder_body()
        self.assertIn("--role digest", body,
                      "without the role the digest honours a wall younger than "
                      "its grace, and a stale refusal loses the edition")

    def test_a_blocked_pool_is_diagnosed_rather_than_reported_as_empty(self):
        body = self._resolve_ladder_body()
        self.assertIn("pool_blocked", body,
                      "an empty ladder because the pool is refused must say so; "
                      "reporting it as an empty ladder sends the reader after "
                      "broken model ids")


if __name__ == "__main__":
    unittest.main(verbosity=2)
