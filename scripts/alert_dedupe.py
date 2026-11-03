#!/usr/bin/env python3
"""alert_dedupe.py -- one alert per failure condition per 6h, plus one recovery.

WHY THIS FILE EXISTS
--------------------
Between 07:35 and 10:30 on 2026-10-27 the same alert fired six times for one
unchanged cause: "AI ladder returned no verdict". Nothing was different between
those six messages -- same broken classifier, same un-judged headlines -- and the
same shape produces a cascade for "evening digest could not be built" (one per
retry attempt) and "never arrived" (one per watchdog run). An alert that repeats
while the cause has not changed teaches the owner to stop reading it, and the
seventh page -- the one that matters -- is the one that gets ignored.

SO: THE CHANNEL IS NOT TOUCHED. The ntfy topic, the secrets and the message
titles stay exactly as they are. This module only answers "should THIS run
post?" and it keeps the answer in a committed JSON file, which is the state
mechanism the repository already uses (state/ every workflow already commits).
No new service, no new channel, no new secret.

THE CONDITION KEY (this is the part that must not be got wrong)
---------------------------------------------------------------
    key = condition id | slot | date bucket

  * Condition id names the FAILURE, not the run: ``breaking.honesty_gate``,
    ``digest.broken``, ``digest.missing``. Two different failures can therefore
    never be merged into one suppressed stream -- they never share a key.
  * Slot and date bucket are what make two genuinely different digest failures
    separate: a broken morning digest and a broken evening digest are two
    conditions, and so are the same slot on two different days.
  * NOTHING about the individual run is in the key. The retry attempt number and
    the run id are deliberately absent, because including either would split one
    condition into one entry per run -- the flood this module exists to stop.

THE WINDOW
----------
The first occurrence of a condition alerts immediately. Further occurrences are
suppressed for ``WINDOW_HOURS`` (6.0). When the window closes, the next
occurrence alerts again, summarising EVERY run since the previous alert: the
count, the first and last occurrence timestamps, and the same underlying cause
text. Windows do not overlap, so the counts in consecutive alerts add up to the
total number of affected runs -- a run is never counted twice and never dropped.
So six failures over three hours arrive as one message that says "6 runs,
07:35 -> 10:30", which is more useful than six identical ones.

RECOVERY
--------
``resolve`` is called when the caller OBSERVES that the condition has stopped
(the ladder produced a verdict, the digest arrived with stories, a slot is no
longer missing). It returns a recovery ONCE and deletes the entry, so a second
call is a no-op. Recovery is signal-driven, never absence-driven: a condition is
not declared healthy merely because it did not run recently, since that is the
same "a blank must never read like good news" rule the rest of this repository
follows.

HYSTERESIS
----------
A single healthy observation is not proof that an outage is over: on the
breaking path one quiet, un-judged run used to clear a genuinely broken ladder,
and the observed result was open/recover/open on alternate runs. Callers that
have a real judgement signal pass ``required=HEALTHY_STREAK_REQUIRED`` (2), and
then a recovery needs that many CONSECUTIVE healthy calls. Any failure recorded
in between resets the streak to zero. The default of 1 keeps the original
one-call contract for callers with no such signal (the digest arms).

Stdlib only.
"""

import datetime
import json
import os

__all__ = [
    "WINDOW_HOURS",
    "KEEP_HOURS",
    "MAX_CONDITIONS",
    "HEALTHY_STREAK_REQUIRED",
    "LEDGER_PATH",
    "condition_key",
    "load",
    "save",
    "record",
    "resolve",
    "resolve_prefix",
    "describe_window",
    "recovery_title",
    "recovery_body",
    "human_duration",
    "QUIET_HOURS_START",
    "QUIET_HOURS_END",
    "QUIET_QUEUE_CAP",
    "QUIET_QUEUE_PATH",
    "QUIET_BATCH_MAX_BYTES",
    "now_utc",
    "npt",
    "in_quiet_hours",
    "quiet_until",
    "held_notice",
    "quiet_item",
    "load_quiet_queue",
    "save_quiet_queue",
    "enqueue_quiet",
    "render_quiet_batches",
    "clear_quiet_items",
    "hold_if_quiet",
    "QUIET_FLUSH_OWNER",
    "QUIET_FLUSH_LEASE",
    "QUIET_FLUSH_LEASE_SECONDS",
    "claim_flush",
    "release_flush",
]

#: How long one condition is silenced after it has alerted.
WINDOW_HOURS = 6.0

#: Consecutive healthy observations a caller with a real judgement signal
#: requires before a recovery is announced. One quiet run is not evidence.
HEALTHY_STREAK_REQUIRED = 2

#: Entries untouched for a week are pruned. A week is far longer than any
#: window here, so pruning can never resurrect a flood.
KEEP_HOURS = 24.0 * 7

#: Hard cap on the ledger, so a mis-keyed condition cannot grow it without end.
MAX_CONDITIONS = 200

#: The committed ledger. state/ is already committed by every workflow here.
LEDGER_PATH = os.path.join("state", "alert_ledger.json")

SCHEMA_VERSION = 1

_EMPTY = {"schema_version": SCHEMA_VERSION, "conditions": {}}


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _utc(moment=None):
    """A timezone-aware UTC datetime. Naive input is read as UTC."""
    if moment is None:
        moment = datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def _stamp(moment):
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(text):
    """A stamp this module wrote, or None. Never raises."""
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def human_duration(seconds):
    """'3h 12m', '45s'. Read by a human, so no decimal places."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "unknown"
    if total < 0:
        total = 0
    if total < 60:
        return "%ds" % total
    minutes = total // 60
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if hours else "%dd" % days


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def condition_key(condition_id, slot="", date=""):
    """`condition id | slot | date bucket`.

    An empty slot or date still occupies its position, so a key can never
    collide with a key that has the other one set: ``digest.missing||``,
    ``digest.missing|morning|`` and ``digest.missing|morning|2026-10-27`` are
    three different conditions.
    """
    parts = ["", "", ""]
    for index, value in enumerate((condition_id, slot, date)):
        parts[index] = "" if value is None else str(value).strip()
    if not parts[0]:
        raise ValueError("condition_id is required: an empty id would merge "
                         "every failure into one condition")
    return "|".join(parts)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def load(path=LEDGER_PATH):
    """Read the ledger. Any problem yields an EMPTY one, never an exception.

    Empty is the safe direction: the next occurrence of a condition alerts
    immediately. Failing loud here would be worse -- a ledger that cannot be
    read must not take the alerting path down with it.
    """
    state = {"schema_version": SCHEMA_VERSION, "conditions": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    conditions = raw.get("conditions")
    if isinstance(conditions, dict):
        state["conditions"] = {str(k): v for k, v in conditions.items()
                               if isinstance(v, dict)}
    return state


def _prune(state, now, keep_hours):
    """Drop entries untouched for `keep_hours`, then cap the count."""
    conditions = state["conditions"]
    cutoff = _utc(now) - datetime.timedelta(hours=float(keep_hours))
    removed = 0
    for key in list(conditions):
        seen = _parse_stamp(conditions[key].get("last_seen"))
        if seen is None or seen <= cutoff:
            del conditions[key]
            removed += 1
    if len(conditions) > MAX_CONDITIONS:
        oldest = sorted(conditions,
                        key=lambda k: conditions[k].get("last_seen") or "")
        for key in oldest[:len(conditions) - MAX_CONDITIONS]:
            del conditions[key]
            removed += 1
    return removed


def save(state, path=LEDGER_PATH, now=None, keep_hours=KEEP_HOURS):
    """Prune, then write atomically. Returns the number of entries pruned."""
    now = _utc(now)
    removed = _prune(state, now, keep_hours)
    state["schema_version"] = SCHEMA_VERSION
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return removed


# ---------------------------------------------------------------------------
# Occurrence -> decision
# ---------------------------------------------------------------------------


def record(state, key, now, cause, window_hours=WINDOW_HOURS):
    """Register one occurrence of `key` and decide whether to alert.

    Returns a dict with:
      alert      True when this run must post.
      runs       runs affected in the window this alert summarises.
      first/last first and last occurrence timestamps in that window.
      cause      the underlying cause text, verbatim.
      suppressed runs that were folded into this message without their own post.
    """
    now = _utc(now)
    stamp = _stamp(now)
    cause = str(cause or "").strip()
    conditions = state.setdefault("conditions", {})
    entry = conditions.get(key)

    if not isinstance(entry, dict):
        entry = {
            "cause": cause,
            "runs": 1,
            "first_seen": stamp,
            "last_seen": stamp,
            "window_first": stamp,
            "window_last": stamp,
            "window_runs": 1,
            "alerted_at": stamp,
        }
        conditions[key] = entry
        decision = {"alert": True, "key": key, "runs": 1, "first": stamp,
                    "last": stamp, "cause": cause, "suppressed": 0,
                    "window_hours": float(window_hours)}
        _close_window(entry, stamp)
        return decision

    entry["runs"] = int(entry.get("runs") or 0) + 1
    entry["last_seen"] = stamp
    # A failure breaks the healthy run: recovery must be N CONSECUTIVE healthy
    # observations, so one bad run in between resets the count to zero.
    entry["healthy_streak"] = 0
    if cause:
        entry["cause"] = cause

    # The occurrence that triggers an alert is counted in THAT alert's window,
    # and the counter is reset afterwards, so consecutive windows never share a
    # run.
    if not entry.get("window_first"):
        entry["window_first"] = stamp
    entry["window_runs"] = int(entry.get("window_runs") or 0) + 1
    entry["window_last"] = stamp

    alerted_at = _parse_stamp(entry.get("alerted_at"))
    due = alerted_at is None or \
        (now - alerted_at) >= datetime.timedelta(hours=float(window_hours))

    if not due:
        # `runs` is the number of occurrences since the last alert, so the
        # caller can print the real count instead of a constant 1. `suppressed`
        # is the same number here: none of these runs posted on its own.
        runs = int(entry.get("window_runs") or 1)
        return {"alert": False, "key": key, "runs": runs,
                "first": entry.get("window_first") or stamp, "last": stamp,
                "cause": entry.get("cause", ""), "suppressed": runs,
                "window_hours": float(window_hours)}

    runs = int(entry.get("window_runs") or 1)
    decision = {"alert": True, "key": key, "runs": runs,
                "first": entry.get("window_first") or stamp,
                "last": entry.get("window_last") or stamp,
                "cause": entry.get("cause", ""), "suppressed": runs - 1,
                "window_hours": float(window_hours)}
    _close_window(entry, stamp)
    return decision


def _close_window(entry, stamp):
    """Start the next window at this run, with an empty count."""
    entry["window_first"] = ""
    entry["window_last"] = ""
    entry["window_runs"] = 0
    entry["alerted_at"] = stamp


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


_NO_RECOVERY = {"recovery": False}


def resolve(state, key, now, required=1):
    """The condition behind `key` stopped. Recovery, exactly once.

    With ``required`` > 1 the caller must call this on that many CONSECUTIVE
    healthy observations before the recovery is announced; the count is kept on
    the entry, so it survives the run boundary. Any failure recorded in between
    resets it (see ``record``). Until the streak is met the entry stays put and
    the return value says how far along it is.

    When the recovery is finally announced the entry is deleted, so a later
    call for the same key is a no-op and the message cannot be sent twice.
    """
    now = _utc(now)
    required = max(1, int(required or 1))
    conditions = state.setdefault("conditions", {})
    entry = conditions.get(key)
    if not isinstance(entry, dict):
        return dict(_NO_RECOVERY)
    streak = int(entry.get("healthy_streak") or 0) + 1
    if streak < required:
        entry["healthy_streak"] = streak
        return {"recovery": False, "key": key, "healthy_streak": streak,
                "healthy_required": required}
    conditions.pop(key, None)
    first = _parse_stamp(entry.get("first_seen"))
    last = _parse_stamp(entry.get("last_seen"))
    seconds = (now - first).total_seconds() if first else 0
    return {"recovery": True, "key": key,
            "runs": int(entry.get("runs") or 0),
            "first": entry.get("first_seen", ""),
            "last": entry.get("last_seen", ""),
            "duration_seconds": seconds,
            "healthy_runs": streak,
            "cause": entry.get("cause", "")}


def resolve_prefix(state, prefix, now, keep_key=None, required=1):
    """Resolve every entry under `prefix` except `keep_key`.

    Used where the caller knows the condition class is over but not which date
    or slot it belonged to (a digest that arrived carries today's slot; the
    entry that alerted may be yesterday's).
    """
    conditions = state.setdefault("conditions", {})
    resolved = []
    for key in sorted(conditions):
        if not key.startswith(prefix) or key == keep_key:
            continue
        recovery = resolve(state, key, now, required=required)
        if recovery.get("recovery"):
            resolved.append(recovery)
    return resolved


# ---------------------------------------------------------------------------
# Message text -- the wording the owner reads
# ---------------------------------------------------------------------------


def describe_window(decision, window_hours=WINDOW_HOURS):
    """The window paragraph appended to an alert body."""
    runs = int(decision.get("runs") or 0)
    if runs <= 1:
        return ("This is the first run affected. Further occurrences of this "
                "same condition will not alert again for %gh."
                % float(window_hours))
    return ("%d runs were affected between %s and %s (same underlying cause). "
            "This is ONE alert for that window; further occurrences will not "
            "alert again for %gh."
            % (runs, decision.get("first", "?"), decision.get("last", "?"),
               float(window_hours)))


def recovery_title(label):
    return "%s recovered" % label


def recovery_body(label, recovery):
    """One clear recovery message: what stopped, how many runs, how long."""
    runs = int(recovery.get("runs") or 0)
    lasted = human_duration(recovery.get("duration_seconds"))
    cause = str(recovery.get("cause") or "").strip()
    body = ("%s is working again: the condition that was alerting has stopped "
            "occurring. %d run(s) were affected between %s and %s (%s)."
            % (label, runs, recovery.get("first") or "?", recovery.get("last") or "?",
               lasted))
    healthy = int(recovery.get("healthy_runs") or 0)
    if healthy > 1:
        # The rule is stated where it changes what the owner reads: this
        # "all clear" was not a single quiet run, it was a measured streak.
        body += (" Confirmed by %d consecutive healthy runs; a single quiet "
                 "run is never treated as a recovery." % healthy)
    if cause:
        body += "\n\nLast recorded cause: %s" % cause
    return body


# ---------------------------------------------------------------------------
# Quiet hours -- the owner's sleep window, and the held-overnight queue
# ---------------------------------------------------------------------------
#
# A NOTIFICATION AT 03:00 IS NOT A NOTIFICATION, it is an alarm. Everything this
# repository sends is read on a phone at night, so a night-time buzz has to mean
# "this genuinely cannot wait"; otherwise the seventh buzz -- the one that
# matters -- is the one that gets ignored. Held, never dropped: the item waits in
# a committed JSON file and rides ONE labelled batch after quiet hours end.
#
# THE WINDOW IS THE OWNER'S WALL CLOCK, NOT UTC. 22:00-07:00 Asia/Kathmandu is
# UTC+05:45 with no DST, so the window is computed from a real zone rather than a
# fixed UTC offset. zoneinfo needs a tz database, which a bare Windows box does
# not have, so the fixed offset is the FALLBACK -- named, and correct for Nepal
# specifically (no DST since 1986).
#
# CATCH-UP IS DELIBERATE: an alert raised at 21:59 that is still being rendered
# at 22:01 is held, and an item held at 06:59 is delivered at 07:0x. The window
# decides by the hour of the run, not by how urgent the item's wording is. The
# only exception is a genuine CRITICAL alert (breaking_monitor.is_critical_alert),
# because "you may already be dead" is not something to schedule for breakfast.

QUIET_HOURS_START = 22   # 22:00 NPT, inclusive
QUIET_HOURS_END = 7      # 07:00 NPT, exclusive

_NPT_OFFSET = datetime.timedelta(hours=5, minutes=45)
_NPT_NAME = "Asia/Kathmandu"

#: The committed queue. state/ is already committed by every alerting workflow.
QUIET_QUEUE_PATH = os.path.join("state", "quiet_queue.json")

#: A cap WITH A STATED BEHAVIOUR: over the cap the OLDEST held item is dropped,
#: counted, and NAMED in the next batch. An unbounded queue would grow all night
#: on one flapping condition, and a silent drop would be a lost alert.
QUIET_QUEUE_CAP = 20

#: Each batch is chunked by size, and cleared as each chunk is delivered, so a
#: queue that will not fit one push is never truncated into silence.
QUIET_BATCH_MAX_BYTES = 2800

QUIET_SCHEMA_VERSION = 1


def _quiet_zone():
    """Asia/Kathmandu, falling back to the same offset when tzdata is absent."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(_NPT_NAME)
    except Exception:
        return datetime.timezone(_NPT_OFFSET, _NPT_NAME)


def now_utc():
    """The current UTC instant, or the one pinned in ALERT_DEDUPE_NOW_UTC.

    The env seam exists so quiet-hours behaviour is TESTABLE at 23:00 and at
    07:00 without waiting for a clock, and so a test cannot quietly become a lie
    when CI happens to run at night. Test/diagnostic only: no workflow sets it.
    """
    raw = (os.environ.get("ALERT_DEDUPE_NOW_UTC") or "").strip()
    pinned = _parse_stamp(raw) if raw else None
    return pinned or datetime.datetime.now(datetime.timezone.utc)


def npt(moment=None):
    """The same instant on the owner's wall clock."""
    return _utc(moment).astimezone(_quiet_zone())


def in_quiet_hours(moment=None):
    """True inside 22:00-07:00 NPT (22:00 is in, 07:00 is out)."""
    hour = npt(moment).hour
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def quiet_until(moment=None):
    """The NPT instant quiet hours next end (tomorrow, when raised after 22:00)."""
    local = npt(moment)
    end = local.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_HOURS_START:
        end = end + datetime.timedelta(days=1)
    return end


def held_notice(count, moment=None):
    """The one line a held run says, including when the item will arrive."""
    return "quiet hours, held %d item(s) until %s NPT" % (
        int(count), quiet_until(moment).strftime("%H:%M"))


def quiet_item(title, body, condition, kind="breaking", priority=3, tags=None,
               now=None):
    """One held item. `condition` is what supersession is keyed on."""
    moment = _utc(now)
    return {
        "condition": str(condition or "").strip(),
        "kind": str(kind or "breaking"),
        "title": str(title or "").strip(),
        "body": str(body or "").strip(),
        "priority": int(priority),
        "tags": [str(t) for t in (tags or [])],
        "raised_at": _stamp(moment),
        "raised_npt": npt(moment).strftime("%Y-%m-%d %H:%M"),
    }


def load_quiet_queue(path=QUIET_QUEUE_PATH):
    """Read the queue. Any problem yields an EMPTY one, exactly like the ledger.

    Empty is the safe direction: the next held item is enqueued normally. The
    one thing that must never happen is a queue that cannot be read taking the
    alerting path down with it.
    """
    doc = {"schema_version": QUIET_SCHEMA_VERSION, "items": [],
           "superseded": 0, "dropped": 0, "dropped_titles": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return doc
    if not isinstance(raw, dict):
        return doc
    items = raw.get("items")
    if isinstance(items, list):
        doc["items"] = [i for i in items
                        if isinstance(i, dict) and i.get("condition")]
    for field in ("superseded", "dropped"):
        try:
            doc[field] = max(0, int(raw.get(field) or 0))
        except (TypeError, ValueError):
            doc[field] = 0
    titles = raw.get("dropped_titles")
    if isinstance(titles, list):
        doc["dropped_titles"] = [str(t) for t in titles]
    return doc


def save_quiet_queue(doc, path=QUIET_QUEUE_PATH):
    """Prune nothing, drop nothing: write exactly what was given, atomically."""
    out = {
        "schema_version": QUIET_SCHEMA_VERSION,
        "items": list(doc.get("items") or []),
        "superseded": int(doc.get("superseded") or 0),
        "dropped": int(doc.get("dropped") or 0),
        "dropped_titles": list(doc.get("dropped_titles") or []),
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return out


def enqueue_quiet(path, item, cap=QUIET_QUEUE_CAP):
    """Hold one item and return the resulting document.

    SAME CONDITION REPLACES the earlier item -- the latest state of a thing wins,
    the same rule the ledger uses -- so a held recovery and a later failure can
    never both be delivered and contradict each other. Over the cap the OLDEST is
    dropped, counted, and named in the next batch.
    """
    doc = load_quiet_queue(path)
    items = doc["items"]
    condition = item.get("condition")
    kept = [i for i in items if i.get("condition") != condition]
    if len(kept) != len(items):
        doc["superseded"] += 1
    kept.append(item)
    if len(kept) > int(cap):
        overflow = len(kept) - int(cap)
        dropped = kept[:overflow]
        doc["dropped"] += overflow
        doc["dropped_titles"].extend(
            str(d.get("title") or "(untitled)") for d in dropped)
        kept = kept[overflow:]
    doc["items"] = kept
    save_quiet_queue(doc, path)
    return doc


def _quiet_block(item):
    """One item as two lines: when it was raised, and what it said."""
    body = " ".join(str(item.get("body") or "").split())
    if len(body) > 240:
        body = body[:239].rstrip() + "\u2026"
    block = "%s NPT — %s" % (item.get("raised_npt") or "?",
                             item.get("title") or "(untitled)")
    return ("%s\n%s" % (block, body)) if body else block


def render_quiet_batches(doc, max_bytes=QUIET_BATCH_MAX_BYTES):
    """[{'title','message','conditions'}] -- the morning batch, size-bounded.

    Whole items are packed, never split, and a queue too large for one push
    becomes several numbered batches instead of a truncated one. The counts and
    the cap/supersession notes sit at the HEAD of the first batch, where a
    truncation cannot reach them.
    """
    items = list(doc.get("items") or [])
    if not items:
        return []
    budget = max(600, int(max_bytes) - 800)
    chunks, current, size = [], [], 0
    for item in items:
        block = _quiet_block(item)
        width = len(block.encode("utf-8")) + 2
        if current and size + width > budget:
            chunks.append(current)
            current, size = [], 0
        current.append((block, item.get("condition") or ""))
        size += width
    if current:
        chunks.append(current)

    notes = []
    if int(doc.get("superseded") or 0):
        notes.append("%d earlier held item(s) were superseded by a later report "
                     "on the same thing, so only the latest state is shown."
                     % int(doc["superseded"]))
    if int(doc.get("dropped") or 0):
        names = "; ".join((doc.get("dropped_titles") or [])[:10])
        notes.append("queue cap %d: dropped the %d oldest held item(s) before "
                     "this batch (%s)." % (QUIET_QUEUE_CAP,
                                           int(doc["dropped"]),
                                           names or "titles not recorded"))

    batches = []
    for index, chunk in enumerate(chunks, 1):
        head = ["Held by quiet hours so they would not wake you. Delivered "
                "once, now that quiet hours are over."]
        if index == 1:
            head.extend(notes)
        head.append("")
        title = "Held overnight — %d item(s) from quiet hours" % len(items)
        if len(chunks) > 1:
            title = "Held overnight — %d item(s), part %d of %d" % (
                len(items), index, len(chunks))
        batches.append({
            "title": title,
            "message": "\n".join(head + [block for block, _c in chunk]),
            "conditions": [c for _b, c in chunk if c],
        })
    return batches


def clear_quiet_items(path, conditions):
    """Remove only the conditions that were actually delivered."""
    doc = load_quiet_queue(path)
    delivered = set(c for c in (conditions or []) if c)
    doc["items"] = [i for i in doc["items"]
                    if i.get("condition") not in delivered]
    if not doc["items"]:
        doc["superseded"] = 0
        doc["dropped"] = 0
        doc["dropped_titles"] = []
    save_quiet_queue(doc, path)
    return doc


def hold_if_quiet(kind, title, body, condition, priority=4, tags=None,
                  now=None, path=QUIET_QUEUE_PATH):
    """THE ONE QUIET-HOURS DECISION every notifier in this repository shares.

    Returns None when the caller may notify now, or a note when the notice has
    been HELD into the queue -- in which case the caller MUST NOT push, because
    the item already rides the 07:00 batch.

    The honesty gate, the live canary and the digest watchdog all come through
    here, so the window, the cap and the supersession rule exist exactly once.
    Re-implementing "is it night?" in a workflow is how two of them would drift.
    """
    moment = _utc(now) if now is not None else now_utc()
    if not in_quiet_hours(moment):
        return None
    doc = enqueue_quiet(path, quiet_item(title, body, condition, kind=kind,
                                         priority=priority, tags=tags,
                                         now=moment))
    return held_notice(len(doc["items"]), moment)


# ---------------------------------------------------------------------------
# One delivery owner for the morning batch
# ---------------------------------------------------------------------------
#
# THE BATCH LEAVES FROM EXACTLY ONE PLACE: the start of main() in
# scripts/breaking_monitor.py, which the 07:05 NPT run reaches every day. Any
# other caller is REFUSED, not served -- two workflows that both drained the
# queue would send the same held items twice and each would clear the other's
# work.
#
# IF THAT ONE RUN DOES NOT HAPPEN (the workflow is disabled, its schedule is
# removed, or the job is skipped) NOTHING IS LOST: the items stay in
# state/quiet_queue.json, which is committed, so the next run that does happen
# delivers them late rather than not at all. A missed 07:05 is a delay, never a
# drop.
#
# A scheduled run and a manual dispatch CAN overlap, so the deliverer takes a
# lease file created with O_EXCL; the second one sees it and does nothing. The
# lease expires after QUIET_FLUSH_LEASE_SECONDS so a crashed run cannot wedge
# delivery for good.

QUIET_FLUSH_OWNER = "breaking_monitor"
QUIET_FLUSH_LEASE = os.path.join("state", "quiet_queue.lease")
QUIET_FLUSH_LEASE_SECONDS = 1800


def claim_flush(now=None, path=QUIET_FLUSH_LEASE):
    """Take the delivery lease. True when THIS process may deliver the batch."""
    moment = _utc(now) if now is not None else now_utc()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                held = _parse_stamp((fh.read() or "").strip())
        except (OSError, ValueError):
            held = None
        fresh = held and (moment - held).total_seconds() < QUIET_FLUSH_LEASE_SECONDS
        if fresh:
            return False
        try:
            os.remove(path)      # a crashed run's stale lease: take it over
        except OSError:
            return False
        return claim_flush(moment, path)
    except OSError:
        return False
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_stamp(moment) + "\n")
    return True


def release_flush(path=QUIET_FLUSH_LEASE):
    """Give the lease back. Safe to call when it was never taken."""
    try:
        os.remove(path)
        return True
    except OSError:
        return False
