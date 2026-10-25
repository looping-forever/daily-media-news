#!/usr/bin/env python3
"""Shared helpers/fixtures for the render honesty + readability tests."""

import io
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "daily-news.yml")

# WHY THE FIXTURE LIVES HERE AND NOT IN state/.
# This used to read `state/mock_stories.json`, which is a RUNTIME directory:
# every real digest run rewrites it, `state/` is gitignored, and a fresh CI
# checkout therefore does not have it at all. The render suite then died with
# FileNotFoundError on CI only, while passing on the machine that happened to
# have a stale copy lying around -- the exact "passes locally, red in CI" class
# this repository keeps paying for.
#
# So the fixture is now a TEST artefact, committed next to the tests that own
# it and never touched by a workflow. `tests/render/test_render_honesty.py`
# (TestRenderFixtureIsolation) asserts this invariant: no file under tests/
# may read a path inside the runtime `state/` directory.
MOCK_STORIES = os.path.join(REPO_ROOT, "tests", "render", "data",
                            "mock_stories.json")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

CUTOFF = "2026-09-27 06:12"
TU_URLS = ["https://iost.tu.edu.np/", "https://tu.edu.np/"]
TU_HOSTS = ["iost.tu.edu.np", "tu.edu.np"]


# --------------------------------------------------------------------------
# LITERAL strings from FROZEN CONTRACT B and C. Both renderers and the ntfy
# heredoc must produce exactly these - that is the whole point of the tests.
# --------------------------------------------------------------------------
B2 = "⚠️ %d of %d sources for this section were unreachable."
B3 = "⚠️ Source status unknown — this section may be incomplete."
B4 = "Nothing new from this source today."
B5 = "⚠️ Source unreachable — check %s"

BANNER = {
    "partial": "⚠️ Some sources failed — this digest may be missing items.",
    "degraded": "⚠️ Some sources were unreachable — this digest may be missing items.",
    "unknown": "⚠️ Source check unavailable — this digest may be missing items.",
}

CUTOFF_OK = "Information checked through: %s NPT" % CUTOFF
CUTOFF_PARTIAL = "Information checked through: %s NPT · some sources failed" % CUTOFF
CUTOFF_DEGRADED = "Information checked through: %s NPT · some sources were unreachable" % CUTOFF
CUTOFF_UNKNOWN = "⚠️ Freshness unverified — no source was confirmed reachable"

NOTE_PREFIX = "⚠️ Research note: "


def health(state, *, by_category=None, hosts_down=None, critical_total=33,
           ok=None, failed=None, skipped=0):
    """A source_health payload in the exact shape the pipeline emits."""
    failed = len(hosts_down or []) if failed is None else failed
    ok = (critical_total - failed) if ok is None else ok
    return {
        "schema_version": 1,
        "state": state,
        "checked_at_npt": CUTOFF,
        "critical": {
            "total": critical_total, "ok": ok, "failed": failed,
            "skipped": skipped, "hosts_down": list(hosts_down or []),
            "reasons": ["dns"] if failed else [],
        },
        "all": {"total": 43, "ok": 43 - failed, "failed": failed, "skipped": skipped},
        "by_category": by_category if by_category is not None else {},
        "failed_names": ["notice:%s" % h for h in (hosts_down or [])],
        "reachable_hosts": ["api.open-meteo.com"],
    }


def cat_entry(total=11, ok=9, failed=2, skipped=0, urls=None):
    return {"total": total, "ok": ok, "failed": failed, "skipped": skipped,
            "urls": list(TU_URLS) if urls is None else urls}


def load_mock_stories():
    with io.open(MOCK_STORIES, encoding="utf-8") as fh:
        return json.load(fh)


def validated_payload(state, *, notes="", by_category=None, hosts_down=None,
                      stories=None, critical_total=33):
    """A synthetic state/stories.validated.json for the given health state.

    `state=None` omits source_health entirely (the legacy-build case, which
    must render `unknown`).
    """
    data = load_mock_stories()
    data["date"] = "2026-09-27"
    data["digest_time"] = "morning"
    data["data_cutoff_npt"] = CUTOFF
    data["generated_at_npt"] = "2026-09-27 06:31"
    if stories is not None:
        data["stories"] = stories
    if notes:
        data["notes"] = notes
    if state is not None:
        data["source_health"] = health(
            state, by_category=by_category, hosts_down=hosts_down,
            critical_total=critical_total)
    else:
        data.pop("source_health", None)
    return data


def write_json(path, data):
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path


def twenty_story_payload(health_state="degraded", hosts_down=None,
                         notes="TU notice board unreachable (DNS SERVFAIL)."):
    """A deliberately bulky 20-story payload for the ntfy byte budget test."""
    base = validated_payload(health_state, notes=notes, hosts_down=hosts_down)
    filler = ("Tribhuvan University and the wider Nepali public sector continue "
              "to publish overlapping notices this morning, and the digest "
              "builder keeps every field intact while doing it. ")
    stories = []
    cats = ["nepal", "gaming", "tech_career", "world_major", "anime"]
    for i in range(20):
        stories.append({
            "title": "Story %02d: a deliberately long headline about Nepali news" % i,
            "summary": filler * 2,
            # Display-only and optional. Half these stories deliberately have
            # NO for_you so the byte-budget tests also cover the path where the
            # label must not render at all.
            "for_you": (filler[:140] if i % 2 == 0 else ""),
            "source_name": "Kathmandu Post",
            "source_url": "https://example.com/story/%02d" % i,
            "reliability": "tier1",
            "category": cats[i % len(cats)],
            "priority": 5,
            "published_at": "2026-09-27",
            "supporting_sources": [],
        })
    base["stories"] = stories
    base["source_health"] = health(
        health_state, hosts_down=hosts_down if hosts_down is not None else TU_HOSTS)
    return base
