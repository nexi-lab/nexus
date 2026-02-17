"""Revision service protocol (Issue #1287, Decision 3A).

Defines the contract for version/revision operations — listing versions,
retrieving specific versions, computing diffs.

Existing implementation: ``nexus.services.version_service.VersionService``
(already extracted, async).

This protocol formalizes the VersionService interface as a kernel-level
contract for future extraction of the remaining revision logic from
NexusFSCoreMixin (snapshot/restore, time-travel queries).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §3 (Kernel tier)
    - Issue #1287: Extract NexusFS Domain Services from God Object
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext

@runtime_checkable
class RevisionServiceProtocol(Protocol):
    """Version and revision operations — list, get, diff.

    Covers both the already-extracted VersionService methods
    and future snapshot/restore operations still in NexusFSCoreMixin.
    """

    async def list_versions(
        self,
        path: str,
        *,
        limit: int = 50,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """List version history for a file.

        Args:
            path: Virtual path to list versions for.
            limit: Maximum number of versions to return.
            offset: Number of versions to skip.
            context: Operation context for permission checks.

        Returns:
            Dict with 'versions' list and pagination metadata.
        """
        ...

    async def get_version(
        self,
        path: str,
        version: int | str,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Get a specific version of a file.

        Args:
            path: Virtual path.
            version: Version number or hash.
            context: Operation context for permission checks.

        Returns:
            Dict with version content and metadata.
        """
        ...

    async def diff_versions(
        self,
        path: str,
        version_a: int | str,
        version_b: int | str,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Compute diff between two versions.

        Args:
            path: Virtual path.
            version_a: First version number or hash.
            version_b: Second version number or hash.
            context: Operation context for permission checks.

        Returns:
            Dict with diff content and metadata.
        """
        ...
