#!/usr/bin/env python3
"""tests/academic/test_worker_kv_writes.py — academic sweep WRITE-BUDGET guard.

`deploy/cloudflare-worker.js` runs its academic sweep on a `*/2` cron, 720
times a day, and used to write BOTH state keys (`academic:last_examined_notice_id`
and `academic:examined`) unconditionally on every single run. That is ~1,440
writes/day against Cloudflare's FREE-tier limit of 1,000 writes/day, so the
account exhausted its write quota daily, Cloudflare started REJECTING writes,
and the floor could never advance again.

This file pins the fix: the writes are now conditional (read-compare-write
against the values the same run just read from KV). A sweep that changes
nothing must perform ZERO writes; a sweep that changes something must perform
exactly the writes it needs — no more, and never so few that a notice is lost.

Pattern mirrors the proven tests/academic/test_worker_parity.py approach: the
Worker's real source is sliced up to the entry points, a harness is appended,
and the REAL handleAcademic is executed under node against a fake KV and a fake
fetch. Nothing here is a Python re-implementation of the Worker's logic — a
proxy is exactly what let a real divergence survive a "parity" test before.

Run:
    py -m unittest discover -s tests/academic -t . -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
WORKER = os.path.join(REPO_ROOT, "deploy", "cloudflare-worker.js")

FLOOR_KEY = "academic:last_examined_notice_id"
EXAMINED_KEY = "academic:examined"
DISPATCHED_PREFIX = "academic:dispatched:"

# Slice marker: everything above this is the sweep implementation, everything
# below it is the Workers entry point object (which uses `export default` and so
# cannot be run as a plain node script). Same marker the parity test uses.
ENTRY_POINTS_MARKER = "// --- Workers entry points"

# The harness. It is appended to the Worker's own source and executed with node,
# so it calls the REAL handleAcademic. It stubs only the two things a Worker
# cannot have in a test: the network (fetch) and Cloudflare's KV.
RUNNER = r"""
const CASE = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));

// ---- fake KV: counts every get/put and can be told to fail --------------
const puts = [];
const gets = [];
const failPut = CASE.failPut || [];
const failGet = CASE.failGet || [];
const store = Object.assign({}, CASE.store || {});
const KV = {
  async get(key) {
    gets.push(key);
    if (failGet.indexOf(key) >= 0) {
      throw new Error("injected KV read failure on " + key);
    }
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  async put(key, value) {
    if (failPut.indexOf(key) >= 0) {
      throw new Error("injected KV write failure on " + key);
    }
    puts.push({ key: key, value: value });
    store[key] = value;
  },
};

// ---- fake feed ----------------------------------------------------------
// Shaped exactly like the live IOST notice list: anchors whose href ends in
// the numeric notice id, title taken from the anchor's inner text.
const cards = (CASE.notices || []).map(function (n) {
  return '<a href="/notices/' + n.id + '">' + n.title + "</a>";
});
const html = "<html><body><ul><li>" + cards.join("</li><li>") +
  "</li></ul></body></html>";

const dispatches = [];
globalThis.fetch = async function (url) {
  const u = String(url);
  if (u.indexOf(ACADEMIC_FEED_URL) === 0) {
    return { ok: true, status: 200, text: async function () { return html; } };
  }
  dispatches.push(u);
  const ok = CASE.dispatchOk !== false;
  return {
    ok: ok,
    status: ok ? 204 : 500,
    text: async function () { return ""; },
  };
};

const env = { KV: KV, GH_TOKEN: "test-token" };

(async function () {
  let summary = null;
  let error = null;
  try {
    summary = await handleAcademic(env);
  } catch (e) {
    error = String(e && e.stack ? e.stack : e);
  }
  // Written to a FILE, not stdout: the Worker logs its own progress to stdout
  // via console.log, so stdout is not a clean channel for a JSON result.
  require("fs").writeFileSync(process.argv[3], JSON.stringify({
    summary: summary,
    error: error,
    puts: puts,
    gets: gets,
    store: store,
    dispatches: dispatches,
  }));
})();
"""


def worker_body():
    with open(WORKER, encoding="utf-8") as fh:
        src = fh.read()
    cut = src.find(ENTRY_POINTS_MARKER)
    if cut < 0:
        raise AssertionError(
            "entry-points marker %r not found in %s — this harness slices the "
            "Worker's real source and cannot be built without it"
            % (ENTRY_POINTS_MARKER, WORKER))
    return src[:cut]


def _node():
    node = shutil.which("node")
    if not node:
        # LOUD, NOT SILENT — same rule as test_worker_parity.py. A write-budget
        # test that skips and reports green is worse than no test at all.
        raise AssertionError(
            "node is required by tests/academic/test_worker_kv_writes.py to "
            "execute the Worker's handleAcademic. It is missing from this "
            "machine, so the write budget is UNVERIFIED. Failing loudly rather "
            "than skipping.")
    return node


def run_sweep(**case):
    """Execute the Worker's REAL handleAcademic against a fake KV + fake feed.

    Each call is a SEPARATE node process, so every case is a COLD ISOLATE: no
    module-level state survives between calls. That is what makes these cases a
    genuine test of cold-start correctness rather than of a warm cache.
    """
    case.setdefault("store", {})
    case.setdefault("notices", [])
    tmpdir = tempfile.mkdtemp(prefix="acad_kv_")
    try:
        script = os.path.join(tmpdir, "harness.js")
        case_path = os.path.join(tmpdir, "case.json")
        out_path = os.path.join(tmpdir, "out.json")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(worker_body() + "\n" + RUNNER)
        with open(case_path, "w", encoding="utf-8") as fh:
            json.dump(case, fh)
        proc = subprocess.run([_node(), script, case_path, out_path],
                              capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            raise AssertionError(
                "node failed to run handleAcademic (exit %s):\n%s"
                % (proc.returncode, proc.stderr[:3000]))
        if not os.path.exists(out_path):
            raise AssertionError(
                "handleAcademic produced no result file. stderr:\n%s\nstdout:\n%s"
                % (proc.stderr[:3000], proc.stdout[:3000]))
        with open(out_path, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def put_keys(result):
    return [p["key"] for p in result["puts"]]


def put_value(result, key):
    for p in result["puts"]:
        if p["key"] == key:
            return p["value"]
    return None


# A real sem-1 CSIT result title (the owner's roll is watching semesters 1
# and 4), and a real non-result notice, so the cases read like production.
RESULT_TITLE = "B.Sc.CSIT I Semester-2082 Exam Result"
IRRELEVANT_TITLE = "Library extended opening hours"
BACK_RESULT_TITLE = "B.Sc.CSIT I Semester Back Result"


class NoChangeSweepIsFreeTest(unittest.TestCase):
    """The core budget guarantee: a quiet sweep writes NOTHING."""

    def _settled_store(self):
        # Floor already covers every listed notice and every id carries a
        # decided outcome, so this run has no work and no state change at all.
        #
        # Written with Python's json.dumps, whose separators are ", " / ": "
        # rather than JSON.stringify's "," / ":". That is DELIBERATE: it proves
        # the Worker's read-compare is SEMANTIC. A byte-wise comparison would
        # call this an identical-value rewrite and burn a write on every single
        # sweep — reintroducing exactly the bug this file guards.
        return {
            FLOOR_KEY: "3",
            EXAMINED_KEY: json.dumps({
                "1": {"outcome": "irrelevant", "reason": "no_semester_match"},
                "2": {"outcome": "irrelevant", "reason": "no_semester_match"},
                "3": {"outcome": "irrelevant", "reason": "no_semester_match"},
            }),
        }

    def test_sweep_with_nothing_to_do_performs_zero_writes(self):
        res = run_sweep(
            store=self._settled_store(),
            notices=[
                {"id": 1, "title": IRRELEVANT_TITLE},
                {"id": 2, "title": IRRELEVANT_TITLE},
                {"id": 3, "title": IRRELEVANT_TITLE},
            ],
        )
        self.assertIsNone(res["error"], "handleAcademic threw:\n%s" % res["error"])
        self.assertTrue(res["summary"]["ok"], res["summary"])
        self.assertEqual(
            res["puts"], [],
            "an unchanged sweep performed %d KV write(s) %r — that is the "
            "720x/day burn this test exists to prevent"
            % (len(res["puts"]), put_keys(res)))
        self.assertEqual(res["summary"]["kvWrites"], 0)
        self.assertEqual(res["summary"]["kvWritesSkipped"], 2,
                         "both state keys should have been recognised as "
                         "unchanged and skipped")
        self.assertEqual(res["summary"]["floor"], 3,
                         "an unchanged sweep must still report the true floor")

    def test_it_still_reads_both_keys_so_the_comparison_is_sound(self):
        """A skip is only sound if the value was actually compared. Assert the
        reads happened, so a future 'optimisation' that skips the read (and
        with it the comparison) fails here instead of silently burning."""
        res = run_sweep(
            store=self._settled_store(),
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     {"id": 2, "title": IRRELEVANT_TITLE},
                     {"id": 3, "title": IRRELEVANT_TITLE}],
        )
        self.assertIn(FLOOR_KEY, res["gets"])
        self.assertIn(EXAMINED_KEY, res["gets"])


class ChangedSweepWritesExactlyWhatIsNeededTest(unittest.TestCase):
    def test_first_sweep_over_three_notices_writes_both_keys_once(self):
        res = run_sweep(
            store={},
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     {"id": 2, "title": IRRELEVANT_TITLE},
                     {"id": 3, "title": IRRELEVANT_TITLE}],
        )
        self.assertIsNone(res["error"], res["error"])
        self.assertTrue(res["summary"]["ok"], res["summary"])
        self.assertEqual(put_keys(res), [EXAMINED_KEY, FLOOR_KEY],
                         "a first sweep must write the examined map and the "
                         "floor exactly once each")
        self.assertEqual(put_value(res, FLOOR_KEY), "3")
        examined = json.loads(put_value(res, EXAMINED_KEY))
        self.assertEqual(sorted(examined), ["1", "2", "3"])
        self.assertEqual(res["summary"]["kvWrites"], 2)
        self.assertEqual(res["summary"]["kvWritesSkipped"], 0)

    def test_a_deferred_notice_writes_the_map_but_stops_the_floor(self):
        """A sweep that records a decision but is blocked by a deferred notice
        must commit the decision (or it is lost) while leaving the floor short
        of the undecidable id.

        Note what this pins about the write budget: because the R2 clamp caps
        the floor at the last id actually ATTEMPTED, and a floor advance always
        implies at least one attempt, the floor and the examined map move
        together. A changed sweep therefore costs 2 writes, never more.
        """
        res = run_sweep(
            store={FLOOR_KEY: "0"},
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     # empty anchor text -> titleDegraded -> deferred, loop stops
                     {"id": 2, "title": ""}],
        )
        self.assertIsNone(res["error"], res["error"])
        self.assertTrue(res["summary"]["ok"], res["summary"])
        self.assertTrue(res["summary"]["stoppedEarly"])
        self.assertEqual(res["summary"]["floor"], 1,
                         "the floor must stop before the deferred id 2")
        self.assertEqual(put_keys(res), [EXAMINED_KEY, FLOOR_KEY])
        self.assertEqual(res["summary"]["kvWrites"], 2)
        examined = json.loads(put_value(res, EXAMINED_KEY))
        self.assertEqual(examined["1"]["outcome"], "irrelevant")
        self.assertEqual(examined["2"]["outcome"], "deferred")

    def test_a_new_notice_after_a_quiet_sweep_is_still_examined(self):
        """THE NO-BURN GUARD for the optimisation itself.

        A quiet sweep skips its writes. The very next sweep sees a brand-new
        result notice. It MUST be examined, dispatched and folded into the
        floor — a skipped write must never become a skipped notice.
        """
        settled = {
            FLOOR_KEY: "3",
            EXAMINED_KEY: json.dumps({
                "1": {"outcome": "irrelevant", "reason": "no_semester_match"},
                "2": {"outcome": "irrelevant", "reason": "no_semester_match"},
                "3": {"outcome": "irrelevant", "reason": "no_semester_match"},
            }),
        }
        # Sweep 1: nothing new -> free.
        quiet = run_sweep(
            store=dict(settled),
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     {"id": 2, "title": IRRELEVANT_TITLE},
                     {"id": 3, "title": IRRELEVANT_TITLE}],
        )
        self.assertEqual(quiet["puts"], [])

        # Sweep 2: the owner's semester-1 result lands, in a COLD isolate that
        # has never seen sweep 1.
        res = run_sweep(
            store=quiet["store"],
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     {"id": 2, "title": IRRELEVANT_TITLE},
                     {"id": 3, "title": IRRELEVANT_TITLE},
                     {"id": 4, "title": RESULT_TITLE}],
        )
        self.assertIsNone(res["error"], res["error"])
        self.assertEqual(res["summary"]["dispatched"], 1,
                         "the new CSIT result notice must be dispatched")
        self.assertEqual(res["summary"]["floor"], 4,
                         "the floor must advance onto the notice just examined")
        # Both state keys, plus the dedup marker for the dispatch. Three writes
        # for the one sweep in a fortnight that actually found something.
        # (The runtime quota, 2026-10-20, ALSO writes `budget:*` on this path --
        # the freeze-cache probe and the month charge. That is accounting, not
        # delivery state, and no delivery path reads it, so it is excluded here.)
        academic_keys = [k for k in put_keys(res) if not k.startswith("budget:")]
        self.assertEqual(set(academic_keys),
                         {FLOOR_KEY, EXAMINED_KEY, DISPATCHED_PREFIX + "4"})
        examined = json.loads(put_value(res, EXAMINED_KEY))
        self.assertEqual(examined["4"]["outcome"], "processed")
        self.assertIn(DISPATCHED_PREFIX + "4", res["store"],
                      "the dedup key for the dispatched notice must be written")

    def test_repeated_cold_sweeps_converge_to_zero_writes(self):
        """Cold-isolate convergence: run the same settled sweep from a fresh
        process twice. The first commits state, the second must be free. This
        is the shape of a real day — 720 identical sweeps, zero writes."""
        first = run_sweep(
            store={},
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     {"id": 2, "title": IRRELEVANT_TITLE}],
        )
        self.assertEqual(len(first["puts"]), 2)
        second = run_sweep(
            store=first["store"],
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     {"id": 2, "title": IRRELEVANT_TITLE}],
        )
        self.assertEqual(second["puts"], [],
                         "a settled sweep re-run in a COLD isolate still wrote "
                         "%r" % put_keys(second))
        self.assertEqual(second["summary"]["kvWritesSkipped"], 2)


class KvWriteFailureStillDefersTest(unittest.TestCase):
    """The fail-safe: a write error defers the run and must NOT move the floor."""

    def test_examined_write_failure_defers_and_leaves_the_floor_alone(self):
        res = run_sweep(
            store={FLOOR_KEY: "7"},
            notices=[{"id": 8, "title": IRRELEVANT_TITLE}],
            failPut=[EXAMINED_KEY],
        )
        self.assertFalse(res["summary"]["ok"], res["summary"])
        self.assertEqual(res["summary"]["reason"], "KV state write failed")
        self.assertNotIn(FLOOR_KEY, put_keys(res),
                         "the floor must not be written when the examined map "
                         "write failed — an advanced floor is the burn")
        self.assertEqual(res["store"][FLOOR_KEY], "7",
                         "the stored floor must be untouched after a write error")

    def test_floor_write_failure_leaves_the_stored_floor_untouched(self):
        res = run_sweep(
            store={FLOOR_KEY: "7"},
            notices=[{"id": 8, "title": IRRELEVANT_TITLE}],
            failPut=[FLOOR_KEY],
        )
        self.assertFalse(res["summary"]["ok"], res["summary"])
        self.assertEqual(res["summary"]["reason"], "KV state write failed")
        self.assertEqual(res["store"][FLOOR_KEY], "7",
                         "a failed floor write must not move the floor")
        # The examined map may have been committed first: that only ever ADDS
        # decided entries, which is safe (a decided id is not re-examined, and
        # it was genuinely decided). The floor is what must never jump.
        self.assertEqual(put_keys(res), [EXAMINED_KEY])

    def test_deferred_title_still_blocks_the_floor_and_still_commits(self):
        """No-burn invariant: an unreadable title is DEFERRED, the loop stops,
        and the floor must not pass it. The deferred record must be committed,
        or the retry would be lost."""
        res = run_sweep(
            store={FLOOR_KEY: "0"},
            notices=[{"id": 1, "title": IRRELEVANT_TITLE},
                     # empty anchor text -> titleDegraded -> deferred
                     {"id": 2, "title": ""},
                     {"id": 3, "title": RESULT_TITLE}],
        )
        self.assertIsNone(res["error"], res["error"])
        self.assertTrue(res["summary"]["ok"], res["summary"])
        self.assertTrue(res["summary"]["stoppedEarly"])
        self.assertEqual(res["summary"]["floor"], 1,
                         "the floor must stop before the deferred id")
        self.assertEqual(res["summary"]["dispatched"], 0,
                         "notice 3 is behind the deferred id 2 and must wait")
        examined = json.loads(put_value(res, EXAMINED_KEY))
        self.assertEqual(examined["2"]["outcome"], "deferred")
        self.assertNotIn("3", examined,
                         "notice 3 was never reached; recording it would burn it")


class StatusVisibilityTest(unittest.TestCase):
    """The status page must let the owner SEE the floor and the write skips."""

    def setUp(self):
        with open(WORKER, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_status_page_reports_the_academic_floor(self):
        self.assertIn("academic floor: ", self.src)
        self.assertIn("academicFloorForStatus", self.src)

    def test_status_page_reports_last_sweep_write_skips(self):
        self.assertIn("academic KV writes: ", self.src)
        self.assertIn("kvWritesSkipped", self.src)

    def test_the_sweep_reports_its_write_count_in_the_summary(self):
        self.assertIn("kvWrites: ledger.written", self.src)
        self.assertIn("kvWritesSkipped: ledger.skipped", self.src)

    def test_the_liveness_signal_is_never_load_bearing(self):
        """ACADEMIC_LAST_SWEEP exists for the status page only. handleAcademic
        may ASSIGN it, but it must never READ it: if the sweep ever consulted
        it, correctness would depend on isolate lifetime — exactly the failure
        mode the no-cache decision exists to avoid."""
        body = worker_body()
        sweep = body[body.index("async function handleAcademic"):]
        self.assertIn("ACADEMIC_LAST_SWEEP = summary;", sweep,
                      "the sweep should record its summary for the status page")
        self.assertEqual(
            sweep.count("ACADEMIC_LAST_SWEEP"), 1,
            "handleAcademic must reference ACADEMIC_LAST_SWEEP exactly once "
            "(the assignment). Any further reference is a READ, which would "
            "make correctness depend on isolate lifetime.")
        # And the status page is the only consumer.
        self.assertIn("ACADEMIC_LAST_SWEEP", self.src)


class SourceLevelGuardsTest(unittest.TestCase):
    """Cheap structural pins, mirroring KvKeyParityTest in the parity module."""

    def setUp(self):
        with open(WORKER, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_examined_map_is_still_written_from_exactly_one_call_site(self):
        import re
        puts = re.findall(r"await env\.KV\.put\(ACADEMIC_EXAMINED_KEY", self.src)
        self.assertEqual(len(puts), 1,
                         "the examined map must be committed from exactly one "
                         "place, so a run can never leave partial state")

    def test_the_floor_is_written_from_exactly_one_call_site(self):
        import re
        puts = re.findall(
            r"await env\.KV\.put\(ACADEMIC_LAST_EXAMINED_KEY", self.src)
        self.assertEqual(len(puts), 1,
                         "the floor must be committed from exactly one place")

    def test_the_examined_map_is_committed_before_the_floor(self):
        """Ordering is the fail-safe: if the map write fails the floor must
        never have moved. Assert it so the order cannot be swapped back."""
        self.assertLess(
            self.src.index("await env.KV.put(ACADEMIC_EXAMINED_KEY"),
            self.src.index("await env.KV.put(ACADEMIC_LAST_EXAMINED_KEY"),
            "the examined map must be written BEFORE the floor so a failure "
            "cannot leave an advanced floor behind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
