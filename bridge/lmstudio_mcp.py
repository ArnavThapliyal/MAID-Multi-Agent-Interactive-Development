"""MCP stdio server that exposes the LM Studio local model (Agent 0) to other agents.

Purpose
    Gives agy and Claude Code two tools:
      * `ask_local_model`         sends a prompt to the model served by LM Studio and
                                  returns its reply.
      * `request_outside_access`  asks the user, through the orchestrator's sandbox
                                  gate, for access to a path outside the repository.
                                  It only works inside an orchestrated run; anywhere
                                  else it answers "deny".

Registration
    agy:          agy mcp add lmstudio /usr/bin/python3 /abs/path/bridge/lmstudio_mcp.py
    Claude Code:  the repo's .mcp.json registers it for sessions started in this repo.

Protocol
    JSON-RPC 2.0, one message per line on stdin/stdout (MCP stdio transport).
    Invariant: stdout carries protocol messages only, so this process logs to the
    files under log/ and never to the console.

Environment
    See bridge/maid_common.py (MAID_LMSTUDIO_URL, MAID_LMSTUDIO_MODEL,
    MAID_LMSTUDIO_TIMEOUT) and logg-template/LoggingTemplate.py.

State mutations
    None in the repository. Appends records to log/maid_execution.jsonl and
    log/maid_human.log.

Side effects
    One HTTP request to LM Studio per `ask_local_model` call. A
    `request_outside_access` call writes a request file under log/sandbox/ and waits
    (MAID_SANDBOX_TIMEOUT seconds, default 300) for the user's decision.

@rollback
    Unregister with `agy mcp remove lmstudio`, and delete the "lmstudio" entry from
    .mcp.json for Claude Code. To revert this file,
    `git checkout -- bridge/lmstudio_mcp.py`. Standard library only, Python 3.9+.
"""
import json
import os
import sys
import time
import uuid

from maid_common import get_maid_logger, get_model, lmstudio_chat, log_event
from maid_runtime import is_path_within_repo, permission_payload, submit_outside_request

SANDBOX_TIMEOUT = float(os.getenv("MAID_SANDBOX_TIMEOUT", "300"))

SERVER_INFO = {"name": "maid-lmstudio", "version": "0.3.0"}

TOOLS = [
    {
        "name": "ask_local_model",
        "description": (
            "Send a prompt to the MAID local model (Agent 0) running on LM Studio "
            "and return its reply. Runs fully offline; use it for delegated drafting, "
            "second opinions, or work that should not leave this machine."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The prompt to send."},
                "system": {"type": "string", "description": "Optional system prompt."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "request_outside_access",
        "description": (
            "Ask the user for permission to read, write or execute a path OUTSIDE the "
            "repository you are confined to. Blocks until the user answers. Returns "
            "approve_once, approve_session or deny. If denied, stay inside the repository."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "description": "The exact outside path you need."},
                "reason": {"type": "string", "description": "Why you need access to this path."},
                "goal_impact": {"type": "string", "description": "How access helps achieve the task goal."},
            },
            "required": ["target_path", "reason", "goal_impact"],
        },
    },
]

# console=None: stdout belongs to the MCP protocol
logger = get_maid_logger("lmstudio_mcp", console=None)


def ask_local_model(prompt, system=None):
    """Send `prompt` (with an optional system prompt) to LM Studio and return the reply text.

    Raises: OSError if LM Studio is unreachable; KeyError/ValueError on a malformed reply.
    Side effects: one HTTP request; the exact prompt and reply are logged.
    @rollback: none needed; nothing is persisted outside the logs.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    message = lmstudio_chat(logger, messages, purpose="mcp ask_local_model")
    return (message.get("content") or "").strip()


def request_outside_access(target_path, reason, goal_impact):
    """File a sandbox permission request with the orchestrator and return its answer as text.

    Outside an orchestrated run (no MAID_REPO_ROOT in the environment) there is nobody
    to ask, so the answer is "deny".

    State mutations: writes a request file under log/sandbox/<correlation-id>/.
    Side effects: blocks up to SANDBOX_TIMEOUT seconds; logs the request and decision.
    @rollback: none needed; an unanswered request grants nothing.
    """
    repo = os.getenv("MAID_REPO_ROOT")
    requester = os.getenv("MAID_AGENT", "worker")
    if not repo:
        decision = {"decision": "deny", "note": "No MAID orchestrator is running this session, so nobody can approve."}
    elif is_path_within_repo(target_path, repo):
        decision = {"decision": "not_needed", "note": "That path is inside the repository; no approval is needed."}
    else:
        payload = permission_payload(target_path, reason, goal_impact, requester)
        log_event(logger, "mcp_sandbox_request", "%s asks for %s" % (requester, payload["target_path"]), **payload)
        decision = submit_outside_request(payload, SANDBOX_TIMEOUT)
    log_event(logger, "mcp_sandbox_decision", "%s -> %s" % (target_path, decision["decision"]), target_path=target_path, **decision)
    return "decision: %s\n%s" % (decision["decision"], decision.get("note", ""))


def handle(message):
    """Return the JSON-RPC result for one request.

    Raises: ValueError for an unknown method or tool (reported as a protocol error).
    Side effects: `tools/call` runs ask_local_model(); every call is logged with its
        own call ID, duration and outcome.
    @rollback: none needed.
    """
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        arguments = params.get("arguments") or {}
        if params.get("name") == "request_outside_access":
            try:
                text = request_outside_access(arguments["target_path"], arguments.get("reason"), arguments.get("goal_impact"))
            except (KeyError, OSError, ValueError) as error:
                return {"content": [{"type": "text", "text": "decision: deny\nrequest failed: %r" % error}], "isError": True}
            return {"content": [{"type": "text", "text": text}]}
        if params.get("name") != "ask_local_model":
            raise ValueError("Unknown tool: %s" % params.get("name"))
        call_id = uuid.uuid4().hex[:8]
        started = time.monotonic()
        log_event(
            logger,
            "mcp_tool_call",
            "ask_local_model called",
            call_id=call_id,
            model=get_model("local"),
            prompt=arguments.get("prompt"),
            system=arguments.get("system"),
        )
        try:
            text = ask_local_model(arguments["prompt"], arguments.get("system"))
        except (KeyError, OSError, ValueError) as error:
            # Invariant: tool failures go back to the caller as a result, not a protocol error
            log_event(
                logger,
                "mcp_tool_error",
                "ask_local_model failed",
                level=40,
                call_id=call_id,
                duration_s=round(time.monotonic() - started, 3),
                error=repr(error),
            )
            return {
                "content": [{"type": "text", "text": "LM Studio call failed: %r" % error}],
                "isError": True,
            }
        log_event(
            logger,
            "mcp_tool_result",
            "ask_local_model answered",
            call_id=call_id,
            duration_s=round(time.monotonic() - started, 3),
            reply=text,
        )
        return {"content": [{"type": "text", "text": text}]}
    raise ValueError("Method not found: %s" % method)


def main():
    """Serve MCP requests from stdin until it closes.

    Side effects: writes one JSON-RPC response line to stdout per request.
    @rollback: none needed; the process holds no state between requests.
    """
    log_event(logger, "mcp_server_start", "lmstudio MCP server started", argv=sys.argv, model=get_model("local"))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as error:
            log_event(logger, "mcp_bad_message", "unparseable line ignored", level=30, line=line, error=repr(error))
            continue
        # Notifications carry no id and get no response
        if "id" not in message:
            continue
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            reply["result"] = handle(message)
        except ValueError as error:
            reply["error"] = {"code": -32601, "message": str(error)}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    log_event(logger, "mcp_server_stop", "lmstudio MCP server stopped")


if __name__ == "__main__":
    main()
