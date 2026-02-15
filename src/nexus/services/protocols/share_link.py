"""Share link service protocol (Issue #1287: Extract domain services).

Defines the contract for W3C TAG Capability URL share link operations.
Existing implementation: ``nexus.core.nexus_fs_share_links.NexusFSShareLinksMixin``.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
    - Issue #227: Document Sharing & Access Links
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@runtime_checkable
class ShareLinkProtocol(Protocol):
    """Service contract for share link operations.

    Implements Capability URL pattern (W3C TAG best practices):
    - Unguessable tokens (UUID v4, 122 bits entropy)
    - Optional password protection
    - Time-limited access with download limits
    - Revocation and access logging
    """

    async def create_share_link(
        self,
        path: str,
        permission_level: str = "viewer",
        expires_in_hours: int | None = None,
        max_access_count: int | None = None,
        password: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def get_share_link(
        self,
        link_id: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def list_share_links(
        self,
        path: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def revoke_share_link(
        self,
        link_id: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def access_share_link(
        self,
        link_id: str,
        password: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def get_share_link_access_logs(
        self,
        link_id: str,
        limit: int = 100,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...
