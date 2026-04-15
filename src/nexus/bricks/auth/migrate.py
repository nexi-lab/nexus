"""Dual-read migration from old OAuth credential store to unified AuthProfile store.

Phase 1 of epic #3722 (#3738). Migration is copy-only — never deletes from
the old store. The old store remains authoritative for writes until Phase 4.

Commands:
  nexus auth migrate          — dry-run, prints the plan
  nexus auth migrate --apply  — copies rows into the new profile store

Architecture (decision 3A): dual-read is implemented at the store layer via
DualReadAuthProfileStore, which wraps the new SqliteAuthProfileStore and an
adapter over the old OAuthCredentialService.

Decision 8A: migration is copy-only, never deletes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nexus.bricks.auth.credential_backend import NexusTokenManagerBackend
from nexus.bricks.auth.profile import (
    AuthProfile,
    AuthProfileFailureReason,
    AuthProfileStore,
    ProfileUsageStats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Migration plan / result types
# ---------------------------------------------------------------------------


@dataclass
class MigrationEntry:
    """One row in the migration plan."""

    profile_id: str
    provider: str
    user_email: str
    action: str  # "copy", "skip_exists", "skip_unmappable"
    reason: str = ""


@dataclass
class MigrationResult:
    """Summary of a migration run."""

    entries: list[MigrationEntry] = field(default_factory=list)
    copied: int = 0
    skipped: int = 0
    errors: int = 0
    dry_run: bool = True

    @property
    def total(self) -> int:
        return self.copied + self.skipped + self.errors


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------


def build_migration_plan(
    old_credentials: list[dict[str, Any]],
    new_store: AuthProfileStore,
) -> list[MigrationEntry]:
    """Build a migration plan from old OAuth credentials to new profile store.

    Args:
        old_credentials: list of dicts from OAuthCredentialService.list_credentials()
        new_store: the target SqliteAuthProfileStore

    Returns:
        List of MigrationEntry describing what would happen.
    """
    entries: list[MigrationEntry] = []

    for cred in old_credentials:
        provider = cred.get("provider", "")
        user_email = cred.get("user_email", "")
        zone_id = cred.get("zone_id") or ""

        if not provider or not user_email:
            entries.append(
                MigrationEntry(
                    profile_id="",
                    provider=provider,
                    user_email=user_email,
                    action="skip_unmappable",
                    reason="missing provider or user_email",
                )
            )
            continue

        # Include zone_id in profile identity to avoid collapsing distinct
        # zone-scoped credentials onto one profile (adversarial finding #1).
        if zone_id and zone_id != "root":
            profile_id = f"{provider}/{user_email}/{zone_id}"
        else:
            profile_id = f"{provider}/{user_email}"

        # Check if already exists in new store
        existing = new_store.get(profile_id)
        if existing is not None:
            entries.append(
                MigrationEntry(
                    profile_id=profile_id,
                    provider=provider,
                    user_email=user_email,
                    action="skip_exists",
                    reason="already present in new store",
                )
            )
            continue

        entries.append(
            MigrationEntry(
                profile_id=profile_id,
                provider=provider,
                user_email=user_email,
                action="copy",
            )
        )

    return entries


def execute_migration(
    plan: list[MigrationEntry],
    old_credentials: list[dict[str, Any]],
    new_store: AuthProfileStore,
    *,
    apply: bool = False,
) -> MigrationResult:
    """Execute (or dry-run) a migration plan.

    Args:
        plan: output of build_migration_plan()
        old_credentials: same list passed to build_migration_plan()
        new_store: target store to write into
        apply: if False (default), dry-run only — no writes

    Returns:
        MigrationResult with counts and per-entry details.
    """
    result = MigrationResult(dry_run=not apply)

    # Index old credentials by zone-aware profile_id for fast lookup
    cred_by_id: dict[str, dict[str, Any]] = {}
    for cred in old_credentials:
        provider = cred.get("provider", "")
        user_email = cred.get("user_email", "")
        zone_id = cred.get("zone_id") or ""
        if provider and user_email:
            if zone_id and zone_id != "root":
                key = f"{provider}/{user_email}/{zone_id}"
            else:
                key = f"{provider}/{user_email}"
            cred_by_id[key] = cred

    for entry in plan:
        result.entries.append(entry)

        if entry.action != "copy":
            result.skipped += 1
            continue

        if not apply:
            result.copied += 1  # would-copy count for dry-run
            continue

        # Actually copy
        cred: dict[str, Any] | None = cred_by_id.get(entry.profile_id)
        if cred is None:
            entry.action = "skip_unmappable"
            entry.reason = "credential disappeared between plan and execute"
            result.errors += 1
            continue

        try:
            zone_id = cred.get("zone_id")
            backend_key = NexusTokenManagerBackend.make_backend_key(
                entry.provider,
                entry.user_email,
                zone_id,
            )

            profile = AuthProfile(
                id=entry.profile_id,
                provider=entry.provider,
                account_identifier=entry.user_email,
                backend=NexusTokenManagerBackend._NAME,
                backend_key=backend_key,
                last_synced_at=datetime.utcnow(),
                usage_stats=ProfileUsageStats(),
            )
            new_store.upsert(profile)
            result.copied += 1
        except Exception as exc:
            logger.warning(
                "Migration error for %s: %s",
                entry.profile_id,
                exc,
            )
            entry.action = "error"
            entry.reason = str(exc)
            result.errors += 1

    return result


# ---------------------------------------------------------------------------
# DualReadAuthProfileStore (decision 3A: store-layer composition)
# ---------------------------------------------------------------------------


class OldStoreAdapter:
    """Adapts the old OAuthCredentialService to the AuthProfileStore read interface.

    Only implements list() and get() — the old store is read-only from the
    dual-read perspective. Writes still go through OAuthCredentialService
    directly (until Phase 4).

    IMPORTANT: This is a point-in-time snapshot, not a live read-through.
    Credentials added/revoked in the old store after construction are not
    visible until the adapter is reconstructed. This is acceptable for the
    migration CLI (runs once) and short-lived dual-read windows. Phase 2
    (#3739) should replace this with a live adapter backed by the actual
    OAuthCredentialService if dual-read is used in long-running processes.
    """

    def __init__(self, old_credentials: list[dict[str, Any]]) -> None:
        self._profiles: dict[str, AuthProfile] = {}
        for cred in old_credentials:
            provider = cred.get("provider", "")
            user_email = cred.get("user_email", "")
            if not provider or not user_email:
                continue
            zone_id = cred.get("zone_id") or ""
            if zone_id and zone_id != "root":
                profile_id = f"{provider}/{user_email}/{zone_id}"
            else:
                profile_id = f"{provider}/{user_email}"
            backend_key = NexusTokenManagerBackend.make_backend_key(
                provider,
                user_email,
                zone_id if zone_id and zone_id != "root" else None,
            )
            self._profiles[profile_id] = AuthProfile(
                id=profile_id,
                provider=provider,
                account_identifier=user_email,
                backend=NexusTokenManagerBackend._NAME,
                backend_key=backend_key,
                usage_stats=ProfileUsageStats(),
            )

    def list(self, *, provider: str | None = None) -> list[AuthProfile]:
        if provider is None:
            return list(self._profiles.values())
        return [p for p in self._profiles.values() if p.provider == provider]

    def get(self, profile_id: str) -> AuthProfile | None:
        return self._profiles.get(profile_id)


class DualReadAuthProfileStore:
    """Reads from new store first, falls back to old store adapter.

    Writes go to the new store only. The old store is read-only.

    This wrapper is used during the dual-read migration window (Phase 1-3).
    Phase 4 removes the dual-read and the old store.
    """

    def __init__(
        self,
        new_store: AuthProfileStore,
        old_adapter: OldStoreAdapter,
    ) -> None:
        self._new = new_store
        self._old = old_adapter

    def list(self, *, provider: str | None = None) -> list[AuthProfile]:
        # Merge both stores. On ID collision: old store provides identity/routing
        # (authoritative for writes during migration window), but new store's
        # usage_stats are preserved (cooldowns, failure counts, disable flags).
        new_profiles = self._new.list(provider=provider)
        old_profiles = self._old.list(provider=provider)
        new_by_id: dict[str, AuthProfile] = {p.id: p for p in new_profiles}
        merged: dict[str, AuthProfile] = dict(new_by_id)
        for p in old_profiles:
            if p.id in new_by_id:
                # Collision: use old identity, preserve new store's usage_stats
                merged[p.id] = AuthProfile(
                    id=p.id,
                    provider=p.provider,
                    account_identifier=p.account_identifier,
                    backend=p.backend,
                    backend_key=p.backend_key,
                    last_synced_at=p.last_synced_at,
                    sync_ttl_seconds=p.sync_ttl_seconds,
                    usage_stats=new_by_id[p.id].usage_stats,
                )
            else:
                merged[p.id] = p
        return list(merged.values())

    def get(self, profile_id: str) -> AuthProfile | None:
        old_result = self._old.get(profile_id)
        new_result = self._new.get(profile_id)
        if old_result is not None and new_result is not None:
            # Collision: old identity + new usage_stats
            return AuthProfile(
                id=old_result.id,
                provider=old_result.provider,
                account_identifier=old_result.account_identifier,
                backend=old_result.backend,
                backend_key=old_result.backend_key,
                last_synced_at=old_result.last_synced_at,
                sync_ttl_seconds=old_result.sync_ttl_seconds,
                usage_stats=new_result.usage_stats,
            )
        if old_result is not None:
            return old_result
        return new_result

    def upsert(self, profile: AuthProfile) -> None:
        self._new.upsert(profile)

    def delete(self, profile_id: str) -> None:
        self._new.delete(profile_id)

    def mark_success(self, profile_id: str) -> None:
        self._new.mark_success(profile_id)

    def mark_failure(
        self,
        profile_id: str,
        reason: AuthProfileFailureReason,
        *,
        raw_error: str | None = None,
    ) -> None:
        self._new.mark_failure(profile_id, reason, raw_error=raw_error)
