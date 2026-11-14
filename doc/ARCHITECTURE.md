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

The part that runs today is `bridge/`. It does not yet use Headroom, OmniRoute, SearXNG, LangGraph or a sandbox.

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

`bridge/lmstudio_mcp.py` is the reverse path: an MCP server whose `ask_local_model` tool lets agy and Claude Code call the local model.

Every script logs to `log/maid_execution.jsonl` and `log/maid_human.log` through `logg-template/LoggingTemplate.py`, tied together by a correlation ID per run.

Setup and usage are in the [README](../README.md).
