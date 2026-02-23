"""RLM inference types — request/response models and error hierarchy.

Defines the data contracts for the RLM (Recursive Language Model) inference
brick. Follows the structured error category pattern:
  - RLMInfrastructureError: sandbox/LLM unavailable → abort immediately
  - RLMCodeError: model wrote bad code → let iteration loop retry
  - RLMBudgetExceededError: cost/time/token limit hit → abort with partial results

Reference: arXiv:2512.24601 (Zhang, Kraska, Khattab — MIT OASYS Lab)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from nexus.contracts.constants import ROOT_ZONE_ID

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class RLMError(Exception):
    """Base exception for all RLM inference errors."""


class RLMInfrastructureError(RLMError):
    """Sandbox or LLM provider unavailable — abort immediately.

    Raised when the execution environment cannot be created (no sandbox
    providers available, LLM API key missing, network unreachable).
    """


class RLMCodeError(RLMError):
    """Model wrote code that failed to execute.

    NOT raised to callers — the iteration loop captures this and feeds
    the error back to the model for self-correction.  Only surfaces if
    all iterations exhaust without recovery.
    """


class RLMBudgetExceededError(RLMError):
    """Cost, time, or token limit exceeded — abort with partial results.

    Attributes:
        partial_result: Best answer so far (may be None if no progress).
        reason: Which budget was exceeded (iterations, duration, tokens).
        iterations_used: Number of iterations completed.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_result: str | None = None,
        reason: str = "unknown",
        iterations_used: int = 0,
    ) -> None:
        super().__init__(message)
        self.partial_result = partial_result
        self.reason = reason
        self.iterations_used = iterations_used


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RLMStatus(StrEnum):
    """Status of an RLM inference job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


class SSEEventType(StrEnum):
    """Server-Sent Event types for streaming RLM inference."""

    STARTED = "rlm.started"
    ITERATION = "rlm.iteration"
    FINAL_ANSWER = "rlm.final_answer"
    ERROR = "rlm.error"
    BUDGET_EXCEEDED = "rlm.budget_exceeded"


# ---------------------------------------------------------------------------
# Data classes (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RLMInferenceRequest:
    """Request to run RLM inference over Nexus data.

    The model receives only the query and context_paths. It uses
    pre-loaded REPL tools (nexus_read, nexus_search) to lazily
    fetch content as needed — aligning with the RLM paradigm of
    treating context as an external variable.
    """

    query: str
    context_paths: tuple[str, ...] = ()
    zone_id: str = ROOT_ZONE_ID
    model: str = "claude-sonnet-4-20250514"
    sub_model: str | None = None
    max_iterations: int = 15
    max_duration_seconds: int = 120
    max_total_tokens: int = 100_000
    sandbox_provider: str | None = None
    stream: bool = True

    def __post_init__(self) -> None:
        if self.max_iterations < 1 or self.max_iterations > 50:
            raise ValueError(f"max_iterations must be 1-50, got {self.max_iterations}")
        if self.max_duration_seconds < 10 or self.max_duration_seconds > 600:
            raise ValueError(
                f"max_duration_seconds must be 10-600, got {self.max_duration_seconds}"
            )
        if self.max_total_tokens < 1_000 or self.max_total_tokens > 1_000_000:
            raise ValueError(f"max_total_tokens must be 1K-1M, got {self.max_total_tokens}")


@dataclass(frozen=True)
class REPLResult:
    """Result of executing code in the RLM sandbox REPL.

    Mirrors the rlm library's REPLResult for compatibility.
    """

    stdout: str = ""
    stderr: str = ""
    execution_time: float = 0.0
    exit_code: int = 0


@dataclass(frozen=True)
class RLMIteration:
    """Record of a single RLM iteration (prompt → code → execute → output)."""

    step: int
    code_executed: str
    repl_result: REPLResult
    tokens_used: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class RLMInferenceResult:
    """Final result of an RLM inference job."""

    status: RLMStatus
    answer: str | None = None
    iterations: tuple[RLMIteration, ...] = ()
    total_tokens: int = 0
    total_duration_seconds: float = 0.0
    error_message: str | None = None


@dataclass(frozen=True)
class SSEEvent:
    """A single Server-Sent Event for streaming RLM progress."""

    event: SSEEventType
    data: dict[str, object] = field(default_factory=dict)
