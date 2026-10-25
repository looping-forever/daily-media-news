#!/usr/bin/env python3
"""scripts/enrich_media.py — post-agent media enrichment for the daily digest.

Google News RSS carries no media (no media:content/thumbnail/enclosure), so
candidates and stories have no image/video and the website/ntfy show no images.
This script is a best-effort post-agent enrichment step: for every story that
has neither an image nor a video, it fetches the story's article page and
extracts Open Graph meta tags (og:image / og:video), attaching real media to
the story.

Contract (frozen — the workflow calls it exactly like this):

    py scripts\\enrich_media.py

Reads:
  - state/stories.validated.json  (a list of story dicts, OR {"stories": [...]})
  - state/candidates.json         (a list of candidate dicts; candidates have
                                   "title", "url", and possibly "image"/"video")

For each story with NO image and NO video:
  1. Find the matching candidate:
       (a) exact source_url match,
       (b) normalized URL match (strip scheme/trailing slash/query/fragment,
           lowercase host),
       (c) normalized title match (lowercase, strip punctuation/whitespace;
           difflib.SequenceMatcher ratio >= 0.85).
  2. Fetch the candidate's url (or the story's source_url if no candidate
     matched) with a browser-like User-Agent header, Accept-Encoding: identity,
     timeout ~6s, following redirects (urllib default).
   3. Parse the HTML for og:image (priority og:image > og:image:url >
      og:image:secure_url > twitter:image) and og:video (priority og:video >
      og:video:url > og:video:secure_url, plus og:video:type). Uses
      html.parser.
   4. Resolve relative/protocol-relative URLs against the page's final URL.
   5. Attach story["image"] = {"url": ..., "type": ..., "source": "og"} and/or
      story["video"] = {"url": ..., "type": ..., "source": "og"} (only include
      "type" if known).
   6. On ANY error (fetch fail, parse fail, no meta found, non-http scheme,
      data: URI): skip that story silently (keep it media-less). Media is
      best-effort.

Security (SSRF guard): the candidate/story URL is rejected before fetching if
its host is localhost/private/reserved (reusing validate_stories'
is_private_or_local_host); the FINAL URL after redirects is re-validated; and
each resolved og:image/og:video/twitter:image URL is rejected if its host is
private/local. Host-less malformed URLs like "http://" are rejected too. A
violation skips that media entry (or that story) without crashing; the script
still exits 0.

Uses a thread pool of ~6 workers for the fetches. Writes the result back to
state/stories.validated.json ATOMICALLY (temp file + os.replace), preserving
the original structure (list vs {"stories": [...]}). Exits 0 always.

Optional CLI overrides for testing:
  --stories-json PATH     (default state/stories.validated.json)
  --candidates-json PATH  (default state/candidates.json)

The og-parsing logic is importable:
  parse_og_meta(html: str, base_url: str) -> dict
  with keys "image" (dict or None) and "video" (dict or None).
"""

import argparse
import base64
import difflib
import html.parser
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Reuse the private/local-host validation from validate_stories.py (same
# scripts dir). validate_stories.py has an `if __name__ == "__main__":` guard,
# so importing it does not execute its CLI. Fall back to inserting the scripts
# dir on sys.path when cwd != scripts/ (mirrors validate_stories' own pattern).
try:
    from validate_stories import is_private_or_local_host
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from validate_stories import is_private_or_local_host

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STORIES = "state/stories.validated.json"
DEFAULT_CANDIDATES = "state/candidates.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

FETCH_TIMEOUT = 6          # seconds
MAX_BYTES = 2 * 1024 * 1024  # cap the fetched page size
WORKERS = 6

# Google News batchexecute decode tuning.
_GN_DECODE_TIMEOUT = 9     # seconds per decode request
_GN_DECODE_DELAY = 0.75    # seconds between batchexecute decode calls (anti-429)
_GN_DECODE_CACHE = {}      # Google News redirect URL -> decoded article URL
_GN_DECODE_LOCK = threading.Lock()

# http(s) schemes only; data:/javascript:/etc. are rejected.
_HTTP_SCHEMES = ("http://", "https://")

# Hostnames (or hostname suffixes) of generic placeholder / CDN domains whose
# og:image / og:video URLs are usually generic or wrong (e.g. a "Google Docs"
# placeholder) rather than the actual article media. When an og URL's host
# matches one of these (case-insensitively, including subdomains), the media is
# treated as "not found" and the story falls through to the no-image path.
#
# Conservative by design: only clearly-generic placeholder services and
# Google's generic image CDN are blocked. Legitimate news image hosts (e.g.
# assets2.cbsnewsstatic.com) are NOT blocked.
_BLOCKED_IMAGE_HOSTS = frozenset({
    # Google image CDN (Google News / Docs / Blogger placeholders).
    "lh3.googleusercontent.com",
    "lh4.googleusercontent.com",
    "lh5.googleusercontent.com",
    "lh6.googleusercontent.com",
    # Any *.googleusercontent.com subdomain (Google's generic image CDN).
    "googleusercontent.com",
    # Google Docs / Drive / static.
    "docs.google.com",
    "drive.google.com",
    "ssl.gstatic.com",
    "www.gstatic.com",
    # Placeholder services.
    "placehold.co",
    "placeholder.com",
    "via.placeholder.com",
    "picsum.photos",
    "dummyimage.com",
})


def _is_blocked_media_host(url):
    """True if the URL's host is a known generic placeholder/CDN host.

    Case-insensitive on the hostname and matches subdomains: e.g.
    ``lh3.googleusercontent.com`` matches, and any ``*.googleusercontent.com``
    matches. Returns False for host-less / unparseable URLs (those are handled
    by the http/private checks elsewhere).
    """
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    for blocked in _BLOCKED_IMAGE_HOSTS:
        if host == blocked or host.endswith("." + blocked):
            return True
    return False


# ---------------------------------------------------------------------------
# URL / title normalization helpers
# ---------------------------------------------------------------------------

def normalize_url(url):
    """Strip scheme, www., query string, fragment and trailing slash; lowercase."""
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", u)
    if u.startswith("www."):
        u = u[4:]
    u = u.split("#", 1)[0]
    u = u.split("?", 1)[0]
    return u.rstrip("/")


def normalize_title(title):
    """Lowercase, strip punctuation/whitespace for title matching."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def _http_url(url):
    """Return the URL if it is http(s) with a valid host, else None.

    Rejects data:/javascript:/etc. schemes AND host-less malformed URLs such
    as "http://" (urlsplit gives an empty netloc/hostname).
    """
    if not url:
        return None
    url = url.strip()
    if not url.lower().startswith(_HTTP_SCHEMES):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    return url


def _is_private_host(url):
    """True if the URL's host is localhost/private/reserved (SSRF guard)."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return True
    host = parts.hostname
    if not host:
        return True
    return is_private_or_local_host(host.lower())


def _google_news_article_id(url):
    """Return the article id (last path segment) of a Google News redirect URL.

    Handles both the ``/rss/articles/<id>`` and ``/rss/read/<id>`` forms.
    Returns None if the URL is not a Google News redirect or has no id.
    """
    try:
        path = urllib.parse.urlparse(url).path
    except ValueError:
        return None
    for marker in ("/articles/", "/read/"):
        if marker in path:
            encoded = path.split(marker, 1)[-1]
            if encoded:
                return encoded
    return None


def _decode_google_news_offline(url):
    """Try the fast offline base64-scan decode of a Google News redirect URL.

    Older (pre-July 2024) Google News URLs embed the article URL as plaintext
    inside the base64-decoded protobuf bytes. Returns the decoded article URL
    on success, or None if the URL is not decodable offline (e.g. the newer
    encrypted ``AU_yqL`` format).
    """
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        encoded = path.split("/articles/", 1)[-1]
        if not encoded:
            return None

        # Add base64 padding if needed.
        missing = len(encoded) % 4
        if missing:
            encoded += "=" * (4 - missing)

        raw = base64.urlsafe_b64decode(encoded)
        text = raw.decode("latin-1")

        # Scan for an http(s) URL in the decoded protobuf bytes.
        idx = text.find("http")
        if idx < 0:
            return None

        # Read until null byte or protobuf field markers that typically follow
        # a length-delimited string field (\x12 = field 2 len, \x1a = field 3 len).
        rest = text[idx:]
        url_end = len(rest)
        for i, ch in enumerate(rest):
            code = ord(ch)
            if code == 0x00 or code in (0x12, 0x1A):
                url_end = i
                break

        candidate = rest[:url_end].rstrip()
        if _http_url(candidate):
            return candidate
        return None
    except Exception:
        return None


def _decode_google_news_batchexecute(url):
    """Resolve a Google News redirect URL via the ``batchexecute`` technique.

    Two HTTP requests:
      1. GET the interstitial page and scrape the ``data-n-a-sg`` (signature)
         and ``data-n-a-ts`` (timestamp) attributes.
      2. POST the ``f.req`` batchexecute payload and parse the ``garturlres``
         frame to recover the real article URL.

    Returns the decoded article URL on success, or None on ANY failure
    (network error, 429, CAPTCHA, null payload, timeout, parse error). Never
    raises.
    """
    # Throttle between batchexecute decode calls to avoid Google 429s. The
    # lock serializes concurrent calls so only one sleeps at a time.
    with _GN_DECODE_LOCK:
        time.sleep(_GN_DECODE_DELAY)

    article_id = _google_news_article_id(url)
    if not article_id:
        return None

    # Request 1: GET the interstitial page to scrape sg/ts.
    page_url = "https://news.google.com/rss/articles/%s" % article_id
    page_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=PENDING+987",
    }
    try:
        req = urllib.request.Request(page_url, headers=page_headers)
        with urllib.request.urlopen(req, timeout=_GN_DECODE_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    # Scrape the signature and timestamp (regex/string search, not a fragile
    # CSS selector — the attributes have moved around the DOM across redesigns).
    sig = None
    ts = None
    m = re.search(r'data-n-a-sg="([^"]+)"', html)
    if m:
        sig = m.group(1)
    m = re.search(r'data-n-a-ts="([^"]+)"', html)
    if m:
        ts = m.group(1)
    if not sig or not ts:
        # Consent page / CAPTCHA / redesign: no params to work with.
        return None
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return None

    # Request 2: POST to batchexecute. The inner payload is JSON-serialized
    # and embedded as a STRING inside the outer structure (double encoding).
    inner = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
          None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        article_id,
        ts_int,   # integer, NOT a quoted string
        sig,
    ])
    outer = json.dumps([[["Fbv4je", inner]]])
    body = "f.req=" + urllib.parse.quote(outer, safe="")

    post_headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://news.google.com",
        "Referer": "https://news.google.com/",
        "X-Same-Domain": "1",
        "User-Agent": USER_AGENT,
        "Cookie": "CONSENT=PENDING+987",
    }
    try:
        req = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body.encode("utf-8"),
            headers=post_headers,
        )
        with urllib.request.urlopen(req, timeout=_GN_DECODE_TIMEOUT) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    # Parse the response: strip the )]}' anti-hijacking prefix (split on the
    # first double newline), find the wrb.fr frame, JSON-decode twice.
    try:
        if "\n\n" in resp_body:
            payload_str = resp_body.split("\n\n", 1)[1]
        else:
            payload_str = resp_body
        payload = json.loads(payload_str)

        frame = None
        for f in payload:
            if isinstance(f, list) and f and f[0] == "wrb.fr":
                frame = f
                break
        if frame is None:
            return None

        data = frame[2] if len(frame) > 2 else None
        if data is None:
            # 200 but null payload: Google couldn't resolve the id.
            return None

        decoded = json.loads(data)
        if not isinstance(decoded, list) or len(decoded) < 2:
            return None
        result_url = decoded[1]
        if _http_url(result_url):
            return result_url
        return None
    except Exception:
        return None


def decode_google_news_url(url):
    """Decode a Google News redirect URL to the actual article URL.

    Google News RSS carries redirect URLs like
    ``https://news.google.com/rss/articles/<base64-encoded-protobuf>?oc=5``.
    The base64 part is a protobuf message; in older formats the article URL
    appears as plaintext inside the decoded bytes, while newer formats encrypt
    the URL (``AU_yqL``-style ids). For those, this resolves the real article
    URL via the Google ``batchexecute`` technique (two HTTP requests).

    Returns the decoded article URL on success, or the original URL on any
    failure (never crashes, never raises). Results are cached in a module-level
    dict to avoid re-decoding the same URL.
    """
    if not url or ("news.google.com/rss/articles/" not in url
                   and "news.google.com/read/" not in url):
        return url

    # Fast offline path first: base64-scan for a plaintext http URL.
    offline = _decode_google_news_offline(url)
    if offline:
        return offline

    # Cache check (only successful decodes are cached).
    with _GN_DECODE_LOCK:
        cached = _GN_DECODE_CACHE.get(url)
    if cached is not None:
        return cached

    decoded = _decode_google_news_batchexecute(url)
    if decoded:
        with _GN_DECODE_LOCK:
            _GN_DECODE_CACHE[url] = decoded
        return decoded
    return url


# ---------------------------------------------------------------------------
# Open Graph meta parsing (html.parser based — no regex for meta parsing)
# ---------------------------------------------------------------------------

class _OGParser(html.parser.HTMLParser):
    """Collect og:image / og:video meta tags from an HTML document.

    Image priority (regardless of document order):
      og:image > og:image:url > og:image:secure_url > twitter:image
    Video priority (regardless of document order):
      og:video > og:video:url > og:video:secure_url
    (plus property="og:video:type" for the type).
    """

    # Priority rank: lower number wins.
    _IMAGE_PRIORITY = {
        "og:image": 0,
        "og:image:url": 1,
        "og:image:secure_url": 2,
        "twitter:image": 3,
    }
    _VIDEO_PRIORITY = {
        "og:video": 0,
        "og:video:url": 1,
        "og:video:secure_url": 2,
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image_url = None
        self.image_rank = None
        self.video_url = None
        self.video_rank = None
        self.video_type = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attr_map = {}
        for key, value in attrs:
            if key is not None:
                attr_map[key.lower()] = value
        prop = (attr_map.get("property") or "").strip().lower()
        name = (attr_map.get("name") or "").strip().lower()
        content = (attr_map.get("content") or "").strip()

        # Image: pick the highest-priority tag seen so far.
        image_rank = self._IMAGE_PRIORITY.get(prop)
        if image_rank is None and name == "twitter:image":
            image_rank = self._IMAGE_PRIORITY["twitter:image"]
        if image_rank is not None and content:
            if self.image_rank is None or image_rank < self.image_rank:
                self.image_rank = image_rank
                self.image_url = content

        # Video: pick the highest-priority tag seen so far.
        video_rank = self._VIDEO_PRIORITY.get(prop)
        if video_rank is not None and content:
            if self.video_rank is None or video_rank < self.video_rank:
                self.video_rank = video_rank
                self.video_url = content
        elif prop == "og:video:type" and self.video_type is None:
            if content:
                self.video_type = content


def parse_og_meta(html, base_url):
    """Parse og:image / og:video meta tags from an HTML string.

    Args:
        html: the raw HTML document (str).
        base_url: the page's final URL, used to resolve relative and
            protocol-relative URLs.

    Returns:
        dict with keys "image" (dict or None) and "video" (dict or None).
        Each media dict has "url", optional "type", and "source": "og".
        Non-http(s) URLs (data:, javascript:, etc.) are dropped.
    """
    parser = _OGParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # Malformed HTML must never crash the caller.
        return {"image": None, "video": None}

    result = {"image": None, "video": None}

    if parser.image_url:
        resolved = urllib.parse.urljoin(base_url or "", parser.image_url)
        resolved = decode_google_news_url(resolved)
        if (_http_url(resolved) and not _is_private_host(resolved)
                and not _is_blocked_media_host(resolved)):
            result["image"] = {"url": resolved, "source": "og"}

    if parser.video_url:
        resolved = urllib.parse.urljoin(base_url or "", parser.video_url)
        resolved = decode_google_news_url(resolved)
        if (_http_url(resolved) and not _is_private_host(resolved)
                and not _is_blocked_media_host(resolved)):
            video = {"url": resolved, "source": "og"}
            if parser.video_type:
                video["type"] = parser.video_type
            result["video"] = video

    return result


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_page(url):
    """Fetch a URL and return (final_url, html) or raise on any failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        final_url = resp.geturl() or url
        raw = resp.read(MAX_BYTES)
    # Decode using the declared charset if present, else UTF-8 with fallback.
    charset = resp.headers.get_content_charset() or "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except (LookupError, ValueError):
        text = raw.decode("utf-8", errors="replace")
    return final_url, text


# ---------------------------------------------------------------------------
# Candidate matching
# ---------------------------------------------------------------------------

def find_candidate(story, candidates):
    """Return the best-matching candidate for a story, or None.

    Match priority:
      (a) exact source_url match,
      (b) normalized URL match,
      (c) normalized title match (SequenceMatcher ratio >= 0.85).
    """
    source_url = (story.get("source_url") or "").strip()
    story_title = normalize_title(story.get("title"))

    # (a) exact source_url match.
    if source_url:
        for c in candidates:
            if (c.get("url") or "").strip() == source_url:
                return c

    # (b) normalized URL match.
    src_norm = normalize_url(source_url)
    if src_norm:
        for c in candidates:
            if normalize_url(c.get("url")) == src_norm:
                return c

    # (c) normalized title match (ratio >= 0.85).
    if story_title:
        best = None
        best_ratio = 0.0
        for c in candidates:
            cand_title = normalize_title(c.get("title"))
            if not cand_title:
                continue
            ratio = difflib.SequenceMatcher(None, story_title, cand_title).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = c
        if best is not None and best_ratio >= 0.85:
            return best

    return None


# ---------------------------------------------------------------------------
# Per-story enrichment (best-effort; never raises)
# ---------------------------------------------------------------------------

def enrich_story(story, candidates):
    """Attempt to attach og media to a single story. Never raises.

    Returns True if the story gained an image or video, else False.
    """
    # Only stories with NO image and NO video are candidates for enrichment.
    if story.get("image") or story.get("video"):
        return False

    candidate = find_candidate(story, candidates)
    if candidate is not None:
        fetch_url = (candidate.get("url") or "").strip()
    else:
        fetch_url = (story.get("source_url") or "").strip()

    # Decode Google News redirect URLs to the actual article URL.
    decoded_url = decode_google_news_url(fetch_url)
    if decoded_url != fetch_url:
        # Update story source_url so downstream (ntfy, render_digest) uses
        # the actual article URL instead of the Google News redirect.
        story["source_url"] = decoded_url
        fetch_url = decoded_url
        # Also decode the candidate URL for matching.
        if candidate is not None:
            cand_decoded = decode_google_news_url(
                (candidate.get("url") or "").strip())
            if cand_decoded != (candidate.get("url") or "").strip():
                candidate = dict(candidate, url=cand_decoded)

    # Reject non-http(s) AND private/local-host URLs before fetching (SSRF).
    if not _http_url(fetch_url) or _is_private_host(fetch_url):
        return False

    try:
        final_url, html = fetch_page(fetch_url)
    except Exception:
        return False

    # urllib follows redirects; re-validate the resolved final URL.
    if _is_private_host(final_url):
        return False

    og = parse_og_meta(html, final_url)

    changed = False
    if og.get("image"):
        story["image"] = og["image"]
        changed = True
    if og.get("video"):
        story["video"] = og["video"]
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_stories(path):
    """Load stories from a JSON file.

    Accepts either a list of story dicts or {"stories": [...]}. Returns
    (stories_list, is_wrapped) where is_wrapped is True for the dict form.
    Raises on unreadable/unparseable input.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, False
    if isinstance(data, dict) and isinstance(data.get("stories"), list):
        return data["stories"], True
    raise ValueError("stories JSON must be a list or {\"stories\": [...]}")


def load_candidates(path):
    """Load candidates from a JSON file.

    Accepts either a list of candidate dicts or a dict with a "candidates"
    list. Returns a list of candidate dicts (empty on any problem).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict) and isinstance(data.get("candidates"), list):
        return [c for c in data["candidates"] if isinstance(c, dict)]
    return []


def save_stories(path, stories, is_wrapped, original_data):
    """Write stories back atomically (temp file + os.replace).

    Preserves the original structure: if the input was {"stories": [...]},
    the wrapper dict is preserved (with its other keys) and only the
    "stories" list is replaced.
    """
    if is_wrapped:
        original_data["stories"] = stories
        payload = original_data
    else:
        payload = stories

    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Post-agent media enrichment: attach og:image/og:video "
                    "to stories that lack media.")
    ap.add_argument("--stories-json", dest="stories_path",
                    default=DEFAULT_STORIES)
    ap.add_argument("--candidates-json", dest="candidates_path",
                    default=DEFAULT_CANDIDATES)
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Load stories. A load failure must be LOUD: a corrupt or unreadable
    # stories file would otherwise stop all enrichment forever, looking
    # exactly like "nothing to enrich". Exit non-zero; only a genuinely
    # empty list is the benign exit-0 case.
    try:
        stories, is_wrapped = load_stories(args.stories_path)
        original_data = None
        if is_wrapped:
            with open(args.stories_path, "r", encoding="utf-8") as f:
                original_data = json.load(f)
    except Exception as exc:
        print("enrich_media: could not read %s (%s)"
              % (args.stories_path, exc))
        return 1

    candidates = load_candidates(args.candidates_path)

    enriched = 0
    failed = 0

    # Enrich only stories lacking both image and video.
    todo = [s for s in stories if isinstance(s, dict)
            and not s.get("image") and not s.get("video")]

    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(
                lambda s: enrich_story(s, candidates), todo))
        enriched = sum(1 for r in results if r)
        failed = len(todo) - enriched

    # Persist atomically, preserving structure.
    try:
        save_stories(args.stories_path, stories, is_wrapped, original_data)
    except Exception as exc:
        # Best-effort: never fail the workflow over persistence, but never
        # swallow it either -- the fetched media is lost after the network cost.
        print("::warning::enrich_media: could not write %s (%s); %d stories "
              "(%d enriched) NOT persisted"
              % (args.stories_path, exc, len(stories), enriched))

    print("enrich_media: %d stories, %d enriched, %d failed"
          % (len(stories), enriched, failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
