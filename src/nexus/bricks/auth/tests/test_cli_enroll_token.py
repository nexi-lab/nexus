"""Tests for `nexus auth enroll-token` admin command (#3804)."""

from __future__ import annotations

import uuid

import pytest
from click.testing import CliRunner
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.cli_commands import auth
from nexus.bricks.auth.tests.test_postgres_profile_store import (
    PG_URL,
    ensure_principal,
    ensure_tenant,
    pg_engine,  # noqa: F401  -- pytest fixture re-export
)


def test_enroll_token_command_prints_token(
    pg_engine: Engine,  # noqa: F811  -- pytest fixture reuse
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = ensure_tenant(pg_engine, f"cli-{uuid.uuid4()}")
    p = ensure_principal(
        pg_engine, tenant_id=t, external_sub=f"u-{uuid.uuid4()}", auth_method="oidc"
    )
    monkeypatch.setenv("NEXUS_ENROLL_TOKEN_SECRET", "cli-secret-32bytes-abcdef01234567")
    monkeypatch.setenv("NEXUS_AUTH_DB_URL", PG_URL)
    runner = CliRunner()
    res = runner.invoke(
        auth,
        [
            "enroll-token",
            "--tenant-id",
            str(t),
            "--principal-id",
            str(p),
            "--ttl-minutes",
            "15",
        ],
    )
    assert res.exit_code == 0, res.output
    # base64url.base64url encoding
    assert "." in res.output
    # Verify a row was inserted
    with pg_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(t)})
        count = conn.execute(
            text("SELECT COUNT(*) FROM daemon_enroll_tokens WHERE tenant_id = :t"),
            {"t": str(t)},
        ).scalar()
    assert count == 1


def test_enroll_token_refuses_without_secret(
    pg_engine: Engine,  # noqa: F811  -- pytest fixture reuse
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = ensure_tenant(pg_engine, f"cli-{uuid.uuid4()}")
    p = ensure_principal(
        pg_engine, tenant_id=t, external_sub=f"u-{uuid.uuid4()}", auth_method="oidc"
    )
    monkeypatch.delenv("NEXUS_ENROLL_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("NEXUS_AUTH_DB_URL", PG_URL)
    runner = CliRunner()
    res = runner.invoke(
        auth,
        ["enroll-token", "--tenant-id", str(t), "--principal-id", str(p)],
    )
    assert res.exit_code != 0
    assert "NEXUS_ENROLL_TOKEN_SECRET" in res.output
