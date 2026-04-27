"""OperationResult — typed outcome with optional degradation warnings.

Replaces silent error swallowing in write/rename/delete hot paths with
explicit, structured degradation signals.  The server layer can map
``result.degraded`` to HTTP 207 Multi-Status.

Usage:
    result = OperationResult(
        value={"content_id": "abc123", "version": 2},
        warnings=(
            OperationWarning("degraded", "tiger_cache", "cache update failed"),
        ),
    )
    if result.degraded:
        log.warning("Operation succeeded with degradation: %s", result.warnings)

One of the Four Pillars of the kernel VFS layer
(NEXUS-LEGO-ARCHITECTURE.md §4.3: Hook Pipeline + Error Classification).

Moved from ``nexus.core.operation_result`` to contracts layer
to fix import-linter four-tier violation (contracts must not import core).
"""

from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class OperationWarning:
    """A non-fatal warning produced during a VFS operation.

    Attributes:
        severity: "degraded" means a side-effect (cache, ReBAC, observer) failed
                  but the core operation succeeded.
                  "cosmetic" means something minor was skipped (e.g., logging).
        component: Which subsystem produced the warning (e.g., "tiger_cache",
                   "rebac", "observer", "parser").
        message: Human-readable description of what failed.
    """

    severity: Literal["degraded", "cosmetic"]
    component: str
    message: str


@dataclass(frozen=True, slots=True)
class OperationResult(Generic[T]):
    """Typed outcome of a VFS operation.

    The ``value`` contains the primary result (e.g., metadata dict for write).
    ``warnings`` collects any non-fatal issues that occurred during side-effects.

    Attributes:
        value: The primary result of the operation.
        warnings: Tuple of warnings (empty if everything succeeded cleanly).
    """

    value: T
    warnings: tuple[OperationWarning, ...] = field(default_factory=tuple)

    @property
    def degraded(self) -> bool:
        """True if any warning has severity 'degraded'."""
        return any(w.severity == "degraded" for w in self.warnings)

    @property
    def ok(self) -> bool:
        """True if the operation completed with no warnings at all."""
        return len(self.warnings) == 0

    def with_warning(
        self,
        severity: Literal["degraded", "cosmetic"],
        component: str,
        message: str,
    ) -> "OperationResult[T]":
        """Return a new result with an additional warning appended."""
        new_warning = OperationWarning(severity, component, message)
        return OperationResult(
            value=self.value,
            warnings=(*self.warnings, new_warning),
        )

    def merge_warnings(self, other: "OperationResult") -> "OperationResult[T]":
        """Return a new result combining warnings from both results."""
        return OperationResult(
            value=self.value,
            warnings=(*self.warnings, *other.warnings),
        )
