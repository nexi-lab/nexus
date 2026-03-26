"""E2E auth enforcement tests for Issues #2048 + #2136.

Validates that ALL newly-protected endpoints reject unauthenticated requests
when api_key is configured. Uses create_app() with static API key (no database
auth needed) to exercise the full auth middleware chain.

Covers:
- governance (require_admin) → 401 without auth
- mobile_search (require_auth) → 401 without auth
- tus_uploads (require_auth) → 401 for PATCH/POST/DELETE, OPTIONS stays public
- x402 topup/config (require_auth) → 401, webhook stays public
- RPC dispatch method name validation (#2136)
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers import mobile_search
from nexus.server.api.v2.routers.tus_uploads import create_tus_uploads_router
from nexus.server.api.v2.routers.x402 import router as x402_router
from nexus.server.api.v2.routers.x402 import webhook_router as x402_webhook_router

# ---------------------------------------------------------------------------
# Fixture: app with static API key auth (no auth overrides!)
# ---------------------------------------------------------------------------


@pytest.fixture
def secured_app() -> FastAPI:
    """FastAPI app with api_key set — triggers real auth checks."""
    app = FastAPI()

    # Register all routers we protect (governance deleted — gRPC-only #1529)
    app.include_router(mobile_search.router)

    tus_public, tus_auth = create_tus_uploads_router(get_upload_service=lambda: MagicMock())
    app.include_router(tus_public, prefix="/api/v2/uploads")
    app.include_router(tus_auth, prefix="/api/v2/uploads")

    app.include_router(x402_webhook_router, prefix="/api/v2")
    app.include_router(x402_router, prefix="/api/v2")

    # Critical: set api_key to trigger real auth checks
    app.state.api_key = "test-secret-key-e2e"
    app.state.auth_provider = None

    # Stubs for endpoints that check app.state
    app.state.nexus_fs = MagicMock()
    app.state.x402_client = MagicMock()
    app.state.credits_service = MagicMock()
    app.state.brick_container = MagicMock()

    return app


@pytest.fixture
def unauthed(secured_app: FastAPI) -> TestClient:
    """Client without auth headers."""
    return TestClient(secured_app)


@pytest.fixture
def authed(secured_app: FastAPI) -> TestClient:
    """Client with valid API key (no server exception propagation)."""
    return TestClient(
        secured_app,
        headers={"Authorization": "Bearer test-secret-key-e2e"},
        raise_server_exceptions=False,
    )


# ===========================================================================
# Governance — require_admin
# ===========================================================================


# ===========================================================================
# Mobile Search — require_auth
# ===========================================================================


class TestMobileSearchAuthEnforcement:
    """mobile_search endpoints must reject unauthenticated requests."""

    def test_detect_returns_401(self, unauthed: TestClient) -> None:
        resp = unauthed.get("/api/v2/mobile/detect")
        assert resp.status_code == 401

    def test_download_returns_401(self, unauthed: TestClient) -> None:
        resp = unauthed.post(
            "/api/v2/mobile/download",
            json={"model_name": "test"},
        )
        assert resp.status_code == 401


# ===========================================================================
# TUS Uploads — require_auth (OPTIONS is public)
# ===========================================================================


class TestTusUploadsAuthEnforcement:
    """tus upload endpoints must reject unauthenticated; OPTIONS stays open."""

    def test_create_upload_returns_401(self, unauthed: TestClient) -> None:
        resp = unauthed.post(
            "/api/v2/uploads",
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": "100"},
        )
        assert resp.status_code == 401

    def test_options_stays_public(self, unauthed: TestClient) -> None:
        resp = unauthed.options("/api/v2/uploads")
        assert resp.status_code != 401, "OPTIONS should not require auth"


# ===========================================================================
# x402 — require_auth for topup/config, webhook stays public
# ===========================================================================


class TestX402AuthEnforcement:
    """x402 topup/config require auth; webhook is public."""

    def test_topup_returns_401(self, unauthed: TestClient) -> None:
        resp = unauthed.post(
            "/api/v2/x402/topup",
            json={"agent_id": "test", "amount": "10.00"},
        )
        assert resp.status_code == 401

    def test_config_returns_401(self, unauthed: TestClient) -> None:
        resp = unauthed.get("/api/v2/x402/config")
        assert resp.status_code == 401

    def test_webhook_stays_public(self, unauthed: TestClient) -> None:
        resp = unauthed.post(
            "/api/v2/x402/webhook",
            json={"event": "test"},
        )
        # Should NOT be 401 (webhook is public; may be 400/422 for bad payload)
        assert resp.status_code != 401, "webhook should not require auth"


# ===========================================================================
# Authenticated requests should pass auth checks
# ===========================================================================


class TestAuthenticatedRequestsPass:
    """Verify that authenticated requests are not blocked by auth."""

    def test_x402_config_with_auth_passes(self, authed: TestClient) -> None:
        resp = authed.get("/api/v2/x402/config")
        assert resp.status_code != 401

    def test_tus_create_with_auth_passes(self, authed: TestClient) -> None:
        resp = authed.post(
            "/api/v2/uploads",
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": "100"},
        )
        assert resp.status_code != 401


# ===========================================================================
# RPC dispatch method name validation (#2136)
# ===========================================================================


class TestRPCDispatchSecurity:
    """Verify dispatch_method() blocks private/malformed method names."""

    @pytest.mark.asyncio
    async def test_private_method_blocked(self) -> None:
        from nexus.server.rpc.dispatch import dispatch_method

        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "_private",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=MagicMock(),
                exposed_methods={"read": MagicMock()},
            )

    @pytest.mark.asyncio
    async def test_dot_method_blocked(self) -> None:
        from nexus.server.rpc.dispatch import dispatch_method

        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "os.system",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=MagicMock(),
                exposed_methods={"read": MagicMock()},
            )

    @pytest.mark.asyncio
    async def test_empty_method_blocked(self) -> None:
        from nexus.server.rpc.dispatch import dispatch_method

        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=MagicMock(),
                exposed_methods={"read": MagicMock()},
            )

    @pytest.mark.asyncio
    async def test_error_does_not_echo_method_name(self) -> None:
        from nexus.server.rpc.dispatch import dispatch_method

        with pytest.raises(ValueError) as exc_info:
            await dispatch_method(
                "_secret_internal",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=MagicMock(),
                exposed_methods={"read": MagicMock()},
            )
        assert "_secret_internal" not in str(exc_info.value)
