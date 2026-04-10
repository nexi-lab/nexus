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

## 1. Core Agent Loop [P0] — DONE

### 1.1 Main Loop — DONE
`ManagedAgentLoop.run()` — CC-equivalent `while tool_use → execute → loop`.
See `services/agent_runtime/managed_loop.py`.

### 1.2 LLM API Call — DONE

Multi-provider strategy: OpenAI-compatible SDK via nova-gateway (translates all LLMs to OpenAI format) + planned `CASAnthropicBackend` for native Claude support. `CASOpenAIBackend.generate_streaming()` yields (token, metadata) through DT_STREAM.

### 1.3 Retry & Error Handling — DONE

Exponential backoff for 429/5xx/network errors, immediate fail on auth errors, tool failures returned as error strings to model.

### 1.4 Tool Call Parsing — DONE

Incremental accumulation of OpenAI-compatible streaming tool calls: per-index argument concatenation across chunks, emitted as complete tool_calls in the "done" control message.

### 1.5 Tool Registry & Execution — DONE

**Key design decision — two-tier tool model**:

- **Tier A: Built-in kernel tools (eager, function-calling)** — DONE.
  Small set (~6) bound via function-calling schema: read_file, write_file, edit_file, bash, grep, glob.

- **Tier B: External CLI tools (lazy, filesystem discovery)** — DONE.
  `nexus chat --tools /path/to/toolset` → DT_MOUNT to `/root/tools/{name}`.
  LLM discovers on-demand via `ls /tools/` + `--help`. Filesystem IS the registry.

### 1.6 Dual Persistence — retained (design rationale)

**Not redundant** — different granularity and timing:

| | CASOpenAIBackend.persist_session() | ManagedAgentLoop._persist_conversation() |
|---|---|---|
| Granularity | Single LLM call (request + response) | Entire conversation (all turns incl. tool results) |
| Purpose | LLM KV cache optimization, audit trail | Session resume (--continue) |
| Timing | After streaming done (in `_run_stream()`) | After tool execution, before next LLM call |

Both retained. CAS dedup ensures no wasted space. `persist_session()` now
lives in `CASOpenAIBackend` directly (LLMStreamingService eliminated in PR #3657).

### 1.7 Session Resume — DONE

Session paths: `/{zone}/agents/{id}/sessions/{session-id}/conversation` and `metadata.json`.

```python
class SessionManager:
    """Session discovery and lifecycle via VFS."""
    async def latest(self) -> str | None:    # --continue
    async def load(self, session_id: str) -> list[dict]:  # --resume <id>
    async def fork(self, source_id: str) -> str:  # --fork-session (CAS copy-on-write)
    async def create(self) -> str:
```

---

## 2. Tool System [P0 — Detailed Design]

### 2.1 Tool Interface — extend Protocol

**CC source**: `Tool.ts:362-695`. CC's Tool has: `name`, `inputSchema`, `call()`,
`description()`, `isReadOnly()`, `isConcurrencySafe()`, `isDestructive()`,
`validateInput()`, `checkPermissions()`, `maxResultSizeChars`, `shouldDefer`,
`alwaysLoad`, `preparePermissionMatcher()`.

**Nexus current**: `Tool` Protocol has `name`, `description`, `input_schema`, `call()`,
`is_read_only()`, `is_concurrent_safe()`. Missing: `max_result_size_chars`,
`validate_input`, `check_permissions`, `is_destructive`, `should_defer`.

**Plan — add to Tool Protocol**:
- `max_result_size_chars: int` — per-tool cap (default 50K). CC: `Tool.ts:466`
- `validate_input(input) -> ValidationResult` — input validation (optional, default pass). Via kernel INTERCEPT hook, not Python layer.
- `check_permissions(input, ctx) -> PermissionResult` — permission gate (optional, default allow). Via Rust CC-like permission service, not ReBAC.
- `is_destructive(input) -> bool` — marks irreversible ops. CC: `Tool.ts:407`
- `should_defer: bool` — lazy schema loading (see §2.5). CC: `Tool.ts:438`

Core Tier A tools (read/write/edit/bash/grep/glob): ✅ DONE.
Protocol extension: ✅ DONE (`tool_registry.py` — `max_result_size_chars`, `is_destructive`, `should_defer` with defaults).

**Layer: Framework (Rust permission svc + Python Protocol) | P0**

### 2.2 Tool Dispatch — DONE

`ToolRegistry.execute_one()` + `schemas()`. See `services/agent_runtime/tool_registry.py`.

### 2.3 Parallel Tool Execution — CC-equivalent exclusive-lock model

**CC source**: `StreamingToolExecutor.ts:34-151`. Algorithm:
- Concurrent-safe tools run in parallel (`asyncio.gather` equivalent)
- Non-concurrent-safe tools require exclusive access — block ALL others
- `canExecuteTool`: returns true IFF (no running tools) OR (this tool AND all running are concurrent-safe)
- Sibling abort: if one concurrent tool errors, signal siblings to short-circuit (`StreamingToolExecutor.ts:45-48`)

**Nexus current**: `ToolRegistry.execute()` classifies concurrent/serial and gathers, BUT `managed_loop` doesn't use it — loops `execute_one` sequentially.

**Plan — implement via pluggable `ConcurrencyPolicy` ABC**:

```python
class ConcurrencyPolicy(Protocol):
    """Pluggable concurrency control for tool execution."""
    async def execute_batch(self, tools: list[ToolCall], executor: ToolRegistry) -> list[str]: ...

class ExclusiveLockPolicy:
    """CC-equivalent: concurrent-safe gather, non-safe exclusive."""
    # Default. Cite: StreamingToolExecutor.ts:129-151
```

Wire into `managed_loop.run()`: replace sequential `for tc in tool_calls`.

**Layer: Framework | P0 | ✅ DONE** — `ExclusiveLockPolicy` default, wired into `managed_loop.run()`.
`ConcurrencyPolicy` injected via DI — replaceable per-agent.

### 2.4 Tool Result Handling — two-tier truncation + spill to VFS

**CC source**: `toolResultStorage.ts:189-356`, `toolLimits.ts:4-49`.

CC behavior (NOT simple `[:50000]` — much more sophisticated):
1. **Per-tool cap**: each tool has `maxResultSizeChars` (default 50K, BashTool=30K, most tools=100K)
2. If result exceeds cap → **persist full content to disk**, send LLM a preview:
   ```
   <persisted-output>
   Output too large (350.2 KB). Full output saved to: {path}

   Preview (first 2.0 KB):
   {head of content, cut at last newline boundary}
   ...
   </persisted-output>
   ```
3. **Per-message aggregate budget** (200K): if N parallel tool results together exceed 200K, persist largest until under budget. Prevents 5×40K=200K blowup.
4. **Empty result handling**: inject `({tool_name} completed with no output)` to prevent LLM tokenization errors. Cite: `toolResultStorage.ts:272-296`.
5. **Preview algorithm**: head-only, truncated at last newline in `[max_bytes*0.5, max_bytes]` window. Cite: `toolResultStorage.ts:339-356`.

**Nexus plan — pluggable ABCs**:

```python
class TruncationStrategy(Protocol):
    """Pluggable preview generation."""
    def generate_preview(self, content: str, max_bytes: int) -> tuple[str, bool]: ...

class ToolResultStorage(Protocol):
    """Pluggable spill-to-disk."""
    async def persist(self, content: str, tool_use_id: str) -> str: ...  # returns VFS path

class MessageBudgetPolicy(Protocol):
    """Pluggable per-message budget enforcement."""
    async def enforce(self, results: list[ToolResult], budget: int) -> list[ToolResult]: ...
```

**Default implementations** match CC behavior but use nexus-native storage:
Large tool results persist to DT_STREAM (WALStreamBackend or MemoryStreamBackend).
LLM reads back via `read_file(same_path, offset=N)` — **same path, no new file**.
This is better than CC's separate file approach: LLM doesn't need to remember a
different path, just reads from the same resource with offset.

Configurable thresholds (CC has per-tool + global):
- `DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000` (per-tool default)
- `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000` (per-message aggregate)
- `PREVIEW_SIZE_BYTES = 2_000` (preview length)

All constants overridable via config.

**Layer: Framework | P0 | ✅ DONE** — `HeadTruncation`, `VFSToolResultStorage`, `DefaultMessageBudget`.
Per-tool truncation in `ExclusiveLockPolicy._execute_one()`, aggregate budget in `managed_loop.run()`.

### 2.5 Deferred Tool Loading / ToolSearch

**CC source**: `Tool.ts:438-449`, `ToolSearchTool/prompt.ts:62-108`.

CC behavior:
1. Tools with `shouldDefer=true` → schema NOT sent to LLM at boot
2. Only tool **names** listed in `<available-deferred-tools>` system reminder
3. LLM calls `ToolSearch` tool → returns full `<function>` JSON schema blocks
4. After ToolSearch, LLM can invoke those tools normally
5. MCP tools always deferred; ToolSearch + Agent never deferred

**Nexus**: Tier B (filesystem discovery via `--tools` DT_MOUNT) covers external tool lazy loading. For built-in tools, Tier A is small (6 tools, ~1.2K tokens) — no need to defer.

When MCP integration is added (§10), adopt `shouldDefer` on Tool Protocol for MCP tools. ToolSearch maps to VFS grep on tool schema files.

**Status**: Tier B ✅ DONE. Built-in deferral deferred until §10.

### 2.6 Complete Tool Inventory (40 CC tools → Nexus mapping)

| CC Tool | §  | Nexus Equivalent | Status |
|---------|-----|-----------------|--------|
| FileReadTool | §2 | `read_file` (Tier A) | ✅ Done |
| FileWriteTool | §2 | `write_file` (Tier A) | ✅ Done |
| FileEditTool | §2 | `edit_file` (Tier A) | ✅ Done |
| BashTool | §2 | `bash` (Tier A) | ✅ Done |
| GlobTool | §2 | `glob` (Tier A) | ✅ Done |
| GrepTool | §2 | `grep` (Tier A) | ✅ Done |
| AgentTool | §5 | AgentRegistry.spawn() + ACP | Infra exists |
| SendMessageTool | §5 | DT_PIPE messaging | Infra exists |
| TodoWriteTool | §6 | — | Needs building |
| TaskCreateTool | §6 | — | Needs building |
| TaskGetTool | §6 | — | Needs building |
| TaskListTool | §6 | — | Needs building |
| TaskUpdateTool | §6 | — | Needs building |
| TaskStopTool | §6 | — | Needs building |
| TaskOutputTool | §6 | — | Needs building |
| EnterPlanModeTool | §6 | — | Needs building |
| ExitPlanModeTool | §6 | — | Needs building |
| SkillTool | §7 | — | Needs building |
| ToolSearchTool | §2.5 | Tier B filesystem discovery | ✅ Different approach |
| AskUserQuestionTool | §11 | StdioPipe interactive prompt | Needs building |
| ConfigTool | §13 | `~/.nexus/config.yaml` edit | Needs building |
| WebFetchTool | §10 | — | Needs building |
| WebSearchTool | §10 | — | Needs building |
| MCPTool | §10 | — | Needs building |
| ListMcpResourcesTool | §10 | — | Needs building |
| ReadMcpResourceTool | §10 | — | Needs building |
| McpAuthTool | §10 | — | Needs building |
| NotebookEditTool | §14 | — | Needs building |
| LSPTool | §10 | — | Needs building |
| REPLTool | §11 | — | Needs building |
| EnterWorktreeTool | §8 | — | Needs building |
| ExitWorktreeTool | §8 | — | Needs building |
| ScheduleCronTool (×3) | §9 | — | Needs building |
| RemoteTriggerTool | §9 | — | Needs building |
| TeamCreateTool | §5 | — | Needs building |
| TeamDeleteTool | §5 | — | Needs building |
| BriefTool | — | — | Skip (KAIROS-gated) |
| SleepTool | §9 | `asyncio.sleep` wrapper | Trivial |
| PowerShellTool | — | — | Skip (Windows only) |
| SyntheticOutputTool | — | — | Skip (testing only) |

**§2 scope**: 6 Tier A tools ✅ Done. Other tools belong to §5-§14.

---

## 3. Permission System [P0 — Detailed Design]

### 3.1 Three-Checkpoint Permission Pipeline

**CC source**: `toolExecution.ts:683-929`. Three sequential checkpoints:

1. **validateInput** (`toolExecution.ts:683-686`) — input shape validation, blocked patterns.
   CC: per-tool `validateInput()` method.
   Nexus: kernel INTERCEPT hook — tool registers a pre-exec validator via `HookSpec`.

2. **PreToolUse hooks** (`toolExecution.ts:800-862`) — user-defined hook chain.
   CC: config-driven hook list (`~/.claude/settings.json`).
   Nexus: kernel INTERCEPT hooks via existing `HookRegistry`. Tool-level hooks register at mount time. Config-driven deny/allow rules in `~/.nexus/config.yaml`.

3. **checkPermissions** (`toolExecution.ts:921-929`) — rule-based permission matching.
   CC: simple wildcard pattern matching (`Bash(git *)`) + interactive user prompt (allow/deny/always-allow).
   Nexus: **new Rust CC-like permission service** (NOT ReBAC — simpler). Inject into kernel via INTERCEPT phase. V1: rule-based deny/allow. V2: add interactive prompt.

**Rust permission service design**:
```rust
/// CC-like rule-based permission matcher.
/// Injected into kernel's INTERCEPT phase as a hook.
pub struct ToolPermissionService {
    rules: Vec<PermissionRule>,  // loaded from ~/.nexus/config.yaml
}

struct PermissionRule {
    tool_pattern: String,      // e.g. "Bash(git *)", "FileWrite(/etc/*)"
    action: PermissionAction,  // Allow | Deny | Ask
}
```

Matches CC's `bashPermissions.ts` wildcard pattern matching. Rules loaded from config. `Ask` deferred to V2 (interactive terminal prompt).

**Layer: Infra (Rust) + Framework | P0 | ✅ DONE (V1 Python)** — `RuleBasedPermissionService` with wildcard pattern matching,
`PermissionRule.from_config()` for `~/.nexus/config.yaml` loading, wired into `ExclusiveLockPolicy._execute_one()`.
Rust acceleration follow-up (same interface, swap implementation).

### 3.2 Path Sandboxing — ✅ DONE

VFS mount boundaries are stronger than CC's `safe_path()`. Every tool operates through `sys_read`/`sys_write` which goes through kernel routing + permission checks. No path escape possible.

### 3.3 Dangerous Command Blocking

**CC source**: `bashSecurity.ts:77-101`. CC has **23 security check categories**:
command substitution, process substitution, shell metacharacters, obfuscated flags,
dangerous variables, IFS injection, git commit substitution, /proc/environ access,
zsh dangerous commands, brace expansion, control characters, unicode whitespace, etc.

**Nexus plan**: implement `BashCommandValidator` (Rust, inject as INTERCEPT hook).
Adopt CC's full 23-category check list. Configurable via deny list in config.
CC: `bashSecurity.ts:16-101`. Each category has pattern + error message.

**Layer: Infra (Rust) | P1 | ✅ DONE (V1 Python)** — `BashCommandValidator` with 23 security categories,
configurable disabled_categories + extra_patterns. Wired into `ExclusiveLockPolicy._execute_one()` for bash tool calls.
Rust acceleration follow-up.

### 3.4 User Hooks — PreToolUse config hooks

CC: `settings.json` → `hooks` array → `preToolUse` entries.
Nexus: `~/.nexus/config.yaml` → `settings.agent.hooks.pre_tool_use` entries.
Loaded at agent boot, registered as INTERCEPT hooks in kernel dispatch.

**Layer: Framework | P1 | ✅ DONE (config loading)** — `RuleBasedPermissionService.from_config()` + `BashCommandValidator.from_config()`.
Hook registration via DI injection (`ManagedAgentLoop(permission_service=..., bash_validator=...)`).
Config-driven user hooks: planned for §10 (MCP integration).

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

## 4A. Sudowork Integration [P0 — Detailed Design]

Integrate `nexus chat` as an ACP-compatible agent in
[sudowork](https://github.com/sudoprivacy/sudowork) — the multi-agent
cowork platform. Nexus appears as a new tab alongside Claude Code,
Codex, Goose, etc.

### 4A.1 ACP JSON-RPC Transport

**Context**: Sudowork spawns agents as child processes communicating via
newline-delimited JSON-RPC 2.0 over stdin/stdout. Each agent binary
accepts a flag (`--acp`, `--experimental-acp`, or subcommand `acp`)
to switch from terminal mode to JSON-RPC mode.

**Nexus**: `nexus chat --acp` activates ACP I/O mode. Orthogonal to
connection method (`--with`):

```
nexus chat                      # terminal + embedded NexusFS
nexus chat --with addr          # terminal + remote NexusFS
nexus chat --acp                # JSON-RPC + embedded NexusFS (sudowork spawns this)
nexus chat --acp --with addr    # JSON-RPC + remote NexusFS
```

**Implementation** — new module `src/nexus/services/agent_runtime/acp_transport.py`:

```python
class AcpTransport:
    """ACP JSON-RPC transport over stdin/stdout."""

    async def read_message(self) -> dict:
        """Read one JSON-RPC message from stdin (newline-delimited)."""

    def write_message(self, msg: dict) -> None:
        """Write one JSON-RPC message to stdout (newline-delimited)."""

    def send_notification(self, method: str, params: dict) -> None:
        """Send a notification (no response expected)."""

    async def send_request(self, method: str, params: dict) -> dict:
        """Send a request and await response (for permission prompts)."""
```

**Layer: Framework | P0 | Needs building**

### 4A.2 ACP Protocol Handler

Handle JSON-RPC methods from sudowork. Creates ManagedAgentLoop on
`session/new` and drives it on `session/prompt`.

**Sudowork → Nexus (incoming):**

| Method | Action |
|--------|--------|
| `initialize` | Return `{ protocolVersion: 1, capabilities: {...} }` |
| `session/new` | Create ManagedAgentLoop, return `{ sessionId, configOptions, models }` |
| `session/load` | Resume session via SessionManager (P1) |
| `session/prompt` | Run `loop.run(prompt)`, stream updates, return when turn completes |
| `session/set_model` | Switch model mid-session (P1) |
| `session/set_config_option` | Update config (P1) |

**Nexus → Sudowork (outgoing):**

| Method | When |
|--------|------|
| `session/update` (notification) | Every token chunk, tool call status change, usage update |
| `session/request_permission` (request) | Tool needs interactive approval (§3 `Ask` action) |

**Implementation** — new module `src/nexus/services/agent_runtime/acp_handler.py`:

```python
class AcpProtocolHandler:
    """ACP protocol handler — bridges sudowork ↔ ManagedAgentLoop."""

    def __init__(self, transport: AcpTransport, **loop_kwargs): ...

    async def run(self) -> None:
        """Main loop: read JSON-RPC messages, dispatch to handlers."""

    async def _handle_initialize(self, params: dict) -> dict: ...
    async def _handle_session_new(self, params: dict) -> dict: ...
    async def _handle_session_prompt(self, params: dict) -> dict: ...
```

**Layer: Framework | P0 | Needs building**

### 4A.3 Push-mode Observer Bridge

AgentObserver currently accumulates updates (pull model). ACP requires
pushing `session/update` notifications to stdout **immediately** as they
happen during `loop.run()`.

**Design**: Add optional `on_update` callback to AgentObserver. When set,
each `observe_update()` call also fires the callback. AcpProtocolHandler
wires this to `transport.send_notification("session/update", ...)`.

```python
class AgentObserver:
    def __init__(self, on_update: Callable | None = None):
        self._on_update = on_update

    def observe_update(self, update_type, update):
        # ... existing accumulation logic ...
        if self._on_update:
            self._on_update(update_type, update)  # push to ACP
```

Also wire streaming token delivery: current REPL prints tokens to terminal.
ACP mode sends them as `agent_message_chunk` notifications instead.

**Layer: Framework | P0 | Needs building**

### 4A.4 Tool Call Status Lifecycle

Sudowork UI shows tool execution progress (spinner, status text). Requires
status transitions: `pending` → `in_progress` → `completed`/`failed`.

Current ExclusiveLockPolicy only emits a single `tool_call` observation.
Extend to emit status transitions:

```
1. Before execution: observe("tool_call", {status: "pending", ...})
2. Start execution:  observe("tool_call_update", {status: "in_progress", ...})
3. After execution:  observe("tool_call_update", {status: "completed"|"failed", ...})
```

Map to ACP `session/update` with `sessionUpdate: "tool_call"` and
`sessionUpdate: "tool_call_update"` respectively.

**Layer: Framework | P0 | Needs building**

### 4A.5 Interactive Permission via ACP

§3.1 defined `PermissionAction.Ask` as V2. For sudowork integration this
becomes P0 — sudowork UI shows approval dialog, user clicks allow/deny.

**Flow**:
1. ExclusiveLockPolicy hits a rule with `action: Ask`
2. Sends `session/request_permission` JSON-RPC **request** to sudowork
3. Sudowork shows dialog with options (allow_once, allow_always, reject_once, reject_always)
4. Sudowork responds with `{ outcome: { outcome, optionId } }`
5. Nexus proceeds or blocks based on response

**Implementation**: Add `AcpPermissionBridge` that wraps `PermissionService`.
When action is `Ask`, it calls `transport.send_request("session/request_permission", ...)`
instead of auto-denying.

**Layer: Framework | P1 | Needs building**

### 4A.6 Sudowork-side Registration

Add nexus to `src/types/acpTypes.ts` in the sudowork repo:

```typescript
nexus: {
    id: 'nexus',
    name: 'Nexus',
    cliCommand: 'nexus',
    acpArgs: ['chat', '--acp'],
    enabled: true,
    supportsStreaming: true,
}
```

Auto-detected via `which nexus`. No custom connector needed — uses
generic backend spawn path.

**Layer: External (sudowork PR) | P0 | Trivial**

### 4A.7 Implementation Order

```
Step 1: AcpTransport (§4A.1) + AcpProtocolHandler skeleton (§4A.2)
        — initialize + session/new + session/prompt (basic, no streaming)
Step 2: Push-mode Observer (§4A.3) + --acp CLI flag
        — full streaming, end-to-end test with sudowork
Step 3: Tool call lifecycle (§4A.4)
        — sudowork shows tool execution progress
Step 4: Interactive permission (§4A.5)
        — approval dialogs in sudowork UI
Step 5: Sudowork PR (§4A.6)
        — add nexus entry to acpTypes.ts
Step 6: session/load + set_model + configOptions (P1 polish)
```

Steps 1-2 are the MVP. After Step 2, `nexus chat --acp` works in sudowork.

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

**Agent runtime modes** (see also `cli-design.md`):

| Mode | NexusFS | Human interaction | Use case |
|------|---------|-------------------|----------|
| Embedded (`nexus chat`) | In-process CLUSTER, exclusive | stdin/stdout terminal | Local dev, CC-like |
| Remote (`nexus chat --with addr`) | REMOTE proxy → nexusd | stdin/stdout terminal | Team/shared nexusd |
| API spawn (nexusd internal) | nexusd's own NexusFS | API (HTTP/gRPC) | Background tasks, workflows |
| ACP (copilot→worker) | nexusd's own NexusFS | StdioPipe + JSON-RPC | Copilot spawns specialist workers |

V1 implements Embedded + Remote. API spawn and ACP already exist in infra.

**Copilot / Worker model**: `nexus chat` starts a **Copilot** — the agent that
interacts with the human. Copilot can spawn **Workers** via ACP for specialist
tasks (CC, cursor, custom tools). Workers are subprocess agents with restricted
tool sets.

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
  │   NO  → Boot embedded NexusFS (invocation-based, exclusive to this process):
  │         1. Resolve deployment profile: --deployment-profile > NEXUS_PROFILE env > "cluster"
  │         2. create_nexus_fs(profile=resolved, backend=CASLocalBackend(~/.nexus/data))
  │         3. Mount LLM backend: sys_setattr("/llm", DT_MOUNT, CASOpenAIBackend(...))
  │         4. Inject StreamManager into backend
  │         NexusFS lifecycle = process lifetime. No nexusd required.
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

**Layer: Agent + Framework | P0 | DONE (V1)**

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

### Done (designed + implemented):

6. ~~**Context Compression** (§4.1)~~ — DONE (CompactionStrategy + DefaultCompactionStrategy, 15 tests)
7. ~~**System Prompt Assembly** (§4.2)~~ — DONE (assemble_system_prompt + vfs_paths, 9 tests)
8. ~~**REPL + CLI** (§11.2 + §13.2)~~ — DONE (`nexus chat`, interactive REPL + one-shot, embedded/remote modes, V1 slash commands)
9. ~~**External tool discovery (Tier B)** (§1.5)~~ — DONE (`--tools PATH` → DT_MOUNT to `/root/tools/{name}`)
10. ~~**Tool Protocol extension** (§2.1)~~ — DONE (max_result_size_chars, is_destructive, should_defer)
11. ~~**Parallel execution** (§2.3)~~ — DONE (ConcurrencyPolicy + ExclusiveLockPolicy, wired into managed_loop)
12. ~~**Tool result handling** (§2.4)~~ — DONE (HeadTruncation + DefaultMessageBudget 50K/200K + VFSToolResultStorage)
13. ~~**Permission pipeline** (§3.1)~~ — DONE V1 (RuleBasedPermissionService, wildcard pattern matching)
14. ~~**Bash security** (§3.3)~~ — DONE V1 (BashCommandValidator, 23 categories)

### To implement next (designed, ready for implementation):

15. **Sudowork ACP integration** (§4A) — ACP JSON-RPC transport + protocol handler + push observer + tool lifecycle + --acp flag

### Deferred Items (not in current scope):
- Multi-agent teams (§5.2, P1)
- Skill loading (§7.1, P1)
- MCP integration (§10.1, P2)
- TUI polish (§11.1, P1)
- Worktree isolation (§8.1, P1)
- Feature flags (§12.1, P2)
- Semantic search in CLUSTER profile (P1 — needs BRICK_SEARCH opt-in)
- Rust acceleration for permission/bash security (swap Python impl → Rust, same Protocol)

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
