"""Agent runtime contracts — protocols and value types for Issue #2761.

Defines the ProcessManager, ToolDispatcher, SessionStore, and agent_loop
contracts for the multi-agent orchestration layer. These are tier-neutral
types with zero runtime dependencies on kernel, services, or bricks.

Linux process model mapping:
    AgentProcess   = task_struct  (process descriptor)
    ProcessManager = do_fork/do_exit (lifecycle management)
    ToolDispatcher = sys_call_table (tool routing + permission checks)
    SessionStore   = core dump / checkpoint (CAS-backed state persistence)

See: docs/architecture/AGENT-PROCESS-ARCHITECTURE.md
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from nexus.contracts.exceptions import NexusError
from nexus.contracts.llm_types import Message

# ======================================================================
# Exceptions
# ======================================================================


class StepAction(StrEnum):
    """Outcome of a single agent_step() turn."""

    CONTINUE = "continue"  # More turns needed (tool calls dispatched)
    DONE = "done"  # Final response produced (no tool calls)
    ERROR = "error"  # LLM call failed


@dataclass(frozen=True, slots=True)
class StepResult:
    """Result of a single agent_step() turn.

    Immutable snapshot: the caller (agent_loop or scheduler) decides
    what to do next based on the action field.
    """

    action: StepAction
    messages: tuple[Message, ...]  # Full message list after this turn
    turn: int  # Current turn number
    error: str | None = None  # Error message if action == ERROR


# ======================================================================
# Exceptions
# ======================================================================


class ProcessError(NexusError):
    """Base exception for agent process operations."""

    is_expected = False


class ProcessNotFoundError(ProcessError):
    """Raised when a process ID does not exist."""

    is_expected = True
    status_code = 404
    error_type = "Not Found"

    def __init__(self, pid: str):
        self.pid = pid
        super().__init__(f"Process not found: {pid}")


class ProcessAlreadyRunningError(ProcessError):
    """Raised when attempting to spawn a duplicate process."""

    is_expected = True
    status_code = 409
    error_type = "Conflict"

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        super().__init__(f"Agent already has a running process: {agent_id}")


class ProcessExitError(ProcessError):
    """Raised when a process terminates abnormally."""

    is_expected = True

    def __init__(self, pid: str, exit_code: int, reason: str = ""):
        self.pid = pid
        self.exit_code = exit_code
        self.reason = reason
        super().__init__(f"Process {pid} exited with code {exit_code}: {reason}")


class ToolPermissionDeniedError(NexusError):
    """Raised when an agent lacks permission to invoke a tool."""

    is_expected = True
    status_code = 403
    error_type = "Forbidden"

    def __init__(self, tool_name: str, agent_id: str):
        self.tool_name = tool_name
        self.agent_id = agent_id
        super().__init__(f"Agent '{agent_id}' denied access to tool '{tool_name}'")


class ToolNotFoundError(NexusError):
    """Raised when a tool name has no registered handler."""

    is_expected = True
    status_code = 404
    error_type = "Not Found"

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool not found: {tool_name}")


class ToolTimeoutError(NexusError):
    """Raised when a tool invocation exceeds its timeout."""

    is_expected = True
    status_code = 504
    error_type = "Gateway Timeout"

    def __init__(self, tool_name: str, timeout: float):
        self.tool_name = tool_name
        self.timeout = timeout
        super().__init__(f"Tool '{tool_name}' timed out after {timeout}s")


class MaxTurnsExceededError(NexusError):
    """Raised when the agent loop exceeds its maximum turn count."""

    is_expected = True
    status_code = 429
    error_type = "Too Many Requests"

    def __init__(self, max_turns: int, agent_id: str):
        self.max_turns = max_turns
        self.agent_id = agent_id
        super().__init__(f"Agent '{agent_id}' exceeded max turns ({max_turns})")


class CheckpointError(NexusError):
    """Raised when session checkpoint/restore fails."""

    is_expected = False

    def __init__(self, message: str, checkpoint_hash: str | None = None):
        self.checkpoint_hash = checkpoint_hash
        super().__init__(message)


class CheckpointNotFoundError(CheckpointError):
    """Raised when a checkpoint hash does not exist in CAS."""

    is_expected = True
    status_code = 404
    error_type = "Not Found"

    def __init__(self, checkpoint_hash: str):
        super().__init__(
            f"Checkpoint not found: {checkpoint_hash[:16]}...",
            checkpoint_hash=checkpoint_hash,
        )


class AgentNotFoundError(ProcessError):
    """Raised when a registered agent ID does not exist."""

    is_expected = True
    status_code = 404
    error_type = "Not Found"

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        super().__init__(f"Agent not registered: {agent_id}")


# ======================================================================
# Value types (frozen dataclasses)
# ======================================================================


class ProcessState(StrEnum):
    """Agent process lifecycle states (maps to Linux process states).

    State machine:
        CREATED ──> RUNNING ──> STOPPED
           │           │           ▲
           │           ▼           │
           │        PAUSED ────────┘
           │           │
           ▼           ▼
         ZOMBIE      ZOMBIE

    CREATED: Process allocated but not yet executing (after fork, before exec)
    RUNNING: Actively executing the agent loop
    PAUSED: Suspended (checkpointed), can be resumed
    STOPPED: Terminated normally (exit code 0) or by signal
    ZOMBIE: Terminated but not yet waited on by parent
    """

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ZOMBIE = "zombie"


# Valid process state transitions (strict allowlist).
PROCESS_TRANSITIONS: dict[ProcessState, frozenset[ProcessState]] = {
    ProcessState.CREATED: frozenset({ProcessState.RUNNING, ProcessState.ZOMBIE}),
    ProcessState.RUNNING: frozenset(
        {ProcessState.PAUSED, ProcessState.STOPPED, ProcessState.ZOMBIE}
    ),
    ProcessState.PAUSED: frozenset(
        {ProcessState.RUNNING, ProcessState.STOPPED, ProcessState.ZOMBIE}
    ),
    ProcessState.STOPPED: frozenset(),  # Terminal state
    ProcessState.ZOMBIE: frozenset(),  # Terminal state (awaiting wait())
}


def validate_process_transition(current: ProcessState, target: ProcessState) -> bool:
    """Check if a process state transition is valid."""
    return target in PROCESS_TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True, slots=True)
class ExitStatus:
    """Process exit status (maps to waitpid() result).

    Attributes:
        pid: Process identifier.
        exit_code: Exit code (0 = success, >0 = error, <0 = signal).
        reason: Human-readable exit reason.
        terminated_at: When the process terminated.
    """

    pid: str
    exit_code: int
    reason: str
    terminated_at: datetime


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of a tool invocation.

    Attributes:
        tool_call_id: Unique identifier for this tool call.
        name: Tool name that was invoked.
        output: Tool output (string or bytes).
        error: Error message if the tool failed (None on success).
        duration_ms: Wall-clock execution time in milliseconds.
    """

    tool_call_id: str
    name: str
    output: str | bytes
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def success(self) -> bool:
        """Whether the tool call succeeded."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class AgentProcess:
    """Immutable snapshot of an agent process (maps to task_struct).

    Attributes:
        pid: Unique process identifier.
        agent_id: Agent that owns this process.
        zone_id: Zone isolation scope.
        state: Current process state.
        parent_pid: Parent process ID (for copilot/worker hierarchy).
        started_at: When the process was created.
        exit_status: Exit status (set when STOPPED/ZOMBIE).
        turn_count: Number of completed turns in the agent loop.
        metadata: Arbitrary process metadata.
    """

    pid: str
    agent_id: str
    zone_id: str
    state: ProcessState
    parent_pid: str | None = None
    started_at: datetime | None = None
    exit_status: ExitStatus | None = None
    turn_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckpointInfo:
    """Metadata about a stored session checkpoint.

    Attributes:
        checkpoint_hash: CAS content hash (etag).
        agent_id: Agent this checkpoint belongs to.
        pid: Process ID at checkpoint time.
        turn_count: Number of completed turns when checkpointed.
        created_at: When the checkpoint was created.
        size_bytes: Size of the serialized checkpoint data.
    """

    checkpoint_hash: str
    agent_id: str
    pid: str
    turn_count: int
    created_at: datetime
    size_bytes: int


class DeliveryPolicy(StrEnum):
    """How tool results and artifacts are delivered to the copilot.

    IMMEDIATE:  Push results as soon as available (real-time).
    DEFERRED:   Buffer results, deliver on next copilot turn.
    ON_DEMAND:  Store results in CAS, copilot pulls explicitly.
    """

    IMMEDIATE = "immediate"
    DEFERRED = "deferred"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Configuration for a worker spawned by a copilot.

    Attributes:
        agent_id: Worker agent identifier.
        zone_id: Zone to spawn the worker in.
        tool_allowlist: Tools the worker is allowed to use (glob patterns).
        max_turns: Maximum turns for the worker's agent loop.
        budget_tokens: Token budget cap for the worker (None = unlimited).
        delivery_policy: How results are delivered back.
        metadata: Arbitrary worker metadata.
    """

    agent_id: str
    zone_id: str
    tool_allowlist: tuple[str, ...] = ("*",)
    max_turns: int = 50
    budget_tokens: int | None = None
    delivery_policy: DeliveryPolicy = DeliveryPolicy.IMMEDIATE
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DelegationResult:
    """Result of delegating work to a worker.

    Attributes:
        task_id: A2A task ID for tracking.
        worker_pid: Worker process PID.
        worker_agent_id: Worker agent identifier.
        delivery_policy: How results are delivered back to the copilot.
    """

    task_id: str
    worker_pid: str
    worker_agent_id: str
    delivery_policy: DeliveryPolicy = DeliveryPolicy.IMMEDIATE


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """Configuration for the agent execution loop.

    Attributes:
        max_turns: Maximum number of turns before forced exit.
        max_context_tokens: Maximum context window size in tokens.
        parallel_tool_dispatch: Whether to dispatch multiple tool calls in parallel.
        tool_timeout: Default timeout for individual tool calls (seconds).
        trim_strategy: Context trimming strategy ("sliding_window" or "summarize").
    """

    max_turns: int = 100
    max_context_tokens: int = 128_000
    parallel_tool_dispatch: bool = True
    tool_timeout: float = 30.0
    trim_strategy: str = "sliding_window"


# ======================================================================
# Protocol interfaces
# ======================================================================


@runtime_checkable
class ProcessManagerProtocol(Protocol):
    """Manages agent process lifecycle (maps to kernel process management).

    Concrete implementations live in ``system_services/agent_runtime/``.
    """

    async def spawn(
        self,
        agent_id: str,
        zone_id: str,
        *,
        parent_pid: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentProcess:
        """Create and start a new agent process.

        Raises:
            ProcessAlreadyRunningError: If the agent already has a running process.
        """
        ...

    async def terminate(self, pid: str, *, reason: str = "terminated") -> bool:
        """Terminate a running process.

        Returns True if the process was terminated, False if already stopped.

        Raises:
            ProcessNotFoundError: If the PID does not exist.
        """
        ...

    async def wait(self, pid: str, *, timeout: float | None = None) -> ExitStatus:
        """Wait for a process to terminate.

        Blocks until the process exits or timeout is reached.

        Raises:
            ProcessNotFoundError: If the PID does not exist.
            TimeoutError: If the timeout is exceeded.
        """
        ...

    async def get_process(self, pid: str) -> AgentProcess | None:
        """Get a process by PID. Returns None if not found."""
        ...

    async def list_processes(
        self,
        *,
        zone_id: str | None = None,
        state: ProcessState | None = None,
    ) -> list[AgentProcess]:
        """List processes with optional filters."""
        ...

    async def checkpoint(self, pid: str) -> str:
        """Checkpoint a running process to CAS.

        Pauses the process, serializes its state, stores in CAS.

        Returns:
            CAS content hash of the checkpoint.

        Raises:
            ProcessNotFoundError: If the PID does not exist.
        """
        ...

    async def restore(
        self,
        checkpoint_hash: str,
        *,
        zone_id: str,
    ) -> AgentProcess:
        """Restore a process from a CAS checkpoint.

        Creates a new process from the checkpointed state.

        Raises:
            CheckpointNotFoundError: If the hash does not exist.
        """
        ...


@runtime_checkable
class ToolDispatcherProtocol(Protocol):
    """Routes tool calls to handlers with permission enforcement.

    Maps to Linux sys_call_table — each tool is a "syscall" that the
    agent process can invoke, subject to access manifest checks.
    """

    async def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        agent_id: str,
        zone_id: str,
        tool_call_id: str | None = None,
    ) -> ToolResult:
        """Dispatch a tool call to its registered handler.

        Checks permissions before executing. Enforces timeout.

        Raises:
            ToolNotFoundError: If no handler is registered for tool_name.
            ToolPermissionDeniedError: If the agent lacks permission.
            ToolTimeoutError: If the tool exceeds its timeout.
        """
        ...

    async def check_permission(
        self,
        tool_name: str,
        *,
        agent_id: str,
        zone_id: str,
    ) -> bool:
        """Check if an agent has permission to invoke a tool.

        Uses the access manifest (first-match-wins semantics).
        """
        ...

    def register_handler(
        self,
        tool_name: str,
        handler: Any,
    ) -> None:
        """Register a tool handler.

        Raises:
            ValueError: If a handler is already registered for tool_name.
        """
        ...

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        ...

    def set_manifest(self, agent_id: str, manifest: Any) -> None:
        """Set or clear the access manifest for an agent."""
        ...


@runtime_checkable
class TaskManagerProtocol(Protocol):
    """Protocol for A2A task management (Issue #2761).

    Decouples CopilotOrchestrator from the concrete bricks.a2a.TaskManager,
    keeping system_services at Tier 1 (no brick imports at module level).
    """

    async def create_task(
        self,
        message: Any,
        *,
        zone_id: str,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Create a new A2A task."""
        ...

    async def get_task(self, task_id: str, *, zone_id: str) -> Any:
        """Get a task by ID."""
        ...

    async def update_task_state(
        self,
        task_id: str,
        new_state: Any,
        *,
        zone_id: str,
    ) -> Any:
        """Transition a task to a new state."""
        ...

    async def cancel_task(self, task_id: str, *, zone_id: str) -> None:
        """Cancel a task."""
        ...


@runtime_checkable
class CopilotOrchestratorProtocol(Protocol):
    """Copilot/worker orchestration — delegates work to spawned workers.

    The copilot orchestrator manages the lifecycle of delegated tasks:
    spawn workers, assign work, monitor progress, collect results,
    and handle cancellation cascades.
    """

    async def delegate(
        self,
        copilot_pid: str,
        message: str,
        worker_config: WorkerConfig,
    ) -> DelegationResult:
        """Delegate work to a new worker.

        Spawns a worker process, creates an A2A task, and sets up
        permission restrictions based on worker_config.

        Returns:
            DelegationResult with task_id and worker_pid.
        """
        ...

    async def stream(
        self,
        task_id: str,  # noqa: ARG002
        *,
        zone_id: str,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        """Stream results from a delegated task (IMMEDIATE policy).

        Yields progress events as they arrive via an asyncio.Queue.
        Raises ValueError if the task uses ON_DEMAND delivery policy.
        """
        ...
        yield  # pragma: no cover — protocol stub

    async def collect(self, task_id: str, *, zone_id: str) -> Any:
        """Await task completion (non-blocking), then return the task.

        Uses asyncio.Event to wait for completion instead of polling.
        Falls back to direct get_task() for backward compatibility.
        """
        ...

    async def complete_task(self, task_id: str, *, zone_id: str) -> None:
        """Signal that a delegated worker has finished."""
        ...

    async def fail_task(self, task_id: str, *, zone_id: str) -> None:
        """Signal that a delegated worker has failed."""
        ...

    async def push_event(self, task_id: str, event: Any) -> None:
        """Push a progress event to the task's result queue."""
        ...

    async def cancel(self, task_id: str, *, zone_id: str) -> None:
        """Cancel a delegated task and terminate the worker."""
        ...

    async def cancel_all(self, copilot_pid: str, *, zone_id: str) -> int:
        """Cancel all tasks delegated by a copilot.

        Returns the number of tasks cancelled.
        """
        ...

    async def list_delegations(self, copilot_pid: str, *, zone_id: str) -> list[DelegationResult]:
        """List all active delegations for a copilot."""
        ...


@runtime_checkable
class SessionStoreProtocol(Protocol):
    """CAS-backed session state persistence for checkpoint/restore.

    Stores serialized agent session data (conversation history, context
    window, tool state) in Content-Addressable Storage.
    """

    async def checkpoint(
        self,
        pid: str,
        session_data: dict[str, Any],
        *,
        agent_id: str,
    ) -> str:
        """Save session state to CAS.

        Returns:
            CAS content hash of the stored checkpoint.

        Raises:
            CheckpointError: If serialization or storage fails.
        """
        ...

    async def restore(self, checkpoint_hash: str) -> dict[str, Any]:
        """Load session state from CAS.

        Returns:
            The deserialized session data dictionary.

        Raises:
            CheckpointNotFoundError: If the hash does not exist.
        """
        ...

    async def list_checkpoints(
        self,
        agent_id: str,
        *,
        limit: int = 50,
    ) -> list[CheckpointInfo]:
        """List available checkpoints for an agent.

        Returns checkpoints ordered by creation time (newest first).
        """
        ...

    async def delete_checkpoint(self, checkpoint_hash: str) -> bool:
        """Delete a checkpoint from CAS.

        Returns True if deleted, False if not found.
        """
        ...
