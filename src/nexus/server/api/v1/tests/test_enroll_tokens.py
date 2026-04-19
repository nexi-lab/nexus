"""Tests for src/nexus/server/api/v1/enroll_tokens.py."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.engine import Engine

from nexus.bricks.auth.tests.test_postgres_profile_store import (
    ensure_principal,
    ensure_tenant,
)
from nexus.server.api.v1.enroll_tokens import (
    EnrollTokenError,
    consume_enroll_token,
    issue_enroll_token,
)

SECRET = b"test-enroll-secret-32bytes-abcdef0"


def _setup(pg_engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    t = ensure_tenant(pg_engine, f"enroll-{uuid.uuid4()}")
    p = ensure_principal(
        pg_engine,
        tenant_id=t,
        external_sub=f"u-{uuid.uuid4()}",
        auth_method="oidc",
    )
    return t, p


def test_issue_and_consume_roundtrip(pg_engine: Engine) -> None:
    t, p = _setup(pg_engine)
    token = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    claims = consume_enroll_token(engine=pg_engine, secret=SECRET, token=token)
    assert claims.tenant_id == t
    assert claims.principal_id == p


def test_reused_token_rejected(pg_engine: Engine) -> None:
    t, p = _setup(pg_engine)
    token = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    consume_enroll_token(engine=pg_engine, secret=SECRET, token=token)
    with pytest.raises(EnrollTokenError, match="reused"):
        consume_enroll_token(engine=pg_engine, secret=SECRET, token=token)


def test_tampered_token_rejected(pg_engine: Engine) -> None:
    t, p = _setup(pg_engine)
    token = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    bad = token[:-3] + "AAA"
    with pytest.raises(EnrollTokenError, match="invalid"):
        consume_enroll_token(engine=pg_engine, secret=SECRET, token=bad)


def test_expired_token_rejected(pg_engine: Engine) -> None:
    t, p = _setup(pg_engine)
    token = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(seconds=-5),
    )
    with pytest.raises(EnrollTokenError, match="expired"):
        consume_enroll_token(engine=pg_engine, secret=SECRET, token=token)


def test_wrong_secret_rejected(pg_engine: Engine) -> None:
    t, p = _setup(pg_engine)
    token = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    other = b"other-enroll-secret-32bytes-abcdef"
    with pytest.raises(EnrollTokenError, match="invalid"):
        consume_enroll_token(engine=pg_engine, secret=other, token=token)


def test_concurrent_consume_exactly_one_wins(pg_engine: Engine) -> None:
    """Two simultaneous consumers of the same token → exactly one wins."""
    import threading

    t, p = _setup(pg_engine)
    token = issue_enroll_token(
        engine=pg_engine,
        secret=SECRET,
        tenant_id=t,
        principal_id=p,
        ttl=timedelta(minutes=15),
    )
    results: list[str | None] = [None, None]

    def worker(i: int) -> None:
        try:
            consume_enroll_token(engine=pg_engine, secret=SECRET, token=token)
            results[i] = "success"
        except EnrollTokenError as exc:
            results[i] = exc.args[0] if exc.args else "error"

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    success_count = sum(1 for r in results if r == "success")
    reused_count = sum(1 for r in results if r == "enroll_token_reused")
    assert success_count == 1, f"expected exactly one winner, got {results}"
    assert reused_count == 1, f"loser should report 'reused', got {results}"
