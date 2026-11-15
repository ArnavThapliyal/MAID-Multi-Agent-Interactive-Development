# MAID Architecture

This document merges the research notes that used to live in `Research-Doc/` with the state of what is actually built. Sections are marked **Implemented** or **Planned**.

## 1. Research findings

The research surveyed open-source tools for a local, multi-agent development environment. Its main finding: strong specialised tools exist for each layer, but nothing integrates them, so the work is in the glue code.

| Layer | Recommended | Alternatives | Notes |
|---|---|---|---|
| Frontend (IDE) | Roo Code, or Cline / the ZooCode fork in VSCodium | Continue.dev, Aider | Roo Code's mode system (Architect, Coder, Debugger) suits multi-agent work. The official extension was archived, hence the forks. |
| Orchestration | LangGraph | CrewAI, AutoGen, MetaGPT | Graph-based state machine with durable state (SQLite/Postgres) and deterministic control. |
| Autonomous workers | OpenHands, SWE-agent | | Good as asynchronous background workers. Poor inside an interactive IDE loop, because their own loop and state conflict with the editor's. |
| Sandbox and web access | Custom Docker sandbox, SearXNG for search | Firecracker microVMs (E2B) | Firecracker is the most secure; a locked-down Docker container is the pragmatic choice. |

### What has to be built

- **IDE-orchestrator middleware**: a bridge between the TypeScript editor extension and the Python orchestrator.
- **Synchronous agent tools**: safe code-editing tools of the kind SWE-agent proved out.
- **Sandbox lifecycle manager**: start, manage and tear down sandboxes programmatically.
- **State condensers and stuck detectors**: guardrails against infinite loops and context overflow.

## 2. Target micro-services architecture (Planned)

MAID is meant to be a set of small local services rather than one process, talking over HTTP, Unix sockets or MCP.

```
               ┌──────────────────────────────────────┐
               │         VSCodium / IDE Frontend      │
               │ (Cline / Roo Code / Custom Extension)│
               └──────────────────┬───────────────────┘
                                  │ (MCP / JSON-RPC)
                                  ▼
               ┌──────────────────────────────────────┐
               │          MAID Orchestrator           │  ◄─── [SearXNG Service]
               │   (Lightweight LangGraph / Python)   │       (Web Search)
               └──────┬────────────────────────┬──────┘
                      │ (Local API)            │ (Local API)
                      ▼                        ▼
         ┌────────────────────────┐       ┌────────────────────────┐
         │     OmniRoute Service  │       │    Headroom Service    │
         │  (Unified API Gateway) │       │ (Context Compression)  │
         └────────────┬───────────┘       └────────────────────────┘
                      │
                      ▼
         ┌────────────────────────┐
         │    LLM Providers       │
         │ (Gemini, DeepSeek, etc)│
         └────────────────────────┘
```

| Service | Repository | Role | Why it keeps MAID lightweight |
|---|---|---|---|
| API Gateway | [OmniRoute](https://github.com/diegosouzapw/OmniRoute) | Central model router and provider manager behind one local OpenAI-compatible endpoint. | Handles 330+ providers and fallbacks itself, so MAID bundles no LLM SDKs. |
| Context Optimizer | [Headroom](https://github.com/headroomlabs-ai/headroom) | Token compressor and shared memory; strips redundant logs, boilerplate and RAG noise from requests. | Strips 60–95% of JSON noise and about 20% of code tokens before they reach the LLM. |
| Search Engine | [SearXNG](https://github.com/searxng/searxng) | Local web search service. | Runs in a small Docker container; no paid search API. |
| Orchestrator | [LangGraph](https://github.com/langchain-ai/langgraph) | State machine and execution brain. | Deterministic graph states instead of a heavyweight multi-agent manager. |
| Workspace | [OpenHands](https://github.com/OpenHands/OpenHands) | Asynchronous sandbox executor. | Runs in a parallel container only when an agent needs to execute commands. |
| Frontend | [Roo-Code](https://github.com/RooCodeInc/Roo-Code) | User interaction / IDE client. | Its open-source architecture (via Cline or ZooCode) bridges the local services to the editor. |

### Dual-proxy pipeline

All LLM traffic goes through Headroom for compression, then OmniRoute for routing.

```
       ┌────────────────────────────────────────────────────────┐
       │                 MAID orchestrator / IDE                │
       └─────────────────────────┬──────────────────────────────┘
                                 │
                     [1] User / Agent Prompt
                                 ▼
       ┌────────────────────────────────────────────────────────┐
       │                Headroom (Local Proxy)                  │
       │   * Compresses system prompts, tool schemas & files    │
       └─────────────────────────┬──────────────────────────────┘
                                 │
                     [2] Compressed Payload
                                 ▼
       ┌────────────────────────────────────────────────────────┐
       │               OmniRoute (Local Gateway)                │
       │   * Manages free API keys, fallback loops & local LLM  │
       └─────────┬──────────────────┬─────────────────┬─────────┘
                 │                  │                 │
                 ▼                  ▼                 ▼
         ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
         │   Agent 0    │   │   Agent 1    │   │  Agents 2-3  │
         │ (Local LLM)  │   │  (Free API)  │   │ (Free APIs)  │
         └──────────────┘   └──────────────┘   └──────────────┘
```

### Design decisions

- **MCP as the glue**: each service exposes an MCP server instead of a custom API.
- **Headroom in front of OmniRoute**: compression first stretches free-tier rate limits further.
- **No monolithic agent loops**: keep orchestrator logic simple; push terminal and browser work to the sandbox.

### Guardrails for volatile free APIs

- **Dynamic model pool discovery**: query OmniRoute's `/v1/models` on startup and assign whatever is live.
- **Failover escalation**: a failed call retries on the next agent's model and finally on the local model.

### Watch out for

- **Port conflicts**: OmniRoute (typically `20128`), Headroom, SearXNG and the orchestrator all bind local ports; keep the mapping in one config file.
- **Process management**: a single runner (docker-compose, supervisor or PM2) should start and stop every service.

## 3. Three-agent bridge (Implemented)

The part that runs today is `bridge/`. It does not yet use Headroom, OmniRoute, SearXNG, LangGraph or a container sandbox; its own sandbox gate (below) confines agents to the repository.

| Agent | Runs as | Role |
|---|---|---|
| Agent 0 | `qwen/qwen3.8-27b` on LM Studio (`localhost:1234`) | The brain: clarifies the task, writes the spec, routes each unit, never edits code itself. |
| agy | `agy -p` (Antigravity CLI) | Worker for scaffolding, boilerplate and web-grounded checks. |
| claude | `claude -p --output-format json` (Claude Code) | Worker for complex logic, refactoring and test verification. |

`bridge/orchestrator.py` runs four phases:

1. **Intake and clarification**: Agent 0 reads the task against the repo map and asks the user targeted questions (skipped with `--no-clarify`).
2. **Spec generation**: Agent 0 breaks the task into atomic units (one file or one function each) and the spec is saved to `doc/tasks/<timestamp>_<slug>.md`.
3. **Routing and isolated execution**: for each unit Agent 0 calls `ask_agy` or `ask_claude`. The orchestrator wraps every call with the repo map, the spec and the isolation rules.
4. **Cross-verification and rollback**: the orchestrator checkpoints git state and file contents before the worker runs, then checks scope, syntax, optional tests, and has the other agent review the diff. A failed unit is rolled back and retried.

`bridge/lmstudio_mcp.py` is the reverse path: an MCP server whose `ask_local_model` tool lets agy and Claude Code call the local model, and whose `request_outside_access` tool lets them ask for a path outside the repository.

Every script logs to `log/maid_execution.jsonl` and `log/maid_human.log` through `logg-template/LoggingTemplate.py`, tied together by a correlation ID per run.

### Modules

| Module | Responsibility |
|---|---|
| `orchestrator.py` | The four phases, worker CLI calls, checkpoint/verify/rollback, `--self-test`. |
| `maid_runtime.py` | `RunControl` (live state, pause/halt/steer, question and permission prompts) and `SandboxGate`. |
| `maid_common.py` | Config, model discovery and runtime switching, the streaming LM Studio client, telemetry and the status line. |
| `maid_ui.py` | The dashboard: `http.server`, REST endpoints, Server-Sent Events, card actions, metrics. |
| `lmstudio_mcp.py` | MCP stdio server for agy and Claude Code. |

### Run control

The orchestrator never talks to the user directly. It goes through one `RunControl` object:

```
            terminal                                  browser
               ▲                                         ▲
               │ input()/print()                         │ SSE + REST
        ┌──────┴───────┐                         ┌───────┴────────┐
        │  RunControl  │   (base class)          │   UIControl    │  (maid_ui.py)
        └──────┬───────┘                         └───────┬────────┘
               └──────────────────┬──────────────────────┘
                                  ▼
                        orchestrator.py (one run per process)
```

`RunControl` holds the live state the dashboard renders (agent states, unit cards, messages), the pause and halt flags, queued steering text, and the two prompts a run can raise: clarifying questions and sandbox permission requests. The dashboard runs the orchestrator in a worker thread and answers those prompts from HTTP requests.

### Model switching

`maid_common` keeps the active model of each agent in one place (`get_model` / `set_model`). Every call site reads it at call time, so a switch from the CLI flags or the dashboard applies to the next model or worker call. `discover_models()` lists what is available: LM Studio's `/v1/models`, `agy models`, and a static alias list for Claude Code.

### Streaming

`lmstudio_chat()` requests `stream: true` and reads the SSE stream, rebuilding content, reasoning and tool calls with `StreamAccumulator`. Each chunk goes to `log/lmstudio_stream.log` and to registered listeners; the orchestrator's listener forwards it to `RunControl.chunk()`, and from there to the dashboard's TERMINAL / STREAM tab. Worker CLIs run under `Popen` and their stdout and stderr are forwarded line by line the same way.

### Sandbox gate

```
 Agent 0 writes worker prompt ──▶ pre-scan for outside paths ──┐
                                                               ▼
 worker calls MCP request_outside_access ──▶ log/sandbox/<id>/req-*.json ──▶ SandboxGate.request()
                                                               │
                              awaiting_permission, SANDBOX_PERMISSION_REQUEST logged
                                                               ▼
                    user: Approve Once | Approve for Session | Deny & Constrain to Repo
                                                               │
              approved dir ──▶ `--add-dir` on the next worker call      denied ──▶ stated in the worker prompt
```

The lock itself is the worker CLIs' own behaviour: they run with the repository as working directory and, headless, refuse paths outside it. The gate is the only way a directory gets added. The MCP server is a grandchild of the orchestrator, so the two exchange requests and answers as files under `log/sandbox/<correlation-id>/`; a request nobody answers is denied.

### Scope check

Before a worker runs, `snapshot()` records the content hash of every dirty path; afterwards the same paths are hashed again, plus whatever is newly dirty. A path counts as touched only if its content changed. Hashing the earlier paths directly, rather than asking git again, matters when a worker edits `.gitignore`: a file that merely became ignored disappears from git's untracked list but is not a change.

### Unit edits

Until a unit starts, its card can be edited from the dashboard. `RunControl.edit_unit()` stores a new description, a pinned worker or a skip flag; the run loop takes those edits immediately before dispatching the unit. A pinned unit still goes through Agent 0, which writes the worker's instructions, but Agent 0 is offered only the pinned agent's tool.

### Monitoring

`lmstudio_chat()` records time to first token and generation speed for every call, and `Telemetry` keeps a bounded per-call history for all three agents. `ResourceSampler` runs one `ps` and one `vm_stat` every two seconds and groups CPU and memory by LM Studio, MAID and the headless worker calls. The dashboard reads both through `/api/metrics`; the benchmark suite uses the same classes.

The dashboard's own cost is kept low on purpose: `UIControl` coalesces stream chunks into one event per 100 ms, the page redraws a zone only when its data signature changes and appends to the stream pane instead of repainting it, and git and spec lookups are cached for three seconds.

### Benchmarks

`benchmarks/bench.py` measures six things: local speed, planning, routing, worker round trips, a fixed end-to-end task, and dashboard overhead (the same prompt with and without the dashboard attached, interleaved). It writes `system.json`, `metrics.json`, `samples.csv`, `summary.md` and screenshots to one folder per run; `benchmarks/compare.py` lines result folders up against a baseline.

### Review fallback

A reviewer that errors or returns no verdict is asked once more; then Agent 0 audits the diff locally; if there is still no verdict the unit fails. Each step logs `REVIEW_FALLBACK`.

### Telemetry and private APIs

The status line is built from data MAID already has: LM Studio's token usage, the cost and usage in `claude -p --output-format json`, and its own count of agy calls. MAID calls no private or undocumented vendor endpoint. Claude's 5-hour and 7-day percentages appear only if a local file provides them.

Setup and usage are in the [README](../README.md).
