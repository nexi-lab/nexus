"""Agent process contracts — kernel process descriptor types.

Defines the ``task_struct`` equivalent for Nexus agent processes:
``AgentProcess``, ``AgentProcessConfig``, ``FileDescriptor``,
``AgentSignal``, ``AgentProcessState``, ``AgentContext``, and
``AgentEvent`` union types.

Pure value objects with zero runtime dependencies on kernel/services/bricks.
Only stdlib + contracts imports.

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §4, §13.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nexus.contracts.agent_types import AgentQoS, QoSClass
from nexus.contracts.llm_types import Message, ToolCall

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentSignal(StrEnum):
    """Signals for agent process control (``signal.h`` equivalent)."""

    SIGTERM = "SIGTERM"  # Graceful shutdown
    SIGSTOP = "SIGSTOP"  # Suspend (transition to STOPPED)
    SIGCONT = "SIGCONT"  # Resume (transition from STOPPED)
    SIGKILL = "SIGKILL"  # Immediate termination
    SIGUSR1 = "SIGUSR1"  # Steering message injection


class AgentProcessState(StrEnum):
    """Agent process lifecycle states (``task_struct`` state equivalent).

    State machine::

        spawn()              schedule()           idle (no work)
        -------> [CREATED] ----------> [RUNNING] ----------> [SLEEPING]
                              ^                    |              |
                              |  tool result       |              |
                              +--------------------+              |
                                                                  | wake
                                                                  |
                        signal(SIGSTOP)                           |
                   +---------------------------+                  |
                   v                           |                  |
             [STOPPED] ----signal(SIGCONT)-----+------------------+
                                                                  |
                        signal(SIGTERM)           exit()           |
                   +---------------------------+                  |
                   v                                              |
             [ZOMBIE] ----parent.wait()-----> [REMOVED]           |
    """

    CREATED = "created"
    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPED = "stopped"
    ZOMBIE = "zombie"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileDescriptor:
    """An open file handle in the agent's fd table.

    Linux analogue: ``struct file`` + file descriptor integer.
    """

    fd: int
    path: str
    mode: str  # "r", "w", "rw"
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class AgentProcessConfig:
    """Configuration for spawning a new agent process.

    Linux analogue: ``execve()`` arguments + ``rlimit`` settings.
    """

    # Identity
    name: str
    agent_type: str = "coding"

    # Execution
    model: str = "claude-sonnet-4-6"
    system_prompt: str | None = None
    mode: str = "interactive"  # "interactive", "print"
    prompt: str | None = None  # initial prompt (for print mode)

    # Resource limits (ulimit equivalent)
    max_turns: int = 100
    max_tokens: int = 1_000_000
    max_storage_mb: int = 1024
    max_context_tokens: int = 200_000
    max_children: int = 10
    exec_timeout: int = 3600
    sandbox_timeout: int = 300

    # QoS
    qos_class: QoSClass = QoSClass.STANDARD
    priority: int = 0

    # Sandbox
    sandbox_id: str | None = None

    # Filesystem
    cwd: str | None = None
    mount_paths: tuple[str, ...] = ()

    # Tools
    tools: tuple[str, ...] = ("read_file", "write_file", "edit_file", "bash", "grep", "glob")
    extensions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """OpenAI-format tool schema for LLM."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Execution context for an agent process.

    Contains everything the agent loop needs to run:
    system prompt, conversation history, and tool schemas.
    """

    system_prompt: str
    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...]  # OpenAI-format tool schemas


@dataclass(frozen=True, slots=True)
class AgentProcess:
    """Kernel process descriptor for a running agent.

    Linux analogue: ``task_struct``.
    """

    # Identity
    pid: str
    ppid: str | None
    name: str
    owner_id: str
    zone_id: str

    # Lifecycle
    state: AgentProcessState
    generation: int

    # Resource accounting
    qos: AgentQoS = field(default_factory=AgentQoS)

    # Filesystem state
    cwd: str = "/"
    root: str = "/"

    # File descriptor table
    fd_table: tuple[FileDescriptor, ...] = ()

    # Execution context
    model: str = "claude-sonnet-4-6"
    system_prompt_path: str | None = None

    # Checkpoint state
    checkpoint_path: str | None = None

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_scheduled: datetime | None = None

    # Sub-process tracking
    children: tuple[str, ...] = ()

    # Config
    config: AgentProcessConfig | None = None


# ---------------------------------------------------------------------------
# Agent event types (streaming events from agent loop)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Streamed text fragment from the LLM."""

    text: str
    event_type: str = "text_delta"


@dataclass(frozen=True, slots=True)
class ToolCallStart:
    """Emitted when the agent starts executing a tool call."""

    tool_call: ToolCall
    event_type: str = "tool_call_start"


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """Emitted when a tool call completes."""

    tool_call: ToolCall
    result: str
    event_type: str = "tool_call_result"


@dataclass(frozen=True, slots=True)
class Completed:
    """Emitted when the agent loop finishes normally."""

    message_count: int
    event_type: str = "completed"


@dataclass(frozen=True, slots=True)
class Error:
    """Emitted when the agent loop encounters an error."""

    error: str
    event_type: str = "error"


# Union type for all agent events
AgentEvent = TextDelta | ToolCallStart | ToolCallResult | Completed | Error
