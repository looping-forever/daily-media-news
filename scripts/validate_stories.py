#!/usr/bin/env python3
"""validate_stories.py — deterministic filtering pipeline for state/stories.json.

Pipeline (in order):
  1. Load input JSON (parse failure -> fatal, exit 1).
  2. Schema-validate every story; violations DROP the individual story.
     Unparseable top-level structure -> fatal, exit 1.
   1b. Derive `source_health` from the collector's `source_results` in
     candidates.json (scripts/source_health.py, pure + total). A missing,
     corrupt or legacy candidates file degrades to state "unknown" and never
     raises. This is what makes the freshness banner honest: it is computed
     here, not asserted by the research agent.
   2c/2d. Stories whose source_url host is KNOWN unreachable this run are
     DROPPED (anti-fabrication gate, reason `source_unreachable:<host>`).
     Hosts that were never attempted are unaffected.
   3. Drop stories whose title+summary contain an excluded keyword from
      config/interests.yml (simple hand-rolled YAML list parser).
   3b. Foreign-promo demotion (gentle, not a veto): entertainment stories
       matching PROMO_EVENT_RE at a foreign (non-Nepal/Kathmandu/Example City)
       location get priority capped at 4 and a `foreign_promo_demoted`
       filter_log entry; location null => skipped.
   4. Deterministic spoiler veto (regex on title+summary):
       - ENTERTAINMENT-ONLY (gaming/anime/manga_manhwa_manhua): spoiler_level
         medium|high OR any pattern hit -> drop. Non-entertainment hard news
         is NEVER vetoed for death/dies/killed terms (a flood death toll is
         news, not a spoiler).
  5. Crime/depression filter: depressing patterns drop the story unless an
     impact marker co-occurs in the same text (closures, warnings, outages...).
   6. Dedup vs history.json (7-day TTL) and intra-run duplicates
      (normalized URL or title token-overlap >= 0.7 Jaccard).
   6b. Active-event tracking via story_state: expired-deadline stories drop;
       stories whose matched ACTIVE event was already reported in this
       digest_time today drop (duplicate_of_active_event); every other
       survivor is upserted as a sighting and tagged event_id + label.
    7. Sort priority desc, personal_relevance desc (default = priority),
       reliability tier asc, title asc; cap at 15.
    7b. `for_you` screening: the display-only personal-consequence line is
       deleted from every story that fails one of six checks (length, banned
       opener, concreteness, hedges, third-person self-reference, summary
       restatement) or the numeral-grounding gate, or that is ineligible
       (personal_relevance / category / reliability), or that falls past the
       `for_you.max_stories` rank cap. Delete-only: never truncated, never
       substituted, and the story itself is never dropped. Config comes from
       config/profile.yml `for_you:` and fails CLOSED when unusable.
   8. ALWAYS write the --out payload (wrapper + filtered_count + filter_log
      + generated_at_npt + data_cutoff_npt).

Exit codes: 0 = >=1 survivor | 2 = 0 survivors (payload still written) | 1 = fatal.

Stdlib only. Windows-friendly (ASCII-only stdout).
"""

import argparse
import difflib
import hashlib
import ipaddress
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

try:
    import story_state
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import story_state

try:
    import source_health
except ImportError:  # direct-script fallback when cwd != scripts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import source_health

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTERTAINMENT_CATEGORIES = {"gaming", "anime", "manga_manhwa_manhua"}
VALID_CATEGORIES = {
    "action", "world_major", "local_city", "nepal", "education_tu",
    "college_local", "bank_local", "gaming", "anime",
    "manga_manhwa_manhua", "tech_career",
}
VALID_RELIABILITY = {"tier1", "tier2", "tier3", "tier4"}
VALID_SPOILER_LEVELS = {"none", "low", "medium", "high"}
VALID_DIGEST_TIMES = {"morning", "evening"}
VALID_RESEARCH_STATUS = {"full", "limited"}

REQUIRED_STR_FIELDS = [
    "title", "summary", "source_name",
    "spoiler_reason", "relevance_reason", "language",
]
OPTIONAL_NULLABLE_STR_FIELDS = ["published_at", "event_date", "location", "effective_date"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_STORIES = 15
MAX_FILTER_LOG = 50
HISTORY_TTL_DAYS = 7

# Media passthrough allowlist: a story's media URL must live on the story's
# own source_url domain OR one of these well-known image/video CDNs. The
# primary anti-invention guard is the candidate passthrough check (the URL
# must have come from the matching candidate); this list is the secondary
# domain gate. Google News RSS carries no media today, so this is a curated
# set of CDNs commonly used by news feeds.
MEDIA_CDN_ALLOWLIST = {
    # Contract-listed CDNs.
    "wp.com", "ytimg.com", "i.ytimg.com", "googleusercontent.com",
    # Common image/video CDNs seen in news feeds.
    "ggpht.com", "gstatic.com", "imgur.com", "redd.it", "twimg.com",
    "cloudinary.com", "akamaized.net", "cloudfront.net", "fastly.net",
    "ssl-images-amazon.com", "steamstatic.com", "wikimedia.org",
    "cdninstagram.com", "fbcdn.net", "pinimg.com", "githubusercontent.com",
    "vimeocdn.com", "imgix.net", "b-cdn.net", "cloudflarestream.com",
    "wixstatic.com", "squarespace-cdn.com", "medium.com", "substackcdn.com",
    "ghost.io", "wordpress.com",
}

# Spoiler word patterns. Faithful flattening of the contracted spec regex
# \b(dies|died|death|killed|dead\b|passes away|ending explained|leaks?\b|leaked|
#    datamine[d]?|raw scan|scanlation|spoiler)s?\b  (+ reveal-style phrases).
SPOILER_WORD_RE = re.compile(
    r"\b(?:dies|died|death|killed|dead|passes\s+away|ending\s+explained|"
    r"leaks?|leaked|datamined?|raw\s+scan|scanlation|spoilers?)\b",
    re.IGNORECASE,
)
REVEAL_PATTERNS = [
    re.compile(r"\bturns\s+out\s+to\s+be\b", re.IGNORECASE),
    re.compile(r"\brevealed\s+to\s+be\b", re.IGNORECASE),
    re.compile(r"\bidentity\s+reveal\b", re.IGNORECASE),
    re.compile(r"\bfinal\s+boss\b", re.IGNORECASE),
]
# Foreign-promo demotion (gentle assist, never a veto): entertainment stories
# about promo/merch/stock events located OUTSIDE Nepal are capped at priority 4.
PROMO_EVENT_RE = re.compile(
    r"\b(concert|first pitch|convention|expo|fan meeting|meet.and.greet|"
    r"merchandise|collab|collaboration|pop.up|store opening|ticket sales|"
    r"theme park|caf(e|\u00e9) collaboration|stock (hits|rallies|high|surges))\b",
    re.IGNORECASE,
)
NEPAL_AREA_RE = re.compile(
    r"\b(?:nepal|kathmandu|example city|example district)\b", re.IGNORECASE)
FOREIGN_PROMO_PRIORITY_CAP = 4

# Internal priority chips the research agent annotates stories with ("★ P5",
# "P7", "★P6"). They are an internal planning aid and must never reach
# user-facing markup, so they are stripped once at the schema boundary
# (validate_story) instead of in each of the three renderers.
INTERNAL_LABEL_RE = re.compile(r"[\u2605\u2606]?\s*P(?:10|[1-9])\b")
LABEL_TRIM_CHARS = " \t\u2605\u2606*-_|/,:;.\u2013\u2014\u00b7"

# Crime / depression filter.
CRIME_RE = re.compile(
    r"\b(?:murder|rape|sexual\s+assault|assault(?:ed)?|robbery|theft|stabbed|"
    r"shot\s+dead|found\s+dead|missing\s+person|body\s+found)\b",
    re.IGNORECASE,
)
IMPACT_MARKER_RE = re.compile(
    r"\b(?:clos(?:ed|ure)|closed\s+for|highway|road\s+blocked|evacuat\w*|"
    r"warning|outage|deadline|schedule\s+change|disrupt(?:ed|s)?)\b",
    re.IGNORECASE,
)

# Recurring forecast endpoints. The open-meteo Example City forecast URL is a
# static endpoint whose query params encode the request, so normalize_url()
# collapses every day's fetch to the same key. Forecasts are recurring by
# design (weather is a configured daily interest), so they must NOT be
# blocked by the 7-day URL TTL; only a same-calendar-day re-send is a
# duplicate. Add any future forecast/weather endpoints here.
FORECAST_URL_RE = re.compile(
    r"^api\.open-meteo\.com/v1/forecast(?:/|$)",
    re.IGNORECASE,
)


def is_forecast_url(url):
    """True for recurring forecast/weather endpoints (normalized form)."""
    return bool(FORECAST_URL_RE.match((url or "").strip()))


KATHMANDU_OFFSET = timedelta(hours=5, minutes=45)


def kathmandu_today():
    """Current date in Asia/Kathmandu (UTC+5:45).

    Equivalent to datetime.utcnow() + timedelta(hours=5, minutes=45) but without
    the Python 3.12+ DeprecationWarning on utcnow().
    """
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now_utc_naive + KATHMANDU_OFFSET).date()


def kathmandu_now_str():
    """Current timestamp in Asia/Kathmandu as 'YYYY-MM-DD HH:MM'."""
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now_utc_naive + KATHMANDU_OFFSET).strftime("%Y-%m-%d %H:%M")


def parse_utc_iso(value):
    """Parse an ISO-8601 timestamp into a naive-UTC datetime, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    txt = value.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def load_collected_at(path):
    """Read candidates.json and return its collected_at as naive-UTC datetime."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        return parse_utc_iso(data.get("collected_at"))
    return None


# ---------------------------------------------------------------------------
# Source-truth loaders (source_health derivation inputs)
#
# Both loaders are TOTAL: they return (value, warning) and never raise, because
# a missing or corrupt input must degrade the honesty signal to "unknown",
# never take the whole digest down.
# ---------------------------------------------------------------------------

def load_source_results(candidates_path):
    """Return (source_results, warning).

    source_results is the collector's per-source truth list, or None when the
    file is missing/corrupt or is a legacy file that predates the key. A
    legacy file is QUIET (it is an expected shape, not an error); an
    unreadable file warns once so a silent "unknown" is never a mystery.
    """
    try:
        with open(candidates_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return None, ("candidates.json unreadable (%s) - source_health unknown"
                      % exc)
    if not isinstance(data, dict):
        return None, None
    results = data.get("source_results")
    if not isinstance(results, list):
        return None, None
    return results, None


def load_candidate_pool(candidates_path):
    """Return (candidates, warning).

    The collector's own candidate list from candidates.json, or None when the
    file is missing/corrupt or carries no `candidates` list. None means
    UNMEASURED: an unmeasured pool is never evidence that a quiet day
    happened, so it can never turn a broken run into a quiet one.
    """
    try:
        with open(candidates_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return None, ("candidates.json unreadable (%s) - candidate pool unknown"
                      % exc)
    if not isinstance(data, dict):
        return None, None
    cands = data.get("candidates")
    if not isinstance(cands, list):
        return None, None
    return cands, None


def load_critical_hosts(config_dir):
    """Return (critical_hosts, warning) from config/collection.json.

    A missing/corrupt config, a missing key or an empty list all yield
    ([], warning): no host counts as critical and the caller states so once.
    """
    path = os.path.join(config_dir or ".", "collection.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return [], ("collection.json unreadable (%s) - critical_hosts empty, "
                    "source_health uses all sources only" % exc)
    raw = data.get("critical_hosts") if isinstance(data, dict) else None
    if not isinstance(raw, (list, tuple)):
        return [], ("collection.json critical_hosts missing or not a list - "
                    "critical_hosts empty, source_health uses all sources only")
    hosts = [h.strip() for h in raw if isinstance(h, str) and h.strip()]
    if not hosts:
        return [], ("collection.json critical_hosts is empty - "
                    "source_health uses all sources only")
    return hosts, None


def strip_internal_label(story):
    """Remove internal priority chips from story["label"] in place.

    Returns the cleaned label (or None when the story has no usable label).
    Chip-only labels such as "★ P5", "P7" and "★P6" become ""; a real event
    label that merely carries a chip ("UPDATED ★ P5") keeps the event label.
    """
    label = story.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    cleaned = re.sub(r"\s+", " ", INTERNAL_LABEL_RE.sub("", label))
    cleaned = cleaned.strip(LABEL_TRIM_CHARS).strip()
    story["label"] = cleaned
    return cleaned


# ---------------------------------------------------------------------------
# Media passthrough validation (image/video are OPTIONAL; never story-killers)
# ---------------------------------------------------------------------------

def build_candidate_media_map(candidates_path):
    """Build the candidate-media lookup used to enforce the passthrough rule.

    Returns {"by_raw_url": {url: entry}, "by_url": {normalized_url: entry},
    "by_title": [(title, entry), ...]} where entry = {"image": set(urls),
    "video": set(urls)}. A story's media URL must have come from the matching
    candidate; matching tries exact URL, normalized URL, then normalized title
    (see find_candidate_media). Missing/unparseable candidates.json yields an
    empty lookup (any story media then fails the passthrough check and is
    dropped, never the story).
    """
    try:
        with open(candidates_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"by_raw_url": {}, "by_url": {}, "by_title": []}
    if not isinstance(data, dict):
        return {"by_raw_url": {}, "by_url": {}, "by_title": []}
    cands = data.get("candidates")
    if not isinstance(cands, list):
        return {"by_raw_url": {}, "by_url": {}, "by_title": []}
    out = {"by_raw_url": {}, "by_url": {}, "by_title": []}
    for c in cands:
        if not isinstance(c, dict):
            continue
        url = c.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        entry = {"image": set(), "video": set()}
        for field in ("image", "video"):
            m = c.get(field)
            if isinstance(m, dict):
                mu = m.get("url")
                if isinstance(mu, str) and mu.strip():
                    entry[field].add(mu.strip())
        out["by_raw_url"][url.strip()] = entry
        out["by_url"][normalize_url(url)] = entry
        title = c.get("title")
        if isinstance(title, str) and title.strip():
            out["by_title"].append((title.strip(), entry))
    return out


def find_candidate_media(candidate_media, source_url, source_title):
    """Return the candidate media entry matching a story, else an empty entry.

    Match order (contract): (a) exact source_url == candidate.url, then
    (b) normalized URL match (scheme, trailing slash, query string and
    fragment stripped; host lowercased), then (c) normalized title match
    (lowercase, punctuation/whitespace stripped; difflib.SequenceMatcher
    ratio >= 0.85, with a token-overlap fallback using the same jaccard
    >= 0.7 threshold as the dedup stage).
    """
    empty = {"image": set(), "video": set()}
    if not isinstance(candidate_media, dict):
        return empty
    by_raw_url = candidate_media.get("by_raw_url", {})
    by_url = candidate_media.get("by_url", {})
    by_title = candidate_media.get("by_title", [])
    src = (source_url or "").strip()
    # (a) exact URL match.
    if src in by_raw_url:
        return by_raw_url[src]
    # (b) normalized URL match.
    src_norm = normalize_url(src)
    if src_norm in by_url:
        return by_url[src_norm]
    # (c) normalized title match.
    src_title_norm = normalize_title(source_title)
    if src_title_norm:
        for cand_title, entry in by_title:
            if difflib.SequenceMatcher(None, src_title_norm,
                                       normalize_title(cand_title)).ratio() >= 0.85:
                return entry
        src_tokens = title_tokens(source_title)
        for cand_title, entry in by_title:
            if jaccard(src_tokens, title_tokens(cand_title)) >= 0.7:
                return entry
    return empty


def is_private_or_local_host(host):
    """True for localhost, IP literals, and private/reserved/loopback ranges."""
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    # Strip IPv6 brackets ("[::1]" -> "::1").
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # hostname, not an IP literal
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_unspecified or addr.is_multicast)


def media_url_problem(url, source_url, allowed_domains, candidate_urls):
    """Return a reason string if a media URL is invalid, else None.

    Checks (contract): http(s) scheme only (no data:/javascript:), no
    localhost/private IP hosts, domain allowlist = the story's own source_url
    domain + CDN allowlist, and passthrough (the URL must be present in the
    matching candidate's media of the same kind).
    """
    if not isinstance(url, str) or not url.strip():
        return "media_schema: url must be a non-empty string"
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return "media_url: unparseable URL"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "media_url: scheme %r not allowed (http/https only)" % scheme
    host = (parsed.hostname or "").lower()
    if not host:
        return "media_url: missing host"
    if is_private_or_local_host(host):
        return "media_url: private/local host %r" % host
    if url not in candidate_urls:
        return "media_url: URL not present in candidate media"
    if not any(host == d or host.endswith("." + d) for d in allowed_domains):
        return "media_url: domain %r not in allowlist" % host
    return None


def validate_media_fields(story, candidate_media):
    """Drop invalid image/video fields from a story; NEVER drop the story.

    candidate_media: lookup from build_candidate_media_map(). Returns
    (story, dropped) where dropped is a list of (field, reason) tuples.
    Null/absent media is normalized to absent (contract: omit the field).
    """
    dropped = []
    cand = find_candidate_media(candidate_media, story.get("source_url", ""),
                                story.get("title", ""))
    allowed = set(MEDIA_CDN_ALLOWLIST)
    src_domain = netloc_of(story.get("source_url", ""))
    if src_domain:
        allowed.add(src_domain)
    for field in ("image", "video"):
        m = story.get(field)
        if m is None:
            story.pop(field, None)  # normalize null -> absent
            continue
        if not isinstance(m, dict):
            story.pop(field, None)
            dropped.append((field, "media_schema: %s must be an object" % field))
            continue
        reason = media_url_problem(m.get("url"), story.get("source_url", ""),
                                   allowed, cand.get(field, set()))
        if reason:
            story.pop(field, None)
            dropped.append((field, reason))
    return story, dropped


# ---------------------------------------------------------------------------
# Small shared helpers (self-contained so each script runs standalone)
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


def normalize_title(title):
    """Lowercase and strip punctuation/whitespace for fuzzy title matching."""
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_hash(title):
    return hashlib.sha1((title or "").strip().lower().encode("utf-8")).hexdigest()


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


def atomic_write_json(path, payload):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Excluded keywords (simple YAML subset parser — no yaml dependency)
# ---------------------------------------------------------------------------

def load_excluded_keywords(config_dir):
    """Parse the top-level `excluded_keywords:` list from config/interests.yml.

    Supports only the simple block style actually used by the file:
        excluded_keywords:
          - LEGO
          - 'Some Keyword'
    The block ends at the next top-level key (a line starting at column 0
    with `key:`). Returns a list of raw keyword strings (case kept; matching
    is done case-insensitively by the caller).
    """
    path = os.path.join(config_dir, "interests.yml")
    keywords = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return keywords
    in_block = False
    for raw in lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        top_level = re.match(r"^([A-Za-z0-9_\-]+)\s*:", line)
        if top_level:
            in_block = top_level.group(1) == "excluded_keywords"
            continue
        if in_block:
            item = re.match(r"^\s+-\s*(.+?)\s*$", line)
            if item:
                kw = item.group(1).strip().strip("'\"").strip()
                if kw:
                    keywords.append(kw)
    return keywords


# ---------------------------------------------------------------------------
# History loading (supports legacy {title,url,date} and current formats)
# ---------------------------------------------------------------------------

def load_history_entries(history_path):
    """Return list of normalized history entries:
    {url_normalized, title_hash, title, date_sent (date or None)}."""
    entries = []
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return entries
    stories = data.get("stories") if isinstance(data, dict) else None
    if not isinstance(stories, list):
        return entries
    for e in stories:
        if not isinstance(e, dict):
            continue
        title = e.get("title") or ""
        url_norm = e.get("url_normalized") or normalize_url(e.get("url", ""))
        thash = e.get("title_hash") or (title_hash(title) if title else "")
        date_sent = e.get("date_sent") or e.get("date")
        parsed_date = None
        if isinstance(date_sent, str):
            try:
                parsed_date = datetime.strptime(date_sent.strip(), "%Y-%m-%d").date()
            except ValueError:
                parsed_date = None
        entries.append({
            "url_normalized": url_norm,
            "title_hash": thash,
            "title": title,
            "date_sent": parsed_date,
        })
    return entries


def history_is_fresh(entry, today):
    """TTL: an entry is fresh while its age is <= HISTORY_TTL_DAYS days."""
    if entry["date_sent"] is None:
        return True  # unknown age -> treat as fresh (conservative dedup)
    return (today - entry["date_sent"]).days <= HISTORY_TTL_DAYS


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _is_str(v):
    return isinstance(v, str)


def validate_story(story, index):
    """Return (ok, reason). reason is None when ok."""
    def fail(why):
        return False, why

    if not isinstance(story, dict):
        return fail("schema: story is not an object")

    for field in REQUIRED_STR_FIELDS:
        if field not in story:
            return fail("schema: missing required field '%s'" % field)
        if not _is_str(story[field]):
            return fail("schema: field '%s' must be a string" % field)

    priority = story.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool):
        return fail("schema: priority must be an integer")
    if not (1 <= priority <= 10):
        return fail("schema: priority %r out of range 1-10" % priority)

    category = story.get("category")
    if category not in VALID_CATEGORIES:
        return fail("schema: invalid category %r" % (category,))

    source_url = story.get("source_url")
    if not _is_str(source_url) or not re.match(r"^https?://", source_url.strip(), re.IGNORECASE):
        return fail("schema: source_url must start with http(s)://")

    reliability = story.get("reliability")
    if reliability not in VALID_RELIABILITY:
        return fail("schema: invalid reliability %r" % (reliability,))

    spoiler_level = story.get("spoiler_level")
    if spoiler_level not in VALID_SPOILER_LEVELS:
        return fail("schema: invalid spoiler_level %r" % (spoiler_level,))

    supporting = story.get("supporting_sources")
    if supporting is not None:
        if not isinstance(supporting, list):
            return fail("schema: supporting_sources must be a list")
        for i, s in enumerate(supporting):
            if not isinstance(s, dict):
                return fail("schema: supporting_sources[%d] is not an object" % i)
            name, url = s.get("name"), s.get("url")
            if not _is_str(name) or not _is_str(url):
                return fail("schema: supporting_sources[%d] needs string name+url" % i)
            if not re.match(r"^https?://", url.strip(), re.IGNORECASE):
                return fail("schema: supporting_sources[%d].url must start with http(s)://" % i)

    for field in OPTIONAL_NULLABLE_STR_FIELDS:
        v = story.get(field)
        if v is not None and not _is_str(v):
            return fail("schema: field '%s' must be a string or null" % field)

    # New optional event-tracking fields (all null-tolerant).
    event_key = story.get("event_key")
    if event_key is not None and not _is_str(event_key):
        return fail("schema: field 'event_key' must be a string or null")

    personal_relevance = story.get("personal_relevance")
    if personal_relevance is not None:
        if not isinstance(personal_relevance, int) or isinstance(personal_relevance, bool):
            return fail("schema: personal_relevance must be an integer")
        if not (1 <= personal_relevance <= 10):
            return fail("schema: personal_relevance %r out of range 1-10" % (personal_relevance,))

    deadline = story.get("deadline")
    if deadline is not None:
        if not _is_str(deadline) or not DATE_RE.match(deadline.strip()):
            return fail("schema: field 'deadline' must match YYYY-MM-DD or be null")
        try:
            datetime.strptime(deadline.strip(), "%Y-%m-%d")
        except ValueError:
            return fail("schema: field 'deadline' is not a valid calendar date")

    # Label normalization is a schema-boundary concern, not a renderer one:
    # internal priority chips never belong in user-facing markup.
    strip_internal_label(story)

    # `for_you` is the display-only personal-consequence line. It is OPTIONAL
    # and it is NEVER null: absent means "render nothing", which is the only
    # shape every renderer wants. A wrong-typed or null value is normalized to
    # absent here (never a dropped story) so a malformed optional line can
    # never take a real headline down with it.
    if "for_you" in story:
        fy = story.get("for_you")
        if not _is_str(fy) or not fy.strip():
            story.pop("for_you", None)

    return True, None


# ---------------------------------------------------------------------------
# `for_you` — the display-only personal-consequence line
#
# WHY THIS REPLACED `why_it_matters`
# ---------------------------------
# `why_it_matters` was in REQUIRED_STR_FIELDS, so a paragraph of commentary was
# MANDATORY on every story, with no length cap, no format and no
# concreteness rule. It made the digest read like a book and it never said
# anything useful about the owner's week. `for_you` is the replacement, and
# there is exactly one slot: a second free-text field would double the waffle.
#
# THE CONTRACT
#   * type: string, or the key is ABSENT. Never null. Absent = render nothing.
#   * length: [min_length, max_length] characters, from config/profile.yml,
#     plus a hard byte ceiling (FOR_YOU_BYTE_CAP) that no profile value may
#     raise above. Bytes, not chars, because the ntfy push budget is 4096
#     BYTES and a char budget inside a byte-limited container is a guaranteed
#     overflow.
#   * It is DISPLAY-ONLY. It is never fed back to the agent, never a dedup
#     key (dedup is title/url, unchanged), never a source of fact, and never
#     the only place a deadline appears -- renderers keep reading `deadline`.
#     A hallucinated line can therefore at worst contradict the authoritative
#     date; it can never replace it.
#
# SCREENING IS DELETE-ONLY
#   Every check below REMOVES the key. Nothing is ever truncated, no generic
#   line is ever substituted, and a failing line is never kept with a warning:
#   a substitute is a guess, and a guess in a personal-consequence slot IS the
#   hallucination vector this field was introduced to close.
#
# WHAT THE GROUNDING GATE DOES *NOT* DO (stated plainly, on purpose)
#   The ground-token check requires every numeral in `for_you` to appear
#   verbatim in the story's own title/summary/deadline/effective_date/
#   event_date. That kills the realistic fabrication surface — an invented
#   deadline, fee or percentage almost always arrives wearing a number — and it
#   costs nothing and runs offline. It is NOT complete. A line can be
#   perfectly token-grounded and still semantically wrong:
#     story:  "12 Sep board meeting discusses the form"
#     for_you:"Form closes 12 Sep"     <-- every numeral grounded, claim false
#   No deterministic check catches that. This gate is a floor, not a proof.
# ---------------------------------------------------------------------------

FOR_YOU_FIELD = "for_you"

# Hard byte ceiling. The profile's max_length is a CHARACTER budget; this is a
# BYTE budget and is deliberately not configurable, because the thing it
# protects (the ntfy 4096-byte push) is a byte budget. A profile that asks for
# a longer line is clamped here rather than overflowing a push.
FOR_YOU_BYTE_CAP = 150

# Restatement guard: a `for_you` that is mostly a re-run of `summary` is
# padding wearing a label, so it is deleted rather than rendered.
FOR_YOU_SUMMARY_JACCARD_MAX = 0.6

# A "content word" for the Jaccard comparison: a lowercase alphabetic run of
# at least this many characters. Letters only, so a shared number or a shared
# date does NOT manufacture agreement, and short strings ("tu", "ai") are not
# content. Mirrors the length convention already used by title_tokens().
FOR_YOU_CONTENT_MIN_LEN = 3

# Closed set. A line that asserts no concrete time, amount or object AND
# carries no imperative is a mood, not a consequence.
ACTION_VERB = frozenset("""
    apply register book check download submit attend pay withdraw avoid carry
    take bring wear wash plan prepare contact verify confirm watch skip
    reschedule cancel ignore save set use install update read open visit call
    claim renew extend enrol enroll sign agree opt keep leave stay
""".split())

# A numeral, a date, a time, a percentage or an amount: any maximal run of
# digits glued by date/time/decimal/thousands punctuation, with an optional
# trailing percent sign. "12", "2025-09-12", "9:30", "50%", "10,000", "2.5%".
FOR_YOU_NUMBER_RE = re.compile(r"\d+(?:[./:,-]\d+)*%?")

# `may` is on the hedge list AND is a month name. "Form closes May 12" is a
# deadline, not a hedge, and deleting it would gut the field on the one day it
# matters most. The month/modal split itself lives in _hedge_hit().
FOR_YOU_MONTHS = frozenset(
    "january february march april may june july august september october "
    "november december jan feb mar apr jun jul aug sep sept oct nov dec"
    .split())

# Config keys the screen needs. `for_you` fails CLOSED: if any of these is
# absent or malformed we cannot screen honestly, so the field is stripped
# everywhere rather than shipped unscreened. No copy of these values (or of
# the word lists) lives in this file — they are read from config/profile.yml
# and nowhere else, because a value in both places is a pair that drifts.
FOR_YOU_REQUIRED_INT = ("min_length", "max_length",
                        "min_personal_relevance", "max_stories")
FOR_YOU_REQUIRED_LIST = ("categories", "reliability_tiers",
                         "banned_openers", "hedge_markers")

# The fact-bearing fields a numeral in `for_you` is allowed to be grounded in.
FOR_YOU_GROUNDING_FIELDS = ("title", "summary", "deadline",
                            "effective_date", "event_date")


def content_words(text):
    """Lowercase alphabetic words of >= FOR_YOU_CONTENT_MIN_LEN characters."""
    pattern = r"[a-z]{%d,}" % FOR_YOU_CONTENT_MIN_LEN
    return set(re.findall(pattern, (text or "").lower()))


def for_you_number_tokens(text):
    """Every digit-bearing token in `text`, verbatim (numbers/dates/times/%)."""
    return FOR_YOU_NUMBER_RE.findall(text or "")


def _grounded(token, haystack):
    """True when `token` occurs verbatim in `haystack` on token boundaries.

    Boundaries matter: "500" must not be accepted because the summary said
    "1500". A hyphen/dot/slash inside the token is part of the token, so
    "2025-09-12" still matches itself.
    """
    return re.search(r"(?<![\w.])" + re.escape(token) + r"(?![\w])",
                     haystack) is not None


def for_you_config_problems(cfg):
    """Return a list of human-readable problems with the `for_you` config.

    Empty list == the screen can run. Anything else means fail closed.
    """
    problems = []
    if not isinstance(cfg, dict) or not cfg:
        return ["config/profile.yml has no `for_you:` block"]
    enabled = cfg.get("enabled")
    if not isinstance(enabled, bool):
        problems.append("for_you.enabled must be true/false")
    for key in FOR_YOU_REQUIRED_INT:
        val = cfg.get(key)
        if not isinstance(val, int) or isinstance(val, bool):
            problems.append("for_you.%s must be an integer" % key)
    for key in FOR_YOU_REQUIRED_LIST:
        val = cfg.get(key)
        if not isinstance(val, list) or not val or \
                not all(isinstance(v, str) and v.strip() for v in val):
            problems.append("for_you.%s must be a non-empty list of strings"
                            % key)
    if not problems:
        if cfg["min_length"] > cfg["max_length"]:
            problems.append("for_you.min_length > for_you.max_length")
        if cfg["max_length"] > FOR_YOU_BYTE_CAP:
            problems.append(
                "for_you.max_length %d exceeds the hard byte cap %d"
                % (cfg["max_length"], FOR_YOU_BYTE_CAP))
    return problems


def _banned_opener_hit(text, openers):
    """True when `text` OPENS with one of `openers` (case-insensitive, \b)."""
    stripped = (text or "").lstrip(" \t\"'()[]*-—:,.!?")
    for opener in openers:
        phrase = (opener or "").strip()
        if not phrase:
            continue
        if re.match(r"(?:%s)\b" % re.escape(phrase), stripped, re.IGNORECASE):
            return phrase
    return None


def _hedge_hit(text, markers):
    """True when a hedge marker is present, ignoring month-name false hits.

    `may` is on the hedge list AND is a month, so "Form closes May 12" would
    otherwise be deleted on the one day the line matters most. A hedge hit
    that is a month name is ignored only when it is CAPITALISED and is not
    followed by a lower-case word, which is what separates the month ("May
    12", "in May.") from the modal ("TU may publish", "May be extended").
    """
    for marker in markers:
        phrase = (marker or "").strip()
        if not phrase:
            continue
        m = re.search(r"\b%s\b" % re.escape(phrase), text or "", re.IGNORECASE)
        if not m:
            continue
        if phrase.lower() in FOR_YOU_MONTHS:
            matched = m.group(0)
            tail = (text or "")[m.end():m.end() + 2]
            if matched[:1].isupper() and not re.match(r"[a-z]", tail):
                continue  # a month, not a hedge
        return m.group(0)
    return None


def _has_concrete_token(text):
    """True for a digit, an ISO date, a clock time, am/pm, or an action verb.

    The owner's own good example is literally "a percent plus an imperative",
    so the shape being required is number/date/time OR a command aimed at him.
    """
    if re.search(r"\d", text):
        return True
    if re.search(r"\d{4}-\d{2}-\d{2}", text):
        return True
    if re.search(r"\b\d{1,2}:\d{2}\b", text):
        return True
    if re.search(r"\b(?:a\.?m\.?|p\.?m\.?)\b", text, re.IGNORECASE):
        return True
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    return bool(words & ACTION_VERB)


def screen_for_you(story, cfg):
    """Decide whether `story["for_you"]` may be rendered.

    Returns None when the line passes (or when there is no line to judge),
    otherwise a short machine-readable reason string. The caller DELETES the
    key on any non-None return. It is never shortened, never replaced.

    cfg is the parsed `for_you:` mapping from config/profile.yml.
    """
    line = story.get(FOR_YOU_FIELD)
    if not isinstance(line, str) or not line.strip():
        return None
    text = line.strip()

    # (0) Fail closed on unusable screening config: an unscreened line is
    #     exactly the defect this field was introduced to remove.
    problems = for_you_config_problems(cfg)
    if problems:
        return "config_incomplete: %s" % "; ".join(problems)
    if not cfg["enabled"]:
        return "disabled"

    # (0b) Eligibility: a line is only WRITTEN for a story that is personal,
    #      in an approved category, and on a source strong enough to carry a
    #      personal imperative. The summary may still appear either way.
    relevance = story.get("personal_relevance")
    if not isinstance(relevance, int) or isinstance(relevance, bool):
        relevance = 0
    if relevance < cfg["min_personal_relevance"]:
        return "ineligible_personal_relevance: %d < %d" % (
            relevance, cfg["min_personal_relevance"])
    if story.get("category") not in cfg["categories"]:
        return "ineligible_category: %r not in for_you.categories" % (
            story.get("category"),)
    if story.get("reliability") not in cfg["reliability_tiers"]:
        return "ineligible_reliability: %r not in for_you.reliability_tiers" % (
            story.get("reliability"),)

    # (1) Length. Under the floor it is a label, not a consequence; over the
    #     ceiling it is an essay, and over the byte cap it breaks a push.
    if len(text) < cfg["min_length"]:
        return "too_short: %d < %d" % (len(text), cfg["min_length"])
    if len(text) > cfg["max_length"]:
        return "too_long: %d > %d" % (len(text), cfg["max_length"])
    nbytes = len(text.encode("utf-8"))
    if nbytes > FOR_YOU_BYTE_CAP:
        return "too_long_bytes: %d > %d" % (nbytes, FOR_YOU_BYTE_CAP)

    # (2) Banned openers: the throat-clearing that made the old field read
    #     like a book ("This is important because ...").
    opener = _banned_opener_hit(text, cfg["banned_openers"])
    if opener:
        return "banned_opener: %r" % opener

    # (3) Concreteness: no number/date/time and no imperative == a mood.
    if not _has_concrete_token(text):
        return "not_concrete: no digit, date, time, am/pm or action verb"

    # (4) Hedges: a personal consequence cannot be conditional. The real
    #     offender from the owner's own text was "may signal further TU
    #     schedule changes" — hedged AND about him in the third person.
    hedge = _hedge_hit(text, cfg["hedge_markers"])
    if hedge:
        return "hedge_marker: %r" % hedge

    # (5) Third-person self-reference. The digest is written TO him.
    if re.search(r"\busers?\b", text, re.IGNORECASE):
        return "third_person_self_reference"

    # (6) Summary restatement: if it says the summary again it is padding.
    sim = jaccard(content_words(text), content_words(story.get("summary")))
    if sim > FOR_YOU_SUMMARY_JACCARD_MAX:
        return "summary_restatement: jaccard %.2f" % sim

    # (7) Honesty gate. Every numeral must be present in the story's own
    #     fact-bearing text. A fabricated fact nearly always arrives wearing
    #     a number; prose is allowed to be paraphrased, numerals are not.
    haystack = " ".join(
        story.get(f) if isinstance(story.get(f), str) else ""
        for f in FOR_YOU_GROUNDING_FIELDS)
    for token in for_you_number_tokens(text):
        if not _grounded(token, haystack):
            return "ungrounded_number: %r" % token

    return None


def rank_key_for_you(story):
    """(personal_relevance, priority) descending, with a stable title tail."""
    relevance = story.get("personal_relevance")
    if not isinstance(relevance, int) or isinstance(relevance, bool):
        relevance = story.get("priority")
    if not isinstance(relevance, int) or isinstance(relevance, bool):
        relevance = 0
    return (-relevance, -int(story.get("priority") or 0), story.get("title") or "")


def apply_for_you_screening(stories, cfg):
    """Delete `for_you` from every story that may not carry it. In place.

    Returns (stats, log_entries). log_entries are drop records shaped like
    the pipeline's filter_log entries, so the owner can see in the run summary
    exactly which lines were removed and why.
    """
    stats = {"kept": 0, "stripped": 0, "screened": 0, "capped": 0,
             "cap": cfg.get("max_stories")
             if isinstance(cfg, dict) else None}
    log = []

    def strip(story, reason):
        story.pop(FOR_YOU_FIELD, None)
        stats["stripped"] += 1
        log.append({"title": str(story.get("title", ""))[:200],
                    "reason": "for_you: %s" % reason})

    for story in stories:
        if FOR_YOU_FIELD not in story:
            continue
        reason = screen_for_you(story, cfg)
        if reason is None:
            stats["kept"] += 1
        else:
            stats["screened"] += 1
            strip(story, reason)

    # Rank cap: a line on every story is noise, and an uncapped byte cost is
    # an unpredictable ntfy payload. Highest (personal_relevance, priority)
    # keeps it; everything past the cap loses the field, not the story.
    cap = cfg.get("max_stories") if isinstance(cfg, dict) else None
    if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0:
        kept = [s for s in stories if FOR_YOU_FIELD in s]
        over = sorted(kept, key=rank_key_for_you)[cap:]
        for story in over:
            stats["capped"] += 1
            strip(story, "rank_cap: past for_you.max_stories (%d)" % cap)
        stats["kept"] = len(kept) - len(over)

    return stats, log


# ---------------------------------------------------------------------------
# `for_you` config reader (YAML subset — no yaml dependency)
# ---------------------------------------------------------------------------

def load_for_you_config(config_dir):
    """Parse the top-level `for_you:` block of config/profile.yml.

    Returns the parsed mapping (possibly empty) — never raises. Supports the
    styles the file actually uses: `key: scalar`, `key: [a, b, c]` inline
    lists, and block lists (`key:` then `  - item`). The block ends at the
    next top-level key. A missing file yields {} and is reported by
    for_you_config_problems(), which is what makes the screen fail closed.
    """
    path = os.path.join(config_dir or ".", "profile.yml")
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    in_block = False
    current_list_key = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        top_level = re.match(r"^([A-Za-z0-9_\-]+)\s*:", line)
        if top_level:
            in_block = top_level.group(1) == "for_you"
            current_list_key = None
            continue
        if not in_block:
            continue
        item = re.match(r"^\s+-\s+(.+?)\s*$", line)
        if item and current_list_key is not None:
            out[current_list_key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^\s+([A-Za-z0-9_\-]+)\s*:\s*(.*?)\s*$", line)
        if not kv:
            continue
        key, raw_value = kv.group(1), kv.group(2)
        if raw_value.startswith("#"):
            raw_value = ""
        if raw_value.startswith("[") and raw_value.endswith("]"):
            body = raw_value[1:-1].strip()
            out[key] = [_scalar(p) for p in body.split(",") if p.strip()] if body else []
            current_list_key = None
        elif raw_value:
            out[key] = _scalar(raw_value)
            current_list_key = None
        else:
            out[key] = []
            current_list_key = key
    return out


def _scalar(token):
    """Coerce a YAML-subset scalar: bool, int, or bare/quoted string."""
    t = (token or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    t = t.split(" #", 1)[0].strip()
    low = t.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.match(r"^-?\d+$", t):
        return int(t)
    return t


# ---------------------------------------------------------------------------
# Filter stages
# ---------------------------------------------------------------------------

def spoiler_violation(category, spoiler_level, text):
    """Return a reason string if the spoiler veto fires, else None.

    The spoiler veto is ENTERTAINMENT-ONLY (config/spoiler-policy.yml is
    entirely about gaming/anime/manga spoilers). Non-entertainment hard news
    is never vetoed for terms like death/dies/killed — a flood death toll is
    news, not a spoiler.
    """
    if category not in ENTERTAINMENT_CATEGORIES:
        return None
    word_hit = SPOILER_WORD_RE.search(text)
    reveal_hit = next((p.pattern for p in REVEAL_PATTERNS if p.search(text)), None)
    if spoiler_level in ("medium", "high"):
        return "spoiler_veto: entertainment story (category=%s) with spoiler_level=%s" % (category, spoiler_level)
    if word_hit:
        return "spoiler_veto: entertainment story (category=%s) matched spoiler pattern (%s)" % (category, word_hit.group(0).lower())
    if reveal_hit:
        return "spoiler_veto: entertainment story (category=%s) matched reveal pattern" % category
    return None


def crime_violation(text):
    if not CRIME_RE.search(text):
        return None
    if IMPACT_MARKER_RE.search(text):
        return None  # practical impact context (closure/warning/outage) keeps it
    hit = CRIME_RE.search(text)
    return "crime_filter: depressing/crime content without impact markers (%s)" % hit.group(0).lower()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FROZEN CONTRACT 1d: what KIND of empty this digest is
# ---------------------------------------------------------------------------
# A zero-story digest used to be one undifferentiated outcome: the run built an
# "empty digest" email, marked itself delivered and went green. That is the
# same class of dishonesty this rebuild exists to remove, because "the sources
# were fine and there was genuinely no news" and "every source returned
# nothing usable" demand opposite responses -- one is a quiet day, the other is
# an outage that must page the owner and must let the fallback ladder fire.
#
# The classification below is a function of MEASURED SOURCE PRODUCTION and
# never of the research agent's opinion. `raw` is what the collector handed
# over, `final` is what survived this validator, `source_health` is
# scripts/source_health.py's pure derivation of what the hosts actually did,
# and the CANDIDATE POOL is the collector's own list from candidates.json.
#
# WHY THE POOL IS AN INPUT: `source_health.state` is `degraded` on every run
# while a critical host stays dark (tu.edu.np has been timing out for days),
# so a health-only rule files EVERY empty digest as `broken` -- including a
# same-day second run where 139 candidates were fetched and every published
# one had already been sent. `degraded` answers "is a critical host dark?",
# NOT "did collection work?". A non-empty candidate pool answers the second
# question directly, and that is the question that separates a quiet day from
# an outage.

#: `source_health.state` values that mean "we could not confirm that any source
#: produced something usable". `degraded` = a critical host is dark; `empty` =
#: sources answered with nothing usable; `unknown` = the check itself was
#: unavailable (missing/misconfigured critical_hosts, or a missing
#: candidates.json). All three mean the EMPTY is not a quiet day.
#:
#: `unknown` is deliberately in this set and NOT treated as `quiet_day`: an
#: unmeasurable state is not evidence of a quiet day, and calling it one is
#: exactly how an outage gets filed under "nothing to report".
BROKEN_HEALTH_STATES = ("degraded", "empty", "unknown")


def _count(value):
    """Best-effort int from a health counter; never raises."""
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


#: Title-token overlap at or above this means "the same story". Shared by the
#: Stage 6 dedup and the empty-outcome measurement so the two cannot drift.
DEDUP_OVERLAP_THRESHOLD = 0.7


def _usable_candidates(candidates):
    """The candidate dicts carrying a title or a URL to match on."""
    usable = []
    if not isinstance(candidates, (list, tuple)):
        return usable
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        title = cand.get("title")
        url = cand.get("url")
        if (isinstance(title, str) and title.strip()) or \
                (isinstance(url, str) and url.strip()):
            usable.append(cand)
    return usable


def history_coverage(candidates, fresh_history):
    """How many collected candidates the fresh history already covers.

    A candidate counts as covered when any dedup rule hits: normalized URL,
    exact title hash, or title-token overlap >= DEDUP_OVERLAP_THRESHOLD (the
    same rule Stage 6 applies). Forecast URLs are excluded from the URL rule
    because they recur by design and Stage 6 re-sends them on a new day.

    This is a MEASUREMENT of how much of the pool was old news. It is not the
    decision: the empty rule only needs to know that the pool was not empty.
    """
    urls, hashes, known_tokens = set(), set(), []
    for entry in fresh_history or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url_normalized")
        if isinstance(url, str) and url:
            urls.add(url)
        thash = entry.get("title_hash")
        if isinstance(thash, str) and thash:
            hashes.add(thash)
        tokens = title_tokens(entry.get("title") or "")
        if tokens:
            known_tokens.append(tokens)

    pool = _usable_candidates(candidates)
    matched = by_url = by_title = by_overlap = 0
    for cand in pool:
        url_norm = normalize_url(cand.get("url") or "")
        title = cand.get("title") if isinstance(cand.get("title"), str) else ""
        if url_norm and url_norm in urls and not is_forecast_url(url_norm):
            by_url += 1
        elif title.strip() and title_hash(title) in hashes:
            by_title += 1
        else:
            tokens = title_tokens(title)
            if tokens and any(jaccard(tokens, known) >= DEDUP_OVERLAP_THRESHOLD
                              for known in known_tokens):
                by_overlap += 1
            else:
                continue
        matched += 1
    return {"total": len(pool), "matched": matched, "by_url": by_url,
            "by_title": by_title, "by_overlap": by_overlap}


def collection_path_evidence(candidates, fresh_history, source_health):
    """Measure whether the COLLECTION path delivered candidates this run.

    ``ok`` is True only when candidates.json was readable AND carried at least
    one usable candidate. That measures the collector, not the writer: a
    non-empty pool proves fetching and parsing worked even when
    ``source_health.state`` is ``degraded``, because one dark critical host can
    coexist with candidates from every other source.
    """
    measured = isinstance(candidates, (list, tuple))
    pool = _usable_candidates(candidates)
    coverage = history_coverage(pool, fresh_history)
    health = source_health if isinstance(source_health, dict) else {}
    all_block = health.get("all")
    all_block = all_block if isinstance(all_block, dict) else {}
    critical = health.get("critical")
    critical = critical if isinstance(critical, dict) else {}

    if not measured:
        summary = "candidates.json unmeasured, pool unknown"
    elif not pool:
        summary = "0 usable candidates"
    else:
        summary = ("%d usable candidates (%d items found across %d sources, "
                   "%d critical ok); %d of %d already sent within the TTL "
                   "(url=%d, title=%d, near-title=%d)"
                   % (len(pool), _count(all_block.get("items_found")),
                      _count(all_block.get("total")),
                      _count(critical.get("ok")), coverage["matched"],
                      coverage["total"], coverage["by_url"],
                      coverage["by_title"], coverage["by_overlap"]))
    return {"measured": measured, "usable": len(pool),
            "ok": measured and bool(pool), "summary": summary}


def derive_empty_outcome(raw, final, source_health, candidates=None,
                         fresh_history=None):
    """Return ``(stories_outcome, empty_kind, empty_evidence)``.

    ``stories_outcome`` is ``"produced"`` or ``"empty"``. ``empty_kind`` is
    ``"quiet_day"``, ``"broken"`` or ``None`` (a produced digest has no kind).
    ``empty_evidence`` is a short human-readable string naming the measurement
    the decision was made from, so the retry ladder's alert can state WHICH
    fact triggered it rather than repeating the conclusion.

    The rows, in order:
      final > 0                                    -> produced, None
      final == 0 and raw > 0                       -> quiet_day (the VALIDATOR
                                                      dropped them: not a
                                                      source outage)
      final == 0 and raw == 0 and the collection
        path delivered >= 1 usable candidate
        and no critical feed is empty              -> quiet_day (the pool was
                                                      real, so fetching and
                                                      parsing worked; the only
                                                      claim left is dedup or
                                                      nothing new. A failed
                                                      critical host does NOT
                                                      override this -- see the
                                                      comment above)
      final == 0 and raw == 0 and (critical
        empty_hosts non-empty or state in
        {degraded, empty, unknown})                -> broken (collection failed
                                                      or could not be measured)
      final == 0 and raw == 0 and state in
        {ok, partial}                              -> quiet_day (sources were up
                                                      and the agent returned
                                                      nothing)
    """
    raw_n = len(raw) if isinstance(raw, (list, tuple)) else 0
    final_n = len(final) if isinstance(final, (list, tuple)) else 0
    health = source_health if isinstance(source_health, dict) else {}
    state = str(health.get("state") or "").strip().lower() or "unknown"
    critical = health.get("critical")
    critical = critical if isinstance(critical, dict) else {}
    raw_hosts = critical.get("empty_hosts")
    empty_hosts = sorted({h.strip() for h in raw_hosts
                          if isinstance(h, str) and h.strip()}) \
        if isinstance(raw_hosts, list) else []
    evidence = ("raw=%d final=%d source_health.state=%s critical=%d/%d ok, "
                "%d failed, %d empty_hosts"
                % (raw_n, final_n, state, _count(critical.get("ok")),
                   _count(critical.get("total")), _count(critical.get("failed")),
                   len(empty_hosts)))
    collection = collection_path_evidence(candidates, fresh_history, health)

    if final_n > 0:
        return "produced", None, evidence
    if raw_n > 0:
        return "empty", "quiet_day", (evidence + " - the validator dropped "
                                      "every collected candidate")
    if collection["ok"] and not empty_hosts:
        # Collection WORKED. `degraded` says a critical host is dark, not that
        # nothing was fetched, and the pool in hand proves otherwise. Filing
        # this as `broken` paged the owner for a quiet day (2026-10-26
        # evening: 139 candidates, 0 published), so the pool decides and the
        # degraded counters are reported instead of obeyed.
        #
        # `empty_hosts` is deliberately NOT overridden: a critical feed that
        # answered and produced nothing is a specific, actionable gap, and it
        # stays `broken` so it still pages.
        return "empty", "quiet_day", (evidence + " - collection path: "
                                      + collection["summary"]
                                      + " -> dedup/newness, not an outage")
    if empty_hosts or state in BROKEN_HEALTH_STATES:
        return "empty", "broken", (evidence + " - critical empty_hosts: "
                                   + (", ".join(empty_hosts) or "none")
                                   + " - collection path: "
                                   + collection["summary"])
    return "empty", "quiet_day", (evidence + " - sources were up; the agent "
                                    "returned nothing")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate/filter daily digest stories.")
    ap.add_argument("--in", dest="in_path", default="state/stories.json")
    ap.add_argument("--out", dest="out_path", default="state/stories.validated.json")
    ap.add_argument("--history", dest="history_path", default="state/history.json")
    # The edition date, derived ONCE by the workflow's slot-date step from the
    # slot's own firing. REQUIRED: the agent cannot know which slot it was asked
    # for, so without this there is no honest source for the day and the run
    # stops rather than publishing the agent's own `date`.
    ap.add_argument("--today", dest="today", default="")
    ap.add_argument("--config-dir", dest="config_dir", default="config")
    ap.add_argument("--state", dest="state_path", default="state/active_stories.json")
    ap.add_argument("--candidates", dest="candidates_path", default="state/candidates.json")
    args = ap.parse_args(argv)

    # ASCII-safe stdout for Windows consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # FAIL LOUDLY WITHOUT A SLOT DATE. Falling back to the agent's `date` here
    # is how two runs from one input printed two different days; falling back
    # to the clock is how the 10th evening slot was filed under the 11th.
    today_override = (args.today or "").strip()
    if not today_override:
        print("FATAL: --today is required. The edition date belongs to the SLOT "
              "and is derived by the workflow's slot-date step; there is no "
              "safe default, because the agent's own `date` is a hint and the "
              "clock is the defect.")
        return 1
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today_override):
        print("FATAL: --today must be YYYY-MM-DD, got %r" % args.today)
        return 1

    # -- Stage 1: load -----------------------------------------------------
    if not os.path.isfile(args.in_path):
        print("FATAL: input file not found: %s" % args.in_path)
        return 1
    try:
        with open(args.in_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print("FATAL: cannot parse input JSON (%s): %s" % (args.in_path, exc))
        return 1

    if not isinstance(data, dict) or not isinstance(data.get("stories"), list):
        print("FATAL: top-level structure unparseable: expected object with 'stories' list")
        return 1

    # Top-level wrapper fields pass through untouched when valid; tolerate
    # odd values with safe defaults instead of failing the whole run.
    digest_time = data.get("digest_time")
    if digest_time not in VALID_DIGEST_TIMES:
        digest_time = ""
    date_val = data.get("date") if isinstance(data.get("date"), str) else ""

    # The agent's `date` is a HINT, never the edition date. The slot-date step
    # owns the day, and its value is the one the marker, the archive name and
    # this header all print. A disagreement is RECORDED (`agent_date`) and
    # reported, never silently rewritten: a model that dated the run a day out
    # is evidence it may have searched the wrong day, and overwriting it hid
    # exactly that signal on 2026-10-26.
    agent_date = date_val
    if date_val != today_override:
        print("WARNING: the agent dated this edition %r but the slot is %r; the "
              "slot date wins and the agent's value is kept in `agent_date`"
              % (date_val, today_override))
    date_val = today_override
    research_status = data.get("research_status")
    if research_status not in VALID_RESEARCH_STATUS:
        research_status = ""
    notes = data.get("notes") if isinstance(data.get("notes"), str) else ""
    # The agent's own verdict, kept verbatim. The published research_status is
    # derived from measured reachability below (contract A4) so a total TU
    # outage can no longer be reported as "full" by construction.
    agent_research_status = (data.get("research_status")
                             if isinstance(data.get("research_status"), str)
                             else "")

    # -- Stage 1b: source health (measured, not asserted) ---------------------
    # The freshness banner is computed HERE, in deterministic code. The agent
    # prompt tells it to keep "full" when most domains succeed, so it cannot
    # notice a single-host university outage.
    source_results, source_results_warn = load_source_results(args.candidates_path)
    if source_results_warn:
        print("WARNING: %s" % source_results_warn)
    critical_hosts, critical_hosts_warn = load_critical_hosts(args.config_dir)
    if critical_hosts_warn:
        print("WARNING: %s" % critical_hosts_warn)
    source_health_block = source_health.compute_source_health(
        source_results, critical_hosts, kathmandu_now_str())
    # Two sets, two questions: `unreachable_hosts` drives the gate (ok-shielded),
    # `failed_hosts` is reported so the owner can see that a host had a failure
    # even when a sibling task kept it alive.
    unreachable_hosts = source_health.unreachable_hosts(source_results)
    failed_hosts = source_health.failed_hosts(source_results)

    raw_stories = data["stories"]
    filter_log = []

    def drop(story, reason):
        filter_log.append({"title": str(story.get("title", ""))[:200], "reason": reason})

    today = kathmandu_today()
    today_iso = today.isoformat()

    # -- Active-story state (event tracking) ---------------------------------
    # Missing state file -> fresh empty init (load_state handles that).
    try:
        state = story_state.load_state(args.state_path)
    except story_state.StateCorruptionError as exc:
        print("FATAL: %s" % exc)
        return 1
    # Expire semantics: deadline < yesterday -> RESOLVED (+ stale/archive moves).
    expire_counts = story_state.expire(state, today)
    active_events = [ev for ev in state.get("stories", [])
                     if isinstance(ev, dict) and ev.get("status") not in ("RESOLVED", "STALE")]
    reported_field = {"morning": "in_morning", "evening": "in_evening"}.get(digest_time)

    # -- Stage 2: schema validation ----------------------------------------
    stage2_counts = {"kept": 0, "dropped": 0}
    valid_stories = []
    for idx, story in enumerate(raw_stories):
        ok, reason = validate_story(story, idx)
        if ok:
            valid_stories.append(story)
            stage2_counts["kept"] += 1
        else:
            drop(story if isinstance(story, dict) else {}, reason)
            stage2_counts["dropped"] += 1

    if not raw_stories:
        print("Input contains zero stories.")

    # -- Stage 2b: foreign-promo demotion (gentle assist, never a veto) ------
    # Entertainment stories about concerts/conventions/merch/stock moves etc.
    # at a FOREIGN location get priority capped (not dropped). Location null
    # or Nepal-area (Nepal/Kathmandu/Example City) => rule skipped. drop() here only
    # appends to filter_log; the story stays in the pipeline.
    promo_demoted = 0
    for story in valid_stories:
        if story["category"] not in ENTERTAINMENT_CATEGORIES:
            continue
        location = story.get("location")
        if not isinstance(location, str) or not location.strip():
            continue  # unknown location -> leave alone
        if NEPAL_AREA_RE.search(location):
            continue  # domestic location -> leave alone
        text = "%s %s" % (story["title"], story["summary"])
        hit = PROMO_EVENT_RE.search(text)
        if not hit:
            continue
        capped_from = story["priority"]
        if capped_from > FOREIGN_PROMO_PRIORITY_CAP:
            story["priority"] = FOREIGN_PROMO_PRIORITY_CAP
        promo_demoted += 1
        drop(story, "foreign_promo_demoted: promo signal (%s) at foreign location (%s); priority %d capped at %d"
             % (hit.group(0).lower(), location.strip(), capped_from, FOREIGN_PROMO_PRIORITY_CAP))

    # -- Stage 2c: media validation (passthrough-only; NEVER drops stories) --
    # image/video are optional. Invalid media (bad scheme, private host,
    # domain outside allowlist, or URL not present in the matching candidate)
    # is DROPPED from the story; the story itself always survives this stage.
    candidate_media = build_candidate_media_map(args.candidates_path)
    media_dropped = {"image": 0, "video": 0}
    for story in valid_stories:
        _, dropped = validate_media_fields(story, candidate_media)
        for field, _reason in dropped:
            media_dropped[field] = media_dropped.get(field, 0) + 1

    # -- Stage 2d: anti-fabrication gate (source reachability) ----------------
    # A source outage is exactly when the agent is most tempted to invent
    # education_tu content, so a story whose source_url host went dark this run
    # is dropped. Two guards keep this from over-deleting real news:
    #   * unreachable_hosts() applies an OK-SHIELD -- a host with any successful
    #     task is still a working source, so a sibling failure never deletes its
    #     real stories. (Per-TASK matching is impossible here: source_results
    #     carries no candidate URLs, and a gnews task's url is the RSS search
    #     endpoint while its candidates are third-party article links.)
    #   * "skipped_budget" is not "failed" -- a task the budget never started
    #     proves nothing, and neither does a host that was never attempted.
    after_source_gate = []
    for story in valid_stories:
        host = source_health.canonical_host(netloc_of(story.get("source_url") or ""))
        if host and host in unreachable_hosts:
            drop(story, "source_unreachable:%s" % host)
        else:
            after_source_gate.append(story)
    source_gate_dropped = len(valid_stories) - len(after_source_gate)
    valid_stories = after_source_gate

    # -- Stage 3: excluded keywords -----------------------------------------
    excluded_keywords = [k.lower() for k in load_excluded_keywords(args.config_dir)]
    after_keywords = []
    for story in valid_stories:
        text = "%s %s" % (story["title"], story["summary"])
        hit = next((kw for kw in excluded_keywords if kw in text.lower()), None)
        if hit:
            drop(story, "excluded_keyword: %r" % hit)
        else:
            after_keywords.append(story)

    # -- Stage 4: spoiler veto ----------------------------------------------
    after_spoiler = []
    for story in after_keywords:
        text = "%s %s" % (story["title"], story["summary"])
        reason = spoiler_violation(story["category"], story["spoiler_level"], text)
        if reason:
            drop(story, reason)
        else:
            after_spoiler.append(story)

    # -- Stage 5: crime/depression filter ------------------------------------
    after_crime = []
    for story in after_spoiler:
        text = "%s %s" % (story["title"], story["summary"])
        reason = crime_violation(text)
        if reason:
            drop(story, reason)
        else:
            after_crime.append(story)

    # -- Stage 6: dedup -------------------------------------------------------
    fresh_history = [e for e in load_history_entries(args.history_path) if history_is_fresh(e, today)]
    hist_urls = {e["url_normalized"] for e in fresh_history if e["url_normalized"]}
    hist_hashes = {e["title_hash"] for e in fresh_history if e["title_hash"]}
    # Forecast URLs are recurring by design: only a same-calendar-day re-send
    # is a duplicate. hist_forecast_urls_today holds forecast URLs already
    # sent TODAY; a forecast sent on an earlier day (or with unknown date) is
    # allowed to be re-sent.
    hist_forecast_urls_today = {
        e["url_normalized"] for e in fresh_history
        if e["url_normalized"] and is_forecast_url(e["url_normalized"])
        and e["date_sent"] == today
    }

    after_dedup = []
    seen_urls = set()
    seen_token_sets = []  # list of (tokens, title) already accepted this run
    for story in after_crime:
        url_norm = normalize_url(story["source_url"])
        thash = title_hash(story["title"])
        tokens = title_tokens(story["title"])
        if url_norm and url_norm in hist_urls:
            if is_forecast_url(url_norm):
                # Recurring forecast: block only if the same forecast was
                # already sent TODAY (same calendar day). A forecast sent on
                # a previous day is allowed to be re-sent.
                if url_norm in hist_forecast_urls_today:
                    drop(story, "duplicate_of_history: forecast URL already sent today")
                    continue
            else:
                drop(story, "duplicate_of_history: URL already sent within TTL")
                continue
        if thash in hist_hashes and not is_forecast_url(url_norm):
            drop(story, "duplicate_of_history: title hash already sent within TTL")
            continue
        if url_norm and url_norm in seen_urls:
            drop(story, "duplicate_in_run: same normalized URL")
            continue
        dup_title = False
        for prev_tokens, prev_title in seen_token_sets:
            if jaccard(tokens, prev_tokens) >= DEDUP_OVERLAP_THRESHOLD:
                drop(story, "duplicate_in_run: title overlap >=0.7 with %r" % prev_title[:80])
                dup_title = True
                break
        if dup_title:
            continue
        seen_urls.add(url_norm)
        seen_token_sets.append((tokens, story["title"]))
        after_dedup.append(story)

    # -- Stage 6b: active-event matching / same-day report dedup --------------
    survivors = []
    for story in after_dedup:
        deadline_raw = story.get("deadline")
        deadline_dt = story_state.parse_date(deadline_raw)
        if deadline_dt is not None and deadline_dt < today:
            drop(story, "deadline_expired: deadline %s is before today" % deadline_dt.isoformat())
            continue
        eid = story_state.match_event(story, active_events)
        if eid is not None and reported_field is not None:
            ev = next((e for e in active_events
                       if isinstance(e, dict) and e.get("event_id") == eid), None)
            if ev is not None and ev.get(reported_field) == today_iso:
                drop(story, "duplicate_of_active_event: event %s already reported in today's %s edition"
                     % (eid, digest_time))
                continue
        event_id, label = story_state.upsert_sighting(state, story, today)
        story["event_id"] = event_id
        story["label"] = label
        survivors.append(story)

    # -- Stage 7: sort + cap ---------------------------------------------------
    tier_rank = {"tier1": 1, "tier2": 2, "tier3": 3, "tier4": 4}

    def relevance_key(s):
        v = s.get("personal_relevance")
        return v if isinstance(v, int) and not isinstance(v, bool) else s["priority"]

    survivors.sort(key=lambda s: (-s["priority"], -relevance_key(s),
                                  tier_rank[s["reliability"]], s["title"]))
    capped = survivors[MAX_STORIES:]
    for story in capped:
        drop(story, "below_priority_cap: ranked below top %d" % MAX_STORIES)
    final_stories = survivors[:MAX_STORIES]

    # -- Stage 7b: `for_you` screening (display-only personal line) ----------
    # Runs AFTER the final ordering so the rank cap matches what the reader
    # actually sees top-to-bottom. It can only DELETE the field; a story is
    # never dropped here, and `deadline`/`summary` are never touched.
    for_you_cfg = load_for_you_config(args.config_dir)
    for_you_stats, for_you_log = apply_for_you_screening(final_stories,
                                                        for_you_cfg)
    filter_log.extend(for_you_log)

    # -- Stage 8: write output payload (ALWAYS on non-fatal) -------------------
    generated_at_npt = kathmandu_now_str()
    generated_dt = datetime.strptime(generated_at_npt, "%Y-%m-%d %H:%M")
    # Honest cutoff (contract A6): load_collected_at yields naive-UTC, shifted
    # to Kathmandu wall time so it compares like-for-like with generated_dt.
    # min(), NOT max(): max() would advertise a freshness newer than the last
    # successful fetch, which is the lie this field exists to stop telling.
    # With no collected_at the only honest claim is "as of now".
    cutoff_dts = [dt + KATHMANDU_OFFSET
                  for dt in (load_collected_at(args.candidates_path),) if dt is not None]
    cutoff_dts.append(generated_dt)
    data_cutoff_npt = min(cutoff_dts).strftime("%Y-%m-%d %H:%M")

    # research_status is a function of MEASURED reachability, not of the
    # agent's free text: ok -> full, anything else -> limited.
    research_status = ("full" if source_health_block.get("state") == "ok"
                       else "limited")

    # Only an empty digest needs the candidate pool, so it is read lazily: a
    # normal run does not pay for another parse of a 135 KB file.
    candidates_pool, candidates_warn = None, None
    if not final_stories:
        candidates_pool, candidates_warn = load_candidate_pool(
            args.candidates_path)
        if candidates_warn:
            print("WARNING: %s" % candidates_warn)

    stories_outcome, empty_kind, empty_evidence = derive_empty_outcome(
        raw_stories, final_stories, source_health_block,
        candidates=candidates_pool, fresh_history=fresh_history)

    payload = {
        "digest_time": digest_time,
        "date": date_val,
        "agent_date": agent_date,
        "research_status": research_status,
        "agent_research_status": agent_research_status,
        "notes": notes,
        "generated_at_npt": generated_at_npt,
        "data_cutoff_npt": data_cutoff_npt,
        "source_health": source_health_block,
        "stories": final_stories,
        "stories_outcome": stories_outcome,
        "empty_kind": empty_kind,
        "empty_evidence": empty_evidence,
        "filtered_count": len(filter_log),
        "filter_log": filter_log[:MAX_FILTER_LOG],
    }
    out_dir = os.path.dirname(os.path.abspath(args.out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    atomic_write_json(args.out_path, payload)

    try:
        story_state.save_state_atomic(args.state_path, state)
    except OSError as exc:
        print("FATAL: cannot save active-story state (%s): %s" % (args.state_path, exc))
        return 1

    # -- Summary ----------------------------------------------------------------
    print("validate_stories: %s" % args.in_path)
    print("  schema:      %d kept, %d dropped" % (stage2_counts["kept"], stage2_counts["dropped"]))
    print("  keywords:    %d dropped (excluded_keywords loaded: %d)" %
          (len(valid_stories) - len(after_keywords), len(excluded_keywords)))
    print("  promo-demote: %d demoted (foreign promo events capped at priority %d)" %
          (promo_demoted, FOREIGN_PROMO_PRIORITY_CAP))
    print("  media:       %d dropped (image=%d, video=%d; stories never dropped for media)" %
          (media_dropped["image"] + media_dropped["video"],
           media_dropped["image"], media_dropped["video"]))
    print("  source-gate: %d dropped (unreachable hosts: %s | hosts with any "
          "failure: %s)"
          % (source_gate_dropped, ", ".join(sorted(unreachable_hosts)) or "none",
             ", ".join(sorted(failed_hosts)) or "none"))
    _sh_all = source_health_block["all"]
    _sh_crit = source_health_block["critical"]
    print("  source_health: %s | critical %d/%d ok, %d failed, %d skipped | all %d/%d ok, %d failed, %d skipped | reasons: %s"
          % (source_health_block["state"], _sh_crit["ok"], _sh_crit["total"],
             _sh_crit["failed"], _sh_crit["skipped"], _sh_all["ok"],
             _sh_all["total"], _sh_all["failed"], _sh_all["skipped"],
             ", ".join(_sh_crit["reasons"]) or "none"))
    print("  research_status: %s (agent said %r)"
          % (research_status, agent_research_status or "-"))
    print("  edition_date: %s (agent said %r)"
          % (date_val, agent_date or "-"))
    print("  spoilers:    %d dropped" % (len(after_keywords) - len(after_spoiler)))
    print("  crime:       %d dropped" % (len(after_spoiler) - len(after_crime)))
    print("  dedup:       %d dropped (%d fresh history entries)" %
          (len(after_crime) - len(after_dedup), len(fresh_history)))
    print("  events:      %d dropped (active events tracked: %d; expire: %d resolved, %d stale, %d archived)" %
          (len(after_dedup) - len(survivors), len(active_events),
           expire_counts["resolved"], expire_counts["stale"], expire_counts["archived"]))
    print("  cap:         %d dropped (cap %d)" % (len(capped), MAX_STORIES))
    print("  for_you:     %d kept, %d stripped (%d failed a screen, %d past the rank cap)"
          % (for_you_stats["kept"], for_you_stats["stripped"],
             for_you_stats["screened"], for_you_stats["capped"]))
    for problem in for_you_config_problems(for_you_cfg):
        print("  WARNING: for_you screen fails closed: %s" % problem)
    print("  survivors:   %d -> %s" % (len(final_stories), args.out_path))
    print("  outcome:     %s%s" % (stories_outcome,
                                  "" if empty_kind is None
                                  else " (%s: %s)" % (empty_kind, empty_evidence)))
    if empty_kind is not None:
        print("  empty-kind:  %s | collection path: %s"
              % (empty_kind,
                 collection_path_evidence(candidates_pool, fresh_history,
                                          source_health_block)["summary"]))
    print("  generated_at_npt: %s | data_cutoff_npt: %s" % (generated_at_npt, data_cutoff_npt))
    if filter_log:
        print("  filter log:")
        for entry in filter_log[:10]:
            print("    - [%s] %s" % (entry["reason"], entry["title"]))
        if len(filter_log) > 10:
            print("    ... and %d more" % (len(filter_log) - 10))

    return 0 if final_stories else 2


if __name__ == "__main__":
    sys.exit(main())
