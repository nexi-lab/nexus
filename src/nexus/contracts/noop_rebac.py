"""NoOp (Null Object) implementations for ReBAC services (Issue #2440).

When ReBAC is disabled via profile or fails to initialize, these stubs
are injected so the system boots without permission enforcement.
All checks return True (allow-all), writes return safe defaults.

Validated by: Linux LSM (permissive mode), Kubernetes RBAC (--authorization-mode=AlwaysAllow),
Kafka (allow.everyone.if.no.acl.found).

Placed in ``contracts/`` because these are protocol-compliant stubs,
not brick implementation code.
"""
# ruff: noqa: ARG002

import logging
from datetime import datetime
from typing import Any

from nexus.contracts.rebac_types import WriteResult

logger = logging.getLogger(__name__)


class NoOpReBACManager:
    """NoOp ReBAC manager — all checks return True, writes return safe defaults.

    Implements ``ReBACBrickProtocol`` structurally (duck typing).
    """

    # ── Core Zanzibar APIs ──────────────────────────────────────────

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
        consistency: Any = None,
    ) -> bool:
        return True

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
        subject_zone_id: str | None = None,
        object_zone_id: str | None = None,
    ) -> WriteResult:
        return WriteResult(
            tuple_id="noop",
            revision=0,
            consistency_token="noop",
            written_at_ms=0.0,
        )

    def rebac_delete(self, tuple_id: str | WriteResult) -> bool:
        return True

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[tuple[str, str]]:
        return []

    # ── Bulk APIs ───────────────────────────────────────────────────

    def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str = "",
        consistency: Any = None,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        return dict.fromkeys(checks, True)

    def rebac_list_objects(
        self,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        zone_id: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        return []

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        return []

    # ── Zone Revision & Cache ──────────────────────────────────────

    def get_zone_revision(
        self,
        zone_id: str | None,
        conn: Any | None = None,
    ) -> int:
        return 0

    def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None:
        pass

    # ── Dir visibility invalidation (used by factory wiring) ───────

    def register_dir_visibility_invalidator(
        self,
        name: str,
        callback: Any,
    ) -> None:
        pass

    # ── Brick Lifecycle ─────────────────────────────────────────────

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def close(self) -> None:
        pass

    def verify_imports(self) -> dict[str, bool]:
        return {}


class NoOpPermissionEnforcer:
    """NoOp permission enforcer — all checks return True (allow-all).

    Implements ``PermissionEnforcerProtocol`` structurally (duck typing).
    """

    def check(
        self,
        path: str,
        permission: Any,
        context: Any,
    ) -> bool:
        return True

    def filter_list(
        self,
        paths: list[str],
        context: Any,
    ) -> list[str]:
        return paths

    def has_accessible_descendants(
        self,
        prefix: str,
        context: Any,
    ) -> bool:
        return True

    def has_accessible_descendants_batch(
        self,
        prefixes: list[str],
        context: Any,
    ) -> dict[str, bool]:
        return dict.fromkeys(prefixes, True)

    def invalidate_cache(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        zone_id: str | None = None,
    ) -> None:
        pass


class NoOpEntityRegistry:
    """NoOp entity registry — all registrations are no-ops.

    Implements ``EntityRegistryProtocol`` structurally (duck typing).
    """

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        parent_type: str | None = None,
        parent_id: str | None = None,
        entity_metadata: dict[str, Any] | None = None,
    ) -> Any:
        return None

    def get_entity(
        self,
        entity_type: str,
        entity_id: str,
    ) -> Any | None:
        return None

    def lookup_entity_by_id(
        self,
        entity_id: str,
    ) -> list[Any]:
        return []

    def get_entities_by_type(
        self,
        entity_type: str,
    ) -> list[Any]:
        return []

    def get_children(
        self,
        parent_type: str,
        parent_id: str,
    ) -> list[Any]:
        return []

    def delete_entity(
        self,
        entity_type: str,
        entity_id: str,
        cascade: bool = True,
    ) -> bool:
        return False


class NoOpAuditStore:
    """NoOp audit store — close() is a no-op."""

    def close(self) -> None:
        pass
