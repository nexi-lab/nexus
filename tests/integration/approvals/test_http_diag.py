"""Integration tests for the approvals brick HTTP diagnostic dump endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nexus.bricks.approvals.http_diag import register_diag_router
from nexus.bricks.approvals.models import ApprovalKind
from nexus.bricks.approvals.service import ApprovalService

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


async def _wait_pending(service: ApprovalService, rid: str) -> None:
    """Poll until the pending row is durably committed."""
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await service.get(rid)) is not None:
            return
    raise AssertionError(f"pending row {rid} never landed in DB")


def test_register_diag_router_refuses_empty_token(approval_service: ApprovalService) -> None:
    """Regression guard for #3790 follow-up security review:
    ``register_diag_router`` must refuse ``allow_subject=None`` /
    empty-string so the unauthenticated leak path is unreachable.

    A real misconfiguration where the env-var lookup returns None is
    cast through str here to keep register_diag_router's strict
    signature satisfied at type-check time. The runtime ``ValueError``
    is what we care about.
    """
    from typing import cast

    app = FastAPI()
    with pytest.raises(ValueError):
        register_diag_router(app, approval_service, allow_subject=cast(str, None))
    with pytest.raises(ValueError):
        register_diag_router(app, approval_service, allow_subject="")


@pytest.mark.asyncio
async def test_diag_dump_returns_pending_rows(approval_service: ApprovalService) -> None:
    tag = _tag()
    rid = f"req_d1_{tag}"
    zone = f"z_{tag}"
    subject = f"diag.example:443:{tag}"
    session_id = f"tok:s:{tag}"

    app = FastAPI()
    register_diag_router(app, approval_service, allow_subject="tok_test")

    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id=rid,
            zone_id=zone,
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            agent_id="ag",
            token_id="tok",
            session_id=session_id,
            reason="r",
            metadata={},
        )
    )
    try:
        await _wait_pending(approval_service, rid)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/hub/approvals/dump?zone_id={zone}",
                headers={"Authorization": "Bearer tok_test"},
            )
        assert r.status_code == 200
        payload = r.json()
        assert any(p["subject"] == subject for p in payload["pending"])
    finally:
        waiter.cancel()
        with contextlib.suppress(BaseException):
            await waiter
