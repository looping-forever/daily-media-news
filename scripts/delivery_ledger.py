#!/usr/bin/env python3
"""scripts/delivery_ledger.py -- punctuality of digest delivery, measured.

WHAT THIS IS
------------
A ledger of push attempts, and ONE health line rendered from it. The line
exists because "the digest is late" is a fact about the world that no page
can currently state: the digest is built, published and pushed, and if the
ntfy send landed 12 minutes late the reader has no way to know.

THE RULE THAT MAKES EVERY NUMBER HERE TRUTHFUL
----------------------------------------------
`delta_s` is ALWAYS `first_ntfy_sent_at - scheduled_instant`.

Never `datetime.now()` at write time. Those are different quantities: a
`now()`-at-write delta measures "how long the writer took to reach this
line", which is fast when the code is fast and says nothing about when the
push happened. Only the two recorded instants measure punctuality, so only
they are used, and the difference of them is what is rendered.

Consequences that are deliberate, not accidents:

  * A LATE digest is rendered as late. The number is printed verbatim. It is
    never rounded away, softened, dropped below a threshold, or replaced with
    "recently". A health line that hides its own worst number is worse than no
    health line, because it is trusted.
  * A send BEFORE its slot is rendered as early, and never as on time: a
    negative delta is a mislabel, not punctuality, and the Worker already
    reports `on_time: false` for it.
  * Nothing here infers, judges or softens. There is no language model, no
    heuristic and no "probably" anywhere in this file. Every value rendered is
    read out of the ledger or computed by arithmetic from two recorded
    instants.
  * NO STATE EVER RENDERS AN EMPTY STRING. Absent, truncated, corrupt or
    history-free data renders the ALARM in state 1. A blank line is the silent
    failure this module exists to prevent: a reader cannot distinguish "no
    problems" from "nothing was measured", and only one of those is good news.

WHY THE LINE NEVER CLAIMS TO DESCRIBE TODAY
-------------------------------------------
The ledger is mirrored through git, so the copy a rendered page reads can lag
the real pushes by hours. Every phrase below is therefore scoped to what was
actually measured -- "the last 7 days", a specific `(slot date)` -- and never
to "today". A footer asserting today's punctuality from a lagging mirror
would be a lie wearing a measurement's clothes.

SHIPPING DARK
-------------
Nothing in the digest pipeline calls this module yet. `render_digest.py`
accepts an OPTIONAL `--delivery-ledger`, and when it is not passed nothing on
the page changes by a single byte. That is the point of the flag: the module
can be complete and correct before any page is allowed to depend on it.
"""

import datetime

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: A push is "on time" when it went out within this many seconds of its
#: scheduled instant. A NAMED constant, not a literal buried in a comparison,
#: because the rendered line is read against it: a reader who wants to know
#: what "on time" means has one number to look up.
ON_TIME_S = 60

#: How many Nepal days of history are kept. `prune` enforces it, and the
#: rendered line names it, so the window and the claim cannot disagree.
RETENTION_DAYS = 7

#: Two pushes a day: the morning and the evening digest.
SLOTS_PER_DAY = 2

#: Nepal is UTC+05:45 with no DST, so every conversion in this repository is a
#: fixed offset. Same constant, same reason, as NPT_OFFSET_MINUTES in
#: tests/workflows/test_workflow_lint.py.
NPT_OFFSET = datetime.timedelta(hours=5, minutes=45)

#: The two slots, in push order.
SLOTS = ("morning", "evening")

#: An NPT hour below this is the morning slot; at or above it, the evening.
#: 12:00 NPT is the midpoint between 07:50 and 19:50, so no real schedule is
#: near enough to the boundary for this to be a judgement call.
SLOT_SPLIT_NPT_HOUR = 12

#: State 1. The only line in this module that is not a measurement, and it is
#: a measurement too: it reports that no measurement could be taken.
ALARM_UNAVAILABLE = ("⚠️ Delivery history unavailable — this digest may have "
                     "arrived late.")

#: The kinds an entry may carry. `delivered`, `partial` and `unsummarised` all
#: mean A DIGEST WAS PUSHED for that slot; they differ in what was in it, and
#: the edition's own label says which on the page. Punctuality measures pushes,
#: so all three count. `unavailable` is a notice and is not a delivery.
KIND_DELIVERED = "delivered"
KIND_PARTIAL = "partial"
KIND_UNSUMMARISED = "unsummarised"
KIND_NTFY_FAILED = "ntfy_failed"
KIND_UNAVAILABLE = "unavailable"
KIND_DUPLICATE_SUPPRESSED = "duplicate_suppressed"

#: The kinds that mean a digest reached the owner's phone for its slot.
PUSHED_KINDS = (KIND_DELIVERED, KIND_PARTIAL, KIND_UNSUMMARISED)


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def _parse_instant(text):
    """Parse an ISO-8601 UTC instant into an aware datetime.

    Aware on purpose. A naive datetime plus a fixed offset is how a
    punctuality module ends up disagreeing with the clock by five and three
    quarter hours, and the disagreement is invisible in the output.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty instant")
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    moment = datetime.datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        raise ValueError("instant %r carries no UTC offset" % (text,))
    return moment.astimezone(datetime.timezone.utc)


def _format_instant(moment):
    return moment.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def npt_date_from_instant(instant):
    """The Nepal calendar date an instant falls on.

    UTC+05:45, no DST, so this is a fixed shift and an exact answer. At
    18:20 UTC the Nepal date is already tomorrow, which is precisely the
    boundary a hand-computed date gets wrong and a ledger cannot.
    """
    return (_parse_instant(instant) + NPT_OFFSET).date().isoformat()


def _npt_shift(moment):
    return moment.astimezone(datetime.timezone.utc) + NPT_OFFSET


def _cron_fields(cron):
    """Split a cron into (minute, hour) after rejecting anything non-daily.

    Only daily crons are accepted, and a day-of-week or day-of-month
    restriction is REFUSED rather than ignored. `expected_slots` owes a slot
    for every day in the window, so a weekday-only schedule read as if it
    fired daily would invent deliveries that never happened -- the same class
    of defect as deriving "what should have happened" from "what is recorded".
    """
    fields = str(cron or "").split()
    if len(fields) != 5:
        raise ValueError("not a 5-field cron: %r" % (cron,))
    if fields[2:] != ["*", "*", "*"]:
        raise ValueError("not a daily cron: %r" % (cron,))
    try:
        minute = int(fields[0])
        hour = int(fields[1])
    except ValueError:
        raise ValueError("non-numeric cron field: %r" % (cron,))
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        raise ValueError("cron field out of range: %r" % (cron,))
    return minute, hour


def slot_from_cron(cron):
    """The slot a cron expression fires: "morning" or "evening".

    Decided on the NEPALI wall clock, never the UTC hour. A schedule change
    that moves the morning push across the 18:15 UTC boundary (12:00 NPT)
    must move the slot with it, and deciding on UTC hours would not.
    """
    minute, hour = _cron_fields(cron)
    npt_hour = (_npt_shift(datetime.datetime(2000, 1, 1, hour, minute,
                                            tzinfo=datetime.timezone.utc))
                .hour)
    return "morning" if npt_hour < SLOT_SPLIT_NPT_HOUR else "evening"


def npt_clock_from_cron(cron):
    """The NPT wall clock a cron fires at, as "HH:MM".

    Rendered into the state-2 line, so it is DERIVED from the ledger's own
    declared schedule rather than hardcoded. A hardcoded "07:20" would put a
    number on the page that no schedule has to agree with.
    """
    minute, hour = _cron_fields(cron)
    shifted = _npt_shift(datetime.datetime(2000, 1, 1, hour, minute,
                                           tzinfo=datetime.timezone.utc))
    return "%02d:%02d" % (shifted.hour, shifted.minute)


def _scheduled_instant(npt_date, cron):
    """The UTC instant a daily cron fires on a given Nepal date.

    Cron fields are UTC -- that is what GitHub Actions runs them in -- so the
    Nepal date and the UTC date are the SAME calendar day for a schedule that
    fires between 18:15 UTC (00:00 NPT) and 18:14 UTC. Both real slots sit
    there: 01:35 UTC is 07:20 NPT the same day, 13:35 UTC is 19:20 NPT the
    same day. Nothing is shifted, because there is nothing to undo.
    """
    minute, hour = _cron_fields(cron)
    day = datetime.date.fromisoformat(str(npt_date))
    return datetime.datetime(day.year, day.month, day.day, hour, minute,
                             tzinfo=datetime.timezone.utc)


def humanise_seconds(seconds):
    """Seconds as "41s" / "2m 05s" / "12m 40s" / "1h 03m 20s".

    Minutes and seconds are zero-padded so a column of durations lines up and
    a reader comparing two of them is comparing digits, not guessing where a
    number ends.
    """
    total = int(round(abs(float(seconds))))
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dm %02ds" % (total // 60, total % 60)
    return "%dh %02dm %02ds" % (total // 3600, (total // 60) % 60,
                                total % 60)


def _describe_delta(seconds):
    """A delta as words: "41s late", or "1m 00s early" when it is negative.

    `humanise_seconds` takes the absolute value, so printing "%s late" straight
    from a negative delta called a pre-slot send "1m late" -- the opposite of
    what happened. The direction is decided here, once.
    """
    if seconds < 0:
        return "%s early" % humanise_seconds(seconds)
    return "%s late" % humanise_seconds(seconds)


# --------------------------------------------------------------------------
# Ledger reading
# --------------------------------------------------------------------------


def load_ledger(path):
    """Read a ledger from disk. Returns None when it cannot be read.

    None is the state-1 trigger, not an error to raise: a missing or corrupt
    file is a thing the health line has an opinion about, and raising here
    would turn a reportable fact into a crashed render.
    """
    import json
    import os

    try:
        if not path or not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("entries"), list):
        return None
    return data


def _as_of(ledger):
    """The instant the ledger was last written. Required.

    Without it "which slots were due yet" is unanswerable, and guessing it
    from the newest entry would let a ledger that stopped being written look
    like one that is merely quiet.
    """
    return _parse_instant(ledger.get("as_of"))


def _declared_crons(ledger):
    """The schedule the ledger declares, in push order, validated."""
    crons = ledger.get("crons")
    if not isinstance(crons, list) or not crons:
        raise ValueError("ledger declares no crons")
    seen = []
    for cron in crons:
        if not isinstance(cron, str):
            raise ValueError("cron is not a string: %r" % (cron,))
        seen.append(cron)
    seen.sort(key=lambda c: _scheduled_instant("2000-01-01", c))
    return seen


def _delta_s(entry):
    """first_ntfy_sent_at - scheduled_instant, in seconds. Never `now()`.

    The whole point of the module is this subtraction; see the module
    docstring. An entry missing either instant has no delta and is not
    counted as on time.
    """
    sent = entry.get("first_ntfy_sent_at")
    scheduled = entry.get("scheduled_instant")
    if not sent or not scheduled:
        return None
    return (_parse_instant(sent) - _parse_instant(scheduled)).total_seconds()


def _slot_label(entry):
    """How a specific slot is named in the line: "(evening 2026-10-06)"."""
    return "%s %s" % (entry.get("slot") or "?", entry.get("npt_date") or "?")


def _entries(ledger, window_days, today):
    """The ledger's entries whose Nepal date falls inside the window."""
    first = datetime.date.fromisoformat(today) - datetime.timedelta(
        days=window_days - 1)
    out = []
    for entry in ledger.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        try:
            day = datetime.date.fromisoformat(str(entry.get("npt_date")))
        except (TypeError, ValueError):
            continue
        if first <= day <= datetime.date.fromisoformat(today):
            out.append(entry)
    out.sort(key=lambda e: str(e.get("scheduled_instant") or ""))
    return out


def expected_slots(ledger, window_days=None, today=None):
    """Every slot the schedule owes in the window, newest last.

    Entries are NOT the source of this list. Deriving "what should have
    happened" from "what is recorded" would make a missed push invisible,
    which is the single most important thing this module has to be able to
    say.
    """
    window_days = RETENTION_DAYS if window_days is None else window_days
    as_of = _as_of(ledger)
    crons = _declared_crons(ledger)
    end = datetime.date.fromisoformat(
        today or npt_date_from_instant(_format_instant(as_of)))
    slots = []
    for back in range(window_days):
        day = end - datetime.timedelta(days=back)
        for cron in crons:
            slots.append({
                "npt_date": day.isoformat(),
                "cron": cron,
                "scheduled_instant": _scheduled_instant(day.isoformat(), cron),
            })
    slots.sort(key=lambda s: s["scheduled_instant"])
    return slots


# --------------------------------------------------------------------------
# The line
# --------------------------------------------------------------------------


def delivery_line(ledger):
    """The punctuality health line. Exactly one of seven states.

    Precedence, most urgent first, chosen so each state is reachable and no
    state can be shadowed by a milder one:

      1  the ledger cannot be read, or holds no usable delivery at all
      5  a slot that was due never produced a push
      6  a slot's ntfy send failed and the email fallback carried it
      2  this run's own slot has not gone out yet (the normal pre-push case)
      4  a slot that DID go out was late, or went out before its own slot
      3  every slot went out, and on time (every delta in [0, ON_TIME_S])

    State 7 is not in that order because it is not an alternative to any of
    them: a suppressed duplicate is appended to whichever line the ledger
    otherwise earned.

    `ledger` is whatever `load_ledger` returned; None, a non-dict, a
    truncated file and a history-free file all reach state 1.
    """
    try:
        return _delivery_line(ledger)
    except Exception:
        # The module's contract is that this function always returns a line,
        # and an unexpected shape in the data is exactly the case where a
        # silent blank would be most damaging. An alarm is the honest answer.
        return ALARM_UNAVAILABLE


def _delivery_line(ledger):
    if not isinstance(ledger, dict):
        return ALARM_UNAVAILABLE
    entries_raw = ledger.get("entries")
    if not isinstance(entries_raw, list):
        return ALARM_UNAVAILABLE

    as_of = _as_of(ledger)
    today = npt_date_from_instant(_format_instant(as_of))
    crons = _declared_crons(ledger)
    slots = expected_slots(ledger, RETENTION_DAYS, today)

    entries = _entries(ledger, RETENTION_DAYS, today)
    # An entry with no usable scheduled_instant cannot be placed on the
    # timeline, so no delta can be computed for it and no slot can be matched
    # to it. Reading past it would report the rest of the window as clean while
    # silently discarding the one record that could contradict it, so an
    # unplaceable entry is an untrustworthy ledger and the alarm says so.
    for entry in entries:
        if not entry.get("scheduled_instant"):
            return ALARM_UNAVAILABLE
        try:
            _parse_instant(entry["scheduled_instant"])
        except ValueError:
            return ALARM_UNAVAILABLE
    delivered = [e for e in entries if e.get("kind") in PUSHED_KINDS]
    by_instant = {}
    for entry in entries:
        if entry.get("scheduled_instant"):
            by_instant[str(entry["scheduled_instant"])] = entry

    # A slot is PENDING when its scheduled instant has not arrived yet. It is
    # neither delivered nor missing: it has not had its turn. Counting it as
    # missing would report an outage for every pre-push render, which trains
    # the reader to ignore the line.
    pending = [s for s in slots
               if s["scheduled_instant"] > as_of
               and _format_instant(s["scheduled_instant"]) not in by_instant]

    # MISSING is the real defect: due, and no delivery recorded at all. An
    # entry that exists but carries `unavailable` has nothing to show for its
    # slot either, so it lands here too.
    missing = []
    for slot in slots:
        key = _format_instant(slot["scheduled_instant"])
        if slot["scheduled_instant"] > as_of:
            continue
        entry = by_instant.get(key)
        if entry is None or entry.get("kind") == KIND_UNAVAILABLE:
            missing.append("%s %s" % (slot_from_cron(slot["cron"]),
                                      slot["npt_date"]))

    failed = [e for e in entries if e.get("kind") == KIND_NTFY_FAILED]
    suppressed = [e for e in entries
                  if e.get("kind") == KIND_DUPLICATE_SUPPRESSED]

    # A send BEFORE its own slot is EARLY, not on time: it cannot belong to a
    # slot that had not begun, and the Worker already reports on_time=false for
    # it. Counting it here made the page say "14/14 on time" while `?health=1`
    # said false for the same entry.
    on_time = [e for e in delivered
               if _delta_s(e) is not None and 0 <= _delta_s(e) <= ON_TIME_S]
    late = [e for e in delivered
            if _delta_s(e) is not None and _delta_s(e) > ON_TIME_S]
    early = [e for e in delivered
             if _delta_s(e) is not None and _delta_s(e) < 0]

    total = len(slots)

    if not delivered and not failed and not suppressed:
        # History-free: no delivery has ever been measured. "0/14 on time" and
        # an empty string would both be false comfort.
        return ALARM_UNAVAILABLE

    if missing:
        line = "Delivery: %d/%d slots delivered · missing: %s" % (
            len(delivered), total, ", ".join(missing))
    elif failed:
        names = ", ".join(_slot_label(e) for e in failed)
        line = "Delivery: %d %s the email fallback (%s)" % (
            len(failed), "slot needed" if len(failed) == 1
            else "slots needed", names)
    elif pending:
        times = " and ".join(npt_clock_from_cron(c) for c in crons)
        latest = max(delivered, key=lambda e: str(e["scheduled_instant"]))
        line = ("Delivery: pushes at %s NPT · last %d days on time %d/%d "
                "· latest %s" % (times, RETENTION_DAYS, len(on_time),
                                 total, _describe_delta(_delta_s(latest))))
    elif late or early:
        # Lateness outranks earliness when both exist: lateness is the harm
        # this line exists to report. With only early sends, the largest
        # pre-slot margin is the one named.
        pool = late or early
        worst = max(pool, key=lambda e: abs(_delta_s(e)))
        latest = max(delivered, key=lambda e: str(e["scheduled_instant"]))
        line = ("Delivery: %d/%d slots on time · worst %s (%s) "
                "· latest %s" % (len(on_time), total,
                                 _describe_delta(_delta_s(worst)),
                                 _slot_label(worst),
                                 _describe_delta(_delta_s(latest))))
    else:
        worst = max(delivered, key=lambda e: _delta_s(e))
        line = ("Delivery: on time for the last %d days (%d/%d slots, "
                "worst %s late)" % (RETENTION_DAYS, len(on_time), total,
                                    humanise_seconds(_delta_s(worst))))

    if suppressed:
        count = len(suppressed)
        line += " · %d %s suppressed as a duplicate" % (
            count, "push was" if count == 1 else "pushes were")
    return line


# --------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------


def prune(ledger, today=None):
    """The ledger with everything older than the retention window removed.

    Anchored on `today`, or on the ledger's own `as_of` when not given, so
    pruning never reads the wall clock: the same ledger and the same argument
    always produce the same result, which is what makes it testable.

    Returns a NEW ledger. The input is not mutated, so a caller can prune and
    still compare against what it had.
    """
    if not isinstance(ledger, dict):
        return ledger
    anchor = today or npt_date_from_instant(_format_instant(_as_of(ledger)))
    kept = _entries(ledger, RETENTION_DAYS, anchor)
    out = dict(ledger)
    out["entries"] = kept
    return out