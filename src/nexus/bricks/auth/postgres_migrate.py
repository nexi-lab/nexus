"""Migration from SqliteAuthProfileStore to PostgresAuthProfileStore.

This is Phase F of epic #3788. Copy-only semantics: the SQLite source is
never modified, and an existing Postgres row (matched by ``id`` within the
target ``(tenant_id, principal_id)`` scope) is preserved unless ``--force``
is specified. Rerunning the tool after a partial apply is safe.

Unlike the existing ``auth migrate`` (OAuth→unified-profile, Phase 1 of
#3722), this migration moves rows between two ``AuthProfileStore``
implementations. Source routing metadata is preserved verbatim; the new
tenant/principal ownership columns are populated from caller-supplied IDs.

The tool does NOT cross the crypto boundary — PR 1 stores plaintext routing
metadata only. Credential resolution still flows through the configured
``CredentialBackend`` (nexus-token-manager, aws-cli, etc.). PR 2 will add
envelope-encrypted ciphertext columns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from nexus.bricks.auth.postgres_profile_store import PostgresAuthProfileStore
from nexus.bricks.auth.profile import AuthProfile, AuthProfileStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan / result types
# ---------------------------------------------------------------------------


@dataclass
class PostgresMigrationEntry:
    """One row in the SQLite→Postgres migration plan."""

    profile_id: str
    provider: str
    action: str  # "copy", "skip_exists", "overwrite", "error"
    reason: str = ""


@dataclass
class PostgresMigrationResult:
    entries: list[PostgresMigrationEntry] = field(default_factory=list)
    copied: int = 0
    skipped: int = 0
    errors: int = 0
    dry_run: bool = True

    @property
    def total(self) -> int:
        return self.copied + self.skipped + self.errors


# ---------------------------------------------------------------------------
# Plan / execute
# ---------------------------------------------------------------------------


def build_migration_plan(
    source: AuthProfileStore,
    target: PostgresAuthProfileStore,
    *,
    force: bool = False,
) -> list[PostgresMigrationEntry]:
    """Walk source rows and decide what to do for each against the target.

    ``force=True`` schedules an overwrite for rows that already exist in the
    target. Default is conservative — existing rows are skipped so operators
    can rerun the tool safely.
    """
    entries: list[PostgresMigrationEntry] = []
    for profile in source.list():
        existing = target.get(profile.id)
        if existing is not None and not force:
            entries.append(
                PostgresMigrationEntry(
                    profile_id=profile.id,
                    provider=profile.provider,
                    action="skip_exists",
                    reason="already present in target (use --force to overwrite)",
                )
            )
            continue

        entries.append(
            PostgresMigrationEntry(
                profile_id=profile.id,
                provider=profile.provider,
                action="overwrite" if existing is not None else "copy",
            )
        )
    return entries


def execute_migration(
    plan: list[PostgresMigrationEntry],
    source: AuthProfileStore,
    target: PostgresAuthProfileStore,
    *,
    apply: bool = False,
) -> PostgresMigrationResult:
    """Execute or dry-run a plan.

    Source profiles are looked up fresh rather than passed alongside the plan
    so that a profile deleted from the SQLite source between plan and apply
    surfaces as an ``error`` entry instead of silently writing stale state.
    """
    result = PostgresMigrationResult(dry_run=not apply)
    for entry in plan:
        result.entries.append(entry)

        if entry.action not in ("copy", "overwrite"):
            result.skipped += 1
            continue

        if not apply:
            result.copied += 1
            continue

        profile = source.get(entry.profile_id)
        if profile is None:
            entry.action = "error"
            entry.reason = "source row disappeared between plan and execute"
            result.errors += 1
            continue

        try:
            target.upsert(_clone_with_fresh_usage_stats(profile))
            result.copied += 1
        except Exception as exc:
            logger.warning("Postgres migration error for %s: %s", entry.profile_id, exc)
            entry.action = "error"
            entry.reason = str(exc)
            result.errors += 1

    return result


def _clone_with_fresh_usage_stats(profile: AuthProfile) -> AuthProfile:
    """Return a copy of ``profile`` without mutating the source object.

    Copy semantics: routing metadata and usage stats both carry over. The
    target store will stamp its own ``created_at`` / ``updated_at``. We do
    NOT reset stats — operators migrating from SQLite want their cooldown
    state and failure counts to follow the rows, otherwise every profile
    would look "healthy" on the new store and mask real ongoing issues.
    """
    return AuthProfile(
        id=profile.id,
        provider=profile.provider,
        account_identifier=profile.account_identifier,
        backend=profile.backend,
        backend_key=profile.backend_key,
        last_synced_at=profile.last_synced_at,
        sync_ttl_seconds=profile.sync_ttl_seconds,
        usage_stats=profile.usage_stats,
    )
