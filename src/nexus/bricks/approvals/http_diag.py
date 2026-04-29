"""Read-only HTTP diagnostic dump for ops smoke-testing."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException

from nexus.bricks.approvals.service import ApprovalService


def register_diag_router(
    app: FastAPI,
    service: ApprovalService,
    *,
    allow_subject: str | None,
) -> None:
    router = APIRouter()

    def _check_auth(authorization: str | None) -> None:
        if allow_subject is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer")
        token = authorization.removeprefix("Bearer ").strip()
        if token != allow_subject:
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
