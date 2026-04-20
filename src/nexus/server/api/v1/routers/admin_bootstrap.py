"""FastAPI router: POST /v1/admin/daemon-bootstrap (dev convenience, #3804).

Dev-loop helper for `nexus up --with-daemon`. Auto-provisions a tenant +
machine principal (idempotent on name) and returns a fresh enroll token so
a single `nexus up` can bring up the server AND enroll the calling machine
without the operator manually minting credentials.

Security posture:
  * Gated on ``NEXUS_ALLOW_ADMIN_BYPASS=true`` AND a matching
    ``X-Admin-User`` header (same guard used by other admin-bypass routes).
  * Production deployments that keep admin-bypass disabled never see this
    endpoint register — the caller in ``fastapi_server`` short-circuits.
  * Tokens are still single-use HMACs with 15-minute TTL; the endpoint
    does not hand out any long-lived secret.
"""

from __future__ import annotations

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
    row = conn.execute(text("SELECT id FROM tenants WHERE name = :n"), {"n": name}).fetchone()
    if row is not None:
        return uuid.UUID(str(row.id))
    tid = uuid.uuid4()
    conn.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, :n)"),
        {"id": str(tid), "n": name},
    )
    return tid


_AUTH_METHOD = "dev-bootstrap"


def _ensure_machine_principal(conn: Connection, *, tenant_id: uuid.UUID, label: str) -> uuid.UUID:
    """Find-or-create a machine principal for (tenant, label).

    Uses ``principal_aliases(tenant_id, auth_method, external_sub)`` as the
    stable lookup key so repeated calls with the same label return the same
    principal.
    """
    # RLS-forced tables need SET LOCAL app.current_tenant before any access.
    conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})

    row = conn.execute(
        text(
            "SELECT principal_id FROM principal_aliases "
            "WHERE tenant_id = :t AND auth_method = :m AND external_sub = :s"
        ),
        {"t": str(tenant_id), "m": _AUTH_METHOD, "s": label},
    ).fetchone()
    if row is not None:
        return uuid.UUID(str(row.principal_id))

    pid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO principals (id, tenant_id, kind, created_at) "
            "VALUES (:id, :t, 'machine', NOW())"
        ),
        {"id": str(pid), "t": str(tenant_id)},
    )
    conn.execute(
        text(
            "INSERT INTO principal_aliases "
            "(tenant_id, auth_method, external_sub, principal_id) "
            "VALUES (:t, :m, :s, :p)"
        ),
        {"t": str(tenant_id), "m": _AUTH_METHOD, "s": label, "p": str(pid)},
    )
    return pid


def make_admin_bootstrap_router(
    *, engine: Engine, enroll_secret: bytes, admin_user: str
) -> APIRouter:
    """Build the ``/v1/admin/daemon-bootstrap`` router.

    Only call this when ``NEXUS_ALLOW_ADMIN_BYPASS`` is true — the wiring
    in ``fastapi_server.create_app`` enforces that outer guard.
    """
    router = APIRouter(prefix="/v1/admin", tags=["admin"])

    @router.post("/daemon-bootstrap", response_model=BootstrapResponse)
    def bootstrap(
        req: BootstrapRequest,
        x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
    ) -> BootstrapResponse:
        if os.environ.get("NEXUS_ALLOW_ADMIN_BYPASS", "").lower() not in (
            "1",
            "true",
            "yes",
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_available")
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
