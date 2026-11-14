# Task spec: Add unit tests for slugify and extract_json

| Field | Value |
|---|---|
| Correlation ID | `04db7d18820c` |
| Created | 2026-11-15T04:33:27+05:30 |
| Base commit | `cae2db1436eecc187baa53669389fd72c38a79bb` |
| Planner | Agent 0 (`qwen/qwen3.8-27b`) |

## Task

add tests for the orchestrator helper functions

## Clarifications

- **Q:** Which functions in bridge/orchestrator.py count as 'helper functions' (e.g., all private/pure utility functions, or a specific subset)?
  **A:** Cover only slugify and extract_json. Put everything in the single file tests/test_orchestrator_helpers.py. Use unittest from the standard library with no mocks and no new dependencies; the test file adds bridge/ to sys.path itself.
- **Q:** Which test framework should be used (pytest, unittest, other)?
  **A:** Cover only slugify and extract_json. Put everything in the single file tests/test_orchestrator_helpers.py. Use unittest from the standard library with no mocks and no new dependencies; the test file adds bridge/ to sys.path itself.
- **Q:** Where should the test file(s) be placed (e.g., tests/, bridge/tests/, test_orchestrator.py at root)?
  **A:** Cover only slugify and extract_json. Put everything in the single file tests/test_orchestrator_helpers.py. Use unittest from the standard library with no mocks and no new dependencies; the test file adds bridge/ to sys.path itself.

## Summary

Create tests/test_orchestrator_helpers.py with unittest-based tests covering slugify and extract_json from bridge/orchestrator.py. No mocks, no third-party dependencies; the test file adds bridge/ to sys.path itself.

## Atomic units

### U1: create `tests/test_orchestrator_helpers.py` (whole file)

Write a self-contained unittest test file. At the top, insert the project root into sys.path so that `from orchestrator import slugify, extract_json` works. Define TestSlugify(unittest.TestCase) with methods covering: a plain ASCII string, a string with special characters and mixed case, an empty string, and a string that is already a valid slug (idempotency). Define TestExtractJson(unittest.TestCase) with methods covering: a valid JSON object string returns the correct parsed value, a valid JSON array string returns the correct list, a string with JSON embedded in surrounding prose (e.g. code fences or free text) still extracts the JSON, and an unparseable string raises an exception (assertRaises). Use only the standard library (unittest, json, re if needed for inspection). Do NOT use mock, unittest.mock, or any third-party package.

- **Scope:** only `tests/test_orchestrator_helpers.py` may be created or modified.
- **Suggested agent:** claude — Test writing and verification is a claude task per the routing guide; requires understanding the exact behaviour of slugify and extract_json to write correct assertions.
- **Required signatures:**
  - `class TestSlugify(unittest.TestCase):`
  - `    def test_plain_ascii(self) -> None`
  - `    def test_special_chars_mixed_case(self) -> None`
  - `    def test_empty_string(self) -> None`
  - `    def test_already_slug_is_idempotent(self) -> None`
  - `class TestExtractJson(unittest.TestCase):`
  - `    def test_valid_json_object(self) -> None`
  - `    def test_valid_json_array(self) -> None`
  - `    def test_json_embedded_in_prose(self) -> None`
  - `    def test_invalid_input_raises(self) -> None`
- **Acceptance:**
  - tests/test_orchestrator_helpers.py exists and imports only from the standard library (unittest, json, sys, os, re at most)
  - The file adds the bridge/ directory (or project root) to sys.path before importing orchestrator
  - TestSlugify contains at least 4 test methods covering plain ASCII, special chars/mixed case, empty string, and idempotency
  - TestExtractJson contains at least 4 test methods covering valid object, valid array, embedded-in-prose extraction, and invalid-input exception
  - No use of mock, unittest.mock, or any third-party package
  - Running `python -m unittest tests/test_orchestrator_helpers` from the project root passes all tests
- **Rollback checkpoint:** taken immediately before dispatch (HEAD hash plus a copy of the file under `log/backups/04db7d18820c/U1-attempt<N>/`).
- **Rollback:** `rm tests/test_orchestrator_helpers.py`


## Execution results

| Unit | File | Status | Agent | Attempts | Rollback |
|---|---|---|---|---|---|
| U1 | `tests/test_orchestrator_helpers.py` | passed | claude | 1 | `rm tests/test_orchestrator_helpers.py` |
