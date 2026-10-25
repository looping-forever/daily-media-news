"""THE MODEL-SELECTOR PIN MUST BE UNREACHABLE FROM PRODUCTION.

Live model discovery shells out to `opencode models <provider> --verbose`. The
CI runner is `ubuntu-24.04`, which has no `opencode` binary, so discovery
answered `[]` with `opencode_not_on_path`, the resolved ladder collapsed to the
3-model config seed, and two tests that assert the ladder is dynamic failed --
every day, producing a red canary and an email.

The fix is a pinned fixture, and a pin that fired in production would be the
exact failure this selector is built to prevent: a genuinely broken selector
would quietly resolve to a frozen list instead of degrading loudly to the seed,
and nobody would ever be told. There is a mutation proof pinning that loud
degradation (`test_a_broken_selector_degrades_LOUDLY_never_silently_to_the_seed`),
so the pin has to stay strictly a test-environment affordance.

This test is what makes that structural rather than a promise in a comment: it
reads every committed workflow and fails if any of them sets the variable that
switches the pin on.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SELECTOR = REPO_ROOT / "scripts" / "model_selector.py"

PIN_ENV = "MODEL_SELECTOR_PIN"


class ModelSelectorPinGuardTests(unittest.TestCase):
    def test_no_committed_workflow_sets_the_pin(self):
        """Production must not be able to reach the fixture, even by accident."""
        offenders = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if PIN_ENV not in text:
                continue
            offenders.append("%s sets %s, so a real run could resolve its model "
                             "ladder from a frozen fixture instead of the "
                             "provider catalogue -- which is the silent "
                             "degradation this selector must never do"
                             % (path.name, PIN_ENV))
        self.assertEqual(offenders, [],
                         "\n".join(offenders))

    def test_the_pin_is_only_read_when_the_cli_is_genuinely_absent(self):
        """A live CLI must always win, so a working runner can never be
        overridden by the fixture even if the variable were set."""
        src = SELECTOR.read_text(encoding="utf-8")
        self.assertIn(PIN_ENV, src, "the selector no longer knows about the pin")
        which = src.find('shutil.which("opencode")')
        self.assertGreater(which, 0, "the selector no longer probes for the CLI")
        # The pin call must sit INSIDE the `if (not exe)` branch, i.e. after the
        # probe and before the `opencode_not_on_path` bail-out.
        pin_call = src.find("read_pinned_models(info)")
        bail = src.find('"opencode_not_on_path"')
        self.assertGreater(pin_call, 0, "the pin is never consulted")
        self.assertGreater(bail, 0, "the selector no longer reports a missing CLI")
        self.assertGreater(which, -1)
        branch = src[which:bail]
        self.assertIn("read_pinned_models(info)", branch, (
            "the pin is consulted somewhere other than the `if (not exe)` branch, "
            "so it could override a live catalogue read"))
        self.assertIsNone(
            re.search(r"read_pinned_models\([^)]*\)\s*\n\s*if\s*\(", src),
            "the pin result is not gated on the CLI being absent")


if __name__ == "__main__":
    unittest.main(verbosity=2)