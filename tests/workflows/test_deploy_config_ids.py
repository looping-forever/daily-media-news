"""The committed deploy config names no real KV namespace id. In either tree.

WHY THIS FILE EXISTS
--------------------
`deploy/wrangler.toml` is the config `wrangler deploy` binds, and until 2026-10-26
the PRIVATE working copy carried the real 32-hex namespace id on the `id` line
while the published copy carried the placeholder. That is a credential-shaped
fact sitting in a tracked file, one `git add` away from a repository -- and the
real id had also been copied into five digest test fixtures, so "fix the one
file" would have left five more copies behind.

The fix moved the id out of the tree: the committed template keeps a placeholder,
and `scripts/deploy-worker.ps1` injects the real id from $env:DMNEWS_KV_NAMESPACE_ID
at deploy time. That only stays true if something FAILS when a real id is pasted
back in, which is this file.

HOW IT DECIDES, WITHOUT KNOWING THE REAL ID
-------------------------------------------
It cannot know the real value -- that is the point -- so it does not compare
against one. It asserts the only permitted value: every declaration of a KV
namespace id in the tree must be the placeholder. A real id is, by definition,
32 hex that is not the placeholder, so:
  * pasting the real id in and removing the placeholder -> one non-placeholder id;
  * pasting it in beside the placeholder                -> one non-placeholder id;
and both fail here. The placeholder is published already, so naming it is safe.

The rule is anchored on the DECLARATION SHAPES rather than "any 32-hex run in the
tree": unrelated 32-hex values exist in fixtures and access-control tests, and a
blanket rule would fire on them until someone deleted the rule.

WHAT IT CANNOT DO
-----------------
It reads the two trees it is pointed at. On CI only one tree exists, so the
cross-tree arm reports a skip there -- stated, not silent. It also cannot tell a
placeholder from a real id that HAPPENS to be the placeholder, which would be a
Cloudflare namespace nobody can be using.
"""

import os
import re
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The published placeholder. Safe to name: it is in the committed config and in
#: the published repository. 32 hex on purpose, so the API accepts the shape and
#: rejects the namespace (`code: 10041 KV namespace not found`) instead of
#: silently binding something else.
PLACEHOLDER = "abcdefabcdefabcdefabcdefabcdefab"

WRANGLER_TOML_REL = "deploy/wrangler.toml"
RENDER_SCRIPT_REL = "scripts/deploy-worker.ps1"
RENDERED_CONFIG_REL = "deploy/wrangler.local.toml"

#: `[[kv_namespaces]] ... id = "<32 hex>"`, up to the next table header. `[^[]`
#: rather than a lazy `.*?` so a `[` inside a comment cannot run the block on.
KV_BLOCK_RE = re.compile(r"\[\[kv_namespaces\]\]([^\[]*)", re.S)
KV_ID_RE = re.compile(r'\bid\s*=\s*"([0-9a-fA-F]{32})"')

#: The test-fixture shape: a module constant holding a synthetic namespace id.
#: Widened to JS/PowerShell assignment and to single quotes, because the same
#: five fixtures are the obvious place the next copy would land.
CONST_ID_RE = re.compile(
    r"\b(?:KV_)?NAMESPACE_ID\b\s*[=:]\s*[\"']([0-9a-fA-F]{32})[\"']")

#: Trees to check beyond the one this file lives in. Set
#: DMNEWS_SIBLING_TREES (os.pathsep-separated) to point at others; when it is
#: unset the two known working copies are tried and any that is absent is
#: reported as a skip. On CI neither exists, which is expected and stated.
SIBLING_ENV = "DMNEWS_SIBLING_TREES"
DEFAULT_SIBLINGS = (
    r"C:\Users\Nitro 5\daily-media-news",
    r"C:\src\dmnews-public",
)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".wrangler"}
MAX_BYTES = 4 * 1024 * 1024


def _git(root, *args):
    """(returncode, stdout) for a git command, or (127, '') if git is absent."""
    try:
        done = subprocess.run(
            ["git", "-C", root] + list(args),
            capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout.decode("utf-8", "replace")


def _read(path):
    """Text of a file, or None when it is binary, too large, or unreadable."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def declared_namespace_ids(text):
    """[(label, value)] for every KV namespace id this text declares.

    `label` names the shape, not the location, so the caller can say WHICH rule
    fired without printing the value.
    """
    found = []
    for block in KV_BLOCK_RE.finditer(text):
        for match in KV_ID_RE.finditer(block.group(1)):
            found.append(("[[kv_namespaces]] id", match.group(1)))
    for match in CONST_ID_RE.finditer(text):
        found.append(("NAMESPACE_ID constant", match.group(1)))
    return found


def tracked_files(root):
    """Tracked, non-ignored paths. Falls back to a walk if git is unavailable."""
    rc, out = _git(root, "ls-files", "-z")
    if rc == 0:
        return [name for name in out.split("\0") if name]
    walked = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            walked.append(os.path.relpath(os.path.join(base, name), root))
    return walked


class CommittedConfigTest(unittest.TestCase):
    """Rule 1: the committed template itself names only the placeholder."""

    def setUp(self):
        self.template = os.path.join(REPO_ROOT, WRANGLER_TOML_REL)
        self.assertTrue(os.path.isfile(self.template),
                        "%s is missing, so the deploy config under test does not "
                        "exist" % WRANGLER_TOML_REL)

    def test_the_template_declares_exactly_one_kv_id_and_it_is_the_placeholder(self):
        text = _read(self.template)
        self.assertIsNotNone(text, "%s is not readable UTF-8" % WRANGLER_TOML_REL)
        blocks = list(KV_BLOCK_RE.finditer(text))
        self.assertEqual(1, len(blocks),
                         "%s must declare exactly one [[kv_namespaces]] table, "
                         "found %d" % (WRANGLER_TOML_REL, len(blocks)))
        ids = KV_ID_RE.findall(blocks[0].group(1))
        self.assertEqual(1, len(ids),
                         "%s must declare exactly one KV namespace id inside its "
                         "[[kv_namespaces]] table, found %d"
                         % (WRANGLER_TOML_REL, len(ids)))
        self.assertEqual(PLACEHOLDER, ids[0].lower(),
                         "%s binds a 32-hex id that is NOT the published "
                         "placeholder. A real namespace id must never be committed: "
                         "supply it through $env:DMNEWS_KV_NAMESPACE_ID and let "
                         "scripts/deploy-worker.ps1 render it. (%s:%d)"
                         % (WRANGLER_TOML_REL, WRANGLER_TOML_REL, self._id_line()))

    def _id_line(self):
        text = _read(self.template)
        for number, line in enumerate(text.split("\n"), 1):
            if KV_ID_RE.search(line):
                return number
        return 0

    def test_no_other_32hex_id_is_committed_in_the_template(self):
        """The `main`, `name` and `compatibility_date` lines hold no such value;
        a second 32-hex id here would mean a second, unaccounted binding."""
        text = _read(self.template)
        found = declared_namespace_ids(text)
        offenders = [label for label, value in found
                     if value.lower() != PLACEHOLDER]
        self.assertEqual([], offenders,
                         "%s declares a namespace id that is not the placeholder "
                         "(%s)" % (WRANGLER_TOML_REL, ", ".join(offenders) or "-"))


class WholeTreeTest(unittest.TestCase):
    """Rule 2: nothing else in the tree may carry a real-looking id either."""

    def test_every_declared_namespace_id_in_the_tree_is_the_placeholder(self):
        offenders = []
        for rel in tracked_files(REPO_ROOT):
            if any(part in SKIP_DIRS for part in rel.split(os.sep)):
                continue
            text = _read(os.path.join(REPO_ROOT, rel))
            if text is None:
                continue
            for label, value in declared_namespace_ids(text):
                if value.lower() != PLACEHOLDER:
                    # Path, shape and line -- never the value, which would put a
                    # real id into a CI log.
                    line = self._line_of(text, value)
                    offenders.append("%s:%d (%s)" % (rel.replace(os.sep, "/"),
                                                      line, label))
        self.assertEqual(
            [], offenders,
            "these declarations hold a 32-hex id that is not the published "
            "placeholder, so a real KV namespace id is committed again:\n  "
            + "\n  ".join(offenders)
            + "\nUse the placeholder in fixtures, and the environment variable at "
              "deploy time.")

    @staticmethod
    def _line_of(text, value):
        for number, line in enumerate(text.split("\n"), 1):
            if value in line:
                return number
        return 0


class RenderScriptTest(unittest.TestCase):
    """Rule 3: the two halves of the substitution must agree, or it is a no-op."""

    def test_the_script_uses_the_same_placeholder_as_the_template(self):
        text = _read(os.path.join(REPO_ROOT, RENDER_SCRIPT_REL))
        self.assertIsNotNone(text, "%s is missing or unreadable" % RENDER_SCRIPT_REL)
        self.assertIn(
            PLACEHOLDER, text,
            "%s does not carry the placeholder %r, so it cannot render the "
            "committed template -- the substitution would match nothing and the "
            "deploy would bind the placeholder id."
            % (RENDER_SCRIPT_REL, PLACEHOLDER))

    def test_the_script_refuses_to_deploy_without_the_environment_variable(self):
        """A fallback to the placeholder is the failure this whole change exists
        to prevent, so the absence of the variable must fail, not default."""
        text = _read(os.path.join(REPO_ROOT, RENDER_SCRIPT_REL))
        self.assertIn("DMNEWS_KV_NAMESPACE_ID", text,
                      "%s does not name the supply variable, so the docs and the "
                      "script would disagree about where the id comes from"
                      % RENDER_SCRIPT_REL)
        self.assertIn("exit 2", text,
                      "%s must exit non-zero when the id is absent; a deploy that "
                      "quietly continues binds the placeholder" % RENDER_SCRIPT_REL)

    def test_the_rendered_config_is_gitignored_and_never_tracked(self):
        # --no-index so the answer is about the PATTERNS, and holds before the
        # rendered file has ever been written on this machine.
        rc, _ = _git(REPO_ROOT, "check-ignore", "--no-index", "-q", RENDERED_CONFIG_REL)
        self.assertEqual(0, rc,
                         "%s is not gitignored. It holds the real namespace id "
                         "after a deploy, so an unignored copy is one `git add` "
                         "away from publication." % RENDERED_CONFIG_REL)
        rc, out = _git(REPO_ROOT, "ls-files", "--", RENDERED_CONFIG_REL)
        self.assertEqual("", out.strip(),
                         "%s is TRACKED. It holds the real namespace id; remove it "
                         "from the index with `git rm --cached`." % RENDERED_CONFIG_REL)


class SiblingTreeTest(unittest.TestCase):
    """Rule 4: the OTHER working copy is held to the same rule.

    Locally this is the whole point -- the two trees are not synced by a tool, so
    a fix applied to one and not the other is exactly how the real id survived in
    the private copy while the published one was clean. On CI only one tree
    exists; that arm reports a skip and says so.
    """

    def _siblings(self):
        configured = os.environ.get(SIBLING_ENV, "")
        candidates = ([p for p in configured.split(os.pathsep) if p.strip()]
                      if configured.strip() else list(DEFAULT_SIBLINGS))
        return [p for p in candidates
                if os.path.abspath(p) != os.path.abspath(REPO_ROOT)]

    def test_no_sibling_tree_commits_a_real_namespace_id(self):
        siblings = self._siblings()
        present = [p for p in siblings if os.path.isdir(os.path.join(p, "deploy"))]
        if not present:
            self.skipTest(
                "no sibling tree to check (tried: %s). On CI only this checkout "
                "exists; the cross-tree arm runs locally." % ", ".join(siblings or ["-"]))

        problems = []
        for root in present:
            template = os.path.join(root, WRANGLER_TOML_REL)
            if not os.path.isfile(template):
                problems.append("%s: no %s" % (root, WRANGLER_TOML_REL))
                continue
            for rel in tracked_files(root):
                if any(part in SKIP_DIRS for part in rel.split(os.sep)):
                    continue
                text = _read(os.path.join(root, rel))
                if text is None:
                    continue
                for label, value in declared_namespace_ids(text):
                    if value.lower() != PLACEHOLDER:
                        problems.append("%s -> %s (%s)"
                                        % (root, rel.replace(os.sep, "/"), label))
        self.assertEqual([], problems,
                         "another working copy commits a 32-hex id that is not "
                         "the placeholder:\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
