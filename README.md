# MADE-Multi-Agent-Development-Environment


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


"Service","GitHub Repository","MAID Role (Micro-service Style)","Why it keeps MAID Lightweight"
"API Gateway","https://github.com/diegosouzapw/OmniRoute","Central Model Router & Provider Manager  . It acts as a single, local OpenAI-compatible endpoint  .","Zero Telemetry & Fallbacks: Instead of bundling multiple LLM SDKs into MAID, OmniRoute handles 330+ providers natively  . It runs locally as a tiny Node.js or Docker process  . "
"Context Optimizer","https://github.com/headroomlabs-ai/headroom","Token Compressor & Shared Memory. It intercepts model requests and strips out redundant logs, code boilerplate, and RAG noise  .","Huge Token Savings: Strips 60–95% of JSON noise and 20% of code tokens before they hit the LLM, keeping local performance snappy and API costs incredibly low  ."
"Search Engine","https://github.com/searxng/searxng","Local Web Search Service","It runs in a tiny local Docker container, giving your agents instant, privacy-first web access without needing heavy, paid search API integrations."
"Orchestrator","https://github.com/langchain-ai/langgraph","State Machine & Execution Brain","Instead of running a heavyweight multi-agent manager, use LangGraph locally to define clean, deterministic graph-states and lightweight agent transitions."
"Workspace","https://github.com/OpenHands/OpenHands","Asynchronous Sandbox Executor","Run this in a parallel Docker container only when the agent needs to run commands, keeping your main IDE workspace safe and isolated. "
"Frontend","https://github.com/RooCodeInc/Roo-Code","User Interaction / IDE Client","While the official extension was archived, the open-source architecture (using Cline or the community ZooCode fork) serves as the perfect lightweight UI bridge to bind your local backend services to the editor  ."


