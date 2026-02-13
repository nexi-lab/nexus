"""Pydantic models for context manifest source types and results (Issue #1341).

Defines the 4 source types that can be declared in a context manifest:
- MCPToolSource: Pre-execute an MCP tool and inject its result
- WorkspaceSnapshotSource: Load a workspace snapshot
- FileGlobSource: Resolve a file glob pattern and load matching files
- MemoryQuerySource: Run a semantic search over agent memory

All models are frozen (immutable) and use Pydantic v2 discriminated unions.

References:
    - Issue #1341: Context manifest with deterministic pre-execution
    - Stripe Minions: https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Discriminator, Field

# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS: float = 30.0
DEFAULT_MAX_RESULT_BYTES: int = 1_048_576  # 1 MB


# ===========================================================================
# ContextSourceProtocol — structural interface for all source types (CQ-3)
# ===========================================================================


@runtime_checkable
class ContextSourceProtocol(Protocol):
    """Structural protocol defining the shared interface for all context sources.

    Every source type must expose these fields. The resolver uses this protocol
    instead of ``Any`` for type safety without coupling to Pydantic models.
    """

    @property
    def type(self) -> str: ...

    @property
    def required(self) -> bool: ...

    @property
    def timeout_seconds(self) -> float: ...

    @property
    def max_result_bytes(self) -> int: ...

    @property
    def source_name(self) -> str:
        """Human-readable name for this source (CQ-1)."""
        ...


# ===========================================================================
# Source type models (Pydantic, frozen)
# ===========================================================================


class MCPToolSource(BaseModel):
    """Pre-execute an MCP tool and inject its result into agent context.

    The tool is invoked with the given args (after template variable
    substitution) and its output is stored under ``/context/``.

    Note: MCP tool execution depends on #1272 (MCP tool-level namespace
    granularity). Until that is implemented, this source type will be
    skipped during resolution.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["mcp_tool"] = "mcp_tool"
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    pre_exec: bool = True
    required: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES

    @property
    def source_name(self) -> str:
        """Human-readable name for this source."""
        return self.tool_name


class WorkspaceSnapshotSource(BaseModel):
    """Load a workspace snapshot into agent context.

    Retrieves the specified snapshot (or latest) and makes its manifest
    available under ``/context/``.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["workspace_snapshot"] = "workspace_snapshot"
    snapshot_id: str | Literal["latest"] = "latest"
    required: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES

    @property
    def source_name(self) -> str:
        """Human-readable name for this source."""
        return self.snapshot_id


class FileGlobSource(BaseModel):
    """Resolve a file glob pattern and load matching files into context.

    The pattern is evaluated against the agent's namespace. Results are
    capped at ``max_files`` entries to prevent context explosion.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["file_glob"] = "file_glob"
    pattern: str = Field(min_length=1)
    max_files: int = Field(default=50, ge=1)
    required: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES

    @property
    def source_name(self) -> str:
        """Human-readable name for this source."""
        return self.pattern


class MemoryQuerySource(BaseModel):
    """Run a semantic search over agent memory and inject top results.

    The query string supports template variables (e.g.,
    ``{{task.description}}``) which are resolved before execution.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["memory_query"] = "memory_query"
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1)
    required: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES

    @property
    def source_name(self) -> str:
        """Human-readable name for this source."""
        return self.query


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

ContextSource = Annotated[
    MCPToolSource | WorkspaceSnapshotSource | FileGlobSource | MemoryQuerySource,
    Discriminator("type"),
]
"""Discriminated union of all context source types.

Pydantic dispatches on the ``type`` field to select the correct model.
"""


# ===========================================================================
# Result types (frozen dataclasses)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class SourceResult:
    """Result of executing a single context source.

    Attributes:
        source_type: The source type string (e.g., "mcp_tool", "file_glob").
        source_name: Human-readable name (tool_name, snapshot_id, pattern, or query).
        status: Execution status — one of "ok", "error", "skipped", "timeout", "truncated".
        data: The resolved content (type varies by source), or None on error/timeout.
        error_message: Human-readable error description (None on success).
        elapsed_ms: Wall-clock time for this source's execution in milliseconds.
    """

    source_type: str
    source_name: str
    status: Literal["ok", "error", "skipped", "timeout", "truncated"]
    data: Any
    error_message: str | None = None
    elapsed_ms: float = 0.0

    @classmethod
    def ok(
        cls,
        source_type: str,
        source_name: str,
        data: Any,
        elapsed_ms: float = 0.0,
    ) -> SourceResult:
        """Create a successful result."""
        return cls(
            source_type=source_type,
            source_name=source_name,
            status="ok",
            data=data,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def error(
        cls,
        source_type: str,
        source_name: str,
        error_message: str,
        elapsed_ms: float = 0.0,
    ) -> SourceResult:
        """Create an error result."""
        return cls(
            source_type=source_type,
            source_name=source_name,
            status="error",
            data=None,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def timeout(
        cls,
        source_type: str,
        source_name: str,
        error_message: str,
        elapsed_ms: float = 0.0,
    ) -> SourceResult:
        """Create a timeout result."""
        return cls(
            source_type=source_type,
            source_name=source_name,
            status="timeout",
            data=None,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def skipped(
        cls,
        source_type: str,
        source_name: str,
        error_message: str,
    ) -> SourceResult:
        """Create a skipped result (e.g., no executor registered)."""
        return cls(
            source_type=source_type,
            source_name=source_name,
            status="skipped",
            data=None,
            error_message=error_message,
        )

    @classmethod
    def truncated(
        cls,
        source_type: str,
        source_name: str,
        data: Any,
        error_message: str,
        elapsed_ms: float = 0.0,
    ) -> SourceResult:
        """Create a truncated result."""
        return cls(
            source_type=source_type,
            source_name=source_name,
            status="truncated",
            data=data,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
        )


@dataclass(frozen=True, slots=True)
class ManifestResult:
    """Aggregate result of resolving an entire context manifest.

    Attributes:
        sources: Tuple of individual source results (ordered by manifest order).
        resolved_at: ISO-8601 timestamp of when resolution completed.
        total_ms: Total wall-clock time for manifest resolution in milliseconds.
    """

    sources: tuple[SourceResult, ...]
    resolved_at: str
    total_ms: float


# ===========================================================================
# Exceptions
# ===========================================================================


class ManifestResolutionError(Exception):
    """Raised when one or more required sources fail during manifest resolution.

    Attributes:
        failed_sources: Tuple of SourceResult objects for the failed required sources.
    """

    def __init__(self, failed_sources: tuple[SourceResult, ...]) -> None:
        self.failed_sources = failed_sources
        names = ", ".join(s.source_name for s in failed_sources)
        super().__init__(f"Required sources failed: {names}")
