#!/usr/bin/env bash
# May this notice wake the owner right now?
#
#   exit 0 -> yes: it is daytime, the caller pushes.
#   exit 1 -> no: the notice has been HELD into state/quiet_queue.json and the
#             07:00 breaking-monitor run delivers it. The caller MUST NOT push.
#
# The window, the cap, the supersession rule and the queue all live in
# scripts/alert_dedupe.py. This file only carries that one answer into bash, so a
# workflow never re-implements "is it night?" and cannot drift from the monitor.
#
# Usage: bash scripts/may-notify.sh <kind> <title> <body> <condition> [priority]
set -u
exec python3 - "$@" <<'PY'
import sys

sys.path.insert(0, "scripts")
import alert_dedupe

if len(sys.argv) < 5:
    print("may-notify: kind, title, body and condition are required")
    raise SystemExit(2)
kind, title, body, condition = sys.argv[1:5]
priority = int(sys.argv[5]) if len(sys.argv) > 5 else 4
note = alert_dedupe.hold_if_quiet(kind, title, body, condition,
                                  priority=priority)
if note is None:
    raise SystemExit(0)
print("may-notify: %s" % note)
raise SystemExit(1)
PY
