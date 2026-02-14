"""Permission (ReBAC) service protocol (Issue #1287: Extract domain services).

Defines the contract for relationship-based access control operations.
Existing implementation: ``nexus.core.nexus_fs_rebac.NexusFSReBACMixin``
backed by ``nexus.core.rebac_manager_enhanced.EnhancedReBACManager``.

Decision 13A: Includes ``check_bulk`` for efficient batch permission checking.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@runtime_checkable
class PermissionProtocol(Protocol):
    """Service contract for relationship-based access control (ReBAC).

    Six API groups:
    - Core: create / check / expand / explain / delete tuples
    - Bulk: check_batch / check_bulk for efficient batch operations
    - Config: get/set ReBAC options
    - Namespace: register / get / create / list / delete namespace schemas
    - Privacy: expand_with_privacy / grant_consent / revoke_consent / make_public / make_private
    - Sharing: share_with_user / share_with_group / revoke / list outgoing/incoming
    - Viewer: dynamic column-level permissions for tabular data
    """

    # ── Core ReBAC Operations ─────────────────────────────────────────────

    async def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
        context: OperationContext | None = None,
        column_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: OperationContext | None = None,
        zone_id: str | None = None,
    ) -> bool: ...

    async def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]: ...

    async def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def rebac_delete(
        self,
        tuple_id: str,
    ) -> bool: ...

    async def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    # ── Bulk Operations (Decision 13A) ────────────────────────────────────

    async def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]: ...

    async def check_bulk(
        self,
        subject: tuple[str, str],
        permission: str,
        objects: list[tuple[str, str]],
        zone_id: str | None = None,
    ) -> dict[tuple[str, str], bool]:
        """Efficiently check one subject's permission against many objects.

        This is the preferred API for permission filtering (e.g., listing
        only files a user can read). Unlike ``rebac_check_batch`` which
        takes arbitrary (subject, perm, object) triples, this method is
        optimized for the common single-subject-many-objects pattern and
        can leverage Tiger Cache bitmap intersections.

        Args:
            subject: (type, id) of the subject to check.
            permission: Permission string (e.g. "read", "write").
            objects: List of (type, id) objects to check against.
            zone_id: Optional zone scope.

        Returns:
            Mapping from each object to its permission result.
        """
        ...

    # ── Configuration ─────────────────────────────────────────────────────

    async def set_rebac_option(self, key: str, value: Any) -> None: ...

    async def get_rebac_option(self, key: str) -> Any: ...

    # ── Namespace Management ──────────────────────────────────────────────

    async def register_namespace(self, namespace: dict[str, Any]) -> None: ...

    async def get_namespace(self, object_type: str) -> dict[str, Any] | None: ...

    async def namespace_create(self, object_type: str, config: dict[str, Any]) -> None: ...

    async def namespace_list(self) -> list[dict[str, Any]]: ...

    async def namespace_delete(self, object_type: str) -> bool: ...

    # ── Privacy / Consent ─────────────────────────────────────────────────

    async def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> list[tuple[str, str]]: ...

    async def grant_consent(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def revoke_consent(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
    ) -> bool: ...

    async def make_public(
        self,
        resource: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def make_private(
        self,
        resource: tuple[str, str],
    ) -> bool: ...

    # ── Cross-Zone Sharing ────────────────────────────────────────────────

    async def share_with_user(
        self,
        resource: tuple[str, str],
        user_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        user_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def share_with_group(
        self,
        resource: tuple[str, str],
        group_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        group_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def revoke_share(
        self,
        resource: tuple[str, str],
        user_id: str,
    ) -> bool: ...

    async def revoke_share_by_id(
        self,
        share_id: str,
    ) -> bool: ...

    async def list_outgoing_shares(
        self,
        resource: tuple[str, str] | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    async def list_incoming_shares(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    # ── Dynamic Viewer (Column-level Permissions) ─────────────────────────

    async def get_dynamic_viewer_config(
        self,
        subject: tuple[str, str],
        file_path: str,
    ) -> dict[str, Any] | None: ...

    async def apply_dynamic_viewer_filter(
        self,
        data: str,
        column_config: dict[str, Any],
        file_format: str = "csv",
    ) -> dict[str, Any]: ...

    async def read_with_dynamic_viewer(
        self,
        file_path: str,
        subject: tuple[str, str],
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...
