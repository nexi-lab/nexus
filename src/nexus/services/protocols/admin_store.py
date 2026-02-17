"""Protocol for administrative RecordStore queries (violationfix #129).

Per KERNEL-ARCHITECTURE.md §3, the kernel must NOT import ORM models
or execute SQLAlchemy queries directly.  This protocol abstracts the
12+ lazy ORM imports that previously lived in ``nexus.core.nexus_fs``
behind a clean service interface.

The implementation lives in ``nexus.services.admin_store``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AdminStoreProtocol(Protocol):
    """Service protocol for administrative RecordStore queries.

    Groups operations that were previously scattered across NexusFS
    as lazy ``from nexus.storage.models import …`` blocks.
    """

    # ------------------------------------------------------------------
    # API-key helpers (was: APIKeyModel queries in nexus_fs.py)
    # ------------------------------------------------------------------

    def get_owner_key_expiration(self, user_id: str) -> datetime | None:
        """Return the expiration of the owner's most-recent active API key.

        Returns ``None`` when no active non-agent key exists or the key
        has no expiration.
        """
        ...

    def get_all_active_agent_keys(self) -> dict[str, dict[str, Any]]:
        """Return active agent API keys keyed by ``subject_id``.

        Each value is a dict with at least ``inherit_permissions: bool``.
        """
        ...

    def get_agent_api_key(self, agent_id: str) -> dict[str, Any] | None:
        """Return active API key info for *agent_id*, or ``None``."""
        ...

    def revoke_agent_api_keys(self, agent_id: str) -> int:
        """Soft-revoke all active API keys for *agent_id*.  Return count."""
        ...

    def delete_api_keys_for_user(self, user_id: str) -> int:
        """Hard-delete all API keys owned by *user_id*.  Return count."""
        ...

    # ------------------------------------------------------------------
    # Entity / User / Zone records
    # ------------------------------------------------------------------

    def update_entity_metadata(
        self, entity_type: str, entity_id: str, metadata_json: str
    ) -> None:
        """Overwrite ``entity_metadata`` column for an entity registry row."""
        ...

    def provision_zone(self, zone_id: str, zone_name: str | None) -> bool:
        """Idempotently create a zone record.  Return ``True`` if created."""
        ...

    def provision_user_record(
        self,
        user_id: str,
        email: str,
        display_name: str | None,
        zone_id: str,
    ) -> bool:
        """Idempotently create (or reactivate) a user record.

        Returns ``True`` if a new row was inserted.
        """
        ...

    def lock_user_and_provision_key(
        self,
        user_id: str,
        zone_id: str,
        api_key_creator: Any,
        api_key_name: str,
        expires_at: datetime | None,
    ) -> tuple[str | None, str | None]:
        """Atomically lock user row, check for existing key, create if missing.

        Returns ``(key_id, raw_api_key)`` or ``(None, None)`` if key
        already existed.
        """
        ...

    def get_user_record(self, user_id: str) -> dict[str, Any] | None:
        """Return basic user info dict, or ``None`` if not found.

        Returned dict has at least: ``user_id``, ``email``, ``zone_id``,
        ``is_global_admin``, ``is_active``.
        """
        ...

    # ------------------------------------------------------------------
    # OAuth cleanup
    # ------------------------------------------------------------------

    def delete_oauth_records(self, user_id: str) -> tuple[int, int]:
        """Delete OAuth API keys and account linkages for *user_id*.

        Returns ``(deleted_oauth_keys, deleted_oauth_accounts)``.
        Silently returns ``(0, 0)`` when OAuth tables do not exist.
        """
        ...

    # ------------------------------------------------------------------
    # Metadata / permission cleanup (rmdir cascade)
    # ------------------------------------------------------------------

    def delete_file_paths_by_prefix(self, path_prefix: str) -> int:
        """Delete ``FilePathModel`` rows whose virtual_path starts with *path_prefix*."""
        ...

    def delete_rebac_tuples_by_path(self, path_prefix: str) -> int:
        """Delete ReBAC tuples for file objects whose object_id starts with *path_prefix*."""
        ...

    # ------------------------------------------------------------------
    # User soft-delete
    # ------------------------------------------------------------------

    def soft_delete_user(self, user_id: str) -> bool:
        """Soft-delete a user record (set is_active=0, deleted_at=now).

        Returns ``True`` if the user was soft-deleted, ``False`` if not found.
        """
        ...

    # ------------------------------------------------------------------
    # Agent key expiration helper
    # ------------------------------------------------------------------

    def get_agent_key_expiration(self, user_id: str) -> datetime:
        """Determine expiration for a new agent API key.

        Uses the owner's key expiration as maximum, or defaults to
        365 days from now.  Raises ``ValueError`` if the owner's
        key has already expired.
        """
        ...
