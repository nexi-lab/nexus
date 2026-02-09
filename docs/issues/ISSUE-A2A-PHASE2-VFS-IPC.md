# Issue: A2A Phase 2 - VFS IPC Integration

**Title**: Integrate A2A tasks with VFS IPC pipes (`/nexus/pipes/`)

**Labels**: `enhancement`, `a2a`, `vfs`, `phase-2`, `architecture`

**Milestone**: Agent OS v1.0

**Depends on**: #1256 (A2A Phase 1 - Protocol Endpoint)

---

## Summary

Integrate the A2A (Agent-to-Agent) protocol with Nexus VFS by mapping A2A tasks to IPC pipe operations at `/nexus/pipes/`. This enables agent-to-agent communication through the native VFS layer, providing persistence, observability, and unified access control via ReBAC.

**Design Reference**: `docs/design/AGENT-OS-DEEP-RESEARCH.md` (lines 1095, 1142-1152, 1184)

**Quote from design doc**:
> "A2A tasks map to VFS IPC paths internally (/ipc/*/inbox). The microkernel doesn't change, only the surface."

---

## Current State (Phase 1)

✅ **Implemented in #1256**:
- Agent Card discovery: `GET /.well-known/agent.json`
- JSON-RPC 2.0 dispatch: `POST /a2a`
- Task state machine: submitted→working→input-required→completed/failed/canceled
- SSE streaming for real-time updates
- PostgreSQL `a2a_tasks` table storage
- Zone-based multi-tenancy
- Auth enforcement (follows ServiceNow, LangSmith patterns)

**Current limitations**:
- ❌ Tasks stored only in PostgreSQL (not VFS)
- ❌ No integration with `/ipc/` paths
- ❌ No VFS-based event streaming
- ❌ Cannot use VFS file watching for SSE
- ❌ Task artifacts not stored as files

---

## Goals (Phase 2)

### 1. **VFS IPC Pipe Infrastructure**

Implement ring buffer-based pipe inodes for IPC:

```
/nexus/pipes/
├─ agents/
│  ├─ agent-a/
│  │  ├─ inbox         # DT_PIPE inode (ring buffer)
│  │  ├─ outbox        # DT_PIPE inode
│  │  └─ status        # DT_REG (agent status)
│  └─ agent-b/
│     └─ inbox
├─ tasks/
│  ├─ {task-id}/
│  │  ├─ task.json     # Task metadata
│  │  ├─ messages/     # Message history
│  │  └─ artifacts/    # File attachments
│  └─ index/
│     ├─ by-zone/
│     ├─ by-agent/
│     └─ by-state/
└─ broadcast/          # Team/org broadcast channels
   └─ team-x
```

**Pipe inode requirements**:
- Type: `DT_PIPE` (new inode type)
- Backend: Ring buffer (fixed-size circular buffer)
- Semantics: Blocking read/write with wait queues
- Persistence: Survive process restarts (backed by Raft)
- Network transparency: Raft replication for distributed agents
- Observable: `cat /nexus/pipes/agents/agent-b/inbox` shows pending messages

### 2. **Map A2A Operations to VFS**

| A2A Method | Current (Phase 1) | Phase 2 (VFS) |
|------------|-------------------|---------------|
| `a2a.tasks.send` | `INSERT INTO a2a_tasks` | `write("/nexus/pipes/agents/{agent}/inbox", task_json)` |
| `a2a.tasks.get` | `SELECT FROM a2a_tasks` | `read("/nexus/pipes/tasks/{task_id}/task.json")` |
| `a2a.tasks.subscribeToTask` | asyncio.Queue + SSE | `subscribe("/nexus/pipes/tasks/{task_id}/*")` |
| Task artifacts | JSON array in DB | `write("/nexus/pipes/tasks/{task_id}/artifacts/{name}")` |
| Message history | JSON array in DB | `write("/nexus/pipes/tasks/{task_id}/messages/{seq}.json")` |

**Example flows**:

```python
# Agent A sends task to Agent B
await nx.write(
    "/nexus/pipes/agents/agent-b/inbox",
    json.dumps({
        "taskId": "task-123",
        "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]}
    })
)

# Agent B receives (blocking read)
task_json = await nx.read("/nexus/pipes/own/inbox")  # Blocks until message available

# Agent B processes and updates task
await nx.write(
    "/nexus/pipes/tasks/task-123/task.json",
    json.dumps({"status": {"state": "working", "timestamp": "..."}})
)

# Client subscribes to task updates (SSE)
async for event in nx.subscribe("/nexus/pipes/tasks/task-123/*"):
    print(f"Task updated: {event}")
```

### 3. **Security via Namespace Manager**

**ReBAC-based access control**:
- If `/nexus/pipes/agents/agent-b/` not in your namespace → cannot send
- Read-only agents: ReBAC grants read on `/nexus/pipes/agents/*/outbox` only
- Zone isolation: `/nexus/pipes/agents/` scoped by zone_id
- Audit trail: All IPC operations logged in Event Log

**Permission checks**:
```python
# Can agent-a send to agent-b?
has_grant = rebac.check(
    subject=("agent", "agent-a"),
    action="write",
    resource="/nexus/pipes/agents/agent-b/inbox"
)
```

### 4. **Backward Compatibility**

**Migration path**:
- Keep PostgreSQL `a2a_tasks` table as read-only archive
- New tasks go to VFS pipes
- Existing tasks readable via legacy API
- Feature flag: `NEXUS_A2A_VFS_ENABLED` (default: false initially)

**Dual-mode operation** (during migration):
```python
if config.a2a_vfs_enabled:
    # Phase 2: VFS-backed
    await vfs_task_manager.create_task(...)
else:
    # Phase 1: DB-backed (current)
    await db_task_manager.create_task(...)
```

---

## Technical Design

### 1. **New Inode Type: `DT_PIPE`**

**File**: `src/nexus/storage/metadata.py`

```python
class InodeType(str, Enum):
    """Inode types in Nexus VFS."""
    FILE = "file"              # DT_REG (regular file)
    DIRECTORY = "directory"    # DT_DIR
    SYMLINK = "symlink"        # DT_LNK
    PIPE = "pipe"              # DT_PIPE (NEW for IPC)
    SOCKET = "socket"          # DT_SOCK (future)
```

**Metadata fields**:
```python
@dataclass
class PipeMetadata:
    """Metadata for DT_PIPE inodes."""
    buffer_size: int          # Ring buffer capacity (default: 4096 bytes)
    blocking_mode: bool       # Block on empty read / full write
    readers: list[str]        # Active reader agent IDs
    writers: list[str]        # Active writer agent IDs
    created_at: datetime
    last_read_at: datetime | None
    last_write_at: datetime | None
```

### 2. **Ring Buffer Backend**

**File**: `src/nexus/backends/pipe_buffer.py` (new)

**Interface**:
```python
class RingBuffer:
    """Fixed-size circular buffer for pipe inodes."""

    def __init__(self, capacity: int = 4096):
        self.capacity = capacity
        self.buffer = bytearray(capacity)
        self.read_pos = 0
        self.write_pos = 0
        self.size = 0  # Current bytes available
        self.wait_queue = asyncio.Queue()  # For blocking operations

    async def write(self, data: bytes) -> int:
        """Write to buffer. Blocks if full and blocking_mode=True."""
        if self.size + len(data) > self.capacity:
            if blocking_mode:
                await self.wait_queue.get()  # Wait for space
            else:
                raise BufferFullError()

        # Write to ring buffer (handle wraparound)
        # Update write_pos, size
        # Notify readers via wait_queue
        return len(data)

    async def read(self, nbytes: int) -> bytes:
        """Read from buffer. Blocks if empty and blocking_mode=True."""
        if self.size == 0:
            if blocking_mode:
                await self.wait_queue.get()  # Wait for data
            else:
                raise BufferEmptyError()

        # Read from ring buffer (handle wraparound)
        # Update read_pos, size
        # Notify writers via wait_queue
        return data

    def peek(self) -> bytes:
        """Non-blocking peek at available data."""
        return self.buffer[self.read_pos:self.read_pos + self.size]
```

**Performance considerations**:
- **Rust implementation** for production (via `nexus_fast` Rust extension)
- Python fallback for development/testing
- Memory-mapped buffers for zero-copy reads
- Lock-free ring buffer using atomic operations

### 3. **VFS Pipe Driver**

**File**: `src/nexus/backends/pipe_backend.py` (new)

```python
class PipeBackend(Backend):
    """Backend for /nexus/pipes/ namespace."""

    def __init__(self):
        self.pipes: dict[str, RingBuffer] = {}  # path -> buffer
        self.metadata: dict[str, PipeMetadata] = {}

    async def read(self, path: str, context: OperationContext) -> bytes:
        """Read from pipe (blocking until data available)."""
        # Check permissions via ReBAC
        if not self._check_permission(context, path, "read"):
            raise PermissionDenied()

        # Get or create ring buffer
        buffer = self.pipes.get(path)
        if not buffer:
            raise FileNotFoundError()

        # Blocking read
        data = await buffer.read(nbytes=4096)

        # Update last_read_at
        self.metadata[path].last_read_at = datetime.now(UTC)

        return data

    async def write(self, path: str, data: bytes, context: OperationContext) -> int:
        """Write to pipe (blocking if full)."""
        # Check permissions via ReBAC
        if not self._check_permission(context, path, "write"):
            raise PermissionDenied()

        # Get or create ring buffer
        buffer = self._get_or_create_pipe(path)

        # Blocking write
        bytes_written = await buffer.write(data)

        # Update last_write_at
        self.metadata[path].last_write_at = datetime.now(UTC)

        # Trigger file watch events
        await self._notify_watchers(path, "modified")

        return bytes_written

    async def subscribe(self, pattern: str, context: OperationContext) -> AsyncGenerator[FileEvent, None]:
        """Subscribe to pipe events (SSE streaming)."""
        # Use VFS file watcher infrastructure
        async for event in self._watch_files(pattern):
            yield event
```

### 4. **A2A Router Integration**

**File**: `src/nexus/a2a/vfs_task_manager.py` (new)

```python
class VFSTaskManager:
    """Task manager backed by VFS pipes (Phase 2)."""

    def __init__(self, nexus_fs: NexusFS):
        self.fs = nexus_fs
        self.pipes_root = "/nexus/pipes"

    async def create_task(
        self,
        message: Message,
        zone_id: str,
        agent_id: str | None,
        context_id: str | None,
    ) -> Task:
        """Create task and deliver to agent's inbox via VFS pipe."""
        task = Task(
            id=str(uuid.uuid4()),
            contextId=context_id or str(uuid.uuid4()),
            status=TaskStatus(state=TaskState.SUBMITTED),
            history=[message],
        )

        # Write task metadata to VFS
        task_path = f"{self.pipes_root}/tasks/{task.id}/task.json"
        await self.fs.write(task_path, task.model_dump_json().encode())

        # Deliver to agent's inbox
        if agent_id:
            inbox_path = f"{self.pipes_root}/agents/{agent_id}/inbox"
            await self.fs.write(
                inbox_path,
                json.dumps({
                    "taskId": task.id,
                    "event": "task_created",
                    "timestamp": datetime.now(UTC).isoformat(),
                }).encode()
            )

        return task

    async def get_task(self, task_id: str, zone_id: str) -> Task:
        """Retrieve task from VFS."""
        task_path = f"{self.pipes_root}/tasks/{task_id}/task.json"
        data = await self.fs.read(task_path)
        return Task.model_validate_json(data)

    async def subscribe_to_task(self, task_id: str, zone_id: str) -> AsyncGenerator[TaskEvent, None]:
        """Subscribe to task updates via VFS file watching."""
        pattern = f"{self.pipes_root}/tasks/{task_id}/*"
        async for event in self.fs.subscribe(pattern):
            # Parse file event into TaskEvent
            if event.path.endswith("task.json"):
                task = await self.get_task(task_id, zone_id)
                yield TaskStatusUpdateEvent(task=task, final=task.status.state in TERMINAL_STATES)
            elif "artifacts" in event.path:
                # Artifact added
                artifact = await self._load_artifact(event.path)
                yield TaskArtifactUpdateEvent(task=..., artifact=artifact)
```

**Migration in router**:
```python
# src/nexus/a2a/router.py
def build_router(
    *,
    nexus_fs: NexusFS | None = None,
    config: NexusConfig | None = None,
    base_url: str | None = None,
    task_manager: TaskManager | None = None,
    use_vfs: bool = False,  # NEW: Phase 2 flag
) -> APIRouter:
    if use_vfs and nexus_fs:
        # Phase 2: VFS-backed task manager
        task_manager = VFSTaskManager(nexus_fs)
    elif task_manager is None:
        # Phase 1: DB-backed (current)
        task_manager = TaskManager()

    # Rest of router setup...
```

---

## Implementation Plan

### **Step 1: VFS Pipe Infrastructure** (P2.1)

- [ ] Add `InodeType.PIPE` to metadata schema
- [ ] Implement `RingBuffer` class (Python prototype)
- [ ] Implement `PipeBackend` driver
- [ ] Add `/nexus/pipes/` namespace registration
- [ ] Unit tests for ring buffer operations

**Deliverable**: Can create/read/write pipe inodes via VFS

### **Step 2: A2A VFS Integration** (P2.2)

- [ ] Implement `VFSTaskManager` class
- [ ] Map `a2a.tasks.send` → `write("/nexus/pipes/agents/{agent}/inbox")`
- [ ] Map `a2a.tasks.get` → `read("/nexus/pipes/tasks/{task_id}/task.json")`
- [ ] Map `a2a.tasks.subscribeToTask` → `subscribe("/nexus/pipes/tasks/{task_id}/*")`
- [ ] Store artifacts as files in `/nexus/pipes/tasks/{task_id}/artifacts/`
- [ ] Integration tests for VFS-backed A2A operations

**Deliverable**: A2A tasks work via VFS (parallel to DB backend)

### **Step 3: Feature Flag & Migration** (P2.3)

- [ ] Add `NEXUS_A2A_VFS_ENABLED` environment variable
- [ ] Implement dual-mode operation (DB vs VFS)
- [ ] Migration script: `a2a_tasks` table → VFS files
- [ ] Performance benchmarks (DB vs VFS)
- [ ] Update documentation

**Deliverable**: Production-ready VFS mode with migration path

### **Step 4: Rust Optimization** (P2.4)

- [ ] Implement ring buffer in Rust (`nexus_fast` extension)
- [ ] Memory-mapped buffer support
- [ ] Lock-free atomic operations
- [ ] Python bindings via PyO3
- [ ] Performance benchmarks vs Python implementation

**Deliverable**: Production-grade ring buffer performance

---

## Testing Requirements

### Unit Tests

- [ ] Ring buffer: write/read/wraparound/overflow
- [ ] Blocking semantics: wait queues, timeouts
- [ ] Concurrent access: multiple readers/writers
- [ ] Pipe metadata: creation, updates, persistence

### Integration Tests

- [ ] A2A task creation → VFS write
- [ ] Task retrieval → VFS read
- [ ] SSE streaming → VFS subscribe
- [ ] Artifact storage → VFS files
- [ ] Cross-agent communication via pipes

### E2E Tests

- [ ] Two agents communicating via `/nexus/pipes/`
- [ ] Task lifecycle: submit → working → completed (all via VFS)
- [ ] Zone isolation: agent-a in zone-1 cannot write to agent-b in zone-2
- [ ] Auth enforcement: unauthorized writes rejected by ReBAC

### Performance Tests

- [ ] Throughput: messages/second through pipes
- [ ] Latency: end-to-end task delivery time
- [ ] Memory usage: ring buffer overhead
- [ ] Comparison: VFS vs DB backend

---

## Success Criteria

✅ **Functional**:
- [ ] A2A tasks stored in `/nexus/pipes/tasks/`
- [ ] Agent inboxes work via `/nexus/pipes/agents/{agent}/inbox`
- [ ] SSE streaming via VFS `subscribe()`
- [ ] Artifacts stored as VFS files
- [ ] All existing A2A tests pass with VFS backend

✅ **Performance**:
- [ ] VFS backend matches or exceeds DB backend latency
- [ ] Ring buffer handles 10k+ messages/second
- [ ] Blocking read/write latency < 1ms

✅ **Security**:
- [ ] ReBAC permissions enforced for all pipe operations
- [ ] Zone isolation prevents cross-zone communication
- [ ] Audit log records all IPC events

✅ **Observability**:
- [ ] Can `cat /nexus/pipes/agents/agent-b/inbox` to inspect queue
- [ ] Pipe metrics exposed: buffer usage, reader/writer counts
- [ ] Event log shows all task state transitions

---

## Open Questions

1. **Ring buffer size**: Default 4KB per pipe, or configurable per-agent?
2. **Overflow behavior**: Block (current design) or drop messages? Configurable?
3. **Message format**: JSON (current) or binary protocol (e.g., protobuf)?
4. **Retention policy**: How long to keep completed tasks in `/nexus/pipes/tasks/`?
5. **Raft replication**: Synchronous (strong consistency) or async (eventual)?
6. **Migration strategy**: Big-bang cutover or gradual rollout with A/B testing?

---

## References

- **Design doc**: `docs/design/AGENT-OS-DEEP-RESEARCH.md`
- **Phase 1 plan**: `plan.md` (lines 240-243)
- **Federation memo**: `docs/design/federation-memo.md` (Section 7m)
- **A2A spec**: https://a2a-protocol.org/latest/specification/
- **Phase 1 issue**: #1256

---

## Estimated Effort

| Phase | Effort | Dependencies |
|-------|--------|--------------|
| P2.1 (Pipe infra) | 2-3 weeks | None |
| P2.2 (A2A integration) | 1-2 weeks | P2.1 |
| P2.3 (Migration) | 1 week | P2.2 |
| P2.4 (Rust optimization) | 2-3 weeks | P2.1 |

**Total**: 6-9 weeks for complete Phase 2 implementation
