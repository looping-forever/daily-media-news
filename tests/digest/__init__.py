"""tests/digest/ -- guards on the digest pipeline's wiring, not its output.

WHY THIS PACKAGE EXISTS
-----------------------
`tests/render/test_render_honesty.py` locates the ntfy push step by SLICING
`.github/workflows/daily-news.yml` between two literal step names:

    start = src.index("- name: Publish to ntfy (primary)")
    end   = src.index("- name: Send digest via Gmail (backup)")
    chunk = src[start:end]
    m     = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", chunk, re.S)

Every ntfy test in that file then `exec`s the heredoc it recovered. That is a
real and correct design -- it tests the bytes the workflow actually ships
rather than a copy of them -- and it is also a silent landmine: renaming
either step does not fail an ASSERTION, it fails `str.index` with a
`ValueError`. The failure reads as "the workflow is broken" in a place where
the workflow is fine and the TEST is out of date, which is the worst possible
place to spend a debugging hour.

So the coupling is pinned explicitly, by the suite that depends on it. The
step names are not interesting in themselves; they are load-bearing INPUTS to
another suite, and that is worth a test.

WHAT IS ASSERTED, AND WHY IT IS NOT JUST TWO STRING ASSERTIONS
--------------------------------------------------------------
Asserting the two literals are present would be near-worthless on its own: it
would still pass if the heredoc between them were deleted, re-indented,
re-terminated or emptied, because the slice is taken by NAME and says nothing
about what is inside it. A name that survives with a broken body is the harder
failure, and it is the one that would ship.

So this suite asserts BOTH:

  1. the two literal step-name strings exist verbatim in daily-news.yml; and
  2. the extraction genuinely works END TO END -- `ntfy_source()` returns
     executable Python, that Python COMPILES, and `run_ntfy()` runs it under
     DRY RUN and parses a real payload back out of the "Payload preview:"
     marker on stdout.

Assertion 2 is the one with teeth: it re-uses the production helpers rather
than reimplementing them, so a change to the extraction breaks this suite too.
The ntfy body is never modified and network access is poisoned.
"""