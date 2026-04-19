"""FastAPI router: POST /v1/daemon/enroll, POST /v1/daemon/refresh (#3804).

Handles daemon onboarding (single-use enroll token → persistent machine row +
ES256 JWT) and periodic refresh (Ed25519-signed request → fresh JWT).
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nexus.server.api.v1.enroll_tokens import (
    EnrollTokenError,
    consume_enroll_token,
)
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner

_JWT_TTL = timedelta(hours=1)
_REFRESH_SKEW = timedelta(seconds=60)


class EnrollRequest(BaseModel):
    enroll_token: str
    pubkey_pem: str
    daemon_version: str
    hostname: str


class EnrollResponse(BaseModel):
    machine_id: uuid.UUID
    jwt: str
    server_pubkey_pem: str


class RefreshBody(BaseModel):
    machine_id: uuid.UUID
    timestamp_utc: datetime


class RefreshRequest(BaseModel):
    body: RefreshBody
    sig_b64: str


class RefreshResponse(BaseModel):
    jwt: str


def make_daemon_router(
    *,
    engine: Engine,
    signer: JwtSigner,
    enroll_secret: bytes,
) -> APIRouter:
    """Build the ``/v1/daemon`` router: enroll + refresh.

    Parameters
    ----------
    engine:
        SQLAlchemy engine used to persist ``daemon_machines`` rows and consume
        ``daemon_enroll_tokens`` atomically.
    signer:
        ES256 JWT signer used to mint daemon tokens (1h TTL).
    enroll_secret:
        HMAC secret shared with the admin CLI that mints enroll tokens.
    """
    router = APIRouter(prefix="/v1/daemon", tags=["daemon"])

    @router.post("/enroll", response_model=EnrollResponse)
    def enroll(req: EnrollRequest) -> EnrollResponse:
        try:
            claims = consume_enroll_token(
                engine=engine, secret=enroll_secret, token=req.enroll_token
            )
        except EnrollTokenError as exc:
            code = exc.args[0] if exc.args else "enroll_token_invalid"
            if code == "enroll_token_reused":
                raise HTTPException(status.HTTP_409_CONFLICT, detail=code) from exc
            # enroll_token_invalid / enroll_token_expired → 401
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=code) from exc

        # Parse PEM → require Ed25519 → store as DER in daemon_machines.pubkey.
        try:
            pub = serialization.load_pem_public_key(req.pubkey_pem.encode())
        except Exception as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="pubkey_invalid") from exc
        if not isinstance(pub, Ed25519PublicKey):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="pubkey_not_ed25519")
        pub_der = pub.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        machine_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :t"),
                {"t": str(claims.tenant_id)},
            )
            conn.execute(
                text(
                    "INSERT INTO daemon_machines "
                    "(id, tenant_id, principal_id, pubkey, "
                    " daemon_version_last_seen, hostname, "
                    " enrolled_at, last_seen_at) "
                    "VALUES (:id, :tid, :pid, :pk, :ver, :host, NOW(), NOW())"
                ),
                {
                    "id": str(machine_id),
                    "tid": str(claims.tenant_id),
                    "pid": str(claims.principal_id),
                    "pk": pub_der,
                    "ver": req.daemon_version,
                    "host": req.hostname,
                },
            )

        daemon_claims = DaemonClaims(
            tenant_id=claims.tenant_id,
            principal_id=claims.principal_id,
            machine_id=machine_id,
        )
        jwt_str = signer.sign(daemon_claims, ttl=_JWT_TTL)
        return EnrollResponse(
            machine_id=machine_id,
            jwt=jwt_str,
            server_pubkey_pem=signer.public_key_pem.decode(),
        )

    @router.post("/refresh", response_model=RefreshResponse)
    def refresh(req: RefreshRequest) -> RefreshResponse:
        # Cross-tenant lookup: ``/refresh`` only knows ``machine_id`` and must
        # resolve ``tenant_id`` from it to honour RLS on subsequent writes.
        # MVP assumption: the server's DB role has ``BYPASSRLS`` (production)
        # or connects as the superuser (dev / tests). A production hardening
        # follow-up will introduce a dedicated ``nexus_daemon_lookup`` role
        # with ``SELECT`` only on the columns we need here and
        # ``BYPASSRLS`` — see TODO(#3804-followup).
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT tenant_id, principal_id, pubkey, revoked_at "
                    "FROM daemon_machines WHERE id = :m"
                ),
                {"m": str(req.body.machine_id)},
            ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="machine_unknown")
        if row.revoked_at is not None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="machine_revoked")

        now = datetime.now(UTC)
        ts = req.body.timestamp_utc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if abs(now - ts) > _REFRESH_SKEW:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="clock_skew")

        try:
            sig = base64.b64decode(req.sig_b64)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="signature_invalid") from exc

        body_bytes = json.dumps(
            {
                "machine_id": str(req.body.machine_id),
                "timestamp_utc": ts.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        try:
            pub = serialization.load_der_public_key(row.pubkey)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="signature_invalid") from exc
        if not isinstance(pub, Ed25519PublicKey):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="signature_invalid")
        try:
            pub.verify(sig, body_bytes)
        except InvalidSignature as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="signature_invalid") from exc

        # Defence-in-depth: writes after lookup run under the resolved tenant.
        with engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :t"),
                {"t": str(row.tenant_id)},
            )
            conn.execute(
                text("UPDATE daemon_machines SET last_seen_at = NOW() WHERE id = :m"),
                {"m": str(req.body.machine_id)},
            )

        daemon_claims = DaemonClaims(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            machine_id=req.body.machine_id,
        )
        return RefreshResponse(jwt=signer.sign(daemon_claims, ttl=_JWT_TTL))

    return router
