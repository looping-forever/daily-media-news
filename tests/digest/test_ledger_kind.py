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

from tests.digest.test_duplicate_detector import ActionsHarness, SLOT, TODAY


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


class TheRecordedEditionIsWhatWasBuiltTests(ActionsHarness):
    """`edition` is DERIVED at the write, so the newest attempt always wins.

    THE DEFECT THIS FILE'S SECOND HALF EXISTS FOR. `kv_record_delivery` merges
    its patch onto whatever the slot already records and wrote `state`/`kind`
    but never `edition`. The 2026-10-28 evening row is the residue:

        "state": "sent", "kind": "unsummarised", "edition": "failed"

    A success inherited the failed edition of the attempt it replaced, and
    ?health=1 could only report a contradiction it had no authority to resolve.
    `edition` is a function of the two fields beside it, so it is computed where
    they are written -- after the honesty correction, never before it.
    """

    def push(self, seed=None, env=None, **fields):
        """Push with this payload onto this seeded slot; return STORED entry."""
        self.payload.update(fields)
        (self.tmp / "state" / "stories.validated.json").write_text(
            json.dumps(self.payload), encoding="utf-8")
        out = self.run_step(history_body="", seed_ledger=seed,
                            env_overrides=env)
        self.assertEqual(
            1, len(self.posts),
            "the push did not happen, so the record inspected below would "
            "describe nothing:\n%s" % out)
        entry = self.stored_slot_entry()
        self.assertIsNotNone(
            entry, "nothing was recorded in KV: %r" % (self.kv_ledger(),))
        return entry

    @staticmethod
    def seeded(entry):
        """A ledger whose slot already holds `entry` -- what a merge runs onto."""
        return {"schema_version": 1, "as_of": "2026-10-28T11:00:00Z",
                "days": {TODAY: {SLOT: entry}}}

    def test_a_success_after_a_failure_clears_the_stale_failed(self):
        """The 2026-10-28 evening shape, replayed forwards.

        The slot holds a VERIFIED failure from an earlier attempt. This run's
        push succeeds, so the record must stop calling the edition failed -- the
        success is the newest fact and the only one the owner's phone agrees
        with.
        """
        seed = self.seeded({
            "state": "failed", "kind": "fallback_last_good", "edition": "failed",
            "ntfy_failed": True, "http_status": 429, "attempts": 3,
            "pending_at": "2026-10-28T11:00:00Z",
        })
        entry = self.push(seed=seed, source_health={"state": "ok"})
        self.assertEqual("sent", entry.get("state"), entry)
        self.assertEqual(
            "fresh", entry.get("edition"),
            "a success inherited the earlier attempt's failed edition: %r"
            % (entry,))
        self.assertNotIn(
            "ntfy_failed", entry,
            "the earlier attempt's verified failure survived a success: %r"
            % (entry,))

    def test_an_unsummarised_build_records_edition_unsummarised(self):
        entry = self.push(seed=None, edition_kind="unsummarised",
                          source_health={"state": "ok"})
        self.assertEqual("unsummarised", entry.get("kind"), entry)
        self.assertEqual(
            "unsummarised", entry.get("edition"),
            "a no-AI edition was recorded with edition=%r, which is the field "
            "?health=1 reads to decide whether the owner got TODAY's news: %r"
            % (entry.get("edition"), entry))

    def test_a_degraded_build_after_a_success_records_the_degradation(self):
        """The mirror, and the direction that under-claims if it is wrong.

        A slot already records a complete digest; this run pushes an
        unsummarised one. The record must describe the NEWEST push, not keep the
        flattering one: the phone is showing headlines, not AI summaries.

        THE SEED CARRIES NO `ntfy_sent_at`, DELIBERATELY. With one, the
        duplicate guard stands down and pushes nothing -- which is the correct
        behaviour for a slot that already went out, and is pinned next door in
        tests/digest/test_duplicate_detector.py. The seed here is the slot whose
        instant was LOST (the 2026-10-26 evening shape: the message was on the
        topic and KV still read `pending`), which is the case where a second
        push really happens.
        """
        seed = self.seeded({
            "state": "pending", "kind": "delivered", "edition": "fresh",
            "sequence_id": "dmn-2026-10-13-morning",
            "pending_at": "2026-10-28T11:00:00Z",
        })
        entry = self.push(seed=seed, edition_kind="unsummarised",
                          source_health={"state": "ok"})
        self.assertEqual("unsummarised", entry.get("kind"), entry)
        self.assertEqual(
            "unsummarised", entry.get("edition"),
            "the slot kept the earlier success's edition beside a degraded "
            "push: %r" % (entry,))

    def test_the_supersede_records_what_it_pushed_not_what_it_replaced(self):
        """A supersede is a SECOND push, and the phone shows the second one.

        `record_delivery` used to write only `ntfy_full_sent_at` and drop the
        `pushed_kind` it was handed, so a slot replaced by a complete AI edition
        kept reading `kind: partial` for the rest of the day. The replaced kind
        is moved aside, never erased.
        """
        seed = self.seeded({
            "state": "sent", "kind": "partial", "edition": "partial",
            "sequence_id": "dmn-2026-10-13-morning",
            "ntfy_sent_at": "2026-10-28T11:00:00Z",
            "first_ntfy_sent_at": "2026-10-28T11:00:00Z",
        })
        entry = self.push(seed=seed, env={"TRIGGER_SOURCE": "supersede"},
                          source_health={"state": "ok"})
        self.assertIn("ntfy_full_sent_at", entry, entry)
        self.assertEqual(
            "delivered", entry.get("kind"),
            "the supersede left the slot describing the degraded push it "
            "replaced: %r" % (entry,))
        self.assertEqual(
            "fresh", entry.get("edition"),
            "a slot replaced by a complete digest is still recorded degraded: %r"
            % (entry,))
        self.assertEqual(
            "partial", entry.get("superseded_kind"),
            "the replaced kind was erased instead of kept: %r" % (entry,))
        self.assertEqual(
            "2026-10-28T11:00:00Z", entry.get("ntfy_sent_at"),
            "the supersede wrote ntfy_sent_at, which the duplicate guard reads "
            "as 'this slot already went out': %r" % (entry,))


if __name__ == "__main__":
    unittest.main()