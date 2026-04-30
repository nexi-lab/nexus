"""Read-only HTTP diagnostic dump for ops smoke-testing.

The dump leaks pending-approval rows (subjects, session_ids, agent_ids,
reasons, metadata). It must always be bearer-token-gated; calling
``register_diag_router`` with ``allow_subject=None`` is a programming
error and raises ``ValueError``. The lifespan caller is responsible for
checking ``NEXUS_APPROVALS_DIAG_TOKEN`` and skipping registration when
unset.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException

from nexus.bricks.approvals.service import ApprovalService


def register_diag_router(
    app: FastAPI,
    service: ApprovalService,
    *,
    allow_subject: str,
) -> None:
    if not allow_subject:
        raise ValueError(
            "register_diag_router: allow_subject must be a non-empty bearer token; "
            "the diag dump cannot be exposed unauthenticated."
        )

    router = APIRouter()

    def _check_auth(authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer")
        token = authorization.removeprefix("Bearer ").strip()
        # Constant-time compare: defends against token-length / timing
        # inference if an attacker is probing for the diag secret.
        if not hmac.compare_digest(token, allow_subject):
            raise HTTPException(status_code=403, detail="forbidden")

    @router.get("/hub/approvals/dump")
    async def dump(
        zone_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        pending = await service.list_pending(zone_id=zone_id)
        return {
            "pending": [p.to_dict() for p in pending],
        }

    app.include_router(router)
