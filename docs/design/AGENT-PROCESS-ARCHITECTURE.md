# Agent Process Architecture: pi-mono on Nexus Kernel

**Status:** Draft
**Authors:** Design Doc
**Goal:** Single agent process running on Nexus, deeply integrated via kernel syscalls

---

## 1. Executive Summary

This document defines how a **pi-mono agent loop** becomes a **first-class process** in Nexus,
modeled after Linux's process abstraction. Every agent I/O operation — file read/write,
memory access, tool execution, IPC — routes through Nexus kernel syscalls. The agent
doesn't touch the outside world directly; Nexus IS its operating system.

**First milestone:** One agent process that can receive a prompt, run a tool loop,
read/write files via NexusFS, persist conversation state to CAS, and be managed
(start/stop/suspend/resume) via the existing AgentRegistry.

---

## 2. The Linux Process Model Mapping

### 2.1 Conceptual Mapping

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LINUX                    NEXUS               │
├─────────────────────────────────────────────────────────────────────┤
│  Process (task_struct)              →  AgentProcess                 │
│  PID                                →  agent_id (from AgentRecord)  │
│  PPID (parent process)              →  parent_agent_id (sub-agents) │
│  Process state (RUNNING/SLEEPING)   →  AgentPhase (ACTIVE/IDLE/...) │
│  Address space (mm_struct)          →  AgentContext (messages+tools) │
│  Stack                              →  conversation history         │
│  Heap                               →  working memory (MEMORY.md)   │
│  File descriptor table (fd_table)   →  AgentFileTable (open files)  │
│  Current working directory          →  cwd (agent namespace root)   │
│  Environment variables              →  agent metadata + settings    │
│  Credentials (uid/gid/caps)         →  OperationContext (owner_id,  │
│                                        zone_id, permissions)        │
│  Signal handlers                    →  steering message handlers    │
│  CPU time slice                     →  LLM inference turn           │
│  Scheduler (CFS/RT)                 →  SchedulerProtocol (Astraea)  │
│  fork()                             →  spawn_child_agent()          │
│  exec()                             →  load_agent_config()          │
│  exit()                             →  agent_terminate()            │
│  wait()                             →  await child completion       │
│  kill(SIGTERM)                      →  steering message "stop"      │
│  kill(SIGSTOP/SIGCONT)              →  suspend/resume transitions   │
│  Core dump                          →  session JSONL checkpoint     │
│  /proc/PID/*                        →  /__proc__/<agent_id>/*       │
│  pipe(2)                            →  DT_PIPE (RingBuffer)         │
│  mmap (shared memory)               →  shared namespace paths       │
│  ulimit                             →  AgentResources (token_budget,│
│                                        storage_limit, context_limit)│
│  nice/renice                        →  QoS class (premium/standard/ │
│                                        spot)                        │
│  CPU (instruction execution)        →  LLM provider (inference)     │
│  Syscall table (sys_call_table)     →  NexusSyscallTable            │
│  errno                              →  NexusError hierarchy         │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Execution Model

In Linux, a process runs on a CPU. In Nexus, an agent process "runs on" an LLM:

```
Linux:                              Nexus:
┌──────────┐                        ┌──────────────┐
│  Process  │  scheduled on →  CPU  │ AgentProcess │  dispatched to →  LLM
│  (code)   │  ← interrupt         │ (prompt+ctx) │  ← tool results
│           │  → syscall           │              │  → syscall (tool call)
│           │  ← return            │              │  ← result
└──────────┘                        └──────────────┘

CPU fetch-decode-execute cycle:     Agent loop cycle:
1. Fetch instruction                1. Send context to LLM
2. Decode opcode                    2. LLM decides action
3. Execute (may trap to kernel)     3. If tool call → trap to Nexus kernel
4. Return to user mode              4. Return tool result, continue loop
```

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  AGENT USERSPACE                                                        │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  pi-mono Agent Loop (adapted)                                    │    │
│  │  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────────┐  │    │
│  │  │ System   │  │ Context  │  │ Tool     │  │ Event Stream    │  │    │
│  │  │ Prompt   │  │ Manager  │  │ Executor │  │ (AgentEvent)    │  │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬────────┘  │    │
│  └───────┼──────────────┼────────────┼──────────────────┼───────────┘    │
│          │              │            │                   │               │
│          │         ┌────▼────────────▼──────────┐       │               │
│          │         │   NEXUS SYSCALL BOUNDARY    │       │               │
│          │         │   (NexusSyscallTable)       │       │               │
│          │         └────┬────────────┬──────────┘       │               │
└──────────┼──────────────┼────────────┼──────────────────┼───────────────┘
           │              │            │                   │
┌──────────▼──────────────▼────────────▼──────────────────▼───────────────┐
│  NEXUS KERNEL                                                           │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐  │
│  │ VFS          │  │ Process      │  │ Scheduler    │  │ IPC        │  │
│  │ (11 syscalls)│  │ Manager      │  │ (Astraea)    │  │ (Pipes,    │  │
│  │              │  │ (NEW)        │  │              │  │  Events)   │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘  │
│         │                 │                  │                │         │
│  ┌──────▼─────────────────▼──────────────────▼────────────────▼──────┐  │
│  │  Storage Pillars                                                  │  │
│  │  ┌────────────┐ ┌─────────────┐ ┌─────────────┐ ┌────────────┐  │  │
│  │  │ Metastore  │ │ ObjectStore │ │ RecordStore  │ │ CacheStore │  │  │
│  │  │ (inodes)   │ │ (CAS blobs) │ │ (relational) │ │ (ephemeral)│  │  │
│  │  └────────────┘ └─────────────┘ └─────────────┘ └────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  BRICKS (Agent Services)                                         │   │
│  │  ┌─────┐ ┌────────┐ ┌──────┐ ┌─────────┐ ┌─────┐ ┌──────────┐ │   │
│  │  │ LLM │ │ Memory │ │Search│ │ Sandbox │ │ Pay │ │ Workflows│ │   │
│  │  └─────┘ └────────┘ └──────┘ └─────────┘ └─────┘ └──────────┘ │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. The AgentProcess: Process Descriptor (`task_struct`)

### 4.1 New Contract: `AgentProcess`

This is the kernel's view of a running agent — analogous to Linux's `task_struct`.

```
Location: contracts/agent_process.py (NEW)
```

```python
@dataclass(frozen=True, slots=True)
class AgentProcess:
    """Kernel process descriptor for a running agent.

    Linux analogue: task_struct

    This is the minimal kernel-visible representation. The full agent
    state (conversation history, tools, etc.) lives in userspace
    (pi-mono's AgentSession). The kernel only tracks what it needs
    for scheduling, resource accounting, and lifecycle.
    """
    # Identity (like pid, tgid, comm)
    pid: str                          # = agent_id from AgentRecord
    ppid: str | None                  # parent agent (for sub-agents)
    name: str                         # human-readable (like /proc/PID/comm)
    owner_id: str                     # uid equivalent
    zone_id: str                      # namespace/cgroup equivalent

    # Lifecycle (like task state flags)
    phase: AgentPhase                 # WARMING/READY/ACTIVE/THINKING/IDLE/...
    generation: int                   # session generation (like exec counter)

    # Resource accounting (like rlimit, cgroup limits)
    resources: AgentResources         # token_budget, storage_limit, context_limit
    qos: AgentQoS                     # scheduling class + eviction class

    # File system state (like fs_struct)
    cwd: str                          # current working directory in NexusFS
    root: str                         # root directory (namespace isolation)

    # File descriptor table (like files_struct)
    fd_table: tuple[FileDescriptor, ...]  # open file handles

    # Execution context
    model: str                        # LLM model binding ("claude-sonnet-4-6")
    system_prompt_path: str           # path to SYSTEM.md in NexusFS

    # Checkpoint state (for suspend/resume)
    checkpoint_path: str | None       # path to session JSONL in NexusFS

    # Timestamps
    created_at: datetime
    last_scheduled: datetime | None   # last time LLM was called

    # Sub-process tracking (like children list)
    children: tuple[str, ...] = ()    # child agent PIDs


@dataclass(frozen=True, slots=True)
class FileDescriptor:
    """An open file handle in the agent's fd table.

    Linux analogue: struct file + file descriptor integer.
    """
    fd: int                           # file descriptor number
    path: str                         # NexusFS path
    mode: str                         # "r", "w", "rw"
    offset: int = 0                   # current read/write position
```

### 4.2 Storage Affinity

| AgentProcess field | Storage Pillar | Rationale |
|---|---|---|
| Identity (pid, owner, zone) | RecordStore | Relational, JOINable with ReBAC |
| Phase, generation | RecordStore | Extends existing AgentRecord |
| Resource usage | CacheStore | Ephemeral counters, TTL |
| fd_table | Process heap (in-memory) | Per-session, not persisted |
| cwd, root | RecordStore | Part of agent metadata |
| Checkpoint (session JSONL) | ObjectStore (CAS) | Immutable blob, content-addressed |
| System prompt | ObjectStore (CAS) + Metastore (inode) | Normal file in NexusFS |

---

## 5. Nexus Syscall Table: Agent Tool → Kernel Call Mapping

Every pi-mono tool call becomes a Nexus kernel syscall. The agent NEVER touches
the host filesystem or network directly — everything goes through Nexus.

### 5.1 Tool → Syscall Mapping

```
┌─────────────────────────────────────────────────────────────────────────┐
│  pi-mono Tool        →   Nexus Syscall          →   Kernel Path        │
├─────────────────────────────────────────────────────────────────────────┤
│  read(path)          →   sys_read(path)          →   VFS → ObjectStore │
│  write(path, content)→   sys_write(path, bytes)  →   VFS → ObjectStore │
│  edit(path, old, new)→   sys_read + sys_write    →   VFS (atomic R/W)  │
│  bash(cmd)           →   sys_exec(cmd)           →   SandboxProtocol   │
│  grep(pattern, path) →   sys_search(query)       →   SearchBrick       │
│  find(pattern)       →   sys_readdir(recursive)  →   VFS → Metastore   │
│  ls(path)            →   sys_readdir(path)       →   VFS → Metastore   │
│                                                                         │
│  --- NEW AGENT-SPECIFIC SYSCALLS ---                                    │
│  memory_store(key,v) →   sys_mem_write           →   MemoryBrick       │
│  memory_search(q)    →   sys_mem_search          →   MemoryBrick       │
│  agent_spawn(config) →   sys_fork + sys_exec     →   ProcessManager    │
│  agent_send(pid,msg) →   sys_pipe_write          →   PipeManager       │
│  agent_recv()        →   sys_pipe_read           →   PipeManager       │
│  pay(amount, to)     →   sys_pay                 →   PayBrick          │
│  llm_call(prompt)    →   sys_llm_read            →   LLMBrick          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.2 New Kernel Contract: `ProcessManagerProtocol`

```
Location: contracts/protocols/process_manager.py (NEW)
```

```python
@runtime_checkable
class ProcessManagerProtocol(Protocol):
    """Kernel contract for agent process lifecycle.

    Linux analogue: kernel/fork.c + kernel/exit.c + kernel/signal.c

    Manages agent processes: creation (fork/exec), termination (exit),
    signaling (steering messages), and parent-child relationships.
    """

    async def spawn(
        self,
        owner_id: str,
        zone_id: str,
        *,
        config: "AgentProcessConfig",
        parent_pid: str | None = None,
    ) -> "AgentProcess":
        """Create a new agent process (fork+exec).

        Allocates PID, creates fd_table, sets cwd, registers with
        AgentRegistry, and starts the agent loop.
        """
        ...

    async def terminate(
        self, pid: str,
        *, exit_code: int = 0,
    ) -> None:
        """Terminate an agent process (exit).

        Closes all fds, persists checkpoint, notifies parent,
        cleans up resources.
        """
        ...

    async def signal(
        self, pid: str,
        signal: "AgentSignal",
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Send a signal to an agent process.

        SIGSTOP → suspend (transition to SUSPENDED)
        SIGCONT → resume (transition to CONNECTED)
        SIGTERM → graceful shutdown (steering message "stop")
        SIGKILL → immediate termination
        SIGUSR1 → steering message injection
        """
        ...

    async def wait(self, pid: str, *, timeout: float | None = None) -> int:
        """Wait for child process termination (waitpid).

        Returns exit code. Blocks until child terminates or timeout.
        """
        ...

    async def get_process(self, pid: str) -> "AgentProcess | None":
        """Read process descriptor (/proc/PID/status equivalent)."""
        ...

    async def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
        parent_pid: str | None = None,
    ) -> list["AgentProcess"]:
        """List processes (ps equivalent)."""
        ...

    async def checkpoint(self, pid: str) -> str:
        """Checkpoint process state to CAS (CRIU equivalent).

        Serializes session JSONL → sys_write to CAS.
        Returns checkpoint path in NexusFS.
        """
        ...

    async def restore(self, checkpoint_path: str) -> "AgentProcess":
        """Restore process from checkpoint (CRIU restore).

        Reads session JSONL from CAS, reconstructs AgentProcess.
        """
        ...
```

### 5.3 New Kernel Contract: `AgentSyscallTable`

This is the boundary between agent userspace and Nexus kernel — analogous to
Linux's `sys_call_table`. Each pi-mono tool invocation is dispatched through this table.

```
Location: contracts/protocols/agent_syscall.py (NEW)
```

```python
@runtime_checkable
class AgentSyscallProtocol(Protocol):
    """Syscall dispatch table for agent tool calls.

    Linux analogue: arch/x86/entry/syscall_64.c (sys_call_table)

    Every tool call from the agent loop enters here. The dispatcher
    validates permissions (ReBAC check), accounts resources (token/storage),
    and routes to the appropriate kernel subsystem.
    """

    async def sys_read(
        self, ctx: OperationContext, path: str,
        *, offset: int = 0, limit: int | None = None,
    ) -> bytes:
        """Read file content. Route: VFS → ObjectStore."""
        ...

    async def sys_write(
        self, ctx: OperationContext, path: str, content: bytes,
    ) -> "WriteResult":
        """Write file content. Route: VFS → ObjectStore."""
        ...

    async def sys_readdir(
        self, ctx: OperationContext, path: str,
        *, recursive: bool = False, pattern: str | None = None,
    ) -> list["FileMetadata"]:
        """List directory. Route: VFS → Metastore."""
        ...

    async def sys_stat(
        self, ctx: OperationContext, path: str,
    ) -> "FileMetadata":
        """Get file metadata. Route: VFS → Metastore."""
        ...

    async def sys_exec(
        self, ctx: OperationContext, command: str,
        *, timeout: int = 300,
    ) -> "ExecResult":
        """Execute command in sandbox. Route: SandboxBrick."""
        ...

    async def sys_search(
        self, ctx: OperationContext, query: str,
        *, path: str | None = None, pattern: str | None = None,
    ) -> list["SearchResult"]:
        """Search file contents. Route: SearchBrick."""
        ...

    async def sys_mem_write(
        self, ctx: OperationContext, key: str, value: str,
        *, embedding: bool = True,
    ) -> str:
        """Store agent memory. Route: MemoryBrick."""
        ...

    async def sys_mem_search(
        self, ctx: OperationContext, query: str,
        *, limit: int = 10,
    ) -> list["MemoryResult"]:
        """Search agent memory (semantic). Route: MemoryBrick."""
        ...

    async def sys_fork(
        self, ctx: OperationContext,
        config: "AgentProcessConfig",
    ) -> str:
        """Spawn child agent process. Route: ProcessManager."""
        ...

    async def sys_pipe_write(
        self, ctx: OperationContext, target_pid: str, message: bytes,
    ) -> int:
        """Write to agent IPC pipe. Route: PipeManager."""
        ...

    async def sys_pipe_read(
        self, ctx: OperationContext,
        *, blocking: bool = True,
    ) -> bytes:
        """Read from agent IPC pipe. Route: PipeManager."""
        ...

    async def sys_pay(
        self, ctx: OperationContext, amount: Decimal, to: str,
    ) -> str:
        """Transfer credits. Route: PayBrick."""
        ...
```

---

## 6. Agent Filesystem Layout

Every agent process gets a namespace-isolated view of NexusFS:

```
/<zone_id>/
├── agents/
│   └── <agent_id>/                    # Agent home directory (cwd)
│       ├── SYSTEM.md                  # System prompt (= pi-mono SYSTEM.md)
│       ├── MEMORY.md                  # Working memory (= pi-mono MEMORY.md)
│       ├── sessions/                  # Session history (JSONL → CAS blobs)
│       │   ├── 2026-03-03_abc123.jsonl
│       │   └── 2026-03-03_def456.jsonl
│       ├── workspace/                 # Agent working files
│       │   ├── src/
│       │   └── output/
│       ├── settings.json              # Agent config (= pi-mono settings.json)
│       └── extensions/                # Agent extensions
│
├── shared/                            # Cross-agent shared namespace
│   ├── knowledge/                     # Shared knowledge base
│   └── artifacts/                     # Shared build artifacts
│
└── __proc__/                          # Process info (virtual, read-only)
    └── <agent_id>/
        ├── status                     # AgentProcess descriptor (JSON)
        ├── fd                         # Open file descriptors
        ├── resources                  # Resource usage
        ├── children                   # Child process list
        └── events                     # Event stream (tail -f)
```

### 6.1 Path Resolution

The agent's `cwd` is set to `/<zone_id>/agents/<agent_id>/` on spawn.
All relative paths in tool calls resolve against this.

The `__proc__` virtual directory is implemented as a PRE-DISPATCH resolver
in `KernelDispatch` (like Linux's procfs), reading live state from
ProcessManager and AgentRegistry.

---

## 7. Agent Loop Integration

### 7.1 Adapted pi-mono Loop

The pi-mono agent loop runs largely unchanged, but its I/O is rewired:

```
                    pi-mono (TypeScript/Node)          Nexus (Python)
                    ========================          ==============

                    ┌─────────────────┐
                    │  Agent Loop     │
                    │  (runLoop)      │
                    │                 │
                    │  1. Get context │
                    │  2. Call LLM ───┼──────────────→ LLMBrick.llm_read()
                    │  3. Parse resp  │                   │
                    │  4. Tool call? ─┼──yes──→ ┌─────────▼──────────┐
                    │       │         │         │ AgentSyscallTable   │
                    │       │ no      │         │ (permission check)  │
                    │       ▼         │         │ (resource account)  │
                    │  5. Done/follow │         │ (route to subsystem)│
                    │       up        │         └─────────┬──────────┘
                    │                 │                    │
                    │  6. Tool result◄┼────────────────────┘
                    │     → add to   │
                    │       context  │
                    │     → continue │
                    └─────────────────┘
```

### 7.2 Bridging: TypeScript ↔ Python

Two viable approaches for the first milestone:

**Option A: RPC Bridge (recommended for v1)**

pi-mono already has an RPC mode (JSON-lines over stdin/stdout). Nexus spawns
pi-mono as a subprocess, communicates via RPC, and intercepts all tool calls:

```
Nexus ProcessManager
    │
    ├── spawn() → subprocess: `pi --mode rpc --model <model>`
    │
    ├── RPC stdin  → {"type": "prompt", "message": "..."}
    │
    ├── RPC stdout ← {"type": "tool_execution_start", "tool": "read", ...}
    │                 ↓
    │            Nexus intercepts tool call
    │            → AgentSyscallTable.sys_read(ctx, path)
    │            → VFS → ObjectStore
    │            → return content
    │                 ↓
    ├── RPC stdin  → {"type": "tool_result", "content": "..."}
    │
    └── RPC stdout ← {"type": "agent_end"}
```

**Option B: Pure Python Re-implementation (future)**

Port pi-mono's minimal agent loop (~500 lines) to Python, running natively
in the Nexus process. Eliminates subprocess overhead and serialization cost.

```python
# Sketch of a Python-native agent loop
async def agent_loop(process: AgentProcess, syscall: AgentSyscallProtocol):
    ctx = process.to_operation_context()
    messages = await load_session(process.checkpoint_path, syscall, ctx)
    tools = build_nexus_tools(syscall, ctx)  # read/write/edit/bash → syscalls

    while True:
        # 1. Check steering messages (signals)
        steering = await check_signals(process.pid)
        if steering:
            messages.extend(steering)

        # 2. Call LLM (= CPU execution)
        response = await syscall.sys_llm_call(ctx, messages, tools)
        messages.append(response)

        # 3. Process tool calls (= syscall traps)
        if response.tool_calls:
            for tc in response.tool_calls:
                result = await syscall.dispatch(ctx, tc.name, tc.arguments)
                messages.append(ToolResult(tc.id, result))
            continue  # back to LLM

        # 4. No tool calls → turn complete
        if not await get_follow_ups(process.pid):
            break

    # Checkpoint on exit
    await syscall.sys_write(ctx, process.checkpoint_path, serialize(messages))
```

---

## 8. Lifecycle: Process States as Linux States

```
                              ┌──────────────────────────────────┐
                              │        AgentProcess States        │
                              └──────────────────────────────────┘

    spawn()                 schedule()              idle (no work)
  ──────────→ [ CREATED ] ──────────→ [ RUNNING ] ──────────→ [ SLEEPING ]
              (WARMING)     ↑          (ACTIVE/     │           (IDLE)
                            │          THINKING)    │              │
                            │              │        │              │
                            │   tool result│   sys_exec()          │ wake (new prompt)
                            │              │        │              │
                            │              ▼        ▼              │
                            │         [ BLOCKED ]                  │
                            │         (waiting for                 │
                            │          sandbox/LLM)                │
                            │              │                       │
                            └──────────────┘                       │
                                                                   │
                         signal(SIGSTOP)                            │
                    ┌────────────────────────────────┐              │
                    ▼                                │              │
              [ STOPPED ]                            │              │
              (SUSPENDED)  ──signal(SIGCONT)──────→──┘              │
                                                                   │
                         signal(SIGTERM)         exit()             │
                    ┌────────────────────────────────┐              │
                    ▼                                               │
              [ ZOMBIE ] ──parent.wait()──→ [ REMOVED ]            │
              (checkpoint                     (unregister)          │
               saved)                                              │
```

### Mapping to existing Nexus AgentState:

| Linux State | Nexus AgentPhase | Trigger |
|---|---|---|
| TASK_RUNNING | ACTIVE / THINKING | Scheduled, LLM responding |
| TASK_INTERRUPTIBLE | IDLE | Waiting for prompt |
| TASK_UNINTERRUPTIBLE | ACTIVE (blocked on I/O) | sys_exec, sys_read on slow backend |
| TASK_STOPPED | SUSPENDED | Admin suspension |
| TASK_ZOMBIE | (no equivalent yet) | **NEW**: TERMINATED (awaiting parent wait) |
| EXIT_DEAD | (unregistered) | Parent reaped, fully cleaned up |

---

## 9. What Needs to Be Built (New Kernel Contracts)

### 9.1 New Contracts

| Contract | File | Linux Analogue | Purpose |
|---|---|---|---|
| `AgentProcess` | `contracts/agent_process.py` | `task_struct` | Process descriptor |
| `FileDescriptor` | `contracts/agent_process.py` | `struct file` | Open file handle |
| `AgentProcessConfig` | `contracts/agent_process.py` | `execve` args | Spawn configuration |
| `AgentSignal` | `contracts/agent_process.py` | `signal.h` | Signal enum |
| `ProcessManagerProtocol` | `contracts/protocols/process_manager.py` | `kernel/fork.c` | Process lifecycle |
| `AgentSyscallProtocol` | `contracts/protocols/agent_syscall.py` | `sys_call_table` | Tool→kernel dispatch |
| `ProcFSResolver` | `core/procfs_resolver.py` | `fs/proc/` | `__proc__` virtual dir |

### 9.2 Extensions to Existing Contracts

| Existing Contract | Change | Rationale |
|---|---|---|
| `AgentRecord` | Add `ppid`, `checkpoint_path`, `cwd` fields | Process descriptor needs parent + fs state |
| `AgentPhase` | Add `TERMINATED` phase | Zombie state for parent notification |
| `AgentRegistryProtocol` | Add `list_children(pid)` method | Process tree traversal |
| `SchedulerProtocol` | No change needed | Already supports priority, QoS, deadline |
| `SandboxProtocol` | No change needed | Already supports create/run/stop |
| `NexusFS` | No change to syscalls | Existing 11 syscalls sufficient for agent I/O |

### 9.3 New Brick: `agent_runtime`

```
Location: bricks/agent_runtime/ (NEW)
```

This brick implements the agent execution engine — the "CPU" that runs agent processes.

```
bricks/agent_runtime/
├── __init__.py
├── runtime.py              # AgentRuntime: main execution engine
├── syscall_dispatcher.py   # AgentSyscallTable implementation
├── process_manager.py      # ProcessManager implementation
├── tool_adapter.py         # pi-mono tool → Nexus syscall adapter
├── session_store.py        # JSONL session ↔ CAS persistence
├── procfs.py               # __proc__ virtual filesystem resolver
└── bridge/
    ├── rpc_bridge.py       # pi-mono RPC mode bridge (Option A)
    └── native_loop.py      # Python-native loop (Option B, future)
```

---

## 10. Data Flow: Complete Request Lifecycle

### Example: Agent reads a file, edits it, runs tests

```
User prompt: "Fix the bug in auth.py and run tests"
                        │
                        ▼
1. ProcessManager.spawn(config) ──────────────────────────────────┐
   │  Creates AgentProcess                                         │
   │  PID = "agent-abc-123"                                       │
   │  cwd = "/zone-1/agents/agent-abc-123/"                       │
   │  Opens session JSONL fd                                       │
   │                                                               │
2. Scheduler.submit(AgentRequest(pid, prompt)) ───────────────────│
   │  Priority: interactive                                        │
   │  Queued in Dragonfly sorted set                              │
   │                                                               │
3. Scheduler.next() → dequeue ────────────────────────────────────│
   │                                                               │
4. AgentRuntime.execute(process, prompt) ─────────────────────────│
   │                                                               │
   │  ┌─ AGENT LOOP (pi-mono) ──────────────────────────────┐    │
   │  │                                                       │    │
   │  │  Turn 1: LLM → tool_call("read", {path: "auth.py"}) │    │
   │  │          │                                            │    │
   │  │          ▼                                            │    │
   │  │  AgentSyscallTable.sys_read(ctx, "auth.py")          │    │
   │  │    → ReBAC check: ctx.owner_id has READ on path? ✓   │    │
   │  │    → VFS.sys_read("/zone-1/agents/agent-abc-123/     │    │
   │  │                     workspace/auth.py")               │    │
   │  │    → Metastore.get(path) → FileMetadata{hash: "a1b2"}│    │
   │  │    → ObjectStore.read_content("a1b2") → bytes        │    │
   │  │    → return content to agent loop                     │    │
   │  │                                                       │    │
   │  │  Turn 2: LLM → tool_call("edit", {path, old, new})  │    │
   │  │          │                                            │    │
   │  │          ▼                                            │    │
   │  │  AgentSyscallTable.sys_read(ctx, path) → old content │    │
   │  │  Apply edit (old_string → new_string)                 │    │
   │  │  AgentSyscallTable.sys_write(ctx, path, new_content) │    │
   │  │    → VFS.sys_write() → ObjectStore.write_content()    │    │
   │  │    → new hash "c3d4", Metastore updated               │    │
   │  │    → FileEvent emitted (OBSERVE phase)                │    │
   │  │                                                       │    │
   │  │  Turn 3: LLM → tool_call("bash", {cmd: "pytest"})   │    │
   │  │          │                                            │    │
   │  │          ▼                                            │    │
   │  │  AgentSyscallTable.sys_exec(ctx, "pytest")           │    │
   │  │    → SandboxProtocol.run_code(sandbox_id, "bash",    │    │
   │  │        "pytest", timeout=300)                         │    │
   │  │    → {stdout: "...", exit_code: 0}                    │    │
   │  │    → return ExecResult to agent loop                  │    │
   │  │                                                       │    │
   │  │  Turn 4: LLM → no tool calls → "Fixed! Tests pass." │    │
   │  │                                                       │    │
   │  └──────────────────────────────────────────────────────┘    │
   │                                                               │
5. Session JSONL → sys_write to CAS (checkpoint)                   │
6. ProcessManager.terminate(pid, exit_code=0)                      │
   │  Close all fds                                                │
   │  AgentRegistry.transition(pid, IDLE)                          │
   │  Notify parent (if sub-agent)                                 │
   └───────────────────────────────────────────────────────────────┘
```

---

## 11. Sub-Agent Spawning (fork + exec)

pi-mono spawns sub-agents by invoking `pi` via bash. In Nexus, this becomes
a proper `fork()`:

```
Parent Agent (PID: parent-001)
    │
    │  tool_call("bash", {cmd: "pi --mode print 'analyze auth.py'"})
    │
    ▼
AgentSyscallTable.sys_exec(ctx, cmd)
    │
    │  Detects "pi" command → intercepts as fork request
    │
    ▼
ProcessManager.spawn(
    owner_id = parent.owner_id,
    zone_id = parent.zone_id,
    config = AgentProcessConfig(
        prompt = "analyze auth.py",
        model = parent.model,        # inherit
        cwd = parent.cwd,            # inherit (like fork)
        mode = "print",              # one-shot
    ),
    parent_pid = "parent-001",
)
    │
    ▼
Child Agent (PID: child-002, PPID: parent-001)
    │  Runs its own agent loop
    │  Has its own fd_table (COW from parent)
    │  Same zone, same permissions
    │  Output captured as ExecResult
    │
    ▼
ProcessManager.terminate(child-002, exit_code=0)
    │  Parent notified
    │  Output returned as tool result
    │
    ▼
Parent Agent continues with child's output
```

---

## 12. IPC: Agent-to-Agent Communication

Uses the existing DT_PIPE (RingBuffer) infrastructure:

```
Agent A (PID: agent-a)                    Agent B (PID: agent-b)
    │                                          │
    │  sys_pipe_write("agent-b", msg)         │
    │       │                                  │
    │       ▼                                  │
    │  PipeManager.pipe_write(                 │  sys_pipe_read()
    │    "/__pipes__/agent-a→agent-b",         │       │
    │    msg_bytes                              │       ▼
    │  )                                       │  PipeManager.pipe_read(
    │       │                                  │    "/__pipes__/agent-a→agent-b"
    │       ▼                                  │  )
    │  RingBuffer.write(msg) ──────────────→  │  RingBuffer.read() → msg
    │                                          │
    │  ~5μs latency (in-process)              │
```

For cross-zone agent communication, the existing A2A brick handles routing
over gRPC federation transport.

---

## 13. Resource Limits (ulimit / cgroups)

```python
@dataclass(frozen=True, slots=True)
class AgentProcessConfig:
    """Configuration for spawning a new agent process.

    Linux analogue: execve() arguments + rlimit settings.
    """
    # Identity
    name: str
    agent_type: str = "coding"     # "coding", "analyst", "reviewer"

    # Execution
    model: str = "claude-sonnet-4-6"
    system_prompt: str | None = None    # custom, or use default
    mode: str = "interactive"           # "interactive", "print", "rpc"
    prompt: str | None = None           # initial prompt (for print mode)

    # Resource limits (ulimit equivalent)
    max_turns: int = 100                # max LLM round-trips
    max_tokens: int = 1_000_000         # total token budget
    max_storage_mb: int = 1024          # max CAS storage
    max_context_tokens: int = 200_000   # context window limit
    max_children: int = 10              # max sub-agent processes
    exec_timeout: int = 3600            # total execution timeout (seconds)
    sandbox_timeout: int = 300          # per-bash-command timeout

    # QoS
    qos_class: QoSClass = QoSClass.STANDARD
    priority: int = 0

    # Filesystem
    cwd: str | None = None              # working directory (default: auto)
    mount_paths: tuple[str, ...] = ()   # additional mounts into namespace

    # Extensions / tools
    tools: tuple[str, ...] = ("read", "write", "edit", "bash")
    extensions: tuple[str, ...] = ()
```

Resource enforcement is done at the syscall boundary:

```
AgentSyscallTable.sys_write(ctx, path, content):
    process = ProcessManager.get_process(ctx.agent_id)

    # Check storage limit
    current_usage = process.resources.storage_used_mb
    new_size = len(content) / (1024 * 1024)
    if current_usage + new_size > process.config.max_storage_mb:
        raise ResourceLimitError("storage limit exceeded")

    # Check permission
    if not await rebac.check(ctx.owner_id, path, Permission.WRITE):
        raise NexusPermissionError(path)

    # Proceed with write
    return await vfs.sys_write(ctx, path, content)
```

---

## 14. The `__proc__` Virtual Filesystem

Implemented as a `VFSPathResolver` in the PRE-DISPATCH phase of `KernelDispatch`:

```python
class ProcFSResolver(VFSPathResolver):
    """Virtual /proc filesystem for agent process introspection.

    Linux analogue: fs/proc/

    Provides read-only virtual files exposing live process state.
    No data is stored — everything is computed on read.
    """

    def matches(self, path: str) -> bool:
        return path.startswith("/__proc__/")

    async def resolve_read(self, path: str, ctx: OperationContext) -> bytes:
        # /__proc__/<pid>/status → JSON of AgentProcess
        # /__proc__/<pid>/fd → JSON list of FileDescriptors
        # /__proc__/<pid>/resources → JSON of resource usage
        # /__proc__/<pid>/children → JSON list of child PIDs
        # /__proc__/<pid>/events → streaming AgentEvent JSONL
        parts = path.split("/")
        pid = parts[2]
        field = parts[3] if len(parts) > 3 else "status"

        process = await self.process_manager.get_process(pid)
        if not process:
            raise NexusFileNotFoundError(path)

        match field:
            case "status": return json.dumps(process.to_dict()).encode()
            case "fd": return json.dumps([fd.to_dict() for fd in process.fd_table]).encode()
            case "resources": return json.dumps(process.resources.to_dict()).encode()
            case "children": return json.dumps(list(process.children)).encode()
            case _: raise NexusFileNotFoundError(path)
```

---

## 15. Deployment Profile

The agent_runtime brick fits into the existing LEGO distro model:

| Profile | agent_runtime | Dependencies |
|---|---|---|
| minimal | no | — |
| embedded | no | — |
| lite | **yes** | agents, scheduler |
| full | **yes** | agents, scheduler, sandbox, memory, search, llm, pay |
| cloud | **yes** | all |

---

## 16. Implementation Phases

### Phase 1: Single Agent Process (First Milestone)

**Goal:** One agent process that reads/writes files via NexusFS

| Step | What | Depends On |
|---|---|---|
| 1.1 | Define `AgentProcess`, `FileDescriptor`, `AgentProcessConfig` contracts | — |
| 1.2 | Define `ProcessManagerProtocol` | 1.1 |
| 1.3 | Define `AgentSyscallProtocol` | 1.1 |
| 1.4 | Implement `ProcessManager` (in-memory, single-node) | 1.2, existing AgentRegistry |
| 1.5 | Implement `AgentSyscallTable` (route to existing VFS) | 1.3, existing NexusFS |
| 1.6 | Implement RPC bridge to pi-mono | 1.4, 1.5 |
| 1.7 | Implement session JSONL ↔ CAS persistence | 1.5 |
| 1.8 | Add `__proc__` resolver to KernelDispatch | 1.4 |
| 1.9 | Integration test: spawn agent → read file → edit → checkpoint | All above |

### Phase 2: Multi-Agent + IPC

| Step | What |
|---|---|
| 2.1 | Sub-agent spawning (fork) via ProcessManager |
| 2.2 | Agent-to-agent pipes (reuse DT_PIPE / RingBuffer) |
| 2.3 | Steering messages as signals |
| 2.4 | Parent-child wait semantics |

### Phase 3: Resource Management + Federation

| Step | What |
|---|---|
| 3.1 | Token budget enforcement at syscall boundary |
| 3.2 | Storage quota enforcement |
| 3.3 | Cross-zone agent migration (checkpoint → restore on remote node) |
| 3.4 | Distributed process table via Raft |

### Phase 4: Native Python Loop (Option B)

| Step | What |
|---|---|
| 4.1 | Port pi-mono core loop (~500 LOC) to Python |
| 4.2 | Eliminate subprocess/RPC overhead |
| 4.3 | Direct async integration with Nexus event loop |

---

## 17. Key Design Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Agent = Process (not thread) | Full isolation, own fd_table, own cwd, own resource limits |
| D2 | All I/O through syscalls | Uniform permission checking, resource accounting, audit logging |
| D3 | RPC bridge first, native loop later | Ship fast with pi-mono as-is; optimize later |
| D4 | Session JSONL in CAS | Content-addressed, immutable, federable, checkpointable |
| D5 | `__proc__` as VFS resolver | Reuses existing KernelDispatch PRE-DISPATCH infrastructure |
| D6 | No new storage pillar | Agent state maps cleanly to existing 4 pillars |
| D7 | Reuse existing AgentRegistry | Extend, don't replace — add ppid + cwd fields |
| D8 | Reuse existing Scheduler | Already has priority, QoS, deadline — no changes needed |
| D9 | Reuse existing PipeManager | DT_PIPE ring buffer is the IPC primitive |
| D10 | Reuse existing SandboxProtocol | bash tool → sandbox execution, already zone-isolated |

---

## 18. Open Questions

1. **LLM provider management**: Should the kernel manage LLM API keys, or delegate to the agent? (Recommendation: kernel manages via existing LLMBrick, agent specifies model preference in AgentProcessConfig)

2. **Context window compaction**: pi-mono handles this internally. Should Nexus offer a kernel-level compaction service, or let the agent handle it? (Recommendation: agent handles, kernel just stores the JSONL)

3. **Extension loading**: pi-mono extensions are TypeScript modules. For the RPC bridge, do we proxy extension discovery through Nexus? (Recommendation: Phase 1 = no extensions, Phase 2 = NexusFS-backed extension directory)

4. **Cost attribution**: When an agent calls `sys_llm_call`, who pays? The agent's owner? The zone? (Recommendation: PayBrick integration — agent has a credit balance, LLM calls deduct from it)
