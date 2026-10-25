"""tests/weather — contract tests for scripts/weather_advice.py.

WHAT IS PINNED HERE
-------------------
scripts/weather_advice.py turns a forecast into decisions the owner acts on
("should I wash clothes", "do I need a jacket"). The owner reads those lines
at 06:00 and changes his morning on them, so a rewording is a behaviour
change and a threshold moved in the wrong direction is advice for the wrong
place. These tests therefore assert the EXACT output string of every rule,
character for character, and pin the three properties that matter more than
any individual line:

  * the SILENT MIDDLE BAND -- 40 <= wash risk < 50 yields an umbrella line and
    NO washing verdict at all;
  * DEGRADATION IS SILENCE -- unusable or stale input returns [], never a
    placeholder the owner cannot act on;
  * THE SERIOUS GUARD -- a commitment intersecting the wash window downgrades
    "don't wash" to "wash after", and is never permitted to produce a
    prohibition.

Every test is offline and clock-free: fixtures only, and staleness is tested
by injecting `now`, never by depending on the day the suite happens to run.
Stdlib unittest only, and no pip installs — the module under test is stdlib
too, including its YAML reader.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
