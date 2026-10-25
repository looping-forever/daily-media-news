"""tests/validate — contract tests for scripts/validate_stories.py `for_you`.

WHAT IS PINNED HERE
-------------------
`for_you` replaced a mandatory free-text field with an optional,
screened, display-only line. That makes the DELETION behaviour the product:
a line that fails any check disappears silently, and a line that passes must
still be short, concrete and grounded in numerals. These tests therefore
assert, for each of the six screens plus the grounding gate, that a
purpose-built offender is DELETED (not shortened, not kept with a warning) and
that an honest line SURVIVES.

Three properties are pinned above any individual check:

  * DELETE-ONLY — no substitution and no truncation. A guess in a
    personal-consequence slot is the hallucination vector, so the only legal
    repair is deletion.
  * FAIL CLOSED — if the `for_you:` config block in config/profile.yml is
    missing or malformed, the screen strips every line rather than shipping
    an unscreened one. config/ is owned by another agent, so the screen must
    degrade safe, not degrade open.
  * DISPLAY-ONLY — `for_you` never becomes a source of fact. Deadlines keep
    rendering from the validated `deadline` field, which is asserted here
    against the real renderer so a hallucinated line can at worst contradict
    the authoritative date and never replace it.

Every test is offline and clock-free: fixtures only, and the screening config
is injected rather than read from disk, so the suite cannot drift with the
owner's profile. Stdlib unittest only.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
