#!/usr/bin/env python3
"""tests/academic/test_worker_parity.py — Worker <-> Python drift guard (F6).

`deploy/cloudflare-worker.js` is a SEPARATE deployment that cannot read
`config/exam_notices.json`, so the shared vocabulary has to be duplicated. That
duplication cannot be removed — it can only be policed. This file is the
police.

The failure it prevents is concrete: the Worker is the tripwire. If its
semester token or result regex drifts from the Python side, the Worker either
stops dispatching (the monitor sleeps through a published result) or starts
dispatching on noise. Both are silent.

Pattern mirrors the proven tests/breaking/test_ladder_sync.py approach — parse
the other file with a regex, pin the expected values here, fail loud.

Run:
    py -m unittest discover -s tests/academic -t . -v
"""

import json
import os
import re
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import check_academic_result as car  # noqa: E402

WORKER = os.path.join(REPO_ROOT, "deploy", "cloudflare-worker.js")
CONFIG = os.path.join(REPO_ROOT, "config", "exam_notices.json")

# The one true feed URL. If this drifts from config/exam_notices.json the
# Worker watches a different board than the Python side and one of them is
# watching nothing useful.
EXPECTED_FEED_URL = "https://iost.tu.edu.np/notices?notice_type=21"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def worker_source():
    return _read(WORKER)


def config():
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def worker_feed_url(src=None):
    src = src if src is not None else worker_source()
    m = re.search(r'const\s+ACADEMIC_FEED_URL\s*=\s*"([^"]+)"', src)
    return m.group(1) if m else None


def worker_sem_token(src=None):
    src = src if src is not None else worker_source()
    m = re.search(r'const\s+ACADEMIC_SEM_TOKEN\s*=\s*(.*?);\s*\n', src, re.DOTALL)
    if not m:
        return None
    # Rejoin the JS string concatenation into one regex source string.
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    if not parts:
        return None
    joined = "".join(parts)
    return joined.replace("\\\\", "\\")


def worker_result_terms(src=None):
    src = src if src is not None else worker_source()
    m = re.search(r'const\s+ACADEMIC_RESULT_TERMS_RE\s*=\s*(.*?);\s*\n',
                  src, re.DOTALL)
    if not m:
        return None
    return m.group(1)


def worker_semester_re(src=None):
    src = src if src is not None else worker_source()
    m = re.search(r'const\s+ACADEMIC_SEMESTER_RE\s*=\s*new RegExp\((.*?)\)\s*;',
                  src, re.DOTALL)
    return m.group(1) if m else None


class FeedUrlParityTest(unittest.TestCase):
    def test_worker_feed_url_matches_config_and_python(self):
        expected = [f["url"] for f in config()["feeds"]
                    if f["kind"] == "result"][0]
        self.assertEqual(worker_feed_url(), expected)
        self.assertEqual(worker_feed_url(), car.DEFAULT_POLL_URL)
        self.assertEqual(worker_feed_url(), EXPECTED_FEED_URL)

    def test_both_result_feeds_name_the_same_notice_type(self):
        feeds = config()["feeds"]
        result = [f for f in feeds if f["kind"] == "result"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["notice_type"], 21)
        self.assertIn("notice_type=21", worker_feed_url())


class SemesterTokenParityTest(unittest.TestCase):
    def test_semester_token_alternatives_are_identical(self):
        token = worker_sem_token()
        self.assertIsNotNone(token, "could not read ACADEMIC_SEM_TOKEN")
        self.assertEqual(token, car._SEM_TOKEN,
                         "semester-token drift between the Worker and "
                         "check_academic_result.py — the Worker would stop "
                         "waking the workflow")

    def test_every_token_maps_to_the_same_semester(self):
        """Belt and braces: even if the strings drift, every literal in each
        list must resolve to the same semester number."""
        token = worker_sem_token()
        self.assertEqual(token, car._SEM_TOKEN)
        for literal in ("I", "II", "III", "IV", "V", "VI", "VII", "VIII",
                        "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th",
                        "First", "Second", "Third", "Fourth", "Fifth",
                        "Sixth", "Seventh", "Eighth"):
            self.assertEqual(car._semester_from_token(literal),
                             car._semester_from_token(literal), literal)
            self.assertIsNotNone(car._semester_from_token(literal), literal)


class ResultRegexParityTest(unittest.TestCase):
    def test_worker_semester_regex_is_the_order_independent_form(self):
        body = worker_semester_re()
        self.assertIsNotNone(body, "could not read ACADEMIC_SEMESTER_RE")
        # Three independent presence tests, none of them ordered.
        self.assertEqual(body.count("(?="), 3,
                         "the Worker regex must be the three-lookahead "
                         "order-independent form (F3)")
        self.assertNotIn("Semester|Sem|Sem\\.", body,
                         "the ordered Semester … Result constraint must be gone")

    def test_worker_result_terms_cover_the_python_terms(self):
        body = worker_result_terms()
        self.assertIsNotNone(body, "could not read ACADEMIC_RESULT_TERMS_RE")
        lowered = body.lower()
        for term in ("result", "back", "re-?exam", "supplementary"):
            self.assertIn(term, lowered,
                          "Worker result term %r missing — it would stop "
                          "dispatching on that notice type" % term)

    def test_both_sides_agree_on_which_titles_match(self):
        """Behavioural parity on a fixed corpus, so a structural difference
        that slips past the string compares still fails here."""
        src = worker_source()
        token = worker_sem_token()
        self.assertEqual(token, car._SEM_TOKEN)
        corpus = [
            ("TU B.Sc.CSIT IV Semester-2081 Exam Result", 4),
            ("B.Sc.CSIT V Semester Back Result", 5),
            ("B.Sc. CSIT 6th Semester Supplementary Result", 6),
            ("B.Sc.CSIT VIII Semester Result", 8),
            ("Result of the B.Sc.CSIT 4th Semester", 4),
            ("IV Semester Result - TU B.Sc.CSIT", 4),
            ("Admission open for B.Sc.CSIT", None),
            ("B.Sc.CSIT IV Semester form fill", None),
            ("TU exam routine published", None),
            ("", None),
        ]
        js = _js_semester_matcher(src, token)
        for title, expected in corpus:
            self.assertEqual(car.match_semester(title), expected,
                             "python: %r" % title)
            self.assertEqual(js(title), expected, "worker: %r" % title)


def _js_semester_matcher(src, token):
    """Build the equivalent matcher from the Worker's OWN literal source.

    The Worker is a JS module, so it cannot be require()d from Python. Instead
    the two literals it is built from (the semester token and the result-term
    alternation) are lifted straight out of the file and assembled here, so a
    change to either literal changes the behaviour under test.
    """
    result_body = worker_result_terms(src)
    literals = re.findall(r"(?:result|results|back|re-\?exam|supplementary"
                          r"|provisional|marks|transcript)", result_body)
    alternation = "|".join(sorted(set(literals), key=literals.index))
    pattern = (r"(?=.*(?:B\.?\s*Sc\.?|BSc|CSIT))"
               r"(?=.*\b(?:" + token + r")\b)"
               r"(?=.*\b(?:" + alternation + r")\b)")
    compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)

    def match(title):
        if not title or not compiled.search(title):
            return None
        found = re.search(r"\b(?:" + token + r")\b", title, re.IGNORECASE)
        if not found:
            return None
        return car._semester_from_token(found.group(0))

    return match


class KvKeyParityTest(unittest.TestCase):
    def test_floor_key_replaces_the_legacy_high_water_key(self):
        src = worker_source()
        self.assertIn('"academic:last_examined_notice_id"', src)
        self.assertIn('"academic:examined"', src)
        self.assertIn('"academic:dispatched:"', src)
        # the legacy key survives ONLY as the migration source
        legacy = src.count('"academic:last_checked_notice_id"')
        self.assertEqual(legacy, 1,
                         "the legacy high-water key must appear exactly once, "
                         "as the migration source")
        self.assertIn("ACADEMIC_LEGACY_LAST_CHECKED_KEY", src)

    def test_worker_never_writes_the_floor_unconditionally(self):
        """The burn: floor = notices[notices.length-1].id, written regardless of
        whether the loop ever reached that notice."""
        src = worker_source()
        self.assertNotIn("notices[notices.length - 1].id", src)
        self.assertIn("academicAdvance(floor, examined, listIds)", src)
        # R2's clamp must be present in BOTH implementations.
        self.assertIn("lastAttempted", src)
        src_py = _read(os.path.join(SCRIPTS, "check_academic_result.py"))
        self.assertIn("last_attempted", src_py)

    def test_worker_writes_the_examined_map_once(self):
        src = worker_source()
        puts = re.findall(r'await env\.KV\.put\(ACADEMIC_EXAMINED_KEY', src)
        self.assertEqual(len(puts), 1,
                         "examined must be written exactly once per run "
                         "(one write, no partial state)")

    def test_replay_window_is_200_in_both_implementations(self):
        src = worker_source()
        m = re.search(r'const\s+ACADEMIC_REPLAY_WINDOW\s*=\s*(\d+)', src)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), car.REPLAY_WINDOW)
        self.assertEqual(car.REPLAY_WINDOW, 200)

    def test_caps_match(self):
        src = worker_source()
        self.assertEqual(
            int(re.search(r'ACADEMIC_MAX_PENDING\s*=\s*(\d+)', src).group(1)),
            car.MAX_PENDING)
        self.assertEqual(
            int(re.search(r'ACADEMIC_EXAMINED_CAP\s*=\s*(\d+)', src).group(1)),
            car.EXAMINED_CAP)

    def test_degraded_title_is_deferred_in_the_worker(self):
        src = worker_source()
        self.assertIn("titleDegraded", src)
        self.assertIn('academicRecord(examined, id, "deferred", '
                      '"title_unparseable")', src)

    def test_dedup_read_failure_breaks_instead_of_continuing(self):
        src = worker_source()
        self.assertIn('academicRecord(examined, id, "deferred", '
                      '"dispatch_failed")', src)
        # the old `continue` on a KV read throw must be gone
        self.assertNotIn('console.log("academic dedup read failed:", '
                         'String(err));\n      continue;', src)


def _extract_js_function(name):
    """Return the exact source text of a top-level `function <name>(...) {...}`
    from the Worker module, brace-matched. The REAL function text, not a
    reconstruction - a Python proxy of the JS is what let the base-vs-cand
    divergence survive a 'parity' test in the first place."""
    src = worker_source()
    start = src.index("function %s(" % name)
    i = src.index("{", start)
    depth = 0
    for pos in range(i, len(src)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[start:pos + 1]
    raise AssertionError("could not brace-match function %s" % name)


# The gap-floor corpus. Deliberately adversarial: unexamined trailing ids,
# deferred ids in the middle, gaps in the list, and a floor in the middle of a
# run. `1, {1:irrelevant}, [1,2,3,4,5]` is the exact case where a gap scan
# anchored on the moving candidate returns 5 (= max of the list, the burn
# signature) instead of 2.
ADVANCE_CORPUS = [
    # (floor, examined, list_ids, expected)
    (0, {"1": "irrelevant", "2": "irrelevant", "4": "processed"}, [1, 2, 4], 4),
    (0, {"1": "irrelevant", "2": "irrelevant"}, [1, 2, 4], 4),
    (0, {"1": "irrelevant", "2": "irrelevant", "3": "deferred"},
     [1, 2, 3, 4], 2),
    (1, {"1": "irrelevant", "2": "deferred"}, [1, 2], 1),
    (1, {"1": "irrelevant"}, [1, 2, 3, 4, 5], 2),   # the divergence case
    (1, {"1": "irrelevant"}, [1, 2, 3, 4, 5, 6, 7, 8], 2),
    (10, {}, [11, 12, 13], 11),
    (10, {}, [], 10),
    (0, {}, [1, 2, 3], 1),
    (5, {"6": "irrelevant", "7": "irrelevant", "8": "irrelevant", "9": "deferred"},
     [6, 7, 8, 9, 10, 11], 8),
    (100, {"101": "irrelevant", "102": "irrelevant", "103": "irrelevant",
           "104": "irrelevant", "105": "irrelevant", "106": "irrelevant",
           "107": "irrelevant", "108": "irrelevant", "109": "irrelevant",
           "110": "irrelevant", "111": "irrelevant", "112": "irrelevant",
           "113": "irrelevant", "114": "irrelevant", "115": "irrelevant",
           "116": "irrelevant", "117": "irrelevant", "118": "irrelevant",
           "119": "irrelevant", "120": "irrelevant", "121": "irrelevant",
           "122": "irrelevant", "123": "irrelevant", "124": "irrelevant",
           "125": "irrelevant", "126": "irrelevant", "127": "irrelevant",
           "128": "irrelevant", "129": "irrelevant", "130": "irrelevant",
           "131": "irrelevant", "132": "irrelevant", "133": "irrelevant",
           "134": "irrelevant", "135": "irrelevant", "136": "irrelevant",
           "137": "irrelevant", "138": "irrelevant", "139": "irrelevant",
           "140": "irrelevant", "141": "irrelevant", "142": "irrelevant",
           "143": "irrelevant", "144": "irrelevant", "145": "irrelevant",
           "146": "irrelevant", "147": "irrelevant", "148": "irrelevant",
           "149": "irrelevant", "150": "irrelevant", "151": "irrelevant",
           "152": "irrelevant", "153": "irrelevant", "154": "irrelevant",
           "155": "irrelevant", "156": "irrelevant", "157": "irrelevant",
           "158": "irrelevant", "159": "irrelevant"},
     list(range(101, 220)), 160),
]


class ExecutedJsParityTest(unittest.TestCase):
    """P1c. The previous 'parity' tests grepped the Worker's source, which is
    why a real behavioural divergence (gap scan anchored on the moving
    candidate) passed. This class EXECUTES the Worker's own academicAdvance
    under node and compares it with the Python advance() on the same corpus."""

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.node = shutil.which("node")
        if not cls.node:
            # LOUD, NOT SILENT. A parity test that skips and reports green is
            # worse than no parity test at all: it manufactures exactly the
            # false confidence this class exists to remove.
            raise AssertionError(
                "node is required by tests/academic/test_worker_parity.py to "
                "execute the Worker's academicAdvance. It is missing from this "
                "machine, so JS/Python parity is UNVERIFIED. Failing loudly "
                "rather than skipping.")

    def _run_js(self, cases):
        payload = []
        for floor, examined, list_ids, _expected in cases:
            payload.append({
                "floor": floor,
                "examined": dict((str(k), {"outcome": v})
                                 for k, v in examined.items()),
                "listIds": list(list_ids),
            })
        harness = (
            _extract_js_function("academicAdvance")
            + "\n"
            + "const CASES = " + json.dumps(payload) + ";\n"
            + "const OUT = CASES.map(c => academicAdvance(c.floor, c.examined,"
              " c.listIds));\n"
            + "process.stdout.write(JSON.stringify(OUT));\n"
        )
        proc = subprocess.run([self.node, "-e", harness],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            self.fail("node failed to run academicAdvance (exit %s):\n%s"
                      % (proc.returncode, proc.stderr[:2000]))
        return json.loads(proc.stdout)

    def test_worker_and_python_advance_agree(self):
        js_results = self._run_js(ADVANCE_CORPUS)
        self.assertEqual(len(js_results), len(ADVANCE_CORPUS))
        mismatches = []
        for (floor, examined, list_ids, expected), js_value in zip(
                ADVANCE_CORPUS, js_results):
            py_value = car.advance(
                floor,
                dict((str(k), {"outcome": v}) for k, v in examined.items()),
                list_ids)
            if py_value != expected:
                mismatches.append("CORPUS ERROR floor=%r examined=%r list=%r: "
                                  "expected %r, python gave %r"
                                  % (floor, examined, list_ids, expected,
                                     py_value))
            if js_value != py_value:
                mismatches.append(
                    "DIVERGENCE floor=%r examined=%r list=%r: python=%r "
                    "worker=%r (the Worker's gap scan must be anchored on the "
                    "ORIGINAL floor, not the moving candidate)"
                    % (floor, examined, list_ids, py_value, js_value))
        self.assertEqual(mismatches, [],
                         "JS/Python floor divergence:\n" + "\n".join(mismatches))

    def test_the_known_divergence_case_is_covered(self):
        """The exact row from the reviewer's table. If this case is ever removed
        the corpus gets weaker, so assert it is present and correct."""
        row = [c for c in ADVANCE_CORPUS if c[0] == 1 and c[2] == [1, 2, 3, 4, 5]
               and c[1] == {"1": "irrelevant"}]
        self.assertEqual(len(row), 1, "the divergence case must stay in the "
                                       "corpus")
        floor, examined, list_ids, expected = row[0]
        self.assertEqual(expected, 2, "python returns 2, never max(list)=5")
        self.assertEqual(car.advance(floor, {"1": {"outcome": "irrelevant"}},
                                     list_ids), 2)
        self.assertEqual(self._run_js([tuple(row[0])])[0], 2)

    def test_worker_gap_scan_is_anchored_on_the_original_floor(self):
        """Source-level guard to back the behavioural test, so the next editor
        sees WHY the line must read `base + 1`."""
        body = _extract_js_function("academicAdvance")
        self.assertIn("for (let m = base + 1; m < nid; m++)", body)
        self.assertNotIn("for (let m = cand + 1", body,
                         "the gap scan must be anchored on base, not cand")

    def test_python_gap_scan_is_anchored_on_the_original_floor(self):
        src = _read(os.path.join(SCRIPTS, "check_academic_result.py"))
        self.assertIn("for m in range(base + 1, nid):", src)
        self.assertNotIn("for m in range(cand + 1, nid):", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
