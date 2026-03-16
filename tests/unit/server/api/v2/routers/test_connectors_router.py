"""Unit tests for connector discovery REST API (Issue #2069).

Tests the GET /api/v2/connectors and GET /api/v2/connectors/{name}/capabilities
endpoints using FastAPI TestClient with a mocked ConnectorRegistry.
"""

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.backends.base.registry import ConnectorInfo
from nexus.contracts.capabilities import ConnectorCapability
from nexus.server.api.v2.routers.connectors import router
from nexus.server.dependencies import require_auth

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------

_test_app = FastAPI()
_test_app.include_router(router)


_test_app.dependency_overrides[require_auth] = lambda: {"authenticated": True, "is_admin": True}
_client = TestClient(_test_app)


def _make_connector_info(
    name: str,
    description: str = "",
    category: str = "storage",
    capabilities: frozenset[ConnectorCapability] | None = None,
    user_scoped: bool = False,
) -> ConnectorInfo:
    """Create a minimal ConnectorInfo for testing."""
    mock_cls = MagicMock()
    mock_cls.CONNECTION_ARGS = {}
    return ConnectorInfo(
        name=name,
        connector_class=mock_cls,
        description=description,
        category=category,
        user_scoped=user_scoped,
        capabilities=capabilities or frozenset(),
    )


# ---------------------------------------------------------------------------
# GET /api/v2/connectors
# ---------------------------------------------------------------------------


class TestListConnectors:
    """GET /api/v2/connectors endpoint."""

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_returns_empty_list(self, mock_registry: MagicMock) -> None:
        mock_registry.list_all.return_value = []
        resp = _client.get("/api/v2/connectors")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connectors"] == []

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_returns_connectors_with_capabilities(self, mock_registry: MagicMock) -> None:
        mock_registry.list_all.return_value = [
            _make_connector_info(
                name="path_gcs",
                description="Google Cloud Storage",
                category="storage",
                capabilities=frozenset(
                    {ConnectorCapability.SIGNED_URL, ConnectorCapability.RENAME}
                ),
            ),
            _make_connector_info(
                name="gmail_connector",
                description="Gmail",
                category="api",
                capabilities=frozenset({ConnectorCapability.OAUTH}),
                user_scoped=True,
            ),
        ]
        resp = _client.get("/api/v2/connectors")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["connectors"]) == 2

        gcs = data["connectors"][0]
        assert gcs["name"] == "path_gcs"
        assert gcs["description"] == "Google Cloud Storage"
        assert gcs["category"] == "storage"
        assert set(gcs["capabilities"]) == {"signed_url", "rename"}
        assert gcs["user_scoped"] is False

        gmail = data["connectors"][1]
        assert gmail["name"] == "gmail_connector"
        assert gmail["user_scoped"] is True

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_capabilities_are_sorted(self, mock_registry: MagicMock) -> None:
        mock_registry.list_all.return_value = [
            _make_connector_info(
                name="test",
                capabilities=frozenset(
                    {
                        ConnectorCapability.STREAMING,
                        ConnectorCapability.BATCH_CONTENT,
                        ConnectorCapability.RENAME,
                    }
                ),
            ),
        ]
        resp = _client.get("/api/v2/connectors")
        caps = resp.json()["connectors"][0]["capabilities"]
        assert caps == sorted(caps)


# ---------------------------------------------------------------------------
# GET /api/v2/connectors/{name}/capabilities
# ---------------------------------------------------------------------------


class TestGetConnectorCapabilities:
    """GET /api/v2/connectors/{name}/capabilities endpoint."""

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_returns_capabilities(self, mock_registry: MagicMock) -> None:
        mock_registry.is_registered.return_value = True
        mock_registry.get_info.return_value = _make_connector_info(
            name="s3_connector",
            capabilities=frozenset(
                {ConnectorCapability.SIGNED_URL, ConnectorCapability.MULTIPART_UPLOAD}
            ),
        )
        resp = _client.get("/api/v2/connectors/s3_connector/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "s3_connector"
        assert set(data["capabilities"]) == {"signed_url", "multipart_upload"}

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_not_found_returns_404(self, mock_registry: MagicMock) -> None:
        mock_registry.is_registered.return_value = False
        resp = _client.get("/api/v2/connectors/nonexistent/capabilities")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    @patch("nexus.backends.base.registry.ConnectorRegistry")
    def test_empty_capabilities(self, mock_registry: MagicMock) -> None:
        mock_registry.is_registered.return_value = True
        mock_registry.get_info.return_value = _make_connector_info(
            name="basic_backend",
            capabilities=frozenset(),
        )
        resp = _client.get("/api/v2/connectors/basic_backend/capabilities")
        assert resp.status_code == 200
        assert resp.json()["capabilities"] == []
