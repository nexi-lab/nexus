"""PasswordVaultService — domain wrapper over SecretsService.

On-demand service (not a BackgroundService) — no ``start`` / ``stop``
lifecycle. All storage, encryption, audit, versioning and soft-delete
is delegated to SecretsService; this module only serializes
``VaultEntry`` ↔ JSON and translates errors into domain exceptions.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.password_vault.schema import VaultEntry

if TYPE_CHECKING:
    from nexus.bricks.secrets.service import SecretsService

logger = logging.getLogger(__name__)


class VaultEntryNotFoundError(LookupError):
    """Raised when a vault entry does not exist (or is soft-deleted)."""


class PasswordVaultService:
    """Domain-typed wrapper over SecretsService for vault entries.

    Every entry lives under a single SecretsService namespace
    (``"passwords"``) and uses the entry's ``title`` as the key.
    Values are JSON-serialised ``VaultEntry`` dicts; SecretsService
    itself sees only opaque strings.
    """

    NAMESPACE = "passwords"

    def __init__(self, secrets_service: SecretsService) -> None:
        self._secrets = secrets_service

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def put_entry(
        self,
        entry: VaultEntry,
        *,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a vault entry. Creates a new version per write."""
        value = json.dumps(entry.model_dump(), sort_keys=True)
        result = self._secrets.put_secret(
            namespace=self.NAMESPACE,
            key=entry.title,
            value=value,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {
            "id": result.get("id"),
            "title": entry.title,
            "version": result.get("version"),
            "created_at": result.get("created_at"),
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_entry(
        self,
        title: str,
        *,
        version: int | None = None,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> VaultEntry:
        """Fetch and decrypt a vault entry (latest version unless specified)."""
        result = self._secrets.get_secret(
            namespace=self.NAMESPACE,
            key=title,
            actor_id=actor_id,
            version=version,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if result is None:
            raise VaultEntryNotFoundError(title)
        return VaultEntry.model_validate(json.loads(result["value"]))

    def list_entries(
        self,
        *,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> list[VaultEntry]:
        """Return every (live) vault entry with its latest-version payload."""
        metadata = self._secrets.list_secrets(
            namespace=self.NAMESPACE,
            include_deleted=False,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not metadata:
            return []

        queries = [{"namespace": self.NAMESPACE, "key": m["key"]} for m in metadata]
        decrypted = self._secrets.batch_get(
            queries=queries,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )

        entries: list[VaultEntry] = []
        for m in metadata:
            composite_key = f"{self.NAMESPACE}:{m['key']}"
            raw = decrypted.get(composite_key)
            if raw is None:
                # SecretsService already logged (disabled / fetch error); skip.
                continue
            try:
                entries.append(VaultEntry.model_validate(json.loads(raw)))
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("Skipping malformed vault entry %r: %s", m["key"], exc)
        return entries

    def list_versions(
        self,
        title: str,
        *,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return version history (for rotation audits, M3)."""
        return self._secrets.list_versions(
            namespace=self.NAMESPACE,
            key=title,
            subject_id=subject_id,
            subject_type=subject_type,
        )

    # ------------------------------------------------------------------
    # Delete / restore (soft)
    # ------------------------------------------------------------------

    def delete_entry(
        self,
        title: str,
        *,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        return bool(
            self._secrets.delete_secret(
                namespace=self.NAMESPACE,
                key=title,
                actor_id=actor_id,
                zone_id=zone_id,
                subject_id=subject_id,
                subject_type=subject_type,
            )
        )

    def restore_entry(
        self,
        title: str,
        *,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        return bool(
            self._secrets.restore_secret(
                namespace=self.NAMESPACE,
                key=title,
                actor_id=actor_id,
                zone_id=zone_id,
                subject_id=subject_id,
                subject_type=subject_type,
            )
        )
