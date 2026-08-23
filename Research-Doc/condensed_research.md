# MAID (Multi-Agent Development Environment)

## Research Summary

The research surveys the open-source ecosystem for building a local, multi-agent software development environment. The core finding: no single, integrated solution exists. Powerful, specialized tools cover different parts of the stack, but a developer has to pick "best-in-class" components per layer and write significant custom "glue" code to make them work together.

The architecture settled on two complementary views of the same system:

1. **Micro-services view** — MAID as a set of small, parallel, low-overhead daemon processes rather than one monolithic app.
2. **Dual-proxy view** — how a single LLM request actually flows through that system on its way to a model.

---

## Micro-services Architecture

```
               ┌──────────────────────────────────────┐
               │         VSCodium / IDE Frontend      │
               │  (Cline / Roo Code / Custom Extension)│
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

Services communicate over fast local protocols (HTTP/gRPC, Unix sockets, or MCP) rather than being bundled into one process.

## Dual-Proxy Pipeline (LLM Traffic Path)

All LLM traffic is routed through **Headroom** for compression before it reaches **OmniRoute**, which maps the (now-compressed) request to whichever free API or local model is available.

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

**"Double Proxy" pattern:** MAID points its LLM requests at Headroom (local port). Headroom compresses prompts/tools and forwards the slimmed payload to OmniRoute (another local port), which routes to the cheapest/fastest available provider. This maximizes speed and minimizes cost.

---

## Core Components

### Frontend / IDE Client
- **Top pick:** Roo Code or Cline, run inside VSCodium.
- One source notes the official Roo Code extension was archived/shut down, and points to the community fork **ZooCode**, or the more widely-adopted **Cline** (formerly Claude Dev), as the practical successor.
- Serves as the lightweight UI bridge binding local backend services to the editor.
- **Other options:** `Continue.dev` for traditional pair-programming workflows; `Aider` for terminal-native workflows.

### Orchestrator — LangGraph
- Called the "unmatched industry standard" for production-grade multi-agent orchestration.
- Graph-based state machine gives deterministic control, durable state-saving (via SQLite/Postgres), and robust error handling — important for complex, long-running coding tasks.
- **Other options:** `CrewAI` (simpler, good for rapid prototyping); `AutoGen` and `MetaGPT` (more conversational or rigidly structured — considered less reliable for complex coding workflows).
- Design guidance: keep the orchestrator's own logic simple and stateless where possible; push heavy operations (terminal execution, browser actions) out to the sandbox rather than folding them into orchestrator state.

### API Gateway — OmniRoute
- Central model router and provider manager; a single, local OpenAI-compatible endpoint.
- Handles 330+ providers natively, avoiding the need to bundle multiple LLM SDKs directly into MAID.
- Runs locally as a small Node.js or Docker process — zero telemetry, built-in fallback handling.
- Typically runs on port `20128`.

### Context Optimizer — Headroom
- Token compressor and shared-memory layer; intercepts model requests and strips redundant logs, code boilerplate, and RAG noise.
- Claimed to strip 60–95% of JSON noise and ~20% of code tokens before they reach the LLM — keeps local performance snappy and stretches free-API rate limits further.

### Search — SearXNG
- Local, privacy-first web search service.
- Runs in a small local Docker container, giving agents web access without a paid search API.

### Autonomous Coding Agents / Sandbox
- **OpenHands** and **SWE-agent** are effective as asynchronous, background workers (e.g., fixing a bug in isolation) but are **poorly suited for direct integration into an interactive IDE loop** — they run their own monolithic loops/state, creating a "split-brain" conflict with the editor's state.
- In MAID, this is handled as an **asynchronous sandbox executor**: run in a parallel Docker container only when an agent needs to execute commands, keeping the main IDE workspace safe and isolated.
- For sandboxing itself: **Firecracker microVMs** (as used by E2B) offer the highest security; a heavily locked-down **Docker** container is the more pragmatic, accessible open-source approach.

---

## Default MAID Team Configuration

| Agent ID | Core Role | Default Model Target (OmniRoute) | Failover / Backup Strategy |
|---|---|---|---|
| **Agent 0** | The Local Anchor | Local LLM (e.g., `Ollama / Llama-3-8B`) | No internet needed. Always available as fallback code generator if APIs drop offline. |
| **Agent 1** | Primary API Agent | Gemini / DeepSeek (via OmniRoute free endpoint) | If rate-limited, OmniRoute dynamically routes to the next available free provider. |
| **Agent 2** | Secondary API Agent | Claude / GPT (via OmniRoute free endpoint) | Falls back to Agent 1's models or Agent 0 (local) if API endpoints fail. |
| **Agent 3** | Specialist/Search Agent | Fast LLM with Web Access (SearXNG) | Falls back to basic local processing if search or APIs are unavailable. |

---

## Key Design Decisions

- **MCP as the glue:** rather than writing custom APIs between each service, expose each parallel service (Roo Code/Cline, Headroom, OmniRoute) as an MCP server for standardized, fast service-to-service communication.
- **Avoid monolithic agent loops:** keep LangGraph logic simple and stateless; offload heavy operations to the Dockerized OpenHands sandbox as an external utility.
- **Guardrails against volatile free APIs** (since OmniRoute's free providers are added/removed/rate-limited constantly):
  - *Dynamic Model Pool Discovery* — query OmniRoute's `/v1/models` endpoint on startup to detect active free models and assign them to Agents 1–3.
  - *Failover Escalation Path* — if Agent 1's call fails, retry against Agent 2's model, then cascade to Agent 0 (local) as the reliable anchor.
  - *Local Compression (Headroom)* — running Headroom before OmniRoute cuts local token count, stretching free-tier rate limits further.

---

## Integration Gaps / Watch Out For

The core challenge isn't a lack of tools — it's the lack of integration between them. Building the recommended stack means building these from scratch:

- **IDE–Orchestrator Middleware:** a bridge connecting the TypeScript-based VS Code extension (frontend) to the Python-based LangGraph backend.
- **Synchronous Agent Tools:** the safe, effective code-editing tools proven out in SWE-agent aren't available out-of-the-box in LangGraph and need to be rebuilt.
- **Sandbox Lifecycle Manager:** a custom manager to programmatically spin up, manage, and tear down Docker sandboxes for code execution.
- **State Condensers and Stuck Detectors:** guardrails inside LangGraph to prevent infinite loops and manage context memory so the system doesn't crash.
- **Port Conflicts:** with OmniRoute, Headroom, SearXNG, and the orchestrator all running locally, port mappings need to be managed in a single config file.
- **Process Management:** as a collection of parallel services, MAID needs a lightweight process runner (docker-compose, or a CLI wrapper using Python's `supervisor` / Node's `PM2`) to start, monitor, and tear down everything with one command.

---