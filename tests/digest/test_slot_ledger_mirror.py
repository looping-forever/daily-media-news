#!/usr/bin/env python3
"""tests/digest/test_slot_ledger_mirror.py — U8: the git mirror of the delivery ledger.

WHAT IS BEING GUARDED
---------------------
`state/last_digest_sent.json` records ONE slot and is overwritten by the next
digest run. That single fact is why `digest-watchdog.yml` could never judge the
slot a >=12h-delayed firing displaced: by the time the evening run started, the
morning slot was gone from the marker whether or not it had been delivered, so
"delivered in the earlier slot" and "never delivered at all" were the same bytes.

U8 adds `state/digest_slot_ledger.json` — the same schema as the Worker's
`digest:ledger`, last 7 NPT days x 2 slots — and makes the watchdog judge the
displaced slot against it.

THE FOUR CLAIMS THIS SUITE PROVES, EACH BY EXECUTING REAL CODE
--------------------------------------------------------------
1. The watchdog no longer re-attributes a >=12h-delayed firing's displaced slot
   to the wrong one: at 16:01 UTC it judges BOTH slots, and a missing morning is
   named as `morning`, never as `evening`.
2. The mirror is written FROM the KV ledger the seal job read, plus the run's own
   build outcomes. Never hand-merged: the delivery half is lifted verbatim, and
   the merge demonstrably refuses to overwrite a KV value.
3. A KV/git disagreement resolves to KV WINNING, and the disagreement is logged.
4. The footer never claims to describe today given the mirror's lag.

PLUS THE U6/U7 GUARDS, re-asserted here so U8 cannot have broken them.

WHAT IS NOT HERE, ON PURPOSE
----------------------------
No re-implementation of any decision. The workflows' own step bodies are
extracted with `heredoc_bodies` and `exec`'d, with `urllib.request.urlopen`
poisoned, so neither Cloudflare nor GitHub is reached and no credential is
needed. A suite that re-derived the merge could agree with itself forever; these
execute the bytes that ship.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_TESTS)
#: A Path, because every path below is joined onto it and `str / str` is a
#: TypeError rather than a path. Built once so a missing repo root fails here
#: rather than inside a dozen tests.
ROOT = Path(_ROOT)
for _p in (_TESTS, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests import workflows as W  # noqa: E402
from tests.workflows.test_workflow_lint import heredoc_bodies  # noqa: E402

sys.path.insert(0, os.path.join(_ROOT, "scripts"))

WATCHDOG = "digest-watchdog.yml"
PREBUILD = "digest-prebuild.yml"
DAILY = "daily-news.yml"

MARKER_STEP = "Check delivery marker"
MIRROR_STEP = "Mirror the delivery ledger into git"
COMMIT_MIRROR_STEP = "Commit the delivery ledger mirror"
COMMIT_STATE_STEP = "Commit state changes"

#: Supplied through the CF_KV_NAMESPACE_ID variable. Built from two halves so no
#: 32-hex literal sits beside a NAMESPACE_ID name (test_deploy_config_ids.py).
NAMESPACE_ID = "0123456789abcdef" * 2
#: The id committed in deploy/wrangler.toml: a PLACEHOLDER that names no
#: namespace. The harness writes it on disk to prove both steps ignore it.
KV_PLACEHOLDER = "abcdefabcdefabcdefabcdefabcdefab"
ACCOUNT_ID = "acc0000000000000000000000000000ab"
TOKEN = "kv-token-not-a-real-secret"

#: The Worker's own constant. Pinned below against deploy/cloudflare-worker.js,
#: because a second copy in a test is a second place to be wrong.
WORKER_LEDGER_KEY = "digest:ledger"


def step_dict(workflow, step_name):
    for _job, name, sd in W.iter_all_steps(workflow):
        if name == step_name:
            return sd
    raise AssertionError("%s has no step %r" % (workflow, step_name))


def step_bodies(workflow, step_name):
    """Every Python heredoc in a step's `run:`, in order."""
    run = step_dict(workflow, step_name)["run"]
    return [body for _tag, body in heredoc_bodies(run)]


def step_block_text(workflow, step_name):
    """The step's RAW YAML block: its comments AND its `run:`.

    A rule that lives only in a comment is invisible to `step_dict()["run"]`,
    and a rule that lives only in code is invisible to a reader skimming the
    diff. The direction of truth is deliberately in BOTH, so the tests that
    assert it is documented must be able to see the whole block, not half of it.
    """
    text = W.raw_workflow_text(workflow)
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "- name: %s" % step_name:
            start = index
            break
    if start is None:
        raise AssertionError("%s has no step named %r in its raw text"
                             % (workflow, step_name))
    indent = len(lines[start]) - len(lines[start].lstrip(" "))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index]
        if not stripped.strip() or stripped.lstrip(" ").startswith("#"):
            continue
        here = len(stripped) - len(stripped.lstrip(" "))
        if here <= indent:
            end = index
            break
    return "\n".join(lines[start:end])


def first_body(workflow, step_name):
    bodies = step_bodies(workflow, step_name)
    if not bodies:
        raise AssertionError(
            "step %r in %s has no Python heredoc. The step was renamed or its body "
            "replaced; update this suite deliberately rather than deleting it."
            % (step_name, workflow))
    return bodies[0]


class _Poisoned:
    """Records every urlopen call and returns a scripted body.

    NO NETWORK IS POSSIBLE from here: any attempt to reach an unrouted URL
    raises, so a step that grew a real fetch fails the suite instead of
    silently reaching Cloudflare from a developer's machine.
    """

    def __init__(self):
        self.requests = []
        self.bodies = {}
        self.raise_for = set()

    def __call__(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        self.requests.append(url)
        for fragment in self.raise_for:
            if fragment in url:
                raise OSError("network disabled by the test harness: %s" % url)
        body = self.bodies.get("default")
        if body is None:
            raise OSError("network disabled by the test harness: %s" % url)
        return _FakeResponse(body)


class _FakeResponse:
    def __init__(self, body):
        self._body = body.encode("utf-8")
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def exec_body(body, env, cwd, urlopen=None):
    """Run a step body with urlopen poisoned. Returns (code, stdout).

    `urlopen` lets a caller supply its own stub — the KV-reading steps need one
    that returns a specific ledger. The DEFAULT poisons it, so a step that
    grows an unrouted fetch fails loudly instead of reaching Cloudflare from a
    developer's machine.
    """
    saved_environ = dict(os.environ)
    saved_urlopen = urllib.request.urlopen
    os.environ.update({k: str(v) for k, v in env.items()})
    urllib.request.urlopen = urlopen if urlopen is not None else _Poisoned()
    buf = io.StringIO()
    code = 0
    cwd_before = os.getcwd()
    try:
        os.chdir(str(cwd))
        with contextlib.redirect_stdout(buf):
            try:
                exec(compile(body, "<step>", "exec"), {"__name__": "step"})
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except BaseException as exc:  # noqa: BLE001 - a crash IS the result
                code = 1
                print("%s: %s" % (type(exc).__name__, exc))
    finally:
        os.chdir(cwd_before)
        os.environ.clear()
        os.environ.update(saved_environ)
        urllib.request.urlopen = saved_urlopen
    return code, buf.getvalue()


def fixed_urlopen(payload):
    """A urlopen that always returns `payload` and records every URL."""
    seen = []

    def _call(request, *args, **kwargs):
        seen.append(getattr(request, "full_url", request))
        return _FakeResponse(payload)

    _call.seen = seen
    return _call


def outputs_of(path):
    """Parse a GITHUB_OUTPUT file into a dict (last value wins, as Actions does)."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if "=" in line:
                key, _sep, value = line.partition("=")
                out[key.strip()] = value.strip()
    return out


def mirror_doc(as_of="2026-10-09T02:10:00Z", days=None):
    return {"schema_version": 1, "as_of": as_of, "days": days or {}}


def entry(slot_date, slot, sent_at="2026-10-09T01:35:12Z", kind="delivered", **extra):
    base = {
        "state": "sent",
        "kind": kind,
        "sequence_id": "dmn-%s-%s" % (slot_date, slot),
        "ntfy_sent_at": sent_at,
        "first_ntfy_sent_at": sent_at,
        "scheduled_instant": "%sT01:35:00Z" % slot_date,
        "ntfy_failed": False,
    }
    base.update(extra)
    return base


class Harness(unittest.TestCase):
    """A temp workspace laid out like a checkout, with the marker + mirror."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="slot-ledger-")
        self.tmp = Path(self._tmp)
        self.addCleanup(shutil.rmtree, self._tmp, True)
        (self.tmp / "state").mkdir()
        (self.tmp / "deploy").mkdir()
        (self.tmp / ".github" / "workflows").mkdir(parents=True)
        # The watchdog reads BOTH committed crons; without them the step fails
        # closed, which is correct behaviour and useless for these tests.
        shutil.copy(ROOT / ".github" / "workflows" / DAILY,
                    self.tmp / ".github" / "workflows" / DAILY)
        shutil.copy(ROOT / ".github" / "workflows" / WATCHDOG,
                    self.tmp / ".github" / "workflows" / WATCHDOG)
        (self.tmp / "deploy" / "wrangler.toml").write_text(
            '\ufeffname = "breaking-monitor"\n'
            'main = "cloudflare-worker.js"\n\n'
            '[[kv_namespaces]]\nbinding = "KV"\nid = "%s"\n' % KV_PLACEHOLDER,
            encoding="utf-8")
        self.outputs = self.tmp / "gh_output.txt"
        self.outputs.write_text("", encoding="utf-8")
        self.summary = self.tmp / "summary.md"
        self.summary.write_text("", encoding="utf-8")

    def write_marker(self, **fields):
        base = {"date": "2026-10-09", "time": "evening", "stories": 9,
                "delivered": True, "channels": {"ntfy": True, "website": True}}
        base.update(fields)
        (self.tmp / "state" / "last_digest_sent.json").write_text(
            json.dumps(base, indent=2), encoding="utf-8")

    def write_mirror(self, doc):
        (self.tmp / "state" / "digest_slot_ledger.json").write_text(
            json.dumps(doc, indent=2), encoding="utf-8")

    def run_watchdog(self, now_utc, event_schedule="0 4,16 * * *",
                     event_name="schedule", cf=None):
        env = {
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_WORKSPACE": str(self.tmp),
            "WATCHDOG_NOW_UTC": now_utc,
            "EVENT_NAME": event_name,
            "EVENT_SCHEDULE": event_schedule,
            "TZ": "UTC",
        }
        if cf is None:
            cf = {"CF_LEDGER_TOKEN": "", "CF_ACCOUNT_ID": ""}
        env.update(cf)
        body = first_body(WATCHDOG, MARKER_STEP)
        return exec_body(body, env, self.tmp)


# =====================================================================
# Claim 1 — the displaced slot is judged, and judged as ITSELF
# =====================================================================


class DisplacedSlotJudgedTests(Harness):
    """A >=12h-delayed firing no longer leaves its displaced slot unjudged."""

    def test_a_missing_morning_is_judged_as_morning_not_evening(self):
        """THE CORE RESIDUAL.

        16:01 UTC is past the 16:00 firing, so `most_recent_firing` attributes
        this run to the EVENING slot. Before U8 the morning digest -- genuinely
        absent from the ledger -- was judged by nothing at all. Now it is judged,
        and it is judged under its own name.
        """
        self.write_marker(date="2026-10-09", time="evening", delivered=True)
        # A CURRENT mirror (as_of well past the morning due time) with NO
        # morning entry and no evening entry either: nothing was pushed.
        self.write_mirror(mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {"evening": entry("2026-10-09", "evening")},
        }))
        code, out = self.run_watchdog("2026-10-09T16:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        # The evening slot delivered (the marker says so), the morning did not.
        self.assertEqual("true", got["miss"],
                         "the displaced morning slot went unjudged again:\n" + out)
        self.assertEqual("morning", got["slot"],
                         "the miss was attributed to the wrong slot:\n" + out)
        self.assertEqual("2026-10-09", got["today"])
        self.assertEqual("MISSING", got["displaced_verdict"])
        self.assertIn("Watchdog (displaced slot): morning digest for 2026-10-09",
                      out)

    def test_a_delivered_morning_is_not_reported_missing(self):
        """The other half, and the one that matters daily.

        If the displaced check called a delivered morning slot MISSING, the
        watchdog would page the owner every single evening. That is the exact
        false alarm this repository has already paid for twice, so it is a
        first-class assertion rather than a footnote.
        """
        self.write_marker(date="2026-10-09", time="evening", delivered=True)
        self.write_mirror(mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {
                "morning": entry("2026-10-09", "morning",
                                 sent_at="2026-10-09T01:35:11Z"),
                "evening": entry("2026-10-09", "evening"),
            },
        }))
        code, out = self.run_watchdog("2026-10-09T16:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("false", got["miss"],
                         "a healthy evening raised a false alarm:\n" + out)
        self.assertEqual("DELIVERED", got["displaced_verdict"])
        self.assertIn("Watchdog (displaced slot): morning digest for 2026-10-09",
                      out)

    def test_the_displaced_slot_is_judged_from_the_ledger_not_the_marker(self):
        """The marker alone cannot answer this, and the test proves it.

        The marker names the EVENING slot and says nothing about the morning, so
        any implementation reading only the marker would have to guess. The
        mirror is the only source here that can, which is why this test writes
        the two files with DIFFERENT content and requires the ledger's answer.
        """
        # The marker claims the morning WAS delivered (legacy shape), while the
        # ledger -- the authority -- says it was not. The ledger must win, or
        # the displaced slot stays unjudged.
        self.write_marker(date="2026-10-09", time="evening", delivered=True,
                          morning="2026-10-09")
        self.write_mirror(mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {"evening": entry("2026-10-09", "evening")},
        }))
        code, out = self.run_watchdog("2026-10-09T16:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("MISSING", got["displaced_verdict"],
                         "the displaced verdict came from the single-slot marker:\n"
                         + out)

    def test_a_stale_mirror_yields_unknown_not_a_false_alarm(self):
        """THE LAG, TAKEN SERIOUSLY.

        The seal job commits the mirror at 06:50/18:50 NPT, so it is up to ~12h
        stale and blind to the newest push. A mirror that PREDATES the slot it
        is asked about cannot testify either way, and reporting that as a miss
        would page the owner nightly until the mirror caught up.
        """
        self.write_marker(date="2026-10-09", time="evening", delivered=True)
        # as_of is 2026-10-09T00:30Z -- BEFORE the 01:35Z morning due time.
        self.write_mirror(mirror_doc(as_of="2026-10-09T00:30:00Z", days={
            "2026-10-09": {"evening": entry("2026-10-09", "evening")},
        }))
        code, out = self.run_watchdog("2026-10-09T16:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("UNKNOWN", got["displaced_verdict"],
                         "a lagging record was reported as a verdict:\n" + out)
        self.assertEqual("false", got["miss"],
                         "a stale mirror produced a false alarm:\n" + out)

    def test_an_entry_without_a_send_is_a_recorded_miss(self):
        """An entry that exists and carries no `ntfy_sent_at` IS a miss.

        This is different from an ABSENT entry, which needs the freshness test.
        A slot the Worker recorded and did not push for has been judged by the
        authority already; second-guessing it would be worse than reading it.
        """
        self.write_marker(date="2026-10-09", time="evening", delivered=True)
        stale = entry("2026-10-09", "morning", sent_at=None)
        stale["ntfy_sent_at"] = None
        stale["first_ntfy_sent_at"] = None
        stale["kind"] = "unavailable"
        self.write_mirror(mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {"morning": stale,
                            "evening": entry("2026-10-09", "evening")},
        }))
        code, out = self.run_watchdog("2026-10-09T16:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("MISSING", got["displaced_verdict"], out)
        self.assertEqual("morning", got["slot"])
        self.assertEqual("true", got["miss"])

    def test_a_sendless_entry_in_a_mirror_that_predates_the_slot_is_unknown(self):
        """THE BUILD HALF IS NOT A DELIVERY VERDICT.

        The mirror step CREATES an entry for the slot it is sealing -- the build
        half, no `ntfy_sent_at` -- forty minutes BEFORE that slot's delivery, so a
        mirror can hold a present-but-sendless entry for a slot whose push it
        predates. Judged as a recorded miss that pages the owner for a delivery
        the mirror could not see yet. The committed 2026-10-18 mirror is exactly
        this shape: `morning` holds only built_at/story_count/source_health_state
        with as_of 01:03:59Z, before that morning's 01:35Z due time -- and the
        morning digest WAS delivered.

        RED before the freshness test reached this arm: the verdict was MISSING
        and `miss` was `true`.
        """
        self.write_marker(date="2026-10-09", time="evening", delivered=True)
        self.write_mirror(mirror_doc(as_of="2026-10-09T00:30:00Z", days={
            "2026-10-09": {
                "morning": {"built_at": "2026-10-09T00:03:40Z",
                            "source_health_state": "ok",
                            "story_count": 2},
                "evening": entry("2026-10-09", "evening"),
            },
        }))
        code, out = self.run_watchdog("2026-10-09T16:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("UNKNOWN", got["displaced_verdict"],
                         "a build half written before the delivery was reported as "
                         "a verdict:\n" + out)
        self.assertEqual("false", got["miss"],
                         "a mirror that cannot testify produced a false alarm:\n"
                         + out)

    def test_a_morning_run_judges_no_displaced_slot(self):
        """The morning firing's predecessor is YESTERDAY's evening.

        Different NPT date, so there is nothing to judge and nothing to say. A
        note printed here would be daily noise, which is why the block is
        gated on the date matching.
        """
        self.write_marker(date="2026-10-09", time="morning", delivered=True)
        self.write_mirror(mirror_doc(as_of="2026-10-09T08:00:00Z", days={}))
        code, out = self.run_watchdog("2026-10-09T04:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("not applicable", got["displaced_verdict"])
        self.assertNotIn("Watchdog (displaced slot)", out)
        self.assertEqual("false", got["miss"])

    def test_the_marker_still_decides_the_slot_this_run_owns(self):
        """U8 must not have replaced the existing verdict, only added to it.

        The displaced judgement is an ADDITION. The run's own slot is still
        decided by the marker and the due-window gate exactly as before, and
        that is what `judged_slot` exists to let a reader confirm.
        """
        self.write_marker(date="2026-10-09", time="evening", delivered=True)
        self.write_mirror(mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {
                "morning": entry("2026-10-09", "morning"),
                "evening": entry("2026-10-09", "evening"),
            },
        }))
        code, out = self.run_watchdog("2026-10-09T16:01:00Z")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("evening", got["judged_slot"])
        self.assertIn("Watchdog: evening digest for 2026-10-09 NPT — DELIVERED", out)


class PrimarySlotLedgerProofTests(Harness):
    """D1 — the slot this run OWNS is also judged from the ledger.

    THE DEFECT THIS PINS. `state/last_digest_sent.json` is written by the
    PREBUILD as `{"delivered": false, "phase": "prebuild"}` and nothing updates
    it when the Worker pushes, so a verdict taken from the marker alone called a
    digest MISSING that had arrived. Measured 2026-10-30 01:35:34Z,
    sequence_id `dmn-2026-10-30-morning`, Worker /health `on_time: true`: eight
    of twelve watchdog runs paged the owner for a delivery that had happened.

    THE DIRECTION IS PART OF THE CONTRACT, and the last two tests here are the
    ones that keep this from becoming a way to HIDE a miss: the ledger may only
    turn a MISS into a DELIVERED when it records a send for that exact slot.
    """

    NOW = "2026-10-09T16:01:00Z"

    def _run(self, mirror, kv=None):
        self.write_mirror(mirror)
        body = first_body(WATCHDOG, MARKER_STEP)
        env = {
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_WORKSPACE": str(self.tmp),
            "WATCHDOG_NOW_UTC": self.NOW,
            "EVENT_NAME": "schedule",
            "EVENT_SCHEDULE": "0 4,16 * * *",
            "TZ": "UTC",
        }
        if kv is None:
            env.update({"CF_LEDGER_TOKEN": "", "CF_ACCOUNT_ID": ""})
            return exec_body(body, env, self.tmp)
        env.update({"CF_LEDGER_TOKEN": TOKEN, "CF_ACCOUNT_ID": ACCOUNT_ID,
                    "CF_KV_NAMESPACE_ID": NAMESPACE_ID})
        return exec_body(body, env, self.tmp,
                         urlopen=fixed_urlopen(json.dumps(kv)))

    def test_a_prebuild_marker_does_not_outvote_a_recorded_send(self):
        """THE PRODUCTION SHAPE, replayed end to end."""
        self.write_marker(date="2026-10-09", time="evening",
                          delivered=False, phase="prebuild")
        mirror = mirror_doc(as_of="2026-10-09T14:10:00Z", days={
            "2026-10-09": {
                "morning": entry("2026-10-09", "morning"),
                "evening": entry("2026-10-09", "evening",
                                 sent_at="2026-10-09T13:35:34Z"),
            },
        })
        code, out = self._run(mirror)
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("false", got["miss"],
                         "the watchdog paged again for a delivered digest:\n" + out)
        self.assertEqual("evening", got["judged_slot"])
        self.assertIn("Watchdog (primary slot): evening digest for 2026-10-09 "
                      "NPT — DELIVERED", out)
        self.assertIn("proved by the git mirror rather than the marker", out)
        self.assertIn("delivered=False phase='prebuild'", out)

    def test_live_kv_proves_the_slot_through_a_lagging_mirror(self):
        """KV is the authority, so a mirror that predates the push cannot hide it."""
        self.write_marker(date="2026-10-09", time="evening",
                          delivered=False, phase="prebuild")
        mirror = mirror_doc(as_of="2026-10-09T01:00:00Z", days={})
        kv = {"schema_version": 1, "days": {"2026-10-09": {
            "morning": entry("2026-10-09", "morning"),
            "evening": entry("2026-10-09", "evening",
                             sent_at="2026-10-09T13:35:34Z"),
        }}}
        code, out = self._run(mirror, kv=kv)
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("false", got["miss"], out)
        self.assertIn("proved by KV rather than the marker", out)

    def test_no_record_of_a_send_is_still_a_miss(self):
        """THE HALF THAT MUST NOT REGRESS: silence proves nothing.

        Nobody records a send for the slot this run owns, so the marker's
        `delivered: false` stands and the alert still fires.
        """
        self.write_marker(date="2026-10-09", time="evening",
                          delivered=False, phase="prebuild")
        mirror = mirror_doc(as_of="2026-10-09T14:10:00Z", days={
            "2026-10-09": {"morning": entry("2026-10-09", "morning")},
        })
        code, out = self._run(mirror)
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("true", got["miss"],
                         "an undelivered slot stopped alerting:\n" + out)
        self.assertEqual("evening", got["slot"])
        self.assertIn("no delivered marker", out)

    def test_a_build_half_entry_is_not_a_send(self):
        """The mirror's own prebuild half must never be read as delivery.

        The mirror step writes an entry for the slot it is sealing BEFORE the
        push, carrying no `ntfy_sent_at`. Treating that as proof would swallow
        every real miss, which is worse than the false alarm this fixes.
        """
        self.write_marker(date="2026-10-09", time="evening",
                          delivered=False, phase="prebuild")
        half = entry("2026-10-09", "evening")
        half["ntfy_sent_at"] = None
        half["first_ntfy_sent_at"] = None
        mirror = mirror_doc(as_of="2026-10-09T14:10:00Z", days={
            "2026-10-09": {"morning": entry("2026-10-09", "morning"),
                           "evening": half},
        })
        code, out = self._run(mirror)
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("true", got["miss"],
                         "a build half was read as a delivery:\n" + out)


# =====================================================================
# Claim 2 — the mirror is copied from KV, never hand-merged
# =====================================================================


class MirrorIsCopiedNotMergedTests(Harness):
    """The delivery half is KV's own bytes. The merge cannot rewrite one."""

    def setUp(self):
        super().setUp()
        self.prebuild = self.tmp / "prebuild"
        self.prebuild.mkdir()

    def run_mirror(self, kv_ledger, artifact=None, cf=None):
        (self.prebuild / "artifact.json").write_text(
            json.dumps(artifact or {
                "built_at": "2026-10-09T01:20:00Z",
                "stories": 11,
                "source_health_state": "ok",
                "is_partial": False,
                "date": "2026-10-09",
                "slot": "morning",
            }, indent=2), encoding="utf-8")
        env = {
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "SLOT": "morning",
            "TODAY": "2026-10-09",
            "ARTIFACT_PATH": "prebuild/artifact.json",
            "CF_LEDGER_TOKEN": (cf or {}).get("CF_LEDGER_TOKEN", TOKEN),
            "CF_ACCOUNT_ID": (cf or {}).get("CF_ACCOUNT_ID", ACCOUNT_ID),
            "CF_KV_NAMESPACE_ID": (cf or {}).get("CF_KV_NAMESPACE_ID", NAMESPACE_ID),
            "TZ": "UTC",
        }
        body = first_body(PREBUILD, MIRROR_STEP)
        return exec_body(body, env, self.tmp,
                         urlopen=fixed_urlopen(kv_ledger))

    def mirror_on_disk(self):
        path = self.tmp / "state" / "digest_slot_ledger.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_delivery_half_is_KV_bytes_verbatim(self):
        """THE CLAIM. Not "equivalent to" KV — the same values.

        Three prior days are seeded in KV, including fields this step has no
        opinion about. Every one must survive into the mirror untouched. A merge
        that dropped, renamed or defaulted any of them would be a hand-merge.
        """
        kv = {"schema_version": 1, "as_of": "2026-10-08T13:40:00Z", "days": {
            "2026-10-07": {
                "morning": entry("2026-10-07", "morning",
                                 sent_at="2026-10-07T01:35:03Z"),
                "evening": entry("2026-10-07", "evening",
                                 sent_at="2026-10-07T13:35:09Z", kind="partial",
                                 is_partial=True),
            },
            "2026-10-08": {
                "morning": entry("2026-10-08", "morning",
                                 sent_at="2026-10-08T01:35:01Z"),
                "evening": entry("2026-10-08", "evening",
                                 sent_at="2026-10-08T13:35:22Z"),
            },
        }}
        code, out = self.run_mirror(json.dumps(kv))
        self.assertEqual(0, code, out)
        mirror = self.mirror_on_disk()
        for day in ("2026-10-07", "2026-10-08"):
            for slot in ("morning", "evening"):
                self.assertEqual(kv["days"][day][slot], mirror["days"][day][slot],
                                 "the delivery half was altered for %s %s:\n%s"
                                 % (day, slot, out))
        self.assertEqual(1, mirror["schema_version"])

    def test_the_build_half_is_added_from_this_run(self):
        """The other half of the direction of truth: git owns BUILD.

        `built_at`, `story_count` and `source_health_state` describe the run
        that BUILT the digest. The seal job dispatched that run and holds its
        artifact record, so it is the only party that can state them.
        """
        kv = {"schema_version": 1, "as_of": "2026-10-08T13:40:00Z", "days": {}}
        code, out = self.run_mirror(json.dumps(kv), artifact={
            "built_at": "2026-10-09T01:20:44Z", "stories": 13,
            "source_health_state": "partial", "is_partial": True,
            "date": "2026-10-09", "slot": "morning"})
        self.assertEqual(0, code, out)
        got = self.mirror_on_disk()["days"]["2026-10-09"]["morning"]
        self.assertEqual("2026-10-09T01:20:44Z", got["built_at"])
        self.assertEqual(13, got["story_count"])
        self.assertEqual("partial", got["source_health_state"])

    def test_the_merge_refuses_to_overwrite_a_value_KV_already_set(self):
        """"KV WINS" made mechanical, not a comment.

        KV already holds a `built_at` for this slot. The step must not replace
        it with this run's own, because the two halves have separate owners and
        a silent overwrite would make the record's provenance unknowable.
        """
        kv = {"schema_version": 1, "as_of": "2026-10-09T00:00:00Z", "days": {
            "2026-10-09": {"morning": entry(
                "2026-10-09", "morning", sent_at="2026-10-09T01:35:05Z",
                built_at="2026-10-08T23:00:00Z", story_count=4)},
        }}
        code, out = self.run_mirror(json.dumps(kv), artifact={
            "built_at": "2026-10-09T01:20:44Z", "stories": 13,
            "source_health_state": "ok", "date": "2026-10-09", "slot": "morning"})
        self.assertEqual(0, code, out)
        got = self.mirror_on_disk()["days"]["2026-10-09"]["morning"]
        self.assertEqual("2026-10-08T23:00:00Z", got["built_at"],
                         "the merge overwrote a value KV had already set:\n" + out)
        self.assertEqual(4, got["story_count"])
        # A key KV did NOT set is still filled in — that is the merge working.
        self.assertEqual("ok", got["source_health_state"])

    def test_no_delivery_field_is_invented_when_KV_is_unreadable(self):
        """The failure mode that would make this file dangerous.

        Git must never stand in for a delivery fact it did not witness. If KV
        cannot be read, the step writes NOTHING: there is no partial mirror, no
        empty `days`, no synthesised `delivered` flag.
        """
        code, out = self.run_mirror("", cf={"CF_LEDGER_TOKEN": "",
                                            "CF_ACCOUNT_ID": ""})
        self.assertEqual(0, code, out)
        self.assertFalse((self.tmp / "state" / "digest_slot_ledger.json").exists(),
                         "a mirror was written with no KV behind it:\n" + out)
        got = outputs_of(self.outputs)
        self.assertEqual("false", got["written"])
        self.assertIn("NO mirror is written", out)

    def test_malformed_KV_produces_no_mirror_rather_than_a_guessed_one(self):
        code, out = self.run_mirror("{not json at all")
        self.assertEqual(0, code, out)
        self.assertFalse((self.tmp / "state" / "digest_slot_ledger.json").exists())
        self.assertIn("rather than a guessed one", out)

    def test_the_prune_matches_the_Worker_retention_window(self):
        """A mirror keeping more days than KV would resurrect dropped history."""
        old = datetime_date(2026, 10, 1)
        days = {}
        for offset in range(0, 9):
            day = (old.toordinal() + offset)
            import datetime as _dt
            iso = _dt.date.fromordinal(day).isoformat()
            days[iso] = {"morning": entry(iso, "morning"),
                         "evening": entry(iso, "evening")}
        kv = {"schema_version": 1, "as_of": "2026-10-09T00:00:00Z", "days": days}
        code, out = self.run_mirror(json.dumps(kv))
        self.assertEqual(0, code, out)
        kept = self.mirror_on_disk()["days"]
        # Today (2026-10-09) plus the six days before it.
        self.assertEqual(7, len(kept), out)
        self.assertIn("2026-10-03", kept)
        self.assertNotIn("2026-10-02", kept)


def datetime_date(y, m, d):
    import datetime
    return datetime.date(y, m, d)


# =====================================================================
# Claim 3 — KV wins a disagreement, and the disagreement is logged
# =====================================================================


class KvWinsDisagreementTests(Harness):
    """Git never decides whether a push happened. KV decides, and says so."""

    def run_with_kv(self, now_utc, mirror_doc_, kv_payload):
        """Run the marker step with a readable live KV ledger.

        `kv_payload` is the DICT (or `{}`), not a pre-encoded string. Encoding
        happens exactly once, here. An earlier version dumped twice, which made
        the payload a JSON *string* rather than a JSON object -- so the step
        correctly reported "KV unreadable" and every KV-wins test passed or
        failed for the wrong reason. The `read_kv_ledger` contract distinguishes
        `{}` from None precisely because that distinction is load-bearing, so
        the harness has to preserve it.
        """
        self.write_marker(date="2026-10-09", time="evening", delivered=True)
        self.write_mirror(mirror_doc_)
        body = first_body(WATCHDOG, MARKER_STEP)
        env = {
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_WORKSPACE": str(self.tmp),
            "WATCHDOG_NOW_UTC": now_utc,
            "EVENT_NAME": "schedule",
            "EVENT_SCHEDULE": "0 4,16 * * *",
            "CF_LEDGER_TOKEN": TOKEN,
            "CF_ACCOUNT_ID": ACCOUNT_ID,
            "CF_KV_NAMESPACE_ID": NAMESPACE_ID,
            "TZ": "UTC",
        }
        payload = kv_payload if isinstance(kv_payload, str) else json.dumps(kv_payload)
        return exec_body(body, env, self.tmp, urlopen=fixed_urlopen(payload))

    def test_git_saying_delivered_does_not_override_KV_saying_it_did_not(self):
        """THE DANGEROUS DIRECTION.

        The mirror (stale, pre-push) has no morning entry, so a naive
        implementation reading git alone would call it UNKNOWN or MISSING. KV
        says the morning WAS delivered. KV wins: the run stays green.
        """
        mirror = mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {"evening": entry("2026-10-09", "evening")},
        })
        kv = {"schema_version": 1, "days": {
            "2026-10-09": {
                "morning": entry("2026-10-09", "morning",
                                 sent_at="2026-10-09T01:35:07Z"),
                "evening": entry("2026-10-09", "evening"),
            },
        }}
        code, out = self.run_with_kv("2026-10-09T16:01:00Z", mirror,
                                     kv)
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("DELIVERED", got["displaced_verdict"],
                         "git's silence outvoted KV's record:\n" + out)
        self.assertEqual("false", got["miss"])

    def test_git_saying_delivered_does_not_override_KV_recording_a_miss(self):
        """THE OTHER DIRECTION, and the one that pages.

        The mirror claims the morning went out; KV holds an entry with no send.
        KV wins, so the morning is MISSING and the run says so. A git-first
        implementation would swallow exactly the alarm this step exists to
        raise, and it would do it silently.
        """
        stale = entry("2026-10-09", "morning", sent_at=None,
                      kind="unavailable")
        stale["ntfy_sent_at"] = None
        stale["first_ntfy_sent_at"] = None
        mirror = mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {"morning": stale,
                            "evening": entry("2026-10-09", "evening")},
        })
        kv = {"schema_version": 1, "days": {
            "2026-10-09": {"evening": entry("2026-10-09", "evening")},
        }}
        code, out = self.run_with_kv("2026-10-09T16:01:00Z", mirror,
                                     kv)
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("MISSING", got["displaced_verdict"],
                         "git outvoted KV on a delivery fact:\n" + out)
        self.assertEqual("true", got["miss"])
        self.assertEqual("morning", got["slot"])

    def test_a_disagreement_is_LOGGED_not_silently_resolved(self):
        """A resolution nobody can see is indistinguishable from a bug.

        The winner is printed with both values named, so a human reading the log
        can tell which source decided and what the other one claimed.
        """
        mirror = mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {"morning": entry("2026-10-09", "morning",
                                            sent_at="2026-10-09T01:35:07Z"),
                            "evening": entry("2026-10-09", "evening")},
        })
        kv = {"schema_version": 1, "days": {
            "2026-10-09": {"evening": entry("2026-10-09", "evening")},
        }}
        code, out = self.run_with_kv("2026-10-09T16:01:00Z", mirror,
                                     kv)
        self.assertEqual(0, code, out)
        self.assertIn("::warning::", out)
        self.assertIn("DISAGREE", out)
        self.assertIn("KV IS AUTHORITATIVE FOR DELIVERY", out)
        self.assertIn("2026-10-09", out)

    def test_agreement_logs_no_disagreement(self):
        """The warning must be rare. A check that always fires is a check
        nobody reads."""
        days = {"2026-10-09": {
            "morning": entry("2026-10-09", "morning"),
            "evening": entry("2026-10-09", "evening"),
        }}
        mirror = mirror_doc(as_of="2026-10-09T08:00:00Z", days=days)
        kv = {"schema_version": 1, "days": days}
        code, out = self.run_with_kv("2026-10-09T16:01:00Z", mirror,
                                     kv)
        self.assertEqual(0, code, out)
        self.assertNotIn("DISAGREE", out)
        self.assertEqual("DELIVERED", outputs_of(self.outputs)["displaced_verdict"])

    def test_an_empty_but_readable_KV_is_still_KV_speaking(self):
        """`{}` means "KV says nothing", which is DIFFERENT from "unreadable".

        Treating them alike would silently demote KV to a fallback and hand the
        verdict back to the lagging mirror — the exact inversion U8 forbids.
        """
        mirror = mirror_doc(as_of="2026-10-09T08:00:00Z", days={
            "2026-10-09": {"morning": entry("2026-10-09", "morning"),
                            "evening": entry("2026-10-09", "evening")},
        })
        code, out = self.run_with_kv("2026-10-09T16:01:00Z", mirror, "{}")
        self.assertEqual(0, code, out)
        got = outputs_of(self.outputs)
        self.assertEqual("MISSING", got["displaced_verdict"],
                         "an empty KV was treated as unreadable:\n" + out)
        self.assertNotIn("could not be consulted", out)


# =====================================================================
# Claim 4 — the footer never claims to describe today
# =====================================================================


class FooterNeverClaimsTodayTests(unittest.TestCase):
    """The mirror lags, so the line that reads it must be scoped to history.

    `scripts/delivery_ledger.py` renders one line from this mirror. The mirror
    is committed AFTER the page that reads it renders, so the copy on the page
    is as of the previous seal -- up to ~12h stale and blind to the newest
    delivery. A footer asserting TODAY's punctuality from that copy is a lie
    wearing a measurement's clothes.
    """

    def _ledger(self, as_of, days, crons=("35 1 * * *", "35 13 * * *")):
        entries = []
        for date, slots in sorted(days.items()):
            for slot, at in sorted(slots.items()):
                scheduled = ("%sT%s:00Z" % (date, "01:35" if slot == "morning"
                                             else "13:35"))
                entries.append({
                    "npt_date": date, "slot": slot, "cron": crons[0 if
                        slot == "morning" else 1],
                    "scheduled_instant": scheduled,
                    "kind": "delivered",
                    "first_ntfy_sent_at": at,
                })
        return {"as_of": as_of, "crons": list(crons), "entries": entries}

    def _line(self, ledger):
        import delivery_ledger
        return delivery_ledger.delivery_line(ledger)

    def test_no_state_of_the_line_claims_to_describe_today(self):
        """Every reachable state, checked for the word.

        Not a sample of the states — all of them. The property is "the line
        never says today", and a property checked on one state is decoration.
        """
        days = {"2026-10-05": {"morning": "2026-10-05T01:35:03Z",
                               "evening": "2026-10-05T13:35:09Z"},
                "2026-10-06": {"morning": "2026-10-06T01:36:40Z",
                               "evening": "2026-10-06T13:35:11Z"}}
        states = {
            "clean": self._ledger("2026-10-06T14:00:00Z", days),
            "late": self._ledger("2026-10-06T14:00:00Z", {
                "2026-10-05": {"morning": "2026-10-05T01:35:03Z",
                               "evening": "2026-10-05T13:35:09Z"},
                "2026-10-06": {"morning": "2026-10-06T02:11:00Z",
                               "evening": "2026-10-06T13:35:11Z"}}),
            "missing": self._ledger("2026-10-06T14:00:00Z", {
                "2026-10-06": {"evening": "2026-10-06T13:35:11Z"}}),
            "pending": self._ledger("2026-10-06T02:00:00Z", days),
            "failed": self._ledger("2026-10-06T14:00:00Z", days),
        }
        states["failed"]["entries"][2]["kind"] = "ntfy_failed"
        for name, ledger in states.items():
            line = self._line(ledger)
            self.assertNotIn("today", line.lower(),
                             "the %s state claims to describe today: %r"
                             % (name, line))
            self.assertTrue(line.strip(),
                            "the %s state rendered an empty line" % name)

    def test_a_mirror_that_predates_the_newest_push_cannot_report_it(self):
        """THE LAG, MEASURED.

        The mirror is written at the seal, BEFORE the slot's own push, so its
        newest recorded day is the previous one. The line built from it must
        therefore count the slots it actually has evidence for — and must not
        present that count as if the window were complete.
        """
        mirror = self._ledger("2026-10-06T01:00:00Z", {
            "2026-10-05": {"morning": "2026-10-05T01:35:03Z",
                           "evening": "2026-10-05T13:35:09Z"}})
        line = self._line(mirror)
        # Exactly the two sends this copy witnessed, out of the full window.
        # A line that reported a complete window here would be inventing the
        # other twelve from nothing.
        self.assertIn("2/", line,
                      "the line does not count only what it measured: %r" % line)
        self.assertNotIn("14/14", line)
        self.assertIn("missing", line.lower(),
                      "an incomplete window must say so rather than look clean")

    def test_the_line_is_scoped_to_the_window_it_actually_measured(self):
        """What it DOES say, which is the useful half of the same property.

        The line is built from a 7-day x 2-slot window, so the denominator is
        14 and the claim is about that window -- never about an unnamed
        present. Asserted as a number so the wording and the retention window
        cannot drift apart silently.
        """
        import delivery_ledger
        line = self._line(self._ledger("2026-10-06T14:00:00Z", {
            "2026-10-05": {"morning": "2026-10-05T01:35:03Z",
                           "evening": "2026-10-05T13:35:09Z"},
            "2026-10-06": {"morning": "2026-10-06T01:35:02Z",
                           "evening": "2026-10-06T13:35:04Z"}}))
        window = delivery_ledger.RETENTION_DAYS * delivery_ledger.SLOTS_PER_DAY
        self.assertIn("/%d" % window, line,
                      "the line does not scope itself to the %d-slot window it "
                      "measured: %r" % (window, line))
        self.assertIn("Delivery", line)


# =====================================================================
# The U6 / U7 guards, re-asserted so U8 cannot have broken them
# =====================================================================


class PriorGuardsStillHoldTests(unittest.TestCase):
    """A change that fixes one thing and breaks another is not a fix."""

    def test_phase_prebuild_still_cannot_seal_a_delivered_marker(self):
        """U6. `phase: prebuild` pushes nothing, so `delivered` must be false."""
        body = first_body(DAILY, "Record delivery status")
        self.assertIn("prebuild", body)
        self.assertIn("website_ok = ntfy_ok = gmail_ok = telegram_ok = False", body)
        self.assertIn("(not dry_run) and (not prebuild) and (stories > 0)", body)

    def test_the_ladder_still_fires_only_on_a_verified_failure(self):
        """U7. A push that arrived is not a failure."""
        body = first_body(DAILY, "Fallback no-op check")
        self.assertIn("ntfy_failed", body)

    def test_the_supersede_cannot_loop(self):
        """U7. The latch is written BEFORE the dispatch, and re-read.

        The order is the whole invariant: `ntfy_full_sent_at` claims a push
        left the job, so writing it first would put a claim of delivery into
        the punctuality ledger before anything was dispatched. So the test
        compares the position of the LATCH WRITE against the position of the
        DISPATCH, not against the guard that merely mentions the field.
        """
        run = step_dict(PREBUILD,
                        "Supersede a degraded push with the full digest")["run"]
        # The latch write, as a statement rather than as a mention.
        latch_write = run.index("'full_artifact_available_at': now")
        # The dispatch, in the shell, after the latch is on disk.
        dispatch = run.index("gh api -X POST")
        self.assertLess(latch_write, dispatch,
                        "the loop latch is written after the dispatch, so a "
                        "failed dispatch would leave no latch")
        # And the second half only records after the dispatch was accepted.
        self.assertIn("ntfy_full_sent_at", run)
        # The re-read that stops a second seal job from repeating it.
        self.assertIn("if entry.get('full_artifact_available_at')", run)

    def test_deliver_now_still_cannot_write_the_ledger(self):
        """U7. The probe must not be able to reach the ledger writer."""
        with open(ROOT / "deploy" / "cloudflare-worker.js", encoding="utf-8") as fh:
            worker = fh.read()
        self.assertIn("const DIGEST_LEDGER_KEY", worker)
        # The delivery probe path passes writer=null; the ledger is unreachable
        # from any fetch handler.
        self.assertIn("deliverDigestPlan(env, slot, null)", worker)

    def test_the_two_literal_ntfy_step_names_are_unchanged(self):
        """U7. Renaming either silently breaks the step that pushes."""
        names = [name for _job, name, _sd in W.iter_all_steps(DAILY)]
        self.assertIn("Publish to ntfy (primary)", names)
        self.assertIn("Send digest via Gmail (backup)", names)


# =====================================================================
# Structural: the mirror step must stay parseable and honest
# =====================================================================


class MirrorStepShapeTests(unittest.TestCase):
    def test_the_ledger_key_literal_matches_the_Worker_constant(self):
        with open(ROOT / "deploy" / "cloudflare-worker.js", encoding="utf-8") as fh:
            worker = fh.read()
        self.assertIn('const DIGEST_LEDGER_KEY = "%s"' % WORKER_LEDGER_KEY, worker)
        body = first_body(PREBUILD, MIRROR_STEP)
        self.assertIn("LEDGER_KEY = '%s'" % WORKER_LEDGER_KEY, body)

    def test_the_mirror_is_committed_and_nothing_else(self):
        """The seal job widened `contents` to write; this bounds the blast radius."""
        step = step_dict(PREBUILD, COMMIT_MIRROR_STEP)
        run = step["run"]
        self.assertIn("git add state/digest_slot_ledger.json", run)
        for other in ("git add state/history.json", "git add docs/",
                      "git add .github", "git add -A", "git commit -a"):
            self.assertNotIn(other, run,
                             "the mirror commit stages more than the mirror")

    def test_the_mirror_commit_is_gated_on_the_step_actually_writing(self):
        step = step_dict(PREBUILD, COMMIT_MIRROR_STEP)
        self.assertIn("steps.mirror.outputs.written == 'true'", str(step.get("if")))

    def test_daily_news_refreshes_only_existing_entries(self):
        """Git must never create a slot entry — that would invent a delivery.

        Asserted BEHAVIOURALLY, by executing the commit step's mirror block
        against a ledger that records no entry for this slot. A wording check
        ("the comment is still there") passes on a step whose guard has been
        deleted, which is exactly the mutation this had to catch.
        """
        body = first_body(DAILY, COMMIT_STATE_STEP)
        with tempfile.TemporaryDirectory(prefix="commit-state-") as tmp:
            work = Path(tmp)
            (work / "state").mkdir()
            # A mirror whose only day is NOT this run's (today, slot): the
            # realistic shape when KV has not yet recorded the push.
            (work / "state" / "digest_slot_ledger.json").write_text(
                json.dumps(mirror_doc(as_of="2026-10-09T02:10:00Z", days={
                    "2026-10-08": {"morning": entry("2026-10-08", "morning")},
                }), indent=2), encoding="utf-8")
            (work / "state" / "stories.validated.json").write_text(
                json.dumps({"stories": [{"title": "a"}],
                            "source_health": {"state": "ok"}}), encoding="utf-8")
            out_file = work / "gh_output.txt"
            out_file.write_text("", encoding="utf-8")
            code, out = exec_body(body, {
                "GITHUB_OUTPUT": str(out_file),
                # SLOT and TODAY are the names the step's own heredoc reads.
                # They are wired to the same values as DIGEST_TIME / env.TODAY
                # in the workflow's `env:` block; a test that passed different
                # names would be testing a step that cannot run.
                "SLOT": "morning",
                "TODAY": "2026-10-09",
            }, work)
            self.assertEqual(0, code, out)
            after = json.loads(
                (work / "state" / "digest_slot_ledger.json").read_text(encoding="utf-8"))
            self.assertNotIn("2026-10-09", after["days"],
                             "git created a slot entry KV never recorded:\n" + out)
            self.assertEqual(
                {"2026-10-08": {"morning": entry("2026-10-08", "morning")}},
                after["days"],
                "the mirror was altered for a slot it must not touch:\n" + out)

    def test_daily_news_refreshes_the_build_half_on_an_existing_entry(self):
        """The other half: on an entry KV DID record, the build half updates.

        Without this the previous test would pass on a step that did nothing at
        all, which is the mirror-image of the decoration problem.
        """
        body = first_body(DAILY, COMMIT_STATE_STEP)
        with tempfile.TemporaryDirectory(prefix="commit-state-") as tmp:
            work = Path(tmp)
            (work / "state").mkdir()
            (work / "state" / "digest_slot_ledger.json").write_text(
                json.dumps(mirror_doc(as_of="2026-10-09T02:10:00Z", days={
                    "2026-10-09": {"morning": entry("2026-10-09", "morning")},
                }), indent=2), encoding="utf-8")
            (work / "state" / "stories.validated.json").write_text(
                json.dumps({"stories": [{"title": "a"}, {"title": "b"}],
                            "source_health": {"state": "partial"}}), encoding="utf-8")
            out_file = work / "gh_output.txt"
            out_file.write_text("", encoding="utf-8")
            code, out = exec_body(body, {
                "GITHUB_OUTPUT": str(out_file),
                "SLOT": "morning",
                "TODAY": "2026-10-09",
            }, work)
            self.assertEqual(0, code, out)
            got = json.loads(
                (work / "state" / "digest_slot_ledger.json")
                .read_text(encoding="utf-8"))["days"]["2026-10-09"]["morning"]
            self.assertEqual(2, got["story_count"])
            self.assertEqual("partial", got["source_health_state"])
            # The delivery half is untouched: KV's own send timestamp survives.
            self.assertEqual("2026-10-09T01:35:12Z", got["ntfy_sent_at"])

    def test_the_direction_of_truth_is_documented_where_a_reader_looks(self):
        """A rule that lives only in a commit message is not a rule.

        The wording is asserted against the step's whole YAML block, because
        that is where someone fixing the next bug will actually be reading --
        the comment above the step and the code inside it.
        """
        for workflow, step_name in ((PREBUILD, MIRROR_STEP),
                                    (WATCHDOG, MARKER_STEP)):
            block = step_block_text(workflow, step_name)
            self.assertIn("KV IS AUTHORITATIVE FOR DELIVERY", block,
                          "%s / %r does not state the direction of truth"
                          % (workflow, step_name))
            self.assertIn("GIT IS AUTHORITATIVE FOR BUILD", block,
                          "%s / %r does not state who owns BUILD"
                          % (workflow, step_name))

    def test_the_lag_is_documented_next_to_the_writer(self):
        """Stated where the mirror is written, not only in a document.

        The lag is the reason the footer wording is constrained, so a reader
        looking at the writer must be told about it in the same breath.
        """
        block = step_block_text(PREBUILD, MIRROR_STEP)
        self.assertIn("LAG", block)
        self.assertIn("must never", block)
        self.assertIn("12h", block)


def run_commit_state_mirror(work, slot, today, artifact=None):
    """Execute the commit step's mirror block in `work`. Returns (code, out).

    `artifact` is the state/stories.validated.json payload, or None to model a
    run whose witness file is not there at all.
    """
    (work / "state").mkdir(exist_ok=True)
    (work / "state" / "digest_slot_ledger.json").write_text(
        json.dumps(mirror_doc(as_of="2026-10-30T13:41:16Z", days={
            today: {
                "morning": entry(today, "morning"),
                slot: entry(today, slot, sent_at="2026-10-30T13:41:16Z",
                            story_count=15, source_health_state="ok"),
            },
        }), indent=2), encoding="utf-8")
    if artifact is not None:
        (work / "state" / "stories.validated.json").write_text(
            json.dumps(artifact), encoding="utf-8")
    out_file = work / "gh_output.txt"
    out_file.write_text("", encoding="utf-8")
    return exec_body(first_body(DAILY, COMMIT_STATE_STEP), {
        "GITHUB_OUTPUT": str(out_file),
        "SLOT": slot,
        "TODAY": today,
    }, work)


def read_commit_entry(work, slot, today):
    return json.loads((work / "state" / "digest_slot_ledger.json")
                      .read_text(encoding="utf-8"))["days"][today][slot]


class AnEmptyReadingCannotEraseADelivery(unittest.TestCase):
    """Run 41064520752: a real 15-story evening send was rewritten as 0/unknown.

    The cause was not the mirror writer's arithmetic, it was that the step
    before it deleted the only witness to the build, so the writer read "gone"
    and recorded it as "empty". Both halves are pinned here: the writer must
    never make a record worse than it already is, and the witness must survive
    until the writer has read it.
    """

    def test_a_missing_witness_leaves_a_populated_record_alone(self):
        with tempfile.TemporaryDirectory(prefix="commit-state-") as tmp:
            work = Path(tmp)
            code, out = run_commit_state_mirror(work, "evening", "2026-10-30",
                                                artifact=None)
            self.assertEqual(0, code, out)
            got = read_commit_entry(work, "evening", "2026-10-30")
            self.assertEqual(15, got["story_count"],
                             "a run with no witness zeroed a delivered slot:\n" + out)
            self.assertEqual("ok", got["source_health_state"],
                             "a run with no witness erased the known health state:"
                             "\n" + out)
            self.assertEqual("sent", got["state"])
            self.assertEqual("2026-10-30T13:41:16Z", got["ntfy_sent_at"])
            self.assertIn("no witness", out,
                          "the writer changed nothing and did not say why:\n" + out)

    def test_an_empty_witness_cannot_zero_a_delivered_slot(self):
        """A readable-but-empty artifact is the clobbered artifact's shape."""
        with tempfile.TemporaryDirectory(prefix="commit-state-") as tmp:
            work = Path(tmp)
            code, out = run_commit_state_mirror(
                work, "evening", "2026-10-30",
                artifact={"stories": [], "source_health": {}})
            self.assertEqual(0, code, out)
            got = read_commit_entry(work, "evening", "2026-10-30")
            self.assertEqual(15, got["story_count"],
                             "0 stories overwrote 15:\n" + out)
            self.assertEqual("ok", got["source_health_state"],
                             "`unknown` overwrote `ok`:\n" + out)

    def test_a_real_witness_still_wins_over_the_previous_count(self):
        """The anti-clobber rule must not become an anti-update rule."""
        with tempfile.TemporaryDirectory(prefix="commit-state-") as tmp:
            work = Path(tmp)
            code, out = run_commit_state_mirror(
                work, "evening", "2026-10-30",
                artifact={"stories": [{"title": str(n)} for n in range(3)],
                          "source_health": {"state": "degraded"}})
            self.assertEqual(0, code, out)
            got = read_commit_entry(work, "evening", "2026-10-30")
            self.assertEqual(3, got["story_count"], out)
            self.assertEqual("degraded", got["source_health_state"],
                             "a real, worse reading must still be recorded:\n" + out)

    def test_an_unreadable_witness_is_not_a_witness(self):
        with tempfile.TemporaryDirectory(prefix="commit-state-") as tmp:
            work = Path(tmp)
            (work / "state").mkdir()
            code, out = run_commit_state_mirror(work, "evening", "2026-10-30",
                                                artifact=None)
            self.assertEqual(0, code, out)
            (work / "state" / "stories.validated.json").write_text(
                "{not json", encoding="utf-8")
            out_file = work / "gh_output.txt"
            code, out = exec_body(first_body(DAILY, COMMIT_STATE_STEP), {
                "GITHUB_OUTPUT": str(out_file),
                "SLOT": "evening",
                "TODAY": "2026-10-30",
            }, work)
            self.assertEqual(0, code, out)
            got = read_commit_entry(work, "evening", "2026-10-30")
            self.assertEqual(15, got["story_count"], out)
            self.assertEqual("ok", got["source_health_state"], out)

    def test_the_witness_survives_until_the_mirror_has_read_it(self):
        """The ordering defect itself, asserted on the two steps that made it.

        `Clean transient state files` deleted the artifact BEFORE `Commit state
        changes` read it. A wording check cannot catch that coming back, so the
        two paths are inspected directly.
        """
        clean = step_dict(DAILY, "Clean transient state files")["run"]
        self.assertNotIn("state/stories.validated.json", clean,
                         "the clean step deletes the mirror's only witness again")
        run = step_dict(DAILY, COMMIT_STATE_STEP)["run"]
        mirror_write = run.index("json.dump(mirror, fh")
        witness_removal = run.index("rm -f state/stories.validated.json")
        self.assertLess(mirror_write, witness_removal,
                        "the witness is deleted before the mirror reads it")
        self.assertNotIn("git add state/stories.validated.json", run)


if __name__ == "__main__":
    unittest.main()
