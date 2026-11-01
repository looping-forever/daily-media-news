#!/usr/bin/env python3
"""scripts/repair_ledger.py -- retire a STORED false delivery claim, declaratively.

WHY THIS EXISTS
---------------
KV key `digest:ledger` holds `2026-10-26 evening` with `state=sent` and
`ntfy_sent_at=2026-10-26T12:34:19Z`. That instant precedes the slot's own
13:35Z (19:20 NPT) due moment by 1h00m41s and falls in that day's MORNING
window, so the evening slot was never served by it. daily-news.yml's
stored-ledger arm reports the contradiction on EVERY run
("LEDGER HONESTY VIOLATION (stored)") and, by design, never rewrites it.

Correcting `state/digest_slot_ledger.json` alone does not fix it. The seal
step in digest-prebuild.yml lifts `days` out of KV verbatim on every seal and
the daily step reads KV, so the mirror is a lagging copy and KV is the
original. The correction has to be made in KV, once, by an explicit human
act -- this script, dispatched through ledger-repair.yml.

DECLARATIVE AND IDEMPOTENT
--------------------------
The corrections are DECLARED in CORRECTIONS: date, slot, and the exact false
claim each one names. A declaration is applied only to a row that still
carries that claim. A row that already carries the correction, or that no
longer claims a delivery before its own slot, is reported and left alone. A
row that still looks mislabelled but does NOT match the declared shape is
REFUSED and nothing is written: rewriting a shape nobody has read would
destroy the evidence this file is about.

Nothing is deleted. The original claim is kept beside the correction
(`original_state`, `original_kind`, `original_ntfy_sent_at`), `kind` is
preserved because it classifies what was BUILT, and every other field --
`scheduled_instant`, `sequence_id`, `reject_plain`, whatever else the row
carries -- is reproduced unchanged.

A second run therefore finds nothing to match, changes nothing, and says so.
That is verified in tests, not assumed: `plan()` is called twice and the
results compared.

KV PATH
-------
Same endpoint, key, credential names and placeholder gate as the daily
workflow's own write (daily-news.yml, `KV write` in the ntfy step):

    {KV_API}/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/digest:ledger
    KV_API = 'https://api.cloudflare.com/client/v4'

The Cloudflare REST API addresses the account's KV namespace directly, so the
write is always to the same remote namespace the Worker and the daily step
read: there is no `--remote` flag to get wrong and no wrangler here (this
repository holds no wrangler credential). The namespace id comes from the
repository VARIABLE `CF_KV_NAMESPACE_ID`, never from deploy/wrangler.toml,
whose committed id is a placeholder that names no namespace.

Secrets are read by name and never printed.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import sys
import urllib.request

# --------------------------------------------------------------------------
# The KV key this repairs, and the slot table it is filed against
# --------------------------------------------------------------------------

LEDGER_KEY = 'digest:ledger'
KV_API = 'https://api.cloudflare.com/client/v4'
KV_NAMESPACE_ENV = 'CF_KV_NAMESPACE_ID'
KV_PLACEHOLDER = 'abcdefabcdefabcdefabcdefabcdefab'
KV_TIMEOUT_S = 30
SCHEMA_VERSION = 1

# Same pair daily-news.yml writes, and the same early-send slack: a send may
# precede its slot by up to 30 minutes of clock drift before it is a mislabel.
SLOT_DUE_UTC = {'morning': (1, 35), 'evening': (13, 35)}
SLOT_EARLY_SLACK_S = 30 * 60

# --------------------------------------------------------------------------
# THE DECLARED CORRECTIONS
# --------------------------------------------------------------------------

#: Each entry names ONE stored row and the exact false claim it still carries.
#:
#: `false_state` and `false_ntfy_sent_at` are the row's own fields as KV holds
#: them. A row whose `state` or instant differs is NOT the row described here,
#: so it is refused rather than rewritten -- a declaration is a statement about
#: a specific record, not permission to edit whatever is found at that slot.
#:
#: `reason` is the sentence the corrected row carries, copied verbatim from the
#: committed mirror so both records say the same thing. The numbers in it are
#: asserted against the arithmetic in tests/digest/test_repair_ledger.py.
CORRECTIONS = (
    {
        'date': '2026-10-26',
        'slot': 'evening',
        'false_state': 'sent',
        'false_ntfy_sent_at': '2026-10-26T12:34:19Z',
        'reason': (
            'delivery claimed (state=sent) while ntfy_sent_at='
            '2026-10-26T12:34:19Z precedes this slot\'s due instant '
            '2026-10-26T13:35:00Z by 1h00m41s; that instant falls in the '
            '2026-10-26 morning window, so the evening slot was never served '
            'by it'
        ),
    },
)

#: The fields a correction writes. Named once so the tests can prove nothing
#: outside this set (and the removal of `ntfy_sent_at`) changes.
CORRECTION_FIELDS = (
    'state', 'corrected', 'corrected_at', 'corrected_reason',
    'original_state', 'original_kind', 'original_ntfy_sent_at',
)

_ABSENT = object()


# --------------------------------------------------------------------------
# Instants and slots
# --------------------------------------------------------------------------


def iso(moment):
    """An aware datetime in the ledger's instant format."""
    return (moment.astimezone(datetime.timezone.utc)
            .replace(microsecond=0).isoformat().replace('+00:00', 'Z'))


def now_iso():
    return iso(datetime.datetime.now(datetime.timezone.utc))


def instant_or_none(value):
    """An instant from a ledger value, or None when it cannot be read.

    None is deliberate: an unreadable instant is not evidence of anything, and
    every caller here treats it as "no claim to judge" rather than "no send".
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def slot_due_instant(date, slot):
    """The UTC instant a slot was due on a Nepal date, or None.

    Same arithmetic as daily-news.yml's `slot_due_instant`: both real slots
    fire on the same UTC calendar day as their Nepal date (01:35Z = 07:20 NPT,
    13:35Z = 19:20 NPT), so nothing is shifted.
    """
    if slot not in SLOT_DUE_UTC:
        return None
    try:
        day = datetime.date.fromisoformat(str(date))
    except (TypeError, ValueError):
        return None
    hour, minute = SLOT_DUE_UTC[slot]
    return datetime.datetime(day.year, day.month, day.day, hour, minute,
                             tzinfo=datetime.timezone.utc)


def humanise_seconds(seconds):
    return '%dh%02dm%02ds' % (seconds // 3600, (seconds % 3600) // 60,
                              seconds % 60)


def slot_holding_instant(date, sent):
    """The (date, slot) whose window an instant falls in, or ''.

    Walked backwards from the bucket's own date, nearest slot first, so the
    report can name where a mislabelled send actually belongs instead of only
    naming the field that is wrong.
    """
    try:
        day = datetime.date.fromisoformat(str(date))
    except (TypeError, ValueError):
        return ''
    for offset in (0, 1, 2, 3):
        candidate = (day - datetime.timedelta(days=offset)).isoformat()
        for name in ('evening', 'morning'):
            due = slot_due_instant(candidate, name)
            if due is not None and sent >= due:
                return '%s %s' % (candidate, name)
    return ''


def false_claim(entry, date, slot):
    """A send that precedes its own slot, described; '' when there is none.

    The same predicate daily-news.yml reports stored rows with, kept here
    because a repair that cannot recognise the claim it is correcting cannot
    tell "already honest" from "still lying". tests/digest/test_repair_ledger.py
    executes the SHIPPED arm beside this one and asserts they agree on every
    shape in play, so the two cannot drift apart quietly.
    """
    if not isinstance(entry, dict):
        return ''
    sent = instant_or_none(entry.get('ntfy_sent_at'))
    if sent is None:
        return ''
    due = slot_due_instant(date, slot)
    if due is None:
        return ''
    if sent >= due - datetime.timedelta(seconds=SLOT_EARLY_SLACK_S):
        return ''
    gap = int((due - sent).total_seconds())
    return ('ntfy_sent_at=%s precedes the %s %s due instant %s by %s, and that '
            'instant falls in %s'
            % (iso(sent), date, slot, iso(due), humanise_seconds(gap),
               slot_holding_instant(date, sent) or 'no slot in this ledger'))


# --------------------------------------------------------------------------
# The correction itself -- pure, and applied to a copy
# --------------------------------------------------------------------------


def corrected_row(entry, declaration, now):
    """The row with its false claim retired and its original kept.

    `kind` is preserved: it classifies what was BUILT, and a fallback edition
    did build. Only `state` is the delivery verdict. `ntfy_sent_at` is removed
    because it is the false claim itself; the value moves to
    `original_ntfy_sent_at` so nothing is lost.
    """
    fixed = dict(entry)
    fixed['state'] = 'failed'
    fixed.pop('ntfy_sent_at', None)
    if entry.get('kind') is not None:
        fixed['original_kind'] = entry['kind']
    fixed['corrected'] = True
    fixed['corrected_at'] = now
    fixed['corrected_reason'] = declaration['reason']
    fixed['original_state'] = entry.get('state')
    fixed['original_ntfy_sent_at'] = entry.get('ntfy_sent_at')
    return fixed


def _row_at(ledger, date, slot):
    days = ledger.get('days')
    if not isinstance(days, dict):
        return None
    slots = days.get(date)
    if not isinstance(slots, dict):
        return None
    row = slots.get(slot)
    return row if isinstance(row, dict) else None


def _describe(row):
    return 'state=%r ntfy_sent_at=%r' % (row.get('state'),
                                         row.get('ntfy_sent_at'))


def plan(ledger, declarations=CORRECTIONS, now=None):
    """Return (new_ledger, report). Neither argument is mutated.

    Every declaration lands in exactly one bucket:

      corrected -- the row carried the declared claim; it is in `new_ledger`.
      already   -- already corrected, or it no longer claims an early send.
      absent    -- there is no such row; the violation was never present.
      refused   -- a row exists and still looks wrong, but it is not the row
                   the declaration describes. Nothing is edited for it.
    """
    if not isinstance(ledger, dict) or not isinstance(ledger.get('days'), dict):
        raise ValueError('the ledger is not an object with a `days` object')
    now = now or now_iso()
    new = copy.deepcopy(ledger)
    report = {'corrected': [], 'already': [], 'absent': [], 'refused': [],
              'untouched': 0}

    for declaration in declarations:
        date, slot = declaration['date'], declaration['slot']
        row = _row_at(new, date, slot)
        if row is None:
            report['absent'].append({'date': date, 'slot': slot})
            continue
        if row.get('corrected') is True:
            report['already'].append({
                'date': date, 'slot': slot,
                'why': 'already carries the correction written at %s'
                       % (row.get('corrected_at') or 'an unrecorded instant'),
            })
            continue
        if not false_claim(row, date, slot):
            report['already'].append({
                'date': date, 'slot': slot,
                'why': 'carries no delivery claim that precedes its own slot',
            })
            continue
        if (row.get('state') != declaration['false_state']
                or row.get('ntfy_sent_at') != declaration['false_ntfy_sent_at']):
            report['refused'].append({
                'date': date, 'slot': slot,
                'why': ('the row still looks mislabelled but is %s, not the '
                        'declared state=%r ntfy_sent_at=%r'
                        % (_describe(row), declaration['false_state'],
                           declaration['false_ntfy_sent_at'])),
            })
            continue
        fixed = corrected_row(row, declaration, now)
        new['days'][date][slot] = fixed
        report['corrected'].append({'date': date, 'slot': slot,
                                    'before': row, 'after': fixed})

    changed = {(item['date'], item['slot']) for item in report['corrected']}
    report['untouched'] = sum(
        len(slots) for date, slots in new['days'].items()
        if isinstance(slots, dict) for _slot in slots
        if (date, _slot) not in changed)
    return new, report


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render(value):
    if value is _ABSENT:
        return '<absent>'
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def row_diff(before, after):
    """The changed fields of ONE row, as '-' / '+' lines.

    Only the affected row is ever printed: the row is the record under
    discussion, and dumping the ledger into a public log would publish delivery
    history that has nothing to do with this repair.
    """
    lines = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key, _ABSENT)
        new = after.get(key, _ABSENT)
        if old == new:
            continue
        lines.append('  - %s: %s' % (key, render(old)))
        lines.append('  + %s: %s' % (key, render(new)))
    return lines


def summary_line(report):
    return ('rows corrected=%d, rows already honest=%d, rows absent=%d, '
            'rows refused=%d, rows untouched=%d'
            % (len(report['corrected']), len(report['already']),
               len(report['absent']), len(report['refused']),
               report['untouched']))


# --------------------------------------------------------------------------
# KV transport -- the same endpoint and credentials the daily step uses
# --------------------------------------------------------------------------


def kv_target():
    """(account, namespace, token, '') or (None, None, None, reason).

    The namespace id is validated, not merely read: the committed placeholder
    in deploy/wrangler.toml is a valid-looking 32-hex string that names no
    namespace, so accepting it would PUT nowhere and report success.
    """
    token = (os.environ.get('CF_LEDGER_TOKEN') or '').strip()
    account = (os.environ.get('CF_ACCOUNT_ID') or '').strip()
    raw = (os.environ.get(KV_NAMESPACE_ENV) or '').strip()
    missing = [name for name, value in (('CF_LEDGER_TOKEN', token),
                                        ('CF_ACCOUNT_ID', account),
                                        (KV_NAMESPACE_ENV, raw)) if not value]
    if missing:
        return None, None, None, ('%s unset; nothing was read and nothing was '
                                  'written' % ' / '.join(missing))
    if len(raw) != 32 or any(c not in '0123456789abcdefABCDEF' for c in raw):
        return None, None, None, ('%s is not a 32-hex namespace id; nothing was '
                                  'read and nothing was written'
                                  % KV_NAMESPACE_ENV)
    if raw.lower() == KV_PLACEHOLDER:
        return None, None, None, ('%s holds the deploy/wrangler.toml placeholder, '
                                  'which names no namespace; nothing was read and '
                                  'nothing was written' % KV_NAMESPACE_ENV)
    return account, raw, token, ''


def kv_url(account, namespace):
    return ('%s/accounts/%s/storage/kv/namespaces/%s/values/%s'
            % (KV_API, account, namespace, LEDGER_KEY))


def kv_error_detail(exc):
    """(HTTP status, Cloudflare errors[0].code, printable label).

    The body is read ONCE and the label never quotes the request URL, which
    carries the account and namespace ids.
    """
    status = getattr(exc, 'code', None)
    code = None
    if status is not None:
        try:
            payload = json.loads((exc.read() or b'').decode('utf-8', 'replace'))
            code = (payload.get('errors') or [{}])[0].get('code')
        except Exception:
            code = None
    printable = (('HTTP %s, Cloudflare code %s' % (status, code or 'none'))
                 if status is not None else exc.__class__.__name__)
    return status, code, printable


def kv_read():
    """(ledger, '') or (None, reason). Never raises.

    An absent key (404 / Cloudflare 10009) is answered as an EMPTY ledger: the
    key has never been written, so no declared violation can be in it, and
    answering "unknown" would make the truthful "never present" impossible.
    Every other failure is unknown, and unknown must never be written over.
    """
    account, namespace, token, reason = kv_target()
    if reason:
        return None, reason
    request = urllib.request.Request(
        kv_url(account, namespace),
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'text/plain'})
    try:
        with urllib.request.urlopen(request, timeout=KV_TIMEOUT_S) as response:
            raw = response.read().decode('utf-8', errors='replace')
    except Exception as exc:
        status, code, detail = kv_error_detail(exc)
        if status == 404 and code == 10009:
            print('the KV ledger key is absent (HTTP 404, Cloudflare 10009) -- '
                  'an empty ledger')
            return {'schema_version': SCHEMA_VERSION, 'days': {}}, ''
        return None, 'the KV ledger could not be read (%s)' % detail
    if not raw.strip():
        return {'schema_version': SCHEMA_VERSION, 'days': {}}, ''
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, 'the KV ledger is not JSON'
    if not isinstance(parsed, dict):
        return None, 'the KV ledger is a %s, not an object' % type(parsed).__name__
    if not isinstance(parsed.get('days'), dict):
        return None, 'the KV ledger carries no `days` object'
    return parsed, ''


def kv_write(ledger):
    """(True, '') or (False, reason). Never raises."""
    account, namespace, token, reason = kv_target()
    if reason:
        return False, reason
    request = urllib.request.Request(
        kv_url(account, namespace),
        data=json.dumps(ledger, ensure_ascii=False).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + token,
                 'Content-Type': 'application/json',
                 'Accept': 'text/plain'},
        method='PUT')
    try:
        with urllib.request.urlopen(request, timeout=KV_TIMEOUT_S) as response:
            return True, 'HTTP %s' % response.status
    except Exception as exc:
        _, _, detail = kv_error_detail(exc)
        return False, detail


def verify(expected, readback, declarations=CORRECTIONS):
    """Every problem the read-back has against what was written.

    The strongest claim this can make is total: the value KV now holds is
    EXACTLY the ledger this script computed, so no other row and no other
    field of the corrected row moved. The read-back is also re-checked against
    the mislabel predicate, because a correction that leaves the row still
    reporting a violation has not fixed anything.
    """
    problems = []
    if readback is None:
        return ['the ledger could not be read back, so nothing is proven']
    for declaration in declarations:
        date, slot = declaration['date'], declaration['slot']
        row = _row_at(readback, date, slot)
        if row is None:
            problems.append('%s %s is absent from the read-back' % (date, slot))
            continue
        if not row.get('corrected'):
            problems.append('%s %s carries no `corrected` marker' % (date, slot))
        stale = false_claim(row, date, slot)
        if stale:
            problems.append('%s %s STILL reports a violation: %s'
                            % (date, slot, stale))
    if readback != expected:
        differing = []
        for declaration in declarations:
            date, slot = declaration['date'], declaration['slot']
            before = _row_at(expected, date, slot) or {}
            after = _row_at(readback, date, slot) or {}
            differing.extend(row_diff(before, after))
        if differing:
            problems.append('the read-back differs from what was written:\n'
                            + '\n'.join(differing))
        else:
            problems.append('the read-back differs from what was written outside '
                            'the declared rows, so another writer changed the '
                            'ledger, or the write did not land as computed')
    return problems


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Retire the declared false delivery claims in KV '
                    'digest:ledger.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true',
                      help='read, plan and print; never PUT (the default)')
    mode.add_argument('--write', action='store_true',
                      help='apply the declared corrections and verify the '
                           'read-back')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or [])
    write = bool(args.write)

    ledger, reason = kv_read()
    if ledger is None:
        print('FATAL: %s. Nothing was written.' % reason)
        return 1
    try:
        expected, report = plan(ledger, CORRECTIONS, now_iso())
    except ValueError as exc:
        print('FATAL: %s. Nothing was written.' % exc)
        return 1

    print('KV %s -- %d declared correction(s), mode: %s'
          % (LEDGER_KEY, len(CORRECTIONS), 'write' if write else 'dry run'))
    print(summary_line(report))

    for item in report['already']:
        print('already honest: %s %s -- %s'
              % (item['date'], item['slot'], item['why']))
    for item in report['absent']:
        print('absent: %s %s -- the row is not in KV, so the declared violation '
              'was never present (already corrected, or never recorded)'
              % (item['date'], item['slot']))
    for item in report['refused']:
        print('REFUSED: %s %s -- %s' % (item['date'], item['slot'], item['why']))

    for item in report['corrected']:
        print('diff %s %s' % (item['date'], item['slot']))
        for line in row_diff(item['before'], item['after']):
            print(line)

    if report['refused']:
        print('FATAL: at least one declared row does not match the shape this '
              'file describes. NOTHING was written: rewriting a row nobody has '
              'read is how the evidence this repair is about gets destroyed.')
        return 2
    if not write:
        print('dry run: nothing was written. `corrected_at` above is the instant '
              'a write would stamp; set dry_run=false to write.')
        return 0
    if not report['corrected']:
        print('nothing to write: every declared row is already honest.')
        return 0

    ok, detail = kv_write(expected)
    if not ok:
        print('FATAL: the KV write failed (%s). Nothing else was attempted.' % detail)
        return 1
    print('KV %s written (%s)' % (LEDGER_KEY, detail))

    readback, reason = kv_read()
    if readback is None:
        print('verification: FAILED -- %s' % reason)
        return 3
    problems = verify(expected, readback, CORRECTIONS)
    if problems:
        print('verification: FAILED')
        for problem in problems:
            print('  %s' % problem)
        return 3
    print('verification: OK -- the read-back carries the declared state=failed, '
          'keeps the original claim in original_*, and every other row and field '
          'is unchanged')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
