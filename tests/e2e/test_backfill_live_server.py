"""Live server E2E test for admin auth on backfill_directory_index.

Issue #1457: Validates the full stack with a real server:
- Creates a real NexusFS with SQLite backend
- Sets up DatabaseAPIKeyAuth with admin and non-admin keys
- Starts FastAPI with permissions enabled
- Tests all auth scenarios via HTTP

Usage:
    .venv/bin/python -m pytest tests/e2e/test_backfill_live_server.py -v --tb=short -p no:xdist -o "addopts=" --log-cli-level=INFO
"""

from __future__ import annotations

import logging
import os

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)


def _save_app_state(monkeypatch):
    """Record _app_state attributes so monkeypatch auto-restores them at teardown."""
    from nexus.server import fastapi_server as fas

    for attr in ("nexus_fs", "api_key", "auth_provider"):
        monkeypatch.setattr(fas._app_state, attr, getattr(fas._app_state, attr))


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """Start a real Nexus server with database auth and permissions enabled.

    Creates:
    - Real NexusFS with SQLite backend in tmp_path
    - DatabaseAPIKeyAuth with admin key + non-admin user key
    - FastAPI app with full auth pipeline

    Yields:
        tuple: (TestClient, admin_key, user_key)
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.server import fastapi_server as fas
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import Base

    _save_app_state(monkeypatch)

    # --- Setup real database ---
    db_path = tmp_path / "nexus.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    # --- Create API keys ---
    with session_factory() as session:
        _admin_id, admin_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="admin-user",
            name="Admin Key",
            is_admin=True,
        )
        _user_id, user_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="regular-user",
            name="User Key",
            is_admin=False,
        )
        session.commit()

    logger.info("Created admin key: %s...", admin_key[:12])
    logger.info("Created user key: %s...", user_key[:12])

    # --- Create auth provider ---
    auth_provider = DatabaseAPIKeyAuth(session_factory)

    # --- Create real NexusFS via embedded connect ---
    import nexus

    data_dir = str(tmp_path / "nexus-data")
    os.makedirs(data_dir, exist_ok=True)
    nexus_fs = nexus.connect(
        config={
            "data_dir": data_dir,
            "enforce_permissions": True,
        }
    )

    # --- Create FastAPI app ---
    app = fas.create_app(
        nexus_fs,
        auth_provider=auth_provider,
        database_url=f"sqlite:///{db_path}",
    )

    # Reset auth cache for clean test
    from nexus.server.dependencies import _reset_auth_cache

    _reset_auth_cache()

    client = TestClient(app)
    logger.info("Live server started with permissions enabled")

    yield client, admin_key, user_key


class TestBackfillLiveServer:
    """E2E tests with real server, real auth, real permissions."""

    def test_admin_key_passes_auth_guard(self, live_server):
        """Admin API key holder passes the admin auth guard.

        Note: The method may fail with an internal error if the metadata
        backend doesn't support backfill_directory_index (e.g., RaftMetadataStore
        in embedded mode). The key assertion is that it does NOT get a
        permission error — the auth guard lets it through.
        """
        client, admin_key, _ = live_server

        response = client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"params": {"prefix": "/"}},
        )

        body = response.json()
        logger.info("Admin response: %s", body)

        # Admin must NOT get a permission error
        if "error" in body:
            assert "Admin privileges required" not in body["error"].get("message", ""), (
                "Admin caller should not be rejected by admin guard"
            )
            assert "Permission denied" not in body["error"].get("message", ""), (
                "Admin caller should not get permission denied"
            )
            # It's OK if backend doesn't support the operation
            logger.info("PASS: Admin passed auth guard (method may not be supported by backend)")
        else:
            assert "result" in body
            logger.info("PASS: Admin key backfill fully succeeded")

    def test_user_key_cannot_backfill(self, live_server):
        """Non-admin API key holder is blocked from backfill_directory_index."""
        client, _, user_key = live_server

        response = client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"params": {"prefix": "/"}},
        )

        body = response.json()
        logger.info("User response: %s", body)

        # Should be rejected with permission error in RPC response
        assert "error" in body, f"Expected RPC error, got: {body}"
        assert "Admin privileges required" in body["error"]["message"], (
            f"Expected admin rejection, got: {body['error']['message']}"
        )
        logger.info("PASS: Non-admin key correctly rejected")

    def test_no_key_gets_401(self, live_server):
        """No auth header returns 401."""
        client, _, _ = live_server

        response = client.post(
            "/api/nfs/backfill_directory_index",
            json={"params": {"prefix": "/"}},
        )

        assert response.status_code == 401
        logger.info("PASS: No auth correctly returns 401")

    def test_invalid_key_gets_401(self, live_server):
        """Invalid API key returns 401."""
        client, _, _ = live_server

        response = client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": "Bearer sk-invalid-key-12345"},
            json={"params": {"prefix": "/"}},
        )

        assert response.status_code == 401
        logger.info("PASS: Invalid key correctly returns 401")

    def test_admin_can_still_do_normal_ops(self, live_server):
        """Admin key can still do normal operations (not broken by admin_only guard)."""
        client, admin_key, _ = live_server

        # Check exists — a simple, safe operation
        response = client.post(
            "/api/nfs/exists",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"params": {"path": "/test-file.txt"}},
        )
        body = response.json()
        logger.info("Exists response: %s", body)
        # Should not get admin error — exists is not admin_only
        if "error" in body:
            assert "Admin privileges required" not in body["error"].get("message", "")

    def test_user_can_still_do_normal_ops(self, live_server):
        """Non-admin key can still do normal operations (regression check)."""
        client, _, user_key = live_server

        # Exists check should work for non-admin
        response = client.post(
            "/api/nfs/exists",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"params": {"path": "/nonexistent.txt"}},
        )
        body = response.json()
        logger.info("Exists response: %s", body)
        # Should not get admin error — exists is not admin_only
        if "error" in body:
            assert "Admin privileges required" not in body["error"].get("message", "")

    def test_non_admin_backfill_with_prefix_rejected(self, live_server):
        """Non-admin user is rejected even with specific prefix args."""
        client, _, user_key = live_server

        response = client.post(
            "/api/nfs/backfill_directory_index",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"params": {"prefix": "/skills", "zone_id": "default"}},
        )

        body = response.json()
        logger.info("Non-admin prefixed backfill response: %s", body)
        assert "error" in body
        assert "Admin privileges required" in body["error"]["message"]
        logger.info("PASS: Non-admin correctly rejected with prefix args")
