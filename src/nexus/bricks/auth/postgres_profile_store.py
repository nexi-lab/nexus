"""PostgreSQL-backed AuthProfileStore for multi-tenant server deployments.

Implements the AuthProfileStore protocol against PostgreSQL with:
  - (tenant_id, principal_id) ownership model (epic #3788 blocker 1)
  - Row-Level Security (RLS) keyed off ``app.current_tenant`` session var
  - FORCE RLS so policies apply even when connected as table owner
  - Tenant-scoped primary keys: ``(tenant_id, id)`` on auth_profiles
  - UUID primary keys generated application-side (no pgcrypto dependency)

Schema overview::

    tenants(id, name, created_at)
    principals(id, tenant_id, kind, parent_principal_id, delegated_scope)
    principal_aliases(auth_method, external_sub, principal_id)
    auth_profiles(tenant_id, id, principal_id, provider, account_identifier,
                  backend, backend_key, <stats columns>, created_at, updated_at)

Writes go through a pooled SQLAlchemy engine. Every transaction issues
``SET LOCAL app.current_tenant`` before the first statement so RLS scopes
rows to this store's configured tenant. Unlike SqliteAuthProfileStore there
is no in-memory LRU cache — Postgres is multi-writer, so per-process caching
would be stale in the presence of other daemons. Local read-through caching
is deferred to the client-side nexus-bot daemon (epic #3788 Phase D).

Crypto columns (ciphertext, wrapped_dek, nonce, aad, kek_version) are added
in this PR (#3803, Phase C). Rows written without an ``encryption_provider``
leave those columns NULL and remain readable; rows written with
``upsert_with_credential`` carry the full envelope tuple.

Feature gate: instantiated only when ``NEXUS_AUTH_STORE=postgres``. Default
remains SqliteAuthProfileStore until downstream consumers migrate.
"""

from __future__ import annotations

import builtins
import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.envelope import (
    AADMismatch,
    AESGCMEnvelope,
    DEKCache,
    EncryptionProvider,
    EnvelopeError,
)
from nexus.bricks.auth.envelope_metrics import (
    DEK_CACHE_HITS,
    DEK_CACHE_MISSES,
    DEK_UNWRAP_ERRORS,
    DEK_UNWRAP_LATENCY,
    KEK_ROTATE_ROWS,
)
from nexus.bricks.auth.profile import (
    RAW_ERROR_MAX_LEN,
    AuthProfile,
    AuthProfileFailureReason,
    ProfileUsageStats,
)

logger = logging.getLogger(__name__)


class CrossPrincipalConflict(Exception):
    """Raised by ``PostgresAuthProfileStore.upsert_strict`` when the target
    ``profile.id`` is already owned by another principal in the same tenant.

    Callers (notably the migration CLI) surface this as a policy decision —
    not a database error — so the operator can delete the foreign row or
    retarget the migration.
    """

    def __init__(self, *, profile_id: str, foreign_principals: list[uuid.UUID]):
        self.profile_id = profile_id
        self.foreign_principals = foreign_principals
        super().__init__(
            f"profile_id={profile_id!r} already owned by "
            f"{', '.join(str(p) for p in foreign_principals)} in the same tenant"
        )


# ---------------------------------------------------------------------------
# Schema (idempotent CREATE TABLE IF NOT EXISTS + RLS policies)
# ---------------------------------------------------------------------------

# Table + index DDL. Does NOT touch RLS so migrations and backfills
# (in _upgrade_shape_in_place) can run before any FORCE ROW LEVEL SECURITY
# policy is active. Production-critical: backfills under FORCE RLS with a
# non-BYPASSRLS role silently affect zero rows.
_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id          UUID PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS principals (
        id                  UUID PRIMARY KEY,
        tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        kind                TEXT NOT NULL CHECK (kind IN ('human','agent','machine')),
        parent_principal_id UUID REFERENCES principals(id) ON DELETE SET NULL,
        delegated_scope     JSONB,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_principals_tenant_id ON principals(tenant_id)",
    # UNIQUE (id, tenant_id) is a no-op for uniqueness (id is already
    # PRIMARY KEY) but lets principal_aliases carry a composite FK
    # ``(tenant_id, principal_id) -> principals(tenant_id, id)`` so a
    # malformed alias cannot point at a principal that lives in another
    # tenant. Schema-level tenant/principal consistency.
    "CREATE UNIQUE INDEX IF NOT EXISTS uix_principals_id_tenant ON principals(id, tenant_id)",
    """
    CREATE TABLE IF NOT EXISTS principal_aliases (
        tenant_id    UUID NOT NULL,
        auth_method  TEXT NOT NULL,
        external_sub TEXT NOT NULL,
        principal_id UUID NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (tenant_id, auth_method, external_sub),
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
        FOREIGN KEY (principal_id, tenant_id)
            REFERENCES principals(id, tenant_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_principal_aliases_principal_id ON principal_aliases(principal_id)",
    """
    CREATE TABLE IF NOT EXISTS auth_profiles (
        tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        principal_id       UUID NOT NULL,
        id                 TEXT NOT NULL,
        provider           TEXT NOT NULL,
        account_identifier TEXT NOT NULL,
        backend            TEXT NOT NULL,
        backend_key        TEXT NOT NULL,
        last_synced_at     TIMESTAMPTZ,
        sync_ttl_seconds   INTEGER NOT NULL DEFAULT 300,
        last_used_at       TIMESTAMPTZ,
        success_count      INTEGER NOT NULL DEFAULT 0,
        failure_count      INTEGER NOT NULL DEFAULT 0,
        cooldown_until     TIMESTAMPTZ,
        cooldown_reason    TEXT,
        disabled_until     TIMESTAMPTZ,
        raw_error          TEXT,
        ciphertext         BYTEA,
        wrapped_dek        BYTEA,
        nonce              BYTEA,
        aad                BYTEA,
        kek_version        INTEGER,
        source_file_hash   TEXT,      -- #3804 audit stamp
        daemon_version     TEXT,      -- #3804 audit stamp
        machine_id         UUID,      -- #3804 audit stamp
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (tenant_id, principal_id, id),
        FOREIGN KEY (principal_id, tenant_id)
            REFERENCES principals(id, tenant_id) ON DELETE CASCADE,
        CONSTRAINT auth_profiles_envelope_all_or_none CHECK (
            (ciphertext IS NULL) = (wrapped_dek IS NULL)
            AND (ciphertext IS NULL) = (nonce IS NULL)
            AND (ciphertext IS NULL) = (aad IS NULL)
            AND (ciphertext IS NULL) = (kek_version IS NULL)
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auth_profiles_provider ON auth_profiles(tenant_id, principal_id, provider)",
    "CREATE INDEX IF NOT EXISTS idx_auth_profiles_tenant_id_only ON auth_profiles(tenant_id, id)",
    """
    CREATE TABLE IF NOT EXISTS daemon_machines (
        id                         UUID PRIMARY KEY,
        tenant_id                  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        principal_id               UUID NOT NULL,
        pubkey                     BYTEA NOT NULL,
        daemon_version_last_seen   TEXT,
        enrolled_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        revoked_at                 TIMESTAMPTZ,
        hostname                   TEXT,
        FOREIGN KEY (principal_id, tenant_id)
            REFERENCES principals(id, tenant_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daemon_machines_tenant_principal "
    "ON daemon_machines(tenant_id, principal_id)",
    """
    CREATE TABLE IF NOT EXISTS daemon_enroll_tokens (
        jti              UUID PRIMARY KEY,
        tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        principal_id     UUID NOT NULL,
        issued_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at       TIMESTAMPTZ NOT NULL,
        used_at          TIMESTAMPTZ,
        FOREIGN KEY (principal_id, tenant_id)
            REFERENCES principals(id, tenant_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daemon_enroll_tokens_expires "
    "ON daemon_enroll_tokens(expires_at)",
)

# RLS statements. Run LAST so the backfill in _upgrade_shape_in_place is not
# shadowed by FORCE RLS policies before the backfill row visibility is set.
_RLS_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE tenants ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tenants FORCE ROW LEVEL SECURITY",
    "ALTER TABLE principals ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE principals FORCE ROW LEVEL SECURITY",
    "ALTER TABLE principal_aliases ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE principal_aliases FORCE ROW LEVEL SECURITY",
    "ALTER TABLE auth_profiles ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE auth_profiles FORCE ROW LEVEL SECURITY",
    "ALTER TABLE daemon_machines ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE daemon_machines FORCE ROW LEVEL SECURITY",
    "ALTER TABLE daemon_enroll_tokens ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE daemon_enroll_tokens FORCE ROW LEVEL SECURITY",
)

# Policies are separate because ``CREATE POLICY`` lacks IF NOT EXISTS in
# PostgreSQL <15 — we drop-then-create for idempotency.
_POLICY_STATEMENTS: tuple[str, ...] = (
    "DROP POLICY IF EXISTS tenant_isolation_tenants ON tenants",
    """
    CREATE POLICY tenant_isolation_tenants ON tenants
        USING (id = current_setting('app.current_tenant', true)::UUID)
    """,
    "DROP POLICY IF EXISTS tenant_isolation_principals ON principals",
    """
    CREATE POLICY tenant_isolation_principals ON principals
        USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    """,
    "DROP POLICY IF EXISTS tenant_isolation_aliases ON principal_aliases",
    """
    CREATE POLICY tenant_isolation_aliases ON principal_aliases
        USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    """,
    "DROP POLICY IF EXISTS tenant_isolation_auth_profiles ON auth_profiles",
    """
    CREATE POLICY tenant_isolation_auth_profiles ON auth_profiles
        USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    """,
    "DROP POLICY IF EXISTS tenant_isolation_daemon_machines ON daemon_machines",
    """
    CREATE POLICY tenant_isolation_daemon_machines ON daemon_machines
        USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    """,
    "DROP POLICY IF EXISTS tenant_isolation_daemon_enroll_tokens ON daemon_enroll_tokens",
    """
    CREATE POLICY tenant_isolation_daemon_enroll_tokens ON daemon_enroll_tokens
        USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    """,
)


def ensure_schema(engine: Engine) -> None:
    """Create (idempotently) every table, index, and RLS policy, and upgrade
    an already-present older shape in place.

    Intended for:
      - Test fixtures (fresh schema per module)
      - Dev bootstrap
      - First-run on an empty production database
      - Upgrading a database that was already bootstrapped by an earlier
        iteration of this module, where ``principal_aliases`` had no
        ``tenant_id`` column and ``auth_profiles`` had PK ``(tenant_id, id)``

    The upgrade path is in-place and idempotent: on a fresh DB the ALTER
    statements are no-ops because the CREATE TABLE DDL already produces the
    current shape. Production rollouts will formalise this with Alembic once
    the Postgres path actually goes live.
    """
    # Advisory lock serializes concurrent ensure_schema() runs (multi-replica
    # bootstrap, parallel test processes). The lock key is a stable hash of
    # the module-qualified name so it never collides with unrelated callers.
    # pg_advisory_xact_lock releases automatically at transaction end.
    _LOCK_KEY = 0x3788A17F  # "#3788 auth-store schema" — arbitrary, stable
    with engine.begin() as conn:
        conn.execute(text(f"SELECT pg_advisory_xact_lock({_LOCK_KEY})"))
        # Order matters: tables → legacy-shape upgrade (backfill UPDATE needs
        # row visibility) → RLS enable/force → policies. Flipping any pair
        # breaks bootstrap on a non-superuser role with pre-existing data.
        for stmt in _TABLE_STATEMENTS:
            conn.execute(text(stmt))
        _upgrade_shape_in_place(conn)
        for stmt in _RLS_STATEMENTS:
            conn.execute(text(stmt))
        for stmt in _POLICY_STATEMENTS:
            conn.execute(text(stmt))


_UPGRADE_TABLES = ("tenants", "principals", "principal_aliases", "auth_profiles")


def _upgrade_shape_in_place(conn: Connection) -> None:
    """Run the ALTERs needed to upgrade a pre-composite-PK shape.

    - ``principal_aliases``: add ``tenant_id`` column + backfill from
      ``principals`` + rebuild PK to ``(tenant_id, auth_method, external_sub)``
      + add composite ``(principal_id, tenant_id)`` FK
    - ``auth_profiles``: rebuild PK from ``(tenant_id, id)`` to
      ``(tenant_id, principal_id, id)``

    Uses ``information_schema`` to detect which steps are needed, so running
    this on an already-current schema is a no-op.

    FORCE ROW LEVEL SECURITY is temporarily disabled for the duration of
    the upgrade so that the cross-tenant backfill ``UPDATE`` can actually
    see the legacy rows. Running a backfill under an active alias policy
    (``tenant_id = current_setting('app.current_tenant', true)::UUID``)
    would silently match zero rows because ``app.current_tenant`` is not
    set during schema bootstrap — ``SET NOT NULL`` would then abort. RLS
    state is restored at the tail so ``_RLS_STATEMENTS`` re-forces policy
    enforcement unchanged. All ``ALTER TABLE`` and the RLS toggling run in
    the same transaction, so a mid-upgrade abort does not leave RLS
    disabled.
    """
    # Record current RLS state so we can restore it, then disable. If a
    # table is absent (first-run, CREATE above already emitted the new
    # shape), the disable no-ops — the statement works on any existing
    # table and we don't care about the initial state.
    for tbl in _UPGRADE_TABLES:
        conn.execute(text(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY"))

    # --- principal_aliases: ensure tenant_id column exists ---
    # Filter by current schema so a sibling schema (e.g. a different
    # test module's copy of the tables) does not fool the upgrade check.
    has_tenant_col = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = CURRENT_SCHEMA() "
            "  AND table_name = 'principal_aliases' "
            "  AND column_name = 'tenant_id'"
        )
    ).fetchone()
    if has_tenant_col is None:
        # IF NOT EXISTS makes the DDL safe if a concurrent runner beat us
        # after the information_schema check.
        conn.execute(
            text(
                "ALTER TABLE principal_aliases "
                "ADD COLUMN IF NOT EXISTS tenant_id UUID "
                "REFERENCES tenants(id) ON DELETE CASCADE"
            )
        )
        conn.execute(
            text(
                "UPDATE principal_aliases pa "
                "SET tenant_id = p.tenant_id "
                "FROM principals p "
                "WHERE pa.principal_id = p.id AND pa.tenant_id IS NULL"
            )
        )
        conn.execute(text("ALTER TABLE principal_aliases ALTER COLUMN tenant_id SET NOT NULL"))
        conn.execute(
            text("ALTER TABLE principal_aliases DROP CONSTRAINT IF EXISTS principal_aliases_pkey")
        )
        conn.execute(
            text(
                "ALTER TABLE principal_aliases "
                "ADD PRIMARY KEY (tenant_id, auth_method, external_sub)"
            )
        )

    # --- principal_aliases: composite (principal_id, tenant_id) FK ---
    # Ensures upgraded installs get the same tenant/principal consistency
    # invariant that fresh installs get from CREATE TABLE. Without this a
    # malformed alias row could point to a principal in another tenant even
    # though its own ``tenant_id`` column says otherwise — the alias RLS
    # policy trusts ``tenant_id`` directly, so that would be a real trust
    # boundary gap.
    has_composite_fk = conn.execute(
        text(
            "SELECT 1 FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON rc.constraint_name = kcu.constraint_name "
            " AND rc.constraint_schema = kcu.table_schema "
            "WHERE kcu.table_schema = CURRENT_SCHEMA() "
            "  AND kcu.table_name = 'principal_aliases' "
            "  AND kcu.column_name IN ('principal_id', 'tenant_id') "
            "GROUP BY rc.constraint_name "
            "HAVING COUNT(DISTINCT kcu.column_name) = 2 "
            "LIMIT 1"
        )
    ).fetchone()
    if has_composite_fk is None:
        # Drop the legacy single-column FK if it exists, then add the
        # composite FK. ``information_schema`` would report the legacy FK
        # as a referential constraint too, so the guard above checks
        # specifically for a constraint that covers BOTH columns.
        conn.execute(
            text(
                "ALTER TABLE principal_aliases "
                "DROP CONSTRAINT IF EXISTS principal_aliases_principal_id_fkey"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE principal_aliases "
                "ADD CONSTRAINT principal_aliases_principal_tenant_fkey "
                "FOREIGN KEY (principal_id, tenant_id) "
                "REFERENCES principals(id, tenant_id) ON DELETE CASCADE"
            )
        )

    # --- auth_profiles: rebuild PK to include principal_id ---
    pk_cols = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                " AND tc.table_schema = kcu.table_schema "
                " AND tc.table_name = kcu.table_name "
                "WHERE tc.table_schema = CURRENT_SCHEMA() "
                "  AND tc.table_name = 'auth_profiles' "
                "  AND tc.constraint_type = 'PRIMARY KEY' "
                "ORDER BY kcu.ordinal_position"
            )
        ).fetchall()
    ]
    if pk_cols and set(pk_cols) != {"tenant_id", "principal_id", "id"}:
        conn.execute(text("ALTER TABLE auth_profiles DROP CONSTRAINT IF EXISTS auth_profiles_pkey"))
        conn.execute(
            text("ALTER TABLE auth_profiles ADD PRIMARY KEY (tenant_id, principal_id, id)")
        )

    # --- auth_profiles: composite (principal_id, tenant_id) FK ---
    # Same invariant as principal_aliases: tenant_id and principal_id on an
    # auth_profiles row must reference the same principal. Fresh installs
    # inherit this from CREATE TABLE; upgraded installs need the ALTER.
    has_ap_composite_fk = conn.execute(
        text(
            "SELECT 1 FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON rc.constraint_name = kcu.constraint_name "
            " AND rc.constraint_schema = kcu.table_schema "
            "WHERE kcu.table_schema = CURRENT_SCHEMA() "
            "  AND kcu.table_name = 'auth_profiles' "
            "  AND kcu.column_name IN ('principal_id', 'tenant_id') "
            "GROUP BY rc.constraint_name "
            "HAVING COUNT(DISTINCT kcu.column_name) = 2 "
            "LIMIT 1"
        )
    ).fetchone()
    if has_ap_composite_fk is None:
        conn.execute(
            text(
                "ALTER TABLE auth_profiles "
                "DROP CONSTRAINT IF EXISTS auth_profiles_principal_id_fkey"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE auth_profiles "
                "ADD CONSTRAINT auth_profiles_principal_tenant_fkey "
                "FOREIGN KEY (principal_id, tenant_id) "
                "REFERENCES principals(id, tenant_id) ON DELETE CASCADE"
            )
        )

    # --- auth_profiles: envelope encryption columns (issue #3803) ---
    for col, decl in (
        ("ciphertext", "BYTEA"),
        ("wrapped_dek", "BYTEA"),
        ("nonce", "BYTEA"),
        ("aad", "BYTEA"),
        ("kek_version", "INTEGER"),
        ("source_file_hash", "TEXT"),  # #3804 audit stamp
        ("daemon_version", "TEXT"),  # #3804 audit stamp
        ("machine_id", "UUID"),  # #3804 audit stamp (fk to daemon_machines.id)
    ):
        conn.execute(text(f"ALTER TABLE auth_profiles ADD COLUMN IF NOT EXISTS {col} {decl}"))
    # CHECK constraint. Use DROP ... IF EXISTS + ADD for idempotency.
    conn.execute(
        text(
            "ALTER TABLE auth_profiles DROP CONSTRAINT IF EXISTS auth_profiles_envelope_all_or_none"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auth_profiles "
            "ADD CONSTRAINT auth_profiles_envelope_all_or_none CHECK ("
            "    (ciphertext IS NULL) = (wrapped_dek IS NULL)"
            "    AND (ciphertext IS NULL) = (nonce IS NULL)"
            "    AND (ciphertext IS NULL) = (aad IS NULL)"
            "    AND (ciphertext IS NULL) = (kek_version IS NULL)"
            ")"
        )
    )


def drop_schema(engine: Engine) -> None:
    """Drop every table created by ``ensure_schema`` (test teardown helper)."""
    with engine.begin() as conn:
        for tbl in (
            "daemon_enroll_tokens",
            "daemon_machines",
            "auth_profiles",
            "principal_aliases",
            "principals",
            "tenants",
        ):
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))


# ---------------------------------------------------------------------------
# Admin helpers — used by tests + the migration CLI (Phase F)
# ---------------------------------------------------------------------------


def ensure_tenant(engine: Engine, name: str) -> uuid.UUID:
    """Return the tenant_id for ``name``, creating the row if absent.

    Bypasses RLS by running as the connecting role (assumes admin context
    for provisioning). Callers must not expose this helper to tenant-scoped
    request handlers.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id FROM tenants WHERE name = :name"),
            {"name": name},
        ).fetchone()
        if row is not None:
            return uuid.UUID(str(row[0]))
        tenant_id = uuid.uuid4()
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": name},
        )
        return tenant_id


def ensure_principal(
    engine: Engine,
    *,
    tenant_id: uuid.UUID,
    kind: str = "human",
    external_sub: str | None = None,
    auth_method: str = "bootstrap",
    parent_principal_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Return principal_id for the given tenant+alias, creating rows if absent.

    ``external_sub`` keys the alias (e.g. OIDC sub claim, machine keypair
    fingerprint). ``auth_method`` is the provider that issued it (``oidc``,
    ``bound-keypair``, ``bootstrap`` for tests/migration).

    Aliases are tenant-scoped: the same ``(auth_method, external_sub)`` can
    map to different principals in different tenants. This keeps tenants as
    hard isolation boundaries — a human whose OIDC sub happens to match
    across tenants is modelled as two distinct principals.
    """
    with engine.begin() as conn:
        if external_sub is not None:
            alias = conn.execute(
                text(
                    "SELECT principal_id FROM principal_aliases "
                    "WHERE tenant_id = :tid "
                    "AND auth_method = :m "
                    "AND external_sub = :s"
                ),
                {"tid": tenant_id, "m": auth_method, "s": external_sub},
            ).fetchone()
            if alias is not None:
                return uuid.UUID(str(alias[0]))

        principal_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind, parent_principal_id) "
                "VALUES (:id, :tid, :kind, :parent)"
            ),
            {
                "id": principal_id,
                "tid": tenant_id,
                "kind": kind,
                "parent": parent_principal_id,
            },
        )
        if external_sub is not None:
            conn.execute(
                text(
                    "INSERT INTO principal_aliases "
                    "    (tenant_id, auth_method, external_sub, principal_id) "
                    "VALUES (:tid, :m, :s, :p)"
                ),
                {
                    "tid": tenant_id,
                    "m": auth_method,
                    "s": external_sub,
                    "p": principal_id,
                },
            )
        return principal_id


# ---------------------------------------------------------------------------
# SQL statements used by the store (tenant/principal scoping baked in)
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO auth_profiles (
    tenant_id, principal_id, id,
    provider, account_identifier, backend, backend_key,
    last_synced_at, sync_ttl_seconds,
    last_used_at, success_count, failure_count,
    cooldown_until, cooldown_reason, disabled_until, raw_error,
    updated_at
) VALUES (
    :tenant_id, :principal_id, :id,
    :provider, :account_identifier, :backend, :backend_key,
    :last_synced_at, :sync_ttl_seconds,
    :last_used_at, :success_count, :failure_count,
    :cooldown_until, :cooldown_reason, :disabled_until, :raw_error,
    NOW()
)
ON CONFLICT (tenant_id, principal_id, id) DO UPDATE SET
    provider           = EXCLUDED.provider,
    account_identifier = EXCLUDED.account_identifier,
    backend            = EXCLUDED.backend,
    backend_key        = EXCLUDED.backend_key,
    last_synced_at     = EXCLUDED.last_synced_at,
    sync_ttl_seconds   = EXCLUDED.sync_ttl_seconds,
    last_used_at       = EXCLUDED.last_used_at,
    success_count      = EXCLUDED.success_count,
    failure_count      = EXCLUDED.failure_count,
    cooldown_until     = EXCLUDED.cooldown_until,
    cooldown_reason    = EXCLUDED.cooldown_reason,
    disabled_until     = EXCLUDED.disabled_until,
    raw_error          = EXCLUDED.raw_error,
    updated_at         = NOW()
"""

_UPSERT_WITH_CREDENTIAL_SQL = """
INSERT INTO auth_profiles (
    tenant_id, principal_id, id,
    provider, account_identifier, backend, backend_key,
    last_synced_at, sync_ttl_seconds,
    last_used_at, success_count, failure_count,
    cooldown_until, cooldown_reason, disabled_until, raw_error,
    ciphertext, wrapped_dek, nonce, aad, kek_version,
    source_file_hash, daemon_version, machine_id,
    updated_at
) VALUES (
    :tenant_id, :principal_id, :id,
    :provider, :account_identifier, :backend, :backend_key,
    :last_synced_at, :sync_ttl_seconds,
    :last_used_at, :success_count, :failure_count,
    :cooldown_until, :cooldown_reason, :disabled_until, :raw_error,
    :ciphertext, :wrapped_dek, :nonce, :aad, :kek_version,
    :source_file_hash, :daemon_version, :machine_id,
    NOW()
)
ON CONFLICT (tenant_id, principal_id, id) DO UPDATE SET
    provider           = EXCLUDED.provider,
    account_identifier = EXCLUDED.account_identifier,
    backend            = EXCLUDED.backend,
    backend_key        = EXCLUDED.backend_key,
    last_synced_at     = EXCLUDED.last_synced_at,
    sync_ttl_seconds   = EXCLUDED.sync_ttl_seconds,
    last_used_at       = EXCLUDED.last_used_at,
    success_count      = EXCLUDED.success_count,
    failure_count      = EXCLUDED.failure_count,
    cooldown_until     = EXCLUDED.cooldown_until,
    cooldown_reason    = EXCLUDED.cooldown_reason,
    disabled_until     = EXCLUDED.disabled_until,
    raw_error          = EXCLUDED.raw_error,
    ciphertext         = EXCLUDED.ciphertext,
    wrapped_dek        = EXCLUDED.wrapped_dek,
    nonce              = EXCLUDED.nonce,
    aad                = EXCLUDED.aad,
    kek_version        = EXCLUDED.kek_version,
    source_file_hash   = EXCLUDED.source_file_hash,
    daemon_version     = EXCLUDED.daemon_version,
    machine_id         = EXCLUDED.machine_id,
    updated_at         = NOW()
"""


def _reason_to_str(reason: AuthProfileFailureReason | None) -> str | None:
    return reason.value if reason else None


def _str_to_reason(val: Any) -> AuthProfileFailureReason | None:
    if val is None:
        return None
    try:
        return AuthProfileFailureReason(val)
    except ValueError:
        return AuthProfileFailureReason.UNKNOWN


def _row_to_profile(row: Any) -> AuthProfile:
    stats = ProfileUsageStats(
        last_used_at=row.last_used_at,
        success_count=row.success_count,
        failure_count=row.failure_count,
        cooldown_until=row.cooldown_until,
        cooldown_reason=_str_to_reason(row.cooldown_reason),
        disabled_until=row.disabled_until,
        raw_error=row.raw_error,
    )
    return AuthProfile(
        id=row.id,
        provider=row.provider,
        account_identifier=row.account_identifier,
        backend=row.backend,
        backend_key=row.backend_key,
        last_synced_at=row.last_synced_at,
        sync_ttl_seconds=row.sync_ttl_seconds,
        usage_stats=stats,
    )


def _profile_params(
    profile: AuthProfile,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> dict[str, Any]:
    s = profile.usage_stats
    raw_error = s.raw_error
    if raw_error and len(raw_error) > RAW_ERROR_MAX_LEN:
        raw_error = raw_error[:RAW_ERROR_MAX_LEN]
    return {
        "tenant_id": tenant_id,
        "id": profile.id,
        "principal_id": principal_id,
        "provider": profile.provider,
        "account_identifier": profile.account_identifier,
        "backend": profile.backend,
        "backend_key": profile.backend_key,
        "last_synced_at": profile.last_synced_at,
        "sync_ttl_seconds": profile.sync_ttl_seconds,
        "last_used_at": s.last_used_at,
        "success_count": s.success_count,
        "failure_count": s.failure_count,
        "cooldown_until": s.cooldown_until,
        "cooldown_reason": _reason_to_str(s.cooldown_reason),
        "disabled_until": s.disabled_until,
        "raw_error": raw_error,
    }


# ---------------------------------------------------------------------------
# PostgresAuthProfileStore
# ---------------------------------------------------------------------------


class PostgresAuthProfileStore:
    """PostgreSQL-backed AuthProfileStore scoped to one (tenant, principal).

    Construction binds the store to a tenant + principal pair. Every
    transaction issues ``SET LOCAL app.current_tenant`` so RLS scopes reads
    to this tenant. Writes also carry ``tenant_id`` / ``principal_id``
    explicitly, providing defense-in-depth if RLS were misconfigured.

    Lifecycle:
        store = PostgresAuthProfileStore(db_url, tenant_id, principal_id)
        ...
        store.close()  # disposes the engine / returns pool connections

    Not safe to share across tenants. Create a new instance per
    (tenant, principal) context — engines are cheap once Postgres is warm.
    """

    def __init__(
        self,
        db_url: str,
        *,
        tenant_id: uuid.UUID | str,
        principal_id: uuid.UUID | str,
        engine: Engine | None = None,
        pool_size: int = 5,
        encryption_provider: EncryptionProvider | None = None,
        dek_cache: DEKCache | None = None,
    ) -> None:
        self._tenant_id = uuid.UUID(str(tenant_id))
        self._principal_id = uuid.UUID(str(principal_id))
        # Allow callers (tests, server with a shared engine) to inject one.
        if engine is None:
            self._engine = create_engine(
                db_url,
                pool_size=pool_size,
                pool_pre_ping=True,
                future=True,
            )
            self._owns_engine = True
        else:
            self._engine = engine
            self._owns_engine = False
        self._encryption_provider = encryption_provider
        self._aesgcm = AESGCMEnvelope()
        self._dek_cache = dek_cache or DEKCache()

    # ------------------------------------------------------------------
    # Internal: scoped-transaction helper
    # ------------------------------------------------------------------

    @contextmanager
    def _scoped(self) -> Iterator[Connection]:
        """Yield a connection with ``app.current_tenant`` bound for this tx.

        ``SET LOCAL`` scopes the setting to the enclosing transaction, so
        every public method wraps its work in this context to force RLS
        evaluation. No implicit commit — caller's exit handles it via
        ``engine.begin()``.
        """
        with self._engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :tid"),
                {"tid": str(self._tenant_id)},
            )
            yield conn

    # ------------------------------------------------------------------
    # AuthProfileStore protocol
    # ------------------------------------------------------------------

    def list(self, *, provider: str | None = None) -> list[AuthProfile]:
        with self._scoped() as conn:
            if provider is None:
                rows = conn.execute(
                    text(
                        "SELECT * FROM auth_profiles "
                        "WHERE tenant_id = :tid AND principal_id = :pid "
                        "ORDER BY id"
                    ),
                    {"tid": self._tenant_id, "pid": self._principal_id},
                ).fetchall()
            else:
                rows = conn.execute(
                    text(
                        "SELECT * FROM auth_profiles "
                        "WHERE tenant_id = :tid AND principal_id = :pid "
                        "AND provider = :p ORDER BY id"
                    ),
                    {
                        "tid": self._tenant_id,
                        "pid": self._principal_id,
                        "p": provider,
                    },
                ).fetchall()
        return [_row_to_profile(r) for r in rows]

    def get(self, profile_id: str) -> AuthProfile | None:
        with self._scoped() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM auth_profiles "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "tid": self._tenant_id,
                    "pid": self._principal_id,
                    "id": profile_id,
                },
            ).fetchone()
        return _row_to_profile(row) if row is not None else None

    def upsert(self, profile: AuthProfile) -> None:
        params = _profile_params(
            profile,
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
        )
        # Take the same ``(tenant_id, profile_id)`` advisory lock that
        # ``upsert_strict`` uses, so a plain ``upsert`` cannot slip a
        # foreign-principal row in between a strict call's check and
        # write. Locks do not block unrelated rows — only writers
        # targeting the same tenant+id tuple serialize.
        lock_key = f"{self._tenant_id}/{profile.id}"
        with self._scoped() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": lock_key},
            )
            self._reject_if_routing_change_on_encrypted(conn, profile)
            conn.execute(text(_UPSERT_SQL), params)

    def _reject_if_routing_change_on_encrypted(
        self, conn: Connection, profile: AuthProfile
    ) -> None:
        """Guard: plain upsert on an encrypted row is allowed ONLY when the
        routing columns (provider / account_identifier / backend / backend_key)
        are unchanged. Otherwise ``backend_key`` could diverge from the still-
        stored ciphertext and ``get_with_credential`` would return a credential
        that no longer matches the routing pointer.

        Stats-only updates from ``CredentialPool.mark_success/mark_failure``
        pass through cleanly because they re-emit the same routing values.
        """
        row = conn.execute(
            text(
                "SELECT provider, account_identifier, backend, backend_key "
                "FROM auth_profiles "
                "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id "
                "  AND ciphertext IS NOT NULL"
            ),
            {"tid": self._tenant_id, "pid": self._principal_id, "id": profile.id},
        ).fetchone()
        if row is None:
            return
        if (
            row.provider != profile.provider
            or row.account_identifier != profile.account_identifier
            or row.backend != profile.backend
            or row.backend_key != profile.backend_key
        ):
            raise ValueError(
                f"auth_profiles row ({self._tenant_id}, {self._principal_id}, "
                f"{profile.id!r}) has encrypted credentials and this plain "
                "upsert would change routing metadata; use "
                "upsert_with_credential() or delete() first."
            )

    def delete(self, profile_id: str) -> None:
        with self._scoped() as conn:
            conn.execute(
                text(
                    "DELETE FROM auth_profiles "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "tid": self._tenant_id,
                    "pid": self._principal_id,
                    "id": profile_id,
                },
            )

    def replace_owned_subset(
        self,
        *,
        upserts: "builtins.list[AuthProfile]",
        deletes: "builtins.list[str]",
    ) -> None:
        if not upserts and not deletes:
            return
        # Acquire advisory locks up front, in sorted order, to keep the
        # lock protocol consistent with ``upsert`` / ``upsert_strict`` and
        # prevent deadlocks between two callers whose upsert-lists overlap
        # on multiple ids. Sorting ensures both callers request the same
        # locks in the same order.
        lock_keys = sorted(
            {f"{self._tenant_id}/{p.id}" for p in upserts}
            | {f"{self._tenant_id}/{pid}" for pid in deletes}
        )
        with self._scoped() as conn:
            for key in lock_keys:
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": key},
                )
            if upserts:
                # Same invariant as plain upsert/upsert_strict: only reject
                # when routing metadata would actually change for an encrypted
                # row. Stats-only re-upserts (same provider/backend/backend_key)
                # remain legal so the existing sync and pool flows keep working
                # against rows already upgraded to carry encrypted credentials.
                by_id = {p.id: p for p in upserts}
                existing = conn.execute(
                    text(
                        "SELECT id, provider, account_identifier, backend, backend_key "
                        "FROM auth_profiles "
                        "WHERE tenant_id = :tid AND principal_id = :pid "
                        "  AND id = ANY(:ids) AND ciphertext IS NOT NULL"
                    ),
                    {
                        "tid": self._tenant_id,
                        "pid": self._principal_id,
                        "ids": list(by_id.keys()),
                    },
                ).fetchall()
                conflicts = [
                    r.id
                    for r in existing
                    if (
                        r.provider != by_id[r.id].provider
                        or r.account_identifier != by_id[r.id].account_identifier
                        or r.backend != by_id[r.id].backend
                        or r.backend_key != by_id[r.id].backend_key
                    )
                ]
                if conflicts:
                    raise ValueError(
                        f"replace_owned_subset would overwrite routing metadata "
                        f"on encrypted rows: {sorted(conflicts)!r}. "
                        "Use upsert_with_credential() or delete() for these rows first."
                    )
            for p in upserts:
                conn.execute(
                    text(_UPSERT_SQL),
                    _profile_params(
                        p,
                        tenant_id=self._tenant_id,
                        principal_id=self._principal_id,
                    ),
                )
            for pid in deletes:
                conn.execute(
                    text(
                        "DELETE FROM auth_profiles "
                        "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                    ),
                    {
                        "tid": self._tenant_id,
                        "pid": self._principal_id,
                        "id": pid,
                    },
                )

    def mark_success(self, profile_id: str) -> None:
        """Record a successful credential use.

        Unlike SqliteAuthProfileStore this does not buffer in memory — other
        daemons may be writing to the same row, so a per-process dirty bit
        would be stale. Direct ``UPDATE`` with ``last_used_at = NOW()`` also
        clears the cooldown window if it has already elapsed.

        Takes the same ``(tenant_id, profile_id)`` advisory lock as the
        other mutators so a concurrent ``upsert`` cannot clobber the
        success-counter increment with caller-supplied stats.
        """
        lock_key = f"{self._tenant_id}/{profile_id}"
        with self._scoped() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": lock_key},
            )
            conn.execute(
                text(
                    "UPDATE auth_profiles SET "
                    "    success_count = success_count + 1, "
                    "    last_used_at = NOW(), "
                    "    cooldown_until = CASE "
                    "        WHEN cooldown_until IS NULL OR cooldown_until <= NOW() "
                    "        THEN NULL ELSE cooldown_until END, "
                    "    cooldown_reason = CASE "
                    "        WHEN cooldown_until IS NULL OR cooldown_until <= NOW() "
                    "        THEN NULL ELSE cooldown_reason END "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "tid": self._tenant_id,
                    "pid": self._principal_id,
                    "id": profile_id,
                },
            )

    def mark_failure(
        self,
        profile_id: str,
        reason: AuthProfileFailureReason,
        *,
        raw_error: str | None = None,
    ) -> None:
        truncated = raw_error[:RAW_ERROR_MAX_LEN] if raw_error else None
        lock_key = f"{self._tenant_id}/{profile_id}"
        with self._scoped() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": lock_key},
            )
            conn.execute(
                text(
                    "UPDATE auth_profiles SET "
                    "    failure_count = failure_count + 1, "
                    "    last_used_at = NOW(), "
                    "    cooldown_reason = :reason, "
                    "    raw_error = COALESCE(:raw_error, raw_error) "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "tid": self._tenant_id,
                    "pid": self._principal_id,
                    "id": profile_id,
                    "reason": reason.value,
                    "raw_error": truncated,
                },
            )

    # ------------------------------------------------------------------
    # Envelope encryption (issue #3803)
    # ------------------------------------------------------------------

    def _require_provider(self) -> EncryptionProvider:
        if self._encryption_provider is None:
            raise RuntimeError(
                "encryption_provider is required for upsert_with_credential / "
                "get_with_credential — construct PostgresAuthProfileStore(..., "
                "encryption_provider=...)"
            )
        return self._encryption_provider

    def _aad_for(self, profile_id: str) -> bytes:
        return f"{self._tenant_id}|{self._principal_id}|{profile_id}".encode()

    @staticmethod
    def _serialize_credential(cred: ResolvedCredential) -> bytes:
        # Canonical JSON: sorted keys, compact separators. Deterministic for
        # rotation rewrap; any change here breaks existing ciphertext readability.
        payload = asdict(cred)
        expires_at: datetime | None = cred.expires_at
        if expires_at is not None:
            payload["expires_at"] = expires_at.isoformat()
        payload["scopes"] = list(cred.scopes)
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _deserialize_credential(data: bytes) -> ResolvedCredential:
        raw = json.loads(data.decode("utf-8"))
        expires_at = raw.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        return ResolvedCredential(
            kind=raw["kind"],
            api_key=raw.get("api_key"),
            access_token=raw.get("access_token"),
            expires_at=expires_at,
            scopes=tuple(raw.get("scopes", ())),
            metadata=raw.get("metadata", {}) or {},
        )

    def upsert_with_credential(
        self,
        profile: AuthProfile,
        credential: ResolvedCredential,
        *,
        source_file_hash: str | None = None,
        daemon_version: str | None = None,
        machine_id: uuid.UUID | None = None,
    ) -> None:
        provider = self._require_provider()
        aad = self._aad_for(profile.id)
        dek = secrets.token_bytes(32)
        nonce, ciphertext = self._aesgcm.encrypt(
            dek, self._serialize_credential(credential), aad=aad
        )
        wrapped_dek, kek_version = provider.wrap_dek(dek, tenant_id=self._tenant_id, aad=aad)
        params = _profile_params(
            profile, tenant_id=self._tenant_id, principal_id=self._principal_id
        )
        params.update(
            ciphertext=ciphertext,
            wrapped_dek=wrapped_dek,
            nonce=nonce,
            aad=aad,
            kek_version=kek_version,
            source_file_hash=source_file_hash,
            daemon_version=daemon_version,
            machine_id=machine_id,
        )
        lock_key = f"{self._tenant_id}/{profile.id}"
        with self._scoped() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": lock_key},
            )
            conn.execute(text(_UPSERT_WITH_CREDENTIAL_SQL), params)

    def get_with_credential(
        self, profile_id: str
    ) -> tuple[AuthProfile, ResolvedCredential | None] | None:
        provider = self._require_provider()
        with self._scoped() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM auth_profiles "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "tid": self._tenant_id,
                    "pid": self._principal_id,
                    "id": profile_id,
                },
            ).fetchone()
        if row is None:
            return None
        profile = _row_to_profile(row)
        if row.ciphertext is None:
            return profile, None
        expected_aad = self._aad_for(profile_id)
        if bytes(row.aad) != expected_aad:
            raise AADMismatch.from_row(
                tenant_id=self._tenant_id,
                profile_id=profile_id,
                kek_version=row.kek_version,
                cause="stored AAD does not match tenant|principal|profile_id",
            )
        cache_key = self._dek_cache.make_key(
            tenant_id=self._tenant_id,
            kek_version=row.kek_version,
            wrapped_dek=bytes(row.wrapped_dek),
        )
        tenant_label = str(self._tenant_id)
        dek = self._dek_cache.get(cache_key)
        if dek is None:
            DEK_CACHE_MISSES.labels(tenant_id=tenant_label).inc()
            try:
                with DEK_UNWRAP_LATENCY.labels(tenant_id=tenant_label).time():
                    dek = provider.unwrap_dek(
                        bytes(row.wrapped_dek),
                        tenant_id=self._tenant_id,
                        aad=expected_aad,
                        kek_version=row.kek_version,
                    )
            except Exception as exc:
                DEK_UNWRAP_ERRORS.labels(
                    tenant_id=tenant_label, error_class=type(exc).__name__
                ).inc()
                raise
            self._dek_cache.put(cache_key, dek)
        else:
            DEK_CACHE_HITS.labels(tenant_id=tenant_label).inc()
        plaintext = self._aesgcm.decrypt(
            dek, bytes(row.nonce), bytes(row.ciphertext), aad=expected_aad
        )
        return profile, self._deserialize_credential(plaintext)

    # ------------------------------------------------------------------
    # Tenant-wide helpers (migration/admin only — outside normal Protocol)
    # ------------------------------------------------------------------

    def upsert_strict(self, profile: AuthProfile) -> None:
        """Atomic write: upsert ``profile`` iff no other principal in this
        tenant owns the same ``profile.id``.

        Intended for migrations + admin writes where silent divergence under
        concurrency would be surprising. The lock guarantees that no other
        writer targeting the same ``(tenant_id, profile_id)`` can observe or
        produce a cross-principal INSERT until this transaction commits.

        The composite PK ``(tenant_id, principal_id, id)`` already prevents
        ownership *takeover*; this method additionally prevents silent
        ownership *divergence* (two principals each holding their own row
        for the same business id inside one tenant), which operators almost
        never want.

        Raises:
            CrossPrincipalConflict: when a foreign owner is present; the
                profile is NOT written.
        """
        lock_key = f"{self._tenant_id}/{profile.id}"
        with self._scoped() as conn:
            # Serialize every writer targeting this (tenant, profile_id)
            # tuple for the duration of the transaction, closing the window
            # between "check" and "upsert" that the migration apply path
            # would otherwise have.
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": lock_key},
            )
            foreign = conn.execute(
                text(
                    "SELECT principal_id FROM auth_profiles "
                    "WHERE tenant_id = :tid AND id = :id "
                    "  AND principal_id != :pid"
                ),
                {
                    "tid": self._tenant_id,
                    "pid": self._principal_id,
                    "id": profile.id,
                },
            ).fetchall()
            if foreign:
                raise CrossPrincipalConflict(
                    profile_id=profile.id,
                    foreign_principals=sorted(row[0] for row in foreign),
                )
            self._reject_if_routing_change_on_encrypted(conn, profile)
            conn.execute(
                text(_UPSERT_SQL),
                _profile_params(
                    profile,
                    tenant_id=self._tenant_id,
                    principal_id=self._principal_id,
                ),
            )

    def tenant_scope_owners_of(self, profile_id: str) -> set[uuid.UUID]:
        """Return every ``principal_id`` that owns ``profile_id`` in this
        store's tenant. Empty set if the id does not exist.

        The composite PK ``(tenant_id, principal_id, id)`` allows the same
        business id (e.g. ``"google/alice"``) to exist under multiple
        principals in the same tenant — ``fetchone()`` would be
        non-deterministic. This helper returns the full set so callers can
        make the two distinct decisions (is it owned by *this* principal?
        is it owned by *some other* principal?) deterministically.

        Bypasses the principal filter used by the Protocol methods. Intended
        exclusively for the migration CLI + admin scripts that need to detect
        cross-principal collisions before calling ``upsert`` (which is now
        principal-scoped via the composite PK and will *not* silently take
        over another principal's row).

        Still honors tenant scoping — returns an empty set for profile_ids
        owned by other tenants, regardless of RLS configuration.
        """
        with self._scoped() as conn:
            rows = conn.execute(
                text("SELECT principal_id FROM auth_profiles WHERE tenant_id = :tid AND id = :id"),
                {"tid": self._tenant_id, "id": profile_id},
            ).fetchall()
        return {row[0] for row in rows}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Dispose the engine (releases pool connections)."""
        if self._owns_engine:
            self._engine.dispose()

    # ------------------------------------------------------------------
    # Introspection (used by tests + migration tool)
    # ------------------------------------------------------------------

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    @property
    def principal_id(self) -> uuid.UUID:
        return self._principal_id

    @property
    def engine(self) -> Engine:
        """Underlying engine — exposed only for test helpers + migration."""
        return self._engine


# ---------------------------------------------------------------------------
# KEK rotation (issue #3803) — module-level admin helper
# ---------------------------------------------------------------------------


class VersionSkewError(Exception):
    """Raised by ``rotate_kek_for_tenant`` when rows already exist at a
    ``kek_version`` greater than the provider's current version.

    Indicates the provider is pointing at an older config than data already
    written — typically a misconfigured ``--kms-config-version`` or a rolled-
    back provider. Refusing to rotate prevents a silent false-success report.
    """

    def __init__(self, *, rows_ahead: int, target_version: int):
        self.rows_ahead = rows_ahead
        self.target_version = target_version
        super().__init__(f"{rows_ahead} rows have kek_version > target ({target_version})")


@dataclass(frozen=True, slots=True)
class RotationReport:
    """Result of a ``rotate_kek_for_tenant`` invocation."""

    rows_rewrapped: int
    rows_failed: int
    rows_remaining: int
    target_version: int


def rotate_kek_for_tenant(
    engine: Engine,
    *,
    tenant_id: uuid.UUID,
    encryption_provider: EncryptionProvider,
    batch_size: int = 100,
    max_rows: int | None = None,
    allow_skew: bool = False,
) -> RotationReport:
    """Rewrap every row in ``tenant_id`` whose ``kek_version`` is older than
    the provider's current version.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so the helper is resumable and
    does not block concurrent writers. Rewraps ``wrapped_dek`` + ``kek_version``
    only; ``ciphertext``, ``nonce``, ``aad`` are untouched so a reader mid-
    rotation decrypts successfully regardless of which version wrote.

    Raises ``VersionSkewError`` by default if any row has
    ``kek_version > target``. Callers who are intentionally rolling back can
    pass ``allow_skew=True`` to suppress the check, but the helper then only
    processes rows with ``kek_version < target`` — ahead-of-target rows remain
    untouched.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if max_rows is not None and max_rows < 1:
        raise ValueError(f"max_rows must be >= 1 when set, got {max_rows}")
    target = encryption_provider.current_version(tenant_id=tenant_id)
    if not allow_skew:
        with engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :tid"),
                {"tid": str(tenant_id)},
            )
            ahead = conn.execute(
                text(
                    "SELECT COUNT(*) FROM auth_profiles "
                    "WHERE tenant_id = :tid AND ciphertext IS NOT NULL "
                    "  AND kek_version > :target"
                ),
                {"tid": tenant_id, "target": target},
            ).scalar_one()
        if ahead:
            raise VersionSkewError(rows_ahead=int(ahead), target_version=target)
    rewrapped = 0
    failed = 0
    tenant_label = str(tenant_id)
    # Track (principal_id, id) pairs that failed this run so they are not
    # re-selected on subsequent batch iterations, preventing an infinite retry
    # loop. The table PK is (tenant_id, principal_id, id) so the same profile
    # id can exist under multiple principals — keying failures on ``id`` alone
    # would starve healthy rows that share an id with a failing one.
    _failed_keys: list[tuple[uuid.UUID, str]] = []
    # Bound CAS-miss churn: if the same (principal_id, id) repeatedly loses
    # the compare-and-swap race (because a concurrent writer keeps bumping the
    # row), promote it to _failed_keys after this many attempts so the loop
    # terminates instead of livelocking on provider calls.
    _cas_misses: dict[tuple[uuid.UUID, str], int] = {}
    _MAX_CAS_MISSES = 3
    while True:
        # max_rows caps SUCCESSFUL rewraps only. Counting failures toward the
        # budget would let a handful of deterministically failing rows starve
        # healthy rows out of a controlled batch.
        if max_rows is not None and rewrapped >= max_rows:
            break
        this_batch = batch_size
        if max_rows is not None:
            this_batch = min(this_batch, max_rows - rewrapped)

        # 1) Snapshot a batch of candidates in a short read-only tx. No
        # FOR UPDATE: holding row locks across Vault/KMS calls would block
        # concurrent upsert_with_credential writers for the full provider
        # RTT. We trade those locks for an optimistic CAS at UPDATE time.
        with engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :tid"),
                {"tid": str(tenant_id)},
            )
            # Exclude rows that already failed so the loop terminates.
            # Postgres has no native tuple-ANY operator across mixed types,
            # so we unzip into two parallel arrays and do a row-subscript test.
            if _failed_keys:
                skip_principals = [p for p, _ in _failed_keys]
                skip_ids = [i for _, i in _failed_keys]
                exclude_clause = (
                    " AND NOT EXISTS (SELECT 1 FROM unnest("
                    "CAST(:skip_principals AS UUID[]), :skip_ids) "
                    "AS sk(p, i) WHERE sk.p = principal_id AND sk.i = id)"
                )
                params: dict[str, builtins.object] = {
                    "tid": tenant_id,
                    "target": target,
                    "lim": this_batch,
                    "skip_principals": skip_principals,
                    "skip_ids": skip_ids,
                }
            else:
                exclude_clause = ""
                params = {"tid": tenant_id, "target": target, "lim": this_batch}
            rows = conn.execute(
                text(
                    "SELECT tenant_id, principal_id, id, wrapped_dek, aad, kek_version "
                    "FROM auth_profiles "
                    "WHERE tenant_id = :tid "
                    "  AND ciphertext IS NOT NULL "
                    "  AND kek_version < :target" + exclude_clause + " ORDER BY principal_id, id "
                    "LIMIT :lim"
                ),
                params,
            ).fetchall()
        if not rows:
            break

        # 2) Provider calls happen OUTSIDE any open transaction. Validate AAD
        # first; unwrap failures are per-row, wrap failures are fatal. Results
        # get staged for a single CAS batch below.
        staged: list[tuple[Any, bytes, int]] = []  # (row, new_wrapped, new_version)
        for row in rows:
            expected_aad = f"{tenant_id}|{row.principal_id}|{row.id}".encode()
            if bytes(row.aad) != expected_aad:
                logger.error(
                    "rotate_kek_for_tenant: AAD mismatch "
                    "tenant=%s principal=%s profile=%s kek_version=%s",
                    tenant_id,
                    row.principal_id,
                    row.id,
                    row.kek_version,
                )
                _failed_keys.append((uuid.UUID(str(row.principal_id)), row.id))
                failed += 1
                continue
            try:
                dek = encryption_provider.unwrap_dek(
                    bytes(row.wrapped_dek),
                    tenant_id=tenant_id,
                    aad=bytes(row.aad),
                    kek_version=row.kek_version,
                )
            except EnvelopeError as exc:
                logger.error(
                    "rotate_kek_for_tenant: per-row unwrap failure "
                    "tenant=%s principal=%s profile=%s kek_version=%s cause=%s",
                    tenant_id,
                    row.principal_id,
                    row.id,
                    row.kek_version,
                    type(exc).__name__,
                )
                _failed_keys.append((uuid.UUID(str(row.principal_id)), row.id))
                failed += 1
                continue
            # Wrap at target version is FATAL to the batch rather than per-row.
            # A wrap error at the new version means the target KEK is unusable
            # (wrong key, IAM issue, etc.); continuing would leave a partially-
            # rotated tenant with rows split across versions.
            try:
                new_wrapped, new_version = encryption_provider.wrap_dek(
                    dek, tenant_id=tenant_id, aad=bytes(row.aad)
                )
            except EnvelopeError as exc:
                logger.error(
                    "rotate_kek_for_tenant: wrap-at-target failed; aborting "
                    "batch tenant=%s target=%s cause=%s",
                    tenant_id,
                    target,
                    type(exc).__name__,
                )
                raise
            staged.append((row, new_wrapped, new_version))

        # 3) Apply all CAS updates in one short tx. Each UPDATE includes the
        # original (wrapped_dek, kek_version) as a predicate, so a concurrent
        # upsert_with_credential that raced ahead of us simply sees 0 rows
        # affected and we skip that row without counting it as rewrapped.
        if staged:
            with engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.current_tenant = :tid"),
                    {"tid": str(tenant_id)},
                )
                for row, new_wrapped, new_version in staged:
                    result = conn.execute(
                        text(
                            "UPDATE auth_profiles SET "
                            "    wrapped_dek = :new_wd, "
                            "    kek_version = :new_v, "
                            "    updated_at = NOW() "
                            "WHERE tenant_id = :tid "
                            "  AND principal_id = :pid "
                            "  AND id = :id "
                            "  AND wrapped_dek = :old_wd "
                            "  AND kek_version = :old_v"
                        ),
                        {
                            "new_wd": new_wrapped,
                            "new_v": new_version,
                            "tid": tenant_id,
                            "pid": row.principal_id,
                            "id": row.id,
                            "old_wd": bytes(row.wrapped_dek),
                            "old_v": row.kek_version,
                        },
                    )
                    if result.rowcount == 1:
                        KEK_ROTATE_ROWS.labels(
                            tenant_id=tenant_label,
                            from_version=str(row.kek_version),
                            to_version=str(new_version),
                        ).inc()
                        rewrapped += 1
                        _cas_misses.pop((uuid.UUID(str(row.principal_id)), row.id), None)
                    else:
                        # Concurrent writer won. Allow a few retries — on
                        # repeated misses treat as failed so we don't livelock
                        # on a perpetually-contended row.
                        key = (uuid.UUID(str(row.principal_id)), row.id)
                        _cas_misses[key] = _cas_misses.get(key, 0) + 1
                        if _cas_misses[key] >= _MAX_CAS_MISSES:
                            logger.error(
                                "rotate_kek_for_tenant: CAS-miss threshold "
                                "exceeded tenant=%s principal=%s profile=%s "
                                "misses=%d",
                                tenant_id,
                                row.principal_id,
                                row.id,
                                _cas_misses[key],
                            )
                            _failed_keys.append(key)
                            failed += 1
    # Final remaining count
    with engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.current_tenant = :tid"),
            {"tid": str(tenant_id)},
        )
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM auth_profiles "
                "WHERE tenant_id = :tid AND ciphertext IS NOT NULL "
                "  AND kek_version < :target"
            ),
            {"tid": tenant_id, "target": target},
        ).scalar_one()
    return RotationReport(
        rows_rewrapped=rewrapped,
        rows_failed=failed,
        rows_remaining=int(remaining),
        target_version=target,
    )
