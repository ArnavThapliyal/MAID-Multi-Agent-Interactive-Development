"""Unit tests for the pure helpers in bridge/orchestrator.py: slugify and extract_json.

Purpose
    Pin down the observable behaviour of the two text helpers the orchestrator
    relies on when naming spec files and parsing Agent 0's replies.

Usage
    python -m unittest tests/test_orchestrator_helpers -v     (from the project root)

@rollback: mutates sys.path for this process only (prepends bridge/ so that
    `orchestrator` is importable). The helpers under test are pure and the
    tests write no files, env vars or git state. Side effects: importing
    `orchestrator` reads its MAID_* env defaults and sets up the MAID logger
    (get_maid_logger), which may open the log files under log/; no worker or
    model call is made. To revert, delete this file:
    `rm tests/test_orchestrator_helpers.py`.
"""

import json
import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(PROJECT_ROOT, "bridge")
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from orchestrator import extract_json, slugify  # noqa: E402

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class TestSlugify(unittest.TestCase):
    """slugify: lowercase, hyphen-separated, filename-safe, never empty.

    @rollback: none (pure function under test, no state mutated).
    """

    def test_plain_ascii(self) -> None:
        self.assertEqual(slugify("hello world"), "hello-world")

    def test_special_chars_mixed_case(self) -> None:
        slug = slugify("  Add Unit-Tests for slugify() & extract_json: v2.0!!  ")
        self.assertEqual(slug, "add-unit-tests-for-slugify-extract-json-v2-0")
        self.assertRegex(slug, SLUG_PATTERN)

    def test_empty_string(self) -> None:
        # An empty slug would produce an unusable filename, so the helper
        # falls back to "task" instead of returning "".
        self.assertEqual(slugify(""), "task")

    def test_only_special_chars_falls_back(self) -> None:
        self.assertEqual(slugify("?!... ---"), "task")

    def test_already_slug_is_idempotent(self) -> None:
        slug = "my-existing-slug"
        self.assertEqual(slugify(slug), slug)
        self.assertEqual(slugify(slugify(slug)), slugify(slug))

    def test_limit_truncates_without_trailing_hyphen(self) -> None:
        self.assertEqual(slugify("abc def ghi", limit=4), "abc")
        long_slug = slugify("word " * 40)
        self.assertLessEqual(len(long_slug), 50)
        self.assertRegex(long_slug, SLUG_PATTERN)


class TestExtractJson(unittest.TestCase):
    """extract_json: first JSON *object* in the text, ValueError otherwise.

    @rollback: none (pure function under test, no state mutated).
    """

    def test_valid_json_object(self) -> None:
        self.assertEqual(extract_json('{"key": "value"}'), {"key": "value"})

    def test_valid_json_array(self) -> None:
        # The helper only ever returns a dict: a top-level array is not
        # returned as a list. An array of objects yields its first object,
        # and an array holding no object is rejected.
        payload = json.dumps([{"id": "U1"}, {"id": "U2"}])
        self.assertEqual(extract_json(payload), {"id": "U1"})
        with self.assertRaises(ValueError):
            extract_json("[1, 2, 3]")

    def test_json_embedded_in_prose(self) -> None:
        fenced = 'Here is the result:\n```json\n{"a": 1}\n```\nHope that helps!'
        self.assertEqual(extract_json(fenced), {"a": 1})

        nested = {"units": [{"id": "U1", "ok": True}], "note": "use {braces}"}
        prose = "Sure, the plan is " + json.dumps(nested) + " and nothing else."
        self.assertEqual(extract_json(prose), nested)

    def test_skips_undecodable_brace_before_object(self) -> None:
        self.assertEqual(extract_json('a {stray} brace, then {"b": 2}'), {"b": 2})

    def test_invalid_input_raises(self) -> None:
        for text in ("this is not json at all", "{not: valid}", "", None):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    extract_json(text)


if __name__ == "__main__":
    unittest.main()
