"""The digest ladder reads the SAME health memory the agent walk keeps.

The summariser used to order by the hard cooldown only, so a rung with eleven
recorded failures still opened every edition until its cooldown file caught up.
Run 42061741326 is the measured case: 99s twice on a rung that produced
nothing, while the next rung answered immediately.

These tests pin the digest half of the fix: health decides the order, a hard
cooldown still ranks last, nothing is ever removed, and exactly ONE readable
order line is emitted per run.
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import model_selector as ms  # noqa: E402
import summarise_stories as ss  # noqa: E402

BROKEN = "gemini/gemini-3.5-flash-lite"
WORKS = "gemini/gemini-3.1-flash-lite"


def story(n):
    return {
        "title": "Story number %d about something that happened" % n,
        "summary": ("Publisher text that is long enough to stand in for a real "
                    "description of the story."),
        "source_name": "example.com",
        "source_url": "https://example.com/%d" % n,
        "category": "world_major",
    }


def reply_for(batch):
    rows = [{"url": s["source_url"],
             "summary": "A model-written sentence about what happened today."}
            for s in batch]
    return json.dumps(rows)


class FakeRun:
    """Answer every batch, and remember which rung was asked in what order."""

    def __init__(self, refuse=()):
        self.calls = []
        self.refuse = set(refuse)

    def __call__(self, prompt, model, timeout=None):
        self.calls.append(model)
        if model in self.refuse:
            self.refuse.discard(model)
            raise ss.Refusal("%s: no answer" % model, kind="timeout")
        # The batch is whatever the prompt lists; one answer per URL is enough
        # because every URL in the prompt is echoed at most once.
        import re
        urls = re.findall(r"https://example\.com/\d+", prompt)
        return json.dumps([{"url": url,
                            "summary": "A model-written sentence about what "
                                       "happened today."}
                           for url in sorted(set(urls))])


class HealthOrdersTheDigestLadder(unittest.TestCase):
    def _run(self, models, health, cooldown=None, stories=None, refuse=()):
        logs = []
        run = FakeRun(refuse=refuse)
        result = ss.summarise(stories or [story(1)], models=list(models),
                              run=run, log=logs.append, cooldown=cooldown,
                              health=health, clock=lambda: 1000.0,
                              sleep=lambda _s: None)
        return result, logs, run

    def test_the_digest_opens_on_the_rung_with_the_better_record(self):
        """BROKEN is configured first and has failed four times; WORKS answered
        five times. The digest must ask WORKS first."""
        health = ms.load_health(None)
        for _ in range(5):
            ms.record_success(health, WORKS, at=1.0)
        ms.record_success(health, BROKEN, at=1.0)
        for _ in range(4):
            ms.record_failure(health, BROKEN, "timeout", at=1.0)

        _result, _logs, run = self._run([BROKEN, WORKS], health)

        self.assertEqual(WORKS, run.calls[0],
                         "the digest still opened on the failing rung: %r"
                         % (run.calls,))

    def test_a_hard_cooldown_still_ranks_last_even_when_most_reliable(self):
        """Health never outranks the one veto, and never removes a rung."""
        health = ms.load_health(None)
        for _ in range(10):
            ms.record_success(health, WORKS, at=1.0)
        ms.record_failure(health, BROKEN, "timeout", at=1.0)
        cooldown = ms.load_cooldown(None)
        # Two hits: `rung_broken` gets one second chance by design.
        for _ in range(2):
            ms.record_cooldown(cooldown, WORKS, "rung_broken", at=time.time())

        # The non-cooled rung refuses once, so the cooled one must still be
        # reached: reordered, never removed.
        result, logs, run = self._run([WORKS, BROKEN], health, cooldown,
                                      refuse=[BROKEN])

        self.assertEqual(BROKEN, run.calls[0],
                         "a cooled rung must not be tried first however good "
                         "its record is: %r" % (run.calls,))
        self.assertEqual(WORKS, run.calls[-1],
                         "a cooled rung is reordered, never removed")
        self.assertTrue(result["summaries"], "the edition must still be written")
        self.assertTrue(any("ladder order" in line for line in logs),
                        "the order change must be stated once: %r" % logs)

    def test_exactly_one_order_line_is_emitted_per_run(self):
        """A per-batch recompute must not re-announce the same line."""
        health = ms.load_health(None)
        ms.record_success(health, WORKS, at=1.0)
        ms.record_failure(health, BROKEN, "timeout", at=1.0)

        _result, logs, _run = self._run(
            [BROKEN, WORKS], health,
            stories=[story(n) for n in range(7)])

        lines = [line for line in logs if "ladder order" in line]
        self.assertEqual(1, len(lines),
                         "the run must state its ladder exactly once, got %r"
                         % (lines,))
        self.assertIn(WORKS, lines[0])
        self.assertIn(BROKEN, lines[0])

    def test_no_history_keeps_the_configured_order_and_still_states_it(self):
        """A fresh install must behave exactly as before, and say so."""
        _result, logs, run = self._run([BROKEN, WORKS], None)

        self.assertEqual(BROKEN, run.calls[0],
                         "with no history the configured order must win")
        self.assertTrue(any("ladder order" in line for line in logs),
                        "even an unchanged order is one line of evidence: %r"
                        % logs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
