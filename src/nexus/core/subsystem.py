"""Subsystem ABC and shared helpers for extracted NexusFS domain services.

Issue #1287: Extract NexusFS Domain Services from God Object.

This module provides:
- ``Subsystem`` ABC: Base class for all extracted subsystems with lifecycle hooks.
- ``ContextIdentity``: Frozen dataclass replacing 10+ copy-paste context extraction sites.
- ``extract_context_identity()``: DRY helper to extract zone/user/admin from OperationContext.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@dataclass(frozen=True)
class ContextIdentity:
    """Extracted identity from OperationContext (DRY helper).

    Replaces the pattern::

        zone_id = getattr(context, "zone_id", None) or "default"
        user_id = getattr(context, "user", None) or "anonymous"
        is_admin = getattr(context, "is_admin", False)

    which appears 10+ times across mixins.
    """

    zone_id: str
    user_id: str
    is_admin: bool


def extract_context_identity(context: OperationContext | None) -> ContextIdentity:
    """Extract zone/user/admin from an OperationContext.

    Safe to call with ``None`` — returns sensible defaults.

    Args:
        context: Optional OperationContext from a request.

    Returns:
        Frozen ContextIdentity with zone_id, user_id, is_admin.
    """
    if context is None:
        return ContextIdentity(zone_id="default", user_id="anonymous", is_admin=False)
    return ContextIdentity(
        zone_id=getattr(context, "zone_id", None) or "default",
        user_id=(
            getattr(context, "user", None)
            or getattr(context, "subject_id", None)
            or "anonymous"
        ),
        is_admin=getattr(context, "is_admin", False),
    )


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
