"""Tests for unit-level orchestrator behaviour: the scope check, pinned agents and
edits made to a unit before it is dispatched.

Purpose
    Offline and deterministic. Uses a throwaway git repo; Agent 0 is stubbed.

State mutations
    Temporary directories, and backup folders under log/backups/ that each test
    removes again.

@rollback
    Nothing to undo. To remove this file, `git rm tests/test_orchestrator_units.py`.
"""
import argparse
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import orchestrator  # noqa: E402
from maid_runtime import RunControl  # noqa: E402

ARGS = argparse.Namespace(test_cmd="", no_review=True, max_retries=0)


def make_unit(file, action="modify", **extra):
    unit = {
        "id": "U1", "file": file, "symbol": "", "action": action, "description": "do it", "signatures": [],
        "suggested_agent": "agy", "routing_reason": "", "acceptance": [],
    }
    unit.update(extra)
    return unit


class ScopeCheckTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.repo = Path(self._directory.name).resolve() / "repo"
        self.repo.mkdir()
        orchestrator.git(self.repo, "init", "-q")
        (self.repo / ".gitignore").write_text("*.log\n")
        (self.repo / "a.py").write_text("A = 1\n")
        orchestrator.git(self.repo, "add", ".")
        orchestrator.git(self.repo, "-c", "user.name=maid", "-c", "user.email=maid@localhost", "commit", "-q", "-m", "init")
        orchestrator.new_correlation_id()
        self.backups = orchestrator.LOG_DIR / "backups" / orchestrator.get_correlation_id()

    def tearDown(self):
        shutil.rmtree(self.backups, ignore_errors=True)
        self._directory.cleanup()

    def verify(self, unit, worker):
        state = orchestrator.checkpoint(self.repo, unit, 1)
        worker()
        return orchestrator.verify_unit(self.repo, unit, state, "agy", ARGS)

    def test_ignoring_an_untracked_file_is_not_an_out_of_scope_change(self):
        """Regression: run c7e29fedfe4b failed U2 because doc/REPORT.md became git-ignored."""
        (self.repo / "doc").mkdir()
        (self.repo / "doc" / "REPORT.md").write_text("# report from an earlier unit\n")

        def worker():
            (self.repo / ".gitignore").write_text("*.log\ndoc/REPORT.md\n")

        passed, checks, touched = self.verify(make_unit(".gitignore"), worker)
        self.assertEqual(touched, [".gitignore"])
        self.assertTrue(passed, checks)
        self.assertEqual((self.repo / "doc" / "REPORT.md").read_text(), "# report from an earlier unit\n")

    def test_a_real_change_to_a_newly_ignored_file_is_still_caught(self):
        (self.repo / "notes.txt").write_text("mine\n")

        def worker():
            (self.repo / ".gitignore").write_text("*.log\nnotes.txt\n")
            (self.repo / "notes.txt").write_text("overwritten by the worker\n")

        passed, checks, touched = self.verify(make_unit(".gitignore"), worker)
        self.assertFalse(passed)
        self.assertEqual(touched, [".gitignore", "notes.txt"])
        self.assertIn("notes.txt", checks[0]["detail"])

    def test_deleting_a_dirty_file_is_still_caught(self):
        (self.repo / "notes.txt").write_text("mine\n")

        def worker():
            (self.repo / "a.py").write_text("A = 2\n")
            (self.repo / "notes.txt").unlink()

        passed, _, touched = self.verify(make_unit("a.py"), worker)
        self.assertFalse(passed)
        self.assertEqual(touched, ["a.py", "notes.txt"])


class PinnedAgentTest(unittest.TestCase):
    PLAN = {"title": "t", "summary": "s"}

    def route(self, unit, reply):
        offered = []

        def agent0_chat(messages, purpose, tools=None, tool_choice=None):
            offered.extend(tool["function"]["name"] for tool in tools)
            return reply

        with mock.patch.object(orchestrator, "agent0_chat", side_effect=agent0_chat), \
                mock.patch.object(orchestrator, "log_event"):
            return orchestrator.route_unit(self.PLAN, unit, "a.py", ""), offered

    @staticmethod
    def call(name, prompt="write it"):
        return {"tool_calls": [{"function": {"name": name, "arguments": json.dumps({"prompt": prompt, "reason": "r"})}}]}

    def test_unpinned_unit_offers_both_tools(self):
        (agent, instructions, _), offered = self.route(make_unit("a.py"), self.call("ask_claude"))
        self.assertEqual((agent, instructions, offered), ("claude", "write it", ["ask_agy", "ask_claude"]))

    def test_pinned_unit_offers_only_that_agents_tool(self):
        (agent, _, _), offered = self.route(make_unit("a.py", pinned_agent="agy"), self.call("ask_agy"))
        self.assertEqual((agent, offered), ("agy", ["ask_agy"]))

    def test_pin_wins_even_if_agent0_calls_the_other_tool(self):
        (agent, instructions, reason), _ = self.route(make_unit("a.py", pinned_agent="claude"), self.call("ask_agy"))
        self.assertEqual((agent, instructions, reason), ("claude", "write it", "pinned by the user"))

    def test_pin_is_the_fallback_when_agent0_makes_no_tool_call(self):
        (agent, instructions, _), _ = self.route(make_unit("a.py", pinned_agent="claude"), {"tool_calls": []})
        self.assertEqual((agent, instructions), ("claude", "do it"))


class UnitEditTest(unittest.TestCase):
    def setUp(self):
        self.control = RunControl()
        self.control.set_units([make_unit("a.py"), dict(make_unit("b.py"), id="U2")])

    def card(self, unit_id):
        return next(unit for unit in self.control.snapshot()["units"] if unit["id"] == unit_id)

    def test_edit_description_and_pin_agent(self):
        self.control.edit_unit("U2", description="  use a dataclass ", pinned_agent="claude")
        self.assertEqual((self.card("U2")["description"], self.card("U2")["agent"]), ("use a dataclass", "claude"))
        self.assertEqual(self.control.take_unit_edits("U2"), {"description": "use a dataclass", "pinned_agent": "claude"})
        self.assertEqual(self.control.take_unit_edits("U2"), {})

    def test_skip_and_restore(self):
        self.control.edit_unit("U2", skip=True)
        self.assertEqual(self.card("U2")["status"], "skipped")
        self.control.edit_unit("U2", skip=False)
        self.assertEqual(self.card("U2")["status"], "pending")
        self.assertEqual(self.control.take_unit_edits("U2"), {"skip": False})

    def test_rejects_bad_edits(self):
        with self.assertRaises(KeyError):
            self.control.edit_unit("U9", skip=True)
        with self.assertRaises(ValueError):
            self.control.edit_unit("U1", description="   ")
        with self.assertRaises(ValueError):
            self.control.edit_unit("U1", pinned_agent="gpt")

    def test_a_started_unit_cannot_be_edited(self):
        self.control.update_unit("U1", column="in_progress", status="coding")
        with self.assertRaises(ValueError):
            self.control.edit_unit("U1", description="too late")
        self.assertEqual(self.control.take_unit_edits("U1"), {})


if __name__ == "__main__":
    unittest.main()
