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

Crypto columns (ciphertext, wrapped_dek, nonce, kek_version, aad) are
deliberately NOT added in this PR — they land in #3788 Phase C. This store
persists routing metadata only, matching the current AuthProfile contract.

Feature gate: instantiated only when ``NEXUS_AUTH_STORE=postgres``. Default
remains SqliteAuthProfileStore until downstream consumers migrate.
"""

from __future__ import annotations

import builtins
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from nexus.bricks.auth.profile import (
    RAW_ERROR_MAX_LEN,
    AuthProfile,
    AuthProfileFailureReason,
    ProfileUsageStats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema (idempotent CREATE TABLE IF NOT EXISTS + RLS policies)
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS: tuple[str, ...] = (
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
    """
    CREATE TABLE IF NOT EXISTS principal_aliases (
        auth_method  TEXT NOT NULL,
        external_sub TEXT NOT NULL,
        principal_id UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (auth_method, external_sub)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_principal_aliases_principal_id ON principal_aliases(principal_id)",
    """
    CREATE TABLE IF NOT EXISTS auth_profiles (
        tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        id                 TEXT NOT NULL,
        principal_id       UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
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
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (tenant_id, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auth_profiles_principal ON auth_profiles(tenant_id, principal_id)",
    "CREATE INDEX IF NOT EXISTS idx_auth_profiles_provider ON auth_profiles(tenant_id, provider)",
    "ALTER TABLE tenants ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tenants FORCE ROW LEVEL SECURITY",
    "ALTER TABLE principals ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE principals FORCE ROW LEVEL SECURITY",
    "ALTER TABLE principal_aliases ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE principal_aliases FORCE ROW LEVEL SECURITY",
    "ALTER TABLE auth_profiles ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE auth_profiles FORCE ROW LEVEL SECURITY",
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
        USING (principal_id IN (
            SELECT id FROM principals
            WHERE tenant_id = current_setting('app.current_tenant', true)::UUID
        ))
    """,
    "DROP POLICY IF EXISTS tenant_isolation_auth_profiles ON auth_profiles",
    """
    CREATE POLICY tenant_isolation_auth_profiles ON auth_profiles
        USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    """,
)


def ensure_schema(engine: Engine) -> None:
    """Create (idempotently) every table, index, and RLS policy.

    Intended for:
      - Test fixtures (fresh schema per module)
      - Dev bootstrap
      - First-run on an empty production database

    Production rollouts should eventually migrate to Alembic — intentionally
    out of scope for PR 1.
    """
    with engine.begin() as conn:
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(text(stmt))
        for stmt in _POLICY_STATEMENTS:
            conn.execute(text(stmt))


def drop_schema(engine: Engine) -> None:
    """Drop every table created by ``ensure_schema`` (test teardown helper)."""
    with engine.begin() as conn:
        for tbl in ("auth_profiles", "principal_aliases", "principals", "tenants"):
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
            return row[0]
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
    """
    with engine.begin() as conn:
        if external_sub is not None:
            alias = conn.execute(
                text(
                    "SELECT principal_id FROM principal_aliases "
                    "WHERE auth_method = :m AND external_sub = :s"
                ),
                {"m": auth_method, "s": external_sub},
            ).fetchone()
            if alias is not None:
                return alias[0]

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
                    "INSERT INTO principal_aliases (auth_method, external_sub, principal_id) "
                    "VALUES (:m, :s, :p)"
                ),
                {"m": auth_method, "s": external_sub, "p": principal_id},
            )
        return principal_id


# ---------------------------------------------------------------------------
# SQL statements used by the store (tenant/principal scoping baked in)
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO auth_profiles (
    tenant_id, id, principal_id,
    provider, account_identifier, backend, backend_key,
    last_synced_at, sync_ttl_seconds,
    last_used_at, success_count, failure_count,
    cooldown_until, cooldown_reason, disabled_until, raw_error,
    updated_at
) VALUES (
    :tenant_id, :id, :principal_id,
    :provider, :account_identifier, :backend, :backend_key,
    :last_synced_at, :sync_ttl_seconds,
    :last_used_at, :success_count, :failure_count,
    :cooldown_until, :cooldown_reason, :disabled_until, :raw_error,
    NOW()
)
ON CONFLICT (tenant_id, id) DO UPDATE SET
    principal_id       = EXCLUDED.principal_id,
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
        with self._scoped() as conn:
            conn.execute(text(_UPSERT_SQL), params)

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
        with self._scoped() as conn:
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
        """
        with self._scoped() as conn:
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
        with self._scoped() as conn:
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
