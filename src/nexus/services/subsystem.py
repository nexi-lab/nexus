"""Subsystem ABC and shared helpers for extracted NexusFS domain services.

Issue #1287: Extract NexusFS Domain Services from God Object.

This module provides:
- ``Subsystem`` ABC: Base class for all extracted subsystems with lifecycle hooks.
- ``ContextIdentity``: Frozen dataclass replacing 10+ copy-paste context extraction sites.
- ``extract_context_identity()``: DRY helper to extract zone/user/admin from OperationContext.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.types import OperationContext as OperationContext  # noqa: F401

# Issue #1291: ContextIdentity and extract_context_identity moved to nexus.core.types.
# Re-exported here for backward compatibility.
from nexus.core.types import ContextIdentity, extract_context_identity  # noqa: F401


class Subsystem(ABC):
    """Base class for extracted NexusFS subsystems.

    Every subsystem must implement ``health_check()`` returning a dict
    with at least a ``"status"`` key (``"ok"`` or ``"degraded"``).

    ``cleanup()`` is optional — override if the subsystem holds resources
    (threads, connections, caches) that need explicit teardown.
    """

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return health status for this subsystem.

        Returns:
            Dict with at least ``{"status": "ok"}`` or ``{"status": "degraded", ...}``.
        """
        ...

    def cleanup(self) -> None:  # noqa: B027
        """Release resources held by this subsystem.

        Default implementation is a no-op. Override if you hold threads,
        connections, or caches that need explicit teardown.
        """
