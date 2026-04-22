"""PasswordVaultService — domain wrapper over SecretsService.

On-demand service (not a BackgroundService) — no ``start`` / ``stop``
lifecycle. All storage, encryption, audit, versioning and soft-delete
is delegated to SecretsService; this module only serializes
``VaultEntry`` ↔ JSON and translates errors into domain exceptions.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, cast

import pyotp

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.secrets_access import AccessAuditContext
from nexus.services.password_vault.schema import VaultEntry

TOTP_PERIOD_SECONDS = 30
TOTP_AUDIT_EVENT_TYPE = "totp_generated"

# ``secrets_service`` is typed as ``Any`` below rather than the concrete
# ``nexus.bricks.secrets.service.SecretsService`` because the import-linter
# four-tier architecture contract forbids ``services`` → ``bricks`` imports
# (including TYPE_CHECKING-only). The wrapper is pure delegation; duck
# typing is sufficient. Expected shape: ``.put_secret``, ``.get_secret``,
# ``.list_secrets``, ``.delete_secret``, ``.restore_secret``,
# ``.list_versions``, ``.batch_get`` — all kwargs as used by the ``/api/v2/secrets``
# router.

logger = logging.getLogger(__name__)


class VaultEntryNotFoundError(LookupError):
    """Raised when a vault entry does not exist (or is soft-deleted)."""


class TotpNotConfiguredError(ValueError):
    """Raised when a vault entry exists but has no ``totp_secret`` to seed TOTP."""


class PasswordVaultService:
    """Domain-typed wrapper over SecretsService for vault entries.

    Every entry lives under a single SecretsService namespace
    (``"passwords"``) and uses the entry's ``title`` as the key.
    Values are JSON-serialised ``VaultEntry`` dicts; SecretsService
    itself sees only opaque strings.
    """

    NAMESPACE = "passwords"

    def __init__(self, secrets_service: Any) -> None:
        self._secrets = secrets_service
        # TOTP oracle cache keyed by ``(subject_id, title, window_index)``.
        # Within one 30s window we return the same code per (subject, entry);
        # callers that hammer the endpoint see one computation, not many.
        self._totp_cache: dict[tuple[str, str, int], str] = {}

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
        audit_context: AccessAuditContext | None = None,
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
            audit_context=audit_context,
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
        audit_context: AccessAuditContext | None = None,
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
            audit_context=audit_context,
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
        return cast(
            "list[dict[str, Any]]",
            self._secrets.list_versions(
                namespace=self.NAMESPACE,
                key=title,
                subject_id=subject_id,
                subject_type=subject_type,
            ),
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

    # ------------------------------------------------------------------
    # TOTP (RFC 6238)
    # ------------------------------------------------------------------

    def generate_totp(
        self,
        title: str,
        *,
        now: float | None = None,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
        audit_context: AccessAuditContext | None = None,
    ) -> dict[str, Any] | None:
        """Compute a current TOTP code from the entry's stored ``totp_secret``.

        The secret never leaves this process — only the 6-digit code and
        window metadata are returned. A single audit event typed
        ``totp_generated`` is recorded (distinct from ``key_accessed`` so
        audit queries can count TOTP requests separately).

        Args:
            title: Vault entry title.
            now: Override current time (seconds since epoch). Tests only.
            actor_id, zone_id, subject_id, subject_type: auth context.
            audit_context: Caller tag merged into audit details.

        Returns:
            ``{"code", "expires_in_seconds", "period_seconds"}`` on success,
            or ``None`` if the entry does not exist / is not visible to the
            subject. (Callers map ``None`` to HTTP 404.)

        Raises:
            TotpNotConfiguredError: Entry exists but has no ``totp_secret``.
                Callers map this to HTTP 422 (spec v3: distinguish
                "not found / unauthorized" from "no TOTP on this entry").
        """
        result = self._secrets.get_secret(
            namespace=self.NAMESPACE,
            key=title,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
            audit_context=audit_context,
            audit_event_type=TOTP_AUDIT_EVENT_TYPE,
        )
        if result is None:
            return None

        entry = VaultEntry.model_validate(json.loads(result["value"]))
        if not entry.totp_secret:
            raise TotpNotConfiguredError(title)

        now_seconds = int(now if now is not None else time.time())
        window_index = now_seconds // TOTP_PERIOD_SECONDS
        cache_key = (subject_id or "anonymous", title, window_index)

        code = self._totp_cache.get(cache_key)
        if code is None:
            code = pyotp.TOTP(entry.totp_secret).at(now_seconds)
            self._totp_cache[cache_key] = code
            # Drop entries from past windows so the dict doesn't grow
            # unbounded under long-running servers.
            self._prune_totp_cache(current_window=window_index)

        return {
            "code": code,
            "expires_in_seconds": TOTP_PERIOD_SECONDS - (now_seconds % TOTP_PERIOD_SECONDS),
            "period_seconds": TOTP_PERIOD_SECONDS,
        }

    def _prune_totp_cache(self, *, current_window: int) -> None:
        """Drop cache entries from windows older than ``current_window``."""
        stale = [k for k in self._totp_cache if k[2] < current_window]
        for k in stale:
            self._totp_cache.pop(k, None)
