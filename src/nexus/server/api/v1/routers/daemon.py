"""FastAPI router: POST /v1/daemon/enroll, POST /v1/daemon/refresh (#3804).

Handles daemon onboarding (single-use enroll token → persistent machine row +
ES256 JWT) and periodic refresh (Ed25519-signed request → fresh JWT).

Refresh uses a *sign-raw* wire contract: the client sends the pre-canonicalized
JSON string (``body_raw``) alongside an Ed25519 signature over ``body_raw``'s
UTF-8 bytes. The server never re-serializes — it verifies over the exact bytes
the client sent, so cross-language / serializer-drift mismatches are impossible.
The signed body includes ``tenant_id`` so the server can ``SET LOCAL
app.current_tenant`` before any ``daemon_machines`` lookup, keeping the path
RLS-safe by default (no ``BYPASSRLS`` reliance).
"""

from __future__ import annotations

import base64
import json
import secrets
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
# Nonce rows older than this are replay-irrelevant (the signed timestamp they
# guard has already fallen outside ``_REFRESH_SKEW`` on both sides). We keep
# a generous buffer — 10× skew — so clock drift on a lagging daemon still
# sees its replay protection intact. Rows older than the retention window are
# pruned opportunistically on refresh.
_NONCE_RETENTION = _REFRESH_SKEW * 10
# Prune opportunistically on a fraction of refreshes. At ~1/32 sampling the
# cleanup cost amortizes across refreshes without any cron/background worker,
# and a few hundred refreshes per minute is enough to keep the table bounded.
_NONCE_PRUNE_SAMPLE_DENOMINATOR = 32


class EnrollRequest(BaseModel):
    enroll_token: str
    pubkey_pem: str
    daemon_version: str
    hostname: str


class EnrollResponse(BaseModel):
    machine_id: uuid.UUID
    jwt: str
    server_pubkey_pem: str


class RefreshRequest(BaseModel):
    """Sign-raw refresh payload.

    ``body_raw`` is the client-canonicalized JSON string, e.g.
    ``{"machine_id":"...","tenant_id":"...","timestamp_utc":"..."}``.
    ``sig_b64`` is the base64 of the Ed25519 signature over
    ``body_raw.encode("utf-8")``.
    """

    body_raw: str
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
        # Parse the pubkey FIRST, before claiming the single-use token.
        # If we consumed the token upfront and then bailed on a malformed
        # pubkey, the operator would have to re-mint a fresh token for
        # every retry — an easy denial-of-enrollment path.
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
        # One transaction: consume token → insert machine. If the INSERT
        # raises, the outer ``engine.begin()`` context rolls back, leaving
        # ``used_at`` NULL so the caller can retry with the same token.
        with engine.begin() as conn:
            try:
                claims = consume_enroll_token(
                    engine=engine,
                    secret=enroll_secret,
                    token=req.enroll_token,
                    conn=conn,
                )
            except EnrollTokenError as exc:
                code = exc.args[0] if exc.args else "enroll_token_invalid"
                if code == "enroll_token_reused":
                    raise HTTPException(status.HTTP_409_CONFLICT, detail=code) from exc
                # enroll_token_invalid / enroll_token_expired → 401
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=code) from exc
            # SET LOCAL is already applied inside consume_enroll_token; the
            # daemon_machines INSERT runs in the same transaction so RLS
            # evaluates against the same tenant.
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
        # ---- Parse the sign-raw payload ---------------------------------
        # Decode the signature first — a malformed b64 means we can't verify
        # anything, so short-circuit as signature_invalid.
        try:
            sig = base64.b64decode(req.sig_b64, validate=True)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="signature_invalid") from exc

        # Parse ``body_raw`` to extract fields, but do NOT re-serialize —
        # we will verify the signature over the exact bytes the client sent.
        try:
            parsed = json.loads(req.body_raw)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="body_malformed") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="body_malformed")

        raw_machine_id = parsed.get("machine_id")
        raw_tenant_id = parsed.get("tenant_id")
        raw_timestamp = parsed.get("timestamp_utc")
        raw_nonce = parsed.get("nonce")
        if (
            not isinstance(raw_machine_id, str)
            or not isinstance(raw_tenant_id, str)
            or not isinstance(raw_timestamp, str)
            or not isinstance(raw_nonce, str)
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="body_malformed")

        try:
            machine_id = uuid.UUID(raw_machine_id)
            tenant_id = uuid.UUID(raw_tenant_id)
            nonce = uuid.UUID(raw_nonce)
        except ValueError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="body_malformed") from exc

        # Validate timestamp: ISO 8601 + tz-aware.
        try:
            ts = datetime.fromisoformat(raw_timestamp)
        except ValueError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="body_malformed") from exc
        if ts.tzinfo is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="body_malformed")

        # ---- Clock skew (cheap check before any DB work) ----------------
        now = datetime.now(UTC)
        if abs(now - ts) > _REFRESH_SKEW:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="clock_skew")

        # ---- One transaction: lookup + signature check + issuance gate --
        # Previously the lookup, signature verification, and last_seen_at
        # UPDATE ran across two separate transactions with a no-guard UPDATE
        # at the end. A revoke that landed between the two windows would
        # still mint a fresh 1h JWT. The fix: acquire a row lock with
        # ``SELECT ... FOR UPDATE`` so any concurrent revoke blocks until
        # this tx commits; then issue the token only if the final
        # revocation-guarded UPDATE actually matches.
        with engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :t"),
                {"t": str(tenant_id)},
            )
            row = conn.execute(
                text(
                    "SELECT tenant_id, principal_id, pubkey, revoked_at "
                    "FROM daemon_machines "
                    "WHERE id = :m AND tenant_id = :t "
                    "FOR UPDATE"
                ),
                {"m": str(machine_id), "t": str(tenant_id)},
            ).fetchone()
            if row is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="machine_unknown")
            if row.revoked_at is not None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="machine_revoked")

            # Signature verification over raw bytes — safe to do inside the
            # tx, holding only the row lock we already acquired.
            try:
                pub = serialization.load_der_public_key(row.pubkey)
            except Exception as exc:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, detail="signature_invalid"
                ) from exc
            if not isinstance(pub, Ed25519PublicKey):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="signature_invalid")
            try:
                pub.verify(sig, req.body_raw.encode("utf-8"))
            except InvalidSignature as exc:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, detail="signature_invalid"
                ) from exc

            # Replay protection: enforce single-use on the nonce BEFORE
            # issuing the JWT. ``ON CONFLICT DO NOTHING RETURNING`` returns
            # an empty set iff a prior request already claimed this
            # (tenant, machine, nonce) tuple — reject as replay.
            claim = conn.execute(
                text(
                    "INSERT INTO daemon_refresh_nonces "
                    "(nonce, tenant_id, machine_id) "
                    "VALUES (:n, :t, :m) "
                    "ON CONFLICT (tenant_id, machine_id, nonce) DO NOTHING "
                    "RETURNING nonce"
                ),
                {"n": str(nonce), "t": str(tenant_id), "m": str(machine_id)},
            ).fetchone()
            if claim is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="nonce_replay")

            # Opportunistic prune. Without this the nonce table grows forever
            # (1 insert per refresh, ~hourly per daemon). We keep rows for
            # ``_NONCE_RETENTION`` so replay protection survives worst-case
            # clock drift, then delete — running on ~1/N refreshes so the
            # cost amortizes. No background job or external scheduler needed.
            if secrets.randbelow(_NONCE_PRUNE_SAMPLE_DENOMINATOR) == 0:
                conn.execute(
                    text("DELETE FROM daemon_refresh_nonces WHERE seen_at < NOW() - :retention"),
                    {"retention": _NONCE_RETENTION},
                )

            # Final issuance gate: UPDATE guarded on ``revoked_at IS NULL``.
            # If a concurrent revoke snuck in between FOR UPDATE and here
            # (impossible under the same row lock, but belt-and-suspenders)
            # the RETURNING set is empty and we reject.
            gate = conn.execute(
                text(
                    "UPDATE daemon_machines SET last_seen_at = NOW() "
                    "WHERE id = :m AND tenant_id = :t AND revoked_at IS NULL "
                    "RETURNING principal_id"
                ),
                {"m": str(machine_id), "t": str(tenant_id)},
            ).fetchone()
            if gate is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="machine_revoked")

        daemon_claims = DaemonClaims(
            tenant_id=row.tenant_id,
            principal_id=row.principal_id,
            machine_id=machine_id,
        )
        return RefreshResponse(jwt=signer.sign(daemon_claims, ttl=_JWT_TTL))

    return router
