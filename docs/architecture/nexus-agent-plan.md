# Nexus Agent — Claude Code Full Observation & Implementation Plan

## Context

Goal: Use Nexus kernel primitives as infrastructure, build a thin agent framework on top,
then implement Nexus Agent — more powerful and flexible than Claude Code.

This document exhaustively lists ALL observable Claude Code mechanisms (100% coverage).
Each item is annotated with:
- **Layer**: Infra / Framework / Agent
- **Priority**: P0 (core) / P1 (important) / P2 (enhancement)
- **Nexus mapping**: Exists / Needs enhancement / Needs building
- **CC status**: Production / Gated / Unreleased

---

## 1. Core Agent Loop [P0 — Detailed Design]

### 1.1 Main Loop [Production]

**CC**: `while stop_reason == "tool_use"` — 20 lines of core logic.

**Nexus**: `ManagedAgentLoop.run()` already has equivalent structure:
```python
while turns < self._max_turns:
    text, tool_calls, meta = await self._call_llm_via_kernel()
    if not tool_calls:
        return finish_turn("stop")
    for tc in tool_calls:
        tool_result = await self._execute_tool(tc)
    # persist + loop
```

**Gap & Action**: Structure is equivalent. Remaining work:
1. Implement tool_calls parsing (line 273 TODO) — see §1.4
2. Expand `_execute_tool()` to full tool registry — see §1.5
3. Add retry wrapper around `_call_llm_via_kernel()` — see §1.3

**Layer: Framework | Nexus: ManagedAgentLoop needs enhancement**

### 1.2 LLM API Call [Production]

**CC**: Anthropic SDK streaming, model selection, effort level, cost tracking.

**Nexus**:
- `CASOpenAIBackend.generate_streaming()` — pure compute, yields (token, metadata)
- `CASOpenAIBackend.start_streaming()` → DT_STREAM → agent reads tokens
- OpenAI-compatible API (SudoRouter can front any LLM)
- Backend configured via mount: `nexus mount /llm --backend=openai_compatible --config='{...}'`
- StreamManager injected at factory boot (`_wired.py:477-488`)

**Multi-provider strategy**:
- Current: OpenAI-compatible SDK only. Nova-gateway (SudoRouter) translates all LLMs to OpenAI format.
- Nova-gateway uses same pattern as LangChain: canonical format (OpenAI) + per-provider Adaptor interface (`ConvertOpenAIRequest()` / `DoResponse()`). Translation happens in the proxy (Go) not in-process.
- No native Anthropic SDK needed — nova-gateway handles Claude ↔ OpenAI format translation including tool_use ↔ tool_calls, message restructuring, and streaming format conversion.
- Plan: add `CASAnthropicBackend` — native Anthropic SDK, tool_use as complete JSON (no incremental argument concatenation), native streaming format. Nova-gateway already exposes `/v1/messages` for Anthropic-native passthrough.
- Benefit: eliminates translation overhead for Claude models, native extended_thinking support, native prompt caching (cache_control).

**Gap & Action**:
- Model selection: constructor accepts `model`, but no runtime switching. Add `/model` command.
- Effort level: map to API extra_params (temperature, etc.)
- Cost tracking: accumulate from `meta["usage"]` in observer.

**Layer: Framework | Nexus: exists, needs enhancement**

### 1.3 Retry & Error Handling [Production]

**CC**: `withRetry.ts` wrapping API calls.
- 429 (rate limit) + 5xx → exponential backoff (1s, 2s, 4s, 8s), max 5 retries
- Auth error → no retry, fail immediately
- Network error → retry
- Tool failure → return error string to model (no retry)

**Nexus plan**: Framework layer, not kernel:
```python
async def _call_llm_with_retry(self):
    for attempt in range(self._max_retries):
        try:
            return await self._call_llm_via_kernel()
        except RateLimitError:
            await asyncio.sleep(2 ** attempt)
        except AuthError:
            raise  # no retry
        except (ServerError, NetworkError):
            await asyncio.sleep(2 ** attempt)
    raise MaxRetriesExceeded()
```

**Layer: Framework | Needs building**

### 1.4 Tool Call Parsing [Production]

**CC**: Anthropic API returns ContentBlock array with `type: "tool_use"`, each containing `id`, `name`, `input` (complete JSON).

**Nexus**: Uses OpenAI-compatible streaming. Tool calls arrive incrementally:
```json
{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_xxx",
  "function": {"name": "read_file", "arguments": "{\"path\":"}}]}}]}
```
Arguments arrive across multiple chunks and must be concatenated.

**Implementation**: Modify `CASOpenAIBackend.generate_streaming()`:
1. Detect `delta.tool_calls` in addition to `delta.content`
2. Accumulate tool_call arguments across chunks (per-index concatenation)
3. On stream end, include complete tool_calls in the "done" control message:
   `{"type": "done", "tool_calls": [...], "finish_reason": "tool_calls", ...}`
4. ManagedAgentLoop reads tool_calls from `meta` in "done" message

**Files to modify**:
- `src/nexus/backends/compute/openai_compatible.py` — `generate_streaming()` add tool_call accumulation
- `src/nexus/services/agent_runtime/managed_loop.py` — extract tool_calls from meta, delete TODO

**Layer: Infra (backend) + Framework (loop) | Needs building**

### 1.5 Tool Registry & Execution [Production]

**CC Tool Interface**: `validateInput() → checkPermissions() → call()` + `isEnabled()`, `isConcurrencySafe()`, `isReadOnly()`, `isDestructive()`, `prompt()`.

**Nexus plan — two-tier tool model**:

**Tier A: Built-in kernel tools (eager, function-calling)**
Small set (~6) of kernel-level tools bound day-1 via function-calling schema:
- `read_file` → `sys_read(path)` (exists)
- `write_file` → `sys_write(path, content)` (exists)
- `edit_file` → `nx.edit(path, edits)` (exists, Tier-2 RPC, fuzzy match + OCC)
- `bash` → SubprocessRunner (new, DT_PIPE pattern from AcpService)
- `grep` → `SearchService.grep()` (exists, Rust-accelerated, all profiles)
- `glob` → `SearchService.glob()` (exists, Rust-accelerated, all profiles)

These are few enough (~6 × 200 tokens = 1.2K) to not dilute context.

**Tier B: External CLI tools (lazy, filesystem discovery)**
User provides tool paths. Tools are well-named CLI executables (e.g. built with cli-args-ssot).
LLM discovers tools on-demand via filesystem navigation:
```
System prompt: "External tools available at /tools/. Use ls and --help to discover."

LLM behavior:
  ls /tools/ → ai-dev-browser/, video-uploader/, gmail-processor/
  ls /tools/ai-dev-browser/tools/ → browser_click, browser_list, ...
  browser_click --help → usage + params
  bash browser_click --selector "#submit" --timeout 5000
```

Benefits over eager binding:
- Supports unlimited tools without context window explosion
- No context dilution (LLM only loads what it needs)
- No schema translation needed — tools are self-describing via --help
- Exceeds CC's approach (CC eager-binds ~40 tools; ToolSearch is a workaround)

Tool path registration:
```
nexus agent --tools /path/to/ai-dev-browser --tools /path/to/video-uploader

# Nexus mounts via DT_MOUNT:
# sys_setattr("/{zone}/tools/ai-dev-browser", entry_type=DT_MOUNT, backend=LocalPathBackend(...))
# sys_setattr("/{zone}/tools/video-uploader", entry_type=DT_MOUNT, backend=LocalPathBackend(...))
# Toolset name = parent folder name
```

System prompt only needs: `"External tools are mounted at /tools/."`
No --help hint needed — cli-args-ssot guarantees any wrong usage returns
SSOT-formatted guidance automatically.

Implementation: DT_MOUNT to real tool directories. PathRouter automatically
routes LLM's ls/cd/exec to the correct physical path. LLM uses built-in
bash/read_file to navigate and execute. Filesystem IS the registry.

**Layer: Framework (built-in tools) + Infra (VFS mount for external) | Needs building**

### 1.6 Dual Persistence Explained

**Not redundant** — different granularity and timing:

| | CASOpenAIBackend.persist_session() | ManagedAgentLoop._persist_conversation() |
|---|---|---|
| Granularity | Single LLM call (request + response) | Entire conversation (all turns incl. tool results) |
| Purpose | LLM KV cache optimization, audit trail | Session resume (--continue) |
| Tool results | Not directly (but included in next turn's request) | Yes |
| Timing | After LLM response, before tool execution | After tool execution, before next LLM call |

Tool results flow: CASOpenAIBackend is stateless — it only sees the request bytes passed to it.
Tool execution happens in ManagedAgentLoop AFTER persist_session, so that iteration's session
doesn't include tool results. However, tool results are appended to `self._messages`, and the
NEXT iteration's `persist_session(request_bytes)` includes them as part of the new LLM request.
So tool results ARE persisted by persist_session — just one iteration later.

Both retained. CAS dedup ensures no wasted space. Tool execution is the only operation
that does NOT go through CASOpenAIBackend — tools run in ManagedAgentLoop directly via
VFS syscalls. This is why persist_session sees tool results one iteration later (as part
of the next LLM request). No other content differences exist between the two persistence
paths. _persist_conversation frequency is already optimal (once after all tools in a turn,
not per-tool).

### 1.7 Session Resume [Production]

**CC**: `--continue` (last session), `--resume <id>` (specific), `--fork-session` (fork).

**Nexus plan**:

Session path convention — under agent path per `vfs_paths.py`:
```
/{zone}/agents/{id}/sessions/{session-id}/conversation
/{zone}/agents/{id}/sessions/{session-id}/metadata.json
```

This nests sessions under the agent, matching CC's `~/.claude/projects/<hash>/sessions/<id>`.
`readdir("/{zone}/agents/{id}/sessions/")` lists all sessions for an agent.

```python
class SessionManager:
    """Session discovery and lifecycle via VFS."""

    async def latest(self) -> str | None:
        """Find most recent session (--continue).
        readdir → sort by metadata.updated_at → return session_id."""

    async def load(self, session_id: str) -> list[dict]:
        """Load conversation (--resume <id>).
        sys_read → JSON deserialize → return messages[]."""

    async def fork(self, source_id: str) -> str:
        """Fork session (--fork-session).
        CAS copy-on-write: new session, same CAS hash. Zero cost."""

    async def create(self) -> str:
        """Create new empty session."""
```

**Layer: Framework | Needs building**

---

## 2. Tool System [P0]

### 2.1 Tool Interface [Production]
- See §1.5 Tool protocol design.
- **Layer: Framework | P0 | Needs building**

### 2.2 Tool Dispatch [Production]
- ToolRegistry + schemas() + execute().
- **Layer: Framework | P0 | Needs building**

### 2.3 Parallel Tool Execution [Production, partial gated]
- Classify concurrent-safe vs serial, gather vs sequential.
- **Layer: Framework | P1 | Needs building**

### 2.4 Tool Result Handling [Production]
- Truncation at 50,000 chars.
- Each tool_use gets a matching tool_result.
- **Layer: Framework | P0 | Needs building**

### 2.5 Deferred Tool Loading / ToolSearch [Gated]
- Lazy schema loading. Tool search via VFS grep on tool definition files.
- **Layer: Framework | P2 | Needs building (deferred)**

### 2.6 Complete Tool Inventory (~40+)

**Production**: Read, Write, Edit, Glob, Grep, Bash, Agent, SendMessage, TodoWrite, TaskCreate/Update/Get/List/Stop/Output, EnterPlanMode, ExitPlanMode, EnterWorktree, ExitWorktree, Skill, WebFetch, WebSearch, NotebookEdit, MCPTool, ListMcpResources, ReadMcpResource, CronCreate/Delete/List, AskUserQuestion, Config

**Gated**: TeamCreate/Delete, ListPeers, Brief, Sleep

**Unreleased**: WebBrowser, TerminalCapture, Workflow, Monitor, Snip, OverflowTest, SubscribePR, Tungsten, VerifyPlanExecution, REPL, SuggestBackgroundPR

---

## 3. Permission System [P0]

### 3.1 Multi-layer Permission Pipeline [Production]
- validateInput → PreToolUse Hooks → Permission Rules → Interactive Prompt → checkPermissions
- **Layer: Infra(ReBAC) + Framework | P0 | Nexus ReBAC stronger, needs interactive prompt**

### 3.2 Path Sandboxing [Production]
- **Layer: Infra | P0 | VFS mount boundary already exists**

### 3.3 Dangerous Command Blocking [Production]
- **Layer: Framework | P1 | Needs building (INTERCEPT hook)**

### 3.4 User Hooks [Production]
- **Layer: Framework | P1 | Needs shell adapter**

---

## 4. Context Management [P0 — Detailed Design]

### 4.1 Context Compression [Production] — DONE

CompactionStrategy protocol + DefaultCompactionStrategy (3 layers: micro/auto/manual).
Wired into ManagedAgentLoop.run(). See `services/agent_runtime/compaction.py`.

**Layer: Framework | P0 | DONE**

### 4.2 System Prompt Assembly [Production] — DONE

Multi-section assembly from VFS via `vfs_paths.agent` path helpers.
See `services/agent_runtime/system_prompt.py`.

**Layer: Framework | P0 | DONE**

### 4.3 Session Persistence [Production]
- SessionManager (§1.7) — DONE in PR #3660.
- **Layer: Framework | P0 | DONE**

---

## 5. Agent Lifecycle [P0/P1]

### 5.1 Subagent Spawn [Production]
- AgentRegistry.spawn() + ManagedAgentLoop. Tool set restriction via ReBAC.
- **Layer: Infra + Framework | P0 | Exists**

### 5.2 Multi-Agent Teams [Gated]
- **Layer: Infra + Framework | P1 | Exists**

### 5.3 Agent Messaging [Production]
- **Layer: Infra + Framework | P1 | Exists (DT_PIPE)**

### 5.4 Shutdown Protocol [Gated]
- **Layer: Infra | P1 | Exists (SIGTERM + wait)**

### 5.5 Autonomous Agents [Gated]
- **Layer: Framework | P1 | Needs building**

### 5.6 Plan Approval Protocol [Gated]
- **Layer: Framework | P1 | Needs building**

### 5.7 Agent Communication [Gated]
- **Layer: Framework | P1 | Zone isolation**

---

## 6. Task & Planning [P1]

### 6.1 TodoWrite [Production] — Layer: Framework | P1
### 6.2 Persistent Tasks [Production] — Layer: Framework | P1
### 6.3 Plan Mode [Production] — Layer: Framework | P1

---

## 7. Skill & Knowledge [P1]

### 7.1 Skill Loading [Production] — Layer: Framework | P1
### 7.2 CLAUDE.md / Project Context [Production] — Layer: Framework | P1
### 7.3 Memory System [Production] — Layer: Framework | P1

---

## 8. Workspace & Isolation [P1]

### 8.1 Git Worktree Isolation [Production] — Layer: Framework + Agent | P1
### 8.2 File Safety [Production] — Layer: Agent | P0 (Write requires Read, Edit requires unique old_string)
### 8.3 Undo / File History [Gated] — Layer: Framework | P2

---

## 9. Background & Async [P1]

### 9.1 Background Tasks [Production] — Layer: Framework | P1 (AgentRegistry + DT_PIPE)
### 9.2 Cron / Scheduled [Gated] — Layer: Framework | P2

---

## 10. External Integration [P1/P2]

### 10.1 MCP Integration [Production] — Layer: Infra | P2
### 10.2 WebFetch / WebSearch [Production] — Layer: Agent | P2
### 10.3 LSP Integration [Unreleased] — Layer: Agent | P2
### 10.4 Git Integration [Production] — Layer: Agent | P1

---

## 11. UI & Interaction [P0/P1 — Detailed Design]

### 11.1 Terminal UI [Production] — Layer: Agent | P1 (Python: Rich/Textual)
Existing `packages/nexus-tui` is a management dashboard (TypeScript/SolidJS), not agent chat.
Agent REPL will be Python-native (see §11.2). TUI polish (colors, progress bars) deferred.

### 11.2 REPL + CLI Entry Point [Production — Detailed Design]

#### Design Decisions (aligned 2026-04-07)

**Two interaction modes (orthogonal to connection method):**

| Mode | Command | Behavior |
|------|---------|----------|
| **Interactive** | `nexus chat` | Persistent REPL, multi-turn, user exits with `/quit` or Ctrl+D |
| **One-shot** | `nexus chat -p "fix the bug"` | Single prompt → agent runs → exits |

**Connection method (transparent to user):**

| Flag | Behavior |
|------|----------|
| (default) | In-process auto-start: boot NexusFS + LLM backend, single process |
| `--profile X` | Connect to existing nexusd via REMOTE profile (RPCTransport + gRPC) |

Default is auto-start (zero setup, like CC). `--profile` reuses existing CLI profile
system (`~/.nexus/config.yaml`). Both modes support both connection methods.

**Streaming**: Default is streaming (打字机效果) via `CASOpenAIBackend.start_streaming()`
→ DT_STREAM → REPL reads tokens in real-time. Matches CC default behavior.

#### CLI Entry Point

`nexus chat` — click subcommand in existing CLI (`src/nexus/cli/`):

```
nexus chat [OPTIONS] [-p PROMPT]

Options:
  -p, --prompt TEXT       One-shot mode: run single prompt and exit
  --model TEXT            Model name (default from config or env)
  --continue              Resume most recent session
  --resume ID             Resume specific session by ID
  --tools PATH...         Mount external tool directories (Tier B, §1.5)
  --profile TEXT          Connect to named nexusd profile (default: in-process)
  --deployment-profile    Deployment profile for in-process mode (default: cluster)
```

Config precedence (matching CC): `CLI args > env vars > config file`

| Setting | CLI Flag | Env Var | Config File |
|---------|----------|---------|-------------|
| Model | `--model` | `NEXUS_LLM_MODEL` | `settings.agent.model` |
| LLM URL | — | `NEXUS_LLM_BASE_URL` | `settings.agent.llm-base-url` |
| API Key | — | `NEXUS_LLM_API_KEY` | `settings.agent.llm-api-key` |
| Deployment profile | `--deployment-profile` | `NEXUS_PROFILE` | `settings.agent.deployment-profile` |
| Tools | `--tools` | — | `{cwd}/.nexus/agent.md` |

#### Bootstrap Sequence

```
nexus chat [--profile X] [-p "prompt"]
  │
  ├─ --profile given?
  │   YES → nexus.connect(profile="remote", url=..., api_key=...)
  │         Returns NexusFS with RPCTransport to existing nexusd
  │   NO  → Boot in-process:
  │         1. Resolve deployment profile: --deployment-profile > NEXUS_PROFILE env > "cluster"
  │         2. create_nexus_fs(profile=resolved, backend=CASLocalBackend(~/.nexus/data))
  │         3. Mount LLM backend: sys_setattr("/llm", DT_MOUNT, CASOpenAIBackend(...))
  │         4. Inject StreamManager into backend
  │
  ├─ Create ManagedAgentLoop(
  │     sys_read=nx.sys_read, sys_write=nx.sys_write,
  │     stream_read=nx._stream_manager.stream_read,
  │     llm_backend=backend, agent_path="/root/agents/default",
  │     cwd=os.getcwd(), model=model,
  │     tool_registry=ToolRegistry(default_tools()),
  │     compactor=DefaultCompactionStrategy(sys_write=nx.sys_write, agent_path=...)
  │   )
  │
  ├─ await loop.initialize()  # Assemble system prompt from VFS (§4.2)
  │
  ├─ -p given?
  │   YES → One-shot: result = await loop.run(prompt); print; exit
  │   NO  → Interactive REPL: enter repl_loop()
  │
  └─ Cleanup: shutdown NexusFS if in-process
```

#### Interactive REPL

```python
async def repl_loop(loop: ManagedAgentLoop) -> None:
    """Interactive REPL with slash commands and streaming output."""
    while True:
        try:
            query = await async_input("nexus > ")
        except (EOFError, KeyboardInterrupt):
            break

        query = query.strip()
        if not query:
            continue

        # Slash commands
        if query.startswith("/"):
            handled = handle_slash_command(query, loop)
            if handled == "quit":
                break
            continue

        # Agent turn with streaming token display
        result = await loop.run(query)
        # Tokens already printed in real-time via stream reader
        # Final status: cost, model, etc.
        print_turn_summary(result)
```

Streaming display: a background task reads from DT_STREAM and prints tokens
as they arrive (`sys.stdout.write(token); sys.stdout.flush()`). The REPL
doesn't block on full response — tokens appear in real-time.

Ctrl+C during LLM call: cancels `CASOpenAIBackend.cancel_stream()`, returns
to prompt. Does NOT exit REPL.

#### Slash Commands (V1)

| Command | V1 | Description |
|---------|-----|-------------|
| `/help` | Yes | Show available commands |
| `/compact` | Yes | Manual context compression (§4.1 Layer 3) |
| `/clear` | Yes | Clear conversation, start fresh |
| `/model MODEL` | Yes | Switch model |
| `/quit` | Yes | Exit REPL |
| `/cost` | Yes | Show accumulated token usage |
| `/sessions` | Yes | List available sessions |
| `/status` | Yes | Show agent status (model, tokens, session) |
| Others | No | Placeholder "Not implemented yet" |

#### Files to Create/Modify

| File | Action |
|------|--------|
| `src/nexus/cli/commands/chat.py` | NEW: click subcommand, bootstrap, REPL loop |
| `src/nexus/cli/main.py` | ADD: register `chat` subcommand |
| `src/nexus/services/agent_runtime/managed_loop.py` | MINOR: streaming token callback hook |

**Layer: Agent + Framework | P0 | Needs building**

### 11.3 Keyboard Shortcuts [Production] — Layer: Agent | P2
### 11.4 Status Line [Production] — Layer: Agent | P2

---

## 12. Internal Architecture [P2]

### 12.1 Feature Flags [Production] — Layer: Framework | P2
### 12.2 Internal vs External User [Gated] — Layer: Framework | P2
### 12.3 Undercover Mode [Gated] — Layer: Agent | P2
### 12.4 Remote Control [Production] — Layer: Framework | P2
### 12.5 Telemetry [Production] — Layer: Infra | P2 (OTel exists)
### 12.6 Model Codenames — Not implementing

---

## 13. Configuration [P0/P1]

### 13.1 Settings System [Production] — Layer: Framework | P1
Existing: `~/.nexus/config.yaml` with profile management (`nexus connect`, `nexus config`).
Agent-specific settings (default model, LLM URL) to be added under `settings.agent.*`.

### 13.2 CLI Arguments [Production] — Layer: Agent | P0
See §11.2 CLI entry point for full argument list.
Precedence: `CLI args > env vars > config file` (matching CC).

### 13.3 Environment Variables [Production] — Layer: Agent | P0
| Variable | Purpose |
|----------|---------|
| `NEXUS_LLM_BASE_URL` | LLM API base URL |
| `NEXUS_LLM_API_KEY` | LLM API key |
| `NEXUS_LLM_MODEL` | Default model name |
| `NEXUS_PROFILE` | Default connection profile |
| `NEXUS_URL` | Remote nexusd URL (REMOTE profile) |
| `NEXUS_API_KEY` | Remote nexusd API key |

---

## 14. Advanced / Future [P2]

### 14.1 KAIROS [Unreleased] | 14.2 Voice Mode [Unreleased] | 14.3 Coordinator Mode [Unreleased]
### 14.4 Bridge Layer [Gated] — Nexus FastAPI exists
### 14.5 Notebook Editing [Production] | 14.6 Multimodal [Production] — P1
### 14.7 Buddy System — Not implementing

---

## 15. Design Patterns

| Pattern | CC Usage | Nexus Equivalent |
|---------|----------|-----------------|
| AsyncGenerator streaming | Full-chain streaming | DT_STREAM |
| Builder + Factory | Tool safe defaults | ToolRegistry |
| Observer + State Machine | Tool lifecycle | OBSERVE phase |
| Snapshot State | File undo/redo | CAS versioning |
| Context Isolation | Per-agent context | Zone isolation |

---

## Stats

- **Total items**: ~85 independent mechanisms
- **Production**: ~50 | **Gated**: ~15 | **Unreleased**: ~15 | **Not implementing**: ~5
- **P0**: ~15 | **P1**: ~25 | **P2**: ~25
- **Nexus exists**: ~20 | **Needs enhancement**: ~10 | **Needs building**: ~35

---

## Current Scope

### Done (merged or in PR #3660):

1. ~~**Tool call parsing** (§1.4)~~ — DONE (PR #3660)
2. ~~**Built-in tools (Tier A)** (§1.5)~~ — DONE (PR #3660, ToolRegistry + 6 tools)
3. ~~**CASAnthropicBackend** (§1.2)~~ — DONE (PR #3660)
4. ~~**Retry wrapper** (§1.3)~~ — DONE (PR #3660)
5. ~~**Session Manager** (§1.7)~~ — DONE (PR #3660)

### To implement next (designed, ready for implementation):

6. ~~**Context Compression** (§4.1)~~ — DONE (CompactionStrategy + DefaultCompactionStrategy, 15 tests)
7. ~~**System Prompt Assembly** (§4.2)~~ — DONE (assemble_system_prompt + vfs_paths, 9 tests)
8. **REPL + CLI** (§11.2 + §13.2) — `nexus chat` click subcommand, interactive + one-shot, streaming
9. **External tool discovery (Tier B)** (§1.5) — DT_MOUNT toolset dirs

### Deferred Items (not in current scope):
- Multi-agent teams (§5.2, P1)
- Skill loading (§7.1, P1)
- MCP integration (§10.1, P2)
- TUI polish (§11.1, P1)
- Worktree isolation (§8.1, P1)
- Feature flags (§12.1, P2)
- Semantic search in CLUSTER profile (P1 — needs BRICK_SEARCH opt-in)

---

## Known Issues & Future Work

### Conversation persistence gap (if _persist_conversation is removed from loop)
If we rely solely on CASOpenAIBackend.persist_session() and remove
ManagedAgentLoop._persist_conversation():

1. **Crash recovery gap**: If agent crashes after tool execution but before next LLM call,
   the most recent tool results are lost (not yet in any persist_session request).
2. **Final-turn gap**: If the last turn of a session includes tool calls, the tool results
   are never included in a persist_session (no next LLM call to carry them).
3. **Session fork**: Requires reconstructing full messages[] from all persist_session records
   (request/response pairs), which is possible but more complex than reading a single
   conversation snapshot.

**Mitigation options (deferred)**:
- Add a lightweight "conversation checkpoint" on session close (single write)
- Add a "flush pending tool results" step before fork
- Accept the gap (tool results can be re-executed from the tool_call definitions in
  the assistant messages, which ARE persisted)
