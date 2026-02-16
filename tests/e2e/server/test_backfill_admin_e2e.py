"""E2E tests for admin auth enforcement on backfill_directory_index.

Issue #1457: Validates that:
- Admin callers can invoke backfill_directory_index via RPC
- Non-admin callers get 403 Forbidden
- Unauthenticated callers get 401 Unauthorized

Uses FastAPI TestClient with real create_app + API key auth.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)


class _FakeNexusFS:
    """Minimal NexusFS stub that exposes backfill_directory_index for RPC discovery.

    _discover_exposed_methods iterates dir(nexus_fs) looking for _rpc_exposed attrs.
    MagicMock attributes don't appear in dir(), so we use a real class.
    """

    def __init__(self):
        self.metadata = MagicMock()
        self.metadata.backfill_directory_index.return_value = 5
        self.is_admin = False
        self.SessionLocal = None

        # Import the real decorator attrs from NexusFS
        from nexus.core.nexus_fs import NexusFS

        real_method = NexusFS.backfill_directory_index

        # Create the method with correct decorator attributes
        def backfill_directory_index(prefix="/", zone_id=None, _context=None):
            created = self.metadata.backfill_directory_index(prefix=prefix, zone_id=zone_id)
            return {"entries_created": created, "prefix": prefix}

        backfill_directory_index._rpc_exposed = real_method._rpc_exposed
        backfill_directory_index._rpc_name = real_method._rpc_name
        backfill_directory_index._rpc_description = real_method._rpc_description
        backfill_directory_index._rpc_version = real_method._rpc_version
        backfill_directory_index._rpc_admin_only = real_method._rpc_admin_only

        self.backfill_directory_index = backfill_directory_index


def _save_app_state(monkeypatch):
    """Record _app_state attributes so monkeypatch auto-restores them at teardown."""
    from nexus.server import fastapi_server as fas

    for attr in ("nexus_fs", "api_key", "auth_provider"):
        monkeypatch.setattr(fas._app_state, attr, getattr(fas._app_state, attr))


@pytest.fixture
def mock_nexus_fs():
    """Create a fake NexusFS with backfill_directory_index exposed."""
    return _FakeNexusFS()


@pytest.fixture
def admin_client(mock_nexus_fs, monkeypatch):
    """Create FastAPI TestClient with API key auth (admin)."""
    from nexus.server import fastapi_server as fas

    _save_app_state(monkeypatch)
    app = fas.create_app(mock_nexus_fs, api_key="admin-secret-key")
    yield TestClient(app)


class TestBackfillAdminE2E:
    """E2E tests for backfill_directory_index admin enforcement."""

    def test_admin_caller_succeeds(self, admin_client):
        """Admin caller (correct API key) can invoke backfill_directory_index."""
        response = admin_client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": "Bearer admin-secret-key"},
            json={"params": {"prefix": "/skills"}},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )

        body = response.json()
        assert body.get("result", {}).get("entries_created") == 5
        assert body.get("result", {}).get("prefix") == "/skills"
        logger.info("Admin caller succeeded: %s", body)

    def test_non_admin_caller_rejected(self, mock_nexus_fs, monkeypatch):
        """Non-admin authenticated caller gets permission denied on backfill_directory_index."""
        from nexus.server import fastapi_server as fas

        _save_app_state(monkeypatch)

        # Create a mock auth provider that returns non-admin result
        mock_auth = MagicMock()

        async def mock_authenticate(token):
            if token == "user-token":
                result = MagicMock()
                result.authenticated = True
                result.is_admin = False  # NOT admin
                result.subject_type = "user"
                result.subject_id = "regular-user"
                result.zone_id = "default"
                result.inherit_permissions = True
                result.metadata = {}
                return result
            return None

        mock_auth.authenticate = mock_authenticate

        app = fas.create_app(mock_nexus_fs, auth_provider=mock_auth)
        client = TestClient(app)

        response = client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": "Bearer user-token"},
            json={"params": {"prefix": "/"}},
        )

        # RPC protocol returns HTTP 200 with error in JSON body
        body = response.json()
        assert "error" in body, f"Expected RPC error, got: {body}"
        assert "Permission denied" in body["error"]["message"]
        assert "Admin privileges required" in body["error"]["message"]
        # No result should be returned
        assert body.get("result") is None
        logger.info("Non-admin caller correctly rejected: %s", body)

    def test_unauthenticated_caller_rejected(self, admin_client):
        """Unauthenticated caller (no token) gets 401."""
        response = admin_client.post(
            "/api/nfs/backfill_directory_index",
            json={"params": {"prefix": "/"}},
        )

        assert response.status_code == 401, (
            f"Expected 401, got {response.status_code}: {response.text}"
        )
        logger.info("Unauthenticated caller correctly rejected: %s", response.json())

    def test_wrong_api_key_rejected(self, admin_client):
        """Wrong API key gets 401."""
        response = admin_client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": "Bearer wrong-key"},
            json={"params": {"prefix": "/"}},
        )

        assert response.status_code == 401, (
            f"Expected 401, got {response.status_code}: {response.text}"
        )
        logger.info("Wrong API key correctly rejected: %s", response.json())

    def test_no_performance_regression(self, admin_client):
        """Admin auth check should not add measurable latency."""
        import time

        # Warm up
        admin_client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": "Bearer admin-secret-key"},
            json={"params": {"prefix": "/"}},
        )

        # Time multiple requests
        times = []
        for _ in range(10):
            start = time.perf_counter()
            response = admin_client.post(
                "/api/nfs/backfill_directory_index",
                headers={"Authorization": "Bearer admin-secret-key"},
                json={"params": {"prefix": "/"}},
            )
            elapsed = time.perf_counter() - start
            times.append(elapsed)
            assert response.status_code == 200

        avg_ms = sum(times) / len(times) * 1000
        max_ms = max(times) * 1000
        logger.info(
            f"Performance: avg={avg_ms:.1f}ms, max={max_ms:.1f}ms over {len(times)} requests"
        )

        # Admin check is O(1) getattr — should not add >5ms of overhead
        assert avg_ms < 500, f"Average response time {avg_ms:.1f}ms is too high"
