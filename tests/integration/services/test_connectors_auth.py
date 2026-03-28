"""Unit tests for connector auth REST API (Issue #3182).

Tests the POST /api/v2/connectors/auth/init and
GET /api/v2/connectors/auth/status endpoints using FastAPI TestClient
with a mocked ConnectorRegistry.
"""

import time
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.connectors import _pending_auth, router
from nexus.server.dependencies import require_auth

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------

_test_app = FastAPI()
_test_app.include_router(router)

_test_app.dependency_overrides[require_auth] = lambda: {"authenticated": True, "is_admin": True}
_client = TestClient(_test_app)


def _setup_app_state() -> None:
    """Wire up mock nexus_fs and auth_service on the test app."""
    nx = MagicMock()
    mount_service = MagicMock()
    mount_service.list_mounts = AsyncMock(return_value=[])
    nx.service.side_effect = lambda name: mount_service if name == "mount" else None
    nx.configs_dir = None

    auth_service = MagicMock()
    auth_service.get_connector_auth_state = AsyncMock(
        return_value={"auth_status": "unknown", "auth_source": None}
    )

    _test_app.state.nexus_fs = nx
    _test_app.state.auth_service = auth_service


# ---------------------------------------------------------------------------
# POST /api/v2/connectors/auth/init
# ---------------------------------------------------------------------------

_OAUTH_YAML_CONTENT = {
    "redirect_uri": "http://localhost:5173/oauth/callback",
    "providers": {
        "gmail": {
            "provider_class": "GoogleOAuthProvider",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "scopes": ["https://mail.google.com/"],
        },
        "slack": {
            "provider_class": "SlackOAuthProvider",
            "client_id_env": "SLACK_CLIENT_ID",
            "scopes": ["chat:write", "channels:read"],
        },
    },
}


def _make_connector_info(name: str, service_name: str | None = "UNSET") -> MagicMock:
    """Create a minimal mock ConnectorInfo with a service_name.

    Pass ``service_name=None`` to simulate a connector with no OAuth provider.
    When omitted, service_name defaults to *name*.
    """
    info = MagicMock()
    info.name = name
    info.service_name = name if service_name == "UNSET" else service_name
    return info


class TestAuthInit:
    """POST /api/v2/connectors/auth/init endpoint."""

    def setup_method(self) -> None:
        _pending_auth.clear()
        _setup_app_state()

    @patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "test-client-id-123"})
    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_success_returns_auth_url(self, mock_registry: MagicMock) -> None:
        mock_registry.is_registered.return_value = True
        mock_registry.get_info.return_value = _make_connector_info(
            "gmail_connector", service_name="gmail"
        )

        with (
            patch("builtins.open", mock_open(read_data="")),
            patch("yaml.safe_load", return_value=_OAUTH_YAML_CONTENT),
            patch("os.path.exists", return_value=True),
        ):
            resp = _client.post(
                "/api/v2/connectors/auth/init",
                json={"connector_name": "gmail_connector"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "auth_url" in data
        assert "accounts.google.com" in data["auth_url"]
        assert "state_token" in data
        assert data["provider"] == "gmail"
        assert data["expires_in"] == 300

        # Verify pending auth state was stored
        assert data["state_token"] in _pending_auth
        entry = _pending_auth[data["state_token"]]
        assert entry["connector_name"] == "gmail_connector"
        assert entry["status"] == "pending"

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_unknown_connector_returns_404(self, mock_registry: MagicMock) -> None:
        mock_registry.is_registered.return_value = False

        resp = _client.post(
            "/api/v2/connectors/auth/init",
            json={"connector_name": "nonexistent_connector"},
        )

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_missing_provider_returns_400(self, mock_registry: MagicMock) -> None:
        mock_registry.is_registered.return_value = True
        mock_registry.get_info.return_value = _make_connector_info(
            "bare_connector", service_name=None
        )
        # service_name is None and no provider override supplied
        # The endpoint resolves provider = req.provider or info.service_name
        # When both are None/falsy, it raises 400.

        resp = _client.post(
            "/api/v2/connectors/auth/init",
            json={"connector_name": "bare_connector"},
        )

        assert resp.status_code == 400
        assert "No OAuth provider" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/v2/connectors/auth/status
# ---------------------------------------------------------------------------


class TestAuthStatus:
    """GET /api/v2/connectors/auth/status endpoint."""

    def setup_method(self) -> None:
        _pending_auth.clear()
        _setup_app_state()

    def test_pending_status(self) -> None:
        """Token exists and auth has not yet completed."""
        _pending_auth["tok-pending"] = {
            "connector_name": "gmail_connector",
            "provider": "gmail",
            "created_at": time.time(),
            "status": "pending",
            "baseline_auth_status": "unknown",
        }

        # auth_service returns "unknown" (not yet authed) — default from _setup_app_state
        resp = _client.get("/api/v2/connectors/auth/status", params={"state_token": "tok-pending"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["connector_name"] == "gmail_connector"

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_completed_status(self, mock_registry: MagicMock) -> None:
        """Auth service reports 'authed' — endpoint returns 'completed'."""
        _pending_auth["tok-done"] = {
            "connector_name": "gmail_connector",
            "provider": "gmail",
            "created_at": time.time(),
            "status": "pending",
            "baseline_auth_status": "unknown",
        }

        mock_registry.get_info.return_value = _make_connector_info(
            "gmail_connector", service_name="gmail"
        )

        # Override auth_service to report successful auth
        auth_service = MagicMock()
        auth_service.get_connector_auth_state = AsyncMock(
            return_value={"auth_status": "authed", "auth_source": "stored:secret"}
        )
        _test_app.state.auth_service = auth_service

        resp = _client.get("/api/v2/connectors/auth/status", params={"state_token": "tok-done"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["connector_name"] == "gmail_connector"
        # Token should be cleaned up after completion
        assert "tok-done" not in _pending_auth

    def test_expired_status(self) -> None:
        """Token created more than 5 minutes ago returns 'expired'."""
        _pending_auth["tok-old"] = {
            "connector_name": "gmail_connector",
            "provider": "gmail",
            "created_at": time.time() - 301,  # 301 seconds ago (>300s TTL)
            "status": "pending",
            "baseline_auth_status": "unknown",
        }

        resp = _client.get("/api/v2/connectors/auth/status", params={"state_token": "tok-old"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "expired"
        assert data["connector_name"] == "gmail_connector"
        assert "expired" in data["message"].lower()
        # Token should be cleaned up after expiration
        assert "tok-old" not in _pending_auth

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_preexisting_auth_stays_pending(self, mock_registry: MagicMock) -> None:
        """When baseline was already 'authed', current 'authed' stays pending.

        This prevents pre-existing auth from causing a false 'completed' report,
        which is the core correctness fix for concurrent auth attempts.
        """
        _pending_auth["tok-preauth"] = {
            "connector_name": "gmail_connector",
            "provider": "gmail",
            "created_at": time.time(),
            "status": "pending",
            "baseline_auth_status": "authed",  # was already authed at init
        }

        mock_registry.get_info.return_value = _make_connector_info(
            "gmail_connector", service_name="gmail"
        )

        # Auth service still reports "authed" — same as baseline
        auth_service = MagicMock()
        auth_service.get_connector_auth_state = AsyncMock(
            return_value={"auth_status": "authed", "auth_source": "stored:secret"}
        )
        _test_app.state.auth_service = auth_service

        resp = _client.get("/api/v2/connectors/auth/status", params={"state_token": "tok-preauth"})

        assert resp.status_code == 200
        data = resp.json()
        # Should remain pending because auth state did NOT change
        assert data["status"] == "pending"
        # Token should still be in pending auth (not cleaned up)
        assert "tok-preauth" in _pending_auth

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_concurrent_auth_only_first_completes(self, mock_registry: MagicMock) -> None:
        """Two pending tokens for the same connector — only the first poll wins.

        When one token detects completion, all other pending tokens for the
        same connector are invalidated. Subsequent polls get 404.
        """
        # Two concurrent auth flows for gmail
        _pending_auth["tok-A"] = {
            "connector_name": "gmail_connector",
            "provider": "gmail",
            "created_at": time.time(),
            "status": "pending",
            "baseline_auth_status": "unknown",
        }
        _pending_auth["tok-B"] = {
            "connector_name": "gmail_connector",
            "provider": "gmail",
            "created_at": time.time(),
            "status": "pending",
            "baseline_auth_status": "unknown",
        }

        mock_registry.get_info.return_value = _make_connector_info(
            "gmail_connector", service_name="gmail"
        )

        auth_service = MagicMock()
        auth_service.get_connector_auth_state = AsyncMock(
            return_value={"auth_status": "authed", "auth_source": "oauth"}
        )
        _test_app.state.auth_service = auth_service

        # First poll (tok-A) claims completion
        resp_a = _client.get("/api/v2/connectors/auth/status", params={"state_token": "tok-A"})
        assert resp_a.status_code == 200
        assert resp_a.json()["status"] == "completed"

        # Both tokens should be invalidated
        assert "tok-A" not in _pending_auth
        assert "tok-B" not in _pending_auth

        # Second poll (tok-B) gets 404 — not a false "completed"
        resp_b = _client.get("/api/v2/connectors/auth/status", params={"state_token": "tok-B"})
        assert resp_b.status_code == 404

    def test_unknown_token_returns_404(self) -> None:
        """Completely unknown token returns 404."""
        resp = _client.get(
            "/api/v2/connectors/auth/status",
            params={"state_token": "tok-does-not-exist"},
        )

        assert resp.status_code == 404
        assert "Unknown or expired" in resp.json()["detail"]
