#!/usr/bin/env python3
"""tests/render/test_delivery_line.py -- the punctuality health line.

Seven states, and the property that matters more than any of them:

    NO STATE EVER RENDERS AN EMPTY STRING.

An absent, truncated, corrupt or history-free ledger renders the ALARM, never
a blank. A blank line is the silent failure this module exists to prevent,
because a reader cannot tell "nothing went wrong" from "nothing was
measured", and only one of those is good news.

Every state below is asserted against its EXACT string, not a substring and
not a shape check: these lines go on a page and are read by a person deciding
whether the digest they are holding is current.

THE FIXTURES ARE HONEST ABOUT PENDING SLOTS
-------------------------------------------
A slot whose scheduled instant is later than the ledger's `as_of` is PENDING,
because the ledger has not been written since. That is the whole mechanism
behind state 2, and it needs no clock and no guessing: the ledger's own write
time already answers "has this slot had its turn yet". Every fixture below
therefore pins `as_of` explicitly, and `every_slot` only emits entries for
slots that had actually fired by then.

Also here: the ledger maths (slot_from_cron, npt_date_from_instant, prune),
the humanised durations, and the proof that the feature SHIPS DARK -- a render
with no --delivery-ledger is byte-identical to one from before it existed.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fixtures import validated_payload, write_json  # noqa: E402

import delivery_ledger  # noqa: E402
import render_digest  # noqa: E402

#: The two schedules, chosen so their NEPALI wall clocks are the ones the
#: design's line is written against: 01:35 UTC = 07:20 NPT, 13:35 UTC =
#: 19:20 NPT. The line derives those numbers from the ledger's declared
#: crons; nothing here is hardcoded into the renderer.
MORNING_CRON = "35 1 * * *"
EVENING_CRON = "35 13 * * *"
CRONS = [MORNING_CRON, EVENING_CRON]

#: The window is 7 Nepal days ending on the ledger's own `as_of`, so every
#: fixture names its window explicitly rather than drifting with today.
DAYS = ["2026-10-%02d" % d for d in (6, 7, 8, 9, 10, 11, 12)]

#: 06:45 NPT on the last day: before this morning's push, so BOTH of the last
#: day's slots are still pending and the other twelve are measurable.
BEFORE_MORNING = "2026-10-12T01:00:00Z"
#: 07:45 NPT on the last day: after the morning push, before the evening one.
AFTER_MORNING = "2026-10-12T02:00:00Z"
#: 19:30 NPT on the last day: after both pushes, so nothing is pending.
AFTER_BOTH = "2026-10-12T14:00:00Z"

UTC = datetime.timezone.utc

ALARM = ("⚠️ Delivery history unavailable — this digest may have arrived "
         "late.")


def slot_cron(slot):
    return MORNING_CRON if slot == "morning" else EVENING_CRON


def entry(npt_date, slot, kind, delta_s=None, duplicate_of=None):
    """One ledger entry, with its instants built from the cron.

    `delta_s` becomes first_ntfy_sent_at rather than a stored field, because
    the module's contract is that a delta is the difference of two recorded
    instants and never a number somebody typed into the file.
    """
    cron = slot_cron(slot)
    scheduled = delivery_ledger._scheduled_instant(npt_date, cron)
    out = {
        "npt_date": npt_date,
        "slot": slot,
        "cron": cron,
        "scheduled_instant": delivery_ledger._format_instant(scheduled),
        "kind": kind,
    }
    if delta_s is not None:
        sent = scheduled + datetime.timedelta(seconds=delta_s)
        out["first_ntfy_sent_at"] = delivery_ledger._format_instant(sent)
    if duplicate_of:
        out["duplicate_of"] = duplicate_of
    return out


def rebuild_cron(item, cron):
    """An entry re-pointed onto a different cron, keeping its measured delta.

    Needed because a delivery is matched to its slot BY scheduled instant: an
    entry recorded against the old cron cannot be matched to a slot of the new
    one, so moving the schedule has to move the recorded instants too. The
    DELTA is what is preserved, because that is the measurement and the cron is
    only the schedule.
    """
    out = dict(item)
    scheduled = delivery_ledger._scheduled_instant(item["npt_date"], cron)
    out["cron"] = cron
    out["scheduled_instant"] = delivery_ledger._format_instant(scheduled)
    sent = item.get("first_ntfy_sent_at")
    if sent:
        delta = (delivery_ledger._parse_instant(sent)
                 - delivery_ledger._parse_instant(item["scheduled_instant"]))
        out["first_ntfy_sent_at"] = delivery_ledger._format_instant(
            scheduled + delta)
    return out


def ledger(as_of, entries, crons=None):
    return {"schema_version": 1, "as_of": as_of,
            "crons": list(crons if crons is not None else CRONS),
            "entries": list(entries)}


def every_slot(as_of, delta_s=30, skip=()):
    """A delivered entry per slot of the window, for slots that had fired.

    `as_of` is honoured on purpose. Emitting an entry for a slot that has not
    happened yet would be a fixture lying about the world, and the state's it
    is used for (2, 5) are precisely the ones decided by which slots are still
    pending.
    """
    cutoff = datetime.datetime.strptime(as_of, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC)
    out = []
    for day in DAYS:
        for slot in ("morning", "evening"):
            if (day, slot) in skip:
                continue
            if delivery_ledger._scheduled_instant(day, slot_cron(slot)) > cutoff:
                continue
            out.append(entry(day, slot, delivery_ledger.KIND_DELIVERED,
                             delta_s))
    return out


def one_late(as_of, delta_s, at, kind=None):
    """A window where every fired slot is on time except `at`, which is late.

    Rewritten in place rather than appended, so no slot ever carries two
    entries for the same instant -- a shape the module has no reason to
    tolerate and the fixtures have no reason to produce.
    """
    entries = every_slot(as_of, 41, skip=[at])
    entries.append(entry(at[0], at[1], kind or delivery_ledger.KIND_DELIVERED,
                         delta_s))
    return entries


# ---------------------------------------------------------------------------
# The seven states, each against its exact string
# ---------------------------------------------------------------------------


class TestStateOneUnreadable(unittest.TestCase):
    """State 1. No measurement possible -> the alarm, never a blank."""

    def test_none_ledger(self):
        self.assertEqual(delivery_ledger.delivery_line(None), ALARM)

    def test_empty_dict(self):
        self.assertEqual(delivery_ledger.delivery_line({}), ALARM)

    def test_entries_wrong_type(self):
        for junk in ("nope", 7, {}, [1, 2]):
            self.assertEqual(
                delivery_ledger.delivery_line({"entries": junk,
                                               "as_of": AFTER_BOTH,
                                               "crons": CRONS}),
                ALARM, junk)

    def test_unparseable_as_of(self):
        for bad in ("", "not-a-date", "2026-10-12", None, "2026-13-45T00:00:00Z"):
            line = delivery_ledger.delivery_line(
                ledger(bad, every_slot(AFTER_BOTH)))
            self.assertEqual(line, ALARM, bad)

    def test_unusable_crons(self):
        for bad in ([], "morning", [7], ["nope"], ["1 2 * *"], ["1 2 * * * *"],
                    ["1 2 * * * 7"], [None]):
            line = delivery_ledger.delivery_line(
                ledger(AFTER_BOTH, every_slot(AFTER_BOTH), crons=bad))
            self.assertEqual(line, ALARM, bad)

    def test_history_free_ledger_is_the_alarm_not_a_false_all_clear(self):
        """A ledger that has never recorded a delivery cannot say 0/14.

        That is the subtle version of the blank-line defect: the numbers would
        be true and the impression completely false.
        """
        for entries in ([], [{"npt_date": "2026-10-12", "slot": "morning"}],
                        [entry("2026-10-12", "morning",
                               delivery_ledger.KIND_UNAVAILABLE)]):
            line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
            self.assertEqual(line, ALARM, entries)
        self.assertNotIn("0/14", line)

    def test_a_truncated_file_reaches_the_alarm(self):
        """What load_ledger returns for half-written JSON, end to end."""
        tmp = tempfile.mkdtemp(prefix="delivery-truncated-")
        try:
            path = os.path.join(tmp, "delivery.json")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write('{"as_of": "2026-10-12T14:00:00Z", "entries": [')
            self.assertIsNone(delivery_ledger.load_ledger(path))
            self.assertEqual(delivery_ledger.delivery_line(
                delivery_ledger.load_ledger(path)), ALARM)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_ledger_reads_a_good_file_back(self):
        tmp = tempfile.mkdtemp(prefix="delivery-good-")
        try:
            path = os.path.join(tmp, "delivery.json")
            data = ledger(AFTER_BOTH, every_slot(AFTER_BOTH))
            with io.open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            self.assertEqual(delivery_ledger.load_ledger(path), data)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_alarm_literal_is_exact(self):
        self.assertEqual(ALARM, "⚠️ Delivery history unavailable — this "
                                "digest may have arrived late.")


class TestStateTwoSlotNotYetDue(unittest.TestCase):
    """State 2. This run's own slot has not gone out yet.

    The normal pre-push case. The line reports the schedule and the measured
    window and says nothing about the pending slots, because nothing has
    happened to them yet -- counting them as missed would report an outage
    twice a day and train the reader to ignore the line.
    """

    def test_exact_line(self):
        data = ledger(BEFORE_MORNING, every_slot(BEFORE_MORNING, 41))
        self.assertEqual(
            delivery_ledger.delivery_line(data),
            "Delivery: pushes at 07:20 and 19:20 NPT · last 7 days on time "
            "12/14 · latest 41s late")

    def test_pending_slots_are_neither_delivered_nor_missing(self):
        line = delivery_ledger.delivery_line(
            ledger(BEFORE_MORNING, every_slot(BEFORE_MORNING, 41)))
        self.assertNotIn("missing", line)
        self.assertNotIn("email fallback", line)

    def test_push_times_come_from_the_declared_crons(self):
        """Move the schedule and the rendered times move with it.

        A hardcoded "07:20" would put a number on the page that nothing has to
        agree with. The entries are rebuilt onto the new schedule too, since an
        entry recorded against the old cron cannot be matched to a new slot.
        """
        moved = ["25 1 * * *", "25 13 * * *"]
        entries = [rebuild_cron(item, moved[0] if item["slot"] == "morning"
                                else moved[1])
                   for item in every_slot(BEFORE_MORNING, 41)]
        data = ledger(BEFORE_MORNING, entries, crons=moved)
        self.assertIn("pushes at 07:10 and 19:10 NPT",
                      delivery_ledger.delivery_line(data))

    def test_lateness_anywhere_does_not_hide_behind_a_pending_slot(self):
        """State 2 reports the count, so a late slot is still visible in it.

        12 of 14 on time is the reader's evidence that something was late; the
        line must not flatten to a clean bill of health because two slots have
        not had their turn.
        """
        entries = one_late(BEFORE_MORNING, 700, ("2026-10-08", "evening"))
        line = delivery_ledger.delivery_line(ledger(BEFORE_MORNING, entries))
        self.assertIn("last 7 days on time 11/14", line)


class TestStateThreeAllOnTime(unittest.TestCase):
    """State 3. Every slot delivered, every delta within ON_TIME_S."""

    def test_exact_line(self):
        data = ledger(AFTER_BOTH, every_slot(AFTER_BOTH, 41))
        self.assertEqual(
            delivery_ledger.delivery_line(data),
            "Delivery: on time for the last 7 days (14/14 slots, worst 41s late)")

    def test_exactly_on_the_threshold_is_still_on_time(self):
        data = ledger(AFTER_BOTH,
                      every_slot(AFTER_BOTH, delivery_ledger.ON_TIME_S))
        self.assertIn("on time for the last 7 days",
                      delivery_ledger.delivery_line(data))

    def test_the_worst_is_the_maximum_across_the_whole_window(self):
        entries = every_slot(AFTER_BOTH, 5)
        entries = one_late(AFTER_BOTH, 59, ("2026-10-09", "evening"))
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertIn("(14/14 slots, worst 59s late)", line)
        del entries

    def test_a_delta_past_the_threshold_is_never_state_three(self):
        """The threshold is ON_TIME_S and it is the ONLY threshold.

        Recorded because the design's example line for this state quotes a
        worst of "2m 05s" -- 125s, more than twice ON_TIME_S, so under the
        stated threshold it is unreachable here. The threshold wins, and this
        pins that 125s renders as state 4, so the example cannot be quietly
        believed.
        """
        data = ledger(AFTER_BOTH, every_slot(AFTER_BOTH, 125))
        self.assertEqual(
            delivery_ledger.delivery_line(data),
            "Delivery: 0/14 slots on time · worst 2m 05s late "
            "(morning 2026-10-06) · latest 2m 05s late")


class TestStateFourLate(unittest.TestCase):
    """State 4. A slot went out late, and the line says so with the number."""

    def test_exact_line(self):
        entries = one_late(AFTER_BOTH, 760, ("2026-10-06", "evening"))
        self.assertEqual(
            delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries)),
            "Delivery: 13/14 slots on time · worst 12m 40s late "
            "(evening 2026-10-06) · latest 41s late")

    def test_the_worst_number_is_printed_verbatim_and_never_softened(self):
        entries = one_late(AFTER_BOTH, 661, ("2026-10-06", "evening"))
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertIn("worst 11m 01s late (evening 2026-10-06)", line)
        for softener in ("recently", "slightly", "about", "~", "nearly",
                         "only"):
            self.assertNotIn(softener, line)

    def test_two_late_slots_still_name_the_worst_one(self):
        entries = one_late(AFTER_BOTH, 760, ("2026-10-06", "evening"))
        entries = [e for e in entries
                   if (e["npt_date"], e["slot"]) != ("2026-10-08", "morning")]
        entries.append(entry("2026-10-08", "morning",
                             delivery_ledger.KIND_DELIVERED, 300))
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertIn("12/14 slots on time", line)
        self.assertIn("worst 12m 40s late (evening 2026-10-06)", line)

    def test_the_worst_slot_is_labelled_with_its_slot_and_date(self):
        entries = one_late(AFTER_BOTH, 760, ("2026-10-06", "evening"))
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertIn("(evening 2026-10-06)", line)


class TestStateFiveMissing(unittest.TestCase):
    """State 5. A slot that was due produced no push at all."""

    def test_exact_line(self):
        # 13 slots had fired by as_of; one of them never produced a push.
        entries = every_slot(AFTER_MORNING, 41,
                             skip=[("2026-10-06", "evening")])
        self.assertEqual(
            delivery_ledger.delivery_line(ledger(AFTER_MORNING, entries)),
            "Delivery: 12/14 slots delivered · missing: evening 2026-10-06")

    def test_missing_is_counted_against_the_fired_slots(self):
        """13 fired, 1 missing -> 12 delivered of 14 the window owes.

        The denominator is what the schedule OWES, not what the file happens
        to contain, so a missed push cannot shrink the number it is measured
        against.
        """
        entries = every_slot(AFTER_MORNING, 41,
                             skip=[("2026-10-06", "evening")])
        line = delivery_ledger.delivery_line(ledger(AFTER_MORNING, entries))
        self.assertIn("12/14", line)

    def test_a_slot_marked_unavailable_counts_as_missing(self):
        entries = every_slot(AFTER_BOTH, 41)
        for item in entries:
            if (item["npt_date"], item["slot"]) == ("2026-10-07", "morning"):
                item["kind"] = delivery_ledger.KIND_UNAVAILABLE
                item["first_ntfy_sent_at"] = None
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertIn("13/14 slots delivered", line)
        self.assertIn("missing: morning 2026-10-07", line)

    def test_several_missing_slots_are_all_named_in_slot_order(self):
        entries = every_slot(AFTER_BOTH, 41,
                             skip=[("2026-10-06", "evening"),
                                   ("2026-10-09", "morning")])
        self.assertEqual(
            delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries)),
            "Delivery: 12/14 slots delivered · missing: evening 2026-10-06, "
            "morning 2026-10-09")

    def test_missing_outranks_late(self):
        """A slot that never arrived is a bigger fact than one that was slow."""
        entries = every_slot(AFTER_MORNING, 41,
                             skip=[("2026-10-06", "evening")])
        entries = [e for e in entries
                   if (e["npt_date"], e["slot"]) != ("2026-10-06", "morning")]
        entries.append(entry("2026-10-06", "morning",
                             delivery_ledger.KIND_DELIVERED, 700))
        line = delivery_ledger.delivery_line(ledger(AFTER_MORNING, entries))
        self.assertEqual(
            line, "Delivery: 12/14 slots delivered · missing: evening 2026-10-06")

    def test_missing_outranks_a_pending_slot(self):
        """Otherwise the normal pre-push case would swallow the defect."""
        entries = every_slot(BEFORE_MORNING, 41,
                             skip=[("2026-10-11", "morning")])
        line = delivery_ledger.delivery_line(ledger(BEFORE_MORNING, entries))
        self.assertIn("missing: morning 2026-10-11", line)


class TestStateSixEmailFallback(unittest.TestCase):
    """State 6. ntfy failed and the email fallback carried the digest."""

    def test_exact_line(self):
        entries = every_slot(AFTER_BOTH, 41)
        for item in entries:
            if (item["npt_date"], item["slot"]) == ("2026-10-07", "morning"):
                item["kind"] = delivery_ledger.KIND_NTFY_FAILED
                item["first_ntfy_sent_at"] = None
        self.assertEqual(
            delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries)),
            "Delivery: 1 slot needed the email fallback (morning 2026-10-07)")

    def test_two_fallbacks_are_counted_and_both_named(self):
        entries = every_slot(AFTER_BOTH, 41)
        for item in entries:
            if (item["npt_date"], item["slot"]) in (("2026-10-07", "morning"),
                                                    ("2026-10-10", "evening")):
                item["kind"] = delivery_ledger.KIND_NTFY_FAILED
                item["first_ntfy_sent_at"] = None
        self.assertEqual(
            delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries)),
            "Delivery: 2 slots needed the email fallback (morning 2026-10-07, "
            "evening 2026-10-10)")

    def test_a_fallback_outranks_lateness(self):
        entries = one_late(AFTER_BOTH, 760, ("2026-10-06", "evening"))
        for item in entries:
            if (item["npt_date"], item["slot"]) == ("2026-10-07", "morning"):
                item["kind"] = delivery_ledger.KIND_NTFY_FAILED
                item["first_ntfy_sent_at"] = None
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertIn("email fallback", line)
        self.assertNotIn("slots on time", line)


class TestStateSevenDuplicateSuppressed(unittest.TestCase):
    """State 7. A suffix, not an alternative: it rides on whichever line the
    rest of the ledger earned."""

    def test_exact_line(self):
        entries = every_slot(BEFORE_MORNING, 41,
                             skip=[("2026-10-09", "evening")])
        entries.append(entry("2026-10-09", "evening",
                             delivery_ledger.KIND_DUPLICATE_SUPPRESSED,
                             duplicate_of="2026-10-09T13:35:00Z"))
        self.assertEqual(
            delivery_ledger.delivery_line(ledger(BEFORE_MORNING, entries)),
            "Delivery: pushes at 07:20 and 19:20 NPT · last 7 days on time "
            "11/14 · latest 41s late · 1 push was suppressed as a duplicate")

    def test_a_suppressed_slot_is_not_counted_as_missing(self):
        entries = every_slot(AFTER_BOTH, 41,
                             skip=[("2026-10-08", "morning")])
        entries.append(entry("2026-10-08", "morning",
                             delivery_ledger.KIND_DUPLICATE_SUPPRESSED,
                             duplicate_of="2026-10-08T01:35:00Z"))
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertNotIn("missing", line)

    def test_the_suffix_appears_on_the_late_line_too(self):
        entries = one_late(AFTER_BOTH, 760, ("2026-10-06", "evening"))
        entries = [e for e in entries
                   if (e["npt_date"], e["slot"]) != ("2026-10-08", "morning")]
        entries.append(entry("2026-10-08", "morning",
                             delivery_ledger.KIND_DUPLICATE_SUPPRESSED,
                             duplicate_of="2026-10-08T01:35:00Z"))
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertIn("worst 12m 40s late (evening 2026-10-06)", line)
        self.assertTrue(line.endswith("1 push was suppressed as a duplicate"),
                        line)

    def test_two_suppressions_are_pluralised(self):
        entries = every_slot(AFTER_BOTH, 41)
        for day, slot in (("2026-10-08", "morning"), ("2026-10-10", "evening")):
            entries = [e for e in entries if (e["npt_date"], e["slot"])
                       != (day, slot)]
            entries.append(entry(day, slot,
                                 delivery_ledger.KIND_DUPLICATE_SUPPRESSED,
                                 duplicate_of="x"))
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertTrue(line.endswith("2 pushes were suppressed as a "
                                      "duplicate"), line)


# ---------------------------------------------------------------------------
# The property the whole module exists for
# ---------------------------------------------------------------------------


def one_of_every_state():
    """A ledger reaching each of the seven states, built like the tests above.

    Ordered by state number, and each pair is (label, ledger). Kept as data
    rather than as seven assertions so the property tests below can walk all
    seven in one place.
    """
    clean = ledger(AFTER_BOTH, every_slot(AFTER_BOTH, 41))
    pending = ledger(BEFORE_MORNING, every_slot(BEFORE_MORNING, 41))
    missing = ledger(BEFORE_MORNING,
                     every_slot(BEFORE_MORNING, 41,
                                skip=[("2026-10-06", "evening")]))
    late = ledger(AFTER_BOTH,
                  one_late(AFTER_BOTH, 760, ("2026-10-06", "evening")))
    fallback_entries = every_slot(AFTER_BOTH, 41)
    for item in fallback_entries:
        if (item["npt_date"], item["slot"]) == ("2026-10-07", "morning"):
            item["kind"] = delivery_ledger.KIND_NTFY_FAILED
            item["first_ntfy_sent_at"] = None
    fallback = ledger(AFTER_BOTH, fallback_entries)
    suppressed_entries = every_slot(BEFORE_MORNING, 41,
                                    skip=[("2026-10-09", "evening")])
    suppressed_entries.append(entry("2026-10-09", "evening",
                                    delivery_ledger.KIND_DUPLICATE_SUPPRESSED,
                                    duplicate_of="x"))
    suppressed = ledger(BEFORE_MORNING, suppressed_entries)
    return [("1 unreadable", None), ("2 pending", pending),
            ("3 on time", clean), ("4 late", late), ("5 missing", missing),
            ("6 fallback", fallback), ("7 duplicate", suppressed)]


class TestNeverEmpty(unittest.TestCase):
    def test_all_seven_states_render_a_non_empty_line(self):
        for label, data in one_of_every_state():
            line = delivery_ledger.delivery_line(data)
            self.assertIsInstance(line, str)
            self.assertTrue(line.strip(), "state %s rendered a blank" % label)

    def test_the_seven_states_render_seven_distinct_lines(self):
        lines = {}
        for label, data in one_of_every_state():
            line = delivery_ledger.delivery_line(data)
            self.assertNotIn(line, lines,
                             "state %s renders the same line as state %s"
                             % (label, lines.get(line)))
            lines[line] = label
        self.assertEqual(7, len(lines), sorted(lines))

    def test_junk_input_never_renders_a_blank(self):
        for junk in (None, {}, [], "", 0, 7, "junk", {"entries": "junk"},
                     {"entries": [], "as_of": "junk", "crons": []},
                     {"entries": [{"npt_date": "nope"}], "as_of": AFTER_BOTH,
                      "crons": CRONS},
                     {"entries": [{"npt_date": "2026-10-12", "slot": "morning",
                                   "kind": "delivered"}], "as_of": AFTER_BOTH,
                      "crons": CRONS}):
            line = delivery_ledger.delivery_line(junk)
            self.assertTrue(line and line.strip(), repr(junk))
            self.assertEqual(ALARM, line, repr(junk))

    def test_the_line_never_claims_to_describe_today(self):
        """The mirror of a git-committed ledger lags by hours.

        Every state is scoped to "the last 7 days" or to a dated slot, so a
        lagging copy can never be read as a claim about now.
        """
        for label, data in one_of_every_state():
            line = delivery_ledger.delivery_line(data)
            self.assertNotIn("today", line.lower(), "state %s: %s"
                             % (label, line))


# ---------------------------------------------------------------------------
# The pure helpers
# ---------------------------------------------------------------------------


class TestNptConversions(unittest.TestCase):
    def test_npt_date_is_the_nepal_calendar_day(self):
        # 18:14:59 UTC is 23:59:59 NPT; 18:15 UTC is 00:00 NPT next day.
        self.assertEqual(delivery_ledger.npt_date_from_instant(
            "2026-10-09T18:14:00Z"), "2026-10-09")
        self.assertEqual(delivery_ledger.npt_date_from_instant(
            "2026-10-09T18:14:59Z"), "2026-10-09")
        self.assertEqual(delivery_ledger.npt_date_from_instant(
            "2026-10-09T18:15:00Z"), "2026-10-10")

    def test_a_naive_instant_is_refused_not_assumed_utc(self):
        with self.assertRaises(ValueError):
            delivery_ledger.npt_date_from_instant("2026-10-09T18:15:00")

    def test_npt_offset_is_345_minutes_with_no_dst(self):
        self.assertEqual(delivery_ledger.NPT_OFFSET,
                         datetime.timedelta(hours=5, minutes=45))

    def test_slot_is_decided_on_the_nepali_clock_not_utc(self):
        self.assertEqual(delivery_ledger.slot_from_cron("5 2 * * *"), "morning")
        self.assertEqual(delivery_ledger.slot_from_cron("5 14 * * *"), "evening")
        self.assertEqual(delivery_ledger.slot_from_cron("35 1 * * *"),
                         "morning")
        self.assertEqual(delivery_ledger.slot_from_cron("35 13 * * *"),
                         "evening")
        # 18:14 UTC is 23:59 NPT and 18:15 UTC is 00:00 NPT the next day.
        # Deciding on UTC hours alone would be wrong about the boundary.
        self.assertEqual(delivery_ledger.slot_from_cron("0 18 * * *"),
                         "evening")
        self.assertEqual(delivery_ledger.slot_from_cron("14 18 * * *"),
                         "evening")

    def test_a_bad_cron_raises_rather_than_guessing_a_slot(self):
        for bad in ("", None, "* * *", "1 2 * * * *", "x 2 * * *",
                    "1 99 * * *", "1 2 * * 7"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                delivery_ledger.slot_from_cron(bad)

    def test_push_clock_is_the_nepal_wall_clock(self):
        self.assertEqual(delivery_ledger.npt_clock_from_cron("35 1 * * *"),
                         "07:20")
        self.assertEqual(delivery_ledger.npt_clock_from_cron("35 13 * * *"),
                         "19:20")
        self.assertEqual(delivery_ledger.npt_clock_from_cron("5 2 * * *"),
                         "07:50")

    def test_the_two_shipped_slots_keep_their_real_schedules(self):
        """daily-news.yml still runs 5 2 and 5 14 UTC; nothing shifted them."""
        self.assertEqual(delivery_ledger.npt_clock_from_cron("5 2 * * *"),
                         "07:50")
        self.assertEqual(delivery_ledger.npt_clock_from_cron("5 14 * * *"),
                         "19:50")
        self.assertEqual(delivery_ledger.slot_from_cron("5 2 * * *"), "morning")
        self.assertEqual(delivery_ledger.slot_from_cron("5 14 * * *"),
                         "evening")


class TestHumanise(unittest.TestCase):
    def test_seconds_minutes_and_hours_are_zero_padded_consistently(self):
        self.assertEqual(delivery_ledger.humanise_seconds(0), "0s")
        self.assertEqual(delivery_ledger.humanise_seconds(41), "41s")
        self.assertEqual(delivery_ledger.humanise_seconds(59), "59s")
        self.assertEqual(delivery_ledger.humanise_seconds(60), "1m 00s")
        self.assertEqual(delivery_ledger.humanise_seconds(125), "2m 05s")
        self.assertEqual(delivery_ledger.humanise_seconds(760), "12m 40s")
        self.assertEqual(delivery_ledger.humanise_seconds(3600), "1h 00m 00s")


class TestDeltaIsAlwaysTwoRecordedInstants(unittest.TestCase):
    def test_delta_is_sent_minus_scheduled_never_now(self):
        item = entry("2026-10-12", "morning", delivery_ledger.KIND_DELIVERED,
                     41)
        self.assertEqual(item["scheduled_instant"], "2026-10-12T01:35:00Z")
        self.assertEqual(item["first_ntfy_sent_at"], "2026-10-12T01:35:41Z")
        self.assertEqual(delivery_ledger._delta_s(item), 41.0)

    def test_a_recorded_delta_field_is_ignored(self):
        """A number somebody typed into the file must not reach the line."""
        item = entry("2026-10-12", "morning", delivery_ledger.KIND_DELIVERED,
                     41)
        item["delta_s"] = 99999
        self.assertEqual(delivery_ledger._delta_s(item), 41.0)

    def test_an_entry_without_both_instants_has_no_delta(self):
        item = {"npt_date": "2026-10-12", "slot": "morning",
                "kind": delivery_ledger.KIND_DELIVERED}
        self.assertIsNone(delivery_ledger._delta_s(item))

    def test_an_entry_without_a_delta_is_never_counted_as_on_time(self):
        entries = every_slot(AFTER_BOTH, 41,
                             skip=[("2026-10-08", "morning")])
        entries.append({"npt_date": "2026-10-08", "slot": "morning",
                        "cron": MORNING_CRON,
                        "scheduled_instant": "2026-10-08T01:35:00Z",
                        "kind": delivery_ledger.KIND_DELIVERED})
        line = delivery_ledger.delivery_line(ledger(AFTER_BOTH, entries))
        self.assertNotIn("14/14", line)
        self.assertTrue(line)


class TestPrune(unittest.TestCase):
    def _wide(self):
        entries = []
        for day in range(1, 15):
            for slot in ("morning", "evening"):
                entries.append(entry("2026-10-%02d" % day, slot,
                                     delivery_ledger.KIND_DELIVERED, 10))
        return ledger("2026-10-14T14:00:00Z", entries)

    def test_keeps_exactly_the_last_seven_nepal_days(self):
        kept = delivery_ledger.prune(self._wide())
        self.assertEqual(14, len(kept["entries"]))
        self.assertEqual(["2026-10-08", "2026-10-09", "2026-10-10",
                          "2026-10-11", "2026-10-12", "2026-10-13",
                          "2026-10-14"],
                         sorted({e["npt_date"] for e in kept["entries"]}))

    def test_prune_does_not_mutate_its_input(self):
        wide = self._wide()
        delivery_ledger.prune(wide)
        self.assertEqual(28, len(wide["entries"]))

    def test_retention_is_the_same_constant_the_line_names(self):
        self.assertEqual(delivery_ledger.RETENTION_DAYS, 7)
        line = delivery_ledger.delivery_line(
            delivery_ledger.prune(self._wide()))
        self.assertIn("last %d days" % delivery_ledger.RETENTION_DAYS, line)

    def test_prune_anchors_on_as_of_and_never_reads_the_wall_clock(self):
        """Two calls an hour apart must give the same answer."""
        wide = self._wide()
        first = delivery_ledger.prune(wide)
        second = delivery_ledger.prune(wide)
        self.assertEqual(first, second)

    def test_the_window_boundary_is_nepal_days_not_utc_days(self):
        """The window is anchored on the NEPALI day, and this discriminates.

        `as_of` is 2026-10-12T18:20Z, which is 2026-10-13 00:05 NPT -- the
        exact boundary where the UTC day and the Nepal day differ. A window
        anchored on the UTC day would cover 2026-10-06..2026-10-12; anchored
        on the Nepal day it covers 2026-10-07..2026-10-13.

        So one entry sits BELOW the Nepal window (10-06, which the UTC reading
        would keep) and one sits INSIDE it but above the UTC one (10-13, which
        the UTC reading would drop). Anchoring on either wrong day fails this,
        which is what makes it a test of the boundary rather than of a number.
        """
        entries = [entry("2026-10-06", "evening",
                         delivery_ledger.KIND_DELIVERED, 10),
                   entry("2026-10-12", "morning",
                         delivery_ledger.KIND_DELIVERED, 10),
                   entry("2026-10-13", "morning",
                         delivery_ledger.KIND_DELIVERED, 10)]
        kept = delivery_ledger.prune(ledger("2026-10-12T18:20:00Z", entries))
        self.assertEqual(["2026-10-12", "2026-10-13"],
                         sorted({e["npt_date"] for e in kept["entries"]}))


# ---------------------------------------------------------------------------
# Shipping dark: no flag, no change of any kind
# ---------------------------------------------------------------------------


class TestShipsDark(unittest.TestCase):
    def test_render_health_block_without_a_line_is_unchanged(self):
        self.assertEqual(render_digest.render_health_block(None, ""),
                         render_digest.render_health_block(None, "", ""))
        self.assertEqual(render_digest.render_health_block(None, "note"),
                         render_digest.render_health_block(None, "note", ""))

    def test_render_page_without_a_line_carries_no_delivery_markup(self):
        page = render_digest.render_page([], "morning", "2026-10-12",
                                         "2026-10-12 06:31",
                                         validated_payload("ok"))
        self.assertNotIn("Delivery:", page)
        self.assertNotIn(delivery_ledger.ALARM_UNAVAILABLE, page)
        self.assertEqual(
            page, render_digest.render_page([], "morning", "2026-10-12",
                                            "2026-10-12 06:31",
                                            validated_payload("ok"), ""))

    def test_the_flag_absent_render_is_byte_identical_to_the_baseline(self):
        """The strongest form: the same CLI invocation, byte for byte.

        `kathmandu_now_str` is frozen so the only variable left between the two
        renders is the ledger, which is what makes the comparison mean
        anything.
        """
        real_clock = render_digest.kathmandu_now_str
        render_digest.kathmandu_now_str = lambda: "2026-10-12 06:31"
        tmp = tempfile.mkdtemp(prefix="delivery-dark-")
        try:
            # A research note is carried on BOTH renders deliberately. Without
            # it the health block is empty for an `ok` payload, so the lit page
            # would CREATE the block and the dark page would have nothing to
            # compare against -- the assertion would then be about block
            # creation rather than about the delivery line. With a note already
            # in the block, the only difference between the two pages is the one
            # added <p>, which is what this test is actually for.
            payload = validated_payload("ok", notes="Checked against the "
                                                  "official TU notice board.")
            payload["stories"] = []
            story_json = write_json(os.path.join(tmp, "stories.json"), payload)
            ledger_path = os.path.join(tmp, "delivery.json")
            with io.open(ledger_path, "w", encoding="utf-8") as handle:
                json.dump(ledger(AFTER_BOTH, every_slot(AFTER_BOTH, 41)),
                          handle)
            renders = {}
            for label in ("dark", "lit"):
                out_dir = os.path.join(tmp, label, "digests")
                index = os.path.join(tmp, label, "index.html")
                argv = ["--digest-time", "morning", "--date", "2026-10-12",
                        "--email-txt", os.path.join(tmp, "absent.txt"),
                        "--stories-json", story_json,
                        "--candidates", os.path.join(tmp, "absent.json"),
                        "--out-dir", out_dir, "--index", index]
                if label == "lit":
                    argv += ["--delivery-ledger", ledger_path]
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = render_digest.main(argv)
                self.assertEqual(rc, 0, label)
                with io.open(os.path.join(out_dir, "2026-10-12-morning.html"),
                             encoding="utf-8") as handle:
                    page = handle.read()
                with io.open(index, encoding="utf-8") as handle:
                    renders[label] = (page, handle.read())

            dark_page, dark_index = renders["dark"]
            lit_page, lit_index = renders["lit"]
            # The index is shared markup with the archive and must not move.
            self.assertEqual(dark_index, lit_index)
            # Without the flag: no line, and not even the alarm.
            self.assertNotIn("Delivery:", dark_page)
            self.assertNotIn(delivery_ledger.ALARM_UNAVAILABLE, dark_page)
            # With the flag: exactly the one measured line, in the hero block.
            self.assertEqual(lit_page.count("Delivery: on time"), 1)
            self.assertIn('<div class="health-banner">', lit_page)
            # And the ONLY difference on the page is that one added element.
            # Derived from the renderer rather than hardcoded, so this asserts
            # ISOLATION OF THE CHANGE (nothing else moved) without freezing the
            # block's exact whitespace, which is the renderer's business.
            injected = '<p class="health-line">%s</p>' % delivery_ledger.delivery_line(
                delivery_ledger.load_ledger(ledger_path))
            self.assertIn(injected, lit_page)
            self.assertEqual(dark_page, lit_page.replace(injected, ""))
        finally:
            render_digest.kathmandu_now_str = real_clock
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_ledger_path_that_does_not_exist_renders_the_alarm(self):
        """State 1 applies only once the flag HAS been passed."""
        tmp = tempfile.mkdtemp(prefix="delivery-missing-")
        try:
            page = render_digest.render_page(
                [], "morning", "2026-10-12", "2026-10-12 06:31",
                validated_payload("ok"),
                delivery_ledger.delivery_line(
                    delivery_ledger.load_ledger(
                        os.path.join(tmp, "absent.json"))))
            self.assertIn(delivery_ledger.ALARM_UNAVAILABLE, page)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_alarm_renders_inside_the_hero_health_block_not_the_footer(self):
        tmp = tempfile.mkdtemp(prefix="delivery-hero-")
        try:
            page = render_digest.render_page(
                [], "morning", "2026-10-12", "2026-10-12 06:31",
                validated_payload("ok"), ALARM)
            block_start = page.index('<div class="health-banner">')
            block_end = page.index("</div>", block_start)
            self.assertIn(ALARM, page[block_start:block_end])
            footer_start = page.index("<footer>")
            self.assertNotIn(ALARM, page[footer_start:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()