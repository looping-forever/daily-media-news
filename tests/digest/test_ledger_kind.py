"""tests/digest/test_ledger_kind.py -- the kind the Actions arm records.

THE DEFECT THIS FILE EXISTS FOR. `record_delivery` wrote `kind: delivered` for
every push the Actions arm made, whatever the push was made of. On 2026-10-28
that arm pushed an unsummarised (no-AI) edition and KV recorded a fresh digest:

    "kind": "delivered", "state": "sent"

Two readers believed it, and both were wrong in ways that cost the owner:

  * `?health=1` printed `"edition": "fresh", "fresh_edition": true` from
    `best.kind`, so the health page said a complete AI digest had arrived on a
    day no model was contacted at all;
  * digest-prebuild.yml:1412 refuses to supersede a slot whose kind is not in
    SUPERSEDED_KINDS, so a real digest sealed later that same day would have
    skipped with "the slot was pushed as 'delivered', which is not a degraded
    push" -- while the comment twelve lines above it says `unsummarised` IS
    degraded precisely so a real digest can replace it.

`kind` says what was BUILT. The rule is the seal's (`digest-prebuild.yml:567`)
and the Worker's (`cloudflare-worker.js:3374`), in that precedence:

    edition_kind == 'unsummarised'  ->  unsummarised
    source_health.state != 'ok'     ->  partial
    otherwise                       ->  delivered

Each test below RUNS the shipped "Publish to ntfy (primary)" step through
tests.digest.test_duplicate_detector.ActionsHarness, so what is asserted is the
record the step really writes, not a string in the workflow.
"""

from __future__ import annotations

import json
import unittest

from tests.digest.test_duplicate_detector import ActionsHarness


class TheRecordedKindIsWhatWasBuiltTests(ActionsHarness):
    """Three measured editions, three honest kinds."""

    def record(self, **fields):
        """Push with this payload and return the slot entry the step wrote."""
        self.payload.update(fields)
        (self.tmp / "state" / "stories.validated.json").write_text(
            json.dumps(self.payload), encoding="utf-8")
        out = self.run_step(history_body="")
        self.assertEqual(
            1, len(self.posts),
            "the push did not happen, so the record inspected below would "
            "describe nothing:\n%s" % out)
        entry = self.slot_entry()
        self.assertIsNotNone(entry, "nothing was recorded in KV: %r"
                             % (self.kv_ledger(),))
        return entry

    def test_an_unsummarised_edition_is_recorded_as_unsummarised(self):
        """The 2026-10-28 shape: no AI, headlines only, health measured ok."""
        entry = self.record(edition_kind="unsummarised",
                            source_health={"state": "ok"})
        self.assertEqual(
            "unsummarised", entry.get("kind"),
            "a no-AI edition was recorded as kind=%r. The sealer's supersede "
            "gate keys on this field, so a real digest sealed later the same day "
            "would refuse to replace it, and ?health=1 tells the owner a fresh "
            "digest arrived: %r" % (entry.get("kind"), entry))
        self.assertEqual("sent", entry.get("state"), entry)

    def test_a_degraded_source_health_is_recorded_as_partial(self):
        """The Worker already writes `partial` for this; the Actions arm did not.

        cloudflare-worker.js:3376 records KIND_PARTIAL whenever the sealed
        artifact says is_partial, and digest-prebuild.yml:567 calls a digest
        partial the moment source health is not `ok`. The Actions arm pushing the
        same edition recorded `delivered`, so the same slug of a day read as
        fresh or partial depending on which arm happened to win the race.
        """
        entry = self.record(source_health={"state": "degraded"})
        self.assertEqual(
            "partial", entry.get("kind"),
            "a digest whose own source check says `degraded` was recorded as "
            "kind=%r, which is the same thing the health page calls a fresh "
            "edition: %r" % (entry.get("kind"), entry))

    def test_unsummarised_outranks_a_degraded_health(self):
        """Precedence, not just the two cases: the Worker checks unsummarised first.

        On a real outage day both facts are true at once -- no model answered AND
        sources failed. `unsummarised` is the one the reader must not have to
        infer, so it wins (cloudflare-worker.js:3374, same order).
        """
        entry = self.record(edition_kind="unsummarised",
                            source_health={"state": "failed"})
        self.assertEqual(
            "unsummarised", entry.get("kind"),
            "the recorded kind was %r. An edition with no AI and failed sources "
            "must not be filed under the weaker label: %r"
            % (entry.get("kind"), entry))

    def test_a_healthy_model_digest_is_still_recorded_as_delivered(self):
        """The control. Without it a fix that stamped everything `partial` passes.

        Every test above asserts a non-`delivered` kind, so all three would stay
        green if the step simply stopped recording healthy digests at all.
        """
        entry = self.record(source_health={"state": "ok"})
        self.assertEqual(
            "delivered", entry.get("kind"),
            "an ordinary model-written digest with healthy sources was recorded "
            "as kind=%r, which would make the sealer supersede a complete "
            "edition and the health page call a fresh digest degraded: %r"
            % (entry.get("kind"), entry))


if __name__ == "__main__":
    unittest.main()
