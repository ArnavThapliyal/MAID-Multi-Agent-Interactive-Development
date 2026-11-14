"""Unit tests for the sandbox gate in bridge/maid_runtime.py.

Purpose
    Path validation (is_path_within_repo, find_outside_paths), the permission
    request payload, the gate's decisions and grants, pause/halt/steer on RunControl,
    and the file IPC used by the MCP tool `request_outside_access`.

State mutations
    Temporary directories only, removed when each test ends.

@rollback
    Nothing to undo. To remove this file, `git rm tests/test_sandbox.py`.
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import maid_runtime  # noqa: E402
from maid_runtime import RunControl, RunHalted, SandboxGate, find_outside_paths, is_path_within_repo, permission_payload  # noqa: E402


class ScriptedControl(RunControl):
    """A RunControl whose permission answers are scripted, recording what it was asked."""

    def __init__(self, decisions):
        super().__init__()
        self.decisions = list(decisions)
        self.asked = []
        self.state_while_asking = []

    def ask_permission(self, payload):
        self.asked.append(payload)
        self.state_while_asking.append(self.snapshot())
        return self.decisions.pop(0)


class TempRepoCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("x")

    def tearDown(self):
        self._directory.cleanup()


class PathValidationTest(TempRepoCase):
    def test_inside_paths(self):
        for path in (self.repo, self.repo / "src" / "a.py", "src/a.py", "./new/file.py", "src/../README.md"):
            self.assertTrue(is_path_within_repo(path, self.repo), path)

    def test_outside_paths(self):
        for path in (self.outside, "/etc/passwd", "../outside/secret.txt", "src/../../outside", "~/.ssh/id_rsa", self.root):
            self.assertFalse(is_path_within_repo(path, self.repo), path)

    def test_sibling_with_same_prefix_is_outside(self):
        (self.root / "repo-evil").mkdir()
        self.assertFalse(is_path_within_repo(self.root / "repo-evil" / "x", self.repo))

    def test_symlink_escaping_the_repo_is_outside(self):
        os.symlink(str(self.outside), str(self.repo / "link"))
        self.assertFalse(is_path_within_repo("link/secret.txt", self.repo))

    def test_prescan_finds_only_outside_paths(self):
        text = (
            "Edit src/a.py and bridge/x.py. Copy %s into the repo, read ~/.ssh/config and ../outside/secret.txt. "
            "Call http://localhost:1234/v1/models and the /api/stream route."
        ) % (self.outside / "secret.txt")
        self.assertEqual(
            find_outside_paths(text, self.repo),
            [str(self.outside / "secret.txt"), "~/.ssh/config", "../outside/secret.txt"],
        )
        self.assertEqual(find_outside_paths("Only touch src/a.py and %s/src/b.py." % self.repo, self.repo), [])


class PermissionPayloadTest(TempRepoCase):
    def test_payload_carries_the_three_fields_and_actions(self):
        payload = permission_payload(self.outside / "secret.txt", "need the fixture", "tests can run", "claude")
        self.assertEqual(payload["target_path"], str(self.outside / "secret.txt"))
        self.assertEqual((payload["reason"], payload["goal_impact"], payload["requester"]), ("need the fixture", "tests can run", "claude"))
        self.assertEqual([action["decision"] for action in payload["actions"]], ["approve_once", "approve_session", "deny"])
        self.assertEqual(
            [action["label"] for action in payload["actions"]],
            ["Approve Once", "Approve for Session", "Deny & Constrain to Repo"],
        )

    def test_missing_justification_is_made_explicit(self):
        payload = permission_payload("/tmp/x", "", None)
        self.assertEqual((payload["reason"], payload["goal_impact"]), ("(no reason given)", "(not stated)"))


class SandboxGateTest(TempRepoCase):
    def gate(self, *decisions):
        control = ScriptedControl(decisions)
        return SandboxGate(self.repo, control), control

    def test_inside_path_needs_no_permission(self):
        gate, control = self.gate()
        self.assertEqual(gate.request(self.repo / "src" / "a.py", "r", "g"), "not_needed")
        self.assertEqual(control.asked, [])

    def test_request_pauses_as_awaiting_permission_and_logs(self):
        gate, control = self.gate("deny")
        with mock.patch.object(maid_runtime, "log_event_safe") as log:
            decision = gate.request(self.outside / "secret.txt", "why", "impact", requester="agy", waiting_agent="agy")
        self.assertEqual(decision, "deny")
        during = control.state_while_asking[0]
        self.assertEqual(during["agents"]["agy"]["state"], "awaiting_permission")
        self.assertEqual(during["sandbox"], "PENDING")
        self.assertEqual(during["permission"]["reason"], "why")
        after = control.snapshot()
        self.assertEqual((after["agents"]["agy"]["state"], after["sandbox"], after["permission"]), ("idle", "LOCKED", None))
        self.assertEqual([call.args[0] for call in log.call_args_list], ["SANDBOX_PERMISSION_REQUEST", "SANDBOX_PERMISSION_DECISION"])

    def test_deny_is_remembered_and_grants_nothing(self):
        gate, control = self.gate("deny")
        self.assertEqual(gate.request(self.outside / "secret.txt", "r", "g"), "deny")
        self.assertEqual(gate.request(self.outside / "secret.txt", "r", "g"), "deny")
        self.assertEqual(len(control.asked), 1)
        self.assertEqual(gate.granted_dirs(), [])
        self.assertFalse(gate.is_allowed(self.outside / "secret.txt"))

    def test_approve_once_covers_one_worker_call(self):
        gate, control = self.gate("approve_once", "deny")
        self.assertEqual(gate.request(self.outside / "secret.txt", "r", "g"), "approve_once")
        self.assertEqual(gate.granted_dirs(), [str(self.outside)])
        self.assertTrue(gate.is_allowed(self.outside / "other.txt"))
        # A call that started before the approval could not use it, so the grant survives it
        gate.consume_once(used=[])
        self.assertEqual(gate.granted_dirs(), [str(self.outside)])
        gate.consume_once(used=gate.granted_dirs())
        self.assertEqual(gate.granted_dirs(), [])
        self.assertEqual(gate.request(self.outside / "secret.txt", "r", "g"), "deny")
        self.assertEqual(len(control.asked), 2)

    def test_approve_for_session_persists_without_asking_again(self):
        gate, control = self.gate("approve_session")
        self.assertEqual(gate.request(self.outside, "r", "g"), "approve_session")
        gate.consume_once()
        self.assertEqual(gate.request(self.outside / "secret.txt", "r", "g"), "approve_session")
        self.assertEqual(len(control.asked), 1)
        gate.revoke_all()
        self.assertFalse(gate.is_allowed(self.outside))

    def test_unknown_answer_is_treated_as_deny(self):
        gate, _ = self.gate("yes please")
        self.assertEqual(gate.request(self.outside, "r", "g"), "deny")

    def test_prescan_sorts_paths_into_approved_and_denied(self):
        gate, control = self.gate("approve_once", "deny")
        text = "Read %s then %s/x" % (self.outside / "secret.txt", self.root / "elsewhere")
        (self.root / "elsewhere").mkdir()
        approved, denied = gate.prescan(text, requester="agent0 -> agy")
        self.assertEqual(approved, [str(self.outside / "secret.txt")])
        self.assertEqual(denied, [str(self.root / "elsewhere" / "x")])
        self.assertEqual(control.asked[0]["requester"], "agent0 -> agy")


class SandboxIpcTest(TempRepoCase):
    """The MCP server files a request; the orchestrator's gate answers it."""

    def test_round_trip_and_timeout(self):
        with mock.patch.object(maid_runtime, "LOG_DIR", self.root / "log"), \
                mock.patch.object(maid_runtime, "SANDBOX_POLL_SECONDS", 0.02):
            control = ScriptedControl(["approve_session"])
            gate = SandboxGate(self.repo, control)
            payload = permission_payload(self.outside / "secret.txt", "why", "impact", "claude")
            answers = []
            worker = threading.Thread(target=lambda: answers.append(maid_runtime.submit_outside_request(payload, 5)))
            worker.start()
            for _ in range(200):
                if gate.serve_pending(waiting_agent="claude"):
                    break
                threading.Event().wait(0.02)
            worker.join(5)
            self.assertEqual(answers[0]["decision"], "approve_session")
            self.assertEqual(control.asked[0]["reason"], "why")
            self.assertEqual(maid_runtime.pending_outside_requests(), [])

            unanswered = permission_payload(self.outside, "r", "g")
            self.assertEqual(maid_runtime.submit_outside_request(unanswered, 0.1)["decision"], "deny")
            self.assertEqual(maid_runtime.pending_outside_requests(), [])


class RunControlTest(unittest.TestCase):
    def test_halt_raises_at_the_next_checkpoint(self):
        control = RunControl()
        control.checkpoint()
        control.halt()
        with self.assertRaises(RunHalted):
            control.checkpoint()

    def test_pause_blocks_until_resume(self):
        control = RunControl()
        control.pause()
        passed = threading.Event()
        worker = threading.Thread(target=lambda: (control.checkpoint(), passed.set()))
        worker.start()
        self.assertFalse(passed.wait(0.3))
        self.assertTrue(control.snapshot()["paused"])
        control.resume()
        self.assertTrue(passed.wait(2))
        worker.join(2)

    def test_steer_is_delivered_once_to_the_right_agent(self):
        control = RunControl()
        control.steer("claude", "  prefer dataclasses ")
        control.steer("claude", "")
        control.steer("nobody", "ignored")
        self.assertEqual(control.take_steer("agy"), [])
        self.assertEqual(control.take_steer("claude"), ["prefer dataclasses"])
        self.assertEqual(control.take_steer("claude"), [])

    def test_events_and_unit_cards(self):
        control = RunControl()
        events = []
        control.subscribe(lambda kind, payload: events.append(kind))
        control.set_units([{"id": "U1", "file": "a.py", "action": "create", "suggested_agent": "agy"}])
        control.update_unit("U1", column="done", status="passed", rollback="rm a.py")
        control.chunk("agent0", "reasoning", "thinking...")
        unit = control.snapshot()["units"][0]
        self.assertEqual((unit["column"], unit["status"], unit["rollback"]), ("done", "passed", "rm a.py"))
        self.assertEqual(control.streams["agent0"], "thinking...")
        self.assertEqual(events, ["state", "state", "chunk"])


if __name__ == "__main__":
    unittest.main()
