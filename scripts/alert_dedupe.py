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

Stdlib only.
"""

import datetime
import json
import os

__all__ = [
    "WINDOW_HOURS",
    "KEEP_HOURS",
    "MAX_CONDITIONS",
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
]

#: How long one condition is silenced after it has alerted.
WINDOW_HOURS = 6.0

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
        return {"alert": False, "key": key, "runs": 0, "first": "", "last": stamp,
                "cause": entry.get("cause", ""), "suppressed": 1,
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


def resolve(state, key, now):
    """The condition behind `key` stopped. Recovery, exactly once.

    The entry is deleted, so a second call for the same key is a no-op and the
    recovery message cannot be sent twice.
    """
    now = _utc(now)
    conditions = state.setdefault("conditions", {})
    entry = conditions.pop(key, None)
    if not isinstance(entry, dict):
        return dict(_NO_RECOVERY)
    first = _parse_stamp(entry.get("first_seen"))
    last = _parse_stamp(entry.get("last_seen"))
    seconds = (now - first).total_seconds() if first else 0
    return {"recovery": True, "key": key,
            "runs": int(entry.get("runs") or 0),
            "first": entry.get("first_seen", ""),
            "last": entry.get("last_seen", ""),
            "duration_seconds": seconds,
            "cause": entry.get("cause", "")}


def resolve_prefix(state, prefix, now, keep_key=None):
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
        recovery = resolve(state, key, now)
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
    if cause:
        body += "\n\nLast recorded cause: %s" % cause
    return body
