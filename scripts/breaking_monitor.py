#!/usr/bin/env python3
"""breaking_monitor.py — scheduled breaking-news watchdog (Stage-A' collect-small
→ deterministic pre-gate → AI classify → verify → spoiler veto → dedup → send).

Stdlib only. Woken by the Worker's detector when a candidate trips, plus a daily
Actions backstop (`breaking-monitor.yml`, `20 1 * * *`) in case it never wakes;
an untripped run appends ONE heartbeat row to state/breaking_log.json and
exits 0 silently.

Environment variables
---------------------
GMAIL_USER        Gmail address used for SMTP authentication (required).
GMAIL_APP_PASSWORD Gmail app-password for SMTP login (required).
GMAIL_RECEIVER    Destination email address for breaking alerts.  When set,
                  alerts are sent to this address instead of GMAIL_USER.
                  When GMAIL_RECEIVER is set the deterministic pre-gate
                  (EMERGENCY_RE) is bypassed so that ALL candidates are
                  forwarded to the AI classifier — useful for a dedicated
                  receiver that wants every detected item.

Pipeline
--------
1. COLLECT-SMALL : reduced source set (Google News RSS gl=NP en+ne for the
   fixed Nepal queries, one en-US world sweep, open-meteo weather).
   Every fetch goes through an injectable ``fetch_fn(url) -> (status, text)``
   so tests run fully offline. Items older than --window-min (default 90) are
   DROPPED here, and unparseable pubDates are dropped too (stricter than the
   main collector) — the weather forecast is the only dateless item
   exempt (it is a live forecast, not an article). Freshest-first, capped at
   --max-candidates (default 40).
2. PRE-GATE      : ONE deterministic gate (importance_gate) evaluated before
   any discard: the sport/gossip veto first (>=2 hits, unchanged), then the
   union of EMERGENCY_RE (high precision) and BREAKING_IMPACT_RE (coverage),
   then the universal-importance tier (_universal_hits) when both are silent.
   Zero trips -> heartbeat log row, exit 0, NOTHING printed. The blob is
   title+snippet, byte-identical to the Cloudflare Worker's (contract 1f).
3. AI CLASSIFY   : gate tripped -> candidates written to
   state/breaking_pending.json, then prompts/breaking-classify.md is piped to
    ``opencode run --auto --model <m>`` exactly like the daily-news.yml
    agent step (ladder read from config/opencode.json, whose IDs are NOT
    repeated here because they rotate — 1 attempt each, 60s timeout per
    attempt) with CANDIDATES_PATH in the env.
    NOTE: --standalone is v2/opencode2-only; v1 run is already in-process, so it must NOT be passed here. --auto keeps the run non-interactive.
   The agent's ENTIRE stdout must be one strict JSON verdict; parse failure /
   AI failure => fail-safe LEVEL 1 (silence) + log row.
4. VERIFY        : level 3 only. Distinct domains among verdict.sources U
   pre-gate-matched candidates must be >=2 OR a tier1 domain present.
   Confidence recomputed deterministically: HIGH = tier1 present or >=2
   independent established-news domains; MEDIUM = >=2 other distinct domains;
   LOW otherwise. LOW blocks sending (downgrade to level 2) UNLESS
   escalated_from_previous is true, in which case the alert renders
   "Developing — not yet independently confirmed.". Rumor markers
   (rumor|unconfirmed|allegedly|unverified) cap the level at 2.
5. SPOILER VETO  : entertainment categories (gaming|anime|manga_manhwa_manhua)
   go through validate_stories.spoiler_violation on title+summary; vetoed =>
   level 1, reason recorded, never sent. Additionally, before ANY level-3
   alert is sent, a category-agnostic high-sensitivity spoiler-word subset
   (ALERT_SPOILER_RE) runs on verdict title+summary; a hit downgrades to
   level 1 (reason spoiler_veto_alert_path).
6. DEDUP         : story_state.match_event against active_stories; existing
   AND already alerted AND NOT (confidence rank increased OR distinct source
   count now > stored source_count OR stored status was RESOLVED and now
   re-tripped) => NO send, last_seen bumped only. escalated_from_previous
   alone never re-alerts.
7. SEND          : ntfy FIRST (NTFY_SERVER root POST, topic in the body,
    priority 5 for CRITICAL / 4 otherwise, 4096-byte budget owned by
    payload_io). It is the primary channel and the one the owner reads, so
    email and Telegram are FALLBACKS: they run only when ntfy did not
    deliver, and are skipped with a recorded reason when it did. "ntfy
    unavailable" means a VERIFIED failure — a non-2xx status, a timeout, a
    connection error, or a transport that returned something unverifiable —
    never the absence of an exception, and never the AI verdict saying so.
    EMAIL (fallback): templates/alert.html tokens rendered (inline fallback
    template with the same tokens when the file does not exist yet) + plain
    text; SMTP identical to the daily-news.yml send step
    (smtp.gmail.com:587 STARTTLS, GMAIL_USER/GMAIL_APP_PASSWORD, subject
    "🚨 Breaking: {title} [{confidence}]"). --dry-run skips SMTP and writes
    state/email-preview.html instead. An SMTP exception persists
    pending_alert:{alert_id: date} in state/breaking_state.json (atomic) so a
    later successful retry clears it instead of double-sending.
    TELEGRAM (fallback to the fallback): when env TELEGRAM_BOT_TOKEN is
    non-empty, the same verified level-3 alert is ALSO pushed to Telegram
    AFTER the successful SMTP send/dry-run (sendMessage, parse_mode=HTML,
    best-effort).
    Destination chat: env TELEGRAM_CHAT_ID when set, otherwise auto-discovered
    as the LAST result[].message.chat.id from getUpdates (cached per-run;
    send the bot a /start message once so its update queue is non-empty).
    Any Telegram failure appends "telegram_failed:<err>" to the log row's
    reason and NEVER raises or alters email/state semantics. Token absent =>
    Telegram is skipped silently. --dry-run writes the exact message body to
    state/telegram-preview.txt instead of touching the network.
    HONESTY: the log row's reason always carries the ntfy outcome
    (ntfy:delivered / ntfy_failed:<err> / ntfy_not_configured:<why>), so a
    fallback's success can never erase the primary failure. When the primary
    AND both fallbacks fail, `sent` is False — never sent:true on nothing.
8. STATE UPDATES : only AFTER a successful send/dry-run — story_state upsert
   (alerted=true, alert_count++, last_alerted, latest_development,
   detection_source from PAYLOAD_SOURCE env, default google-news) +
   breaking_log row {run_at, checked, tripped, level, confidence, event_id,
   cost_seconds} (cap 500 rows, atomic writes throughout). The rendered
   alert carries a "Detected via: {detection_source}" line.

Exit codes: 0 normal (including the silent heartbeat and fail-safe silence)
| 3 corrupt active-story state | 1 config fatal (bad --sources-json).

CLI:
  python scripts/breaking_monitor.py --state-dir state --config-dir config \
      [--dry-run] [--window-min 90] [--max-candidates 40] [--sources-json F]

--sources-json overrides the built-in source list. Format: JSON array of
  [{"name": "...", "url": "...", "kind": "rss"|"weather"}, ...]
  ("kind" optional, default "rss").

Works on Ubuntu python3 and Windows py 3.13. No datetime.utcnow().
"""

import argparse
import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import collect_news
    import model_selector
    import payload_io
    import story_state
    import validate_stories as vs
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import collect_news
    import model_selector
    import payload_io
    import story_state
    import validate_stories as vs

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import ntfy_classes  # noqa: E402  (deliberate: after the sys.path insert)

PROMPT_PATH = os.path.join(REPO_ROOT, "prompts", "breaking-classify.md")
ALERT_TEMPLATE_PATH = os.path.join(REPO_ROOT, "templates", "alert.html")

FETCH_TIMEOUT_SEC = 10
MAX_WORKERS = 8
LOG_CAP = 500

# AI stage: exactly like daily-news.yml's agent step, compressed to 1 attempt
# per model and a short overall budget. Ladder is read from
# config/opencode.json (single source of truth).
#
# DYNAMIC SELECTION (2026-10-13). The ladder used to be a hardcoded ordered
# list with an identical embedded fallback tuple in every consumer, and it
# cost a mass-casualty alert: `opencode/big-pickle` timed out at 60s and
# `opencode/mimo-v2.5-free` returned an unparseable verdict, so nothing was
# decided and nothing was sent. Hardcoding rotted for real -- by 2026-10-18
# that `mimo-v2.5-free` was absent from the live catalogue (measured with
# `opencode models opencode`), so rung 2 was a model that burns attempts
# proving it is gone. Which absence is temporary is the provider's business,
# which is why nothing here claims a live model exists or that one is dead.
#
# config/opencode.json's `ladder` is now the owner's SEED -- his stated
# preference, still the single source of truth, and still the first thing
# tried -- and the rest of the attempt order is discovered live from the
# provider and filtered by what that listing actually reports. See
# scripts/model_selector.py for the discovery, ranking and health-memory
# contracts, and for why there is no embedded fallback model list any more.
OPENCODE_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "opencode.json")

#: Filenames are fixed; their contents are owned by model_selector.
MODEL_HEALTH_FILENAME = "model_health.json"
MODEL_CACHE_FILENAME = "model_cache.json"


def _opencode_config():
    """config/opencode.json, or {} — the caller degrades, never crashes."""
    return model_selector.load_config(OPENCODE_CONFIG_PATH)


def _load_ladder():
    """The owner's seed/preference ladder, from config/opencode.json ONLY.

    No embedded fallback tuple any more. That tuple WAS the bug: it was a
    second copy of the model list, in code, which the 2026-10-13 incident
    proved can be silently stale (it named a model Zen had discontinued). A
    total config failure now yields an empty seed, which discovery and the
    existing fail-safe already handle — loudly.
    """
    ladder = _opencode_config().get("ladder")
    if isinstance(ladder, list):
        return tuple(m for m in ladder if isinstance(m, str) and m)
    return ()


AI_MODELS = _load_ladder()

#: Unchanged at 60s: a working model's allowance is never shortened to make
#: a bad day look better.
AI_PER_ATTEMPT_TIMEOUT_SEC = 60

#: THE WHOLE AI STAGE IS BOUNDED, discovery included. Before this, the worst
#: case was models x attempts x timeout = 3 x 2 x 60 = 360s of wall clock
#: before a verdict was (or was not) reached. Now it cannot exceed this,
#: because every attempt's timeout is min(60, remaining budget).
AI_TOTAL_BUDGET_SEC = 150

#: Depth cap. A dead ladder costs its share of the budget, not a fixed 60s
#: each, so depth is bounded as well as time.
AI_MAX_MODELS = 4

# Deterministic pre-gate (contract regex — do not reword casually;
# groups are non-capturing so findall() returns whole-match strings).
EMERGENCY_RE = re.compile(
    r"\b(?:war|invasion|nuclear (?:test|accident|plant)|earthquake"
    r"|magnitude 6\.[5-9]|magnitude [7-9]|flood warning|landslide warning"
    r"|national emergency|Example Highway (?:closed|blocked)"
    r"|Example Bank (?:fraud|security|cyber)|bank of nepal.*cyber"
    r"|major outage)\b",
    re.IGNORECASE,
)
# Pure-sport/gossip suppression: >=2 vocabulary hits in title+snippet means the
# emergency-looking word is incidental (e.g. "war drama box office score").
SPORT_GOSSIP_RE = re.compile(r"\b(match|score|goal|celebrity|box office)\b",
                             re.IGNORECASE)
SPORT_GOSSIP_DOMINATING_HITS = 2

# Impact vocabulary — the SECOND tier of the single gate (contract 1e).
# EMERGENCY_RE above is the high-precision tier and is kept VERBATIM (frozen
# contract string); it is now a SUBSET of this union rather than the whole
# gate. It existed alone as pre_gate_check's only source while
# UNIVERSAL_IMPORTANCE_RE sat ~470 lines later, applied AFTER the candidate
# had already been discarded — two vocabularies for one decision, never
# reconciled. Measured against real collector output, 16 of 20 realistic urgent
# Nepal headlines passed straight through the old gate, including a real
# headline from this repository's own collector output:
#   "Mountainside vanishes in seconds in Nepal landslide as heavy rain kills 24"
# The union below is what those 20 need.
#
# A wider gate means more false positives. That is the intended trade: an
# urgent alert that is missed costs a real result, an extra candidate costs one
# AI classification. Both halves are measured against the frozen corpus in
# tests/breaking/fixtures/real_headlines.json.
BREAKING_IMPACT_RE = re.compile(
    r"\b(?:landslide|avalanche|subsidence|erosion"
    r"|killed|kills|died|dead|death|injured|missing|rescued|survivors|trapped|buried"
    r"|outage|shutdown|blackout|cut\s*off|blocked|disrupt(?:ed|ion)"
    r"|cyber\s*attack|hack(?:ed|ing)?|breach|ransomware|phishing|leak(?:ed|s)?"
    r"|crash(?:ed)?|collision|derail(?:ed)?|sank|sunk|burst|collapse[ds]?|explosion|blast"
    r"|fire|wildfire|blaze|arson"
    r"|strike|bandh|protest|curfew"
    r"|emergency|evacuat(?:e|ed|ion)|rescue|police\s+(?:killed|arrest)"
    r"|epidemic|outbreak|pandemic|cholera|measles|magnitude\s+\d)\b", re.IGNORECASE)

# Measured WIDENING (pass 2). BREAKING_IMPACT_RE above tripped only 0.84 of
# the urgent half of the frozen corpus, and the misses were not marginal: they
# were plural forms ("landslides" fails a bare `landslide` with \b after it),
# the word "flood" itself, and road-closure phrasing. Those are the most Nepal-
# urgent words there are, so the base regex as contracted is a coverage bug, not
# a precision win. The base regex is kept VERBATIM (frozen contract string) and
# this supplement is UNIONED with it, never substituted for it.
BREAKING_IMPACT_WIDE_RE = re.compile(
    r"\b(?:"
    # plural / inflected forms the base list missed outright
    r"flood(?:s|ing|ed)?|landslides?|avalanches?|mudslides?|landslip"
    r"|explosions?|fires?|crashes?|collisions?|outages?|shutdowns?"
    r"|protests?|strikes?|evacuations?|emergencies|blockades?"
    r"|casualties|injuries|fatalities|death\s+toll|death\s+count"
    r"|bodies|casualt(?:y|ies)"
    # road + service closure, the everyday form of a breaking Nepal alert
    r"|road(?:s|way)?\s+(?:is\s+|are\s+)?(?:blocked|closed|washed|cut|submerged)"
    r"|highways?\s+(?:is\s+|are\s+)?(?:blocked|closed|cut)"
    r"|(?:closed|blocked|cut\s+off|washed\s+away|sealed)\s+(?:off|for|at|from|on)"
    r"|traffic\s+(?:halted|suspended|stopped|disrupted)"
    r"|power\s+(?:cut|outage|failure)|load\s+shedding"
    r"|(?:telecom|internet|network|pipeline|gas|water|electricity)\s+"
    r"(?:disrupt\w*|down|outage|cut)"
    # structural damage and mass movement
    r"|swept\s+away|washed\s+away|toppled|collapsed?|destro(?:y|ys|ied)"
    r"|land\s+slide|debris|landslides?\s+hit"
    # response / recovery as a live event
    r"|rescued?|rescuers?|search\s+(?:under\s+way|for|operation)|"
    r"land\s+search|relief\s+operation|evacuat\w*"
    r"|day\s+of\s+mourning|condolence"
    # crime / security with a live hook
    r"|explosive|arrest(?:ed|s)?|apprehend\w*"
    r")\b", re.IGNORECASE)

# NOT-A-LIVE-EVENT framing veto (measured, pass 3). With the widened
# vocabulary the urgent half passed but the non-urgent half tripped 0.36 of
# the time, and almost every one of those was the SAME failure: a feature,
# explainer, retrospective or photo gallery ABOUT the disaster rather than a
# report of one. Google News is full of them in the month after an event
# ("Weeks after Nepal flood, thousands still missing", "Inside the Nepal Tunnel
# Rescue Operation", "Dams or death traps?").
#
# This runs AFTER the sport/gossip veto and BEFORE hit counting, exactly like
# it, and it can only ever suppress. It is deliberately a small list of
# framing markers, not a topic list: it says "this is journalism about the
# event", never "this event is unimportant". Anything that reads as a live
# report keeps the whole union intact.
NOT_A_LIVE_EVENT_RE = re.compile(
    # (i) analysis / explainer / commentary framing
    r"\b(?:explainer|explained|commentary|editorial|op-?ed|analysis|"
    r"infographic|retrospective|anniversary|here'?s\s+what|"
    r"what\s+we\s+know|what\s+caused|what\s+triggered|the\s+odds\s+of|"
    r"study\s+finds|scientists?\s+say|experts?\s+(?:say|warn)|"
    r"why\s+(?:is|are|do|does|did|has|have)|how\s+(?:to|why|and)|"
    r"who\s+(?:is|are|was|were|really)|"
    r"first\s+study|breakdown|factbox|fact\s+box|"
    # (v) a climate / attribution frame: the event is the SUBJECT of a study,
    #     not the report of it
    r"climate\s+(?:justice|change|change'?s|change’s|crisis|connect\w*|link)|"
    r"glacial\s+retreat|hydropower\s+(?:risk|fragility)|wider\s+climate|"
    r"carbon\s+brief|greenhouse|loss-and-damage|emissions|scientists?\b|"
    r"glacier\s+break|"
    # (vi) an institution or aid agency talking about itself, or a diary piece
    r"cargo\s+drones|deliver\w*\s+(?:relief|aid|supplies)|turns\s+to\s+drones|"
    r"drone\b|vows?\s+to|ambivalence|hesitan\w+|how\s+two\s+men|"
    r"survived\s+nine\s+days|recalls\s+ordeal|heartbreak|come\s+to\s+terms|"
    r"struggles|harbinger|what'?s\s+to\s+come|routed|rerouted|"
    r"alternative\s+routes|air\s+travel|the\s+health\s+system|"
    r"mental\s+health|aiding|sympathy|condolence|study|"
    r"survey\s+finds|world\s+fears)\b"
    # (ii) a retrospective time marker: the report is ABOUT a past window,
    #      however alarming its numbers. This is the single largest source of
    #      false positives in a corpus captured a month after the event.
    r"|\b(?:one|a|1)\s+month\s+(?:since|after|later|on|of)|"
    r"weeks?\s+after|months?\s+after|days?\s+after\s+(?:the\s+)?"
    r"(?:flood|landslide|avalanche|disaster|quake|earthquake|storm|"
    r"tragedy|flash)|"
    r"\d+\s+(?:days?|weeks?|months?|years?)\s+(?:after|since|on)\b|"
    r"one\s+year\s+on|still\s+a\s+lockdown"
    # (iii) a packaged media item rather than a report
    r"|\b(?:video|photo\s*gallery|photos?|pics|week\s+in\s+pictures|"
    r"in\s+pics|watch|licensable\s+picture|news\s+wrap|"
    r"scenes\s+from|footage\s+shows?|watch:)\b"
    # (iv) an institution reporting on itself after the fact
    r"|\b(?:charity\s+appeal|donat(?:e|es|ed|ion)|relief\s+fund|"
    r"responds?\s+to|response\s+continues|humanitarian\s+response|"
    r"operational\s+(?:update|response)|aid\s+(?:update|mission)|"
    r"assessment\s+ordered|damage\s+assessment|"
    r"compensation|restores?\s+(?:power|electricity)|reopens?|"
    r"reopened|pension|celebrat\w*|offers?\s+(?:a|an|the)\s+"
    r"(?:lifeline|relief)|answer\w*\s+to|debate)\b",
    re.IGNORECASE)

# A bare section label with no event in it. Google News emits these as if they
# were headlines: "Nepal Landslide - Dayton Daily News". There is no verb, no
# location, no casualty, nothing to alert on, and every word in it is topic
# vocabulary. Rule: the title minus a trailing " - Publisher" suffix must carry
# at least MIN_INFO_WORDS content words, or the candidate is site chrome and is
# never alerted on. This is a structural test, not a vocabulary one, so it
# cannot be tuned to a particular event.
BARE_LABEL_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{2,40}$")
MIN_INFO_WORDS = 4

# HARD LIVE EVIDENCE. The framing veto above is deliberately broad, and
# broadness has a cost: measured against the frozen corpus it was suppressing
# real casualties, because "Video: Mountainside vanishes in seconds in Nepal
# landslide as heavy rain kills 24" is a packaged media item AND 24 deaths.
# Nobody should be told 24 people are not worth an alert because the outlet
# put a camera there first.
#
# So the framing veto is CONDITIONAL: it only applies when this narrow,
# casualty-specific vocabulary is absent. These are the words that mean a
# person is dead, missing, trapped or being carried out. Nothing else in the
# veto can outrank them.
HARD_LIVE_EVIDENCE_RE = re.compile(
    r"\b(?:kill(?:ed|s|ing)?|die[sd]?|dead|death\s+toll|deaths?\b|fatalit\w+|"
    r"casualt\w+|bod(?:y|ies)|injur(?:ed|ies)|missing|unaccountable|"
    r"uncontactable|trapped|stranded|buried|rescued|rescuers?|"
    r"survivors?|swept\s+away|washed\s+away|toppled)\b",
    re.IGNORECASE)

# HARD LIVE DISRUPTION — the SECOND override, for the class the casualty
# vocabulary cannot see. HARD_LIVE_EVIDENCE_RE above answers "is a person hurt
# or missing?". This one answers "is something cut off RIGHT NOW?", and it
# exists because of a measured, real miss rather than a hypothetical:
#
#   "Krishna Bhir landslide blocks Muglin-Kathmandu road, repairs may take a
#    month"  (urgent:true, real corpus)
#     title-only gate -> True  "unified:landslide"      <- correct, SHOULD alert
#     title+snippet   -> False "not_a_live_event"      <- a genuine alert lost
#
# The reason is a collision of vocabulary, not a mislabelled row: the snippet
# repeats the headline and ends "... a month OnlineKhabar English News", where
# "a month O..." lands on the retrospective marker (see the (ii) clause of
# NOT_A_LIVE_EVENT_RE). The veto then suppressed a road that is BLOCKED TODAY
# because the surrounding sentence happened to mention a duration.
#
# The rule being fixed, stated once: a duration is not a retraction. "Repairs
# may take a month" is a story about how long the block will last; the road is
# blocked for all of it. So HARD live evidence — in either class — now
# outranks the framing veto, and the veto is re-applied only when BOTH are
# silent. Genuine non-events ("how to apply", explainers, opinion, "what does
# X mean", retrospectives, packaged media) carry neither a casualty nor a
# present-tense disruption, so they are still suppressed exactly as before.
#
# Scope discipline: this regex is additive and consulted ONLY as an override
# inside the veto. It does not add a single hit to the gate vocabulary, so the
# urgent:true / urgent:false trade below is bounded exactly by the number of
# rows that were BOTH vetoed AND carry one of these words.
HARD_LIVE_DISRUPTION_RE = re.compile(
    r"\b(?:block(?:s|ed|ing|age)?|clos(?:ed|es|ing|ure)|"
    r"shut(?:\s*down)?|cut\s+off|severed|"
    r"evacuat\w+|impassable|washed\s+away|swept\s+away)\b",
    re.IGNORECASE)

# The single place the veto is applied, so the two override classes can never
# be applied inconsistently between the alert path and the tests.
def _framing_veto_applies(text):
    """True when NOT_A_LIVE_EVENT framing wins: nothing hard is in the blob."""
    if not NOT_A_LIVE_EVENT_RE.search(text):
        return False
    if HARD_LIVE_EVIDENCE_RE.search(text):
        return False   # casualty / fatality / missing / rescue outranks framing
    if HARD_LIVE_DISRUPTION_RE.search(text):
        return False   # blocked / closed / evacuated / cut off does too
    return True


def _is_bare_label(title):
    # SCRIPT-AWARE, AND IN LOCKSTEP WITH deploy/cloudflare-worker.js:isBareLabel.
    #
    # THE DEFECT (2026-10-13). The tokeniser was `[A-Za-z0-9']+`, ASCII-only. A
    # Devanagari headline therefore tokenised to ZERO words, scored 0 informative
    # words, failed MIN_INFO_WORDS, and was discarded as a bare section label
    # BEFORE the emergency vocabulary was ever consulted. Measured over the live
    # feeds at that instant: 6 of 6 in-window headlines suppressed, and 55 of 55
    # over 72h on onlinekhabar and 12 of 12 on nepalnews. Nepali-language
    # breaking news -- which is most of it -- could not reach the classifier on
    # either arm while the code read as though those feeds were being ingested.
    #
    # THE CLASS IS ASCII ALNUM + APOSTROPHE + U+0900-U+097F (Devanagari), spelled
    # identically on both sides. It is a literal class and not `\w` or `\p{L}`
    # on purpose: Python's `\w` is Unicode-aware and JavaScript's is ASCII-only,
    # and `\p{L}` needs the `u` flag and depends on the engine's Unicode tables.
    # Two engines with two Unicode property tables is a parity risk this gate
    # cannot carry -- tests/breaking/test_breaking_chain.py runs BOTH
    # implementations over the frozen 616-row corpus and fails on ANY
    # divergence, so the two classes must mean byte-for-byte the same thing.
    #
    # Devanagari punctuation (the danda, U+0964) sits inside this block but is one
    # code point long, and the informative count requires len > 1, so a danda is
    # a separator in practice rather than a word.
    stripped = BARE_LABEL_SUFFIX_RE.sub("", (title or "").strip())
    words = [w for w in re.findall(r"[A-Za-z0-9'\u0900-\u097F]+", stripped)
             if len(w) > 1]
    return len(words) < MIN_INFO_WORDS

RUMOR_RE = re.compile(r"\b(rumor|rumour|unconfirmed|allegedly|unverified)\b",
                      re.IGNORECASE)

# Alert-path spoiler veto: built-in candidates carry
# domain_category="breaking_feed" and verdict categories rarely name an
# entertainment category, so the category-scoped veto is inert on the breaking
# feed. Before ANY level-3 alert is sent, this high-sensitivity subset
# (alternation fragments reused from validate_stories' SPOILER_WORD_RE /
# HIGH_SENSITIVITY_RE) runs against verdict title+summary regardless of
# category; a hit downgrades the alert to level 1 (silent log row).
ALERT_SPOILER_RE = re.compile(
    r"\b(?:dies|died|death|killed|ending\s+explained|datamined?"
    r"|leaks?|leaked|raw\s+scan|scanlation)\b",
    re.IGNORECASE,
)

TIER1_DOMAINS = (
    "gov.np", "nea.org.np", "tu.edu.np", "iost.tu.edu.np", "college.example.edu",
    "bank.example.com", "moecdc.gov.np", "can.org.np",
)
ESTABLISHED_NEWS_DOMAINS = (
    "kathmandupost.com", "thehimalayantimes.com", "onlinekhabar.com",
    "setopati.com", "risingnepaldaily.com", "gorkhapatraonline.com",
    "nepalitimes.com", "bbc.com", "reuters.com", "apnews.com", "cnn.com",
    "aljazeera.com", "theguardian.com", "nytimes.com", "afp.com",
)
CONFIDENCE_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

DEVELOPING_NOTICE_TEXT = "Developing — not yet independently confirmed."

# Detection-source values accepted from the Cloudflare Worker's
# repository_dispatch payload (PAYLOAD_SOURCE env, set by
# .github/workflows/breaking-monitor.yml from client_payload). Anything
# missing/unknown resolves to DEFAULT_DETECTION_SOURCE so a malformed payload
# can never inject junk into active_stories state.
DETECTION_SOURCES = ("google-news", "rss")
DEFAULT_DETECTION_SOURCE = "google-news"

# ---------------------------------------------------------------------------
# Universal importance patterns — items matching ANY of these bypass the
# personal-relevance filter because everyone should know about them.
# ---------------------------------------------------------------------------
UNIVERSAL_IMPORTANCE_RE = re.compile(
    r"\b(?:"
    # Mass-casualty events — note: the death count threshold (N > 10) is
    # enforced separately in check_universal_importance(); this branch is
    # only reached when the dedicated death-count extraction matches.
    # We do NOT include "kills N" / "N dead" here to avoid bypassing
    # the threshold.
    # Major natural disasters — magnitude 7+ (the "major" threshold per USGS),
    # or disaster words paired with casualty/destruction context.
    r"magnitude\s+(?:[7-9]|[1-9]\d)\b"
    r"|(?:earthquake|tsunami|cyclone|hurricane|typhoon)"
    r"[\s,]+(?:kills?|dead|devastat\w*|destruct\w*|magnitude)"
    r"|volcan(?:ic|o)\s+(?:eruption|active|explode)"
    r"|flood\s+(?:kills?|dead|devastat\w*|warning\s+issued)"
    # Disease outbreaks / pandemics
    r"|(?:outbreak|pandemic|epidemic)\b"
    # Significant geopolitical events
    r"|(?:war|invasion|coup|peace\s+deal|ceasefire|sanctions?)\b"
    # Major infrastructure failures
    r"|(?:power\s+(?:grid|station)|internet\s+(?:outage|shutdown|offage)"
    r"|water\s+(?:crisis|shortage|contamination))"
    # Major policy changes affecting large populations
    r"|(?:new\s+law|ban\s+imposed|nationwide\s+ban|nationwide\s+restriction"
    r"|major\s+policy\s+change|new\s+sanctions?\b)"
    # Mass-casualty / terrorism (word-level triggers, no count required)
    r"|(?:mass\s+casualt\w*|mass\s+kill\w*|terrorist\s+attack"
    r"|bombing\s+(?:kills?|dead))"
    r")",
    re.IGNORECASE,
)

UNIVERSAL_MIN_DEATH_COUNT = 10


def _universal_hits(text):
    """(matched, why) for the universal-importance tier on one blob.

    The single home of the mass-casualty logic. It used to live inline in
    check_universal_importance() ~1000 lines AFTER pre_gate_check had already
    thrown the candidate away, which is the whole bug: a parser that never
    looks cannot disagree about a headline. The gate calls this FIRST, and
    check_universal_importance() still calls it, so UNIVERSAL_MIN_DEATH_COUNT
    is defined in exactly one place and the two consumers cannot drift.
    """
    death_match = re.search(
        r"(\d{1,4})\s+(?:dead|kills?|killed|deaths?|casualt(?:y|ies))\b"
        r"|(?:dead|kills?|killed|deaths?|casualt(?:y|ies))\s+(\d{1,4})\b",
        text, re.IGNORECASE)
    if death_match:
        raw = death_match.group(1) or death_match.group(2)
        try:
            count = int(raw)
        except (ValueError, TypeError):
            count = 0
        if count > UNIVERSAL_MIN_DEATH_COUNT:
            return True, "universal:deaths>%d(%d)" % (UNIVERSAL_MIN_DEATH_COUNT,
                                                      count)
    if UNIVERSAL_IMPORTANCE_RE.search(text):
        return True, "universal:pattern"
    return False, ""


def importance_gate(text, title=None):
    """THE breaking gate (contract 1e): one union, evaluated ONCE, before any
    discard. Returns (tripped, why, hits).

    Ordering is part of the contract:
      1. the sport/gossip veto runs FIRST, before any hit counting, so a wider
         vocabulary can never drown it out;
      2. the not-a-live-event framing veto and the bare-label test, both
         suppress-only — the framing veto is skipped whenever HARD live
         evidence of EITHER class is present (casualty, or blocked/closed/
         evacuated/cut-off), because a duration or a packaged-media framing is
         not a retraction of a road that is blocked now;
      3. EMERGENCY_RE (high precision) | BREAKING_IMPACT_RE (coverage)
         | BREAKING_IMPACT_WIDE_RE (the measured widening);
      4. only when all three are silent, the universal-importance tier.

    `title` is optional and used only by the bare-label test; it defaults to
    the whole blob so the gate is still correct when a caller has no separate
    title.
    """
    gossip_hits = len(SPORT_GOSSIP_RE.findall(text))
    if gossip_hits >= SPORT_GOSSIP_DOMINATING_HITS:
        return False, "sport_gossip_dominating", []
    if _framing_veto_applies(text):
        return False, "not_a_live_event", []
    if _is_bare_label(title if title is not None else text):
        return False, "bare_label", []
    hits = ({h.lower() for h in EMERGENCY_RE.findall(text)}
            | {h.lower() for h in BREAKING_IMPACT_RE.findall(text)}
            | {h.lower() for h in BREAKING_IMPACT_WIDE_RE.findall(text)})
    if not hits:
        univ, why = _universal_hits(text)
        if univ:
            hits.add(why)
    if not hits:
        return False, "", []
    return True, "unified:" + ",".join(sorted(hits))[:160], sorted(hits)


def payload_detection_source():
    """detection_source for this run: PAYLOAD_SOURCE env when it names a known
    source, DEFAULT_DETECTION_SOURCE otherwise."""
    val = (os.environ.get("PAYLOAD_SOURCE") or "").strip().lower()
    return val if val in DETECTION_SOURCES else DEFAULT_DETECTION_SOURCE

# Reduced source set (COLLECT-SMALL).
BREAKING_NP_QUERIES = [
    "Nepal", "local district", "Tribhuvan University", "bank notice",
    "Nepal electricity NEA",
]
WORLD_SWEEP_QUERY = "major world news"

# Severe-weather thresholds for the open-meteo weather candidate.
SEVERE_PRECIP_MM = 50.0
SEVERE_WEATHERCODE = 65

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

KATHMANDU_OFFSET = timedelta(hours=5, minutes=45)


# ---------------------------------------------------------------------------
# Inline fallback alert template (used only while templates/alert.html does
# not exist; carries exactly the contracted tokens).
# ---------------------------------------------------------------------------

FALLBACK_ALERT_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{{TITLE}}</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;">
  <h1 style="color:#b30000;">&#128680; Breaking &mdash; Level {{LEVEL}} [{{CONFIDENCE}}]</h1>
  <h2>{{TITLE}}</h2>
  <p>{{SUMMARY}}</p>
  <p><strong>Why urgent:</strong> {{WHY_URGENT}}</p>
  <h3>Sources</h3>
  <ul>{{SOURCES}}</ul>
  <p style="color:#111;font-weight:bold;">Alert fired: {{FIRED_NPT}}</p>
  <p style="color:#555;font-size:0.9em;">Data cutoff: {{CUTOFF_NPT}} (Asia/Kathmandu)</p>
  <p style="color:#b30000;font-weight:bold;">{{DEVELOPING_NOTICE}}</p>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Tiny atomic-JSON helpers (log/state files owned by this script)
# ---------------------------------------------------------------------------

def _unique_tmp(path):
    """Per-process/per-thread tmp name: same directory as the target (so
    os.replace stays atomic on the same volume) but no cross-thread
    collisions on Windows, where opening the same .tmp twice fails hard."""
    return "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())


def _replace_atomic(tmp_path, path):
    """os.replace with a short retry: on Windows, a destination momentarily
    locked by a concurrent writer/AV makes replace raise PermissionError.
    First attempt is immediate (the common case); the backoff only kicks in
    under genuine contention."""
    last_exc = None
    delay = 0.005
    for attempt in range(20):
        try:
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(delay)
            delay = min(delay * 1.5, 0.05)
    raise last_exc


def write_json_atomic(path, payload):
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = _unique_tmp(path)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _replace_atomic(tmp_path, path)


def write_text_atomic(path, text):
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = _unique_tmp(path)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    _replace_atomic(tmp_path, path)


def read_json_or(path, default):
    """Best-effort JSON read; missing/corrupt -> default (log/state files are
    auxiliary: availability beats exactly-once, unlike active_stories)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_breaking_log(log_path, row, cap=LOG_CAP):
    rows = read_json_or(log_path, [])
    if not isinstance(rows, list):
        rows = []
    rows.append(row)
    if len(rows) > cap:
        rows = rows[-cap:]
    write_json_atomic(log_path, rows)


def load_breaking_state(path):
    data = read_json_or(path, {})
    if not isinstance(data, dict):
        data = {}
    pending = data.get("pending_alert")
    if not isinstance(pending, dict):
        pending = {}
    return {"pending_alert": pending}


# ---------------------------------------------------------------------------
# Fetch layer (injectable): fetch_fn(url) -> (status:int, text:str)
# ---------------------------------------------------------------------------

def default_fetch_fn(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": collect_news.USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/html, "
                  "application/json, */*",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        return resp.status, resp.read(collect_news.MAX_BYTES_RSS).decode(
            "utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Source plan + collection
# ---------------------------------------------------------------------------

def builtin_sources():
    srcs = []
    for query in BREAKING_NP_QUERIES:
        for locale in ("np-en", "np-ne"):
            srcs.append({
                "name": "gnews:%s:%s" % (locale, query),
                "url": collect_news.gnews_url(query, locale),
                "kind": "rss",
            })
    srcs.append({
        "name": "gnews:en-us:world",
        "url": collect_news.gnews_url(WORLD_SWEEP_QUERY, "en-us"),
        "kind": "rss",
    })
    srcs.append({
        "name": "weather:open-meteo",
        "url": collect_news.WEATHER_URL,
        "kind": "weather",
    })
    return srcs


def load_sources(sources_json_path):
    """Built-in list, or the --sources-json override (config-fatal on junk)."""
    if not sources_json_path:
        return builtin_sources()
    try:
        with open(sources_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read --sources-json %s: %s"
                          % (sources_json_path, exc))
    if not isinstance(data, list) or not data:
        raise ConfigError("--sources-json must be a non-empty JSON array")
    out = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ConfigError("--sources-json[%d] needs name+url" % i)
        out.append({
            "name": str(entry.get("name") or ("source-%d" % i)),
            "url": str(entry["url"]),
            "kind": str(entry.get("kind") or "rss"),
            # optional domain_category override so synthetic feeds (tests)
            # can exercise the entertainment spoiler-veto path
            "category": str(entry.get("category") or ""),
        })
    return out


def _mk_cand(title, url, source, dt, snippet, category, language, kind):
    return {
        "title": (title or "")[:300],
        "url": url or "",
        "source": source or "unknown",
        "published": dt.isoformat() if dt else None,
        "snippet": (snippet or "")[:collect_news.SNIPPET_MAX_CHARS],
        "domain_category": category,
        "language": language,
        "_dt": dt,
        "_kind": kind,
    }


def parse_rss_text(text, source_name, category, language):
    """Mirror of collect_news.rss_task operating on fetched text."""
    root = ET.fromstring(text.encode("utf-8", errors="replace"))
    out = []
    for item in root.iter("item"):
        title = collect_news.clean_ws(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        dt = collect_news.parse_pubdate(
            (item.findtext("pubDate") or "").strip())
        snippet = collect_news.strip_html(item.findtext("description") or "")
        src_el = item.find("source")
        src_home = (src_el.get("url") or "").strip() if src_el is not None else ""
        domain = (collect_news.netloc_of(src_home)
                  or collect_news.netloc_of(link) or source_name)
        out.append(_mk_cand(title, link, domain, dt, snippet, category,
                            language, "rss"))
    return out


def parse_weather_text(text):
    """Location forecast -> one synthetic candidate + severe flag.

    Severe iff any day has precipitation_sum >= 50mm or weathercode >= 65;
    the severe variant's text deliberately contains 'flood warning' so the
    deterministic pre-gate can see it (numbers quoted from the API payload,
    nothing invented)."""
    payload = json.loads(text)
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    prcp = daily.get("precipitation_sum") or []
    wcode = daily.get("weathercode") or []
    if not times:
        raise ValueError("open-meteo returned no daily data")
    severe = False
    parts = []
    for i, day in enumerate(times[:3]):
        code = wcode[i] if i < len(wcode) else None
        mm = prcp[i] if i < len(prcp) else None
        code_txt = collect_news.WEATHERCODE_TEXT.get(code, "unknown")
        parts.append("%s: precip %s mm, %s"
                     % (day, "n/a" if mm is None else "%.1f" % mm, code_txt))
        if isinstance(mm, (int, float)) and mm >= SEVERE_PRECIP_MM:
            severe = True
        if isinstance(code, int) and code >= SEVERE_WEATHERCODE:
            severe = True
    base = "Weather forecast (next %d days, Asia/Kathmandu, open-meteo): %s" \
           % (min(3, len(times)), "; ".join(parts))
    if severe:
        title = "Severe weather warning: flood warning risk"
        snippet = "Flood warning threshold met — " + base
    else:
        title = "Weather forecast (%d-day forecast)" % min(3, len(times))
        snippet = base
    return [_mk_cand(title, collect_news.WEATHER_URL, "open-meteo.com", None,
                     snippet, "weather_forecast", "en", "weather")], severe


def _fetch_source(source, fetch_fn):
    """One source task -> candidates list; raises on any failure."""
    status, text = fetch_fn(source["url"])
    if status is not None and int(status) >= 400:
        raise RuntimeError("HTTP %s" % status)
    if source["kind"] == "weather":
        cands, _severe = parse_weather_text(text)
        return cands
    return parse_rss_text(text, source["name"],
                          source.get("category") or "breaking_feed", "en")


def apply_window_and_cap(candidates, now_utc, window_min, max_candidates):
    """Freshness window + dedup + freshest-first cap.

    Unparseable dates are DROPPED here (stricter than the main collector);
    the dateless weather forecast is the only exemption."""
    cutoff = now_utc - timedelta(minutes=max(1, window_min))
    kept, dropped_stale, dropped_nodate = [], 0, 0
    for cand in candidates:
        if cand.get("_kind") == "weather":
            kept.append(cand)          # live forecast: dateless by design
            continue
        dt = cand.get("_dt")
        if dt is None:
            dropped_nodate += 1
            continue
        if dt < cutoff:
            dropped_stale += 1
            continue
        kept.append(cand)

    kept.sort(key=lambda c: c.get("_dt") or _EPOCH, reverse=True)

    seen_urls, seen_tokens, final = set(), [], []
    dropped_dup = 0
    for cand in kept:
        nu = vs.normalize_url(cand.get("url", ""))
        if not nu or nu in seen_urls:
            dropped_dup += 1
            continue
        tokens = vs.title_tokens(cand.get("title", ""))
        if any(vs.jaccard(tokens, prev) >= 0.7 for prev in seen_tokens):
            dropped_dup += 1
            continue
        seen_urls.add(nu)
        seen_tokens.append(tokens)
        final.append(cand)
        if len(final) >= max(1, max_candidates):
            break

    stats = {"dropped_stale": dropped_stale,
             "dropped_nodate": dropped_nodate, "dropped_dup": dropped_dup}
    return final, stats


def collect_candidates(sources, fetch_fn, window_min, max_candidates,
                       now_utc=None):
    """Fetch all sources (thread pool), apply the freshness window, dedup,
    sort freshest-first and cap. Returns (candidates, sources_ok,
    sources_failed, stats). Unparseable dates are DROPPED here (the weather
    forecast is the only dateless exemption)."""
    now_utc = now_utc or datetime.now(timezone.utc)

    sources_ok, sources_failed, raw = [], [], []
    pool = cf.ThreadPoolExecutor(max_workers=min(MAX_WORKERS,
                                                 max(1, len(sources))))
    try:
        future_map = {pool.submit(_fetch_source, s, fetch_fn): s
                      for s in sources}
        for fut in cf.as_completed(future_map):
            src = future_map[fut]
            try:
                raw.extend(fut.result())
                sources_ok.append(src["name"])
            except Exception as exc:
                err = collect_news.clean_ws("%s: %s"
                                            % (type(exc).__name__, exc))[:200]
                sources_failed.append("%s (%s)" % (src["name"], err))
    finally:
        pool.shutdown(wait=False)

    final, stats = apply_window_and_cap(raw, now_utc, window_min,
                                        max_candidates)
    return final, sources_ok, sources_failed, stats


# ---------------------------------------------------------------------------
# Stage 2: deterministic pre-gate
# ---------------------------------------------------------------------------

def _gate_blob(title, snippet):
    """FROZEN gate blob (contract 1f). Identical literal on the Cloudflare
    Worker side — the Worker used to gate on title ONLY, so it was strictly
    tighter than Python and the loss was silent. tests/breaking/
    test_worker_gate_parity.py extracts the literal out of the .js and fails on
    drift."""
    return "%s %s" % (title, snippet)


def pre_gate_check(text):
    """(tripped, why) for one title+snippet blob. Thin wrapper kept for the
    existing call sites; the decision itself is importance_gate()."""
    tripped, why, _hits = importance_gate(text)
    return tripped, why


def pre_gate(candidates):
    """(tripped, matched) — signature unchanged. Also returns the reasons via
    the pre_gate_why side-channel the caller logs, so a widened gate can be
    audited after the fact instead of guessed at."""
    tripped, matched, whys = False, [], []
    for cand in candidates:
        blob = _gate_blob(cand.get("title", ""), cand.get("snippet", ""))
        hit, why, _hits = importance_gate(blob, cand.get("title", ""))
        if hit:
            tripped = True
            matched.append(cand)
            whys.append("%s :: %s" % (cand.get("title", ""), why))
    pre_gate.last_why = whys            # read by main() for the log row
    return tripped, matched


# ---------------------------------------------------------------------------
# Stage 3: AI classification (opencode, exactly like daily-news.yml)
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Hard-evidence FAST PATH -- what replaces the AI ladder on the one class of
# event the ladder must never be allowed to swallow.
# ---------------------------------------------------------------------------
#
# THE LOSS (2026-10-13 09:03Z, run 38373618708). The Worker tripwire dispatched
# "At least 139 migrants dead after boat capsized in Bay of Bengal". That
# headline TRIPPED the deterministic gate -- `unified:dead` -- and then
# opencode/big-pickle timed out and opencode/mimo-v2.5-free returned an
# unparseable verdict. ai_classify returned (None, err), main() logged
# `ai_fail_safe` and returned 0. Nothing was sent. A 09:06Z dispatch of "Army
# helicopter rescues three trapped by landslide in Gorkha" hit the same wall.
# The honesty gate DID fire, so the fault was loud -- and the owner still
# missed a mass-casualty story.
#
# WHY THE FAST PATH RUNS BEFORE THE LADDER, NOT AFTER IT. Two failure modes:
#   * fast path first, AI only if it does not fire: on a bad provider day a
#     139-death alert waits out the whole ladder (up to three 60s attempts
#     plus retries) before anyone hears of it. The wait is the harm.
#   * AI first, fast path on failure: strictly fewer alerts, but the mass-
#     casualty class -- the one class where minutes matter -- pays the full
#     ladder latency exactly when the ladder is sickest.
# The second failure mode is worse and bounded-and-visible beats quiet-and-
# quick, so the fast path is authoritative when it fires and the ladder is not
# consulted for that run. Skipping the ladder is recorded in the log row and
# printed in the step summary; nothing about it is hidden.
#
# WHY IT CANNOT FABRICATE. The verdict it builds asserts nothing the headline
# does not: the title is the headline VERBATIM, the summary is EMPTY (never
# invented, and deliberately empty because ALERT_SPOILER_RE matches "death"),
# and the only source is the candidate's own link. Every downstream safety
# stage still runs UNCHANGED -- spoiler veto, Stage 4 verification,
# universal-importance override, Stage 6 dedup, rumor cap -- so the fast path
# cannot bypass dedup, cannot bypass the rumor cap, and cannot assert a
# confidence the domain check would not have produced. It replaces THE
# CLASSIFIER and nothing else.
#
# WHAT QUALIFIES. ALL of:
#   1. the deterministic pre-gate already tripped (the caller only reaches
#      here on the tripped branch), and the sport/gossip veto does not dominate;
#   2. the title is not a bare section label;
#   3. no rumour marker anywhere in the gate blob;
#   4. HARD_LIVE_EVIDENCE_RE -- the repo's OWN shared hard-evidence class, the
#      same literal the Worker gate uses -- matches the blob;
#   5. a death/casualty count STRICTLY GREATER THAN UNIVERSAL_MIN_DEATH_COUNT
#      (10, the threshold this repo already committed to for "everyone should
#      know") is extractable FROM THE TITLE, inside one clause.
#
# The count must exceed the repo's existing threshold rather than introduce a
# second one, and it must come from the TITLE rather than the snippet so the
# whole decision is provable from the headline alone -- no cross-boundary
# arithmetic, and nothing that depends on how the snippet happens to be built.
#
# WHAT DOES NOT QUALIFY, explicitly:
#   * casualty vocabulary with NO count -- "Army helicopter rescues three
#     trapped by landslide in Gorkha", a Forbes "...after the rescue". Those
#     are three people, or a feature framing. They stay on the AI path.
#   * any count <= 10.
#   * a declared-emergency phrasing. Deliberately EXCLUDED: it matched 0 of
#     the 616 frozen corpus rows, so it would be an unmeasurable clause added
#     on a bad day, which is the opposite of the evidence this repo runs on.
#   * a count visible only in the snippet, never in the title.
#   * rumoured, bare-label and sport/gossip rows -- already suppressed upstream
#     by the gate, re-asserted here so the fast path cannot be widened by a
#     future gate change alone.
#
# MEASURED, not asserted: over the 616-row frozen corpus this fires on 32
# rows, and every one of the 32 is a genuine mass-casualty headline
# ("21 dead, 300+ landslides...", "death toll reaches 800, with more than 3,000
# missing", "kill at least 68"). Zero false positives. The 2026-10-13 headline
# "At least 139 migrants dead" fires at 139; the existing tight extractor reads
# NOTHING on it, because "139 migrants dead" puts a word between the count and
# the casualty noun -- which is precisely the shape the incident took, and why
# reusing the existing extractor verbatim would NOT have fixed the loss.
HARD_EVIDENCE_COUNT_RE = re.compile(
    r"(\d{1,4})\s+(?:[A-Za-z-]+\s+){0,3}?"
    r"(?:dead|deaths?|died|dies|killed|kills?|fatalit\w+|casualt\w+|bodies)"
    r"|(?:dead|deaths?|died|dies|killed|kills?|fatalit\w+|casualt\w+|bodies)"
    r"\s+(?:[A-Za-z-]+\s+){0,3}?(\d{1,4})\b",
    re.IGNORECASE)


def stated_casualty_count(title):
    """(count, span) for a casualty count stated in *title*, else (None, '').

    The intervening-word class is `[A-Za-z-]` only, so a number can never walk
    across a comma, colon or dash into a later clause. That is what keeps
    "12 hours ago, 3 dead" at 3 and out, and it is why the match is taken from
    the title rather than the gate blob: on the blob, "One Dead - Ratopati 55
    Injured..." matched the PUBLISHER SUFFIX's leading number and called one
    casualty 55. Measured, then fixed.
    """
    m = HARD_EVIDENCE_COUNT_RE.search(title or "")
    if not m:
        return None, ""
    raw = m.group(1) or m.group(2)
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return None, ""
    return count, m.group(0)


def hard_evidence_fast_path(candidates):
    """(verdict|None, why) -- a level-3 verdict built with NO classifier.

    See the block comment above for the qualification rule and the measurement.
    Returns (None, "") for anything that does not qualify, which is the common
    case and is never a failure: the AI ladder is then consulted exactly as
    before.
    """
    for cand in candidates or []:
        title = cand.get("title", "") or ""
        blob = _gate_blob(title, cand.get("snippet", "") or "")
        if len(SPORT_GOSSIP_RE.findall(blob)) >= SPORT_GOSSIP_DOMINATING_HITS:
            continue
        if _is_bare_label(title):
            continue
        if RUMOR_RE.search(blob):
            continue
        if not HARD_LIVE_EVIDENCE_RE.search(blob):
            continue
        count, span = stated_casualty_count(title)
        if count is None or count <= UNIVERSAL_MIN_DEATH_COUNT:
            continue

        link = cand.get("url", "") or ""
        source_name = cand.get("source", "") or ""
        return ({
            "level": 3,
            # LOW is the honest starting point: nothing has been verified. The
            # domain recomputation in Stage 4 and the universal-importance
            # override decide the final value, and they run on the real matched
            # candidates -- not on anything this function asserts.
            "confidence": "LOW",
            "event_key": "",
            # VERBATIM. Never rewritten, never summarised, never embellished.
            "title": title,
            # EMPTY ON PURPOSE. Nothing is invented, and an empty summary also
            # keeps the verdict clear of ALERT_SPOILER_RE, which matches
            # "death"/"killed"/"died" and would otherwise downgrade the alert
            # to a silent level-1 log row.
            "summary": "",
            "why_urgent": (
                "hard-evidence fast path: the headline states %d dead (%r), "
                "above the repo's %d mass-casualty threshold, and the AI "
                "ladder was not consulted for this run"
                % (count, span, UNIVERSAL_MIN_DEATH_COUNT)),
            # The candidate's OWN link. Not a second-hand citation, not a
            # search result: the one URL the pipeline already holds for this
            # exact headline.
            "sources": ([{"name": source_name, "url": link}] if link else []),
            "category_suggestion": "",
            "universal_importance": True,
            "hard_evidence_fast_path": True,
        }, "fast_path:%s(dead=%d)" % (span, count))
    return None, ""


def _strip_internals(cand):
    return {k: v for k, v in cand.items() if not k.startswith("_")}


def default_ai_runner(model, prompt_text, env, timeout_sec):
    """`cat prompt | opencode run --auto --model <model>` equivalent.
    NOTE: --standalone is v2/opencode2-only; v1 run is already in-process."""
    exe = shutil.which("opencode")
    if not exe:
        raise RuntimeError("opencode binary not found on PATH")
    proc = subprocess.run(
        [exe, "run", "--auto", "--model", model],
        input=prompt_text, env=env, capture_output=True,
        timeout=timeout_sec, text=True, encoding="utf-8",
        errors="replace")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def extract_verdict_json(stdout_text):
    """First balanced JSON object found anywhere in the agent's output."""
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        brace = stdout_text.find("{", pos)
        if brace < 0:
            return None
        try:
            obj, _end = decoder.raw_decode(stdout_text[brace:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        pos = brace + 1


def normalize_verdict(obj):
    """Strict-ish schema coercion; None when the verdict is unusable."""
    if not isinstance(obj, dict):
        return None
    level = obj.get("level")
    if isinstance(level, bool) or not isinstance(level, int) \
            or level not in (1, 2, 3):
        return None
    confidence = obj.get("confidence")
    if confidence not in CONFIDENCE_RANK:
        return None
    sources = []
    for s in obj.get("sources") or []:
        if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"]:
            sources.append({"name": str(s.get("name") or ""),
                            "url": s["url"].strip()})
    # escalated_from_previous arrives from a model, so it arrives stringly-
    # typed as often as not ("true", "True", " false "). `is True` read the
    # string as False AND stored that False, so the model's real answer was
    # gone before any consumer saw it: silent, no error, no log, and a
    # LOW-confidence alert lost its DEVELOPING_NOTICE_TEXT safety caveat
    # ("Developing - not yet independently confirmed.") and was downgraded
    # instead - presented as firm when the model had said it was escalating.
    # The argument is stronger here than for universal_importance, because
    # this field's consumer is a TRUTHINESS read
    # (`if verdict.get("escalated_from_previous"):`): a boundary that passed
    # the model's strings through would make "false" TRUTHY and hedge a
    # firmly confirmed alert. Coerce ONCE here, at the boundary, and the
    # field is a guaranteed bool everywhere downstream. Coercing again at
    # the consumer would be redundant and would mask a future regression at
    # this boundary.
    # Junk (None, absent, ints, containers) is False, never an exception: a
    # bad optional field must not void an otherwise usable verdict.
    esc_val = obj.get("escalated_from_previous")
    if isinstance(esc_val, bool):
        escalated = esc_val
    elif isinstance(esc_val, str):
        escalated = esc_val.strip().lower() == "true"
    else:
        escalated = False
    # universal_importance arrives from a model, so it arrives stringly-typed
    # as often as not ("true", "True", " false "). `is True` read the string
    # as False AND stored that False, so the model's real answer was gone
    # before any consumer saw it: silent, no error, no log, and a critical
    # story silently lost its level-3 bump, its HIGH confidence and its
    # survival through the personal-relevance gate. The regex arm
    # (check_universal_importance) reads only title+summary, so it cannot
    # cover this. Coerce ONCE here, at the boundary, and the field is a
    # guaranteed bool everywhere downstream - which is what makes the
    # consumer's `is True` correct. Coercing again at the consumer would be
    # redundant and would mask a future regression at this boundary.
    # Junk (None, absent, ints, containers) is False, never an exception:
    # a bad optional field must not void an otherwise usable verdict.
    ui_val = obj.get("universal_importance")
    if isinstance(ui_val, bool):
        universal_importance = ui_val
    elif isinstance(ui_val, str):
        universal_importance = ui_val.strip().lower() == "true"
    else:
        universal_importance = False
    return {
        "level": level,
        "confidence": confidence,
        "event_key": str(obj.get("event_key") or "").strip(),
        "title": str(obj.get("title") or "").strip(),
        "summary": str(obj.get("summary") or "").strip(),
        "why_urgent": str(obj.get("why_urgent") or "").strip(),
        "escalated_from_previous": escalated,
        "sources": sources,
        "category_suggestion": str(obj.get("category_suggestion") or "").strip(),
        "universal_importance": universal_importance,
    }


def ai_classify(pending_path, runner=None, models=None,
                prompt_path=PROMPT_PATH, attempts_per_model=2):
    """Returns (verdict|None, error). Any failure => (None, err) and the
    caller fails safe to level 1 (silence).

    `models=None` (the production default) means RESOLVE DYNAMICALLY: the
    config seed, extended by what the provider actually serves right now,
    reordered by measured health, capped to AI_MAX_MODELS and bounded by
    AI_TOTAL_BUDGET_SEC. An explicit `models=[...]` bypasses selection
    entirely and is what the tests drive.

    ONE extra attempt per model, and ONLY for a transport/timeout failure.
    The 2026-10-13 incident was one model timing out and one returning junk --
    provider flakiness, not a bad model, so a single immediate re-ask is the
    honest response. An UNPARSEABLE verdict is deliberately NOT retried: that
    is the model answering, and asking it the same question again is how a
    broken classifier burns the job's whole time budget. Both outcomes are
    recorded in health memory, so the NEXT run reorders around them.

    Health memory can only REORDER the attempt list, never remove a model
    from it — see model_selector.select. Total failure still returns
    (None, err), still decides nothing, and still raises the honesty gate.
    """
    started = time.monotonic()
    deadline = started + AI_TOTAL_BUDGET_SEC
    state_dir = os.path.dirname(os.path.abspath(pending_path))
    health_path = os.path.join(state_dir, MODEL_HEALTH_FILENAME)
    health = model_selector.load_health(health_path)

    if models is None:
        try:
            selected, info = model_selector.select(
                _opencode_config(), health,
                cache_path=os.path.join(state_dir, MODEL_CACHE_FILENAME),
                max_models=AI_MAX_MODELS, now=time.time())
        except Exception as exc:  # selection must never end the run
            # DEGRADED, NOT SILENT. Falling back to the static seed here is
            # exactly the state the 2026-10-13 incident happened in -- a fixed
            # list that can name a model the provider no longer serves -- so a
            # broken selector is named in the error string that reaches the log
            # row and the honesty gate, not swallowed.
            selected = list(AI_MODELS)
            info = {"source": "seed",
                    "select_error": "%s: %s"
                                    % (type(exc).__name__,
                                       collect_news.clean_ws(str(exc))[:120])}
            print("ai_classify: SELECTOR FAILED (%s) -- falling back to the "
                  "config seed %r" % (info["select_error"], selected))
    else:
        selected, info = list(models), {"source": "explicit"}
    # Read by tests and by the log row: which models this run ACTUALLY tried.
    ai_classify.last_models = list(selected)
    ai_classify.last_info = info

    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            prompt_text = f.read()
    except OSError as exc:
        return None, "prompt_unreadable: %s" % exc
    runner = runner or default_ai_runner
    env = dict(os.environ)
    env["CANDIDATES_PATH"] = os.path.abspath(pending_path)
    errors = ["models=%s source=%s%s"
          % (",".join(selected), info.get("source"),
             (" select_error=%s" % info["select_error"])
             if info.get("select_error") else "")]
    out_of_time = False
    for model in selected:
        for attempt in range(1, max(1, attempts_per_model) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                out_of_time = True
                break
            # max(1, min(60, remaining)): an attempt never overruns the stage
            # budget, and an attempt that still has budget keeps its full 60s.
            timeout = model_selector.attempt_timeout(
                deadline, time.monotonic(), AI_PER_ATTEMPT_TIMEOUT_SEC)
            try:
                rc, out, err = runner(model, prompt_text, env, timeout)
            except subprocess.TimeoutExpired:
                model_selector.record_failure(health, model, "timeout",
                                              at=time.time())
                errors.append("%s: timed out after %ds" % (model, timeout))
                if attempt < attempts_per_model:
                    print("ai_classify: %s attempt %d timed out, retrying"
                          % (model, attempt))
                    continue
                break
            except Exception as exc:
                model_selector.record_failure(health, model, "transport",
                                              at=time.time())
                errors.append("%s: %s" % (model,
                                          collect_news.clean_ws(str(exc))[:120]))
                if attempt < attempts_per_model:
                    print("ai_classify: %s attempt %d failed transport, retrying"
                          % (model, attempt))
                    continue
                break
            verdict = normalize_verdict(extract_verdict_json(out))
            if verdict is not None:
                model_selector.record_success(health, model, at=time.time())
                model_selector.save_health(health_path, health)
                return verdict, None
            tail = collect_news.clean_ws(out)[-160:]
            errors.append("%s: unparseable verdict (rc=%s) %r"
                          % (model, rc, tail))
            # The model answered; it answered unusably. Do not re-ask, but DO
            # remember it: this is the 2026-10-13 mimo failure, and the next
            # run must not pay for it again.
            model_selector.record_failure(health, model, "garbage",
                                          at=time.time())
            break
        if out_of_time:
            errors.append("ai_budget_exhausted after %ds"
                          % AI_TOTAL_BUDGET_SEC)
            break
    model_selector.save_health(health_path, health)
    return None, "; ".join(errors) or "no AI attempts made"


# ---------------------------------------------------------------------------
# Stage 4: verification (level 3 only)
# ---------------------------------------------------------------------------

def _domain_matches(domain, base):
    return domain == base or domain.endswith("." + base)


def is_tier1_domain(domain):
    return any(_domain_matches(domain, d) for d in TIER1_DOMAINS)


def is_established_domain(domain):
    return any(_domain_matches(domain, d) for d in ESTABLISHED_NEWS_DOMAINS)


def verify_level3(verdict, matched_candidates):
    """Recompute confidence deterministically; returns
    (domains:set, tier1_hit:bool, confidence:str)."""
    domains = set()
    for s in verdict.get("sources") or []:
        d = collect_news.netloc_of(s.get("url", ""))
        if d:
            domains.add(d)
    for cand in matched_candidates or []:
        d = collect_news.netloc_of(cand.get("url", ""))
        if d:
            domains.add(d)
    tier1_hit = any(is_tier1_domain(d) for d in domains)
    established = {d for d in domains if is_established_domain(d)}
    if tier1_hit or len(established) >= 2:
        confidence = "HIGH"
    elif len(domains) >= 2:
        confidence = "MEDIUM"       # >=2 distinct tier2/3 domains
    else:
        confidence = "LOW"
    return domains, tier1_hit, confidence


# ---------------------------------------------------------------------------
# Rendering + sending
# ---------------------------------------------------------------------------

def render_alert(template_html, ctx):
    html_out = template_html
    for key, val in ctx.items():
        if key.startswith("_") or not isinstance(val, str):
            continue  # internal helper fields are not template tokens
        html_out = html_out.replace("{{%s}}" % key, val)
    return html_out


def build_plain_text(ctx):
    lines = [
        "🚨 BREAKING ALERT — LEVEL %s [%s]" % (ctx["LEVEL"], ctx["CONFIDENCE"]),
        "",
        ctx["TITLE"],
        "",
        ctx["SUMMARY"],
        "",
        "Why urgent: %s" % ctx["WHY_URGENT"],
        "",
        "Sources:",
    ]
    lines.extend("- %s" % s for s in ctx["_SOURCE_LINES"])
    # Event-driven alert: the label is the REAL fire time, never a
    # morning/evening slot (those belong to the 2x/day digest only).
    lines.extend(["", "Alert fired: %s" % alert_fired_str(ctx["CUTOFF_NPT"])])
    lines.append("Data cutoff: %s (Asia/Kathmandu)" % ctx["CUTOFF_NPT"])
    if ctx.get("DETECTED_VIA"):
        lines.append(ctx["DETECTED_VIA"])
    if ctx["DEVELOPING_NOTICE"]:
        lines.append(ctx["DEVELOPING_NOTICE"])
    return "\n".join(lines)


def default_smtp_factory(host, port):
    return smtplib.SMTP(host, port)


def send_email(smtp_factory, subject, html_body, text_body):
    """Send an email via Gmail SMTP.

    Recipient is resolved from GMAIL_RECEIVER (dedicated receiver) with
    fallback to GMAIL_USER when the former is not set.
    """
    user = os.environ.get("GMAIL_USER", "")
    receiver = os.environ.get("GMAIL_RECEIVER") or user
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = receiver
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtp_factory("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def stable_alert_id(verdict):
    basis = "%s|%s" % ((verdict.get("event_key") or "").strip().lower(),
                       (verdict.get("title") or "").strip().lower())
    return "brk-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# THE DELIVERY RECEIPT — written by the SENDER, when it actually sends
# ---------------------------------------------------------------------------
#
# WHY THIS FILE EXISTS. Until 2026-10-13 the watchdog's breaking arm asked the
# Cloudflare Worker "have you ever dispatched anything?" via
# `breaking_dispatch_ever`, and escalated a `false` answer as
# "THE BREAKING CHAIN HAS NEVER RUN ... Every breaking alert is being missed
# silently." That was factually wrong: four level-3 alerts were delivered to
# ntfy on 2026-10-11/12, each logging `breaking ntfy delivered (HTTP 2xx)`. The
# field measured the Worker's TRIGGER leg while breaking-monitor.yml's own
# `schedule` cron (then `20 */6 * * *`, daily `20 1 * * *` since 2026-10-19) was
# the SENDER that actually worked. A trigger
# leg is not a delivery record, and the watchdog reported it as one, so it
# re-paged on every run indefinitely. A safety system that cries wolf is worse
# than no safety system.
#
# THE RULE THIS FILE ESTABLISHES: **delivery is recorded as a POSITIVE FACT by
# the code that performed the send, at the moment the send returned 2xx.** It is
# never inferred from an absence, because "prove something is broken from
# nothing is recorded" is precisely the defect being closed.
#
# THREE WRITES, AND ONLY THREE, so the file can never drift into a shape the
# watchdog cannot judge:
#   * `last_delivery` — written ONLY on a confirmed 2xx from ntfy. Monotonic in
#     the sense that matters: it is never written by a failed or skipped send, so
#     its presence is proof and its age is only ever a note (a quiet news week is
#     normal and must never page the owner).
#   * `last_attempt` — written ONLY when a send was ACTUALLY attempted, carrying
#     that attempt's ok flag. `not_configured` and `--dry-run` are NOT attempts
#     and are never written, because notify_ntfy's own contract already says so.
#     This is what keeps "the chain runs and fails" a loud fault instead of an
#     invisible one, WITHOUT inferring failure from silence.
#   * `history` — the last N attempts, newest first, for a human reading it.
#
# The file is committed by breaking-monitor.yml and read by the watchdog's
# breaking arm, which is the same repo-checked-out mechanism the digest arms
# already use for `state/last_digest_sent.json`.
BREAKING_DELIVERY_SCHEMA_VERSION = 1
BREAKING_DELIVERY_HISTORY_MAX = 10


def load_breaking_delivery(path, default=None):
    """Read the receipt. A missing or corrupt file is `default`, never an
    exception: this file is observability, and a broken observer must not be
    able to stop an alert from being sent."""
    if default is None:
        default = {"schema_version": BREAKING_DELIVERY_SCHEMA_VERSION,
                   "last_delivery": None, "last_attempt": None, "history": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    out = dict(default)
    out.update(data)
    out["schema_version"] = data.get("schema_version")
    if not isinstance(out.get("history"), list):
        out["history"] = []
    return out


def record_breaking_attempt(path, ok, note, title="", extra=None):
    """Record one ATTEMPTED send and, when it worked, the delivery it proves.

    Called from notify_ntfy and nowhere else, so there is exactly one place in
    this file where a delivery verdict becomes a durable fact. `ok` is the HTTP
    verdict and nothing else: no verdict field, no classifier output and no
    model decision can reach this function, so the honesty gate cannot talk the
    code into recording a send that did not happen or hiding one that did.
    """
    row = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": bool(ok),
        "channel": "ntfy",
        "note": str(note or "")[:200],
        "title": str(title or "")[:200],
    }
    if extra:
        row.update(extra)
    doc = load_breaking_delivery(path)
    doc["schema_version"] = BREAKING_DELIVERY_SCHEMA_VERSION
    doc["last_attempt"] = row
    # ONLY a confirmed 2xx writes last_delivery. This is the load-bearing line:
    # it is the whole difference between a positive delivery record and an
    # inference from an absence.
    if ok:
        doc["last_delivery"] = dict(row)
    hist = [row] + [h for h in (doc.get("history") or [])
                    if isinstance(h, dict)]
    doc["history"] = hist[:BREAKING_DELIVERY_HISTORY_MAX]
    try:
        write_json_atomic(path, doc)
    except OSError as exc:
        # Losing the receipt costs observability and nothing else. The alert is
        # already sent or already failed, and neither verdict may depend on
        # whether a bookkeeping write worked.
        print("::warning::breaking delivery receipt not written (%s)" % exc)
    return row


# ---------------------------------------------------------------------------
# ntfy — the PRIMARY breaking-alert channel (email/Telegram are FALLBACKS)
#
# The owner reads breaking news on ntfy, the same channel as the daily digest
# and every watchdog alert, so a breaking event that paged email+Telegram but
# NOT ntfy reached the wrong place first. Order is the contract: ntfy is
# attempted FIRST, and email/Telegram fire only when ntfy genuinely failed.
# All three every time was three notifications for one event.
#
# Secrets (GitHub Actions -> env in breaking-monitor.yml):
#   NTFY_TOPIC   topic name. A publish/subscribe capability, so it is a
#                SECRET (wrangler secret for the Worker, Actions secret here).
#                It is never printed, logged, committed or put on a command
#                line: it is referenced by NAME only, and it travels in the
#                POST body rather than the URL precisely so an exception
#                string can never quote it back into a log.
#   NTFY_SERVER  ntfy root address. A plain var in deploy/wrangler.toml,
#                because an address is not a credential.
# ---------------------------------------------------------------------------


def ntfy_configured():
    """(configured:bool, why:str) for the ntfy channel.

    NOT CONFIGURED is deliberately a state of its own, distinct from FAILED.
    An unset secret is not evidence that ntfy is down, and recording it as a
    delivery failure would cry wolf on every local and CI run. It is still
    written into the run's reason, because "the primary channel was never even
    attempted" is exactly the silence this chain has already been burned by.
    """
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    server = (os.environ.get("NTFY_SERVER") or "").strip()
    if not topic or not server:
        return False, "NTFY_TOPIC/NTFY_SERVER unset"
    if not server.startswith(("http://", "https://")):
        return False, "NTFY_SERVER is not an http(s) address"
    return True, ""


def ntfy_http(url, payload):
    """Default ntfy transport: POST-JSON to the NTFY_SERVER ROOT (NOT /topic),
    topic carried in the body — the shape daily-news.yml and the
    digest-watchdog empty-digest arm already use.

    Returns (status:int, body_text:str) and never raises for a non-2xx: that
    status IS the signal ntfy_send acts on. Injectable so tests run offline.
    """
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "Markdown": "yes"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            return resp.status, resp.read(collect_news.MAX_BYTES_RSS).decode(
                "utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def ntfy_send(title, message, priority, tags, click="", http_fn=None):
    """POST one ntfy message -> (ok:bool, err:str). Never raises.

    UNAVAILABILITY IS A VERIFIED SIGNAL, NOT THE ABSENCE OF AN EXCEPTION.
    This is the load-bearing part of ntfy-first: `ok` is True ONLY on a 2xx
    status that was actually read back. A non-2xx (surfaced as an int by
    ntfy_http), a timeout and a connection error are all failures the caller
    acts on, and a transport that returns something that is not an int status
    is treated as UNVERIFIED, not as success. There is deliberately no broad
    `except` anywhere on the success path that could turn "unknown" into
    "delivered" — the shape this repo has been burned by repeatedly.

    WHAT IS NOT VERIFIABLE HERE: that the owner's phone later rendered the
    push. A 2xx proves ntfy accepted and stored the message, nothing more.
    That limit is stated rather than claimed as end-to-end delivery.
    """
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    server = (os.environ.get("NTFY_SERVER") or "").strip()
    body = {"topic": topic, "title": title, "message": message,
            "priority": priority, "tags": list(tags), "click": click or ""}
    # payload_io owns the 4096-byte budget for every ntfy push in this repo;
    # this module does not keep a second copy of the number.
    body, _truncated = payload_io.fit_ntfy_body(body)
    http_fn = http_fn or ntfy_http
    try:
        status, _ = http_fn(server.rstrip("/") + "/", body)
    except Exception as exc:
        # Connection error or timeout: a real, verified non-delivery.
        return False, collect_news.clean_ws("%s: %s"
                                            % (type(exc).__name__, exc))[:160]
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False, "unverified: transport returned %r, not an int status" \
            % (status,)
    if 200 <= code < 300:
        return True, ""
    return False, "http_%d" % code


def ntfy_alert_title(verdict, label):
    """Short ntfy TITLE: what happened, then why the reader should care.

    A collapsed phone notification shows the title and nothing else, so the
    consequence has to live HERE — the same reason the watchdog titles lead
    with the outcome ("A TU result could go out with nobody watching") rather
    than with the component name and a count.
    """
    head = collect_news.clean_ws(verdict.get("title") or "")[:90]
    if label == "CRITICAL":
        return "🚨 CRITICAL: %s — check this now" % head
    return "🚨 Breaking: %s" % head


def _join_sentences(first, second):
    """Two clauses as two sentences: a terminator when the first lacks one, and
    the second clause capitalised. Only the ntfy orientation line uses this,
    where "...struck near the district. damage has been reported" would read as a
    typo on a lock screen."""
    a = collect_news.clean_ws(first)
    b = collect_news.clean_ws(second)
    if not b:
        return a
    if a and a[-1] not in ".!?":
        a += "."
    return ("%s %s" % (a, b[0].upper() + b[1:])).strip()


def ntfy_lead_critical(detailed):
    """CRITICAL orientation sentence: what happened, then the consequence."""
    what = collect_news.clean_ws(detailed.get("what") or "")
    impact = collect_news.clean_ws(detailed.get("impact") or "")
    if not what:
        what = collect_news.clean_ws(detailed.get("headline") or "")
    return _join_sentences(what, impact)


def ntfy_lead_standard(verdict):
    """Standard orientation sentence: the first sentence of the summary, then
    the judge's own "why it matters". `why_urgent` IS that field, so it is
    used verbatim rather than paraphrased into something weaker.
    """
    return _join_sentences(first_sentence(verdict.get("summary")),
                           verdict.get("why_urgent") or "")


def ntfy_alert_message(lead, facts, link, detection_source, cutoff_npt):
    """Assemble the ntfy MESSAGE body from parts the caller has decided.

    THE ORDER IS THE CONTRACT: `lead` orients (one complete sentence saying
    what happened and what it means), `facts` detail, and everything the
    reader does not act on — detection source, fire time, data cutoff — goes
    LAST. A truncated or collapsed notification must never be the thing that
    loses the reason the alert fired.
    """
    lines = [lead, ""]
    lines.extend(facts)
    if link:
        lines.append("🔗 Read more: %s" % link)
    lines.append("")
    lines.append("Detected via %s · fired %s · checked through %s"
                 % (detection_source, alert_fired_str(cutoff_npt), cutoff_npt))
    return "\n".join(lines)


def notify_ntfy(verdict, detailed, confidence, label, paths, args,
                source_url, detection_source, cutoff_npt, http_fn=None):
    """Attempt the PRIMARY channel. Returns (state:str, note:str).

    `state` is exactly one of:
      "delivered"       a 2xx that was actually read back.
      "failed"          a verified failure: non-2xx, timeout, connection error.
      "not_configured"  NTFY_TOPIC/NTFY_SERVER unset or unusable — never even
                        attempted, and NOT the same claim as "ntfy is down".
      "dry_run"         --dry-run: rendered to state/ntfy-preview.txt, no
                        network touched at all.

    `note` is appended to the run's reason, so a fallback's success can never
    erase the primary failure from the record.

    Bookkeeping here is DETERMINISTIC and derived only from the HTTP status.
    Nothing in this function reads `verdict` for a delivery decision: the
    breaking path involves an AI judge, and a model must not be able to talk
    the code into reporting a send that did not happen (or suppress one).
    """
    title = ntfy_alert_title(verdict, label)
    if label == "CRITICAL":
        lead = ntfy_lead_critical(detailed)
        facts = [
            "• Where: %s" % (detailed.get("where") or "N/A"),
            "• When: %s" % (detailed.get("when") or "N/A"),
            "• Status: %s" % (detailed.get("status") or "N/A"),
            "• Confidence: %s" % confidence,
        ]
    else:
        lead = ntfy_lead_standard(verdict)
        facts = [
            "• Category: %s"
            % (verdict.get("category_suggestion") or "local_city"),
            "• Confidence: %s" % confidence,
        ]
    message = ntfy_alert_message(lead, facts, source_url, detection_source,
                                 cutoff_npt)
    # CRITICAL is max priority: an event the owner may have to act on tonight.
    priority = 5 if label == "CRITICAL" else 4
    tags = (["rotating_light", "warning"] if label == "CRITICAL"
            else ["warning"])
    # `breaking` first, always. The breaking chain and the daily digest share a
    # priority and used to share every tag too, so on a collapsed notification
    # the only thing telling them apart was the title. The tag is a literal from
    # ntfy_classes and is never built from the headline, which is model-adjacent
    # text -- a breaking alert must not be able to relabel itself.
    tags = ntfy_classes.with_class_tags(ntfy_classes.BREAKING, tags)

    if args.dry_run:
        # --dry-run never touches the network: the exact message lands in
        # state/ntfy-preview.txt beside email-preview.html.
        write_text_atomic(paths["ntfy_preview"], "TITLE: %s\n\n%s\n"
                          % (title, message))
        return "dry_run", "ntfy:dry_run_preview"

    configured, why = ntfy_configured()
    if not configured:
        # NOT AN ATTEMPT. ntfy_send was never called, so no attempt is recorded
        # and last_delivery is untouched -- recording a failure here would be the
        # inference-from-an-absence this receipt was built to avoid.
        return "not_configured", "ntfy_not_configured:%s" % why

    ok, err = ntfy_send(title, message, priority, tags, click=source_url,
                        http_fn=http_fn)
    # THE DELIVERY RECEIPT, written here and only here: inside the branch that
    # just learned the HTTP answer, from the HTTP answer alone.
    receipt_path = paths.get("breaking_delivery")
    if receipt_path:
        record_breaking_attempt(
            receipt_path, ok,
            "ntfy:delivered" if ok else "ntfy_failed:%s" % err, title,
            extra={"label": label})
    if ok:
        print("breaking ntfy delivered (HTTP 2xx): %s" % title)
        return "delivered", "ntfy:delivered"
    print("::warning::breaking ntfy FAILED (%s): %s" % (err, title))
    return "failed", "ntfy_failed:%s" % err


# ---------------------------------------------------------------------------
# Telegram fan-out for Level-3 alerts (FALLBACK, runs only when ntfy did not
# deliver AND email did; best-effort)
#
# Secrets (GitHub Actions secrets -> env in breaking-monitor.yml):
#   TELEGRAM_BOT_TOKEN  bot token from @BotFather; absent/empty => Telegram is
#                       skipped silently and email-only behavior is unchanged.
#   TELEGRAM_CHAT_ID    optional explicit destination chat id. When unset the
#                       chat id is auto-discovered as the LAST
#                       result[].message.chat.id in the bot's getUpdates
#                       queue — i.e. the user must have sent /start to the bot
#                       once; the discovery result is cached per-run.
# ---------------------------------------------------------------------------

_TG_CHAT_ID_CACHE = {}      # token -> chat_id; reset at each main() run


def tg_http(url, payload=None):
    """Default Telegram Bot API transport: GET when payload is None,
    POST-JSON otherwise. Returns (status:int, body_text:str). Injectable so
    tests run fully offline."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        return resp.status, resp.read(collect_news.MAX_BYTES_RSS).decode(
            "utf-8", errors="replace")


def tg_get_chat_id(token, http_fn=None):
    """LAST result[].message.chat.id from getUpdates (10s timeout); None when
    the queue has no usable chat id or the call fails. Cached per-run."""
    if token in _TG_CHAT_ID_CACHE:
        return _TG_CHAT_ID_CACHE[token]
    http_fn = http_fn or tg_http
    try:
        _status, body = http_fn(
            "https://api.telegram.org/bot%s/getUpdates" % token, None)
        payload = json.loads(body)
    except Exception:
        return None
    last_id = None
    results = payload.get("result") if isinstance(payload, dict) else None
    for upd in results or []:
        if not isinstance(upd, dict):
            continue
        chat = (upd.get("message") or {}).get("chat") or {}
        if isinstance(chat, dict) and chat.get("id") is not None:
            last_id = chat["id"]
    if last_id is not None:
        _TG_CHAT_ID_CACHE[token] = last_id
    return last_id


def tg_send(token, chat_id, text, http_fn=None):
    """sendMessage (10s timeout) -> (ok:bool, err:str). Never raises."""
    http_fn = http_fn or tg_http
    try:
        status, body = http_fn(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": False})
    except Exception as exc:
        return False, collect_news.clean_ws("%s: %s"
                                            % (type(exc).__name__, exc))[:160]
    try:
        payload = json.loads(body)
    except ValueError:
        return False, "non-json response (HTTP %s)" % status
    if isinstance(payload, dict) and payload.get("ok") is True:
        return True, ""
    err = ""
    if isinstance(payload, dict):
        err = str(payload.get("description") or "")
    return False, (err or ("HTTP %s" % status))[:160]


def first_sentence(text):
    """First sentence of a summary (up to the first '.', '!' or '?' followed
    by whitespace/end); the whole text when no terminator exists."""
    t = (text or "").strip()
    m = re.match(r"^(.*?[.!?])(?:\s|$)", t, re.DOTALL)
    return (m.group(1) if m else t)[:300]


# ---------------------------------------------------------------------------
# Universal importance detection
# ---------------------------------------------------------------------------

def check_universal_importance(item):
    """Return True when *item* (verdict or candidate dict with title+summary)
    matches universal-importance criteria — events everyone should know about
    regardless of personal interests.  Checks title+summary for mass-casualty
    counts, major natural disasters, pandemics, geopolitical events,
    infrastructure failures, and major policy changes."""
    title = item.get("title", "")
    summary = item.get("summary", "")
    text = "%s %s" % (title, summary)
    # The mass-casualty threshold and the pattern list now live in
    # _universal_hits(), shared with the pre-gate (contract 1e). This function
    # is the post-classification consumer; it must not keep its own copy.
    matched, _why = _universal_hits(text)
    return matched


# ---------------------------------------------------------------------------
# Detailed alert generation for CRITICAL / universal-importance items
# ---------------------------------------------------------------------------

_LOCATION_RE = re.compile(
    r"(?:in|near|at|from)\s+"
    r"([A-Z][A-Za-z\u00C0-\u024F]+(?:[\s,]+[A-Z][A-Za-z\u00C0-\u024F]+)*)")
_TIME_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"
    r"|\b(today|yesterday|tonight|just\s+now|breaking|live)\b",
    re.IGNORECASE)
_IMPACT_RE = re.compile(
    r"(?:affect(?:ed|s)?|impact(?:ed|s)?|damage|homeless|displaced"
    r"|injured|evacuated|disrupt(?:ed|s)?)\s+[^.]{4,120}",
    re.IGNORECASE)


def _extract_location(title, summary):
    """Best-effort location extraction from title+summary."""
    text = "%s %s" % (title, summary)
    m = _LOCATION_RE.search(text)
    if m:
        return m.group(1).rstrip(" ,.")
    # Fall back: look for a proper-noun / district / province pattern
    fallback = re.search(
        r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\s+"
        r"(?:district|province|region|city|municipality)", text)
    if fallback:
        return fallback.group(0).strip()
    return ""


def _extract_time(title, summary):
    """Best-effort time/date extraction."""
    text = "%s %s" % (title, summary)
    m = _TIME_RE.search(text)
    if m:
        return m.group(1) or m.group(2) or ""
    return ""


def _extract_impact(title, summary):
    """Best-effort impact description extraction."""
    text = "%s %s" % (title, summary)
    m = _IMPACT_RE.search(text)
    if m:
        return m.group(0).strip()
    return ""


def _determine_status(title, summary):
    """Heuristic current-situation status."""
    text = ("%s %s" % (title, summary)).lower()
    if re.search(r"\b(?:resolved|ended|contained|recovered)\b", text):
        return "Resolved"
    if re.search(r"\b(?:ongoing|developing|active|continuing|still)\b", text):
        return "Ongoing"
    return "Developing"


def generate_detailed_alert(item):
    """Build a structured dict for the CRITICAL / universal-importance
    detailed bullet-point alert format.

    Returns dict with keys: headline, what, where, when, impact, status,
    source.
    """
    title = item.get("title", "")
    summary = item.get("summary", "")
    source_url = ""
    for s in item.get("sources") or []:
        if isinstance(s, dict) and s.get("url"):
            source_url = s["url"]
            break
    return {
        "headline": "🚨 CRITICAL ALERT: %s" % title,
        "what": first_sentence(summary),
        "where": _extract_location(title, summary),
        "when": _extract_time(title, summary),
        "impact": _extract_impact(title, summary),
        "status": _determine_status(title, summary),
        "source": source_url,
    }


def _render_critical_html(detailed, confidence, detection_source, cutoff_npt):
    """Render the CRITICAL bullet-point email body (HTML)."""
    esc = html.escape
    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
        '<title>%s</title></head>' % esc(detailed["headline"]),
        '<body style="font-family:Arial,Helvetica,sans-serif;'
        'max-width:640px;margin:0 auto;">',
        '<h1 style="color:#b30000;">%s</h1>' % esc(detailed["headline"]),
        '<p style="font-size:1.05em;">',
        '<ul style="list-style:none;padding-left:0;">',
        '<li><strong>What:</strong> %s</li>' % esc(detailed["what"]),
        '<li><strong>Where:</strong> %s</li>' % esc(detailed["where"] or "N/A"),
        '<li><strong>When:</strong> %s</li>' % esc(detailed["when"] or "N/A"),
        '<li><strong>Impact:</strong> %s</li>' % esc(detailed["impact"] or "Assessment ongoing"),
        '<li><strong>Status:</strong> %s</li>' % esc(detailed["status"]),
        '</ul></p>',
    ]
    if detailed["source"]:
        parts.append(
            '<p><a href="%s">🔗 Source: read more</a></p>' % esc(detailed["source"]))
    parts.append(
        '<p style="color:#555;font-size:0.9em;">'
        'Alert fired: %s</p>' % esc(alert_fired_str(cutoff_npt)))
    parts.append(
        '<p style="color:#555;font-size:0.9em;">'
        'Data cutoff: %s (Asia/Kathmandu)</p>' % esc(cutoff_npt))
    parts.append(
        '<p style="color:#555;font-size:0.9em;">'
        'Detected via: %s</p>' % esc(detection_source))
    parts.append('</body></html>')
    return "\n".join(parts)


def _render_critical_plain(detailed, confidence, detection_source, cutoff_npt):
    """Render the CRITICAL bullet-point plain-text email body."""
    lines = [
        "🚨 CRITICAL ALERT: %s" % detailed["headline"].replace("🚨 CRITICAL ALERT: ", ""),
        "",
        "• What: %s" % (detailed["what"] or "N/A"),
        "• Where: %s" % (detailed["where"] or "N/A"),
        "• When: %s" % (detailed["when"] or "N/A"),
        "• Impact: %s" % (detailed["impact"] or "Assessment ongoing"),
        "• Status: %s" % detailed["status"],
    ]
    if detailed["source"]:
        lines.append("• 🔗 Source: %s" % detailed["source"])
    lines.extend([
        "",
        "Alert fired: %s" % alert_fired_str(cutoff_npt),
        "Data cutoff: %s (Asia/Kathmandu)" % cutoff_npt,
        "Detected via: %s" % detection_source,
    ])
    return "\n".join(lines)


def _render_critical_telegram(detailed, detection_source, cutoff_npt):
    """Render the CRITICAL bullet-point Telegram message (HTML)."""
    esc = html.escape
    parts = [
        "<b>%s</b>" % esc(detailed["headline"]),
        "",
        "• <b>What:</b> %s" % esc(detailed["what"] or "N/A"),
        "• <b>Where:</b> %s" % esc(detailed["where"] or "N/A"),
        "• <b>When:</b> %s" % esc(detailed["when"] or "N/A"),
        "• <b>Impact:</b> %s" % esc(detailed["impact"] or "Assessment ongoing"),
        "• <b>Status:</b> %s" % esc(detailed["status"]),
    ]
    if detailed["source"]:
        clean_src, is_gnews = _clean_url_for_telegram(detailed["source"])
        parts.append('• %s' % clean_src)
    parts.append(
        "<i>Detected via %s · fired %s · checked through %s</i>"
        % (esc(detection_source), esc(alert_fired_str(cutoff_npt)),
           esc(cutoff_npt)))
    return "\n".join(parts)


def _clean_url_for_telegram(url):
    """Replace Google News redirect URLs with a clickable HTML link.

    Google News RSS article URLs are opaque redirects that look ugly in
    Telegram.  When detected, return (html_link, is_gnews).  Normal URLs
    (kathmandupost.com, onlinekhabar.com, etc.) are wrapped in a clickable
    link too, with is_gnews=False.
    """
    esc = html.escape
    if url and "news.google.com/rss/articles" in url:
        return '<a href="%s">\U0001f4f0 Read article</a>' % esc(url, quote=True), True
    return ('<a href="%s">\U0001f517 Read article</a>' % esc(url or "", quote=True),
            False)


def telegram_alert_text(confidence, title, label, category, summary,
                        source_url, detection_source, cutoff_npt):
    """HTML message body pushed to the Telegram channel for one alert
    (simplified format for non-CRITICAL items)."""
    esc = html.escape
    clean_url, is_gnews = _clean_url_for_telegram(source_url)
    source_line = clean_url
    # The real NPT fire time rides on every non-CRITICAL alert too: the
    # simplified format used to drop cutoff_npt entirely, which left the
    # owner with no way to tell when a night alert actually fired.
    return (
        "\U0001f6a8 <b>BREAKING</b> [%s] %s\n"
        "%s · %s\n"
        "%s\n"
        "<i>Detected via %s · fired %s</i>"
    ) % (esc(confidence), esc(title),
         esc(label or "NEW"), esc(category),
         source_line,
         esc(detection_source), esc(alert_fired_str(cutoff_npt)))


def notify_telegram(verdict, confidence, label, paths, args, source_url,
                    cutoff_npt, detection_source, universal_override=False):
    """Best-effort Telegram push AFTER a successful email send/dry-run.

    Returns "" on success/skip or ";telegram_failed:<err>" which the caller
    appends to the log row's reason. NEVER raises; email/state semantics are
    untouched regardless of Telegram outcome.

    When *universal_override* is True (CRITICAL items), the detailed
    bullet-point format is used instead of the standard one-liner.
    """
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return ""                       # token absent => skip silently
    if universal_override:
        detailed = generate_detailed_alert(verdict)
        text = _render_critical_telegram(detailed, detection_source, cutoff_npt)
    else:
        text = telegram_alert_text(
            confidence, verdict["title"], label,
            verdict.get("category_suggestion") or "local_city",
            verdict["summary"], source_url, detection_source, cutoff_npt)
    if args.dry_run:
        # --dry-run never touches the network: the exact message body lands
        # in state/telegram-preview.txt next to email-preview.html.
        write_text_atomic(paths["telegram_preview"], text)
        return ""
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not chat_id:
        chat_id = tg_get_chat_id(token)
    if not chat_id:
        return ";telegram_failed:no_chat_id"
    ok, err = tg_send(token, chat_id, text)
    return "" if ok else ";telegram_failed:%s" % err


# ---------------------------------------------------------------------------
# Alert processing (stages 4–8 for one tripped run)
# ---------------------------------------------------------------------------

def kathmandu_now_str():
    now = datetime.now(timezone.utc).replace(tzinfo=None) + KATHMANDU_OFFSET
    return now.strftime("%Y-%m-%d %H:%M")


def alert_fired_str(fired_npt=None):
    """Unambiguous NPT fire time for a breaking/urgent alert.

    A breaking alert is EVENT-driven, never slot-driven: it must never be
    labelled morning/evening (that word belongs to the 2x/day digest only).
    The owner reads these at night on a phone, so the wall-clock time it
    actually fired is stamped on every human-visible surface instead.

    Reuses the existing kathmandu_now_str() helper (single NPT clock in this
    module, no hand-rolled +05:45 shift). Format matches the digest's
    "Information checked through: YYYY-MM-DD HH:MM NPT" convention so the
    owner reads one clock format everywhere.
    """
    fired = fired_npt or kathmandu_now_str()
    return "%s NPT" % fired


def alert_subject(subject, fired_npt=None):
    """Append the real NPT fire time to an alert subject.

    The subject is the first thing rendered on a lock screen, so it is the
    one surface that can never be ambiguous about when the alert fired.
    """
    return "%s — %s" % (subject, alert_fired_str(fired_npt))


def process_alert(verdict, matched_candidates, state, paths, args,
                  smtp_factory=None, now_utc=None):
    """Stages 4→8. Returns (sent:bool, final_level:int, confidence:str,
    event_id:str, reason:str)."""
    smtp_factory = smtp_factory or default_smtp_factory
    now_utc = now_utc or datetime.now(timezone.utc)
    today_iso = story_state.kathmandu_today().isoformat()
    # Where the top tripped item came from (worker dispatch payload; the
    # standalone scheduled run has no payload -> "google-news").
    detection_source = payload_detection_source()

    level = verdict["level"]
    confidence = verdict["confidence"]
    notes = []
    # A fast-path verdict rides the SAME stages as an AI one, so this note is
    # the record that the classifier was bypassed. It reaches the log row and
    # therefore the run summary, which is the only honest place for it: a
    # bypass that cannot be read afterwards is indistinguishable from a
    # classifier that happened to agree.
    if verdict.get("hard_evidence_fast_path"):
        notes.append("hard_evidence_fast_path(no_ai_verdict)")

    # --- Stage 5: spoiler veto -------------------------------------------
    ent_cats = set()
    if verdict.get("category_suggestion") in vs.ENTERTAINMENT_CATEGORIES:
        ent_cats.add(verdict["category_suggestion"])
    for cand in matched_candidates or []:
        if cand.get("domain_category") in vs.ENTERTAINMENT_CATEGORIES:
            ent_cats.add(cand["domain_category"])
    veto_reason = None
    for cat in sorted(ent_cats):
        reason = vs.spoiler_violation(
            cat, "none", "%s %s" % (verdict["title"], verdict["summary"]))
        if reason:
            veto_reason = reason
            break
    if veto_reason:
        level = 1
        notes.append(veto_reason)

    # Category-agnostic alert-path spoiler veto: fires before any level-3
    # alert can be sent, even when no candidate/verdict carries an
    # entertainment category.
    if level == 3 and ALERT_SPOILER_RE.search(
            "%s %s" % (verdict["title"], verdict["summary"])):
        level = 1
        notes.append("spoiler_veto_alert_path")

    # --- Stage 4: verification (level 3 only) -----------------------------
    developing_notice = ""
    if level == 3:
        domains, tier1_hit, recomputed = verify_level3(verdict,
                                                       matched_candidates)
        if recomputed != confidence:
            notes.append("confidence_adjusted:%s->%s" % (confidence, recomputed))
            confidence = recomputed
        if confidence == "LOW":
            if verdict.get("escalated_from_previous"):
                developing_notice = DEVELOPING_NOTICE_TEXT
            else:
                level = 2
                notes.append("low_confidence_send_block_downgrade")
        if RUMOR_RE.search("%s %s" % (verdict["title"], verdict["summary"])):
            if level == 3:
                level = 2
            notes.append("rumor_marker_level_cap")

    # --- Universal importance override (after classification + verification,
    #     before dedup): items matching universal-importance criteria bypass
    #     the personal-relevance filter regardless of level/confidence.
    #     Two-signal design:
    #       AI says universal_importance=true  →  trust AI judgment (override)
    #       regex check_universal_importance   →  deterministic confirmation
    #       Both true                          →  double-confirm (strong)
    universal_override = False
    ai_universal = verdict.get("universal_importance") is True
    regex_universal = check_universal_importance(verdict)
    if ai_universal or regex_universal:
        universal_override = True
        if level < 3:
            level = 3
        if CONFIDENCE_RANK.get(confidence, 0) < CONFIDENCE_RANK["HIGH"]:
            confidence = "HIGH"
        notes.append("universal_importance_override"
                     "(ai=%s,regex=%s)" % (ai_universal, regex_universal))
        print("universal_importance override applied: %s" % verdict["title"])

    # --- Stage 6: alert dedup ----------------------------------------------
    first_url = ""
    for s in verdict.get("sources") or []:
        if s.get("url"):
            first_url = s["url"]
            break
    if not first_url and matched_candidates:
        first_url = matched_candidates[0].get("url", "")
    story_stub = {
        "title": verdict["title"],
        "event_key": verdict.get("event_key") or None,
        "url": first_url,
        "sources": [s.get("url", "") for s in verdict.get("sources") or []],
    }
    eid = story_state.match_event(story_stub, state.get("stories", []))
    if eid is not None:
        ev = next((e for e in state.get("stories", [])
                   if isinstance(e, dict) and e.get("event_id") == eid), None)
        if ev is not None and ev.get("alerted"):
            prev_rank = CONFIDENCE_RANK.get(ev.get("last_confidence") or "", 0)
            rank_increased = CONFIDENCE_RANK[confidence] > prev_rank
            try:
                stored_source_count = int(ev.get("source_count") or 0)
            except (TypeError, ValueError):
                stored_source_count = 0
            now_sources = {story_state.normalize_url(s.get("url", ""))
                           for s in verdict.get("sources") or []}
            now_sources.discard("")
            source_count_grew = len(now_sources) > stored_source_count
            was_resolved = ev.get("status") == "RESOLVED"
            # escalated_from_previous alone is no longer trusted: re-alert on
            # escalation only for a deterministic signal (rank increase, more
            # distinct sources than stored, or revival of a RESOLVED event).
            if not (rank_increased or source_count_grew or was_resolved):
                ev["last_seen"] = today_iso
                ev["updated_at"] = story_state.kathmandu_now_iso()
                story_state.save_state_atomic(paths["active"], state)
                return False, level, confidence, eid, "duplicate_no_escalation"

    # --- Gate: only a verified level 3 ever interrupts ----------------------
    if level != 3:
        return False, level, confidence, eid or "", \
            "; ".join(notes) or "below_alert_level"

    # --- Stage 7: render + send --------------------------------------------
    # Determine event_label early for CRITICAL detection (the final label
    # comes from story_state.upsert_sighting in Stage 8, but we need the
    # CRITICAL flag here for rendering decisions).
    alert_label = "CRITICAL" if universal_override else ""
    source_lines, source_html = [], []
    for s in verdict.get("sources") or []:
        line = "%s — %s" % (s.get("name") or "source", s.get("url", ""))
        source_lines.append(line)
        source_html.append('<li><a href="%s">%s</a></li>'
                           % (s.get("url", ""), s.get("name") or "source"))
    ctx = {
        "TITLE": verdict["title"],
        "CONFIDENCE": confidence,
        "LEVEL": str(level),
        "SUMMARY": verdict["summary"],
        "WHY_URGENT": verdict["why_urgent"],
        "SOURCES": "".join(source_html),
        "CUTOFF_NPT": kathmandu_now_str(),
        "DEVELOPING_NOTICE": developing_notice,
        "DETECTED_VIA": "Detected via: %s" % detection_source,
        "_SOURCE_LINES": source_lines,
    }
    cutoff_npt = ctx["CUTOFF_NPT"]
    # Real fire time, stamped on every human-visible alert surface. Never a
    # morning/evening slot: a breaking alert is event-driven.
    ctx["FIRED_NPT"] = alert_fired_str(cutoff_npt)
    if universal_override:
        # --- CRITICAL: detailed bullet-point format ---
        detailed = generate_detailed_alert(verdict)
        html_body = _render_critical_html(
            detailed, confidence, detection_source, cutoff_npt)
        text_body = _render_critical_plain(
            detailed, confidence, detection_source, cutoff_npt)
        subject = alert_subject(
            "🚨 CRITICAL: %s [%s]" % (verdict["title"], confidence), cutoff_npt)
    else:
        # --- Standard one-liner format ---
        template_html = FALLBACK_ALERT_HTML
        try:
            with open(ALERT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                template_html = f.read()
        except OSError:
            pass  # templates/alert.html not created yet -> inline fallback
        html_body = render_alert(template_html, ctx)
        # Detection-source provenance, injected AFTER rendering so it reaches
        # the preview/live email even when templates/alert.html does not carry a
        # dedicated token (render_alert only substitutes tokens a template has).
        via_html = ('<p style="color:#555;font-size:0.9em;">Detected via: %s'
                    '</p>' % html.escape(detection_source))
        body_close = html_body.lower().rfind("</body>")
        html_body = (html_body[:body_close] + via_html + html_body[body_close:]
                     if body_close >= 0 else html_body + via_html)
        text_body = build_plain_text(ctx)
        subject = alert_subject(
            "🚨 Breaking: %s [%s]" % (verdict["title"], confidence), cutoff_npt)

    alert_id = stable_alert_id(verdict)
    bstate = load_breaking_state(paths["breaking_state"])

    # --- Stage 7a: ntfy FIRST; email/Telegram below are FALLBACKS -----------
    # `detailed` is only built on the CRITICAL path; ntfy falls back to the
    # verdict fields on the standard path, so one render serves both.
    ntfy_state, ntfy_note = notify_ntfy(
        verdict, detailed if universal_override else {}, confidence,
        alert_label, paths, args, first_url, detection_source, cutoff_npt)
    delivery_notes = [ntfy_note] if ntfy_note else []

    if args.dry_run:
        # --dry-run skips SMTP; the rendered alert lands in
        # state/email-preview.html for inspection instead.
        write_text_atomic(paths["preview"], html_body)
    elif ntfy_state == "delivered":
        # Primary channel answered, so email and Telegram are SKIPPED on
        # purpose: paging all three for one event is three notifications for
        # one event, and ntfy is the channel the owner reads. The skips are
        # recorded rather than left as silence.
        delivery_notes.append("email_skipped:ntfy_delivered")
        delivery_notes.append("telegram_skipped:ntfy_delivered")
        # success clears any earlier pending marker for this alert
        if bstate["pending_alert"].pop(alert_id, None) is not None:
            write_json_atomic(paths["breaking_state"], bstate)
    else:
        try:
            send_email(smtp_factory, subject, html_body, text_body)
        except Exception as exc:
            # TOTAL FAILURE (primary and fallback both down). `sent` stays
            # False and the ntfy note is kept, so neither failure is hidden
            # behind the other's success.
            bstate["pending_alert"][alert_id] = today_iso
            write_json_atomic(paths["breaking_state"], bstate)
            return False, level, confidence, eid or "", \
                "; ".join(delivery_notes + [
                    "smtp_error: %s"
                    % collect_news.clean_ws(str(exc))[:160]])
        # success clears any earlier pending marker for this alert
        if bstate["pending_alert"].pop(alert_id, None) is not None:
            write_json_atomic(paths["breaking_state"], bstate)

    # --- Stage 8: state updates (only after successful send/dry-run) --------
    reliability = "tier1" if any(is_tier1_domain(d) for d in
                                 (collect_news.netloc_of(s.get("url", ""))
                                  for s in verdict.get("sources") or [])) \
        else "tier3"
    story_for_state = {
        "title": verdict["title"],
        "category": verdict.get("category_suggestion") or "local_city",
        "event_key": verdict.get("event_key") or None,
        "url": first_url,
        "sources": [s.get("url", "") for s in verdict.get("sources") or []],
        "summary": verdict["summary"],
        "latest_development": verdict["summary"],
        "reliability": reliability,
    }
    event_id, event_label = story_state.upsert_sighting(state, story_for_state,
                                                        today_iso)
    # Override label for universal-importance items (must happen AFTER
    # upsert_sighting so the state event gets the real label first, then
    # we re-tag the in-memory label used for Telegram and the log row).
    if universal_override:
        event_label = "CRITICAL"
    ev = next((e for e in state["stories"]
               if e.get("event_id") == event_id), None)
    if ev is not None:
        ev["alerted"] = True
        ev["alert_count"] = int(ev.get("alert_count") or 0) + 1
        ev["last_alerted"] = story_state.kathmandu_now_iso()
        ev["last_confidence"] = confidence
        ev["detection_source"] = detection_source
    story_state.save_state_atomic(paths["active"], state)

    # --- Stage 7b: Telegram fan-out (FALLBACK, best-effort) ----------------
    # Runs only when the primary channel did NOT deliver, after the successful
    # SMTP send/dry-run AND after the state updates above, so a Telegram
    # failure can never alter email/state semantics — it only appends a note
    # to the log row's reason.
    tg_note = ""
    if ntfy_state != "delivered":
        try:
            tg_note = notify_telegram(verdict, confidence, event_label, paths,
                                      args, first_url, ctx["CUTOFF_NPT"],
                                      detection_source,
                                      universal_override=universal_override)
        except Exception as exc:
            tg_note = ";telegram_failed:%s" % collect_news.clean_ws(
                "%s: %s" % (type(exc).__name__, exc))[:120]
    reason = "sent"
    if notes:
        reason += "; " + "; ".join(notes)
    if delivery_notes:
        reason += "; " + "; ".join(delivery_notes)
    return True, level, confidence, event_id, reason + tg_note


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None, fetch_fn=None, ai_runner=None, smtp_factory=None):
    ap = argparse.ArgumentParser(
        description="Hourly breaking-news monitor (silent unless verified).")
    ap.add_argument("--state-dir", dest="state_dir", default="state")
    ap.add_argument("--config-dir", dest="config_dir", default="config")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--window-min", dest="window_min", type=int, default=90)
    ap.add_argument("--max-candidates", dest="max_candidates", type=int,
                    default=40)
    ap.add_argument("--sources-json", dest="sources_json", default="")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    t0 = time.perf_counter()
    _TG_CHAT_ID_CACHE.clear()      # chat-id auto-discovery cache is per-run

    state_dir = args.state_dir
    os.makedirs(state_dir, exist_ok=True)
    paths = {
        "active": os.path.join(state_dir, "active_stories.json"),
        "log": os.path.join(state_dir, "breaking_log.json"),
        "pending": os.path.join(state_dir, "breaking_pending.json"),
        "breaking_state": os.path.join(state_dir, "breaking_state.json"),
        "preview": os.path.join(state_dir, "email-preview.html"),
        "telegram_preview": os.path.join(state_dir,
                                         "telegram-preview.txt"),
        "ntfy_preview": os.path.join(state_dir, "ntfy-preview.txt"),
        # The delivery receipt the watchdog's breaking arm reads. See
        # record_breaking_attempt for why it exists and why it is written only
        # from a confirmed send.
        "breaking_delivery": os.path.join(state_dir, "breaking_delivery.json"),
    }

    # Active-story state: corruption is FATAL (exit 3), never auto-reset.
    try:
        state = story_state.load_state(paths["active"])
    except story_state.StateCorruptionError as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 3

    # Sources: bad override file is a config fatality (exit 1).
    try:
        sources = load_sources(args.sources_json)
    except ConfigError as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 1

    candidates, sources_ok, sources_failed, _stats = collect_candidates(
        sources, fetch_fn or default_fetch_fn, args.window_min,
        args.max_candidates)
    checked = len(candidates)

    def log_row(**extra):
        row = {"run_at": datetime.now(timezone.utc).isoformat(
                   timespec="seconds"),
               "checked": checked,
               "sources_ok": len(sources_ok),
               "sources_failed": len(sources_failed)}
        row.update(extra)
        row["cost_seconds"] = round(time.perf_counter() - t0, 2)
        append_breaking_log(paths["log"], row)
        return row

    # Stage 2: deterministic pre-gate.
    # When GMAIL_RECEIVER is set the pre-gate is bypassed so ALL candidates
    # proceed to AI classification — this supports a dedicated-receiver use
    # case where every detected item should be evaluated.
    bypass_pregate = bool(os.environ.get("GMAIL_RECEIVER"))
    if bypass_pregate:
        tripped, matched = True, candidates
        # pre_gate() never runs on the bypass path, so its side-channel still
        # holds whatever the LAST call in this process left there (or nothing).
        # Clear it: otherwise a bypass run logs a previous run's audit trail
        # against these candidates, which is exactly the "widened gate, no
        # evidence" failure the side-channel exists to prevent.
        pre_gate.last_why = []
        print("pre-gate bypassed: GMAIL_RECEIVER is set, forwarding %d "
              "candidates to AI classifier" % len(candidates))
    else:
        tripped, matched = pre_gate(candidates)
    # Why each candidate tripped, beside pre_gate_matched in the payload. A
    # wider gate is only auditable if the reason survives the run.
    pre_gate_why = list(getattr(pre_gate, "last_why", []))
    if not tripped:
        log_row(tripped=False)                     # silent heartbeat
        return 0

    # Stage 3a: the hard-evidence fast path, BEFORE the ladder. A headline that
    # states a mass-casualty death toll is not sent on the classifier's say-so
    # (see hard_evidence_fast_path for why it is authoritative and why it
    # fabricates nothing). The ladder is not consulted for that run, which is
    # recorded in the log row rather than left implicit.
    fast_verdict, fast_why = hard_evidence_fast_path(matched)

    # Stage 3: AI classify (fail-safe silence on any failure).
    write_json_atomic(paths["pending"], {
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window_min": args.window_min,
        "pre_gate_matched": [c.get("title", "") for c in matched],
        "pre_gate_why": pre_gate_why,
        "candidates": [_strip_internals(c) for c in candidates],
    })

    if fast_verdict is not None:
        print("breaking_monitor: HARD-EVIDENCE FAST PATH (%s) -- the AI ladder "
              "was not consulted for this run" % fast_why)
        verdict, ai_err = fast_verdict, None
    else:
        verdict, ai_err = ai_classify(paths["pending"], runner=ai_runner)

    if verdict is None:
        # The un-judged headlines ride the log row so the honesty gate can
        # carry them. The Worker dispatch payload is NOT enough on its own: a
        # scheduled run has no payload at all, so the notice would arrive
        # naming a fault and no event -- exactly the shape of the 2026-10-13
        # loss, where the owner was told the classifier had failed and not
        # what it had failed on.
        log_row(tripped=True, level=1, confidence="", event_id="",
                sent=False, pre_gate_why=pre_gate_why,
                unjudged_titles=[c.get("title", "") for c in matched][:5],
                reason="ai_fail_safe: %s" % (ai_err or "unknown")[:200])
        return 0

    sent, level, confidence, event_id, reason = process_alert(
        verdict, matched, state, paths, args, smtp_factory=smtp_factory)
    log_row(tripped=True, level=level, confidence=confidence,
            event_id=event_id, sent=sent, pre_gate_why=pre_gate_why,
            reason=reason[:200])

    if sent:
        print("breaking_monitor: ALERT sent level=%s confidence=%s (%s)"
              % (level, confidence, event_id))
    elif level >= 2 or any(mark in reason for mark in
                           ("smtp_error", "ntfy_failed", "ntfy_not_configured")):
        print("breaking_monitor: no send (level=%s confidence=%s reason=%s)"
              % (level, confidence, reason[:120]))
    # level-1 / silent outcomes print nothing beyond the log row.
    return 0


if __name__ == "__main__":
    sys.exit(main())
