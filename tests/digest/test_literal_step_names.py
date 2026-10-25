#!/usr/bin/env python3
"""tests/digest/test_literal_step_names.py -- the two ntfy step names.

THE LANDMINE
------------
`tests/render/test_render_honesty.py` recovers the ntfy push step by slicing
`.github/workflows/daily-news.yml` between two literal step names:

    start = src.index("- name: Publish to ntfy (primary)")
    end   = src.index("- name: Send digest via Gmail (backup)")

Later phases of the punctuality work WILL edit that workflow. A rename does not
produce an assertion failure -- it produces `ValueError: substring not found`
raised from inside another suite, on a different file, with no mention of step
names. This suite exists so that rename fails HERE instead, loudly and with a
sentence that says what to do about it.

WHY IT IS NOT JUST TWO `assertIn` CALLS
---------------------------------------
Because that test would be nearly worthless on its own. The slice is taken by
NAME, so both literals survive happily while the heredoc between them is
deleted, emptied, re-indented or given a broken `PY` terminator. Every ntfy
test in the render suite would fail with a confusing `IndentationError` or
`SyntaxError` from exec'd text, and the step-name assertion would still be
green.

So both halves are asserted, and the second half is the load-bearing one:

  * both literal names are present verbatim;
  * the extraction returns text that COMPILES;
  * `run_ntfy()` executes the real heredoc under DRY RUN and parses a real
    payload back out of the "Payload preview:" marker.

That is the whole path a reader of the render suite depends on, exercised for
real rather than asserted about. The helpers are IMPORTED from
test_render_honesty, never reimplemented: a copy would agree with itself and
tell us nothing when the original drifted.

OFFLINE BY CONSTRUCTION
-----------------------
`run_ntfy` sets DRY_RUN=true and poisons `urllib.request.urlopen` to raise, so
this can never make a network call, and it asserts the DRY RUN marker ran so
the branch is not merely assumed. Every write lands in a temp directory. No
step is renamed and no heredoc body is modified by this file.
"""

import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)
if os.path.join(_TESTS, "render") not in sys.path:
    sys.path.insert(0, os.path.join(_TESTS, "render"))

from render.test_render_honesty import (  # noqa: E402
    WORKFLOW, ntfy_source, run_ntfy)

from fixtures import twenty_story_payload, validated_payload  # noqa: E402

#: The two literals `ntfy_source()` slices the workflow by, stated here as
#: named constants rather than as strings buried in assertions, so a failure
#: names the exact thing that moved.
#:
#: They are duplicated from `ntfy_source` rather than imported, because that
#: module hardcodes them inside its two `src.index(...)` calls and exposes no
#: constants to import -- and that file is not owned by this change. That
#: duplication is the honest shape of this guard: it pins the names from the
#: OUTSIDE. The moment someone updates the index() calls in
#: test_render_honesty.py, this suite fails and the two are updated together.
NTFY_STEP_NAME = "- name: Publish to ntfy (primary)"
NTFY_NEXT_STEP_NAME = "- name: Send digest via Gmail (backup)"


class TestLiteralStepNamesExist(unittest.TestCase):
    """Half one. The strings themselves, verbatim, in the real workflow."""

    def test_both_step_names_are_present_verbatim(self):
        with open(WORKFLOW, encoding="utf-8") as handle:
            src = handle.read()
        for name in (NTFY_STEP_NAME, NTFY_NEXT_STEP_NAME):
            self.assertIn(name, src,
                          "the literal step name %r is gone from %s. The ntfy "
                          "tests in tests/render/test_render_honesty.py slice "
                          "this workflow between these two names, so renaming "
                          "one breaks every ntfy test with a bare ValueError "
                          "instead of a readable failure. Either keep the name "
                          "or update the two index() calls in that file "
                          "together with this test."
                          % (name, os.path.basename(WORKFLOW)))

    def test_the_two_names_are_distinct(self):
        self.assertNotEqual(NTFY_STEP_NAME, NTFY_NEXT_STEP_NAME)

    def test_the_ntfy_name_comes_before_the_gmail_name(self):
        """Order matters: the slice is [ntfy, gmail), so reversed names would
        yield an empty or inverted chunk rather than an error."""
        with open(WORKFLOW, encoding="utf-8") as handle:
            src = handle.read()
        self.assertLess(src.index(NTFY_STEP_NAME), src.index(NTFY_NEXT_STEP_NAME))


class TestHeredocExtractionActuallyWorks(unittest.TestCase):
    """Half two. The path the render suite depends on, exercised for real."""

    def test_extraction_returns_compilable_python(self):
        source = ntfy_source()
        self.assertTrue(source.strip(),
                        "the slice between the two step names held nothing")
        try:
            compile(source, "daily-news.yml:ntfy", "exec")
        except SyntaxError as exc:
            self.fail("the recovered heredoc does not compile (%s). The step "
                      "names may still be intact while the body between them "
                      "is re-indented or its PY terminator moved." % exc)

    def test_extraction_really_contains_the_ntfy_push_code(self):
        """Not a substring check on the names -- on the CODE inside the slice.

        This is what proves the slice boundaries still surround the push rather
        than an empty or adjacent step.
        """
        source = ntfy_source()
        for needle in ("urllib.request", "Payload preview:"):
            self.assertIn(needle, source,
                          "the slice between the two step names no longer "
                          "contains %r; the boundaries are probably wrong"
                          % needle)

    def test_run_ntfy_executes_the_heredoc_and_parses_a_payload(self):
        """THE END-TO-END HALF.

        This is the render suite's actual dependency: exec the real heredoc
        under DRY RUN, then decode the JSON that follows "Payload preview:" in
        its stdout. If any link in that chain breaks -- the slice, the heredoc
        marker, the dedent, the DRY RUN branch, the preview print -- this
        fails here with a readable message instead of there with a
        ValueError from str.index.
        """
        tmp = tempfile.mkdtemp(prefix="digest-stepnames-")
        try:
            got = run_ntfy(tmp, twenty_story_payload("degraded"))
            self.assertIsInstance(got, dict,
                                  "run_ntfy did not decode a JSON object from "
                                  "the Payload preview marker: %r" % (got,))
            # A payload that parsed is not automatically a usable payload.
            self.assertIn("title", got)
            self.assertIn("message", got)
            self.assertTrue(got["message"].strip(),
                            "the parsed ntfy payload carried an empty message")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_parsed_payload_is_the_one_that_was_fed_in(self):
        """The parse must reflect the input, not merely return some JSON.

        A payload preview that drifted into printing a constant would still
        decode cleanly and still satisfy a bare assertIsInstance check.
        """
        tmp = tempfile.mkdtemp(prefix="digest-stepnames-echo-")
        try:
            payload = validated_payload("ok")
            got = run_ntfy(tmp, payload, kind="evening")
            titles = [s.get("title") for s in payload["stories"]]
            self.assertTrue(titles)
            for title in titles:
                self.assertIn(title, got["message"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()