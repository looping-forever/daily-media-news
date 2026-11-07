"""article_text.py -- a bounded article fetch for stories with no publisher text.

WHY THIS EXISTS. Run 42376820992 skipped 12 of 27 stories with "no publisher text
to summarise", and those shipped as headline-only cards -- the weakest thing the
digest publishes. The fix is to read the article itself, not to invent a sentence:
`summarise_stories` asks the model to write from this text, and every acceptance
gate still applies to the answer.

Rules, each because of a way a page can hurt the run:

  * robots.txt is checked FIRST and a disallowed path is never requested. RFC
    9309: a 4xx answer means no rules were published, so it permits the fetch; a
    5xx or an unreachable robots.txt is refused, because an unreadable permission
    is not a yes.
  * one request carries a 5s timeout, an honest User-Agent, and a 512KB cap, so a
    hostile page can neither hang the job nor flood it.
  * scripts, styles, navs, headers, footers and tags are dropped, and only the
    longest contiguous run of prose paragraphs is kept, so a cookie banner is not
    mistaken for the article.
  * anything that is not a 200 of text, or that is under 200 characters, is None.
    A caller never has to guess whether the answer is usable.
"""

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

#: The same honest identity `llm_providers` sends: who is calling, and where to
#: complain. A browser string is never acceptable and a library default is a
#: known bot-management trigger.
USER_AGENT = ("dmnews-digest/1.0 "
              "(+https://github.com/looping-forever/daily-media-news)")

TIMEOUT_SECONDS = 5
MAX_BYTES = 512 * 1024
ROBOTS_MAX_BYTES = 64 * 1024
MIN_TEXT_CHARS = 200
MIN_PARAGRAPH_CHARS = 40
MAX_TEXT_CHARS = 4000

TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "text/xhtml")

#: Never prose: skipped whole, including everything inside them.
SKIP_TAGS = frozenset((
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "nav", "header", "footer", "aside", "form", "button", "select", "figure",
))

#: A paragraph boundary. Text either side is a different block.
BLOCK_TAGS = frozenset((
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "section", "article",
    "main", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
))

_WHITESPACE_RE = re.compile(r"\s+")


def _collapse(text):
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


class _ParagraphParser(HTMLParser):
    """HTML in, prose paragraphs out. Tags, scripts and chrome never survive."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._buffer = []
        self.paragraphs = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        # A self-closing skip tag must not leave the counter raised: `<script />`
        # would otherwise silence the rest of the page.
        if tag in SKIP_TAGS:
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self._skip:
                self._skip -= 1
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buffer.append(data)

    def _flush(self):
        text = _collapse("".join(self._buffer))
        self._buffer = []
        if text:
            self.paragraphs.append(text)

    def close(self):
        super().close()
        self._flush()


def _longest_block(paragraphs):
    """The longest run of prose paragraphs, as one string.

    A short block (a label, a cookie line, a byline) ENDS a run instead of being
    joined to it, so the banner above the article cannot ride along with the prose.
    """
    runs = []
    run = []
    for paragraph in paragraphs:
        if len(paragraph) < MIN_PARAGRAPH_CHARS:
            if run:
                runs.append(run)
            run = []
            continue
        run.append(paragraph)
    if run:
        runs.append(run)
    if not runs:
        return ""
    best = max(runs, key=lambda rows: sum(len(row) for row in rows))
    return "\n\n".join(best)[:MAX_TEXT_CHARS]


def clean_html(html):
    """Plain article text from a page, or "" when there is no prose in it."""
    parser = _ParagraphParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:      # a malformed page is None, never a crash
        return ""
    return _longest_block(parser.paragraphs)


def open_page(url, timeout, max_bytes):
    """(status, content type, body) for one GET. Raises like urllib does."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", None) or response.getcode()
        content_type = response.headers.get("Content-Type", "")
        body = response.read(max_bytes + 1)
        return int(status or 0), str(content_type), body


def robots_url_for(url):
    """The robots.txt that governs *url*, or "" when the scheme has none."""
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return "%s://%s/robots.txt" % (parts.scheme, parts.netloc)


def robots_allows(url, open_page=open_page, timeout=TIMEOUT_SECONDS):
    """True unless a published robots.txt disallows this path.

    RFC 9309: "unavailable" (4xx) means no rules were published, so the fetch is
    allowed; 5xx or an unreachable robots.txt is refused, and so is a file that
    disallows the path. This is the ONE place that decision is made.
    """
    robots = robots_url_for(url)
    if not robots:
        return False
    try:
        status, _content_type, body = open_page(
            robots, timeout, ROBOTS_MAX_BYTES)
    except urllib.error.HTTPError as error:   # a real answer that was not a 200
        return 400 <= int(getattr(error, "code", 0) or 0) < 500
    except Exception:
        return False
    status = int(status or 0)
    if 400 <= status < 500:
        return True
    if status != 200:
        return False
    try:
        parser = RobotFileParser()
        parser.parse(body.decode("utf-8", "replace").splitlines())
        return bool(parser.can_fetch(USER_AGENT, str(url)))
    except Exception:
        return False


def fetch_with_reason(url, timeout=TIMEOUT_SECONDS, open_page=open_page):
    """(text|None, reason) -- `robots`, `http`, `type`, `empty` or `error`."""
    if not robots_allows(url, open_page=open_page, timeout=timeout):
        return None, "robots"
    try:
        status, content_type, body = open_page(url, timeout, MAX_BYTES)
    except urllib.error.HTTPError as error:   # a real answer that was not a 200
        return None, "http"
    except Exception:
        return None, "error"
    if int(status) != 200:
        return None, "http"
    if not str(content_type or "").lower().startswith(TEXT_TYPES):
        return None, "type"
    try:
        html = body.decode("utf-8", "replace")
    except Exception:
        return None, "error"
    text = clean_html(html)
    if len(text) < MIN_TEXT_CHARS:
        return None, "empty"
    return text, "ok"


def fetch_article_text(url, timeout=TIMEOUT_SECONDS, open_page=open_page):
    """The article's own text for *url*, or None. Never raises."""
    try:
        return fetch_with_reason(url, timeout=timeout, open_page=open_page)[0]
    except Exception:
        return None
