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
from sqlalchemy.engine import Engine

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


def consume_enroll_token(
    *,
    engine: Engine,
    secret: bytes,
    token: str,
) -> EnrollClaims:
    """Verify HMAC, expiry, and single-use; mark ``used_at``."""
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

    with engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.current_tenant = :t"),
            {"t": str(tid)},
        )
        row = conn.execute(
            text("SELECT used_at FROM daemon_enroll_tokens WHERE jti = :jti AND tenant_id = :tid"),
            {"jti": str(jti), "tid": str(tid)},
        ).fetchone()
        if row is None:
            raise EnrollTokenError("enroll_token_invalid")
        if row.used_at is not None:
            raise EnrollTokenError("enroll_token_reused")
        conn.execute(
            text(
                "UPDATE daemon_enroll_tokens SET used_at = NOW() "
                "WHERE jti = :jti AND tenant_id = :tid"
            ),
            {"jti": str(jti), "tid": str(tid)},
        )

    return EnrollClaims(jti=jti, tenant_id=tid, principal_id=pid, exp=exp)
