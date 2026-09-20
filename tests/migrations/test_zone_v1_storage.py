"""2B NEXUS-STORAGE tests: fresh/upgrade equivalence, constraints, fencing,
provenance isolation, no-cascade-delete, and the legacy mapper's §10.3 rules."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from alembic import command
from nexus.storage.models._base import Base
from nexus.storage.zone_migration import (
    can_import_api_key_zone,
    decompose_legacy_settings,
    map_legacy_domain,
    map_legacy_zone,
)

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
ZONE_V1_TABLES = {
    "zone_grants",
    "zone_operations",
    "zone_mounts",
    "zone_runtime_outbox",
    "zone_grant_projection_outbox",
    "zone_authorization_epochs",
    "zone_delegations",
    "rebac_relation_sources",
}


@pytest.fixture()
def upgraded_sqlite():
    """A SQLite database upgraded through the full alembic chain to head.

    foreign_keys=ON per connection: SQLite skips FK enforcement by default,
    and the RESTRICT-on-delete guarantee is exactly what these tests assert.
    """
    engine = sa.create_engine("sqlite://", future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    cfg = Config(str(REPO_ROOT / "alembic" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    # Run offline of env.py's URL plumbing: point the config straight at the engine.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    return engine


@pytest.fixture()
def fresh_sqlite():
    """A SQLite database built straight from the ORM metadata."""
    engine = sa.create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return engine


def _table_names(engine) -> set[str]:
    insp = sa.inspect(engine)
    return set(insp.get_table_names())


def _columns(engine, table: str) -> set[str]:
    insp = sa.inspect(engine)
    return {c["name"] for c in insp.get_columns(table)}


def test_upgrade_and_fresh_agree_on_zone_v1_tables(upgraded_sqlite, fresh_sqlite):
    for engine in (upgraded_sqlite, fresh_sqlite):
        assert _table_names(engine) >= ZONE_V1_TABLES
    assert _columns(upgraded_sqlite, "zone_grants") == _columns(fresh_sqlite, "zone_grants")
    assert _columns(upgraded_sqlite, "zone_operations") == _columns(fresh_sqlite, "zone_operations")
    assert _columns(upgraded_sqlite, "rebac_relation_sources") == _columns(
        fresh_sqlite, "rebac_relation_sources"
    )


@pytest.mark.postgres
def test_postgresql_upgrade_and_fresh_agree_on_zone_v1_tables():
    """Run the real Alembic chain and ORM metadata in isolated PG schemas."""
    database_url = os.environ.get("NEXUS_E2E_DATABASE_URL")
    if not database_url:
        pytest.skip("NEXUS_E2E_DATABASE_URL is not configured")

    suffix = uuid.uuid4().hex[:12]
    upgraded_schema = f"zone_v1_upgrade_{suffix}"
    fresh_schema = f"zone_v1_fresh_{suffix}"
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)

    def scoped_engine(schema: str):
        return sa.create_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema}"},
            poolclass=NullPool,
            future=True,
        )

    with admin.begin() as conn:
        conn.execute(sa.schema.CreateSchema(upgraded_schema))
        conn.execute(sa.schema.CreateSchema(fresh_schema))
    upgraded = scoped_engine(upgraded_schema)
    fresh = scoped_engine(fresh_schema)
    try:
        cfg = Config(str(REPO_ROOT / "alembic" / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        with upgraded.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        Base.metadata.create_all(fresh)

        assert _table_names(upgraded) >= ZONE_V1_TABLES
        assert _table_names(fresh) >= ZONE_V1_TABLES
        for table in ZONE_V1_TABLES:
            assert _columns(upgraded, table) == _columns(fresh, table)
    finally:
        upgraded.dispose()
        fresh.dispose()
        with admin.begin() as conn:
            conn.execute(sa.schema.DropSchema(upgraded_schema, cascade=True))
            conn.execute(sa.schema.DropSchema(fresh_schema, cascade=True))
        admin.dispose()


def test_zones_canonical_columns_are_expand_only(upgraded_sqlite):
    cols = _columns(upgraded_sqlite, "zones")
    canonical = {
        "display_name",
        "canonical_status",
        "canonical_revision",
        "created_by",
        "placement_location",
        "placement_data_domain",
        "trust_domain",
        "placement_region",
        "replication_policy_ref",
        "labels",
        "runtime_observed_receipt",
        "runtime_health",
        "runtime_observed_at",
    }
    assert canonical <= cols
    # Legacy columns all survive (expand-only).
    assert {"name", "phase", "settings", "domain"} <= cols


def _seed_zone(conn, zone_id="z-alpha-1"):
    conn.execute(
        sa.text(
            "INSERT INTO zones (zone_id, name, phase, finalizers, indexing_mode, created_at, updated_at)"
            " VALUES (:z, :n, 'Active', '[]', 'all', :t, :t)"
        ),
        {"z": zone_id, "n": "Alpha", "t": datetime.now(UTC)},
    )


def test_grant_source_uniqueness_enforced(upgraded_sqlite):
    with upgraded_sqlite.begin() as conn:
        _seed_zone(conn)
        conn.execute(
            sa.text(
                "INSERT INTO zone_grants (grant_id, zone_id, grantee, capabilities, source_type, source_id,"
                " issued_by, reason, policy_version, revision, status, created_at)"
                " VALUES ('g1', 'z-alpha-1', '{}', '[]', 'manual', 's-1', '{}', 'r', 'p', 'v1', 'active', :t)"
            ),
            {"t": datetime.now(UTC)},
        )
    with pytest.raises(IntegrityError), upgraded_sqlite.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO zone_grants (grant_id, zone_id, grantee, capabilities, source_type, source_id,"
                " issued_by, reason, policy_version, revision, status, created_at)"
                " VALUES ('g2', 'z-alpha-1', '{}', '[]', 'manual', 's-1', '{}', 'r', 'p', 'v1', 'active', :t)"
            ),
            {"t": datetime.now(UTC)},
        )


def test_operation_idempotency_key_conflicts_rejected(upgraded_sqlite):
    now = datetime.now(UTC)
    with upgraded_sqlite.begin() as conn:
        _seed_zone(conn)
        conn.execute(
            sa.text(
                "INSERT INTO zone_operations (operation_id, action, zone_id, state, step, retryable,"
                " idempotency_scope, idempotency_key, request_hash, generation, fence, created_at, updated_at)"
                " VALUES ('op1', 'create', 'z-alpha-1', 'queued', 'validate', 0, 'scope-1', 'key-1', 'hash-a', 0, 0, :t, :t)"
            ),
            {"t": now},
        )
    with pytest.raises(IntegrityError), upgraded_sqlite.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO zone_operations (operation_id, action, zone_id, state, step, retryable,"
                " idempotency_scope, idempotency_key, request_hash, generation, fence, created_at, updated_at)"
                " VALUES ('op2', 'create', 'z-alpha-1', 'queued', 'validate', 0, 'scope-1', 'key-1', 'hash-B', 0, 0, :t, :t)"
            ),
            {"t": now},
        )


def test_overlapping_grant_edges_survive_one_revoke(upgraded_sqlite):
    """Two grants derive the same relation; revoking one must not delete the
    other's edge — provenance per grant, not per tuple."""
    now = datetime.now(UTC)
    with upgraded_sqlite.begin() as conn:
        _seed_zone(conn)
        for gid in ("g1", "g2"):
            conn.execute(
                sa.text(
                    "INSERT INTO zone_grants (grant_id, zone_id, grantee, capabilities, source_type, source_id,"
                    " issued_by, reason, policy_version, revision, status, created_at)"
                    " VALUES (:g, 'z-alpha-1', '{}', '[]', 'manual', :s, '{}', 'r', 'p', 'v1', 'revoked', :t)"
                ),
                {"g": gid, "s": f"src-{gid}", "t": now},
            )
        for gid in ("g1", "g2"):
            conn.execute(
                sa.text(
                    "INSERT INTO rebac_relation_sources (subject, relation, object, source_grant_id, reference_state, created_at, updated_at)"
                    " VALUES ('user:u1', 'zone.data.read', 'zone:z-alpha-1', :g, 'active', :t, :t)"
                ),
                {"g": gid, "t": now},
            )
        # independent authoritative relation on the same tuple
        conn.execute(
            sa.text(
                "INSERT INTO rebac_relation_sources (subject, relation, object, source_grant_id, reference_state, created_at, updated_at)"
                " VALUES ('user:u1', 'zone.data.read', 'zone:z-alpha-1', NULL, 'active', :t, :t)"
            ),
            {"t": now},
        )

    # Revoke of g1 removes only g1's derived edge.
    with upgraded_sqlite.begin() as conn:
        deleted = conn.execute(
            sa.text("DELETE FROM rebac_relation_sources WHERE source_grant_id = 'g1'")
        ).rowcount
        assert deleted == 1
    with upgraded_sqlite.begin() as conn:
        remaining = conn.execute(
            sa.text("SELECT source_grant_id FROM rebac_relation_sources")
        ).fetchall()
    assert {r[0] for r in remaining} == {"g2", None}


def test_stale_worker_fence_rejects_late_write(upgraded_sqlite):
    """A worker whose lease expired writes back through the fence: the UPDATE
    matches zero rows because the fence moved on."""
    now = datetime.now(UTC)
    with upgraded_sqlite.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO zone_operations (operation_id, action, state, step, retryable,"
                " idempotency_scope, idempotency_key, request_hash, lease_owner, lease_expires_at,"
                " generation, fence, created_at, updated_at)"
                " VALUES ('op1', 'create', 'running', 'provision', 1, 's', 'k', 'h', 'worker-A', :past,"
                " 1, 7, :t, :t)"
            ),
            {"past": now - timedelta(minutes=5), "t": now},
        )
        # A new worker takes over: bumps generation and fence.
        conn.execute(
            sa.text(
                "UPDATE zone_operations SET lease_owner='worker-B', generation=2, fence=8, updated_at=:t"
                " WHERE operation_id='op1'"
            ),
            {"t": now},
        )

    # worker-A's late write targets the fence it remembers (7): zero rows.
    with upgraded_sqlite.begin() as conn:
        rows = conn.execute(
            sa.text(
                "UPDATE zone_operations SET state='failed', step='stale-write-attempt', updated_at=:t"
                " WHERE operation_id='op1' AND lease_owner='worker-A' AND fence=7"
            ),
            {"t": now},
        ).rowcount
        assert rows == 0
        state = conn.execute(
            sa.text("SELECT state FROM zone_operations WHERE operation_id='op1'")
        ).scalar()
    assert state == "running"


def test_zone_delete_restricted_while_grants_reference_it(upgraded_sqlite):
    now = datetime.now(UTC)
    with upgraded_sqlite.begin() as conn:
        _seed_zone(conn)
        conn.execute(
            sa.text(
                "INSERT INTO zone_grants (grant_id, zone_id, grantee, capabilities, source_type, source_id,"
                " issued_by, reason, policy_version, revision, status, created_at)"
                " VALUES ('g1', 'z-alpha-1', '{}', '[]', 'manual', 's', '{}', 'r', 'p', 'v1', 'active', :t)"
            ),
            {"t": now},
        )
    with pytest.raises(IntegrityError), upgraded_sqlite.begin() as conn:
        conn.execute(sa.text("DELETE FROM zones WHERE zone_id='z-alpha-1'"))


# ── legacy mapper (§10.3) ─────────────────────────────────────────────────────


def test_active_maps_only_with_runtime_readback():
    ok = map_legacy_zone(
        zone_id="z",
        phase="Active",
        deleted_at=None,
        runtime_identity_verified=True,
        deletion_tombstone_verifiable=False,
        replica_receipts_complete=False,
    )
    assert ok.can_apply and ok.canonical_status == "active"
    degraded = map_legacy_zone(
        zone_id="z",
        phase="Active",
        deleted_at=None,
        runtime_identity_verified=False,
        deletion_tombstone_verifiable=False,
        replica_receipts_complete=False,
    )
    assert degraded.outcome == "degraded" and not degraded.can_apply


def test_terminating_and_terminated_are_blockers_without_evidence():
    terminating = map_legacy_zone(
        zone_id="z",
        phase="Terminating",
        deleted_at=None,
        runtime_identity_verified=False,
        deletion_tombstone_verifiable=False,
        replica_receipts_complete=False,
    )
    assert terminating.outcome == "blocker"
    terminated = map_legacy_zone(
        zone_id="z",
        phase="Terminated",
        deleted_at=datetime.now(UTC),
        runtime_identity_verified=False,
        deletion_tombstone_verifiable=False,
        replica_receipts_complete=False,
    )
    assert terminated.outcome == "blocker"
    ok_deleted = map_legacy_zone(
        zone_id="z",
        phase="Terminated",
        deleted_at=datetime.now(UTC),
        runtime_identity_verified=False,
        deletion_tombstone_verifiable=True,
        replica_receipts_complete=True,
    )
    assert ok_deleted.can_apply and ok_deleted.canonical_status == "deleted"


def test_domain_never_auto_maps():
    value, blockers = map_legacy_domain("office")
    assert value is None and blockers


def test_settings_decompose_keeps_unknowns():
    out = decompose_legacy_settings('{"location":"edge","region":"eu","quota":7}')
    assert out["placement_location"] == "edge"
    assert out["placement_region"] == "eu"
    assert out["unmapped"] == {"quota": 7}
    bad = decompose_legacy_settings("not json")
    assert bad["placement_location"] is None and "__invalid_json__" in bad["unmapped"]


def test_api_key_zone_import_needs_all_four_attributions():
    ok = can_import_api_key_zone(
        key_id="k",
        zone_id="z",
        subject_determinable=True,
        issuer_determinable=True,
        permissions_determinable=True,
        source_determinable=True,
    )
    assert ok.importable
    partial = can_import_api_key_zone(
        key_id="k",
        zone_id="z",
        subject_determinable=True,
        issuer_determinable=False,
        permissions_determinable=True,
        source_determinable=False,
    )
    assert not partial.importable and len(partial.blockers) == 2
