"""Unit tests for bridge/maid_common.py: model discovery, the streaming parser and
the quota status line.

Purpose
    Everything here is offline. LM Studio, agy and the filesystem are stubbed, so the
    tests run in milliseconds and need no model.

State mutations
    Temporary files only (tempfile), removed when each test ends. set_model() calls
    are undone in tearDown.

@rollback
    Nothing to undo. To remove this file, `git rm tests/test_maid_common.py`.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import maid_common  # noqa: E402

AGY_MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
    "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
)


class FakeResponse(io.BytesIO):
    """A urlopen() result: a context manager over bytes, iterable by line."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ModelSwitchingTest(unittest.TestCase):
    def setUp(self):
        self.saved = maid_common.get_models()

    def tearDown(self):
        for agent, name in self.saved.items():
            maid_common.set_model(agent, name)

    def test_switch_takes_effect_immediately_and_reaches_the_environment(self):
        maid_common.set_model("agy", "gemini-3.8-flash-low")
        self.assertEqual(maid_common.get_model("agy"), "gemini-3.8-flash-low")
        self.assertEqual(maid_common.os.environ["MAID_AGY_MODEL"], "gemini-3.8-flash-low")

    def test_claude_default_means_no_model_flag(self):
        maid_common.set_model("claude", "opus")
        self.assertEqual(maid_common.get_model("claude"), "opus")
        self.assertEqual(maid_common.set_model("claude", "default"), "")

    def test_rejects_unknown_agent_and_empty_local_model(self):
        with self.assertRaises(KeyError):
            maid_common.set_model("gpt", "x")
        with self.assertRaises(ValueError):
            maid_common.set_model("local", "")


class ModelDiscoveryTest(unittest.TestCase):
    def discover(self, urlopen, run):
        with mock.patch.object(maid_common.urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(maid_common.subprocess, "run", side_effect=run), \
                mock.patch.object(maid_common, "_read_json_file", return_value={"model": "claude-fable-5-2"}):
            return maid_common.discover_models(timeout=1)

    def test_parse_agy_models_keeps_ids_only(self):
        self.assertEqual(
            maid_common.parse_agy_models(AGY_MODELS_OUTPUT),
            ["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "claude-sonnet-4-6"],
        )
        self.assertEqual(maid_common.parse_agy_models("error: not signed in\n"), [])

    def test_all_three_sources(self):
        body = json.dumps({"data": [{"id": "qwen/qwen3.8-27b"}, {"id": "text-embedding-nomic-embed-text-v1.5"}]})

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, AGY_MODELS_OUTPUT, "")

        found = self.discover(lambda *a, **k: FakeResponse(body.encode()), run)
        self.assertEqual(found["local"]["models"], ["qwen/qwen3.8-27b"])
        self.assertEqual(found["local"]["error"], "")
        self.assertIn("gemini-3.8-flash-high", found["agy"]["models"])
        for alias in ("default", "sonnet", "opus", "haiku", "claude-fable-5-2"):
            self.assertIn(alias, found["claude"]["models"])
        self.assertEqual(len(found["claude"]["models"]), len(set(found["claude"]["models"])))

    def test_falls_back_when_everything_is_down(self):
        def urlopen(*args, **kwargs):
            raise OSError("connection refused")

        def run(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        found = self.discover(urlopen, run)
        self.assertIn("not reachable", found["local"]["error"])
        self.assertEqual(found["local"]["models"], [maid_common.get_model("local")])
        self.assertIn("not available", found["agy"]["error"])
        self.assertEqual(found["agy"]["models"], [maid_common.get_model("agy")])


class StreamingParserTest(unittest.TestCase):
    def test_parse_sse_line(self):
        self.assertEqual(maid_common.parse_sse_line(b'data: {"a": 1}\n'), {"a": 1})
        self.assertIs(maid_common.parse_sse_line("data: [DONE]"), maid_common.DONE)
        for ignored in ("", ": keep-alive", "event: message", "data: {broken", "data: [1, 2]"):
            self.assertIsNone(maid_common.parse_sse_line(ignored))

    def test_accumulator_rebuilds_reasoning_content_and_usage(self):
        accumulator = maid_common.StreamAccumulator()
        deltas = []
        for delta in (
            {"reasoning_content": "Let me "},
            {"reasoning_content": "think."},
            {"content": "Hello"},
            {"content": ", world"},
        ):
            deltas += accumulator.feed({"choices": [{"delta": delta}]})
        accumulator.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        accumulator.feed({"choices": [], "usage": {"completion_tokens": 7}})
        self.assertEqual(deltas, [("reasoning", "Let me "), ("reasoning", "think."), ("content", "Hello"), ("content", ", world")])
        message = accumulator.message()
        self.assertEqual((message["reasoning_content"], message["content"]), ("Let me think.", "Hello, world"))
        self.assertEqual((accumulator.finish_reason, accumulator.usage), ("stop", {"completion_tokens": 7}))

    def test_accumulator_joins_tool_call_fragments(self):
        accumulator = maid_common.StreamAccumulator()
        for call in (
            {"index": 0, "id": "c1", "function": {"name": "ask_agy", "arguments": ""}},
            {"index": 0, "function": {"arguments": '{"prompt": '}},
            {"index": 0, "function": {"arguments": '"hi"}'}},
            {"index": 1, "id": "c2", "function": {"name": "ask_claude", "arguments": "{}"}},
        ):
            accumulator.feed({"choices": [{"delta": {"tool_calls": [call]}}]})
        calls = accumulator.message()["tool_calls"]
        self.assertEqual([call["function"]["name"] for call in calls], ["ask_agy", "ask_claude"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"prompt": "hi"})
        self.assertEqual(calls[0]["id"], "c1")

    def test_lmstudio_chat_streams_chunks_to_listeners_and_the_log(self):
        lines = [
            'data: {"choices": [{"delta": {"reasoning_content": "hmm"}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "42"}}]}',
            'data: {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 10}}',
            "data: [DONE]",
        ]
        seen, sent = [], []

        def urlopen(request, timeout=None):
            sent.append(json.loads(request.data))
            return FakeResponse(("\n".join(lines) + "\n").encode())

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(maid_common, "LOG_DIR", Path(directory)), \
                mock.patch.object(maid_common, "TELEMETRY", maid_common.Telemetry(Path(directory) / "t.json")), \
                mock.patch.object(maid_common.urllib.request, "urlopen", side_effect=urlopen):
            message = maid_common.lmstudio_chat(
                mock.MagicMock(), [{"role": "user", "content": "q"}], stream=True, on_chunk=lambda kind, text: seen.append((kind, text))
            )
            stream_log = (Path(directory) / maid_common.STREAM_LOG_NAME).read_text()
            telemetry = maid_common.TELEMETRY.snapshot()
        self.assertTrue(sent[0]["stream"])
        self.assertEqual(message["content"], "42")
        self.assertEqual(seen, [("reasoning", "hmm"), ("content", "42")])
        self.assertIn("[reasoning] hmm", stream_log)
        self.assertIn("[content] 42", stream_log)
        self.assertEqual(telemetry["local"]["completion_tokens"], 10)


class StatusLineTest(unittest.TestCase):
    MODELS = {"local": "qwen/qwen3.8-27b", "agy": "gemini-3.8-flash-medium", "claude": ""}
    GIT = {"branch": "main", "added": 10, "deleted": 2}

    def telemetry(self, directory):
        return maid_common.Telemetry(Path(directory) / "telemetry.json")

    def test_empty_session_uses_clean_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(maid_common, "read_claude_rate_limits", return_value={}), \
                mock.patch.dict(maid_common.os.environ, {"MAID_AGY_QUOTA_EST": ""}):
            line = maid_common.format_status_line(self.telemetry(directory).snapshot(), self.MODELS, self.GIT)
        self.assertEqual(
            line,
            "◆ Local (qwen3.8-27b): ∞ UNLIMITED (0.0 tok/s, 0 tok) | ◆ Claude (default): 5h n/a 7d n/a ($0.00) | "
            "◆ agy (gemini-3.8-flash-medium): 0/? (idle) | 🌿 main (+10 -2) | 🔒 Sandbox: LOCKED",
        )

    def test_populated_session(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(maid_common, "read_claude_rate_limits", return_value={"five_hour_pct": 41.6, "seven_day_pct": 17}), \
                mock.patch.dict(maid_common.os.environ, {"MAID_AGY_QUOTA_EST": "200"}):
            telemetry = self.telemetry(directory)
            telemetry.record_local({"prompt_tokens": 1000, "completion_tokens": 500}, 50.0)
            telemetry.record_claude({"total_cost_usd": 0.178, "usage": {"input_tokens": 4, "output_tokens": 186}}, True)
            telemetry.record_agy(12.0, True, "done")
            telemetry.record_agy(8.0, False, "Error: RESOURCE_EXHAUSTED quota exceeded")
            snapshot = telemetry.snapshot()
            line = maid_common.format_status_line(snapshot, dict(self.MODELS, claude="opus"), self.GIT, sandbox="PENDING")
        self.assertIn("∞ UNLIMITED (10.0 tok/s, 1.5k tok)", line)
        self.assertIn("◆ Claude (opus): 5h 42% 7d 17% ($0.18)", line)
        self.assertIn("◆ agy (gemini-3.8-flash-medium): 2/200 (rate-limited)", line)
        self.assertTrue(line.endswith("🔒 Sandbox: PENDING"))
        self.assertEqual(snapshot["agy"]["avg_latency_s"], 10.0)
        self.assertEqual(snapshot["claude"]["output_tokens"], 186)

    def test_rate_limit_detection(self):
        self.assertTrue(maid_common.detect_rate_limit("HTTP 429 Too Many Requests"))
        self.assertTrue(maid_common.detect_rate_limit("You have hit your usage limit"))
        self.assertEqual(maid_common.detect_rate_limit("Created tests/test_x.py"), "")

    def test_claude_rate_file_is_optional_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rates.json"
            self.assertEqual(maid_common.read_claude_rate_limits(path), {})
            path.write_text('{"five_hour_pct": 42, "seven_day_pct": "lots", "other": 1}')
            self.assertEqual(maid_common.read_claude_rate_limits(path), {"five_hour_pct": 42})
            path.write_text("not json")
            self.assertEqual(maid_common.read_claude_rate_limits(path), {})


if __name__ == "__main__":
    unittest.main()
