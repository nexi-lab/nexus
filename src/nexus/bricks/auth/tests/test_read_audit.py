"""Tests for ReadAuditWriter — 100% on cache-miss, 1% sample on cache-hit (#3818)."""

from __future__ import annotations

import os
import random
import uuid

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.auth.postgres_profile_store import ensure_schema
from nexus.bricks.auth.read_audit import ReadAuditWriter


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _seed_tenant_principal_and_machine(engine, tenant_id, principal_id, machine_id):
    """Seed tenant + principal + an unrevoked daemon_machines row.

    The machine row is required because ReadAuditWriter.write now opens
    every transaction with a SELECT ... FOR SHARE on daemon_machines
    (atomic revocation gate, F26). Without an active row the writer
    raises MachineUnknownOrRevoked before any audit insert.
    """
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id), "n": f"rt-{tenant_id}"},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal_id), "t": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO daemon_machines (id, tenant_id, principal_id, pubkey) "
                "VALUES (:m, :t, :p, :pk) ON CONFLICT DO NOTHING"
            ),
            {
                "m": str(machine_id),
                "t": str(tenant_id),
                "p": str(principal_id),
                "pk": b"rt-pubkey-" + machine_id.bytes,
            },
        )


def _count_reads(engine, tenant_id):
    # RLS is bypassed for superusers, so scope the count explicitly by
    # tenant_id to avoid cross-test contamination in a shared test DB.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        return conn.execute(
            text("SELECT COUNT(*) FROM auth_profile_reads WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar()


def test_writes_100_percent_on_cache_miss(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    _seed_tenant_principal_and_machine(engine, tenant, principal, machine)
    writer = ReadAuditWriter(engine=engine, hit_sample_rate=0.01)

    for _ in range(20):
        writer.write(
            tenant_id=tenant,
            principal_id=principal,
            auth_profile_id="github-default",
            caller_machine_id=machine,
            caller_kind="daemon",
            provider="github",
            purpose="test",
            cache_hit=False,
            kek_version=1,
        )

    assert _count_reads(engine, tenant) == 20


def test_samples_one_percent_on_cache_hit(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    _seed_tenant_principal_and_machine(engine, tenant, principal, machine)
    rng = random.Random(42)
    expected = sum(1 for _ in range(100) if rng.random() < 0.01)

    writer = ReadAuditWriter(engine=engine, hit_sample_rate=0.01, rng=random.Random(42))
    for _ in range(100):
        writer.write(
            tenant_id=tenant,
            principal_id=principal,
            auth_profile_id="github-default",
            caller_machine_id=machine,
            caller_kind="daemon",
            provider="github",
            purpose="test",
            cache_hit=True,
            kek_version=1,
        )

    assert _count_reads(engine, tenant) == expected


def test_truncates_purpose_to_256_chars(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    _seed_tenant_principal_and_machine(engine, tenant, principal, machine)
    writer = ReadAuditWriter(engine=engine, hit_sample_rate=0.01)

    long_purpose = "x" * 1000
    writer.write(
        tenant_id=tenant,
        principal_id=principal,
        auth_profile_id="github-default",
        caller_machine_id=machine,
        caller_kind="daemon",
        provider="github",
        purpose=long_purpose,
        cache_hit=False,
        kek_version=1,
    )

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        row = conn.execute(
            text("SELECT purpose FROM auth_profile_reads WHERE tenant_id = :t"),
            {"t": str(tenant)},
        ).fetchone()
    assert len(row[0]) == 256


def test_cache_miss_insert_failure_raises_audit_write_failed():
    """Cache-miss audit MUST fail closed — drop a credential without a record
    of who read it would create a forensics blind spot. The router maps the
    raised exception to 503 so the caller never sees the credential."""
    from unittest.mock import MagicMock

    from nexus.bricks.auth.consumer import AuditWriteFailed

    bad_engine = MagicMock()
    bad_engine.begin.side_effect = RuntimeError("partition exhausted")
    writer = ReadAuditWriter(engine=bad_engine, hit_sample_rate=0.0)

    with pytest.raises(AuditWriteFailed):
        writer.write(
            tenant_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            auth_profile_id="x",
            caller_machine_id=uuid.uuid4(),
            caller_kind="daemon",
            provider="github",
            purpose="t",
            cache_hit=False,
            kek_version=1,
        )


def test_cache_hit_pre_gate_failure_raises_audit_write_failed():
    """Cache-hit failure BEFORE the SHARE-lock gate proves the daemon row
    is unrevoked must fail closed. Otherwise a connection-loss or
    statement-timeout against ``SET LOCAL`` / ``SELECT FOR SHARE`` would
    let the consumer silently treat the gate as passed and return the
    cached credential — bypassing the very protection F26 added.

    Regression for codex round-13 finding F28.
    """
    from unittest.mock import MagicMock

    from nexus.bricks.auth.consumer import AuditWriteFailed

    bad_engine = MagicMock()
    bad_engine.begin.side_effect = RuntimeError("transient db blip")
    writer = ReadAuditWriter(engine=bad_engine, hit_sample_rate=1.0)
    with pytest.raises(AuditWriteFailed):
        writer.write(
            tenant_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            auth_profile_id="x",
            caller_machine_id=uuid.uuid4(),
            caller_kind="daemon",
            provider="github",
            purpose="t",
            cache_hit=True,
            kek_version=1,
        )


def test_cache_hit_post_gate_insert_failure_is_swallowed(engine):
    """Cache-hit failure AFTER the gate is proven (the audit INSERT itself
    fails) is best-effort telemetry — the SHARE-lock SELECT already proved
    the daemon is allowed to read, and the cached credential's first-time
    audit was written at miss-time. Operational telemetry blip on the
    sampled INSERT must not block the caller."""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    _seed_tenant_principal_and_machine(engine, tenant, principal, machine)

    # auth_profile_reads.kek_version is INTEGER NOT NULL; passing a value
    # the column can't store (a string, sneaked past the type checker
    # via Any) makes psycopg2 raise during INSERT — after SET LOCAL +
    # SELECT FOR SHARE have already succeeded, so we exercise the
    # post-gate cache-hit swallow path specifically.
    from typing import Any

    bad_kek: Any = "not-an-int"
    writer = ReadAuditWriter(engine=engine, hit_sample_rate=1.0)
    writer.write(  # no raise — gate passed, INSERT failure swallowed
        tenant_id=tenant,
        principal_id=principal,
        auth_profile_id="x",
        caller_machine_id=machine,
        caller_kind="daemon",
        provider="github",
        purpose="t",
        cache_hit=True,
        kek_version=bad_kek,
    )
    # Confirm no row was inserted (INSERT failed) but no exception bubbled.
    assert _count_reads(engine, tenant) == 0
