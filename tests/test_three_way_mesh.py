"""Tests for the three-way mesh, the retry-after-rollback path and the review fallback.

Purpose
    * Offline, deterministic tests (always run): the real execute_unit() is driven
      through a failed attempt, a rollback from log/backups/ and a passing retry;
      review_diff() is driven down its fallback ladder with stubbed reviewers.
    * Live mesh tests (run only when MAID_LIVE_TESTS=1): Agent 0 -> agy,
      Agent 0 -> claude, and agy & claude -> Agent 0 through the MCP server.
      They need LM Studio, agy and claude and take a few minutes.

Usage
    python3 -m unittest discover -s tests
    MAID_LIVE_TESTS=1 python3 -m unittest tests.test_three_way_mesh -v
    python3 bridge/orchestrator.py --self-test      # same checks, as a table

State mutations
    The offline tests create and delete a temporary git repo and their own backup
    directories under log/backups/. They append to the log files under log/.

@rollback
    Nothing to undo; the tests clean up after themselves. To remove this file,
    `git rm tests/test_three_way_mesh.py`.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import orchestrator  # noqa: E402

LIVE = os.getenv("MAID_LIVE_TESTS") == "1"
UNIT = {"id": "U1", "file": "x.py", "symbol": "", "action": "modify", "description": "d"}


def cli_result(text, ok=True):
    """Build a run_cli()-shaped result for stubbing.

    @rollback: none (pure).
    """
    return {"ok": ok, "text": text, "returncode": 0 if ok else 1, "stdout": text, "stderr": "", "duration_s": 0.0}


class RetryAfterRollbackTest(unittest.TestCase):
    """Attempt 1 fails, is rolled back from the backup, and attempt 2 passes."""

    def test_failed_attempt_is_rolled_back_and_retried(self):
        ok, detail, checks = orchestrator.exercise_retry_rollback()
        for name, held in checks.items():
            self.assertTrue(held, name)
        self.assertTrue(ok, detail)
        self.assertEqual(len(checks), 6)


class ReviewFallbackTest(unittest.TestCase):
    """An unusable review escalates instead of passing silently."""

    def run_review(self, cli_results, audit):
        events = []
        with mock.patch.object(orchestrator, "run_cli", side_effect=cli_results) as run_cli, \
                mock.patch.object(orchestrator, "agent0_audit", return_value=audit) as agent0_audit, \
                mock.patch.object(orchestrator, "log_event", side_effect=lambda logger, event, *a, **k: events.append(event)):
            ok, detail = orchestrator.review_diff(Path("."), UNIT, "diff", "agy")
        return ok, detail, run_cli.call_count, agent0_audit.call_count, events.count("REVIEW_FALLBACK")

    def test_clear_verdict_needs_no_fallback(self):
        ok, _, cli_calls, audits, fallbacks = self.run_review([cli_result("VERDICT: PASS\nfine")], ("PASS", ""))
        self.assertEqual((ok, cli_calls, audits, fallbacks), (True, 1, 0, 0))

    def test_fail_verdict_fails(self):
        ok, _, cli_calls, _, fallbacks = self.run_review([cli_result("VERDICT: FAIL\nbug")], ("PASS", ""))
        self.assertEqual((ok, cli_calls, fallbacks), (False, 1, 0))

    def test_no_verdict_retries_the_reviewer_once(self):
        ok, _, cli_calls, audits, fallbacks = self.run_review(
            [cli_result("looks good to me"), cli_result("VERDICT: PASS")], ("FAIL", "")
        )
        self.assertEqual((ok, cli_calls, audits, fallbacks), (True, 2, 0, 1))

    def test_reviewer_error_falls_back_to_agent0_audit(self):
        ok, detail, cli_calls, audits, fallbacks = self.run_review(
            [cli_result("", ok=False), cli_result("", ok=False)], ("PASS", "matches the unit")
        )
        self.assertEqual((ok, cli_calls, audits, fallbacks), (True, 2, 1, 2))
        self.assertIn("agent 0 audit", detail)

    def test_agent0_audit_can_fail_the_unit(self):
        ok, _, _, audits, _ = self.run_review([cli_result("?"), cli_result("?")], ("FAIL", "wrong"))
        self.assertEqual((ok, audits), (False, 1))

    def test_no_verdict_anywhere_fails_instead_of_passing(self):
        ok, detail, cli_calls, audits, fallbacks = self.run_review(
            [cli_result("?"), cli_result("?")], (None, "unreachable")
        )
        self.assertEqual((ok, cli_calls, audits, fallbacks), (False, 2, 1, 3))
        self.assertIn("inconclusive", detail)


@unittest.skipUnless(LIVE, "live mesh tests need LM Studio, agy and claude; set MAID_LIVE_TESTS=1")
class LiveMeshTest(unittest.TestCase):
    """The three legs of the mesh against the real agents."""

    @classmethod
    def setUpClass(cls):
        orchestrator.new_correlation_id()
        rows = orchestrator.self_test(orchestrator.REPO_ROOT)
        orchestrator.print_self_test(rows)
        cls.rows = {row["leg"]: row for row in rows}

    def leg(self, prefix):
        return next(row for name, row in self.rows.items() if name.startswith(prefix))

    def test_leg1_agent0_dispatches_to_agy(self):
        self.assertTrue(self.leg("Leg 1")["ok"], self.leg("Leg 1")["detail"])

    def test_leg2_agent0_dispatches_review_to_claude(self):
        self.assertTrue(self.leg("Leg 2")["ok"], self.leg("Leg 2")["detail"])

    def test_leg3_workers_call_agent0_through_mcp(self):
        self.assertTrue(self.leg("Leg 3")["ok"], self.leg("Leg 3")["detail"])


if __name__ == "__main__":
    unittest.main()
