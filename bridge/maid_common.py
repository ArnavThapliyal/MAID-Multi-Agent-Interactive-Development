"""Shared helpers for the MAID bridge scripts.

Purpose
    Holds what bridge/orchestrator.py and bridge/lmstudio_mcp.py both need:
    configuration, the logging wiring, the LM Studio HTTP call and git helpers.

Environment
    MAID_LMSTUDIO_URL      LM Studio OpenAI-compatible base URL (default http://localhost:1234/v1)
    MAID_LMSTUDIO_MODEL    Agent 0 model (default qwen/qwen3.8-27b)
    MAID_LMSTUDIO_TIMEOUT  seconds to wait for one completion (default 900)
    MAID_LMSTUDIO_MAX_TOKENS  token cap per completion, reasoning included (default 8000)
    MAID_LMSTUDIO_REASONING   reasoning_effort sent to LM Studio; empty to omit (default low)

State mutations
    * Prepends <repo>/logg-template to sys.path so LoggingTemplate is importable.
    * Importing LoggingTemplate has the logging side effects documented there.

Side effects
    lmstudio_chat() sends an HTTP request to LM Studio. git() runs a git subprocess
    with exactly the arguments it is given; every caller in this package passes
    read-only commands except where its own docstring says otherwise.

@rollback
    This module persists nothing itself. To revert it,
    `git checkout -- bridge/maid_common.py`.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
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
)

LMSTUDIO_URL = os.getenv("MAID_LMSTUDIO_URL", "http://localhost:1234/v1")
LMSTUDIO_MODEL = os.getenv("MAID_LMSTUDIO_MODEL", "qwen/qwen3.8-27b")
LMSTUDIO_TIMEOUT = float(os.getenv("MAID_LMSTUDIO_TIMEOUT", "900"))
LMSTUDIO_MAX_TOKENS = int(os.getenv("MAID_LMSTUDIO_MAX_TOKENS", "8000"))
LMSTUDIO_REASONING = os.getenv("MAID_LMSTUDIO_REASONING", "low")

__all__ = [
    "REPO_ROOT",
    "LOG_DIR",
    "LMSTUDIO_URL",
    "LMSTUDIO_MODEL",
    "LMSTUDIO_TIMEOUT",
    "get_correlation_id",
    "get_maid_logger",
    "log_event",
    "new_correlation_id",
    "lmstudio_chat",
    "git",
    "git_head",
]


def lmstudio_chat(logger, messages, tools=None, tool_choice=None, purpose="chat"):
    """Send one chat completion to LM Studio and return the assistant message dict.

    Args:
        logger: logger from get_maid_logger(); the full request and reply are logged.
        messages: OpenAI-style message list.
        tools: optional OpenAI-style tool definitions.
        tool_choice: optional tool_choice value ("auto", "required", "none").
        purpose: short label stored with the log records.

    Returns:
        The `choices[0].message` dict (keys: content, tool_calls, reasoning_content).

    Raises:
        OSError (including urllib.error.URLError) if LM Studio is unreachable,
        ValueError/KeyError if the reply is not a valid completion.

    Side effects: one HTTP POST to LMSTUDIO_URL; two log records.
    @rollback: none needed; the call is stateless on the LM Studio side.
    """
    # Invariant: every completion is bounded, so a runaway reasoning loop cannot stall a run
    payload = {"model": LMSTUDIO_MODEL, "messages": messages, "max_tokens": LMSTUDIO_MAX_TOKENS}
    if LMSTUDIO_REASONING:
        payload["reasoning_effort"] = LMSTUDIO_REASONING
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    log_event(
        logger,
        "lmstudio_request",
        "agent 0 <- %s" % purpose,
        level=10,
        purpose=purpose,
        url=LMSTUDIO_URL,
        model=LMSTUDIO_MODEL,
        messages=messages,
        tool_choice=tool_choice,
    )
    request = urllib.request.Request(
        LMSTUDIO_URL + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=LMSTUDIO_TIMEOUT) as response:
        body = json.load(response)
    message = body["choices"][0]["message"]
    log_event(
        logger,
        "lmstudio_response",
        "agent 0 -> %s (%.1fs)" % (purpose, time.monotonic() - started),
        level=10,
        purpose=purpose,
        duration_s=round(time.monotonic() - started, 3),
        content=message.get("content"),
        reasoning=message.get("reasoning_content"),
        tool_calls=message.get("tool_calls"),
        usage=body.get("usage"),
    )
    return message


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
