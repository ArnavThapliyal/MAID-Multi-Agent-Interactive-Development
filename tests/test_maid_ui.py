"""Unit tests for bridge/maid_ui.py: spec parsing, state trimming, the browser-driven
RunControl, and the HTTP layer's security checks.

Purpose
    Offline. A real server is started on an ephemeral loopback port against a
    throwaway git repo; no model or worker CLI is called.

State mutations
    Temporary directories only. The orchestrator's run driver and the model
    selection are restored after each test.

@rollback
    Nothing to undo. To remove this file, `git rm tests/test_maid_ui.py`.
"""
import json
import logging
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import maid_common  # noqa: E402
import maid_ui  # noqa: E402
import orchestrator  # noqa: E402
from maid_runtime import RunControl, SandboxGate  # noqa: E402

SPEC = """# Task spec: Add a retry helper

## Atomic units

### U1: modify `bridge/maid_common.py` → `retry`

Do it.

### U2: create `tests/test_retry.py` (whole file)

Test it.

### U3: create `doc/retry.md` (whole file)

## Execution results

| Unit | File | Status | Agent | Attempts | Rollback |
|---|---|---|---|---|---|
| U1 | `bridge/maid_common.py` | passed | claude | 2 | `git checkout -- bridge/maid_common.py` |
| U2 | `tests/test_retry.py` | failed | agy | 2 | `already rolled back automatically` |
| U3 | `doc/retry.md` | not run | | | |
"""


class PureHelpersTest(unittest.TestCase):
    def test_parse_spec_builds_cards_with_columns(self):
        cards = {card["id"]: card for card in maid_ui.parse_spec(SPEC, "20260101-000000_retry.md")}
        self.assertEqual(sorted(cards), ["U1", "U2", "U3"])
        self.assertEqual(
            (cards["U1"]["column"], cards["U1"]["agent"], cards["U1"]["attempt"], cards["U1"]["rollback"]),
            ("done", "claude", 2, "git checkout -- bridge/maid_common.py"),
        )
        self.assertEqual((cards["U2"]["column"], cards["U2"]["status"], cards["U2"]["rollback"]), ("backlog", "failed", ""))
        self.assertEqual((cards["U3"]["column"], cards["U3"]["status"]), ("backlog", "not run"))
        self.assertEqual(cards["U1"]["title"], "Add a retry helper")
        self.assertTrue(all(card["history"] for card in cards.values()))

    def test_spec_without_results_is_all_backlog(self):
        cards = maid_ui.parse_spec(SPEC.split("## Execution results")[0], "x.md")
        self.assertEqual({card["column"] for card in cards}, {"backlog"})

    def test_light_state_drops_bulky_fields(self):
        state = RunControl().snapshot()
        state["messages"] = [{"prompt": "x" * 9000}]
        state["units"] = [{"id": "U1", "diff": "+big"}, {"id": "U2", "diff": ""}]
        light = maid_ui.light_state(state)
        self.assertNotIn("messages", light)
        self.assertEqual(light["messages_count"], 1)
        self.assertEqual([(unit["diff"], unit["has_diff"]) for unit in light["units"]], [("", True), ("", False)])
        self.assertEqual(state["units"][0]["diff"], "+big")

    def test_summarise_record_caps_fields(self):
        record = maid_ui.summarise_record({"event": "e", "data": {"prompt": "p" * 9000, "flags": ["--x"], "n": 3}})
        self.assertLess(len(record["data"]["prompt"]), 4100)
        self.assertIn("more chars", record["data"]["prompt"])
        self.assertEqual((record["data"]["flags"], record["data"]["n"]), ('["--x"]', "3"))


class UIControlTest(unittest.TestCase):
    def setUp(self):
        self.control = maid_ui.UIControl(maid_ui.EventBus())

    def wait_for(self, predicate):
        for _ in range(200):
            if predicate(self.control.snapshot()):
                return True
            threading.Event().wait(0.01)
        return False

    def test_questions_wait_for_the_browser(self):
        answers = []
        worker = threading.Thread(target=lambda: answers.append(self.control.ask_questions(["Which file?", "Which style?"])))
        worker.start()
        self.assertTrue(self.wait_for(lambda state: state["questions"] == ["Which file?", "Which style?"]))
        self.assertEqual(self.control.snapshot()["run"]["status"], "awaiting_answers")
        self.control.submit_answers(["a.py", ""])
        worker.join(2)
        self.assertEqual(answers, [["a.py", ""]])
        self.assertIsNone(self.control.snapshot()["questions"])

    def test_permission_waits_for_a_matching_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            repo.mkdir()
            gate = SandboxGate(repo, self.control)
            decisions = []
            worker = threading.Thread(
                target=lambda: decisions.append(gate.request(Path(directory).resolve() / "elsewhere", "why", "impact", "claude", "claude"))
            )
            worker.start()
            self.assertTrue(self.wait_for(lambda state: state["permission"] is not None))
            pending = self.control.snapshot()
            self.assertEqual(pending["agents"]["claude"]["state"], "awaiting_permission")
            self.assertFalse(self.control.submit_permission("wrong-id", "approve_once"))
            self.assertFalse(self.control.submit_permission(pending["permission"]["id"], "maybe"))
            self.assertTrue(self.control.submit_permission(pending["permission"]["id"], "approve_session"))
            worker.join(2)
            self.assertEqual(decisions, ["approve_session"])
            self.assertEqual(self.control.snapshot()["sandbox"], "LOCKED")

    def test_halt_releases_pending_prompts_safely(self):
        results = []
        worker = threading.Thread(target=lambda: results.append(self.control.ask_permission({"id": "x"})))
        worker.start()
        self.control.halt()
        worker.join(2)
        self.assertEqual(results, ["deny"])


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_control = orchestrator.control
        cls.saved_models = maid_common.get_models()
        cls.directory = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.directory.name).resolve() / "repo"
        (cls.repo / "doc" / "tasks").mkdir(parents=True)
        (cls.repo / "doc" / "tasks" / "20260101-000000_retry.md").write_text(SPEC)
        maid_common.git(cls.repo, "init", "-q")
        maid_common.git(cls.repo, "add", ".")
        maid_common.git(cls.repo, "-c", "user.name=maid", "-c", "user.email=maid@localhost", "commit", "-q", "-m", "init")
        cls.patches = [
            mock.patch.object(maid_ui, "PROJECTS_FILE", Path(cls.directory.name) / "projects.json"),
            mock.patch.object(maid_ui, "discover_models", return_value={
                agent: {"models": ["m1", "m2"], "current": "m1", "source": "test", "error": ""} for agent in ("local", "agy", "claude")
            }),
        ]
        for patch in cls.patches:
            patch.start()
        cls.app = maid_ui.App(cls.repo)
        maid_ui.Handler.app = cls.app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), maid_ui.Handler)
        cls.server.daemon_threads = True
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for patch in cls.patches:
            patch.stop()
        maid_root = logging.getLogger("maid")
        for handler in [h for h in maid_root.handlers if isinstance(h, maid_ui.TraceHandler)]:
            maid_root.removeHandler(handler)
        orchestrator.set_control(cls.saved_control)
        for agent, name in cls.saved_models.items():
            maid_common.set_model(agent, name)
        cls.directory.cleanup()

    def call(self, path, body=None, token=maid_ui.TOKEN, host=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-MAID-Token"] = token
        if host:
            headers["Host"] = host
        request = urllib.request.Request(
            self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers,
            method="GET" if body is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def test_page_is_served_with_the_token(self):
        status, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(maid_ui.TOKEN, body)
        self.assertNotIn("__TOKEN__", body)
        for zone in ('id="board"', 'id="deck"', 'id="drawer"', 'id="foot"', "TERMINAL / STREAM", "GIT DIFF", "MESSAGES", "TRACES"):
            self.assertIn(zone, body)

    def test_state_shape_and_history_cards(self):
        status, body = self.call("/api/state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(state["project"]["path"], str(self.repo))
        self.assertFalse(state["busy"])
        self.assertEqual(sorted(state["run"]["agents"]), ["agent0", "agy", "claude"])
        self.assertEqual([card["id"] for card in state["history"]], ["U1", "U2", "U3"])
        self.assertIn("🔒 Sandbox: LOCKED", state["status_line"])

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.call("/api/state", host="evil.example.com")[0], 403)
        self.assertEqual(self.call("/api/control", {"action": "halt"}, host="evil.example.com")[0], 403)

    def test_post_requires_the_token(self):
        self.assertEqual(self.call("/api/control", {"action": "pause"}, token="")[0], 403)
        self.assertEqual(self.call("/api/control", {"action": "pause"}, token="guess")[0], 403)
        self.assertFalse(self.app.control.snapshot()["paused"])

    def test_pause_and_resume(self):
        self.assertEqual(self.call("/api/control", {"action": "pause"})[0], 200)
        self.assertTrue(self.app.control.snapshot()["paused"])
        self.assertEqual(self.call("/api/control", {"action": "resume"})[0], 200)
        self.assertFalse(self.app.control.snapshot()["paused"])
        self.assertEqual(self.call("/api/control", {"action": "explode"})[0], 400)

    def test_model_switch_applies_without_restart(self):
        self.assertEqual(self.call("/api/models", {"agent": "agy", "model": "gemini-3.8-flash-low"})[0], 200)
        self.assertEqual(maid_common.get_model("agy"), "gemini-3.8-flash-low")
        self.assertEqual(self.app.control.snapshot()["agents"]["agy"]["model"], "gemini-3.8-flash-low")
        self.assertEqual(json.loads(self.call("/api/models")[1])["agy"]["current"], "gemini-3.8-flash-low")
        self.assertEqual(self.call("/api/models", {"agent": "gpt", "model": "x"})[0], 400)

    def test_steer_is_queued_for_the_agent(self):
        self.assertEqual(self.call("/api/steer", {"agent": "claude", "text": "keep it small"})[0], 200)
        self.assertEqual(self.app.control.take_steer("claude"), ["keep it small"])
        self.assertEqual(self.call("/api/steer", {"agent": "root", "text": "x"})[0], 400)

    def test_bad_requests_do_not_start_runs(self):
        self.assertEqual(self.call("/api/task", {"task": "   "})[0], 409)
        self.assertEqual(self.call("/api/project", {"path": "/definitely/not/a/repo"})[0], 409)
        self.assertEqual(self.call("/api/permission", {"id": "none", "decision": "approve_once"})[0], 409)
        self.assertEqual(self.call("/api/rollback", {"unit": "U99"})[0], 400)
        self.assertEqual(self.call("/api/nope", {})[0], 404)
        self.assertFalse(self.app.busy)


if __name__ == "__main__":
    unittest.main()
