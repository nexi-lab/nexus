"""A2A protocol Pydantic models.

Implements the Google Agent-to-Agent (A2A) protocol specification types.
See: https://a2a-protocol.org/latest/specification/

All models use Pydantic v2 for validation, serialization, and OpenAPI
schema generation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ============================================================================
# Task State
# ============================================================================


class TaskState(str, Enum):
    """A2A task lifecycle states."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


#: States from which no further transitions are allowed.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}
)

#: Valid state transitions as (from_state -> set of to_states).
VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.SUBMITTED: frozenset(
        {TaskState.WORKING, TaskState.CANCELED, TaskState.REJECTED}
    ),
    TaskState.WORKING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
            TaskState.INPUT_REQUIRED,
        }
    ),
    TaskState.INPUT_REQUIRED: frozenset(
        {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED}
    ),
    # Terminal states: no outgoing transitions
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
    TaskState.REJECTED: frozenset(),
}


def is_valid_transition(from_state: TaskState, to_state: TaskState) -> bool:
    """Check whether a state transition is valid."""
    return to_state in VALID_TRANSITIONS.get(from_state, frozenset())


# ============================================================================
# Message Parts (discriminated union on "type" field)
# ============================================================================


class TextPart(BaseModel):
    """Plain text content."""

    type: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class FileContent(BaseModel):
    """File reference or inline bytes."""

    name: str | None = None
    mimeType: str | None = None
    url: str | None = None
    bytes: str | None = None  # base64-encoded

    model_config = ConfigDict(extra="forbid")


class FilePart(BaseModel):
    """File content part."""

    type: Literal["file"] = "file"
    file: FileContent
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class DataPart(BaseModel):
    """Structured JSON data part."""

    type: Literal["data"] = "data"
    data: dict[str, Any]
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


#: Discriminated union of Part types.
Part = Annotated[TextPart | FilePart | DataPart, Field(discriminator="type")]


# ============================================================================
# Messages and Artifacts
# ============================================================================


class Message(BaseModel):
    """A message exchanged between client and agent."""

    role: Literal["user", "agent"]
    parts: list[Part]
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class Artifact(BaseModel):
    """An output artifact produced by the agent."""

    artifactId: str
    name: str | None = None
    description: str | None = None
    parts: list[Part]
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


# ============================================================================
# Task
# ============================================================================


class TaskStatus(BaseModel):
    """Current status of a task."""

    state: TaskState
    message: Message | None = None
    timestamp: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class Task(BaseModel):
    """An A2A task."""

    id: str
    contextId: str | None = None
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


# ============================================================================
# Streaming Events
# ============================================================================


class TaskStatusUpdateEvent(BaseModel):
    """Streaming event: task status changed."""

    taskId: str
    status: TaskStatus
    final: bool = False

    model_config = ConfigDict(extra="forbid")


class TaskArtifactUpdateEvent(BaseModel):
    """Streaming event: new or updated artifact."""

    taskId: str
    artifact: Artifact
    append: bool | None = None

    model_config = ConfigDict(extra="forbid")


# ============================================================================
# Agent Card
# ============================================================================


class AgentSkill(BaseModel):
    """A skill advertised in the Agent Card."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] | None = None

    model_config = ConfigDict(extra="forbid")


class AgentCapabilities(BaseModel):
    """Declared agent capabilities."""

    streaming: bool = False
    pushNotifications: bool = False

    model_config = ConfigDict(extra="forbid")


class AuthScheme(BaseModel):
    """Authentication scheme declaration."""

    type: str  # "apiKey", "httpBearer", "oauth2", "openIdConnect"
    # Additional fields vary by type — use extra="allow"

    model_config = ConfigDict(extra="allow")


class AgentProvider(BaseModel):
    """Information about the agent provider / organization."""

    organization: str
    url: str | None = None

    model_config = ConfigDict(extra="forbid")


class AgentCard(BaseModel):
    """A2A Agent Card served at /.well-known/agent.json."""

    name: str
    description: str
    url: str
    version: str
    provider: AgentProvider | None = None
    documentationUrl: str | None = None
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    authentication: list[AuthScheme] = Field(default_factory=list)
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[AgentSkill] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


# ============================================================================
# JSON-RPC Envelope
# ============================================================================


class A2ARequest(BaseModel):
    """A2A JSON-RPC 2.0 request."""

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] | None = None
    id: str | int

    model_config = ConfigDict(extra="forbid")


class A2AErrorData(BaseModel):
    """JSON-RPC error object."""

    code: int
    message: str
    data: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class A2AResponse(BaseModel):
    """A2A JSON-RPC 2.0 response."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any = None
    error: A2AErrorData | None = None

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def success(cls, request_id: str | int, result: Any) -> A2AResponse:
        """Create a success response."""
        return cls(id=request_id, result=result)

    @classmethod
    def from_error(
        cls, request_id: str | int | None, error: A2AErrorData
    ) -> A2AResponse:
        """Create an error response."""
        return cls(id=request_id, error=error)


# ============================================================================
# Method-Specific Params
# ============================================================================


class SendParams(BaseModel):
    """Parameters for a2a.tasks.send / a2a.tasks.sendStreamingMessage."""

    message: Message
    configuration: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class TaskQueryParams(BaseModel):
    """Parameters for a2a.tasks.get."""

    taskId: str
    historyLength: int | None = None

    model_config = ConfigDict(extra="forbid")


class TaskIdParams(BaseModel):
    """Parameters for a2a.tasks.cancel and a2a.tasks.subscribeToTask."""

    taskId: str

    model_config = ConfigDict(extra="forbid")


class PushNotificationConfig(BaseModel):
    """Webhook configuration for push notifications."""

    url: str
    token: str | None = None
    authentication: AuthScheme | None = None

    model_config = ConfigDict(extra="forbid")


class SetPushNotificationParams(BaseModel):
    """Parameters for a2a.tasks.createPushNotificationConfig."""

    taskId: str
    pushNotificationConfig: PushNotificationConfig

    model_config = ConfigDict(extra="forbid")


class GetPushNotificationParams(BaseModel):
    """Parameters for a2a.tasks.getPushNotificationConfig."""

    taskId: str

    model_config = ConfigDict(extra="forbid")


class DeletePushNotificationParams(BaseModel):
    """Parameters for a2a.tasks.deletePushNotificationConfig."""

    taskId: str
    pushNotificationConfigId: str

    model_config = ConfigDict(extra="forbid")
