"""Shared helpers for the MAID bridge scripts.

Purpose
    Holds what every bridge script needs:
      * configuration and the logging wiring
      * the active model of each agent, switchable at runtime
      * model discovery for all three agents
      * the LM Studio chat client, with live SSE token streaming
      * telemetry and the quota status line
      * git helpers

Environment
    MAID_LMSTUDIO_URL         LM Studio OpenAI-compatible base URL (default http://localhost:1234/v1)
    MAID_LMSTUDIO_MODEL       Agent 0 model (default qwen/qwen3.8-27b)
    MAID_LMSTUDIO_TIMEOUT     seconds to wait for a completion, or between stream chunks (default 900)
    MAID_LMSTUDIO_MAX_TOKENS  token cap per completion, reasoning included (default 8000)
    MAID_LMSTUDIO_REASONING   reasoning_effort sent to LM Studio; empty to omit (default low)
    MAID_LMSTUDIO_STREAM      "0" disables SSE streaming (default on)
    MAID_AGY_MODEL            agy model (default gemini-3.8-flash-medium)
    MAID_CLAUDE_MODEL         Claude Code model; empty means the account default
    MAID_AGY_QUOTA_EST        your estimate of agy's daily request quota, for the status line
    MAID_CLAUDE_RATE_FILE     JSON file with Claude 5h/7d usage percentages (see read_claude_rate_limits)

Network and private-data policy
    The only network endpoint this module calls is the LM Studio server on this
    machine. It calls no private or undocumented vendor API: Claude cost and token
    figures come from the documented `claude -p --output-format json` output, and
    the Claude 5h/7d percentages are read only from a local file you choose to
    provide. Nothing here reads credentials or tokens.

State mutations
    * Prepends <repo>/logg-template to sys.path so LoggingTemplate is importable.
    * set_model() changes the active model in memory and in os.environ.
    * lmstudio_chat() appends to log/lmstudio_stream.log.
    * Telemetry persists agy's daily call count in log/telemetry.json.
    All files are under log/ and git-ignored.

@rollback
    Delete log/lmstudio_stream.log and log/telemetry.json to discard what was
    recorded. To revert this module, `git checkout -- bridge/maid_common.py`.
    Standard library only, Python 3.9+.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The logging template lives in a directory whose name is not a valid package name
sys.path.insert(0, str(REPO_ROOT / "logg-template"))
from LoggingTemplate import (  # noqa: E402
    LOG_DIR,
    get_correlation_id,
    get_maid_logger,
    log_event,
    new_correlation_id,
    set_correlation_id,
)

LMSTUDIO_URL = os.getenv("MAID_LMSTUDIO_URL", "http://localhost:1234/v1")
LMSTUDIO_TIMEOUT = float(os.getenv("MAID_LMSTUDIO_TIMEOUT", "900"))
LMSTUDIO_MAX_TOKENS = int(os.getenv("MAID_LMSTUDIO_MAX_TOKENS", "8000"))
LMSTUDIO_REASONING = os.getenv("MAID_LMSTUDIO_REASONING", "low")
LMSTUDIO_STREAM = os.getenv("MAID_LMSTUDIO_STREAM", "1") != "0"
STREAM_LOG_NAME = "lmstudio_stream.log"
TELEMETRY_NAME = "telemetry.json"
METRICS_LOG_NAME = "metrics.jsonl"
CALL_HISTORY = 300
SAMPLE_HISTORY = 900

DEFAULT_MODELS = {"local": "qwen/qwen3.8-27b", "agy": "gemini-3.8-flash-medium", "claude": ""}
MODEL_ENV = {"local": "MAID_LMSTUDIO_MODEL", "agy": "MAID_AGY_MODEL", "claude": "MAID_CLAUDE_MODEL"}
CLAUDE_MODEL_ALIASES = ["default", "sonnet", "opus", "haiku", "claude-fable-5-2"]
DONE = object()

# Kept for callers that only need the startup value; use get_model("local") for the live one
LMSTUDIO_MODEL = os.getenv(MODEL_ENV["local"], DEFAULT_MODELS["local"])

__all__ = [
    "REPO_ROOT",
    "LOG_DIR",
    "LMSTUDIO_URL",
    "LMSTUDIO_MODEL",
    "LMSTUDIO_TIMEOUT",
    "DONE",
    "TELEMETRY",
    "ResourceSampler",
    "StreamAccumulator",
    "Telemetry",
    "add_stream_listener",
    "detect_rate_limit",
    "discover_models",
    "format_status_line",
    "get_correlation_id",
    "get_maid_logger",
    "get_model",
    "get_models",
    "git",
    "git_head",
    "git_summary",
    "lmstudio_chat",
    "log_event",
    "new_correlation_id",
    "metrics_snapshot",
    "parse_agy_models",
    "parse_ps",
    "parse_sse_line",
    "parse_vm_stat",
    "read_claude_rate_limits",
    "set_correlation_id",
    "set_model",
    "status_line",
]


# --------------------------------------------------------------------------- #
# Active models (switchable at runtime)
# --------------------------------------------------------------------------- #

_models = {agent: os.getenv(MODEL_ENV[agent], DEFAULT_MODELS[agent]) for agent in DEFAULT_MODELS}
_models_lock = threading.Lock()


def get_model(agent):
    """Return the model currently selected for "local", "agy" or "claude".

    An empty string for claude means "use the account default" (no --model flag).
    Every call site reads this at call time, so a switch takes effect on the next
    model or worker call without a restart.

    @rollback: none (pure read).
    """
    with _models_lock:
        return _models[agent]


def get_models():
    """Return a copy of the agent -> model mapping.

    @rollback: none (pure read).
    """
    with _models_lock:
        return dict(_models)


def set_model(agent, name):
    """Switch an agent's model at runtime and return the new value.

    Args:
        agent: "local", "agy" or "claude".
        name: model identifier; "default" or "" resets claude to the account default.

    State mutations: updates the in-memory mapping and the matching MAID_*_MODEL
        environment variable, so child processes (the MCP server) follow the switch.
    Raises: KeyError for an unknown agent; ValueError for an empty local or agy model.
    @rollback: call again with the previous value from get_model().
    """
    name = str(name or "").strip()
    if agent == "claude" and name == "default":
        name = ""
    if agent not in _models:
        raise KeyError(agent)
    if not name and agent != "claude":
        raise ValueError("a model name is required for %s" % agent)
    with _models_lock:
        _models[agent] = name
    os.environ[MODEL_ENV[agent]] = name
    return name


# --------------------------------------------------------------------------- #
# Model discovery
# --------------------------------------------------------------------------- #


def parse_agy_models(text):
    """Parse `agy models` output into a list of model ids.

    The CLI prints one `id<TAB>display name` line per model after a status line.

    @rollback: none (pure).
    """
    models = []
    for line in (text or "").splitlines():
        if "\t" not in line:
            continue
        model_id = line.split("\t", 1)[0].strip()
        if model_id and " " not in model_id and model_id not in models:
            models.append(model_id)
    return models


def _read_json_file(path):
    """Return the parsed JSON object in `path`, or {} if it is missing or invalid.

    @rollback: none (read-only).
    """
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def discover_models(timeout=8):
    """Find the models each agent can use right now.

    Sources:
      * local   GET <LM Studio>/models; if the server is down, `lms ls --json`.
                Embedding models are left out.
      * agy     `agy models`; if that fails, only the current model is offered.
      * claude  the Claude Code aliases plus the model set in ~/.claude/settings.json.
                Claude Code has no command that lists models, so this list is static.

    Returns: {"local"|"agy"|"claude": {"models": [...], "current": str,
        "source": str, "error": str}}. Never raises; a failed source is reported
        in "error" and falls back to the current model.

    Side effects: one HTTP GET to LM Studio, up to two short subprocesses, and two
        reads of local settings files (model name only).
    @rollback: none (read-only).
    """
    result = {}

    local = {"models": [], "current": get_model("local"), "source": "lmstudio /v1/models", "error": ""}
    try:
        with urllib.request.urlopen(LMSTUDIO_URL + "/models", timeout=timeout) as response:
            entries = json.load(response).get("data", [])
        local["models"] = [entry["id"] for entry in entries if "embed" not in entry.get("id", "").lower()]
    except (OSError, ValueError, KeyError) as error:
        local["error"] = "LM Studio not reachable: %s" % error
        lms = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms")
        try:
            listing = subprocess.run([lms, "ls", "--json"], capture_output=True, text=True, timeout=timeout)
            local["models"] = [entry["modelKey"] for entry in json.loads(listing.stdout) if entry.get("type") == "llm"]
            local["source"] = "lms ls"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
    result["local"] = local

    agy = {"models": [], "current": get_model("agy"), "source": "agy models", "error": ""}
    try:
        listing = subprocess.run(
            [os.getenv("MAID_AGY_BIN", "agy"), "models"], capture_output=True, text=True, timeout=max(timeout, 20)
        )
        agy["models"] = parse_agy_models(listing.stdout)
        if not agy["models"]:
            agy["error"] = (listing.stderr or listing.stdout).strip()[:200] or "agy listed no models"
    except (OSError, subprocess.SubprocessError) as error:
        agy["error"] = "agy not available: %s" % error
    result["agy"] = agy

    claude = {"models": list(CLAUDE_MODEL_ALIASES), "current": get_model("claude") or "default", "source": "static aliases", "error": ""}
    configured = str(_read_json_file(Path.home() / ".claude" / "settings.json").get("model") or "")
    if configured:
        claude["source"] = "static aliases + ~/.claude/settings.json"
        claude["models"].append(configured)
    result["claude"] = claude

    for info in result.values():
        # Invariant: the current model is always selectable, even when discovery failed
        if info["current"] and info["current"] not in info["models"]:
            info["models"].insert(0, info["current"])
        info["models"] = list(dict.fromkeys(info["models"]))
    return result


# --------------------------------------------------------------------------- #
# LM Studio streaming
# --------------------------------------------------------------------------- #

_stream_listeners = []
_stream_log_lock = threading.Lock()


def add_stream_listener(listener):
    """Register `listener(kind, text)` to receive every streamed chunk.

    `kind` is "reasoning", "content" or "tool_call". Used by the orchestrator to
    forward tokens to the UI. A listener that raises is ignored.

    State mutations: appends to the module-level listener list (idempotent).
    @rollback: `maid_common._stream_listeners.remove(listener)`.
    """
    if listener not in _stream_listeners:
        _stream_listeners.append(listener)


def parse_sse_line(line):
    """Parse one line of a Server-Sent Events stream from /v1/chat/completions.

    Returns: the decoded chunk dict for a `data: {...}` line, the DONE sentinel for
        `data: [DONE]`, or None for anything else (blank lines, comments, bad JSON).

    @rollback: none (pure).
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return DONE
    try:
        chunk = json.loads(data)
    except ValueError:
        return None
    return chunk if isinstance(chunk, dict) else None


class StreamAccumulator:
    """Rebuilds a complete assistant message from streamed chunks.

    feed() returns the text deltas of one chunk so they can be shown live;
    message() returns the same dict a non-streaming completion would have produced.

    State mutations: in-memory only.
    @rollback: discard the instance.
    """

    def __init__(self):
        self.content = ""
        self.reasoning = ""
        self.tool_calls = {}
        self.usage = None
        self.finish_reason = None

    def feed(self, chunk):
        """Apply one chunk; return its deltas as a list of (kind, text) pairs.

        @rollback: none needed.
        """
        deltas = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self.reasoning += reasoning
                deltas.append(("reasoning", reasoning))
            if isinstance(delta.get("content"), str) and delta["content"]:
                self.content += delta["content"]
                deltas.append(("content", delta["content"]))
            for call in delta.get("tool_calls") or []:
                slot = self.tool_calls.setdefault(
                    call.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                    deltas.append(("tool_call", "\n[tool call: %s] " % function["name"]))
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]
                    deltas.append(("tool_call", function["arguments"]))
        return deltas

    def message(self):
        """Return the accumulated assistant message.

        @rollback: none (pure read).
        """
        return {
            "role": "assistant",
            "content": self.content,
            "reasoning_content": self.reasoning,
            "tool_calls": [self.tool_calls[index] for index in sorted(self.tool_calls)],
        }


def _stream_log(text):
    """Append raw text to log/lmstudio_stream.log.

    State mutations: appends to the stream log.
    @rollback: delete the file.
    """
    with _stream_log_lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / STREAM_LOG_NAME).open("a", encoding="utf-8") as handle:
            handle.write(text)


def lmstudio_chat(logger, messages, tools=None, tool_choice=None, purpose="chat", stream=None, on_chunk=None):
    """Send one chat completion to LM Studio and return the assistant message dict.

    With streaming on (the default), tokens are read from the SSE stream as they are
    generated. Each chunk is appended to log/lmstudio_stream.log and passed to
    `on_chunk` and to every listener registered with add_stream_listener(), so the
    model's reasoning and answer can be watched live.

    Args:
        logger: logger from get_maid_logger(); the full request and reply are logged.
        messages: OpenAI-style message list.
        tools: optional OpenAI-style tool definitions.
        tool_choice: optional tool_choice value ("auto", "required", "none").
        purpose: short label stored with the log records.
        stream: True/False to force streaming on or off; None uses MAID_LMSTUDIO_STREAM.
        on_chunk: optional callable(kind, text) for this call only.

    Returns:
        The assistant message dict (keys: content, tool_calls, reasoning_content).

    Raises:
        OSError (including urllib.error.URLError) if LM Studio is unreachable,
        ValueError/KeyError if the reply is not a valid completion.

    Side effects: one HTTP POST to LM Studio on this machine; two log records;
        appends to the stream log; updates TELEMETRY.
    @rollback: none needed; the call is stateless on the LM Studio side.
    """
    model = get_model("local")
    streaming = LMSTUDIO_STREAM if stream is None else stream
    # Invariant: every completion is bounded, so a runaway reasoning loop cannot stall a run
    payload = {"model": model, "messages": messages, "max_tokens": LMSTUDIO_MAX_TOKENS}
    if LMSTUDIO_REASONING:
        payload["reasoning_effort"] = LMSTUDIO_REASONING
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    if streaming:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    log_event(
        logger,
        "lmstudio_request",
        "agent 0 <- %s" % purpose,
        level=10,
        purpose=purpose,
        url=LMSTUDIO_URL,
        model=model,
        stream=streaming,
        messages=messages,
        tool_choice=tool_choice,
    )
    request = urllib.request.Request(
        LMSTUDIO_URL + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    usage = None
    first_token_at = None
    with urllib.request.urlopen(request, timeout=LMSTUDIO_TIMEOUT) as response:
        if streaming:
            accumulator = StreamAccumulator()
            last_kind = None
            _stream_log(
                "\n\n=== %s | %s | %s | %s ===\n"
                % (datetime.now().astimezone().isoformat(timespec="seconds"), get_correlation_id(), model, purpose)
            )
            for raw in response:
                chunk = parse_sse_line(raw)
                if chunk is DONE:
                    break
                if chunk is None:
                    continue
                for kind, text in accumulator.feed(chunk):
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    if kind != last_kind:
                        _stream_log("\n[%s] " % kind)
                        last_kind = kind
                    _stream_log(text)
                    for listener in ([on_chunk] if on_chunk else []) + list(_stream_listeners):
                        try:
                            listener(kind, text)
                        except Exception:  # noqa: BLE001 - a broken listener must never stop a completion
                            pass
            message, usage = accumulator.message(), accumulator.usage
        else:
            body = json.load(response)
            message, usage = body["choices"][0]["message"], body.get("usage")
    duration = time.monotonic() - started
    ttft = first_token_at - started if first_token_at is not None else None
    TELEMETRY.record_local(usage, duration, model, ttft_s=ttft, purpose=purpose)
    log_event(
        logger,
        "lmstudio_response",
        "agent 0 -> %s (%.1fs)" % (purpose, duration),
        level=10,
        purpose=purpose,
        duration_s=round(duration, 3),
        ttft_s=round(ttft, 3) if ttft is not None else None,
        content=message.get("content"),
        reasoning=message.get("reasoning_content"),
        tool_calls=message.get("tool_calls"),
        usage=usage,
    )
    return message


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #


def git(repo, *args, check=True):
    """Run `git <args>` in `repo` and return the CompletedProcess (text mode).

    Args:
        repo: path of the repository to run in.
        *args: git arguments.
        check: raise CalledProcessError on a non-zero exit when True.

    Side effects: whatever the given git command does. Read-only commands
        (rev-parse, status, diff, ls-files, cat-file) change nothing.
    @rollback: depends on the command; callers that mutate state document it.
    """
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        check=check,
    )


def git_head(repo):
    """Return the current commit hash of `repo`, or "" if it has no commits.

    @rollback: none (read-only).
    """
    result = git(repo, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def git_summary(repo):
    """Return {"branch", "added", "deleted"} for the status line.

    Counts are lines changed in tracked files relative to HEAD. On any git error the
    branch is "?" and the counts are 0.

    @rollback: none (read-only).
    """
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    added = deleted = 0
    for line in git(repo, "diff", "--numstat", "HEAD", check=False).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            deleted += int(parts[1])
    return {"branch": branch.stdout.strip() if branch.returncode == 0 else "?", "added": added, "deleted": deleted}


# --------------------------------------------------------------------------- #
# Telemetry and the quota status line
# --------------------------------------------------------------------------- #

_RATE_LIMIT = re.compile(r"rate.?limit|quota|\b429\b|resource.?exhausted|too many requests|usage limit", re.IGNORECASE)


def detect_rate_limit(text):
    """Return the first rate-limit phrase found in a CLI's output, or "".

    @rollback: none (pure).
    """
    match = _RATE_LIMIT.search(text or "")
    return match.group(0) if match else ""


def read_claude_rate_limits(path=None):
    """Return Claude's 5-hour and 7-day usage percentages, if a local file provides them.

    MAID never calls a private Anthropic endpoint for these numbers. It only reads a
    JSON file that you can have your own Claude Code status-line script write:
        {"five_hour_pct": 42, "seven_day_pct": 17}
    The file defaults to log/claude_rate_limits.json (MAID_CLAUDE_RATE_FILE overrides).

    Returns: {"five_hour_pct": number, "seven_day_pct": number} with whichever keys
        are present and numeric; {} if the file is missing or unusable.

    @rollback: none (read-only).
    """
    data = _read_json_file(path or os.getenv("MAID_CLAUDE_RATE_FILE") or LOG_DIR / "claude_rate_limits.json")
    return {
        key: data[key]
        for key in ("five_hour_pct", "seven_day_pct")
        if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool)
    }


class Telemetry:
    """Session counters behind the status line.

    Local: completions, tokens and the latest tokens/second.
    Claude: calls, session cost and tokens, from `claude -p --output-format json`.
    agy: calls this session and today, latency, and whether its output mentioned a
        rate limit. agy reports no quota, so "today" is counted here and persisted.

    State mutations: in-memory counters; agy's daily count in log/telemetry.json.
    @rollback: delete log/telemetry.json to reset the daily count; `reset()` clears
        the session counters.
    """

    def __init__(self, path=None):
        self._lock = threading.Lock()
        self._path = Path(path) if path else LOG_DIR / TELEMETRY_NAME
        self.reset()

    def reset(self):
        """Clear the session counters (the persisted daily count is kept).

        @rollback: not reversible; the counters are diagnostics only.
        """
        with self._lock:
            self.local = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "tok_s": 0.0}
            self.claude = {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "status": "idle"}
            self.agy = {"calls": 0, "latency_s": 0.0, "total_latency_s": 0.0, "status": "idle", "note": ""}
            self.calls = deque(maxlen=CALL_HISTORY)

    def _agy_today(self, increment=0):
        """Read (and optionally bump) today's persisted agy call count.

        State mutations: rewrites log/telemetry.json when increment is non-zero.
        @rollback: delete the file.
        """
        today = date.today().isoformat()
        data = _read_json_file(self._path)
        count = data.get("agy_calls", 0) if data.get("date") == today else 0
        if increment:
            count += increment
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps({"date": today, "agy_calls": count}), encoding="utf-8")
            except OSError:
                pass
        return count

    def _call(self, agent, purpose, duration_s, ok=True, **extra):
        """Append one per-call record (caller holds the lock).

        @rollback: none needed; the history is bounded.
        """
        self.calls.append(dict(extra, at=time.time(), agent=agent, purpose=purpose, duration_s=round(duration_s, 3), ok=ok))

    def record_local(self, usage, duration_s, model="", ttft_s=None, purpose=""):
        """Record one LM Studio completion.

        Tokens/second is generation speed: completion tokens over the time after
        the first token arrived (or over the whole call if that is unknown).

        @rollback: none needed.
        """
        usage = usage or {}
        completion = usage.get("completion_tokens") or 0
        generating = duration_s - ttft_s if ttft_s is not None and duration_s > ttft_s else duration_s
        tok_s = completion / generating if completion and generating > 0 else 0.0
        with self._lock:
            self.local["calls"] += 1
            self.local["prompt_tokens"] += usage.get("prompt_tokens") or 0
            self.local["completion_tokens"] += completion
            if tok_s:
                self.local["tok_s"] = tok_s
            self._call(
                "local", purpose, duration_s, model=model, tok_s=round(tok_s, 2),
                ttft_s=round(ttft_s, 3) if ttft_s is not None else None,
                prompt_tokens=usage.get("prompt_tokens") or 0, completion_tokens=completion,
            )

    def record_claude(self, parsed, ok, duration_s=0.0, purpose=""):
        """Record one `claude -p --output-format json` result (cost and token usage).

        @rollback: none needed.
        """
        parsed = parsed if isinstance(parsed, dict) else {}
        usage = parsed.get("usage") or {}
        with self._lock:
            self.claude["calls"] += 1
            self.claude["cost_usd"] += parsed.get("total_cost_usd") or 0.0
            self.claude["input_tokens"] += (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
            self.claude["output_tokens"] += usage.get("output_tokens") or 0
            limited = detect_rate_limit(str(parsed.get("result") or "")) if not ok else ""
            self.claude["status"] = "rate-limited" if limited else ("ok" if ok else "error")
            self._call("claude", purpose, duration_s, ok, cost_usd=round(parsed.get("total_cost_usd") or 0.0, 4))

    def record_agy(self, duration_s, ok, output="", purpose=""):
        """Record one agy call: latency, outcome, and any rate-limit message.

        State mutations: bumps the persisted daily count.
        @rollback: none needed.
        """
        limited = detect_rate_limit(output) if not ok else ""
        with self._lock:
            self.agy["calls"] += 1
            self.agy["latency_s"] = duration_s
            self.agy["total_latency_s"] += duration_s
            self.agy["status"] = "rate-limited" if limited else ("ok" if ok else "error")
            self.agy["note"] = limited
            self._call("agy", purpose, duration_s, ok)
        self._agy_today(1)

    def snapshot(self):
        """Return the counters as one JSON-serialisable dict.

        @rollback: none (read-only apart from reading log/telemetry.json).
        """
        with self._lock:
            local, claude, agy = dict(self.local), dict(self.claude), dict(self.agy)
            calls = list(self.calls)
        local["last_ttft_s"] = next((call["ttft_s"] for call in reversed(calls) if call["agent"] == "local" and call.get("ttft_s") is not None), None)
        agy["calls_today"] = self._agy_today()
        agy["quota_est"] = os.getenv("MAID_AGY_QUOTA_EST", "")
        agy["avg_latency_s"] = round(agy["total_latency_s"] / agy["calls"], 1) if agy["calls"] else 0.0
        claude["rate_limits"] = read_claude_rate_limits()
        return {"local": local, "claude": claude, "agy": agy, "calls": calls}


TELEMETRY = Telemetry()


def _short_model(name):
    """Drop the publisher prefix from a model id for display (qwen/x -> x).

    @rollback: none (pure).
    """
    return (name or "default").split("/")[-1]


def _percent(value):
    return "%d%%" % round(value) if isinstance(value, (int, float)) else "n/a"


def format_status_line(telemetry, models, git_info, sandbox="LOCKED"):
    """Render the one-line quota and telemetry status bar.

    Args:
        telemetry: Telemetry.snapshot() output.
        models: get_models() output.
        git_info: git_summary() output.
        sandbox: "LOCKED" or "PENDING".

    Returns: e.g.
        ◆ Local (qwen3.8-27b): ∞ UNLIMITED (11.2 tok/s, 4.1k tok) | ◆ Claude (default):
        5h 42% 7d 17% ($0.18) | ◆ agy (gemini-3.8-flash-medium): 3/? (ok) | 🌿 main
        (+10 -2) | 🔒 Sandbox: LOCKED

    @rollback: none (pure).
    """
    local, claude, agy = telemetry["local"], telemetry["claude"], telemetry["agy"]
    tokens = local["prompt_tokens"] + local["completion_tokens"]
    token_text = "%.1fk tok" % (tokens / 1000.0) if tokens >= 1000 else "%d tok" % tokens
    limits = claude.get("rate_limits") or {}
    return " | ".join(
        [
            "◆ Local (%s): ∞ UNLIMITED (%.1f tok/s, %s)" % (_short_model(models.get("local")), local["tok_s"], token_text),
            "◆ Claude (%s): 5h %s 7d %s ($%.2f)"
            % (
                _short_model(models.get("claude")),
                _percent(limits.get("five_hour_pct")),
                _percent(limits.get("seven_day_pct")),
                claude["cost_usd"],
            ),
            "◆ agy (%s): %d/%s (%s)"
            % (_short_model(models.get("agy")), agy.get("calls_today", agy["calls"]), agy.get("quota_est") or "?", agy["status"]),
            "🌿 %s (+%d -%d)" % (git_info["branch"], git_info["added"], git_info["deleted"]),
            "🔒 Sandbox: %s" % sandbox,
        ]
    )


def status_line(repo, sandbox="LOCKED"):
    """Return the status line for the current session and `repo`.

    Side effects: read-only git queries.
    @rollback: none.
    """
    return format_status_line(TELEMETRY.snapshot(), get_models(), git_summary(repo), sandbox)


# --------------------------------------------------------------------------- #
# Resource sampling (live monitoring)
# --------------------------------------------------------------------------- #

PROCESS_GROUPS = ("lmstudio", "dashboard", "orchestrator", "agy", "claude")


def classify_process(command):
    """Map a process command line to one of PROCESS_GROUPS, or None if it is not ours.

    @rollback: none (pure).
    """
    lowered = command.lower()
    executable = command.split(" ", 1)[0].rsplit("/", 1)[-1].lower()
    if "lm studio" in lowered or "/.lmstudio/" in lowered or executable.startswith("lms"):
        return "lmstudio"
    if "maid_ui.py" in lowered:
        return "dashboard"
    if "orchestrator.py" in lowered or "lmstudio_mcp.py" in lowered or "bench.py" in lowered:
        return "orchestrator"
    # Only headless worker calls count; an interactive agy or Claude Code session is not MAID's
    if executable == "agy" and " --print " in command:
        return "agy"
    if executable == "claude" and " -p " in command:
        return "claude"
    return None


def parse_ps(text):
    """Sum CPU % and resident memory per process group from `ps -axo pcpu=,rss=,command=` output.

    Returns: {group: {"cpu": percent, "rss_mb": megabytes, "procs": count}} for every
        group in PROCESS_GROUPS (zeros when no process matched).

    @rollback: none (pure).
    """
    groups = {name: {"cpu": 0.0, "rss_mb": 0.0, "procs": 0} for name in PROCESS_GROUPS}
    for line in (text or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        group = classify_process(parts[2])
        if group is None:
            continue
        try:
            cpu, rss_kb = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        groups[group]["cpu"] = round(groups[group]["cpu"] + cpu, 1)
        groups[group]["rss_mb"] = round(groups[group]["rss_mb"] + rss_kb / 1024.0, 1)
        groups[group]["procs"] += 1
    return groups


def parse_vm_stat(text):
    """Turn macOS `vm_stat` output into megabytes: free, inactive, active, wired, compressed.

    Also returns "available_mb" (free + inactive). macOS keeps very little memory
    strictly free, so that sum is the figure that says how much room is left.

    Returns: {} if the output is not vm_stat's (for example on Linux).

    @rollback: none (pure).
    """
    page = re.search(r"page size of (\d+) bytes", text or "")
    if not page:
        return {}
    scale = int(page.group(1)) / (1024.0 * 1024.0)
    wanted = {
        "free_mb": "Pages free",
        "inactive_mb": "Pages inactive",
        "active_mb": "Pages active",
        "wired_mb": "Pages wired down",
        "compressed_mb": "Pages occupied by compressor",
    }
    memory = {}
    for key, label in wanted.items():
        match = re.search(r"%s:\s+(\d+)" % re.escape(label), text)
        if match:
            memory[key] = round(int(match.group(1)) * scale)
    if "free_mb" in memory:
        memory["available_mb"] = memory["free_mb"] + memory.get("inactive_mb", 0)
    return memory


class ResourceSampler(threading.Thread):
    """Samples CPU and memory of LM Studio, MAID and the worker CLIs in the background.

    Cost: one `ps` and one `vm_stat` subprocess per interval (2 seconds by default),
    a few milliseconds of CPU each. It never touches the model's request path.

    State mutations: keeps the last SAMPLE_HISTORY samples in memory and, when
        `log=True`, appends each one to log/metrics.jsonl (git-ignored).
    @rollback: `stop()` ends the thread; delete log/metrics.jsonl to discard samples.
    """

    def __init__(self, interval=2.0, log=True):
        super().__init__(daemon=True, name="maid-sampler")
        self.interval = interval
        self.log = log
        self.samples = deque(maxlen=SAMPLE_HISTORY)
        self._stop_event = threading.Event()

    def sample_once(self):
        """Take one sample and return it.

        Side effects: two short subprocesses; appends to the history (and the log).
        @rollback: none needed.
        """
        sample = {"at": round(time.time(), 2), "groups": parse_ps(""), "memory": {}, "load1": 0.0}
        try:
            listing = subprocess.run(["ps", "-axo", "pcpu=,rss=,command="], capture_output=True, text=True, timeout=5)
            sample["groups"] = parse_ps(listing.stdout)
            sample["memory"] = parse_vm_stat(
                subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            sample["load1"] = round(os.getloadavg()[0], 2)
        except OSError:
            pass
        sample["local_tok_s"] = round(TELEMETRY.local["tok_s"], 2)
        self.samples.append(sample)
        if self.log:
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                with (LOG_DIR / METRICS_LOG_NAME).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(sample) + "\n")
            except OSError:
                pass
        return sample

    def run(self):
        while not self._stop_event.wait(self.interval):
            self.sample_once()

    def stop(self):
        """Stop sampling.

        @rollback: start a new ResourceSampler.
        """
        self._stop_event.set()


def metrics_snapshot(sampler=None, samples=120):
    """Return telemetry, the per-call history and recent resource samples as one dict.

    @rollback: none (read-only).
    """
    telemetry = TELEMETRY.snapshot()
    return {
        "telemetry": {key: value for key, value in telemetry.items() if key != "calls"},
        "calls": telemetry["calls"],
        "samples": list(sampler.samples)[-samples:] if sampler else [],
    }
