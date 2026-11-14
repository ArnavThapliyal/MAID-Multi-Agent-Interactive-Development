"""MCP stdio server that exposes the LM Studio local model (Agent 0) to other agents.

Purpose
    Gives agy and Claude Code one tool, `ask_local_model`, which sends a prompt to
    the model served by LM Studio and returns its reply.

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
    One HTTP request to LM Studio per `ask_local_model` call.

@rollback
    Unregister with `agy mcp remove lmstudio`, and delete the "lmstudio" entry from
    .mcp.json for Claude Code. To revert this file,
    `git checkout -- bridge/lmstudio_mcp.py`. Standard library only, Python 3.9+.
"""
import json
import sys
import time
import uuid

from maid_common import get_maid_logger, get_model, lmstudio_chat, log_event

SERVER_INFO = {"name": "maid-lmstudio", "version": "0.2.0"}

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
    }
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
        if params.get("name") != "ask_local_model":
            raise ValueError("Unknown tool: %s" % params.get("name"))
        arguments = params.get("arguments") or {}
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
