# MAID: Multi-Agent Interactive Development

MAID is a local-first multi-agent setup for software development. A model running on your own machine acts as the brain: it clarifies the task, writes a spec, and routes each piece of work to one of two cloud coding agents, checking and rolling back their changes.

What runs today is the **three-agent bridge** in `bridge/`. The wider micro-services design (Headroom, OmniRoute, SearXNG, LangGraph, OpenHands, Roo Code) is still planned; see [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md).

## The three agents

| Agent | Runs as | Role |
|---|---|---|
| **Agent 0** | `qwen/qwen3.8-27b` (6-bit) on LM Studio, `localhost:1234` | The brain. Clarifies, plans, routes and never edits code itself. |
| **agy** | `agy -p` (Antigravity CLI), headless | Worker for fast scaffolding, boilerplate and web-grounded checks. |
| **claude** | `claude -p --output-format json` (Claude Code), headless | Worker for complex logic, refactoring and test verification. |

```
                        ┌──────────────────────────────┐
        task ─────────▶ │   Agent 0 (local, LM Studio) │ ◀──── clarifying answers
                        │   bridge/orchestrator.py     │
                        └───────┬──────────────┬───────┘
                        ask_agy │              │ ask_claude
                                ▼              ▼
                        ┌──────────────┐ ┌──────────────┐
                        │     agy      │ │    claude    │
                        └──────┬───────┘ └───────┬──────┘
                               │ ask_local_model │
                               └───────┬─────────┘
                                       ▼
                         bridge/lmstudio_mcp.py (MCP server)
                                       │
                                       ▼
                              Agent 0 (local model)
```

## Workflow

`bridge/orchestrator.py` runs four phases:

1. **Intake and clarification.** Agent 0 reads your task against the repository tree, works out what is missing, and asks you up to four targeted questions. Pass `--no-clarify` to skip this.
2. **Spec generation.** Agent 0 breaks the task into atomic units, each one file or one function, with required signatures and a rollback checkpoint. The spec is saved to `doc/tasks/<timestamp>_<slug>.md`.
3. **Routing and isolated execution.** For each unit, Agent 0 calls `ask_agy` or `ask_claude`. The orchestrator wraps every call with the repo map, the full spec and fixed isolation rules: the worker may create or modify only the assigned file and must include `@rollback` comments.
4. **Cross-verification and rollback.** Before a worker runs, the orchestrator records the git commit and backs up the affected files. Afterwards it checks that only the assigned file changed, that it parses, that your `--test-cmd` passes (if given), and has the *other* agent review the diff. A failed unit is rolled back automatically and retried; a passed unit comes with a one-command rollback.

## Repository layout

| Path | Contents |
|---|---|
| `bridge/orchestrator.py` | The four-phase orchestrator. |
| `bridge/lmstudio_mcp.py` | MCP server exposing `ask_local_model` to agy and Claude Code. |
| `bridge/maid_common.py` | Shared config, LM Studio call, git helpers. |
| `logg-template/LoggingTemplate.py` | Structured logging used by every script. |
| `doc/ARCHITECTURE.md` | Research findings and the planned architecture. |
| `doc/tasks/` | Task specs written by the orchestrator. |
| `log/` | Runtime logs and rollback backups (git-ignored). |

## Requirements

### Software

| Component | Version tested | Notes |
|---|---|---|
| macOS | 27.2.1 | The scripts are plain Python and should run on Linux or Windows, but only macOS is tested. |
| Python | 3.9+ | Tested on 3.9.6 (`/usr/bin/python3`) and 3.11. Standard library only; nothing to `pip install`. |
| git | any recent | The target repo must have at least one commit. |
| LM Studio | with the `lms` CLI | Serves the OpenAI-compatible API on `http://localhost:1234/v1`. |
| Antigravity CLI (`agy`) | 1.1.22 | Signed in, with MCP support (`agy mcp add`). |
| Claude Code (`claude`) | 2.1.311 | Signed in. |
| Local model | `qwen/qwen3.8-27b`, 6-bit, 22.81 GB | Must be trained for tool use. Loaded with a 132,000-token context. |
| agy model | `gemini-3.8-flash-medium` | Any model from `agy models`. |

### Hardware (the machine this was built and tested on)

| Part | Spec |
|---|---|
| Machine | MacBook Pro (Mac16,8) |
| Chip | Apple M4 Pro, 14 CPU cores (10 performance + 4 efficiency), 20 GPU cores |
| Memory | 48 GB unified |
| Disk | About 23 GB for the model |

The model is the only heavy part. `qwen/qwen3.8-27b` at 6-bit needs roughly 23 GB of memory for its weights plus more for context, so plan for 32 GB or more. On a smaller machine, set `MAID_LMSTUDIO_MODEL` to a smaller tool-capable model; smaller models are untested here.

## Setup

**1. Start LM Studio and load the model**

```bash
lms get qwen/qwen3.8-27b          # download (skip if you already have it)
lms load qwen/qwen3.8-27b --context-length 132000
lms server start                  # serves http://localhost:1234
curl http://localhost:1234/v1/models
```

**2. Check both worker CLIs**

```bash
agy -p "Reply with exactly: AGY_OK"
claude -p "Reply with exactly: CLAUDE_OK"
```

**3. Clone this repo**

```bash
git clone https://github.com/ArnavThapliyal/MAID-Multi-Agent-Interactive-Development.git
cd MAID-Multi-Agent-Interactive-Development
```

**4. Register the local model with agy**

```bash
agy mcp add lmstudio /usr/bin/python3 "$(pwd)/bridge/lmstudio_mcp.py"
agy mcp list
```

Headless agy denies MCP tools unless they are allowed in advance. Add this to `~/.gemini/antigravity-cli/settings.json`:

```json
"permissions": {
  "allow": ["mcp(lmstudio/ask_local_model)"]
}
```

**5. Register the local model with Claude Code**

```bash
claude mcp add lmstudio --scope local -- /usr/bin/python3 "$(pwd)/bridge/lmstudio_mcp.py"
claude mcp list
```

Headless Claude Code needs the same kind of allow rule. Add this to `~/.claude/settings.json`:

```json
"permissions": {
  "allow": ["mcp__lmstudio__ask_local_model"]
}
```

The repo's `.claude/settings.json` carries the same rule, but Claude Code only applies project settings once you have trusted the folder in an interactive session.

**6. Test both directions**

```bash
agy -p "Call the ask_local_model tool with the prompt 'What is 17*23?' and report its reply."
claude -p "Call the ask_local_model tool with the prompt 'What is 12*12?' and report its reply."
python3 bridge/orchestrator.py "your task" --plan-only
```

## Usage

```bash
python3 bridge/orchestrator.py "add a retry helper to bridge/maid_common.py"
```

| Flag | Effect |
|---|---|
| `--no-clarify` | Skip the Phase 1 questions. |
| `--plan-only` | Stop after writing the spec; run no workers. |
| `--no-review` | Skip the cross-agent diff review (scope, syntax and test checks still run). |
| `--test-cmd "pytest -q"` | Command to run after each unit; a non-zero exit fails the unit. |
| `--max-retries N` | Retries per unit after a rollback (default 1). |
| `--max-units N` | Most atomic units a plan may have (default 8). |
| `--repo PATH` | Work in another git repository (default: this one). |

Exit code is `0` when every unit passed, `1` when a unit failed (and was rolled back), `2` on a setup or planning error.

### Configuration

All settings are optional environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `MAID_LMSTUDIO_URL` | `http://localhost:1234/v1` | LM Studio base URL. |
| `MAID_LMSTUDIO_MODEL` | `qwen/qwen3.8-27b` | Agent 0 model. |
| `MAID_LMSTUDIO_TIMEOUT` | `900` | Seconds to wait for one local completion. |
| `MAID_LMSTUDIO_MAX_TOKENS` | `8000` | Token cap per local completion, reasoning included. |
| `MAID_LMSTUDIO_REASONING` | `low` | `reasoning_effort` sent to LM Studio; set empty to omit it. |
| `MAID_AGY_BIN` / `MAID_AGY_MODEL` / `MAID_AGY_TIMEOUT` | `agy` / `gemini-3.8-flash-medium` / `10m` | agy binary, model and `--print-timeout`. |
| `MAID_CLAUDE_BIN` / `MAID_CLAUDE_MODEL` | `claude` / account default | Claude Code binary and model. |
| `MAID_WORKER_TIMEOUT` | `900` | Hard limit in seconds for one worker CLI call. |
| `MAID_LOG_DIR` | `<repo>/log` | Where logs and backups go. |
| `MAID_LOG_LEVEL` / `MAID_LOG_FORMAT` | `DEBUG` / text | Log level; `JSON` makes the console output JSON too. |

For `lmstudio_mcp.py`, pass variables at registration, for example `agy mcp add --env MAID_LMSTUDIO_MODEL=... lmstudio ...`.

## Logs

Every script in `bridge/` writes each record twice:

| File | Format |
|---|---|
| `log/maid_execution.jsonl` | One JSON object per line. |
| `log/maid_human.log` | The same record as indented text. |

Each run has a correlation ID, which workers and the MCP server inherit, so one `grep` follows a task across all three agents. Records include the exact prompts sent, the agent selected and why, CLI flags, execution time, stdout and stderr, the verification checks, the diff, and the git commit before and after each worker call.

```bash
grep '"correlation_id": "a8df6b021d8f"' log/maid_execution.jsonl | python3 -m json.tool --json-lines
```

## Rollback

- **Failed units** are rolled back automatically before any retry.
- **Passed units** print a one-command rollback, also recorded in the spec's "Execution results" table and in the logs: `git checkout -- <file>` for a modified tracked file, `rm <file>` for a created file, or `cp <backup> <file>` when the file already had uncommitted changes.
- **Backups** of every file a worker could affect are kept under `log/backups/<correlation-id>/`.
- **Source files** each carry `@rollback` notes describing what they change and how to revert it.

The orchestrator never commits, stashes or pushes; committing passed work is left to you.

## Notes

- Prompts sent through `ask_agy` and `ask_claude` leave your machine and use your agy and Claude Code accounts. Prompts sent through `ask_local_model` stay local.
- Workers run with edit permission (`agy --mode accept-edits`, `claude --permission-mode acceptEdits`) in the target repo. The scope check catches and reverts edits outside the assigned file after the fact; it does not prevent them.
- Agent 0 is a reasoning model running locally, so planning takes minutes rather than seconds on the hardware above.
