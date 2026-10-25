"""Staleness and integrity guard for tests/fixtures/real/ (contract 1h).

WHY THIS EXISTS, AND WHY THE THREE RULES HAVE DIFFERENT SEVERITIES
-----------------------------------------------------------------
Every fixture in that tree is real bytes from a live site, captured by hand and
committed so the parsers are proven against reality instead of against a
hand-written body that was shaped to match the parser. That makes the tree
valuable and makes it dangerous in three specific ways:

  (a) A LYING fixture -- a sha256 that does not match the bytes on disk -- is
      WORSE THAN NO FIXTURE. It reads as evidence and proves nothing, and a
      reviewer has no way to see the lie without recomputing a hash. HARD
      FAILURE. This is the one rule with teeth, because it is the only one that
      can be wrong by accident without anyone noticing.

  (b) A STALE fixture is OLDER, not wrong. Notice 15673's page will still parse
      the same way next year. Failing CI because a capture is 91 days old would
      train everyone to ignore this file, which is how a guard becomes
      decoration. WARNING ONLY.

  (c) An ORPHAN file -- bytes on disk that no manifest row indexes -- is a
      capture nobody reads. It looks like coverage in a directory listing and is
      invisible to every expectation test, so it is a false promise. HARD
      FAILURE: the manifest is the index and the single source of truth, and an
      unindexed fixture is exactly the "test nobody wrote" hole this whole unit
      exists to close.

Stdlib only, fully offline, read-only: it inspects the tree and the JSON and
never writes.
"""

import hashlib
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
MANIFEST = os.path.join(HERE, "manifest.json")

#: (b) Staleness. 90 days. Warning only, and the reason is in the docstring.
STALE_AFTER_DAYS = 90


def _load_manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _crlf_counts():
    """`{name: number of CR bytes}` for every capture on disk.

    RULE (d), and the reason this function exists at all. This tree is hashed
    byte-for-byte and the hashes are verified on an Ubuntu CI runner, where git
    checks files out with NO line-ending conversion. A Windows capture written
    with `core.autocrlf=true` committed its CRLF as LF, so a fresh checkout held
    fewer bytes than the manifest recorded, and two tests went red on every run
    while passing on the capturing machine. A capture that drifts on checkout is
    a lying fixture by definition: it proves nothing on the runner that
    actually checks it.

    So CR bytes in a capture are not a style question. Each one is a byte that
    will be stripped on the runner and break the hash. Zero is the contract.
    """
    counts = {}
    for name in sorted(os.listdir(HERE)):
        if name == "__pycache__" or name.endswith(".meta.json"):
            continue
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            counts[name] = fh.read().count(b"\r")
    return counts


def _parse_iso(value):
    import datetime
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ManifestShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest()
        cls.samples = cls.manifest.get("samples") or []

    def test_manifest_indexes_at_least_one_sample(self):
        self.assertTrue(self.samples, "manifest.json indexes nothing")

    def test_every_sample_names_a_file_that_exists(self):
        for sample in self.samples:
            path = os.path.join(HERE, sample["file"])
            self.assertTrue(os.path.isfile(path),
                            "manifest indexes a missing file: %s" % sample["file"])

    def test_every_sample_declares_a_guard(self):
        """A fixture must say what it PROTECTS.

        Without a guard, deleting it looks harmless and nobody can tell which
        behaviour its bytes are the only evidence for.
        """
        for sample in self.samples:
            self.assertTrue(
                (sample.get("guard") or "").strip(),
                "%s declares no guard: it would be deleted as 'just a fixture'"
                % sample.get("file"))

    def test_every_sample_declares_an_expect_block(self):
        """A capture without an `expect` block is a file, not a contract."""
        for sample in self.samples:
            self.assertIsInstance(sample.get("expect"), dict,
                                  "%s has no expect block" % sample.get("file"))
            self.assertTrue(sample["expect"],
                            "%s has an empty expect block" % sample.get("file"))

    def test_required_coverage_is_present(self):
        """The minimum this tree must hold, per the capture contract."""
        kinds = {s.get("kind") for s in self.samples}
        self.assertIn("feed", kinds, "no genuine feed response captured")
        self.assertIn("notice_list", kinds, "no IOST notice list captured")
        self.assertGreaterEqual(
            len([s for s in self.samples if s.get("kind") == "notice_detail"]), 3,
            "expected at least 3 IOST detail pages")
        self.assertIn("pdf_shape", kinds, "no pdf_shape metadata captured")
        origins = {s.get("origin") for s in self.samples}
        self.assertIn("iost.tu.edu.np", origins)
        self.assertIn("news.google.com", origins)

    def test_a_devanagari_detail_page_is_captured(self):
        """The 15696 shape: a real Nepali-titled notice, pinned to `no_match`.

        The token-set regression this tree exists to catch is invisible in a
        Latin-only capture set, so the presence of a Devanagari sample is
        asserted rather than hoped for.
        """
        dev = [s for s in self.samples
               if s.get("kind") == "notice_detail"
               and (s.get("expect") or {}).get("script") == "devanagari"]
        self.assertTrue(dev, "no Devanagari detail page captured")
        self.assertTrue(
            any((s["expect"].get("classify_status") == "no_match") for s in dev),
            "the Devanagari sample must pin a real verdict, not just a script")

    def test_sample_filenames_follow_the_declared_pattern(self):
        """`<kind>_<host>_<YYYY-MM-DD>.<ext>`.

        A date-stamped name is what lets a reviewer see at a glance HOW OLD a
        capture is without opening a file, and what makes a re-capture
        obviously a new file rather than an overwrite.
        """
        import re
        pat = re.compile(
            r"^(notice_list|notice_detail|feed|pdf_shape)_[A-Za-z0-9.\-]+_"
            r"\d{4}-\d{2}-\d{2}\.(html|rss|json)$")
        for sample in self.samples:
            self.assertRegex(sample["file"], pat,
                             "%s does not follow the naming pattern"
                             % sample["file"])


class ShaIntegrityTests(unittest.TestCase):
    """(a) HARD FAILURE. A lying fixture is worse than no fixture."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest()
        cls.samples = cls.manifest.get("samples") or []

    def test_every_declared_sha256_matches_the_file_on_disk(self):
        mismatched = []
        for sample in self.samples:
            path = os.path.join(HERE, sample["file"])
            if not os.path.isfile(path):
                mismatched.append("%s: file missing" % sample["file"])
                continue
            actual = _sha256_file(path)
            if actual != sample.get("sha256"):
                mismatched.append(
                    "%s: manifest says %s, disk says %s"
                    % (sample["file"], str(sample.get("sha256"))[:16],
                       actual[:16]))
        self.assertEqual(
            [], mismatched,
            "a fixture whose sha256 does not match its bytes is LYING - it "
            "reads as evidence and proves nothing:\n  " + "\n  ".join(mismatched))

    def test_every_sidecar_sha256_matches_too(self):
        """Each capture writes a `.meta.json` sidecar; it must agree as well."""
        mismatched = []
        for sample in self.samples:
            sidecar_path = os.path.join(HERE, sample["file"] + ".meta.json")
            if not os.path.isfile(sidecar_path):
                mismatched.append("%s.meta.json: missing" % sample["file"])
                continue
            with open(sidecar_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            for key in ("source_url", "captured_at", "sha256", "bytes",
                        "trimmed", "note"):
                if key not in meta:
                    mismatched.append("%s.meta.json: no %r"
                                      % (sample["file"], key))
            path = os.path.join(HERE, sample["file"])
            if os.path.isfile(path) and meta.get("sha256") != _sha256_file(path):
                mismatched.append("%s: sidecar sha256 disagrees with the file"
                                  % sample["file"])
        self.assertEqual([], mismatched,
                         "sidecar metadata must be complete and true:\n  "
                         + "\n  ".join(mismatched))

    def test_no_capture_carries_a_byte_that_checkout_will_strip(self):
        """(d) HARD FAILURE. A CR in a capture is a hash waiting to break.

        The 2026-09-30 CI failure was exactly this and nothing else: two
        captures were recorded with 2 and 12 CR bytes, `core.autocrlf=true`
        stripped them at commit time, and the Ubuntu checkout therefore held
        different bytes than the manifest recorded. The content was fine and the
        guard was right to fail -- it was the only thing in the repository that
        noticed.

        Asserting it here means the next capture fails on the machine that made
        it, where the fix is one `_lf()` call, instead of on CI days later,
        where the only honest options are re-recording a hash or deleting
        evidence.
        """
        offenders = ["%s: %d CR byte(s)" % (name, count)
                     for name, count in _crlf_counts().items() if count]
        self.assertEqual(
            [], offenders,
            "capture(s) contain CR bytes. This tree is byte-hashed and the hash "
            "is checked on an Ubuntu checkout, which applies no line-ending "
            "conversion: a CR written here is stripped in the repository and "
            "the recorded sha256 stops matching the committed bytes. Re-capture "
            "through tests/fixtures/real/capture.py, which normalises to LF:\n  "
            + "\n  ".join(offenders))

    def test_manifest_and_sidecar_agree_on_bytes(self):
        for sample in self.samples:
            sidecar_path = os.path.join(HERE, sample["file"] + ".meta.json")
            if not os.path.isfile(sidecar_path):
                continue
            with open(sidecar_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            self.assertEqual(sample.get("bytes"), meta.get("bytes"),
                             "%s: manifest and sidecar disagree on size"
                             % sample["file"])


class StalenessTests(unittest.TestCase):
    """(b) WARNING ONLY. An old capture is older, not wrong."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest()
        cls.samples = cls.manifest.get("samples") or []

    def test_no_capture_is_older_than_the_staleness_window(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        stale = []
        for sample in self.samples:
            stamp = _parse_iso(sample.get("captured_at"))
            if stamp is None:
                stale.append("%s: captured_at is missing or unparseable (%r)"
                             % (sample.get("file"), sample.get("captured_at")))
                continue
            age = (now - stamp).days
            if age > STALE_AFTER_DAYS:
                stale.append("%s: %d days old (>%d) - re-capture with "
                             "tests/fixtures/real/capture.py"
                             % (sample["file"], age, STALE_AFTER_DAYS))
            elif age > STALE_AFTER_DAYS - 15:
                print("canary fixtures: %s is %d days old; re-capture within %d "
                      "days" % (sample["file"], age, 15 - (age - STALE_AFTER_DAYS)))
        if stale:
            print("::warning::real fixtures stale:\n  " + "\n  ".join(stale))
        self.assertEqual(
            [], stale,
            "WARNING-ONLY RULE, and it failed anyway: %d capture(s) are older "
            "than %d days. Re-run tests/fixtures/real/capture.py and update the "
            "manifest. If a capture genuinely cannot be refreshed, delete the "
            "sample rather than letting it rot silently."
            % (len(stale), STALE_AFTER_DAYS))

    def test_staleness_is_a_warning_not_a_silent_pass(self):
        """The severity split is asserted, not just described.

        A stale capture must NOT be treated like a lying one. If this test ever
        fails, someone has made staleness fatal and the guard has started
        crying wolf, which is how guards get ignored.
        """
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        ages = []
        for sample in self.samples:
            stamp = _parse_iso(sample.get("captured_at"))
            if stamp is not None:
                ages.append((now - stamp).days)
        self.assertTrue(ages, "no parseable captured_at values")
        newest = min(ages)
        self.assertLessEqual(
            newest, STALE_AFTER_DAYS,
            "the NEWEST capture is already past the window; refresh the tree")


class OrphanTests(unittest.TestCase):
    """(c) HARD FAILURE. An unindexed fixture is one nobody reads."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest()
        cls.samples = cls.manifest.get("samples") or []

    #: The only non-fixture files allowed to live in this tree. The freshness
    #: guard is itself one of them, so the orphan rule has to name them
    #: explicitly rather than sweep "ignore *.py" -- a blanket *.py exemption
    #: would let a half-written capture helper sit here unindexed forever.
    TOOLING_FILES = frozenset({
        "manifest.json",       # the index itself
        "capture.py",          # how a capture is made
        "test_manifest_freshness.py",   # this guard
        "__pycache__",
    })

    #: A capture artefact's name shape. Anything matching it MUST be indexed.
    ARTEFACT_RE = __import__("re").compile(
        r"^(notice_list|notice_detail|feed|pdf_shape)_[A-Za-z0-9.\-]+_"
        r"\d{4}-\d{2}-\d{2}\.(html|rss|json)(\.meta\.json)?$")

    def _on_disk(self):
        return {name for name in os.listdir(HERE)
                if name not in self.TOOLING_FILES and not name.startswith(".")}

    def test_no_orphan_capture_files(self):
        """Every capture artefact on disk must be indexed by the manifest.

        Capture writes a data file AND a `.meta.json` sidecar, so the index has
        to cover both; an indexed sample whose sidecar is missing is the mirror
        image of the same problem and is caught by the sidecar test above.
        """
        indexed = set()
        for sample in self.samples:
            indexed.add(sample["file"])
            indexed.add(sample["file"] + ".meta.json")
        on_disk = self._on_disk()
        # Anything that LOOKS like a capture artefact must be indexed. Anything
        # else in this tree is a hard failure too -- this directory has no
        # business holding loose files at all.
        orphans = sorted(on_disk - indexed)
        unrecognised = sorted(
            name for name in on_disk
            if name not in indexed and not self.ARTEFACT_RE.match(name))
        self.assertEqual(
            [], orphans,
            "unindexed fixture(s) in tests/fixtures/real/:\n  %s\n"
            "These are captures nobody reads: the directory listing makes them "
            "look like coverage, and no expectation test touches them. Either "
            "re-run tests/fixtures/real/capture.py so the manifest indexes them, "
            "or delete them." % "\n  ".join(orphans))
        self.assertEqual(
            [], unrecognised,
            "unrecognised file(s) in tests/fixtures/real/:\n  %s\n"
            "This directory holds captures, their sidecars, the manifest, the "
            "capture tool and this guard -- nothing else. A file nobody can "
            "classify is a file nobody reads." % "\n  ".join(unrecognised))

    def test_every_indexed_sample_has_both_a_file_and_a_sidecar(self):
        for sample in self.samples:
            for name in (sample["file"], sample["file"] + ".meta.json"):
                self.assertTrue(
                    os.path.isfile(os.path.join(HERE, name)),
                    "manifest indexes %s but it is not on disk" % name)

    def test_no_manifest_row_points_at_the_same_file_twice(self):
        seen = {}
        for sample in self.samples:
            prev = seen.get(sample["file"])
            self.assertIsNone(
                prev, "%s is indexed twice; the second row is unreachable"
                % sample["file"])
            seen[sample["file"]] = sample


class NoStudentDataTests(unittest.TestCase):
    """A TU result sheet is a table of OTHER students' names, rolls and marks.

    "It is public" is not a licence to mirror that into a git repository, and a
    PDF committed "just for the test" is unrecoverable once pushed. So the
    committed PDF evidence is `pdf_shape_*.json` metadata only, and this
    asserts that shape rather than trusting the policy note.
    """

    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest()
        cls.samples = cls.manifest.get("samples") or []

    def test_no_binary_pdf_is_committed_anywhere_in_the_tree(self):
        pdfs = []
        for dirpath, _dirs, files in os.walk(HERE):
            for name in files:
                if name.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(dirpath, name))
        self.assertEqual(
            [], pdfs,
            "a real result PDF must never be committed:\n  " + "\n  ".join(pdfs))

    def test_pdf_shape_samples_are_metadata_with_the_required_keys(self):
        shapes = [s for s in self.samples if s.get("kind") == "pdf_shape"]
        self.assertTrue(shapes, "no pdf_shape sample captured")
        for sample in shapes:
            self.assertTrue(sample["file"].endswith(".json"),
                            "%s: a pdf_shape sample must be JSON metadata"
                            % sample["file"])
            with open(os.path.join(HERE, sample["file"]), encoding="utf-8") as fh:
                payload = json.load(fh)
            for key in ("source_url", "captured_at", "source_sha256", "bytes",
                        "page_count", "roll_shaped_tokens_present",
                        "roll_split_across_columns", "extractor", "note"):
                self.assertIn(key, payload,
                              "%s: pdf_shape metadata is missing %r"
                              % (sample["file"], key))
            for banned in ("text", "names", "students", "rolls", "marks",
                           "roll", "name"):
                self.assertNotIn(
                    banned, payload,
                    "%s: %r must never be committed - a result sheet is other "
                    "students' records" % (sample["file"], banned))

    def test_pdf_shape_records_a_real_roll_shape(self):
        """The shape evidence has to be EVIDENCE, not a placeholder.

        `roll_shaped_tokens_present: false` with no explanation would mean the
        capture proved nothing about the reader, and a fixture that proves
        nothing should not be committed at all.
        """
        for sample in self.samples:
            if sample.get("kind") != "pdf_shape":
                continue
            with open(os.path.join(HERE, sample["file"]), encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertTrue(
                payload.get("roll_shaped_tokens_present"),
                "%s: the captured sheet carried no roll-shaped token, so this "
                "sample evidences nothing about the reader" % sample["file"])
            self.assertTrue(
                payload.get("readable_by_sheet_text_is_readable"),
                "%s: the captured sheet was NOT readable by "
                "sheet_text_is_readable; if that is a real change, update the "
                "manifest deliberately rather than deleting the evidence"
                % sample["file"])


class RealBytesTests(unittest.TestCase):
    """These captures must be GENUINE, not reconstructions.

    `tests/collection/test_collector_retry.py` (not this file) asserts a real
    fixture is not the hand-written `<rss><channel><item><title>Headline N`
    shape. This asserts the complementary half: the bytes parse as the real
    documents claim to be, and they are big enough and varied enough to be
    actual responses rather than minimal ones.
    """

    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest()
        cls.samples = cls.manifest.get("samples") or []

    def test_feed_capture_is_a_real_rss_document_with_varied_titles(self):
        import xml.etree.ElementTree as ET
        feeds = [s for s in self.samples if s.get("kind") == "feed"]
        self.assertTrue(feeds, "no feed capture")
        for sample in feeds:
            with open(os.path.join(HERE, sample["file"]), "rb") as fh:
                data = fh.read()
            root = ET.fromstring(data)
            self.assertEqual("rss", root.tag.rsplit("}", 1)[-1].lower())
            items = [el for el in root.iter()
                     if el.tag.rsplit("}", 1)[-1] == "item"]
            self.assertGreaterEqual(len(items), 5,
                                    "%s: only %d items" % (sample["file"],
                                                           len(items)))
            titles = [(el.findtext("title") or "") for el in items]
            self.assertFalse(
                [t for t in titles if t.startswith("Headline ")],
                "%s: carries the synthetic 'Headline N' marker, so it is a "
                "reconstruction rather than a capture" % sample["file"])
            self.assertGreaterEqual(
                len(set(titles)), 20,
                "%s: only %d distinct titles; a real feed is not a ladder of "
                "repeats" % (sample["file"], len(set(titles))))
            self.assertGreater(
                len(data), 20000,
                "%s: %d bytes is too small to be a real Google News response"
                % (sample["file"], len(data)))

    def test_notice_captures_are_real_pages(self):
        """A real IOST page is a full HTML document, not a three-anchor stub."""
        for sample in self.samples:
            if sample.get("kind") not in ("notice_list", "notice_detail"):
                continue
            with open(os.path.join(HERE, sample["file"]), "rb") as fh:
                data = fh.read()
            text = data.decode("utf-8", errors="replace").lower()
            self.assertIn("<html", text,
                          "%s: not an HTML document" % sample["file"])
            self.assertIn("</html>", text,
                          "%s: truncated" % sample["file"])
            self.assertGreater(
                len(data), 20000,
                "%s: %d bytes is too small to be a real page"
                % (sample["file"], len(data)))


if __name__ == "__main__":
    unittest.main()
