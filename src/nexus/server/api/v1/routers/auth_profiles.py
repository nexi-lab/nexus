"""FastAPI router: POST /v1/auth-profiles (daemon push, #3804).

The daemon envelope-encrypts the user credential locally and ships the
5-field envelope (``ciphertext``/``wrapped_dek``/``nonce``/``aad``/
``kek_version``) plus routing metadata + audit stamps. The server never
decrypts on this path — it persists the bytes verbatim via
``PostgresAuthProfileStore.upsert_with_envelope`` so no KMS/Vault round-
trip or access to plaintext is required.

Authentication: ``Authorization: Bearer <jwt>`` (ES256) issued by the
server's ``JwtSigner``. Verification happens before any DB work; every
push also re-checks ``daemon_machines.revoked_at`` so revocation is an
immediate control (not bounded by the JWT lifetime).

Atomicity: the profile upsert and the ``auth_profile_writes`` audit row
execute inside the SAME database transaction. Either both commit or
neither — retries never observe a profile mutation without its matching
audit entry.

Conflict handling: advisory only. If the client provides
``updated_at_override`` that is older than the stored ``updated_at`` AND
the ``source_file_hash`` differs, a WARNING is logged but the write still
proceeds ("last-write wins on updated_at").
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.postgres_profile_store import PostgresAuthProfileStore
from nexus.bricks.auth.profile import AuthProfile
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner, JwtVerifyError

log = logging.getLogger(__name__)


class EnvelopePayload(BaseModel):
    ciphertext_b64: str
    wrapped_dek_b64: str
    nonce_b64: str
    aad_b64: str
    kek_version: int


class PushRequest(BaseModel):
    id: str
    provider: str
    account_identifier: str
    backend: str
    backend_key: str
    envelope: EnvelopePayload
    source_file_hash: str
    sync_ttl_seconds: int = 300
    # Required + non-empty at the API boundary: every central write carries
    # the exact daemon build that produced it so rollback / incident
    # forensics can attribute bad state. Missing or empty → 422 from pydantic.
    daemon_version: str = Field(
        ...,
        min_length=1,
        description=(
            "Daemon build version. Required — persisted on every write for "
            "rollback / forensics attribution when a bad daemon emits corrupt "
            "state."
        ),
    )
    updated_at_override: datetime | None = Field(
        default=None,
        description="Test-only: override updated_at for conflict-detection tests.",
    )


def _verify_auth(
    signer: JwtSigner,
    authorization: str | None,
) -> DaemonClaims:
    """Parse ``Authorization: Bearer <jwt>`` and verify via ``signer``.

    Raises:
        HTTPException(401, "missing_bearer"): header missing or not a Bearer.
        HTTPException(401, <reason>): JWT signature/expiry/issuer/audience
            mismatch; ``<reason>`` comes straight from ``JwtVerifyError``.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_bearer")
    token = authorization[len("Bearer ") :].strip()
    try:
        return signer.verify(token)
    except JwtVerifyError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def make_auth_profiles_router(*, engine: Engine, signer: JwtSigner) -> APIRouter:
    """Build the ``/v1/auth-profiles`` router.

    Parameters
    ----------
    engine:
        SQLAlchemy engine for the shared per-request transaction that wraps
        the revocation check, profile upsert, and audit insert.
    signer:
        ES256 ``JwtSigner`` that minted the daemon's current JWT. The router
        only *verifies* tokens — it does not issue them.
    """
    router = APIRouter(prefix="/v1/auth-profiles", tags=["auth-profiles"])

    @router.post("")
    def push(
        req: PushRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        claims = _verify_auth(signer, authorization)

        # Decode the envelope once — fail fast on malformed b64.
        try:
            envelope = {
                "ciphertext": base64.b64decode(req.envelope.ciphertext_b64),
                "wrapped_dek": base64.b64decode(req.envelope.wrapped_dek_b64),
                "nonce": base64.b64decode(req.envelope.nonce_b64),
                "aad": base64.b64decode(req.envelope.aad_b64),
                "kek_version": req.envelope.kek_version,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="envelope_b64_invalid",
            ) from exc

        profile = AuthProfile(
            id=req.id,
            provider=req.provider,
            account_identifier=req.account_identifier,
            backend=req.backend,
            backend_key=req.backend_key,
            sync_ttl_seconds=req.sync_ttl_seconds,
            last_synced_at=datetime.now(UTC),
        )

        store = PostgresAuthProfileStore(
            db_url=str(engine.url),
            tenant_id=claims.tenant_id,
            principal_id=claims.principal_id,
            engine=engine,
            # No encryption_provider — the envelope was pre-built by the daemon.
        )

        # Single transaction: revocation check → advisory conflict read →
        # profile upsert → audit insert. If ANY step fails the whole push
        # rolls back, so retries never observe a half-written state.
        with engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :t"),
                {"t": str(claims.tenant_id)},
            )

            # Revocation / existence check (immediate control, independent
            # of JWT expiry). ``FOR UPDATE`` row-locks the machine for the
            # duration of this transaction so a concurrent revoke has to
            # either land BEFORE this SELECT (we see it) or wait until
            # after we commit the write. Without the lock the revoke could
            # slide in between this check and the upsert/audit, allowing
            # one post-revocation write.
            machine_row = conn.execute(
                text(
                    "SELECT revoked_at FROM daemon_machines "
                    "WHERE tenant_id = :t AND principal_id = :p AND id = :m "
                    "FOR UPDATE"
                ),
                {
                    "t": str(claims.tenant_id),
                    "p": str(claims.principal_id),
                    "m": str(claims.machine_id),
                },
            ).fetchone()
            if machine_row is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="machine_unknown",
                )
            if machine_row.revoked_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="machine_revoked",
                )

            # Advisory conflict detection — log only, still write.
            cur = conn.execute(
                text(
                    "SELECT source_file_hash, updated_at FROM auth_profiles "
                    "WHERE tenant_id = :t AND principal_id = :p AND id = :id"
                ),
                {
                    "t": str(claims.tenant_id),
                    "p": str(claims.principal_id),
                    "id": req.id,
                },
            ).fetchone()
            if (
                cur is not None
                and cur.source_file_hash is not None
                and cur.source_file_hash != req.source_file_hash
                and req.updated_at_override is not None
                and req.updated_at_override < cur.updated_at
            ):
                log.warning(
                    "push_conflict_stale_write tenant=%s principal=%s id=%s "
                    "server_hash=%s incoming_hash=%s",
                    claims.tenant_id,
                    claims.principal_id,
                    req.id,
                    cur.source_file_hash,
                    req.source_file_hash,
                )

            # Profile upsert (atomic with audit insert below). daemon_version
            # comes from the push payload so central state carries the exact
            # daemon build that produced each envelope.
            store.upsert_with_envelope(
                profile,
                envelope=envelope,
                source_file_hash=req.source_file_hash,
                daemon_version=req.daemon_version,
                machine_id=claims.machine_id,
                conn=conn,
            )

            # Append-only audit record — one row per push, never updated or deleted.
            conn.execute(
                text(
                    "INSERT INTO auth_profile_writes "
                    "(id, tenant_id, principal_id, auth_profile_id, machine_id, "
                    " daemon_version, source_file_hash) "
                    "VALUES (:id, :tid, :pid, :apid, :mid, :ver, :hash)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(claims.tenant_id),
                    "pid": str(claims.principal_id),
                    "apid": req.id,
                    "mid": str(claims.machine_id),
                    "ver": req.daemon_version,
                    "hash": req.source_file_hash,
                },
            )

        return {"status": "ok"}

    return router
