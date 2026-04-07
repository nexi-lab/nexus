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

### 4.1 Context Compression [Production]

Pluggable ABC with CC-compatible default implementation:

```python
class CompactionStrategy(Protocol):
    async def compact(self, messages: list[dict], ctx: CompactContext) -> list[dict]: ...

class CompactContext:
    token_estimate: int        # current token count
    max_tokens: int            # threshold for auto_compact
    llm_call: Callable         # for strategies that need LLM summarization
    sys_write: SysWriteFn      # for transcript persistence to VFS
    agent_path: str            # for transcript VFS path
```

Default implementation (CC-compatible, 3 layers):

**Layer 1 — micro_compact** (every turn, in ManagedAgentLoop.run() before LLM call):
- Scan messages for role="tool" entries
- Keep last 3 tool results at full fidelity
- Older tool results with content > 100 chars → replace with `[cleared]`
- In-place mutation, no LLM call needed

**Layer 2 — auto_compact** (token threshold trigger, in ManagedAgentLoop.run()):
- Trigger when `estimate_tokens(messages) > 100K`
- Save full transcript to VFS: `/{zone}/agents/{id}/transcripts/{timestamp}.jsonl`
- Call LLM to generate summary (last 80K chars, max 2000 tokens)
- Replace all messages with `[Compressed]\n\n{summary}`
- Insert compact_boundary marker

**Layer 3 — manual compact** (/compact slash command or model calls compact tool):
- Same logic as auto_compact but user/model triggered
- Exposed as slash command in REPL and as a tool in ToolRegistry

Integration point: ManagedAgentLoop.run() calls compaction before each LLM call:
```
while turns < max_turns:
    self._compactor.micro_compact(self._messages)
    if self._compactor.should_auto_compact(self._messages):
        self._messages = await self._compactor.auto_compact(self._messages)
    response = await self._call_llm_with_retry()
    ...
```

**Layer: Framework | P0 | Needs building**

### 4.2 System Prompt Assembly [Production]

CC has 19 sections in its system prompt. Nexus matches with VFS files:

| # | CC Section | Nexus VFS File | Dynamic? |
|---|-----------|----------------|----------|
| 1 | Identity & Role | `{agent_path}/SYSTEM.md` | No |
| 2 | Strengths | `{agent_path}/SYSTEM.md` | No |
| 3 | Guidelines | `{agent_path}/SYSTEM.md` | No |
| 4 | Notes | `{agent_path}/SYSTEM.md` | No |
| 5 | Environment info | Generated at runtime | Yes |
| 6 | Model identity | Generated at runtime | Yes |
| 7 | Knowledge cutoff | Generated at runtime | Yes |
| 8 | Git status | Generated at runtime | Yes |
| 9 | Tool descriptions | `ToolRegistry.schemas()` → API tools param | Yes |
| 10 | Output efficiency | `{agent_path}/prompts/output_efficiency.md` | No |
| 11 | Model patches | `{agent_path}/prompts/model_patches.md` | Conditional |
| 12 | Project context | `{cwd}/.nexus/agent.md` (equiv to CLAUDE.md) | Yes |
| 13 | Conditional | Feature flags | Conditional |
| 14 | JSON formatting | `{agent_path}/prompts/json_formatting.md` | No |
| 15 | Tool batching | `{agent_path}/prompts/tool_batching.md` | No |

system-reminder injections (runtime, as user messages in conversation):
- Current date
- Skill availability list
- Deferred tool availability
- Task-tool nudges

Assembly in `initialize()`:
```python
async def _assemble_system_prompt(self) -> str:
    parts = []
    # Static sections from SYSTEM.md (1-4)
    parts.append(await self._sys_read(f"{self._agent_path}/SYSTEM.md"))
    # Dynamic environment block (5-8)
    parts.append(self._generate_env_block())
    # Static prompt fragments (10, 14, 15)
    for name in ("output_efficiency", "json_formatting", "tool_batching"):
        try:
            parts.append(await self._sys_read(f"{self._agent_path}/prompts/{name}.md"))
        except Exception:
            pass
    # Project context (12) — read from cwd
    try:
        parts.append(await self._sys_read(f"{self._cwd}/.nexus/agent.md"))
    except Exception:
        pass
    return "\n\n".join(p.decode("utf-8").strip() for p in parts if p)
```

Tool descriptions (9) go in the API `tools` parameter, not in system prompt text.

**Layer: Framework | P0 | Needs building**

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

### 11.2 REPL + Slash Commands [Production — Detailed Design]

**Agent startup**: Uses same pattern as 3rd-party AcpService but with MANAGED kind:
1. `AgentRegistry.spawn(kind=MANAGED)` → PID
2. Create StdioPipe for user terminal ↔ agent communication
3. Register DT_PIPEs at `/{zone}/proc/{pid}/fd/0,1,2`
4. Start ManagedAgentLoop with session from SessionManager

**CLI entry point**: `nexus chat` (click subcommand), connects to local nexusd:
```
nexus chat [--model MODEL] [--continue] [--resume ID] [--fork-session ID] [--tools PATH...]
```

**REPL loop**:
```python
while True:
    query = input("nexus >> ")
    if query.startswith("/"):
        handle_slash_command(query)
    elif query.strip():
        result = await loop.run(query)
        print(result.text)
```

**Slash command registry** — all CC commands listed, core ones implemented first:

| Command | Status | Description |
|---------|--------|-------------|
| `/help` | Implement | Show help |
| `/compact` | Implement | Manual context compression |
| `/clear` | Implement | Clear conversation |
| `/model` | Implement | Switch model |
| `/quit` | Implement | Exit REPL |
| `/sessions` | Implement | List sessions |
| `/cost` | Implement | Show token usage / cost |
| `/commit` | Placeholder | Skill: git commit |
| `/review-pr` | Placeholder | Skill: PR review |
| `/pdf` | Placeholder | Skill: PDF processing |
| `/simplify` | Placeholder | Skill: code simplification |
| `/loop` | Placeholder | Skill: recurring task |
| `/schedule` | Placeholder | Skill: cron scheduling |
| `/tasks` | Placeholder | Show task board |
| `/team` | Placeholder | Show team roster |
| `/inbox` | Placeholder | Check inbox |
| `/effort` | Placeholder | Set effort level |
| `/btw` | Placeholder | Side question |
| `/stickers` | Placeholder | Easter egg |
| `/thinkback` | Placeholder | Year in review |
| `/good-claude` | Placeholder | Easter egg |
| `/bughunter` | Placeholder | Easter egg |
| `/fast` | Placeholder | Toggle fast mode |
| `/verbose` | Placeholder | Toggle verbose output |
| `/config` | Placeholder | Edit settings |
| `/keybindings` | Placeholder | Keybinding config |
| `/update-config` | Placeholder | Skill: config update |
| `/claude-api` | Placeholder | Skill: API help |
| `/doctor` | Placeholder | Diagnostics |
| `/init` | Placeholder | Project setup |
| `/status` | Placeholder | Agent status |
| `/memory` | Placeholder | Memory system |
| `/forget` | Placeholder | Remove memory |

Placeholder = registered in slash command map but returns "Not implemented yet".
Skill-backed commands will use the skill system when implemented (§7.1).

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
### 13.2 CLI Arguments [Production] — Layer: Agent | P0
### 13.3 Environment Variables [Production] — Layer: Agent | P0

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

6. **Context Compression** (§4.1) — pluggable CompactionStrategy ABC, CC-compatible default
7. **System Prompt Assembly** (§4.2) — 15 sections mapped to VFS files, _assemble_system_prompt()
8. **REPL + CLI** (§11.2 + §13.2) — `nexus chat` via click, slash command registry, StdioPipe agent
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
