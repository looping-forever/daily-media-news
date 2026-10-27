"""Workflow-YAML parsing and shell-lint helpers for tests/workflows/.

WHY THIS EXISTS
---------------
425 Python tests passed on 2026-09-27 while all four production workflows
were dead, for two reasons nothing in this repo checked:

  * Bug A (stream drift) -- a gate piped `opencode run --help` into
    `grep -q` on stdout, but the CLI had moved `--help` to stderr, so a
    working `--auto` flag read as missing and the digest died at its
    pre-spend gate.
  * Bug B (YAML indentation) -- a `python3 - <<'PY'` heredoc had its body
    and `PY` terminator indented 2 columns inside a block scalar, so bash
    never saw a column-0 terminator and the step died with
    `here-document at line N delimited by end-of-file`.

Both bugs live in shell embedded in YAML, so they are invisible to any
test that only imports Python.  The checks live in
tests/workflows/test_workflow_lint.py; this module only supplies parsing
and process plumbing.

PARSER SELECTION
----------------
`yaml.safe_load` (PyYAML) is the reference parser and is used whenever it
is importable.  Because a stdlib-only suite must also run on a bare
interpreter, `load_workflow_text` falls back to `_MiniYAML`, a strict
loader for the YAML subset these five workflows use: block mappings,
block sequences, literal block scalars and flow collections.  The
fallback implements YAML's own block-scalar auto-detection (the body's
indentation is measured from the first non-blank body line, and must be
greater than the parent node's indentation) rather than stripping a
hardcoded number of spaces.  That distinction is the whole point:
tests/academic/test_exam_notices.py strips with a hardcoded
`line.startswith(" " * 10)` and is therefore structurally blind to the
exact bug class this suite guards.  The fallback loader's correctness is
pinned by committed golden cases in the lint module, and is additionally
cross-checked against PyYAML whenever PyYAML is importable.

Everything here is stdlib, offline and read-only: no network, no GitHub
API, no Cloudflare, no writes outside tempfile directories.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
STUB_OPENCODE = FIXTURE_DIR / "stub_opencode.sh"

#: Every workflow under test.  `daily-news.yml` first because it holds the
#: opencode gate that Bug A killed, and Bug A is the higher-severity of the
#: two outages (it killed the digest at its pre-spend gate).
#:
#: U6 ADDED `digest-prebuild.yml` HERE, AND THE ENTRY IS THE POINT. This tuple
#: is hardcoded, so a new workflow that nobody adds is a workflow that every
#: check below silently SKIPS -- no lint, no `bash -n`, no heredoc check, no
#: secret scan. A green run of this suite then means "the five workflows I was
#: told about are fine", which reads exactly like "the workflows are fine".
#: Registration is therefore part of adding a workflow, in the same change.
WORKFLOW_NAMES = (
    "daily-news.yml",
    "breaking-monitor.yml",
    "academic-result.yml",
    "digest-watchdog.yml",
    "drift-canary.yml",
    "digest-prebuild.yml",
    # Added 2026-10-19 with the file itself. A workflow that spends minutes to
    # guard minutes still spends minutes, so it is registered, budgeted in
    # config/actions-budget.json, and carries a CRON_NPT_TABLE row.
    "budget-sentinel.yml",
    # Added 2026-10-27 with the file itself. No schedule and no cron, so it
    # contributes nothing to the cron table -- but it still runs a job on a
    # runner, so it carries a budget entry. It is registered here for the
    # reason this tuple exists at all: an unregistered workflow is one that
    # every check below silently skips, and a green suite then means "the
    # workflows I was told about are fine".
    "delivery-selftest.yml",
)

try:  # pragma: no cover - environment dependent, both branches are exercised
    import yaml as _pyyaml
except ImportError:  # pragma: no cover
    _pyyaml = None

PARSER_NAME = "PyYAML %s" % _pyyaml.__version__ if _pyyaml is not None else "stdlib _MiniYAML"


def parser_name() -> str:
    """Name of the parser actually in use (recorded in test output)."""
    return PARSER_NAME


# --------------------------------------------------------------------------
# MiniYAML: strict loader for the subset these workflows use
# --------------------------------------------------------------------------


class MiniYAMLError(ValueError):
    """Raised when a construct outside the supported subset is met.

    Failing loudly is deliberate.  A loader that silently mis-parses a
    workflow would hand the lint wrong `run:` strings and let a real
    syntax error through -- the exact class of failure this suite exists
    to stop.
    """


_BLOCK_HEADER_RE = re.compile(r"^([|>])([+-]?)(\d?)([+-]?)\s*$")

#: A mapping entry is `<key>: <value>` or `<key>:` where the key is a plain
#: identifier.  Restricting the key shape is what lets a sequence item be
#: told apart from a nested mapping: `- morning` is a scalar, `- cron: 'x'`
#: is a mapping, and neither test depends on a colon being absent.
_MAPPING_ENTRY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*):(\s.*|)$")


class _MiniYAML:
    """Indentation-driven parser for a documented YAML subset.

    Supported: block mappings, block sequences of scalars/mappings, literal
    (`|`) and folded (`>`) block scalars with auto-detected or explicit
    indentation and `-`/`+` chomping, flow sequences and flow mappings,
    single/double-quoted scalars, plain scalars, and full-line comments.

    Deliberately unsupported (raises MiniYAMLError rather than guessing):
    anchors, aliases, tags, multiple documents, merge keys, and block
    scalars used as sequence items (`- |`).
    """

    def __init__(self, text: str) -> None:
        # splitlines() keeps fidelity for indentation; join defensively so a
        # CRLF file cannot smuggle a stray \r into every scalar.
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # Clip chomping keeps the final line break only when the source
        # actually had one.  A workflow whose last line is a `PY` terminator
        # with no trailing newline is a real case in this repo, and PyYAML
        # (the reference parser) returns no trailing "\n" for it.
        self._trailing_newline = text.endswith("\n") or text.endswith("\r")

    # -- low level helpers -------------------------------------------------

    @staticmethod
    def _indent_of(raw: str) -> int:
        return len(raw) - len(raw.lstrip(" "))

    @staticmethod
    def _is_blank(raw: str) -> bool:
        return raw.strip() == ""

    @classmethod
    def _skip(cls, raw: str) -> bool:
        # A `#` at the start of a (possibly indented) line is a YAML comment
        # and carries no data.  A `#` INSIDE a block scalar is handled by the
        # block-scalar reader, which never routes lines through here.
        return cls._is_blank(raw) or raw.lstrip(" ").startswith("#")

    def _next_significant(self, start: int):
        i = start
        while i < len(self.lines) and self._skip(self.lines[i]):
            i += 1
        return i

    # -- block scalars -----------------------------------------------------

    def _read_block_scalar(self, header: str, parent_indent: int, i: int):
        m = _BLOCK_HEADER_RE.match(header)
        if not m:
            raise MiniYAMLError("unsupported block scalar header %r" % header)
        style, chomp_a, explicit, chomp_b = m.groups()
        chomp = chomp_a or chomp_b or ""
        if style == ">":
            raise MiniYAMLError(
                "folded block scalar ('>') is outside the supported subset; "
                "convert it to a literal block ('|') or extend the loader"
            )

        # Collect every line that belongs to the body: blank lines, or lines
        # indented deeper than the parent node.  This is YAML's rule, and it
        # is why the loader is immune to a hardcoded strip width.
        body_raw: list[str] = []
        j = i
        while j < len(self.lines):
            raw = self.lines[j]
            if self._is_blank(raw):
                body_raw.append(raw)
                j += 1
                continue
            if self._indent_of(raw) <= parent_indent:
                break
            body_raw.append(raw)
            j += 1

        while body_raw and self._is_blank(body_raw[-1]):
            body_raw.pop()
            j -= 1

        if explicit:
            body_indent = parent_indent + int(explicit)
        else:
            non_blank = [ln for ln in body_raw if not self._is_blank(ln)]
            if not non_blank:
                return "", j
            body_indent = self._indent_of(non_blank[0])

        lines_out = []
        for raw in body_raw:
            lines_out.append(raw[body_indent:] if len(raw) > body_indent else "")
        text = "\n".join(lines_out)

        if chomp == "-":
            return text, j
        if chomp == "+":
            return text + "\n", j
        # clip (default): collapse every trailing line break into one, and
        # emit it only if the source had a line break after the last body
        # line.  Matching this detail is what keeps the fallback loader
        # byte-identical to PyYAML, so the parity check has no noise.
        text = text.rstrip("\n")
        if not text:
            return "", j
        if not self._trailing_newline and body_raw and body_raw[-1] is self.lines[-1]:
            return text, j
        return text + "\n", j

    # -- scalars -----------------------------------------------------------

    def _parse_flow(self, raw: str):
        assert raw[0] in "[{"
        closer = "]" if raw[0] == "[" else "}"
        inner = raw[1:-1] if raw.rstrip().endswith(closer) else raw[1:]
        items = self._split_top(inner)
        if raw[0] == "[":
            return [self._parse_flow_item(it) for it in items]
        out = {}
        for it in items:
            key, _sep, val = it.partition(":")
            out[self._plain(key.strip())] = self._parse_flow_item(val.strip())
        return out

    @staticmethod
    def _split_top(inner: str) -> list[str]:
        parts, buf, depth, quote = [], [], 0, None
        for ch in inner:
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in "'\"":
                quote = ch
                buf.append(ch)
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
                continue
            buf.append(ch)
        if "".join(buf).strip():
            parts.append("".join(buf))
        return [p.strip() for p in parts if p.strip()]

    def _parse_flow_item(self, raw: str):
        raw = raw.strip()
        if raw[:1] in "[{":
            return self._parse_flow(raw)
        if raw[:1] == "'" and raw[-1:] == "'" and len(raw) >= 2:
            return raw[1:-1].replace("''", "'")
        if raw[:1] == '"' and raw[-1:] == '"' and len(raw) >= 2:
            return _unescape_double(raw[1:-1])
        return _strip_comment(raw)

    @staticmethod
    def _plain(raw: str) -> str:
        raw = _strip_comment(raw)
        if raw[:1] == "'" and raw[-1:] == "'" and len(raw) >= 2:
            return raw[1:-1].replace("''", "'")
        if raw[:1] == '"' and raw[-1:] == '"' and len(raw) >= 2:
            return _unescape_double(raw[1:-1])
        return raw.strip()

    # -- structure ---------------------------------------------------------

    def parse(self):
        i = self._next_significant(0)
        if i >= len(self.lines):
            return {}
        value, _i = self._parse_block(self._indent_of(self.lines[i]), i)
        return value

    def _parse_block(self, indent: int, i: int):
        """Parse one block at `indent`, returning (value, next_line_index)."""
        i = self._next_significant(i)
        if i >= len(self.lines):
            return None, i
        raw = self.lines[i]
        text = raw.strip()
        if text == "-" or text.startswith("- "):
            return self._parse_seq(indent, i)
        if not _MAPPING_ENTRY_RE.match(text):
            # A block that is a single scalar (e.g. a sequence item rewritten
            # in place, or a bare document).
            return self._plain(text), i + 1
        return self._parse_map(indent, i)

    def _parse_map(self, indent: int, i: int):
        out: dict = {}
        while True:
            i = self._next_significant(i)
            if i >= len(self.lines):
                break
            raw = self.lines[i]
            cur = self._indent_of(raw)
            if cur < indent:
                break
            if cur > indent:
                raise MiniYAMLError(
                    "line %d: unexpected indent %d (expected %d): %r"
                    % (i + 1, cur, indent, raw.strip()[:80])
                )
            text = raw.strip()
            if text.startswith("- "):
                raise MiniYAMLError("line %d: sequence item inside a mapping" % (i + 1))
            m = _MAPPING_ENTRY_RE.match(text)
            if not m:
                raise MiniYAMLError("line %d: not a mapping entry: %r" % (i + 1, text[:80]))
            key = m.group(1).strip()
            rest = text[len(key) + 1:].strip()
            if key.startswith("&") or key.startswith("*") or key.startswith("!"):
                raise MiniYAMLError("line %d: anchors/aliases/tags unsupported" % (i + 1))
            if key in out:
                raise MiniYAMLError("line %d: duplicate key %r" % (i + 1, key))

            if rest == "":
                nxt = self._next_significant(i + 1)
                if nxt < len(self.lines) and self._indent_of(self.lines[nxt]) > indent:
                    value, i = self._parse_block(self._indent_of(self.lines[nxt]), nxt)
                else:
                    value, i = None, i + 1
            elif rest[0] in "|>":
                value, i = self._read_block_scalar(rest, indent, i + 1)
            else:
                if rest[0] in "[{":
                    value = self._parse_flow(rest)
                else:
                    value = self._plain(rest)
                i += 1
            out[key] = value
        return out, i

    def _parse_seq(self, indent: int, i: int):
        out: list = []
        while True:
            i = self._next_significant(i)
            if i >= len(self.lines):
                break
            raw = self.lines[i]
            cur = self._indent_of(raw)
            if cur < indent:
                break
            if cur > indent:
                raise MiniYAMLError("line %d: unexpected indent in sequence" % (i + 1))
            text = raw.strip()
            if not (text == "-" or text.startswith("- ")):
                break
            if text == "-":
                nxt = self._next_significant(i + 1)
                if nxt < len(self.lines) and self._indent_of(self.lines[nxt]) > indent:
                    value, i = self._parse_block(self._indent_of(self.lines[nxt]), nxt)
                else:
                    value, i = None, i + 1
                out.append(value)
                continue
            # `- rest...`: rewrite the line so `rest` starts at its true
            # column, then parse it as a nested block.  Standard YAML puts
            # the item's sibling keys at exactly that column.
            rest = text[2:]
            lead = len(rest) - len(rest.lstrip(" "))
            col = indent + 2 + lead
            if rest.strip()[:1] in "|>":
                raise MiniYAMLError(
                    "line %d: block scalar as a sequence item ('- |') is "
                    "outside the supported subset" % (i + 1)
                )
            self.lines[i] = " " * col + rest.strip()
            value, i = self._parse_block(col, i)
            out.append(value)
        return out, i


def _strip_comment(raw: str) -> str:
    """Remove a trailing YAML comment from a plain scalar.

    A `#` only starts a comment when it follows whitespace, so `--foo#bar`
    and URL fragments survive.  Quote-aware so a `#` inside `'...'` does not.
    """
    quote = None
    for idx, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch == "#" and idx > 0 and raw[idx - 1] in " \t":
            return raw[:idx].rstrip()
    return raw.rstrip()


def _unescape_double(raw: str) -> str:
    out, i = [], 0
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0"}
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(simple.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Public parsing helpers
# --------------------------------------------------------------------------


def load_workflow_text(text: str, *, prefer_pyyaml: bool = True):
    """Parse one workflow document into plain dicts/lists/strings.

    Uses PyYAML when available, else the stdlib subset loader.  Both
    return the same `str` values for every scalar, so callers never have to
    care which one ran (only the cross-check test does).
    """
    if prefer_pyyaml and _pyyaml is not None:
        doc = _pyyaml.safe_load(text)
        return _coerce_scalars(doc)
    return _coerce_scalars(_MiniYAML(text).parse())


def _coerce_scalars(node):
    """Normalise PyYAML output to the loader's string-valued shape.

    PyYAML applies YAML 1.1 typing, so `on:` becomes the boolean key `True`,
    `true` becomes `True` and `10` becomes `10`.  The lint only ever needs
    strings, and the cross-check compares parser to parser, so normalise
    both sides to `str` and leave every structural difference intact.
    """
    if isinstance(node, dict):
        return {_key_to_str(k): _coerce_scalars(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_coerce_scalars(v) for v in node]
    if node is None:
        return ""
    if isinstance(node, bool):
        return "true" if node else "false"
    return node if isinstance(node, str) else str(node)


def _key_to_str(key) -> str:
    # YAML 1.1 turns the bare key `on` into boolean True.
    if key is True:
        return "on"
    if key is False:
        return "off"
    return key if isinstance(key, str) else str(key)


def load_workflow(name: str):
    """Parse a workflow by file name from .github/workflows/."""
    path = WORKFLOW_DIR / name
    if not path.is_file():
        raise FileNotFoundError("workflow not found: %s" % path)
    return load_workflow_text(path.read_text(encoding="utf-8"))


def iter_run_blocks(name: str):
    """Yield (job_id, step_name, run_text, step_dict) for every `run:` step.

    Walks the parsed document rather than the raw text so the shell handed
    to `bash -n` is exactly what GitHub Actions would execute, with YAML
    block-scalar indentation already resolved by the parser.
    """
    doc = load_workflow(name)
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        raise MiniYAMLError("%s: `jobs` is not a mapping" % name)
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in (job.get("steps") or []):
            if not isinstance(step, dict) or "run" not in step:
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            yield job_id, str(step.get("name") or "<unnamed step>"), run, step


def iter_all_steps(name: str):
    """Yield (job_id, step_name, step_dict) for every step, `run` or not."""
    doc = load_workflow(name)
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in (job.get("steps") or []):
            if isinstance(step, dict):
                yield job_id, str(step.get("name") or "<unnamed step>"), step


def iter_schedule_crons(name: str):
    """Yield each `on.schedule[].cron` expression in a workflow."""
    doc = load_workflow(name)
    on = doc.get("on")
    if not isinstance(on, dict):
        return
    for entry in (on.get("schedule") or []):
        if isinstance(entry, dict) and entry.get("cron"):
            yield str(entry["cron"]).strip()


def raw_workflow_text(name: str) -> str:
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Process plumbing
# --------------------------------------------------------------------------


def bash_executable():
    """Locate bash, or return None.

    None is never treated as "skip quietly" by the tests: every caller
    decides explicitly, and the decision is stated in the failure message.
    """
    from shutil import which

    for candidate in ("bash", "/bin/bash", r"C:\Program Files\Git\bin\bash.exe"):
        found = which(candidate) if os.sep not in candidate else (
            candidate if os.path.isfile(candidate) else None
        )
        if found:
            return found
    return None


def bash_syntax_check(script: str, bash: str) -> subprocess.CompletedProcess:
    """`bash -n` on a script supplied over stdin.

    stdin rather than a temp file: a Windows temp path is not reliably
    acceptable to a Cygwin/MSYS bash, and stdin has no path translation at
    all.  `bash -n` with no script argument reads the program from stdin
    and only parses it -- nothing executes, so this is offline and inert.
    """
    return subprocess.run(
        [bash, "-n"],
        input=script,
        capture_output=True,
        # Explicit UTF-8 in BOTH directions.  These scripts contain emoji and
        # an em dash; on a cp1252 console the default text mode raises
        # UnicodeEncodeError while writing stdin, which would turn a
        # perfectly good workflow into a bogus test failure.
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def run_bash_script(script: str, env_overrides=None, cwd=None, bash=None, timeout=120):
    """Execute a bash script from a FILE, optionally rewriting env/PATH.

    THE SCRIPT GOES IN A FILE, NOT IN `-c`, AND THAT IS THE WHOLE POINT OF THIS
    HELPER NOW. Windows hands a process one command-line string, and this
    platform's bash truncates a long `-c` argument once the environment block is
    large as well. A step whose `run:` body passed ~8 KB while carrying a dozen
    env vars was silently cut in half and died with:

        bash: -c: line 179: syntax error: unexpected end of file from `case'
        command on line 176

    which reads exactly like a broken workflow and is in fact a broken
    invocation. Measured against daily-news.yml's date step (8808-char body,
    5858-char env): `bash -c` truncated it at ~8247 characters, while the
    byte-identical script run from a file exited 0. A file has no such ceiling,
    and the script text, the env, the cwd and the timeout are all unchanged --
    this is a fixed invocation, not a weakened check. Every caller of this
    helper inherits the fix, which is why it lives here rather than in the one
    suite that happened to hit the limit first.
    """
    bash = bash or bash_executable()
    env = dict(os.environ)
    if env_overrides:
        env.update({k: str(v) for k, v in env_overrides.items()})
    fd, path = tempfile.mkstemp(prefix="run-bash-", suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        return subprocess.run(
            [bash, path],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@contextlib.contextmanager
def frozen_datetime(instant_iso: str):
    """Pin `datetime.datetime.now()` for `exec`'d code to one instant.

    A freezegun in a dozen lines, and deliberately not a dependency: this
    repository's suites are stdlib-only by construction, and the instant a test
    runs must never decide its result.

    WHY IT EXISTS. The defects guarded by it are all about what a job does when
    it starts hours late -- after the Nepal midnight that follows its own
    scheduled firing, which is the case that mislabelled the archive on
    2026-10-10. A test that can only ask "what is the date right now" cannot
    ask that question at all, and would go green or red depending on the hour
    it was run.

    Only `datetime.datetime` is substituted; every other name in the module --
    `timedelta`, `timezone`, `date` -- is the real one, so the code under test
    exercises its own arithmetic rather than a proxy of it.
    """
    real = sys.modules["datetime"]
    pinned = types.ModuleType("datetime")
    for attr in dir(real):
        setattr(pinned, attr, getattr(real, attr))
    instant = real.datetime.fromisoformat(instant_iso)

    class _Frozen(real.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    pinned.datetime = _Frozen
    sys.modules["datetime"] = pinned
    try:
        yield instant
    finally:
        sys.modules["datetime"] = real


def install_stub_on_path(bin_dir, script_body: str, name: str = "opencode") -> Path:
    """Write an executable stub script into `bin_dir` and return its path.

    Written with LF endings and mode 0o755: a CRLF stub would leave a stray
    `\\r` on the shebang line, and a non-executable file would turn every
    "gate must pass" case into "command not found" -- a green test that
    proves nothing.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / name
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(script_body)
    os.chmod(target, 0o755)
    return target
