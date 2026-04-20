"""HMAC-signed single-use enrollment tokens (#3804).

Admin CLI mints an enroll token scoped to (tenant_id, principal_id, jti, exp).
The jti is persisted in ``daemon_enroll_tokens``; consuming marks
``used_at = NOW()``. Replay, tamper, and expiry are all rejected.

Token format: ``base64url(body_json) + "." + base64url(hmac_sha256(body))``.
Body JSON is canonical: sorted keys, ``(",", ":")`` separators.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

_ALG = "HS256"


class EnrollTokenError(Exception):
    """Invalid, expired, reused, or tampered enroll token."""


@dataclass(frozen=True)
class EnrollClaims:
    jti: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    exp: datetime


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(secret: bytes, body: bytes) -> bytes:
    return hmac.new(secret, body, hashlib.sha256).digest()


def issue_enroll_token(
    *,
    engine: Engine,
    secret: bytes,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    ttl: timedelta,
) -> str:
    """Insert a ``daemon_enroll_tokens`` row and return an encoded token string."""
    jti = uuid.uuid4()
    now = datetime.now(UTC)
    exp = now + ttl
    with engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.current_tenant = :t"),
            {"t": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO daemon_enroll_tokens "
                "(jti, tenant_id, principal_id, issued_at, expires_at) "
                "VALUES (:jti, :tid, :pid, :iat, :exp)"
            ),
            {
                "jti": str(jti),
                "tid": str(tenant_id),
                "pid": str(principal_id),
                "iat": now,
                "exp": exp,
            },
        )
    body = json.dumps(
        {
            "alg": _ALG,
            "exp": int(exp.timestamp()),
            "jti": str(jti),
            "pid": str(principal_id),
            "tid": str(tenant_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    sig = _sign(secret, body)
    return f"{_b64(body)}.{_b64(sig)}"


def _parse_and_verify_token(*, secret: bytes, token: str) -> EnrollClaims:
    """Verify HMAC signature and expiry; return parsed claims WITHOUT consuming.

    Used by the router so it can do all fallible work (pubkey parse, tenant
    guard) before burning the single-use token. A separate ``_claim_token_jti``
    runs inside the caller's transaction to do the atomic ``used_at`` update.
    """
    try:
        body_b64, sig_b64 = token.split(".")
        body = _unb64(body_b64)
        sig = _unb64(sig_b64)
    except Exception as exc:
        raise EnrollTokenError("enroll_token_invalid") from exc

    expected = _sign(secret, body)
    if not hmac.compare_digest(sig, expected):
        raise EnrollTokenError("enroll_token_invalid")

    try:
        parsed = json.loads(body.decode())
        if parsed["alg"] != _ALG:
            raise EnrollTokenError("enroll_token_invalid")
        jti = uuid.UUID(parsed["jti"])
        tid = uuid.UUID(parsed["tid"])
        pid = uuid.UUID(parsed["pid"])
        exp = datetime.fromtimestamp(int(parsed["exp"]), tz=UTC)
    except EnrollTokenError:
        raise
    except Exception as exc:
        raise EnrollTokenError("enroll_token_invalid") from exc

    if datetime.now(UTC) >= exp:
        raise EnrollTokenError("enroll_token_expired")

    return EnrollClaims(jti=jti, tenant_id=tid, principal_id=pid, exp=exp)


def _claim_token_jti(conn: Connection, *, claims: EnrollClaims) -> None:
    """Atomically mark a token as used inside the caller's transaction.

    Raises ``EnrollTokenError`` if the row is missing or already consumed.
    Caller must have ``SET LOCAL app.current_tenant = :t`` already.
    """
    claimed = conn.execute(
        text(
            "UPDATE daemon_enroll_tokens SET used_at = NOW() "
            "WHERE jti = :jti AND tenant_id = :tid AND used_at IS NULL "
            "RETURNING jti"
        ),
        {"jti": str(claims.jti), "tid": str(claims.tenant_id)},
    ).fetchone()
    if claimed is not None:
        return
    probe = conn.execute(
        text("SELECT used_at FROM daemon_enroll_tokens WHERE jti = :jti AND tenant_id = :tid"),
        {"jti": str(claims.jti), "tid": str(claims.tenant_id)},
    ).fetchone()
    if probe is None:
        raise EnrollTokenError("enroll_token_invalid")
    raise EnrollTokenError("enroll_token_reused")


def consume_enroll_token(
    *,
    engine: Engine,
    secret: bytes,
    token: str,
    conn: Connection | None = None,
) -> EnrollClaims:
    """Verify HMAC, expiry, and single-use; mark ``used_at``.

    When ``conn`` is provided the atomic claim runs inside the caller's
    transaction — callers can roll back any downstream insert failure and
    re-use the token (the token is NOT consumed until their transaction
    commits). When ``conn`` is None the function opens its own transaction
    for backward compatibility with callers that don't need atomicity.
    """
    claims = _parse_and_verify_token(secret=secret, token=token)
    if conn is not None:
        conn.execute(
            text("SET LOCAL app.current_tenant = :t"),
            {"t": str(claims.tenant_id)},
        )
        _claim_token_jti(conn, claims=claims)
        return claims

    with engine.begin() as scoped:
        scoped.execute(
            text("SET LOCAL app.current_tenant = :t"),
            {"t": str(claims.tenant_id)},
        )
        _claim_token_jti(scoped, claims=claims)
    return claims
