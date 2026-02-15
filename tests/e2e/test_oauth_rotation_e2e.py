"""E2E test for OAuth token rotation + secrets audit pipeline.

Issue #997: Exercises the full lifecycle through FastAPI endpoints:
  create credential → use → expire → rotate → audit trail → reuse detection

Uses FastAPI TestClient with mock OAuth provider and real SQLite DB.
"""

from __future__ import annotations

import gc
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.core.exceptions import AuthenticationError
from nexus.server.auth.oauth_provider import OAuthCredential
from nexus.server.auth.token_manager import TokenManager, _hash_token
from nexus.storage.models._base import Base
from nexus.storage.models.refresh_token_history import RefreshTokenHistoryModel
from nexus.storage.secrets_audit_logger import SecretsAuditLogger

logger = logging.getLogger(__name__)


@pytest.fixture
def e2e_setup():
    """Set up real DB with TokenManager + SecretsAuditLogger."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    audit_logger = SecretsAuditLogger(session_factory=session_factory)
    manager = TokenManager(db_url=db_url, audit_logger=audit_logger)

    yield {
        "manager": manager,
        "audit_logger": audit_logger,
        "session_factory": session_factory,
        "engine": engine,
    }

    manager.close()
    engine.dispose()
    gc.collect()
    Path(db_path).unlink(missing_ok=True)


class TestOAuthRotationE2E:
    """Full lifecycle E2E test."""

    @pytest.mark.asyncio
    async def test_full_rotation_lifecycle(self, e2e_setup):
        """Test: create → use → expire → rotate → audit → reuse detection."""
        manager = e2e_setup["manager"]
        audit_logger = e2e_setup["audit_logger"]
        session_factory = e2e_setup["session_factory"]

        # === Step 1: Create credential ===
        initial_cred = OAuthCredential(
            access_token="ya29.initial_access",
            refresh_token="1//initial_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="alice@test.com",
            client_id="test_client",
            token_uri="https://oauth2.googleapis.com/token",
        )

        cred_id = await manager.store_credential(
            provider="google",
            user_email="alice@test.com",
            credential=initial_cred,
        )
        assert cred_id

        # Verify audit event was created
        events, _ = audit_logger.list_events_cursor(filters={"event_type": "credential_created"})
        assert len(events) == 1
        assert events[0].actor_id == "alice@test.com"

        # Get token family
        creds = await manager.list_credentials()
        family_id = creds[0]["token_family_id"]
        assert creds[0]["rotation_counter"] == 0

        # === Step 2: Use valid token (from cache) ===
        token1 = await manager.get_valid_token("google", "alice@test.com")
        assert token1 == "ya29.initial_access"

        token2 = await manager.get_valid_token("google", "alice@test.com")
        assert token2 == token1  # Should come from cache

        # === Step 3: Simulate expiry + register mock provider ===
        from nexus.storage.models import OAuthCredentialModel

        with session_factory() as session:
            model = session.execute(
                select(OAuthCredentialModel).where(
                    OAuthCredentialModel.user_email == "alice@test.com"
                )
            ).scalar_one()
            model.expires_at = datetime.now(UTC) - timedelta(hours=1)
            session.commit()

        # Clear cache to force DB re-read
        manager._token_cache.clear()

        # Mock provider that returns a NEW refresh token (rotation)
        mock_provider = AsyncMock()
        rotated_cred = OAuthCredential(
            access_token="ya29.rotated_access",
            refresh_token="1//ROTATED_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=rotated_cred)
        manager.register_provider("google", mock_provider)

        # === Step 4: Get token triggers rotation ===
        token3 = await manager.get_valid_token("google", "alice@test.com")
        assert token3 == "ya29.rotated_access"

        # Verify rotation counter incremented
        creds = await manager.list_credentials()
        assert creds[0]["rotation_counter"] == 1
        assert creds[0]["token_family_id"] == family_id  # Same family

        # Verify old refresh token is in history
        old_hash = _hash_token("1//initial_refresh")
        with session_factory() as session:
            history = session.execute(
                select(RefreshTokenHistoryModel).where(
                    RefreshTokenHistoryModel.refresh_token_hash == old_hash
                )
            ).scalar_one_or_none()
            assert history is not None
            assert history.token_family_id == family_id

        # Verify audit events
        rotation_events, _ = audit_logger.list_events_cursor(
            filters={"event_type": "token_rotated"}
        )
        assert len(rotation_events) >= 1

        refresh_events, _ = audit_logger.list_events_cursor(
            filters={"event_type": "token_refreshed"}
        )
        assert len(refresh_events) >= 1

        # === Step 5: Reuse detection ===
        assert manager.detect_reuse(family_id, old_hash) is True

        # Current token should NOT be detected as reuse
        current_hash = _hash_token("1//ROTATED_refresh")
        assert manager.detect_reuse(family_id, current_hash) is False

        # === Step 6: Invalidate family on reuse ===
        revoked = manager.invalidate_family(family_id)
        assert revoked == 1

        # Credential should be revoked
        creds = await manager.list_credentials()
        assert len(creds) == 0

        # Verify family_invalidated audit event
        invalidation_events, _ = audit_logger.list_events_cursor(
            filters={"event_type": "family_invalidated"}
        )
        assert len(invalidation_events) == 1

    @pytest.mark.asyncio
    async def test_audit_integrity_across_lifecycle(self, e2e_setup):
        """Verify all audit records have valid integrity hashes."""
        manager = e2e_setup["manager"]
        audit_logger = e2e_setup["audit_logger"]

        # Create and revoke a credential
        cred = OAuthCredential(
            access_token="ya29.test",
            refresh_token="1//test",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await manager.store_credential(
            provider="google",
            user_email="bob@test.com",
            credential=cred,
        )
        await manager.revoke_credential("google", "bob@test.com")

        # All audit records should have valid hashes
        events, _ = audit_logger.list_events_cursor(limit=100)
        assert len(events) >= 2  # credential_created + credential_revoked

        for event in events:
            assert audit_logger.verify_integrity_from_row(event) is True

    @pytest.mark.asyncio
    async def test_rate_limiting_prevents_rapid_rotation(self, e2e_setup):
        """Rate limiting should prevent rapid refresh attempts."""
        manager = e2e_setup["manager"]
        session_factory = e2e_setup["session_factory"]

        cred = OAuthCredential(
            access_token="ya29.rate_test",
            refresh_token="1//rate_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),  # Expired
        )
        await manager.store_credential(
            provider="google",
            user_email="charlie@test.com",
            credential=cred,
        )

        # Set last_refreshed_at to NOW
        from nexus.storage.models import OAuthCredentialModel

        with session_factory() as session:
            model = session.execute(
                select(OAuthCredentialModel).where(
                    OAuthCredentialModel.user_email == "charlie@test.com"
                )
            ).scalar_one()
            model.last_refreshed_at = datetime.now(UTC)
            session.commit()

        # Mock provider should NOT be called
        mock_provider = AsyncMock()
        mock_provider.refresh_token = AsyncMock()
        manager.register_provider("google", mock_provider)

        token = await manager.get_valid_token("google", "charlie@test.com")
        assert token is not None
        mock_provider.refresh_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuse_detection_during_rotation_e2e(self, e2e_setup):
        """Full lifecycle: pre-seeded history triggers reuse detection + audit event."""
        manager = e2e_setup["manager"]
        audit_logger = e2e_setup["audit_logger"]
        session_factory = e2e_setup["session_factory"]

        # Step 1: Create credential
        cred = OAuthCredential(
            access_token="ya29.victim_access",
            refresh_token="1//victim_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),  # Already expired
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="victim@test.com",
            client_id="test_client",
            token_uri="https://oauth2.googleapis.com/token",
        )
        await manager.store_credential(
            provider="google",
            user_email="victim@test.com",
            credential=cred,
        )

        # Step 2: Get family ID and pre-seed history (simulating prior rotation)
        from nexus.storage.models import OAuthCredentialModel

        with session_factory() as session:
            model = session.execute(
                select(OAuthCredentialModel).where(
                    OAuthCredentialModel.user_email == "victim@test.com"
                )
            ).scalar_one()
            family_id = model.token_family_id

            # Pre-insert the current refresh hash into history
            history_entry = RefreshTokenHistoryModel(
                token_family_id=family_id,
                credential_id=model.credential_id,
                refresh_token_hash=_hash_token("1//victim_refresh"),
                rotation_counter=0,
                zone_id="default",
                rotated_at=datetime.now(UTC),
            )
            session.add(history_entry)
            session.commit()

        # Step 3: Mock provider returning rotated token
        mock_provider = AsyncMock()
        attacker_cred = OAuthCredential(
            access_token="ya29.attacker_access",
            refresh_token="1//attacker_new_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=attacker_cred)
        manager.register_provider("google", mock_provider)

        # Step 4: Attempt should raise due to reuse detection
        with pytest.raises(AuthenticationError, match="reuse detected"):
            await manager.get_valid_token("google", "victim@test.com")

        # Step 5: Credential should be revoked
        creds = await manager.list_credentials()
        victim_creds = [c for c in creds if c.get("user_email") == "victim@test.com"]
        assert len(victim_creds) == 0

        # Step 6: Verify token_reuse_detected audit event
        reuse_events, _ = audit_logger.list_events_cursor(
            filters={"event_type": "token_reuse_detected"}
        )
        assert len(reuse_events) >= 1
        assert reuse_events[0].actor_id == "victim@test.com"


# ==========================================================================
# FastAPI TestClient e2e — secrets audit REST endpoints
# ==========================================================================


class _FakeNexusFS:
    """Minimal NexusFS stub for secrets audit endpoint testing."""

    def __init__(self, session_local: Any) -> None:
        self.SessionLocal = session_local


@pytest.fixture
def fastapi_e2e(monkeypatch):
    """FastAPI TestClient with real DB, admin API key, and secrets audit wired up."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)

    # Seed some audit events
    audit_logger = SecretsAuditLogger(session_factory=sf)
    record_id = audit_logger.log_event(
        event_type="credential_created",
        actor_id="alice@test.com",
        provider="google",
        credential_id="cred-001",
        zone_id="default",
    )
    audit_logger.log_event(
        event_type="token_rotated",
        actor_id="alice@test.com",
        provider="google",
        credential_id="cred-001",
        zone_id="default",
        details={"rotation_counter": 1},
    )

    fake_nfs = _FakeNexusFS(session_local=sf)
    from nexus.server import fastapi_server as fas

    # Save and restore module-level _fastapi_app to avoid cross-test pollution
    old_app = fas._fastapi_app
    monkeypatch.setattr(fas, "_fastapi_app", old_app)

    app = fas.create_app(fake_nfs, api_key="admin-key-997")  # type: ignore[arg-type]
    client = TestClient(app)

    yield {
        "client": client,
        "audit_logger": audit_logger,
        "record_id": record_id,
    }

    engine.dispose()
    gc.collect()
    Path(db_path).unlink(missing_ok=True)


class TestSecretsAuditRestE2E:
    """FastAPI TestClient e2e tests for /api/v2/secrets-audit/* endpoints."""

    def test_list_events_returns_audit_trail(self, fastapi_e2e):
        """Admin can list secrets audit events."""
        client = fastapi_e2e["client"]
        resp = client.get(
            "/api/v2/secrets-audit/events",
            headers={"Authorization": "Bearer admin-key-997"},
        )
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert len(body["events"]) == 2
        assert body["events"][0]["event_type"] in ("credential_created", "token_rotated")
        logger.info("List events OK: %d events", len(body["events"]))

    def test_list_events_filter_by_event_type(self, fastapi_e2e):
        """Filter events by event_type."""
        client = fastapi_e2e["client"]
        resp = client.get(
            "/api/v2/secrets-audit/events?event_type=token_rotated",
            headers={"Authorization": "Bearer admin-key-997"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["events"]) == 1
        assert body["events"][0]["event_type"] == "token_rotated"

    def test_get_single_event(self, fastapi_e2e):
        """Get a single audit event by ID."""
        client = fastapi_e2e["client"]
        record_id = fastapi_e2e["record_id"]
        resp = client.get(
            f"/api/v2/secrets-audit/events/{record_id}",
            headers={"Authorization": "Bearer admin-key-997"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == record_id
        assert body["event_type"] == "credential_created"

    def test_integrity_verification(self, fastapi_e2e):
        """Verify integrity hash for a record."""
        client = fastapi_e2e["client"]
        record_id = fastapi_e2e["record_id"]
        resp = client.get(
            f"/api/v2/secrets-audit/integrity/{record_id}",
            headers={"Authorization": "Bearer admin-key-997"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_valid"] is True
        assert body["record_id"] == record_id

    def test_export_json(self, fastapi_e2e):
        """Export audit events as JSON."""
        client = fastapi_e2e["client"]
        resp = client.get(
            "/api/v2/secrets-audit/events/export?format=json",
            headers={"Authorization": "Bearer admin-key-997"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        import json

        body = json.loads(resp.text)
        assert len(body["events"]) == 2

    def test_export_csv(self, fastapi_e2e):
        """Export audit events as CSV."""
        client = fastapi_e2e["client"]
        resp = client.get(
            "/api/v2/secrets-audit/events/export?format=csv",
            headers={"Authorization": "Bearer admin-key-997"},
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        lines = resp.text.strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows

    def test_unauthenticated_rejected(self, fastapi_e2e):
        """Unauthenticated request gets 401."""
        client = fastapi_e2e["client"]
        resp = client.get("/api/v2/secrets-audit/events")
        assert resp.status_code == 401

    def test_non_admin_rejected(self, fastapi_e2e, monkeypatch):
        """Non-admin authenticated user gets 403."""
        from nexus.server import fastapi_server as fas

        mock_auth = MagicMock()

        async def mock_authenticate(token: str) -> Any:
            if token == "user-token":
                result = MagicMock()
                result.authenticated = True
                result.is_admin = False
                result.subject_type = "user"
                result.subject_id = "regular-user"
                result.zone_id = "default"
                result.inherit_permissions = True
                result.metadata = {}
                return result
            return None

        mock_auth.authenticate = mock_authenticate

        fake_nfs = _FakeNexusFS(session_local=fastapi_e2e["audit_logger"]._session_factory)

        old_app = fas._fastapi_app
        monkeypatch.setattr(fas, "_fastapi_app", old_app)

        app = fas.create_app(fake_nfs, auth_provider=mock_auth)  # type: ignore[arg-type]
        client = TestClient(app)

        resp = client.get(
            "/api/v2/secrets-audit/events",
            headers={"Authorization": "Bearer user-token"},
        )
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    def test_nonexistent_event_returns_404(self, fastapi_e2e):
        """Get nonexistent event returns 404."""
        client = fastapi_e2e["client"]
        resp = client.get(
            "/api/v2/secrets-audit/events/nonexistent-id",
            headers={"Authorization": "Bearer admin-key-997"},
        )
        assert resp.status_code == 404
