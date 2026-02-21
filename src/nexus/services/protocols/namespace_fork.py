"""Namespace fork service protocol (Issue #1273).

Defines the contract for agent namespace forking — Plan 9 ``rfork(RFNAMEG)``
inspired fork/merge/discard lifecycle.

Existing implementation:
    ``nexus.system_services.namespace.namespace_fork_service.AgentNamespaceForkService``

Storage Affinity: **In-memory only** — fork overlays are ephemeral CoW dicts
    with TTL auto-cleanup.  No persistence needed.

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - Issue #1273: Agent namespace forking for speculative execution
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.namespace_fork_types import (
        ForkMode,
        NamespaceForkInfo,
        NamespaceMergeResult,
    )


@runtime_checkable
class AgentNamespaceForkProtocol(Protocol):
    """Service contract for agent namespace forking.

    All methods are synchronous — fork overlays are in-memory only.

    Note: ``get_fork()`` is intentionally excluded from the protocol.
    It returns the concrete ``AgentNamespace`` implementation type.
    Callers needing metadata should use ``get_fork_info()`` instead.
    Direct ``AgentNamespace`` access is an implementation detail for
    consumers that depend on the concrete service class.
    """

    def fork(
        self,
        agent_id: str,
        zone_id: str | None = None,
        *,
        parent_fork_id: str | None = None,
        mode: ForkMode = ...,
    ) -> NamespaceForkInfo: ...

    def merge(
        self,
        fork_id: str,
        *,
        strategy: str = "fail",
    ) -> NamespaceMergeResult: ...

    def discard(self, fork_id: str) -> None: ...

    def get_fork_info(self, fork_id: str) -> NamespaceForkInfo: ...

    def list_forks(self, agent_id: str | None = None) -> list[NamespaceForkInfo]: ...

    def cleanup_expired(self) -> int: ...
