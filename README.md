# MAID: Multi-Agent Interactive Development

MAID is a local-first multi-agent setup for software development. A model running on your own machine acts as the brain: it clarifies the task, writes a spec, and routes each piece of work to one of two cloud coding agents, checking and rolling back their changes.

What runs today is the **three-agent bridge** in `bridge/`, driven from the terminal or from a local web dashboard. The wider micro-services design (Headroom, OmniRoute, SearXNG, LangGraph, OpenHands, Roo Code) is still planned; see [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md).

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

A review that produces no verdict never passes silently. The reviewer is asked once more, then Agent 0 audits the diff locally, and if nobody returns a verdict the unit fails. Each step is logged as a `REVIEW_FALLBACK` event.

## Repository layout

| Path | Contents |
|---|---|
| `bridge/orchestrator.py` | The four-phase orchestrator and its `--self-test`. |
| `bridge/maid_ui.py` | The local web dashboard. |
| `bridge/maid_runtime.py` | Run control (pause, halt, steer, live state) and the sandbox permission gate. |
| `bridge/maid_common.py` | Config, model discovery and switching, the streaming LM Studio client, telemetry and the status line, git helpers. |
| `bridge/lmstudio_mcp.py` | MCP server exposing `ask_local_model` and `request_outside_access` to agy and Claude Code. |
| `tests/` | Unit tests (standard-library `unittest`). |
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
  "allow": [
    "mcp(lmstudio/ask_local_model)",
    "mcp(lmstudio/request_outside_access)"
  ]
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

That rule is only needed for your own headless `claude` sessions. The orchestrator does not depend on it: it passes `--allowedTools` for both MCP tools on every `claude` call it makes.

**6. Test both directions**

```bash
agy -p "Call the ask_local_model tool with the prompt 'What is 17*23?' and report its reply."
claude -p "Call the ask_local_model tool with the prompt 'What is 12*12?' and report its reply."
python3 bridge/orchestrator.py --self-test
```

`--self-test` prints a pass/fail table for the whole mesh: Agent 0 → agy, Agent 0 → claude, agy and claude → Agent 0 through the MCP server under one correlation ID, and a deterministic retry-after-rollback exercise. It takes about two minutes.

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
| `--repo PATH` | Work in another git repository (default: this one). Also the sandbox root. |
| `--local-model`, `--agy-model`, `--claude-model` | Model for each agent in this run. |
| `--list-models` | Print the models each agent can use, then exit. |
| `--self-test` | Verify the three-way mesh and the retry path, then exit. |

Exit code is `0` when every unit passed, `1` when a unit failed (and was rolled back), `2` on a setup or planning error, `130` when the run was halted.

## Dashboard

```bash
python3 bridge/maid_ui.py            # http://127.0.0.1:8765
python3 bridge/maid_ui.py --port 9000 --repo /path/to/another/repo
```

A single-page dashboard served by Python's `http.server`, with live updates over Server-Sent Events. No packages, no build step.

| Zone | What it shows |
|---|---|
| **Top bar** | Project selector, git branch with `+added -deleted`, a model dropdown per agent, the new-task input, and Agent 0's clarifying questions as answer cards. |
| **Kanban board** | Backlog → In Progress → Review → Done. One card per atomic unit with its ID, target file, assigned worker, verification checks, rollback command and a **Rollback Unit** button. Units from earlier specs in `doc/tasks/` appear as dimmed cards. |
| **Agent blocks** | Agent 0, agy and claude: live state (`idle`, `thinking`, `coding`, `reviewing`, `awaiting_permission`), active model, current target, elapsed time. |
| **Inspector** | Opens when you click an agent or a card. Tabs: **TERMINAL / STREAM** (Agent 0's tokens as they are generated, worker stdout/stderr), **GIT DIFF** (colourised diff of the unit's file and its rollback command), **MESSAGES** (exact prompts and raw responses), **TRACES** (records from `log/maid_execution.jsonl` for the run's correlation ID). Below the tabs: **Pause**, **Halt** and a **steer** box. |
| **Footer** | The status line. |

Controls:

- **Pause** takes effect between agent calls; a worker that is already running finishes first.
- **Halt** stops the worker in progress and rolls back the unit it was on.
- **Steer** queues text for the selected agent; it is injected into that agent's next prompt (next turn or retry).
- **Rollback Unit** works for units that passed in the current dashboard session and only while no run is active. For older units, run the command shown on the card.

The dashboard can start coding agents, so it binds to `127.0.0.1` only, rejects requests with a non-localhost `Host` header, and requires a per-process token on every state-changing request. Do not put it behind a proxy that exposes it to a network.

## Model selection

`discover_models()` asks each agent what it can run: LM Studio's `/v1/models` (or `lms ls` if the server is down), `agy models`, and for Claude Code the aliases `default`, `sonnet`, `opus`, `haiku` and `claude-fable-5-2` plus the model in `~/.claude/settings.json`. Claude Code has no command that lists models, so its list is static.

```bash
python3 bridge/orchestrator.py --list-models
python3 bridge/orchestrator.py "task" --agy-model gemini-3.8-flash-high --claude-model opus
```

Models are read at the moment of each call, so a switch from the dashboard dropdowns applies to the next model or worker call of a running task, with no restart. That is how you change models between units.

## Sandbox permission gate

Every agent is confined to the target repository (`--repo`). Workers start with the repository as their working directory and no other directories, and in headless mode the worker CLIs refuse to touch anything else. On top of that:

1. Every prompt Agent 0 writes for a worker is pre-scanned for paths that resolve outside the repository.
2. Workers are told to call the MCP tool `request_outside_access(target_path, reason, goal_impact)` if a unit cannot be done without an outside path.

Either way the run pauses with the agent in `awaiting_permission`, a `SANDBOX_PERMISSION_REQUEST` is logged, and you are shown the exact path, the reason and the goal impact, in the dashboard banner or in the terminal:

| Action | Effect |
|---|---|
| **Approve Once** | The directory is passed to the next worker call with `--add-dir`, then the grant is dropped. |
| **Approve for Session** | The directory stays granted until the orchestrator or dashboard process exits. |
| **Deny & Constrain to Repo** | Nothing is granted; the worker is told the path is off limits and to finish inside the repository. |

Limits to know: the pre-scan is a text heuristic, a grant covers the path's directory, and a grant approved while a worker is already running applies from the next worker call. A request that nobody answers within `MAID_SANDBOX_TIMEOUT` seconds is denied.

## Status line

Printed after each unit and at the end of a terminal run, and shown in the dashboard footer:

```
◆ Local (qwen3.8-27b): ∞ UNLIMITED (8.2 tok/s, 4.1k tok) | ◆ Claude (default): 5h n/a 7d n/a ($0.16) | ◆ agy (gemini-3.8-flash-medium): 4/? (ok) | 🌿 feat/three-agent-orchestrator (+10 -3) | 🔒 Sandbox: LOCKED
```

| Segment | Source |
|---|---|
| Local | Tokens per second and token count from LM Studio's responses. |
| Claude cost | `total_cost_usd` in the output of `claude -p --output-format json`, summed over the session. |
| Claude 5h / 7d | Only from a local file, `log/claude_rate_limits.json` (or `MAID_CLAUDE_RATE_FILE`), containing `{"five_hour_pct": 42, "seven_day_pct": 17}`. Without it the line shows `n/a`. You can have your own Claude Code status-line script write that file. |
| agy | Calls made today, counted by MAID in `log/telemetry.json`, over `MAID_AGY_QUOTA_EST` (`?` if unset), and `rate-limited` when agy's output mentions a rate limit. agy itself reports no quota. |
| Sandbox | `LOCKED`, or `PENDING` while a permission request is waiting. |

**No private APIs.** MAID calls no private or undocumented vendor endpoint and reads no credentials or tokens. The only network endpoint it calls itself is LM Studio on your machine; everything else goes through the `agy` and `claude` CLIs you are signed in to.

## Tests

```bash
python3 -m unittest discover -s tests                              # offline, about 3 seconds
MAID_LIVE_TESTS=1 python3 -m unittest tests.test_three_way_mesh -v # adds the live mesh legs
```

The offline suite covers model discovery, the streaming parser, sandbox path validation and the permission payload, the status line, the review fallback, the dashboard's HTTP layer, and a real retry-after-rollback run against a stub worker.

### Configuration

All settings are optional environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `MAID_LMSTUDIO_URL` | `http://localhost:1234/v1` | LM Studio base URL. |
| `MAID_LMSTUDIO_MODEL` | `qwen/qwen3.8-27b` | Agent 0 model. |
| `MAID_LMSTUDIO_TIMEOUT` | `900` | Seconds to wait for one local completion. |
| `MAID_LMSTUDIO_MAX_TOKENS` | `8000` | Token cap per local completion, reasoning included. |
| `MAID_LMSTUDIO_REASONING` | `low` | `reasoning_effort` sent to LM Studio; set empty to omit it. |
| `MAID_LMSTUDIO_STREAM` | on | Set `0` to turn off token streaming from LM Studio. |
| `MAID_SANDBOX_TIMEOUT` | `300` | Seconds a worker's outside-access request waits before it is denied. |
| `MAID_AGY_QUOTA_EST` | unset | Your estimate of agy's daily request quota, for the status line. |
| `MAID_CLAUDE_RATE_FILE` | `log/claude_rate_limits.json` | Optional file with Claude's 5h/7d usage percentages. |
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
| `log/lmstudio_stream.log` | Agent 0's raw token stream, reasoning and answer, as it was generated. |

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
- Workers run with edit permission (`agy --mode accept-edits`, `claude --permission-mode acceptEdits`) inside the target repo. The sandbox gate keeps them in the repo; within it, the scope check catches and reverts edits outside the assigned file after the fact rather than preventing them.
- Workers cannot run shell commands in headless mode. They write files; the orchestrator runs the syntax check and your `--test-cmd`.
- Do not edit files in the target repo while a unit is executing: the scope check cannot tell your edit from the worker's and will revert it.
- Agent 0 is a reasoning model running locally, so planning takes minutes rather than seconds on the hardware above.
