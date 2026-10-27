#!/usr/bin/env python3
"""tests/academic/test_state_restore.py -- the two state-load failures, told apart.

An ABSENT state file and a PRESENT but invalid one both raised StateError and
exited 3, and academic-result.yml mapped 3 to "corrupt state file". The October
2026 diagnosis was an absent file: this repository's history is fresh (root
e090b85), state/academic_state.json was never committed because it carries the
owner's roll number, so the run reported corruption for a file that had never
existed here.

The remedies are opposite -- restore an absent file, never overwrite an invalid
one -- so they are separate exit codes now, and the message names which case it
is. A JSON parse error and a schema violation are both "present but invalid":
neither may be replaced, and neither may be reported as missing.

Run:
    py tests\\academic\\test_state_restore.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import check_academic_result as car  # noqa: E402

WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "academic-result.yml")
POLL_URL = "https://example.invalid/notices?notice_type=21"


def _must_not_poll(url):
    """A fetch_fn that proves run() returned before it touched the network."""
    raise AssertionError("run() called the feed with no usable state: %r" % (url,))


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class StateNotFoundTests(unittest.TestCase):
    """An absent file is not corruption. It is a file to restore."""

    def test_absent_state_exits_4_and_names_the_kv_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "academic_state.json")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = car.run(path, POLL_URL, False, fetch_fn=_must_not_poll)
            message = err.getvalue()
            self.assertEqual(4, code, message)
            self.assertIn("state not found", message)
            self.assertIn(car.STATE_KV_KEY, message)
            self.assertIn(path, message)
            # The word that sent the diagnosis the wrong way: nothing was
            # corrupt, the file did not exist.
            self.assertNotIn("corrupt", message.lower())
            self.assertFalse(
                os.path.exists(path),
                "an absent state must never be re-created by a run: "
                "re-baselining last_examined_notice_id turns an old notice "
                "into a fake new result")

    def test_load_state_raises_the_subclass_so_callers_can_tell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "academic_state.json")
            with self.assertRaises(car.StateNotFound) as caught:
                car.load_state(path)
            # Still a StateError, so every existing `except StateError` keeps
            # working; the subclass only ADDS the ability to tell them apart.
            self.assertIsInstance(caught.exception, car.StateError)


class StatePresentButInvalidTests(unittest.TestCase):
    """A file that exists and cannot be trusted must be refused, not replaced."""

    def _run(self, path):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = car.run(path, POLL_URL, False, fetch_fn=_must_not_poll)
        return code, err.getvalue()

    def test_unparseable_state_exits_3_and_says_it_is_present(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "academic_state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            before = _read_bytes(path)
            code, message = self._run(path)
            self.assertEqual(3, code, message)
            self.assertIn("state present but", message)
            self.assertIn(path, message)
            self.assertNotIn("state not found", message)
            self.assertEqual(
                before, _read_bytes(path),
                "a present-but-invalid state must be left exactly as found: "
                "the caller refuses to overwrite it")

    def test_bad_schema_exits_3_and_names_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "academic_state.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 99}, handle)
            code, message = self._run(path)
            self.assertEqual(3, code, message)
            self.assertIn("state present but invalid", message)
            self.assertIn("schema_version", message)
            self.assertNotIn("state not found", message)


class KvKeyIsTheSameInBothPlacesTests(unittest.TestCase):
    """The script's error message names the key; the workflow must use it."""

    def test_workflow_mentions_the_script_kv_key(self):
        with open(WORKFLOW, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(
            car.STATE_KV_KEY, text,
            "academic-result.yml no longer mentions %r, so the key named by "
            "the state-not-found message is read by nothing"
            % car.STATE_KV_KEY)
        self.assertIn("Restore academic state from KV", text)
        self.assertIn("Persist academic state to KV", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
