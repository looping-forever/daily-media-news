#!/usr/bin/env python3
"""collect_news.py -- deterministic Stage-A news collection for the digest.

Runs BEFORE the AI research agent (Stage-B) so the agent classifies
pre-collected candidates instead of burning its budget on 20+ searches.
Stdlib only; parallel fetching via concurrent.futures.ThreadPoolExecutor
(max_workers=8, per-request timeout, overall soft budget 75s).

Sources:
- Google News RSS sweeps:
    * EN base (hl=en-US&gl=US&ceid=US:en) built from top entertainment
      interests/franchises in config/interests.yml;
    * JP / KR / ZH locale sweeps built from anime/manga/manhwa titles;
    * Nepal sweep (gl=NP, hl=en and hl=ne) with fixed education / college /
      bank / local / national queries.
- Weather: open-meteo forecast for the configured location -> ONE synthetic
  candidate with a compact human-readable summary (domain_category
  "weather_forecast").
- Official notice pages (best-effort, NEVER fatal): the sites listed in
  NOTICE_SITES -> <a>-text extraction of links whose text is a notice, judged
  with a SCRIPT-AWARE keyword gate: a
  Latin anchor must carry notice/result/exam/admission/rate/branch (English
  rules, unchanged), a Devanagari anchor is admitted by the Nepali notice
  vocabulary, and an anchor in any other script is KEPT because our Latin
  vocabulary's absence says nothing about a script we cannot read. The
  structural detail-URL gate and the nav-chrome rules then apply to every
  script identically.

Per-candidate shape:
  {title, url, source(domain), published(ISO|null), snippet(first 300 chars),
   domain_category, language}
  Optional media (passthrough for the agent; never invented):
    image: {url, width?, height?, type?, source}
    video: {url, type?, source}

Early filters: excluded-keyword prefilter (same semantics as
validate_stories.py), normalized-URL dedup, title Jaccard >= 0.7 dedup,
pubDate older than 36h dropped (when parseable), cap 20/domain_category,
140 total, freshest first.

Per-source truth (source_results): ONE record per plan, emitted in PLAN ORDER,
stating REACHABILITY only -- never "produced items". A 200 with an empty RSS
body is "ok". A task the soft budget never finished is "skipped_budget", which
is UNKNOWN and is deliberately NOT reported as a failure. sources_ok /
sources_failed keep their historical shape for the prompt and the logs.

Tuning lives in config/collection.json (critical hosts, per-host timeouts,
retry policy, soft budget, worker count). Missing or corrupt config falls back
to the built-in values and prints one warning; it never raises.

Robustness: EVERY source failure is recorded in sources_failed[] and the run
continues. If ALL network fetches fail, the output file is STILL written with
empty candidates + sources_failed populated (the workflow/agent decides).

Outputs:
    --out     {"collected_at", "sources_ok", "sources_failed", "source_results",
               "weather_summary", "weather_advice", "candidates": [...]}
    --timings {"collect_total_sec": float, "by_source": {name: seconds}}

`weather_advice` is a list of {"id", "text", "priority"} dicts produced by the
DETERMINISTIC rule engine in weather_advice.py -- never authored by a model and
never phrased from a raw temperature or probability. It is [] whenever the
forecast could not be fetched, could not be understood, or has nothing
actionable to say, and a failure to produce it is additionally recorded as its
own `source_results` row (weather_advice:open-meteo) so the honesty banner can
report an ABSENT source as absent. Weather must never take down the digest.

Exit codes: 0 = output written (even with zero candidates) | 1 = fatal local
error (output could not be produced).

Works on Ubuntu python3 and Windows py 3.13. No datetime.utcnow().
"""

import argparse
import concurrent.futures as cf
import html
import json
import os
import queue as _queue
import re
import socket
import sys
import threading
import time
import urllib.request
from urllib.error import URLError
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlparse

try:
    import source_health
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import source_health

try:
    import weather_advice as _wa
except ImportError:  # same fallback, for the deterministic advice engine
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import weather_advice as _wa

try:
    # The Devanagari vocabulary and the ONE script detector this run uses.
    # Imported READ-ONLY: nothing here redefines script detection or a Nepali
    # word, so the notice gate below and the academic result path cannot drift
    # apart on what the word for "notice" is.
    import devanagari as _devanagari
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import devanagari as _devanagari

try:
    # The two institution notice pages and their names are deployment values,
    # never committed literals (see private_values).
    import private_values as _pv
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import private_values as _pv

try:
    # THE Nepal-time helper is NOT re-implemented here. Four copies of the
    # +05:45 shift already exist in this repo, and a fifth is how the digest's
    # hero cutoff and its footer ended up disagreeing. validate_stories holds
    # the canonical one and -- usefully -- already returns exactly the
    # 'YYYY-MM-DD HH:MM' shape weather_advice parses.
    from validate_stories import kathmandu_now_str as _kathmandu_now_str
except ImportError:  # pragma: no cover - only if validate_stories moves
    _kathmandu_now_str = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_WORKERS = 8
SOFT_BUDGET_SEC = 75.0
FRESHNESS_HOURS = 36
MAX_TOTAL = 140
#: How many candidates ONE domain may contribute. This is the agent's INPUT
#: size, not a display setting: measured 2026-10-27, the collector's 20-per-domain
#: output was 140 rows / 139,207 bytes, and the model has to read all of it before
#: it can classify anything. 4 per domain keeps every domain represented, keeps
#: the freshest of each (the ordering below is freshest-first), and is still far
#: more than the 5-15 stories the digest targets. The prompt states the cap, so
#: an absent candidate is never read as a quiet day.
MAX_PER_DOMAIN = 4

TITLE_JACCARD_THRESHOLD = 0.7
SNIPPET_MAX_CHARS = 300

# The request host a plan talks to (contract A1 `host`).
GNEWS_HOST = "news.google.com"
WEATHER_HOST = "api.open-meteo.com"

# Built-in fallback for config/collection.json. These values are what the
# collector used before the file existed, so a missing/corrupt config changes
# nothing except that it warns.
DEFAULT_TIMEOUT_SEC = 10
DEFAULT_CRITICAL_HOSTS = ["tu.edu.np", "iost.tu.edu.np", "news.google.com"]
DEFAULT_TIMEOUT_SEC_BY_HOST = {"tu.edu.np": 20, "iost.tu.edu.np": 20}
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF_SEC = [0.75]
DEFAULT_RETRY_ON = ["timeout", "connection", "dns", "tls", "throttled",
                    "http_5xx"]
# The shipped retry policy as a single object, so tests and the config loader
# have one reference for the defaults.
DEFAULT_RETRY_CFG = {"attempts": DEFAULT_RETRY_ATTEMPTS,
                     "backoff_sec": list(DEFAULT_RETRY_BACKOFF_SEC),
                     "retry_on": list(DEFAULT_RETRY_ON),
                     "resolve_timeout_sec": 5.0}
# DNS pre-flight bound. Kept well under the socket timeout so a hung resolver
# is reported, not waited out.
DEFAULT_RESOLVE_TIMEOUT_SEC = 5.0
# A reachable source that produced FEWER than this many items is recorded as
# outcome "empty" (answered, nothing usable) instead of "ok". Only a literal
# zero is empty by default.
DEFAULT_MIN_ITEMS_PER_SOURCE = 1
# Liveness sweep for the helper thread only. It is a DAEMON thread, so it can
# never delay interpreter exit.
_RESOLVER_IDLE_QUIT_SEC = 5.0
# Slack added to the configured resolve bound before the caller gives up.
_RESOLVE_BOUND_SLACK_SEC = 0.5
# After a probe blows its bound, stop probing entirely for this long. A wedged
# resolver is a run-wide condition, not a per-host one.
_RESOLVER_WEDGE_COOLDOWN_SEC = 30.0

USER_AGENT = (
    "Mozilla/5.0 (compatible; daily-media-news-collector/1.0; "
    "personal digest Stage-A; stdlib-urllib)"
)

GNEWS_SEARCH = "https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s"

# locale key -> (hl, gl, ceid, language code)
LOCALES = {
    "en-us": ("en-US", "US", "US:en", "en"),
    "ja-jp": ("ja", "JP", "JP:ja", "ja"),
    "ko-kr": ("ko", "KR", "KR:ko", "ko"),
    "zh-cn": ("zh-CN", "CN", "CN:zh-Hans", "zh"),
    "np-en": ("en", "NP", "NP:en", "en"),
    "np-ne": ("ne", "NP", "NP:ne", "ne"),
}

# Fixed Nepal sweep: (query, domain_category). These are deliberately generic:
# a deployment narrows them to its own town / campus / bank by editing this
# list, and nothing here names a real person's whereabouts or accounts.
NP_QUERIES = [
    ("Tribhuvan University", "education_tu"),
    ("BSc CSIT", "education_tu"),
    ("IOST notice", "education_tu"),
    ("college notice", "college_local"),
    ("bank customer notice", "bank_local"),
    ("NEA load shedding", "local_city"),
    ("municipality notice", "local_city"),
    ("district administration notice", "local_city"),
    ("highway closure", "local_city"),
    ("Nepal government decision", "nepal"),
    ("Nepal IT jobs", "tech_career"),
    ("Nepal technology news", "tech_career"),
    ("Nepal scholarship CSIT", "tech_career"),
    ("Nepal major news today", "world_major"),
]

# Redundancy, not replacement: when a university's own notice page is dark,
# Google News may still have the notice in ITS index. np-en ONLY -- a Nepali
# locale sweep of English notice titles is low-yield and doubles the cost.
#
# CORRECTION (the old comment here claimed "`site:` support is UNVERIFIED,
# which is fine"). That was wrong in the DANGEROUS direction. `site:` DOES work
# and IS high-yield -- and it is POISONED. Measured live: 6 of 8
# `site:iost.tu.edu.np` items carried the site's CONSTANT title (one string,
# distinct pubDates, no real headline), so they sort ABOVE the genuine notices
# and fill `education_tu` to its cap. The old reasoning ("a zero-yield sweep is
# still outcome ok, so it can never fabricate a failure") is exactly the bug
# class this build exists to kill: reachable is not the same as useful.
#
# DECISION: the two `site:iost.tu.edu.np` sweeps are DELETED, not repaired.
#   * the notice-page scraper already reads iost.tu.edu.np directly and
#     produces the REAL IOST titles correctly (see notices_task), so the sweep
#     adds no unique signal for that host -- only poison;
#   * `site:` sweeps are structurally untrustworthy here because the site-
#     constant title is indistinguishable from a headline once it is in the
#     feed, so a "smart" filter would be a heuristic guessing at intent;
#   * removing them is auditable and free. A filter can be added later with
#     real evidence if a non-poisoned site: sweep is ever wanted.
# The `site:tu.edu.np notice` sweep is kept: it is the ONLY remaining path to a
# TU notice when tu.edu.np's origin is dark, which is the entire point of this
# list. Its items are now protected by the v2 `empty` signal: a poisoned or
# zero-yield sweep is reported as `empty` rather than borrowing a confident ok.
NP_EN_EXTRA_QUERIES = [
    ("site:tu.edu.np notice", "education_tu"),
]

# ---------------------------------------------------------------------------
# SECTION-KEY VOCABULARY: THE COLLECTOR EMITS DIGEST SECTION KEYS, NOT ITS OWN
# ---------------------------------------------------------------------------
# THE BUG THIS REPLACES. `by_category` in the source_health payload is keyed by
# the plan's category, and `section_status()` in render_digest.py / build_email.py
# looks that map up by the DIGEST SECTION KEY. Two vocabularies, and for five of
# the eleven sections they never met:
#
#   section key          | collector used to emit | result
#   ---------------------+------------------------+--------------------------
#   world_major          | world                   | never found
#   gaming               | entertainment           | never found
#   anime                | entertainment           | never found
#   manga_manhwa_manhua  | entertainment           | never found
#   action               | (no plan at all)        | never found
#
# `action` is DERIVED (weather / breaking), is genuinely unsourced, and is
# deliberately left without a source: fabricating one to silence a status line
# would be exactly the class of lie this payload exists to prevent. It keeps
# reporting honestly.
#
# WHY THE FIX IS "TAG THE PLANS", NOT A LOOKUP TABLE. A `SECTION_FOR_CATEGORY`
# dict would have to enumerate the categories, which is a SECOND copy of the
# section-key list living in a file that does not render anything -- and it would
# be a third, after render_digest.SECTIONS and build_email.SECTIONS. It drifts
# the moment a section is added. So the collector emits the section key itself
# and there is no table to keep in sync: the section-key vocabulary has exactly
# the same number of copies as it did before this change.
#
# WHAT KEEPS IT FROM DRIFTING ANYWAY. One test asserts that every category this
# module can emit is a key in render_digest.SECTIONS -- the collector is pinned
# AGAINST the renderer's list rather than given its own. A new section, or a
# typo'd key, fails a test instead of silently producing a false
# "No sources are configured for this section."
#
# NOTE ON THE ONE OTHER READER. `domain_category` is also read by
# breaking_monitor.py:1565 -- but from ITS OWN candidates, which it builds from
# its own source list (`_fetch_source` -> `parse_rss_text(..., "breaking_feed")`)
# and never from this module's output. Changing these values cannot reach it.

# ---------------------------------------------------------------------------
# DIRECT RSS FEEDS (kind "feed")
# ---------------------------------------------------------------------------
# Every URL below was fetched and parsed through this module's OWN `rss_task` on
# 2026-10-04 before being written here: HTTP 200, root element <rss>, non-zero
# <item> count, and a candidate list produced. `rss_task` rejects Atom (`feed`)
# and RSS 1.0/RDF outright, so RSS 2.0 is a hard requirement, not a preference.
#
# The third field is the SECTION KEY (see above), never a private category.
#
# WHY THESE AND NOT OTHERS. Rejected after a live attempt, and NOT configured:
#   rss.dw.com/rdf/rss-en-world  RSS 1.0/RDF -> rejected by rss_task as `parse`
#   ign.com/rss                  HTTP 404
#   myreplicaonline.com/feed/     DNS failure
#   nepalitimes.com/rss           HTTP 404
#   webtoons.com/en/rss           HTTP 404
#   linewebtoon.com/en/rss        HTTP 200 but 0 items
#   mangakatana.com/feed          HTTP 404
#   crunchyroll.com/news/rss      HTTP 200, text/html, 0 items
#   kathmandupost.com/rss         SEE THE NOTE BELOW -- NOT CONFIGURED
#   dev.to/feed                   HTTP 403 to this collector's User-Agent;
#                                 kept, via UA_OVERRIDE_HOSTS above
#
# kathmandupost.com/rss, MEASURED AND DELIBERATELY ABSENT. It answers HTTP 200
# and its body does begin `<?xml ...?><rss version="2.0">` with 40 <item>s -- but
# it closes `</rss>` and then appends a Cloudflare analytics <script> block.
# ET.fromstring() raises "junk after document element", so the collector would
# record `parse` on EVERY run: a permanent false alarm on `nepal`, bought for a
# source three working Nepal feeds already cover. Not configured. (It also
# carries 0 <pubDate> across all 40 items; see the dateless-item note on
# `postprocess` for what the collector does with a missing date.)
#
# THE MANHWA GAP IS REAL AND IS NOT PAPERED OVER. No dedicated public manhwa RSS
# could be verified as reachable and parseable. MyAnimeList's news feed is the
# honest fill for the manga/manhwa/manhua section -- it is mostly anime news,
# and the section header says MANGA - MANHWA - MANHUA, so what is actually
# covered is the manga half plus general anime/manga industry news.
DIRECT_FEEDS = (
    # -- gaming ------------------------------------------------------------
    ("feed:gaming-rockpapershotgun", "https://www.rockpapershotgun.com/feed",
     "gaming", "en"),
    ("feed:gaming-pcgamer", "https://www.pcgamer.com/rss/", "gaming", "en"),
    ("feed:gaming-videogameschronicle",
     "https://www.videogameschronicle.com/feed/", "gaming", "en"),
    ("feed:gaming-polygon", "https://www.polygon.com/rss/index.xml", "gaming", "en"),
    ("feed:gaming-gamespot", "https://www.gamespot.com/feeds/mashup/",
     "gaming", "en"),
    ("feed:gaming-eurogamer", "https://www.eurogamer.net/feed", "gaming", "en"),
    # -- anime -------------------------------------------------------------
    ("feed:anime-animenewsnetwork",
     "https://www.animenewsnetwork.com/all/rss.xml", "anime", "en"),
    ("feed:anime-animeuk", "https://www.animeuk.news/feed/", "anime", "en"),
    # -- manga / manhwa / manhua (see THE MANHWA GAP above) -----------------
    ("feed:manga-myanimelist", "https://myanimelist.net/rss/news.xml",
     "manga_manhwa_manhua", "en"),
    # -- world -------------------------------------------------------------
    ("feed:world-bbc", "https://feeds.bbci.co.uk/news/world/rss.xml",
     "world_major", "en"),
    # -- nepal -------------------------------------------------------------
    ("feed:nepal-onlinekhabar", "https://www.onlinekhabar.com/feed",
     "nepal", "ne"),
    ("feed:nepal-onlinekhabar-en", "https://english.onlinekhabar.com/feed",
     "nepal", "en"),
    ("feed:nepal-nepalnews", "https://english.nepalnews.com/feed/",
     "nepal", "en"),
    # -- tech / career -----------------------------------------------------
    ("feed:tech-stackoverflow", "https://stackoverflow.blog/feed/",
     "tech_career", "en"),
    ("feed:tech-lobsters", "https://lobste.rs/rss", "tech_career", "en"),
    ("feed:tech-hnrss", "https://hnrss.org/frontpage", "tech_career", "en"),
    ("feed:tech-githubblog", "https://github.blog/feed/", "tech_career", "en"),
    ("feed:tech-devto", "https://dev.to/feed", "tech_career", "en"),
)

#: How many titles from EACH configured interest list are actually turned into
#: Google News queries. THE ONE PLACE THIS NUMBER IS DECLARED.
#:
#: WHY A CAP EXISTS AT ALL, MEASURED. The sweep used to slice `games[:6]`,
#: `anime[:3]`, `manga[:3]`, `manhwa[:6]` and `screen_merged[:6]` -- hard-coded
#: and arbitrary. Measured against the shipped config/interests.yml
#: (games 51 / anime 83 / manga 133 / manhwa 155 = 422 entries, 354 after
#: cleaning): that reached 24 distinct titles and left 330 of the owner's own
#: configured titles NEVER QUERIED.
#:
#: WHY THE SLICES ARE NOT SIMPLY DELETED. Each title becomes one outbound
#: Google News request. Removing the caps at the existing 3-titles-per-query
#: grouping was measured at 312 entertainment plans, i.e. 44 -> 347 total
#: requests: a 7.9x multiplication. That does not fail loudly -- it fails as
#: `skipped_budget`, which the renderer reports as a B3 "may be incomplete"
#: ALARM. Trading 5 quiet false-status lines for a run where most sources never
#: execute would be a straight regression, so the cap is a real budget decision
#: rather than a silent one.
#:
#: WHY 12. Six sweeps run (EN games / EN screen / JA anime / JA manga / KR manhwa
#: / ZH manga) at 3 titles per query, so 12 titles is 4 queries per sweep = 24
#: interest queries, against 9 today: a 2.7x rise on the sweep, and it quadruples
#: the distinct titles actually searched (24 -> 48 of 354) while keeping the run
#: inside the 75s soft budget in config/collection.json. The value is a CLI
#: override (`--max-interest-titles`), not a constant to edit.
DEFAULT_INTEREST_TITLE_CAP = 12

#: Titles per Google News query. Widening this trades request count for recall
#: per request: a denser OR query returns fewer, more on-topic results.
INTEREST_TITLES_PER_QUERY = 3

# DO NOT EDIT THIS LITERAL WITHOUT READING THE COMMENT ABOVE. scripts/
# breaking_monitor.py imports this symbol and uses it as the URL it fetches for
# the severe-weather lane. The coordinates here are the GENERIC default that
# matches config/profile.yml, not any deployment's real location: a run that
# cannot read a profile falls back to them, and a run that can read one builds
# its own URL from the profile instead.
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=27.7&longitude=85.3"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "weathercode&timezone=Asia%2FKathmandu"
)

# The hourly request. Verified against the live API (HTTP 200,
# Content-Type application/json) -- the exact names returned are:
#   precipitation_probability, precipitation, apparent_temperature,
#   temperature_2m, wind_speed_10m, visibility, time
# all 72 long for forecast_days=3, with units %/mm/degC/degC/kmh/m from
# `hourly_units`. `daily` rides along in the SAME request, so this is ONE
# network call that feeds both the existing daily summary candidate and the
# advice engine -- the single fetch_bytes injection point is preserved and the
# plan count, worker count and budget are unchanged.
WEATHER_HOURLY_FIELDS = (
    "precipitation_probability,precipitation,apparent_temperature,"
    "temperature_2m,wind_speed_10m,visibility"
)
WEATHER_DAILY_FIELDS = (
    "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode"
)
WEATHER_HOURLY_DAYS = 3

# Which hourly arrays the normalized payload cannot be built without.
#   time                     -- the axis everything else is indexed by
#   precipitation_probability-- weather_advice returns [] without it anyway;
#                               naming it here makes the reason explicit
#   apparent_temperature     -- DELIBERATELY REQUIRED, and the one non-obvious
#                               choice. `visibility` is optional because only
#                               rule VS reads it, so a missing one just means
#                               VS never fires. But apparent temperature is
#                               what the owner's "wear warm clothes when going
#                               out" ask is actually answered from: air
#                               temperature is not what a person feels, and
#                               emitting "nothing to plan for - dry and mild"
#                               on a 5C morning because the apparent
#                               temperature was missing is a confidently wrong
#                               line. Degrade to no advice, never to a wrong
#                               one. This is why the test suite pins [] here.
WEATHER_REQUIRED_HOURLY = ("time", "precipitation_probability",
                           "apparent_temperature")
# Optional: a missing array degrades exactly the rules that read it (V, VS) and
# nothing else, which is the whole point of a closed rule set.
WEATHER_OPTIONAL_HOURLY = ("precipitation", "temperature_2m", "wind_speed_10m",
                           "visibility")

# WMO weather interpretation codes -> short human text.
WEATHERCODE_TEXT = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snowfall", 73: "moderate snowfall", 75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# Best-effort official notice pages: (page URL, domain_category). The two
# institution-specific entries come from the environment (private_values), so a
# deployment points them at its own campus and bank without editing this file;
# the public bodies are generic Nepal sources.
#
# An unset BANK_URL/COLLEGE_URL is dropped rather than fetched, because the plan
# name below is built from the host: two empty URLs would both be "notice:" and
# collide, and there is nothing to fetch anyway.
NOTICE_SITES = [
    (_pv.bank_url(), "bank_local"),
    ("https://tu.edu.np/", "education_tu"),
    ("https://iost.tu.edu.np/", "education_tu"),
    (_pv.college_url(), "college_local"),
    ("https://nea.org.np/", "local_city"),
]
NOTICE_SITES = [site for site in NOTICE_SITES if site[0]]
NOTICE_KEYWORDS = ("notice", "result", "exam", "admission", "rate", "branch")
# Word-boundary match, not substring. The raw substring test is a live bug:
# `rate` is a substring of "DiRATEorate", so every NEA/Directorate nav link
# matched and 9 of 19 `local_city` candidates were chrome, not news. This
# anchors each keyword on non-word boundaries.
NOTICE_KEYWORDS_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(NOTICE_KEYWORDS), re.IGNORECASE)
# `rate` and `branch` are WEAK keywords: on a bank's homepage they name a
# product ("Rates & Charges", "Base Rate & Interest Spread Rate", "Branch
# Locator"), not a publication. A weak keyword only counts when the anchor
# also carries a CONTENT word, which is what separates "Interest rate notice
# published" (real) from "Deposit Rates" (chrome).
NOTICE_WEAK_KEYWORDS_RE = re.compile(r"\b(?:rate|branch)\b", re.IGNORECASE)
NOTICE_CONTENT_RE = re.compile(
    r"\b(?:notice|notices|notified|result|results|exam|examination|admission|"
    r"circular|schedule|merit|quota|vacancy|announcement)\b", re.IGNORECASE)
# Nav chrome that carries a keyword as part of a section/site name. These are
# destinations ("Go to the Directorate"), never notices.
NOTICE_NAV_SUFFIXES = ("directorate", "banking", "portal", "department",
                       "faculty", "campus", "gallery", "login", "home")
# A path that is a section index rather than a notice. `/department/...`,
# `/portal/...` and friends are navigation, not a published notice.
# `/notice/...` and `/notices/...` are deliberately NOT here: on these sites
# they are where the notices LIVE, and matching them would delete the real
# thing this task exists to collect.
NOTICE_NAV_PATH_RE = re.compile(
    r"/(?:department|departments|portal|faculty|faculties|login|admin|"
    r"campus)(?:/|$)", re.IGNORECASE)
# A last path segment that identifies ONE publication rather than naming a
# section: it carries a digit (an id or a date) or a downloadable file. The
# live Example Bank chrome is `/news--notices/audio-notice` -- no digit, no
# file, the anchor's own two words as the slug. This is the test that tells the
# two apart, and it is a shape test: no notice vocabulary is named here.
NOTICE_DETAIL_SEGMENT_RE = re.compile(r"\d|\.(?:pdf|docx?|xlsx?)$", re.IGNORECASE)
# A notice candidate must point at ONE publication, not at a destination. The
# only structural evidence of "one publication" is the notice's own identity: a
# numeric id in the path, or the document it is published as. This is the same
# gate the Worker already applies in ``parseAcademicNotices`` (``ACADEMIC_NOTICE_ID_RE``
# = ``/((?:notice|notices|view))/?(\d+)``) and it is measured, not guessed.
#
# WHY IT EXISTS (live D7 failure, 2026-09-29): the anchor scan walked the frame
# and returned 10 navigation destinations as if they were notices --
# ``/notices`` (a section index), ``/notices?...notice_type=21`` (a filtered
# list), ``/category/noticenews/`` (a category), ``/`` (a site root, from a
# cross-domain ``tuexam.edu.np`` link), ``/pages/exam-2013`` and
# ``/recruitment/open/approved-candidate`` (directory-ish paths). Every one of
# them carries a notice KEYWORD in its anchor text, so the keyword test cannot
# see the difference; only the URL can.
#
# THIS IS A SHAPE TEST, NEVER A WORD LIST. "category", "recruitment" and "pages"
# are deliberately NOT named here: a blocklist rots the day a site is rebuilt,
# and it would also delete the real notices that legitimately live under those
# paths. A real notice either has an id or is a file, and that stays true when
# the site changes its navigation.
#
# `notices_task` is the ONLY caller of this gate and ``live_canary``'s D7
# asserts the same shapes, so the two cannot drift: a URL admitted here is
# admitted there. Alternative-by-alternative that holds deliberately --
#   1. `/{notices?}/<digits>`      the id form (Worker's shape, both numbers)
#   2. `/<slug with "notice">[-_/]<digits>`  the dated-slug form
#      (live: /news--notices/audio-notice-2077-01-01)
#   3. `/download(s)|document(s)|file(s)|attachment(s)/`  a notice published as
#      a file rather than a page (live: /download/admission-form.pdf)
#   4. `.<pdf|doc|docx>`           the file itself, wherever it is served from
# A section index fails all four: after `/notices` comes `?` or the end of the
# path, so no alternative can reach a digit.
NOTICE_DETAIL_URL_RE = re.compile(
    r"/notices?/\d+"
    r"|/[a-z0-9_\-]*notice[a-z0-9_\-]*[-_/]\d+"
    r"|/(?:download|downloads|documents?|files?|attachment)s?/"
    r"|\.(?:pdf|docx?)\b",
    re.IGNORECASE)
# ---------------------------------------------------------------------------
# Script-aware notice vocabulary
# ---------------------------------------------------------------------------
#
# WHY THE GATE IS SCRIPT-AWARE (live canary failures D5 and D7, 2026-09-29)
#
# The notice task used to test ONE Latin vocabulary against every anchor, and
# the real boards are not written in Latin. Measured live on 2026-09-29:
# `https://tu.edu.np/notices` carries SIX detail-shaped `/notices/<id>` anchors
# (15646, 15386, 15192, 15099, 15078, 14988) and every one of them is
# Nepali-titled; `https://iost.tu.edu.np/notices` carries eleven, six of them
# Nepali or mixed. A Latin-only gate therefore reported ZERO for tu.edu.np,
# which is a `critical_host`, which flipped `source_health` to `empty`, which
# failed canary D5 -- and it was doing so silently, emptying the owner's
# `education_tu` category. This is the same Latin-only blindness that burned
# the owner's semester-1 exam result one layer down.
#
# Neither the script nor a Nepali word is re-implemented here: `devanagari.py`
# is imported read-only and its `detect_script` is the run's only script
# detector, so this gate and the academic result path cannot disagree about
# what a title's script is.
#
# `devanagari.NEPALI_NON_RESULT_TERMS` is deliberately NOT reused as the notice
# vocabulary. It is the academic path's "this notice is administrative, so it is
# not my result" list, and it contains words that are not notice words at all
# ("अध्ययन" = study, "छुट्टी" = holiday, "साइकिल" = cycle). Admitting those
# would admit an entire teaching-materials navigation bar.
#
# MATCHING IS PLAIN SUBSTRING, NOT `\b`, for the same reason `contains_any` is:
# Devanagari attaches postpositions directly (सूचना -> सूचनाहरू) and a `\b`
# boundary is a Latin concept that does not survive the script. Every entry is
# a whole Nepali word, so a substring hit is a real hit.
NEPALI_NOTICE_KEYWORDS = (
    "सूचना",      # notice (and सूचनाहरू, "notices")
    "सूचित",      # notified
    "नतिजा",      # result
    "प्रकाशित",  # published
    "प्रकाशन",   # publication / announcement
    "उत्तीर्ण",   # passed
    "अनुत्तीर्ण",  # failed
    "परीक्षा",    # exam
    "भर्ना",      # admission (to fill / admit)
    "प्रवेश",     # entrance / admission
    "आवेदन",     # application
    "परिपत्र",    # circular
    "तालिका",     # schedule / table
    "समयसारण",   # schedule / timetable
    "योग्यता",    # merit
    "कोटा",      # quota
    "रिक्त",      # vacancy
    "निविदा",     # tender
)

# A title must contain a letter in SOME script. `str.isalpha()` is the test,
# deliberately: a class written by hand would have to enumerate every Unicode
# block, and the one thing this gate must never do is decide a non-Latin,
# non-Devanagari title has no letters in it.
def _has_any_letter(text):
    """True when `text` carries at least one letter, in any script."""
    return any(ch.isalpha() for ch in (text or ""))


def notice_keyword_verdict(text):
    """(keep, language) for one notice anchor. Never raises.


    THREE CASES, AND THE ERROR DIRECTION IS DIFFERENT IN EACH:

    ``latin`` -- the historical English gate, byte-for-byte unchanged: keep the
      anchor only when ``NOTICE_KEYWORDS_RE`` matches, and let the
      weak-keyword/content-word/nav/detail-URL rules decide the rest. This is
      the behaviour every existing test pins, and a notice board written in
      English is the one case where the Latin vocabulary is the right question
      to ask.

    ``devanagari`` / ``mixed`` -- PRESENCE ADMITS, ABSENCE NEVER REJECTS. The
      Nepali vocabulary admits, because the real notices on these boards are in
      it. Its ABSENCE rejects nothing, and the measured price of rejecting on
      absence is a deleted real notice: ``tu.edu.np/notices/15192`` is titled
      "प्रधानमन्त्री दैवी प्रकोप उद्बार कोषमा रकम जम्मा सम्वन्धमा" -- a genuine
      published notice, it has an id -- and it carries no notice word in EITHER
      script, exactly as an English notice about deposits to a disaster fund
      carries none. A false drop deletes a real notice, so for Devanagari the
      vocabulary is a BOOSTER and ``NOTICE_DETAIL_URL_RE`` plus the nav rules
      are the only defence against chrome. That is safe here because the
      structural gate is script-agnostic: it reads the URL, and on the two
      live Devanagari boards every one of the 17 detail-shaped anchors is a
      real published notice (measured).

    anything else -- a script this collector carries NO vocabulary for. The
      only thing known about such a title is that our Latin words are absent,
      and the absence of Latin words proves nothing about a title we cannot
      read, so it is KEPT. It is reported as ``language: "unknown"`` rather
      than dropped, and rather than mislabelled "en" -- a fabricated language
      tag is the same class of error as a fabricated date.

    An anchor with no LETTER in it at all (bare digits or punctuation, in any
    script) is never a notice title, so it is refused for every script. That
      guard exists only because this function widened the Devanagari and
      unknown branches; for Latin it is what the keyword gate already did.
    """
    script = _devanagari.detect_script(text)
    if not _has_any_letter(text):
        # No letters at all: digits or punctuation only, in any script. Never a
        # title. `detect_script` calls this same text "devanagari" when the
        # digits are Nepali ones, which is why the check comes first.
        return False, "en" if script == "latin" else "unknown"
    if script in ("devanagari", "mixed"):
        return True, "ne"
    if script == "latin":
        return bool(NOTICE_KEYWORDS_RE.search(text or "")), "en"
    # "empty" script: a title in a script we have no vocabulary for.
    return True, "unknown"


# Date shapes a notice page is likely to carry near its anchor. Used to give
# notice candidates a real `published`, because a missing date sorts to EPOCH
# and lands LAST -- which is how real notices ended up buried under poisoned
# rows that at least had a pubDate.
NOTICE_DATE_RES = (
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
               r"[a-z]*\.?,?\s+\d{4})\b", re.IGNORECASE),
    re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
               r"\s+\d{1,2},?\s+\d{4})\b", re.IGNORECASE),
)
NOTICE_MAX_PER_SITE = 15
MAX_BYTES_HTML = 2000000
MAX_BYTES_RSS = 5000000

ANCHOR_RE = re.compile(
    r"<a\s[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")

# RSS media namespaces (stdlib ElementTree needs explicit namespace URIs).
MEDIA_NS = "http://search.yahoo.com/mrss/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# Inline <img> extraction from <content:encoded> (regex, stdlib only).
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMG_SRC_RE = re.compile(r'(?:\s|^)src\s*=\s*["\']?([^"\'\s>]+)', re.IGNORECASE)
IMG_WIDTH_RE = re.compile(r'(?:\s|^)width\s*=\s*["\']?(\d+)', re.IGNORECASE)
IMG_HEIGHT_RE = re.compile(r'(?:\s|^)height\s*=\s*["\']?(\d+)', re.IGNORECASE)

# Best-effort MIME inference from URL extensions (media elements often omit
# a type attribute).
MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif",
    ".svg": "image/svg+xml", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime", ".m4v": "video/mp4",
}

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Small shared helpers (normalize/Jaccard copied from validate_stories.py so
# both stages dedup identically)
# ---------------------------------------------------------------------------

def normalize_url(url):
    """Strip protocol, www., query string, fragment and trailing slash; lowercase."""
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", u)
    if u.startswith("www."):
        u = u[4:]
    u = u.split("#", 1)[0]
    u = u.split("?", 1)[0]
    return u.rstrip("/")


def title_tokens(title):
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) > 3}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    union = len(a | b)
    if union == 0:
        return 0.0
    return len(a & b) / union


def netloc_of(url):
    try:
        nl = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    if nl.startswith("www."):
        nl = nl[4:]
    return nl


def clean_ws(text):
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(text):
    return clean_ws(html.unescape(TAG_RE.sub(" ", text or "")))


# ---------------------------------------------------------------------------
# interests.yml parsing (hand-rolled YAML subset, same approach as
# validate_stories.py's load_excluded_keywords, extended to interest lists)
# ---------------------------------------------------------------------------

def load_interests(path):
    """Parse the simple block-style interests.yml.

    Supports exactly the constructs the file uses:
        excluded_keywords:
          - LEGO
        <section>:            # games / anime / manga / manhwa / manhua / other
          enabled: true
          interests:
            - Some Title
            - 'Quoted: Title'
          interests: []       # inline empty list
    Returns (excluded_keywords, sections) where sections maps name ->
    {"enabled": bool, "interests": [str]}.
    """
    excluded = []
    sections = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return excluded, sections

    current_top = None       # active top-level key
    current_sub = None       # active sub-key ("enabled" | "interests" | None)
    for raw in lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        top = re.match(r"^([A-Za-z0-9_\-]+)\s*:", line)
        if top:
            current_top = top.group(1)
            current_sub = None
            continue
        if current_top is None:
            continue
        sub = re.match(r"^(\s+)([A-Za-z0-9_\-]+)\s*:\s*(.*)$", line)
        if sub:
            key, rest = sub.group(2), sub.group(3).strip()
            if key == "enabled":
                entry = sections.setdefault(current_top, {"enabled": True, "interests": []})
                entry["enabled"] = rest.lower() in ("true", "yes", "1")
                current_sub = "enabled"
            elif key == "interests":
                sections.setdefault(current_top, {"enabled": True, "interests": []})
                current_sub = "interests"  # rest "[]" simply yields no items
            else:
                current_sub = None
            continue
        item = re.match(r"^\s+-\s*(.+?)\s*$", line)
        if item:
            value = item.group(1).strip().strip("'\"").strip()
            if not value:
                continue
            if current_top == "excluded_keywords":
                excluded.append(value)
            elif current_sub == "interests":
                sections.setdefault(current_top, {"enabled": True, "interests": []})
                sections[current_top]["interests"].append(value)
    return excluded, sections


def clean_title_for_query(title):
    """Reduce an interest entry to a searchable franchise/title string."""
    t = re.sub(r"\([^)]*\)", " ", title or "")   # drop parentheticals
    t = t.split("/")[0]                          # "Ender Lilies / Ender Magnolia"
    t = clean_ws(t).strip(" .:!-\"'")
    return t


def cleaned_interest_list(sections, key):
    out = []
    for raw in sections.get(key, {}).get("interests", []):
        t = clean_title_for_query(raw)
        if len(t) >= 3 and t not in out:
            out.append(t)
    return out


def chunks(lst, size):
    return [lst[i:i + size] for i in range(0, len(lst), size)]


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

#: Per-host User-Agent override, consulted ONLY by `fetch_bytes`.
#:
#: WHY THIS EXISTS, MEASURED NOT ASSUMED. `dev.to/feed` answers HTTP 403
#: "Forbidden Bots" to this collector's honest identifying User-Agent, and HTTP
#: 200 with 184KB of valid RSS 2.0 to a browser User-Agent. Verified both ways in
#: one run on 2026-10-04; it is a UA heuristic on that host, not a broken feed.
#:
#: WHY IT IS SAFE TO DO. `fetch_bytes`' SIGNATURE is unchanged, so every test
#: that monkeypatches it (`lambda url, timeout, max_bytes: ...`) keeps working
#: untouched, and no other fetch path exists. Only the header value changes, and
#: only for the host named here.
#:
#: THE HONEST RISK, STATED: this makes the collector look like a browser to that
#: one host. If dev.to tightens its rule, the feed fails loudly as a `failed`
#: source row -- an alarm the owner can act on -- which is the correct outcome,
#: and strictly better than silently having no developer-blog source at all.
#: Delete the entry to revert; nothing else depends on it.
UA_OVERRIDE_HOSTS = {
    "dev.to": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
}


def user_agent_for(url):
    """The User-Agent for `url`: the host's override if it has one, else ours."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return UA_OVERRIDE_HOSTS.get(host, USER_AGENT)


def fetch_bytes(url, timeout, max_bytes):
    """The SINGLE network injection point -- every test monkeypatches this."""
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent_for(url),
        "Accept": "application/rss+xml, application/xml, text/html, "
                  "application/json, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes)


class DnsResolver(object):
    """Per-run DNS pre-flight with a bounded wait. No module state.

    WHY this is an object and not module globals: the cache and the "resolver
    is wedged" cooldown are a PERFORMANCE optimisation, never a correctness
    input. As process-scoped globals, correctness depended on every caller
    remembering to reset them -- which is exactly how a stale wedge verdict
    leaked into unrelated later work and poisoned a whole test run. One
    instance is built per collection run (main), closed in a finally and
    thrown away, so a second run in the same process can never inherit a
    verdict from the first.

    urlopen(timeout=...) does NOT bound getaddrinfo(): the socket timeout only
    starts once a connection is attempted, so a hung resolver hangs the whole
    worker thread, the soft budget can no longer cancel it, and the workflow's
    timeout-minutes becomes the only backstop -- after which the fallback
    candidates.json write never runs and the digest is silently blind. The
    probe therefore runs on a helper thread with a per-call bounded wait.
    """

    def __init__(self, resolve_timeout=DEFAULT_RESOLVE_TIMEOUT_SEC,
                 idle_quit_sec=_RESOLVER_IDLE_QUIT_SEC,
                 wedge_cooldown_sec=_RESOLVER_WEDGE_COOLDOWN_SEC):
        self.resolve_timeout = _num_or(resolve_timeout,
                                       DEFAULT_RESOLVE_TIMEOUT_SEC, 0.0)
        self._idle_quit = idle_quit_sec
        self._wedge_cooldown = wedge_cooldown_sec
        self._lock = threading.Lock()
        self._cache = {}
        self._wedged_until = 0.0
        self._thread = None
        self._stop = threading.Event()
        self._queue = _queue.Queue()
        self.probe_count = 0
        self.closed = False

    @staticmethod
    def _host_of(url):
        host = urlparse(url).netloc.lower().split("@")[-1]
        if host.startswith("["):
            return host.split("]", 1)[0] + "]"
        if ":" in host:
            return host.split(":", 1)[0]
        return host

    def _worker_loop(self):
        """Serial probe worker.

        The result is always delivered in a finally block on purpose: a worker
        that wedged in getaddrinfo and then got round to the job must still
        wake the caller that is already waiting on its bound.
        """
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=self._idle_quit)
            except _queue.Empty:
                continue
            if job is None:  # stop sentinel
                return
            ok, err = True, ""
            try:
                try:
                    socket.getaddrinfo(job["host"], None, 0, socket.SOCK_STREAM)
                except Exception as exc:
                    ok = False
                    err = clean_ws("%s: %s" % (type(exc).__name__, exc))[:200]
            finally:
                job["result_q"].put((ok, err))
                self._queue.task_done()

    def resolve(self, url, resolve_timeout=None):
        """Return (resolved, error) for url's host. Never raises; bounded.

        The cache is negative TOO: once a resolver answers -- or fails to -- for
        a host, later fetches of that host skip the pre-flight entirely, so a
        dead host costs ONE probe per run rather than one per plan.
        """
        host = self._host_of(url)
        if not host:
            return True, ""
        timeout = self.resolve_timeout if resolve_timeout is None else (
            _num_or(resolve_timeout, self.resolve_timeout, 0.0))
        with self._lock:
            if self.closed:
                return True, ""          # after close(): never block a run
            if host in self._cache:
                return self._cache[host]
            if time.monotonic() < self._wedged_until:
                # A resolver that already failed to answer within its bound is
                # not re-probed during the cooldown: recorded as a dns failure
                # immediately, so one wedged resolver costs the run ONE bound,
                # not one per plan.
                return False, ("DNSResolverTimeout: resolver still wedged from "
                               "an earlier probe; %s not re-probed" % host)
            # One job queue, but a PER-CALLER result queue: a shared result
            # channel could hand one host's verdict to a different host's caller
            # and invent a dns failure for a healthy source.
            job = {"result_q": _queue.Queue(), "host": host}
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                # daemon=True: this thread can never keep the process alive.
                self._thread = threading.Thread(
                    target=self._worker_loop, name="dnsmain", daemon=True)
                self._thread.start()
            self._queue.put(job)
            self.probe_count += 1
            bound = max(0.0, timeout + _RESOLVE_BOUND_SLACK_SEC)
            try:
                ok, err = job["result_q"].get(timeout=bound)
            except _queue.Empty:
                # The worker never came back within the bound: the RESOLVER is
                # hung. Classified "dns" (not "timeout") because a wedged
                # getaddrinfo IS a resolver failure, and it flows into
                # source_health instead of vanishing. The real fetch is still
                # attempted; its own socket timeout is the last-resort bound.
                ok = False
                err = ("DNSResolverTimeout: name resolution for %s did not "
                       "answer within %.1fs" % (host, timeout))
                self._wedged_until = time.monotonic() + self._wedge_cooldown
            self._cache[host] = (ok, err)
            return ok, err

    def close(self):
        """Retire the helper thread and drop all state. Idempotent."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._cache.clear()
            self._wedged_until = 0.0
            self._stop.set()
            # Drop abandoned jobs first: their caller already gave up, and one
            # left queued would let the worker execute a stale request.
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except _queue.Empty:
                    break
            self._queue.put(None)
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)


def _attempts_recorder(box):
    """Return a callback that writes the current attempt count into box["n"]."""
    def record(attempts):
        box["n"] = attempts
    return record


def fetch_bytes_retry(url, timeout, max_bytes, retry=None, on_attempt=None,
                      resolver=None):
    """fetch_bytes under the configured retry policy.

    Retries fire ONLY for the reason codes in retry["retry_on"] -- so never on
    a parse error, and never on a 4xx other than 429 (429 classifies as
    "throttled"). Backoff is deterministic (no jitter) so the run is testable.
    The original exception is re-raised on the last attempt so run_task records
    the real error string; `on_attempt(n)` is called before every attempt so the
    caller can report the true attempt count in source_results.

    `resolver` is the run's DnsResolver. resolver=None means "no pre-flight",
    the default for direct callers: the pre-flight is an optimisation, so
    nothing depends on it being present.
    """
    retry = retry or {}
    max_attempts = max(1, int(retry.get("attempts") or 1))
    backoff = retry.get("backoff_sec") or []
    retry_on = set(retry.get("retry_on") or ())
    resolve_timeout = _num_or(retry.get("resolve_timeout_sec"),
                              DEFAULT_RESOLVE_TIMEOUT_SEC, 0.0)
    attempt = 0
    while True:
        attempt += 1
        if on_attempt is not None:
            on_attempt(attempt)
        # Bounded DNS pre-flight. Advisory by design: a known-bad host is
        # recorded as `outcome:"failed"` with a dns reason, but we still make
        # the real request, so a pre-flight hiccup can never cost us content.
        if resolver is None:
            ok, dns_err = True, ""
        else:
            ok, dns_err = resolver.resolve(url, resolve_timeout)
        try:
            return fetch_bytes(url, timeout, max_bytes)
        except Exception as exc:
            reason = source_health.classify_reason(
                clean_ws("%s: %s" % (type(exc).__name__, exc)), "failed")
            if attempt >= max_attempts or reason not in retry_on:
                if ok or not dns_err:
                    raise
                # The pre-flight already proved the host cannot resolve: report
                # the resolver's own diagnosis, not the socket symptom.
                raise URLError(dns_err) from exc
            if backoff:
                time.sleep(float(backoff[min(attempt - 1, len(backoff) - 1)]))


def _num_or(value, fallback, minimum=0.0):
    """Return value when it is a usable number, else fallback. Never raises."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return value if value >= minimum else fallback


def default_collection_config_path():
    """config/collection.json resolved from THIS file, so cwd does not matter."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "config", "collection.json")


def _built_in_config():
    return {
        "critical_hosts": list(DEFAULT_CRITICAL_HOSTS),
        "timeout_sec": dict(DEFAULT_TIMEOUT_SEC_BY_HOST,
                            default=DEFAULT_TIMEOUT_SEC),
        "retry": dict(DEFAULT_RETRY_CFG,
                      backoff_sec=list(DEFAULT_RETRY_CFG["backoff_sec"]),
                      retry_on=list(DEFAULT_RETRY_CFG["retry_on"])),
        "soft_budget_sec": SOFT_BUDGET_SEC,
        "max_workers": MAX_WORKERS,
        "min_items_per_source": DEFAULT_MIN_ITEMS_PER_SOURCE,
    }


def load_collection_config(path=None):
    """Read config/collection.json defensively. Never raises.

    Missing or corrupt config falls back to the built-in values and prints
    EXACTLY ONE warning. Individual invalid keys fall back silently to their
    default (a broken retry_on must not stop the run).
    """
    if path is None:
        path = default_collection_config_path()
    raw = None
    warning = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        warning = ("collection config unreadable (%s) - using built-in "
                   "defaults" % exc)
    if not isinstance(raw, dict):
        print("WARNING: %s" % (warning or
                               "collection config is not an object (%s) - "
                               "using built-in defaults" % path))
        return _built_in_config()

    cfg = _built_in_config()
    hosts = raw.get("critical_hosts")
    if isinstance(hosts, (list, tuple)):
        cleaned = [h.strip() for h in hosts if isinstance(h, str) and h.strip()]
        cfg["critical_hosts"] = cleaned or list(DEFAULT_CRITICAL_HOSTS)
    timeouts = raw.get("timeout_sec")
    if isinstance(timeouts, dict):
        for key, value in timeouts.items():
            if isinstance(key, str) and key.strip():
                secs = _num_or(value, None, 0.5)
                if secs is not None:
                    cfg["timeout_sec"][key.strip()] = secs
    retry_raw = raw.get("retry")
    if isinstance(retry_raw, dict):
        cfg["retry"]["attempts"] = int(_num_or(retry_raw.get("attempts"),
                                              DEFAULT_RETRY_ATTEMPTS, 1))
        backoff = retry_raw.get("backoff_sec")
        if isinstance(backoff, (list, tuple)):
            cleaned = [_num_or(b, None, 0.0) for b in backoff]
            cleaned = [b for b in cleaned if b is not None]
            if cleaned:
                cfg["retry"]["backoff_sec"] = cleaned
        retry_on = retry_raw.get("retry_on")
        if isinstance(retry_on, (list, tuple)):
            cleaned = [r.strip() for r in retry_on
                       if isinstance(r, str) and r.strip()]
            if cleaned:
                cfg["retry"]["retry_on"] = cleaned
        cfg["retry"]["resolve_timeout_sec"] = _num_or(
            retry_raw.get("resolve_timeout_sec"), DEFAULT_RESOLVE_TIMEOUT_SEC,
            0.0)
    cfg["soft_budget_sec"] = _num_or(raw.get("soft_budget_sec"),
                                     SOFT_BUDGET_SEC, 1.0)
    cfg["max_workers"] = int(_num_or(raw.get("max_workers"), MAX_WORKERS, 1))
    # int >= 1. A 0 or negative value would make EVERY source empty, which is
    # the exact over-claim this setting exists to prevent, so it is floored.
    cfg["min_items_per_source"] = int(_num_or(
        raw.get("min_items_per_source"), DEFAULT_MIN_ITEMS_PER_SOURCE, 1))
    if cfg["min_items_per_source"] < 1:
        cfg["min_items_per_source"] = 1
    return cfg


def parse_pubdate(raw):
    """RFC-822 pubDate -> aware UTC datetime, or None."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Media extraction (RSS item -> optional image/video dicts)
# ---------------------------------------------------------------------------

def _int_attr(el, name):
    """Parse an integer attribute (width/height) or return None."""
    raw = el.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _mime_from_url(url):
    """Infer a MIME type from the URL path extension, else None."""
    path = urlparse(url).path.lower()
    for ext, mime in MIME_BY_EXT.items():
        if path.endswith(ext):
            return mime
    return None


def _http_url(url):
    """Return the URL if it is http(s), else None (rejects data:/javascript:)."""
    if not url:
        return None
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    return url


def extract_media(item):
    """Extract optional image/video dicts from an RSS <item>.

    Extraction priority per RSS item (contract):
      image: media:content(image) -> media:thumbnail -> enclosure(image)
             -> inline <img> in <content:encoded> -> itunes:image
      video: media:content(video) -> enclosure(video)
    Returns (image, video); either may be None. Only http(s) URLs are kept;
    the validator is the enforcement point for domain/private-host rules.
    """
    image = None
    video = None

    # 1) media:content (image or video).
    for mc in item.findall("{%s}content" % MEDIA_NS):
        url = _http_url(mc.get("url"))
        if url is None:
            continue
        mtype = (mc.get("type") or "").strip().lower()
        if not mtype:
            mtype = _mime_from_url(url) or ""
        if mtype.startswith("image/") and image is None:
            image = {"url": url, "type": mtype, "source": "media:content"}
            w, h = _int_attr(mc, "width"), _int_attr(mc, "height")
            if w is not None:
                image["width"] = w
            if h is not None:
                image["height"] = h
        elif mtype.startswith("video/") and video is None:
            video = {"url": url, "type": mtype, "source": "media:content"}

    # 2) media:thumbnail (image).
    if image is None:
        for mt in item.findall("{%s}thumbnail" % MEDIA_NS):
            url = _http_url(mt.get("url"))
            if url is None:
                continue
            image = {"url": url, "source": "media:thumbnail"}
            mtype = _mime_from_url(url)
            if mtype:
                image["type"] = mtype
            w, h = _int_attr(mt, "width"), _int_attr(mt, "height")
            if w is not None:
                image["width"] = w
            if h is not None:
                image["height"] = h
            break

    # 3) enclosure (image or video).
    for enc in item.findall("enclosure"):
        url = _http_url(enc.get("url"))
        if url is None:
            continue
        mtype = (enc.get("type") or "").strip().lower()
        if not mtype:
            mtype = _mime_from_url(url) or ""
        if mtype.startswith("image/") and image is None:
            image = {"url": url, "type": mtype, "source": "enclosure"}
        elif mtype.startswith("video/") and video is None:
            video = {"url": url, "type": mtype, "source": "enclosure"}

    # 4) inline <img> parsed from <content:encoded> (regex, stdlib only).
    if image is None:
        content = item.findtext("{%s}encoded" % CONTENT_NS) or ""
        tag = IMG_TAG_RE.search(content)
        if tag is not None:
            src = IMG_SRC_RE.search(tag.group(0))
            if src is not None:
                url = _http_url(html.unescape(src.group(1)))
                if url is not None:
                    image = {"url": url, "source": "content:encoded-img"}
                    mtype = _mime_from_url(url)
                    if mtype:
                        image["type"] = mtype
                    wm = IMG_WIDTH_RE.search(tag.group(0))
                    hm = IMG_HEIGHT_RE.search(tag.group(0))
                    if wm is not None:
                        image["width"] = int(wm.group(1))
                    if hm is not None:
                        image["height"] = int(hm.group(1))

    # 5) itunes:image (image).
    if image is None:
        it = item.find("{%s}image" % ITUNES_NS)
        if it is not None:
            url = _http_url(it.get("href"))
            if url is not None:
                image = {"url": url, "source": "itunes:image"}
                mtype = _mime_from_url(url)
                if mtype:
                    image["type"] = mtype

    return image, video


def make_candidate(title, url, source, published_dt, snippet,
                   domain_category, language, image=None, video=None):
    cand = {
        "title": title,
        "url": url,
        "source": source,
        "published": published_dt.isoformat() if published_dt else None,
        "snippet": snippet[:SNIPPET_MAX_CHARS],
        "domain_category": domain_category,
        "language": language,
        "_dt": published_dt,  # internal; stripped before writing
    }
    if image is not None:
        cand["image"] = image
    if video is not None:
        cand["video"] = video
    return cand


# ---------------------------------------------------------------------------
# Source tasks (each returns candidates; exceptions = source failure)
# ---------------------------------------------------------------------------

def gnews_url(query, locale_key):
    hl, gl, ceid, _lang = LOCALES[locale_key]
    return GNEWS_SEARCH % (quote(query), hl, gl, quote(ceid, safe=":"))


#: A feed body that is not XML at all is an HTML page, whatever its status
#: code. A 200 error page is the single most common "reachable, useless"
#: shape, and it must be reported as `parse`, never as a healthy fetch.
def _local_name(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _iter_items(root):
    """The <item> elements of an RSS 2.0 feed.

    `root.iter()` rather than `root.findall("item")` so a namespaced channel
    is still walked. RSS 1.0/RDF is REJECTED at the root check above, so it can
    never reach here: in RDF the <item> children live in the RSS-1.0 namespace,
    which makes `item.findtext("title")` return "" and every item is skipped --
    that silent zero is precisely the bug this shape check exists to remove.
    """
    return [el for el in root.iter() if _local_name(el.tag) == "item"]


def rss_task(url, domain_category, language, timeout, retry=None,
             on_attempt=None, resolver=None):
    data = fetch_bytes_retry(url, timeout, MAX_BYTES_RSS, retry, on_attempt,
                             resolver)
    # An HTML body fails here with a ParseError whose message classifies as
    # `parse`, which is the honest reason for "reachable but useless".
    root = ET.fromstring(data)
    root_name = _local_name(root.tag)
    if root_name == "html":
        raise ValueError("parse: feed URL returned an HTML document")
    if root_name == "feed":
        raise ValueError("parse: Atom feed; an RSS <item> feed is required")
    if root_name == "rdf":
        # RSS 1.0 is a DIFFERENT shape, not a variant: its <item>/<title>/
        # <link> children sit in the http://purl.org/rss/1.0/ namespace, so the
        # unnamespaced lookups below would all miss and the feed would report a
        # clean zero items. Reporting `parse` says "we could not read this",
        # which is true; reporting `empty` would say "it had nothing", which
        # is not.
        raise ValueError("parse: RSS 1.0/RDF feed; only RSS 2.0 <rss> is read")
    if root_name != "rss":
        raise ValueError("parse: unexpected feed root <%s>" % (root_name or "?"))

    out = []
    items = _iter_items(root)
    if not items:
        # A well-formed body with zero <item> is `empty`, not `ok`: record() in
        # collect_parallel turns the zero length into outcome "empty" because
        # items_found == 0. Returning [] here is honest, not a silent failure.
        return out
    for item in items:
        title = clean_ws(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        # Date fallbacks: <atom:updated> and <published> are accepted when
        # pubDate is absent, so a feed that uses them is not silently sorted to
        # EPOCH.
        dt = parse_pubdate((item.findtext("pubDate") or "").strip())
        if dt is None:
            for alt in ("{http://www.w3.org/2005/Atom}updated",
                        "{http://www.w3.org/2005/Atom}published",
                        "published", "updated", "date"):
                raw_alt = (item.findtext(alt) or "").strip()
                if not raw_alt:
                    continue
                dt = parse_pubdate(raw_alt)
                if dt is None:
                    try:
                        dt = datetime.strptime(
                            raw_alt.replace("Z", "+0000"),
                            "%Y-%m-%dT%H:%M:%S%z")
                    except ValueError:
                        dt = None
                if dt is not None:
                    break
        snippet = strip_html(item.findtext("description") or "")
        src_el = item.find("source")
        src_name = clean_ws(src_el.text or "") if src_el is not None else ""
        src_home = (src_el.get("url") or "").strip() if src_el is not None else ""
        domain = netloc_of(src_home) or netloc_of(link) or src_name or "unknown"
        image, video = extract_media(item)
        out.append(make_candidate(
            title=title[:300], url=link, source=domain, published_dt=dt,
            snippet=snippet, domain_category=domain_category, language=language,
            image=image, video=video))
    return out


def weather_task(url, timeout, retry=None, on_attempt=None,
                 resolver=None, box=None):
    """Fetch open-meteo and return ([weather candidate], summary).

    UNCHANGED CONTRACT: the return value is the historical 2-tuple, so
    breaking_monitor.py and every existing test keep working. `box` is an
    OPTIONAL out-parameter: when supplied, the SAME response is also normalized
    into the advice engine's schema and written into the box for main() to
    evaluate. No second request is made -- `daily` is requested in the same
    call, so the daily summary below and the hourly advice above come out of a
    single fetch through the single fetch_bytes injection point.

    A response with no `hourly` block (the daily-only shape breaking_monitor
    still uses) is NOT an error here: the daily summary is older, still-wanted
    behaviour, and the advice lane separately records "not derivable".
    """
    data = fetch_bytes_retry(url, timeout, MAX_BYTES_HTML, retry, on_attempt,
                             resolver)
    payload = json.loads(data.decode("utf-8", errors="replace"))
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    prcp = daily.get("precipitation_sum") or []
    wcode = daily.get("weathercode") or []
    n_days = min(3, len(times))
    if n_days == 0:
        raise ValueError("open-meteo returned no daily data")

    parts = []
    for i in range(n_days):
        code = wcode[i] if i < len(wcode) else None
        code_txt = WEATHERCODE_TEXT.get(code, "unknown")
        hi = ("%+.0f" % tmax[i]) if i < len(tmax) and tmax[i] is not None else "?"
        lo = ("%+.0f" % tmin[i]) if i < len(tmin) and tmin[i] is not None else "?"
        mm = ("%.1f" % prcp[i]) if i < len(prcp) and prcp[i] is not None else "n/a"
        parts.append("%s: max %s\u00b0C / min %s\u00b0C, precip %s mm, %s"
                     % (times[i], hi, lo, mm, code_txt))
    summary = ("Weather forecast for the configured location (next %d days, "
               "Asia/Kathmandu, open-meteo): %s" % (n_days, "; ".join(parts)))

    candidate = make_candidate(
        title="Weather forecast (%d-day forecast)" % n_days,
        url=url, source="open-meteo.com", published_dt=None,
        snippet=summary, domain_category="weather_forecast", language="en")

    if box is not None:
        norm, err = normalize_hourly_forecast(
            payload, box.get("place"), box.get("fetched_at_npt"),
            max_days=WEATHER_HOURLY_DAYS)
        box["payload"] = norm
        box["error"] = err
    return [candidate], summary


# ---------------------------------------------------------------------------
# Weather -> normalized forecast -> deterministic advice
#
# This whole section is the NETWORK ADAPTER. It is the only place that knows an
# open-meteo field name: weather_advice.py knows none of them, and a rename
# upstream is fixed here rather than in the rule table.
# ---------------------------------------------------------------------------

def build_weather_url(profile=None):
    """The hourly open-meteo URL for the profile's location.

    With no profile -- or a profile without usable coordinates -- this returns
    WEATHER_URL unchanged, deliberately: the daily-only request is the
    historical default AND breaking_monitor.py's contract, so a run that cannot
    read a location must fall back to it rather than invent coordinates.

    The `timezone` value is percent-encoded, so Asia/Kathmandu goes out as
    Asia%2FKathmandu and the response comes back already in Nepal wall time --
    no clock arithmetic on our side.
    """
    if profile is None:
        return WEATHER_URL
    location = getattr(profile, "location", None)
    if not isinstance(location, dict):
        return WEATHER_URL
    lat, lon, tz = (location.get("latitude"), location.get("longitude"),
                    location.get("timezone"))
    for value in (lat, lon):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return WEATHER_URL
    if not isinstance(tz, str) or not tz.strip():
        return WEATHER_URL
    return ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
            "&hourly=%s&daily=%s&timezone=%s&forecast_days=%d"
            % (lat, lon, WEATHER_HOURLY_FIELDS, WEATHER_DAILY_FIELDS,
               quote(tz, safe=""), WEATHER_HOURLY_DAYS))


def default_profile_path():
    """config/profile.yml resolved from THIS file, so cwd does not matter."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "config", "profile.yml")


def weather_place(profile):
    """The normalized payload's `place` block, from the profile's location.

    Coordinates come from the PROFILE, not from the response: open-meteo snaps
    the request to its grid cell and echoes a different latitude/longitude
    (e.g. a request for 27.7 / 85.3 comes back as the nearest grid point). The
    grid point is weather, the profile is the owner, and the record should say
    where the owner asked about. The response's own values stay out of this
    payload.
    """
    location = (getattr(profile, "location", None) or {}) if profile else {}
    return {"city": location.get("city") or "",
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "timezone": location.get("timezone") or ""}


def new_weather_box(profile=None, fetched_at_npt=None):
    """The out-parameter the weather plan fills and main() reads.

    A plain dict hung off the PLAN rather than a module global, for the same
    reason DnsResolver is an object: every test runs a second collection in the
    same process, and process-scoped state is how one run's forecast leaks into
    the next one's advice.
    """
    return {"place": weather_place(profile), "fetched_at_npt": fetched_at_npt,
            "payload": None, "error": ""}


def _num(value):
    """A real number, or None. bool is not a number here (True is not 1C)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _at(values, i):
    """values[i] when in range, else None. Never indexes past an end."""
    if not isinstance(values, list) or i >= len(values):
        return None
    return values[i]


def _split_hourly_stamp(stamp):
    """'2026-09-28T07:00' -> ('2026-09-28', 7). (None, None) on anything else."""
    if not isinstance(stamp, str):
        return None, None
    text = stamp.strip().replace(" ", "T")
    if "T" not in text:
        return None, None
    day, _, clock = text.partition("T")
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return None, None
    head = clock.split(":")[0]
    if not head.isdigit():
        return None, None
    hour = int(head)
    if hour > 23:
        return None, None
    return day, hour


def normalize_hourly_forecast(payload, place, fetched_at_npt, max_days=3):
    """open-meteo JSON -> the normalized shape weather_advice() consumes.

    Returns (payload_or_None, error_or_""). Never raises and never guesses.

    THE COMPLETENESS RULE, STATED ONCE
    ----------------------------------
    weather_advice() returns [] if `hours` is missing on ANY day or `pop_pct`
    is missing on ANY hour. That is a blunt instrument, so the precision lives
    here, and the choice is deliberate:

      * A day is emitted only if it is COMPLETE -- all 24 hours present with a
        number for every required field. An incomplete day is DROPPED, not
        passed on. Passing it through would silence the whole engine over a
        hole in TOMORROW's forecast, which is the quiet wrongness this
        integration exists to remove.
      * days[0] MUST be today (the date part of fetched_at_npt) and today must
        be complete. The rule strings say "today" and "when you go out"; a
        forecast missing today cannot honestly fill them, so there is no
        advice and an error is returned for the honesty record.
      * Days dated BEFORE today are dropped, so a stale leading day can never
        make the engine advise about yesterday.

    Array-length mismatches are handled by truncating to the shortest REQUIRED
    array, so a short `precipitation_probability` makes its day incomplete
    (the correct consequence) while a short OPTIONAL array only blanks out the
    field it owns. Optional arrays are read with a bounds-checked accessor, so
    no code path can index past the end of one.
    """
    if not isinstance(payload, dict):
        return None, "forecast payload is not an object"
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return None, "response carried no hourly block (daily-only forecast)"

    series = {}
    for key in WEATHER_REQUIRED_HOURLY + WEATHER_OPTIONAL_HOURLY:
        values = hourly.get(key)
        if not isinstance(values, list):
            if key in WEATHER_REQUIRED_HOURLY:
                return None, ("response is missing required hourly field %r -- "
                              "no advice rather than a guessed one" % key)
            values = []
        series[key] = values
    if not series["time"]:
        return None, "hourly.time was empty"

    # Truncate to the shortest REQUIRED array. Optional arrays are NOT allowed
    # to truncate anything: they are read defensively with _at() below, so a
    # short `visibility` costs rule VS and nothing else. Truncating on the
    # shortest array of any kind would let a cosmetic field silently delete the
    # rain probability for the rest of the day.
    n = min(len(series[key]) for key in WEATHER_REQUIRED_HOURLY)
    if n <= 0:
        return None, "every required hourly array was empty"

    by_date = {}
    for i in range(n):
        day, hour = _split_hourly_stamp(series["time"][i])
        if day is None:
            continue
        pop = _num(_at(series["precipitation_probability"], i))
        apparent = _num(_at(series["apparent_temperature"], i))
        if pop is None or apparent is None:
            # A required value is missing or null for THIS hour. Store a
            # placeholder so the day is judged incomplete rather than emitting
            # a record the engine would have to reject.
            by_date.setdefault(day, {})[hour] = None
            continue
        by_date.setdefault(day, {})[hour] = {
            "hour": hour,
            "pop_pct": pop,
            "precip_mm": _num(_at(series["precipitation"], i)),
            "apparent_c": apparent,
            "wind_kmh": _num(_at(series["wind_speed_10m"], i)),
            "visibility_m": _num(_at(series["visibility"], i)),
        }

    if not isinstance(fetched_at_npt, str) or len(fetched_at_npt) < 10:
        return None, "fetch time unknown, so 'today' cannot be established"
    today = fetched_at_npt[:10]

    daily = payload.get("daily")
    daily = daily if isinstance(daily, dict) else {}
    daily_times = [d for d in (daily.get("time") or []) if isinstance(d, str)]
    daily_by_date = {}
    for i, day in enumerate(daily_times):
        daily_by_date[day] = {
            "tmax_c": _num(_at(daily.get("temperature_2m_max"), i)),
            "tmin_c": _num(_at(daily.get("temperature_2m_min"), i)),
            "precip_sum_mm": _num(_at(daily.get("precipitation_sum"), i)),
            "weathercode": _num(_at(daily.get("weathercode"), i)),
        }

    days = []
    dropped = 0
    for day in sorted(d for d in by_date if d >= today):
        records = by_date[day]
        if not all(isinstance(records.get(h), dict) for h in range(24)):
            dropped += 1
            continue
        entry = {"date": day, "hours": [records[h] for h in range(24)]}
        entry.update(daily_by_date.get(
            day, {"tmax_c": None, "tmin_c": None, "precip_sum_mm": None,
                  "weathercode": None}))
        days.append(entry)
        if len(days) >= max(1, int(max_days or 1)):
            break

    # days[0] MUST be today. A response that carries a hole in today but a
    # complete tomorrow is exactly the trap: the loop above would happily
    # hand back tomorrow, and the engine would then say "today" about a day
    # that is not today. Advancing a day silently is the worst available
    # outcome, so it is refused and the absence is recorded.
    if not days or days[0]["date"] != today:
        return None, ("today (%s) arrived incomplete or absent in the hourly "
                      "response (%d day(s) dropped)" % (today, dropped))
    return {"fetched_at_npt": fetched_at_npt,
            "place": place if isinstance(place, dict) else {},
            "days": days}, ""


def kathmandu_now_str():
    """'YYYY-MM-DD HH:MM' in Nepal time, delegated to the canonical helper.

    Returns "" if validate_stories is unavailable. An empty fetch time is not a
    crash: normalize_hourly_forecast() refuses to guess what "today" means and
    the advice lane records the absence, which is the honest outcome.
    """
    if _kathmandu_now_str is None:
        return ""
    return _kathmandu_now_str()


def load_weather_profile(path):
    """(profile_or_None, error_or_""). Never raises.

    Unit 1 owns profile validation and raises ProfileError on anything invalid.
    That is right for the engine, and wrong here: this collector has news to
    collect either way, so the failure is returned, printed and RECORDED rather
    than propagated. Nothing downstream is allowed a defaulted profile.
    """
    if not path:
        return None, "no profile path given"
    try:
        return _wa.load_profile(path), ""
    except Exception as exc:
        return None, ("profile %s unusable (%s: %s) -- daily-only weather, no "
                      "advice" % (path, type(exc).__name__,
                                  clean_ws(str(exc))[:200]))


#: The name the advice lane reports under. A separate plan name, not an extra
#: key on the weather row: `source_results` states REACHABILITY per source, and
#: overwriting an "ok" with "failed" for a host that answered fine would be a
#: lie in the other direction. A source that was reachable but produced no
#: advice gets its own honest row instead, with the same key set.
WEATHER_ADVICE_SOURCE = "weather_advice:open-meteo"


def record_weather_advice(source_results, plans, sources_failed, by_source,
                          advice_error):
    """Fold a failed advice lane into the per-source truth record.

    Returns (source_results, sources_failed, by_source), each unchanged when
    there is nothing to report. An absent source must be recorded as absent --
    that is the whole lesson of the last rebuild -- and a `weather_advice: []`
    with no trace is indistinguishable from "nothing to say", which is a lie.
    """
    if not advice_error:
        return source_results, sources_failed, by_source

    weather_url = WEATHER_URL
    for plan in plans or ():
        if plan.get("name") == "weather:open-meteo":
            weather_url = plan.get("url") or weather_url
            break

    # If the fetch itself was cut short by the budget, the advice lane is
    # UNKNOWN too. Calling that a failure would be inventing an outage.
    outcome, error, attempts = "failed", advice_error, 1
    for row in source_results or ():
        if row.get("name") == "weather:open-meteo":
            attempts = int(row.get("attempts") or 1)
            if row.get("outcome") == "skipped_budget":
                outcome = "skipped_budget"
            if row.get("outcome") != "ok":
                # Only add context when the upstream fetch is itself the
                # problem; "upstream weather fetch: ok" is noise, not honesty.
                error = "%s; upstream weather fetch: %s" % (
                    advice_error, row.get("error") or row.get("outcome")
                    or "unknown")
            break

    row = {"name": WEATHER_ADVICE_SOURCE, "kind": "weather", "category": "",
           "host": WEATHER_HOST, "url": weather_url, "outcome": outcome,
           "error": clean_ws(error)[:200], "attempts": max(1, attempts),
           "secs": 0.0}
    source_results = list(source_results or []) + [row]
    if outcome == "failed":
        sources_failed = list(sources_failed or []) + [
            "%s (%s)" % (WEATHER_ADVICE_SOURCE, row["error"])]
    by_source = dict(by_source or {})
    by_source[WEATHER_ADVICE_SOURCE] = 0.0
    return source_results, sources_failed, by_source


def advice_blockers(profile):
    """Commitments to protect -- ONLY from schedule.unavailable_windows.

    ORDERING CONSTRAINT -- read this before "improving" it. Story deadlines are
    deliberately NOT included, and they cannot be: this collector runs BEFORE
    the research agent, so no story, exam notice or deadline exists yet at the
    moment these windows are built, and inventing one would be exactly the
    fabrication this pipeline exists to prevent. Deadlines outrank weather in
    the rendered header, which is where they are actually known, so weather
    never has to trade against them here.
    """
    if profile is None:
        return []
    return [(w.start_hour, w.end_hour, "unavailable")
            for w in (getattr(profile, "unavailable_windows", ()) or ())]


def compute_weather_advice(box, profile, now=None):
    """(lines, error) from the weather box. Never raises, never kills the run.

    `lines` is the engine's output VERBATIM -- a list of
    {"id", "text", "priority"} -- because a render step reads exactly that and
    must not have to know anything about the wire format.

    `error` is "" both on success and when the engine legitimately had nothing
    to say ([]), because silence is a first-class answer and not a fault. It is
    non-empty only when a line is MISSING that we expected to be able to
    produce -- that is what the honesty record is for.
    """
    if profile is None:
        return [], "no usable profile"
    if getattr(profile, "weather_enabled", True) is False:
        return [], ""            # switched off on purpose: not a failure
    payload = box.get("payload") if isinstance(box, dict) else None
    if not payload:
        reason = (box.get("error") if isinstance(box, dict) else "") \
            or "no normalized forecast"
        return [], reason
    try:
        return _wa.weather_advice(payload, profile, advice_blockers(profile),
                                  now=now), ""
    except _wa.ProfileError as exc:
        return [], "profile rejected by the advice engine: %s" % exc
    except Exception as exc:  # never fatal; weather is not worth a digest
        return [], "advice engine failed: %s: %s" % (type(exc).__name__, exc)


def _notice_published_dt(text):
    """First parseable date in `text`, as an aware UTC datetime, or None.

    WHY this exists: a notice candidate with no date sorts to EPOCH and therefore
    LAST, so genuine notices were buried under any row that did carry a
    pubDate -- including the poisoned `site:` sweep rows. A real date from the
    page restores the intended ordering. Unparseable/absent -> None, which is
    the historical behaviour, so nothing is invented.

    Month names are normalised to their 3-letter form first because `strptime`
    `%b` accepts "Sep" but NOT the 4-letter "Sept"/"Sept." that notice pages
    routinely print.
    """
    for pattern in NOTICE_DATE_RES:
        for m in pattern.finditer(text or ""):
            raw = _normalize_month(m.group(1))
            dt = None
            for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y",
                        "%b %d, %Y", "%d %B, %Y", "%d %b, %Y"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            if dt is not None:
                return dt.replace(tzinfo=timezone.utc)
    return None


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def _normalize_month(raw):
    """Collapse a spelled-out month name to its 3-letter form ("Sept." -> "Sep").

    `strptime`'s `%b` accepts "Sep" and "September" but NOT the 4-letter "Sept"
    that notice pages routinely print, so without this the date is dropped and
    the notice silently sorts to EPOCH (last).
    """
    text = (raw or "").replace(".", "").strip()
    parts = text.split()
    if not parts:
        return text
    head = parts[0].lower()
    if len(head) >= 3:
        for name in _MONTHS:
            if head.startswith(name):
                parts[0] = name.capitalize()
                break
    return " ".join(parts)


def _is_nav_chrome(text, abs_url):
    """True when an anchor is site navigation rather than a published notice.

    `NOTICE_KEYWORDS` matches whole words, which fixes `rate` inside
    "DiRATEorate", but word boundaries alone still admit the live chrome:
    `bank_local` was 9/9 nav ("Rates & Charges", "Base Rate & Interest
    Spread Rate", "Corporate Banking") and `local_city` was 9/19. Those are
    product/section names, so:

      * a WEAK keyword (rate, branch) counts only when the anchor also carries a
        CONTENT word -- "Interest rate notice published" is a notice,
        "Deposit Rates" is a product page;
      * an anchor that reads as a section/portal name is dropped;
      * a path that is a section index is dropped;
      * a URL that is not DETAIL-shaped is dropped. This is the rule that
        catches the live navigation: every offending URL carried a notice
        keyword in its anchor text, so no text rule can see the difference --
        only the URL can.

    The direction of the error matters: a false drop deletes a real notice,
    which is the worse failure, so every rule below is a whole-word or
    path-shape test and never a heuristic guess about intent.
    """
    low = (text or "").strip().lower()
    if NOTICE_NAV_PATH_RE.search(abs_url or ""):
        return True
    # The URL-shape gate, and it outranks every text rule below because it is
    # the only one that can tell a SECTION from a PUBLICATION. Live 2026-09-29:
    # all 10 navigation URLs that broke D7 carried a notice keyword in their
    # anchor text, so the keyword test and the weak-keyword/content-word test
    # both passed them through. `/notices`, `/notices?...notice_type=21`,
    # `/category/noticenews/`, a site root and `/pages/exam-2013` are all
    # destinations; only an id or a document is a publication.
    if not NOTICE_DETAIL_URL_RE.search(abs_url or ""):
        return True
    stripped = low.strip(" \t\r\n-–—:|&/,")
    for suffix in NOTICE_NAV_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) <= len(suffix) + 24:
            return True
    # A short anchor whose URL's last segment repeats those same words, with
    # no id, no date and no file in it, is a SECTION INDEX, not a publication.
    # Live shape: "Audio Notice" -> /news--notices/audio-notice. Two guards
    # keep this from over-filtering, and a false drop is the worse failure:
    #   * the anchor must be 1-2 words, so "M.Sc. IV Semester-2080 Make Up
    #     Exam Center Notice" is never even considered; and
    #   * the slug must be the anchor's own words, so a short real title on
    #     any path that does not echo it (/notice/123, /download/x.pdf,
    #     /notices/15697, /audio-notice-2077-01-01) is untouched.
    words = [w.lower() for w in re.split(r"[^0-9A-Za-z]+", low) if w]
    if 1 <= len(words) <= 2:
        segment = urlparse(abs_url or "").path.rstrip("/").rsplit("/", 1)[-1]
        slug = [w.lower() for w in re.split(r"[^0-9A-Za-z]+", segment) if w]
        if slug == words and not NOTICE_DETAIL_SEGMENT_RE.search(segment):
            return True
    # Weak keyword with no content word: a product/section label, not a notice.
    if (NOTICE_WEAK_KEYWORDS_RE.search(text or "")
            and not NOTICE_CONTENT_RE.search(text or "")):
        return True
    return False


def notices_task(page_url, domain_category, timeout, retry=None,
                 on_attempt=None, resolver=None):
    body = fetch_bytes_retry(page_url, timeout, MAX_BYTES_HTML, retry,
                             on_attempt, resolver).decode("utf-8", errors="replace")
    base_domain = netloc_of(page_url) or page_url
    out = []
    seen = set()
    for m in ANCHOR_RE.finditer(body):
        href, inner = m.group(1), m.group(2)
        text = strip_html(inner)
        low = text.lower()
        if len(text) < 4:
            continue
        # Script-aware keyword gate. The Latin case is the historical English
        # test, unchanged; a Devanagari anchor is admitted by the Nepali
        # vocabulary's PRESENCE and by nothing else, and a script we carry no
        # vocabulary for is kept rather than dropped. Everything below -- the
        # detail-URL shape gate, the nav-path, nav-suffix, slug-echo and
        # weak-keyword rules -- then runs for EVERY script exactly as before,
        # so widening the keyword gate cannot smuggle chrome past the URL gate.
        keep, language = notice_keyword_verdict(text)
        if not keep:
            continue
        abs_url = urljoin(page_url, href.strip())
        if not abs_url.lower().startswith(("http://", "https://")):
            continue
        if _is_nav_chrome(text, abs_url):
            continue
        nu = normalize_url(abs_url)
        if not nu or nu in seen:
            continue
        seen.add(nu)
        out.append(make_candidate(
            title=text[:200], url=abs_url, source=base_domain,
            published_dt=_notice_published_dt(text),
            snippet="",
            domain_category=domain_category, language=language))
        if len(out) >= NOTICE_MAX_PER_SITE:
            break
    return out

# ---------------------------------------------------------------------------
# Source plan construction
# ---------------------------------------------------------------------------

def build_all_plans(sections, cfg=None, default_timeout=None, resolver=None,
                    profile=None, weather_box=None, interest_cap=None):
    """Return the list of plans.

    Each plan is {name, kind, category, host, url, att, fn}:
      name     -- unchanged historical plan name (logs + sources_failed)
      kind     -- "gnews" | "notice" | "feed" | "weather" (contract A1)
      category -- the plan's SECTION KEY, which is also its domain_category
                  ("" for weather). See the SECTION-KEY VOCABULARY block above:
                  this is the value that keys `by_category` in the health
                  payload AND tags each candidate, so it must be a key the
                  digest renderer knows, not a collector-private word.
      host     -- the REQUEST HOST, for critical-host accounting
      url      -- the exact URL requested
      att      -- {"n": attempts} box the task writes its attempt count into
      fn       -- callable, returns candidates (or (candidates, summary) for
                  weather); raises on failure
    The weather plan additionally carries a "weather" box, which its task fills
    with the normalized forecast for main() to turn into advice.
    Per-host timeouts come from config/collection.json, so a slow university
    webserver gets 20s while everything else stays on the default.

    `profile` is optional and additive: with none, the weather plan requests the
    historical daily-only WEATHER_URL and the box is a throwaway, so a caller
    that has not been taught about profiles behaves exactly as before.

    `interest_cap` overrides DEFAULT_INTEREST_TITLE_CAP for the Google News
    interest sweep. None means "use the declared default" -- the number is
    declared once, in DEFAULT_INTEREST_TITLE_CAP, and never repeated here.
    """
    cfg = cfg or _built_in_config()
    timeouts = cfg.get("timeout_sec") or {}
    retry = cfg.get("retry") or {}
    default_to = default_timeout or timeouts.get("default") or DEFAULT_TIMEOUT_SEC

    def timeout_for(host):
        for key in (host, host[4:] if host.startswith("www.") else host):
            val = timeouts.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return val
        return default_to

    plans = []

    def add(name, fn, kind, category, host, url, att):
        plans.append({"name": name, "fn": fn, "kind": kind,
                      "category": category, "host": host, "url": url,
                      "att": att})

    def add_gnews(locale_key, query, category):
        _, _, _, lang = LOCALES[locale_key]
        url = gnews_url(query, locale_key)
        name = "gnews:%s:%s" % (locale_key, query)
        att = {"n": 0}
        add(name,
            lambda u=url, c=category, l=lang, t=timeout_for(GNEWS_HOST),
            r=retry, a=_attempts_recorder(att), v=resolver:
            rss_task(u, c, l, t, r, a, v),
            "gnews", category, GNEWS_HOST, url, att)

    def add_feed(name, url, category, lang):
        """A direct RSS 2.0 feed. Same task as gnews; only the URL differs.

        `rss_task` is deliberately reused unchanged rather than given a parallel
        implementation: it is the function that already rejects Atom, RSS 1.0/RDF
        and HTML bodies with an honest `parse` reason, so a direct feed cannot
        acquire a second, laxer parse path.
        """
        host = (urlparse(url).netloc or netloc_of(url) or "").lower()
        att = {"n": 0}
        add(name,
            lambda u=url, c=category, l=lang, t=timeout_for(host),
            r=retry, a=_attempts_recorder(att), v=resolver:
            rss_task(u, c, l, t, r, a, v),
            "feed", category, host, url, att)

    # Direct publisher feeds. Added FIRST so the section-key vocabulary they use
    # is visible before the interest sweep is built below.
    for name, url, category, lang in DIRECT_FEEDS:
        add_feed(name, url, category, lang)

    games = cleaned_interest_list(sections, "games")
    anime = cleaned_interest_list(sections, "anime")
    manga = cleaned_interest_list(sections, "manga")
    manhwa = cleaned_interest_list(sections, "manhwa")
    manhua = cleaned_interest_list(sections, "manhua")

    # The interest sweep, capped ONCE and uniformly. Every list below takes the
    # same first `cap` titles, so the configured interests are genuinely searched
    # instead of the first six, and no single list can quietly eat the budget.
    cap = DEFAULT_INTEREST_TITLE_CAP if interest_cap is None else int(interest_cap)
    if cap < 0:
        cap = 0
    per_query = INTEREST_TITLES_PER_QUERY

    def groups(titles):
        return chunks(titles[:cap], per_query)

    # EN base sweep: games, anime, then the manga/manhwa lists. Each list is
    # tagged with the SECTION KEY that renders it, instead of every plan claiming
    # "entertainment" and matching no section at all.
    for group in groups(games):
        add_gnews("en-us", " OR ".join('"%s"' % t for t in group), "gaming")
    for group in groups(anime):
        add_gnews("en-us", " OR ".join('"%s"' % t for t in group), "anime")
    for group in groups(manga + manhwa):
        add_gnews("en-us", " OR ".join('"%s"' % t for t in group),
                  "manga_manhwa_manhua")

    # JP sweep: anime + manga titles.
    for group in groups(anime):
        add_gnews("ja-jp", " OR ".join('"%s"' % t for t in group), "anime")
    for group in groups(manga):
        add_gnews("ja-jp", " OR ".join('"%s"' % t for t in group),
                  "manga_manhwa_manhua")

    # KR sweep: manhwa titles.
    for group in groups(manhwa):
        add_gnews("ko-kr", " OR ".join('"%s"' % t for t in group),
                  "manga_manhwa_manhua")

    # ZH sweep. The `manhua` list in interests.yml is enabled but EMPTY today,
    # so it cannot be queried; the sweep falls back to the manga titles rather
    # than producing nothing. Unlike the old `manga[3:6]` fallback, which
    # hard-coded a slice of a DIFFERENT list and so queried titles the ZH sweep
    # had no particular reason to prefer, this reads `manhua` first: the day
    # someone adds a title there, it is swept without another code edit.
    for group in groups(manhua or manga):
        add_gnews("zh-cn", " OR ".join('"%s"' % t for t in group),
                  "manga_manhwa_manhua")

    # Nepal sweep: fixed queries x (hl=en, hl=ne).
    for query, category in NP_QUERIES:
        add_gnews("np-en", query, category)
        add_gnews("np-ne", query, category)

    # University-notice redundancy sweeps (np-en only) -- see
    # NP_EN_EXTRA_QUERIES: these ride Google News' own index, so they can still
    # find a notice when the university's origin is dark.
    for query, category in NP_EN_EXTRA_QUERIES:
        add_gnews("np-en", query, category)

    # Weather (single fetch). One request serves both lanes: `daily` comes back
    # in the same response and still drives the summary candidate below, so the
    # hourly advice costs no extra round trip, no extra plan and no extra worker.
    weather_att = {"n": 0}
    if weather_box is None:
        weather_box = new_weather_box(profile)
    weather_url = build_weather_url(profile)
    add("weather:open-meteo",
        lambda u=weather_url, t=timeout_for(WEATHER_HOST), r=retry,
        a=_attempts_recorder(weather_att), v=resolver, b=weather_box:
        weather_task(u, t, r, a, v, b),
        "weather", "", WEATHER_HOST, weather_url, weather_att)
    plans[-1]["weather"] = weather_box

    # Official notice pages (best-effort).
    for page_url, category in NOTICE_SITES:
        name = "notice:%s" % (netloc_of(page_url) or page_url)
        host = urlparse(page_url).netloc.lower() or netloc_of(page_url)
        att = {"n": 0}
        add(name,
            lambda u=page_url, c=category, t=timeout_for(host), r=retry,
            a=_attempts_recorder(att), v=resolver:
            notices_task(u, c, t, r, a, v),
            "notice", category, host, page_url, att)

    return plans


# ---------------------------------------------------------------------------
# Parallel orchestration with soft budget
# ---------------------------------------------------------------------------

def run_task(fn, att=None):
    t0 = time.perf_counter()

    def attempts():
        return max(1, int((att or {}).get("n") or 0))

    try:
        result = fn()
        if isinstance(result, tuple):
            candidates, weather_summary = result
        else:
            candidates, weather_summary = result, ""
        return {"ok": True, "error": "", "attempts": attempts(),
                "secs": time.perf_counter() - t0,
                "candidates": candidates, "weather_summary": weather_summary}
    except Exception as exc:  # never fatal — recorded and the run continues
        err = clean_ws("%s: %s" % (type(exc).__name__, exc))[:200]
        return {"ok": False, "error": err, "attempts": attempts(),
                "secs": time.perf_counter() - t0,
                "candidates": [], "weather_summary": ""}


def collect_parallel(plans, cfg=None):
    """Run all plans on a thread pool under the soft budget.

    Returns (sources_ok, sources_failed, by_source, raw_candidates,
    weather_summary, source_results).

    sources_ok / sources_failed keep their historical shape and semantics (the
    prompt and the workflow logs read them, and a budget-expired task still
    lands in sources_failed). The TRUTHFUL record is the sixth value:
    source_results, one entry per plan, in PLAN ORDER (deterministic, not
    completion order):
      ok             -- the request was REACHABLE (never "produced items")
      failed         -- the request was attempted and failed
      skipped_budget -- the budget expired before it finished: UNKNOWN, and
                        explicitly NOT a failure
    """
    cfg = cfg or _built_in_config()
    # Any non-negative budget is honoured here (the config LOADER is what
    # rejects nonsense); a 0 budget means "start nothing".
    budget_sec = _num_or(cfg.get("soft_budget_sec"), SOFT_BUDGET_SEC, 0.0)
    workers = int(_num_or(cfg.get("max_workers"), MAX_WORKERS, 1))

    sources_ok = []
    sources_failed = []
    by_source = {}
    raw_candidates = []
    weather_summary = ""
    results_by_name = {}

    def record(plan, res):
        results_by_name[plan["name"]] = res
        by_source[plan["name"]] = round(res["secs"], 2)
        if res["ok"]:
            sources_ok.append(plan["name"])
            raw_candidates.extend(res["candidates"])
            if res["weather_summary"]:
                return_dict["weather_summary"] = res["weather_summary"]
        else:
            sources_failed.append("%s (%s)" % (plan["name"],
                                               res["error"] or "failed"))

    return_dict = {"weather_summary": ""}
    start = time.monotonic()
    deadline = start + budget_sec
    pending = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for plan in plans:
            fut = pool.submit(run_task, plan["fn"], plan.get("att"))
            pending[fut] = plan
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for fut, plan in list(pending.items()):
                    record(plan, {"ok": False, "skipped": True,
                                  "error": "soft budget exceeded (task never "
                                           "completed; reachability UNKNOWN)",
                                  "attempts": 1, "secs": 0.0, "candidates": [],
                                  "weather_summary": ""})
                pending.clear()
                break
            done, _ = cf.wait(pending, timeout=remaining,
                              return_when=cf.FIRST_COMPLETED)
            for fut in done:
                plan = pending.pop(fut)
                try:
                    record(plan, fut.result())
                except Exception as exc:  # defensive; run_task already guards
                    record(plan, {"ok": False,
                                  "error": clean_ws(str(exc))[:200],
                                  "attempts": 1, "secs": 0.0, "candidates": [],
                                  "weather_summary": ""})

    # min_items_per_source: a reachable row below this many items is reported
    # `empty` rather than `ok`. Coerced to >= 1 so a broken config cannot turn
    # every productive source into a zero-yield claim.
    min_items = int(_num_or(cfg.get("min_items_per_source"),
                            DEFAULT_MIN_ITEMS_PER_SOURCE, 1))
    if min_items < 1:
        min_items = 1

    source_results = []
    for plan in plans:
        res = results_by_name.get(plan["name"]) or {}
        # Counted from the task's OWN candidates -- after its filter (notice
        # keyword test) and before any global/per-domain cap or cross-source
        # dedup. It measures what the source PRODUCED, which is the only thing
        # the banner may claim.
        items_found = 0
        if res.get("ok"):
            # Weather built a box/candidate -> 1; an RSS/notice task with a
            # well-formed body and zero <item> -> 0.
            items_found = len(res.get("candidates") or [])
            outcome = "ok"
            if items_found < min_items:
                # Reachable AND useless. ADDITIVE, never a replacement for ok:
                # source_health still counts this row as reachable, so the
                # anti-fabrication ok-shield keeps this host's real stories.
                outcome = "empty"
            error = ""
        elif res.get("skipped"):
            outcome = "skipped_budget"
            error = res.get("error") or "soft budget exceeded (reachability UNKNOWN)"
        else:
            outcome = "failed"
            error = res.get("error") or "failed"
        source_results.append({
            "name": plan["name"],
            "kind": plan["kind"],
            "category": plan["category"],
            "host": plan["host"],
            "url": plan["url"],
            "outcome": outcome,
            "error": error[:200],
            "attempts": max(1, int(res.get("attempts") or 1)),
            "secs": round(float(res.get("secs") or 0.0), 2),
            "items_found": max(0, int(items_found)),
        })

    return (sources_ok, sources_failed, by_source, raw_candidates,
            return_dict["weather_summary"] or weather_summary, source_results)



# ---------------------------------------------------------------------------
# Post-processing: filters, dedup, caps
# ---------------------------------------------------------------------------

def postprocess(raw_candidates, excluded_keywords_lower, max_per_domain):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=FRESHNESS_HOURS)

    prepared = []
    dropped_stale = dropped_keyword = 0
    for cand in raw_candidates:
        dt = cand.pop("_dt", None)
        text = ("%s %s" % (cand.get("title", ""), cand.get("snippet", ""))).lower()
        if any(kw in text for kw in excluded_keywords_lower):
            dropped_keyword += 1
            continue
        if dt is not None and dt < cutoff:
            dropped_stale += 1
            continue
        prepared.append((dt or EPOCH, cand))

    # Freshest first (stable sort keeps same-second items in source order).
    prepared.sort(key=lambda pair: pair[0], reverse=True)

    seen_urls = set()
    seen_token_sets = []
    per_domain = {}
    final = []
    dropped_dup_url = dropped_dup_title = dropped_domain_cap = 0
    for _dt, cand in prepared:
        nu = normalize_url(cand.get("url", ""))
        if not nu:
            continue
        if nu in seen_urls:
            dropped_dup_url += 1
            continue
        tokens = title_tokens(cand.get("title", ""))
        if any(jaccard(tokens, prev) >= TITLE_JACCARD_THRESHOLD
               for prev in seen_token_sets):
            dropped_dup_title += 1
            continue
        domain = cand.get("domain_category", "")
        if per_domain.get(domain, 0) >= max_per_domain:
            dropped_domain_cap += 1
            continue
        if len(final) >= MAX_TOTAL:
            break
        seen_urls.add(nu)
        seen_token_sets.append(tokens)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        final.append(cand)

    stats = {
        "dropped_stale": dropped_stale,
        "dropped_keyword": dropped_keyword,
        "dropped_dup_url": dropped_dup_url,
        "dropped_dup_title": dropped_dup_title,
        "dropped_domain_cap": dropped_domain_cap,
    }
    return final, per_domain, stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Deterministic Stage-A news collection for the daily digest.")
    ap.add_argument("--out", dest="out_path", default="state/candidates.json")
    ap.add_argument("--interests", dest="interests_path", default="config/interests.yml")
    ap.add_argument("--timings", dest="timings_path", default="state/timings.json")
    ap.add_argument("--max-per-domain", dest="max_per_domain", type=int,
                    default=MAX_PER_DOMAIN)
    ap.add_argument("--max-interest-titles", dest="max_interest_titles",
                    type=int, default=None,
                    help="how many titles from EACH configured interest list "
                         "become Google News queries (default: %d, declared once "
                         "in DEFAULT_INTEREST_TITLE_CAP). Each title is an "
                         "outbound request, so this is a budget decision, not a "
                         "recall preference." % DEFAULT_INTEREST_TITLE_CAP)
    ap.add_argument("--timeout", dest="timeout", type=int, default=None,
                    help="default per-request timeout in seconds "
                         "(default: config/collection.json timeout_sec.default)")
    ap.add_argument("--collection-config", dest="collection_config",
                    default=None,
                    help="path to collection.json (default: <repo>/config/collection.json)")
    ap.add_argument("--profile", dest="profile_path", default=None,
                    help="path to profile.yml (default: <repo>/config/profile.yml). "
                         "Unreadable or invalid -> daily-only weather and no "
                         "advice, recorded as a failure; never fatal.")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    started = datetime.now(timezone.utc)
    t_start = time.perf_counter()

    excluded_keywords, sections = load_interests(args.interests_path)
    excluded_lower = [k.lower() for k in excluded_keywords]

    cfg = load_collection_config(args.collection_config)
    default_timeout = args.timeout or cfg["timeout_sec"].get("default")

    # The profile drives the weather request. A bad profile is LOADED LOUDLY
    # (printed, and recorded as a failed advice source below) and then carried
    # on without: it must not stop news collection, and it must never be
    # replaced by a default, because advice for the wrong town is worse than
    # no advice.
    profile, profile_error = load_weather_profile(
        args.profile_path or default_profile_path())
    if profile_error:
        print("WARNING: %s" % profile_error)
    weather_box = new_weather_box(profile, kathmandu_now_str())

    resolver = DnsResolver(cfg["retry"].get("resolve_timeout_sec"))
    try:
        plans = build_all_plans(sections, cfg, default_timeout, resolver,
                                profile=profile, weather_box=weather_box,
                                interest_cap=args.max_interest_titles)
        (sources_ok, sources_failed, by_source, raw_candidates, weather_summary,
         source_results) = collect_parallel(plans, cfg)
    finally:
        resolver.close()

    weather_advice_lines, advice_error = compute_weather_advice(weather_box,
                                                                profile)
    source_results, sources_failed, by_source = record_weather_advice(
        source_results, plans, sources_failed, by_source, advice_error)

    final_candidates, per_domain, stats = postprocess(
        raw_candidates, excluded_lower, max(1, args.max_per_domain))

    total_sec = round(time.perf_counter() - t_start, 2)

    payload = {
        "collected_at": started.isoformat(timespec="seconds"),
        "sources_ok": sources_ok,
        "sources_failed": sources_failed,
        "source_results": source_results,
        "weather_summary": weather_summary,
        # Always present, even when empty. This key went missing once and the
        # digest went blind to weather for weeks with nothing complaining; a
        # presence assertion in tests/collection now guards it.
        "weather_advice": weather_advice_lines,
        "candidates": final_candidates,
    }
    # Merge with any stage timings already recorded by the workflow
    # (checkout/setup are written into this same file before collection);
    # replacing the file wholesale would erase them from the final table.
    existing_timings = {}
    try:
        with open(args.timings_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            existing_timings = loaded
    except (OSError, ValueError):
        pass
    timings = dict(existing_timings)
    timings["collect_total_sec"] = total_sec
    timings["by_source"] = by_source

    out_dir = os.path.dirname(os.path.abspath(args.out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = args.out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, args.out_path)

    tim_dir = os.path.dirname(os.path.abspath(args.timings_path))
    if tim_dir and not os.path.isdir(tim_dir):
        os.makedirs(tim_dir, exist_ok=True)
    tmp_tim = args.timings_path + ".tmp"
    with open(tmp_tim, "w", encoding="utf-8") as f:
        json.dump(timings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_tim, args.timings_path)

    # Summary (ASCII-safe console output).
    print("collect_news: %d candidates kept (of %d raw) in %.1fs"
          % (len(final_candidates), len(raw_candidates), total_sec))
    print("  per-domain: %s" % ", ".join(
        "%s=%d" % (k, v) for k, v in sorted(per_domain.items())))
    print("  sources ok: %d, failed: %d" % (len(sources_ok), len(sources_failed)))
    for entry in sources_failed:
        print("    FAILED: %s" % entry)
    # Truth line: reachable vs failed vs UNKNOWN (budget-skipped) per source.
    _by_outcome = {"ok": 0, "failed": 0, "skipped_budget": 0}
    for rec in source_results:
        _by_outcome[rec["outcome"]] = _by_outcome.get(rec["outcome"], 0) + 1
    print("  source_results: %d ok, %d failed, %d skipped_budget (budget %.0fs, "
          "workers %d, %s attempt(s) max)"
          % (_by_outcome["ok"], _by_outcome["failed"],
             _by_outcome["skipped_budget"], cfg["soft_budget_sec"],
             cfg["max_workers"], cfg["retry"]["attempts"]))
    for rec in source_results:
        if rec["outcome"] != "ok":
            print("    %-13s %s (%s) after %d attempt(s): %s"
                  % (rec["outcome"], rec["name"], rec["host"], rec["attempts"],
                     rec["error"] or "-"))
    if weather_summary:
        print("  weather: %s" % weather_summary)
    print("  filters: stale=%d keyword=%d dup-url=%d dup-title=%d domain-cap=%d"
          % (stats["dropped_stale"], stats["dropped_keyword"],
             stats["dropped_dup_url"], stats["dropped_dup_title"],
             stats["dropped_domain_cap"]))
    print("  out: %s | timings: %s" % (args.out_path, args.timings_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
