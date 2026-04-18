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

from nexus.bricks.auth.postgres_profile_store import (
    CrossPrincipalConflict,
    PostgresAuthProfileStore,
)
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
    # "copy"              → row is absent in the target; will be inserted
    # "overwrite"         → row exists under the *same* principal; will be
    #                       re-written (only reached with ``force=True``)
    # "skip_exists"       → row exists under the *same* principal; plan opted
    #                       to leave it
    # "conflict_principal"→ row exists under a *different* principal in the
    #                       same tenant; aborts the apply to prevent
    #                       silent ownership reassignment (never auto-healed,
    #                       even with ``force=True`` — the operator must
    #                       delete the foreign row or pick a different
    #                       destination principal)
    # "error"             → plan-to-apply drift (source row disappeared)
    action: str
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

    Three cases for each source row:

    1. Target row exists under *another* principal in the same tenant →
       ``conflict_principal``. Never auto-healed — silently rewriting another
       principal's row is exactly the ownership-takeover failure mode we want
       to prevent. Operator must delete the foreign row or pick a different
       destination principal before the migration can proceed.
    2. Target row exists under *this* principal → ``skip_exists`` by default,
       ``overwrite`` if ``force=True``.
    3. No target row → ``copy``.
    """
    entries: list[PostgresMigrationEntry] = []
    target_principal = target.principal_id
    for profile in source.list():
        owners = target.tenant_scope_owners_of(profile.id)
        owned_by_self = target_principal in owners
        other_owners = sorted(owners - {target_principal})

        if other_owners:
            # Any foreign owner aborts the migration for this id — listing
            # all of them surfaces the full blast radius to the operator
            # (not just whichever row Postgres returned first).
            entries.append(
                PostgresMigrationEntry(
                    profile_id=profile.id,
                    provider=profile.provider,
                    action="conflict_principal",
                    reason=(
                        "row already owned by "
                        f"{', '.join(str(p) for p in other_owners)} "
                        "in the same tenant; refuse to reassign ownership "
                        "during migration"
                    ),
                )
            )
            continue

        if owned_by_self and not force:
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
                action="overwrite" if owned_by_self else "copy",
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

        if entry.action == "conflict_principal":
            # Ownership-takeover guard — counts as an error, not a skip, so
            # CLI exits non-zero and scripts do not treat it as a clean run.
            result.errors += 1
            continue

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

        # Atomic conflict-check + write. ``upsert_strict`` takes an
        # advisory lock scoped to ``(tenant_id, profile_id)`` inside the
        # same transaction, so concurrent writers targeting the same id in
        # the same tenant are serialized. The plan-time check is a
        # user-visible preview; ``upsert_strict`` is the authoritative
        # enforcement point.
        try:
            target.upsert_strict(_clone_with_fresh_usage_stats(profile))
            result.copied += 1
        except CrossPrincipalConflict as exc:
            entry.action = "conflict_principal"
            entry.reason = (
                "row already owned by "
                f"{', '.join(str(p) for p in exc.foreign_principals)} "
                "in the same tenant at apply time; refuse to reassign "
                "ownership"
            )
            result.errors += 1
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
