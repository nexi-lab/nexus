"""Minimal FastAPI test server for TypeScript SDK E2E tests.

Starts the actual pay router with:
- Static API key authentication (require_auth enabled)
- CreditsService in disabled mode (unlimited credits, no TigerBeetle needed)
- X402Client disabled (no blockchain needed)

Usage:
    python tests/e2e-server.py
    # Server runs on http://localhost:4219 with API key "sk-e2e-test-key"
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import uvicorn
from fastapi import FastAPI

# Import the actual pay router and exception handlers
from nexus.pay.credits import CreditsService
from nexus.pay.sdk import NexusPay
from nexus.server.api.v2.routers.pay import (
    _register_pay_exception_handlers,
    get_nexuspay,
)
from nexus.server.api.v2.routers.pay import (
    router as pay_router,
)

# Static API key for E2E tests
API_KEY = "sk-e2e-test-key"
PORT = 4219


def create_e2e_app() -> FastAPI:
    """Create a minimal FastAPI app with real pay router + auth."""
    app = FastAPI(title="NexusPay E2E Test Server")

    # Mount the real pay router (includes all 8 endpoints)
    app.include_router(pay_router)
    _register_pay_exception_handlers(app)

    # --- Mock CreditsService (disabled mode behavior) ---
    credits_service = AsyncMock(spec=CreditsService)
    credits_service.get_balance = AsyncMock(return_value=Decimal("100.000000"))
    credits_service.get_balance_with_reserved = AsyncMock(
        return_value=(Decimal("100.000000"), Decimal("5.000000"))
    )
    credits_service.check_budget = AsyncMock(return_value=True)
    credits_service.transfer = AsyncMock(return_value="tx-e2e-ts-001")
    credits_service.transfer_batch = AsyncMock(return_value=["tx-batch-001", "tx-batch-002"])
    credits_service.reserve = AsyncMock(return_value="res-e2e-ts-001")
    credits_service.commit_reservation = AsyncMock()
    credits_service.release_reservation = AsyncMock()
    credits_service.deduct_fast = AsyncMock(return_value=True)

    app.state.credits_service = credits_service
    app.state.x402_client = None  # No x402 for E2E tests

    # --- Auth: override get_nexuspay to enforce static API key ---
    from fastapi import Depends, Header, HTTPException

    async def e2e_require_auth(
        authorization: str | None = Header(None, alias="Authorization"),
        x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
    ) -> dict:
        """Real auth check: validates Bearer token against static API key."""
        if not authorization:
            raise HTTPException(status_code=401, detail="Missing Authorization header")

        token = ""
        if authorization.startswith("Bearer "):
            token = authorization[7:]
        elif authorization.startswith("sk-"):
            token = authorization
        else:
            raise HTTPException(status_code=401, detail="Invalid Authorization format")

        if token != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")

        return {
            "authenticated": True,
            "is_admin": True,
            "subject_type": "user",
            "subject_id": "e2e-agent",
            "zone_id": "default",
            "x_agent_id": x_agent_id or "e2e-agent",
        }

    async def e2e_get_nexuspay(
        auth_result: dict = Depends(e2e_require_auth),
    ) -> NexusPay:
        """Create NexusPay SDK instance from auth context — same as production."""
        agent_id = auth_result.get("x_agent_id") or auth_result.get("subject_id", "anonymous")
        zone_id = auth_result.get("zone_id", "default")

        return NexusPay(
            api_key=f"nx_live_{agent_id}",
            credits_service=credits_service,
            x402_client=None,
            x402_enabled=False,
            zone_id=zone_id,
        )

    # Override the dependency to use our auth
    app.dependency_overrides[get_nexuspay] = e2e_get_nexuspay

    # Health endpoint for readiness check
    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "auth": "static", "api_key_required": True}

    return app


if __name__ == "__main__":
    app = create_e2e_app()
    print(f"E2E test server starting on http://localhost:{PORT}")
    print(f"API Key: {API_KEY}")
    print(f"Auth: Bearer {API_KEY}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
