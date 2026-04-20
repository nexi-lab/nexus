"""FastAPI router: POST /v1/admin/daemon-bootstrap (dev convenience, #3804).

Dev-loop helper for `nexus up --with-daemon`. Auto-provisions a tenant +
machine principal (idempotent on name) and returns a fresh enroll token so
a single `nexus up` can bring up the server AND enroll the calling machine
without the operator manually minting credentials.

Security posture:
  * Gated on ``NEXUS_ALLOW_ADMIN_BYPASS=true`` AND a non-empty
    ``NEXUS_ADMIN_BOOTSTRAP_TOKEN`` env var. The caller MUST present the
    token via ``X-Admin-Token`` header, compared in constant time.
    ``X-Admin-User`` is advisory metadata only — not an auth factor.
  * Production deployments that keep admin-bypass disabled OR leave the
    bootstrap token unset never see this endpoint register — the caller in
    ``fastapi_server`` short-circuits.
  * Tokens are still single-use HMACs with 15-minute TTL; the endpoint
    does not hand out any long-lived secret.
"""

from __future__ import annotations

import hmac
import os
import uuid
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from nexus.server.api.v1.enroll_tokens import issue_enroll_token


class BootstrapRequest(BaseModel):
    tenant_name: str = Field(default="dev-local", max_length=128)
    principal_label: str = Field(default="dev-laptop", max_length=128)
    ttl_minutes: int = Field(default=15, ge=1, le=60)


class BootstrapResponse(BaseModel):
    tenant_id: str
    principal_id: str
    enroll_token: str


def _ensure_tenant(conn: Connection, *, name: str) -> uuid.UUID:
    """Idempotent find-or-create for tenants.name.

    Uses ``INSERT ... ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
    RETURNING id`` so concurrent calls converge on the same tenant id without
    tripping the UNIQUE constraint. The no-op UPDATE forces RETURNING to
    emit a row on both insert and conflict paths.
    """
    tid = uuid.uuid4()
    row = conn.execute(
        text(
            "INSERT INTO tenants (id, name) VALUES (:id, :n) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id"
        ),
        {"id": str(tid), "n": name},
    ).fetchone()
    assert row is not None  # RETURNING always yields a row with DO UPDATE
    return uuid.UUID(str(row.id))


_AUTH_METHOD = "dev-bootstrap"


def _ensure_machine_principal(conn: Connection, *, tenant_id: uuid.UUID, label: str) -> uuid.UUID:
    """Idempotent find-or-create for a machine principal keyed by label.

    ``principal_aliases`` PK is ``(tenant_id, auth_method, external_sub)``,
    so a conflict on that tuple identifies an existing enrollment. We:
      1. ``INSERT`` a fresh ``principals`` row (id collision impossible for
         a freshly-generated UUID, but ``ON CONFLICT (id) DO NOTHING``
         guards against caller reuse).
      2. ``INSERT`` the alias; if the (tenant, auth_method, label) tuple
         already exists, ``DO UPDATE`` returns the existing ``principal_id``
         — which is the principal the caller should reuse.
      3. Delete the orphaned freshly-minted ``principals`` row when the
         alias pointed to an existing principal, so we don't leak rows
         under concurrent bootstrap.
    """
    conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})

    new_pid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO principals (id, tenant_id, kind, created_at) "
            "VALUES (:id, :t, 'machine', NOW()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(new_pid), "t": str(tenant_id)},
    )
    row = conn.execute(
        text(
            "INSERT INTO principal_aliases "
            "(tenant_id, auth_method, external_sub, principal_id) "
            "VALUES (:t, :m, :s, :p) "
            "ON CONFLICT (tenant_id, auth_method, external_sub) "
            "DO UPDATE SET external_sub = EXCLUDED.external_sub "
            "RETURNING principal_id"
        ),
        {"t": str(tenant_id), "m": _AUTH_METHOD, "s": label, "p": str(new_pid)},
    ).fetchone()
    assert row is not None  # DO UPDATE guarantees a RETURNING row
    resolved_pid = uuid.UUID(str(row.principal_id))
    if resolved_pid != new_pid:
        # Alias pointed to a pre-existing principal; drop our speculative insert.
        conn.execute(
            text("DELETE FROM principals WHERE id = :id AND tenant_id = :t"),
            {"id": str(new_pid), "t": str(tenant_id)},
        )
    return resolved_pid


def make_admin_bootstrap_router(
    *,
    engine: Engine,
    enroll_secret: bytes,
    admin_user: str,
    bootstrap_token: bytes,
) -> APIRouter:
    """Build the ``/v1/admin/daemon-bootstrap`` router.

    Only call this when ``NEXUS_ALLOW_ADMIN_BYPASS`` is true AND
    ``NEXUS_ADMIN_BOOTSTRAP_TOKEN`` is a non-empty value — the wiring in
    ``fastapi_server.create_app`` enforces both outer guards.

    ``bootstrap_token`` is the raw bytes the caller must present in
    ``X-Admin-Token``. An empty token is a programmer error and raises
    ``ValueError`` to fail closed.
    """
    if not bootstrap_token:
        raise ValueError("bootstrap_token must be non-empty")

    router = APIRouter(prefix="/v1/admin", tags=["admin"])

    @router.post("/daemon-bootstrap", response_model=BootstrapResponse)
    def bootstrap(
        req: BootstrapRequest,
        x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
        x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ) -> BootstrapResponse:
        if os.environ.get("NEXUS_ALLOW_ADMIN_BYPASS", "").lower() not in (
            "1",
            "true",
            "yes",
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_available")
        # Auth: constant-time compare on the shared token. The user header is
        # advisory — matching only X-Admin-User does NOT authenticate.
        presented = (x_admin_token or "").encode()
        if not hmac.compare_digest(presented, bootstrap_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="admin_token_mismatch"
            )
        if x_admin_user != admin_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="admin_user_mismatch"
            )

        with engine.begin() as conn:
            tenant_id = _ensure_tenant(conn, name=req.tenant_name)
            principal_id = _ensure_machine_principal(
                conn, tenant_id=tenant_id, label=req.principal_label
            )

        token = issue_enroll_token(
            engine=engine,
            secret=enroll_secret,
            tenant_id=tenant_id,
            principal_id=principal_id,
            ttl=timedelta(minutes=req.ttl_minutes),
        )
        return BootstrapResponse(
            tenant_id=str(tenant_id),
            principal_id=str(principal_id),
            enroll_token=token,
        )

    return router
